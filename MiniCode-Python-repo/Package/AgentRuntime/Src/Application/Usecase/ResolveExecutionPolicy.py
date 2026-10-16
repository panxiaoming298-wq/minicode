"""Resolve mode presets without prescribing a task graph or provider API."""

from __future__ import annotations

from ..Dto.ExecutionPolicy import ExecutionPolicy, PolicyRequest


def _limit(value: int | None, default: int, name: str, *, minimum: int = 1) -> int:
    result = default if value is None else value
    if type(result) is not int or result < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return result


class ResolveExecutionPolicy:
    def execute(self, request: PolicyRequest) -> ExecutionPolicy:
        if request.mode not in {"standard", "deep", "ultra"}:
            raise ValueError("mode must be standard, deep, or ultra")
        standard = request.mode == "standard"
        max_agents = _limit(request.max_agents, 4 if request.mode != "ultra" else 8, "max_agents")
        active = _limit(request.max_active_workers, 4 if request.mode == "ultra" else 1, "max_active_workers")
        effort = request.reasoning_effort or ("medium" if standard else "xhigh")
        if effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("reasoning_effort is not a supported policy intent")
        autonomous = request.autonomous_delegation
        if autonomous is not None and type(autonomous) is not bool:
            raise ValueError("autonomous_delegation must be a boolean")
        return ExecutionPolicy(
            mode=request.mode,
            max_active_workers=min(active, max_agents),
            max_agents=max_agents,
            max_delegation_depth=1,
            reasoning_effort=effort,
            autonomous_delegation=not standard if autonomous is None else autonomous,
        )
