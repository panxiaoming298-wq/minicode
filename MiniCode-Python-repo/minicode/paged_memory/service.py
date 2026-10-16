"""Runtime coordination for tiered context and lossless authoritative memory.

This module is the only component the agent loop needs to know about.  It
coordinates SQLite, local artifact persistence and the context builder while
keeping every derived summary linked to raw source material.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import asdict
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from minicode.runtime_protocol import AgentResultStatus
from minicode.runtime_messages import is_user_input, runtime_message_kind
from minicode.context_borrowing import ContextBorrowing

from .artifacts import (
    ArtifactIntegrityError,
    ArtifactStoreUnavailable,
    _select_bytes,
    build_artifact_store,
    build_encrypted_spool,
)
from .chunking import chunk_utf8_text, event_chunk_records, tool_text_chunk_records
from .config import PagedMemoryConfig
from .context_builder import PagedContextBuilder
from .code_focus import CodeFocus
from .handoff import HandoffNotes
from .rotation import ContextRotation
from .history_journal import HistoryIntegrityError
from .tool_result_projection import output_summary, prepare_projection
from .result_reclamation import ResultReclamation
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
    HandoffNoteConflict,
    ContextRotationConflict,
    MemoryRecord,
    MemoryMode,
    MemorySearchRequest,
    MemoryStatus,
    MemoryType,
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
from .policy import MemoryModePolicy, TokenBudgetManager, initialize_rerank_state
from .context_utilization import ContextUtilization
from .query import retrieval_query
from .markdown_export import is_generated_export, is_export_read
from .retrieval import (
    DependencyResolver,
    LexicalMemoryRetriever,
    WorkingSetManager,
    dependency_contract_hash,
    merge_state_dependencies,
)
from .sqlite_store import SqliteMemoryStore, StateVersionConflict

logger = logging.getLogger(__name__)


class PagedMemoryError(RuntimeError):
    """Base class for errors that have explicit execution semantics."""


class AuthorityUnavailableError(PagedMemoryError):
    """The authoritative store cannot durably accept or verify an operation."""


class AuthorityRecoveringError(AuthorityUnavailableError):
    """A rolled-back operation needs a fresh authority read before resuming."""


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
        if is_user_input(message):
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
        spool_store: Any | None = None,
        run_id: str | None = None,
        step_namespace: str = "",
        runtime_values: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        # Display/tool filtering only; execution still checks the live Worker.
        # A restored checkpoint never restores this process-local scope.
        from minicode.runtime_protocol import ToolEffect
        effects = (runtime_values or {}).get("_allowedToolEffects")
        self.context_tool_effects = None if effects is None else frozenset(ToolEffect(v) for v in effects)
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
        initial_mode = config.manual_mode or config.default_mode
        self.current_mode = initial_mode.canonical
        self.storage_health = StorageHealth.NORMAL
        self._authority_health_before_recovery: StorageHealth | None = None
        self.metrics = TaskMetrics(counter_run_id=self.run_id)
        initialize_rerank_state(self.metrics, initial_mode)
        self.last_packet: ContextPacket | None = None
        self._request_budget = None
        self.current_step_id = ""
        self._step_sequence = 0
        self._step_finalized = True
        self._last_checkpoint: CheckpointRecord | None = None
        self._step_contract_sources: list[str] = []
        self._step_acceptance_criteria: dict[str, Any] = {}
        self._operational_metrics: dict[str, float | int] = {
            "page_fault_count": 0,
            "raw_page_tokens": 0,
            "artifact_read_latency_ms": 0.0,
            "artifact_upload_failure_count": 0,
            "source_pointer_resolution_failure_count": 0,
            "tool_result_original_size": 0,
            "tool_result_prompt_size": 0,
            "output_tokens": 0,
            "authority_write_latency_ms": 0.0,
            "task_state_version_conflict": 0,
        }
        self._lock = threading.RLock()
        self._owns_authority = authority is None
        self._owns_artifact_store = artifact_store is None
        self._closed = False
        self._last_maintenance_at: float | None = None
        self._last_maintenance_report: dict[str, Any] = {}

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
                f"authoritative memory store is unavailable ({getattr(exc, 'sqlite_errorname', type(exc).__name__)}: {exc})"
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
        self.token_budget = TokenBudgetManager(model_name=model_name)
        self.result_reclamation = ResultReclamation(self)
        self.mode_policy = MemoryModePolicy(config)
        self.dependency_resolver = DependencyResolver(
            self.authority,
            max_depth=config.dependency_max_depth,
            source_validator=self._dependency_source_is_resolvable,
        )
        self.working_set = WorkingSetManager(self.authority)
        self.retriever = LexicalMemoryRetriever(
            self.authority,
            lexical_candidate_limit=config.lexical_candidate_limit,
            timeout_seconds=config.retrieval_timeout_seconds,
        )
        self.handoff_notes = HandoffNotes(self, runtime_values)
        self.context_rotation = ContextRotation(self)
        self.context_utilization = ContextUtilization(self)
        self.context_borrowing = ContextBorrowing(self)
        self.code_focus = CodeFocus(self)
        self.context_builder = PagedContextBuilder(
            authority=self.authority,
            config=config,
            mode_policy=self.mode_policy,
            token_budget=self.token_budget,
            retriever=self.retriever,
            dependency_resolver=self.dependency_resolver,
            working_set=self.working_set,
            handoff_notes=self.handoff_notes,
            context_rotation=self.context_rotation,
            code_focus=self.code_focus,
            context_tool_names=self.context_tool_names,
            reranker=self._build_reranker(
                config, runtime_values, authority=self.authority
            ),
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

    def context_tool_names(self) -> frozenset[str]:
        from .tools import create_paged_memory_tools
        return frozenset(tool.name for tool in create_paged_memory_tools(self))

    @staticmethod
    def _build_reranker(
        config: PagedMemoryConfig, runtime: Mapping[str, Any] | None, *, authority=None
    ):
        from .reranking import OptionalMemoryReranker

        return OptionalMemoryReranker(config, runtime, authority=authority)

    @classmethod
    def from_runtime(
        cls,
        *,
        runtime: Mapping[str, Any] | None,
        cwd: str | Path,
        messages: Sequence[Mapping[str, Any]],
        session: Any | None = None,
        model_name: str = "",
        model_context_limit: int | None = None,
    ) -> PagedMemoryRuntime | None:
        runtime_values: Mapping[str, Any] = runtime or {}
        from minicode.context_manager import resolve_context_limit

        model_name = model_name or str(runtime_values.get("model", ""))
        resolved_limit = resolve_context_limit(model_name, dict(runtime_values))
        model_context_limit = min(resolved_limit, model_context_limit) if model_context_limit is not None else resolved_limit
        config = PagedMemoryConfig.from_sources(runtime_values, cwd=cwd)
        if not (config.enabled or config.shadow_write):
            return None
        session_id = str(
            getattr(session, "session_id", "") or getattr(session, "id", "")
        )
        # Helpers may receive only a scoped assignment and the server-owned
        # user contract. Never promote an injected control message to the goal.
        instructions = runtime_values.get("_memoryUserInstructions") or ()
        contract = runtime_values.get("_memoryTaskContract")
        goal = _latest_user_text(messages) or (
            str(instructions[-1]) if instructions else
            str(contract.get("request", "")) if isinstance(contract, Mapping) else ""
        )
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
        node_id = str(runtime_values.get("_runtimeNodeId") or "")
        attempt_id = str(runtime_values.get("_runtimeAttemptId") or "")
        step_namespace = node_id
        if node_id and attempt_id:
            # A resumed Worker starts its local sequence at one again. Its
            # attempt owns a new step namespace; reusing only node_id would
            # overwrite COMPLETED steps and collide with their finalization
            # idempotency keys. Hash both full IDs before identifier truncation.
            activation = content_hash_text(stable_json([node_id, attempt_id])).removeprefix("sha256:")
            step_namespace = f"{node_id[:64]}.attempt.{activation}"
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
                step_namespace=step_namespace,
                runtime_values=runtime_values,
            )
            # A new process/session resumes the last verified view
            # automatically; callers should not need a model tool invocation
            # merely to recover mode and progress counters.
            instance.restore_latest_checkpoint()
            # A checkpoint restores progress, not provider message history.
            # Detect an abbreviated history before the new run writes it again.
            incoming_user_hashes = {
                content_hash_text(_message_content(message))
                for message in messages
                if is_user_input(message)
            }
            previous_inputs = instance.authority.recent_events(
                tenant_id=instance.tenant_id,
                task_id=instance.task_id,
                event_types=(EventType.USER_INPUT.value,),
                limit=100,
            )
            if any(
                event.run_id != instance.run_id
                and event.content_hash not in incoming_user_hashes
                for event in previous_inputs
            ):
                instance.metrics.cross_session_required = True
            if config.manual_mode is not None:
                instance.current_mode = config.manual_mode.canonical
                instance.metrics.rerank_eligible = config.manual_mode is MemoryMode.PAGED_MEMORY
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
            instance.configure_task_contract(runtime_values, messages)
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
        config.validate()
        return SqliteMemoryStore(config.sqlite_path)

    def _persist_event(
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
                memories=memories,
                decision=decision,
                checkpoint=checkpoint,
                step_status=step_status,
            )
        except (HandoffNoteConflict, ContextRotationConflict, HistoryIntegrityError):
            # Preserve exact conflicts/integrity failures instead of presenting
            # them as retryable storage outages.
            raise
        except StateVersionConflict as exc:
            self._operational_metrics["task_state_version_conflict"] += 1
            self._mark_authority_recovering()
            raise AuthorityRecoveringError(
                "authoritative state changed concurrently; the operation was rolled back. "
                "Re-read current state before resuming; completed tool effects must not be replayed."
            ) from exc
        except sqlite3.OperationalError as exc:
            busy = getattr(exc, "sqlite_errorcode", 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            if busy:
                self._mark_authority_recovering()
            else:
                self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            error_type = AuthorityRecoveringError if busy else AuthorityUnavailableError
            raise error_type(f"authoritative write could not be committed ({getattr(exc, 'sqlite_errorname', type(exc).__name__)}: {exc}); existing results are retained, "
                             "state advancement is blocked until storage recovery") from exc
        except Exception as exc:
            self.storage_health = StorageHealth.AUTHORITY_UNAVAILABLE
            raise AuthorityUnavailableError(
                "authoritative write failed; state advancement is blocked"
            ) from exc
        finally:
            self._operational_metrics["authority_write_latency_ms"] = (
                time.perf_counter() - write_started
            ) * 1_000
        if self._authority_health_before_recovery is not None:
            if self.storage_health == StorageHealth.RECOVERING:
                self.storage_health = self._authority_health_before_recovery
            self._authority_health_before_recovery = None
        return result

    def _mark_authority_recovering(self) -> None:
        if self._authority_health_before_recovery is None:
            self._authority_health_before_recovery = self.storage_health
        self.storage_health = StorageHealth.RECOVERING

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


    def _text_artifact_chunk_records(
        self,
        *,
        output: str,
        event: AgentEvent,
        artifact: ArtifactRecord | None,
        tool_name: str,
        ok: bool,
    ) -> list[MemoryRecord]:
        """Project tool text beyond its navigation preview into searchable ranges.

        The event or artifact remains the authority for the original payload. Chunk
        records contain bounded text used by SQLite keyword retrieval, plus an
        exact byte range and digest that can be independently verified.
        """

        return tool_text_chunk_records(output=output, event=event, artifact=artifact,
            tool_name=tool_name, phase=self.metrics.current_phase, ok=ok,
            estimate_tokens=self._estimate_chunk_tokens, raw_page_token_limit=self.config.raw_page_token_limit)

    def _estimate_chunk_tokens(self, text: str) -> int:
        return self.token_budget.estimate_text(text)

    def _chunk_token_limit(self) -> int:
        return max(1, min(384, self.config.raw_page_token_limit))

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
                )
            )
        return stored

    def _message_memory(self, event: AgentEvent, *, role: str) -> MemoryRecord | None:
        """Project a raw conversation event into a source-linked search card."""
        content = event.content_text
        if not content or is_generated_export(content):
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
        self.context_rotation.remember_initial_history(messages)
        role_types = {
            "user": EventType.USER_INPUT,
            "assistant": EventType.MODEL_OUTPUT,
            "assistant_progress": EventType.MODEL_OUTPUT,
            "assistant_tool_call": EventType.TOOL_CALL,
            "tool_result": EventType.TOOL_RESULT,
        }
        for index, message in enumerate(messages):
            role = str(message.get("role", ""))
            runtime_kind = runtime_message_kind(message)
            event_type = EventType.STATE_UPDATED if runtime_kind else role_types.get(role)
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
            if runtime_kind:
                event_metadata["runtime_message_kind"] = runtime_kind
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
                if not runtime_kind and role in {"user", "assistant", "assistant_progress"}
                else None
            )
            if memory is not None:
                event.content_json["memory_id"] = memory.id
            chunks = self._conversation_chunks(event, role=role) if memory else []
            persisted_event, _ = self._persist_event(
                event,
                memory=memory,
                memories=chunks,
            )
            if chunks and persisted_event.id != event.id:
                # Replaying history written by an older version repairs only
                # missing derived chunks, without rewriting its raw event.
                for chunk in self._conversation_chunks(persisted_event, role=role):
                    if not self.authority.get_memories(
                        tenant_id=self.tenant_id,
                        task_id=self.task_id,
                        memory_ids=[chunk.id],
                    ):
                        self.authority.put_memory(
                            chunk
                        )

    def _conversation_chunks(
        self, event: AgentEvent, *, role: str
    ) -> list[MemoryRecord]:
        if is_generated_export(event.content_text):
            return []
        preview_limit = max(800, min(4_000, self.config.raw_page_token_limit * 4))
        if (
            len(event.content_text) <= preview_limit
            and self._estimate_chunk_tokens(event.content_text)
            <= self._chunk_token_limit()
        ):
            return []
        return event_chunk_records(
            event=event,
            role=role,
            phase=self.metrics.current_phase,
            estimate_tokens=self._estimate_chunk_tokens,
            max_tokens=self._chunk_token_limit(),
        )

    def configure_task_contract(
        self, runtime: Mapping[str, Any], messages: Sequence[Mapping[str, Any]]
    ) -> None:
        """Project server-owned task and Worker contracts into required evidence.

        User prose is retained verbatim; this does not pretend to infer every
        constraint from natural language. Structured constraints and acceptance
        criteria come from the same contract the Scheduler executes.
        """
        contract = runtime.get("_memoryTaskContract")
        if not isinstance(contract, Mapping):
            user_instructions = runtime.get("_memoryUserInstructions")
            if user_instructions is None:
                user_instructions = [
                    _message_content(message)
                    for message in messages
                    if is_user_input(message)
                ]
            contract = {
                "request": str(user_instructions[-1]) if user_instructions else "",
                "constraints": runtime.get(
                    "_taskContextConstraints", runtime.get("taskConstraints", ())
                ),
                "acceptance_criteria": runtime.get("acceptanceCriteria", ()),
                "expected_outputs": runtime.get("expectedOutputs", ()),
                "contract_revision": runtime.get("runtimeContractRevision", 1),
                "user_instructions": user_instructions,
            }
        revision = contract.get("contract_revision", 1)
        values = {
            "runtime.task_request": str(contract.get("request", "")),
            "runtime.task_rules": stable_json(
                {
                    "constraints": contract.get("constraints", ()),
                    "acceptance_criteria": contract.get("acceptance_criteria", ()),
                    "expected_outputs": contract.get("expected_outputs", ()),
                }
            ),
        }
        for key, text in values.items():
            if not text:
                continue
            if key == "runtime.task_rules" and not any(json.loads(text).values()):
                active = self.authority.active_decisions(
                    tenant_id=self.tenant_id, task_id=self.task_id
                )
                if not any(decision.decision_key == key for decision in active):
                    continue
            decision = self.record_decision(
                key=key,
                text=text,
                structured_value={"contract_revision": revision},
                idempotency_key=f"{self.run_id}:contract:{key}:{revision}:{content_hash_text(text)}",
            )
            if decision.status != MemoryStatus.ACTIVE:
                raise ValueError(
                    "task contract has been superseded; refresh the Worker contract"
                )

        worker_contract = runtime.get("_memoryWorkerContract")
        if not isinstance(worker_contract, Mapping):
            worker_contract = {
                "objective": _latest_user_text(messages),
                "acceptance_criteria": contract.get("acceptance_criteria", ()),
            }
        self._step_acceptance_criteria = {
            "criteria": list(worker_contract.get("acceptance_criteria", ()) or ())
        }
        payloads = [("worker_contract", dict(worker_contract))]
        instructions = contract.get(
            "user_instructions", runtime.get("_memoryUserInstructions")
        )
        if instructions is None:
            instructions = [
                _message_content(message)
                for message in messages
                if is_user_input(message)
            ]
        payloads.extend(
            ("user_instruction", str(instruction))
            for instruction in instructions
            if instruction
        )
        results = runtime.get("_memoryDependencyResults", {})
        for node_id in worker_contract.get("dependency_result_refs", ()):
            result = results.get(node_id) if isinstance(results, Mapping) else None
            # Consultation visibility is not a successful DAG dependency. The
            # Scheduler can return a failed review so the primary can repair it;
            # retain its native disposition without granting completion credit.
            consultation = (
                isinstance(result, Mapping)
                and worker_contract.get("node_id") == "primary"
                and node_id in worker_contract.get("consultation_result_refs", ())
                and result.get("node_id") == node_id
                and isinstance(runtime.get("_runtimeActiveRunId"), str)
                and bool(runtime["_runtimeActiveRunId"])
                and result.get("run_id") == runtime.get("_runtimeActiveRunId")
                and bool(result.get("attempt_id"))
                and (result.get("consultation_state"), result.get("status")) in {
                    ("FAILED_TERMINAL", AgentResultStatus.FAILED_TERMINAL.value),
                    ("CANCELED", AgentResultStatus.CANCELED.value),
                }
            )
            if (
                not isinstance(result, Mapping)
                or (result.get("status") != AgentResultStatus.SUCCEEDED.value and not consultation)
            ):
                raise ValueError(f"required Worker result is unavailable: {node_id}")
            dependency_result = dict(result)
            # Saved-note receipts deliberately do not enter the knowledge index.
            # Give the declared successor their exact current-run audit pointer,
            # so review does not have to search old summaries for a missing ID.
            if result.get("node_id") == node_id and isinstance(result.get("run_id"), str):
                receipt = self.handoff_notes.dependency_receipt(node_id=node_id, run_id=result["run_id"],
                    attempt_id=str(result.get("attempt_id") or ""))
                if receipt:
                    dependency_result["handoff_note_receipt"] = receipt
            payloads.append((f"worker_result:{node_id}", dependency_result))
        sources = []
        for kind, payload in payloads:
            text = payload if kind == "user_instruction" else stable_json(payload)
            digest = content_hash_text(text)
            event = AgentEvent(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                run_id=self.run_id,
                event_type=(
                    EventType.USER_INPUT
                    if kind == "user_instruction"
                    else EventType.STATE_UPDATED
                ),
                content_text=text,
                content_json={"contract_source": kind},
                content_hash=digest,
                token_count=self.token_budget.estimate_text(text),
                idempotency_key=f"{self.run_id}:{self.step_namespace}:{kind}:{digest}",
            )
            persisted, _ = self._persist_event(event)
            sources.append(persisted.id)
        self._step_contract_sources = sources

    def begin_step(
        self,
        sequence_no: int,
        *,
        user_input: str = "",
        acceptance_criteria: Mapping[str, Any] | None = None,
        phase: str = "EXECUTION",
    ) -> str:
        with self._lock:
            if user_input:
                self.code_focus.user_input = user_input
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
                    acceptance_criteria=(
                        acceptance_criteria
                        if acceptance_criteria is not None
                        else self._step_acceptance_criteria
                    ),
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
                for source_id in self._step_contract_sources:
                    self.add_dependency(
                        step_id=self.current_step_id,
                        dependency_type=DependencyType.EVENT,
                        dependency_id=source_id,
                        required=True,
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
        chunks = self._conversation_chunks(event, role="assistant") if memory else []
        self._persist_event(
            event,
            memory=memory,
            memories=chunks,
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
        output: str, *, tool_name: str, ok: bool, enhanced: bool = True, query: str = ""
    ) -> tuple[str, dict[str, Any], list[str]]:
        return output_summary(output, tool_name=tool_name, ok=ok, enhanced=enhanced, query=query)

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
        read_sha256: str = "",
    ) -> tuple[ToolResultEnvelope, str]:
        """Serialize concurrent receipts before touching one authority connection."""
        with self._lock:
            return self._capture_tool_result(
                call=call,
                output=output,
                ok=ok,
                force_inline=force_inline,
                read_sha256=read_sha256,
            )

    def _capture_tool_result(
        self,
        *,
        call: Mapping[str, Any],
        output: str,
        ok: bool,
        force_inline: bool = False,
        read_sha256: str = "",
    ) -> tuple[ToolResultEnvelope, str]:
        """Persist a real tool receipt before returning a compact prompt value.

        Large outputs are never shortened unless their exact bytes first exist
        in the primary artifact store or the authenticated encrypted spool.
        """
        call_id = str(call.get("id", ""))
        tool_name = str(call.get("toolName", "unknown"))
        is_navigation_receipt = tool_name in MEMORY_NAVIGATION_TOOL_NAMES or is_export_read(
            call, output, root=self.config.export_root, cwd=self.cwd
        )
        raw = output.encode("utf-8")
        digest = content_hash_bytes(raw)
        summary, statistics, sample = self._tool_summary(
            output, tool_name=tool_name, ok=ok, enhanced=self.config.tool_result_projection_enabled,
            query=self.context_rotation.user_input or self.goal,
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
            "context_projection_allowed": not force_inline and not is_navigation_receipt,
            **self.code_focus.observe(call, output, ok=ok and not is_navigation_receipt, read_sha256=read_sha256),
        }
        memory: MemoryRecord | None = None
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
        persisted_event, _ = self._persist_event(
            event,
            state_patch=state_patch or None,
            artifact=artifact,
            memory=memory,
            memories=chunk_memories,
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
            # Worker's completed Step in SQLite.
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
        # A checkpoint without a state patch is an observation, not a state
        # transition. Rebuild it from fresh authority after a sibling commits.
        for attempt in range(3):
            try:
                return self._create_checkpoint(reason=reason)
            except AuthorityRecoveringError as error:
                if attempt == 2 or not isinstance(error.__cause__, StateVersionConflict):
                    raise
        raise AssertionError("unreachable")

    def _create_checkpoint(self, *, reason: str) -> CheckpointRecord:
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
        if checkpoint is None or checkpoint.snapshot.get("checkpoint_type", "TASK_STATE") != "TASK_STATE":
            return None
        expected = content_hash_text(stable_json(checkpoint.snapshot))
        if expected != checkpoint.content_hash:
            raise ArtifactIntegrityError("checkpoint content hash mismatch")
        self.current_mode = checkpoint.memory_mode.canonical
        self.current_step_id = checkpoint.current_step_id
        metrics = checkpoint.snapshot.get("metrics", {})
        if isinstance(metrics, dict):
            allowed = TaskMetrics.__dataclass_fields__.keys()
            self.metrics = TaskMetrics(
                **{key: value for key, value in metrics.items() if key in allowed}
            )
            if not self.metrics.counter_run_id:
                self.metrics.counter_run_id = checkpoint.run_id
        initialize_rerank_state(self.metrics, checkpoint.memory_mode)
        self._last_checkpoint = checkpoint
        self.context_borrowing.stop()
        self._request_budget = None
        if preserve_active_step and active_step and not active_step_finalized:
            # A model-visible recovery read must not orphan the currently
            # running Step. It restores the checkpoint view while keeping the
            # live execution boundary that will receive the real tool receipt.
            self.current_step_id = active_step
            self._step_finalized = False
        else:
            self._step_finalized = True
        return checkpoint

    def prepare_model_messages(self, messages, **options):
        budget = options.get("request_budget")
        normal = getattr(budget, "normal_budget", budget)
        recovery = self.context_borrowing
        selected = recovery.request_budget(normal) if recovery.handoff_only else normal
        # Full request -> early/failed-capacity handoff -> borrowed handoff.
        # These are local measurements, not extra model requests. Restoration
        # remains pending until ProviderCall admits the final complete body.
        for attempt in range(3):
            try:
                prepared, packet = self._prepare_model_messages(messages, **{**options, "request_budget": selected})
                if recovery.restoring and self._request_budget is not normal:
                    # An explicit rotation may only become visible during
                    # this build. Discard its loan-sized planning view too.
                    return self._prepare_model_messages(messages, **{**options, "request_budget": normal})
                if recovery.begin_preparation(packet, normal):
                    # Rebuild with the actual restricted tool schemas and
                    # handoff guidance before the first model dispatch.
                    selected = recovery.request_budget(normal)
                    continue
                return prepared, packet
            except ContextBudgetExceeded as error:
                if recovery.restoring and self._request_budget is not normal:
                    return self._prepare_model_messages(messages, **{**options, "request_budget": normal})
                if recovery.restoring or attempt == 2:
                    raise
                if not recovery.handoff_only:
                    if not recovery.begin_handoff(error, normal):
                        raise
                elif not recovery.activate(error, selected):
                    raise
                selected = recovery.request_budget(normal)

    def _prepare_model_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        step_id: str | None = None,
        phase_boundary: bool = False,
        required_facets: Sequence[str] = (),
        acceptance_criteria: Mapping[str, Any] | None = None,
        system_context: str = "",
        request_budget: Any | None = None,
        _rotation_retry: bool = False,
        bind_history: bool = False,
    ) -> tuple[list[dict[str, Any]], ContextPacket]:
        self._request_budget = request_budget
        self.maintain_memories()
        self.context_rotation.capture(messages, user_input=_latest_user_text(messages) or self.goal, bind_history=bind_history)
        messages = self.context_rotation.request_messages
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
        # Use the same whole-exchange tail selection as model_messages(). This
        # proves source presence in the actual request, not merely in history.
        visible_tail = self.token_budget.recent_messages(
            [message for message in messages if message.get("role") != "system"],
            token_budget=(0 if self.config.elastic_context_budget_enabled else self.config.recent_event_token_budget),
            retain_latest_user=True,
        )
        inline_content_hashes: dict[str, list[str]] = {}
        for message in visible_tail:
            role, content = message.get("role"), message.get("content")
            if not isinstance(content, str) or not content:
                continue
            event_type = (
                EventType.USER_INPUT if is_user_input(message) else
                (EventType.TOOL_ERROR if message.get("isError") else EventType.TOOL_RESULT)
                if role == "tool_result" else None
            )
            if event_type is not None:
                inline_content_hashes.setdefault(event_type.value, []).append(content_hash_text(content))
        packet = self.context_builder.build(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            run_id=self.run_id,
            step_id=step_id or self.current_step_id or f"{self.run_id}.step.0",
            user_input=_latest_user_text(messages) or self.goal,
            metrics=self.metrics,
            current_mode=self.current_mode,
            model_context_limit=self.model_context_limit,
            system_context=system_context,
            acceptance_criteria=(
                acceptance_criteria
                if acceptance_criteria is not None
                else self._step_acceptance_criteria
            ),
            required_facets=required_facets,
            phase_boundary=phase_boundary,
            inline_content_hashes=inline_content_hashes,
            history_messages=messages,
            tool_schema_tokens=int((self.last_packet.debug_metadata if self.last_packet else {}).get(
                "actual_tool_schema_tokens", 0)),
            request_budget=request_budget,
        )
        packet.debug_metadata["context_build_latency_ms"] = (
            time.perf_counter() - context_started
        ) * 1_000
        rotation = packet.debug_metadata.get("context_rotation", {})
        if rotation.get("status") == "active":
            self.context_borrowing.rotated(rotation)
        if self.context_rotation.enabled:
            packet.debug_metadata["history_journal"] = {
                "basis": self.context_rotation.frame.get("history_basis"),
                "events": len(self.context_rotation.journal.records),
                "rebuilt_view": self.context_rotation.journal.rebuilt,
                "tools_reexecuted": False,
            }
        packet.debug_metadata["memory_lifecycle"] = dict(self._last_maintenance_report)
        if self.config.tool_result_projection_enabled and packet.memory_mode != MemoryMode.INLINE:
            packet.debug_metadata["tool_result_projection"] = prepare_projection(
                self, packet, self.context_builder.recent_history(packet, messages),
                user_input=_latest_user_text(messages) or self.goal)
            preview_sources = {item["source_event_id"] for item in packet.debug_metadata["tool_result_projection"]["replacements"]}
            # Previously deduplicated optional cards now have a bounded source
            # receipt, not full inline evidence. Facet obligations and semantic
            # records were excluded from this deduplication in the first place.
            covered = packet.debug_metadata["inline_source_covered_sources"]
            packet.debug_metadata["preview_source_covered_memory_ids"] = sorted(
                key for key, source in covered.items() if source in preview_sources)
            packet.debug_metadata["inline_source_covered_memory_ids"] = sorted(
                key for key, source in covered.items() if source not in preview_sources)
        else:
            self.result_reclamation.clear()
        from .inline_sources import prepare_render_coverage
        packet.debug_metadata["render_inline_source_proofs"] = prepare_render_coverage(self, packet)
        previous_mode = self.current_mode
        self.current_mode = packet.memory_mode
        self.last_packet = packet
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
        if self.controls_context:
            packet = self.context_builder.fit_request_budget(packet, messages, request_budget)
            self.last_packet = packet
        # Decide from the input actually being sent after optional paging. The
        # canonical transcript remains searchable but is not all in this request.
        self._update_request_pressure(packet, messages)
        self.context_borrowing.mark_packet(packet)
        auto_rotation = self.context_utilization.try_auto_rotate(packet) if not _rotation_retry else False
        if auto_rotation:
            self.context_borrowing.rotated(auto_rotation)
            prepared, recovered = self.prepare_model_messages(messages, step_id=step_id, phase_boundary=phase_boundary,
                required_facets=required_facets, acceptance_criteria=acceptance_criteria,
                system_context=system_context, request_budget=request_budget, _rotation_retry=True, bind_history=bind_history)
            recovered.debug_metadata["context_auto_rotation"] = auto_rotation
            return prepared, recovered
        prepared = (
            self.context_builder.model_messages(packet, messages)
            if self.controls_context
            else [dict(message) for message in messages]
        )
        if packet.memory_mode is MemoryMode.INLINE:
            self.context_utilization.before_request(packet, step_id=step_id or self.current_step_id)
        self.enforce_provider_budget(prepared)
        # A real capacity failure must reach the restricted handoff fallback
        # even when the earlier, ordinary protection budget has been used up.
        if packet.memory_mode is not MemoryMode.INLINE:
            self.context_utilization.before_request(packet, step_id=step_id or self.current_step_id)
        if self.controls_context:
            self._touch_memories([
                item.item_id for item in (
                    *packet.mandatory_dependencies, *packet.working_set,
                    *packet.supplemental_memories,
                )
            ])
        return prepared, packet

    def _update_request_pressure(self, packet, messages):
        if self._request_budget is None or not self.controls_context:
            return
        from .context_utilization import pressure_status
        pressure = packet.debug_metadata["context_pressure"]
        measured = self._request_budget.measure(self.context_builder.model_messages(packet, messages))
        pressure.setdefault("unpaged_input_tokens", pressure["admission_input_tokens"])
        pressure["admission_input_tokens"] = measured
        limit = max(1, pressure.get("pressure_window", pressure["input_limit"]))
        pressure["effective_pressure_ratio"] = min(2.0, pressure["admission_input_tokens"] / limit)
        pressure["forecast_pressure_ratio"] = min(2.0,
            (pressure["admission_input_tokens"] + pressure["growth_reserve_tokens"]) / limit)
        packet.debug_metadata["context_utilization"] = pressure_status(self.config, pressure)

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
        if self._request_budget is not None:
            if tool_definitions:
                from minicode.tooling import ToolRegistry
                self._request_budget.tools = ToolRegistry(list(tool_definitions))
            provider_input_tokens = self._request_budget.measure(messages)
            input_limit = self._request_budget.input_limit
        if self.last_packet is not None:
            self.last_packet.debug_metadata.update(
                {
                    "actual_prepared_input_tokens": message_tokens,
                    "actual_tool_schema_tokens": tool_schema_tokens,
                    "reserved_tool_tokens": reserved_tool_tokens,
                    "actual_provider_input_tokens": provider_input_tokens,
                    "hard_input_token_limit": input_limit,
                    "provider_budget_exceeded": provider_input_tokens > input_limit,
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
        idempotency_key: str = "",
    ) -> DecisionRecord:
        value = dict(structured_value or {})
        decision_digest = content_hash_text(
            stable_json({"key": key, "text": text, "value": value})
        )
        idempotency_key = (
            idempotency_key
            or f"{self.current_step_id}:decision:{key}:{decision_digest}"
        )
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
        active_decision = {
            "decision_id": decision.id,
            "source_event_id": event.id,
            "status": MemoryStatus.ACTIVE.value,
            "content_hash": decision_digest,
        }
        try:
            persisted_event, _ = self._persist_event(
                event,
                state_patch={"_active_decisions_upsert": {key: active_decision}},
                decision=decision,
                memory=memory,
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
        expected_contract_hash: str | None = None,
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
            if (
                high_risk
                and expected_contract_hash is not None
                and dependency_contract_hash(dependencies, state)
                != expected_contract_hash
            ):
                errors.append(
                    "required context changed after model input; rebuild context before execution"
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
        return _select_bytes(
            self._event_source_payload(event_id), start=start, end=end, unit=unit
        )

    def _event_source_payload(self, event_id: str) -> bytes:
        """Resolve and verify the same source for event length and range reads."""
        event = self.read_event(event_id)
        if event.artifact_uri:
            artifact = self._artifact_from_event(event)
            if artifact is None or artifact.content_hash != event.content_hash:
                raise ArtifactIntegrityError("event artifact registry/hash mismatch")
            payload = self.read_artifact_range(
                artifact.id, start=0, end=artifact.size_bytes, unit="BYTE"
            )
        else:
            payload = event.content_text.encode("utf-8")
            # An empty tool result is still a complete, verifiable original.
            # Structured events use the JSON fallback only when its hash is
            # actually needed to resolve the event's source representation.
            if not payload and content_hash_bytes(payload) != event.content_hash:
                payload = stable_json(event.content_json).encode("utf-8")
        if content_hash_bytes(payload) != event.content_hash:
            raise ArtifactIntegrityError("event source content hash mismatch")
        return payload

    def event_source_end(self, event_id: str, *, unit: str) -> int:
        payload = self._event_source_payload(event_id)
        normalized = unit.upper()
        if normalized == "BYTE":
            return len(payload)
        text = payload.decode("utf-8")
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
                payload = self._event_source_payload(event.id)
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
        selected = _select_bytes(payload, start=start, end=end, unit=unit)
        self._touch_memories([memory_id])
        return selected

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

    def maintain_memories(self, *, force: bool = False) -> dict[str, Any]:
        """Age this task's supplemental memories independently of legacy context."""
        from minicode.memory_lifecycle import MAINTENANCE_INTERVAL_SECONDS

        with self._lock:
            now = time.monotonic()
            if (not force and self._last_maintenance_at is not None
                    and now - self._last_maintenance_at < MAINTENANCE_INTERVAL_SECONDS):
                return dict(self._last_maintenance_report)
            try:
                report = self.authority.maintain_memories(
                    tenant_id=self.tenant_id, task_id=self.task_id, now=now_epoch(),
                )
            except Exception as exc:
                logger.warning("Paged memory lifecycle maintenance failed: %s", exc)
                self._last_maintenance_report = {"error": type(exc).__name__}
                return dict(self._last_maintenance_report)
            self._last_maintenance_report = report
            self._last_maintenance_at = None if report.get("has_more") else now
            return dict(report)

    def _touch_memories(self, memory_ids: Sequence[str]) -> None:
        if not memory_ids:
            return
        try:
            self.authority.touch_memories(
                tenant_id=self.tenant_id, task_id=self.task_id,
                memory_ids=memory_ids, now=now_epoch(),
            )
        except Exception as exc:
            logger.warning("Paged memory access persistence failed: %s", exc)

    def search(self, query: str, *, limit: int | None = None) -> list[MemoryRecord]:
        self.maintain_memories()
        request = MemorySearchRequest(
            tenant_id=self.tenant_id,
            task_id=self.task_id,
            query=retrieval_query(query),
            candidate_limit=limit or self.config.candidate_limit,
        )
        records = [
            hit.record
            for hit in self.retriever.search(
                request,
            )
        ]
        self._touch_memories([record.id for record in records])
        return records


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
        try:
            pending = self.authority.list_artifacts(
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                statuses=(ArtifactStatus.PENDING_UPLOAD.value,),
            )
        except Exception:  # noqa: BLE001 - telemetry cannot alter execution.
            pending = []

        selected_count = int(debug.get("selected_memory_count", 0))
        working_count = int(debug.get("working_set_item_count", 0))
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "step_id": self.current_step_id,
            "memory_mode": self.current_mode.value,
            "mode_change_reason": debug.get("mode_reason", ""),
            "rerank_admission": debug.get("rerank_admission", {}),
            "context_utilization": debug.get("context_utilization", {}),
            "context_tool_forecast": debug.get("context_tool_forecast", {}),
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
            "supplemental_memory_tokens": section_tokens("supplemental_memories"),
            "handoff_note_tokens": section_tokens("handoff_notes"),
            "handoff_note": dict(debug.get("handoff_note", {"status": "disabled"})),
            "context_rotation": dict(debug.get("context_rotation", {"status": "disabled"})),
            "context_pressure": dict(debug.get("context_pressure", {})),
            "context_allocation": dict(debug.get("context_allocation", {})),
            "code_focus": dict(debug.get("code_focus", {"status": "disabled"})),
            "prepared_message_tokens_estimated": debug.get("actual_prepared_input_tokens"),
            "tool_schema_tokens_estimated": debug.get("actual_tool_schema_tokens"),
            "provider_input_tokens_estimated": debug.get("actual_provider_input_tokens"),
            "provider_budget_exceeded": debug.get("provider_budget_exceeded", False),
            "lexical_latency_ms": debug.get("lexical_latency_ms", 0.0),
            "tool_result_projection": {key: value for key, value in debug.get("tool_result_projection", {}).items()
                                       if key != "replacements"},
            "raw_page_tokens": int(self._operational_metrics["raw_page_tokens"]),
            "retrieved_candidate_count": int(debug.get("retrieved_candidate_count", 0)),
            "selected_memory_count": selected_count,
            "adaptive_retrieval": dict(debug.get("adaptive_retrieval", {})),
            "rerank": dict(debug.get("rerank", {"status": "not_requested"})),
            "duplicate_memory_count": int(debug.get("duplicate_memory_count", 0)),
            "superseded_memory_count": 0,
            "page_fault_count": int(self._operational_metrics["page_fault_count"]),
            "working_set_hit_rate": working_count
            / max(1, working_count + selected_count),
            "artifact_read_latency_ms": float(
                self._operational_metrics["artifact_read_latency_ms"]
            ),
            "context_build_latency_ms": float(
                debug.get("context_build_latency_ms", 0.0)
            ),
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
            "authority_write_latency_ms": float(
                self._operational_metrics["authority_write_latency_ms"]
            ),
            "authority_availability_state": (
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
        effective_state = self.storage_health
        if not authority_ok:
            effective_state = StorageHealth.AUTHORITY_UNAVAILABLE
        elif not artifact_ok:
            effective_state = StorageHealth.ARTIFACT_DEGRADED
        return {"state": effective_state.value, "authority": authority_ok,
                "artifact_store": artifact_ok, "task_id": self.task_id,
                "run_id": self.run_id, "mode": self.current_mode.value,
                "metrics": self.metrics_snapshot()}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.context_borrowing.stop()
        builder = getattr(self, "context_builder", None)
        if builder is not None and builder.reranker is not None:
            builder.reranker.close()
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
    "AuthorityRecoveringError",
    "AuthorityUnavailableError",
    "ContextBudgetExceeded",
    "PagedMemoryError",
    "PagedMemoryRuntime",
    "PreflightBlockedError",
]
