"""Shared structured file search, filtering, limits, and execution receipts."""
from __future__ import annotations

import base64
import fnmatch
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from minicode.tooling import ToolContext, ToolResult
from minicode.tools import _ripgrep
from minicode.workspace import resolve_tool_path
from minicode.tools._validation import validate_contract


SEARCH_INPUT = {"type": "object", "additionalProperties": False, "required": ["pattern"], "properties": {
    "pattern": {"type": "string", "minLength": 1,
                "description": "Regular expression; ripgrep auto-selects PCRE2 for lookaround/backreferences when available"},
    "path": {"type": "string", "minLength": 1, "default": ".",
             "description": "File or directory to search; omit for the current directory"},
    **{name: {"oneOf": [{"type": "string", "minLength": 1},
                         {"type": "array", "items": {"type": "string", "minLength": 1}}, {"type": "null"}],
              "description": f"Glob pattern(s) to {name}, e.g. '*.py' or ['*.ts', '*.tsx']; omit or null for no filter"}
       for name in ("include", "exclude")},
    "context_lines": {"type": "integer", "minimum": 0, "maximum": 100, "default": 0,
                      "description": "Number of context lines before and after each match (0-100). "
                                     "Use larger values for a single file or few matches; output limits still apply. "
                                     "Use read_file for complete code sections"},
    "case_sensitive": {"type": "boolean", "default": False},
}}


SKIP_DIRS = frozenset({
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', '.tox',
    'dist', 'build', '.hg', '.svn', '.next', '.nuxt', 'target',
    'vendor', 'Pods', '.dart_tool', '.gradle', '.idea', '.vscode',
    'coverage', '.coverage', 'htmlcov', '.mypy_cache', '.pytest_cache',
    '.ruff_cache', '.pytype',
})

# Binary file extensions to skip
BINARY_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.svg',
    '.mp3', '.mp4', '.wav', '.avi', '.mov', '.mkv', '.flv',
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.so', '.dll', '.dylib', '.exe', '.bin', '.dat',
    '.pyc', '.pyo', '.class', '.o', '.obj',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
    '.sqlite', '.db', '.lock',
})

MAX_RESULTS = 200
MAX_RESULT_SIZE = 50_000  # chars
SEARCH_TIMEOUT_SECONDS = 10.0
MAX_NATIVE_SNAPSHOT_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------

def _matches_glob(path: Path, include_globs: list[str] | None, exclude_globs: list[str] | None) -> bool:
    """Check if a path matches include/exclude glob patterns.

    Args:
        path: File path relative to search root
        include_globs: If provided, file must match at least one
        exclude_globs: If provided, file must NOT match any
    """
    posix_path = path.as_posix()
    name = path.name

    if exclude_globs:
        for pattern in exclude_globs:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(posix_path, pattern):
                return False

    if include_globs:
        for pattern in include_globs:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(posix_path, pattern):
                return True
        return False  # Didn't match any include pattern

    return True


# ---------------------------------------------------------------------------
# Search logic
# ---------------------------------------------------------------------------

def _eligible(path: Path, options: dict) -> bool:
    return (
        not any(part in SKIP_DIRS or part.startswith(".") for part in path.parts)
        and path.suffix.lower() not in BINARY_EXTENSIONS
        and _matches_glob(path, options["include"], options["exclude"])
    )


def _simple_glob(pattern: str) -> bool:
    # Only push patterns whose filename semantics agree with fnmatch down to
    # rg. Path globs, []/{} and literal ! retain the existing Python semantics.
    return bool(pattern) and pattern.isascii() and "**" not in pattern and not any(
        char in pattern for char in "/\\[]{}!\n\r"
    )


def _type_glob(pattern: str, *, ignore_case: bool) -> str:
    if not ignore_case:
        return pattern
    return "".join(f"[{char.lower()}{char.upper()}]" if char.isascii() and char.isalpha() else char for char in pattern)


