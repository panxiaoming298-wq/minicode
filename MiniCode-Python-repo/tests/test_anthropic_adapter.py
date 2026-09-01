import json
import urllib.error

import pytest
from minicode.anthropic_adapter import AnthropicModelAdapter, _messages_endpoint
from minicode.openai_adapter import OpenAIModelAdapter
from minicode.model_registry import create_model_adapter
from minicode.tooling import ToolDefinition, ToolRegistry


class DummyResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                name="read_file",
                description="Read file",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda _input, _context: None,
            )
        ]
    )


def test_anthropic_adapter_parses_tool_use(monkeypatch) -> None:
    payload = {
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "<progress>thinking</progress>"},
            {"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {"path": "README.md"}},
        ],
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=60: DummyResponse(payload))
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://api.anthropic.com", "authToken": "x"},
        _tool_registry(),
    )

    step = adapter.next([{"role": "system", "content": "sys"}, {"role": "user", "content": "read me"}])

    assert step.type == "tool_calls"
    assert step.content == "thinking"
    assert step.contentKind == "progress"
    assert step.calls[0]["toolName"] == "read_file"


def test_anthropic_adapter_parses_final_text(monkeypatch) -> None:
    payload = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "<final>done</final>"}],
    }
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout=60: DummyResponse(payload))
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://api.anthropic.com", "authToken": "x"},
        _tool_registry(),
    )

    step = adapter.next([{"role": "system", "content": "sys"}, {"role": "user", "content": "finish"}])

    assert step.type == "assistant"
    assert step.content == "done"
    assert step.kind == "final"


def test_messages_endpoint_normalizes_base_url_variants() -> None:
    assert _messages_endpoint("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"
    assert _messages_endpoint("https://proxy.example.com/v1") == "https://proxy.example.com/v1/messages"
    assert _messages_endpoint("https://proxy.example.com/v1/messages") == "https://proxy.example.com/v1/messages"


def test_anthropic_adapter_uses_normalized_messages_endpoint(monkeypatch) -> None:
    payload = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "<final>ok</final>"}],
    }
    seen: dict[str, str] = {}

    def _fake_urlopen(request, timeout=60):
        seen["url"] = request.full_url
        return DummyResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://proxy.example.com/v1", "authToken": "x"},
        _tool_registry(),
    )

    step = adapter.next([{"role": "system", "content": "sys"}, {"role": "user", "content": "finish"}])

    assert step.type == "assistant"
    assert seen["url"] == "https://proxy.example.com/v1/messages"


def test_anthropic_adapter_surfaces_underlying_url_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=60: (_ for _ in ()).throw(urllib.error.URLError("connection refused")),
    )
    monkeypatch.setenv("MINI_CODE_MAX_RETRIES", "0")
    adapter = AnthropicModelAdapter(
        {"model": "claude", "baseUrl": "https://proxy.example.com/v1", "authToken": "x"},
        _tool_registry(),
    )

    with pytest.raises(RuntimeError, match="connection refused"):
        adapter.next([{"role": "system", "content": "sys"}, {"role": "user", "content": "finish"}])


def test_create_model_adapter_overrides_stale_anthropic_runtime_model() -> None:
    runtime = {
        "model": "deepseek-v4-pro[1m]",
        "baseUrl": "https://proxy.example.com/v1",
        "authToken": "x",
    }

    adapter = create_model_adapter(
        "claude-haiku-3-20240307",
        tools=_tool_registry(),
        runtime=runtime,
    )

    # Provider detection may select OpenAI adapter when the URL format
    # suggests an OpenAI-compatible endpoint, even for anthropic model names.
    assert isinstance(adapter, (AnthropicModelAdapter, OpenAIModelAdapter))
    assert adapter.runtime.get("model") == "claude-haiku-3-20240307"

