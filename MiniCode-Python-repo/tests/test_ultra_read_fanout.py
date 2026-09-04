from __future__ import annotations

import threading

from minicode.tooling import ToolRegistry
from minicode.unified_runtime import run_runtime_turn


def test_ultra_parallel_reads_then_isolated_unique_writer_then_verifier(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "input.txt").write_text("base", encoding="utf-8")
    temporary_root = tmp_path / "runtime-temp"
    barrier = threading.Barrier(2)
    starts: list[str] = []
    memory_task_ids: list[str | None] = []
    lock = threading.Lock()

    def fake_agent_loop(**kwargs):
        node_id = kwargs["runtime"]["_runtimeNodeId"]
        with lock:
            starts.append(node_id)
            memory_task_ids.append(kwargs["runtime"].get("_memoryTaskId"))
        if node_id.startswith("explorer-"):
            barrier.wait(timeout=5)
            content = f"evidence from {node_id}"
        elif node_id == "exclusive-writer":
            (tmp_path.__class__(kwargs["cwd"]) / "result.txt").write_text(
                "integrated writer output",
                encoding="utf-8",
            )
            content = "writer done"
        elif node_id == "final-verifier":
            assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "integrated writer output"
            content = "all integrated checks passed <verification_passed>"
        else:
            raise AssertionError(node_id)
        return [*kwargs["messages"], {"role": "assistant", "content": content}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {
        "executionProfile": "ultra",
        "runtimeV2": True,
        "model": "fake",
        "runtimeMaxActiveWorkers": 2,
        "runtimeMaxAgents": 4,
        "runtimeTemporaryRoot": str(temporary_root),
        "ultraSubproblems": ["audit the first independent path", "audit the second independent path"],
        "_workerModelFactory": lambda tools: object(),
    }
    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "implement using independent audits"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    trace = runtime["lastRuntimeTrace"]
    assert trace["topology_decision"]["selected_route"] == "ultra-read-fanout"
    assert trace["scheduler"]["peak_active_workers"] == 2
    assert trace["scheduler"]["peak_active_writers"] == 1
    assert set(starts[:2]) == {"explorer-1", "explorer-2"}
    assert memory_task_ids
    assert set(memory_task_ids) == {runtime["_memoryTaskId"]}
    assert runtime["_memoryTaskId"].startswith("task_run-")
    assert starts.index("exclusive-writer") > max(starts.index("explorer-1"), starts.index("explorer-2"))
    assert starts.index("final-verifier") > starts.index("exclusive-writer")
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "integrated writer output"
    assert trace["delivery"]["completed"] is True
    assert messages[-1]["content"].startswith("writer done")
    assert temporary_root.is_dir()
    assert list(temporary_root.iterdir()) == []
