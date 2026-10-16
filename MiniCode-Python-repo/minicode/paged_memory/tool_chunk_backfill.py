"""Bounded, restartable repair of derived tool-text search cards.

Original events/artifacts are hash-verified and never rewritten. A failed event
keeps the cursor before that event; repeating a committed batch is idempotent.
"""
from __future__ import annotations

import sqlite3
import time
import math

from minicode.history_query import cursor_integer, decode_cursor, encode_cursor, scope_key
from .chunking import tool_text_chunk_records
from .models import ArtifactStatus, EventType, content_hash_bytes
from .markdown_export import is_generated_export
from .policy import TokenBudgetManager


def backfill_tool_chunks(*, authority, artifacts, spool, config, tenant_id, task_id,
                         apply=False, cursor="", limit=25, max_source_bytes=8 * 1024 * 1024,
                         timeout=10.0, model_name=""):
    if not tenant_id or not task_id or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Backfill requires a tenant/task and an event limit between 1 and 100.")
    if type(max_source_bytes) is not int or not 1 <= max_source_bytes <= 64 * 1024 * 1024:
        raise ValueError("Source byte budget must be between 1 byte and 64 MiB.")
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("Backfill time budget must be positive and at most 60 seconds.")
    key = scope_key("tool-chunk-backfill-v1", str(authority.path.resolve()), tenant_id, task_id,
                    options={"apply": apply, "model": model_name, "page_tokens": config.raw_page_token_limit})
    saved = decode_cursor(cursor, key) if cursor else None
    snapshot = cursor_integer(saved, "snapshot") if saved else None
    position = cursor_integer(saved, "position") if saved else 0
    if snapshot is not None and position > snapshot:
        raise ValueError("Backfill cursor position exceeds its snapshot.")
    deadline = time.monotonic() + timeout
    budget = TokenBudgetManager(model_name=model_name)
    report = {"dry_run": not apply, "events_scanned": 0, "expected_chunks": 0,
              "existing_chunks": 0, "missing_chunks": 0, "inserted_chunks": 0,
              "skipped_sources": 0, "errors": [], "stop_reason": "exhausted"}
    with authority.query_deadline(deadline):
        snapshot, batch, more = authority.history_event_page(tenant_id=tenant_id, task_id=task_id,
            snapshot=snapshot, position=position, recent_first=False, role="tool_result", limit=limit)
    by_uri = None
    for sequence, event in batch:
        if time.monotonic() >= deadline:
            more, report["stop_reason"] = True, "time_budget"
            break
        try:
            if event.content_json.get("searchable_memory") is False:
                report["skipped_sources"] += 1
                position = sequence
                report["events_scanned"] += 1
                continue
            artifact, store = None, None
            if event.artifact_uri:
                if by_uri is None:
                    with authority.query_deadline(deadline):
                        by_uri = {a.uri: a for a in authority.list_artifacts(tenant_id=tenant_id, task_id=task_id)}
                artifact = by_uri.get(event.artifact_uri)
                if artifact is None or artifact.status not in {ArtifactStatus.ACTIVE, ArtifactStatus.PENDING_UPLOAD}:
                    raise ValueError("source artifact unavailable")
                if not artifact.media_type.casefold().startswith("text/"):
                    report["skipped_sources"] += 1
                    position = sequence
                    report["events_scanned"] += 1
                    continue
                if artifact.size_bytes > max_source_bytes:
                    raise ValueError("source exceeds byte budget")
                store = spool if artifact.status is ArtifactStatus.PENDING_UPLOAD else artifacts
                if store is None:
                    raise ValueError("source store unavailable")
            raw = store.get_bytes(artifact) if artifact else event.content_text.encode("utf-8")
            if len(raw) > max_source_bytes or content_hash_bytes(raw) != event.content_hash:
                raise ValueError("source exceeds byte budget or has an invalid checksum")
            output = raw.decode("utf-8")
            if is_generated_export(output):
                report["skipped_sources"] += 1
                position = sequence
                report["events_scanned"] += 1
                continue
            records = tool_text_chunk_records(output=output, event=event, artifact=artifact,
                tool_name=event.content_json.get("tool_name", ""), phase=event.content_json.get("phase", ""),
                ok=event.event_type is not EventType.TOOL_ERROR, estimate_tokens=budget.estimate_text,
                raw_page_token_limit=config.raw_page_token_limit)
            # At most one event is committed at once; an interrupted event has
            # neither partial cards nor an advanced continuation position.
            missing = []
            with authority.query_deadline(deadline), authority._transaction():
                existing = {}
                for start in range(0, len(records), 200):
                    existing.update((m.id, m) for m in authority.get_memories(tenant_id=tenant_id,
                        task_id=task_id, memory_ids=[m.id for m in records[start:start + 200]]))
                for record in records:
                    previous = existing.get(record.id)
                    if previous is not None:
                        if previous.content_hash != record.content_hash or previous.preview_text != record.preview_text:
                            raise ValueError("derived chunk conflicts with original source")
                    else:
                        missing.append(record)
                        if apply:
                            authority.put_memory(record)
            report["expected_chunks"] += len(records)
            report["missing_chunks"] += len(missing)
            report["existing_chunks"] += len(records) - len(missing)
            report["inserted_chunks"] += len(missing) if apply else 0
            position = sequence
            report["events_scanned"] += 1
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            report["errors"].append({"source_event_id": event.id, "error": type(exc).__name__, "reason": str(exc)})
            more, report["stop_reason"] = True, "source_error"
            break
    else:
        if more:
            report["stop_reason"] = "event_limit"
    report.update(has_more=more, next_cursor=encode_cursor(key, snapshot=snapshot, position=position) if more else None,
                  complete=not more and not report["errors"])
    return report
