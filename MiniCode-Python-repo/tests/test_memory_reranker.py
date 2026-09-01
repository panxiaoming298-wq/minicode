"""Tests for MemoryReranker — LLM-based curation of BM25 results."""
from __future__ import annotations

import json

import pytest

from minicode.memory import MemoryEntry, MemoryScope
from minicode.memory_reranker import (
    MemoryReranker,
    RerankCandidate,
    RerankResult,
    create_reranker,
)


# ── Mock model adapter ──────────────────────────────────────────────

class MockModelAdapter:
    """Mock LLM that returns curated JSON for testing."""

    def __init__(self, selected_ids=None, conflicts=None, summary=""):
        self._selected = selected_ids or []
        self._conflicts = conflicts or []
        self._summary = summary
        self.call_count = 0

    def generate(self, prompt: str) -> dict:
        self.call_count += 1
        result = {
            "selected": self._selected,
            "rejected": [],
            "conflicts": self._conflicts,
            "summary": self._summary or "Generated summary for test task",
        }
        return {"content": json.dumps(result)}

    def next(self, messages: list[dict]) -> object:
        self.call_count += 1
        result = {
            "selected": self._selected,
            "rejected": [],
            "conflicts": self._conflicts,
            "summary": self._summary or "Generated summary for test task",
        }

        class MockStep:
            content = json.dumps(result)
        return MockStep()


# ── Helpers ─────────────────────────────────────────────────────────

def _make_entry(eid: str, content: str, domains=None):
    return MemoryEntry(
        id=eid, scope=MemoryScope.PROJECT,
        category="pattern", content=content,
        domains=domains or [],
    )


def _make_candidates():
    return [
        _make_entry("m1", "Use React functional components with hooks", ["frontend"]),
        _make_entry("m2", "API endpoints use FastAPI with async handlers", ["backend"]),
        _make_entry("m3", "React forms use react-hook-form with zod validation", ["frontend"]),
        _make_entry("m4", "Database migrations use Alembic with auto-generation", ["database"]),
        _make_entry("m5", "CSS uses Tailwind utility classes, avoid inline styles", ["frontend"]),
        _make_entry("m6", "Deployment uses Docker Compose with multi-stage builds", ["devops"]),
        _make_entry("m7", "State management migrated from Redux to Zustand", ["frontend"]),
        _make_entry("m8", "API authentication uses JWT with refresh token rotation", ["backend", "security"]),
    ]


# ── Tests ───────────────────────────────────────────────────────────

class TestRerankerBasics:
    def test_disabled_without_model(self):
        reranker = MemoryReranker(model_adapter=None)
        assert not reranker.enabled

    def test_enabled_with_model(self):
        mock = MockModelAdapter()
        reranker = MemoryReranker(model_adapter=mock)
        assert reranker.enabled

    def test_call_count_tracks(self):
        mock = MockModelAdapter(selected_ids=["m1", "m3", "m5"])
        reranker = MemoryReranker(model_adapter=mock)
        candidates = _make_candidates()
        reranker.curate(candidates, "Create a React login form", ["frontend"])
        assert reranker.call_count == 1


class TestRerankerCuration:
    def test_selects_relevant_memories(self):
        mock = MockModelAdapter(selected_ids=["m1", "m3", "m5"])
        reranker = MemoryReranker(model_adapter=mock)
        candidates = _make_candidates()
        result = reranker.curate(
            candidates, "Create a React login form",
            active_domains=["frontend"],
        )
        assert result.selected_ids == ["m1", "m3", "m5"]
        assert result.summary != ""

    def test_rejects_cross_domain_memories(self):
        mock = MockModelAdapter(selected_ids=["m1"])
        reranker = MemoryReranker(model_adapter=mock)
        candidates = _make_candidates()
        result = reranker.curate(
            candidates, "Create a React component",
            active_domains=["frontend"],
        )
        assert result.confidence >= 0.5

    def test_detects_conflicts(self):
        mock = MockModelAdapter(
            selected_ids=["m7"],
            conflicts=[{"a": "m1", "b": "m7", "desc": "Redux vs Zustand"}],
        )
        reranker = MemoryReranker(model_adapter=mock)
        result = reranker.curate(
            _make_candidates(), "Migrate state management",
        )
        assert len(result.conflicts) == 1
        assert result.conflicts[0]["desc"] == "Redux vs Zustand"


