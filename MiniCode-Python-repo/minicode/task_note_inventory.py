"""Read saved handoffs without running memory initialization or retrieval.

The task store owns current notes. Native call/result pairs in this session's
retained history also keep older and fork-inherited notes inspectable. Neither
source proves that a later model request included the note.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from minicode.config import load_effective_settings
from minicode.paged_memory.config import PagedMemoryConfig
from minicode.paged_memory.handoff import NOTE_FIELDS, validate_note_update
from minicode.paged_memory.models import content_hash_text
from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id

LIMIT = 100
MAX_NOTE_BYTES = 131_072


def _note_fields(value: dict, revision: int) -> dict:
    checked = validate_note_update({**value, "expected_revision": revision})
    return {key: checked[key] for key in NOTE_FIELDS}


def _history_notes(messages: list[dict]) -> tuple[dict[str, dict], bool]:
    """Only accept successful, paired native note writes, never prose claims."""
    pending, notes = {}, {}
    invalid = False
    for message in messages:
        if not isinstance(message, dict) or message.get("toolName") != "memory_note_update":
            continue
        call_id = message.get("toolUseId")
        if not isinstance(call_id, str) or not call_id:
            continue
        if message.get("role") == "assistant_tool_call":
            pending[call_id] = message.get("input")
            continue
        if message.get("role") != "tool_result" or call_id not in pending:
            continue
        value = pending.pop(call_id)
        if message.get("isError") is not False:
            continue
        try:
            content = message.get("content")
            if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_NOTE_BYTES:
                continue
            receipt = json.loads(content)
            if not isinstance(receipt, dict) or receipt.get("status") != "saved":
                continue
            event_id, revision, content_hash = (receipt.get(key) for key in ("source_event_id", "revision", "content_hash"))
            if (not isinstance(event_id, str) or not event_id.startswith("event_") or len(event_id) > 200
                    or type(revision) is not int or revision < 1
                    or not isinstance(content_hash, str) or len(content_hash) != 71 or not content_hash.startswith("sha256:")):
                raise ValueError("invalid saved note receipt")
            if not isinstance(value, dict) or value.get("expected_revision") != revision - 1:
                raise ValueError("note call and save receipt disagree")
            fields = _note_fields(value, revision - 1)
            notes[event_id] = {"sourceEventId": event_id, "contentHash": content_hash, "revision": revision,
                "owner": "primary", "savedAt": None, "reason": value.get("reason", "manual"),
                "origin": "session_history", "fields": fields}
            if len(notes) > LIMIT:
                del notes[next(iter(notes))]
        except (ValueError, TypeError, KeyError):
            invalid = True
    return notes, invalid


def _stored_notes(config: PagedMemoryConfig, session_id: str) -> tuple[list[dict], bool, bool]:
    path = Path(config.sqlite_path).absolute()
    if is_unsafe_stored_path(path, boundary=Path(path.anchor), reject_hardlinks=True):
        raise ValueError("unsafe note storage path")
    if not path.exists():
        return [], False, False
    task_id = session_id if session_id.startswith("task_") else "task_" + session_id
    notes, invalid = [], False
    # SqliteMemoryStore's constructor migrates schemas. Inspection must not.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 1.0
        database.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        rows = database.execute(
            "SELECT id, content_text, content_hash, created_at FROM agent_event "
            "WHERE tenant_id=? AND task_id=? AND event_type='HANDOFF_NOTE_UPDATED' "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (config.tenant_id, task_id, LIMIT + 1),
        ).fetchall()
        for row in rows[:LIMIT]:
            try:
                text = row["content_text"]
                if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_NOTE_BYTES:
                    raise ValueError("note body unavailable")
                if content_hash_text(text) != row["content_hash"]:
                    raise ValueError("note body hash mismatch")
                value = json.loads(text)
                owner, revision = value["owner"], value["revision"]
                if (value.get("schema_version") != 1 or not isinstance(owner, str) or not owner
                        or type(revision) is not int or revision < 1):
                    raise ValueError("invalid saved handoff")
                saved_at = row["created_at"]
                if type(saved_at) not in (int, float) or not math.isfinite(saved_at):
                    raise ValueError("invalid note timestamp")
                fields = _note_fields(value["note"], revision)
                notes.append({"sourceEventId": row["id"], "contentHash": row["content_hash"],
                    "owner": owner, "revision": revision, "savedAt": saved_at,
                    "reason": value.get("reason", "manual"), "origin": "task_store", "fields": fields})
            except (ValueError, TypeError, KeyError):
                invalid = True
    return notes, invalid, len(rows) > LIMIT


def task_note_inventory(session: Any) -> dict[str, Any]:
    history, invalid = _history_notes(session.messages)
    notes, truncated, error = [], False, ""
    try:
        if not _valid_session_id(session.session_id):
            raise ValueError("invalid note session")
        settings = load_effective_settings(session.workspace)
        config = PagedMemoryConfig.from_sources(settings, cwd=session.workspace,
            environ={**settings.get("env", {}), **os.environ})
        stored, invalid_stored, truncated = _stored_notes(config, session.session_id)
        invalid = invalid or invalid_stored
        for note in stored:
            history.pop(note["sourceEventId"], None)
        notes = stored
    except (OSError, ValueError, TypeError, sqlite3.Error):
        error = "交接笔记存储暂时无法读取；已保留的会话历史仍可查看。"
    # History is already restricted to this branch's retained prefix. Never
    # query another task because its ID happens to appear in message text.
    notes.extend(reversed(list(history.values())))
    seen = set()
    for note in notes:
        group = (note["origin"], note["owner"])
        note["latest"] = group not in seen
        seen.add(group)
    return {"status": "partial" if (invalid or error) and notes else "unavailable" if invalid or error else "ready",
        "notes": notes, "truncated": truncated, "error": error or ("部分交接记录不完整，暂未展示。" if invalid else "")}
