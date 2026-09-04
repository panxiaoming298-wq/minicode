import threading

from minicode.agent_loop import (
    _execute_single_tool,
    _fetch_has_lookup_evidence,
    run_agent_turn,
)
from minicode.model_switcher import ModelSwitcher
from minicode.state import create_app_store
from minicode.tooling import ToolDefinition, ToolRegistry, ToolResult
from minicode.types import (
    AgentStep,
    ChatMessage,
    ModelAdapter,
    RuntimeEvent,
    StepDiagnostics,
)
from types import SimpleNamespace


class ScriptedModel(ModelAdapter):
    def __init__(self, steps: list[AgentStep]) -> None:
        self._steps = steps
        self.calls = 0

    def next(self, messages: list[ChatMessage], on_stream_chunk=None) -> AgentStep:
        step = self._steps[self.calls]
        self.calls += 1
        return step


class StoreCapturingModel(ModelAdapter):
    def __init__(self) -> None:
        self.received_store = None

    def next(self, messages: list[ChatMessage], on_stream_chunk=None, store=None) -> AgentStep:
        self.received_store = store
        return AgentStep(type="assistant", content="done")


class ProviderUnavailableModel(ModelAdapter):
    model_id = "deepseek-v4-pro[1m]"

    def next(self, messages: list[ChatMessage], on_stream_chunk=None, store=None) -> AgentStep:
        raise RuntimeError("No available channel for model deepseek-v4-pro[1m] under group cc")


class NamedProviderUnavailableModel(ModelAdapter):
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def next(self, messages: list[ChatMessage], on_stream_chunk=None, store=None) -> AgentStep:
        raise RuntimeError(f"No available channel for model {self.model_id} under group cc")


def test_tool_timeout_does_not_wait_for_noncooperative_tool() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    result_box = {}

    def block(_input_data: dict, _context) -> ToolResult:
        started.set()
        release.wait(timeout=5)
        return ToolResult(ok=True, output="late")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="block",
                description="block",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=block,
            )
        ]
    )

    def execute() -> None:
        result_box["result"] = _execute_single_tool(
            {"id": "timeout", "toolName": "block", "input": {}},
            registry,
            ".",
            None,
            None,
            {},
            None,
            1,
            None,
            None,
            tool_scheduler=SimpleNamespace(_force_tool_timeout=0.01),
        )
        finished.set()

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert started.wait(timeout=1)
        assert finished.wait(timeout=1)
        assert result_box["result"].ok is False
        assert "timed out" in result_box["result"].output
    finally:
        release.set()
        thread.join(timeout=1)


def test_tool_executor_crash_is_not_retried_as_a_duplicate_call() -> None:
    class CrashingRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, _name, _input, _context):
            self.calls += 1
            raise RuntimeError("boom")

    registry = CrashingRegistry()
    result = _execute_single_tool(
        {"id": "crash", "toolName": "crash", "input": {}},
        registry,
        ".",
        None,
        None,
        {},
        None,
        1,
        None,
        None,
    )

    assert result.ok is False
    assert "crashed (RuntimeError)" in result.output
    assert registry.calls == 1


class UnnamedProviderUnavailableModel(ModelAdapter):
    def __init__(self, error_model_id: str) -> None:
        self.error_model_id = error_model_id

    def next(self, messages: list[ChatMessage], on_stream_chunk=None, store=None) -> AgentStep:
        raise RuntimeError(f"No available channel for model {self.error_model_id} under group cc")


def test_agent_turn_executes_tool_and_returns_assistant() -> None:
    def run_echo(input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output=f"echo:{input_data['text']}")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_echo,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[{"id": "1", "toolName": "echo", "input": {"text": "hi"}}],
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert any(message["role"] == "tool_result" for message in messages)


def test_agent_turn_groups_parallel_tool_calls_before_results() -> None:
    def run_echo(input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output=f"echo:{input_data['text']}")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_echo,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[
                    {"id": "1", "toolName": "echo", "input": {"text": "a"}},
                    {"id": "2", "toolName": "echo", "input": {"text": "b"}},
                ],
                reasoningContent="Both inputs can be checked in parallel.",
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    relevant = [
        message
        for message in messages
        if message["role"] in {"assistant_tool_call", "tool_result"}
    ]
    assert [message["role"] for message in relevant] == [
        "assistant_tool_call",
        "assistant_tool_call",
        "tool_result",
        "tool_result",
    ]
    assert [message["toolUseId"] for message in relevant] == ["1", "2", "1", "2"]
    assert relevant[0]["reasoningContent"] == "Both inputs can be checked in parallel."


