"""Memory system stress tests — 500+ entries, concurrent ops, rapid cycles."""
from __future__ import annotations

import tempfile
import threading
import time

from minicode.memory import MemoryEntry, MemoryFile, MemoryManager, MemoryScope, MemoryTier


class TestMemoryStressLargeVolume:
    """500+ entries: search, CRUD, index rebuild performance."""

    def test_index_500_entries(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT, max_entries=1000, max_size_bytes=200 * 1024)
        for i in range(500):
            mf.add_entry(MemoryEntry(
                id=f"stress-{i}", scope=MemoryScope.PROJECT,
                category="pattern",
                content=f"Memory entry {i}: project convention for handling edge cases in production",
                domains=["frontend"] if i % 3 == 0 else ["backend"] if i % 3 == 1 else ["database"],
            ))
        assert len(mf.entries) == 500
        # Index should be valid
        mf._ensure_cache_valid()
        assert len(mf._id_index) == 500

    def test_search_500_entries(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT, max_entries=1000, max_size_bytes=200 * 1024)
        for i in range(500):
            mf.add_entry(MemoryEntry(
                id=f"srch-{i}", scope=MemoryScope.PROJECT,
                category="pattern",
                content=f"React component pattern {i}: use hooks for state management",
                domains=["frontend"] if i % 2 == 0 else ["backend"],
            ))
        t0 = time.time()
        results = mf.search("React component hooks", active_domains=["frontend"])
        elapsed = time.time() - t0
        assert len(results) > 0
        assert elapsed < 2.0, f"Search too slow: {elapsed:.2f}s"

    def test_rapid_add_delete(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT)
        for i in range(100):
            entry = MemoryEntry(
                id=f"rapid-{i}", scope=MemoryScope.PROJECT,
                category="pattern", content=f"Rapid entry {i}",
            )
            mf.add_entry(entry)
        assert len(mf.entries) == 100
        for i in range(0, 100, 2):
            mf.delete_entry(f"rapid-{i}")
        assert len(mf.entries) == 50
        mf._ensure_cache_valid()

    def test_domain_distribution_search(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT)
        domains_list = ["frontend", "backend", "database", "devops", "testing"]
        for i in range(300):
            mf.add_entry(MemoryEntry(
                id=f"dom-{i}", scope=MemoryScope.PROJECT,
                category="pattern",
                content=f"Domain entry {i}: best practices for {domains_list[i % 5]}",
                domains=[domains_list[i % 5]],
            ))
        results = mf.search("best practices", active_domains=["frontend"])
        assert len(results) > 0
        # Frontend entries should dominate top results
        top_ids = [e.id for e in results[:5]]
        frontend_in_top = sum(1 for eid in top_ids if int(eid.split("-")[1]) % 5 == 0)
        assert frontend_in_top >= 1


class TestMemoryStressConcurrent:
    """Concurrent access: threads reading while writing."""

    def test_concurrent_reads(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT)
        for i in range(200):
            mf.add_entry(MemoryEntry(
                id=f"conc-{i}", scope=MemoryScope.PROJECT,
                category="pattern", content=f"Concurrent entry {i} with unique pattern data",
            ))

        errors = []
        def search_worker(worker_id):
            try:
                for _ in range(20):
                    results = mf.search(f"entry {worker_id * 10}")
                    assert isinstance(results, list)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=search_worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent errors: {errors}"

    def test_concurrent_add_and_search(self):
        mf = MemoryFile(scope=MemoryScope.PROJECT)
        for i in range(100):
            mf.add_entry(MemoryEntry(
                id=f"cas-{i}", scope=MemoryScope.PROJECT,
                category="pattern", content=f"Base entry {i}",
            ))
        mf._ensure_cache_valid()

        errors = []
        def worker(worker_id):
            try:
                for j in range(10):
                    eid = f"cas-new-{worker_id}-{j}"
                    mf.add_entry(MemoryEntry(
                        id=eid, scope=MemoryScope.PROJECT,
                        category="pattern", content=f"Worker {worker_id} entry {j}",
                    ))
                    results = mf.search(f"Worker {worker_id}")
                    assert isinstance(results, list)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Errors: {errors}"


class TestMemoryStressTiers:
    """Multi-tier lifecycle under stress."""

    def test_rapid_promote_demote(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryManager(project_root=tmp)
            for s in MemoryScope:
                mgr.memories[s].entries.clear()
            for i in range(50):
                mgr.add_entry(
                    MemoryScope.PROJECT, category="pattern",
                    content=f"Tier test entry {i}",
                    tags=["test"],
                )
            # Simulate usage
            for e in mgr.memories[MemoryScope.PROJECT].entries:
                e.usage_count = 10
            result = mgr.promote_memories()
            assert isinstance(result, dict)
            assert "promoted_to_long" in result

    def test_link_50_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryManager(project_root=tmp)
            for s in MemoryScope:
                mgr.memories[s].entries.clear()
            for i in range(50):
                mgr.add_entry(
                    MemoryScope.PROJECT, category="pattern",
                    content=f"Linked entry {i}: React component pattern for forms",
                    tags=["react", "form"],
                )
            links = mgr.link_memories(similarity_threshold=0.3)
            assert isinstance(links, int)


class TestMemoryStressPipeline:
    """Full pipeline stress: rapid read/write/maintain cycles."""

    def test_rapid_pipeline_cycles(self):
        from minicode.memory_pipeline import MemoryPipeline

        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryManager(project_root=tmp)
            for s in MemoryScope:
                mgr.memories[s].entries.clear()
            for i in range(30):
                mgr.add_entry(
                    MemoryScope.PROJECT, category="pattern",
                    content=f"Pipeline entry {i}: React {i % 3} pattern for frontend development",
                    tags=["react"],
                )

            pipeline = MemoryPipeline(mgr)
            pipeline.initialize(model_adapter=None, workspace_path=tmp)

            # Rapid cycles
            for cycle in range(20):
                results = pipeline.read(f"React component pattern {cycle}", ["src/App.tsx"])
                assert isinstance(results, list)
                pipeline.write(f"Task {cycle}", [{"type": "tool_call", "count": 1}])
                if cycle % 5 == 0:
                    pipeline.maintain(force=True)
