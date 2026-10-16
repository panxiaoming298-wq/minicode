from __future__ import annotations

from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path
from minicode.tools._validation import validate_contract

from pathlib import Path
import hashlib
import re

DEFAULT_READ_LIMIT = 8000
MAX_READ_LIMIT = 20000
DEFAULT_LINE_COUNT = 100
MAX_LINE_COUNT = 400

def _validate(input_data: dict) -> dict:
    validate_contract(input_data, READ_FILE_INPUT)
    path = input_data["path"]
    focus = input_data.get("focus_symbol")
    # Explicit legacy character cursors remain raw by default. Ordinary/line
    # reads carry citation coordinates, avoiding separate reads just to count.
    numbered = input_data.get("line_numbers", "offset" not in input_data and "limit" not in input_data)
    extra = {"line_numbers": numbered}
    if focus is not None:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", focus):
            raise ValueError("focus_symbol must be a Python qualified name")
        extra["focus_symbol"] = focus
    if "start_line" in input_data or "line_count" in input_data:
        if "offset" in input_data or "limit" in input_data:
            raise ValueError("Use start_line/line_count OR character offset/limit, not both.")
        start_line = input_data.get("start_line", 1)
        line_count = input_data.get("line_count", DEFAULT_LINE_COUNT)
        return {"path": path, "start_line": start_line, "line_count": line_count, **extra}
    offset = input_data.get("offset", 0)
    limit = input_data.get("limit", DEFAULT_READ_LIMIT)
    return {"path": path, "offset": offset, "limit": limit, **extra}


def _run(input_data: dict, context) -> ToolResult:
    target = resolve_tool_path(context, input_data["path"], "read")
    from minicode.candidate_read_obligations import current_read_obligations
    from minicode.workspace_provider import snapshot_revision, workspace_snapshot
    obligations = current_read_obligations()
    before_revision = snapshot_revision(workspace_snapshot(Path(context.cwd))) if obligations is not None else None

    try:
        # Each tool call is a fresh observation. mtime (even with a short TTL)
        # is not content identity: quick edits, checkout and timestamp-preserving
        # writers can change bytes without changing it. Rely on the OS file
        # cache, not a process-global cache of potentially stale evidence.
        raw_content = target.read_bytes()
        content = raw_content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        return ToolResult(
            ok=False,
            output=f"File {input_data['path']} appears to be binary. Cannot read as text.",
            failureKind="unsupported_content",
        )
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            from minicode.native_read_set import current_native_read_set
            observations = current_native_read_set()
            if observations is not None:
                observations.record_missing(target)
        kind = ("not_found" if isinstance(error, FileNotFoundError) else
                "permission_denied" if isinstance(error, PermissionError) else
                "invalid_input" if isinstance(error, (IsADirectoryError, NotADirectoryError)) else None)
        return ToolResult(ok=False, output=f"Cannot read file {input_data['path']}: {error}", failureKind=kind)

    line_headers: list[str] = []
    if "start_line" in input_data:
        lines = content.splitlines(keepends=True)
        first = input_data["start_line"] - 1
        if first >= len(lines) and first != 0:
            # A failed read observed nothing about the file's content, so it can
            # never close an obligation: the caller learns the line does not
            # exist, not what the file says.
            return ToolResult(ok=False, output=f"start_line exceeds TOTAL_LINES: {len(lines)}", failureKind="invalid_input")
        selected = lines[first:first + input_data["line_count"]]
        offset = sum(map(len, lines[:first]))
        requested_end = offset + sum(map(len, selected))
        end = min(requested_end, offset + MAX_READ_LIMIT)
        # A single long line can exceed the output cap. Return its exact
        # character cursor rather than skipping the unread remainder.
        continuation = (f"offset {end}" if end < requested_end
                        else f"start_line {first + len(selected) + 1}")
        line_headers = [f"START_LINE: {first + 1}", f"TOTAL_LINES: {len(lines)}",
                        f"LINE_WINDOW_CHAR_CAPPED: {'yes' if end < requested_end else 'no'}"]
    else:
        offset = input_data["offset"]
        end = min(len(content), offset + input_data["limit"])
        continuation = f"offset {end}"
    chunk = content[offset:end]
    shown = chunk
    if input_data["line_numbers"]:
        if "start_line" in input_data:
            first_line = input_data["start_line"]
        else:
            prefix = content[:offset]
            first_line = len(prefix.splitlines()) + (not prefix or prefix.endswith("\n"))
            line_headers.append(f"START_LINE: {first_line}")
        line_headers.append("LINE_NUMBERS: display-only; offsets exclude prefixes")
        shown = "".join(f"{number}: {line}" for number, line in
                        enumerate(chunk.splitlines(keepends=True), first_line))
    from minicode.native_read_set import current_native_read_set
    observations = current_native_read_set()
    if observations is not None:
        observations.record_read(target, raw_content)
    truncated = end < len(content)
    header = "\n".join(
        [
            f"FILE: {input_data['path']}",
            "OFFSET_UNIT: characters (0-based, END exclusive)",
            f"OFFSET: {offset}",
            f"END: {end}",
            f"TOTAL_CHARS: {len(content)}",
            *line_headers,
            f"TRUNCATED: {'yes - more file content; if needed, call read_file again with ' + continuation if truncated else 'no'}",
            "",
        ]
    )
    # Record the version-bound ranges returned by the native tool. Request
    # projection happens later, so this proves tool coverage, not provider
    # visibility, comprehension or continued residency. Only complete coverage
    # closes the existing write gate; task acceptance needs independent checks.
    if obligations is not None and snapshot_revision(workspace_snapshot(Path(context.cwd))) == before_revision:
        relative = target.resolve().relative_to(Path(context.cwd).resolve()).as_posix()
        obligations.observe_window(relative, raw_content, revision=before_revision,
                                   start=offset, end=end, total=len(content))
    result = ToolResult(ok=True, output=header + shown,
                        contextReadSha256=hashlib.sha256(raw_content).hexdigest())
    # Authenticate at the native reader. Production workspace wrappers replace
    # the outer handler, so registry-level callable identity loses this source.
    # Permissions and exact-byte observations above still precede projection.
    skill_runtime = (context._runtime or {}).get("_skillRuntime")
    if skill_runtime is not None:
        return skill_runtime.project_source_read(read_file_tool, input_data, result, context)
    return result


