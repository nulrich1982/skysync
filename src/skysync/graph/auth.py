"""Graph authentication. ORCHESTRATOR-OWNED — hard constraint #1.

Microsoft To Do task CRUD is DELEGATED-ONLY in Graph: app-only (client
credentials) tokens are rejected for /me/todo. So this module:

  * uses MSAL ``PublicClientApplication`` with a serializable token cache;
  * authenticates interactively ONCE via device code (``skysync login``);
  * on every subsequent run calls ``acquire_token_silent`` — MSAL transparently
    redeems the refresh token, and refresh tokens are ROTATED by AAD, so we
    persist the cache back to the DPAPI store after EVERY acquisition. Skipping
    that persist is how unattended setups die weeks later; we never skip it.

The SharePoint leg may run either on the same delegated token (default; scope
``Sites.ReadWrite.All`` added at login) or app-only via client credentials
(``graph.sharepoint_auth = "app_only"``) — SharePoint, unlike To Do, supports
app-only. See DESIGN_NOTES.md for the trade-off.

The token cache (which contains the refresh token) is itself a secret and is
stored DPAPI-encrypted under the name ``msal_token_cache``.
"""

from __future__ import annotations

import logging
from typing import Callable

import msal
import requests

from ..config import GraphConfig
from ..errors import AuthError
from ..retry import retry_call
from ..secrets import SecretStore

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# offline_access / openid / profile are added by MSAL automatically.
TODO_SCOPES = ["Tasks.ReadWrite"]
SP_DELEGATED_SCOPES = ["Sites.ReadWrite.All"]
APP_ONLY_SCOPES = ["https://graph.microsoft.com/.default"]

_CACHE_SECRET_NAME = "msal_token_cache"


class DelegatedGraphAuth:
    """Delegated (user) auth with DPAPI-persisted, rotation-safe token cache."""

    def __init__(self, cfg: GraphConfig, store: SecretStore, include_sharepoint_scope: bool = False):
        self._store = store
        self._cache = msal.SerializableTokenCache()
        cached = store.get_optional(_CACHE_SECRET_NAME)
        if cached:
            self._cache.deserialize(cached)
        # tenant_id "consumers" = personal Microsoft account (the default
        # two-way deployment); a GUID = work/school tenant.
        self._app = msal.PublicClientApplication(
            cfg.client_id,
            authority=f"https://login.microsoftonline.com/{cfg.tenant_id}",
            token_cache=self._cache,
        )
        self._scopes = list(TODO_SCOPES)
        if include_sharepoint_scope:
            self._scopes += SP_DELEGATED_SCOPES

    # -- cache persistence (constraint #1: persist ROTATED refresh token) ----
    def _persist_cache(self) -> None:
        if self._cache.has_state_changed:
            self._store.set(_CACHE_SECRET_NAME, self._cache.serialize())
            log.debug("MSAL token cache persisted (rotated refresh token saved)")

    def login_device_flow(self) -> str:
        """One-time interactive login. Prints the device-code instructions."""
        flow = self._app.initiate_device_flow(scopes=self._scopes)
        if "user_code" not in flow:
            raise AuthError(f"device flow could not start: {flow.get('error_description', flow)}")
        print()
        print(flow["message"])  # e.g. "go to https://microsoft.com/devicelogin and enter XXXX"
        print()
        result = self._app.acquire_token_by_device_flow(flow)  # blocks until done
        self._persist_cache()
        if "access_token" not in result:
            raise AuthError(f"login failed: {result.get('error_description', result)}")
        user = result.get("id_token_claims", {}).get("preferred_username", "<unknown>")
        log.info("logged in as %s; token cache stored via DPAPI", user)
        return user

    def get_token(self) -> str:
        accounts = self._app.get_accounts()
        if not accounts:
            raise AuthError("no cached account — run 'python -m skysync.main login' once interactively")
        result = self._app.acquire_token_silent(self._scopes, account=accounts[0])
        # acquire_token_silent may have redeemed (and rotated) the refresh
        # token even on failure paths; persist before raising.
        self._persist_cache()
        if not result or "access_token" not in result:
            raise AuthError(
                "silent token acquisition failed (refresh token expired or revoked). "
                "Run 'python -m skysync.main login' again. "
                f"Details: {result.get('error_description') if result else 'no result'}"
            )
        return result["access_token"]


class AppOnlyGraphAuth:
    """Client-credentials auth — permitted ONLY for the SharePoint leg."""

    def __init__(self, cfg: GraphConfig, store: SecretStore):
        secret = store.get("graph_client_secret")  # raises ConfigError if unseeded
        self._app = msal.ConfidentialClientApplication(
            cfg.client_id,
            client_credential=secret,
            authority=f"https://login.microsoftonline.com/{cfg.tenant_id}",
        )

    def get_token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=APP_ONLY_SCOPES)
        if "access_token" not in result:
            raise AuthError(f"app-only token failed: {result.get('error_description', result)}")
        return result["access_token"]


class GraphSession:
    """Authenticated Graph HTTP with retry; refreshes once on 401, then fails
    loud with AuthError so the run aborts cleanly."""

    def __init__(self, token_provider: Callable[[], str], timeout: float = 30.0):
        self._get_token = token_provider
        self._timeout = timeout
        self._http = requests.Session()

    def request(self, method: str, url: str, *, what: str, **kw) -> requests.Response:
        if url.startswith("/"):
            url = GRAPH_BASE + url
        token = self._get_token()

        def _do(tok: str) -> requests.Response:
            headers = dict(kw.get("headers") or {})
            headers["Authorization"] = f"Bearer {tok}"
            opts = {k: v for k, v in kw.items() if k != "headers"}
            return self._http.request(method, url, headers=headers, timeout=self._timeout, **opts)

        resp = retry_call(lambda: _do(token), what=what)
        if resp.status_code == 401:
            log.info("%s: 401, refreshing token once", what)
            token2 = self._get_token()
            resp = retry_call(lambda: _do(token2), what=what)
            if resp.status_code == 401:
                raise AuthError(f"{what}: Graph rejected token twice (expired/revoked?)")
        return resp

    def get_json(self, url: str, *, what: str, **kw) -> dict:
        return self.request("GET", url, what=what, **kw).json()

    def post_json(self, url: str, body: dict, *, what: str) -> dict:
        return self.request("POST", url, what=what, json=body).json()

    def patch_json(self, url: str, body: dict, *, what: str) -> dict:
        return self.request("PATCH", url, what=what, json=body).json()

    def delete(self, url: str, *, what: str) -> None:
        self.request("DELETE", url, what=what)

    def iter_items(self, url: str, *, what: str, params: dict | None = None):
        """Yield items across @odata.nextLink pages."""
        page = self.get_json(url, what=what, params=params)
        while True:
            yield from page.get("value", [])
            nxt = page.get("@odata.nextLink")
            if not nxt:
                return
            page = self.get_json(nxt, what=what)
