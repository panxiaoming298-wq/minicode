"""Runtime coordination for tiered context and lossless authoritative memory.

This module is the only component the agent loop needs to know about.  It
coordinates the relational authority, artifact persistence, derived vector
index and context builder while keeping every derived summary linked to raw
source material.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import asdict
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .artifacts import (
    ArtifactIntegrityError,
    ArtifactStoreUnavailable,
    _select_bytes,
    build_artifact_store,
    build_encrypted_spool,
)
from .chunking import artifact_chunk_records, chunk_utf8_text
from .config import PagedMemoryConfig
from .context_builder import PagedContextBuilder
from .indexer import OutboxIndexer, make_memory_index_outbox
from .models import (
    MEMORY_NAVIGATION_TOOL_NAMES,
    AgentEvent,
    ArtifactRecord,
    ArtifactStatus,
    CheckpointRecord,
    ContextPacket,
    DecisionRecord,
    DependencyRecord,
    DependencyType,
    EventType,
    MemoryMode,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryType,
    OutboxRecord,
    PreflightAuditResult,
    SourceRange,
    StorageHealth,
    TaskMetrics,
    TaskRetentionPolicy,
    ToolResultEnvelope,
    apply_transactional_state_patch,
    content_hash_bytes,
    content_hash_text,
    new_id,
    now_epoch,
    stable_json,
)
from .policy import MemoryModePolicy, TokenBudgetManager
from .postgres_store import PostgresMemoryStore
from .retrieval import (
    DependencyResolver,
    HybridMemoryRetriever,
    WorkingSetManager,
    merge_state_dependencies,
)
from .sqlite_store import SqliteMemoryStore, StateVersionConflict
from .vector_index import build_vector_index, configured_index_descriptor

logger = logging.getLogger(__name__)


class PagedMemoryError(RuntimeError):
    """Base class for errors that have explicit execution semantics."""


class AuthorityUnavailableError(PagedMemoryError):
    """The authoritative store cannot durably accept or verify an operation."""


class ArtifactPersistenceRequired(PagedMemoryError):
    """A large result could not be durably stored and therefore cannot be trimmed."""


class ContextBudgetExceeded(PagedMemoryError):
    """The exact provider request cannot fit without dropping required source."""

    def __init__(self, *, estimated_tokens: int, input_limit: int) -> None:
        self.estimated_tokens = estimated_tokens
        self.input_limit = input_limit
        super().__init__(
            "lossless context request exceeds the hard model input budget: "
            f"estimated {estimated_tokens} tokens, limit {input_limit}"
        )


class PreflightBlockedError(PagedMemoryError):
    def __init__(self, result: PreflightAuditResult) -> None:
        self.result = result
        details = "; ".join(result.errors) or "required evidence is incomplete"
        super().__init__(f"memory preflight blocked step {result.step_id}: {details}")


def _message_content(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)


def _safe_identifier(value: str, *, fallback: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "_.-" else "_" for char in value
    )
    return (cleaned.strip("._-") or fallback)[:160]


def _stable_artifact_id(idempotency_key: str) -> str:
    """Map one logical tool result to one retry-safe object identity."""
    digest = content_hash_text(idempotency_key).removeprefix("sha256:")
    return f"artifact_{digest[:32]}"


def _latest_user_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _message_content(message)
    return ""


class PagedMemoryRuntime:
    """Coordinates durable writes, retrieval, paging and fail-closed audits.

    ``enabled`` controls whether the generated context is sent to the model.
    ``shadow_write`` exercises persistence and packet construction but returns
    the legacy message list unchanged.  Either flag activates event capture.
    """

    def __init__(
        self,
        *,
        config: PagedMemoryConfig,
        cwd: str | Path,
        task_id: str,
        goal: str,
        model_name: str = "",
        model_context_limit: int = 128_000,
        authority: Any | None = None,
        artifact_store: Any | None = None,
        vector_index: Any | None = None,
        spool_store: Any | None = None,
        run_id: str | None = None,
        step_namespace: str = "",
    ) -> None:
        self.config = config
        self.cwd = Path(cwd).resolve()
        self.tenant_id = config.tenant_id
        self.task_id = _safe_identifier(task_id, fallback=new_id("task"))
        self.run_id = _safe_identifier(run_id or new_id("run"), fallback=new_id("run"))
        self.step_namespace = (
            _safe_identifier(step_namespace, fallback="") if step_namespace else ""
        )
        self.goal = goal
        self.model_name = model_name
        self.model_context_limit = max(1_024, int(model_context_limit or 128_000))
        self.current_mode = config.manual_mode or config.default_mode
        self.storage_health = StorageHealth.NORMAL
        self.metrics = TaskMetrics()
        self.last_packet: ContextPacket | None = None
        self.current_step_id = ""
        self._step_sequence = 0
        self._step_finalized = True
        self._last_checkpoint: CheckpointRecord | None = None
        self._operational_metrics: dict[str, float | int] = {
            "page_fault_count": 0,
            "raw_page_tokens": 0,
            "artifact_read_latency_ms": 0.0,
            "artifact_upload_failure_count": 0,
            "source_pointer_resolution_failure_count": 0,
            "outbox_retry_count": 0,
            "tool_result_original_size": 0,
            "tool_result_prompt_size": 0,
            "output_tokens": 0,
            "authority_write_latency_ms": 0.0,
            "task_state_version_conflict": 0,
        }
        self._lock = threading.RLock()
        self._owns_authority = authority is None
        self._owns_artifact_store = artifact_store is None
        self._owns_vector_index = vector_index is None
        self._closed = False

        try:
            self.authority = authority or self._build_authority(config)
            self.authority.initialize()
            if not self.authority.health_check():
                raise AuthorityUnavailableError(
                    "authoritative memory store health check failed"
                )
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            if isinstance(exc, AuthorityUnavailableError):
                raise
            raise AuthorityUnavailableError(
                "authoritative memory store is unavailable"
            ) from exc

        self.artifact_store = artifact_store or build_artifact_store(config)
        self.spool_store = spool_store
        if self.spool_store is None:
            try:
                self.spool_store = build_encrypted_spool(config)
            except (RuntimeError, ValueError) as exc:
                # A misconfigured optional spool must not make the normal store
                # unusable; an outage will still fail closed rather than truncate.
                logger.warning("Encrypted artifact spool disabled: %s", exc)
                self.spool_store = None
        self.vector_index = vector_index
        self.index_descriptor = configured_index_descriptor(config)
        if self.vector_index is None and config.milvus_enabled:
            try:
                self.vector_index = build_vector_index(config)
            except Exception as exc:  # noqa: BLE001 - optional SDK errors vary by adapter.
                logger.warning(
                    "Milvus unavailable; authority lexical retrieval remains active: %s",
                    exc,
                )
                self.vector_index = None
                self.storage_health = StorageHealth.INDEX_DEGRADED
        if self.vector_index is not None:
            descriptor = getattr(self.vector_index, "index_descriptor", None)
            if descriptor is not None:
                self.index_descriptor = descriptor

        self.token_budget = TokenBudgetManager(model_name=model_name)
        self.mode_policy = MemoryModePolicy(config)
        self.dependency_resolver = DependencyResolver(
            self.authority,
            max_depth=config.dependency_max_depth,
            source_validator=self._dependency_source_is_resolvable,
        )
        self.working_set = WorkingSetManager(self.authority)
        self.retriever = HybridMemoryRetriever(self.authority, self.vector_index)
        self.context_builder = PagedContextBuilder(
            authority=self.authority,
            config=config,
            mode_policy=self.mode_policy,
            token_budget=self.token_budget,
            retriever=self.retriever,
            dependency_resolver=self.dependency_resolver,
            working_set=self.working_set,
        )
        try:
            self.authority.ensure_task(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                goal=goal,
                workspace_id=config.workspace_id,
                user_id=config.user_id,
                cross_session_required=self.metrics.cross_session_required,
                restart_recovery_required=self.metrics.restart_recovery_required,
            )
            self.authority.create_run(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                run_id=self.run_id,
                mode=self.current_mode.value,
                model_name=model_name,
                model_context_limit=self.model_context_limit,
            )
            if (
                self.authority.get_retention_policy(
                    tenant_id=self.tenant_id, task_id=self.task_id
                )
                is None
            ):
                retention_until = (
                    now_epoch() + config.retention_default_days * 86400
                    if config.retention_default_days > 0
                    else None
                )
                self.authority.set_retention_policy(
                    TaskRetentionPolicy(
                        tenant_id=self.tenant_id,
                        task_id=self.task_id,
                        retention_until=retention_until,
                        legal_hold=config.legal_hold_default,
                        set_by="runtime-default",
                    )
                )
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            self.close()
            raise AuthorityUnavailableError(
                "failed to create authoritative task/run"
            ) from exc

        if self.storage_health != StorageHealth.NORMAL:
            self._record_health_event(
                self.storage_health, "startup dependency unavailable"
            )

    @property
    def active(self) -> bool:
        return self.config.enabled or self.config.shadow_write

    @property
    def controls_context(self) -> bool:
        return self.config.enabled

    @classmethod
    def from_runtime(
        cls,
        *,
        runtime: Mapping[str, Any] | None,
        cwd: str | Path,
        messages: Sequence[Mapping[str, Any]],
        session: Any | None = None,
        model_name: str = "",
        model_context_limit: int = 128_000,
    ) -> PagedMemoryRuntime | None:
        runtime_values: Mapping[str, Any] = runtime or {}
        config = PagedMemoryConfig.from_sources(runtime_values, cwd=cwd)
        if not (config.enabled or config.shadow_write):
            return None
        session_id = str(
            getattr(session, "session_id", "") or getattr(session, "id", "")
        )
        goal = _latest_user_text(messages)
        explicit_task_id = str(
            runtime_values.get("_memoryTaskId")
            or runtime_values.get("memoryTaskId")
            or ""
        ).strip()
        if explicit_task_id:
            task_id = explicit_task_id
        elif session_id:
            task_id = (
                session_id if session_id.startswith("task_") else f"task_{session_id}"
            )
        else:
            # Prompt hashes merge unrelated executions that happen to ask the
            # same thing.  Allocate one task identity and retain it on the
            # mutable runtime object so all turns/Workers sharing that runtime
            # converge on the same authority scope.
            task_id = new_id("task")
            if isinstance(runtime, MutableMapping):
                runtime["_memoryTaskId"] = task_id
        configured_run_id = str(
            runtime_values.get("_runtimeActiveRunId")
            or runtime_values.get("_memoryRunId")
            or ""
        ).strip()
        instance: PagedMemoryRuntime | None = None
        try:
            instance = cls(
                config=config,
                cwd=cwd,
                task_id=task_id,
                goal=goal,
                model_name=model_name,
                model_context_limit=model_context_limit,
                run_id=configured_run_id or None,
                step_namespace=str(runtime_values.get("_runtimeNodeId", "")),
            )
            # A new process/session resumes the last verified view
            # automatically; callers should not need a model tool invocation
            # merely to recover mode and progress counters.
            instance.restore_latest_checkpoint()
            if config.manual_mode is not None:
                instance.current_mode = config.manual_mode
            instance.authority.update_run_mode(
                tenant_id=instance.tenant_id,
                task_id=instance.task_id,
                run_id=instance.run_id,
                mode=instance.current_mode.value,
            )

            def runtime_flag(*names: str) -> bool:
                for name in names:
                    value = runtime_values.get(name)
                    if isinstance(value, bool):
                        return value
                    if str(value or "").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }:
                        return True
                return False

            if runtime_values.get("_memoryParallelSubtasks") is not None:
                try:
                    instance.metrics.parallel_subtasks = max(
                        instance.metrics.parallel_subtasks,
                        int(runtime_values.get("_memoryParallelSubtasks", 0) or 0),
                    )
                except (TypeError, ValueError):
                    pass
            instance.metrics.cross_session_required = (
                instance.metrics.cross_session_required
                or runtime_flag(
                    "memoryCrossSessionRequired", "_memoryCrossSessionRequired"
                )
            )
            instance.metrics.restart_recovery_required = (
                instance.metrics.restart_recovery_required
                or runtime_flag(
                    "memoryRestartRecoveryRequired", "_memoryRestartRecoveryRequired"
                )
            )
            instance.metrics.audit_required = (
                instance.metrics.audit_required
                or runtime_flag("memoryAuditRequired", "_memoryAuditRequired")
            )
            instance.authority.ensure_task(
                tenant_id=instance.tenant_id,
                task_id=instance.task_id,
                goal=goal,
                workspace_id=config.workspace_id,
                user_id=config.user_id,
                cross_session_required=instance.metrics.cross_session_required,
                restart_recovery_required=instance.metrics.restart_recovery_required,
            )
            instance.record_initial_messages(messages)
            return instance
        except Exception as exc:
            if instance is not None:
                instance.close()
            if config.shadow_write and not config.enabled:
                logger.warning(
                    "Paged-memory shadow path disabled after startup failure: %s", exc
                )
                return None
            raise

    @staticmethod
    def _build_authority(config: PagedMemoryConfig) -> Any:
        if config.backend == "postgres":
            return PostgresMemoryStore(config.database_url)
        return SqliteMemoryStore(config.sqlite_path)

    def _persist_event(
        self,
        event: AgentEvent,
        *,
        state_patch: Mapping[str, Any] | None = None,
        artifact: ArtifactRecord | None = None,
        memory: MemoryRecord | None = None,
        outbox: OutboxRecord | None = None,
        memories: Sequence[MemoryRecord] = (),
        outboxes: Sequence[OutboxRecord] = (),
        decision: DecisionRecord | None = None,
        checkpoint: CheckpointRecord | None = None,
        step_status: str = "",
    ) -> tuple[AgentEvent, Any | None]:
        if self._closed:
            raise AuthorityUnavailableError("paged memory runtime is closed")
        write_started = time.perf_counter()
        try:
            result = self.authority.persist_event(
                event,
                state_patch=state_patch,
                artifact=artifact,
                memory=memory,
                outbox=outbox,
                memories=memories,
                outboxes=outboxes,
                decision=decision,
                checkpoint=checkpoint,
                step_status=step_status,
            )
        except StateVersionConflict as exc:
            self._operational_metrics["task_state_version_conflict"] += 1
            self.storage_health = StorageHealth.RECOVERING
            raise AuthorityUnavailableError(
                "authoritative state changed concurrently; state advancement is blocked"
            ) from exc
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            raise AuthorityUnavailableError(
                "authoritative write failed; state advancement is blocked"
            ) from exc
        finally:
            self._operational_metrics["authority_write_latency_ms"] = (
                time.perf_counter() - write_started
            ) * 1_000
        return result

    def _idempotent_event(self, idempotency_key: str) -> AgentEvent | None:
        try:
            return self.authority.get_event_by_idempotency(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            raise AuthorityUnavailableError(
                "authoritative idempotency lookup failed; operation is blocked"
            ) from exc

    def _memory_from_source_event(
        self,
        event_id: str,
        *,
        memory_type: MemoryType | None = None,
    ) -> MemoryRecord:
        try:
            memories = self.authority.all_memories(
                tenant_id=self.tenant_id, task_id=self.task_id
            )
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            raise AuthorityUnavailableError(
                "authoritative memory lookup failed; operation is blocked"
            ) from exc
        match = next(
            (
                memory
                for memory in memories
                if memory.source_event_id == event_id
                and (memory_type is None or memory.memory_type == memory_type)
            ),
            None,
        )
        if match is None:
            raise ArtifactIntegrityError(
                f"idempotent event {event_id} has no committed memory projection"
            )
        return match

    def _decision_from_source_event(
        self, *, decision_key: str, event_id: str
    ) -> DecisionRecord:
        try:
            decisions = self.authority.decision_history(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                decision_key=decision_key,
            )
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            raise AuthorityUnavailableError(
                "authoritative decision lookup failed; operation is blocked"
            ) from exc
        match = next(
            (value for value in decisions if value.source_event_id == event_id), None
        )
        if match is None:
            raise ArtifactIntegrityError(
                f"idempotent event {event_id} has no committed decision projection"
            )
        return match

    def _artifact_from_event(self, event: AgentEvent) -> ArtifactRecord | None:
        if not event.artifact_uri:
            return None
        try:
            artifacts = self.authority.list_artifacts(
                tenant_id=self.tenant_id, task_id=self.task_id
            )
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            raise AuthorityUnavailableError(
                "authoritative artifact lookup failed; operation is blocked"
            ) from exc
        artifact = next(
            (
                value
                for value in artifacts
                if value.source_event_id == event.id and value.uri == event.artifact_uri
            ),
            None,
        )
        if artifact is None:
            raise ArtifactIntegrityError(
                f"event {event.id} points to an unregistered artifact"
            )
        if artifact.status == ArtifactStatus.PENDING_UPLOAD:
            if self.spool_store is None:
                raise ArtifactStoreUnavailable(
                    "pending artifact has no configured encrypted spool"
                )
            data = self.spool_store.get_bytes(artifact)
            if content_hash_bytes(data) != event.content_hash:
                raise ArtifactIntegrityError("pending artifact/event hash mismatch")
        else:
            self._verify_artifact_record(artifact, expected_hash=event.content_hash)
        return artifact

    def _event(
        self,
        event_type: EventType,
        *,
        content_text: str = "",
        content_json: Mapping[str, Any] | None = None,
        step_id: str = "",
        artifact_uri: str = "",
        content_hash: str = "",
        token_count: int = 0,
        caused_by_event_id: str = "",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> AgentEvent:
        return AgentEvent(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            step_id=step_id or self.current_step_id,
            event_type=event_type,
            content_text=content_text,
            content_json=dict(content_json or {}),
            artifact_uri=artifact_uri,
            content_hash=content_hash,
            token_count=token_count,
            caused_by_event_id=caused_by_event_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )

    def _record_health_event(self, health: StorageHealth, reason: str) -> None:
        event = self._event(
            EventType.STORAGE_HEALTH_CHANGED,
            content_text=f"storage health={health.value}: {reason}",
            content_json={"health": health.value, "reason": reason},
            idempotency_key=f"{self.run_id}:health:{health.value}:{content_hash_text(reason)}",
        )
        try:
            self.authority.persist_event(event)
        except Exception:  # noqa: BLE001 - telemetry must not mask the original outage.
            logger.warning(
                "Could not persist storage health transition to %s", health.value
            )

    def _set_health(self, health: StorageHealth, reason: str) -> None:
        if self.storage_health == health:
            return
        self.storage_health = health
        if health != StorageHealth.AUTHORITY_UNAVAILABLE:
            self._record_health_event(health, reason)

    def _index_outbox_for(self, memory: MemoryRecord) -> OutboxRecord | None:
        """Create durable index work even while the optional index is offline."""
        if not self.config.indexer_enabled or not (
            self.config.milvus_enabled or self.vector_index is not None
        ):
            return None
        return make_memory_index_outbox(
            memory,
            index_descriptor=self.index_descriptor,
        )

    def _text_artifact_chunk_records(
        self,
        *,
        output: str,
        event: AgentEvent,
        artifact: ArtifactRecord | None,
        tool_name: str,
        ok: bool,
    ) -> list[MemoryRecord]:
        """Project every byte of a text artifact into searchable source ranges.

        The artifact remains the authority for the original payload.  Chunk
        records contain bounded text used by database FTS and Milvus, plus an
        exact byte range and digest that can be independently verified.
        """

        if artifact is None or not artifact.media_type.casefold().startswith("text/"):
            return []
        chunk_size = max(
            1_024,
            min(7_000, self.config.raw_page_token_limit * 3),
        )
        return artifact_chunk_records(
            text=output,
            event=event,
            artifact=artifact,
            tool_name=tool_name,
            phase=self.metrics.current_phase,
            ok=ok,
            estimate_tokens=self.token_budget.estimate_text,
            max_bytes=chunk_size,
            max_tokens=384,
        )

    def _persist_text_artifact_chunks(
        self,
        *,
        output: str,
        event: AgentEvent,
        artifact: ArtifactRecord | None,
        tool_name: str,
        ok: bool,
    ) -> list[MemoryRecord]:
        """Idempotently repair chunk projections for an earlier commit."""

        stored: list[MemoryRecord] = []
        for memory in self._text_artifact_chunk_records(
            output=output,
            event=event,
            artifact=artifact,
            tool_name=tool_name,
            ok=ok,
        ):
            stored.append(
                self.authority.put_memory(
                    memory,
                    outbox=self._index_outbox_for(memory),
                )
            )
        return stored

    def _message_memory(self, event: AgentEvent, *, role: str) -> MemoryRecord | None:
        """Project a raw conversation event into a source-linked search card."""
        content = event.content_text
        if not content:
            return None
        preview_limit = max(800, min(4_000, self.config.raw_page_token_limit * 4))
        labels = {
            "user": ("User message", 0.80),
            "assistant": ("Model output", 0.60),
            "assistant_progress": ("Model progress", 0.45),
        }
        title, importance = labels.get(role, ("Conversation event", 0.50))
        return MemoryRecord(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            step_id=self.current_step_id,
            memory_type=MemoryType.FACT,
            title=title,
            preview_text=content[:preview_limit],
            status=MemoryStatus.ACTIVE,
            importance=importance,
            source_event_id=event.id,
            source_range=SourceRange(
                unit="BYTE", start=0, end=len(content.encode("utf-8"))
            ),
            content_hash=event.content_hash,
            token_count=event.token_count,
            phase=self.metrics.current_phase,
            facets=["conversation", role],
        )

    def record_initial_messages(self, messages: Sequence[Mapping[str, Any]]) -> None:
        """Idempotently capture the input history before any lossy context operation."""
        role_types = {
            "user": EventType.USER_INPUT,
            "assistant": EventType.MODEL_OUTPUT,
            "assistant_progress": EventType.MODEL_OUTPUT,
            "assistant_tool_call": EventType.TOOL_CALL,
            "tool_result": EventType.TOOL_RESULT,
        }
        for index, message in enumerate(messages):
            role = str(message.get("role", ""))
            event_type = role_types.get(role)
            if event_type is None:
                continue
            content = _message_content(message)
            if role == "assistant_tool_call":
                content = json.dumps(
                    {
                        "toolName": message.get("toolName", ""),
                        "input": message.get("input", {}),
                        "toolUseId": message.get("toolUseId", ""),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            if role == "tool_result" and bool(message.get("isError")):
                event_type = EventType.TOOL_ERROR
            digest = content_hash_text(content)
            event_metadata = {"role": role, "history_index": index}
            if role in {"assistant_tool_call", "tool_result"}:
                event_metadata["tool_name"] = str(message.get("toolName", ""))
                event_metadata["tool_call_id"] = str(message.get("toolUseId", ""))
            event = self._event(
                event_type,
                content_text=content,
                content_json=event_metadata,
                content_hash=digest,
                token_count=self.token_budget.estimate_text(content),
                correlation_id=str(message.get("toolUseId", "")),
                idempotency_key=f"{self.run_id}:history:{index}:{role}:{digest}",
            )
            memory = (
                self._message_memory(event, role=role)
                if role in {"user", "assistant", "assistant_progress"}
                else None
            )
            if memory is not None:
                event.content_json["memory_id"] = memory.id
            self._persist_event(
                event,
                memory=memory,
                outbox=self._index_outbox_for(memory) if memory else None,
            )

    def begin_step(
        self,
        sequence_no: int,
        *,
        user_input: str = "",
        acceptance_criteria: Mapping[str, Any] | None = None,
        phase: str = "EXECUTION",
    ) -> str:
        with self._lock:
            if self.current_step_id and not self._step_finalized:
                self.finalize_step(success=True)
            self._step_sequence = max(self._step_sequence, sequence_no)
            namespace = f".node.{self.step_namespace}" if self.step_namespace else ""
            self.current_step_id = f"{self.run_id}{namespace}.step.{sequence_no}"
            self._step_finalized = False
            try:
                self.authority.upsert_step(
                    tenant_id=self.tenant_id,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    step_id=self.current_step_id,
                    sequence_no=sequence_no,
                    title=f"Agent step {sequence_no}",
                    description=user_input[:1_000],
                    status="RUNNING",
                    acceptance_criteria=acceptance_criteria,
                )
                event = self._event(
                    EventType.STATE_UPDATED,
                    content_text=f"step {sequence_no} started",
                    content_json={"sequence_no": sequence_no, "phase": phase},
                    idempotency_key=f"{self.current_step_id}:begin",
                )
                self._persist_event(
                    event,
                    state_patch={
                        "current_step_id": self.current_step_id,
                        "phase": phase,
                    },
                )
            except AuthorityUnavailableError:
                raise
            except Exception as exc:
                self._set_health(
                    StorageHealth.AUTHORITY_UNAVAILABLE, "step start failed"
                )
                raise AuthorityUnavailableError("could not durably start step") from exc
            return self.current_step_id

    def record_model_step(self, step: Any, *, sequence_no: int) -> AgentEvent:
        content = str(getattr(step, "content", "") or "")
        calls = list(getattr(step, "calls", []) or [])
        payload = {
            "type": str(getattr(step, "type", "")),
            "kind": getattr(step, "kind", None),
            "content_kind": getattr(step, "contentKind", None),
            "tool_call_ids": [str(call.get("id", "")) for call in calls],
        }
        event = self._event(
            EventType.MODEL_OUTPUT,
            content_text=content,
            content_json=payload,
            token_count=self.token_budget.estimate_text(
                content or stable_json(payload)
            ),
            idempotency_key=f"{self.current_step_id}:model:{sequence_no}:{content_hash_text(content or stable_json(payload))}",
        )
        memory = self._message_memory(event, role="assistant")
        if memory is not None:
            event.content_json["memory_id"] = memory.id
        self._persist_event(
            event,
            memory=memory,
            outbox=self._index_outbox_for(memory) if memory else None,
        )
        self._operational_metrics["output_tokens"] = event.token_count
        return event

    def record_tool_call(self, call: Mapping[str, Any]) -> AgentEvent:
        call_id = str(call.get("id", ""))
        material = json.dumps(
            {"toolName": call.get("toolName", ""), "input": call.get("input", {})},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        event = self._event(
            EventType.TOOL_CALL,
            content_text=material,
            content_json={
                "tool_call_id": call_id,
                "tool_name": call.get("toolName", ""),
            },
            correlation_id=call_id,
            token_count=self.token_budget.estimate_text(material),
            idempotency_key=f"{self.current_step_id}:tool-call:{call_id}",
        )
        self._persist_event(event)
        return event

    @staticmethod
    def _tool_summary(
        output: str, *, tool_name: str, ok: bool
    ) -> tuple[str, dict[str, Any], list[str]]:
        lines = output.splitlines()
        non_empty = [line.strip() for line in lines if line.strip()]
        preview = "\n".join(non_empty[:6])[:800]
        sample = [line[:240] for line in non_empty[:3]]
        status = "succeeded" if ok else "failed"
        summary = f"{tool_name} {status}; {len(output.encode('utf-8'))} bytes, {len(lines)} lines"
        if preview:
            summary += f"\n{preview}"
        statistics: dict[str, Any] = {
            "bytes": len(output.encode("utf-8")),
            "characters": len(output),
            "lines": len(lines),
        }
        try:
            parsed = json.loads(output)
            statistics["json_type"] = type(parsed).__name__
            if isinstance(parsed, (list, dict)):
                statistics["json_items"] = len(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        return summary, statistics, sample

    def _observe_tool_envelope(self, *, raw_size: int, prompt_output: str) -> None:
        self._operational_metrics["tool_result_original_size"] = raw_size
        self._operational_metrics["tool_result_prompt_size"] = len(
            prompt_output.encode("utf-8")
        )

    def capture_tool_result(
        self,
        *,
        call: Mapping[str, Any],
        output: str,
        ok: bool,
        force_inline: bool = False,
    ) -> tuple[ToolResultEnvelope, str]:
        """Serialize concurrent receipts before touching one authority connection."""
        with self._lock:
            return self._capture_tool_result(
                call=call,
                output=output,
                ok=ok,
                force_inline=force_inline,
            )

    def _capture_tool_result(
        self,
        *,
        call: Mapping[str, Any],
        output: str,
        ok: bool,
        force_inline: bool = False,
    ) -> tuple[ToolResultEnvelope, str]:
        """Persist a real tool receipt before returning a compact prompt value.

        Large outputs are never shortened unless their exact bytes first exist
        in the primary artifact store or the authenticated encrypted spool.
        """
        call_id = str(call.get("id", ""))
        tool_name = str(call.get("toolName", "unknown"))
        is_navigation_receipt = tool_name in MEMORY_NAVIGATION_TOOL_NAMES
        raw = output.encode("utf-8")
        digest = content_hash_bytes(raw)
        summary, statistics, sample = self._tool_summary(
            output, tool_name=tool_name, ok=ok
        )
        event_type = EventType.TOOL_RESULT if ok else EventType.TOOL_ERROR
        idempotency_key = f"{self.current_step_id}:tool-result:{call_id}:{digest}"
        artifact_object_id = _stable_artifact_id(idempotency_key)
        existing_event = self._idempotent_event(idempotency_key)
        if existing_event is not None:
            if (
                existing_event.event_type != event_type
                or existing_event.content_hash != digest
            ):
                raise ArtifactIntegrityError(
                    "idempotency key resolves to a different tool result"
                )
            existing_artifact = self._artifact_from_event(existing_event)
            if not is_navigation_receipt:
                self._persist_text_artifact_chunks(
                    output=output,
                    event=existing_event,
                    artifact=existing_artifact,
                    tool_name=tool_name,
                    ok=ok,
                )
            envelope = ToolResultEnvelope(
                result_id=call_id or existing_event.id,
                tool_name=tool_name,
                status="SUCCESS" if ok else "ERROR",
                summary=summary,
                statistics=statistics,
                warnings=(
                    ["artifact is pending upload from encrypted local spool"]
                    if existing_artifact
                    and existing_artifact.status == ArtifactStatus.PENDING_UPLOAD
                    else []
                ),
                sample=sample,
                artifact_id=existing_artifact.id if existing_artifact else "",
                content_hash=digest,
                truncated=bool(existing_artifact),
                source_event_id=existing_event.id,
            )
            prompt_output = envelope.prompt_text() if existing_artifact else output
            self._observe_tool_envelope(raw_size=len(raw), prompt_output=prompt_output)
            return envelope, prompt_output
        event = self._event(
            event_type,
            content_hash=digest,
            token_count=self.token_budget.estimate_text(output),
            correlation_id=call_id,
            idempotency_key=idempotency_key,
        )
        artifact: ArtifactRecord | None = None
        is_large = not force_inline and len(raw) >= self.config.artifact_large_bytes
        if is_large:
            try:
                artifact = self.artifact_store.put_bytes(
                    raw,
                    artifact_id=artifact_object_id,
                    tenant_id=self.tenant_id,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    step_id=self.current_step_id,
                    media_type="text/plain; charset=utf-8",
                    artifact_type="TOOL_RESULT",
                )
                self._verify_artifact_record(artifact, expected_hash=digest)
            except Exception as primary_error:
                self._operational_metrics["artifact_upload_failure_count"] += 1
                self._set_health(StorageHealth.ARTIFACT_DEGRADED, str(primary_error))
                if self.spool_store is None:
                    raise ArtifactPersistenceRequired(
                        "large tool result could not be durably stored; step is paused"
                    ) from primary_error
                try:
                    artifact = self.spool_store.put_bytes(
                        raw,
                        artifact_id=artifact_object_id,
                        tenant_id=self.tenant_id,
                        task_id=self.task_id,
                        run_id=self.run_id,
                        step_id=self.current_step_id,
                        media_type="text/plain; charset=utf-8",
                        artifact_type="TOOL_RESULT",
                    )
                    artifact.metadata.update(
                        {
                            "sequence_no": self._step_sequence,
                            "correlation_id": call_id,
                            "idempotency_key": idempotency_key,
                            "source_spool_uri": artifact.uri,
                        }
                    )
                    self._verify_artifact_record(
                        artifact, expected_hash=digest, read_back=False
                    )
                except Exception as spool_error:
                    raise ArtifactPersistenceRequired(
                        "large tool result could not be stored in primary or encrypted spool"
                    ) from spool_error
            artifact.source_event_id = event.id
            event.content_text = summary
            event.artifact_uri = artifact.uri
        else:
            event.content_text = output

        event.content_json = {
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "ok": ok,
            "statistics": statistics,
            "full_content_location": "artifact" if artifact else "event",
            "searchable_memory": not is_navigation_receipt,
        }
        memory: MemoryRecord | None = None
        outbox: OutboxRecord | None = None
        if not is_navigation_receipt:
            preview_text = summary
            if artifact is None:
                preview_bytes = max(
                    800,
                    min(8_000, self.config.raw_page_token_limit * 4),
                )
                inline_chunks = chunk_utf8_text(
                    output,
                    max_bytes=max(256, preview_bytes),
                )
                preview_text = inline_chunks[0].text if inline_chunks else summary
            memory = MemoryRecord(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                run_id=self.run_id,
                step_id=self.current_step_id,
                memory_type=MemoryType.TOOL_RESULT if ok else MemoryType.ERROR,
                title=f"{tool_name} {'result' if ok else 'error'}",
                preview_text=preview_text,
                status=MemoryStatus.ACTIVE if ok else MemoryStatus.OPEN,
                importance=0.70 if ok else 0.90,
                source_event_id=event.id,
                artifact_id=artifact.id if artifact else "",
                source_range=SourceRange(unit="BYTE", start=0, end=len(raw)),
                content_hash=digest,
                token_count=event.token_count,
                phase=self.metrics.current_phase,
                facets=["tool_result", tool_name, "success" if ok else "error"],
            )
            outbox = self._index_outbox_for(memory)
        state_patch: dict[str, Any] = {}
        if artifact and not is_navigation_receipt:
            state_patch["_active_artifacts_append"] = [artifact.id]
        if not ok and not is_navigation_receipt:
            state_patch["_open_errors_append"] = [
                {
                    "event_id": event.id,
                    "tool": tool_name,
                    "step_id": self.current_step_id,
                    "content_hash": digest,
                }
            ]
        chunk_memories = (
            self._text_artifact_chunk_records(
                output=output,
                event=event,
                artifact=artifact,
                tool_name=tool_name,
                ok=ok,
            )
            if not is_navigation_receipt
            else []
        )
        chunk_outboxes = [
            candidate
            for chunk in chunk_memories
            if (candidate := self._index_outbox_for(chunk)) is not None
        ]
        persisted_event, _ = self._persist_event(
            event,
            state_patch=state_patch or None,
            artifact=artifact,
            memory=memory,
            outbox=outbox,
            memories=chunk_memories,
            outboxes=chunk_outboxes,
        )
        if persisted_event.id != event.id:
            committed_artifact = self._artifact_from_event(persisted_event)
            if artifact is not None and (
                committed_artifact is None
                or committed_artifact.id != artifact.id
                or committed_artifact.uri != artifact.uri
            ):
                store = (
                    self.spool_store
                    if artifact.status == ArtifactStatus.PENDING_UPLOAD
                    else self.artifact_store
                )
                if store is not None:
                    try:
                        store.delete(artifact)
                    except Exception:  # noqa: BLE001 - later orphan GC retries cleanup.
                        logger.warning(
                            "Could not remove a losing idempotent artifact upload: %s",
                            artifact.uri,
                        )
            if not is_navigation_receipt:
                self._persist_text_artifact_chunks(
                    output=output,
                    event=persisted_event,
                    artifact=committed_artifact,
                    tool_name=tool_name,
                    ok=ok,
                )
            envelope = ToolResultEnvelope(
                result_id=call_id or persisted_event.id,
                tool_name=tool_name,
                status="SUCCESS" if ok else "ERROR",
                summary=summary,
                statistics=statistics,
                warnings=(
                    ["artifact is pending upload from encrypted local spool"]
                    if committed_artifact
                    and committed_artifact.status == ArtifactStatus.PENDING_UPLOAD
                    else []
                ),
                sample=sample,
                artifact_id=committed_artifact.id if committed_artifact else "",
                content_hash=digest,
                truncated=bool(committed_artifact),
                source_event_id=persisted_event.id,
            )
            prompt_output = envelope.prompt_text() if committed_artifact else output
            self._observe_tool_envelope(raw_size=len(raw), prompt_output=prompt_output)
            return envelope, prompt_output
        if artifact is not None:
            self.metrics.large_artifact_count += 1
        self.metrics.cumulative_tool_result_tokens += event.token_count
        envelope = ToolResultEnvelope(
            result_id=call_id or event.id,
            tool_name=tool_name,
            status="SUCCESS" if ok else "ERROR",
            summary=summary,
            statistics=statistics,
            warnings=(
                ["artifact is pending upload from encrypted local spool"]
                if artifact and artifact.status == ArtifactStatus.PENDING_UPLOAD
                else []
            ),
            sample=sample,
            artifact_id=artifact.id if artifact else "",
            content_hash=digest,
            truncated=bool(artifact),
            source_event_id=event.id,
        )
        prompt_output = envelope.prompt_text() if artifact else output
        self._observe_tool_envelope(raw_size=len(raw), prompt_output=prompt_output)
        return envelope, prompt_output

    def finalize_step(self, *, success: bool) -> CheckpointRecord:
        if self._step_finalized and self._last_checkpoint is not None:
            return self._last_checkpoint
        status = "COMPLETED" if success else "FAILED"
        idempotency_key = f"{self.current_step_id}:finalize:{status}"
        existing = self._idempotent_event(idempotency_key)
        if existing is not None:
            checkpoint_id = str(existing.content_json.get("checkpoint_id", ""))
            checkpoint = (
                self.authority.get_checkpoint(
                    tenant_id=self.tenant_id,
                    task_id=self.task_id,
                    checkpoint_id=checkpoint_id,
                )
                if checkpoint_id
                else self.authority.latest_checkpoint(
                    tenant_id=self.tenant_id, task_id=self.task_id
                )
            )
            step = self.authority.get_step(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                step_id=self.current_step_id,
            )
            if checkpoint is None or step is None or step.get("status") != status:
                raise AuthorityUnavailableError(
                    "an incomplete prior step finalization requires repair"
                )
            restored = self.restore_checkpoint(checkpoint.id)
            if restored is None:
                raise AuthorityUnavailableError(
                    "committed checkpoint cannot be restored"
                )
            return restored

        state = self.authority.get_state(tenant_id=self.tenant_id, task_id=self.task_id)
        if state is None:
            raise AuthorityUnavailableError(
                "task state is missing; finalization denied"
            )
        timestamp = now_epoch()
        patch: dict[str, Any] = {"updated_at": timestamp}
        if success:
            # Merge under the authority transaction lock.  Replacing the full
            # list from this stale pre-read can otherwise lose a sibling
            # Worker's completed Step on PostgreSQL.
            patch["_completed_steps_append"] = [self.current_step_id]
        projected_state = apply_transactional_state_patch(state, patch)
        projected_metrics = asdict(self.metrics)
        if success:
            projected_metrics["completed_steps"] = len(projected_state.completed_steps)
        reason = f"step-{status.lower()}"
        checkpoint = CheckpointRecord(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            state_version=projected_state.version,
            current_step_id=self.current_step_id,
            memory_mode=self.current_mode,
            snapshot={
                "schema_version": 1,
                "task_state": projected_state.to_dict(),
                "memory_mode": self.current_mode.value,
                "storage_health": self.storage_health.value,
                "metrics": projected_metrics,
                "reason": reason,
            },
        )
        event = self._event(
            EventType.STATE_UPDATED,
            content_text=f"step {self.current_step_id} {status.lower()}",
            content_json={
                "status": status,
                "checkpoint_id": checkpoint.id,
            },
            idempotency_key=idempotency_key,
        )
        _, committed_state = self._persist_event(
            event,
            state_patch=patch,
            checkpoint=checkpoint,
            step_status=status,
        )
        if success:
            self.metrics.completed_steps = max(
                self.metrics.completed_steps + 1,
                len(committed_state.completed_steps) if committed_state else 0,
            )
        self._step_finalized = True
        self._last_checkpoint = checkpoint
        return checkpoint

    def create_checkpoint(self, *, reason: str = "manual") -> CheckpointRecord:
        if self.storage_health == StorageHealth.AUTHORITY_UNAVAILABLE:
            raise AuthorityUnavailableError(
                "cannot checkpoint without the authority store"
            )
        state = self.authority.get_state(tenant_id=self.tenant_id, task_id=self.task_id)
        if state is None:
            raise AuthorityUnavailableError("task state is missing; checkpoint denied")
        snapshot = {
            "schema_version": 1,
            "task_state": state.to_dict(),
            "memory_mode": self.current_mode.value,
            "storage_health": self.storage_health.value,
            "metrics": asdict(self.metrics),
            "reason": reason,
        }
        checkpoint = CheckpointRecord(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            state_version=state.version,
            current_step_id=self.current_step_id,
            memory_mode=self.current_mode,
            snapshot=snapshot,
        )
        try:
            event = self._event(
                EventType.CHECKPOINT_CREATED,
                content_text=(
                    f"checkpoint={checkpoint.id}; state_version={checkpoint.state_version}; "
                    f"hash={checkpoint.content_hash}"
                ),
                content_json={
                    "checkpoint_id": checkpoint.id,
                    "state_version": checkpoint.state_version,
                    "content_hash": checkpoint.content_hash,
                    "reason": reason,
                },
                content_hash=checkpoint.content_hash,
                idempotency_key=f"{checkpoint.id}:event",
            )
            self._persist_event(event, checkpoint=checkpoint)
        except AuthorityUnavailableError:
            raise
        except Exception as exc:
            self._set_health(StorageHealth.AUTHORITY_UNAVAILABLE, "checkpoint failed")
            raise AuthorityUnavailableError(
                "checkpoint could not be persisted"
            ) from exc
        self._last_checkpoint = checkpoint
        return checkpoint

    @property
    def step_finalized(self) -> bool:
        return self._step_finalized

    def restore_latest_checkpoint(self) -> CheckpointRecord | None:
        return self.restore_checkpoint()

    def restore_checkpoint(
        self, checkpoint_id: str = "", *, preserve_active_step: bool = False
    ) -> CheckpointRecord | None:
        active_step = self.current_step_id
        active_step_finalized = self._step_finalized
        checkpoint = (
            self.authority.get_checkpoint(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                checkpoint_id=checkpoint_id,
            )
            if checkpoint_id
            else self.authority.latest_checkpoint(
                tenant_id=self.tenant_id, task_id=self.task_id
            )
        )
        if checkpoint is None:
            return None
        expected = content_hash_text(stable_json(checkpoint.snapshot))
        if expected != checkpoint.content_hash:
            raise ArtifactIntegrityError("checkpoint content hash mismatch")
        self.current_mode = checkpoint.memory_mode
        self.current_step_id = checkpoint.current_step_id
        metrics = checkpoint.snapshot.get("metrics", {})
        if isinstance(metrics, dict):
            allowed = TaskMetrics.__dataclass_fields__.keys()
            self.metrics = TaskMetrics(
                **{key: value for key, value in metrics.items() if key in allowed}
            )
        self._last_checkpoint = checkpoint
        if preserve_active_step and active_step and not active_step_finalized:
            # A model-visible recovery read must not orphan the currently
            # running Step. It restores the checkpoint view while keeping the
            # live execution boundary that will receive the real tool receipt.
            self.current_step_id = active_step
            self._step_finalized = False
        else:
            self._step_finalized = True
        return checkpoint

    def prepare_model_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        step_id: str | None = None,
        phase_boundary: bool = False,
        required_facets: Sequence[str] = (),
        acceptance_criteria: Mapping[str, Any] | None = None,
        system_context: str = "",
    ) -> tuple[list[dict[str, Any]], ContextPacket]:
        estimated = sum(
            self.token_budget.estimate_message(message) for message in messages
        )
        self.metrics.estimated_input_tokens = estimated
        self.metrics.context_usage_ratio = min(
            2.0, estimated / self.model_context_limit
        )
        self.metrics.current_phase = (
            self.authority.get_state(
                tenant_id=self.tenant_id, task_id=self.task_id
            ).phase
            if self.authority.get_state(tenant_id=self.tenant_id, task_id=self.task_id)
            else self.metrics.current_phase
        )
        context_started = time.perf_counter()
        packet = self.context_builder.build(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            step_id=step_id or self.current_step_id or f"{self.run_id}.step.0",
            user_input=_latest_user_text(messages),
            metrics=self.metrics,
            current_mode=self.current_mode,
            model_context_limit=self.model_context_limit,
            system_context=system_context,
            acceptance_criteria=acceptance_criteria,
            required_facets=required_facets,
            phase_boundary=phase_boundary,
        )
        packet.debug_metadata["context_build_latency_ms"] = (
            time.perf_counter() - context_started
        ) * 1_000
        previous_mode = self.current_mode
        self.current_mode = packet.memory_mode
        self.last_packet = packet
        if packet.memory_mode == MemoryMode.PAGED_MEMORY:
            if self.retriever.last_vector_error or self.vector_index is None:
                self._set_health(
                    StorageHealth.INDEX_DEGRADED,
                    "paged mode rejected or could not use Milvus candidates; "
                    "authority lexical retrieval remains active",
                )
            elif self.storage_health == StorageHealth.INDEX_DEGRADED:
                self._set_health(
                    StorageHealth.NORMAL,
                    "verified Milvus retrieval recovered",
                )
        if previous_mode != self.current_mode:
            self.authority.update_run_mode(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                run_id=self.run_id,
                mode=self.current_mode.value,
            )
            event = self._event(
                EventType.MEMORY_MODE_CHANGED,
                content_text=f"{previous_mode.value} -> {self.current_mode.value}",
                content_json={
                    "from": previous_mode.value,
                    "to": self.current_mode.value,
                    "reason": packet.debug_metadata.get("mode_reason", ""),
                },
                idempotency_key=(
                    f"{self.current_step_id}:mode:{previous_mode.value}:{self.current_mode.value}"
                ),
            )
            self._persist_event(event)
        prepared = (
            self.context_builder.model_messages(packet, messages)
            if self.controls_context
            else [dict(message) for message in messages]
        )
        self.enforce_provider_budget(prepared)
        return prepared, packet

    def enforce_provider_budget(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tool_definitions: Sequence[Any] = (),
    ) -> None:
        """Fail before dispatch when messages plus the real tool surface do not fit.

        ``prepare_model_messages`` first checks with the configured tool reserve.
        The agent loop checks again after dynamic memory tools are added or removed,
        so a large MCP/tool schema cannot bypass the hard context guard.
        """
        message_tokens = sum(
            self.token_budget.estimate_message(message) for message in messages
        )
        serialized_tools: list[dict[str, Any]] = []
        for definition in tool_definitions:
            if isinstance(definition, Mapping):
                name = definition.get("name", "")
                description = definition.get("description", "")
                schema = definition.get(
                    "input_schema", definition.get("parameters", {})
                )
            else:
                name = getattr(definition, "name", "")
                description = getattr(definition, "description", "")
                schema = getattr(definition, "input_schema", {})
            serialized_tools.append(
                {
                    "name": str(name),
                    "description": str(description),
                    "input_schema": schema if isinstance(schema, Mapping) else {},
                }
            )
        tool_schema_tokens = (
            self.token_budget.estimate_text(stable_json(serialized_tools))
            + (4 * len(serialized_tools))
            if serialized_tools
            else 0
        )
        reserved_tool_tokens = max(self.config.tool_token_reserve, tool_schema_tokens)
        provider_input_tokens = message_tokens + reserved_tool_tokens
        input_limit = max(
            0,
            self.model_context_limit
            - self.config.output_token_reserve
            - self.config.safety_token_reserve,
        )
        if self.last_packet is not None:
            self.last_packet.debug_metadata.update(
                {
                    "actual_prepared_input_tokens": message_tokens,
                    "actual_tool_schema_tokens": tool_schema_tokens,
                    "reserved_tool_tokens": reserved_tool_tokens,
                    "actual_provider_input_tokens": provider_input_tokens,
                    "hard_input_token_limit": input_limit,
                }
            )
        if provider_input_tokens > input_limit:
            # Required source, the live request and callable tool schemas are
            # never silently truncated. Stop before the provider call.
            raise ContextBudgetExceeded(
                estimated_tokens=provider_input_tokens,
                input_limit=input_limit,
            )

    def record_decision(
        self,
        *,
        key: str,
        text: str,
        structured_value: Mapping[str, Any] | None = None,
    ) -> DecisionRecord:
        value = dict(structured_value or {})
        decision_digest = content_hash_text(
            stable_json({"key": key, "text": text, "value": value})
        )
        idempotency_key = f"{self.current_step_id}:decision:{key}:{decision_digest}"
        existing_event = self._idempotent_event(idempotency_key)
        if existing_event is not None:
            return self._decision_from_source_event(
                decision_key=key, event_id=existing_event.id
            )
        event = self._event(
            EventType.DECISION_CREATED,
            content_text=text,
            content_json={"decision_key": key, "value": value},
            token_count=self.token_budget.estimate_text(text),
            idempotency_key=idempotency_key,
        )
        decision = DecisionRecord(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            decision_key=key,
            decision_text=text,
            structured_value=value,
            source_event_id=event.id,
        )
        memory = MemoryRecord(
            id=decision.id,
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            step_id=self.current_step_id,
            memory_type=MemoryType.DECISION,
            title=f"Decision: {key}",
            preview_text=text,
            importance=1.0,
            source_event_id=event.id,
            content_hash=event.content_hash,
            token_count=event.token_count,
            phase=self.metrics.current_phase,
            facets=["decision", key],
        )
        outbox = self._index_outbox_for(memory)
        active_decision = {
            "decision_id": decision.id,
            "source_event_id": event.id,
            "status": MemoryStatus.ACTIVE.value,
        }
        try:
            persisted_event, _ = self._persist_event(
                event,
                state_patch={"_active_decisions_upsert": {key: active_decision}},
                decision=decision,
                memory=memory,
                outbox=outbox,
            )
            stored = (
                decision
                if persisted_event.id == event.id
                else self._decision_from_source_event(
                    decision_key=key, event_id=persisted_event.id
                )
            )
        except Exception as exc:
            self._set_health(
                StorageHealth.AUTHORITY_UNAVAILABLE, "decision transaction failed"
            )
            raise AuthorityUnavailableError(
                "decision was not durably committed"
            ) from exc
        if stored.supersedes_id:
            self.working_set.invalidate(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                memory_ids=[stored.supersedes_id],
            )
        return stored

    def record_constraint(
        self, *, text: str, facets: Sequence[str] = ()
    ) -> MemoryRecord:
        normalized_facets = list(dict.fromkeys(facets))
        idempotency_key = (
            f"{self.current_step_id}:constraint:"
            f"{content_hash_text(stable_json({'text': text, 'facets': normalized_facets}))}"
        )
        existing_event = self._idempotent_event(idempotency_key)
        if existing_event is not None:
            return self._memory_from_source_event(
                existing_event.id, memory_type=MemoryType.CONSTRAINT
            )
        event = self._event(
            EventType.CONSTRAINT_CREATED,
            content_text=text,
            content_json={"facets": normalized_facets},
            token_count=self.token_budget.estimate_text(text),
            idempotency_key=idempotency_key,
        )
        memory = MemoryRecord(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            step_id=self.current_step_id,
            memory_type=MemoryType.CONSTRAINT,
            title="Hard constraint",
            preview_text=text,
            importance=1.0,
            source_event_id=event.id,
            content_hash=content_hash_text(text),
            token_count=self.token_budget.estimate_text(text),
            phase=self.metrics.current_phase,
            facets=list(dict.fromkeys(["constraint", *normalized_facets])),
        )
        outbox = self._index_outbox_for(memory)
        persisted_event, _ = self._persist_event(
            event,
            state_patch={
                "_active_constraints_append": [
                    {
                        "memory_id": memory.id,
                        "source_event_id": event.id,
                        "status": MemoryStatus.ACTIVE.value,
                    }
                ]
            },
            memory=memory,
            outbox=outbox,
        )
        if persisted_event.id != event.id:
            return self._memory_from_source_event(
                persisted_event.id, memory_type=MemoryType.CONSTRAINT
            )
        return memory

    def add_dependency(
        self,
        *,
        step_id: str,
        dependency_type: DependencyType | str,
        dependency_id: str,
        required: bool = True,
    ) -> DependencyRecord:
        kind = (
            dependency_type
            if isinstance(dependency_type, DependencyType)
            else DependencyType(str(dependency_type).upper())
        )
        dependency = DependencyRecord(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            step_id=step_id,
            dependency_type=kind,
            dependency_id=dependency_id,
            required=required,
        )
        try:
            return self.authority.add_dependency(dependency)
        except Exception as exc:
            self._set_health(
                StorageHealth.AUTHORITY_UNAVAILABLE, "dependency write failed"
            )
            raise AuthorityUnavailableError(
                "dependency was not durably committed"
            ) from exc

    def preflight(
        self,
        *,
        step_id: str | None = None,
        high_risk: bool = True,
        expected_state_version: int | None = None,
        idempotency_key: str = "",
    ) -> PreflightAuditResult:
        target_step = step_id or self.current_step_id
        errors: list[str] = []
        missing: list[str] = []
        unresolved: list[str] = []
        superseded: list[str] = []
        authority_ready = bool(self.authority.health_check())
        if not authority_ready:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            errors.append("authoritative store unavailable")
            state = None
            dependencies: list[DependencyRecord] = []
            items: list[Any] = []
        else:
            state = self.authority.get_state(
                tenant_id=self.tenant_id, task_id=self.task_id
            )
            dependencies = self.dependency_resolver.resolve_closure(
                tenant_id=self.tenant_id, task_id=self.task_id, step_id=target_step
            )
            dependencies = merge_state_dependencies(
                dependencies,
                state=state,
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                step_id=target_step,
            )
            items, missing, unresolved = self.dependency_resolver.load_items(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                dependencies=dependencies,
            )
        state_version_matches = True
        if state is None:
            errors.append("current task state is missing")
            state_version = -1
        else:
            state_version = state.version
            if (
                expected_state_version is not None
                and state.version != expected_state_version
            ):
                state_version_matches = False
                errors.append(
                    f"task state version mismatch: expected {expected_state_version}, got {state.version}"
                )
        if high_risk and not idempotency_key:
            errors.append("high-risk operation has no idempotency key")
        if high_risk and self.storage_health in {
            StorageHealth.ARTIFACT_DEGRADED,
            StorageHealth.AUTHORITY_UNAVAILABLE,
            StorageHealth.RECOVERING,
        }:
            errors.append(
                f"high-risk operation denied while storage health is {self.storage_health.value}"
            )

        for dependency in dependencies:
            if not dependency.required:
                continue
            matching_items = [
                item
                for item in items
                if item.item_id == dependency.dependency_id
                or (
                    dependency.dependency_type == DependencyType.DECISION
                    and item.item_type == "DECISION"
                )
            ]
            if dependency.dependency_id in missing:
                continue
            if any(
                item.status
                in {MemoryStatus.SUPERSEDED.value, MemoryStatus.TOMBSTONED.value}
                for item in matching_items
            ):
                superseded.append(dependency.dependency_id)
                continue
            try:
                self._verify_dependency_source(dependency)
            except (
                ArtifactIntegrityError,
                ArtifactStoreUnavailable,
                ValueError,
            ) as exc:
                unresolved.append(dependency.dependency_id)
                errors.append(
                    f"source {dependency.dependency_id} is not resolvable: {exc}"
                )
        missing = list(dict.fromkeys(missing))
        unresolved = list(dict.fromkeys(unresolved))
        superseded = list(dict.fromkeys(superseded))
        required_count = sum(1 for dependency in dependencies if dependency.required)
        failed = set(missing) | set(unresolved) | set(superseded)
        resolved_count = max(0, required_count - len(failed))
        if missing:
            errors.append("missing required dependencies: " + ", ".join(missing))
        if unresolved:
            errors.append(
                "unresolved required source pointers: " + ", ".join(unresolved)
            )
        if superseded:
            errors.append(
                "superseded/tombstoned dependencies: " + ", ".join(superseded)
            )
        self._operational_metrics["source_pointer_resolution_failure_count"] = len(
            unresolved
        )
        if high_risk:
            allowed = not errors and resolved_count == required_count
        else:
            # Safe reads remain available to diagnose and recover missing
            # evidence, but never when authority/scope/version is unverifiable.
            allowed = authority_ready and state is not None and state_version_matches
        return PreflightAuditResult(
            allowed=allowed,
            task_id=self.task_id,
            step_id=target_step,
            state_version=state_version,
            required_count=required_count,
            resolved_count=resolved_count,
            missing_dependency_ids=missing,
            unresolved_source_ids=unresolved,
            superseded_ids=superseded,
            errors=errors,
        )

    def require_preflight(self, **kwargs: Any) -> PreflightAuditResult:
        result = self.preflight(**kwargs)
        if not result.allowed:
            raise PreflightBlockedError(result)
        return result

    def _verify_dependency_source(self, dependency: DependencyRecord) -> None:
        kind = dependency.dependency_type
        dependency_id = dependency.dependency_id
        if kind == DependencyType.EVENT:
            event = self.authority.get_event(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                event_id=dependency_id,
            )
            if event is None:
                raise ValueError("event does not exist in tenant/task scope")
            self._verify_event_source(event)
            return
        if kind == DependencyType.DECISION:
            decisions = self.authority.active_decisions(
                tenant_id=self.tenant_id, task_id=self.task_id
            )
            decision = next(
                (
                    value
                    for value in decisions
                    if value.id == dependency_id or value.decision_key == dependency_id
                ),
                None,
            )
            if decision is None:
                raise ValueError("active decision does not exist")
            if not decision.source_event_id:
                raise ValueError("decision has no source event")
            event = self.authority.get_event(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                event_id=decision.source_event_id,
            )
            if event is None:
                raise ValueError("decision source event is missing")
            self._verify_event_source(event)
            if event.content_text != decision.decision_text:
                raise ValueError("decision differs from its immutable source event")
            return
        if kind in {DependencyType.MEMORY, DependencyType.CONSTRAINT}:
            records = self.authority.get_memories(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                memory_ids=[dependency_id],
            )
            if not records:
                raise ValueError("memory registry entry is missing")
            record = records[0]
            if record.status not in {MemoryStatus.ACTIVE, MemoryStatus.OPEN}:
                raise ValueError(f"memory status is {record.status.value}")
            if record.artifact_id:
                artifact = self.authority.get_artifact(
                    tenant_id=self.tenant_id,
                    task_id=self.task_id,
                    artifact_id=record.artifact_id,
                )
                if artifact is None:
                    raise ValueError("memory artifact registry entry is missing")
                self._verify_artifact_record(artifact)
                source_range = record.source_range
                if record.memory_type == MemoryType.ARTIFACT_CHUNK:
                    if source_range.end <= source_range.start:
                        raise ValueError("artifact chunk has no authoritative range")
                    if source_range.unit.upper() != "BYTE":
                        raise ValueError(
                            "artifact-backed memory range must use BYTE coordinates"
                        )
                    chunk = self.read_artifact_range(
                        artifact.id,
                        start=source_range.start,
                        end=source_range.end,
                        unit="BYTE",
                    )
                    if content_hash_bytes(chunk) != record.content_hash:
                        raise ValueError("memory artifact range hash mismatch")
                elif artifact.content_hash != record.content_hash:
                    raise ValueError("memory artifact hash mismatch")
                if record.source_event_id:
                    event = self.authority.get_event(
                        tenant_id=self.tenant_id,
                        task_id=self.task_id,
                        event_id=record.source_event_id,
                    )
                    if event is None:
                        raise ValueError("memory source event is missing")
                    self._verify_event_source(event)
                    if event.content_hash != artifact.content_hash:
                        raise ValueError("memory source event/hash mismatch")
                return
            if record.source_event_id:
                event = self.authority.get_event(
                    tenant_id=self.tenant_id,
                    task_id=self.task_id,
                    event_id=record.source_event_id,
                )
                if event is None:
                    raise ValueError("memory source event is missing")
                self._verify_event_source(event)
                if event.content_hash != record.content_hash:
                    raise ValueError("memory differs from its immutable source event")
                return
            raise ValueError("memory has no resolvable event or artifact source")
        if kind == DependencyType.ARTIFACT:
            artifact = self.authority.get_artifact(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                artifact_id=dependency_id,
            )
            if artifact is None:
                raise ValueError("artifact registry entry is missing")
            self._verify_artifact_record(artifact)
            return
        if kind == DependencyType.CHECKPOINT:
            checkpoint = self.authority.latest_checkpoint(
                tenant_id=self.tenant_id, task_id=self.task_id
            )
            if checkpoint is None or checkpoint.id != dependency_id:
                raise ValueError("checkpoint is not the latest verified checkpoint")
            if (
                content_hash_text(stable_json(checkpoint.snapshot))
                != checkpoint.content_hash
            ):
                raise ValueError("checkpoint hash mismatch")
            return
        if kind == DependencyType.STEP:
            step = self.authority.get_step(
                tenant_id=self.tenant_id, task_id=self.task_id, step_id=dependency_id
            )
            if step is None:
                raise ValueError("predecessor step is missing")
            if str(step.get("status")) != "COMPLETED":
                raise ValueError(f"predecessor step status is {step.get('status')}")

    def _dependency_source_is_resolvable(self, dependency: DependencyRecord) -> bool:
        try:
            self._verify_dependency_source(dependency)
            return True
        except (
            ArtifactIntegrityError,
            ArtifactStoreUnavailable,
            KeyError,
            ValueError,
        ):
            return False

    def _verify_event_source(self, event: AgentEvent) -> None:
        if event.tenant_id != self.tenant_id or event.task_id != self.task_id:
            raise ValueError("event tenant/task scope mismatch")
        if event.artifact_uri:
            artifacts = self.authority.list_artifacts(
                tenant_id=self.tenant_id, task_id=self.task_id
            )
            artifact = next(
                (value for value in artifacts if value.uri == event.artifact_uri), None
            )
            if artifact is None:
                raise ValueError("event artifact registry entry is missing")
            if artifact.content_hash != event.content_hash:
                raise ValueError("event/artifact content hash mismatch")
            self._verify_artifact_record(artifact, expected_hash=event.content_hash)
            return
        declared_hash = str(event.content_json.get("content_hash", ""))
        if declared_hash:
            if declared_hash != event.content_hash:
                raise ValueError("event declared content hash mismatch")
            return
        material = event.content_text or stable_json(event.content_json)
        if content_hash_text(material) != event.content_hash:
            raise ValueError("event content hash mismatch")

    def _verify_artifact_record(
        self,
        artifact: ArtifactRecord,
        *,
        expected_hash: str = "",
        read_back: bool = True,
    ) -> None:
        if artifact.tenant_id != self.tenant_id or artifact.task_id != self.task_id:
            raise ArtifactIntegrityError("artifact tenant/task scope mismatch")
        if expected_hash and artifact.content_hash != expected_hash:
            raise ArtifactIntegrityError(
                "artifact write returned a different content hash"
            )
        if artifact.status == ArtifactStatus.PENDING_UPLOAD:
            if read_back:
                raise ArtifactStoreUnavailable("artifact is pending upload")
            return
        if artifact.status != ArtifactStatus.ACTIVE:
            raise ArtifactIntegrityError(f"artifact status is {artifact.status.value}")
        if read_back:
            data = self.artifact_store.get_bytes(artifact)
            if content_hash_bytes(data) != artifact.content_hash:
                raise ArtifactIntegrityError("artifact read-back hash mismatch")

    def read_event(self, event_id: str) -> AgentEvent:
        event = self.authority.get_event(
            tenant_id=self.tenant_id, task_id=self.task_id, event_id=event_id
        )
        if event is None:
            raise KeyError(f"event not found in task scope: {event_id}")
        return event

    def read_event_range(
        self,
        event_id: str,
        *,
        start: int = 0,
        end: int = 16_000,
        unit: str = "BYTE",
    ) -> bytes:
        if start < 0 or end < start:
            raise ValueError("event range must satisfy 0 <= start <= end")
        text = self.read_event(event_id).content_text
        normalized = unit.upper()
        if normalized == "BYTE":
            return text.encode("utf-8")[start:end]
        if normalized == "CHAR":
            return text[start:end].encode("utf-8")
        if normalized == "LINE":
            return "\n".join(text.splitlines()[start:end]).encode("utf-8")
        if normalized == "PAGE":
            return "\f".join(text.split("\f")[start:end]).encode("utf-8")
        raise ValueError("event range unit must be BYTE, CHAR, LINE, or PAGE")

    def event_source_end(self, event_id: str, *, unit: str) -> int:
        text = self.read_event(event_id).content_text
        normalized = unit.upper()
        if normalized == "BYTE":
            return len(text.encode("utf-8"))
        if normalized == "CHAR":
            return len(text)
        if normalized == "LINE":
            return len(text.splitlines())
        if normalized == "PAGE":
            return len(text.split("\f"))
        raise ValueError("event range unit must be BYTE, CHAR, LINE, or PAGE")

    def read_artifact_range(
        self,
        artifact_id: str,
        *,
        start: int = 0,
        end: int = 16_000,
        unit: str = "BYTE",
        query: str = "",
        log_level: str = "",
    ) -> bytes:
        artifact = self.authority.get_artifact(
            tenant_id=self.tenant_id, task_id=self.task_id, artifact_id=artifact_id
        )
        if artifact is None:
            raise KeyError(f"artifact not found in task scope: {artifact_id}")
        if artifact.status not in {
            ArtifactStatus.ACTIVE,
            ArtifactStatus.PENDING_UPLOAD,
        }:
            raise ArtifactIntegrityError(
                f"artifact is not readable in status {artifact.status.value}: {artifact.id}"
            )
        store = (
            self.spool_store
            if artifact.status == ArtifactStatus.PENDING_UPLOAD
            else self.artifact_store
        )
        if store is None:
            raise ArtifactStoreUnavailable("pending artifact spool is not configured")
        read_started = time.perf_counter()
        try:
            data = store.read_range(
                artifact,
                start=start,
                end=end,
                unit=unit,
                query=query,
                log_level=log_level,
            )
            self._operational_metrics["page_fault_count"] += 1
            self._operational_metrics["raw_page_tokens"] += (
                self.token_budget.estimate_text(data.decode("utf-8", errors="replace"))
            )
            return data
        except ValueError:
            # Invalid pagination input is not evidence of object loss.
            raise
        except ArtifactIntegrityError:
            if artifact.status in {
                ArtifactStatus.ACTIVE,
                ArtifactStatus.PENDING_UPLOAD,
            }:
                self.authority.update_artifact_status(
                    tenant_id=self.tenant_id,
                    task_id=self.task_id,
                    artifact_id=artifact.id,
                    status=ArtifactStatus.CORRUPT.value,
                )
            self._set_health(
                StorageHealth.ARTIFACT_DEGRADED,
                f"artifact integrity check failed: {artifact.id}",
            )
            raise
        except ArtifactStoreUnavailable:
            # Timeouts and outages are recoverable and do not prove MISSING.
            self._set_health(
                StorageHealth.ARTIFACT_DEGRADED,
                f"artifact store unavailable: {artifact.id}",
            )
            raise
        except Exception:
            self._set_health(
                StorageHealth.ARTIFACT_DEGRADED, f"artifact read failed: {artifact.id}"
            )
            raise
        finally:
            self._operational_metrics["artifact_read_latency_ms"] = (
                time.perf_counter() - read_started
            ) * 1_000

    def _memory_source_payload(self, memory: MemoryRecord) -> bytes:
        """Load and authenticate the exact source owned by one memory card."""

        source_range = memory.source_range
        bounded = source_range.end > source_range.start
        source_unit = source_range.unit.upper()
        if memory.artifact_id:
            artifact = self.authority.get_artifact(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                artifact_id=memory.artifact_id,
            )
            if artifact is None:
                raise ArtifactIntegrityError(
                    "memory artifact registry entry is missing"
                )
            if bounded:
                payload = self.read_artifact_range(
                    artifact.id,
                    start=source_range.start,
                    end=source_range.end,
                    unit=source_unit,
                )
            else:
                payload = self.read_artifact_range(
                    artifact.id,
                    start=0,
                    end=artifact.size_bytes,
                    unit="BYTE",
                )
        elif memory.source_event_id:
            event = self.read_event(memory.source_event_id)
            self._verify_event_source(event)
            if bounded:
                payload = self.read_event_range(
                    event.id,
                    start=source_range.start,
                    end=source_range.end,
                    unit=source_unit,
                )
            else:
                payload = event.content_text.encode("utf-8")
        else:
            raise ArtifactIntegrityError("memory has no raw source pointer")

        if content_hash_bytes(payload) != memory.content_hash:
            raise ArtifactIntegrityError("memory source range hash mismatch")
        return payload

    def read_memory_range(
        self,
        memory_id: str,
        *,
        start: int = 0,
        end: int = 16_000,
        unit: str = "BYTE",
    ) -> bytes:
        """Read coordinates relative to one authenticated Memory source.

        Artifact coordinates remain absolute in ``read_artifact_range``. Keeping
        the two coordinate systems separate prevents a tail chunk from silently
        reading the beginning of its parent object.
        """

        memories = self.authority.get_memories(
            tenant_id=self.tenant_id, task_id=self.task_id, memory_ids=[memory_id]
        )
        if not memories:
            raise KeyError(f"memory not found in task scope: {memory_id}")
        memory = memories[0]
        payload = self._memory_source_payload(memory)
        return _select_bytes(payload, start=start, end=end, unit=unit)

    def memory_source_end(self, memory_id: str, *, unit: str) -> int | None:
        """Return the exclusive end in Memory-relative coordinates."""
        memories = self.authority.get_memories(
            tenant_id=self.tenant_id, task_id=self.task_id, memory_ids=[memory_id]
        )
        if not memories:
            raise KeyError(f"memory not found in task scope: {memory_id}")
        memory = memories[0]
        normalized = unit.upper()
        if (
            memory.source_range.end > memory.source_range.start
            and memory.source_range.unit.upper() == normalized
        ):
            return memory.source_range.end - memory.source_range.start
        payload = self._memory_source_payload(memory)
        if normalized == "BYTE":
            return len(payload)
        text = payload.decode("utf-8")
        if normalized == "CHAR":
            return len(text)
        if normalized == "LINE":
            return len(text.splitlines())
        if normalized == "PAGE":
            return len(text.split("\f"))
        raise ValueError("memory range unit must be BYTE, CHAR, LINE, or PAGE")

    def artifact_source_end(
        self,
        artifact_id: str,
        *,
        unit: str,
        query: str = "",
        log_level: str = "",
    ) -> int:
        """Return an exact exclusive end for a scoped, verified Artifact view."""
        artifact = self.authority.get_artifact(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            artifact_id=artifact_id,
        )
        if artifact is None:
            raise KeyError(f"artifact not found in task scope: {artifact_id}")
        normalized = unit.upper()
        if normalized == "BYTE" and not query and not log_level:
            return artifact.size_bytes
        # Whole-object hashes require a verified full read anyway.  Use a
        # range large enough to include every logical text unit, then measure
        # the filtered view with the same semantics as read_artifact_range.
        data = self.read_artifact_range(
            artifact_id,
            start=0,
            end=artifact.size_bytes,
            unit=normalized,
            query=query,
            log_level=log_level,
        )
        if normalized == "BYTE":
            return len(data)
        text = data.decode("utf-8")
        if normalized == "CHAR":
            return len(text)
        if normalized == "LINE":
            return len(text.splitlines())
        if normalized == "PAGE":
            return len(text.split("\f"))
        raise ValueError("artifact range unit must be BYTE, CHAR, LINE, or PAGE")

    def page_memory(
        self,
        memory_id: str,
        *,
        start: int = 0,
        end: int = 16_000,
        unit: str = "BYTE",
    ) -> str:
        """Compatibility text view; model tools use lossless byte pages."""
        return self.read_memory_range(
            memory_id, start=start, end=end, unit=unit
        ).decode("utf-8", errors="replace")

    def search(self, query: str, *, limit: int | None = None) -> list[MemoryRecord]:
        request = MemorySearchRequest(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            query=query,
            candidate_limit=limit or self.config.candidate_limit,
        )
        return [
            hit.record
            for hit in self.retriever.search(
                request,
                use_vector=(self.current_mode == MemoryMode.PAGED_MEMORY),
            )
        ]

    def process_outbox(self, *, limit: int = 100) -> dict[str, int]:
        counts = {"processed": 0, "retried": 0, "failed": 0, "skipped": 0}
        if self.vector_index is None:
            return counts
        counts = OutboxIndexer(
            authority=self.authority,
            vector_index=self.vector_index,
            config=self.config,
        ).run_once(limit=limit)
        self._operational_metrics["outbox_retry_count"] += counts["retried"]
        if counts["retried"] or counts["failed"]:
            self._set_health(
                StorageHealth.INDEX_DEGRADED, "index outbox processing incomplete"
            )
        elif (
            counts["processed"]
            and self.vector_index.health_check()
            and self.storage_health == StorageHealth.INDEX_DEGRADED
        ):
            self._set_health(StorageHealth.NORMAL, "index outbox caught up")
        return counts

    def replay_pending_artifacts(self) -> dict[str, int]:
        """Idempotently move encrypted spool objects into the primary store."""
        counts = {"uploaded": 0, "failed": 0, "skipped": 0}
        if self.spool_store is None:
            return counts
        self._set_health(StorageHealth.RECOVERING, "replaying pending artifacts")
        pending = self.authority.list_artifacts(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            statuses=(ArtifactStatus.PENDING_UPLOAD.value,),
        )

        def pending_order(artifact: ArtifactRecord) -> tuple[int, float, str, str, str]:
            try:
                sequence_no = int(artifact.metadata.get("sequence_no", 0))
            except (TypeError, ValueError):
                sequence_no = 0
            return (
                sequence_no,
                artifact.created_at,
                str(artifact.metadata.get("correlation_id", "")),
                str(artifact.metadata.get("idempotency_key", "")),
                artifact.id,
            )

        pending.sort(key=pending_order)
        for artifact in pending:
            uploaded: ArtifactRecord | None = None
            try:
                data = self.spool_store.get_bytes(artifact)
                if content_hash_bytes(data) != artifact.content_hash:
                    raise ArtifactIntegrityError("spool replay hash mismatch")
                uploaded = self.artifact_store.put_bytes(
                    data,
                    artifact_id=artifact.id,
                    tenant_id=artifact.tenant_id,
                    task_id=artifact.task_id,
                    run_id=artifact.run_id,
                    step_id=artifact.step_id,
                    media_type=artifact.media_type,
                    artifact_type=artifact.artifact_type,
                )
                self._verify_artifact_record(
                    uploaded, expected_hash=artifact.content_hash
                )
                self.authority.update_artifact_status(
                    tenant_id=self.tenant_id,
                    task_id=self.task_id,
                    artifact_id=artifact.id,
                    status=ArtifactStatus.ACTIVE.value,
                    uri=uploaded.uri,
                )
                self.spool_store.delete(artifact)
                counts["uploaded"] += 1
            except Exception as exc:  # noqa: BLE001 - report spans storage adapters.
                # A client can report a timeout after COMMIT. Confirm the
                # authoritative result before retrying or reporting loss.
                if uploaded is not None:
                    try:
                        committed = self.authority.get_artifact(
                            tenant_id=self.tenant_id,
                            task_id=self.task_id,
                            artifact_id=artifact.id,
                        )
                        if (
                            committed is not None
                            and committed.status == ArtifactStatus.ACTIVE
                            and committed.uri == uploaded.uri
                            and committed.content_hash == artifact.content_hash
                        ):
                            self._verify_artifact_record(
                                committed, expected_hash=artifact.content_hash
                            )
                            self.spool_store.delete(artifact)
                            counts["uploaded"] += 1
                            continue
                    except Exception as verification_error:  # noqa: BLE001
                        logger.debug(
                            "Could not confirm post-commit artifact state for %s: %s",
                            artifact.id,
                            type(verification_error).__name__,
                        )
                logger.warning("Artifact replay failed for %s: %s", artifact.id, exc)
                counts["failed"] += 1

        # Recover the crash window after Registry commit and before spool
        # deletion. The duplicate is removed only after primary hash checks.
        try:
            spool_uris = {uri for uri, _modified in self.spool_store.list_objects()}
            active = self.authority.list_artifacts(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                statuses=(ArtifactStatus.ACTIVE.value,),
            )
            for artifact in active:
                source_spool_uri = str(artifact.metadata.get("source_spool_uri", ""))
                if not source_spool_uri or source_spool_uri not in spool_uris:
                    continue
                self._verify_artifact_record(
                    artifact, expected_hash=artifact.content_hash
                )
                stale_spool_record = ArtifactRecord(
                    **{
                        **artifact.__dict__,
                        "uri": source_spool_uri,
                        "status": ArtifactStatus.PENDING_UPLOAD,
                    }
                )
                self.spool_store.delete(stale_spool_record)
                counts["skipped"] += 1
        except Exception as exc:  # noqa: BLE001 - residue remains retryable.
            logger.warning("Promoted artifact spool cleanup failed: %s", exc)
            counts["failed"] += 1
        if counts["failed"]:
            self._set_health(
                StorageHealth.ARTIFACT_DEGRADED, "pending artifact replay incomplete"
            )
        elif self.vector_index is None and self.config.milvus_enabled:
            self._set_health(
                StorageHealth.INDEX_DEGRADED,
                "artifact store recovered; index unavailable",
            )
        else:
            self._set_health(StorageHealth.NORMAL, "pending artifact replay verified")
        return counts

    @staticmethod
    def _safe_health(component: Any, *, absent_is_healthy: bool = False) -> bool:
        if component is None:
            return absent_is_healthy
        try:
            return bool(component.health_check())
        except Exception:  # noqa: BLE001 - optional adapters use unrelated errors.
            return False

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return a secret-free snapshot for a logging, OTel or Prometheus bridge."""
        packet = self.last_packet
        debug = packet.debug_metadata if packet is not None else {}

        def section_tokens(name: str) -> int:
            if packet is None:
                return 0
            return sum(
                max(0, item.estimated_tokens) for item in getattr(packet, name, ())
            )

        pending: list[ArtifactRecord] = []
        index_lag = 0
        try:
            pending = self.authority.list_artifacts(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                statuses=(ArtifactStatus.PENDING_UPLOAD.value,),
            )
            index_lag = len(self.authority.pending_outbox(limit=1_000_000))
        except Exception:  # noqa: BLE001 - telemetry cannot alter execution.
            pending = []
            index_lag = 0

        selected_count = int(debug.get("selected_memory_count", 0))
        working_count = int(debug.get("working_set_item_count", 0))
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "step_id": self.current_step_id,
            "memory_mode": self.current_mode.value,
            "mode_change_reason": debug.get("mode_reason", ""),
            "model_context_limit": self.model_context_limit,
            "estimated_input_tokens": (
                packet.estimated_input_tokens if packet is not None else 0
            ),
            "actual_input_tokens": None,
            "output_tokens": int(self._operational_metrics["output_tokens"]),
            "system_tokens": section_tokens("system_context"),
            "state_tokens": section_tokens("task_state")
            + section_tokens("current_step"),
            "recent_event_tokens": section_tokens("recent_messages"),
            "dependency_tokens": section_tokens("mandatory_dependencies"),
            "working_set_tokens": section_tokens("working_set"),
            "milvus_memory_tokens": section_tokens("supplemental_memories"),
            "raw_page_tokens": int(self._operational_metrics["raw_page_tokens"]),
            "retrieved_candidate_count": int(debug.get("retrieved_candidate_count", 0)),
            "selected_memory_count": selected_count,
            "duplicate_memory_count": int(debug.get("duplicate_memory_count", 0)),
            "superseded_memory_count": 0,
            "page_fault_count": int(self._operational_metrics["page_fault_count"]),
            "working_set_hit_rate": working_count
            / max(1, working_count + selected_count),
            "milvus_latency_ms": float(debug.get("milvus_latency_ms", 0.0)),
            "artifact_read_latency_ms": float(
                self._operational_metrics["artifact_read_latency_ms"]
            ),
            "context_build_latency_ms": float(
                debug.get("context_build_latency_ms", 0.0)
            ),
            "index_lag": index_lag,
            "outbox_retry_count": int(self._operational_metrics["outbox_retry_count"]),
            "task_state_version_conflict": int(
                self._operational_metrics["task_state_version_conflict"]
            ),
            "tool_result_original_size": int(
                self._operational_metrics["tool_result_original_size"]
            ),
            "tool_result_prompt_size": int(
                self._operational_metrics["tool_result_prompt_size"]
            ),
            "required_dependency_count": int(debug.get("required_dependency_count", 0)),
            "resolved_required_dependency_count": int(
                debug.get("resolved_required_dependency_count", 0)
            ),
            "required_dependency_coverage_ratio": float(
                debug.get("required_dependency_coverage_ratio", 1.0)
            ),
            "required_facet_coverage_ratio": float(
                debug.get("required_facet_coverage_ratio", 1.0)
            ),
            "source_pointer_resolution_failure_count": int(
                max(
                    int(
                        self._operational_metrics[
                            "source_pointer_resolution_failure_count"
                        ]
                    ),
                    len(debug.get("unresolved_source_ids", ())),
                )
            ),
            "postgres_write_latency_ms": float(
                self._operational_metrics["authority_write_latency_ms"]
            ),
            "postgres_availability_state": (
                "AVAILABLE" if self._safe_health(self.authority) else "UNAVAILABLE"
            ),
            "artifact_store_availability_state": (
                "AVAILABLE" if self._safe_health(self.artifact_store) else "UNAVAILABLE"
            ),
            "artifact_upload_failure_count": int(
                self._operational_metrics["artifact_upload_failure_count"]
            ),
            "local_durable_spool_bytes": sum(item.size_bytes for item in pending),
            "supplemental_retrieval_degraded": bool(
                debug.get("supplemental_retrieval_degraded", False)
            ),
            "vector_candidate_mismatch_count": int(
                debug.get("vector_candidate_mismatch_count", 0)
            ),
            # Lifecycle metrics are emitted by LifecycleManager operations.
            "artifact_orphan_candidate_count": None,
            "hard_delete_propagation_lag_ms": None,
            "last_successful_backup_age_seconds": None,
            "restore_drill_success": None,
        }

    def emit_metrics(self, sink: Any) -> None:
        """Send one sanitized snapshot to an existing metrics adapter."""
        snapshot = self.metrics_snapshot()
        if callable(sink):
            sink(snapshot)
            return
        record = getattr(sink, "record", None)
        if not callable(record):
            raise TypeError("metrics sink must be callable or expose record(snapshot)")
        record(snapshot)

    def health_snapshot(self) -> dict[str, Any]:
        authority_ok = self._safe_health(self.authority)
        artifact_ok = self._safe_health(self.artifact_store)
        index_ok = self._safe_health(
            self.vector_index,
            absent_is_healthy=not self.config.milvus_enabled,
        )
        expected_entries: dict[str, str] = {}
        actual_entries: dict[str, str] = {}
        reconciliation_error = ""
        if authority_ok and self.config.milvus_enabled:
            try:
                expected_entries = {
                    memory.id: memory.content_hash
                    for memory in self.authority.all_memories(
                        tenant_id=self.tenant_id,
                        task_id=self.task_id,
                    )
                    if memory.status in {MemoryStatus.ACTIVE, MemoryStatus.OPEN}
                }
                if self.vector_index is None:
                    reconciliation_error = "vector index unavailable"
                else:
                    list_entries = getattr(self.vector_index, "list_entries", None)
                    if not callable(list_entries):
                        raise TypeError(
                            "vector index does not expose hash reconciliation"
                        )
                    actual_entries = dict(
                        list_entries(
                            tenant_id=self.tenant_id,
                            task_id=self.task_id,
                            statuses=(
                                MemoryStatus.ACTIVE.value,
                                MemoryStatus.OPEN.value,
                            ),
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - health output is fail-safe.
                reconciliation_error = type(exc).__name__
                index_ok = False
        expected_ids = set(expected_entries)
        actual_ids = set(actual_entries)
        missing_ids = sorted(expected_ids - actual_ids)
        orphan_ids = sorted(actual_ids - expected_ids)
        hash_mismatch_ids = sorted(
            memory_id
            for memory_id in expected_ids & actual_ids
            if actual_entries[memory_id] != expected_entries[memory_id]
        )
        effective_state = self.storage_health
        if not authority_ok:
            effective_state = StorageHealth.AUTHORITY_UNAVAILABLE
        elif self.config.milvus_enabled and (
            not index_ok
            or reconciliation_error
            or missing_ids
            or orphan_ids
            or hash_mismatch_ids
        ):
            effective_state = StorageHealth.INDEX_DEGRADED
        return {
            "state": effective_state.value,
            "authority": authority_ok,
            "artifact_store": artifact_ok,
            "vector_index": index_ok,
            "vector_consistency": {
                "index_id": self.index_descriptor.index_id,
                "collection": self.index_descriptor.collection,
                "expected": len(expected_ids),
                "actual": len(actual_ids),
                "missing": len(missing_ids),
                "orphan": len(orphan_ids),
                "hash_mismatch": len(hash_mismatch_ids),
                "missing_ids": missing_ids[:20],
                "orphan_ids": orphan_ids[:20],
                "hash_mismatch_ids": hash_mismatch_ids[:20],
                "error": reconciliation_error,
            },
            "task_id": self.task_id,
            "run_id": self.run_id,
            "mode": self.current_mode.value,
            "metrics": self.metrics_snapshot(),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_vector_index and self.vector_index is not None:
            try:
                self.vector_index.close()
            except Exception:
                logger.warning("Could not close vector index", exc_info=True)
        if (
            self._owns_artifact_store
            and getattr(self, "artifact_store", None) is not None
        ):
            close = getattr(self.artifact_store, "close", None)
            if callable(close):
                close()
        if self._owns_authority and getattr(self, "authority", None) is not None:
            try:
                self.authority.close()
            except Exception:
                logger.warning("Could not close authority store", exc_info=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "ArtifactPersistenceRequired",
    "AuthorityUnavailableError",
    "ContextBudgetExceeded",
    "PagedMemoryError",
    "PagedMemoryRuntime",
    "PreflightBlockedError",
]
