from __future__ import annotations

import json
import sqlite3

import pytest

from minicode.agent_loop import run_agent_turn
from minicode.context_manager import ContextManager
from minicode.paged_memory.artifacts import (
    ArtifactIntegrityError,
    ArtifactStoreUnavailable,
    FileArtifactStore,
)
from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.lifecycle import LifecycleManager
from minicode.paged_memory.models import (
    AgentEvent,
    ArtifactRecord,
    ArtifactStatus,
    DependencyRecord,
    DependencyType,
    EventType,
    MemoryMode,
    MemoryRecord,
    MemorySearchRequest,
    MemoryType,
    ModeSelection,
    StorageHealth,
    content_hash_bytes,
    new_id,
)
from minicode.paged_memory.service import (
    ArtifactPersistenceRequired,
    AuthorityUnavailableError,
    ContextBudgetExceeded,
    PagedMemoryRuntime,
)
from minicode.paged_memory.tools import create_paged_memory_tools
from minicode.paged_memory.vector_index import InMemoryVectorIndex
from minicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from minicode.types import AgentStep


def _config(tmp_path, **changes) -> PagedMemoryConfig:
    values = {
        "enabled": True,
        "sqlite_path": str(tmp_path / "state.db"),
        "artifact_root": str(tmp_path / "artifacts"),
        "backup_root": str(tmp_path / "backups"),
        "artifact_large_bytes": 32,
        "working_set_step_threshold": 2,
        "paged_step_threshold": 4,
        "retention_default_days": 30,
    }
    values.update(changes)
    return PagedMemoryConfig(**values)


class UnavailableArtifactStore:
    def put_bytes(self, data: bytes, **kwargs):
        raise ArtifactStoreUnavailable("offline")

    def health_check(self) -> bool:
        return False

    def close(self) -> None:
        return None


class PendingSpool:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.modified: dict[str, float] = {}

    def put_bytes(self, data: bytes, **kwargs) -> ArtifactRecord:
        artifact_id = str(kwargs.get("artifact_id") or new_id("artifact"))
        self.data[artifact_id] = data
        self.modified[artifact_id] = 0.0
        return ArtifactRecord(
            id=artifact_id,
            tenant_id=str(kwargs["tenant_id"]),
            task_id=str(kwargs["task_id"]),
            run_id=str(kwargs["run_id"]),
            step_id=str(kwargs["step_id"]),
            artifact_type=str(kwargs["artifact_type"]),
            uri=f"spool:local/{artifact_id}",
            content_hash=content_hash_bytes(data),
            media_type=str(kwargs["media_type"]),
            size_bytes=len(data),
            status=ArtifactStatus.PENDING_UPLOAD,
        )

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        return self.data[artifact.id]

    def read_range(
        self, artifact: ArtifactRecord, *, start: int, end: int, **kwargs
    ) -> bytes:
        return self.get_bytes(artifact)[start:end]

    def delete(self, artifact: ArtifactRecord) -> None:
        self.data.pop(artifact.id, None)
        self.modified.pop(artifact.id, None)

    def list_objects(self):
        return tuple(
            (f"spool:local/{artifact_id}", self.modified[artifact_id])
            for artifact_id in self.data
        )


class RecoveringArtifactStore:
    def __init__(self, root) -> None:
        self.available = False
        self.inner = FileArtifactStore(root)
        self.uploads: list[bytes] = []
        self.upload_records: list[ArtifactRecord] = []

    def put_bytes(self, data: bytes, **kwargs) -> ArtifactRecord:
        if not self.available:
            raise ArtifactStoreUnavailable("offline")
        self.uploads.append(data)
        record = self.inner.put_bytes(data, **kwargs)
        self.upload_records.append(record)
        return record

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        if not self.available:
            raise ArtifactStoreUnavailable("offline")
        return self.inner.get_bytes(artifact)

    def read_range(self, artifact: ArtifactRecord, **kwargs) -> bytes:
        if not self.available:
            raise ArtifactStoreUnavailable("offline")
        return self.inner.read_range(artifact, **kwargs)

    def delete(self, artifact: ArtifactRecord) -> None:
        if not self.available:
            raise ArtifactStoreUnavailable("offline")
        self.inner.delete(artifact)

    def health_check(self) -> bool:
        return self.available

    def close(self) -> None:
        return None


class CountingVectorIndex(InMemoryVectorIndex):
    def __init__(self) -> None:
        super().__init__()
        self.health_calls = 0
        self.search_calls = 0

    def health_check(self) -> bool:
        self.health_calls += 1
        return True

    def search(self, request):
        self.search_calls += 1
        return super().search(request)


class FailingSearchVectorIndex(InMemoryVectorIndex):
    def search(self, request):
        raise RuntimeError("milvus temporarily unavailable")


def test_large_tool_result_has_exact_artifact_and_small_envelope(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1, user_input="run")
    raw = "row\n" * 100
    envelope, prompt = runtime.capture_tool_result(
        call={"id": "call-1", "toolName": "query"}, output=raw, ok=True
    )

    assert envelope.truncated is True
    assert envelope.artifact_id
    assert raw not in prompt
    assert len(prompt) < 4_000
    assert (
        runtime.read_artifact_range(
            envelope.artifact_id, start=0, end=len(raw.encode()), unit="BYTE"
        ).decode()
        == raw
    )
    event = runtime.read_event(envelope.source_event_id)
    assert event.content_hash == content_hash_bytes(raw.encode())
    assert event.artifact_uri
    duplicate, duplicate_prompt = runtime.capture_tool_result(
        call={"id": "call-1", "toolName": "query"}, output=raw, ok=True
    )
    assert duplicate.source_event_id == envelope.source_event_id
    assert duplicate.artifact_id == envelope.artifact_id
    assert duplicate_prompt == prompt
    assert len(runtime.authority.list_artifacts(tenant_id="local", task_id="task")) == 1
    observed: list[dict] = []
    runtime.emit_metrics(observed.append)
    assert observed[0]["tool_result_original_size"] == len(raw.encode())
    assert observed[0]["tool_result_prompt_size"] == len(prompt.encode())
    assert raw not in json.dumps(observed[0], ensure_ascii=False)
    runtime.finalize_step(success=True)
    runtime.close()


