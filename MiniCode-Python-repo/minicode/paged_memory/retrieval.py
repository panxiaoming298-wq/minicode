"""Explicit dependency resolution, hybrid retrieval and phase working set."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from .models import (
    MEMORY_NAVIGATION_TOOL_NAMES,
    ArtifactStatus,
    ContextItem,
    DependencyRecord,
    DependencyType,
    MemoryHit,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    TaskStateSnapshot,
    content_hash_text,
)
from .policy import TokenBudgetManager


def merge_state_dependencies(
    dependencies: Sequence[DependencyRecord],
    *,
    state: TaskStateSnapshot | None,
    tenant_id: str,
    task_id: str,
    step_id: str,
) -> list[DependencyRecord]:
    """Make active constraints and decisions implicit required dependencies.

    The task-state projection is useful for navigation, but high-risk execution
    must resolve the immutable source behind every active rule.  This helper is
    shared by context construction and preflight so their coverage accounting
    cannot drift apart.
    """

    merged = list(dependencies)

    def require_existing(
        dependency_types: set[DependencyType], dependency_ids: set[str]
    ) -> bool:
        for index, dependency in enumerate(merged):
            if (
                dependency.dependency_type in dependency_types
                and dependency.dependency_id in dependency_ids
            ):
                if not dependency.required:
                    merged[index] = replace(dependency, required=True)
                return True
        return False

    if state is None:
        return merged

    for raw in state.active_constraints:
        if not isinstance(raw, dict):
            continue
        memory_id = str(raw.get("memory_id", "")).strip()
        if not memory_id or require_existing(
            {DependencyType.CONSTRAINT, DependencyType.MEMORY}, {memory_id}
        ):
            continue
        merged.append(
            DependencyRecord(
                tenant_id=tenant_id,
                task_id=task_id,
                step_id=step_id,
                dependency_type=DependencyType.CONSTRAINT,
                dependency_id=memory_id,
                required=True,
            )
        )

    for decision_key, raw in state.active_decisions.items():
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status", MemoryStatus.ACTIVE.value)).upper()
        if status not in {MemoryStatus.ACTIVE.value, MemoryStatus.OPEN.value}:
            continue
        decision_id = str(raw.get("decision_id") or raw.get("id") or "").strip()
        aliases = {str(decision_key).strip(), decision_id} - {""}
        if not aliases or require_existing({DependencyType.DECISION}, aliases):
            continue
        merged.append(
            DependencyRecord(
                tenant_id=tenant_id,
                task_id=task_id,
                step_id=step_id,
                dependency_type=DependencyType.DECISION,
                dependency_id=decision_id or str(decision_key),
                required=True,
            )
        )

    merged.sort(
        key=lambda value: (
            not value.required,
            value.dependency_depth,
            value.created_at,
        )
    )
    return merged


class DependencyResolver:
    def __init__(
        self,
        authority: Any,
        *,
        max_depth: int = 3,
        source_validator: Callable[[DependencyRecord], bool] | None = None,
    ) -> None:
        self.authority = authority
        self.max_depth = max_depth
        self.source_validator = source_validator

    def resolve_closure(
        self,
        *,
        tenant_id: str,
        task_id: str,
        step_id: str,
    ) -> list[DependencyRecord]:
        pending: deque[tuple[str, int]] = deque([(step_id, 0)])
        visited_steps: set[str] = set()
        seen: set[tuple[str, str]] = set()
        resolved: list[DependencyRecord] = []
        while pending:
            current_step, depth = pending.popleft()
            if current_step in visited_steps or depth > self.max_depth:
                continue
            visited_steps.add(current_step)
            for dependency in self.authority.dependencies_for_step(
                tenant_id=tenant_id, task_id=task_id, step_id=current_step
            ):
                key = (dependency.dependency_type.value, dependency.dependency_id)
                if key not in seen:
                    dependency.dependency_depth = depth
                    resolved.append(dependency)
                    seen.add(key)
                if (
                    dependency.dependency_type == DependencyType.STEP
                    and depth < self.max_depth
                ):
                    pending.append((dependency.dependency_id, depth + 1))
        resolved.sort(
            key=lambda value: (
                not value.required,
                value.dependency_depth,
                value.created_at,
            )
        )
        return resolved

    def load_items(
        self,
        *,
        tenant_id: str,
        task_id: str,
        dependencies: Sequence[DependencyRecord],
    ) -> tuple[list[ContextItem], list[str], list[str]]:
        items: list[ContextItem] = []
        missing: list[str] = []
        unresolved_sources: list[str] = []
        for dependency in dependencies:
            item = self._load_one(tenant_id, task_id, dependency)
            if item is None:
                if dependency.required:
                    missing.append(dependency.dependency_id)
                continue
            if dependency.required:
                kind = dependency.dependency_type
                pointer_required = kind in {
                    DependencyType.DECISION,
                    DependencyType.MEMORY,
                    DependencyType.CONSTRAINT,
                }
                unresolved = pointer_required and not (
                    item.source_event_id or item.artifact_id
                )
                unresolved = unresolved or (
                    kind == DependencyType.ARTIFACT
                    and item.status != ArtifactStatus.ACTIVE.value
                )
                unresolved = unresolved or (
                    kind == DependencyType.STEP and item.status != "COMPLETED"
                )
                if self.source_validator is not None:
                    unresolved = unresolved or not self.source_validator(dependency)
                if unresolved:
                    unresolved_sources.append(dependency.dependency_id)
            items.append(item)
        return items, missing, unresolved_sources

    def _load_one(
        self, tenant_id: str, task_id: str, dependency: DependencyRecord
    ) -> ContextItem | None:
        kind = dependency.dependency_type
        dependency_id = dependency.dependency_id
        if kind == DependencyType.EVENT:
            event = self.authority.get_event(
                tenant_id=tenant_id, task_id=task_id, event_id=dependency_id
            )
            if event is None:
                return None
            text = (
                event.content_text
                or f"[{event.event_type.value}] artifact={event.artifact_uri}"
            )
            return ContextItem(
                item_id=event.id,
                item_type=event.event_type.value,
                text=text,
                priority=100 if dependency.required else 70,
                required=dependency.required,
                source_event_id=event.id,
                content_hash=event.content_hash,
                reason_loaded="explicit event dependency",
            )
        if kind == DependencyType.DECISION:
            decisions = self.authority.active_decisions(
                tenant_id=tenant_id, task_id=task_id
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
                return None
            return ContextItem(
                item_id=decision.id,
                item_type="DECISION",
                text=decision.decision_text,
                priority=100 if dependency.required else 75,
                required=dependency.required,
                source_event_id=decision.source_event_id,
                content_hash=content_hash_text(decision.decision_text),
                status=decision.status.value,
                reason_loaded="explicit active decision dependency",
            )
        if kind in {DependencyType.MEMORY, DependencyType.CONSTRAINT}:
            memories = self.authority.get_memories(
                tenant_id=tenant_id, task_id=task_id, memory_ids=[dependency_id]
            )
            if not memories:
                return None
            memory = memories[0]
            if memory.status not in {MemoryStatus.ACTIVE, MemoryStatus.OPEN}:
                return None
            return memory_to_item(
                memory,
                required=dependency.required,
                priority=100 if dependency.required else 70,
                reason=f"explicit {kind.value.lower()} dependency",
            )
        if kind == DependencyType.ARTIFACT:
            artifact = self.authority.get_artifact(
                tenant_id=tenant_id, task_id=task_id, artifact_id=dependency_id
            )
            if artifact is None or artifact.status not in {
                ArtifactStatus.ACTIVE,
                ArtifactStatus.PENDING_UPLOAD,
            }:
                return None
            return ContextItem(
                item_id=artifact.id,
                item_type="ARTIFACT",
                text=(
                    f"Artifact {artifact.id}: type={artifact.artifact_type}, media={artifact.media_type}, "
                    f"size={artifact.size_bytes}, hash={artifact.content_hash}, status={artifact.status.value}"
                ),
                priority=100 if dependency.required else 70,
                required=dependency.required,
                source_event_id=artifact.source_event_id,
                artifact_id=artifact.id,
                content_hash=artifact.content_hash,
                status=artifact.status.value,
                reason_loaded="explicit artifact dependency",
            )
        if kind == DependencyType.CHECKPOINT:
            checkpoint = self.authority.latest_checkpoint(
                tenant_id=tenant_id, task_id=task_id
            )
            if checkpoint is None or checkpoint.id != dependency_id:
                return None
            return ContextItem(
                item_id=checkpoint.id,
                item_type="CHECKPOINT",
                text=f"Checkpoint state version {checkpoint.state_version}; step={checkpoint.current_step_id}",
                priority=100 if dependency.required else 70,
                required=dependency.required,
                content_hash=checkpoint.content_hash,
                reason_loaded="explicit checkpoint dependency",
            )
        if kind == DependencyType.STEP:
            getter = getattr(self.authority, "get_step", None)
            step = (
                getter(tenant_id=tenant_id, task_id=task_id, step_id=dependency_id)
                if callable(getter)
                else None
            )
            if step is None:
                return None
            status = str(step.get("status", "UNKNOWN"))
            return ContextItem(
                item_id=dependency_id,
                item_type="STEP",
                text=f"Required predecessor step: {dependency_id}; status={status}",
                priority=95 if dependency.required else 65,
                required=dependency.required,
                content_hash=content_hash_text(f"{dependency_id}:{status}"),
                status=status,
                reason_loaded="explicit step dependency",
            )
        return None


class WorkingSetManager:
    def __init__(
        self, authority: Any, *, max_items: int = 32, ttl_seconds: int = 3600
    ) -> None:
        self.authority = authority
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds

    def get(self, *, tenant_id: str, task_id: str, phase: str) -> list[MemoryRecord]:
        getter = getattr(self.authority, "working_set_get", None)
        if not callable(getter):
            return []
        ids = getter(tenant_id=tenant_id, task_id=task_id, phase=phase)[
            : self.max_items
        ]
        records = self.authority.verify_active_memories(
            tenant_id=tenant_id, task_id=task_id, memory_ids=ids
        )
        return [
            record
            for record in records
            if not set(record.facets).intersection(MEMORY_NAVIGATION_TOOL_NAMES)
        ]

    def touch(
        self,
        *,
        tenant_id: str,
        task_id: str,
        phase: str,
        memory_ids: Sequence[str],
        state_version: int,
    ) -> None:
        toucher = getattr(self.authority, "working_set_touch", None)
        if callable(toucher):
            toucher(
                tenant_id=tenant_id,
                task_id=task_id,
                phase=phase,
                memory_ids=list(memory_ids)[: self.max_items],
                state_version=state_version,
                ttl_seconds=self.ttl_seconds,
            )

    def invalidate(
        self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str]
    ) -> None:
        invalidator = getattr(self.authority, "working_set_invalidate", None)
        if callable(invalidator):
            invalidator(tenant_id=tenant_id, task_id=task_id, memory_ids=memory_ids)


class HybridMemoryRetriever:
    def __init__(
        self, authority: Any, vector_index: Any | None = None, *, rrf_k: int = 60
    ) -> None:
        self.authority = authority
        self.vector_index = vector_index
        self.rrf_k = rrf_k
        self.last_lexical_latency_ms = 0.0
        self.last_vector_latency_ms = 0.0
        self.last_vector_error = ""
        self.last_vector_mismatch_ids: list[str] = []
        self.last_candidate_count = 0

    def reset_observation(self) -> None:
        self.last_lexical_latency_ms = 0.0
        self.last_vector_latency_ms = 0.0
        self.last_vector_error = ""
        self.last_vector_mismatch_ids = []
        self.last_candidate_count = 0

    def _search_vector(
        self, request: MemorySearchRequest
    ) -> tuple[list[Any], float, str]:
        """Run the optional lane without turning its failure into authority failure."""

        started = time.perf_counter()
        try:
            return (
                list(self.vector_index.search(request)),
                (time.perf_counter() - started) * 1_000,
                "",
            )
        except Exception as exc:  # noqa: BLE001 - SDKs expose unrelated error types.
            return (
                [],
                (time.perf_counter() - started) * 1_000,
                f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _record_matches_request(
        record: MemoryRecord, request: MemorySearchRequest
    ) -> bool:
        if record.status.value not in request.statuses:
            return False
        if (
            request.memory_types
            and record.memory_type.value not in request.memory_types
        ):
            return False
        return not set(request.excluded_facets).intersection(record.facets)

    @staticmethod
    def _vector_metadata_matches(hit: Any, record: MemoryRecord) -> bool:
        """Verify every vector field that can affect filtering or ranking."""

        return (
            str(getattr(hit, "content_hash", "")) == record.content_hash
            and str(getattr(hit, "status", "")) == record.status.value
            and str(getattr(hit, "memory_type", "")) == record.memory_type.value
            and str(getattr(hit, "phase", "")) == record.phase
            and str(getattr(hit, "run_id", "")) == record.run_id
            and set(getattr(hit, "facets", ())) == set(record.facets)
        )

    def search(
        self, request: MemorySearchRequest, *, use_vector: bool
    ) -> list[MemoryHit]:
        self.reset_observation()
        if not use_vector:
            lexical_started = time.perf_counter()
            lexical = self.authority.search_memories_lexical(request)
            self.last_lexical_latency_ms = (
                time.perf_counter() - lexical_started
            ) * 1_000
            self.last_candidate_count = len(lexical)
            return lexical

        if self.vector_index is None:
            lexical_started = time.perf_counter()
            lexical = self.authority.search_memories_lexical(request)
            self.last_lexical_latency_ms = (
                time.perf_counter() - lexical_started
            ) * 1_000
            self.last_candidate_count = len(lexical)
            self.last_vector_error = "vector index is not configured"
            return lexical

        # Start the optional vector lane first, then execute authority FTS on the
        # caller thread. The relational connection is not shared across workers.
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="paged-memory-vector"
        ) as executor:
            vector_future = executor.submit(self._search_vector, request)
            lexical_started = time.perf_counter()
            lexical = self.authority.search_memories_lexical(request)
            self.last_lexical_latency_ms = (
                time.perf_counter() - lexical_started
            ) * 1_000
            vector, self.last_vector_latency_ms, self.last_vector_error = (
                vector_future.result()
            )

        if self.last_vector_error:
            self.last_candidate_count = len(lexical)
            return lexical

        raw_ids = list(
            dict.fromkeys(
                [hit.record.id for hit in lexical]
                + [str(hit.memory_id) for hit in vector]
            )
        )
        self.last_candidate_count = len(raw_ids)
        verified = self.authority.verify_active_memories(
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            memory_ids=raw_ids,
        )
        eligible = {
            record.id: record
            for record in verified
            if self._record_matches_request(record, request)
        }

        valid_vector: list[Any] = []
        mismatches: list[str] = []
        for hit in vector:
            record = eligible.get(str(hit.memory_id))
            if record is None or not self._vector_metadata_matches(hit, record):
                mismatches.append(str(hit.memory_id))
                continue
            valid_vector.append(hit)
        self.last_vector_mismatch_ids = list(dict.fromkeys(mismatches))
        if self.last_vector_mismatch_ids:
            self.last_vector_error = (
                "vector candidate verification failed for "
                f"{len(self.last_vector_mismatch_ids)} item(s)"
            )

        scores: dict[str, float] = {}
        lexical_map = {
            hit.record.id: hit for hit in lexical if hit.record.id in eligible
        }
        for rank, hit in enumerate(lexical, start=1):
            if hit.record.id not in eligible:
                continue
            scores[hit.record.id] = scores.get(hit.record.id, 0.0) + 1.0 / (
                self.rrf_k + rank
            )

        # Preserve original vector ranks when a stale row is discarded. A bad
        # candidate must not promote every item below it in the same request.
        valid_vector_ids = {id(hit) for hit in valid_vector}
        for rank, hit in enumerate(vector, start=1):
            if id(hit) not in valid_vector_ids:
                continue
            scores[hit.memory_id] = scores.get(hit.memory_id, 0.0) + 1.0 / (
                self.rrf_k + rank
            )

        ids = sorted(scores, key=lambda memory_id: scores[memory_id], reverse=True)[
            : request.candidate_limit
        ]
        vector_scores = {hit.memory_id: hit.score for hit in valid_vector}
        results: list[MemoryHit] = []
        for memory_id in ids:
            record = eligible[memory_id]
            lexical_hit = lexical_map.get(record.id)
            results.append(
                MemoryHit(
                    record=record,
                    score=scores.get(record.id, 0.0),
                    lexical_score=lexical_hit.lexical_score if lexical_hit else 0.0,
                    vector_score=vector_scores.get(record.id, 0.0),
                    reason="hybrid-rrf-verified",
                )
            )
        return results


class MinimumCoverSelector:
    def __init__(self, token_budget: TokenBudgetManager) -> None:
        self.token_budget = token_budget

    def select(
        self,
        candidates: Sequence[MemoryHit],
        *,
        required_facets: Sequence[str],
        token_budget: int,
    ) -> list[MemoryRecord]:
        remaining = set(required_facets)
        selected: list[MemoryRecord] = []
        selected_terms: set[str] = set()
        used = 0
        pool = list(candidates)
        while pool:

            def value(hit: MemoryHit) -> tuple[float, float, float]:
                facets = set(hit.record.facets)
                gain = len(facets & remaining)
                terms = set(hit.record.preview_text.casefold().split())
                duplicate = len(terms & selected_terms) / max(
                    1, len(terms | selected_terms)
                )
                tokens = max(
                    1, self.token_budget.estimate_text(hit.record.preview_text)
                )
                return (float(gain), hit.score - duplicate * 0.4, -float(tokens))

            best = max(pool, key=value)
            pool.remove(best)
            record = best.record
            tokens = max(1, self.token_budget.estimate_text(record.preview_text))
            if used + tokens > token_budget:
                continue
            terms = set(record.preview_text.casefold().split())
            duplicate = len(terms & selected_terms) / max(
                1, len(terms | selected_terms)
            )
            if duplicate >= 0.90:
                continue
            selected.append(record)
            used += tokens
            selected_terms.update(terms)
            remaining.difference_update(record.facets)
        return selected


def memory_to_item(
    memory: MemoryRecord,
    *,
    required: bool = False,
    priority: int = 50,
    reason: str = "retrieval",
) -> ContextItem:
    return ContextItem(
        item_id=memory.id,
        item_type=memory.memory_type.value,
        text=memory.preview_text,
        priority=priority,
        required=required,
        source_event_id=memory.source_event_id,
        artifact_id=memory.artifact_id,
        source_range=memory.source_range,
        content_hash=memory.content_hash,
        status=memory.status.value,
        reason_loaded=reason,
        facets=tuple(memory.facets),
    )
