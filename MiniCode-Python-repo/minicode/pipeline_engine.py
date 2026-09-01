"""Pipeline Engine - Task execution orchestration.

Deepened work chain:
  Raw Input -> Intent Parser -> Task Object -> Pipeline -> Execution -> Result

The Pipeline Engine:
- Breaks a TaskObject into executable Steps
- Orchestrates step execution with dependency resolution
- Handles constraints, retries, fallbacks
- Produces a structured Result
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from minicode.task_object import TaskObject, TaskState, ConstraintType
from minicode.cybernetic_supervisor import CyberneticSupervisor
from minicode.decision_audit import get_auditor, DecisionType, DecisionOutcome
from minicode.logging_config import get_logger
from minicode.progress_controller import ProgressController, ProgressSignal
from minicode.verification_controller import VerificationController

logger = get_logger("pipeline_engine")


class StepType(str, Enum):
    ANALYZE = "analyze"
    PLAN = "plan"
    READ = "read"
    GENERATE = "generate"
    MODIFY = "modify"
    VERIFY = "verify"
    REVIEW = "review"
    DOCUMENT = "document"
    COMMIT = "commit"
    NOTIFY = "notify"


class StepState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


@dataclass
class Step:
    """A single executable step in a pipeline."""
    id: str
    type: StepType
    description: str
    handler: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    max_retries: int = 1
    timeout_seconds: float = 60.0
    state: StepState = StepState.PENDING
    result: Any = None
    error: str = ""
    execution_time_ms: float = 0.0
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type.value, "description": self.description,
            "handler": self.handler, "params": self.params,
            "depends_on": self.depends_on, "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "state": self.state.value, "result": self.result,
            "error": self.error, "execution_time_ms": self.execution_time_ms,
            "retry_count": self.retry_count,
        }


@dataclass
class PipelinePlan:
    """Execution plan for a TaskObject."""
    id: str
    task_id: str
    steps: list[Step] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "task_id": self.task_id,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
        }

    def get_step(self, step_id: str) -> Step | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def get_ready_steps(self) -> list[Step]:
        completed = {s.id for s in self.steps if s.state in (StepState.COMPLETED, StepState.SKIPPED)}
        return [s for s in self.steps
                if s.state == StepState.PENDING
                and all(d in completed for d in s.depends_on)]

    def is_complete(self) -> bool:
        return all(s.state in (StepState.COMPLETED, StepState.SKIPPED, StepState.FAILED)
                   for s in self.steps)

    def has_failures(self) -> bool:
        return any(s.state == StepState.FAILED for s in self.steps)


@dataclass
class PipelineResult:
    """Result of pipeline execution."""
    task_id: str
    plan_id: str
    success: bool
    completed_steps: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    total_time_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "plan_id": self.plan_id,
            "success": self.success, "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps, "outputs": self.outputs,
            "summary": self.summary, "total_time_ms": self.total_time_ms,
            "error": self.error,
        }


class StepPlanner:
    """Plans execution steps from a TaskObject."""

    def plan(self, task: TaskObject) -> PipelinePlan:
        plan_id = f"plan-{task.id}"
        steps: list[Step] = []
        intent_type = task.parsed_intent.intent_type.value if task.parsed_intent else ""
        action_type = task.parsed_intent.action_type.value if task.parsed_intent else ""

        steps.append(Step(
            id="analyze", type=StepType.ANALYZE,
            description="Analyze task requirements and context",
            handler="analyze_task",
        ))

        if task.relevant_files:
            steps.append(Step(
                id="read_files", type=StepType.READ,
                description=f"Read relevant files: {', '.join(task.relevant_files[:3])}",
                handler="read_file", params={"paths": task.relevant_files},
                depends_on=["analyze"],
            ))

        steps.append(Step(
            id="plan", type=StepType.PLAN,
            description="Plan implementation approach",
            handler="plan_approach",
            depends_on=["analyze"] + (["read_files"] if task.relevant_files else []),
        ))

        if action_type == "create":
            steps.append(Step(
                id="generate", type=StepType.GENERATE,
                description="Generate new code/content",
                handler="generate_code",
                depends_on=["plan"],
            ))
        elif action_type in ("update", "delete"):
            steps.append(Step(
                id="modify", type=StepType.MODIFY,
                description="Modify existing code/files",
                handler="modify_code",
                depends_on=["plan"],
            ))
        elif action_type in ("read", "analyze"):
            steps.append(Step(
                id="analyze_deep", type=StepType.ANALYZE,
                description="Deep analysis of code/content",
                handler="deep_analyze",
                depends_on=["plan"],
            ))
        elif action_type == "execute":
            steps.append(Step(
                id="execute", type=StepType.VERIFY,
                description="Execute command/test",
                handler="execute_command",
                depends_on=["plan"],
            ))

        has_test = any(c.type == ConstraintType.TEST_REQUIRED for c in task.constraints)
        if has_test or intent_type in ("code", "test"):
            work_step = "generate" if action_type == "create" else "modify" if action_type in ("update", "delete") else "analyze_deep"
            steps.append(Step(
                id="verify", type=StepType.VERIFY,
                description="Verify correctness with tests",
                handler="run_tests",
                depends_on=[work_step],
            ))

        has_review = any(c.type == ConstraintType.REQUIRES_REVIEW for c in task.constraints)
        if has_review:
            work_step = "generate" if action_type == "create" else "modify"
            steps.append(Step(
                id="review", type=StepType.REVIEW,
                description="Review output quality",
                handler="review_output",
                depends_on=[work_step],
            ))

        if intent_type in ("code", "document"):
            work_step = "generate" if action_type == "create" else "modify" if action_type in ("update", "delete") else "analyze_deep"
            dep = [work_step]
            if any(s.id == "verify" for s in steps):
                dep.append("verify")
            steps.append(Step(
                id="document", type=StepType.DOCUMENT,
                description="Update documentation",
                handler="update_docs",
                depends_on=dep,
            ))

        final_deps = [s.id for s in steps if s.id not in ("analyze", "read_files")]
        steps.append(Step(
            id="notify", type=StepType.NOTIFY,
            description="Notify user of result",
            handler="notify_user",
            depends_on=final_deps[-1:] if final_deps else ["plan"],
        ))

        logger.debug("Planned %d steps for task %s", len(steps), task.id)
        return PipelinePlan(id=plan_id, task_id=task.id, steps=steps)


class StepExecutor:
    """Executes individual steps."""

    def __init__(self):
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._verification_controller = VerificationController()
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        self._handlers["analyze_task"] = self._handle_analyze
        self._handlers["read_file"] = self._handle_read
        self._handlers["plan_approach"] = self._handle_plan
        self._handlers["generate_code"] = self._handle_generate
        self._handlers["modify_code"] = self._handle_modify
        self._handlers["deep_analyze"] = self._handle_analyze
        self._handlers["execute_command"] = self._handle_execute
        self._handlers["run_tests"] = self._handle_verify
        self._handlers["review_output"] = self._handle_review
        self._handlers["update_docs"] = self._handle_document
        self._handlers["notify_user"] = self._handle_notify

    def register_handler(self, name: str, handler: Callable[..., Any]) -> None:
        self._handlers[name] = handler

    def execute(self, step: Step, task: TaskObject) -> tuple[bool, Any]:
        handler = self._handlers.get(step.handler)
        if not handler:
            return False, f"No handler registered for: {step.handler}"

        step.state = StepState.RUNNING
        start = time.time()

        try:
            result = handler(step, task)
            step.execution_time_ms = (time.time() - start) * 1000
            step.state = StepState.COMPLETED
            step.result = result
            return True, result
        except Exception as e:
            step.execution_time_ms = (time.time() - start) * 1000
            step.error = str(e)
            step.retry_count += 1

            if step.retry_count < step.max_retries:
                step.state = StepState.RETRYING
                logger.warning("Step %s failed, retrying (%d/%d): %s",
                               step.id, step.retry_count, step.max_retries, e)
                return self.execute(step, task)
            else:
                step.state = StepState.FAILED
                logger.error("Step %s failed after %d retries: %s",
                             step.id, step.retry_count, e)
                return False, str(e)

    def _handle_analyze(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"intent": task.parsed_intent.to_dict() if task.parsed_intent else {},
                "files": task.relevant_files, "constraints": [c.to_dict() for c in task.constraints]}

    def _handle_read(self, step: Step, task: TaskObject) -> dict[str, Any]:
        paths = step.params.get("paths", [])
        return {"files_read": len(paths), "paths": paths}

    def _handle_plan(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"approach": f"Plan for {task.title}", "steps_planned": len(task.expected_outputs)}

    def _handle_generate(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"generated": True, "output_type": "code", "files": task.relevant_files}

    def _handle_modify(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"modified": True, "files": task.relevant_files}

    def _handle_execute(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"executed": True, "task": task.title}

    def _handle_verify(self, step: Step, task: TaskObject) -> dict[str, Any]:
        plan = self._verification_controller.plan_for_task(task)
        return {
            "verified": plan.mode.value == "none",
            "tests_passed": None,
            "verification_plan": plan.to_dict(),
        }

    def _handle_review(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"reviewed": True, "issues_found": 0}

    def _handle_document(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"documented": True, "docs_updated": True}

    def _handle_notify(self, step: Step, task: TaskObject) -> dict[str, Any]:
        return {"notified": True, "result": task.result_summary}


class PipelineEngine:
    """Orchestrates full pipeline execution."""

    def __init__(self):
        self.planner = StepPlanner()
        self.executor = StepExecutor()
        self._audit = get_auditor()
        self._progress_controller = ProgressController()
        self._supervisor = CyberneticSupervisor()

    def run(self, task: TaskObject) -> PipelineResult:
        plan = self.planner.plan(task)
        return self.execute(task, plan)

    def execute(self, task: TaskObject, plan: PipelinePlan) -> PipelineResult:
        task.set_state(TaskState.RUNNING)
        start = time.time()

        self._audit.record(
            DecisionType.ROUTING,
            reasoning=f"Pipeline execution for task {task.id}",
            selected_option=plan.id,
            input_context={"task_id": task.id, "steps": len(plan.steps)},
        )

        try:
            while not plan.is_complete():
                ready = plan.get_ready_steps()
                if not ready:
                    break
                for step in ready:
                    success, result = self.executor.execute(step, task)
                    if not success:
                        break

            completed = [s.id for s in plan.steps if s.state == StepState.COMPLETED]
            failed = [s.id for s in plan.steps if s.state == StepState.FAILED]
            outputs = {s.id: s.result for s in plan.steps if s.result is not None}

            if plan.has_failures():
                task.set_state(TaskState.FAILED)
                task.error_message = failed[0] if failed else "Unknown failure"
            else:
                task.set_state(TaskState.COMPLETED)
                task.result_summary = f"Completed {len(completed)}/{len(plan.steps)} steps"

            total_time = (time.time() - start) * 1000
            success = not plan.has_failures()
            verify_result = outputs.get("verify") if isinstance(outputs.get("verify"), dict) else {}
            tests_passed = verify_result.get("tests_passed") if verify_result else None
            progress_decision = self._progress_controller.decide(ProgressSignal(
                total_steps=len(plan.steps),
                completed_steps=len(completed),
                failed_steps=len(failed),
                tool_calls=len(completed) + len(failed),
                tool_errors=len(failed),
                output_changed=bool(outputs),
                tests_passed=tests_passed,
                elapsed_seconds=total_time / 1000,
                max_steps=len(plan.steps),
            ))
            outputs["progress_control"] = progress_decision.to_dict()
            supervisor_snapshots = [
                self._supervisor.snapshot_from_decision("progress", outputs["progress_control"])
            ]
            if isinstance(verify_result, dict) and isinstance(verify_result.get("verification_plan"), dict):
                supervisor_snapshots.append(
                    self._supervisor.snapshot_from_decision(
                        "verification",
                        verify_result["verification_plan"],
                    )
                )
            outputs["cybernetic_supervisor"] = self._supervisor.report(supervisor_snapshots).to_dict()

            self._audit.complete_decision(
                DecisionOutcome.SUCCESS if success else DecisionOutcome.FAILURE,
                total_time,
                task.result_summary,
                task.error_message,
            )

            return PipelineResult(
                task_id=task.id, plan_id=plan.id, success=success,
                completed_steps=completed, failed_steps=failed,
                outputs=outputs, summary=task.result_summary, total_time_ms=total_time,
                error=task.error_message,
            )
        except Exception as e:
            task.set_state(TaskState.FAILED)
            task.error_message = str(e)
            total_time = (time.time() - start) * 1000
            return PipelineResult(
                task_id=task.id, plan_id=plan.id, success=False,
                error=str(e), total_time_ms=total_time,
            )


_engine: PipelineEngine | None = None


def get_pipeline_engine() -> PipelineEngine:
    global _engine
    if _engine is None:
        _engine = PipelineEngine()
    return _engine


def process_task(raw_input: str) -> tuple[TaskObject, PipelinePlan, PipelineResult]:
    from minicode.intent_parser import parse_intent
    from minicode.task_object import build_task

    intent = parse_intent(raw_input)
    task = build_task(intent, raw_input)
    engine = get_pipeline_engine()
    result = engine.run(task)
    return task, engine.planner.plan(task), result