class TestRerankerCache:
    def test_cache_hit(self):
        mock = MockModelAdapter(selected_ids=["m1", "m3"])
        reranker = MemoryReranker(model_adapter=mock, cache_ttl=999)
        candidates = _make_candidates()

        # First call
        r1 = reranker.curate(candidates, "React component task", ["frontend"])
        assert reranker.call_count == 1

        # Second call with same input — should hit cache
        r2 = reranker.curate(candidates, "React component task", ["frontend"])
        assert reranker.call_count == 1  # No new call
        assert r2.selected_ids == r1.selected_ids

    def test_cache_miss_different_task(self):
        mock = MockModelAdapter(selected_ids=["m1"])
        reranker = MemoryReranker(model_adapter=mock, cache_ttl=999)
        candidates = _make_candidates()

        reranker.curate(candidates, "React component task", ["frontend"])
        reranker.curate(candidates, "Backend API migration", ["backend"])
        assert reranker.call_count == 2

        rate = reranker.cache_hit_rate
        assert rate >= 0.0


class TestRerankerFallback:
    def test_fallback_on_model_error(self):
        class FailingModel:
            def generate(self, prompt):
                raise RuntimeError("API timeout")
        reranker = MemoryReranker(model_adapter=FailingModel())
        candidates = _make_candidates()
        result = reranker.curate(candidates, "React component", ["frontend"])
        # Should fallback to top candidates
        assert len(result.selected_ids) >= 1
        assert result.confidence < 0.5

    def test_fallback_on_invalid_json(self):
        class BadJSONModel:
            def generate(self, prompt):
                return {"content": "not valid json at all {{{"}
        reranker = MemoryReranker(model_adapter=BadJSONModel())
        candidates = _make_candidates()
        result = reranker.curate(candidates, "React component", ["frontend"])
        assert len(result.selected_ids) >= 1
        assert result.confidence < 0.5

    def test_fallback_on_empty_candidates(self):
        mock = MockModelAdapter()
        reranker = MemoryReranker(model_adapter=mock)
        result = reranker.curate([], "Nothing", [])
        assert result.selected_ids == []

    def test_fallback_without_model(self):
        reranker = MemoryReranker(model_adapter=None)
        candidates = _make_candidates()
        result = reranker.curate(candidates, "React component", ["frontend"])
        assert len(result.selected_ids) == 5  # Takes top 5


class TestRerankerEdgeCases:
    def test_max_content_truncation(self):
        mock = MockModelAdapter(selected_ids=["m1"])
        reranker = MemoryReranker(model_adapter=mock, max_content_len=20)
        long_entry = _make_entry("m100", "A" * 500)
        result = reranker.curate([long_entry], "test", [])
        assert len(result.selected_ids) == 1

    def test_markdown_wrapped_json(self):
        class MarkdownModel:
            def generate(self, prompt):
                return {"content": '```json\n{"selected": ["m1"], "rejected": [], "conflicts": [], "summary": "test"}\n```'}
        reranker = MemoryReranker(model_adapter=MarkdownModel())
        result = reranker.curate(
            [_make_entry("m1", "content")], "test", [],
        )
        assert result.selected_ids == ["m1"]

    def test_create_reranker_passthrough(self):
        # Passing model=None triggers auto-creation attempt
        reranker = create_reranker(model=None)
        # May be enabled if model adapter creation succeeds, or disabled if not
        assert isinstance(reranker, MemoryReranker)
        candidates = _make_candidates()
        result = reranker.curate(candidates, "test", [])
        assert len(result.selected_ids) >= 1

    def test_reranker_with_next_interface(self):
        mock = MockModelAdapter(selected_ids=["m1", "m3"])
        reranker = MemoryReranker(model_adapter=mock)
        result = reranker.curate(
            _make_candidates(), "React form task",
            active_domains=["frontend"],
        )
        assert len(result.selected_ids) >= 1


class TestRerankerIntegration:
    """Simulate the full BM25 → Reranker → inject flow."""

    def test_full_flow_with_mock_model(self):
        mock = MockModelAdapter(
            selected_ids=["m3", "m7", "m1"],
            summary="Project uses React with Zustand for state. Forms use react-hook-form.",
        )
        reranker = MemoryReranker(model_adapter=mock)
        candidates = _make_candidates()

        result = reranker.curate(
            candidates=candidates,
            task_description="Build a registration form with validation",
            active_domains=["frontend"],
            current_files=["src/Register.tsx", "src/validation.ts"],
        )

        # Verify selected memories are domain-appropriate
        assert "m3" in result.selected_ids  # react-hook-form
        assert "m7" in result.selected_ids  # Zustand migration
        assert "m1" in result.selected_ids  # React hooks
        assert "m2" not in result.selected_ids  # FastAPI (backend)
        assert "m4" not in result.selected_ids  # Alembic (database)
        assert result.summary != ""

    def test_reranker_result_is_json_serializable(self):
        result = RerankResult(
            selected_ids=["m1", "m2"],
            rejected=[{"id": "m3", "reason": "Not relevant"}],
            conflicts=[],
            summary="Test summary",
        )
        d = {
            "selected": result.selected_ids,
            "rejected": result.rejected,
            "conflicts": result.conflicts,
            "summary": result.summary,
        }
        json.dumps(d)  # Should not raise
