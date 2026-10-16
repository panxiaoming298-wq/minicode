"""Per-request usage collection and a Worker-local binding to the shared Runtime ledger."""

from __future__ import annotations

from minicode.runtime_profiles import parallel_candidates_enabled
from minicode.output_budget import select_output_budget, fit_output_context, context_capacity, planning_capacity, bind_output_recovery
from minicode.context_borrowing import current_context_borrowing, request_context_limit

import io
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from minicode.runtime_protocol import AgentInvocationSpec, BudgetEnvelope, new_id
from minicode.runtime_budget import (
    bailian_complete_output, has_function_tool, prepare_runtime_tool_batching,
    prepare_reasoning_history, prepare_consultation_thinking, fit_consultation_thinking,
    prepare_primary_thinking, fit_primary_thinking,
    prepare_primary_budget_effort,
)
from minicode.primary_closure import (
    has_candidate_handoff, prepare_closure_advice, prepare_consultation_closure_advice,
    prepare_readonly_closure_advice,
)
from minicode.runtime_state import ReducerError, RuntimeReducer
from minicode.runtime_request_context import RuntimeRequestContext
from minicode.runtime_protocol import NodeState, RequestedMode, ToolEffect
from minicode.worker_audit import current_worker_audit
from minicode.raw_response_evidence import (
    POLICY as RAW_CAPTURE_POLICY,
    RawCaptureLimits,
    RawResponseCapture,
    put_evidence,
)
from minicode.usage_ledger import (
    BudgetUnavailable,
    ModelUsageUnreconciled,
    ConsultationBudgetLimited,
    TokenPricing,
    add_amounts,
    nonnegative,
)

# Every fixed Runtime entry tool, by name. A live request that carries all six
# Builder tools must be diagnosable as such; the set is deliberately a literal so
# the diagnostic cannot drift with whatever a ToolRegistry happens to hold.
RUNTIME_ENTRY_TOOL_NAMES = frozenset({
    "submit_candidate", "submit_builder_candidate", "run_acceptance_checks", "submit_consultation",
    "runtime_context_read", "discover_tools", "consult_agents", "task",
    "spawn_builders", "poll_task_results", "wait_dependencies",
    "accept_builder_patch", "apply_builder_patch", "cancel_task",
    "select_builder_candidate",
    "spawn_agents", "poll_agents", "wait_agents", "cancel_agent", "send_agent_message", "resume_agent", "team_control", "reassign_agent",
})


class ProviderBalanceUnavailable(RuntimeError):
    """Provider refused billing; distinct from user cancellation or task quota."""


class RunUsageBudget:
    """Coordinate admission across Workers; the Reducer owns all durable amounts."""

    def __init__(
        self,
        reducer: RuntimeReducer,
        budget: BudgetEnvelope,
        cancellation: threading.Event,
        runtime: Mapping[str, Any],
    ) -> None:
        self.reducer = reducer
        self.budget = budget
        self.cancellation = cancellation
        self.runtime = runtime
        self._condition = threading.Condition()
        self._active: set[str] = set()
        self._provider_balance_failure: dict[str, Any] | None = None
        from minicode.token_estimation import estimate_was_exceeded
        calls = reducer.snapshot()["usage_ledger"]["calls"].values()
        self.input_estimator_failed = any(estimate_was_exceeded(call)
            for call in calls)
        self.context_input_anchors = {}
        for call in calls:
            if call.get("status") == "settled":
                usage = call.get("usage") or {}
                prepared = usage.get("prepared_request") or {}
                self.observe_context_input(call.get("node_id"),
                    prepared.get("call_identity", {}).get("purpose", "agent"),
                    prepared.get("context_input"), usage)
        # A process restart must not silently reopen a rejected run. The next
        # user turn has its own run/ledger; the old billing receipts stay intact.
        if any(call.get("usage", {}).get("response_shape", {}).get("http_error", {}).get("classification") == "provider_balance"
               for call in calls):
            self._provider_balance_failure = {"classification": "provider_balance"}

    def observe_context_input(self, node_id, purpose, diagnostic, usage):
        from minicode.context_meter import usage_anchor
        anchor = usage_anchor(diagnostic, usage)
        if anchor is not None:
            with self._condition:
                self.context_input_anchors[(node_id, purpose)] = anchor

    def reserve(
        self, invocation: AgentInvocationSpec, *,
        prepare_input_reservation: Callable[[], int] | None = None, **request: Any,
    ) -> dict[str, Any]:
        """Admit atomically, refreshing optional request-local input after waits.

        Preparation can quote current capacity, but cannot alter the configured
        output allowance or reserve capacity itself. Every retry is rechecked.
        """
        with self._condition:
            while True:
                if self._provider_balance_failure is not None:
                    raise ProviderBalanceUnavailable(
                        "Insufficient Balance: provider rejected billing; further model requests in this run are stopped. "
                        "Retained work requires an explicit continuation after balance is restored.")
                if self.cancellation.is_set() or self.reducer.node_cancel_epoch(invocation.node_id) > 0:
                    raise BudgetUnavailable("run canceled before model request")
                for cap in (self.budget, invocation.budget):
                    if cap.deadline is not None and time.time() >= cap.deadline:
                        raise BudgetUnavailable(
                            "deadline exhausted before model request"
                        )
                try:
                    prepared_request = request if prepare_input_reservation is None else {
                        **request, "input_tokens": prepare_input_reservation(),
                    }
                    record = self.reducer.reserve_model_call(
                        node_id=invocation.node_id,
                        attempt_id=invocation.attempt_id,
                        budget=self.budget,
                        attempt_budget=invocation.budget,
                        **prepared_request,
                    )
                except BudgetUnavailable as error:
                    if not self._active or error.diagnostics.get("classification") == "context_window":
                        raise
                    # A local request may free a reservation shortly. Persisted
                    # reservations from a previous process are never auto-freed.
                    self._condition.wait(timeout=0.05)
                    continue
                self._active.add(record["call_id"])
                return record

    def release(self, call_id: str) -> None:
        with self._condition:
            self._active.discard(call_id)
            self._condition.notify_all()

    def observe_provider_rejection(self, status: int, error: Mapping[str, Any]) -> bool:
        """Only explicit billing responses trip the run-wide request circuit.

        In-flight calls still settle normally; neither usage nor candidate
        authority is rewritten. A new user-requested run gets a fresh circuit.
        """
        code = str(error.get("code") or error.get("type") or "").casefold()
        message = str(error.get("message") or "").strip().casefold().replace(" ", "")
        exhausted = (status in {400, 402, 403, 429} and
            (code in {"insufficient_quota", "insufficient_balance", "insufficientbalance", "balance_not_enough"}
             or message in {"insufficientbalance", "insufficientbalance."}))
        if exhausted:
            with self._condition:
                self._provider_balance_failure = {"status": status, "classification": "provider_balance"}
                self._condition.notify_all()
        return exhausted


@dataclass(frozen=True, slots=True)
class UsageBinding:
    budget: RunUsageBudget
    invocation: AgentInvocationSpec
    request_context: RuntimeRequestContext | None = None
    post_delivery_learning: bool = False

    @property
    def read_only_primary(self) -> bool:
        # The live invocation supplies the scope, never prompt text or a saved
        # memory marker. This controls optional context cost, not tool authority.
        return (self.invocation.role == "primary"
                and self.invocation.allowed_effects == (ToolEffect.READ_ONLY,))

    @property
    def read_only_final_verifier(self) -> bool:
        return (self.invocation.node_id == "final-verifier" and self.invocation.role == "verifier"
                and self.invocation.allowed_effects == (ToolEffect.READ_ONLY,))

    @property
    def bounded(self) -> bool:
        return any(
            cap.max_tokens is not None or cap.max_cost is not None
            for cap in (self.budget.budget, self.invocation.budget)
        )


_binding: ContextVar[UsageBinding | None] = ContextVar(
    "runtime_model_usage", default=None
)


def provider_balance_blocked() -> bool:
    """Read the native run circuit, never infer billing from model/tool text."""
    binding = _binding.get()
    return binding is not None and binding.budget._provider_balance_failure is not None


class ModelContextBudget:
    """Per-step planning with the actual adapter, output policy and context meter.

    This only builds local request data. It cannot reserve usage, call a model,
    dispatch a task or change canonical history. Live state is read again at send.
    """
    def __init__(self, model, tools, *, output_recovery=None, stream=False, skill_runtime=None, config=None):
        from minicode.context_manager import resolve_context_limit
        from minicode.paged_memory.config import PagedMemoryConfig
        from minicode.context_meter import RequestContextMeter
        from minicode.tooling import bind_model_tools

        self.model, self.tools, self.stream = model, tools, stream
        self.binding = _binding.get()
        self.skill_runtime = skill_runtime
        self.runtime = {**model.runtime, **(self.binding.budget.runtime if self.binding else {})}
        self.config = config or PagedMemoryConfig.from_sources(self.runtime)
        with bind_model_tools(tools), bind_output_recovery(output_recovery):
            body, self.endpoint, self.protocol = model.context_request([], stream=stream)
            self.output_policy = select_output_budget(model.runtime, body, endpoint=self.endpoint, protocol=self.protocol)
        self.model_id = str(body.get("model", ""))
        self.output_tokens = self.output_policy["requested_tokens"]
        self.context_window = resolve_context_limit(str(body.get("model", "")), self.runtime)
        self.capacity = planning_capacity(context_window=self.context_window,
            output_policy=self.output_policy, config=self.config)
        self.input_limit = self.capacity["effective_input"]
        previous = (self.binding.budget.context_input_anchors.get((self.binding.invocation.node_id, "agent"))
                    if self.binding else None)
        self.estimator = RequestContextMeter(provider=str(model.runtime.get("provider") or self.protocol),
            endpoint=self.endpoint, protocol=self.protocol, previous=previous)

    def measure(self, messages):
        from minicode.tooling import bind_model_tools

        with bind_model_tools(self.tools):
            body, _, _ = self.model.context_request(list(messages), stream=self.stream)
        key = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
        body = {**body, key: self.output_tokens}
        if self.protocol == "openai":
            body, _ = prepare_reasoning_history(body, endpoint=self.endpoint)
        binding = self.binding
        if binding is not None and binding.invocation.role == "primary":
            if binding.budget.reducer.async_agents_enabled:
                from minicode.agent_request_context import AgentRequestView
                body = AgentRequestView(binding.budget.reducer, binding.invocation).attach(body)
            if self.protocol == "openai" and parallel_candidates_enabled(binding.budget.runtime):
                from minicode.builder_request_context import BuilderRequestView
                body = BuilderRequestView(binding.budget.reducer, binding.invocation).attach(body)
        if self.skill_runtime is not None:
            body = self.skill_runtime.request_view().attach(body)
        return self.estimator.measure(body)


