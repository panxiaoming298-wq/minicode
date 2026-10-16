"""A live verifier's one-way, complete-evidence final judgment boundary.

The two-request quote is a conservative stopping signal, not a reservation of
future tool output. Only the selected final request is atomically reserved.
No paged observation is substituted for a complete receipt in that request.
"""
from copy import deepcopy
import hashlib
import json

from minicode.usage_ledger import BudgetUnavailable


POLICY = "native-verifier-final-judgment-v1"
NOTICE = (
    " Evidence acquisition is now CLOSED for this final judgment: the remaining budget does not support "
    "another exploration-and-judgment cycle. Earlier instructions to use tools are inactive in this phase. "
    "All previously recorded tool outputs are included in full; initial source ranges may still be partial. "
    "Evaluate every original criterion independently using only the evidence actually present. If essential "
    "evidence is missing or a condition is unresolved, explain the gap and emit <verification_failed>; "
    "never infer that unread evidence passed. Otherwise give the supported verdict using the original "
    "terminal marker. Return the judgment now, without tool calls or progress-only output."
)


def fingerprint(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class VerifierJudgment:
    def __init__(self, *, unavailable_on_gap=False):
        self.unavailable_on_gap = unavailable_on_gap
        self.complete = None
        self.projected = None
        self.proposed = None
        self.admitted = False

    def observe_projection(self, complete, projected):
        self.complete, self.projected = deepcopy(complete), deepcopy(projected)
        self.proposed = None

    def prepare(self, body, *, quote, desired_output, measure):
        self.proposed = None
        diagnostic = {"policy": POLICY, "selected": False, "future_capacity_reserved": False,
                      "counts_as_acceptance": False, "read_growth_guaranteed": False}
        if self.complete is None or body != self.projected:
            return body, {**diagnostic, "reason": "no_matching_native_evidence_projection"}
        if body.get("tool_choice", "auto") not in ("auto", "none"):
            if self.admitted:
                raise BudgetUnavailable("explicit tool choice conflicts with the closed verifier judgment phase")
            return body, {**diagnostic, "reason": "explicit_tool_choice_requires_original_request"}
        try:
            two_funded = quote(self.complete, copies=2)["output_reservation"] >= desired_output * 2
        except BudgetUnavailable:
            two_funded = False
        diagnostic["two_complete_current_requests_quoted"] = two_funded
        if two_funded and body.get("tool_choice") != "none" and not self.admitted:
            return body, diagnostic
        candidate = deepcopy(self.complete)
        candidate.pop("tools", None)
        candidate.pop("parallel_tool_calls", None)
        candidate["tool_choice"] = "none"
        packet = candidate["messages"][-1]
        prefix, evidence = packet["content"].split("\n", 1)
        notice = NOTICE
        if self.unavailable_on_gap:
            notice = notice.replace("explain the gap and emit <verification_failed>",
                                    "explain the gap and emit <verification_unavailable>")
        packet["content"] = prefix + notice + "\n" + evidence
        self.proposed = fingerprint(candidate)
        try:
            funded = quote(candidate)["output_reservation"] >= desired_output
        except BudgetUnavailable:
            funded = False
        return candidate, {**diagnostic, "selected": True, "phase": "final_judgment",
            "trigger": "phase_already_closed" if self.admitted else
                "explicit_no_tools" if body.get("tool_choice") == "none" else "continuation_not_funded",
            "complete_tool_observations_preserved": True, "tools_closed_after_admission": True,
            "full_judgment_output_quoted": funded,
            "evidence_packet_sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
            "input_before": measure(body), "input_after": measure(candidate)}

    def admitted_request(self, body):
        if self.proposed == fingerprint(body):
            self.admitted = True

    def tool_blocker(self):
        if self.admitted:
            from minicode.tooling import ToolResult
            return ToolResult(ok=False, failureKind="policy_denied", inputRejected=True,
                runtimeStopReason="verifier_judgment_tools_closed", output=(
                    "VerifierJudgmentClosed: no tool was executed. The admitted request requires an independent "
                    "final verdict from existing evidence; unexpected tool calls cannot reopen acquisition."))
        return None
