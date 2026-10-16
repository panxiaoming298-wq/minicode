"""Versioned contracts shared by every MiniCode execution profile.

The objects in this module deliberately contain no provider-specific model
configuration.  They are the durable boundary between planning, scheduling,
workers, policy, verification, and session persistence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "minicode-runtime-v2"


class RequestedMode(str, Enum):
    STANDARD = "standard"
    DEEP = "deep"
    ULTRA = "ultra"


class SelectedRoute(str, Enum):
    PRIMARY_LED = "primary-led"
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
    WAITING_CHILDREN = "WAITING_CHILDREN"
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
    WAITING_CHILDREN = "waiting_children"
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
    CANCEL_TASK = "CancelTask"
    CLAIM_NODE = "ClaimNode"
    START_ATTEMPT = "StartAttempt"
    RENEW_LEASE = "RenewLease"
    SUBMIT_RESULT = "SubmitResult"
    BEGIN_VERIFICATION = "BeginVerification"
    ACCEPT_VERIFICATION = "AcceptVerification"
    FAIL_VERIFICATION = "FailVerification"
    REQUEST_REPAIR = "RequestRepair"
    ACKNOWLEDGE_ADVISORY_FAILURE = "AcknowledgeAdvisoryFailure"


class RuntimeEventType(str, Enum):
    TOOL_CALL_APPROVED = "ToolCallApproved"
    TOOL_CALL_REJECTED = "ToolCallRejected"
    TASK_AMENDED = "TaskAmended"
    RUN_CANCELED = "RunCanceled"
    RUN_RESUMED = "RunResumed"
    NODE_READY = "NodeReady"
    NODE_CLAIMED = "NodeClaimed"
    ATTEMPT_STARTED = "AttemptStarted"
    LEASE_RENEWED = "LeaseRenewed"
    MODEL_CALL_RESERVED = "ModelCallReserved"
    MODEL_REQUEST_DECLINED = "ModelRequestDeclined"
    MODEL_USAGE_RECORDED = "ModelUsageRecorded"
    RESULT_ACCEPTED = "ResultAccepted"
    RESULT_REJECTED = "ResultRejected"
    WORKSPACE_COMMITTED = "WorkspaceCommitted"
    CANDIDATE_INTEGRATION_STARTED = "CandidateIntegrationStarted"
    CANDIDATE_INTEGRATION_FAILED = "CandidateIntegrationFailed"
    VERIFICATION_STARTED = "VerificationStarted"
    VERIFICATION_ACCEPTED = "VerificationAccepted"
    VERIFICATION_FAILED = "VerificationFailed"
    PRIMARY_SUSPENDED = "PrimarySuspended"
    PRIMARY_RESUMABLE = "PrimaryResumable"
    REPAIR_REQUESTED = "RepairRequested"
    REPAIR_STOPPED = "RepairStopped"
    CONSULTATION_DELIVERED = "ConsultationDelivered"
    CONSULTATION_RECOVERY_REQUESTED = "ConsultationRecoveryRequested"
    CONSULTATION_SUMMARY_READ = "ConsultationSummaryRead"
    ADVISORY_FAILURE_ACKNOWLEDGED = "AdvisoryFailureAcknowledged"
    CONSULTATION_BUDGET_DECLINED = "ConsultationBudgetDeclined"
    PRIMARY_ERROR_RECOVERY_RECORDED = "PrimaryErrorRecoveryRecorded"
    NODE_CANCELED = "NodeCanceled"
    TASK_CANCEL_INTENT = "TaskCancelIntent"
    CHILD_OUTCOME_RECORDED = "ChildOutcomeRecorded"
    DEPENDENCY_WAIT_REGISTERED = "DependencyWaitRegistered"
    DEPENDENCY_WAIT_RESUMED = "DependencyWaitResumed"
    PATCH_RECORDED = "PatchRecorded"
    CANDIDATE_LEASE_OBSERVED = "CandidateLeaseObserved"
    WORKER_SETTLED = "WorkerSettled"
    CANDIDATE_RETIRED = "CandidateRetired"
    BUILDER_SELECTED = "BuilderSelected"
    AGENT_MESSAGE_QUEUED = "AgentMessageQueued"
    AGENT_MESSAGE_CONSUMED = "AgentMessageConsumed"
    AGENT_MESSAGE_REJECTED = "AgentMessageRejected"
    AGENT_CHECKPOINT_RECORDED = "AgentCheckpointRecorded"
    AGENT_TEAM_CHANGED = "AgentTeamChanged"
    AGENT_RESULT_DELIVERED = "AgentResultDelivered"
    AGENT_RESULT_HANDLED = "AgentResultHandled"
    AGENT_CHECK_RECORDED = "AgentCheckRecorded"
    AGENT_INTERRUPTION_REQUESTED = "AgentInterruptionRequested"
    AGENT_TASK_DISMISSED = "AgentTaskDismissed"
    USER_INPUT_QUEUED = "UserInputQueued"
    USER_INPUT_CONSUMED = "UserInputConsumed"
    USER_INPUT_RESOLVED = "UserInputResolved"
    USER_INPUT_CANCELED = "UserInputCanceled"
    USER_INPUT_SEALED = "UserInputSealed"
    REVIEW_ASSESSED = "ReviewAssessed"


class ExecutionFailureKind(str, Enum):
    POLICY_DENIED = "PolicyDenied"
    APPROVAL_EXPIRED = "ApprovalExpired"
    TOOL_FAILED = "ToolFailed"
    TOOL_TIMEOUT = "ToolTimeout"
    UNCERTAIN_SIDE_EFFECT = "UncertainSideEffect"
    WORKER_TIMED_OUT = "WorkerTimedOut"
    WORKSPACE_BUSY = "WorkspaceBusy"
    PROTOCOL_ERROR = "ProtocolError"
    BUDGET_EXHAUSTED = "BudgetExhausted"
    PROVIDER_BALANCE = "ProviderBalanceUnavailable"
    PROVIDER_ERROR = "ProviderError"
    MODEL_USAGE_UNRECONCILED = "ModelUsageUnreconciled"
    CONSULTATION_BUDGET_LIMITED = "ConsultationBudgetLimited"
    MODEL_OUTPUT_TRUNCATED = "ModelOutputTruncated"
    VERIFICATION_FAILED = "VerificationFailed"
    REVIEW_UNAVAILABLE = "ReviewUnavailable"
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
        def compatible(items: list[tuple[str, Any]]) -> dict[str, Any]:
            value = dict(items)
            # Keep old PlanNode wire shapes, including nested checkpoints.
            if value.get("criticality") == "required" and "node_id" in value and "objective" in value:
                value.pop("criticality")
            if value.get("context_files") == ():
                value.pop("context_files")
            if value.get("alternative_group") is None:
                value.pop("alternative_group", None)
            if value.get("required_consultation") is None and "envelope" in value:
                value.pop("required_consultation", None)
            for name in ("review_policy", "runtime_profile", "primary_runtime_profile", "review_result"):
                if value.get(name) is None:
                    value.pop(name, None)
            return value

        return _jsonable(asdict(self, dict_factory=compatible))  # type: ignore[call-overload]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def acceptance_criterion_ids(criteria: tuple[str, ...]) -> tuple[str, ...]:
    """Return ordered, stable identifiers without exposing mutable indices alone."""

    return tuple(
        f"criterion-{index:03d}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"
        for index, text in enumerate(criteria, start=1)
    )


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
class RequiredConsultation(SerializableContract):
    """Explicit per-task process condition, never inferred from prose or mode."""

    min_helpers: int
    before_workspace_write: bool = True
    single_batch: bool = True
    schema_version: str = "minicode-required-consultation-v1"

    def __post_init__(self) -> None:
        if type(self.min_helpers) is not int or not 1 <= self.min_helpers <= 8:
            raise ValueError("required consultation min_helpers must be an integer from 1 to 8")
        if type(self.before_workspace_write) is not bool or type(self.single_batch) is not bool:
            raise ValueError("required consultation ordering options must be booleans")
        if self.schema_version != "minicode-required-consultation-v1":
            raise ValueError("unsupported required consultation version")

    @classmethod
    def parse(cls, value: Any) -> RequiredConsultation | None:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) - {
            "min_helpers", "before_workspace_write", "single_batch", "schema_version"
        } or "min_helpers" not in value:
            raise ValueError("required consultation must be an explicit bounded process contract")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class TaskContract(SerializableContract):
    envelope: TaskEnvelope
    contract_revision: int = 1
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    budget: BudgetEnvelope = field(default_factory=BudgetEnvelope)
    schema_version: str = SCHEMA_VERSION
    required_consultation: RequiredConsultation | None = None

    def __post_init__(self) -> None:
        if self.required_consultation is not None and not isinstance(self.required_consultation, RequiredConsultation):
            raise ValueError("TaskContract requires a typed RequiredConsultation")


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
    autonomous_delegation: bool = False
    # Experimental multi-candidate execution. Default OFF: with it disabled the
    # profile serialises writers exactly as before. When enabled the primary and
    # up to ``max_active_writers - 1`` Builders each own a distinct candidate and
    # execute at the same time, still bounded by the stricter of
    # ``max_active_workers`` and ``max_agents``.
    parallel_candidates: bool = False
    max_active_writers: int = 1
    async_agents: bool = False
    agent_messages: bool = False
    agent_resume: bool = False
    agent_teams: bool = False
    agent_fork: bool = False
    coordinator: bool = False
    agent_team_writes: bool = False
    shared_workspace_writes: bool = False
    primary_runtime_profile: str | None = None
    review_policy: Mapping[str, Any] | None = None
    dynamic_review: bool = False

    def __post_init__(self) -> None:
        if type(self.dynamic_review) is not bool or self.dynamic_review and self.review_policy is None:
            raise ValueError("dynamic review requires an admitted primary review policy")
        if self.primary_runtime_profile not in {None, "single", "single-deep"}:
            raise ValueError("invalid primary runtime profile")
        if self.review_policy is not None:
            from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
            if not self.primary_led:
                raise ValueError("review policy requires primary-led execution")
            object.__setattr__(self, "review_policy", ReviewPolicy.from_dict(self.review_policy).to_dict())

    @property
    def primary_led(self) -> bool:
        return self.selected_route is SelectedRoute.PRIMARY_LED

    @property
    def effective_writer_capacity(self) -> int:
        """Concurrent candidate writers, never above the total worker ceiling."""
        requested = self.max_active_writers if self.parallel_candidates or self.shared_workspace_writes else 1
        return max(1, min(requested, max(1, self.max_active_workers)))

    def to_dict(self) -> dict[str, Any]:
        result = SerializableContract.to_dict(self)
        if not self.dynamic_review:
            result.pop("dynamic_review", None)
        if self.primary_runtime_profile is None:
            result.pop("primary_runtime_profile", None)
        if self.review_policy is None:
            result.pop("review_policy", None)
        if not self.shared_workspace_writes:
            result.pop("shared_workspace_writes", None)
        if not self.async_agents:
            result.pop("async_agents", None)
        if not self.agent_messages:
            result.pop("agent_messages", None)
        if not self.agent_resume:
            result.pop("agent_resume", None)
        if not self.agent_teams:
            result.pop("agent_teams", None)
        if not self.agent_fork:
            result.pop("agent_fork", None)
        if not self.coordinator:
            result.pop("coordinator", None)
        if not self.agent_team_writes:
            result.pop("agent_team_writes", None)
        if not self.primary_led:
            # Keep the legacy profile hash byte-for-byte compatible. Adding
            # a new policy field must not invalidate an existing V3 run.
            result.pop("autonomous_delegation", None)
        if not self.parallel_candidates and self.max_active_writers == 1:
            # Same rule for the experimental parallel-candidate fields: a profile
            # that did not opt in must hash exactly as it did before they existed.
            result.pop("parallel_candidates", None)
            result.pop("max_active_writers", None)
        return result

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
    requires_workspace_write: bool
    reasons: tuple[str, ...]
    rejected_routes: Mapping[str, str]
    schema_version: str = SCHEMA_VERSION


def consultation_context_files(value: Any) -> tuple[str, ...]:
    """Explicit workspace-relative initial reads, never an inferred file scope."""
    if not isinstance(value, (list, tuple)) or len(value) > 8:
        raise ValueError("context_files must contain at most 8 workspace-relative file paths")
    for name in value:
        if (not isinstance(name, str) or not 1 <= len(name) <= 256
            or any(ord(char) < 32 for char in name) or "\\" in name or ":" in name
            or PureWindowsPath(name).drive or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
            raise ValueError("context_files requires literal relative paths using /, without traversal or drive prefixes")
    if len(set(value)) != len(value):
        raise ValueError("context_files must not repeat a path")
    return tuple(value)


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
    criticality: str = "required"
    context_files: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    alternative_group: str | None = None
    runtime_profile: str | None = None
    review_policy: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.runtime_profile is not None and (
            self.runtime_profile not in {"single", "single-deep"} or self.node_id != "primary" or self.role != "primary"
        ):
            raise ValueError("frozen runtime profile is only available to the primary")
        if self.review_policy is not None:
            from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
            policy = ReviewPolicy.from_dict(self.review_policy)
            if not ((self.node_id, self.role) == ("primary", "primary") or (
                (self.node_id, self.role) == ("final-verifier", "verifier")
                and self.dependencies == ("primary",) and self.allowed_effects == (ToolEffect.READ_ONLY,)
                and policy.scope != "basic"
            )):
                raise ValueError("review policy requires the primary or its independent final verifier")
            object.__setattr__(self, "review_policy", policy.to_dict())
        paths = consultation_context_files(self.context_files)
        if paths and (not self.node_id.startswith("consult-")
                      or self.role not in {"explorer", "planner", "reviewer", "verifier"}
                      or self.allowed_effects != (ToolEffect.READ_ONLY,)):
            raise ValueError("context_files is only available to read-only consultation nodes")
        object.__setattr__(self, "context_files", paths)
        scope = tuple(self.write_scope)
        if len(scope) > 64 or any(not isinstance(p, str) or not p or "\\" in p or ":" in p
            or any(part in {"", ".", ".."} for part in p.split("/")) for p in scope):
            raise ValueError("write_scope requires bounded literal relative paths")
        object.__setattr__(self, "write_scope", scope)
        if self.alternative_group is not None and (
            not isinstance(self.alternative_group, str) or not self.alternative_group.strip()
            or len(self.alternative_group) > 96 or self.alternative_group != self.alternative_group.strip()
            or not self.node_id.startswith("builder-") or self.role != "writer"
            or self.allowed_effects != (ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE)
        ):
            raise ValueError("alternative_group requires a bounded name on an isolated Builder")

    @property
    def is_writer(self) -> bool:
        return ToolEffect.WORKSPACE_WRITE in self.allowed_effects

    def to_dict(self) -> dict[str, Any]:
        result = SerializableContract.to_dict(self)
        if self.runtime_profile is None:
            result.pop("runtime_profile", None)
        if self.review_policy is None:
            result.pop("review_policy", None)
        return result


@dataclass(frozen=True, slots=True)
class PlanVersion(SerializableContract):
    run_id: str
    plan_version: int
    contract_revision: int
    nodes: tuple[PlanNode, ...]
    created_at: float = field(default_factory=time.time)
    supersedes: int | None = None
    schema_version: str = SCHEMA_VERSION


def validate_review_plan(nodes: tuple[PlanNode, ...]) -> None:
    """Bind the optional outcome rule to an immutable primary assignment."""
    if not any(node.review_policy is not None for node in nodes):
        return
    primaries = [node for node in nodes if node.node_id == "primary" and node.role == "primary"]
    if len(primaries) != 1 or primaries[0].review_policy is None:
        raise ValueError("review policy requires its frozen primary assignment")
    primary = primaries[0]
    reviewers = [node for node in nodes if node.node_id == "final-verifier"]
    review = primary.review_policy
    if review["scope"] == "basic":
        if reviewers:
            raise ValueError("basic review policy cannot contain a final reviewer")
    elif len(reviewers) != 1 or reviewers[0].review_policy != review:
        raise ValueError("final reviewer must retain the primary's admitted review policy")


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
    covered_criteria_ids: tuple[str, ...] = ()
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
    retry_feedback: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    context_files: tuple[str, ...] = ()


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
    reasoning_budget: str = "medium"
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class RuntimeRunIdentity(SerializableContract):
    """Immutable material that a persisted run is allowed to resume against."""

    run_id: str
    workspace: str
    request_hash: str
    messages_hash: str
    profile_hash: str
    policy_hash: str
    session_id: str | None
    requested_profile: RequestedMode
    selected_route: SelectedRoute
    policy_version: str = "runtime-policy-v1"
    prompt_template_version: str = "agent-loop-v1"
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ExecutionFailure(SerializableContract):
    kind: ExecutionFailureKind
    message: str
    retryable: bool = False
    ambiguous_side_effect: bool = False
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class WorkerContinuation(SerializableContract):
    continuation_id: str
    checkpoint: ArtifactRef
    children: tuple[PlanNode, ...]
    input_revision: str
    schema_version: str = "minicode-continuation-v1"
    kind: str = "consultation"
    dependency_wait: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PrimaryRepair(SerializableContract):
    repair_id: str
    run_id: str
    node_id: str
    prior_attempt_id: str
    plan_version: int
    contract_revision: int
    candidate_revision: str
    checkpoint: ArtifactRef
    diagnostics: tuple[VerificationRecord, ...]
    schema_version: str = "minicode-primary-repair-v1"
    review_result: AgentResult | None = None


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
    usage: Mapping[str, int | float | str | bool | None] = field(default_factory=dict)
    metrics: Mapping[str, int | float] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    failure: ExecutionFailure | None = None
    schema_version: str = SCHEMA_VERSION
    continuation: WorkerContinuation | None = None
    session_checkpoint: ArtifactRef | None = None
    consultation_summary_ref: ArtifactRef | None = None

    def to_dict(self) -> dict[str, Any]:
        result = SerializableContract.to_dict(self)
        if self.continuation is None:
            result.pop("continuation", None)
        if self.session_checkpoint is None:
            result.pop("session_checkpoint", None)
        if self.consultation_summary_ref is None:
            result.pop("consultation_summary_ref", None)
        return result


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
    parent_attempt_id: str | None = None


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
