from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, ClassVar

import pytest

from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.models import MemoryRecord, MemorySearchRequest, MemoryType
from minicode.paged_memory.vector_index import (
    FastEmbedEmbeddingProvider,
    HashEmbeddingProvider,
    InMemoryVectorIndex,
    MilvusVectorIndex,
    redact_secrets,
)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "sk-abcdefghijklmnopqrstuvwx",
        "pk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwx",
        "github_pat_abcdefghijklmnopqrstuvwx",
        "AKIAABCDEFGHIJKLMNOP",
        "ASIAABCDEFGHIJKLMNOP",
    ],
)
def test_redact_secrets_covers_provider_key_formats(secret: str) -> None:
    redacted, count = redact_secrets(f"credential={secret}")

    assert secret not in redacted
    assert "[REDACTED]" in redacted
    assert count >= 1


class _FakeDataType:
    VARCHAR = "VARCHAR"
    INT64 = "INT64"
    FLOAT = "FLOAT"
    FLOAT_VECTOR = "FLOAT_VECTOR"


class _FakeSchema:
    def __init__(self, **kwargs: Any) -> None:
        self.fields: list[dict[str, Any]] = []
        self.description = str(kwargs.get("description", ""))

    def add_field(self, *, field_name: str, datatype: Any, **kwargs: Any) -> None:
        params = {key: kwargs[key] for key in ("max_length", "dim") if key in kwargs}
        self.fields.append(
            {
                "name": field_name,
                "type": datatype,
                "params": params,
                "is_primary": bool(kwargs.get("is_primary", False)),
            }
        )


class _FakeIndexParams:
    def __init__(self) -> None:
        self.indexes: list[dict[str, Any]] = []

    def add_index(self, **kwargs: Any) -> None:
        self.indexes.append(dict(kwargs))


class _FakeQueryIterator:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.emitted = False
        self.closed = False

    def next(self) -> list[dict[str, Any]]:
        if self.emitted:
            return []
        self.emitted = True
        return list(self.rows)

    def close(self) -> None:
        self.closed = True


class _FakeMilvusClient:
    instances: ClassVar[list[_FakeMilvusClient]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.connect_kwargs = kwargs
        self.exists = False
        self.fields: list[dict[str, Any]] = []
        self.description = ""
        self.index: dict[str, Any] = {}
        self.loaded: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []
        self.flushes: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.get_rows: list[dict[str, Any]] = []
        self.search_result: list[list[dict[str, Any]]] = []
        self.searches: list[dict[str, Any]] = []
        self.query_rows: list[dict[str, Any]] = []
        self.query_iterators: list[_FakeQueryIterator] = []
        self.drop_count = 0
        self.closed = False
        self.__class__.instances.append(self)

    @staticmethod
    def create_schema(**kwargs: Any) -> _FakeSchema:
        return _FakeSchema(**kwargs)

    @staticmethod
    def prepare_index_params() -> _FakeIndexParams:
        return _FakeIndexParams()

    def has_collection(self, _name: str, **_kwargs: Any) -> bool:
        return self.exists

    def create_collection(
        self,
        *,
        schema: _FakeSchema,
        index_params: _FakeIndexParams,
        **_kwargs: Any,
    ) -> None:
        self.exists = True
        self.fields = list(schema.fields)
        self.description = schema.description
        self.index = dict(index_params.indexes[0])

    def describe_collection(self, _name: str, **_kwargs: Any) -> dict[str, Any]:
        return {"fields": self.fields, "description": self.description}

    def list_indexes(self, _name: str, **_kwargs: Any) -> list[str]:
        return [str(self.index.get("field_name", ""))] if self.index else []

    def describe_index(
        self, _name: str, _index_name: str, **_kwargs: Any
    ) -> dict[str, Any]:
        return dict(self.index)

    def load_collection(self, **kwargs: Any) -> None:
        self.loaded.append(dict(kwargs))

    def upsert(self, **kwargs: Any) -> dict[str, Any]:
        self.upserts.append(dict(kwargs))
        return {"upsert_count": len(kwargs["data"])}

    def flush(self, **kwargs: Any) -> None:
        self.flushes.append(dict(kwargs))

    def delete(self, **kwargs: Any) -> dict[str, int]:
        self.deletes.append(dict(kwargs))
        return {"delete_count": len(kwargs.get("ids", []))}

    def get(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.get_rows)

    def search(self, **kwargs: Any) -> list[list[dict[str, Any]]]:
        self.searches.append(dict(kwargs))
        return self.search_result

    def query_iterator(self, **_kwargs: Any) -> _FakeQueryIterator:
        iterator = _FakeQueryIterator(self.query_rows)
        self.query_iterators.append(iterator)
        return iterator

    def drop_collection(self, _name: str, **_kwargs: Any) -> None:
        self.drop_count += 1
        self.exists = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_pymilvus(monkeypatch: pytest.MonkeyPatch) -> type[_FakeMilvusClient]:
    module = ModuleType("pymilvus")
    module.DataType = _FakeDataType
    module.MilvusClient = _FakeMilvusClient
    _FakeMilvusClient.instances = []
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    return _FakeMilvusClient


def _config() -> PagedMemoryConfig:
    return PagedMemoryConfig(
        milvus_enabled=True,
        milvus_uri="http://milvus.invalid:19530",
        milvus_collection="memory_test",
        milvus_dense_dim=4,
        milvus_timeout_seconds=7,
    )


def _record(**kwargs: Any) -> MemoryRecord:
    values: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "task_id": "task-a",
        "run_id": "run-a",
        "memory_type": MemoryType.FACT,
        "title": "Fact",
        "preview_text": "remember this fact",
        "facets": ["fact", "verified"],
    }
    values.update(kwargs)
    return MemoryRecord(**values)


