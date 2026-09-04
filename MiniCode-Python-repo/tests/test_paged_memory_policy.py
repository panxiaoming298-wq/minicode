from __future__ import annotations

import threading

import pytest

from minicode.config import MINI_CODE_DIR
from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.models import (
    ContextItem,
    MemoryHit,
    MemoryMode,
    MemoryRecord,
    MemorySearchRequest,
    MemoryType,
    TaskMetrics,
)
from minicode.paged_memory.policy import MemoryModePolicy, TokenBudgetManager
from minicode.paged_memory.retrieval import HybridMemoryRetriever, MinimumCoverSelector
from minicode.paged_memory.vector_index import VectorHit


def test_mode_policy_upgrades_by_priority() -> None:
    config = PagedMemoryConfig(
        working_set_step_threshold=3,
        paged_step_threshold=8,
        working_set_context_ratio=0.30,
        paged_context_ratio=0.60,
    )
    policy = MemoryModePolicy(config)

    small = policy.choose_mode(TaskMetrics(completed_steps=1), MemoryMode.INLINE)
    medium = policy.choose_mode(TaskMetrics(completed_steps=3), MemoryMode.INLINE)
    large = policy.choose_mode(
        TaskMetrics(cross_session_required=True), MemoryMode.INLINE
    )

    assert small.mode is MemoryMode.INLINE
    assert medium.mode is MemoryMode.WORKING_SET
    assert large.mode is MemoryMode.PAGED_MEMORY


def test_mode_downgrade_requires_phase_boundary_hysteresis() -> None:
    config = PagedMemoryConfig(
        downgrade_stable_steps=2,
        working_set_step_threshold=3,
        paged_step_threshold=8,
    )
    policy = MemoryModePolicy(config)
    metrics = TaskMetrics()

    assert (
        policy.choose_mode(
            metrics, MemoryMode.PAGED_MEMORY, task_id="t", phase_boundary=False
        ).mode
        is MemoryMode.PAGED_MEMORY
    )
    assert (
        policy.choose_mode(
            metrics, MemoryMode.PAGED_MEMORY, task_id="t", phase_boundary=True
        ).mode
        is MemoryMode.PAGED_MEMORY
    )
    selection = policy.choose_mode(
        metrics, MemoryMode.PAGED_MEMORY, task_id="t", phase_boundary=True
    )
    assert selection.mode is MemoryMode.INLINE
    assert selection.changed


def test_manual_mode_override_wins() -> None:
    policy = MemoryModePolicy(PagedMemoryConfig())
    selection = policy.choose_mode(
        TaskMetrics(), MemoryMode.INLINE, manual_override=MemoryMode.PAGED_MEMORY
    )
    assert selection.mode is MemoryMode.PAGED_MEMORY
    assert selection.reason == "task manual override"


def test_required_items_are_never_dropped_even_over_budget() -> None:
    budget = TokenBudgetManager(estimate_fn=len)
    required = ContextItem(
        item_id="required",
        item_type="CONSTRAINT",
        text="x" * 20,
        priority=100,
        required=True,
    )
    optional = ContextItem(
        item_id="optional",
        item_type="FACT",
        text="y" * 20,
        priority=10,
    )
    selected, omitted, metadata = budget.pack([optional, required], token_budget=5)

    assert selected == [required]
    assert metadata["over_budget"] is True
    assert {item["item_id"] for item in omitted} == {"optional"}


def test_recent_message_tail_keeps_tool_call_result_pair() -> None:
    budget = TokenBudgetManager(estimate_fn=lambda _: 20)
    messages = [
        {"role": "user", "content": "old"},
        {
            "role": "assistant_tool_call",
            "toolUseId": "call-1",
            "toolName": "read_file",
            "input": {},
        },
        {
            "role": "tool_result",
            "toolUseId": "call-1",
            "toolName": "read_file",
            "content": "result",
        },
    ]
    selected = budget.recent_messages(messages, token_budget=25)
    assert [message["role"] for message in selected] == [
        "assistant_tool_call",
        "tool_result",
    ]


def test_minimum_cover_prefers_one_card_covering_required_facets() -> None:
    budget = TokenBudgetManager(estimate_fn=lambda text: len(text.split()))
    complete = MemoryRecord(
        tenant_id="t",
        task_id="task",
        memory_type=MemoryType.FACT,
        title="complete",
        preview_text="covers both",
        facets=["constraint", "decision"],
    )
    partial = MemoryRecord(
        tenant_id="t",
        task_id="task",
        memory_type=MemoryType.FACT,
        title="partial",
        preview_text="only one",
        facets=["constraint"],
    )
    selected = MinimumCoverSelector(budget).select(
        [MemoryHit(partial, 0.9), MemoryHit(complete, 0.8)],
        required_facets=["constraint", "decision"],
        token_budget=2,
    )
    assert [record.id for record in selected] == [complete.id]


