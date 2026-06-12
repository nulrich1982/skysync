"""Grocery pairing: Skylight LIST <-> To Do list via a second engine instance.

Covers the SkylightListTaskClient adapter against a stub API, and the engine
semantics specific to this pairing: dateless items (no due-date stamping),
unwindowed fetch (absence IS a delete), and mirror deletes both ways.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from skysync.engine import SyncEngine, SyncPolicy
from skysync.errors import ConfigError
from skysync.ledger import Ledger
from skysync.mock_client import InMemoryTaskClient
from skysync.models import CanonicalTask, RemoteTask
from skysync.skylight.list_adapter import SkylightListTaskClient


# ------------------------------------------------------------- stub API ----


def _item(item_id: str, label: str, status: str = "pending") -> SimpleNamespace:
    return SimpleNamespace(id=item_id, attributes=SimpleNamespace(label=label, status=status))


class StubSkylightApi:
    """Just enough of SkylightApi for the list adapter."""

    def __init__(self, list_label: str = "Grocery List"):
        self._ids = itertools.count(1)
        self.list_id = "498886"
        self.list_label = list_label
        self.items: dict[str, SimpleNamespace] = {}

    def get_lists(self):
        return [SimpleNamespace(id=self.list_id, attributes=SimpleNamespace(label=self.list_label))]

    def get_list_items(self, list_id: str):
        assert list_id == self.list_id
        return list(self.items.values())

    def create_list_item(self, list_id: str, label: str):
        item = _item(f"li-{next(self._ids)}", label)
        self.items[item.id] = item
        return item

    def update_list_item(self, list_id: str, item_id: str, *, label: str | None = None, status: str | None = None):
        item = self.items[item_id]
        if label is not None:
            item.attributes.label = label
        if status is not None:
            item.attributes.status = status
        return item

    def delete_list_item(self, list_id: str, item_id: str) -> None:
        self.items.pop(item_id, None)


# -------------------------------------------------------- adapter tests ----


def test_adapter_resolves_list_case_insensitively_and_missing_raises():
    api = StubSkylightApi(list_label="Grocery List")
    client = SkylightListTaskClient(api, "grocery list")
    assert client.list_tasks() == []
    with pytest.raises(ConfigError, match="Shopping"):
        SkylightListTaskClient(api, "Shopping").list_tasks()


def test_adapter_parses_and_writes_status_values():
    api = StubSkylightApi()
    api.items["li-9"] = _item("li-9", "  Milk ", status="completed")
    client = SkylightListTaskClient(api, "Grocery List")
    [rt] = client.list_tasks()
    assert rt.task.title == "Milk" and rt.task.status == "completed"

    created = client.create_task(CanonicalTask(title="Eggs", status="completed"), "iid-1")
    assert api.items[created.remote_id].attributes.status == "completed"
    client.update_task(created.remote_id, CanonicalTask(title="Eggs", status="open"), "iid-1")
    assert api.items[created.remote_id].attributes.status == "pending"  # upstream uncheck value


def test_adapter_projection_is_title_and_status_only():
    client = SkylightListTaskClient(StubSkylightApi(), "Grocery List")
    t = CanonicalTask(title="Bread", notes="sourdough", assignee="avery", status="open")
    assert client.project(t) == {"title": "Bread", "status": "open"}
    assert client.supports(t)


# ----------------------------------------------------- engine pairing ------


@dataclass
class GroceryEnv:
    todo: InMemoryTaskClient
    sky: SkylightListTaskClient
    api: StubSkylightApi
    ledger: Ledger
    policy: SyncPolicy = field(
        default_factory=lambda: SyncPolicy(
            deletes_todo_to_skylight=True,
            deletes_skylight_to_todo=True,  # mirror_deletes
            undated_due_today=False,
            sky_absence_trusted=True,
        )
    )

    def run(self):
        return SyncEngine(self.ledger, {"todo": self.todo, "skylight": self.sky}, self.policy).run()

    def assert_converged(self) -> None:
        self.todo.write_log.clear()
        before = {k: (v.attributes.label, v.attributes.status) for k, v in self.api.items.items()}
        self.run()
        after = {k: (v.attributes.label, v.attributes.status) for k, v in self.api.items.items()}
        assert self.todo.write_log == [] and before == after, "grocery pairing not converged"


def make_grocery_env() -> GroceryEnv:
    api = StubSkylightApi()
    return GroceryEnv(
        todo=InMemoryTaskClient("todo", mapped_assignees={}),
        sky=SkylightListTaskClient(api, "Grocery List"),
        api=api,
        ledger=Ledger(":memory:"),
    )


def grocery(title: str, status: str = "open") -> CanonicalTask:
    return CanonicalTask(title=title, status=status)  # type: ignore[arg-type]


def test_todo_item_appears_on_frame_and_back():
    env = make_grocery_env()
    env.todo.seed(grocery("Milk"))
    env.run()
    assert [i.attributes.label for i in env.api.items.values()] == ["Milk"]
    env.assert_converged()
    # and the reverse direction
    env.api.create_list_item(env.api.list_id, "Bananas")
    env.run()
    titles = {s.task.title for s in env.todo.items.values()}
    assert titles == {"Milk", "Bananas"}
    env.assert_converged()


def test_no_due_date_is_ever_stamped():
    env = make_grocery_env()
    env.todo.seed(grocery("Oat milk"))
    env.run()
    assert next(iter(env.todo.items.values())).task.due_date is None
    env.assert_converged()


def test_check_off_on_frame_completes_todo():
    env = make_grocery_env()
    env.todo.seed(grocery("Cucumbers"))
    env.run()
    item_id = next(iter(env.api.items))
    env.api.update_list_item(env.api.list_id, item_id, status="completed")
    env.run()
    assert next(iter(env.todo.items.values())).task.status == "completed"
    env.assert_converged()


def test_delete_on_frame_mirrors_to_todo():
    env = make_grocery_env()
    env.todo.seed(grocery("Party food"))
    env.run()
    env.api.delete_list_item(env.api.list_id, next(iter(env.api.items)))
    env.run()
    assert env.todo.items == {}  # absence is trusted + mirror deletes
    rows = env.ledger.all_rows()
    assert len(rows) == 1 and rows[0].deleted
    env.assert_converged()


def test_delete_in_todo_mirrors_to_frame():
    env = make_grocery_env()
    env.api.create_list_item(env.api.list_id, "Fage")
    env.run()
    env.todo.user_delete(next(iter(env.todo.items)))
    env.run()
    assert env.api.items == {}
    env.assert_converged()


def test_notes_edit_in_todo_never_writes_the_frame():
    env = make_grocery_env()
    env.todo.seed(grocery("Frozen peaches"))
    env.run()
    env.todo.user_edit(next(iter(env.todo.items)), notes="the good brand")
    env.run()
    # canonical absorbed the note; frame untouched (it can't represent notes)
    assert [i.attributes.label for i in env.api.items.values()] == ["Frozen peaches"]
    env.assert_converged()
