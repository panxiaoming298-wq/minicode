"""Web service routes and worker-to-host ownership handoff."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import threading
import time

from fastapi import HTTPException, Query

from minicode.development_services import DevelopmentServiceManager


@dataclass
class ServiceRequest:
    canceled: threading.Event = field(default_factory=threading.Event)
    acknowledged: threading.Event = field(default_factory=threading.Event)


class WebServiceBridge:
    def __init__(self, services: DevelopmentServiceManager):
        self.services = services
        self.lock = threading.Lock()
        self.pending: dict[tuple[str, str], ServiceRequest] = {}

    def cancel_run(self, run_id: str) -> None:
        with self.lock:
            for key, operation in self.pending.items():
                if key[0] == run_id:
                    operation.canceled.set()

    def dispatch(self, record, payload: dict, write) -> None:
        request_id = payload.get("requestId")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            return
        key = (record.run_id, request_id)
        kind = payload.get("kind")
        with self.lock:
            if kind in {"service_ack", "service_cancel"}:
                operation = self.pending.get(key)
                if operation is not None:
                    (operation.acknowledged if kind == "service_ack" else operation.canceled).set()
                return
            if key in self.pending:
                return
            operation = ServiceRequest()
            self.pending[key] = operation

        def canceled():
            return (operation.canceled.is_set() or record.status not in {"queued", "running"}
                    or record.process is None or record.process.poll() is not None)

        def perform():
            result = None
            try:
                from minicode.tools.development_services import validate_start, validate_status, validate_control
                action = payload.get("action")
                validator = {"start": validate_start, "status": validate_status,
                             "logs": validate_control, "stop": validate_control}.get(action)
                if validator is None:
                    raise ValueError("未知开发服务请求")
                if action in {"start", "stop"} and not record.requires_workspace_write:
                    raise ValueError("当前任务明确只读，不能启动或停止服务")
                data = validator(payload.get("input", {}))
                if action == "start" and not data.get("cwd"):
                    raise ValueError("启动服务缺少已授权的工作目录")
                if canceled():
                    raise ValueError("服务操作已取消")
                result = self.services.invoke(action, data, workspace=record.workspace,
                                              session_id=record.session_id, cancelled=canceled)
                write(record, {"kind": "service_response", "requestId": request_id, "result": result})
                if action == "start" and result.get("service", {}).get("status") == "running" and not result.get("reused"):
                    # Keep a newly created service only after its worker actually
                    # received the readiness receipt. Lost replies are cleaned up.
                    deadline = time.monotonic() + 5
                    while not operation.acknowledged.wait(0.05):
                        if canceled() or time.monotonic() >= deadline:
                            self.services.stop(result["service"]["serviceId"], reason="启动回执未确认，已清理本次服务。")
                            break
            except (ValueError, OSError, RuntimeError, HTTPException) as error:
                if result and result.get("service", {}).get("status") == "running" and not result.get("reused") and payload.get("action") == "start":
                    self.services.stop(result["service"]["serviceId"], reason="启动回执未送达，已清理本次服务。")
                try:
                    write(record, {"kind": "service_response", "requestId": request_id, "error": str(error)})
                except HTTPException:
                    pass
            finally:
                with self.lock:
                    self.pending.pop(key, None)

        threading.Thread(target=perform, daemon=True, name=f"service-request-{request_id}").start()


def install_service_routes(app, manager) -> None:
    from minicode.web_api import _safe_payload, _require_unarchived_session

    def existing(service_id):
        try:
            return manager.services.get(service_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/development-services")
    def services_list(workspace: str | None = Query(default=None, max_length=4096),
                      session_id: str | None = Query(default=None, alias="sessionId", max_length=128)):
        if not workspace and not session_id:
            raise HTTPException(400, "请选择项目或会话后查看开发服务。")
        if session_id:
            _require_unarchived_session(session_id)
        return _safe_payload({"services": manager.services.list(workspace=workspace, session_id=session_id)})

    @app.get("/api/development-services/{service_id}/logs")
    def service_logs(service_id: str):
        existing(service_id)
        return _safe_payload(manager.services.logs(service_id))

    @app.post("/api/development-services/{service_id}/stop")
    async def service_stop(service_id: str):
        existing(service_id)
        try:
            return _safe_payload({"service": await asyncio.to_thread(manager.services.stop, service_id)})
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/development-services/{service_id}/restart")
    async def service_restart(service_id: str):
        existing(service_id)
        try:
            return _safe_payload(await asyncio.to_thread(manager.services.restart, service_id))
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(409, str(error)) from error