def test_large_text_artifact_tail_is_searchable_and_range_verifiable(
    tmp_path,
) -> None:
    vector = InMemoryVectorIndex()
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.PAGED_MEMORY,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="recover exact artifact tail",
        vector_index=vector,
    )
    runtime.begin_step(1)
    marker = "ZXQTAILNEEDLE987"
    output = ("ordinary output row\n" * 1_200) + marker
    envelope, _ = runtime.capture_tool_result(
        call={"id": "long-output", "toolName": "database_query"},
        output=output,
        ok=True,
    )

    memories = runtime.authority.all_memories(tenant_id="local", task_id="task")
    chunks = [
        memory for memory in memories if memory.memory_type == MemoryType.ARTIFACT_CHUNK
    ]
    assert len(chunks) > 1
    assert b"".join(
        runtime.read_artifact_range(
            envelope.artifact_id,
            start=chunk.source_range.start,
            end=chunk.source_range.end,
            unit="BYTE",
        )
        for chunk in chunks
    ) == output.encode("utf-8")

    assert runtime.process_outbox(limit=100)["processed"] == len(memories)
    hits = runtime.search(marker, limit=10)
    matching = [
        memory
        for memory in hits
        if memory.memory_type == MemoryType.ARTIFACT_CHUNK
        and marker in memory.preview_text
    ]
    assert matching
    assert matching[0].source_range.start > 0
    chunk = matching[0]
    registry = ToolRegistry(create_paged_memory_tools(runtime))
    page_result = registry.execute(
        "memory_get",
        {"memory_id": chunk.id},
        ToolContext(cwd=str(tmp_path)),
    )
    assert page_result.ok
    page = json.loads(page_result.output)
    assert page["page"]["coordinate_system"] == "MEMORY_RELATIVE"
    assert page["page"]["start"] == 0
    assert page["page"]["end"] == len(chunk.preview_text.encode("utf-8"))
    assert page["content"] == chunk.preview_text
    assert page["memory"]["source_range"] == chunk.source_range.to_dict()
    assert (
        runtime.read_memory_range(
            chunk.id,
            start=0,
            end=len(chunk.preview_text),
            unit="CHAR",
        ).decode("utf-8")
        == chunk.preview_text
    )
    runtime._verify_dependency_source(
        DependencyRecord(
            tenant_id="local",
            task_id="task",
            step_id=runtime.current_step_id,
            dependency_type=DependencyType.MEMORY,
            dependency_id=chunk.id,
        )
    )
    original_chunk_hash = chunk.content_hash
    runtime.authority._connection.execute(
        "UPDATE agent_memory SET content_hash=? WHERE id=?",
        ("sha256:" + "0" * 64, chunk.id),
    )
    with pytest.raises(ValueError, match="range hash mismatch"):
        runtime._verify_dependency_source(
            DependencyRecord(
                tenant_id="local",
                task_id="task",
                step_id=runtime.current_step_id,
                dependency_type=DependencyType.MEMORY,
                dependency_id=chunk.id,
            )
        )
    runtime.authority._connection.execute(
        "UPDATE agent_memory SET content_hash=? WHERE id=?",
        (original_chunk_hash, chunk.id),
    )
    assert matching[0].id in {
        hit.memory_id
        for hit in vector.search(
            MemorySearchRequest(
                tenant_id="local",
                task_id="task",
                query=marker,
                candidate_limit=10,
            )
        )
    }

    duplicate, _ = runtime.capture_tool_result(
        call={"id": "long-output", "toolName": "database_query"},
        output=output,
        ok=True,
    )
    assert duplicate.source_event_id == envelope.source_event_id
    assert len(
        runtime.authority.all_memories(tenant_id="local", task_id="task")
    ) == len(memories)
    runtime.close()


def test_memory_navigation_receipts_are_audited_without_recursive_projection(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.WORKING_SET,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    runtime.begin_step(1, user_input="resume")

    search_envelope, _ = runtime.capture_tool_result(
        call={"id": "search", "toolName": "memory_search"},
        output='{"count": 1}',
        ok=True,
    )
    failed_envelope, _ = runtime.capture_tool_result(
        call={"id": "get", "toolName": "memory_get"},
        output="range invalid",
        ok=False,
    )

    search_event = runtime.read_event(search_envelope.source_event_id)
    failed_event = runtime.read_event(failed_envelope.source_event_id)
    assert search_event.content_json["searchable_memory"] is False
    assert failed_event.content_json["searchable_memory"] is False
    assert runtime.authority.all_memories(tenant_id="local", task_id="task") == []
    state = runtime.authority.get_state(tenant_id="local", task_id="task")
    assert state is not None
    assert state.open_errors == []

    web_envelope, _ = runtime.capture_tool_result(
        call={"id": "web", "toolName": "web_search"},
        output="Cristiano Ronaldo latest news",
        ok=True,
    )
    memories = runtime.authority.all_memories(tenant_id="local", task_id="task")
    assert len(memories) == 1
    assert memories[0].facets == ["tool_result", "web_search", "success"]
    runtime.record_initial_messages(
        [
            {
                "role": "tool_result",
                "toolName": "checkpoint_restore",
                "toolUseId": "historic-restore",
                "content": "historic navigation receipt",
            }
        ]
    )
    _, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "summarize the news"}]
    )
    recent_event_ids = {item.source_event_id for item in packet.recent_messages}
    assert search_envelope.source_event_id not in recent_event_ids
    assert failed_envelope.source_event_id not in recent_event_ids
    assert web_envelope.source_event_id in recent_event_ids
    assert all(
        item.text != "historic navigation receipt" for item in packet.recent_messages
    )
    runtime.close()


def test_search_and_working_set_ignore_legacy_memory_navigation_receipts(
    tmp_path,
) -> None:
    vector = InMemoryVectorIndex()
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.PAGED_MEMORY,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        vector_index=vector,
    )
    runtime.begin_step(1)
    polluted = MemoryRecord(
        tenant_id="local",
        task_id="task",
        memory_type=MemoryType.TOOL_RESULT,
        title="memory_search result",
        preview_text="Cristiano Ronaldo latest news",
        facets=["tool_result", "memory_search", "success"],
    )
    source = MemoryRecord(
        tenant_id="local",
        task_id="task",
        memory_type=MemoryType.TOOL_RESULT,
        title="web_search result",
        preview_text="Verified Cristiano Ronaldo club news",
        facets=["tool_result", "web_search", "success"],
    )
    runtime.authority.put_memory(polluted)
    runtime.authority.put_memory(source)
    vector.upsert([polluted, source])

    assert [record.id for record in runtime.search("Ronaldo", limit=10)] == [source.id]
    state = runtime.authority.get_state(tenant_id="local", task_id="task")
    assert state is not None
    runtime.working_set.touch(
        tenant_id="local",
        task_id="task",
        phase=state.phase,
        memory_ids=[polluted.id, source.id],
        state_version=state.version,
    )
    assert {
        record.id
        for record in runtime.working_set.get(
            tenant_id="local", task_id="task", phase=state.phase
        )
    } == {source.id}
    runtime.close()


