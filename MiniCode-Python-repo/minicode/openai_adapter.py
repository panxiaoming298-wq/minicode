"""OpenAI-compatible API adapter for MiniCode.

Supports GPT-4o, GPT-4-turbo, GPT-4o-mini and any OpenAI-compatible endpoint
(e.g., Azure OpenAI, local LLMs with OpenAI-compatible API).
"""

from __future__ import annotations

import json
import hashlib
import http.client
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Literal

from minicode.api_retry import RETRYABLE_STATUS, calculate_backoff
from minicode.cost_tracker import calculate_cost
from minicode.provider_usage import ProviderCall, ProviderBalanceUnavailable, provider_balance_blocked
from minicode.provider_errors import ProviderStreamError, safe_identifier
from minicode.state import (
    AppState,
    Store,
    add_cost,
    record_api_error,
    update_context_usage,
)
from minicode.tooling import model_tool_registry
from minicode.tool_argument_diagnostics import classify_arguments, decode_tool_arguments
from minicode.types import AgentStep, StepDiagnostics

DEFAULT_MAX_RETRIES = 4
MAX_STREAM_RETRIES = 2
MAX_STREAM_RETRY_DELAY = 60.0
OPENAI_MODELS = {"gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-5.5", "gpt5.5", "o1", "o1-mini", "o3-mini"}
DEFAULT_OPENAI_USER_AGENT = "MiniCode-Python/0.5.0 (OpenAI-Compatible Adapter)"
# Other Server-Sent Events fields. ``data`` is handled separately because it
# carries the payload; these carry framing the OpenAI-compatible shape ignores.
_OTHER_SSE_FIELDS = re.compile(r"^(?:event|id|retry)\s*:")
# Bounded per-response loss journal: the count is exact, the retained detail is
# capped so a hostile or broken stream cannot grow memory without limit.
_MAX_RECORDED_LOSSES = 64


def _is_openai_model(model: str) -> bool:
    """Check if model name indicates an OpenAI-compatible API."""
    model_lower = model.lower()
    # Direct match
    if model_lower in OPENAI_MODELS:
        return True
    # Prefix match for versioned models
    for prefix in ("gpt-5", "gpt-4", "gpt-3.5", "gpt5", "o1-", "o3-", "chatgpt-"):
        if model_lower.startswith(prefix):
            return True
    # Check if explicitly using OpenAI base URL
    base_url = os.environ.get("OPENAI_BASE_URL", os.environ.get("OPENAI_API_BASE", ""))
    if base_url and "openai" in base_url.lower():
        return True
    return False


def _get_openai_base_url(runtime: dict[str, Any]) -> str:
    """Get OpenAI-compatible base URL."""
    return (
        runtime.get("openaiBaseUrl", "")
        or os.environ.get("OPENAI_BASE_URL", "")
        or os.environ.get("OPENAI_API_BASE", "")
        or "https://api.openai.com"
    ).rstrip("/")


