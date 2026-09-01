"""Memory Reranker — LLM-based curation of BM25 search results.

Phase N1 of memory optimization. After BM25 retrieves top-15 candidates,
the Reranker calls a lightweight LLM to:

1. Select the 3-5 most relevant memories for the current task
2. Detect contradictory memory pairs
3. Generate a 2-3 sentence context summary

Design principles:
- Lightweight: uses Haiku-level model, ~$0.0005/query
- Cached: LRU cache (task + memory IDs → result), 60s TTL
- Fallback: any failure → original BM25 results unchanged
- JSON output: strict format with robust parsing

Architecture:
  BM25 top-15 → Reranker.curate() → top 3-5 + summary + conflicts
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("memory_reranker")

# ── Data types ─────────────────────────────────────────────────────

@dataclass
class RerankCandidate:
    """A memory candidate to be evaluated by the reranker."""
    id: str
    content: str  # Truncated to 200 chars for prompt efficiency
    domain: str = ""
    tags: str = ""
    usage: int = 0


@dataclass
class RerankResult:
    """Output of the reranker."""
    selected_ids: list[str]
    rejected: list[dict[str, str]]  # [{id, reason}]
    conflicts: list[dict[str, str]]  # [{a, b, desc}]
    summary: str  # 2-3 sentence context summary
    confidence: float = 0.7
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def empty(cls) -> "RerankResult":
        return cls(selected_ids=[], rejected=[], conflicts=[], summary="")

    @classmethod
    def fallback(cls, candidate_ids: list[str]) -> "RerankResult":
        """Fallback result: take top candidates as-is."""
        n = min(5, len(candidate_ids))
        return cls(
            selected_ids=candidate_ids[:n],
            rejected=[],
            conflicts=[],
            summary="",
            confidence=0.3,
        )


# ── Prompt template ─────────────────────────────────────────────────

RERANK_PROMPT = """You are a code memory curator. Your job: filter BM25 search results to keep ONLY truly relevant memories.

Current task: {task_description}
Active domains: {active_domains}
Recently edited files: {current_files}

Candidate memories (from BM25 search, may contain cross-domain noise):
{candidates_formatted}

Instructions:
1. Select 3-5 memories MOST relevant to the current task
   - PRIORITY 1: Same domain as active_domains
   - PRIORITY 2: Closely related domain
   - DO NOT select memories from unrelated domains unless they are genuinely relevant
2. For every memory you reject, explain WHY (e.g., "backend memory, current task is frontend")
3. Mark contradictory pairs (e.g., "use Redux" vs "migrated to Zustand")
4. Write a 2-3 sentence summary of key context for this task

CRITICAL: If active domains are [frontend] and a memory is about database migrations, REJECT it.
If active domains are [database] and a memory is about React components, REJECT it.