def test_agent_turn_emits_callbacks() -> None:
    events: list[tuple[str, str]] = []

    def run_echo(input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output=f"echo:{input_data['text']}")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_echo,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(type="tool_calls", content="working", contentKind="progress", calls=[{"id": "1", "toolName": "echo", "input": {"text": "hi"}}]),
            AgentStep(type="assistant", content="done"),
        ]
    )

    run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        on_tool_start=lambda name, _input: events.append(("start", name)),
        on_tool_result=lambda name, _output, _error: events.append(("result", name)),
        on_assistant_message=lambda content: events.append(("assistant", content)),
        on_progress_message=lambda content: events.append(("progress", content)),
    )

    assert ("progress", "working") in events
    assert ("start", "echo") in events
    assert ("result", "echo") in events
    assert ("assistant", "done") in events


def test_agent_turn_retries_empty_response_then_continues() -> None:
    model = ScriptedModel(
        [
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content="done"),
        ]
    )
    registry = ToolRegistry([])

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert any(
        message["role"] == "user" and "last response was empty" in message["content"]
        for message in messages
    )


def test_agent_turn_handles_recoverable_pause_turn() -> None:
    model = ScriptedModel(
        [
            AgentStep(
                type="assistant",
                content="",
                diagnostics=StepDiagnostics(stopReason="pause_turn", ignoredBlockTypes=["thinking"]),
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )
    registry = ToolRegistry([])
    progress_events: list[str] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        on_progress_message=progress_events.append,
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert any("pause_turn" in event for event in progress_events)


def test_agent_turn_returns_fallback_after_repeated_empty_responses() -> None:
    model = ScriptedModel(
        [
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
        ]
    )
    registry = ToolRegistry([])

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    assert "empty response" in messages[-1]["content"].lower()


def test_tool_registry_dispose_calls_disposer() -> None:
    disposed: list[bool] = []
    registry = ToolRegistry([], disposer=lambda: disposed.append(True))

    registry.dispose()

    assert disposed == [True]


def test_agent_turn_passes_store_to_provider_adapter() -> None:
    model = StoreCapturingModel()
    registry = ToolRegistry([])
    store = create_app_store()

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        store=store,
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert model.received_store is store


def test_single_deep_runtime_profile_allows_extra_turn_budget() -> None:
    def run_echo(input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output=f"echo:{input_data['text']}")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_echo,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[{"id": "1", "toolName": "echo", "input": {"text": "hi"}}],
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "continue the durable state repair"},
        ],
        cwd=".",
        max_steps=1,
        runtime={"runtimeProfile": "single-deep"},
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert model.calls == 2


def test_single_deep_runtime_profile_emits_phase_progress_updates() -> None:
    model = ScriptedModel(
        [
            AgentStep(
                type="assistant",
                content="scanning the relevant files",
                kind="progress",
            ),
            AgentStep(type="assistant", content="done"),
        ]
    )
    registry = ToolRegistry([])
    progress_events: list[str] = []
    runtime_events: list[RuntimeEvent] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "repair the runtime policy"},
        ],
        cwd=".",
        runtime={"runtimeProfile": "single-deep"},
        on_progress_message=progress_events.append,
        on_runtime_event=runtime_events.append,
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert any("Runtime phase: explore." in event for event in progress_events)
    assert any(event.category == "phase" and event.phase == "explore" for event in runtime_events)


def test_agent_turn_preserves_typed_await_user_from_tool_results() -> None:
    def run_gate(_input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output="Need your approval", awaitUser=True)

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="approval_gate",
                description="approval gate",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_gate,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[{"id": "1", "toolName": "approval_gate", "input": {}}],
            ),
        ]
    )

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
    )

    assert messages[-1] == {"role": "assistant", "content": "Need your approval"}


