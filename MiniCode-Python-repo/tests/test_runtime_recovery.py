from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    BudgetEnvelope,
    ContextManifest,
    ExecutionProfile,
    PlanNode,
    PlanVersion,
    RequestedMode,
    RuntimeCommandType,
    SelectedRoute,
    TaskAmendment,
    VerificationLevel,
    VerificationRecord,
    WriteIsolation,
)
from minicode.runtime_scheduler import TaskScheduler
from minicode.runtime_state import (
    RuntimeReducer,
    RuntimeStateIntegrityError,
    RuntimeStateStore,
    command,
)
from minicode.tooling import ToolRegistry
from minicode.unified_runtime import run_runtime_turn


@dataclass
class FakeClock:
    value: float = 100.0

    def now(self) -> float:
        return self.value


def _profile() -> ExecutionProfile:
    return ExecutionProfile(
        requested_mode=RequestedMode.STANDARD,
        selected_route=SelectedRoute.STANDARD_SINGLE,
        reasoning_budget="low",
        planner_budget=0,
        verifier_budget=0,
        max_active_workers=1,
        max_agents=1,
        max_tokens=None,
        max_cost=None,
        deadline=None,
        retry_budget=1,
        max_delegation_depth=1,
        verification_level=VerificationLevel.LIGHT,
        write_isolation=WriteIsolation.DIRECT,
    )


def _plan(*, run_id: str = "run-1", max_attempts: int = 2) -> PlanVersion:
    return PlanVersion(
        run_id=run_id,
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("node", "work", "standard", max_attempts=max_attempts),),
    )


def _context(node: PlanNode, attempt_id: str, budget: BudgetEnvelope) -> ContextManifest:
    return ContextManifest(
        contract_revision=1,
        plan_version=1,
        node_id=node.node_id,
        base_revision="rev",
        included_files={},
        budget_allocation=budget,
    )


def _verify(result: AgentResult) -> VerificationRecord:
    return VerificationRecord("schema", True, f"node:{result.node_id}")


def _result(invocation: AgentInvocationSpec, status: AgentResultStatus) -> AgentResult:
    return AgentResult(
        run_id=invocation.run_id,
        plan_version=invocation.plan_version,
        node_id=invocation.node_id,
        attempt_id=invocation.attempt_id,
        status=status,
        base_revision=invocation.context_manifest.base_revision,
        output_summary=status.value,
        contract_revision=invocation.context_manifest.contract_revision,
    )


class ApprovalThenSuccessWorker:
    def __init__(self) -> None:
        self.calls = 0
        self.idempotency_keys: list[str] = []

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.calls += 1
        self.idempotency_keys.append(invocation.idempotency_key)
        status = (
            AgentResultStatus.WAITING_APPROVAL
            if self.calls == 1
            else AgentResultStatus.SUCCEEDED
        )
        return _result(invocation, status)


def test_waiting_approval_pauses_without_cancel_and_resumes_as_new_attempt() -> None:
    reducer = RuntimeReducer(_plan(max_attempts=1))
    worker = ApprovalThenSuccessWorker()
    first = TaskScheduler(reducer, _profile()).execute(
        worker,
        context_builder=_context,
        node_verifier=_verify,
    )

    assert not first.completed
    assert not first.canceled
    assert first.active_leases == 0
    assert reducer.node_state("node").value == "WAITING_APPROVAL"
    assert "VerificationStarted" not in {
        event["event_type"] for event in reducer.snapshot()["events"]
    }

    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.APPROVE_TOOL_CALL,
            {"approval_id": "approval-1", "node_id": "node"},
        )
    )
    second = TaskScheduler(reducer, _profile()).execute(
        worker,
        context_builder=_context,
        node_verifier=_verify,
    )

    assert second.completed
    assert worker.calls == 2
    assert worker.idempotency_keys[0]
    assert worker.idempotency_keys[0] == worker.idempotency_keys[1]
    assert len(reducer.snapshot()["nodes"]["node"]["attempts"]) == 2


class NeverCalledWorker:
    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        raise AssertionError("persisted successful Result must be verified, not rerun")


