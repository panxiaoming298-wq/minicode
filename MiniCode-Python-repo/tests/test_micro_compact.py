"""Tests for minicode.micro_compact — lightweight tool result trimming."""

from __future__ import annotations

import time

import pytest

from minicode.micro_compact import (
    COMPRESSIBLE_READ_ONLY_TOOLS,
    MicroCompactionStats,
    MicroCompactor,
    MicroCompactorConfig,
    get_micro_compactor,
    micro_compact,
)


def _make_assistant(content: str = "hello") -> dict:
    return {"role": "assistant", "content": content}


def _make_tool_use(name: str, tool_id: str = "id1") -> dict:
    return {"role": "tool_use", "data": {"tool_name": name, "tool_id": tool_id}}


def _make_tool_result(name: str, content: str = "result data here", tool_id: str = "id1") -> dict:
    return {
        "role": "tool_result",
        "content": content,
        "data": {"tool_name": name, "tool_id": tool_id},
    }


# ── Basic compact (no action) ────────────────────────────────────────────

class TestNoAction:
    def test_empty_messages(self) -> None:
        mc = MicroCompactor()
        msgs, stats = mc.compact([])
        assert msgs == []
        assert stats.messages_before == 0

    def test_below_threshold(self) -> None:
        mc = MicroCompactor()
        msgs = [_make_assistant(), _make_tool_use("read_file"), _make_tool_result("read_file", "short")]
        result, stats = mc.compact(msgs)
        assert len(result) == 3
        assert stats.reason == "no_action"


# ── Time-based compaction ─────────────────────────────────────────────────

class TestTimeBased:
    def test_idle_triggers(self) -> None:
        mc = MicroCompactor(
            config=MicroCompactorConfig(
                keep_recent_groups=1,
                idle_threshold_seconds=1,
                budget_based_enabled=False,
            )
        )
        # Build 3 assistant groups, each with compressible tool results
        msgs = [
            _make_assistant("a1"),
            _make_tool_use("read_file", "id_a1"),
            _make_tool_result("read_file", "old data", "id_a1"),
            _make_assistant("a2"),
            _make_tool_use("web_search", "id_a2"),
            _make_tool_result("web_search", "search result", "id_a2"),
            _make_assistant("a3"),
            _make_tool_use("read_file", "id_a3"),
            _make_tool_result("read_file", "new data", "id_a3"),
        ]

        # Simulate idle
        result, stats = mc.compact(msgs, current_time=time.time() + 3600)
        assert stats.reason.startswith("time_based")
        # Only the last keep_recent_groups (1) should be preserved
        assert len(result) < len(msgs)
        # The last assistant group should remain
        assert result[-3:] == msgs[-3:]

    def test_not_idle_skips(self) -> None:
        mc = MicroCompactor(
            config=MicroCompactorConfig(
                keep_recent_groups=1,
                idle_threshold_seconds=3600,
                budget_based_enabled=False,
            )
        )
        msgs = [
            _make_assistant("a1"),
            _make_tool_use("read_file", "id1"),
            _make_tool_result("read_file", "data", "id1"),
        ]
        result, stats = mc.compact(msgs, current_time=time.time())
        assert stats.reason == "no_action"
        assert len(result) == 3

    def test_non_compressible_tools_preserved(self) -> None:
        mc = MicroCompactor(
            config=MicroCompactorConfig(
                keep_recent_groups=1,
                idle_threshold_seconds=1,
                budget_based_enabled=False,
            )
        )
        msgs = [
            _make_assistant("a1"),
            _make_tool_use("todo_write", "id1"),
            _make_tool_result("todo_write", "priority task", "id1"),
            _make_assistant("a2"),
            _make_tool_use("read_file", "id2"),
            _make_tool_result("read_file", "data", "id2"),
        ]
        result, stats = mc.compact(msgs, current_time=time.time() + 3600)
        # todo_write tools should be preserved
        has_todo = any("todo_write" in str(m.get("data", {}).get("tool_name", ""))
                       for m in result)
        assert has_todo, "todo_write should not be compressed"


# ── Budget-based compaction ───────────────────────────────────────────────

class TestBudgetBased:
    def test_exceeding_budget_trims_oldest(self) -> None:
        mc = MicroCompactor(
            config=MicroCompactorConfig(
                tool_result_budget_tokens=100,  # very tight
                keep_recent_groups=2,
                time_based_enabled=False,
            )
        )
        # Create many compressible results to exceed budget
        msgs = []
        for i in range(20):
            msgs.append(_make_assistant(f"a{i}"))
            msgs.append(_make_tool_use("read_file", f"id_a{i}"))
            msgs.append(_make_tool_result("read_file", "x" * 200, f"id_a{i}"))
        result, stats = mc.compact(msgs)
        assert stats.reason == "budget_exceeded"
        assert len(result) < len(msgs)

    def test_under_budget_skips(self) -> None:
        mc = MicroCompactor(
            config=MicroCompactorConfig(
                tool_result_budget_tokens=100_000,
            )
        )
        msgs = [
            _make_assistant(),
            _make_tool_use("read_file"),
            _make_tool_result("read_file", "tiny"),
        ]
        result, stats = mc.compact(msgs)
        assert stats.reason == "no_action"

    def test_budget_trims_oldest_and_keeps_newer(self) -> None:
        """When over budget, the OLDEST compressible results are trimmed and the
        more-recent ones are kept — and compaction must actually fire (not return
        no_action) whenever the total exceeds the budget."""
        mc = MicroCompactor(
            config=MicroCompactorConfig(
                tool_result_budget_tokens=60,  # fits one ~50-token result
                keep_recent_groups=1,
                time_based_enabled=False,
            )
        )
        msgs = [
            _make_assistant("a0"),
            _make_tool_use("read_file", "old"),
            _make_tool_result("read_file", "OLD-" + "x" * 200, "old"),
            _make_assistant("a1"),
            _make_tool_use("read_file", "new"),
            _make_tool_result("read_file", "NEW-" + "y" * 200, "new"),
            _make_assistant("a2"),  # protected (last group)
            _make_tool_use("read_file", "prot"),
            _make_tool_result("read_file", "P-" + "z" * 200, "prot"),
        ]
        result, stats = mc.compact(msgs)
        assert stats.reason == "budget_exceeded"
        bodies = [str(m.get("content", "")) for m in result]
        assert any("NEW-" in b for b in bodies), "newer old result should be kept"
        assert not any("OLD-" in b for b in bodies), "oldest result should be trimmed"


# ── Compressible tool detection ───────────────────────────────────────────

class TestCompressibleDetection:
    def test_read_only_tools_are_compressible(self) -> None:
        mc = MicroCompactor()
        for tool_name in ["read_file", "web_search", "grep_files", "list_files"]:
            msg = _make_tool_result(tool_name, "data")
            assert mc._is_compressible(msg), f"{tool_name} should be compressible"

    def test_non_compressible_tools_protected(self) -> None:
        mc = MicroCompactor()
        for tool_name in ["todo_write", "task", "memory"]:
            msg = _make_tool_result(tool_name, "data")
            assert not mc._is_compressible(msg), f"{tool_name} should NOT be compressible"


# ── Singleton ─────────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_micro_compactor_returns_same(self) -> None:
        m1 = get_micro_compactor()
        m2 = get_micro_compactor()
        assert m1 is m2

    def test_micro_compact_convenience(self) -> None:
        msgs, stats = micro_compact([])
        assert msgs == []
        assert isinstance(stats, MicroCompactionStats)
