from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import minicode.config as config_module
import minicode.session as session_module
import minicode.web_api as web_api


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
    return tmp_path


def _client(workspace: Path, isolated_web_home: Path) -> TestClient:
    app = web_api.create_app(
        default_workspace=workspace,
        frontend_dir=isolated_web_home / "missing-frontend",
    )
    return TestClient(app)


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
        assert body["apiKeyMask"].endswith("9876")
        assert secret not in response.text

        loaded = client.get("/api/settings")
        assert loaded.status_code == 200
        assert secret not in loaded.text

    settings = json.loads((isolated_web_home / ".mini-code" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"]["DEEPSEEK_API_KEY"] == secret


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


def test_mock_run_streams_structured_events(tmp_path: Path, isolated_web_home: Path) -> None:
    with _client(tmp_path, isolated_web_home) as client:
        started = client.post(
            "/api/runs",
            json={"workspace": str(tmp_path), "prompt": "hello from browser"},
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
        assert snapshot["run"]["status"] == "completed"
        kinds = [event["kind"] for event in snapshot["events"]]
        assert "run_started" in kinds
        assert "assistant" in kinds
        assert "done" in kinds
        assert kinds[-1] == "status"