def test_restart_recovers_result_persisted_before_verification(tmp_path) -> None:
    store = RuntimeStateStore(tmp_path / "state.json")
    reducer = RuntimeReducer(_plan(), store=store)
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.CLAIM_NODE,
            {"node_id": "node", "plan_version": 1, "lease_seconds": 10},
        )
    )
    attempt = reducer.current_attempt("node")
    assert attempt is not None
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.START_ATTEMPT,
            {"node_id": "node", "attempt_id": attempt.attempt_id},
        )
    )
    invocation = AgentInvocationSpec(
        run_id="run-1",
        plan_version=1,
        node_id="node",
        attempt_id=attempt.attempt_id,
        role="standard",
        objective="work",
        context_manifest=_context(_plan().nodes[0], attempt.attempt_id, BudgetEnvelope()),
        allowed_effects=_plan().nodes[0].allowed_effects,
        budget=BudgetEnvelope(),
        idempotency_key=attempt.idempotency_key,
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {
                "result": _result(invocation, AgentResultStatus.SUCCEEDED),
                "fencing_token": attempt.lease.fencing_token.value,
            },
        )
    )

    restored = RuntimeReducer.restore(store.load() or {}, store=store)
    report = TaskScheduler(restored, _profile()).execute(
        NeverCalledWorker(),
        context_builder=_context,
        node_verifier=_verify,
    )
    assert report.completed
    assert restored.node_state("node").value == "SUCCEEDED"


class RecordingWorker:
    def __init__(self, *, crash_first: bool = False) -> None:
        self.crash_first = crash_first
        self.keys: list[str] = []

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.keys.append(invocation.idempotency_key)
        if self.crash_first and len(self.keys) == 1:
            raise RuntimeError("injected crash")
        return _result(invocation, AgentResultStatus.SUCCEEDED)


def test_expired_claim_is_recovered_and_retry_reuses_idempotency_key() -> None:
    clock = FakeClock()
    reducer = RuntimeReducer(_plan(), clock=clock)
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.CLAIM_NODE,
            {
                "node_id": "node",
                "plan_version": 1,
                "lease_seconds": 5,
                "idempotency_key": "stable-operation-key",
            },
        )
    )
    attempt = reducer.current_attempt("node")
    assert attempt is not None
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.START_ATTEMPT,
            {"node_id": "node", "attempt_id": attempt.attempt_id},
        )
    )
    clock.value = 106.0
    worker = RecordingWorker()
    report = TaskScheduler(reducer, _profile(), clock=clock).execute(
        worker,
        context_builder=_context,
        node_verifier=_verify,
    )
    assert report.completed
    assert worker.keys == ["stable-operation-key"]


def test_stale_contract_result_is_rejected_before_verification() -> None:
    reducer = RuntimeReducer(_plan())
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.CLAIM_NODE,
            {"node_id": "node", "plan_version": 1},
        )
    )
    attempt = reducer.current_attempt("node")
    assert attempt is not None
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.START_ATTEMPT,
            {"node_id": "node", "attempt_id": attempt.attempt_id},
        )
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.AMEND_TASK,
            {
                "amendment": TaskAmendment(
                    amendment_id="amend-1",
                    run_id="run-1",
                    base_contract_revision=1,
                    next_contract_revision=2,
                    changed_fields={"request": "changed"},
                )
            },
        )
    )
    stale = AgentResult(
        run_id="run-1",
        plan_version=1,
        node_id="node",
        attempt_id=attempt.attempt_id,
        status=AgentResultStatus.SUCCEEDED,
        base_revision="rev",
        output_summary="stale",
        contract_revision=1,
    )
    events = reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {"result": stale, "fencing_token": attempt.lease.fencing_token.value},
        )
    )
    assert events[0].payload["reason"] == "result references a stale TaskContract revision"


def test_retry_after_worker_crash_reuses_same_idempotency_key() -> None:
    reducer = RuntimeReducer(_plan())
    worker = RecordingWorker(crash_first=True)
    report = TaskScheduler(reducer, _profile()).execute(
        worker,
        context_builder=_context,
        node_verifier=_verify,
    )
    assert report.completed
    assert len(worker.keys) == 2
    assert worker.keys[0] == worker.keys[1]