def _filename_include(pattern: str) -> bool:
    # fnmatch '*' can consume directory separators: 'src*.py' also includes
    # src/nested/code.py. Only literals or plain suffix globs can safely limit
    # rg to matching basenames without losing those relative-path matches.
    return _simple_glob(pattern) and "?" not in pattern and "*" not in pattern.lstrip("*")


def _command(executable: str, target: str, options: dict) -> list[str]:
    command = [
        executable, "--no-config", "--json", "--line-buffered", "--no-follow",
        "--no-ignore", "--no-hidden", "--color", "never", "--sort", "path",
        "--engine", "auto", "--encoding", "none", "--crlf",
        "--context", str(options["context_lines"]),
        "--case-sensitive" if options["case_sensitive"] else "--ignore-case",
    ]
    # Type filters apply to files, whereas negative --globs also prune matching
    # directories (e.g. excluding *.txt must not hide folder.txt/code.py).
    # fnmatch is case-insensitive on Windows, unlike rg's default glob mode.
    def add_type(name: str, pattern: str, ignore_case: bool = os.name == "nt") -> None:
        command.extend(("--type-add", name + ":" + _type_glob(pattern, ignore_case=ignore_case)))

    if options["include"] and all(_filename_include(pattern) for pattern in options["include"]):
        for pattern in options["include"]:
            add_type("minicodeinclude", pattern)
        command.extend(("--type", "minicodeinclude"))
    simple_excludes = [pattern for pattern in options["exclude"] if _simple_glob(pattern)]
    if simple_excludes:
        for pattern in simple_excludes:
            add_type("minicodeexclude", pattern)
        command.extend(("--type-not", "minicodeexclude"))
    # These exclusions come last so includes cannot open hidden/generated dirs.
    for directory in sorted(SKIP_DIRS):
        command.extend(("--glob", "!" + directory))
    command.extend(("--glob", "!.*"))
    for suffix in sorted(BINARY_EXTENSIONS):
        add_type("minicodebinary", "*" + suffix, ignore_case=True)
    command.extend(("--type-not", "minicodebinary"))
    command.extend(("-e", options["pattern"], "--", target))
    return command


def _json_text(value: dict) -> str:
    if "text" in value:
        if not isinstance(value["text"], str):
            raise ValueError("Invalid ripgrep text value")
        return value["text"]
    return base64.b64decode(value["bytes"], validate=True).decode("utf-8")


@dataclass(frozen=True)
class SearchLine:
    path: str
    line_number: int
    text: str
    is_match: bool
    truncated: bool = False

    @property
    def prefix(self) -> str:
        return f"{self.path}:{self.line_number}: {' ' if not self.is_match else ''}"

    def render(self) -> str:
        suffix = " ... [line truncated]" if self.truncated else ""
        return self.prefix + self.text + suffix


class FileSearchResult:
    """Bounded observations plus the unchanged search success/failure status."""

    def __init__(self) -> None:
        self.lines: list[SearchLine] = []
        self.size = 0
        self.matches = 0
        self.files: set[str] = set()
        self.truncated = ""
        self.receipt = ToolResult(ok=True, output="")
        self.native_file_observation: tuple[Path, bytes] | None = None
        self.native_directory_observation: tuple[list[tuple[Path, bytes]], dict[Path, str]] | None = None

    def to_tool_result(
        self,
        *,
        header: str = "",
        match_label: str = "match(es)",
        empty_output: str = "No matches found.",
    ) -> ToolResult:
        diagnostic = self.receipt.output
        if not self.lines and not self.truncated and not diagnostic and self.receipt.ok:
            output = header + "\n\n" + empty_output if header else empty_output
            return replace(self.receipt, output=output)
        footer = f"{self.matches} {match_label} in {len(self.files)} file(s)"
        if self.truncated:
            footer += f"\n[truncated: {self.truncated}; results may be incomplete. Narrow the search scope.]"
        if diagnostic:
            footer += "\n" + diagnostic
        prefix = header + "\n\n" if header else ""
        body = "\n".join(line.render() for line in self.lines)
        available = max(0, MAX_RESULT_SIZE - len(prefix) - len(footer) - 2)
        if len(body) > available:
            footer = "[partial output truncated]\n" + footer
            available = max(0, MAX_RESULT_SIZE - len(prefix) - len(footer) - 2)
        output = (prefix + body[:available] + "\n\n" + footer).strip()[:MAX_RESULT_SIZE]
        return replace(self.receipt, output=output)


