from __future__ import annotations

import pytest

from minicode.runtime_protocol import RequestedMode, SelectedRoute
from minicode.unified_runtime import ProfileResolver, TopologyResolver


@pytest.mark.parametrize(
    ("task_text", "runtime", "expected"),
    [
        (
            "audit",
            {"ultraSubproblems": ["audit call sites", "audit session recovery"]},
            SelectedRoute.ULTRA_READ_FANOUT,
        ),
        (
            "debug the strongly coupled root cause",
            {"ultraSubproblems": ["trace entry path", "trace state path"]},
            SelectedRoute.DEEP_SINGLE,
        ),
        (
            "review parallel writes to the same file",
            {"ultraSubproblems": ["inspect writer one", "inspect writer two"]},
            SelectedRoute.DEEP_SINGLE,
        ),
        (
            "audit",
            {
                "ultraSubproblems": ["audit call sites", "audit session recovery"],
                "runtimeMaxAgents": 2,
            },
            SelectedRoute.DEEP_SINGLE,
        ),
    ],
)
def test_ultra_topology_decision_is_auditable(task_text, runtime, expected) -> None:
    decision = TopologyResolver().decide(
        requested_mode=RequestedMode.ULTRA,
        request=task_text,
        runtime=runtime,
    )
    assert decision.selected_route is expected
    assert decision.reasons
    assert decision.rejected_routes
    assert decision.to_dict()["requested_profile"] == "ultra"


def test_default_profile_is_standard_and_no_auto_is_accepted() -> None:
    assert ProfileResolver.requested_mode({}) is RequestedMode.STANDARD
    with pytest.raises(ValueError, match="standard.*ultra"):
        ProfileResolver.requested_mode({"executionProfile": "auto"})


@pytest.mark.parametrize(
    "hints",
    [
        {"deterministic_verifiability": "false"},
        {"expected_parallel_gain": "nan"},
        {"estimated_extra_cost": -1},
    ],
)
def test_invalid_or_false_topology_hints_fail_closed_to_deep_single(hints) -> None:
    decision = TopologyResolver().decide(
        requested_mode=RequestedMode.ULTRA,
        request="audit",
        runtime={
            "ultraSubproblems": ["audit call sites", "audit session recovery"],
            "topologyHints": hints,
        },
    )
    assert decision.selected_route is SelectedRoute.DEEP_SINGLE


def test_non_finite_deadline_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite timestamp"):
        ProfileResolver().resolve(
            runtime={"executionProfile": "standard", "runtimeDeadline": "nan"},
            selected_route=SelectedRoute.STANDARD_SINGLE,
            request="work",
        )


def test_ultra_verifier_budget_is_adaptive_and_zero_is_respected() -> None:
    resolver = ProfileResolver()

    conversational = resolver.resolve(
        runtime={"executionProfile": "ultra"},
        selected_route=SelectedRoute.DEEP_SINGLE,
        request="你现在是什么思考强度",
    )
    mutating = resolver.resolve(
        runtime={"executionProfile": "ultra"},
        selected_route=SelectedRoute.DEEP_SINGLE,
        request="修复项目中的并发 bug",
    )
    explicitly_disabled = resolver.resolve(
        runtime={"executionProfile": "ultra", "runtimeVerifierBudget": 0},
        selected_route=SelectedRoute.DEEP_SINGLE,
        request="修复项目中的并发 bug",
    )
    fanout = resolver.resolve(
        runtime={"executionProfile": "ultra", "runtimeVerifierBudget": 0},
        selected_route=SelectedRoute.ULTRA_READ_FANOUT,
        request="audit two independent paths",
    )

    assert conversational.verifier_budget == 0
    assert mutating.verifier_budget == 1
    assert explicitly_disabled.verifier_budget == 0
    assert fanout.verifier_budget == 1
