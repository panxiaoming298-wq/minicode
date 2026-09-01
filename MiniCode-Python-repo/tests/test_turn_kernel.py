from minicode.task_object import TaskState
from minicode.turn_kernel import (
    TurnBudgetSignals,
    TurnRecurrentState,
    TurnVerificationState,
    build_stable_task_pack,
    decide_assistant_turn,
    decide_tool_turn,
    derive_turn_step_policy,
)


class DummyTask:
    title = "Repair reader"
    goal = "Keep durable state stable"
    description = "Refactor the turn kernel"


class DummySlotState:
    value = "running"


class DummySlot:
    state = DummySlotState()


class DummyTaskGraph:
    slots = {"turn:task-1": DummySlot()}

    def get_progress_percentage(self) -> float:
        return 35.0


def test_turn_recurrent_state_maps_await_user_to_paused() -> None:
    turn_state = TurnRecurrentState(max_steps=5)
    turn_state.set_stop_reason("await_user")

    assert turn_state.final_task_state() is TaskState.PAUSED


def test_build_stable_task_pack_includes_graph_and_protected_context() -> None:
    pack = build_stable_task_pack(
        task=DummyTask(),
        task_metadata={"intent_type": "code", "action_type": "update"},
        protected_context=["user asked for durable state retention"],
        task_graph=DummyTaskGraph(),
        task_slot_key="turn:task-1",
        latest_tool_result_summary="read_file: loaded turn kernel",
        progress_state={"summary": "patched the assistant decision path"},
        verification_state=TurnVerificationState(
            strict=True,
            requires_explicit_final=True,
            last_verification_note="need explicit final answer",
        ),
        budget_signals=TurnBudgetSignals(
            remaining_steps=7,
            tool_error_count=1,
            saw_tool_result=True,
        ),
    )

    assert pack is not None
    text = pack.to_protected_text()
    assert "Task graph: progress=35%" in text
    assert "slot=running" in text
    assert "Latest tool result: read_file: loaded turn kernel" in text
    assert "Protected context:" in text


def test_derive_turn_step_policy_becomes_verification_heavy_late_in_single_deep() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        widen_after_step=6,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 6
    turn_state.saw_tool_result = True
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.phase == "verify"
    assert turn_state.verification_state.requires_explicit_final is True
    assert "phase=verify" in turn_state.verification_state.last_verification_note


def test_derive_turn_step_policy_allows_widening_after_stall_threshold() -> None:
    turn_state = TurnRecurrentState(
        max_steps=10,
        profile_name="single-deep",
        widen_after_step=4,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 5
    turn_state.tool_error_count = 2
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.allow_widening is True
    assert "widening=ready" in turn_state.verification_state.last_verification_note


def test_derive_turn_step_policy_requires_explicit_signal_before_widening() -> None:
    turn_state = TurnRecurrentState(
        max_steps=10,
        profile_name="single-deep",
        widen_after_step=4,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 5
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.allow_widening is False
    assert policy.widening_reason == ""


def test_derive_turn_step_policy_records_model_stall_as_widening_reason() -> None:
    turn_state = TurnRecurrentState(
        max_steps=10,
        profile_name="single-deep",
        widen_after_step=4,
        empty_response_retry_limit=3,
        verification_state=TurnVerificationState(strict=True),
    )
    turn_state.step = 5
    turn_state.empty_response_retry_count = 3
    turn_state._refresh_budget_signals()

    policy = derive_turn_step_policy(turn_state)

    assert policy.allow_widening is True
    assert "stalled repeatedly" in policy.widening_reason
    assert "assistant returned repeated empty responses" in policy.widening_evidence_summary


def test_turn_recurrent_state_widening_transition_extends_budget_once() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        widen_after_step=4,
    )
    turn_state.step = 6
    turn_state._refresh_budget_signals()

    first = turn_state.activate_widening(extra_steps=5)
    second = turn_state.activate_widening(extra_steps=5)

    assert first is True
    assert second is False
    assert turn_state.widening_active is True
    assert turn_state.widening_transition_count == 1
    assert turn_state.max_steps == 13


def test_decide_assistant_turn_returns_verification_failed_in_late_verify_mode() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        verification_state=TurnVerificationState(
            strict=True,
            requires_explicit_final=True,
        ),
    )
    turn_state.step = 3
    turn_state._refresh_budget_signals()
    turn_state.saw_tool_result = True
    turn_state.empty_response_retry_count = turn_state.empty_response_retry_limit

    decision = decide_assistant_turn(
        turn_state=turn_state,
        step_content="",
        step_kind=None,
        stop_reason=None,
        block_types=None,
        ignored_block_types=None,
        is_empty=True,
        treat_as_progress=False,
        is_recoverable_thinking_stop=False,
        format_diagnostics=lambda *_: "",
        nudge_continue="continue",
        nudge_after_tool_result="after tool",
        resume_after_pause="resume pause",
        resume_after_max_tokens="resume tokens",
        nudge_after_empty_response="empty after tool",
        nudge_after_empty_no_tools="empty no tools",
        step_policy=derive_turn_step_policy(turn_state),
    )

    assert decision.kind == "fallback"
    assert decision.stop_reason == "verification_failed"
    assert "verification failure" in (decision.assistant_content or "").lower()


def test_decide_assistant_turn_rejects_unsupported_final_in_verify_mode() -> None:
    turn_state = TurnRecurrentState(
        max_steps=8,
        profile_name="single-deep",
        verification_state=TurnVerificationState(
            strict=True,
            requires_explicit_final=True,
        ),
    )
    turn_state.step = 4
    turn_state.record_tool_result(True, summary="pytest: 5 passed")

    decision = decide_assistant_turn(
        turn_state=turn_state,
        step_content="Done, the fix is complete.",
        step_kind=None,
        stop_reason=None,
        block_types=None,
        ignored_block_types=None,
        is_empty=False,
        treat_as_progress=False,
        is_recoverable_thinking_stop=False,
        format_diagnostics=lambda *_: "",
        nudge_continue="continue",
        nudge_after_tool_result="after tool",
        resume_after_pause="resume pause",
        resume_after_max_tokens="resume tokens",
        nudge_after_empty_response="empty after tool",
        nudge_after_empty_no_tools="empty no tools",
        step_policy=derive_turn_step_policy(turn_state),
    )

    assert decision.kind == "progress"
    assert "verification guard" in (decision.assistant_content or "").lower()
    assert "pytest: 5 passed" in (decision.user_content or "")


def test_decide_tool_turn_keeps_await_user_typed() -> None:
    decision = decide_tool_turn(
        tool_name="ask_user",
        result_output="Need approval",
        await_user=True,
    )

    assert decision.kind == "await_user"
    assert decision.stop_reason == "await_user"