def test_milvus_client_adapter_creates_validated_shared_collection_without_release(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]

    assert client.connect_kwargs == {
        "uri": "http://milvus.invalid:19530",
        "timeout": 7,
        "db_name": "default",
    }
    assert client.exists
    assert client.loaded[-1]["collection_name"] == "memory_test"
    assert client.index["metric_type"] == "COSINE"

    index.close()

    # Closing a task connection must not release the server-side shared collection.
    assert client.closed
    assert not hasattr(client, "release_collection")


def test_milvus_client_adapter_uses_modern_vector_api_and_utf8_byte_limits(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]
    record = _record(
        title="password=top-secret " + "界" * 500,
        preview_text="界" * 4_000,
    )

    assert index.upsert([record]) == 1

    call = client.upserts[-1]
    row = call["data"][0]
    assert call["collection_name"] == "memory_test"
    assert "top-secret" not in row["title"]
    assert len(row["title"].encode("utf-8")) <= 1024
    assert len(row["preview_text"].encode("utf-8")) <= 8192
    assert row["facets"] == "fact\nverified"
    assert client.flushes == []


def test_milvus_client_search_uses_hard_tenant_task_scope_and_soft_run_phase(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]
    client.search_result = [
        [
            {
                "id": "memory-1",
                "distance": 0.75,
                "entity": {
                    "memory_id": "memory-1",
                    "content_hash": "sha256:one",
                    "status": "ACTIVE",
                    "memory_type": "FACT",
                    "facets": "fact",
                    "phase": "BUILD",
                    "run_id": "run-a",
                },
            },
            {
                "id": "memory-hidden",
                "distance": 0.5,
                "entity": {
                    "memory_id": "memory-hidden",
                    "facets": "memory_search",
                },
            },
        ]
    ]

    hits = index.search(
        MemorySearchRequest(
            tenant_id="tenant-a",
            task_id="task-a",
            run_id="run-a",
            phase="BUILD",
            query="fact",
        )
    )

    assert [(hit.memory_id, hit.score) for hit in hits] == [
        ("memory-1", pytest.approx(0.85))
    ]
    call = client.searches[-1]
    assert call["collection_name"] == "memory_test"
    assert 'tenant_id == "tenant-a"' in call["filter"]
    assert 'task_id == "task-a"' in call["filter"]
    assert "run_id" not in call["filter"]
    assert "phase" not in call["filter"]
    assert call["output_fields"] == [
        "memory_id",
        "content_hash",
        "status",
        "memory_type",
        "facets",
        "phase",
        "run_id",
    ]
    assert call["consistency_level"] == "Strong"
    assert "expr" not in call and "param" not in call
    assert hits[0].content_hash == "sha256:one"
    assert hits[0].status == "ACTIVE"
    assert hits[0].memory_type == "FACT"


def test_milvus_status_update_is_partial_and_only_targets_existing_ids(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]
    client.get_rows = [{"memory_id": "present"}]

    assert index.mark_status(["present", "missing"], "RESOLVED") == 1

    call = client.upserts[-1]
    assert call["data"] == [{"memory_id": "present", "status": "RESOLVED"}]
    assert call["partial_update"] is True


def test_milvus_schema_mismatch_fails_before_serving_queries(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]
    dense = next(field for field in client.fields if field["name"] == "dense_vector")
    dense["params"]["dim"] = 8

    with pytest.raises(RuntimeError, match="dimension is 8, expected 4"):
        index._validate_collection()


