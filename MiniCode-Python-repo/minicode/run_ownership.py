"""Exclusive ownership of one DURABLE Runtime state file.

The Runtime state file has exactly ONE authoritative in-memory owner: the live
process that assembled the run and holds its Scheduler/Reducer. Every durable
mutation of that run must travel through that owner's serialized command
channel.

A second object that merely re-reads the same JSON is NOT an owner. Before this
module existed, an operator surface could restore its own Reducer from disk, run
a real retirement, and persist a whole snapshot -- silently reinstating every
state field the live run had changed since that snapshot was read. Atomic file
replacement never prevented that: it makes one write complete, not one writer
exclusive.

This lease supplies the missing fact. It is a non-blocking OS file lock plus a
small owner record, so:

* the live run holds it for its whole life (competing writers fail to acquire),
* after the owning process really exits the operating system drops the lock, so
  a new process can prove there is no live holder instead of guessing,
* the owner record names the run so a foreign run/generation/Worker object is
  refused rather than silently accepted.

It is deliberately about *ownership*, not about scheduling: it grants no write
capability over any workspace and does not replace the candidate write scope.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Self

from minicode.store_lock import store_lock

RUN_OWNER_SCHEMA = "minicode-runtime-run-owner-v1"


class RunStateBusyError(RuntimeError):
    """Raised when another live process still owns this Runtime state."""


class RunStateOwnerMismatch(RuntimeError):
    """Raised when a caller presents objects that do not own this run."""


def normalize_state_path(path: str | Path) -> str:
    value = os.path.normcase(str(Path(path).expanduser().resolve()))
    if os.name == "nt":
        if value.startswith("\\\\?\\unc\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return value


def owner_lock_path(state_path: str | Path, *, lock_root: str | Path | None = None) -> Path:
    """One canonical namespace per state path, independent of caller configuration.

    ``lock_root`` remains accepted for old callers but cannot redirect authority.
    Metadata belongs to the state directory's ignored Runtime metadata subtree;
    it is never read or copied as candidate content by workspace_snapshot.
    """

    state = Path(normalize_state_path(state_path))
    digest = hashlib.sha256(str(state).encode("utf-8")).hexdigest()
    root = state.parent / ".mini-code" / "runtime-state-locks"
    lock = root / f"{digest}.owner.lock"
    # A valid state path can be near MAX_PATH already; its adjacent lock name
    # must still work. Prefixing changes Win32 transport, not lock identity.
    if os.name == "nt":
        value = str(lock)
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return lock


def read_owner_record(state_path: str | Path, *, lock_root: str | Path | None = None) -> dict[str, Any] | None:
    """The last recorded owner claim, or ``None`` when nobody ever claimed it."""

    return _read_claim(_claim_path(owner_lock_path(state_path, lock_root=lock_root)))


def _read_claim(record: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _claim_path(lock: Path) -> Path:
    return lock.with_name(f"{lock.name}.owner.json")


class RuntimeRunOwnerLease:
    """Hold the single-writer claim for one durable Runtime state file.

    ``acquire()`` is deliberately non-blocking: a run that cannot own its state
    file must fail loudly instead of queueing behind another live run.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        run_id: str,
        lock_root: str | Path | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("a run owner lease needs a run id")
        self.state_path = Path(state_path).expanduser().resolve()
        self.run_id = run_id
        self.path = owner_lock_path(self.state_path, lock_root=lock_root)
        self.record_path = _claim_path(self.path)
        self._handle: BinaryIO | None = None
        self.acquired_at: float | None = None
        self._claim: dict[str, Any] | None = None
        self._bound_reducer: Any | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def _record(self) -> dict[str, Any] | None:
        """Read the claim THIS lease publishes, never a differently-rooted one."""

        return _read_claim(self.record_path)

    def acquire(self) -> Self:
        # A writer without an owner may already be committing. Establishing a
        # new owner and anonymous commits must share the same transaction lock.
        with store_lock(self.path.with_suffix(".commit.lock")):
            return self._acquire_locked()

    def _acquire_locked(self) -> Self:
        if self._handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._record()
        with store_lock(self.path.parent / ".run-owners.lock"):
            try:
                handle = self.path.open("x+b")
            except FileExistsError:
                handle = self.path.open("r+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            try:
                _lock(handle)
            except OSError as error:
                handle.close()
                raise RunStateBusyError(
                    "another live process still owns this Runtime state: "
                    f"{self.state_path}"
                ) from error
            try:
                claim = {
                    "schema": RUN_OWNER_SCHEMA,
                    "run_id": self.run_id,
                    "state_path": str(self.state_path),
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "acquired_at": time.time(),
                    "previous_run_id": (previous or {}).get("run_id"),
                }
                encoded = json.dumps(claim, sort_keys=True).encode("utf-8")
                handle.seek(0)
                handle.write(encoded)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
                # The claim is published beside the lock: a liveness probe and the
                # holder itself must read the SAME record, whatever lock_root was
                # configured, or "who owns this?" would have two answers.
                self.record_path.write_text(
                    json.dumps(claim, sort_keys=True, indent=2), encoding="utf-8")
            except Exception:
                _unlock(handle)
                handle.close()
                raise
            self._handle = handle
            self._claim = claim
            self.acquired_at = claim["acquired_at"]
            return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        self.acquired_at = None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    def validate(self, *, run_id: str | None = None) -> dict[str, Any]:
        """Confirm the held claim still names this run and this state file."""

        if self._handle is None:
            raise RunStateOwnerMismatch("this lease does not hold the ownership claim")
        record = self._claim or self._record()
        if record is None or record.get("schema") != RUN_OWNER_SCHEMA:
            raise RunStateOwnerMismatch("the Runtime state has no recorded owner claim")
        if record.get("run_id") != self.run_id or (run_id is not None and run_id != self.run_id):
            raise RunStateOwnerMismatch(
                f"the Runtime state is owned by run {record.get('run_id')!r}, not {self.run_id!r}"
            )
        if normalize_state_path(record.get("state_path", "")) != normalize_state_path(self.state_path):
            raise RunStateOwnerMismatch("the recorded owner claim names a different state file")
        landed = self._record()
        if landed is None or any(
            landed.get(key) != record.get(key) for key in ("run_id", "state_path", "pid")
        ):
            raise RunStateOwnerMismatch(
                "the recorded owner claim was replaced while this lease was held")
        return record

    def bind(self, reducer: Any) -> Self:
        """Bind a held lease to exactly one authoritative in-memory Reducer."""
        self.validate(run_id=reducer.run_id)
        if reducer.state_path is None or normalize_state_path(reducer.state_path) != normalize_state_path(self.state_path):
            raise RunStateOwnerMismatch("the Reducer belongs to another state path")
        if self._bound_reducer is not None and self._bound_reducer is not reducer:
            raise RunStateOwnerMismatch("this ownership claim is already bound to another Reducer")
        self._bound_reducer = reducer
        reducer._store.owner_lease = self
        return self

    def validate_binding(self, reducer: Any) -> None:
        self.validate(run_id=reducer.run_id)
        if self._bound_reducer is not reducer:
            raise RunStateOwnerMismatch("the supplied Reducer is not the bound live state owner")


def live_holder(state_path: str | Path, *, lock_root: str | Path | None = None) -> dict[str, Any] | None:
    """Whether a live process still holds this state, as an executable fact.

    Returns the owner record when a live process holds the OS lock (including
    this process), otherwise ``None``. A recorded claim whose owner process has
    exited leaves no lock behind, so this is a liveness probe and not a file
    existence check.
    """

    path = owner_lock_path(state_path, lock_root=lock_root)
    if not path.is_file():
        return None
    key = os.path.normcase(os.path.abspath(path))
    with _PROCESS_LOCK:
        held_here = key in _PROCESS_HELD
    if held_here:
        # This very process owns the state, which is the strongest form of "live".
        return read_owner_record(state_path, lock_root=lock_root) or {
            "schema": RUN_OWNER_SCHEMA, "run_id": None, "unreadable": True}
    try:
        with path.open("r+b") as handle:
            try:
                _lock(handle)
            except OSError:
                return read_owner_record(state_path, lock_root=lock_root) or {
                    "schema": RUN_OWNER_SCHEMA, "run_id": None, "unreadable": True}
            _unlock(handle)
    except OSError:
        # The lock file exists but cannot be opened for writing; treat an
        # unreadable ownership record as "assume live" rather than writing blind.
        return read_owner_record(state_path, lock_root=lock_root) or {
            "schema": RUN_OWNER_SCHEMA, "run_id": None, "unreadable": True}
    return None


_PROCESS_LOCK = threading.Lock()
_PROCESS_HELD: set[str] = set()


def _lock(handle: BinaryIO) -> None:
    key = os.path.normcase(os.path.abspath(handle.name))
    with _PROCESS_LOCK:
        if key in _PROCESS_HELD:
            raise OSError("this process already holds the Runtime state ownership")
        _PROCESS_HELD.add(key)
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl = importlib.import_module("fcntl")

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        with _PROCESS_LOCK:
            _PROCESS_HELD.discard(key)
        raise


def _unlock(handle: BinaryIO) -> None:
    key = os.path.normcase(os.path.abspath(handle.name))
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        with _PROCESS_LOCK:
            _PROCESS_HELD.discard(key)


def owner_summary(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A redacted owner summary for receipts; never includes bytes or secrets."""

    if record is None:
        return None
    return {key: record.get(key) for key in ("run_id", "pid", "host", "acquired_at")}
