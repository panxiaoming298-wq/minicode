"""Conservative rerank routing and source/facet-balanced input selection."""

from __future__ import annotations

import re
from collections import Counter


def source_key(hit):
    record = hit.record
    return record.artifact_id or record.source_event_id or record.id


def balanced_candidates(hits, required_facets=()):
    """Keep the head candidate, reserve missing facets, then rotate sources.

    The original ranking breaks ties; no uncalibrated score threshold is used.
    """
    pool = list(hits)
    sources = Counter()
    facets = Counter()
    remaining = set(required_facets)
    while pool:
        if not sources:
            index = 0
        else:
            index = max(
                range(len(pool)),
                key=lambda i: (
                    len(remaining.intersection(pool[i].record.facets)),
                    -sources[source_key(pool[i])],
                    -sum(facets[f] for f in pool[i].record.facets),
                    -i,
                ),
            )
        hit = pool.pop(index)
        sources[source_key(hit)] += 1
        facets.update(hit.record.facets)
        remaining.difference_update(hit.record.facets)
        yield hit


def rerank_route(query, hits, *, required_facets=(), policy="adaptive"):
    if policy == "always":
        return True, "always"
    if len(set(required_facets)) > 1 or re.search(
        r"比较|对比|区别|综合|所有|compare|versus|\bvs\b", query, re.IGNORECASE
    ):
        return True, "multi_topic"
    identifiers = re.findall(
        r"\b[A-Za-z]+_[A-Za-z0-9_]+\b|\b\w+\.(?:py|ts|js|json|yaml)\b", query
    )
    # Skip only an unambiguous, literal navigation lookup. Explanations,
    # negations, corrections and multiple matching versions still need scoring.
    navigation = bool(
        re.search(r"在哪|位置|定位|where|locate|find", query, re.IGNORECASE)
    )
    if (
        identifiers
        and navigation
        and not re.search(r"为什么|如何|怎么|不|禁止|why|how|not", query, re.IGNORECASE)
    ):
        matches = [
            h
            for h in hits
            if all(
                token.casefold()
                in (h.record.title + " " + h.record.preview_text).casefold()
                for token in identifiers
            )
        ]
        if len(matches) == 1 and hits and matches[0].record.id == hits[0].record.id:
            first = matches[0]
            if (
                first.lexical_score > 0
                and set(required_facets) <= set(first.record.facets)
            ):
                return False, "exact_navigation_match"
    return True, "relevance_evaluation"
