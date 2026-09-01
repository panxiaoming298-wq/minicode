"""Adversarial / robustness tests for the Python-specific compaction subsystems.

These don't mirror a TS oracle (these modules are re-architected) — instead they
throw varied/malformed inputs and check invariants, to surface latent crashes.
"""

from __future__ import annotations

import pytest

from minicode.context_compactor import ToolResultBudgetManager
from minicode.micro_compact import MicroCompactor, MicroCompactorConfig


# ---------------------------------------------------------------------------
# ToolResultBudgetManager: must never crash on any content shape
# ---------------------------------------------------------------------------


_ADVERSARIAL_CONTENTS = [
    None,
    "",
    "   \n\t ",
    0,
    False,
    [],
    {},
    ["x" * 6000],            # list whose str() form is large
    {"k": "v" * 6000},       # dict whose str() form is large
    "normal small",
    "Z" * 6000,              # large string (persisted)
    "\x00\x01\x02binary-ish",
    "中文内容" * 1000,        # CJK
]


@pytest.mark.parametrize("content", _ADVERSARIAL_CONTENTS)
def test_check_and_replace_never_crashes_on_content_shapes(content, tmp_path) -> None:
    mgr = ToolResultBudgetManager(workspace=str(tmp_path), persist_threshold=1000)
    messages = [{"role": "tool_result", "toolName": "run_command", "content": content, "toolUseId": "x"}]
    modified, saved = mgr.check_and_replace(messages)  # must not raise
    assert isinstance(modified, list)
    assert len(modified) == 1
    assert isinstance(saved, int)


def test_check_and_replace_handles_message_without_content_key(tmp_path) -> None:
    mgr = ToolResultBudgetManager(workspace=str(tmp_path))
    messages = [{"role": "tool_result", "toolName": "run_command", "toolUseId": "x"}]  # no "content"
    modified, _ = mgr.check_and_replace(messages)
    assert modified[0]["content"] == ""  # missing -> normalized to ""


def test_check_and_replace_preserves_non_tool_messages(tmp_path) -> None:
    mgr = ToolResultBudgetManager(workspace=str(tmp_path), persist_threshold=10)
    messages = [
        {"role": "user", "content": "X" * 9000},
        {"role": "assistant", "content": [{"type": "text", "text": "Y" * 9000}]},
        {"role": "system", "content": "Z" * 9000},
    ]
    modified, saved = mgr.check_and_replace(messages)
    assert saved == 0  # only tool_result messages are candidates
    assert modified[0]["content"] == "X" * 9000
    assert modified[2]["content"] == "Z" * 9000


# ---------------------------------------------------------------------------
# MicroCompactor: compact() must never crash and preserve invariants
# ---------------------------------------------------------------------------


def _mc() -> MicroCompactor:
    return MicroCompactor(
        config=MicroCompactorConfig(
            tool_result_budget_tokens=50,
            keep_recent_groups=1,
            idle_threshold_seconds=0,  # always idle -> exercises time-based path
            time_based_enabled=True,
            budget_based_enabled=True,
        )
    )


def test_micro_compact_never_crashes_on_malformed_messages() -> None:
    mc = _mc()
    malformed_lists = [
        [],
        [{}],
        [{"role": None}],
        [{"role": "tool_result"}],                       # no content, no data
        [{"role": "tool_result", "content": None}],
        [{"role": "tool_use"}],                          # no data
        [{"role": "assistant", "content": None}],
        [{"role": "tool_result", "data": None, "content": "x" * 9999}],
        [{"role": "tool_result", "data": {}, "content": "x" * 9999}],
        [{"role": "weird_role", "content": "x" * 9999}],
    ]
    for msgs in malformed_lists:
        result, stats = mc.compact(msgs, current_time=1e9)  # must not raise
        assert isinstance(result, list)
        # compact never increases message count
        assert len(result) <= len(msgs)


def test_micro_compact_never_drops_non_tool_messages() -> None:
    mc = _mc()
    msgs = [
        {"role": "user", "content": "keep me"},
        {"role": "assistant", "content": "a0"},
        {"role": "tool_use", "data": {"tool_name": "read_file", "tool_id": "1"}},
        {"role": "tool_result", "data": {"tool_name": "read_file", "tool_id": "1"}, "content": "x" * 9999},
        {"role": "assistant", "content": "a1"},
    ]
    result, _ = mc.compact(msgs, current_time=1e9)
    # user + assistant messages must always survive
    roles = [m.get("role") for m in result]
    assert "user" in roles
    assert roles.count("assistant") == 2


def test_micro_compact_idempotent_second_run_no_crash() -> None:
    """Running compact on already-compacted output must be stable."""
    mc = _mc()
    msgs = [
        {"role": "assistant", "content": "a0"},
        {"role": "tool_result", "data": {"tool_name": "read_file", "tool_id": "1"}, "content": "x" * 9999},
        {"role": "assistant", "content": "a1"},
    ]
    once, _ = mc.compact(msgs, current_time=1e9)
    twice, _ = mc.compact(once, current_time=1e9)  # must not raise / not grow
    assert len(twice) <= len(once)
