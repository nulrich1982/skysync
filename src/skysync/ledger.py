"""Idempotent sync ledger. ORCHESTRATOR-OWNED — hard constraint #3.

SQLite, two tables:

``ledger``
    One row per logical task, keyed by a stable ``internal_id`` (uuid4, also
    embedded into SharePoint/To Do items as a marker). Stores every remote id
    (``todo_task_id``, ``skylight_chore_id``, ...), the assignee and Skylight
    category, status, the canonical content (JSON) + its ``content_hash``, a
    last-seen *projection hash per side* (delta detection without timestamps),
    ``last_synced``, and a ``deleted`` tombstone flag. Tombstones keep their
    remaining remote ids until each side's delete is confirmed, which makes
    delete propagation resumable and loop-proof.

``pending_ops``
    A write-ahead journal. Every remote write is recorded *before* it is
    attempted (state ``planned`` -> ``executing`` -> ``done``/``failed``), and
    the op's row mutation is committed in the SAME transaction that marks it
    ``done``. A mid-run crash therefore leaves an ``executing`` op behind; the
    next run's recovery pass resolves it by *searching* the remote side
    (marker or content match) instead of blind-creating — replay can adopt,
    never duplicate.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import LedgerCorruptionError
from .models import CanonicalTask, Side

log = logging.getLogger(__name__)

_SIDE_ID_COL = {"sp": "sp_item_id", "todo": "todo_task_id", "skylight": "skylight_chore_id"}
_SIDE_HASH_COL = {"sp": "sp_hash", "todo": "todo_hash", "skylight": "sky_hash"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    internal_id          TEXT PRIMARY KEY,
    sp_item_id           TEXT,
    todo_task_id         TEXT,
    todo_list_id         TEXT,
    skylight_chore_id    TEXT,
    skylight_category_id TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL DEFAULT 'open',
    canonical_json       TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    sp_hash              TEXT,
    todo_hash            TEXT,
    sky_hash             TEXT,
    sky_detached         INTEGER NOT NULL DEFAULT 0,
    deleted              INTEGER NOT NULL DEFAULT 0,
    last_synced          TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_ledger_sp   ON ledger(sp_item_id)        WHERE sp_item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ix_ledger_todo ON ledger(todo_task_id)      WHERE todo_task_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ix_ledger_sky  ON ledger(skylight_chore_id) WHERE skylight_chore_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS pending_ops (
    op_id            TEXT PRIMARY KEY,
    internal_id      TEXT NOT NULL,
    side             TEXT NOT NULL,
    action           TEXT NOT NULL,           -- create|update|delete
    payload          TEXT NOT NULL,           -- desired canonical state + target ids
    state            TEXT NOT NULL,           -- planned|executing|done|failed
    result_remote_id TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ops_state ON pending_ops(state);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_to_json(t: CanonicalTask) -> str:
    return json.dumps(
        {
            "title": t.title,
            "notes": t.notes,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "assignee": t.assignee,
            "status": t.status,
        },
        sort_keys=True,
    )


def canonical_from_json(blob: str) -> CanonicalTask:
    d = json.loads(blob)
    return CanonicalTask(
        title=d["title"],
        notes=d.get("notes", ""),
        due_date=date.fromisoformat(d["due_date"]) if d.get("due_date") else None,
        assignee=d.get("assignee"),
        status=d.get("status", "open"),
    )


@dataclass
class LedgerRow:
    internal_id: str
    sp_item_id: str | None
    todo_task_id: str | None
    todo_list_id: str | None
    skylight_chore_id: str | None
    skylight_category_id: str | None
    assignee: str | None
    status: str
    canonical_json: str
    content_hash: str
    sp_hash: str | None
    todo_hash: str | None
    sky_hash: str | None
    sky_detached: bool
    deleted: bool
    last_synced: str | None
    created_at: str
    updated_at: str

    def canonical(self) -> CanonicalTask:
        return canonical_from_json(self.canonical_json)

    def side_id(self, side: Side) -> str | None:
        return getattr(self, _SIDE_ID_COL[side])

    def side_hash(self, side: Side) -> str | None:
        return getattr(self, _SIDE_HASH_COL[side])


@dataclass
class PendingOp:
    op_id: str
    internal_id: str
    side: Side
    action: str
    payload: dict[str, Any]
    state: str
    result_remote_id: str | None
    error: str | None


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")  # crash safety over speed
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------- rows ----
    def _row(self, r: sqlite3.Row) -> LedgerRow:
        d = dict(r)
        d["sky_detached"] = bool(d["sky_detached"])
        d["deleted"] = bool(d["deleted"])
        return LedgerRow(**d)

    def all_rows(self, include_deleted: bool = True) -> list[LedgerRow]:
        q = "SELECT * FROM ledger" + ("" if include_deleted else " WHERE deleted = 0")
        return [self._row(r) for r in self._db.execute(q)]

    def get(self, internal_id: str) -> LedgerRow | None:
        r = self._db.execute("SELECT * FROM ledger WHERE internal_id = ?", (internal_id,)).fetchone()
        return self._row(r) if r else None

    def find_by_remote(self, side: Side, remote_id: str) -> LedgerRow | None:
        col = _SIDE_ID_COL[side]
        r = self._db.execute(f"SELECT * FROM ledger WHERE {col} = ?", (remote_id,)).fetchone()
        return self._row(r) if r else None

    def insert_row(
        self,
        internal_id: str | None,
        canonical: CanonicalTask,
        content_hash: str,
        **cols: Any,
    ) -> LedgerRow:
        iid = internal_id or str(uuid.uuid4())
        now = _now()
        base = {
            "internal_id": iid,
            "canonical_json": canonical_to_json(canonical),
            "content_hash": content_hash,
            "assignee": canonical.assignee,
            "status": canonical.status,
            "created_at": now,
            "updated_at": now,
        }
        base.update(cols)
        names = ", ".join(base)
        ph = ", ".join("?" for _ in base)
        with self._db:
            self._db.execute(f"INSERT INTO ledger ({names}) VALUES ({ph})", tuple(base.values()))
        row = self.get(iid)
        assert row is not None
        return row

    def update_row(self, internal_id: str, **cols: Any) -> None:
        """Update arbitrary ledger columns in one transaction."""
        self._apply_row_update(self._db, internal_id, cols)
        self._db.commit()

    @staticmethod
    def _apply_row_update(db: sqlite3.Connection, internal_id: str, cols: dict[str, Any]) -> None:
        if not cols:
            return
        cols = dict(cols)
        cols["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in cols)
        cur = db.execute(
            f"UPDATE ledger SET {sets} WHERE internal_id = ?", (*cols.values(), internal_id)
        )
        if cur.rowcount != 1:
            raise LedgerCorruptionError(f"ledger row {internal_id} missing during update")

    def set_canonical(self, internal_id: str, canonical: CanonicalTask, content_hash: str) -> None:
        self.update_row(
            internal_id,
            canonical_json=canonical_to_json(canonical),
            content_hash=content_hash,
            assignee=canonical.assignee,
            status=canonical.status,
        )

    # ----------------------------------------------------- pending ops -----
    def record_op(self, internal_id: str, side: Side, action: str, payload: dict[str, Any]) -> str:
        op_id = str(uuid.uuid4())
        now = _now()
        with self._db:
            self._db.execute(
                "INSERT INTO pending_ops (op_id, internal_id, side, action, payload, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'planned', ?, ?)",
                (op_id, internal_id, side, action, json.dumps(payload, sort_keys=True), now, now),
            )
        return op_id

    def mark_op(self, op_id: str, state: str, error: str | None = None) -> None:
        with self._db:
            self._db.execute(
                "UPDATE pending_ops SET state = ?, error = ?, updated_at = ? WHERE op_id = ?",
                (state, error, _now(), op_id),
            )

    def complete_op(
        self,
        op_id: str,
        internal_id: str,
        row_updates: dict[str, Any],
        result_remote_id: str | None = None,
    ) -> None:
        """Atomically mark the op done AND apply its ledger-row mutation.

        This is the crash-safety hinge: either both are recorded or neither,
        so replay logic can always trust 'done' to mean 'row reflects it'.
        """
        with self._db:
            self._db.execute(
                "UPDATE pending_ops SET state = 'done', result_remote_id = ?, updated_at = ? WHERE op_id = ?",
                (result_remote_id, _now(), op_id),
            )
            row_updates = dict(row_updates)
            row_updates.setdefault("last_synced", _now())
            self._apply_row_update(self._db, internal_id, row_updates)

    def pending_ops(self, states: Iterable[str] = ("planned", "executing")) -> list[PendingOp]:
        marks = ", ".join("?" for _ in tuple(states))
        rows = self._db.execute(
            f"SELECT * FROM pending_ops WHERE state IN ({marks}) ORDER BY created_at", tuple(states)
        ).fetchall()
        return [
            PendingOp(
                op_id=r["op_id"],
                internal_id=r["internal_id"],
                side=r["side"],
                action=r["action"],
                payload=json.loads(r["payload"]),
                state=r["state"],
                result_remote_id=r["result_remote_id"],
                error=r["error"],
            )
            for r in rows
        ]

    def prune_ops(self, keep_done: int = 500) -> None:
        """Keep the journal small; done/failed ops are audit history only."""
        with self._db:
            self._db.execute(
                "DELETE FROM pending_ops WHERE state IN ('done', 'failed') AND op_id NOT IN ("
                " SELECT op_id FROM pending_ops WHERE state IN ('done', 'failed')"
                " ORDER BY updated_at DESC LIMIT ?)",
                (keep_done,),
            )

    # ------------------------------------------------------------- meta ----
    def meta_get(self, key: str) -> str | None:
        r = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_set(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
