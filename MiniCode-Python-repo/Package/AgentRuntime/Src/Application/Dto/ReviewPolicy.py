"""Frozen independent review scope; deterministic acceptance stays separate."""

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ReviewPolicyRequest:
    mode: str
    code_change: bool = False
    change_scope: str = "unknown"
    risk_signals: tuple[str, ...] = ()
    multi_agent_integration: bool = False
    explicit_review: bool | None = None
    requested_scope: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    scope: str
    required: bool
    reasons: tuple[str, ...]
    reasoning_budget: str = "medium"
    runtime_profile: str = "single"
    version: str = "review-policy-v1"

    def __post_init__(self) -> None:
        if self.scope not in {"basic", "focused", "full"} or type(self.required) is not bool:
            raise ValueError("invalid independent review scope or requirement")
        if self.scope == "basic" and self.required:
            raise ValueError("basic checks cannot satisfy required independent review")
        if self.runtime_profile not in {"single", "single-deep"} or self.version != "review-policy-v1":
            raise ValueError("unsupported independent review policy")
        if self.reasoning_budget not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("invalid review reasoning effort")
        if not self.reasons or any(not isinstance(reason, str) or not reason for reason in self.reasons):
            raise ValueError("review policy requires recorded reasons")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReviewPolicy":
        data = dict(value)
        data["reasons"] = tuple(data.get("reasons", ()))
        return cls(**data)
