"""Native read-set capture for one Worker activation.

Why this exists
---------------
A patch's cross-version admission argument is "the files this Builder really read
did not change underneath it". That argument is only as good as the read set, and
deriving the read set from *changed paths* is circular: it says the Builder read
exactly the files it wrote, so a change to some other file it genuinely depended
on is invisible and the patch is admitted anyway.

What this records
-----------------
Every observation a real tool made during one activation, as native facts:

* ``reads`` -- path to the sha256 of the content the tool really returned, or the
  empty string when the tool observed that the file does not exist;
* ``directories`` -- directories a tool really enumerated, so a file that appears
  in one later invalidates the patch instead of being missed;
* ``dynamic`` -- set when a tool ran something whose file access cannot be
  attributed (a shell command, a test runner), which makes the whole set
  INCOMPLETE and therefore ineligible for cross-version admission.

What it cannot prove
--------------------
It observes the tools this process ran through the Runtime's registry. A child
process that reads files on its own is exactly the ``dynamic`` case, and is
reported as unknown rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

READ_SET_SCHEMA = "minicode-native-read-set-v1"

# Tools whose file access cannot be attributed to a path. Running one makes the
# activation's read set incomplete; "we do not know what it read" must never be
# recorded as "it read nothing".
_DYNAMIC_TOOLS = frozenset({
    "run_command", "test_runner", "run_tests", "python", "shell", "bash", "pwsh",
    "make", "git",
})


class NativeReadSet:
    """The observations one activation really made, keyed by candidate root."""

    def __init__(self, *, root: Path | str | None = None, revision: str = "") -> None:
        self.root = Path(root).resolve() if root is not None else None
        self.revision = str(revision or "")
        self._reads: dict[str, str] = {}
        self._directories: set[str] = set()
        self._directory_hashes: dict[str, str] = {}
        self._unproven_directories: set[str] = set()
        self._dynamic: set[str] = set()
        self._search_results: dict[int, tuple[Any, str, str]] = {}
        self._lock = threading.Lock()

    # ---- recording -----------------------------------------------------

    @classmethod
    def from_snapshot(cls, value, *, root):
        result = cls(root=root, revision=str((value or {}).get("revision", "")))
        if not isinstance(value, Mapping) or value.get("schema") != READ_SET_SCHEMA:
            result.record_dynamic("restored-read-set-unavailable")
            return result
        for name, digest in value.get("read_hashes", {}).items():
            if digest == "":
                result.record_missing(name)
            else:
                result.note_observed(name, digest)
        for directory in value.get("read_directories", ()):
            result.record_directory(directory, digest=value.get("directory_hashes", {}).get(directory))
        for omission in value.get("omissions", ()):
            result.record_dynamic(omission)
        if value.get("complete") is not True:
            result.record_dynamic("restored-incomplete")
        return result

    def record_read(self, path: Path | str, content: bytes) -> None:
        """Record the bytes a real tool returned for one path."""
        name = self._relative(path)
        if name is None:
            self.record_dynamic("read-outside-candidate")
            return
        with self._lock:
            self._reads[name] = hashlib.sha256(content).hexdigest()

    def record_missing(self, path: Path | str) -> None:
        """Record that a tool observed a path and it was NOT there."""
        name = self._relative(path)
        if name is None:
            self.record_dynamic("missing-path-outside-candidate")
            return
        with self._lock:
            self._reads[name] = ""

    def record_search_result(self, arguments: Mapping[str, Any], result: Any,
                             path: Path, content: bytes) -> None:
        """Bind a real single-file search receipt to the exact bytes fed to rg.

        These ephemeral object bindings are consumed once by the registry. An
        older read/hash, a rendered string, or restored state cannot authenticate
        a subsequent search whose producer did not capture its actual input.
        """
        self.record_read(path, content)
        self.authenticate_search_result(arguments, result)

    def authenticate_search_result(self, arguments: Mapping[str, Any], result: Any) -> None:
        """Bind a producer's completed snapshot search, including empty scopes."""
        with self._lock:
            self._search_results[id(result)] = (result,
                json.dumps(arguments, sort_keys=True, ensure_ascii=False),
                hashlib.sha256(result.output.encode("utf-8")).hexdigest())

    def consume_search_result(self, arguments: Any, result: Any) -> bool:
        with self._lock:
            proof = self._search_results.pop(id(result), None)
        return bool(proof is not None and proof[0] is result and result.ok
            and proof[1] == json.dumps(arguments, sort_keys=True, ensure_ascii=False)
            and proof[2] == hashlib.sha256(result.output.encode("utf-8")).hexdigest())

    def record_directory(self, path: Path | str, *, digest: str | None = None) -> None:
        """Record that a tool really enumerated one directory."""
        name = self._relative(path)
        if name is None:
            self.record_dynamic("directory-outside-candidate")
            return
        with self._lock:
            self._directories.add(name)
            self._directory_hashes.pop(name, None)
            if digest is None:
                self._unproven_directories.add(name)
            if (name not in self._unproven_directories and isinstance(digest, str)
                    and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
                self._directory_hashes[name] = digest

    def record_dynamic(self, tool_name: str) -> None:
        """Record that a tool's file access could not be attributed."""
        with self._lock:
            self._dynamic.add(str(tool_name))

    def prepare_file_creation(self, path: Path) -> dict[str, tuple[str, str]]:
        """Carry directory observations forward over a native file creation.

        Only a previously observed, still identical membership is eligible.
        Predict the single new file/ancestor entry rather than refreshing the
        whole read set, which could silently incorporate unrelated changes.
        """
        from minicode.directory_observation import directory_observation, membership_digest

        name = self._relative(path)
        if self.root is None or name is None:
            return {}
        updates = {}
        for directory, old_digest in self.directory_hashes().items():
            prefix = "" if directory == "." else directory + "/"
            if not name.startswith(prefix):
                continue
            child, separator, _ = name[len(prefix):].partition("/")
            try:
                entries, observed = directory_observation(self.root / directory, root=self.root)
                if observed != old_digest:
                    raise OSError("directory changed outside this observed creation")
                projected = dict(entries)
                expected_kind = "dir" if separator else "file"
                if projected.get(child) not in {None, expected_kind}:
                    raise OSError("creation conflicts with observed path type")
                projected[child] = expected_kind
                updates[directory] = (old_digest, membership_digest(projected.items()))
            except (OSError, ValueError, RuntimeError):
                self.record_dynamic("file-creation:directory-observation-changed")
        return updates

    def finish_file_creation(self, updates: Mapping[str, tuple[str, str]]) -> None:
        """Publish only an exact, successful creation's membership transition."""
        from minicode.directory_observation import directory_observation

        for directory, (old_digest, expected_digest) in updates.items():
            try:
                _, actual = directory_observation(self.root / directory, root=self.root)
                if actual != expected_digest:
                    raise OSError("creation did not match the predicted membership")
            except (OSError, ValueError, RuntimeError):
                self.record_dynamic("file-creation:directory-observation-unverified")
                continue
            with self._lock:
                # A concurrent listing may have recorded newer native evidence;
                # never overwrite it or revive an unproven recursive listing.
                if self._directory_hashes.get(directory) == old_digest:
                    self._directory_hashes[directory] = expected_digest

    def note_observed(self, path: Path | str, digest: str) -> None:
        """Record an observation whose hash was captured elsewhere (a snapshot).

        The Runtime's context snapshot names the bytes the Worker was handed, so
        those paths and hashes are genuine read evidence; recording them keeps the
        read set complete without pretending a tool call produced them.
        """
        name = self._relative(path)
        if name is None:
            try:
                name = Path(path).as_posix()
            except (TypeError, ValueError):
                return
        if not isinstance(digest, str) or len(digest) != 64:
            self.record_dynamic(f"snapshot:{name}")
            return
        with self._lock:
            self._reads.setdefault(name, digest)

    def observe_call(self, tool_name: str, arguments: Mapping[str, Any] | None,
                     result_ok: bool, output: str = "") -> None:
        """Record what one real tool call observed, from its own arguments."""
        if not isinstance(tool_name, str) or not tool_name:
            return
        if tool_name in _DYNAMIC_TOOLS:
            self.record_dynamic(tool_name)
            return
        if tool_name == "discover_tools":
            return
        if not isinstance(arguments, Mapping):
            return
        if tool_name in {"grep_files", "code_nav", "diff_viewer", "web_search"}:
            # These tools do not yet provide a complete byte-level observation.
            self.record_dynamic(tool_name)
            return
        if tool_name not in {"read_file", "list_files", "file_tree", "write_file", "edit_file"}:
            self.record_dynamic(f"unattributed:{tool_name}")
            return
        for key in ("path", "file_path", "target", "target_path", "filename"):
            raw = arguments.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            if tool_name in {"read_file", "code_nav", "diff_viewer"}:
                full = self._absolute(raw)
                if full is None:
                    continue
                if result_ok and self._relative(full) not in self.read_hashes():
                    self.record_dynamic("read_file:missing-native-byte-observation")
                elif not result_ok:
                    # A failed read is not evidence of absence: the tool may have
                    # been refused by policy rather than finding no file.
                    if self.read_hashes().get(self._relative(full)) != "":
                        self.record_dynamic(f"{tool_name}:unreadable")
            elif tool_name in {"list_files", "file_tree"}:
                full = self._absolute(raw)
                if full is not None and result_ok:
                    # list_files records its exact names/types in the producer,
                    # before the execution-thread binding ends. Recursive legacy
                    # listings keep their conservative directory dependency.
                    if tool_name != "list_files" or self._relative(full) not in self.directory_hashes():
                        self.record_directory(full)
                else:
                    self.record_dynamic(f"{tool_name}:unreadable")
            return
        if tool_name in {"grep_files", "web_search"}:
            # A search reads an unenumerable set of files under a root.
            raw = arguments.get("path") or arguments.get("root")
            if isinstance(raw, str) and raw:
                self.record_directory(self._absolute(raw) or raw)

    # ---- native facts --------------------------------------------------

    @property
    def complete(self) -> bool:
        """Whether EVERY observation this activation made is attributable."""
        with self._lock:
            return not self._dynamic

    @property
    def omissions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._dynamic))

    def read_hashes(self) -> dict[str, str]:
        with self._lock:
            return dict(self._reads)

    def directories(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._directories))

    def directory_hashes(self) -> dict[str, str]:
        with self._lock:
            return dict(self._directory_hashes)

    def snapshot(self) -> dict[str, Any]:
        return {"schema": READ_SET_SCHEMA, "revision": self.revision,
                "read_hashes": self.read_hashes(), "read_directories": list(self.directories()),
                "complete": self.complete, "omissions": list(self.omissions),
                "observed_paths": len(self._reads),
                **({"directory_hashes": self.directory_hashes()} if self.directory_hashes() else {})}

    # ---- helpers -------------------------------------------------------

    def _absolute(self, raw: str) -> Path | None:
        if self.root is None:
            return None
        candidate = Path(raw)
        return candidate if candidate.is_absolute() else self.root / candidate

    def _relative(self, path: Path | str) -> str | None:
        if self.root is None:
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            return candidate.resolve().relative_to(self.root).as_posix()
        except (OSError, ValueError):
            return None