def _collect_event(result: FileSearchResult, root: Path, options: dict, event: dict[str, Any]) -> bool:
    kind = event["type"]
    if kind not in {"match", "context"}:
        return True
    data = event["data"]
    try:
        path = Path(_json_text(data["path"]))
        text = _json_text(data["lines"]).rstrip("\r\n")
    except UnicodeDecodeError:
        # Preserve exact UTF-8 observations rather than replacement characters.
        return True
    relative = path.relative_to(root) if path.is_absolute() else path
    if ".." in relative.parts:
        raise ValueError("ripgrep returned a path outside the search root")
    if not _eligible(relative, options):
        return True
    number = data["line_number"]
    if type(number) is not int or number < 1:
        raise ValueError("ripgrep returned an invalid line number")
    entry = SearchLine(relative.as_posix(), number, text, kind == "match")
    rendered_size = len(entry.render()) + 1
    # Reserve room for tool-specific labels, the summary, and truncation notice.
    available = MAX_RESULT_SIZE - 512 - result.size
    stop = False
    if rendered_size > available:
        result.truncated = "output character limit reached"
        if available <= len(entry.prefix) + 24:
            return False
        entry = replace(entry, text=text[:available - len(entry.prefix) - 24], truncated=True)
        rendered_size = len(entry.render()) + 1
        stop = True
    result.lines.append(entry)
    result.size += rendered_size
    if entry.is_match:
        result.matches += 1
        result.files.add(entry.path)
        if result.matches >= MAX_RESULTS:
            result.truncated = f"stopped at {MAX_RESULTS} matching lines"
            stop = True
    return not stop


def validate_input(input_data: dict) -> dict:
    validate_contract(input_data, SEARCH_INPUT)
    pattern = input_data.get("pattern")
    if not isinstance(pattern, str) or not pattern or "\0" in pattern:
        raise ValueError("pattern is required")

    # Validate regex
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}")

    globs = {}
    for name in ("include", "exclude"):
        value = input_data.get(name)
        if value is None:
            value = []
        elif isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(item, str) and item and "\0" not in item for item in value):
            raise ValueError(f"{name} must be a glob string or a list of non-empty glob strings")
        globs[name] = value

    path = input_data.get("path", ".")
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("path must be a non-empty path string")
    context_lines = input_data.get("context_lines", 0)
    case_sensitive = input_data.get("case_sensitive", False)

    return {
        "pattern": pattern,
        "path": path,
        **globs,
        "context_lines": context_lines,
        "case_sensitive": case_sensitive,
    }


