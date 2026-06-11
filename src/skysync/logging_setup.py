"""Logging: console + rotating file, with a redaction filter as a backstop so
no Authorization header, JWT, or password-looking string reaches the logs even
if a bug tries to log one."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

# JWTs, Authorization headers, and long base64-ish runs.
_REDACTIONS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+(\s+\S+)?)"),
    re.compile(r"(?i)((?:password|client_secret|refresh_token|access_token|token)\"?\s*[:=]\s*\"?)([^\s\"',}]+)"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]+\b"),  # JWT
    re.compile(r"\b[A-Za-z0-9+/_\-]{48,}={0,2}\b"),  # long base64-ish blobs
]


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = msg
        for rx in _REDACTIONS:
            red = rx.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", red)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


def setup_logging(log_dir: str | Path, level: str = "INFO") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level.upper())

    fh = logging.handlers.RotatingFileHandler(
        Path(log_dir) / "skysync.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.addFilter(RedactionFilter())

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.addFilter(RedactionFilter())

    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(ch)
    # third-party noise
    logging.getLogger("msal").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
