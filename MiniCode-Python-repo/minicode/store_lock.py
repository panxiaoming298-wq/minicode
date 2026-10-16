"""Reentrant cross-process locks for small durable-store transactions."""

from __future__ import annotations

import functools
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from minicode.path_safety import is_link_or_reparse

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


class StoreBusyError(TimeoutError):
    """Another transaction held the durable-store lease past the deadline."""


@contextmanager
def store_lock(path: Path, *, timeout: float = 30):
    deadline = time.monotonic() + max(0, timeout)
    path = Path(os.path.abspath(path))
    key = os.path.normcase(str(path))
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=max(0, timeout)):
        raise StoreBusyError("durable store is busy")
    try:
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_link_or_reparse(path) or (path.exists() and path.stat().st_nlink > 1):
            raise OSError("unsafe store lock path")
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise StoreBusyError("durable store is busy")
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


def serialized_sessions(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        from minicode import session
        with store_lock(session.SESSIONS_DIR / ".store.lock"):
            return function(*args, **kwargs)
    return wrapped
