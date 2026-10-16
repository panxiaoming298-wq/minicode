"""Bound network research without revoking the registered tools."""

from __future__ import annotations

import threading

from minicode.tooling import ToolResult


NETWORK_RESEARCH_TOOLS = frozenset({"web_search", "web_fetch"})


class NetworkResearchBudget:
    """Reserve source-reading calls and admit concurrent calls atomically."""

    def __init__(self, limit: int, *, reserve_fetches: bool = False) -> None:
        self.limit = limit
        self.search_limit = max(1, limit // 2) if reserve_fetches else limit
        self._lock = threading.Lock()
        self._count = 0
        self._search_count = 0
        self._admitted: set[tuple[str, str]] = set()
        self._stop_reason = ""

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def search_limit_reached(self) -> bool:
        with self._lock:
            return self._search_count >= self.search_limit

    def stop(self, reason: str) -> None:
        with self._lock:
            self._stop_reason = reason

    def admit(self, tool_name: str, call_id: str = "") -> ToolResult | None:
        if tool_name not in NETWORK_RESEARCH_TOOLS:
            return None
        with self._lock:
            # Native transport retries belong to the same admitted tool call.
            identity = (tool_name, call_id)
            if call_id and identity in self._admitted:
                return None
            if self._stop_reason or self._count >= self.limit:
                reason = self._stop_reason or f"the per-turn network research limit ({self.limit} calls) was reached"
                guidance = "Answer from the collected evidence and state any gaps."
            elif tool_name == "web_search" and self._search_count >= self.search_limit:
                reason = f"the search allocation ({self.search_limit} calls) was reached"
                guidance = "web_fetch remains available to read and verify the collected source URLs."
            else:
                self._count += 1
                self._search_count += tool_name == "web_search"
                if call_id:
                    self._admitted.add(identity)
                return None
            return ToolResult(
                ok=False,
                inputRejected=True,
                failureKind="policy_denied",
                output=(
                    f"NetworkResearchLimit: {tool_name} is registered, but {reason}. "
                    f"No network request was sent. {guidance} "
                    "This is a limit for this turn, not a missing tool or disabled network access."
                ),
            )
