from __future__ import annotations

import json

from minicode.permissions import pause_for_user_input
from minicode.tooling import ToolDefinition, ToolResult


def _question(input_data: dict) -> dict:
    if not isinstance(input_data, dict):
        raise ValueError("each question must be an object")
    question = input_data.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > 12_000:
        raise ValueError("question must contain 1 to 12000 characters")
    options = input_data.get("options", [])
    if (not isinstance(options, list) or len(options) > 8
            or any(not isinstance(option, str) or not option.strip() or len(option) > 2_000
                   for option in options)):
        raise ValueError("options must contain at most 8 non-empty strings, each up to 2000 characters")
    options = list(dict.fromkeys(option.strip() for option in options))
    recommended = input_data.get("recommendedOption")
    if recommended is not None:
        if not isinstance(recommended, str) or recommended.strip() not in options:
            raise ValueError("recommendedOption must exactly match one of the suggested options")
        recommended = recommended.strip()
    return {"question": question.strip(), **({"options": options} if options else {}),
            **({"recommendedOption": recommended} if recommended is not None else {})}


def _validate(input_data: dict) -> dict:
    if "questions" not in input_data:
        return _question(input_data)
    if any(key in input_data for key in ("question", "options", "recommendedOption")):
        raise ValueError("use either question/options or questions, not both")
    questions = input_data["questions"]
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise ValueError("questions must contain 1 to 3 independent questions")
    normalized = [_question(question) for question in questions]
    if len({question["question"] for question in normalized}) != len(normalized):
        raise ValueError("questions must not repeat the same question")
    return {"questions": normalized}


def _run(input_data: dict, context) -> ToolResult:
    prompt = (context._runtime or {}).get("_userInputPrompt")
    if callable(prompt):
        with pause_for_user_input():
            answer = prompt(input_data, context)
        if answer is None:
            return ToolResult(ok=False, output="User input was canceled; no answer was supplied.",
                              runtimeStopReason="canceled")
        if "questions" in input_data and (not isinstance(answer, list) or len(answer) != len(input_data["questions"])
                or any(not isinstance(value, str) or not value.strip() for value in answer)):
            raise ValueError("interactive host must return one answer for every question")
        result = {"answers": [{"question": question["question"], "answer": value}
                              for question, value in zip(input_data["questions"], answer)]} if "questions" in input_data else {
            "question": input_data["question"], "answer": answer}
        return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False))
    questions = input_data.get("questions", [input_data])
    text = "\n\n".join((f"{index + 1}. " if len(questions) > 1 else "") + question["question"]
                       + ("\n" + "\n".join(f"- {option}" for option in question["options"]) if question.get("options") else "")
                       for index, question in enumerate(questions))
    return ToolResult(
        ok=True,
        output=text,
        awaitUser=True,
        waitKind="input",
        waitReason="user input is required",
    )


ask_user_tool = ToolDefinition(
    name="ask_user",
    description="Ask only for missing decisions that affect the task. Collect up to 3 already-known, independent questions in one questions array instead of interrupting repeatedly. Ask a dependent follow-up only after its prerequisite answer. Use question/options for a single question. When offering options, choose one recommended option based on the task context, put it first, and set recommendedOption to its exact text. The recommendation is guidance, not the user's answer. Wait for all requested answers before continuing; never invent an answer or repeat an answered question.",
    input_schema={"type": "object", "properties": {
        "question": {"type": "string", "maxLength": 12000},
        "options": {"type": "array", "items": {"type": "string", "maxLength": 2000},
                    "maxItems": 8, "description": "Optional suggested answers; the user can always enter their own."},
        "recommendedOption": {"type": "string", "maxLength": 2000,
                              "description": "The exact text of the one option you recommend; place it first in options."},
        "questions": {"type": "array", "minItems": 1, "maxItems": 3,
                      "description": "Group independent questions that can already be answered. Do not include questions whose wording or relevance depends on another answer.",
                      "items": {"type": "object", "properties": {
                          "question": {"type": "string", "maxLength": 12000},
                          "options": {"type": "array", "maxItems": 8,
                                      "items": {"type": "string", "maxLength": 2000}},
                          "recommendedOption": {"type": "string", "maxLength": 2000,
                                                "description": "The exact text of the one recommended option; place it first in options."},
                      }, "required": ["question"]}},
    }, "oneOf": [{"required": ["question"], "not": {"required": ["questions"]}},
                 {"required": ["questions"], "not": {"required": ["question"]}}]},
    validator=_validate,
    run=_run,
)
