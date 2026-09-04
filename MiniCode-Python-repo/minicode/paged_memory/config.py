"""Configuration and validation for paged memory."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR

from .models import MemoryMode, RetentionRule

_DEFAULT_MEMORY_ROOT = MINI_CODE_DIR / "memory-v2"
_DEFAULT_EMBEDDING_CACHE = MINI_CODE_DIR / "models" / "fastembed"


def _bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def _int(value: Any, default: int, *, minimum: int = 0) -> int:
    if value in (None, ""):
        return default
    parsed = int(value)
    if parsed < minimum:
        raise ValueError(f"value must be >= {minimum}: {parsed}")
    return parsed


def _float(
    value: Any, default: float, *, minimum: float = 0.0, maximum: float | None = None
) -> float:
    if value in (None, ""):
        return default
    parsed = float(value)
    if parsed < minimum or (maximum is not None and parsed > maximum):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise ValueError(f"value must be >= {minimum}{suffix}: {parsed}")
    return parsed


def _json_object(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    if value in (None, ""):
        return dict(default)
    if isinstance(value, Mapping):
        return dict(value)
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise TypeError("configuration value must be a JSON object")
    return parsed


def _retention_rules(value: Any) -> tuple[RetentionRule, ...]:
    if value in (None, ""):
        return ()
    parsed = value if isinstance(value, (list, tuple)) else json.loads(str(value))
    if not isinstance(parsed, (list, tuple)):
        raise TypeError("MEMORY_RETENTION_RULES_JSON must be a JSON array")
    rules: list[RetentionRule] = []
    tuple_fields = {
        "tenant_ids",
        "event_types",
        "memory_types",
        "artifact_types",
        "sensitivity_levels",
        "task_statuses",
    }
    allowed = RetentionRule.__dataclass_fields__.keys()
    for raw in parsed:
        if not isinstance(raw, Mapping):
            raise TypeError("each retention rule must be a JSON object")
        unknown = set(raw) - set(allowed)
        if unknown:
            raise ValueError(
                "unknown retention rule fields: " + ", ".join(sorted(unknown))
            )
        values = dict(raw)
        for field_name in tuple_fields:
            if field_name in values:
                items = values[field_name]
                if not isinstance(items, (list, tuple)):
                    raise TypeError(f"retention rule {field_name} must be an array")
                values[field_name] = tuple(str(item) for item in items)
        rule = RetentionRule(**values)
        if rule.deletion_mode not in {"HARD_DELETE", "TOMBSTONE"}:
            raise ValueError("retention deletion_mode must be HARD_DELETE or TOMBSTONE")
        if rule.retention_seconds is not None and rule.retention_seconds < 0:
            raise ValueError("retention_seconds must be non-negative")
        if rule.archive_after_seconds is not None and rule.archive_after_seconds < 0:
            raise ValueError("archive_after_seconds must be non-negative")
        rules.append(rule)
    return tuple(rules)


@dataclass(frozen=True)
class PagedMemoryConfig:
    enabled: bool = False
    shadow_write: bool = False
    auto_mode: bool = True
    default_mode: MemoryMode = MemoryMode.INLINE
    manual_mode: MemoryMode | None = None

    backend: str = "sqlite"
    database_url: str = ""
    sqlite_path: str = str(_DEFAULT_MEMORY_ROOT / "state.db")
    tenant_id: str = "local"
    workspace_id: str = ""
    user_id: str = "local"

    working_set_context_ratio: float = 0.30
    paged_context_ratio: float = 0.60
    working_set_step_threshold: int = 12
    paged_step_threshold: int = 40
    tool_result_token_threshold: int = 10_000
    large_artifact_threshold: int = 3
    downgrade_stable_steps: int = 5

    dependency_max_depth: int = 3
    recent_event_token_budget: int = 1_500
    working_set_token_budget: int = 2_000
    supplemental_token_budget: int = 1_200
    raw_page_token_limit: int = 1_500
    candidate_limit: int = 40
    output_token_reserve: int = 4_096
    tool_token_reserve: int = 2_048
    safety_token_reserve: int = 1_024
    inline_recent_token_budget: int = 24_000

    artifact_store_type: str = "file"
    artifact_root: str = str(_DEFAULT_MEMORY_ROOT / "artifacts")
    artifact_large_bytes: int = 16_000
    artifact_endpoint: str = ""
    artifact_bucket: str = "minicode-artifacts"
    artifact_access_key: str = ""
    artifact_secret_key: str = ""
    artifact_region: str = "us-east-1"
    artifact_secure: bool = False
    local_spool_enabled: bool = True
    local_spool_root: str = str(_DEFAULT_MEMORY_ROOT / "spool")
    local_spool_max_bytes: int = 256 * 1024 * 1024
    local_spool_min_free_bytes: int = 64 * 1024 * 1024
    local_spool_key: str = ""

    indexer_enabled: bool = True
    indexer_async: bool = True
    indexer_max_retries: int = 8
    indexer_retry_backoff_seconds: int = 5
    milvus_enabled: bool = False
    milvus_uri: str = ""
    milvus_token: str = ""
    milvus_database: str = "default"
    milvus_collection: str = "agent_memory_v2"
    milvus_dense_dim: int = 512
    milvus_metric_type: str = "COSINE"
    milvus_consistency_level: str = "Strong"
    milvus_index_type: str = "AUTOINDEX"
    milvus_search_params: dict[str, Any] | None = None
    milvus_enable_sparse: bool = False
    milvus_enable_hybrid: bool = True
    milvus_ranker: str = "RRF"
    milvus_batch_size: int = 64
    milvus_timeout_seconds: int = 10
    embedding_provider: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_revision: str = "fastembed-0.8.0-builtin-v1"
    embedding_cache_dir: str = str(_DEFAULT_EMBEDDING_CACHE)
    embedding_threads: int = 0
    embedding_batch_size: int = 64
    embedding_allow_hash: bool = False

    retention_default_days: int = 365
    archive_max_events: int = 100_000
    archive_max_uncompressed_bytes: int = 1024 * 1024 * 1024
    artifact_gc_grace_hours: int = 24
    backup_max_age_hours: int = 24
    backup_root: str = str(_DEFAULT_MEMORY_ROOT / "backups")
    backup_encryption_key: str = ""
    backup_rpo_hours: int = 24
    backup_rto_hours: int = 4
    legal_hold_default: bool = False
    retention_rules: tuple[RetentionRule, ...] = ()

    @classmethod
    def from_sources(
        cls,
        runtime: Mapping[str, Any] | None = None,
        *,
        cwd: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> PagedMemoryConfig:
        runtime = runtime or {}
        env = environ or os.environ
        # Runtime V2 may execute a Writer from a temporary workspace clone.  All
        # Workers in one task must nevertheless resolve relative authority,
        # Artifact, spool and backup paths against the coordinator workspace.
        # The internal key is injected before isolation and is deliberately
        # only a base path: environment values still keep their normal
        # precedence below.
        authority_cwd = runtime.get("_memoryAuthorityCwd") or cwd or Path.cwd()
        root = Path(str(authority_cwd)).resolve()

        def get(env_name: str, runtime_name: str, default: Any = "") -> Any:
            if env_name in env and env[env_name] != "":
                return env[env_name]
            value = runtime.get(runtime_name, default)
            return default if value in (None, "") else value

        default_mode = MemoryMode(
            str(get("MEMORY_MODE_DEFAULT", "memoryModeDefault", "INLINE")).upper()
        )
        manual_raw = (
            str(get("MEMORY_MODE_OVERRIDE", "memoryModeOverride", "")).strip().upper()
        )
        manual_mode = MemoryMode(manual_raw) if manual_raw else None
        sqlite_raw = str(get("MEMORY_SQLITE_PATH", "memorySqlitePath", cls.sqlite_path))
        artifact_raw = str(
            get("ARTIFACT_STORE_ROOT", "artifactStoreRoot", cls.artifact_root)
        )
        spool_raw = str(
            get("MEMORY_LOCAL_SPOOL_ROOT", "memoryLocalSpoolRoot", cls.local_spool_root)
        )
        backup_raw = str(get("MEMORY_BACKUP_ROOT", "memoryBackupRoot", cls.backup_root))
        embedding_cache_raw = str(
            get(
                "MEMORY_EMBEDDING_CACHE_DIR",
                "memoryEmbeddingCacheDir",
                cls.embedding_cache_dir,
            )
        )

        def rooted(value: str) -> str:
            path = Path(value).expanduser()
            return str(path if path.is_absolute() else root / path)

        config = cls(
            enabled=_bool(get("MEMORY_V2_ENABLED", "memoryV2", False), False),
            shadow_write=_bool(
                get("MEMORY_SHADOW_WRITE", "memoryShadowWrite", False), False
            ),
            auto_mode=_bool(get("MEMORY_MODE_AUTO", "memoryModeAuto", True), True),
            default_mode=default_mode,
            manual_mode=manual_mode,
            backend=str(get("MEMORY_BACKEND", "memoryBackend", "sqlite")).lower(),
            database_url=str(get("MEMORY_DATABASE_URL", "memoryDatabaseUrl", "")),
            sqlite_path=rooted(sqlite_raw),
            tenant_id=str(get("MEMORY_TENANT_ID", "memoryTenantId", "local")),
            workspace_id=str(
                get("MEMORY_WORKSPACE_ID", "memoryWorkspaceId", root.name)
            ),
            user_id=str(get("MEMORY_USER_ID", "memoryUserId", "local")),
            working_set_context_ratio=_float(
                get(
                    "MEMORY_WORKING_SET_CONTEXT_RATIO",
                    "memoryWorkingSetContextRatio",
                    0.30,
                ),
                0.30,
                maximum=1.0,
            ),
            paged_context_ratio=_float(
                get("MEMORY_PAGED_CONTEXT_RATIO", "memoryPagedContextRatio", 0.60),
                0.60,
                maximum=1.0,
            ),
            working_set_step_threshold=_int(
                get(
                    "MEMORY_WORKING_SET_STEP_THRESHOLD",
                    "memoryWorkingSetStepThreshold",
                    12,
                ),
                12,
            ),
            paged_step_threshold=_int(
                get("MEMORY_PAGED_STEP_THRESHOLD", "memoryPagedStepThreshold", 40), 40
            ),
            tool_result_token_threshold=_int(
                get(
                    "MEMORY_TOOL_RESULT_TOKEN_THRESHOLD",
                    "memoryToolResultTokenThreshold",
                    10_000,
                ),
                10_000,
            ),
            large_artifact_threshold=_int(
                get(
                    "MEMORY_LARGE_ARTIFACT_THRESHOLD", "memoryLargeArtifactThreshold", 3
                ),
                3,
            ),
            downgrade_stable_steps=_int(
                get("MEMORY_DOWNGRADE_STABLE_STEPS", "memoryDowngradeStableSteps", 5),
                5,
                minimum=1,
            ),
            dependency_max_depth=_int(
                get("MEMORY_DEPENDENCY_MAX_DEPTH", "memoryDependencyMaxDepth", 3),
                3,
                minimum=1,
            ),
            recent_event_token_budget=_int(
                get(
                    "MEMORY_RECENT_EVENT_TOKEN_BUDGET",
                    "memoryRecentEventTokenBudget",
                    1500,
                ),
                1500,
            ),
            working_set_token_budget=_int(
                get(
                    "MEMORY_WORKING_SET_TOKEN_BUDGET",
                    "memoryWorkingSetTokenBudget",
                    2000,
                ),
                2000,
            ),
            supplemental_token_budget=_int(
                get(
                    "MEMORY_SUPPLEMENTAL_TOKEN_BUDGET",
                    "memorySupplementalTokenBudget",
                    1200,
                ),
                1200,
            ),
            raw_page_token_limit=_int(
                get("MEMORY_RAW_PAGE_TOKEN_LIMIT", "memoryRawPageTokenLimit", 1500),
                1500,
            ),
            candidate_limit=_int(
                get("MEMORY_CANDIDATE_LIMIT", "memoryCandidateLimit", 40), 40, minimum=1
            ),
            output_token_reserve=_int(
                get("MEMORY_OUTPUT_TOKEN_RESERVE", "memoryOutputTokenReserve", 4096),
                4096,
            ),
            tool_token_reserve=_int(
                get("MEMORY_TOOL_TOKEN_RESERVE", "memoryToolTokenReserve", 2048), 2048
            ),
            safety_token_reserve=_int(
                get("MEMORY_SAFETY_TOKEN_RESERVE", "memorySafetyTokenReserve", 1024),
                1024,
            ),
            inline_recent_token_budget=_int(
                get(
                    "MEMORY_INLINE_RECENT_TOKEN_BUDGET",
                    "memoryInlineRecentTokenBudget",
                    24000,
                ),
                24000,
            ),
            artifact_store_type=str(
                get("ARTIFACT_STORE_TYPE", "artifactStoreType", "file")
            ).lower(),
            artifact_root=rooted(artifact_raw),
            artifact_large_bytes=_int(
                get("MEMORY_ARTIFACT_LARGE_BYTES", "memoryArtifactLargeBytes", 16000),
                16000,
                minimum=1,
            ),
            artifact_endpoint=str(
                get("ARTIFACT_STORE_ENDPOINT", "artifactStoreEndpoint", "")
            ),
            artifact_bucket=str(
                get(
                    "ARTIFACT_STORE_BUCKET", "artifactStoreBucket", "minicode-artifacts"
                )
            ),
            artifact_access_key=str(
                get("ARTIFACT_STORE_ACCESS_KEY", "artifactStoreAccessKey", "")
            ),
            artifact_secret_key=str(
                get("ARTIFACT_STORE_SECRET_KEY", "artifactStoreSecretKey", "")
            ),
            artifact_region=str(
                get("ARTIFACT_STORE_REGION", "artifactStoreRegion", "us-east-1")
            ),
            artifact_secure=_bool(
                get("ARTIFACT_STORE_SECURE", "artifactStoreSecure", False), False
            ),
            local_spool_enabled=_bool(
                get("MEMORY_LOCAL_SPOOL_ENABLED", "memoryLocalSpoolEnabled", True), True
            ),
            local_spool_root=rooted(spool_raw),
            local_spool_max_bytes=_int(
                get(
                    "MEMORY_LOCAL_SPOOL_MAX_BYTES",
                    "memoryLocalSpoolMaxBytes",
                    268435456,
                ),
                268435456,
                minimum=1,
            ),
            local_spool_min_free_bytes=_int(
                get(
                    "MEMORY_LOCAL_SPOOL_MIN_FREE_BYTES",
                    "memoryLocalSpoolMinFreeBytes",
                    67108864,
                ),
                67108864,
            ),
            local_spool_key=str(
                get("MEMORY_LOCAL_SPOOL_KEY", "memoryLocalSpoolKey", "")
            ),
            indexer_enabled=_bool(
                get("MEMORY_INDEXER_ENABLED", "memoryIndexerEnabled", True), True
            ),
            indexer_async=_bool(
                get("MEMORY_INDEXER_ASYNC", "memoryIndexerAsync", True), True
            ),
            indexer_max_retries=_int(
                get("MEMORY_INDEXER_MAX_RETRIES", "memoryIndexerMaxRetries", 8), 8
            ),
            indexer_retry_backoff_seconds=_int(
                get(
                    "MEMORY_INDEXER_RETRY_BACKOFF_SECONDS",
                    "memoryIndexerRetryBackoffSeconds",
                    5,
                ),
                5,
            ),
            milvus_enabled=_bool(get("MILVUS_ENABLED", "milvusEnabled", False), False),
            milvus_uri=str(get("MILVUS_URI", "milvusUri", "")),
            milvus_token=str(get("MILVUS_TOKEN", "milvusToken", "")),
            milvus_database=str(get("MILVUS_DATABASE", "milvusDatabase", "default")),
            milvus_collection=str(
                get("MILVUS_COLLECTION", "milvusCollection", "agent_memory_v2")
            ),
            milvus_dense_dim=_int(
                get("MILVUS_DENSE_DIM", "milvusDenseDim", 512), 512, minimum=1
            ),
            milvus_metric_type=str(
                get("MILVUS_METRIC_TYPE", "milvusMetricType", "COSINE")
            ).upper(),
            milvus_consistency_level=str(
                get(
                    "MILVUS_CONSISTENCY_LEVEL",
                    "milvusConsistencyLevel",
                    "Strong",
                )
            ).title(),
            milvus_index_type=str(
                get("MILVUS_INDEX_TYPE", "milvusIndexType", "AUTOINDEX")
            ).upper(),
            milvus_search_params=_json_object(
                get("MILVUS_SEARCH_PARAMS", "milvusSearchParams", {}), {}
            ),
            milvus_enable_sparse=_bool(
                get("MILVUS_ENABLE_SPARSE", "milvusEnableSparse", False), False
            ),
            milvus_enable_hybrid=_bool(
                get("MILVUS_ENABLE_HYBRID", "milvusEnableHybrid", True), True
            ),
            milvus_ranker=str(get("MILVUS_RANKER", "milvusRanker", "RRF")).upper(),
            milvus_batch_size=_int(
                get("MILVUS_BATCH_SIZE", "milvusBatchSize", 64), 64, minimum=1
            ),
            milvus_timeout_seconds=_int(
                get("MILVUS_TIMEOUT_SECONDS", "milvusTimeoutSeconds", 10), 10, minimum=1
            ),
            embedding_provider=str(
                get(
                    "MEMORY_EMBEDDING_PROVIDER",
                    "memoryEmbeddingProvider",
                    "fastembed",
                )
            ).lower(),
            embedding_model=str(
                get(
                    "MEMORY_EMBEDDING_MODEL",
                    "memoryEmbeddingModel",
                    "BAAI/bge-small-zh-v1.5",
                )
            ),
            embedding_revision=str(
                get(
                    "MEMORY_EMBEDDING_REVISION",
                    "memoryEmbeddingRevision",
                    "fastembed-0.8.0-builtin-v1",
                )
            ),
            embedding_cache_dir=rooted(embedding_cache_raw),
            embedding_threads=_int(
                get("MEMORY_EMBEDDING_THREADS", "memoryEmbeddingThreads", 0),
                0,
            ),
            embedding_batch_size=_int(
                get(
                    "MEMORY_EMBEDDING_BATCH_SIZE",
                    "memoryEmbeddingBatchSize",
                    64,
                ),
                64,
                minimum=1,
            ),
            embedding_allow_hash=_bool(
                get(
                    "MEMORY_EMBEDDING_ALLOW_HASH",
                    "memoryEmbeddingAllowHash",
                    False,
                ),
                False,
            ),
            retention_default_days=_int(
                get("MEMORY_RETENTION_DEFAULT_DAYS", "memoryRetentionDefaultDays", 365),
                365,
            ),
            archive_max_events=_int(
                get("MEMORY_ARCHIVE_MAX_EVENTS", "memoryArchiveMaxEvents", 100_000),
                100_000,
                minimum=1,
            ),
            archive_max_uncompressed_bytes=_int(
                get(
                    "MEMORY_ARCHIVE_MAX_UNCOMPRESSED_BYTES",
                    "memoryArchiveMaxUncompressedBytes",
                    1073741824,
                ),
                1073741824,
                minimum=1,
            ),
            artifact_gc_grace_hours=_int(
                get("MEMORY_ARTIFACT_GC_GRACE_HOURS", "memoryArtifactGcGraceHours", 24),
                24,
            ),
            backup_max_age_hours=_int(
                get("MEMORY_BACKUP_MAX_AGE_HOURS", "memoryBackupMaxAgeHours", 24), 24
            ),
            backup_root=rooted(backup_raw),
            backup_encryption_key=str(
                get("MEMORY_BACKUP_ENCRYPTION_KEY", "memoryBackupEncryptionKey", "")
            ),
            backup_rpo_hours=_int(
                get("MEMORY_BACKUP_RPO_HOURS", "memoryBackupRpoHours", 24),
                24,
                minimum=1,
            ),
            backup_rto_hours=_int(
                get("MEMORY_BACKUP_RTO_HOURS", "memoryBackupRtoHours", 4), 4, minimum=1
            ),
            legal_hold_default=_bool(
                get("MEMORY_LEGAL_HOLD_DEFAULT", "memoryLegalHoldDefault", False), False
            ),
            retention_rules=_retention_rules(
                get("MEMORY_RETENTION_RULES_JSON", "memoryRetentionRules", "")
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.backend not in {"sqlite", "postgres"}:
            raise ValueError("MEMORY_BACKEND must be sqlite or postgres")
        active = self.enabled or self.shadow_write
        if self.backend == "postgres" and active and not self.database_url:
            raise ValueError(
                "MEMORY_DATABASE_URL is required when PostgreSQL memory is enabled"
            )
        if not 0 <= self.working_set_context_ratio < self.paged_context_ratio <= 1:
            raise ValueError(
                "memory mode context ratios must satisfy 0 <= working < paged <= 1"
            )
        if self.working_set_step_threshold >= self.paged_step_threshold:
            raise ValueError(
                "working-set step threshold must be lower than paged threshold"
            )
        if self.artifact_store_type not in {"file", "s3", "minio"}:
            raise ValueError("ARTIFACT_STORE_TYPE must be file, s3, or minio")
        if (
            self.artifact_store_type in {"s3", "minio"}
            and active
            and (not self.artifact_endpoint or not self.artifact_bucket)
        ):
            raise ValueError("Artifact endpoint and bucket are required for S3/MinIO")
        if self.milvus_enabled and active and not self.milvus_uri:
            raise ValueError("MILVUS_URI is required when Milvus is enabled")
        if self.milvus_metric_type not in {"COSINE", "IP", "L2"}:
            raise ValueError("MILVUS_METRIC_TYPE must be COSINE, IP, or L2")
        if self.milvus_consistency_level not in {
            "Strong",
            "Bounded",
            "Session",
            "Eventually",
        }:
            raise ValueError(
                "MILVUS_CONSISTENCY_LEVEL must be Strong, Bounded, Session, "
                "or Eventually"
            )
        if self.embedding_provider not in {"fastembed", "hash"}:
            raise ValueError("MEMORY_EMBEDDING_PROVIDER must be fastembed or hash")
        if self.milvus_enabled and not self.embedding_model:
            raise ValueError("MEMORY_EMBEDDING_MODEL is required when Milvus is enabled")
        if self.milvus_enabled and not self.embedding_revision:
            raise ValueError(
                "MEMORY_EMBEDDING_REVISION is required when Milvus is enabled"
            )
        if (
            self.milvus_enabled
            and self.embedding_provider == "hash"
            and not self.embedding_allow_hash
        ):
            raise ValueError(
                "hash embeddings are test-only; set a real embedding provider or "
                "explicitly opt in with MEMORY_EMBEDDING_ALLOW_HASH=true"
            )

    def safe_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "shadow_write": self.shadow_write,
            "backend": self.backend,
            "default_mode": self.default_mode.value,
            "manual_mode": self.manual_mode.value if self.manual_mode else None,
            "artifact_store_type": self.artifact_store_type,
            "milvus_enabled": self.milvus_enabled,
            "milvus_collection": self.milvus_collection,
            "milvus_consistency_level": self.milvus_consistency_level,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_revision": self.embedding_revision,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "retention_rule_count": len(self.retention_rules),
            "archive_max_events": self.archive_max_events,
        }