_BOUND: dict[int, NativeReadSet] = {}
_BOUND_LOCK = threading.Lock()


def bind_native_read_set(read_set: NativeReadSet | None):
    """Bind the read set for THIS thread's activation, like the write boundary."""
    from contextlib import contextmanager

    @contextmanager
    def _binding():
        key = threading.get_ident()
        previous = None
        if read_set is not None:
            with _BOUND_LOCK:
                previous = _BOUND.get(key)
                _BOUND[key] = read_set
        try:
            yield read_set
        finally:
            if read_set is not None:
                with _BOUND_LOCK:
                    if previous is None:
                        _BOUND.pop(key, None)
                    else:
                        _BOUND[key] = previous

    return _binding()


def current_native_read_set() -> NativeReadSet | None:
    with _BOUND_LOCK:
        return _BOUND.get(threading.get_ident())


def observe_tool_observation(tool_name: str, arguments: Any, result: Any) -> None:
    """Record one finished tool call against the bound read set, if any."""
    read_set = current_native_read_set()
    if read_set is None:
        return
    if tool_name == "grep_files" and read_set.consume_search_result(arguments, result):
        return
    ok = bool(getattr(result, "ok", False))
    output = getattr(result, "output", "") if ok else ""
    read_set.observe_call(tool_name, arguments if isinstance(arguments, Mapping) else None,
                          ok, output if isinstance(output, str) else "")


def merge_observations(*sets: NativeReadSet | None) -> NativeReadSet:
    """One read set from several activations of the same candidate."""
    merged = NativeReadSet()
    for item in sets:
        if item is None:
            continue
        if merged.root is None:
            merged.root = item.root
        with item._lock:
            merged._reads.update(item._reads)
            merged._directories.update(item._directories)
            for name in item._directories:
                merged._directory_hashes.pop(name, None)
            merged._directory_hashes.update(item._directory_hashes)
            merged._unproven_directories.update(item._unproven_directories)
            for name in merged._unproven_directories:
                merged._directory_hashes.pop(name, None)
            merged._dynamic.update(item._dynamic)
    return merged


def read_set_is_complete(sets: Iterable[NativeReadSet | None]) -> bool:
    return all(item is None or item.complete for item in sets)
