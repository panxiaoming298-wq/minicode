"""Unified Standard/Ultra Runtime orchestration.

Standard and Ultra share every protocol and authority in this module.  The
Standard fast path uses deterministic zero-model planning/validation/gating and
one existing AgentLoop Worker; Ultra may select deep-single or read fan-out.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from minicode.delegation import SchedulerDelegationGateway
from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore
from minicode.runtime_protocol import (
    AgentResult,
    BudgetEnvelope,
    ContextManifest,
    ExecutionProfile,
    PlanNode,
    PlanVersion,
    RequestedMode,
    RuntimeCommand,
    SelectedRoute,
    TaskAmendment,
    TaskContract,
    TaskEnvelope,
    ToolEffect,
    TopologyDecision,
    VerificationLevel,
    VerificationRecord,
    WriteIsolation,
    new_id,
)
from minicode.runtime_scheduler import SchedulerReport, TaskScheduler
from minicode.runtime_state import RuntimeReducer, RuntimeStateStore
from minicode.task_graph import CanonicalPlanGraph
from minicode.tooling import ToolRegistry
from minicode.turn_events import TurnEvent
from minicode.types import ChatMessage
from minicode.verifier_registry import (
    CallableVerifier,
    CompletionGate,
    IsolatedCommandVerifier,
    NodeResultVerifier,
    VerificationContext,
    VerifierRegistry,
)
from minicode.worker_runtime import AgentLoopWorkerRunner, workspace_revision
from minicode.workspace_provider import workspace_snapshot


class PlanValidationError(ValueError):
    pass


_INTEGRATED_VERIFICATION_TASK = re.compile(
    r"(?ix)"
    r"\b(?:implement|modify|change|fix|debug|diagnos(?:e|is)|refactor|migrate|"
    r"build|create|delete|install|configure|audit|secure|test|root[ -]?cause)\b"
    r"|实现|修改|修复|新增|添加|删除|重构|迁移|创建|构建|开发|安装|配置|"
    r"排查|诊断|审计|安全检查|根因|竞态|测试|验收"
)


def _requires_independent_verifier(request: str, runtime: Mapping[str, Any]) -> bool:
    """Return whether a deep-single task has an integrated result to verify.

    Ultra still gives conversational and explanatory requests the deep Worker,
    but a second model verifier is only useful when the turn produces or audits
    an external deliverable.  Callers can override the conservative classifier.
    """

    if "runtimeRequireIndependentVerifier" in runtime:
        return _strict_bool(
            runtime.get("runtimeRequireIndependentVerifier"),
            default=False,
        )
    hints = runtime.get("topologyHints")
    if isinstance(hints, Mapping) and "requires_independent_verification" in hints:
        return _strict_bool(
            hints.get("requires_independent_verification"),
            default=False,
        )
    if runtime.get("runtimeVerificationCommands"):
        return True
    if runtime.get("acceptanceCriteria") or runtime.get("expectedOutputs"):
        return True
    return bool(_INTEGRATED_VERIFICATION_TASK.search(request))


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    contract: TaskContract
    profile: ExecutionProfile
    topology: TopologyDecision
    plan: PlanVersion
    scheduler_report: SchedulerReport
    delivery: Any
    runtime_snapshot: Mapping[str, Any]
    context_manifests: tuple[ContextManifest, ...]
    messages: tuple[dict[str, Any], ...]

    def trace(self) -> dict[str, Any]:
        return {
            "schema_version": "minicode-runtime-trace-v2",
            "contract": self.contract.to_dict(),
            "profile": self.profile.to_dict(),
            "topology_decision": self.topology.to_dict(),
            "plan": self.plan.to_dict(),
            "scheduler": {
                "run_id": self.scheduler_report.run_id,
                "completed": self.scheduler_report.completed,
                "canceled": self.scheduler_report.canceled,
                "claimed_attempts": self.scheduler_report.claimed_attempts,
                "peak_active_workers": self.scheduler_report.peak_active_workers,
                "peak_active_writers": self.scheduler_report.peak_active_writers,
                "active_leases": self.scheduler_report.active_leases,
                "tokens_used": self.scheduler_report.tokens_used,
                "cost_used": self.scheduler_report.cost_used,
                "result_node_ids": list(self.scheduler_report.result_node_ids),
                "reasons": list(self.scheduler_report.reasons),
            },
            "delivery": self.delivery.to_dict(),
            "state": dict(self.runtime_snapshot),
            "context_manifests": [manifest.to_dict() for manifest in self.context_manifests],
        }


class ProfileResolver:
    @staticmethod
    def requested_mode(runtime: Mapping[str, Any] | None) -> RequestedMode:
        value = str(
            (runtime or {}).get("executionProfile")
            or (runtime or {}).get("profile")
            or "standard"
        ).strip().lower()
        try:
            return RequestedMode(value)
        except ValueError as error:
            raise ValueError("execution profile must be 'standard' or 'ultra'") from error

    def resolve(
        self,
        *,
        runtime: Mapping[str, Any] | None,
        selected_route: SelectedRoute,
        request: str,
        now: float | None = None,
    ) -> ExecutionProfile:
        runtime = runtime or {}
        mode = self.requested_mode(runtime)
        raw_deadline = runtime.get("runtimeDeadline")
        deadline = _finite_deadline(raw_deadline)
        max_tokens = _positive_int(runtime.get("runtimeMaxTokens"))
        max_cost = _positive_float(runtime.get("runtimeMaxCost"))
        if mode is RequestedMode.STANDARD:
            reasoning = "medium" if len(request) > 500 else "low"
            return ExecutionProfile(
                requested_mode=mode,
                selected_route=SelectedRoute.STANDARD_SINGLE,
                reasoning_budget=reasoning,
                planner_budget=0,
                verifier_budget=0,
                max_active_workers=1,
                max_agents=max(1, _positive_int(runtime.get("runtimeMaxAgents")) or 4),
                max_tokens=max_tokens,
                max_cost=max_cost,
                deadline=deadline,
                retry_budget=max(0, int(runtime.get("runtimeRetryBudget", 0) or 0)),
                max_delegation_depth=1,
                verification_level=VerificationLevel.LIGHT,
                write_isolation=WriteIsolation.DIRECT,
                dynamic_worker_delegation=True,
            )
        raw_verifier_budget = runtime.get("runtimeVerifierBudget")
        if raw_verifier_budget is None:
            verifier_budget = 1 if _requires_independent_verifier(request, runtime) else 0
        else:
            verifier_budget = max(0, int(raw_verifier_budget))
        if selected_route is SelectedRoute.ULTRA_READ_FANOUT:
            verifier_budget = max(1, verifier_budget)
        return ExecutionProfile(
            requested_mode=mode,
            selected_route=selected_route,
            reasoning_budget="high",
            planner_budget=max(0, int(runtime.get("runtimePlannerBudget", 0) or 0)),
            verifier_budget=verifier_budget,
            max_active_workers=max(1, _positive_int(runtime.get("runtimeMaxActiveWorkers")) or 4),
            max_agents=max(1, _positive_int(runtime.get("runtimeMaxAgents")) or 6),
            max_tokens=max_tokens,
            max_cost=max_cost,
            deadline=deadline,
            retry_budget=max(0, int(runtime.get("runtimeRetryBudget", 1) or 1)),
            max_delegation_depth=1,
            verification_level=VerificationLevel.STRICT,
            write_isolation=WriteIsolation.TEMPORARY_WORKSPACE,
            dynamic_worker_delegation=False,
        )


class TopologyResolver:
    """Deterministic, auditable Ultra topology selection."""

    _STRONG_COUPLING = re.compile(
        r"(?i)root[ -]?cause|race condition|strongly coupled|single invariant|根因|竞态|强耦合|整体迁移"
    )
    _HIGH_WRITE_CONFLICT = re.compile(
        r"(?i)parallel writes?|same files?|merge conflict|高写冲突|并行写|同一文件"
    )

    def decide(
        self,
        *,
        requested_mode: RequestedMode,
        request: str,
        runtime: Mapping[str, Any] | None,
    ) -> TopologyDecision:
        runtime = runtime or {}
        if requested_mode is RequestedMode.STANDARD:
            return TopologyDecision(
                requested_profile=requested_mode,
                selected_route=SelectedRoute.STANDARD_SINGLE,
                independent_subproblems=(),
                shared_context_dependency="low",
                write_conflict_risk="none",
                expected_parallel_gain=0.0,
                deterministic_verifiability=True,
                estimated_extra_cost=0.0,
                reasons=("Standard always uses the deterministic single-Worker route",),
                rejected_routes={
                    SelectedRoute.DEEP_SINGLE.value: "not requested",
                    SelectedRoute.ULTRA_READ_FANOUT.value: "not requested",
                },
            )

        hints = runtime.get("topologyHints")
        hints = hints if isinstance(hints, Mapping) else {}
        explicit = runtime.get("ultraSubproblems")
        subproblems = tuple(
            str(item).strip()
            for item in explicit
            if str(item).strip()
        ) if isinstance(explicit, (list, tuple)) else self._extract_subproblems(request)
        shared = str(hints.get("shared_context_dependency", "high" if self._STRONG_COUPLING.search(request) else "low")).lower()
        conflict = str(hints.get("write_conflict_risk", "high" if self._HIGH_WRITE_CONFLICT.search(request) else "none")).lower()
        deterministic = _strict_bool(
            hints.get("deterministic_verifiability"),
            default=True,
        )
        gain = _topology_score(
            hints.get("expected_parallel_gain"),
            default=0.35 if len(subproblems) >= 2 else 0.0,
            invalid=0.0,
        )
        extra_cost = _topology_score(
            hints.get("estimated_extra_cost"),
            default=0.10 if len(subproblems) >= 2 else 0.0,
            invalid=1.0,
        )
        max_agents = _positive_int(runtime.get("runtimeMaxAgents")) or 6
        max_tokens = _positive_int(runtime.get("runtimeMaxTokens"))

        checks = {
            "at least two independent subproblems": len(subproblems) >= 2,
            "subproblems have explicit objectives": all(len(item) >= 3 for item in subproblems),
            "results are independently verifiable": deterministic,
            "parallel work has material gain": gain >= 0.20,
            "shared context dependency is low": shared == "low",
            "no unisolated parallel writes": conflict in {"none", "low"},
            "gain exceeds coordination cost": gain > extra_cost,
            "agent/token budget permits fan-out": max_agents >= len(subproblems) + 2 and (max_tokens is None or max_tokens >= 4_000 * len(subproblems)),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if not failed:
            return TopologyDecision(
                requested_profile=requested_mode,
                selected_route=SelectedRoute.ULTRA_READ_FANOUT,
                independent_subproblems=subproblems,
                shared_context_dependency=shared,
                write_conflict_risk=conflict,
                expected_parallel_gain=gain,
                deterministic_verifiability=deterministic,
                estimated_extra_cost=extra_cost,
                reasons=tuple(name for name, passed in checks.items() if passed),
                rejected_routes={SelectedRoute.DEEP_SINGLE.value: "fan-out passed every safety/benefit condition"},
            )
        return TopologyDecision(
            requested_profile=requested_mode,
            selected_route=SelectedRoute.DEEP_SINGLE,
            independent_subproblems=subproblems,
            shared_context_dependency=shared,
            write_conflict_risk=conflict,
            expected_parallel_gain=gain,
            deterministic_verifiability=deterministic,
            estimated_extra_cost=extra_cost,
            reasons=("Ultra selected deep-single because: " + "; ".join(failed),),
            rejected_routes={SelectedRoute.ULTRA_READ_FANOUT.value: "; ".join(failed)},
        )

    @staticmethod
    def _extract_subproblems(request: str) -> tuple[str, ...]:
        candidates: list[str] = []
        for line in request.splitlines():
            match = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$", line)
            if match:
                item = match.group(1).strip()
                if item and len(item) >= 8:
                    candidates.append(item)
        # Avoid treating a long implementation checklist as independently
        # verifiable unless the caller provides explicit topology hints.
        return tuple(candidates[:4]) if 2 <= len(candidates) <= 4 else ()


class PlanCompiler:
    def compile(
        self,
        contract: TaskContract,
        profile: ExecutionProfile,
        topology: TopologyDecision,
    ) -> PlanVersion:
        retry_attempts = max(1, profile.retry_budget + 1)
        read = (ToolEffect.READ_ONLY,)
        # A Writer/Deep Worker is the task's fully capable execution node.  In
        # particular it must retain the built-in web tools: otherwise Ultra
        # exposes ``web_search``/``web_fetch`` in the capability snapshot but
        # filters them out of the actual Worker, which encourages models to
        # fall back to approval-heavy ``run_command curl`` calls.
        write = (
            ToolEffect.READ_ONLY,
            ToolEffect.WORKSPACE_WRITE,
            ToolEffect.EXTERNAL_SIDE_EFFECT,
            ToolEffect.PRIVILEGED,
        )
        nodes: tuple[PlanNode, ...]
        if topology.selected_route is SelectedRoute.STANDARD_SINGLE:
            nodes = (
                PlanNode(
                    node_id="root",
                    objective=contract.envelope.request,
                    role="standard",
                    allowed_effects=(
                        ToolEffect.READ_ONLY,
                        ToolEffect.WORKSPACE_WRITE,
                        ToolEffect.EXTERNAL_SIDE_EFFECT,
                        ToolEffect.PRIVILEGED,
                    ),
                    acceptance_criteria=contract.acceptance_criteria,
                    max_attempts=retry_attempts,
                ),
            )
        elif topology.selected_route is SelectedRoute.DEEP_SINGLE:
            deep_worker = PlanNode(
                node_id="deep-worker",
                objective=contract.envelope.request,
                role="deep",
                allowed_effects=write,
                acceptance_criteria=contract.acceptance_criteria,
                max_attempts=retry_attempts,
            )
            nodes = (deep_worker,) if profile.max_agents < 2 or profile.verifier_budget == 0 else (
                deep_worker,
                PlanNode(
                    node_id="final-verifier",
                    objective=(
                        "Independently verify the integrated result for this original request:\n"
                        + contract.envelope.request
                    ),
                    role="verifier",
                    dependencies=("deep-worker",),
                    allowed_effects=read,
                    acceptance_criteria=(
                        contract.acceptance_criteria
                        or ("verify the delivered result addresses the original request",)
                    ),
                    max_attempts=retry_attempts,
                ),
            )
        else:
            explorers = tuple(
                PlanNode(
                    node_id=f"explorer-{index}",
                    objective=objective,
                    role="explorer" if index % 2 else "reviewer",
                    allowed_effects=read,
                    acceptance_criteria=("return independently verifiable read-only evidence",),
                    max_attempts=retry_attempts,
                )
                for index, objective in enumerate(topology.independent_subproblems, start=1)
            )
            dependencies = tuple(node.node_id for node in explorers)
            nodes = (
                *explorers,
                PlanNode(
                    node_id="exclusive-writer",
                    objective=contract.envelope.request,
                    role="writer",
                    dependencies=dependencies,
                    allowed_effects=write,
                    acceptance_criteria=contract.acceptance_criteria,
                    max_attempts=retry_attempts,
                ),
                PlanNode(
                    node_id="final-verifier",
                    objective=(
                        "Independently verify the integrated result for this original request:\n"
                        + contract.envelope.request
                    ),
                    role="verifier",
                    dependencies=("exclusive-writer",),
                    allowed_effects=read,
                    acceptance_criteria=(
                        contract.acceptance_criteria
                        or ("verify the delivered result addresses the original request",)
                    ),
                    max_attempts=retry_attempts,
                ),
            )
        return PlanVersion(
            run_id=contract.envelope.run_id,
            plan_version=1,
            contract_revision=contract.contract_revision,
            nodes=nodes,
        )


class PlanValidator:
    def validate(self, plan: PlanVersion, profile: ExecutionProfile) -> None:
        if not plan.nodes:
            raise PlanValidationError("PlanGraph must contain at least one PlanNode")
        if (
            profile.requested_mode is RequestedMode.STANDARD
            and profile.selected_route is not SelectedRoute.STANDARD_SINGLE
        ):
            raise PlanValidationError("Standard profile must use standard-single")
        if (
            profile.requested_mode is RequestedMode.ULTRA
            and profile.selected_route is SelectedRoute.STANDARD_SINGLE
        ):
            raise PlanValidationError("Ultra profile may not use standard-single")
        ids = [node.node_id for node in plan.nodes]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("PlanNode ids must be unique")
        known = set(ids)
        for node in plan.nodes:
            missing = set(node.dependencies) - known
            if missing:
                raise PlanValidationError(f"node {node.node_id} has missing dependencies: {sorted(missing)}")
            if node.role in {"explorer", "reviewer", "verifier"} and set(node.allowed_effects) != {ToolEffect.READ_ONLY}:
                raise PlanValidationError(f"{node.role} node must be read-only")
        self._assert_acyclic(plan)
        writers = [node for node in plan.nodes if node.is_writer]
        if profile.requested_mode is RequestedMode.ULTRA and len(writers) != 1:
            raise PlanValidationError("Ultra V1 requires exactly one logical Writer")
        if profile.selected_route is SelectedRoute.ULTRA_READ_FANOUT:
            explorers = [node for node in plan.nodes if node.role in {"explorer", "reviewer"}]
            if len(explorers) < 2:
                raise PlanValidationError("ultra-read-fanout requires at least two read-only nodes")
            writer = writers[0]
            explorer_ids = {node.node_id for node in explorers}
            if not explorer_ids.issubset(set(writer.dependencies)):
                raise PlanValidationError("fan-out Writer must depend on every read-only explorer")
            verifiers = [node for node in plan.nodes if node.role == "verifier"]
            if len(verifiers) != 1 or writer.node_id not in verifiers[0].dependencies:
                raise PlanValidationError("fan-out requires one Verifier after the Writer")
        if profile.selected_route is SelectedRoute.STANDARD_SINGLE and len(plan.nodes) != 1:
            raise PlanValidationError("standard-single requires exactly one PlanNode")
        if len(plan.nodes) > profile.max_agents:
            raise PlanValidationError("Plan exceeds max_agents")

    @staticmethod
    def _assert_acyclic(plan: PlanVersion) -> None:
        edges = {node.node_id: set(node.dependencies) for node in plan.nodes}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise PlanValidationError("PlanGraph contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in edges[node_id]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in edges:
            visit(node_id)


class RunCoordinator:
    def __init__(self, *, artifact_store: RuntimeArtifactStore | None = None) -> None:
        self.artifact_store = artifact_store or RuntimeArtifactStore()

    def run(
        self,
        *,
        contract: TaskContract,
        profile: ExecutionProfile,
        topology: TopologyDecision,
        plan: PlanVersion,
        model: Any,
        tools: ToolRegistry,
        messages: list[dict[str, Any]],
        cwd: str,
        runtime: dict[str, Any],
        permissions: Any | None,
        session: Any | None,
        agent_turn_kwargs: dict[str, Any],
    ) -> RuntimeOutcome:
        state_path = None
        configured_state = runtime.get("runtimeStatePath")
        if configured_state:
            state_path = Path(str(configured_state))
        else:
            from minicode.config import MINI_CODE_DIR

            state_path = (
                MINI_CODE_DIR
                / "runtime-v2"
                / "runs"
                / f"{contract.envelope.run_id}.json"
            )
        amendment = runtime.get("_taskAmendment")
        amended_plan = runtime.get("_amendedPlan")
        state_store = RuntimeStateStore(state_path)
        persisted = state_store.load() if _as_bool(runtime.get("runtimeResume", False)) else None
        if persisted is not None:
            reducer = RuntimeReducer.restore(persisted, store=state_store)
            if reducer.run_id != contract.envelope.run_id:
                raise ValueError("persisted Runtime run_id does not match TaskEnvelope")
            expected_revision = (
                amendment.base_contract_revision
                if isinstance(amendment, TaskAmendment)
                else contract.contract_revision
            )
            if reducer.snapshot()["contract_revision"] != expected_revision:
                raise ValueError("persisted Runtime contract revision does not match TaskContract")
        else:
            reducer = RuntimeReducer(plan, store=state_store)

        if amendment is not None or amended_plan is not None:
            if not isinstance(amendment, TaskAmendment) or not isinstance(amended_plan, PlanVersion):
                raise TypeError("_taskAmendment and _amendedPlan must be supplied together")
            if contract.contract_revision != amendment.next_contract_revision:
                raise ValueError("TaskContract does not match amendment next revision")
            reducer.install_plan(amended_plan, amendment)
        for pending_command in runtime.get("_runtimeCommands", ()):
            if not isinstance(pending_command, RuntimeCommand):
                raise TypeError("_runtimeCommands entries must be RuntimeCommand objects")
            reducer.apply(pending_command)
        PlanValidator().validate(reducer.current_plan, profile)
        injected_cancellation = runtime.get("_cancellationEvent")
        cancellation = (
            injected_cancellation
            if callable(getattr(injected_cancellation, "is_set", None))
            and callable(getattr(injected_cancellation, "set", None))
            else threading.Event()
        )
        scheduler = TaskScheduler(reducer, profile, cancellation_event=cancellation)

        def model_factory(worker_tools: ToolRegistry):
            injected_factory = runtime.get("_workerModelFactory")
            if callable(injected_factory):
                return injected_factory(worker_tools)
            from minicode.model_registry import create_model_adapter

            return create_model_adapter(
                model=str(runtime.get("model", "")),
                tools=worker_tools,
                runtime=runtime,
            )

        runtime_context = SimpleNamespace(delegation_gateway=None)
        nodes_by_id = {node.node_id: node for node in reducer.current_plan.nodes}
        depth_cache: dict[str, int] = {}

        def node_depth(node_id: str) -> int:
            cached = depth_cache.get(node_id)
            if cached is not None:
                return cached
            dependencies = nodes_by_id[node_id].dependencies
            depth = (
                0
                if not dependencies
                else 1 + max(node_depth(dependency) for dependency in dependencies)
            )
            depth_cache[node_id] = depth
            return depth

        layer_widths: dict[int, int] = {}
        for planned_node in reducer.current_plan.nodes:
            depth = node_depth(planned_node.node_id)
            layer_widths[depth] = layer_widths.get(depth, 0) + 1
        runtime["_memoryParallelSubtasks"] = max(layer_widths.values(), default=1)
        runtime["_memoryRestartRecoveryRequired"] = _as_bool(
            runtime.get("runtimeResume", False)
        )
        runtime["_memoryAuditRequired"] = (
            profile.verification_level is VerificationLevel.STRICT
        )
        worker = AgentLoopWorkerRunner(
            model=model,
            tools=tools,
            messages=messages,
            cwd=cwd,
            runtime=runtime,
            permissions=permissions,
            session=session,
            artifact_store=self.artifact_store,
            cancellation_event=cancellation,
            model_factory=model_factory,
            runtime_context=runtime_context,
            isolate_writes=profile.write_isolation is WriteIsolation.TEMPORARY_WORKSPACE,
            temporary_root=Path(str(runtime["runtimeTemporaryRoot"])) if runtime.get("runtimeTemporaryRoot") else None,
            agent_turn_kwargs=agent_turn_kwargs,
        )

        node_result_verifier = NodeResultVerifier(
            self.artifact_store,
            strict=profile.verification_level is VerificationLevel.STRICT,
        )

        def verify_node(result: AgentResult) -> VerificationRecord:
            context = VerificationContext(
                contract=contract,
                plan=reducer.current_plan,
                workspace=Path(cwd),
                result=result,
            )
            return node_result_verifier.verify(context)

        def context_builder(node: PlanNode, attempt_id: str, budget: BudgetEnvelope) -> ContextManifest:
            if profile.selected_route is SelectedRoute.STANDARD_SINGLE and node.node_id == "root":
                snapshot = {}
                base_revision = _quick_base_revision(Path(cwd))
            else:
                snapshot = workspace_snapshot(Path(cwd))
                base_revision = workspace_revision(Path(cwd))
            artifacts = tuple(
                artifact
                for dependency in node.dependencies
                for result in [worker.result_for(dependency)]
                if result is not None
                for artifact in result.artifact_refs
            )
            return ContextManifest(
                contract_revision=contract.contract_revision,
                plan_version=reducer.current_plan.plan_version,
                node_id=node.node_id,
                base_revision=base_revision,
                included_files=snapshot,
                dependency_result_refs=node.dependencies,
                artifact_refs=artifacts,
                memory_record=(
                    str(getattr(session, "session_id", "")) or None
                    if session is not None
                    else None
                ),
                tool_capability_scope=node.allowed_effects,
                budget_allocation=budget,
            )

        runtime_context.delegation_gateway = SchedulerDelegationGateway(
            scheduler=scheduler,
            worker=worker,
            context_builder=context_builder,
            node_verifier=verify_node,
        )
        report = scheduler.execute(
            worker,
            context_builder=context_builder,
            node_verifier=verify_node,
        )
        if profile.selected_route is SelectedRoute.STANDARD_SINGLE:
            standard_error = worker.exception_for("root")
            if standard_error is not None:
                raise standard_error
        snapshot = reducer.snapshot()
        final_records = self._final_verification(
            contract=contract,
            plan=reducer.current_plan,
            workspace=Path(cwd),
            report=report,
            runtime=runtime,
        )
        results: list[AgentResult] = []
        for node in reducer.current_plan.nodes:
            result = worker.result_for(node.node_id) or reducer.node_result(node.node_id)
            if result is not None:
                results.append(result)
        artifacts = tuple(artifact for result in results for artifact in result.artifact_refs)
        preferred_node = (
            "root"
            if profile.selected_route is SelectedRoute.STANDARD_SINGLE
            else "exclusive-writer"
            if profile.selected_route is SelectedRoute.ULTRA_READ_FANOUT
            else "deep-worker"
        )
        preferred = worker.result_for(preferred_node) or reducer.node_result(preferred_node)
        summary = preferred.output_summary if preferred else "Runtime did not produce a deliverable Worker result"
        delivery = CompletionGate().evaluate(
            contract=contract,
            plan=reducer.current_plan,
            runtime_snapshot=snapshot,
            workspace=Path(cwd),
            verification_records=final_records,
            level=profile.verification_level,
            output_summary=summary,
            result_refs=(result.node_id for result in results),
            artifact_refs=artifacts,
        )
        if profile.selected_route is SelectedRoute.STANDARD_SINGLE:
            if delivery.completed:
                output_messages = worker.messages_for("root")
                if output_messages is None and preferred is not None:
                    persisted_output = preferred.output_summary
                    if preferred.artifact_refs:
                        try:
                            persisted_output = self.artifact_store.get_text(
                                preferred.artifact_refs[0]
                            )
                        except (ArtifactIntegrityError, UnicodeDecodeError):
                            persisted_output = preferred.output_summary
                    output_messages = [
                        *messages,
                        {"role": "assistant", "content": persisted_output},
                    ]
                if output_messages is None:
                    output_messages = messages
            else:
                output_messages = [
                    *messages,
                    {
                        "role": "assistant",
                        "content": (
                            "Runtime completion gate rejected delivery: "
                            + "; ".join(delivery.reasons)
                        ),
                    },
                ]
        else:
            final_text = summary
            if not delivery.completed:
                final_text = "Runtime completion gate rejected delivery: " + "; ".join(delivery.reasons)
            output_messages = [*messages, {"role": "assistant", "content": final_text}]
        outcome = RuntimeOutcome(
            contract=contract,
            profile=profile,
            topology=topology,
            plan=reducer.current_plan,
            scheduler_report=report,
            delivery=delivery,
            runtime_snapshot=snapshot,
            context_manifests=worker.context_manifests(),
            messages=tuple(output_messages),
        )
        self._record(outcome, runtime=runtime, session=session)
        if profile.selected_route is not SelectedRoute.STANDARD_SINGLE:
            self._publish_delivery(
                agent_turn_kwargs=agent_turn_kwargs,
                messages=output_messages,
                completed=delivery.completed,
            )
        return outcome

    @staticmethod
    def _publish_delivery(
        *,
        agent_turn_kwargs: Mapping[str, Any],
        messages: list[dict[str, Any]],
        completed: bool,
    ) -> None:
        """Publish exactly one coordinator-owned terminal event sequence."""

        content = ""
        for message in reversed(messages):
            if message.get("role") == "assistant" and str(message.get("content", "")).strip():
                content = str(message["content"])
                break
        callbacks = agent_turn_kwargs.get("callbacks")

        def publish(event: TurnEvent) -> None:
            if callbacks is None:
                return
            try:
                on_event = getattr(callbacks, "on_event", None)
                if callable(on_event):
                    on_event(event)
                    return
                if event.kind == "assistant":
                    handler = getattr(callbacks, "on_assistant_message", None)
                    if callable(handler):
                        handler(event.content)
            except Exception:  # noqa: BLE001 - UI callbacks never own Runtime success
                return

        if completed and content:
            publish(TurnEvent.assistant_message(step=None, content=content))
            legacy_assistant = agent_turn_kwargs.get("on_assistant_message")
            if callable(legacy_assistant):
                legacy_assistant(content)
        publish(
            TurnEvent.completed(
                step=None,
                messages=cast(list[ChatMessage], messages),
                stop_reason="done" if completed else "verification_failed",
            )
        )

    @staticmethod
    def _final_verification(
        *,
        contract: TaskContract,
        plan: PlanVersion,
        workspace: Path,
        report: SchedulerReport,
        runtime: Mapping[str, Any],
    ) -> tuple[VerificationRecord, ...]:
        injected = runtime.get("_finalVerifierRegistry")
        if isinstance(injected, VerifierRegistry):
            return injected.run(
                VerificationContext(contract=contract, plan=plan, workspace=workspace, final=True),
                allow_model=False,
            )
        registry = VerifierRegistry(
            (
                CallableVerifier(
                    name="integrated-runtime-state",
                    kind="exit_code_schema",
                    callback=lambda context: (
                        report.completed,
                        "all current Plan nodes succeeded against the integrated workspace"
                        if report.completed
                        else "; ".join(report.reasons)
                        or "one or more current Plan nodes did not succeed",
                    ),
                ),
            )
        )
        configured = runtime.get("runtimeVerificationCommands", ())
        if configured and not isinstance(configured, (list, tuple)):
            raise TypeError("runtimeVerificationCommands must be a list")
        for index, item in enumerate(configured):
            if not isinstance(item, Mapping):
                raise TypeError("each runtime verification command must be a mapping")
            raw_command = item.get("command")
            if not isinstance(raw_command, (list, tuple)) or not raw_command:
                raise TypeError("verification command must be a non-empty argument list")
            registry.register(
                IsolatedCommandVerifier(
                    name=str(item.get("name") or f"verification-command-{index + 1}"),
                    kind=str(item.get("kind") or "integration_test"),
                    command=tuple(str(part) for part in raw_command),
                    timeout=float(item.get("timeout", 300.0)),
                )
            )
        return registry.run(
            VerificationContext(contract=contract, plan=plan, workspace=workspace, final=True),
            allow_model=False,
        )

    @staticmethod
    def _record(outcome: RuntimeOutcome, *, runtime: dict[str, Any], session: Any | None) -> None:
        trace = outcome.trace()
        runtime["lastRuntimeTrace"] = trace
        if session is not None:
            runs = getattr(session, "runtime_runs", None)
            if isinstance(runs, list):
                runs.append(trace)


def run_runtime_turn(**kwargs) -> list[dict[str, Any]]:
    """Compatibility-shaped product entry for the unified Runtime."""
    model = kwargs.pop("model")
    tools = kwargs.pop("tools")
    messages = list(kwargs.pop("messages"))
    cwd = str(kwargs.pop("cwd"))
    permissions = kwargs.pop("permissions", None)
    session = kwargs.pop("session", None)
    supplied_runtime = kwargs.pop("runtime", None)
    runtime = supplied_runtime if supplied_runtime is not None else {}

    if not _as_bool(runtime.get("runtimeV2", True)):
        from minicode.agent_loop import run_agent_turn

        return [
            dict(message)
            for message in run_agent_turn(
                model=model,
                tools=tools,
                messages=messages,
                cwd=cwd,
                permissions=permissions,
                session=session,
                runtime=runtime,
                **kwargs,
            )
        ]

    mode = ProfileResolver.requested_mode(runtime)
    topology = TopologyResolver().decide(
        requested_mode=mode,
        request=_last_user_request(messages),
        runtime=runtime,
    )
    profile = ProfileResolver().resolve(
        runtime=runtime,
        selected_route=topology.selected_route,
        request=_last_user_request(messages),
    )
    envelope = TaskEnvelope(
        run_id=str(runtime.get("runtimeRunId") or new_id("run")),
        request=_last_user_request(messages),
        workspace=str(Path(cwd).resolve()),
        requested_profile=mode,
        session_id=str(getattr(session, "session_id", "") or "") or None,
    )
    if (
        envelope.session_id is None
        and not str(runtime.get("_memoryTaskId") or runtime.get("memoryTaskId") or "").strip()
    ):
        # Fan-out Workers copy the Runtime mapping before paged memory starts.
        # Seed one task identity on the coordinator-owned mapping so every copy
        # writes to the same authority scope instead of allocating per Worker.
        runtime["_memoryTaskId"] = f"task_{envelope.run_id}"
    contract = TaskContract(
        envelope=envelope,
        contract_revision=max(1, int(runtime.get("runtimeContractRevision", 1) or 1)),
        constraints=tuple(str(item) for item in runtime.get("taskConstraints", ()) if str(item)),
        acceptance_criteria=tuple(
            str(item) for item in runtime.get("acceptanceCriteria", ()) if str(item)
        ),
        expected_outputs=tuple(str(item) for item in runtime.get("expectedOutputs", ()) if str(item)),
        budget=profile.budget,
    )
    plan = PlanCompiler().compile(contract, profile, topology)
    canonical_graph = CanonicalPlanGraph(plan)
    PlanValidator().validate(canonical_graph.current, profile)
    progress = kwargs.get("on_progress_message")
    if callable(progress):
        progress(
            f"Runtime profile={profile.requested_mode.value}; route={profile.selected_route.value}; "
            f"workers<={profile.max_active_workers}; verification={profile.verification_level.value}"
        )
        if mode is RequestedMode.ULTRA and topology.selected_route is SelectedRoute.DEEP_SINGLE:
            progress(topology.reasons[0])
    outcome = RunCoordinator().run(
        contract=contract,
        profile=profile,
        topology=topology,
        plan=canonical_graph.current,
        model=model,
        tools=tools,
        messages=messages,
        cwd=cwd,
        runtime=runtime,
        permissions=permissions,
        session=session,
        agent_turn_kwargs=kwargs,
    )
    return list(outcome.messages)


def _last_user_request(messages: Iterable[Mapping[str, Any]]) -> str:
    materialized = list(messages)
    for message in reversed(materialized):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _finite_deadline(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("runtimeDeadline must be a finite timestamp") from error
    if not math.isfinite(parsed):
        raise ValueError("runtimeDeadline must be a finite timestamp")
    return parsed


def _strict_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return False


def _topology_score(value: Any, *, default: float, invalid: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return invalid
    return parsed if math.isfinite(parsed) and 0.0 <= parsed <= 1.0 else invalid


def _quick_base_revision(workspace: Path) -> str:
    # Standard is the latency-sensitive compatibility path.  Spawning Git for
    # every turn costs around 100 ms on Windows and HEAD alone would not bind
    # dirty workspace content anyway.  Tool calls that need exact mutation
    # safety carry their own operation digest; this marker only identifies the
    # fast-path workspace generation without adding a subprocess/model call.
    marker = f"{workspace.resolve()}:{workspace.stat().st_mtime_ns if workspace.exists() else 0}"
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()
