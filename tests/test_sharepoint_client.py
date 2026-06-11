"""Tests for SharePointTaskClient."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from skysync.errors import PermanentApiError, SchemaDriftError
from skysync.graph.sharepoint_client import SharePointTaskClient
from skysync.models import CanonicalTask

from .fakes_graph import FakeGraphSession

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SITE_ID = "site-123"
LIST_ID = "list-456"
ITEMS_URL = f"{GRAPH_BASE}/sites/{SITE_ID}/lists/{LIST_ID}/items"

SP_ASSIGNEES = {
    "alice": "Alice Smith",
    "bob": "Bob Jones",
}


def _sp_item(
    item_id: str,
    title: str = "Task",
    notes: str = "",
    due_date: str | None = None,
    assignee: str | None = None,
    status: str = "open",
    internal_id: str | None = None,
    lm: str = "2026-06-01T10:00:00Z",
) -> dict:
    fields: dict[str, Any] = {
        "Title": title,
        "Notes": notes,
        "Status": status,
    }
    if due_date:
        fields["DueDate"] = due_date
    if assignee:
        fields["Assignee"] = assignee
    if internal_id:
        fields["InternalId"] = internal_id
    return {
        "id": item_id,
        "lastModifiedDateTime": lm,
        "fields": fields,
    }


def _make_client(routes: dict) -> tuple[SharePointTaskClient, FakeGraphSession]:
    session = FakeGraphSession(routes)
    client = SharePointTaskClient(session, SITE_ID, LIST_ID, SP_ASSIGNEES)
    return client, session


# ===========================================================================
# 1. list_tasks — basic
# ===========================================================================


def test_list_tasks_empty():
    client, _ = _make_client({("GET", ITEMS_URL): {"value": []}})
    tasks = client.list_tasks()
    assert tasks == []


def test_list_tasks_returns_remote_tasks():
    item = _sp_item("i1", title="Buy groceries")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].remote_id == "i1"
    assert tasks[0].side == "sp"


# ===========================================================================
# 2. Canonical normalization
# ===========================================================================


def test_canonical_title_stripped():
    item = _sp_item("i2", title="  Hello  ", notes="  World  ")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.title == "Hello"
    assert tasks[0].task.notes == "World"


def test_canonical_missing_title_defaults():
    item = {"id": "i3", "lastModifiedDateTime": "2026-06-01T00:00:00Z", "fields": {}}
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.title == ""
    assert tasks[0].task.notes == ""


def test_canonical_due_date_parsed():
    item = _sp_item("i4", due_date="2026-09-15T00:00:00Z")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.due_date == datetime.date(2026, 9, 15)


def test_canonical_due_date_none_when_missing():
    item = _sp_item("i5")  # no DueDate
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.due_date is None


def test_canonical_status_completed():
    item = _sp_item("i6", status="completed")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.status == "completed"


def test_canonical_status_completed_case_insensitive():
    item = _sp_item("i7", status="Completed")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.status == "completed"


def test_canonical_status_open_for_other():
    item = _sp_item("i8", status="In Progress")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.status == "open"


# ===========================================================================
# 3. Assignee reverse mapping
# ===========================================================================


def test_assignee_mapped_by_display_value():
    """'Alice Smith' in Assignee column -> 'alice' child key."""
    item = _sp_item("i10", assignee="Alice Smith")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.assignee == "alice"


def test_assignee_mapped_case_insensitive():
    """'alice smith' (lower) should still map to 'alice'."""
    item = _sp_item("i11", assignee="alice smith")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.assignee == "alice"


def test_assignee_mapped_by_key():
    """If the raw value equals a child key, it should resolve to that key."""
    item = _sp_item("i12", assignee="bob")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.assignee == "bob"


def test_assignee_unmapped_value_lowercased():
    """Unknown assignee value -> lowercase of the raw string."""
    item = _sp_item("i13", assignee="Charlie Brown")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.assignee == "charlie brown"


def test_assignee_missing_is_none():
    item = _sp_item("i14")  # no Assignee field
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.assignee is None


def test_assignee_empty_string_is_none():
    item = _sp_item("i15")
    item["fields"]["Assignee"] = ""
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].task.assignee is None


# ===========================================================================
# 4. InternalId / marker
# ===========================================================================


def test_internal_id_extracted():
    item = _sp_item("i20", internal_id="iid-20")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].marker_internal_id == "iid-20"


def test_internal_id_none_when_missing():
    item = _sp_item("i21")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    tasks = client.list_tasks()
    assert tasks[0].marker_internal_id is None


# ===========================================================================
# 5. Pagination
# ===========================================================================


def test_pagination_nextlink():
    page1 = {
        "value": [_sp_item("p1", title="Page1")],
        "@odata.nextLink": f"{ITEMS_URL}?$skiptoken=xyz",
    }
    page2 = {"value": [_sp_item("p2", title="Page2")]}
    routes = {
        ("GET", ITEMS_URL): page1,
        ("GET", f"{ITEMS_URL}?$skiptoken=xyz"): page2,
    }
    client, _ = _make_client(routes)
    tasks = client.list_tasks()
    ids = {rt.remote_id for rt in tasks}
    assert ids == {"p1", "p2"}


# ===========================================================================
# 6. create_task
# ===========================================================================


def test_create_task_payload_shape():
    response = {
        "id": "new-1",
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        "fields": {
            "Title": "New task",
            "Notes": "some notes",
            "DueDate": "2026-09-01T00:00:00Z",
            "Assignee": "Alice Smith",
            "Status": "open",
            "InternalId": "iid-c1",
        },
    }
    routes = {("POST", ITEMS_URL): response}
    client, session = _make_client(routes)
    task = CanonicalTask(
        title="New task",
        notes="some notes",
        due_date=datetime.date(2026, 9, 1),
        assignee="alice",
        status="open",
    )
    rt = client.create_task(task, "iid-c1")
    assert rt.remote_id == "new-1"

    post_call = next(c for c in session.calls if c["method"] == "POST")
    fields = post_call["json"]["fields"]
    assert fields["Title"] == "New task"
    assert fields["Notes"] == "some notes"
    assert fields["DueDate"] == "2026-09-01T00:00:00Z"
    assert fields["Assignee"] == "Alice Smith"  # display value
    assert fields["Status"] == "open"
    assert fields["InternalId"] == "iid-c1"


def test_create_task_no_due_date_omits_field():
    response = {
        "id": "new-2",
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        "fields": {"Title": "No due", "Status": "open", "InternalId": "iid-c2"},
    }
    routes = {("POST", ITEMS_URL): response}
    client, session = _make_client(routes)
    task = CanonicalTask(title="No due", assignee=None)
    client.create_task(task, "iid-c2")
    post_call = next(c for c in session.calls if c["method"] == "POST")
    assert "DueDate" not in post_call["json"]["fields"]


def test_create_task_no_assignee_omits_field():
    response = {
        "id": "new-3",
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        "fields": {"Title": "Unassigned", "Status": "open", "InternalId": "iid-c3"},
    }
    routes = {("POST", ITEMS_URL): response}
    client, session = _make_client(routes)
    task = CanonicalTask(title="Unassigned", assignee=None)
    client.create_task(task, "iid-c3")
    post_call = next(c for c in session.calls if c["method"] == "POST")
    assert "Assignee" not in post_call["json"]["fields"]


def test_create_task_unmapped_assignee_uses_raw_value():
    """An assignee not in sp_assignees is passed as-is (lowercase canonical)."""
    response = {
        "id": "new-4",
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        "fields": {"Title": "Charlie task", "Assignee": "charlie", "Status": "open", "InternalId": "iid-c4"},
    }
    routes = {("POST", ITEMS_URL): response}
    client, session = _make_client(routes)
    task = CanonicalTask(title="Charlie task", assignee="charlie")
    client.create_task(task, "iid-c4")
    post_call = next(c for c in session.calls if c["method"] == "POST")
    assert post_call["json"]["fields"]["Assignee"] == "charlie"


# ===========================================================================
# 7. update_task
# ===========================================================================


def test_update_task_patches_fields_url():
    item = _sp_item("u1", title="Old title", internal_id="iid-u1")
    routes = {
        ("GET", ITEMS_URL): {"value": [item]},
        ("PATCH", f"{ITEMS_URL}/u1/fields"): {"Title": "New title"},
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="New title", assignee="alice")
    rt = client.update_task("u1", task, "iid-u1")
    assert rt.remote_id == "u1"
    patch_call = next(c for c in session.calls if c["method"] == "PATCH")
    assert "/u1/fields" in patch_call["url"]
    assert patch_call["json"]["Title"] == "New title"


def test_update_task_clears_due_date():
    item = _sp_item("u2", due_date="2026-08-01T00:00:00Z", internal_id="iid-u2")
    routes = {
        ("GET", ITEMS_URL): {"value": [item]},
        ("PATCH", f"{ITEMS_URL}/u2/fields"): {"Title": "Task"},
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="Task", due_date=None, assignee=None)
    client.update_task("u2", task, "iid-u2")
    patch_call = next(c for c in session.calls if c["method"] == "PATCH")
    assert patch_call["json"]["DueDate"] is None


def test_update_task_clears_assignee():
    item = _sp_item("u3", assignee="Alice Smith", internal_id="iid-u3")
    routes = {
        ("GET", ITEMS_URL): {"value": [item]},
        ("PATCH", f"{ITEMS_URL}/u3/fields"): {"Title": "Task"},
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="Task", assignee=None)
    client.update_task("u3", task, "iid-u3")
    patch_call = next(c for c in session.calls if c["method"] == "PATCH")
    assert patch_call["json"]["Assignee"] is None


def test_update_task_always_reasserts_internal_id():
    item = _sp_item("u4", internal_id="iid-u4")
    routes = {
        ("GET", ITEMS_URL): {"value": [item]},
        ("PATCH", f"{ITEMS_URL}/u4/fields"): {"Title": "Task"},
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="Task", assignee=None)
    client.update_task("u4", task, "iid-u4")
    patch_call = next(c for c in session.calls if c["method"] == "PATCH")
    assert patch_call["json"]["InternalId"] == "iid-u4"


def test_update_task_returns_remote_task_with_desired_state():
    """PATCH returns only fields; RemoteTask is built from desired state."""
    item = _sp_item("u5", title="Old", internal_id="iid-u5")
    routes = {
        ("GET", ITEMS_URL): {"value": [item]},
        ("PATCH", f"{ITEMS_URL}/u5/fields"): {"Title": "Updated"},
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(
        title="Updated",
        notes="new notes",
        due_date=datetime.date(2026, 10, 1),
        assignee="bob",
        status="completed",
    )
    rt = client.update_task("u5", task, "iid-u5")
    assert rt.task.title == "Updated"
    assert rt.task.notes == "new notes"
    assert rt.task.due_date == datetime.date(2026, 10, 1)
    assert rt.task.assignee == "bob"
    assert rt.task.status == "completed"
    assert rt.marker_internal_id == "iid-u5"
    assert rt.last_modified is None  # PATCH doesn't return this


# ===========================================================================
# 8. delete_task — 404 tolerated
# ===========================================================================


def test_delete_task_success():
    item = _sp_item("d1")
    routes = {
        ("GET", ITEMS_URL): {"value": [item]},
        ("DELETE", f"{ITEMS_URL}/d1"): None,
    }
    client, session = _make_client(routes)
    client.list_tasks()
    client.delete_task("d1")
    delete_calls = [c for c in session.calls if c["method"] == "DELETE"]
    assert len(delete_calls) == 1


def test_delete_task_404_tolerated():
    item = _sp_item("d2")
    routes = {("GET", ITEMS_URL): {"value": [item]}}
    client, session = _make_client(routes)
    client.list_tasks()
    from skysync.errors import PermanentApiError as PAE

    def fake_delete(url, *, what):
        raise PAE("not found", status=404)

    session.delete = fake_delete
    client.delete_task("d2")  # must not raise


def test_delete_task_non_404_propagates():
    item = _sp_item("d3")
    routes = {("GET", ITEMS_URL): {"value": [item]}}
    client, session = _make_client(routes)
    client.list_tasks()
    from skysync.errors import PermanentApiError as PAE

    def fake_delete(url, *, what):
        raise PAE("forbidden", status=403)

    session.delete = fake_delete
    with pytest.raises(PAE):
        client.delete_task("d3")


# ===========================================================================
# 9. find_by_marker / find_recovery_candidate
# ===========================================================================


def test_find_by_marker_found():
    item = _sp_item("f1", internal_id="iid-f1")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    result = client.find_by_marker("iid-f1")
    assert result is not None
    assert result.remote_id == "f1"


def test_find_by_marker_not_found():
    item = _sp_item("f2")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    result = client.find_by_marker("nonexistent")
    assert result is None


def test_find_recovery_candidate_delegates():
    item = _sp_item("f3", internal_id="iid-f3")
    client, _ = _make_client({("GET", ITEMS_URL): {"value": [item]}})
    task = CanonicalTask(title="any")
    result = client.find_recovery_candidate(task, "iid-f3")
    assert result is not None
    assert result.remote_id == "f3"


# ===========================================================================
# 10. project()
# ===========================================================================


def test_project_all_fields():
    client, _ = _make_client({("GET", ITEMS_URL): {"value": []}})
    task = CanonicalTask(
        title="  My Task  ",
        notes="  Notes  ",
        due_date=datetime.date(2026, 11, 1),
        assignee="alice",
        status="completed",
    )
    proj = client.project(task)
    assert proj["title"] == "My Task"
    assert proj["notes"] == "Notes"
    assert proj["due_date"] == "2026-11-01"
    assert proj["assignee"] == "alice"
    assert proj["status"] == "completed"


def test_project_due_date_none():
    client, _ = _make_client({("GET", ITEMS_URL): {"value": []}})
    task = CanonicalTask(title="No due", due_date=None)
    proj = client.project(task)
    assert proj["due_date"] is None


# ===========================================================================
# 11. DueDate parse/format round-trip
# ===========================================================================


def test_due_date_round_trip():
    """Create then list: DueDate format should parse back to the same date."""
    original_date = datetime.date(2026, 12, 25)
    response = {
        "id": "rt1",
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        "fields": {
            "Title": "Christmas",
            "Status": "open",
            "InternalId": "iid-rt1",
            "DueDate": f"{original_date.isoformat()}T00:00:00Z",
        },
    }
    routes = {
        ("POST", ITEMS_URL): response,
        ("GET", ITEMS_URL): {"value": [response]},
    }
    client, session = _make_client(routes)
    # Create
    task = CanonicalTask(title="Christmas", due_date=original_date)
    rt_created = client.create_task(task, "iid-rt1")
    assert rt_created.task.due_date == original_date

    # Verify the sent format
    post_call = next(c for c in session.calls if c["method"] == "POST")
    sent_due = post_call["json"]["fields"]["DueDate"]
    assert sent_due == "2026-12-25T00:00:00Z"

    # List back — same date
    tasks = client.list_tasks()
    rt_listed = next(t for t in tasks if t.remote_id == "rt1")
    assert rt_listed.task.due_date == original_date


# ===========================================================================
# 12. SchemaDriftError on malformed payloads
# ===========================================================================


def test_schema_drift_missing_value_key():
    routes = {("GET", ITEMS_URL): {"wrongkey": []}}
    client, _ = _make_client(routes)
    with pytest.raises(SchemaDriftError):
        client.list_tasks()


def test_schema_drift_item_missing_id():
    bad_item = {
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        "fields": {"Title": "No id"},
    }
    routes = {("GET", ITEMS_URL): {"value": [bad_item]}}
    client, _ = _make_client(routes)
    with pytest.raises(SchemaDriftError, match="'id'"):
        client.list_tasks()


def test_schema_drift_item_missing_fields():
    bad_item = {
        "id": "bf1",
        "lastModifiedDateTime": "2026-06-01T00:00:00Z",
        # No 'fields' key
    }
    routes = {("GET", ITEMS_URL): {"value": [bad_item]}}
    client, _ = _make_client(routes)
    with pytest.raises(SchemaDriftError, match="'fields'"):
        client.list_tasks()


# ===========================================================================
# 13. representable / supports
# ===========================================================================


def test_representable_always_true():
    client, _ = _make_client({("GET", ITEMS_URL): {"value": []}})
    assert client.representable("assignee", "anyone") is True
    assert client.representable("assignee", None) is True
    assert client.representable("title", "x") is True


def test_supports_always_true():
    client, _ = _make_client({("GET", ITEMS_URL): {"value": []}})
    assert client.supports(CanonicalTask(title="x")) is True


def test_side_is_sp():
    client, _ = _make_client({("GET", ITEMS_URL): {"value": []}})
    assert client.side == "sp"


def test_projection_fields():
    client, _ = _make_client({("GET", ITEMS_URL): {"value": []}})
    assert set(client.PROJECTION_FIELDS) == {"title", "notes", "due_date", "assignee", "status"}
