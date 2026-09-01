from __future__ import annotations

from .chrome import (
    RESET, DIM, BOLD, ITALIC, HIGHLIGHT_BG,
    BRIGHT_GREEN,
)
from .theme import theme


def render_input_prompt(current_input: str, cursor_offset: int, compact: bool = False) -> str:
    """Render the input prompt line(s), supports multi-line via Ctrl+J.

    Format matches the Rust version:
      mini-code> <input with cursor>
    Multi-line input renders each line with a continuation prefix.
    """
    t = theme()
    offset = max(0, min(cursor_offset, len(current_input)))
    prefix = f"{t.input}{BOLD}mini-code>{RESET} "
    cont_prefix = f"{t.subtle}          {RESET}"

    if '\n' in current_input:
        # Multi-line: split and find which line the cursor is on
        lines = current_input.split('\n')
        rendered = []
        pos = 0
        for li, line in enumerate(lines):
            is_last = li == len(lines) - 1
            pfx = prefix if li == 0 else cont_prefix
            if pos <= offset < pos + len(line) + (0 if is_last else 1):
                # Cursor is in this line
                col = offset - pos
                before = line[:col]
                cur = line[col] if col < len(line) else " "
                after = line[col + 1:]
                rendered.append(f" {pfx}{before}{HIGHLIGHT_BG}{BRIGHT_GREEN}{cur}{RESET}{after}")
            else:
                rendered.append(f" {pfx}{line}")
            pos += len(line) + 1  # +1 for the \n
        input_line = "\n".join(rendered)
    else:
        before = current_input[:offset]
        current = current_input[offset] if offset < len(current_input) else " "
        after = current_input[offset + 1:]
        placeholder = (
            "" if current_input
            else f"{ITALIC} Type a message or /help for commands{RESET}"
        )
        input_line = f" {prefix}{before}{HIGHLIGHT_BG}{BRIGHT_GREEN}{current}{RESET}{after}{DIM}{placeholder}{RESET}"

    if compact:
        return input_line

    key_enter = f"{t.subtle}[{RESET}{DIM}Enter{RESET}{t.subtle}]{RESET} {t.subtle}send{RESET}"
    key_newline = f"{t.subtle}[{RESET}{DIM}^J{RESET}{t.subtle}]{RESET} {t.subtle}nl{RESET}"
    key_help = f"{t.subtle}[{RESET}{DIM}/help{RESET}{t.subtle}]{RESET} {t.subtle}cmds{RESET}"
    key_esc = f"{t.subtle}[{RESET}{DIM}Esc{RESET}{t.subtle}]{RESET} {t.subtle}clear{RESET}"
    key_exit = f"{t.subtle}[{RESET}{DIM}^C{RESET}{t.subtle}]{RESET} {t.subtle}exit{RESET}"

    line1 = f"  {key_enter}  {key_newline}  {key_help}  {key_esc}  {key_exit}"
    line2 = ""

    return "\n".join([line1, line2, input_line])
