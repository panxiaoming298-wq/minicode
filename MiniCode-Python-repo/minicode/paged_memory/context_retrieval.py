"""One deadline for supplemental recall, reranking and source verification."""

from __future__ import annotations

import time
from dataclasses import replace

from .adaptive_retrieval import (
    CandidateBudget,
    deduplicate_candidates,
    retrieve_context_candidates,
    verify_after_rerank,
)
from .reranking import apply_grades
from .retrieval import MinimumCoverSelector
from .source_neighbors import source_neighbors


def source_identity(hit):
    r = hit.record
    return (
        r.id,
        r.content_hash,
        r.title,
        r.preview_text,
        tuple(r.facets),
        r.source_event_id,
        r.artifact_id,
        r.source_range,
    )


def refill_reason(candidates, selected, request, config, *, changed_sources=0):
    covered = {f for record in selected for f in record.facets}
    if set(request.required_facets) - covered:
        return "missing_facets"
    if not selected:
        return "no_relevant_cards"
    if changed_sources and len(selected) < min(3, config.context_card_limit):
        return "changed_sources"
    scored = [h for h in candidates if h.rerank_score is not None]
    useful = [h for h in scored if h.rerank_score >= config.rerank_min_score]
    if (
        scored
        and len(useful) * 4 < len(scored)
        and len(selected) < min(3, config.context_card_limit)
    ):
        return "low_relevance_coverage"
    return ""


def combine_observations(attempts):
    result = dict(attempts[-1])
    for key in (
        "model_calls",
        "tokens",
        "candidate_count",
        "input_tokens_estimated",
        "latency_ms",
        "changed_source_count",
    ):
        values = [a.get(key, 0) for a in attempts]
        result[key] = sum(values) if all(v is not None for v in values) else None
    costs = [a.get("cost") for a in attempts]
    result["cost"] = sum(costs) if all(v is not None for v in costs) else None
    result["applied"] = any(a.get("applied") for a in attempts)
    result["attempted"] = any(a.get("attempted") for a in attempts)
    result["tokens_known"] = all(
        a.get("tokens_known", a.get("tokens") is not None) for a in attempts
    )
    result["usage_pending"] = any(a.get("usage_pending", False) for a in attempts)
    if result["applied"] and not attempts[-1].get("applied"):
        result["status"] = "partial"
    result["passes"] = attempts
    return result


