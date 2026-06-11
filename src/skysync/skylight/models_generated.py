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
    """A {id, type} pair used inside 'relationships' payloads."""
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