def test_single_deep_runtime_transitions_into_widened_mode() -> None:
    model = ScriptedModel(
        [
            AgentStep(type="assistant", content="still exploring", kind="progress"),
            AgentStep(type="assistant", content="still exploring", kind="progress"),
            AgentStep(type="assistant", content="still exploring", kind="progress"),
            AgentStep(type="assistant", content="still exploring", kind="progress"),
            AgentStep(type="assistant", content="still exploring", kind="progress"),
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content="done with a broader plan"),
        ]
    )
    registry = ToolRegistry([])
    progress_events: list[str] = []
    runtime_events: list[RuntimeEvent] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "repair the runtime policy"},
        ],
        cwd=".",
        runtime={"runtimeProfile": "single-deep"},
        on_progress_message=progress_events.append,
        on_runtime_event=runtime_events.append,
    )

    assert messages[-1] == {"role": "assistant", "content": "done with a broader plan"}
    assert any("wider search" in event.lower() or "widened mode" in event.lower() for event in progress_events)
    assert any("escalation trigger" in event.lower() for event in progress_events)
    assert any(event.category == "widening" for event in runtime_events)
    assert any(
        event.category == "stop" and event.stop_reason == "done"
        for event in runtime_events
    )
    assert any(
        message["role"] == "user" and "Switch to widened mode" in message["content"]
        for message in messages
    )


def test_single_deep_verify_phase_blocks_unsupported_final_until_evidence_is_cited() -> None:
    def run_echo(input_data: dict, _context) -> ToolResult:
        return ToolResult(ok=True, output=f"pytest: {input_data['suite']} passed")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_echo,
            )
        ]
    )
    model = ScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[{"id": "1", "toolName": "echo", "input": {"suite": "reader_probe"}}],
            ),
            AgentStep(
                type="assistant",
                content="I have enough context now.",
                kind="progress",
            ),
            AgentStep(type="assistant", content="Done, the fix is complete."),
            AgentStep(
                type="assistant",
                content="Verified with pytest: reader_probe passed, so the fix is complete.",
            ),
        ]
    )
    progress_events: list[str] = []
    runtime_events: list[RuntimeEvent] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "repair the runtime policy"},
        ],
        cwd=".",
        runtime={"runtimeProfile": "single-deep"},
        on_progress_message=progress_events.append,
        on_runtime_event=runtime_events.append,
    )

    assert messages[-1] == {
        "role": "assistant",
        "content": "Verified with pytest: reader_probe passed, so the fix is complete.",
    }
    assert any("verification guard" in event.lower() for event in progress_events)
    assert any(event.category == "guard" for event in runtime_events)
    assert any(
        event.category == "stop" and event.stop_reason == "done"
        for event in runtime_events
    )
    assert any(
        message["role"] == "user" and "strict verification mode" in message["content"]
        for message in messages
    )


def test_agent_turn_switches_to_fallback_model_on_provider_channel_error(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime_events: list[RuntimeEvent] = []
    seen: dict[str, str] = {}

    def _fake_create_model_adapter(model: str, tools, runtime=None, force_mock: bool = False):
        seen["model"] = model
        fallback_model = ScriptedModel([AgentStep(type="assistant", content="ok via fallback")])
        fallback_model.model_id = model
        return fallback_model

    monkeypatch.setenv("ANTHROPIC_MODEL_FALLBACKS", "qwen3.6-plus")
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(api_key="test-key"),
    )
    monkeypatch.setattr("minicode.model_switcher.create_model_adapter", _fake_create_model_adapter)

    messages = run_agent_turn(
        model=ProviderUnavailableModel(),
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        runtime={},
        on_runtime_event=runtime_events.append,
    )

    assert messages[-1] == {"role": "assistant", "content": "ok via fallback"}
    assert seen["model"] == "qwen3.6-plus"
    assert any(event.category == "recovery" and "qwen3.6-plus" in event.message for event in runtime_events)


def test_agent_turn_does_not_bounce_between_failed_provider_fallback_models(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime_events: list[RuntimeEvent] = []
    created_models: list[str] = []

    def _failing_create_model_adapter(model: str, tools, runtime=None, force_mock: bool = False):
        created_models.append(model)
        return NamedProviderUnavailableModel(model)

    monkeypatch.setenv("ANTHROPIC_MODEL_FALLBACKS", "claude-haiku-3-20240307")
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(api_key="test-key"),
    )
    monkeypatch.setattr("minicode.model_switcher.create_model_adapter", _failing_create_model_adapter)

    messages = run_agent_turn(
        model=NamedProviderUnavailableModel("deepseek-v4-pro[1m]"),
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        runtime={},
        on_runtime_event=runtime_events.append,
    )

    assert "provider availability failure" in messages[-1]["content"].lower()
    assert "not a local retry loop" in messages[-1]["content"].lower()
    assert "active channel:" in messages[-1]["content"].lower()
    assert "next step:" in messages[-1]["content"].lower()
    assert "deepseek-v4-pro[1m] failed" in messages[-1]["content"].lower()
    # Provider-specific fallback guidance varies by runtime; check for the
    # invariant substring instead of the exact provider-prefixed message.
    assert "fallbackmodels" in messages[-1]["content"].lower()
    assert created_models[0] == "claude-haiku-3-20240307"
    assert "deepseek-v4-pro[1m]" not in created_models
    assert len(created_models) == len(set(created_models))
    assert any(event.category == "recovery" for event in runtime_events)


