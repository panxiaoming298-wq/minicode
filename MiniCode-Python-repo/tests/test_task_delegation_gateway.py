from __future__ import annotations

import ast
from pathlib import Path

from minicode.tooling import ToolContext, ToolRegistry
from minicode.tools.task import task_tool
from minicode.unified_runtime import run_runtime_turn


def test_task_source_cannot_import_or_call_agent_loop() -> None:
    source_path = Path(__file__).parents[1] / "minicode" / "tools" / "task.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = []
    called_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.append(node.func.id)
    assert "minicode.agent_loop" not in imported
    assert "run_agent_turn" not in called_names


def test_standard_task_registers_child_attempt_and_stays_serial(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    task_outputs: list[str] = []

    def fake_agent_loop(**kwargs):
        node_id = kwargs["runtime"]["_runtimeNodeId"]
        calls.append(node_id)
        if node_id == "root":
            result = kwargs["tools"].execute(
                "task",
                {
                    "description": "inspect child",
                    "prompt": "inspect one independent fact",
                    "agent_type": "explore",
                },
                ToolContext(
                    cwd=kwargs["cwd"],
                    runtime_context=kwargs["runtime"]["_runtimeContext"],
                    node_id=node_id,
                    attempt_id=kwargs["runtime"]["_runtimeAttemptId"],
                ),
            )
            assert result.ok
            task_outputs.append(result.output)
            return [*kwargs["messages"], {"role": "assistant", "content": "parent used child"}]
        return [*kwargs["messages"], {"role": "assistant", "content": "child evidence"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {
        "executionProfile": "standard",
        "runtimeV2": True,
        "model": "fake",
        "_workerModelFactory": lambda tools: object(),
    }
    run_runtime_turn(
        model=object(),
        tools=ToolRegistry([task_tool]),
        messages=[{"role": "user", "content": "delegate once"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    trace = runtime["lastRuntimeTrace"]
    delegated = [node_id for node_id in trace["state"]["nodes"] if node_id.startswith("delegated-")]
    assert calls[0] == "root"
    assert len(delegated) == 1
    assert calls[1] == delegated[0]
    assert trace["scheduler"]["claimed_attempts"] == 2
    assert trace["scheduler"]["peak_active_workers"] == 1
    assert trace["state"]["nodes"][delegated[0]]["state"] == "SUCCEEDED"
    assert "Artifact" not in task_outputs[0] or "sha256=" in task_outputs[0]


def test_ultra_task_dynamic_delegation_is_denied(tmp_path) -> None:
    class Gateway:
        run_id = "run-1"
        max_delegation_depth = 1

        def submit(self, request):
            from minicode.runtime_protocol import AgentResult, AgentResultStatus, ExecutionFailure, ExecutionFailureKind
            return AgentResult(
                run_id=request.run_id,
                plan_version=1,
                node_id=request.parent_node_id,
                attempt_id="denied",
                status=AgentResultStatus.FAILED_TERMINAL,
                base_revision="rev",
                output_summary="dynamic Worker delegation is disabled for this profile",
                failure=ExecutionFailure(ExecutionFailureKind.POLICY_DENIED, "disabled"),
            )

    result = task_tool.run(
        {"description": "x", "prompt": "x", "agent_type": "explore"},
        ToolContext(cwd=str(tmp_path), runtime_context=Gateway(), node_id="explorer-1"),
    )
    assert not result.ok
    assert "disabled" in result.output


def test_standard_nested_task_cannot_exceed_delegation_depth(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    nested_outputs: list[str] = []

    def invoke_task(kwargs, *, agent_type: str):
        return kwargs["tools"].execute(
            "task",
            {
                "description": "nested",
                "prompt": "must remain bounded",
                "agent_type": agent_type,
            },
            ToolContext(
                cwd=kwargs["cwd"],
                runtime_context=kwargs["runtime"]["_runtimeContext"],
                node_id=kwargs["runtime"]["_runtimeNodeId"],
                attempt_id=kwargs["runtime"]["_runtimeAttemptId"],
            ),
        )

    def fake_agent_loop(**kwargs):
        node_id = kwargs["runtime"]["_runtimeNodeId"]
        calls.append(node_id)
        if node_id == "root":
            assert invoke_task(kwargs, agent_type="general").ok
            content = "parent complete"
        else:
            nested = invoke_task(kwargs, agent_type="general")
            nested_outputs.append(nested.output)
            assert not nested.ok
            content = "child observed bounded delegation"
        return [*kwargs["messages"], {"role": "assistant", "content": content}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {
        "executionProfile": "standard",
        "runtimeV2": True,
        "_workerModelFactory": lambda tools: object(),
    }
    run_runtime_turn(
        model=object(),
        tools=ToolRegistry([task_tool]),
        messages=[{"role": "user", "content": "delegate within one level"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    assert len(calls) == 2
    assert calls[0] == "root"
    assert calls[1].startswith("delegated-")
    assert "max_delegation_depth exceeded" in nested_outputs[0]
