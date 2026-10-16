"""Configuration and validation for paged memory."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR

from .models import MemoryMode, RetentionRule

_DEFAULT_MEMORY_ROOT = MINI_CODE_DIR / "memory-v2"


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
    handoff_notes_enabled: bool = False
    context_rotation_enabled: bool = False
    context_mode_policy_enabled: bool = False
    tool_result_projection_enabled: bool = False
    tool_result_reclaim_policy_enabled: bool = False
    tool_result_reclaim_min_tokens: int = 512
    tool_result_reclaim_min_ratio: float = 0.10
    elastic_context_budget_enabled: bool = False
    context_utilization_enabled: bool = False
    context_note_warning_ratio: float = 0.80
    context_rotation_target_ratio: float = 0.85
    context_protection_ratio: float = 0.90
    context_growth_reserve_tokens: int = 8_192
    context_recovery_max_steps: int = 6
    code_focus_enabled: bool = False
    handoff_note_token_budget: int = 2_048

    backend: str = "sqlite"
    data_root: str = ""
    export_root: str = str(_DEFAULT_MEMORY_ROOT / "exports")
    sqlite_path: str = str(_DEFAULT_MEMORY_ROOT / "state.db")
    tenant_id: str = "local"
    workspace_id: str = ""
    user_id: str = "local"

    # Keep legacy setting names: working_set_* enters paging; paged_* admits
    # optional reranking. Working-set token budgets remain an active component.
    working_set_context_ratio: float = 0.30
    paged_context_ratio: float = 0.60
    working_set_step_threshold: int = 12
    paged_step_threshold: int = 40
    tool_result_token_threshold: int = 10_000
    large_artifact_threshold: int = 3
    downgrade_stable_steps: int = 5

    dependency_max_depth: int = 3
    recent_event_token_budget: int = 1_500
    recent_memory_token_budget: int = 1_500
    working_set_token_budget: int = 2_000
    supplemental_token_budget: int = 1_200
    raw_page_token_limit: int = 1_500
    candidate_limit: int = 40
    lexical_candidate_limit: int = 0  # 0 inherits candidate_limit
    context_card_limit: int = 12
    retrieval_refill_multiplier: int = 4
    retrieval_timeout_seconds: float = 3.0
    context_retrieval_timeout_seconds: float = 12.0
    post_rerank_refill: bool = True
    source_neighbor_limit: int = 8
    adaptive_retrieval: bool = False
    adaptive_min_candidates: int = 20
    adaptive_max_candidates: int = 80
    rerank_enabled: bool = False
    rerank_shadow: bool = False
    rerank_policy: str = "adaptive"
    rerank_shared_coordination: bool = True
    rerank_provider: str = ""
    rerank_model: str = ""
    rerank_base_url: str = ""
    rerank_api_key: str = field(default="", repr=False)
    rerank_candidate_limit: int = 32
    rerank_input_token_budget: int = 6000
    rerank_timeout_seconds: float = 8.0
    rerank_failure_threshold: int = 2
    rerank_cooldown_seconds: float = 60.0
    rerank_cache_ttl_seconds: float = 120.0
    rerank_min_score: int = 2
    output_token_reserve: int = 4_096
    tool_token_reserve: int = 2_048
    safety_token_reserve: int = 1_024
    inline_recent_token_budget: int = 24_000

    artifact_store_type: str = "file"
    artifact_root: str = str(_DEFAULT_MEMORY_ROOT / "artifacts")
    artifact_large_bytes: int = 16_000
    local_spool_enabled: bool = True
    local_spool_root: str = str(_DEFAULT_MEMORY_ROOT / "spool")
    local_spool_max_bytes: int = 256 * 1024 * 1024
    local_spool_min_free_bytes: int = 64 * 1024 * 1024
    local_spool_key: str = ""


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
        env = environ if environ is not None else os.environ
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

        # A primary-led Ultra request pins its task and Worker instructions.
        # At 1500 tokens those alone can exhaust the recent-exchange allowance,
        # evicting the source as soon as the Worker reads its specification (and
        # vice versa). Keep a bounded multi-file working conversation. This is
        # a context allocation, not extra run budget; native admission and the
        # provider context guard still account for every transmitted byte.
        elastic = _bool(get("MEMORY_ELASTIC_CONTEXT_BUDGET_ENABLED", "memoryElasticContextBudgetEnabled", False), False)
        primary_ultra = runtime.get("runtimeOrchestration") == "primary" and runtime.get("executionProfile") == "ultra"
        recent_default = 12_000 if primary_ultra else (4000 if elastic else 1500)
        working_default, supplemental_default = (3000, 2000) if elastic else (2000, 1200)
        # Notes include source IDs and JSON structure as well as prose. Allow
        # larger multi-file handoffs while preserving explicit budget overrides.
        handoff_default = 4_096 if primary_ultra else cls.handoff_note_token_budget

        default_mode = MemoryMode(
            str(get("MEMORY_MODE_DEFAULT", "memoryModeDefault", "INLINE")).upper()
        )
        manual_raw = (
            str(get("MEMORY_MODE_OVERRIDE", "memoryModeOverride", "")).strip().upper()
        )
        manual_mode = MemoryMode(manual_raw) if manual_raw else None
        data_root_raw = str(get("MINI_CODE_DATA_ROOT", "memoryDataRoot", ""))
        data_root = Path(data_root_raw).expanduser() if data_root_raw else None
        if data_root is not None and not data_root.is_absolute():
            data_root = root / data_root
        sqlite_default = str(data_root / "database" / "state.db") if data_root else cls.sqlite_path
        sqlite_raw = str(get("MEMORY_SQLITE_PATH", "memorySqlitePath", sqlite_default))
        artifact_raw = str(
            get("ARTIFACT_STORE_ROOT", "artifactStoreRoot", str(data_root / "artifacts") if data_root else cls.artifact_root)
        )
        spool_raw = str(
            get("MEMORY_LOCAL_SPOOL_ROOT", "memoryLocalSpoolRoot", str(data_root / "spool") if data_root else cls.local_spool_root)
        )
        backup_raw = str(get("MEMORY_BACKUP_ROOT", "memoryBackupRoot", str(data_root / "backups") if data_root else cls.backup_root))
        export_raw = str(get("MEMORY_EXPORT_ROOT", "memoryExportRoot", str(data_root / "exports") if data_root else cls.export_root))

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
            handoff_notes_enabled=_bool(
                get("MEMORY_HANDOFF_NOTES_ENABLED", "memoryHandoffNotesEnabled", False), False
            ),
            handoff_note_token_budget=_int(
                get("MEMORY_HANDOFF_NOTE_TOKEN_BUDGET", "memoryHandoffNoteTokenBudget", handoff_default),
                handoff_default, minimum=256,
            ),
            context_rotation_enabled=_bool(
                get("MEMORY_CONTEXT_ROTATION_ENABLED", "memoryContextRotationEnabled", False), False
            ),
            context_mode_policy_enabled=_bool(
                get("MEMORY_CONTEXT_MODE_POLICY_ENABLED", "memoryContextModePolicyEnabled", False), False
            ),
            tool_result_projection_enabled=_bool(
                get("MEMORY_TOOL_RESULT_PROJECTION_ENABLED", "memoryToolResultProjectionEnabled", False), False
            ),
            tool_result_reclaim_policy_enabled=_bool(
                get("MEMORY_TOOL_RESULT_RECLAIM_POLICY_ENABLED", "memoryToolResultReclaimPolicyEnabled", False), False
            ),
            tool_result_reclaim_min_tokens=_int(
                get("MEMORY_TOOL_RESULT_RECLAIM_MIN_TOKENS", "memoryToolResultReclaimMinTokens", 512), 512, minimum=1
            ),
            tool_result_reclaim_min_ratio=_float(
                get("MEMORY_TOOL_RESULT_RECLAIM_MIN_RATIO", "memoryToolResultReclaimMinRatio", .10), .10
            ),
            elastic_context_budget_enabled=elastic,
            context_utilization_enabled=_bool(
                get("MEMORY_CONTEXT_UTILIZATION_ENABLED", "memoryContextUtilizationEnabled", False), False
            ),
            context_note_warning_ratio=_float(
                get("MEMORY_CONTEXT_NOTE_WARNING_RATIO", "memoryContextNoteWarningRatio", 0.80), 0.80,
            ),
            context_rotation_target_ratio=_float(
                get("MEMORY_CONTEXT_ROTATION_TARGET_RATIO", "memoryContextRotationTargetRatio", 0.85), 0.85,
            ),
            context_protection_ratio=_float(
                get("MEMORY_CONTEXT_PROTECTION_RATIO", "memoryContextProtectionRatio", 0.90), 0.90,
            ),
            context_growth_reserve_tokens=_int(
                get("MEMORY_CONTEXT_GROWTH_RESERVE_TOKENS", "memoryContextGrowthReserveTokens", 8192), 8192,
                minimum=256,
            ),
            context_recovery_max_steps=_int(
                get("MEMORY_CONTEXT_RECOVERY_MAX_STEPS", "memoryContextRecoveryMaxSteps", 6), 6, minimum=2,
            ),
            code_focus_enabled=_bool(
                get("MEMORY_CODE_FOCUS_ENABLED", "memoryCodeFocusEnabled", False), False
            ),
            recent_memory_token_budget=_int(
                get("MEMORY_RECENT_MEMORY_TOKEN_BUDGET", "memoryRecentMemoryTokenBudget", 1500), 1500
            ),
            backend=str(get("MEMORY_BACKEND", "memoryBackend", "sqlite")).lower(),
            data_root=str(data_root.resolve()) if data_root else "",
            export_root=rooted(export_raw),
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
                    recent_default,
                ),
                recent_default,
            ),
            working_set_token_budget=_int(
                get(
                    "MEMORY_WORKING_SET_TOKEN_BUDGET",
                    "memoryWorkingSetTokenBudget",
                    working_default,
                ),
                working_default,
            ),
            supplemental_token_budget=_int(
                get(
                    "MEMORY_SUPPLEMENTAL_TOKEN_BUDGET",
                    "memorySupplementalTokenBudget",
                    supplemental_default,
                ),
                supplemental_default,
            ),
            raw_page_token_limit=_int(
                get("MEMORY_RAW_PAGE_TOKEN_LIMIT", "memoryRawPageTokenLimit", 1500),
                1500,
            ),
            candidate_limit=_int(
                get("MEMORY_CANDIDATE_LIMIT", "memoryCandidateLimit", 40), 40, minimum=1
            ),
            lexical_candidate_limit=_int(
                get("MEMORY_LEXICAL_CANDIDATE_LIMIT", "memoryLexicalCandidateLimit", 0),
                0,
            ),
            context_card_limit=_int(
                get("MEMORY_CONTEXT_CARD_LIMIT", "memoryContextCardLimit", 12),
                12,
                minimum=1,
            ),
            retrieval_refill_multiplier=_int(
                get(
                    "MEMORY_RETRIEVAL_REFILL_MULTIPLIER",
                    "memoryRetrievalRefillMultiplier",
                    4,
                ),
                4,
                minimum=1,
            ),
            retrieval_timeout_seconds=_float(
                get(
                    "MEMORY_RETRIEVAL_TIMEOUT_SECONDS",
                    "memoryRetrievalTimeoutSeconds",
                    3.0,
                ),
                3.0,
                minimum=0.01,
            ),
            context_retrieval_timeout_seconds=_float(
                get(
                    "MEMORY_CONTEXT_RETRIEVAL_TIMEOUT_SECONDS",
                    "memoryContextRetrievalTimeoutSeconds",
                    12.0,
                ),
                12.0,
            ),
            post_rerank_refill=_bool(
                get("MEMORY_POST_RERANK_REFILL", "memoryPostRerankRefill", True), True
            ),
            source_neighbor_limit=_int(
                get("MEMORY_SOURCE_NEIGHBOR_LIMIT", "memorySourceNeighborLimit", 8), 8
            ),
            adaptive_retrieval=_bool(
                get("MEMORY_ADAPTIVE_RETRIEVAL", "memoryAdaptiveRetrieval", False),
                False,
            ),
            adaptive_min_candidates=_int(
                get(
                    "MEMORY_ADAPTIVE_MIN_CANDIDATES", "memoryAdaptiveMinCandidates", 20
                ),
                20,
            ),
            adaptive_max_candidates=_int(
                get(
                    "MEMORY_ADAPTIVE_MAX_CANDIDATES", "memoryAdaptiveMaxCandidates", 80
                ),
                80,
            ),
            rerank_enabled=_bool(
                get("MEMORY_RERANK_ENABLED", "memoryRerankEnabled", False), False
            ),
            rerank_shadow=_bool(
                get("MEMORY_RERANK_SHADOW", "memoryRerankShadow", False), False
            ),
            rerank_policy=str(
                get("MEMORY_RERANK_POLICY", "memoryRerankPolicy", "adaptive")
            )
            .strip()
            .lower(),
            rerank_shared_coordination=_bool(
                get(
                    "MEMORY_RERANK_SHARED_COORDINATION",
                    "memoryRerankSharedCoordination",
                    True,
                ),
                True,
            ),
            rerank_provider=str(
                get("MEMORY_RERANK_PROVIDER", "memoryRerankProvider", "") or ""
            ).strip(),
            rerank_model=str(
                get("MEMORY_RERANK_MODEL", "memoryRerankModel", "") or ""
            ).strip(),
            rerank_base_url=str(
                get("MEMORY_RERANK_BASE_URL", "memoryRerankBaseUrl", "") or ""
            ).strip(),
            rerank_api_key=str(
                get("MEMORY_RERANK_API_KEY", "memoryRerankApiKey", "") or ""
            ).strip(),
            rerank_candidate_limit=_int(
                get("MEMORY_RERANK_CANDIDATE_LIMIT", "memoryRerankCandidateLimit", 32),
                32,
            ),
            rerank_input_token_budget=_int(
                get(
                    "MEMORY_RERANK_INPUT_TOKEN_BUDGET",
                    "memoryRerankInputTokenBudget",
                    6000,
                ),
                6000,
            ),
            rerank_timeout_seconds=_float(
                get("MEMORY_RERANK_TIMEOUT_SECONDS", "memoryRerankTimeoutSeconds", 8.0),
                8.0,
            ),
            rerank_failure_threshold=_int(
                get(
                    "MEMORY_RERANK_FAILURE_THRESHOLD", "memoryRerankFailureThreshold", 2
                ),
                2,
            ),
            rerank_cooldown_seconds=_float(
                get(
                    "MEMORY_RERANK_COOLDOWN_SECONDS",
                    "memoryRerankCooldownSeconds",
                    60.0,
                ),
                60.0,
            ),
            rerank_cache_ttl_seconds=_float(
                get(
                    "MEMORY_RERANK_CACHE_TTL_SECONDS",
                    "memoryRerankCacheTtlSeconds",
                    120.0,
                ),
                120.0,
            ),
            rerank_min_score=_int(
                get("MEMORY_RERANK_MIN_SCORE", "memoryRerankMinScore", 2), 2
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
        if self.context_rotation_enabled and not self.handoff_notes_enabled:
            raise ValueError("context rotation requires handoff notes to be enabled")
        if self.rerank_policy not in {"adaptive", "always"}:
            raise ValueError("rerank policy must be adaptive or always")
        if not 0 <= self.source_neighbor_limit <= 32:
            raise ValueError("source neighbor limit must be between 0 and 32")
        if (
            not math.isfinite(self.context_retrieval_timeout_seconds)
            or self.context_retrieval_timeout_seconds <= 0
        ):
            raise ValueError("context retrieval deadline must be finite and positive")
        if not 1 <= self.adaptive_min_candidates <= self.adaptive_max_candidates <= 512:
            raise ValueError(
                "adaptive candidate bounds must satisfy 1 <= min <= max <= 512"
            )
        if (
            not 1 <= self.rerank_candidate_limit <= 128
            or self.rerank_input_token_budget < 256
        ):
            raise ValueError(
                "rerank needs 1..128 candidates and at least 256 input tokens"
            )
        if not 0 <= self.rerank_min_score <= 3 or self.rerank_failure_threshold < 1:
            raise ValueError(
                "invalid rerank relevance threshold or circuit failure threshold"
            )
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                self.rerank_timeout_seconds,
                self.rerank_cooldown_seconds,
                self.rerank_cache_ttl_seconds,
            )
        ):
            raise ValueError("rerank time limits must be finite and positive")
        if (
            not math.isfinite(self.retrieval_timeout_seconds)
            or self.retrieval_timeout_seconds <= 0
        ):
            raise ValueError("retrieval timeout must be finite and positive")
        if not 1 <= self.retrieval_refill_multiplier <= 8:
            raise ValueError("retrieval refill multiplier must be between 1 and 8")
        if self.context_card_limit < 1:
            raise ValueError("context card limit must be positive")
        if not 1 <= self.candidate_limit <= 512 or any(
            not 0 <= value <= 512
            for value in (self.lexical_candidate_limit,)
        ):
            raise ValueError("retrieval candidate limits must be at most 512")
        if self.backend != "sqlite":
            raise ValueError("Only SQLite memory is supported; configure a local data root.")
        if not 256 <= self.handoff_note_token_budget <= 4_096:
            raise ValueError("MEMORY_HANDOFF_NOTE_TOKEN_BUDGET must be between 256 and 4096")
        if not 0 < self.context_note_warning_ratio < self.context_rotation_target_ratio < self.context_protection_ratio < 1:
            raise ValueError("context pressure ratios must satisfy 0 < note < rotation < protection < 1")
        if self.context_utilization_enabled and not all((self.context_mode_policy_enabled,
                self.elastic_context_budget_enabled, self.handoff_notes_enabled, self.context_rotation_enabled)):
            raise ValueError("context utilization requires effective mode policy, elastic budgets, handoff notes and rotation")
        if self.tool_result_reclaim_policy_enabled and not self.tool_result_projection_enabled:
            raise ValueError("tool result reclamation requires tool result projection")
        if self.tool_result_reclaim_min_tokens < 1 or not 0 <= self.tool_result_reclaim_min_ratio < 1:
            raise ValueError("reclamation needs positive minimum tokens and 0 <= minimum ratio < 1")
        if not 0 <= self.working_set_context_ratio < self.paged_context_ratio <= 1:
            raise ValueError(
                "memory mode context ratios must satisfy 0 <= working < paged <= 1"
            )
        if self.working_set_step_threshold >= self.paged_step_threshold:
            raise ValueError(
                "working-set step threshold must be lower than paged threshold"
            )
        if self.artifact_store_type != "file":
            raise ValueError("Only local file artifacts are supported; configure ARTIFACT_STORE_ROOT.")

    def safe_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "shadow_write": self.shadow_write,
            "handoff_notes_enabled": self.handoff_notes_enabled,
            "context_rotation_enabled": self.context_rotation_enabled,
            "context_mode_policy_enabled": self.context_mode_policy_enabled,
            "tool_result_projection_enabled": self.tool_result_projection_enabled,
            "tool_result_reclamation": {
                "enabled": self.tool_result_reclaim_policy_enabled,
                "min_tokens": self.tool_result_reclaim_min_tokens,
                "min_ratio": self.tool_result_reclaim_min_ratio,
            },
            "elastic_context_budget_enabled": self.elastic_context_budget_enabled,
            "context_utilization": {
                "enabled": self.context_utilization_enabled,
                "note_warning_ratio": self.context_note_warning_ratio,
                "rotation_target_ratio": self.context_rotation_target_ratio,
                "protection_ratio": self.context_protection_ratio,
                "growth_reserve_tokens": self.context_growth_reserve_tokens,
                "recovery_max_steps": self.context_recovery_max_steps,
            },
            "code_focus_enabled": self.code_focus_enabled,
            "context_section_budgets": {
                "recent_conversation": self.recent_event_token_budget,
                "recent_memory": self.recent_memory_token_budget,
                "working_set": self.working_set_token_budget,
                "supplemental": self.supplemental_token_budget,
            },
            "handoff_note_token_budget": self.handoff_note_token_budget,
            "backend": self.backend,
            "default_mode": self.default_mode.canonical.value,
            "manual_mode": self.manual_mode.canonical.value if self.manual_mode else None,
            "legacy_mode_inputs": [
                name for name, mode in (("default_mode", self.default_mode),
                                        ("manual_mode", self.manual_mode))
                if mode is MemoryMode.WORKING_SET
            ],
            "mode_selection": {
                "automatic": self.auto_mode,
                "paging_context_ratio": self.working_set_context_ratio,
                "paging_step_threshold": self.working_set_step_threshold,
                "rerank_context_ratio": self.paged_context_ratio,
                "rerank_step_threshold": self.paged_step_threshold,
            },
            "artifact_store_type": self.artifact_store_type,
            "retrieval_limits": {
                "lexical": self.lexical_candidate_limit or self.candidate_limit,
                "fused": self.candidate_limit,
                "supplemental_cards": self.context_card_limit,
                "refill_multiplier": self.retrieval_refill_multiplier,
            },
            "retrieval_timeout_seconds": self.retrieval_timeout_seconds,
            "context_retrieval_timeout_seconds": self.context_retrieval_timeout_seconds,
            "post_rerank_refill": self.post_rerank_refill,
            "source_neighbor_limit": self.source_neighbor_limit,
            "adaptive_retrieval": {
                "enabled": self.adaptive_retrieval,
                "min_candidates": self.adaptive_min_candidates,
                "max_candidates": self.adaptive_max_candidates,
            },
            "rerank": {
                "enabled": self.rerank_enabled,
                "shadow": self.rerank_shadow,
                "policy": self.rerank_policy,
                "legacy_local_only": (self.manual_mode is MemoryMode.WORKING_SET
                    or (self.manual_mode is None and not self.auto_mode
                        and self.default_mode is MemoryMode.WORKING_SET)),
                "shared_coordination": self.rerank_shared_coordination,
                "provider": self.rerank_provider,
                "model": self.rerank_model,
                "candidate_limit": self.rerank_candidate_limit,
                "input_token_budget": self.rerank_input_token_budget,
                "timeout_seconds": self.rerank_timeout_seconds,
                "min_score": self.rerank_min_score,
            },
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "retention_rule_count": len(self.retention_rules),
            "archive_max_events": self.archive_max_events,
        }
