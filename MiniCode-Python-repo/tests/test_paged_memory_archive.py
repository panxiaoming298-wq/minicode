from __future__ import annotations

from pathlib import Path

import pytest

from minicode.paged_memory.archive import (
    ArchiveIntegrityError,
    EventArchiveManager,
)
from minicode.paged_memory.artifacts import FileArtifactStore
from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.lifecycle import LifecycleManager
from minicode.paged_memory.models import (
    AgentEvent,
    ArchiveStatus,
    EventType,
    RetentionRule,
)
from minicode.paged_memory.sqlite_store import SqliteMemoryStore


def _components(tmp_path: Path, *, rules=()):
    store = SqliteMemoryStore(tmp_path / "state.db")
    store.ensure_task(
        tenant_id="tenant-a",
        task_id="task-a",
        goal="archive exact events",
    )
    store.create_run(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        mode="PAGED_MEMORY",
    )
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    manager = EventArchiveManager(
        authority=store,
        artifact_store=artifacts,
        retention_rules=rules,
    )
    return store, artifacts, manager


def _event(*, event_id: str, created_at: float, text: str) -> AgentEvent:
    return AgentEvent(
        id=event_id,
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        step_id="step-a",
        event_type=EventType.TOOL_RESULT,
        content_text=text,
        content_json={"nested": {"exact": True}, "unicode": "窗口外记忆"},
        idempotency_key=f"source:{event_id}",
        created_at=created_at,
    )


def test_event_archive_is_lossless_verified_and_idempotent(tmp_path: Path) -> None:
    store, _artifacts, manager = _components(tmp_path)
    first = _event(event_id="event-a", created_at=10.0, text="原始正文 A\nline 2")
    second = _event(event_id="event-b", created_at=11.0, text="原始正文 B")
    store.persist_event(first)
    store.persist_event(second)

    preview = manager.archive_range(
        tenant_id="tenant-a", task_id="task-a", dry_run=True
    )
    assert preview["event_count"] == 2
    assert store.list_archive_manifests(tenant_id="tenant-a", task_id="task-a") == []

    created = manager.archive_range(
        tenant_id="tenant-a", task_id="task-a", dry_run=False
    )
    assert created["created"] is True
    assert created["status"] == ArchiveStatus.VERIFIED.value
    assert created["source_events_deleted"] == 0
    archive_id = created["archive_id"]

    documents = manager.read_archive_events(
        tenant_id="tenant-a",
        task_id="task-a",
        archive_id=archive_id,
        limit=10,
    )
    assert [value["id"] for value in documents] == ["event-a", "event-b"]
    assert documents[0]["content_text"] == first.content_text
    assert documents[0]["content_json"] == first.content_json
    assert documents[0]["content_hash"] == first.content_hash
    assert store.get_event(
        tenant_id="tenant-a", task_id="task-a", event_id="event-a"
    ) is not None

    repeated = manager.archive_range(
        tenant_id="tenant-a", task_id="task-a", dry_run=False
    )
    assert repeated["existing"] is True
    assert repeated["archive_id"] == archive_id
    assert len(
        store.list_archive_manifests(tenant_id="tenant-a", task_id="task-a")
    ) == 1
    assert manager.verify_archive(
        tenant_id="tenant-a", task_id="task-a", archive_id=archive_id
    )["valid"]
    store.close()


def test_archive_detects_object_tampering(tmp_path: Path) -> None:
    store, artifacts, manager = _components(tmp_path)
    store.persist_event(_event(event_id="event-a", created_at=10.0, text="exact"))
    result = manager.archive_range(
        tenant_id="tenant-a", task_id="task-a", dry_run=False
    )
    manifest = store.get_archive_manifest(
        tenant_id="tenant-a", task_id="task-a", archive_id=result["archive_id"]
    )
    assert manifest is not None
    artifact = store.get_artifact(
        tenant_id="tenant-a",
        task_id="task-a",
        artifact_id=manifest.artifact_id,
    )
    assert artifact is not None
    target = artifacts._resolve(artifact.uri.removeprefix("file:"))
    target.write_bytes(b"not-the-archive")

    with pytest.raises(ArchiveIntegrityError):
        manager.verify_archive(
            tenant_id="tenant-a",
            task_id="task-a",
            archive_id=manifest.id,
        )
    store.close()


