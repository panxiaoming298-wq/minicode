"""Working memory protection for context compaction.

Inspired by Learn Claude Code best practices:
- Preserve key continuity information during context compression
- Protect active task context from being summarized away
- Maintain conversation flow continuity across compaction boundaries

Provides:
- WorkingMemoryTracker: Tracks and protects critical context
- ContinuityMarker: Marks important conversation flow points
- MemoryBudgetAllocator: Allocates token budget for working memory
"""

from __future__ import annotations

import time
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from copy import deepcopy
from typing import Any

from minicode.context_manager import estimate_tokens


@dataclass
class WorkingMemoryEntry:
    """A single working memory entry that should be protected during compaction."""

    content: str
    entry_type: str  # "active_task", "user_intent", "key_decision", "error_context"
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = no expiry
    importance: float = 1.0  # 0.0 - 1.0, higher = more protected

    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def token_count(self) -> int:
        """Estimate token count for this entry."""
        return estimate_tokens(self.content)


class WorkingMemoryTracker:
    """Tracks and protects critical context during compaction.

    This implements the "working memory protection" pattern from
    Learn Claude Code best practices. During context compression,
    entries in this tracker are preserved to maintain conversation
    continuity and task coherence.
    """

    def __init__(
        self,
        max_entries: int = 15,
        max_tokens: int = 4000,
    ) -> None:
        self._entries: list[WorkingMemoryEntry] = []
        self.max_entries = max_entries
        self.max_tokens = max_tokens
        # One explicit current-task slot, separate from best-effort/TTL notes.
        # The Runtime consumes this data through its own native receipt gate;
        # it is not automatically injected into StableTaskPack or granted trust.
        self._task_context: dict[str, Any] | None = None

    def task_context(self) -> dict[str, Any] | None:
        return deepcopy(self._task_context)

    def set_task_context(self, value: dict[str, Any] | None) -> None:
        """Bounded, explicit replacement; ordinary note eviction cannot erase it.

        This stores data only. Restoring the same JSON never restores native
        tool observations, delivery/reading credit or permission to project it.
        """
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError("task working context must be an object")
            if len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > 16000:
                raise ValueError("task working context exceeds 16000 UTF-8 bytes; existing context was retained")
        self._task_context = deepcopy(value)

    def add(
        self,
        content: str,
        entry_type: str = "active_task",
        ttl_seconds: float | None = None,
        importance: float = 1.0,
    ) -> WorkingMemoryEntry:
        """Add a working memory entry to be protected.

        Args:
            content: The content to protect
            entry_type: Type of working memory (active_task, user_intent, etc.)
            ttl_seconds: Time-to-live in seconds (None = no expiry)
            importance: Importance score 0.0-1.0 (higher = more protected)
        """
        expires_at = None
        if ttl_seconds is not None:
            expires_at = time.time() + ttl_seconds

        entry = WorkingMemoryEntry(
            content=content,
            entry_type=entry_type,
            expires_at=expires_at,
            importance=importance,
        )

        self._entries.append(entry)
        self._enforce_limits()
        return entry

    def remove(self, entry: WorkingMemoryEntry) -> None:
        """Remove a working memory entry."""
        if entry in self._entries:
            self._entries.remove(entry)

    def clear_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        before = len(self._entries)
        self._entries = [e for e in self._entries if not e.is_expired()]
        return before - len(self._entries)

    def get_protected_content(self) -> list[str]:
        """Get all non-expired content that should be protected."""
        self.clear_expired()
        return [e.content for e in self._entries]

    def get_protected_tokens(self) -> int:
        """Get total token count of protected content."""
        return sum(e.token_count() for e in self._entries if not e.is_expired())

    def get_stats(self) -> dict[str, Any]:
        """Get working memory statistics."""
        self.clear_expired()
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "protected_tokens": self.get_protected_tokens(),
            "max_tokens": self.max_tokens,
            "utilization": self.get_protected_tokens() / self.max_tokens
            if self.max_tokens > 0
            else 0,
        }

    def _enforce_limits(self) -> None:
        """Remove lowest-priority entries if exceeding limits."""
        # Remove expired first
        self.clear_expired()

        # Remove by token budget
        while self.get_protected_tokens() > self.max_tokens and self._entries:
            # Remove lowest importance entry
            self._entries.sort(key=lambda e: e.importance)
            self._entries.pop(0)

        # Remove by entry count
        while len(self._entries) > self.max_entries and self._entries:
            self._entries.sort(key=lambda e: e.importance)
            self._entries.pop(0)

    def format_status(self) -> str:
        """Format working memory status for display."""
        stats = self.get_stats()
        lines = [
            "Working Memory",
            "=" * 50,
            f"Entries: {stats['entries']}/{stats['max_entries']}",
            f"Protected tokens: {stats['protected_tokens']:,}/{stats['max_tokens']:,} ({stats['utilization']*100:.0f}%)",
            "",
        ]

        if self._entries:
            lines.append("Protected Content:")
            for entry in self._entries:
                expires = ""
                if entry.expires_at:
                    remaining = entry.expires_at - time.time()
                    if remaining > 0:
                        expires = f" (expires in {remaining/60:.0f}m)"
                    else:
                        expires = " (EXPIRED)"
                preview = entry.content[:60].replace("\n", " ")
                lines.append(f"  • [{entry.entry_type}] {preview}...{expires}")

        return "\n".join(lines)


