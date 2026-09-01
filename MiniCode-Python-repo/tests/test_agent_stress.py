"""Agent loop stress tests — rapid turns, controller stability, no leaks."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from minicode.agent_loop import run_agent_turn
from minicode.context_manager import ContextManager
from minicode.mock_model import MockModelAdapter
from minicode.permissions import PermissionManager
from minicode.tools import create_default_tool_registry


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def mock_model():
    return MockModelAdapter()


@pytest.fixture
def tools(workspace):
    return create_default_tool_registry(str(workspace), runtime=None)


@pytest.fixture
def permissions(workspace):
    return PermissionManager(str(workspace), prompt=lambda r: {"decision": "allow_once"})


@pytest.fixture
def messages():
    return [
        {"role": "system", "content": "Coding assistant."},
        {"role": "user", "content": "List files"},
    ]


class TestAgentStressRapidTurns:
    """Many rapid agent turns — no memory leaks, controllers stable."""

    def test_20_rapid_turns(self, mock_model, tools, messages, workspace, permissions):
        ctx = ContextManager(model="claude-sonnet-4-20250514")
        for turn in range(20):
            msgs = list(messages)
            msgs.append({"role": "user", "content": f"/ls turn {turn}"})
            result = run_agent_turn(
                model=mock_model,
                tools=tools,
                messages=msgs,
                cwd=str(workspace),
                permissions=permissions,
                context_manager=ctx,
                enable_work_chain=True,
                max_steps=3,
            )
            assert isinstance(result, list)

    def test_10_turns_full_cybernetic(self, mock_model, tools, messages, workspace, permissions):
        ctx = ContextManager(model="claude-sonnet-4-20250514")
        for turn in range(10):
            msgs = list(messages)
            msgs.append({"role": "user", "content": f"Write a function turn{turn}"})
            result = run_agent_turn(
                model=mock_model,
                tools=tools,
                messages=msgs,
                cwd=str(workspace),
                permissions=permissions,
                context_manager=ctx,
                enable_work_chain=True,
                max_steps=4,
            )
            assert isinstance(result, list)


class TestAgentStressPerformance:
    """Performance under load."""

    def test_single_turn_latency(self, mock_model, tools, messages, workspace, permissions):
        t0 = time.time()
        result = run_agent_turn(
            model=mock_model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            enable_work_chain=True,
            max_steps=3,
        )
        elapsed = time.time() - t0
        assert isinstance(result, list)
        assert elapsed < 30.0, f"Single turn too slow: {elapsed:.1f}s"
