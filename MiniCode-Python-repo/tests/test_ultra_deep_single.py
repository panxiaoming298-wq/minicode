from __future__ import annotations

from minicode.runtime_protocol import ToolEffect
from minicode.tooling import ToolRegistry
from minicode.tools.web_fetch import web_fetch_tool
from minicode.tools.web_search import web_search_tool
from minicode.turn_events import TurnEvent, TurnEventQueue
from minicode.unified_runtime import run_runtime_turn


def _emit_worker_terminal(callbacks, messages, content: str) -> None:
    callbacks.on_event(TurnEvent.assistant_message(step=1, content=content))
    callbacks.on_event(
        TurnEvent.completed(
            step=1,
            messages=[*messages, {"role": "assistant", "content": content}],
            stop_reason="done",
        )
    )


def test_ultra_simple_question_delivers_once_without_spawning_verifier(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_agent_loop(**kwargs):
        node_id = kwargs["runtime"]["_runtimeNodeId"]
        calls.append(node_id)
        assert kwargs["tools"].find("web_search") is web_search_tool
        assert kwargs["tools"].find("web_fetch") is web_fetch_tool
        content = "当前是 Ultra，但这个问题只需要一次深度回答。"
        _emit_worker_terminal(kwargs["callbacks"], kwargs["messages"], content)
        return [*kwargs["messages"], {"role": "assistant", "content": content}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    events = TurnEventQueue(include_stream_chunks=True, include_thinking_chunks=True)
    runtime = {
        "executionProfile": "ultra",
        "runtimeV2": True,
        "model": "fake",
        "_workerModelFactory": lambda _tools: object(),
    }

    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([web_search_tool, web_fetch_tool]),
        messages=[{"role": "user", "content": "你现在是什么思考强度"}],
        cwd=str(tmp_path),
        runtime=runtime,
        callbacks=events,
    )

    delivered: list[TurnEvent] = []
    events.drain(delivered.append)
    assert calls == ["deep-worker"]
    assert [node["node_id"] for node in runtime["lastRuntimeTrace"]["plan"]["nodes"]] == [
        "deep-worker"
    ]
    assert ToolEffect.EXTERNAL_SIDE_EFFECT.value in (
        runtime["lastRuntimeTrace"]["plan"]["nodes"][0]["allowed_effects"]
    )
    assert runtime["lastRuntimeTrace"]["profile"]["verifier_budget"] == 0
    assert runtime["lastRuntimeTrace"]["delivery"]["completed"] is True
    assert messages[-1]["content"] == "当前是 Ultra，但这个问题只需要一次深度回答。"
    assert [event.kind for event in delivered] == ["assistant", "done"]
    assert delivered[0].content == messages[-1]["content"]
    assert delivered[-1].messages[-1]["content"] == messages[-1]["content"]


def test_ultra_mutating_deep_task_verifies_original_request_without_final_conflict(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    verifier_prompt = ""

    def fake_agent_loop(**kwargs):
        nonlocal verifier_prompt
        node_id = kwargs["runtime"]["_runtimeNodeId"]
        calls.append(node_id)
        if node_id == "deep-worker":
            content = "修复完成，并已执行针对性验证。"
        elif node_id == "final-verifier":
            verifier_prompt = kwargs["messages"][-1]["content"]
            content = "<verification_passed>"
        else:
            raise AssertionError(node_id)
        _emit_worker_terminal(kwargs["callbacks"], kwargs["messages"], content)
        return [*kwargs["messages"], {"role": "assistant", "content": content}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    events = TurnEventQueue(include_stream_chunks=True, include_thinking_chunks=True)
    runtime = {
        "executionProfile": "ultra",
        "runtimeV2": True,
        "model": "fake",
        "_workerModelFactory": lambda _tools: object(),
    }

    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "修复项目中的并发 bug"}],
        cwd=str(tmp_path),
        runtime=runtime,
        callbacks=events,
    )

    delivered: list[TurnEvent] = []
    events.drain(delivered.append)
    assert calls == ["deep-worker", "final-verifier"]
    assert "original request:\n修复项目中的并发 bug" in verifier_prompt
    assert "Do not use a <final> wrapper" in verifier_prompt
    assert "Finish with a concise, evidence-backed <final> response" not in verifier_prompt
    assert runtime["lastRuntimeTrace"]["delivery"]["completed"] is True
    assert messages[-1]["content"] == "修复完成，并已执行针对性验证。"
    assert [event.kind for event in delivered] == ["assistant", "done"]
    assert delivered[0].content == messages[-1]["content"]
