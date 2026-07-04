"""Probe POST /api/sessions to see WHY login is rejected. Prints status and
response body only — never the password."""

from __future__ import annotations

import requests

from skysync.config import load_config
from skysync.secrets import SecretStore

cfg = load_config("config.toml")
store = SecretStore(cfg.resolve("secrets"))
email = store.get("skylight_email")
password = store.get("skylight_password")

print(f"email seeded: {email!r}  (len {len(email)})")
print(f"password seeded: length {len(password)}, has leading/trailing space: "
      f"{password != password.strip()}")

# Match the real browser exactly: origin/referer = ourskylight.com, current
# Edge UA, plus the captured skylight-api-version header.
s = requests.Session()
s.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
        ),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://ourskylight.com",
        "Referer": "https://ourskylight.com/",
        "priority": "u=1, i",
    }
)
if cfg.skylight.headers:
    s.headers.update(cfg.skylight.headers)
    print("applying config headers:", list(cfg.skylight.headers))
r = s.post(
    "https://app.ourskylight.com/api/sessions",
    json={"email": email, "password": password},
    timeout=30,
)
print(f"\nPOST /api/sessions (browser-matched) -> HTTP {r.status_code}")
print("response body:", r.text[:500])