def test_memory_get_clamps_oversized_range_and_returns_pagination(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    runtime.capture_tool_result(
        call={"id": "source", "toolName": "web_search"},
        output="x" * 6_782,
        ok=True,
    )
    memory = runtime.authority.all_memories(tenant_id="local", task_id="task")[0]
    registry = ToolRegistry(create_paged_memory_tools(runtime))

    first = registry.execute(
        "memory_get",
        {
            "memory_id": memory.id,
            "start": 0,
            "end": 6_782,
            "unit": "BYTE",
        },
        ToolContext(cwd=str(tmp_path)),
    )
    assert first.ok
    first_page = json.loads(first.output)
    assert runtime.token_budget.estimate_text(first_page["content"]) <= (
        runtime.config.raw_page_token_limit
    )
    assert first_page["page"]["content_truncated_to_page_budget"] is False
    assert first_page["page"]["end"] == len(first_page["content"].encode())
    assert first_page["page"]["has_more"] is True
    assert first_page["page"]["next_start"] == first_page["page"]["end"]
    assert first_page["page"]["request_clamped"] is True
    assert first_page["page"]["requested_end"] == 6_782
    assert first_page["page"]["start"] == 0
    assert first_page["page"]["unit"] == "BYTE"

    second = registry.execute(
        "memory_get",
        {
            "memory_id": memory.id,
            "start": first_page["page"]["next_start"],
            "end": 6_782,
            "unit": "BYTE",
        },
        ToolContext(cwd=str(tmp_path)),
    )
    assert second.ok
    second_page = json.loads(second.output)
    assert first_page["content"] + second_page["content"] == "x" * 6_782
    assert second_page["page"]["has_more"] is False
    assert second_page["page"]["next_start"] is None
    assert len(second_page["content"]) == 6_782 - len(first_page["content"])
    runtime.close()


def test_large_tool_result_retry_after_authority_failure_reuses_object(
    tmp_path, monkeypatch
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    original_persist = runtime.authority.persist_event
    attempts = 0

    def fail_first_persist(event, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("authority temporarily unavailable")
        return original_persist(event, **kwargs)

    monkeypatch.setattr(runtime.authority, "persist_event", fail_first_persist)
    call = {"id": "call", "toolName": "query"}
    raw = "authority-retry\n" * 20

    with pytest.raises(AuthorityUnavailableError):
        runtime.capture_tool_result(call=call, output=raw, ok=True)
    envelope, _ = runtime.capture_tool_result(call=call, output=raw, ok=True)

    assert runtime.metrics.large_artifact_count == 1
    assert len(tuple(runtime.artifact_store.list_objects())) == 1
    assert (
        runtime.read_artifact_range(
            envelope.artifact_id, end=len(raw.encode())
        ).decode()
        == raw
    )
    runtime.close()


def test_artifact_chunk_projection_failure_rolls_back_event_bundle(
    tmp_path, monkeypatch
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    original_put_memory = runtime.authority._put_memory

    def fail_chunk_projection(database, memory):
        if memory.memory_type == MemoryType.ARTIFACT_CHUNK:
            raise sqlite3.OperationalError("injected chunk projection failure")
        return original_put_memory(database, memory)

    monkeypatch.setattr(runtime.authority, "_put_memory", fail_chunk_projection)
    call = {"id": "atomic-chunks", "toolName": "query"}
    output = "searchable artifact payload\n" * 100

    with pytest.raises(AuthorityUnavailableError):
        runtime.capture_tool_result(call=call, output=output, ok=True)

    assert runtime.authority.all_memories(tenant_id="local", task_id="task") == []
    assert runtime.authority.list_artifacts(tenant_id="local", task_id="task") == []
    assert len(tuple(runtime.artifact_store.list_objects())) == 1

    monkeypatch.setattr(runtime.authority, "_put_memory", original_put_memory)
    envelope, _ = runtime.capture_tool_result(call=call, output=output, ok=True)
    memories = runtime.authority.all_memories(tenant_id="local", task_id="task")
    assert envelope.artifact_id
    assert any(memory.memory_type == MemoryType.ARTIFACT_CHUNK for memory in memories)
    assert len(tuple(runtime.artifact_store.list_objects())) == 1
    runtime.close()


def test_losing_idempotency_race_does_not_delete_committed_artifact(
    tmp_path, monkeypatch
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    call = {"id": "call", "toolName": "query"}
    raw = "same-result\n" * 20
    first, _ = runtime.capture_tool_result(call=call, output=raw, ok=True)
    monkeypatch.setattr(runtime, "_idempotent_event", lambda _key: None)

    raced, _ = runtime.capture_tool_result(call=call, output=raw, ok=True)

    assert raced.source_event_id == first.source_event_id
    assert raced.artifact_id == first.artifact_id
    assert len(tuple(runtime.artifact_store.list_objects())) == 1
    assert (
        runtime.read_artifact_range(first.artifact_id, end=len(raw.encode()))
        == raw.encode()
    )
    runtime.close()


def test_artifact_outage_without_safe_spool_fails_closed(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path, local_spool_enabled=False),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=UnavailableArtifactStore(),
    )
    runtime.begin_step(1)
    with pytest.raises(ArtifactPersistenceRequired, match="paused"):
        runtime.capture_tool_result(
            call={"id": "call", "toolName": "query"},
            output="x" * 100,
            ok=True,
        )
    assert runtime.storage_health is StorageHealth.ARTIFACT_DEGRADED
    runtime.close()


def test_artifact_outage_uses_pending_spool_and_blocks_dependency(tmp_path) -> None:
    spool = PendingSpool()
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=UnavailableArtifactStore(),
        spool_store=spool,
    )
    step_id = runtime.begin_step(1)
    envelope, _ = runtime.capture_tool_result(
        call={"id": "call", "toolName": "query"}, output="x" * 100, ok=True
    )
    artifact = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=envelope.artifact_id
    )
    assert artifact is not None and artifact.status is ArtifactStatus.PENDING_UPLOAD
    assert artifact.metadata["sequence_no"] == 1
    assert artifact.metadata["correlation_id"] == "call"
    assert artifact.metadata["idempotency_key"].endswith(
        f":tool-result:call:{envelope.content_hash}"
    )
    runtime.add_dependency(
        step_id=step_id,
        dependency_type=DependencyType.ARTIFACT,
        dependency_id=envelope.artifact_id,
    )
    audit = runtime.preflight(high_risk=True, idempotency_key="write-1")
    assert audit.allowed is False
    assert envelope.artifact_id in audit.unresolved_source_ids
    runtime.close()


def test_pending_spool_replays_by_hash_before_health_recovers(tmp_path) -> None:
    spool = PendingSpool()
    primary = RecoveringArtifactStore(tmp_path / "primary")
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=primary,
        spool_store=spool,
    )
    runtime.begin_step(1)
    raw = "replay-me\n" * 20
    envelope, _ = runtime.capture_tool_result(
        call={"id": "call", "toolName": "query"}, output=raw, ok=True
    )
    assert runtime.storage_health is StorageHealth.ARTIFACT_DEGRADED
    assert not runtime.preflight(
        high_risk=True, idempotency_key="before-recovery"
    ).allowed

    primary.available = True
    replay = runtime.replay_pending_artifacts()

    assert replay == {"uploaded": 1, "failed": 0, "skipped": 0}
    assert runtime.storage_health is StorageHealth.NORMAL
    artifact = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=envelope.artifact_id
    )
    assert artifact is not None and artifact.status is ArtifactStatus.ACTIVE
    assert runtime.read_artifact_range(
        envelope.artifact_id, end=len(raw.encode("utf-8"))
    ) == raw.encode("utf-8")
    assert runtime.preflight(high_risk=True, idempotency_key="after-recovery").allowed
    runtime.close()


def test_pending_spool_replays_in_stable_task_sequence(tmp_path) -> None:
    spool = PendingSpool()
    primary = RecoveringArtifactStore(tmp_path / "primary")
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=primary,
        spool_store=spool,
    )
    runtime.begin_step(1)
    first = b"first-result-" * 10
    runtime.capture_tool_result(
        call={"id": "call-1", "toolName": "query"},
        output=first.decode(),
        ok=True,
    )
    runtime.begin_step(2)
    second = b"second-result-" * 10
    runtime.capture_tool_result(
        call={"id": "call-2", "toolName": "query"},
        output=second.decode(),
        ok=True,
    )

    primary.available = True
    report = runtime.replay_pending_artifacts()

    assert report == {"uploaded": 2, "failed": 0, "skipped": 0}
    assert primary.uploads == [first, second]
    runtime.close()


