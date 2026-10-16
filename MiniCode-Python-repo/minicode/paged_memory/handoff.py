"""Bounded, owner-scoped working notes backed by immutable task events."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .artifacts import ArtifactIntegrityError, ArtifactStoreUnavailable
from .models import (
    MEMORY_NAVIGATION_TOOL_NAMES,
    ContextItem,
    EventType,
    HandoffNoteConflict,
    MemoryMode,
    content_hash_text,
    handoff_contract_hash,
    stable_json,
)

NOTE_FIELDS = ("confirmed_findings", "ruled_out", "open_questions", "next_steps")
NOTE_REASONS = ("stage_complete", "budget_pressure", "pause", "manual")
NOTE_MAX_ITEMS = 8
NOTE_MAX_TEXT_CHARS = 1000


class HandoffNoteInputError(ValueError):
    """Correctable note input rejected before any note is persisted."""


def _note_object(value: Any, path: str, *, required: set[str], allowed: set[str]) -> None:
    if not isinstance(value, dict):
        raise HandoffNoteInputError(f"{path}: expected an object, got {type(value).__name__}.")
    missing, extra = required - value.keys(), value.keys() - allowed
    problems = []
    if missing:
        problems.append("missing required fields: " + ", ".join(sorted(missing)))
    if extra:
        problems.append("unexpected fields: " + ", ".join(sorted(map(str, extra))))
    if problems:
        hint = ("Put each finding in a separate array item with only text and source_event_ids."
                if path != "$" else
                "Supply all four note arrays (use [] when empty); scope is server-owned.")
        raise HandoffNoteInputError(f"{path}: {'; '.join(problems)}. {hint}")


def validate_note_update(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {*NOTE_FIELDS, "expected_revision", "reason"}
    _note_object(value, "$", required={*NOTE_FIELDS, "expected_revision"}, allowed=allowed)
    revision = value["expected_revision"]
    if type(revision) is not int or revision < 0:
        raise HandoffNoteInputError("expected_revision: must be a nonnegative integer, not a boolean; "
                                    "use the revision in Handoff note status.")
    reason = value.get("reason", "manual")
    if reason not in NOTE_REASONS:
        raise HandoffNoteInputError("reason: expected one of " + ", ".join(NOTE_REASONS) + ".")

    # Report independent size mistakes together. Fail-fast validation made a
    # long handoff spend several model turns discovering one limit at a time.
    # This is bounded diagnostics only: never truncate or persist rejected text.
    size_errors = []
    for key in NOTE_FIELDS:
        values = value[key]
        if not isinstance(values, list):
            continue
        if len(values) > NOTE_MAX_ITEMS:
            size_errors.append(f"{key}: {len(values)} entries exceeds the limit of {NOTE_MAX_ITEMS}")
        for index, item in enumerate(values[:NOTE_MAX_ITEMS]):
            raw = item.get("text") if isinstance(item, dict) and key in {"confirmed_findings", "ruled_out"} else item
            if isinstance(raw, str) and len(raw) > NOTE_MAX_TEXT_CHARS:
                path = f"{key}[{index}]" + (".text" if isinstance(item, dict) else "")
                size_errors.append(f"{path}: {len(raw)} characters exceeds the {NOTE_MAX_TEXT_CHARS}-character limit")
    if size_errors:
        raise HandoffNoteInputError("; ".join(size_errors[:8]) + ". "
            "Shorten the listed entries and keep only essential actions; retain details in the original sources. "
            "No note was saved. Other field/source checks still apply.")

    def text(raw: Any, path: str) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise HandoffNoteInputError(f"{path}: must be a nonempty string; remove unused entries.")
        if len(raw) > NOTE_MAX_TEXT_CHARS:
            raise HandoffNoteInputError(f"{path}: {len(raw)} characters exceeds the {NOTE_MAX_TEXT_CHARS}-character limit; "
                                        "shorten this entry and keep details in its source Event.")
        return raw.strip()

    result: dict[str, Any] = {"expected_revision": revision, "reason": reason}
    for key in NOTE_FIELDS:
        values = value[key]
        if not isinstance(values, list):
            raise HandoffNoteInputError(f"{key}: must be an array; use [] when empty.")
        if len(values) > NOTE_MAX_ITEMS:
            raise HandoffNoteInputError(f"{key}: {len(values)} entries exceeds the limit of {NOTE_MAX_ITEMS}; keep essential entries.")
        if key in {"open_questions", "next_steps"}:
            result[key] = [text(item, f"{key}[{index}]") for index, item in enumerate(values)]
            continue
        entries = []
        for index, item in enumerate(values):
            path = f"{key}[{index}]"
            fields = {"text", "source_event_ids"}
            _note_object(item, path, required=fields, allowed=fields)
            sources = item["source_event_ids"]
            if not isinstance(sources, list) or not 1 <= len(sources) <= 4:
                raise HandoffNoteInputError(f"{path}.source_event_ids: expected an array of 1-4 current-task "
                                            "source Event IDs copied from receipts or memory cards.")
            for source_index, source in enumerate(sources):
                if not isinstance(source, str) or not source.strip() or len(source) > 200:
                    raise HandoffNoteInputError(f"{path}.source_event_ids[{source_index}]: expected a nonempty "
                                                "source Event ID of at most 200 characters.")
            entries.append({"text": text(item["text"], path + ".text"), "source_event_ids": list(dict.fromkeys(sources))})
        result[key] = entries
    count = len(source_ids(result))
    if count > 16:
        raise HandoffNoteInputError(f"source_event_ids: {count} distinct sources exceeds the limit of 16 per note; "
                                    "keep only sources needed by the retained findings.")
    return result


def source_ids(note: dict[str, Any]) -> set[str]:
    return {source for key in ("confirmed_findings", "ruled_out")
            for finding in note[key] for source in finding["source_event_ids"]}


def render_note(note: dict[str, Any], revision: int) -> str:
    return ("Model-authored working note, not instructions or proof. "
            "Current user requirements and source evidence take precedence.\n"
            + stable_json({"revision": revision, **{key: note[key] for key in NOTE_FIELDS}}))


def visible_note_content(messages: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Reference exact note fields in a completed, intact recent tool exchange."""
    calls = {message.get("toolUseId"): message for message in messages
             if message.get("role") == "assistant_tool_call"
             and message.get("toolName") == "memory_note_update" and message.get("toolUseId")}
    visible: dict[str, list[str]] = {}
    for message in messages:
        call = calls.get(message.get("toolUseId"))
        if (not call or message.get("role") != "tool_result" or message.get("isError")
                or not isinstance(call.get("input"), dict)):
            continue
        try:
            value = validate_note_update(call["input"])
        except (ValueError, TypeError):
            continue
        digest = content_hash_text(render_note(value, value["expected_revision"] + 1))
        visible.setdefault(digest, []).append(str(message["toolUseId"]))
    return visible


