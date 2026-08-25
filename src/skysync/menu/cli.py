"""Sync school lunch menus to Skylight.

    python -m skysync.menu.cli --config config.toml --dry-run
    python -m skysync.menu.cli --config config.toml            (live)
    python -m skysync.menu.cli --config config.toml --show     (print, no writes)
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys

from ..config import load_config
from ..errors import ConfigError, SkySyncError
from ..logging_setup import setup_logging
from ..secrets import SecretStore
from ..skylight.client import SkylightApi
from .fdmealplanner import FDMealPlannerClient, mark_daily_specials
from .sync import MenuRunReport, MenuSync, months_to_sync

log = logging.getLogger("skysync.menu")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skysync.menu", description=__doc__)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--dry-run", action="store_true", help="plan writes, execute none")
    ap.add_argument("--show", action="store_true", help="just print the fetched menu")
    ap.add_argument("--months-ahead", type=int, default=None)
    ns = ap.parse_args(argv)

    # The Windows console defaults to cp1252 and cannot print the menu emoji;
    # printing must never break a run (API payloads are UTF-8 regardless).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable stream
            pass

    try:
        cfg = load_config(ns.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.resolve(cfg.general.log_dir), cfg.general.log_level)

    if not cfg.menu.children:
        print("no [menu.children] configured — nothing to do", file=sys.stderr)
        return 2

    months = months_to_sync(
        datetime.date.today(),
        ns.months_ahead if ns.months_ahead is not None else cfg.menu.months_ahead,
    )
    client = FDMealPlannerClient(account_id=cfg.menu.account_id)

    if ns.show:
        for child, mc in cfg.menu.children.items():
            days = []
            for y, m in months:
                days.extend(client.fetch_month(mc.location_id, y, m))
            mark_daily_specials(days)
            print(f"\n=== {child} (loc {mc.location_id}) — {len(days)} school days ===")
            for d in days:
                print(f"  {d.date}  {cfg.menu.title_prefix}{d.headline()}")
        return 0

    store = SecretStore(cfg.resolve("secrets"))
    api = SkylightApi(cfg.skylight.frame_id, store, extra_headers=cfg.skylight.headers)
    syncer = MenuSync(
        api,
        cfg.skylight.frame_id,
        timezone=cfg.menu.timezone,
        title_prefix=cfg.menu.title_prefix,
        dry_run=ns.dry_run,
    )
    report = MenuRunReport()
    try:
        for child, mc in cfg.menu.children.items():
            syncer.sync_child(
                child, mc.location_id, mc.skylight_category, months, report, client=client
            )
    except SkySyncError as exc:
        log.error("menu sync failed (%s): %s", type(exc).__name__, exc)
        return 1

    if ns.dry_run:
        for line in report.planned:
            print("  [DRY-RUN]", line)
    print(json.dumps(report.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