def retrieve_supplemental(
    *,
    authority,
    retriever,
    reranker,
    config,
    token_budget,
    request,
    user_input,
    rerank_allowed,
    inline_coverage=None,
    selection_token_budget=None,
):
    started = time.monotonic()
    request = replace(
        request,
        deadline_monotonic=min(
            request.deadline_monotonic
            if request.deadline_monotonic is not None
            else float("inf"),
            started + config.context_retrieval_timeout_seconds,
        ),
    )
    info = {
        "enabled": config.adaptive_retrieval,
        "rounds": [],
        "post_rerank_refill": {"attempted": False},
    }
    selector = MinimumCoverSelector(token_budget)
    attempts = []
    candidates = []
    observed_sources = {}
    recall_observations = []

    def observe_recall():
        recall_observations.append(
            {
                "lexical": retriever.last_lexical_latency_ms,
                "refills": retriever.last_refill_count,
                "lexical_error": retriever.last_lexical_error,
            }
        )

    def select(values, applied):
        return selector.select(
            values,
            required_facets=request.required_facets,
            token_budget=(config.supplemental_token_budget if selection_token_budget is None else selection_token_budget),
            card_limit=config.context_card_limit,
            rerank_min_score=config.rerank_min_score if applied else None,
        )

    def enrich(values):
        neighbors = source_neighbors(
            authority, request, values, limit=config.source_neighbor_limit
        )
        info["neighbor_count"] = info.get("neighbor_count", 0) + len(neighbors)
        combined = deduplicate_candidates([*values, *neighbors])
        return [hit for hit in combined if inline_coverage is None
                or not inline_coverage.covers(hit.record, required_facets=request.required_facets)]

    def score(values, *, force=False):
        ranked = reranker.rerank(
            request.query,
            values,
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            required_facets=request.required_facets,
            deadline_monotonic=request.deadline_monotonic,
            force=force,
        )
        observation = dict(reranker.observation)
        attempts.append(observation)
        if observation["attempted"] or observation["applied"]:
            checked = verify_after_rerank(authority, request, ranked)
            observation["changed_source_count"] = len(ranked) - len(checked)
            ranked = checked
        for hit in ranked:
            if hit.rerank_score is not None:
                observed_sources[source_identity(hit)] = int(hit.rerank_score)
        return ranked

    try:
        candidates, recall = retrieve_context_candidates(
            retriever,
            config,
            request,
            user_input=user_input,
        )
        observe_recall()
        info.update(recall)
        initial_has_more = retriever.last_has_more
        candidates = enrich(candidates)
        if rerank_allowed and reranker is not None:
            candidates = score(candidates)
        applied = bool(attempts and attempts[0]["applied"])
        selected = select(candidates, applied)
        reason = refill_reason(
            candidates,
            selected,
            request,
            config,
            changed_sources=attempts[0].get("changed_source_count", 0)
            if attempts
            else 0,
        )
        if config.post_rerank_refill and applied and reason:
            refill = info["post_rerank_refill"]
            refill["reason"] = reason
            if request.deadline_monotonic - time.monotonic() <= 0.5:
                refill["stop_reason"] = "retrieval_deadline"
            else:
                pool = candidates
                rounds = info.get("rounds", [])
                previous = (
                    rounds[-1]
                    if rounds
                    else {
                        "lexical": config.lexical_candidate_limit
                        or config.candidate_limit,
                        "fused": config.candidate_limit,
                    }
                )
                if (
                    config.adaptive_retrieval
                    and initial_has_more
                    and previous["fused"] < config.adaptive_max_candidates
                ):
                    refill["attempted"] = True
                    expanded = CandidateBudget(
                        *[
                            min(config.adaptive_max_candidates, previous[key] * 2)
                            for key in ("lexical", "fused")
                        ],
                        reason="post-rerank-coverage",
                    )
                    extra, extra_info = retrieve_context_candidates(
                        retriever,
                        config,
                        request,
                        user_input=user_input,
                        starting_budget=expanded,
                    )
                    observe_recall()
                    refill["recall"] = extra_info
                    # Keep earlier valid candidates if the additional recall timed out.
                    pool = deduplicate_candidates([*extra, *candidates])
                pool = enrich(pool)
                pool = verify_after_rerank(authority, request, pool)
                novel = [h for h in pool if source_identity(h) not in observed_sources]
                refill["novel_candidates"] = len(novel)
                if novel:
                    refill["attempted"] = True
                    score(novel, force=True)
                else:
                    refill["stop_reason"] = "no_unscored_candidates"
                # Apply only scores attached to the exact current source snapshot.
                pool = verify_after_rerank(authority, request, pool)
                scored = [h for h in pool if source_identity(h) in observed_sources]
                candidates = apply_grades(
                    pool, scored, [observed_sources[source_identity(h)] for h in scored]
                )
                selected = select(candidates, True)
        info["selected_facets"] = sorted({f for r in selected for f in r.facets})
        info["missing_facets"] = sorted(
            set(request.required_facets) - set(info["selected_facets"])
        )
        info["final_candidate_count"] = len(candidates)
        info["selected_tokens"] = sum(
            token_budget.estimate_text(r.preview_text) for r in selected
        )
        return selected, info
    except TimeoutError:
        # An unverified supplemental snapshot cannot replace mandatory evidence.
        info["stop_reason"] = "context_retrieval_deadline"
        info["missing_facets"] = list(request.required_facets)
        return [], info
    finally:
        info["latency_ms"] = (time.monotonic() - started) * 1000
        if attempts:
            reranker.observation = combine_observations(attempts)
        if recall_observations:
            retriever.last_lexical_latency_ms = sum(
                r["lexical"] for r in recall_observations
            )
            retriever.last_refill_count = sum(r["refills"] for r in recall_observations)
            retriever.last_lexical_error = next(
                (
                    r["lexical_error"]
                    for r in reversed(recall_observations)
                    if r["lexical_error"]
                ),
                "",
            )
