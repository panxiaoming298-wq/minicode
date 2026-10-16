"""Bounded, cancellable transport for ripgrep's JSON Lines protocol."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import Any, Callable


MAX_JSON_EVENT_BYTES = 1_000_000
MAX_STDERR_BYTES = 8_192


@dataclass(frozen=True)
class RipgrepReceipt:
    returncode: int
    stderr: str
    stop_reason: str | None = None
    terminated_early: bool = False


def search(
    command: list[str],
    cwd: Path,
    on_event: Callable[[dict[str, Any]], bool],
    *,
    timeout_seconds: float,
    cancellation_event: Any | None = None,
    stdin_content: bytes | None = None,
) -> RipgrepReceipt:
    """Deliver events until completion, cancellation, timeout, or a result cap.

    Both pipes are drained concurrently on Windows as well as POSIX. The queue,
    unfinished JSON event, and retained stderr all have fixed memory bounds;
    limiting the final rendered string alone would not bound subprocess output.
    """
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin_content is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    # Popen with binary PIPEs and default buffering creates BufferedReaders.
    assert isinstance(process.stdout, BufferedReader)
    assert isinstance(process.stderr, BufferedReader)
    chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=16)
    stop_readers = threading.Event()

    def publish(kind: str, chunk: bytes | None) -> None:
        while not stop_readers.is_set():
            try:
                chunks.put((kind, chunk), timeout=0.05)
                return
            except queue.Full:
                continue

    def read_pipe(kind: str, pipe: BufferedReader) -> None:
        try:
            while not stop_readers.is_set():
                chunk = pipe.read1(8_192)
                if not chunk:
                    break
                publish(kind, chunk)
        except OSError as error:
            publish("read_error", str(error).encode("utf-8", errors="replace"))
        finally:
            publish(kind, None)

    readers = [
        threading.Thread(target=read_pipe, args=(kind, pipe), name=f"minicode-rg-{kind}", daemon=True)
        for kind, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    input_errors: list[str] = []

    def write_input() -> None:
        # A captured file is searched through stdin, never reopened by rg.
        # Drain output concurrently so even large snapshots cannot deadlock.
        assert process.stdin is not None and stdin_content is not None
        try:
            view = memoryview(stdin_content)
            for offset in range(0, len(view), 8192):
                process.stdin.write(view[offset:offset + 8192])
            process.stdin.flush()
        except OSError as error:
            input_errors.append(str(error))
        finally:
            try:
                process.stdin.close()
            except OSError as error:
                input_errors.append(str(error))

    if stdin_content is not None:
        readers.append(threading.Thread(target=write_input, name="minicode-rg-stdin", daemon=True))
    pending = bytearray()
    stderr = bytearray()
    stderr_truncated = False
    stop_reason = None
    open_pipes = 2
    saw_summary = False
    terminated_early = False
    try:
        for reader in readers:
            reader.start()
        while open_pipes or process.poll() is None:
            if cancellation_event is not None and cancellation_event.is_set():
                stop_reason = "canceled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_reason = "timeout"
                break
            try:
                kind, chunk = chunks.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                open_pipes -= 1
                continue
            if kind == "read_error":
                stop_reason = "pipe_error"
                stderr.extend(chunk[:max(0, MAX_STDERR_BYTES - len(stderr))])
                break
            if kind == "stderr":
                available = MAX_STDERR_BYTES - len(stderr)
                stderr.extend(chunk[:available])
                stderr_truncated |= len(chunk) > available
                continue
            pending.extend(chunk)
            while pending:
                newline = pending.find(b"\n")
                if newline > MAX_JSON_EVENT_BYTES or (newline < 0 and len(pending) > MAX_JSON_EVENT_BYTES):
                    stop_reason = "event_limit"
                    break
                if newline < 0:
                    break
                raw = pending[:newline]
                del pending[:newline + 1]
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict) or event.get("type") not in {
                        "begin", "match", "context", "end", "summary",
                    } or not isinstance(event.get("data"), dict):
                        raise ValueError("Unexpected ripgrep JSON event")
                    saw_summary |= event["type"] == "summary"
                    if not on_event(event):
                        stop_reason = "result_limit"
                        break
                except (ValueError, KeyError, TypeError):
                    stop_reason = "invalid_output"
                    break
            if stop_reason:
                break
    finally:
        stop_readers.set()
        if process.poll() is None:
            terminated_early = True
            process.kill()
        process.wait()
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()

    if stop_reason is None and process.returncode in (0, 1) and (pending or not saw_summary):
        stop_reason = "invalid_output"
    if stop_reason is None and input_errors:
        stop_reason = "pipe_error"
    error_text = stderr.decode("utf-8", errors="replace").strip()
    if stderr_truncated:
        error_text += "\n[stderr truncated]"
    return RipgrepReceipt(process.returncode, error_text, stop_reason, terminated_early)
