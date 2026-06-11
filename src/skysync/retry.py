"""Retry with exponential backoff + full jitter for all outbound HTTP.

Every client call goes through ``retry_call``. 429/5xx and network errors are
retried (honoring Retry-After); other 4xx raise ``PermanentApiError``
immediately. After the budget is exhausted a ``TransientApiError`` is raised
so the run aborts cleanly and the next scheduled run picks up.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import requests

from .errors import PermanentApiError, TransientApiError

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _retry_after_seconds(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; fall back to computed backoff


def retry_call(
    fn: Callable[[], requests.Response],
    *,
    what: str,
    retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
) -> requests.Response:
    """Call ``fn`` (which performs one HTTP request) with retry.

    Returns the response on any 2xx. Raises PermanentApiError on
    non-retryable 4xx, TransientApiError once retries are exhausted.
    """
    last_status: int | None = None
    last_err: str = ""
    for attempt in range(retries + 1):
        try:
            resp = fn()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_status, last_err = None, f"{type(exc).__name__}: {exc}"
            log.warning("%s: network error (attempt %d/%d): %s", what, attempt + 1, retries + 1, last_err)
        else:
            if resp.status_code < 300:
                return resp
            last_status = resp.status_code
            last_err = resp.text[:500]
            if resp.status_code not in RETRYABLE_STATUSES:
                raise PermanentApiError(
                    f"{what}: HTTP {resp.status_code}: {last_err}", status=resp.status_code
                )
            log.warning("%s: HTTP %d (attempt %d/%d)", what, resp.status_code, attempt + 1, retries + 1)
            ra = _retry_after_seconds(resp)
            if ra is not None and attempt < retries:
                sleep(min(ra, max_delay))
                continue
        if attempt < retries:
            # Exponential backoff with full jitter.
            delay = random.uniform(0, min(max_delay, base_delay * (2**attempt)))
            sleep(delay)
    raise TransientApiError(f"{what}: giving up after {retries + 1} attempts: {last_err}", status=last_status)