@contextmanager
def bind_usage(
    budget: RunUsageBudget | None, invocation: AgentInvocationSpec,
    request_context: RuntimeRequestContext | None = None,
    *, post_delivery_learning: bool = False,
) -> Iterator[None]:
    token = _binding.set(
        UsageBinding(budget, invocation, request_context, post_delivery_learning) if budget is not None else None
    )
    try:
        yield
    finally:
        _binding.reset(token)


def observe_request_context(messages: list[dict[str, Any]], tools: Any, *, enabled: bool,
                            stages_enabled: bool = True, paged_memory_view=None, canonical_messages=None) -> None:
    binding = _binding.get()
    if binding is not None and binding.request_context is not None:
        binding.request_context.observe(messages if enabled else [], read_tools={
            tool.name for tool in tools.list() if tool.effect is ToolEffect.READ_ONLY
        } if enabled else set(), pageable_read_tools={
            tool.name for tool in tools.list() if tool.effect is ToolEffect.READ_ONLY
            and tool.metadata is not None and tool.metadata.context_result_kind == "read_data"
        } if enabled else set(), enabled=enabled, stages_enabled=stages_enabled)
        binding.request_context.observe_candidate_progress(messages if canonical_messages is None else canonical_messages)
        binding.request_context.observe_verifier_history(messages if canonical_messages is None else canonical_messages)
        binding.request_context.observe_primary_handoff_history(messages if canonical_messages is None else canonical_messages)
        if enabled and paged_memory_view is not None:
            binding.request_context.bind_paged_memory_view(paged_memory_view)


def runtime_result_is_preview() -> bool:
    """Native formatter hint only; no status is inferred from a preview."""
    binding = _binding.get()
    return bool(binding is not None and binding.request_context is not None
                and binding.request_context.labels_result_preview())


def verifier_tool_blocker():
    """A live final-judgment admission closes tools, independently of model compliance."""
    binding = _binding.get()
    if binding is not None and binding.request_context is not None:
        return binding.request_context.verifier_tool_blocker()
    return None


def native_verifier_has_terminal_decision(content: str) -> bool:
    """Classify a live final Verifier's completed assistant text, never accept it.

    A progress preamble cannot override the assignment's exact terminal marker.
    Truncation/evidence guards and Worker validation still run after this hint.
    Consultations keep their separate native submission protocol.
    """
    from minicode.verifier_decision import terminal_marker
    binding = _binding.get()
    return bool(binding is not None
        and binding.budget.runtime.get("runtimeOrchestration") == "primary"
        and binding.invocation.node_id == "final-verifier"
        and binding.invocation.role == "verifier"
        and binding.invocation.allowed_effects == (ToolEffect.READ_ONLY,)
        and terminal_marker(content) is not None)


def observe_request_tool_result(call: dict[str, Any], result: Any, tools: Any) -> None:
    """Bind concrete execution receipts to this activation's request view only."""
    audit = current_worker_audit()
    if audit is not None:
        audit.tool_result(call, result)
    binding = _binding.get()
    if binding is not None and binding.request_context is not None:
        definition = tools.find(call.get("toolName", ""))
        if definition is not None:
            binding.request_context.record_tool_result(call, result, effect=definition.effect,
                pageable_read=bool(definition.metadata and definition.metadata.context_result_kind == "read_data"),
                candidate_check=bool(definition.metadata and definition.metadata.context_result_kind == "candidate_check"))


def observe_request_tool_message(message: dict[str, Any]) -> None:
    """Observe the exact final native rendering separately from raw receipts."""
    binding = _binding.get()
    if binding is not None and binding.request_context is not None:
        binding.request_context.record_tool_message(message)


def execution_memory_rerank_calls() -> int | None:
    """Optional ranking calls in execution, primary or consultation activations.

    The ledger survives memory-object recreation. This is a ceiling on optional
    external ranking work, never permission to omit required source evidence.
    """
    binding = _binding.get()
    if binding is None:
        return None
    native_execution = (binding.request_context is not None
        and binding.request_context.has_execution_request_phase())
    # A scoped read-only helper has an explicit question and required sources.
    # Continue fresh local retrieval after its first optional external ranking;
    # the independent final verifier retains its existing retrieval policy.
    consultation = (binding.invocation.node_id.startswith(("consult-", "delegated-", "agent-"))
        and binding.invocation.role in {"explorer", "planner", "reviewer", "verifier"}
        and binding.invocation.allowed_effects == (ToolEffect.READ_ONLY,))
    if not (binding.read_only_primary or native_execution or consultation):
        return None
    return sum(call.get("attempt_id") == binding.invocation.attempt_id
        and (call.get("usage") or {}).get("prepared_request", {}).get("call_identity", {}).get("purpose") == "memory_rerank"
        for call in binding.budget.reducer.snapshot()["usage_ledger"]["calls"].values())


def current_usage_observation() -> dict[str, Any]:
    binding = _binding.get()
    if binding is None:
        return {"tokens": None, "cost": None, "model_calls": 0, "budget_pressure": 0.0}
    reducer = binding.budget.reducer
    usage = reducer.usage_summary(binding.invocation.attempt_id)
    total = reducer.usage_summary()
    pressure = 0.0
    effective = reducer.effective_model_budget(binding.invocation.node_id, binding.budget.budget)
    for cap, values in ((effective, total), (binding.invocation.budget, usage)):
        if cap.max_tokens is not None:
            pressure = max(pressure, (values["tokens"] + values["reserved_tokens"]) / max(1, cap.max_tokens))
        if cap.max_cost is not None:
            pressure = max(pressure, (values["known_cost"] + values["reserved_cost"]) / cap.max_cost if cap.max_cost > 0 else 1.0)
    return {**usage, "budget_pressure": min(1.0, pressure),
            **({"closure_budget": reducer.closure_budget_snapshot()} if reducer.closure_reserve_enabled else {})}


def resolve_pricing(
    runtime: Mapping[str, Any], provider: str, model: str
) -> TokenPricing | None:
    configured = runtime.get("runtimeModelPricing", {})
    entry = (
        configured.get(f"{provider}:{model}")
        if isinstance(configured, Mapping)
        else None
    )
    if not isinstance(entry, Mapping):
        return None
    # Cache creation can cost more than ordinary input, so it has no guessed default.
    if not all(key in entry for key in ("input", "output", "cacheRead", "cacheWrite")):
        raise BudgetUnavailable(
            "runtimeModelPricing needs input/output/cacheRead/cacheWrite rates"
        )
    return TokenPricing(
        entry["input"], entry["output"], entry["cacheRead"], entry["cacheWrite"]
    )


def normalize_usage(
    raw: Mapping[str, Any], protocol: str, pricing: TokenPricing | None
) -> dict[str, Any]:
    """Normalize cumulative counters, including cached input and reasoning output."""
    input_key, output_key = (
        ("prompt_tokens", "completion_tokens")
        if protocol == "openai"
        else ("input_tokens", "output_tokens")
    )

    def counter(value: Any, key: str) -> int:
        amount = nonnegative(value, key)
        if not amount.is_integer():
            raise ValueError(f"{key} must be an integer token counter")
        return int(amount)

    input_tokens = counter(raw.get(input_key, 0), input_key)
    output_tokens = counter(raw.get(output_key, 0), output_key)
    cached = created = 0
    cache_known = False
    if protocol == "openai":
        details = raw.get("prompt_tokens_details") or {}
        cached = counter(details.get("cached_tokens", raw.get("prompt_cache_hit_tokens", 0)), "cached_tokens")
        cache_known = "cached_tokens" in details or "prompt_cache_hit_tokens" in raw
        created = counter(details.get("cache_write_tokens", 0), "cache_write_tokens")
    elif protocol == "anthropic":
        cache_known = "cache_read_input_tokens" in raw
        cached = counter(
            raw.get("cache_read_input_tokens", 0), "cache_read_input_tokens"
        )
        created = counter(
            raw.get("cache_creation_input_tokens", 0), "cache_creation_input_tokens"
        )
        input_tokens += cached + created
    if cached + created > input_tokens:
        raise ValueError("cache token counters exceed total input")
    complete = input_key in raw and output_key in raw
    total = max(
        input_tokens + output_tokens,
        counter(raw.get("total_tokens", 0), "total_tokens"),
    )
    reported_cost = raw.get("cost")
    if reported_cost is not None:
        cost = nonnegative(reported_cost, "provider cost")
        source = "provider"
    elif pricing is not None and complete and total == input_tokens + output_tokens:
        cost = pricing.cost(input_tokens, output_tokens, cached, created)
        source = "configured_rates"
    else:
        cost, source = None, "unknown"
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cached,
        "cache_tokens_known": cache_known and complete,
        "cache_creation_tokens": created,
        "uncached_input_tokens": input_tokens - cached - created,
        "tokens": total,
        "cost": cost,
        "cost_source": source,
        "tokens_known": complete,
    }
    # A subset of output tokens, never an extra charge. Absence means unknown.
    details = raw.get("completion_tokens_details") if protocol == "openai" else None
    if isinstance(details, Mapping) and details.get("reasoning_tokens") is not None:
        reasoning = counter(details["reasoning_tokens"], "reasoning_tokens")
        if reasoning > output_tokens:
            raise ValueError("reasoning tokens exceed total output tokens")
        result["reasoning_tokens"] = reasoning
    return result


def input_reservation(body: Mapping[str, Any]) -> int:
    """Conservative local text estimate including tool schemas and framing.

    This is an admission estimate, not a provider token-count API. Opaque media
    cannot be bounded with it and must not use a hard text-token/cost budget.
    """
    return (
        len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        + 256
    )


