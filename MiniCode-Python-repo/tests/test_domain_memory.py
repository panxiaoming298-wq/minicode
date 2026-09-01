"""Tests for domain classifier + domain-aware memory search + TaskContext."""
from __future__ import annotations

import time

from minicode.agent_reflection import ReflectionEngine, ReflectionResult
from minicode.domain_classifier import (
    DomainType,
    classify,
    get_active_domain_values,
)
from minicode.memory import MemoryEntry, MemoryFile, MemoryManager, MemoryScope


# ── Domain Classifier ───────────────────────────────────────────────

class TestDomainClassifier:
    def test_frontend_from_tsx(self):
        domains = classify(current_files=["src/App.tsx"])
        assert (DomainType.FRONTEND, 0.6) in domains

    def test_backend_from_py(self):
        domains = classify(current_files=["api/server.py"])
        domain_values = [d.value for d, _ in domains]
        assert "backend" in domain_values

    def test_database_from_sql(self):
        domains = classify(current_files=["migrations/001.sql"])
        assert any(d == DomainType.DATABASE for d, _ in domains)

    def test_devops_from_dockerfile(self):
        domains = classify(current_files=["Dockerfile"])
        assert any(d == DomainType.DEVOPS for d, _ in domains)

    def test_testing_from_spec_file(self):
        domains = classify(current_files=["src/App.spec.ts"])
        assert any(d == DomainType.TESTING for d, _ in domains)

    def test_intent_keyword_react(self):
        domains = classify(intent_text="create a React component for form validation")
        assert any(d == DomainType.FRONTEND for d, _ in domains)

    def test_intent_keyword_api(self):
        domains = classify(intent_text="build a REST API endpoint for user authentication")
        assert any(d == DomainType.BACKEND for d, _ in domains)

    def test_combined_signals(self):
        domains = classify(
            current_files=["src/Login.tsx", "src/auth.ts"],
            intent_text="implement JWT token refresh in React",
        )
        domain_values = [d.value for d, _ in domains]
        assert "frontend" in domain_values or "security" in domain_values

    def test_no_signals_returns_general(self):
        domains = classify()
        assert (DomainType.GENERAL, 0.5) in domains

    def test_user_domain_overrides(self):
        domains = classify(user_domains=["database"])
        assert (DomainType.DATABASE, 1.0) in domains

    def test_get_active_domain_values(self):
        values = get_active_domain_values(current_files=["src/App.tsx", "api/server.py"])
        assert "frontend" in values
        assert "backend" in values

    def test_python_data_science_crossover(self):
        domains = classify(current_files=["notebook.ipynb"])
        domain_values = [d.value for d, _ in domains]
        assert "data_science" in domain_values


# ── N3: Domain Query Expansion ──────────────────────────────────────

class TestDomainQueryExpansion:
    def test_frontend_expansion(self):
        from minicode.memory import _expand_query_terms
        terms = _expand_query_terms(["component"], active_domains=["frontend"])
        assert "widget" in terms or "组件" in terms

    def test_backend_expansion(self):
        from minicode.memory import _expand_query_terms
        terms = _expand_query_terms(["api"], active_domains=["backend"])
        assert "endpoint" in terms or "端点" in terms

    def test_database_expansion(self):
        from minicode.memory import _expand_query_terms
        terms = _expand_query_terms(["migration"], active_domains=["database"])
        assert "alembic" in terms or "迁移" in terms

    def test_devops_expansion(self):
        from minicode.memory import _expand_query_terms
        terms = _expand_query_terms(["deploy"], active_domains=["devops"])
        assert "部署" in terms or "release" in terms

    def test_base_expansion_still_works(self):
        from minicode.memory import _expand_query_terms
        terms = _expand_query_terms(["function"])
        assert "func" in terms

    def test_no_domains_no_domain_expansion(self):
        from minicode.memory import _expand_query_terms
        with_domains = _expand_query_terms(["component"], active_domains=["frontend"])
        without = _expand_query_terms(["component"])
        assert len(with_domains) > len(without)

    def test_search_with_domain_expansion(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT)
        mf.add_entry(MemoryEntry(
            id="fe-widget", scope=MemoryScope.PROJECT,
            category="pattern", content="Use controlled widget for form inputs",
            domains=["frontend"],
        ))
        results = mf.search("component", active_domains=["frontend"])
        # "component" domain-expands to include "widget" which matches the entry
        assert len(results) == 1


# ── Domain-Aware Memory Search ─────────────────────────────────────

