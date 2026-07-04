"""Hands-off Skylight Bearer token renewal via a real headless browser login.

Captured Skylight tokens expire (roughly weekly). Rather than re-capturing a
token by hand from DevTools, this module drives an actual Chromium browser
through the login form at ``https://app.ourskylight.com/auth/session/new``
and sniffs the fresh ``Authorization: Bearer <token>`` header off the SPA's
own authenticated API calls (``/api/frames/{frame_id}/...``). A real browser
satisfies the app-version gate natively, so no extra headers are needed here.

Split into two functions so the persistence/orchestration logic is testable
without Playwright installed:

    _run_browser_login(...)     -- the actual browser automation (requires
                                    the optional 'browser' extra)
    refresh_token_via_browser(...) -- reads credentials, calls the above,
                                    persists and returns the token

Never logs or prints the password or the token value.
"""

from __future__ import annotations

import logging

from skysync.errors import AuthError

log = logging.getLogger(__name__)

_LOGIN_URL = "https://app.ourskylight.com/auth/session/new"
_INSTALL_HINT = 'pip install -e ".[browser]"; then run: playwright install chromium'


def _run_browser_login(
    email: str,
    password: str,
    frame_id: str,
    *,
    headless: bool = True,
    timeout_s: float = 90.0,
) -> str:
    """Drive a headless Chromium login and return the full Authorization
    header value (e.g. "Bearer abc123") captured from the app's first
    authenticated /api/frames/{frame_id} request. Raises AuthError on any
    failure (login rejected, no token seen within timeout).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AuthError(
            "Playwright is not installed; hands-off Skylight token refresh "
            f"requires it. Install with: {_INSTALL_HINT}"
        ) from exc

    captured: dict[str, str] = {}
    api_urls_seen: list[str] = []

    def _on_request(request: object) -> None:
        try:
            url = request.url  # type: ignore[attr-defined]
            if "/api/" not in url:
                return
            auth = request.headers.get("authorization")  # type: ignore[attr-defined]
            api_urls_seen.append(url.split("?")[0] + (" [auth]" if auth else ""))
            # The app's authenticated calls carry a Bearer token; the login
            # POST does not, and an early bootstrap request may send an EMPTY
            # "Bearer" placeholder — require a real token value after the scheme.
            if auth and auth.lower().startswith("bearer "):
                value = auth.split(None, 1)[1].strip()
                if len(value) >= 20:
                    captured.setdefault("authorization", auth)
        except Exception:
            log.debug("token_refresh: ignoring malformed request during capture", exc_info=True)

    def _poll_for_token(page: object, budget_ms: int) -> bool:
        waited = 0
        while waited < budget_ms:
            if "authorization" in captured:
                return True
            page.wait_for_timeout(500)  # type: ignore[attr-defined]
            waited += 500
        return "authorization" in captured

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
            context.on("request", _on_request)
            page = context.new_page()

            log.info("token_refresh: navigating to Skylight login page")
            page.goto(_LOGIN_URL, timeout=30000)
            page.fill('input[name="email"]', email)
            page.fill('input[name="password"]', password)

            log.info("token_refresh: submitting login")
            submit = page.locator('button[type="submit"]').first
            if submit.count() == 0:
                submit = page.get_by_role("button", name="Log In").first
            submit.click()

            # Let the post-login redirect chain settle, then fail fast on a
            # rejected login (still sitting on the login form with an error).
            page.wait_for_timeout(3000)
            try:
                if "/auth/session/new" in page.url:
                    body = page.locator("body").inner_text(timeout=1000).lower()
                    if "invalid" in body or "incorrect" in body or "wrong" in body:
                        raise AuthError("Skylight browser login failed: credentials rejected")
            except AuthError:
                raise
            except Exception:
                pass

            # The calendar app lives on ourskylight.com and makes the
            # authenticated /api/... calls we sniff. Nudge the app to load it
            # if the post-login redirect didn't already.
            if not _poll_for_token(page, 8000):
                for app_url in ("https://ourskylight.com/", "https://app.ourskylight.com/"):
                    try:
                        log.info("token_refresh: loading app to trigger authenticated API calls")
                        page.goto(app_url, timeout=30000)
                    except Exception:
                        continue
                    if _poll_for_token(page, int(timeout_s * 1000) // 2):
                        break

            if "authorization" not in captured:
                seen = ", ".join(dict.fromkeys(api_urls_seen)) or "none"
                raise AuthError(
                    f"Skylight browser login failed: no Bearer token seen within {timeout_s:.0f}s. "
                    f"API requests observed: {seen}"
                )

            log.info("token_refresh: token captured")
            return captured["authorization"]
        finally:
            browser.close()


def refresh_token_via_browser(cfg: object, store: object) -> str:
    """Read skylight_email/skylight_password from ``store``, drive a browser
    login, persist the captured Authorization header value as the secret
    "skylight_token", and return it. Raises AuthError on failure.
    """
    email = store.get("skylight_email")  # type: ignore[attr-defined]
    password = store.get("skylight_password")  # type: ignore[attr-defined]
    frame_id = cfg.skylight.frame_id  # type: ignore[attr-defined]
    headless = cfg.skylight.auto_refresh_headless  # type: ignore[attr-defined]

    token = _run_browser_login(email, password, frame_id, headless=headless)
    store.set("skylight_token", token)  # type: ignore[attr-defined]
    return token


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m skysync.skylight.token_refresh",
        description="Renew the Skylight Bearer token via a headless browser login.",
    )
    ap.add_argument("--config", default="config.toml", help="Path to config.toml")
    ap.add_argument("--show", action="store_true", help="Print a masked confirmation of the refreshed token")
    ns = ap.parse_args(argv)

    from skysync.config import load_config
    from skysync.secrets import SecretStore

    cfg = load_config(ns.config)
    store = SecretStore(cfg.resolve("secrets"))

    val = refresh_token_via_browser(cfg, store)
    if ns.show:
        print(f"refreshed skylight_token: {val[:10]}...(len {len(val)})")
    else:
        print("refreshed skylight_token: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
