from __future__ import annotations

from minicode.runtime_protocol import (
    AgentInvocationSpec,
    BudgetEnvelope,
    ContextManifest,
    ToolEffect,
)
from minicode.tooling import ToolDefinition, ToolRegistry, ToolResult
from minicode.worker_runtime import AgentLoopWorkerRunner


def test_ambiguous_never_auto_retry_tool_makes_worker_terminal(tmp_path, monkeypatch) -> None:
    calls = 0

    def fake_agent_loop(**kwargs):
        nonlocal calls
        calls += 1
        kwargs["on_tool_start"]("http_request", {"method": "POST"})
        raise RuntimeError("crash after remote side effect, before result persistence")

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    tool = ToolDefinition(
        name="http_request",
        description="external request",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=lambda value, context: ToolResult(ok=True, output="sent"),
    )
    runner = AgentLoopWorkerRunner(
        model=object(),
        tools=ToolRegistry([tool]),
        messages=[{"role": "user", "content": "post once"}],
        cwd=str(tmp_path),
        runtime={},
    )
    invocation = AgentInvocationSpec(
        run_id="run-1",
        plan_version=1,
        node_id="writer",
        attempt_id="attempt-1",
        role="writer",
        objective="post once",
        context_manifest=ContextManifest(
            contract_revision=1,
            plan_version=1,
            node_id="writer",
            base_revision="rev",
            included_files={},
        ),
        allowed_effects=(ToolEffect.EXTERNAL_SIDE_EFFECT,),
        budget=BudgetEnvelope(retry_budget=3),
    )
    result = runner.run(invocation)

    assert calls == 1
    assert result.status.value == "failed_terminal"
    assert result.failure is not None
    assert result.failure.ambiguous_side_effect
    assert not result.failure.retryable
    assert "automatic replay is forbidden" in result.failure.message
