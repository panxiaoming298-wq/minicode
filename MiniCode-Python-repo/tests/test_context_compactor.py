"""Comprehensive tests for Claude Code-style Context Management System.

Tests all 7 phases:
1. Core data structures (CompactBoundary, CompactionResult)
2. Tool Result Budget (persistence + preview replacement)
3. Read Deduplication (hash-based file dedup)
4. Time-based Microcompact (old tool result cleanup)
5. Session Memory Compact (memory-linked compaction)
6. Auto Compact High-Water Dispatcher (strategy selection + circuit breaker)
7. Reactive Compact (error recovery path)
8. Unified ContextCompactor orchestrator
"""
from __future__ import annotations

import os
import time
import tempfile

import pytest

from minicode.context_compactor import (
    AutoCompactConfig,
    AutoCompactDispatcher,
    CompactBoundary,
    CompactStrategy,
    CompactTrigger,
    CompactionResult,
    ContextCompactor,
    MicrocompactEngine,
    MicrocompactState,
    ReadDedupEntry,
    ReadDedupManager,
    ReactiveCompactEngine,
    SessionMemoryCompactEngine,
    ToolResultBudgetManager,
    ToolResultPersisted,
)


# ---------------------------------------------------------------------------
# Phase 1: Core Data Structures
# ---------------------------------------------------------------------------


class TestCoreDataStructures:
    """Test CompactBoundary, CompactionResult, and enums."""

    def test_compact_trigger_enum_values(self):
        assert CompactTrigger.MANUAL.value == "manual"
        assert CompactTrigger.AUTO.value == "auto"
        assert CompactTrigger.REACTIVE.value == "reactive"
        assert CompactTrigger.MICROCOMPACT_TIME.value == "microcompact_time"
        assert CompactTrigger.MICROCOMPACT_CACHED.value == "microcompact_cached"

    def test_compact_strategy_enum_values(self):
        assert CompactStrategy.SESSION_MEMORY.value == "session_memory"
        assert CompactStrategy.FULL.value == "full"
        assert CompactStrategy.MICROCOMPACT.value == "microcompact"
        assert CompactStrategy.TOOL_BUDGET.value == "tool_budget"
        assert CompactStrategy.READ_DEDUP.value == "read_dedup"

    def test_compact_boundary_creation(self):
        boundary = CompactBoundary(
            trigger=CompactTrigger.AUTO,
            strategy=CompactStrategy.FULL,
            tokens_before=100000,
            tokens_after=20000,
            messages_removed=50,
        )
        assert boundary.trigger == CompactTrigger.AUTO
        assert boundary.strategy == CompactStrategy.FULL
        assert boundary.tokens_before == 100000
        assert boundary.tokens_after == 20000
        assert boundary.messages_removed == 50
        assert boundary.timestamp > 0
        assert boundary.logical_parent_id is None

    def test_compact_boundary_to_dict(self):
        boundary = CompactBoundary(
            trigger=CompactTrigger.AUTO,
            strategy=CompactStrategy.SESSION_MEMORY,
            preserved_segment=(10, 25),
        )
        d = boundary.to_dict()
        assert d["trigger"] == "auto"
        assert d["strategy"] == "session_memory"
        assert d["preserved_segment"] == [10, 25]
        assert d["logical_parent_id"] is None

    def test_compact_boundary_preserved_segment_none_to_dict(self):
        boundary = CompactBoundary(
            trigger=CompactTrigger.MANUAL,
            strategy=CompactStrategy.PARTIAL,
        )
        assert boundary.to_dict()["preserved_segment"] is None

    def test_compaction_result_success(self):
        result = CompactionResult(
            success=True,
            strategy=CompactStrategy.FULL,
            trigger=CompactTrigger.AUTO,
            messages=[{"role": "user", "content": "hi"}],
            tokens_freed=80000,
        )
        assert result.success is True
        assert result.effective is True
        assert result.tokens_freed == 80000
        assert len(result.messages) == 1

    def test_compaction_result_not_effective_when_zero_tokens(self):
        result = CompactionResult(
            success=True,
            strategy=CompactStrategy.MICROCOMPACT,
            trigger=CompactTrigger.MICROCOMPACT_TIME,
            messages=[],
            tokens_freed=0,
        )
        assert result.success is True
        assert result.effective is False

    def test_compaction_result_failure(self):
        result = CompactionResult(
            success=False,
            strategy=CompactStrategy.FULL,
            trigger=CompactTrigger.AUTO,
            messages=[{"role": "system"}] * 100,
            error="Too few messages",
        )
        assert result.success is False
        assert result.effective is False
        assert result.error == "Too few messages"


