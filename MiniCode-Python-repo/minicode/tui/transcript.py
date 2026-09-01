from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

from .chrome import (
    _cached_terminal_size,
    RESET,
    DIM,
    ICON_DIVIDER,
    ICON_DOT,
    _looks_like_diff_block,
    colorize_unified_diff_block,
)
from .markdown import render_markdownish
from .theme import theme
from .types import TranscriptEntry

# Pre-build the separator string once (immutable)
_SEPARATOR = f"  {DIM}{ICON_DOT} {ICON_DIVIDER * 3} {ICON_DOT}{RESET}"
_SEPARATOR_LINES = ["", _SEPARATOR, ""]
_SEPARATOR_LINE_COUNT = 3

# Tool names that produce diff output
_DIFF_TOOLS = frozenset({"edit_file", "patch_file", "diff_viewer"})

# Tool output preview limits (match Rust TOOL_PREVIEW_LINES / TOOL_PREVIEW_CHARS)
_TOOL_PREVIEW_LINES = 6
_TOOL_PREVIEW_CHARS = 180


# ---------------------------------------------------------------------------
# Visual-line wrapping (port of TS charDisplayWidth / stringDisplayWidth /
# wrapPanelBodyLine). The transcript scroll offset must count width-wrapped
# VISUAL rows, not just newline-split logical lines, otherwise long lines make
# the scrollbar/scroll offset under-count (TS parity).
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _char_display_width(ch: str) -> int:
    """Display column width of one character (2 for CJK/fullwidth/emoji)."""
    code = ord(ch)
    if 0x1100 <= code <= 0x115F or code in (0x2329, 0x232A):
        return 2
    if 0x2E80 <= code <= 0xA4CF and code != 0x303F:
        return 2
    if 0xAC00 <= code <= 0xD7A3:
        return 2
    if 0xF900 <= code <= 0xFAFF:
        return 2
    if 0xFE10 <= code <= 0xFE19 or 0xFE30 <= code <= 0xFE6F:
        return 2
    if 0xFF00 <= code <= 0xFF60 or 0xFFE0 <= code <= 0xFFE6:
        return 2
    if 0x1F300 <= code <= 0x1FAF6:
        return 2
    if 0x20000 <= code <= 0x3FFFD:
        return 2
    return 1


def _string_display_width(text: str) -> int:
    return sum(_char_display_width(ch) for ch in _strip_ansi(text))


def _transcript_panel_width() -> int:
    cols, _ = _cached_terminal_size()
    return max(60, cols)


def _wrap_panel_body_line(line: str, width: int) -> list[str]:
    """Wrap a rendered line to ``width`` display columns (port of TS
    ``wrapPanelBodyLine``).

    Uses ``inner = width - 4`` to leave room for panel padding; greedy-packs by
    character display width. Lines that already fit are returned unchanged
    (ANSI styling preserved). Lines that overflow are returned as wrapped plain
    segments (matching TS, which wraps the stripped text).
    """
    inner = max(0, width - 4)
    if inner <= 0:
        return [""]
    if _string_display_width(line) <= inner:
        return [line]
    parts: list[str] = []
    current = ""
    current_width = 0
    for ch in _strip_ansi(line):
        ch_width = _char_display_width(ch)
        if current_width + ch_width > inner:
            parts.append(current)
            current = ch
            current_width = ch_width
            continue
        current += ch
        current_width += ch_width
    if current:
        parts.append(current)
    return parts


def _is_runtime_progress_message(text: str) -> bool:
    normalized = " ".join((text or "").split()).lower()
    runtime_prefixes = (
        "runtime phase:",
        "verification guard:",
        "compacted context for the current runtime phase.",
        "depth stalled;",
    )
    if normalized.startswith(runtime_prefixes):
        return True
    return (
        "widened mode is active" in normalized
        or "widening is now available" in normalized
        or "escalation trigger:" in normalized
    )


def _is_runtime_entry(entry: TranscriptEntry) -> bool:
    return entry.category == "runtime" or _is_runtime_progress_message(entry.body)


def _runtime_label_text(entry: TranscriptEntry) -> str:
    runtime_kind = (entry.runtimeKind or "").strip()
    return f"runtime:{runtime_kind}" if runtime_kind else "runtime"


