from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.memory import MemoryManager, MemoryScope, MemoryEntry
from minicode.logging_config import get_logger

logger = get_logger("memory_injector")


@dataclass
class InjectedMemory:
    """A memory entry prepared for injection into context."""
    content: str
    category: str
    relevance_score: float
    source: str  # "search", "tag", "category"


class MemoryInjectionMode(str, Enum):
    NONE = "none"
    SUMMARY = "summary"
    STANDARD = "standard"
    STRONG = "strong"


@dataclass
class MemoryInjectionSignal:
    """Observed state for memory-injection control."""

    context_usage: float = 0.0
    retrieval_quality: float = 0.5
    user_correction_count: int = 0
    recent_failure: bool = False
    task_repetition: bool = False
    active_domains: list[str] = field(default_factory=list)


@dataclass
class MemoryInjectionDecision:
    """Controller output for memory injection."""

    mode: MemoryInjectionMode
    max_memories: int
    min_relevance: float
    max_tokens_per_memory: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "max_memories": self.max_memories,
            "min_relevance": round(self.min_relevance, 3),
            "max_tokens_per_memory": self.max_tokens_per_memory,
            "reasons": list(self.reasons),
        }


class MemoryInjectionController:
    """Feedback controller for how much memory to inject."""

    def decide(
        self,
        signal: MemoryInjectionSignal,
        *,
        base_max_memories: int,
        base_min_relevance: float,
        base_max_tokens: int,
    ) -> MemoryInjectionDecision:
        reasons: list[str] = []
        max_memories = base_max_memories
        min_relevance = base_min_relevance
        max_tokens = base_max_tokens
        mode = MemoryInjectionMode.STANDARD

        if signal.context_usage >= 0.90:
            mode = MemoryInjectionMode.NONE
            reasons.append("critical context pressure")
            return MemoryInjectionDecision(mode, 0, 1.0, 0, reasons)

        if signal.context_usage >= 0.75:
            mode = MemoryInjectionMode.SUMMARY
            max_memories = max(1, min(max_memories, 2))
            max_tokens = max(40, min(max_tokens, 80))
            min_relevance = max(min_relevance, 0.55)
            reasons.append("high context pressure")

        if signal.retrieval_quality < 0.35:
            max_memories = max(1, max_memories - 2)
            min_relevance = max(min_relevance, 0.50)
            reasons.append("low retrieval quality")
        elif signal.retrieval_quality >= 0.75 and signal.context_usage < 0.65:
            mode = MemoryInjectionMode.STRONG
            max_memories = min(base_max_memories + 2, max_memories + 2)
            min_relevance = max(0.15, min_relevance - 0.10)
            reasons.append("high retrieval quality")

        if signal.recent_failure:
            mode = MemoryInjectionMode.STRONG if signal.context_usage < 0.75 else mode
            max_memories = min(base_max_memories + 1, max_memories + 1)
            min_relevance = max(0.15, min_relevance - 0.10)
            reasons.append("recent failure recovery")

        if signal.user_correction_count > 0:
            max_memories = max(1, max_memories - signal.user_correction_count)
            min_relevance = min(0.90, min_relevance + 0.10 * signal.user_correction_count)
            reasons.append("user corrections indicate memory risk")

        if signal.task_repetition and signal.context_usage < 0.80:
            max_memories = min(base_max_memories + 1, max_memories + 1)
            reasons.append("repeated task can reuse memory")

        max_memories = max(0, min(base_max_memories + 2, max_memories))
        max_tokens = max(0, max_tokens)
        min_relevance = max(0.0, min(1.0, min_relevance))

        if max_memories == 0:
            mode = MemoryInjectionMode.NONE

        return MemoryInjectionDecision(
            mode=mode,
            max_memories=max_memories,
            min_relevance=min_relevance,
            max_tokens_per_memory=max_tokens,
            reasons=reasons or ["standard memory injection"],
        )


