"""Fake GraphSession for unit tests.

FakeGraphSession(routes) where routes is:
    {(method, url_without_query): payload_or_list_of_payloads}

  - method is "GET", "POST", "PATCH", "DELETE"
  - url_without_query is the full URL minus any query-string
  - payload is the dict that would be returned by the real method
    (for DELETE, ignored / can be None)
  - When a list is provided, payloads are consumed in order (first call
    gets routes[key][0], second gets routes[key][1], etc.)

.calls records every call as:
    {"method": str, "url": str, "params": dict|None, "json": dict|None}

iter_items follows "@odata.nextLink" pagination within the fake response
chain (each page must be a separate payload in the list for that key, or
the nextLink key can point to a new route key).

Raises AssertionError on an unmatched (method, url) pair.
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlparse


class FakeGraphSession:
    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        # Normalise: wrap single payloads in a list; track consumption index
        self._routes: dict[tuple[str, str], list[Any]] = {}
        self._indices: dict[tuple[str, str], int] = {}
        for key, payload in routes.items():
            if isinstance(payload, list) and not isinstance(payload, dict):
                # List of payloads to consume in order (but a plain list that
                # IS also a dict would be weird; dicts are never lists in Python)
                self._routes[key] = payload
            else:
                self._routes[key] = [payload]
            self._indices[key] = 0
        self.calls: list[dict[str, Any]] = []

    def _key_for(self, method: str, url: str) -> tuple[str, str]:
        """Strip query string from URL to form the lookup key."""
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else parsed.path
        return (method.upper(), base)

    def _next_payload(self, method: str, url: str) -> Any:
        # Exact match (including query string) wins — lets tests route
        # nextLink pages like ".../tasks?$skiptoken=abc" distinctly.
        exact = (method.upper(), url)
        key = exact if exact in self._routes else self._key_for(method, url)
        if key not in self._routes:
            raise AssertionError(
                f"FakeGraphSession: no route for ({method!r}, {url!r}). "
                f"Known routes: {sorted(self._routes.keys())}"
            )
        idx = self._indices[key]
        payloads = self._routes[key]
        if idx >= len(payloads):
            # Repeat last payload (useful for idempotent GETs)
            idx = len(payloads) - 1
        payload = payloads[idx]
        self._indices[key] = min(idx + 1, len(payloads) - 1)
        return payload

    def _record(self, method: str, url: str, params: Any = None, json: Any = None) -> None:
        self.calls.append({"method": method.upper(), "url": url, "params": params, "json": json})

    # -- GraphSession API surface ----------------------------------------------

    def get_json(self, url: str, *, what: str, params: dict | None = None, **kw: Any) -> dict:
        self._record("GET", url, params=params)
        return self._next_payload("GET", url)

    def post_json(self, url: str, body: dict, *, what: str) -> dict:
        self._record("POST", url, json=body)
        return self._next_payload("POST", url)

    def patch_json(self, url: str, body: dict, *, what: str) -> dict:
        self._record("PATCH", url, json=body)
        return self._next_payload("PATCH", url)

    def delete(self, url: str, *, what: str) -> None:
        self._record("DELETE", url)
        self._next_payload("DELETE", url)

    def iter_items(
        self,
        url: str,
        *,
        what: str,
        params: dict | None = None,
    ) -> Iterator[dict]:
        """Yield items from paged responses, following @odata.nextLink."""
        current_url = url
        current_params = params
        for _page_no in range(50):
            self._record("GET", current_url, params=current_params)
            page = self._next_payload("GET", current_url)
            if "value" not in page:
                summary = str(page)[:300]
                from skysync.errors import SchemaDriftError
                raise SchemaDriftError("iter_items response missing 'value'", summary)
            yield from page["value"]
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return
            current_url = nxt
            current_params = None  # nextLink already has params baked in
        raise AssertionError(
            "FakeGraphSession.iter_items exceeded 50 pages — a nextLink chain "
            "is looping (this once ate all system RAM; never remove this cap)"
        )
