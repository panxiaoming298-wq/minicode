"""Compressed, shared history objects; completed turns retain small manifests.

Sequences use persistent 32-item chunks. An unchanged prefix is reused, while
compaction creates a new sequence without invalidating older turn manifests.
All callers hold the session-store lock, including garbage collection.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from minicode.path_safety import is_unsafe_stored_path
from minicode.history_storage import active_history


class HistoryObjectTooLarge(ValueError):
    """The source requires an explicit read instead of a bounded history scan."""


def _root() -> Path:
    from minicode import session
    boundary = session.SESSIONS_DIR.absolute()
    if os.name == "nt":
        value = str(boundary)
        if not value.startswith("\\\\?\\"):
            value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
        boundary = Path(value)
    root = boundary / "history_objects"
    if is_unsafe_stored_path(root, boundary=boundary):
        raise OSError("unsafe history object store")
    return root


def _valid_object_key(key: str) -> bool:
    return isinstance(key, str) and len(key) == 64 and all(c in "0123456789abcdef" for c in key)


def _path(key: str) -> Path:
    if not _valid_object_key(key):
        raise ValueError("invalid history object reference")
    path = _root() / f"{key}.json.gz"
    if is_unsafe_stored_path(path, boundary=_root(), reject_hardlinks=True):
        raise OSError("unsafe history object")
    return path


def put(value: Any, *, kind: str = "value") -> str:
    raw = json.dumps({"kind": kind, "data": value}, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    key = hashlib.sha256(raw).hexdigest()
    authority = active_history()
    if authority:
        authority.put_object(key, gzip.compress(raw, mtime=0))
        return key
    path = _path(key)
    if path.exists():
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".object-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(gzip.compress(raw, mtime=0))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return key


def read(key: str, *, max_bytes: int | None = None) -> dict:
    return decode_object(key, copy_object(key), max_bytes=max_bytes)


def copy_object(key: str, *, max_bytes: int | None = None) -> bytes:
    """Copy immutable compressed bytes while the caller holds the history lease."""
    authority = active_history()
    if authority:
        return authority.read_object(key, max_bytes=max_bytes)
    with _path(key).open("rb") as source:
        compressed = source.read() if max_bytes is None else source.read(max_bytes + 1)
    if max_bytes is not None and len(compressed) > max_bytes:
        raise HistoryObjectTooLarge("Saved history source exceeds the search byte budget.")
    return compressed


def decode_object(key: str, compressed: bytes, *, max_bytes: int | None = None) -> dict:
    """Verify/decode a copied object without holding the store lease."""
    if max_bytes is None:
        raw = gzip.decompress(compressed)
    else:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as source:
            raw = source.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise HistoryObjectTooLarge("Saved history source exceeds the search byte budget.")
    if hashlib.sha256(raw).hexdigest() != key:
        raise ValueError("history object checksum mismatch")
    return json.loads(raw)


def search_tool_aliases(display, raw):
    """Only pair unambiguous receipts; preserve repeated calls and attempts."""
    def identity(value):
        call_id = value.get("toolUseId")
        if not call_id:
            return None
        return tuple(value.get(key, "") for key in ("nodeId", "toolUseId", "toolName"))

    by_raw, by_display = {}, {}
    for values, grouped, is_display in ((raw, by_raw, False), (display, by_display, True)):
        for index, value in enumerate(values):
            if not isinstance(value, dict) or (is_display and value.get("role") != "tool_result"):
                continue
            key = identity(value)
            if key:
                grouped.setdefault(key, []).append(index)
    aliases = {}
    for key, targets in by_raw.items():
        sources = by_display.get(key, [])
        # A legacy receipt may lack attempt metadata. Never guess when the same
        # call ID occurs more than once in either sequence.
        for source in sources:
            message = display[source]
            compatible = [target for target in targets if all(
                message.get(field, "") == raw[target].get(field, "")
                for field in ("attemptId", "executionId", "isError"))]
            if len(sources) == len(targets) == 1:
                compatible = [target for target in targets if all(
                    not message.get(field) or not raw[target].get(field)
                    or message[field] == raw[target][field]
                    for field in ("attemptId", "executionId"))
                    and bool(message.get("isError")) == bool(raw[target].get("isError"))]
            if len(compatible) == 1 and (len(sources) == len(targets) == 1
                    or (message.get("attemptId") and message.get("executionId"))):
                aliases[str(source)] = compatible[0]
    return aliases


def sequence(values: list) -> str | None:
    previous = None
    for start in range(0, len(values), 32):
        previous = put({"previous": previous, "items": [put(v) for v in values[start:start + 32]]},
                       kind="sequence")
    return previous


def expand(key: str | None) -> list:
    chunks = []
    seen = set()
    while key:
        if key in seen:
            raise ValueError("history sequence cycle")
        seen.add(key)
        value = read(key)
        if value["kind"] != "sequence":
            raise ValueError("invalid history sequence")
        chunks.append(value["data"]["items"])
        key = value["data"]["previous"]
    return [read(item)["data"] for chunk in reversed(chunks) for item in chunk]


def sequence_refs(key: str | None, *, deadline: float | None = None) -> list[str]:
    """Read the small chunk directory without decoding individual messages."""
    chunks, seen = [], set()
    while key:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("History directory read deadline exceeded.")
        if key in seen:
            raise ValueError("history sequence cycle")
        seen.add(key)
        value = read(key)
        if value["kind"] != "sequence":
            raise ValueError("invalid history sequence")
        chunks.append(value["data"]["items"])
        key = value["data"]["previous"]
    return [item for chunk in reversed(chunks) for item in chunk]


def encode(snapshot: dict) -> dict:
    from minicode.session_display import summarize_messages
    result = {key: value for key, value in snapshot.items()
              if key not in {"messages", "displayMessages", "history", "contextState", "rawToolResults", "fileChanges"}}
    if snapshot.get("fileChanges") is not None:
        from minicode.turn_changes import change_summary
        result.update(fileChanges=change_summary(snapshot["fileChanges"]), changesRef=put(snapshot["fileChanges"]))
    messages = list(snapshot.get("messages") or [])
    system = messages.pop(0) if messages and messages[0].get("role") == "system" else None
    result.update(formatVersion=2, displaySummary=summarize_messages(snapshot.get("displayMessages") or [], snapshot.get("prompt", "")),
                  systemRef=put(system) if system else None,
                  messagesRef=sequence(messages), displayRef=sequence(snapshot.get("displayMessages") or []),
                  historyRef=sequence(snapshot.get("history") or []),
                  rawToolsRef=sequence(snapshot.get("rawToolResults") or []),
                  contextRef=put(snapshot.get("contextState") or {}),
                  searchToolAliases=search_tool_aliases(snapshot.get("displayMessages") or [],
                                                      snapshot.get("rawToolResults") or []))
    return result


def decode(manifest: dict, *, display_only: bool = False) -> dict:
    if manifest.get("formatVersion") != 2:
        return manifest
    value = dict(manifest)
    value["displayMessages"] = expand(manifest.get("displayRef"))
    if not display_only:
        value["messages"] = expand(manifest.get("messagesRef"))
        if manifest.get("systemRef"):
            value["messages"].insert(0, read(manifest["systemRef"])["data"])
        value["history"] = expand(manifest.get("historyRef"))
        value["contextState"] = read(manifest["contextRef"])["data"]
        value["rawToolResults"] = expand(manifest.get("rawToolsRef"))
        if manifest.get("changesRef"):
            value["fileChanges"] = read(manifest["changesRef"])["data"]
    return value


def collect_unreferenced() -> int:
    from minicode import session
    pending = []
    retained = set()
    authority = active_history()
    manifests = authority.manifests() if authority else []
    # Only sequence pages have outgoing references. Message/context payloads
    # are leaves: reading every retained payload here freezes the entire store
    # during a single-session deletion. Readers still verify payload checksums.
    for path in ([] if authority else session.SESSIONS_DIR.glob("*/history/*.json")):
        if path.name == "index.json":
            continue
        if is_unsafe_stored_path(path, boundary=session.SESSIONS_DIR, reject_hardlinks=True):
            raise OSError("unsafe retained history manifest")
        value = json.loads(path.read_text(encoding="utf-8"))
        manifests.append(value)
    for value in manifests:
        if not isinstance(value, dict) or any(
            value.get(key) and not _valid_object_key(value[key]) for key in
            ("messagesRef", "systemRef", "displayRef", "historyRef", "contextRef", "rawToolsRef", "changesRef")
        ):
            raise ValueError("invalid history manifest reference")
        pending.extend(value[key] for key in ("messagesRef", "displayRef", "historyRef", "rawToolsRef") if value.get(key))
        retained.update(value[key] for key in ("systemRef", "contextRef", "changesRef") if value.get(key))
    sequences = set()
    while pending:
        key = pending.pop()
        if key in sequences:
            continue
        value = read(key)
        if (value["kind"] != "sequence" or not isinstance(value["data"], dict)
                or not isinstance(value["data"].get("items"), list)
                or "previous" not in value["data"]
                or any(not _valid_object_key(item) for item in value["data"]["items"])
                or (value["data"].get("previous") is not None
                    and not _valid_object_key(value["data"]["previous"]))):
            raise ValueError("invalid history sequence")
        sequences.add(key)
        retained.add(key)
        retained.update(value["data"]["items"])
        if value["data"]["previous"]:
            pending.append(value["data"]["previous"])
    # Establish that every ownership edge has a stored identity before deleting
    # anything, without decompressing leaf payloads under the store lease.
    if authority:
        if not retained.issubset(authority.object_keys()):
            raise ValueError("session history object is missing")
        return authority.collect(retained)
    for key in retained:
        if not _path(key).is_file():
            raise ValueError("session history object is missing")
    removed = 0
    for path in _root().glob("*.json.gz"):
        key = path.name.removesuffix(".json.gz")
        if key not in retained:
            _path(key).unlink()
            removed += 1
    return removed
