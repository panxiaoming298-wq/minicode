from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace

import pytest

from minicode.paged_memory.admin import main as memory_admin_main
from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.lifecycle import (
    LifecycleManager,
    RetentionDenied,
    RetentionPolicyEngine,
)
from minicode.paged_memory.models import (
    AgentEvent,
    DependencyType,
    EventType,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryType,
    RetentionRule,
    TaskRetentionPolicy,
)
from minicode.paged_memory.service import PagedMemoryRuntime
from minicode.paged_memory.sqlite_store import SqliteMemoryStore
from minicode.paged_memory.vector_index import InMemoryVectorIndex


def _runtime(tmp_path, *, artifact_large_bytes: int = 4) -> PagedMemoryRuntime:
    config = PagedMemoryConfig(
        enabled=True,
        sqlite_path=str(tmp_path / "state.db"),
        artifact_root=str(tmp_path / "artifacts"),
        backup_root=str(tmp_path / "backups"),
        artifact_large_bytes=artifact_large_bytes,
        artifact_gc_grace_hours=1,
    )
    runtime = PagedMemoryRuntime(
        config=config, cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    runtime.capture_tool_result(
        call={"id": "call", "toolName": "query"}, output="payload", ok=True
    )
    runtime.finalize_step(success=True)
    return runtime


def test_retention_expiry_never_overrides_legal_hold() -> None:
    rule = RetentionRule(name="short", retention_seconds=10)
    now = 100.0
    assert RetentionPolicyEngine.expired(
        created_at=0, now=now, rule=rule, task_policy=None
    )
    assert not RetentionPolicyEngine.expired(
        created_at=0,
        now=now,
        rule=rule,
        task_policy=TaskRetentionPolicy(tenant_id="t", task_id="task", legal_hold=True),
    )


def test_retention_rules_match_tenant_type_sensitivity_and_task_status() -> None:
    sensitive_errors = RetentionRule(
        name="regulated-errors",
        retention_seconds=86400,
        archive_after_seconds=3600,
        deletion_mode="HARD_DELETE",
        backup_class="REGULATED",
        tenant_ids=("regulated",),
        memory_types=("ERROR",),
        sensitivity_levels=("HIGH",),
        task_statuses=("COMPLETED",),
    )
    engine = RetentionPolicyEngine([sensitive_errors])

    assert (
        engine.match(
            tenant_id="regulated",
            memory_type="ERROR",
            sensitivity_level="HIGH",
            task_status="COMPLETED",
        )
        is sensitive_errors
    )
    assert (
        engine.match(
            tenant_id="public",
            memory_type="ERROR",
            sensitivity_level="HIGH",
            task_status="COMPLETED",
        )
        is None
    )


def test_retention_rules_load_from_configuration(tmp_path) -> None:
    config = PagedMemoryConfig.from_sources(
        cwd=tmp_path,
        environ={
            "MEMORY_ARCHIVE_MAX_EVENTS": "321",
            "MEMORY_ARCHIVE_MAX_UNCOMPRESSED_BYTES": "654321",
            "MEMORY_RETENTION_RULES_JSON": (
                '[{"name":"sensitive-artifacts","artifact_types":["TOOL_RESULT"],'
                '"sensitivity_levels":["HIGH"],"retention_seconds":3600,'
                '"backup_class":"REGULATED"}]'
            ),
        },
    )

    assert len(config.retention_rules) == 1
    assert config.retention_rules[0].artifact_types == ("TOOL_RESULT",)
    assert config.retention_rules[0].backup_class == "REGULATED"
    assert config.archive_max_events == 321
    assert config.archive_max_uncompressed_bytes == 654321


def test_orphan_gc_has_grace_period_and_dry_run(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    orphan = runtime.artifact_store.put_bytes(
        b"orphan",
        tenant_id="local",
        task_id="orphan-task",
        run_id="run",
        step_id="step",
        media_type="text/plain",
        artifact_type="TEST",
    )
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    )
    assert (
        manager.orphan_candidates(tenant_id="local", now=time.time(), grace_hours=1)
        == []
    )
    future = time.time() + 7200
    dry = manager.gc_orphans(tenant_id="local", dry_run=True, now=future, grace_hours=1)
    assert [candidate["uri"] for candidate in dry["candidates"]] == [orphan.uri]
    assert runtime.artifact_store.get_bytes(orphan) == b"orphan"
    applied = manager.gc_orphans(
        tenant_id="local", dry_run=False, now=future, grace_hours=1
    )
    assert applied["deleted"] == [orphan.uri]
    runtime.close()


def test_orphan_gc_protects_event_reference_when_registry_is_missing(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    object_without_registry = runtime.artifact_store.put_bytes(
        b"referenced",
        tenant_id="local",
        task_id="task",
        run_id=runtime.run_id,
        step_id=runtime.current_step_id,
        media_type="text/plain",
        artifact_type="TEST",
    )
    runtime.authority.persist_event(
        AgentEvent(
            tenant_id="local",
            task_id="task",
            run_id=runtime.run_id,
            event_type=EventType.ARTIFACT_CREATED,
            artifact_uri=object_without_registry.uri,
            content_hash=object_without_registry.content_hash,
            idempotency_key="event-only-artifact-reference",
        )
    )
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    )

    candidates = manager.orphan_candidates(
        tenant_id="local", now=time.time() + 7200, grace_hours=1
    )

    assert object_without_registry.uri not in {
        candidate["uri"] for candidate in candidates
    }
    assert runtime.artifact_store.get_bytes(object_without_registry) == b"referenced"
    runtime.close()


def test_reference_integrity_detects_missing_artifact_object(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    )
    artifact = runtime.authority.list_artifacts(tenant_id="local", task_id="task")[0]
    runtime.artifact_store.delete(artifact)
    report = manager.reference_integrity_scan(tenant_id="local", task_id="task")
    assert not report["valid"]
    assert artifact.id in report["missing_artifact_objects"]
    runtime.close()


def test_reference_integrity_scans_step_dependency_and_checkpoint_graph(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    )
    healthy = manager.reference_integrity_scan(tenant_id="local", task_id="task")
    assert healthy["valid"]
    assert healthy["checkpoints"] == 1

    runtime.add_dependency(
        step_id=runtime.current_step_id,
        dependency_type=DependencyType.EVENT,
        dependency_id="missing-event",
    )
    broken = manager.reference_integrity_scan(tenant_id="local", task_id="task")

    assert not broken["valid"]
    assert broken["invalid_dependencies"] == [
        f"step:{runtime.current_step_id}:EVENT:missing-event"
    ]
    runtime.close()


def test_reference_integrity_reconciles_authority_and_vector_ids(tmp_path) -> None:
    vector = InMemoryVectorIndex()
    config = PagedMemoryConfig(
        enabled=True,
        sqlite_path=str(tmp_path / "state.db"),
        artifact_root=str(tmp_path / "artifacts"),
        backup_root=str(tmp_path / "backups"),
        artifact_large_bytes=10_000,
        milvus_enabled=True,
        milvus_uri="http://milvus.invalid:19530",
    )
    runtime = PagedMemoryRuntime(
        config=config,
        cwd=tmp_path,
        task_id="task",
        goal="vector consistency",
        vector_index=vector,
    )
    runtime.begin_step(1)
    constraint = runtime.record_constraint(text="preserve exact vector state")
    assert runtime.process_outbox()["processed"] == 1
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        vector_index=vector,
        config=config,
    )
    assert manager.reference_integrity_scan(tenant_id="local", task_id="task")["valid"]

    vector.delete([constraint.id])
    orphan = MemoryRecord(
        id="orphan-vector",
        tenant_id="local",
        task_id="task",
        memory_type=MemoryType.FACT,
        title="orphan",
        preview_text="not in authority",
        status=MemoryStatus.ACTIVE,
    )
    vector.upsert([orphan])
    broken = manager.reference_integrity_scan(tenant_id="local", task_id="task")

    assert not broken["valid"]
    assert broken["missing_vector_ids"] == [constraint.id]
    assert broken["orphan_vector_ids"] == [orphan.id]
    health = runtime.health_snapshot()
    assert health["state"] == "INDEX_DEGRADED"
    assert health["vector_consistency"]["missing"] == 1
    assert health["vector_consistency"]["orphan"] == 1
    runtime.close()


