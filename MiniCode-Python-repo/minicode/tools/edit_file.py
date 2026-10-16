"""Edit file tool with precise string matching, inspired by Claude Code's Edit tool.

Key features:
- Exact string match with line/whitespace-aware comparison
- Multiple match detection and disambiguation
- Fuzzy whitespace matching (tabs vs spaces, trailing whitespace)
- Line number diagnostics on mismatch
- Replace-all mode
"""
from __future__ import annotations

import difflib

from minicode.file_review import apply_reviewed_file_change, load_existing_file
from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path
from minicode.tools._validation import normalize_aliases, validate_contract


# ---------------------------------------------------------------------------
# String matching helpers
# ---------------------------------------------------------------------------

def _normalize_line(line: str) -> str:
    """Normalize a line for fuzzy matching: strip trailing whitespace, normalize tabs."""
    return line.rstrip().replace("\t", "    ")


def _find_exact_match(content: str, search: str, fuzzy: bool = False) -> list[tuple[int, int]]:
    """Find all occurrences of search in content.
    
    Returns list of (start_char_offset, end_char_offset) tuples.
    
    Args:
        content: The file content to search in
        search: The search string
        fuzzy: If True, use whitespace-fuzzy matching (normalize tabs/trailing whitespace)
    """
    if not search:
        return []
    
    if not fuzzy:
        # Exact matching
        results = []
        start = 0
        while True:
            idx = content.find(search, start)
            if idx == -1:
                break
            results.append((idx, idx + len(search)))
            start = idx + 1
        return results
    
    # Fuzzy matching: compare line-by-line with normalized whitespace
    search_lines = search.split("\n")
    content_lines = content.split("\n")
    results = []
    
    for i in range(len(content_lines) - len(search_lines) + 1):
        match = True
        for j, search_line in enumerate(search_lines):
            if _normalize_line(content_lines[i + j]) != _normalize_line(search_line):
                match = False
                break
        if match:
            # Calculate character offsets from line positions
            char_start = sum(len(line) + 1 for line in content_lines[:i])
            char_end = char_start + len("\n".join(content_lines[i:i + len(search_lines)]))
            results.append((char_start, char_end))
    
    return results


def _describe_indentation(indent: str) -> str:
    if not indent:
        return "no leading whitespace"
    return ", ".join(f"{count} {label}{'s' if count != 1 else ''}"
                     for char, label in ((" ", "space"), ("\t", "tab"))
                     if (count := indent.count(char)))


def _format_mismatch_diagnostic(content: str, search: str) -> str:
    """Produce a helpful diagnostic when search string is not found.
    
    Shows the closest match with line numbers and diff hints.
    """
    search_lines = search.split("\n")
    content_lines = content.split("\n")
    
    # Ignore indentation ONLY when ranking diagnostic candidates. Actual edits
    # still use _find_exact_match, including its original whitespace rules.
    search_text = [line.strip() for line in search_lines]
    content_text = [line.strip() for line in content_lines]
    best_score = (0.0, 0.0)
    best_start = -1
    alternatives = []
    
    # Search in a sliding window
    window_size = len(search_lines)
    for i in range(max(1, len(content_lines) - window_size + 1)):
        window = content_lines[i:i + window_size]
        if len(window) != window_size:
            continue
        text_ratio = difflib.SequenceMatcher(
            None, content_text[i:i + window_size], search_text, autojunk=False,
        ).ratio() if any(search_text) else 0.0
        exact_ratio = difflib.SequenceMatcher(None, window, search_lines, autojunk=False).ratio()
        score = (text_ratio, exact_ratio)
        if score > best_score:
            best_score = score
            best_start = i
            alternatives = []
        elif score == best_score and best_start >= 0 and len(alternatives) < 3:
            alternatives.append(i + 1)
    
    lines = ["Search string not found in file."]
    
    if best_start >= 0 and best_score[0] > 0.3:
        lines.append("")
        lines.append(f"Closest text at line {best_start + 1} (ignoring surrounding whitespace for diagnosis only):")
        if alternatives:
            lines.append("Other equally close regions start at lines: " + ", ".join(map(str, alternatives))
                         + ". Read more context to identify the intended location.")
        lines.append("")
        
        # Show the closest match with line numbers
        for j, line in enumerate(content_lines[best_start:best_start + window_size]):
            line_num = best_start + 1 + j
            prefix = "  "
            # Check if this line matches the search line
            if j < len(search_lines):
                if line != search_lines[j]:
                    prefix = ">>"
            lines.append(f"{prefix} {line_num:4d} | {line}")
        
        # Show diff hint
        lines.extend(["", "Hints:"])
        for j, (expected, actual) in enumerate(zip(search_lines, content_lines[best_start:best_start + window_size])):
            if expected == actual:
                continue
            old_indent = expected[:len(expected) - len(expected.lstrip(" \t"))]
            file_indent = actual[:len(actual) - len(actual.lstrip(" \t"))]
            if old_indent != file_indent:
                lines.append(f"  Line {best_start + 1 + j}: indentation mismatch: old has {_describe_indentation(old_indent)}; "
                             f"file has {_describe_indentation(file_indent)}.")
            lines.append(f"  Line {best_start + 1 + j}: old={expected!r}; file={actual!r}")
    else:
        # No close match found, show first few lines of file for context
        lines.append("")
        lines.append(f"File has {len(content_lines)} lines. First 10 lines:")
        for i, line in enumerate(content_lines[:10]):
            lines.append(f"  {i + 1:4d} | {line}")
    
    lines.extend(["", "No changes were made. Read the intended region and copy its exact text into old, "
                  "without display line numbers. fuzzy=true normalizes tabs and trailing whitespace only; "
                  "it does not ignore leading indentation. Do not repeat the unchanged mismatch."])
    return "\n".join(lines)


