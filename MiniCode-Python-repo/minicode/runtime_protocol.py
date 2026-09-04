"""Versioned contracts shared by every MiniCode execution profile.

The objects in this module deliberately contain no provider-specific model
configuration.  They are the durable boundary between planning, scheduling,
workers, policy, verification, and session persistence.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "minicode-runtime-v2"


class RequestedMode(str, Enum):
    STANDARD = "standard"
    ULTRA = "ultra"


class SelectedRoute(str, Enum):
    STANDARD_SINGLE = "standard-single"
    DEEP_SINGLE = "deep-single"
    ULTRA_READ_FANOUT = "ultra-read-fanout"


class VerificationLevel(str, Enum):
    LIGHT = "light"
    STRICT = "strict"


class WriteIsolation(str, Enum):
    DIRECT = "direct"
    TEMPORARY_WORKSPACE = "temporary-workspace"


class NodeState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_INPUT = "WAITING_INPUT"
    VERIFYING = "VERIFYING"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    SUCCEEDED = "SUCCEEDED"
    CANCELED = "CANCELED"
    SUPERSEDED = "SUPERSEDED"


class AgentResultStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    CANCELED = "canceled"


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    PRIVILEGED = "privileged"


class ReplayPolicy(str, Enum):
    SAFE = "safe"
    IDEMPOTENT_WITH_KEY = "idempotent_with_key"
    VERIFY_BEFORE_RETRY = "verify_before_retry"
    NEVER_AUTO_RETRY = "never_auto_retry"


class RuntimeCommandType(str, Enum):
    APPROVE_TOOL_CALL = "ApproveToolCall"
    REJECT_TOOL_CALL = "RejectToolCall"
    AMEND_TASK = "AmendTask"
    CANCEL_RUN = "CancelRun"
    RESUME_RUN = "ResumeRun"
    CLAIM_NODE = "ClaimNode"
    START_ATTEMPT = "StartAttempt"
    SUBMIT_RESULT = "SubmitResult"
    BEGIN_VERIFICATION = "BeginVerification"
    ACCEPT_VERIFICATION = "AcceptVerification"
    FAIL_VERIFICATION = "FailVerification"


class RuntimeEventType(str, Enum):
    TOOL_CALL_APPROVED = "ToolCallApproved"
    TOOL_CALL_REJECTED = "ToolCallRejected"
    TASK_AMENDED = "TaskAmended"
    RUN_CANCELED = "RunCanceled"
    RUN_RESUMED = "RunResumed"
    NODE_READY = "NodeReady"
    NODE_CLAIMED = "NodeClaimed"
    ATTEMPT_STARTED = "AttemptStarted"
    RESULT_ACCEPTED = "ResultAccepted"
    RESULT_REJECTED = "ResultRejected"
    WORKSPACE_COMMITTED = "WorkspaceCommitted"
    VERIFICATION_STARTED = "VerificationStarted"
    VERIFICATION_ACCEPTED = "VerificationAccepted"
    VERIFICATION_FAILED = "VerificationFailed"


class ExecutionFailureKind(str, Enum):
    POLICY_DENIED = "PolicyDenied"
    APPROVAL_EXPIRED = "ApprovalExpired"
    TOOL_FAILED = "ToolFailed"
    WORKER_TIMED_OUT = "WorkerTimedOut"
    VERIFICATION_FAILED = "VerificationFailed"
    CANCELED = "Canceled"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


class SerializableContract:
    schema_version: str

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))  # type: ignore[call-overload]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class BudgetEnvelope(SerializableContract):
    max_agents: int = 1
    max_active_workers: int = 1
    max_delegation_depth: int = 1
    max_tokens: int | None = None
    max_cost: float | None = None
    deadline: float | None = None
    retry_budget: int = 0
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class TaskEnvelope(SerializableContract):
    run_id: str
    request: str
    workspace: str
    requested_profile: RequestedMode = RequestedMode.STANDARD
    created_at: float = field(default_factory=time.time)
    session_id: str | None = None
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class TaskContract(SerializableContract):
    envelope: TaskEnvelope
    contract_revision: int = 1
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    budget: BudgetEnvelope = field(default_factory=BudgetEnvelope)
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class TaskAmendment(SerializableContract):
    amendment_id: str
    run_id: str
    base_contract_revision: int
    next_contract_revision: int
    changed_fields: Mapping[str, Any]
    affected_node_ids: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ExecutionProfile(SerializableContract):
    requested_mode: RequestedMode
    selected_route: SelectedRoute
    reasoning_budget: str
    planner_budget: int
    verifier_budget: int
    max_active_workers: int
    max_agents: int
    max_tokens: int | None
    max_cost: float | None
    deadline: float | None
    retry_budget: int
    max_delegation_depth: int
    verification_level: VerificationLevel
    write_isolation: WriteIsolation
    dynamic_worker_delegation: bool = False
    schema_version: str = SCHEMA_VERSION

    @property
    def budget(self) -> BudgetEnvelope:
        return BudgetEnvelope(
            max_agents=self.max_agents,
            max_active_workers=self.max_active_workers,
            max_delegation_depth=self.max_delegation_depth,
            max_tokens=self.max_tokens,
            max_cost=self.max_cost,
            deadline=self.deadline,
            retry_budget=self.retry_budget,
        )


@dataclass(frozen=True, slots=True)
class TopologyDecision(SerializableContract):
    requested_profile: RequestedMode
    selected_route: SelectedRoute
    independent_subproblems: tuple[str, ...]
    shared_context_dependency: str
    write_conflict_risk: str
    expected_parallel_gain: float
    deterministic_verifiability: bool
    estimated_extra_cost: float
    reasons: tuple[str, ...]
    rejected_routes: Mapping[str, str]
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class PlanNode(SerializableContract):
    node_id: str
    objective: str
    role: str
    dependencies: tuple[str, ...] = ()
    allowed_effects: tuple[ToolEffect, ...] = (ToolEffect.READ_ONLY,)
    acceptance_criteria: tuple[str, ...] = ()
    max_attempts: int = 1
    schema_version: str = SCHEMA_VERSION

    @property
    def is_writer(self) -> bool:
        return ToolEffect.WORKSPACE_WRITE in self.allowed_effects


@dataclass(frozen=True, slots=True)
class PlanVersion(SerializableContract):
    run_id: str
    plan_version: int
    contract_revision: int
    nodes: tuple[PlanNode, ...]
    created_at: float = field(default_factory=time.time)
    supersedes: int | None = None
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class FencingToken(SerializableContract):
    value: int
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class Lease(SerializableContract):
    lease_owner: str
    lease_expiry: float
    fencing_token: FencingToken
    schema_version: str = SCHEMA_VERSION

    def is_valid(self, now: float) -> bool:
        return now < self.lease_expiry


@dataclass(frozen=True, slots=True)
class NodeAttempt(SerializableContract):
    attempt_id: str
    run_id: str
    plan_version: int
    node_id: str
    attempt_number: int
    lease: Lease
    state: NodeState = NodeState.CLAIMED
    idempotency_key: str = ""
    started_at: float | None = None
    completed_at: float | None = None
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ArtifactRef(SerializableContract):
    artifact_id: str
    content_hash: str
    size: int
    media_type: str
    store_key: str
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class EvidenceRef(SerializableContract):
    evidence_type: str
    resource: str
    content_hash: str
    claim: str
    producer_node_id: str
    base_revision: str
    line_start: int | None = None
    line_end: int | None = None
    page: int | None = None
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class VerificationRecord(SerializableContract):
    verifier: str
    passed: bool
    scope: str
    details: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()
    deterministic: bool = True
    created_at: float = field(default_factory=time.time)
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ContextManifest(SerializableContract):
    contract_revision: int
    plan_version: int
    node_id: str
    base_revision: str
    included_files: Mapping[str, str]
    dependency_result_refs: tuple[str, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    memory_record: str | None = None
    policy_version: str = "runtime-policy-v1"
    prompt_template_version: str = "agent-loop-v1"
    tool_capability_scope: tuple[ToolEffect, ...] = (ToolEffect.READ_ONLY,)
    budget_allocation: BudgetEnvelope = field(default_factory=BudgetEnvelope)
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AgentInvocationSpec(SerializableContract):
    run_id: str
    plan_version: int
    node_id: str
    attempt_id: str
    role: str
    objective: str
    context_manifest: ContextManifest
    allowed_effects: tuple[ToolEffect, ...]
    budget: BudgetEnvelope
    acceptance_criteria: tuple[str, ...] = ()
    idempotency_key: str = ""
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ExecutionFailure(SerializableContract):
    kind: ExecutionFailureKind
    message: str
    retryable: bool = False
    ambiguous_side_effect: bool = False
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class AgentResult(SerializableContract):
    run_id: str
    plan_version: int
    node_id: str
    attempt_id: str
    status: AgentResultStatus
    base_revision: str
    output_summary: str
    contract_revision: int = 1
    evidence_refs: tuple[EvidenceRef, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    changed_resources: tuple[str, ...] = ()
    verification_records: tuple[VerificationRecord, ...] = ()
    usage: Mapping[str, int | float] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    failure: ExecutionFailure | None = None
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class RuntimeCommand(SerializableContract):
    command_id: str
    run_id: str
    command_type: RuntimeCommandType
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class RuntimeEvent(SerializableContract):
    event_id: str
    run_id: str
    sequence: int
    event_type: RuntimeEventType
    payload: Mapping[str, Any]
    command_id: str | None = None
    created_at: float = field(default_factory=time.time)
    schema_version: str = SCHEMA_VERSION


def operation_digest(
    *,
    run_id: str,
    node_id: str,
    attempt_id: str,
    tool_call_id: str,
    cwd: str,
    arguments: Mapping[str, Any],
    environment_scope: Mapping[str, Any],
    base_revision: str,
) -> str:
    payload = {
        "run_id": run_id,
        "node_id": node_id,
        "attempt_id": attempt_id,
        "tool_call_id": tool_call_id,
        "cwd": cwd,
        "arguments": _jsonable(arguments),
        "environment_scope": _jsonable(environment_scope),
        "base_revision": base_revision,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalRequest(SerializableContract):
    approval_id: str
    run_id: str
    node_id: str
    attempt_id: str
    tool_call_id: str
    operation_digest: str
    cwd: str
    arguments: Mapping[str, Any]
    environment_scope: Mapping[str, Any]
    base_revision: str
    expires_at: float
    single_use: bool = True
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        node_id: str,
        attempt_id: str,
        tool_call_id: str,
        cwd: str,
        arguments: Mapping[str, Any],
        environment_scope: Mapping[str, Any],
        base_revision: str,
        expires_at: float,
        single_use: bool = True,
    ) -> ApprovalRequest:
        digest = operation_digest(
            run_id=run_id,
            node_id=node_id,
            attempt_id=attempt_id,
            tool_call_id=tool_call_id,
            cwd=cwd,
            arguments=arguments,
            environment_scope=environment_scope,
            base_revision=base_revision,
        )
        return cls(
            approval_id=new_id("approval"),
            run_id=run_id,
            node_id=node_id,
            attempt_id=attempt_id,
            tool_call_id=tool_call_id,
            operation_digest=digest,
            cwd=cwd,
            arguments=dict(arguments),
            environment_scope=dict(environment_scope),
            base_revision=base_revision,
            expires_at=expires_at,
            single_use=single_use,
        )


@dataclass(frozen=True, slots=True)
class ApprovalGrant(SerializableContract):
    approval_id: str
    operation_digest: str
    granted_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DelegationRequest(SerializableContract):
    run_id: str
    parent_node_id: str
    requested_role: str
    objective: str
    context_requirements: tuple[str, ...]
    allowed_tool_capabilities: tuple[ToolEffect, ...]
    proposed_budget: BudgetEnvelope
    delegation_depth: int
    idempotency_key: str
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class DeliveryManifest(SerializableContract):
    run_id: str
    plan_version: int
    completed: bool
    output_summary: str
    result_refs: tuple[str, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    verification_records: tuple[VerificationRecord, ...] = ()
    reasons: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
