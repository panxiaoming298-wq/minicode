"""Browser-only projection; model history is not a user-authored transcript."""
from collections import Counter
from copy import deepcopy

DISPLAY_SUMMARY_VERSION = 4


def is_question_message(message: dict) -> bool:
    return message.get("role") == "run_event" and (message.get("event") or {}).get("kind") in {
        "question_requested", "question_resolved",
    }


def summarize_messages(messages: list[dict], prompt: str) -> dict:
    """Keep the primary delivery separate from the expandable work transcript."""
    primary = lambda message: message.get("nodeId") in {None, "", "root", "primary"}
    answer_index = next((index for index in range(len(messages) - 1, -1, -1)
                         if messages[index].get("role") == "assistant"
                         and messages[index].get("phase") == "final_answer"
                         and primary(messages[index])), None)
    if answer_index is None:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not primary(message):
                continue
            if message.get("role") == "assistant_tool_call":
                break
            if message.get("role") == "assistant" and message.get("phase") != "commentary":
                answer_index = index
                break
    visible = []
    # Keep the latest primary check snapshot in the lightweight turn summary.
    # Older assessments remain in the original process transcript.
    check_report = next((message for message in reversed(messages) if message.get("role") == "tool_result"
                         and message.get("toolName") == "record_task_checks" and message.get("isError") is False
                         and primary(message)), None)
    # Preserve the original positions when the lightweight summary is merged
    # with process pages. Questions must never jump below later tool calls.
    for index, message in enumerate(messages):
        if (is_question_message(message) or index == answer_index or message is check_report
                or message.get("role") == "tool_image"
                or message.get("role") == "user"):
            visible.append({**deepcopy(message), "displayIndex": index})
    if not any(message.get("role") == "user" for message in visible):
        visible.insert(0, {"role": "user", "content": prompt, "displayIndex": 0})
    process_count = sum(message.get("role") not in {"user", "tool_image"} and index != answer_index
                        and not is_question_message(message)
                        for index, message in enumerate(messages))
    return {"version": DISPLAY_SUMMARY_VERSION, "displayMessages": visible, "answerIndex": answer_index,
            "processMessageCount": process_count,
            "toolCallCount": sum(message.get("role") == "assistant_tool_call" for message in messages),
            "processAvailable": process_count > 0}


def project_messages(messages: list[dict], prompts: list[str], *, runtime: bool = False) -> list[dict]:
    """Use the independent input ledger, never Runtime prompt-prefix heuristics.

    This fallback is for in-flight / pre-snapshot conversations. Completed Web
    turns use their separately captured display messages. Legacy conversations
    without either a ledger or Runtime trace retain their old user messages.
    """
    remaining = Counter(prompt for prompt in prompts if isinstance(prompt, str))
    result = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant", "assistant_tool_call", "tool_result", "tool_image", "run_event"}:
            continue
        if role == "user" and (prompts or runtime) and not message.get("_userInputId"):
            content = message.get("content")
            if not isinstance(content, str) or remaining[content] == 0:
                continue
            remaining[content] -= 1
        result.append(deepcopy(message))
    return result
