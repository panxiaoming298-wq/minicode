import pytest

from minicode.tui import render_banner, render_panel, render_permission_prompt, render_transcript
from minicode.tui.types import TranscriptEntry


def test_render_panel_contains_title() -> None:
    rendered = render_panel("Demo", "body")
    assert "Demo" in rendered
    assert "body" in rendered


def test_render_banner_includes_model() -> None:
    rendered = render_banner(
        {"model": "claude-test", "baseUrl": "https://api.anthropic.com"},
        "/tmp/demo",
        ["cwd: /tmp/demo"],
        {"transcriptCount": 1, "messageCount": 2, "skillCount": 3, "mcpCount": 4},
    )
    assert "claude-test" in rendered
    assert "api.anthropic.com" in rendered


def test_render_transcript_shows_tool_entry() -> None:
    transcript = [
        TranscriptEntry(id=1, kind="user", body="hi"),
        TranscriptEntry(id=2, kind="tool", body="done", toolName="read_file", status="success"),
    ]
    rendered = render_transcript(transcript, scroll_offset=0)
    assert "read_file" in rendered
    assert "ok" in rendered


def test_render_transcript_shows_intermediate_collapse_phase() -> None:
    transcript = [
        TranscriptEntry(
            id=1,
            kind="tool",
            body="full output here",
            toolName="run_command",
            status="success",
            collapsePhase=1,
        ),
    ]

    rendered = render_transcript(transcript, scroll_offset=0)

    assert "run_command" in rendered
    assert "collapsing" in rendered


def test_render_transcript_shows_collapsed_summary_when_fully_collapsed() -> None:
    transcript = [
        TranscriptEntry(
            id=1,
            kind="tool",
            body="full output here",
            toolName="run_command",
            status="success",
            collapsed=True,
            collapsedSummary="short summary",
            collapsePhase=3,
        ),
    ]

    rendered = render_transcript(transcript, scroll_offset=0)

    assert "run_command" in rendered
    assert "short summary" in rendered
    assert "full output here" not in rendered


def test_render_permission_prompt_lists_choices() -> None:
    rendered = render_permission_prompt(
        {
            "summary": "Need approval",
            "details": ["target: demo.txt"],
            "choices": [{"key": "1", "label": "allow once"}],
        }
    )
    assert "Need approval" in rendered
    assert "allow once" in rendered


# ---------------------------------------------------------------------------
# Windows alternate-screen fix (GitHub issue #7, point 2)
# ---------------------------------------------------------------------------


def test_is_dumb_terminal_false_on_windows_with_empty_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty TERM on Windows must NOT disable the alternate screen buffer.

    Otherwise every redraw frame accumulates in the scrollback and shows as
    stacked/garbled frames when scrolling up (GitHub issue #7, point 2).
    """
    from minicode.tui import screen

    monkeypatch.setattr(screen.sys, "platform", "win32")
    monkeypatch.setattr(screen.sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("TERM", raising=False)

    assert screen._is_dumb_terminal() is False


def test_is_dumb_terminal_true_when_output_is_piped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minicode.tui import screen

    monkeypatch.setattr(screen.sys.stdout, "isatty", lambda: False)

    assert screen._is_dumb_terminal() is True


def test_is_dumb_terminal_true_for_explicitly_limited_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minicode.tui import screen

    for term in ("dumb", "linux"):
        monkeypatch.setattr(screen.sys.stdout, "isatty", lambda: True)
        monkeypatch.setenv("TERM", term)
        assert screen._is_dumb_terminal() is True, term


# ---------------------------------------------------------------------------
# /collapse slash command (TS parity) — collapses expanded tool-output blocks
# ---------------------------------------------------------------------------


def test_collapse_command_collapses_tool_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import minicode.tui.input_handler as input_handler_module
    from minicode.permissions import PermissionManager
    from minicode.tui.state import ScreenState, TtyAppArgs
    from minicode.tui.types import TranscriptEntry
    from minicode.tooling import ToolRegistry

    # Avoid touching the real on-disk history file during this unit test.
    monkeypatch.setattr(input_handler_module, "save_history_entries", lambda hist: None)

    state = ScreenState()
    state.transcript = [
        TranscriptEntry(id=1, kind="tool", body="big output", toolName="read_file", status="success"),
        TranscriptEntry(id=2, kind="assistant", body="hi"),
        TranscriptEntry(id=3, kind="tool", body="more", toolName="grep_files", status="success", collapsed=True),
    ]
    args = TtyAppArgs(
        runtime={},
        tools=ToolRegistry([]),
        model=object(),
        messages=[],
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
    )

    ret = input_handler_module._handle_input(args, state, lambda: None, submitted_raw_input="/collapse")

    assert ret is False
    tools = [e for e in state.transcript if e.kind == "tool"]
    # Both tool entries are now collapsed (one was already, one got collapsed).
    assert all(e.collapsed for e in tools)
    assert sum(1 for e in tools if e.collapsed) == 2