def search_files(input_data: dict, context: ToolContext, *, intent: str = "search") -> FileSearchResult:
    result = FileSearchResult()
    try:
        options = validate_input(input_data)
    except (ValueError, TypeError) as error:
        result.receipt = ToolResult(ok=False, output=f"Invalid search input: {error}", failureKind="invalid_input")
        return result
    explicit_file = None
    try:
        root = resolve_tool_path(context, options["path"], intent)
        if root.is_file():
            explicit_file = root
            # A bare '-' is rg's stdin sentinel, even after '--'.
            target = "./" + root.name
            root = root.parent
            # Explicit file arguments bypass native rg filters.
            if not _eligible(Path(target), options):
                return result
        elif root.is_dir():
            target = "."
        else:
            result.receipt = ToolResult(ok=False, failureKind="not_found", output=(
                f"Search path does not exist or is not a file/directory: {options['path']}"
            ))
            return result
    except PermissionError as error:
        result.receipt = ToolResult(ok=False, output=f"Permission denied: {error}", failureKind="permission_denied")
        return result
    except RuntimeError as error:
        # Legacy permission managers report a denied approval as RuntimeError.
        # Keep this handling confined to path access, not process execution.
        result.receipt = ToolResult(ok=False, output=f"Cannot access search path: {error}")
        return result
    except OSError as error:
        result.receipt = ToolResult(ok=False, output=f"Cannot read search path: {error}")
        return result

    configured = os.environ.get("MINI_CODE_RIPGREP_PATH")
    executable = shutil.which(configured or "rg")
    if executable is None:
        result.receipt = ToolResult(ok=False, failureKind="unavailable_tool", output=(
            f"ripgrep executable not found: {configured or 'rg'}. "
            "Install ripgrep (rg) on PATH, or set MINI_CODE_RIPGREP_PATH to its executable path."
        ))
        return result
    snapshot = None
    from minicode.native_read_set import current_native_read_set

    if explicit_file is None and current_native_read_set() is not None:
        captured = _search_directory_snapshot(root, options, context, executable)
        if captured is not None:
            return captured

    if explicit_file is not None and current_native_read_set() is not None:
        try:
            with explicit_file.open("rb") as source:
                raw = source.read(MAX_NATIVE_SNAPSHOT_BYTES + 1)
            if len(raw) <= MAX_NATIVE_SNAPSHOT_BYTES and b"\x00" not in raw:
                raw.decode("utf-8")  # Unsupported bytes retain conservative evidence.
                snapshot = raw
        except UnicodeDecodeError:
            pass
        except OSError as error:
            result.receipt = ToolResult(ok=False, output=f"Cannot capture search source: {error}")
            return result

    def collect(event: dict[str, Any]) -> bool:
        if snapshot is not None and event["type"] in {"match", "context"}:
            if _json_text(event["data"]["path"]) != "<stdin>":
                raise ValueError("Snapshot search returned a non-stdin path")
            event = {**event, "data": {**event["data"], "path": {"text": target}}}
        return _collect_event(result, root, options, event)

    try:
        receipt = _ripgrep.search(
            _command(executable, "-" if snapshot is not None else target, options), root,
            collect,
            timeout_seconds=SEARCH_TIMEOUT_SECONDS,
            cancellation_event=getattr(context, "cancellation_event", None),
            stdin_content=snapshot,
        )
    except OSError as error:
        result.receipt = ToolResult(ok=False, failureKind="unavailable_tool", output=f"Cannot start ripgrep: {error}")
        return result

    _apply_receipt(result, receipt)
    if (snapshot is not None and result.receipt.ok and receipt.stop_reason is None
            and not result.truncated and not receipt.stderr):
        result.native_file_observation = (explicit_file, snapshot)
    return result


def _apply_receipt(result: FileSearchResult, receipt: _ripgrep.RipgrepReceipt) -> None:
    """Use the same limits and failure semantics for live and captured searches."""
    if receipt.stop_reason == "canceled":
        result.receipt = ToolResult(ok=False, failureKind="canceled", output="Search canceled; results are incomplete.")
    elif receipt.stop_reason == "timeout":
        result.receipt = ToolResult(ok=False, timedOut=True, output=(
            f"Search timed out after {SEARCH_TIMEOUT_SECONDS:g}s; results are incomplete. Narrow the search."
        ))
    elif receipt.stop_reason in {"invalid_output", "pipe_error"}:
        result.receipt = ToolResult(ok=False, output=(
            f"Cannot read ripgrep JSON output ({receipt.stop_reason}); results are incomplete.\n{receipt.stderr}"
        ))
    elif receipt.returncode not in (0, 1) and not receipt.terminated_early:
        result.receipt = ToolResult(ok=False, output=(
            f"ripgrep failed (exit {receipt.returncode}); results are incomplete.\n{receipt.stderr}"
        ))
    else:
        if receipt.stop_reason == "event_limit":
            result.truncated = "a ripgrep JSON event exceeded the safe size limit"
        result.receipt = ToolResult(ok=True, output=f"ripgrep stderr:\n{receipt.stderr}" if receipt.stderr else "")


