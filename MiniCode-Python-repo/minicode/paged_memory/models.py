"""Domain records for paged memory.

Raw events and artifacts are authoritative.  Memory cards, previews and
context packets are derived projections and always retain source references.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# Results produced while navigating paged memory are audit receipts, not new
# knowledge.  Projecting them back into searchable memory creates recursive
# search/get records that crowd out the source memories they reference.
MEMORY_NAVIGATION_TOOL_NAMES = (
    "memory_search",
    "memory_get",
    "artifact_read",
    "checkpoint_restore",
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now_epoch() -> float:
    return time.time()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def content_hash_text(value: str) -> str:
    return content_hash_bytes(value.encode("utf-8"))


class MemoryMode(str, Enum):
    INLINE = "INLINE"
    WORKING_SET = "WORKING_SET"
    PAGED_MEMORY = "PAGED_MEMORY"


class EventType(str, Enum):
    USER_INPUT = "USER_INPUT"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    TOOL_ERROR = "TOOL_ERROR"
    STATE_UPDATED = "STATE_UPDATED"
    DECISION_CREATED = "DECISION_CREATED"
    DECISION_SUPERSEDED = "DECISION_SUPERSEDED"
    CONSTRAINT_CREATED = "CONSTRAINT_CREATED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    MEMORY_MODE_CHANGED = "MEMORY_MODE_CHANGED"
    MEMORY_INDEX_REQUESTED = "MEMORY_INDEX_REQUESTED"
    MEMORY_INDEXED = "MEMORY_INDEXED"
    MEMORY_IMPORTED = "MEMORY_IMPORTED"
    STORAGE_HEALTH_CHANGED = "STORAGE_HEALTH_CHANGED"
    DELETE_REQUESTED = "DELETE_REQUESTED"
    DELETE_COMPLETED = "DELETE_COMPLETED"
    ARCHIVE_CREATED = "ARCHIVE_CREATED"
    ARCHIVE_RESTORED = "ARCHIVE_RESTORED"


class MemoryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"
    TOMBSTONED = "TOMBSTONED"


class ArtifactStatus(str, Enum):
    PENDING_UPLOAD = "PENDING_UPLOAD"
    ACTIVE = "ACTIVE"
    MISSING = "MISSING"
    CORRUPT = "CORRUPT"
    TOMBSTONED = "TOMBSTONED"


class ArchiveStatus(str, Enum):
    ACTIVE = "ACTIVE"
    VERIFIED = "VERIFIED"
    RESTORED = "RESTORED"
    TOMBSTONED = "TOMBSTONED"


class StorageHealth(str, Enum):
    NORMAL = "NORMAL"
    INDEX_DEGRADED = "INDEX_DEGRADED"
    ARTIFACT_DEGRADED = "ARTIFACT_DEGRADED"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"
    RECOVERING = "RECOVERING"


class DependencyType(str, Enum):
    EVENT = "EVENT"
    DECISION = "DECISION"
    CONSTRAINT = "CONSTRAINT"
    STEP = "STEP"
    ARTIFACT = "ARTIFACT"
    MEMORY = "MEMORY"
    CHECKPOINT = "CHECKPOINT"


class MemoryType(str, Enum):
    CONSTRAINT = "CONSTRAINT"
    DECISION = "DECISION"
    FACT = "FACT"
    ERROR = "ERROR"
    TOOL_RESULT = "TOOL_RESULT"
    ARTIFACT_CHUNK = "ARTIFACT_CHUNK"
    PHASE_SUMMARY = "PHASE_SUMMARY"
    USER_PREFERENCE = "USER_PREFERENCE"
    CHECKPOINT_NOTE = "CHECKPOINT_NOTE"


@dataclass(frozen=True)
class SourceRange:
    unit: str = "CHAR"
    start: int = 0
    end: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskMetrics:
    context_usage_ratio: float = 0.0
    estimated_input_tokens: int = 0
    completed_steps: int = 0
    cumulative_tool_result_tokens: int = 0
    large_artifact_count: int = 0
    parallel_subtasks: int = 0
    cross_session_required: bool = False
    restart_recovery_required: bool = False
    current_phase: str = ""
    recent_page_fault_rate: float = 0.0
    recent_memory_hit_rate: float = 0.0
    repeated_dependency_count: int = 0
    audit_required: bool = False
    mode_downgrade_streak: int = 0


@dataclass(frozen=True)
class ModeSelection:
    mode: MemoryMode
    reason: str
    changed: bool = False


@dataclass
class AgentEvent:
    tenant_id: str
    task_id: str
    run_id: str
    event_type: EventType
    id: str = field(default_factory=lambda: new_id("event"))
    step_id: str = ""
    content_text: str = ""
    content_json: dict[str, Any] = field(default_factory=dict)
    artifact_uri: str = ""
    content_hash: str = ""
    token_count: int = 0
    caused_by_event_id: str = ""
    correlation_id: str = ""
    idempotency_key: str = ""
    created_at: float = field(default_factory=now_epoch)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.content_hash:
            material = self.content_text or stable_json(self.content_json)
            self.content_hash = content_hash_text(material)


@dataclass
class TaskStateSnapshot:
    tenant_id: str
    task_id: str
    version: int = 0
    phase: str = "INITIAL"
    current_step_id: str = ""
    goal: str = ""
    active_constraints: list[dict[str, Any]] = field(default_factory=list)
    active_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_errors: list[dict[str, Any]] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)
    active_artifacts: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=now_epoch)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def apply_patch(self, patch: Mapping[str, Any]) -> TaskStateSnapshot:
        data = self.to_dict()
        for key, value in patch.items():
            if key in data and key not in {"tenant_id", "task_id", "version"}:
                data[key] = value
        data["version"] = self.version + 1
        data["updated_at"] = float(patch.get("updated_at", now_epoch()))
        return TaskStateSnapshot(**data)


def apply_transactional_state_patch(
    state: TaskStateSnapshot, patch: Mapping[str, Any]
) -> TaskStateSnapshot:
    """Apply a state patch plus transaction-local merge operations.

    The merge operations let callers append a constraint or upsert one
    decision without first reading and later replacing the entire collection.
    Authority adapters invoke this only after locking the current state row,
    preventing concurrent event transactions from losing each other's writes.
    """

    effective = dict(patch)
    decision_updates = effective.pop("_active_decisions_upsert", None)
    constraint_additions = effective.pop("_active_constraints_append", None)
    artifact_additions = effective.pop("_active_artifacts_append", None)
    error_additions = effective.pop("_open_errors_append", None)
    completed_step_additions = effective.pop("_completed_steps_append", None)

    if decision_updates is not None:
        if not isinstance(decision_updates, Mapping):
            raise TypeError("_active_decisions_upsert must be a mapping")
        active_decisions = dict(state.active_decisions)
        active_decisions.update(
            {str(key): dict(value) for key, value in decision_updates.items()}
        )
        effective["active_decisions"] = active_decisions

    if constraint_additions is not None:
        if not isinstance(constraint_additions, Sequence) or isinstance(
            constraint_additions, (str, bytes)
        ):
            raise TypeError("_active_constraints_append must be a sequence")
        active_constraints = [dict(value) for value in state.active_constraints]
        seen = {
            str(value.get("memory_id") or value.get("source_event_id") or "")
            for value in active_constraints
        }
        for value in constraint_additions:
            if not isinstance(value, Mapping):
                raise TypeError("constraint additions must be mappings")
            addition = dict(value)
            identity = str(
                addition.get("memory_id") or addition.get("source_event_id") or ""
            )
            if identity and identity not in seen:
                active_constraints.append(addition)
                seen.add(identity)
        effective["active_constraints"] = active_constraints

    if artifact_additions is not None:
        if not isinstance(artifact_additions, Sequence) or isinstance(
            artifact_additions, (str, bytes)
        ):
            raise TypeError("_active_artifacts_append must be a sequence")
        effective["active_artifacts"] = list(
            dict.fromkeys(
                [*state.active_artifacts, *(str(value) for value in artifact_additions)]
            )
        )

    if error_additions is not None:
        if not isinstance(error_additions, Sequence) or isinstance(
            error_additions, (str, bytes)
        ):
            raise TypeError("_open_errors_append must be a sequence")
        open_errors = [dict(value) for value in state.open_errors]
        known_events = {str(value.get("event_id", "")) for value in open_errors}
        for value in error_additions:
            if not isinstance(value, Mapping):
                raise TypeError("error additions must be mappings")
            addition = dict(value)
            event_id = str(addition.get("event_id", ""))
            if not event_id or event_id not in known_events:
                open_errors.append(addition)
                known_events.add(event_id)
        effective["open_errors"] = open_errors[-50:]

    if completed_step_additions is not None:
        if not isinstance(completed_step_additions, Sequence) or isinstance(
            completed_step_additions, (str, bytes)
        ):
            raise TypeError("_completed_steps_append must be a sequence")
        effective["completed_steps"] = list(
            dict.fromkeys(
                [
                    *state.completed_steps,
                    *(str(value) for value in completed_step_additions if str(value)),
                ]
            )
        )

    return state.apply_patch(effective)


@dataclass
class DecisionRecord:
    tenant_id: str
    task_id: str
    decision_key: str
    decision_text: str
    id: str = field(default_factory=lambda: new_id("decision"))
    structured_value: dict[str, Any] = field(default_factory=dict)
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_event_id: str = ""
    supersedes_id: str = ""
    superseded_by_id: str = ""
    created_at: float = field(default_factory=now_epoch)
    updated_at: float = field(default_factory=now_epoch)


@dataclass
class DependencyRecord:
    tenant_id: str
    task_id: str
    step_id: str
    dependency_type: DependencyType
    dependency_id: str
    id: str = field(default_factory=lambda: new_id("dependency"))
    required: bool = True
    dependency_depth: int = 0
    created_at: float = field(default_factory=now_epoch)


@dataclass
class ArtifactRecord:
    tenant_id: str
    task_id: str
    run_id: str
    step_id: str
    artifact_type: str
    uri: str
    content_hash: str
    media_type: str
    size_bytes: int
    id: str = field(default_factory=lambda: new_id("artifact"))
    metadata: dict[str, Any] = field(default_factory=dict)
    source_event_id: str = ""
    status: ArtifactStatus = ArtifactStatus.ACTIVE
    created_at: float = field(default_factory=now_epoch)
    updated_at: float = field(default_factory=now_epoch)
    schema_version: int = 1


@dataclass
class ArchiveManifest:
    """Relational index for one immutable, lossless cold archive object."""

    tenant_id: str
    task_id: str
    artifact_id: str
    uri: str
    content_hash: str
    object_hash: str
    event_count: int
    uncompressed_size_bytes: int
    compressed_size_bytes: int
    id: str = field(default_factory=lambda: new_id("archive"))
    archive_type: str = "EVENT_SEGMENT"
    codec: str = "gzip"
    media_type: str = "application/gzip"
    first_event_id: str = ""
    last_event_id: str = ""
    first_created_at: float = 0.0
    last_created_at: float = 0.0
    rule_name: str = ""
    backup_class: str = "STANDARD"
    status: ArchiveStatus = ArchiveStatus.ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)
    last_verified_at: float | None = None
    restored_at: float | None = None
    created_at: float = field(default_factory=now_epoch)
    updated_at: float = field(default_factory=now_epoch)
    schema_version: int = 1


@dataclass
class MemoryRecord:
    tenant_id: str
    task_id: str
    memory_type: MemoryType
    title: str
    preview_text: str
    id: str = field(default_factory=lambda: new_id("memory"))
    run_id: str = ""
    step_id: str = ""
    status: MemoryStatus = MemoryStatus.ACTIVE
    importance: float = 0.5
    source_event_id: str = ""
    artifact_id: str = ""
    source_range: SourceRange = field(default_factory=SourceRange)
    content_hash: str = ""
    token_count: int = 0
    phase: str = ""
    facets: list[str] = field(default_factory=list)
    milvus_collection: str = ""
    milvus_primary_key: str = ""
    indexed_at: float | None = None
    created_at: float = field(default_factory=now_epoch)
    updated_at: float = field(default_factory=now_epoch)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash_text(self.preview_text)


@dataclass
class CheckpointRecord:
    tenant_id: str
    task_id: str
    run_id: str
    state_version: int
    current_step_id: str
    memory_mode: MemoryMode
    snapshot: dict[str, Any]
    id: str = field(default_factory=lambda: new_id("checkpoint"))
    content_hash: str = ""
    created_at: float = field(default_factory=now_epoch)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = content_hash_text(stable_json(self.snapshot))


@dataclass
class OutboxRecord:
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: new_id("outbox"))
    status: str = "PENDING"
    retry_count: int = 0
    next_retry_at: float = 0.0
    created_at: float = field(default_factory=now_epoch)
    processed_at: float | None = None
    claim_token: str = ""


class OutboxClaimLostError(RuntimeError):
    """The outbox row was reclaimed after this consumer's lease expired."""


