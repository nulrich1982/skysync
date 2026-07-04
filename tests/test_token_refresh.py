"""Tests for skysync.skylight.token_refresh.

Must pass WITHOUT playwright installed. `refresh_token_via_browser` is tested
by monkeypatching `_run_browser_login`; the "no playwright" path is exercised
directly against the real `_run_browser_login` with the import forced to fail.
"""

from __future__ import annotations

import builtins
import types

import pytest

from skysync.errors import AuthError
from skysync.skylight import token_refresh


class FakeStore:
    """Minimal in-memory stand-in for skysync.secrets.SecretStore."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    def get(self, name: str) -> str:
        return self._values[name]

    def get_optional(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)


def _make_cfg(*, frame_id: str = "frame-123", auto_refresh_headless: bool = True) -> types.SimpleNamespace:
    skylight = types.SimpleNamespace(frame_id=frame_id, auto_refresh_headless=auto_refresh_headless)
    return types.SimpleNamespace(skylight=skylight)


def test_refresh_persists_and_returns(monkeypatch):
    store = FakeStore({"skylight_email": "a@example.com", "skylight_password": "hunter2"})
    cfg = _make_cfg()

    captured_args: dict[str, object] = {}

    def fake_run_browser_login(email, password, frame_id, *, headless=True, timeout_s=90.0):
        captured_args["email"] = email
        captured_args["password"] = password
        captured_args["frame_id"] = frame_id
        captured_args["headless"] = headless
        return "Bearer freshtok"

    monkeypatch.setattr(token_refresh, "_run_browser_login", fake_run_browser_login)

    result = token_refresh.refresh_token_via_browser(cfg, store)

    assert result == "Bearer freshtok"
    assert store.get("skylight_token") == "Bearer freshtok"
    assert captured_args["email"] == "a@example.com"
    assert captured_args["password"] == "hunter2"
    assert captured_args["frame_id"] == "frame-123"
    assert captured_args["headless"] is True


def test_login_failure_raises(monkeypatch):
    store = FakeStore({"skylight_email": "a@example.com", "skylight_password": "hunter2"})
    cfg = _make_cfg()

    def fake_run_browser_login(email, password, frame_id, *, headless=True, timeout_s=90.0):
        raise AuthError("Skylight browser login failed: credentials rejected by login form")

    monkeypatch.setattr(token_refresh, "_run_browser_login", fake_run_browser_login)

    with pytest.raises(AuthError, match="credentials rejected"):
        token_refresh.refresh_token_via_browser(cfg, store)

    # Nothing should be persisted on failure.
    assert store.get_optional("skylight_token") is None


def test_missing_playwright_raises_autherror(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright.sync_api" or name.startswith("playwright"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(AuthError, match="(?i)playwright"):
        token_refresh._run_browser_login("a@example.com", "hunter2", "frame-123")
