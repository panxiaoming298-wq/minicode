"""Ownership and retrieval policy for shared historical task summaries.

Legacy text is never used to guess a source conversation. Inspection and policy
checks are read-only; only an explicit session deletion removes owned summaries.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id

TASK_CATEGORIES = {"reflection", "task_context", "task_history", "runtime_delivery", "runtime_outcome"}
_INTERNAL_TASK = re.compile(
    r"^\s*(?:Solve this strongly coupled task as|You are an independent(?: and)? read-only Verifier|"
    r"You are (?:a|an) .*?(?:Worker|Verifier)\b)", re.IGNORECASE,
)
_SMALL_TALK = re.compile(
    r"(?:你好|您好|嗨|早上好|晚上好|谢谢|介绍一下自己|自我介绍|你是谁|"
    r"你(?:现在)?(?:可以|能|会)(?:做|干)什么|你有什么(?:功能|能力)|"
    r"你现在是什么(?:模型|思考强度)|hello(?: from browser)?|hi|hey|"
    r"who are you|what can you do|introduce yourself|thanks)", re.IGNORECASE,
)


def trivial_task(text: str) -> bool:
    text = re.sub(r"[\s,，。.!！?？:：;；、]+", " ", text).strip()
    # Do not suppress a real task just because it starts with a greeting.
    remainder = _SMALL_TALK.sub("", text)
    return not re.sub(r"[\s,，。.!！?？:：;；、]+", "", remainder)


def internal_task(text: str) -> bool:
    return bool(_INTERNAL_TASK.search(text))


def source_sessions(entry: Any) -> list[str]:
    return list(dict.fromkeys(
        source[8:] for source in entry.sources
        if source.startswith("session:") and _valid_session_id(source[8:])
    ))


def history_title(entry: Any) -> str:
    text = entry.content.strip()
    if entry.category in {"runtime_delivery", "runtime_outcome"}:
        try:
            data = json.loads(text[text.index("{"):])
            text = str(data.get("task") or text)
        except (ValueError, TypeError):
            pass
    text = re.sub(r"^(?:Task Context|任务摘要|任务背景)\s*[:：]\s*", "", text)
    if internal_task(text):
        return "旧版内部执行记录"
    return (text.splitlines()[0] if text else "未命名任务摘要")[:160]


def history_matches(entry: Any, query: str) -> bool:
    """Require topic evidence, not a match on generic template words."""
    if not query.strip() or trivial_task(query) or internal_task(query):
        return False
    content = entry.content.casefold()
    words = set(re.findall(r"[a-z0-9_][a-z0-9_./\\-]{2,}", query.casefold()))
    words -= {"the", "and", "for", "with", "this", "that", "task", "please", "what", "how", "can", "you"}
    if any(re.search(r"(?<![a-z0-9_])" + re.escape(word) + r"(?![a-z0-9_])", content) for word in words):
        return True
    pairs = {chunk[i:i + 2] for chunk in re.findall(r"[\u3400-\u9fff]+", query) for i in range(len(chunk) - 1)}
    pairs -= {"请问", "帮我", "一下", "如何", "怎么", "什么", "可以", "之前", "上次", "任务", "记录", "一个", "这个", "我们", "你能", "是否", "完成", "成功", "验证", "结果"}
    return sum(pair in content for pair in pairs) >= min(2, max(1, len(pairs)))


class HistoryPolicy:
    """Fresh source-state snapshot for one inventory or retrieval operation."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self._sessions: dict[str, dict[str, str]] = {}

    def session(self, session_id: str) -> dict[str, str]:
        if session_id not in self._sessions:
            self._sessions[session_id] = self._read_session(session_id)
        return self._sessions[session_id]

    def _read_session(self, session_id: str) -> dict[str, str]:
        from minicode.session import SESSIONS_DIR
        from minicode.session_trash import SessionTrash, SessionTrashError
        result = {"sessionId": session_id, "status": "missing"}
        try:
            marker = SessionTrash(SESSIONS_DIR).get(session_id)
            if marker is not None:
                return {**result, "status": "archived"}
            # Avoid loading conversations or repairing the session index merely
            # to inspect ownership. Both current and legacy carriers are read.
            for path in (SESSIONS_DIR / session_id / "metadata.json", SESSIONS_DIR / f"{session_id}.json"):
                if is_unsafe_stored_path(path, boundary=SESSIONS_DIR, reject_hardlinks=True):
                    return {**result, "status": "unavailable"}
                if not path.is_file():
                    continue
                if path.stat().st_size > 4 * 1024 * 1024:
                    return {**result, "status": "unavailable"}
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("session_id") != session_id:
                    return {**result, "status": "unavailable"}
                workspace = data.get("cwd") or data.get("workspace")
                if not workspace or Path(workspace).resolve() != self.workspace:
                    return {**result, "status": "unavailable"}
                return {**result, "status": "active"}
        except (OSError, ValueError, TypeError, AttributeError, SessionTrashError):
            return {**result, "status": "unavailable"}
        return result

    def describe(self, entry: Any) -> dict[str, Any]:
        owners = [self.session(sid) for sid in source_sessions(entry)]
        if entry.tier.value == "archival":
            reason = "archived"
        elif not owners:
            reason = "legacy"
        elif any(owner["status"] == "archived" for owner in owners):
            reason = "source_archived"
        elif any(owner["status"] == "missing" for owner in owners):
            reason = "source_missing"
        elif any(owner["status"] != "active" for owner in owners):
            reason = "source_unavailable"
        else:
            reason = "eligible"
        return {"title": history_title(entry), "sourceSessions": owners,
                "sourceTurns": [s[5:] for s in entry.sources if s.startswith("turn:")],
                "retrievalStatus": reason, "autoRetrieval": reason == "eligible"}

    def allows(self, entry: Any, query: str) -> bool:
        if entry.category not in TASK_CATEGORIES:
            return True
        return self.describe(entry)["autoRetrieval"] and history_matches(entry, query)


def purge_session_history(workspace: str, session_id: str) -> int:
    """Remove exclusively owned automatic summaries, preserving other memory.

    Called before deleting the session carrier so a failed cleanup can be retried.
    Unknown, explicitly kept, and multi-source records are never guessed away.
    """
    from minicode.context_sources import _read_memory
    from minicode.memory import (
        MemoryFile,
        MemoryManager,
        MemoryPaths,
        MemoryScope,
        _memory_write_lock,
    )

    paths = MemoryPaths.for_workspace(workspace)
    removed = 0
    for scope in MemoryScope:
        root = getattr(paths, scope.value + "_memory")
        path = root / "memory.json"
        for name in ("memory.json", "MEMORY.md", ".memory.lock"):
            if is_unsafe_stored_path(root / name, boundary=root.parent, reject_hardlinks=True):
                raise ValueError("unsafe memory path during session deletion")
        if not path.exists():
            continue
        with _memory_write_lock(root / ".memory.lock"):
            data, entries = _read_memory(path, scope.value)
            deleted = {entry.id for entry in entries
                       if entry.category in TASK_CATEGORIES
                       and entry.origin not in {"user_explicit", "user_corrected"}
                       and set(source_sessions(entry)) == {session_id}}
            if not deleted:
                continue
            retained = [entry for entry in entries if entry.id not in deleted]
            data["entries"] = [raw for raw in data["entries"] if raw["id"] not in deleted]
            data["last_updated"] = time.time()
            MemoryManager._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
            MemoryManager._atomic_write(root / "MEMORY.md", MemoryFile(scope=scope, entries=retained).format_as_markdown())
            removed += len(deleted)
    return removed
