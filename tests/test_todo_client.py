"""Tests for TodoTaskClient."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from skysync.errors import ConfigError, PermanentApiError, SchemaDriftError
from skysync.graph.todo_client import TodoTaskClient
from skysync.models import CanonicalTask

from .fakes_graph import FakeGraphSession

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

TODO_BASE = "https://graph.microsoft.com/v1.0/me/todo/lists"

CHILD_LISTS = {"alice": "Alice Tasks", "bob": "Bob Tasks"}
DEFAULT_LIST = "Shared Tasks"


def _lists_response() -> dict:
    """Fake GET /me/todo/lists response."""
    return {
        "value": [
            {"id": "list-alice", "displayName": "Alice Tasks", "wellknownListName": "none"},
            {"id": "list-bob", "displayName": "Bob Tasks", "wellknownListName": "none"},
            {"id": "list-shared", "displayName": "Shared Tasks", "wellknownListName": "none"},
        ]
    }


def _task(
    task_id: str,
    title: str,
    status: str = "notStarted",
    body_content: str = "",
    due: str | None = None,
    linked_resources: list[dict] | None = None,
    lm: str = "2026-06-01T10:00:00Z",
) -> dict:
    raw: dict[str, Any] = {
        "id": task_id,
        "title": title,
        "status": status,
        "body": {"content": body_content, "contentType": "text"},
        "lastModifiedDateTime": lm,
    }
    if due:
        raw["dueDateTime"] = {"dateTime": due, "timeZone": "UTC"}
    if linked_resources is not None:
        raw["linkedResources"] = linked_resources
    return raw


def _skysync_lr(internal_id: str) -> dict:
    return {
        "id": "lr-1",
        "applicationName": "SkySync",
        "externalId": internal_id,
        "displayName": "SkySync sync marker",
    }


def _make_client(routes: dict) -> tuple[TodoTaskClient, FakeGraphSession]:
    session = FakeGraphSession(routes)
    client = TodoTaskClient(session, CHILD_LISTS, DEFAULT_LIST)
    return client, session


def _basic_routes(
    alice_tasks: list[dict] | None = None,
    bob_tasks: list[dict] | None = None,
    shared_tasks: list[dict] | None = None,
) -> dict:
    """Build routes with list resolution + per-list tasks."""
    routes: dict = {
        ("GET", TODO_BASE): _lists_response(),
        ("GET", f"{TODO_BASE}/list-alice/tasks"): {"value": alice_tasks or []},
        ("GET", f"{TODO_BASE}/list-bob/tasks"): {"value": bob_tasks or []},
        ("GET", f"{TODO_BASE}/list-shared/tasks"): {"value": shared_tasks or []},
    }
    return routes


# ===========================================================================
# 1. List resolution
# ===========================================================================


def test_list_resolution_caches():
    """list_ids resolved once even across multiple calls."""
    client, session = _make_client(_basic_routes())
    client.list_tasks()
    client.list_tasks()  # snapshot refresh; lists should NOT be re-fetched
    list_fetches = [c for c in session.calls if c["url"] == TODO_BASE]
    assert len(list_fetches) == 1


def test_unknown_child_list_raises_config_error():
    """A child list name not present in Graph -> ConfigError."""
    routes = {
        ("GET", TODO_BASE): {
            "value": [
                {"id": "list-shared", "displayName": "Shared Tasks", "wellknownListName": "none"},
                # Alice Tasks is MISSING
                {"id": "list-bob", "displayName": "Bob Tasks", "wellknownListName": "none"},
            ]
        }
    }
    client, _ = _make_client(routes)
    with pytest.raises(ConfigError, match="Alice Tasks"):
        client.list_tasks()


def test_unknown_default_list_raises_config_error():
    """Default list name not present in Graph -> ConfigError."""
    routes = {
        ("GET", TODO_BASE): {
            "value": [
                {"id": "list-alice", "displayName": "Alice Tasks", "wellknownListName": "none"},
                {"id": "list-bob", "displayName": "Bob Tasks", "wellknownListName": "none"},
                # Shared Tasks is MISSING
            ]
        }
    }
    client, _ = _make_client(routes)
    with pytest.raises(ConfigError, match="Shared Tasks"):
        client.list_tasks()


# ===========================================================================
# 2. Canonical normalization
# ===========================================================================


def test_canonical_whitespace_title_stripped():
    t = _task("t1", "  Buy milk  ", body_content="  some notes  ")
    client, _ = _make_client(_basic_routes(alice_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t1")
    assert task.task.title == "Buy milk"
    assert task.task.notes == "some notes"


def test_canonical_missing_body_defaults_to_empty():
    raw = {
        "id": "t2",
        "title": "No body",
        "status": "notStarted",
        "lastModifiedDateTime": "2026-06-01T10:00:00Z",
    }
    client, _ = _make_client(_basic_routes(shared_tasks=[raw]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t2")
    assert task.task.notes == ""


def test_canonical_due_date_parsed():
    t = _task("t3", "Meeting", due="2026-07-15T00:00:00.0000000")
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t3")
    assert task.task.due_date == datetime.date(2026, 7, 15)


def test_canonical_due_date_none_when_missing():
    t = _task("t4", "No due")
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t4")
    assert task.task.due_date is None


def test_canonical_status_completed():
    t = _task("t5", "Done", status="completed")
    client, _ = _make_client(_basic_routes(alice_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t5")
    assert task.task.status == "completed"


def test_canonical_status_open_for_notstarted():
    t = _task("t6", "Todo", status="notStarted")
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t6")
    assert task.task.status == "open"


def test_canonical_assignee_from_child_list():
    t = _task("t7", "Alice task")
    client, _ = _make_client(_basic_routes(alice_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t7")
    assert task.task.assignee == "alice"


def test_canonical_assignee_none_for_default_list():
    t = _task("t8", "Shared task")
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t8")
    assert task.task.assignee is None


# ===========================================================================
# 3. Marker extraction
# ===========================================================================


def test_marker_extracted_from_linked_resources():
    lr = _skysync_lr("internal-abc")
    t = _task("t9", "Marked", linked_resources=[lr])
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t9")
    assert task.marker_internal_id == "internal-abc"


def test_marker_none_when_no_skysync_resource():
    lr = {"id": "lr-x", "applicationName": "SomeOtherApp", "externalId": "xyz", "displayName": "x"}
    t = _task("t10", "No marker", linked_resources=[lr])
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t10")
    assert task.marker_internal_id is None


def test_marker_none_when_no_linked_resources():
    t = _task("t11", "No LR")
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    tasks = client.list_tasks()
    task = next(rt for rt in tasks if rt.remote_id == "t11")
    assert task.marker_internal_id is None


# ===========================================================================
# 4. find_by_marker
# ===========================================================================


def test_find_by_marker_returns_task():
    lr = _skysync_lr("iid-42")
    t = _task("t12", "Findme", linked_resources=[lr])
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    result = client.find_by_marker("iid-42")
    assert result is not None
    assert result.remote_id == "t12"


def test_find_by_marker_returns_none_when_absent():
    t = _task("t13", "Not marked")
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    result = client.find_by_marker("nonexistent")
    assert result is None


# ===========================================================================
# 5. Pagination via @odata.nextLink
# ===========================================================================


def test_pagination_nextlink():
    page1 = {
        "value": [_task("p1", "Page1Task")],
        "@odata.nextLink": f"{TODO_BASE}/list-shared/tasks?$skiptoken=abc",
    }
    page2 = {"value": [_task("p2", "Page2Task")]}
    routes = {
        ("GET", TODO_BASE): _lists_response(),
        ("GET", f"{TODO_BASE}/list-alice/tasks"): {"value": []},
        ("GET", f"{TODO_BASE}/list-bob/tasks"): {"value": []},
        ("GET", f"{TODO_BASE}/list-shared/tasks"): page1,
        ("GET", f"{TODO_BASE}/list-shared/tasks?$skiptoken=abc"): page2,
    }
    client, _ = _make_client(routes)
    tasks = client.list_tasks()
    ids = {rt.remote_id for rt in tasks}
    assert ids == {"p1", "p2"}


# ===========================================================================
# 6. create_task payload shape
# ===========================================================================


def test_create_task_payload_shape_with_due_date():
    created_raw = _task(
        "new-1", "Do stuff", due="2026-08-01T00:00:00.0000000",
        linked_resources=[_skysync_lr("iid-new")]
    )
    routes = {
        **_basic_routes(),
        ("POST", f"{TODO_BASE}/list-shared/tasks"): created_raw,
    }
    client, session = _make_client(routes)
    client.list_tasks()  # seed snapshot

    task = CanonicalTask(
        title="Do stuff",
        notes="Some notes",
        due_date=datetime.date(2026, 8, 1),
        assignee=None,
        status="open",
    )
    rt = client.create_task(task, "iid-new")

    # Check POST was called with correct body
    post_call = next(c for c in session.calls if c["method"] == "POST")
    body = post_call["json"]
    assert body["title"] == "Do stuff"
    assert body["body"]["content"] == "Some notes"
    assert body["body"]["contentType"] == "text"
    assert body["status"] == "notStarted"
    assert body["dueDateTime"]["dateTime"] == "2026-08-01T00:00:00.0000000"
    assert body["dueDateTime"]["timeZone"] == "UTC"
    lr_list = body["linkedResources"]
    assert len(lr_list) == 1
    lr = lr_list[0]
    assert lr["applicationName"] == "SkySync"
    assert lr["externalId"] == "iid-new"

    # Check returned RemoteTask
    assert rt.remote_id == "new-1"
    assert rt.marker_internal_id == "iid-new"


def test_create_task_no_due_date():
    """dueDateTime key must NOT be present in POST body when no due date."""
    created_raw = _task("new-2", "No due task", linked_resources=[_skysync_lr("iid-nd")])
    routes = {
        **_basic_routes(),
        ("POST", f"{TODO_BASE}/list-shared/tasks"): created_raw,
    }
    client, session = _make_client(routes)
    client.list_tasks()

    task = CanonicalTask(title="No due task", assignee=None)
    client.create_task(task, "iid-nd")

    post_call = next(c for c in session.calls if c["method"] == "POST")
    assert "dueDateTime" not in post_call["json"]


def test_create_task_completed_status():
    created_raw = _task("new-3", "Done task", status="completed",
                        linked_resources=[_skysync_lr("iid-done")])
    routes = {
        **_basic_routes(),
        ("POST", f"{TODO_BASE}/list-shared/tasks"): created_raw,
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="Done task", status="completed", assignee=None)
    client.create_task(task, "iid-done")
    post_call = next(c for c in session.calls if c["method"] == "POST")
    assert post_call["json"]["status"] == "completed"


def test_create_task_routes_to_child_list_by_assignee():
    """create_task with assignee='bob' should POST to list-bob."""
    created_raw = _task("new-4", "Bob task", linked_resources=[_skysync_lr("iid-bob")])
    routes = {
        **_basic_routes(),
        ("POST", f"{TODO_BASE}/list-bob/tasks"): created_raw,
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="Bob task", assignee="bob")
    rt = client.create_task(task, "iid-bob")
    assert rt.remote_id == "new-4"
    post_call = next(c for c in session.calls if c["method"] == "POST")
    assert "/list-bob/tasks" in post_call["url"]


def test_create_task_marker_set_when_response_lacks_linked_resources():
    """If response has no linkedResources, marker_internal_id is set from sent id."""
    created_raw = _task("new-5", "No LR in response")  # no linkedResources key
    routes = {
        **_basic_routes(),
        ("POST", f"{TODO_BASE}/list-shared/tasks"): created_raw,
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="No LR in response", assignee=None)
    rt = client.create_task(task, "iid-fallback")
    assert rt.marker_internal_id == "iid-fallback"


# ===========================================================================
# 7. update_task — same list (PATCH)
# ===========================================================================


def test_update_task_same_list_patch():
    existing = _task("t20", "Old title", linked_resources=[_skysync_lr("iid-20")])
    patched = _task("t20", "New title")
    routes = {
        **_basic_routes(alice_tasks=[existing]),
        ("PATCH", f"{TODO_BASE}/list-alice/tasks/t20"): patched,
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="New title", assignee="alice")
    rt = client.update_task("t20", task, "iid-20")
    assert rt.remote_id == "t20"
    patch_call = next(c for c in session.calls if c["method"] == "PATCH")
    assert patch_call["json"]["title"] == "New title"


def test_update_task_clears_due_date_with_none():
    """When due_date is None the PATCH must send dueDateTime: null."""
    existing = _task("t21", "Has due", due="2026-08-01T00:00:00.0000000",
                     linked_resources=[_skysync_lr("iid-21")])
    patched = _task("t21", "Has due")
    routes = {
        **_basic_routes(alice_tasks=[existing]),
        ("PATCH", f"{TODO_BASE}/list-alice/tasks/t21"): patched,
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="Has due", assignee="alice", due_date=None)
    client.update_task("t21", task, "iid-21")
    patch_call = next(c for c in session.calls if c["method"] == "PATCH")
    assert "dueDateTime" in patch_call["json"]
    assert patch_call["json"]["dueDateTime"] is None


# ===========================================================================
# 8. update_task — assignee change triggers create+delete
# ===========================================================================


def test_update_task_assignee_change_creates_in_new_list_and_deletes_old():
    """Moving alice -> bob: create in list-bob, delete from list-alice."""
    existing = _task("t30", "Move me", linked_resources=[_skysync_lr("iid-30")])
    new_task_raw = _task("new-30", "Move me", linked_resources=[_skysync_lr("iid-30")])
    routes = {
        **_basic_routes(alice_tasks=[existing]),
        ("POST", f"{TODO_BASE}/list-bob/tasks"): new_task_raw,
        ("DELETE", f"{TODO_BASE}/list-alice/tasks/t30"): None,
    }
    client, session = _make_client(routes)
    client.list_tasks()
    task = CanonicalTask(title="Move me", assignee="bob")
    rt = client.update_task("t30", task, "iid-30")
    # New RemoteTask should have the new id
    assert rt.remote_id == "new-30"
    # Verify DELETE was called for old id
    delete_calls = [c for c in session.calls if c["method"] == "DELETE"]
    assert any("t30" in c["url"] for c in delete_calls)


# ===========================================================================
# 9. delete_task — 404 tolerated
# ===========================================================================


def test_delete_task_404_is_idempotent():
    existing = _task("t40", "To delete")
    routes = {
        **_basic_routes(shared_tasks=[existing]),
    }
    client, session = _make_client(routes)
    client.list_tasks()
    # Inject a 404 PermanentApiError by making delete raise it
    from skysync.errors import PermanentApiError as PAE

    original_delete = session.delete

    def fake_delete(url, *, what):
        raise PAE("not found", status=404)

    session.delete = fake_delete
    # Should not raise
    client.delete_task("t40")


def test_delete_task_non_404_error_propagates():
    existing = _task("t41", "To delete 2")
    routes = {**_basic_routes(shared_tasks=[existing])}
    client, session = _make_client(routes)
    client.list_tasks()
    from skysync.errors import PermanentApiError as PAE

    def fake_delete(url, *, what):
        raise PAE("forbidden", status=403)

    session.delete = fake_delete
    with pytest.raises(PAE):
        client.delete_task("t41")


# ===========================================================================
# 10. project() and representable()
# ===========================================================================


def test_project_all_fields():
    client, _ = _make_client(_basic_routes())
    task = CanonicalTask(
        title="  Hello  ",
        notes="  Notes  ",
        due_date=datetime.date(2026, 9, 1),
        assignee="alice",
        status="open",
    )
    proj = client.project(task)
    assert proj["title"] == "Hello"
    assert proj["notes"] == "Notes"
    assert proj["due_date"] == "2026-09-01"
    assert proj["assignee"] == "alice"
    assert proj["status"] == "open"


def test_project_unmapped_assignee_replaced_with_none():
    """An assignee not in child_lists should appear as None in project()."""
    client, _ = _make_client(_basic_routes())
    task = CanonicalTask(title="X", assignee="charlie")
    proj = client.project(task)
    assert proj["assignee"] is None


def test_project_due_date_none():
    client, _ = _make_client(_basic_routes())
    task = CanonicalTask(title="X", due_date=None)
    proj = client.project(task)
    assert proj["due_date"] is None


def test_representable_known_assignee():
    client, _ = _make_client(_basic_routes())
    assert client.representable("assignee", "alice") is True


def test_representable_none_assignee():
    client, _ = _make_client(_basic_routes())
    assert client.representable("assignee", None) is True


def test_representable_unknown_assignee():
    client, _ = _make_client(_basic_routes())
    assert client.representable("assignee", "charlie") is False


def test_representable_other_fields_always_true():
    client, _ = _make_client(_basic_routes())
    assert client.representable("title", "anything") is True
    assert client.representable("status", "open") is True


# ===========================================================================
# 11. SchemaDriftError on malformed payloads
# ===========================================================================


def test_schema_drift_missing_value_key():
    """iter_items response without 'value' key raises SchemaDriftError."""
    routes = {
        ("GET", TODO_BASE): _lists_response(),
        ("GET", f"{TODO_BASE}/list-alice/tasks"): {"wrongkey": []},
        ("GET", f"{TODO_BASE}/list-bob/tasks"): {"value": []},
        ("GET", f"{TODO_BASE}/list-shared/tasks"): {"value": []},
    }
    client, _ = _make_client(routes)
    with pytest.raises(SchemaDriftError):
        client.list_tasks()


def test_schema_drift_task_missing_id():
    bad_task = {
        "title": "No id",
        "status": "notStarted",
        "body": {"content": "", "contentType": "text"},
        "lastModifiedDateTime": "2026-06-01T10:00:00Z",
    }
    routes = {
        ("GET", TODO_BASE): _lists_response(),
        ("GET", f"{TODO_BASE}/list-alice/tasks"): {"value": []},
        ("GET", f"{TODO_BASE}/list-bob/tasks"): {"value": []},
        ("GET", f"{TODO_BASE}/list-shared/tasks"): {"value": [bad_task]},
    }
    client, _ = _make_client(routes)
    with pytest.raises(SchemaDriftError, match="'id'"):
        client.list_tasks()


def test_schema_drift_list_missing_id():
    routes = {
        ("GET", TODO_BASE): {
            "value": [
                # Missing 'id' in one list item
                {"displayName": "Alice Tasks", "wellknownListName": "none"},
                {"id": "list-bob", "displayName": "Bob Tasks", "wellknownListName": "none"},
                {"id": "list-shared", "displayName": "Shared Tasks", "wellknownListName": "none"},
            ]
        }
    }
    client, _ = _make_client(routes)
    with pytest.raises(SchemaDriftError):
        client.list_tasks()


# ===========================================================================
# 12. find_recovery_candidate delegates to find_by_marker
# ===========================================================================


def test_find_recovery_candidate_delegates():
    lr = _skysync_lr("iid-rec")
    t = _task("t50", "Recovery task", linked_resources=[lr])
    client, _ = _make_client(_basic_routes(shared_tasks=[t]))
    task = CanonicalTask(title="Recovery task")
    result = client.find_recovery_candidate(task, "iid-rec")
    assert result is not None
    assert result.remote_id == "t50"


# ===========================================================================
# 13. Misc
# ===========================================================================

def test_supports_always_true():
    client, _ = _make_client(_basic_routes())
    assert client.supports(CanonicalTask(title="anything")) is True


def test_side_is_todo():
    client, _ = _make_client(_basic_routes())
    assert client.side == "todo"


def test_projection_fields():
    client, _ = _make_client(_basic_routes())
    assert set(client.PROJECTION_FIELDS) == {"title", "notes", "due_date", "assignee", "status"}