def _runtime_meta_suffix(entry: TranscriptEntry) -> str:
    if not _is_runtime_entry(entry):
        return ""

    meta_parts: list[str] = []
    if entry.runtimeStep is not None:
        meta_parts.append(f"step={entry.runtimeStep}")
    if entry.runtimePhase:
        meta_parts.append(f"phase={entry.runtimePhase}")
    if entry.runtimeStopReason:
        meta_parts.append(f"reason={entry.runtimeStopReason}")
    if entry.runtimeVerificationFocus:
        meta_parts.append(f"verify={entry.runtimeVerificationFocus}")
    return f" [{' '.join(meta_parts)}]" if meta_parts else ""


def _runtime_trace_token(entry: TranscriptEntry) -> str | None:
    if not _is_runtime_entry(entry):
        return None

    step_suffix = f"@{entry.runtimeStep}" if entry.runtimeStep is not None else ""
    runtime_kind = (entry.runtimeKind or "").strip().lower()

    if runtime_kind == "phase":
        detail = (entry.runtimePhase or "unknown").strip() or "unknown"
        return f"phase:{detail}{step_suffix}"
    if runtime_kind == "guard":
        detail = (
            (entry.runtimeVerificationFocus or "").strip()
            or (entry.runtimeStopReason or "").strip()
            or "verification"
        )
        return f"guard:{detail}{step_suffix}"
    if runtime_kind == "widening":
        detail = (entry.runtimeStopReason or "").strip() or "escalation"
        return f"widen:{detail}{step_suffix}"
    if runtime_kind == "stop":
        detail = (entry.runtimeStopReason or "").strip() or "done"
        return f"stop:{detail}{step_suffix}"
    if runtime_kind == "compaction":
        detail = (entry.runtimePhase or "").strip() or "context"
        return f"compact:{detail}{step_suffix}"
    if runtime_kind == "recovery":
        detail = (entry.runtimeStopReason or "").strip() or "resume"
        return f"recover:{detail}{step_suffix}"

    return f"{runtime_kind or 'runtime'}{step_suffix}"


def _runtime_trace_summary(entries: list[TranscriptEntry]) -> str | None:
    trace_tokens: list[str] = []
    for entry in entries:
        token = _runtime_trace_token(entry)
        if token and (not trace_tokens or trace_tokens[-1] != token):
            trace_tokens.append(token)
    if not trace_tokens:
        return None
    return " -> ".join(trace_tokens)


def format_runtime_summary_line(entries: list[TranscriptEntry]) -> str | None:
    runtime_summary = _runtime_trace_summary(entries)
    if not runtime_summary:
        return None
    return f"runtime-summary: {runtime_summary}"


def _indent_block(text: str, prefix: str = "  ") -> str:
    """Indent all lines in a block of text."""
    return "\n".join(prefix + line for line in text.split("\n"))


def preview_tool_body(tool_name: str, body: str) -> str:
    """Truncate tool output based on tool name and content size."""
    max_chars = 1000 if tool_name == "read_file" else 1800
    max_lines = 20 if tool_name == "read_file" else 36

    lines = body.split("\n")
    limited_lines = lines[:max_lines] if len(lines) > max_lines else lines
    limited = "\n".join(limited_lines)

    if len(limited) > max_chars:
        limited = limited[:max_chars] + "..."

    if limited != body:
        return f"{limited}\n{DIM}... output truncated in transcript{RESET}"

    return limited


