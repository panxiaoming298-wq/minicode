from __future__ import annotations

import io
import json
import urllib.error
from http.client import HTTPMessage
from typing import Any

import pytest

from minicode.openai_adapter import (
    DEFAULT_OPENAI_USER_AGENT,
    OpenAIModelAdapter,
    _to_openai_messages,
)
from minicode.model_registry import create_model_adapter


class _DummyTools:
    def list(self) -> list[object]:
        return []


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body


def _runtime() -> dict[str, str]:
    return {
        "model": "gpt5.5",
        "openaiBaseUrl": "https://www.cctq.ai",
        "openaiApiKey": "test-key",
    }


def test_openai_adapter_sets_compatible_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _fake_urlopen(request, timeout=0):  # noqa: ANN001
        captured["user_agent"] = request.get_header("User-agent")
        return _FakeResponse({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    adapter = OpenAIModelAdapter(_runtime(), _DummyTools())

    step = adapter.next([{"role": "user", "content": "Reply with exactly OK."}])

    assert step.content == "OK"
    assert captured["user_agent"] == DEFAULT_OPENAI_USER_AGENT


@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        ("https://www.cctq.ai", "https://www.cctq.ai/v1/chat/completions"),
        ("https://www.cctq.ai/", "https://www.cctq.ai/v1/chat/completions"),
        ("https://www.cctq.ai/v1", "https://www.cctq.ai/v1/chat/completions"),
        ("https://www.cctq.ai/v1/", "https://www.cctq.ai/v1/chat/completions"),
        ("https://www.cctq.ai/v1/chat/completions", "https://www.cctq.ai/v1/chat/completions"),
    ],
)
def test_openai_adapter_accepts_provider_root_api_base_or_full_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    expected_url: str,
) -> None:
    captured: dict[str, str] = {}

    def _fake_urlopen(request, timeout=0):  # noqa: ANN001
        captured["url"] = request.full_url
        return _FakeResponse({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    runtime = {**_runtime(), "openaiBaseUrl": base_url}
    adapter = OpenAIModelAdapter(runtime, _DummyTools())

    step = adapter.next([{"role": "user", "content": "Reply with exactly OK."}])

    assert step.content == "OK"
    assert captured["url"] == expected_url


def test_openai_adapter_does_not_duplicate_explicit_provider_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _fake_urlopen(request, timeout=0):  # noqa: ANN001
        captured["url"] = request.full_url
        return _FakeResponse({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    runtime = {
        **_runtime(),
        "openaiBaseUrl": "https://api.deepseek.com/chat/completions",
        "_openai_chat_completions_path": "/chat/completions",
    }

    OpenAIModelAdapter(runtime, _DummyTools()).next(
        [{"role": "user", "content": "Reply with exactly OK."}]
    )

    assert captured["url"] == "https://api.deepseek.com/chat/completions"


def test_openai_adapter_surfaces_non_json_http_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_urlopen(request, timeout=0):  # noqa: ANN001
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=HTTPMessage(),
            fp=io.BytesIO(b"error code: 1010"),
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    adapter = OpenAIModelAdapter(_runtime(), _DummyTools())

    with pytest.raises(RuntimeError, match="error code: 1010"):
        adapter.next([{"role": "user", "content": "Reply with exactly OK."}])


def test_openai_adapter_does_not_retry_permanent_503_model_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def _fake_urlopen(request, timeout=0):  # noqa: ANN001
        calls["count"] += 1
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "Unavailable",
            hdrs=HTTPMessage(),
            fp=io.BytesIO(
                json.dumps(
                    {
                        "error": {
                            "code": "model_not_found",
                            "message": "No available channel for model gpt5.5 under group Codex-Sale",
                        }
                    }
                ).encode("utf-8")
            ),
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    adapter = OpenAIModelAdapter(_runtime(), _DummyTools())

    with pytest.raises(RuntimeError, match="No available channel for model gpt5.5"):
        adapter.next([{"role": "user", "content": "Reply with exactly OK."}])

    assert calls["count"] == 1


def test_deepseek_provider_uses_official_endpoint_and_reasoning_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    thinking_deltas: list[str] = []

    def _fake_urlopen(request, timeout=0):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": "I should inspect the repository.",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    adapter = create_model_adapter(
        "deepseek-v4-pro",
        _DummyTools(),
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "deepseekApiKey": "deepseek-test-key",
            "deepseekBaseUrl": "https://api.deepseek.com",
            "deepseekThinking": "enabled",
            "deepseekReasoningEffort": "max",
        },
    )

    step = adapter.next(
        [{"role": "user", "content": "Inspect the README."}],
        on_thinking_delta=thinking_deltas.append,
    )

    assert isinstance(adapter, OpenAIModelAdapter)
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer deepseek-test-key"
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert captured["body"]["reasoning_effort"] == "max"
    assert "tools" not in captured["body"]
    assert step.type == "tool_calls"
    assert step.reasoningContent == "I should inspect the repository."
    assert thinking_deltas == ["I should inspect the repository."]


def test_openai_messages_round_trip_reasoning_and_parallel_tool_calls() -> None:
    _system, messages = _to_openai_messages(
        [
            {
                "role": "assistant_progress",
                "content": "Checking both files.",
                "reasoningContent": "Need both inputs before deciding.",
            },
            {
                "role": "assistant_tool_call",
                "toolUseId": "call-1",
                "toolName": "read_file",
                "input": {"path": "README.md"},
                "reasoningContent": "Need both inputs before deciding.",
            },
            {
                "role": "assistant_tool_call",
                "toolUseId": "call-2",
                "toolName": "read_file",
                "input": {"path": "pyproject.toml"},
                "reasoningContent": "Need both inputs before deciding.",
            },
            {
                "role": "tool_result",
                "toolUseId": "call-1",
                "content": "readme",
                "isError": False,
            },
            {
                "role": "tool_result",
                "toolUseId": "call-2",
                "content": "project",
                "isError": False,
            },
        ]
    )

    assert [message["role"] for message in messages] == ["assistant", "tool", "tool"]
    assert messages[0]["reasoning_content"] == "Need both inputs before deciding."
    assert [call["id"] for call in messages[0]["tool_calls"]] == ["call-1", "call-2"]
