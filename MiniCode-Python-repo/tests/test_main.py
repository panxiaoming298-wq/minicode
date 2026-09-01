from __future__ import annotations

import codecs
import subprocess
import sys
from pathlib import Path

from minicode.main import (
    _handle_inspect_session_request,
    _handle_preview_rewind_request,
    _handle_replay_session_request,
)
from minicode.session import create_file_checkpoint, create_new_session


def test_handle_inspect_session_request_prints_session_summary(
    monkeypatch,
    capsys,
) -> None:
    session = create_new_session(workspace="/tmp/test")
    create_file_checkpoint(
        session,
        file_path="/tmp/test/demo.txt",
        existed=True,
        previous_content="before",
    )
    session.transcript_entries = [
        {"id": 1, "kind": "assistant", "body": "Collected evidence."},
    ]
    session.update_metadata()
    monkeypatch.setattr("minicode.main._resolve_target_session", lambda cwd, session_id: session)

    code = _handle_inspect_session_request("/tmp/test", "latest")
    captured = capsys.readouterr()

    assert code == 0
    assert f"Session inspect: {session.session_id[:8]}" in captured.out
    assert "Recent checkpoints:" in captured.out
    assert "Recent transcript (1 shown):" in captured.out


def test_handle_inspect_session_request_errors_when_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr("minicode.main._resolve_target_session", lambda cwd, session_id: None)

    code = _handle_inspect_session_request("/tmp/test", "latest")
    captured = capsys.readouterr()

    assert code == 1
    assert "No saved session found for inspection." in captured.err


def test_handle_replay_session_request_prints_session_timeline(
    monkeypatch,
    capsys,
) -> None:
    session = create_new_session(workspace="/tmp/test")
    session.history = ["continue with runtime trace"]
    session.transcript_entries = [
        {"id": 1, "kind": "assistant", "body": "Collected evidence."},
    ]
    session.update_metadata()
    monkeypatch.setattr("minicode.main._resolve_target_session", lambda cwd, session_id: session)

    code = _handle_replay_session_request("/tmp/test", "latest")
    captured = capsys.readouterr()

    assert code == 0
    assert f"Session replay: {session.session_id[:8]}" in captured.out
    assert "Prompt history (1 shown):" in captured.out
    assert "Transcript timeline (1 shown):" in captured.out


def test_handle_preview_rewind_request_prints_rewind_plan(
    monkeypatch,
    capsys,
) -> None:
    session = create_new_session(workspace="/tmp/test")
    create_file_checkpoint(
        session,
        file_path="/tmp/test/demo.txt",
        existed=True,
        previous_content="before",
    )
    session.update_metadata()
    monkeypatch.setattr("minicode.main._resolve_target_session", lambda cwd, session_id: session)

    code = _handle_preview_rewind_request("/tmp/test", "latest", 1, None)
    captured = capsys.readouterr()

    assert code == 0
    assert f"Rewind preview for session {session.session_id[:8]}" in captured.out
    assert "Would restore 1 checkpoint(s) across 1 file(s)." in captured.out


def test_handle_preview_rewind_request_errors_when_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr("minicode.main._resolve_target_session", lambda cwd, session_id: None)

    code = _handle_preview_rewind_request("/tmp/test", "latest", 1, None)
    captured = capsys.readouterr()

    assert code == 1
    assert "No saved session found to preview." in captured.err


def test_piped_utf8_bom_local_command_is_handled_without_model_call() -> None:
    repo_root = Path(__file__).resolve().parent.parent

    result = subprocess.run(
        [sys.executable, "-m", "minicode.main"],
        input=codecs.BOM_UTF8 + b"/memory\n",
        cwd=repo_root,
        capture_output=True,
        check=False,
    )

    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")

    assert result.returncode == 0
    assert "Memory System Status" in stdout
    assert "Model API error" not in stdout
    assert "Model API error" not in stderr