def test_pending_spool_retry_reuses_same_primary_object(tmp_path, monkeypatch) -> None:
    spool = PendingSpool()
    primary = RecoveringArtifactStore(tmp_path / "primary")
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=primary,
        spool_store=spool,
    )
    runtime.begin_step(1)
    envelope, _ = runtime.capture_tool_result(
        call={"id": "call", "toolName": "query"},
        output="retry-safe-result\n" * 20,
        ok=True,
    )
    original_update = runtime.authority.update_artifact_status
    attempts = 0

    def fail_first_status_update(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("status commit unavailable")
        return original_update(**kwargs)

    monkeypatch.setattr(
        runtime.authority, "update_artifact_status", fail_first_status_update
    )
    primary.available = True

    first = runtime.replay_pending_artifacts()
    second = runtime.replay_pending_artifacts()

    assert first == {"uploaded": 0, "failed": 1, "skipped": 0}
    assert second == {"uploaded": 1, "failed": 0, "skipped": 0}
    assert [record.id for record in primary.upload_records] == [
        envelope.artifact_id,
        envelope.artifact_id,
    ]
    assert len({record.uri for record in primary.upload_records}) == 1
    assert len(tuple(primary.inner.list_objects())) == 1
    artifact = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=envelope.artifact_id
    )
    assert artifact is not None and artifact.status is ArtifactStatus.ACTIVE
    runtime.close()


def test_pending_spool_treats_post_commit_timeout_as_success(
    tmp_path, monkeypatch
) -> None:
    spool = PendingSpool()
    primary = RecoveringArtifactStore(tmp_path / "primary")
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=primary,
        spool_store=spool,
    )
    runtime.begin_step(1)
    envelope, _ = runtime.capture_tool_result(
        call={"id": "call", "toolName": "query"},
        output="commit-was-successful\n" * 20,
        ok=True,
    )
    original_update = runtime.authority.update_artifact_status

    def commit_then_timeout(**kwargs):
        original_update(**kwargs)
        raise sqlite3.OperationalError("response lost after commit")

    monkeypatch.setattr(
        runtime.authority, "update_artifact_status", commit_then_timeout
    )
    primary.available = True

    report = runtime.replay_pending_artifacts()

    assert report == {"uploaded": 1, "failed": 0, "skipped": 0}
    assert envelope.artifact_id not in spool.data
    artifact = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=envelope.artifact_id
    )
    assert artifact is not None and artifact.status is ArtifactStatus.ACTIVE
    assert len(tuple(primary.inner.list_objects())) == 1
    runtime.close()


def test_replay_cleans_spool_residue_left_after_registry_commit(tmp_path) -> None:
    spool = PendingSpool()
    primary = RecoveringArtifactStore(tmp_path / "primary")
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=primary,
        spool_store=spool,
    )
    runtime.begin_step(1)
    envelope, _ = runtime.capture_tool_result(
        call={"id": "call", "toolName": "query"},
        output="crash-window\n" * 20,
        ok=True,
    )
    pending = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=envelope.artifact_id
    )
    assert pending is not None
    primary.available = True
    promoted = primary.put_bytes(
        spool.get_bytes(pending),
        artifact_id=pending.id,
        tenant_id=pending.tenant_id,
        task_id=pending.task_id,
        run_id=pending.run_id,
        step_id=pending.step_id,
        media_type=pending.media_type,
        artifact_type=pending.artifact_type,
    )
    runtime.authority.update_artifact_status(
        tenant_id="local",
        task_id="task",
        artifact_id=pending.id,
        status=ArtifactStatus.ACTIVE.value,
        uri=promoted.uri,
    )

    report = runtime.replay_pending_artifacts()

    assert report == {"uploaded": 0, "failed": 0, "skipped": 1}
    assert envelope.artifact_id not in spool.data
    assert len(tuple(primary.inner.list_objects())) == 1
    runtime.close()


def test_real_delete_removes_primary_and_promoted_spool_copy(tmp_path) -> None:
    spool = PendingSpool()
    primary = RecoveringArtifactStore(tmp_path / "primary")
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        artifact_store=primary,
        spool_store=spool,
    )
    runtime.begin_step(1)
    envelope, _ = runtime.capture_tool_result(
        call={"id": "call", "toolName": "query"},
        output="delete-all-copies\n" * 20,
        ok=True,
    )
    pending = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=envelope.artifact_id
    )
    assert pending is not None
    primary.available = True
    promoted = primary.put_bytes(
        spool.get_bytes(pending),
        artifact_id=pending.id,
        tenant_id=pending.tenant_id,
        task_id=pending.task_id,
        run_id=pending.run_id,
        step_id=pending.step_id,
        media_type=pending.media_type,
        artifact_type=pending.artifact_type,
    )
    runtime.authority.update_artifact_status(
        tenant_id="local",
        task_id="task",
        artifact_id=pending.id,
        status=ArtifactStatus.ACTIVE.value,
        uri=promoted.uri,
    )

    report = LifecycleManager(
        authority=runtime.authority,
        artifact_store=primary,
        spool_store=spool,
        config=runtime.config,
    ).delete_task(
        tenant_id="local",
        task_id="task",
        authorized=True,
        actor="admin",
        reason="verified request",
    )

    assert report["artifact_objects"] == 2
    assert not spool.data
    assert tuple(primary.inner.list_objects()) == ()
    assert runtime.authority.get_state(tenant_id="local", task_id="task") is None
    runtime.close()


def test_inline_mode_never_calls_vector_index(tmp_path) -> None:
    vector = CountingVectorIndex()
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.INLINE,
            milvus_enabled=True,
            milvus_uri="http://unused.invalid:19530",
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        vector_index=vector,
    )
    runtime.begin_step(1)
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "short task"},
    ]

    prepared, packet = runtime.prepare_model_messages(messages)

    assert packet.memory_mode is MemoryMode.INLINE
    assert prepared == messages
    assert vector.search_calls == 0
    assert vector.health_calls == 0
    runtime.close()


def test_paged_mode_reports_milvus_failure_and_uses_authority_fallback(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.PAGED_MEMORY,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        vector_index=FailingSearchVectorIndex(),
    )
    runtime.begin_step(1)
    source = AgentEvent(
        tenant_id="local",
        task_id="task",
        run_id=runtime.run_id,
        event_type=EventType.USER_INPUT,
        content_text="the selected database is PostgreSQL",
        idempotency_key="fallback-source",
    )
    runtime.authority.persist_event(source)
    fact = MemoryRecord(
        tenant_id="local",
        task_id="task",
        memory_type=MemoryType.FACT,
        title="database fact",
        preview_text="the selected database is PostgreSQL",
        source_event_id=source.id,
        content_hash=source.content_hash,
    )
    runtime.authority.put_memory(fact)

    _messages, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "which PostgreSQL database was selected?"}]
    )

    assert [item.item_id for item in packet.supplemental_memories] == [fact.id]
    assert packet.debug_metadata["supplemental_retrieval_degraded"] is True
    assert "RuntimeError" in packet.debug_metadata["supplemental_retrieval_error"]
    assert runtime.storage_health is StorageHealth.INDEX_DEGRADED
    runtime.close()


