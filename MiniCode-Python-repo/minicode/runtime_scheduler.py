"""The sole Worker scheduler for Standard, Deep, and Ultra execution profiles."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from minicode.candidate_ownership import (
    CandidateConflict,
    CandidateLease,
    CandidateRegistry,
    CapacityExhausted,
    CapacityReservation,
    ExecutionCapacity,
)
from minicode.candidate_write_scope import (
    CandidateWriteAccess,
    bind_candidate_write_access,
)
from minicode.path_safety import is_unsafe_stored_path
from minicode.candidate_patches import (
    APPLICATION_SCHEMA,
    APPLIED_SCHEMA,
    DEPENDENCY_PIN_SCHEMA,
    PATCH_APPLIED,
    PATCH_FAILED,
    PATCH_UNCERTAIN,
    AuthoritativeFacts,
    CandidatePatch,
    DependencyVector,
    PatchConflict,
    PatchLedger,
    decide_promotion,
)
from minicode.consultation_forecast import CONSULTATION_TOKEN_CAP
from minicode.dependency_waits import (
    DependencyEdge,
    PATCH_APPLIED_AND_CHECKED,
    ResultMailbox,
    TERMINAL_OBSERVED,
    TaskOutcome,
    WaitError,
    WaitSpec,
)
from minicode.native_read_set import NativeReadSet
from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    ArtifactRef,
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
    consultation_context_files,
    new_id,
)
from minicode.runtime_state import Clock, ReducerError, RuntimeReducer, SystemClock, command
from minicode.runtime_shutdown import ExecutionDrain
from minicode.usage_ledger import budget_stop_failure_kind


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
    cost_used: float | None
    result_node_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    model_calls: int = 0
    tokens_known: bool = True
    cost_known: bool = True
    reserved_tokens: int = 0
    reserved_cost: float = 0.0
    unsettled_task_ids: tuple[str, ...] = ()


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
        on_activity: Callable[[], None] | None = None,
        execution_drain: ExecutionDrain | None = None,
        review_snapshot: Callable[[AgentResult], Mapping[str, str]] | None = None,
    ) -> None:
        self.reducer = reducer
        self.on_activity = on_activity
        self.review_snapshot = review_snapshot
        self.profile = profile
        self.clock = clock or SystemClock()
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        self.lease_seconds = lease_seconds
        self.cancellation_event = cancellation_event or threading.Event()
        if not math.isfinite(cancellation_grace_seconds):
            raise ValueError("cancellation_grace_seconds must be finite")
        self.cancellation_grace_seconds = max(0.0, cancellation_grace_seconds)
        self.execution_drain = execution_drain if execution_drain is not None else ExecutionDrain()
        # Ownership is per candidate, never global. `_write_lock` used to make a
        # Worker's whole model+tool loop exclusive to every other writer; that
        # serialised independent candidates and made write rights and execution
        # slots the same resource. The two are now separate: CandidateRegistry
        # grants exactly one live writer per candidate and a SHORT mutation lock,
        # ExecutionCapacity reserves a global execution slot before any work is
        # handed to the executor.
        workers = max(1, self.profile.max_active_workers)
        self.capacity = ExecutionCapacity(
            max_workers=workers,
            max_writers=self.profile.effective_writer_capacity,
        )
        self.candidates = CandidateRegistry()
        from minicode.shared_writes import SharedWorkspaceWrites
        self.shared_writes = SharedWorkspaceWrites(self)
        self.mailbox = ResultMailbox()
        # Hydrated from the durable snapshot below, so rebuilding a Scheduler over
        # the same live Reducer does not silently start with an empty ledger.
        self.patch_ledger = PatchLedger(restore=reducer.patch_records())
        for patch_id, record in self.patch_ledger.snapshot().items():
            if record != reducer.patch_records().get(patch_id):
                reducer.record_patch_record(patch_id, {**reducer.patch_records().get(patch_id, {}), **record})
        self._candidate_revision = None
        self._candidate_changes = None
        self._running_lock = threading.Lock()
        self._dispatch_lock = threading.RLock()
        self._claimed_attempts = 0
        self._claimed_nodes: set[str] = set()
        self._returned_primary_attempts: set[str] = set()
        self._idempotency_keys: dict[str, str] = {}
        self._candidate_generations: dict[str, int] = {}
        self._fencing_tokens: dict[str, int] = {}
        self._builder_lifecycle: Any | None = None
        # Async builder registrations, keyed by the deterministic task id.
        self._builder_tasks: dict[str, dict[str, Any]] = {}
        self._waits: dict[str, WaitSpec] = {}
        self._dispatch_wake = threading.Event()
        self._hydrate_durable_orchestration()
        restored_snapshot = reducer.snapshot()
        for node_id, runtime in restored_snapshot.get("nodes", {}).items():
            attempts = runtime.get("attempts", []) if isinstance(runtime, dict) else []
            if attempts and attempts[0].get("idempotency_key"):
                self._idempotency_keys[str(node_id)] = str(attempts[0]["idempotency_key"])
            if attempts:
                self._claimed_nodes.add(str(node_id))
                self._fencing_tokens[str(node_id)] = int(attempts[-1]["lease"]["fencing_token"]["value"])
            if str(node_id).startswith("builder-"):
                definition = runtime["definition"]
                self._builder_tasks[str(node_id)] = {"task_id": node_id, "candidate_id": self.candidate_id_for(node_id),
                    "objective": definition["objective"], "depends_on": definition.get("dependencies", []), "parent_node_id": "primary"}
        for event in restored_snapshot.get("events", []):
            if event.get("event_type") == "CandidateLeaseObserved":
                lease = event["payload"]
                self.candidates.restore_fencing_floor(lease["candidate_id"], int(lease["fencing_token"]))
                self._candidate_generations[lease["candidate_id"]] = max(
                    self._candidate_generations.get(lease["candidate_id"], 0), int(lease["generation"]))
        self._tokens_used = max(0, int(restored_snapshot.get("tokens_used", 0) or 0))
        self._cost_used = max(0.0, float(restored_snapshot.get("cost_used", 0.0) or 0.0))
        self._reasons: list[str] = []
        # The reason a budget or deadline has stopped new work, while Workers that
        # are already running are still allowed to finish and be collected. It is
        # deliberately distinct from `cancellation_event`, which means "stop now".
        self._stop_requested: str | None = None
        # Only claims made by this Scheduler are eligible for heartbeats.
        # A restored attempt still belongs to its previous process.
        self._owned_leases: dict[str, tuple[str, str, int]] = {}
        self._lease_thread_lock = threading.Lock()
        self._lease_users = 0
        self._lease_stop: threading.Event | None = None
        self._lease_thread: threading.Thread | None = None
        self._quarantine_reservations: dict[str, CapacityReservation] = {}
        for attempt_id, record in reducer.candidate_quarantines().items():
            self.candidates.quarantine(record["candidate_id"])
            if record["status"] == "pending":
                self._quarantine_reservations[attempt_id] = self.capacity.reserve(
                    node_id=record["node_id"], kind="write", candidate_id=record["candidate_id"])

    @contextmanager
    def _maintain_leases(self) -> Iterator[None]:
        """Keep leases alive even while context preparation or delegation blocks."""
        with self._lease_thread_lock:
            if self._lease_users == 0:
                stop = threading.Event()
                self._lease_stop = stop
                self._lease_thread = threading.Thread(
                    target=self._lease_heartbeat,
                    args=(stop,),
                    name="minicode-lease-heartbeat",
                    daemon=True,
                )
                self._lease_thread.start()
            self._lease_users += 1
        try:
            yield
        finally:
            thread = None
            with self._lease_thread_lock:
                self._lease_users -= 1
                if self._lease_users == 0:
                    if self._lease_stop is not None:
                        self._lease_stop.set()
                    thread = self._lease_thread
                    self._lease_thread = None
                    self._lease_stop = None
            if thread is not None:
                thread.join()

    def _lease_heartbeat(self, stop: threading.Event) -> None:
        interval = min(10.0, self.lease_seconds / 3)
        while not stop.wait(interval):
            if self.cancellation_event.is_set():
                return
            try:
                with self._running_lock:
                    owned = tuple(self._owned_leases.items())
                for node_id, (attempt_id, owner, token) in owned:
                    if stop.is_set() or self.cancellation_event.is_set():
                        return
                    attempt = self.reducer.current_attempt(node_id)
                    if (
                        attempt is None
                        or attempt.attempt_id != attempt_id
                        or attempt.state not in {NodeState.CLAIMED, NodeState.RUNNING, NodeState.VERIFYING}
                    ):
                        continue
                    remaining = attempt.lease.lease_expiry - self.clock.now()
                    if remaining <= 0:
                        raise RuntimeError(f"active Worker lease expired: {node_id}")
                    if remaining > self.lease_seconds * 2 / 3:
                        continue
                    events = self.reducer.apply(command(
                        self.reducer.run_id,
                        RuntimeCommandType.RENEW_LEASE,
                        {
                            "node_id": node_id,
                            "attempt_id": attempt_id,
                            "lease_owner": owner,
                            "fencing_token": token,
                            "lease_seconds": self.lease_seconds,
                        },
                    ))
                    if not any(event.event_type.value == "LeaseRenewed" for event in events):
                        current = self.reducer.current_attempt(node_id)
                        if (current is not None and current.attempt_id == attempt_id
                            and current.state in {NodeState.CLAIMED, NodeState.RUNNING, NodeState.VERIFYING}):
                            raise RuntimeError(f"active Worker lease renewal was rejected: {node_id}")
            except Exception as error:  # noqa: BLE001 - any heartbeat failure must cancel the run.
                self._reasons.append(f"lease renewal failed ({type(error).__name__}): {error}")
                self.cancellation_event.set()
                return

    @property
    def active_workers(self) -> int:
        return self.capacity.occupied

    @property
    def active_writers(self) -> int:
        return self.capacity.active_writers

    # ---- candidate ownership and capacity ------------------------------

    def _hydrate_durable_orchestration(self) -> None:
        """Recover child results and waits from the persisted Reducer state.

        A Scheduler is rebuilt on every verification cycle and after a restart.
        Recovering these from the durable snapshot is what makes "result arrived"
        and "wait registered" facts rather than process-lifetime accidents.
        """
        for payload in self.reducer.child_outcomes():
            try:
                self.mailbox.publish(TaskOutcome.from_dict(payload))
            except (KeyError, TypeError, ValueError):
                continue
        for payload in self.reducer.dependency_waits():
            try:
                self._waits[payload["wait_id"]] = WaitSpec.from_dict(payload)
            except (KeyError, TypeError, ValueError, WaitError):
                continue

    def candidate_id_for(self, node_or_id: PlanNode | str) -> str:
        """The candidate a node writes. The primary owns P; each Builder its own."""
        node_id = node_or_id if isinstance(node_or_id, str) else node_or_id.node_id
        return "primary" if node_id == "primary" else f"candidate-{node_id}"

    def _holds_candidate(self, node: PlanNode, lease: CandidateLease | None) -> bool:
        if lease is None:
            return True
        attempt = self.reducer.current_attempt(node.node_id)
        return (lease.owner_node_id == node.node_id
                and attempt is not None and lease.owner_attempt_id == attempt.attempt_id)

    def candidate_snapshot(self) -> dict[str, Any]:
        """Ownership + capacity + mailbox evidence; grants nothing by itself."""
        return {
            "candidates": self.candidates.snapshot(),
            "capacity": self.capacity.snapshot(),
            "mailbox_cursor": self.mailbox.cursor(),
            "wait_count": len(self._waits),
            "quarantines": self.reducer.candidate_quarantines(),
        }

    def retire_quarantined_candidate(self, *, attempt_id: str, worker) -> dict[str, Any]:
        """Operator/native API only: archive proof retires isolated bytes, never promotes them."""
        with self._dispatch_lock:
            record = self.reducer.candidate_quarantines()[attempt_id]
            if record["status"] == "pending":
                node = self._node_definition(record["node_id"])
                if not node.is_writer or set(node.allowed_effects) - {ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE}:
                    raise RuntimeError("external side effects require separate operator reconciliation")
                if any(inv.attempt_id == attempt_id and not future.done()
                       for future, (_node, inv, _token) in (getattr(self, "_active_futures", None) or {}).items()):
                    raise RuntimeError("the Worker has not exited")
                with self.candidates.mutation(candidate_id=record["candidate_id"], as_runtime=True, timeout=1):
                    evidence = worker.verify_quarantined_candidate(record)
                    self.reducer.retire_candidate_quarantine(attempt_id, evidence)
            lease = self.candidates.lease_for(record["candidate_id"])
            if lease is not None and lease.owner_attempt_id == attempt_id:
                self.candidates.release(candidate_id=lease.candidate_id, owner_token=lease.owner_token)
                self.reducer.record_candidate_lease(lease, released=True)
            reservation = self._quarantine_reservations.pop(attempt_id, None)
            if reservation is not None:
                self.capacity.release(reservation)
            return self.reducer.candidate_quarantines()[attempt_id]

    def cancel(self, reason: str = "canceled") -> None:
        self.cancellation_event.set()
        self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.CANCEL_RUN,
                {"reason": reason},
            )
        )

    def prepare_consultation(self, *, parent_id: str, attempt_id: str, revision: str,
                             assignments: tuple[dict, ...], worker: Worker) -> dict:
        """Validate a proposal; it becomes runnable only with the durable checkpoint."""
        if not self.profile.dynamic_worker_delegation:
            raise ValueError("dynamic Worker delegation is disabled for this profile")
        if not self.profile.primary_led or not self.reducer.continuations_enabled:
            raise ValueError("durable consultation is not enabled")
        parent = self._node_definition(parent_id)
        attempt = self.reducer.current_attempt(parent_id)
        if (self.cancellation_event.is_set() or parent.role != "primary" or attempt is None
            or attempt.attempt_id != attempt_id or self.reducer.node_state(parent_id) is not NodeState.RUNNING
            or not attempt.lease.is_valid(self.clock.now())):
            raise ValueError("only the live primary attempt may prepare consultation")
        if self._budget_exhausted():
            raise ValueError(self._reasons[-1])
        if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
            raise ValueError("consultation requires a SHA-256 candidate revision")
        process = self.reducer.required_consultation_status()
        if process and not process["satisfied"]:
            if process["blocked"]:
                raise ValueError(process["reason"])
            required_count = sum(item.get("criticality", "required") == "required" for item in assignments)
            minimum = process["min_helpers"] if process["single_batch"] else 1
            if required_count < minimum:
                raise ValueError(f"required consultation needs at least {minimum} required helpers in this batch")
        children = []
        for assignment in assignments:
            files = consultation_context_files(assignment.get("context_files", ()))
            identity = json.dumps({"run": self.reducer.run_id, "parent": parent_id, "revision": revision,
                                   "contract": self.reducer.current_plan.contract_revision,
                                   "prompt": assignment["prompt"], "agent_type": assignment["agent_type"],
                                   **({"context_files": files} if files else {})}, sort_keys=True)
            children.append(PlanNode(
                "consult-" + hashlib.sha256(identity.encode()).hexdigest(), assignment["prompt"],
                {"explore": "explorer", "plan": "planner", "review": "reviewer", "verify": "verifier"}[assignment["agent_type"]],
                allowed_effects=(ToolEffect.READ_ONLY,),
                acceptance_criteria=("return evidence for the primary's bounded consultation",),
                max_attempts=min(1 + self.profile.retry_budget, 2),
                criticality=assignment.get("criticality", "required"),
                context_files=files,
            ))
        # Identity deliberately excludes criticality: retrying the same advice
        # as advisory cannot downgrade its already-registered required node.
        existing = {node.node_id: node for node in self.reducer.current_plan.nodes}
        if any(child.node_id in existing and existing[child.node_id] != child for child in children):
            raise ValueError("consultation id conflicts with a registered definition; criticality is immutable")
        if children and all(child.node_id in existing for child in children):
            results = [self.reducer.node_result(child.node_id) for child in children]
            if all(result is not None and self.reducer.node_state(child.node_id) is NodeState.SUCCEEDED
                   for child, result in zip(children, results)):
                return {"status": "completed", "results": [result.to_dict() for result in results]}
            raise ValueError("matching consultation did not complete successfully")
        self.reducer.validate_consultation(parent_id, tuple(children))
        admission = self.reducer.admit_consultation_budget(parent_id, attempt_id, tuple(child.objective for child in children),
            helper_token_limits=tuple(min(self.profile.budget.max_tokens or CONSULTATION_TOKEN_CAP,
                                          CONSULTATION_TOKEN_CAP) * (1 if self.reducer.shared_consultation_budget else child.max_attempts)
                                      for child in children), shared_pool=self.reducer.shared_consultation_budget)
        if not admission["allowed"]:
            return {"status": "declined", "reason": admission["reason"], "budget": admission,
                    "message": "No helper was registered or queued. Do not retry an unchanged or larger batch. "
                               "Reduce optional work or continue in primary only if delegation is optional; "
                               "otherwise report the unmet requirement. Required checks remain mandatory."}
        worker.queue_consultation(parent_id, attempt_id, revision, tuple(children))
        return {"status": "queued", "children": [child.node_id for child in children], "budget": admission,
                "message": "Primary will checkpoint and wait; these helpers have not run yet."}

    def acknowledge_advisory_failure(self, *, parent_id: str, attempt_id: str, child_id: str, reason: str) -> dict:
        attempt = self.reducer.current_attempt(parent_id)
        if self.cancellation_event.is_set() or attempt is None or self._budget_exhausted():
            raise ValueError("the Runtime cannot accept advisory takeover now")
        self.reducer.apply(command(self.reducer.run_id, RuntimeCommandType.ACKNOWLEDGE_ADVISORY_FAILURE, {
            "node_id": parent_id, "attempt_id": attempt_id, "fencing_token": attempt.lease.fencing_token.value,
            "child_id": child_id, "reason": reason,
        }))
        return {"node_id": child_id, "state": self.reducer.node_state(child_id).value,
                "diagnosis": self.reducer.node_diagnosis(child_id),
                "message": "Advisory failure retained. Primary owns the replacement work; all required acceptance checks still apply."}

    def retry_consultation(self, *, parent_id: str, attempt_id: str, revision: str, worker) -> dict:
        status = self.reducer.consultation_recovery_status(parent_id, attempt_id)
        if not status["allowed"] or self.cancellation_event.is_set() or self._budget_exhausted():
            return {**status, "status": "blocked", "message": "Review remains pending. Preserve progress and explain the unresolved findings and next step."}
        reading = self.reducer.consultation_read_status()
        if not reading["satisfied"]:
            return {**status, "status": "blocked", "reason": reading["reason"]}
        continuation = self.reducer.continuation_for(parent_id)
        children = continuation.children
        self.reducer.validate_consultation(parent_id, children)
        admission = self.reducer.admit_consultation_budget(parent_id, attempt_id, tuple(n.objective for n in children),
            helper_token_limits=tuple(min(self.profile.budget.max_tokens or CONSULTATION_TOKEN_CAP,
                CONSULTATION_TOKEN_CAP) * (1 if self.reducer.shared_consultation_budget else n.max_attempts) for n in children),
            shared_pool=self.reducer.shared_consultation_budget)
        if not admission["allowed"]:
            return {**status, "status": "blocked", "reason": admission["reason"], "budget": admission}
        worker.queue_consultation(parent_id, attempt_id, revision, children)
        return {**status, "status": "queued", "message": "Rechecking the current candidate against the unchanged original batch. Prior failures remain recorded."}

    # ---- asynchronous Builder delegation --------------------------------

    def _live_primary(self, parent_node_id: str, parent_attempt_id: str) -> str | None:
        """The refusal reason when the caller is not the live primary attempt."""
        try:
            parent = self._node_definition(parent_node_id)
        except KeyError:
            return "delegation parent does not exist"
        attempt = self.reducer.current_attempt(parent_node_id)
        if (parent.role != "primary"
                or self.reducer.node_state(parent_node_id) is not NodeState.RUNNING
                or attempt is None or attempt.attempt_id != parent_attempt_id
                or not attempt.lease.is_valid(self.clock.now())):
            return "only the live primary attempt may spawn builders"
        if self.cancellation_event.is_set() or self.reducer.snapshot().get("canceled"):
            return "the run is canceled"
        return None

    def spawn_builders(
        self, *, parent_node_id: str, parent_attempt_id: str, tasks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Register bounded Builder tasks on their own candidates and return.

        Registration is NOT execution and NOT completion: the returned task IDs
        carry no claim that any Builder ran. The primary keeps executing in its
        own candidate, and the dispatch loop starts each Builder as soon as its
        declared dependencies are satisfied — so one Builders finishing does not
        wait for its siblings.
        """
        if not self.profile.parallel_candidates:
            return {"schema": "minicode-builder-spawn-v1", "registered": [], "refused": list(tasks),
                    "reason": "parallel candidates are disabled for this profile",
                    "registration_is_not_execution": True}
        refusal = self._live_primary(parent_node_id, parent_attempt_id)
        if refusal is not None:
            return {"schema": "minicode-builder-spawn-v1", "registered": [], "refused": list(tasks),
                    "reason": refusal, "registration_is_not_execution": True}
        registered: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        known = {node.node_id for node in self.reducer.current_plan.nodes}
        with self._dispatch_lock:
            objectives = tuple(str(spec.get("objective", "")) for spec in tasks
                if "builder-" + hashlib.sha256(str(spec.get("idempotency_key", "")).encode()).hexdigest()[:16] not in known)
            if objectives:
                admission = self.reducer.admit_consultation_budget(parent_node_id, parent_attempt_id, objectives,
                    shared_pool=self.reducer.shared_consultation_budget)
                if not admission["allowed"]:
                    return {"registered": [], "refused": [{"error": admission["reason"]}],
                        "budget": admission, "registration_is_not_execution": True}
            for spec in tasks:
                outcome = self._register_builder(spec, parent_node_id=parent_node_id, known=known)
                if "error" in outcome:
                    refused.append(outcome)
                    continue
                registered.append(outcome)
                known.add(outcome["task_id"])
        if registered:
            # Wake the dispatch loop so the new READY nodes are considered now
            # rather than at the next poll tick. The primary never blocks here.
            self._dispatch_wake.set()
        return {
            "schema": "minicode-builder-spawn-v1",
            "parent_node_id": parent_node_id,
            "registered": registered,
            "refused": refused,
            "registration_is_not_execution": True,
            "notice": ("Tasks are registered with their own candidate and will start when their "
                       "declared dependencies are satisfied; poll_task_results or "
                       "wait_dependencies reports what actually happened."),
        }

    def spawn_agents(self, *, parent_id: str, attempt_id: str, tasks, worker: Worker,
                     cancellation_event=None) -> dict:
        # Admission and primary file writes share a short boundary: a scope
        # cannot be handed over in the middle of a parent's file mutation.
        with self._dispatch_lock, self.shared_writes.lock:
            return self._spawn_agents(parent_id=parent_id, attempt_id=attempt_id, tasks=tasks,
                worker=worker, cancellation_event=cancellation_event)

    def _spawn_agents(self, *, parent_id: str, attempt_id: str, tasks, worker: Worker,
                      cancellation_event=None, recovery=None) -> dict:
        """Append shared helpers, or explicitly isolated ones, without blocking their parent."""
        if not self.profile.async_agents or not self.reducer.async_agents_enabled:
            raise ValueError("async agents are disabled")
        registered, refused = [], []
        with self._dispatch_lock:
            refusal = self._live_primary(parent_id, attempt_id)
            if refusal or not self.profile.dynamic_worker_delegation:
                raise ValueError(refusal or "dynamic delegation is disabled")
            if self._budget_exhausted():
                raise ValueError(self._reasons[-1])
            new_tasks = tuple(spec.objective for spec in tasks if "agent-" + hashlib.sha256(
                f"{self.reducer.run_id}:{parent_id}:{spec.idempotency_key}".encode()).hexdigest()[:32]
                not in self.reducer.agent_sessions())
            if new_tasks:
                admission = self.reducer.admit_consultation_budget(parent_id, attempt_id, new_tasks,
                    shared_pool=self.reducer.shared_consultation_budget)
                if not admission["allowed"]:
                    raise ValueError(admission.get("reason", "closure reserves prevent new assistant work"))
            for spec in tasks:
                identity = f"{self.reducer.run_id}:{parent_id}:{spec.idempotency_key}"
                node_id = "agent-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
                node = PlanNode(node_id, spec.objective,
                    {"explore": "explorer", "plan": "planner", "review": "reviewer", "verify": "verifier", "worker": "shared_writer"}[spec.agent_type],
                    allowed_effects=((ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE) if spec.agent_type == "worker"
                                     else (ToolEffect.READ_ONLY,)), write_scope=spec.write_scope,
                    acceptance_criteria=spec.acceptance_criteria or (("implement the assigned edits within write_scope and report changed files and incomplete work; final acceptance belongs to the primary",)
                        if spec.agent_type == "worker" else ("return source observations with workspace consistency stated for the parent",)),
                    max_attempts=1 if spec.agent_type == "worker" else min(1 + self.profile.retry_budget, 2))
                from minicode.agent_collaboration import assignment_metadata
                metadata = assignment_metadata(spec) if self.reducer.collaboration_config else {}
                if metadata and not metadata["name"]:
                    metadata["name"] = spec.objective[:60]
                existing = self.reducer.agent_sessions().get(node_id)
                if existing is not None:
                    same_recovery = (bool(existing.get("recovery_source")) == bool(recovery)
                        and (not recovery or (existing["recovery_source"]["run_id"] == recovery["source_run_id"]
                            and existing["recovery_source"]["task_id"] == recovery["task_id"]
                            and existing["session_id"] == recovery["session_id"])))
                    if (not same_recovery or existing["definition"] != node.to_dict() or existing.get("context_mode", "isolated") != spec.context_mode
                        or existing.get("optional", False) != spec.optional
                        or existing["workspace"]["mode"] != spec.isolation
                        or tuple(ref["artifact_id"] for ref in existing.get("project_report_refs", ())) != spec.report_artifact_ids
                        or any(existing.get(k) != v for k, v in metadata.items())):
                        refused.append({"task_id": node_id, "error": "idempotency_key already identifies a different assignment"})
                    else:
                        registered.append(self._agent_receipt(existing, reused=True))
                    continue
                if cancellation_event is not None and cancellation_event.is_set():
                    refused.append({"task_id": node_id, "error": "preparation canceled before registration"})
                    continue
                try:
                    self.reducer.validate_agent_budget(node_id)
                    if not self.reducer.collaboration_config and (spec.name or spec.checks or spec.acceptance_criteria or spec.optional):
                        raise ValueError("task briefs and local checks require a new collaboration-enabled run")
                    if spec.agent_type == "worker" and (not self.shared_writes.enabled
                        or not self._node_definition(parent_id).is_writer
                        or spec.isolation != "shared" or not spec.write_scope):
                        raise ValueError("shared worker requires enabled direct writing, a writable parent and a nonempty write_scope")
                    if spec.agent_type == "worker" and (self.capacity.max_workers < 2 or self.capacity.max_writers < 2):
                        raise ValueError("shared workers require at least 2 worker and 2 writer slots: one is reserved "
                            "for primary recovery. Increase runtimeMaxActiveWorkers/runtimeMaxActiveWriters or let "
                            "the primary edit directly; no shared task was registered")
                    reports = self._project_report_refs(worker, spec.report_artifact_ids)
                    if spec.agent_type == "verify" and spec.isolation == "shared":
                        raise ValueError("independent verification requires isolation=snapshot or worktree")
                    fork_checkpoint = None
                    if spec.context_mode == "fork":
                        if not self.profile.agent_fork or self._node_definition(parent_id).allowed_effects != (ToolEffect.READ_ONLY,):
                            raise ValueError("fork requires an enabled read-only parent; use an isolated assistant for different permissions")
                        fork_checkpoint = self.reducer.agent_checkpoint(parent_id)
                        worker.validate_agent_fork(parent_id, attempt_id, fork_checkpoint)
                    view = self._capture_agent_workspace(worker, parent_id, attempt_id, node_id, cancellation_event,
                        isolation=spec.isolation)
                    if (fork_checkpoint is not None and spec.isolation != "shared"
                        and fork_checkpoint["input_revision"] != view["input_revision"]):
                        raise ValueError("parent input changed since the fork checkpoint; use an isolated assistant")
                    record = {"task_id": node_id, "parent_node_id": parent_id,
                        "session_id": recovery["session_id"] if recovery else node_id,
                        "generation": recovery["generation"] + 1 if recovery else 1,
                        "context_mode": spec.context_mode,
                        **({"fork_checkpoint": fork_checkpoint} if fork_checkpoint is not None else {}),
                        "idempotency_key": spec.idempotency_key, "definition": node.to_dict(),
                        "project_report_refs": [ref.to_dict() for ref in reports],
                        "contract_revision": self.reducer.current_plan.contract_revision, **metadata, **view}
                    if recovery:
                        from minicode.agent_recovery import copy_retained_context
                        record.update(copy_retained_context(recovery, artifacts=worker.artifact_store,
                            run_id=self.reducer.run_id, record=record))
                    self.reducer.register_delegated_node(node, parent_node_id=parent_id,
                        parent_attempt_id=attempt_id, agent_session=record)
                except Exception as error:
                    # An uncertain persistence write stops the Runtime; it must not
                    # delete a snapshot whose registration may have reached disk.
                    if getattr(self.reducer, "_persistence_error", None) is not None:
                        raise
                    worker.discard_async_workspace(node_id)
                    refused.append({"task_id": node_id, "error": f"{type(error).__name__}: {error}"})
                    continue
                registered.append(self._agent_receipt(record))
        if registered:
            self._dispatch_wake.set()
        return {"schema": "minicode-agent-dispatch-v1", "registered": registered,
            "refused": refused, "registration_is_not_execution": True}

    def _cancel_orphaned_agents(self) -> None:
        """A terminal parent cannot consume further assistant work.

        Preserve the parent's original failure and every settled child result.
        Only unfinished registered assistants/Builders are fenced; retryable/waiting
        parents retain their helpers and normal continuation behavior.
        """
        # Builders are registered separately from read-only async assistants.
        # Omitting them leaves a dead primary's writer waiting for approval (or
        # still spending model budget) even though nobody can accept its patch.
        children = {**self._builder_tasks, **self.reducer.agent_sessions()}
        for task_id, record in children.items():
            parent_result = self.reducer.node_result(record["parent_node_id"])
            if (parent_result is not None and parent_result.failure is not None
                and parent_result.failure.kind is ExecutionFailureKind.PROVIDER_BALANCE
                and self.reducer.node_state(task_id) in {NodeState.RUNNING, NodeState.CLAIMED}):
                # The request circuit stops the next model step. Let the current
                # batch settle and checkpoint instead of losing its last edits.
                continue
            if (self.reducer.node_state(record["parent_node_id"]) in {NodeState.FAILED_TERMINAL, NodeState.CANCELED}
                and self.reducer.node_state(task_id) not in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL,
                    NodeState.CANCELED, NodeState.SUPERSEDED}):
                self.cancel_task(task_id=task_id, reason="parent terminated; assistant result can no longer be consumed",
                    operation_id="parent-terminated:" + task_id)

    def _agent_receipt(self, record: Mapping, *, reused: bool = False) -> dict:
        projection = self.reducer.agent_task_projection(record) if self.reducer.collaboration_config else {}
        return {"task_id": record["task_id"], "parent_node_id": record["parent_node_id"],
            "session_id": record["session_id"], "generation": record["generation"],
            "role": record["definition"]["role"], "context_mode": record.get("context_mode", "isolated"),
            "name": record.get("name", ""), "checks": record.get("checks", []),
            "local_checks": self.reducer.agent_check_summaries(record["task_id"]),
            **({"worker_exited": self.reducer.agent_worker_settlement(record["task_id"]) is not None,
                "continuation_available": projection["continuationAvailable"], "session_state": projection["sessionState"],
                "optional": projection["optional"], "dismissed": projection["dismissed"],
                "dismissal_available": projection["dismissalAvailable"]}
               if self.reducer.collaboration_config else {}),
            "continuation_policy": (("fresh_scope_continuation" if self.reducer.collaboration_config else "fresh_scoped_assignment_required") if record["definition"]["role"] == "shared_writer"
                else "independent_verifier_cannot_resume" if record["definition"]["role"] == "verifier" else "successful_idle_only"),
            "input_revision": record["input_revision"], "contract_revision": record["contract_revision"],
            "isolation": record["workspace"]["mode"],
            **({"write_scope": record["definition"]["write_scope"], "writes_already_in_parent": True}
               if record["definition"]["role"] == "shared_writer" else {}),
            "report_artifact_ids": [ref["artifact_id"] for ref in record.get("project_report_refs", ())],
            "input_consistency": "live_shared" if record["workspace"]["mode"] == "shared" else "fixed",
            "state": self.reducer.node_state(record["task_id"]).value, "reused": reused}

    def _project_report_refs(self, worker, artifact_ids):
        from minicode.project_verification import coordinator_checks_enabled
        if artifact_ids and not coordinator_checks_enabled(worker.runtime):
            raise ValueError("project reports require coordinator project checks")
        refs = []
        for artifact_id in artifact_ids:
            ref = worker.artifact_store.owned_reference(artifact_id, run_id=self.reducer.run_id,
                                                       node_id="npm-build-and-browser")
            if ref is None or ref.media_type not in {"image/png", "application/json"}:
                raise ValueError(f"report artifact is not a native project check from this run: {artifact_id}")
            refs.append(ref)
        return tuple(refs)

    def _capture_agent_workspace(self, worker, parent_id, attempt_id, node_id, cancellation_event, *, isolation="shared"):
        from minicode.workspace_provider import WorkspacePreparationCanceled, ASYNC_PREPARATION_SECONDS
        from minicode.runtime_cancellation import ActivationCancellation
        if cancellation_event is not None and cancellation_event.is_set():
            raise WorkspacePreparationCanceled("preparation canceled before registration")
        deadline = time.monotonic() + ASYNC_PREPARATION_SECONDS
        preparation = ActivationCancellation(cancellation_event or self.cancellation_event,
            lambda: time.monotonic() >= deadline)
        try:
            view = worker.capture_async_workspace(parent_id, attempt_id, node_id, cancellation_event=preparation, isolation=isolation)
        except WorkspacePreparationCanceled as error:
            if time.monotonic() >= deadline:
                raise WorkspacePreparationCanceled(
                    f"workspace preparation exceeded its {ASYNC_PREPARATION_SECONDS:g}s deadline before registration") from error
            raise
        if preparation.is_set():
            raise WorkspacePreparationCanceled("preparation canceled before registration")
        if cancellation_event is not None and cancellation_event.is_set():
            raise WorkspacePreparationCanceled("preparation canceled before registration")
        refusal = self._live_primary(parent_id, attempt_id)
        if refusal:
            raise ValueError(refusal)
        return view

    def recover_agent(self, *, parent_id, attempt_id, item, idempotency_key, objective, worker,
                      cancellation_event=None, write_scope=None, acceptance_criteria=None, checks=None):
        """Admit a retained conversation as fresh work, with the same session ID."""
        with self._dispatch_lock, self.shared_writes.lock:
            if not self.profile.agent_resume or not self.reducer.agent_resume_enabled:
                raise ValueError("agent resume is disabled")
            original = item["record"]
            if item["generation"] >= self.reducer.agent_generation_limit:
                raise ValueError("agent continuation generation quota exhausted")
            key = "agent-" + hashlib.sha256(f"{self.reducer.run_id}:{parent_id}:{idempotency_key}".encode()).hexdigest()[:32]
            if any(r["session_id"] == item["session_id"] and r["task_id"] != key for r in self.reducer.agent_sessions().values()):
                raise ValueError("retained session already continued; use its current task ID")
            from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
            role = original["definition"]["role"]
            if role == "shared_writer" and not self.profile.shared_workspace_writes:
                raise ValueError("retained writer needs current shared write authority")
            tasks = createApp().validate_agent_dispatch({"tasks": [{
                "idempotency_key": idempotency_key, "objective": objective,
                "agent_type": {"explorer": "explore", "planner": "plan", "reviewer": "review", "shared_writer": "worker"}[role],
                "name": original.get("name", ""), "isolation": original["workspace"]["mode"],
                "optional": original.get("optional", False),
                "write_scope": write_scope or [],
                "acceptance_criteria": list(dict.fromkeys([*original["definition"]["acceptance_criteria"], *(acceptance_criteria or [])])),
                "checks": checks if checks is not None else original.get("checks", [])}]})
            result = self._spawn_agents(parent_id=parent_id, attempt_id=attempt_id, tasks=tasks,
                worker=worker, cancellation_event=cancellation_event, recovery=item)
            if result["refused"]:
                raise ValueError(result["refused"][0]["error"])
            return {**result["registered"][0], "source_run_id": item["source_run_id"], "retained_context": True}

    def resume_agent(self, *, parent_id, attempt_id, task_id, idempotency_key, objective, worker, checkpoint,
                      cancellation_event=None, write_scope=None, acceptance_criteria=None, checks=None) -> dict:
        """Continue an idle logical session as a new, bounded graph node."""
        with self._dispatch_lock, self.shared_writes.lock:
            if not self.profile.agent_resume or not self.reducer.agent_resume_enabled:
                raise ValueError("agent resume is disabled")
            refusal = self._live_primary(parent_id, attempt_id)
            if refusal or self._budget_exhausted():
                raise ValueError(refusal or self._reasons[-1])
            records = self.reducer.agent_sessions()
            previous = records.get(task_id)
            if previous is None or previous["parent_node_id"] != parent_id:
                raise ValueError("resume requires an owned assistant")
            writer = previous["definition"]["role"] == "shared_writer"
            if writer and not self.reducer.collaboration_config:
                raise ValueError("shared writers need a fresh spawn_agents assignment with an explicit write_scope; existing edits are preserved")
            from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
            assignment = createApp().validate_agent_dispatch({"tasks": [{
                "idempotency_key": idempotency_key, "objective": objective,
                "agent_type": "worker" if writer else {"explorer": "explore", "planner": "plan", "reviewer": "review", "verifier": "verify"}[previous["definition"]["role"]],
                "isolation": previous["workspace"]["mode"], "write_scope": write_scope or (),
                "acceptance_criteria": acceptance_criteria or (), "checks": checks or (),
                "name": previous.get("name", ""), "optional": previous.get("optional", False)}]} )[0]
            node_id = "agent-" + hashlib.sha256(f"{self.reducer.run_id}:{parent_id}:resume:{idempotency_key}".encode()).hexdigest()[:32]
            interrupted = self.reducer.is_agent_interrupted(task_id)
            continuing_interruption = interrupted or records.get(node_id, {}).get("continuation_kind") == "interrupted"
            criteria = (tuple(dict.fromkeys((*previous["definition"]["acceptance_criteria"], *assignment.acceptance_criteria)))
                if continuing_interruption else assignment.acceptance_criteria or tuple(previous["definition"]["acceptance_criteria"]))
            if node_id in records:
                existing = records[node_id]
                if (existing.get("predecessor_task_id") != task_id or existing["definition"]["objective"] != objective
                    or tuple(existing["definition"].get("write_scope", ())) != assignment.write_scope
                    or existing.get("checks", []) != list(assignment.checks)
                    or tuple(existing["definition"]["acceptance_criteria"]) != criteria):
                    raise ValueError("resume key already identifies another assignment")
                return self._agent_receipt(existing, reused=True)
            if ((self.reducer.node_state(task_id) is not NodeState.SUCCEEDED and not interrupted)
                or task_id in self.reducer.dismissed_agent_ids()
                or previous["definition"]["role"] == "verifier"
                or (self.reducer.node_cancel_epoch(task_id) and not interrupted) or previous["contract_revision"] != self.reducer.current_plan.contract_revision):
                raise ValueError(f"cannot resume {task_id}: state={self.reducer.node_state(task_id).value}, role={previous['definition']['role']}. "
                    "Only successful idle or explicitly interrupted assistants may continue; shared writers need a fresh write_scope and independent verifiers never resume. "
                    "Ordinary cancellation and failure cannot be bypassed through continuation.")
            if any(r.get("predecessor_task_id") == task_id for r in records.values()):
                raise ValueError("this generation already continued; address the latest task")
            if previous["generation"] >= self.reducer.agent_generation_limit:
                raise ValueError("agent continuation generation quota exhausted")
            self.reducer.validate_agent_budget(previous["session_id"])
            settlement = self.reducer.agent_worker_settlement(task_id)
            if not settlement or settlement.get("ambiguous_side_effect"):
                raise ValueError("the previous worker has not demonstrably exited")
            if checkpoint is None or checkpoint != self.reducer.agent_checkpoint(task_id):
                raise ValueError("resume requires the current owned conversation checkpoint")
            admission = self.reducer.admit_consultation_budget(parent_id, attempt_id, (objective,),
                shared_pool=self.reducer.shared_consultation_budget)
            if not admission["allowed"]:
                raise ValueError(admission.get("reason", "closure reserves prevent assistant continuation"))
            if writer and (not self.shared_writes.enabled or not self._node_definition(parent_id).is_writer):
                raise ValueError("writer continuation requires the current primary's shared write authority")
            node = PlanNode(node_id, objective, previous["definition"]["role"],
                allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE) if writer else (ToolEffect.READ_ONLY,),
                write_scope=assignment.write_scope,
                acceptance_criteria=criteria,
                max_attempts=1 if writer else min(1 + self.profile.retry_budget, 2))
            try:
                reports = self._project_report_refs(worker,
                    tuple(ref["artifact_id"] for ref in previous.get("project_report_refs", ())))
                view = self._capture_agent_workspace(worker, parent_id, attempt_id, node_id, cancellation_event,
                    isolation=previous["workspace"]["mode"])
                old_files, new_files = previous["workspace"]["base_hashes"], view["workspace"]["base_hashes"]
                changed = (None if view["workspace"]["mode"] == "shared" else
                    sorted(path for path in old_files.keys() | new_files.keys() if old_files.get(path) != new_files.get(path)))
                record = {"task_id": node_id, "parent_node_id": parent_id, "session_id": previous["session_id"],
                    "generation": previous["generation"] + 1, "predecessor_task_id": task_id,
                    **({"continuation_kind": "interrupted"} if interrupted else {}),
                    "resume_checkpoint": checkpoint, "changed_paths": changed,
                    "project_report_refs": [ref.to_dict() for ref in reports],
                    "idempotency_key": idempotency_key, "definition": node.to_dict(),
                    **({"name": previous.get("name", ""), "checks": list(assignment.checks)} if self.reducer.collaboration_config else {}),
                    **({"optional": True} if assignment.optional else {}),
                    "contract_revision": self.reducer.current_plan.contract_revision, **view}
                self.reducer.register_delegated_node(node, parent_node_id=parent_id,
                    parent_attempt_id=attempt_id, agent_session=record)
            except Exception:
                if getattr(self.reducer, "_persistence_error", None) is None:
                    worker.discard_async_workspace(node_id)
                raise
            self._dispatch_wake.set()
            return {**self._agent_receipt(record), "changed_paths": changed,
                "notice": "New activation; reread relevant evidence. shared inputs have no fixed version and changed_paths=null means unknown, not unchanged."}

    def reassign_agent(self, *, parent_id, attempt_id, task_id, idempotency_key, objective, worker,
                       cancellation_event=None) -> dict:
        with self._dispatch_lock:
            refusal = self._live_primary(parent_id, attempt_id)
            if refusal or not self.profile.agent_teams or self._budget_exhausted():
                raise ValueError(refusal or "team admission or remaining budget is unavailable")
            records = self.reducer.agent_sessions()
            previous = records.get(task_id)
            if previous is None or previous["parent_node_id"] != parent_id or objective != previous["definition"]["objective"]:
                raise ValueError("reassignment requires the owned failed task and its unchanged objective")
            node_id = "agent-" + hashlib.sha256(f"{self.reducer.run_id}:{parent_id}:reassign:{idempotency_key}".encode()).hexdigest()[:32]
            if node_id in records:
                existing = records[node_id]
                if existing.get("replaces_task_id") != task_id:
                    raise ValueError("reassignment key identifies another task")
                return self._agent_receipt(existing, reused=True)
            self.reducer.validate_agent_reassignment(task_id)
            if task_id in self.reducer.dismissed_agent_ids():
                raise ValueError("dismissed optional work cannot be reassigned")
            if (previous["generation"] >= self.reducer.agent_generation_limit
                or any((r.get("predecessor_task_id") or r.get("replaces_task_id")) == task_id for r in records.values())):
                raise ValueError("current generation already reassigned or total task quota exhausted")
            self.reducer.validate_agent_budget(previous["session_id"])
            admission = self.reducer.admit_consultation_budget(parent_id, attempt_id, (objective,),
                shared_pool=self.reducer.shared_consultation_budget)
            if not admission["allowed"]:
                raise ValueError(admission.get("reason", "closure reserves prevent replacement work"))
            node = PlanNode(node_id, objective, previous["definition"]["role"], allowed_effects=(ToolEffect.READ_ONLY,),
                acceptance_criteria=tuple(previous["definition"].get("acceptance_criteria", ())),
                max_attempts=min(1 + self.profile.retry_budget, 2))
            try:
                view = self._capture_agent_workspace(worker, parent_id, attempt_id, node_id, cancellation_event,
                    isolation=previous["workspace"]["mode"])
                record = {"task_id": node_id, "parent_node_id": parent_id, "session_id": previous["session_id"],
                    "generation": previous["generation"] + 1, "replaces_task_id": task_id, "context_mode": "isolated",
                    "idempotency_key": idempotency_key, "definition": node.to_dict(),
                    **({"name": previous.get("name", ""), "checks": []} if self.reducer.collaboration_config else {}),
                    **({"optional": True} if previous.get("optional") else {}),
                    "contract_revision": self.reducer.current_plan.contract_revision, **view}
                self.reducer.register_delegated_node(node, parent_node_id=parent_id, parent_attempt_id=attempt_id, agent_session=record)
            except Exception:
                if getattr(self.reducer, "_persistence_error", None) is None:
                    worker.discard_async_workspace(node_id)
                raise
            self._dispatch_wake.set()
            return {**self._agent_receipt(record), "replaces_task_id": task_id,
                "notice": "Old failure is retained. Only a verified replacement result can discharge this obligation."}

    def _register_builder(
        self, spec: Mapping[str, Any], *, parent_node_id: str, known: set[str],
        builder_recovery: Mapping[str, Any] | None = None, parent_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(spec, Mapping):
            return {"error": "a builder task must be a mapping"}
        key = spec.get("idempotency_key")
        objective = spec.get("objective")
        if not isinstance(key, str) or not key.strip():
            return {"error": "a builder task needs a non-blank idempotency_key"}
        if not isinstance(objective, str) or not objective.strip():
            return {"error": "a builder task needs a non-blank objective"}
        depends_on = spec.get("depends_on") or ()
        if isinstance(depends_on, str) or not isinstance(depends_on, (list, tuple)):
            return {"error": "depends_on must be a sequence of task ids"}
        dependencies = tuple(str(item) for item in depends_on)
        if len(set(dependencies)) != len(dependencies):
            return {"error": "duplicate dependency edges are not allowed"}
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        node_id = f"builder-{digest}"
        if node_id not in known:
            if self.reducer.collaboration_config:
                try:
                    self.reducer.validate_agent_budget(node_id)
                except ValueError as error:
                    return {"error": str(error), "task_id": node_id}
            elif len(known) >= self.profile.max_agents:
                return {"error": "max_agents budget exhausted", "task_id": node_id}
        if node_id in dependencies:
            return {"error": "a builder task cannot depend on itself", "task_id": node_id}
        unknown = [item for item in dependencies if item not in known]
        if unknown:
            # Dependency cycles are refused at registration rather than discovered
            # at a deadline: an unknown id can only ever be a cycle or a typo.
            return {"error": f"unknown dependency task ids: {', '.join(sorted(unknown))}",
                    "task_id": node_id}
        acceptance = spec.get("acceptance_criteria") or ()
        if isinstance(acceptance, str) or not isinstance(acceptance, (list, tuple)):
            return {"error": "acceptance_criteria must be a sequence of strings"}
        try:
            node = PlanNode(
                node_id=node_id,
                objective=objective.strip(),
                role="writer",
                dependencies=dependencies,
                allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE),
                acceptance_criteria=tuple(str(item) for item in acceptance),
                max_attempts=1,
                criticality="required",
                write_scope=tuple(spec.get("write_scope", ())),
                alternative_group=spec.get("alternative_group"),
            )
            self.reducer.register_delegated_node(node, parent_node_id=parent_node_id,
                builder_idempotency_key=key,
                builder_recovery=builder_recovery, parent_attempt_id=parent_attempt_id,
                dependency_predicates={task_id: str(spec.get("predicate") or "patch_applied_and_checked") for task_id in dependencies})
        except Exception as error:  # noqa: BLE001 - a refusal must be reportable, not fatal
            return {"error": f"registration refused ({type(error).__name__}): {error}",
                    "task_id": node_id}
        candidate_id = self.candidate_id_for(node)
        record = {
            "task_id": node_id, "candidate_id": candidate_id, "objective": node.objective,
            "depends_on": list(dependencies), "parent_node_id": parent_node_id,
            "alternative_group": node.alternative_group,
            "registered_at": self.clock.now(),
        }
        self._builder_tasks.setdefault(node_id, record)
        return record

    def select_builder_candidate(self, *, task_id: str, reason: str, operation_id: str,
                                 parent_attempt_id: str) -> dict[str, Any]:
        with self._dispatch_lock, self.reducer.publication_guard():
            selection = self.reducer.select_builder_candidate(node_id=task_id,
                parent_attempt_id=parent_attempt_id, reason=reason, operation_id=operation_id)
            self._dispatch_wake.set()
            return {"schema": "minicode-builder-selection-v1", **selection,
                    "notice": "Selection does not cancel active alternatives. Cancel unwanted work and wait for its actual termination before submission."}

    # ---- patch acceptance and application -------------------------------

    def bind_candidate_evidence(self, *, revision, changes, root=None) -> None:
        """Bind the native evidence source the write boundary must read.

        ``revision(candidate_id) -> str | None`` reports the candidate's real
        current bytes revision; ``changes(source_candidate_id, target_candidate_id)
        -> Mapping[str,str] | None`` reports what differs between a patch's origin
        base and the target's current bytes. Without them no cross-version
        admission is possible, and the decision says so instead of trusting a
        caller-supplied revision string.
        """
        if not callable(revision) or not callable(changes):
            raise ValueError("candidate evidence requires callable revision and change sources")
        self._candidate_revision = revision
        self._candidate_changes = changes
        self._candidate_root = root

    def _authoritative_facts(
        self, patch: CandidatePatch, *,
        native_revision: str | None = None,
        intervening_changes: Mapping[str, str] | None = None,
    ) -> AuthoritativeFacts:
        """Read the write boundary's real facts; never trust a caller's boolean."""
        target = patch.target_candidate_id
        revision = native_revision
        changes = intervening_changes
        unavailable = ""
        if self._candidate_revision is None or self._candidate_changes is None:
            unavailable = "no native candidate evidence source is bound"
        else:
            if revision is None:
                revision = self._candidate_revision(target)
            if changes is None:
                changes = self._candidate_changes(patch.source_candidate_id, target)
        # The producer's live identity comes from the reducer, not from the lease:
        # a Builder's candidate lease is released when its Worker returns, and the
        # patch must still be checked against the attempt that is current NOW. An
        # unknown producer node simply has no live attempt.
        try:
            producer_attempt = self.reducer.current_attempt(patch.owner_node_id)
        except (KeyError, Exception):  # noqa: BLE001 - an unknown node is not a crash
            producer_attempt = None
        target_lease = self.candidates.lease_for(target)
        selection = self.reducer.candidate_selection_for(patch.owner_node_id)
        if self.reducer.candidate_first and self.reducer.node_state(patch.owner_node_id) is not NodeState.SUCCEEDED:
            unavailable = "Builder result has not passed native validation"
        if producer_attempt is not None and not self.reducer.builder_may_promote(patch.owner_node_id):
            unavailable = "Builder candidate is unselected, discarded or quarantined"
        from minicode.directory_observation import current_directory_hashes
        directory_root = self._candidate_root(target) if callable(self._candidate_root) else None
        directory_hashes = current_directory_hashes(
            Path(directory_root) if directory_root is not None else None,
            patch.read_write_set.directory_hashes)
        return AuthoritativeFacts(
            native_revision=revision,
            producer_attempt_id=producer_attempt.attempt_id if producer_attempt else None,
            producer_generation=self._candidate_generations.get(patch.source_candidate_id),
            producer_fencing_token=self._fencing_tokens.get(patch.owner_node_id),
            plan_version=self.reducer.current_plan.plan_version,
            contract_revision=self.reducer.current_plan.contract_revision,
            cancel_epoch=self.reducer.candidate_cancel_epoch(target),
            canceled=self.reducer.candidate_canceled(target),
            # The producer's OWN cancellation, from the same native source. A
            # cancelled Builder keeps its attempt id, so this cannot be inferred
            # from the identity fields above.
            producer_canceled=self.reducer.candidate_canceled(patch.source_candidate_id),
            producer_cancel_epoch=self.reducer.candidate_cancel_epoch(
                patch.source_candidate_id),
            dependency_vector=self._current_dependency_vector(),
            revoked_dependencies=self._revoked_dependencies(patch),
            selection_winner=(selection or {}).get("selected_node_id"),
            selection_operation_id=(selection or {}).get("operation_id"),
            selection_group=(selection or {}).get("group"),
            intervening_changes=changes,
            target_owner_attempt_id=target_lease.owner_attempt_id if target_lease else None,
            facts_complete=not unavailable,
            unavailable_reason=unavailable,
            directory_hashes=directory_hashes,
            projected_directory_hashes=current_directory_hashes(
                Path(directory_root) if directory_root is not None else None,
                patch.read_write_set.directory_hashes, writes=patch.content_hashes),
        )

    @staticmethod
    def _pin_payload(predicate: str, task_id: str, pin: Mapping[str, Any],
                     *, run_id: str, plan_version: int, contract_revision: int) -> dict[str, Any]:
        """The identity-bearing slice a dependency pin asserts, plus audit fields.

        ``pinned`` holds exactly the fields the predicate itself asserts, so two
        pins for the same dependency compare equal only when the dependency really
        is the same. ``observed`` records the rest (published event id, mailbox
        sequence) for audit; a legitimate republish reassigns those, so they are
        deliberately outside the comparison.
        """

        identity = {
            "generation": int(pin.get("generation", 1)),
            "attempt_id": str(pin.get("attempt_id") or ""),
            "candidate_id": str(pin.get("candidate_id") or ""),
            "terminal": pin.get("terminal"),
        }
        if predicate != TERMINAL_OBSERVED:
            identity.update({
                "artifact_sha256": pin.get("artifact_sha256"),
                "revision": pin.get("revision"),
            })
        if predicate == PATCH_APPLIED_AND_CHECKED:
            identity.update({
                "applied_revision": pin.get("applied_revision"),
                "patch_sha256": pin.get("patch_sha256"),
            })
        return {
            "schema": DEPENDENCY_PIN_SCHEMA,
            "run_id": run_id,
            "plan_version": plan_version,
            "contract_revision": contract_revision,
            "task_id": task_id,
            "predicate": predicate,
            "pinned": identity,
            "observed": {
                "artifact_id": pin.get("artifact_id"),
                "event_id": pin.get("event_id"),
                "sequence": int(pin.get("sequence", 0)),
            },
        }

    def _dependency_entry(self, task_id: str, predicate: str) -> dict[str, Any] | None:
        """The evidence that releases one declared edge, as a pinnable entry.

        The predicate is evaluated by the SAME reducer method the READY/CLAIM gates
        use, so an entry exists exactly when the edge really released its consumer.
        A task with no declared dependency contributes no entry: an empty vector
        means "this work depends on nothing", never "the evidence was skipped".
        """

        if not self.reducer.dependency_edge_satisfied(task_id, predicate):
            return None
        pin = self.reducer.dependency_pin(task_id, predicate)
        return {
            "task_id": task_id,
            "generation": int(pin.get("generation", 1)),
            "artifact_id": pin.get("artifact_id"),
            "artifact_sha256": pin.get("artifact_sha256"),
            "predicate": predicate,
            "applied_revision": pin.get("applied_revision"),
            "pinned": self._pin_payload(
                predicate, task_id, pin, run_id=self.reducer.run_id,
                plan_version=self.reducer.current_plan.plan_version,
                contract_revision=self.reducer.current_plan.contract_revision),
        }

    def _dependency_vector_for(self, node: PlanNode) -> DependencyVector:
        """Pin, per DECLARED edge, the evidence that actually released this consumer."""

        declared = self.reducer.dependency_predicates_for(node.node_id)
        entries = []
        for task_id in node.dependencies:
            predicate = declared.get(task_id)
            if predicate is None:
                # A dependency registered without an explicit predicate defaults to
                # the code-dependency semantic, the same default the READY gate uses.
                predicate = PATCH_APPLIED_AND_CHECKED
            entry = self._dependency_entry(task_id, predicate)
            if entry is not None:
                entries.append(entry)
        return DependencyVector(tuple(entries))

    def _revoked_dependencies(self, patch: CandidatePatch) -> dict[str, str]:
        """Which pinned dependencies no longer describe the current dependency."""

        revoked: dict[str, str] = {}
        for entry in patch.dependency_vector.entries:
            pin = entry.get("pinned")
            if not isinstance(pin, Mapping):
                continue
            reason = self.reducer.verify_dependency_pin(pin)
            if reason:
                revoked[str(entry["task_id"])] = reason
        return revoked

    def _current_dependency_vector(self) -> DependencyVector:
        """The still-exact pins of every dependency a consumer declares.

        This is what a patch's own pins are compared against, so it is built from
        the same per-predicate evidence rather than from applied patches alone.
        """

        predicates: dict[str, str] = {}
        for declared in self.reducer.current_plan.nodes:
            for task_id, predicate in self.reducer.dependency_predicates_for(
                    declared.node_id).items():
                predicates.setdefault(task_id, predicate)
        entries: list[dict[str, Any]] = []
        for task_id in sorted(predicates):
            entry = self._dependency_entry(task_id, predicates[task_id])
            if entry is not None:
                entries.append(entry)
        return DependencyVector(tuple(entries))

    def _persist_patch(self, patch_id: str) -> None:
        """Persist the ledger state WITHOUT discarding the produced patch payload."""
        record = self.patch_ledger.record(patch_id)
        if record is None:
            return
        existing = self.reducer.patch_records().get(patch_id) or {}
        merged = {**existing, **record}
        for key in ("patch", "candidate_id", "node_id", "candidate_revision", "changed_paths"):
            if key in existing:
                merged[key] = existing[key]
        self.reducer.record_patch_record(patch_id, merged)

    def accept_patch(self, patch: CandidatePatch, *, operation_id: str) -> dict[str, Any]:
        """Decide and record one patch acceptance. Accepting is not applying."""
        existing = self.patch_ledger.record(patch.patch_id)
        if existing is not None and existing.get("state") != PATCH_FAILED:
            return {"schema": APPLIED_SCHEMA, "accepted": existing.get("state") != PATCH_FAILED,
                    "decision": {"accepted": True, "path": existing.get("path"),
                                 "reason": "acceptance already recorded",
                                 "requires_rebase": False},
                    "receipt": existing, "replayed": True}
        facts = self._authoritative_facts(patch)
        decision = decide_promotion(patch, facts)
        if not decision.accepted:
            previous = self.reducer.patch_records().get(patch.patch_id, {})
            rejection = {"decision": decision.to_dict(), "target_revision": facts.native_revision}
            if previous.get("last_rejection") != rejection:
                self.reducer.record_patch_record(patch.patch_id, {**previous, "last_rejection": rejection})
            return {"schema": APPLIED_SCHEMA, "accepted": False,
                    "decision": decision.to_dict(), "receipt": None, "replayed": False}
        receipt = self.patch_ledger.accept(patch, decision=decision, operation_id=operation_id,
                                           cancel_epoch=facts.cancel_epoch, facts=facts)
        self._persist_patch(patch.patch_id)
        return {"schema": APPLIED_SCHEMA, "accepted": True, "decision": decision.to_dict(),
                "receipt": receipt, "replayed": False}

    def apply_patch(self, patch: CandidatePatch, *, operation_id: str, apply) -> dict[str, Any]:
        """Apply an accepted patch under a SHORT exclusive mutation of the target.

        ``apply`` performs the real byte-level write and MUST return the sha256 of
        the bytes that actually landed, so the receipt describes verified content
        rather than an intention. A repeat application of the same identity returns
        the recorded receipt and never calls the writer again.

        Order is the safety property: the target revision is re-read INSIDE the
        mutation boundary (an observation taken before the lock is not the revision
        the write happened at), the pre-write revision is recorded as the apply
        intent, and the revision observed AFTER the write is what the receipt
        reports as applied.
        """
        if not callable(apply):
            raise ValueError("apply_patch needs a callable that performs the real write")
        replayed = self.patch_ledger.reusable_application(patch, operation_id=operation_id)
        if replayed is not None:
            return {"schema": APPLICATION_SCHEMA, "receipt": replayed, "replayed": True,
                    "writer_called": False, "invalidated_checks": True,
                    "grants_verification": False}
        recorded = self.patch_ledger.record(patch.patch_id)
        if recorded is not None and recorded.get("state") == PATCH_UNCERTAIN:
            # A previous attempt left the target's real state unknown. Nothing may
            # be written until that is resolved from the candidate's own bytes.
            return {"schema": APPLICATION_SCHEMA, "receipt": recorded, "replayed": False,
                    "writer_called": False, "uncertain": True,
                    "refused": {"reason": (
                        "the previous application of this patch has an uncertain outcome; "
                        "the target's real bytes must be reconciled before any retry")},
                    "invalidated_checks": False, "grants_verification": False}
        # Re-read the authoritative facts at the write boundary: the decision that
        # was valid at acceptance is not a capability.
        try:
            with self.candidates.mutation(candidate_id=patch.target_candidate_id, as_runtime=True), self.reducer.publication_guard():
                facts = self._authoritative_facts(patch)
                decision = decide_promotion(patch, facts)
                if not decision.accepted:
                    return {"schema": APPLICATION_SCHEMA, "receipt": None, "replayed": False,
                            "writer_called": False, "refused": decision.to_dict(),
                            "invalidated_checks": False, "grants_verification": False}
                # The revision the write really starts from, read while exclusive.
                expected = facts.native_revision
                if not isinstance(expected, str) or not expected:
                    return {"schema": APPLICATION_SCHEMA, "receipt": None, "replayed": False,
                            "writer_called": False,
                            "refused": {"reason": "the target revision could not be observed "
                                                  "at the write boundary"},
                            "invalidated_checks": False, "grants_verification": False}
                self.patch_ledger.begin_apply(patch, operation_id=operation_id,
                                              native_revision=expected,
                                              cancel_epoch=facts.cancel_epoch, facts=facts)
                self._persist_patch(patch.patch_id)
                try:
                    observed = apply()
                except BaseException as error:
                    # A write that did not complete is never reported as applied.
                    # Whether bytes landed is unknown, so the record is explicitly
                    # uncertain and the caller must reconcile the real files first.
                    self.patch_ledger.fail(patch, reason=f"{type(error).__name__}: {error}",
                                           uncertain=True)
                    self._persist_patch(patch.patch_id)
                    raise
                applied = self._candidate_revision(patch.target_candidate_id) \
                    if self._candidate_revision is not None else expected
                if not isinstance(observed, str) or observed != patch.patch_sha256:
                    self.patch_ledger.fail(patch, reason="the applied bytes do not match the accepted patch hash", uncertain=True)
                    self._persist_patch(patch.patch_id)
                    raise PatchConflict("the applied bytes do not match the accepted patch hash")
                receipt = self.patch_ledger.complete(patch, observed_sha256=observed, native_revision=applied)
                self._persist_patch(patch.patch_id)
        except PatchConflict:
            raise
        if not isinstance(observed, str) or observed != patch.patch_sha256:
            self.patch_ledger.fail(
                patch, reason="the bytes that landed do not match the accepted patch hash",
                uncertain=True)
            self._persist_patch(patch.patch_id)
            raise PatchConflict("the applied bytes do not match the accepted patch hash")
        return {"schema": APPLICATION_SCHEMA, "receipt": receipt, "replayed": False,
                "writer_called": True, "invalidated_checks": True,
                "grants_verification": False}

    def reconcile_patch(
        self, patch: CandidatePatch, *, operation_id: str, candidate_root: Path,
    ) -> dict[str, Any]:
        """Resolve an uncertain application from the target candidate's real bytes.

        The Runtime re-hashes every path the patch changes and hands those native
        facts to the ledger, which decides whether the write had landed, had not
        started, or stopped part-way. Without this step the patch is neither
        replayed nor declared successful.
        """
        record = self.patch_ledger.record(patch.patch_id)
        if record is None or record.get("state") != PATCH_UNCERTAIN:
            raise PatchConflict("only an uncertain application can be reconciled")
        root = Path(candidate_root).resolve()
        resolve_root = getattr(self, "_candidate_root", None)
        if not callable(resolve_root) or resolve_root(patch.target_candidate_id) is None \
                or root != Path(resolve_root(patch.target_candidate_id)).resolve():
            raise PatchConflict("reconciliation requires the Runtime-owned target candidate")
        with self.candidates.mutation(candidate_id=patch.target_candidate_id, as_runtime=True), self.reducer.publication_guard():
            observed: dict[str, str | None] = {}
            for name in patch.changed_paths:
                path = root / name
                if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
                    raise PatchConflict(f"the reconciliation path for {name} escapes the candidate")
                observed[name] = (hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
            revision = self._candidate_revision(patch.target_candidate_id) \
                if self._candidate_revision is not None else "unobserved"
            record = self.patch_ledger.reconcile(patch, operation_id=operation_id,
                                                 target_revision=revision,
                                                 byte_identity=observed)
            self._persist_patch(patch.patch_id)
        return {"schema": APPLICATION_SCHEMA, "receipt": record,
                "reconciled": record.get("reconciled"), "applied": record.get("applied")}

    def poll_task_results(self, *, task_ids: Collection[str] | None = None,                          cursor: int = 0) -> dict[str, Any]:
        """New results since ``cursor``. Read-only: it reserves no model budget."""
        results, next_cursor = self.mailbox.results_since(cursor, task_ids=task_ids)
        return {
            "schema": "minicode-builder-poll-v1",
            "cursor": next_cursor,
            "results": [item.to_dict() for item in results],
            "states": {task_id: self._task_state(task_id) for task_id in
                       (task_ids if task_ids is not None else self._builder_tasks)},
            "milestones": {task_id: self.task_milestones(task_id) for task_id in
                           (task_ids if task_ids is not None else self._builder_tasks)},
        }

    def _task_state(self, task_id: str) -> str:
        try:
            return self.reducer.node_state(task_id).value
        except Exception:  # noqa: BLE001 - an unknown task has no state, not an error
            return "UNKNOWN"

    def task_milestones(self, task_id: str) -> dict[str, Any]:
        """The three dependency milestones, kept distinct on purpose.

        ``artifact_validated`` is a versioned, hash-bearing artifact; Code
        depending on a patch must wait for ``patch_applied_and_checked``;
        ``terminal_observed`` only says the outcome is readable.
        """
        latest = self.mailbox.latest_for(task_id)
        state = self._task_state(task_id)
        result = None
        candidate_id = self._builder_tasks.get(task_id, {}).get("candidate_id")
        try:
            result = self.reducer.node_result(task_id)
        except Exception:  # noqa: BLE001 - no result yet is a state, not a failure
            result = None
        artifact_refs = tuple(getattr(result, "artifact_refs", ()) or ())
        return {
            "task_id": task_id,
            "candidate_id": candidate_id,
            "state": state,
            "artifact_validated": bool(latest and latest.terminal == "succeeded" and latest.artifact_sha256
                and not self.reducer.node_cancel_epoch(task_id)),
            "patch_applied_and_checked": bool(latest and latest.terminal == "succeeded" and latest.patch_applied
                and latest.checked and not self.reducer.node_cancel_epoch(task_id)),
            "terminal_observed": bool(latest and latest.terminal) or state in {
                "SUCCEEDED", "FAILED_TERMINAL", "FAILED_RETRYABLE", "CANCELED"},
            "observed_revision": latest.revision if latest else None,
            "terminal": latest.terminal if latest else None,
        }

    def wait_dependencies(
        self, *, owner_node_id: str, edges: Sequence[Mapping[str, Any]], mode: str = "all",
        failure_policy: str = "block", timeout_seconds: float | None = None,
        wait_id: str | None = None, checkpoint_ref: str | None = None,
        timeout_policy: str = "block",
    ) -> dict[str, Any]:
        """Register a versioned WaitSpec and evaluate it in the same step.

        Registration and the check against results that already exist happen
        together, so a result that arrived first and a result that arrives later
        both produce exactly one release. ``all`` waits only the declared set;
        ``any`` releases once on the first satisfying edge.
        """
        try:
            with self._dispatch_lock, self.reducer.publication_guard():
                declared = [edge["task_id"] for edge in edges]
                generations = [self._candidate_generations.get(self.candidate_id_for(task_id), 1)
                               for task_id in declared]
                spec = WaitSpec(
                    wait_id=new_id(f"wait-{owner_node_id}") if wait_id is None else wait_id,
                    generation=min(generations) if generations else 1, owner_node_id=owner_node_id,
                    edges=tuple(DependencyEdge(task_id=edge["task_id"], predicate=edge["predicate"],
                        required=edge.get("required", True), generation=generation)
                        for edge, generation in zip(edges, generations)),
                    mode=mode, failure_policy=failure_policy, timeout_seconds=timeout_seconds,
                    cancel_epoch=self.reducer.node_cancel_epoch(owner_node_id),
                    checkpoint_ref=checkpoint_ref, created_at=self.clock.now(),
                    timeout_policy=timeout_policy,
                    schema_version="minicode-agent-wait-v1" if timeout_policy == "return" else "minicode-dependency-wait-v1")
                self.reducer.register_dependency_wait(spec.to_dict())
                spec = WaitSpec.from_dict(self.reducer.dependency_wait(spec.wait_id))
                self._waits[spec.wait_id] = spec
                decision = self.reducer.dependency_wait_decision(spec.wait_id)
                if decision.get("resume_allowed"):
                    self.reducer.record_wait_resume(spec.wait_id)
                return {"schema": "minicode-dependency-wait-v1", "registered": True,
                        "wait": spec.to_dict(), "decision": decision,
                        "resumed": self.reducer.wait_resumes(spec.wait_id)}
        except (WaitError, ReducerError, KeyError, TypeError, AttributeError) as error:
            return {"schema": "minicode-dependency-wait-v1", "registered": False,
                    "error": f"{type(error).__name__}: {error}"}

    def dependency_status(self, wait_id: str) -> dict[str, Any]:
        try:
            decision = self.reducer.dependency_wait_decision(wait_id)
        except KeyError:
            return {"schema": "minicode-dependency-wait-v1", "known": False, "wait_id": wait_id}
        return {"schema": "minicode-dependency-wait-v1", "known": True,
                "durable_resumes": self.reducer.wait_resumes(wait_id),
                "decision": decision}

    def _wake_waiting_owner(self, spec: WaitSpec, decision: Mapping[str, Any]) -> dict[str, Any]:
        """Turn a satisfied wait into exactly ONE durable resume of its owner.

        Querying a wait is not resuming it: a status lookup that merely reported
        ``resume_allowed`` left the owner suspended forever and counted nothing
        durably. This consumption step requires a real suspended owner and uses
        the reducer's durable gate to prevent duplicate or out-of-order resumes.
        """
        owner = spec.owner_node_id
        if not owner:
            return {"woke": False, "reason": "the wait declares no owner to resume"}
        if not decision.get("resume_allowed"):
            return {"woke": False, "reason": decision.get("reason") or "the wait is not satisfied",
                    "status": decision.get("status")}
        if self.reducer.node_state(owner) is not NodeState.WAITING_CHILDREN:
            # Either it never yielded, or it has already been resumed. Both mean
            # this observation must not create an activation.
            return {"woke": False,
                    "reason": f"the owner {owner} is not suspended on its dependencies",
                    "owner_state": self.reducer.node_state(owner).value}
        # Recheck and consume against durable state under the reducer lock.
        resumed, reason = self.reducer.resume_from_wait(
            owner_node_id=owner, wait_id=spec.wait_id,
            consumed_cursor=int(decision.get("cursor") or 0))
        if not resumed:
            return {"woke": False, "reason": reason,
                    "durable_resumes": self.reducer.wait_resumes(spec.wait_id)}
        self._dispatch_wake.set()
        return {"woke": True, "owner_node_id": owner, "owner_resumed": True,
                "durable_resumes": self.reducer.wait_resumes(spec.wait_id),
                "cursor": decision.get("cursor")}

    def bind_builder_lifecycle(self, lifecycle) -> None:
        """Bind the Runtime-side object that owns Builder candidates and patches.

        The Scheduler drives it: a Builder node gets its own isolated candidate
        before its Worker starts, and the produced patch is exported and persisted
        after it returns. Nothing here grants the primary any new write right to
        the user's target directory.
        """
        if lifecycle is None:
            raise ValueError("a builder lifecycle binding is required")
        for name in ("open_candidate", "publish_patch"):
            if not callable(getattr(lifecycle, name, None)):
                raise ValueError(f"a builder lifecycle must implement {name}")
        self._builder_lifecycle = lifecycle

    def _builder_lifecycle_for(self, node_id: str) -> Any | None:
        lifecycle = getattr(self, "_builder_lifecycle", None)
        if lifecycle is None or not node_id.startswith("builder-"):
            return None
        return lifecycle

    @staticmethod
    def _isolated_candidate_root(worker: Any, node: PlanNode) -> Any | None:
        """The runtime-owned isolated candidate this writer may write, if any.

        A worker that keeps no isolated candidate (a fully synchronous primary is
        working directly in the user's target directory) has no candidate
        boundary, and the gate is simply unbound for it.
        """
        reader = getattr(worker, "isolated_candidate_root", None)
        if not callable(reader):
            return None
        try:
            return reader(node.node_id)
        except Exception:  # noqa: BLE001 - an unreadable candidate is "no boundary"
            return None

    def _export_builder_read_set(self, node: PlanNode,
                                 invocation: AgentInvocationSpec | None) -> NativeReadSet:
        """Actual tool observations; manifest file hashes are not visible file bytes."""
        observed: NativeReadSet | None = None
        reader = getattr(self._execute_worker, "native_read_set_for", None)
        if callable(reader):
            try:
                candidate = reader(node.node_id)
            except Exception:  # noqa: BLE001 - an unreadable read set is incomplete
                candidate = None
            if isinstance(candidate, NativeReadSet):
                observed = candidate
        if observed is None:
            observed = NativeReadSet()
            observed.record_dynamic("activation-read-capture-unavailable")
        if not observed.complete and not observed.omissions:
            observed.record_dynamic("read-set-unknown")
        return observed

    def _open_builder_candidate(self, node: PlanNode, invocation: AgentInvocationSpec):
        """Give a Builder its own isolated candidate before its Worker runs."""
        lifecycle = self._builder_lifecycle_for(node.node_id)
        if lifecycle is None:
            return None
        for preparation in range(3):
            try:
                return lifecycle.open_candidate(node_id=node.node_id, attempt_id=invocation.attempt_id)
            except Exception as error:  # noqa: BLE001 - retain the real setup cause
                # No Worker has started and create() cleans its owned partial
                # directory. Only transient copy I/O gets a bounded local retry;
                # authority/conflict errors are never retried as filesystem I/O.
                if (isinstance(error, (OSError, shutil.Error)) and preparation < 2
                        and not self.cancellation_event.wait(0.1)
                        and not self.reducer.node_cancel_epoch(node.node_id)):
                    continue
                logging.getLogger(__name__).exception("Builder candidate preparation failed: %s", node.node_id)
                self._reasons.append(
                    f"builder candidate unavailable ({type(error).__name__}): {error}")
                raise

    def _export_builder_patch(self, node: PlanNode, invocation: AgentInvocationSpec | None,
                              result: AgentResult) -> None:
        """Export and persist the Builder's real bytes as an exact patch."""
        lifecycle = self._builder_lifecycle_for(node.node_id)
        if (lifecycle is None or result.status is not AgentResultStatus.SUCCEEDED
                or self.reducer.node_state(node.node_id) is not NodeState.SUCCEEDED):
            return
        handle = getattr(lifecycle.builders.get(node.node_id), "handle", None)
        if handle is None:
            return
        from minicode.builder_delegation import export_candidate_patch

        try:
            patch = export_candidate_patch(
                handle,
                patch_id=f"patch-{node.node_id}",
                owner_node_id=node.node_id,
                owner_attempt_id=result.attempt_id,
                owner_generation=self._candidate_generations.get(
                    self.candidate_id_for(node), 1),
                owner_fencing_token=self._fencing_tokens.get(node.node_id, 1),
                plan_version=result.plan_version,
                contract_revision=result.contract_revision,
                target_candidate_id="primary",
                producer_cancel_epoch=self.reducer.candidate_cancel_epoch(
                    self.candidate_id_for(node)),
                dependency_vector=lifecycle.builders[node.node_id].dependency_vector,
                native_read_set=self._export_builder_read_set(node, invocation),
            )
        except Exception as error:  # noqa: BLE001 - no patch is an outcome, not a crash
            self._reasons.append(
                f"builder patch export refused ({type(error).__name__}): {error}")
            return
        lifecycle.publish_patch(node_id=node.node_id, patch=patch)

    def _builder_dependency_vector(self, node: PlanNode) -> DependencyVector:
        """Pin, per declared edge, the dependency facts this Builder built against."""

        return self._dependency_vector_for(node)

    def refresh_task_outcome(self, node_id: str) -> bool:
        """Republish a child's outcome after its patch facts changed.

        The produced-and-not-applied outcome is a real, auditable fact, and so is
        the applied-and-checked one. Publishing the second does not rewrite the
        first: both stay readable, and the newest one is what a dependency edge
        evaluates against.
        """
        result = self.reducer.node_result(node_id)
        if result is None:
            return False
        try:
            node = self._node_definition(node_id)
        except KeyError:
            return False
        self._publish_task_outcome(node, result)
        return True

    def cancel_task(self, *, task_id: str, reason: str = "canceled by the Runtime",
                    subtree: bool = True, operation_id: str | None = None,
                    dependency_successors: bool = False) -> dict[str, Any]:
        """Cancel one task, its CREATION subtree, and optionally its dependents.

        ``subtree`` follows who registered whom; a node that merely depends on the
        canceled task is not part of its subtree and is only canceled when the
        caller explicitly asks for ``dependency_successors``. The same
        ``operation_id`` is idempotent: repeating it does not advance any epoch
        and does not re-cancel anything.
        """
        with self._dispatch_lock:
            events = self.reducer.cancel_task(
                task_id, reason=reason, subtree=subtree,
                operation_id=operation_id, dependency_successors=dependency_successors)
            # Two DIFFERENT facts, and conflating them is how a settled result gets
            # erased: ``reported`` is what the caller is told this operation covers
            # (including a replay, whose persisted intent names its targets), while
            # ``stopped`` is what this command actually canceled. Only a target that
            # was really stopped may have its outcome republished as a cancellation.
            reported = {event.payload.get("node_id") for event in events
                        if event.event_type.value == "NodeCanceled"}
            stopped = {str(item) for event in events
                       if event.event_type.value == "TaskCancelIntent"
                       for item in (event.payload.get("canceled_targets") or ())}
            if not reported:
                reported = {str(item) for event in events
                            if event.event_type.value == "TaskCancelIntent"
                            for item in (event.payload.get("targets") or ())}
            for future, (node, _invocation, _token) in list((getattr(self, "_active_futures", None) or {}).items()):
                if node.node_id not in stopped:
                    continue
                if future.cancel():
                    self.reducer.record_worker_completion(self._canceled_result(_invocation))
                    reservation = getattr(self, "_active_reservations", {}).pop(future, None)
                    if reservation is not None:
                        self.capacity.release(reservation)
            for node_id in sorted(stopped):
                result = self.reducer.node_result(node_id)
                if result is not None:
                    self._publish_task_outcome(self._node_definition(node_id), result)
                else:
                    with self.reducer.publication_guard():
                        outcome = TaskOutcome(task_id=node_id,
                            generation=self._candidate_generations.get(self.candidate_id_for(node_id), 1),
                            candidate_id=self.candidate_id_for(node_id), revision="", terminal="canceled",
                            artifact_sha256=None, patch_applied=False, checked=False,
                            event_id=f"cancel-outcome:{node_id}:{self.reducer.node_cancel_epoch(node_id)}",
                            sequence=self.reducer.mailbox_sequence() + 1)
                        if self.reducer.record_child_outcome(outcome.to_dict()):
                            self.mailbox.publish(outcome)
            self._wake_satisfied_waits()
            self._dispatch_wake.set()
        return {"schema": "minicode-task-cancel-v1", "task_id": task_id, "subtree": subtree,
                "operation_id": operation_id, "reason": reason,
                "cancel_epoch": self.reducer.node_cancel_epoch(task_id),
                "canceled_node_ids": sorted(reported),
                "notice": "Cancellation is an intent plus a per-task epoch; already-running "
                          "external work may still finish and is reported, never claimed to "
                          "have stopped instantly."}

    def _publish_task_outcome(self, node: PlanNode, result: AgentResult) -> None:
        with self.reducer.publication_guard():
            self._publish_task_outcome_locked(node, result)

    @staticmethod
    def _patch_checked(record: Mapping[str, Any]) -> bool:
        checks = record.get("checks") or {}
        return record.get("state") == PATCH_APPLIED and bool(checks) and all(
            isinstance(item, Mapping) and item.get("passed") is True for item in checks.values())

    def _publish_task_outcome_locked(self, node: PlanNode, result: AgentResult) -> None:
        """Persist one child result into the durable mailbox, idempotently.

        The applied/checked facts come from the real patch receipt -- never from a
        writer node merely having succeeded. Producing an artifact is one fact;
        having it applied to the target candidate and passing the combination check
        is a different one.
        """
        terminal = {
            AgentResultStatus.SUCCEEDED: "succeeded",
            AgentResultStatus.FAILED_TERMINAL: "failed",
            AgentResultStatus.FAILED_RETRYABLE: "failed",
            AgentResultStatus.CANCELED: "canceled",
        }.get(result.status)
        # The durable record is the source of truth for the applied/checked facts.
        # A Builder's exported patch IS its artifact, so a produced patch that has
        # not been accepted and applied still counts as artifact_validated and
        # still does NOT count as patch_applied_and_checked.
        record = self.reducer.patch_records().get(f"patch-{node.node_id}") or {}
        applied = record.get("state") == PATCH_APPLIED
        ledger_checks = record.get("checks") or {}
        # Checked means every registered check explicitly PASSED. A missing result,
        # an unknown shape, a crash or a refusal all leave the predicate unsatisfied,
        # so a code dependency can never be released by an unchecked combination.
        checked = applied and bool(ledger_checks) and all(
            isinstance(item, Mapping) and item.get("passed") is True
            for item in ledger_checks.values())
        # A cancelled producer's late success is audit data, never a success edge:
        # the outcome is published as cancelled so no declared dependency on it can
        # be released by work the Runtime already stopped.
        if self.reducer.node_cancel_epoch(node.node_id) > 0:
            terminal = "canceled"
            applied = False
            checked = False
        elif self.reducer.node_state(node.node_id) in {NodeState.FAILED_TERMINAL, NodeState.FAILED_RETRYABLE}:
            terminal, applied, checked = "failed", False, False
        artifact = next((ref.content_hash for ref in getattr(result, "artifact_refs", ()) or ()), None)
        if artifact is None:
            artifact = record.get("patch_sha256") or record.get("observed_sha256")
        identity = hashlib.sha256(json.dumps({"record": record, "terminal": terminal,
            "cancel_epoch": self.reducer.node_cancel_epoch(node.node_id), "artifact": artifact},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        outcome = TaskOutcome(
            task_id=node.node_id,
            generation=self._candidate_generations.get(self.candidate_id_for(node), 1),
            candidate_id=self.candidate_id_for(node),
            revision=(record.get("applied_revision") or record.get("candidate_revision")
                      or result.base_revision or ""),
            artifact_sha256=artifact,
            patch_applied=applied,
            checked=bool(checked),
            terminal=terminal,
            event_id=(f"outcome-{self.reducer.run_id}-{node.node_id}-{result.attempt_id}"
                      f"-{identity}"),
            sequence=self.reducer.mailbox_sequence() + 1,
            cause_event_id=record.get("operation_id"),
        )
        if self.reducer.record_child_outcome(outcome.to_dict()):
            self.mailbox.publish(outcome)
            # A result that satisfies a registered wait releases its owner once,
            # HERE, so the wake is driven by the durable outcome rather than by
            # the owner happening to ask again later.
            self._wake_satisfied_waits()

    def _wake_satisfied_waits(self) -> tuple[dict[str, Any], ...]:
        """Release every suspended owner whose declared wait is now satisfied."""
        self.reducer.wake_for_user_input()
        wakes: list[dict[str, Any]] = []
        for payload in self.reducer.dependency_waits():
            spec = WaitSpec.from_dict(payload)
            wait_id = spec.wait_id
            if not spec.owner_node_id:
                continue
            if self.reducer.node_state(spec.owner_node_id) is not NodeState.WAITING_CHILDREN:
                continue
            decision = self.reducer.dependency_wait_decision(wait_id)
            if not decision.get("resume_allowed"):
                if decision.get("status") == "blocked":
                    self.reducer.fail_dependency_wait(spec.wait_id)
                continue
            wakes.append({"wait_id": wait_id, **self._wake_waiting_owner(spec, decision)})
        return tuple(wakes)

    def execute_delegation(
        self,
        request: DelegationRequest,
        *,
        worker: Worker,
        context_builder: ContextBuilder,
        node_verifier: NodeVerifier,
    ) -> AgentResult:
        with self._maintain_leases():
            try:
                return self._execute_delegation(
                    request, worker=worker, context_builder=context_builder, node_verifier=node_verifier,
                )
            finally:
                if self.on_activity:
                    self.on_activity()

    def _execute_delegation(
        self,
        request: DelegationRequest,
        *,
        worker: Worker,
        context_builder: ContextBuilder,
        node_verifier: NodeVerifier,
    ) -> AgentResult:
        """Run a governed synchronous child in the suspended parent slot.

        The child is appended to the canonical PlanGraph, claimed and fenced by
        this Scheduler, and reduced like every preplanned node.  Because the
        parent calls this method synchronously, max_active_workers=1 remains
        true even though the child has its own Node/Attempt identity.
        """
        if not self.profile.dynamic_worker_delegation:
            return self._delegation_denied(request, "dynamic Worker delegation is disabled for this profile")
        if request.run_id != self.reducer.run_id:
            return self._delegation_denied(request, "delegation run_id does not match Runtime")
        if self.profile.primary_led:
            process = self.reducer.required_consultation_status()
            if process and not process["satisfied"]:
                return self._delegation_denied(request, process["reason"] + "; use durable consult_agents, not synchronous task")
            try:
                parent = self._node_definition(request.parent_node_id)
            except KeyError:
                return self._delegation_denied(request, "delegation parent does not exist")
            attempt = self.reducer.current_attempt(request.parent_node_id)
            if (
                parent.role != "primary"
                or self.reducer.node_state(parent.node_id) is not NodeState.RUNNING
                or attempt is None
                or request.parent_attempt_id != attempt.attempt_id
                or not attempt.lease.is_valid(self.clock.now())
            ):
                return self._delegation_denied(request, "only the active primary attempt may request helpers")
            if (
                not request.allowed_tool_capabilities
                or any(effect is not ToolEffect.READ_ONLY for effect in request.allowed_tool_capabilities)
                or not set(request.allowed_tool_capabilities).issubset(parent.allowed_effects)
            ):
                return self._delegation_denied(request, "primary-led helpers must remain within the parent's read-only scope")
            if request.delegation_depth != 1:
                return self._delegation_denied(request, "primary-led helpers cannot recursively delegate")
        if request.delegation_depth > self.profile.max_delegation_depth:
            return self._delegation_denied(request, "max_delegation_depth exceeded")
        if self.cancellation_event.is_set():
            return self._delegation_denied(request, "parent run is canceled", canceled=True)
        if self._budget_exhausted():
            reason = self._reasons[-1]
            self.cancel(reason)
            return self._delegation_denied(request, reason, canceled=True)
        digest = hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()
        node_id = f"delegated-{digest}"
        role = {
            "explore": "explorer", "plan": "planner" if self.profile.primary_led else "explorer",
            "review": "reviewer", "verify": "verifier", "general": "writer",
        }.get(request.requested_role)
        if role is None or (self.profile.primary_led and role == "writer"):
            return self._delegation_denied(request, "requested helper role is not allowed")
        node = PlanNode(
            node_id=node_id,
            objective=request.objective,
            role=role,
            allowed_effects=request.allowed_tool_capabilities,
            acceptance_criteria=("return a structured result to the parent Worker",),
            max_attempts=max(1, min(1 + self.profile.retry_budget, 2)),
        )
        if self.reducer.closure_reserve_enabled:
            existing = next((item for item in self.reducer.current_plan.nodes if item.node_id == node_id), None)
            if existing == node and self.reducer.node_state(node_id) is NodeState.SUCCEEDED:
                return self.reducer.node_result(node_id)
            admission = self.reducer.admit_consultation_budget(request.parent_node_id, request.parent_attempt_id, (request.objective,),
                helper_token_limits=(None if request.proposed_budget.max_tokens is None else
                                     request.proposed_budget.max_tokens * node.max_attempts,))
            if not admission["allowed"]:
                return self._delegation_denied(request, admission["reason"] +
                    "; no helper dispatched; do not retry unchanged; continue directly only if delegation is optional, otherwise report the unmet requirement")
        with self._dispatch_lock:
            claimed = self._claim_delegation(request, node)
        if isinstance(claimed, AgentResult):
            return claimed
        attempt_id, fencing_token = claimed
        if self.on_activity:
            self.on_activity()
        result = self._invoke_delegation(request, node, attempt_id, worker, context_builder)
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
            return replace(
                result, status=AgentResultStatus.FAILED_TERMINAL,
                output_summary="delegated result was rejected by Runtime",
                failure=ExecutionFailure(ExecutionFailureKind.POLICY_DENIED, "delegated result was not accepted"),
            )
        result = self.reducer.node_result(node.node_id) or result
        if self._budget_exhausted(for_delivery=True, attempt_id=result.attempt_id):
            reason = self._reasons[-1]
            self.cancel(reason)
            self._discard_worker_result(worker, result)
            # The stop reason is preserved as the failure kind: a bounded budget
            # that refused delivery is BudgetExhausted (or a deadline Timeout), not
            # a user cancellation.
            return replace(
                result,
                status=AgentResultStatus.CANCELED,
                failure=ExecutionFailure(self._budget_failure_kind(reason), reason),
            )
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
        if self.reducer.node_state(node.node_id) is not NodeState.SUCCEEDED:
            return replace(
                result, status=AgentResultStatus.FAILED_TERMINAL,
                output_summary="delegated result failed verification",
                failure=ExecutionFailure(ExecutionFailureKind.VERIFICATION_FAILED, "delegated result failed verification"),
            )
        return result

    def _invoke_delegation(
        self, request: DelegationRequest, node: PlanNode, attempt_id: str,
        worker: Worker, context_builder: ContextBuilder,
    ) -> AgentResult:
        # Registration and claim are atomic under the dispatch lock. All child
        # initialization happens afterwards: workspace adapters and context
        # builders may inspect its canonical role/attempt. Replays and rejected
        # admissions never bind a workspace or run initialization again.
        stage = "workspace binding"
        try:
            bind_workspace = getattr(worker, "bind_delegation_workspace", None)
            if self.profile.primary_led and callable(bind_workspace):
                bind_workspace(request.parent_node_id, request.parent_attempt_id, node.node_id)
            stage = "context assembly"
            manifest = context_builder(node, attempt_id, request.proposed_budget)
            manifest = replace(manifest, retry_feedback=self.reducer.retry_feedback(node.node_id))
            stage = "invocation assembly"
            invocation = AgentInvocationSpec(
                run_id=request.run_id, plan_version=self.reducer.current_plan.plan_version,
                node_id=node.node_id, attempt_id=attempt_id, role=node.role, objective=node.objective,
                context_manifest=manifest, allowed_effects=node.allowed_effects,
                budget=request.proposed_budget, acceptance_criteria=node.acceptance_criteria,
                idempotency_key=request.idempotency_key, reasoning_budget=self.profile.reasoning_budget,
            )
        except BaseException as error:
            # Use the execution boundary's interrupt semantics during every
            # initialization stage too; a child must not interrupt its parent.
            if isinstance(error, KeyboardInterrupt) and threading.current_thread() is threading.main_thread():
                raise
            # Initialization failures settle the registered child through the
            # same result/cleanup path as execution failures. They are runtime
            # errors, not policy denials. No Worker/model has started.
            return AgentResult(
                run_id=request.run_id, plan_version=self.reducer.current_plan.plan_version,
                node_id=node.node_id, attempt_id=attempt_id,
                status=AgentResultStatus.FAILED_TERMINAL, base_revision="unavailable",
                contract_revision=self.reducer.current_plan.contract_revision,
                output_summary=f"delegation {stage} failed",
                usage={"tokens": 0, "cost": 0.0},
                failure=ExecutionFailure(ExecutionFailureKind.PROTOCOL_ERROR, f"{stage} failed ({type(error).__name__}): {error}"),
            )
        # The primary is synchronously suspended. Do not create another
        # executor or return before the child exits.
        try:
            return self._validated_worker_result(worker.run(invocation), invocation)
        except BaseException as error:
            # A synchronous child runs on its parent's executor thread. An
            # extension's sys.exit/KeyboardInterrupt must remain a child failure.
            # A real interrupt on the application's main thread still propagates.
            if isinstance(error, KeyboardInterrupt) and threading.current_thread() is threading.main_thread():
                raise
            return self._crashed_result(error, node, invocation)

    def _claim_delegation(
        self, request: DelegationRequest, node: PlanNode,
    ) -> tuple[str, int] | AgentResult:
        """Register and claim before the main dispatch loop can see READY.

        This is a synchronous borrowed slot, not an asynchronous continuation.
        The lock prevents the ordinary executor from also launching the child.
        """
        try:
            existing = self._node_definition(node.node_id)
        except KeyError:
            existing = None
        if existing is not None:
            if existing != node:
                return self._delegation_denied(request, "delegation idempotency key conflicts with a different definition")
            persisted_result = self.reducer.node_result(node.node_id)
            state = self.reducer.node_state(node.node_id)
            if persisted_result is not None and state is NodeState.SUCCEEDED:
                return persisted_result
            if state is NodeState.FAILED_RETRYABLE:
                self.reducer.prepare_retry(node.node_id)
            elif state is not NodeState.READY:
                return self._delegation_denied(request, "matching delegation is active or did not pass verification")
        else:
            # Count registered work, not just claimed attempts, to reserve
            # planned verification capacity before accepting another helper.
            if self.reducer.collaboration_config:
                try:
                    self.reducer.validate_agent_budget(node.node_id)
                except ValueError as error:
                    return self._delegation_denied(request, str(error))
            elif len(self.reducer.current_plan.nodes) >= self.profile.max_agents:
                return self._delegation_denied(request, "max_agents budget exhausted")
            self.reducer.register_delegated_node(node, parent_node_id=request.parent_node_id)
        claimed = self._claim(node, idempotency_key=request.idempotency_key)
        return claimed if claimed is not None else self._delegation_denied(request, "Scheduler could not claim delegated node")

    def execute(
        self,
        worker: Worker,
        *,
        context_builder: ContextBuilder,
        node_verifier: NodeVerifier,
    ) -> SchedulerReport:
        if self.execution_drain.pending_nodes:
            raise RuntimeError("previous canceled executions have not exited; run ownership remains held")
        with self._maintain_leases():
            try:
                return self._execute(worker, context_builder=context_builder, node_verifier=node_verifier)
            finally:
                if self.on_activity:
                    self.on_activity()

    def _execute(
        self,
        worker: Worker,
        *,
        context_builder: ContextBuilder,
        node_verifier: NodeVerifier,
    ) -> SchedulerReport:
        max_workers = max(1, self.profile.max_active_workers)
        self.capacity.set_limits(max_workers=max_workers,
                                 max_writers=self.profile.effective_writer_capacity)
        active: dict[Future[AgentResult], tuple[PlanNode, AgentInvocationSpec, int]] = {}
        reservations: dict[Future[AgentResult], CapacityReservation] = {}
        self._active_futures = active
        self._active_reservations = reservations
        self._execute_worker = worker
        self._execute_context_builder = context_builder
        self._execute_node_verifier = node_verifier
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="minicode-worker")
        self._executor = executor
        cancellation_deadline: float | None = None
        try:
            while True:
                if self.on_activity:
                    self.on_activity()
                self._wake_satisfied_waits()
                self._cancel_orphaned_agents()
                if (self.cancellation_event.is_set() and self._stop_requested is None
                        and not self.reducer.snapshot().get("canceled")):
                    # A user/parent cancel fences results immediately. An
                    # internal budget stop also stops model/tools immediately,
                    # but must drain in-flight receipts before CANCEL_RUN makes
                    # SUBMIT_RESULT stale. Explicit cancel() still wins at once.
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
                        effectful = [
                            node
                            for node, _invocation, _token in active.values()
                            if any(
                                effect is not ToolEffect.READ_ONLY
                                for effect in node.allowed_effects
                            )
                        ]
                        self._reasons.append(
                            "cancellation grace expired; cleanup pending for "
                            + ", ".join(sorted(node.node_id for node, _invocation, _token in active.values()))
                            + ("; write capacity and workspace ownership remain held until actual exit"
                               if effectful else "; read-only executions have not exited")
                        )
                        break
                if not active:
                    self._recover_verifying(worker, node_verifier)
                    self._recover_child_publications(worker)
                    self._wake_satisfied_waits()
                    self._cancel_orphaned_agents()
                    self._prepare_retries()
                    self._recover_expired_attempts()
                    self._decline_optional_review_at_budget_limit()
                    if self.reducer.terminal():
                        break
                # Four separate decisions, deliberately not one:
                #   1. stop admitting NEW work   -- decided every pass, active or not;
                #   2. propagate a stop request  -- so live work learns it must wind
                #      down instead of appearing to run to a normal finish;
                #   3. collect late results and their real usage -- a result that
                #      arrives after the limit is still accounted, never discarded;
                #   4. permit final delivery     -- refused by the for_delivery check.
                # Gating all of this on `not active` is what previously let a Worker
                # outlive its own deadline without ever seeing the stop request.
                admission_exhausted = (not self.cancellation_event.is_set() and not self._stop_requested
                                      and self._budget_exhausted())
                if (not self.cancellation_event.is_set()
                        and not self._stop_requested
                        and admission_exhausted and not self._optional_review_closeout_can_settle()):
                    self._stop_requested = self._reasons[-1]
                    # Publish the reason to the activations that are already running
                    # before the cancellation reaches them: they report the failure,
                    # and a stop caused by an exhausted budget must not read as a
                    # user cancellation. Announced before the event, never after.
                    note_reason = getattr(worker, "note_stop_reason", None)
                    if callable(note_reason):
                        note_reason(self._stop_requested)
                    # Stop actual model/tool execution immediately. The reducer
                    # still collects each in-flight result and its usage below.
                    self.cancellation_event.set()
                    self._reasons.append(
                        f"budget or deadline reached ({self._stop_requested}); no new work "
                        "will be admitted while active Workers wind down")

                if self.cancellation_event.is_set() and not active:
                    if self._stop_requested is not None:
                        self.cancel(self._stop_requested)
                    break

                # Every submitted node already holds a reservation, including the
                # ones still queued inside the executor, so the ceiling cannot be
                # exceeded by several nodes observing "0 active" in one pass.
                capacity = max_workers - self.capacity.occupied
                context_failed = False
                if (capacity > 0 and not self.cancellation_event.is_set()
                        and self._stop_requested is None and not admission_exhausted):
                    for node_id in sorted(self.reducer.ready_node_ids(), key=lambda item: item != "primary"):
                        if capacity <= 0:
                            break
                        node = self._node_definition(node_id)
                        if self.profile.primary_led and node_id.startswith("delegated-"):
                            # P1a consultations run only on the suspended
                            # primary's stack, including retries. Dispatching
                            # one here could race its candidate or revive a
                            # discarded workspace after parent termination.
                            continue
                        if not self._can_claim(node):
                            continue
                        shared_ready = getattr(worker, "shared_workspace_ready", None)
                        if callable(shared_ready) and not shared_ready(node_id) and active:
                            continue
                        continuation = self.reducer.continuation_for(node_id)
                        if continuation is not None:
                            deliver = getattr(worker, "deliver_consultation_results", None)
                            if not callable(deliver):
                                raise ValueError("Worker does not support durable consultation delivery")
                            snapshot = self.reducer.snapshot()
                            deliver(node_id, [
                                {"node_id": child.node_id, "state": self.reducer.node_state(child.node_id).value,
                                 "input_revision": continuation.input_revision,
                                 "criticality": child.criticality,
                                 "diagnosis": self.reducer.node_diagnosis(child.node_id),
                                 "result": self._consultation_result(child.node_id),
                                 "verification_records": [event["payload"]["record"] for event in snapshot["events"]
                                                          if event["payload"].get("node_id") == child.node_id
                                                          and event["payload"].get("attempt_id") == snapshot["nodes"][child.node_id].get("current_attempt_id")
                                                          and event["payload"].get("record")]}
                                for child in continuation.children
                            ])
                        claimed = self._claim(node)
                        if claimed is None:
                            continue
                        attempt_id, fencing_token = claimed
                        if continuation is not None and continuation.kind == "consultation":
                            self.reducer.record_consultation_delivery(node_id, attempt_id)
                        node_budget = self.profile.budget
                        if node_id.startswith("consult-"):
                            node_budget = self.reducer.consultation_worker_budget(node_budget)
                        try:
                            manifest = context_builder(node, attempt_id, node_budget)
                        except (Exception, SystemExit) as error:
                            # No Worker has started. Persist an explicit failure
                            # instead of leaving a claimed attempt orphaned.
                            failure = AgentResult(
                                self.reducer.run_id, self.reducer.current_plan.plan_version,
                                node_id, attempt_id, AgentResultStatus.FAILED_TERMINAL, "unavailable",
                                "context assembly failed", contract_revision=self.reducer.current_plan.contract_revision,
                                usage={"tokens": 0, "cost": 0.0},
                                failure=ExecutionFailure(ExecutionFailureKind.PROTOCOL_ERROR, f"context assembly failed ({type(error).__name__}): {error}"),
                            )
                            # The activation never started, so its exit and zero
                            # usage are known. Persist both before waking waiters
                            # or exposing an automatic completion notice.
                            self.reducer.record_worker_completion(failure)
                            self._account_usage(failure)
                            self._persist_worker_result(failure, fencing_token)
                            if node_id != "primary":
                                self._publish_task_outcome(node, failure)
                            context_failed = True
                            continue
                        allowed_effects = node.allowed_effects
                        if node.role == "primary" and self.reducer.repair_count:
                            allowed_effects = tuple(effect for effect in allowed_effects if effect in {ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE})
                        manifest = replace(manifest, retry_feedback=self.reducer.retry_feedback(node.node_id),
                                           tool_capability_scope=allowed_effects, context_files=node.context_files)
                        attached = self.reducer.agent_sessions().get(node_id, {}).get("project_report_refs", ())
                        if attached:
                            manifest = replace(manifest, artifact_refs=(*manifest.artifact_refs,
                                *(ArtifactRef(**ref) for ref in attached)))
                        invocation = AgentInvocationSpec(
                            run_id=self.reducer.run_id,
                            plan_version=self.reducer.current_plan.plan_version,
                            node_id=node.node_id,
                            attempt_id=attempt_id,
                            role=node.role,
                            objective=node.objective,
                            context_manifest=manifest,
                            allowed_effects=allowed_effects,
                            budget=node_budget,
                            acceptance_criteria=node.acceptance_criteria,
                            idempotency_key=self._idempotency_keys.get(node.node_id, ""),
                            reasoning_budget=(node.review_policy["reasoning_budget"]
                                if node.node_id == "final-verifier" and node.review_policy is not None
                                else self.profile.reasoning_budget),
                        )
                        candidate_id = self.candidate_id_for(node)
                        try:
                            reservation = self.capacity.reserve(
                                node_id=node.node_id,
                                kind="write" if node.is_writer else "read",
                                candidate_id=candidate_id if node.is_writer else None,
                            )
                        except CapacityExhausted:
                            # No other node can start either; the reservation made
                            # this decision, not a stale local counter.
                            self._reasons.append("execution capacity is reserved by queued work")
                            break
                        try:
                            self._fencing_tokens[node.node_id] = fencing_token
                            future = executor.submit(
                                self._run_worker, worker, invocation, node, candidate_id, reservation,
                            )
                        except BaseException:
                            self.capacity.release(reservation)
                            raise
                        active[future] = (node, invocation, fencing_token)
                        self._fencing_tokens[node.node_id] = fencing_token
                        reservations[future] = reservation
                        capacity -= 1
                        if self.on_activity:
                            self.on_activity()

                if not active:
                    if self.reducer.terminal() or self.cancellation_event.is_set():
                        break
                    if self._stop_requested is not None:
                        # The limit was reached and every Worker that was running
                        # has now been collected with its real usage, so the run
                        # ends as canceled rather than pretending it completed.
                        self.cancel(self._stop_requested)
                        break
                    if context_failed:
                        # Synchronous failure may have made a waiting primary
                        # READY after the current ready-list was captured.
                        continue
                    idle_wait = self._idle_agent_wait_timeout()
                    if idle_wait is not None:
                        # Recovered/live child leases need not have a local
                        # Future. Keep servicing the parent's native deadline
                        # rather than ending dispatch with WAITING_CHILDREN.
                        self._dispatch_wake.wait(min(idle_wait, self._wait_timeout()))
                        self._dispatch_wake.clear()
                        continue
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
                    reservations.pop(future, None)
                    result = self._future_result(future, node, invocation)
                    self._account_usage(result)
                    events = self._persist_worker_result(result, fencing_token)
                    accepted = any(event.event_type.value == "ResultAccepted" for event in events)
                    if not accepted:
                        self._discard_worker_result(worker, result)
                        if (
                            not self.cancellation_event.is_set()
                            and self.reducer.node_state(node.node_id) is not NodeState.CANCELED
                        ):
                            safe_to_replay = self._is_pure_read_only(node)
                            if self.reducer.expire_attempt(
                                node.node_id,
                                allow_retry=safe_to_replay,
                            ) and not safe_to_replay:
                                self._reasons.append(
                                    "effectful Worker lease expired; automatic replay is forbidden"
                                )
                        continue
                    result = self.reducer.node_result(node.node_id) or result
                    if self._budget_exhausted(for_delivery=True, attempt_id=result.attempt_id):
                        self.cancel(self._reasons[-1])
                        self._discard_worker_result(worker, result)
                        continue
                    if result.status is AgentResultStatus.FAILED_RETRYABLE:
                        self._discard_worker_result(worker, result)
                        # A failed attempt is not a final dependency outcome.
                        # Publishing it early permanently blocks registered waits.
                        retrying = self.reducer.prepare_retry(node.node_id)
                        if not retrying and node.node_id != "primary":
                            self._publish_task_outcome(node, result)
                        continue
                    if result.status is AgentResultStatus.WAITING_CHILDREN:
                        # The checkpoint and child registration were accepted
                        # atomically. Keep its candidate; the executor slot is
                        # already free for the ordinary child dispatch loop.
                        self._wake_satisfied_waits()
                        if self.on_activity:
                            self.on_activity()
                        continue
                    if result.status is not AgentResultStatus.SUCCEEDED:
                        if node.node_id != "primary":
                            self._publish_task_outcome(node, result)
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
                        if node.node_id != "primary":
                            self._publish_task_outcome(node, result)
                        continue
                    self._verify_result(node.node_id, result, node_verifier)
                    if node.node_id != "primary":
                        # A Builder's real bytes become an exact, hashable patch
                        # before anything can depend on them, and the outcome is
                        # published only afterwards.
                        self._export_builder_patch(node, invocation, result)
                        # Every child outcome is persisted to the mailbox before it
                        # can be consumed, so a wait registered later still sees it.
                        self._publish_task_outcome(node, result)
                    if self.reducer.candidate_first and node.is_writer and self.reducer.node_state(node.node_id) is not NodeState.SUCCEEDED:
                        self._discard_worker_result(worker, result)
        except BaseException as error:
            # Stop signals alone do not fence SUBMIT_RESULT. Persist the abort
            # before a surviving Worker can publish a late successful result.
            try:
                self.cancel(f"scheduler dispatch aborted ({type(error).__name__})")
            except Exception:
                logging.getLogger(__name__).exception("Could not persist scheduler cancellation")
            raise
        finally:
            if active:
                # Stop surviving tools before handing them to late collection.
                self.cancellation_event.set()
            for future in active:
                if not future.cancel():
                    node, _invocation, _token = active[future]
                    self.execution_drain.track(future, node_id=node.node_id,
                        collect=partial(self._discard_late_future, worker))
            executor.shutdown(wait=not active, cancel_futures=True)
            if not self.execution_drain.pending_nodes:
                self._retry_worker_cleanup(worker)
            # A future cancelled before it started never enters `_run_worker`, so
            # its reservation is released here. `release` is idempotent, so the
            # normal path (where the Worker already released it) is unaffected and
            # a queued writer that is cancelled cannot leak a slot.
            for future, reservation in reservations.items():
                if future.cancelled() and future in active:
                    _node, invocation, _token = active[future]
                    self.reducer.record_worker_completion(self._canceled_result(invocation))
                if future.done() and reservation not in self._quarantine_reservations.values():
                    self.capacity.release(reservation)
            self._active_futures = None
            self._executor = None
            self._execute_worker = None
            self._execute_context_builder = None
            self._execute_node_verifier = None

        if self._stop_requested is not None and not self.reducer.snapshot().get("canceled"):
            # A terminal last result may have ended the loop before the normal
            # empty-active branch. Keep its receipt while closing the stopped
            # run; no successful whole-run delivery follows a stop request.
            self.cancel(self._stop_requested)
        elif not self.cancellation_event.is_set() and self._budget_exhausted(for_delivery=True):
            self.cancel(self._reasons[-1])
        snapshot = self.reducer.snapshot()
        usage = self.reducer.usage_summary()
        result_nodes = tuple(
            sorted(
                node_id
                for node_id, runtime in snapshot["nodes"].items()
                if runtime.get("result") is not None
            )
        )
        completed = not snapshot.get("canceled") and not snapshot.get("paused") and bool(snapshot["nodes"]) and not self.reducer.completion_blockers()
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
            peak_active_workers=self.capacity.peak_workers,
            peak_active_writers=self.capacity.peak_writers,
            active_leases=active_leases,
            tokens_used=usage["tokens"],
            cost_used=usage["cost"],
            result_node_ids=result_nodes,
            reasons=tuple(self._reasons),
            model_calls=usage["model_calls"],
            tokens_known=usage["tokens_known"],
            cost_known=usage["cost_known"],
            reserved_tokens=usage["reserved_tokens"],
            reserved_cost=usage["reserved_cost"],
            unsettled_task_ids=self.execution_drain.pending_nodes,
        )

    def _node_definition(self, node_id: str) -> PlanNode:
        for node in self.reducer.current_plan.nodes:
            if node.node_id == node_id:
                return node
        # Historical canceled scopes retain native settlement and audit records.
        historical = self.reducer.snapshot().get("nodes", {}).get(node_id)
        if historical is not None:
            return self.reducer._node(node_id).definition
        raise KeyError(node_id)

    def _decline_optional_review_at_budget_limit(self) -> None:
        """Close an unfunded optional opinion without canceling finished work.

        Exact exhaustion stops new requests; delivery still enforces the original
        run ceiling and reconciled usage. No fixed startup reserve or extra credit.
        """
        if self.cancellation_event.is_set() or self._stop_requested or "final-verifier" not in self.reducer.ready_node_ids():
            return
        node = self._node_definition("final-verifier")
        if node.review_policy is None or node.review_policy["required"]:
            return
        usage = self.reducer.usage_summary()
        budget = self.profile.budget
        if (not usage["tokens_known"] or (budget.max_cost is not None and not usage["cost_known"])
            or usage["reserved_tokens"] or usage["reserved_cost"]
            or budget.deadline is not None and self.clock.now() >= budget.deadline):
            return
        limited = (budget.max_tokens is not None and usage["tokens"] >= budget.max_tokens
                   or budget.max_cost is not None and usage["known_cost"] >= budget.max_cost)
        if not limited:
            return
        claimed = self._claim(node)
        if claimed is None:
            return
        attempt_id, token = claimed
        parent = self.reducer.node_result("primary")
        result = AgentResult(self.reducer.run_id, self.reducer.current_plan.plan_version,
            node.node_id, attempt_id, AgentResultStatus.FAILED_TERMINAL,
            parent.base_revision if parent is not None else "unavailable",
            "Optional independent review not started: the run has no remaining model budget",
            contract_revision=self.reducer.current_plan.contract_revision, usage={"tokens": 0, "cost": 0.0},
            failure=ExecutionFailure(ExecutionFailureKind.REVIEW_UNAVAILABLE,
                "Optional independent review not started: the run has no remaining model budget"))
        self.reducer.record_worker_completion(result)
        self._account_usage(result)
        self._persist_worker_result(result, token)
        self._reasons.append(result.output_summary)

    def _optional_review_closeout_can_settle(self) -> bool:
        """A returned primary may settle at the ceiling; no new work is funded.

        The executor persists usage/result before releasing its Future. Stopping
        that cleanup frame at exact exhaustion used to reject a completed primary
        before its optional, unfunded reviewer could be declined.
        """
        policy = self.reducer.effective_review_policy
        if policy is None or policy["required"] or policy["scope"] == "basic" and not self.reducer.dynamic_review_enabled:
            return False
        primary = self.reducer.node_result("primary")
        attempt = self.reducer.current_attempt("primary")
        with self._running_lock:
            returned = attempt is not None and attempt.attempt_id in self._returned_primary_attempts
        accepted = (primary is not None and attempt is not None and primary.attempt_id == attempt.attempt_id
                    and primary.status is AgentResultStatus.SUCCEEDED)
        if not returned and not accepted:
            return False
        usage, budget = self.reducer.usage_summary(), self.profile.budget
        return (usage["tokens_known"] and (budget.max_cost is None or usage["cost_known"])
            and not usage["reserved_tokens"] and not usage["reserved_cost"]
            and (budget.deadline is None or self.clock.now() < budget.deadline)
            and (budget.max_tokens is None or usage["tokens"] <= budget.max_tokens)
            and (budget.max_cost is None or usage["known_cost"] <= budget.max_cost))

    def _can_claim(self, node: PlanNode) -> bool:
        if not self.shared_writes.can_start(node):
            return False
        # A suspended primary gives up its execution reservation, not its
        # ability to recover. Keep one worker/writer slot available even when
        # child tools hang or their uncertain effects retain reservations.
        if (node.node_id != "primary" and self.shared_writes.enabled and self.shared_writes.assignments()
                and self.reducer.node_state("primary") not in {
                    NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.CANCELED, NodeState.SUPERSEDED}):
            children = [r for r in self.capacity.snapshot()["reservations"] if r["node_id"] != "primary"]
            if (len(children) >= self.capacity.max_workers - 1
                    or node.is_writer and sum(r["kind"] == "write" for r in children) >= self.capacity.max_writers - 1):
                return False
        # This policy/capacity probe also accepts a proposed final verifier.
        # The actual claim still requires registration in the Reducer.
        registered = any(n.node_id == node.node_id for n in self.reducer.current_plan.nodes)
        if registered and (not self.reducer.builder_may_execute(node.node_id) or any(
            record["node_id"] == node.node_id for record in self.reducer.candidate_quarantines().values())):
            return False
        if (self.profile.primary_led and not self.profile.dynamic_worker_delegation
                and node.node_id != "primary"
                and not node.node_id.startswith("builder-")
                and not (node.node_id == "final-verifier" and node.role == "verifier")):
            self._reasons.append("general helper admission is disabled for this profile")
            return False
        sessions = self.reducer.agent_sessions() if self.reducer.collaboration_config else {}
        claim_key = lambda task: sessions.get(task, {}).get("session_id", task)
        claimed = {claim_key(task) for task in self._claimed_nodes}
        if claim_key(node.node_id) not in claimed and len(claimed) >= self.profile.max_agents:
            self._reasons.append("max_agents budget exhausted")
            return False
        if node.is_writer:
            # One live writer per candidate, keyed by candidate identity rather
            # than by a global "some writer exists" count. A different candidate
            # is free to start immediately.
            candidate_id = self.candidate_id_for(node)
            lease = self.candidates.lease_for(candidate_id)
            if not self._holds_candidate(node, lease):
                self._reasons.append(f"candidate {candidate_id} already has a live writer")
                return False
            if self.capacity.active_writers >= self.capacity.max_writers:
                self._reasons.append("candidate writer capacity is fully reserved")
                return False
        return True

    def _claim(
        self,
        node: PlanNode,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[str, int] | None:
        with self._dispatch_lock:
            return self._claim_unlocked(node, idempotency_key=idempotency_key)

    def _claim_unlocked(
        self,
        node: PlanNode,
        *,
        idempotency_key: str | None = None,
    ) -> tuple[str, int] | None:
        if node.role == "shared_writer" and (not self.shared_writes.enabled or not self.shared_writes.can_start(node)):
            return None
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
        with self._running_lock:
            self._owned_leases[node.node_id] = (
                attempt.attempt_id, attempt.lease.lease_owner, attempt.lease.fencing_token.value,
            )
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
        events = self.reducer.apply(
            command(
                self.reducer.run_id,
                RuntimeCommandType.BEGIN_VERIFICATION,
                {"node_id": node_id},
            )
        )
        if not any(event.event_type.value == "VerificationStarted" for event in events):
            self._fail_workspace_commit(node_id, "node verification was not authorized by the reducer")
            return
        try:
            record = node_verifier(result)
            if not isinstance(record, VerificationRecord) or type(record.passed) is not bool:
                raise TypeError("node verifier must return a VerificationRecord with a boolean verdict")
        except (Exception, SystemExit) as error:  # Main-thread KeyboardInterrupt remains a user stop.
            record = VerificationRecord(
                verifier="scheduler-node-verifier",
                passed=False,
                scope=f"node:{node_id}",
                details=f"verifier crashed ({type(error).__name__}): {error}",
            )
        if record.passed:
            if node_id == "primary" and self.reducer.dynamic_review_enabled:
                try:
                    observe = self.review_snapshot
                    if observe is None:
                        raise ReducerError("native candidate snapshot observer is unavailable")
                    self.reducer.assess_delivery_review(result, observe(result))
                except (Exception, SystemExit) as error:
                    self.reducer.apply(command(self.reducer.run_id, RuntimeCommandType.FAIL_VERIFICATION,
                        {"node_id": node_id, "reason": f"observed review assessment failed: {error}"}))
                    return
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
                {"node_id": node_id, "reason": record.details, "record": record.to_dict()},
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
            if node.role == "shared_writer":
                validate = getattr(worker, "validate_shared_result", None)
                if not callable(validate):
                    raise RuntimeError("shared writer requires native file-write receipts")
                self.reducer.commit_workspace(node.node_id, attempt_id=result.attempt_id,
                    fencing_token=fencing_token, commit=lambda: validate(result))
                return None
            if self.reducer.candidate_first:
                # A Builder's product and the primary's delivery candidate are two
                # different things, so they get two different production branches.
                # A Builder proves its own isolated candidate really holds the change
                # it reports; the primary proves its accepted delivery checkpoint.
                # Neither path deletes verification to mark a node successful.
                if node.node_id.startswith("builder-"):
                    validate_builder = getattr(worker, "validate_builder_candidate", None)
                    if not callable(validate_builder):
                        raise RuntimeError(
                            "candidate-first Builder has no durable candidate validator")
                    validate_builder(result)
                    return None
                validate = getattr(worker, "validate_candidate", None)
                if not callable(validate):
                    raise RuntimeError("candidate-first Writer has no durable candidate validator")
                validate(result)
                return None
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
        self._account_usage(result)
        self._discard_worker_result(worker, result)
        self._retry_worker_cleanup(worker)

    def _consultation_result(self, node_id: str) -> dict | None:
        result = self.reducer.node_result(node_id)
        if result is None:
            return None
        payload = result.to_dict()
        state = self.reducer.node_state(node_id)
        if state is not NodeState.SUCCEEDED:
            payload["status"] = AgentResultStatus(state.value.lower()).value
            payload["runtime_diagnostics"] = list(self.reducer.retry_feedback(node_id))
            payload["diagnosis"] = self.reducer.node_diagnosis(node_id)
        return payload

    def _recover_verifying(self, worker: Worker, node_verifier: NodeVerifier) -> None:
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
                if definition.get("role") == "shared_writer":
                    attempt = self.reducer.current_attempt(str(node_id))
                    error = self._commit_worker_result(worker, self._node_definition(str(node_id)), result,
                        attempt.lease.fencing_token.value)
                    if error:
                        self._fail_workspace_commit(str(node_id), error)
                    else:
                        self._verify_result(str(node_id), result, node_verifier)
                    continue
                if self.reducer.candidate_first:
                    validate = getattr(worker, "validate_builder_candidate" if str(node_id).startswith("builder-")
                                       else "validate_candidate", None)
                    try:
                        if not callable(validate):
                            raise RuntimeError("candidate recovery requires a durable candidate validator")
                        validate(result)
                    except Exception as error:
                        self._fail_workspace_commit(str(node_id), f"candidate recovery failed: {error}")
                        continue
                    self._verify_result(str(node_id), result, node_verifier)
                    continue
                self._fail_workspace_commit(
                    str(node_id),
                    "workspace commit was not durably confirmed before restart",
                )
                continue
            self._verify_result(str(node_id), result, node_verifier)

    def _recover_child_publications(self, worker: Worker) -> None:
        """Replay durable results into the patch ledger and mailbox, without execution."""
        for node in self.reducer.current_plan.nodes:
            if node.role == "primary":
                continue
            result = self.reducer.node_result(node.node_id)
            if result is None or self.reducer.node_state(node.node_id) not in {
                NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.CANCELED,
            }:
                continue
            if (self.reducer.candidate_first and node.node_id.startswith("builder-") and self.reducer.node_state(node.node_id) is NodeState.SUCCEEDED
                    and f"patch-{node.node_id}" not in self.reducer.patch_records()):
                validate = getattr(worker, "validate_builder_candidate", None)
                if not callable(validate):
                    raise RuntimeError("Builder publication recovery requires its durable candidate")
                validate(result)
                self._export_builder_patch(node, None, result)
            self._publish_task_outcome(node, result)

    def _persist_worker_result(self, result: AgentResult, fencing_token: int):
        # Commit before Future notification. The collector uses this SAME command,
        # so a result is accepted once even after a crash at the notification edge.
        return self.reducer.apply(command(self.reducer.run_id, RuntimeCommandType.SUBMIT_RESULT,
            {"result": result, "fencing_token": fencing_token},
            command_id=f"worker-result:{result.run_id}:{result.node_id}:{result.attempt_id}"))

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
                node = self._node_definition(str(node_id))
                safe_to_replay = self._is_pure_read_only(node)
                if self.reducer.expire_attempt(
                    str(node_id),
                    allow_retry=safe_to_replay,
                ) and not safe_to_replay:
                    self._reasons.append(
                        "recovered effectful Worker lease expired with uncertain side effects; "
                        "automatic replay is forbidden"
                    )

    @staticmethod
    def _is_pure_read_only(node: PlanNode) -> bool:
        return bool(node.allowed_effects) and all(
            effect is ToolEffect.READ_ONLY for effect in node.allowed_effects
        )

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

    def _idle_agent_wait_timeout(self) -> float | None:
        if not self.profile.async_agents or self.reducer.node_state("primary") is not NodeState.WAITING_CHILDREN:
            return None
        continuation = self.reducer.continuation_for("primary")
        if continuation is None or continuation.kind != "dependency" or not continuation.dependency_wait:
            return None
        wait_id = continuation.dependency_wait["wait_id"]
        if self.reducer.wait_resumes(wait_id):
            return None
        wait = self.reducer.dependency_wait(wait_id)
        agents = self.reducer.agent_sessions()
        if (wait["owner_node_id"] != "primary" or wait.get("timeout_policy") != "return"
                or not all(edge["task_id"] in agents for edge in wait["edges"])):
            return None
        from minicode.dependency_waits import DEFAULT_AGENT_WAIT_SECONDS
        return max(0.0, wait["created_at"] + (wait.get("timeout_seconds") or DEFAULT_AGENT_WAIT_SECONDS) - self.clock.now())

    def _run_worker(
        self,
        worker: Worker,
        invocation: AgentInvocationSpec,
        node: PlanNode,
        candidate_id: str,
        reservation: CapacityReservation,
    ) -> AgentResult:
        """Run one Worker under a per-candidate lease and a reserved slot.

        No global lock is taken here. Two Builders on different candidates run at
        the same time; the primary keeps running while its builders do. The slot
        was already reserved before this call was submitted, so a queued node is
        already counted against the ceiling.
        """
        lease: CandidateLease | None = None
        uncertain = False
        # The bounded stop's reason is published BEFORE anything else can fail, so
        # every cancellation report from this activation carries its real cause and
        # an exhausted budget never reads as a user cancellation.
        note_reason = getattr(worker, "note_stop_reason", None)
        if callable(note_reason):
            note_reason(self._stop_reason_text())
        try:
            builder_candidate = self._open_builder_candidate(node, invocation) if node.is_writer else None
        except Exception as error:
            result = self._failed_result(invocation, node,
                f"Builder candidate preparation failed ({type(error).__name__}): {error}")
            result = replace(result, metrics={"candidate_preparation": {
                "worker_started": False, "error_type": type(error).__name__,
                "message": str(error), "patch_created": False}})
            self.reducer.record_worker_completion(result)
            self.capacity.release(reservation)
            return result
        if builder_candidate is not None and builder_candidate.handle is not None:
            snapshot = dict(builder_candidate.handle.base_hashes)
            from minicode.workspace_provider import snapshot_revision
            invocation = replace(invocation, context_manifest=replace(invocation.context_manifest,
                base_revision=snapshot_revision(snapshot), included_files=snapshot))
        if node.is_writer and self._builder_lifecycle_for(node.node_id) is not None \
                and builder_candidate is None:
            result = self._failed_result(invocation, node, "the Builder candidate could not be created")
            self.reducer.record_worker_completion(result)
            self.capacity.release(reservation)
            return result
        try:
            if node.is_writer:
                generation = self._candidate_generations.get(candidate_id, 0) + 1
                try:
                    lease = self.candidates.claim(
                        candidate_id=candidate_id,
                        owner_node_id=node.node_id,
                        owner_attempt_id=invocation.attempt_id,
                        owner_kind="primary" if node.role == "primary" else "builder",
                        base_revision=invocation.context_manifest.base_revision,
                        generation=generation,
                        write_scope=node.write_scope,
                    )
                except CandidateConflict as error:
                    result = self._candidate_conflict_result(invocation, node, error)
                    self.reducer.record_worker_completion(result)
                    return result
                self._candidate_generations[candidate_id] = generation
                self.reducer.record_candidate_lease(lease)
            if self.cancellation_event.is_set() or self._stop_requested is not None:
                # A Worker that has not started yet is never started once the run
                # is canceled OR a budget/deadline has stopped new admission. It
                # has no lease, has touched nothing, and reports a real cancel.
                result = self._canceled_result(invocation)
                self.reducer.record_worker_completion(result)
                return result
            if builder_candidate is not None and builder_candidate.handle is not None:
                # The Runtime opens the candidate and hands THESE bytes to this one
                # activation; the Worker must not open a second, inner candidate.
                handover = getattr(worker, "hand_over_candidate_workspace", None)
                binder = (handover if callable(handover)
                          else getattr(worker, "bind_candidate_workspace", None))
                if callable(binder):
                    binder(node.node_id, builder_candidate.handle)
                if not callable(handover):
                    self._reasons.append(
                        "Worker cannot accept a handed-over Builder candidate; "
                        "the Builder may write in a candidate the Runtime does not own")
            # Bind the SAME candidate registry and the SAME live lease the Runtime
            # uses to this activation's thread, so the real file tools and the
            # Runtime's apply cannot both mutate one candidate.
            isolated_root = (builder_candidate.handle.workspace
                            if builder_candidate is not None
                            and builder_candidate.handle is not None
                            else self._isolated_candidate_root(worker, node))
            write_access = (
                CandidateWriteAccess(candidate_id=candidate_id,
                                     owner_token=lease.owner_token,
                                     root=isolated_root if isolated_root is not None else
                                     lambda: self._isolated_candidate_root(worker, node),
                                     canceled=lambda: self.cancellation_event.is_set() or self.reducer.node_cancel_epoch(node.node_id) > 0)
                if lease is not None and (isolated_root is not None or getattr(worker, "isolate_writes", False)) else None)
            try:
                with bind_candidate_write_access(self.candidates, write_access), self.shared_writes.activation(invocation):
                    try:
                        if (not node.is_writer and invocation.context_manifest.retry_feedback
                            and node.node_id.startswith(("consult-", "agent-", "delegated-"))):
                            # Occupy only this reserved child slot; the parent and
                            # other children continue. Cancellation interrupts it.
                            self.cancellation_event.wait(1.0)
                        result = (self._canceled_result(invocation)
                                  if self.cancellation_event.is_set() or self.reducer.node_cancel_epoch(node.node_id)
                                  else worker.run(invocation))
                        result = self._validated_worker_result(result, invocation)
                    except BaseException as error:
                        # This is an executor-thread boundary, not the main
                        # thread's Ctrl+C handler. Never let a child extension
                        # terminate the scheduler via a BaseException.
                        result = self._crashed_result(error, node, invocation)
                    uncertain = bool(result.failure and result.failure.ambiguous_side_effect)
                    uncertain = uncertain or (result.metrics.get("candidate_recovery", {}).get("reason") == "uncertain_side_effect_or_inflight_tool")
                    if node.node_id == "primary" and result.status is AgentResultStatus.SUCCEEDED and not uncertain:
                        # An in-memory execution fact, not task acceptance. It
                        # closes the short frame between accounting returned
                        # usage and persisting the returned result below.
                        with self._running_lock:
                            self._returned_primary_attempts.add(invocation.attempt_id)
                    if uncertain:
                        self.candidates.quarantine(candidate_id)
                        self._quarantine_reservations[result.attempt_id] = reservation
                        self._reasons.append(f"{node.node_id}: uncertain effect retains candidate ownership and capacity until native reconciliation")
                    # Read-only asynchronous sessions also need durable exit
                    # evidence for continuation, retirement and team closure.
                    if self.profile.parallel_candidates or self.profile.async_agents or uncertain or result.metrics.get("candidate_recovery"):
                        self.reducer.record_worker_completion(result)
                    self._account_usage(result)
                    self._persist_worker_result(result, self._fencing_tokens[node.node_id])
                    return result
            finally:
                if builder_candidate is not None:
                    unbind = getattr(worker, "unbind_candidate_workspace", None)
                    if callable(unbind):
                        unbind(node.node_id)
        finally:
            if lease is not None and not uncertain:
                self.candidates.release(candidate_id=candidate_id, owner_token=lease.owner_token)
                self.reducer.record_candidate_lease(lease, released=True)
            if not uncertain:
                self.capacity.release(reservation)

    def _failed_result(self, invocation: AgentInvocationSpec, node: PlanNode,
                       message: str) -> AgentResult:
        return AgentResult(
            run_id=invocation.run_id, plan_version=invocation.plan_version,
            node_id=node.node_id, attempt_id=invocation.attempt_id,
            status=AgentResultStatus.FAILED_TERMINAL,
            base_revision=invocation.context_manifest.base_revision,
            output_summary=message,
            contract_revision=invocation.context_manifest.contract_revision,
            usage={"tokens": 0, "cost": 0.0},
            failure=ExecutionFailure(ExecutionFailureKind.WORKSPACE_BUSY, message),
        )

    def _candidate_conflict_result(
        self, invocation: AgentInvocationSpec, node: PlanNode, error: BaseException,
    ) -> AgentResult:
        """A refused writer never runs and never gains write rights.

        The node is retryable because nothing was executed and no candidate bytes
        were touched; the ownership check is re-evaluated on the next claim.
        """
        return AgentResult(
            run_id=invocation.run_id, plan_version=invocation.plan_version,
            node_id=node.node_id, attempt_id=invocation.attempt_id,
            status=AgentResultStatus.FAILED_RETRYABLE,
            base_revision=invocation.context_manifest.base_revision,
            output_summary=f"candidate ownership conflict: {error}",
            contract_revision=invocation.context_manifest.contract_revision,
            usage={"tokens": 0, "cost": 0.0},
            failure=ExecutionFailure(
                ExecutionFailureKind.WORKSPACE_BUSY,
                f"candidate {self.candidate_id_for(node)} already has a live writer",
            ),
        )

    def _future_result(
        self,
        future: Future[AgentResult],
        node: PlanNode,
        invocation: AgentInvocationSpec,
    ) -> AgentResult:
        try:
            return future.result()
        except BaseException as error:
            if future.cancelled() and isinstance(error, CancelledError):
                result = self._canceled_result(invocation)
                self.reducer.record_worker_completion(result)
                return result
            # Only consume exceptions actually stored by the worker Future.
            # A Ctrl+C arriving while the main thread collects a result is not
            # a child failure and must retain its normal interrupt semantics.
            if not future.done() or future.cancelled() or future.exception() is not error:
                raise
            result = self._crashed_result(error, node, invocation)
            if invocation.attempt_id not in self.reducer.snapshot().get("worker_completions", {}):
                self.reducer.record_worker_completion(result)
            return result

    @staticmethod
    def _validated_worker_result(result: AgentResult, invocation: AgentInvocationSpec) -> AgentResult:
        if not isinstance(result, AgentResult):
            raise TypeError("worker must return an AgentResult")
        if any(getattr(result, field) != getattr(invocation, field)
               for field in ("run_id", "plan_version", "node_id", "attempt_id")):
            raise ValueError("worker result does not belong to this activation")
        if (not isinstance(result.status, AgentResultStatus)
                or not isinstance(result.metrics, Mapping) or not isinstance(result.usage, Mapping)):
            raise ValueError("worker returned an invalid result envelope")
        return result

    def _crashed_result(self, error: BaseException, node: PlanNode, invocation: AgentInvocationSpec) -> AgentResult:
        pure_read_only = self._is_pure_read_only(node)
        return AgentResult(
            run_id=invocation.run_id, plan_version=invocation.plan_version,
            node_id=node.node_id, attempt_id=invocation.attempt_id,
            status=AgentResultStatus.FAILED_RETRYABLE if pure_read_only else AgentResultStatus.FAILED_TERMINAL,
            base_revision=invocation.context_manifest.base_revision,
            output_summary=f"worker crashed ({type(error).__name__}): {error}",
            contract_revision=invocation.context_manifest.contract_revision,
            failure=ExecutionFailure(
                ExecutionFailureKind.TOOL_FAILED if pure_read_only else ExecutionFailureKind.UNCERTAIN_SIDE_EFFECT,
                f"worker crashed ({type(error).__name__}): {error}",
                retryable=pure_read_only, ambiguous_side_effect=not pure_read_only,
            ),
        )

    def _canceled_result(self, invocation: AgentInvocationSpec) -> AgentResult:
        """A Worker that never ran because the run stopped.

        The stop reason is preserved. A Worker refused because the bounded budget
        was already reached is a budget outcome, and reporting it as a plain user
        cancellation would hide the cause the caller has to act on.
        """

        reason = self._stop_requested or (
            "task canceled before worker execution" if self.reducer.node_cancel_epoch(invocation.node_id)
            and not self.cancellation_event.is_set() else "parent run canceled")
        return AgentResult(
            run_id=invocation.run_id,
            plan_version=invocation.plan_version,
            node_id=invocation.node_id,
            attempt_id=invocation.attempt_id,
            status=AgentResultStatus.CANCELED,
            base_revision=invocation.context_manifest.base_revision,
            output_summary=f"worker canceled before start ({reason})",
            contract_revision=invocation.context_manifest.contract_revision,
            usage={"tokens": 0, "cost": 0.0},
            failure=ExecutionFailure(self._budget_failure_kind(reason), reason),
        )

    def _account_usage(self, result: AgentResult) -> None:
        self.reducer.record_result_usage(result)
        usage = self.reducer.usage_summary()
        with self._running_lock:
            self._tokens_used = usage["tokens"]
            self._cost_used = usage["known_cost"]

    def _budget_exhausted(self, *, for_delivery: bool = False, attempt_id: str | None = None) -> bool:
        """Whether a budget or deadline limit has been reached.

        ``for_delivery`` asks the stricter question -- "must the final
        deliverable be refused?" -- which also fails closed on unsettled usage.
        The plain form answers "may new work be admitted?", and is what a live
        Worker's own deadline check is compared against.
        """
        budget = self.profile.budget
        usage = self.reducer.usage_summary()
        tokens_used, cost_used = usage["tokens"], usage["known_cost"]
        delivery_usage = self.reducer.usage_summary(attempt_id) if attempt_id is not None else usage
        if for_delivery and (
            (budget.max_tokens is not None and not delivery_usage["tokens_known"])
            or (budget.max_cost is not None and not delivery_usage["cost_known"])
        ):
            self._reasons.append("model usage remains unsettled under a bounded budget")
            return True
        if self._deadline_reached():
            return True
        if budget.max_tokens is not None and (
            tokens_used > budget.max_tokens if for_delivery else tokens_used >= budget.max_tokens
        ):
            self._reasons.append("max_tokens budget exhausted")
            return True
        if budget.max_cost is not None and (
            cost_used > budget.max_cost if for_delivery else cost_used >= budget.max_cost
        ):
            self._reasons.append("max_cost budget exhausted")
            return True
        return False

    def _budget_failure_kind(self, reason: str) -> ExecutionFailureKind:
        """Which kind a budget stop really is; one definition, shared with Workers."""

        return budget_stop_failure_kind(reason)

    def _stop_reason_text(self) -> str:
        """The recorded reason new work stopped, published for the Workers to report.

        Workers see the cancellation event but not why it was raised; without this
        a budget stop and a user cancellation are indistinguishable at the point
        the failure kind is chosen.
        """

        return self._stop_requested or ""

    def _deadline_reached(self) -> bool:
        """Whether the configured deadline has passed, recording it once."""
        budget = self.profile.budget
        if budget.deadline is None or self.clock.now() < budget.deadline:
            return False
        if "deadline exhausted" not in self._reasons:
            self._reasons.append("deadline exhausted")
        return True

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
