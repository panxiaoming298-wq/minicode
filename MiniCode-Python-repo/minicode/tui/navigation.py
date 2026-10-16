from __future__ import annotations

from typing import Any
import re

from minicode.cli_commands import SLASH_COMMANDS
from minicode.tui.chrome import _cached_terminal_size
from minicode.tui.state import ScreenState, TtyAppArgs
from minicode.tui.chrome import get_permission_prompt_max_scroll_offset
from minicode.tui.transcript import get_transcript_max_scroll_offset


_HEADER_LINES_ESTIMATE = 11
_PROMPT_LINES_ESTIMATE = 7
_FOOTER_LINES = 1
_GAPS = 3
_TRANSCRIPT_FRAME_LINES = 4


def _get_transcript_body_lines(args: TtyAppArgs, state: ScreenState) -> int:
    _, rows = _cached_terminal_size()
    rows = max(24, rows)
    chrome_overhead = (
        _HEADER_LINES_ESTIMATE
        + _PROMPT_LINES_ESTIMATE
        + _FOOTER_LINES
        + _GAPS
        + _TRANSCRIPT_FRAME_LINES
    )
    return max(6, rows - chrome_overhead)


def _get_max_transcript_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    return get_transcript_max_scroll_offset(
        state.transcript,
        _get_transcript_body_lines(args, state),
        state.transcript_revision,
    )


def _scroll_transcript_by(args: TtyAppArgs, state: ScreenState, delta: int) -> bool:
    max_offset = _get_max_transcript_scroll_offset(args, state)
    next_offset = max(0, min(max_offset, state.transcript_scroll_offset + delta))
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def _jump_transcript_to_edge(args: TtyAppArgs, state: ScreenState, target: str) -> bool:
    next_offset = _get_max_transcript_scroll_offset(args, state) if target == "top" else 0
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    return True


def _scroll_pending_approval_by(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or not pending.details_expanded:
        return False
    max_offset = get_permission_prompt_max_scroll_offset(pending.request, expanded=True)
    next_offset = max(0, min(max_offset, pending.details_scroll_offset + delta))
    if next_offset == pending.details_scroll_offset:
        return False
    pending.details_scroll_offset = next_offset
    return True


def _toggle_pending_approval_expand(state: ScreenState) -> bool:
    pending = state.pending_approval
    if not pending or pending.request.get("kind") != "edit":
        return False
    pending.details_expanded = not pending.details_expanded
    pending.details_scroll_offset = 0
    return True


def _move_pending_approval_selection(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or pending.feedback_mode:
        return False
    total = len(pending.request.get("choices", []))
    if total <= 0:
        return False
    pending.selected_choice_index = (pending.selected_choice_index + delta + total) % total
    return True


def _history_up(state: ScreenState) -> bool:
    if not state.history or state.history_index <= 0:
        return False
    if state.history_index == len(state.history):
        state.history_draft = state.input
    state.history_index -= 1
    state.input = state.history[state.history_index] if state.history_index < len(state.history) else ""
    state.cursor_offset = len(state.input)
    return True


def _history_down(state: ScreenState) -> bool:
    if state.history_index >= len(state.history):
        return False
    state.history_index += 1
    state.input = (
        state.history_draft
        if state.history_index == len(state.history)
        else (state.history[state.history_index] if state.history_index < len(state.history) else "")
    )
    state.cursor_offset = len(state.input)
    return True


def _get_visible_commands(input_text: str, cwd: str | None = None, state: ScreenState | None = None) -> list[Any]:
    if not input_text.startswith("/"):
        if state is not None:
            state.skill_completion_workspace = None
            state.slash_dismissed_input = None
        return []
    if state is not None and state.slash_dismissed_input == input_text:
        return []
    # Once arguments start, leave cursor editing and Enter to the normal input flow.
    explicit = re.fullmatch(r"/skill[ \t]+([^\s]*)", input_text, re.IGNORECASE)
    if re.match(r"/skill\s", input_text, re.IGNORECASE) and explicit is None:
        if state is not None:
            state.skill_completion_workspace = None
        return []
    shorthand = re.fullmatch(r"/([^\s]*)", input_text)
    query = explicit.group(1) if explicit else ("" if input_text == "/skill" else shorthand.group(1) if shorthand else None)
    skills = []
    if cwd and query is not None:
        if state is not None and state.skill_completion_workspace == cwd:
            skills = state.skill_completion_commands
        else:
            from Main.MinicodeFrontline.Src.Application.Entry.LocalCommandSurface import SlashCommand
            from minicode.skills import discover_skills
            skills = [SlashCommand(f"/skill {s.name}", f"/skill {s.name}", s.description or "使用此技能处理任务")
                      for s in discover_skills(cwd) if s.user_invocable and not s.error
                      and re.fullmatch(r"[^\s/\\]+", s.name)]
            if state is not None:
                state.skill_completion_workspace = cwd
                state.skill_completion_commands = skills
        needle = query.casefold()
        skills = [s for s in skills if needle in s.name.removeprefix("/skill ").casefold()]
        skills.sort(key=lambda s: (not s.name.removeprefix("/skill ").casefold().startswith(needle), s.name.casefold()))
    if explicit:
        return skills
    commands = [cmd for cmd in SLASH_COMMANDS if cmd.usage.casefold().startswith(input_text.casefold())]
    exact = [cmd for cmd in commands if cmd.name == input_text]
    return exact + skills + [cmd for cmd in commands if cmd not in exact]