def test_authority_outage_blocks_preflight_and_new_writes(
    tmp_path, monkeypatch
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    monkeypatch.setattr(runtime.authority, "health_check", lambda: False)

    audit = runtime.preflight(high_risk=True, idempotency_key="write")

    assert not audit.allowed
    assert "authoritative store unavailable" in audit.errors
    assert runtime.storage_health is StorageHealth.AUTHORITY_UNAVAILABLE

    def fail_write(*_args, **_kwargs):
        raise sqlite3.OperationalError("database offline")

    monkeypatch.setattr(runtime.authority, "persist_event", fail_write)
    with pytest.raises(AuthorityUnavailableError, match="state advancement is blocked"):
        runtime.record_constraint(text="must remain durable")
    runtime.close()


def test_required_dependency_coverage_and_source_pointer(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    step_id = runtime.begin_step(1)
    runtime.add_dependency(
        step_id=step_id,
        dependency_type=DependencyType.EVENT,
        dependency_id="missing-event",
    )
    _packet_messages, packet = runtime.prepare_model_messages(
        [{"role": "system", "content": "contract"}, {"role": "user", "content": "go"}]
    )
    assert packet.debug_metadata["required_dependency_coverage_ratio"] == 0.0
    assert packet.debug_metadata["missing_dependency_ids"] == ["missing-event"]
    audit = runtime.preflight(high_risk=True, idempotency_key="write")
    assert not audit.allowed
    assert audit.coverage_ratio == 0.0
    runtime.close()


def test_active_constraint_and_decision_are_implicit_required_dependencies(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    constraint = runtime.record_constraint(text="Never publish without approval")
    decision = runtime.record_decision(
        key="database", text="Use PostgreSQL", structured_value={"major": 16}
    )

    _messages, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "perform the next high-risk write"}]
    )
    audit = runtime.preflight(high_risk=True, idempotency_key="write-implicit")

    required_ids = {item.item_id for item in packet.mandatory_dependencies}
    assert required_ids == {constraint.id, decision.id}
    assert packet.debug_metadata["required_dependency_count"] == 2
    assert packet.debug_metadata["required_dependency_coverage_ratio"] == 1.0
    assert audit.allowed and audit.required_count == audit.resolved_count == 2
    assert "Never publish without approval" not in packet.task_state[0].text
    assert "Use PostgreSQL" not in packet.task_state[0].text
    runtime.close()


def test_corrupt_implicit_constraint_source_blocks_high_risk_preflight(
    tmp_path, monkeypatch
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    constraint = runtime.record_constraint(text="Approval is mandatory")
    original_get_event = runtime.authority.get_event

    def corrupt_source(**kwargs):
        event = original_get_event(**kwargs)
        if event is not None and event.id == constraint.source_event_id:
            event.content_hash = "sha256:" + "0" * 64
        return event

    monkeypatch.setattr(runtime.authority, "get_event", corrupt_source)

    audit = runtime.preflight(high_risk=True, idempotency_key="write-corrupt")

    assert not audit.allowed
    assert constraint.id in audit.unresolved_source_ids
    runtime.close()


def test_required_event_with_bad_hash_is_unresolved_and_blocks(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    step_id = runtime.begin_step(1)
    corrupt = AgentEvent(
        tenant_id="local",
        task_id="task",
        run_id=runtime.run_id,
        step_id=step_id,
        event_type=EventType.USER_INPUT,
        content_text="authoritative value",
        content_hash="sha256:" + "0" * 64,
    )
    runtime.authority.persist_event(corrupt)
    runtime.add_dependency(
        step_id=step_id,
        dependency_type=DependencyType.EVENT,
        dependency_id=corrupt.id,
    )

    _messages, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "use the required value"}]
    )
    audit = runtime.preflight(high_risk=True, idempotency_key="write")

    assert packet.debug_metadata["required_dependency_coverage_ratio"] == 0.0
    assert packet.debug_metadata["unresolved_source_ids"] == [corrupt.id]
    assert not audit.allowed and corrupt.id in audit.unresolved_source_ids
    runtime.close()


def test_completed_step_and_verified_checkpoint_are_valid_required_sources(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    step_id = runtime.begin_step(1)
    predecessor_id = f"{runtime.run_id}.predecessor"
    runtime.authority.upsert_step(
        tenant_id="local",
        task_id="task",
        run_id=runtime.run_id,
        step_id=predecessor_id,
        sequence_no=0,
        status="COMPLETED",
    )
    checkpoint = runtime.create_checkpoint(reason="dependency-test")
    runtime.add_dependency(
        step_id=step_id,
        dependency_type=DependencyType.STEP,
        dependency_id=predecessor_id,
    )
    runtime.add_dependency(
        step_id=step_id,
        dependency_type=DependencyType.CHECKPOINT,
        dependency_id=checkpoint.id,
    )

    _messages, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "continue"}]
    )
    audit = runtime.preflight(high_risk=True, idempotency_key="write")

    assert packet.debug_metadata["required_dependency_count"] == 2
    assert packet.debug_metadata["required_dependency_coverage_ratio"] == 1.0
    assert packet.debug_metadata["unresolved_source_ids"] == []
    assert audit.allowed and audit.resolved_count == 2
    runtime.close()


def test_superseded_decision_card_is_removed_from_index_and_retrieval(
    tmp_path,
) -> None:
    vector = InMemoryVectorIndex()
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        vector_index=vector,
    )
    runtime.begin_step(1)
    first = runtime.record_decision(key="database", text="Use SQLite")
    assert runtime.process_outbox()["processed"] == 1
    assert [record.id for record in vector.get([first.id])] == [first.id]

    second = runtime.record_decision(key="database", text="Use PostgreSQL")
    result = runtime.process_outbox()
    active = runtime.authority.active_decisions(
        tenant_id="local", task_id="task", decision_key="database"
    )
    searchable = runtime.search("database PostgreSQL", limit=10)
    state = runtime.authority.get_state(tenant_id="local", task_id="task")
    _messages, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "continue with the selected database"}]
    )

    assert result["processed"] == 2
    assert [decision.id for decision in active] == [second.id]
    assert state is not None
    assert state.active_decisions["database"]["decision_id"] == second.id
    assert [item.item_id for item in packet.mandatory_dependencies] == [second.id]
    assert vector.get([first.id]) == []
    assert [record.id for record in vector.get([second.id])] == [second.id]
    assert first.id not in {record.id for record in searchable}
    runtime.close()