@dataclass
class ContextItem:
    item_id: str
    item_type: str
    text: str
    priority: int
    required: bool = False
    estimated_tokens: int = 0
    source_event_id: str = ""
    artifact_id: str = ""
    source_range: SourceRange | None = None
    content_hash: str = ""
    status: str = "ACTIVE"
    reason_loaded: str = ""
    facets: tuple[str, ...] = ()

    def dedup_key(self) -> tuple[str, str, str]:
        return (self.item_id, self.source_event_id, self.content_hash)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.source_range is not None:
            data["source_range"] = self.source_range.to_dict()
        return data


@dataclass
class ContextPacket:
    memory_mode: MemoryMode
    system_context: list[ContextItem] = field(default_factory=list)
    task_state: list[ContextItem] = field(default_factory=list)
    current_step: list[ContextItem] = field(default_factory=list)
    mandatory_dependencies: list[ContextItem] = field(default_factory=list)
    recent_messages: list[ContextItem] = field(default_factory=list)
    working_set: list[ContextItem] = field(default_factory=list)
    supplemental_memories: list[ContextItem] = field(default_factory=list)
    loaded_raw_spans: list[ContextItem] = field(default_factory=list)
    artifact_manifest: list[dict[str, Any]] = field(default_factory=list)
    estimated_input_tokens: int = 0
    token_budget: int = 0
    omitted_items: list[dict[str, Any]] = field(default_factory=list)
    source_references: list[dict[str, Any]] = field(default_factory=list)
    debug_metadata: dict[str, Any] = field(default_factory=dict)

    def all_items(self) -> list[ContextItem]:
        return [
            *self.system_context,
            *self.task_state,
            *self.current_step,
            *self.mandatory_dependencies,
            *self.recent_messages,
            *self.working_set,
            *self.supplemental_memories,
            *self.loaded_raw_spans,
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_mode": self.memory_mode.value,
            "system_context": [item.to_dict() for item in self.system_context],
            "task_state": [item.to_dict() for item in self.task_state],
            "current_step": [item.to_dict() for item in self.current_step],
            "mandatory_dependencies": [
                item.to_dict() for item in self.mandatory_dependencies
            ],
            "recent_messages": [item.to_dict() for item in self.recent_messages],
            "working_set": [item.to_dict() for item in self.working_set],
            "supplemental_memories": [
                item.to_dict() for item in self.supplemental_memories
            ],
            "loaded_raw_spans": [item.to_dict() for item in self.loaded_raw_spans],
            "artifact_manifest": list(self.artifact_manifest),
            "estimated_input_tokens": self.estimated_input_tokens,
            "token_budget": self.token_budget,
            "omitted_items": list(self.omitted_items),
            "source_references": list(self.source_references),
            "debug_metadata": dict(self.debug_metadata),
        }


