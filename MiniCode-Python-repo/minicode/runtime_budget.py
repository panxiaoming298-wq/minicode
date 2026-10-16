"""Provider-aware application of Runtime reasoning budgets."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
import re

from minicode.model_registry import detect_provider, resolve_model_info


_BAILIAN_QWEN_COMPLETE_OUTPUT = frozenset({
    "qwen3.7-max", "qwen3.7-max-2026-05-20", "qwen3.7-max-2026-06-08",
    "qwen3.7-flash", "qwen3.7-flash-2026-07-15",
    "qwen3.8-flash",
})


def bailian_qwen_complete_output(model: str, endpoint: str) -> bool:
    """Recognize documented models on the actual public Bailian endpoint.

    Do not infer capabilities from a model alias on an arbitrary proxy. Source:
    https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
    Verified Qwen3.7 Max / Flash and Qwen3.8 Flash IDs: max_tokens excludes thinking;
    max_completion_tokens includes it. Do not infer unlisted future aliases.
    """
    if model not in _BAILIAN_QWEN_COMPLETE_OUTPUT:
        return False
    return _bailian_compatible_endpoint(endpoint)


def bailian_complete_output(model: str, endpoint: str) -> bool:
    """Total reasoning+answer cap on exact, documented Alibaba deployments.

    GLM-5.2 supports max_completion_tokens independently of thinking_budget,
    with the same documented ten-token tolerance. This grants no Qwen history,
    effort, batching or request-compaction capability. Third-party direct
    deployments and unknown aliases must not inherit this policy. Verified
    2026-09-11: https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
    """
    # Exact Alibaba DeepSeek Flash 0731 / Pro 0813 support this total-output cap.
    # Its native max_tokens already includes reasoning, but using the explicit
    # complete-output field retains the documented ten-token accounting margin.
    # Verified 2026-09-13 in the same OpenAI-compatible Chat API reference.
    # deepseek-v4.1-flash: exact alias and the same complete-output accounting,
    # including the ten-token tolerance, verified in the Chat API reference
    # above on 2026-09-21. Unknown aliases and third-party routes stay excluded.
    return (model in _BAILIAN_QWEN_COMPLETE_OUTPUT or model in {
        "glm-5.2", "deepseek-v4-flash-0731", "deepseek-v4-pro-0813",
        "deepseek-v4.1-flash"}) and _bailian_compatible_endpoint(endpoint)


def has_function_tool(body: Mapping[str, Any], name: str) -> bool:
    """Inspect the prepared wire view; a matching name grants no authority."""
    tools = body.get("tools")
    return isinstance(tools, list) and any(
        isinstance(tool, Mapping) and tool.get("type") == "function"
        and isinstance(tool.get("function"), Mapping) and tool["function"].get("name") == name
        for tool in tools)


def official_deepseek_flash(model: str, endpoint: str) -> bool:
    """Exact official V4.1 Flash route for fresh stateless evidence requests.

    This is deliberately separate from Bailian output-field/history policies.
    Ordinary thinking tool continuations retain all reasoning_content; a native
    evidence request starts a new deliberation with complete instructions and
    authenticated source receipts. No partially erased continuation is sent.
    https://api-docs.deepseek.com/guides/thinking_mode/ (verified 2026-09-22)
    """
    if model != "deepseek-flash":
        return False
    try:
        parsed = urlsplit(endpoint)
        return (parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"
            and parsed.port in {None, 443} and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and parsed.path.rstrip("/") in {
                "", "/v1", "/chat/completions", "/v1/chat/completions"})
    except ValueError:
        return False


def prepare_runtime_tool_batching(body: Mapping[str, Any], *, endpoint: str, scope: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Permit multiple proposed calls; the Runtime still decides execution order.

    The caller must be a bounded primary with a native handoff/required
    consultation, or a bounded read-only consultation with its native report
    tool. Honor explicit overrides, including false. This does not enable worker
    fan-out, waive approval, or make side-effecting tools concurrency-safe.
    Verified 2026-09-08: https://help.aliyun.com/zh/model-studio/qwen-function-calling
    """
    if scope not in {"primary", "required_consultation", "consultation"}:
        raise ValueError("tool batching requires a recognized Runtime invocation scope")
    prepared = dict(body)
    models = {"qwen3.7-max", "qwen3.7-max-2026-05-20", "qwen3.7-max-2026-06-08",
              "qwen3.7-flash", "qwen3.7-flash-2026-07-15", "qwen3.8-flash"}
    if body.get("model") not in models or not _bailian_compatible_endpoint(endpoint):
        return prepared, None
    if "parallel_tool_calls" not in body:
        prepared["parallel_tool_calls"] = True
    return prepared, {"policy": "bailian-runtime-tool-batch-v2", "scope": scope,
                      "source": "explicit_request" if "parallel_tool_calls" in body else
                                ("runtime_consultation" if scope == "consultation" else "runtime_primary"),
                      "multiple_calls_requested": prepared["parallel_tool_calls"] is True,
                      "execution_order": "runtime_effect_barriers_and_terminal_handoff"}


