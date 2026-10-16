"""Explicit dependency resolution, keyword retrieval and phase working set."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from .lexical import lexical_tokens
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
    stable_json,
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
        if str(raw.get("status", "ACTIVE")).upper() not in {"ACTIVE", "OPEN"}:
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


def dependency_contract_hash(
    dependencies: Sequence[DependencyRecord],
    state: TaskStateSnapshot | None = None,
) -> str:
    """Fingerprint the required read set, excluding unrelated task progress."""
    required = sorted(
        (
            dependency.dependency_type.value,
            dependency.dependency_id,
            dependency.closure_truncated,
        )
        for dependency in dependencies
        if dependency.required
    )
    # A dependency may name a decision key rather than its immutable version
    # ID. Include the active version map so key aliases cannot bypass the fence.
    versions = {
        key: {
            field: raw.get(field)
            for field in ("id", "decision_id", "source_event_id", "status")
        }
        for key, raw in (state.active_decisions.items() if state else ())
        if isinstance(raw, dict)
        and str(raw.get("status", "ACTIVE")).upper() in {"ACTIVE", "OPEN"}
    }
    return content_hash_text(stable_json({"required": required, "decisions": versions}))


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
        resolved_by_key: dict[tuple[str, str], DependencyRecord] = {}
        while pending:
            current_step, depth = pending.popleft()
            if current_step in visited_steps or depth > self.max_depth:
                continue
            visited_steps.add(current_step)
            for dependency in self.authority.dependencies_for_step(
                tenant_id=tenant_id, task_id=task_id, step_id=current_step
            ):
                key = (dependency.dependency_type.value, dependency.dependency_id)
                dependency = replace(dependency, dependency_depth=depth)
                previous = resolved_by_key.get(key)
                if previous is not None:
                    dependency = replace(
                        previous,
                        required=previous.required or dependency.required,
                    )
                resolved_by_key[key] = dependency
                if (
                    dependency.dependency_type == DependencyType.STEP
                    and depth < self.max_depth
                ):
                    pending.append((dependency.dependency_id, depth + 1))
        resolved = list(resolved_by_key.values())
        for dependency in resolved:
            # A longer path may hit the limit after a shorter path already
            # expanded the same Step. Only genuinely unexpanded edges are gaps.
            if (
                dependency.dependency_type == DependencyType.STEP
                and dependency.dependency_id not in visited_steps
            ):
                dependency.closure_truncated = bool(
                    self.authority.dependencies_for_step(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        step_id=dependency.dependency_id,
                    )
                )
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
                unresolved = dependency.closure_truncated or (
                    pointer_required and not (item.source_event_id or item.artifact_id)
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
                facets=(
                    ("user_instruction",)
                    if event.content_json.get("contract_source") == "user_instruction"
                    else ()
                ),
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
                facets=(
                    ("task_contract",)
                    if decision.decision_key.startswith("runtime.task_")
                    else ()
                ),
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


class LexicalMemoryRetriever:
    """Scoped SQLite keyword retrieval with a bounded query deadline."""

    def __init__(
        self,
        authority: Any,
        *,
        lexical_candidate_limit: int = 0,
        timeout_seconds: float = 3.0,
    ) -> None:
        self.authority = authority
        self.lexical_candidate_limit = lexical_candidate_limit
        self.timeout_seconds = timeout_seconds
        self.reset_observation()

    def reset_observation(self) -> None:
        self.last_lexical_latency_ms = 0.0
        self.last_candidate_count = 0
        self.last_refill_count = 0
        self.last_has_more = False
        self.last_lexical_has_more = False
        self.last_lexical_error = ""

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

    def search(
        self, request: MemorySearchRequest, *, lexical_limit: int | None = None
    ) -> list[MemoryHit]:
        from .search_budget import authority_deadline, has_more

        self.reset_observation()
        if request.candidate_limit <= 0:
            return []
        deadline = min(
            time.monotonic() + self.timeout_seconds,
            request.deadline_monotonic or float("inf"),
        )
        lexical_request = replace(
            request,
            deadline_monotonic=deadline,
            candidate_limit=lexical_limit or self.lexical_candidate_limit or request.candidate_limit,
        )
        started = time.perf_counter()
        try:
            with authority_deadline(self.authority, deadline):
                hits = self.authority.search_memories_lexical(lexical_request)
                self.last_candidate_count = len(hits)
                self.last_lexical_has_more = has_more(hits, lexical_request.candidate_limit)
                self.last_has_more = (
                    self.last_lexical_has_more or len(hits) > request.candidate_limit
                )
                return hits[: request.candidate_limit]
        except TimeoutError:
            self.last_lexical_error = "retrieval_deadline"
            return []
        finally:
            self.last_lexical_latency_ms = (time.perf_counter() - started) * 1000


def _similarity(left: str, right: str) -> float:
    # Preserve changed numbers, identifiers, operators and negation. Compare
    # against individual selected cards, not their ever-growing term union.
    protected = r"[A-Za-z0-9_]+(?:[.\-/][A-Za-z0-9_]+)*|[<>=!]+|不|无|未|非|禁止|允许|必须|仅|启用|禁用"
    if re.findall(protected, left.casefold()) != re.findall(
        protected, right.casefold()
    ):
        return 0.0
    a, b = set(lexical_tokens(left)), set(lexical_tokens(right))
    return len(a & b) / max(1, len(a | b))


class MinimumCoverSelector:
    def __init__(self, token_budget: TokenBudgetManager) -> None:
        self.token_budget = token_budget

    def select(
        self,
        candidates: Sequence[MemoryHit],
        *,
        required_facets: Sequence[str],
        token_budget: int,
        card_limit: int | None = None,
        rerank_min_score: float | None = None,
    ) -> list[MemoryRecord]:
        remaining = set(required_facets)
        selected: list[MemoryRecord] = []
        used = 0
        pool = list(candidates)
        score_scale = max((abs(hit.score) for hit in pool), default=1.0) or 1.0
        while pool:
            cap_reached = card_limit is not None and len(selected) >= card_limit
            if cap_reached and not remaining:
                break

            def value(hit: MemoryHit) -> tuple[float, float, float]:
                facets = set(hit.record.facets)
                gain = len(facets & remaining)
                duplicate = max(
                    (
                        _similarity(hit.record.preview_text, item.preview_text)
                        for item in selected
                    ),
                    default=0.0,
                )
                tokens = max(
                    1, self.token_budget.estimate_text(hit.record.preview_text)
                )
                return (
                    float(gain),
                    hit.score / score_scale - duplicate * 0.4,
                    -float(tokens),
                )

            best = max(pool, key=value)
            if cap_reached and not set(best.record.facets).intersection(remaining):
                break
            pool.remove(best)
            record = best.record
            if (
                rerank_min_score is not None
                and (best.rerank_score is None or best.rerank_score < rerank_min_score)
                and not set(record.facets).intersection(remaining)
            ):
                continue
            tokens = max(1, self.token_budget.estimate_text(record.preview_text))
            if used + tokens > token_budget:
                continue
            duplicate = max(
                (
                    _similarity(record.preview_text, item.preview_text)
                    for item in selected
                ),
                default=0.0,
            )
            if duplicate >= 0.90 and not set(record.facets).intersection(remaining):
                continue
            selected.append(record)
            used += tokens
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
