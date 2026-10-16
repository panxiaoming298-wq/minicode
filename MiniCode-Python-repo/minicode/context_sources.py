"""Inspectable source receipts and read-only memory inventory.

Receipts are made at the provider boundary, never inferred from a filename or
an assistant claim. Inspection does not repair, migrate or create memory stores.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id, atomic_write_json
from minicode.store_lock import store_lock
from minicode.task_history import TASK_CATEGORIES, HistoryPolicy

SCHEMA = "minicode-context-sources-v1"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_receipt(value: Any, session_id: str) -> bool:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("sessionId") != session_id:
        return False
    if type(value.get("capturedAt")) not in (int, float) or not math.isfinite(value["capturedAt"]):
        return False
    if not all(isinstance(value.get(key), str) for key in ("requestId", "runId", "turnId")):
        return False
    if not all(type(value.get(key)) is bool for key in ("rulesKnown", "memoryKnown", "taskMemoryKnown")):
        return False
    fields = {"rules": ("name", "path", "contentHash", "status"),
              "memories": ("id", "scope", "contentHash", "kind"),
              "notes": ("id", "type", "sourceEventId", "contentHash", "text"),
              "taskReferences": ("id", "type", "sourceEventId", "contentHash")}
    return all(isinstance(value.get(key), list) and all(
        isinstance(item, dict) and all(isinstance(item.get(field), str) for field in required)
        for item in value[key]) for key, required in fields.items())


def _path(session_id: str, sessions_dir: Path | None = None) -> tuple[Path, Path]:
    from minicode.session import SESSIONS_DIR
    if not _valid_session_id(session_id):
        raise ValueError("invalid session")
    root = Path(sessions_dir or SESSIONS_DIR)
    path = root / session_id / "context-sources.json"
    if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
        raise OSError("unsafe context source path")
    return root, path


def load_sources(session_id: str, sessions_dir: Path | None = None) -> dict[str, Any] | None:
    try:
        _, path = _path(session_id, sessions_dir)
        if not path.is_file() or path.stat().st_size > 262_144:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if _valid_receipt(value, session_id):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def capture_sources(body, runtime, *, request_id: str, view=None) -> dict[str, Any]:
    from minicode.context_usage import _text_parts
    system = list(_text_parts(body.get("system")))
    other = []
    for message in body.get("messages", []):
        if isinstance(message, dict):
            target = system if message.get("role") in {"system", "developer"} else other
            target.extend(_text_parts(message.get("content")))
    rules = []
    for layer in runtime.get("_contextSourceRules", []):
        text = layer.get("prepared_text", "")
        rules.append({
            "name": layer["name"], "path": layer["path"], "contentHash": layer.get("content_hash", ""),
            "status": "loaded" if text and any(text in part for part in system) else "not_loaded",
        })
    sections = [section for section in runtime.get("_contextUsageMemorySections", ())
                if section and any(section in part for part in [*system, *other])]
    memories = [{"id": entry["id"], "scope": entry["scope"], "contentHash": digest(entry["content"]),
                 "kind": "task_history" if entry.get("category") in TASK_CATEGORIES else "long_term"}
                for entry in runtime.get("_contextSourceMemories", [])
                if entry.get("content") and any(entry["content"] in section for section in sections)]
    notes, task_references = [], []
    # These items belong to the native packet; quoted user text cannot create
    # one. Only content retained in a native, final request section counts.
    native_sections = [identities for section, identities in getattr(view, "source_sections", {}).items()
                       if section and any(section in part for part in [*system, *other])]
    selected = set().union(*native_sections)
    seen = set()
    for item in getattr(view, "source_items", ()):
        identity = (item["id"], item["sourceEventId"], item["contentHash"])
        if identity in selected and identity not in seen:
            seen.add(identity)
            value = {key: item[key] for key in ("id", "type", "sourceEventId", "contentHash")}
            if item["group"] == "handoff_notes":
                notes.append({**value, "text": item["text"][:16000], "truncated": len(item["text"]) > 16000})
            else:
                task_references.append(value)
    return {"schema": SCHEMA, "requestId": request_id, "runId": str(runtime.get("runtimeRunId") or ""),
            "turnId": str(runtime.get("_webTurnId") or ""), "capturedAt": time.time(),
            "rulesKnown": "_contextSourceRules" in runtime, "memoryKnown": "_contextSourceMemories" in runtime,
            "rules": rules, "memories": memories, "notes": notes,
            "taskReferences": task_references, "taskMemoryKnown": bool(native_sections)}


def record_sources(session_id: str, value: dict[str, Any], sessions_dir: Path | None = None) -> None:
    root, path = _path(session_id, sessions_dir)
    payload = {**value, "sessionId": session_id}
    if not _valid_receipt(payload, session_id):
        raise ValueError("invalid context source receipt")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 262_144:
        raise ValueError("context source receipt exceeds limit")
    with store_lock(root / ".store.lock", timeout=0.2):
        previous = load_sources(session_id, root)
        if previous and previous["capturedAt"] > value["capturedAt"]:
            return
        atomic_write_json(path, payload)


def _read_memory(path: Path, scope: str):
    from minicode.memory import MemoryEntry, _validate_memory_data
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("memory index exceeds inspection limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    valid, _errors = _validate_memory_data(data)
    if not valid:
        raise ValueError("invalid memory index")
    parsed, ids = [], set()
    for raw in data["entries"]:
        # Missing legacy timestamps remain unknown and produce stable revisions.
        entry = MemoryEntry.from_dict({**raw, "scope": raw.get("scope", scope),
            "created_at": raw.get("created_at", 0), "updated_at": raw.get("updated_at", raw.get("created_at", 0))})
        if not entry.id or entry.id in ids or entry.scope.value != scope:
            raise ValueError("invalid or duplicate memory identity")
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in (entry.created_at, entry.updated_at, entry.last_accessed, entry.last_decay_at)):
            raise ValueError("invalid memory timestamp")
        if any(not isinstance(value, list) or not all(isinstance(item, str) for item in value)
               for value in (entry.domains, entry.related_to)):
            raise ValueError("invalid memory metadata")
        ids.add(entry.id)
        parsed.append(entry)
    return data, parsed


def memory_inventory(workspace: str | Path) -> dict[str, Any]:
    from minicode.memory import MemoryPaths
    paths = MemoryPaths.for_workspace(str(workspace))
    scopes, entries, history = [], [], []
    learning_entries = []
    history_policy = HistoryPolicy(workspace)
    for name in ("user", "project", "local"):
        root = getattr(paths, name + "_memory")
        path = root / "memory.json"
        scope = {"name": name, "entries": 0, "sizeBytes": 0, "categories": [],
                 "status": "ready", "path": str(path), "error": ""}
        try:
            if is_unsafe_stored_path(path, boundary=root.parent, reject_hardlinks=True):
                raise ValueError("unsafe memory path")
            if not path.exists():
                if (root / "MEMORY.md").exists():
                    raise ValueError("Markdown memory has no entry index")
                scopes.append(scope)
                continue
            _data, parsed = _read_memory(path, name)
            learning_entries.extend(parsed)
            for entry in parsed:
                value = {"id": entry.id, "scope": name, "category": entry.category, "content": entry.content,
                         "origin": entry.origin, "sources": entry.sources, "updatedAt": entry.updated_at,
                         "archived": entry.tier.value == "archival", "revision": digest(json.dumps(entry.to_dict(), sort_keys=True)),
                         "contentHash": digest(entry.content), "path": str(path)}
                target = history if entry.category in TASK_CATEGORIES else entries
                if target is history:
                    value.update(history_policy.describe(entry), createdAt=entry.created_at)
                target.append(value)
                if target is entries and not value["archived"]:
                    scope["entries"] += 1
                    scope["sizeBytes"] += len(entry.content.encode("utf-8"))
                    scope["categories"].append(entry.category)
            scope["categories"] = sorted(set(scope["categories"]))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            scope.update(status="unavailable", error=type(exc).__name__)
        scopes.append(scope)
    from minicode.memory_learning import LearningPolicy
    learning = LearningPolicy.from_snapshot(str(workspace), learning_entries,
        unavailable=any(scope["status"] != "ready" for scope in scopes))
    for value in entries:
        entry = learning.entries[value["id"]]
        if entry.origin == "agent_consolidated":
            value.update(learningStatus=learning.state(entry), reviewAfter=entry.review_after or None,
                         retentionReason=entry.retention_reason, sourceVersions=entry.source_versions)
    return {"schema": "minicode-memory-inventory-v1", "capturedAt": str(time.time()), "scopes": scopes,
            "entries": entries, "taskHistory": sorted(history, key=lambda item: item["createdAt"], reverse=True),
            "status": "ready" if all(s["status"] == "ready" for s in scopes) else "partial"}


def revise_memory(workspace: str | Path, scope: str, entry_id: str, revision: str,
                  *, content: str | None = None, archived: bool | None = None) -> None:
    """Compare and write under the same scope lock used by Agent persistence."""
    from minicode.memory import (
        MemoryFile,
        MemoryManager,
        MemoryPaths,
        MemoryScope,
        MemoryTier,
    )
    if scope not in {"user", "project", "local"}:
        raise ValueError("invalid memory scope")
    root = getattr(MemoryPaths.for_workspace(str(workspace)), scope + "_memory")
    path = root / "memory.json"
    if is_unsafe_stored_path(path, boundary=root.parent, reject_hardlinks=True):
        raise ValueError("unsafe memory path")
    # MemoryManager uses this same reentrant file lock for read/merge/write.
    for filename in ("MEMORY.md", ".memory.lock"):
        if is_unsafe_stored_path(root / filename, boundary=root.parent, reject_hardlinks=True):
            raise ValueError("unsafe memory path")
    from minicode.memory import _memory_write_lock
    with _memory_write_lock(root / ".memory.lock"):
        data, entries = _read_memory(path, scope)
        entry = next((item for item in entries if item.id == entry_id and item.scope.value == scope), None)
        if entry is None:
            raise KeyError(entry_id)
        if digest(json.dumps(entry.to_dict(), sort_keys=True)) != revision:
            raise ValueError("memory_changed")
        if content is not None:
            if not content.strip() or len(content.encode("utf-8")) > 16_384:
                raise ValueError("memory content must be nonempty and at most 16 KB")
            entry.content = content.strip()
            entry.origin = "user_corrected"
            entry.sources = list(dict.fromkeys([*entry.sources, "user:memory-editor"]))
            entry.source_versions, entry.review_after, entry.retention_reason = {}, 0.0, ""
            entry.confirmed_version = ""
        if archived is not None:
            entry.tier = MemoryTier.ARCHIVAL if archived else MemoryTier.SHORT_TERM
            if not archived:
                entry.retention_reason = ""
                if entry.origin == "agent_consolidated" and entry.source_versions:
                    from minicode.memory_learning import REVIEW_SECONDS
                    entry.review_after = time.time() + REVIEW_SECONDS
        # Validate the complete latest scope under its write lock. Do not call
        # _enforce_limits here: an edit must never evict existing knowledge.
        memory = MemoryFile(scope=MemoryScope(scope), entries=entries)
        if (len(entries) > memory.max_entries
                or sum(len(item.content.encode("utf-8")) for item in entries) > memory.max_size_bytes):
            raise ValueError("memory_capacity_exceeded")
        entry.updated_at = time.time()
        data["entries"] = [{**raw, **item.to_dict()} for raw, item in zip(data["entries"], entries, strict=True)]
        data["last_updated"] = time.time()
        MemoryManager._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        MemoryManager._atomic_write(root / "MEMORY.md", memory.format_as_markdown())
