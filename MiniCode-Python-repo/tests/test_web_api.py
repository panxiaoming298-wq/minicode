from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import minicode.config as config_module
import minicode.session as session_module
from minicode import web_api, web_worker
from minicode.turn_events import TurnEvent


@pytest.fixture
def isolated_web_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    mini_code_dir = tmp_path / ".mini-code"
    sessions_dir = mini_code_dir / "sessions"
    settings_path = mini_code_dir / "settings.json"
    monkeypatch.setattr(session_module, "MINI_CODE_DIR", mini_code_dir)
    monkeypatch.setattr(session_module, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(config_module, "MINI_CODE_DIR", mini_code_dir)
    monkeypatch.setattr(config_module, "MINI_CODE_SETTINGS_PATH", settings_path)
    monkeypatch.setattr(web_api, "MINI_CODE_SETTINGS_PATH", settings_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MINI_CODE_WEB_FORCE_MOCK", "1")
    monkeypatch.delenv("MINI_CODE_EXECUTION_PROFILE", raising=False)
    return tmp_path


def _client(workspace: Path, isolated_web_home: Path) -> TestClient:
    app = web_api.create_app(
        default_workspace=workspace,
        frontend_dir=isolated_web_home / "missing-frontend",
    )
    return TestClient(app)


def test_web_event_sink_announces_task_analysis_once_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict] = []
    monkeypatch.setattr(web_worker, "_emit", emitted.append)
    sink = web_worker.WebEventSink()

    sink.on_event(TurnEvent.thinking_chunk(step=1, content="hidden-one"))
    sink.on_event(TurnEvent.thinking_chunk(step=1, content="hidden-two"))
    sink.on_event(TurnEvent.thinking_chunk(step=2, content="hidden-three"))

    assert emitted == [
        {
            "kind": "thinking_status",
            "step": 1,
            "content": "正在分析当前任务",
        }
    ]
    assert all("hidden" not in str(event) for event in emitted)


def test_settings_key_is_saved_but_never_returned(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    secret = "sk-test-super-secret-9876"
    with _client(tmp_path, isolated_web_home) as client:
        response = client.put(
            "/api/settings",
            json={
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "baseUrl": "https://api.deepseek.com",
                "apiKey": secret,
                "thinkingMode": "enabled",
                "reasoningEffort": "high",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["apiKeyConfigured"] is True
        assert body["executionProfile"] == "standard"
        assert body["apiKeyMask"].endswith("9876")
        assert secret not in response.text

        loaded = client.get("/api/settings")
        assert loaded.status_code == 200
        assert secret not in loaded.text

    settings = json.loads((isolated_web_home / ".mini-code" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["DEEPSEEK_API_KEY"] == secret


def test_web_event_redaction_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-never-return-this-secret"

    def fail_redaction(_value):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(web_api, "redact_sensitive_payload", fail_redaction)
    monkeypatch.setattr(web_worker, "redact_sensitive_payload", fail_redaction)

    record = web_api.RunRecord(
        run_id="redaction-run",
        session_id="redaction-session",
        workspace=".",
        prompt_preview="safe",
        execution_profile="standard",
    )
    event = record.add_event({"kind": "error", "message": secret})
    worker_payload = web_worker._json_safe({"kind": "error", "message": secret})

    assert secret not in str(event)
    assert event["kind"] == "error"
    assert event["message"].startswith("Sensitive event omitted")
    assert isinstance(worker_payload, dict)
    assert worker_payload["kind"] == "error"
    assert secret not in str(worker_payload)


def test_run_snapshot_bounds_retention_and_resumes_after_sequence_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_api, "_MAX_RETAINED_RUN_EVENTS", 3)
    record = web_api.RunRecord(
        run_id="cursor-run",
        session_id="cursor-session",
        workspace=".",
        prompt_preview="cursor",
        execution_profile="standard",
    )
    for index in range(5):
        record.add_event({"kind": "progress", "content": f"event-{index + 1}"})

    snapshot = record.snapshot(after=3)
    full_window = record.snapshot()

    assert snapshot["run"]["eventCount"] == 5
    assert snapshot["run"]["retainedEventCount"] == 3
    assert snapshot["run"]["eventsDiscardedThrough"] == 2
    assert [event["sequence"] for event in snapshot["events"]] == [4, 5]
    assert [event["sequence"] for event in full_window["events"]] == [2, 3, 4, 5]
    assert full_window["events"][0]["kind"] == "event_gap"


def test_rejects_non_local_origin(tmp_path: Path, isolated_web_home: Path) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        response = client.get("/api/health", headers={"Origin": "https://example.com"})
    assert response.status_code == 403


def test_session_endpoints_use_workspace_scope(tmp_path: Path, isolated_web_home: Path) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        assert created.status_code == 200
        session_id = created.json()["session"]["sessionId"]

        listed = client.get("/api/sessions", params={"workspace": str(tmp_path)})
        assert listed.status_code == 200
        assert [item["sessionId"] for item in listed.json()["sessions"]] == [session_id]

        run_snapshot = client.get(f"/api/sessions/{session_id}/run")
        assert run_snapshot.status_code == 200
        assert run_snapshot.json() == {"run": None, "events": []}


def test_navigation_and_session_snapshot_expose_a_background_run(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)})
        session_id = created.json()["session"]["sessionId"]
        record = web_api.RunRecord(
            run_id="background-run",
            session_id=session_id,
            workspace=str(tmp_path.resolve()),
            prompt_preview="后台继续处理",
            execution_profile="standard",
            status="running",
        )
        record.add_event({"kind": "status", "status": "running", "content": "任务已启动"})
        manager = client.app.state.run_manager
        with manager._lock:
            manager._runs[record.run_id] = record

        navigation = client.get("/api/navigation")
        assert navigation.status_code == 200
        summary = next(
            item for item in navigation.json()["sessions"]
            if item["sessionId"] == session_id
        )
        assert summary["status"] == "running"

        snapshot = client.get(f"/api/sessions/{session_id}/run")
        assert snapshot.status_code == 200
        assert snapshot.json()["run"]["runId"] == record.run_id
        assert snapshot.json()["run"]["status"] == "running"
        assert snapshot.json()["events"][0]["content"] == "任务已启动"


def test_workspace_open_changes_current_workspace_and_tracks_recents(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    other_workspace = tmp_path / "another-project"
    other_workspace.mkdir()

    with _client(tmp_path, isolated_web_home) as client:
        opened = client.post("/api/workspaces/open", json={"path": str(other_workspace)})
        assert opened.status_code == 200
        assert opened.json()["workspace"] == str(other_workspace.resolve())
        assert opened.json()["recentWorkspaces"][0]["name"] == "another-project"

        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.json()["workspace"] == str(other_workspace.resolve())
        assert [item["path"] for item in bootstrap.json()["recentWorkspaces"]] == [
            str(other_workspace.resolve()),
            str(tmp_path.resolve()),
        ]


def test_workspace_open_rejects_a_file_path(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    file_path = tmp_path / "single-file.py"
    file_path.write_text("print('hello')\n", encoding="utf-8")

    with _client(tmp_path, isolated_web_home) as client:
        response = client.post("/api/workspaces/open", json={"path": str(file_path)})

    assert response.status_code == 400
    assert "请选择项目文件夹" in response.json()["detail"]


def test_workspace_picker_returns_native_selection(
    tmp_path: Path,
    isolated_web_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "picked-project"
    selected.mkdir()
    monkeypatch.setattr(web_api, "_pick_workspace_dialog", lambda _initial: selected.resolve())

    with _client(tmp_path, isolated_web_home) as client:
        response = client.post(
            "/api/workspaces/pick",
            json={"initialPath": str(tmp_path)},
        )

    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "workspace": str(selected.resolve())}


def test_windows_picker_passes_initial_path_through_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return web_api.subprocess.CompletedProcess(
            args,
            0,
            json.dumps(str(tmp_path.resolve()), ensure_ascii=False) + "\n",
            "",
        )

    monkeypatch.setattr(web_api.subprocess, "run", fake_run)

    selected = web_api._pick_windows_workspace_dialog(tmp_path)

    assert selected == tmp_path.resolve()
    assert str(tmp_path) not in captured["args"]
    assert captured["env"]["MINICODE_PICKER_INITIAL"] == str(tmp_path)


def test_workspace_status_scopes_git_to_selected_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run_git(_workspace: Path, args: list[str]):
        captured.extend(args)
        return web_api.subprocess.CompletedProcess(args, 0, "## main\n M inside.py\n", "")

    monkeypatch.setattr(web_api, "_run_git", fake_run_git)

    status = web_api._workspace_status(tmp_path)

    assert captured[-2:] == ["--", "."]
    assert status["files"] == [{"status": " M", "path": "inside.py"}]


def test_workspace_diff_rejects_paths_outside_selected_directory(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()

    with _client(workspace, isolated_web_home) as client:
        response = client.get(
            "/api/workspace/diff",
            params={"path": str(workspace), "file": "../outside.py"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "文件必须位于当前工作区内。"


def test_navigation_lists_global_conversations_and_persists_pins(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    second_workspace = tmp_path / "second-project"
    second_workspace.mkdir()

    with _client(tmp_path, isolated_web_home) as client:
        first = client.post("/api/sessions", json={"workspace": str(tmp_path)}).json()["session"]
        second = client.post("/api/sessions", json={"workspace": str(second_workspace)}).json()["session"]

        navigation = client.get("/api/navigation")
        assert navigation.status_code == 200
        assert {item["sessionId"] for item in navigation.json()["sessions"]} == {
            first["sessionId"],
            second["sessionId"],
        }

        pinned_session = client.put(
            "/api/navigation/pins",
            json={"kind": "session", "target": second["sessionId"], "pinned": True},
        )
        assert pinned_session.status_code == 200
        assert pinned_session.json()["pinnedSessionIds"] == [second["sessionId"]]

        pinned_project = client.put(
            "/api/navigation/pins",
            json={"kind": "workspace", "target": str(second_workspace), "pinned": True},
        )
        assert pinned_project.status_code == 200
        assert pinned_project.json()["pinnedWorkspacePaths"] == [str(second_workspace.resolve())]

        deleted = client.delete(f"/api/sessions/{second['sessionId']}")
        assert deleted.status_code == 200
        assert client.get("/api/navigation").json()["pinnedSessionIds"] == []


def test_navigation_separates_project_and_standalone_conversations(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with _client(project, isolated_web_home) as client:
        project_session = client.post(
            "/api/sessions",
            json={"scope": "project", "workspace": str(project)},
        ).json()["session"]
        standalone_session = client.post(
            "/api/sessions",
            json={"scope": "standalone"},
        ).json()["session"]

        assert project_session["scope"] == "project"
        assert project_session["projectPath"] == str(project.resolve())
        assert standalone_session["scope"] == "standalone"
        assert standalone_session["projectPath"] is None
        assert standalone_session["workspace"] == str(
            (isolated_web_home / ".mini-code" / "standalone").resolve()
        )

        navigation = client.get("/api/navigation").json()
        sessions = {item["sessionId"]: item for item in navigation["sessions"]}
        assert sessions[project_session["sessionId"]]["scope"] == "project"
        assert sessions[standalone_session["sessionId"]]["scope"] == "standalone"

        resumed_with_wrong_scope = client.post(
            "/api/runs",
            json={
                "scope": "project",
                "workspace": str(project),
                "sessionId": standalone_session["sessionId"],
                "prompt": "wrong scope",
            },
        )
        assert resumed_with_wrong_scope.status_code == 400

        deleted = client.delete(f"/api/sessions/{standalone_session['sessionId']}")
        assert deleted.status_code == 200
        state = json.loads((isolated_web_home / ".mini-code" / "web-state.json").read_text(encoding="utf-8"))
        assert standalone_session["sessionId"] not in state["standaloneSessions"]


def test_workspace_file_browser_lists_source_and_reads_original_text(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    workspace = tmp_path / "project"
    source = workspace / "src"
    ignored = workspace / "node_modules"
    source.mkdir(parents=True)
    ignored.mkdir()
    (workspace / "README.md").write_text("# Project\n", encoding="utf-8")
    (source / "main.py").write_text("print('你好')\n", encoding="utf-8")
    (ignored / "package.js").write_text("ignored\n", encoding="utf-8")
    (workspace / "image.png").write_bytes(b"\x89PNG\x00binary")

    with _client(workspace, isolated_web_home) as client:
        root = client.get("/api/workspace/files", params={"path": str(workspace)})
        assert root.status_code == 200
        assert [entry["name"] for entry in root.json()["entries"]] == [
            "src",
            "image.png",
            "README.md",
        ]

        nested = client.get(
            "/api/workspace/files",
            params={"path": str(workspace), "directory": "src"},
        )
        assert nested.status_code == 200
        assert nested.json()["entries"][0]["path"] == "src/main.py"

        original = client.get(
            "/api/workspace/file",
            params={"path": str(workspace), "file": "src/main.py"},
        )
        assert original.status_code == 200
        assert original.json()["content"].replace("\r\n", "\n") == "print('你好')\n"
        assert original.json()["encoding"] == "utf-8"

        binary = client.get(
            "/api/workspace/file",
            params={"path": str(workspace), "file": "image.png"},
        )
        assert binary.status_code == 415

        outside = client.get(
            "/api/workspace/file",
            params={"path": str(workspace), "file": "../outside.py"},
        )
        assert outside.status_code == 400


def test_workspace_capabilities_expose_names_not_mcp_execution_config(
    tmp_path: Path,
    isolated_web_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / ".mini-code" / "skills" / "review-helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Review helper\n\nReview the current change set.\n",
        encoding="utf-8",
    )
    secret = "mcp-private-token-123"
    monkeypatch.setattr(
        web_api,
        "load_runtime_config",
        lambda _workspace: {
            "provider": "mock",
            "mcpServers": {
                "private-server": {
                    "command": "python C:/private/server.py",
                    "env": {"PRIVATE_TOKEN": secret},
                    "protocol": "stdio",
                }
            },
        },
    )

    with _client(tmp_path, isolated_web_home) as client:
        response = client.get(
            "/api/workspace/capabilities",
            params={"path": str(tmp_path)},
        )

    assert response.status_code == 200
    payload = response.json()["capabilities"]
    assert {item["name"] for item in payload["skills"]} == {"review-helper"}
    assert payload["mcpServers"][0]["name"] == "private-server"
    assert payload["mcpServers"][0]["status"] == "configured"
    assert any(tool["name"] == "read_file" for tool in payload["tools"])
    assert "command" not in payload["mcpServers"][0]
    assert "server.py" not in response.text
    assert secret not in response.text
    assert "path" not in payload["skills"][0]


def test_session_inspection_strips_resume_only_content(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        created = client.post("/api/sessions", json={"workspace": str(tmp_path)}).json()
        session_id = created["session"]["sessionId"]
        session = session_module.load_session(session_id)
        assert session is not None
        session.skills = [{
            "name": "safe-skill",
            "description": "safe description",
            "source": "project",
            "path": "C:/secret/SKILL.md",
        }]
        session.mcp_servers = [{
            "name": "safe-mcp",
            "status": "connected",
            "toolCount": 2,
            "command": "python C:/secret/server.py",
        }]
        session.instruction_layers = [{
            "name": "project-managed",
            "scope": "project",
            "kind": "managed",
            "exists": True,
            "path": "C:/secret/POLICY.md",
            "content": "instruction-secret-456",
        }]
        session.checkpoints = [
            session_module.FileCheckpoint(
                checkpoint_id="checkpoint-safe",
                created_at=time.time(),
                file_path=str(tmp_path / "src" / "main.py"),
                existed=True,
                previous_content="checkpoint-secret-789",
            )
        ]
        session.runtime_runs = [{
            "schema_version": "minicode-runtime-trace-v2",
            "plan": {
                "run_id": "run-safe",
                "plan_version": 1,
                "contract_revision": 1,
                "nodes": [],
            },
            "delivery": {
                "completed": True,
                "artifact_refs": [{
                    "artifact_id": "artifact-safe",
                    "content_hash": "sha256-safe",
                    "size": 12,
                    "media_type": "text/plain",
                    "store_key": "artifact-store-secret",
                }],
                "verification_records": [{
                    "verifier": "pytest",
                    "passed": True,
                    "scope": "project",
                    "details": "1 passed",
                    "evidence_refs": [{
                        "evidence_type": "test",
                        "resource": "C:/secret/test-output.log",
                        "content_hash": "evidence-safe",
                        "claim": "tests passed",
                        "producer_node_id": "node-1",
                        "base_revision": "base-safe",
                    }],
                }],
            },
            "profile": {},
            "topology_decision": {},
            "scheduler": {},
            "state": {},
            "context_manifests": [],
        }]
        session_module.save_session(session, force_full=True)

        response = client.get(f"/api/sessions/{session_id}/inspect")

    assert response.status_code == 200
    inspect = response.json()["inspect"]
    assert inspect["skills"][0]["name"] == "safe-skill"
    assert inspect["mcpServers"][0]["name"] == "safe-mcp"
    assert inspect["instructionLayers"][0]["name"] == "project-managed"
    assert inspect["checkpoints"][0]["checkpointId"] == "checkpoint-safe"
    assert inspect["runtimeRuns"][0]["delivery"]["artifacts"][0]["artifactId"] == "artifact-safe"
    assert inspect["runtimeRuns"][0]["delivery"]["verificationRecords"][0]["evidence"][0]["claim"] == "tests passed"
    for secret in [
        "C:/secret/SKILL.md",
        "C:/secret/server.py",
        "C:/secret/POLICY.md",
        "instruction-secret-456",
        "checkpoint-secret-789",
        "artifact-store-secret",
        "C:/secret/test-output.log",
    ]:
        assert secret not in response.text


def test_mock_run_streams_structured_events(tmp_path: Path, isolated_web_home: Path) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        started = client.post(
            "/api/runs",
            json={
                "workspace": str(tmp_path),
                "prompt": "你现在是什么思考强度",
                "executionProfile": "ultra",
            },
        )
        assert started.status_code == 200
        assert started.json()["run"]["executionProfile"] == "ultra"
        run_id = started.json()["run"]["runId"]

        snapshot = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            snapshot = client.get(f"/api/runs/{run_id}").json()
            if snapshot["run"]["status"] in {"completed", "failed", "stopped"}:
                break
            time.sleep(0.1)

        assert snapshot is not None
        assert snapshot["run"]["status"] == "completed"
        kinds = [event["kind"] for event in snapshot["events"]]
        assert "run_started" in kinds
        assert "capability_snapshot" in kinds
        assert kinds.count("assistant") == 1
        assert kinds.count("done") == 1
        assert "tool_start" not in kinds
        assert "error" not in kinds
        assert kinds[-1] == "status"
        assistant = next(event for event in snapshot["events"] if event["kind"] == "assistant")
        assert "Runtime completion gate rejected delivery" not in assistant["content"]
        run_started = next(event for event in snapshot["events"] if event["kind"] == "run_started")
        assert run_started["executionProfile"] == "ultra"
        persisted_payload = client.get(
            f"/api/sessions/{started.json()['run']['sessionId']}"
        ).json()
        assert persisted_payload["session"]["status"] == "completed"
        persisted = persisted_payload["messages"]
        assert persisted[-1]["role"] == "assistant"
        assert persisted[-1]["content"] == assistant["content"]
        inspection = client.get(
            f"/api/sessions/{started.json()['run']['sessionId']}/inspect"
        ).json()["inspect"]
        assert inspection["runtimeRuns"][-1]["webRunId"] == run_id
        assert inspection["runtimeRuns"][-1]["runId"] == run_id
        session_run = client.get(
            f"/api/sessions/{started.json()['run']['sessionId']}/run"
        )
        assert session_run.status_code == 200
        assert session_run.json()["run"]["runId"] == run_id
        assert session_run.json()["run"]["status"] == "completed"
        assert session_run.json()["events"][-1]["status"] == "completed"


def test_completion_gate_rejection_is_a_single_failed_run_error(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        started = client.post(
            "/api/runs",
            json={
                "workspace": str(tmp_path),
                "prompt": "修复项目中的并发 bug",
                "executionProfile": "ultra",
            },
        )
        assert started.status_code == 200
        run_id = started.json()["run"]["runId"]

        snapshot = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            snapshot = client.get(f"/api/runs/{run_id}").json()
            if snapshot["run"]["status"] in {"completed", "failed", "stopped"}:
                break
            time.sleep(0.1)

        assert snapshot is not None
        assert snapshot["run"]["status"] == "failed"
        errors = [event for event in snapshot["events"] if event["kind"] == "error"]
        assert len(errors) == 1
        assert errors[0]["message"].startswith("Runtime completion gate rejected delivery:")
        assert not any(event["kind"] == "assistant" for event in snapshot["events"])
        persisted_payload = client.get(
            f"/api/sessions/{started.json()['run']['sessionId']}"
        ).json()
        assert persisted_payload["session"]["status"] == "failed"
        persisted = persisted_payload["messages"]
        assert persisted[-1]["role"] == "assistant"
        assert persisted[-1]["content"] == errors[0]["message"]


def test_run_rejects_non_public_execution_profile(
    tmp_path: Path,
    isolated_web_home: Path,
) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        response = client.post(
            "/api/runs",
            json={
                "workspace": str(tmp_path),
                "prompt": "do not expose internal topology",
                "executionProfile": "deep",
            },
        )

    assert response.status_code == 422
