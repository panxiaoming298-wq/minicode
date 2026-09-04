"""Tiered context and lossless out-of-window memory.

The package keeps authoritative records separate from derived retrieval data.
It is intentionally optional at import time: local SQLite/file adapters work
without third-party dependencies, while PostgreSQL, S3 and Milvus adapters are
loaded only when selected by configuration.
"""

from .archive import EventArchiveManager
from .config import PagedMemoryConfig
from .models import (
    ArchiveManifest,
    ArchiveStatus,
    ArtifactRecord,
    ArtifactStatus,
    ContextItem,
    ContextPacket,
    EventType,
    MemoryMode,
    MemoryStatus,
    PreflightAuditResult,
    StorageHealth,
    TaskMetrics,
    TaskStateSnapshot,
    ToolResultEnvelope,
)
from .service import (
    ArtifactPersistenceRequired,
    AuthorityUnavailableError,
    ContextBudgetExceeded,
    PagedMemoryRuntime,
    PreflightBlockedError,
)

__all__ = [
    "ArchiveManifest",
    "ArchiveStatus",
    "ArtifactPersistenceRequired",
    "ArtifactRecord",
    "ArtifactStatus",
    "AuthorityUnavailableError",
    "ContextBudgetExceeded",
    "ContextItem",
    "ContextPacket",
    "EventArchiveManager",
    "EventType",
    "MemoryMode",
    "MemoryStatus",
    "PagedMemoryConfig",
    "PagedMemoryRuntime",
    "PreflightAuditResult",
    "PreflightBlockedError",
    "StorageHealth",
    "TaskMetrics",
    "TaskStateSnapshot",
    "ToolResultEnvelope",
]
