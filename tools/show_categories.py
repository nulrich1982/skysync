"""Print every Skylight category with its chore-chart flags."""

from __future__ import annotations

from skysync.config import load_config
from skysync.secrets import SecretStore
from skysync.skylight.client import SkylightApi

cfg = load_config("config.toml")
api = SkylightApi(cfg.skylight.frame_id, SecretStore(cfg.resolve("secrets")))
for c in api.get_categories():
    a = c.attributes
    print(
        f"{a.label!r:32} id={c.id:<10} linked_to_profile={a.linked_to_profile} "
        f"chore_chart={getattr(a, 'selected_for_chore_chart', None)}"
    )
