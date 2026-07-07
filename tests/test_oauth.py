"""Tests for skysync.skylight.oauth — PKCE login, refresh rotation, caching."""

from __future__ import annotations

import base64
import hashlib
import time

import pytest

from skysync.errors import AuthError
from skysync.skylight import oauth


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None, location=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}
        if location is not None:
            self.headers["Location"] = location

    def json(self):
        return self._json


class FakeStore:
    def __init__(self, **values):
        self._v = dict(values)

    def get(self, name):
        return self._v[name]

    def get_optional(self, name):
        return self._v.get(name)

    def set(self, name, value):
        self._v[name] = value

    def exists(self, name):
        return name in self._v

    def delete(self, name):
        self._v.pop(name, None)


class ScriptedSession:
    """Fake requests.Session that answers /auth and /oauth by URL, echoing the
    PKCE state back on the authorize redirect."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    state = None

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if url.endswith("/auth/session/new"):
            return FakeResponse(text='<input name="authenticity_token" value="csrf-xyz">')
        if "/oauth/authorize" in url:
            params = kw.get("params")
            if params and "state" in params:
                # Step 1: server stashes the request, 302s to login.
                self.state = params["state"]
                return FakeResponse(status_code=302, location=f"{oauth.AUTH_BASE}/auth/session/new")
            # Loop hop after login: emit the custom-scheme code.
            return FakeResponse(
                status_code=302, location=f"skylight-family://welcome?code=auth-code-123&state={self.state}"
            )
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/auth/session"):
            # Successful login redirects back through /oauth/authorize.
            return FakeResponse(status_code=302, location=f"{oauth.AUTH_BASE}/oauth/authorize")
        if url.endswith("/oauth/token"):
            return FakeResponse(
                json_data={
                    "access_token": "access-A",
                    "refresh_token": "refresh-A",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                }
            )
        raise AssertionError(f"unexpected POST {url}")


def test_pkce_pair_is_valid_s256():
    verifier, challenge = oauth._pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_initial_login_persists_refresh(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(oauth, "_session", lambda h: ScriptedSession())
    oauth.initial_login(store, "a@example.com", "pw")
    assert store.get_optional("skylight_refresh_token") == "refresh-A"
    assert store.get_optional("skylight_access_token") == "access-A"
    assert int(store.get_optional("skylight_access_expiry")) > int(time.time())


def test_initial_login_rejected_credentials(monkeypatch):
    class RejectSession(ScriptedSession):
        def post(self, url, **kw):
            if url.endswith("/auth/session"):
                # Rejected login bounces back to the login page.
                return FakeResponse(status_code=302, location=f"{oauth.AUTH_BASE}/auth/session/new")
            return super().post(url, **kw)

    monkeypatch.setattr(oauth, "_session", lambda h: RejectSession())
    with pytest.raises(AuthError, match="login rejected"):
        oauth.initial_login(FakeStore(), "a@example.com", "bad")


def test_get_access_token_uses_cache(monkeypatch):
    store = FakeStore(
        skylight_refresh_token="refresh-A",
        skylight_access_token="cached-access",
        skylight_access_expiry=str(int(time.time()) + 3600),
    )

    def boom(*a, **k):
        raise AssertionError("should not refresh when cache is valid")

    monkeypatch.setattr(oauth, "refresh_access_token", boom)
    assert oauth.get_access_token(store) == "cached-access"


def test_get_access_token_refreshes_when_expired(monkeypatch):
    store = FakeStore(
        skylight_refresh_token="refresh-A",
        skylight_access_token="old",
        skylight_access_expiry=str(int(time.time()) - 10),  # expired
    )
    monkeypatch.setattr(oauth, "_session", lambda h: ScriptedSession())
    tok = oauth.get_access_token(store)
    assert tok == "access-A"
    assert store.get_optional("skylight_refresh_token") == "refresh-A"  # rotated (same value here)


def test_refresh_rotates_and_persists(monkeypatch):
    store = FakeStore(skylight_refresh_token="refresh-OLD")

    class RotateSession(ScriptedSession):
        def post(self, url, **kw):
            assert kw["data"]["grant_type"] == "refresh_token"
            assert kw["data"]["refresh_token"] == "refresh-OLD"
            return FakeResponse(json_data={"access_token": "access-NEW", "refresh_token": "refresh-NEW", "expires_in": 7200})

    monkeypatch.setattr(oauth, "_session", lambda h: RotateSession())
    tok = oauth.refresh_access_token(store)
    assert tok == "access-NEW"
    assert store.get_optional("skylight_refresh_token") == "refresh-NEW"  # rotation persisted


def test_refresh_without_token_raises():
    with pytest.raises(AuthError, match="one-time login"):
        oauth.refresh_access_token(FakeStore())


def test_refresh_4xx_prompts_relogin(monkeypatch):
    store = FakeStore(skylight_refresh_token="revoked")

    class RejectSession(ScriptedSession):
        def post(self, url, **kw):
            return FakeResponse(status_code=400, text='{"error":"invalid_grant"}')

    monkeypatch.setattr(oauth, "_session", lambda h: RejectSession())
    with pytest.raises(AuthError, match="Re-run the"):
        oauth.refresh_access_token(store)


def test_has_oauth():
    assert oauth.has_oauth(FakeStore(skylight_refresh_token="x")) is True
    assert oauth.has_oauth(FakeStore()) is False