class TestDomainMemorySearch:
    def test_memory_entry_has_domains(self):
        entry = MemoryEntry(
            id="test-1", scope=MemoryScope.PROJECT,
            category="pattern", content="React hooks best practices",
            domains=["frontend", "react"],
        )
        assert "frontend" in entry.domains

    def test_memory_entry_domains_backward_compatible(self):
        entry = MemoryEntry(
            id="test-2", scope=MemoryScope.PROJECT,
            category="convention", content="Import order in Go",
        )
        assert entry.domains == []

    def test_domain_search_boosts_match(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT)
        mf.add_entry(MemoryEntry(
            id="fe-1", scope=MemoryScope.PROJECT,
            category="pattern", content="Use React functional components",
            domains=["frontend"],
        ))
        mf.add_entry(MemoryEntry(
            id="be-1", scope=MemoryScope.PROJECT,
            category="pattern", content="Use Python decorators for API routes",
            domains=["backend"],
        ))
        results = mf.search("React component", active_domains=["frontend"])
        assert len(results) >= 1
        # Frontend entry should rank higher
        if len(results) >= 2:
            assert results[0].id == "fe-1"

    def test_search_without_domains_still_works(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT)
        mf.add_entry(MemoryEntry(
            id="fe-1", scope=MemoryScope.PROJECT,
            category="pattern", content="React component pattern",
            domains=["frontend"],
        ))
        results = mf.search("React component")
        assert len(results) >= 1

    def test_memory_entry_to_dict_includes_domains(self):
        entry = MemoryEntry(
            id="test-3", scope=MemoryScope.PROJECT,
            category="convention", content="Use tabs not spaces",
            domains=["frontend"],
        )
        d = entry.to_dict()
        assert "domains" in d
        assert d["domains"] == ["frontend"]

    def test_memory_entry_from_dict_restores_domains(self):
        data = {
            "id": "test-4", "scope": "project",
            "category": "convention", "content": "Use ESLint",
            "domains": ["frontend", "testing"],
        }
        entry = MemoryEntry.from_dict(data)
        assert entry.domains == ["frontend", "testing"]


# ── TaskContext (ReflectionEngine) ─────────────────────────────────

class TestTaskContext:
    def test_reflection_result_has_task_context(self):
        rr = ReflectionResult(
            task_summary="Add login form",
            success=True,
            key_decisions=[],
            errors_encountered=[],
            lessons_learned=[],
            suggested_improvements=[],
            confidence=0.8,
            task_context={
                "files": ["src/Login.tsx", "src/auth.ts"],
                "libraries": ["react", "zustand"],
                "tools": ["write_file", "read_file"],
            },
        )
        assert rr.task_context["files"] == ["src/Login.tsx", "src/auth.ts"]
        assert "react" in rr.task_context["libraries"]

    def test_to_memory_entry_with_context(self):
        rr = ReflectionResult(
            task_summary="Add login form",
            success=True,
            key_decisions=["Used react-hook-form + zod"],
            errors_encountered=[],
            lessons_learned=["Validate early"],
            suggested_improvements=[],
            confidence=0.8,
            task_context={
                "files": ["src/Login.tsx"],
                "libraries": ["react-hook-form", "zod"],
            },
        )
        mem = rr.to_memory_entry()
        assert mem["category"] == "task_context"
        assert "domains" in mem
        assert "react-hook-form" in mem["tags"]
        assert "zod" in mem["tags"]
        assert mem["metadata"]["task_context"]["files"] == ["src/Login.tsx"]

    def test_reflection_engine_extracts_task_context(self):
        engine = ReflectionEngine()
        trace = [
            {"type": "tool_call", "name": "write_file", "path": "src/Login.tsx"},
            {"type": "tool_call", "name": "read_file", "path": "src/auth.ts"},
            {"type": "assistant", "content": "I used react-hook-form with zod for validation"},
            {"type": "tool_call", "name": "execute_command", "input": "npm install react-hook-form zod"},
        ]
        result = engine.reflect("Create login form", trace)
        ctx = result.task_context
        assert "files" in ctx, f"Expected files in task_context, got {ctx}"
        assert len(ctx["files"]) >= 1

    def test_to_memory_entry_fallback_reflection(self):
        rr = ReflectionResult(
            task_summary="General task",
            success=True,
            key_decisions=[],
            errors_encountered=[],
            lessons_learned=[],
            suggested_improvements=[],
            confidence=0.7,
            task_context={},
        )
        mem = rr.to_memory_entry()
        assert mem["category"] == "reflection"
        assert "self-reflection" in mem["tags"]
