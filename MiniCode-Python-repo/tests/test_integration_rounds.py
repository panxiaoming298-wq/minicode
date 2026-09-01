"""Integration tests: exercise the fixed subsystems end-to-end together.

Each "round" drives a realistic flow through the real default tool registry +
agent loop / session / memory / mcp / compaction / prompt, with the fixes from
this session in place. Run repeatedly to check stability (no flakes, no
regressions across the bug-fix batch).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from minicode.agent_loop import run_agent_turn
from minicode.context_compactor import ToolResultBudgetManager
from minicode.context_manager import ContextManager, compute_context_stats
from minicode.headless import _make_auto_approve_prompt
from minicode.memory import MemoryEntry, MemoryManager, MemoryScope
from minicode.mcp import create_mcp_backed_tools
from minicode.micro_compact import MicroCompactor, MicroCompactorConfig
from minicode.permissions import PermissionManager
from minicode.prompt import build_system_prompt_bundle
from minicode.session import create_new_session, load_session, save_session
from minicode.tools import create_default_tool_registry
from minicode.tooling import ToolContext
from minicode.types import AgentStep, ModelAdapter, ChatMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ScriptedModel(ModelAdapter):
    def __init__(self, steps: list[AgentStep]) -> None:
        self._steps = steps
        self.calls = 0

    def next(self, messages, on_stream_chunk=None, store=None):
        step = self._steps[self.calls] if self.calls < len(self._steps) else AgentStep(type="assistant", content="(done)")
        self.calls += 1
        return step


def _tools(cwd):
    return create_default_tool_registry(str(cwd), runtime={"model": "mock"})


def _perm_allow(cwd):
    return PermissionManager(str(cwd), prompt=_make_auto_approve_prompt())


def _tr(name, body, tid):
    return {"role": "tool_result", "content": body, "data": {"tool_name": name, "tool_id": tid}}


# ---------------------------------------------------------------------------
# Round 1: multi-turn write -> read -> edit, with session save/resume
# ---------------------------------------------------------------------------


def test_round1_multi_turn_edits_and_session_resume(tmp_path):
    tools = _tools(tmp_path)
    perm = _perm_allow(tmp_path)
    target = tmp_path / "r1.txt"

    # Turn 1: write
    m1 = ScriptedModel([
        AgentStep(type="tool_calls", calls=[{"id": "1", "toolName": "write_file", "input": {"path": str(target), "content": "v1\n"}}]),
        AgentStep(type="assistant", content="wrote v1"),
    ])
    run_agent_turn(model=m1, tools=tools, messages=[{"role": "system", "content": "s"}], cwd=str(tmp_path), permissions=perm, runtime={"model": "mock"})
    assert target.read_text(encoding="utf-8") == "v1\n"

    # Turn 2: read it back
    m2 = ScriptedModel([
        AgentStep(type="tool_calls", calls=[{"id": "2", "toolName": "read_file", "input": {"path": str(target)}}]),
        AgentStep(type="assistant", content="read it"),
    ])
    res = run_agent_turn(model=m2, tools=tools, messages=[{"role": "system", "content": "s"}], cwd=str(tmp_path), permissions=perm, runtime={"model": "mock"})
    assert any("v1" in (m.get("content") or "") for m in res if m.get("role") == "tool_result")

    # Session round-trip (session update_metadata must not crash on any content)
    s = create_new_session(str(tmp_path))
    s.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}]  # None content
    save_session(s)
    loaded = load_session(s.session_id)
    assert loaded is not None and loaded.messages[0]["content"] == "hi"
    tools.dispose()


# ---------------------------------------------------------------------------
# Round 2: MCP-backed tool (spawn + protocol path that the npx fix touched)
# ---------------------------------------------------------------------------


def test_round2_mcp_echo_end_to_end(tmp_path):
    fake = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
    mcp = create_mcp_backed_tools(cwd=str(tmp_path), mcp_servers={
        "fake": {"command": "python", "args": [str(fake)], "protocol": "newline-json"}})
    echo = next((t for t in mcp["tools"] if t.name == "mcp__fake__echo"), None)
    assert echo is not None, [t.name for t in mcp["tools"]]
    result = echo.run({"text": "integration"}, ToolContext(cwd=str(tmp_path), permissions=None, session=None))
    assert result.ok and result.output == "echo:integration"
    mcp["dispose"]()


# ---------------------------------------------------------------------------
# Round 3: memory add (incl. None content) + retrieval
# ---------------------------------------------------------------------------


def test_round3_memory_with_none_content(tmp_path):
    mgr = MemoryManager(project_root=tmp_path)
    mf = mgr.memories[MemoryScope.PROJECT]
    mf.entries.append(MemoryEntry(id="bad", content=None, scope=MemoryScope.PROJECT, category="c"))
    mf.entries.append(MemoryEntry(id="good", content="how to configure logging level", scope=MemoryScope.PROJECT, category="convention"))
    results = mgr.search("logging level")  # must not crash on None entry
    assert any("logging" in e.content for e in results)


# ---------------------------------------------------------------------------
# Round 4: microcompactor budget compaction trims oldest (the fixed behavior)
# ---------------------------------------------------------------------------


def test_round4_microcompactor_budget_trims_oldest():
    def tr(name, body, tid):
        return {"role": "tool_result", "content": body, "data": {"tool_name": name, "tool_id": tid}}
    def tu(name, tid):
        return {"role": "tool_use", "data": {"tool_name": name, "tool_id": tid}}
    def a(t):
        return {"role": "assistant", "content": t}

    mc = MicroCompactor(config=MicroCompactorConfig(
        tool_result_budget_tokens=60, keep_recent_groups=1, time_based_enabled=False))
    msgs = [
        a("a0"), tu("read_file", "old"), tr("read_file", "OLD-" + "x" * 200, "old"),
        a("a1"), tu("read_file", "new"), tr("read_file", "NEW-" + "y" * 200, "new"),
        a("a2"), tu("read_file", "prot"), tr("read_file", "P-" + "z" * 200, "prot"),
    ]
    result, stats = mc.compact(msgs)
    bodies = [str(m.get("content", "")) for m in result]
    assert stats.reason == "budget_exceeded"
    assert any("NEW-" in b for b in bodies)      # newer kept
    assert not any("OLD-" in b for b in bodies)  # oldest trimmed (was reversed before fix)


# ---------------------------------------------------------------------------
# Round 5: model switch updates context window + warning level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,window", [
    ("claude-opus-4-6", 200_000),
    ("gpt-4o", 128_000),
    ("deepseek-chat", 128_000),
    ("gemini-2.5-pro", 1_048_576),
])
def test_round5_model_switch_context_window(model, window):
    cm = ContextManager(model="claude-sonnet-4-6")
    cm.update_model(model)
    assert cm.context_window == window
    # compute_context_stats should not crash and reflect the window
    stats = compute_context_stats([{"role": "user", "content": "x" * 600_000}], model)
    assert stats["context_window"] == window
    assert 0.0 <= stats["utilization"] <= 1.0


# ---------------------------------------------------------------------------
# Round 6: prompt build with malformed MCP / None permission (no crash)
# ---------------------------------------------------------------------------


def test_round6_prompt_builder_robust_to_malformed_inputs():
    extras = {
        "mcpServers": [
            {"name": "broken", "status": "error"},          # missing toolCount
            "not-a-dict",                                     # wholly malformed
        ],
        "skills": [{"name": "s"}],                           # missing description
        "memory_context": "",
        "runtime": {},
    }
    bundle = build_system_prompt_bundle(".", ["ok", None, "x"], extras)  # None in permission_summary
    assert isinstance(bundle.prompt, str) and bundle.prompt
    assert "ok" in bundle.prompt


# ---------------------------------------------------------------------------
# Round 7: ToolResultBudgetManager with None + large content (no crash)
# ---------------------------------------------------------------------------


def test_round7_tool_result_budget_none_and_large(tmp_path):
    mgr = ToolResultBudgetManager(workspace=str(tmp_path), persist_threshold=1000)
    msgs = [
        {"role": "tool_result", "toolName": "run_command", "content": None, "toolUseId": "n"},
        {"role": "tool_result", "toolName": "read_file", "content": "Z" * 6000, "toolUseId": "l"},
        {"role": "tool_result", "toolName": "grep_files", "content": ["a", "b"], "toolUseId": "x"},
    ]
    modified, saved = mgr.check_and_replace(msgs)  # must not raise
    assert modified[0]["content"] == ""           # None coerced
    assert saved > 0                              # large persisted
    assert "[Tool result persisted to disk" in modified[1]["content"]
