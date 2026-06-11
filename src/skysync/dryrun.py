"""Dry-run wrapper: reads pass through, writes are logged and faked.

main.py pairs this with a THROWAWAY COPY of the ledger so a dry run plans
exactly what a live run would, records it nowhere real, and touches no remote.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

from .models import CanonicalTask, RemoteTask, Side, TaskClient

log = logging.getLogger(__name__)


class DryRunClient:
    def __init__(self, inner: TaskClient):
        self._inner = inner
        self.side: Side = inner.side
        self.PROJECTION_FIELDS = inner.PROJECTION_FIELDS
        self._ids = itertools.count(1)
        self.planned: list[str] = []

    # -------- reads delegate ------------------------------------------------
    def list_tasks(self) -> list[RemoteTask]:
        return self._inner.list_tasks()

    def find_by_marker(self, internal_id: str) -> RemoteTask | None:
        return self._inner.find_by_marker(internal_id)

    def find_recovery_candidate(self, task: CanonicalTask, internal_id: str) -> RemoteTask | None:
        return self._inner.find_recovery_candidate(task, internal_id)

    def project(self, task: CanonicalTask) -> dict[str, Any]:
        return self._inner.project(task)

    def representable(self, fieldname: str, value: Any) -> bool:
        return self._inner.representable(fieldname, value)

    def supports(self, task: CanonicalTask) -> bool:
        return self._inner.supports(task)

    # -------- writes are announced, never executed --------------------------
    def _note(self, msg: str) -> None:
        self.planned.append(msg)
        log.info("[DRY-RUN] %s", msg)

    def create_task(self, task: CanonicalTask, internal_id: str) -> RemoteTask:
        self._note(f"would CREATE on {self.side}: {task.title!r} (assignee={task.assignee}, due={task.due_date})")
        return RemoteTask(
            side=self.side,
            remote_id=f"dryrun-{self.side}-{next(self._ids)}",
            task=task,
            marker_internal_id=internal_id,
        )

    def update_task(self, remote_id: str, task: CanonicalTask, internal_id: str) -> RemoteTask:
        self._note(f"would UPDATE {self.side}/{remote_id}: {task.title!r} -> status={task.status}, due={task.due_date}, assignee={task.assignee}")
        return RemoteTask(side=self.side, remote_id=remote_id, task=task, marker_internal_id=internal_id)

    def delete_task(self, remote_id: str) -> None:
        self._note(f"would DELETE {self.side}/{remote_id}")
