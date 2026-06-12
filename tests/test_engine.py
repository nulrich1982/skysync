"""Exhaustive reconciliation-engine tests (orchestrator-authored).

Covers: create/update/complete/delete originating on each side; conflict
policies; mid-run crash + replay (no duplicates); orphaned rows; ledger
rebuild; schema drift; expired tokens; loop-proofing incl. upstream value
normalization; unmapped assignees; the windowed-Skylight absence guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pytest

from skysync.engine import SyncEngine, SyncPolicy
from skysync.errors import AuthError, SchemaDriftError, TransientApiError
from skysync.ledger import Ledger
from skysync.mock_client import Fault, InMemoryTaskClient, SimulatedCrash
from skysync.models import CanonicalTask

TODAY = date(2026, 6, 10)


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def tick(self, minutes: int = 1) -> datetime:
        self.t += timedelta(minutes=minutes)
        return self.t


@dataclass
class Env:
    sp: InMemoryTaskClient
    todo: InMemoryTaskClient
    sky: InMemoryTaskClient
    ledger: Ledger
    clock: Clock
    policy: SyncPolicy = field(default_factory=SyncPolicy)

    @property
    def clients(self) -> dict:
        return {"sp": self.sp, "todo": self.todo, "skylight": self.sky}

    def run(self):
        """Fresh engine per run, like real scheduled runs."""
        return SyncEngine(self.ledger, self.clients, self.policy, today=lambda: TODAY).run()

    def writes(self) -> int:
        return len(self.sp.write_log) + len(self.todo.write_log) + len(self.sky.write_log)

    def clear_writes(self) -> None:
        self.sp.write_log.clear()
        self.todo.write_log.clear()
        self.sky.write_log.clear()

    def assert_converged(self) -> None:
        """A converged system performs ZERO writes on the next run."""
        self.clear_writes()
        self.run()
        assert self.writes() == 0, (
            f"not converged: sp={self.sp.write_log} todo={self.todo.write_log} sky={self.sky.write_log}"
        )

    def only_row(self):
        rows = [r for r in self.ledger.all_rows() if not r.deleted]
        assert len(rows) == 1, f"expected 1 active row, got {len(rows)}"
        return rows[0]


def make_env(policy: SyncPolicy | None = None) -> Env:
    clock = Clock()
    return Env(
        sp=InMemoryTaskClient("sp", mapped_assignees={"avery": "sp", "blake": "sp"}, clock=clock),
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


def converged_env(**task_kw) -> tuple[Env, CanonicalTask]:
    env = make_env()
    t = task(**task_kw)
    env.sp.seed(t)
    env.run()
    env.assert_converged()
    env.clear_writes()
    return env, t


# ---------------------------------------------------------------- creates ----


@pytest.mark.parametrize("origin", ["sp", "todo", "skylight"])
def test_create_propagates_everywhere(origin):
    env = make_env()
    client = env.clients[origin]
    client.seed(task())
    env.run()

    assert len(env.sp.items) == 1 and len(env.todo.items) == 1 and len(env.sky.items) == 1
    row = env.only_row()
    assert row.sp_item_id and row.todo_task_id and row.skylight_chore_id
    assert row.last_synced is not None
    assert row.assignee == "avery" and row.status == "open"
    # engine-created items on marker-capable sides carry the marker (the
    # origin item was created by the user, so it has none — content binding
    # covers it on a rebuild)
    for side in ("sp", "todo"):
        if side == origin:
            continue
        stored = next(iter(env.clients[side].items.values()))
        assert stored.marker == row.internal_id
    # to do task landed in the child's list, chore in the child's category
    assert next(iter(env.todo.items.values())).container == "list-avery"
    assert next(iter(env.sky.items.values())).container == "cat-avery"
    env.assert_converged()


def test_create_from_skylight_has_empty_notes():
    env = make_env()
    env.sky.seed(task(notes=""))
    env.run()
    assert next(iter(env.sp.items.values())).task.notes == ""
    env.assert_converged()


def test_completed_task_is_not_backfilled_to_other_sides():
    """Done is done: an already-completed task is never CREATED on sides that
    don't have it — and skipping it must not loop (zero writes thereafter)."""
    env = make_env()
    env.sp.seed(task(status="completed", due=None))
    env.run()
    assert env.todo.items == {} and env.sky.items == {}
    row = env.only_row()
    assert row.status == "completed"
    assert row.canonical().due_date is None  # and no date was stamped on it
    env.assert_converged()


