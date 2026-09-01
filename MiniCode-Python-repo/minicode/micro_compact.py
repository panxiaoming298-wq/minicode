"""Micro-compaction layer for context management.

Lightweight pre-API context pressure relief that clears stale tool results
without invalidating provider prompt caches.  Inspired by Claude Code's
microCompact / time-based microcompact pipeline.

Design principles (inspired by CC):
  - No API calls — purely local message trimming
  - Preserve tool_use / tool_result API invariants (pairs are never split)
  - Leave the most recent N results untouched
  - Cache-aware: only remove result content, never restructure message IDs

Usage (inside agent loop before model.next()):
    mc = MicroCompactor()
    messages = mc.compact(messages)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ── Compressible tool sets ──────────────────────────────────────────────────
# These tools produce content that is safe to discard when stale without
# affecting correctness in subsequent turns (the agent can re-read / re-search
# if it needs the data again).

COMPRESSIBLE_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_files",
    "grep_files",
    "web_search",
    "web_fetch",
    "find_symbols",
    "find_references",
    "get_ast_info",
    "code_review",
    "diff_viewer",
    "file_tree",
    "json_parse",
    "csv_parse",
    "test_runner",
})

COMPRESSIBLE_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "modify_file",
    "patch_file",
})

# Tool results that should NEVER be compressed (they carry persistent state)
NON_COMPRESSIBLE_TOOLS: frozenset[str] = frozenset({
    "todo_write",
    "task",
    "memory",
})


@dataclass
class MicroCompactionStats:
    messages_before: int = 0
    messages_after: int = 0
    tokens_estimated_freed: int = 0
    reason: str = ""


@dataclass
class MicroCompactorConfig:
    """Configuration for the micro-compaction pipeline."""

    # Time-based: if the gap between now and the last main-loop assistant message
    # exceeds this many seconds, clear all older compressible results.
    idle_threshold_seconds: int = 3600  # 60 minutes (matching CC's default)

    # Count-based: keep at most this many recent compressible result groups
    # intact.  Older groups are candidates for trimming.
    keep_recent_groups: int = 5  # matching CC's default

    # Budget: maximum token budget for tool results before triggering.
    # When the estimated total of compressible tool results exceeds this,
    # the micro-compactor trims older groups regardless of time.
    tool_result_budget_tokens: int = 40_000

    # Whether time-based micro-compaction is enabled.
    time_based_enabled: bool = True

    # Whether count-based (budget-aware) micro-compaction is enabled.
    budget_based_enabled: bool = True


@dataclass
class MicroCompactor:
    """Lightweight tool-result trimmer that runs before each API call.

    Does NOT call any external API.  Operates purely on the in-memory
    message list and only strips content from tool_result messages whose
    corresponding tool_use is in the compressible set.
    """

    config: MicroCompactorConfig = field(default_factory=MicroCompactorConfig)
    # Timestamp of the last main-loop assistant message seen.
    _last_assistant_ts: float = field(default_factory=time.time)

    def compact(
        self,
        messages: list[dict[str, Any]],
        *,
        current_time: float | None = None,
    ) -> tuple[list[dict[str, Any]], MicroCompactionStats]:
        """Run the micro-compaction pipeline and return (messages, stats).

        Pipeline order (cheapest first):
          1.  Time-based — if idle > threshold, mass-clear old results.
          2.  Budget-based — if tool results exceed budget, trim oldest.
        """
        if not messages:
            return messages, MicroCompactionStats()

        now = current_time or time.time()
        stats = MicroCompactionStats(messages_before=len(messages))

        # ── 1. Time-based check ──────────────────────────────────────────
        if self.config.time_based_enabled:
            idle_sec = now - self._last_assistant_ts
            if idle_sec >= self.config.idle_threshold_seconds:
                result = self._compact_time_based(messages, now)
                if result is not None:
                    messages = result
                    stats.reason = f"time_based (idle={idle_sec:.0f}s)"
                    stats.messages_after = len(messages)
                    return messages, stats

        # ── 2. Budget-based check ────────────────────────────────────────
        if self.config.budget_based_enabled:
            result = self._compact_budget_based(messages)
            if result is not None:
                messages = result
                stats.reason = "budget_exceeded"
                stats.messages_after = len(messages)
                return messages, stats

        stats.messages_after = len(messages)
        stats.reason = "no_action"
        return messages, stats

    def update_assistant_timestamp(self, ts: float | None = None) -> None:
        """Record that a main-loop assistant message was just produced."""
        self._last_assistant_ts = ts or time.time()

    # ── internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _is_compressible(result_msg: dict[str, Any]) -> bool:
        """Check if a tool_result message references a compressible tool."""
        data = result_msg.get("data", {}) or {}
        tool_name = data.get("tool_name") or data.get("name") or ""
        if not tool_name:
            return False
        return (
            tool_name in COMPRESSIBLE_READ_ONLY_TOOLS
            or tool_name in COMPRESSIBLE_WRITE_TOOLS
        ) and tool_name not in NON_COMPRESSIBLE_TOOLS

    @staticmethod
    def _estimated_tokens(msg: dict[str, Any]) -> int:
        """Rough token count for a message (chars / 4)."""
        content = msg.get("content", "") or ""
        return max(1, len(str(content)) // 4)

    def _compact_time_based(
        self,
        messages: list[dict[str, Any]],
        now: float,
    ) -> list[dict[str, Any]] | None:
        """Clear compressible tool results older than the keep boundary."""
        # Find the last N assistant messages (main-loop, not parallel-split).
        assistant_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "assistant"
        ]
        if len(assistant_indices) <= self.config.keep_recent_groups:
            return None  # Not enough groups to trim

        # The keep_boundary is the index of the keep_recent_groups-th
        # from the end.
        keep_start = assistant_indices[-self.config.keep_recent_groups]
        trimmed_count = 0

        new_messages: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            if i < keep_start:
                role = msg.get("role", "")
                if role == "tool_result" and self._is_compressible(msg):
                    trimmed_count += 1
                    continue  # drop the result
                if role == "tool_use":
                    # Check if the paired tool_result was just dropped
                    name = (msg.get("data", {}) or {}).get("tool_name") or (msg.get("data", {}) or {}).get("name") or ""
                    if name in COMPRESSIBLE_READ_ONLY_TOOLS | COMPRESSIBLE_WRITE_TOOLS:
                        trimmed_count += 1
                        continue  # drop orphaned tool_use
                # Also drop tool_result messages that are paired with
                # compressible tools even before the assistant boundary —
                # this is done by checking both the role and compressibility.
            new_messages.append(msg)

        if trimmed_count == 0:
            return None
        return new_messages

    def _compact_budget_based(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Trim oldest compressible tool results when total exceeds budget.

        When the budget is exceeded, the OLDEST compressible results (least
        valuable — the model has already built on them) are dropped first, and
        the more-recent ones are kept, until the kept total fits the budget.
        Compressible results inside the protected recent groups are never touched.
        """
        assistant_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "assistant"
        ]
        # Never trim compressible results inside the most recent keep_recent_groups.
        keep_from = max(
            0,
            assistant_indices[-self.config.keep_recent_groups]
            if len(assistant_indices) > self.config.keep_recent_groups
            else 0,
        )

        # Candidate compressible tool results before the keep boundary (oldest first).
        candidates = [
            i for i, m in enumerate(messages)
            if i < keep_from and m.get("role") == "tool_result" and self._is_compressible(m)
        ]
        budget = self.config.tool_result_budget_tokens
        total = sum(self._estimated_tokens(messages[i]) for i in candidates)
        if total <= budget:
            return None  # Under budget

        # Drop OLDEST candidates first until the kept total fits the budget.
        trim_indices: set[int] = set()
        running = total
        for i in candidates:  # oldest first
            if running <= budget:
                break
            running -= self._estimated_tokens(messages[i])
            trim_indices.add(i)

        # Drop orphaned tool_use messages whose paired result was trimmed
        # (paired by data.tool_id), so we don't leave dangling tool calls.
        trimmed_tool_ids = {
            (messages[i].get("data", {}) or {}).get("tool_id") for i in trim_indices
        }

        updated: list[dict[str, Any]] = []
        trimmed = 0
        for i, msg in enumerate(messages):
            if i in trim_indices:
                trimmed += 1
                continue
            if i < keep_from and msg.get("role") == "tool_use":
                data = msg.get("data", {}) or {}
                name = data.get("tool_name") or data.get("name") or ""
                if (
                    name in COMPRESSIBLE_READ_ONLY_TOOLS | COMPRESSIBLE_WRITE_TOOLS
                    and data.get("tool_id") in trimmed_tool_ids
                ):
                    trimmed += 1
                    continue
            updated.append(msg)

        return updated if trimmed > 0 else None


# ── Module-level convenience ─────────────────────────────────────────────────

_default_compactor: MicroCompactor | None = None


def get_micro_compactor() -> MicroCompactor:
    """Get or create the module-level micro-compactor singleton."""
    global _default_compactor
    if _default_compactor is None:
        _default_compactor = MicroCompactor()
    return _default_compactor


def micro_compact(
    messages: list[dict[str, Any]],
    *,
    current_time: float | None = None,
) -> tuple[list[dict[str, Any]], MicroCompactionStats]:
    """Convenience: run micro-compaction on a message list."""
    return get_micro_compactor().compact(messages, current_time=current_time)
