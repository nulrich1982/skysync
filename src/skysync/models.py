"""Canonical task model, side projections, hashing, and client protocol.

The engine never compares raw API payloads. Each side adapter converts its
remote object into a ``CanonicalTask`` and exposes a *projection*: the subset
of canonical fields that side can actually represent (Skylight chores have no
notes field, for example). Delta detection hashes the projection, so a change
a side cannot represent never triggers a pointless (and loop-prone) write to
that side.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

Side = Literal["sp", "todo", "skylight"]
SIDES: tuple[Side, ...] = ("sp", "todo", "skylight")

Status = Literal["open", "completed"]


@dataclass(frozen=True)
class CanonicalTask:
    title: str
    notes: str = ""
    due_date: date | None = None
    assignee: str | None = None  # child key from [mapping.children.*]
    status: Status = "open"

    def replace(self, **kw: Any) -> "CanonicalTask":
        from dataclasses import replace as _replace

        return _replace(self, **kw)


@dataclass
class RemoteTask:
    """A task as it currently exists on one side."""

    side: Side
    remote_id: str
    task: CanonicalTask
    # Internal id embedded in the remote object (SP InternalId column,
    # To Do linkedResource). None for sides without marker support (Skylight).
    marker_internal_id: str | None = None
    last_modified: datetime | None = None
    etag: str | None = None
    raw: Any = field(default=None, repr=False)
    # Extra side-specific routing info (e.g. To Do list id the task lives in).
    container_id: str | None = None


def canonical_hash(projection: dict[str, Any]) -> str:
    """Stable hash of a side projection (sorted keys, ISO dates)."""

    def _default(o: Any) -> str:
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        raise TypeError(f"unhashable projection value: {type(o)!r}")

    blob = json.dumps(projection, sort_keys=True, separators=(",", ":"), default=_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TaskClient(Protocol):
    """Contract every side adapter implements. The engine depends only on this.

    All write methods must be *absolute*: they push the full desired state, so
    replaying them after a crash is harmless (idempotent updates/deletes).
    Creates are NOT idempotent upstream; the engine guards them with the
    pending-ops journal plus ``find_by_marker``/``find_recovery_candidate``.

    Normalization contract (critical for loop-proofing): ``list_tasks`` and
    ``project`` must normalize identically — titles/notes stripped, due dates
    as ISO ``YYYY-MM-DD`` or None, status ``open``/``completed`` — so that a
    value we wrote hashes the same when read back. A side that normalizes
    differently upstream converges in one extra cycle instead of looping,
    because hashes stabilize.
    """

    side: Side

    # Canonical fields this side can represent at all. Sides merge INTO the
    # canonical task only these fields when they change.
    PROJECTION_FIELDS: tuple[str, ...]

    def representable(self, fieldname: str, value: Any) -> bool:
        """Whether this side can faithfully express ``value`` for ``field``
        (e.g. To Do can only express assignees that map to a list). A side's
        report of an unrepresentable field is never merged into canonical —
        otherwise a title edit in To Do could clobber an assignee it cannot
        see."""
        ...

    def supports(self, task: CanonicalTask) -> bool:
        """Whether this side should hold this task at all (e.g. Skylight
        requires an assignee that maps to a category). Unsupported tasks get
        no ops planned for this side."""
        ...

    def list_tasks(self) -> list[RemoteTask]: ...

    def create_task(self, task: CanonicalTask, internal_id: str) -> RemoteTask: ...

    def update_task(self, remote: str, task: CanonicalTask, internal_id: str) -> RemoteTask: ...

    def delete_task(self, remote_id: str) -> None: ...

    def find_by_marker(self, internal_id: str) -> RemoteTask | None:
        """Locate a task by embedded internal id (crash recovery). Sides
        without marker support return None unconditionally."""
        ...

    def find_recovery_candidate(self, task: CanonicalTask, internal_id: str) -> RemoteTask | None:
        """Marker-less recovery (Skylight): best-effort match of an in-flight
        create by content. Must only return high-confidence matches."""
        ...

    def project(self, task: CanonicalTask) -> dict[str, Any]:
        """The subset of canonical fields this side can represent, normalized
        exactly as ``list_tasks`` would report them after a round-trip."""
        ...
