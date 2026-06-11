"""Microsoft Graph SharePoint task client.

Two layers:
  - thin API helpers that call Graph through a provided GraphSession;
  - SharePointTaskClient: full TaskClient protocol adapter.

Constructor: SharePointTaskClient(session, site_id, list_id, sp_assignees)
  sp_assignees: {lowercase_child_key: display_value_for_Assignee_column}

List columns (internal names): Title, Notes, DueDate, Assignee, Status, InternalId.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from ..errors import PermanentApiError, SchemaDriftError
from ..models import CanonicalTask, RemoteTask, Side
from .auth import GRAPH_BASE, GraphSession

log = logging.getLogger(__name__)

PROJECTION_FIELDS: tuple[str, ...] = ("title", "notes", "due_date", "assignee", "status")


# ---------------------------------------------------------------------------
# Thin API layer
# ---------------------------------------------------------------------------


def _sp_items_url(site_id: str, list_id: str) -> str:
    return f"{GRAPH_BASE}/sites/{site_id}/lists/{list_id}/items"


def _list_sp_tasks(session: GraphSession, site_id: str, list_id: str) -> list[dict]:
    url = _sp_items_url(site_id, list_id)
    return list(
        session.iter_items(
            url,
            what=f"list SP items {list_id}",
            params={
                "$expand": "fields($select=Title,Notes,DueDate,Assignee,Status,InternalId)",
                "$top": "200",
            },
        )
    )


def _create_sp_item(session: GraphSession, site_id: str, list_id: str, fields: dict) -> dict:
    url = _sp_items_url(site_id, list_id)
    return session.post_json(url, {"fields": fields}, what=f"create SP item in {list_id}")


def _patch_sp_item(
    session: GraphSession, site_id: str, list_id: str, item_id: str, fields: dict
) -> dict:
    url = f"{_sp_items_url(site_id, list_id)}/{item_id}/fields"
    return session.patch_json(url, fields, what=f"patch SP item {item_id}")


def _delete_sp_item(
    session: GraphSession, site_id: str, list_id: str, item_id: str
) -> None:
    url = f"{_sp_items_url(site_id, list_id)}/{item_id}"
    session.delete(url, what=f"delete SP item {item_id}")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_sp_date(raw: str | None) -> date | None:
    """Parse a SP DueDate like '2026-06-10T00:00:00Z' -> date."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def _parse_sp_datetime(raw: str | None) -> datetime | None:
    """Parse lastModifiedDateTime."""
    if not raw:
        return None
    raw = raw.rstrip("Z")
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _reverse_map_assignee(
    raw_value: str | None,
    sp_assignees: dict[str, str],
) -> str | None:
    """Map a raw Assignee column value to a child_key.

    Priority:
    1. Exact match against a VALUE in sp_assignees (case-insensitive) -> child_key
    2. Exact match against a KEY in sp_assignees (case-insensitive) -> child_key
    3. Non-empty raw value -> lowercase of the raw value
    4. Empty/None -> None
    """
    if not raw_value:
        return None
    raw_lower = raw_value.lower()
    # Check against values (display names)
    for key, display in sp_assignees.items():
        if display.lower() == raw_lower:
            return key
    # Check against keys
    if raw_lower in sp_assignees:
        return raw_lower
    # Fallback: lowercase of raw
    return raw_lower


# ---------------------------------------------------------------------------
# TaskClient implementation
# ---------------------------------------------------------------------------


