"""Label-based metrics that distinguish recall pools from injected evidence."""

import math


def retrieval_metrics(*, relevant_ids, candidate_ids, ranked_ids, selected_ids):
    relevant = set(relevant_ids)
    chosen = list(selected_ids)
    candidates, selected = set(candidate_ids), set(chosen)
    ranked = list(ranked_ids)
    rr = next((1 / i for i, key in enumerate(ranked, 1) if key in relevant), 0.0)
    seen = set()
    dcg = 0.0
    for i, key in enumerate(ranked[:10], 1):
        if key in relevant and key not in seen:
            dcg += 1 / math.log2(i + 1)
        seen.add(key)
    ideal = sum(1 / math.log2(i + 1) for i in range(1, min(10, len(relevant)) + 1))
    return {
        "candidate_recall": len(relevant & candidates) / len(relevant)
        if relevant
        else None,
        "context_recall": len(relevant & selected) / len(relevant)
        if relevant
        else None,
        "context_precision": len(relevant & selected) / len(selected)
        if selected
        else None,
        "mrr": rr if relevant else None,
        "ndcg10": dcg / ideal if ideal else None,
        "selected_duplicate_ratio": 1 - len(selected) / len(chosen) if chosen else 0.0,
        "irrelevant_selected_count": len(selected - relevant),
        "no_match_clean": not selected if not relevant else None,
    }