def test_repeated_decision_reuses_committed_event_and_projection(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    state_before = runtime.authority.get_state(tenant_id="local", task_id="task")
    assert state_before is not None
    first = runtime.record_decision(
        key="database", text="Use PostgreSQL", structured_value={"major": 16}
    )
    state_after_first = runtime.authority.get_state(tenant_id="local", task_id="task")
    repeated = runtime.record_decision(
        key="database", text="Use PostgreSQL", structured_value={"major": 16}
    )

    decisions = runtime.authority.decision_history(
        tenant_id="local", task_id="task", decision_key="database"
    )
    memories = runtime.authority.all_memories(tenant_id="local", task_id="task")
    assert repeated.id == first.id
    assert state_after_first is not None
    assert state_after_first.version == state_before.version + 1
    assert state_after_first.active_decisions["database"] == {
        "decision_id": first.id,
        "source_event_id": first.source_event_id,
        "status": "ACTIVE",
    }
    assert (
        runtime.authority.get_state(tenant_id="local", task_id="task").version
        == state_after_first.version
    )
    assert len(decisions) == 1
    assert [memory.id for memory in memories] == [first.id]
    assert memories[0].source_event_id == first.source_event_id
    runtime.close()


def test_failed_tool_does_not_complete_step(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    step_id = runtime.begin_step(1)
    runtime.capture_tool_result(
        call={"id": "call", "toolName": "test"}, output="failed", ok=False
    )
    runtime.finalize_step(success=False)
    state = runtime.authority.get_state(tenant_id="local", task_id="task")
    assert state is not None
    assert step_id not in state.completed_steps
    assert state.open_errors
    assert (
        runtime.authority.get_step(tenant_id="local", task_id="task", step_id=step_id)[
            "status"
        ]
        == "FAILED"
    )
    runtime.close()


def test_checkpoint_can_restore_mode_and_current_step(tmp_path) -> None:
    config = _config(tmp_path, default_mode=MemoryMode.PAGED_MEMORY)
    first = PagedMemoryRuntime(
        config=config, cwd=tmp_path, task_id="task", goal="goal", run_id="run-one"
    )
    step_id = first.begin_step(1)
    checkpoint = first.finalize_step(success=True)
    committed_state = first.authority.get_state(tenant_id="local", task_id="task")
    assert committed_state is not None
    assert checkpoint.snapshot["task_state"] == committed_state.to_dict()
    assert (
        first.authority.get_step(tenant_id="local", task_id="task", step_id=step_id)[
            "status"
        ]
        == "COMPLETED"
    )
    first.close()

    second = PagedMemoryRuntime(
        config=config, cwd=tmp_path, task_id="task", goal="goal", run_id="run-two"
    )
    restored = second.restore_latest_checkpoint()
    assert restored is not None and restored.id == checkpoint.id
    assert second.current_step_id == step_id
    assert second.current_mode is MemoryMode.PAGED_MEMORY
    second.close()


def test_concurrent_state_change_is_merged_into_step_finalization(
    tmp_path, monkeypatch
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    step_id = runtime.begin_step(1)
    original_persist_event = runtime.authority.persist_event

    def persist_after_interleaved_state_change(event, **kwargs):
        current = runtime.authority.get_state(tenant_id="local", task_id="task")
        assert current is not None
        runtime.authority.compare_and_set_state(
            current.apply_patch({"phase": "CONCURRENT"}),
            expected_version=current.version,
        )
        return original_persist_event(event, **kwargs)

    monkeypatch.setattr(
        runtime.authority,
        "persist_event",
        persist_after_interleaved_state_change,
    )

    checkpoint = runtime.finalize_step(success=True)

    step = runtime.authority.get_step(
        tenant_id="local", task_id="task", step_id=step_id
    )
    state = runtime.authority.get_state(tenant_id="local", task_id="task")
    assert step is not None and step["status"] == "COMPLETED"
    assert state is not None and state.phase == "CONCURRENT"
    assert state.completed_steps == [step_id]
    assert checkpoint.snapshot["task_state"] == state.to_dict()
    assert checkpoint.state_version == state.version
    assert runtime.storage_health is StorageHealth.NORMAL
    runtime.close()


def test_checkpoint_tool_restores_requested_checkpoint_not_only_latest(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path, default_mode=MemoryMode.INLINE),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    first_step = runtime.begin_step(1)
    first = runtime.finalize_step(success=True)
    runtime.current_mode = MemoryMode.PAGED_MEMORY
    runtime.begin_step(2)
    latest = runtime.finalize_step(success=True)
    assert first.id != latest.id

    registry = ToolRegistry(create_paged_memory_tools(runtime))
    result = registry.execute(
        "checkpoint_restore",
        {"checkpoint_id": first.id},
        ToolContext(cwd=str(tmp_path)),
    )

    assert result.ok
    assert first.id in result.output
    assert runtime.current_step_id == first_step
    assert runtime.current_mode is MemoryMode.INLINE
    runtime.close()


def test_artifact_tool_rejects_cross_task_source(tmp_path) -> None:
    config = _config(tmp_path, artifact_large_bytes=4)
    owner = PagedMemoryRuntime(
        config=config,
        cwd=tmp_path,
        task_id="owner",
        goal="owner goal",
    )
    owner.begin_step(1)
    envelope, _prompt = owner.capture_tool_result(
        call={"id": "private", "toolName": "seed"},
        output="private-data",
        ok=True,
    )
    reader = PagedMemoryRuntime(
        config=config,
        cwd=tmp_path,
        task_id="reader",
        goal="reader goal",
    )
    reader.begin_step(1)
    registry = ToolRegistry(create_paged_memory_tools(reader))

    result = registry.execute(
        "artifact_read",
        {"artifact_id": envelope.artifact_id, "start": 0, "end": 12},
        ToolContext(cwd=str(tmp_path)),
    )

    assert not result.ok
    assert "task scope" in result.output
    owner.close()
    reader.close()


class CapturingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.received: list[list[dict]] = []

    def next(self, messages, on_stream_chunk=None, store=None):
        self.received.append(messages)
        self.calls += 1
        if self.calls == 1:
            return AgentStep(
                type="tool_calls",
                calls=[{"id": "call-1", "toolName": "echo", "input": {}}],
            )
        return AgentStep(type="assistant", content="done", kind="final")


class FinalOnlyModel:
    def next(self, messages, on_stream_chunk=None, store=None):
        return AgentStep(type="assistant", content="done", kind="final")


class ToolCatalogModel:
    def __init__(self, tools: ToolRegistry, *, artifact_id: str = "") -> None:
        self.tools = tools
        self.artifact_id = artifact_id
        self.tool_catalogs: list[list[str]] = []
        self.received: list[list[dict]] = []

    def next(self, messages, on_stream_chunk=None, store=None):
        self.tool_catalogs.append(self.tools.list_all())
        self.received.append(messages)
        if self.artifact_id and len(self.tool_catalogs) == 1:
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "artifact-read-call",
                        "toolName": "artifact_read",
                        "input": {
                            "artifact_id": self.artifact_id,
                            "start": 2,
                            "end": 8,
                            "unit": "BYTE",
                        },
                    }
                ],
            )
        return AgentStep(type="assistant", content="done", kind="final")


def test_inline_agent_loop_does_not_expose_paging_tools(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.INLINE,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    registry = ToolRegistry([])
    model = ToolCatalogModel(registry)

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "user", "content": "short"}],
        cwd=str(tmp_path),
        runtime={},
        enable_work_chain=False,
        paged_memory_runtime=runtime,
    )

    assert messages[-1]["content"] == "done"
    assert model.tool_catalogs == [[]]
    assert model.tools is registry
    runtime.close()


def test_enabled_inline_path_bypasses_legacy_lossy_compaction(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.INLINE,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    registry = ToolRegistry([])
    model = ToolCatalogModel(registry)
    context_manager = ContextManager(context_window=32)
    compaction_calls = 0

    def unexpected_compaction():
        nonlocal compaction_calls
        compaction_calls += 1
        return [{"role": "user", "content": "lossy"}]

    context_manager.compact_messages = unexpected_compaction
    original = "exact-history-" * 100

    run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "user", "content": original}],
        cwd=str(tmp_path),
        runtime={},
        context_manager=context_manager,
        enable_work_chain=False,
        paged_memory_runtime=runtime,
    )

    assert compaction_calls == 0
    assert model.received[0][0]["content"] == original
    runtime.close()


