"""Immutable completed-turn snapshots, independent of compactable messages."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id, atomic_write_json
from minicode.store_lock import serialized_sessions
from minicode.history_objects import decode, encode
from minicode.history_storage import active_history, history_operation
from minicode.session_display import project_messages
from minicode.turn_changes import change_summary, project_file_changes


class HistoryError(ValueError):
    pass


def project_binding(workspace: str, inherited: dict | None = None) -> dict:
    binding = dict(inherited or {})
    project = str(Path(binding.get("project_path") or workspace).resolve())
    binding.setdefault("project_path", project)
    binding.setdefault("project_id", hashlib.sha256(os.path.normcase(project).encode()).hexdigest()[:24])
    return binding


def _root(session_id: str) -> Path:
    from minicode import session
    if not _valid_session_id(session_id):
        raise HistoryError("无效的会话标识。")
    path = session.SESSIONS_DIR / session_id / "history"
    if is_unsafe_stored_path(path, boundary=session.SESSIONS_DIR):
        raise HistoryError("会话历史路径不安全。")
    return path


def _read(path: Path, default: Any = None) -> Any:
    if is_unsafe_stored_path(path, boundary=path.parent, reject_hardlinks=True):
        raise HistoryError("会话历史文件不安全。")
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HistoryError("会话历史损坏，未修改原始记录。") from exc


def _index(session_id: str) -> list[dict]:
    authority = active_history()
    rows = authority.index(session_id) if authority else _read(_root(session_id) / "index.json", [])
    if not isinstance(rows, list):
        raise HistoryError("会话轮次索引损坏。")
    return rows


def _save_index(session_id: str, rows: list[dict]) -> None:
    authority = active_history()
    if authority:
        authority.save_index(session_id, rows)
    else:
        atomic_write_json(_root(session_id) / "index.json", rows)


def _write_snapshot(session_id: str, value: dict, *, encoded: bool = False) -> None:
    manifest = value if encoded else encode(value)
    existing = _manifest(session_id, manifest["turnId"])
    if existing is not None:
        if existing != manifest:
            raise HistoryError("已发布的轮次快照不可修改。")
        return
    authority = active_history()
    if authority:
        authority.save_manifest(session_id, manifest)
    else:
        atomic_write_json(_root(session_id) / f"{manifest['turnId']}.json", manifest)


def _manifest(session_id: str, turn_id: str):
    authority = active_history()
    return authority.manifest(session_id, turn_id) if authority else _read(_root(session_id) / f"{turn_id}.json")


def _snapshot(session_id: str, turn_id: str, *, display_only: bool = False, manifest_only: bool = False) -> dict:
    if not _valid_session_id(turn_id):
        raise HistoryError("无效的轮次标识。")
    value = _manifest(session_id, turn_id)
    if not isinstance(value, dict) or value.get("turnId") != turn_id:
        raise HistoryError("此轮次没有完整快照，无法分叉。")
    try:
        return value if manifest_only else decode(value, display_only=display_only)
    except (OSError, ValueError, KeyError) as exc:
        raise HistoryError("历史快照缺失或校验失败，无法继续读取。") from exc


def _complete_messages(messages: list[dict]) -> bool:
    pending: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant_tool_call":
            identity = message.get("toolUseId")
            if not identity or identity in pending:
                return False
            pending.add(identity)
        elif role == "tool_result":
            identity = message.get("toolUseId")
            if identity not in pending:
                return False
            pending.remove(identity)
        elif role == "tool" or message.get("tool_calls"):
            return False  # Unsupported legacy wire shape cannot certify a boundary.
    return not pending and bool(messages) and messages[-1].get("role") == "assistant"


@serialized_sessions
@history_operation
def ensure_legacy_history(session) -> None:
    root = _root(session.session_id)
    if _index(session.session_id) or (not active_history() and (root / "index.json").exists()) or not session.messages:
        return
    original = [m for m in session.messages if isinstance(m, dict)]
    messages = project_messages(original, session.history, runtime=bool(session.runtime_runs))
    prompts = [m.get("content") for m in messages if m.get("role") == "user"]
    # Input history must prove that compaction has not removed or rewritten turns.
    certified = (messages == [m for m in original if m.get("role") not in {"system", "developer"}]
                 and session.metadata.status == "completed" and bool(prompts)
                 and prompts == session.history and _complete_messages(messages)
                 and not any(m.get("role") == "tool_result" and "[Tool result envelope]" in str(m.get("content", "")) for m in messages))
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    original_starts = [i for i, m in enumerate(original) if m.get("role") == "user"]
    rows = []
    for number, start in enumerate(starts):
        end = starts[number + 1] if number + 1 < len(starts) else len(messages)
        segment = messages[start:end]
        turn_id = "legacy-" + hashlib.sha256(f"{session.session_id}:{number}".encode()).hexdigest()[:24]
        ready = certified and _complete_messages(segment)
        row = {"turnId": turn_id, "prompt": str(segment[0].get("content", "")),
               "status": "completed" if ready else "unavailable", "createdAt": session.created_at,
               "completedAt": session.updated_at, "canFork": ready,
               "reason": "" if ready else "旧记录未保存完整轮次快照，无法确认历史边界。"}
        _write_snapshot(session.session_id, {
            **row, "messages": deepcopy(original[:original_starts[number + 1]
                if number + 1 < len(original_starts) else len(original)]) if ready else [],
            "displayMessages": deepcopy(segment), "history": prompts[:number + 1],
            "contextState": {},
        })
        rows.append(row)
    _save_index(session.session_id, rows)


@serialized_sessions
@history_operation(read_only=True)
def display_messages(session, *, message_count: int | None = None) -> list[dict]:
    """Read a display projection without migrating or rewriting old snapshots.

    A run's event watermark counts raw model messages. For a partial watermark
    use exactly that prefix, then project it; never include a later snapshot.
    """
    messages = session.messages if message_count is None else session.messages[:message_count]
    fallback = project_messages(messages, session.history, runtime=bool(session.runtime_runs))
    if message_count is not None:
        return fallback
    rows = _index(session.session_id)
    if not rows or [row.get("prompt") for row in rows] != session.history or any(
        row.get("status") == "running" for row in rows
    ):
        return fallback
    result = []
    try:
        for row in rows[-20:]:
            snapshot = _snapshot(session.session_id, row["turnId"], display_only=True)
            result.extend(project_messages(snapshot["displayMessages"], [row["prompt"]], runtime=True))
    except (HistoryError, KeyError, TypeError, ValueError, OSError):
        # The turns API still reports unavailable snapshots. This legacy API
        # cannot grant a fork boundary or alter the original model history.
        return fallback
    return result


@serialized_sessions
@history_operation
def begin_turn(session, prompt: str) -> str:
    ensure_legacy_history(session)
    rows = _index(session.session_id)
    turn_id = uuid.uuid4().hex
    row = {"turnId": turn_id, "prompt": prompt, "status": "running",
           "createdAt": time.time(), "canFork": False, "reason": "轮次尚未完成。"}
    from minicode.session import save_session
    session.metadata.status = "running"
    session.metadata.review_pending = False
    save_session(session, force_full=True)
    rows.append(row)
    _save_index(session.session_id, rows)
    return turn_id


@serialized_sessions
@history_operation
def finish_turn(session, turn_id: str, *, status: str, display_messages: list[dict],
                context_state: dict | None = None, raw_tool_results: list[dict] | None = None,
                file_changes: dict | None = None) -> None:
    rows = _index(session.session_id)
    row = next((r for r in rows if r["turnId"] == turn_id), None)
    if row is None:
        raise HistoryError("轮次不存在。")
    if row["status"] != "running":
        return
    previous = _manifest(session.session_id, turn_id)
    if previous:
        # Recover a file-backend interruption between snapshot and index publication.
        row.update({key: previous[key] for key in row.keys() | {"completedAt", "fileChanges"} if key in previous})
        _save_index(session.session_id, rows)
        return
    ready = status == "completed" and _complete_messages(session.messages)
    row.update(status=status, completedAt=time.time(), canFork=ready,
               reason="" if ready else "此轮次未成功完成或消息边界不完整。")
    if file_changes is not None:
        row["fileChanges"] = change_summary(file_changes)
    state = deepcopy(context_state if context_state is not None else session.context_state)
    transcript = deepcopy(display_messages)
    for message in transcript:
        if message.get("role") == "run_event" and isinstance(message.get("event"), dict):
            # A turn outcome is not the result of each earlier operation.
            message["event"]["turnStatus"] = status
    snapshot = {**row, "messages": deepcopy(session.messages),
                "displayMessages": transcript,
                "rawToolResults": deepcopy(raw_tool_results or []),
                "history": list(session.history), "contextState": state,
                **({"fileChanges": file_changes} if file_changes is not None else {})}
    # Publish only after the durable conversation and immutable snapshot exist.
    from minicode.session import save_session
    session.context_state = state
    save_session(session, force_full=True)
    _write_snapshot(session.session_id, snapshot)
    _save_index(session.session_id, rows)


@serialized_sessions
def list_turns(session, *, before: int | None = None, limit: int = 20) -> dict:
    _reconcile_turns(session)
    return _read_turns(session, before=before, limit=limit)


@history_operation
def _reconcile_turns(session) -> None:
    """Publish legacy/interrupted-turn recovery before decoding large objects."""
    ensure_legacy_history(session)
    rows = _index(session.session_id)
    recovered = False
    for row in rows:
        if row["status"] != "running":
            continue
        previous = _manifest(session.session_id, row["turnId"])
        if previous:
            row.update({key: previous[key] for key in row.keys() | {"completedAt", "fileChanges"} if key in previous})
            recovered = True
        elif session.metadata.status in {"completed", "stopped", "failed"}:
            row.update(status="unavailable", canFork=False, reason="执行中断，未发布完整快照。")
            recovered = True
    if recovered:
        _save_index(session.session_id, rows)


@history_operation(read_only=True)
def _read_turns(session, *, before: int | None, limit: int) -> dict:
    rows = _index(session.session_id)
    end = min(len(rows), before if before is not None else len(rows))
    start = max(0, end - max(1, min(limit, 100)))
    values = []
    for entry in rows[start:end]:
        row = dict(entry)
        if row.get("fileChanges") is not None:
            row["fileChanges"] = change_summary(row["fileChanges"])
        try:
            row["displayMessages"] = _snapshot(session.session_id, row["turnId"], display_only=True)["displayMessages"]
        except (HistoryError, ValueError, OSError):
            row["canFork"] = False
            row["displayMessages"] = [{"role": "user", "content": row["prompt"]}]
            if row["status"] != "running":
                row["reason"] = "完整快照缺失或校验失败，无法分叉。"
        values.append(row)
    return {"turns": values, "nextBefore": start if start else None, "total": len(rows)}


@serialized_sessions
@history_operation(read_only=True)
def turn_file_changes(session, turn_id: str) -> dict | None:
    """Read the immutable diff object without loading model/tool history."""
    from minicode.history_objects import read
    snapshot = _snapshot(session.session_id, turn_id, manifest_only=True)
    value = read(snapshot["changesRef"])["data"] if snapshot.get("changesRef") else snapshot.get("fileChanges")
    return project_file_changes(value)


@serialized_sessions
def fork_session(source, turn_id: str, *, workspace: str | None = None,
                 binding: dict | None = None, child_id: str | None = None):
    from minicode.session import save_session
    child = _copy_fork_history(source, turn_id, workspace=workspace, binding=binding, child_id=child_id)
    # Only publish the child after its SQLite transaction has committed.
    save_session(child, force_full=True)
    return child


@serialized_sessions
@history_operation
def validate_fork(source, turn_id: str) -> None:
    ensure_legacy_history(source)
    row = next((row for row in _index(source.session_id) if row["turnId"] == turn_id), None)
    if not row or not row.get("canFork"):
        raise HistoryError("只能从具有完整快照的已完成轮次分叉。")
    _snapshot(source.session_id, turn_id)


@history_operation
def _copy_fork_history(source, turn_id: str, *, workspace: str | None,
                       binding: dict | None, child_id: str | None):
    from minicode.session import create_new_session, save_session
    ensure_legacy_history(source)
    rows = _index(source.session_id)
    offset = next((i for i, row in enumerate(rows) if row["turnId"] == turn_id), -1)
    if offset < 0 or not rows[offset].get("canFork"):
        raise HistoryError("只能从具有完整快照的已完成轮次分叉。")
    snapshot = _snapshot(source.session_id, turn_id)
    child = create_new_session(workspace or source.workspace)
    if child_id:
        child.session_id = child_id
    child.binding = project_binding(source.workspace, binding or source.binding)
    child.binding.update(forked_from_id=source.session_id, forked_at_turn_id=turn_id,
                         forked_at=time.time())
    child.messages = deepcopy(snapshot["messages"])
    child.history = list(snapshot["history"])
    child.context_state = deepcopy(snapshot.get("contextState") or {})
    child.metadata.status = "active"
    # Copy all prefix snapshots: the child remains usable after its parent is deleted.
    for row in rows[:offset + 1]:
        if _manifest(source.session_id, row["turnId"]):
            value = _snapshot(source.session_id, row["turnId"], manifest_only=True)
            from minicode.run_images import fork_images, image_messages
            pictures = fork_images(source.session_id, child.session_id, row["turnId"])
            if pictures:
                value = decode(value)
                value["displayMessages"] = [message for message in value.get("displayMessages", [])
                                            if message.get("role") != "tool_image"] + image_messages(pictures)
                _write_snapshot(child.session_id, value)
            else:
                _write_snapshot(child.session_id, value, encoded=True)
    _save_index(child.session_id, rows[:offset + 1])
    return child


@serialized_sessions
@history_operation
def delete_history(session_id: str, *, collect: bool = True) -> None:
    authority = active_history()
    if authority:
        authority.delete_session(session_id)
    if collect:
        from minicode.history_objects import collect_unreferenced
        collect_unreferenced()
