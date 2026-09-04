from __future__ import annotations

from minicode.runtime_protocol import (
    PlanNode,
    PlanVersion,
    RequestedMode,
    TaskContract,
    TaskEnvelope,
    ToolEffect,
    VerificationLevel,
    VerificationRecord,
)
from minicode.verifier_registry import (
    CallableVerifier,
    CompletionGate,
    VerificationContext,
    VerifierRegistry,
)


def _contract_plan(tmp_path):
    contract = TaskContract(
        envelope=TaskEnvelope(
            run_id="run-1",
            request="change",
            workspace=str(tmp_path),
            requested_profile=RequestedMode.ULTRA,
        ),
        acceptance_criteria=("integration passes",),
    )
    plan = PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode(
                node_id="writer",
                objective="change",
                role="writer",
                allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE),
            ),
        ),
    )
    return contract, plan


def test_final_gate_rejects_integration_failure_even_when_nodes_succeeded(tmp_path) -> None:
    contract, plan = _contract_plan(tmp_path)
    snapshot = {
        "current_plan_version": 1,
        "contract_revision": 1,
        "nodes": {
            "writer": {
                "state": "SUCCEEDED",
                "result": {"evidence_refs": [{"claim": "node passed"}]},
            }
        },
    }
    delivery = CompletionGate().evaluate(
        contract=contract,
        plan=plan,
        runtime_snapshot=snapshot,
        workspace=tmp_path,
        verification_records=(
            VerificationRecord(
                verifier="integration-test",
                passed=False,
                scope="final",
                details="integrated test failed",
            ),
        ),
        level=VerificationLevel.STRICT,
        output_summary="nodes passed",
    )
    assert not delivery.completed
    assert "final verification records failed" in "; ".join(delivery.reasons)


def test_verifier_registry_orders_deterministic_checks_and_fails_closed(tmp_path) -> None:
    contract, plan = _contract_plan(tmp_path)
    calls: list[str] = []

    def passing(name):
        def callback(context):
            calls.append(name)
            return True, "ok"
        return callback

    def crashing(context):
        calls.append("static")
        raise RuntimeError("boom")

    registry = VerifierRegistry(
        [
            CallableVerifier("model", "model_judgment", passing("model"), deterministic=False),
            CallableVerifier("unit", "unit_test", passing("unit")),
            CallableVerifier("schema", "exit_code_schema", passing("schema")),
            CallableVerifier("static", "static_analysis", crashing),
        ]
    )
    records = registry.run(
        VerificationContext(contract=contract, plan=plan, workspace=tmp_path, final=True),
        allow_model=False,
    )
    assert calls == ["schema", "unit", "static"]
    assert not records[-1].passed
    assert "crashed" in records[-1].details
