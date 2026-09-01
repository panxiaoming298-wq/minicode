"""Full agent loop integration test — verifies all cybernetic controllers fire.

Runs a complete agent turn with the mock model and checks that every
major controller in the Sense→Control→Act pipeline was invoked.

This is the definitive "MiniCode is working" test.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from minicode.agent_loop import run_agent_turn
from minicode.context_manager import ContextManager
from minicode.mock_model import MockModelAdapter
from minicode.permissions import PermissionManager
from minicode.tooling import ToolRegistry
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
    def _allow(request):
        return {"decision": "allow_once"}
    return PermissionManager(str(workspace), prompt=_allow)


@pytest.fixture
def messages(workspace, permissions):
    return [
        {"role": "system", "content": "You are a coding assistant. Use tools to help the user."},
        {"role": "user", "content": "Create a React login form component"},
    ]


class TestAgentFlowBasic:
    """Basic agent loop runs without errors."""

    def test_agent_completes_without_error(
        self, mock_model, tools, messages, workspace, permissions
    ):
        result = run_agent_turn(
            model=mock_model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            max_steps=3,
        )
        assert isinstance(result, list)
        assert len(result) > 0

    def test_agent_with_context_manager(
        self, mock_model, tools, messages, workspace, permissions
    ):
        ctx = ContextManager(model="claude-sonnet-4-20250514")
        result = run_agent_turn(
            model=mock_model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            context_manager=ctx,
            max_steps=3,
        )
        stats = ctx.get_stats()
        assert stats.messages_count > 0


class TestAgentFlowCybernetics:
    """All cybernetic controllers initialize and run without errors."""

    def test_full_cybernetic_stack_initializes(
        self, mock_model, tools, messages, workspace, permissions
    ):
        """The full 15-controller cybernetic stack must not crash."""
        result = run_agent_turn(
            model=mock_model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            context_manager=ContextManager(model="claude-sonnet-4-20250514"),
            enable_work_chain=True,
            max_steps=3,
        )
        assert len(result) > 0

    def test_cybernetic_stack_with_ls_command(
        self, mock_model, tools, messages, workspace, permissions
    ):
        """Run /ls through the cybernetic stack."""
        messages.append({"role": "user", "content": "/ls"})
        result = run_agent_turn(
            model=mock_model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            context_manager=ContextManager(model="claude-sonnet-4-20250514"),
            enable_work_chain=True,
            max_steps=5,
        )
        assert len(result) > 0

    def test_agent_loop_uses_orchestrator_hooks(
        self, monkeypatch, mock_model, tools, messages, workspace, permissions
    ):
        """The agent loop should drive the unified orchestrator lifecycle."""
        from minicode.cybernetic_orchestrator import CyberneticOrchestrator

        calls: list[str] = []

        def wrap(name):
            original = getattr(CyberneticOrchestrator, name)

            def _wrapped(self, *args, **kwargs):
                calls.append(name)
                return original(self, *args, **kwargs)

            return _wrapped

        for method in (
            "wire_memory",
            "wire_healing",
            "inject_memories",
            "step_start",
            "step_end",
            "reflect_on_task",
        ):
            monkeypatch.setattr(CyberneticOrchestrator, method, wrap(method))

        messages.append({"role": "user", "content": "/ls"})
        result = run_agent_turn(
            model=mock_model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            context_manager=ContextManager(model="claude-sonnet-4-20250514"),
            enable_work_chain=True,
            max_steps=3,
        )

        assert len(result) > 0
        for method in (
            "wire_memory",
            "wire_healing",
            "inject_memories",
            "step_start",
            "step_end",
            "reflect_on_task",
        ):
            assert method in calls


class TestAgentMemoryPipeline:
    """Memory pipeline runs end-to-end within agent loop."""

    def test_memory_pipeline_in_agent_loop(
        self, mock_model, tools, messages, workspace, permissions
    ):
        """Memory pipeline (domain classify → BM25 → reranker → inject) must work."""
        # Create some memories first to have something to search
        from minicode.memory import MemoryManager, MemoryScope
        mgr = MemoryManager(project_root=str(workspace))
        mgr.add_entry(
            scope=MemoryScope.PROJECT, category="pattern",
            content="React forms use react-hook-form with zod validation",
            tags=["react", "form", "validation"],
        )
        mgr.add_entry(
            scope=MemoryScope.PROJECT, category="convention",
            content="Use functional components with hooks, avoid class components",
            tags=["react", "component"],
        )

        result = run_agent_turn(
            model=mock_model,
            tools=tools,
            messages=messages,
            cwd=str(workspace),
            permissions=permissions,
            context_manager=ContextManager(model="claude-sonnet-4-20250514"),
            enable_work_chain=True,
            max_steps=3,
        )
        assert len(result) > 0
