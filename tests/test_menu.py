"""School-lunch menu: FDMealPlanner parsing + Skylight event sync."""

from __future__ import annotations

import datetime

import pytest

from skysync.errors import ConfigError, SchemaDriftError
from skysync.menu.fdmealplanner import DayMenu, FDMealPlannerClient, mark_daily_specials
from skysync.menu.sync import (
    MenuRunReport,
    MenuSync,
    _all_day_bounds,
    build_description,
    content_hash,
    months_to_sync,
    parse_stamp,
    stamp,
)


def _row(date: str, items: list[tuple[str, int, int]]) -> dict:
    return {
        "strMenuForDate": date,
        "menuRecipiesData": [
            {"componentEnglishName": n, "isEntreeType": e, "sequenceNumber": s}
            for n, e, s in items
        ],
    }


ELEM_DAY = _row(
    "2026-09-01",
    [("Chicken Nuggets", 1, 1), ("Not-A-Nut Butter & Jelly Sandwich", 1, 2),
     ("Garden Salad with Cheese Entree", 1, 3), ("Oven Baked Fries", 0, 4), ("Fresh Apple", 0, 5)],
)


# ------------------------------------------------------------------ parsing --

def test_parse_day_splits_entrees_and_sides():
    d = FDMealPlannerClient._parse_day(ELEM_DAY)
    assert d.date == datetime.date(2026, 9, 1)
    assert d.entrees == [
        "Chicken Nuggets", "Not-A-Nut Butter & Jelly Sandwich", "Garden Salad with Cheese Entree",
    ]
    assert d.sides == ["Oven Baked Fries", "Fresh Apple"]


def test_parse_day_orders_by_sequence():
    d = FDMealPlannerClient._parse_day(
        _row("2026-09-01", [("Second", 1, 2), ("First", 1, 1)])
    )
    assert d.entrees == ["First", "Second"]


def test_parse_day_missing_items_is_schema_drift():
    with pytest.raises(SchemaDriftError):
        FDMealPlannerClient._parse_day({"strMenuForDate": "2026-09-01"})


def test_parse_day_bad_date_is_schema_drift():
    with pytest.raises(SchemaDriftError):
        FDMealPlannerClient._parse_day({"strMenuForDate": "nonsense", "menuRecipiesData": []})


def test_fetch_month_validates_envelope(monkeypatch):
    c = FDMealPlannerClient()

    class R:
        status_code = 200
        text = "{}"
        def json(self): return {"unexpected": 1}

    monkeypatch.setattr("skysync.menu.fdmealplanner.retry_call", lambda fn, what: R())
    with pytest.raises(SchemaDriftError, match="result"):
        c.fetch_month(389, 2026, 9)


# ------------------------------------------------------- specials / headline --

def test_middle_school_specials_vs_standing():
    """19 always-available items + a couple of daily specials (Abigail's case)."""
    standing_names = [f"Standing {i}" for i in range(6)]
    days = [
        DayMenu(date=datetime.date(2026, 9, d), entrees=standing_names + [f"Special {d}"])
        for d in (1, 2, 3, 4, 7)
    ]
    standing = mark_daily_specials(days)
    assert standing == set(standing_names)
    assert days[0].specials == ["Special 1"]
    # >5 entrées -> auto mode shows the specials
    assert days[0].headline() == "Special 1"


def test_elementary_headline_is_first_entree():
    """Elementary rotates all 3 options, so nothing is 'standing' — show #1."""
    days = [
        DayMenu(date=datetime.date(2026, 9, 1), entrees=["Chicken Nuggets", "PB&J", "Salad"]),
        DayMenu(date=datetime.date(2026, 9, 2), entrees=["Pizza", "Bagel", "Hummus"]),
    ]
    mark_daily_specials(days)
    assert days[0].headline() == "Chicken Nuggets"
    assert days[1].headline() == "Pizza"


def test_headline_modes_and_cap():
    d = DayMenu(date=datetime.date(2026, 9, 1), entrees=["A", "B"], specials=["X", "Y", "Z", "W"])
    assert d.headline(mode="first") == "A"
    assert d.headline(mode="specials", max_items=2) == "X, Y"


def test_headline_empty_day():
    assert DayMenu(date=datetime.date(2026, 9, 1)).headline() == ""


# ------------------------------------------------------------ marker/hashing --

def test_stamp_roundtrip():
    body = build_description(
        DayMenu(date=datetime.date(2026, 9, 1), entrees=["Nuggets"], sides=["Fries"]), set()
    )
    h = content_hash("t", body)
    assert parse_stamp(stamp(body, h)) == h


