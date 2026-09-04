from __future__ import annotations

from benchmarks.paged_memory_eval import run_evaluation


def test_offline_paged_memory_evaluation_gates() -> None:
    report = run_evaluation()
    assert report["passed"] is True
    assert all(report["gates"].values())
