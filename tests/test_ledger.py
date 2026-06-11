"""Ledger unit tests: persistence, atomicity of complete_op, journal lifecycle."""

from __future__ import annotations

from datetime import date

import pytest

from skysync.errors import LedgerCorruptionError
from skysync.ledger import Ledger, canonical_from_json, canonical_to_json
from skysync.models import CanonicalTask


def t(**kw) -> CanonicalTask:
    base = dict(title="Sweep", notes="under the table too", due_date=date(2026, 6, 12), assignee="avery", status="open")
    base.update(kw)
    return CanonicalTask(**base)


def test_canonical_json_roundtrip():
    task = t()
    assert canonical_from_json(canonical_to_json(task)) == task
    undated = t(due_date=None, assignee=None)
    assert canonical_from_json(canonical_to_json(undated)) == undated


def test_insert_get_find():
    led = Ledger(":memory:")
    row = led.insert_row(None, t(), "hash1", sp_item_id="sp-1", sp_hash="h-sp")
    assert led.get(row.internal_id).sp_item_id == "sp-1"
    assert led.find_by_remote("sp", "sp-1").internal_id == row.internal_id
    assert led.find_by_remote("todo", "sp-1") is None
    assert row.canonical() == t()


def test_remote_ids_are_unique():
    import sqlite3

    led = Ledger(":memory:")
    led.insert_row(None, t(), "h", todo_task_id="todo-1")
    with pytest.raises(sqlite3.IntegrityError):
        led.insert_row(None, t(title="other"), "h2", todo_task_id="todo-1")


def test_complete_op_is_atomic_op_plus_row():
    led = Ledger(":memory:")
    row = led.insert_row(None, t(), "h")
    op_id = led.record_op(row.internal_id, "todo", "create", {"task": canonical_to_json(t())})
    led.mark_op(op_id, "executing")
    led.complete_op(op_id, row.internal_id, {"todo_task_id": "todo-9", "todo_hash": "h-t"}, "todo-9")
    assert led.pending_ops(("planned", "executing")) == []
    fresh = led.get(row.internal_id)
    assert fresh.todo_task_id == "todo-9" and fresh.todo_hash == "h-t"
    assert fresh.last_synced is not None


def test_complete_op_on_missing_row_fails_whole_transaction():
    led = Ledger(":memory:")
    row = led.insert_row(None, t(), "h")
    op_id = led.record_op(row.internal_id, "todo", "create", {})
    with pytest.raises(LedgerCorruptionError):
        led.complete_op(op_id, "no-such-row", {"todo_task_id": "x"})
    # op must NOT have been marked done if the row update failed
    assert [o.op_id for o in led.pending_ops(("planned",))] == [op_id]


def test_pending_ops_lifecycle_and_prune():
    led = Ledger(":memory:")
    row = led.insert_row(None, t(), "h")
    ids = [led.record_op(row.internal_id, "sp", "update", {"n": i}) for i in range(3)]
    led.mark_op(ids[0], "failed", "boom")
    assert {o.op_id for o in led.pending_ops(("planned",))} == set(ids[1:])
    assert led.pending_ops(("failed",))[0].error == "boom"
    led.mark_op(ids[1], "done")
    led.mark_op(ids[2], "done")
    led.prune_ops(keep_done=1)
    assert len(led.pending_ops(("done", "failed"))) == 1


def test_meta_roundtrip():
    led = Ledger(":memory:")
    assert led.meta_get("last_run_utc") is None
    led.meta_set("last_run_utc", "2026-06-10T00:00:00+00:00")
    led.meta_set("last_run_utc", "2026-06-10T00:15:00+00:00")
    assert led.meta_get("last_run_utc") == "2026-06-10T00:15:00+00:00"


def test_persists_to_disk(tmp_path):
    p = tmp_path / "ledger.sqlite3"
    led = Ledger(p)
    row = led.insert_row(None, t(), "h", skylight_chore_id="sky-5", skylight_category_id="cat-1")
    led.close()
    led2 = Ledger(p)
    fresh = led2.get(row.internal_id)
    assert fresh.skylight_chore_id == "sky-5" and fresh.skylight_category_id == "cat-1"
    led2.close()