def _render_transcript_entry(entry: TranscriptEntry) -> str:
    """Render a single TranscriptEntry with Morandi theme colors."""
    t = theme()

    if entry.kind == "user":
        label = f"{t.user}{t.bold}▶ you{t.reset}"
        return f"{label}\n{_indent_block(entry.body)}"

    if entry.kind == "assistant":
        label = f"{t.assistant}{t.bold}▶ assistant{t.reset}"
        return f"{label}\n{_indent_block(render_markdownish(entry.body))}"

    if entry.kind == "progress":
        label_text = (
            _runtime_label_text(entry)
            if _is_runtime_entry(entry)
            else "progress"
        )
        label = f"{t.progress}{t.bold}▶ {label_text}{t.reset}"
        meta_suffix = _runtime_meta_suffix(entry)
        return f"{label}{meta_suffix}\n{_indent_block(render_markdownish(entry.body))}"

    if entry.kind == "tool":
        if entry.status == "running":
            status_label = f"{t.tool}{ICON_DOT} running{t.reset}"
        elif entry.status == "success":
            status_label = f"{t.assistant}ok{t.reset}"
        else:
            status_label = f"{t.tool_error}err{t.reset}"

        tool_name_display = f"{t.tool}{t.bold}{entry.toolName}{t.reset}"

        body_lines = entry.body.split("\n") if entry.body else []
        total_lines = len(body_lines)
        collapsible_by_lines = total_lines > _TOOL_PREVIEW_LINES
        collapsible_by_chars = any(
            len(ln) > _TOOL_PREVIEW_CHARS for ln in body_lines[:_TOOL_PREVIEW_LINES]
        )
        is_collapsed = entry.collapsed or entry.collapsePhase == 3
        is_collapsing = entry.collapsePhase in (1, 2)
        can_toggle = collapsible_by_lines or collapsible_by_chars or is_collapsing

        if can_toggle:
            if is_collapsing:
                toggle_text = f"  {t.expandable}{t.bold}[collapsing]{t.reset}"
            else:
                toggle_text = (
                    f"  {t.expandable}{t.bold}[收起]{t.reset}"
                    if not is_collapsed
                    else f"  {t.expandable}{t.bold}[展开]{t.reset}"
                )
        else:
            toggle_text = ""

        label = (
            f"{t.tool}{t.bold}▶ tool{t.reset} {tool_name_display}"
            f" {status_label}{toggle_text}"
        )

        if entry.status == "running":
            body = entry.body
        elif is_collapsing:
            body = _render_tool_body(entry, body_lines, total_lines, collapsible_by_lines, is_collapsed)
        elif is_collapsed:
            summary = entry.collapsedSummary or "output collapsed"
            body = f"{t.subtle}{t.italic}{summary}{t.reset}"
        else:
            body = _render_tool_body(entry, body_lines, total_lines, collapsible_by_lines, is_collapsed)

        return f"{label}\n{_indent_block(body)}"

    return ""


def _render_tool_body(entry, body_lines, total_lines, collapsible, is_collapsed):
    """Render tool body with diff coloring for edit/patch/diff tools."""
    t = theme()
    body = entry.body

    if entry.toolName in _DIFF_TOOLS and _looks_like_diff_block(body):
        colored = colorize_unified_diff_block(body)
        if collapsible and not is_collapsed:
            preview = "\n".join(colored.split("\n")[:_TOOL_PREVIEW_LINES])
            hidden = max(0, total_lines - _TOOL_PREVIEW_LINES)
            return preview + (f"\n{t.subtle}  ... {hidden} more lines{t.reset}" if hidden > 0 else "")
        return colored

    if collapsible and not is_collapsed:
        preview = "\n".join(body_lines[:_TOOL_PREVIEW_LINES])
        hidden = max(0, total_lines - _TOOL_PREVIEW_LINES)
        return preview_tool_body(entry.toolName or "", render_markdownish(preview)) + (
            f"\n{t.subtle}  ... {hidden} more lines{t.reset}" if hidden > 0 else ""
        )
    return preview_tool_body(entry.toolName or "", render_markdownish(body))


def get_transcript_window_size(window_size: int | None = None) -> int:
    if window_size is not None:
        return max(4, window_size)
    _, rows = _cached_terminal_size()
    return max(8, rows - 15)


@dataclass(slots=True)
class TranscriptLayout:
    revision: int
    total_lines: int
    entry_line_starts: list[int]
    entry_line_counts: list[int]


_EntryCacheKey = tuple[
    str,
    str,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
    str | None,
    bool,
    int | None,
    str | None,
    str | None,
]
_entry_cache: dict[_EntryCacheKey, list[str]] = {}
_line_count_cache: dict[_EntryCacheKey, int] = {}
_LayoutCacheKey = tuple[int, int, int, int]
_layout_cache: dict[_LayoutCacheKey, TranscriptLayout] = {}
_CACHE_MAX_SIZE = 500
_LAYOUT_CACHE_MAX_SIZE = 64


def _entry_cache_key(entry: TranscriptEntry) -> _EntryCacheKey:
    """Build a collision-free key from entry render-affecting state."""
    return (
        entry.kind,
        entry.body,
        entry.category,
        entry.runtimeKind,
        entry.runtimeStep,
        entry.runtimePhase,
        entry.runtimeStopReason,
        entry.runtimeVerificationFocus,
        entry.status,
        entry.collapsed,
        entry.collapsePhase,
        entry.collapsedSummary,
        entry.toolName,
    )


