from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

from minicode.api_retry import (
    RETRYABLE_STATUS,
    calculate_backoff,
)
from minicode.provider_usage import ProviderCall
from minicode.state import add_cost, record_api_error, update_context_usage
from minicode.tooling import model_tool_registry
from minicode.types import AgentStep, StepDiagnostics

if TYPE_CHECKING:
    from minicode.state import AppState, Store

DEFAULT_MAX_RETRIES = 4


def _get_retry_limit() -> int:
    try:
        value = int(float(os.environ.get("MINI_CODE_MAX_RETRIES", DEFAULT_MAX_RETRIES)))
    except ValueError:
        value = DEFAULT_MAX_RETRIES
    return max(0, value)


def _parse_retry_after_seconds(retry_after: str | None) -> float | None:
    """Parse Retry-After header into seconds."""
    if not retry_after:
        return None
    try:
        seconds = float(retry_after)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(retry_after)
        return max(0.0, target.timestamp() - time.time())
    except (ValueError, TypeError):
        pass
    return None


def _read_json_body(response) -> Any:
    text = response.read().decode("utf-8")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": {"message": text.strip()}}


def _extract_error_message(data: Any, status: int) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return f"Model request failed: {status}"


def _messages_endpoint(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/messages"
    return normalized + "/v1/messages"


def _parse_assistant_text(content: str) -> tuple[str, str | None]:
    trimmed = content.strip()
    if not trimmed:
        return "", None
    markers = [
        ("<final>", "final", "</final>"),
        ("[FINAL]", "final", None),
        ("<progress>", "progress", "</progress>"),
        ("[PROGRESS]", "progress", None),
    ]
    for prefix, kind, closing_tag in markers:
        if trimmed.startswith(prefix):
            raw = trimmed[len(prefix) :].strip()
            if closing_tag:
                raw = raw.replace(closing_tag, "").strip()
            return raw, kind
    return trimmed, None


def _to_text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _to_assistant_text(message: dict[str, Any]) -> str:
    if message["role"] == "assistant_progress":
        return f"<progress>\n{message['content']}\n</progress>"
    return message["content"]


def _push_anthropic_message(messages: list[dict[str, Any]], role: str, block: dict[str, Any]) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": role, "content": [block]})


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        if role == "user":
            _push_anthropic_message(converted, "user", _to_text_block(message["content"]))
            continue
        if role in {"assistant", "assistant_progress"}:
            _push_anthropic_message(converted, "assistant", _to_text_block(_to_assistant_text(message)))
            continue
        if role == "assistant_tool_call":
            _push_anthropic_message(
                converted,
                "assistant",
                {"type": "tool_use", "id": message["toolUseId"], "name": message["toolName"], "input": message["input"]},
            )
            continue
        _push_anthropic_message(
            converted,
            "user",
            {
                "type": "tool_result",
                "tool_use_id": message["toolUseId"],
                "content": message["content"],
                "is_error": message["isError"],
            },
        )
    return system, converted


class AnthropicModelAdapter:
    supports_runtime_usage = True

    def __init__(self, runtime: dict[str, Any], tools) -> None:
        self.runtime = runtime
        self.tools = tools
        # Cache the serialized tool list — tools rarely change within a session
        self._cached_tools_json: list[dict[str, Any]] | None = None
        self._tools_cache_key: int = 0  # hash of tool list for invalidation
        self._thinking_blocks: list[dict[str, Any]] = []  # Preserve thinking blocks for round-trip

    def _get_serialized_tools(self) -> list[dict[str, Any]]:
        """Get serialized tool list with caching."""
        from minicode.tooling import ToolRegistry

        registry = model_tool_registry(self.tools) or self.tools or ToolRegistry([])
        current_tools = registry.list()
        current_key = hash(tuple((t.name, t.description, json.dumps(t.input_schema, sort_keys=True)) for t in current_tools))
        if self._cached_tools_json is None or current_key != self._tools_cache_key:
            self._cached_tools_json = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in current_tools
            ]
            self._tools_cache_key = current_key
        return self._cached_tools_json

    def next(self, messages: list[dict[str, Any]], on_stream_chunk: Callable[[str], None] | None = None, on_thinking_delta: Callable[[str], None] | None = None, store: Store[AppState] | None = None) -> AgentStep:
        with ProviderCall(self.runtime, "anthropic") as call:
            step = self._next(messages, on_stream_chunk, on_thinking_delta, store, call=call)
        step.usage = call.usage
        step.imageInputIds = list(call.image_input_ids)
        if step.diagnostics is not None:
            step.diagnostics.outputBudget = dict(call.request_diagnostics.get("output_budget", {}))
        return step

    def context_request(self, messages, *, stream=False):
        """Pure request preview; replay state is consumed only by dispatch."""
        system_message, converted_messages = _to_anthropic_messages(messages)

        # Replay stored thinking blocks into the first assistant message
        # with text content (DeepSeek extended thinking round-trip)
        if self._thinking_blocks:
            for i in range(len(converted_messages)):
                msg = converted_messages[i]
                if msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        converted_messages[i] = dict(msg)
                        converted_messages[i]["content"] = list(self._thinking_blocks) + content
                        break

        request_body = {
            "model": self.runtime.get("model", ""),
            "system": system_message,
            "messages": converted_messages,
            "tools": self._get_serialized_tools(),
        }
        # Disable extended thinking for non-Anthropic models that support it
        # but require round-trip preservation our message format can't provide
        if self.runtime.get("disableThinking"):
            request_body["thinking"] = {"type": "disabled"}
        if self.runtime.get("maxOutputTokens") is not None:
            request_body["max_tokens"] = self.runtime["maxOutputTokens"]
        if stream:
            request_body["stream"] = True
        from minicode.image_input import prepare_request
        endpoint = _messages_endpoint(self.runtime.get("baseUrl", ""))
        return prepare_request(request_body, self.runtime, "anthropic", endpoint, source_messages=messages), endpoint, "anthropic"

    def _next(self, messages: list[dict[str, Any]], on_stream_chunk: Callable[[str], None], on_thinking_delta: Callable[[str], None], store: Store[AppState] | None, *, call: ProviderCall) -> AgentStep:
        request_body, endpoint, _ = self.context_request(messages, stream=bool(on_stream_chunk))
        self._thinking_blocks = []

        request = urllib.request.Request(
            url=endpoint,
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                **(
                    {"x-api-key": self.runtime["apiKey"]}
                    if self.runtime.get("apiKey")
                    else {"Authorization": f"Bearer {self.runtime.get('authToken', '')}"}
                ),
            },
            method="POST",
        )

        max_retries = _get_retry_limit()
        response = None
        last_exception: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                timeout = int(os.environ.get("MINICODE_MODEL_TIMEOUT", "60"))
                response = call.open(request, timeout=timeout)
                break
            except urllib.error.HTTPError as error:
                last_exception = error
                response = error
                if error.code not in RETRYABLE_STATUS or attempt >= max_retries:
                    break
                # Use semantic error classification for adaptive backoff
                from minicode.api_retry import classify_error
                category = classify_error(error)
                retry_after = _parse_retry_after_seconds(error.headers.get("retry-after"))
                wait = calculate_backoff(attempt, retry_after=retry_after,
                                        category=category)
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                last_exception = error
                response = None
                if attempt >= max_retries:
                    if isinstance(error, urllib.error.URLError):
                        break
                    raise
                wait = calculate_backoff(attempt)
                time.sleep(wait)
        if response is None:
            if last_exception is not None:
                raise RuntimeError(
                    f"Model request failed before receiving a response: {last_exception}"
                ) from last_exception
            raise RuntimeError("Model request failed before receiving a response")

        if not on_stream_chunk:
            data = _read_json_body(response)
            call.observe(data.get("usage"), complete=True)
            call.check()
            status = getattr(response, "status", getattr(response, "code", 200))
            if status >= 400:
                if store:
                    store.set_state(record_api_error())
                raise RuntimeError(_extract_error_message(data, status))
    
            # Update store with API call success and cost tracking
            if store:
                # Calculate token usage and cost (with cache support)
                from minicode.cost_tracker import calculate_cost
                usage = data.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
                
                cost_usd = calculate_cost(
                    model=self.runtime.get("model", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                )
                if cost_usd > 0:
                    store.set_state(add_cost(cost_usd))
                
                # Update context usage
                total_tokens = input_tokens + output_tokens
                store.set_state(update_context_usage(total_tokens))
    
            tool_calls: list[dict[str, Any]] = []
            text_parts: list[str] = []
            block_types: list[str] = []
            ignored_block_types: list[str] = []
    
            for block in data.get("content", []) if isinstance(data, dict) else []:
                block_type = block.get("type")
                block_types.append(block_type)
                if block_type == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block_type == "tool_use" and isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
                    tool_calls.append({"id": block["id"], "toolName": block["name"], "input": block.get("input")})
                elif block_type == "thinking":
                    self._thinking_blocks.append(block)  # Preserve for round-trip
                else:
                    ignored_block_types.append(str(block_type))
    
            parsed_text, kind = _parse_assistant_text("\n".join(text_parts).strip())
            diagnostics = StepDiagnostics(
                stopReason=data.get("stop_reason") if isinstance(data, dict) else None,
                blockTypes=block_types,
                ignoredBlockTypes=ignored_block_types,
            )
    
            if tool_calls:
                return AgentStep(
                    type="tool_calls",
                    calls=tool_calls,
                    content=parsed_text,
                    contentKind=kind,
                    diagnostics=diagnostics,
                )
            return AgentStep(type="assistant", content=parsed_text, kind=kind, diagnostics=diagnostics)

        # STREAMING PARSER
        tool_calls = []
        text_parts = []
        block_types = []
        ignored_block_types = []
        active_tool_call = None
        active_thinking_block = None
        active_block = False
        active_block_index = None
        stop_reason = None
        saw_message_stop = False
        parse_complete = True
        assembly_complete = True
        loss_reasons = []
        lost_event_count = 0

        def record_loss(reason):
            nonlocal lost_event_count
            lost_event_count += 1
            if reason not in loss_reasons:
                loss_reasons.append(reason)
        
        # Streaming cost tracking
        stream_input_tokens = 0
        stream_output_tokens = 0
        stream_cache_read_tokens = 0
        stream_cache_creation_tokens = 0
        
        for line in response:
            call.check_first_output()
            try:
                line_str = line.decode("utf-8").strip()
            except UnicodeDecodeError:
                parse_complete = False
                record_loss("invalid_utf8")
                continue
            if not line_str.startswith("data:"):
                continue
            data_str = line_str[5:].strip()
            if data_str == "[DONE]":
                continue
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                parse_complete = False
                record_loss("invalid_json")
                continue
            if not isinstance(event, dict):
                parse_complete = False
                record_loss("invalid_event")
                continue
                
            etype = event.get("type")
            if etype == "message_start":
                # Initial usage from message_start
                msg = event.get("message", {})
                usage = msg.get("usage", {})
                call.observe(usage, complete=False)
                stream_input_tokens = usage.get("input_tokens", 0)
                stream_cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                stream_cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
            elif etype == "content_block_start":
                if active_block:
                    assembly_complete = False
                    record_loss("overlapping_content_blocks")
                active_block = True
                active_block_index = event.get("index")
                cb = event.get("content_block", {})
                c_type = cb.get("type")
                block_types.append(c_type)
                if c_type == "tool_use":
                    if cb.get("name"):
                        call.observe_first_output()
                    active_tool_call = {
                        "id": cb.get("id"),
                        "name": cb.get("name"),
                        "input": cb.get("input", {}),
                        "input_json": ""
                    }
                elif c_type == "thinking":
                    active_thinking_block = {"type": "thinking", "thinking": ""}
            elif etype == "content_block_delta":
                if not active_block or event.get("index") != active_block_index:
                    assembly_complete = False
                    record_loss("unmatched_content_delta")
                delta = event.get("delta", {})
                if delta.get("text") or delta.get("thinking") or delta.get("partial_json"):
                    call.observe_first_output()
                d_type = delta.get("type")
                if d_type == "text_delta":
                    chunk = delta.get("text", "")
                    text_parts.append(chunk)
                    on_stream_chunk(chunk)
                elif d_type == "input_json_delta":
                    if active_tool_call:
                        active_tool_call["input_json"] += delta.get("partial_json", "")
                elif d_type == "thinking_delta":
                    if active_thinking_block:
                        chunk = delta.get("thinking", "")
                        active_thinking_block["thinking"] += chunk
                        if on_thinking_delta:
                            on_thinking_delta(chunk)
                elif d_type == "signature_delta":
                    if active_thinking_block:
                        active_thinking_block["signature"] = active_thinking_block.get("signature", "") + delta.get("signature", "")
            elif etype == "content_block_stop":
                if not active_block or event.get("index") != active_block_index:
                    assembly_complete = False
                    record_loss("unmatched_content_stop")
                active_block = False
                active_block_index = None
                if active_tool_call:
                    try:
                        parsed_input = (json.loads(active_tool_call["input_json"])
                            if active_tool_call["input_json"] else active_tool_call["input"])
                    except json.JSONDecodeError:
                        parse_complete = False
                        record_loss("invalid_tool_arguments")
                        parsed_input = None
                    tool_calls.append({
                        "id": active_tool_call["id"],
                        "toolName": active_tool_call["name"],
                        "input": parsed_input
                    })
                    active_tool_call = None
                if active_thinking_block:
                    self._thinking_blocks.append(active_thinking_block)
                    active_thinking_block = None
            elif etype == "message_delta":
                delta = event.get("delta", {})
                if "stop_reason" in delta:
                    stop_reason = delta["stop_reason"]
                # Final output tokens from message_delta
                usage = event.get("usage", {})
                call.observe(usage, complete=bool(delta.get("stop_reason")))
                if usage.get("output_tokens"):
                    stream_output_tokens = usage["output_tokens"]
            elif etype == "message_stop":
                call.observe({}, complete=True)
                saw_message_stop = True
                call.check()
                break
            elif etype == "error":
                err = event.get("error", {})
                raise RuntimeError(f"Streaming error: {err.get('message', 'Unknown')}")
            call.check()

        if active_block:
            assembly_complete = False
            record_loss("unclosed_content_block")
        if active_tool_call is not None:
            # Keep the interrupted proposal visible to the batch refusal path.
            # It must not be mistaken for an assistant-only response or executed.
            tool_calls.append({"id": active_tool_call["id"], "toolName": active_tool_call["name"],
                "input": None, "rawArguments": active_tool_call["input_json"],
                "inputError": "Stream ended before the tool content block completed"})
        if not saw_message_stop:
            loss_reasons.append("missing_message_stop")
        transport_complete = saw_message_stop and parse_complete and assembly_complete
        if not transport_complete:
            self._thinking_blocks = []
        termination = "message_stop" if saw_message_stop else "eof"
        call.record_stream_integrity(transport_termination=termination,
            network_complete=saw_message_stop, parse_complete=parse_complete,
            assembly_complete=assembly_complete, loss_reasons=loss_reasons,
            lost_events=[], lost_event_count=lost_event_count)
        
        # Update store with streaming cost tracking
        if store:
            from minicode.cost_tracker import calculate_cost
            cost_usd = calculate_cost(
                model=self.runtime.get("model", ""),
                input_tokens=stream_input_tokens,
                output_tokens=stream_output_tokens,
                cache_read_tokens=stream_cache_read_tokens,
                cache_creation_tokens=stream_cache_creation_tokens,
            )
            if cost_usd > 0:
                store.set_state(add_cost(cost_usd))
            total_tokens = stream_input_tokens + stream_output_tokens
            store.set_state(update_context_usage(total_tokens))
                
        parsed_text, kind = _parse_assistant_text("".join(text_parts).strip())
        diagnostics = StepDiagnostics(
            stopReason=stop_reason,
            blockTypes=block_types,
            ignoredBlockTypes=ignored_block_types,
            transportComplete=transport_complete,
            transportTermination=termination,
            rawResponseIncompleteReason=None if transport_complete else ",".join(loss_reasons),
            networkComplete=saw_message_stop,
            parseComplete=parse_complete,
            assemblyComplete=assembly_complete,
            lostEventCount=lost_event_count,
            streamLossReasons=loss_reasons,
        )
        if tool_calls:
            return AgentStep(
                type="tool_calls",
                calls=tool_calls,
                content=parsed_text,
                contentKind=kind,
                diagnostics=diagnostics,
            )
        return AgentStep(type="assistant", content=parsed_text, kind=kind, diagnostics=diagnostics)
