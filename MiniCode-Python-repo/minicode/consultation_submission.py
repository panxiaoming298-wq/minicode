"""Per-activation read-only report handoff; never a task completion authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from minicode.consultation_report_draft import ConsultationReportDraft
from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore
from minicode.runtime_protocol import ReplayPolicy, ToolEffect, new_id
from minicode.runtime_state import ReducerError
from minicode.tooling import ToolContext, ToolDefinition, ToolMetadata, ToolResult
from minicode.turn_events import AgentTurnOutcome
from minicode.verifier_decision import terminal_marker
from minicode.workspace_provider import WorkspaceConflict


SUBMIT_CONSULTATION = "submit_consultation"
REPORT_LIMIT = 16_000
SUMMARY_BYTES_LIMIT = 2_000
PAGED_SUMMARY_BYTES_LIMIT = 16_000
PAGED_SUMMARY_POLICY = "consultation-paged-decision-v1"
DECISION_FIRST_POLICY = "decision-summary-optional-report-v2"
SUMMARY_MEDIA_TYPE = "text/vnd.minicode.consultation-summary; charset=utf-8"
VERIFIER_VERDICT_INSTRUCTION = (
    "Set verdict to passed only when every assigned criterion is supported, failed for a concrete unmet "
    "criterion, or unavailable when required evidence or checks cannot be obtained. "
    "Explain findings, failed checks and gaps in summary/report. These are ordinary prose: no terminal "
    "decision marker or XML tag is required. The verdict is the helper's judgment, not task acceptance. "
)
VERDICTS = ("passed", "failed", "unavailable")


def _report(value: dict, *, summary_limit: int = SUMMARY_BYTES_LIMIT) -> tuple[str, str | None]:
    if not isinstance(value, dict) or "report" not in value or not set(value) <= {"report", "summary"}:
        raise ValueError("submit_consultation accepts report and summary; identity and acceptance remain Runtime-owned")
    report = value["report"]
    if not isinstance(report, str) or not 1 <= len(report.strip()) <= REPORT_LIMIT:
        raise ValueError(f"report must contain 1 to {REPORT_LIMIT} characters")
    summary = None
    if "summary" in value:
        summary = value["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a nonempty, self-contained decision summary")
        summary = summary.strip()
        summary_bytes = len(summary.encode("utf-8"))
        if summary_bytes > summary_limit:
            raise ValueError(f"summary must fit {summary_limit} UTF-8 bytes (received {summary_bytes}, "
                f"exceeds by {summary_bytes - summary_limit}); bytes are not characters. "
                "Keep source locations, decisions, key risks and failed checks; details belong in report")
    # Old report-only tool histories remain valid; the current model-facing
    # schema requests both fields. Never derive a decision by slicing a report.
    return report.strip(), summary


class ConsultationSubmission:
    def __init__(self, authorize: Callable[[ToolContext], str], artifacts: RuntimeArtifactStore | None = None,
                 *, paged_summaries: bool = False, decision_first: bool = False,
                 verifier: bool = False) -> None:
        if type(paged_summaries) is not bool or (paged_summaries and artifacts is None):
            raise ValueError("paged summaries require an artifact store and explicit boolean policy")
        self.paged_summaries = paged_summaries
        if type(decision_first) is not bool or (decision_first and not paged_summaries):
            raise ValueError("decision-first handoff requires the native paged-summary contract")
        self.decision_first = decision_first
        self._verifier = verifier
        self._authorize = authorize
        self._context: ToolContext | None = None
        self._revision = ""
        self._report = ""
        self._summary: str | None = None
        self._receipt = ""
        self._consumed = False
        self._submitted = None
        self._draft = ConsultationReportDraft(artifacts, authorize) if artifacts is not None else None

    def _validate_decision(self, text: str | None, field: str) -> None:
        # Syntax only: Runtime still judges the result. Never infer or append
        # a pass, and never turn an explicit failed verdict into a retry.
        if self._verifier and text is not None and terminal_marker(text) is None:
            raise ValueError(f"{field}: provide the separate verdict field (passed, failed or unavailable). "
                "Only legacy calls without verdict require a decision marker as the final non-empty line. "
                "No report was submitted. Correct the submitted text using the existing evidence; "
                  "do not repeat reads solely to fix this format error.")

    def _validate(self, value: dict) -> tuple[str, str, str | None, str | None]:
        verdict = None
        if self._verifier and isinstance(value, dict) and "verdict" in value:
            verdict = value["verdict"]
            if not isinstance(verdict, str) or verdict not in VERDICTS:
                raise ValueError("verdict: expected passed, failed or unavailable; no report was submitted")
            value = {key: item for key, item in value.items() if key != "verdict"}
        # Old in-flight calls/checkpoints retain their exact marker protocol.
        # A new explicit verdict never comes from inferring or rewriting prose.
        def validate_decision(text, field):
            if verdict is None:
                self._validate_decision(text, field)

        if self.decision_first and isinstance(value, dict) and set(value) == {"summary"}:
            # The model explicitly authors one complete document for both
            # roles. No generated/sliced summary or empty report substitution.
            report, summary = _report({"report": value["summary"], "summary": value["summary"]},
                summary_limit=PAGED_SUMMARY_BYTES_LIMIT)
            validate_decision(summary, "summary")
            return "report", report, summary, verdict
        if self.paged_summaries and (not isinstance(value, dict) or set(value) != {"report", "summary"}):
            raise ValueError("paged consultation requires explicit complete report and summary in this activation")
        if isinstance(value, dict) and set(value) == {"draft_id", "summary"}:
            draft_id = value["draft_id"]
            if self._draft is None or not isinstance(draft_id, str) or not 1 <= len(draft_id) <= 100:
                raise ValueError("draft_id must identify this activation's retained report")
            _, summary = _report({"report": "retained report", "summary": value["summary"]})
            validate_decision(summary, "summary")
            return "draft", draft_id, summary, verdict
        # Return independent format problems together. A byte overflow must not
        # hide a missing verdict and force the helper to resend a long report
        # just to discover the next problem. Never repair or infer a decision.
        errors = []
        try:
            report, summary = _report(value, summary_limit=PAGED_SUMMARY_BYTES_LIMIT if self.paged_summaries else SUMMARY_BYTES_LIMIT)
        except ValueError as error:
            errors.append(str(error))
        if isinstance(value, dict):
            for field in ("report", "summary"):
                text = value.get(field)
                if isinstance(text, str) and text.strip():
                    try:
                        validate_decision(text.strip(), field)
                    except ValueError as error:
                        errors.append(str(error))
        if errors:
            raise ValueError("; ".join(errors))
        return "report", report, summary, verdict

    def recover_rejection(self, call: dict, output: str, context: ToolContext) -> str:
        """Called only after standalone native input validation failed, before effects.

        No submission is issued. Malformed JSON, invalid reports/fields, missing
        or non-string summaries and oversized staging payloads are not repaired.
        """
        if (self.paged_summaries or self._draft is None or self._context is not None or call.get("inputError")
            or call.get("toolName") != SUBMIT_CONSULTATION or call.get("id") != context.tool_call_id
            or not isinstance(call.get("input"), dict)
            or set(call["input"]) not in ({"report", "summary"}, {"report", "summary", "verdict"})):
            return output
        value = call["input"]
        try:
            _report({"report": value["report"]})
            if "verdict" in value:
                if not self._verifier or value["verdict"] not in VERDICTS:
                    return output
            else:
                self._validate_decision(value["report"], "report")
            summary = value["summary"]
            if (not isinstance(summary, str) or not 1 <= len(summary.strip()) <= REPORT_LIMIT
                or len(summary.strip().encode("utf-8")) <= SUMMARY_BYTES_LIMIT):
                return output
            return self._draft.retain(call, output, context)
        except (ValueError, TypeError, OSError, ArtifactIntegrityError, WorkspaceConflict, ReducerError):
            return output  # No handle/view unless persistence and authority both succeed.

    def prepare_recovery_view(self, body, *, endpoint, contents):
        if self._draft is None or self._context is not None:
            return dict(body), None
        try:
            return self._draft.prepare(body, endpoint=endpoint, contents=contents)
        except (ValueError, TypeError, OSError, ArtifactIntegrityError, WorkspaceConflict, ReducerError):
            return dict(body), None

    def tool(self) -> ToolDefinition:
        decision_instruction = VERIFIER_VERDICT_INSTRUCTION if self._verifier else ""
        summary_instruction = (
            "Aim for 2000 UTF-8 bytes for inline delivery. If all critical findings need more space, "
            "submit the complete summary (up to 16000 UTF-8 bytes): Runtime stores it losslessly and "
            "requires the parent to read every page before writing or completing. Do not discard risks to fit the inline view. "
            if self.paged_summaries else
            "At most 2000 UTF-8 bytes, NOT 2000 characters. For mostly Chinese text, aim for "
            "at most 600 characters including source locations. "
            "This is the parent's default handoff. "
        )
        return ToolDefinition(
            name=SUBMIT_CONSULTATION,
            description=(
                "Return your bounded read-only findings to Runtime and end this helper without another model reply. "
                "Use summary for a self-contained decision: exact source-relative locations, recommended change, "
                "critical boundary cases, failed checks and unresolved risks. "
                + decision_instruction + summary_instruction + ("Submit summary alone when it contains the complete findings; Runtime stores that exact text as both report and decision. "
                "Add report only for distinct supporting detail; do not repeat summary or omit critical caveats. "
                if self.decision_first else "Put supporting detail in report; do not omit a decision-critical caveat from summary. A concise report may equal summary. ")
                + "Call once, alone or LAST after reads/checks; an earlier error or wait prevents handoff. "
                "No extra tool calls are needed once your findings are supported. "
                "This does not certify acceptance or finish the parent task; Runtime verifies the report."
            ),
            input_schema={"type": "object", "properties": {
                "report": {"type": "string", "minLength": 1, "maxLength": REPORT_LIMIT,
                    "description": decision_instruction + "Complete supporting findings with source locations and uncertainties."},
                "summary": {"type": "string", "minLength": 1,
                    "maxLength": PAGED_SUMMARY_BYTES_LIMIT if self.paged_summaries else SUMMARY_BYTES_LIMIT,
                    "description": decision_instruction + summary_instruction + "Preserve critical findings and uncertainties; supporting detail goes in report."},
                **({"verdict": {"type": "string", "enum": list(VERDICTS),
                                "description": VERIFIER_VERDICT_INSTRUCTION}} if self._verifier else {}),
            }, "required": (["summary"] if self.decision_first else ["report", "summary"])
                + (["verdict"] if self._verifier else []), "additionalProperties": False},
            validator=self._validate, run=self._submit,
            metadata=ToolMetadata(name=SUBMIT_CONSULTATION, description="Read-only consultation handoff",
                                  effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.NEVER_AUTO_RETRY),
        )

    def _submit(self, submitted: tuple[str, str, str | None, str | None], context: ToolContext) -> ToolResult:
        if self._context is not None:
            return ToolResult(ok=False, output="Consultation submission already issued; replay is forbidden")
        try:
            revision = self._authorize(context)
            kind, value, summary, verdict = submitted
            report = self._draft.resolve(value, submitting_call_id=context.tool_call_id) if kind == "draft" else value
            if kind == "draft" and self._draft.verdict(value) != verdict:
                raise ValueError("verdict: preserve the retained report's verdict when correcting its summary")
        except (ValueError, OSError, ArtifactIntegrityError, WorkspaceConflict, ReducerError) as error:
            return ToolResult(ok=False, output=f"ConsultationSubmissionDenied: {error}")
        self._submitted = submitted
        self._context, self._revision, self._report, self._summary = context, revision, report, summary
        self._receipt = json.dumps({"status": "consultation_submitted", "handoff_id": new_id("consultation"),
            "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(), "delivered": False,
            **({"verdict": verdict} if verdict is not None else {}),
            **({"summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest()} if summary is not None else {})}, sort_keys=True)
        return ToolResult(ok=True, output=self._receipt, runtimeStopReason="consultation_submitted")

    def consume(self, outcome: AgentTurnOutcome) -> str:
        if (self._consumed or self._context is None or outcome.stop_reason != "consultation_submitted"
            or outcome.wait_kind is not None or outcome.timed_out or outcome.ambiguous_side_effect):
            raise WorkspaceConflict("consultation handoff has no valid per-activation submission")
        calls = [m for m in outcome.messages if m.get("role") == "assistant_tool_call"]
        receipts = [m for m in outcome.messages if m.get("role") == "tool_result"]
        if (not calls or not receipts
            or sum(m.get("toolUseId") == self._context.tool_call_id for m in calls) != 1
            or sum(m.get("toolUseId") == self._context.tool_call_id for m in receipts) != 1
            or any(m.get("toolName") != SUBMIT_CONSULTATION or m.get("toolUseId") != self._context.tool_call_id
                   for m in (calls[-1], receipts[-1]))
            or receipts[-1].get("isError") is not False
            or not str(receipts[-1].get("content", "")).startswith(self._receipt)
            or self._validate(calls[-1].get("input")) != self._submitted):
            raise WorkspaceConflict("consultation handoff does not match its issued terminal receipt")
        if self._authorize(self._context) != self._revision:
            raise WorkspaceConflict("consultation source changed after report submission")
        if self._submitted[0] == "draft" and self._draft.resolve(self._submitted[1]) != self._report:
            raise WorkspaceConflict("consultation report draft changed after submission")
        self._consumed = True
        return self._report

    @property
    def decision_summary(self) -> str | None:
        """Available only after both fields match a live, consumed receipt."""
        if not self._consumed:
            raise WorkspaceConflict("decision summary has no consumed consultation submission")
        return self._summary

    @property
    def verdict(self) -> str | None:
        """Only a consumed, identity-bound submission can provide a verdict."""
        if not self._consumed:
            raise WorkspaceConflict("verdict has no consumed consultation submission")
        return self._submitted[3]