def test_agent_loop_removes_paging_tools_after_inline_downgrade(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )

    class PagedThenInlinePolicy:
        def __init__(self) -> None:
            self.calls = 0

        def choose_mode(self, _metrics, current_mode, **_kwargs):
            self.calls += 1
            target = MemoryMode.PAGED_MEMORY if self.calls == 1 else MemoryMode.INLINE
            return ModeSelection(
                mode=target,
                reason="test transition",
                changed=target != current_mode,
            )

    class TransitionModel:
        def __init__(self, tools) -> None:
            self.tools = tools
            self.catalogs: list[list[str]] = []

        def next(self, messages, on_stream_chunk=None, store=None):
            self.catalogs.append(self.tools.list_all())
            if len(self.catalogs) == 1:
                return AgentStep(
                    type="tool_calls",
                    calls=[{"id": "echo-1", "toolName": "echo", "input": {}}],
                )
            return AgentStep(type="assistant", content="done", kind="final")

    policy = PagedThenInlinePolicy()
    runtime.mode_policy = policy
    runtime.context_builder.mode_policy = policy
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda _value, _context: ToolResult(ok=True, output="ok"),
            )
        ]
    )
    model = TransitionModel(registry)

    run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "user", "content": "transition"}],
        cwd=str(tmp_path),
        runtime={},
        enable_work_chain=False,
        paged_memory_runtime=runtime,
    )

    paging_names = {
        "memory_search",
        "memory_get",
        "artifact_read",
        "checkpoint_restore",
    }
    assert paging_names.issubset(model.catalogs[0])
    assert model.catalogs[1] == ["echo"]
    assert model.tools is registry
    runtime.close()


def test_paged_agent_loop_exposes_tools_and_reads_exact_artifact_range(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.PAGED_MEMORY,
            artifact_large_bytes=1_000,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    runtime.begin_step(0)
    envelope, _prompt = runtime.capture_tool_result(
        call={"id": "seed", "toolName": "seed"},
        output="0123456789" * 200,
        ok=True,
    )
    runtime.finalize_step(success=True)
    registry = ToolRegistry([])
    model = ToolCatalogModel(registry, artifact_id=envelope.artifact_id)

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "user", "content": "read exact source"}],
        cwd=str(tmp_path),
        runtime={},
        enable_work_chain=False,
        paged_memory_runtime=runtime,
    )

    expected_tools = {
        "memory_search",
        "memory_get",
        "artifact_read",
        "checkpoint_restore",
    }
    assert expected_tools.issubset(model.tool_catalogs[0])
    tool_result = next(
        message for message in model.received[1] if message.get("role") == "tool_result"
    )
    assert '"content": "234567"' in tool_result["content"]
    assert any(
        "For omitted detail, use memory_search" in message.get("content", "")
        for message in model.received[0]
    )
    assert model.tools is registry
    assert messages[-1]["content"] == "done"
    runtime.close()


def test_agent_loop_uses_envelope_but_preserves_raw_artifact(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path, artifact_large_bytes=8),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    model = CapturingModel()
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="large echo",
                input_schema={"type": "object"},
                validator=lambda value: value,
                # Exceeds ToolRegistry's legacy 50K prompt truncation threshold.
                run=lambda _value, _context: ToolResult(ok=True, output="z" * 60_000),
            )
        ]
    )
    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        cwd=str(tmp_path),
        runtime={},
        enable_work_chain=False,
        paged_memory_runtime=runtime,
    )
    assert messages[-1]["content"] == "done"
    second_input = model.received[1]
    tool_messages = [
        message for message in second_input if message.get("role") == "tool_result"
    ]
    assert tool_messages and "Tool result envelope" in tool_messages[0]["content"]
    assert len(tool_messages[0]["content"]) < 4_000
    artifacts = runtime.authority.list_artifacts(tenant_id="local", task_id="task")
    assert len(artifacts) == 1
    assert runtime.artifact_store.get_bytes(artifacts[0]) == b"z" * 60_000
    runtime.close()


def test_shadow_failure_disables_only_shadow_path(tmp_path, monkeypatch) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path, enabled=False, shadow_write=True),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )

    def fail_shadow(*_args, **_kwargs):
        raise AuthorityUnavailableError("shadow authority offline")

    monkeypatch.setattr(runtime, "record_initial_messages", fail_shadow)
    messages = run_agent_turn(
        model=FinalOnlyModel(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "go"}],
        cwd=str(tmp_path),
        runtime={},
        enable_work_chain=False,
        paged_memory_runtime=runtime,
    )
    assert messages[-1]["content"] == "done"
    runtime.close()


def test_enabled_finalization_failure_cannot_return_false_success(
    tmp_path, monkeypatch
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )

    def fail_checkpoint(*_args, **_kwargs):
        raise AuthorityUnavailableError("checkpoint offline")

    monkeypatch.setattr(runtime, "finalize_step", fail_checkpoint)
    with pytest.raises(AuthorityUnavailableError, match="checkpoint offline"):
        run_agent_turn(
            model=FinalOnlyModel(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "go"}],
            cwd=str(tmp_path),
            runtime={},
            enable_work_chain=False,
            paged_memory_runtime=runtime,
        )
    runtime.close()


def test_memory_page_cursor_never_skips_page_units(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path, raw_page_token_limit=100),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    runtime.record_initial_messages([{"role": "user", "content": "alpha\fbeta\fgamma"}])
    memory = runtime.authority.all_memories(tenant_id="local", task_id="task")[0]
    registry = ToolRegistry(create_paged_memory_tools(runtime))

    first = registry.execute(
        "memory_get",
        {"memory_id": memory.id, "start": 0, "end": 2, "unit": "PAGE"},
        ToolContext(cwd=str(tmp_path)),
    )
    assert first.ok
    first_page = json.loads(first.output)
    assert first_page["content"] == "alpha\fbeta"
    assert first_page["page"]["next_start"] == 2

    second = registry.execute(
        "memory_get",
        {
            "memory_id": memory.id,
            "start": first_page["page"]["next_start"],
            "end": 3,
            "unit": "PAGE",
        },
        ToolContext(cwd=str(tmp_path)),
    )
    assert second.ok
    assert json.loads(second.output)["content"] == "gamma"
    runtime.close()