def test_parse_stamp_ignores_foreign_events():
    assert parse_stamp(None) is None
    assert parse_stamp("Soccer practice") is None


def test_description_flags_always_available():
    d = DayMenu(date=datetime.date(2026, 9, 1), entrees=["Nuggets", "PB&J"], sides=["Fries"])
    body = build_description(d, {"PB&J"})
    assert "1. Nuggets" in body and "(always available)" in body
    assert "Sides: Fries" in body


def test_all_day_bounds_handles_dst():
    s, e = _all_day_bounds(datetime.date(2026, 9, 1), "America/New_York")  # EDT
    assert s == "2026-09-01T00:00:00.000-04:00" and e == "2026-09-02T00:00:00.000-04:00"
    s2, _ = _all_day_bounds(datetime.date(2026, 12, 1), "America/New_York")  # EST
    assert s2 == "2026-12-01T00:00:00.000-05:00"


def test_months_to_sync_wraps_year():
    assert months_to_sync(datetime.date(2026, 12, 5), 2) == [(2026, 12), (2027, 1), (2027, 2)]


# ------------------------------------------------------------------- syncing --

class FakeCat:
    def __init__(self, cid, label):
        self.id = cid
        self.attributes = type("A", (), {"label": label})()


class FakeApi:
    """Minimal SkylightApi stand-in recording calendar_event writes.

    Emulates Skylight's EXCLUSIVE ``date_max`` so the boundary-day bug that
    duplicated the final menu day every run stays fixed.
    """

    def __init__(self, existing=None):
        self.existing = existing or []
        self.calls = []

    def get_categories(self):
        return [FakeCat("1943073", "Madeline"), FakeCat("1943059", "Abigail")]

    def _request(self, method, path, json=None, params=None):
        self.calls.append((method, path, json))
        if method == "GET":
            lo = (params or {}).get("date_min", "0000-01-01")
            hi = (params or {}).get("date_max", "9999-12-31")
            visible = [
                e for e in self.existing
                if lo <= (e["attributes"]["starts_at"][:10]) < hi  # date_max exclusive
            ]
            payload = {"data": visible}
        else:
            payload = {"data": {"id": "new"}}
        return type("R", (), {"status_code": 200, "json": lambda self, p=payload: p})()


def _client_returning(days):
    class C:
        def fetch_month(self, loc, y, m):
            return list(days)
    return C()


def _menu_day():
    return DayMenu(date=datetime.date(2026, 9, 1), entrees=["Chicken Nuggets", "PB&J"], sides=["Fries"])


def test_sync_creates_missing_day():
    api = FakeApi()
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([_menu_day()])
    )
    assert (rep.created, rep.updated, rep.deleted) == (1, 0, 0)
    posts = [c for c in api.calls if c[0] == "POST"]
    assert len(posts) == 1
    body = posts[0][2]
    assert body["all_day"] is True
    assert body["category_id"] == "1943073"
    assert body["summary"].endswith("Chicken Nuggets")
    assert parse_stamp(body["description"])


def test_sync_is_idempotent_second_run():
    """An unchanged day must produce zero writes."""
    day = _menu_day()
    summary = "\U0001f374 Chicken Nuggets"
    body = build_description(day, set())
    digest = content_hash(summary, body)
    existing = [{
        "id": "e1",
        "attributes": {"description": stamp(body, digest), "starts_at": "2026-09-01T00:00:00.000-04:00"},
        "relationships": {"category": {"data": {"id": "1943073"}}},
    }]
    api = FakeApi(existing)
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([day])
    )
    assert (rep.created, rep.updated, rep.unchanged, rep.deleted) == (0, 0, 1, 0)
    assert [c for c in api.calls if c[0] in ("POST", "PUT", "DELETE")] == []


def test_sync_updates_changed_menu():
    existing = [{
        "id": "e1",
        "attributes": {"description": stamp("old", "deadbeef1234"), "starts_at": "2026-09-01T00:00:00.000-04:00"},
        "relationships": {"category": {"data": {"id": "1943073"}}},
    }]
    api = FakeApi(existing)
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([_menu_day()])
    )
    assert (rep.created, rep.updated) == (0, 1)
    assert [c[0] for c in api.calls if c[0] == "PUT"] == ["PUT"]


