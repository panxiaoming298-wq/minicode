"""Revision-bound requests for registered checks from a read-only verifier.

The model supplies no command, path, coverage or permission. Each activation
executes at most once; repeated requests return the retained native receipt.
"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from weakref import WeakKeyDictionary

from minicode.candidate_checks import _NATIVE_CHECK_HANDLERS
from minicode.registered_checks import observe_candidate_checks
from minicode.runtime_protocol import AgentResultStatus, ExecutionFailure, ExecutionFailureKind, ReplayPolicy, ToolEffect
from minicode.tooling import ToolDefinition, ToolMetadata, ToolResult


REQUEST_VERIFICATION = "request_verification"
_NATIVE_REQUESTS = WeakKeyDictionary()


def native_verification_request(tool):
    """Identify a live native handler, never a same-named extension."""
    try:
        return _NATIVE_REQUESTS.get(getattr(tool, "run", None))
    except TypeError:
        return None


class VerificationRequest:
    def __init__(self, *, authorize, workspace, runtime, reducer, invocation, cancellation, artifacts):
        self.authorize, self.workspace = authorize, workspace
        self.runtime, self.reducer, self.invocation = runtime, reducer, invocation
        self.cancellation, self.artifacts = cancellation, artifacts
        self._lock = threading.Lock()
        self._issued = False
        self.receipt = None
        self.artifact = None

    @property
    def unresolved(self):
        return self._issued and (self.receipt is None or self.receipt["status"] not in {"passed", "not_required"})

    def handoff(self):
        """A Runtime-authored evidence gap; never fabricate a successful verdict."""
        return "Verification evidence is incomplete. The primary must provide the missing/failed check " \
            "and arrange fresh verification; unrun checks cannot pass. " + json.dumps({
                "requested_by": self.invocation.node_id,
                "objective": self.invocation.objective,
                "acceptance_criteria": self.invocation.acceptance_criteria,
                "receipt": self.receipt,
                "report_artifact": self.artifact.to_dict() if self.artifact else None,
            }, ensure_ascii=False)

    def attach_to_result(self, result):
        if self.unresolved and result.status is AgentResultStatus.SUCCEEDED:
            message = self.handoff()
            result = replace(result, status=AgentResultStatus.FAILED_TERMINAL, output_summary=message,
                verification_records=(), failure=ExecutionFailure(ExecutionFailureKind.VERIFICATION_FAILED, message))
        elif self.unresolved:
            result = replace(result, output_summary=result.output_summary + "\n\n" + self.handoff())
        if self.artifact is not None:
            result = replace(result, artifact_refs=(*result.artifact_refs, self.artifact))
        return result

    def tool(self):
        def validate(value):
            if not isinstance(value, dict) or value:
                raise ValueError("request_verification takes no arguments; Runtime owns checks, commands and input revision")
            return {}

        def run(_value, context):
            with self._lock:
                revision = self.authorize(context)
                if self._issued and self.receipt is None:
                    return ToolResult(ok=False, verificationFailed=True,
                        output="Verification request has no settled receipt; it cannot be replayed. Return this evidence gap to the primary.")
                reused = self._issued
                if not self._issued:
                    self._issued = True
                    receipt = observe_candidate_checks(workspace=self.workspace,
                        candidate_revision=revision, authorize=lambda: self.authorize(context),
                        runtime=self.runtime, reducer=self.reducer, invocation=self.invocation,
                        cancellation=context.cancellation_event or self.cancellation,
                        permissions=context.permissions, artifact_store=self.artifacts,
                        require_command_permissions=True)
                    # Cancellation, workspace changes and stale activations must
                    # fail before publishing a reusable receipt.
                    if self.authorize(context) != revision:
                        raise ValueError("verification input changed before receipt publication")
                    receipt = {**receipt, "run_id": self.invocation.run_id,
                        "node_id": self.invocation.node_id, "attempt_id": self.invocation.attempt_id,
                        "acceptance_criteria": self.invocation.acceptance_criteria}
                    artifact = self.artifacts.put_text(json.dumps(receipt, ensure_ascii=False),
                        run_id=self.invocation.run_id, node_id=self.invocation.node_id,
                        media_type="application/json")
                    self.receipt, self.artifact = receipt, artifact
                if self.receipt["candidate_revision"] != revision:
                    raise ValueError("verification receipt belongs to a different input revision")
                passed = not self.unresolved
                return ToolResult(ok=passed, verificationFailed=not passed,
                    output=json.dumps({**self.receipt, "reused": reused,
                        "report_artifact": self.artifact.to_dict(),
                        "next": ("No automatic code/browser checks apply. Inspect relevant document evidence; no test or criterion was certified."
                                 if self.receipt["status"] == "not_required" else
                                 "Read the original checks and judge their actual coverage. A check pass does not certify every criterion."
                                 if passed else "Return the unresolved criterion, needed evidence and suggested check to the primary. "
                                 "The registered checks are missing, failed or unavailable; do not repeat this request or claim verification passed.")},
                        ensure_ascii=False))

        _NATIVE_CHECK_HANDLERS.add(run)
        _NATIVE_REQUESTS[run] = self
        return ToolDefinition(name=REQUEST_VERIFICATION,
            description="Request Runtime-registered checks for this verifier's fixed input version in its current workspace. "
                "Isolation is used only when configured. Temporary outputs are separate; source changes invalidate evidence. "
                "No arguments, arbitrary commands, edits, new checks or permission grants. Existing command approvals apply. "
                "Only one execution per activation; repeats read the retained receipt. Not final acceptance. "
                "If checks are missing/failed, report the exact evidence gap and suggested check to the primary.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            validator=validate, run=run,
            metadata=ToolMetadata(name=REQUEST_VERIFICATION, description="Registered verification request",
                effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.NEVER_AUTO_RETRY))
