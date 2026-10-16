"""Independent classification of provider tool-argument integrity.

Provider termination and Runtime diagnosis are separate facts. This module never
rewrites ``finish_reason``: a provider that stopped with ``tool_calls`` keeps that
value even when the arguments are unusable. What it adds is an independently
computed classification plus an explicit decision about whether the arguments may
reach a tool.

Guessing is never an option. Truncated or malformed arguments are preserved
verbatim for continuation, refused before any side effect, and reported through a
classification whose evidence is reproducible from stored bytes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

VALID = "valid"
INVALID_ARGUMENTS = "invalid_arguments"
INCOMPLETE_ARGUMENTS_AT_OUTPUT_CAP = "incomplete_arguments_at_output_cap"
TRANSPORT_INCOMPLETE = "transport_incomplete"
ASSEMBLY_ERROR = "assembly_error"
UNKNOWN = "unknown"

CLASSIFICATIONS = frozenset({
    VALID, INVALID_ARGUMENTS, INCOMPLETE_ARGUMENTS_AT_OUTPUT_CAP,
    TRANSPORT_INCOMPLETE, ASSEMBLY_ERROR, UNKNOWN,
})

# Provider-side terminations that are not a completed tool proposal.
_INCOMPLETE_FINISH_REASONS = frozenset({"length", "max_tokens"})


@dataclass(frozen=True, slots=True)
class ArgumentDiagnostic:
    """One classification, with the evidence that produced it.

    ``provider_finish_reason`` is recorded verbatim. ``runtime_classification``
    is this module's independent judgment. Neither field replaces the other.
    """

    runtime_classification: str
    provider_finish_reason: str
    parse_ok: bool
    parse_error_type: str | None
    parse_error_message: str | None
    raw_argument_bytes: int
    argument_sha256: str
    output_token_limit: int | None
    output_tokens_used: int | None
    at_output_cap: bool
    transport_complete: bool
    transport_termination: str
    unresolved_string: bool
    balance: dict[str, int]
    evidence_complete: bool
    may_execute: bool
    refusal_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def argument_text(value: Any) -> str:
    """Return the provider's exact argument text without inventing one."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def decode_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Decode one protocol argument object; malformed output is not an empty call.

    This is the single decoder shared by the live adapter and by the classifier
    in this module, so a verdict about the same bytes cannot diverge between the
    layer that refuses the call and the layer that classifies it.
    """
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    if not isinstance(arguments, str):
        raise ValueError("arguments must be a JSON string")
    parsed = json.loads(arguments, parse_constant=reject_constant)
    if not isinstance(parsed, dict):
        raise ValueError("arguments must decode to an object")
    json.dumps(parsed, allow_nan=False)  # Also rejects numeric overflow such as 1e999.
    return parsed


def rejected_argument_details(call: dict[str, Any]) -> dict[str, Any]:
    """Describe exact rejected bytes without repairing or decoding a prefix.

    The full original remains in protocol history. The bounded preview is for
    display only and must never become an executable input or recovery identity.
    """
    raw = call.get("rawArguments")
    details: dict[str, Any] = {"classification": call.get("executionBlocked") or INVALID_ARGUMENTS}
    if not isinstance(raw, str):
        details["message"] = "The provider did not supply a JSON argument string."
        return details
    details.update(rawArguments=raw[:4000], truncated=len(raw) > 4000)
    try:
        decode_tool_arguments(raw)
    except json.JSONDecodeError as error:
        details.update(message=error.msg, line=error.lineno, column=error.colno, position=error.pos)
    except (ValueError, TypeError, RecursionError) as error:
        details["message"] = str(error)[:300]
    else:
        details["message"] = "The response was rejected before tool execution."
    return details


def scan_json_text(raw: str) -> dict[str, int]:
    """Byte-level JSON structure scan; never repairs, never filters.

    The counts are diagnostics for a human or a verifier, not a parser: the
    authoritative parse result still comes from ``json.loads``.
    """
    curly = square = 0
    in_string = False
    escaped = False
    for char in raw:
        if escaped:
            escaped = False
            continue
        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
    return {"unbalanced_curly": curly, "unbalanced_square": square,
            "unterminated_string": 1 if in_string else 0}


def classify_arguments(
    raw: str,
    *,
    provider_finish_reason: str | None,
    transport_complete: bool = True,
    transport_termination: str = "unknown",
    output_token_limit: int | None = None,
    output_tokens_used: int | None = None,
    parse_error: BaseException | None = None,
    parsed_ok: bool | None = None,
    sha256: str = "",
) -> ArgumentDiagnostic:
    """Classify one provider tool call's arguments.

    Order matters. A transport integrity gap outranks everything: bytes we never
    received cannot be classified as a provider formatting choice. A parse
    failure is reported with its real decoder error. Only then is the output cap
    considered, and only as corroboration, never as the sole verdict.

    ``parse_error``/``parsed_ok`` let the caller pass in the verdict of the very
    decoder it already ran, so this function never disagrees with that decoder.
    When they are omitted the real decoder is run here.
    """
    encoded_length = len(raw.encode("utf-8"))
    balance = scan_json_text(raw)
    finish = provider_finish_reason if isinstance(provider_finish_reason, str) else "unknown"
    at_cap = (output_token_limit is not None and output_tokens_used is not None
              and output_tokens_used >= output_token_limit)

    decoded_is_object = bool(parsed_ok)
    if parse_error is None and parsed_ok is None:
        try:
            decoded = decode_tool_arguments(raw)
        except (ValueError, TypeError) as error:
            parse_error = error
            decoded_is_object = False
        else:
            decoded_is_object = isinstance(decoded, dict)

    def build(classification: str, *, may_execute: bool, refusal_reason: str,
              evidence_complete: bool) -> ArgumentDiagnostic:
        return ArgumentDiagnostic(
            runtime_classification=classification,
            provider_finish_reason=finish,
            parse_ok=decoded_is_object,
            parse_error_type=type(parse_error).__name__ if parse_error is not None else None,
            parse_error_message=str(parse_error) if parse_error is not None else None,
            raw_argument_bytes=encoded_length,
            argument_sha256=sha256,
            output_token_limit=output_token_limit,
            output_tokens_used=output_tokens_used,
            at_output_cap=at_cap,
            transport_complete=transport_complete,
            transport_termination=transport_termination,
            unresolved_string=bool(balance["unterminated_string"]),
            balance=balance,
            evidence_complete=evidence_complete,
            may_execute=may_execute,
            refusal_reason=refusal_reason,
        )

    if not transport_complete:
        return build(TRANSPORT_INCOMPLETE, may_execute=False, evidence_complete=False, refusal_reason=(
            "the response ended without a recognized terminal marker "
            f"(transport_termination={transport_termination}); the remaining bytes are unknown"))
    if decoded_is_object:
        return build(VALID, may_execute=True, refusal_reason="", evidence_complete=True)
    if parsed_ok is False and parse_error is None:
        return build(ASSEMBLY_ERROR, may_execute=False, evidence_complete=True, refusal_reason=(
            "the caller's decoder rejected the argument bytes without a decoder exception"))
    if parse_error is None:
        # Decoded but not an object: a shape the tool contract cannot consume.
        return build(INVALID_ARGUMENTS, may_execute=False, evidence_complete=True, refusal_reason=(
            "arguments decoded to a non-object JSON value; a tool call needs a JSON object"))
    if at_cap:
        # Cap hit is corroboration, never the sole verdict: the decoder error is
        # reported above and the provider's own finish reason is kept verbatim.
        return build(INCOMPLETE_ARGUMENTS_AT_OUTPUT_CAP, may_execute=False, evidence_complete=True,
                     refusal_reason=(
                         f"arguments did not parse and the provider's output used its full "
                         f"{output_token_limit}-token allowance (finish_reason={finish})"))
    if finish in _INCOMPLETE_FINISH_REASONS:
        return build(INCOMPLETE_ARGUMENTS_AT_OUTPUT_CAP, may_execute=False, evidence_complete=True,
                     refusal_reason=(
                         f"the provider reported an incomplete termination "
                         f"(finish_reason={finish}) with unparsable arguments"))
    if finish == "unknown":
        return build(UNKNOWN, may_execute=False, evidence_complete=True, refusal_reason=(
            "arguments did not parse and no provider termination was observed"))
    return build(INVALID_ARGUMENTS, may_execute=False, evidence_complete=True, refusal_reason=(
        f"arguments are not a valid JSON object (finish_reason={finish}); guessing them is not allowed"))


def batch_may_execute(diagnostics: list[ArgumentDiagnostic]) -> bool:
    """A batch executes only when every call's arguments are independently valid."""
    return bool(diagnostics) and all(item.may_execute for item in diagnostics)
