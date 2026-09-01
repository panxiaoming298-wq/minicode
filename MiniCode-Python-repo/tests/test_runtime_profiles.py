from minicode.runtime_profiles import get_runtime_profile, resolve_runtime_profile


def test_get_runtime_profile_defaults_to_single() -> None:
    profile = get_runtime_profile(None)

    assert profile.name == "single"
    assert profile.max_steps == 50


def test_resolve_runtime_profile_keeps_single_deep_budget_floor() -> None:
    profile = resolve_runtime_profile(
        {"runtimeProfile": "single-deep"},
        fallback_max_steps=1,
    )

    assert profile.name == "single-deep"
    assert profile.max_steps == 80
    assert profile.widening_step_bonus == 6


def test_resolve_runtime_profile_preserves_explicit_single_budget() -> None:
    profile = resolve_runtime_profile(
        {"runtimeProfile": "single"},
        fallback_max_steps=12,
    )

    assert profile.name == "single"
    assert profile.max_steps == 12
