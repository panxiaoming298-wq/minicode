from __future__ import annotations

import sys
import threading

from minicode.tooling import ToolRegistry
from minicode.unified_runtime import run_runtime_turn


def test_standard_uses_unified_runtime_without_extra_planner_or_verifier_model_calls(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_agent_loop(**kwargs):
        calls.append(kwargs["runtime"]["_runtimeNodeId"])
        return [*kwargs["messages"], {"role": "assistant", "content": "standard-ok"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {"executionProfile": "standard", "runtimeV2": True, "model": "fake"}
    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "answer a simple question"},
        ],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    assert calls == ["root"]
    assert messages[-1]["content"] == "standard-ok"
    trace = runtime["lastRuntimeTrace"]
    assert trace["profile"]["requested_mode"] == "standard"
    assert trace["profile"]["selected_route"] == "standard-single"
    assert trace["profile"]["planner_budget"] == 0
    assert trace["profile"]["verifier_budget"] == 0
    assert trace["scheduler"]["peak_active_workers"] == 1
    assert trace["delivery"]["completed"] is True


def test_empty_runtime_mapping_keeps_identity_and_receives_trace(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "minicode.agent_loop.run_agent_turn",
        lambda **kwargs: [
            *kwargs["messages"],
            {"role": "assistant", "content": "default-standard"},
        ],
    )
    runtime = {}

    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "use defaults"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    assert messages[-1]["content"] == "default-standard"
    assert runtime["lastRuntimeTrace"]["profile"]["requested_mode"] == "standard"


def test_preexisting_parent_cancellation_prevents_standard_worker_start(
    tmp_path,
    monkeypatch,
) -> None:
    calls = 0

    def must_not_run(**kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("canceled Runtime must not start a Worker")

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", must_not_run)
    cancellation = threading.Event()
    cancellation.set()
    runtime = {
        "runtimeV2": True,
        "executionProfile": "standard",
        "_cancellationEvent": cancellation,
    }

    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "cancel before start"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    assert calls == 0
    assert messages[-1]["content"].startswith("Runtime completion gate rejected delivery")
    assert runtime["lastRuntimeTrace"]["scheduler"]["canceled"] is True


def test_runtime_v2_flag_off_is_a_temporary_legacy_fallback(tmp_path, monkeypatch) -> None:
    calls = 0

    def fake_agent_loop(**kwargs):
        nonlocal calls
        calls += 1
        return [{"role": "assistant", "content": "legacy"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {"runtimeV2": False}
    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "simple"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )
    assert calls == 1
    assert messages[-1]["content"] == "legacy"
    assert "lastRuntimeTrace" not in runtime


def test_configured_integrated_verifier_can_block_standard_delivery(tmp_path, monkeypatch) -> None:
    def fake_agent_loop(**kwargs):
        return [*kwargs["messages"], {"role": "assistant", "content": "unverified answer"}]

    monkeypatch.setattr("minicode.agent_loop.run_agent_turn", fake_agent_loop)
    runtime = {
        "executionProfile": "standard",
        "runtimeV2": True,
        "runtimeVerificationCommands": [
            {
                "name": "injected-integration-test",
                "kind": "integration_test",
                "command": [sys.executable, "-c", "raise SystemExit(9)"],
            }
        ],
    }
    messages = run_runtime_turn(
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "produce then verify"}],
        cwd=str(tmp_path),
        runtime=runtime,
    )

    assert messages[-1]["content"].startswith("Runtime completion gate rejected delivery")
    assert runtime["lastRuntimeTrace"]["delivery"]["completed"] is False
    records = runtime["lastRuntimeTrace"]["delivery"]["verification_records"]
    assert [record["verifier"] for record in records] == [
        "integrated-runtime-state",
        "injected-integration-test",
    ]