def test_reference_integrity_detects_same_id_vector_hash_drift(tmp_path) -> None:
    vector = InMemoryVectorIndex()
    config = PagedMemoryConfig(
        enabled=True,
        sqlite_path=str(tmp_path / "state.db"),
        artifact_root=str(tmp_path / "artifacts"),
        backup_root=str(tmp_path / "backups"),
        artifact_large_bytes=10_000,
        milvus_enabled=True,
        milvus_uri="http://milvus.invalid:19530",
    )
    runtime = PagedMemoryRuntime(
        config=config,
        cwd=tmp_path,
        task_id="task",
        goal="hash-aware vector consistency",
        vector_index=vector,
    )
    runtime.begin_step(1)
    constraint = runtime.record_constraint(text="preserve the authoritative hash")
    assert runtime.process_outbox()["processed"] == 1
    vector._records[constraint.id][0].content_hash = "sha256:" + "0" * 64

    report = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        vector_index=vector,
        config=config,
    ).reference_integrity_scan(tenant_id="local", task_id="task")
    health = runtime.health_snapshot()

    assert not report["valid"]
    assert report["missing_vector_ids"] == []
    assert report["orphan_vector_ids"] == []
    assert report["vector_hash_mismatch_ids"] == [constraint.id]
    assert health["state"] == "INDEX_DEGRADED"
    assert health["vector_consistency"]["hash_mismatch"] == 1
    assert health["vector_consistency"]["hash_mismatch_ids"] == [constraint.id]
    runtime.close()


