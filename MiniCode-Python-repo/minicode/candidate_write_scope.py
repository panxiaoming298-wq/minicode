"""The candidate write boundary that the Runtime and the real file tools share.

Why this exists
---------------
A Builder writes an isolated candidate; the Runtime applies an accepted patch
into the primary candidate under a short exclusive mutation lock. Those two facts
were previously enforced in two unrelated places: the ownership registry guarded
the Runtime's copy loop, and the file tools wrote wherever their ``cwd`` pointed.
A file tool therefore wrote the same candidate the Runtime believed it held
exclusively, which is not mutual exclusion at all.

What this enforces
------------------
One candidate root, one live writer, one gate. A Worker that has been handed a
candidate binds a :class:`CandidateWriteAccess` for the duration of its
activation; every write-effect tool call in that activation is then checked
against the SAME :class:`CandidateRegistry` the Runtime uses, on the thread that
is really executing the tool:

* the writer must still hold the lease it was granted (right candidate *and*
  right owner token), so a superseded or released writer cannot write;
* the declared ``write_scope`` must allow the path, so a Builder cannot reach
  outside the bounded paths it was given;
* the resolved target must stay inside the candidate root the Runtime owns, so a
  write cannot be redirected into the user's target directory or another
  candidate by a path, a drive letter or a ``..`` segment.

What this cannot do
-------------------
It is not an OS sandbox and it does not stop a child process spawned by a shell
tool. It also cannot prove that a *caller* keeps the Runtime's mutation lock
short; it proves only that both sides pass through the same registry and the same
per-candidate cap.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from minicode.candidate_ownership import CandidateConflict, CandidateRegistry

WRITE_BOUNDARY_SCHEMA = "minicode-candidate-write-boundary-v1"


@dataclass(frozen=True, slots=True)
class CandidateWriteAccess:
    """The candidate one activation may write, and the lease that proves it."""

    candidate_id: str
    owner_token: str
    root: Path | Callable[[], Path | None]
    # How long the write may wait for the candidate's short mutation lock before
    # it is refused. A positive value serialises a real write behind a Runtime
    # apply instead of racing it; it is bounded because the lock is never held
    # across a model request.
    lock_timeout_seconds: float = 90.0
    canceled: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("a write boundary needs a candidate id")
        if not isinstance(self.owner_token, str) or not self.owner_token:
            raise ValueError("a write boundary needs the owner token of its live lease")
        if isinstance(self.lock_timeout_seconds, bool) or \
                not isinstance(self.lock_timeout_seconds, (int, float)) or \
                self.lock_timeout_seconds < 0:
            raise ValueError("lock_timeout_seconds must be a non-negative number")
        if not callable(self.root):
            object.__setattr__(self, "root", Path(self.root).resolve())

    def resolved_root(self) -> Path:
        if self.canceled is not None and self.canceled():
            raise CandidateWriteRefused("this activation's candidate write right was canceled")
        root = self.root() if callable(self.root) else self.root
        if root is None:
            raise CandidateWriteRefused("the activation has no live isolated candidate")
        return Path(root).resolve()


class CandidateWriteRefused(RuntimeError):
    """A write was refused at the candidate boundary; no bytes were written."""


_BINDINGS: dict[int, tuple[CandidateRegistry, CandidateWriteAccess]] = {}
_BINDINGS_LOCK = threading.Lock()
_CONTROL_MUTATIONS = threading.local()


def holds_candidate_control_mutation(registry: CandidateRegistry, candidate_id: str) -> bool:
    """Recognize this thread's already-held native control guard, never a model claim."""
    held = getattr(_CONTROL_MUTATIONS, "held", None)
    if held is None:
        return False
    bound_registry, access, guard = held
    if bound_registry is not registry or access.candidate_id != candidate_id or not guard.acquired:
        return False
    access.resolved_root()
    lease = registry.lease_for(candidate_id)
    return lease is not None and lease.owner_token == access.owner_token


@contextmanager
def bind_candidate_write_access(
    registry: CandidateRegistry | None,
    access: CandidateWriteAccess | None,
) -> Iterator[CandidateWriteAccess | None]:
    """Bind the candidate boundary for THIS thread's activation.

    The binding is keyed by thread because a tool call runs on the thread that
    executes the Worker. Two Builders are on different threads and therefore
    cannot see each other's candidate, and a thread that never binds (the ordinary
    non-parallel path) keeps its historical unrestricted behaviour.
    """
    key = threading.get_ident()
    if registry is None or access is None:
        yield None
        return
    with _BINDINGS_LOCK:
        previous = _BINDINGS.get(key)
        _BINDINGS[key] = (registry, access)
    try:
        yield access
    finally:
        with _BINDINGS_LOCK:
            if previous is None:
                _BINDINGS.pop(key, None)
            else:
                _BINDINGS[key] = previous


def current_candidate_write_access() -> CandidateWriteAccess | None:
    """The boundary bound on this thread, or ``None`` when nothing is bound."""
    with _BINDINGS_LOCK:
        bound = _BINDINGS.get(threading.get_ident())
    return bound[1] if bound is not None else None


