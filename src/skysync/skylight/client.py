"""Typed HTTP client for the unofficial Skylight Calendar API.

The Skylight API is REVERSE-ENGINEERED and WILL drift; every response is
validated against pydantic models and failures raise SchemaDriftError
immediately (fail loud, never coerce silently).

Auth scheme: observed in the wild as BOTH ``Authorization: Bearer <token>``
(browser-captured tokens) and ``Authorization: Basic <token>`` (the opaque
token returned by POST /api/sessions, sent verbatim — NOT base64 credentials).
A captured token that includes its scheme prefix is sent exactly as captured;
a bare token defaults to Basic.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import requests
from pydantic import ValidationError

from skysync.errors import AuthError, PermanentApiError, SchemaDriftError
from skysync.retry import retry_call
from skysync.secrets import SecretStore

from .models_generated import (
    CategoriesResponse,
    Category,
    Chore,
    ChoreResponse,
    ChoresResponse,
    ListItem,
    ListItemResponse,
    ListsResponse,
    SessionResponse,
    SkylightList,
)

log = logging.getLogger(__name__)


class SkylightApi:
    """Client for the unofficial Skylight Calendar REST API.

    Every public method validates the response with the appropriate pydantic
    envelope model and raises ``SchemaDriftError`` on any mismatch.
    """

    def __init__(
        self,
        frame_id: str,
        secrets: SecretStore,
        base_url: str = "https://app.ourskylight.com/api",
        timeout: float = 30.0,
    ) -> None:
        self._frame_id = frame_id
        self._secrets = secrets
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        # Cached full Authorization header value — resolved lazily.
        self._auth_header: str | None = None
        # Whether we authenticated via email+password (vs. pre-captured token).
        self._password_mode: bool = False

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _resolve_auth_header(self) -> str:
        """Return (and cache) the full Authorization header value, performing
        login if needed."""
        if self._auth_header is not None:
            return self._auth_header

        raw = self._secrets.get_optional("skylight_token")
        if raw is not None:
            raw = raw.strip()
            # A captured header value that already names its scheme ("Bearer
            # xyz" / "Basic xyz") is sent exactly as captured — both schemes
            # have been observed in the wild. A bare token defaults to Basic
            # (the scheme used for session-login tokens).
            if raw.lower().startswith(("basic ", "bearer ")):
                self._auth_header = raw
            else:
                self._auth_header = f"Basic {raw}"
            self._password_mode = False
            return self._auth_header

        # Fall back to email + password login.
        self._password_mode = True
        self._auth_header = f"Basic {self._login()}"
        return self._auth_header

    def _login(self) -> str:
        """POST /sessions and return the token. Raises AuthError on failure."""
        email = self._secrets.get("skylight_email")
        password = self._secrets.get("skylight_password")
        url = f"{self._base_url}/sessions"
        try:
            resp = retry_call(
                lambda: self._session.post(
                    url,
                    json={"email": email, "password": password},
                    timeout=self._timeout,
                ),
                what="POST /sessions",
            )
        except PermanentApiError as exc:
            raise AuthError(
                f"Skylight login failed (HTTP {exc.status}). "
                "Check skylight_email / skylight_password secrets."
            ) from exc
        return self._parse(resp, SessionResponse, "POST /sessions").data.attributes.token

    def _invalidate_token(self) -> None:
        self._auth_header = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> requests.Response:
        """Single HTTP request with retry. Auth header added automatically.

        Never includes the auth token in logs or error messages.

        Note: retry_call raises PermanentApiError for non-retryable 4xx including
        401. We intercept 401 here to handle re-login (password mode) or a clear
        error message (token mode).
        """
        auth_header = self._resolve_auth_header()
        url = f"{self._base_url}{path}"
        what = f"{method.upper()} {path}"

        def _do() -> requests.Response:
            return self._session.request(
                method,
                url,
                headers={"Authorization": auth_header},
                json=json,
                params=params,
                timeout=self._timeout,
            )

        try:
            return retry_call(_do, what=what)
        except PermanentApiError as exc:
            if exc.status != 401:
                raise
            # 401 handling.
            if self._password_mode:
                log.info("%s: got 401, attempting re-login once", what)
                self._invalidate_token()
                new_header = f"Basic {self._login()}"
                self._auth_header = new_header

                def _do2() -> requests.Response:
                    return self._session.request(
                        method,
                        url,
                        headers={"Authorization": new_header},
                        json=json,
                        params=params,
                        timeout=self._timeout,
                    )

                try:
                    return retry_call(_do2, what=what)
                except PermanentApiError as exc2:
                    if exc2.status == 401:
                        raise AuthError(
                            "Skylight returned 401 after re-login. "
                            "Credentials may be invalid. "
                            "Re-seed skylight_email / skylight_password."
                        ) from exc2
                    raise
            else:
                raise AuthError(
                    "Skylight returned 401. The captured skylight_token may be expired. "
                    "Re-capture it and re-seed with: "
                    "python -m skysync.secrets set skylight_token"
                ) from exc

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(resp: requests.Response, model: type, what: str) -> Any:
        """Parse a response into a pydantic model; raise SchemaDriftError on failure."""
        body_text = resp.text
        try:
            data = resp.json()
        except Exception as exc:
            raise SchemaDriftError(
                f"SchemaDrift: {what} returned non-JSON body",
                f"HTTP {resp.status_code}; first 300 chars: {body_text[:300]!r}",
            ) from exc
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            errs = exc.errors()
            locations = [str(e["loc"]) for e in errs[:3]]
            raise SchemaDriftError(
                f"SchemaDrift: {what} response did not match {model.__name__}",
                f"HTTP {resp.status_code}; {len(errs)} validation error(s); "
                f"first locations: {locations}; body[:300]: {body_text[:300]!r}",
            ) from exc

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_categories(self) -> list[Category]:
        """GET /frames/{frame_id}/categories."""
        resp = self._request("GET", f"/frames/{self._frame_id}/categories")
        return self._parse(resp, CategoriesResponse, "GET /categories").data

    def get_chores(
        self,
        after: date,
        before: date,
        include_late: bool = True,
    ) -> ChoresResponse:
        """GET /frames/{frame_id}/chores with date window."""
        params = {
            "after": after.isoformat(),
            "before": before.isoformat(),
            "include_late": "true" if include_late else "false",
        }
        resp = self._request("GET", f"/frames/{self._frame_id}/chores", params=params)
        return self._parse(resp, ChoresResponse, "GET /chores")

    def create_chore(
        self,
        summary: str,
        category_id: str,
        start: date | None,
    ) -> Chore:
        """POST /frames/{frame_id}/chores/create_multiple — returns the first chore.

        NOTE: The vendored OpenAPI spec writes this path as
        "/chores/{choreId}reate_multiple" — that is a HAR-conversion artifact
        where "c" of "create_multiple" was parsed as the choreId path parameter.
        The real path is /chores/create_multiple (confirmed from HAR example URL).
        """
        payload: dict[str, Any] = {
            "summary": summary,
            "category_id": str(category_id),
            "category_ids": [str(category_id)],
            "recurring": False,
            "routine": False,
            "recurrence_set": None,
            "recurring_until": None,
            "start": start.isoformat() if start is not None else None,
            "start_time": None,
        }
        resp = self._request(
            "POST",
            f"/frames/{self._frame_id}/chores/create_multiple",
            json=payload,
        )
        envelope = self._parse(resp, ChoresResponse, "POST /chores/create_multiple")
        if not envelope.data:
            raise SchemaDriftError(
                "SchemaDrift: POST /chores/create_multiple returned empty data list",
                f"body[:300]: {resp.text[:300]!r}",
            )
        return envelope.data[0]

    def update_chore(
        self,
        chore_id: str,
        *,
        summary: str | None = None,
        category_id: str | None = None,
        start: date | None = None,
        status: str | None = None,
    ) -> Chore:
        """PUT /frames/{frame_id}/chores/{chore_id}."""
        payload: dict[str, Any] = {}
        if summary is not None:
            payload["summary"] = summary
        if category_id is not None:
            payload["category_id"] = str(category_id)
            payload["category_ids"] = [str(category_id)]
        if start is not None:
            payload["start"] = start.isoformat()
        if status is not None:
            payload["status"] = status
        resp = self._request(
            "PUT",
            f"/frames/{self._frame_id}/chores/{chore_id}",
            json=payload,
        )
        return self._parse(resp, ChoreResponse, f"PUT /chores/{chore_id}").data

    def delete_chore(self, chore_id: str) -> None:
        """DELETE /frames/{frame_id}/chores/{chore_id}. 404 is tolerated."""
        try:
            self._request("DELETE", f"/frames/{self._frame_id}/chores/{chore_id}")
        except PermanentApiError as exc:
            if exc.status == 404:
                return  # Already gone — treat as success.
            raise

    # ------------------------------------------------------------------
    # List methods
    # ------------------------------------------------------------------

    def get_lists(self) -> list[SkylightList]:
        """GET /frames/{frame_id}/lists."""
        resp = self._request("GET", f"/frames/{self._frame_id}/lists")
        return self._parse(resp, ListsResponse, "GET /lists").data

    def get_list_items(self, list_id: str) -> list[ListItem]:
        """GET /frames/{frame_id}/lists — items are in 'included'; filter by list_id.

        The spec returns lists + included items in a single GET /lists call; we
        extract items whose relationships.list.data.id matches list_id.
        """
        resp = self._request("GET", f"/frames/{self._frame_id}/lists")
        envelope = self._parse(resp, ListsResponse, "GET /lists (for items)")
        result: list[ListItem] = []
        for item in envelope.included:
            extra = item.model_extra or {}
            rel = extra.get("relationships") or {}
            lst_data = (rel.get("list") or {}).get("data") or {}
            if isinstance(lst_data, dict) and str(lst_data.get("id", "")) == str(list_id):
                result.append(item)
        return result

    def create_list_item(self, list_id: str, label: str) -> ListItem:
        """POST /frames/{frame_id}/lists/{list_id}/list_items."""
        resp = self._request(
            "POST",
            f"/frames/{self._frame_id}/lists/{list_id}/list_items",
            json={"label": label},
        )
        return self._parse(resp, ListItemResponse, "POST /list_items").data

    def update_list_item(
        self,
        list_id: str,
        item_id: str,
        *,
        label: str | None = None,
        status: str | None = None,
    ) -> ListItem:
        """PUT /frames/{frame_id}/lists/{list_id}/list_items/{item_id}."""
        payload: dict[str, Any] = {}
        if label is not None:
            payload["label"] = label
        if status is not None:
            payload["status"] = status
        resp = self._request(
            "PUT",
            f"/frames/{self._frame_id}/lists/{list_id}/list_items/{item_id}",
            json=payload,
        )
        return self._parse(resp, ListItemResponse, f"PUT /list_items/{item_id}").data

    def delete_list_item(self, list_id: str, item_id: str) -> None:
        """DELETE /frames/{frame_id}/lists/{list_id}/list_items/{item_id}. 404 tolerated."""
        try:
            self._request(
                "DELETE",
                f"/frames/{self._frame_id}/lists/{list_id}/list_items/{item_id}",
            )
        except PermanentApiError as exc:
            if exc.status == 404:
                return
            raise