def _search_directory_snapshot(root, options, context, executable):
    """Bounded native search over exact input bytes, including negative matches.

    Large, aliased or unsupported scopes retain the ordinary conservative path.
    A snapshot is never inferred from rg's matching paths alone.
    """
    from minicode.directory_observation import directory_observation
    from minicode.path_safety import is_unsafe_stored_path

    started = time.monotonic()
    result = FileSearchResult()
    cancellation = getattr(context, "cancellation_event", None)

    def interrupted():
        reason = ("canceled" if cancellation is not None and cancellation.is_set()
                  else "timeout" if time.monotonic() - started >= SEARCH_TIMEOUT_SECONDS else None)
        if reason:
            _apply_receipt(result, _ripgrep.RipgrepReceipt(-1, "", reason))
        return reason is not None

    files, directories, pending, total = [], {}, [root], 0
    try:
        while pending:
            directory = pending.pop()
            if interrupted():
                return result
            if len(files) + len(directories) >= 128:
                return None
            entries, digest = directory_observation(directory, root=root)
            directories[directory] = digest
            for name, kind in entries:
                if interrupted():
                    return result
                path = directory / name
                relative = path.relative_to(root)
                if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts):
                    continue
                if kind == "dir":
                    pending.append(path)
                elif _eligible(relative, options):
                    if len(files) >= 128 or is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
                        return None
                    with path.open("rb") as stream:
                        raw = stream.read(4 * 1024 * 1024 - total + 1)
                    total += len(raw)
                    if total > 4 * 1024 * 1024 or b"\x00" in raw:
                        return None
                    raw.decode("utf-8")
                    files.append((path, raw))
    except (OSError, ValueError, UnicodeError):
        return None
    if interrupted():
        return result
    if not files:
        result.native_directory_observation = (files, directories)
        return result
    # Starting one rg process per file consumed nearly the entire deadline on
    # Windows. Stage the bounded captured bytes once and search them together.
    # Synthetic names preserve order and avoid stdin sentinels, long paths and
    # source aliases; only original source names may reach the tool result.
    try:
        with tempfile.TemporaryDirectory(prefix="minicode-search-") as temporary:
            captured_root = Path(temporary)
            source_names = {}
            for index, (path, raw) in enumerate(sorted(files)):
                if interrupted():
                    return result
                name = f"{index:04d}"
                (captured_root / name).write_bytes(raw)
                source_names[name] = path.relative_to(root).as_posix()

            def collect(event):
                if event["type"] in {"match", "context"}:
                    name = Path(_json_text(event["data"]["path"])).as_posix()
                    if name not in source_names:
                        raise ValueError("snapshot search returned an unexpected path")
                    event = {**event, "data": {**event["data"], "path": {"text": source_names[name]}}}
                return _collect_event(result, root, options, event)

            if interrupted():
                return result
            receipt = _ripgrep.search(
                _command(executable, ".", {**options, "include": [], "exclude": []}),
                captured_root, collect,
                timeout_seconds=SEARCH_TIMEOUT_SECONDS - (time.monotonic() - started),
                cancellation_event=cancellation,
            )
    except OSError as error:
        result.receipt = ToolResult(ok=False, output=f"Cannot search captured source: {error}")
        return result
    _apply_receipt(result, receipt)
    if result.receipt.ok and receipt.stop_reason is None and not result.truncated and not receipt.stderr:
        result.native_directory_observation = (files, directories)
    return result
