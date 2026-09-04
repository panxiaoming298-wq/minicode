from __future__ import annotations

import os
import time
import uuid

import pytest

from minicode.paged_memory.archive import EventArchiveManager
from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.lifecycle import LifecycleManager
from minicode.paged_memory.models import (
    DependencyType,
    MemoryMode,
    MemorySearchRequest,
)
from minicode.paged_memory.service import PagedMemoryRuntime

pytestmark = pytest.mark.skipif(
    os.environ.get("MINICODE_MEMORY_EXTERNAL_INTEGRATION") != "1",
    reason=(
        "set MINICODE_MEMORY_EXTERNAL_INTEGRATION=1 to test real "
        "PostgreSQL, MinIO, and Milvus services"
    ),
)


def _external_config() -> PagedMemoryConfig:
    return PagedMemoryConfig(
        enabled=True,
        auto_mode=False,
        default_mode=MemoryMode.PAGED_MEMORY,
        backend="postgres",
        database_url=os.environ.get(
            "MEMORY_DATABASE_URL",
            "postgresql://minicode:minicode-dev-only@localhost:55432/minicode",
        ),
        tenant_id="memory-integration",
        workspace_id="memory-integration",
        user_id="integration-runner",
        artifact_store_type="minio",
        artifact_endpoint=os.environ.get(
            "ARTIFACT_STORE_ENDPOINT", "http://localhost:59000"
        ),
        artifact_bucket=os.environ.get("ARTIFACT_STORE_BUCKET", "minicode-artifacts"),
        artifact_access_key=os.environ.get("ARTIFACT_STORE_ACCESS_KEY", "minioadmin"),
        artifact_secret_key=os.environ.get("ARTIFACT_STORE_SECRET_KEY", "minioadmin"),
        artifact_large_bytes=32,
        local_spool_enabled=False,
        milvus_enabled=True,
        milvus_uri=os.environ.get("MILVUS_URI", "http://localhost:59530"),
        milvus_collection=os.environ.get(
            "MILVUS_COLLECTION", "agent_memory_integration_v2"
        ),
        milvus_dense_dim=int(os.environ.get("MILVUS_DENSE_DIM", "512")),
        embedding_provider=os.environ.get(
            "MEMORY_EMBEDDING_PROVIDER", "fastembed"
        ),
        embedding_model=os.environ.get(
            "MEMORY_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        ),
        embedding_revision=os.environ.get(
            "MEMORY_EMBEDDING_REVISION", "fastembed-0.8.0-builtin-v1"
        ),
        indexer_retry_backoff_seconds=0,
    )


def test_postgres_minio_milvus_checkpoint_round_trip(tmp_path) -> None:
    """Exercise only real external adapters; local doubles are not accepted here."""
    config = _external_config()
    task_id = f"task_it_{uuid.uuid4().hex}"
    marker = f"required-marker-{uuid.uuid4().hex}"
    payload = (f"full external artifact {marker}\n" * 20).encode()
    runtime: PagedMemoryRuntime | None = None
    artifact_id = ""
    checkpoint_id = ""
    try:
        runtime = PagedMemoryRuntime(
            config=config,
            cwd=tmp_path,
            task_id=task_id,
            goal="external memory integration",
        )
        step_id = runtime.begin_step(1, user_input="persist and recover")
        constraint = runtime.record_constraint(
            text=f"The final operation must preserve {marker}",
            facets=("hard-constraint", "external-integration"),
        )
        envelope, _prompt = runtime.capture_tool_result(
            call={"id": "external-call", "toolName": "query"},
            output=payload.decode(),
            ok=True,
        )
        artifact_id = envelope.artifact_id
        runtime.add_dependency(
            step_id=step_id,
            dependency_type=DependencyType.ARTIFACT,
            dependency_id=artifact_id,
        )

        runtime.process_outbox(limit=10_000)
        indexed_memories = []
        # A separately deployed consumer may already hold the second row and
        # Milvus flush latency can exceed a few seconds on a cold container.
        for _ in range(300):
            indexed_memories = runtime.authority.all_memories(
                tenant_id=config.tenant_id, task_id=task_id
            )
            if (
                len(indexed_memories) >= 2
                and all(memory.indexed_at is not None for memory in indexed_memories)
            ):
                break
            time.sleep(0.1)
        assert len(indexed_memories) >= 2
        assert all(memory.indexed_at is not None for memory in indexed_memories)
        vector_hits = runtime.vector_index.search(
            MemorySearchRequest(
                tenant_id=config.tenant_id,
                task_id=task_id,
                query=marker,
            )
        )
        assert constraint.id in {hit.memory_id for hit in vector_hits}
        assert constraint.id in {memory.id for memory in runtime.search(marker)}
        foreign_hits = runtime.vector_index.search(
            MemorySearchRequest(
                tenant_id=config.tenant_id,
                task_id=f"other_{task_id}",
                query=marker,
            )
        )
        assert foreign_hits == []
        cross_tenant_hits = runtime.vector_index.search(
            MemorySearchRequest(
                tenant_id="other-memory-integration",
                task_id=task_id,
                query=marker,
            )
        )
        assert cross_tenant_hits == []
        assert (
            runtime.read_artifact_range(artifact_id, end=len(payload), unit="BYTE")
            == payload
        )
        audit = runtime.preflight(high_risk=True, idempotency_key="external-write")
        assert audit.allowed and audit.coverage_ratio == 1.0

        checkpoint = runtime.finalize_step(success=True)
        checkpoint_id = checkpoint.id
        runtime.close()
        runtime = None
        runtime = PagedMemoryRuntime(
            config=config,
            cwd=tmp_path,
            task_id=task_id,
            goal="external memory integration",
        )
        restored = runtime.restore_latest_checkpoint()
        assert restored is not None and restored.id == checkpoint_id
        archive_manager = EventArchiveManager(
            authority=runtime.authority,
            artifact_store=runtime.artifact_store,
        )
        archive = archive_manager.archive_range(
            tenant_id=config.tenant_id,
            task_id=task_id,
            dry_run=False,
        )
        assert archive["created"] and archive["uri"].startswith("s3://")
        assert archive_manager.verify_archive(
            tenant_id=config.tenant_id,
            task_id=task_id,
            archive_id=archive["archive_id"],
        )["valid"]
        archived_events = archive_manager.read_archive_events(
            tenant_id=config.tenant_id,
            task_id=task_id,
            archive_id=archive["archive_id"],
            limit=10_000,
        )
        assert constraint.source_event_id in {event["id"] for event in archived_events}
        integrity = LifecycleManager(
            authority=runtime.authority,
            artifact_store=runtime.artifact_store,
            vector_index=runtime.vector_index,
            config=config,
        ).reference_integrity_scan(
            tenant_id=config.tenant_id,
            task_id=task_id,
        )
        assert integrity["valid"]
    finally:
        if runtime is not None:
            try:
                LifecycleManager(
                    authority=runtime.authority,
                    artifact_store=runtime.artifact_store,
                    vector_index=runtime.vector_index,
                    config=config,
                ).delete_task(
                    tenant_id=config.tenant_id,
                    task_id=task_id,
                    authorized=True,
                    actor="integration-runner",
                    reason="remove isolated integration fixture",
                )
            finally:
                runtime.close()
