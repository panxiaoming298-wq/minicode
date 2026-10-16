"""Ports used by the memory domain and context builder."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from .models import (
    AgentEvent,
    ArchiveManifest,
    ArtifactRecord,
    CheckpointRecord,
    ContextItem,
    DecisionRecord,
    DependencyRecord,
    MemoryHit,
    MemoryRecord,
    MemorySearchRequest,
    TaskRetentionPolicy,
    TaskStateSnapshot,
)


class AuthorityStore(Protocol):
    def initialize(self) -> None: ...
    def close(self) -> None: ...
    def health_check(self) -> bool: ...
    def sync_history(self, *, tenant_id: str, task_id: str, stream: str,
                     messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict], dict[int, Mapping[str, Any]]]: ...
    def history_migration(self, *, tenant_id: str, task_id: str, stream: str, key: str,
                          proof: Mapping[str, Any] | None = None) -> dict | None: ...

    def ensure_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        goal: str,
        workspace_id: str = "",
        user_id: str = "",
        cross_session_required: bool = False,
        restart_recovery_required: bool = False,
    ) -> None: ...
    def create_run(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str,
        mode: str,
        model_name: str = "",
        model_context_limit: int = 0,
    ) -> None: ...
    def update_run_mode(
        self, *, tenant_id: str, task_id: str, run_id: str, mode: str
    ) -> None: ...
    def upsert_step(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        sequence_no: int,
        title: str = "",
        description: str = "",
        status: str = "RUNNING",
        acceptance_criteria: Mapping[str, Any] | None = None,
        parent_step_id: str = "",
    ) -> None: ...
    def get_step(
        self, *, tenant_id: str, task_id: str, step_id: str
    ) -> Mapping[str, Any] | None: ...

    def persist_event(
        self,
        event: AgentEvent,
        *,
        state_patch: Mapping[str, Any] | None = None,
        artifact: ArtifactRecord | None = None,
        memory: MemoryRecord | None = None,
        memories: Sequence[MemoryRecord] = (),
        decision: DecisionRecord | None = None,
        checkpoint: CheckpointRecord | None = None,
        step_status: str = "",
    ) -> tuple[AgentEvent, TaskStateSnapshot | None]: ...
    def get_event(
        self, *, tenant_id: str, task_id: str, event_id: str
    ) -> AgentEvent | None: ...
    def history_event_page(self, *, tenant_id: str, task_id: str, snapshot: int | None = None,
                           position: int | None = None, recent_first: bool = True, role: str = "",
                           tool_name: str = "", run_id: str = "", limit: int = 100
                           ) -> tuple[int, list[tuple[int, AgentEvent]], bool]: ...
    def get_event_by_idempotency(
        self, *, tenant_id: str, task_id: str, idempotency_key: str
    ) -> AgentEvent | None: ...
    def tool_result_events(
        self, *, tenant_id: str, task_id: str, call_ids: Sequence[str]
    ) -> list[AgentEvent]: ...
    def recent_events(
        self,
        *,
        tenant_id: str,
        task_id: str,
        limit: int = 100,
        event_types: Sequence[str] = (),
    ) -> list[AgentEvent]: ...
    def events_in_range(
        self,
        *,
        tenant_id: str,
        task_id: str,
        after: float | None = None,
        before: float | None = None,
        limit: int = 100_000,
        event_types: Sequence[str] = (),
        exclude_event_types: Sequence[str] = (),
    ) -> list[AgentEvent]: ...

    def get_state(
        self, *, tenant_id: str, task_id: str
    ) -> TaskStateSnapshot | None: ...
    def compare_and_set_state(
        self, state: TaskStateSnapshot, *, expected_version: int
    ) -> TaskStateSnapshot: ...

    def put_decision(self, decision: DecisionRecord) -> DecisionRecord: ...
    def active_decisions(
        self, *, tenant_id: str, task_id: str, decision_key: str = ""
    ) -> list[DecisionRecord]: ...
    def decision_history(
        self, *, tenant_id: str, task_id: str, decision_key: str
    ) -> list[DecisionRecord]: ...

    def add_dependency(self, dependency: DependencyRecord) -> DependencyRecord: ...
    def dependencies_for_step(
        self, *, tenant_id: str, task_id: str, step_id: str
    ) -> list[DependencyRecord]: ...
    def all_dependencies(
        self, *, tenant_id: str, task_id: str
    ) -> list[DependencyRecord]: ...

    def get_artifact(
        self, *, tenant_id: str, task_id: str, artifact_id: str
    ) -> ArtifactRecord | None: ...
    def list_artifacts(
        self, *, tenant_id: str, task_id: str, statuses: Sequence[str] = ()
    ) -> list[ArtifactRecord]: ...
    def all_artifacts(
        self, *, tenant_id: str | None = None, task_id: str | None = None
    ) -> list[ArtifactRecord]: ...
    def update_artifact_status(
        self,
        *,
        tenant_id: str,
        task_id: str,
        artifact_id: str,
        status: str,
        uri: str | None = None,
    ) -> None: ...

    def put_memory(
        self, memory: MemoryRecord
    ) -> MemoryRecord: ...
    def get_memories(
        self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str]
    ) -> list[MemoryRecord]: ...
    def search_memories_lexical(
        self, request: MemorySearchRequest
    ) -> list[MemoryHit]: ...
    def verify_active_memories(
        self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str]
    ) -> list[MemoryRecord]: ...
    def all_memories(
        self, *, tenant_id: str | None = None, task_id: str | None = None
    ) -> list[MemoryRecord]: ...
    def maintain_memories(self, *, tenant_id: str, task_id: str, now: float) -> dict[str, Any]: ...
    def touch_memories(self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str], now: float) -> None: ...


    def put_checkpoint(self, checkpoint: CheckpointRecord) -> CheckpointRecord: ...
    def get_checkpoint(
        self, *, tenant_id: str, task_id: str, checkpoint_id: str
    ) -> CheckpointRecord | None: ...
    def latest_checkpoint(
        self, *, tenant_id: str, task_id: str
    ) -> CheckpointRecord | None: ...
    def all_checkpoints(
        self, *, tenant_id: str, task_id: str
    ) -> list[CheckpointRecord]: ...

    def set_retention_policy(
        self, policy: TaskRetentionPolicy
    ) -> TaskRetentionPolicy: ...
    def get_retention_policy(
        self, *, tenant_id: str, task_id: str
    ) -> TaskRetentionPolicy | None: ...

    def put_archive_manifest(self, manifest: ArchiveManifest) -> ArchiveManifest: ...
    def get_archive_manifest(
        self, *, tenant_id: str, task_id: str, archive_id: str
    ) -> ArchiveManifest | None: ...
    def list_archive_manifests(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> list[ArchiveManifest]: ...

    def referenced_artifact_ids(self, *, tenant_id: str | None = None) -> set[str]: ...
    def artifact_reference_exists(
        self, *, tenant_id: str, artifact_id: str, uri: str
    ) -> bool: ...
    def hard_delete_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        request_id: str = "",
        actor_hash: str = "",
        reason_hash: str = "",
    ) -> dict[str, int]: ...
    def get_deletion_audit(self, *, request_id: str) -> Mapping[str, Any] | None: ...


class ArtifactStore(Protocol):
    def put_bytes(
        self,
        data: bytes,
        *,
        artifact_id: str = "",
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        media_type: str,
        artifact_type: str,
    ) -> ArtifactRecord: ...
    def get_bytes(self, artifact: ArtifactRecord) -> bytes: ...
    def read_range(
        self,
        artifact: ArtifactRecord,
        *,
        start: int,
        end: int,
        unit: str = "BYTE",
        query: str = "",
        log_level: str = "",
    ) -> bytes: ...
    def delete(self, artifact: ArtifactRecord) -> None: ...
    def list_objects(self) -> Iterable[tuple[str, float]]: ...
    def health_check(self) -> bool: ...
    def close(self) -> None: ...


class WorkingSetStore(Protocol):
    def get(self, *, tenant_id: str, task_id: str, phase: str) -> list[str]: ...
    def touch(
        self,
        *,
        tenant_id: str,
        task_id: str,
        phase: str,
        memory_ids: Sequence[str],
        state_version: int,
    ) -> None: ...
    def invalidate(
        self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str]
    ) -> None: ...


class ContextPacker(Protocol):
    def pack(
        self, items: Sequence[ContextItem], *, token_budget: int
    ) -> tuple[list[ContextItem], list[dict[str, Any]]]: ...
