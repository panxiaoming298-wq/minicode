"""Application-owned development services, separate from turn/worker lifetime."""
from __future__ import annotations

import atexit
import codecs
from dataclasses import dataclass, field
import http.client
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
import uuid

from minicode.service_process import OwnedProcess

_ACTIVE = {"starting", "running", "stopping"}
_LOG_LIMIT = 64_000
_FILE_LOG_LIMIT = 256_000


def display_command(command: list[str]) -> list[str]:
    from minicode.release_readiness import redact_sensitive_payload
    result = redact_sensitive_payload(list(command))
    for index, argument in enumerate(command[:-1]):
        if re.fullmatch(r"--?(?:.*[_-])?(?:password|secret|token|api[_-]?key|authorization)", argument, re.IGNORECASE):
            result[index + 1] = "[REDACTED]"
    return result


def ready_address(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("ready_url 必须是本机 HTTP/HTTPS 地址，例如 http://127.0.0.1:5173/")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("ready_url 不接受凭据或片段")
    host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    authority = f"[{host}]" if ":" in host else host
    normalized = urlunsplit((parsed.scheme, f"{authority}:{port}", parsed.path or "/", parsed.query, ""))
    return normalized, host, port


def _http_ready(url: str) -> bool:
    parsed = urlsplit(url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=0.4)
    try:
        connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
        response = connection.getresponse()
        return 200 <= response.status < 400
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


@dataclass
class ServiceRecord:
    service_id: str
    command: list[str]
    cwd: str
    workspace: str
    url: str
    startup_timeout: int
    session_ids: set[str] = field(default_factory=set)
    status: str = "starting"
    started_at: int = field(default_factory=lambda: int(time.time() * 1000))
    ready_at: int | None = None
    exit_code: int | None = None
    error: str = ""
    owned: OwnedProcess | None = field(default=None, repr=False)
    logs: str = ""
    settled: threading.Event = field(default_factory=threading.Event, repr=False)
    operation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    reader: threading.Thread | None = field(default=None, repr=False)

    def public(self) -> dict:
        exited = self.status == "running" and self.owned is not None and self.owned.process.poll() is not None
        return {"serviceId": self.service_id, "command": display_command(self.command), "cwd": self.cwd,
                "workspace": self.workspace, "sessionIds": sorted(self.session_ids), "url": self.url,
                "status": "failed" if exited else self.status, "pid": self.owned.process.pid if self.owned else None,
                "startedAt": self.started_at, "readyAt": self.ready_at,
                "exitCode": self.owned.process.returncode if exited else self.exit_code,
                "error": "开发服务已退出，请查看日志。" if exited and not self.error else self.error}


class DevelopmentServiceManager:
    def __init__(self, storage_dir: Path | None = None, *, max_active: int = 8):
        self.storage_dir = Path(storage_dir) if storage_dir is not None else None
        self.max_active = max_active
        self.lock = threading.RLock()
        self.records: dict[str, ServiceRecord] = {}
        self.closed = False
        self._restore()
        atexit.register(self.close)

    def _restore(self) -> None:
        if self.storage_dir is None or not (self.storage_dir / "services.json").exists():
            return
        data = json.loads((self.storage_dir / "services.json").read_text(encoding="utf-8"))
        if not isinstance(data, list) or len(data) > 64:
            raise ValueError("开发服务记录格式错误，请检查本机服务存储")
        for value in data:
            if not re.fullmatch(r"svc_[0-9a-f]{32}", str(value.get("serviceId", ""))):
                raise ValueError("开发服务记录身份错误")
            record = ServiceRecord(service_id=value["serviceId"], command=value["command"],
                cwd=value["cwd"], workspace=value["workspace"], url=ready_address(value["url"])[0],
                startup_timeout=value.get("startupTimeout", 30), session_ids=set(value.get("sessionIds", [])),
                status="stopped" if value.get("status") in _ACTIVE else value.get("status", "stopped"),
                started_at=value["startedAt"], ready_at=value.get("readyAt"), exit_code=value.get("exitCode"),
                error="MiniCode 上次退出，服务已停止。" if value.get("status") in _ACTIVE else value.get("error", ""))
            # Historical PIDs are never adopted or used as termination authority.
            record.settled.set()
            log_path = self.storage_dir / f"{record.service_id}.log"
            if log_path.exists():
                with log_path.open("rb") as stream:
                    stream.seek(max(0, log_path.stat().st_size - _LOG_LIMIT))
                    record.logs = stream.read().decode("utf-8", errors="replace")
            self.records[record.service_id] = record

    def _persist(self) -> None:
        if self.storage_dir is None:
            return
        with self.lock:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            # The private state keeps launch arguments for an explicit restart;
            # sanitized display arguments never become execution authority.
            payload = [{**record.public(), "command": list(record.command), "startupTimeout": record.startup_timeout}
                       for record in self.records.values()]
            temporary = self.storage_dir / "services.json.tmp"
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.storage_dir / "services.json")

    def list(self, *, workspace: str | None = None, session_id: str | None = None) -> list[dict]:
        scope = os.path.normcase(str(Path(workspace).resolve())) if workspace else None
        with self.lock:
            return [record.public() for record in self.records.values()
                    if (scope is None or os.path.normcase(record.workspace) == scope)
                    and (session_id is None or session_id in record.session_ids)]

    def get(self, service_id: str, *, workspace: str | None = None) -> ServiceRecord:
        with self.lock:
            record = self.records.get(service_id)
            if record is None or (workspace is not None and os.path.normcase(record.workspace) != os.path.normcase(str(Path(workspace).resolve()))):
                raise ValueError("开发服务不存在或不属于当前项目")
            return record

    def logs(self, service_id: str, *, workspace: str | None = None) -> dict:
        record = self.get(service_id, workspace=workspace)
        with self.lock:
            return {"service": record.public(), "output": record.logs, "truncated": len(record.logs) >= _LOG_LIMIT}

    def _log(self, record: ServiceRecord, text: str) -> None:
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        with self.lock:
            record.logs = (record.logs + text)[-_LOG_LIMIT:]
            if self.storage_dir is not None:
                path = self.storage_dir / f"{record.service_id}.log"
                if path.exists() and path.stat().st_size >= _FILE_LOG_LIMIT:
                    os.replace(path, path.with_suffix(".previous.log"))
                with path.open("ab") as stream:
                    stream.write(text.encode("utf-8"))

    def _read_logs(self, record: ServiceRecord) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := record.owned.process.stdout.read1(4096):
                self._log(record, decoder.decode(chunk))
            self._log(record, decoder.decode(b"", final=True))
        except (OSError, ValueError) as error:
            with self.lock:
                record.error = f"读取服务日志失败：{error}"
        finally:
            record.owned.process.stdout.close()

    def _watch(self, record: ServiceRecord) -> None:
        return_code = record.owned.process.wait()
        # A launcher exiting must not leave descendants behind.
        record.owned.terminate()
        if record.reader is not None:
            record.reader.join(timeout=2)
        with self.lock:
            if self.records.get(record.service_id) is not record:
                return
            record.exit_code = return_code
            if record.status in {"starting", "running"}:
                record.status = "failed"
                record.error = f"开发服务已退出（退出码 {return_code}）。请查看日志。"
            record.settled.set()
            try:
                self._persist()
            except OSError as error:
                record.error += f" 服务状态保存失败：{error}"

    def start(self, command: list[str], cwd: str, ready_url: str, *, startup_timeout: int = 30,
              workspace: str | None = None, session_id: str | None = None,
              cancelled: Callable[[], bool] = lambda: False) -> dict:
        from minicode.tools.development_services import validate_start
        from minicode.tools.run_command import _build_execution_command

        validate_start({"command": command[0] if command else "", "args": command[1:],
                        "cwd": cwd, "ready_url": ready_url, "startup_timeout": startup_timeout})
        cwd = str(Path(cwd).resolve())
        if not Path(cwd).is_dir():
            raise ValueError("开发服务目录不存在")
        workspace = str(Path(workspace or cwd).resolve())
        url, host, port = ready_address(ready_url)
        if cancelled():
            raise ValueError("服务启动已取消")
        with self.lock:
            if self.closed:
                raise ValueError("MiniCode 正在退出，无法启动服务")
            existing = next((record for record in self.records.values()
                if os.path.normcase(record.cwd) == os.path.normcase(cwd) and record.command == command and record.url == url
                and os.path.normcase(record.workspace) == os.path.normcase(workspace)), None)
            if existing is not None and existing.status in {"starting", "running"}:
                record = existing
                reused = True
            else:
                if sum(record.status in _ACTIVE for record in self.records.values()) >= self.max_active:
                    raise ValueError("开发服务数量已达上限，请先停止不再使用的服务")
                # Refuse occupied ports before creating any child; never adopt them.
                with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    if probe.connect_ex((host, port)) == 0:
                        raise ValueError(f"端口 {port} 已被其他服务占用，请先核对该服务或选择空闲端口")
                record = ServiceRecord(service_id=existing.service_id if existing else f"svc_{uuid.uuid4().hex}",
                    command=list(command), cwd=cwd, workspace=workspace, url=url, startup_timeout=startup_timeout,
                    session_ids=set(existing.session_ids) if existing else set())
                if existing is None and len(self.records) >= 64:
                    removable = next((key for key, item in self.records.items() if item.status not in _ACTIVE), None)
                    if removable:
                        del self.records[removable]
                        if self.storage_dir is not None:
                            for suffix in (".log", ".previous.log"):
                                (self.storage_dir / f"{removable}{suffix}").unlink(missing_ok=True)
                self.records[record.service_id] = record
                reused = False
            if session_id:
                record.session_ids.add(session_id)
            try:
                self._persist()
            except OSError as error:
                if not reused:
                    record.status = "failed"
                    record.error = f"保存服务记录失败，未启动进程：{error}"
                    record.settled.set()
                raise
        if reused:
            while not record.settled.wait(0.05):
                if cancelled():
                    raise ValueError("等待已有服务的请求已取消；原服务的启动由原请求管理")
            return {"service": record.public(), "reused": True}
        try:
            with record.operation_lock:
                if cancelled() or self.closed or record.status != "starting":
                    raise ValueError("服务启动已取消")
                executable, args = _build_execution_command(command[0], command[0], command[1:],
                                                            use_shell=False, background_shell=False)
                record.owned = OwnedProcess([executable, *args], cwd)
                record.reader = threading.Thread(target=self._read_logs, args=(record,), daemon=True,
                                                  name=f"service-log-{record.service_id}")
                record.reader.start()
                threading.Thread(target=self._watch, args=(record,), daemon=True, name=f"service-watch-{record.service_id}").start()
            deadline = time.monotonic() + startup_timeout
            while True:
                if cancelled() or self.closed or record.status in {"stopping", "stopped"}:
                    raise ValueError("服务启动已取消")
                if record.owned.process.poll() is not None or record.status == "failed":
                    raise ValueError("开发服务在就绪前退出，请查看服务日志")
                if record.owned.owns_listener(port, host) and _http_ready(url):
                    with record.operation_lock, self.lock:
                        if cancelled() or self.closed or record.status != "starting" or record.owned.process.poll() is not None:
                            raise ValueError("服务就绪前已被停止")
                        record.status = "running"
                        record.ready_at = int(time.time() * 1000)
                        self._persist()
                        record.settled.set()
                    return {"service": record.public(), "reused": False}
                if time.monotonic() >= deadline:
                    raise ValueError(f"开发服务在 {startup_timeout} 秒内未就绪，已停止本次进程。请查看日志。")
                time.sleep(0.08)
        except (OSError, ValueError, RuntimeError) as error:
            self.stop(record.service_id, reason=str(error), failed=not cancelled() and not self.closed)
            return {"service": record.public(), "reused": False}

    def stop(self, service_id: str, *, reason: str = "", failed: bool = False) -> dict:
        record = self.get(service_id)
        with record.operation_lock:
            with self.lock:
                if record.status not in _ACTIVE and (record.owned is None or record.owned.process.poll() is not None):
                    return record.public()
                record.status = "stopping"
            if record.owned is not None:
                record.owned.terminate()
                record.exit_code = record.owned.process.returncode
                if record.reader is not None:
                    record.reader.join(timeout=2)
            with self.lock:
                record.status = "failed" if failed else "stopped"
                record.error = reason
                record.settled.set()
                self._persist()
                return record.public()

    def restart(self, service_id: str, *, cancelled: Callable[[], bool] = lambda: False) -> dict:
        record = self.get(service_id)
        self.stop(service_id)
        return self.start(record.command, record.cwd, record.url, startup_timeout=record.startup_timeout,
                          workspace=record.workspace, cancelled=cancelled)

    def invoke(self, action: str, data: dict, *, workspace: str, session_id: str | None = None,
               cancelled: Callable[[], bool] = lambda: False) -> dict:
        if action == "start":
            return self.start([data["command"], *data.get("args", [])], data["cwd"], data["ready_url"],
                startup_timeout=data.get("startup_timeout", 30), workspace=workspace, session_id=session_id, cancelled=cancelled)
        service_id = data.get("service_id")
        if action == "status" and not service_id:
            return {"services": self.list(workspace=workspace)}
        record = self.get(service_id, workspace=workspace)
        if action == "status":
            return {"service": record.public()}
        if action == "logs":
            return self.logs(service_id, workspace=workspace)
        if action == "stop":
            if cancelled():
                raise ValueError("停止服务的请求已取消")
            return {"service": self.stop(service_id)}
        raise ValueError("未知开发服务操作")

    def close(self) -> None:
        atexit.unregister(self.close)
        with self.lock:
            self.closed = True
            ids = [record.service_id for record in self.records.values() if record.status in _ACTIVE]
        for service_id in ids:
            try:
                self.stop(service_id, reason="MiniCode 已退出，服务已停止。")
            except (OSError, ValueError, RuntimeError):
                # Close ownership handles even when storage is unavailable.
                record = self.records.get(service_id)
                if record is not None and record.owned is not None:
                    record.owned.terminate()