Return ONLY valid JSON:
{{
  "selected": ["id1", "id3"],
  "rejected": [{{"id": "id2", "reason": "backend: FastAPI endpoint, current task is frontend"}}],
  "conflicts": [],
  "summary": "..."
}}"""


# ── Reranker ────────────────────────────────────────────────────────

class MemoryReranker:
    """LLM-based curator for BM25 search results.

    Usage:
        reranker = MemoryReranker(model_adapter)
        result = reranker.curate(candidates, task_description, ...)
        # Filter original memories by result.selected_ids
    """

    def __init__(
        self,
        model_adapter: Any | None = None,
        max_candidates: int = 15,
        max_content_len: int = 200,
        cache_size: int = 256,
        cache_ttl: float = 60.0,
    ):
        """
        Args:
            model_adapter: Any callable with a `generate(prompt) -> str` method.
                           If None, reranker acts as pass-through.
            max_candidates: Max BM25 results to feed into the prompt.
            max_content_len: Truncate each memory content to this length.
            cache_size: LRU cache entries.
            cache_ttl: Cache lifetime in seconds.
        """
        self._model = model_adapter
        self._max_candidates = max_candidates
        self._max_content_len = max_content_len
        self._cache_ttl = cache_ttl
        self._enabled = model_adapter is not None
        self._call_count = 0
        self._cache_hits = 0
        self._fallback_count = 0

        # Internal LRU cache
        self._cache: dict[str, tuple[float, RerankResult]] = {}
        self._cache_max = cache_size

    # ── Public API ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def cache_hit_rate(self) -> float:
        total = self._call_count + self._cache_hits
        return self._cache_hits / total if total > 0 else 0.0

    def curate(
        self,
        candidates: list[Any],
        task_description: str,
        active_domains: list[str] | None = None,
        current_files: list[str] | None = None,
    ) -> RerankResult:
        """Curate BM25 results via LLM.

        Args:
            candidates: MemoryEntry objects from BM25 search (top N).
            task_description: Current task text.
            active_domains: Currently active domain context.
            current_files: Files being edited.

        Returns:
            RerankResult with selected IDs, conflicts, and summary.
            On any failure, returns fallback with top candidates.
        """
        if not self._enabled or not candidates:
            return RerankResult.fallback(
                [getattr(c, 'id', '') for c in candidates[:5]]
            )

        # Build cache key
        cache_key = self._build_cache_key(candidates, task_description)

        # Check cache
        if cache_key in self._cache:
            ts, result = self._cache[cache_key]
            if time.time() - ts < self._cache_ttl:
                self._cache_hits += 1
                return result
            del self._cache[cache_key]

        self._call_count += 1

        try:
            result = self._call_llm(
                candidates, task_description, active_domains or [], current_files or []
            )
            self._add_to_cache(cache_key, result)
            return result
        except Exception as e:
            logger.warning("MemoryReranker: LLM call failed, fallback to BM25: %s", e)
            self._fallback_count += 1
            return RerankResult.fallback(
                [getattr(c, 'id', '') for c in candidates[:5]]
            )

    # ── Internal ─────────────────────────────────────────────────

    def _build_cache_key(self, candidates: list[Any], task: str) -> str:
        ids = sorted([getattr(c, 'id', '') for c in candidates])
        key = hashlib.md5(
            (task[:100] + "|" + "|".join(ids[:15])).encode(),
            usedforsecurity=False,
        ).hexdigest()
        return key

    def _add_to_cache(self, key: str, result: RerankResult) -> None:
        if len(self._cache) >= self._cache_max:
            # Evict oldest entry
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[key] = (time.time(), result)

    def _call_llm(
        self,
        candidates: list[Any],
        task_description: str,
        active_domains: list[str],
        current_files: list[str],
    ) -> RerankResult:
        """Build prompt, call LLM, parse result."""

        # Build candidate list for prompt
        candidate_items: list[RerankCandidate] = []
        for c in candidates[:self._max_candidates]:
            content = getattr(c, 'content', '')
            candidate_items.append(RerankCandidate(
                id=getattr(c, 'id', ''),
                content=content[:self._max_content_len].replace('\n', ' '),
                domain=", ".join(getattr(c, 'domains', [])) if hasattr(c, 'domains') else "",
                tags=", ".join(getattr(c, 'tags', [])[:5]) if hasattr(c, 'tags') else "",
                usage=getattr(c, 'usage_count', 0),
            ))

        # Format candidates for prompt
        candidates_fmt = "\n".join(
            f"  [{c.id}] domains={c.domain} tags={c.tags} used={c.usage}x\n    {c.content}"
            for c in candidate_items
        )

        # Build prompt
        prompt = RERANK_PROMPT.format(
            task_description=task_description[:300],
            active_domains=", ".join(active_domains) if active_domains else "unknown",
            current_files=", ".join(current_files[:10]) if current_files else "unknown",
            candidates_formatted=candidates_fmt,
        )

        # Call model — supports both `generate()` and `next()` interfaces
        response_text = ""
        if hasattr(self._model, 'generate'):
            raw = self._model.generate(prompt)
            if isinstance(raw, dict):
                response_text = raw.get("content", "") or raw.get("text", "")
            else:
                response_text = str(raw)
        elif hasattr(self._model, 'next'):
            # Save and clear thinking blocks so the reranker doesn't interfere
            # with the main agent loop's thinking round-trip
            saved_thinking = getattr(self._model, '_thinking_blocks', None)
            if saved_thinking is not None:
                self._model._thinking_blocks = []
            msgs = [{"role": "user", "content": prompt}]
            step = self._model.next(msgs)
            response_text = getattr(step, 'content', '') or str(step)
            # Clear thinking blocks from reranker call — they belong to a
            # different conversation context
            if hasattr(self._model, '_thinking_blocks'):
                self._model._thinking_blocks = []
        else:
            raise RuntimeError("Model adapter must have generate() or next() method")

        return self._parse_response(response_text, candidate_items)

    def _parse_response(
        self, text: str, candidates: list[RerankCandidate]
    ) -> RerankResult:
        """Extract JSON from LLM response, with robust fallback."""
        # Try to extract JSON from the response (may have markdown wrappers)
        json_text = text.strip()
        if json_text.startswith("```"):
            # Strip markdown code fences
            lines = json_text.split("\n")
            json_text = "\n".join(lines[1:]) if lines[0].startswith("```") else json_text
            if json_text.endswith("```"):
                json_text = json_text[:-3].strip()

        # Find JSON object boundaries
        start = json_text.find("{")
        end = json_text.rfind("}")
        if start >= 0 and end > start:
            json_text = json_text[start:end + 1]

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            logger.debug("Reranker JSON parse failed, using fallback. Raw: %.100s", text)
            return RerankResult.fallback([c.id for c in candidates])

        # Validate selected IDs exist in candidates
        valid_ids = {c.id for c in candidates}
        selected = [sid for sid in data.get("selected", []) if sid in valid_ids]

        if not selected:
            # If no valid selections, take first 3 candidates
            selected = [c.id for c in candidates[:3]]

        return RerankResult(
            selected_ids=selected,
            rejected=data.get("rejected", []),
            conflicts=data.get("conflicts", []),
            summary=data.get("summary", ""),
            confidence=0.7 if selected else 0.3,
        )


# ── Convenience factory ─────────────────────────────────────────────

def create_reranker(model: Any | None = None, **kwargs: Any) -> MemoryReranker:
    """Create a MemoryReranker, auto-detecting suitable model if none provided."""
    if model is not None:
        return MemoryReranker(model_adapter=model, **kwargs)

    # Try to create a lightweight model for reranking
    try:
        from minicode.model_registry import create_model_adapter
        adapter = create_model_adapter(
            model="claude-haiku-3-5-20241022",
            tools=None,
            runtime={"model": "claude-haiku-3-5-20241022"},
        )
        return MemoryReranker(model_adapter=adapter, **kwargs)
    except Exception:
        logger.info("MemoryReranker: no model available, running in pass-through mode")
        return MemoryReranker(model_adapter=None, **kwargs)
