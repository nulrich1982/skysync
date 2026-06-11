"""In-memory ``TaskClient`` implementation.

Used by ``--mock`` mode (seeded from JSON fixtures, no network) and by the
engine test suite (which additionally injects crashes/faults to prove the
ledger's replay safety). Behaves like each real side:

* ``sp``      — full fidelity, marker column, timestamps.
* ``todo``    — full fidelity, marker (linkedResource), timestamps; assignee
                representable only when mapped to a list; container = list.
* ``skylight``— no notes, no marker, NO timestamps; requires a mappable
                assignee; container = category.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import CanonicalTask, RemoteTask, Side


class SimulatedCrash(BaseException):
    """Stand-in for process death in tests. BaseException so nothing in the
    engine accidentally catches it."""


@dataclass
class _Stored:
    task: CanonicalTask
    marker: str | None = None
    container: str | None = None
    last_modified: datetime | None = None


@dataclass
class Fault:
    action: str  # create|update|delete
    when: str = "before"  # before|after applying the remote effect
    exc: BaseException = field(default_factory=SimulatedCrash)
    remaining: int = 1  # fire on the Nth matching write


class InMemoryTaskClient:
    def __init__(
        self,
        side: Side,
        *,
        mapped_assignees: dict[str, str] | None = None,  # child_key -> container id
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        normalize: Callable[[CanonicalTask], CanonicalTask] | None = None,
    ):
        self.side: Side = side
        self.items: dict[str, _Stored] = {}
        self.write_log: list[tuple[str, str]] = []  # (action, remote_id)
        self.fault: Fault | None = None
        self.clock = clock
        self.normalize = normalize  # simulates upstream value normalization
        self.mapped = mapped_assignees or {}
        self._ids = itertools.count(1)

        if side == "skylight":
            self.PROJECTION_FIELDS: tuple[str, ...] = ("title", "due_date", "assignee", "status")
            self.marker_support = False
            self.expose_timestamps = False
        else:
            self.PROJECTION_FIELDS = ("title", "notes", "due_date", "assignee", "status")
            self.marker_support = True
            self.expose_timestamps = True

    # ------------------------------------------------------------ contract --
    def representable(self, fieldname: str, value: Any) -> bool:
        if fieldname != "assignee" or self.side == "sp":
            return True
        if self.side == "todo":
            return value is None or value in self.mapped
        return value is not None and value in self.mapped  # skylight

    def supports(self, task: CanonicalTask) -> bool:
        if self.side == "skylight":
            return task.assignee is not None and task.assignee in self.mapped
        return True

    def project(self, task: CanonicalTask) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in self.PROJECTION_FIELDS:
            v = getattr(task, f)
            if f == "assignee" and not self.representable("assignee", v):
                v = None
            elif f == "due_date":
                v = v.isoformat() if v else None
            elif f in ("title", "notes"):
                v = (v or "").strip()
            out[f] = v
        return out

    def list_tasks(self) -> list[RemoteTask]:
        return [self._remote(rid) for rid in sorted(self.items, key=self._sort_key)]

    def _sort_key(self, rid: str) -> str:
        return rid

    def _remote(self, rid: str) -> RemoteTask:
        s = self.items[rid]
        return RemoteTask(
            side=self.side,
            remote_id=rid,
            task=s.task,
            marker_internal_id=s.marker if self.marker_support else None,
            last_modified=s.last_modified if self.expose_timestamps else None,
            container_id=s.container,
            raw=None,
        )

    def _maybe_fault(self, action: str, when: str) -> None:
        f = self.fault
        if f and f.action == action and f.when == when:
            f.remaining -= 1
            if f.remaining <= 0:
                self.fault = None
                raise f.exc

    def _store(self, task: CanonicalTask, marker: str | None) -> str:
        if self.normalize:
            task = self.normalize(task)
        rid = f"{self.side}-{next(self._ids)}"
        self.items[rid] = _Stored(
            task=task,
            marker=marker,
            container=self.mapped.get(task.assignee or ""),
            last_modified=self.clock(),
        )
        return rid

    def create_task(self, task: CanonicalTask, internal_id: str) -> RemoteTask:
        self._maybe_fault("create", "before")
        rid = self._store(task, internal_id if self.marker_support else None)
        self.write_log.append(("create", rid))
        self._maybe_fault("create", "after")
        return self._remote(rid)

    def update_task(self, remote_id: str, task: CanonicalTask, internal_id: str) -> RemoteTask:
        self._maybe_fault("update", "before")
        if remote_id not in self.items:
            from .errors import PermanentApiError

            raise PermanentApiError(f"{self.side}: {remote_id} not found", status=404)
        if self.normalize:
            task = self.normalize(task)
        s = self.items[remote_id]
        # Like real To Do: an assignee change moves containers; To Do can't
        # move tasks, so simulate create+delete with a NEW remote id.
        new_container = self.mapped.get(task.assignee or "")
        if self.side == "todo" and new_container != s.container:
            del self.items[remote_id]
            rid = self._store(task, s.marker)
            self.write_log.append(("update-move", rid))
            self._maybe_fault("update", "after")
            return self._remote(rid)
        s.task = task
        s.container = new_container or s.container
        s.last_modified = self.clock()
        self.write_log.append(("update", remote_id))
        self._maybe_fault("update", "after")
        return self._remote(remote_id)

    def delete_task(self, remote_id: str) -> None:
        self._maybe_fault("delete", "before")
        self.items.pop(remote_id, None)  # idempotent, like the real clients
        self.write_log.append(("delete", remote_id))
        self._maybe_fault("delete", "after")

    def find_by_marker(self, internal_id: str) -> RemoteTask | None:
        if not self.marker_support:
            return None
        for rid, s in self.items.items():
            if s.marker == internal_id:
                return self._remote(rid)
        return None

    def find_recovery_candidate(self, task: CanonicalTask, internal_id: str) -> RemoteTask | None:
        if self.marker_support:
            return self.find_by_marker(internal_id)
        hits = [
            rid
            for rid, s in self.items.items()
            if s.task.title.strip() == task.title.strip()
            and s.task.assignee == task.assignee
            and s.task.due_date == task.due_date
        ]
        return self._remote(hits[0]) if len(hits) == 1 else None

    # ----------------------------------------------------- test/fixture API --
    def seed(self, task: CanonicalTask, *, marker: str | None = None, when: datetime | None = None) -> str:
        rid = self._store(task, marker)
        if when is not None:
            self.items[rid].last_modified = when
        return rid

    def user_edit(self, remote_id: str, *, when: datetime | None = None, **changes: Any) -> None:
        s = self.items[remote_id]
        s.task = s.task.replace(**changes)
        if "assignee" in changes:
            s.container = self.mapped.get(changes["assignee"] or "")
        s.last_modified = when or self.clock()

    def user_delete(self, remote_id: str) -> None:
        del self.items[remote_id]


def load_fixture_clients(fixture_path: str | Path) -> dict[Side, InMemoryTaskClient]:
    """Build the three mock clients from a JSON fixture file.

    Shape: {"mapped_assignees": {"avery": {...containers per side...}}, "sides":
    {"sp": [{"task": {...}, "marker": "..."}], "todo": [...], "skylight": [...]}}
    """
    raw = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    mapped = raw.get("mapped_assignees", {})
    clients: dict[Side, InMemoryTaskClient] = {}
    for side in ("sp", "todo", "skylight"):
        per_side = {k: v.get(side, f"{side}-container-{k}") for k, v in mapped.items()}
        client = InMemoryTaskClient(side, mapped_assignees=per_side)  # type: ignore[arg-type]
        for entry in raw.get("sides", {}).get(side, []):
            t = entry["task"]
            task = CanonicalTask(
                title=t["title"],
                notes=t.get("notes", ""),
                due_date=date.fromisoformat(t["due_date"]) if t.get("due_date") else None,
                assignee=t.get("assignee"),
                status=t.get("status", "open"),
            )
            client.seed(task, marker=entry.get("marker"))
        clients[side] = client  # type: ignore[index]
    return clients
