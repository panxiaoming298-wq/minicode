"""Collect deleted-session runtime records without touching project workspaces.

Deletion receipts survive partial failures. Every retry rechecks retained session
references and the run's OS ownership lock. Unknown/aliased data is retained.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile

from minicode.path_safety import is_unsafe_stored_path, is_link_or_reparse, _resolved_comparison_path
from minicode.run_ownership import RuntimeRunOwnerLease, live_holder, owner_lock_path
from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import ArtifactRef
from minicode.runtime_state import RuntimeStateStore
from minicode.session_contract import atomic_write_json, _valid_session_id
from minicode.store_lock import serialized_sessions


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_STATE_SCHEMA = re.compile(r"minicode-runtime-state-v\d+\Z")


def session_run_ids(session):
    ids = set()
    for trace in session.runtime_runs:
        if not isinstance(trace, dict):
            continue
        value = trace.get("run_id") or (trace.get("state") or {}).get("run_id")
        if isinstance(value, str) and _RUN_ID.fullmatch(value):
            ids.add(value)
    return ids


def _referenced_runs(value):
    """Protect cross-run evidence as well as a session's own executions."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("run_id") and isinstance(item, str) and _RUN_ID.fullmatch(item):
                found.add(item)
            if key == "store_key" and isinstance(item, str):
                first = item.replace("\\", "/").split("/", 1)[0]
                if _RUN_ID.fullmatch(first):
                    found.add(first)
            found.update(_referenced_runs(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_referenced_runs(item))
    return found


class RuntimeCleanup:
    def __init__(self, state_root=None):
        from minicode import session
        self.root = Path(state_root if state_root is not None else session.MINI_CODE_DIR).absolute()
        self.runs = self.root / "runtime-v2" / "runs"
        self.pending = self.root / "runtime-v2" / "deleted-sessions"
        self.artifacts = RuntimeArtifactStore(self.root / "memory-v2" / "runtime-artifacts")
        self._references_stamp = None
        self._references = set()

    def _safe(self, path, *, boundary=None):
        boundary = boundary or self.root
        # Add the Windows extended prefix to both lexical paths. Do not resolve
        # away junctions or strip significant trailing characters before checks.
        path = Path(_resolved_comparison_path(Path(path).absolute()))
        boundary = Path(_resolved_comparison_path(Path(boundary).absolute()))
        if is_unsafe_stored_path(path, boundary=boundary, reject_hardlinks=path.is_file()):
            raise ValueError("runtime cleanup path escapes or aliases its owned root")

    def queue_session(self, session):
        # A fork may be the last remaining reference to another run's evidence.
        # Queue those runs too; the retained graph and OS lease decide whether
        # they are actually collectible after this session disappears.
        ids = session_run_ids(session) | _referenced_runs(session.runtime_runs)
        if not ids:
            return
        if not _valid_session_id(session.session_id):
            raise ValueError("invalid deleted session identity")
        path = self.pending / (session.session_id + ".json")
        self._safe(path)
        if path.exists():
            previous = self._receipt(path, session.session_id)
            ids.update(previous["run_ids"])
        atomic_write_json(path, {"schema": "minicode-deleted-session-v1", "session_id": session.session_id,
                                 "run_ids": sorted(ids)})

    def _receipt(self, path, session_id):
        self._safe(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("schema") != "minicode-deleted-session-v1" or value.get("session_id") != session_id
            or not isinstance(value.get("run_ids"), list)
            or any(not isinstance(i, str) or not _RUN_ID.fullmatch(i) for i in value["run_ids"])):
            raise ValueError("invalid runtime deletion receipt")
        return value

    @serialized_sessions
    def _retained(self):
        from minicode import session as sessions
        from minicode.session import list_sessions, load_session
        # Native saves atomically replace the index under this same lock. Also
        # watch legacy files and bundle directory identities for import/removal.
        # A large maintenance batch can reuse a reference graph only while this
        # generation is unchanged; never cache merely by time or session ID.
        index = sessions._session_index_file()
        self._safe(index, boundary=sessions.MINI_CODE_DIR)
        paths = [index]
        if sessions.SESSIONS_DIR.exists():
            paths.extend(p for p in sessions.SESSIONS_DIR.iterdir()
                         if _valid_session_id(p.stem if p.suffix == ".json" else p.name))
        stamp = []
        for path in paths:
            boundary = sessions.MINI_CODE_DIR if path == index else sessions.SESSIONS_DIR
            self._safe(path, boundary=boundary)
            if path.exists():
                metadata = path.stat()
                stamp.append((str(path), metadata.st_ino, metadata.st_size, metadata.st_mtime_ns))
        stamp = tuple(sorted(stamp)) if index.is_file() else None
        if stamp is not None and stamp == self._references_stamp:
            return set(self._references)
        retained = set()
        for meta in list_sessions(include_archived=True):
            session = load_session(meta.session_id)
            if session is None:
                raise ValueError("cannot establish retained session references")
            retained.update(_referenced_runs(session.runtime_runs))
        self._references_stamp, self._references = stamp, retained
        return retained

    def _artifact_directory(self, run_id):
        directory = self.artifacts.root / run_id
        self._safe(directory, boundary=self.artifacts.root)
        if not directory.exists():
            return directory
        # Only the artifact store's paired files may be removed recursively.
        # A foreign file or link makes the entire run remain pending.
        files = set()
        for current, dirs, names in os.walk(directory, followlinks=False):
            root = Path(current)
            for name in dirs:
                self._safe(root / name, boundary=self.artifacts.root)
            for name in names:
                self._regular_file(root / name)
            files.update(root / name for name in names)
        expected = set()
        for path in sorted(files):
            if path.suffix != ".json":
                continue
            ref = ArtifactRef(**json.loads(path.read_text(encoding="utf-8")))
            blob = self.artifacts.root / ref.store_key
            if (ref.store_key.replace("\\", "/").split("/", 1)[0] != run_id
                or path.stem != ref.artifact_id or blob != path.with_suffix(".blob")
                or (blob.exists() and not blob.is_file())):
                raise ValueError("runtime artifact ownership is incomplete")
            # Corrupt payload bytes are still disposable when the owning run
            # was deleted. Metadata/path ownership, not payload validity, is
            # the deletion boundary; reading evidence still verifies its hash.
            # Removing bytes before metadata makes an interrupted collection
            # retryable: the surviving reference still establishes ownership.
            expected.add(path)
            if blob.exists():
                expected.add(blob)
        if files != expected:
            raise ValueError("runtime artifacts contain unrecognized files")
        return directory

    @staticmethod
    def _regular_file(path):
        # Ancestors were checked once for this walk directory. Checking each
        # file's own metadata avoids repeatedly resolving the same parent chain.
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or is_link_or_reparse(path):
            raise ValueError("runtime artifact is not an unaliased regular file")

    def _inspect_run(self, run_id):
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid runtime identity")
        path = self.runs / (run_id + ".json")
        self._safe(path)
        if path.exists():
            state = RuntimeStateStore(path).load_required()
            if (state.get("run_id") != run_id or not _STATE_SCHEMA.fullmatch(str(state.get("schema_version")))
                or not isinstance(state.get("nodes"), dict) or not isinstance(state.get("plans"), dict)):
                raise ValueError("unrecognized runtime state")
        self._safe(owner_lock_path(path))
        RuntimeStateStore(path)._chunks.inventory(run_id)
        directory = self._artifact_directory(run_id)
        return path, directory

    def _remove_artifacts(self, directory):
        if not directory.exists():
            return
        for current, _, names in os.walk(directory, topdown=False, followlinks=False):
            root = Path(current)
            self._safe(root, boundary=self.artifacts.root)
            for name in sorted(names, key=lambda name: (not name.endswith(".blob"), name)):
                path = root / name
                self._regular_file(path)
                path.unlink()
            root.rmdir()

    def collect_runs(self, run_ids, *, dry_run=True):
        """Explicit maintenance or a deletion receipt; never age-only eviction."""
        protected = self._retained()
        results = []
        for run_id in sorted(set(run_ids)):
            result = {"run_id": run_id}
            try:
                if run_id in protected:
                    result["status"] = "retained_reference"
                else:
                    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
                        raise ValueError("invalid runtime identity")
                    path = self.runs / (run_id + ".json")
                    self._safe(path)
                    self._safe(owner_lock_path(path))
                    if live_holder(path) is not None:
                        result["status"] = "busy"
                    elif dry_run:
                        path, directory = self._inspect_run(run_id)
                        result.update(status="eligible", state_bytes=path.stat().st_size if path.exists() else 0,
                                      artifact_directory=directory.exists())
                    else:
                        with RuntimeRunOwnerLease(path, run_id=run_id):
                            # The nonblocking OS lease excludes an execution
                            # starting after preview. Inspect again under it.
                            path, directory = self._inspect_run(run_id)
                            result["status"] = self._remove_run(run_id, path, directory)
            except (OSError, ValueError, RuntimeError, TypeError) as error:
                result.update(status="pending", reason=f"{type(error).__name__}: {error}")
            results.append(result)
        return results

    @serialized_sessions
    def _remove_run(self, run_id, path, directory):
        # Recheck references after taking the run lease, without holding the
        # session lock during the potentially long artifact inventory.
        if run_id in self._retained():
            return "retained_reference"
        self._remove_artifacts(directory)
        store = RuntimeStateStore(path)
        with store._commit_lock():
            store._chunks.inventory(run_id)
            # Remove the commit point first. An interrupted explicit deletion
            # then resumes from owned orphan pages, never a broken live state.
            path.unlink(missing_ok=True)
            store._chunks.remove(run_id)
        return "removed"

    def finish_session(self, session_id):
        from minicode.session import load_session
        if not _valid_session_id(session_id) or load_session(session_id) is not None:
            return False
        path = self.pending / (session_id + ".json")
        self._safe(path)
        if not path.exists():
            return True
        receipt = self._receipt(path, session_id)
        results = self.collect_runs(receipt["run_ids"], dry_run=False)
        if any(item["status"] not in {"removed", "retained_reference"} for item in results):
            return False
        path.unlink()
        return True

    def empty_legacy_workspaces(self, *, temporary_root=None, dry_run=True):
        """Only empty legacy shells. Nonempty unmarked snapshots are retained."""
        root = Path(temporary_root or tempfile.gettempdir()).absolute()
        results = []
        for container in sorted(root.glob("minicode-runtime-v2-*")):
            workspace = container / "workspace"
            try:
                self._safe(container, boundary=root)
                self._safe(workspace, boundary=root)
                if not workspace.is_dir() or set(container.iterdir()) != {workspace}:
                    continue
                empty_directories = []
                has_content = False
                for current, dirs, files in os.walk(workspace, followlinks=False):
                    directory = Path(current)
                    self._safe(directory, boundary=root)
                    if files:
                        has_content = True
                        break
                    for name in dirs:
                        self._safe(directory / name, boundary=root)
                    empty_directories.append(directory)
                if has_content:
                    continue
                # No recursive deletion: if new content appears, rmdir fails.
                if not dry_run:
                    for directory in reversed(empty_directories):
                        self._safe(directory, boundary=root)
                        directory.rmdir()
                    container.rmdir()
                results.append({"path": str(container), "status": "eligible" if dry_run else "removed"})
            except (OSError, ValueError) as error:
                results.append({"path": str(container), "status": "pending", "reason": str(error)})
        return results