def test_cross_session_raw_messages_are_rendered_and_searchable(
    tmp_path, monkeypatch
) -> None:
    for name in (
        "MEMORY_V2_ENABLED",
        "MEMORY_SQLITE_PATH",
        "MEMORY_MODE_DEFAULT",
        "MEMORY_MODE_AUTO",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = {
        "memoryV2": True,
        "memoryModeAuto": False,
        "memoryModeDefault": "WORKING_SET",
        "memorySqlitePath": str(tmp_path / "shared.db"),
        "artifactStoreRoot": str(tmp_path / "artifacts"),
        "memoryBackupRoot": str(tmp_path / "backups"),
        "memoryLocalSpoolEnabled": False,
    }
    secret = "AUTH-CODE-ZEBRA-7319"
    first = PagedMemoryRuntime.from_runtime(
        runtime=settings,
        cwd=tmp_path,
        messages=[{"role": "user", "content": secret}],
    )
    assert first is not None
    task_id = first.task_id
    completed_step = first.begin_step(1, user_input=secret)
    first.finalize_step(success=True)
    first.close()

    second = PagedMemoryRuntime.from_runtime(
        runtime=settings,
        cwd=tmp_path,
        messages=[{"role": "user", "content": "What was the auth code?"}],
    )
    assert second is not None and second.task_id == task_id
    assert second.current_step_id == completed_step
    assert second.metrics.completed_steps == 1
    prepared, _packet = second.prepare_model_messages(
        [{"role": "user", "content": "What was the auth code?"}]
    )
    assert any(secret in str(message.get("content", "")) for message in prepared)
    assert second.search("ZEBRA-7319")
    second.close()


def test_hard_input_budget_fails_before_required_source_is_dropped(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.INLINE,
            output_token_reserve=100,
            tool_token_reserve=100,
            safety_token_reserve=100,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        model_context_limit=1_024,
    )
    with pytest.raises(ContextBudgetExceeded) as raised:
        runtime.prepare_model_messages(
            [{"role": "user", "content": "required-exact-input-" * 1_000}]
        )
    assert raised.value.estimated_tokens > raised.value.input_limit
    runtime.close()


def test_hard_input_budget_counts_dynamic_tool_schemas(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.INLINE,
            output_token_reserve=100,
            tool_token_reserve=10,
            safety_token_reserve=100,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        model_context_limit=1_024,
    )
    prepared, _packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "small request"}]
    )

    with pytest.raises(ContextBudgetExceeded) as raised:
        runtime.enforce_provider_budget(
            prepared,
            tool_definitions=[
                {
                    "name": "oversized_schema",
                    # Repeated single characters compress into few BPE tokens
                    # under some tokenizer versions, so use actual word tokens.
                    "description": "oversized schema field " * 2_000,
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )

    assert raised.value.estimated_tokens > raised.value.input_limit
    runtime.close()


def test_artifact_read_errors_preserve_or_correct_registry_status(tmp_path) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path), cwd=tmp_path, task_id="task", goal="goal"
    )
    runtime.begin_step(1)
    envelope, _ = runtime.capture_tool_result(
        call={"id": "artifact", "toolName": "query"},
        output="durable-content" * 10,
        ok=True,
    )
    artifact = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=envelope.artifact_id
    )
    assert artifact is not None and artifact.status is ArtifactStatus.ACTIVE

    with pytest.raises(ValueError):
        runtime.read_artifact_range(artifact.id, start=5, end=4)
    unchanged = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=artifact.id
    )
    assert unchanged is not None and unchanged.status is ArtifactStatus.ACTIVE

    artifact_path = runtime.artifact_store.root / artifact.uri.removeprefix("file:")
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        runtime.read_artifact_range(artifact.id, start=0, end=4)
    corrupt = runtime.authority.get_artifact(
        tenant_id="local", task_id="task", artifact_id=artifact.id
    )
    assert corrupt is not None and corrupt.status is ArtifactStatus.CORRUPT
    with pytest.raises(ArtifactIntegrityError, match="not readable"):
        runtime.read_artifact_range(artifact.id, start=0, end=1)
    runtime.close()


def test_index_outbox_is_written_while_configured_vector_index_is_offline(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(tmp_path, milvus_enabled=True),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
        vector_index=InMemoryVectorIndex(),
    )
    runtime.begin_step(1)
    runtime.vector_index = None
    memory = runtime.record_constraint(text="retain this while Milvus is offline")
    pending = runtime.authority.pending_outbox(limit=10)
    assert [record.aggregate_id for record in pending] == [memory.id]
    runtime.close()


def test_parallel_workers_share_run_without_step_id_collisions(tmp_path) -> None:
    config = _config(tmp_path)
    first = PagedMemoryRuntime(
        config=config,
        cwd=tmp_path,
        task_id="shared-task",
        goal="goal",
        run_id="shared-run",
        step_namespace="explorer-a",
    )
    second = PagedMemoryRuntime(
        config=config,
        cwd=tmp_path,
        task_id="shared-task",
        goal="goal",
        run_id="shared-run",
        step_namespace="explorer-b",
    )

    first_step = first.begin_step(1)
    second_step = second.begin_step(1)
    assert first_step != second_step
    first.finalize_step(success=True)
    second.finalize_step(success=True)

    state = second.authority.get_state(tenant_id="local", task_id="shared-task")
    assert state is not None
    assert set(state.completed_steps) == {first_step, second_step}
    first.close()
    second.close()


def test_oversized_prior_event_becomes_source_ref_not_hidden_truncation(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.WORKING_SET,
            recent_event_token_budget=120,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    original = "large-prior-source-" * 5_000 + "TAIL-SENTINEL"
    runtime.record_initial_messages([{"role": "user", "content": original}])
    memory = runtime.authority.all_memories(tenant_id="local", task_id="task")[0]

    prepared, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "continue from the prior source"}]
    )
    rendered = "\n".join(str(message.get("content", "")) for message in prepared)
    assert original not in rendered
    assert "USER_INPUT_SOURCE_REF" in rendered
    assert memory.id in rendered
    assert packet.recent_messages[0].source_event_id == memory.source_event_id
    assert (
        runtime.page_memory(
            memory.id,
            start=len(original.encode()) - len("TAIL-SENTINEL"),
            end=len(original.encode()),
            unit="BYTE",
        )
        == "TAIL-SENTINEL"
    )
    runtime.close()


def test_oversized_required_event_stays_required_as_pageable_source_ref(
    tmp_path,
) -> None:
    runtime = PagedMemoryRuntime(
        config=_config(
            tmp_path,
            auto_mode=False,
            default_mode=MemoryMode.PAGED_MEMORY,
            raw_page_token_limit=80,
            recent_event_token_budget=80,
        ),
        cwd=tmp_path,
        task_id="task",
        goal="goal",
    )
    original = "required-source-" * 2_000 + "REQUIRED-TAIL"
    runtime.record_initial_messages([{"role": "user", "content": original}])
    memory = runtime.authority.all_memories(tenant_id="local", task_id="task")[0]
    step_id = runtime.begin_step(1)
    runtime.add_dependency(
        step_id=step_id,
        dependency_type=DependencyType.EVENT,
        dependency_id=memory.source_event_id,
    )

    prepared, packet = runtime.prepare_model_messages(
        [{"role": "user", "content": "use the required source"}],
        step_id=step_id,
    )
    rendered = "\n".join(str(message.get("content", "")) for message in prepared)
    assert original not in rendered
    assert "USER_INPUT_SOURCE_REF" in rendered
    assert memory.source_event_id in rendered
    assert packet.debug_metadata["paged_required_source_count"] == 1

    registry = ToolRegistry(create_paged_memory_tools(runtime))
    start = len(original.encode()) - len("REQUIRED-TAIL")
    receipt = registry.execute(
        "memory_get",
        {
            "source_event_id": memory.source_event_id,
            "start": start,
            "end": len(original.encode()),
            "unit": "BYTE",
        },
        ToolContext(cwd=str(tmp_path)),
    )
    assert receipt.ok and json.loads(receipt.output)["content"] == "REQUIRED-TAIL"
    runtime.close()
