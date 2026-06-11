"""CLI for dumping Graph To Do / SharePoint tasks.

Usage:
    python -m skysync.graph.cli todo-dump [--config config.toml]
    python -m skysync.graph.cli sp-dump   [--config config.toml]

Output: a JSON array of task descriptors — no secrets included.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..config import load_config
from ..secrets import SecretStore
from .auth import AppOnlyGraphAuth, DelegatedGraphAuth, GraphSession
from .sharepoint_client import SharePointTaskClient
from .todo_client import TodoTaskClient


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _remote_task_to_dict(rt: Any) -> dict:
    t = rt.task
    return {
        "remote_id": rt.remote_id,
        "marker_internal_id": rt.marker_internal_id,
        "last_modified": rt.last_modified.isoformat() if rt.last_modified else None,
        "container_id": rt.container_id,
        "task": {
            "title": t.title,
            "notes": t.notes,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "assignee": t.assignee,
            "status": t.status,
        },
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skysync.graph.cli",
        description="Dump Microsoft Graph To Do / SharePoint tasks as JSON",
    )
    parser.add_argument("command", choices=["todo-dump", "sp-dump"])
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    ns = parser.parse_args(argv)

    cfg = load_config(Path(ns.config))
    store = SecretStore(cfg.resolve("secrets"))

    sp_delegated = cfg.sharepoint is not None and cfg.graph.sharepoint_auth == "delegated"

    if ns.command == "todo-dump":
        auth = DelegatedGraphAuth(cfg.graph, store, include_sharepoint_scope=sp_delegated)
        session = GraphSession(auth.get_token)

        child_lists = {
            key.lower(): child.todo_list
            for key, child in cfg.mapping.children.items()
        }
        client = TodoTaskClient(session, child_lists, cfg.todo.default_list)
        tasks = client.list_tasks()

    else:  # sp-dump
        if cfg.sharepoint is None:
            print(
                "sp-dump: no [sharepoint] section in config.toml — this deployment "
                "runs two-way To Do <-> Skylight (the ledger is the system of record).",
                file=sys.stderr,
            )
            return 2
        if cfg.graph.sharepoint_auth == "app_only":
            sp_auth = AppOnlyGraphAuth(cfg.graph, store)
        else:
            sp_auth = DelegatedGraphAuth(cfg.graph, store, include_sharepoint_scope=True)
        session = GraphSession(sp_auth.get_token)

        sp_assignees = {
            key.lower(): child.sp_assignee or key.lower()
            for key, child in cfg.mapping.children.items()
        }
        client = SharePointTaskClient(
            session,
            cfg.sharepoint.site_id,
            cfg.sharepoint.list_id,
            sp_assignees,
        )
        tasks = client.list_tasks()

    output = [_remote_task_to_dict(rt) for rt in tasks]
    print(json.dumps(output, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