def test_backfill_completed_policy_restores_old_behavior():
    env = make_env(SyncPolicy(backfill_completed=True))
    env.sp.seed(task(status="completed"))
    env.run()
    assert next(iter(env.todo.items.values())).task.status == "completed"
    assert next(iter(env.sky.items.values())).task.status == "completed"
    env.assert_converged()


def test_create_cap_drains_backlog_across_runs():
    env = make_env(SyncPolicy(max_creates_per_side=2))
    for i in range(5):
        env.sp.seed(task(title=f"Task {i}"))
    r1 = env.run()
    assert len(env.todo.items) == 2 and len(env.sky.items) == 2
    assert r1.counts["deferred_creates_todo"] == 3
    env.run()
    assert len(env.todo.items) == 4
    env.run()
    assert len(env.todo.items) == 5 and len(env.sky.items) == 5
    env.assert_converged()


# ------------------------------------------------------- updates / completes ----


@pytest.mark.parametrize("origin", ["sp", "todo", "skylight"])
def test_title_edit_propagates(origin):
    env, _ = converged_env()
    client = env.clients[origin]
    rid = next(iter(client.items))
    client.user_edit(rid, title="Feed the cat TWICE")
    env.run()
    for side in ("sp", "todo", "skylight"):
        assert next(iter(env.clients[side].items.values())).task.title == "Feed the cat TWICE"
    env.assert_converged()


@pytest.mark.parametrize("origin", ["sp", "todo", "skylight"])
def test_completion_propagates_both_ways(origin):
    env, _ = converged_env()
    client = env.clients[origin]
    rid = next(iter(client.items))
    client.user_edit(rid, status="completed")
    env.run()
    for side in ("sp", "todo", "skylight"):
        assert next(iter(env.clients[side].items.values())).task.status == "completed", side
    row = env.only_row()
    assert row.status == "completed"
    env.assert_converged()


def test_notes_edit_does_not_touch_skylight():
    env, _ = converged_env(notes="bring the brush")
    rid = next(iter(env.sp.items))
    env.sp.user_edit(rid, notes="bring the brush AND a towel")
    env.clear_writes()
    env.run()
    assert env.sky.write_log == []  # skylight cannot represent notes; no write
    assert next(iter(env.todo.items.values())).task.notes == "bring the brush AND a towel"
    env.assert_converged()


def test_due_date_change_propagates():
    env, _ = converged_env()
    rid = next(iter(env.todo.items))
    env.todo.user_edit(rid, due_date=TODAY + timedelta(days=3))
    env.run()
    assert next(iter(env.sky.items.values())).task.due_date == TODAY + timedelta(days=3)
    env.assert_converged()


def test_assignee_change_moves_containers():
    env, _ = converged_env()
    rid = next(iter(env.sp.items))
    env.sp.user_edit(rid, assignee="blake")
    env.run()
    row = env.only_row()
    todo_stored = next(iter(env.todo.items.values()))
    assert todo_stored.container == "list-blake"
    assert row.todo_task_id in env.todo.items  # new id after the simulated move
    assert next(iter(env.sky.items.values())).container == "cat-blake"
    assert row.assignee == "blake"
    env.assert_converged()


# ----------------------------------------------------------------- deletes ----


def test_delete_in_todo_propagates_to_skylight_and_sp():
    env, _ = converged_env()
    env.todo.user_delete(next(iter(env.todo.items)))
    env.run()
    assert env.sp.items == {} and env.sky.items == {}
    rows = env.ledger.all_rows()
    assert len(rows) == 1 and rows[0].deleted
    assert rows[0].sp_item_id is None and rows[0].skylight_chore_id is None
    env.assert_converged()


def test_delete_on_skylight_only_detaches():
    env, _ = converged_env()
    env.sky.user_delete(next(iter(env.sky.items)))
    env.clear_writes()
    env.run()
    # SP and To Do untouched; row detached from skylight; chore NOT re-created
    assert len(env.sp.items) == 1 and len(env.todo.items) == 1
    assert env.sky.items == {}
    row = env.only_row()
    assert row.sky_detached and row.skylight_chore_id is None
    env.assert_converged()
    # later edits still flow sp <-> todo, skylight stays detached
    env.sp.user_edit(next(iter(env.sp.items)), title="still synced")
    env.run()
    assert next(iter(env.todo.items.values())).task.title == "still synced"
    assert env.sky.items == {}
    env.assert_converged()


