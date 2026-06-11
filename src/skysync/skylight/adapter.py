"""Skylight-side TaskClient adapter.

Implements the ``TaskClient`` protocol from ``skysync.models`` for the
Skylight Calendar API, using ``SkylightApi`` for transport.

Key design decisions:
- Skylight chores have no notes field; PROJECTION_FIELDS omits "notes".
- Assignees are resolved through Skylight category labels; unresolvable
  assignees make a task unsupported on this side.
- Recurring/routine chores are skipped unless ``sync_recurring=True``.
- No marker-embed support; crash recovery uses title+assignee+due_date match.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Callable

from skysync.errors import ConfigError, PermanentApiError
from skysync.models import CanonicalTask, RemoteTask

from .client import SkylightApi
from .models_generated import Category, Chore

log = logging.getLogger(__name__)

Side = str  # "skylight"


class SkylightTaskClient:
    """TaskClient for Skylight Calendar chores.

    Parameters
    ----------
    api:
        Configured ``SkylightApi`` instance.
    child_categories:
        Maps lowercase child_key -> Skylight category label, e.g.
        ``{"kayla": "Kayla", "garrett": "Garrett"}``.
    window_past_days:
        How many days before today to include in list_tasks.
    window_future_days:
        How many days after today to include in list_tasks.
    sync_recurring:
        If False (default), recurring/routine chores are skipped by list_tasks.
    today:
        Callable returning today's date; injectable for tests.
    """

    side: str = "skylight"
    PROJECTION_FIELDS: tuple[str, ...] = ("title", "due_date", "assignee", "status")

    def __init__(
        self,
        api: SkylightApi,
        child_categories: dict[str, str],
        window_past_days: int,
        window_future_days: int,
        sync_recurring: bool = False,
        today: Callable[[], datetime.date] = datetime.date.today,
    ) -> None:
        self._api = api
        # Maps lowercase child_key -> configured Skylight category label.
        self._child_categories = child_categories
        self._window_past = window_past_days
        self._window_future = window_future_days
        self._sync_recurring = sync_recurring
        self._today = today

        # Lazy category maps — populated on first access.
        self._id_to_label: dict[str, str] | None = None  # category_id -> label
        self._lower_label_to_id: dict[str, str] | None = None  # lower(label) -> id

    # ------------------------------------------------------------------
    # Category resolution (lazy)
    # ------------------------------------------------------------------

    def _ensure_categories(self) -> None:
        if self._id_to_label is not None:
            return
        cats: list[Category] = self._api.get_categories()
        self._id_to_label = {c.id: c.attributes.label for c in cats}
        self._lower_label_to_id = {c.attributes.label.lower(): c.id for c in cats}

    def _resolve_category_id(self, assignee: str) -> str | None:
        """Return Skylight category id for a canonical assignee, or None."""
        self._ensure_categories()
        lower = assignee.lower()
        # 1. Check child_categories map: child_key -> configured label.
        if lower in self._child_categories:
            configured_label = self._child_categories[lower]
            cat_id = self._lower_label_to_id.get(configured_label.lower())  # type: ignore[union-attr]
            if cat_id is None:
                raise ConfigError(
                    f"Skylight category label {configured_label!r} (configured for "
                    f"child key {assignee!r}) was not found in the frame's categories. "
                    "Check [mapping.children] skylight_category values in config.toml."
                )
            return cat_id
        # 2. Fallback: if the assignee string matches any category label directly.
        return self._lower_label_to_id.get(lower)  # type: ignore[union-attr]

    def _category_id_to_assignee(self, category_id: str | None) -> str | None:
        """Reverse-map category_id -> canonical assignee string."""
        if category_id is None:
            return None
        self._ensure_categories()
        label = self._id_to_label.get(category_id)  # type: ignore[union-attr]
        if label is None:
            return None
        # If this label is one of the child_categories values, return the child key.
        lower_label = label.lower()
        for child_key, configured_label in self._child_categories.items():
            if configured_label.lower() == lower_label:
                return child_key
        # Otherwise return lowercase label as passthrough.
        return lower_label

    # ------------------------------------------------------------------
    # TaskClient protocol
    # ------------------------------------------------------------------

    def representable(self, fieldname: str, value: Any) -> bool:
        """Whether this side can faithfully represent the given field+value."""
        if fieldname == "assignee":
            if value is None:
                return False
            try:
                cat_id = self._resolve_category_id(str(value))
            except ConfigError:
                return False
            return cat_id is not None
        # All other projection fields are always representable.
        return True

    def supports(self, task: CanonicalTask) -> bool:
        """A task is supported if its assignee resolves to a Skylight category."""
        if task.assignee is None:
            return False
        try:
            cat_id = self._resolve_category_id(task.assignee)
        except ConfigError:
            return False
        return cat_id is not None

    def list_tasks(self) -> list[RemoteTask]:
        """Fetch chores from the configured window and convert to RemoteTasks."""
        today = self._today()
        after = today - datetime.timedelta(days=self._window_past)
        before = today + datetime.timedelta(days=self._window_future)
        envelope = self._api.get_chores(after, before)

        results: list[RemoteTask] = []
        for chore in envelope.data:
            if not self._sync_recurring and (
                chore.attributes.recurring or chore.attributes.routine
            ):
                continue
            results.append(self._chore_to_remote(chore))
        return results

    def _chore_to_remote(self, chore: Chore) -> RemoteTask:
        """Convert a Chore to a RemoteTask."""
        attrs = chore.attributes
        raw_status = attrs.status
        status: str = (
            "completed" if raw_status in {"complete", "completed"} else "open"
        )
        task = CanonicalTask(
            title=attrs.summary.strip(),
            notes="",
            due_date=attrs.start,
            assignee=self._category_id_to_assignee(chore.category_id),
            status=status,  # type: ignore[arg-type]
        )
        return RemoteTask(
            side="skylight",
            remote_id=chore.id,
            task=task,
            marker_internal_id=None,
            last_modified=None,
            container_id=chore.category_id,
            raw=chore,
        )

    def create_task(self, task: CanonicalTask, internal_id: str) -> RemoteTask:
        """Create a new chore on Skylight."""
        cat_id = self._resolve_category_id(task.assignee or "")
        if cat_id is None:
            raise PermanentApiError(
                f"Cannot create Skylight chore: assignee {task.assignee!r} does not "
                "resolve to a category. The engine should have checked supports() first.",
                status=None,
            )
        chore = self._api.create_chore(
            summary=task.title.strip(),
            category_id=cat_id,
            start=task.due_date,
        )
        if task.status == "completed":
            chore = self._api.update_chore(chore.id, status="complete")
        return self._chore_to_remote(chore)

    def update_task(
        self, remote_id: str, task: CanonicalTask, internal_id: str
    ) -> RemoteTask:
        """Update an existing chore on Skylight."""
        cat_id = self._resolve_category_id(task.assignee or "") if task.assignee else None
        skylight_status = "complete" if task.status == "completed" else "pending"
        chore = self._api.update_chore(
            remote_id,
            summary=task.title.strip(),
            category_id=cat_id,
            start=task.due_date,
            status=skylight_status,
        )
        return self._chore_to_remote(chore)

    def delete_task(self, remote_id: str) -> None:
        """Delete a chore on Skylight."""
        self._api.delete_chore(remote_id)

    def find_by_marker(self, internal_id: str) -> RemoteTask | None:
        """Skylight chores have no marker field; always returns None."""
        return None

    def find_recovery_candidate(
        self, task: CanonicalTask, internal_id: str
    ) -> RemoteTask | None:
        """Content-based recovery: match title + assignee + due_date.

        Returns a match only if EXACTLY ONE candidate is found (high confidence).
        0 or 2+ candidates -> None.
        """
        remote_tasks = self.list_tasks()
        target_title = task.title.strip()
        candidates = [
            rt
            for rt in remote_tasks
            if (
                rt.task.title == target_title
                and rt.task.assignee == task.assignee
                and rt.task.due_date == task.due_date
            )
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def project(self, task: CanonicalTask) -> dict[str, Any]:
        """Return the canonical projection this side can represent."""
        due_iso: str | None = (
            task.due_date.isoformat() if task.due_date is not None else None
        )
        assignee: str | None = (
            task.assignee if self.representable("assignee", task.assignee) else None
        )
        return {
            "title": task.title.strip(),
            "due_date": due_iso,
            "assignee": assignee,
            "status": task.status,
        }