def _get_entry_lines(entry: TranscriptEntry) -> list[str]:
    content_key = _entry_cache_key(entry)
    width = _transcript_panel_width()
    cache_key = (content_key, width)

    cached = _entry_cache.get(cache_key)
    if cached is not None:
        return cached

    # Render logical lines, then wrap each to the panel width so the entry's
    # line count reflects on-screen visual rows (TS parity).
    logical = _render_transcript_entry(entry).split("\n")
    lines: list[str] = []
    for logical_line in logical:
        lines.extend(_wrap_panel_body_line(logical_line, width))

    if len(_entry_cache) > _CACHE_MAX_SIZE:
        keys = list(_entry_cache.keys())
        for k in keys[: len(keys) // 2]:
            del _entry_cache[k]
            _line_count_cache.pop(k, None)

    _entry_cache[cache_key] = lines
    return lines


def _get_entry_line_count(entry: TranscriptEntry) -> int:
    content_key = _entry_cache_key(entry)
    width = _transcript_panel_width()
    cache_key = (content_key, width)

    cached_lc = _line_count_cache.get(cache_key)
    if cached_lc is not None:
        return cached_lc

    cached_full = _entry_cache.get(cache_key)
    if cached_full is not None:
        count = len(cached_full)
        _line_count_cache[cache_key] = count
        return count

    lines = _get_entry_lines(entry)
    count = len(lines)
    _line_count_cache[cache_key] = count
    return count


def _layout_cache_key(
    entries: list[TranscriptEntry],
    revision: int | None,
) -> _LayoutCacheKey | None:
    if revision is None:
        return None
    return (id(entries), revision, len(entries), _transcript_panel_width())


def _build_transcript_layout(
    entries: list[TranscriptEntry],
    revision: int | None,
) -> TranscriptLayout:
    cache_key = _layout_cache_key(entries, revision)
    if cache_key is not None:
        cached = _layout_cache.get(cache_key)
        if cached is not None:
            return cached

    entry_line_starts: list[int] = []
    entry_line_counts: list[int] = []
    current_line = 0

    for i, entry in enumerate(entries):
        if i > 0:
            current_line += _SEPARATOR_LINE_COUNT
        entry_line_starts.append(current_line)
        line_count = _get_entry_line_count(entry)
        entry_line_counts.append(line_count)
        current_line += line_count

    layout = TranscriptLayout(
        revision=revision or 0,
        total_lines=current_line,
        entry_line_starts=entry_line_starts,
        entry_line_counts=entry_line_counts,
    )

    if cache_key is not None:
        if len(_layout_cache) >= _LAYOUT_CACHE_MAX_SIZE:
            for key in list(_layout_cache.keys())[: len(_layout_cache) // 2]:
                del _layout_cache[key]
        _layout_cache[cache_key] = layout
    return layout


def _compute_total_lines(entries: list[TranscriptEntry], revision: int | None = None) -> int:
    if not entries:
        return 0
    return _build_transcript_layout(entries, revision).total_lines


def _render_visible_window(
    entries: list[TranscriptEntry],
    start_line: int,
    end_line: int,
    revision: int | None = None,
) -> list[str]:
    if not entries:
        return []

    layout = _build_transcript_layout(entries, revision)
    result: list[str] = []
    if not layout.entry_line_starts:
        return result

    start_index = bisect_left(layout.entry_line_starts, start_line)
    if start_index > 0:
        start_index -= 1

    for i in range(start_index, len(entries)):
        entry_start = layout.entry_line_starts[i]
        entry_line_count = layout.entry_line_counts[i]
        entry_end = entry_start + entry_line_count

        if i > 0:
            sep_start = entry_start - _SEPARATOR_LINE_COUNT
            sep_end = entry_start
            if sep_start < end_line and sep_end > start_line:
                vis_start = max(0, start_line - sep_start)
                vis_end = min(_SEPARATOR_LINE_COUNT, end_line - sep_start)
                result.extend(_SEPARATOR_LINES[vis_start:vis_end])

        if entry_start >= end_line:
            break

        if entry_start < end_line and entry_end > start_line:
            lines = _get_entry_lines(entries[i])
            vis_start = max(0, start_line - entry_start)
            vis_end = min(entry_line_count, end_line - entry_start)
            result.extend(lines[vis_start:vis_end])

    return result


def get_transcript_max_scroll_offset(
    entries: list[TranscriptEntry],
    window_size: int | None = None,
    revision: int | None = None,
) -> int:
    if not entries:
        return 0
    total = _compute_total_lines(entries, revision)
    ws = get_transcript_window_size(window_size)
    return max(0, total - ws)


def render_transcript(
    entries: list[TranscriptEntry],
    scroll_offset: int,
    window_size: int | None = None,
    revision: int | None = None,
) -> str:
    """Render a windowed view of the transcript. O(visible)."""
    t = theme()
    if not entries:
        return ""

    layout = _build_transcript_layout(entries, revision)
    total_lines = layout.total_lines
    ws = get_transcript_window_size(window_size)
    max_offset = max(0, total_lines - ws)
    offset = max(0, min(scroll_offset, max_offset))

    if offset == 0:
        end = total_lines
        start = max(0, end - ws)
        visible_lines = _render_visible_window(entries, start, end, revision)
        body = "\n".join(visible_lines)
        scrollbar = _render_scrollbar(offset, max_offset, len(visible_lines))
        return _interleave_scrollbar(body, scrollbar)

    content_ws = max(1, ws - 1)
    end = total_lines - offset
    start = max(0, end - content_ws)
    visible_lines = _render_visible_window(entries, start, end, revision)
    body = "\n".join(visible_lines)
    scrollbar = _render_scrollbar(offset, max_offset, len(visible_lines))

    indicator = (
        f"{body}\n"
        f"{t.subtle}  {ICON_DIVIDER * 2} scroll {offset}/{max_offset} "
        f"(PgUp/PgDn or scroll){ICON_DIVIDER * 2}{t.reset}"
    )
    return _interleave_scrollbar(indicator, scrollbar)


# 8-segment Unicode blocks for sub-character scrollbar precision
# ' ' (0/8) → '▏' (1/8) → '▎' (2/8) → '▍' (3/8) → '▌' (4/8) → '▋' (5/8) → '▊' (6/8) → '▉' (7/8) → '█' (8/8)
_SCROLLBAR_BLOCKS = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█']


def _render_scrollbar(offset: int, max_offset: int, height: int) -> list[str]:
    """Render a vertical scrollbar with 8-segment Unicode block precision."""
    if max_offset <= 0 or height < 3:
        return [" "] * max(1, height)
    # Thumb position with sub-character precision
    ratio = offset / max_offset
    precise_pos = ratio * (height - 1)
    whole = int(precise_pos)
    remainder = precise_pos - whole
    part_idx = int(remainder * 8)  # 0-8, map to _SCROLLBAR_BLOCKS

    bar = []
    for i in range(height):
        if i < whole:
            bar.append("░")  # above thumb
        elif i == whole:
            bar.append(_SCROLLBAR_BLOCKS[part_idx])  # partial block for smooth position
        elif i == whole + 1 and part_idx > 0:
            bar.append(_SCROLLBAR_BLOCKS[8 - part_idx])  # complementary block below thumb
        elif i == 0 and offset > 0:
            bar.append("▲")
        elif i == height - 1 and offset < max_offset:
            bar.append("▼")
        else:
            bar.append("░")
    return bar


def _interleave_scrollbar(body: str, scrollbar: list[str]) -> str:
    """Append scrollbar characters to each line of body."""
    lines = body.split("\n")
    result = []
    for i, line in enumerate(lines):
        if i < len(scrollbar):
            result.append(f"{line}{scrollbar[i]}")
        else:
            result.append(line)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# Legacy full-render API (backward compat)
# ---------------------------------------------------------------------------

def _render_transcript_lines(entries: list[TranscriptEntry]) -> list[str]:
    """Render all entries into lines with separators. Kept for backward compat."""
    all_lines: list[str] = []
    for i, entry in enumerate(entries):
        if i > 0:
            all_lines.extend(_SEPARATOR_LINES)
        all_lines.extend(_get_entry_lines(entry))
    return all_lines


def format_transcript_text(entries: list[TranscriptEntry]) -> str:
    """Format transcript entries as plain text (no ANSI) for file saving."""
    parts = []
    runtime_summary_line = format_runtime_summary_line(entries)
    if runtime_summary_line:
        parts.append(f"runtime-summary\n  {runtime_summary_line.removeprefix('runtime-summary: ')}")
    for entry in entries:
        if entry.kind == "user":
            label = "you"
        elif entry.kind == "progress" and _is_runtime_entry(entry):
            label = _runtime_label_text(entry) + _runtime_meta_suffix(entry)
        else:
            label = entry.kind
        if entry.kind == "tool":
            status_text = f" ({entry.status})" if entry.status else ""
            label = f"{entry.toolName or 'tool'}{status_text}"
        indented = "\n".join("  " + line for line in entry.body.splitlines())
        parts.append(f"{label}\n{indented}")
    return "\n\n---\n\n".join(parts)
