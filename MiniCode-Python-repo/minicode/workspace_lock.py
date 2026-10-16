"""Cross-process leases for the single integrated workspace Writer."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO, Self

from minicode.runtime_protocol import ExecutionFailureKind
from minicode.store_lock import store_lock
from minicode.session_contract import atomic_write_json


class WorkspaceBusyError(RuntimeError):
    """Raised when another run already owns the workspace Writer lease."""

    kind = ExecutionFailureKind.WORKSPACE_BUSY


_PROCESS_LOCK = threading.Lock()
_PROCESS_HELD: set[str] = set()


def normalize_workspace_path(workspace: Path | str) -> str:
    resolved = str(Path(workspace).expanduser().resolve())
    normalized = os.path.normcase(resolved)
    if os.name == "nt":
        if normalized.startswith("\\\\?\\unc\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
    return normalized


class WorkspaceWriteLease:
    """Keep a non-blocking OS file lock alive for one Runtime run."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        run_id: str,
        lock_root: Path | None = None,
    ) -> None:
        normalized = normalize_workspace_path(workspace)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if lock_root is None:
            from minicode.config import MINI_CODE_DIR

            lock_root = MINI_CODE_DIR / "runtime-v2" / "workspace-locks"
        self.workspace = normalized
        self.run_id = run_id
        self.path = Path(lock_root).expanduser().resolve() / f"{digest}.lock"
        if os.name == "nt":
            # Owner metadata and its same-directory atomic temporary file can
            # exceed MAX_PATH even when the configured lock root does not.
            # Only the storage path changes namespace; workspace identity above
            # remains canonical so ordinary and extended paths share one lease.
            value = str(self.path)
            if not value.startswith("\\\\?\\"):
                self.path = Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
        self._handle: BinaryIO | None = None

    def acquire(self) -> Self:
        with store_lock(self.path.parent / ".registry.lock"):
            self._check_overlapping_leases()
            return self._acquire()

    def _check_overlapping_leases(self) -> None:
        current = Path(self.workspace)
        for owner in self.path.parent.glob("*.owner.json"):
            try:
                lock_path = owner.with_name(owner.name.replace(".owner.json", ".lock"))
                # A historical workspace may no longer exist. Probe the stable
                # OS lock before resolving its path; only live owners matter.
                # The registry lock prevents a new owner racing this retirement.
                if str(lock_path) not in _PROCESS_HELD:
                    try:
                        handle = lock_path.open("r+b")
                    except FileNotFoundError:
                        self._retire_owner(owner)
                        continue
                    with handle:
                        try:
                            self._lock_file(handle)
                        except OSError:
                            pass  # Another process still owns it.
                        else:
                            try:
                                self._retire_owner(owner)
                            finally:
                                self._unlock_file(handle)
                            continue
                data = json.loads(owner.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("workspace"), str):
                    # An unreadable or incomplete ownership record names no
                    # workspace, so it can neither match nor be compared. The
                    # authoritative check is still the lock below this loop, which
                    # fails closed; a malformed record must not crash the caller.
                    continue
                other = Path(normalize_workspace_path(data["workspace"]))
                if current != other and current not in other.parents and other not in current.parents:
                    continue
                raise WorkspaceBusyError(f"workspace overlaps another Writer run: {other}")
            except FileNotFoundError:
                continue

    @staticmethod
    def _retire_owner(owner: Path) -> None:
        # Never unlink the lock file: doing so could create two lock identities
        # for one workspace. A failed metadata cleanup is safe to retry later.
        try:
            owner.unlink(missing_ok=True)
        except OSError:
            pass

    def _acquire(self) -> Self:
        key = str(self.path)
        with _PROCESS_LOCK:
            if key in _PROCESS_HELD:
                raise WorkspaceBusyError(
                    f"workspace is busy with another Writer run: {self.workspace}"
                )
            _PROCESS_HELD.add(key)
        handle: BinaryIO | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                handle = self.path.open("x+b")
            except FileExistsError:
                handle = self.path.open("r+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            try:
                self._lock_file(handle)
            except OSError as error:
                handle.close()
                raise WorkspaceBusyError(
                    f"workspace is busy with another Writer run: {self.workspace}"
                ) from error
            metadata = json.dumps(
                {
                    "run_id": self.run_id,
                    "workspace": self.workspace,
                    "pid": os.getpid(),
                    "acquired_at": time.time(),
                },
                sort_keys=True,
            ).encode("utf-8")
            handle.seek(0)
            handle.write(metadata)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            atomic_write_json(self.path.with_suffix(".owner.json"), {
                "workspace": self.workspace, "run_id": self.run_id,
            })
            self._handle = handle
            return self
        except WorkspaceBusyError:
            with _PROCESS_LOCK:
                _PROCESS_HELD.discard(key)
            raise
        except Exception:
            if handle is not None:
                try:
                    self._unlock_file(handle)
                finally:
                    handle.close()
            with _PROCESS_LOCK:
                _PROCESS_HELD.discard(key)
            raise

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            with store_lock(self.path.parent / ".registry.lock"):
                self._retire_owner(self.path.with_suffix(".owner.json"))
                self._unlock_file(handle)
                handle.close()
                with _PROCESS_LOCK:
                    _PROCESS_HELD.discard(str(self.path))
        finally:
            if not handle.closed:
                handle.close()
                with _PROCESS_LOCK:
                    _PROCESS_HELD.discard(str(self.path))

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    @staticmethod
    def _lock_file(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl = importlib.import_module("fcntl")

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_file(handle: BinaryIO) -> None:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                return
            fcntl = importlib.import_module("fcntl")

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