def _validate(input_data: dict) -> dict:
    input_data = normalize_aliases(input_data, {"search": "old", "replace": "new", "replaceAll": "replace_all"})
    validate_contract(input_data, EDIT_FILE_INPUT)
    path = input_data.get("path")
    search = input_data["old"]
    replace = input_data["new"]
    replace_all = input_data.get("replace_all", False)
    fuzzy = input_data.get("fuzzy", False)
    
    # Normalize \r\n → \n so that search/replace strings provided by the
    # model always match the file content (read_text uses universal newlines).
    search = search.replace("\r\n", "\n")
    replace = replace.replace("\r\n", "\n")
    
    return {
        "path": path,
        "search": search,
        "replace": replace,
        "replace_all": replace_all,
        "fuzzy": fuzzy,
    }


def _run(input_data: dict, context) -> ToolResult:
    from minicode.tools.write_file import _enforce_read_obligation

    refusal = _enforce_read_obligation(input_data, context)
    if refusal is not None:
        return refusal
    target = resolve_tool_path(context, input_data["path"], "write")
    content = load_existing_file(target)
    
    # Try exact matching first
    matches = _find_exact_match(content, input_data["search"], fuzzy=False)
    
    # If no exact match, try fuzzy matching (whitespace-tolerant)
    if not matches and input_data.get("fuzzy", False):
        matches = _find_exact_match(content, input_data["search"], fuzzy=True)
    
    if not matches:
        # No match found — provide helpful diagnostic
        diagnostic = _format_mismatch_diagnostic(content, input_data["search"])
        return ToolResult(ok=False, output=diagnostic)
    
    if len(matches) > 1 and not input_data["replace_all"]:
        # Multiple matches — report them with line numbers
        content_lines = content.split("\n")
        match_lines = []
        for start_offset, _ in matches:
            # Find line number from char offset
            char_count = 0
            for line_num, line in enumerate(content_lines, start=1):
                if char_count + len(line) + 1 > start_offset:
                    match_lines.append(line_num)
                    break
                char_count += len(line) + 1
        
        return ToolResult(
            ok=False,
            output=(
                f"Found {len(matches)} matches for the search string. "
                f"Use replace_all=true to replace all occurrences, or provide more context to make the match unique.\n"
                f"Matches at lines: {', '.join(str(line) for line in match_lines)}"
            ),
        )
    
    # Apply replacement
    if input_data["replace_all"]:
        # Replace from end to start to preserve offsets
        next_content = content
        for start_offset, end_offset in reversed(matches):
            next_content = next_content[:start_offset] + input_data["replace"] + next_content[end_offset:]
    else:
        start_offset, end_offset = matches[0]
        next_content = content[:start_offset] + input_data["replace"] + content[end_offset:]
    
    return apply_reviewed_file_change(context, input_data["path"], target, next_content)


EDIT_FILE_INPUT = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1, "description": "File path to edit"},
            "old": {"type": "string", "minLength": 1, "description": "Text to find (exact match required)"},
            "new": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default: false)"},
            "fuzzy": {"type": "boolean", "description": "Normalize tabs and trailing whitespace only; leading indentation must still match (default: false)"},
        },
        "required": ["path", "old", "new"],
    }


edit_file_tool = ToolDefinition(
    name="edit_file",
    description=(
        "Replace a substring in a file using path, old and new. The old string must match exactly (including whitespace and indentation). "
        "If it appears multiple times, provide more surrounding context or set replace_all=true. "
        "Use JSON booleans true/false, never strings. On mismatch, a diagnostic shows the closest match and line numbers. "
        "Legacy search/replace/replaceAll names are accepted, but must not conflict with old/new/replace_all."
    ),
    input_schema=EDIT_FILE_INPUT,
    validator=_validate,
    run=_run,
)