@dataclass
class ToolResultEnvelope:
    result_id: str
    tool_name: str
    status: str
    summary: str
    statistics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    sample: list[Any] = field(default_factory=list)
    artifact_id: str = ""
    content_hash: str = ""
    truncated: bool = False
    source_event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_text(self) -> str:
        return "[Tool result envelope]\n" + json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        )


@dataclass(frozen=True)
class MemorySearchRequest:
    tenant_id: str
    task_id: str
    query: str
    statuses: tuple[str, ...] = (MemoryStatus.ACTIVE.value, MemoryStatus.OPEN.value)
    memory_types: tuple[str, ...] = ()
    phase: str = ""
    run_id: str = ""
    candidate_limit: int = 40
    required_facets: tuple[str, ...] = ()
    excluded_facets: tuple[str, ...] = MEMORY_NAVIGATION_TOOL_NAMES


@dataclass
class MemoryHit:
    record: MemoryRecord
    score: float
    lexical_score: float = 0.0
    vector_score: float = 0.0
    reason: str = ""


@dataclass
class PreflightAuditResult:
    allowed: bool
    task_id: str
    step_id: str
    state_version: int
    required_count: int
    resolved_count: int
    missing_dependency_ids: list[str] = field(default_factory=list)
    unresolved_source_ids: list[str] = field(default_factory=list)
    superseded_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def coverage_ratio(self) -> float:
        if self.required_count == 0:
            return 1.0
        return self.resolved_count / self.required_count


@dataclass(frozen=True)
class RetentionRule:
    name: str
    retention_seconds: int | None = None
    archive_after_seconds: int | None = None
    legal_hold: bool = False
    deletion_mode: str = "TOMBSTONE"
    backup_class: str = "STANDARD"
    tenant_ids: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()
    memory_types: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ()
    sensitivity_levels: tuple[str, ...] = ()
    task_statuses: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskRetentionPolicy:
    tenant_id: str
    task_id: str
    retention_until: float | None = None
    legal_hold: bool = False
    deletion_mode: str = "HARD_DELETE"
    set_by: str = "system"
    updated_at: float = field(default_factory=now_epoch)


def unique_strings(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))
