"""Explicit, durable request projections; canonical history is never modified."""
from __future__ import annotations

import json
from itertools import combinations, islice, permutations
from collections.abc import Mapping, Sequence
from typing import Any

from minicode.runtime_messages import is_user_input, runtime_context_kind, runtime_message_kind

from .artifacts import ArtifactIntegrityError, ArtifactStoreUnavailable
from .models import (
    ContextRotationConflict, EventType, MemoryMode, content_hash_text,
    handoff_contract_hash, now_epoch, stable_json,
)
from .history_journal import HISTORY_BASIS, HistoryJournal, HistoryIntegrityError, durable_indices, boundary, verify_boundary

ROTATION_REASONS = ("stage_complete", "budget_pressure", "manual")
STABLE_HISTORY_BASIS = HISTORY_BASIS
ROTATION_COMMIT_ATTEMPTS = 3
# Only snapshots that AgentLoop removes and rebuilds between requests belong
# outside the sealed history. Persistent controls and all human input stay in it.
_REPLACEABLE_CONTEXT_KINDS_V1 = frozenset({"task_state", "tool_input_recovery", "tool_recovery_lessons"})
_REPLACEABLE_CONTEXT_KINDS_V2 = _REPLACEABLE_CONTEXT_KINDS_V1 | {"shared_writer_coordination"}
_ROLE_SNAPSHOTS = frozenset({"primary_assignment", "primary_delegation"})
_REPLACEABLE_CONTEXT_KINDS = _REPLACEABLE_CONTEXT_KINDS_V2 | _ROLE_SNAPSHOTS | {"task_check_evidence"}
_HISTORY_CONTEXT_KINDS = {
    "stable-history-v1": _REPLACEABLE_CONTEXT_KINDS_V1,
    "stable-history-v2": _REPLACEABLE_CONTEXT_KINDS_V2,
    "stable-history-v3": _REPLACEABLE_CONTEXT_KINDS,
}
_PROTECTED_FACETS = {"user_instruction", "task_contract", "acceptance_criteria", "required_evidence"}


def validate_rotation(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"expected_epoch", "expected_note_revision", "reason"}:
        raise ValueError("rotation accepts only expected_epoch, expected_note_revision and reason")
    for name, minimum in (("expected_epoch", 0), ("expected_note_revision", 1)):
        if type(value.get(name)) is not int or value[name] < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    reason = value.get("reason", "stage_complete")
    if reason not in ROTATION_REASONS:
        raise ValueError("unknown context rotation reason")
    return {**value, "reason": reason}


def _digest(messages: Sequence[Mapping[str, Any]]) -> str:
    # Provider reasoning may be stripped on persistence; it cannot change the
    # identity of instructions, calls or results at the rotation boundary.
    return content_hash_text(stable_json([
        {key: value for key, value in message.items() if key != "reasoningContent"}
        for message in messages
    ]))


def _anchor_indices(messages: Sequence[Mapping[str, Any]], anchor: Mapping[str, Any]) -> list[int]:
    # Immutable older records used the complete history, including snapshots.
    # Never reinterpret their counts or hashes in the new coordinate system.
    if "history_basis" not in anchor:
        return list(range(len(messages)))
    # Freeze each version's coordinates: persisted counts and hashes must not
    # change meaning when a newly recognized snapshot is excluded.
    basis = anchor["history_basis"]
    if basis == HISTORY_BASIS:
        return durable_indices(messages)
    excluded = _HISTORY_CONTEXT_KINDS.get(basis) if isinstance(basis, str) else None
    if excluded is None:
        raise ValueError("unsupported rotation history basis")
    positions = [index for index, message in enumerate(messages)
                 if runtime_context_kind(message) not in excluded]
    if basis == "stable-history-v2":
        positions = _reconcile_role_positions(messages, anchor, positions)
    return positions


