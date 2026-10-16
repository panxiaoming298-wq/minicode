"""Transactional SQLite memory lifecycle operations.

Only supplemental ACTIVE memories age out. Constraints, decisions, user
preferences, unresolved records, and explicit required sources stay available.
Raw events/artifacts are never deleted by this maintenance operation.
"""

from __future__ import annotations

from typing import Any, Sequence

from minicode.memory_lifecycle import (
    ARCHIVE_AFTER_SECONDS,
    DECAY_PERIOD_SECONDS,
    decay_schedule,
)


def maintain_records(
    db: Any,
    *,
    placeholder: str,
    tenant_id: str,
    task_id: str,
    now: float,
) -> dict[str, Any]:
    p = placeholder
    report: dict[str, Any] = {"decayed": 0, "archived": 0, "has_more": False}
    # Callers already hold SQLite's BEGIN IMMEDIATE writer lock.
    task = db.execute(
        f"SELECT status FROM agent_task WHERE tenant_id={p} AND id={p}",
        (tenant_id, task_id),
    ).fetchone()
    if task is None or task["status"] in {"DELETING", "DELETED", "TOMBSTONED"}:
        return report
    policy = db.execute(
        f"SELECT legal_hold FROM memory_retention_policy WHERE tenant_id={p} AND task_id={p}",
        (tenant_id, task_id),
    ).fetchone()
    if policy is not None and policy["legal_hold"]:
        return report
    rows = db.execute(
        f"""SELECT m.id,m.content_hash,m.importance,m.created_at,m.updated_at,
                   COALESCE(l.last_accessed_at,0) AS last_accessed_at,
                   COALESCE(l.last_decay_at,0) AS last_decay_at
            FROM agent_memory m LEFT JOIN agent_memory_lifecycle l ON l.memory_id=m.id
            WHERE m.tenant_id={p} AND m.task_id={p} AND m.status='ACTIVE'
              AND m.memory_type IN ('FACT','ERROR','TOOL_RESULT','ARTIFACT_CHUNK',
                                    'PHASE_SUMMARY','CHECKPOINT_NOTE')
              AND CAST(m.facets AS TEXT) NOT LIKE {p} AND CAST(m.facets AS TEXT) NOT LIKE {p}
              AND m.created_at<={p} AND m.updated_at<={p}
              AND COALESCE(l.last_accessed_at,0)<={p}
              AND COALESCE(l.last_decay_at,0)<={p}
              AND NOT EXISTS (
                SELECT 1 FROM agent_dependency d
                WHERE d.tenant_id=m.tenant_id AND d.task_id=m.task_id AND d.required=TRUE
                  AND ((d.dependency_type IN ('MEMORY','CONSTRAINT') AND d.dependency_id=m.id)
                    OR (d.dependency_type='EVENT' AND d.dependency_id=m.source_event_id)
                    OR (d.dependency_type='ARTIFACT' AND d.dependency_id=m.artifact_id)))
            ORDER BY m.created_at,m.id LIMIT 1000""",
        (
            tenant_id,
            task_id,
            '%"pinned"%',
            '%"permanent"%',
            *(now - DECAY_PERIOD_SECONDS for _ in range(4)),
        ),
    ).fetchall()
    report["has_more"] = len(rows) == 1000
    for row in rows:
        activity = max(row["created_at"], row["updated_at"], row["last_accessed_at"])
        periods, boundary = decay_schedule(
            now=now,
            last_activity=activity,
            last_decay=row["last_decay_at"],
        )
        if not periods:
            continue
        importance = max(0.0, row["importance"] * 0.5**periods)
        archived = now - activity >= ARCHIVE_AFTER_SECONDS
        db.execute(
            f"UPDATE agent_memory SET importance={p},status={p} "
            f"WHERE id={p} AND tenant_id={p} AND task_id={p} AND status='ACTIVE'",
            (
                importance,
                "ARCHIVED" if archived else "ACTIVE",
                row["id"],
                tenant_id,
                task_id,
            ),
        )
        db.execute(
            f"""INSERT INTO agent_memory_lifecycle
                (memory_id,tenant_id,task_id,last_decay_at,archived_at)
                VALUES ({p},{p},{p},{p},{p}) ON CONFLICT(memory_id) DO UPDATE SET
                last_decay_at=excluded.last_decay_at,archived_at=excluded.archived_at""",
            (row["id"], tenant_id, task_id, boundary, now if archived else 0.0),
        )
        if archived:
            db.execute(
                f"DELETE FROM memory_working_set WHERE tenant_id={p} AND task_id={p} AND memory_id={p}",
                (tenant_id, task_id, row["id"]),
            )
        report["decayed"] += importance != row["importance"]
        report["archived"] += archived
    return report


def touch_records(
    db: Any,
    *,
    placeholder: str,
    tenant_id: str,
    task_id: str,
    memory_ids: Sequence[str],
    now: float,
) -> None:
    if not memory_ids:
        return
    p = placeholder
    task = db.execute(
        f"SELECT status FROM agent_task WHERE tenant_id={p} AND id={p}",
        (tenant_id, task_id),
    ).fetchone()
    if task is None or task["status"] in {"DELETING", "DELETED", "TOMBSTONED"}:
        return
    for memory_id in dict.fromkeys(memory_ids):
        db.execute(
            f"""INSERT INTO agent_memory_lifecycle
                (memory_id,tenant_id,task_id,last_accessed_at)
                SELECT id,tenant_id,task_id,{p} FROM agent_memory
                WHERE id={p} AND tenant_id={p} AND task_id={p} AND status IN ('ACTIVE','OPEN')
                ON CONFLICT(memory_id) DO UPDATE SET last_accessed_at=
                CASE WHEN excluded.last_accessed_at>agent_memory_lifecycle.last_accessed_at
                     THEN excluded.last_accessed_at ELSE agent_memory_lifecycle.last_accessed_at END""",
            (now, memory_id, tenant_id, task_id),
        )