def test_delete_in_sharepoint_propagates():
    env, _ = converged_env()
    env.sp.user_delete(next(iter(env.sp.items)))
    env.run()
    assert env.todo.items == {} and env.sky.items == {}
    env.assert_converged()


def test_todo_delete_policy_off_detaches_only():
    env = make_env(SyncPolicy(deletes_todo_to_skylight=False))
    env.sp.seed(task())
    env.run()
    env.todo.user_delete(next(iter(env.todo.items)))
    env.run()
    assert len(env.sp.items) == 1 and len(env.sky.items) == 1
    row = env.only_row()
    assert row.todo_task_id is not None  # re-created? No: detached then re-created as missing side
    # NOTE: with the policy off, the detach clears the binding and the next
    # propagate re-creates the task on To Do (it is a supported side with no
    # item). That is the documented semantics of "deletes don't propagate".
    env.assert_converged()


def test_orphaned_row_every_side_gone():
    env, _ = converged_env()
    env.sp.user_delete(next(iter(env.sp.items)))
    env.todo.user_delete(next(iter(env.todo.items)))
    env.sky.user_delete(next(iter(env.sky.items)))
    env.clear_writes()
    env.run()
    rows = env.ledger.all_rows()
    assert len(rows) == 1 and rows[0].deleted
    assert env.writes() == 0
    env.assert_converged()


def test_skylight_absence_outside_window_is_ignored():
    far = TODAY + timedelta(days=90)  # beyond the 60-day fetch window
    env = make_env()
    env.sp.seed(task(due=far))
    env.run()
    # simulate the chore falling outside the windowed fetch
    env.sky.user_delete(next(iter(env.sky.items)))
    env.clear_writes()
    env.run()
    row = env.only_row()
    assert not row.sky_detached
    assert row.skylight_chore_id is not None  # absence proved nothing
    assert len(env.sp.items) == 1 and len(env.todo.items) == 1


# ---------------------------------------------------------------- conflicts ----


def test_conflict_most_recent_wins_and_loser_logged():
    env, _ = converged_env()
    env.todo.user_edit(next(iter(env.todo.items)), title="todo version", when=env.clock.tick())
    env.sp.user_edit(next(iter(env.sp.items)), title="sp version LATER", when=env.clock.tick())
    report = env.run()
    for side in ("sp", "todo", "skylight"):
        assert next(iter(env.clients[side].items.values())).task.title == "sp version LATER"
    assert len(report.conflicts) == 1
    assert "todo" in report.conflicts[0] and "todo version" in report.conflicts[0]
    env.assert_converged()


def test_conflict_younger_todo_beats_older_sp():
    env, _ = converged_env()
    env.sp.user_edit(next(iter(env.sp.items)), title="sp first", when=env.clock.tick())
    env.todo.user_edit(next(iter(env.todo.items)), title="todo later", when=env.clock.tick())
    env.run()
    assert next(iter(env.sp.items.values())).task.title == "todo later"
    env.assert_converged()


def test_conflict_skylight_without_timestamp_loses():
    env, _ = converged_env()
    env.sky.user_edit(next(iter(env.sky.items)), status="completed")
    env.clock.tick()
    env.sp.user_edit(next(iter(env.sp.items)), title="sp edit wins")
    report = env.run()
    # sp wins wholly: title applied, skylight's completion discarded (logged)
    row = env.only_row()
    assert row.status == "open"
    assert next(iter(env.sky.items.values())).task.status == "open"
    assert next(iter(env.sky.items.values())).task.title == "sp edit wins"
    assert any("skylight" in c for c in report.conflicts)
    env.assert_converged()


def test_conflict_timestamp_tie_resolves_by_sp_precedence():
    env, _ = converged_env()
    when = env.clock.tick()
    env.todo.user_edit(next(iter(env.todo.items)), title="todo tie", when=when)
    env.sp.user_edit(next(iter(env.sp.items)), title="sp tie", when=when)
    env.run()
    assert next(iter(env.todo.items.values())).task.title == "sp tie"
    env.assert_converged()


