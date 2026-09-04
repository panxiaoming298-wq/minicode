from __future__ import annotations

import sqlite3

import pytest

from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.indexer import OutboxIndexer
from minicode.paged_memory.models import (
    AgentEvent,
    CheckpointRecord,
    DecisionRecord,
    EventType,
    MemoryMode,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryType,
    OutboxRecord,
    TaskRetentionPolicy,
)
from minicode.paged_memory.sqlite_store import SqliteMemoryStore, StateVersionConflict
from minicode.paged_memory.vector_index import InMemoryVectorIndex


def _prepared_store(tmp_path) -> SqliteMemoryStore:
    store = SqliteMemoryStore(tmp_path / "state.db")
    store.ensure_task(tenant_id="tenant-a", task_id="task-a", goal="goal")
    store.create_run(
        tenant_id="tenant-a", task_id="task-a", run_id="run-a", mode="INLINE"
    )
    store.upsert_step(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        step_id="step-a",
        sequence_no=1,
    )
    return store


def _index_outbox(
    memory: MemoryRecord, vector: InMemoryVectorIndex
) -> OutboxRecord:
    descriptor = vector.index_descriptor
    return OutboxRecord(
        aggregate_type="MEMORY",
        aggregate_id=memory.id,
        event_type="UPSERT_MEMORY_INDEX",
        payload={
            "tenant_id": memory.tenant_id,
            "task_id": memory.task_id,
            "memory_id": memory.id,
            "content_hash": memory.content_hash,
            "index_id": descriptor.index_id,
            "collection": descriptor.collection,
            "index_descriptor": descriptor.to_dict(),
        },
    )


