"""Request-local output allowances and recovery; never changes saved limits."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


@dataclass(slots=True)
class OutputRecovery:
    """Owned by one AgentLoop, including its model calls and tool checkpoints."""

    allowances: dict[str, int] = field(default_factory=dict)
    context_allowances: dict[str, int] = field(default_factory=dict)
    latest: dict[str, Any] = field(default_factory=dict)

    def observe(self, evidence: Mapping[str, Any] | None) -> None:
        # Missing provider evidence must not reuse a previous model's identity.
        self.latest = dict(evidence or {})

    def increase(self) -> tuple[int, int] | None:
        evidence = self.latest
        current = evidence.get("effective_tokens")
        ceiling = evidence.get("model_max_tokens")
        requested = evidence.get("requested_tokens")
        identity = evidence.get("identity")
        if (evidence.get("mode") != "auto" or not isinstance(identity, str)
            or type(current) is not int or type(ceiling) is not int
            or type(requested) is not int or current < requested):
            # A reduced ledger grant is a total-budget constraint, not a reason
            # to request still more output on the next attempt.
            return None
        context_ceiling = evidence.get("context_output_ceiling")
        increased = min(ceiling, current * 2)
        if type(context_ceiling) is int and increased > context_ceiling:
            if current < context_ceiling:
                increased = context_ceiling
            elif increased > current:
                # More output cannot fit this same history. The next planner
                # must make room through verified handoff/paging before retry.
                # Keep this local to the model/route and this loop; never grow
                # the saved context window or accept the truncated response.
                self.context_allowances[identity] = increased
                self.latest["context_recovery_requested"] = True
        if increased <= current:
            return None
        self.allowances[identity] = increased
        return current, increased


_recovery: ContextVar[OutputRecovery | None] = ContextVar("model_output_recovery", default=None)


@contextmanager
def bind_output_recovery(recovery: OutputRecovery | None) -> Iterator[None]:
    token = _recovery.set(recovery)
    try:
        yield
    finally:
        _recovery.reset(token)


def _official_route(endpoint: str, hostname: str, paths: set[str]) -> bool:
    try:
        parsed = urlsplit(endpoint)
        return (parsed.scheme == "https" and parsed.hostname == hostname
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment and parsed.path.rstrip("/") in paths)
    except ValueError:
        return False


def select_output_budget(runtime: Mapping[str, Any], body: Mapping[str, Any], *,
                         endpoint: str, protocol: str) -> dict[str, Any]:
    """Resolve route capabilities or an explicitly supported model reference.

    DeepSeek defaults and exact maximum verified 2026-10-16:
    https://api-docs.deepseek.com/api/create-chat-completion/
    The deepseek-v4.1-flash relay name uses deepseek-flash as its reference;
    this does not verify a relay's own limits or rewrite the requested model.
    Other exact, built-in models use a conservative allowance within their
    catalog capability. Unrecognized models/routes keep a conservative limit.
    """
    model = str(body.get("model", runtime.get("model", "")))
    identity = hashlib.sha256(f"{protocol}\n{endpoint.rstrip('/')}\n{model}".encode()).hexdigest()
    explicit = next((body[key] for key in ("max_completion_tokens", "max_tokens")
                     if type(body.get(key)) is int and body[key] > 0), None)
    if explicit is None and type(runtime.get("maxOutputTokens")) is int and runtime["maxOutputTokens"] > 0:
        explicit = runtime["maxOutputTokens"]
    maximum = None
    initial = 8192
    source = "conservative_default"
    reference_model = "deepseek-flash" if protocol == "openai" and model == "deepseek-v4.1-flash" else None
    official_deepseek = (
        protocol == "openai" and model in {"deepseek-flash", "deepseek-v4-pro"}
        and _official_route(endpoint, "api.deepseek.com", {"", "/v1", "/chat/completions", "/v1/chat/completions"})
    )
    if official_deepseek or reference_model:
        maximum = 393216
        thinking = body.get("thinking")
        disabled = isinstance(thinking, Mapping) and thinking.get("type") == "disabled"
        effort = body.get("reasoning_effort", "high")
        initial = 8192 if disabled or effort == "none" else 131072 if effort in {"max", "ultra"} else 65536
        source = "model_reference" if reference_model else "provider_default"
    else:
        official = (
            protocol == "openai" and _official_route(endpoint, "api.openai.com", {"/v1", "/v1/chat/completions"})
            or protocol == "anthropic" and _official_route(endpoint, "api.anthropic.com", {"", "/v1", "/v1/messages"})
        )
        if official:
            from minicode.model_registry import BUILTIN_MODELS
            info = BUILTIN_MODELS.get(model)
            if info is not None and info.max_output_tokens:
                maximum = info.max_output_tokens
                initial = min(32768, maximum)
                source = "model_catalog"
    recovery = _recovery.get()
    desired = explicit if explicit is not None else max(initial, recovery.allowances.get(identity, 0) if recovery else 0)
    if maximum is not None:
        desired = min(desired, maximum)
    return {"mode": "manual" if explicit is not None else "auto", "source": "explicit" if explicit is not None else source,
            "identity": identity, "requested_tokens": desired, "preferred_tokens": desired, "initial_tokens": initial,
            "context_recovery_tokens": recovery.context_allowances.get(identity, 0) if recovery and explicit is None else 0,
            "model_max_tokens": maximum, "minimum_tokens": min(desired, 1024),
            **({"reference_model": reference_model} if reference_model else {})}


def context_capacity(*, context_window: int, output_tokens: int, output_floor: int = 4096,
                     safety_tokens: int = 1024) -> dict[str, int | str]:
    """Hard input ceiling for one output allowance within the total window."""
    reserve = max(output_floor, output_tokens) + safety_tokens
    return {"context_window": context_window, "effective_input": max(0, context_window - reserve),
            "reserved_tokens": reserve,
            "output_reserve_tokens": max(output_floor, output_tokens), "safety_reserve_tokens": safety_tokens,
            "capacity_error": "output_reserve_exceeds_window" if reserve >= context_window else ""}


def planning_capacity(*, context_window: int, output_policy: Mapping[str, Any], config) -> dict[str, Any]:
    """Separate the operating window, handoff thresholds and per-call output.

    Automatic output lends unused capacity to history. Keep a bounded working
    allowance (up to 32K), rather than reserving the provider's entire default.
    Explicit output settings retain their existing fixed-reservation contract.
    The smaller allowance is for planning only: dispatch measures the complete
    request and fits its actual output before reserving usage or sending HTTP.
    """
    desired = output_policy.get("preferred_tokens", output_policy["requested_tokens"])
    automatic = output_policy["mode"] == "auto"
    working_output = (min(desired, 32768, max(config.output_token_reserve, context_window // 10))
                      if automatic else desired)
    capacity = context_capacity(context_window=context_window, output_tokens=working_output,
        output_floor=config.output_token_reserve, safety_tokens=config.safety_token_reserve)
    # An explicit large output allowance or a tiny window may require earlier
    # handoff. Protection must never start beyond the hard admission ceiling.
    pressure_window = min(context_window, int(capacity["effective_input"] / config.context_protection_ratio))
    recovery_output = output_policy.get("context_recovery_tokens", 0) if automatic else 0
    if recovery_output:
        # Ask for an earlier handoff, without shrinking admission so abruptly
        # that the very request needed to write its note can no longer fit.
        # A recovery target can exceed a small window; retain room for native
        # evidence and let actual dispatch fit output within the unchanged cap.
        recovery_input = max(config.output_token_reserve,
                             context_window - recovery_output - config.safety_token_reserve)
        pressure_window = min(pressure_window, int(recovery_input / config.context_protection_ratio))
    capacity["budget_policy"] = {
        "version": "total-window-v1", "output_mode": output_policy["mode"], "output_limit": desired,
        "input_limit": capacity["effective_input"], "pressure_window": pressure_window,
        "note_at_tokens": int(pressure_window * config.context_note_warning_ratio),
        "compact_at_tokens": int(pressure_window * config.context_rotation_target_ratio),
        "protect_at_tokens": int(pressure_window * config.context_protection_ratio),
    }
    return capacity


def fit_output_context(policy: Mapping[str, Any], *, input_tokens: int, context_window: int,
                       safety_tokens: int = 1024) -> dict[str, Any]:
    """Allow automatic output to fit a measured request without growing context."""
    result = dict(policy)
    available = max(0, context_window - input_tokens - safety_tokens)
    result["context_output_ceiling"] = available
    if result["mode"] == "auto" and available >= result["minimum_tokens"]:
        result["requested_tokens"] = min(result.get("preferred_tokens", result["requested_tokens"]), available)
    return result
