"""Evidence and retention rules for automatically learned cross-task knowledge.

Task history remains evidence, never a rule file. An insight is advisory and
valid only while its original evidence versions are available and unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from minicode.memory_lifecycle import DAY_SECONDS

REVIEW_SECONDS = 90 * DAY_SECONDS
PROTECTED_ORIGINS = {"user_explicit", "user_corrected"}
REUSABLE_CATEGORIES = {"architecture", "convention", "decision", "pattern", "workflow", "lesson"}
_TRANSIENT = re.compile(
    r"(?:\b\d+\s+(?:tests?\s+)?passed\b|\b(?:port|pid)\s*[:=]?\s*\d{2,}\b|"
    r"(?:端口|进程号)\s*[:：]?\s*\d+|(?:本次|这次|当前).{0,8}(?:测试通过|已完成)|"
    r"task completed successfully|used \d+ unique tool)", re.IGNORECASE,
)


def protected(entry: Any) -> bool:
    return entry.origin in PROTECTED_ORIGINS or bool(set(entry.tags) & {"pinned", "permanent"})


def evidence_version(entry: Any) -> str:
    """Access counters and maintenance clocks do not change an evidence version."""
    value = [entry.scope.value, entry.category, entry.content, entry.origin, sorted(entry.sources)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode("utf-8")).hexdigest()


def insight_id(entries: list[Any]) -> str:
    versions = sorted((entry.id, evidence_version(entry)) for entry in entries)
    return "insight-" + hashlib.sha256(json.dumps(versions).encode()).hexdigest()


def verified_task_evidence(entry: Any) -> bool:
    """Old permission-only receipts remain viewable, but cannot teach a rule."""
    from minicode.runtime_memory import POLICY
    from minicode.task_history import TASK_CATEGORIES
    from minicode.workspace_files import is_internal_state_path

    if entry.category not in TASK_CATEGORIES:
        return True
    try:
        record = json.loads(entry.content[entry.content.index("{"):])
        count, paths = record.get("project_change_count"), record.get("project_changes")
        return (record.get("policy") == POLICY and type(count) is int and count > 0
                and isinstance(paths, list) and 0 < len(paths) <= count
                and all(isinstance(name, str) and name and not is_internal_state_path(name) for name in paths))
    except (ValueError, TypeError, AttributeError):
        return False


class LearningPolicy:
    """One read-only snapshot, including edits from other running sessions."""

    def __init__(self, manager: Any, *, now: float | None = None):
        from minicode.context_sources import _read_memory
        from minicode.path_safety import is_unsafe_stored_path
        from minicode.task_history import HistoryPolicy

        self.now = time.time() if now is None else now
        self.history = HistoryPolicy(manager.workspace)
        self.entries = {}
        self.unavailable = set()
        for scope in manager.memories:
            root = manager._get_scope_path(scope)
            path = root / "memory.json"
            try:
                if is_unsafe_stored_path(path, boundary=root.parent, reject_hardlinks=True):
                    raise ValueError("unsafe memory evidence path")
                if not path.is_file() and (path.exists() or (root / "MEMORY.md").exists()
                                          or manager._persisted_entries.get(scope)):
                    raise ValueError("memory evidence index unavailable")
                # Unsaved in-process entries can be inspected, but cannot be
                # used as evidence for an automatic durable write.
                entries = _read_memory(path, scope.value)[1] if path.is_file() else []
                self.entries.update((entry.id, entry) for entry in entries)
            except (OSError, ValueError, TypeError):
                self.unavailable.add(scope)

    @classmethod
    def from_snapshot(cls, workspace: str, entries: list[Any], *, unavailable: bool = False):
        """Reuse the inspector's read-only snapshot without creating stores."""
        from minicode.task_history import HistoryPolicy
        policy = cls.__new__(cls)
        policy.now, policy.history = time.time(), HistoryPolicy(workspace)
        policy.entries = {entry.id: entry for entry in entries}
        policy.unavailable = {"unavailable"} if unavailable else set()
        return policy

    def state(self, entry: Any) -> str:
        if entry.tier.value == "archival":
            return "archived"
        if protected(entry) or entry.origin != "agent_consolidated":
            return "eligible"
        if not entry.source_versions or not entry.review_after or not entry.confirmed_version:
            return "legacy_unverified"
        if self.now >= entry.review_after:
            return "expired"
        for source_id, version in entry.source_versions.items():
            source = self.entries.get(source_id)
            if source is None:
                return "source_unavailable" if self.unavailable else "source_missing"
            if source.tier.value == "archival":
                return "source_archived"
            if evidence_version(source) != version:
                return "source_changed"
            if source.origin == "agent_consolidated":
                return "source_unverified"
            source_state = self.source_state(source)
            if source_state != "eligible":
                return source_state
        if self.unavailable:
            return "source_unavailable"
        if entry.confirmed_version != self.confirmed_version:
            return "confirmation_changed"
        return "eligible"

    @property
    def confirmed_version(self) -> str:
        confirmed = sorted((e.id, evidence_version(e)) for e in self.entries.values()
                           if protected(e) and e.tier.value != "archival")
        return hashlib.sha256(json.dumps(confirmed).encode()).hexdigest()

    def source_state(self, entry: Any) -> str:
        from minicode.task_history import source_sessions
        if not verified_task_evidence(entry):
            return "source_unverified"
        sessions = source_sessions(entry)
        if not sessions:
            return "source_unverified"
        states = {self.history.session(sid)["status"] for sid in sessions}
        if "unavailable" in states:
            return "source_unavailable"
        if "missing" in states:
            return "source_missing"
        if "archived" in states:
            return "source_archived"
        return "eligible"

    def source_available(self, entry: Any) -> bool:
        return self.source_state(entry) == "eligible"

    def candidate(self, entry: Any) -> bool:
        from minicode.task_history import (
            TASK_CATEGORIES,
            history_title,
            internal_task,
            trivial_task,
        )
        if (entry.tier.value == "archival" or protected(entry)
                or entry.category not in REUSABLE_CATEGORIES | TASK_CATEGORIES
                or entry.origin == "agent_consolidated" or not self.source_available(entry)):
            return False
        if not 30 <= len(entry.content) <= 6000:
            return False
        if entry.category in TASK_CATEGORIES:
            title = history_title(entry)
            if trivial_task(title) or internal_task(title):
                return False
        elif _TRANSIENT.search(entry.content):
            return False
        return True

    def corroborated(self, entries: list[Any]) -> bool:
        from minicode.task_history import source_sessions
        return (3 <= len(entries) <= 5 and all(self.candidate(entry) for entry in entries)
                and len({sid for entry in entries for sid in source_sessions(entry)}) >= 2
                and len({entry.content.strip() for entry in entries}) >= 3)

    def select_evidence(self, entries: list[Any]) -> list[Any]:
        """Bound a cluster while preserving source and content diversity."""
        from minicode.task_history import source_sessions
        candidates = sorted((entry for entry in entries if self.candidate(entry)), key=lambda entry: entry.id)
        if not candidates:
            return []
        selected = [candidates[0]]
        sessions = set(source_sessions(selected[0]))
        if len(sessions) < 2:
            other = next((entry for entry in candidates[1:] if set(source_sessions(entry)) - sessions), None)
            if other is None:
                return []
            selected.append(other)
        contents = {entry.content.strip() for entry in selected}
        for entry in candidates:
            if len(selected) == 5:
                break
            if entry not in selected and entry.content.strip() not in contents:
                selected.append(entry)
                contents.add(entry.content.strip())
        return selected if self.corroborated(selected) else []