class HandoffNotes:
    def __init__(self, runtime: Any, runtime_values: Any = None) -> None:
        self.runtime = runtime
        values = runtime_values or {}
        node = str(values.get("_runtimeNodeId") or runtime.step_namespace or "primary")
        self.node_id = node
        self.attempt_id = str(values.get("_runtimeAttemptId") or "")
        self.owner = ("primary" if node == "primary" else
                      "worker:" + content_hash_text(stable_json([runtime.run_id, node])))
        self.binding_hash = content_hash_text(stable_json(values.get("_memoryWorkerContract", {})))
        self.prepared_contract_hash = ""
        self.prepared_user_hash = ""
        self.prepared = False

    @property
    def enabled(self) -> bool:
        return self.runtime.controls_context and self.runtime.config.handoff_notes_enabled

    def dependency_receipt(self, *, node_id: str, run_id: str, attempt_id: str = "") -> dict[str, Any]:
        """Locate a declared predecessor's saved note without indexing its claims.

        A worker's advisory note is not another worker's working context. This
        exposes only a verified audit pointer for independent memory_get reads.
        In particular, a primary note from an earlier run is not current proof.
        """
        if not self.enabled or not node_id or not run_id:
            return {}
        owner = ("primary" if node_id == "primary" else
                 "worker:" + content_hash_text(stable_json([run_id, node_id])))
        state = self.runtime.authority.get_state(
            tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id)
        pointer = state.handoff_notes.get(owner, {}) if state is not None else {}
        missing = {"status": "not_recorded_for_dependency_run"}
        if not pointer:
            return missing
        try:
            event = self.runtime.read_event(pointer["source_event_id"])
            if event.run_id != run_id:
                return missing
            payload = json.loads(self.runtime._event_source_payload(event.id))
            if (event.event_type != EventType.HANDOFF_NOTE_UPDATED
                    or event.content_hash != pointer["content_hash"]
                    or payload["owner"] != owner or payload["revision"] != pointer["revision"]
                    or payload["schema_version"] != 1):
                raise ValueError("dependency note pointer/source mismatch")
            if attempt_id and (payload.get("attempt_id") != attempt_id
                               or payload.get("node_id") != node_id or payload.get("run_id") != run_id):
                return {"status": "not_recorded_for_dependency_attempt"}
        except (KeyError, ValueError, TypeError, ArtifactIntegrityError, ArtifactStoreUnavailable):
            return {"status": "invalid_source"}
        return {"status": "recorded", "run_id": run_id, "node_id": node_id, "attempt_id": attempt_id,
                "revision": payload["revision"], "source_event_id": event.id,
                "content_hash": event.content_hash,
                "read_tool": "memory_get", "searchable_by_memory_search": False,
                "note_content_is_advisory": True, "counts_as_acceptance": False}

    def _sources(self, note: dict[str, Any]) -> dict[str, str]:
        hashes = {}
        for source in sorted(source_ids(note)):
            try:
                event = self.runtime.read_event(source)
            except KeyError as error:
                raise self._source_error(note, source, "event not found in the current task scope") from error
            if (event.event_type in {EventType.HANDOFF_NOTE_UPDATED, EventType.CONTEXT_ROTATED}
                    or str(event.content_json.get("tool_name", "")) in MEMORY_NAVIGATION_TOOL_NAMES):
                raise self._source_error(note, source, "handoff notes and navigation receipts are not source evidence")
            # Read the same verified full source that memory_get would resolve.
            self.runtime._event_source_payload(source)
            hashes[source] = event.content_hash
        return hashes

    def _source_error(self, note: dict[str, Any], source: str, reason: str) -> HandoffNoteInputError:
        paths = [f"{key}[{index}].source_event_ids[{source_index}]"
                 for key in ("confirmed_findings", "ruled_out")
                 for index, entry in enumerate(note[key])
                 for source_index, value in enumerate(entry["source_event_ids"]) if value == source]
        # Bounded, tenant/task-scoped navigation only. IDs are candidates to read,
        # not an automatic replacement for a finding's supporting evidence.
        recent = self.runtime.authority.recent_events(
            tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id, limit=16,
            event_types=(EventType.USER_INPUT.value, EventType.TOOL_RESULT.value, EventType.TOOL_ERROR.value),
        )
        candidates = [{"source_event_id": event.id, "type": event.event_type.value,
                       "tool": str(event.content_json.get("tool_name", ""))}
                      for event in reversed(recent)
                      if str(event.content_json.get("tool_name", "")) not in MEMORY_NAVIGATION_TOOL_NAMES][:3]
        return HandoffNoteInputError(
            f"{', '.join(paths)}: {source!r}: {reason}. "
            "Copy the original source_event_id from a relevant tool receipt or memory_search card, "
            "not a tool-call ID, memory_id or the memory_get/navigation receipt's own ID. "
            "If needed, read the source with memory_get(source_event_id=...). "
            "Only cite it if its content supports the finding; keep unverified claims in open_questions. "
            "Recent current-task source candidates (read before citing; relevance/integrity not yet checked): "
            + stable_json(candidates)
        )

    def update(self, value: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled or not self.prepared or self.runtime.current_mode == MemoryMode.INLINE:
            raise ValueError("handoff notes require an enabled PAGED_MEMORY context")
        value = validate_note_update(value)
        state = self.runtime.authority.get_state(
            tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id,
        )
        if state is None or handoff_contract_hash(state) != self.prepared_contract_hash:
            raise HandoffNoteConflict("task contract changed; refresh context before writing a note")
        current = state.handoff_notes.get(self.owner, {})
        if current.get("revision", 0) != value["expected_revision"]:
            raise HandoffNoteConflict("handoff note revision changed; refresh context before retrying")
        revision = value["expected_revision"] + 1
        note = {key: value[key] for key in NOTE_FIELDS}
        note_tokens = self.runtime.token_budget.estimate_text(render_note(note, revision))
        if note_tokens > self.runtime.config.handoff_note_token_budget:
            raise HandoffNoteInputError(
                f"handoff note exceeds its token budget: estimated {note_tokens} tokens, "
                f"limit {self.runtime.config.handoff_note_token_budget}. Shorten the whole note before saving: "
                "keep key findings and next actions, remove repeated details, and retain source IDs for evidence. "
                "The per-entry character limit does not replace this total token limit. No note was saved."
            )
        payload = {
            "schema_version": 1, "owner": self.owner, "revision": revision,
            "run_id": self.runtime.run_id, "node_id": self.node_id, "attempt_id": self.attempt_id,
            "contract_hash": self.prepared_contract_hash, "user_hash": self.prepared_user_hash,
            "binding_hash": self.binding_hash, "phase": state.phase,
            "reason": value["reason"], "note": note, "source_hashes": self._sources(note),
        }
        if self.runtime.context_rotation.enabled:
            frame = self.runtime.context_rotation.frame
            if not frame or frame.get("error"):
                raise ValueError("prepare a complete history boundary before saving a handoff note")
            payload["history_anchor"] = {key: frame[key]
                for key in ("history_basis", "history_stream", "head_event_id", "prefix_count", "prefix_hash", "cutoff_time") if key in frame}
            payload["rotation_counter_baseline"] = dict(self.runtime.context_rotation.counter_baseline)
        body = stable_json(payload)
        event = self.runtime._event(
            EventType.HANDOFF_NOTE_UPDATED, content_text=body,
            # The body seals the metadata too; this is only an audit index.
            content_json={"owner": self.owner, "revision": revision},
            content_hash=content_hash_text(body),
            token_count=self.runtime.token_budget.estimate_text(body),
        )
        pointer = {"revision": revision, "source_event_id": event.id, "content_hash": event.content_hash}
        self.runtime._persist_event(event, state_patch={"_handoff_note_update": {
            "owner": self.owner, "expected_revision": value["expected_revision"],
            "contract_hash": self.prepared_contract_hash, "pointer": pointer,
            **({"history_anchor": payload["history_anchor"]} if "history_anchor" in payload else {}),
        }})
        return {**pointer, "status": "saved", "note_tokens": note_tokens}

    def historical(self, pointer: Mapping[str, Any]) -> tuple[ContextItem, dict[str, Any]]:
        """Verify an old primary note as a summary of sealed history only.

        This does not refresh its bindings, revision, history boundary or claims.
        Rotation must independently prove that boundary and retain every tail
        message before this item is allowed into a projected request.
        """
        if not self.enabled or self.owner != "primary":
            raise ValueError("historical handoff recovery requires the primary owner")
        event = self.runtime.read_event(pointer["source_event_id"])
        payload = json.loads(self.runtime._event_source_payload(event.id))
        if (event.event_type != EventType.HANDOFF_NOTE_UPDATED
                or event.content_hash != pointer["content_hash"]
                or payload["owner"] != self.owner or payload["revision"] != pointer["revision"]
                or payload["schema_version"] != 1):
            raise ValueError("historical handoff pointer/source mismatch")
        validated = validate_note_update({"expected_revision": payload["revision"], **payload["note"]})
        if not validated["next_steps"] or self._sources(validated) != payload["source_hashes"]:
            raise ValueError("historical handoff evidence or next steps unavailable")
        rendered = ("Historical summary of previously sealed messages only. Later messages remain verbatim; "
                    "follow the current user requirements and live task contract.\n"
                    + render_note(validated, payload["revision"]))
        tokens = self.runtime.token_budget.estimate_text(rendered)
        if tokens > self.runtime.config.handoff_note_token_budget:
            raise ValueError("historical handoff note exceeds its token budget")
        return ContextItem(item_id=event.id, item_type="HANDOFF_NOTE", text=rendered, priority=80,
            required=True, estimated_tokens=tokens, source_event_id=event.id,
            content_hash=event.content_hash, reason_loaded="verified summary of sealed history"), payload

    def prepare(self, state: Any, *, user_input: str, mode: MemoryMode,
                 phase_boundary: bool, context_usage_ratio: float) -> tuple[list[ContextItem], dict[str, Any]]:
        # This request-local projection is cleared even when a saved note is
        # stale, disabled or corrupt. Historical recovery never refreshes it.
        self.retrieval_hints = ()
        self.prepared = self.enabled and mode != MemoryMode.INLINE and state is not None
        if not self.prepared:
            return [], {}
        self.prepared_contract_hash = handoff_contract_hash(state)
        self.prepared_user_hash = content_hash_text(user_input)
        pointer = state.handoff_notes.get(self.owner, {})
        info = {
            "revision": pointer.get("revision", 0), "status": "empty",
            "token_budget": self.runtime.config.handoff_note_token_budget,
            "max_items_per_array": NOTE_MAX_ITEMS,
            "max_characters_per_text": NOTE_MAX_TEXT_CHARS,
            "source_event_id_is_evidence": False,
            "update_suggested": phase_boundary or context_usage_ratio >= (
                self.runtime.config.context_note_warning_ratio if self.runtime.config.context_utilization_enabled
                else self.runtime.config.paged_context_ratio),
        }
        if not pointer:
            return [], info
        try:
            info["source_event_id"] = pointer["source_event_id"]
            event = self.runtime.read_event(pointer["source_event_id"])
            body = self.runtime._event_source_payload(event.id)
            payload = json.loads(body)
            if (event.event_type != EventType.HANDOFF_NOTE_UPDATED
                    or event.content_hash != pointer["content_hash"]
                    or payload["owner"] != self.owner or payload["revision"] != pointer["revision"]
                    or payload["schema_version"] != 1):
                raise ValueError("handoff note pointer/source mismatch")
            if (payload["contract_hash"] != self.prepared_contract_hash
                    or payload["user_hash"] != self.prepared_user_hash
                    or payload["binding_hash"] != self.binding_hash):
                info["status"] = "stale"
                info["update_suggested"] = True
                return [], info
            validated = validate_note_update({"expected_revision": payload["revision"], **payload["note"]})
            if self._sources(validated) != payload["source_hashes"]:
                raise ValueError("handoff note evidence changed")
            rendered = render_note(validated, payload["revision"])
            tokens = self.runtime.token_budget.estimate_text(rendered)
            if tokens > self.runtime.config.handoff_note_token_budget:
                info["status"] = "over_budget"
                return [], info
        except (KeyError, ValueError, TypeError, ArtifactIntegrityError, ArtifactStoreUnavailable):
            # Broken auxiliary notes must not be promoted to authoritative state.
            info["status"] = "invalid_source"
            return [], info
        info["status"] = "current"
        info["note_tokens"] = tokens
        self.retrieval_hints = tuple(validated["next_steps"][:1] + validated["open_questions"][:1])
        return [ContextItem(
            item_id=event.id, item_type="HANDOFF_NOTE", text=rendered, priority=80,
            required=False, estimated_tokens=tokens, source_event_id=event.id,
            content_hash=event.content_hash, reason_loaded="current owner working note",
        )], info