def _reconcile_role_positions(messages, anchor, positions):
    """Recover old checkpoints only when relocating native policy proves the exact seal.

    Older resumes removed and reappended these two producer-owned snapshots.
    Never change the saved hash, relax evidence checks, or rewrite canonical history.
    The returned indices are a verified coordinate view, not a repaired receipt.
    """
    count = anchor.get("prefix_count")
    if type(count) is not int or not 0 < count <= len(positions):
        return positions
    expected = anchor.get("prefix_hash")
    if _digest([messages[i] for i in positions[:count]]) == expected:
        return positions
    # Subsequent activations keep the original bytes and append updated policy.
    # Only the earliest copy of each native kind can repair this old boundary.
    first_policy = {}
    for i in positions:
        kind = runtime_context_kind(messages[i])
        if kind in _ROLE_SNAPSHOTS:
            first_policy.setdefault(kind, i)
    policies = list(first_policy.values())
    if not policies:
        return positions
    slots = anchor.get("retained_indices", [])
    if (not isinstance(slots, list) or len(slots) > 64
            or any(type(i) is not int or not 0 <= i < count for i in slots)
            or slots != sorted(set(slots))):
        return positions
    base = [i for i in positions if i not in policies]
    # Recent retained slots are the usual policy location. Bound recovery work
    # for malformed/very large anchors; a miss still fails the original seal.
    for targets in islice(combinations(reversed(slots), len(policies)), 64):
        for ordered in permutations(policies):
            candidate = list(base)
            for target, index in zip(sorted(targets), ordered):
                candidate.insert(target, index)
            if _digest([messages[i] for i in candidate[:count]]) == expected:
                return candidate
    return positions


