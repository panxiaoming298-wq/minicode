from __future__ import annotations

from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import (
    AgentInvocationSpec,
    BudgetEnvelope,
    ContextManifest,
    ToolEffect,
)
from minicode.tooling import ToolContext, ToolRegistry
from minicode.worker_runtime import AgentLoopWorkerRunner


def test_worker_output_over_8000_is_stored_whole_and_referenced(tmp_path, monkeypatch) -> None:
    output = "x" * 9_000 + "TAIL-SENTINEL"

    def fake_agent_loop(**kwargs):
        return [*kwargs["messages"], {"role": "assistant", "content": output}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    store = RuntimeArtifactStore(tmp_path / "artifacts")
    runner = AgentLoopWorkerRunner(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "inspect"}],
        cwd=str(tmp_path),
        runtime={"runtimeV2": True},
        artifact_store=store,
    )
    invocation = AgentInvocationSpec(
        run_id="run-1",
        plan_version=1,
        node_id="node-1",
        attempt_id="attempt-1",
        role="explorer",
        objective="inspect",
        context_manifest=ContextManifest(
            contract_revision=1,
            plan_version=1,
            node_id="node-1",
            base_revision="rev",
            included_files={},
        ),
        allowed_effects=(ToolEffect.READ_ONLY,),
        budget=BudgetEnvelope(),
    )
    result = runner.run(invocation)

    assert len(result.artifact_refs) == 1
    assert "Full output stored" in result.output_summary
    assert "TAIL-SENTINEL" not in result.output_summary
    assert store.get_text(result.artifact_refs[0]).endswith("TAIL-SENTINEL")
    assert store.get_text(result.artifact_refs[0]) == output


def test_dependent_verifier_receives_complete_artifact_not_only_summary(tmp_path, monkeypatch) -> None:
    output = "x" * 9_000 + "TAIL-FOR-VERIFIER"
    verifier_saw_tail = False
    dependency_artifact_id = ""

    def fake_agent_loop(**kwargs):
        nonlocal verifier_saw_tail
        node_id = kwargs["runtime"]["_runtimeNodeId"]
        if node_id == "explorer":
            content = output
        else:
            assert not any(
                "TAIL-FOR-VERIFIER" in str(message.get("content", ""))
                for message in kwargs["messages"]
            )
            receipt = kwargs["tools"].execute(
                "runtime_artifact_read",
                {
                    "artifact_id": dependency_artifact_id,
                    "start": 8_990,
                    "end": 9_100,
                },
                ToolContext(cwd=str(tmp_path)),
            )
            verifier_saw_tail = receipt.ok and "TAIL-FOR-VERIFIER" in receipt.output
            content = "<verification_passed>"
        return [*kwargs["messages"], {"role": "assistant", "content": content}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runner = AgentLoopWorkerRunner(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "inspect"}],
        cwd=str(tmp_path),
        runtime={"runtimeV2": True},
        artifact_store=RuntimeArtifactStore(tmp_path / "artifacts"),
    )
    first = AgentInvocationSpec(
        run_id="run-1",
        plan_version=1,
        node_id="explorer",
        attempt_id="attempt-1",
        role="explorer",
        objective="inspect",
        context_manifest=ContextManifest(
            contract_revision=1,
            plan_version=1,
            node_id="explorer",
            base_revision="rev",
            included_files={},
        ),
        allowed_effects=(ToolEffect.READ_ONLY,),
        budget=BudgetEnvelope(),
    )
    first_result = runner.run(first)
    assert first_result.artifact_refs
    dependency_artifact_id = first_result.artifact_refs[0].artifact_id
    second = AgentInvocationSpec(
        run_id="run-1",
        plan_version=1,
        node_id="verifier",
        attempt_id="attempt-2",
        role="verifier",
        objective="verify",
        context_manifest=ContextManifest(
            contract_revision=1,
            plan_version=1,
            node_id="verifier",
            base_revision="rev",
            included_files={},
            dependency_result_refs=("explorer",),
        ),
        allowed_effects=(ToolEffect.READ_ONLY,),
        budget=BudgetEnvelope(),
    )
    result = runner.run(second)
    assert result.status.value == "succeeded"
    assert verifier_saw_tail
