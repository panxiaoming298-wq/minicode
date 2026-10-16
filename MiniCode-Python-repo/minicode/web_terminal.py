"""Local browser terminal tickets and a flow-controlled WebSocket bridge."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import queue
import secrets
import threading
import time

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from minicode.terminal_process import TerminalUnavailable, WindowsTerminal, powershell_path


class TerminalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workspace: str = Field(default="", max_length=4096)
    session_id: str | None = Field(default=None, alias="sessionId", min_length=1, max_length=128)
    cols: int = Field(default=80, ge=20, le=400)
    rows: int = Field(default=24, ge=4, le=200)


@dataclass
class TerminalTicket:
    request: TerminalRequest
    workspace: Path
    expires: float


class TerminalManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.tickets: dict[str, TerminalTicket] = {}
        self.active: dict[str, tuple[TerminalTicket, WindowsTerminal]] = {}
        self.closed = False

    def issue(self, request: TerminalRequest, workspace: Path) -> str:
        powershell_path()
        with self.lock:
            now = time.monotonic()
            self.tickets = {key: ticket for key, ticket in self.tickets.items() if ticket.expires > now}
            if self.closed or len(self.active) >= 4 or len(self.tickets) >= 32:
                raise TerminalUnavailable("终端数量已达上限，请结束不用的终端后重试。")
            token = secrets.token_urlsafe(32)
            self.tickets[token] = TerminalTicket(request, workspace, now + 30)
            return token

    def take(self, token: str) -> TerminalTicket:
        with self.lock:
            ticket = self.tickets.pop(token, None)
            if self.closed or ticket is None or ticket.expires <= time.monotonic():
                raise TerminalUnavailable("终端连接凭据已过期，请重新打开终端。")
            return ticket

    def start(self, token: str, ticket: TerminalTicket) -> WindowsTerminal:
        with self.lock:
            if self.closed or len(self.active) >= 4:
                raise TerminalUnavailable("终端数量已达上限，请结束不用的终端后重试。")
            process = WindowsTerminal(ticket.workspace, ticket.request.cols, ticket.request.rows)
            self.active[token] = (ticket, process)
            return process

    def release(self, token: str) -> None:
        with self.lock:
            entry = self.active.get(token)
        if entry is not None:
            entry[1].close()
            with self.lock:
                self.active.pop(token, None)

    def close_session(self, session_id: str) -> None:
        with self.lock:
            self.tickets = {key: ticket for key, ticket in self.tickets.items() if ticket.request.session_id != session_id}
            tokens = [key for key, (ticket, _) in self.active.items() if ticket.request.session_id == session_id]
        for token in tokens:
            self.release(token)

    def workspace_active(self, workspace: str) -> bool:
        with self.lock:
            return any(ticket.workspace == Path(workspace).resolve() for ticket, _ in self.active.values())

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.tickets.clear()
            tokens = list(self.active)
        for token in tokens:
            self.release(token)


def _local_connection(connection: Request | WebSocket, *, require_origin: bool = False) -> bool:
    # HTTP middleware does not protect WebSocket routes. Keep shell access local
    # even if the user enabled remote access for the rest of the workbench.
    from minicode.web_api import _LOCAL_HOST_RE, _LOCAL_ORIGIN_RE
    origin = connection.headers.get("origin", "")
    if not _LOCAL_HOST_RE.fullmatch(connection.headers.get("host", "")):
        return False
    if (require_origin and not origin) or (origin and not _LOCAL_ORIGIN_RE.fullmatch(origin)):
        return False
    peer = connection.client.host if connection.client else ""
    if peer == "testclient":  # Starlette's in-process transport, never a TCP peer.
        return True
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _terminal_workspace(request: TerminalRequest, app) -> Path:
    from minicode import session as sessions
    from minicode.web_api import _require_unarchived_session, _resolve_workspace, _session_scope

    if request.session_id:
        _require_unarchived_session(request.session_id)
        metadata = sessions.load_session_metadata(request.session_id)
        if metadata is None:
            raise HTTPException(404, "会话不存在。")
        if _session_scope(request.session_id, metadata.workspace) != "project":
            raise HTTPException(400, "请先打开项目，再使用终端。")
        if worktree_id := metadata.binding.get("worktree_id"):
            app.state.worktree_operations.service.get(worktree_id, require_ready=True)
        # A saved session's directory is authoritative; ignore the client path.
        return _resolve_workspace(metadata.workspace)
    if not request.workspace.strip():
        raise HTTPException(400, "请先打开项目，再使用终端。")
    return _resolve_workspace(request.workspace)


def install_terminal_routes(app, terminals: TerminalManager) -> None:
    @app.post("/api/terminal/tickets")
    async def ticket_create(body: TerminalRequest, request: Request):
        if not _local_connection(request):
            raise HTTPException(403, "终端只接受本机页面的连接。")
        workspace = await asyncio.to_thread(_terminal_workspace, body, app)
        try:
            token = terminals.issue(body, workspace)
        except TerminalUnavailable as error:
            raise HTTPException(503, str(error)) from error
        return {"ticket": token, "workspace": str(workspace), "shell": "PowerShell"}

    @app.websocket("/api/terminal/{token}")
    async def terminal_socket(websocket: WebSocket, token: str):
        if not _local_connection(websocket, require_origin=True):
            await websocket.close(code=1008)
            return
        try:
            ticket = terminals.take(token)
            workspace = await asyncio.to_thread(_terminal_workspace, ticket.request, app)
            if workspace != ticket.workspace:
                raise TerminalUnavailable("会话工作目录已变化，请重新打开终端。")
        except (TerminalUnavailable, HTTPException, ValueError, OSError):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        process = None
        tasks: list[asyncio.Task] = []
        requested_close = False
        sequence = 0
        acknowledged = asyncio.Event()
        failure = ""
        try:
            def start_current():
                # Serialize the final binding check with archive/shutdown
                # cleanup so a terminal cannot start after its owner closes.
                with terminals.lock:
                    if _terminal_workspace(ticket.request, app) != workspace:
                        raise TerminalUnavailable("会话工作目录已变化，请重新打开终端。")
                    return terminals.start(token, ticket)

            # Finish ownership registration even if the browser disconnects
            # during native process startup, then clean it in the same finally.
            startup = asyncio.create_task(asyncio.to_thread(start_current))
            try:
                process = await asyncio.shield(startup)
            except asyncio.CancelledError:
                process = await startup
                raise
            await websocket.send_json({"type": "ready", "workspace": str(workspace), "shell": "PowerShell", "pid": process.pid})

            async def output():
                nonlocal sequence
                while not process.finished.is_set() or not process.output.empty():
                    try:
                        data = process.output.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                        continue
                    sequence += 1
                    acknowledged.clear()
                    await websocket.send_json({"type": "output", "data": data, "sequence": sequence})
                    # Renderer acknowledgements bound browser/transport buffers
                    # as well as the native output queue; never discard logs.
                    await asyncio.wait_for(acknowledged.wait(), timeout=30)

            async def input_messages():
                nonlocal requested_close
                while True:
                    raw = await websocket.receive_text()
                    if len(raw) > 32768:
                        raise ValueError("一次终端输入过大，请分段粘贴。")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("终端消息格式无效。")
                    kind = message.get("type")
                    if kind == "input" and isinstance(data := message.get("data"), str) and 0 < len(data) <= 8192:
                        await asyncio.to_thread(process.write, data)
                    elif kind == "resize" and type(cols := message.get("cols")) is int and type(rows := message.get("rows")) is int and 20 <= cols <= 400 and 4 <= rows <= 200:
                        await asyncio.to_thread(process.resize, cols, rows)
                    elif kind == "ack" and type(message.get("sequence")) is int and message["sequence"] == sequence:
                        acknowledged.set()
                    elif kind == "close":
                        requested_close = True
                        return
                    else:
                        raise ValueError("终端消息格式无效。")

            tasks = [asyncio.create_task(output()), asyncio.create_task(input_messages())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            failure = process.error
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception as error:
            failure = str(error) if isinstance(error, (TerminalUnavailable, ValueError)) else "终端连接中断，请重新打开终端。"
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.shield(asyncio.to_thread(terminals.release, token))
            except OSError:
                failure = "终端进程清理尚未确认，请检查本机进程后重试。"
        try:
            await websocket.send_json({"type": "error", "message": failure} if failure else {
                "type": "closed" if requested_close else "exit", "exitCode": process.exit_code if process else None,
                "message": "终端及其子进程已结束。"})
            await websocket.close(code=1011 if failure else 1000)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
