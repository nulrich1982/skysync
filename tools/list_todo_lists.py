"""Print every To Do list visible to the signed-in account (incl. shared)."""

from __future__ import annotations

from skysync.config import load_config
from skysync.graph.auth import DelegatedGraphAuth, GraphSession
from skysync.secrets import SecretStore

cfg = load_config("config.toml")
session = GraphSession(DelegatedGraphAuth(cfg.graph, SecretStore(cfg.resolve("secrets"))).get_token)
for item in session.iter_items("/me/todo/lists", what="list To Do lists"):
    print(
        f"{item.get('displayName')!r:45} owner={item.get('isOwner')} "
        f"shared={item.get('isShared')} wellknown={item.get('wellknownListName')}"
    )
