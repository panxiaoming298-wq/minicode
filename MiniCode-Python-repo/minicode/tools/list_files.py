from __future__ import annotations

import json
from pathlib import Path

from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path
from minicode.directory_observation import directory_observation
from minicode.native_read_set import current_native_read_set
from minicode.tools._validation import InputContractError, validate_contract


LIST_FILES_INPUT = {"type": "object", "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1, "default": ".", "description": "One known directory; narrow this before increasing limits."},
        "pattern": {"oneOf": [{"type": "string", "minLength": 1},
            {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string", "minLength": 1}}],
            "description": 'Filename glob or actual JSON array, e.g. "*.py" or ["*.png", "*.jpg"]. Do not encode the array as a string; enables bounded filename discovery.'},
        "recursive": {"type": "boolean", "default": False},
        "max_depth": {"type": "integer", "minimum": 0, "maximum": 20, "default": 6},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        "max_entries": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 20000},
        "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 10, "default": 5},
    }}


def _validate(input_data: dict) -> dict:
    validate_contract(input_data, LIST_FILES_INPUT)
    patterns = input_data.get("pattern", [])
    for index, pattern in enumerate([patterns] if isinstance(patterns, str) else patterns):
        # An encoded array is valid JSON input but is not the intended glob.
        # Reject it before scanning; ordinary glob character classes stay valid.
        if not pattern.lstrip().startswith("["):
            continue
        try:
            decoded = json.loads(pattern)
        except (ValueError, RecursionError):
            continue
        if isinstance(decoded, list) and decoded and all(isinstance(value, str) for value in decoded):
            raise InputContractError(
                'pattern contains a JSON array encoded as text. Use "pattern": "*.py" '
                'or "pattern": ["*.py", "*.txt"], not a quoted array. No search was performed.',
                path=["pattern"] if isinstance(patterns, str) else ["pattern", index],
                expected="glob string or actual JSON array of globs", actual="JSON array encoded as a string")
    return {**input_data, "path": input_data.get("path", ".")}


def _run(input_data: dict, context) -> ToolResult:
    target = resolve_tool_path(context, input_data["path"], "list")
    if not target.exists():
        return ToolResult(ok=False, output=f"Path does not exist: {input_data['path']}")
    if any(key != "path" for key in input_data):
        if not target.is_dir():
            return ToolResult(ok=False, output="Filename discovery requires a directory path; use read_file for this file.",
                failureKind="invalid_input", inputValidationFailed=True, inputRejected=True,
                inputIssues=[{"path": ["path"], "expected": "directory for filename discovery", "actual": "file"}])
        from minicode.tools._directory_search import find_names
        return find_names(Path(target), input_data, getattr(context, "cancellation_event", None))
    if target.is_file():
        return ToolResult(ok=True, output=f"file {Path(input_data['path']).name}")

    read_set = current_native_read_set()
    if read_set is not None and read_set.root is not None:
        entries, digest = directory_observation(Path(target), root=read_set.root)
        read_set.record_directory(target, digest=digest)
        lines = [f"{'dir ' if kind == 'dir' else 'file'} {name}" for name, kind in entries]
    else:
        entries = sorted(Path(target).iterdir(), key=lambda item: item.name.lower())
        lines = [f"{'dir ' if entry.is_dir() else 'file'} {entry.name}" for entry in entries]
    return ToolResult(ok=True, output="\n".join(lines[:200]) if lines else "(empty)")


list_files_tool = ToolDefinition(
    name="list_files",
    description=("List a directory, or discover filenames with pattern and recursive=true. "
        "Use this for locating files/images instead of recursive shell scans. Discovery has depth, entry, result and time limits; "
        "skips generated/hidden directories and links. Inspect complete and limits_reached before claiming no match. "
        "Attached image references are already available; do not rediscover them by scanning storage directories."),
    input_schema=LIST_FILES_INPUT,
    validator=_validate,
    run=_run,
)
