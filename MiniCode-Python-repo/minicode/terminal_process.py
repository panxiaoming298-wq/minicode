"""An owned Windows pseudoconsole with bounded output and process-tree cleanup."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from minicode.service_process import WindowsJob


class TerminalUnavailable(RuntimeError):
    pass


def powershell_path() -> str:
    if os.name != "nt":
        raise TerminalUnavailable("集成终端目前支持 Windows，请在 Windows 本机打开。")
    if importlib.util.find_spec("winpty") is None:
        raise TerminalUnavailable('终端依赖未安装，请在后端环境运行：python -m pip install -e ".[web]"，然后重启服务。')
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        raise TerminalUnavailable("未找到 PowerShell，请安装后重启 MiniCode。")
    return executable


class WindowsTerminal:
    def __init__(self, workspace: Path, cols: int, rows: int):
        from winpty import Backend, PTY

        executable = powershell_path()
        self.workspace = workspace
        self.output: queue.Queue[str] = queue.Queue(maxsize=32)
        self.finished = threading.Event()
        self.stopping = threading.Event()
        self.lock = threading.RLock()
        self.exit_code: int | None = None
        self.error = ""
        self.job: WindowsJob | None = None
        self.pty = PTY(cols, rows, backend=Backend.ConPTY)
        self.reader: threading.Thread | None = None
        self.closed = False
        environment = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        initialization = (
            "[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false); "
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
            "$OutputEncoding = [Console]::OutputEncoding"
        )
        try:
            spawned = self.pty.spawn(executable, cwd=str(workspace),
                cmdline=" " + subprocess.list2cmdline(["-NoLogo", "-NoProfile", "-NoExit", "-Command", initialization]),
                env="\0".join(f"{key}={value}" for key, value in environment.items()) + "\0")
            if not spawned or not self.pty.pid:
                raise TerminalUnavailable("PowerShell 启动失败，请检查本机运行环境。")
            self.pid = self.pty.pid
            # No profile or user input runs before process-tree ownership exists.
            self.job = WindowsJob.for_pid(self.pid)
            self.reader = threading.Thread(target=self._read, daemon=True, name=f"terminal-{self.pid}")
            self.reader.start()
        except BaseException:
            if self.job is not None:
                self.job.close()
            elif self.pty.pid:
                subprocess.run(["taskkill", "/PID", str(self.pty.pid), "/T", "/F"],
                    capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            self.pty = None
            raise

    def _read(self) -> None:
        try:
            while not self.stopping.is_set():
                data = self.pty.read(blocking=False)
                if data:
                    for start in range(0, len(data), 8192):
                        while not self.stopping.is_set():
                            try:
                                self.output.put(data[start:start + 8192], timeout=0.1)
                                break
                            except queue.Full:
                                continue
                elif not self.pty.isalive():
                    self.exit_code = self.pty.get_exitstatus()
                    break
                else:
                    self.stopping.wait(0.01)
        except Exception:
            if not self.stopping.is_set():
                if not self.pty.isalive():
                    self.exit_code = self.pty.get_exitstatus()
                else:
                    self.error = "终端输出连接中断，请重新打开终端。"
        finally:
            self.finished.set()

    def write(self, data: str) -> None:
        with self.lock:
            if self.stopping.is_set() or self.finished.is_set():
                raise EOFError("终端已结束。")
            self.pty.write(data)

    def resize(self, cols: int, rows: int) -> None:
        with self.lock:
            if not self.stopping.is_set() and not self.finished.is_set():
                self.pty.set_size(cols, rows)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.stopping.set()
            try:
                if self.job is not None:
                    self.job.terminate()
                    deadline = time.monotonic() + 3
                    while self.job.active_processes():
                        if time.monotonic() >= deadline:
                            raise OSError("终端子进程清理尚未完成。")
                        time.sleep(0.02)
            finally:
                if self.job is not None:
                    self.job.close()
                    self.job = None
            if self.reader is not None:
                self.reader.join(timeout=2)
                if self.reader.is_alive():
                    raise OSError("终端输出线程尚未退出。")
            self.pty = None
            self.closed = True
            self.finished.set()
