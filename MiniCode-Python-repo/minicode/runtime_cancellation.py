"""An activation-local stop signal linked to run cancellation and durable node intent."""
from __future__ import annotations

import threading
import time


class ToolApprovalCanceled(RuntimeError):
    """Execution stopped before a pending approval could authorize the tool."""


class ActivationCancellation:
    def __init__(self, parent, canceled):
        self.parent = parent
        self.canceled = canceled
        self.local = threading.Event()

    def is_set(self):
        return self.local.is_set() or self.parent.is_set() or self.canceled()

    def set(self):
        self.local.set()

    def wait(self, timeout=None):
        end = time.monotonic() + timeout if timeout is not None else None
        while not self.is_set():
            remaining = end - time.monotonic() if end is not None else 0.05
            if remaining <= 0:
                return False
            self.local.wait(min(0.05, remaining))
        return True
