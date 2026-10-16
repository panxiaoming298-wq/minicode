"""Lightweight browser history over immutable turn manifests.

Reading a view never migrates or rewrites a saved model conversation. Older
manifests are projected in memory; new snapshots carry their delivery summary.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from copy import deepcopy

from minicode import session as sessions
from minicode.history_objects import read, sequence_refs
from minicode.history_storage import active_history, history_operation
from minicode.session_display import DISPLAY_SUMMARY_VERSION, is_question_message, summarize_messages
from minicode.session_history import HistoryError, _index, _manifest
from minicode.store_lock import serialized_sessions
from minicode.turn_changes import change_summary

PAGE_BYTES = 256 * 1024
MESSAGE_PREVIEW_CHARS = 16_384
_summaries: OrderedDict[tuple, dict] = OrderedDict()
_summary_bytes = 0


def _summary(manifest: dict) -> dict:
    saved = manifest.get("displaySummary")
    if isinstance(saved, dict) and saved.get("version") == DISPLAY_SUMMARY_VERSION:
        return deepcopy(saved)
    if manifest.get("formatVersion") != 2:
        return summarize_messages(manifest.get("displayMessages") or [], manifest.get("prompt", ""))
    authority = active_history()
    scope = (str(sessions.SESSIONS_DIR), getattr(authority, "store_id", "file"))
    key = (*scope, manifest.get("displayRef"), manifest.get("prompt"), DISPLAY_SUMMARY_VERSION)
    if key in _summaries:
        _summaries.move_to_end(key)
        return deepcopy(_summaries[key])
    refs = sequence_refs(manifest.get("displayRef"))
    answer, answer_index, fallback, tool_seen = None, None, None, False
    for index in range(len(refs) - 1, -1, -1):
        message = read(refs[index])["data"]
        if message.get("nodeId") not in {None, "", "primary", "root"}:
            continue
        role = message.get("role")
        if role == "assistant" and message.get("phase") == "final_answer":
            answer, answer_index = message, index
            break
        if role == "assistant" and message.get("phase") != "commentary" and not tool_seen and fallback is None:
            fallback = (message, index)
        if role == "assistant_tool_call":
            tool_seen = True
        if role == "user":
            break
        if fallback is not None and role == "assistant_tool_call":
            # A legacy, unphased delivery after the last tool is sufficient.
            break
    if answer is None and fallback:
        answer, answer_index = fallback
    visible = [{"role": "user", "content": manifest.get("prompt", "")}]
    if answer is not None:
        visible.append(answer)
    count = max(0, len(refs) - len(visible))
    result = {"displayMessages": visible, "answerIndex": answer_index,
              "processMessageCount": count, "processAvailable": count > 0}
    if isinstance(saved, dict):
        result = deepcopy(saved)
    # Upgrade the read-only projection of older immutable snapshots once, then
    # cache it. Stream object reads so large tool outputs are not held in memory.
    inline = []
    visible = [message for message in result["displayMessages"]
               if message.get("role") != "user" and not is_question_message(message)]
    process_count, tool_count = 0, 0
    for index, ref in enumerate(refs):
        message = read(ref)["data"]
        role = message.get("role")
        if role == "user" or is_question_message(message):
            inline.append({**message, "displayIndex": index})
        if role not in {"user", "tool_image"} and index != result.get("answerIndex") and not is_question_message(message):
            process_count += 1
        if role == "assistant_tool_call":
            tool_count += 1
        for displayed in visible:
            if "displayIndex" not in displayed and (displayed == message
                    or displayed.get("role") == "assistant" and index == result.get("answerIndex")):
                displayed["displayIndex"] = index
    if not any(message.get("role") == "user" for message in inline):
        inline.insert(0, {"role": "user", "content": manifest.get("prompt", ""), "displayIndex": 0})
    result["displayMessages"] = sorted([*visible, *inline], key=lambda message: message.get("displayIndex", 0))
    result["version"] = DISPLAY_SUMMARY_VERSION
    result["processMessageCount"] = process_count
    result["toolCallCount"] = tool_count
    result["processAvailable"] = process_count > 0
    global _summary_bytes
    size = len(json.dumps(result, ensure_ascii=False).encode())
    if size <= 2 * 1024 * 1024:
        _summaries[key] = result
        _summary_bytes += size
        while len(_summaries) > 128 or _summary_bytes > 8 * 1024 * 1024:
            _, removed = _summaries.popitem(last=False)
            _summary_bytes -= len(json.dumps(removed, ensure_ascii=False).encode())
    return deepcopy(result)


def _required_manifest(session_id: str, turn_id: str) -> dict:
    if turn_id.startswith("legacy-view-") and turn_id[12:].isdigit():
        current = sessions.load_session(session_id)
        if current is None:
            raise HistoryError("会话不存在。")
        from minicode.session_display import project_messages
        messages = project_messages(current.messages, current.history, runtime=bool(current.runtime_runs))
        starts = [index for index, message in enumerate(messages) if message.get("role") == "user"]
        number = int(turn_id[12:])
        if number >= len(starts):
            raise HistoryError("消息不存在。")
        segment = messages[starts[number]:starts[number + 1] if number + 1 < len(starts) else len(messages)]
        return {"turnId": turn_id, "displayMessages": segment, "prompt": segment[0].get("content", "")}
    if not sessions._valid_session_id(turn_id):
        raise HistoryError("无效的轮次标识。")
    manifest = _manifest(session_id, turn_id)
    if not isinstance(manifest, dict) or manifest.get("turnId") != turn_id:
        raise HistoryError("此轮次没有可读取的完整快照。")
    return manifest


@serialized_sessions
@history_operation(read_only=True)
def conversation_page(session_id: str, *, before: int | None = None, limit: int = 5) -> dict:
    metadata = sessions.load_session_metadata(session_id)
    if metadata is None:
        raise HistoryError("会话不存在。")
    rows = _index(session_id)
    if not rows and metadata.message_count:
        # Compatibility for pre-snapshot sessions only. Preserve their records.
        current = sessions.load_session(session_id)
        from minicode.session_display import project_messages
        messages = project_messages(current.messages, current.history, runtime=bool(current.runtime_runs))
        starts = [i for i, message in enumerate(messages) if message.get("role") == "user"]
        rows = [{"turnId": f"legacy-view-{i}", "prompt": messages[start].get("content", ""),
                 "status": "unavailable", "canFork": False, "reason": "旧记录没有完整轮次快照，无法分叉。",
                 "displayMessages": messages[start:starts[i + 1] if i + 1 < len(starts) else len(messages)]}
                for i, start in enumerate(starts)]
    # Include the projected counts in the revision, so an open browser also
    # invalidates cached legacy cards after the application-state filter changes.
    rows = [{**row, "fileChanges": change_summary(row["fileChanges"])}
            if row.get("fileChanges") is not None else row for row in rows]
    revision = hashlib.sha256(json.dumps([DISPLAY_SUMMARY_VERSION, str(sessions.SESSIONS_DIR), metadata.updated_at,
                                          metadata.message_count, rows], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    end = min(len(rows), before if before is not None else len(rows))
    start = max(0, end - max(1, min(limit, 20)))
    values, size = [], 0
    for index in range(end - 1, start - 1, -1):
        row = dict(rows[index])
        try:
            if "displayMessages" in row:
                summary = summarize_messages(row["displayMessages"], row["prompt"])
                row["displayRevision"] = hashlib.sha256(json.dumps(row["displayMessages"], ensure_ascii=False).encode()).hexdigest()
                row.update(summary)
            else:
                manifest = _required_manifest(session_id, row["turnId"])
                if row["status"] == "running" and manifest.get("status") != "running":
                    row.update({key: manifest[key] for key in ("status", "completedAt", "canFork", "reason", "fileChanges") if key in manifest})
                row["displayRevision"] = manifest.get("displayRef") or hashlib.sha256(json.dumps(manifest.get("displayMessages", []), ensure_ascii=False).encode()).hexdigest()
                row.update(_summary(manifest))
        except (HistoryError, KeyError, TypeError, ValueError, OSError):
            row.update(displayMessages=[{"role": "user", "content": row["prompt"]}], processAvailable=False,
                       canFork=False, reason="完整快照缺失或校验失败，无法读取工作过程或分叉。")
            if row["status"] == "running" and metadata.status in {"completed", "stopped", "failed"}:
                row["status"] = "unavailable"
        if row.get("fileChanges") is not None:
            row["fileChanges"] = change_summary(row["fileChanges"])
        # Tool images commit independently of the worker's terminal snapshot.
        # A killed worker or a collapsed/paged transcript must not hide them.
        from minicode.run_images import RunImageStore, image_messages
        pictures = RunImageStore(session_id).records(turn_id=row["turnId"])
        if pictures:
            row["displayMessages"] = [message for message in row["displayMessages"] if message.get("role") != "tool_image"]
            row["displayMessages"].extend(image_messages(pictures))
            row["displayRevision"] = str(row.get("displayRevision", "")) + ":" + str(pictures[-1]["createdAt"]) + ":" + str(len(pictures))
        row["viewOnly"] = True
        row_size = len(json.dumps(row, ensure_ascii=False).encode())
        if values and size + row_size > PAGE_BYTES:
            start = index + 1
            break
        values.insert(0, row)
        size += row_size
    return {"turns": values, "nextBefore": start if start else None, "total": len(rows), "revision": revision}


@serialized_sessions
@history_operation(read_only=True)
def turn_message_page(session_id: str, turn_id: str, *, after: int = 0, limit: int = 60) -> dict:
    manifest = _required_manifest(session_id, turn_id)
    refs = sequence_refs(manifest.get("displayRef")) if manifest.get("formatVersion") == 2 else None
    messages = manifest.get("displayMessages") or []
    total = len(refs) if refs is not None else len(messages)
    values, size = [], 0
    index = min(after, total)
    while index < min(total, after + limit):
        message = deepcopy(read(refs[index])["data"] if refs is not None else messages[index])
        message["displayIndex"] = index
        clipped = []
        for field in ("content", "output", "input", "toolRecovery"):
            value = message.get(field)
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if len(text) > MESSAGE_PREVIEW_CHARS:
                if field == "toolRecovery":
                    message.pop(field, None)
                else:
                    message[field] = text[:MESSAGE_PREVIEW_CHARS] + "\n…"
                clipped.append(field)
        command = message.get("commandResult")
        if isinstance(command, dict):
            for stream in ("stdout", "stderr"):
                value = command.get(stream)
                if isinstance(value, str) and len(value) > MESSAGE_PREVIEW_CHARS:
                    command[stream] = value[:MESSAGE_PREVIEW_CHARS] + "\n…"
                    if "commandResult" not in clipped:
                        clipped.append("commandResult")
        if clipped:
            message["truncatedFields"] = clipped
        message_size = len(json.dumps(message, ensure_ascii=False).encode())
        if values and size + message_size > PAGE_BYTES:
            break
        values.append(message)
        size += message_size
        index += 1
    return {"messages": values, "nextAfter": index if index < total else None, "total": total}


@serialized_sessions
@history_operation(read_only=True)
def turn_message(session_id: str, turn_id: str, index: int) -> dict:
    manifest = _required_manifest(session_id, turn_id)
    refs = sequence_refs(manifest.get("displayRef")) if manifest.get("formatVersion") == 2 else None
    messages = manifest.get("displayMessages") or []
    total = len(refs) if refs is not None else len(messages)
    if index < 0 or index >= total:
        raise HistoryError("消息不存在。")
    return read(refs[index])["data"] if refs is not None else messages[index]