def test_milvus_descriptor_mismatch_requires_a_new_collection(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]
    client.description = "Rebuildable MiniCode memory cards; index_id=stale"

    with pytest.raises(RuntimeError, match="index descriptor"):
        index._validate_collection()


def test_milvus_list_ids_uses_iterator_and_closes_it(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]
    client.query_rows = [{"memory_id": "one"}, {"memory_id": "two"}]

    assert index.list_ids(
        tenant_id="tenant-a",
        task_id="task-a",
        statuses=("ACTIVE", "OPEN"),
    ) == {"one", "two"}
    assert client.query_iterators[-1].closed


def test_milvus_list_entries_returns_hashes_for_same_id_reconciliation(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]
    client.query_rows = [
        {"memory_id": "one", "content_hash": "sha256:one"},
        {"memory_id": "two", "content_hash": "sha256:two"},
    ]

    assert index.list_entries(
        tenant_id="tenant-a",
        task_id="task-a",
        statuses=("ACTIVE", "OPEN"),
    ) == {"one": "sha256:one", "two": "sha256:two"}
    assert client.query_iterators[-1].closed


def test_scoped_milvus_rebuild_never_drops_the_shared_collection(
    fake_pymilvus: type[_FakeMilvusClient],
) -> None:
    index = MilvusVectorIndex(_config(), HashEmbeddingProvider(4))
    client = fake_pymilvus.instances[-1]

    assert (
        index.rebuild(
            [_record(id="replacement")],
            tenant_id="tenant-a",
            task_id="task-a",
        )
        == 1
    )

    assert client.drop_count == 0
    assert client.deletes[-1]["filter"] == (
        'tenant_id == "tenant-a" and task_id == "task-a"'
    )
    assert "ids" not in client.deletes[-1]
    assert len(client.flushes) == 1
    assert client.flushes[-1]["timeout"] == 7.0


def test_scoped_in_memory_rebuild_preserves_other_tasks() -> None:
    index = InMemoryVectorIndex(HashEmbeddingProvider(4))
    old = _record(id="old")
    other = _record(id="other", task_id="task-b")
    replacement = _record(id="replacement")
    index.upsert([old, other])

    assert index.rebuild([replacement], tenant_id="tenant-a", task_id="task-a") == 1

    assert index.get([old.id]) == []
    assert [record.id for record in index.get([other.id])] == [other.id]
    assert [record.id for record in index.get([replacement.id])] == [replacement.id]


def test_in_memory_index_soft_boosts_run_scope_and_rejects_bad_embeddings() -> None:
    index = InMemoryVectorIndex(HashEmbeddingProvider(4))
    global_record = _record(id="global", run_id="")
    matching_record = _record(id="matching", run_id="run-a")
    foreign_record = _record(id="foreign", run_id="run-b")
    index.upsert([global_record, matching_record, foreign_record])

    hits = index.search(
        MemorySearchRequest(
            tenant_id="tenant-a",
            task_id="task-a",
            run_id="run-a",
            query="remember fact",
        )
    )

    assert {hit.memory_id for hit in hits} == {"global", "matching", "foreign"}
    assert hits[0].memory_id == "matching"

    class BadProvider:
        dimension = 4

        @staticmethod
        def embed(_texts: list[str]) -> list[list[float]]:
            return []

    with pytest.raises(ValueError, match="0 vectors for 1 texts"):
        InMemoryVectorIndex(BadProvider()).upsert([global_record])


def test_fastembed_provider_uses_distinct_passage_and_query_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    class FakeTextEmbedding:
        calls: ClassVar[list[tuple[str, list[str], int]]] = []

        @staticmethod
        def get_embedding_size(_model_name: str) -> int:
            return 4

        def __init__(self, **_kwargs: Any) -> None:
            pass

        def passage_embed(self, texts, *, batch_size: int):
            material = list(texts)
            self.calls.append(("passage", material, batch_size))
            return [[1.0, 0.0, 0.0, 0.0] for _ in material]

        def query_embed(self, texts, *, batch_size: int):
            material = list(texts)
            self.calls.append(("query", material, batch_size))
            return [[0.0, 1.0, 0.0, 0.0] for _ in material]

    module = ModuleType("fastembed")
    module.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    provider = FastEmbedEmbeddingProvider(
        model_name="fake-zh",
        dimension=4,
        revision="test-revision",
        cache_dir=str(tmp_path / "models"),
        batch_size=7,
    )

    assert provider.embed_documents(["document"]) == [[1.0, 0.0, 0.0, 0.0]]
    assert provider.embed_queries(["query"]) == [[0.0, 1.0, 0.0, 0.0]]
    assert FakeTextEmbedding.calls == [
        ("passage", ["document"], 7),
        ("query", ["query"], 7),
    ]
