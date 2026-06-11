"""Microsoft Graph To Do task client.

Two layers:
  - thin API helpers (list_todo_lists, list_todo_tasks, etc.) that call Graph
    through a provided GraphSession;
  - TodoTaskClient: full TaskClient protocol adapter.

Constructor: TodoTaskClient(session, child_lists, default_list)
  child_lists: {lowercase_child_key: to_do_list_display_name}
  default_list: display name of the default list (tasks with no mapped assignee)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from ..errors import ConfigError, PermanentApiError, SchemaDriftError
from ..models import CanonicalTask, RemoteTask, Side
from .auth import GRAPH_BASE, GraphSession

log = logging.getLogger(__name__)

_TODO_BASE = f"{GRAPH_BASE}/me/todo/lists"

PROJECTION_FIELDS: tuple[str, ...] = ("title", "notes", "due_date", "assignee", "status")


# ---------------------------------------------------------------------------
# Thin API layer
# ---------------------------------------------------------------------------


def _list_todo_lists(session: GraphSession) -> list[dict]:
    """Return all To Do lists for /me."""
    return list(
        session.iter_items(
            f"{_TODO_BASE}",
            what="list To Do lists",
        )
    )


def _list_tasks_in_list(session: GraphSession, list_id: str) -> list[dict]:
    """Return all tasks (with linkedResources expanded) from a single list."""
    url = f"{_TODO_BASE}/{list_id}/tasks"
    return list(
        session.iter_items(
            url,
            what=f"list tasks in list {list_id}",
            params={"$expand": "linkedResources", "$top": "100"},
        )
    )


def _create_todo_task(session: GraphSession, list_id: str, body: dict) -> dict:
    url = f"{_TODO_BASE}/{list_id}/tasks"
    return session.post_json(url, body, what=f"create task in list {list_id}")


def _patch_todo_task(session: GraphSession, list_id: str, task_id: str, body: dict) -> dict:
    url = f"{_TODO_BASE}/{list_id}/tasks/{task_id}"
    return session.patch_json(url, body, what=f"patch task {task_id}")


def _delete_todo_task(session: GraphSession, list_id: str, task_id: str) -> None:
    url = f"{_TODO_BASE}/{list_id}/tasks/{task_id}"
    session.delete(url, what=f"delete task {task_id}")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_due_date(due: dict | None) -> date | None:
    """Parse To Do dueDateTime object -> date."""
    if not due:
        return None
    raw = due.get("dateTime")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def _marker_from_resources(linked_resources: list[dict] | None) -> str | None:
    """Extract SkySync externalId from linkedResources array."""
    if not linked_resources:
        return None
    for lr in linked_resources:
        if lr.get("applicationName") == "SkySync":
            eid = lr.get("externalId")
            if eid:
                return eid
    return None


def _parse_task(raw: dict, list_id: str, assignee: str | None) -> RemoteTask:
    """Convert a raw Graph To Do task dict into a RemoteTask."""
    task_id = raw.get("id")
    if not task_id:
        summary = str(raw)[:300]
        raise SchemaDriftError("To Do task missing 'id'", summary)

    title = (raw.get("title") or "").strip()

    body_obj = raw.get("body") or {}
    notes = (body_obj.get("content") or "").strip()

    due_date = _parse_due_date(raw.get("dueDateTime"))

    graph_status = raw.get("status") or "notStarted"
    status: str = "completed" if graph_status == "completed" else "open"

    lm_raw = raw.get("lastModifiedDateTime")
    last_modified = _parse_datetime(lm_raw)

    linked_resources: list[dict] = raw.get("linkedResources") or []
    marker = _marker_from_resources(linked_resources)

    task = CanonicalTask(
        title=title,
        notes=notes,
        due_date=due_date,
        assignee=assignee,
        status=status,  # type: ignore[arg-type]
    )
    return RemoteTask(
        side="todo",
        remote_id=task_id,
        task=task,
        marker_internal_id=marker,
        last_modified=last_modified,
        container_id=list_id,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# TaskClient implementation
# ---------------------------------------------------------------------------


class TodoTaskClient:
    """TaskClient for Microsoft To Do (delegated auth only)."""

    side: Side = "todo"
    PROJECTION_FIELDS: tuple[str, ...] = PROJECTION_FIELDS

    def __init__(
        self,
        session: GraphSession,
        child_lists: dict[str, str],
        default_list: str,
    ) -> None:
        self._session = session
        # child_lists: lowercase child_key -> display name
        self._child_lists: dict[str, str] = {k.lower(): v for k, v in child_lists.items()}
        self._default_list_name: str = default_list
        # Resolved lazily: display name (lower) -> list id
        self._list_ids: dict[str, str] | None = None
        # Snapshot from last list_tasks call: remote_id -> RemoteTask
        self._snapshot: dict[str, RemoteTask] | None = None

    # -- list resolution -------------------------------------------------------

    def _resolve_lists(self) -> None:
        """Fetch all To Do lists and populate _list_ids; raises ConfigError for missing."""
        if self._list_ids is not None:
            return
        raw_lists = _list_todo_lists(self._session)
        by_name: dict[str, str] = {}  # lower display name -> id
        for item in raw_lists:
            item_id = item.get("id")
            name = item.get("displayName")
            if not item_id or not name:
                summary = str(item)[:300]
                raise SchemaDriftError("To Do list item missing 'id' or 'displayName'", summary)
            by_name[name.lower()] = item_id

        result: dict[str, str] = {}

        # Resolve default list
        dl_lower = self._default_list_name.lower()
        if dl_lower not in by_name:
            raise ConfigError(
                f"To Do list not found: '{self._default_list_name}' (configured as default_list)"
            )
        result[dl_lower] = by_name[dl_lower]

        # Resolve each child list
        for child_key, display_name in self._child_lists.items():
            dl = display_name.lower()
            if dl not in by_name:
                raise ConfigError(
                    f"To Do list not found: '{display_name}' (configured for child '{child_key}')"
                )
            result[dl] = by_name[dl]

        self._list_ids = result

    def _default_list_id(self) -> str:
        self._resolve_lists()
        assert self._list_ids is not None
        return self._list_ids[self._default_list_name.lower()]

    def _child_list_id(self, child_key: str) -> str:
        """Get list id for a child key (key must be in child_lists)."""
        self._resolve_lists()
        assert self._list_ids is not None
        display = self._child_lists[child_key]
        return self._list_ids[display.lower()]

    def _assignee_to_list_id(self, assignee: str | None) -> tuple[str | None, str]:
        """Return (child_key_or_None, list_id) for a task's assignee."""
        if assignee and assignee in self._child_lists:
            return assignee, self._child_list_id(assignee)
        return None, self._default_list_id()

    # -- watched list ids -------------------------------------------------------

    def _watched_list_ids(self) -> dict[str, str | None]:
        """Return {list_id: child_key_or_None} for all watched lists."""
        self._resolve_lists()
        assert self._list_ids is not None
        result: dict[str, str | None] = {}
        # default list -> None assignee
        result[self._default_list_id()] = None
        # child lists
        for child_key, display_name in self._child_lists.items():
            lid = self._list_ids[display_name.lower()]
            result[lid] = child_key
        return result

    # -- TaskClient protocol ---------------------------------------------------

    def list_tasks(self) -> list[RemoteTask]:
        self._resolve_lists()
        watched = self._watched_list_ids()
        tasks: list[RemoteTask] = []
        snapshot: dict[str, RemoteTask] = {}
        for list_id, assignee in watched.items():
            raw_tasks = _list_tasks_in_list(self._session, list_id)
            for raw in raw_tasks:
                rt = _parse_task(raw, list_id, assignee)
                tasks.append(rt)
                snapshot[rt.remote_id] = rt
        self._snapshot = snapshot
        return tasks

    def create_task(self, task: CanonicalTask, internal_id: str) -> RemoteTask:
        _, list_id = self._assignee_to_list_id(
            task.assignee if self.representable("assignee", task.assignee) else None
        )
        body: dict[str, Any] = {
            "title": task.title.strip(),
            "body": {"content": task.notes.strip(), "contentType": "text"},
            "status": "completed" if task.status == "completed" else "notStarted",
            "linkedResources": [
                {
                    "applicationName": "SkySync",
                    "externalId": internal_id,
                    "displayName": "SkySync sync marker",
                }
            ],
        }
        if task.due_date is not None:
            body["dueDateTime"] = {
                "dateTime": f"{task.due_date.isoformat()}T00:00:00.0000000",
                "timeZone": "UTC",
            }
        response = _create_todo_task(self._session, list_id, body)
        task_id = response.get("id")
        if not task_id:
            summary = str(response)[:300]
            raise SchemaDriftError("create_task response missing 'id'", summary)

        # Parse the response the same way list_tasks does
        assignee_for_rt = task.assignee if self.representable("assignee", task.assignee) else None
        # Ensure linkedResources marker is set (Graph may not return it immediately)
        raw = dict(response)
        if not _marker_from_resources(raw.get("linkedResources")):
            raw.setdefault("linkedResources", [])
            # inject so _parse_task picks it up
            raw["linkedResources"] = [
                {
                    "applicationName": "SkySync",
                    "externalId": internal_id,
                    "displayName": "SkySync sync marker",
                }
            ]
        rt = _parse_task(raw, list_id, assignee_for_rt)
        if self._snapshot is not None:
            self._snapshot[rt.remote_id] = rt
        return rt

    def update_task(self, remote_id: str, task: CanonicalTask, internal_id: str) -> RemoteTask:
        # Find current container
        current_rt = self._snapshot.get(remote_id) if self._snapshot else None
        if current_rt is None:
            # Refresh snapshot
            self.list_tasks()
            current_rt = self._snapshot.get(remote_id) if self._snapshot else None
        if current_rt is None:
            raise PermanentApiError(
                f"Task {remote_id} not found in any watched To Do list",
                status=404,
            )
        current_list_id = current_rt.container_id

        # Determine target list
        assignee_repr = task.assignee if self.representable("assignee", task.assignee) else None
        _, target_list_id = self._assignee_to_list_id(assignee_repr)

        if target_list_id != current_list_id:
            # Graph v1.0 cannot move tasks: create in new list, delete old
            new_rt = self.create_task(task, internal_id)
            self.delete_task(remote_id)
            return new_rt

        # Same list: PATCH
        body: dict[str, Any] = {
            "title": task.title.strip(),
            "body": {"content": task.notes.strip(), "contentType": "text"},
            "status": "completed" if task.status == "completed" else "notStarted",
        }
        if task.due_date is not None:
            body["dueDateTime"] = {
                "dateTime": f"{task.due_date.isoformat()}T00:00:00.0000000",
                "timeZone": "UTC",
            }
        else:
            # Explicitly clear due date
            body["dueDateTime"] = None

        response = _patch_todo_task(self._session, current_list_id, remote_id, body)
        resp_id = response.get("id")
        if not resp_id:
            # PATCH may return partial; reconstruct from desired state
            response = dict(response)
            response.setdefault("id", remote_id)

        assignee_for_rt = assignee_repr
        rt = _parse_task(response, current_list_id, assignee_for_rt)
        if self._snapshot is not None:
            self._snapshot[rt.remote_id] = rt
        return rt

    def delete_task(self, remote_id: str) -> None:
        # We need the list id; get it from snapshot
        current_rt = self._snapshot.get(remote_id) if self._snapshot else None
        if current_rt is None:
            # Refresh
            self.list_tasks()
            current_rt = self._snapshot.get(remote_id) if self._snapshot else None
        if current_rt is None:
            # Already gone
            return
        list_id = current_rt.container_id
        try:
            _delete_todo_task(self._session, list_id, remote_id)
        except PermanentApiError as exc:
            if exc.status == 404:
                pass  # idempotent
            else:
                raise
        # Remove from snapshot
        if self._snapshot is not None:
            self._snapshot.pop(remote_id, None)

    def find_by_marker(self, internal_id: str) -> RemoteTask | None:
        """Scan the snapshot (refresh if needed) for a task with the given marker."""
        if self._snapshot is None:
            self.list_tasks()
        assert self._snapshot is not None
        for rt in self._snapshot.values():
            if rt.marker_internal_id == internal_id:
                return rt
        return None

    def find_recovery_candidate(self, task: CanonicalTask, internal_id: str) -> RemoteTask | None:
        return self.find_by_marker(internal_id)

    def project(self, task: CanonicalTask) -> dict[str, Any]:
        """Return the projection dict, with assignee replaced by None when not representable."""
        assignee = task.assignee if self.representable("assignee", task.assignee) else None
        due_date_val: str | None = task.due_date.isoformat() if task.due_date is not None else None
        return {
            "title": task.title.strip(),
            "notes": task.notes.strip(),
            "due_date": due_date_val,
            "assignee": assignee,
            "status": task.status,
        }

    def representable(self, fieldname: str, value: Any) -> bool:
        if fieldname == "assignee":
            return value is None or value in self._child_lists
        return True

    def supports(self, task: CanonicalTask) -> bool:
        return True
