from minicode.intent_parser import ActionType, IntentType, ParsedIntent
from minicode.pipeline_engine import Step, StepExecutor, StepType
from minicode.task_object import ConstraintType, TaskObject
from minicode.verification_controller import (
    VerificationController,
    VerificationMode,
    VerificationRisk,
    VerificationSignal,
)


def _task(
    *,
    intent_type: IntentType = IntentType.CODE,
    action_type: ActionType = ActionType.UPDATE,
    files: list[str] | None = None,
) -> TaskObject:
    task = TaskObject(
        raw_input="update code",
        parsed_intent=ParsedIntent(
            raw_input="update code",
            intent_type=intent_type,
            action_type=action_type,
            confidence=1.0,
        ),
        relevant_files=files or [],
    )
    if intent_type in {IntentType.CODE, IntentType.DEBUG, IntentType.REFACTOR, IntentType.TEST}:
        task.add_constraint(ConstraintType.TEST_REQUIRED, reason="Code modification requires tests")
    return task


class TestVerificationController:
    def test_read_only_task_uses_no_verification(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(intent_type="question", action_type="read", changed_files=[])
        )
        assert plan.risk == VerificationRisk.LOW
        assert plan.mode == VerificationMode.NONE
        assert plan.commands == []

    def test_python_core_change_selects_targeted_tests(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(
                changed_files=["minicode/context_cybernetics.py"],
                intent_type="code",
                action_type="update",
                requires_tests=True,
            )
        )
        assert plan.risk in {VerificationRisk.HIGH, VerificationRisk.CRITICAL}
        assert plan.mode in {VerificationMode.TARGETED, VerificationMode.FULL}
        assert any(
            "test_context_cybernetics.py" in cmd or cmd == "pytest -q"
            for cmd in plan.commands
        )

    def test_previous_failure_escalates_to_full_verification(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(
                changed_files=["minicode/agent_loop.py", "tests/test_agent_loop.py"],
                intent_type="debug",
                action_type="update",
                requires_tests=True,
                previous_verification_failed=True,
                recent_failures=3,
            )
        )
        assert plan.risk == VerificationRisk.CRITICAL
        assert plan.mode == VerificationMode.FULL
        assert plan.commands == ["pytest -q"]

    def test_documentation_change_does_not_force_tests(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(
                changed_files=["docs/architecture.md"],
                intent_type="document",
                action_type="update",
            )
        )
        assert plan.mode == VerificationMode.NONE
        assert plan.should_run is False

    def test_feedback_signal_marks_failed_plan_for_escalation(self):
        controller = VerificationController()
        plan = controller.plan(
            VerificationSignal(changed_files=["minicode/model_registry.py"], intent_type="code")
        )
        feedback = controller.update_from_result(plan, passed=False)
        assert feedback.previous_verification_failed is True
        assert feedback.recent_failures == 1
        assert feedback.changed_files == plan.changed_files


class TestVerificationPipelineIntegration:
    def test_pipeline_verify_step_returns_risk_adaptive_plan(self):
        executor = StepExecutor()
        task = _task(files=["minicode/context_cybernetics.py"])
        step = Step(
            id="verify",
            type=StepType.VERIFY,
            description="Verify correctness with tests",
            handler="run_tests",
        )

        success, result = executor.execute(step, task)

        assert success is True
        assert result["tests_passed"] is None
        assert result["verification_plan"]["mode"] in {"targeted", "full"}
        assert any("pytest" in cmd for cmd in result["verification_plan"]["commands"])