def test_reference_integrity_verifies_artifact_chunk_digest(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    chunk = next(
        memory
        for memory in runtime.authority.all_memories(tenant_id="local", task_id="task")
        if memory.memory_type == MemoryType.ARTIFACT_CHUNK
    )
    runtime.authority._connection.execute(
        "UPDATE agent_memory SET content_hash=? WHERE id=?",
        ("sha256:" + "0" * 64, chunk.id),
    )
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    )

    report = manager.reference_integrity_scan(tenant_id="local", task_id="task")

    assert not report["valid"]
    assert report["memory_hash_mismatches"] == [chunk.id]
    runtime.close()


def test_admin_backfills_only_historical_text_artifact_chunks(
    tmp_path, monkeypatch, capsys
) -> None:
    config = PagedMemoryConfig(
        enabled=True,
        sqlite_path=str(tmp_path / "state.db"),
        artifact_root=str(tmp_path / "artifacts"),
        backup_root=str(tmp_path / "backups"),
        artifact_large_bytes=4,
    )
    runtime = PagedMemoryRuntime(
        config=config, cwd=tmp_path, task_id="task", goal="historical backfill"
    )
    runtime.begin_step(1)
    tail_marker = "historicaltailmarker"
    output = ("prefix material for chunking\n" * 500) + tail_marker
    runtime.capture_tool_result(
        call={"id": "historical", "toolName": "query"},
        output=output,
        ok=True,
    )
    runtime.authority._connection.execute(
        "DELETE FROM agent_memory WHERE memory_type='ARTIFACT_CHUNK'"
    )
    runtime.authority._connection.commit()
    runtime.close()

    monkeypatch.setenv("MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("MEMORY_SQLITE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("ARTIFACT_STORE_TYPE", "file")
    monkeypatch.setenv("ARTIFACT_STORE_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MEMORY_LOCAL_SPOOL_ENABLED", "false")
    monkeypatch.setenv("MILVUS_ENABLED", "false")

    base_args = [
        "--cwd",
        str(tmp_path),
        "backfill-artifact-chunks",
        "--tenant-id",
        "local",
        "--task-id",
        "task",
    ]
    assert memory_admin_main(base_args) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"]
    assert dry_run["missing_chunks"] > 1
    assert dry_run["inserted_chunks"] == 0

    assert memory_admin_main([*base_args, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["inserted_chunks"] == applied["missing_chunks"]

    store = SqliteMemoryStore(tmp_path / "state.db")
    hits = store.search_memories_lexical(
        MemorySearchRequest(
            tenant_id="local",
            task_id="task",
            query=tail_marker,
        )
    )
    assert any(hit.record.memory_type is MemoryType.ARTIFACT_CHUNK for hit in hits)
    store.close()


def test_backup_verify_and_restore_drill_cover_state_and_artifact(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    )
    backup = manager.create_local_backup()
    verified = manager.verify_local_backup(backup)
    drill = manager.restore_drill(backup)
    assert verified["valid"] and verified["artifact_count"] == 1
    assert drill["valid"] and drill["events"] >= 1 and drill["states"] == 1
    runtime.close()


def test_replay_artifacts_admin_command_runs_integrity_check(tmp_path, capsys) -> None:
    exit_code = memory_admin_main(
        [
            "--cwd",
            str(tmp_path),
            "replay-artifacts",
            "--tenant-id",
            "local",
            "--task-id",
            "task",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["replay"] == {"uploaded": 0, "failed": 0, "skipped": 0}
    assert report["integrity"]["valid"]


def test_real_delete_is_blocked_by_legal_hold_then_propagates(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    )
    manager.set_task_retention(
        tenant_id="local",
        task_id="task",
        retention_until=0,
        legal_hold=True,
        set_by="compliance",
    )
    with pytest.raises(RetentionDenied, match="legal hold"):
        manager.delete_task(
            tenant_id="local",
            task_id="task",
            authorized=True,
            actor="admin",
            reason="request",
        )
    manager.set_task_retention(
        tenant_id="local",
        task_id="task",
        retention_until=0,
        legal_hold=False,
        set_by="compliance",
    )
    report = manager.delete_task(
        tenant_id="local",
        task_id="task",
        authorized=True,
        actor="admin",
        reason="request",
    )
    assert report["artifact_objects"] == 1
    assert runtime.authority.get_state(tenant_id="local", task_id="task") is None
    audit = runtime.authority.get_deletion_audit(request_id=report["request_id"])
    assert audit is not None
    assert audit["execution_status"] == "COMPLETED"
    assert audit["actor_hash"] == report["actor_hash"]
    assert audit["reason_hash"] == report["reason_hash"]
    assert audit["task_id_hash"] == report["task_id_hash"]
    assert "admin" not in audit.values()
    assert "request" not in audit.values()
    assert "payload" not in str(audit)
    runtime.close()


def test_real_delete_fails_closed_when_configured_index_is_unavailable(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        vector_index=None,
        config=replace(runtime.config, milvus_enabled=True),
    )

    with pytest.raises(RetentionDenied, match="vector index is unavailable"):
        manager.delete_task(
            tenant_id="local",
            task_id="task",
            authorized=True,
            actor="admin",
            reason="request",
        )

    assert runtime.authority.get_state(tenant_id="local", task_id="task") is not None
    assert runtime.authority.list_artifacts(tenant_id="local", task_id="task")
    runtime.close()


def test_partial_distributed_delete_keeps_fence_and_can_resume(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.begin_step(2)
    runtime.capture_tool_result(
        call={"id": "second", "toolName": "query"},
        output="second-payload",
        ok=True,
    )
    artifacts = runtime.authority.list_artifacts(tenant_id="local", task_id="task")
    assert len(artifacts) == 2

    class FailSecondDelete:
        def __init__(self, inner) -> None:
            self.inner = inner
            self.calls = 0

        def delete(self, artifact) -> None:
            self.calls += 1
            if self.calls == 2:
                raise OSError("injected delete failure")
            self.inner.delete(artifact)

    manager = LifecycleManager(
        authority=runtime.authority,
        artifact_store=FailSecondDelete(runtime.artifact_store),
        config=runtime.config,
    )
    with pytest.raises(RetentionDenied, match="distributed delete incomplete"):
        manager.delete_task(
            tenant_id="local",
            task_id="task",
            authorized=True,
            actor="admin",
            reason="verified request",
        )

    assert (
        runtime.authority.task_status(tenant_id="local", task_id="task") == "DELETING"
    )
    with pytest.raises(sqlite3.IntegrityError, match="task is not ACTIVE"):
        runtime.authority.persist_event(
            AgentEvent(
                tenant_id="local",
                task_id="task",
                run_id=runtime.run_id,
                event_type=EventType.USER_INPUT,
                content_text="late",
            )
        )

    report = LifecycleManager(
        authority=runtime.authority,
        artifact_store=runtime.artifact_store,
        config=runtime.config,
    ).delete_task(
        tenant_id="local",
        task_id="task",
        authorized=True,
        actor="admin",
        reason="resume verified request",
    )
    assert report["execution_status"] == "COMPLETED"
    assert runtime.authority.get_state(tenant_id="local", task_id="task") is None
    runtime.close()
