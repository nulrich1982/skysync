"""Skylight OAuth 2.0 auth — the hands-off token path.

Skylight's mobile client uses OAuth 2.0 Authorization-Code + PKCE (public
client ``skylight-mobile``, no secret). Access tokens expire after 2 hours;
refresh tokens ROTATE on every use. So auth becomes fully automated, exactly
like the Microsoft Graph leg:

    * one-time ``initial_login`` mints the first refresh token (reusing the
      hosted Rails login form we already talk to);
    * ``get_access_token`` returns a cached access token, transparently
      refreshing (and persisting the rotated refresh token) when it expires.

Credit: the OAuth flow was reverse-engineered by andreabedini/skylight-cli.

Secrets used (platform secret store — DPAPI on Windows, permissions-only
files on Linux; see ``skysync.secrets``):
    skylight_refresh_token   rotating refresh token (the durable credential)
    skylight_access_token    cached 2-hour access token
    skylight_access_expiry   unix epoch (str) when the access token expires

No token or password is ever logged.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets as _secrets
import time
from urllib.parse import parse_qs, urlparse

import requests

from ..errors import AuthError, PermanentApiError
from ..retry import retry_call
from ..secrets import SecretStore, _BACKEND_LABEL

log = logging.getLogger(__name__)

AUTH_BASE = "https://app.ourskylight.com"
CLIENT_ID = "skylight-mobile"
REDIRECT_URI = "skylight-family://welcome"
SCOPE = "everything"

_REFRESH_SECRET = "skylight_refresh_token"
_ACCESS_SECRET = "skylight_access_token"
_EXPIRY_SECRET = "skylight_access_expiry"

_BROWSERISH = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    ),
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = _secrets.token_urlsafe(64)  # 43-128 chars, URL-safe
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _session(extra_headers: dict[str, str] | None) -> requests.Session:
    s = requests.Session()
    s.headers.update(_BROWSERISH)
    if extra_headers:
        s.headers.update(extra_headers)
    return s


def _token_request(sess: requests.Session, data: dict[str, str]) -> dict:
    """POST /oauth/token and return the parsed JSON, or raise AuthError."""
    url = f"{AUTH_BASE}/oauth/token"
    resp = sess.post(url, data=data, timeout=30, allow_redirects=False)
    if resp.status_code != 200:
        body = resp.text[:300]
        raise AuthError(f"Skylight /oauth/token {data.get('grant_type')} failed (HTTP {resp.status_code}): {body}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise AuthError("Skylight /oauth/token returned non-JSON") from exc
    if "access_token" not in payload or "refresh_token" not in payload:
        raise AuthError("Skylight /oauth/token response missing access_token/refresh_token")
    return payload


def _persist(store: SecretStore, payload: dict) -> str:
    """Persist rotated refresh token FIRST (crash safety), then access token +
    expiry. Returns the access token."""
    # Refresh token rotates each use; losing it means re-login, so save it
    # before anything else can fail.
    store.set(_REFRESH_SECRET, payload["refresh_token"])
    expires_in = int(payload.get("expires_in", 7200))
    store.set(_ACCESS_SECRET, payload["access_token"])
    store.set(_EXPIRY_SECRET, str(int(time.time()) + expires_in))
    return payload["access_token"]


def refresh_access_token(store: SecretStore, extra_headers: dict[str, str] | None = None) -> str:
    """Exchange the stored refresh token for a fresh access token, persisting
    the rotated refresh token. Returns the new access token."""
    refresh = store.get_optional(_REFRESH_SECRET)
    if not refresh:
        raise AuthError(
            "No Skylight refresh token. Run the one-time login: "
            "python -m skysync.skylight.oauth login"
        )
    sess = _session(extra_headers)
    try:
        resp = retry_call(
            lambda: sess.post(
                f"{AUTH_BASE}/oauth/token",
                data={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh},
                timeout=30,
                allow_redirects=False,
            ),
            what="POST /oauth/token (refresh)",
        )
    except PermanentApiError as exc:
        # 4xx: the refresh token is revoked/expired (or rotated away by a lost
        # write). A new interactive login is required.
        raise AuthError(
            "Skylight refresh token rejected (revoked/expired). Re-run the "
            "one-time login: python -m skysync.skylight.oauth login"
        ) from exc
    payload = resp.json()
    if "access_token" not in payload or "refresh_token" not in payload:
        raise AuthError("Skylight refresh response missing tokens")
    log.info("skylight oauth: access token refreshed")
    return _persist(store, payload)


def get_access_token(store: SecretStore, extra_headers: dict[str, str] | None = None) -> str:
    """Return a valid access token, using the cached one until it is within
    120s of expiry, otherwise refreshing."""
    access = store.get_optional(_ACCESS_SECRET)
    expiry = store.get_optional(_EXPIRY_SECRET)
    if access and expiry:
        try:
            if int(expiry) - 120 > int(time.time()):
                return access
        except ValueError:
            pass
    return refresh_access_token(store, extra_headers)


def has_oauth(store: SecretStore) -> bool:
    return bool(store.get_optional(_REFRESH_SECRET))


def initial_login(
    store: SecretStore,
    email: str,
    password: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """One-time: log in via the hosted Rails form, run the PKCE authorization-
    code flow, and persist the first refresh token. Reuses the same
    /auth/session form the web app uses."""
    import html as htmllib
    import re

    sess = _session(extra_headers)
    verifier, challenge = _pkce_pair()
    state = _secrets.token_urlsafe(24)
    authorize_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
    }

    # 1. Start the OAuth request FIRST. The server stashes it against the
    #    session and 302s to the hosted login page; after login it will
    #    redirect back here to mint the code.
    sess.get(f"{AUTH_BASE}/oauth/authorize", params=authorize_params, allow_redirects=False, timeout=30)

    # 2. Hosted login form: CSRF token.
    r = sess.get(f"{AUTH_BASE}/auth/session/new", timeout=30)
    m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', r.text)
    if not m:
        raise AuthError("Could not find the login form CSRF token")
    csrf = htmllib.unescape(m.group(1))

    # 3. Submit credentials, then follow the redirect chain (which loops back
    #    through /oauth/authorize) until it emits the custom-scheme code.
    resp = sess.post(
        f"{AUTH_BASE}/auth/session",
        data={"authenticity_token": csrf, "email": email, "password": password},
        headers={"Referer": f"{AUTH_BASE}/auth/session/new"},
        allow_redirects=False,
        timeout=30,
    )
    code = None
    for _ in range(8):
        location = resp.headers.get("Location", "")
        if not location:
            break
        if location.startswith(REDIRECT_URI):
            qs = parse_qs(urlparse(location).query)
            code = (qs.get("code") or [None])[0]
            if (qs.get("state") or [None])[0] != state:
                raise AuthError("OAuth state mismatch — aborting (possible interference)")
            break
        if "/auth/session/new" in location:
            raise AuthError("Skylight login rejected — check skylight_email / skylight_password")
        if location.startswith("/"):
            location = AUTH_BASE + location
        resp = sess.get(location, allow_redirects=False, timeout=30)
    if not code:
        raise AuthError("OAuth flow did not yield an authorization code after login")

    # 4. Exchange the code for the first access + refresh token pair.
    payload = _token_request(
        sess,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        },
    )
    _persist(store, payload)
    log.info("skylight oauth: initial login complete; refresh token stored")


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from ..config import load_config

    ap = argparse.ArgumentParser(prog="python -m skysync.skylight.oauth")
    ap.add_argument("action", choices=["login", "refresh", "status"])
    ap.add_argument("--config", default="config.toml")
    ns = ap.parse_args(argv)

    cfg = load_config(ns.config)
    store = SecretStore(cfg.resolve("secrets"))
    headers = cfg.skylight.headers

    if ns.action == "login":
        email = store.get("skylight_email")
        password = store.get("skylight_password")
        initial_login(store, email, password, headers)
        print(f"OK: Skylight OAuth login complete; refresh token stored ({_BACKEND_LABEL}).")
    elif ns.action == "refresh":
        tok = refresh_access_token(store, headers)
        print(f"OK: refreshed access token ({tok[:8]}...len {len(tok)}).")
    else:  # status
        print(f"refresh token present: {has_oauth(store)}")
        exp = store.get_optional(_EXPIRY_SECRET)
        if exp:
            secs = int(exp) - int(time.time())
            print(f"cached access token expires in: {secs}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
