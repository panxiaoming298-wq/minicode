"""Tests for minicode.background_tasks — task slot and lifecycle management."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import minicode.background_tasks as _bt
from minicode.background_tasks import (
    _refresh_record,
    can_start_new_task,
    check_completed_tasks,
    format_slot_status,
    get_background_task,
    get_slot_stats,
    list_background_tasks,
    register_background_shell_task,
    register_completion_callback,
    set_max_slots,
)


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Ensure global state is clean before each test."""
    _bt._background_tasks.clear()
    _bt._slot_callbacks.clear()
    _bt._max_slots = 5


# ---------------------------------------------------------------------------
# register_background_shell_task
# ---------------------------------------------------------------------------

class TestRegisterBackgroundShellTask:
    def test_creates_task_with_correct_fields(self) -> None:
        before = int(time.time() * 1000)
        result = register_background_shell_task("echo hello", pid=12345, cwd="/tmp")
        after = int(time.time() * 1000)

        assert result.taskId.startswith("task_")
        assert result.type == "local_bash"
        assert result.command == "echo hello"
        assert result.pid == 12345
        assert result.status == "running"
        assert before <= result.startedAt <= after

    def test_adds_task_to_registry(self) -> None:
        result = register_background_shell_task("cmd", pid=1, cwd=".")
        assert result.taskId in _bt._background_tasks
        record = _bt._background_tasks[result.taskId]
        assert record["command"] == "cmd"
        assert record["pid"] == 1
        assert record["status"] == "running"

    def test_label_truncates_long_commands(self) -> None:
        long_cmd = "x" * 100
        result = register_background_shell_task(long_cmd, pid=1, cwd=".")
        record = _bt._background_tasks[result.taskId]
        assert len(record["label"]) == 60

    def test_generates_unique_ids(self) -> None:
        r1 = register_background_shell_task("a", pid=1, cwd=".")
        r2 = register_background_shell_task("b", pid=2, cwd=".")
        assert r1.taskId != r2.taskId


# ---------------------------------------------------------------------------
# list_background_tasks / get_background_task
# ---------------------------------------------------------------------------

