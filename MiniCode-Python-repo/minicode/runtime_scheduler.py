"""The sole Worker scheduler for Standard and Ultra execution profiles."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from functools import partial
from typing import Protocol

from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    BudgetEnvelope,
    ContextManifest,
    DelegationRequest,
    ExecutionFailure,
    ExecutionFailureKind,
    ExecutionProfile,
    NodeState,
    PlanNode,
    RuntimeCommandType,
    ToolEffect,
    VerificationRecord,
    new_id,
)
from minicode.runtime_state import Clock, RuntimeReducer, SystemClock, command


class Worker(Protocol):
    def run(self, invocation: AgentInvocationSpec) -> AgentResult: ...


ContextBuilder = Callable[[PlanNode, str, BudgetEnvelope], ContextManifest]
NodeVerifier = Callable[[AgentResult], VerificationRecord]


@dataclass(frozen=True, slots=True)
class SchedulerReport:
    run_id: str
    completed: bool
    canceled: bool
    claimed_attempts: int
    peak_active_workers: int
    peak_active_writers: int
    active_leases: int
    tokens_used: int
    cost_used: float
    result_node_ids: tuple[str, ...]
    reasons: tuple[str, ...]


class TaskScheduler:
    """Claim, launch, retry, and cancel all Workers through one authority."""

    def __init__(
        self,
        reducer: RuntimeReducer,
        profile: ExecutionProfile,
        *,
        clock: Clock | None = None,
        lease_seconds: float = 300.0,
        cancellation_event: threading.Event | None = None,
        cancellation_grace_seconds: float = 1.0,
    ) -> None:
        self.reducer = reducer
        self.profile = profile
        self.clock = clock or SystemClock()
        self.lease_seconds = lease_seconds
        self.cancellation_event = cancellation_event or threading.Event()
        self.cancellation_grace_seconds = max(0.0, cancellation_grace_seconds)
        self._write_lock = threading.Lock()
        self._running_lock = threading.Lock()
        self._active_workers = 0
        self._active_writers = 0
        self._peak_active_workers = 0
        self._peak_active_writers = 0
        self._claimed_attempts = 0
        self._claimed_nodes: set[str] = set()
        self._idempotency_keys: dict[str, str] = {}
        restored_snapshot = reducer.snapshot()
        for node_id, runtime in restored_snapshot.get("nodes", {}).items():
            attempts = runtime.get("attempts", []) if isinstance(runtime, dict) else []
            if attempts and attempts[0].get("idempotency_key"):
                self._idempotency_keys[str(node_id)] = str(attempts[0]["idempotency_key"])
            if attempts:
                self._claimed_nodes.add(str(node_id))
        self._tokens_used = max(0, int(restored_snapshot.get("tokens_used", 0) or 0))
        self._cost_used = max(0.0, float(restored_snapshot.get("cost_used", 0.0) or 0.0))
        self._reasons: list[str] = []

    @property
    def active_workers(self) -> int:
        with self._running_lock:
            return self._active_workers

    @property
    def active_writers(self) -> int:
        with self._running_lock:
            return self._active_writers

    def cancel(self, reason: str = "canceled") -> None:
        self.cancellation_event.set()
        self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.CANCEL_RUN,
                {"reason": reason},
            )
        )

    def execute_delegation(
        self,
        request: DelegationRequest,
        *,
        worker: Worker,
        context_builder: ContextBuilder,
        node_verifier: NodeVerifier,
    ) -> AgentResult:
        """Run a Standard compatibility child in the suspended parent slot.

        The child is appended to the canonical PlanGraph, claimed and fenced by
        this Scheduler, and reduced like every preplanned node.  Because the
        parent calls this method synchronously, max_active_workers=1 remains
        true even though the child has its own Node/Attempt identity.
        """
        if not self.profile.dynamic_worker_delegation:
            return self._delegation_denied(request, "dynamic Worker delegation is disabled for this profile")
        if request.delegation_depth > self.profile.max_delegation_depth:
            return self._delegation_denied(request, "max_delegation_depth exceeded")
        if self.cancellation_event.is_set():
            return self._delegation_denied(request, "parent run is canceled", canceled=True)
        if self._budget_exhausted():
            reason = self._reasons[-1]
            self.cancel(reason)
            return self._delegation_denied(request, reason, canceled=True)
        digest = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:16]
        node_id = f"delegated-{digest}"
        if node_id not in self._claimed_nodes and len(self._claimed_nodes) >= self.profile.max_agents:
            return self._delegation_denied(request, "max_agents budget exhausted")
        role = "explorer" if request.requested_role in {"explore", "plan"} else "writer"
        node = PlanNode(
            node_id=node_id,
            objective=request.objective,
            role=role,
            allowed_effects=request.allowed_tool_capabilities,
            acceptance_criteria=("return a structured result to the parent Worker",),
            max_attempts=max(1, min(1 + self.profile.retry_budget, 2)),
        )
        self.reducer.register_delegated_node(node, parent_node_id=request.parent_node_id)
        claimed = self._claim(node, idempotency_key=request.idempotency_key)
        if claimed is None:
            return self._delegation_denied(request, "Scheduler could not claim delegated node")
        attempt_id, fencing_token = claimed
        manifest = context_builder(node, attempt_id, request.proposed_budget)
        invocation = AgentInvocationSpec(
            run_id=request.run_id,
            plan_version=self.reducer.current_plan.plan_version,
            node_id=node.node_id,
            attempt_id=attempt_id,
            role=node.role,
            objective=node.objective,
            context_manifest=manifest,
            allowed_effects=node.allowed_effects,
            budget=request.proposed_budget,
            acceptance_criteria=node.acceptance_criteria,
            idempotency_key=request.idempotency_key,
        )
        # The active parent is suspended on this call; do not increment the
        # active Worker gauge or create a second executor.
        result = worker.run(invocation)
        self._account_usage(result)
        events = self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.SUBMIT_RESULT,
                {"result": result, "fencing_token": fencing_token},
            )
        )
        if not any(event.event_type.value == "ResultAccepted" for event in events):
            self._discard_worker_result(worker, result)
            return result
        if result.status is not AgentResultStatus.SUCCEEDED:
            self._discard_worker_result(worker, result)
            if result.status is AgentResultStatus.FAILED_RETRYABLE:
                self.reducer.prepare_retry(node.node_id)
            return result
        commit_error = self._commit_worker_result(worker, node, result, fencing_token)
        if commit_error is not None:
            self._fail_workspace_commit(node.node_id, commit_error)
            terminal = self.reducer.node_state(node.node_id) is NodeState.FAILED_TERMINAL
            return replace(
                result,
                status=(
                    AgentResultStatus.FAILED_TERMINAL
                    if terminal
                    else AgentResultStatus.FAILED_RETRYABLE
                ),
                output_summary="workspace commit failed",
                failure=ExecutionFailure(
                    ExecutionFailureKind.VERIFICATION_FAILED,
                    commit_error,
                    retryable=not terminal,
                ),
            )
        self._verify_result(node.node_id, result, node_verifier)
        return result

    def execute(
        self,
        worker: Worker,
        *,
        context_builder: ContextBuilder,
        node_verifier: NodeVerifier,
    ) -> SchedulerReport:
        max_workers = max(1, self.profile.max_active_workers)
        active: dict[Future[AgentResult], tuple[PlanNode, AgentInvocationSpec, int]] = {}
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="minicode-worker")
        cancellation_deadline: float | None = None
        try:
            while True:
                if self.cancellation_event.is_set() and not self.reducer.snapshot().get("canceled"):
                    self.reducer.apply(
                        command(
                            self.reducer.run_id,
                            RuntimeCommandType.CANCEL_RUN,
                            {"reason": "parent cancellation requested"},
                        )
                    )
                if self.cancellation_event.is_set():
                    if cancellation_deadline is None:
                        cancellation_deadline = (
                            time.monotonic() + self.cancellation_grace_seconds
                        )
                    if active and time.monotonic() >= cancellation_deadline:
                        self._reasons.append(
                            "cancellation grace expired with "
                            f"{len(active)} Worker(s) still running"
                        )
                        break
                if not active:
                    self._recover_verifying(node_verifier)
                    self._prepare_retries()
                    self._recover_expired_attempts()
                    if self.reducer.terminal():
                        break
                if not self.cancellation_event.is_set() and self._budget_exhausted():
                    self.cancel(self._reasons[-1])

                if self.cancellation_event.is_set() and not active:
                    break

                capacity = max_workers - len(active)
                if capacity > 0 and not self.cancellation_event.is_set():
                    for node_id in self.reducer.ready_node_ids():
                        if capacity <= 0:
                            break
                        node = self._node_definition(node_id)
                        if not self._can_claim(node):
                            continue
                        claimed = self._claim(node)
                        if claimed is None:
                            continue
                        attempt_id, fencing_token = claimed
                        manifest = context_builder(node, attempt_id, self.profile.budget)
                        invocation = AgentInvocationSpec(
                            run_id=self.reducer.run_id,
                            plan_version=self.reducer.current_plan.plan_version,
                            node_id=node.node_id,
                            attempt_id=attempt_id,
                            role=node.role,
                            objective=node.objective,
                            context_manifest=manifest,
                            allowed_effects=node.allowed_effects,
                            budget=self.profile.budget,
                            acceptance_criteria=node.acceptance_criteria,
                            idempotency_key=self._idempotency_keys.get(node.node_id, ""),
                        )
                        future = executor.submit(self._run_worker, worker, invocation, node.is_writer)
                        active[future] = (node, invocation, fencing_token)
                        capacity -= 1

                if not active:
                    if self.reducer.terminal() or self.cancellation_event.is_set():
                        break
                    blocked_reason = self._blocked_reason()
                    self._reasons.append(blocked_reason)
                    break

                wait_timeout = self._wait_timeout()
                if cancellation_deadline is not None:
                    wait_timeout = min(
                        wait_timeout,
                        max(0.0, cancellation_deadline - time.monotonic()),
                    )
                done, _ = wait(
                    tuple(active),
                    timeout=wait_timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue
                for future in done:
                    node, invocation, fencing_token = active.pop(future)
                    result = self._future_result(future, node, invocation)
                    self._account_usage(result)
                    events = self.reducer.apply(
                        command(
                            self.reducer.run_id,
                            RuntimeCommandType.SUBMIT_RESULT,
                            {"result": result, "fencing_token": fencing_token},
                        )
                    )
                    accepted = any(event.event_type.value == "ResultAccepted" for event in events)
                    if not accepted:
                        self._discard_worker_result(worker, result)
                        if (
                            not self.cancellation_event.is_set()
                            and self.reducer.node_state(node.node_id) is not NodeState.CANCELED
                        ):
                            self.reducer.expire_attempt(node.node_id)
                        continue
                    if result.status is AgentResultStatus.FAILED_RETRYABLE:
                        self._discard_worker_result(worker, result)
                        self.reducer.prepare_retry(node.node_id)
                        continue
                    if result.status is not AgentResultStatus.SUCCEEDED:
                        self._discard_worker_result(worker, result)
                        continue
                    commit_error = self._commit_worker_result(
                        worker,
                        node,
                        result,
                        fencing_token,
                    )
                    if commit_error is not None:
                        self._fail_workspace_commit(node.node_id, commit_error)
                        continue
                    self._verify_result(node.node_id, result, node_verifier)
        finally:
            for future in active:
                if not future.cancel():
                    future.add_done_callback(
                        partial(self._discard_late_future, worker)
                    )
            executor.shutdown(wait=not active, cancel_futures=True)
            self._retry_worker_cleanup(worker)

        snapshot = self.reducer.snapshot()
        result_nodes = tuple(
            sorted(
                node_id
                for node_id, runtime in snapshot["nodes"].items()
                if runtime.get("result") is not None
            )
        )
        completed = bool(snapshot["nodes"]) and all(
            runtime.get("state") == NodeState.SUCCEEDED.value
            for runtime in snapshot["nodes"].values()
        )
        active_leases = sum(
            1
            for runtime in snapshot["nodes"].values()
            for attempt in runtime.get("attempts", [])
            if attempt.get("state") in {"CLAIMED", "RUNNING", "WAITING_APPROVAL", "WAITING_INPUT", "VERIFYING"}
            and float(attempt.get("lease", {}).get("lease_expiry", 0.0)) > self.clock.now()
        )
        return SchedulerReport(
            run_id=self.reducer.run_id,
            completed=completed,
            canceled=bool(snapshot.get("canceled")),
            claimed_attempts=self._claimed_attempts,
            peak_active_workers=self._peak_active_workers,
            peak_active_writers=self._peak_active_writers,
            active_leases=active_leases,
            tokens_used=self._tokens_used,
            cost_used=self._cost_used,
            result_node_ids=result_nodes,
            reasons=tuple(self._reasons),
        )

    def _node_definition(self, node_id: str) -> PlanNode:
        for node in self.reducer.current_plan.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def _can_claim(self, node: PlanNode) -> bool:
        if node.node_id not in self._claimed_nodes and len(self._claimed_nodes) >= self.profile.max_agents:
            self._reasons.append("max_agents budget exhausted")
            return False
        return not (node.is_writer and self.active_writers > 0)

    def _claim(
        self,
        node: PlanNode,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[str, int] | None:
        stable_key = self._idempotency_keys.setdefault(
            node.node_id,
            idempotency_key or new_id("worker"),
        )
        events = self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.CLAIM_NODE,
                {
                    "node_id": node.node_id,
                    "plan_version": self.reducer.current_plan.plan_version,
                    "lease_owner": f"local:{threading.get_ident()}",
                    "lease_seconds": self.lease_seconds,
                    "idempotency_key": stable_key,
                },
            )
        )
        if not any(event.event_type.value == "NodeClaimed" for event in events):
            return None
        attempt = self.reducer.current_attempt(node.node_id)
        if attempt is None:
            return None
        started = self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.START_ATTEMPT,
                {"node_id": node.node_id, "attempt_id": attempt.attempt_id},
            )
        )
        if not any(event.event_type.value == "AttemptStarted" for event in started):
            return None
        self._claimed_attempts += 1
        self._claimed_nodes.add(node.node_id)
        return attempt.attempt_id, attempt.lease.fencing_token.value

    def _verify_result(
        self,
        node_id: str,
        result: AgentResult,
        node_verifier: NodeVerifier,
    ) -> None:
        self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.BEGIN_VERIFICATION,
                {"node_id": node_id},
            )
        )
        try:
            record = node_verifier(result)
        except Exception as error:  # noqa: BLE001 - verifier adapters are isolated.
            record = VerificationRecord(
                verifier="scheduler-node-verifier",
                passed=False,
                scope=f"node:{node_id}",
                details=f"verifier crashed ({type(error).__name__}): {error}",
            )
        if record.passed:
            self.reducer.apply(
                command(
                    self.reducer.run_id,
                    RuntimeCommandType.ACCEPT_VERIFICATION,
                    {"node_id": node_id, "record": record.to_dict()},
                )
            )
            return
        self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.FAIL_VERIFICATION,
                {"node_id": node_id, "reason": record.details},
            )
        )
        self.reducer.prepare_retry(node_id)

    def _commit_worker_result(
        self,
        worker: Worker,
        node: PlanNode,
        result: AgentResult,
        fencing_token: int,
    ) -> str | None:
        if not node.is_writer:
            return None
        if self.cancellation_event.is_set():
            self._discard_worker_result(worker, result)
            return "run was canceled before workspace commit"

        commit_result = getattr(worker, "commit_result", None)

        def commit() -> tuple[str, ...]:
            if callable(commit_result):
                return tuple(commit_result(result))
            # Compatibility Workers may already apply their write in run().
            return result.changed_resources

        try:
            self.reducer.commit_workspace(
                node.node_id,
                attempt_id=result.attempt_id,
                fencing_token=fencing_token,
                commit=commit,
            )
        except Exception as error:  # noqa: BLE001 - workspace adapters vary.
            self._discard_worker_result(worker, result)
            return f"workspace commit failed ({type(error).__name__}): {error}"
        return None

    def _fail_workspace_commit(self, node_id: str, reason: str) -> None:
        self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.FAIL_VERIFICATION,
                {"node_id": node_id, "reason": reason},
            )
        )
        self.reducer.prepare_retry(node_id)

    def _discard_worker_result(self, worker: Worker, result: AgentResult) -> None:
        discard = getattr(worker, "discard_result", None)
        if not callable(discard):
            return
        try:
            discard(result)
        except Exception as error:  # noqa: BLE001 - cleanup is best effort.
            self._reasons.append(
                f"workspace discard failed ({type(error).__name__}): {error}"
            )

    def _retry_worker_cleanup(self, worker: Worker) -> None:
        retry_cleanup = getattr(worker, "retry_cleanup", None)
        if not callable(retry_cleanup):
            return
        try:
            failures = tuple(retry_cleanup())
        except Exception as error:  # noqa: BLE001 - cleanup adapters vary.
            self._reasons.append(
                f"workspace cleanup retry crashed ({type(error).__name__}): {error}"
            )
            return
        if failures:
            self._reasons.append(
                f"workspace cleanup remains pending for {len(failures)} handle(s)"
            )

    def _discard_late_future(
        self,
        worker: Worker,
        future: Future[AgentResult],
    ) -> None:
        try:
            result = future.result()
        except Exception:  # noqa: BLE001 - late worker failures are already terminal.
            return
        self._discard_worker_result(worker, result)
        self._retry_worker_cleanup(worker)

    def _recover_verifying(self, node_verifier: NodeVerifier) -> None:
        """Finish verification after a crash between Result and gate commit."""
        snapshot = self.reducer.snapshot()
        for node_id, runtime in snapshot.get("nodes", {}).items():
            if not isinstance(runtime, dict) or runtime.get("state") != NodeState.VERIFYING.value:
                continue
            result = self.reducer.node_result(str(node_id))
            if result is None:
                continue
            definition = runtime.get("definition", {})
            allowed_effects = (
                definition.get("allowed_effects", [])
                if isinstance(definition, dict)
                else []
            )
            is_writer = ToolEffect.WORKSPACE_WRITE.value in allowed_effects
            if is_writer and not bool(runtime.get("workspace_committed", False)):
                self._fail_workspace_commit(
                    str(node_id),
                    "workspace commit was not durably confirmed before restart",
                )
                continue
            self._verify_result(str(node_id), result, node_verifier)

    def _prepare_retries(self) -> None:
        snapshot = self.reducer.snapshot()
        for node_id, runtime in snapshot.get("nodes", {}).items():
            if isinstance(runtime, dict) and runtime.get("state") == NodeState.FAILED_RETRYABLE.value:
                self.reducer.prepare_retry(str(node_id))

    def _recover_expired_attempts(self) -> None:
        snapshot = self.reducer.snapshot()
        for node_id, runtime in snapshot.get("nodes", {}).items():
            if not isinstance(runtime, dict) or runtime.get("state") not in {
                NodeState.CLAIMED.value,
                NodeState.RUNNING.value,
            }:
                continue
            attempts = runtime.get("attempts", [])
            if not attempts:
                continue
            expiry = float(attempts[-1].get("lease", {}).get("lease_expiry", 0.0))
            if expiry <= self.clock.now():
                self.reducer.expire_attempt(str(node_id))

    def _blocked_reason(self) -> str:
        snapshot = self.reducer.snapshot()
        states = {
            str(runtime.get("state", ""))
            for runtime in snapshot.get("nodes", {}).values()
            if isinstance(runtime, dict)
        }
        if NodeState.WAITING_APPROVAL.value in states:
            return "run is waiting for approval"
        if NodeState.WAITING_INPUT.value in states:
            return "run is waiting for user input"
        if states & {NodeState.CLAIMED.value, NodeState.RUNNING.value}:
            return "a recovered Worker lease is still active"
        return "no runnable nodes remain"

    def _wait_timeout(self) -> float:
        deadline = self.profile.deadline
        if deadline is None:
            return 0.1
        return max(0.0, min(0.1, deadline - self.clock.now()))

    def _run_worker(
        self,
        worker: Worker,
        invocation: AgentInvocationSpec,
        is_writer: bool,
    ) -> AgentResult:
        write_acquired = False
        if is_writer:
            self._write_lock.acquire()
            write_acquired = True
        with self._running_lock:
            self._active_workers += 1
            self._peak_active_workers = max(self._peak_active_workers, self._active_workers)
            if is_writer:
                self._active_writers += 1
                self._peak_active_writers = max(self._peak_active_writers, self._active_writers)
        try:
            if self.cancellation_event.is_set():
                return self._canceled_result(invocation)
            return worker.run(invocation)
        finally:
            with self._running_lock:
                self._active_workers -= 1
                if is_writer:
                    self._active_writers -= 1
            if write_acquired:
                self._write_lock.release()

    def _future_result(
        self,
        future: Future[AgentResult],
        node: PlanNode,
        invocation: AgentInvocationSpec,
    ) -> AgentResult:
        try:
            return future.result()
        except Exception as error:  # noqa: BLE001 - worker exceptions become results.
            return AgentResult(
                run_id=self.reducer.run_id,
                plan_version=self.reducer.current_plan.plan_version,
                node_id=node.node_id,
                attempt_id=invocation.attempt_id,
                status=AgentResultStatus.FAILED_RETRYABLE,
                base_revision=invocation.context_manifest.base_revision,
                output_summary="worker crashed",
                contract_revision=invocation.context_manifest.contract_revision,
                failure=ExecutionFailure(
                    ExecutionFailureKind.TOOL_FAILED,
                    f"worker crashed ({type(error).__name__}): {error}",
                    retryable=True,
                ),
            )

    @staticmethod
    def _canceled_result(invocation: AgentInvocationSpec) -> AgentResult:
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=AgentResultStatus.CANCELED,
            base_revision=invocation.context_manifest.base_revision,
            output_summary="worker canceled before start",
            contract_revision=invocation.context_manifest.contract_revision,
            failure=ExecutionFailure(ExecutionFailureKind.CANCELED, "parent run canceled"),
        )

    def _account_usage(self, result: AgentResult) -> None:
        self._tokens_used += int(result.usage.get("tokens", 0) or 0)
        self._cost_used += float(result.usage.get("cost", 0.0) or 0.0)

    def _budget_exhausted(self) -> bool:
        budget = self.profile.budget
        if budget.deadline is not None and self.clock.now() >= budget.deadline:
            self._reasons.append("deadline exhausted")
            return True
        if budget.max_tokens is not None and self._tokens_used >= budget.max_tokens:
            self._reasons.append("max_tokens budget exhausted")
            return True
        if budget.max_cost is not None and self._cost_used >= budget.max_cost:
            self._reasons.append("max_cost budget exhausted")
            return True
        return False

    def _delegation_denied(
        self,
        request: DelegationRequest,
        reason: str,
        *,
        canceled: bool = False,
    ) -> AgentResult:
        return AgentResult(
            run_id=request.run_id,
            plan_version=self.reducer.current_plan.plan_version,
            node_id=request.parent_node_id,
            attempt_id="delegation-denied",
            status=AgentResultStatus.CANCELED if canceled else AgentResultStatus.FAILED_TERMINAL,
            base_revision="unknown",
            output_summary=reason,
            contract_revision=self.reducer.current_plan.contract_revision,
            failure=ExecutionFailure(
                ExecutionFailureKind.CANCELED if canceled else ExecutionFailureKind.POLICY_DENIED,
                reason,
            ),
        )