def test_hybrid_retrieval_runs_vector_and_lexical_lanes_concurrently() -> None:
    barrier = threading.Barrier(2)
    record = MemoryRecord(
        id="memory-concurrent",
        tenant_id="tenant",
        task_id="task",
        run_id="run",
        memory_type=MemoryType.FACT,
        title="concurrent",
        preview_text="parallel retrieval",
        phase="BUILD",
        facets=["fact"],
    )

    class Authority:
        def search_memories_lexical(self, _request):
            barrier.wait(timeout=2)
            return [MemoryHit(record=record, score=1.0, lexical_score=1.0)]

        def verify_active_memories(self, **_kwargs):
            return [record]

    class Vector:
        def health_check(self):
            raise AssertionError("hot-path retrieval must not issue a probe first")

        def search(self, _request):
            barrier.wait(timeout=2)
            return [
                VectorHit(
                    memory_id=record.id,
                    score=0.9,
                    content_hash=record.content_hash,
                    status=record.status.value,
                    memory_type=record.memory_type.value,
                    phase=record.phase,
                    run_id=record.run_id,
                    facets=tuple(record.facets),
                )
            ]

    retriever = HybridMemoryRetriever(Authority(), Vector())
    hits = retriever.search(
        MemorySearchRequest(tenant_id="tenant", task_id="task", query="parallel"),
        use_vector=True,
    )

    assert [hit.record.id for hit in hits] == [record.id]
    assert hits[0].reason == "hybrid-rrf-verified"
    assert retriever.last_vector_error == ""


def test_stale_vector_hash_is_rejected_without_losing_lexical_hit() -> None:
    record = MemoryRecord(
        id="memory-current",
        tenant_id="tenant",
        task_id="task",
        memory_type=MemoryType.FACT,
        title="current",
        preview_text="authoritative content",
        facets=["fact"],
    )

    class Authority:
        def search_memories_lexical(self, _request):
            return [MemoryHit(record=record, score=1.0, lexical_score=1.0)]

        def verify_active_memories(self, **_kwargs):
            return [record]

    class StaleVector:
        def search(self, _request):
            return [
                VectorHit(
                    memory_id=record.id,
                    score=1.0,
                    content_hash="sha256:" + "0" * 64,
                    status=record.status.value,
                    memory_type=record.memory_type.value,
                    phase=record.phase,
                    run_id=record.run_id,
                    facets=tuple(record.facets),
                )
            ]

    retriever = HybridMemoryRetriever(Authority(), StaleVector())
    hits = retriever.search(
        MemorySearchRequest(tenant_id="tenant", task_id="task", query="content"),
        use_vector=True,
    )

    assert [hit.record.id for hit in hits] == [record.id]
    assert hits[0].vector_score == 0.0
    assert retriever.last_vector_mismatch_ids == [record.id]
    assert "verification failed" in retriever.last_vector_error


def test_invalid_mode_threshold_configuration_fails_clearly(tmp_path) -> None:
    with pytest.raises(ValueError, match="working < paged"):
        PagedMemoryConfig.from_sources(
            {
                "memoryWorkingSetContextRatio": 0.8,
                "memoryPagedContextRatio": 0.6,
            },
            cwd=tmp_path,
            environ={},
        )


def test_shadow_mode_validates_required_external_authority_config(tmp_path) -> None:
    with pytest.raises(ValueError, match="MEMORY_DATABASE_URL"):
        PagedMemoryConfig.from_sources(
            {
                "memoryShadowWrite": True,
                "memoryBackend": "postgres",
            },
            cwd=tmp_path,
            environ={},
        )


def test_invalid_milvus_consistency_level_fails_clearly(tmp_path) -> None:
    with pytest.raises(ValueError, match="MILVUS_CONSISTENCY_LEVEL"):
        PagedMemoryConfig.from_sources(
            cwd=tmp_path,
            environ={"MILVUS_CONSISTENCY_LEVEL": "linearizable"},
        )


def test_relative_storage_paths_use_coordinator_authority_root(tmp_path) -> None:
    coordinator = tmp_path / "coordinator"
    isolated = tmp_path / "isolated-writer"
    coordinator.mkdir()
    isolated.mkdir()
    config = PagedMemoryConfig.from_sources(
        {"_memoryAuthorityCwd": str(coordinator)},
        cwd=isolated,
        environ={
            "MEMORY_SQLITE_PATH": "memory/state.db",
            "ARTIFACT_STORE_ROOT": "memory/artifacts",
            "MEMORY_LOCAL_SPOOL_ROOT": "memory/spool",
            "MEMORY_BACKUP_ROOT": "memory/backups",
        },
    )

    assert config.sqlite_path == str(coordinator / "memory" / "state.db")
    assert config.artifact_root == str(coordinator / "memory" / "artifacts")
    assert config.local_spool_root == str(coordinator / "memory" / "spool")
    assert config.backup_root == str(coordinator / "memory" / "backups")


def test_default_storage_paths_share_one_user_level_root(tmp_path) -> None:
    config = PagedMemoryConfig.from_sources(cwd=tmp_path, environ={})
    expected = MINI_CODE_DIR / "memory-v2"

    assert config.sqlite_path == str(expected / "state.db")
    assert config.artifact_root == str(expected / "artifacts")
    assert config.local_spool_root == str(expected / "spool")
    assert config.backup_root == str(expected / "backups")
