"""Configuration: TOML file with NON-SECRET values only, validated by pydantic.

Secrets (Skylight credentials, Graph client secret, token caches) live in the
DPAPI store — see ``skysync.secrets``. Anything secret found in the config
file is a hard error to stop the obvious mistake early.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .errors import ConfigError

FORBIDDEN_KEYS = {"password", "client_secret", "token", "secret", "bearer"}


class GeneralConfig(BaseModel):
    state_dir: str = "state"
    log_dir: str = "logs"
    log_level: str = "INFO"


class HeartbeatConfig(BaseModel):
    file: str = "state/heartbeat.json"
    ping_url: str = ""  # optional dead-man's-switch GET on success
    max_age_minutes: int = 45


class ScheduleConfig(BaseModel):
    interval_minutes: int = 15  # consumed by register-task.ps1


class GraphConfig(BaseModel):
    # "consumers" for a personal Microsoft account (the default deployment),
    # a tenant GUID for a work/school account.
    tenant_id: str
    client_id: str
    # SharePoint leg auth — only relevant when [sharepoint] is configured.
    # To Do is ALWAYS delegated (Graph does not support app-only To Do CRUD).
    sharepoint_auth: Literal["delegated", "app_only"] = "delegated"

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _not_placeholder(cls, v: str) -> str:
        if not v or v.startswith("YOUR-"):
            raise ValueError("set real Graph tenant_id/client_id in config.toml")
        return v


class SharePointConfig(BaseModel):
    site_id: str
    list_id: str


class TodoConfig(BaseModel):
    default_list: str = "Tasks"  # tasks with no mapped assignee land here


class SkylightConfig(BaseModel):
    frame_id: str
    chore_window_days_past: int = 14
    chore_window_days_future: int = 60
    sync_recurring: bool = False  # recurring/routine chores are Skylight-native


class GroceryConfig(BaseModel):
    """Second sync pairing: a Skylight LIST mirrored to a To Do list."""

    enabled: bool = False
    skylight_list: str = "Grocery List"  # label of the list on the frame
    todo_list: str = "Grocery List"  # display name of the To Do list
    # Groceries are ephemeral; by default deletes mirror in BOTH directions
    # (clearing the frame's list clears To Do, and vice versa).
    mirror_deletes: bool = True


class SyncConfig(BaseModel):
    conflict_policy: Literal["most_recent_wins", "sharepoint_wins"] = "most_recent_wins"
    completions: Literal["both_ways"] = "both_ways"
    deletes_todo_to_skylight: bool = True
    deletes_skylight_to_todo: bool = False
    deletes_sharepoint_propagate: bool = True
    stream_mode: Literal["per_child", "shared"] = "per_child"
    # Already-completed tasks are never CREATED on a side that lacks them
    # (completions on synced tasks always propagate regardless).
    backfill_completed: bool = False
    # At most this many creates per side per run; a backlog drains across
    # successive scheduled runs instead of flooding an API in one shot.
    max_creates_per_run: int = 100


class ChildMapping(BaseModel):
    todo_list: str
    skylight_category: str
    sp_assignee: str | None = None  # only used when [sharepoint] is configured


class MappingConfig(BaseModel):
    children: dict[str, ChildMapping] = Field(default_factory=dict)


class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    graph: GraphConfig
    # Optional: omit the [sharepoint] section entirely for two-way
    # To Do <-> Skylight sync (the ledger is the system of record).
    sharepoint: SharePointConfig | None = None
    todo: TodoConfig = Field(default_factory=TodoConfig)
    grocery: GroceryConfig = Field(default_factory=GroceryConfig)
    skylight: SkylightConfig
    sync: SyncConfig = Field(default_factory=SyncConfig)
    mapping: MappingConfig = Field(default_factory=MappingConfig)

    # Directory containing config.toml; relative paths resolve against it.
    base_dir: Path = Path(".")

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path)


def _scan_for_secrets(raw: dict, path: str = "") -> None:
    for k, v in raw.items():
        where = f"{path}.{k}" if path else k
        if isinstance(v, dict):
            _scan_for_secrets(v, where)
        elif any(bad in k.lower() for bad in FORBIDDEN_KEYS) and isinstance(v, str) and v:
            raise ConfigError(
                f"config key '{where}' looks like a secret. Secrets must be seeded via "
                f"'python -m skysync.secrets set <name>' (DPAPI), never stored in config.toml."
            )


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p} (copy config.example.toml to config.toml)")
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    _scan_for_secrets(raw)
    try:
        cfg = AppConfig(**raw)
    except Exception as exc:  # pydantic ValidationError -> friendly message
        raise ConfigError(f"invalid config {p}: {exc}") from exc
    cfg.base_dir = p.resolve().parent
    return cfg
