"""Bounded context recall budgets, independent of model-generated confidence."""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .config import PagedMemoryConfig
from .models import MemoryHit, MemorySearchRequest
from .retrieval import LexicalMemoryRetriever, _similarity
from .search_budget import authority_deadline


@dataclass(frozen=True)
class CandidateBudget:
    lexical: int
    fused: int
    reason: str


def initial_budget(
    config: PagedMemoryConfig, query: str, facets: Sequence[str]
) -> CandidateBudget:
    """Classify the current question, not the possibly much longer task goal."""
    broad = (
        len(set(facets)) > 1
        or bool(
            re.search(
                r"比较|对比|区别|分别|综合|梳理|所有|compare|versus|\bvs\b",
                query,
                re.IGNORECASE,
            )
        )
        or len(re.findall(r"[?？;；\n]", query)) >= 2
    )
    exact = bool(
        re.search(
            r"`[^`]+`|\b[A-Za-z]+_[A-Za-z0-9_]+\b|\b\w+\.(?:py|ts|js|json|yaml)\b",
            query,
        )
    )
    factor, reason = (
        (2.0, "broad")
        if broad
        else ((0.5, "identifier") if exact else (1.0, "general"))
    )
    if config.supplemental_token_budget < 400:
        factor = min(factor, 0.5)
        reason += ":small-context-budget"

    def bounded(base: int) -> int:
        return max(
            config.adaptive_min_candidates,
            min(config.adaptive_max_candidates, int(base * factor)),
        )

    return CandidateBudget(
        bounded(config.lexical_candidate_limit or config.candidate_limit),
        bounded(config.candidate_limit),
        reason,
    )


def deduplicate_candidates(hits: Sequence[MemoryHit]) -> list[MemoryHit]:
    # Exact duplicate navigation cards only; changed identifiers, negation,
    # memory types and facets must remain available to the cover selector.
    seen = set()
    result = []
    for hit in hits:
        record = hit.record
        key = (
            record.memory_type,
            record.title,
            record.preview_text,
            tuple(sorted(record.facets)),
        )
        if key not in seen:
            seen.add(key)
            result.append(hit)
    return result


def verify_after_rerank(
    authority, request: MemorySearchRequest, hits: Sequence[MemoryHit]
) -> list[MemoryHit]:
    """An external request may outlive a concurrent correction or deletion."""
    with authority_deadline(authority, request.deadline_monotonic):
        current = {
            record.id: record
            for record in authority.verify_active_memories(
                tenant_id=request.tenant_id,
                task_id=request.task_id,
                memory_ids=[hit.record.id for hit in hits],
            )
        }
    valid = []
    for hit in hits:
        record = current.get(hit.record.id)
        if record is None or not LexicalMemoryRetriever._record_matches_request(
            record, request
        ):
            continue
        fields = (
            "content_hash",
            "preview_text",
            "title",
            "facets",
            "source_event_id",
            "artifact_id",
            "source_range",
        )
        if all(getattr(record, name) == getattr(hit.record, name) for name in fields):
            valid.append(replace(hit, record=record))
    return valid


def expansion_reason(
    hits: Sequence[MemoryHit], request: MemorySearchRequest, target: int
) -> str:
    covered = {facet for hit in hits for facet in hit.record.facets}
    if set(request.required_facets) - covered:
        return "missing-facets"
    unique: list[str] = []
    for hit in hits:
        text = hit.record.preview_text
        if all(_similarity(text, previous) < 0.9 for previous in unique):
            unique.append(text)
        if len(unique) >= target:
            return ""
    return "insufficient-distinct-candidates"


def retrieve_context_candidates(
    retriever: LexicalMemoryRetriever,
    config: PagedMemoryConfig,
    request: MemorySearchRequest,
    *,
    user_input: str,
    starting_budget: CandidateBudget | None = None,
) -> tuple[list[MemoryHit], dict]:
    if not config.adaptive_retrieval:
        return retriever.search(request), {"enabled": False}
    budget = starting_budget or initial_budget(
        config, user_input, request.required_facets
    )
    deadline = min(
        request.deadline_monotonic or float("inf"),
        time.monotonic() + config.retrieval_timeout_seconds,
    )
    rounds = []
    lexical_ms = 0.0
    stale_refills = 0
    target = min(
        config.context_card_limit * 2, max(1, config.supplemental_token_budget // 100)
    )
    stop_reason = "coverage-sufficient"
    while True:
        current = replace(
            request, candidate_limit=budget.fused, deadline_monotonic=deadline
        )
        hits = retriever.search(
            current,
            lexical_limit=budget.lexical,
        )
        lexical_ms += retriever.last_lexical_latency_ms
        stale_refills += retriever.last_refill_count
        rounds.append(
            {
                "lexical": budget.lexical,
                "fused": budget.fused,
                "returned": len(hits),
                "reason": budget.reason,
                "has_more": retriever.last_has_more,
            }
        )
        reason = expansion_reason(hits, request, target)
        if not reason:
            break
        if retriever.last_lexical_error:
            stop_reason = "retrieval-deadline"
            break
        if not retriever.last_has_more:
            stop_reason = "candidate-pool-exhausted"
            break
        if time.monotonic() >= deadline:
            stop_reason = "retrieval-deadline"
            break
        if budget.fused >= config.adaptive_max_candidates or len(rounds) >= 3:
            stop_reason = "candidate-cap"
            break
        budget = CandidateBudget(
            min(config.adaptive_max_candidates, budget.lexical * 2),
            min(config.adaptive_max_candidates, budget.fused * 2),
            reason,
        )
    retriever.last_lexical_latency_ms = lexical_ms
    retriever.last_refill_count = stale_refills
    return deduplicate_candidates(hits), {
        "enabled": True,
        "rounds": rounds,
        "stop_reason": stop_reason,
    }