def test_sharepoint_wins_policy():
    env = make_env(SyncPolicy(conflict_policy="sharepoint_wins"))
    env.sp.seed(task())
    env.run()
    env.sp.user_edit(next(iter(env.sp.items)), title="sp old", when=env.clock.tick())
    env.todo.user_edit(next(iter(env.todo.items)), title="todo newer", when=env.clock.tick())
    env.run()
    assert next(iter(env.todo.items.values())).task.title == "sp old"
    env.assert_converged()


def test_single_side_change_is_not_a_conflict():
    env, _ = converged_env()
    env.todo.user_edit(next(iter(env.todo.items)), title="just an edit")
    report = env.run()
    assert report.conflicts == []


# ------------------------------------------------------ crash + replay safety ----


@pytest.mark.parametrize("side", ["todo", "skylight"])
def test_crash_after_create_applied_adopts_no_duplicate(side):
    """Crash AFTER the remote create but BEFORE recording it: replay must
    find and adopt the item, never create a second one."""
    env = make_env()
    env.sp.seed(task())
    env.clients[side].fault = Fault("create", "after")
    with pytest.raises(SimulatedCrash):
        env.run()
    env.run()  # replay
    assert len(env.clients[side].items) == 1, "duplicate created on replay!"
    creates = [w for w in env.clients[side].write_log if w[0] == "create"]
    assert len(creates) == 1
    row = env.only_row()
    assert row.side_id(side) == next(iter(env.clients[side].items))
    env.assert_converged()


@pytest.mark.parametrize("side", ["todo", "skylight"])
def test_crash_before_create_applied_recreates_once(side):
    env = make_env()
    env.sp.seed(task())
    env.clients[side].fault = Fault("create", "before")
    with pytest.raises(SimulatedCrash):
        env.run()
    env.run()
    assert len(env.clients[side].items) == 1
    env.assert_converged()


def test_crash_mid_delete_propagation_resumes():
    env, _ = converged_env()
    env.todo.user_delete(next(iter(env.todo.items)))
    env.sky.fault = Fault("delete", "before")
    with pytest.raises(SimulatedCrash):
        env.run()
    env.run()
    assert env.sp.items == {} and env.sky.items == {} and env.todo.items == {}
    env.assert_converged()


def test_crash_mid_todo_move_recovers_via_marker():
    env, _ = converged_env()
    env.sp.user_edit(next(iter(env.sp.items)), assignee="blake")
    env.todo.fault = Fault("update", "after")  # move applied, never recorded
    with pytest.raises(SimulatedCrash):
        env.run()
    env.run()
    assert len(env.todo.items) == 1
    row = env.only_row()
    assert row.todo_task_id == next(iter(env.todo.items))
    assert next(iter(env.todo.items.values())).container == "list-blake"
    env.assert_converged()


def test_stray_marker_duplicate_is_deleted():
    env, _ = converged_env()
    row = env.only_row()
    env.todo.seed(task(title="stray copy"), marker=row.internal_id)
    env.run()
    assert len(env.todo.items) == 1
    assert env.only_row().todo_task_id == next(iter(env.todo.items))
    env.assert_converged()


def test_transient_error_aborts_then_next_run_recovers():
    env = make_env()
    env.sp.seed(task())
    env.todo.fault = Fault("create", "before", exc=TransientApiError("503 from Graph", status=503))
    with pytest.raises(TransientApiError):
        env.run()
    env.run()
    assert len(env.todo.items) == 1
    env.assert_converged()


def test_schema_drift_in_snapshot_aborts_cleanly(monkeypatch):
    env, _ = converged_env()
    rows_before = {r.internal_id: r.content_hash for r in env.ledger.all_rows()}

    def boom():
        raise SchemaDriftError("skylight changed shape", "data is now a dict")

    monkeypatch.setattr(env.sky, "list_tasks", boom)
    with pytest.raises(SchemaDriftError):
        env.run()
    assert {r.internal_id: r.content_hash for r in env.ledger.all_rows()} == rows_before
    monkeypatch.undo()
    env.assert_converged()


def test_schema_drift_during_write_aborts_and_replays():
    env = make_env()
    env.sp.seed(task())
    env.sky.fault = Fault("create", "before", exc=SchemaDriftError("response shape changed"))
    with pytest.raises(SchemaDriftError):
        env.run()
    env.run()
    assert len(env.sky.items) == 1
    env.assert_converged()


