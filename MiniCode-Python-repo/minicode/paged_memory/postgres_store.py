"""PostgreSQL authority adapter.

The driver is imported lazily so the base local package keeps zero mandatory
runtime dependencies. Install the ``memory`` extra for server deployments.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .lexical import memory_search_document, postgres_tsquery, scope_boost
from .models import (
    AgentEvent,
    ArchiveManifest,
    ArchiveStatus,
    ArtifactRecord,
    CheckpointRecord,
    DecisionRecord,
    DependencyRecord,
    MemoryHit,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    OutboxClaimLostError,
    OutboxRecord,
    TaskRetentionPolicy,
    TaskStateSnapshot,
    apply_transactional_state_patch,
    content_hash_text,
    new_id,
    now_epoch,
    stable_json,
)
from .sqlite_store import SqliteMemoryStore, StateVersionConflict


class PostgresMemoryStore:
    def __init__(self, database_url: str, *, connect_timeout: int = 10) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL memory requires the optional 'memory' dependencies: "
                "pip install 'minicode-py[memory]'"
            ) from exc
        self._Jsonb = Jsonb
        self._connection = psycopg.connect(
            database_url,
            connect_timeout=connect_timeout,
            autocommit=False,
            row_factory=dict_row,
        )
        self.initialize()

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            yield cursor

    def initialize(self) -> None:
        migration = Path(__file__).with_name("sql") / "001_postgres.sql"
        sql = migration.read_text(encoding="utf-8")
        with self._connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.execute(
                "SELECT id,title,preview_text,facets FROM agent_memory "
                "WHERE search_document=''"
            )
            rows = cursor.fetchall()
            if rows:
                cursor.executemany(
                    "UPDATE agent_memory SET search_document=%s WHERE id=%s",
                    [
                        (
                            memory_search_document(
                                title=str(row["title"]),
                                preview_text=str(row["preview_text"]),
                                facets=tuple(row["facets"] or ()),
                            ),
                            row["id"],
                        )
                        for row in rows
                    ],
                )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def health_check(self) -> bool:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                row = cursor.fetchone()
            self._connection.rollback()
            return bool(row and row["ok"] == 1)
        except Exception:  # noqa: BLE001 - connection drivers expose multiple subclasses.
            try:
                self._connection.rollback()
            except Exception:  # noqa: BLE001, S110 - a dead connection cannot roll back.
                pass
            return False

    def ensure_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        goal: str,
        workspace_id: str = "",
        user_id: str = "",
        cross_session_required: bool = False,
        restart_recovery_required: bool = False,
    ) -> None:
        now = now_epoch()
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_task(
                    tenant_id,id,workspace_id,user_id,goal,cross_session_required,
                    restart_recovery_required,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,id) DO UPDATE SET
                    goal=CASE WHEN excluded.goal<>'' THEN excluded.goal ELSE agent_task.goal END,
                    workspace_id=CASE WHEN excluded.workspace_id<>'' THEN excluded.workspace_id ELSE agent_task.workspace_id END,
                    user_id=CASE WHEN excluded.user_id<>'' THEN excluded.user_id ELSE agent_task.user_id END,
                    cross_session_required=agent_task.cross_session_required OR excluded.cross_session_required,
                    restart_recovery_required=agent_task.restart_recovery_required OR excluded.restart_recovery_required,
                    updated_at=excluded.updated_at
                WHERE agent_task.status='ACTIVE'
                RETURNING status
                """,
                (
                    tenant_id,
                    task_id,
                    workspace_id,
                    user_id,
                    goal,
                    cross_session_required,
                    restart_recovery_required,
                    now,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("task is not ACTIVE")
            cursor.execute(
                """
                INSERT INTO agent_task_state(tenant_id,task_id,goal,updated_at)
                VALUES (%s,%s,%s,%s) ON CONFLICT(tenant_id,task_id) DO NOTHING
                """,
                (tenant_id, task_id, goal, now),
            )

    def create_run(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str,
        mode: str,
        model_name: str = "",
        model_context_limit: int = 0,
    ) -> None:
        now = now_epoch()
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_run(tenant_id,id,task_id,current_memory_mode,model_name,model_context_limit,started_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,id) DO NOTHING
                """,
                (
                    tenant_id,
                    run_id,
                    task_id,
                    mode,
                    model_name,
                    model_context_limit,
                    now,
                ),
            )
            cursor.execute(
                "UPDATE agent_task SET current_run_id=%s,updated_at=%s WHERE tenant_id=%s AND id=%s",
                (run_id, now, tenant_id, task_id),
            )

    def task_status(self, *, tenant_id: str, task_id: str) -> str | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM agent_task WHERE tenant_id=%s AND id=%s",
                (tenant_id, task_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return str(row["status"]) if row else None

    def begin_task_delete(self, *, tenant_id: str, task_id: str) -> str | None:
        """Fence asynchronous consumers before external objects are removed."""
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT status FROM agent_task WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (tenant_id, task_id),
            )
            row = cursor.fetchone()
            if row is None or str(row["status"]) == "DELETING":
                return None
            previous = str(row["status"])
            cursor.execute(
                "UPDATE agent_task SET status='DELETING',updated_at=%s "
                "WHERE tenant_id=%s AND id=%s",
                (now_epoch(), tenant_id, task_id),
            )
            return previous

    def restore_task_status(
        self, *, tenant_id: str, task_id: str, status: str
    ) -> bool:
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE agent_task SET status=%s,updated_at=%s "
                "WHERE tenant_id=%s AND id=%s AND status='DELETING'",
                (status, now_epoch(), tenant_id, task_id),
            )
            return cursor.rowcount == 1

    def update_run_mode(
        self, *, tenant_id: str, task_id: str, run_id: str, mode: str
    ) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE agent_run SET current_memory_mode=%s "
                "WHERE tenant_id=%s AND task_id=%s AND id=%s",
                (mode, tenant_id, task_id, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"run not found in task scope: {run_id}")

    def upsert_step(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        sequence_no: int,
        title: str = "",
        description: str = "",
        status: str = "RUNNING",
        acceptance_criteria: Mapping[str, Any] | None = None,
        parent_step_id: str = "",
    ) -> None:
        now = now_epoch()
        completed_at = now if status in {"COMPLETED", "FAILED", "CANCELLED"} else None
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_step(
                    tenant_id,id,run_id,task_id,parent_step_id,sequence_no,title,description,
                    status,acceptance_criteria,started_at,completed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,id) DO UPDATE SET
                    title=excluded.title,description=excluded.description,status=excluded.status,
                    acceptance_criteria=excluded.acceptance_criteria,
                    completed_at=COALESCE(excluded.completed_at,agent_step.completed_at)
                """,
                (
                    tenant_id,
                    step_id,
                    run_id,
                    task_id,
                    parent_step_id,
                    sequence_no,
                    title,
                    description,
                    status,
                    self._Jsonb(dict(acceptance_criteria or {})),
                    now,
                    completed_at,
                ),
            )
            cursor.execute(
                "UPDATE agent_task SET current_step_id=%s,updated_at=%s WHERE tenant_id=%s AND id=%s",
                (step_id, now, tenant_id, task_id),
            )

    def get_step(
        self, *, tenant_id: str, task_id: str, step_id: str
    ) -> dict[str, Any] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_step WHERE tenant_id=%s AND task_id=%s AND id=%s",
                (tenant_id, task_id, step_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return dict(row) if row else None

    def persist_event(
        self,
        event: AgentEvent,
        *,
        state_patch: Mapping[str, Any] | None = None,
        artifact: ArtifactRecord | None = None,
        memory: MemoryRecord | None = None,
        outbox: OutboxRecord | None = None,
        memories: Sequence[MemoryRecord] = (),
        outboxes: Sequence[OutboxRecord] = (),
        decision: DecisionRecord | None = None,
        checkpoint: CheckpointRecord | None = None,
        step_status: str = "",
    ) -> tuple[AgentEvent, TaskStateSnapshot | None]:
        bundled = (artifact, memory, decision, checkpoint, *memories)
        if any(
            value is not None
            and (value.tenant_id != event.tenant_id or value.task_id != event.task_id)
            for value in bundled
        ):
            raise ValueError("event bundle must share one tenant/task scope")
        with self._transaction() as cursor:
            if event.idempotency_key:
                cursor.execute(
                    "SELECT * FROM agent_event "
                    "WHERE tenant_id=%s AND task_id=%s AND idempotency_key=%s",
                    (event.tenant_id, event.task_id, event.idempotency_key),
                )
                existing = cursor.fetchone()
                if existing:
                    cursor.execute(
                        "SELECT * FROM agent_task_state WHERE tenant_id=%s AND task_id=%s",
                        (event.tenant_id, event.task_id),
                    )
                    state_row = cursor.fetchone()
                    return self._event(existing), self._state(
                        state_row
                    ) if state_row else None
            cursor.execute(
                """
                INSERT INTO agent_event(
                    id,tenant_id,task_id,run_id,step_id,event_type,content_text,content_json,
                    artifact_uri,content_hash,token_count,caused_by_event_id,correlation_id,
                    idempotency_key,schema_version,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    event.id,
                    event.tenant_id,
                    event.task_id,
                    event.run_id,
                    event.step_id,
                    event.event_type.value,
                    event.content_text,
                    self._Jsonb(event.content_json),
                    event.artifact_uri,
                    event.content_hash,
                    event.token_count,
                    event.caused_by_event_id,
                    event.correlation_id,
                    event.idempotency_key,
                    event.schema_version,
                    event.created_at,
                ),
            )
            if artifact is not None:
                self._put_artifact(cursor, artifact)
            if memory is not None:
                requested_memory_id = memory.id
                memory = self._put_memory(cursor, memory)
                if outbox is not None and outbox.aggregate_id == requested_memory_id:
                    self._align_memory_outbox(outbox, memory)
            if outbox is not None:
                self._put_outbox(cursor, outbox)
            extra_outboxes = {
                item.aggregate_id: item
                for item in outboxes
                if item.event_type == "UPSERT_MEMORY_INDEX"
            }
            for extra_memory in memories:
                requested_memory_id = extra_memory.id
                stored_memory = self._put_memory(cursor, extra_memory)
                extra_outbox = extra_outboxes.get(requested_memory_id)
                if extra_outbox is not None:
                    self._align_memory_outbox(extra_outbox, stored_memory)
            for extra_outbox in outboxes:
                self._put_outbox(cursor, extra_outbox)
            if decision is not None:
                self._put_decision(
                    cursor,
                    decision,
                    project_state=state_patch is None,
                    index_outbox_template=outbox,
                )
            state = None
            if state_patch is not None:
                cursor.execute(
                    "SELECT * FROM agent_task_state WHERE tenant_id=%s AND task_id=%s FOR UPDATE",
                    (event.tenant_id, event.task_id),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "INSERT INTO agent_task_state(tenant_id,task_id,updated_at) VALUES (%s,%s,%s)",
                        (event.tenant_id, event.task_id, now_epoch()),
                    )
                    cursor.execute(
                        "SELECT * FROM agent_task_state WHERE tenant_id=%s AND task_id=%s FOR UPDATE",
                        (event.tenant_id, event.task_id),
                    )
                    row = cursor.fetchone()
                current = self._state(row)
                state = apply_transactional_state_patch(current, state_patch)
                self._write_state(cursor, state, expected_version=current.version)
            if checkpoint is not None:
                checkpoint_state = state
                if checkpoint_state is None:
                    cursor.execute(
                        "SELECT * FROM agent_task_state "
                        "WHERE tenant_id=%s AND task_id=%s FOR UPDATE",
                        (event.tenant_id, event.task_id),
                    )
                    checkpoint_row = cursor.fetchone()
                    checkpoint_state = (
                        self._state(checkpoint_row)
                        if checkpoint_row is not None
                        else None
                    )
                if checkpoint_state is None:
                    raise StateVersionConflict(
                        "checkpoint state is unavailable"
                    )
                if checkpoint.state_version != checkpoint_state.version:
                    if state_patch is None:
                        raise StateVersionConflict(
                            "checkpoint state version conflict: "
                            f"expected {checkpoint.state_version}, got {checkpoint_state.version}"
                        )
                    checkpoint.state_version = checkpoint_state.version
                    checkpoint.snapshot = {
                        **checkpoint.snapshot,
                        "task_state": checkpoint_state.to_dict(),
                    }
                    metrics = checkpoint.snapshot.get("metrics")
                    if isinstance(metrics, dict):
                        metrics["completed_steps"] = len(
                            checkpoint_state.completed_steps
                        )
                    checkpoint.content_hash = content_hash_text(
                        stable_json(checkpoint.snapshot)
                    )
                self._put_checkpoint(cursor, checkpoint)
            if step_status:
                if step_status not in {"RUNNING", "COMPLETED", "FAILED"}:
                    raise ValueError("invalid atomic step status")
                if not event.step_id:
                    raise ValueError("atomic step status requires event.step_id")
                completed_at = (
                    now_epoch() if step_status in {"COMPLETED", "FAILED"} else None
                )
                cursor.execute(
                    "UPDATE agent_step SET status=%s,completed_at=%s "
                    "WHERE tenant_id=%s AND task_id=%s AND id=%s",
                    (
                        step_status,
                        completed_at,
                        event.tenant_id,
                        event.task_id,
                        event.step_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("atomic step update target is missing")
            return event, state

    def get_event(
        self, *, tenant_id: str, task_id: str, event_id: str
    ) -> AgentEvent | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_event WHERE tenant_id=%s AND task_id=%s AND id=%s",
                (tenant_id, task_id, event_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return self._event(row) if row else None

    def get_event_by_idempotency(
        self, *, tenant_id: str, task_id: str, idempotency_key: str
    ) -> AgentEvent | None:
        if not idempotency_key:
            return None
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_event
                WHERE tenant_id=%s AND task_id=%s AND idempotency_key=%s
                """,
                (tenant_id, task_id, idempotency_key),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return self._event(row) if row else None

    def recent_events(
        self,
        *,
        tenant_id: str,
        task_id: str,
        limit: int = 100,
        event_types: Sequence[str] = (),
    ) -> list[AgentEvent]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=%s AND task_id=%s"
        if event_types:
            where += " AND event_type = ANY(%s)"
            params.append(list(event_types))
        params.append(max(1, limit))
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM agent_event WHERE {where} ORDER BY created_at DESC,id DESC LIMIT %s",
                params,
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._event(row) for row in reversed(rows)]

    def events_in_range(
        self,
        *,
        tenant_id: str,
        task_id: str,
        after: float | None = None,
        before: float | None = None,
        limit: int = 100_000,
        event_types: Sequence[str] = (),
        exclude_event_types: Sequence[str] = (),
    ) -> list[AgentEvent]:
        params: list[Any] = [tenant_id, task_id]
        where = ["tenant_id=%s", "task_id=%s"]
        if after is not None:
            where.append("created_at>=%s")
            params.append(after)
        if before is not None:
            where.append("created_at<%s")
            params.append(before)
        if event_types:
            where.append("event_type = ANY(%s)")
            params.append(list(event_types))
        if exclude_event_types:
            where.append("NOT (event_type = ANY(%s))")
            params.append(list(exclude_event_types))
        params.append(max(1, limit))
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_event WHERE "
                + " AND ".join(where)
                + " ORDER BY created_at ASC,id ASC LIMIT %s",
                params,
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._event(row) for row in rows]

    def get_state(self, *, tenant_id: str, task_id: str) -> TaskStateSnapshot | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_task_state WHERE tenant_id=%s AND task_id=%s",
                (tenant_id, task_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return self._state(row) if row else None

    def compare_and_set_state(
        self, state: TaskStateSnapshot, *, expected_version: int
    ) -> TaskStateSnapshot:
        if state.version != expected_version + 1:
            raise ValueError("new state version must equal expected_version + 1")
        with self._transaction() as cursor:
            self._write_state(cursor, state, expected_version=expected_version)
        return state

    def put_decision(self, decision: DecisionRecord) -> DecisionRecord:
        with self._transaction() as cursor:
            self._put_decision(cursor, decision)
        return decision

    def _put_decision(
        self,
        cursor: Any,
        decision: DecisionRecord,
        *,
        project_state: bool = True,
        index_outbox_template: OutboxRecord | None = None,
    ) -> DecisionRecord:
        cursor.execute(
            """
            SELECT * FROM agent_decision WHERE tenant_id=%s AND task_id=%s
            AND decision_key=%s AND status='ACTIVE'
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE
            """,
            (decision.tenant_id, decision.task_id, decision.decision_key),
        )
        old = cursor.fetchone()
        if old and old["id"] != decision.id:
            decision.supersedes_id = old["id"]
            cursor.execute(
                "UPDATE agent_decision SET status='SUPERSEDED',superseded_by_id=%s,updated_at=%s WHERE id=%s",
                (decision.id, now_epoch(), old["id"]),
            )
            cursor.execute(
                """
                UPDATE agent_memory SET status='SUPERSEDED',updated_at=%s
                WHERE tenant_id=%s AND task_id=%s AND id=%s
                """,
                (now_epoch(), decision.tenant_id, decision.task_id, old["id"]),
            )
            if index_outbox_template is not None:
                descriptor_payload: dict[str, Any] = {
                    key: index_outbox_template.payload[key]
                    for key in ("index_id", "collection", "index_descriptor")
                    if key in index_outbox_template.payload
                }
                self._put_outbox(
                    cursor,
                    OutboxRecord(
                        aggregate_type="MEMORY",
                        aggregate_id=old["id"],
                        event_type="UPSERT_MEMORY_INDEX",
                        payload={
                            "tenant_id": decision.tenant_id,
                            "task_id": decision.task_id,
                            "memory_id": old["id"],
                            **descriptor_payload,
                        },
                    ),
                )
        cursor.execute(
            """
            INSERT INTO agent_decision(
                id,tenant_id,task_id,decision_key,decision_text,structured_value,status,
                source_event_id,supersedes_id,superseded_by_id,created_at,updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET
                decision_text=excluded.decision_text,structured_value=excluded.structured_value,
                status=excluded.status,source_event_id=excluded.source_event_id,
                supersedes_id=excluded.supersedes_id,superseded_by_id=excluded.superseded_by_id,
                updated_at=excluded.updated_at
            """,
            (
                decision.id,
                decision.tenant_id,
                decision.task_id,
                decision.decision_key,
                decision.decision_text,
                self._Jsonb(decision.structured_value),
                decision.status.value,
                decision.source_event_id,
                decision.supersedes_id,
                decision.superseded_by_id,
                decision.created_at,
                decision.updated_at,
            ),
        )
        row = None
        if project_state:
            cursor.execute(
                "SELECT * FROM agent_task_state WHERE tenant_id=%s AND task_id=%s FOR UPDATE",
                (decision.tenant_id, decision.task_id),
            )
            row = cursor.fetchone()
        if row:
            current = self._state(row)
            active = dict(current.active_decisions)
            active[decision.decision_key] = {
                "decision_id": decision.id,
                "source_event_id": decision.source_event_id,
                "status": decision.status.value,
            }
            self._write_state(
                cursor,
                current.apply_patch({"active_decisions": active}),
                expected_version=current.version,
            )
        return decision

    def active_decisions(
        self, *, tenant_id: str, task_id: str, decision_key: str = ""
    ) -> list[DecisionRecord]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=%s AND task_id=%s AND status='ACTIVE'"
        if decision_key:
            where += " AND decision_key=%s"
            params.append(decision_key)
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM agent_decision WHERE {where} ORDER BY updated_at DESC",
                params,
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._decision(row) for row in rows]

    def decision_history(
        self, *, tenant_id: str, task_id: str, decision_key: str
    ) -> list[DecisionRecord]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_decision WHERE tenant_id=%s AND task_id=%s AND decision_key=%s ORDER BY created_at ASC",
                (tenant_id, task_id, decision_key),
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._decision(row) for row in rows]

    def add_dependency(self, dependency: DependencyRecord) -> DependencyRecord:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_dependency(
                    id,tenant_id,task_id,step_id,dependency_type,dependency_id,required,
                    dependency_depth,created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,task_id,step_id,dependency_type,dependency_id)
                DO UPDATE SET required=agent_dependency.required OR excluded.required,
                              dependency_depth=LEAST(agent_dependency.dependency_depth,excluded.dependency_depth)
                RETURNING *
                """,
                (
                    dependency.id,
                    dependency.tenant_id,
                    dependency.task_id,
                    dependency.step_id,
                    dependency.dependency_type.value,
                    dependency.dependency_id,
                    dependency.required,
                    dependency.dependency_depth,
                    dependency.created_at,
                ),
            )
            row = cursor.fetchone()
        return self._dependency(row)

    def dependencies_for_step(
        self, *, tenant_id: str, task_id: str, step_id: str
    ) -> list[DependencyRecord]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_dependency WHERE tenant_id=%s AND task_id=%s AND step_id=%s ORDER BY required DESC,dependency_depth,created_at",
                (tenant_id, task_id, step_id),
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._dependency(row) for row in rows]

    def all_dependencies(
        self, *, tenant_id: str, task_id: str
    ) -> list[DependencyRecord]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_dependency
                WHERE tenant_id=%s AND task_id=%s
                ORDER BY step_id,required DESC,dependency_depth ASC,created_at ASC
                """,
                (tenant_id, task_id),
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._dependency(row) for row in rows]

    def get_artifact(
        self, *, tenant_id: str, task_id: str, artifact_id: str
    ) -> ArtifactRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_artifact WHERE tenant_id=%s AND task_id=%s AND id=%s",
                (tenant_id, task_id, artifact_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return self._artifact(row) if row else None

    def list_artifacts(
        self, *, tenant_id: str, task_id: str, statuses: Sequence[str] = ()
    ) -> list[ArtifactRecord]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=%s AND task_id=%s"
        if statuses:
            where += " AND status = ANY(%s)"
            params.append(list(statuses))
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM agent_artifact WHERE {where} ORDER BY created_at DESC",
                params,
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._artifact(row) for row in rows]

    def all_artifacts(
        self, *, tenant_id: str | None = None, task_id: str | None = None
    ) -> list[ArtifactRecord]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id:
            where.append("tenant_id=%s")
            params.append(tenant_id)
        if task_id:
            where.append("task_id=%s")
            params.append(task_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM agent_artifact{clause} ORDER BY created_at", params
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._artifact(row) for row in rows]

    def update_artifact_status(
        self,
        *,
        tenant_id: str,
        task_id: str,
        artifact_id: str,
        status: str,
        uri: str | None = None,
    ) -> None:
        with self._transaction() as cursor:
            if uri is None:
                cursor.execute(
                    "UPDATE agent_artifact SET status=%s,updated_at=%s WHERE tenant_id=%s AND task_id=%s AND id=%s",
                    (status, now_epoch(), tenant_id, task_id, artifact_id),
                )
            else:
                cursor.execute(
                    "UPDATE agent_artifact SET status=%s,uri=%s,updated_at=%s WHERE tenant_id=%s AND task_id=%s AND id=%s",
                    (status, uri, now_epoch(), tenant_id, task_id, artifact_id),
                )

    def put_memory(
        self, memory: MemoryRecord, *, outbox: OutboxRecord | None = None
    ) -> MemoryRecord:
        with self._transaction() as cursor:
            stored = self._put_memory(cursor, memory)
            if outbox:
                self._align_memory_outbox(outbox, stored)
                self._put_outbox(cursor, outbox)
        return stored

    def get_memories(
        self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str]
    ) -> list[MemoryRecord]:
        if not memory_ids:
            return []
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_memory WHERE tenant_id=%s AND task_id=%s AND id = ANY(%s)",
                (tenant_id, task_id, list(memory_ids)),
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        by_id = {row["id"]: self._memory(row) for row in rows}
        return [by_id[memory_id] for memory_id in memory_ids if memory_id in by_id]

    def search_memories_lexical(self, request: MemorySearchRequest) -> list[MemoryHit]:
        if request.candidate_limit <= 0 or not request.statuses:
            return []
        params: list[Any] = [request.tenant_id, request.task_id, list(request.statuses)]
        where = "tenant_id=%s AND task_id=%s AND status = ANY(%s)"
        if request.memory_types:
            where += " AND memory_type = ANY(%s)"
            params.append(list(request.memory_types))
        query = postgres_tsquery(request.query)
        pool_limit = max(request.candidate_limit * 4, request.candidate_limit)
        with self._connection.cursor() as cursor:
            if query:
                cursor.execute(
                    f"""
                    SELECT *, ts_rank_cd(
                        to_tsvector('simple', search_document),
                        to_tsquery('simple', %s)
                    ) AS lexical_rank
                    FROM agent_memory
                    WHERE {where}
                      AND to_tsvector('simple', search_document)
                          @@ to_tsquery('simple', %s)
                    ORDER BY lexical_rank DESC, importance DESC, updated_at DESC
                    LIMIT %s
                    """,
                    (query, *params, query, pool_limit),
                )
            else:
                cursor.execute(
                    f"""
                    SELECT *, 0.0 AS lexical_rank FROM agent_memory
                    WHERE {where}
                    ORDER BY importance DESC,updated_at DESC LIMIT %s
                    """,
                    (*params, pool_limit),
                )
            rows = cursor.fetchall()
        self._connection.rollback()
        hits: list[MemoryHit] = []
        excluded_facets = set(request.excluded_facets)
        for row in rows:
            record = self._memory(row)
            if excluded_facets.intersection(record.facets):
                continue
            lexical = max(0.0, float(row.get("lexical_rank") or 0.0))
            if query and lexical <= 0:
                continue
            facet_gain = len(set(request.required_facets) & set(record.facets)) * 0.15
            recency = 1.0 / (1.0 + max(0.0, now_epoch() - record.updated_at) / 86400.0)
            score = (
                lexical
                + facet_gain
                + record.importance * 0.10
                + recency * 0.03
                + scope_boost(
                    record_phase=record.phase,
                    record_run_id=record.run_id,
                    phase=request.phase,
                    run_id=request.run_id,
                )
            )
            hits.append(
                MemoryHit(
                    record=record,
                    score=score,
                    lexical_score=lexical,
                    reason="postgres-fts",
                )
            )
        hits.sort(key=lambda hit: (hit.score, hit.record.updated_at), reverse=True)
        return hits[: request.candidate_limit]

    def verify_active_memories(
        self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str]
    ) -> list[MemoryRecord]:
        return [
            memory
            for memory in self.get_memories(
                tenant_id=tenant_id, task_id=task_id, memory_ids=memory_ids
            )
            if memory.status in {MemoryStatus.ACTIVE, MemoryStatus.OPEN}
        ]

    def update_memory_indexed(
        self,
        *,
        tenant_id: str,
        task_id: str,
        memory_id: str,
        collection: str,
        primary_key: str,
    ) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE agent_memory SET milvus_collection=%s,milvus_primary_key=%s,indexed_at=%s,updated_at=%s WHERE tenant_id=%s AND task_id=%s AND id=%s",
                (
                    collection,
                    primary_key,
                    now_epoch(),
                    now_epoch(),
                    tenant_id,
                    task_id,
                    memory_id,
                ),
            )

    def pending_outbox(
        self, *, limit: int = 100, now: float | None = None
    ) -> list[OutboxRecord]:
        current = now_epoch() if now is None else now
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_outbox WHERE status IN ('PENDING','RETRY') AND next_retry_at<=%s ORDER BY created_at LIMIT %s",
                (current, max(1, limit)),
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._outbox(row) for row in rows]

    def claim_outbox(
        self,
        *,
        limit: int = 1,
        now: float | None = None,
        lease_seconds: float = 60.0,
        index_id: str = "",
    ) -> list[OutboxRecord]:
        """Atomically lease due rows so concurrent consumers cannot duplicate work."""
        current = now_epoch() if now is None else now
        lease_until = current + max(1.0, lease_seconds)
        claim_token = new_id("claim")
        with self._transaction() as cursor:
            index_filter = " AND payload->>'index_id'=%s" if index_id else ""
            claim_params: list[Any] = [current, current]
            if index_id:
                claim_params.append(index_id)
            claim_params.extend((max(1, limit), lease_until, claim_token))
            cursor.execute(
                f"""
                WITH candidates AS (
                    SELECT id FROM agent_outbox
                    WHERE ((status IN ('PENDING','RETRY') AND next_retry_at<=%s)
                        OR (status='PROCESSING' AND next_retry_at<=%s))
                        {index_filter}
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE agent_outbox AS target
                SET status='PROCESSING',next_retry_at=%s,claim_token=%s
                FROM candidates
                WHERE target.id=candidates.id
                RETURNING target.*
                """,
                claim_params,
            )
            rows = cursor.fetchall()
        return [self._outbox(row) for row in rows]

    def complete_outbox(self, outbox_id: str, *, claim_token: str) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE agent_outbox "
                "SET status='COMPLETED',processed_at=%s,claim_token='' "
                "WHERE id=%s AND status='PROCESSING' AND claim_token=%s",
                (now_epoch(), outbox_id, claim_token),
            )
            self._require_outbox_claim(cursor.rowcount, outbox_id)

    def fail_outbox(
        self, outbox_id: str, *, claim_token: str, retry_at: float
    ) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE agent_outbox "
                "SET status='RETRY',retry_count=retry_count+1,next_retry_at=%s,claim_token='' "
                "WHERE id=%s AND status='PROCESSING' AND claim_token=%s",
                (retry_at, outbox_id, claim_token),
            )
            self._require_outbox_claim(cursor.rowcount, outbox_id)

    def dead_letter_outbox(self, outbox_id: str, *, claim_token: str) -> None:
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE agent_outbox "
                "SET status='FAILED',processed_at=%s,claim_token='' "
                "WHERE id=%s AND status='PROCESSING' AND claim_token=%s",
                (now_epoch(), outbox_id, claim_token),
            )
            self._require_outbox_claim(cursor.rowcount, outbox_id)

    @staticmethod
    def _require_outbox_claim(updated: int, outbox_id: str) -> None:
        if updated != 1:
            raise OutboxClaimLostError(
                f"outbox claim is no longer current: {outbox_id}"
            )

    def put_checkpoint(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        with self._transaction() as cursor:
            self._put_checkpoint(cursor, checkpoint)
        return checkpoint

    def _put_checkpoint(
        self, cursor: Any, checkpoint: CheckpointRecord
    ) -> CheckpointRecord:
        cursor.execute(
            """
            INSERT INTO agent_checkpoint(
                id,tenant_id,task_id,run_id,state_version,current_step_id,memory_mode,
                snapshot,content_hash,schema_version,created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING
            """,
            (
                checkpoint.id,
                checkpoint.tenant_id,
                checkpoint.task_id,
                checkpoint.run_id,
                checkpoint.state_version,
                checkpoint.current_step_id,
                checkpoint.memory_mode.value,
                self._Jsonb(checkpoint.snapshot),
                checkpoint.content_hash,
                checkpoint.schema_version,
                checkpoint.created_at,
            ),
        )
        return checkpoint

    def latest_checkpoint(
        self, *, tenant_id: str, task_id: str
    ) -> CheckpointRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM agent_checkpoint WHERE tenant_id=%s AND task_id=%s ORDER BY created_at DESC LIMIT 1",
                (tenant_id, task_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return self._checkpoint(row) if row else None

    def all_checkpoints(
        self, *, tenant_id: str, task_id: str
    ) -> list[CheckpointRecord]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_checkpoint
                WHERE tenant_id=%s AND task_id=%s ORDER BY created_at ASC
                """,
                (tenant_id, task_id),
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._checkpoint(row) for row in rows]

    def get_checkpoint(
        self, *, tenant_id: str, task_id: str, checkpoint_id: str
    ) -> CheckpointRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM agent_checkpoint
                WHERE tenant_id=%s AND task_id=%s AND id=%s
                """,
                (tenant_id, task_id, checkpoint_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return self._checkpoint(row) if row else None

    def set_retention_policy(self, policy: TaskRetentionPolicy) -> TaskRetentionPolicy:
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO memory_retention_policy(
                    tenant_id,task_id,retention_until,legal_hold,deletion_mode,set_by,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,task_id) DO UPDATE SET
                    retention_until=excluded.retention_until,
                    legal_hold=excluded.legal_hold,
                    deletion_mode=excluded.deletion_mode,
                    set_by=excluded.set_by,
                    updated_at=excluded.updated_at
                """,
                (
                    policy.tenant_id,
                    policy.task_id,
                    policy.retention_until,
                    policy.legal_hold,
                    policy.deletion_mode,
                    policy.set_by,
                    policy.updated_at,
                ),
            )
        return policy

    def get_retention_policy(
        self, *, tenant_id: str, task_id: str
    ) -> TaskRetentionPolicy | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM memory_retention_policy WHERE tenant_id=%s AND task_id=%s",
                (tenant_id, task_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        if row is None:
            return None
        return TaskRetentionPolicy(
            tenant_id=row["tenant_id"],
            task_id=row["task_id"],
            retention_until=row["retention_until"],
            legal_hold=bool(row["legal_hold"]),
            deletion_mode=row["deletion_mode"],
            set_by=row["set_by"],
            updated_at=row["updated_at"],
        )

    def put_archive_manifest(self, manifest: ArchiveManifest) -> ArchiveManifest:
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM agent_artifact WHERE id=%s AND tenant_id=%s AND task_id=%s",
                (manifest.artifact_id, manifest.tenant_id, manifest.task_id),
            )
            artifact = cursor.fetchone()
            if artifact is None:
                raise ValueError("archive manifest artifact is missing or out of scope")
            if (
                artifact["uri"] != manifest.uri
                or artifact["content_hash"] != manifest.object_hash
                or artifact["size_bytes"] != manifest.compressed_size_bytes
            ):
                raise ValueError("archive manifest does not match artifact registry")
            cursor.execute(
                "SELECT * FROM memory_archive_manifest WHERE id=%s", (manifest.id,)
            )
            existing = cursor.fetchone()
            if existing is not None:
                incoming = self._archive_manifest_values(manifest)
                immutable = (
                    "tenant_id",
                    "task_id",
                    "artifact_id",
                    "uri",
                    "content_hash",
                    "object_hash",
                    "event_count",
                    "uncompressed_size_bytes",
                    "compressed_size_bytes",
                    "first_event_id",
                    "last_event_id",
                    "first_created_at",
                    "last_created_at",
                    "rule_name",
                    "backup_class",
                    "schema_version",
                )
                if any(existing[key] != incoming[key] for key in immutable):
                    raise ValueError("archive manifest immutable fields changed")
            values = self._archive_manifest_values(manifest)
            values["metadata"] = self._Jsonb(manifest.metadata)
            cursor.execute(
                """
                INSERT INTO memory_archive_manifest(
                    id,tenant_id,task_id,artifact_id,uri,archive_type,codec,media_type,
                    content_hash,object_hash,event_count,uncompressed_size_bytes,
                    compressed_size_bytes,first_event_id,last_event_id,first_created_at,
                    last_created_at,rule_name,backup_class,status,metadata,last_verified_at,
                    restored_at,schema_version,created_at,updated_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s
                )
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,metadata=excluded.metadata,
                    last_verified_at=excluded.last_verified_at,
                    restored_at=excluded.restored_at,updated_at=excluded.updated_at
                """,
                tuple(values.values()),
            )
        return manifest

    def get_archive_manifest(
        self, *, tenant_id: str, task_id: str, archive_id: str
    ) -> ArchiveManifest | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM memory_archive_manifest "
                "WHERE tenant_id=%s AND task_id=%s AND id=%s",
                (tenant_id, task_id, archive_id),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return self._archive_manifest(row) if row else None

    def list_archive_manifests(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> list[ArchiveManifest]:
        params: list[Any] = [tenant_id, task_id]
        where = "tenant_id=%s AND task_id=%s"
        if statuses:
            where += " AND status = ANY(%s)"
            params.append(list(statuses))
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM memory_archive_manifest WHERE {where} "
                "ORDER BY first_created_at ASC,id ASC",
                params,
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._archive_manifest(row) for row in rows]

    def working_set_get(self, *, tenant_id: str, task_id: str, phase: str) -> list[str]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT memory_id FROM memory_working_set WHERE tenant_id=%s AND task_id=%s AND phase=%s AND expires_at>%s ORDER BY hit_count DESC,last_used_at DESC",
                (tenant_id, task_id, phase, now_epoch()),
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [row["memory_id"] for row in rows]

    def working_set_touch(
        self,
        *,
        tenant_id: str,
        task_id: str,
        phase: str,
        memory_ids: Sequence[str],
        state_version: int,
        ttl_seconds: int = 3600,
    ) -> None:
        now = now_epoch()
        with self._transaction() as cursor:
            for memory_id in memory_ids:
                cursor.execute(
                    """
                    INSERT INTO memory_working_set(tenant_id,task_id,phase,memory_id,hit_count,state_version,expires_at,last_used_at)
                    VALUES (%s,%s,%s,%s,1,%s,%s,%s)
                    ON CONFLICT(tenant_id,task_id,phase,memory_id) DO UPDATE SET
                        hit_count=memory_working_set.hit_count+1,state_version=excluded.state_version,
                        expires_at=excluded.expires_at,last_used_at=excluded.last_used_at
                    """,
                    (
                        tenant_id,
                        task_id,
                        phase,
                        memory_id,
                        state_version,
                        now + ttl_seconds,
                        now,
                    ),
                )

    def working_set_invalidate(
        self, *, tenant_id: str, task_id: str, memory_ids: Sequence[str]
    ) -> None:
        if not memory_ids:
            return
        with self._transaction() as cursor:
            cursor.execute(
                "DELETE FROM memory_working_set WHERE tenant_id=%s AND task_id=%s AND memory_id = ANY(%s)",
                (tenant_id, task_id, list(memory_ids)),
            )

    def referenced_artifact_ids(self, *, tenant_id: str | None = None) -> set[str]:
        with self._connection.cursor() as cursor:
            if tenant_id:
                cursor.execute(
                    "SELECT id FROM agent_artifact WHERE status<>'TOMBSTONED' AND tenant_id=%s",
                    (tenant_id,),
                )
            else:
                cursor.execute(
                    "SELECT id FROM agent_artifact WHERE status<>'TOMBSTONED'"
                )
            rows = cursor.fetchall()
        self._connection.rollback()
        return {row["id"] for row in rows}

    def artifact_reference_exists(
        self, *, tenant_id: str, artifact_id: str, uri: str
    ) -> bool:
        serialized_id = f'%"{artifact_id}"%'
        checks = (
            (
                "SELECT 1 FROM agent_artifact WHERE tenant_id=%s AND (id=%s OR uri=%s) LIMIT 1",
                (tenant_id, artifact_id, uri),
            ),
            (
                "SELECT 1 FROM agent_event WHERE tenant_id=%s AND artifact_uri=%s LIMIT 1",
                (tenant_id, uri),
            ),
            (
                "SELECT 1 FROM agent_memory WHERE tenant_id=%s AND artifact_id=%s LIMIT 1",
                (tenant_id, artifact_id),
            ),
            (
                "SELECT 1 FROM agent_task_state WHERE tenant_id=%s AND active_artifacts::text LIKE %s LIMIT 1",
                (tenant_id, serialized_id),
            ),
            (
                "SELECT 1 FROM agent_checkpoint WHERE tenant_id=%s AND snapshot::text LIKE %s LIMIT 1",
                (tenant_id, serialized_id),
            ),
            (
                (
                    "SELECT 1 FROM memory_archive_manifest WHERE tenant_id=%s "
                    "AND (artifact_id=%s OR uri=%s) AND status<>'TOMBSTONED' LIMIT 1"
                ),
                (tenant_id, artifact_id, uri),
            ),
        )
        with self._connection.cursor() as cursor:
            found = False
            for query, params in checks:
                cursor.execute(query, params)
                if cursor.fetchone() is not None:
                    found = True
                    break
        self._connection.rollback()
        return found

    def hard_delete_task(
        self,
        *,
        tenant_id: str,
        task_id: str,
        request_id: str = "",
        actor_hash: str = "",
        reason_hash: str = "",
    ) -> dict[str, int]:
        tables = [
            "memory_working_set",
            "memory_retention_policy",
            "memory_archive_manifest",
            "agent_outbox",
            "agent_memory",
            "agent_checkpoint",
            "agent_dependency",
            "agent_decision",
            "agent_artifact",
            "agent_event",
            "agent_step",
            "agent_task_state",
            "agent_run",
            "agent_task",
        ]
        counts: dict[str, int] = {}
        with self._transaction() as cursor:
            cursor.execute("SELECT set_config('minicode.allow_hard_delete','on',true)")
            for table in tables:
                if table == "agent_outbox":
                    cursor.execute(
                        "DELETE FROM agent_outbox WHERE aggregate_id IN (SELECT id FROM agent_memory WHERE tenant_id=%s AND task_id=%s)",
                        (tenant_id, task_id),
                    )
                else:
                    column = "id" if table == "agent_task" else "task_id"
                    cursor.execute(
                        f"DELETE FROM {table} WHERE tenant_id=%s AND {column}=%s",
                        (tenant_id, task_id),
                    )
                counts[table] = max(0, cursor.rowcount)
            cursor.execute(
                "INSERT INTO memory_deletion_audit(request_id,tenant_id,task_id_hash,scope,actor_hash,reason_hash,execution_status,deleted_counts,requested_at,completed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request_id or new_id("delete"),
                    tenant_id,
                    content_hash_text(task_id),
                    "TASK",
                    actor_hash,
                    reason_hash,
                    "COMPLETED",
                    self._Jsonb(counts),
                    now_epoch(),
                    now_epoch(),
                ),
            )
        return counts

    def get_deletion_audit(self, *, request_id: str) -> Mapping[str, Any] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM memory_deletion_audit WHERE request_id=%s",
                (request_id,),
            )
            row = cursor.fetchone()
        self._connection.rollback()
        return dict(row) if row else None

    def all_memories(
        self, *, tenant_id: str | None = None, task_id: str | None = None
    ) -> list[MemoryRecord]:
        where: list[str] = []
        params: list[Any] = []
        if tenant_id:
            where.append("tenant_id=%s")
            params.append(tenant_id)
        if task_id:
            where.append("task_id=%s")
            params.append(task_id)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM agent_memory{clause} ORDER BY created_at", params
            )
            rows = cursor.fetchall()
        self._connection.rollback()
        return [self._memory(row) for row in rows]

    def _put_artifact(self, cursor: Any, artifact: ArtifactRecord) -> None:
        cursor.execute(
            """
            INSERT INTO agent_artifact(
                id,tenant_id,task_id,run_id,step_id,artifact_type,uri,content_hash,
                media_type,size_bytes,metadata,source_event_id,status,schema_version,created_at,updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET
                uri=excluded.uri,content_hash=excluded.content_hash,size_bytes=excluded.size_bytes,
                metadata=excluded.metadata,source_event_id=excluded.source_event_id,
                status=excluded.status,updated_at=excluded.updated_at
            """,
            (
                artifact.id,
                artifact.tenant_id,
                artifact.task_id,
                artifact.run_id,
                artifact.step_id,
                artifact.artifact_type,
                artifact.uri,
                artifact.content_hash,
                artifact.media_type,
                artifact.size_bytes,
                self._Jsonb(artifact.metadata),
                artifact.source_event_id,
                artifact.status.value,
                artifact.schema_version,
                artifact.created_at,
                artifact.updated_at,
            ),
        )

    def _put_memory(self, cursor: Any, memory: MemoryRecord) -> MemoryRecord:
        cursor.execute(
            """
            SELECT * FROM agent_memory
            WHERE tenant_id=%s AND task_id=%s AND memory_type=%s AND content_hash=%s
              AND source_event_id=%s AND artifact_id=%s
              AND source_start=%s AND source_end=%s
            """,
            (
                memory.tenant_id,
                memory.task_id,
                memory.memory_type.value,
                memory.content_hash,
                memory.source_event_id,
                memory.artifact_id,
                memory.source_range.start,
                memory.source_range.end,
            ),
        )
        existing = cursor.fetchone()
        if existing:
            return self._memory(existing)
        cursor.execute(
            """
            INSERT INTO agent_memory(
                id,tenant_id,task_id,run_id,step_id,memory_type,title,preview_text,status,
                importance,source_event_id,artifact_id,source_start,source_end,source_unit,
                content_hash,token_count,phase,facets,search_document,milvus_collection,
                milvus_primary_key,indexed_at,created_at,updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                memory.id,
                memory.tenant_id,
                memory.task_id,
                memory.run_id,
                memory.step_id,
                memory.memory_type.value,
                memory.title,
                memory.preview_text,
                memory.status.value,
                memory.importance,
                memory.source_event_id,
                memory.artifact_id,
                memory.source_range.start,
                memory.source_range.end,
                memory.source_range.unit,
                memory.content_hash,
                memory.token_count,
                memory.phase,
                self._Jsonb(memory.facets),
                memory_search_document(
                    title=memory.title,
                    preview_text=memory.preview_text,
                    facets=memory.facets,
                ),
                memory.milvus_collection,
                memory.milvus_primary_key,
                memory.indexed_at,
                memory.created_at,
                memory.updated_at,
            ),
        )
        return memory

    @staticmethod
    def _align_memory_outbox(outbox: OutboxRecord, memory: MemoryRecord) -> None:
        outbox.aggregate_id = memory.id
        outbox.payload = {
            **outbox.payload,
            "tenant_id": memory.tenant_id,
            "task_id": memory.task_id,
            "memory_id": memory.id,
            "content_hash": memory.content_hash,
        }

    def _put_outbox(self, cursor: Any, outbox: OutboxRecord) -> None:
        cursor.execute(
            """
            INSERT INTO agent_outbox(
                id,aggregate_type,aggregate_id,event_type,payload,status,retry_count,
                next_retry_at,created_at,processed_at,claim_token
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                outbox.id,
                outbox.aggregate_type,
                outbox.aggregate_id,
                outbox.event_type,
                self._Jsonb(outbox.payload),
                outbox.status,
                outbox.retry_count,
                outbox.next_retry_at,
                outbox.created_at,
                outbox.processed_at,
                outbox.claim_token,
            ),
        )

    def _write_state(
        self, cursor: Any, state: TaskStateSnapshot, *, expected_version: int
    ) -> None:
        cursor.execute(
            """
            UPDATE agent_task_state SET version=%s,phase=%s,current_step_id=%s,goal=%s,
                active_constraints=%s,active_decisions=%s,open_errors=%s,completed_steps=%s,
                pending_steps=%s,active_artifacts=%s,blocked_by=%s,updated_at=%s
            WHERE tenant_id=%s AND task_id=%s AND version=%s
            """,
            (
                state.version,
                state.phase,
                state.current_step_id,
                state.goal,
                self._Jsonb(state.active_constraints),
                self._Jsonb(state.active_decisions),
                self._Jsonb(state.open_errors),
                self._Jsonb(state.completed_steps),
                self._Jsonb(state.pending_steps),
                self._Jsonb(state.active_artifacts),
                self._Jsonb(state.blocked_by),
                state.updated_at,
                state.tenant_id,
                state.task_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StateVersionConflict(
                f"task state version conflict for {state.task_id}: expected {expected_version}"
            )

    @staticmethod
    def _normalize_json_rows(row: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        for key in (
            "content_json",
            "active_constraints",
            "active_decisions",
            "open_errors",
            "completed_steps",
            "pending_steps",
            "active_artifacts",
            "blocked_by",
            "structured_value",
            "metadata",
            "snapshot",
            "facets",
            "payload",
        ):
            if key in data and not isinstance(data[key], str):
                data[key] = json.dumps(data[key], ensure_ascii=False)
        return data

    @classmethod
    def _event(cls, row: Mapping[str, Any]) -> AgentEvent:
        return SqliteMemoryStore._event(cls._normalize_json_rows(row))

    @classmethod
    def _state(cls, row: Mapping[str, Any]) -> TaskStateSnapshot:
        return SqliteMemoryStore._state(cls._normalize_json_rows(row))

    @classmethod
    def _decision(cls, row: Mapping[str, Any]) -> DecisionRecord:
        return SqliteMemoryStore._decision(cls._normalize_json_rows(row))

    @staticmethod
    def _dependency(row: Mapping[str, Any]) -> DependencyRecord:
        return SqliteMemoryStore._dependency(row)

    @classmethod
    def _artifact(cls, row: Mapping[str, Any]) -> ArtifactRecord:
        return SqliteMemoryStore._artifact(cls._normalize_json_rows(row))

    @classmethod
    def _memory(cls, row: Mapping[str, Any]) -> MemoryRecord:
        return SqliteMemoryStore._memory(cls._normalize_json_rows(row))

    @classmethod
    def _checkpoint(cls, row: Mapping[str, Any]) -> CheckpointRecord:
        return SqliteMemoryStore._checkpoint(cls._normalize_json_rows(row))

    @classmethod
    def _outbox(cls, row: Mapping[str, Any]) -> OutboxRecord:
        return SqliteMemoryStore._outbox(cls._normalize_json_rows(row))

    @staticmethod
    def _archive_manifest_values(manifest: ArchiveManifest) -> dict[str, Any]:
        values = SqliteMemoryStore._archive_manifest_values(manifest)
        values["metadata"] = manifest.metadata
        return values

    @classmethod
    def _archive_manifest(cls, row: Mapping[str, Any]) -> ArchiveManifest:
        data = dict(row)
        metadata = data.get("metadata", {})
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        return ArchiveManifest(
            id=data["id"],
            tenant_id=data["tenant_id"],
            task_id=data["task_id"],
            artifact_id=data["artifact_id"],
            uri=data["uri"],
            archive_type=data["archive_type"],
            codec=data["codec"],
            media_type=data["media_type"],
            content_hash=data["content_hash"],
            object_hash=data["object_hash"],
            event_count=data["event_count"],
            uncompressed_size_bytes=data["uncompressed_size_bytes"],
            compressed_size_bytes=data["compressed_size_bytes"],
            first_event_id=data["first_event_id"],
            last_event_id=data["last_event_id"],
            first_created_at=data["first_created_at"],
            last_created_at=data["last_created_at"],
            rule_name=data["rule_name"],
            backup_class=data["backup_class"],
            status=ArchiveStatus(data["status"]),
            metadata=dict(metadata),
            last_verified_at=data["last_verified_at"],
            restored_at=data["restored_at"],
            schema_version=data["schema_version"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )
