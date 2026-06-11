"""Heartbeat / dead-man's-switch.

Every successful run rewrites ``heartbeat.json`` with a timestamp and run
summary. Monitoring options (both optional, both work offline-first):
  * check-heartbeat.ps1 alerts if the file is older than
    heartbeat.max_age_minutes (run it from Task Scheduler too, or eyeball it);
  * heartbeat.ping_url, if set, is GET-pinged on success — point it at a
    healthchecks.io-style monitor and you get e-mail when runs stop.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)


def write_heartbeat(path: str | Path, summary: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **summary}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(p)  # atomic on same volume


def ping(url: str) -> None:
    if not url:
        return
    try:
        requests.get(url, timeout=10)
    except requests.RequestException as exc:
        # Monitoring failure must never fail the sync itself.
        log.warning("heartbeat ping failed: %s", exc)