class TestListAndGet:
    def test_list_empty(self) -> None:
        assert list_background_tasks() == []

    def test_list_single(self) -> None:
        register_background_shell_task("cmd", pid=1, cwd=".")
        tasks = list_background_tasks()
        assert len(tasks) == 1
        assert tasks[0]["command"] == "cmd"

    def test_get_missing(self) -> None:
        assert get_background_task("nonexistent") is None

    def test_get_existing(self) -> None:
        r = register_background_shell_task("cmd", pid=1, cwd=".")
        task = get_background_task(r.taskId)
        assert task is not None
        assert task["taskId"] == r.taskId

    def test_refreshes_status_on_list(self) -> None:
        # Create a "completed" record directly and verify its status is preserved
        _bt._background_tasks["done"] = {
            "taskId": "done", "type": "local_bash",
            "command": "x", "pid": 9999, "status": "completed",
            "startedAt": 0, "label": "x",
        }
        # Mock _is_process_alive to return False (process gone) — but refresh
        # skips records already marked "completed", so status should stay.
        tasks = list_background_tasks()
        assert tasks[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# slot stats
# ---------------------------------------------------------------------------

class TestSlotStats:
    def test_empty_slots(self) -> None:
        stats = get_slot_stats()
        assert stats == {
            "used_slots": 0,
            "max_slots": 5,
            "available_slots": 5,
            "total_tracked": 0,
        }

    def test_with_running_task(self) -> None:
        register_background_shell_task("cmd", pid=1, cwd=".")
        stats = get_slot_stats()
        assert stats["used_slots"] == 1
        assert stats["available_slots"] == 4
        assert stats["total_tracked"] == 1

    def test_can_start_when_available(self) -> None:
        assert can_start_new_task() is True

    def test_cannot_start_when_full(self) -> None:
        for i in range(5):
            register_background_shell_task(f"cmd{i}", pid=i, cwd=".")
        assert can_start_new_task() is False


# ---------------------------------------------------------------------------
# set_max_slots
# ---------------------------------------------------------------------------

class TestSetMaxSlots:
    def test_increase_slots(self) -> None:
        set_max_slots(10)
        assert _bt._max_slots == 10

    def test_minimum_one(self) -> None:
        set_max_slots(0)
        assert _bt._max_slots == 1

    def test_slot_stats_updated(self) -> None:
        set_max_slots(3)
        for _ in range(3):
            register_background_shell_task("cmd", pid=1, cwd=".")
        stats = get_slot_stats()
        assert stats["max_slots"] == 3
        assert stats["available_slots"] == 0


# ---------------------------------------------------------------------------
# register_completion_callback / check_completed_tasks
# ---------------------------------------------------------------------------

class TestCompletionCallbacks:
    def test_register_callback(self) -> None:
        cb = lambda tid, rec: None
        register_completion_callback("task_1", cb)
        assert "task_1" in _bt._slot_callbacks

    def test_callback_fired_on_completed_task(self) -> None:
        fired: list[str] = []

        def cb(task_id: str, rec: dict) -> None:
            fired.append(task_id)

        # Use "running" status with a dead PID so _refresh_record marks it done
        _bt._background_tasks["task_x"] = {
            "taskId": "task_x", "type": "local_bash",
            "command": "x", "pid": 99999, "status": "running",
            "startedAt": 0, "label": "x",
        }
        register_completion_callback("task_x", cb)
        completed = check_completed_tasks()

        # Should be detected since dead pid transitions status
        assert "task_x" in completed
        assert fired == ["task_x"]
        # Callback consumed after firing
        assert "task_x" not in _bt._slot_callbacks

    def test_callback_error_does_not_block(self) -> None:
        def bad_cb(_task_id: str, _rec: dict) -> None:
            raise RuntimeError("callback error")

        _bt._background_tasks["task_y"] = {
            "taskId": "task_y", "type": "local_bash",
            "command": "y", "pid": 99999, "status": "running",
            "startedAt": 0, "label": "y",
        }
        register_completion_callback("task_y", bad_cb)
        # Should not raise
        check_completed_tasks()

    def test_multiple_callbacks(self) -> None:
        fired: list[str] = []

        _bt._background_tasks["task_a"] = {
            "taskId": "task_a", "type": "local_bash",
            "command": "a", "pid": 99999, "status": "running",
            "startedAt": 0, "label": "a",
        }
        register_completion_callback("task_a", lambda tid, r: fired.append("cb1"))
        register_completion_callback("task_a", lambda tid, r: fired.append("cb2"))
        check_completed_tasks()

        # Second registration overwrites first (dict behavior)
        assert fired == ["cb2"]


# ---------------------------------------------------------------------------
# format_slot_status
# ---------------------------------------------------------------------------

class TestFormatSlotStatus:
    def test_empty_display(self) -> None:
        output = format_slot_status()
        assert "Slots: 0/5 used" in output
        assert "Available: 5" in output
        assert "Total tracked: 0" in output

    def test_with_running_tasks(self) -> None:
        r = register_background_shell_task("echo hello world", pid=123, cwd=".")
        output = format_slot_status()
        assert "Slots: 1/5 used" in output
        assert r.taskId in output
        assert "echo hello world" in output


# ---------------------------------------------------------------------------
# _refresh_record edge cases
# ---------------------------------------------------------------------------

class TestRefreshRecord:
    def test_skips_non_running(self) -> None:
        rec = {"status": "completed", "pid": 1}
        result = _refresh_record(rec)
        assert result is rec
        assert result["status"] == "completed"

    def test_no_pid(self) -> None:
        rec = {"status": "running", "pid": None}
        result = _refresh_record(rec)
        assert result is rec

    def test_process_dead_marks_completed(self) -> None:
        rec = {"status": "running", "pid": 99999}
        # pid 99999 is almost certainly not alive
        result = _refresh_record(rec)
        assert result is rec
        assert result["status"] in ("completed", "failed")