def test_agent_turn_respects_runtime_anthropic_family_model_overrides(monkeypatch) -> None:
    registry = ToolRegistry([])
    created_models: list[str] = []

    def _failing_create_model_adapter(model: str, tools, runtime=None, force_mock: bool = False):
        created_models.append(model)
        return NamedProviderUnavailableModel(model)

    monkeypatch.delenv("ANTHROPIC_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="test-key"
            if model.startswith("claude") or model == "deepseek-v4-pro[1m]"
            else ""
        ),
    )
    monkeypatch.setattr("minicode.model_switcher.create_model_adapter", _failing_create_model_adapter)

    messages = run_agent_turn(
        model=NamedProviderUnavailableModel("deepseek-v4-pro[1m]"),
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        runtime={
            "anthropicDefaultSonnetModel": "deepseek-v4-pro[1m]",
            "anthropicDefaultOpusModel": "deepseek-v4-pro[1m]",
            "anthropicDefaultHaikuModel": "deepseek-v4-pro[1m]",
        },
    )

    assert "provider availability failure" in messages[-1]["content"].lower()
    assert created_models == []


def test_model_switcher_uses_snapshotted_anthropic_family_overrides_when_runtime_mutates(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime = {
        "model": "deepseek-v4-pro[1m]",
        "anthropicDefaultSonnetModel": "deepseek-v4-pro[1m]",
        "anthropicDefaultOpusModel": "deepseek-v4-pro[1m]",
        "anthropicDefaultHaikuModel": "deepseek-v4-pro[1m]",
    }

    monkeypatch.delenv("ANTHROPIC_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="test-key"
            if model.startswith("claude") or model == "deepseek-v4-pro[1m]"
            else ""
        ),
    )

    switcher = ModelSwitcher(
        current_model="deepseek-v4-pro[1m]",
        current_runtime=runtime,
        current_tools=registry,
    )
    switcher.record_runtime_failure("deepseek-v4-pro[1m]")

    runtime.pop("anthropicDefaultSonnetModel")
    runtime.pop("anthropicDefaultOpusModel")
    runtime.pop("anthropicDefaultHaikuModel")

    assert switcher._fallback_candidates() == []


def test_model_switcher_defaults_blank_anthropic_family_overrides_to_current_non_claude_model(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime = {"model": "deepseek-v4-pro[1m]"}

    monkeypatch.delenv("ANTHROPIC_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="test-key"
            if model.startswith("claude") or model == "deepseek-v4-pro[1m]"
            else ""
        ),
    )

    switcher = ModelSwitcher(
        current_model="deepseek-v4-pro[1m]",
        current_runtime=runtime,
        current_tools=registry,
    )
    switcher.record_runtime_failure("deepseek-v4-pro[1m]")

    assert switcher._fallback_candidates() == []


def test_agent_turn_infers_active_runtime_model_when_adapter_has_no_model_id(monkeypatch) -> None:
    registry = ToolRegistry([])
    created_models: list[str] = []

    def _failing_create_model_adapter(model: str, tools, runtime=None, force_mock: bool = False):
        created_models.append(model)
        return NamedProviderUnavailableModel(model)

    monkeypatch.delenv("ANTHROPIC_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="test-key"
            if model.startswith("claude") or model == "deepseek-v4-pro[1m]"
            else ""
        ),
    )
    monkeypatch.setattr("minicode.model_switcher.create_model_adapter", _failing_create_model_adapter)

    messages = run_agent_turn(
        model=UnnamedProviderUnavailableModel("deepseek-v4-pro[1m]"),
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        runtime={"model": "deepseek-v4-pro[1m]"},
    )

    assert "provider availability failure" in messages[-1]["content"].lower()
    assert "deepseek-v4-pro[1m] failed" in messages[-1]["content"].lower()
    assert created_models == []