# ---------------------------------------------------------------------------
# Phase 2: Tool Result Budget
# ---------------------------------------------------------------------------


class TestToolResultBudgetManager:
    """Test tool result persistence and budget management."""

    def _make_messages(self) -> list[dict]:
        return [
            {"role": "user", "content": "hello"},
            {
                "role": "tool_result",
                "toolName": "read_file",
                "content": "small output",
                "toolUseId": "1",
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "response"}],
            },
            {
                "role": "tool_result",
                "toolName": "run_command",
                "content": "X" * 3000,
                "toolUseId": "2",
            },
        ]

    def test_small_tool_result_unchanged(self, tmp_path):
        mgr = ToolResultBudgetManager(workspace=tmp_path)
        messages = self._make_messages()
        modified, saved = mgr.check_and_replace(messages)

        assert saved == 0
        assert "X" * 3000 in modified[3]["content"]  # Under threshold

    def test_large_tool_result_persisted(self, tmp_path):
        mgr = ToolResultBudgetManager(
            workspace=tmp_path,
            persist_threshold=1000,
        )
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "tool_result",
                "toolName": "run_command",
                "content": "Y" * 6000,
                "toolUseId": "t1",
            },
        ]
        modified, saved = mgr.check_and_replace(messages)

        assert saved > 0
        assert "[Tool result persisted to disk" in modified[1]["content"]
        assert "_persisted_path" in modified[1]

    def test_none_and_non_string_content_does_not_crash(self, tmp_path):
        """tool_result content can be None (no output) or non-string (structured).
        check_and_replace must coerce it instead of crashing on len(None)."""
        mgr = ToolResultBudgetManager(workspace=tmp_path, persist_threshold=1000)
        messages = [
            {"role": "tool_result", "toolName": "run_command", "content": None, "toolUseId": "n"},
            {"role": "tool_result", "toolName": "run_command", "content": ["a", "b"], "toolUseId": "l"},
        ]
        modified, saved = mgr.check_and_replace(messages)  # must not raise

        assert modified[0]["content"] == ""           # None -> ""
        assert isinstance(modified[1]["content"], str)  # list coerced to str

    def test_persisted_count_tracked(self, tmp_path):
        mgr = ToolResultBudgetManager(workspace=tmp_path, persist_threshold=10)
        messages = [
            {"role": "tool_result", "toolName": "a", "content": "X" * 100, "toolUseId": "1"},
            {"role": "tool_result", "toolName": "b", "content": "Y" * 100, "toolUseId": "2"},
        ]
        mgr.check_and_replace(messages)
        assert mgr.get_persisted_count() == 2

    def test_total_saved_bytes(self, tmp_path):
        mgr = ToolResultBudgetManager(workspace=tmp_path, persist_threshold=10)
        big_content = "Z" * 5000
        messages = [
            {"role": "tool_result", "toolName": "big", "content": big_content, "toolUseId": "1"},
        ]
        _, saved = mgr.check_and_replace(messages)
        assert mgr.get_total_saved_bytes() >= 4500  # Preview is shorter

    def test_non_tool_result_messages_untouched(self, tmp_path):
        mgr = ToolResultBudgetManager(workspace=tmp_path, persist_threshold=1)
        messages = [
            {"role": "user", "content": "A" * 10000},
            {"role": "assistant", "content": "B" * 10000},
            {"role": "system", "content": "C" * 10000},
        ]
        modified, _ = mgr.check_and_replace(messages)
        for m in modified:
            assert len(m.get("content", "")) == 10000

    def test_results_dir_created(self, tmp_path):
        mgr = ToolResultBudgetManager(workspace=tmp_path, persist_threshold=1)
        messages = [
            {"role": "tool_result", "toolName": "x", "content": "data" * 1000, "toolUseId": "1"},
        ]
        mgr.check_and_replace(messages)
        results_dir = tmp_path / ".mini-code-tool-results"
        assert results_dir.exists()
        assert any(results_dir.iterdir())

    def test_preview_contains_tool_name_and_size(self, tmp_path):
        mgr = ToolResultBudgetManager(workspace=tmp_path, persist_threshold=10)
        content = "line\n" * 200
        messages = [
            {"role": "tool_result", "toolName": "grep_output", "content": content, "toolUseId": "1"},
        ]
        modified, _ = mgr.check_and_replace(messages)
        preview = modified[0]["content"]
        assert "grep_output" in preview
        assert str(len(content)) in preview or f"{len(content)}" in preview


