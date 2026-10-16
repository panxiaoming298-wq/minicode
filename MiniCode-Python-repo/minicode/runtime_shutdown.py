"""Keep run and workspace ownership until canceled executions really settle."""
from __future__ import annotations

from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
import logging
import threading


logger = logging.getLogger(__name__)


class ExecutionDrain:
    """A bounded caller wait must not shorten the lifetime of a live writer.

    Futures remain registered through late receipt collection, not merely until
    Future.done(). No exit receipt, replay permission or file rollback is invented.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: dict[Future, str] = {}
        self._ownership: ExitStack | None = None
        self._transferred = False
        self._closing = False

    @property
    def pending_nodes(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(sorted(set(self._pending.values())))

    def wait(self, timeout: float) -> bool:
        """Wait for actual execution, receipt collection and deferred lock release."""
        with self._condition:
            return self._condition.wait_for(lambda: not self._pending and not self._closing, timeout)

    def track(self, future: Future, *, node_id: str, collect) -> None:
        with self._condition:
            if self._transferred:
                raise RuntimeError("cannot admit execution after run ownership was transferred")
            if future in self._pending:
                return
            self._pending[future] = node_id

        def settled(completed):
            try:
                collect(completed)
            except Exception:
                logger.exception("Late canceled worker receipt collection failed: %s", node_id)
            finally:
                with self._condition:
                    self._pending.pop(completed, None)
                    ownership = self._take_ownership()
                    self._condition.notify_all()
                self._close(ownership)

        future.add_done_callback(settled)

    def _take_ownership(self):
        if self._pending or self._ownership is None:
            return None
        ownership, self._ownership = self._ownership, None
        self._closing = True
        return ownership

    def _close(self, ownership):
        if ownership is None:
            return
        try:
            ownership.close()
        finally:
            with self._condition:
                self._closing = False
                self._condition.notify_all()

    @contextmanager
    def ownership(self, *leases):
        """Transfer existing OS locks to late collectors when the caller returns."""
        with ExitStack() as stack:
            acquired = tuple(stack.enter_context(lease) for lease in leases)
            try:
                yield acquired
            finally:
                with self._condition:
                    self._transferred = True
                    self._ownership = stack.pop_all()
                    ownership = self._take_ownership()
                self._close(ownership)
