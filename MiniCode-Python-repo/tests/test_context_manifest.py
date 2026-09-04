from __future__ import annotations

import threading

from minicode.tooling import ToolRegistry
from minicode.unified_runtime import run_runtime_turn


def test_every_ultra_worker_records_auditable_context_manifest(tmp_path, monkeypatch) -> None:
    (tmp_path / "input.txt").write_text("context", encoding="utf-8")
    barrier = threading.Barrier(2)

    def fake_agent_loop(**kwargs):
        node_id = kwargs["runtime"]["_runtimeNodeId"]
        if node_id.startswith("explorer-"):
            barrier.wait(timeout=5)
            content = "evidence"
        elif node_id == "final-verifier":
            content = "integrated state accepted <verification_passed>"
        else:
            content = "writer complete"
        return [*kwargs["messages"], {"role": "assistant", "content": content}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {
        "executionProfile": "ultra",
        "runtimeV2": True,
        "runtimeMaxActiveWorkers": 2,
        "runtimeMaxAgents": 4,
        "ultraSubproblems": ["inspect independent alpha", "inspect independent beta"],
        "_workerModelFactory": lambda tools: object(),
    }
    run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "inspect then integrate"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    manifests = {
        item["node_id"]: item
        for item in runtime["lastRuntimeTrace"]["context_manifests"]
    }
    assert set(manifests) == {
        "explorer-1",
        "explorer-2",
        "exclusive-writer",
        "final-verifier",
    }
    for manifest in manifests.values():
        assert manifest["contract_revision"] == 1
        assert manifest["plan_version"] == 1
        assert len(manifest["base_revision"]) == 64
        assert len(manifest["included_files"]["input.txt"]) == 64
        assert manifest["policy_version"]
        assert manifest["prompt_template_version"]
        assert manifest["tool_capability_scope"]
        assert manifest["budget_allocation"]["max_agents"] == 4
    assert manifests["exclusive-writer"]["dependency_result_refs"] == [
        "explorer-1",
        "explorer-2",
    ]
    assert manifests["final-verifier"]["dependency_result_refs"] == [
        "exclusive-writer"
    ]