def test_archive_restores_missing_event_with_original_identity(tmp_path: Path) -> None:
    store, _artifacts, manager = _components(tmp_path)
    original = _event(
        event_id="event-restore-me", created_at=10.0, text="must stay byte exact"
    )
    store.persist_event(original)
    result = manager.archive_range(
        tenant_id="tenant-a", task_id="task-a", dry_run=False
    )

    store._hard_delete_allowed = True
    try:
        with store._transaction() as db:
            db.execute("DELETE FROM agent_event WHERE id=?", (original.id,))
    finally:
        store._hard_delete_allowed = False

    preview = manager.restore_archive(
        tenant_id="tenant-a",
        task_id="task-a",
        archive_id=result["archive_id"],
        dry_run=True,
    )
    assert preview["missing_event_count"] == 1
    applied = manager.restore_archive(
        tenant_id="tenant-a",
        task_id="task-a",
        archive_id=result["archive_id"],
        dry_run=False,
    )
    assert applied["restored_event_count"] == 1
    restored = store.get_event(
        tenant_id="tenant-a", task_id="task-a", event_id=original.id
    )
    assert restored is not None
    assert restored.content_text == original.content_text
    assert restored.content_json == original.content_json
    assert restored.content_hash == original.content_hash
    manifest = store.get_archive_manifest(
        tenant_id="tenant-a", task_id="task-a", archive_id=result["archive_id"]
    )
    assert manifest is not None and manifest.status is ArchiveStatus.RESTORED
    store.close()


def test_archive_due_executes_retention_rule_without_duplicates(tmp_path: Path) -> None:
    rules = (
        RetentionRule(
            name="cold-events",
            archive_after_seconds=60,
            event_types=(EventType.TOOL_RESULT.value,),
            backup_class="COLD",
        ),
    )
    store, _artifacts, manager = _components(tmp_path, rules=rules)
    store.persist_event(_event(event_id="event-old", created_at=10.0, text="old"))

    preview = manager.archive_due(
        tenant_id="tenant-a", task_id="task-a", now=100.0, dry_run=True
    )
    assert preview["candidate_count"] == 1
    assert preview["archive_count"] == 1
    assert store.list_archive_manifests(tenant_id="tenant-a", task_id="task-a") == []

    applied = manager.archive_due(
        tenant_id="tenant-a", task_id="task-a", now=100.0, dry_run=False
    )
    assert applied["candidate_count"] == 1
    assert applied["operations"][0]["backup_class"] == "COLD"
    repeated = manager.archive_due(
        tenant_id="tenant-a", task_id="task-a", now=200.0, dry_run=False
    )
    assert repeated["candidate_count"] == 0
    assert repeated["archive_count"] == 0
    store.close()


def test_local_backup_restore_drill_includes_archive_manifest_and_object(
    tmp_path: Path,
) -> None:
    store, artifacts, manager = _components(tmp_path)
    store.persist_event(_event(event_id="event-a", created_at=10.0, text="backup me"))
    manager.archive_range(tenant_id="tenant-a", task_id="task-a", dry_run=False)
    lifecycle = LifecycleManager(
        authority=store,
        artifact_store=artifacts,
        config=PagedMemoryConfig(
            backup_root=str(tmp_path / "backups"),
            artifact_root=str(tmp_path / "artifacts"),
            sqlite_path=str(tmp_path / "state.db"),
        ),
    )

    backup = lifecycle.create_local_backup()
    assert lifecycle.verify_local_backup(backup)["valid"]
    drill = lifecycle.restore_drill(backup)
    assert drill["restore_drill_success"]
    assert drill["archives"] == 1
    assert drill["artifacts"] == 1
    store.close()
