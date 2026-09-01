from minicode.intent_parser import ActionType, IntentType, ParsedIntent
from minicode.pipeline_engine import get_pipeline_engine
from minicode.progress_controller import (
    ProgressAction,
    ProgressController,
    ProgressSignal,
)
from minicode.task_object import TaskObject


class TestProgressController:
    def test_healthy_progress_continues(self):
        decision = ProgressController().decide(
            ProgressSignal(total_steps=5, completed_steps=3, tool_calls=3, output_changed=True)
        )
        assert decision.action in {ProgressAction.CONTINUE, ProgressAction.VERIFY}
        assert decision.health_score > 0.5

    def test_output_change_requests_verification(self):
        decision = ProgressController().decide(
            ProgressSignal(total_steps=4, completed_steps=2, output_changed=True, tests_passed=None)
        )
        assert decision.action == ProgressAction.VERIFY

    def test_stalled_execution_requests_confirmation(self):
        decision = ProgressController().decide(
            ProgressSignal(
                total_steps=8,
                completed_steps=0,
                failed_steps=2,
                tool_calls=8,
                tool_errors=6,
                output_changed=False,
                max_steps=8,
            )
        )
        assert decision.action == ProgressAction.REQUEST_CONFIRMATION
        assert decision.stall_score >= 0.75

    def test_completed_and_verified_stops(self):
        decision = ProgressController().decide(
            ProgressSignal(total_steps=4, completed_steps=4, output_changed=True, tests_passed=True)
        )
        assert decision.action == ProgressAction.STOP

    def test_step_pressure_narrows_scope(self):
        decision = ProgressController().decide(
            ProgressSignal(total_steps=9, completed_steps=4, tool_calls=4, max_steps=10)
        )
        assert decision.action == ProgressAction.NARROW_SCOPE


class TestProgressPipelineIntegration:
    def test_pipeline_result_includes_progress_control(self):
        task = TaskObject(
            raw_input="explain code",
            parsed_intent=ParsedIntent(
                raw_input="explain code",
                intent_type=IntentType.EXPLAIN,
                action_type=ActionType.READ,
                confidence=1.0,
            ),
        )
        result = get_pipeline_engine().run(task)
        assert "progress_control" in result.outputs
        assert result.outputs["progress_control"]["action"] in {
            "continue", "verify", "stop", "narrow_scope", "switch_strategy", "request_confirmation",
        }

