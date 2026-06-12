"""Diagnose silent token acquisition: print the error, never the token."""

from __future__ import annotations

import msal

from skysync.config import load_config
from skysync.graph.auth import TODO_SCOPES
from skysync.secrets import SecretStore

cfg = load_config("config.toml")
store = SecretStore(cfg.resolve("secrets"))
cache = msal.SerializableTokenCache()
raw = store.get_optional("msal_token_cache")
if raw:
    cache.deserialize(raw)
app = msal.PublicClientApplication(
    cfg.graph.client_id,
    authority=f"https://login.microsoftonline.com/{cfg.graph.tenant_id}",
    token_cache=cache,
)
accounts = app.get_accounts()
print("accounts:", [a.get("username") for a in accounts])
print("requesting scopes:", TODO_SCOPES)
result = app.acquire_token_silent_with_error(TODO_SCOPES, account=accounts[0] if accounts else None)
if result is None:
    print("result: None (no matching token/RT for this authority+scopes)")
elif "access_token" in result:
    print("SUCCESS: token acquired silently (not shown), expires_in:", result.get("expires_in"))
else:
    print("error:", result.get("error"))
    print("description:", result.get("error_description"))
    print("suberror:", result.get("suberror"))
