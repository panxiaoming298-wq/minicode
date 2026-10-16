"""Owned process groups, including Windows job lifetime and listener identity."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading


class WindowsJob:
    def __init__(self, process: subprocess.Popen):
        from ctypes import wintypes as w

        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("limits", Limits), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.api.TerminateJobObject.restype = w.BOOL
        self.api.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.api.QueryInformationJobObject.restype = w.BOOL
        self.api.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
        self.api.IsProcessInJob.restype = w.BOOL
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = Extended()
            limits.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def contains(self, pid: int) -> bool:
        from ctypes import wintypes as w
        if not self.handle:
            return False
        handle = self.api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            value = w.BOOL()
            return bool(self.api.IsProcessInJob(handle, self.handle, ctypes.byref(value)) and value.value)
        finally:
            self.api.CloseHandle(handle)

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def terminate(self) -> None:
        """Terminate only the processes registered to this owned job."""
        if not self.handle:
            raise OSError("Process job is already closed")
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self) -> int:
        """Read the kernel's live count, including children whose leader exited."""
        from ctypes import wintypes as w

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ("user_time", "kernel_time", "period_user_time", "period_kernel_time")] + [
                        (name, w.DWORD) for name in ("page_faults", "total", "active", "terminated")]

        if not self.handle:
            raise OSError("Process job is already closed")
        info = Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active


def _windows_listener_pids(port: int, family: int) -> set[int]:
    from ctypes import wintypes as w

    class Row4(ctypes.Structure):
        _fields_ = [(name, w.DWORD) for name in ("state", "local_addr", "local_port", "remote_addr", "remote_port", "pid")]

    class Row6(ctypes.Structure):
        _fields_ = [("local_addr", w.BYTE * 16), ("local_scope", w.DWORD), ("local_port", w.DWORD),
                    ("remote_addr", w.BYTE * 16), ("remote_scope", w.DWORD), ("remote_port", w.DWORD),
                    ("state", w.DWORD), ("pid", w.DWORD)]

    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG]
    api.restype = w.DWORD
    size = w.DWORD()
    api(None, ctypes.byref(size), False, family, 3, 0)  # TCP_TABLE_OWNER_PID_LISTENER
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = api(buffer, ctypes.byref(size), False, family, 3, 0)
        if result == 122:
            continue
        if result:
            raise OSError(result, "无法核对开发服务的监听进程")
        count = w.DWORD.from_buffer_copy(buffer).value
        row_type = Row6 if family == socket.AF_INET6 else Row4
        return {row.pid for offset in range(count)
                if socket.ntohs((row := row_type.from_buffer_copy(buffer, 4 + offset * ctypes.sizeof(row_type))).local_port & 0xffff) == port}
    raise OSError("监听进程表持续变化，请重试启动")


class OwnedProcess:
    def __init__(self, argv: list[str], cwd: str):
        self.lock = threading.RLock()
        self.terminated = False
        self.job: WindowsJob | None = None
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "FORCE_COLOR": "0"}
        # Isolated Python keeps minicode/types.py and project files from
        # shadowing the launch gate's standard-library imports.
        self.process = subprocess.Popen([sys.executable, "-I", "-u", str(Path(__file__).with_name("service_host.py"))],
            cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
        try:
            if os.name == "nt":
                self.job = WindowsJob(self.process)
            self.process.stdin.write((json.dumps({"argv": argv, "cwd": cwd}, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.close()
        except BaseException:
            self.terminate()
            raise

    def owns_listener(self, port: int, host: str) -> bool:
        with self.lock:
            if self.process.poll() is not None:
                return False
            if self.job is not None:
                return any(self.job.contains(pid) for pid in _windows_listener_pids(port,
                    socket.AF_INET6 if ":" in host else socket.AF_INET))
            if Path("/proc/net/tcp").exists():
                inodes = set()
                for name in ("tcp", "tcp6"):
                    for line in Path("/proc/net", name).read_text().splitlines()[1:]:
                        parts = line.split()
                        if parts[3] == "0A" and int(parts[1].split(":")[1], 16) == port:
                            inodes.add(f"socket:[{parts[9]}]")
                for directory in Path("/proc").iterdir():
                    if not directory.name.isdigit():
                        continue
                    try:
                        if os.getpgid(int(directory.name)) == self.process.pid:
                            if any(os.readlink(fd) in inodes for fd in (directory / "fd").iterdir()):
                                return True
                    except (OSError, ProcessLookupError):
                        continue
                return False
            result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                    capture_output=True, text=True, timeout=2)
            for pid in result.stdout.split():
                try:
                    if os.getpgid(int(pid)) == self.process.pid:
                        return True
                except (OSError, ValueError):
                    continue
            return False

    def terminate(self) -> None:
        with self.lock:
            if self.terminated:
                return
            if self.job is not None:
                self.job.close()
                self.job = None
            elif os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=2)
                    # Join surviving descendants even if the launcher exited.
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
            elif self.process.poll() is None:
                self.process.kill()  # Only the unopened launch gate can reach here.
            self.process.wait(timeout=5)
            self.terminated = True