class MemoryInjector:
    """Injects relevant memories into agent context based on task content."""

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        max_injected_memories: int = 5,
        min_relevance: float = 0.3,
        max_tokens_per_memory: int = 200,
        injection_cooldown: float | None = None,
        controller: MemoryInjectionController | None = None,
        reranker: Any | None = None,
    ):
        self._memory = memory_manager
        self._max_injected = max_injected_memories
        self._min_relevance = min_relevance
        self._max_tokens = max_tokens_per_memory
        self._controller = controller or MemoryInjectionController()
        self._reranker = reranker
        self._last_decision: MemoryInjectionDecision | None = None
        self._last_query: str = ""
        self._last_injection_time: float = 0.0
        self._injection_cooldown: float = injection_cooldown if injection_cooldown is not None else 30.0
        self._task_hash: str = ""
        self._cached_result: list[InjectedMemory] = []
        self._last_rerank_result: Any = None

    @staticmethod
    def _hash_task(task_description: str, current_files: tuple[str, ...] | None) -> str:
        """Compute a fast hash for cache key."""
        h = hashlib.md5(task_description.encode(), usedforsecurity=False)
        if current_files:
            for f in current_files:
                h.update(f.encode())
        return h.hexdigest()

    def inject_for_task(
        self,
        task_description: str,
        current_files: list[str] | None = None,
        signal: MemoryInjectionSignal | None = None,
    ) -> list[InjectedMemory]:
        """Search and prepare relevant memories for a task.

        Args:
            task_description: Description of the current task
            current_files: List of files currently being worked on

        Returns:
            List of injected memories sorted by relevance
        """
        if self._memory is None:
            return []

        decision = self._controller.decide(
            signal or MemoryInjectionSignal(),
            base_max_memories=self._max_injected,
            base_min_relevance=self._min_relevance,
            base_max_tokens=self._max_tokens,
        )
        self._last_decision = decision
        if decision.mode == MemoryInjectionMode.NONE:
            return []

        # Cooldown check - don't inject too frequently
        task_hash = self._hash_task(task_description, tuple(current_files) if current_files else None)
        if time.time() - self._last_injection_time < self._injection_cooldown:
            if task_description == self._last_query:
                return []  # Same query within cooldown, skip

        # Cache check: return cached result for identical tasks (after cooldown)
        if task_hash == self._task_hash and self._cached_result:
            return self._cached_result.copy()

        self._last_query = task_description
        self._last_injection_time = time.time()

        memories: list[tuple[float, MemoryEntry, str]] = []

        # Derive active domains if not already in signal
        active_domains = (signal.active_domains if signal else []) or []
        if not active_domains and current_files:
            try:
                from minicode.domain_classifier import get_active_domain_values
                active_domains = get_active_domain_values(
                    current_files=current_files,
                    intent_text=task_description,
                )
            except Exception:
                pass

        # Search across all scopes with domain-aware boosting
        for scope in MemoryScope:
            results = self._memory.search(
                task_description,
                scope=scope,
                limit=decision.max_memories * 2,
                min_relevance=decision.min_relevance,
                active_domains=active_domains if active_domains else None,
            )
            for entry in results:
                # Calculate composite relevance
                relevance = self._calculate_relevance(entry, task_description, current_files)
                memories.append((relevance, entry, scope.value))

        # Sort by relevance and take top candidates for reranking
        memories.sort(key=lambda x: x[0], reverse=True)
        top_entries = [entry for _, entry, _ in memories[:15]]

        # N1: LLM Reranker — curate BM25 results for precision
        rerank_selected_ids: set[str] | None = None
        rerank_summary: str = ""
        if self._reranker and hasattr(self._reranker, 'enabled') and self._reranker.enabled and top_entries:
            try:
                rerank_result = self._reranker.curate(
                    candidates=top_entries,
                    task_description=task_description,
                    active_domains=active_domains if active_domains else None,
                    current_files=current_files,
                )
                self._last_rerank_result = rerank_result
                rerank_selected_ids = set(rerank_result.selected_ids)
                rerank_summary = rerank_result.summary
                if rerank_result.conflicts:
                    logger.info(
                        "Reranker: %d selected, %d conflicts, cache_hit=%.0f%%",
                        len(rerank_selected_ids),
                        len(rerank_result.conflicts),
                        self._reranker.cache_hit_rate * 100,
                    )
            except Exception:
                pass  # Fallback to BM25 on any error

        injected: list[InjectedMemory] = []
        seen_content: set[str] = set()

        # Filter memories: use reranker selection if available, else top N
        for relevance, entry, scope_name in memories[:decision.max_memories * 2]:
            if rerank_selected_ids is not None and entry.id not in rerank_selected_ids:
                continue
            content = entry.content[:decision.max_tokens_per_memory * 4]  # Rough char limit
            content_key = content[:100].lower()

            if content_key in seen_content:
                continue
            seen_content.add(content_key)

            injected.append(InjectedMemory(
                content=content,
                category=entry.category,
                relevance_score=relevance,
                source=f"{scope_name}_search",
            ))

        # Inject reranker summary as a special memory entry if available
        if rerank_summary and injected:
            injected.insert(0, InjectedMemory(
                content=f"[AI Curator Summary]\n{rerank_summary}",
                category="curated_context",
                relevance_score=1.0,
                source="reranker",
            ))

        # Also search by tags if task has code-related keywords
        tag_memories = self._inject_by_tags(task_description, decision)
        for mem in tag_memories:
            content_key = mem.content[:100].lower()
            if content_key not in seen_content and len(injected) < decision.max_memories:
                seen_content.add(content_key)
                injected.append(mem)

        logger.info(
            "Injected %d memories for task: %s",
            len(injected),
            task_description[:50],
        )

        self._task_hash = task_hash
        self._cached_result = injected.copy()
        return injected

    def inject_on_failure(
        self,
        error_message: str,
        tool_name: str,
        signal: MemoryInjectionSignal | None = None,
    ) -> list[InjectedMemory]:
        """Search for similar past failures and solutions.

        Args:
            error_message: The error message from the failed tool
            tool_name: Name of the tool that failed

        Returns:
            List of relevant memories that might contain solutions
        """
        if self._memory is None:
            return []

        control_signal = signal or MemoryInjectionSignal(recent_failure=True, retrieval_quality=0.6)
        decision = self._controller.decide(
            control_signal,
            base_max_memories=self._max_injected,
            base_min_relevance=0.2,
            base_max_tokens=self._max_tokens,
        )
        self._last_decision = decision
        if decision.mode == MemoryInjectionMode.NONE:
            return []

        # Search for memories related to this error and tool
        query = f"{tool_name} {error_message[:100]}"

        memories: list[tuple[float, MemoryEntry, str]] = []

        for scope in MemoryScope:
            results = self._memory.search(
                query,
                scope=scope,
                limit=decision.max_memories,
                min_relevance=decision.min_relevance,
            )
            for entry in results:
                # Boost memories in "testing" or "decision" categories
                relevance = 0.5  # Base relevance for failure context
                if entry.category in ["testing", "decision", "code-pattern"]:
                    relevance += 0.2
                if tool_name in entry.content.lower():
                    relevance += 0.15
                memories.append((relevance, entry, scope.value))

        memories.sort(key=lambda x: x[0], reverse=True)

        injected: list[InjectedMemory] = []
        for relevance, entry, scope_name in memories[:decision.max_memories]:
            injected.append(InjectedMemory(
                content=entry.content[:decision.max_tokens_per_memory * 4],
                category=entry.category,
                relevance_score=relevance,
                source=f"{scope_name}_failure_recovery",
            ))

        if injected:
            logger.info(
                "Injected %d recovery memories for %s failure",
                len(injected),
                tool_name,
            )

        return injected

    @property
    def last_decision(self) -> MemoryInjectionDecision | None:
        return self._last_decision

    def format_for_prompt(self, memories: list[InjectedMemory]) -> str:
        """Format injected memories for inclusion in system prompt.

        Args:
            memories: List of memories to format

        Returns:
            Formatted string for prompt injection
        """
        if not memories:
            return ""

        lines = ["## Relevant Context from Memory", ""]

        for i, mem in enumerate(memories, 1):
            lines.append(f"{i}. [{mem.category}] {mem.content}")

        lines.append("")
        lines.append("Use the above context to inform your decisions.")

        return "\n".join(lines)

    def _calculate_relevance(
        self,
        entry: MemoryEntry,
        task_description: str,
        current_files: list[str] | None,
    ) -> float:
        """Calculate composite relevance score for a memory entry."""
        score = 0.5  # Base score

        # Boost if memory category matches task type
        task_lower = task_description.lower()
        if entry.category == "architecture" and any(kw in task_lower for kw in ["design", "structure", "api"]):
            score += 0.2
        elif entry.category == "testing" and any(kw in task_lower for kw in ["test", "assert", "verify"]):
            score += 0.2
        elif entry.category == "convention" and any(kw in task_lower for kw in ["style", "naming", "format"]):
            score += 0.2

        # Boost if memory mentions current files
        if current_files:
            entry_lower = entry.content.lower()
            for file_path in current_files:
                file_name = file_path.split("/")[-1].split("\\")[-1]
                if file_name.lower() in entry_lower:
                    score += 0.15

        # Boost recent memories
        age_hours = (time.time() - entry.updated_at) / 3600
        if age_hours < 24:
            score += 0.1
        elif age_hours < 168:  # 1 week
            score += 0.05

        return min(1.0, score)

    def _inject_by_tags(
        self,
        task_description: str,
        decision: MemoryInjectionDecision,
    ) -> list[InjectedMemory]:
        """Find memories by matching tags to task keywords."""
        if self._memory is None:
            return []

        # Extract potential tags from task description
        task_lower = task_description.lower()
        keywords = []

        # Common code-related keywords
        code_keywords = [
            "api", "test", "function", "class", "database", "config",
            "security", "performance", "git", "docker", "deploy",
        ]
        for kw in code_keywords:
            if kw in task_lower:
                keywords.append(kw)

        memories: list[InjectedMemory] = []
        seen: set[str] = set()

        for keyword in keywords[:3]:  # Limit to top 3 keywords
            for scope in MemoryScope:
                tagged = self._memory.search_by_tag(scope, keyword)
                for entry in tagged:
                    content_key = entry.content[:100].lower()
                    if content_key not in seen:
                        seen.add(content_key)
                        memories.append(InjectedMemory(
                            content=entry.content[:decision.max_tokens_per_memory * 4],
                            category=entry.category,
                            relevance_score=0.6,  # Tag matches are fairly relevant
                            source=f"{scope.value}_tag",
                        ))

        return memories[:decision.max_memories]
