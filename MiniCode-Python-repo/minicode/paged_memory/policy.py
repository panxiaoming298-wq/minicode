"""Memory mode policy and token-aware context packing."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .config import PagedMemoryConfig
from .models import ContextItem, MemoryMode, ModeSelection, TaskMetrics

_MODE_RANK = {
    MemoryMode.INLINE: 0,
    MemoryMode.WORKING_SET: 1,
    MemoryMode.PAGED_MEMORY: 2,
}


class MemoryModePolicy:
    def __init__(self, config: PagedMemoryConfig) -> None:
        self.config = config

    def choose_mode(
        self,
        metrics: TaskMetrics,
        current_mode: MemoryMode,
        *,
        task_id: str = "default",
        manual_override: MemoryMode | None = None,
        phase_boundary: bool = False,
    ) -> ModeSelection:
        override = manual_override or self.config.manual_mode
        if override is not None:
            metrics.mode_downgrade_streak = 0
            return ModeSelection(
                mode=override,
                reason="task manual override",
                changed=override != current_mode,
            )
        if not self.config.auto_mode:
            target = self.config.default_mode
            return ModeSelection(
                target, "automatic mode selection disabled", target != current_mode
            )

        target, reason = self._candidate(metrics)
        if _MODE_RANK[target] >= _MODE_RANK[current_mode]:
            metrics.mode_downgrade_streak = 0
            return ModeSelection(target, reason, target != current_mode)

        if not phase_boundary:
            return ModeSelection(
                current_mode, "downgrade deferred until a phase boundary", False
            )

        metrics.mode_downgrade_streak += 1
        streak = metrics.mode_downgrade_streak
        if streak < self.config.downgrade_stable_steps:
            return ModeSelection(
                current_mode,
                f"downgrade hysteresis {streak}/{self.config.downgrade_stable_steps}",
                False,
            )
        metrics.mode_downgrade_streak = 0
        return ModeSelection(target, f"stable phase-boundary downgrade: {reason}", True)

    def _candidate(self, metrics: TaskMetrics) -> tuple[MemoryMode, str]:
        if metrics.cross_session_required:
            return MemoryMode.PAGED_MEMORY, "cross-session recovery required"
        if metrics.restart_recovery_required:
            return MemoryMode.PAGED_MEMORY, "restart recovery required"
        if metrics.parallel_subtasks > 1:
            return (
                MemoryMode.PAGED_MEMORY,
                "parallel subtasks require shared paged memory",
            )
        if metrics.context_usage_ratio >= self.config.paged_context_ratio:
            return MemoryMode.PAGED_MEMORY, "paged context-usage threshold reached"
        if metrics.completed_steps >= self.config.paged_step_threshold:
            return MemoryMode.PAGED_MEMORY, "paged step threshold reached"
        if metrics.audit_required:
            return MemoryMode.PAGED_MEMORY, "auditability explicitly required"
        if metrics.context_usage_ratio >= self.config.working_set_context_ratio:
            return MemoryMode.WORKING_SET, "working-set context-usage threshold reached"
        if metrics.completed_steps >= self.config.working_set_step_threshold:
            return MemoryMode.WORKING_SET, "working-set step threshold reached"
        if metrics.large_artifact_count >= self.config.large_artifact_threshold:
            return MemoryMode.WORKING_SET, "large artifact threshold reached"
        if (
            metrics.cumulative_tool_result_tokens
            >= self.config.tool_result_token_threshold
        ):
            return MemoryMode.WORKING_SET, "tool-result token threshold reached"
        if metrics.repeated_dependency_count > 1:
            return MemoryMode.WORKING_SET, "repeated phase dependencies detected"
        return MemoryMode.INLINE, "task remains small"


class TokenBudgetManager:
    """Tokenizer-aware estimator and priority packer.

    Required items are never dropped. If they exceed the nominal budget the
    result explicitly reports the overage so the caller can page raw detail or
    stop before a risky operation.
    """

    def __init__(
        self,
        *,
        model_name: str = "",
        estimate_fn: Callable[[str], int] | None = None,
    ) -> None:
        self.model_name = model_name
        self.estimated = True
        self._estimate_fn = estimate_fn or self._load_tokenizer(model_name)

    def _load_tokenizer(self, model_name: str) -> Callable[[str], int]:
        try:
            import tiktoken

            try:
                encoder = tiktoken.encoding_for_model(model_name)
            except KeyError:
                encoder = tiktoken.get_encoding("cl100k_base")
            self.estimated = False
            return lambda text: len(encoder.encode(text))
        except ImportError:
            return self._conservative_estimate

    @staticmethod
    def _conservative_estimate(text: str) -> int:
        cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
        other = len(text) - cjk
        return max(1, cjk + (other + 2) // 3)

    def estimate_text(self, text: str) -> int:
        return max(1, int(self._estimate_fn(text)))

    def estimate_message(self, message: Mapping[str, Any]) -> int:
        material = json.dumps(
            dict(message), ensure_ascii=False, sort_keys=True, default=str
        )
        return self.estimate_text(material) + 4

    def annotate(self, item: ContextItem) -> ContextItem:
        if item.estimated_tokens <= 0:
            item.estimated_tokens = self.estimate_text(item.text)
        return item

    def pack(
        self,
        items: Sequence[ContextItem],
        *,
        token_budget: int,
    ) -> tuple[list[ContextItem], list[dict[str, Any]], dict[str, Any]]:
        deduped: list[ContextItem] = []
        seen: set[tuple[str, str, str]] = set()
        omitted: list[dict[str, Any]] = []
        for item in items:
            self.annotate(item)
            key = item.dedup_key()
            if key in seen:
                omitted.append({"item_id": item.item_id, "reason": "duplicate"})
                continue
            seen.add(key)
            deduped.append(item)

        required = sorted(
            (item for item in deduped if item.required),
            key=lambda item: (-item.priority, item.estimated_tokens, item.item_id),
        )
        optional = sorted(
            (item for item in deduped if not item.required),
            key=lambda item: (-item.priority, item.estimated_tokens, item.item_id),
        )
        selected = list(required)
        used = sum(item.estimated_tokens for item in required)
        for item in optional:
            if used + item.estimated_tokens <= token_budget:
                selected.append(item)
                used += item.estimated_tokens
            else:
                omitted.append(
                    {
                        "item_id": item.item_id,
                        "reason": "token_budget",
                        "estimated_tokens": item.estimated_tokens,
                    }
                )
        metadata = {
            "required_tokens": sum(item.estimated_tokens for item in required),
            "selected_tokens": used,
            "budget": token_budget,
            "over_budget": used > token_budget,
            "estimated": self.estimated,
        }
        return selected, omitted, metadata

    def recent_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        token_budget: int,
    ) -> list[dict[str, Any]]:
        """Select a recent provider-valid tail without splitting tool pairs."""
        if not messages:
            return []
        selected: list[tuple[int, dict[str, Any]]] = []
        used = 0
        required_tool_ids: set[str] = set()
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            material = dict(message)
            tokens = self.estimate_message(material)
            role = str(material.get("role", ""))
            tool_id = str(material.get("toolUseId", ""))
            must_take = bool(tool_id and tool_id in required_tool_ids)
            if used + tokens > token_budget and selected and not must_take:
                continue
            selected.append((index, material))
            used += tokens
            if role == "tool_result" and tool_id:
                required_tool_ids.add(tool_id)
            elif role == "assistant_tool_call" and tool_id:
                required_tool_ids.discard(tool_id)
            if used >= token_budget and not required_tool_ids:
                break
        selected.reverse()
        if required_tool_ids:
            # A missing call is less safe than retaining a larger tail.
            first_selected = selected[0][0] if selected else len(messages)
            for index in range(first_selected - 1, -1, -1):
                candidate = dict(messages[index])
                if (
                    candidate.get("role") == "assistant_tool_call"
                    and str(candidate.get("toolUseId", "")) in required_tool_ids
                ):
                    selected.insert(0, (index, candidate))
                    required_tool_ids.discard(str(candidate.get("toolUseId")))
                if not required_tool_ids:
                    break
        return [message for _, message in selected]
