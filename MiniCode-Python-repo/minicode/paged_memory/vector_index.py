"""Rebuildable vector index adapters and secret-safe embeddings."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from .config import PagedMemoryConfig
from .lexical import scope_boost
from .models import MemoryRecord, MemorySearchRequest

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|passwd|secret|cookie)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    re.compile(r"\b(?:sk|pk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
)


def redact_secrets(text: str) -> tuple[str, int]:
    result = text
    count = 0
    for pattern in _SECRET_PATTERNS:
        result, replacements = pattern.subn(
            lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]",
            result,
        )
        count += replacements
    return result, count


class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


_PREPROCESSING_VERSION = "memory-card-v2"


@dataclass(frozen=True)
class VectorIndexDescriptor:
    """Immutable identity for one compatible set of vectors."""

    collection: str
    provider: str
    model: str
    revision: str
    dimension: int
    metric: str
    preprocessing_version: str = _PREPROCESSING_VERSION

    @property
    def index_id(self) -> str:
        material = json.dumps(
            self.to_dict(include_id=False),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "vidx_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "collection": self.collection,
            "provider": self.provider,
            "model": self.model,
            "revision": self.revision,
            "dimension": self.dimension,
            "metric": self.metric,
            "preprocessing_version": self.preprocessing_version,
        }
        if include_id:
            result["index_id"] = self.index_id
        return result


def _provider_identity(provider: EmbeddingProvider) -> tuple[str, str, str]:
    return (
        str(getattr(provider, "provider_name", provider.__class__.__name__)),
        str(getattr(provider, "model_name", provider.__class__.__name__)),
        str(getattr(provider, "revision", "unspecified")),
    )


def configured_index_descriptor(config: PagedMemoryConfig) -> VectorIndexDescriptor:
    return VectorIndexDescriptor(
        collection=config.milvus_collection,
        provider=config.embedding_provider,
        model=config.embedding_model,
        revision=config.embedding_revision,
        dimension=config.milvus_dense_dim,
        metric=config.milvus_metric_type,
    )


class HashEmbeddingProvider:
    """Deterministic local embedding used for tests and offline fallback.

    It is a feature-hashing lexical vector, not a semantic model. Production
    deployments can inject an embedding provider matching the configured
    Milvus dimension without changing the index or context code.
    """

    def __init__(self, dimension: int = 384) -> None:
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self._dimension = dimension
        self.provider_name = "hash"
        self.model_name = "feature-hash"
        self.revision = "v1"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)

    def _one(self, text: str) -> list[float]:
        values = [0.0] * self._dimension
        tokens = re.findall(r"[A-Za-z0-9_./:-]+|[\u3400-\u9fff]", text.casefold())
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            slot = int.from_bytes(digest[:8], "big") % self._dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            values[slot] += sign
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values


class FastEmbedEmbeddingProvider:
    """Local ONNX semantic embeddings with distinct query/passage encoding."""

    provider_name = "fastembed"

    def __init__(
        self,
        *,
        model_name: str,
        dimension: int,
        revision: str,
        cache_dir: str,
        threads: int = 0,
        batch_size: int = 64,
    ) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "FastEmbed embeddings require the optional 'memory' dependencies: "
                "pip install 'minicode-py[memory]'"
            ) from exc
        actual_dimension = int(TextEmbedding.get_embedding_size(model_name))
        if actual_dimension != dimension:
            raise ValueError(
                f"embedding model {model_name!r} has dimension {actual_dimension}; "
                f"MILVUS_DENSE_DIM={dimension}"
            )
        self.model_name = model_name
        self.revision = revision
        self._dimension = dimension
        self._batch_size = max(1, int(batch_size))
        cache = Path(cache_dir).expanduser()
        cache.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "model_name": model_name,
            "cache_dir": str(cache),
            "lazy_load": True,
        }
        if threads > 0:
            kwargs["threads"] = threads
        self._model = TextEmbedding(**kwargs)

    @property
    def dimension(self) -> int:
        return self._dimension

    @staticmethod
    def _materialize(vectors: Iterable[Any]) -> list[list[float]]:
        result: list[list[float]] = []
        for vector in vectors:
            values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
            result.append([float(value) for value in values])
        return result

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._materialize(
            self._model.passage_embed(texts, batch_size=self._batch_size)
        )

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._materialize(
            self._model.query_embed(texts, batch_size=self._batch_size)
        )


def _checked_embeddings(
    provider: EmbeddingProvider,
    texts: Sequence[str],
    *,
    purpose: str = "document",
) -> list[list[float]]:
    """Reject malformed provider output instead of silently dropping records."""

    method_name = "embed_queries" if purpose == "query" else "embed_documents"
    method = getattr(provider, method_name, provider.embed)
    vectors = list(method(texts))
    if len(vectors) != len(texts):
        raise ValueError(
            f"embedding provider returned {len(vectors)} vectors for {len(texts)} texts"
        )
    checked: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if len(vector) != provider.dimension:
            raise ValueError(
                f"embedding {index} has dimension {len(vector)}; "
                f"expected {provider.dimension}"
            )
        material = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in material):
            raise ValueError(f"embedding {index} contains a non-finite value")
        checked.append(material)
    return checked


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """Truncate rebuildable display text without splitting a UTF-8 code point."""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class VectorHit:
    memory_id: str
    score: float
    content_hash: str
    status: str
    memory_type: str
    phase: str = ""
    run_id: str = ""
    facets: tuple[str, ...] = ()


class InMemoryVectorIndex:
    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.embedding_provider = embedding_provider or HashEmbeddingProvider()
        provider, model, revision = _provider_identity(self.embedding_provider)
        self.index_descriptor = VectorIndexDescriptor(
            collection="in-memory",
            provider=provider,
            model=model,
            revision=revision,
            dimension=self.embedding_provider.dimension,
            metric="COSINE",
        )
        self._records: dict[str, tuple[MemoryRecord, list[float]]] = {}

    def upsert(self, records: Sequence[MemoryRecord]) -> int:
        safe_texts = [
            redact_secrets(f"{record.title}\n{record.preview_text}")[0]
            for record in records
        ]
        vectors = _checked_embeddings(
            self.embedding_provider, safe_texts, purpose="document"
        )
        for record, vector in zip(records, vectors):
            self._records[record.id] = (record, vector)
        return len(records)

    def delete(self, memory_ids: Sequence[str]) -> int:
        removed = 0
        for memory_id in memory_ids:
            if self._records.pop(memory_id, None) is not None:
                removed += 1
        return removed

    def mark_status(self, memory_ids: Sequence[str], status: str) -> int:
        changed = 0
        for memory_id in memory_ids:
            pair = self._records.get(memory_id)
            if pair is None:
                continue
            pair[0].status = type(pair[0].status)(status)
            changed += 1
        return changed

    def search(self, request: MemorySearchRequest) -> list[VectorHit]:
        if request.candidate_limit <= 0 or not request.statuses:
            return []
        query = redact_secrets(request.query)[0]
        vector = _checked_embeddings(self.embedding_provider, [query], purpose="query")[
            0
        ]
        results: list[VectorHit] = []
        excluded_facets = set(request.excluded_facets)
        for memory_id, (record, candidate) in self._records.items():
            if (
                record.tenant_id != request.tenant_id
                or record.task_id != request.task_id
            ):
                continue
            if record.status.value not in request.statuses:
                continue
            if (
                request.memory_types
                and record.memory_type.value not in request.memory_types
            ):
                continue
            if excluded_facets.intersection(record.facets):
                continue
            score = sum(left * right for left, right in zip(vector, candidate))
            score += scope_boost(
                record_phase=record.phase,
                record_run_id=record.run_id,
                phase=request.phase,
                run_id=request.run_id,
            )
            results.append(
                VectorHit(
                    memory_id=memory_id,
                    score=score,
                    content_hash=record.content_hash,
                    status=record.status.value,
                    memory_type=record.memory_type.value,
                    phase=record.phase,
                    run_id=record.run_id,
                    facets=tuple(record.facets),
                )
            )
        results.sort(key=lambda hit: hit.score, reverse=True)
        return results[: request.candidate_limit]

    def get(self, memory_ids: Sequence[str]) -> list[MemoryRecord]:
        return [
            self._records[memory_id][0]
            for memory_id in memory_ids
            if memory_id in self._records
        ]

    def list_ids(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> set[str]:
        return set(
            self.list_entries(
                tenant_id=tenant_id,
                task_id=task_id,
                statuses=statuses,
            )
        )

    def list_entries(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> dict[str, str]:
        """Return the indexed identity and source digest for reconciliation."""

        allowed = set(statuses)
        return {
            memory_id: record.content_hash
            for memory_id, (record, _vector) in self._records.items()
            if record.tenant_id == tenant_id
            and record.task_id == task_id
            and (not allowed or record.status.value in allowed)
        }

    def health_check(self) -> bool:
        return True

    def rebuild(
        self,
        records: Iterable[MemoryRecord],
        *,
        tenant_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        material = list(records)
        if task_id and not tenant_id:
            raise ValueError("task-scoped vector rebuild requires tenant_id")
        if tenant_id:
            if any(
                record.tenant_id != tenant_id
                or (task_id is not None and record.task_id != task_id)
                for record in material
            ):
                raise ValueError("vector rebuild records escape the requested scope")
            stale_ids = [
                memory_id
                for memory_id, (record, _vector) in self._records.items()
                if record.tenant_id == tenant_id
                and (task_id is None or record.task_id == task_id)
            ]
            self.delete(stale_ids)
        else:
            self._records.clear()
        return self.upsert(material)

    def close(self) -> None:
        self._records.clear()


class MilvusVectorIndex:
    """MilvusClient adapter with a shared, scalar-filtered collection."""

    _OUTPUT_FIELDS: ClassVar[tuple[str, ...]] = (
        "memory_id",
        "tenant_id",
        "workspace_id",
        "user_id",
        "task_id",
        "run_id",
        "step_id",
        "memory_type",
        "status",
        "phase",
        "title",
        "preview_text",
        "source_event_id",
        "artifact_id",
        "artifact_uri",
        "source_start",
        "source_end",
        "source_unit",
        "importance",
        "token_count",
        "created_at_epoch",
        "updated_at_epoch",
        "content_hash",
        "facets",
    )

    _VARCHAR_LIMITS: ClassVar[dict[str, int]] = {
        "memory_id": 128,
        "tenant_id": 128,
        "workspace_id": 256,
        "user_id": 128,
        "task_id": 128,
        "run_id": 128,
        "step_id": 128,
        "memory_type": 64,
        "status": 32,
        "phase": 128,
        "title": 1024,
        "preview_text": 8192,
        "source_event_id": 128,
        "artifact_id": 128,
        "artifact_uri": 2048,
        "source_unit": 16,
        "content_hash": 96,
        "facets": 4096,
    }

    def __init__(
        self, config: PagedMemoryConfig, embedding_provider: EmbeddingProvider
    ) -> None:
        if embedding_provider.dimension != config.milvus_dense_dim:
            raise ValueError(
                f"embedding dimension {embedding_provider.dimension} does not match "
                f"MILVUS_DENSE_DIM={config.milvus_dense_dim}"
            )
        try:
            from pymilvus import DataType, MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "Milvus requires the optional 'memory' dependencies: "
                "pip install 'minicode-py[memory]'"
            ) from exc
        self.config = config
        self.embedding_provider = embedding_provider
        provider, model, revision = _provider_identity(embedding_provider)
        self.index_descriptor = VectorIndexDescriptor(
            collection=config.milvus_collection,
            provider=provider,
            model=model,
            revision=revision,
            dimension=embedding_provider.dimension,
            metric=config.milvus_metric_type,
        )
        self._DataType = DataType
        self._MilvusClient = MilvusClient
        connect_kwargs: dict[str, Any] = {
            "uri": config.milvus_uri,
            "timeout": config.milvus_timeout_seconds,
        }
        if config.milvus_token:
            connect_kwargs["token"] = config.milvus_token
        if config.milvus_database:
            connect_kwargs["db_name"] = config.milvus_database
        self.client = MilvusClient(**connect_kwargs)
        try:
            self._ensure_collection()
        except Exception:
            self.client.close()
            raise

    @property
    def _timeout(self) -> float:
        return float(self.config.milvus_timeout_seconds)

    def _field_definitions(self) -> list[tuple[str, Any, dict[str, Any]]]:
        varchar = self._DataType.VARCHAR
        return [
            ("memory_id", varchar, {"is_primary": True, "max_length": 128}),
            ("tenant_id", varchar, {"max_length": 128}),
            ("workspace_id", varchar, {"max_length": 256}),
            ("user_id", varchar, {"max_length": 128}),
            ("task_id", varchar, {"max_length": 128}),
            ("run_id", varchar, {"max_length": 128}),
            ("step_id", varchar, {"max_length": 128}),
            ("memory_type", varchar, {"max_length": 64}),
            ("status", varchar, {"max_length": 32}),
            ("phase", varchar, {"max_length": 128}),
            ("title", varchar, {"max_length": 1024}),
            ("preview_text", varchar, {"max_length": 8192}),
            ("source_event_id", varchar, {"max_length": 128}),
            ("artifact_id", varchar, {"max_length": 128}),
            ("artifact_uri", varchar, {"max_length": 2048}),
            ("source_start", self._DataType.INT64, {}),
            ("source_end", self._DataType.INT64, {}),
            ("source_unit", varchar, {"max_length": 16}),
            ("importance", self._DataType.FLOAT, {}),
            ("token_count", self._DataType.INT64, {}),
            ("created_at_epoch", self._DataType.INT64, {}),
            ("updated_at_epoch", self._DataType.INT64, {}),
            ("content_hash", varchar, {"max_length": 96}),
            ("facets", varchar, {"max_length": 4096}),
            (
                "dense_vector",
                self._DataType.FLOAT_VECTOR,
                {"dim": self.config.milvus_dense_dim},
            ),
        ]

    def _ensure_collection(self) -> None:
        name = self.config.milvus_collection
        if not self.client.has_collection(name, timeout=self._timeout):
            schema = self._MilvusClient.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
                description=(
                    "Rebuildable MiniCode memory cards; "
                    f"index_id={self.index_descriptor.index_id}"
                ),
            )
            for field_name, datatype, kwargs in self._field_definitions():
                schema.add_field(
                    field_name=field_name,
                    datatype=datatype,
                    **kwargs,
                )
            index_params = self.client.prepare_index_params()
            index_params.add_index(
                field_name="dense_vector",
                index_type=self.config.milvus_index_type,
                metric_type=self.config.milvus_metric_type,
                params={},
            )
            try:
                self.client.create_collection(
                    collection_name=name,
                    schema=schema,
                    index_params=index_params,
                    consistency_level=self.config.milvus_consistency_level,
                    timeout=self._timeout,
                )
            except Exception:
                # Multiple indexers may race to initialize the one shared
                # collection.  Suppress only the case where a peer completed it.
                if not self.client.has_collection(name, timeout=self._timeout):
                    raise
        self._validate_collection()
        self.client.load_collection(collection_name=name, timeout=self._timeout)

    def _validate_collection(self) -> None:
        name = self.config.milvus_collection
        description = self.client.describe_collection(name, timeout=self._timeout)
        fields = {
            str(field.get("name", "")): field
            for field in description.get("fields", [])
            if isinstance(field, Mapping)
        }
        errors: list[str] = []
        expected_marker = f"index_id={self.index_descriptor.index_id}"
        if expected_marker not in str(description.get("description", "")):
            errors.append(
                "collection index descriptor is missing or differs; use a new "
                "MILVUS_COLLECTION and rebuild instead of mixing embeddings"
            )
        for field_name, datatype, options in self._field_definitions():
            field = fields.get(field_name)
            if field is None:
                errors.append(f"missing field {field_name}")
                continue
            if field.get("type") != datatype:
                errors.append(f"field {field_name} has an incompatible type")
            params = field.get("params", {})
            params = params if isinstance(params, Mapping) else {}
            if "max_length" in options:
                try:
                    actual_max = int(params.get("max_length", 0))
                except (TypeError, ValueError):
                    actual_max = 0
                if actual_max < int(options["max_length"]):
                    errors.append(
                        f"field {field_name} max_length is {actual_max}, "
                        f"expected at least {options['max_length']}"
                    )
            if "dim" in options:
                try:
                    actual_dim = int(params.get("dim", 0))
                except (TypeError, ValueError):
                    actual_dim = 0
                if actual_dim != int(options["dim"]):
                    errors.append(
                        f"field {field_name} dimension is {actual_dim}, "
                        f"expected {options['dim']}"
                    )
        primary = fields.get("memory_id", {})
        if not primary.get("is_primary", False):
            errors.append("memory_id is not the primary field")

        indexes = self.client.list_indexes(
            name, field_name="dense_vector", timeout=self._timeout
        )
        if not indexes:
            errors.append("dense_vector index is missing")
        else:
            index = self.client.describe_index(
                name, str(indexes[0]), timeout=self._timeout
            )
            metric = str(index.get("metric_type", "")).upper()
            if metric != self.config.milvus_metric_type:
                errors.append(
                    f"dense_vector metric is {metric or 'unknown'}, "
                    f"expected {self.config.milvus_metric_type}"
                )
        if errors:
            detail = "; ".join(errors)
            raise RuntimeError(f"Milvus collection {name!r} is incompatible: {detail}")

    def upsert(self, records: Sequence[MemoryRecord]) -> int:
        if not records:
            return 0
        safe: list[tuple[MemoryRecord, str]] = []
        for record in records:
            text, _ = redact_secrets(f"{record.title}\n{record.preview_text}")
            safe.append((record, text))
        vectors = _checked_embeddings(
            self.embedding_provider,
            [text for _, text in safe],
            purpose="document",
        )
        rows: list[dict[str, Any]] = []
        for (record, _safe_text), vector in zip(safe, vectors):
            title, _ = redact_secrets(record.title)
            preview, _ = redact_secrets(record.preview_text)
            rows.append(
                {
                    "memory_id": self._exact_varchar("memory_id", record.id),
                    "tenant_id": self._exact_varchar("tenant_id", record.tenant_id),
                    "workspace_id": "",
                    "user_id": "",
                    "task_id": self._exact_varchar("task_id", record.task_id),
                    "run_id": self._exact_varchar("run_id", record.run_id),
                    "step_id": self._exact_varchar("step_id", record.step_id),
                    "memory_type": self._exact_varchar(
                        "memory_type", record.memory_type.value
                    ),
                    "status": self._exact_varchar("status", record.status.value),
                    "phase": self._exact_varchar("phase", record.phase),
                    "title": _utf8_prefix(title, self._VARCHAR_LIMITS["title"]),
                    "preview_text": _utf8_prefix(
                        preview, self._VARCHAR_LIMITS["preview_text"]
                    ),
                    "source_event_id": self._exact_varchar(
                        "source_event_id", record.source_event_id
                    ),
                    "artifact_id": self._exact_varchar(
                        "artifact_id", record.artifact_id
                    ),
                    "artifact_uri": "",
                    "source_start": record.source_range.start,
                    "source_end": record.source_range.end,
                    "source_unit": self._exact_varchar(
                        "source_unit", record.source_range.unit
                    ),
                    "importance": float(record.importance),
                    "token_count": int(record.token_count),
                    "created_at_epoch": int(record.created_at),
                    "updated_at_epoch": int(record.updated_at),
                    "content_hash": self._exact_varchar(
                        "content_hash", record.content_hash
                    ),
                    "facets": self._exact_varchar(
                        "facets", "\n".join(record.facets), allow_newline=True
                    ),
                    "dense_vector": vector,
                }
            )
        self.client.upsert(
            collection_name=self.config.milvus_collection,
            data=rows,
            timeout=self._timeout,
        )
        return len(rows)

    def delete(self, memory_ids: Sequence[str]) -> int:
        if not memory_ids:
            return 0
        ids = [self._exact_varchar("memory_id", value) for value in memory_ids]
        self.client.delete(
            collection_name=self.config.milvus_collection,
            ids=ids,
            timeout=self._timeout,
        )
        return len(memory_ids)

    def mark_status(self, memory_ids: Sequence[str], status: str) -> int:
        if not memory_ids:
            return 0
        ids = [self._exact_varchar("memory_id", value) for value in memory_ids]
        rows = self.client.get(
            collection_name=self.config.milvus_collection,
            ids=ids,
            output_fields=["memory_id"],
            consistency_level=self.config.milvus_consistency_level,
            timeout=self._timeout,
        )
        present_ids = [str(row["memory_id"]) for row in rows if row.get("memory_id")]
        if present_ids:
            safe_status = self._exact_varchar("status", status)
            self.client.upsert(
                collection_name=self.config.milvus_collection,
                data=[
                    {"memory_id": memory_id, "status": safe_status}
                    for memory_id in present_ids
                ],
                partial_update=True,
                timeout=self._timeout,
            )
        return len(present_ids)

    def flush(self) -> None:
        """Seal pending segments only at explicit maintenance boundaries."""

        self.client.flush(
            collection_name=self.config.milvus_collection,
            timeout=self._timeout,
        )

    def search(self, request: MemorySearchRequest) -> list[VectorHit]:
        if request.candidate_limit <= 0 or not request.statuses:
            return []
        query, _ = redact_secrets(request.query)
        vector = _checked_embeddings(self.embedding_provider, [query], purpose="query")[
            0
        ]
        filters = [
            f'tenant_id == "{self._escape(request.tenant_id)}"',
            f'task_id == "{self._escape(request.task_id)}"',
            self._in_expr("status", request.statuses),
        ]
        if request.memory_types:
            filters.append(self._in_expr("memory_type", request.memory_types))
        result = self.client.search(
            collection_name=self.config.milvus_collection,
            data=[vector],
            anns_field="dense_vector",
            search_params={
                "metric_type": self.config.milvus_metric_type,
                "params": self.config.milvus_search_params or {},
            },
            limit=request.candidate_limit,
            filter=" and ".join(filters),
            output_fields=[
                "memory_id",
                "content_hash",
                "status",
                "memory_type",
                "facets",
                "phase",
                "run_id",
            ],
            consistency_level=self.config.milvus_consistency_level,
            timeout=self._timeout,
        )
        hits: list[VectorHit] = []
        for hit in result[0] if result else []:
            if isinstance(hit, Mapping):
                raw_entity = hit.get("entity", {})
                entity = raw_entity if isinstance(raw_entity, Mapping) else {}
                memory_id = (
                    entity.get("memory_id") or hit.get("memory_id") or hit.get("id", "")
                )
                score = hit.get("distance", hit.get("score", 0.0))
            else:
                raw_entity = getattr(hit, "entity", None)
                entity = raw_entity if isinstance(raw_entity, Mapping) else {}
                memory_id = entity.get("memory_id") or getattr(hit, "id", "")
                score = getattr(hit, "score", 0.0)
            if not memory_id:
                continue
            facets = tuple(str(entity.get("facets") or "").splitlines())
            if set(request.excluded_facets).intersection(facets):
                continue
            normalized_score = (
                -float(score)
                if self.config.milvus_metric_type == "L2"
                else float(score)
            )
            normalized_score += scope_boost(
                record_phase=str(entity.get("phase") or ""),
                record_run_id=str(entity.get("run_id") or ""),
                phase=request.phase,
                run_id=request.run_id,
            )
            hits.append(
                VectorHit(
                    memory_id=str(memory_id),
                    score=normalized_score,
                    content_hash=str(entity.get("content_hash") or ""),
                    status=str(entity.get("status") or ""),
                    memory_type=str(entity.get("memory_type") or ""),
                    phase=str(entity.get("phase") or ""),
                    run_id=str(entity.get("run_id") or ""),
                    facets=facets,
                )
            )
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits

    def get(self, memory_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not memory_ids:
            return []
        ids = [self._exact_varchar("memory_id", value) for value in memory_ids]
        return list(
            self.client.get(
                collection_name=self.config.milvus_collection,
                ids=ids,
                output_fields=list(self._OUTPUT_FIELDS),
                consistency_level=self.config.milvus_consistency_level,
                timeout=self._timeout,
            )
        )

    def list_ids(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> set[str]:
        return set(
            self.list_entries(
                tenant_id=tenant_id,
                task_id=task_id,
                statuses=statuses,
            )
        )

    def list_entries(
        self,
        *,
        tenant_id: str,
        task_id: str,
        statuses: Sequence[str] = (),
    ) -> dict[str, str]:
        """Stream IDs with hashes so a same-ID stale vector cannot look healthy."""

        filters = [
            f'tenant_id == "{self._escape(tenant_id)}"',
            f'task_id == "{self._escape(task_id)}"',
        ]
        if statuses:
            filters.append(self._in_expr("status", statuses))
        iterator = self.client.query_iterator(
            collection_name=self.config.milvus_collection,
            batch_size=1_000,
            filter=" and ".join(filters),
            output_fields=["memory_id", "content_hash"],
            consistency_level=self.config.milvus_consistency_level,
            timeout=self._timeout,
        )
        entries: dict[str, str] = {}
        try:
            while True:
                rows = iterator.next()
                if not rows:
                    break
                for row in rows:
                    if isinstance(row, Mapping):
                        memory_id = row.get("memory_id") or row.get("id")
                    else:
                        memory_id = getattr(row, "memory_id", "") or getattr(
                            row, "id", ""
                        )
                    if memory_id:
                        content_hash = (
                            row.get("content_hash", "")
                            if isinstance(row, Mapping)
                            else getattr(row, "content_hash", "")
                        )
                        entries[str(memory_id)] = str(content_hash or "")
        finally:
            iterator.close()
        return entries

    def health_check(self) -> bool:
        try:
            return bool(
                self.client.has_collection(
                    self.config.milvus_collection, timeout=self._timeout
                )
            )
        except Exception:  # noqa: BLE001 - health checks collapse SDK failures.
            return False

    def rebuild(
        self,
        records: Iterable[MemoryRecord],
        *,
        tenant_id: str | None = None,
        task_id: str | None = None,
    ) -> int:
        material = list(records)
        if task_id and not tenant_id:
            raise ValueError("task-scoped vector rebuild requires tenant_id")
        if tenant_id:
            if any(
                record.tenant_id != tenant_id
                or (task_id is not None and record.task_id != task_id)
                for record in material
            ):
                raise ValueError("vector rebuild records escape the requested scope")
            safe_tenant = self._exact_varchar("tenant_id", tenant_id)
            filters = [f'tenant_id == "{self._escape(safe_tenant)}"']
            if task_id is not None:
                safe_task = self._exact_varchar("task_id", task_id)
                filters.append(f'task_id == "{self._escape(safe_task)}"')
            self.client.delete(
                collection_name=self.config.milvus_collection,
                filter=" and ".join(filters),
                timeout=self._timeout,
            )
        else:
            if self.client.has_collection(
                self.config.milvus_collection, timeout=self._timeout
            ):
                self.client.drop_collection(
                    self.config.milvus_collection, timeout=self._timeout
                )
            self._ensure_collection()
        count = 0
        size = self.config.milvus_batch_size
        for offset in range(0, len(material), size):
            count += self.upsert(material[offset : offset + size])
        if tenant_id is not None or material:
            self.flush()
        return count

    def close(self) -> None:
        # Collection load state is shared server-side. Releasing it here would
        # let one task disrupt every other task using the shared index.
        self.client.close()

    @classmethod
    def _exact_varchar(
        cls, field: str, value: str, *, allow_newline: bool = False
    ) -> str:
        text = str(value)
        max_bytes = cls._VARCHAR_LIMITS[field]
        if any(
            ord(char) < 32 and not (allow_newline and char == "\n") for char in text
        ):
            raise ValueError(f"Milvus field {field} contains control characters")
        actual_bytes = len(text.encode("utf-8"))
        if actual_bytes > max_bytes:
            raise ValueError(
                f"Milvus field {field} is {actual_bytes} UTF-8 bytes; "
                f"maximum is {max_bytes}"
            )
        return text

    @staticmethod
    def _escape(value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("Milvus filter value contains control characters")
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @classmethod
    def _in_expr(cls, field: str, values: Sequence[str]) -> str:
        allowed_fields = {"memory_id", "status", "memory_type"}
        if field not in allowed_fields:
            raise ValueError("unsupported Milvus filter field")
        rendered = ",".join(f'"{cls._escape(str(value))}"' for value in values)
        return f"{field} in [{rendered}]"


def build_vector_index(
    config: PagedMemoryConfig, embedding_provider: EmbeddingProvider | None = None
):
    provider = embedding_provider
    if provider is None:
        if config.embedding_provider == "fastembed":
            provider = FastEmbedEmbeddingProvider(
                model_name=config.embedding_model,
                dimension=config.milvus_dense_dim,
                revision=config.embedding_revision,
                cache_dir=config.embedding_cache_dir,
                threads=config.embedding_threads,
                batch_size=config.embedding_batch_size,
            )
        elif config.embedding_provider == "hash":
            if config.milvus_enabled and not config.embedding_allow_hash:
                raise ValueError(
                    "hash embeddings are test-only; set "
                    "MEMORY_EMBEDDING_ALLOW_HASH=true only for explicit tests"
                )
            provider = HashEmbeddingProvider(config.milvus_dense_dim)
        else:
            raise ValueError(
                f"unsupported embedding provider: {config.embedding_provider}"
            )
    if config.milvus_enabled:
        return MilvusVectorIndex(config, provider)
    return InMemoryVectorIndex(provider)


__all__ = [
    "FastEmbedEmbeddingProvider",
    "HashEmbeddingProvider",
    "InMemoryVectorIndex",
    "MilvusVectorIndex",
    "VectorHit",
    "VectorIndexDescriptor",
    "build_vector_index",
    "configured_index_descriptor",
    "redact_secrets",
]