def test_model_switcher_prefers_runtime_configured_fallback_models(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime = {
        "model": "claude-sonnet-4-20250514",
        "fallbackModels": ["gpt-4o"],
    }
    created_models: list[str] = []

    monkeypatch.delenv("MINI_CODE_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="test-key" if model == "gpt-4o" else ""
        ),
    )
    monkeypatch.setattr(
        "minicode.model_switcher.create_model_adapter",
        lambda model, tools, runtime=None, force_mock=False: created_models.append(model) or object(),
    )

    switcher = ModelSwitcher(
        current_model="claude-sonnet-4-20250514",
        current_runtime=runtime,
        current_tools=registry,
    )
    result = switcher.switch_to_fallback(reason="provider_outage")

    assert result.success is True
    assert result.new_model == "gpt-4o"
    assert switcher.current_model == "gpt-4o"
    assert created_models == ["gpt-4o"]


def test_model_switcher_does_not_pin_cross_provider_fallback_to_primary_provider() -> None:
    registry = ToolRegistry([])
    runtime = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "fallbackModels": ["gpt-4o"],
        "deepseekApiKey": "deepseek-key",
        "deepseekBaseUrl": "https://api.deepseek.com",
        "openaiApiKey": "openai-key",
        "openaiBaseUrl": "https://api.openai.com",
    }
    switcher = ModelSwitcher(
        current_model="deepseek-v4-pro",
        current_runtime=runtime,
        current_tools=registry,
    )

    result = switcher.switch_to("gpt-4o", reason="provider_outage")

    assert result.success is True
    assert result.old_provider == "deepseek"
    assert result.new_provider == "openai"
    assert runtime["configuredModel"] == "deepseek-v4-pro"
    assert runtime["model"] == "gpt-4o"


def test_agent_turn_uses_default_runtime_fallback_chain_without_explicit_configuration(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime_events: list[RuntimeEvent] = []
    seen: dict[str, str] = {}

    def _fake_create_model_adapter(model: str, tools, runtime=None, force_mock: bool = False):
        seen["model"] = model
        fallback_model = ScriptedModel([AgentStep(type="assistant", content="ok via default fallback")])
        fallback_model.model_id = model
        return fallback_model

    monkeypatch.delenv("MINI_CODE_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="test-key" if model in {"gpt-4o", "gpt-4o-mini", "deepseek-v4-pro[1m]"} else ""
        ),
    )
    monkeypatch.setattr("minicode.model_switcher.create_model_adapter", _fake_create_model_adapter)

    messages = run_agent_turn(
        model=ProviderUnavailableModel(),
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        runtime={
            "openaiApiKey": "openai-key",
            "openaiBaseUrl": "https://api.openai.com",
        },
        on_runtime_event=runtime_events.append,
    )

    assert messages[-1] == {"role": "assistant", "content": "ok via default fallback"}
    assert seen["model"] == "gpt-4o"
    assert any(event.category == "recovery" and "gpt-4o" in event.message for event in runtime_events)


def test_model_switcher_bounds_custom_openai_host_fallbacks(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime = {
        "model": "gpt5.5",
        "openaiApiKey": "openai-key",
        "openaiBaseUrl": "https://www.cctq.ai",
        "_openaiExposedModels": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    }

    monkeypatch.delenv("MINI_CODE_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="openai-key",
            base_url="https://www.cctq.ai",
        ),
    )

    switcher = ModelSwitcher(
        current_model="gpt5.5",
        current_runtime=runtime,
        current_tools=registry,
    )
    switcher.record_runtime_failure("gpt5.5")

    assert switcher._fallback_candidates() == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


def test_model_switcher_probes_provider_exposed_models_on_custom_openai_host(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime = {
        "model": "gpt5.5",
        "openaiApiKey": "openai-key",
        "openaiBaseUrl": "https://www.cctq.ai",
    }
    probed: list[bool] = []

    monkeypatch.delenv("MINI_CODE_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="openai-key",
            base_url="https://www.cctq.ai",
        ),
    )

    def _fake_probe(current_runtime):
        probed.append(True)
        current_runtime["_openaiExposedModels"] = [
            "claude-sonnet-4-6",
            "gpt-image-1.5",
            "gpt-4o-audio-preview",
        ]
        return (
            "claude-sonnet-4-6",
            "gpt-image-1.5",
            "gpt-4o-audio-preview",
        )

    monkeypatch.setattr("minicode.model_switcher.probe_openai_exposed_models", _fake_probe)

    switcher = ModelSwitcher(
        current_model="gpt5.5",
        current_runtime=runtime,
        current_tools=registry,
    )
    switcher.record_runtime_failure("gpt5.5")

    assert switcher._fallback_candidates() == ["claude-sonnet-4-6"]
    assert probed == [True]


