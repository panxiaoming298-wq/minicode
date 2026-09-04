from __future__ import annotations

import subprocess
import threading

import minicode.tools.run_command as command_module
from minicode.tooling import ToolContext
from minicode.tools.run_command import run_command_tool


class BlockingProcess:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = None
        self.read_started = threading.Event()
        self.released = threading.Event()
        self.killed = False
        self.stdout = self.BlockingStream(self, b"partial")
        self.stderr = self.BlockingStream(self, b"")

    class BlockingStream:
        def __init__(self, process, payload: bytes) -> None:
            self.process = process
            self.payload = payload
            self.returned = False

        def read(self, _size: int = -1) -> bytes:
            self.process.read_started.set()
            self.process.released.wait(timeout=5)
            if self.returned:
                return b""
            self.returned = True
            return self.payload

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.killed:
            raise subprocess.TimeoutExpired(["python"], timeout or 0.1)
        self.returncode = -1
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -1
        self.released.set()


def test_runtime_cancel_terminates_inflight_command_without_sleep(tmp_path, monkeypatch) -> None:
    process = BlockingProcess()
    cancellation = threading.Event()
    monkeypatch.setattr(command_module.subprocess, "Popen", lambda *args, **kwargs: process)

    def kill(target):
        assert target is process
        process.kill()

    monkeypatch.setattr(command_module, "_kill_process_tree", kill)
    result_box = {}
    thread = threading.Thread(
        target=lambda: result_box.setdefault(
            "result",
            run_command_tool.run(
                {"command": "python", "args": ["-c", "pass"], "timeout": 30},
                ToolContext(cwd=str(tmp_path), cancellation_event=cancellation),
            ),
        )
    )
    thread.start()
    assert process.read_started.wait(timeout=5)
    cancellation.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert process.killed
    assert not result_box["result"].ok
    assert "canceled by parent Runtime" in result_box["result"].output


def test_governed_runtime_denies_detached_background_command(tmp_path) -> None:
    result = run_command_tool.run(
        {"command": "echo background &", "args": []},
        ToolContext(cwd=str(tmp_path), cancellation_event=threading.Event()),
    )
    assert not result.ok
    assert result.output.startswith("PolicyDenied:")