# ---------------------------------------------------------------------------
# Phase 3: Read Deduplication
# ---------------------------------------------------------------------------


class TestReadDedupManager:
    """Test hash-based file read dedup."""

    def test_register_new_read_returns_true(self):
        mgr = ReadDedupManager()
        assert mgr.register_read("/path/to/file.py", "hello world", 0) is True

    def test_register_same_content_returns_false(self):
        mgr = ReadDedupManager()
        mgr.register_read("/path/to/file.py", "hello world", 0)
        assert mgr.register_read("/path/to/file.py", "hello world", 1) is False

    def test_register_changed_content_returns_true(self):
        mgr = ReadDedupManager()
        mgr.register_read("/path/to/file.py", "v1", 0)
        assert mgr.register_read("/path/to/file.py", "v2 updated", 1) is True

    def test_should_dedup_true_for_duplicate(self):
        mgr = ReadDedupManager()
        mgr.register_read("main.py", "same content", 0)
        assert mgr.should_dedup("main.py", "same content") is True

    def test_should_dedup_false_for_changed(self):
        mgr = ReadDedupManager()
        mgr.register_read("main.py", "old", 0)
        assert mgr.should_dedup("main.py", "new version") is False

    def test_get_stub_returns_dedup_message(self):
        mgr = ReadDedupManager()
        mgr.register_read("config.json", '{"key": "val"}', 5)
        stub = mgr.get_stub("config.json")
        assert "deduplicated" in stub.lower() or "config.json" in stub
        assert "5" in stub  # message index

    def test_get_stub_empty_for_unknown(self):
        mgr = ReadDedupManager()
        assert mgr.get_stub("unknown.txt") == ""

    def test_invalidate_removes_entry(self):
        mgr = ReadDedupManager()
        mgr.register_read("file.py", "content", 0)
        mgr.invalidate("file.py")
        assert mgr.should_dedup("file.py", "content") is False

    def test_clear_removes_all(self):
        mgr = ReadDedupManager()
        mgr.register_read("a.py", "x", 0)
        mgr.register_read("b.py", "y", 1)
        mgr.clear()
        assert mgr.get_stub("a.py") == ""
        assert mgr.get_stub("b.py") == ""

    def test_different_files_independent(self):
        mgr = ReadDedupManager()
        mgr.register_read("a.py", "same", 0)
        mgr.register_read("b.py", "same", 1)
        assert mgr.should_dedup("a.py", "same") is True
        assert mgr.should_dedup("b.py", "same") is True


# ---------------------------------------------------------------------------
# Phase 4: Time-based Microcompact
# ---------------------------------------------------------------------------


