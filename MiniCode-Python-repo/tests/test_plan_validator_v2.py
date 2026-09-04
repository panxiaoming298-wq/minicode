from __future__ import annotations

from dataclasses import replace

import pytest

from minicode.runtime_protocol import PlanNode, PlanVersion, SelectedRoute, ToolEffect
from minicode.unified_runtime import PlanValidationError, PlanValidator, ProfileResolver


def _ultra_profile(route=SelectedRoute.DEEP_SINGLE):
    return ProfileResolver().resolve(
        runtime={"executionProfile": "ultra", "runtimeMaxAgents": 6},
        selected_route=route,
        request="complex",
    )


def test_ultra_v1_rejects_two_logical_writers_before_scheduling() -> None:
    writer_effects = (ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE)
    plan = PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("writer-a", "a", "writer", allowed_effects=writer_effects),
            PlanNode("writer-b", "b", "writer", allowed_effects=writer_effects),
        ),
    )
    with pytest.raises(PlanValidationError, match="exactly one"):
        PlanValidator().validate(plan, _ultra_profile())


def test_plan_validator_rejects_cycle_and_non_readonly_explorer() -> None:
    cycle = PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("a", "a", "explorer", dependencies=("b",)),
            PlanNode("b", "b", "explorer", dependencies=("a",)),
            PlanNode(
                "writer",
                "write",
                "writer",
                dependencies=("a", "b"),
                allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE),
            ),
        ),
    )
    with pytest.raises(PlanValidationError, match="cycle"):
        PlanValidator().validate(cycle, _ultra_profile())

    unsafe = replace(
        cycle,
        nodes=(
            PlanNode(
                "explorer",
                "inspect",
                "explorer",
                allowed_effects=(ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE),
            ),
            cycle.nodes[-1],
        ),
    )
    with pytest.raises(PlanValidationError, match="read-only"):
        PlanValidator().validate(unsafe, _ultra_profile())


def test_plan_validator_rejects_empty_plan_and_incomplete_fanout_barriers() -> None:
    empty = PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(),
    )
    with pytest.raises(PlanValidationError, match="at least one"):
        PlanValidator().validate(empty, _ultra_profile())

    writer_effects = (ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE)
    incomplete = PlanVersion(
        run_id="run-1",
        plan_version=1,
        contract_revision=1,
        nodes=(
            PlanNode("explorer-a", "a", "explorer"),
            PlanNode("explorer-b", "b", "reviewer"),
            PlanNode(
                "writer",
                "write",
                "writer",
                dependencies=("explorer-a",),
                allowed_effects=writer_effects,
            ),
            PlanNode("verifier", "verify", "verifier", dependencies=("writer",)),
        ),
    )
    with pytest.raises(PlanValidationError, match="every read-only explorer"):
        PlanValidator().validate(
            incomplete,
            _ultra_profile(SelectedRoute.ULTRA_READ_FANOUT),
        )
