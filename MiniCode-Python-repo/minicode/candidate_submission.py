"""Explicit primary handoff, not an acceptance decision or budget fallback.

The per-activation capability owns its receipt in memory. A model/tool string
cannot recreate it. Durable ownership remains the existing completed candidate
checkpoint and Reducer acceptance; interruption before that point is incomplete.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence

from minicode.runtime_protocol import ReplayPolicy, ToolEffect, new_id
from minicode.runtime_state import ReducerError
from minicode.tooling import ToolContext, ToolDefinition, ToolMetadata, ToolRegistry, ToolResult
from minicode.turn_events import AgentTurnOutcome, TurnEvent
from minicode.workspace_provider import WorkspaceConflict

SUBMIT_CANDIDATE = "submit_candidate"
SUBMIT_BUILDER_CANDIDATE = "submit_builder_candidate"
SUBMISSION_TOOLS = frozenset({SUBMIT_CANDIDATE, SUBMIT_BUILDER_CANDIDATE, "submit_consultation"})


class CandidateVerificationFailure(WorkspaceConflict):
    """Registered commands completed and rejected this candidate's contents."""


def _submission_input(value: dict, *, builder: bool = False) -> tuple[str, str | None]:
    allowed = {"summary"} if builder else {"summary", "answer"}
    if not isinstance(value, dict) or "summary" not in value or set(value) - allowed:
        raise ValueError("submission accepts summary and an optional primary answer; workspace identity is Runtime-owned")
    summary = value["summary"]
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 2000:
        raise ValueError("summary must contain 1 to 2000 characters")
    answer = None
    if "answer" in value:
        if not isinstance(value["answer"], str) or not value["answer"].strip():
            raise ValueError("answer must be a non-empty user-facing response")
        answer = value["answer"].strip()
    # Native handoff carries prose, not file observations. Preserve the
    # non-mapping control payload boundary used by the read-set recorder.
    return summary.strip(), answer


class CandidateSubmission:
    def __init__(self, authorize: Callable[[ToolContext], str], *, builder: bool = False,
                 direct: bool = False,
                 seal: Callable[[ToolContext], None] | None = None,
                 preflight: Callable[[ToolContext, str], None] | None = None) -> None:
        if builder and direct:
            raise ValueError("Builder handoff requires an isolated candidate")
        self._authorize = authorize
        self._seal = seal
        self._preflight = preflight
        self.builder = builder
        self.direct = direct
        self.name = SUBMIT_BUILDER_CANDIDATE if builder else SUBMIT_CANDIDATE
        self.stop_reason = "builder_submitted" if builder else "candidate_submitted"
        self._context: ToolContext | None = None
        self._revision = ""
        self._summary = ""
        self._input: tuple[str, str | None] | None = None
        self._receipt = ""
        self._consumed = False

    def tool(self) -> ToolDefinition:
        properties = {
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000,
                        "description": "Internal handoff: brief changes and caveats; never used as the user's answer"},
        }
        if not self.builder:
            properties["answer"] = {"type": "string", "minLength": 1,
                "description": "Complete user-facing final answer, with the detail requested. Provide this unless the same response already includes the complete <final> answer in its text channel. Report observed checks honestly; do not claim Runtime acceptance."}
        return ToolDefinition(
            name=self.name,
            description=(
                "Hand this Builder's current isolated candidate back to Runtime for patch export and parent review. "
                "Ends this Builder without another model reply; does NOT apply a patch or declare task success. "
                "Call once, LAST after final writes in the SAME batch (executed in order), or alone if already ready. "
                "Earlier errors/waits prevent handoff. The primary must accept the patch and Runtime must still "
                "check, independently verify and integrate the final candidate. Report unrun checks as unrun."
            ) if self.builder else (
                "Submit the current DIRECT workspace for the task's REGISTERED ACCEPTANCE CHECKS. "
                "Call once, LAST after final writes/readback in the SAME ordered batch, or alone when ready. "
                "Earlier errors/waits prevent submission. Failed checks return diagnostics for repair. "
                "A successful handoff ends this AgentLoop without another model reply; task success still requires "
                "Runtime's final checks on the submitted workspace version. Files have already been edited directly; "
                "this tool does not integrate or roll back changes. Report caveats and unrun checks honestly."
            ) if self.direct else (
                "Request the task's REGISTERED ACCEPTANCE CHECKS on this primary's current isolated candidate, "
                "then controlled integration if accepted. This is the native way to run those checks; "
                "their harness need not be present in the candidate directory. "
                "Preferred finish path for ready changes: no separate run_acceptance_checks or precheck pass is required. "
                "Ends this AgentLoop without another model reply; does NOT declare task success. "
                "Call once, LAST in a batch after final writes/checks (executed in order), or alone. "
                "Any earlier error/wait prevents submission. Required consultation must already be satisfied. "
                "Runtime reruns the task's original acceptance checks and returns failures for bounded repair."
            ),
            input_schema={"type": "object", "properties": properties,
                          "required": ["summary"], "additionalProperties": False},
            validator=lambda value: _submission_input(value, builder=self.builder), run=self._submit,
            metadata=ToolMetadata(name=self.name, description="Builder candidate handoff" if self.builder else "Primary candidate handoff",
                                  effect=ToolEffect.WORKSPACE_WRITE, replay_policy=ReplayPolicy.NEVER_AUTO_RETRY,
                                  candidate_control=True),
        )

    @property
    def consumed_revision(self) -> str:
        if not self._consumed:
            raise WorkspaceConflict("candidate receipt has not been consumed")
        return self._revision

    def _submit(self, value: tuple[str, str | None], context: ToolContext) -> ToolResult:
        if self._context is not None:
            return ToolResult(ok=False, output="Candidate submission already issued; replay is forbidden")
        try:
            revision = self._authorize(context)
            if self._preflight is not None:
                self._preflight(context, revision)
                if self._authorize(context) != revision:
                    raise WorkspaceConflict("candidate changed during submission checks")
            if self._seal is not None:
                self._seal(context)
        except CandidateVerificationFailure as error:
            return ToolResult(ok=False, output=f"CandidateSubmissionDenied: {error}", verificationFailed=True)
        except (ValueError, WorkspaceConflict, ReducerError) as error:
            return ToolResult(ok=False, output=f"CandidateSubmissionDenied: {error}")
        self._context, self._revision, self._summary = context, revision, value[0]
        self._input = value
        self._receipt = json.dumps({"status": self.stop_reason, "handoff_id": new_id("candidate"),
                                    "candidate_revision": revision, "delivered": False}, sort_keys=True)
        return ToolResult(ok=True, output=self._receipt, runtimeStopReason=self.stop_reason)

    def consume(self, outcome: AgentTurnOutcome) -> str:
        """Authenticate the terminal receipt and recheck current bytes/authority."""
        if (self._consumed or self._context is None or outcome.stop_reason != self.stop_reason
            or outcome.wait_kind is not None or outcome.timed_out or outcome.ambiguous_side_effect):
            raise WorkspaceConflict("candidate handoff has no valid per-activation submission")
        messages = outcome.messages
        receipts = [m for m in messages if m.get("role") == "tool_result"]
        calls = [m for m in messages if m.get("role") == "assistant_tool_call"]
        if (not receipts or not calls
            or any(m.get("toolName") != self.name or m.get("toolUseId") != self._context.tool_call_id
                   for m in (receipts[-1], calls[-1]))
            or receipts[-1].get("isError") is not False
            or not str(receipts[-1].get("content", "")).startswith(self._receipt)
            or _submission_input(calls[-1].get("input"), builder=self.builder) != self._input):
            raise WorkspaceConflict("candidate handoff does not match the issued terminal tool receipt")
        if self._authorize(self._context) != self._revision:
            raise WorkspaceConflict("candidate changed after explicit submission")
        self._consumed = True
        return self._summary