def history_for_anchor(messages: Sequence[Mapping[str, Any]], anchor: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Use the same versioned boundary for note freshness, projection and counters."""
    return [messages[index] for index in _anchor_indices(messages, anchor)]


def verify_anchor(history, anchor):
    if anchor.get("history_basis") == HISTORY_BASIS:
        verify_boundary(history, anchor)
    elif _digest(history[:anchor["prefix_count"]]) != anchor["prefix_hash"]:
        raise ValueError("rotation prefix fingerprint does not match this history")


def _retained_indices(messages: Sequence[Mapping[str, Any]]) -> list[int]:
    """Validate complete exchanges and retain entire overlapping protected groups."""
    calls: dict[str, int] = {}
    seen: set[str] = set()
    ends: dict[int, int] = {}
    for index, message in enumerate(messages):
        role = message.get("role")
        if role not in {"user", "assistant", "assistant_progress", "assistant_tool_call", "tool_result"}:
            raise ValueError("unsupported history role at rotation boundary")
        if role not in {"assistant_tool_call", "tool_result"}:
            continue
        tool_id = str(message.get("toolUseId") or "")
        if not tool_id:
            raise ValueError("tool history has no call ID")
        if role == "assistant_tool_call":
            if tool_id in seen:
                raise ValueError("duplicate tool call ID in history")
            seen.add(tool_id)
            calls[tool_id] = index
        else:
            if tool_id not in calls:
                raise ValueError("orphan or duplicate tool result in history")
            start = calls.pop(tool_id)
            if message.get("toolName") != messages[start].get("toolName"):
                raise ValueError("tool call/result name mismatch in history")
            ends[start] = index
    if calls:
        raise ValueError("incomplete tool exchange at rotation boundary")
    retained: list[int] = []
    start = 0
    while start < len(messages):
        end = ends.get(start, start)
        cursor = start
        while cursor <= end:
            end = max(end, ends.get(cursor, cursor))
            cursor += 1
        if any(is_user_input(m) or runtime_message_kind(m) == "control" for m in messages[start:end + 1]):
            retained.extend(range(start, end + 1))
        start = end + 1
    return retained


def project_history(messages: Sequence[Mapping[str, Any]], info: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pure, repeatable projection used by both native and shared-budget requests."""
    original = [dict(message) for message in messages]
    if info.get("status") != "active":
        return original
    anchor = info["anchor"]
    positions = _anchor_indices(original, anchor)
    history = [original[index] for index in positions]
    count = anchor["prefix_count"]
    if type(count) is not int or count <= 0 or count > len(history):
        raise ValueError("rotation prefix is unavailable in this history")
    prefix = history[:count]
    verify_anchor(history, anchor)
    keep = _retained_indices(prefix)
    if keep != anchor["retained_indices"]:
        raise ValueError("rotation protected groups do not match this history")
    _retained_indices(original)  # Also rejects reused IDs crossing the boundary.
    retained = set(keep)
    omitted = {positions[index] for index in range(count) if index not in retained}
    # Keep current snapshots verbatim and in their supplied order, regardless of
    # where the old snapshots were when the note/rotation was committed.
    return [message for index, message in enumerate(original) if index not in omitted]


class ContextRotation:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.frame: dict[str, Any] = {}
        self.messages: Sequence[Mapping[str, Any]] = ()
        self.user_input = ""
        self.counter_baseline: dict[str, Any] = {}
        self.historical_note_items: list[Any] = []
        self.journal = HistoryJournal(runtime)
        self.request_messages = []
        self.legacy_messages = []
        self.legacy_anchor = {}
        self.initial_history = None

    @property
    def enabled(self) -> bool:
        return self.runtime.handoff_notes.enabled and self.runtime.config.context_rotation_enabled

    def remember_initial_history(self, messages) -> None:
        # Native input capture runs before steering/recovery snapshots refresh.
        # Keep this original checkpoint available for a one-time legacy proof.
        if self.enabled and self.initial_history is None:
            self.initial_history = json.loads(stable_json([m for m in messages if m.get("role") != "system"]))

    def capture(self, messages: Sequence[Mapping[str, Any]], *, user_input: str, bind_history: bool = False) -> None:
        self.frame = {}
        self.messages = ()
        self.user_input = user_input
        self.counter_baseline = {}
        self.request_messages = list(messages)
        if not self.enabled:
            return
        # Keep the old coordinate view until any legacy boundary has been proven.
        self.legacy_messages = json.loads(stable_json([m for m in messages if m.get("role") != "system"]))
        synchronized = self.journal.sync(messages)
        rebuilt = self.journal.rebuilt
        self.messages = [m for m in synchronized if m.get("role") != "system"]
        state = self.runtime.authority.get_state(tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id)
        self.legacy_anchor = {}
        if state:
            pointer = state.context_rotations.get(self.runtime.handoff_notes.owner)
            if pointer:
                try:
                    self.legacy_anchor = self._rotation_source(pointer)["anchor"]
                    self.resolve_anchor(self.legacy_anchor)
                except (KeyError, TypeError, ValueError, ArtifactIntegrityError, ArtifactStoreUnavailable):
                    pass  # prepare() reports the exact invalid source; never reseal it.
            note = state.handoff_notes.get(self.runtime.handoff_notes.owner)
            if note:
                try:
                    _, saved = self.runtime.handoff_notes.historical(note)
                    self.resolve_anchor(saved["history_anchor"])
                except (KeyError, TypeError, ValueError, ArtifactIntegrityError, ArtifactStoreUnavailable):
                    pass
        self.request_messages = self.journal.sync(synchronized, bind=True) if bind_history else synchronized
        self.journal.rebuilt = rebuilt or self.journal.rebuilt
        # Deep copy: later execution may append, but the note covers this boundary.
        self.messages = json.loads(stable_json([m for m in self.request_messages if m.get("role") != "system"]))
        self.counter_baseline = {
            "completed_steps": len(state.completed_steps) if state else self.runtime.metrics.completed_steps,
            "tool_result_tokens": self.runtime.metrics.cumulative_tool_result_tokens,
            "large_artifact_count": self.runtime.metrics.large_artifact_count,
            "run_id": self.runtime.metrics.counter_run_id,
        }
        try:
            anchor = self.journal.anchor(self.messages)
            history = history_for_anchor(self.messages, anchor)
            retained = _retained_indices(history)
            self.frame = {
                **anchor,
                "retained_indices": retained, "cutoff_time": now_epoch(),
            }
        except (TypeError, ValueError) as exc:
            self.frame = {"error": str(exc)}

    def resolve_anchor(self, anchor, *, messages=None):
        """Migrate an old seal only with byte-exact proof, retained independently of its view."""
        if anchor.get("history_basis") == HISTORY_BASIS:
            if anchor.get("history_stream") != self.journal.stream:
                raise HistoryIntegrityError("foreign_history_stream")
            return dict(anchor)
        fields = ("history_basis", "prefix_count", "prefix_hash")
        original = dict(anchor)
        if all(original.get(k) == self.legacy_anchor.get(k) for k in fields):
            original = {**self.legacy_anchor, **original}
        proof = self.journal.migration(original)
        if proof is None:
            candidates = [self.legacy_messages if messages is None else messages]
            if self.initial_history is not None:
                candidates.append(self.initial_history)
            for candidate in candidates:
                try:
                    history = history_for_anchor(candidate, original)
                    count = original.get("prefix_count")
                    if type(count) is not int or not 0 < count <= len(history):
                        raise HistoryIntegrityError("legacy_boundary_unavailable", sequence=count)
                    prefix = history[:count]
                    verify_anchor(prefix, original)
                    retained = _retained_indices(prefix)
                    if "retained_indices" in original and retained != original["retained_indices"]:
                        raise HistoryIntegrityError("legacy_protected_groups_changed")
                except (KeyError, TypeError, ValueError) as error:
                    last_error = error
                    continue
                proof = {"anchor": original, "messages": prefix}
                break
            if proof is None:
                raise last_error
        if any(proof["anchor"].get(k) != original.get(k) for k in fields):
            raise HistoryIntegrityError("legacy_proof_changed")
        prefix = proof["messages"]
        if len(prefix) != original["prefix_count"]:
            raise HistoryIntegrityError("legacy_proof_incomplete")
        verify_anchor(prefix, original)
        keep = _retained_indices(prefix)
        if "retained_indices" in original and keep != original["retained_indices"]:
            raise HistoryIntegrityError("legacy_protected_groups_changed")
        durable = [prefix[i] for i in durable_indices(prefix)]
        translated = {**boundary(durable, self.journal.stream), "retained_indices": _retained_indices(durable),
                      "cutoff_time": original.get("cutoff_time", 0)}
        verify_anchor(history_for_anchor(self.messages if messages is None else messages, translated), translated)
        self.journal.migration(original, proof=proof)
        return translated

    def prepare(self, state: Any, note_items: Sequence[Any], note_info: Mapping[str, Any],
                mode: MemoryMode) -> dict[str, Any]:
        self.historical_note_items = []
        if not self.enabled or mode == MemoryMode.INLINE or state is None:
            return {}
        notes = self.runtime.handoff_notes
        pointer = state.context_rotations.get(notes.owner, {})
        info: dict[str, Any] = {"epoch": pointer.get("epoch", 0), "status": "ready"}
        if not pointer:
            if self.frame.get("error"):
                info["status"] = "invalid_history"
            return info
        try:
            payload = self._rotation_source(pointer)
            historical = payload.get("note_scope") == "sealed_history"
            if historical and note_info.get("status") == "stale":
                item, current_note = notes.historical(payload["note_pointer"])
                # Recheck the note's own proof, not just the rotation's digest.
                boundary, _, _ = self._covered_boundary(current_note, saved_only=True)
                if self.resolve_anchor(payload["anchor"]) != boundary:
                    raise ValueError("historical rotation exceeds its note boundary")
                self.historical_note_items = [item]
            elif not note_items or note_info.get("status") != "current":
                return {**info, "status": "note_unavailable"}
            else:
                current_note = json.loads(self.runtime._event_source_payload(note_info["source_event_id"]))
            if not current_note["note"]["next_steps"]:
                return {**info, "status": "note_unavailable"}
            if not historical and (payload["contract_hash"] != handoff_contract_hash(state)
                    or payload["user_hash"] != notes.prepared_user_hash
                    or payload["binding_hash"] != notes.binding_hash):
                return {**info, "status": "stale"}
            if note_info["revision"] < pointer["note_revision"]:
                raise ValueError("rotation note revision is unavailable")
            if note_info["revision"] == pointer["note_revision"] and state.handoff_notes[notes.owner] != payload["note_pointer"]:
                raise ValueError("rotation note pointer mismatch")
            active = {**info, "status": "active", "anchor": self.resolve_anchor(payload["anchor"]), "source_event_id": pointer["source_event_id"]}
            if historical:
                active["note_scope"] = "sealed_history"
            # Optional for older immutable rotation events. Missing baselines
            # keep the conservative lifetime policy until the next rotation.
            active["counter_baseline"] = payload.get("counter_baseline", {})
            try:
                projected = project_history(self.messages, active)
            except (KeyError, TypeError, ValueError) as error:
                return {**info, "status": "history_mismatch", "diagnostic": str(error),
                        "source_event_id": pointer["source_event_id"]}
            active.update(note_revision=payload["note_pointer"]["revision"],
                          reason=payload.get("reason", ""), history_preserved=True)
            active["retained_tail_message_count"] = payload.get("retained_tail_message_count", 0)
            active["omitted_message_count"] = len(self.messages) - len(projected)
            active["omitted_message_tokens"] = max(0,
                sum(self.runtime.token_budget.estimate_message(m) for m in self.messages)
                - sum(self.runtime.token_budget.estimate_message(m) for m in projected))
            return active
        except (KeyError, TypeError, ValueError, ArtifactIntegrityError, ArtifactStoreUnavailable) as error:
            self.historical_note_items = []
            return {**info, "status": "invalid_source", "diagnostic": str(error), "source_event_id": pointer.get("source_event_id")}

    def _rotation_source(self, pointer: Mapping[str, Any]) -> dict[str, Any]:
        event = self.runtime.read_event(pointer["source_event_id"])
        payload = json.loads(self.runtime._event_source_payload(event.id))
        if (event.event_type != EventType.CONTEXT_ROTATED or event.content_hash != pointer["content_hash"]
                or payload["schema_version"] != 1 or payload["owner"] != self.runtime.handoff_notes.owner
                or payload["epoch"] != pointer["epoch"]
                or payload["note_pointer"]["revision"] != pointer["note_revision"]
                or payload.get("note_scope") == "sealed_history" and self.runtime.handoff_notes.owner != "primary"
                or payload.get("note_scope") not in {None, "sealed_history"}):
            raise ValueError("rotation pointer/source mismatch")
        return payload

    def recover_saved_note(self) -> dict[str, Any] | None:
        """Recover before model dispatch without pretending to author a fresh note."""
        notes = self.runtime.handoff_notes
        if (not self.enabled or not notes.prepared or notes.owner != "primary"
                or self.runtime.current_mode == MemoryMode.INLINE):
            return None
        if not self.frame or self.frame.get("error"):
            raise ValueError(self.frame.get("error", "complete history required for recovery"))
        contract = notes.prepared_contract_hash
        for attempt in range(1, ROTATION_COMMIT_ATTEMPTS + 1):
            state = self.runtime.authority.get_state(tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id)
            if state is None or handoff_contract_hash(state) != contract:
                raise ValueError("task contract changed during historical recovery")
            _, info = notes.prepare(state, user_input=self.user_input, mode=self.runtime.current_mode,
                                    phase_boundary=False, context_usage_ratio=0)
            if info.get("status") != "stale":
                return None
            pointer = state.handoff_notes[notes.owner]
            _, saved_note = notes.historical(pointer)
            anchor, baseline, tail = self._covered_boundary(saved_note, saved_only=True)
            current = state.context_rotations.get(notes.owner, {})
            if current:
                previous = self._rotation_source(current)
                # A previously recovered boundary remains usable on later turns.
                # Never advance the epoch repeatedly to conceal unsummarized tail.
                if (previous.get("note_scope") == "sealed_history"
                        and pointer["revision"] <= current["note_revision"]):
                    active = self.prepare(state, (), info, self.runtime.current_mode)
                    if active.get("status") != "active":
                        raise ValueError("concurrent historical recovery could not be verified")
                    # Another connection committed after this packet was built.
                    # Rebuild the caller's request against that durable receipt.
                    return {**current, "status": "committed", "applies": "next_model_request",
                            "note_scope": "sealed_history", "history_preserved": True,
                            "already_applied": True, "automatic": True, "recovery_attempts": attempt,
                            "omitted_message_count": active["omitted_message_count"],
                            "retained_tail_message_count": active["retained_tail_message_count"]}
                project_history(self.messages, {"status": "active", "anchor": self.resolve_anchor(previous["anchor"])})
            value = {"expected_epoch": current.get("epoch", 0), "reason": "budget_pressure"}
            try:
                return {**self._commit(value, pointer, anchor, baseline, tail, note_scope="sealed_history"),
                        "automatic": True, "recovery_attempts": attempt}
            except ContextRotationConflict:
                if attempt == ROTATION_COMMIT_ATTEMPTS:
                    raise
        return None

    def rotate(self, value: dict[str, Any]) -> dict[str, Any]:
        value = validate_rotation(value)
        notes = self.runtime.handoff_notes
        if not self.enabled or not notes.prepared or self.runtime.current_mode == MemoryMode.INLINE:
            raise ValueError("rotation requires an enabled PAGED_MEMORY context")
        if not self.frame or self.frame.get("error"):
            raise ValueError(self.frame.get("error", "prepare a complete context before rotating"))
        contract_hash = notes.prepared_contract_hash
        for attempt in range(1, ROTATION_COMMIT_ATTEMPTS + 1):
            state = self.runtime.authority.get_state(tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id)
            if state is None or handoff_contract_hash(state) != contract_hash:
                raise ContextRotationConflict("task contract changed; refresh context before rotating")
            try:
                result = self._commit_or_confirm(value, state)
            except ContextRotationConflict:
                # Only optimistic commit races are retried. Invalid sources,
                # changed requirements and broken history never become success.
                if attempt == ROTATION_COMMIT_ATTEMPTS:
                    raise ContextRotationConflict(
                        "context kept changing during 3 commit attempts; history is preserved; "
                        "refresh the handoff context before retrying") from None
                continue
            return {**result, "requested_epoch": value["expected_epoch"],
                    "requested_note_revision": value["expected_note_revision"],
                    "recovery_attempts": attempt,
                    "reconciled": bool(result.get("already_applied") or attempt > 1
                                       or result["note_revision"] != value["expected_note_revision"])}
        raise AssertionError("unreachable rotation attempt")

    def _commit_or_confirm(self, value: dict[str, Any], state: Any) -> dict[str, Any]:
        notes = self.runtime.handoff_notes
        current = state.context_rotations.get(notes.owner, {})
        epoch = current.get("epoch", 0)
        if epoch not in {value["expected_epoch"], value["expected_epoch"] + 1}:
            raise ValueError("context epoch is outside this handoff; refresh context before retrying")
        pointer = state.handoff_notes.get(notes.owner, {})
        if value["expected_note_revision"] > pointer.get("revision", 0):
            raise ValueError("requested handoff note revision is unavailable; refresh context before rotating")
        note_items, note_info = notes.prepare(state, user_input=self.user_input, mode=self.runtime.current_mode,
                                               phase_boundary=False, context_usage_ratio=0)
        if not note_items or note_info.get("status") != "current":
            raise ValueError("rotation requires a current, verified handoff note")
        active = self.prepare(state, note_items, note_info, self.runtime.current_mode) if current else {}
        # A new turn can legitimately invalidate the previous projection's
        # user/task/worker binding. Its durable source is still verified by
        # prepare(); a fresh current note may seal a new boundary below. Only an
        # active projection can be returned as an already-applied receipt.
        if current and active.get("status") not in {"active", "stale"}:
            raise ValueError(f"previous rotation cannot be verified: {active.get('status', 'invalid_source')}")
        if (current and active.get("status") == "active"
                and current["note_revision"] >= value["expected_note_revision"]
                and (epoch == value["expected_epoch"] + 1 or pointer["revision"] == current["note_revision"])):
            # A durable source, matching history and live note were verified by
            # prepare(); this is a receipt read, never another epoch write.
            return {**current, "status": "committed", "applies": "next_model_request",
                    "already_applied": True, "history_preserved": True,
                    "omitted_message_count": active["omitted_message_count"],
                    "retained_tail_message_count": active["retained_tail_message_count"]}
        if epoch != value["expected_epoch"]:
            raise ValueError("another handoff advanced the context; refresh context before retrying")
        if pointer["revision"] <= current.get("note_revision", 0):
            raise ValueError("save a fresh handoff note before another rotation")
        note_payload = json.loads(self.runtime._event_source_payload(pointer["source_event_id"]))
        anchor, baseline, retained_tail = self._covered_boundary(note_payload)
        if not note_payload["note"]["next_steps"]:
            raise ValueError("handoff note must include next_steps before rotating")
        return self._commit(value, pointer, anchor, baseline, retained_tail)

    def _commit(self, value: Mapping[str, Any], pointer: Mapping[str, Any], anchor: dict[str, Any],
                baseline: dict[str, Any], retained_tail: int, *, note_scope: str | None = None) -> dict[str, Any]:
        notes = self.runtime.handoff_notes
        omitted = anchor["prefix_count"] - len(anchor["retained_indices"])
        if omitted <= 0:
            raise ValueError("no completed optional history to rotate")
        epoch = value["expected_epoch"] + 1
        body = stable_json({
            "schema_version": 1, "epoch": epoch, "owner": notes.owner,
            "contract_hash": notes.prepared_contract_hash, "user_hash": notes.prepared_user_hash,
            "binding_hash": notes.binding_hash, "note_pointer": pointer,
            "anchor": anchor, "reason": value["reason"],
            "counter_baseline": baseline, "retained_tail_message_count": retained_tail,
            **({"note_scope": note_scope} if note_scope else {}),
        })
        event = self.runtime._event(EventType.CONTEXT_ROTATED, content_text=body,
            content_json={"owner": notes.owner, "epoch": epoch}, content_hash=content_hash_text(body),
            token_count=self.runtime.token_budget.estimate_text(body))
        head = {"epoch": epoch, "source_event_id": event.id, "content_hash": event.content_hash,
                "note_revision": pointer["revision"]}
        self.runtime._persist_event(event, state_patch={"_context_rotation_update": {
            "owner": notes.owner, "expected_epoch": value["expected_epoch"], "note_pointer": pointer,
            "contract_hash": notes.prepared_contract_hash, "pointer": head, "history_anchor": anchor,
        }})
        if self.runtime.config.context_utilization_enabled:
            if not retained_tail:
                self.runtime.context_utilization.reset_growth()
        return {**head, "status": "committed", "applies": "next_model_request",
                "omitted_message_count": omitted, "history_preserved": True,
                "retained_tail_message_count": retained_tail,
                **({"note_scope": note_scope} if note_scope else {})}

    def _covered_boundary(self, note: Mapping[str, Any], *, saved_only: bool = False) -> tuple[dict[str, Any], dict[str, Any], int]:
        """Only replace sealed history; later evidence stays verbatim in the request."""
        saved = note.get("history_anchor")
        if not isinstance(saved, dict):
            raise ValueError("handoff note has no verified history boundary; refresh memory_note_update")
        if not saved_only and self.runtime.context_utilization.note_covers_history(note, self.messages):
            return dict(self.frame), dict(self.counter_baseline), 0
        saved = self.resolve_anchor(saved)
        history = history_for_anchor(self.messages, saved)
        count = saved.get("prefix_count")
        if type(count) is not int or count <= 0 or count > len(history):
            raise ValueError("handoff note history boundary is unavailable")
        prefix = history[:count]
        verify_anchor(history, saved)
        anchor = {key: saved[key] for key in ("prefix_count", "prefix_hash", "history_basis", "history_stream", "head_event_id") if key in saved}
        anchor.update(retained_indices=_retained_indices(prefix), cutoff_time=saved.get("cutoff_time", 0))
        # Also checks the complete tail, including IDs reused across the boundary.
        project_history(self.messages, {"status": "active", "anchor": anchor})
        return anchor, dict(note.get("rotation_counter_baseline", {})), len(history) - count

    def filter_items(self, items: Sequence[Any], info: Mapping[str, Any]) -> list[Any]:
        if info.get("status") != "active":
            return list(items)
        kept = []
        for item in items:
            protected = (item.required or item.item_type.startswith(("USER_INPUT", "TOOL_ERROR", "CONSTRAINT", "DECISION"))
                         or bool(_PROTECTED_FACETS.intersection(item.facets)))
            if not protected and item.source_event_id:
                try:
                    event = self.runtime.read_event(item.source_event_id)
                    if event.created_at <= info["anchor"]["cutoff_time"]:
                        continue
                except (KeyError, ValueError, ArtifactIntegrityError, ArtifactStoreUnavailable):
                    pass  # Uncertain provenance never licenses extra omission.
            kept.append(item)
        return kept
