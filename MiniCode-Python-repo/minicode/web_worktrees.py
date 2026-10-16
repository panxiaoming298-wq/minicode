"""Persistent background operations for Web session environments."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from typing import Literal

from minicode import session as sessions
from minicode.git_snapshot import WorktreeError, fingerprint
from minicode.git_workspaces import GitWorktreeService
from minicode.history_storage import HistoryStorageError
from minicode.session_contract import atomic_write_json
from minicode.session_history import HistoryError, fork_session, list_turns, project_binding, validate_fork
from minicode.store_lock import store_lock, serialized_sessions
from minicode.workspace_lock import WorkspaceBusyError

logger = logging.getLogger(__name__)


class ForkRequest(BaseModel):
    turn_id: str = Field(alias="turnId", min_length=1, max_length=64)
    mode: Literal["local", "worktree"] = "worktree"
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    include_ignored: list[str] = Field(default_factory=list, alias="includeIgnored", max_length=32)


class ApplyRequest(BaseModel):
    preview_id: str = Field(alias="previewId", min_length=32, max_length=32)
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)


class TurnUndoRequest(BaseModel):
    action: Literal["undo", "restore"]
    revision: int = Field(ge=1)


class WorktreeOperations:
    def __init__(self, manager):
        self.manager = manager
        self.service = GitWorktreeService()
        self.root = self.service.root / "operations"

    def _path(self, identity):
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise WorktreeError("无效的操作标识。")
        return self.root / f"{identity}.json"

    def _read_operation(self, path):
        with store_lock(path.with_suffix(".state.lock")):
            return self.service._read(path)

    def _write_operation(self, path, record):
        # Windows readers can deny replacement while a file handle is open.
        # Keep this short state lock separate from the long operation lease.
        with store_lock(path.with_suffix(".state.lock")):
            atomic_write_json(path, record)

    def get(self, identity):
        path = self._path(identity)
        record = self._read_operation(path)
        if record["status"] == "preparing":
            try:
                with store_lock(path.with_suffix(".lock"), timeout=0):
                    record = self._read_operation(path)
                    if record["status"] == "preparing":
                        record.update(status="failed", error="操作被进程中断。目录及恢复日志已保留，请检查后重试。", code="interrupted")
                        self._write_operation(path, record)
            except TimeoutError:
                pass
        return record

    def submit(self, kind, request_id, payload):
        identity = hashlib.sha256(f"{kind}:{request_id}".encode()).hexdigest()
        path = self._path(identity)
        with store_lock(self.root / ".requests.lock"):
            if path.exists():
                previous = self._read_operation(path)
                if previous["fingerprint"] != fingerprint(payload):
                    raise WorktreeError("请求标识已用于另一项操作。", code="duplicate")
                return previous
            record = {"operationId": identity, "kind": kind, "status": "preparing",
                      "createdAt": time.time(), "fingerprint": fingerprint(payload),
                      "worktreeId": payload.get("worktreeId"), "sessionId": None}
            # The worker owns the operation lock before its pending record is visible.
            started = threading.Event()
            startup_errors: list[Exception] = []
            thread = threading.Thread(target=self._execute, args=(path, record, payload, started, startup_errors), daemon=True)
            thread.start()
            if not started.wait(5):
                raise WorktreeError("无法启动工作树后台操作。")
            if startup_errors:
                raise WorktreeError("工作树操作记录保存失败，请检查历史存储后重试。", code="storage_error") from startup_errors[0]
            return self._read_operation(path)

    def _execute(self, path, record, payload, started, startup_errors):
        initialized = False
        try:
            with store_lock(path.with_suffix(".lock")):
                self._write_operation(path, record)
                initialized = True
                started.set()
                try:
                    if record["kind"] in {"create", "fork"}:
                        result = self._create(record, payload)
                    elif record["kind"] == "apply":
                        result = self.service.apply(payload["worktreeId"], payload["previewId"])
                    else:
                        with self.manager._lock:
                            self.require_idle(payload["worktreeId"])
                            result = self.service.public(self.service.cleanup(payload["worktreeId"], discard=payload["discard"]))
                    record.update(status="ready", result=result)
                except Exception as exc:
                    record.update(status="failed", error=str(exc), code=getattr(exc, "code", "failed"), files=getattr(exc, "files", []))
                record["completedAt"] = time.time()
                self._write_operation(path, record)
        except Exception as exc:
            if not initialized:
                startup_errors.append(exc)
                started.set()
            else:
                # Leave the durable pending receipt for get() to diagnose; never
                # replay an operation whose final receipt could not be saved.
                logger.exception("Worktree operation receipt could not be saved: %s", record["operationId"])

    @serialized_sessions
    def _create(self, operation, payload):
        from minicode.web_api import _set_session_scope
        source = sessions.load_session(payload["sourceId"]) if payload.get("sourceId") else None
        if payload.get("sourceId") and source is None:
            raise HistoryError("来源会话已经删除。")
        if source:
            validate_fork(source, payload["turnId"])
        workspace = Path(source.workspace if source else payload["workspace"])
        binding = project_binding(str(workspace), source.binding if source else {})
        worktree = None
        child_id = uuid.uuid4().hex[:12]
        operation["sessionId"] = child_id
        self._write_operation(self._path(operation["operationId"]), operation)
        try:
            # Scope must be durable before the child becomes runnable.
            _set_session_scope(child_id, payload["scope"])
            if payload["mode"] == "worktree":
                worktree = self.service.create(workspace, Path(binding["project_path"]),
                                               worktree_id=payload["worktreeId"], include_ignored=payload["includeIgnored"])
                workspace = Path(worktree["workspace"])
                binding["worktree_id"] = worktree["worktreeId"]
            if source:
                child = fork_session(source, payload["turnId"], workspace=str(workspace), binding=binding, child_id=child_id)
            else:
                child = sessions.create_new_session(str(workspace))
                child.session_id = child_id
                child.binding = binding
                sessions.save_session(child, force_full=True)
            operation.update(sessionId=child.session_id, worktreeId=binding.get("worktree_id"))
            return {"sessionId": child.session_id, "workspace": child.workspace,
                    "worktree": self.service.public(worktree) if worktree else None}
        except Exception:
            # A late index-write failure may leave a complete, durable child.
            # Never remove its ready execution directory during compensation.
            if worktree and sessions.load_session(child_id) is None:
                self.service.cleanup(worktree["worktreeId"], discard=True)
            raise

    def require_idle(self, worktree_id):
        record = self.service.get(worktree_id)
        if any(run.status not in {"completed", "failed", "stopped"}
               and Path(run.workspace).resolve() == Path(record["workspace"]).resolve()
               for run in self.manager._runs.values()):
            raise WorktreeError("工作树正在运行，不能清理。", code="busy")

    @serialized_sessions
    def prepare_session_deletion(self, current, choice):
        identity = current.binding.get("worktree_id")
        if not identity:
            return
        self.require_idle(identity)
        # Same-directory forks can share a worktree; another session still owns it.
        shared = any(m.session_id != current.session_id and m.binding.get("worktree_id") == identity
                     for m in sessions.list_sessions(include_archived=True))
        if not shared and choice != "keep":
            self.service.cleanup(identity, discard=choice == "discard")
        current.binding.pop("worktree_id", None)
        sessions.save_session(current, force_full=True)


def install_worktree_routes(app, operations):
    from minicode.web_api import _require_unarchived_session, _session_scope, _safe_payload, _session_metadata_payload
    from minicode.conversation_view import conversation_page, turn_message_page, turn_message
    from fastapi.responses import JSONResponse

    for error in (WorktreeError, HistoryError, HistoryStorageError, WorkspaceBusyError):
        async def handle(_request, exc):
            return JSONResponse(status_code=503 if isinstance(exc, HistoryStorageError) else 409,
                                content={"detail": str(exc), "code": getattr(exc, "code", "unavailable"),
                                         "files": getattr(exc, "files", [])})
        app.add_exception_handler(error, handle)

    def source_session(identity):
        _require_unarchived_session(identity)
        current = sessions.load_session(identity)
        if current is None:
            raise HTTPException(404, "会话不存在。")
        return current

    def source_metadata(identity):
        _require_unarchived_session(identity)
        metadata = sessions.load_session_metadata(identity)
        if metadata is None:
            raise HTTPException(404, "会话不存在。")
        return metadata

    @app.get("/api/sessions/{identity}/view")
    def conversation_view(identity: str, before: int | None = Query(None, ge=0), limit: int = Query(5, ge=1, le=20)):
        metadata = source_metadata(identity)
        page = conversation_page(identity, before=before, limit=limit)
        page["session"] = _session_metadata_payload(metadata, scope=_session_scope(identity, metadata.workspace))
        return JSONResponse(content=_safe_payload(page), headers={"Cache-Control": "private, no-cache"})

    @app.get("/api/sessions/{identity}/turns/{turn_id}/messages")
    def turn_messages(identity: str, turn_id: str, after: int = Query(0, ge=0), limit: int = Query(60, ge=1, le=100)):
        source_metadata(identity)
        return _safe_payload(turn_message_page(identity, turn_id, after=after, limit=limit))

    @app.get("/api/sessions/{identity}/turns/{turn_id}/messages/{index}")
    def message_detail(identity: str, turn_id: str, index: int):
        source_metadata(identity)
        return {"message": _safe_payload(turn_message(identity, turn_id, index))}

    @app.get("/api/sessions/{identity}/turns")
    def turns(identity: str, before: int | None = Query(None, ge=0), limit: int = Query(20, ge=1, le=100)):
        return _safe_payload(list_turns(source_session(identity), before=before, limit=limit))

    @app.get("/api/sessions/{identity}/turns/{turn_id}/changes")
    def turn_changes(identity: str, turn_id: str):
        from minicode.session_history import turn_file_changes
        return {"changes": _safe_payload(turn_file_changes(source_session(identity), turn_id))}

    def undo_context(identity, turn_id):
        from minicode.workspace_lock import normalize_workspace_path
        current = source_session(identity)
        latest = list_turns(current, limit=1)["turns"]
        eligible = bool(latest and latest[-1]["turnId"] == turn_id and latest[-1]["status"] == "completed")
        workspace = Path(normalize_workspace_path(current.workspace))
        busy = any(run.status not in {"completed", "failed", "stopped"} and (
            run.session_id == identity or Path(normalize_workspace_path(run.workspace)) == workspace
            or workspace in Path(normalize_workspace_path(run.workspace)).parents
            or Path(normalize_workspace_path(run.workspace)) in workspace.parents)
            for run in operations.manager._runs.values())
        return workspace, eligible, busy

    @app.get("/api/sessions/{identity}/turns/{turn_id}/undo")
    def turn_undo_get(identity: str, turn_id: str):
        from minicode.turn_undo import TurnUndoStore
        with operations.manager._lock, store_lock(sessions.SESSIONS_DIR / ".store.lock"):
            workspace, eligible, busy = undo_context(identity, turn_id)
            return {"undo": TurnUndoStore(identity).status(workspace, turn_id, eligible=eligible, busy=busy)}

    @app.post("/api/sessions/{identity}/turns/{turn_id}/undo")
    def turn_undo_post(identity: str, turn_id: str, body: TurnUndoRequest):
        from minicode.turn_undo import TurnUndoStore
        from minicode.workspace_lock import WorkspaceWriteLease
        with operations.manager._lock, store_lock(sessions.SESSIONS_DIR / ".store.lock"):
            workspace, eligible, busy = undo_context(identity, turn_id)
            if not eligible or busy:
                raise WorktreeError("工作区有任务正在运行。" if busy else "仅支持最近一轮已完成的修改。", code="busy" if busy else "stale")
            with WorkspaceWriteLease(workspace, run_id="turn-undo-" + identity):
                return {"undo": TurnUndoStore(identity).apply(workspace, turn_id, body.action, body.revision)}

    @app.post("/api/sessions/{identity}/fork", status_code=202)
    def fork(identity: str, body: ForkRequest):
        source = source_session(identity)
        scope = _session_scope(identity, source.workspace)
        if scope == "standalone" and body.mode != "local":
            raise HTTPException(400, "独立对话仅支持原目录分叉。")
        if source.binding.get("worktree_id"):
            operations.service.get(source.binding["worktree_id"], require_ready=True)
        return {"operation": operations.submit("fork", body.request_id, {
            "sourceId": identity, "turnId": body.turn_id, "scope": scope, "mode": body.mode,
            "includeIgnored": body.include_ignored,
            "worktreeId": hashlib.sha256(f"fork:{body.request_id}".encode()).hexdigest()[:32] if body.mode == "worktree" else None,
        })}

    @app.get("/api/worktree-operations/{identity}")
    def operation(identity: str):
        return {"operation": operations.get(identity)}

    @app.get("/api/worktrees/{identity}/changes")
    def changes(identity: str):
        return operations.service.changes(identity)

    @app.post("/api/worktrees/{identity}/apply-preview")
    def preview(identity: str):
        return operations.service.preview(identity)

    @app.post("/api/worktrees/{identity}/apply", status_code=202)
    def apply(identity: str, body: ApplyRequest):
        return {"operation": operations.submit("apply", body.request_id, {"worktreeId": identity, "previewId": body.preview_id})}

    @app.delete("/api/worktrees/{identity}", status_code=202)
    def cleanup(identity: str, requestId: str = Query(min_length=1, max_length=128), discard: bool = False):
        with operations.manager._lock:
            operations.require_idle(identity)
            terminals = getattr(app.state, "terminal_manager", None)
            if terminals is not None and terminals.workspace_active(operations.service.get(identity)["workspace"]):
                raise HTTPException(409, "工作树中有终端正在运行，请先结束终端再删除。")
        return {"operation": operations.submit("cleanup", requestId, {"worktreeId": identity, "discard": discard})}
