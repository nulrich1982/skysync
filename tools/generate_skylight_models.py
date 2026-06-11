"""Generator: parses the vendored Skylight OpenAPI YAML and emits
src/skysync/skylight/models_generated.py with curated pydantic v2 models.

Regenerate with:
    python tools/generate_skylight_models.py

The generator asserts that expected field names exist in the spec schemas so
that spec updates that rename fields cause regeneration to fail loudly rather
than silently producing stale models.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "src" / "skysync" / "skylight" / "spec" / "skylight-openapi.yaml"
OUT_PATH = REPO_ROOT / "src" / "skysync" / "skylight" / "models_generated.py"


def load_spec() -> dict:
    with open(SPEC_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_schema(spec: dict, path: str, method: str) -> dict:
    """Return the 200/201 response JSON schema for a given path+method."""
    path_item = spec["paths"].get(path)
    if path_item is None:
        # Try alternate paths (e.g. the HAR artifact for create_multiple)
        for k in spec["paths"]:
            # The HAR-derived spec writes "/chores/{choreId}reate_multiple" —
            # the 'c' of create_multiple was eaten by the path parameter.
            if "reate_multiple" in k and path.endswith("create_multiple"):
                path_item = spec["paths"][k]
                break
    if path_item is None:
        raise KeyError(f"path not found in spec: {path!r}")
    op = path_item.get(method)
    if op is None:
        raise KeyError(f"method {method!r} not found at {path!r}")
    for code in ("200", "201"):
        resp = op.get("responses", {}).get(code)
        if resp:
            return resp["content"]["application/json"]["schema"]
    raise KeyError(f"no 200/201 response schema at {method.upper()} {path}")


def assert_field(schema: dict, *keys: str, context: str = "") -> None:
    """Walk nested schema keys, asserting each exists; raise with context if missing."""
    current = schema
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            raise AssertionError(
                f"Spec drift detected: key path {keys!r} missing at step {k!r} "
                f"(context: {context}). Regenerate after updating spec manually."
            )
        current = current[k]


def validate_chores_schema(schema: dict) -> None:
    """Assert expected fields exist in the chores GET response schema."""
    data_item = schema["properties"]["data"]["items"]["properties"]
    assert_field(data_item, "attributes", context="chores data item")
    attrs = data_item["attributes"]["properties"]
    for field in ("summary", "status", "recurring", "routine", "completed_on",
                  "start", "start_time", "recurrence_set", "recurring_until",
                  "reward_points", "position", "emoji_icon", "group", "id"):
        assert_field(attrs, field, context=f"ChoreAttributes.{field}")
    assert_field(data_item, "id", context="Chore.id")
    assert_field(data_item, "type", context="Chore.type")
    assert_field(data_item, "relationships", context="Chore.relationships")


def validate_categories_schema(schema: dict) -> None:
    """Assert expected fields exist in the categories GET response schema."""
    data_item = schema["properties"]["data"]["items"]["properties"]
    attrs = data_item["attributes"]["properties"]
    for field in ("id", "label", "color", "linked_to_profile", "selected_for_chore_chart"):
        assert_field(attrs, field, context=f"CategoryAttributes.{field}")
    assert_field(data_item, "id", context="Category.id")
    assert_field(data_item, "type", context="Category.type")


def validate_lists_schema(schema: dict) -> None:
    """Assert expected fields exist in the lists GET response schema."""
    data_item = schema["properties"]["data"]["items"]["properties"]
    attrs = data_item["attributes"]["properties"]
    for field in ("color", "label", "kind"):
        assert_field(attrs, field, context=f"ListAttributes.{field}")
    # Included list_items
    incl = schema["properties"]["included"]["items"]["properties"]
    ia = incl["attributes"]["properties"]
    for field in ("id", "label", "status", "position", "section", "created_at"):
        assert_field(ia, field, context=f"ListItemAttributes.{field}")


def validate_list_item_post_schema(schema: dict) -> None:
    """Assert expected fields in POST list_items response."""
    attrs = schema["properties"]["data"]["properties"]["attributes"]["properties"]
    for field in ("id", "label", "status", "position", "section", "created_at"):
        assert_field(attrs, field, context=f"ListItemAttributes.{field}")


def validate_sessions_schema(schema: dict) -> None:
    """Assert token field exists in sessions POST response."""
    attrs = schema["properties"]["data"]["properties"]["attributes"]["properties"]
    assert_field(attrs, "token", context="SessionData.token")
    assert_field(schema["properties"]["data"]["properties"], "id", context="SessionData.id")


def run() -> None:
    spec = load_spec()

    # Load and validate schemas for each operation
    chores_get_schema = get_schema(spec, "/api/frames/{id}/chores", "get")
    validate_chores_schema(chores_get_schema)

    chore_put_schema = get_schema(spec, "/api/frames/{id}/chores/{id1}", "put")
    # Single chore response — check attributes present
    assert "data" in chore_put_schema["properties"], "ChoreResponse.data missing"

    categories_schema = get_schema(spec, "/api/frames/{id}/categories", "get")
    validate_categories_schema(categories_schema)

    lists_schema = get_schema(spec, "/api/frames/{id}/lists", "get")
    validate_lists_schema(lists_schema)

    list_item_post_schema = get_schema(
        spec, "/api/frames/{id}/lists/{id1}/list_items", "post"
    )
    validate_list_item_post_schema(list_item_post_schema)

    list_item_put_schema = get_schema(
        spec, "/api/frames/{id}/lists/{id1}/list_items/{id2}", "put"
    )
    validate_list_item_post_schema(list_item_put_schema)

    sessions_schema = get_schema(spec, "/api/sessions", "post")
    validate_sessions_schema(sessions_schema)

    # Also validate create_multiple path
    cm_schema = get_schema(spec, "/api/frames/{id}/chores/create_multiple", "post")
    assert "data" in cm_schema["properties"], "ChoresResponse.data missing in create_multiple"

    print(f"All spec assertions passed. Writing {OUT_PATH}")
    OUT_PATH.write_text(GENERATED_SOURCE, encoding="utf-8")
    print("Done.")


GENERATED_SOURCE = '''\
# GENERATED — do not edit by hand.
# Source: src/skysync/skylight/spec/skylight-openapi.yaml (vendored, unofficial)
# Regenerate with:  python tools/generate_skylight_models.py
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Shared building block
# ---------------------------------------------------------------------------


class RelationshipRef(BaseModel):
    """A {id, type} pair used inside \'relationships\' payloads."""
    model_config = ConfigDict(extra="allow")

    id: str
    type: str