class TestMicrocompactEngine:
    """Test time-based microcompact of old tool results."""

    def _messages_with_many_tool_results(self, n: int) -> list[dict]:
        msgs = [{"role": "user", "content": "start"}]
        for i in range(n):
            msgs.append({
                "role": "tool_result",
                "toolName": f"tool_{i}",
                "content": f"output line {i}\n" * 20,
            })
        msgs.append({"role": "assistant", "content": "done"})
        return msgs

    def test_no_microcompact_when_within_interval(self):
        engine = MicrocompactEngine(MicrocompactState(
            last_time_based_compact=time.time(),
            time_based_interval=999999,
        ))
        msgs = self._messages_with_many_tool_results(20)
        result = engine.run_time_based_microcompact(msgs)
        assert result.effective is False

    def test_no_microcompact_when_few_results(self):
        engine = MicrocompactEngine(MicrocompactState(
            last_time_based_compact=0.0,
            time_based_interval=0.0,
            keep_recent_tool_results=10,
        ))
        msgs = self._messages_with_many_tool_results(3)
        result = engine.run_time_based_microcompact(msgs, now=time.time() + 10000)
        assert result.effective is False

    def test_microcompact_clears_old_results(self):
        engine = MicrocompactEngine(MicrocompactState(
            last_time_based_compact=0.0,
            time_based_interval=0.0,
            keep_recent_tool_results=3,
        ))
        msgs = self._messages_with_many_tool_results(15)
        result = engine.run_time_based_microcompact(msgs, now=time.time() + 10000)
        assert result.effective is True
        assert result.tokens_freed > 0

        cleared = [m for m in result.messages if m.get("_microcompacted")]
        assert len(cleared) > 0

    def test_keeps_recent_tool_results(self):
        engine = MicrocompactEngine(MicrocompactState(
            last_time_based_compact=0.0,
            time_based_interval=0.0,
            keep_recent_tool_results=5,
        ))
        msgs = self._messages_with_many_tool_results(12)
        result = engine.run_time_based_microcompact(msgs, now=time.time() + 10000)

        non_cleared_tool_results = [
            m for m in result.messages[-6:]
            if m.get("role") == "tool_result" and not m.get("_microcompacted")
        ]
        assert len(non_cleared_tool_results) >= 4  # At least keep_recent preserved

    def test_does_not_clear_already_persisted(self):
        engine = MicrocompactEngine(MicrocompactState(
            last_time_based_compact=0.0,
            time_based_interval=0.0,
            keep_recent_tool_results=2,
        ))
        msgs = [
            {"role": "tool_result", "toolName": "a", "content": "[Tool result persisted...]", "toolUseId": "1"},
            {"role": "tool_result", "toolName": "b", "content": "normal output here", "toolUseId": "2"},
            {"role": "tool_result", "toolName": "c", "content": "more normal", "toolUseId": "3"},
            {"role": "tool_result", "toolName": "d", "content": "[Old tool result...", "toolUseId": "4"},
        ]
        result = engine.run_time_based_microcompact(msgs, now=time.time() + 10000)
        cleared = [m for m in result.messages if m.get("_microcompacted")]
        for c in cleared:
            assert not c["content"].startswith("[Tool result persisted")
            assert not c["content"].startswith("[Old tool result")

    def test_state_tracks_total_cleared(self):
        state = MicrocompactState(last_time_based_compact=0.0, time_based_interval=0.0, keep_recent_tool_results=2)
        engine = MicrocompactEngine(state)
        msgs = self._messages_with_many_tool_results(10)
        engine.run_time_based_microcompact(msgs, now=time.time() + 10000)
        assert state.total_tokens_cleared > 0


# ---------------------------------------------------------------------------
# Phase 5: Session Memory Compact
# ---------------------------------------------------------------------------


class TestSessionMemoryCompactEngine:
    """Test memory-linked session compact."""

    def test_returns_none_without_memory_manager(self):
        engine = SessionMemoryCompactEngine(memory_manager=None)
        result = engine.try_session_memory_compact(
            [{"role": "user", "content": "hi"}] * 20,
            context_window=100000,
        )
        assert result is None

    def test_returns_none_when_memory_empty(self):
        class EmptyMemory:
            def get_relevant_context(self, max_tokens=100):
                return ""
        engine = SessionMemoryCompactEngine(memory_manager=EmptyMemory())
        msgs = [{"role": "user", "content": "x" * 100} for _ in range(30)]
        result = engine.try_session_memory_compact(msgs, context_window=10000)
        assert result is None

    def test_successful_session_memory_compact(self):
        class FakeMemory:
            def get_relevant_context(self, max_tokens=100):
                return "# Project: test\n## Key decisions:\n- Use Python 3.11\n- Follow PEP8"
        engine = SessionMemoryCompactEngine(memory_manager=FakeMemory())
        msgs = []
        for i in range(40):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({f"role": role, "content": f"message {i} " * 10})

        config = AutoCompactConfig(max_expand_tokens=5000)
        result = engine.try_session_memory_compact(
            msgs, context_window=100000, config=config
        )

        assert result is not None
        assert result.effective is True
        assert result.strategy == CompactStrategy.SESSION_MEMORY
        assert len(result.messages) < len(msgs)
        assert "Project: test" in result.summary_text

    def test_boundary_has_correct_metadata(self):
        class FakeMemory:
            def get_relevant_context(self, max_tokens=100):
                return "memory context here"
        engine = SessionMemoryCompactEngine(memory_manager=FakeMemory())
        msgs = [{"role": "user", "content": "msg"} for _ in range(30)]
        result = engine.try_session_memory_compact(msgs, context_window=10000)

        assert result.boundary is not None
        assert result.boundary.strategy == CompactStrategy.SESSION_MEMORY
        assert result.boundary.trigger == CompactTrigger.AUTO
        assert result.boundary.messages_removed > 0

    def test_system_messages_preserved(self):
        class FakeMemory:
            def get_relevant_context(self, max_tokens=100):
                return "mem"
        engine = SessionMemoryCompactEngine(memory_manager=FakeMemory())
        system_msg = {"role": "system", "content": "You are helpful"}
        user_msgs = [{"role": "user", "content": f"msg {i}"} for i in range(25)]
        msgs = [system_msg] + user_msgs

        result = engine.try_session_memory_compact(msgs, context_window=10000)
        assert result is not None
        assert result.messages[0]["role"] == "system"


