from __future__ import annotations

import json

import minicode.session as session_module
from minicode.session import create_new_session, load_session, save_session


def test_runtime_trace_round_trips_in_legacy_json_and_bundle(tmp_path, monkeypatch) -> None:
    mini_dir = tmp_path / ".mini-code"
    sessions = mini_dir / "sessions"
    monkeypatch.setattr(session_module, "MINI_CODE_DIR", mini_dir)
    monkeypatch.setattr(session_module, "SESSIONS_DIR", sessions)
    session = create_new_session(workspace=str(tmp_path / "workspace"))
    session.runtime_runs.append(
        {
            "schema_version": "minicode-runtime-trace-v2",
            "profile": {"requested_mode": "standard", "selected_route": "standard-single"},
            "state": {"run_id": "run-1"},
        }
    )
    save_session(session, force_full=True)

    loaded = load_session(session.session_id)
    assert loaded is not None
    assert loaded.runtime_runs == session.runtime_runs
    conversation = json.loads(
        (sessions / session.session_id / "conversation.json").read_text(encoding="utf-8")
    )
    assert conversation["python_extensions"]["runtime_runs"] == session.runtime_runs


def test_runtime_trace_is_included_in_incremental_delta(tmp_path, monkeypatch) -> None:
    mini_dir = tmp_path / ".mini-code"
    sessions = mini_dir / "sessions"
    monkeypatch.setattr(session_module, "MINI_CODE_DIR", mini_dir)
    monkeypatch.setattr(session_module, "SESSIONS_DIR", sessions)
    session = create_new_session(workspace=str(tmp_path))
    save_session(session, force_full=True)
    session._delta_save_count = 1
    session.runtime_runs.append({"schema_version": "minicode-runtime-trace-v2", "run_id": "run-delta"})
    save_session(session)
    delta_files = list((sessions / "deltas" / session.session_id).glob("delta_*.json"))
    assert delta_files
    assert json.loads(delta_files[0].read_text(encoding="utf-8"))["runtime_runs"][0]["run_id"] == "run-delta"

    loaded = load_session(session.session_id)
    assert loaded is not None
    assert loaded.runtime_runs[-1]["run_id"] == "run-delta"