def current_candidate_write_binding():
    """Transfer only this activation's capability to its tool execution thread."""
    with _BINDINGS_LOCK:
        return _BINDINGS.get(threading.get_ident())


def _registry_for_current_thread() -> CandidateRegistry | None:
    with _BINDINGS_LOCK:
        bound = _BINDINGS.get(threading.get_ident())
    return bound[0] if bound is not None else None


def _candidate_relative(root: Path, path: str | os.PathLike[str]) -> str | None:
    """The candidate-relative path, or ``None`` when it leaves the candidate.

    Purely lexical first (a drive letter, a UNC prefix, a ``..`` segment and a
    backslash are refused before any filesystem call), then confirmed against the
    resolved root. A target that escapes the candidate root -- including one that
    only escapes after following a link -- is refused.
    """
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    normalised = path.replace("\\", "/")
    if normalised.startswith("/") or (len(normalised) >= 2 and normalised[1] == ":"):
        return None
    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    relative = "/".join(parts)
    try:
        resolved = (root / relative).resolve()
        if resolved != root and root not in resolved.parents:
            return None
    except OSError:
        return None
    return relative


@contextmanager
def candidate_write_boundary(
    path: str | os.PathLike[str],
) -> Iterator[str | None]:
    """Hold the candidate's short mutation lock across one real write.

    This is the mutex itself, not a pre-check: the Runtime's apply takes the SAME
    :meth:`CandidateRegistry.mutation` guard for the same candidate, so a file tool
    and an apply cannot both be inside their write. The guard is taken for the
    duration of the write and released immediately after; it is never held across a
    model request, and a second entrant is refused rather than queued indefinitely.
    """
    access = current_candidate_write_access()
    if access is None:
        yield None
        return
    registry = _registry_for_current_thread()
    if registry is None:
        raise CandidateWriteRefused(
            "the candidate write boundary has no ownership registry to re-check")
    relative = _candidate_relative(access.resolved_root(), path)
    if relative is None:
        raise CandidateWriteRefused(
            f"path {str(path)!r} leaves the candidate this activation owns "
            f"({access.candidate_id}); a Worker writes only inside its own candidate "
            "and never the user's target directory or another candidate")
    try:
        with registry.mutation(candidate_id=access.candidate_id,
                               owner_token=access.owner_token,
                               timeout=float(access.lock_timeout_seconds), canceled=access.canceled):
            access.resolved_root()
            if not registry.scope_allows(access.candidate_id, access.owner_token, relative):
                raise CandidateWriteRefused(f"{relative!r} is outside this candidate's live write scope")
            yield relative
    except CandidateConflict as error:
        raise CandidateWriteRefused(
            f"candidate {access.candidate_id!r}: the mutation boundary is held "
            f"elsewhere, so the write of {relative!r} was refused rather than raced "
            f"({error})") from error


def enforce_candidate_write(path: str | os.PathLike[str]) -> str | None:
    """Check a write against the bound candidate boundary without holding the lock.

    Returns the candidate-relative path when the write is authorised, and raises
    :class:`CandidateWriteRefused` otherwise. With nothing bound this is a no-op,
    so callers outside a governed candidate are unaffected. Use
    :func:`candidate_write_boundary` when the check must also exclude a concurrent
    Runtime apply for the duration of the write.
    """
    access = current_candidate_write_access()
    if access is None:
        return None
    registry = _registry_for_current_thread()
    if registry is None:
        raise CandidateWriteRefused(
            "the candidate write boundary has no ownership registry to re-check")
    relative = _candidate_relative(access.resolved_root(), path)
    if relative is None:
        raise CandidateWriteRefused(
            f"path {str(path)!r} leaves the candidate this activation owns "
            f"({access.candidate_id}); a Worker writes only inside its own candidate "
            "and never the user's target directory or another candidate")
    lease = registry.lease_for(access.candidate_id)
    if lease is None:
        raise CandidateWriteRefused(
            f"candidate {access.candidate_id!r} has no live writer lease; the write "
            "would bypass the Runtime's mutation boundary")
    try:
        holds = registry.scope_allows(access.candidate_id, access.owner_token, relative)
    except CandidateConflict as error:  # pragma: no cover - scope_allows returns False
        raise CandidateWriteRefused(str(error)) from error
    if not holds:
        raise CandidateWriteRefused(
            f"candidate {access.candidate_id!r}: the live lease does not grant a "
            f"write of {relative!r} (owner token superseded, or outside the declared "
            "write scope)")
    return relative


def boundary_snapshot() -> dict[str, Any]:
    """JSON-safe view of the thread bindings; never contains an owner token."""
    with _BINDINGS_LOCK:
        bindings = {
            str(key): {"candidate_id": value[1].candidate_id, "root": str(value[1].root)}
            for key, value in _BINDINGS.items()
        }
    return {"schema": WRITE_BOUNDARY_SCHEMA, "bound_threads": len(bindings),
            "bindings": bindings}