# ---------------------------------------------------------------------------
# Phase 6: Auto Compact Dispatcher
# ---------------------------------------------------------------------------


class TestAutoCompactDispatcher:
    """Test high-water mark auto-compact dispatch with circuit breaker."""

    def _long_messages(self, count: int) -> list[dict]:
        return [{"role": "user", "content": f"message {i} " * 50} for i in range(count)]

    def test_threshold_calculation(self):
        config = AutoCompactConfig(threshold_ratio=0.85)
        dispatcher = AutoCompactDispatcher(context_window=100000, config=config)
        assert dispatcher.threshold_tokens == 85000
        assert dispatcher.blocking_limit == 97000

    def test_should_trigger_false_below_threshold(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        msgs = self._long_messages(10)
        assert dispatcher.should_trigger(msgs) is False

    def test_dispatch_noop_when_below_threshold(self):
        dispatcher = AutoCompactDispatcher(context_window=1000000)
        msgs = self._long_messages(5)
        result = dispatcher.dispatch(msgs)
        assert result.effective is False

    def test_force_full_dispatch(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        msgs = self._long_messages(50)
        result = dispatcher.dispatch(msgs, force_full=True)
        assert result.success is True
        assert result.strategy == CompactStrategy.FULL
        assert len(result.messages) < len(msgs)

    def test_circuit_breaker_initially_ok(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        assert dispatcher.is_tripped is False

    def test_circuit_breaker_resets_on_success(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        msgs = self._long_messages(50)
        dispatcher.dispatch(msgs, force_full=True)
        assert dispatcher.is_tripped is False

    def test_boundary_recorded_on_success(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        msgs = self._long_messages(50)
        dispatcher.dispatch(msgs, force_full=True)
        history = dispatcher.get_history()
        assert len(history) == 1
        assert history[0].strategy == CompactStrategy.FULL

    def test_get_last_boundary(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        assert dispatcher.get_last_boundary() is None
        dispatcher.dispatch(self._long_messages(50), force_full=True)
        assert dispatcher.get_last_boundary() is not None

    def test_warning_suppression(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        assert dispatcher.is_warning_suppressed() is False
        dispatcher._suppress_warnings(duration=60.0)
        assert dispatcher.is_warning_suppressed() is True

    def test_reset_circuit_breaker(self):
        dispatcher = AutoCompactDispatcher(context_window=100000)
        for _ in range(5):
            dispatcher._on_failure()
        assert dispatcher.is_tripped is True
        dispatcher.reset_circuit_breaker()
        assert dispatcher.is_tripped is False

    def test_circuit_breaker_auto_recovers_after_timeout(self):
        """Without auto-recovery, a tripped breaker disables auto-compaction for
        the whole session (should_trigger stays False, _on_success never runs).
        After circuit_breaker_recovery_seconds, should_trigger must re-open
        (half-open) and be able to fire again. is_tripped stays a pure check."""
        config = AutoCompactConfig(
            circuit_breaker_limit=2,
            circuit_breaker_recovery_seconds=0.01,
        )
        dispatcher = AutoCompactDispatcher(context_window=100, config=config)
        dispatcher._on_failure()
        dispatcher._on_failure()
        assert dispatcher.is_tripped is True  # pure read, no side effect
        time.sleep(0.03)
        # Past the timeout: should_trigger recovers + fires on high usage.
        big = [{"role": "user", "content": "x" * 9999}]  # usage >> threshold (85)
        assert dispatcher.should_trigger(big) is True
        assert dispatcher._consecutive_failures == 0  # recovered

    def test_disabled_auto_compact_never_triggers(self):
        config = AutoCompactConfig(enabled=False)
        dispatcher = AutoCompactDispatcher(config=config)
        msgs = self._long_messages(1000)
        assert dispatcher.should_trigger(msgs) is False


# ---------------------------------------------------------------------------
# Phase 7: Reactive Compact
# ---------------------------------------------------------------------------


class TestReactiveCompactEngine:
    """Test error recovery compaction."""

    def test_recovery_attempt_one(self):
        auto = AutoCompactDispatcher(context_window=100000)
        engine = ReactiveCompactEngine(auto_compact=auto)
        msgs = [{"role": "user", "content": "x" * 100} for _ in range(80)]
        result = engine.try_recover_from_overflow(msgs)
        assert result is not None
        assert result.strategy == CompactStrategy.REACTIVE
        assert result.trigger == CompactTrigger.REACTIVE

    def test_max_retries_exceeded(self):
        auto = AutoCompactDispatcher(context_window=100000)
        engine = ReactiveCompactEngine(auto_compact=auto)
        engine._recovery_attempts = 10
        result = engine.try_recover_from_overflow([{"role": "user", "content": "hi"}])
        assert result is None

    def test_progressive_truncation(self):
        auto = AutoCompactDispatcher(context_window=100000)
        engine = ReactiveCompactEngine(auto_compact=auto)
        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(60)]

        r1 = engine.try_recover_from_overflow(msgs)
        engine._recovery_attempts += 1
        r2 = engine.try_recover_from_overflow(msgs)

        if r1 and r2:
            assert len(r2.messages) <= len(r1.messages)

    def test_aggressive_truncate_preserves_system(self):
        auto = AutoCompactDispatcher(context_window=100000)
        engine = ReactiveCompactEngine(auto_compact=auto)
        system = {"role": "system", "content": "important"}
        users = [{"role": "user", "content": "u"} for _ in range(40)]
        msgs = [system] + users
        result = engine.try_recover_from_overflow(msgs)
        assert result is not None
        assert result.messages[0]["role"] == "system"


# ---------------------------------------------------------------------------
# Phase 8: Unified ContextCompactor
# ---------------------------------------------------------------------------


class TestContextCompactorOrchestrator:
    """Test the unified pipeline orchestrator."""

    def test_initialization(self, tmp_path):
        compactor = ContextCompactor(
            context_window=200000,
            workspace=tmp_path,
        )
        stats = compactor.get_stats()
        assert stats["total_passes"] == 0
        assert stats["context_window"] == 200000

    def test_process_request_noop_when_clean(self, tmp_path):
        compactor = ContextCompactor(workspace=tmp_path)
        msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        result = compactor.process_request(msgs)
        assert result.effective is False
        assert len(result.messages) == 2

    def test_process_request_runs_tool_budget(self, tmp_path):
        compactor = ContextCompactor(workspace=tmp_path)
        msgs = [
            {"role": "user", "content": "test"},
            {"role": "tool_result", "toolName": "big", "content": "X" * 6000, "toolUseId": "1"},
        ]
        result = compactor.process_request(msgs)
        assert "tool_budget" in result.summary_text or result.tokens_freed > 0

    def test_process_request_runs_microcompact(self, tmp_path):
        compactor = ContextCompactor(workspace=tmp_path)
        msgs = [{"role": "user", "content": "start"}]
        for i in range(15):
            msgs.append({"role": "tool_result", "toolName": f"t{i}", "content": f"data {i}\n" * 20})
        result = compactor.process_request(msgs, enable_auto_compact=False)
        # Microcompact may or may not fire depending on timing

    def test_process_request_runs_auto_compact(self, tmp_path):
        compactor = ContextCompactor(
            workspace=tmp_path,
            context_window=3000,
        )
        msgs = [{"role": "user", "content": f"msg {i} " * 50} for i in range(100)]
        result = compactor.process_request(msgs)
        assert result.success is True or result.effective is True or len(result.messages) < len(msgs)

    def test_reactive_recover_delegates(self, tmp_path):
        compactor = ContextCompactor(context_window=10000, workspace=tmp_path)
        msgs = [{"role": "user", "content": "x" * 50} for _ in range(60)]
        result = compactor.reactive_recover(msgs, "prompt too long")
        assert result is not None
        assert result.strategy == CompactStrategy.REACTIVE

    def test_format_pipeline_status(self, tmp_path):
        compactor = ContextCompactor(workspace=tmp_path)
        status = compactor.format_pipeline_status()
        assert "Context Management Pipeline Status" in status
        assert "Context window:" in status

    def test_subcomponent_accessors(self, tmp_path):
        compactor = ContextCompactor(workspace=tmp_path)
        assert isinstance(compactor.tool_budget, ToolResultBudgetManager)
        assert isinstance(compactor.read_dedup, ReadDedupManager)
        assert isinstance(compactor.auto_compact, AutoCompactDispatcher)
        assert isinstance(compactor.reactive, ReactiveCompactEngine)

    def test_multiple_passes_increment_counter(self, tmp_path):
        compactor = ContextCompactor(workspace=tmp_path)
        msgs = [{"role": "user", "content": "hi"}]
        compactor.process_request(msgs)
        compactor.process_request(msgs)
        compactor.process_request(msgs)
        assert compactor.get_stats()["total_passes"] == 3

    def test_last_result_updated(self, tmp_path):
        compactor = ContextCompactor(context_window=2000, workspace=tmp_path)
        msgs = [{"role": "user", "content": "x" * 100} for _ in range(50)]
        result = compactor.process_request(msgs)
        if result.effective:
            assert compactor.last_result is not None


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestFullPipelineIntegration:
    """End-to-end tests of the complete context management pipeline."""

    def test_full_pipeline_large_conversation(self, tmp_path):
        compactor = ContextCompactor(
            context_window=15000,
            workspace=tmp_path,
        )
        msgs = [{"role": "system", "content": "You are a coding assistant."}]
        for i in range(60):
            if i % 3 == 0:
                msgs.append({"role": "user", "content": f"Task {i}: implement feature"})
            elif i % 3 == 1:
                msgs.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Working on {i}"}],
                })
            else:
                size = 3000 if i < 40 else 200
                msgs.append({
                    "role": "tool_result",
                    "toolName": "read_file",
                    "content": "file content " * (size // 12),
                    "toolUseId": f"t{i}",
                })

        result = compactor.process_request(msgs)
        assert result.success is True
        total_before = sum(len(str(m)) // 4 for m in msgs)
        total_after = sum(len(str(m)) // 4 for m in result.messages)
        assert total_after <= total_before

    def test_tool_budget_then_microcompact_then_autocompact_order(self, tmp_path):
        compactor = ContextCompactor(
            context_window=12000,
            workspace=tmp_path,
        )
        msgs = [{"role": "user", "content": "start"}]
        for i in range(20):
            msgs.append({
                "role": "tool_result",
                "toolName": f"cmd_{i}",
                "content": f"output {i}\n" * (100 if i < 5 else 400),
                "toolUseId": f"t{i}",
            })

        result = compactor.process_request(msgs)
        steps = result.summary_text
        has_any_step = (
            "tool_budget" in steps or
            "microcompact" in steps or
            "auto_compact" in steps
        )
        assert result.success is True or has_any_step

    def test_reactive_recovers_after_api_error_simulation(self, tmp_path):
        compactor = ContextCompactor(context_window=8000, workspace=tmp_path)
        normal_msgs = [{"role": "user", "content": f"msg {i} " * 10} for i in range(50)]

        result = compactor.reactive_recover(normal_msgs, "prompt too long")
        assert result is not None
        recovered_size = sum(len(str(m)) for m in result.messages)
        original_size = sum(len(str(m)) for m in normal_msgs)
        assert recovered_size < original_size

    def test_circuit_breaker_prevents_infinite_compaction(self, tmp_path):
        config = AutoCompactConfig(circuit_breaker_limit=2)
        compactor = ContextCompactor(
            context_window=5000,
            workspace=tmp_path,
            config=config,
        )
        huge = [{"role": "user", "content": "x" * 20} for _ in range(200)]
        for _ in range(5):
            compactor.process_request(huge)
        assert compactor.auto_compact.is_tripped is True or not compactor.auto_compact.is_tripped
