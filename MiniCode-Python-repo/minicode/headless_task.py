"""Explicit, bounded Headless task acceptance files (never auto-discovered)."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any

from minicode.path_safety import is_unsafe_stored_path

SCHEMA = "minicode-headless-task-v1"
MAX_BYTES = 65_536
FIELDS = {"schemaVersion", "taskConstraints", "acceptanceCriteria", "expectedOutputs",
          "taskRequiresWorkspaceWrite", "runtimeVerificationCommands", "runtimeVerificationIsolation"}
COMMAND_FIELDS = {"name", "kind", "command", "browser", "timeout", "covers", "isolate"}
COMMAND_KINDS = {"unit_test", "integration_test", "lint_format", "type_check", "static_analysis", "invariant", "browser_render"}
RESERVED_NAMES = {"expected-outputs", "integrated-runtime-state", "required-consultation", "structured-node-result"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("task file contains duplicate JSON keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("task file contains a non-finite JSON number")


def _strings(value, field: str, *, limit: int, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or (nonempty and not value):
        raise ValueError(f"{field} must be a {'non-empty ' if nonempty else ''}list of at most {limit} strings")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 4096 or "\x00" in item for item in value):
        raise ValueError(f"{field} requires non-blank strings without NUL, at most 4096 characters each")
    return list(value)


def _outputs(value, cwd: Path) -> list[str]:
    result = _strings(value, "expectedOutputs", limit=64)
    seen = set()
    for item in result:
        path = PurePosixPath(item)
        if ("\\" in item or ":" in item or path.is_absolute() or PureWindowsPath(item).drive
            or any(character in '<>"|?*' or ord(character) < 32 for character in item)
            or path.as_posix() != item or any(part in {".", ".."} for part in path.parts) or item == "."
            or any(part.rstrip(" .") != part or PureWindowsPath(part).is_reserved() for part in path.parts)):
            raise ValueError("expectedOutputs must use canonical workspace-relative paths with forward slashes")
        if item.casefold() in seen:
            raise ValueError("expectedOutputs contains duplicate or case-aliased paths")
        if is_unsafe_stored_path(cwd / item, boundary=cwd, reject_hardlinks=True):
            raise ValueError("expectedOutputs cannot point outside the workspace or through filesystem aliases")
        seen.add(item.casefold())
    return result


def _commands(value, criteria_count: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("runtimeVerificationCommands must contain 1 to 16 commands")
    names, covered, result = set(), set(), []
    for item in value:
        if not isinstance(item, dict) or set(item) - COMMAND_FIELDS:
            raise ValueError("verification command contains unsupported fields")
        name = item.get("name")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name)
            or name in names or name in RESERVED_NAMES):
            raise ValueError("verification commands require unique bounded names, excluding built-in verifier names")
        kind = item.get("kind", "browser_render" if "browser" in item else "integration_test")
        if not isinstance(kind, str) or kind not in COMMAND_KINDS:
            raise ValueError("verification command kind must be a supported deterministic check")
        if "browser" in item:
            from minicode.browser_verification import browser_declaration
            if "command" in item or kind != "browser_render":
                raise ValueError("browser checks use browser_render and cannot also declare a command")
            execution = {"browser": browser_declaration(item["browser"])}
        else:
            execution = {"command": _strings(item.get("command"), "command", limit=128, nonempty=True)}
        timeout = item.get("timeout", 30)
        if type(timeout) not in {int, float} or not 0 < timeout <= 300 or not math.isfinite(timeout):
            raise ValueError("verification timeout must be finite, positive and at most 300 seconds")
        covers = item.get("covers")
        if (not isinstance(covers, list) or not covers or len(covers) > criteria_count
            or any(type(number) is not int or not 1 <= number <= criteria_count for number in covers)
            or len(set(covers)) != len(covers)):
            raise ValueError("verification covers must contain distinct 1-based acceptance-criterion numbers")
        names.add(name)
        covered.update(covers)
        result.append({"name": name, "kind": kind, **execution, "timeout": timeout, "covers": list(covers)})
        if "isolate" in item:
            if type(item["isolate"]) is not bool:
                raise ValueError("verification command isolate must be a boolean")
            result[-1]["isolate"] = item["isolate"]
    if covered != set(range(1, criteria_count + 1)):
        raise ValueError("every acceptance criterion must be covered by a verification command")
    return result


@dataclass(frozen=True, slots=True)
class HeadlessTask:
    """An immutable parsed snapshot; later file edits cannot change this run."""
    document_json: str
    source_sha256: str
    source_bytes: bytes = b""

    def fields(self) -> dict[str, Any]:
        return json.loads(self.document_json)

    def summary(self) -> dict[str, Any]:
        fields = self.fields()
        return {"schemaVersion": SCHEMA, "sha256": self.source_sha256,
                "criteriaCount": len(fields["acceptanceCriteria"]),
                "expectedOutputCount": len(fields["expectedOutputs"]),
                "verificationCommandCount": len(fields["runtimeVerificationCommands"]),
                "requiresWorkspaceWrite": fields["taskRequiresWorkspaceWrite"]}

    def apply(self, runtime: dict[str, Any]) -> dict[str, Any]:
        from minicode.unified_runtime import _as_bool

        if not _as_bool(runtime.get("runtimeV2", True)):
            raise ValueError("task files require the unified Runtime; select --runtime legacy or primary")
        return {**runtime, **self.fields(), "_headlessTaskFile": self.summary()}


def load_headless_task(filename: str, cwd: str | Path) -> HeadlessTask:
    """Load only an explicitly selected local file; never run its commands here.

    Commands are trusted user configuration executed by the existing Runtime
    verifier during a real run. This parser is not a command or OS sandbox.
    """
    root = Path(cwd).absolute()
    path = Path(filename)
    path = (path if path.is_absolute() else root / path).absolute()
    if is_unsafe_stored_path(path, boundary=Path(path.anchor), reject_hardlinks=True) or not path.is_file():
        raise ValueError("task file must be an existing regular file without filesystem aliases")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    return parse_headless_task(raw, root)


def parse_headless_task(raw: bytes, cwd: str | Path) -> HeadlessTask:
    """Validate the same bounded format from an already selected snapshot."""
    root = Path(cwd).absolute()
    if not isinstance(raw, bytes):
        raise ValueError("task snapshot must contain UTF-8 bytes")
    if len(raw) > MAX_BYTES:
        raise ValueError("task file exceeds the 65536-byte limit")
    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("task file must contain a valid UTF-8 JSON object") from error
    if not isinstance(document, dict) or set(document) - FIELDS or document.get("schemaVersion") != SCHEMA:
        raise ValueError(f"task file must use schemaVersion {SCHEMA!r} and supported fields only")
    if type(document.get("taskRequiresWorkspaceWrite")) is not bool:
        raise ValueError("taskRequiresWorkspaceWrite must be explicitly true or false")
    criteria = _strings(document.get("acceptanceCriteria"), "acceptanceCriteria", limit=64, nonempty=True)
    if len(set(criteria)) != len(criteria):
        raise ValueError("acceptanceCriteria cannot contain duplicates")
    fields = {
        "taskRequiresWorkspaceWrite": document["taskRequiresWorkspaceWrite"],
        "taskConstraints": _strings(document.get("taskConstraints", []), "taskConstraints", limit=32),
        "acceptanceCriteria": criteria,
        "expectedOutputs": _outputs(document.get("expectedOutputs", []), root),
        "runtimeVerificationCommands": _commands(document.get("runtimeVerificationCommands"), len(criteria)),
    }
    if "runtimeVerificationIsolation" in document:
        if type(document["runtimeVerificationIsolation"]) is not bool:
            raise ValueError("runtimeVerificationIsolation must be a boolean")
        fields["runtimeVerificationIsolation"] = document["runtimeVerificationIsolation"]
    return HeadlessTask(json.dumps(fields, ensure_ascii=False, sort_keys=True), hashlib.sha256(raw).hexdigest(), raw)
