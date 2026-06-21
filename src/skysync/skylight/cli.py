"""CLI entry point for the Skylight client.

Usage:
    python -m skysync.skylight.cli dump [--config config.toml] \\
        [--days-past N] [--days-future N]

Prints a JSON document with categories, chores, and lists.
No secrets are included in the output.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from typing import Any


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m skysync.skylight.cli",
        description="Dump Skylight frame data as JSON (no secrets in output).",
    )
    sub = ap.add_subparsers(dest="command", required=True)
    dump_p = sub.add_parser("dump", help="Dump categories, chores, and lists.")
    dump_p.add_argument("--config", default="config.toml", help="Path to config.toml")
    dump_p.add_argument("--days-past", type=int, default=14, help="Days before today to include")
    dump_p.add_argument("--days-future", type=int, default=60, help="Days after today to include")
    ns = ap.parse_args(argv)

    if ns.command == "dump":
        return _cmd_dump(ns)
    ap.print_help()
    return 1


def _cmd_dump(ns: argparse.Namespace) -> int:
    # Local imports to keep startup fast and avoid import-time side effects.
    from skysync.config import load_config
    from skysync.secrets import SecretStore
    from skysync.skylight.client import SkylightApi

    cfg = load_config(ns.config)
    secrets = SecretStore(cfg.resolve("secrets"))
    api = SkylightApi(frame_id=cfg.skylight.frame_id, secrets=secrets, extra_headers=cfg.skylight.headers)

    today = datetime.date.today()
    after = today - datetime.timedelta(days=ns.days_past)
    before = today + datetime.timedelta(days=ns.days_future)

    # Categories
    categories_raw: list[dict[str, Any]] = []
    for cat in api.get_categories():
        categories_raw.append(
            {
                "id": cat.id,
                "label": cat.attributes.label,
                "color": cat.attributes.color,
            }
        )

    # Chores
    chores_raw: list[dict[str, Any]] = []
    envelope = api.get_chores(after, before)
    for chore in envelope.data:
        a = chore.attributes
        chores_raw.append(
            {
                "id": chore.id,
                "summary": a.summary,
                "status": a.status,
                "start": a.start.isoformat() if a.start else None,
                "category_id": chore.category_id,
                "recurring": a.recurring,
                "routine": a.routine,
            }
        )

    # Lists + items — re-fetch raw to get the included list_items.
    from skysync.skylight.models_generated import ListsResponse

    resp = api._request("GET", f"/frames/{api._frame_id}/lists")
    full_envelope = SkylightApi._parse(resp, ListsResponse, "GET /lists (dump)")

    # Build a list_id -> items map using model_extra for relationships (extra="allow").
    items_by_list: dict[str, list[dict[str, Any]]] = {}
    for item in full_envelope.included:
        ia = item.attributes
        list_id_ref: str | None = None
        # relationships is stored in model_extra when extra="allow".
        extra = item.model_extra or {}
        rel = extra.get("relationships") or {}
        if isinstance(rel, dict):
            lst_rel = rel.get("list") or {}
            if isinstance(lst_rel, dict):
                d = lst_rel.get("data") or {}
                if isinstance(d, dict):
                    list_id_ref = str(d.get("id", "")) or None
        items_by_list.setdefault(list_id_ref or "", []).append(
            {
                "id": item.id,
                "label": ia.label,
                "status": ia.status,
                "position": ia.position,
                "section": ia.section,
            }
        )

    lists_raw: list[dict[str, Any]] = []
    for sl in full_envelope.data:
        lists_raw.append(
            {
                "id": sl.id,
                "label": sl.attributes.label,
                "items": items_by_list.get(sl.id, []),
            }
        )

    doc: dict[str, Any] = {
        "categories": categories_raw,
        "chores": chores_raw,
        "lists": lists_raw,
    }
    print(json.dumps(doc, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
