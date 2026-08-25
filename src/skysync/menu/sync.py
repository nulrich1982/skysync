"""Write school lunch menus to Skylight as all-day calendar events.

One all-day event per school day per child, on that child's profile:

    summary      "🍴 Chicken Nuggets"          (the day's hot entrée/specials)
    description  every entrée option + sides, then a marker line

Idempotency: each event we own carries a marker + content hash in its
description. On every run we read the existing events in range and
  * create  days we have no event for,
  * update  days whose menu changed (hash differs),
  * skip    days that already match (the common case — zero writes),
  * delete  our events for days that vanished from the published menu.
Events we did not create (school concerts, birthdays...) are never touched.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from ..errors import ConfigError
from .fdmealplanner import DayMenu, FDMealPlannerClient, mark_daily_specials

log = logging.getLogger(__name__)

MARKER = "[skysync-menu]"
_MARKER_PREFIX = "[skysync-menu "


@dataclass
class MenuRunReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped_children: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)  # dry-run descriptions

    def summary(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "skipped_children": self.skipped_children,
        }


def build_description(day: DayMenu, standing: set[str]) -> str:
    """Full menu detail for the event body."""
    lines: list[str] = []
    if day.entrees:
        lines.append("Entrées:")
        for i, e in enumerate(day.entrees, 1):
            tag = "" if e not in standing else "  (always available)"
            lines.append(f"  {i}. {e}{tag}")
    if day.sides:
        lines.append("")
        lines.append("Sides: " + ", ".join(day.sides))
    return "\n".join(lines)


def content_hash(summary: str, description: str) -> str:
    return hashlib.sha256(f"{summary}\x00{description}".encode("utf-8")).hexdigest()[:12]


def stamp(description: str, digest: str) -> str:
    return f"{description}\n\n{_MARKER_PREFIX}{digest}]"


def parse_stamp(description: str | None) -> str | None:
    """Return the content hash if this description is one of ours."""
    if not description or _MARKER_PREFIX not in description:
        return None
    tail = description.rsplit(_MARKER_PREFIX, 1)[1]
    return tail.split("]", 1)[0].strip() or None


def _all_day_bounds(date: datetime.date, tz: str) -> tuple[str, str]:
    """Skylight all-day events run local-midnight to next local-midnight."""
    zone = ZoneInfo(tz)
    start = datetime.datetime.combine(date, datetime.time(0, 0), tzinfo=zone)
    end = start + datetime.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S.000%z"
    def iso(dt: datetime.datetime) -> str:
        s = dt.strftime(fmt)
        return s[:-2] + ":" + s[-2:]  # +0000 -> +00:00
    return iso(start), iso(end)


class MenuSync:
    def __init__(
        self,
        api,
        frame_id: str,
        *,
        timezone: str = "America/New_York",
        title_prefix: str = "🍴 ",
        dry_run: bool = False,
    ) -> None:
        self.api = api
        self.frame_id = frame_id
        self.timezone = timezone
        self.title_prefix = title_prefix
        self.dry_run = dry_run
        self._categories: dict[str, str] | None = None

    def _category_id(self, label: str) -> str:
        if self._categories is None:
            self._categories = {
                (c.attributes.label or "").strip().lower(): c.id for c in self.api.get_categories()
            }
        cid = self._categories.get(label.strip().lower())
        if not cid:
            raise ConfigError(
                f"Skylight category {label!r} not found on the frame; have: "
                f"{sorted(self._categories)}"
            )
        return cid

    def _existing(self, category_id: str, start: datetime.date, end: datetime.date) -> dict[datetime.date, dict]:
        """Our menu events in ``[start, end]``, keyed by date.

        NOTE: Skylight's ``date_max`` is EXCLUSIVE — querying date_max=the last
        menu day silently omits that day, which duplicated it on every run. We
        query one day past the end and then filter back to the real window, so
        the extra day can never be considered for deletion.
        """
        resp = self.api._request(
            "GET",
            f"/frames/{self.frame_id}/calendar_events",
            params={
                "date_min": start.isoformat(),
                "date_max": (end + datetime.timedelta(days=1)).isoformat(),
                "timezone": self.timezone,
            },
        )
        out: dict[datetime.date, dict] = {}
        for ev in resp.json().get("data") or []:
            attrs = ev.get("attributes") or {}
            digest = parse_stamp(attrs.get("description"))
            if not digest:
                continue  # not ours — never touch
            cat = ((ev.get("relationships") or {}).get("category") or {}).get("data") or {}
            if str(cat.get("id")) != str(category_id):
                continue
            starts = attrs.get("starts_at") or ""
            try:
                d = datetime.date.fromisoformat(starts[:10])
            except ValueError:
                continue
            if not (start <= d <= end):
                continue  # the extra probe day — outside our managed window
            entry = {"id": ev.get("id"), "digest": digest}
            if d in out:
                # More than one menu event for the same day+child: keep the
                # first, mark the rest for removal (self-heals any duplicates).
                out[d].setdefault("duplicates", []).append(entry)
            else:
                out[d] = entry
        return out

    def sync_child(
        self,
        child: str,
        location_id: int,
        category_label: str,
        months: list[tuple[int, int]],
        report: MenuRunReport,
        client: FDMealPlannerClient | None = None,
    ) -> None:
        client = client or FDMealPlannerClient()
        category_id = self._category_id(category_label)

        days: list[DayMenu] = []
        for year, month in months:
            days.extend(client.fetch_month(location_id, year, month))
        if not days:
            log.info("menu: no published days for %s (loc %s) — nothing to do", child, location_id)
            report.skipped_children.append(child)
            return
        standing = mark_daily_specials(days)

        # The window we MANAGE is the whole span of months being synced, not
        # just the days that came back. Otherwise an event for a day the school
        # later un-publishes falls outside the range and is never cleaned up.
        lo = datetime.date(months[0][0], months[0][1], 1)
        last_y, last_m = months[-1]
        hi = (
            datetime.date(last_y + (last_m == 12), (last_m % 12) + 1, 1)
            - datetime.timedelta(days=1)
        )
        existing = self._existing(category_id, lo, hi)
        seen: set[datetime.date] = set()

        for day in days:
            seen.add(day.date)
            headline = day.headline()
            if not headline:
                continue
            summary = f"{self.title_prefix}{headline}"
            body = build_description(day, standing)
            digest = content_hash(summary, body)
            described = stamp(body, digest)
            starts_at, ends_at = _all_day_bounds(day.date, self.timezone)
            payload = {
                "summary": summary,
                "description": described,
                "all_day": True,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "timezone": self.timezone,
                "category_id": str(category_id),
            }
            found = existing.get(day.date)
            for extra in (found or {}).get("duplicates", []):
                if self.dry_run:
                    report.planned.append(f"DELETE {child} {day.date} (duplicate)")
                else:
                    self.api._request(
                        "DELETE", f"/frames/{self.frame_id}/calendar_events/{extra['id']}"
                    )
                report.deleted += 1
            if found is None:
                if self.dry_run:
                    report.planned.append(f"CREATE {child} {day.date} {summary}")
                else:
                    self.api._request("POST", f"/frames/{self.frame_id}/calendar_events", json=payload)
                report.created += 1
            elif found["digest"] != digest:
                if self.dry_run:
                    report.planned.append(f"UPDATE {child} {day.date} -> {summary}")
                else:
                    self.api._request(
                        "PUT", f"/frames/{self.frame_id}/calendar_events/{found['id']}", json=payload
                    )
                report.updated += 1
            else:
                report.unchanged += 1

        for date, ev in existing.items():
            if date in seen:
                continue
            if self.dry_run:
                report.planned.append(f"DELETE {child} {date} (no longer published)")
            else:
                self.api._request("DELETE", f"/frames/{self.frame_id}/calendar_events/{ev['id']}")
            report.deleted += 1


def months_to_sync(today: datetime.date, months_ahead: int) -> list[tuple[int, int]]:
    """(year, month) for the current month plus N following."""
    out = []
    y, m = today.year, today.month
    for _ in range(months_ahead + 1):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out
