"""Deterministic, single-writer state reducer for the unified Runtime."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_protocol import (
    AgentResult,
    AgentResultStatus,
    ArtifactRef,
    EvidenceRef,
    ExecutionFailure,
    ExecutionFailureKind,
    FencingToken,
    Lease,
    NodeAttempt,
    NodeState,
    PlanNode,
    PlanVersion,
    RuntimeCommand,
    RuntimeCommandType,
    RuntimeEvent,
    RuntimeEventType,
    TaskAmendment,
    ToolEffect,
    VerificationRecord,
    new_id,
)


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class RuntimeStateIntegrityError(RuntimeError):
    """The configured state path aliases data outside its logical store."""


@dataclass(slots=True)
class NodeRuntime:
    definition: PlanNode
    state: NodeState
    attempts: list[NodeAttempt] = field(default_factory=list)
    current_attempt_id: str | None = None
    result: AgentResult | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    resume_credits: int = 0
    workspace_committed: bool = True


@dataclass(slots=True)
class RuntimeState:
    run_id: str
    contract_revision: int
    current_plan_version: int
    plans: dict[int, PlanVersion]
    nodes: dict[str, NodeRuntime]
    events: list[RuntimeEvent] = field(default_factory=list)
    canceled: bool = False
    paused: bool = False
    processed_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    consumed_approvals: set[str] = field(default_factory=set)
    next_fencing_token: int = 1
    tokens_used: int = 0
    cost_used: float = 0.0


class ReducerError(RuntimeError):
    """A command was structurally invalid for the current Runtime state."""


class RuntimeStateStore:
    """Atomic JSON snapshot store.

    It is intentionally small and local.  Vector databases may index evidence,
    but never become authoritative for this state.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_safe(self.path, reject_hardlinks=True)
        temporary = self.path.with_name(f".{self.path.name}.{new_id('tmp')}.tmp")
        self._ensure_safe(temporary, reject_hardlinks=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load(self) -> dict[str, Any] | None:
        if (
            is_unsafe_stored_path(
                self.path,
                boundary=self.path.parent,
                reject_hardlinks=True,
            )
            or not self.path.is_file()
        ):
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _ensure_safe(self, path: Path, *, reject_hardlinks: bool = False) -> None:
        if is_unsafe_stored_path(
            path,
            boundary=self.path.parent,
            reject_hardlinks=reject_hardlinks,
        ):
            raise RuntimeStateIntegrityError(
                "Runtime state path escapes or aliases its configured store"
            )


def _initial_node_state(node: PlanNode) -> NodeState:
    return NodeState.PENDING if node.dependencies else NodeState.READY


class RuntimeReducer:
    """The only object allowed to mutate global Runtime state.

    Scheduler, workers, policy, and verifiers submit typed commands.  The
    reducer validates version/attempt/lease/fencing constraints, appends an
    event, then optionally persists the whole state under the same lock.
    """

    def __init__(
        self,
        plan: PlanVersion,
        *,
        clock: Clock | None = None,
        store: RuntimeStateStore | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._store = store
        self._lock = threading.RLock()
        self._state = RuntimeState(
            run_id=plan.run_id,
            contract_revision=plan.contract_revision,
            current_plan_version=plan.plan_version,
            plans={plan.plan_version: plan},
            nodes={
                node.node_id: NodeRuntime(node, _initial_node_state(node))
                for node in plan.nodes
            },
        )
        self._persist()

    @classmethod
    def restore(
        cls,
        payload: Mapping[str, Any],
        *,
        clock: Clock | None = None,
        store: RuntimeStateStore | None = None,
    ) -> RuntimeReducer:
        """Rehydrate a persisted snapshot without replaying side effects."""
        plans = {
            int(key): _plan_from_dict(value)
            for key, value in dict(payload.get("plans", {})).items()
            if isinstance(value, Mapping)
        }
        current_version = int(payload.get("current_plan_version", 0))
        if current_version not in plans:
            raise ReducerError("snapshot has no current PlanVersion")
        reducer = cls(plans[current_version], clock=clock, store=None)
        nodes: dict[str, NodeRuntime] = {}
        raw_nodes = payload.get("nodes", {})
        if not isinstance(raw_nodes, Mapping):
            raise ReducerError("snapshot nodes must be a mapping")
        for node_id, raw in raw_nodes.items():
            if not isinstance(raw, Mapping):
                continue
            definition = _node_from_dict(raw.get("definition", {}))
            attempts = [
                _attempt_from_dict(item)
                for item in raw.get("attempts", [])
                if isinstance(item, Mapping)
            ]
            raw_result = raw.get("result")
            state = NodeState(str(raw.get("state", NodeState.PENDING.value)))
            nodes[str(node_id)] = NodeRuntime(
                definition=definition,
                state=state,
                attempts=attempts,
                current_attempt_id=(
                    str(raw.get("current_attempt_id"))
                    if raw.get("current_attempt_id") is not None
                    else None
                ),
                result=_result_from_dict(raw_result) if isinstance(raw_result, Mapping) else None,
                rejection_reasons=[str(item) for item in raw.get("rejection_reasons", [])],
                resume_credits=int(raw.get("resume_credits", 0)),
                workspace_committed=bool(
                    raw.get(
                        "workspace_committed",
                        not (state is NodeState.VERIFYING and definition.is_writer),
                    )
                ),
            )
        events = [
            _event_from_dict(item)
            for item in payload.get("events", [])
            if isinstance(item, Mapping)
        ]
        reducer._state = RuntimeState(
            run_id=str(payload.get("run_id", plans[current_version].run_id)),
            contract_revision=int(payload.get("contract_revision", plans[current_version].contract_revision)),
            current_plan_version=current_version,
            plans=plans,
            nodes=nodes,
            events=events,
            canceled=bool(payload.get("canceled", False)),
            paused=bool(payload.get("paused", False)),
            processed_commands={
                str(key): tuple(str(item) for item in value)
                for key, value in dict(payload.get("processed_commands", {})).items()
                if isinstance(value, list)
            },
            consumed_approvals={
                str(item) for item in payload.get("consumed_approvals", [])
            },
            next_fencing_token=int(payload.get("next_fencing_token", 1)),
            tokens_used=max(0, int(payload.get("tokens_used", 0) or 0)),
            cost_used=max(0.0, float(payload.get("cost_used", 0.0) or 0.0)),
        )
        reducer._store = store
        reducer._persist()
        return reducer

    @property
    def run_id(self) -> str:
        return self._state.run_id

    @property
    def current_plan(self) -> PlanVersion:
        return self._state.plans[self._state.current_plan_version]

    def node_state(self, node_id: str) -> NodeState:
        with self._lock:
            return self._node(node_id).state

    def node_result(self, node_id: str) -> AgentResult | None:
        with self._lock:
            return self._node(node_id).result

    def current_attempt(self, node_id: str) -> NodeAttempt | None:
        with self._lock:
            runtime = self._node(node_id)
            if runtime.current_attempt_id is None:
                return None
            return next(
                (item for item in runtime.attempts if item.attempt_id == runtime.current_attempt_id),
                None,
            )

    def ready_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            self._refresh_ready_nodes()
            return tuple(
                node_id
                for node_id, runtime in self._state.nodes.items()
                if runtime.state is NodeState.READY
            )

    def terminal(self) -> bool:
        terminal_states = {
            NodeState.SUCCEEDED,
            NodeState.FAILED_TERMINAL,
            NodeState.CANCELED,
            NodeState.SUPERSEDED,
        }
        with self._lock:
            return all(node.state in terminal_states for node in self._state.nodes.values())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def apply(self, command: RuntimeCommand) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            if command.run_id != self._state.run_id:
                raise ReducerError("command run_id does not match reducer run")
            prior_ids = self._state.processed_commands.get(command.command_id)
            if prior_ids is not None:
                by_id = {event.event_id: event for event in self._state.events}
                return tuple(by_id[event_id] for event_id in prior_ids if event_id in by_id)

            handlers: dict[RuntimeCommandType, Callable[[RuntimeCommand], list[RuntimeEvent]]] = {
                RuntimeCommandType.CLAIM_NODE: self._claim_node,
                RuntimeCommandType.START_ATTEMPT: self._start_attempt,
                RuntimeCommandType.SUBMIT_RESULT: self._submit_result,
                RuntimeCommandType.BEGIN_VERIFICATION: self._begin_verification,
                RuntimeCommandType.ACCEPT_VERIFICATION: self._accept_verification,
                RuntimeCommandType.FAIL_VERIFICATION: self._fail_verification,
                RuntimeCommandType.CANCEL_RUN: self._cancel_run,
                RuntimeCommandType.RESUME_RUN: self._resume_run,
                RuntimeCommandType.AMEND_TASK: self._amend_task,
                RuntimeCommandType.APPROVE_TOOL_CALL: self._approval,
                RuntimeCommandType.REJECT_TOOL_CALL: self._approval,
            }
            handler = handlers.get(command.command_type)
            if handler is None:
                raise ReducerError(f"unsupported command: {command.command_type.value}")
            events = handler(command)
            self._state.processed_commands[command.command_id] = tuple(
                event.event_id for event in events
            )
            self._refresh_ready_nodes()
            self._persist()
            return tuple(events)

    def install_plan(self, plan: PlanVersion, amendment: TaskAmendment) -> RuntimeEvent:
        """Install a replan atomically through the reducer."""
        with self._lock:
            if plan.run_id != self.run_id or amendment.run_id != self.run_id:
                raise ReducerError("replan run_id mismatch")
            if plan.plan_version <= self._state.current_plan_version:
                raise ReducerError("new plan version must increase")
            if amendment.base_contract_revision != self._state.contract_revision:
                raise ReducerError("amendment base revision is stale")
            if plan.contract_revision != amendment.next_contract_revision:
                raise ReducerError("plan and amendment revisions differ")

            affected = set(amendment.affected_node_ids)
            for node_id in affected:
                runtime = self._state.nodes.get(node_id)
                if runtime is not None:
                    runtime.state = NodeState.SUPERSEDED
            self._state.plans[plan.plan_version] = plan
            self._state.current_plan_version = plan.plan_version
            self._state.contract_revision = plan.contract_revision

            next_nodes: dict[str, NodeRuntime] = {}
            for node in plan.nodes:
                prior = self._state.nodes.get(node.node_id)
                if (
                    prior is not None
                    and node.node_id not in affected
                    and prior.state is NodeState.SUCCEEDED
                    and prior.definition == node
                ):
                    next_nodes[node.node_id] = prior
                else:
                    next_nodes[node.node_id] = NodeRuntime(node, _initial_node_state(node))
            # ``nodes`` is always the current PlanVersion projection.  Prior
            # versions remain durable in ``plans`` and the event log, but may
            # not keep a hidden runnable node in the active state machine.
            self._state.nodes = next_nodes
            event = self._emit(
                RuntimeEventType.TASK_AMENDED,
                {
                    "amendment": amendment.to_dict(),
                    "plan_version": plan.plan_version,
                    "superseded_nodes": sorted(affected),
                },
            )
            self._refresh_ready_nodes()
            self._persist()
            return event

    def register_delegated_node(self, node: PlanNode, *, parent_node_id: str) -> RuntimeEvent:
        """Append a compatibility delegation to the one canonical PlanGraph.

        This is not a replan: the contract and dependencies are unchanged and
        the parent is synchronously suspended.  The append is nevertheless
        serialized and audited by the Reducer, never held in a shadow graph.
        """
        with self._lock:
            if node.node_id in self._state.nodes:
                existing = self._state.nodes[node.node_id]
                if existing.definition != node:
                    raise ReducerError("delegation id collides with a different node")
                return self._emit(
                    RuntimeEventType.NODE_READY,
                    {"node_id": node.node_id, "parent_node_id": parent_node_id, "duplicate": True},
                )
            parent = self._node(parent_node_id)
            if parent.state not in {NodeState.RUNNING, NodeState.WAITING_INPUT, NodeState.WAITING_APPROVAL}:
                raise ReducerError("delegation parent is not active")
            plan = self.current_plan
            self._state.plans[plan.plan_version] = replace(plan, nodes=(*plan.nodes, node))
            self._state.nodes[node.node_id] = NodeRuntime(node, NodeState.READY)
            event = self._emit(
                RuntimeEventType.NODE_READY,
                {"node_id": node.node_id, "parent_node_id": parent_node_id, "delegated": True},
            )
            self._persist()
            return event

    def _node(self, node_id: str) -> NodeRuntime:
        try:
            return self._state.nodes[node_id]
        except KeyError as error:
            raise ReducerError(f"unknown node: {node_id}") from error

    def _emit(
        self,
        event_type: RuntimeEventType,
        payload: Mapping[str, Any],
        *,
        command_id: str | None = None,
    ) -> RuntimeEvent:
        event = RuntimeEvent(
            event_id=new_id("event"),
            run_id=self.run_id,
            sequence=len(self._state.events) + 1,
            event_type=event_type,
            payload=dict(payload),
            command_id=command_id,
            created_at=self._clock.now(),
        )
        self._state.events.append(event)
        return event

    def _reject(
        self,
        command: RuntimeCommand,
        node_id: str,
        reason: str,
    ) -> list[RuntimeEvent]:
        runtime = self._state.nodes.get(node_id)
        if runtime is not None:
            runtime.rejection_reasons.append(reason)
        return [
            self._emit(
                RuntimeEventType.RESULT_REJECTED,
                {"node_id": node_id, "reason": reason},
                command_id=command.command_id,
            )
        ]

    def _claim_node(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if self._state.canceled:
            return self._reject(command, node_id, "run is canceled")
        if runtime.state is not NodeState.READY:
            return self._reject(command, node_id, f"node is not READY: {runtime.state.value}")
        if int(command.payload.get("plan_version", 0)) != self._state.current_plan_version:
            return self._reject(command, node_id, "claim references a non-current plan")

        attempt_number = len(runtime.attempts) + 1
        budgeted_attempt_number = attempt_number - runtime.resume_credits
        if budgeted_attempt_number > runtime.definition.max_attempts:
            runtime.state = NodeState.FAILED_TERMINAL
            return self._reject(command, node_id, "attempt budget exhausted")
        lease_seconds = max(0.001, float(command.payload.get("lease_seconds", 30.0)))
        owner = str(command.payload.get("lease_owner") or "local-worker")
        token = self._state.next_fencing_token
        self._state.next_fencing_token += 1
        attempt = NodeAttempt(
            attempt_id=new_id("attempt"),
            run_id=self.run_id,
            plan_version=self._state.current_plan_version,
            node_id=node_id,
            attempt_number=attempt_number,
            lease=Lease(owner, self._clock.now() + lease_seconds, FencingToken(token)),
            state=NodeState.CLAIMED,
            idempotency_key=str(command.payload.get("idempotency_key") or new_id("idempotency")),
        )
        runtime.attempts.append(attempt)
        runtime.current_attempt_id = attempt.attempt_id
        runtime.state = NodeState.CLAIMED
        return [
            self._emit(
                RuntimeEventType.NODE_CLAIMED,
                {"node_id": node_id, "attempt": attempt.to_dict()},
                command_id=command.command_id,
            )
        ]

    def _start_attempt(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        attempt = self.current_attempt(node_id)
        if attempt is None or attempt.attempt_id != command.payload.get("attempt_id"):
            return self._reject(command, node_id, "attempt is not current")
        if runtime.state is not NodeState.CLAIMED:
            return self._reject(command, node_id, "node is not CLAIMED")
        if not attempt.lease.is_valid(self._clock.now()):
            return self._reject(command, node_id, "lease expired before worker start")
        started = replace(attempt, state=NodeState.RUNNING, started_at=self._clock.now())
        runtime.attempts[-1] = started
        runtime.state = NodeState.RUNNING
        return [
            self._emit(
                RuntimeEventType.ATTEMPT_STARTED,
                {"node_id": node_id, "attempt_id": attempt.attempt_id},
                command_id=command.command_id,
            )
        ]

    def _submit_result(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        result = command.payload.get("result")
        if not isinstance(result, AgentResult):
            raise ReducerError("SubmitResult payload must contain AgentResult")
        node_id = result.node_id
        if result.run_id != self.run_id:
            return self._reject(command, node_id, "result run_id does not match Runtime")
        if result.plan_version != self._state.current_plan_version:
            return self._reject(command, node_id, "result references a non-current plan")
        if result.contract_revision != self._state.contract_revision:
            return self._reject(command, node_id, "result references a stale TaskContract revision")
        runtime = self._node(node_id)
        attempt = self.current_attempt(node_id)
        if runtime.state is NodeState.SUPERSEDED:
            return self._reject(command, node_id, "node is superseded")
        if attempt is None or result.attempt_id != attempt.attempt_id:
            return self._reject(command, node_id, "result is not for the current attempt")
        if runtime.result is not None:
            return self._reject(command, node_id, "attempt already has an accepted result")
        submitted_token = int(command.payload.get("fencing_token", -1))
        if submitted_token != attempt.lease.fencing_token.value:
            return self._reject(command, node_id, "stale fencing token")
        if not attempt.lease.is_valid(self._clock.now()):
            return self._reject(command, node_id, "expired lease")
        if runtime.state is not NodeState.RUNNING:
            return self._reject(command, node_id, f"node is not RUNNING: {runtime.state.value}")

        self._state.tokens_used += max(0, int(result.usage.get("tokens", 0) or 0))
        self._state.cost_used += max(0.0, float(result.usage.get("cost", 0.0) or 0.0))
        runtime.result = result
        runtime.workspace_committed = not (
            runtime.definition.is_writer and result.status is AgentResultStatus.SUCCEEDED
        )
        terminal_map = {
            AgentResultStatus.FAILED_RETRYABLE: NodeState.FAILED_RETRYABLE,
            AgentResultStatus.FAILED_TERMINAL: NodeState.FAILED_TERMINAL,
            AgentResultStatus.WAITING_APPROVAL: NodeState.WAITING_APPROVAL,
            AgentResultStatus.WAITING_INPUT: NodeState.WAITING_INPUT,
            AgentResultStatus.CANCELED: NodeState.CANCELED,
        }
        runtime.state = terminal_map.get(result.status, NodeState.VERIFYING)
        released_lease = attempt.lease
        if runtime.state in {
            NodeState.FAILED_RETRYABLE,
            NodeState.FAILED_TERMINAL,
            NodeState.WAITING_APPROVAL,
            NodeState.WAITING_INPUT,
            NodeState.CANCELED,
        }:
            released_lease = replace(attempt.lease, lease_expiry=self._clock.now())
        runtime.attempts[-1] = replace(
            attempt,
            state=runtime.state,
            lease=released_lease,
            completed_at=self._clock.now() if runtime.state is not NodeState.VERIFYING else None,
        )
        return [
            self._emit(
                RuntimeEventType.RESULT_ACCEPTED,
                {"node_id": node_id, "attempt_id": attempt.attempt_id, "status": result.status.value},
                command_id=command.command_id,
            )
        ]

    def commit_workspace(
        self,
        node_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        commit: Callable[[], tuple[str, ...]],
    ) -> tuple[str, ...]:
        """Serialize a Writer's filesystem commit with cancel and fencing decisions.

        The Worker prepares changes outside the canonical workspace.  Holding the
        reducer lock while revalidating and applying the commit gives cancellation,
        replanning, and stale attempts a deterministic order relative to the write.
        """
        with self._lock:
            runtime = self._node(node_id)
            attempt = self.current_attempt(node_id)
            if self._state.canceled:
                raise ReducerError("run was canceled before workspace commit")
            if runtime.state is not NodeState.VERIFYING:
                raise ReducerError("node is not VERIFYING")
            if runtime.result is None or runtime.result.status is not AgentResultStatus.SUCCEEDED:
                raise ReducerError("node has no successful accepted result")
            if attempt is None or attempt.attempt_id != attempt_id:
                raise ReducerError("workspace commit is not for the current attempt")
            if runtime.result.attempt_id != attempt_id:
                raise ReducerError("workspace commit does not match accepted result")
            if attempt.lease.fencing_token.value != fencing_token:
                raise ReducerError("stale fencing token before workspace commit")
            if not attempt.lease.is_valid(self._clock.now()):
                raise ReducerError("lease expired before workspace commit")
            if runtime.workspace_committed:
                return runtime.result.changed_resources

            changed = tuple(commit())
            runtime.workspace_committed = True
            self._emit(
                RuntimeEventType.WORKSPACE_COMMITTED,
                {"node_id": node_id, "attempt_id": attempt_id, "changed_resources": list(changed)},
            )
            self._persist()
            return changed

    def _begin_verification(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if runtime.state is not NodeState.VERIFYING:
            return self._reject(command, node_id, "node is not VERIFYING")
        if runtime.definition.is_writer and not runtime.workspace_committed:
            return self._reject(command, node_id, "writer workspace commit is not confirmed")
        return [
            self._emit(
                RuntimeEventType.VERIFICATION_STARTED,
                {"node_id": node_id},
                command_id=command.command_id,
            )
        ]

    def _accept_verification(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if runtime.state is not NodeState.VERIFYING:
            return self._reject(command, node_id, "node is not VERIFYING")
        runtime.state = NodeState.SUCCEEDED
        if runtime.attempts:
            runtime.attempts[-1] = replace(
                runtime.attempts[-1],
                state=NodeState.SUCCEEDED,
                lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                completed_at=self._clock.now(),
            )
        return [
            self._emit(
                RuntimeEventType.VERIFICATION_ACCEPTED,
                {"node_id": node_id, "record": command.payload.get("record")},
                command_id=command.command_id,
            )
        ]

    def _fail_verification(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if runtime.state is not NodeState.VERIFYING:
            return self._reject(command, node_id, "node is not VERIFYING")
        can_retry = (
            len(runtime.attempts) - runtime.resume_credits
            < runtime.definition.max_attempts
        )
        runtime.state = NodeState.FAILED_RETRYABLE if can_retry else NodeState.FAILED_TERMINAL
        runtime.result = None if can_retry else runtime.result
        if runtime.attempts:
            runtime.attempts[-1] = replace(
                runtime.attempts[-1],
                state=runtime.state,
                lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                completed_at=self._clock.now(),
            )
        return [
            self._emit(
                RuntimeEventType.VERIFICATION_FAILED,
                {
                    "node_id": node_id,
                    "retryable": can_retry,
                    "reason": command.payload.get("reason", "verification failed"),
                },
                command_id=command.command_id,
            )
        ]

    def prepare_retry(self, node_id: str) -> bool:
        """Move a bounded retryable node back to READY through the reducer lock."""
        with self._lock:
            runtime = self._node(node_id)
            if runtime.state is not NodeState.FAILED_RETRYABLE:
                return False
            if len(runtime.attempts) - runtime.resume_credits >= runtime.definition.max_attempts:
                runtime.state = NodeState.FAILED_TERMINAL
                self._persist()
                return False
            runtime.state = NodeState.READY
            runtime.current_attempt_id = None
            runtime.result = None
            runtime.workspace_committed = True
            self._persist()
            return True

    def expire_attempt(self, node_id: str) -> bool:
        """Fence an expired claim and make a bounded retry available."""
        with self._lock:
            runtime = self._node(node_id)
            attempt = self.current_attempt(node_id)
            if attempt is None or attempt.lease.is_valid(self._clock.now()):
                return False
            can_retry = (
                len(runtime.attempts) - runtime.resume_credits
                < runtime.definition.max_attempts
            )
            runtime.state = NodeState.FAILED_RETRYABLE if can_retry else NodeState.FAILED_TERMINAL
            runtime.attempts[-1] = replace(
                attempt,
                state=runtime.state,
                completed_at=self._clock.now(),
            )
            runtime.current_attempt_id = None
            runtime.result = None
            runtime.workspace_committed = True
            if can_retry:
                runtime.state = NodeState.READY
            self._persist()
            return True

    def _cancel_run(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        self._state.canceled = True
        for runtime in self._state.nodes.values():
            if runtime.state not in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.SUPERSEDED}:
                runtime.state = NodeState.CANCELED
                if runtime.attempts and runtime.current_attempt_id:
                    runtime.attempts[-1] = replace(
                        runtime.attempts[-1],
                        state=NodeState.CANCELED,
                        lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                        completed_at=self._clock.now(),
                    )
        return [
            self._emit(
                RuntimeEventType.RUN_CANCELED,
                {"reason": command.payload.get("reason", "canceled")},
                command_id=command.command_id,
            )
        ]

    def _resume_run(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        if self._state.canceled:
            raise ReducerError("a canceled run cannot be resumed")
        self._state.paused = False
        return [self._emit(RuntimeEventType.RUN_RESUMED, {}, command_id=command.command_id)]

    def _amend_task(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        amendment = command.payload.get("amendment")
        if not isinstance(amendment, TaskAmendment):
            raise ReducerError("AmendTask requires TaskAmendment")
        if amendment.base_contract_revision != self._state.contract_revision:
            raise ReducerError("amendment base revision is stale")
        affected = set(amendment.affected_node_ids)
        for node_id in affected:
            runtime = self._state.nodes.get(node_id)
            if runtime is not None:
                runtime.state = NodeState.SUPERSEDED
        self._state.contract_revision = amendment.next_contract_revision
        return [
            self._emit(
                RuntimeEventType.TASK_AMENDED,
                {"amendment": amendment.to_dict(), "superseded_nodes": sorted(affected)},
                command_id=command.command_id,
            )
        ]

    def _approval(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        approval_id = str(command.payload.get("approval_id", ""))
        approved = command.command_type is RuntimeCommandType.APPROVE_TOOL_CALL
        if approved and approval_id in self._state.consumed_approvals:
            raise ReducerError("approval already consumed")
        if approved:
            self._state.consumed_approvals.add(approval_id)
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._state.nodes.get(node_id)
        if runtime is not None and runtime.state is NodeState.WAITING_APPROVAL:
            if approved:
                # A suspended process cannot be assumed to still exist after
                # restart.  Resume by creating a fresh fenced Attempt for the
                # same logical Node; its persisted idempotency key is reused by
                # the Scheduler for any replay-safe operation.
                runtime.state = NodeState.READY
                runtime.current_attempt_id = None
                runtime.result = None
                runtime.resume_credits += 1
            else:
                runtime.state = NodeState.FAILED_TERMINAL
                if runtime.attempts:
                    runtime.attempts[-1] = replace(
                        runtime.attempts[-1],
                        state=NodeState.FAILED_TERMINAL,
                        lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                        completed_at=self._clock.now(),
                    )
        return [
            self._emit(
                RuntimeEventType.TOOL_CALL_APPROVED if approved else RuntimeEventType.TOOL_CALL_REJECTED,
                dict(command.payload),
                command_id=command.command_id,
            )
        ]

    def _refresh_ready_nodes(self) -> None:
        for runtime in self._state.nodes.values():
            if runtime.state is not NodeState.PENDING:
                continue
            dependencies = [self._state.nodes.get(item) for item in runtime.definition.dependencies]
            if dependencies and all(item is not None and item.state is NodeState.SUCCEEDED for item in dependencies):
                runtime.state = NodeState.READY
                self._emit(RuntimeEventType.NODE_READY, {"node_id": runtime.definition.node_id})

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "schema_version": "minicode-runtime-state-v2",
            "run_id": self._state.run_id,
            "contract_revision": self._state.contract_revision,
            "current_plan_version": self._state.current_plan_version,
            "plans": {str(key): value.to_dict() for key, value in self._state.plans.items()},
            "nodes": {
                node_id: {
                    "definition": runtime.definition.to_dict(),
                    "state": runtime.state.value,
                    "attempts": [attempt.to_dict() for attempt in runtime.attempts],
                    "current_attempt_id": runtime.current_attempt_id,
                    "result": runtime.result.to_dict() if runtime.result else None,
                    "rejection_reasons": list(runtime.rejection_reasons),
                    "resume_credits": runtime.resume_credits,
                    "workspace_committed": runtime.workspace_committed,
                }
                for node_id, runtime in self._state.nodes.items()
            },
            "events": [event.to_dict() for event in self._state.events],
            "canceled": self._state.canceled,
            "paused": self._state.paused,
            "processed_commands": {
                key: list(value) for key, value in self._state.processed_commands.items()
            },
            "consumed_approvals": sorted(self._state.consumed_approvals),
            "next_fencing_token": self._state.next_fencing_token,
            "tokens_used": self._state.tokens_used,
            "cost_used": self._state.cost_used,
        }

    def _persist(self) -> None:
        if self._store is not None:
            self._store.save(self._snapshot_unlocked())


def command(
    run_id: str,
    command_type: RuntimeCommandType,
    payload: Mapping[str, Any] | None = None,
    *,
    command_id: str | None = None,
) -> RuntimeCommand:
    return RuntimeCommand(
        command_id=command_id or new_id("command"),
        run_id=run_id,
        command_type=command_type,
        payload=dict(payload or {}),
    )


def _node_from_dict(raw: Mapping[str, Any]) -> PlanNode:
    return PlanNode(
        node_id=str(raw.get("node_id", "")),
        objective=str(raw.get("objective", "")),
        role=str(raw.get("role", "")),
        dependencies=tuple(str(item) for item in raw.get("dependencies", [])),
        allowed_effects=tuple(ToolEffect(str(item)) for item in raw.get("allowed_effects", [])),
        acceptance_criteria=tuple(str(item) for item in raw.get("acceptance_criteria", [])),
        max_attempts=int(raw.get("max_attempts", 1)),
    )


def _plan_from_dict(raw: Mapping[str, Any]) -> PlanVersion:
    return PlanVersion(
        run_id=str(raw.get("run_id", "")),
        plan_version=int(raw.get("plan_version", 1)),
        contract_revision=int(raw.get("contract_revision", 1)),
        nodes=tuple(
            _node_from_dict(item)
            for item in raw.get("nodes", [])
            if isinstance(item, Mapping)
        ),
        created_at=float(raw.get("created_at", 0.0)),
        supersedes=int(raw["supersedes"]) if raw.get("supersedes") is not None else None,
    )


def _attempt_from_dict(raw: Mapping[str, Any]) -> NodeAttempt:
    lease_raw = raw.get("lease", {})
    if not isinstance(lease_raw, Mapping):
        lease_raw = {}
    token_raw = lease_raw.get("fencing_token", {})
    token_value = token_raw.get("value", 0) if isinstance(token_raw, Mapping) else token_raw
    return NodeAttempt(
        attempt_id=str(raw.get("attempt_id", "")),
        run_id=str(raw.get("run_id", "")),
        plan_version=int(raw.get("plan_version", 1)),
        node_id=str(raw.get("node_id", "")),
        attempt_number=int(raw.get("attempt_number", 1)),
        lease=Lease(
            lease_owner=str(lease_raw.get("lease_owner", "")),
            lease_expiry=float(lease_raw.get("lease_expiry", 0.0)),
            fencing_token=FencingToken(int(token_value)),
        ),
        state=NodeState(str(raw.get("state", NodeState.CLAIMED.value))),
        idempotency_key=str(raw.get("idempotency_key", "")),
        started_at=float(raw["started_at"]) if raw.get("started_at") is not None else None,
        completed_at=float(raw["completed_at"]) if raw.get("completed_at") is not None else None,
    )


def _evidence_from_dict(raw: Mapping[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        evidence_type=str(raw.get("evidence_type", "")),
        resource=str(raw.get("resource", "")),
        content_hash=str(raw.get("content_hash", "")),
        claim=str(raw.get("claim", "")),
        producer_node_id=str(raw.get("producer_node_id", "")),
        base_revision=str(raw.get("base_revision", "")),
        line_start=int(raw["line_start"]) if raw.get("line_start") is not None else None,
        line_end=int(raw["line_end"]) if raw.get("line_end") is not None else None,
        page=int(raw["page"]) if raw.get("page") is not None else None,
    )


def _artifact_from_dict(raw: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(raw.get("artifact_id", "")),
        content_hash=str(raw.get("content_hash", "")),
        size=int(raw.get("size", 0)),
        media_type=str(raw.get("media_type", "application/octet-stream")),
        store_key=str(raw.get("store_key", "")),
    )


def _verification_from_dict(raw: Mapping[str, Any]) -> VerificationRecord:
    return VerificationRecord(
        verifier=str(raw.get("verifier", "")),
        passed=bool(raw.get("passed", False)),
        scope=str(raw.get("scope", "")),
        details=str(raw.get("details", "")),
        evidence_refs=tuple(
            _evidence_from_dict(item)
            for item in raw.get("evidence_refs", [])
            if isinstance(item, Mapping)
        ),
        deterministic=bool(raw.get("deterministic", True)),
        created_at=float(raw.get("created_at", 0.0)),
    )


def _result_from_dict(raw: Mapping[str, Any]) -> AgentResult:
    failure_raw = raw.get("failure")
    failure = None
    if isinstance(failure_raw, Mapping):
        failure = ExecutionFailure(
            kind=ExecutionFailureKind(str(failure_raw.get("kind"))),
            message=str(failure_raw.get("message", "")),
            retryable=bool(failure_raw.get("retryable", False)),
            ambiguous_side_effect=bool(failure_raw.get("ambiguous_side_effect", False)),
        )
    return AgentResult(
        run_id=str(raw.get("run_id", "")),
        plan_version=int(raw.get("plan_version", 1)),
        node_id=str(raw.get("node_id", "")),
        attempt_id=str(raw.get("attempt_id", "")),
        status=AgentResultStatus(str(raw.get("status"))),
        base_revision=str(raw.get("base_revision", "")),
        output_summary=str(raw.get("output_summary", "")),
        contract_revision=int(raw.get("contract_revision", 1)),
        evidence_refs=tuple(
            _evidence_from_dict(item)
            for item in raw.get("evidence_refs", [])
            if isinstance(item, Mapping)
        ),
        artifact_refs=tuple(
            _artifact_from_dict(item)
            for item in raw.get("artifact_refs", [])
            if isinstance(item, Mapping)
        ),
        changed_resources=tuple(str(item) for item in raw.get("changed_resources", [])),
        verification_records=tuple(
            _verification_from_dict(item)
            for item in raw.get("verification_records", [])
            if isinstance(item, Mapping)
        ),
        usage=dict(raw.get("usage", {})),
        warnings=tuple(str(item) for item in raw.get("warnings", [])),
        failure=failure,
    )


def _event_from_dict(raw: Mapping[str, Any]) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=str(raw.get("event_id", "")),
        run_id=str(raw.get("run_id", "")),
        sequence=int(raw.get("sequence", 0)),
        event_type=RuntimeEventType(str(raw.get("event_type"))),
        payload=dict(raw.get("payload", {})),
        command_id=str(raw["command_id"]) if raw.get("command_id") is not None else None,
        created_at=float(raw.get("created_at", 0.0)),
    )
