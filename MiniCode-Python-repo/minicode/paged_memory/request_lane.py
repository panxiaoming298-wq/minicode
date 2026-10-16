"""Bound optional background requests without making a timed-out caller join it."""

from collections.abc import Callable
from concurrent.futures import Future
from threading import BoundedSemaphore, Lock, Thread
from typing import Any

# No unbounded queue or executor shutdown join if an HTTP provider stalls.
_CAPACITY = BoundedSemaphore(4)


class RequestLane:
    def __init__(self, *, capacity=None, name="paged-memory-request") -> None:
        self._lock = Lock()
        self._capacity = capacity if capacity is not None else _CAPACITY
        self._name = name
        self._pending: Future | None = None
        self._closed = False

    def submit(self, operation: Callable[[], Any]) -> Future | None:
        with self._lock:
            if self._closed or (self._pending is not None and not self._pending.done()):
                return None
            if not self._capacity.acquire(blocking=False):
                return None
            future: Future = Future()
            self._pending = future

            def run() -> None:
                try:
                    result = operation()
                except BaseException as exc:  # noqa: BLE001 - complete the Future even on worker exit
                    future.set_exception(exc)
                else:
                    future.set_result(result)
                finally:
                    self._capacity.release()

            try:
                Thread(target=run, name=self._name, daemon=True).start()
            except BaseException:
                self._pending = None
                self._capacity.release()
                raise
            return future

    def close(self, cleanup: Callable[[], Any] | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = self._pending
        if cleanup is not None:
            if pending is not None and not pending.done():
                pending.add_done_callback(lambda _future: cleanup())
            else:
                cleanup()
