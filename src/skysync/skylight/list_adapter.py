"""Skylight LIST adapter: exposes one Skylight list (e.g. the grocery list)
through the TaskClient protocol, so a second engine instance can mirror it
against a Microsoft To Do list.

Differences from chores: items have only a label + status (no dates, notes,
assignees, markers, or timestamps); the items endpoint is NOT windowed, so
absence from a snapshot is authoritative (the paired engine runs with
``sky_absence_trusted=True``).
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import ConfigError
from ..models import CanonicalTask, RemoteTask
from .client import SkylightApi

log = logging.getLogger(__name__)


class SkylightListTaskClient:
    side = "skylight"
    PROJECTION_FIELDS: tuple[str, ...] = ("title", "status")

    def __init__(self, api: SkylightApi, list_label: str):
        self._api = api
        self._label = list_label
        self._list_id: str | None = None

    def _resolve_list_id(self) -> str:
        if self._list_id is None:
            lists = self._api.get_lists()
            for lst in lists:
                label = getattr(lst.attributes, "label", None) or ""
                if label.strip().lower() == self._label.strip().lower():
                    self._list_id = lst.id
                    break
            else:
                known = [getattr(l.attributes, "label", "?") for l in lists]
                raise ConfigError(
                    f"Skylight list {self._label!r} not found on the frame; lists there: {known}"
                )
        return self._list_id

    # ---------------------------------------------------------- protocol ----
    def list_tasks(self) -> list[RemoteTask]:
        list_id = self._resolve_list_id()
        out: list[RemoteTask] = []
        for item in self._api.get_list_items(list_id):
            status = "completed" if (item.attributes.status or "") in {"completed", "complete"} else "open"
            task = CanonicalTask(title=(item.attributes.label or "").strip(), status=status)  # type: ignore[arg-type]
            out.append(
                RemoteTask(
                    side="skylight",
                    remote_id=item.id,
                    task=task,
                    marker_internal_id=None,
                    last_modified=None,  # list items expose no modified time
                    container_id=list_id,
                    raw=item,
                )
            )
        return out

    def create_task(self, task: CanonicalTask, internal_id: str) -> RemoteTask:
        list_id = self._resolve_list_id()
        item = self._api.create_list_item(list_id, task.title.strip())
        if task.status == "completed":
            item = self._api.update_list_item(list_id, item.id, status="completed")
        status = "completed" if (item.attributes.status or "") in {"completed", "complete"} else "open"
        return RemoteTask(
            side="skylight",
            remote_id=item.id,
            task=CanonicalTask(title=(item.attributes.label or "").strip(), status=status),  # type: ignore[arg-type]
            container_id=list_id,
        )

    def update_task(self, remote_id: str, task: CanonicalTask, internal_id: str) -> RemoteTask:
        list_id = self._resolve_list_id()
        item = self._api.update_list_item(
            list_id,
            remote_id,
            label=task.title.strip(),
            status="completed" if task.status == "completed" else "pending",
        )
        status = "completed" if (item.attributes.status or "") in {"completed", "complete"} else "open"
        return RemoteTask(
            side="skylight",
            remote_id=item.id,
            task=CanonicalTask(title=(item.attributes.label or "").strip(), status=status),  # type: ignore[arg-type]
            container_id=list_id,
        )

    def delete_task(self, remote_id: str) -> None:
        self._api.delete_list_item(self._resolve_list_id(), remote_id)

    def find_by_marker(self, internal_id: str) -> RemoteTask | None:
        return None  # no marker support on list items

    def find_recovery_candidate(self, task: CanonicalTask, internal_id: str) -> RemoteTask | None:
        hits = [rt for rt in self.list_tasks() if rt.task.title == task.title.strip()]
        return hits[0] if len(hits) == 1 else None

    def project(self, task: CanonicalTask) -> dict[str, Any]:
        return {"title": task.title.strip(), "status": task.status}

    def representable(self, fieldname: str, value: Any) -> bool:
        return fieldname in self.PROJECTION_FIELDS

    def supports(self, task: CanonicalTask) -> bool:
        return True
