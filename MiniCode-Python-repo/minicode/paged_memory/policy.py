"""Memory mode policy and token-aware context packing."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from minicode.runtime_messages import is_user_input, runtime_message_kind

from .config import PagedMemoryConfig
from .models import ContextItem, MemoryMode, ModeSelection, TaskMetrics

def initialize_rerank_state(metrics: TaskMetrics, previous_mode: MemoryMode) -> None:
    """Recover old policy state before canonicalizing a legacy mode.

    Old PAGED_MEMORY alone admitted reranking. Never infer permission from the
    canonical mode of an old WORKING_SET or a new local-only paged checkpoint.
    """
    if metrics.rerank_eligible is None:
        metrics.rerank_eligible = previous_mode is MemoryMode.PAGED_MEMORY
        if metrics.rerank_eligible:
            metrics.rerank_downgrade_streak = metrics.mode_downgrade_streak
            metrics.rerank_downgrade_observation_id = metrics.mode_downgrade_observation_id


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
        observation_id: str = "",
        minimum_mode: MemoryMode = MemoryMode.INLINE,
    ) -> ModeSelection:
        initialize_rerank_state(metrics, current_mode)
        override = manual_override or self.config.manual_mode
        if override is not None or not self.config.auto_mode:
            raw_target = override if override is not None else self.config.default_mode
            target = raw_target.canonical
            self._reset_downgrade(metrics, "mode")
            self._reset_downgrade(metrics, "rerank")
            metrics.rerank_eligible = raw_target is MemoryMode.PAGED_MEMORY
            return ModeSelection(
                mode=target,
                reason=("task manual override" if override is not None
                        else "automatic mode selection disabled"),
                changed=target != current_mode,
                rerank_allowed=metrics.rerank_eligible,
                rerank_reason=("legacy WORKING_SET selects local retrieval"
                               if raw_target is MemoryMode.WORKING_SET
                               else "explicit mode configuration"),
            )

        rerank_target, rerank_reason = self._rerank_candidate(metrics)
        paging_target, reason = self._paging_candidate(metrics)
        if rerank_target and not self.config.context_utilization_enabled:
            paging_target, reason = True, rerank_reason
        if minimum_mode.canonical is MemoryMode.PAGED_MEMORY and not paging_target:
            paging_target, reason = True, "active rotation requires a paged context"
        paged, reason = self._stabilize(
            metrics, "mode", current_mode.canonical is MemoryMode.PAGED_MEMORY,
            paging_target, reason, phase_boundary, observation_id,
        )
        target = MemoryMode.PAGED_MEMORY if paged else MemoryMode.INLINE
        if paged:
            metrics.rerank_eligible, rerank_reason = self._stabilize(
                metrics, "rerank", bool(metrics.rerank_eligible), rerank_target,
                rerank_reason, phase_boundary, observation_id,
            )
        else:
            metrics.rerank_eligible = False
            self._reset_downgrade(metrics, "rerank")
            rerank_reason = "inline context uses no supplemental reranking"
        return ModeSelection(
            target, reason, target != current_mode,
            metrics.rerank_eligible, rerank_reason,
        )

    @staticmethod
    def _reset_downgrade(metrics: TaskMetrics, prefix: str) -> None:
        setattr(metrics, f"{prefix}_downgrade_streak", 0)
        setattr(metrics, f"{prefix}_downgrade_observation_id", "")

    def _stabilize(
        self, metrics: TaskMetrics, prefix: str, current: bool, target: bool,
        reason: str, phase_boundary: bool, observation_id: str,
    ) -> tuple[bool, str]:
        if target or not current:
            self._reset_downgrade(metrics, prefix)
            return target, reason

        if not phase_boundary:
            return current, "downgrade deferred until a phase boundary"

        observation_key = f"{prefix}_downgrade_observation_id"
        if observation_id and observation_id == getattr(metrics, observation_key):
            return current, "downgrade observation already counted"
        setattr(metrics, observation_key, observation_id)
        streak = getattr(metrics, f"{prefix}_downgrade_streak") + 1
        setattr(metrics, f"{prefix}_downgrade_streak", streak)
        if streak < self.config.downgrade_stable_steps:
            return current, f"downgrade hysteresis {streak}/{self.config.downgrade_stable_steps}"
        self._reset_downgrade(metrics, prefix)
        return target, f"stable phase-boundary downgrade: {reason}"

    def _rerank_candidate(self, metrics: TaskMetrics) -> tuple[bool, str]:
        # Preserve the previous high-tier admission conditions independently of
        # paging. Admission still requires the global switch and query policy.
        if metrics.cross_session_required:
            return True, "cross-session recovery required"
        if metrics.restart_recovery_required:
            return True, "restart recovery required"
        if metrics.parallel_subtasks > 1:
            return True, "parallel subtasks require shared paged memory"
        if metrics.context_usage_ratio >= self.config.paged_context_ratio:
            return True, "rerank context-usage threshold reached"
        if metrics.completed_steps >= self.config.paged_step_threshold:
            return True, "rerank step threshold reached"
        if metrics.audit_required:
            return True, "auditability explicitly required"
        return False, "automatic rerank admission threshold not reached"

    def _paging_candidate(self, metrics: TaskMetrics) -> tuple[bool, str]:
        if self.config.context_utilization_enabled:
            if (metrics.cross_session_required or metrics.restart_recovery_required
                    or metrics.parallel_subtasks > 1 or metrics.audit_required):
                return True, "task requires paged memory capabilities"
            if max(metrics.context_usage_ratio, metrics.context_forecast_ratio) >= self.config.context_note_warning_ratio:
                return True, "current or forecast context pressure requires handoff capability"
            return False, "live context fits; counters alone do not shorten history"
        if metrics.context_usage_ratio >= self.config.working_set_context_ratio:
            return True, "paging context-usage threshold reached"
        if metrics.completed_steps >= self.config.working_set_step_threshold:
            return True, "paging step threshold reached"
        if metrics.large_artifact_count >= self.config.large_artifact_threshold:
            return True, "large artifact threshold reached"
        if (
            metrics.cumulative_tool_result_tokens
            >= self.config.tool_result_token_threshold
        ):
            return True, "tool-result token threshold reached"
        if metrics.repeated_dependency_count > 1:
            return True, "repeated phase dependencies detected"
        return False, "task remains small"


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
        # Journal references survive checkpoints but never enter provider input.
        material = json.dumps(
            {key: value for key, value in message.items() if key != "_historyEvent"},
            ensure_ascii=False, sort_keys=True, default=str
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
        retain_latest_user: bool = False,
        retain_all_users: bool = False,
    ) -> list[dict[str, Any]]:
        """Pack whole tool exchanges and optionally pin the live user request.

        Overlapping call/result intervals form one atomic group, including
        parallel tool calls. Required groups may exceed the soft tail budget;
        the provider guard enforces the hard limit without losing instructions.
        """
        if not messages:
            return []
        call_indices: dict[str, int] = {}
        interval_ends: dict[int, int] = {}
        for index, message in enumerate(messages):
            tool_id = str(message.get("toolUseId", ""))
            if message.get("role") == "assistant_tool_call" and tool_id:
                call_indices[tool_id] = index
            elif message.get("role") == "tool_result" and tool_id in call_indices:
                start = call_indices.pop(tool_id)
                interval_ends[start] = index
        groups: list[tuple[int, int]] = []
        start = 0
        while start < len(messages):
            end = interval_ends.get(start, start)
            cursor = start
            while cursor <= end:
                end = max(end, interval_ends.get(cursor, cursor))
                cursor += 1
            groups.append((start, end + 1))
            start = end + 1

        latest_user = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if is_user_input(messages[i])
            ),
            -1,
        )
        required_groups = {len(groups) - 1}
        # Persistent native instructions must not depend on search recall.
        # Keep their original role, order and complete tool-exchange group.
        required_groups.update(
            i for i, (start, end) in enumerate(groups)
            if any(runtime_message_kind(message) == "control" for message in messages[start:end])
        )
        if retain_latest_user and latest_user >= 0:
            required_groups.update(
                i for i, (start, end) in enumerate(groups) if start <= latest_user < end
            )
        if retain_all_users:
            required_groups.update(
                i for i, (start, end) in enumerate(groups)
                if any(is_user_input(message) for message in messages[start:end])
            )
        costs = [
            sum(self.estimate_message(message) for message in messages[start:end])
            for start, end in groups
        ]
        chosen = set(required_groups)
        used = sum(costs[i] for i in chosen)
        for index in range(len(groups) - 1, -1, -1):
            if index not in chosen and used + costs[index] <= token_budget:
                chosen.add(index)
                used += costs[index]
        return [
            dict(message)
            for index in sorted(chosen)
            for message in messages[groups[index][0] : groups[index][1]]
        ]