def test_event_idempotency_and_append_only_trigger(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    event = AgentEvent(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        event_type=EventType.USER_INPUT,
        content_text="original",
        idempotency_key="same-request",
    )
    first, _ = store.persist_event(event)
    duplicate, _ = store.persist_event(
        AgentEvent(
            tenant_id="tenant-a",
            task_id="task-a",
            run_id="run-a",
            event_type=EventType.USER_INPUT,
            content_text="different",
            idempotency_key="same-request",
        )
    )
    assert duplicate.id == first.id
    assert duplicate.content_text == "original"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._connection.execute(
            "UPDATE agent_event SET content_text='changed' WHERE id=?", (first.id,)
        )
    store.close()


def test_event_idempotency_key_is_scoped_to_task(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    store.ensure_task(tenant_id="tenant-a", task_id="task-b", goal="other")
    store.create_run(
        tenant_id="tenant-a", task_id="task-b", run_id="run-b", mode="INLINE"
    )
    first, _ = store.persist_event(
        AgentEvent(
            tenant_id="tenant-a",
            task_id="task-a",
            run_id="run-a",
            event_type=EventType.USER_INPUT,
            content_text="task a",
            idempotency_key="shared-operation-key",
        )
    )
    second, _ = store.persist_event(
        AgentEvent(
            tenant_id="tenant-a",
            task_id="task-b",
            run_id="run-b",
            event_type=EventType.USER_INPUT,
            content_text="task b",
            idempotency_key="shared-operation-key",
        )
    )

    assert first.id != second.id
    assert second.task_id == "task-b"
    assert second.content_text == "task b"
    store.close()


def test_task_state_optimistic_lock(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    state = store.get_state(tenant_id="tenant-a", task_id="task-a")
    assert state is not None
    updated = state.apply_patch({"phase": "BUILD"})
    store.compare_and_set_state(updated, expected_version=state.version)
    with pytest.raises(StateVersionConflict):
        store.compare_and_set_state(
            updated,
            expected_version=state.version,
        )
    store.close()


def test_decision_supersede_excludes_old_version(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    first = store.put_decision(
        DecisionRecord(
            tenant_id="tenant-a",
            task_id="task-a",
            decision_key="database",
            decision_text="SQLite",
        )
    )
    second = store.put_decision(
        DecisionRecord(
            tenant_id="tenant-a",
            task_id="task-a",
            decision_key="database",
            decision_text="PostgreSQL",
        )
    )
    active = store.active_decisions(tenant_id="tenant-a", task_id="task-a")
    history = store.decision_history(
        tenant_id="tenant-a", task_id="task-a", decision_key="database"
    )
    by_id = {decision.id: decision for decision in history}
    assert [decision.id for decision in active] == [second.id]
    assert by_id[first.id].status is MemoryStatus.SUPERSEDED
    assert by_id[first.id].superseded_by_id == second.id
    assert second.supersedes_id == first.id
    store.close()


def test_event_and_decision_are_one_atomic_commit(tmp_path, monkeypatch) -> None:
    store = _prepared_store(tmp_path)
    event = AgentEvent(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        event_type=EventType.DECISION_CREATED,
        content_text="PostgreSQL",
        idempotency_key="decision-atomic",
    )
    decision = DecisionRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        decision_key="database",
        decision_text="PostgreSQL",
        source_event_id=event.id,
    )

    def fail_decision(*_args, **_kwargs):
        raise sqlite3.IntegrityError("injected decision failure")

    monkeypatch.setattr(store, "_put_decision", fail_decision)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.persist_event(event, decision=decision)

    assert (
        store.get_event(tenant_id="tenant-a", task_id="task-a", event_id=event.id)
        is None
    )
    assert store.active_decisions(tenant_id="tenant-a", task_id="task-a") == []
    store.close()


def test_idempotent_existing_event_never_accepts_a_new_projection(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    committed = AgentEvent(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        event_type=EventType.DECISION_CREATED,
        content_text="original",
        idempotency_key="existing-event",
    )
    store.persist_event(committed)
    phantom_event = AgentEvent(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        event_type=EventType.DECISION_CREATED,
        content_text="phantom",
        idempotency_key="existing-event",
    )
    phantom_decision = DecisionRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        decision_key="database",
        decision_text="phantom",
        source_event_id=phantom_event.id,
    )

    resolved, _ = store.persist_event(phantom_event, decision=phantom_decision)

    assert resolved.id == committed.id
    assert store.active_decisions(tenant_id="tenant-a", task_id="task-a") == []
    store.close()


def test_event_and_checkpoint_are_one_atomic_commit(tmp_path, monkeypatch) -> None:
    store = _prepared_store(tmp_path)
    before = store.get_state(tenant_id="tenant-a", task_id="task-a")
    assert before is not None
    checkpoint = CheckpointRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        state_version=before.version + 1,
        current_step_id="step-a",
        memory_mode=MemoryMode.INLINE,
        snapshot={"task_state": {"version": 0}},
    )
    event = AgentEvent(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        step_id="step-a",
        event_type=EventType.CHECKPOINT_CREATED,
        content_text=checkpoint.id,
        idempotency_key="checkpoint-atomic",
    )

    def fail_checkpoint(*_args, **_kwargs):
        raise sqlite3.IntegrityError("injected checkpoint failure")

    monkeypatch.setattr(store, "_put_checkpoint", fail_checkpoint)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.persist_event(
            event,
            state_patch={"phase": "DONE"},
            checkpoint=checkpoint,
            step_status="COMPLETED",
        )

    assert (
        store.get_event(tenant_id="tenant-a", task_id="task-a", event_id=event.id)
        is None
    )
    assert store.latest_checkpoint(tenant_id="tenant-a", task_id="task-a") is None
    after = store.get_state(tenant_id="tenant-a", task_id="task-a")
    assert after is not None and after.version == before.version
    step = store.get_step(tenant_id="tenant-a", task_id="task-a", step_id="step-a")
    assert step is not None and step["status"] == "RUNNING"
    store.close()


def test_tenant_and_task_scope_are_mandatory_for_search(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    store.ensure_task(tenant_id="tenant-b", task_id="task-b", goal="other")
    store.put_memory(
        MemoryRecord(
            tenant_id="tenant-a",
            task_id="task-a",
            memory_type=MemoryType.FACT,
            title="A",
            preview_text="shared secret alpha",
        )
    )
    store.put_memory(
        MemoryRecord(
            tenant_id="tenant-b",
            task_id="task-b",
            memory_type=MemoryType.FACT,
            title="B",
            preview_text="shared secret beta",
        )
    )
    hits = store.search_memories_lexical(
        MemorySearchRequest(
            tenant_id="tenant-a", task_id="task-a", query="shared secret"
        )
    )
    assert hits
    assert {(hit.record.tenant_id, hit.record.task_id) for hit in hits} == {
        ("tenant-a", "task-a")
    }
    store.close()


def test_lexical_search_keeps_cross_run_phase_memory_visible(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    matching = MemoryRecord(
        id="matching-scope",
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        memory_type=MemoryType.FACT,
        title="vector repair",
        preview_text="keep the durable semantic retrieval marker",
        phase="BUILD",
        source_event_id="event-current-scope",
    )
    older = MemoryRecord(
        id="older-scope",
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-before",
        memory_type=MemoryType.FACT,
        title="earlier vector repair",
        preview_text="keep the durable semantic retrieval marker",
        phase="DISCOVERY",
        source_event_id="event-older-scope",
    )
    store.put_memory(matching)
    store.put_memory(older)

    hits = store.search_memories_lexical(
        MemorySearchRequest(
            tenant_id="tenant-a",
            task_id="task-a",
            run_id="run-a",
            phase="BUILD",
            query="semantic retrieval marker",
            candidate_limit=10,
        )
    )

    assert {hit.record.id for hit in hits} == {matching.id, older.id}
    assert hits[0].record.id == matching.id
    store.close()


def test_fts_finds_relevant_memory_beyond_old_thousand_row_scan(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    target = MemoryRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        memory_type=MemoryType.FACT,
        title="old but relevant",
        preview_text="ZXQDEEPNEEDLE987 长期记忆召回",
        importance=0.01,
    )
    store.put_memory(target)
    for index in range(1_001):
        store.put_memory(
            MemoryRecord(
                tenant_id="tenant-a",
                task_id="task-a",
                memory_type=MemoryType.FACT,
                title=f"recent filler {index}",
                preview_text=f"unrelated material {index}",
                importance=1.0,
            )
        )

    hits = store.search_memories_lexical(
        MemorySearchRequest(
            tenant_id="tenant-a",
            task_id="task-a",
            query="ZXQDEEPNEEDLE987 长期记忆",
            candidate_limit=5,
        )
    )

    assert target.id in {hit.record.id for hit in hits}
    store.close()


def test_outbox_replay_is_idempotent(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    memory = MemoryRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        step_id="step-a",
        memory_type=MemoryType.FACT,
        title="fact",
        preview_text="durable source",
        source_event_id="event-source",
    )
    vector = InMemoryVectorIndex()
    outbox = _index_outbox(memory, vector)
    store.put_memory(memory, outbox=outbox)
    worker = OutboxIndexer(
        authority=store, vector_index=vector, config=PagedMemoryConfig()
    )
    assert worker.run_once()["processed"] == 1
    assert worker.run_once()["processed"] == 0
    assert len(vector.get([memory.id])) == 1
    store.close()


def test_outbox_consumer_embeds_and_upserts_one_claimed_batch(tmp_path) -> None:
    store = _prepared_store(tmp_path)

    class BatchRecordingVector(InMemoryVectorIndex):
        def __init__(self) -> None:
            super().__init__()
            self.batches: list[list[str]] = []

        def upsert(self, records):
            self.batches.append([record.id for record in records])
            return super().upsert(records)

    vector = BatchRecordingVector()
    memories = [
        MemoryRecord(
            id=f"batched-{index}",
            tenant_id="tenant-a",
            task_id="task-a",
            run_id="run-a",
            step_id="step-a",
            memory_type=MemoryType.FACT,
            title=f"fact {index}",
            preview_text=f"durable batch item {index}",
            source_event_id=f"event-source-{index}",
        )
        for index in range(3)
    ]
    for memory in memories:
        store.put_memory(memory, outbox=_index_outbox(memory, vector))

    result = OutboxIndexer(
        authority=store,
        vector_index=vector,
        config=PagedMemoryConfig(),
    ).run_once(limit=10)

    assert result == {"processed": 3, "retried": 0, "failed": 0, "skipped": 0}
    assert len(vector.batches) == 1
    assert set(vector.batches[0]) == {memory.id for memory in memories}
    assert {record.id for record in vector.get([memory.id for memory in memories])} == {
        memory.id for memory in memories
    }
    store.close()


def test_outbox_claim_lease_prevents_duplicate_consumers(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    memory = MemoryRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        step_id="step-a",
        memory_type=MemoryType.FACT,
        title="leased fact",
        preview_text="only one consumer may own this row",
        source_event_id="event-source",
    )
    vector = InMemoryVectorIndex()
    outbox = _index_outbox(memory, vector)
    store.put_memory(memory, outbox=outbox)
    sibling = SqliteMemoryStore(tmp_path / "state.db")

    first_claim = store.claim_outbox(now=100.0, lease_seconds=10.0)
    duplicate_claim = sibling.claim_outbox(now=100.0, lease_seconds=10.0)
    recovered_claim = sibling.claim_outbox(now=111.0, lease_seconds=10.0)

    assert [item.id for item in first_claim] == [outbox.id]
    assert duplicate_claim == []
    assert [item.id for item in recovered_claim] == [outbox.id]
    assert first_claim[0].claim_token
    assert recovered_claim[0].claim_token
    assert first_claim[0].claim_token != recovered_claim[0].claim_token
    with pytest.raises(RuntimeError, match="claim is no longer current"):
        store.complete_outbox(
            outbox.id, claim_token=first_claim[0].claim_token
        )
    sibling.complete_outbox(
        outbox.id, claim_token=recovered_claim[0].claim_token
    )
    assert sibling.pending_outbox(now=200.0) == []
    sibling.close()
    store.close()


def test_outbox_consumer_removes_vector_when_task_enters_delete_fence(
    tmp_path,
) -> None:
    store = _prepared_store(tmp_path)
    memory = MemoryRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        run_id="run-a",
        step_id="step-a",
        memory_type=MemoryType.FACT,
        title="deletion race",
        preview_text="must not survive task deletion",
        source_event_id="event-source",
    )
    class DeleteFencingVector(InMemoryVectorIndex):
        def upsert(self, records):
            super().upsert(records)
            assert store.begin_task_delete(
                tenant_id="tenant-a", task_id="task-a"
            ) == "ACTIVE"

    vector = DeleteFencingVector()
    outbox = _index_outbox(memory, vector)
    store.put_memory(memory, outbox=outbox)
    worker = OutboxIndexer(
        authority=store, vector_index=vector, config=PagedMemoryConfig()
    )

    result = worker.run_once()

    assert result == {"processed": 0, "retried": 0, "failed": 0, "skipped": 1}
    assert vector.get([memory.id]) == []
    assert store.pending_outbox(limit=10) == []
    assert store.task_status(tenant_id="tenant-a", task_id="task-a") == "DELETING"
    assert store.restore_task_status(
        tenant_id="tenant-a", task_id="task-a", status="ACTIVE"
    )
    store.close()


def test_deleting_task_rejects_new_authoritative_writes(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    assert store.begin_task_delete(
        tenant_id="tenant-a", task_id="task-a"
    ) == "ACTIVE"

    with pytest.raises(sqlite3.IntegrityError, match="task is not ACTIVE"):
        store.persist_event(
            AgentEvent(
                tenant_id="tenant-a",
                task_id="task-a",
                run_id="run-a",
                event_type=EventType.USER_INPUT,
                content_text="late write",
            )
        )
    with pytest.raises(RuntimeError, match="task is not ACTIVE"):
        store.ensure_task(
            tenant_id="tenant-a",
            task_id="task-a",
            goal="must not reopen",
        )

    assert store.task_status(tenant_id="tenant-a", task_id="task-a") == "DELETING"
    store.close()


def test_retention_policy_round_trip(tmp_path) -> None:
    store = _prepared_store(tmp_path)
    policy = TaskRetentionPolicy(
        tenant_id="tenant-a",
        task_id="task-a",
        retention_until=123.0,
        legal_hold=True,
        set_by="compliance",
    )
    store.set_retention_policy(policy)
    assert store.get_retention_policy(tenant_id="tenant-a", task_id="task-a") == policy
    store.close()


def test_sqlite_v2_memory_identity_migrates_without_losing_rows(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = _prepared_store(tmp_path)
    first = MemoryRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        memory_type=MemoryType.FACT,
        title="first source",
        preview_text="same fact",
        source_event_id="event-one",
    )
    store.put_memory(first)
    store.close()

    database = sqlite3.connect(path)
    current_sql = database.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_memory'"
    ).fetchone()[0]
    old_sql = current_sql.replace(", source_event_id, artifact_id", "")
    assert old_sql != current_sql
    database.execute("DROP INDEX agent_memory_search")
    database.execute("ALTER TABLE agent_memory RENAME TO agent_memory_new")
    database.execute(old_sql)
    database.execute("INSERT INTO agent_memory SELECT * FROM agent_memory_new")
    database.execute("DROP TABLE agent_memory_new")
    database.execute("DELETE FROM memory_schema_version")
    database.execute(
        "INSERT INTO memory_schema_version(version, applied_at) VALUES (2, 0)"
    )
    database.commit()
    database.close()

    migrated = SqliteMemoryStore(path)
    second = MemoryRecord(
        tenant_id="tenant-a",
        task_id="task-a",
        memory_type=MemoryType.FACT,
        title="second source",
        preview_text="same fact",
        source_event_id="event-two",
    )
    migrated.put_memory(second)
    rows = migrated.all_memories(tenant_id="tenant-a", task_id="task-a")

    assert {row.id for row in rows} == {first.id, second.id}
    assert {row.source_event_id for row in rows} == {"event-one", "event-two"}
    versions = {
        row[0]
        for row in migrated._connection.execute(
            "SELECT version FROM memory_schema_version"
        ).fetchall()
    }
    assert 2 in versions and 9 in versions
    migrated.close()