def _get_openai_chat_completions_url(runtime: dict[str, Any]) -> str:
    base_url = _get_openai_base_url(runtime)
    explicit_path = str(runtime.get("_openai_chat_completions_path", "") or "").strip()
    if explicit_path:
        normalized_path = f"/{explicit_path.lstrip('/')}"
        if base_url.endswith(normalized_path):
            return base_url
        return f"{base_url.rstrip('/')}/{explicit_path.lstrip('/')}"
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _get_openai_api_key(runtime: dict[str, Any]) -> str:
    """Get OpenAI API key."""
    return (
        runtime.get("openaiApiKey", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )


def _read_openai_response_body(response: Any) -> bytes:
    """Read the response body exactly once.

    The single read is what the raw-response evidence records and what parsing
    consumes, so a recorded prefix is byte-identical to the parsed prefix.
    """
    return response.read()


def _decode_body_exactly(capture: Any, raw_body: bytes) -> tuple[str, dict[str, Any]]:
    """Store the provider's bytes, then decode strictly for the parser only.

    Re-encoding a replacement-decoded body would store different bytes and make
    the recorded hash a re-encoded hash rather than the raw one. The capture
    therefore keeps the original bytes and the decode result is reported as its
    own fact.
    """
    capture.observe_bytes(raw_body)
    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError as error:
        capture.note_body_decode(ok=False, error_type=type(error).__name__)
        return raw_body.decode("utf-8", errors="replace"), {
            "ok": False, "error_type": type(error).__name__}
    capture.note_body_decode(ok=True)
    return text, {"ok": True, "error_type": None}


def _parse_openai_response_body(
    response: Any = None, *, body: bytes | None = None, text: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Parse an OpenAI-compatible response body from bytes read exactly once."""
    if text is None:
        raw_body = _read_openai_response_body(response) if body is None else body
        decoded = raw_body.decode("utf-8", errors="replace")
    else:
        decoded = text
    if not decoded.strip():
        return {}, ""
    try:
        return json.loads(decoded), decoded
    except json.JSONDecodeError:
        return {}, decoded


@dataclass
class _BufferedHTTPResponse:
    status: int
    code: int
    headers: Any
    body: bytes

    def read(self) -> bytes:
        return self.body


def _is_non_retryable_openai_error(status: int, data: dict[str, Any], raw_text: str) -> bool:
    if status < 500:
        return False
    error_block = data.get("error", {}) if isinstance(data, dict) else {}
    error_block = error_block if isinstance(error_block, dict) else {"message": str(error_block)}
    code = str(error_block.get("code", "")).lower()
    message = str(error_block.get("message", "") or raw_text).lower()
    permanent_markers = (
        "model_not_found",
        "no available channel",
        "insufficient_quota",
        "monthly usage limit reached",
        "invalid api key",
    )
    return any(marker in code or marker in message for marker in permanent_markers)


def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Decode one protocol argument object through the shared strict decoder.

    Live parsing and independent classification must never disagree about the
    same bytes, so both call the same implementation.
    """
    return decode_tool_arguments(arguments)


def _openai_tool_call(tool_id: str, name: str, arguments: Any) -> dict[str, Any]:
    """Preserve malformed provider arguments as non-executable protocol history."""
    call: dict[str, Any] = {"id": tool_id, "toolName": name, "input": {}}
    try:
        call["input"] = _parse_tool_arguments(arguments)
    except (ValueError, TypeError):
        # Preserve the exact incomplete text in canonical history for provider
        # continuation. The executor must reject it before any tool/hook runs.
        call["inputError"] = "invalid_json_object"
        call["rawArguments"] = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return call


def classify_calls(
    calls: list[dict[str, Any]],
    *,
    finish_reason: str | None,
    transport_complete: bool,
    transport_termination: str,
    raw_arguments: list[str] | None = None,
    output_token_limit: int | None = None,
    output_tokens_used: int | None = None,
) -> list[dict[str, Any]]:
    """Independently classify every decoded call against its exact provider bytes.

    ``raw_arguments`` carries the protocol text exactly as the provider sent it,
    so the recorded byte counts and hashes describe the provider's bytes rather
    than a local re-serialization. The classifier re-runs the same decoder the
    adapter used, so its verdict is reproducible from those bytes.
    """
    classified: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        rejected = bool(call.get("inputError"))
        if raw_arguments is not None and index < len(raw_arguments):
            raw_text = raw_arguments[index]
        else:
            raw = call.get("rawArguments") if rejected else call.get("input")
            raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        try:
            parsed = _parse_tool_arguments(raw_text)
        except (ValueError, TypeError) as error:
            parse_error: BaseException | None = error
            decoded_ok = False
        else:
            parse_error = None
            decoded_ok = isinstance(parsed, dict)
        diagnostic = classify_arguments(
            raw_text,
            provider_finish_reason=finish_reason,
            transport_complete=transport_complete,
            transport_termination=transport_termination,
            output_token_limit=output_token_limit,
            output_tokens_used=output_tokens_used,
            parse_error=parse_error,
            parsed_ok=decoded_ok,
            sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        )
        classified.append({"call_id": call.get("id"), "tool_name": call.get("toolName"),
                           "rejected_by_parser": rejected, **diagnostic.to_dict()})
        if not diagnostic.may_execute:
            # Defence in depth: a call whose own evidence says it may not execute
            # carries that verdict on the call itself, so the tool boundary can
            # refuse it even if a future caller forgets the batch-level gate.
            call["executionBlocked"] = diagnostic.runtime_classification
    return classified


def _to_openai_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert internal message format to OpenAI Chat Completion format.
    
    Returns (system_message, chat_messages)
    """
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    
    for message in messages:
        role = message["role"]
        content = message.get("content", "")
        
        if role == "system":
            system_parts.append(content)
            continue
        
        if role == "user":
            converted.append({"role": "user", "content": content})
            continue
        
        if role in ("assistant", "assistant_progress"):
            text = content
            if role == "assistant_progress":
                text = f"<progress>\n{content}\n</progress>"
            assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
            reasoning_content = str(message.get("reasoningContent", "") or "")
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            converted.append(assistant_message)
            continue
        
        if role == "assistant_tool_call":
            # OpenAI format: assistant message with tool_calls
            tool_call = {
                "id": message["toolUseId"],
                "type": "function",
                "function": {
                    "name": message["toolName"],
                    "arguments": (message["rawArguments"]
                        if message.get("inputError") and isinstance(message.get("rawArguments"), str)
                        else json.dumps(message["input"]) if isinstance(message["input"], dict) else "{}"),
                },
            }
            reasoning_content = str(message.get("reasoningContent", "") or "")
            if converted and converted[-1].get("role") == "assistant":
                converted[-1].setdefault("tool_calls", []).append(tool_call)
                if reasoning_content:
                    converted[-1]["reasoning_content"] = reasoning_content
            else:
                assistant_message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                }
                if reasoning_content:
                    assistant_message["reasoning_content"] = reasoning_content
                converted.append(assistant_message)
            continue
        
        if role == "tool_result":
            converted.append({
                "role": "tool",
                "tool_call_id": message["toolUseId"],
                "content": message.get("content", ""),
            })
            continue
    
    system_message = "\n\n".join(system_parts)
    return system_message, converted


AssistantTextKind = Literal["final", "progress"]


def _parse_assistant_text(content: str) -> tuple[str, AssistantTextKind | None]:
    """Parse progress/final markers from assistant text."""
    trimmed = content.strip()
    if not trimmed:
        return "", None
    markers: list[tuple[str, AssistantTextKind, str | None]] = [
        ("<final>", "final", "</final>"),
        ("[FINAL]", "final", None),
        ("<progress>", "progress", "</progress>"),
        ("[PROGRESS]", "progress", None),
    ]
    for prefix, kind, closing_tag in markers:
        if trimmed.startswith(prefix):
            raw = trimmed[len(prefix):].strip()
            if closing_tag:
                raw = raw.replace(closing_tag, "").strip()
            return raw, kind
    return trimmed, None


class OpenAIModelAdapter:
    """Model adapter for OpenAI-compatible APIs."""

    supports_runtime_usage = True
    
    def __init__(self, runtime: dict[str, Any], tools) -> None:
        self.runtime = runtime
        self.tools = tools
        self._cached_tools_json: list[dict[str, Any]] | None = None
        self._tools_cache_key: int = 0
    
    def _get_serialized_tools(self) -> list[dict[str, Any]]:
        """Get serialized tool list in OpenAI function format with caching."""
        from minicode.tooling import ToolRegistry

        registry = model_tool_registry(self.tools) or self.tools or ToolRegistry([])
        current_tools = registry.list()
        current_key = hash(tuple((t.name, t.description, json.dumps(t.input_schema, sort_keys=True)) for t in current_tools))
        if self._cached_tools_json is None or current_key != self._tools_cache_key:
            self._cached_tools_json = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in current_tools
            ]
            self._tools_cache_key = current_key
        return self._cached_tools_json
    
    def next(
        self,
        messages: list[dict[str, Any]],
        on_stream_chunk: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        store: Store[AppState] | None = None,
        on_provider_recovery: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentStep:
        with ProviderCall(self.runtime, "openai") as call:
            retries = 0
            while True:
                try:
                    step = self._next(messages, on_stream_chunk, on_thinking_delta, store, call=call,
                                      stream_recovery=retries > 0)
                    break
                except ProviderStreamError as error:
                    error.attempts, error.retries = call.http_attempt_count, retries
                    error.model = safe_identifier(self.runtime.get("model"))
                    diagnostic = error.diagnostic()
                    # Settle this failed HTTP attempt before waiting or opening
                    # another. Missing usage remains unknown, never zero.
                    call.finish_provider_failure(diagnostic)
                    if provider_balance_blocked():
                        # Keep the existing run-wide billing pause/recovery
                        # contract if a retry receives an HTTP balance refusal.
                        raise ProviderBalanceUnavailable("provider balance unavailable") from error
                    # A long Retry-After pauses this run rather than retrying
                    # before the service's requested time or waiting unboundedly.
                    can_retry = (error.retryable and retries < MAX_STREAM_RETRIES
                                 and error.retry_after <= MAX_STREAM_RETRY_DELAY
                                 and call.http_attempt_count < DEFAULT_MAX_RETRIES + 1)
                    delay = max(error.retry_after, calculate_backoff(retries)) if can_retry else 0.0
                    if on_provider_recovery:
                        on_provider_recovery({**diagnostic, "status": "retrying" if can_retry else "stopped",
                                              "retry": retries + 1 if can_retry else retries,
                                              "max_retries": MAX_STREAM_RETRIES, "delay_seconds": delay})
                    if not can_retry:
                        raise
                    call.wait_for_retry(delay)
                    retries += 1
        # Final settlement can still reject an over-budget or unaccounted
        # response. Announce recovery only after that boundary has passed.
        if (retries and on_provider_recovery
                and (step.diagnostics is None or step.diagnostics.transportComplete)):
            on_provider_recovery({"status": "recovered", "retries": retries,
                                  "http_attempts": call.http_attempt_count})
        step.usage = call.usage
        step.imageInputIds = list(call.image_input_ids)
        if step.diagnostics is not None:
            step.diagnostics.outputBudget = dict(call.request_diagnostics.get("output_budget", {}))
        return step

    def context_request(self, messages, *, stream=False):
        """Build the same request for planning and dispatch, without credentials or IO."""
        system_message, converted_messages = _to_openai_messages(messages)
        
        request_body: dict[str, Any] = {
            "model": self.runtime["model"],
            "messages": converted_messages,
        }
        serialized_tools = self._get_serialized_tools()
        if serialized_tools:
            request_body["tools"] = serialized_tools
        
        if system_message:
            request_body["messages"].insert(0, {"role": "system", "content": system_message})
        
        if self.runtime.get("maxOutputTokens") is not None:
            request_body["max_tokens"] = self.runtime["maxOutputTokens"]
        
        if stream:
            request_body["stream"] = True
            request_body["stream_options"] = {"include_usage": True}

        for params in (self.runtime.get("_openrouter_params", {}), self.runtime.get("_provider_params", {})):
            request_body.update({key: value for key, value in params.items() if value is not None})
        from minicode.image_input import prepare_request
        endpoint = _get_openai_chat_completions_url(self.runtime)
        return prepare_request(request_body, self.runtime, "openai", endpoint, source_messages=messages), endpoint, "openai"

    def _next(
        self,
        messages: list[dict[str, Any]],
        on_stream_chunk: Callable[[str], None] | None,
        on_thinking_delta: Callable[[str], None] | None,
        store: Store[AppState] | None,
        *,
        call: ProviderCall,
        stream_recovery: bool = False,
    ) -> AgentStep:
        request_body, endpoint, _ = self.context_request(messages, stream=bool(on_stream_chunk))
        
        api_key = _get_openai_api_key(self.runtime)
        
        # Build headers — support OpenRouter and custom endpoints
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": DEFAULT_OPENAI_USER_AGENT,
        }
        # OpenRouter extra headers (HTTP-Referer, X-Title)
        openrouter_headers = self.runtime.get("_openrouter_headers", {})
        headers.update(openrouter_headers)
        # Custom endpoint extra headers
        custom_headers = self.runtime.get("_custom_headers", {})
        headers.update(custom_headers)
        # Declarative provider headers (DeepSeek and future compatible providers)
        provider_headers = self.runtime.get("_provider_headers", {})
        headers.update(provider_headers)

        request = urllib.request.Request(
            url=endpoint,
            data=json.dumps(request_body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        
        # Retry logic
        max_retries = DEFAULT_MAX_RETRIES
        response = None
        # HTTP connection retries and stream retries share one total ceiling.
        for attempt in range(max_retries + 1 - call.http_attempt_count):
            try:
                timeout = int(os.environ.get("MINICODE_MODEL_TIMEOUT", "120"))
                response = call.open(request, timeout=timeout)
                break
            except urllib.error.HTTPError as error:
                buffered_error = _BufferedHTTPResponse(
                    status=error.code,
                    code=error.code,
                    headers=getattr(error, "headers", None),
                    body=error.read(),
                )
                response = buffered_error
                # The retry/error body was already read above; record those exact
                # bytes once, before any decoding or classification of them.
                capture = call.begin_raw_capture(
                    transport="error_body" if not on_stream_chunk else "stream")
                decoded_error, decode_fact = _decode_body_exactly(capture, buffered_error.body)
                call.finish_raw_capture(transport_termination="eof", complete=decode_fact["ok"])
                parsed_error, raw_error_text = _parse_openai_response_body(
                    body=buffered_error.body, text=decoded_error)
                parsed_error = parsed_error if isinstance(parsed_error, dict) else {}
                call.observe(parsed_error.get("usage"), complete=True)
                if (
                    error.code not in RETRYABLE_STATUS
                    or call.http_attempt_count >= max_retries + 1
                    or _is_non_retryable_openai_error(error.code, parsed_error, raw_error_text)
                ):
                    break
                from minicode.api_retry import classify_error
                category = classify_error(error)
                wait = calculate_backoff(attempt, category=category)
                call.wait_for_retry(wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if call.http_attempt_count >= max_retries + 1:
                    if stream_recovery:
                        raise ProviderStreamError({"error": {"type": type(error).__name__}},
                            transport=True, stage="connection") from error
                    raise
                wait = calculate_backoff(attempt)
                call.wait_for_retry(wait)
        
        if response is None:
            raise RuntimeError("OpenAI request failed before receiving a response")
        
        if not on_stream_chunk:
            # Non-streaming response
            capture = call.begin_raw_capture(transport="non_stream")
            raw_body = _read_openai_response_body(response)
            decoded_body, decode_fact = _decode_body_exactly(capture, raw_body)
            call.finish_raw_capture(transport_termination="eof", complete=decode_fact["ok"])
            data, raw_text = _parse_openai_response_body(body=raw_body, text=decoded_body)
            try:
                choices = data.get("choices", [])
                choice = choices[0] if choices else {}
                message = choice.get("message", {})
                text_content = message.get("content", "") or ""
                reasoning_content = message.get("reasoning_content", "") or ""
                stop_reason = choice.get("finish_reason")
                protocol_calls = message.get("tool_calls") or []
                raw_arguments = [tc.get("function", {}).get("arguments") for tc in protocol_calls]
                tool_calls = [_openai_tool_call(tc.get("id", ""), tc.get("function", {}).get("name", ""),
                    tc.get("function", {}).get("arguments")) for tc in protocol_calls]
                # Freeze diagnostics before the immutable cumulative receipt.
                call.observe_response_shape(stop_reason=stop_reason, content=text_content,
                                            reasoning_content=reasoning_content, calls=tool_calls)
            finally:
                # Malformed response structure must not erase usage received
                # from the provider, even if parsing cannot produce a step.
                call.observe(data.get("usage"), complete=True)
            call.check()
            status = getattr(response, "status", getattr(response, "code", 200))
            
            if status >= 400:
                if store:
                    store.set_state(record_api_error())
                error_msg = (
                    data.get("error", {}).get("message")
                    or raw_text.strip()
                    or f"OpenAI API error: {status}"
                )
                raise RuntimeError(error_msg)
            if not data:
                raise RuntimeError(
                    "OpenAI-compatible endpoint returned a non-JSON success payload."
                )
            
            # Cost tracking
            if store:
                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                cost_usd = calculate_cost(
                    model=self.runtime.get("model", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                if cost_usd > 0:
                    store.set_state(add_cost(cost_usd))
                store.set_state(update_context_usage(input_tokens + output_tokens))
            
            # Parse response
            if not choices:
                return AgentStep(type="assistant", content="")
            if reasoning_content and on_thinking_delta:
                on_thinking_delta(reasoning_content)
            
            parsed_text, kind = _parse_assistant_text(text_content.strip())
            diagnostics = StepDiagnostics(
                stopReason=stop_reason,
                blockTypes=["tool_calls"] if tool_calls else (["text"] if text_content else []),
                ignoredBlockTypes=[],
                transportComplete=decode_fact["ok"],
                transportTermination="eof",
                rawResponseIncompleteReason=(
                    None if decode_fact["ok"] else
                    "the response body was not valid UTF-8; the recorded bytes are the raw "
                    f"response ({decode_fact['error_type']})"),
                networkComplete=True,
                parseComplete=decode_fact["ok"],
                assemblyComplete=decode_fact["ok"],
                lostEventCount=0 if decode_fact["ok"] else 1,
                streamLossReasons=[] if decode_fact["ok"] else ["undecodable_body"],
                argumentDiagnostics=classify_calls(
                    tool_calls, finish_reason=stop_reason, transport_complete=decode_fact["ok"],
                    transport_termination="eof", raw_arguments=raw_arguments,
                    output_token_limit=call.reserved_output_tokens(),
                    output_tokens_used=((data.get("usage") or {}).get("completion_tokens")
                                        if isinstance(data.get("usage"), dict) else None),
                ),
            )
            
            if tool_calls:
                return AgentStep(
                    type="tool_calls",
                    calls=tool_calls,
                    content=parsed_text,
                    contentKind=kind,
                    reasoningContent=reasoning_content,
                    diagnostics=diagnostics,
                )
            return AgentStep(
                type="assistant",
                content=parsed_text,
                kind=kind,
                reasoningContent=reasoning_content,
                diagnostics=diagnostics,
            )
        
        # Streaming response
        if isinstance(response, _BufferedHTTPResponse):
            capture = call.begin_raw_capture(transport="stream")
            capture.observe_text(response.body.decode("utf-8", errors="replace"))
            call.finish_raw_capture(transport_termination="eof")
            data, raw_text = _parse_openai_response_body(body=response.body)
            data = data if isinstance(data, dict) else {}
            if store:
                store.set_state(record_api_error())
            error = data.get("error")
            error = error if isinstance(error, dict) else {"message": str(error or "")}
            error_msg = (
                error.get("message")
                or raw_text.strip()
                or f"OpenAI API error: {response.status}"
            )
            if stream_recovery:
                # Recovery may change failure shape (SSE error -> HTTP 404/5xx).
                # Do not let that reopen the generic whole-Worker retry path.
                raise ProviderStreamError({"error": {**error, "status": response.status},
                    "request_id": data.get("request_id") or data.get("requestId")},
                    headers=response.headers, stage="http_response")
            raise RuntimeError(error_msg)

        raw_capture = call.begin_raw_capture(transport="stream")
        tool_calls = []
        text_parts = []
        reasoning_parts = []
        active_tool_calls: dict[int, dict[str, Any]] = {}
        stop_reason = None
        stream_input_tokens = 0
        stream_output_tokens = 0
        lost_events: list[dict[str, Any]] = []
        loss_reasons: list[str] = []
        lost_event_count = 0

        def record_loss(reason: str, event: dict[str, Any], detail: str) -> None:
            """Record one event this decoder could not consume losslessly.

            The count is always exact; the retained detail is bounded. A loss is
            never silent: it is a failure reason, it clears the parse/assembly
            completeness flags and it refuses the whole assembled batch.
            """
            nonlocal lost_event_count
            lost_event_count += 1
            if reason not in loss_reasons:
                loss_reasons.append(reason)
            if len(lost_events) < _MAX_RECORDED_LOSSES:
                lost_events.append({
                    "reason": reason, "sequence": event.get("sequence"),
                    "observed_bytes": event.get("observed_bytes"),
                    "sha256": event.get("sha256"), "detail": detail,
                })

        def record_stream_shape() -> None:
            calls = [_openai_tool_call(active_tool_calls[i]["id"], active_tool_calls[i]["name"],
                     active_tool_calls[i]["arguments"]) for i in sorted(active_tool_calls)]
            call.observe_response_shape(stop_reason=stop_reason, content="".join(text_parts),
                                        reasoning_content="".join(reasoning_parts), calls=calls)

        stream_termination = "abandoned"
        network_complete = False  # True only after the provider's own terminal marker.
        try:
            lines = iter(response)
            while True:
                try:
                    line = next(lines)
                except StopIteration:
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError,
                        http.client.IncompleteRead, http.client.RemoteDisconnected) as transport_error:
                    # Only a failure reading the provider socket permits a
                    # retry. Output callbacks and local storage can raise the
                    # same exception classes without any upstream failure.
                    raise ProviderStreamError({"error": {"type": type(transport_error).__name__}},
                        headers=getattr(response, "headers", None), transport=True,
                        partial=bool(text_parts or reasoning_parts or active_tool_calls)) from transport_error
                # Raw transport event, recorded before decoding or classification.
                observed = raw_capture.observe_line(line)
                call.check_first_output()
                try:
                    line_str = line.decode("utf-8").strip()
                except UnicodeDecodeError as decode_error:
                    record_loss("undecodable_event", observed, type(decode_error).__name__)
                    continue
                if not line_str or line_str.startswith(":"):
                    continue  # SSE comment or event separator: framing, not payload.
                if line_str.startswith("data:"):
                    payload = line_str[5:]
                    if payload.startswith(" "):
                        payload = payload[1:]
                elif _OTHER_SSE_FIELDS.match(line_str):
                    continue  # Recognized framing field this shape ignores.
                else:
                    record_loss("unsupported_event_shape", observed, line_str[:64])
                    continue
                if not payload.strip():
                    continue  # An empty data field carries no event.
                if payload == "[DONE]":
                    stream_termination = "done_marker"
                    network_complete = True
                    try:
                        record_stream_shape()
                    finally:
                        call.observe({}, complete=True)
                    break
                try:
                    event_body = json.loads(payload)
                except json.JSONDecodeError as event_error:
                    # The bytes for this event are known, but its content is not.
                    # Continuing would assemble a proposal from a stream we know
                    # is missing information, so the loss is recorded instead.
                    record_loss("unparseable_event", observed, type(event_error).__name__)
                    continue
                if not isinstance(event_body, dict):
                    record_loss("unsupported_event_shape", observed,
                                f"decoded {type(event_body).__name__} is not an event object")
                    continue
                event = event_body

                if event.get("error"):
                    call.outcome = "failed"
                usage = event.get("usage")
                if isinstance(usage, dict):
                    try:
                        if not event.get("choices"):
                            record_stream_shape()
                    finally:
                        call.observe(usage, complete=not event.get("choices"))
                    stream_input_tokens = usage.get("prompt_tokens", 0)
                    stream_output_tokens = usage.get("completion_tokens", 0)
                call.check()
                if event.get("error"):
                    raise ProviderStreamError(event, headers=getattr(response, "headers", None),
                        partial=bool(text_parts or reasoning_parts or active_tool_calls))

                choices = event.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                if delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"):
                    call.observe_first_output()
                finish_reason = choices[0].get("finish_reason")
                if finish_reason:
                    stop_reason = finish_reason

                # Text content
                content = delta.get("content", "")
                if content:
                    text_parts.append(content)
                    on_stream_chunk(content)
                reasoning_content = delta.get("reasoning_content", "")
                if reasoning_content:
                    reasoning_parts.append(reasoning_content)
                    if on_thinking_delta:
                        on_thinking_delta(reasoning_content)

                # Tool calls (incremental)
                tc_deltas = delta.get("tool_calls", [])
                for tc_delta in tc_deltas:
                    idx = tc_delta.get("index", 0)
                    if idx not in active_tool_calls:
                        active_tool_calls[idx] = {
                            "id": tc_delta.get("id", ""),
                            "name": "",
                            "arguments": "",
                        }
                    func = tc_delta.get("function", {})
                    if func.get("name"):
                        active_tool_calls[idx]["name"] = func["name"]
                    if func.get("arguments"):
                        active_tool_calls[idx]["arguments"] += func["arguments"]
                    if tc_delta.get("id"):
                        active_tool_calls[idx]["id"] = tc_delta["id"]
        except BaseException as stream_error:
            # The capture is frozen with the real termination, never a guess.
            call.finish_raw_capture(transport_termination="exception",
                                    error_type=type(stream_error).__name__, complete=False)
            raise
        if stream_termination == "abandoned":
            # Iteration ended without the provider's terminal marker.
            stream_termination = "eof"
        parse_complete = not loss_reasons
        transport_complete = network_complete and parse_complete
        call.record_stream_integrity(
            transport_termination=stream_termination, network_complete=network_complete,
            parse_complete=parse_complete, assembly_complete=parse_complete,
            loss_reasons=list(loss_reasons), lost_events=lost_events,
            lost_event_count=lost_event_count)
        call.finish_raw_capture(transport_termination=stream_termination,
                                complete=transport_complete)
        # Finalize tool calls
        raw_arguments = []
        for idx in sorted(active_tool_calls.keys()):
            tc = active_tool_calls[idx]
            raw_arguments.append(tc["arguments"])
            tool_calls.append(_openai_tool_call(tc["id"], tc["name"], tc["arguments"]))

        call.observe_response_shape(stop_reason=stop_reason, content="".join(text_parts),
                                    reasoning_content="".join(reasoning_parts), calls=tool_calls)
        
        # Streaming cost tracking
        if store:
            # Estimate if not provided in stream
            if stream_input_tokens == 0:
                from minicode.context_manager import estimate_messages_tokens
                stream_input_tokens = estimate_messages_tokens(messages)
            if stream_output_tokens == 0:
                stream_output_tokens = len("".join(text_parts)) // 4
            
            cost_usd = calculate_cost(
                model=self.runtime.get("model", ""),
                input_tokens=stream_input_tokens,
                output_tokens=stream_output_tokens,
            )
            if cost_usd > 0:
                store.set_state(add_cost(cost_usd))
            store.set_state(update_context_usage(stream_input_tokens + stream_output_tokens))
        
        parsed_text, kind = _parse_assistant_text("".join(text_parts).strip())
        output_token_limit = call.reserved_output_tokens()
        if not network_complete:
            incomplete_reason = (
                f"stream ended with transport_termination={stream_termination} "
                "before the provider's terminal marker")
        elif loss_reasons:
            incomplete_reason = (
                f"the transport completed but {lost_event_count} event(s) could not be consumed "
                f"losslessly ({','.join(loss_reasons)}); the assembled proposal is not trustworthy")
        else:
            incomplete_reason = None
        diagnostics = StepDiagnostics(
            stopReason=stop_reason,
            blockTypes=["tool_calls"] if tool_calls else (["text"] if text_parts else []),
            ignoredBlockTypes=[],
            transportComplete=transport_complete,
            transportTermination=stream_termination,
            rawResponseIncompleteReason=incomplete_reason,
            networkComplete=network_complete,
            parseComplete=parse_complete,
            assemblyComplete=parse_complete,
            lostEventCount=lost_event_count,
            streamLossReasons=list(loss_reasons),
            argumentDiagnostics=classify_calls(
                tool_calls, finish_reason=stop_reason, transport_complete=transport_complete,
                transport_termination=stream_termination, raw_arguments=raw_arguments,
                output_token_limit=output_token_limit,
                output_tokens_used=(stream_output_tokens if stream_output_tokens > 0 else None)),
        )
        
        if tool_calls:
            return AgentStep(
                type="tool_calls",
                calls=tool_calls,
                content=parsed_text,
                contentKind=kind,
                reasoningContent="".join(reasoning_parts),
                diagnostics=diagnostics,
            )
        return AgentStep(
            type="assistant",
            content=parsed_text,
            kind=kind,
            reasoningContent="".join(reasoning_parts),
            diagnostics=diagnostics,
        )