def test_sync_deletes_unpublished_day_but_not_foreign_events():
    existing = [
        {  # ours, for a day no longer published
            "id": "mine",
            "attributes": {"description": stamp("x", "aaaaaaaaaaaa"), "starts_at": "2026-09-02T00:00:00.000-04:00"},
            "relationships": {"category": {"data": {"id": "1943073"}}},
        },
        {  # someone else's event on the same profile — must be untouched
            "id": "foreign",
            "attributes": {"description": "Soccer practice", "starts_at": "2026-09-01T00:00:00.000-04:00"},
            "relationships": {"category": {"data": {"id": "1943073"}}},
        },
    ]
    api = FakeApi(existing)
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([_menu_day()])
    )
    assert rep.deleted == 1
    deletes = [c for c in api.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and "mine" in deletes[0][1]


def test_sync_ignores_other_childs_events():
    """An identical-looking menu event on Abigail's profile is not Madeline's."""
    existing = [{
        "id": "abby",
        "attributes": {"description": stamp("x", "bbbbbbbbbbbb"), "starts_at": "2026-09-01T00:00:00.000-04:00"},
        "relationships": {"category": {"data": {"id": "1943059"}}},
    }]
    api = FakeApi(existing)
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([_menu_day()])
    )
    assert rep.created == 1 and rep.deleted == 0


def test_dry_run_writes_nothing():
    api = FakeApi()
    rep = MenuRunReport()
    MenuSync(api, "1982998", dry_run=True).sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([_menu_day()])
    )
    assert rep.created == 1
    assert [c for c in api.calls if c[0] != "GET"] == []
    assert rep.planned and "CREATE" in rep.planned[0]


def test_unknown_category_raises_configerror():
    rep = MenuRunReport()
    with pytest.raises(ConfigError, match="not found"):
        MenuSync(FakeApi(), "1982998").sync_child(
            "nobody", 389, "Nonexistent", [(2026, 9)], rep, client=_client_returning([_menu_day()])
        )


def test_last_day_is_not_duplicated_on_rerun():
    """Regression: Skylight's date_max is exclusive, so the final menu day was
    invisible to the existing-events query and got re-created every run."""
    days = [
        DayMenu(date=datetime.date(2026, 9, 29), entrees=["Nuggets"], sides=[]),
        DayMenu(date=datetime.date(2026, 9, 30), entrees=["Pizza"], sides=[]),  # boundary
    ]
    existing = []
    for d, name in ((days[0], "Nuggets"), (days[1], "Pizza")):
        summary = f"\U0001f374 {name}"
        body = build_description(d, set())
        existing.append({
            "id": f"e{d.date.day}",
            "attributes": {
                "description": stamp(body, content_hash(summary, body)),
                "starts_at": f"{d.date.isoformat()}T00:00:00.000-04:00",
            },
            "relationships": {"category": {"data": {"id": "1943073"}}},
        })
    api = FakeApi(existing)
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning(days)
    )
    assert (rep.created, rep.updated, rep.unchanged, rep.deleted) == (0, 0, 2, 0)
    assert [c for c in api.calls if c[0] in ("POST", "PUT", "DELETE")] == []


def test_duplicate_events_self_heal():
    """Two menu events for the same day+child: keep one, delete the extra."""
    day = _menu_day()
    summary = "\U0001f374 Chicken Nuggets"
    body = build_description(day, set())
    digest = content_hash(summary, body)
    ev = lambda i: {
        "id": f"dup{i}",
        "attributes": {"description": stamp(body, digest), "starts_at": "2026-09-01T00:00:00.000-04:00"},
        "relationships": {"category": {"data": {"id": "1943073"}}},
    }
    api = FakeApi([ev(1), ev(2)])
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([day])
    )
    assert rep.deleted == 1 and rep.unchanged == 1 and rep.created == 0
    deletes = [c for c in api.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and "dup2" in deletes[0][1]


def test_probe_day_beyond_window_is_never_deleted():
    """The extra day we query past the end must not be treated as ours to delete."""
    day = DayMenu(date=datetime.date(2026, 9, 30), entrees=["Pizza"], sides=[])
    existing = [{  # ours, but in the NEXT month's window — out of scope this run
        "id": "october",
        "attributes": {"description": stamp("x", "cccccccccccc"), "starts_at": "2026-10-01T00:00:00.000-04:00"},
        "relationships": {"category": {"data": {"id": "1943073"}}},
    }]
    api = FakeApi(existing)
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([day])
    )
    assert rep.deleted == 0
    assert [c for c in api.calls if c[0] == "DELETE"] == []


def test_no_published_days_is_noop():
    api = FakeApi()
    rep = MenuRunReport()
    MenuSync(api, "1982998").sync_child(
        "madeline", 389, "Madeline", [(2026, 9)], rep, client=_client_returning([])
    )
    assert rep.skipped_children == ["madeline"] and api.calls == []