def test_expired_graph_token_aborts_before_any_write(monkeypatch):
    env = make_env()
    env.sp.seed(task())

    def expired():
        raise AuthError("refresh token expired; run login")

    monkeypatch.setattr(env.todo, "list_tasks", expired)
    with pytest.raises(AuthError):
        env.run()
    assert env.todo.write_log == [] and env.sky.write_log == []
    monkeypatch.undo()
    env.run()
    assert len(env.todo.items) == 1
    env.assert_converged()


# ----------------------------------------------------- loop-proofing extras ----


def test_upstream_normalization_converges_and_stops():
    """Skylight 'normalizes' titles upstream; the system must converge (one
    inbound correction) and then stop — never ping-pong."""
    env = make_env()
    env.sky.normalize = lambda t: t.replace(title=t.title.upper())
    env.sp.seed(task(title="feed the cat"))
    env.run()  # creates; skylight stores FEED THE CAT
    env.run()  # inbound normalization absorbed, propagated to sp/todo
    assert next(iter(env.sp.items.values())).task.title == "FEED THE CAT"
    sky_creates = [w for w in env.sky.write_log if w[0] == "create"]
    assert len(sky_creates) == 1  # never re-created or ping-ponged
    env.assert_converged()


def test_unmapped_assignee_skips_skylight_and_survives_todo_edits():
    env = make_env()
    env.sp.seed(task(assignee="grandma"))
    env.run()
    assert env.sky.items == {}  # no category for grandma
    assert len(env.todo.items) == 1  # default list (no container)
    env.assert_converged()
    # an edit in To Do must not clobber the assignee To Do cannot represent
    env.todo.user_edit(next(iter(env.todo.items)), title="watered the plants")
    env.run()
    sp_stored = next(iter(env.sp.items.values()))
    assert sp_stored.task.title == "watered the plants"
    assert sp_stored.task.assignee == "grandma"
    env.assert_converged()


def test_undated_task_gets_today_stamped_for_skylight():
    env = make_env()
    env.sp.seed(task(due=None))
    env.run()
    assert len(env.sky.items) == 1
    for side in ("sp", "todo", "skylight"):
        assert next(iter(env.clients[side].items.values())).task.due_date == TODAY, side
    env.assert_converged()


def test_clearing_due_date_restamps_instead_of_looping():
    """Skylight cannot clear a chore's start date; clearing the due date in
    SP must re-stamp today everywhere, not replan the same write forever."""
    env, _ = converged_env(due=TODAY + timedelta(days=2))
    env.sp.user_edit(next(iter(env.sp.items)), due_date=None)
    env.run()
    for side in ("sp", "todo", "skylight"):
        assert next(iter(env.clients[side].items.values())).task.due_date == TODAY, side
    env.assert_converged()


def test_undated_unmapped_task_keeps_no_due_date():
    env = make_env()
    env.sp.seed(task(assignee=None, due=None))
    env.run()
    assert env.sky.items == {}
    assert next(iter(env.sp.items.values())).task.due_date is None
    env.assert_converged()


def test_ledger_rebuild_from_scratch_creates_no_duplicates():
    env, _ = converged_env()
    fresh = Ledger(":memory:")
    env2 = Env(sp=env.sp, todo=env.todo, sky=env.sky, ledger=fresh, clock=env.clock, policy=env.policy)
    env2.clear_writes()
    env2.run()
    assert len(env2.sp.items) == 1 and len(env2.todo.items) == 1 and len(env2.sky.items) == 1
    assert env2.writes() == 0  # marker + content binding, zero remote churn
    rows = [r for r in fresh.all_rows() if not r.deleted]
    assert len(rows) == 1
    assert rows[0].sp_item_id and rows[0].todo_task_id and rows[0].skylight_chore_id
    env2.assert_converged()


def test_two_identical_tasks_stay_distinct():
    env = make_env()
    env.sp.seed(task(title="Make bed"))
    env.run()
    env.assert_converged()
    env.sp.seed(task(title="Make bed"))  # genuinely a second task
    env.run()
    assert len(env.sp.items) == 2 and len(env.todo.items) == 2 and len(env.sky.items) == 2
    env.assert_converged()


def test_assignee_change_to_unmapped_removes_skylight_chore():
    env, _ = converged_env()
    env.sp.user_edit(next(iter(env.sp.items)), assignee="grandma")
    env.run()
    assert env.sky.items == {}  # skylight can no longer hold it
    assert len(env.todo.items) == 1
    env.assert_converged()