# ---------------------------------------------------------------------------
# Chore models
# ---------------------------------------------------------------------------


class ChoreAttributes(BaseModel):
    """Core fields from the chore attributes sub-object.

    extra="allow" so additive spec drift is tolerated; required core fields
    are declared explicitly and validated strictly.
    """
    model_config = ConfigDict(extra="allow")

    id: int | str
    summary: str
    status: str
    completed_on: date | None = None
    start: date | None = None
    start_time: str | None = None
    recurring: bool = False
    routine: bool = False
    recurrence_set: list[str] | None = None
    recurring_until: date | str | None = None
    reward_points: int | None = None
    position: int | None = None
    emoji_icon: str | None = None
    group: str | int | None = None


class Chore(BaseModel):
    """JSON:API resource object for a Skylight chore."""
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    attributes: ChoreAttributes
    # relationships is optional (absent on some DELETE responses)
    relationships: dict[str, Any] | None = None

    @property
    def category_id(self) -> str | None:
        """Return the category id from relationships.category.data.id, if present."""
        if not self.relationships:
            return None
        cat = self.relationships.get("category")
        if not cat:
            return None
        data = cat.get("data") if isinstance(cat, dict) else None
        if not data:
            return None
        return str(data.get("id")) if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Category models
# ---------------------------------------------------------------------------


class CategoryAttributes(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | str
    label: str
    color: str | None = None
    linked_to_profile: bool = False
    selected_for_chore_chart: bool | None = None


class Category(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    attributes: CategoryAttributes


# ---------------------------------------------------------------------------
# List / ListItem models
# ---------------------------------------------------------------------------


class ListAttributes(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    color: str | None = None
    kind: str | None = None
    default_grocery_list: bool = False
    hide_on_device: bool = False


class SkylightList(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    attributes: ListAttributes


class ListItemAttributes(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | str
    label: str
    status: str | None = None
    position: int | None = None
    section: str | None = None
    created_at: datetime | None = None


class ListItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    attributes: ListItemAttributes


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------


class SessionAttributes(BaseModel):
    model_config = ConfigDict(extra="allow")

    token: str
    email: str | None = None
    subscription_status: str | None = None


class SessionData(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    attributes: SessionAttributes


# ---------------------------------------------------------------------------
# Envelope models (API response wrappers)
# ---------------------------------------------------------------------------


class ChoresResponse(BaseModel):
    """Envelope for list-of-chores responses (GET chores, POST create_multiple)."""
    model_config = ConfigDict(extra="allow")

    data: list[Chore]
    included: list[Category] = []


class ChoreResponse(BaseModel):
    """Envelope for single-chore responses (PUT chore)."""
    model_config = ConfigDict(extra="allow")

    data: Chore
    included: list[Category] = []


class CategoriesResponse(BaseModel):
    """Envelope for GET categories."""
    model_config = ConfigDict(extra="allow")

    data: list[Category]


class ListsResponse(BaseModel):
    """Envelope for GET lists (data=lists, included=list_items)."""
    model_config = ConfigDict(extra="allow")

    data: list[SkylightList]
    included: list[ListItem] = []


class ListItemResponse(BaseModel):
    """Envelope for POST/PUT list_items (single item)."""
    model_config = ConfigDict(extra="allow")

    data: ListItem


class SessionResponse(BaseModel):
    """Envelope for POST sessions."""
    model_config = ConfigDict(extra="allow")

    data: SessionData
'''


if __name__ == "__main__":
    run()
