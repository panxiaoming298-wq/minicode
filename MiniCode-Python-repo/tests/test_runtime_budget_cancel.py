from __future__ import annotations

import threading
from dataclasses import dataclass, replace

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
    SelectedRoute,
    VerificationLevel,
    VerificationRecord,
    WriteIsolation,
)
from minicode.runtime_scheduler import TaskScheduler
from minicode.runtime_state import RuntimeReducer


@dataclass
class FakeClock:
    value: float = 100.0

    def now(self) -> float:
        return self.value


def _profile(**changes) -> ExecutionProfile:
    profile = ExecutionProfile(
        requested_mode=RequestedMode.ULTRA,
        selected_route=SelectedRoute.DEEP_SINGLE,
        reasoning_budget="high",
        planner_budget=0,
        verifier_budget=1,
        max_active_workers=1,
        max_agents=2,
        max_tokens=None,
        max_cost=None,
        deadline=None,
        retry_budget=0,
        max_delegation_depth=1,
        verification_level=VerificationLevel.STRICT,
        write_isolation=WriteIsolation.TEMPORARY_WORKSPACE,
    )
    return replace(profile, **changes)


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


def _result(
    invocation: AgentInvocationSpec,
    *,
    status: AgentResultStatus = AgentResultStatus.SUCCEEDED,
    tokens: int = 0,
    cost: float = 0.0,
) -> AgentResult:
    return AgentResult(
        run_id=invocation.run_id,
        plan_version=invocation.plan_version,
        node_id=invocation.node_id,
        attempt_id=invocation.attempt_id,
        status=status,
        base_revision="rev",
        output_summary=status.value,
        contract_revision=1,
        usage={"tokens": tokens, "cost": cost},
    )


class DeadlineWorker:
    def __init__(self, clock: FakeClock, cancellation: threading.Event) -> None:
        self.clock = clock
        self.cancellation = cancellation

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.clock.value = 102.0
        assert self.cancellation.wait(timeout=5)
        return _result(invocation, status=AgentResultStatus.CANCELED)


def test_deadline_cancels_active_worker_without_sleep_based_coordination() -> None:
    clock = FakeClock()
    cancellation = threading.Event()
    plan = PlanVersion(
        run_id="run-deadline",
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("node", "wait", "deep"),),
    )
    reducer = RuntimeReducer(plan, clock=clock)
    scheduler = TaskScheduler(
        reducer,
        _profile(max_agents=1, deadline=101.0),
        clock=clock,
        cancellation_event=cancellation,
    )
    report = scheduler.execute(
        DeadlineWorker(clock, cancellation),
        context_builder=_context,
        node_verifier=_verify,
    )

    assert report.canceled
    assert report.active_leases == 0
    assert "deadline exhausted" in report.reasons


class TokenWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.calls.append(invocation.node_id)
        return _result(invocation, tokens=11)


def test_token_budget_stops_dependent_worker_expansion() -> None:
    plan = PlanVersion(
        run_id="run-token",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("first", "first", "explorer"),
            PlanNode("second", "second", "deep", dependencies=("first",)),
        ),
    )
    worker = TokenWorker()
    report = TaskScheduler(
        RuntimeReducer(plan),
        _profile(max_tokens=10),
    ).execute(worker, context_builder=_context, node_verifier=_verify)

    assert worker.calls == ["first"]
    assert report.tokens_used == 11
    assert report.canceled
    assert "max_tokens budget exhausted" in report.reasons


def test_exact_token_budget_stops_dependent_worker_expansion() -> None:
    plan = PlanVersion(
        run_id="run-token-exact",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("first", "first", "explorer"),
            PlanNode("second", "second", "deep", dependencies=("first",)),
        ),
    )

    class ExactTokenWorker(TokenWorker):
        def run(self, invocation: AgentInvocationSpec) -> AgentResult:
            self.calls.append(invocation.node_id)
            return _result(invocation, tokens=10)

    worker = ExactTokenWorker()
    report = TaskScheduler(
        RuntimeReducer(plan),
        _profile(max_tokens=10),
    ).execute(worker, context_builder=_context, node_verifier=_verify)

    assert worker.calls == ["first"]
    assert report.tokens_used == 10
    assert report.canceled
    assert "max_tokens budget exhausted" in report.reasons


class CostWorker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, invocation: AgentInvocationSpec) -> AgentResult:
        self.calls.append(invocation.node_id)
        return _result(invocation, cost=1.25)


def test_cost_budget_stops_dependent_worker_expansion() -> None:
    plan = PlanVersion(
        run_id="run-cost",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("first", "first", "explorer"),
            PlanNode("second", "second", "deep", dependencies=("first",)),
        ),
    )
    worker = CostWorker()
    report = TaskScheduler(
        RuntimeReducer(plan),
        _profile(max_cost=1.0),
    ).execute(worker, context_builder=_context, node_verifier=_verify)

    assert worker.calls == ["first"]
    assert report.cost_used == 1.25
    assert report.canceled
    assert "max_cost budget exhausted" in report.reasons


def test_exact_cost_budget_stops_dependent_worker_expansion() -> None:
    plan = PlanVersion(
        run_id="run-cost-exact",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("first", "first", "explorer"),
            PlanNode("second", "second", "deep", dependencies=("first",)),
        ),
    )

    class ExactCostWorker(CostWorker):
        def run(self, invocation: AgentInvocationSpec) -> AgentResult:
            self.calls.append(invocation.node_id)
            return _result(invocation, cost=1.0)

    worker = ExactCostWorker()
    report = TaskScheduler(
        RuntimeReducer(plan),
        _profile(max_cost=1.0),
    ).execute(worker, context_builder=_context, node_verifier=_verify)

    assert worker.calls == ["first"]
    assert report.cost_used == 1.0
    assert report.canceled
    assert "max_cost budget exhausted" in report.reasons


def test_max_agents_stops_new_node_claims() -> None:
    plan = PlanVersion(
        run_id="run-agents",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("first", "first", "explorer"),
            PlanNode("second", "second", "explorer", dependencies=("first",)),
        ),
    )
    worker = TokenWorker()
    report = TaskScheduler(
        RuntimeReducer(plan),
        _profile(max_agents=1),
    ).execute(worker, context_builder=_context, node_verifier=_verify)

    assert worker.calls == ["first"]
    assert report.claimed_attempts == 1
    assert "max_agents budget exhausted" in report.reasons
