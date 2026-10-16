"""Provider-neutral policy inputs and effective limits for a primary session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Mode = Literal["standard", "deep", "ultra"]


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    mode: Mode
    max_active_workers: int | None = None
    max_agents: int | None = None
    reasoning_effort: str | None = None
    autonomous_delegation: bool | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    mode: Mode
    max_active_workers: int
    max_agents: int
    max_delegation_depth: int
    reasoning_effort: str
    autonomous_delegation: bool
    helper_roles: tuple[str, ...] = ("explorer", "planner", "reviewer", "verifier")
    helper_effects: tuple[str, ...] = ("read_only",)
    version: str = "primary-policy-v1"
