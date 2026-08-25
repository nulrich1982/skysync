"""FDMealPlanner client — school lunch menus.

The district (Sudbury, accountId 71) publishes menus through FDMealPlanner
(Whitsons Culinary Group). The **v1** data-locator API is public: no token, no
login. (The newer v2 API requires a Bearer token; v1 returns the same menu data
without one, so we use v1 deliberately.)

One request returns a whole month of school days, with each day's items already
parsed as JSON in ``menuRecipiesData`` — no XML parsing needed.

Item shape (fields we use):
    componentEnglishName  "Chicken Nuggets"
    isEntreeType          1 = an entrée choice, 0 = side/drink/condiment
    sequenceNumber        display order

NOTE: every item carries ``IsShowOnMenu=0`` for this district, so the filter
used by some other integrations would discard everything — we filter on
``isEntreeType`` instead.
"""

from __future__ import annotations

import datetime
import logging
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field

import requests

from ..errors import SchemaDriftError
from ..retry import retry_call

log = logging.getLogger(__name__)

BASE = "https://apiservicelocators.fdmealplanner.com/api/v1/data-locator-webapi"
TENANT_ID = 3
MEAL_PERIOD_LUNCH = 2

# Cloudflare-ish friendliness + the JSON casing the app requests.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "accept": "application/json",
    "x-jsonresponsecase": "camel",
    "Origin": "https://www.fdmealplanner.com",
    "Referer": "https://www.fdmealplanner.com/",
}


@dataclass
class DayMenu:
    """One school day's lunch."""

    date: datetime.date
    entrees: list[str] = field(default_factory=list)
    sides: list[str] = field(default_factory=list)
    # Entrées unique to this day (i.e. not on the always-available standing
    # menu). Populated by ``mark_daily_specials``.
    specials: list[str] = field(default_factory=list)

    def headline(self, mode: str = "auto", max_items: int = 3) -> str:
        """Title text for this day.

        Two very different menu styles exist in one district:

        * elementary (Haynes) publishes ~3 rotating options a day, where
          option 1 is *the* hot entrée — so show the first entrée;
        * middle school publishes ~19 always-available items plus 2-4 daily
          specials — so show the specials.

        ``auto`` picks per day by how many entrées there are, which handles
        both without per-school configuration.
        """
        if mode == "auto":
            mode = "specials" if len(self.entrees) > 5 else "first"
        if mode == "specials" and self.specials:
            return ", ".join(self.specials[:max_items])
        return self.entrees[0] if self.entrees else ""


class FDMealPlannerClient:
    def __init__(
        self,
        account_id: int = 71,
        tenant_id: int = TENANT_ID,
        meal_period_id: int = MEAL_PERIOD_LUNCH,
        timeout: float = 30.0,
    ) -> None:
        self.account_id = account_id
        self.tenant_id = tenant_id
        self.meal_period_id = meal_period_id
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    def fetch_month(self, location_id: int, year: int, month: int) -> list[DayMenu]:
        """Return every published school day in the given month."""
        start = datetime.date(year, month, 1)
        end = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
        sq = urllib.parse.quote(start.strftime("%m %d %Y"))
        eq = urllib.parse.quote(end.strftime("%m %d %Y"))
        url = (
            f"{BASE}/{self.tenant_id}/meals?accountId={self.account_id}&endDate={eq}"
            f"&isActive=true&isStandalone&locationId={location_id}"
            f"&mealPeriodId={self.meal_period_id}&menuId=0&monthId={month}"
            f"&selectedDate={sq}&startDate={sq}&tenantId={self.tenant_id}"
            f"&timeOffset=300&year={year}"
        )
        resp = retry_call(
            lambda: self._session.get(url, timeout=self.timeout),
            what=f"GET fdmealplanner meals loc={location_id} {year}-{month:02d}",
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SchemaDriftError(
                "FDMealPlanner returned non-JSON", resp.text[:200]
            ) from exc
        if "result" not in payload:
            raise SchemaDriftError(
                "FDMealPlanner response missing 'result'", str(payload)[:200]
            )

        days: list[DayMenu] = []
        for row in payload["result"] or []:
            day = self._parse_day(row)
            if day is not None:
                days.append(day)
        days.sort(key=lambda d: d.date)
        log.info(
            "fdmealplanner: loc=%s %s-%02d -> %d school days", location_id, year, month, len(days)
        )
        return days

    @staticmethod
    def _parse_day(row: dict) -> DayMenu | None:
        raw_date = row.get("strMenuForDate")
        if not raw_date:
            return None
        try:
            date = datetime.date.fromisoformat(raw_date[:10])
        except ValueError as exc:
            raise SchemaDriftError("Unparseable menu date", str(raw_date)[:60]) from exc

        items = row.get("menuRecipiesData")
        if items is None:
            raise SchemaDriftError(
                "FDMealPlanner day missing 'menuRecipiesData'", str(list(row.keys()))[:200]
            )
        entrees, sides = [], []
        for it in sorted(items, key=lambda x: x.get("sequenceNumber") or 0):
            name = (it.get("componentEnglishName") or it.get("componentName") or "").strip()
            if not name:
                continue
            (entrees if it.get("isEntreeType") == 1 else sides).append(name)
        if not entrees and not sides:
            return None
        return DayMenu(date=date, entrees=entrees, sides=sides)


def mark_daily_specials(days: list[DayMenu], standing_threshold: float = 0.8) -> set[str]:
    """Split each day's entrées into 'specials' vs the standing menu.

    Both schools list always-available entrées every day (PB&J, garden salad;
    at the middle school also pizza, burgers, wraps...). Those swamp a calendar
    title, so we treat an entrée appearing on >= ``standing_threshold`` of days
    as standing, and whatever remains as that day's specials. Returns the
    standing set (useful for the event description).
    """
    if not days:
        return set()
    counts = Counter(name for d in days for name in d.entrees)
    cutoff = max(2, len(days) * standing_threshold)
    standing = {name for name, c in counts.items() if c >= cutoff}
    for d in days:
        d.specials = [n for n in d.entrees if n not in standing]
    return standing
