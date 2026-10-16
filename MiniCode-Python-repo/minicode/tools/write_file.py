from __future__ import annotations

from minicode.file_review import apply_reviewed_file_change
from minicode.tooling import ToolDefinition
from minicode.workspace import resolve_tool_path
from minicode.tools._validation import validate_contract


WRITE_FILE_INPUT = {"type": "object", "additionalProperties": False,
    "properties": {"path": {"type": "string", "minLength": 1}, "content": {"type": "string"}},
    "required": ["path", "content"]}


def _validate(input_data: dict) -> dict:
    validate_contract(input_data, WRITE_FILE_INPUT)
    return {"path": input_data["path"], "content": input_data["content"]}


def _run(input_data: dict, context):
    refusal = _enforce_read_obligation(input_data, context)
    if refusal is not None:
        return refusal
    target = resolve_tool_path(context, input_data["path"], "write")
    return apply_reviewed_file_change(context, input_data["path"], target, input_data["content"])


write_file_tool = ToolDefinition(
    name="write_file",
    description="Write a UTF-8 text file relative to the workspace root.",
    input_schema=WRITE_FILE_INPUT,
    validator=_validate,
    run=_run,
)

def _enforce_read_obligation(input_data, context):
    """FIX09 gate: refuse a write to content the model has not really read.

    Returns a refusal ToolResult, or None when the write may proceed. With no
    binding this is a no-op, so behaviour outside a bound candidate is unchanged.
    """
    from minicode.candidate_read_obligations import ReadObligationUnmet, enforce_write
    from minicode.tooling import ToolResult

    path = input_data.get("path")
    if not isinstance(path, str) or not path:
        return None
    try:
        enforce_write(path)
    except ReadObligationUnmet as unmet:
        return ToolResult(ok=False, inputRejected=True, output=(
            f"UnreadCandidateContent: {unmet}; no write was attempted. Read the current "
            "revision of that file first."))
    return None