def test_agent_turn_provider_outage_guidance_prefers_provider_exposed_models_when_default_openai_failover_exists(monkeypatch) -> None:
    registry = ToolRegistry([])

    monkeypatch.delenv("MINI_CODE_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="openai-key",
            base_url="https://www.cctq.ai",
        ),
    )
    monkeypatch.setattr(
        "minicode.model_switcher.create_model_adapter",
        lambda model, tools, runtime=None, force_mock=False: NamedProviderUnavailableModel(model),
    )

    messages = run_agent_turn(
        model=NamedProviderUnavailableModel("gpt5.5"),
        tools=registry,
        messages=[{"role": "system", "content": "sys"}],
        cwd=".",
        runtime={
            "model": "gpt5.5",
            "openaiApiKey": "openai-key",
            "openaiBaseUrl": "https://www.cctq.ai",
            "_openaiExposedModels": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        },
    )

    final_message = messages[-1]["content"].lower()
    assert "provider availability failure" in final_message
    assert "active channel: openai via openaiapikey/openaibaseurl" in final_message
    assert "default failover is already available" in final_message
    assert "claude-sonnet-4-6, claude-haiku-4-5-20251001" in final_message
    assert "currently exposes:" in final_message
    assert "add fallbackmodels or openaifallbackmodels to enable model failover" not in final_message


def test_model_switcher_bounds_custom_openai_host_fallbacks_with_legacy_api_base_url(monkeypatch) -> None:
    registry = ToolRegistry([])
    runtime = {
        "model": "gpt5.5",
        "openaiApiKey": "openai-key",
        "openaiBaseUrl": "https://www.cctq.ai",
        "_openaiExposedModels": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    }

    monkeypatch.delenv("MINI_CODE_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("OPENAI_MODEL_FALLBACKS", raising=False)
    monkeypatch.setattr(
        "minicode.model_switcher.build_provider_config",
        lambda model, runtime=None: SimpleNamespace(
            api_key="openai-key",
            api_base_url="https://www.cctq.ai",
        ),
    )

    switcher = ModelSwitcher(
        current_model="gpt5.5",
        current_runtime=runtime,
        current_tools=registry,
    )
    switcher.record_runtime_failure("gpt5.5")

    assert switcher._fallback_candidates() == ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]


class ToolAwareResearchModel(ModelAdapter):
    def __init__(self, tools: ToolRegistry, sequence: list[str]) -> None:
        self.tools = tools
        self.sequence = sequence
        self.calls = 0

    def next(self, messages: list[ChatMessage], on_stream_chunk=None) -> AgentStep:
        self.calls += 1
        if all(self.tools.find(name) is None for name in ("web_search", "web_fetch")):
            return AgentStep(
                type="assistant",
                content="根据已经取得并标注日期的来源，现将结果整理如下。",
            )
        index = min(self.calls - 1, len(self.sequence) - 1)
        tool_name = self.sequence[index]
        tool_input = (
            {"query": "C罗 近况"}
            if tool_name == "web_search"
            else {"url": f"https://example.com/source-{self.calls}"}
        )
        return AgentStep(
            type="tool_calls",
            calls=[
                {
                    "id": f"network-{self.calls}",
                    "toolName": tool_name,
                    "input": tool_input,
                }
            ],
        )


def _research_tool(name: str, executions: list[str]) -> ToolDefinition:
    def run(_input_data: dict, _context) -> ToolResult:
        executions.append(name)
        if name == "web_search":
            return ToolResult(
                ok=True,
                output=(
                    "Search results for: C罗 近况\n"
                    "PROVIDER: Google News RSS\n"
                    "RELEVANCE_FILTER: strong\n"
                    "URL: https://example.com/source\n"
                    "Published: Tue, 01 Sep 2026 08:00:00 GMT"
                ),
            )
        return ToolResult(
            ok=True,
            output="URL: https://example.com/source\nSTATUS: 200\nReuters dated report",
        )

    return ToolDefinition(
        name=name,
        description=f"fake {name}",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=run,
    )


