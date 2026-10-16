"""Model invocation boundary: adapter compatibility, scoped tools and measured calls."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from minicode.provider_usage import guarded_model_next
from minicode.output_budget import OutputRecovery, bind_output_recovery
from minicode.state import AppState, Store
from minicode.tooling import ToolRegistry, bind_model_tools
from minicode.turn_telemetry import measured
from minicode.types import AgentStep, ChatMessage, ModelAdapter


@measured("model")
def _model_next(
    model: ModelAdapter,
    messages: list[ChatMessage],
    *,
    on_stream_chunk: Callable[[str], None] | None,
    on_thinking_chunk: Callable[[str], None] | None = None,
    store: Store[AppState] | None,
    tools: ToolRegistry | None = None,
    output_recovery: OutputRecovery | None = None,
    on_provider_recovery: Callable[[dict[str, Any]], None] | None = None,
) -> AgentStep:
    """Call provider adapters with store/thinking support while preserving test doubles."""
    kwargs: dict[str, Any] = {"on_stream_chunk": on_stream_chunk}

    try:
        sig = inspect.signature(model.next)
        param_names = set(sig.parameters.keys())
        has_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if has_kwargs or "on_thinking_delta" in param_names:
            kwargs["on_thinking_delta"] = on_thinking_chunk
        if has_kwargs or "store" in param_names:
            kwargs["store"] = store
        if "on_provider_recovery" in param_names:
            kwargs["on_provider_recovery"] = on_provider_recovery
    except (TypeError, ValueError):
        # Can't inspect signature (e.g. some mock objects) — be conservative
        pass

    with bind_output_recovery(output_recovery), bind_model_tools(tools) if tools is not None else nullcontext():
        result = guarded_model_next(model, messages, kwargs)
    if output_recovery is not None:
        output_recovery.observe(result.diagnostics.outputBudget if result.diagnostics is not None else None)
    return result