def test_product_runtime_can_resume_persisted_waiting_run(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "resume-state.json"
    store = RuntimeStateStore(state_path)
    plan = PlanVersion(
        run_id="run-resume",
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("root", "resume the same request", "standard", max_attempts=1),),
    )
    reducer = RuntimeReducer(plan, store=store)
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.CLAIM_NODE,
            {
                "node_id": "root",
                "plan_version": 1,
                "idempotency_key": "resume-stable-key",
            },
        )
    )
    attempt = reducer.current_attempt("root")
    assert attempt is not None
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.START_ATTEMPT,
            {"node_id": "root", "attempt_id": attempt.attempt_id},
        )
    )
    reducer.apply(
        command(
            reducer.run_id,
            RuntimeCommandType.SUBMIT_RESULT,
            {
                "result": AgentResult(
                    run_id="run-resume",
                    plan_version=1,
                    node_id="root",
                    attempt_id=attempt.attempt_id,
                    status=AgentResultStatus.WAITING_APPROVAL,
                    base_revision="rev",
                    output_summary="waiting",
                ),
                "fencing_token": attempt.lease.fencing_token.value,
            },
        )
    )

    seen_keys: list[str] = []

    def fake_agent_loop(**kwargs):
        seen_keys.append(kwargs["runtime"]["_runtimeIdempotencyKey"])
        return [*kwargs["messages"], {"role": "assistant", "content": "resumed"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {
        "runtimeV2": True,
        "executionProfile": "standard",
        "runtimeRunId": "run-resume",
        "runtimeStatePath": str(state_path),
        "runtimeResume": True,
        "_runtimeCommands": (
            command(
                "run-resume",
                RuntimeCommandType.APPROVE_TOOL_CALL,
                {"approval_id": "approval-1", "node_id": "root"},
            ),
        ),
    }
    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "resume the same request"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    assert messages[-1]["content"] == "resumed"
    assert seen_keys == ["resume-stable-key"]
    assert runtime["lastRuntimeTrace"]["delivery"]["completed"] is True


def test_product_runtime_resumes_completed_result_and_usage_without_rerun(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "completed-state.json"
    calls = 0

    def first_agent_loop(**kwargs):
        nonlocal calls
        calls += 1
        return [
            *kwargs["messages"],
            {"role": "assistant", "content": "persisted final answer"},
        ]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", first_agent_loop)
    first_runtime = {
        "runtimeV2": True,
        "executionProfile": "standard",
        "runtimeRunId": "run-completed-resume",
        "runtimeStatePath": str(state_path),
        "_usageExtractor": lambda result, inputs: {
            "tokens": 17,
            "cost": 0.25,
            "model_calls": 1,
        },
    }
    first_messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "persist this answer"}],
        cwd=str(tmp_path),
        runtime=first_runtime,
    )
    assert first_messages[-1]["content"] == "persisted final answer"
    assert calls == 1

    def must_not_rerun(**kwargs):
        raise AssertionError("completed persisted Worker must not be rerun")

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", must_not_rerun)
    resumed_runtime = {
        "runtimeV2": True,
        "executionProfile": "standard",
        "runtimeRunId": "run-completed-resume",
        "runtimeStatePath": str(state_path),
        "runtimeResume": True,
    }
    resumed_messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "persist this answer"}],
        cwd=str(tmp_path),
        runtime=resumed_runtime,
    )

    assert resumed_messages[-1] == {
        "role": "assistant",
        "content": "persisted final answer",
    }
    trace = resumed_runtime["lastRuntimeTrace"]
    assert trace["scheduler"]["claimed_attempts"] == 0
    assert trace["scheduler"]["tokens_used"] == 17
    assert trace["scheduler"]["cost_used"] == 0.25
    assert trace["delivery"]["completed"] is True


def test_state_atomic_replace_interruption_preserves_last_complete_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    store = RuntimeStateStore(tmp_path / "atomic-state.json")
    store.save({"revision": 1, "complete": True})

    def fail_replace(source, destination):
        raise OSError("injected replace interruption")

    monkeypatch.setattr("minicode.runtime_state.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace interruption"):
        store.save({"revision": 2, "complete": True})

    assert store.load() == {"revision": 1, "complete": True}


def test_state_store_rejects_hardlink_alias_on_load_and_save(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"revision": 1}\n', encoding="utf-8")
    state_path = tmp_path / "state.json"
    os.link(outside, state_path)
    store = RuntimeStateStore(state_path)

    assert store.load() is None
    with pytest.raises(RuntimeStateIntegrityError, match="aliases"):
        store.save({"revision": 2})
    assert outside.read_text(encoding="utf-8") == '{"revision": 1}\n'
