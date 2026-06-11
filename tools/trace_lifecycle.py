"""Self-verification trace: one task's create -> complete -> delete lifecycle
through the reconciliation engine in mock mode, asserting zero duplicates and
full convergence at every step.

    python tools/trace_lifecycle.py
"""

from __future__ import annotations

import json
from datetime import date

from skysync.engine import SyncEngine, SyncPolicy
from skysync.ledger import Ledger
from skysync.mock_client import InMemoryTaskClient
from skysync.models import CanonicalTask

TODAY = date.today()

clients = {
    "sp": InMemoryTaskClient("sp", mapped_assignees={"avery": "sp"}),
    "todo": InMemoryTaskClient("todo", mapped_assignees={"avery": "list-avery"}),
    "skylight": InMemoryTaskClient("skylight", mapped_assignees={"avery": "cat-avery"}),
}
ledger = Ledger(":memory:")


def run(step: str) -> None:
    report = SyncEngine(ledger, clients, SyncPolicy(), today=lambda: TODAY).run()
    counts = {side: len(c.items) for side, c in clients.items()}
    rows = ledger.all_rows()
    print(f"\n== {step}")
    print(f"   items per side : {counts}")
    print(f"   ledger         : {len(rows)} row(s), "
          f"{sum(1 for r in rows if r.deleted)} tombstoned")
    print(f"   engine counts  : {json.dumps(report.counts)}")


def assert_each_side(n: int) -> None:
    for side, c in clients.items():
        assert len(c.items) == n, f"{side} has {len(c.items)} items, expected {n} — DUPLICATE/LOSS"


def assert_converged() -> None:
    for c in clients.values():
        c.write_log.clear()
    SyncEngine(ledger, clients, SyncPolicy(), today=lambda: TODAY).run()
    writes = {s: list(c.write_log) for s, c in clients.items() if c.write_log}
    assert not writes, f"not converged, extra writes: {writes}"


# 1. CREATE on SharePoint (the master)
clients["sp"].seed(CanonicalTask(title="Trace: water the plants", notes="back porch too",
                                 due_date=TODAY, assignee="avery", status="open"))
run("CREATE on SharePoint -> propagates to To Do + Skylight")
assert_each_side(1)
assert_converged()

# 2. COMPLETE on the Skylight frame
sky_id = next(iter(clients["skylight"].items))
clients["skylight"].user_edit(sky_id, status="completed")
run("COMPLETE on Skylight -> propagates to SharePoint + To Do")
assert_each_side(1)
for side, c in clients.items():
    st = next(iter(c.items.values())).task.status
    assert st == "completed", f"{side} status is {st}"
assert_converged()

# 3. DELETE in To Do
clients["todo"].user_delete(next(iter(clients["todo"].items)))
run("DELETE in To Do -> removes Skylight chore + SharePoint row")
assert_each_side(0)
rows = ledger.all_rows()
assert len(rows) == 1 and rows[0].deleted, "expected exactly one tombstoned row"
assert rows[0].sp_item_id is None and rows[0].todo_task_id is None and rows[0].skylight_chore_id is None
assert_converged()

print("\nLIFECYCLE TRACE PASSED: zero duplicates, zero loops, clean tombstone.")
