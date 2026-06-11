"""Two-way mode (Option A): To Do <-> Skylight with NO SharePoint side.

The engine must behave identically with two clients — the ledger alone is the
system of record. Mirrors the critical three-way scenarios plus the two-way
conflict precedence (To Do has timestamps, Skylight doesn't, so To Do wins
mixed conflicts and ties).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pytest

from skysync.engine import SyncEngine, SyncPolicy
from skysync.ledger import Ledger
from skysync.mock_client import Fault, InMemoryTaskClient, SimulatedCrash
from skysync.models import CanonicalTask

TODAY = date(2026, 6, 11)


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def tick(self, minutes: int = 1) -> datetime:
        self.t += timedelta(minutes=minutes)
        return self.t


@dataclass
class Env2:
    todo: InMemoryTaskClient
    sky: InMemoryTaskClient
    ledger: Ledger
    clock: Clock
    policy: SyncPolicy = field(default_factory=SyncPolicy)

    @property
    def clients(self) -> dict:
        return {"todo": self.todo, "skylight": self.sky}

    def run(self):
        return SyncEngine(self.ledger, self.clients, self.policy, today=lambda: TODAY).run()

    def writes(self) -> int:
        return len(self.todo.write_log) + len(self.sky.write_log)

    def clear_writes(self) -> None:
        self.todo.write_log.clear()
        self.sky.write_log.clear()

    def assert_converged(self) -> None:
        self.clear_writes()
        self.run()
        assert self.writes() == 0, f"not converged: todo={self.todo.write_log} sky={self.sky.write_log}"

    def only_row(self):
        rows = [r for r in self.ledger.all_rows() if not r.deleted]
        assert len(rows) == 1
        return rows[0]


def make_env(policy: SyncPolicy | None = None) -> Env2:
    clock = Clock()
    return Env2(
        todo=InMemoryTaskClient(
            "todo", mapped_assignees={"avery": "list-avery", "blake": "list-blake"}, clock=clock
        ),
        sky=InMemoryTaskClient(
            "skylight", mapped_assignees={"avery": "cat-avery", "blake": "cat-blake"}, clock=clock
        ),
        ledger=Ledger(":memory:"),
        clock=clock,
        policy=policy or SyncPolicy(),
    )


def task(title="Feed the cat", assignee="avery", due=TODAY, status="open", notes="") -> CanonicalTask:
    return CanonicalTask(title=title, notes=notes, due_date=due, assignee=assignee, status=status)


@pytest.mark.parametrize("origin", ["todo", "skylight"])
def test_create_propagates_to_the_other_side(origin):
    env = make_env()
    env.clients[origin].seed(task())
    env.run()
    assert len(env.todo.items) == 1 and len(env.sky.items) == 1
    row = env.only_row()
    assert row.todo_task_id and row.skylight_chore_id
    assert row.sp_item_id is None  # no third side exists, nothing dangles
    env.assert_converged()


@pytest.mark.parametrize("origin", ["todo", "skylight"])
def test_completion_propagates(origin):
    env = make_env()
    env.todo.seed(task())
    env.run()
    env.clients[origin].user_edit(next(iter(env.clients[origin].items)), status="completed")
    env.run()
    for side in ("todo", "skylight"):
        assert next(iter(env.clients[side].items.values())).task.status == "completed", side
    env.assert_converged()


def test_conflict_todo_beats_timestampless_skylight():
    env = make_env()
    env.todo.seed(task())
    env.run()
    env.sky.user_edit(next(iter(env.sky.items)), title="frame edit")
    env.clock.tick()
    env.todo.user_edit(next(iter(env.todo.items)), title="todo edit wins")
    report = env.run()
    assert next(iter(env.sky.items.values())).task.title == "todo edit wins"
    assert any("skylight" in c for c in report.conflicts)
    env.assert_converged()


def test_delete_in_todo_removes_chore_and_tombstones():
    env = make_env()
    env.todo.seed(task())
    env.run()
    env.todo.user_delete(next(iter(env.todo.items)))
    env.run()
    assert env.sky.items == {} and env.todo.items == {}
    rows = env.ledger.all_rows()
    assert len(rows) == 1 and rows[0].deleted
    env.assert_converged()


def test_delete_on_skylight_detaches_and_todo_survives():
    env = make_env()
    env.todo.seed(task())
    env.run()
    env.sky.user_delete(next(iter(env.sky.items)))
    env.run()
    assert len(env.todo.items) == 1 and env.sky.items == {}
    assert env.only_row().sky_detached
    env.assert_converged()
    # edits still sync... to nowhere else, but must not error or resurrect
    env.todo.user_edit(next(iter(env.todo.items)), title="still here")
    env.run()
    assert env.sky.items == {}
    env.assert_converged()


@pytest.mark.parametrize("side", ["todo", "skylight"])
def test_crash_after_create_replay_adopts(side):
    env = make_env()
    origin = "skylight" if side == "todo" else "todo"
    env.clients[origin].seed(task())
    env.clients[side].fault = Fault("create", "after")
    with pytest.raises(SimulatedCrash):
        env.run()
    env.run()
    assert len(env.clients[side].items) == 1, "duplicate on replay"
    assert len([w for w in env.clients[side].write_log if w[0] == "create"]) == 1
    env.assert_converged()


def test_unmapped_assignee_lives_in_todo_only():
    env = make_env()
    env.todo.seed(task(assignee=None))
    env.run()
    assert env.sky.items == {}  # unsupported on skylight
    assert len(env.todo.items) == 1
    env.assert_converged()


def test_undated_task_stamped_for_skylight():
    env = make_env()
    env.todo.seed(task(due=None))
    env.run()
    assert next(iter(env.sky.items.values())).task.due_date == TODAY
    assert next(iter(env.todo.items.values())).task.due_date == TODAY
    env.assert_converged()


def test_ledger_rebuild_two_way_no_duplicates():
    env = make_env()
    env.todo.seed(task())
    env.run()
    env.assert_converged()
    fresh = Ledger(":memory:")
    env2 = Env2(todo=env.todo, sky=env.sky, ledger=fresh, clock=env.clock, policy=env.policy)
    env2.clear_writes()
    env2.run()
    assert len(env2.todo.items) == 1 and len(env2.sky.items) == 1
    assert env2.writes() == 0
    assert len([r for r in fresh.all_rows() if not r.deleted]) == 1
    env2.assert_converged()
