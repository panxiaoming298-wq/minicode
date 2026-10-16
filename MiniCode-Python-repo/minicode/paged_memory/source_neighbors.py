"""Bounded adjacent-source recall over exact, non-overlapping byte ranges."""

from __future__ import annotations

from .models import MemoryHit, content_hash_text
from .retrieval import LexicalMemoryRetriever
from .search_budget import authority_deadline


def source_neighbors(authority, request, hits, *, limit):
    seeds = [
        h
        for h in hits
        if h.record.source_range.unit == "BYTE"
        and h.record.source_range.end > h.record.source_range.start
        and (h.record.artifact_id or "event_chunk" in h.record.facets)
    ]
    if not seeds or limit <= 0 or not hasattr(authority, "_transaction"):
        return []
    # Breadth is bounded even when an unusually large candidate pool is supplied.
    seeds = seeds[:32]
    placeholders = ",".join("?" for _ in seeds)
    sql = (
        "SELECT DISTINCT n.* FROM agent_memory a JOIN agent_memory n "
        "ON n.tenant_id=a.tenant_id AND n.task_id=a.task_id "
        "AND n.source_event_id=a.source_event_id AND n.artifact_id=a.artifact_id "
        "AND n.memory_type=a.memory_type AND n.source_unit='BYTE' "
        "AND (n.source_end=a.source_start OR n.source_start=a.source_end) "
        "WHERE a.tenant_id=? AND a.task_id=? AND a.source_unit='BYTE' "
        f"AND a.id IN ({placeholders}) AND a.status IN ('ACTIVE','OPEN') "
        "AND n.status IN ('ACTIVE','OPEN') AND n.source_end>n.source_start ORDER BY n.source_start,n.id LIMIT ?"
    )
    with (
        authority_deadline(authority, request.deadline_monotonic),
        authority._transaction() as db,
    ):
        rows = db.execute(
            sql,
            (
                request.tenant_id,
                request.task_id,
                *[h.record.id for h in seeds],
                limit + len(seeds),
            ),
        ).fetchall()
    seen = {h.record.id for h in hits}
    result = []
    score = min((h.score for h in seeds), default=0)
    for row in rows:
        record = authority._memory(row)
        if record.id in seen or not LexicalMemoryRetriever._record_matches_request(
            record, request
        ):
            continue
        if content_hash_text(record.preview_text) != record.content_hash:
            continue
        if (
            len(record.preview_text.encode("utf-8"))
            != record.source_range.end - record.source_range.start
        ):
            continue
        result.append(MemoryHit(record, score, reason="verified-source-neighbor"))
        seen.add(record.id)
        if len(result) >= limit:
            break
    return result