def _observe_obligation(target: Path, context, requested: str, content: bytes) -> None:
    """Record a whole-file read against the bound obligations.

    A partial window, a truncated response or a failed window returned a subset of
    the file, so it proves visibility of that subset and nothing more.
    """
    from minicode.candidate_read_obligations import current_read_obligations, observe_read

    obligations = current_read_obligations()
    if obligations is None or not obligations.open_paths():
        return
    try:
        relative = target.resolve().relative_to(Path(context.cwd).resolve()).as_posix()
    except (AttributeError, ValueError, OSError):
        relative = requested
    # The observation revision is the candidate revision the obligation belongs
    # to, never a value echoed back from the caller: echoing the expected value
    # would let a stale read satisfy the promise it was created under.
    from minicode.workspace_provider import snapshot_revision, workspace_snapshot
    actual_revision = snapshot_revision(workspace_snapshot(Path(context.cwd)))
    observe_read(relative, content, revision=actual_revision)


READ_FILE_INPUT = {"type": "object", "additionalProperties": False, "properties": {
        "path": {"type": "string", "minLength": 1},
        "line_numbers": {"type": "boolean", "description": "Show display-only original line numbers. Defaults true for ordinary/line reads, false for explicit character cursors."},
        "focus_symbol": {"type": ["string", "null"], "maxLength": 200, "description": "Optional exact Python function qualified name (e.g. Handler.run) to retain as current code focus. Null means no focus. Does not change the read window; read all module windows to resolve imports."},
        "start_line": {"type": "integer", "minimum": 1, "description": "First line, 1-based (default 1)."},
        "line_count": {"type": "integer", "minimum": 1, "maximum": MAX_LINE_COUNT,
                       "description": "Number of lines (default 100). Do not combine with offset/limit."},
        "offset": {"type": "integer", "minimum": 0, "description": "Legacy 0-based CHARACTER offset (default 0)."},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_READ_LIMIT,
                  "description": "Legacy CHARACTER count (default 8000); omit to use the default, never null."},
    }, "required": ["path"]}


read_file_tool = ToolDefinition(
    name="read_file",
    description=("Read a UTF-8 file relative to the workspace root. For code, use start_line/line_count; "
                 "ordinary/line reads show original line numbers for citations. Prefixes are not file content. "
                 "Legacy offset/limit count CHARACTERS, never lines, and default to raw output. "
                 "Choose one mode; omit unused cursor fields instead of supplying null. "
                 "Example line read: {\"path\":\"src/main.py\",\"start_line\":1,\"line_count\":80}. "
                 "At most 20000 source characters are returned. A complete requested line window "
                 "can still report TRUNCATED when later file lines exist; read those only when needed."),
    input_schema=READ_FILE_INPUT,
    validator=_validate,
    run=_run,
)
