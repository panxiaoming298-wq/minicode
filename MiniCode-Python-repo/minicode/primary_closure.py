"""Request-local closure advice, not a reservation, task plan or completion gate."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from minicode.usage_ledger import BudgetUnavailable


_ADVICE = (
    "Runtime closure advisory: conservative reservations do not cover two more same-size full-output requests. "
    "This is NOT an exact token count or a guaranteed last turn; future input can grow. "
    "Prioritize the remaining implementation and required checks; avoid redundant reads and optional expansion. "
    "When ready, explicitly call submit_candidate LAST in the same ordered batch as final writes/checks, or alone. "
    "Submission requests the registered checks itself; do not add a separate diagnostic precheck merely to finish. "
    "Earlier errors/waits prevent handoff. Do not waive required consultation, permissions or acceptance. "
    "If the task cannot be completed safely, report what remains incomplete. Submission is not success."
)

_WORKING_ADVICE = (
    "Runtime closure advisory: conservative reservations cannot cover two more same-size full-output requests. "
    "This is not an exact count or guaranteed last turn; future input can grow. "
    "Native report reading is complete; no current working notes are recorded. If another model request is needed, "
    "call record_working_context now alongside necessary tools in this response. For EVERY delivered summary artifact, "
    "retain its exact artifact ID, supported decisions, source locations, critical unresolved risks and next steps. "
    "Notes are your unverified assessments; full reports remain readable. Do not copy entire reports, invent findings "
    "or omit risks for brevity. If ready to finish instead, batch final writes/checks and submit_candidate LAST. "
    "Errors/waits block handoff. Notes waive no reading, permissions, required checks or submission; integration still "
    "requires acceptance. If neither path is safe, report incomplete. Neither notes nor submission guarantee success."
)

_CONSULTATION_ADVICE = (
    "Runtime consultation closure advisory: the shared ledger cannot currently fund two same-size full-output requests. "
    "This is not an exact token count or a guaranteed last turn; history growth and sibling reservations can change capacity. "
    "Prioritize your bounded findings and missing essential evidence. If ready, call submit_consultation with supported "
    "findings, source locations, actual check results and unresolved risks. Avoid redundant reads/checks; do not invent "
    "restrictions or claim failed/unrun checks passed. Do not waive assigned requirements or permissions; the primary owns "
    "implementation and acceptance. Reporting is not task success."
)

_READ_ONLY_ADVICE = (
    "Runtime read-only closure advisory: conservative reservations cannot fund two more same-size full-output requests. "
    "This is not an exact token count or a guaranteed last turn; future input can grow. "
    "Use the source evidence already read to answer the requested questions with locations. "
    "Make further reads only to resolve a specific essential gap; avoid broad exploration or rechecking unchanged evidence. "
    "Distinguish demonstrated findings from unverified risks and state missing evidence or unrun checks explicitly. "
    "Any required independent verification and its reserved budget remain in force. Do not waive requirements, "
    "permissions or checks to finish; report incomplete work honestly. This advisory is not acceptance or proof of completion."
)


def has_candidate_handoff(body: Mapping[str, Any]) -> bool:
    """Inspect the wire tool view only; this never grants execution authority."""
    return _has_handoff(body, "submit_candidate")


def _has_handoff(body: Mapping[str, Any], name: str) -> bool:
    tools = body.get("tools")
    return isinstance(tools, list) and any(
        isinstance(tool, Mapping) and tool.get("type") == "function"
        and isinstance(tool.get("function"), Mapping)
        and tool["function"].get("name") == name for tool in tools
    )


def prepare_closure_advice(
    body: Mapping[str, Any], *, quote: Callable[..., dict[str, Any]], desired_output: int,
    working_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Use native note readiness only when pressure and its own cost allow it.

    The compiler supplies and revalidates readiness, not caller/model text.
    No forced tool choice, extra model turn, memory write or budget reservation.
    An unaffordable note hint falls back to the original closure advisory.
    """
    tools = body.get("tools")
    if working_context is not None and isinstance(tools, list) and sum(
        isinstance(tool, Mapping) and tool.get("type") == "function"
        and isinstance(tool.get("function"), Mapping)
        and tool["function"].get("name") == "record_working_context" for tool in tools
    ) == 1:
        prepared, diagnostic = _prepare_closure_advice(body, quote=quote, desired_output=desired_output,
            handoff="submit_candidate", advice=_WORKING_ADVICE, policy="primary-closure-advice-v1")
        if diagnostic and diagnostic.get("omission_reason") == "preserve_current_request_grant":
            prepared, diagnostic = _prepare_closure_advice(body, quote=quote, desired_output=desired_output,
                handoff="submit_candidate", advice=_ADVICE, policy="primary-closure-advice-v1")
            if diagnostic is not None:
                diagnostic["working_context_omission_reason"] = "preserve_current_request_grant"
        elif diagnostic and (diagnostic["message_included"]
            or diagnostic.get("omission_reason") == "identical_advice_already_present"):
            diagnostic["working_context"] = dict(working_context)
        return prepared, diagnostic
    return _prepare_closure_advice(body, quote=quote, desired_output=desired_output,
        handoff="submit_candidate", advice=_ADVICE, policy="primary-closure-advice-v1")


