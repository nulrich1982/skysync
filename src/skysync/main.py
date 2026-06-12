"""SkySync entrypoint.

    python -m skysync.main login              one-time interactive Graph auth
    python -m skysync.main run --live         the scheduled-task command
    python -m skysync.main run --dry-run      real reads, planned writes only
    python -m skysync.main run --mock [PATH]  fixture-backed, no network
    python -m skysync.main status             heartbeat + ledger overview

Exit codes: 0 success, 1 run failed (transient/auth/drift — next scheduled run
retries), 2 configuration problem.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import heartbeat
from .config import AppConfig, load_config
from .engine import RunReport, SyncEngine, SyncPolicy
from .errors import AuthError, ConfigError, SchemaDriftError, SkySyncError
from .ledger import Ledger
from .logging_setup import setup_logging
from .models import Side, TaskClient

log = logging.getLogger("skysync")

DEFAULT_FIXTURE = "fixtures/family.json"


# --------------------------------------------------------------- wiring ----


def build_policy(cfg: AppConfig) -> SyncPolicy:
    return SyncPolicy(
        conflict_policy=cfg.sync.conflict_policy,
        deletes_todo_to_skylight=cfg.sync.deletes_todo_to_skylight,
        deletes_skylight_to_todo=cfg.sync.deletes_skylight_to_todo,
        deletes_sharepoint_propagate=cfg.sync.deletes_sharepoint_propagate,
        sky_window_past_days=cfg.skylight.chore_window_days_past,
        sky_window_future_days=cfg.skylight.chore_window_days_future,
        backfill_completed=cfg.sync.backfill_completed,
        max_creates_per_side=cfg.sync.max_creates_per_run,
    )


def grocery_policy(cfg: AppConfig) -> SyncPolicy:
    return SyncPolicy(
        conflict_policy=cfg.sync.conflict_policy,
        deletes_todo_to_skylight=cfg.grocery.mirror_deletes,
        deletes_skylight_to_todo=cfg.grocery.mirror_deletes,
        undated_due_today=False,  # groceries are dateless; never stamp dates
        sky_absence_trusted=True,  # list items are fetched unwindowed
        backfill_completed=cfg.sync.backfill_completed,
        max_creates_per_side=cfg.sync.max_creates_per_run,
    )


def build_live_clients(cfg: AppConfig) -> dict[Side, TaskClient]:
    # Imported lazily so --mock works without msal/requests reachability.
    from .graph.auth import AppOnlyGraphAuth, DelegatedGraphAuth, GraphSession
    from .graph.sharepoint_client import SharePointTaskClient
    from .graph.todo_client import TodoTaskClient
    from .secrets import SecretStore
    from .skylight.adapter import SkylightTaskClient
    from .skylight.client import SkylightApi

    store = SecretStore(cfg.resolve("secrets"))
    delegated = DelegatedGraphAuth(
        cfg.graph,
        store,
        include_sharepoint_scope=sharepoint_delegated(cfg),
    )
    todo_session = GraphSession(delegated.get_token)

    children = {k.lower(): v for k, v in cfg.mapping.children.items()}
    todo = TodoTaskClient(
        todo_session,
        child_lists={k: v.todo_list for k, v in children.items()},
        default_list=cfg.todo.default_list,
    )
    sp = None
    if cfg.sharepoint is not None:
        if cfg.graph.sharepoint_auth == "app_only":
            sp_session = GraphSession(AppOnlyGraphAuth(cfg.graph, store).get_token)
        else:
            sp_session = todo_session
        sp = SharePointTaskClient(
            sp_session,
            site_id=cfg.sharepoint.site_id,
            list_id=cfg.sharepoint.list_id,
            sp_assignees={k: v.sp_assignee or k for k, v in children.items()},
        )
    sky_api = SkylightApi(cfg.skylight.frame_id, store)
    sky = SkylightTaskClient(
        sky_api,
        child_categories={k: v.skylight_category for k, v in children.items()},
        window_past_days=cfg.skylight.chore_window_days_past,
        window_future_days=cfg.skylight.chore_window_days_future,
        sync_recurring=cfg.skylight.sync_recurring,
    )
    clients: dict[Side, TaskClient] = {"todo": todo, "skylight": sky}
    if sp is not None:
        clients["sp"] = sp
    else:
        log.info("no [sharepoint] configured: two-way To Do <-> Skylight mode (ledger is the system of record)")
    return clients


def build_grocery_clients(cfg: AppConfig) -> dict[Side, TaskClient]:
    """Second pairing: one Skylight LIST mirrored to one To Do list. Reuses
    the same auth (separate client instances, separate ledger)."""
    from .graph.auth import DelegatedGraphAuth, GraphSession
    from .graph.todo_client import TodoTaskClient
    from .secrets import SecretStore
    from .skylight.client import SkylightApi
    from .skylight.list_adapter import SkylightListTaskClient

    store = SecretStore(cfg.resolve("secrets"))
    session = GraphSession(
        DelegatedGraphAuth(cfg.graph, store, include_sharepoint_scope=sharepoint_delegated(cfg)).get_token
    )
    todo = TodoTaskClient(session, child_lists={}, default_list=cfg.grocery.todo_list)
    sky = SkylightListTaskClient(SkylightApi(cfg.skylight.frame_id, store), cfg.grocery.skylight_list)
    return {"todo": todo, "skylight": sky}


def sharepoint_delegated(cfg: AppConfig) -> bool:
    return cfg.sharepoint is not None and cfg.graph.sharepoint_auth == "delegated"


# -------------------------------------------------------------- run lock ----


class RunLock:
    """File-based single-instance guard (Task Scheduler overlap protection)."""

    def __init__(self, state_dir: Path, stale_minutes: int = 60):
        self.path = state_dir / "skysync.lock"
        self.stale_minutes = stale_minutes
        self._acquired = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    json.dump({"pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat()}, f)
                self._acquired = True
                return self
            except FileExistsError:
                age_min = (time.time() - self.path.stat().st_mtime) / 60
                if attempt == 1 and age_min > self.stale_minutes:
                    log.warning("removing stale run lock (%.0f min old)", age_min)
                    self.path.unlink(missing_ok=True)
                    continue
                raise SkySyncError(
                    f"another run appears active ({self.path}, {age_min:.0f} min old); exiting"
                )
        raise AssertionError("unreachable")

    def __exit__(self, *exc) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)


# ------------------------------------------------------------- commands ----


def cmd_login(cfg: AppConfig) -> int:
    from .graph.auth import DelegatedGraphAuth
    from .secrets import SecretStore

    auth = DelegatedGraphAuth(
        cfg.graph,
        SecretStore(cfg.resolve("secrets")),
        include_sharepoint_scope=sharepoint_delegated(cfg),
    )
    user = auth.login_device_flow()
    print(f"Logged in as {user}. The refresh token is cached (DPAPI) and will rotate on every run.")
    return 0


def cmd_run(cfg: AppConfig, mode: str, fixture: str | None) -> int:
    state_dir = cfg.resolve(cfg.general.state_dir)
    started = time.monotonic()

    grocery_clients: dict[Side, TaskClient] | None = None
    grocery_ledger: Ledger | None = None

    if mode == "mock":
        from .mock_client import load_fixture_clients

        fixture_path = cfg.resolve(fixture or DEFAULT_FIXTURE)
        clients: dict[Side, TaskClient] = load_fixture_clients(fixture_path)  # type: ignore[assignment]
        ledger = Ledger(state_dir / "ledger-mock.sqlite3")
        log.info("MOCK run from fixture %s (ledger-mock.sqlite3; grocery pairing skipped)", fixture_path)
    elif mode == "dry-run":
        from .dryrun import DryRunClient

        clients = {side: DryRunClient(c) for side, c in build_live_clients(cfg).items()}  # type: ignore[misc]
        tmpdir = Path(tempfile.mkdtemp(prefix="skysync-dryrun-"))
        real = state_dir / "ledger.sqlite3"
        if real.exists():
            shutil.copy2(real, tmpdir / "ledger.sqlite3")
        ledger = Ledger(tmpdir / "ledger.sqlite3")
        if cfg.grocery.enabled:
            grocery_clients = {side: DryRunClient(c) for side, c in build_grocery_clients(cfg).items()}  # type: ignore[misc]
            real_g = state_dir / "ledger-grocery.sqlite3"
            if real_g.exists():
                shutil.copy2(real_g, tmpdir / "ledger-grocery.sqlite3")
            grocery_ledger = Ledger(tmpdir / "ledger-grocery.sqlite3")
        log.info("DRY-RUN against throwaway ledger copies (%s)", tmpdir)
    else:  # live
        clients = build_live_clients(cfg)
        ledger = Ledger(state_dir / "ledger.sqlite3")
        if cfg.grocery.enabled:
            grocery_clients = build_grocery_clients(cfg)
            grocery_ledger = Ledger(state_dir / "ledger-grocery.sqlite3")

    grocery_report: RunReport | None = None
    try:
        with RunLock(state_dir):
            engine = SyncEngine(ledger, clients, build_policy(cfg))
            report: RunReport = engine.run()
            if grocery_clients is not None and grocery_ledger is not None:
                grocery_report = SyncEngine(grocery_ledger, grocery_clients, grocery_policy(cfg)).run()
    finally:
        ledger.close()
        if grocery_ledger is not None:
            grocery_ledger.close()

    duration = round(time.monotonic() - started, 1)
    summary = {"mode": mode, "duration_s": duration, **report.summary()}
    if grocery_report is not None:
        summary["grocery"] = grocery_report.summary()
    log.info("run complete: %s", json.dumps(summary, default=str))

    if mode == "live":
        heartbeat.write_heartbeat(cfg.resolve(cfg.heartbeat.file), summary)
        heartbeat.ping(cfg.heartbeat.ping_url)
    if mode == "dry-run":
        all_clients = list(clients.values()) + list((grocery_clients or {}).values())
        planned = [p for c in all_clients for p in getattr(c, "planned", [])]
        print(json.dumps({"planned_writes": planned, **summary}, indent=2, default=str))
    if mode == "mock":
        print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_status(cfg: AppConfig) -> int:
    hb_path = cfg.resolve(cfg.heartbeat.file)
    out: dict = {"heartbeat": None, "ledger": None}
    if hb_path.exists():
        out["heartbeat"] = json.loads(hb_path.read_text(encoding="utf-8"))
    ledger_path = cfg.resolve(cfg.general.state_dir) / "ledger.sqlite3"
    if ledger_path.exists():
        led = Ledger(ledger_path)
        rows = led.all_rows()
        out["ledger"] = {
            "active": sum(1 for r in rows if not r.deleted),
            "tombstoned": sum(1 for r in rows if r.deleted),
            "detached_from_skylight": sum(1 for r in rows if r.sky_detached),
            "last_run_utc": led.meta_get("last_run_utc"),
            "unresolved_ops": len(led.pending_ops(("planned", "executing"))),
        }
        led.close()
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skysync", description=__doc__)
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("login")
    sub.add_parser("status")
    runp = sub.add_parser("run")
    mode = runp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_const", dest="mode", const="live")
    mode.add_argument("--dry-run", action="store_const", dest="mode", const="dry-run")
    mode.add_argument("--mock", nargs="?", const=DEFAULT_FIXTURE, default=None, metavar="FIXTURE")
    ns = ap.parse_args(argv)

    try:
        cfg = load_config(ns.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.resolve(cfg.general.log_dir), cfg.general.log_level)

    try:
        if ns.command == "login":
            return cmd_login(cfg)
        if ns.command == "status":
            return cmd_status(cfg)
        mode_val = ns.mode or ("mock" if ns.mock else None)
        fixture = ns.mock if isinstance(ns.mock, str) else None
        return cmd_run(cfg, mode_val or "mock", fixture)
    except ConfigError as exc:
        log.error("configuration problem: %s", exc)
        return 2
    except (AuthError, SchemaDriftError, SkySyncError) as exc:
        log.error("run failed (%s): %s", type(exc).__name__, exc)
        return 1
    except Exception:
        log.exception("unexpected failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
