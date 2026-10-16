"""Identify native continuation prompts without exempting evidence or user input."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from minicode.runtime_messages import runtime_message, runtime_message_kind

NUDGE_CONTINUE = (
    "Continue immediately from your <progress> update with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete. "
    "Prefer taking the next concrete action over explaining what you plan to do."
)
NUDGE_AFTER_TOOL_RESULT = (
    "You have received tool results. Review them briefly, then take the next "
    "concrete action: call another tool, edit code, or give an explicit <final> "
    "answer only if the task is truly complete. Do not restate what you just saw."
)
NUDGE_AFTER_EMPTY_RESPONSE = (
    "Your last response was empty. This often happens after tool errors or when "
    "the model is uncertain. Pick the most likely next action and try it — you can "
    "adjust based on results. Call a tool, edit code, or give <final> if done."
)
NUDGE_AFTER_EMPTY_NO_TOOLS = (
    "Your last response was empty but you have not used any tools yet. Start by "
    "inspecting the relevant files (read_file, grep_files, list_files) to understand "
    "the codebase before making changes."
)
RESUME_AFTER_PAUSE = (
    "Resume from the previous pause. Continue with the next concrete tool call, "
    "code change, or <final> answer."
)
RESUME_AFTER_MAX_TOKENS = (
    "Your previous response was cut short by the token limit. Resume immediately "
    "with the next concrete action — pick up where you left off."
)
VERIFY_FOLLOWUP = (
    "You are in verification mode. Use the current evidence to run the most "
    "relevant validation step, summarize the result, and only then finalize "
    "or explain the remaining blocker."
)
EXPLORE_FOLLOWUP = (
    "You are still in exploration mode. Inspect the most relevant files, "
    "tests, or symbols first so the next step is grounded in evidence."
)
EMPTY_VERIFY_FOLLOWUP = (
    "Your last response was empty during verification mode. Resume with a "
    "single concrete validation step or state the exact blocker."
)
EMPTY_WIDENING_FOLLOWUP = (
    "Your last response was empty after the current line of attack stalled. "
    "Resume with one wider search step or explicitly compare the next two options."
)
RESUME_AFTER_TRANSPORT = (
    "The previous response was interrupted or could not be parsed completely; "
    "it is not a completed result. Resume from the existing tool receipts without replaying "
    "completed operations. Produce a concise, complete, self-contained response within "
    "the existing budget; do not just append text to the interrupted response."
)

_LEGACY_CONTINUATIONS = frozenset({NUDGE_CONTINUE, NUDGE_AFTER_TOOL_RESULT,
    NUDGE_AFTER_EMPTY_RESPONSE, NUDGE_AFTER_EMPTY_NO_TOOLS, RESUME_AFTER_PAUSE,
    RESUME_AFTER_MAX_TOKENS, VERIFY_FOLLOWUP, EXPLORE_FOLLOWUP,
    EMPTY_VERIFY_FOLLOWUP, EMPTY_WIDENING_FOLLOWUP, RESUME_AFTER_TRANSPORT})


def runtime_feedback_message(content: str, *, kind: Literal["continuation"] | None = None) -> dict[str, Any]:
    message = runtime_message(content, kind="feedback")
    if kind is not None:
        if kind != "continuation":
            raise ValueError("unknown runtime feedback kind")
        message["_runtimeFeedbackKind"] = kind
    return message


def is_continuation_feedback(message: Mapping[str, Any]) -> bool:
    if runtime_message_kind(message) != "feedback":
        return False
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return False
    if "_runtimeFeedbackKind" in message:
        return message["_runtimeFeedbackKind"] == "continuation"
    # Older immutable histories have only the feedback provenance marker. Exact
    # known prompts are compatible; never infer from prefixes or arbitrary text.
    return content in _LEGACY_CONTINUATIONS
