"""Message provenance for context selection, never tool or Runtime authority.

Runtime instructions still use the provider's user role. The private marker
survives native history/checkpoints but is not part of the provider wire schema.
Unmarked messages remain human input, even when their text says ``Runtime``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal


RuntimeMessageKind = Literal["control", "feedback"]


def runtime_message(
    content: str, *, kind: RuntimeMessageKind = "control"
) -> dict[str, Any]:
    # Preserve native str subclasses used by request-observation trackers.
    return {"role": "user", "content": content, "_runtimeMessageKind": kind}


def runtime_message_kind(message: Mapping[str, Any]) -> str:
    kind = message.get("_runtimeMessageKind")
    return kind if message.get("role") == "user" and kind in ("control", "feedback") else ""


def is_user_input(message: Mapping[str, Any]) -> bool:
    return message.get("role") == "user" and not runtime_message_kind(message)


def runtime_context_message(content: str, *, kind: str) -> dict[str, Any]:
    """An owned, replaceable state snapshot; never a new human instruction.

    Keep these snapshots after conversation history so changing runtime data
    does not invalidate the provider's stable system/history prefix.
    """
    return {**runtime_message(content), "_runtimeContextKind": kind}


def runtime_context_kind(message: Mapping[str, Any]) -> str:
    kind = message.get("_runtimeContextKind")
    return kind if runtime_message_kind(message) and isinstance(kind, str) else ""


def is_runtime_snapshot(message: Mapping[str, Any]) -> bool:
    """The producer's context type is replaceable state, independent of its name.

    Ordinary human messages and persistent runtime controls do not qualify.
    This is the same contract for existing and future runtime_context_message kinds.
    """
    return runtime_message_kind(message) == "control" and bool(runtime_context_kind(message))