def test_schedule_fetch_counts_only_matching_result_with_date_or_time() -> None:
    candidate_urls = {"https://www.formula1.com/en/racing/2026"}
    request = "下一次最近的一场 F1 比赛是什么时候"

    assert _fetch_has_lookup_evidence(
        request,
        "URL: https://www.formula1.com/en/racing/2026\nSTATUS: 200\nF1 news and videos",
        candidate_urls,
    ) is False
    assert _fetch_has_lookup_evidence(
        request,
        "URL: https://www.formula1.com/en/racing/2026\nSTATUS: 200\nItaly 09-06 21:00",
        candidate_urls,
    ) is True
    assert _fetch_has_lookup_evidence(
        request,
        "URL: https://www.formula1.com/en/racing/2026\nSTATUS: 200\nItaly 04 - 06 Sep",
        candidate_urls,
    ) is True
    assert _fetch_has_lookup_evidence(
        request,
        "URL: https://example.com/unrelated\nSTATUS: 200\nItaly 09-06 21:00",
        candidate_urls,
    ) is False


def test_agent_turn_stops_current_lookup_after_one_strong_search_and_fetch() -> None:
    executions: list[str] = []
    registry = ToolRegistry(
        [
            _research_tool("web_search", executions),
            _research_tool("web_fetch", executions),
        ]
    )
    model = ToolAwareResearchModel(registry, ["web_search", "web_fetch", "web_fetch"])
    runtime_events: list[RuntimeEvent] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "再给我查查C罗的近况"},
        ],
        cwd=".",
        runtime={"runtimeProfile": "single-deep"},
        enable_work_chain=False,
        on_runtime_event=runtime_events.append,
    )

    assert executions == ["web_search", "web_fetch"]
    assert model.calls == 3
    assert model.tools is registry
    assert messages[-1]["role"] == "assistant"
    assert any(
        event.category == "guard" and "足够的相关网页证据" in event.message
        for event in runtime_events
    )


def test_agent_turn_stops_after_one_cross_source_dated_news_search() -> None:
    executions: list[str] = []

    def run_search(_input_data: dict, _context) -> ToolResult:
        executions.append("web_search")
        return ToolResult(
            ok=True,
            output=(
                "Search results for: C罗 近况\n"
                "PROVIDER: Google News RSS\n"
                "RELEVANCE_FILTER: strong\n"
                "Source: Reuters\nPublished: Tue, 01 Sep 2026 08:00:00 GMT\n"
                "Source: ESPN\nPublished: Mon, 31 Aug 2026 08:00:00 GMT\n"
                "Source: USA Today\nPublished: Sun, 30 Aug 2026 08:00:00 GMT\n"
                "EVIDENCE_READY: cross-source dated news results"
            ),
        )

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="web_search",
                description="fake rich news search",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=run_search,
            )
        ]
    )
    model = ToolAwareResearchModel(registry, ["web_search"])
    runtime_events: list[RuntimeEvent] = []
    progress_messages: list[str] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "C罗近况"},
        ],
        cwd=".",
        runtime={"runtimeProfile": "single-deep"},
        enable_work_chain=False,
        on_runtime_event=runtime_events.append,
        on_progress_message=progress_messages.append,
    )

    assert executions == ["web_search"]
    assert model.calls == 2
    assert messages[-1]["role"] == "assistant"
    assert any(
        event.category == "guard" and "足够的相关网页证据" in event.message
        for event in runtime_events
    )
    assert not any("足够的相关网页证据" in message for message in progress_messages)


def test_agent_turn_enforces_network_limit_when_model_keeps_searching() -> None:
    executions: list[str] = []
    registry = ToolRegistry([_research_tool("web_search", executions)])
    model = ToolAwareResearchModel(registry, ["web_search"])
    runtime_events: list[RuntimeEvent] = []

    messages = run_agent_turn(
        model=model,
        tools=registry,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "查一些资料"},
        ],
        cwd=".",
        runtime={"networkToolLimit": 2},
        enable_work_chain=False,
        on_runtime_event=runtime_events.append,
    )

    assert executions == ["web_search", "web_search"]
    assert model.calls == 3
    assert model.tools is registry
    assert messages[-1]["role"] == "assistant"
    assert any(
        event.category == "guard" and "上限（2 次）" in event.message
        for event in runtime_events
    )