def _bailian_compatible_endpoint(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
    except ValueError:
        return False
    host = parsed.hostname or ""
    public = host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or re.fullmatch(
        r"ws-[a-z0-9-]+\.(?:cn-beijing|ap-southeast-1|us-east-1|eu-central-1|ap-northeast-1)\.maas\.aliyuncs\.com", host,
    ) is not None
    return public and parsed.path.rstrip("/") in {
        "/compatible-mode/v1", "/compatible-mode/v1/chat/completions",
    }


def _consultation_thinking_limit(output_limit: int) -> int:
    # A bounded helper must also emit a report/tool call. This allocation is a
    # Runtime heuristic, not a provider's quality promise or an extra budget.
    return max(1, min(1024, output_limit // 3))


def prepare_consultation_thinking(
    body: Mapping[str, Any], *, endpoint: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Partition an eligible helper's *complete* output, before reserving bytes.

    Caller must verify a bounded, registered, read-only consultation with its
    native report tool. Keep this capability independent of tool batching and
    total-output support. Exact Qwen3.8 Flash and Alibaba GLM-5.2 support
    thinking_budget. Honor explicit controls (including reasoning_effort);
    never disable thinking or infer GLM effort from Qwen's mapping.
    Verified 2026-09-11: https://help.aliyun.com/zh/model-studio/deep-thinking
    https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
    """
    prepared = dict(body)
    if (body.get("model") not in {"qwen3.8-flash", "glm-5.2"} or not _bailian_compatible_endpoint(endpoint)
        or type(body.get("max_completion_tokens")) is not int or body["max_completion_tokens"] < 1):
        return prepared, None
    overrides = [key for key in ("thinking_budget", "reasoning_effort") if key in body]
    if "enable_thinking" in body and body["enable_thinking"] is not True:
        overrides.append("enable_thinking")
    policy = "bailian-glm-consultation-thinking-v1" if body["model"] == "glm-5.2" else "bailian-consultation-thinking-v1"
    diagnostic = {"policy": policy, "scope": "consultation",
                  "source": "explicit_request" if overrides else "runtime_consultation",
                  "override_fields": overrides, "guaranteed_report": False}
    if overrides:
        return prepared, diagnostic
    output_limit = body["max_completion_tokens"]
    prepared["thinking_budget"] = _consultation_thinking_limit(output_limit)
    return fit_consultation_thinking(prepared, diagnostic, output_limit=output_limit)


def fit_consultation_thinking(
    body: Mapping[str, Any], diagnostic: dict[str, Any] | None, *, output_limit: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Shrink our provisional value to the atomic grant; never enlarge the wire.

    A concurrent request may change the grant after a quote. Only reducing the
    positive numeric value after reservation cannot add unreserved input bytes.
    Explicit user controls are not clamped or rewritten by this policy.
    """
    prepared = dict(body)
    if diagnostic is None or diagnostic["source"] != "runtime_consultation":
        return prepared, diagnostic
    limit = min(int(body["thinking_budget"]), _consultation_thinking_limit(output_limit))
    prepared["thinking_budget"] = limit
    return prepared, {**diagnostic, "thinking_token_limit": limit,
                      "total_output_token_limit": output_limit,
                      "nonthinking_allowance_tokens": max(0, output_limit - limit)}


def _primary_thinking_limit(output_limit: int, phase: str) -> int:
    # Keep more reasoning room for a diagnosed repair than for implementing
    # delivered findings. These are heuristics inside the existing total cap.
    cap, divisor = (4096, 2) if phase == "candidate_repair" else (2048, 3)
    return max(1, min(cap, output_limit // divisor))


def prepare_primary_thinking(
    body: Mapping[str, Any], *, endpoint: str, phase: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Allocate output only after an authenticated primary phase transition.

    The caller must check the live isolated writer and its durable consultation
    or repair state. A tool name or prompt is not a phase transition. Do not
    alter initial planning, explicit controls, history or acceptance. On Qwen3.8
    a thinking_budget <= 4096 also maps to low effort; this is a deliberate
    phase-specific tradeoff, NOT an effort-neutral cap. Exact Alibaba GLM-5.2
    supports a thinking budget too, but this does not verify its applied effort
    or authorize removal of its preserved thinking history. Verified 2026-09-11:
    https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
    """
    if phase not in {"post_consultation", "candidate_repair"}:
        raise ValueError("primary output allocation requires a recognized execution phase")
    prepared = dict(body)
    if (body.get("model") not in {"qwen3.8-flash", "glm-5.2"} or not _bailian_compatible_endpoint(endpoint)
        or type(body.get("max_completion_tokens")) is not int or body["max_completion_tokens"] < 1):
        return prepared, None
    overrides = [key for key in ("thinking_budget", "reasoning_effort") if key in body]
    if "enable_thinking" in body and body["enable_thinking"] is not True:
        overrides.append("enable_thinking")
    policy = "bailian-glm-primary-phase-thinking-v1" if body["model"] == "glm-5.2" else "bailian-primary-phase-thinking-v1"
    diagnostic = {"policy": policy, "scope": "primary", "phase": phase,
                  "source": "explicit_request" if overrides else "runtime_primary",
                  "override_fields": overrides, "guaranteed_completion": False,
                  "authority": "output_allocation_only"}
    if overrides:
        return prepared, diagnostic
    output_limit = body["max_completion_tokens"]
    prepared["thinking_budget"] = _primary_thinking_limit(output_limit, phase)
    return fit_primary_thinking(prepared, diagnostic, output_limit=output_limit)


def fit_primary_thinking(
    body: Mapping[str, Any], diagnostic: dict[str, Any] | None, *, output_limit: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Fit only our provisional number to the atomic grant, never add bytes."""
    prepared = dict(body)
    if diagnostic is None or diagnostic["source"] != "runtime_primary":
        return prepared, diagnostic
    limit = min(int(body["thinking_budget"]), _primary_thinking_limit(output_limit, diagnostic["phase"]))
    prepared["thinking_budget"] = limit
    return prepared, {**diagnostic, "thinking_token_limit": limit,
                      "total_output_token_limit": output_limit,
                      "nonthinking_allowance_tokens": max(0, output_limit - limit),
                      **({"expected_provider_effort": "low"} if body.get("model") == "qwen3.8-flash" else {})}


def prepare_reasoning_history(body: Mapping[str, Any], *, endpoint: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Omit only history this documented deployment already ignores by default.

    This is wire normalization, not memory compaction or a token-count discount.
    Original messages/checkpoints and current reasoning output remain untouched.
    Explicit preserved thinking, ambiguous values, proxies and other providers
    must keep their continuation state. Verified 2026-09-08:
    https://help.aliyun.com/zh/model-studio/deep-thinking (传递思考过程).
    """
    prepared = dict(body)
    # Keep this capability list separate from output-cap semantics: expanding
    # one provider feature must not implicitly enable another feature.
    models = {
        "qwen3.7-max", "qwen3.7-max-2026-05-20", "qwen3.7-max-2026-06-08",
        "qwen3.7-flash", "qwen3.7-flash-2026-07-15",
    }
    if (body.get("model") not in models or not _bailian_compatible_endpoint(endpoint)
        or ("preserve_thinking" in body and body["preserve_thinking"] is not False)
        or not isinstance(body.get("messages"), list)):
        return prepared, None
    messages, count, size = [], 0, 0
    for message in body["messages"]:
        if (isinstance(message, Mapping) and message.get("role") == "assistant"
            and isinstance(message.get("reasoning_content"), str)):
            messages.append({key: value for key, value in message.items() if key != "reasoning_content"})
            count += 1
            size += len(message["reasoning_content"].encode("utf-8"))
        else:
            messages.append(message)
    prepared["messages"] = messages
    return prepared, {
        "policy": "bailian-qwen37-default-ignored-history-v1",
        "authority": "documented_provider_semantics",
        "omitted_messages": count, "omitted_utf8_bytes": size,
        "preserve_thinking": False if "preserve_thinking" in body else "absent",
    }


@dataclass(frozen=True, slots=True)
class ReasoningBudgetDecision:
    model: str
    provider: str
    requested: str
    applied: str | None
    degraded: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_reasoning_budget(
    *,
    model: str,
    runtime: dict[str, Any],
    requested: str,
) -> tuple[dict[str, Any], ReasoningBudgetDecision]:
    """Apply only declared catalog or exact, documented deployment capabilities."""

    provider = detect_provider(model, runtime, probe_openai_models=False)
    info = resolve_model_info(model, provider)
    supported = (
        info.supported_reasoning_efforts
        if info.provider is provider
        else ()
    )
    configured = dict(runtime)
    endpoint_key = {"custom": "customBaseUrl", "bailian": "bailianBaseUrl"}.get(provider.value, "openaiBaseUrl")
    bailian_deepseek = (model == "deepseek-v4-flash-0731"
                       and provider.value in {"custom", "bailian", "openai"}
                       and _bailian_compatible_endpoint(str(runtime.get(endpoint_key) or "")))
    if bailian_deepseek:
        # Deployment capability, not a prefix guess or direct DeepSeek policy.
        # https://help.aliyun.com/zh/model-studio/deepseek-api (2026-09-13)
        supported = ("low", "high", "max")
    if not supported:
        endpoint_key = {"custom": "customBaseUrl", "bailian": "bailianBaseUrl"}.get(provider.value, "openaiBaseUrl")
        reason = "provider/model catalog does not declare reasoning-budget support"
        if model.startswith("qwen3.7-") and bailian_qwen_complete_output(model, str(runtime.get(endpoint_key) or "")):
            reason = (
                "Bailian Qwen3.7 uses enable_thinking/thinking_budget, not this reasoning_effort mapping; "
                "requested effort was not applied. The model may still think under provider defaults."
            )
        return configured, ReasoningBudgetDecision(
            model=model,
            provider=provider.value,
            requested=requested,
            applied=None,
            degraded=True,
            reason=reason,
        )

    normalized = str(requested).strip().lower()
    if normalized in supported:
        applied = normalized
    elif normalized == "medium" and "high" in supported:
        applied = "high"
    elif normalized in {"xhigh", "ultra"} and "max" in supported:
        applied = "max"
    else:
        applied = supported[-1]
    if bailian_deepseek:
        configured.pop("_runtimeReasoningEffort", None)
        explicit = {**runtime.get("_openrouter_params", {}), **runtime.get("_provider_params", {})}
        if not any(key in explicit for key in ("reasoning_effort", "thinking_budget", "enable_thinking")):
            # Provenance for a request-local budget tradeoff. Direct DeepSeek's
            # separate setting does not control this Alibaba deployment.
            configured["_runtimeReasoningEffort"] = {"model": model, "effort": applied}
        configured["_provider_params"] = {**configured.get("_provider_params", {}), "reasoning_effort": applied}
    else:
        configured["deepseekReasoningEffort"] = applied
    return configured, ReasoningBudgetDecision(
        model=model,
        provider=provider.value,
        requested=normalized,
        applied=applied,
        degraded=applied != normalized,
        reason=(
            "reasoning budget applied through declared provider capability"
            if applied == normalized
            else f"requested budget mapped to supported effort {applied}"
        ),
    )


def prepare_primary_budget_effort(body: Mapping[str, Any], *, endpoint: str,
                                  runtime: Mapping[str, Any], quote, desired_output: int):
    """Trade Runtime-selected thinking effort for room to finish bounded work.

    Caller must establish a live primary with observed source reads (including
    fresh native initial observations) or an owned candidate backed by native
    successful writes. A first request without source observations stays high;
    explicit provider controls and all reasoning history remain unchanged.
    This is a disclosed quality/budget heuristic, not a smaller hard reservation
    or a completion guarantee. Exact deployment capability verified 2026-09-13:
    https://help.aliyun.com/zh/model-studio/deepseek-api#推理强度（reasoning_effort）
    """
    from minicode.usage_ledger import BudgetUnavailable

    owner = runtime.get("_runtimeReasoningEffort")
    if (body.get("model") != "deepseek-v4-flash-0731" or not _bailian_compatible_endpoint(endpoint)
        or owner != {"model": body.get("model"), "effort": body.get("reasoning_effort")}
        or body.get("reasoning_effort") != "high" or "thinking_budget" in body
        or ("enable_thinking" in body and body["enable_thinking"] is not True)
        or body.get("tool_choice", "auto") not in ("auto", "required")):
        return dict(body), None
    try:
        funded = quote(body, copies=2)["output_reservation"] >= desired_output * 2
    except BudgetUnavailable:
        funded = False
    diagnostic = {"policy": "bailian-deepseek-primary-budget-effort-v1", "source": "runtime_profile",
        "requested_effort": "high", "applied_effort": "high" if funded else "low",
        "two_requests_funded": funded, "output_token_limit_changed": False,
        "reasoning_history_changed": False, "quality_tradeoff": not funded,
        "guaranteed_completion": False, "counts_as_acceptance": False}
    return (dict(body) if funded else {**body, "reasoning_effort": "low"}), diagnostic


def apply_budget_to_existing_adapter(
    adapter: Any,
    decision: ReasoningBudgetDecision,
    *, reasoning_owner: Mapping[str, str] | None = None,
) -> bool:
    """Update an already-created compatible adapter for the Standard fast path."""

    if decision.applied is None:
        return False
    adapter_runtime = getattr(adapter, "runtime", None)
    if not isinstance(adapter_runtime, dict):
        return False
    params = dict(adapter_runtime.get("_provider_params", {}))
    params["reasoning_effort"] = decision.applied
    adapter_runtime["_provider_params"] = params
    adapter_runtime.pop("_runtimeReasoningEffort", None)
    if reasoning_owner == {"model": decision.model, "effort": decision.applied}:
        adapter_runtime["_runtimeReasoningEffort"] = dict(reasoning_owner)
    return True
