"""Reconciliation engine. ORCHESTRATOR-OWNED — hard constraint #3.

One run:

1. **Recover** — resolve any ``executing`` ops left by a crash: for creates,
   search the remote side (marker, else high-confidence content match) and
   *adopt* the result instead of re-creating; interrupted updates/deletes are
   dropped (they are absolute / idempotent and will be re-derived).
2. **Snapshot** — ``list_tasks()`` from every side.
3. **Match** — bind snapshot items to ledger rows by remote id, then adopt
   marker-bearing items whose ids changed (heals lost links); marker items
   whose row slot is already occupied are stray duplicates from a crashed
   move and get deleted.
4. **New rows** — remaining unmatched items become ledger rows; missing sides
   get creates planned (SharePoint, the system of record, first).
5. **Absence** — a row's side id that vanished from the snapshot is a remote
   delete: tombstone + propagate, or detach, per the delete policy. Skylight
   absence is only trusted when the task's due date is inside the fetch
   window (the chores endpoint is windowed).
6. **Diff & conflict** — per side, hash the remote's *projection* and compare
   with the stored last-seen hash. A side whose remote equals the current
   canonical projection is silently absorbed (that's our own write landing).
   One real change wins outright; several -> conflict policy
   (most-recently-modified wins; sides without timestamps lose to sides with
   them; remaining ties resolve sp > todo > skylight). The discarded side's
   content is logged.
7. **Propagate** — the new canonical state is pushed to every side whose
   projection hash differs (writes are absolute, hence replay-safe).
8. **Execute** — every write journaled planned -> executing -> done, with the
   ledger-row mutation committed atomically with 'done'.

Loop-proofing: we never write a side whose projection already hashes equal to
canonical, and our own writes are recognized on the next run by hash equality
— no timestamps involved, no echo loops. A remote that normalizes our values
shows up as a one-time inbound change and converges.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

from .errors import AuthError, PermanentApiError, SchemaDriftError, TransientApiError
from .ledger import Ledger, LedgerRow, PendingOp, canonical_from_json, canonical_to_json
from .models import SIDES, CanonicalTask, RemoteTask, Side, TaskClient, canonical_hash

log = logging.getLogger(__name__)

_SIDE_ID_COL = {"sp": "sp_item_id", "todo": "todo_task_id", "skylight": "skylight_chore_id"}
_SIDE_HASH_COL = {"sp": "sp_hash", "todo": "todo_hash", "skylight": "sky_hash"}
_SIDE_CONTAINER_COL = {"sp": None, "todo": "todo_list_id", "skylight": "skylight_category_id"}
# Conflict tie-break and create/update order: master first.
_PRECEDENCE: tuple[Side, ...] = ("sp", "todo", "skylight")


@dataclass
class SyncPolicy:
    conflict_policy: Literal["most_recent_wins", "sharepoint_wins"] = "most_recent_wins"
    deletes_todo_to_skylight: bool = True
    deletes_skylight_to_todo: bool = False
    deletes_sharepoint_propagate: bool = True
    undated_due_today: bool = True
    sky_window_past_days: int = 14
    sky_window_future_days: int = 60
    # Chores are fetched in a date window, so absence outside it proves
    # nothing. List items (grocery sync) are fetched unwindowed — there,
    # absence IS a delete and this flag makes the engine trust it.
    sky_absence_trusted: bool = False
    # Never CREATE an already-completed task on a side that doesn't have it —
    # done is done; completions still propagate to sides that DO have it.
    backfill_completed: bool = False
    # Safety valve: at most this many creates per side per run; the surplus
    # re-derives next run (15-min cadence drains a backlog gradually instead
    # of slamming an unofficial API — or amplifying a bug — in one shot).
    max_creates_per_side: int = 100


@dataclass
class PlannedOp:
    op_id: str
    internal_id: str
    side: Side
    action: Literal["create", "update", "delete"]
    task: CanonicalTask | None = None
    target_remote_id: str | None = None
    desired_hash: str | None = None
    stray: bool = False  # delete of a duplicate not bound to row columns


@dataclass
class RunReport:
    counts: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def summary(self) -> dict:
        return {"counts": self.counts, "conflicts": self.conflicts, "failures": self.failures}


def full_hash(task: CanonicalTask) -> str:
    """Content hash over the complete canonical task (ledger.content_hash)."""
    return canonical_hash(
        {
            "title": task.title,
            "notes": task.notes,
            "due_date": task.due_date.isoformat() if task.due_date else None,
            "assignee": task.assignee,
            "status": task.status,
        }
    )


class SyncEngine:
    def __init__(
        self,
        ledger: Ledger,
        clients: dict[Side, TaskClient],
        policy: SyncPolicy,
        today: "callable[[], date]" = date.today,
    ):
        self.ledger = ledger
        self.clients = clients
        self.policy = policy
        self.today = today
        # (internal_id, side) -> RemoteTask currently present
        self._present: dict[tuple[str, Side], RemoteTask] = {}
        self._plan: list[PlannedOp] = []
        self._report = RunReport()
        self._creates_planned: dict[Side, int] = {}

    # ------------------------------------------------------------ helpers --
    def _proj_hash(self, side: Side, task: CanonicalTask) -> str:
        return canonical_hash(self.clients[side].project(task))

    def _plan_op(
        self,
        row_id: str,
        side: Side,
        action: str,
        *,
        task: CanonicalTask | None = None,
        target: str | None = None,
        stray: bool = False,
    ) -> None:
        payload = {
            "task": canonical_to_json(task) if task else None,
            "target_remote_id": target,
            "stray": stray,
        }
        op_id = self.ledger.record_op(row_id, side, action, payload)
        self._plan.append(
            PlannedOp(
                op_id=op_id,
                internal_id=row_id,
                side=side,
                action=action,  # type: ignore[arg-type]
                task=task,
                target_remote_id=target,
                desired_hash=self._proj_hash(side, task) if task else None,
                stray=stray,
            )
        )

    def _sky_visible(self, task: CanonicalTask) -> bool:
        """Skylight chores are fetched in a date window; absence outside the
        window (or with no date) proves nothing and must not be read as a
        delete."""
        if task.due_date is None:
            return False
        t = self.today()
        from datetime import timedelta

        return (
            t - timedelta(days=self.policy.sky_window_past_days)
            <= task.due_date
            <= t + timedelta(days=self.policy.sky_window_future_days)
        )

    # ------------------------------------------------------------ phases ---
    def run(self) -> RunReport:
        self._present = {}
        self._plan = []
        self._report = RunReport()
        self._creates_planned = {s: 0 for s in self.clients}

        self._recover()
        snapshots = self._snapshot()
        unmatched = self._match(snapshots)
        self._adopt_new(unmatched)

        for row in self.ledger.all_rows():
            if row.deleted:
                continue
            row = self._handle_absences(row)
            if row.deleted:
                continue
            canonical = self._resolve_changes(row)
            self._propagate(row, canonical)

        self._sweep_tombstones()
        self._execute()
        self.ledger.prune_ops()
        self.ledger.meta_set("last_run_utc", datetime.now(timezone.utc).isoformat())
        return self._report

    # -- 1: crash recovery ---------------------------------------------------
    def _recover(self) -> None:
        for op in self.ledger.pending_ops(("planned", "executing")):
            if op.state == "planned":
                # Recorded but never attempted; deltas will re-derive it.
                self.ledger.mark_op(op.op_id, "failed", "dropped at recovery (never executed)")
                continue
            log.warning("recovering interrupted %s/%s op for %s", op.side, op.action, op.internal_id)
            if op.action == "create":
                self._recover_create(op)
            else:
                # Updates are absolute and deletes are existence-checked by the
                # absence/tombstone phases — safe to re-derive.
                self.ledger.mark_op(op.op_id, "failed", f"interrupted {op.action}; re-deriving")
            self._report.bump("recovered_ops")

    def _recover_create(self, op: PendingOp) -> None:
        client = self.clients.get(op.side)
        row = self.ledger.get(op.internal_id)
        task_json = op.payload.get("task")
        if client is None or row is None or not task_json:
            self.ledger.mark_op(op.op_id, "failed", "no client/row/payload for recovery")
            return
        task = canonical_from_json(task_json)
        found = client.find_by_marker(op.internal_id)
        if found is None:
            # Marker-less side (Skylight): scan for an item whose projection
            # matches what we tried to create AND that no ledger row owns.
            # Adopting an unbound exact match can never duplicate; a bound
            # match belongs to a different task that merely looks identical.
            desired_h = self._proj_hash(op.side, task)
            for rt in client.list_tasks():
                if (
                    self._proj_hash(op.side, rt.task) == desired_h
                    and self.ledger.find_by_remote(op.side, rt.remote_id) is None
                ):
                    found = rt
                    break
        if found is not None:
            updates: dict = {
                _SIDE_ID_COL[op.side]: found.remote_id,
                _SIDE_HASH_COL[op.side]: self._proj_hash(op.side, found.task),
            }
            ccol = _SIDE_CONTAINER_COL[op.side]
            if ccol and found.container_id:
                updates[ccol] = found.container_id
            self.ledger.complete_op(op.op_id, op.internal_id, updates, result_remote_id=found.remote_id)
            log.info("adopted in-flight %s create for %s as %s (no duplicate)", op.side, op.internal_id, found.remote_id)
        else:
            self.ledger.mark_op(op.op_id, "failed", "in-flight create not found remotely; will re-plan")

    # -- 2/3: snapshot + match ------------------------------------------------
    def _snapshot(self) -> dict[Side, dict[str, RemoteTask]]:
        snaps: dict[Side, dict[str, RemoteTask]] = {}
        for side, client in self.clients.items():
            items = client.list_tasks()
            snaps[side] = {rt.remote_id: rt for rt in items}
            self._report.bump(f"seen_{side}", len(items))
        return snaps

    def _match(self, snaps: dict[Side, dict[str, RemoteTask]]) -> dict[Side, list[RemoteTask]]:
        unmatched: dict[Side, list[RemoteTask]] = {s: [] for s in self.clients}
        rows = self.ledger.all_rows()
        for side in self.clients:
            snap = dict(snaps[side])
            for row in rows:
                rid = row.side_id(side)
                if rid and rid in snap:
                    self._present[(row.internal_id, side)] = snap.pop(rid)
            # marker pass: heal lost links / kill strays from crashed moves
            for rid, rt in list(snap.items()):
                if not rt.marker_internal_id:
                    continue
                row = self.ledger.get(rt.marker_internal_id)
                if row is None:
                    continue  # ledger predates this marker; treat as new below
                if (row.internal_id, side) in self._present:
                    log.warning(
                        "stray duplicate on %s (%s) for row %s — deleting (crashed move)",
                        side, rid, row.internal_id,
                    )
                    self._plan_op(row.internal_id, side, "delete", target=rid, stray=True)
                else:
                    log.info("re-adopting %s item %s for row %s via marker", side, rid, row.internal_id)
                    updates: dict = {_SIDE_ID_COL[side]: rid}
                    ccol = _SIDE_CONTAINER_COL[side]
                    if ccol and rt.container_id:
                        updates[ccol] = rt.container_id
                    self.ledger.update_row(row.internal_id, **updates)
                    self._present[(row.internal_id, side)] = rt
                snap.pop(rid)
            unmatched[side] = list(snap.values())
        return unmatched

    # -- 4: new rows -----------------------------------------------------------
    def _adopt_new(self, unmatched: dict[Side, list[RemoteTask]]) -> None:
        for side in _PRECEDENCE:
            if side not in self.clients:
                continue
            for rt in unmatched[side]:
                canonical = rt.task
                row_cols: dict = {
                    _SIDE_ID_COL[side]: rt.remote_id,
                    _SIDE_HASH_COL[side]: self._proj_hash(side, rt.task),
                }
                ccol = _SIDE_CONTAINER_COL[side]
                if ccol and rt.container_id:
                    row_cols[ccol] = rt.container_id
                # An item whose marker no longer resolves (fresh/rebuilt
                # ledger) is treated like a marker-less one for binding.
                marker_row = self.ledger.get(rt.marker_internal_id) if rt.marker_internal_id else None
                if (not rt.marker_internal_id or marker_row is None) and (
                    bound := self._content_bind(side, rt)
                ):
                    # Item that projects identically to a row not yet bound on
                    # this side: bind instead of duplicating. This is what
                    # makes a ledger rebuild non-destructive.
                    self.ledger.update_row(bound.internal_id, **row_cols)
                    self._present[(bound.internal_id, side)] = rt
                    log.info("content-bound %s item %s to row %s", side, rt.remote_id, bound.internal_id)
                    continue
                if marker_row is not None:
                    # Ledger-rebuild case: an earlier side already re-created
                    # this row this run (same marker on both sides). Bind to
                    # it instead of violating the primary key.
                    self.ledger.update_row(marker_row.internal_id, **row_cols)
                    self._present[(marker_row.internal_id, side)] = rt
                    log.info("re-bound %s item %s to rebuilt row %s", side, rt.remote_id, marker_row.internal_id)
                    continue
                row = self.ledger.insert_row(
                    rt.marker_internal_id, canonical, full_hash(canonical), **row_cols
                )
                self._present[(row.internal_id, side)] = rt
                self._report.bump(f"new_from_{side}")
                log.info("new task from %s: %r (row %s)", side, canonical.title, row.internal_id)

    def _content_bind(self, side: Side, rt: RemoteTask) -> LedgerRow | None:
        h = self._proj_hash(side, rt.task)
        for row in self.ledger.all_rows():
            if row.deleted or row.side_id(side) is not None:
                continue
            if side == "skylight" and row.sky_detached:
                continue
            if self._proj_hash(side, row.canonical()) == h:
                return row
        return None

    # -- 5: absence / deletes ----------------------------------------------------
    def _handle_absences(self, row: LedgerRow) -> LedgerRow:
        p = self.policy
        canonical = row.canonical()
        for side in self.clients:
            sid = row.side_id(side)
            if not sid or (row.internal_id, side) in self._present:
                continue
            if side == "skylight":
                if row.sky_detached:
                    continue
                if not (p.sky_absence_trusted or self._sky_visible(canonical)):
                    continue  # outside fetch window — absence proves nothing
                if p.deletes_skylight_to_todo:
                    self._tombstone(row, "skylight")
                else:
                    log.warning(
                        "chore for %r deleted on Skylight frame; detaching (policy: skylight deletes don't propagate)",
                        canonical.title,
                    )
                    self.ledger.update_row(
                        row.internal_id,
                        skylight_chore_id=None,
                        skylight_category_id=None,
                        sky_hash=None,
                        sky_detached=1,
                    )
                    self._report.bump("skylight_detached")
            elif side == "todo":
                if p.deletes_todo_to_skylight:
                    self._tombstone(row, "todo")
                else:
                    self.ledger.update_row(row.internal_id, todo_task_id=None, todo_list_id=None, todo_hash=None)
            elif side == "sp":
                if p.deletes_sharepoint_propagate:
                    self._tombstone(row, "sp")
                else:
                    self.ledger.update_row(row.internal_id, sp_item_id=None, sp_hash=None)
            row = self.ledger.get(row.internal_id) or row
        return row

    def _tombstone(self, row: LedgerRow, origin: Side) -> None:
        log.info("task %r deleted on %s — tombstoning and propagating delete", row.canonical().title, origin)
        self.ledger.update_row(
            row.internal_id,
            deleted=1,
            **{_SIDE_ID_COL[origin]: None, _SIDE_HASH_COL[origin]: None},
        )
        self._report.bump(f"deleted_from_{origin}")

    # -- 6: diff + conflict ----------------------------------------------------
    def _resolve_changes(self, row: LedgerRow) -> CanonicalTask:
        current = row.canonical()
        changed: list[tuple[Side, RemoteTask, str]] = []
        for side in self.clients:
            rt = self._present.get((row.internal_id, side))
            if rt is None:
                continue
            h_remote = self._proj_hash(side, rt.task)
            if h_remote == row.side_hash(side):
                continue
            if h_remote == self._proj_hash(side, current):
                continue  # our own previous write landing; absorbed in propagate
            changed.append((side, rt, h_remote))

        if not changed:
            return current

        winner_side, winner_rt = self._pick_winner(changed)
        new_canonical = self._merge(current, winner_side, winner_rt)
        for side, rt, _h in changed:
            if side == winner_side:
                continue
            discarded = self._merge(current, side, rt)
            msg = (
                f"CONFLICT on {row.internal_id} ({current.title!r}): {winner_side} wins, "
                f"{side} discarded -> {canonical_to_json(discarded)}"
            )
            log.warning(msg)
            self._report.conflicts.append(msg)

        if new_canonical != current:
            self.ledger.set_canonical(row.internal_id, new_canonical, full_hash(new_canonical))
            self._report.bump(f"changed_from_{winner_side}")
            log.info("row %s updated from %s: %r", row.internal_id, winner_side, new_canonical.title)
        return new_canonical

    def _pick_winner(self, changed: list[tuple[Side, RemoteTask, str]]) -> tuple[Side, RemoteTask]:
        if len(changed) == 1:
            side, rt, _ = changed[0]
            return side, rt
        if self.policy.conflict_policy == "sharepoint_wins":
            for side, rt, _ in changed:
                if side == "sp":
                    return side, rt
        # most_recent_wins: sides without timestamps (Skylight exposes none)
        # lose to sides with them; full ties resolve by precedence sp>todo>sky.
        epoch = datetime.min.replace(tzinfo=timezone.utc)

        def key(entry: tuple[Side, RemoteTask, str]):
            side, rt, _ = entry
            lm = rt.last_modified
            if lm is not None and lm.tzinfo is None:
                lm = lm.replace(tzinfo=timezone.utc)
            return (lm or epoch, -_PRECEDENCE.index(side))

        return max(changed, key=key)[:2]

    def _merge(self, current: CanonicalTask, side: Side, rt: RemoteTask) -> CanonicalTask:
        """Merge a side's report into canonical, only for fields that side can
        actually express for the current value (e.g. To Do cannot see an
        unmapped assignee, so its report must not clobber it)."""
        client = self.clients[side]
        updates = {
            f: getattr(rt.task, f)
            for f in client.PROJECTION_FIELDS
            if client.representable(f, getattr(current, f))
        }
        return current.replace(**updates)

    # -- 7: propagate ---------------------------------------------------------
    def _propagate(self, row: LedgerRow, canonical: CanonicalTask) -> None:
        row = self.ledger.get(row.internal_id) or row
        # Undated tasks that Skylight holds (or should hold) get today stamped
        # as the due date: the frame's chore chart is date-based, and the
        # Skylight PUT cannot clear a start date — leaving canonical undated
        # would replan the same update forever. This intentionally propagates
        # to all sides — documented policy, not an accident.
        sky = self.clients.get("skylight")
        if (
            self.policy.undated_due_today
            and canonical.due_date is None
            and sky is not None
            and not row.sky_detached
            and sky.supports(canonical)
            # only when Skylight will actually hold it: open tasks, or a
            # chore that is already bound (whose start can't be cleared)
            and (canonical.status == "open" or row.side_id("skylight") is not None)
        ):
            canonical = canonical.replace(due_date=self.today())
            self.ledger.set_canonical(row.internal_id, canonical, full_hash(canonical))
            log.info("stamped due=%s on undated task %r for Skylight", canonical.due_date, canonical.title)

        for side in _PRECEDENCE:
            client = self.clients.get(side)
            if client is None:
                continue
            if side == "skylight" and row.sky_detached:
                continue
            sid = row.side_id(side)
            rt = self._present.get((row.internal_id, side))
            if not client.supports(canonical):
                if sid and rt is not None:
                    log.warning(
                        "%s can no longer hold %r (assignee %r unmappable); removing there",
                        side, canonical.title, canonical.assignee,
                    )
                    self._plan_op(row.internal_id, side, "delete", target=sid)
                continue
            desired_h = self._proj_hash(side, canonical)
            if rt is not None:
                h_remote = self._proj_hash(side, rt.task)
                if h_remote == desired_h:
                    if row.side_hash(side) != desired_h:
                        self.ledger.update_row(row.internal_id, **{_SIDE_HASH_COL[side]: desired_h})
                    continue
                self._plan_op(row.internal_id, side, "update", task=canonical, target=rt.remote_id)
            elif sid is None:
                if canonical.status == "completed" and not self.policy.backfill_completed:
                    # done is done — never back-fill finished tasks onto a
                    # side that doesn't have them (no op, no journal entry;
                    # re-evaluated and skipped again every run, zero writes)
                    self._report.bump(f"skipped_completed_create_{side}")
                    continue
                if self._creates_planned[side] >= self.policy.max_creates_per_side:
                    self._report.bump(f"deferred_creates_{side}")
                    continue  # surplus re-derives next run (backlog drains)
                self._creates_planned[side] += 1
                self._plan_op(row.internal_id, side, "create", task=canonical)
            # else: id known but absent -> handled by absence phase already

    # -- tombstone sweep --------------------------------------------------------
    def _sweep_tombstones(self) -> None:
        for row in self.ledger.all_rows():
            if not row.deleted:
                continue
            for side in self.clients:
                sid = row.side_id(side)
                if not sid:
                    continue
                if (row.internal_id, side) in self._present:
                    self._plan_op(row.internal_id, side, "delete", target=sid)
                else:
                    # already gone remotely; just release the id
                    self.ledger.update_row(
                        row.internal_id, **{_SIDE_ID_COL[side]: None, _SIDE_HASH_COL[side]: None}
                    )

    # -- 8: execute -------------------------------------------------------------
    def _execute(self) -> None:
        # Deletes last, master (sp) first within creates/updates, master last
        # within deletes — partial failure leaves the system of record intact.
        def order(op: PlannedOp):
            if op.action == "delete":
                return (1, len(_PRECEDENCE) - _PRECEDENCE.index(op.side))
            return (0, _PRECEDENCE.index(op.side))

        for op in sorted(self._plan, key=order):
            self.ledger.mark_op(op.op_id, "executing")
            client = self.clients[op.side]
            try:
                if op.action == "create":
                    assert op.task is not None
                    result = client.create_task(op.task, op.internal_id)
                    updates = self._write_updates(op, result)
                    self.ledger.complete_op(op.op_id, op.internal_id, updates, result.remote_id)
                elif op.action == "update":
                    assert op.task is not None and op.target_remote_id is not None
                    result = client.update_task(op.target_remote_id, op.task, op.internal_id)
                    updates = self._write_updates(op, result)
                    self.ledger.complete_op(op.op_id, op.internal_id, updates, result.remote_id)
                else:  # delete
                    assert op.target_remote_id is not None
                    client.delete_task(op.target_remote_id)
                    updates = (
                        {}
                        if op.stray
                        else {_SIDE_ID_COL[op.side]: None, _SIDE_HASH_COL[op.side]: None}
                    )
                    self.ledger.complete_op(op.op_id, op.internal_id, updates)
                self._report.bump(f"{op.action}_{op.side}")
            except PermanentApiError as exc:
                # Isolated upstream rejection: record, keep going.
                msg = f"{op.action} on {op.side} for {op.internal_id} failed permanently: {exc}"
                log.error(msg)
                self._report.failures.append(msg)
                self.ledger.mark_op(op.op_id, "failed", str(exc))
            except (TransientApiError, AuthError, SchemaDriftError) as exc:
                # Systemic: stop cleanly. Journal + ledger are consistent;
                # the next scheduled run recovers and re-derives.
                self.ledger.mark_op(op.op_id, "failed", str(exc))
                log.error("aborting run on %s: %s", type(exc).__name__, exc)
                raise

    def _write_updates(self, op: PlannedOp, result: RemoteTask) -> dict:
        updates: dict = {
            _SIDE_ID_COL[op.side]: result.remote_id,
            _SIDE_HASH_COL[op.side]: op.desired_hash,
        }
        ccol = _SIDE_CONTAINER_COL[op.side]
        if ccol and result.container_id:
            updates[ccol] = result.container_id
        return updates