# The argument a write-effect tool uses to name the file it changes. A tool that
# mutates the workspace under a name this list does not know is refused rather
# than waved through: "the gate did not recognise the target" must never mean
# "the gate allowed it".
_TARGET_KEYS = ("path", "file_path", "target", "target_path", "filename")


def write_target(tool_name: str, input_data: Any) -> str | None:
    """The path a write-effect tool call is about, or ``None`` when unknown."""
    if not isinstance(input_data, dict):
        return None
    for key in _TARGET_KEYS:
        value = input_data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@contextmanager
def candidate_command_boundary(cwd: str, input_data: Any) -> Iterator[str]:
    """Lease the whole candidate for a permission-governed native command.

    A process can change many files, so a directory is not a file write target.
    Require a whole-candidate lease and keep the mutation guard until the process
    exits. This is ownership coordination, not an OS process sandbox; the native
    command tool must still perform its ordinary permission/cancellation checks.
    """
    from minicode.path_safety import is_unsafe_stored_path

    access = current_candidate_write_access()
    registry = _registry_for_current_thread()
    if access is None or registry is None:
        raise CandidateWriteRefused("command execution needs a live candidate binding")
    root = access.resolved_root()
    base = Path(cwd)
    requested = input_data.get("cwd") if isinstance(input_data, dict) else None
    target = Path(requested) if requested else base
    if requested and not target.is_absolute():
        target = base / target
    if (is_unsafe_stored_path(base, boundary=root)
            or is_unsafe_stored_path(target, boundary=root) or not target.is_dir()):
        raise CandidateWriteRefused("command cwd must be an existing directory inside the owned candidate")
    try:
        with registry.mutation(candidate_id=access.candidate_id, owner_token=access.owner_token,
                               timeout=float(access.lock_timeout_seconds), canceled=access.canceled) as lease:
            if access.resolved_root() != root:
                raise CandidateWriteRefused("the candidate root changed before command execution")
            if lease is None or lease.write_scope:
                raise CandidateWriteRefused(
                    "commands require a whole-candidate write lease; a scoped writer must use file tools")
            if is_unsafe_stored_path(target, boundary=root):
                raise CandidateWriteRefused("command cwd changed outside the owned candidate")
            yield str(target.resolve())
    except CandidateConflict as error:
        raise CandidateWriteRefused(f"command candidate mutation refused: {error}") from error


def enforce_tool_write(tool_name: str, effect: Any, input_data: Any) -> str | None:
    """Apply the bound candidate boundary to one real tool invocation.

    Returns the candidate-relative target when the write is authorised (and, when a
    boundary is bound, leaves the caller owning the candidate's mutation lock).
    Only a write-effect tool is gated. A write-effect tool whose target cannot be
    read out of the arguments is refused, because a write the boundary cannot name
    is a write the boundary cannot authorise.
    """
    if current_candidate_write_access() is None:
        return None
    if getattr(effect, "value", effect) == "read_only":
        return None
    target = write_target(tool_name, input_data)
    if target is None:
        raise CandidateWriteRefused(
            f"{tool_name}: this tool changes the workspace but the bound candidate "
            "boundary cannot name the path it changes, so the write was refused")
    return enforce_candidate_write(target)


@contextmanager
def tool_candidate_write_boundary(
    tool_name: str,
    effect: Any,
    input_data: Any,
    *, candidate_control: bool = False, command_cwd: str | None = None,
) -> Iterator[str | None]:
    """Authorise one tool call and hold the candidate's mutation lock for it.

    This is the single hook a write-effect tool passes through. With no boundary
    bound on this thread it yields ``None`` and changes nothing. With a boundary
    bound it names the target, re-checks the live lease and the write scope, takes
    the candidate's short mutation lock for the duration of the call, and refuses
    the call -- before any byte is written -- when any of those fail.
    """
    access = current_candidate_write_access()
    if access is None:
        yield None
        return
    if getattr(effect, "value", effect) == "read_only":
        yield None
        return
    if command_cwd is not None:
        with candidate_command_boundary(command_cwd, input_data) as resolved_cwd:
            yield resolved_cwd
        return
    if candidate_control:
        access.resolved_root()
        registry = _registry_for_current_thread()
        if registry is None:
            raise CandidateWriteRefused("native candidate control has no ownership registry")
        guard = registry.mutation(candidate_id=access.candidate_id, owner_token=access.owner_token,
                                  timeout=float(access.lock_timeout_seconds), canceled=access.canceled)
        try:
            with guard:
                access.resolved_root()
                previous = getattr(_CONTROL_MUTATIONS, "held", None)
                _CONTROL_MUTATIONS.held = (registry, access, guard)
                try:
                    yield None
                finally:
                    _CONTROL_MUTATIONS.held = previous
        except CandidateConflict as error:
            raise CandidateWriteRefused(f"native state/control mutation refused: {error}") from error
        return
    target = write_target(tool_name, input_data)
    if target is None:
        raise CandidateWriteRefused(
            f"{tool_name}: this tool changes the workspace but the bound candidate "
            "boundary cannot name the path it changes, so the write was refused")
    with candidate_write_boundary(target) as relative:
        yield relative