class SharePointTaskClient:
    """TaskClient for a SharePoint list used as a task store."""

    side: Side = "sp"
    PROJECTION_FIELDS: tuple[str, ...] = PROJECTION_FIELDS

    def __init__(
        self,
        session: GraphSession,
        site_id: str,
        list_id: str,
        sp_assignees: dict[str, str],
    ) -> None:
        self._session = session
        self._site_id = site_id
        self._list_id = list_id
        # child_key (lower) -> display value for Assignee column
        self._sp_assignees: dict[str, str] = {k.lower(): v for k, v in sp_assignees.items()}
        # Snapshot: remote_id -> RemoteTask
        self._snapshot: dict[str, RemoteTask] | None = None

    # -- Parsing ---------------------------------------------------------------

    def _parse_item(self, raw: dict) -> RemoteTask:
        item_id = raw.get("id")
        if not item_id:
            summary = str(raw)[:300]
            raise SchemaDriftError("SP item missing 'id'", summary)

        fields = raw.get("fields")
        if fields is None:
            summary = str(raw)[:300]
            raise SchemaDriftError("SP item missing 'fields'", summary)

        title = (fields.get("Title") or "").strip()
        notes = (fields.get("Notes") or "").strip()
        due_date = _parse_sp_date(fields.get("DueDate"))

        raw_status = (fields.get("Status") or "").lower()
        status: str = "completed" if raw_status == "completed" else "open"

        assignee = _reverse_map_assignee(fields.get("Assignee"), self._sp_assignees)
        marker = fields.get("InternalId") or None

        lm_raw = raw.get("lastModifiedDateTime")
        last_modified = _parse_sp_datetime(lm_raw)

        task = CanonicalTask(
            title=title,
            notes=notes,
            due_date=due_date,
            assignee=assignee,
            status=status,  # type: ignore[arg-type]
        )
        return RemoteTask(
            side="sp",
            remote_id=item_id,
            task=task,
            marker_internal_id=marker,
            last_modified=last_modified,
            container_id=None,
            raw=raw,
        )

    # -- Field dict building ---------------------------------------------------

    def _build_fields(self, task: CanonicalTask, internal_id: str | None) -> dict[str, Any]:
        """Build the fields dict for create/update."""
        fields: dict[str, Any] = {
            "Title": task.title.strip(),
            "Notes": task.notes.strip() if task.notes else None,
            "Status": "completed" if task.status == "completed" else "open",
        }
        if task.due_date is not None:
            fields["DueDate"] = f"{task.due_date.isoformat()}T00:00:00Z"
        # When None, omit from create (absence is fine); for update we handle separately
        if task.assignee is not None:
            # Map to display value if present, else use raw canonical string
            fields["Assignee"] = self._sp_assignees.get(task.assignee, task.assignee)
        if internal_id is not None:
            fields["InternalId"] = internal_id
        return fields

    def _build_fields_for_update(self, task: CanonicalTask, internal_id: str) -> dict[str, Any]:
        """Build fields dict for PATCH; explicitly None-clear absent optional fields."""
        fields: dict[str, Any] = {
            "Title": task.title.strip(),
            "Notes": task.notes.strip() if task.notes else None,
            "Status": "completed" if task.status == "completed" else "open",
            "InternalId": internal_id,
        }
        # Explicitly clear or set DueDate
        if task.due_date is not None:
            fields["DueDate"] = f"{task.due_date.isoformat()}T00:00:00Z"
        else:
            fields["DueDate"] = None
        # Explicitly clear or set Assignee
        if task.assignee is not None:
            fields["Assignee"] = self._sp_assignees.get(task.assignee, task.assignee)
        else:
            fields["Assignee"] = None
        return fields

    # -- TaskClient protocol ---------------------------------------------------

    def list_tasks(self) -> list[RemoteTask]:
        raw_items = _list_sp_tasks(self._session, self._site_id, self._list_id)
        tasks: list[RemoteTask] = []
        snapshot: dict[str, RemoteTask] = {}
        for raw in raw_items:
            rt = self._parse_item(raw)
            tasks.append(rt)
            snapshot[rt.remote_id] = rt
        self._snapshot = snapshot
        return tasks

    def create_task(self, task: CanonicalTask, internal_id: str) -> RemoteTask:
        fields = self._build_fields(task, internal_id)
        response = _create_sp_item(self._session, self._site_id, self._list_id, fields)
        item_id = response.get("id")
        if not item_id:
            summary = str(response)[:300]
            raise SchemaDriftError("create SP item response missing 'id'", summary)
        rt = self._parse_item(response)
        if self._snapshot is not None:
            self._snapshot[rt.remote_id] = rt
        return rt

    def update_task(self, remote_id: str, task: CanonicalTask, internal_id: str) -> RemoteTask:
        fields = self._build_fields_for_update(task, internal_id)
        # PATCH /sites/{site}/lists/{list}/items/{id}/fields
        # The response is just the fields object; build RemoteTask from desired state
        _patch_sp_item(self._session, self._site_id, self._list_id, remote_id, fields)

        # Reconstruct RemoteTask from desired state (PATCH returns only fields dict)
        rt = RemoteTask(
            side="sp",
            remote_id=remote_id,
            task=CanonicalTask(
                title=task.title.strip(),
                notes=task.notes.strip(),
                due_date=task.due_date,
                assignee=_reverse_map_assignee(
                    fields.get("Assignee"), self._sp_assignees
                ),
                status=task.status,
            ),
            marker_internal_id=internal_id,
            last_modified=None,
            container_id=None,
        )
        if self._snapshot is not None:
            self._snapshot[remote_id] = rt
        return rt

    def delete_task(self, remote_id: str) -> None:
        try:
            _delete_sp_item(self._session, self._site_id, self._list_id, remote_id)
        except PermanentApiError as exc:
            if exc.status == 404:
                pass  # idempotent
            else:
                raise
        if self._snapshot is not None:
            self._snapshot.pop(remote_id, None)

    def find_by_marker(self, internal_id: str) -> RemoteTask | None:
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
        due_date_val: str | None = task.due_date.isoformat() if task.due_date is not None else None
        return {
            "title": task.title.strip(),
            "notes": task.notes.strip(),
            "due_date": due_date_val,
            "assignee": task.assignee,
            "status": task.status,
        }

    def representable(self, fieldname: str, value: Any) -> bool:
        return True

    def supports(self, task: CanonicalTask) -> bool:
        return True
