from __future__ import annotations

import ast
from pathlib import Path

from minicode.pipeline_engine import PipelineEngine
from minicode.task_graph import CanonicalPlanGraph
from minicode.runtime_protocol import PlanNode, PlanVersion


ROOT = Path(__file__).parents[1]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_product_entrypoints_import_unified_runtime_not_agent_loop() -> None:
    for relative in (
        "minicode/main.py",
        "minicode/headless.py",
        "minicode/web_worker.py",
        "minicode/tui/input_handler.py",
    ):
        imports = _imported_modules(ROOT / relative)
        assert "minicode.unified_runtime" in imports
        assert "minicode.agent_loop" not in imports


def test_pipeline_is_not_runtime_authority_and_canonical_graph_wraps_plan() -> None:
    assert PipelineEngine.runtime_authority is False
    plan = PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(PlanNode("node", "work", "standard"),),
    )
    graph = CanonicalPlanGraph(plan)
    assert graph.current is plan
    assert graph.version == 1
    assert graph.to_dict()["run_id"] == "run-1"
