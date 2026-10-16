from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping


def parallel_candidates_enabled(runtime: Mapping[str, Any] | None) -> bool:
    """Resolved execution hint, never authority or a public policy mutation."""
    runtime = runtime or {}
    return runtime.get("_runtimeParallelCandidates", runtime.get("runtimeParallelCandidates")) is True


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Legacy-compatible reasoning profile for one Worker turn.

    This is intentionally independent from Runtime V2's public
    ``ExecutionProfile`` (Standard/Deep/Ultra). It controls AgentLoop depth only;
    it never chooses Worker topology or verification strength.
    """

    name: str
    max_steps: int | None
    empty_response_retry_limit: int = 2
    recoverable_thinking_retry_limit: int = 3
    working_memory_ttl_seconds: float | None = 1800
    working_memory_importance: float = 1.0
    strict_step_verification: bool = False
    widen_after_step: int | None = None
    widening_step_bonus: int = 0


_PROFILES: dict[str, RuntimeProfile] = {
    "single": RuntimeProfile(
        name="single",
        max_steps=50,
        empty_response_retry_limit=2,
        recoverable_thinking_retry_limit=3,
        working_memory_ttl_seconds=1800,
        working_memory_importance=1.0,
        strict_step_verification=False,
        widen_after_step=None,
        widening_step_bonus=0,
    ),
    "single-deep": RuntimeProfile(
        name="single-deep",
        max_steps=80,
        empty_response_retry_limit=3,
        recoverable_thinking_retry_limit=5,
        working_memory_ttl_seconds=7200,
        working_memory_importance=1.4,
        strict_step_verification=True,
        widen_after_step=6,
        widening_step_bonus=6,
    ),
}


def get_runtime_profile(name: str | None) -> RuntimeProfile:
    key = str(name or "single").strip().lower()
    return _PROFILES.get(key, _PROFILES["single"])


def worker_runtime_profile(runtime: Mapping[str, Any], *, role: str, node_id: str) -> str:
    """Use the admitted mode depth; old runs retain their legacy role mapping."""
    if role == "primary" and runtime.get("_runtimePrimaryProfile") is not None:
        return str(runtime["_runtimePrimaryProfile"])
    if role == "verifier" and node_id == "final-verifier" and runtime.get("_runtimeReviewPolicy"):
        return str(runtime["_runtimeReviewPolicy"]["runtime_profile"])
    return "single-deep" if role in {"deep", "writer", "verifier"} else str(runtime.get("runtimeProfile", "single"))


def resolve_runtime_profile(
    runtime: Mapping[str, Any] | None,
    *,
    fallback_max_steps: int | None = None,
) -> RuntimeProfile:
    requested_name = runtime.get("runtimeProfile") if runtime else None
    profile = get_runtime_profile(str(requested_name or "single"))

    # A validated per-run unlimited choice must reach the AgentLoop too;
    # otherwise a healthy long task still fails at 50/80 tool steps.
    if runtime and runtime.get("_runtimeUnlimitedSteps") is True:
        return replace(profile, max_steps=None)

    resolved_max_steps = profile.max_steps
    if fallback_max_steps is not None:
        if profile.name == "single-deep":
            if resolved_max_steps is None:
                resolved_max_steps = fallback_max_steps
            else:
                resolved_max_steps = max(resolved_max_steps, fallback_max_steps)
        else:
            resolved_max_steps = fallback_max_steps

    return replace(profile, max_steps=resolved_max_steps)
