"""Typed errors. The engine distinguishes transient (retryable, run aborts
cleanly) from permanent (configuration/contract) failures, and schema drift
(the unofficial Skylight API changed shape) which must always fail loud."""

from __future__ import annotations


class SkySyncError(Exception):
    """Base for all project errors."""


class ConfigError(SkySyncError):
    """Bad or missing configuration / secret."""


class AuthError(SkySyncError):
    """Authentication failed (expired/revoked token, bad credentials).

    The run must abort cleanly without partial ledger corruption; the next
    scheduled run retries, and the heartbeat goes stale so monitoring fires.
    """


class TransientApiError(SkySyncError):
    """Retryable upstream failure (429/5xx/network). Raised only after the
    retry budget is exhausted."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PermanentApiError(SkySyncError):
    """Non-retryable upstream failure (4xx other than 429)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SchemaDriftError(SkySyncError):
    """A response did not match the expected (validated) shape.

    The Skylight API is unofficial and WILL drift; we fail loud with the
    offending payload summarized, never coerce silently.
    """

    def __init__(self, message: str, payload_summary: str | None = None):
        super().__init__(message if payload_summary is None else f"{message}: {payload_summary}")
        self.payload_summary = payload_summary


class LedgerCorruptionError(SkySyncError):
    """The sync ledger is in a state that must not be auto-repaired."""