def prepare_consultation_closure_advice(
    body: Mapping[str, Any], *, quote: Callable[..., dict[str, Any]], desired_output: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Advise an eligible shared-budget helper; the caller checks live authority."""
    return _prepare_closure_advice(body, quote=quote, desired_output=desired_output,
        handoff="submit_consultation", advice=_CONSULTATION_ADVICE, policy="consultation-closure-advice-v1")


def prepare_readonly_closure_advice(
    body: Mapping[str, Any], *, quote: Callable[..., dict[str, Any]], desired_output: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The caller must establish a live read-only primary; no write handoff is invented."""
    return _prepare_closure_advice(body, quote=quote, desired_output=desired_output,
        handoff=None, advice=_READ_ONLY_ADVICE, policy="readonly-primary-closure-advice-v1")


def _prepare_closure_advice(
    body: Mapping[str, Any], *, quote: Callable[..., dict[str, Any]], desired_output: int,
    handoff: str | None, advice: str, policy: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Quote one and two copies of today's request without mutating the ledger.

    The two-copy forecast uses the same conservative reservation as admission,
    not an empirical token ratio. It cannot predict the next response/history.
    Include advice only if its own bytes preserve the current output grant;
    final atomic admission must still run after this function.
    """
    prepared = dict(body)
    if (not isinstance(body.get("messages"), list) or (handoff is not None and not _has_handoff(body, handoff))
        or body.get("tool_choice", "auto") not in ("auto", "required")):
        return prepared, None
    try:
        current = quote(body)
    except BudgetUnavailable:
        return prepared, None
    try:
        two_funded = quote(body, copies=2)["output_reservation"] >= 2 * desired_output
    except BudgetUnavailable:
        two_funded = False
    diagnostic = {"policy": policy, "authority": "advisory_only",
                  "basis": "two_same_size_conservative_reservations",
                  "input_reservation": current["input_reservation"],
                  "output_reservation": current["output_reservation"],
                  "two_requests_funded": two_funded, "message_included": False}
    if two_funded:
        return prepared, diagnostic
    if body["messages"] and body["messages"][-1] == {"role": "user", "content": advice}:
        diagnostic["omission_reason"] = "identical_advice_already_present"
        return prepared, diagnostic
    candidate = {**prepared, "messages": [*body["messages"], {"role": "user", "content": advice}]}
    try:
        advised = quote(candidate)
    except BudgetUnavailable:
        advised = None
    if advised is None or advised["output_reservation"] < current["output_reservation"]:
        diagnostic["omission_reason"] = "preserve_current_request_grant"
        return prepared, diagnostic
    diagnostic.update(message_included=True, message_sha256=hashlib.sha256(advice.encode()).hexdigest(),
                      added_input_reservation=advised["input_reservation"] - current["input_reservation"])
    return candidate, diagnostic
