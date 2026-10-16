"""Demand-driven context growth and native handoff admission at high pressure."""
from __future__ import annotations

import json
from threading import Lock

from minicode.runtime_feedback import is_continuation_feedback

from .artifacts import ArtifactIntegrityError, ArtifactStoreUnavailable
from .models import MemoryMode
from .history_journal import HistoryIntegrityError

# These exemptions allow recovery or control of existing work, never new work.
RECOVERY_TOOLS = frozenset({"memory_note_update", "context_rotate", "discover_tools",
    "memory_search", "history_search", "session_history_search", "session_history_read",
    "memory_get", "artifact_read", "runtime_context_read",
    "poll_agents", "wait_agents", "cancel_agent"})
_NOTE_SUFFIX_TOOLS = frozenset({"memory_note_update", "discover_tools"})


class ContextRecoveryRequired(RuntimeError):
    """Stop a bounded recovery loop without deleting authoritative context."""


def growth_reserve(config, metrics, input_limit):
    return max(min(config.context_growth_reserve_tokens, max(256, input_limit // 10)),
               metrics.context_tool_token_peak)


def pressure_status(config, pressure):
    if not config.context_utilization_enabled:
        return {}
    current = pressure["effective_pressure_ratio"]
    forecast = pressure["forecast_pressure_ratio"]
    ratio = max(current, forecast)
    stage = ("protect" if ratio >= config.context_protection_ratio else
             "rotate" if ratio >= config.context_rotation_target_ratio else
             "write_note" if ratio >= config.context_note_warning_ratio else "normal")
    return {"policy": "context-utilization-v1", "stage": stage,
            "current_ratio": current, "forecast_ratio": forecast,
            "input_limit": pressure["input_limit"],
            "pressure_window": pressure.get("pressure_window", pressure["input_limit"]),
            "growth_reserve_tokens": pressure["growth_reserve_tokens"],
            "estimated_input_tokens": pressure.get("admission_input_tokens",
                pressure["projected_message_tokens_estimated"]
                + pressure["required_packet_tokens_estimated"] + pressure["reserved_tool_tokens"]),
            "protection": stage == "protect"}


class ContextUtilization:
    def __init__(self, runtime):
        self.runtime = runtime
        self.batch_reason = ""
        self._peak_lock = Lock()

    @property
    def enabled(self):
        return self.runtime.controls_context and self.runtime.config.context_utilization_enabled

    def note_covers_history(self, payload, messages):
        from .rotation import _retained_indices, history_for_anchor, verify_anchor

        anchor = payload.get("history_anchor", {})
        if not isinstance(anchor, dict):
            return False
        try:
            anchor = self.runtime.context_rotation.resolve_anchor(anchor, messages=messages)
            messages = history_for_anchor(messages, anchor)
            _retained_indices(messages)  # Incomplete or reused call IDs cannot certify a note.
            verify_anchor(messages, anchor)
        except (KeyError, TypeError, ValueError):
            return False
        count = anchor.get("prefix_count")
        calls = {}
        for message in messages[count:]:
            role = message.get("role")
            if role == "assistant_tool_call":
                if message.get("toolName") not in _NOTE_SUFFIX_TOOLS:
                    return False
                calls[message.get("toolUseId")] = message.get("toolName")
            elif role == "tool_result":
                if calls.pop(message.get("toolUseId"), None) != message.get("toolName"):
                    return False
                if message.get("isError"):
                    return False
            elif role not in {"assistant", "assistant_progress"}:
                if not is_continuation_feedback(message):
                    return False
        return not calls

    def validate_rotation_note(self, payload, messages):
        if self.enabled and not self.note_covers_history(payload, messages):
            raise ValueError("refresh memory_note_update after the latest business results before rotating")

    def try_auto_rotate(self, packet):
        if (not self.enabled or packet.debug_metadata.get("context_utilization", {}).get("stage")
                not in {"rotate", "protect"}):
            return False
        notes = packet.debug_metadata.get("handoff_note", {})
        rotation = packet.debug_metadata.get("context_rotation", {})
        if notes.get("status") == "stale" and packet.memory_mode is not MemoryMode.INLINE:
            if (rotation.get("status") == "active" and rotation.get("note_scope") == "sealed_history"
                    and notes.get("revision", 0) <= rotation.get("note_revision", 0)):
                return False
            try:
                return self.runtime.context_rotation.recover_saved_note() or False
            except HistoryIntegrityError:
                raise
            except (KeyError, TypeError, ValueError, ArtifactIntegrityError, ArtifactStoreUnavailable) as error:
                packet.debug_metadata["context_utilization"]["rotation_deferred"] = str(error)
                return False
        if notes.get("status") != "current" or packet.memory_mode is MemoryMode.INLINE:
            return False
        payload = json.loads(self.runtime._event_source_payload(notes["source_event_id"]))
        if not payload["note"]["next_steps"]:
            return False
        state = self.runtime.authority.get_state(tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id)
        previous = state.context_rotations.get(self.runtime.handoff_notes.owner, {})
        if notes["revision"] <= previous.get("note_revision", 0):
            return False
        # rotate() rechecks sources, ownership, contract, epoch and note revision
        # atomically. Any failed validation is a real failed rotation, not success.
        try:
            result = self.runtime.context_rotation.rotate({"expected_epoch": rotation.get("epoch", 0),
                "expected_note_revision": notes["revision"], "reason": "budget_pressure"})
        except HistoryIntegrityError:
            raise
        except ValueError as error:
            packet.debug_metadata["context_utilization"]["rotation_deferred"] = str(error)
            return False
        return {**result, "automatic": True}

    def before_request(self, packet, *, step_id=""):
        self.batch_reason = ""
        if not self.enabled:
            return
        status = packet.debug_metadata["context_utilization"]
        metrics = self.runtime.metrics
        epoch = packet.debug_metadata.get("context_rotation", {}).get("epoch", 0)
        user_hash = self.runtime.handoff_notes.prepared_user_hash
        if (epoch != metrics.context_protection_epoch or not status["protection"]
                or user_hash != metrics.context_protection_user_hash):
            metrics.context_protection_epoch = epoch
            metrics.context_protection_user_hash = user_hash
            metrics.context_protection_steps = 0
            metrics.context_protection_observation_id = ""
        if not status["protection"]:
            return
        if packet.memory_mode is MemoryMode.INLINE:
            raise ContextRecoveryRequired("Context protection requires PAGED_MEMORY; the explicit INLINE override prevents handoff.")
        recovery = self.runtime.context_borrowing
        if recovery.handoff_only:
            # The failed-capacity episode has one bounded request counter.
            # Preparing, filtering or enlarging the same request consumes no
            # additional step, and ordinary warning attempts cannot exhaust it.
            status["recovery_steps"] = recovery.requests
            if recovery.requests >= self.runtime.config.context_recovery_max_steps:
                raise ContextRecoveryRequired("Context handoff did not complete within the recovery budget. "
                    "New work is stopped; task history, sources and asynchronous task state remain stored.")
            return
        observation = f"{self.runtime.run_id}:{step_id or self.runtime.current_step_id}:{epoch}"
        if observation != metrics.context_protection_observation_id:
            metrics.context_protection_steps += 1
            metrics.context_protection_observation_id = observation
        status["recovery_steps"] = metrics.context_protection_steps
        if metrics.context_protection_steps > self.runtime.config.context_recovery_max_steps:
            raise ContextRecoveryRequired("Context handoff did not complete within the recovery budget. "
                "New work is stopped; task history, sources and asynchronous task state remain stored.")

    def begin_tool_batch(self, calls, tools):
        self.batch_reason = ""
        packet = self.runtime.last_packet
        if not self.enabled or packet is None:
            return
        status = packet.debug_metadata.get("context_utilization", {})
        if not status:
            return
        estimate = 0
        for call in calls:
            if call.get("toolName") in RECOVERY_TOOLS:
                continue
            definition = tools.find(call.get("toolName", ""))
            metadata = getattr(definition, "metadata", None)
            result_hint = max(2048, int(getattr(metadata, "max_result_size_chars", 10000)) // 2)
            estimate += max(result_hint, self.runtime.metrics.context_tool_token_peak)
            estimate += self.runtime.token_budget.estimate_text(json.dumps(call.get("input", {}), ensure_ascii=False))
        projected = status["estimated_input_tokens"] + estimate
        packet.debug_metadata["context_tool_forecast"] = {
            "result_and_argument_tokens_estimated": estimate,
            "forecast_input_tokens": projected, "call_count": len(calls),
            "is_hard_upper_bound": False,
        }
        if estimate and projected >= self.runtime.config.context_protection_ratio * status.get("pressure_window", status["input_limit"]):
            self.batch_reason = ("The whole tool batch may exceed the context protection limit. "
                "No new business operation in this batch is admitted. Reduce the batch, or save "
                "memory_note_update and rotate before continuing.")
            if "memory_note_update" not in self.runtime.context_tool_names():
                self.batch_reason = ("The whole tool batch may exceed the context protection limit. "
                    "No new business operation in this batch is admitted. Reduce the batch or use bounded reads. "
                    "This scope cannot record notes or rotate; if essential evidence cannot fit, report the gap.")

    def block_tool(self, call):
        borrowing = self.runtime.context_borrowing
        if borrowing.recovering and call.get("toolName") not in RECOVERY_TOOLS:
            return ("Borrowed context is for handoff recovery only. This business operation was not executed. "
                    "Save memory_note_update and finish a verified rotation; business work requires normal context admission.")
        if not self.enabled or call.get("toolName") in RECOVERY_TOOLS:
            return None
        status = (self.runtime.last_packet.debug_metadata.get("context_utilization", {})
                  if self.runtime.last_packet else {})
        if status.get("protection"):
            if "memory_note_update" not in self.runtime.context_tool_names():
                return ("Context protection: this operation was not executed. Bounded memory reads remain available. "
                        "This scope cannot record notes or rotate; report the evidence gap if it cannot fit.")
            return ("Context protection: this operation was not executed. Save a fresh memory_note_update "
                    "with next_steps; the runtime will rotate the verified context before new work. "
                    "Bounded memory reads and existing-agent poll/wait/cancel remain available.")
        return self.batch_reason or None

    def observe_result(self, call, output):
        if self.enabled and call.get("toolName") not in RECOVERY_TOOLS:
            tokens = self.runtime.token_budget.estimate_text(output)
            with self._peak_lock:
                self.runtime.metrics.context_tool_token_peak = max(self.runtime.metrics.context_tool_token_peak, tokens)

    def reset_growth(self):
        with self._peak_lock:
            self.runtime.metrics.context_tool_token_peak = 0