def parse_insight(raw: str, entries: list[Any]) -> tuple[str, list[Any]] | None:
    """The model proposes a bounded fact and cites only supplied evidence IDs."""
    if not isinstance(raw, str) or len(raw) > 8192:
        return None
    text = raw.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict) or result.get("decision") != "remember":
        return None
    if set(result) != {"decision", "content", "source_ids"}:
        return None
    content, ids = result["content"], result["source_ids"]
    if (not isinstance(content, str) or not 20 <= len(content.strip()) <= 600
            or _TRANSIENT.search(content) or not isinstance(ids, list)
            or not all(isinstance(item, str) for item in ids)
            or not 3 <= len(set(ids)) <= 5 or len(ids) != len(set(ids))):
        return None
    by_id = {entry.id: entry for entry in entries}
    if any(item not in by_id for item in ids):
        return None
    return content.strip(), [by_id[item] for item in ids]


def retain_insight(manager: Any, content: str, evidence: list[Any], *, confirmed_version: str | None = None) -> bool:
    """Recheck after synthesis; keep originals and never evict to make room."""
    from copy import deepcopy

    from minicode.memory import MemoryEntry, MemoryScope, MemoryTier

    with manager._mutation_lock:
        policy = LearningPolicy(manager)
        if policy.unavailable or confirmed_version is not None and confirmed_version != policy.confirmed_version:
            return False
        current = [policy.entries.get(entry.id) for entry in evidence]
        if (any(entry is None for entry in current) or not policy.corroborated(current)
                or any(evidence_version(old) != evidence_version(new) for old, new in zip(evidence, current))):
            return False
        eid = insight_id(evidence)
        # This also respects a user's archive/correction of the same insight.
        if eid in policy.entries:
            return False
        scope = MemoryScope.LOCAL
        memory = manager.memories[scope]
        before, baseline = deepcopy(memory), deepcopy(manager._persisted_entries.get(scope, {}))
        memory.entries.append(MemoryEntry(
            id=eid, scope=scope, category="insight", content=content,
            origin="agent_consolidated", tier=MemoryTier.SHORT_TERM,
            sources=["memory:" + entry.id for entry in evidence],
            source_versions={entry.id: evidence_version(entry) for entry in evidence},
            confirmed_version=policy.confirmed_version,
            review_after=policy.now + REVIEW_SECONDS,
            domains=sorted({domain for entry in evidence for domain in entry.domains}),
            related_to=[entry.id for entry in evidence], tags=["curator-insight"],
        ))
        try:
            manager._save_scope(scope, maintenance=True, preserve_existing=True)
        except Exception:
            manager.memories[scope], manager._persisted_entries[scope] = before, baseline
            raise
        return True
