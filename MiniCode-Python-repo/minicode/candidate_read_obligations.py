"""Version-bound tool-read obligations for omitted candidate content.

Why this exists
---------------
When a candidate is larger than the declared snapshot budget, a file can be
*reported* by hash and size instead of inlined. Telling the model "please read it
first" is not a control: nothing in the Runtime refused a write to a file whose
current content the file tools never returned. FIX09 asks for a native obligation --
the write is refused until the exact revision of that file really was read.

What this enforces
------------------
``ReadObligations`` records, for one candidate revision, the paths whose content
was omitted. A write to one of those paths is refused while the obligation is
open. Reading the file through the real read tool closes it only when the bytes
read still hash to the value the obligation was created under, so a change between
the obligation and the read re-opens it rather than silently satisfying a stale
promise. A new candidate revision retires every obligation of the old one.

What it cannot do
-----------------
It is not a sandbox and it does not make an unread file safe: it only refuses the
write. A path that is not under obligation is unaffected, and this module never
grants authority by itself. These are tool-boundary observations: request paging
can subsequently replace content with a preview. Closing this gate proves neither
full provider-request visibility nor understanding or continued residency. Task
acceptance must use independent, source-bound verification.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicode.path_safety import is_unsafe_stored_path

OBLIGATION_SCHEMA = "minicode-candidate-read-obligation-v1"


class ReadObligationUnmet(RuntimeError):
    """A write was refused because its target still has an open read obligation."""

    def __init__(self, message: str, *, paths: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.paths = tuple(sorted(set(paths)))


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("an obligation path must be a plain repository-relative string")
    if value.startswith("/") or value.startswith("//"):
        raise ValueError("an obligation path must not be absolute")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("an obligation path must not traverse or contain empty segments")
    return value


@dataclass(frozen=True, slots=True)
class Obligation:
    path: str
    sha256: str
    revision: str
    size_bytes: int
    reason: str = "content was not inlined in the candidate snapshot"

    def to_dict(self) -> dict[str, Any]:
        return {"schema": OBLIGATION_SCHEMA, "path": self.path, "sha256": self.sha256,
                "revision": self.revision, "size_bytes": int(self.size_bytes),
                "reason": self.reason}


@dataclass(slots=True)
class ReadObligations:
    """The open read obligations of ONE candidate revision."""

    candidate_id: str
    revision: str
    _open: dict[str, Obligation] = field(default_factory=dict)
    _closed: dict[str, str] = field(default_factory=dict)
    revision_observer: Callable[[], str] | None = None
    _windows: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("read obligations require a candidate id")
        if not isinstance(self.revision, str) or not self.revision:
            raise ValueError("read obligations require a revision")

    @classmethod
    def from_snapshot(cls, candidate_id: str, snapshot: Mapping[str, Any]) -> "ReadObligations":
        """Build the obligations a candidate snapshot reports as omitted."""
        obligations = cls(candidate_id=candidate_id,
                          revision=str(snapshot.get("workspace_revision") or ""))
        for entry in snapshot.get("files_omitted") or ():
            if not isinstance(entry, Mapping):
                continue
            path = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(path, str) or not isinstance(digest, str):
                continue
            obligations.add(path, digest, int(entry.get("size_bytes") or 0),
                            reason=str(entry.get("reason") or
                                       "content was not inlined in the candidate snapshot"))
        return obligations

    def add(self, path: str, sha256: str, size_bytes: int = 0, *, reason: str = "") -> Obligation:
        path = _relative(path)
        if not isinstance(sha256, str) or not sha256:
            raise ValueError("an obligation needs the content hash it was created for")
        obligation = Obligation(path=path, sha256=sha256, revision=self.revision,
                                size_bytes=int(size_bytes),
                                reason=reason or "content was not inlined in the candidate snapshot")
        self._open[path] = obligation
        self._closed.pop(path, None)
        return obligation

    def open_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._open))

    def pending(self) -> tuple[Obligation, ...]:
        return tuple(self._open[path] for path in self.open_paths())

    def closed_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._closed))

    def satisfies(self, path: str) -> bool:
        """Whether this path may be written: no obligation, or a satisfied one."""
        try:
            key = _relative(path)
        except ValueError:
            return False
        return key not in self._open

    def require_writable(self, path: str) -> None:
        """Refuse a write whose target still owes a read of its current bytes."""
        try:
            key = _relative(path)
        except ValueError as error:
            raise ReadObligationUnmet(str(error), paths=[str(path)]) from error
        obligation = self._open.get(key)
        if ((key in self._open or key in self._closed) and self.revision_observer is not None
            and self.revision_observer() != self.revision):
            raise ReadObligationUnmet("candidate changed after the version-bound read; refresh and read the current content", paths=[key])
        if obligation is not None:
            raise ReadObligationUnmet(
                f"{key} still owes a version-bound read of its current content "
                f"(revision {obligation.revision}); read it before writing it",
                paths=[key])

    def observe_read(self, path: str, content: bytes, *, revision: str) -> bool:
        """Close an obligation only when the real bytes still match it.

        Returns whether the path's obligation is now satisfied. A read of a
        different revision, or of bytes whose hash moved, does not close it: the
        obligation was created against specific bytes and only those bytes satisfy it.
        """
        key = _relative(path)
        obligation = self._open.get(key)
        if obligation is None:
            return True
        if revision != obligation.revision:
            return False
        if hashlib.sha256(content).hexdigest() != obligation.sha256:
            return False
        self._open.pop(key, None)
        self._closed[key] = revision
        return True

    def observe_window(self, path, content, *, revision, start, end, total):
        """Accumulate native tool-return ranges at one revision, before request paging."""
        with self._lock:
            key = _relative(path)
            obligation = self._open.get(key)
            if obligation is None:
                return True
            if revision != obligation.revision or hashlib.sha256(content).hexdigest() != obligation.sha256:
                self._windows.pop(key, None)
                return False
            if not 0 <= start <= end <= total:
                return False
            windows = self._windows.setdefault(key, [])
            windows.append((start, end))
            frontier = 0
            for first, last in sorted(windows):
                if first > frontier:
                    break
                frontier = max(frontier, last)
            if frontier >= total:
                self._windows.pop(key, None)
                return self.observe_read(key, content, revision=revision)
            return False

    def revision_advanced(self, revision: str) -> bool:
        return revision != self.revision

    def snapshot(self) -> dict[str, Any]:
        return {"schema": OBLIGATION_SCHEMA, "candidate_id": self.candidate_id,
                "revision": self.revision, "open": [item.to_dict() for item in self.pending()],
                "closed": sorted(self._closed), "open_count": len(self._open)}


_BOUND: ContextVar["ReadObligations | None"] = ContextVar(
    "minicode_read_obligations", default=None)


def bind_read_obligations(obligations: ReadObligations | None):
    """Bind the obligations the tool boundary must enforce, like `bind_usage`.

    The binding is a :class:`contextvars.ContextVar`, so it is scoped to the
    current execution context rather than to the process. Two parallel Workers
    therefore cannot make each other's obligations invisible -- a process-global
    dictionary did exactly that, and a sibling thread clearing its own binding
    silently unbound the primary's live obligations.
    """
    from contextlib import contextmanager

    @contextmanager
    def _binding():
        token = _BOUND.set(obligations if isinstance(obligations, ReadObligations) else None)
        try:
            yield obligations
        finally:
            _BOUND.reset(token)

    return _binding()


def current_read_obligations() -> ReadObligations | None:
    bound = _BOUND.get()
    return bound if isinstance(bound, ReadObligations) else None


def enforce_write(path: str) -> None:
    """The single gate a candidate write must pass. No obligation means no gate."""
    obligations = current_read_obligations()
    if obligations is not None:
        obligations.require_writable(path)


def observe_read(path: str, content: bytes, *, revision: str) -> bool:
    """Record a native read against the bound obligations, if any."""
    obligations = current_read_obligations()
    if obligations is None:
        return True
    return obligations.observe_read(path, content, revision=revision)