def execute_submission_batch(calls: Sequence[dict], tools: ToolRegistry,
                             execute: Callable[[dict], ToolResult], *,
                             submission_name: str = SUBMIT_CANDIDATE,
                             on_event: Callable[[TurnEvent], None] | None = None,
                             recover_input: Callable[[dict, str], str] | None = None,
                             step: int | None = None) -> list[tuple[dict, ToolResult]]:
    """Execute ordered prerequisites and one terminal handoff; receipt every call.

    No concurrent partitioning: a check must see preceding writes. Reject the
    entire malformed batch before effects. After an error, wait or yield, later
    calls are explicitly not executed, including the candidate handoff.
    """
    reason = ""
    rejection_reason = "invalid_batch"
    submit = tools.find(submission_name)
    if (submission_name not in SUBMISSION_TOOLS
        or not calls or submit is None or calls[-1].get("toolName") != submission_name
        or sum(c.get("toolName") in SUBMISSION_TOOLS for c in calls) != 1
        or len({c.get("id") for c in calls}) != len(calls)
        or any(not isinstance(c.get("id"), str) or not c["id"] for c in calls)):
        reason = f"{submission_name} must be available and occur exactly once, LAST, with unique call IDs"
    else:
        try:
            submit.validator(calls[-1].get("input"))
        except (ValueError, TypeError, KeyError) as error:
            reason = str(error)
            rejection_reason = "invalid_submission_input"
    results = []
    for call in calls:
        if reason:
            output = "NotExecuted: " + reason
            if (recover_input is not None and len(calls) == 1 and submission_name == "submit_consultation"
                and rejection_reason == "invalid_submission_input" and not call.get("inputError")):
                # A native diagnostic may retain an immutable rejected report.
                # It cannot change failure/stop semantics or run prerequisites.
                output = recover_input(call, output)
            result = ToolResult(ok=False, output=output, inputRejected=True)
            if on_event is not None:
                # Do not announce a tool start/result for a call never run.
                # A diagnostic consumer cannot change the rejection decision.
                try:
                    on_event(TurnEvent.tool_rejected(step=step, tool_name=str(call.get("toolName") or ""),
                        tool_input=call.get("input"), output=result.output,
                        tool_call_id=str(call.get("id") or ""), reason=rejection_reason))
                except Exception:
                    logging.getLogger(__name__).debug("Batch rejection observer failed", exc_info=True)
        else:
            result = execute(call)
            if (not result.ok or result.awaitUser or result.waitKind or result.waitReason or result.runtimeYield or result.runtimeStopReason
                or result.timedOut or result.ambiguousSideEffect or result.backgroundTask is not None):
                reason = "an earlier call failed, is unresolved, or ended this batch; inspect its receipt before resubmitting"
                rejection_reason = "prior_call_incomplete"
        results.append((call, result))
    return results
