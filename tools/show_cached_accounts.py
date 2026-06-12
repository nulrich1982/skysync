"""Show which accounts the MSAL cache holds (usernames/authority only)."""

from __future__ import annotations

import msal

from skysync.config import load_config
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
if not accounts:
    print("cache holds NO account usable for authority:", cfg.graph.tenant_id)
for a in accounts:
    print(f"username={a.get('username')}  environment={a.get('environment')}  "
          f"home_account_id={str(a.get('home_account_id'))[:20]}...")
