"""Conversation-owned context preferences, separate from worker history saves."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from minicode.context_manager import configured_context_cap, get_model_context_window, resolve_context_limit
from minicode.context_usage import _capacity
from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id, atomic_write_json
from minicode.store_lock import store_lock

DEFAULT_CONTEXT_TOKENS = 400_000
DEFAULT_REASONING_EFFORT = "high"


class ConversationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # null follows the model/server limit. K uses decimal tokens throughout the UI.
    max_tokens: int | None = Field(default=DEFAULT_CONTEXT_TOKENS, alias="maxTokens",
                                   strict=True, ge=8192, le=9_007_199_254_740_991)
    reasoning_effort: Literal["low", "high", "max"] | None = Field(default=None, alias="reasoningEffort")

    def request(self) -> dict[str, Any]:
        # Older context-only clients remain valid. An absent effort is a fixed
        # conversation default, never a live reference to the global setting.
        return self.model_dump(by_alias=True, exclude={"reasoning_effort"} if self.reasoning_effort is None else set())

    def merge(self, previous: "ConversationContext") -> "ConversationContext":
        return previous.model_copy(update={name: getattr(self, name) for name in self.model_fields_set})

    def apply(self, runtime: dict[str, Any]) -> dict[str, Any]:
        # Remember the original administrative ceiling so raising a conversation
        # limit in a queued task does not remain stuck at the previous selection.
        server_cap = runtime.get("_conversationBaseContextCap", configured_context_cap(runtime))
        caps = [cap for cap in (server_cap, self.max_tokens) if cap]
        cap = min(caps) if caps else 0
        configured = {**runtime, "_conversationBaseContextCap": server_cap,
                      "contextWindow": cap, "maxContextTokens": cap}
        from minicode.runtime_budget import apply_reasoning_budget
        effort = self.reasoning_effort or DEFAULT_REASONING_EFFORT
        configured, decision = apply_reasoning_budget(
            model=str(runtime.get("model") or "default"), runtime=configured, requested=effort)
        if decision.applied is not None:
            configured["runtimeReasoningEffort"] = effort
            configured["_provider_params"] = {**configured.get("_provider_params", {}), "reasoning_effort": decision.applied}
            # Explicit conversation controls must not be treated as an automatic
            # provider budget choice that may silently lower the effort.
            configured.pop("_runtimeReasoningEffort", None)
        return configured

    def view(self, runtime: dict[str, Any]) -> dict[str, Any]:
        from minicode.runtime_budget import apply_reasoning_budget
        model = str(runtime.get("model") or "default")
        model_window = get_model_context_window(model)
        capacity = _capacity(model, self.apply(runtime), runtime.get("maxOutputTokens"), planning=True)
        base_cap = runtime.get("_conversationBaseContextCap", configured_context_cap(runtime))
        _, reasoning = apply_reasoning_budget(model=model, runtime=runtime,
            requested=self.reasoning_effort or DEFAULT_REASONING_EFFORT)
        return {**self.request(), "reasoningEffort": self.reasoning_effort or DEFAULT_REASONING_EFFORT,
                "reasoningEffortSupported": reasoning.applied is not None,
                "model": model,
                "modelMaxTokens": model_window.context_window,
                "modelCapacityKnown": model_window.known,
                "availableMaxTokens": resolve_context_limit(model, {"contextWindow": base_cap}),
                "effectiveMaxTokens": capacity["context_window"],
                "reservedTokens": capacity["reserved_tokens"],
                "outputReserveTokens": capacity["output_reserve_tokens"],
                "safetyReserveTokens": capacity["safety_reserve_tokens"],
                "effectiveInputTokens": capacity["effective_input"],
                "budgetPolicy": public_budget_policy(capacity["budget_policy"]),
                "capacityError": capacity["capacity_error"]}

    def validate_capacity(self, runtime: dict[str, Any]) -> None:
        view = self.view(runtime)
        if view["capacityError"]:
            raise ValueError(f"上下文上限需大于输出及安全预留（{view['reservedTokens']:,} tokens），请调高上限或降低单次输出设置。")


def public_budget_policy(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    return {"version": value["version"], "outputMode": value["output_mode"],
            "outputLimit": value["output_limit"], "inputLimit": value["input_limit"],
            "pressureWindow": value["pressure_window"], "noteAtTokens": value["note_at_tokens"],
            "compactAtTokens": value["compact_at_tokens"], "protectAtTokens": value["protect_at_tokens"]}


def _path(session_id: str, sessions_dir: Path | None = None) -> tuple[Path, Path]:
    if not _valid_session_id(session_id):
        raise ValueError("无效的对话编号。")
    if sessions_dir is None:
        from minicode.session import SESSIONS_DIR
        sessions_dir = SESSIONS_DIR
    root = Path(sessions_dir)
    path = root / session_id / "context-settings.json"
    if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
        raise OSError("上下文设置路径不安全。")
    return root, path


def load_context_settings(session_id: str, sessions_dir: Path | None = None) -> ConversationContext:
    _, path = _path(session_id, sessions_dir)
    if not path.exists():
        return ConversationContext()
    if path.stat().st_size > 4096:
        raise ValueError("上下文设置损坏，请重新保存。")
    return ConversationContext.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_context_settings(session_id: str, settings: ConversationContext,
                          sessions_dir: Path | None = None) -> None:
    root, path = _path(session_id, sessions_dir)
    with store_lock(root / ".store.lock"):
        _path(session_id, root)
        atomic_write_json(path, settings.request())