def prepared_request_diagnostics(body: Mapping[str, Any], *, complete_output: bool) -> dict[str, Any]:
    """Non-content diagnostics of the prepared request, not provider acknowledgement.

    UTF-8 sizes help locate repeated context/schema overhead; they are NOT token
    counts and never replace the ledger's reservations or provider usage.
    """
    def size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    key = next((name for name in ("max_completion_tokens", "max_tokens") if name in body), None)
    roles: dict[str, dict[str, int]] = {}
    for message in body.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        role = role if isinstance(role, str) and role in {"system", "developer", "user", "assistant", "tool"} else "other"
        part = roles.setdefault(role, {"count": 0, "serialized_utf8_bytes": 0,
            "content_utf8_bytes": 0, "reasoning_utf8_bytes": 0, "tool_calls_utf8_bytes": 0})
        part["count"] += 1
        part["serialized_utf8_bytes"] += size(message)
        for field, metric in (("content", "content_utf8_bytes"), ("reasoning_content", "reasoning_utf8_bytes"),
                              ("tool_calls", "tool_calls_utf8_bytes")):
            if field in message:
                part[metric] += size(message[field])
    return {
        "messages_utf8_bytes": size(body.get("messages", [])),
        "tools_utf8_bytes": size(body.get("tools", [])),
        "message_count": len(body.get("messages") or []),
        "tool_count": len(body.get("tools") or []),
        # Only fixed Runtime entry names: user/MCP tool names may themselves
        # contain private data. Full schemas are represented by their hash.
        # Every tool the Runtime itself registers belongs here, including the
        # Builder lifecycle tools: omitting them made a live request that DID
        # carry all six look like a request that carried none.
        "runtime_tool_names": [tool.get("function", tool).get("name") for tool in (body.get("tools") or [])
            if isinstance(tool, Mapping) and isinstance(tool.get("function", tool), Mapping)
            and tool.get("function", tool).get("name") in RUNTIME_ENTRY_TOOL_NAMES][:256],
        "tool_schema_sha256": hashlib.sha256(json.dumps(body.get("tools", []), ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")).hexdigest(),
        "message_roles": roles,
        "output_parameter": key,
        "output_token_limit": body.get(key) if key else None,
        "output_scope": "thinking_and_answer" if complete_output and key else "provider_specific",
        "output_tolerance_tokens": 10 if complete_output and key else None,
    }


def _has_media(value: Any) -> bool:
    if isinstance(value, Mapping):
        # Tool schemas also have a `type`: it may be a union array or a nested
        # property descriptor, not a message content tag. Never hash those.
        content_type = value.get("type")
        if isinstance(content_type, str) and content_type in {
            "image",
            "image_url",
            "input_image",
            "input_audio",
            "file",
            "document",
        }:
            return True
        return any(_has_media(item) for item in value.values())
    return isinstance(value, list) and any(_has_media(item) for item in value)


def wire_request_record(body: Mapping[str, Any], wire_body: bytes,
                        record: Mapping[str, Any] | None) -> dict[str, Any]:
    """The output allowance the final serialized request really carried.

    The ledger's ``output_reservation`` adds the documented provider output
    tolerance on top of the cap that is actually sent, so the two numbers differ
    by design. Diagnosing "the provider used its whole output allowance" against
    the reservation would silently never fire; diagnosing it against this record
    compares a real provider counter with the exact field on the wire. The
    tolerance is reported here as its own field, never removed from the ledger.
    """
    if not isinstance(body, Mapping):
        raise TypeError("wire request evidence requires the serialized request body")
    key = next((name for name in ("max_completion_tokens", "max_tokens") if name in body), None)
    value = body.get(key) if key else None
    cap = value if type(value) is int and value > 0 else None
    reservation = record.get("output_reservation") if isinstance(record, Mapping) else None
    reservation = reservation if type(reservation) is int else None
    return {
        "schema": "model-wire-request-v1",
        "output_cap_source": "final_serialized_wire_body",
        "wire_body_sha256": hashlib.sha256(wire_body).hexdigest(),
        "wire_body_bytes": len(wire_body),
        "output_parameter": key,
        "output_cap_tokens": cap,
        "output_reservation_tokens": reservation,
        "output_tolerance_tokens": (
            reservation - cap if reservation is not None and cap is not None else None),
    }


def model_call_identity(model: str, provider: str, request_url: str) -> dict[str, str]:
    """A credential-free identity of the actual HTTP destination, not its alias."""
    return {"model": model, "provider": provider,
            "endpoint_sha256": hashlib.sha256(request_url.rstrip("/").encode()).hexdigest()}


class ProviderFirstOutputTimeout(TimeoutError):
    kind = "provider_first_output_timeout"


class ProviderCall:
    """Collect every HTTP attempt, even when a retry or stream parser fails."""

    def __init__(self, runtime: Mapping[str, Any], protocol: str, *, purpose: str = "agent") -> None:
        if purpose not in {"agent", "memory_rerank", "memory_consolidate"}:
            raise ValueError("unregistered model-call purpose")
        self.purpose = purpose
        self.runtime = runtime
        self.protocol = protocol
        self.binding = _binding.get()
        self.provider = str(runtime.get("provider") or protocol)
        self.model = str(runtime.get("model") or "unknown")
        pricing_runtime = self.binding.budget.runtime if self.binding else runtime
        self.pricing = resolve_pricing(pricing_runtime, self.provider, self.model)
        self.record: dict[str, Any] | None = None
        self.raw_usage: dict[str, Any] = {}
        self.request_diagnostics: dict[str, Any] = {}
        self.response_diagnostics: dict[str, Any] = {}
        self.provider_failure: dict[str, Any] | None = None
        self.complete = False
        self._rate_limit_rejected = False
        self.outcome = "succeeded"
        self.response: Any = None
        self._finished: list[dict[str, Any]] = []
        self.audit = current_worker_audit()
        self._audit_declined = False
        self.attempt_count = 0
        self.http_attempt_count = 0
        self.raw_capture: RawResponseCapture | None = None
        self.raw_capture_receipt: dict[str, Any] | None = None
        self.raw_capture_record: dict[str, Any] | None = None
        # The output field the final serialized wire body actually carried, with
        # its hash. Distinct from the ledger reservation, which deliberately adds
        # the documented provider output tolerance on top of that cap.
        self.wire_request: dict[str, Any] | None = None
        self.stream_integrity: dict[str, Any] | None = None
        self._http_started_at: float | None = None
        self.context_snapshot: dict[str, Any] | None = None
        self.image_input_ids: list[str] = []
        self.first_output_latency_ms: float | None = None
        try:
            limit = float(os.environ.get("MINICODE_MODEL_FIRST_OUTPUT_TIMEOUT", "120"))
            self.first_output_timeout = limit if math.isfinite(limit) and limit > 0 else 120.0
        except ValueError:
            self.first_output_timeout = 120.0

    def check_first_output(self) -> None:
        """Heartbeats keep HTTP alive, but cannot extend waiting for useful output.

        Called on every stream frame. Silent sockets still use the transport's
        read timeout; after the first real delta only the ordinary limits apply.
        """
        self.check()
        if (self._http_started_at is not None and self.first_output_latency_ms is None
            and time.monotonic() - self._http_started_at >= self.first_output_timeout):
            raise ProviderFirstOutputTimeout(
                f"No model text, reasoning or tool output within {self.first_output_timeout:g}s; "
                "connection heartbeats do not count as output. Usage may be unreported.")

    def observe_first_output(self) -> None:
        """First nonempty streamed text, reasoning or tool delta; never header latency."""
        if self._http_started_at is not None and self.first_output_latency_ms is None:
            self.first_output_latency_ms = round((time.monotonic() - self._http_started_at) * 1000, 3)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is not None and self.record is not None and not self.complete:
            self.response_diagnostics["transport_error"] = {"type": type(exc).__name__,
                "kind": getattr(exc, "kind", None)}
        try:
            self._finish("failed" if exc_type else self.outcome)
        finally:
            if self.response is not None:
                close = getattr(self.response, "close", None)
                if callable(close):
                    close()
        if exc_type is None and self.binding:
            # A sibling request (including an auxiliary call in this attempt)
            # can acquire the released reservation before we get here. Its
            # pending bill is normal in-flight work, not this call's unknown
            # usage. Check our own completed HTTP attempts for reconciliation;
            # keep run/attempt totals for the actual ceiling checks below.
            completed = self.usage
            usage = self.binding.budget.reducer.usage_summary()
            local = self.binding.budget.reducer.usage_summary(
                self.binding.invocation.attempt_id
            )
            for cap, totals in (
                (self.binding.budget.budget, usage),
                (self.binding.invocation.budget, local),
            ):
                if ((cap.max_tokens is not None and not completed["tokens_known"])
                    or (cap.max_cost is not None and not completed["cost_known"])):
                    raise ModelUsageUnreconciled("model usage is incomplete; reconciliation is required",
                        diagnostics=self.binding.budget.reducer._state.usage_ledger.reconciliation_diagnostics())
                if cap.max_tokens is not None and totals["tokens"] > cap.max_tokens:
                    raise BudgetUnavailable("model token usage exceeds the budget")
                if cap.max_cost is not None and totals["known_cost"] > cap.max_cost:
                    raise BudgetUnavailable("model cost exceeds the budget")

    def check(self) -> None:
        if self.binding:
            if (self.binding.budget.cancellation.is_set()
                or self.binding.budget.reducer.node_cancel_epoch(self.binding.invocation.node_id) > 0):
                raise BudgetUnavailable("run canceled during model request")
            for cap in (self.binding.budget.budget, self.binding.invocation.budget):
                if cap.deadline is not None and time.time() >= cap.deadline:
                    raise BudgetUnavailable("deadline exhausted during model request")

    def wait_for_retry(self, seconds: float) -> None:
        """Keep provider backoff inside the run's cancellation/deadline guard."""
        until = time.monotonic() + seconds
        while True:
            self.check()
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def finish_provider_failure(self, diagnostic: dict[str, Any]) -> None:
        # Independent of the raw-body prefix limit: the terminal failure's
        # controlled labels and request id survive even a very long stream.
        self.provider_failure = dict(diagnostic)
        self.outcome = "failed"
        self._finish("failed")

    def _prepare_prompt_cache(self, body, endpoint):
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
        runtime = self.binding.budget.runtime if self.binding else self.runtime
        invocation = self.binding.invocation if self.binding else None
        return createApp().prepare_prompt_cache(body, mode=runtime.get("runtimePromptCache", "off"),
            protocol=self.protocol, provider=self.provider, endpoint=endpoint or "",
            namespace=invocation.run_id if invocation else "",
            permissions=[effect.value for effect in invocation.allowed_effects] if invocation else [])

    def begin(self, body: Mapping[str, Any], *, request_url: str | None = None) -> dict[str, Any]:
        self._audit_declined = False
        try:
            return self._begin(body, request_url=request_url)
        except BudgetUnavailable as error:
            if self.audit is not None and not self._audit_declined:
                self.audit.request("request_rejected_before_preparation", dict(body), purpose=self.purpose,
                    protocol=self.protocol, http_sent=False, error_type=type(error).__name__,
                    admission=error.diagnostics)
            raise

    def _begin(self, body: Mapping[str, Any], *, request_url: str | None = None) -> dict[str, Any]:
        self._finish(self.outcome)
        self.check()
        borrowing = current_context_borrowing()
        if borrowing is not None and borrowing.handoff_only:
            borrowing.validate_request(body, purpose=self.purpose, protocol=self.protocol,
                endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""))
        identity = model_call_identity(self.model, self.provider, request_url or "")
        # This private evaluation constraint belongs to the shared Run budget.
        # A fallback adapter cannot remove it by replacing its local runtime.
        policy = self.binding.budget.runtime.get("_modelCallPolicy") if self.binding else None
        if policy is not None and (not request_url or not isinstance(policy, Mapping)
                                   or policy.get(self.purpose) != identity):
            self.binding.budget.reducer.record_model_request_declined(
                self.binding.invocation.node_id, self.binding.invocation.attempt_id,
                {"schema_version": "model-request-declined-v1", "http_sent": False,
                 "ledger_entry_created": False, "classification": "model_route",
                 "purpose": self.purpose, "identity": identity})
            raise BudgetUnavailable("model request differs from the frozen purpose/provider/destination policy")
        from minicode.image_input import image_reservation
        try:
            image_reservation(dict(body), request_url or "")
        except ValueError as error:
            if self.binding and self.binding.bounded:
                raise BudgetUnavailable(str(error)) from error
        if (
            self.binding
            and self.binding.bounded
            and body.get("n", 1) != 1
        ):
            raise BudgetUnavailable(
                "hard budget requires a single text completion with countable input"
            )
        if self.binding and self.binding.bounded and body.get("model", self.model) != self.model:
            raise BudgetUnavailable("request model differs from the model bound to usage accounting")
        from minicode.token_estimation import RequestInputEstimator
        estimator = RequestInputEstimator(provider=self.provider, endpoint=request_url or "",
            disabled=lambda: self.binding is None or self.binding.budget.input_estimator_failed
                or self.binding.budget.runtime.get("runtimeTokenEstimation") != "official-tokenizer")
        measure_input = estimator.measure
        from minicode.context_meter import RequestContextMeter
        previous = (self.binding.budget.context_input_anchors.get((self.binding.invocation.node_id, self.purpose))
                    if self.binding else None)
        context_meter = RequestContextMeter(provider=self.provider, endpoint=request_url or "",
            protocol=self.protocol, previous=previous)
        measure_context = context_meter.measure
        complete_output = self.protocol == "openai" and bailian_complete_output(
            self.model, request_url or str(self.runtime.get("openaiBaseUrl") or ""),
        )
        # Bailian documents an output-count tolerance of up to 10 tokens. Reserve
        # it as OUTPUT, including its output-price cost, not as cheap input.
        tolerance = 10 if complete_output else 0
        output_policy = (select_output_budget(self.runtime, body,
            endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or self.runtime.get("baseUrl") or ""),
            protocol=self.protocol) if self.purpose == "agent" else None)
        desired = (output_policy["requested_tokens"] if output_policy is not None else int(
            body.get("max_completion_tokens") or body.get("max_tokens") or 4096))
        self.raw_usage = {}
        self.request_diagnostics = {}
        self.response_diagnostics = {}
        self.provider_failure = None
        self.first_output_latency_ms = None
        self._http_started_at = None
        self.context_snapshot = None
        self.complete = False
        self._rate_limit_rejected = False
        self.outcome = "succeeded"
        self.raw_capture = None
        self.raw_capture_receipt = None
        self.raw_capture_record = None
        self.wire_request = None
        self.stream_integrity = None
        body = dict(body)
        body, cache_diagnostic = self._prepare_prompt_cache(body, request_url)
        output_key = None
        if output_policy is not None or (self.binding and self.binding.bounded) or (complete_output and ("max_tokens" in body or "max_completion_tokens" in body)):
            output_key = (
                "max_completion_tokens"
                if complete_output or "max_completion_tokens" in body
                or (self.protocol == "openai" and self.provider == "openai"
                    and urllib.parse.urlparse(str(self.runtime.get("openaiBaseUrl", "https://api.openai.com/v1"))).hostname == "api.openai.com")
                else "max_tokens"
            )
            body[output_key] = desired
            body.pop("max_tokens" if output_key == "max_completion_tokens" else "max_completion_tokens", None)
        history_diagnostics = None
        if self.protocol == "openai":
            # Use the fully assembled request (including provider overrides)
            # and actual destination. Reserve exactly the normalized wire body,
            # never merely discount ignored bytes while still sending them.
            body, history_diagnostics = prepare_reasoning_history(
                body, endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""),
            )
        primary_runtime = (self.protocol == "openai" and self.binding is not None and self.binding.bounded
                           and self.binding.budget.runtime.get("runtimeOrchestration") == "primary")
        read_only_primary_request = (self.purpose == "agent" and self.binding is not None
            and self.binding.read_only_primary
            and self.binding.budget.runtime.get("runtimeOrchestration") == "primary")
        read_only_verifier_request = (self.purpose == "agent" and self.binding is not None
            and self.binding.read_only_final_verifier
            and self.binding.budget.runtime.get("runtimeOrchestration") == "primary")
        primary_request = bool(primary_runtime and self.binding.invocation.role == "primary"
                               and has_candidate_handoff(body))
        batching_scope = "primary" if primary_request else None
        if primary_runtime and not batching_scope:
            invocation = self.binding.invocation
            if invocation.role == "primary" and has_function_tool(body, "consult_agents"):
                process = self.binding.budget.reducer.required_consultation_status()
                if process and not process["satisfied"] and not process["blocked"]:
                    batching_scope = "required_consultation"
            elif (invocation.node_id.startswith(("consult-", "delegated-"))
                  and invocation.role in {"explorer", "planner", "reviewer", "verifier"}
                  and invocation.allowed_effects == (ToolEffect.READ_ONLY,)
                  and has_function_tool(body, "submit_consultation")):
                batching_scope = "consultation"
        batching_diagnostics = closure_diagnostics = None
        if batching_scope:
            body, batching_diagnostics = prepare_runtime_tool_batching(
                body, endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""),
                scope=batching_scope,
            )
        thinking_diagnostics = None
        if batching_scope == "consultation":
            body, thinking_diagnostics = prepare_consultation_thinking(
                body, endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""),
            )
        elif primary_request and body.get("tool_choice", "auto") in ("auto", "required"):
            phase = self._primary_output_phase()
            if phase is not None:
                body, thinking_diagnostics = prepare_primary_thinking(
                    body, endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""), phase=phase,
                )
        context_tool_diagnostics = None
        recovery_diagnostics = None
        source_diagnostics = None
        if batching_scope == "consultation" and self.binding.request_context is not None:
            body, source_diagnostics = self.binding.request_context.prepare_initial_context_view(body)
            body, recovery_diagnostics = self.binding.request_context.prepare_consultation_recovery(
                body, endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""))
            body, context_tool_diagnostics = self.binding.request_context.prepare_tool_view(body)
        # Quote the normalized wire shape. Parameter renaming must not add
        # unreserved input bytes after admission; a smaller grant only shrinks it.
        before_input = measure_input(body)
        projected_count = delivery_count = projected_input_count = 0
        consultation_input_count = consultation_receipt_count = 0
        consultation_preview_count = 0
        delivery_audit_count = 0
        stage_diagnostics = None
        compiler_diagnostics = None
        paged_memory_diagnostics = None
        effort_diagnostics = None
        builder_view = None
        if (self.protocol == "openai" and self.purpose == "agent" and self.binding is not None
            and self.binding.invocation.role == "primary"
            and parallel_candidates_enabled(self.binding.budget.runtime)):
            from minicode.builder_request_context import BuilderRequestView
            archive = (self.binding.request_context.archive_completed_builder_inventory
                if self.binding.request_context is not None and any(
                    item.get("function", {}).get("name") == "runtime_context_read" for item in body.get("tools", [])) else None)
            builder_view = BuilderRequestView(self.binding.budget.reducer, self.binding.invocation, archive=archive,
                merge_progress=(self.binding.request_context._builder_merge_progress
                    if self.binding.request_context is not None else None))
        agent_view = None
        if (self.protocol in {"openai", "anthropic"} and self.purpose == "agent" and self.binding is not None
            and self.binding.invocation.role == "primary" and self.binding.budget.reducer.async_agents_enabled):
            from minicode.agent_request_context import AgentRequestView
            agent_view = AgentRequestView(self.binding.budget.reducer, self.binding.invocation)
        from minicode.skill_runtime import current_skill_runtime
        skill_runtime = current_skill_runtime() if self.purpose == "agent" else None
        self.skill_view = skill_runtime.request_view() if skill_runtime is not None else None
        if self.skill_view is not None and self.skill_view.message is None:
            self.skill_view = None
        state_views = [view for view in (builder_view, agent_view, self.skill_view) if view is not None]
        # Required live state participates in output fitting too. Previously it
        # was appended only after the fit, making an otherwise valid auto-sized
        # request fail the async-state guard. Canonical history is untouched.
        if output_policy is not None:
            from minicode.paged_memory.config import PagedMemoryConfig
            capacity_runtime = {**self.runtime, **(self.binding.budget.runtime if self.binding else {})}
            config = PagedMemoryConfig.from_sources(capacity_runtime)
            complete_body = body
            for state_view in state_views:
                complete_body = state_view.attach(complete_body)
            output_policy = fit_output_context(output_policy, input_tokens=measure_context(complete_body) + tolerance,
                context_window=request_context_limit(self.model, capacity_runtime),
                safety_tokens=config.safety_token_reserve)
            desired = output_policy["requested_tokens"]
            body[output_key] = desired
            before_input = measure_input(body)
        compiled_primary = False
        if self.binding:
            call_id = new_id("model-call")
            if self.binding.post_delivery_learning and (self.purpose != "memory_consolidate" or body.get("tools")):
                raise BudgetUnavailable("post-delivery admission is only for tool-free memory consolidation")
            # An explicit report allowance retains the shared-worker contract.
            # Automatic allowances may fit a smaller atomic ledger grant; a high
            # vendor default must not itself prevent a funded helper from working.
            full_output_required = (self.binding.budget.reducer.uses_shared_consultation_budget(self.binding.invocation.node_id)
                and (output_policy is None or output_policy["mode"] == "manual"))
            minimum_output = (desired if full_output_required else
                output_policy["minimum_tokens"] if output_policy is not None else 1) + tolerance
            request = dict(call_id=call_id, model=self.model, provider=self.provider,
                           output_tokens=desired + tolerance, minimum_output_tokens=minimum_output,
                           pricing=self.pricing)
            if self.binding.post_delivery_learning:
                request["post_delivery_learning"] = True
            def quote(candidate: Mapping[str, Any], *, copies: int = 1) -> dict[str, Any]:
                for state_view in state_views:
                    candidate = state_view.attach(candidate)
                return self.binding.budget.reducer.quote_model_call(
                    node_id=self.binding.invocation.node_id, attempt_id=self.binding.invocation.attempt_id,
                    budget=self.binding.budget.budget, attempt_budget=self.binding.invocation.budget,
                    **{**request, "input_tokens": measure_input(candidate) * copies,
                       "output_tokens": (desired + tolerance) * copies,
                       "minimum_output_tokens": minimum_output * copies},
                )
            if self.protocol == "openai" and self.binding.bounded and self.binding.request_context is not None:
                # Every bounded Worker in this orchestration shares the same
                # ledger. In particular, the final verifier must not rebuild
                # optional memory at window size after primary-only paging.
                # The native view still requires an exact body/reader match;
                # auxiliary calls cannot inherit another request's projection.
                if primary_runtime and self.binding.budget.reducer.closure_reserve_enabled and self.purpose == "agent":
                    body, paged_memory_diagnostics = self.binding.request_context.project_paged_memory(
                        body, quote=quote, desired_output=desired + tolerance, measure=measure_input,
                        endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""),
                        read_only_primary=read_only_primary_request,
                        read_only_verifier=read_only_verifier_request,
                    )
                bound_handoff = bool(paged_memory_diagnostics
                    and paged_memory_diagnostics.get("selected") is True
                    and paged_memory_diagnostics.get("policy") in {
                        "native-primary-handoff-deliberation-v1", "native-builder-work-deliberation-v1"})
                # A native phase has already bound its exact request. Generic
                # historical paging must not rewrite that packet or the new
                # thinking/tool continuation afterward. Admission below still
                # requires the complete output and revalidates ownership.
                if not bound_handoff and primary_runtime and self.binding.invocation.role == "primary" and self.binding.request_context.compiles_primary_request():
                    from minicode.runtime_request_compiler import compile_primary_request
                    def finish(candidate, *, working_context=None):
                        return (prepare_closure_advice(candidate, quote=quote, desired_output=desired + tolerance,
                            working_context=working_context)
                            if primary_request and self.binding.budget.reducer.closure_reserve_enabled else (dict(candidate), None))
                    compiled = compile_primary_request(self.binding.request_context, body,
                        endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""),
                        quote=quote, desired_output=desired + tolerance, finish=finish, measure=measure_input)
                    body, stage_diagnostics, closure_diagnostics = compiled.body, compiled.stage, compiled.closure
                    projected_count, delivery_count = compiled.projected_results, compiled.projected_deliveries
                    compiler_diagnostics = compiled.diagnostic
                    compiled_primary = True
                elif not bound_handoff:
                    if primary_runtime and self.binding.invocation.role == "primary":
                        body, stage_diagnostics = self.binding.request_context.prepare_stage_view(
                            body, endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""))
                    # With paged memory, plan for the next equal-size request too.
                    # This only pages native historical data; final admission still
                    # quotes/reserves one real request including all reasoning.
                    def context_quote(candidate):
                        if paged_memory_diagnostics is None:
                            return quote(candidate)
                        result = quote(candidate, copies=2)
                        return {**result, "output_reservation": result["output_reservation"] // 2}
                    body, projected_count, delivery_count = self.binding.request_context.project(
                        body, quote=context_quote, desired_output=desired + tolerance,
                    )
                if not bound_handoff:
                    projected_input_count = self.binding.request_context.projected_tool_inputs
                    consultation_input_count = self.binding.request_context.projected_consultation_inputs
                    consultation_receipt_count = self.binding.request_context.projected_consultation_receipts
                    consultation_preview_count = self.binding.request_context.projected_consultation_previews
                    delivery_audit_count = self.binding.request_context.projected_delivery_audit_pages
                if stage_diagnostics is not None and stage_diagnostics.get("phase") == "candidate_handoff":
                    stage_diagnostics["consultation_metadata_pages"] = self.binding.request_context.projected_candidate_metadata_pages
            if (not compiled_primary and self.protocol == "openai"
                    and self.binding.request_context is not None
                    and (self.binding.request_context.has_primary_request_phase()
                         or read_only_primary_request or read_only_verifier_request)
                    and paged_memory_diagnostics is None):
                # Unbounded protocol runs still prepare their native memory view
                # before admission. Quoting after reserve would reuse an already
                # committed call ID and could change unreserved request bytes.
                body, paged_memory_diagnostics = self.binding.request_context.project_paged_memory(
                    body, quote=quote, desired_output=desired + tolerance, measure=measure_input,
                    endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""),
                    read_only_primary=read_only_primary_request, read_only_verifier=read_only_verifier_request)
            if (primary_request and self.binding.budget.reducer.closure_reserve_enabled
                and paged_memory_diagnostics is not None
                and paged_memory_diagnostics.get("reason") != "request_or_reader_unavailable"
                and (effort_basis := self.binding.request_context.primary_budget_effort_evidence()) is not None):
                body, effort_diagnostics = prepare_primary_budget_effort(body,
                    endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""),
                    runtime=self.runtime, quote=quote, desired_output=desired + tolerance)
                if effort_diagnostics is not None:
                    effort_diagnostics["evidence_origin"] = effort_basis
            after_context_input = compiled.before_advice_input if compiled_primary else measure_input(body)
            prepare_input = None
            if primary_request and self.binding.budget.reducer.closure_reserve_enabled and not compiled_primary:
                body, closure_diagnostics = prepare_closure_advice(body, quote=quote, desired_output=desired + tolerance)
            elif (read_only_primary_request and self.binding.bounded
                  and self.binding.budget.reducer.closure_reserve_enabled):
                body, closure_diagnostics = prepare_readonly_closure_advice(
                    body, quote=quote, desired_output=desired + tolerance)
            elif batching_scope == "consultation" and full_output_required:
                unadvised_body = body
                def prepare_input() -> int:
                    nonlocal body, closure_diagnostics
                    # A sibling may release capacity inside reserve's wait loop.
                    # Always start from the same lossless, unadvised request;
                    # do not accumulate stale messages or silently shrink output.
                    body, closure_diagnostics = dict(unadvised_body), None
                    if self._consultation_closure_eligible():
                        body, closure_diagnostics = prepare_consultation_closure_advice(
                            body, quote=quote, desired_output=desired + tolerance,
                        )
                    return measure_input(body)
            if (paged_memory_diagnostics is not None
                and paged_memory_diagnostics.get("policy") == "native-verifier-evidence-v1"
                and paged_memory_diagnostics.get("selected") is True):
                # A projected evidence packet must still have its live owner
                # after any reservation wait. Keep the full judgment allowance;
                # a quote alone cannot turn a smaller grant into completion.
                request["minimum_output_tokens"] = desired + tolerance
                verifier_body = body
                verifier_diagnostics = dict(paged_memory_diagnostics)
                def prepare_input() -> int:
                    nonlocal body, paged_memory_diagnostics
                    body, judgment_diagnostic = self.binding.request_context.prepare_verifier_judgment(
                        verifier_body, quote=quote, desired_output=desired + tolerance, measure=measure_input)
                    paged_memory_diagnostics = {**verifier_diagnostics, "final_judgment": judgment_diagnostic}
                    if judgment_diagnostic["selected"]:
                        paged_memory_diagnostics.update(phase="final_judgment", paged_observation_handles=[],
                            packet_sha256=judgment_diagnostic["evidence_packet_sha256"],
                            input_reservation_after=measure_input(body),
                            complete_output_quoted=judgment_diagnostic["full_judgment_output_quoted"])
                    return measure_input(body)
            if (paged_memory_diagnostics is not None
                and paged_memory_diagnostics.get("policy") in {"native-primary-handoff-deliberation-v1", "native-builder-work-deliberation-v1"}
                and paged_memory_diagnostics.get("selected") is True):
                # Evidence deliberation and Builder continuations retain the complete output cap.
                # Recheck native ownership, canonical input and candidate bytes
                # inside reserve, then once more before returning a wire body.
                request["minimum_output_tokens"] = desired + tolerance
                def prepare_input() -> int:
                    self.binding.request_context.validate_primary_handoff(body)
                    return measure_input(body)
            if (paged_memory_diagnostics is not None
                and paged_memory_diagnostics.get("policy") == "native-primary-argument-recovery-v1"
                and paged_memory_diagnostics.get("selected") is True):
                # Bounded argument-error recovery keeps the ordinary complete
                # output allowance; nothing is renegotiated to force admission.
                # Ownership, canonical input and candidate bytes are rechecked
                # inside reserve, then once more before a wire body returns.
                request["minimum_output_tokens"] = desired + tolerance
                recovery_body = body
                def prepare_input() -> int:
                    self.binding.request_context.validate_primary_recovery(recovery_body)
                    return measure_input(recovery_body)
            if state_views:
                prior_prepare = prepare_input
                def prepare_input() -> int:
                    nonlocal body
                    for state_view in state_views:
                        body = state_view.detach(body)
                        state_view.refresh()
                    if prior_prepare is not None:
                        prior_prepare()
                    for state_view in state_views:
                        body = state_view.attach(body)
                    input_tokens = measure_input(body)
                    return input_tokens
            try:
                if output_policy is not None:
                    # Projections, closure advice and refreshed native state can
                    # change the request after the first fit. Refit once before
                    # the ledger reservation; recheck on every reservation wait.
                    prior_prepare_input = prepare_input
                    if prior_prepare_input:
                        prior_prepare_input()
                    full_context_output = (full_output_required or bool(paged_memory_diagnostics
                        and paged_memory_diagnostics.get("selected") is True
                        and paged_memory_diagnostics.get("policy") in {
                            "native-verifier-evidence-v1", "native-primary-handoff-deliberation-v1",
                            "native-builder-work-deliberation-v1", "native-primary-argument-recovery-v1"}))
                    if not full_context_output:
                        output_policy = fit_output_context(output_policy, input_tokens=measure_context(body) + tolerance,
                            context_window=request_context_limit(self.model, capacity_runtime),
                            safety_tokens=config.safety_token_reserve)
                        desired = output_policy["requested_tokens"]
                        body[output_key] = desired
                        # Recheck the complete body after applying the output
                        # control. Only tighten so the fit cannot oscillate.
                        confirmed = fit_output_context(output_policy,
                            input_tokens=measure_context(body) + tolerance,
                            context_window=request_context_limit(self.model, capacity_runtime),
                            safety_tokens=config.safety_token_reserve)
                        desired = min(desired, confirmed["requested_tokens"])
                        output_policy = {**confirmed, "requested_tokens": desired}
                        body[output_key] = desired
                        request["output_tokens"] = desired + tolerance
                    def prepare_input() -> int:
                        tokens = prior_prepare_input() if prior_prepare_input else measure_input(body)
                        if borrowing is not None and borrowing.handoff_only:
                            borrowing.validate_request(body, purpose=self.purpose, protocol=self.protocol,
                                endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""))
                        self._enforce_context_capacity(measure_context(body), request["output_tokens"], capacity_runtime, config)
                        return tokens
                    prepare_input()
                self.record = self.binding.budget.reserve(
                    self.binding.invocation, input_tokens=measure_input(body),
                    prepare_input_reservation=prepare_input, **request,
                )
                if self.purpose == "agent" and self.binding.request_context is not None:
                    self.binding.request_context.admit_verifier_request(body)
                    if (paged_memory_diagnostics is not None
                        and paged_memory_diagnostics.get("policy") in {"native-primary-handoff-deliberation-v1", "native-builder-work-deliberation-v1"}
                        and paged_memory_diagnostics.get("selected") is True):
                        self.binding.request_context.admit_primary_handoff(body)
                    if (paged_memory_diagnostics is not None
                        and paged_memory_diagnostics.get("policy") == "native-primary-argument-recovery-v1"
                        and paged_memory_diagnostics.get("selected") is True):
                        # Credit is consumed only for a request that was really
                        # admitted; a refusal above never reaches this line.
                        self.binding.request_context.admit_primary_recovery(body)
            except BudgetUnavailable as error:
                diagnostic = {
                    "schema_version": "model-request-declined-v1", "http_sent": False,
                    "ledger_entry_created": False,
                    "classification": "helper_request" if isinstance(error, ConsultationBudgetLimited) else "budget_request",
                    "prepared_request": prepared_request_diagnostics(body, complete_output=complete_output),
                    "admission": error.diagnostics,
                    "input_estimate_before_projection": before_input,
                    "input_estimate_after_projection": measure_input(body),
                    "projected_tool_results": projected_count,
                    "projected_tool_inputs": projected_input_count,
                    "projected_consultation_deliveries": delivery_count,
                    "projected_consultation_inputs": consultation_input_count,
                    "projected_consultation_receipts": consultation_receipt_count,
                    "projected_consultation_previews": consultation_preview_count,
                    **({"projected_delivery_audit_pages": delivery_audit_count,
                        "delivery_audit_policy": "consultation-audit-view-v1"} if delivery_audit_count else {}),
                }
                diagnostic["prepared_request"]["input_estimate"] = dict(estimator.diagnostic)
                measure_context(body)
                diagnostic["prepared_request"]["context_input"] = dict(context_meter.diagnostic)
                if output_policy is not None:
                    diagnostic["prepared_request"]["output_budget"] = dict(output_policy)
                if history_diagnostics is not None:
                    diagnostic["prepared_request"]["reasoning_history"] = history_diagnostics
                if batching_diagnostics is not None:
                    diagnostic["prepared_request"]["tool_batching"] = batching_diagnostics
                if thinking_diagnostics is not None:
                    diagnostic["prepared_request"]["thinking_output_budget"] = thinking_diagnostics
                if context_tool_diagnostics is not None:
                    diagnostic["prepared_request"]["context_tool_view"] = context_tool_diagnostics
                if recovery_diagnostics is not None:
                    diagnostic["prepared_request"]["consultation_summary_repair"] = recovery_diagnostics
                if source_diagnostics is not None:
                    diagnostic["prepared_request"]["consultation_source_view"] = source_diagnostics
                if closure_diagnostics is not None:
                    diagnostic["prepared_request"]["closure_advice"] = closure_diagnostics
                if stage_diagnostics is not None:
                    diagnostic["prepared_request"]["stage_context"] = stage_diagnostics
                if compiler_diagnostics is not None:
                    diagnostic["prepared_request"]["context_compiler"] = compiler_diagnostics
                if paged_memory_diagnostics is not None:
                    diagnostic["prepared_request"]["paged_memory_budget"] = paged_memory_diagnostics
                if effort_diagnostics is not None:
                    diagnostic["prepared_request"]["primary_budget_effort"] = effort_diagnostics
                if builder_view is not None:
                    diagnostic["prepared_request"]["builder_context"] = dict(builder_view.diagnostic)
                if agent_view is not None:
                    diagnostic["prepared_request"]["agent_context"] = dict(agent_view.diagnostic)
                self.binding.budget.reducer.record_model_request_declined(
                    self.binding.invocation.node_id, self.binding.invocation.attempt_id, diagnostic,
                )
                if self.audit is not None:
                    self.audit.request("request_declined", body, call_id=call_id, purpose=self.purpose,
                        protocol=self.protocol, identity=identity, diagnostics=diagnostic,
                        http_sent=False, ledger_entry_created=False)
                    self._audit_declined = True
                raise
        else:
            paged_memory_diagnostics = None
            if self.skill_view is not None:
                body = self.skill_view.attach(body)
                self.skill_view.enforce_context_limit(runtime=self.runtime, model=self.model,
                    input_tokens=measure_context(body), output_tokens=desired + tolerance)
            if output_policy is not None:
                self._enforce_context_capacity(measure_context(body), desired + tolerance, capacity_runtime, config)
            self.record = {
                "call_id": new_id("model-call"),
                "output_reservation": desired + tolerance,
            }
        prepared = dict(body)
        if output_key is not None:
            prepared[output_key] = self.record["output_reservation"] - tolerance
        prepared, thinking_diagnostics = fit_consultation_thinking(
            prepared, thinking_diagnostics, output_limit=self.record["output_reservation"] - tolerance,
        )
        prepared, thinking_diagnostics = fit_primary_thinking(
            prepared, thinking_diagnostics, output_limit=self.record["output_reservation"] - tolerance,
        )
        for state_view in state_views:
            state_view.validate(prepared)
        prepared, cache_diagnostic = self._prepare_prompt_cache(prepared, request_url)
        self.request_diagnostics = prepared_request_diagnostics(prepared, complete_output=complete_output)
        if output_policy is not None:
            self.request_diagnostics["output_budget"] = {**output_policy,
                "effective_tokens": prepared[output_key]}
        if cache_diagnostic is not None:
            self.request_diagnostics["prompt_cache"] = cache_diagnostic
        measure_input(prepared)
        self.request_diagnostics["input_estimate"] = dict(estimator.diagnostic)
        measure_context(prepared)
        self.request_diagnostics["context_input"] = dict(context_meter.diagnostic)
        self.request_diagnostics["call_identity"] = {"purpose": self.purpose, **identity}
        if self.skill_view is not None:
            self.request_diagnostics["skill_rules"] = dict(self.skill_view.diagnostic)
        if isinstance(self.runtime.get("_skillCatalogDiagnostics"), dict):
            self.request_diagnostics["skill_catalog"] = dict(self.runtime["_skillCatalogDiagnostics"])
        if builder_view is not None:
            self.request_diagnostics["builder_context"] = dict(builder_view.diagnostic)
        if agent_view is not None:
            self.request_diagnostics["agent_context"] = dict(agent_view.diagnostic)
        if history_diagnostics is not None:
            self.request_diagnostics["reasoning_history"] = history_diagnostics
        if batching_diagnostics is not None:
            self.request_diagnostics["tool_batching"] = batching_diagnostics
        if thinking_diagnostics is not None:
            self.request_diagnostics["thinking_output_budget"] = thinking_diagnostics
        if context_tool_diagnostics is not None:
            self.request_diagnostics["context_tool_view"] = context_tool_diagnostics
        if recovery_diagnostics is not None:
            self.request_diagnostics["consultation_summary_repair"] = recovery_diagnostics
        if source_diagnostics is not None:
            self.request_diagnostics["consultation_source_view"] = source_diagnostics
        if closure_diagnostics is not None:
            self.request_diagnostics["closure_advice"] = closure_diagnostics
        if stage_diagnostics is not None:
            self.request_diagnostics["stage_context"] = stage_diagnostics
        if compiler_diagnostics is not None:
            self.request_diagnostics["context_compiler"] = compiler_diagnostics
        if paged_memory_diagnostics is not None:
            self.request_diagnostics["paged_memory_budget"] = paged_memory_diagnostics
        if effort_diagnostics is not None:
            self.request_diagnostics["primary_budget_effort"] = effort_diagnostics
        if projected_count or delivery_count or projected_input_count:
            self.request_diagnostics["request_context"] = {
                "projected_tool_results": projected_count,
                "projected_tool_inputs": projected_input_count,
                "projected_consultation_deliveries": delivery_count,
                "projected_consultation_inputs": consultation_input_count,
                "projected_consultation_receipts": consultation_receipt_count,
                "projected_consultation_previews": consultation_preview_count,
                **({"projected_delivery_audit_pages": delivery_audit_count,
                    "delivery_audit_policy": "consultation-audit-view-v1"} if delivery_audit_count else {}),
                "input_estimate_before": before_input, "input_estimate_after": after_context_input,
                "authority": "lossless_worker_history", "trigger": "authorized_delivery_metadata_or_insufficient_full_output_quote",
            }
        if borrowing is not None and borrowing.recovering:
            from minicode.paged_memory.config import PagedMemoryConfig

            capacity_runtime = {**self.runtime, **(self.binding.budget.runtime if self.binding else {})}
            config = PagedMemoryConfig.from_sources(capacity_runtime)
            if borrowing.handoff_only:
                borrowing.validate_request(prepared, purpose=self.purpose, protocol=self.protocol,
                    endpoint=request_url or str(self.runtime.get("openaiBaseUrl") or ""))
            self._enforce_context_capacity(measure_context(prepared), self.record["output_reservation"],
                                           capacity_runtime, config)
            if borrowing.restoring and self.purpose == "agent":
                # Keep the episode closed to new borrowing until the complete
                # wire body, refreshed native state and usage admission pass.
                borrowing.restored(borrowing.memory.last_packet)
                self.request_diagnostics["context_borrowing"] = dict(
                    borrowing.memory.last_packet.debug_metadata["context_borrowing"])
            else:
                self.request_diagnostics["context_borrowing"] = borrowing.diagnostic()
        if self.audit is not None:
            self.audit.request("request_admitted", prepared, call_id=self.record["call_id"],
                purpose=self.purpose, protocol=self.protocol, diagnostics=self.request_diagnostics,
                input_reservation=self.record.get("input_reservation"),
                output_reservation=self.record["output_reservation"], http_sent=False)
        return prepared

    def _enforce_context_capacity(self, input_tokens, output_tokens, runtime, config) -> None:
        capacity = context_capacity(context_window=request_context_limit(self.model, runtime),
            output_tokens=output_tokens, output_floor=config.output_token_reserve,
            safety_tokens=config.safety_token_reserve)
        if capacity["capacity_error"] or input_tokens > capacity["effective_input"]:
            raise BudgetUnavailable("context window cannot fit the complete request and output allowance",
                diagnostics={"classification": "context_window", "input_reservation": input_tokens,
                    "input_limit": capacity["effective_input"], "context_window": capacity["context_window"],
                    "output_reservation": output_tokens, "safety_reserve_tokens": config.safety_token_reserve})

    def _consultation_closure_eligible(self) -> bool:
        """A wire tool name alone cannot activate shared-helper closure advice.

        Reuse the live report eligibility check without consuming its capability.
        Quotes do not hold capacity; admission and report handoff still recheck.
        """
        reducer, invocation = self.binding.budget.reducer, self.binding.invocation
        if (not reducer.continuations_enabled or not reducer.closure_reserve_enabled
            or reducer.contract is None
            or reducer.contract.envelope.requested_profile not in {RequestedMode.DEEP, RequestedMode.ULTRA}):
            return False
        try:
            reducer.validate_consultation_submission(invocation)
        except ReducerError:
            return False
        return True

    def _primary_output_phase(self) -> str | None:
        """Read durable authority, never infer execution from assistant prose.

        Reuse the candidate eligibility check without consuming its capability.
        Admission and the actual handoff recheck authority independently. An
        ineligible/stale request receives no automatic reasoning downgrade.
        """
        reducer, invocation = self.binding.budget.reducer, self.binding.invocation
        if (not reducer.continuations_enabled or reducer.contract is None
            or reducer.contract.envelope.requested_profile not in {RequestedMode.DEEP, RequestedMode.ULTRA}):
            return None
        try:
            reducer.validate_candidate_submission(invocation)
        except ReducerError:
            return None
        repair = reducer.pending_primary_repair()
        if repair is not None and repair.prior_attempt_id != invocation.attempt_id:
            return "candidate_repair"
        continuation = reducer.continuation_for(invocation.node_id)
        if continuation is not None and continuation.children and all(
            reducer.node_state(child.node_id) is NodeState.SUCCEEDED for child in continuation.children
        ):
            return "post_consultation"
        return None

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        # An adapter reuses Request on retry. Keep the reference form independent of wire bytes.
        reference_body = getattr(request, "_minicode_reference_body", None)
        if reference_body is None:
            reference_body = json.loads(request.data or b"{}")
            request._minicode_reference_body = reference_body
        prepared = self.begin(reference_body, request_url=request.full_url)
        from minicode.image_input import materialize, managed_ids
        # Admission must preserve every image; never silently dispatch a text-only projection.
        if managed_ids(prepared) != managed_ids(reference_body):
            raise BudgetUnavailable("上下文裁剪丢失了图片，请开启新对话或减少附件后重试。")
        wire_body = json.dumps(materialize(prepared)).encode("utf-8")
        self.image_input_ids = list(managed_ids(prepared))
        request.data = wire_body
        # The one place the final bytes exist. Everything an at-cap diagnosis
        # needs is read from this serialization, never re-derived from the
        # ledger reservation.
        self.wire_request = wire_request_record(prepared, wire_body, self.record)
        if self.binding:
            for cap in (self.binding.budget.budget, self.binding.invocation.budget):
                if cap.deadline is not None:
                    timeout = min(timeout, max(0.001, cap.deadline - time.time()))
        try:
            from minicode.context_usage import record_for_call, report_capture_error
            try:
                self.context_snapshot = record_for_call(self, prepared, request.full_url)
            except Exception as error:  # Display telemetry never changes admission/transport.
                report_capture_error(error)
            if self.audit is not None:
                self.audit.record("http_attempt_started", call_id=self.record["call_id"],
                    wire_body_sha256=hashlib.sha256(request.data).hexdigest(), wire_body_bytes=len(request.data),
                    output_evidence=dict(self.wire_request or {}))
            self._http_started_at = time.monotonic()
            self.http_attempt_count += 1
            self.response = urllib.request.urlopen(request, timeout=timeout)
            if self.skill_view is not None:
                self.skill_view.submitted(prepared)
                self.request_diagnostics["skill_rules"] = dict(self.skill_view.diagnostic)
            return self.response
        except urllib.error.HTTPError as error:
            self.outcome = "failed"
            diagnostic = {"status": error.code, "stage": "http_response"}
            self.response_diagnostics["http_error"] = diagnostic
            # Preserve opaque identifiers, never the raw body, headers, URL or
            # provider message (which can repeat credentials or prompt content).
            def identifier(value):
                return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value) else None

            for name in ("x-request-id", "request-id", "x-dashscope-request-id"):
                request_id = identifier(error.headers.get(name)) if error.headers else None
                if request_id:
                    diagnostic["request_id"] = request_id
                    break
            body = error.read()
            error.close()
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                payload = {}
            if isinstance(payload, Mapping):
                error_value = payload.get("error")
                error_value = error_value if isinstance(error_value, Mapping) else payload
                if self.binding is not None and self.binding.budget.observe_provider_rejection(error.code, error_value):
                    diagnostic["classification"] = "provider_balance"
                code = identifier(error_value.get("code")) or identifier(error_value.get("type"))
                if code:
                    diagnostic["code"] = code
                request_id = identifier(payload.get("request_id")) or identifier(payload.get("requestId"))
                if request_id and "request_id" not in diagnostic:
                    diagnostic["request_id"] = request_id
                self.observe(payload.get("usage"), complete=True)
                # Only an explicit pre-generation rate-limit rejection proves
                # no inference occurred. Timeouts, generic 429s and missing or
                # malformed usage on other errors remain unreconciled. A real
                # usage receipt always takes precedence over this inference.
                if (error.code == 429 and code in {"rate_limit_exceeded", "rate_limit_error"}
                        and payload.get("usage") is None
                        and not any(payload.get(key) for key in ("choices", "content", "output"))
                        and not diagnostic.get("classification")):
                    diagnostic["classification"] = "rate_limit_rejected"
                    self._rate_limit_rejected = True
                    self.complete = True
            # Preserve the response for the adapter's existing error classifier.
            if error.code in {400, 415, 422} and managed_ids(prepared):
                error_message = str(payload.get("error", "")) if isinstance(payload, Mapping) else ""
                if re.search(r"image|vision|multimodal|media.type|base64|图片", error_message, re.I):
                    diagnostic["classification"] = "image_input_rejected"
                    raise ValueError("模型接口拒绝了图片输入，请检查该模型和接口的视觉支持，或移除图片后重试。") from None
            buffered = urllib.error.HTTPError(
                error.url, error.code, error.msg, error.hdrs, io.BytesIO(body)
            )
            self.response = buffered
            raise buffered from error
        except BaseException as error:
            self.outcome = "failed"
            self.response_diagnostics["transport_error"] = {
                "type": type(error).__name__, "kind": getattr(error, "kind", None)}
            raise

    def reserved_output_tokens(self) -> int | None:
        """The output cap the provider was really given, if it is known.

        Read-only: this states what the final wire body asked the provider to
        allow, so an at-cap diagnosis compares a real provider counter against a
        real request field instead of the ledger reservation. The reservation is
        larger by the documented output tolerance; that difference is reported by
        ``output_tolerance_tokens()`` and never folded into this number.
        """
        wire = self.wire_request
        if wire is None:
            return None
        cap = wire.get("output_cap_tokens")
        return cap if type(cap) is int else None

    def output_reservation_tokens(self) -> int | None:
        """The ledger's output allowance for this call, tolerance included."""
        wire = self.wire_request
        if wire is not None and type(wire.get("output_reservation_tokens")) is int:
            return wire["output_reservation_tokens"]
        record = self.record or self.raw_capture_record
        if record is None:
            return None
        reservation = record.get("output_reservation")
        return reservation if type(reservation) is int else None

    def output_tolerance_tokens(self) -> int | None:
        """The tolerance added to the wire cap to form the reservation."""
        wire = self.wire_request
        if wire is None:
            return None
        tolerance = wire.get("output_tolerance_tokens")
        return tolerance if type(tolerance) is int else None

    def observe_response_shape(self, *, stop_reason: Any, content: str,
                               reasoning_content: str, calls: list[dict[str, Any]]) -> None:
        """Record bounded protocol diagnostics, never generated text or arguments.

        Called after parsing the actual response. This does not settle usage,
        certify a report, or turn partial provider counters into known totals.
        """
        if self.complete:
            return  # A final cumulative receipt, including its diagnostics, is immutable.
        self.response_diagnostics.update({
            "schema": "model-response-shape-v1",
            "stop_reason": (stop_reason if isinstance(stop_reason, str)
                and stop_reason in {"stop", "length", "tool_calls", "function_call", "content_filter", "pause_turn", "max_tokens"}
                else "unknown"),
            "content_utf8_bytes": len(content.encode("utf-8")),
            "reasoning_utf8_bytes": len(reasoning_content.encode("utf-8")),
            "tool_call_count": len(calls),
            "invalid_argument_calls": sum(bool(call.get("inputError")) for call in calls),
        })
        if self.audit is not None:
            self.audit.record("model_response", call_id=self.record["call_id"] if self.record else None,
                stop_reason=stop_reason, content=content, reasoning_content=reasoning_content, calls=calls,
                diagnostics=self.response_diagnostics)
        # The raw-response capture is deliberately NOT reset here. Shape
        # diagnostics are emitted several times per response (a usage event, the
        # terminal marker, the end of the stream), and resetting would discard
        # every event recorded before the last one. Only begin_raw_capture starts
        # a new HTTP attempt's evidence.

    def request_raw_capture_started(self, payload: Mapping[str, Any]) -> None:
        """Record the pre-HTTP attempt identity for the raw evidence capture."""
        if self.audit is not None:
            self.audit.record("raw_response_attempt", call_id=self.record["call_id"] if self.record else None,
                capture_policy=RAW_CAPTURE_POLICY, **dict(payload))

    def record_stream_integrity(self, *, transport_termination: str, network_complete: bool,
                                parse_complete: bool, assembly_complete: bool,
                                loss_reasons: list[str], lost_events: list[dict[str, Any]],
                                lost_event_count: int) -> dict[str, Any]:
        """Journal why a transport-complete body is still not a usable batch.

        Network end, parse integrity and call assembly are recorded as three
        separate facts. A stream that ended on the provider's own terminal marker
        yet dropped an event is neither silent nor "complete": the loss is a
        durable record and the counts are exact even when the retained detail is
        bounded.
        """
        integrity = {
            "schema": "stream-integrity-v1",
            "transport_termination": transport_termination,
            "network_complete": bool(network_complete),
            "parse_complete": bool(parse_complete),
            "assembly_complete": bool(assembly_complete),
            "lost_event_count": int(lost_event_count),
            "loss_reasons": list(loss_reasons),
            "lost_events": [dict(item) for item in lost_events],
            "lost_event_detail_truncated": int(lost_event_count) > len(lost_events),
        }
        self.stream_integrity = integrity
        if self.audit is not None:
            self.audit.record("stream_integrity", call_id=self.record["call_id"] if self.record else None,
                              **integrity)
        return integrity

    def begin_raw_capture(self, *, transport: str, limits: RawCaptureLimits | None = None) -> RawResponseCapture:
        """Open one bounded raw-response capture for a single HTTP attempt.

        Called once per HTTP attempt, before the response body is consumed. The
        adapter feeds the very bytes it parses, so this never re-reads the
        stream and never changes the caller's streaming choice.
        """
        self.attempt_count += 1
        self.raw_capture_record = dict(self.record) if self.record else None
        self.raw_capture = RawResponseCapture(
            artifacts=self.audit.artifacts if self.audit is not None else None,
            run_id=self.audit.invocation.run_id if self.audit is not None else "",
            node_id=self.audit.invocation.node_id if self.audit is not None else "",
            attempt_id=self.audit.invocation.attempt_id if self.audit is not None else "",
            model_call_id=self.record["call_id"] if self.record else None,
            http_attempt=self.attempt_count, transport=transport, limits=limits)
        self.raw_capture_receipt = None
        self.request_raw_capture_started(self.raw_capture.header())
        return self.raw_capture

    def finish_raw_capture(self, *, transport_termination: str,
                           error_type: str | None = None,
                           complete: bool | None = None) -> dict[str, Any] | None:
        """Freeze and persist the capture receipt, then bind it to the ledger.

        Authority-neutral: the receipt only records what was observed. Usage
        settlement and admission are unaffected.
        """
        capture = self.raw_capture
        if capture is None:
            return None
        receipt = capture.finalize(transport_termination=transport_termination,
                                   error_type=error_type, complete=complete)
        if self.audit is not None:
            receipt = put_evidence(self.audit.artifacts, capture, receipt=receipt)
        self.raw_capture_receipt = receipt
        if self.audit is not None:
            self.audit.record("raw_response_capture", call_id=self.record["call_id"] if self.record else None,
                receipt=receipt)
        return receipt

    def _capture_usage(self, usage: dict[str, Any]) -> dict[str, Any]:
        """Attach the raw-response receipt only while the receipt is mutable.

        A final cumulative usage observation can legitimately settle a call
        before the transport read finishes (non-streaming bodies are parsed
        after the usage block is seen). The ledger treats a settled usage object
        as immutable, so response evidence is added only to a still-pending
        call; a later re-settlement with extra keys would otherwise either look
        like double charging or silently rewrite a frozen receipt.
        """
        usage["first_output_latency_ms"] = self.first_output_latency_ms
        if self.provider_failure is not None:
            usage["provider_failure"] = dict(self.provider_failure)
        record = self.record if self.record is not None else self.raw_capture_record
        if self.raw_capture_receipt is None or record is None:
            return usage
        if self.binding is not None:
            ledger = self.binding.budget.reducer._state.usage_ledger
            settled = ledger.calls.get(record["call_id"], {}).get("status")
            if settled is not None and settled != "pending":
                return usage
        usage["raw_response"] = dict(self.raw_capture_receipt)
        return usage

    def observe(self, raw: Any, *, complete: bool) -> None:
        if not isinstance(raw, Mapping):
            return
        candidate = {**self.raw_usage, **raw}
        usage = normalize_usage(candidate, self.protocol, self.pricing)
        from minicode.context_usage import observe_input, report_capture_error
        try:
            observe_input(self.context_snapshot, usage, complete=complete)
        except Exception as error:
            report_capture_error(error)
        if self.request_diagnostics:
            usage["prepared_request"] = dict(self.request_diagnostics)
        if self.response_diagnostics:
            usage["response_shape"] = dict(self.response_diagnostics)
        self._capture_usage(usage)
        self.raw_usage = candidate
        self.complete = complete and usage["tokens_known"]
        if self.binding and self.record:
            if self.complete:
                self.binding.budget.observe_context_input(self.binding.invocation.node_id, self.purpose,
                    self.request_diagnostics.get("context_input"), usage)
            from minicode.token_estimation import estimate_was_exceeded
            if estimate_was_exceeded({**self.record, "usage": usage}):
                self.binding.budget.input_estimator_failed = True
            # Partial observations survive a process loss; reservations remain
            # held until the entire request reports its final cumulative usage.
            self.binding.budget.reducer.record_model_usage(
                self.record["call_id"],
                usage,
                complete=self.complete,
                outcome=self.outcome if self.complete else "pending",
            )

    def _finish(self, outcome: str) -> None:
        if self.record is None:
            return
        record, self.record = self.record, None
        if self.raw_capture is not None and self.raw_capture_receipt is None:
            # An attempt that never reached its own finalization (transport
            # exception, cancellation, parse failure) still keeps its evidence,
            # explicitly marked as an abnormal termination.
            self.finish_raw_capture(transport_termination="abandoned")
        usage = normalize_usage(self.raw_usage, self.protocol, self.pricing)
        if self._rate_limit_rejected:
            # Record the rejection's accounting basis separately from provider
            # counters; never manufacture a usage block in the raw response.
            usage.update(tokens_known=True, cost=0.0, cost_source="request_rejected")
        if self.request_diagnostics:
            usage["prepared_request"] = dict(self.request_diagnostics)
        if self.response_diagnostics:
            usage["response_shape"] = dict(self.response_diagnostics)
        self._capture_usage(usage)
        try:
            if self.binding:
                self.binding.budget.reducer.record_model_usage(
                    record["call_id"],
                    usage,
                    complete=self.complete,
                    outcome=outcome,
                )
            self._finished.append({**usage, "tokens_known": self.complete})
            if self.audit is not None:
                self.audit.record("model_call_finished", call_id=record["call_id"], outcome=outcome,
                    complete=self.complete, usage=usage)
        finally:
            if self.binding:
                self.binding.budget.release(record["call_id"])
            if self.response is not None:
                close = getattr(self.response, "close", None)
                if callable(close):
                    close()
                self.response = None

    @property
    def usage(self) -> dict[str, Any]:
        known = all(
            item["tokens_known"] and item["cost"] is not None for item in self._finished
        )
        return {
            "tokens": sum(item["tokens"] for item in self._finished),
            "cost": add_amounts(*(item["cost"] for item in self._finished))
            if known
            else None,
            "model_calls": len(self._finished),
            "cost_known": known,
            "tokens_known": all(item["tokens_known"] for item in self._finished),
        }


def guarded_model_next(
    model: Any, messages: list[dict[str, Any]], kwargs: dict[str, Any]
) -> Any:
    if getattr(model, "supports_runtime_usage", False) or _binding.get() is None:
        return model.next(messages, **kwargs)
    binding = _binding.get()
    if binding and binding.bounded:
        raise BudgetUnavailable(
            "model adapter does not implement request-level budget admission"
        )
    runtime = getattr(model, "runtime", {})
    with ProviderCall(
        runtime if isinstance(runtime, Mapping) else {}, "normalized"
    ) as call:
        call.begin({"messages": messages})
        step = model.next(messages, **kwargs)
        raw = getattr(step, "usage", {})
        call.observe(raw, complete=True)
    return step