@dataclass
class ContinuityMarker:
    """Marks important conversation flow points.

    During compaction, these markers help reconstruct the
    conversation narrative even after messages are summarized.
    """

    marker_type: str  # "task_start", "decision_point", "error_recovered", "user_redirect"
    description: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class ConversationContinuityManager:
    """Manages conversation continuity across compaction boundaries.

    When context is compacted, this manager helps reconstruct the
    conversation flow by preserving key transition points.
    """

    def __init__(self, max_markers: int = 20) -> None:
        self._markers: list[ContinuityMarker] = []
        self.max_markers = max_markers

    def add_marker(
        self,
        marker_type: str,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> ContinuityMarker:
        """Add a continuity marker."""
        marker = ContinuityMarker(
            marker_type=marker_type,
            description=description,
            metadata=metadata or {},
        )
        self._markers.append(marker)

        # Enforce limit
        if len(self._markers) > self.max_markers:
            self._markers = self._markers[-self.max_markers:]

        return marker

    def get_recent_markers(self, limit: int = 10) -> list[ContinuityMarker]:
        """Get recent continuity markers."""
        return self._markers[-limit:]

    def get_markers_since(self, timestamp: float) -> list[ContinuityMarker]:
        """Get markers added after a specific timestamp."""
        return [m for m in self._markers if m.timestamp > timestamp]

    def format_continuity_summary(self) -> str:
        """Format conversation continuity for display."""
        if not self._markers:
            return "No continuity markers."

        lines = ["Conversation Continuity", "=" * 50, ""]
        for marker in self._markers[-10:]:  # Last 10 markers
            time_str = time.strftime("%H:%M:%S", time.localtime(marker.timestamp))
            lines.append(f"  [{time_str}] [{marker.marker_type}] {marker.description}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_working_memory = WorkingMemoryTracker()
_continuity_manager = ConversationContinuityManager()
_worker_memory: ContextVar[tuple[WorkingMemoryTracker, ConversationContinuityManager] | None] = ContextVar(
    "worker_working_memory", default=None,
)


@contextmanager
def isolated_working_memory() -> Iterator[None]:
    """Keep short-term state local to a Worker, including nested delegation."""
    token = _worker_memory.set((WorkingMemoryTracker(), ConversationContinuityManager()))
    try:
        yield
    finally:
        _worker_memory.reset(token)


def get_working_memory() -> WorkingMemoryTracker:
    """Get this Worker's tracker, or the legacy tracker outside a Worker."""
    scoped = _worker_memory.get()
    return scoped[0] if scoped is not None else _working_memory


def get_continuity_manager() -> ConversationContinuityManager:
    """Get this Worker's continuity manager, or the legacy standalone manager."""
    scoped = _worker_memory.get()
    return scoped[1] if scoped is not None else _continuity_manager


def snapshot_working_state() -> dict[str, Any]:
    """Lossless short-term context; kept private in completed-turn snapshots."""
    tracker, continuity = get_working_memory(), get_continuity_manager()
    return {"entries": [asdict(entry) for entry in tracker._entries],
            "markers": [asdict(marker) for marker in continuity._markers],
            "max_entries": tracker.max_entries, "max_tokens": tracker.max_tokens,
            "max_markers": continuity.max_markers,
            **({"task_context": tracker.task_context()} if tracker.task_context() is not None else {})}


def restore_working_state(value: dict[str, Any]) -> None:
    """Restore data only, inside the new Worker's independent context scope."""
    tracker, continuity = get_working_memory(), get_continuity_manager()
    tracker._entries = [WorkingMemoryEntry(**deepcopy(entry)) for entry in value.get("entries", [])]
    continuity._markers = [ContinuityMarker(**deepcopy(marker)) for marker in value.get("markers", [])]
    tracker.max_entries = int(value.get("max_entries", tracker.max_entries))
    tracker.max_tokens = int(value.get("max_tokens", tracker.max_tokens))
    continuity.max_markers = int(value.get("max_markers", continuity.max_markers))
    tracker.set_task_context(value.get("task_context"))


def protect_context(
    content: str,
    entry_type: str = "active_task",
    ttl_seconds: float | None = None,
    importance: float = 1.0,
) -> WorkingMemoryEntry:
    """Convenience function to protect context during compaction."""
    return get_working_memory().add(
        content,
        entry_type,
        ttl_seconds,
        importance=importance,
    )


def mark_continuity(
    marker_type: str,
    description: str,
    metadata: dict[str, Any] | None = None,
) -> ContinuityMarker:
    """Convenience function to add a continuity marker."""
    return get_continuity_manager().add_marker(marker_type, description, metadata)
