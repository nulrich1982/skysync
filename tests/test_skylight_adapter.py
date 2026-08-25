"""Tests for src/skysync/skylight/adapter.py."""
from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from skysync.errors import ConfigError, PermanentApiError
from skysync.models import CanonicalTask
from skysync.skylight.adapter import SkylightTaskClient
from skysync.skylight.models_generated import (
    Category,
    CategoryAttributes,
    Chore,
    ChoreAttributes,
    ChoresResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_category(cat_id: str, label: str, color: str = "#aabbcc") -> Category:
    return Category(
        id=cat_id,
        type="category",
        attributes=CategoryAttributes(
            id=int(cat_id),
            label=label,
            color=color,
            linked_to_profile=True,
            selected_for_chore_chart=True,
        ),
    )


def make_chore(
    chore_id: str,
    summary: str,
    status: str = "pending",
    category_id: str = "100",
    recurring: bool = False,
    routine: bool = False,
    start: str | None = "2025-12-29",
) -> Chore:
    return Chore(
        id=chore_id,
        type="chore",
        attributes=ChoreAttributes(
            id=int(chore_id) if chore_id.isdigit() else chore_id,
            summary=summary,
            status=status,
            completed_on=None,
            start=datetime.date.fromisoformat(start) if start else None,
            start_time=None,
            recurring=recurring,
            routine=routine,
            recurrence_set=None,
            recurring_until=None,
            reward_points=None,
            position=1,
            emoji_icon=None,
            group=chore_id,
        ),
        relationships={"category": {"data": {"id": category_id, "type": "category"}}},
    )


def make_adapter(
    categories: list[Category] | None = None,
    chores: list[Chore] | None = None,
    child_categories: dict[str, str] | None = None,
    sync_recurring: bool = False,
    today: datetime.date | None = None,
) -> SkylightTaskClient:
    if categories is None:
        categories = [
            make_category("100", "Kayla"),
            make_category("200", "Garrett"),
        ]
    if chores is None:
        chores = []
    if child_categories is None:
        child_categories = {"kayla": "Kayla", "garrett": "Garrett"}
    _today = today or datetime.date(2025, 12, 29)

    api = MagicMock()
    api.get_categories.return_value = categories
    api.get_chores.return_value = ChoresResponse(data=chores, included=categories)

    def fake_create_chore(summary: str, category_id: str, start: Any) -> Chore:
        return make_chore("999", summary, category_id=category_id, start=start.isoformat() if start else None)

    def fake_update_chore(chore_id: str, **kw: Any) -> Chore:
        status = kw.get("status", "pending")
        summary = kw.get("summary", "Updated")
        cat_id = kw.get("category_id", "100")
        return make_chore(chore_id, summary, status=status, category_id=cat_id)

    api.create_chore.side_effect = fake_create_chore
    api.update_chore.side_effect = fake_update_chore
    api.delete_chore.return_value = None

    adapter = SkylightTaskClient(
        api=api,
        child_categories=child_categories,
        window_past_days=14,
        window_future_days=60,
        sync_recurring=sync_recurring,
        today=lambda: _today,
    )
    return adapter


# ---------------------------------------------------------------------------
# Assignee resolution tests
# ---------------------------------------------------------------------------


class TestAssigneeResolution:
    def test_child_key_resolves_to_category(self) -> None:
        """child_key 'kayla' -> category id '100'."""
        adapter = make_adapter()
        cat_id = adapter._resolve_category_id("kayla")
        assert cat_id == "100"

    def test_child_key_case_insensitive(self) -> None:
        """'KAYLA' resolves the same as 'kayla'."""
        adapter = make_adapter()
        assert adapter._resolve_category_id("KAYLA") == "100"

    def test_configured_non_chore_chart_category_fails_loud(self) -> None:
        """A CONFIGURED mapping to a category with selected_for_chore_chart =
        False must raise ConfigError (chores there are invisible on the frame
        — observed live with a calendar category)."""
        cal = make_category("400", "Whole Family")
        cal.attributes.selected_for_chore_chart = False
        adapter = make_adapter(
            categories=[make_category("100", "Kayla"), cal],
            child_categories={"family": "Whole Family"},
        )
        with pytest.raises(ConfigError, match="chore chart"):
            adapter.supports(CanonicalTask(title="x", assignee="family"))

    def test_unmapped_label_to_non_chart_category_is_skipped_not_fatal(self) -> None:
        """A merely *incidental* assignee matching a non-chart category label
        resolves to None (task skipped for Skylight) rather than raising."""
        cal = make_category("400", "Holidays")
        cal.attributes.selected_for_chore_chart = False
        adapter = make_adapter(categories=[cal], child_categories={})
        assert adapter._resolve_category_id("holidays") is None
        assert not adapter.supports(CanonicalTask(title="x", assignee="holidays"))

    def test_unmapped_label_passthrough_lowercase(self) -> None:
        """An assignee not in child_categories but matching a category label resolves."""
        categories = [make_category("300", "Aunt Maria")]
        adapter = make_adapter(categories=categories, child_categories={})
        assert adapter._resolve_category_id("aunt maria") == "300"

    def test_unmapped_label_passthrough_mixed_case(self) -> None:
        """Case-insensitive match for direct label passthrough."""
        categories = [make_category("300", "Aunt Maria")]
        adapter = make_adapter(categories=categories, child_categories={})
        assert adapter._resolve_category_id("Aunt Maria") == "300"

    def test_unresolvable_returns_none(self) -> None:
        """Unknown assignee returns None."""
        adapter = make_adapter()
        result = adapter._resolve_category_id("nobody")
        assert result is None

    def test_configured_label_missing_from_frame_raises_config_error(self) -> None:
        """child_categories label not present in frame raises ConfigError."""
        adapter = make_adapter(
            categories=[make_category("100", "Kayla")],
            child_categories={"garrett": "Garrett"},  # Garrett not in frame
        )
        with pytest.raises(ConfigError, match="Garrett"):
            adapter._resolve_category_id("garrett")

    def test_reverse_map_child_key(self) -> None:
        """category_id that maps to a configured child label returns child_key."""
        adapter = make_adapter()
        assert adapter._category_id_to_assignee("100") == "kayla"

    def test_reverse_map_passthrough(self) -> None:
        """category_id not in child_categories returns lowercase label."""
        categories = [make_category("300", "Uncle Bob")]
        adapter = make_adapter(categories=categories, child_categories={})
        assert adapter._category_id_to_assignee("300") == "uncle bob"

    def test_reverse_map_unknown_id(self) -> None:
        """Unknown category_id returns None."""
        adapter = make_adapter()
        assert adapter._category_id_to_assignee("999") is None


# ---------------------------------------------------------------------------
# supports / representable tests
# ---------------------------------------------------------------------------


class TestSupportsAndRepresentable:
    def test_supports_resolvable_assignee(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(title="Test", assignee="kayla")
        assert adapter.supports(task) is True

    def test_supports_false_for_none_assignee(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(title="Test", assignee=None)
        assert adapter.supports(task) is False

    def test_supports_false_for_unresolvable(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(title="Test", assignee="nobody")
        assert adapter.supports(task) is False

    def test_representable_assignee_true(self) -> None:
        adapter = make_adapter()
        assert adapter.representable("assignee", "kayla") is True

    def test_representable_assignee_false_unresolvable(self) -> None:
        adapter = make_adapter()
        assert adapter.representable("assignee", "nobody") is False

    def test_representable_assignee_false_none(self) -> None:
        adapter = make_adapter()
        assert adapter.representable("assignee", None) is False

    def test_representable_other_fields_always_true(self) -> None:
        adapter = make_adapter()
        assert adapter.representable("title", "anything") is True
        assert adapter.representable("due_date", None) is True
        assert adapter.representable("status", "open") is True


# ---------------------------------------------------------------------------
# list_tasks tests
# ---------------------------------------------------------------------------


class TestListTasks:
    def test_list_tasks_returns_remote_tasks(self) -> None:
        chores = [make_chore("55900629", "Nail Trim", category_id="100")]
        adapter = make_adapter(chores=chores)
        tasks = adapter.list_tasks()
        assert len(tasks) == 1
        rt = tasks[0]
        assert rt.side == "skylight"
        assert rt.remote_id == "55900629"
        assert rt.task.title == "Nail Trim"
        assert rt.task.assignee == "kayla"
        assert rt.task.notes == ""
        assert rt.marker_internal_id is None
        assert rt.last_modified is None

    def test_recurring_chores_skipped_by_default(self) -> None:
        chores = [
            make_chore("1", "Non-recurring", recurring=False, routine=False),
            make_chore("2", "Recurring", recurring=True, routine=False),
            make_chore("3", "Routine", recurring=False, routine=True),
        ]
        adapter = make_adapter(chores=chores, sync_recurring=False)
        tasks = adapter.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].remote_id == "1"

    def test_recurring_chores_included_when_sync_recurring(self) -> None:
        chores = [
            make_chore("1", "Non-recurring", recurring=False, routine=False),
            make_chore("2", "Recurring", recurring=True, routine=False),
            make_chore("3", "Routine", recurring=False, routine=True),
        ]
        adapter = make_adapter(chores=chores, sync_recurring=True)
        tasks = adapter.list_tasks()
        assert len(tasks) == 3

    def test_status_complete_maps_to_completed(self) -> None:
        chores = [make_chore("1", "Done", status="complete")]
        adapter = make_adapter(chores=chores)
        tasks = adapter.list_tasks()
        assert tasks[0].task.status == "completed"

    def test_status_completed_maps_to_completed(self) -> None:
        chores = [make_chore("1", "Done", status="completed")]
        adapter = make_adapter(chores=chores)
        tasks = adapter.list_tasks()
        assert tasks[0].task.status == "completed"

    def test_status_pending_maps_to_open(self) -> None:
        chores = [make_chore("1", "Open", status="pending")]
        adapter = make_adapter(chores=chores)
        tasks = adapter.list_tasks()
        assert tasks[0].task.status == "open"

    def test_container_id_is_category_id(self) -> None:
        chores = [make_chore("1", "Test", category_id="200")]
        adapter = make_adapter(chores=chores)
        tasks = adapter.list_tasks()
        assert tasks[0].container_id == "200"

    def test_title_stripped(self) -> None:
        chores = [make_chore("1", "  Spaces  ")]
        adapter = make_adapter(chores=chores)
        tasks = adapter.list_tasks()
        assert tasks[0].task.title == "Spaces"


# ---------------------------------------------------------------------------
# create_task / update_task tests
# ---------------------------------------------------------------------------


class TestCreateUpdateTask:
    def test_create_task_calls_api(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(
            title="New Chore",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
        )
        rt = adapter.create_task(task, "internal-1")
        assert rt.remote_id == "999"
        assert rt.task.title == "New Chore"

    def test_create_task_immediately_completes_if_completed(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(
            title="Done Task",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
            status="completed",
        )
        rt = adapter.create_task(task, "internal-1")
        # update_chore should have been called to mark complete
        adapter._api.update_chore.assert_called_once_with("999", status="complete")

    def test_create_task_unresolvable_assignee_raises(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(title="Test", assignee="nobody")
        with pytest.raises(PermanentApiError):
            adapter.create_task(task, "internal-1")

    def test_update_task_splits_status_into_second_call(self) -> None:
        """Skylight rejects PUTs mixing completion status with other
        attributes (observed live) — attrs first, then status alone."""
        adapter = make_adapter()
        task = CanonicalTask(
            title="Done",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
            status="completed",
        )
        adapter.update_task("55900629", task, "internal-1")
        calls = adapter._api.update_chore.call_args_list
        assert len(calls) == 2
        assert "status" not in calls[0].kwargs  # attributes-only PUT
        assert calls[0].kwargs.get("summary") == "Done"
        assert calls[1].kwargs == {"status": "complete"}  # status-only PUT

    def test_update_task_skips_status_call_when_unchanged(self) -> None:
        adapter = make_adapter()  # fake update_chore returns status "pending"
        task = CanonicalTask(
            title="Open",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
            status="open",
        )
        adapter.update_task("55900629", task, "internal-1")
        calls = adapter._api.update_chore.call_args_list
        assert len(calls) == 1
        assert "status" not in calls[0].kwargs


# ---------------------------------------------------------------------------
# find_by_marker / find_recovery_candidate tests
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_find_by_marker_always_none(self) -> None:
        adapter = make_adapter()
        assert adapter.find_by_marker("any-id") is None

    def test_find_recovery_candidate_exact_single_match(self) -> None:
        chores = [
            make_chore("1", "My Task", category_id="100", start="2025-12-29"),
        ]
        adapter = make_adapter(chores=chores)
        task = CanonicalTask(
            title="My Task",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
        )
        result = adapter.find_recovery_candidate(task, "unused")
        assert result is not None
        assert result.remote_id == "1"

    def test_find_recovery_candidate_no_match_returns_none(self) -> None:
        chores = [make_chore("1", "Different Task", category_id="100")]
        adapter = make_adapter(chores=chores)
        task = CanonicalTask(
            title="My Task",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
        )
        assert adapter.find_recovery_candidate(task, "unused") is None

    def test_find_recovery_candidate_two_matches_returns_none(self) -> None:
        """Two candidates = ambiguous = None."""
        chores = [
            make_chore("1", "My Task", category_id="100", start="2025-12-29"),
            make_chore("2", "My Task", category_id="100", start="2025-12-29"),
        ]
        adapter = make_adapter(chores=chores)
        task = CanonicalTask(
            title="My Task",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
        )
        assert adapter.find_recovery_candidate(task, "unused") is None

    def test_find_recovery_candidate_assignee_mismatch_returns_none(self) -> None:
        chores = [make_chore("1", "My Task", category_id="200")]  # garrett, not kayla
        adapter = make_adapter(chores=chores)
        task = CanonicalTask(
            title="My Task",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),
        )
        assert adapter.find_recovery_candidate(task, "unused") is None

    def test_find_recovery_candidate_due_date_mismatch_returns_none(self) -> None:
        chores = [make_chore("1", "My Task", category_id="100", start="2025-12-30")]
        adapter = make_adapter(chores=chores)
        task = CanonicalTask(
            title="My Task",
            assignee="kayla",
            due_date=datetime.date(2025, 12, 29),  # different date
        )
        assert adapter.find_recovery_candidate(task, "unused") is None


# ---------------------------------------------------------------------------
# project tests
# ---------------------------------------------------------------------------


class TestProject:
    def test_project_includes_projection_fields(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(
            title="  Test  ",
            notes="some notes",
            due_date=datetime.date(2025, 12, 29),
            assignee="kayla",
            status="open",
        )
        proj = adapter.project(task)
        assert set(proj.keys()) == {"title", "due_date", "assignee", "status"}
        assert proj["title"] == "Test"  # stripped
        assert proj["due_date"] == "2025-12-29"
        assert proj["assignee"] == "kayla"
        assert proj["status"] == "open"
        assert "notes" not in proj

    def test_project_unresolvable_assignee_is_none(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(title="Test", assignee="nobody")
        proj = adapter.project(task)
        assert proj["assignee"] is None

    def test_project_none_due_date(self) -> None:
        adapter = make_adapter()
        task = CanonicalTask(title="Test", assignee="kayla", due_date=None)
        proj = adapter.project(task)
        assert proj["due_date"] is None
