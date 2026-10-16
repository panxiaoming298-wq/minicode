from __future__ import annotations

import uuid

from minicode.tooling import ToolDefinition, ToolContext, ToolResult
from minicode.tools._validation import validate_fields


# ---------------------------------------------------------------------------
# UUID Generate
# ---------------------------------------------------------------------------

def _validate_uuid_generate(input_data: dict) -> dict:
    validate_fields(input_data, count=int, version=int)
    count = input_data.get("count", 1)
    version = input_data.get("version", 4)

    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError("count must be an integer between 1 and 100")
    if type(version) is not int or version not in {1, 4}:
        raise ValueError("version must be 1 or 4")
    return {"count": count, "version": version}


def _run_uuid_generate(input_data: dict, context: ToolContext) -> ToolResult:
    # Direct callers get the same contract as ToolRegistry callers.
    try:
        values = _validate_uuid_generate(input_data)
    except (ValueError, TypeError) as error:
        return ToolResult(ok=False, output=str(error), failureKind="invalid_input")
    count, version = values["count"], values["version"]

    if version == 1:
        uuids = [str(uuid.uuid1()) for _ in range(count)]
    else:
        uuids = [str(uuid.uuid4()) for _ in range(count)]
    
    output = "\n".join(uuids) if count > 1 else uuids[0]
    return ToolResult(ok=True, output=output)


uuid_generate_tool = ToolDefinition(
    name="uuid_generate",
    description="Generate UUIDs (version 1 or 4).",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Number of UUIDs to generate (1-100)"},
            "version": {"type": "integer", "enum": [1, 4], "description": "UUID version: 1 (timestamp) or 4 (random)"}
        }
    },
    validator=_validate_uuid_generate,
    run=_run_uuid_generate,
)


# ---------------------------------------------------------------------------
# Text Sort
# ---------------------------------------------------------------------------

def _validate_text_sort(input_data: dict) -> dict:
    validate_fields(input_data, required=("content",), content=str, reverse=bool, numeric=bool, ignore_case=bool)
    content = input_data.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content is required")
    return {
        "content": content,
        "reverse": input_data.get("reverse", False),
        "numeric": input_data.get("numeric", False),
        "ignore_case": input_data.get("ignore_case", False),
    }


def _run_text_sort(input_data: dict, context: ToolContext) -> ToolResult:
    content = input_data["content"]
    reverse = input_data.get("reverse", False)
    numeric = input_data.get("numeric", False)
    ignore_case = input_data.get("ignore_case", False)
    
    lines = content.strip().split("\n")
    
    # Filter empty lines for sorting, keep them at end
    non_empty = [line for line in lines if line.strip()]
    empty = [line for line in lines if not line.strip()]
    
    # Sort
    if numeric:
        try:
            non_empty.sort(key=lambda x: float(x.strip()), reverse=reverse)
        except ValueError:
            return ToolResult(ok=False, output="Cannot sort numerically - invalid numbers", failureKind="invalid_input")
    elif ignore_case:
        non_empty.sort(key=lambda x: x.lower(), reverse=reverse)
    else:
        non_empty.sort(reverse=reverse)
    
    result = non_empty + empty
    return ToolResult(ok=True, output="\n".join(result))


text_sort_tool = ToolDefinition(
    name="text_sort",
    description="Sort text lines (alphabetically or numerically).",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "content": {"type": "string", "description": "Text to sort (one line per item)"},
            "reverse": {"type": "boolean", "description": "Sort in descending order"},
            "numeric": {"type": "boolean", "description": "Sort numerically"},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive sorting"}
        },
        "required": ["content"]
    },
    validator=_validate_text_sort,
    run=_run_text_sort,
)


# ---------------------------------------------------------------------------
# Text Dedupe
# ---------------------------------------------------------------------------

def _validate_text_dedupe(input_data: dict) -> dict:
    validate_fields(input_data, required=("content",), content=str, preserve_order=bool)
    content = input_data.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content is required")
    return {"content": content, "preserve_order": input_data.get("preserve_order", True)}


def _run_text_dedupe(input_data: dict, context: ToolContext) -> ToolResult:
    content = input_data["content"]
    preserve_order = input_data.get("preserve_order", True)
    
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    
    if preserve_order:
        seen = set()
        result = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                result.append(line)
    else:
        result = list(set(lines))
    
    return ToolResult(ok=True, output="\n".join(result))


text_dedupe_tool = ToolDefinition(
    name="text_dedupe",
    description="Remove duplicate lines from text.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "content": {"type": "string", "description": "Text with potential duplicates"},
            "preserve_order": {"type": "boolean", "description": "Keep first occurrence order (default: true)"}
        },
        "required": ["content"]
    },
    validator=_validate_text_dedupe,
    run=_run_text_dedupe,
)


# ---------------------------------------------------------------------------
# Text Join
# ---------------------------------------------------------------------------

def _validate_text_join(input_data: dict) -> dict:
    validate_fields(input_data, required=("items",), items=str, separator=str)
    items = input_data.get("items", "")
    separator = input_data.get("separator", "\n")
    if not isinstance(items, str):
        raise ValueError("items is required")
    return {"items": items, "separator": separator}


def _run_text_join(input_data: dict, context: ToolContext) -> ToolResult:
    items = input_data["items"]
    separator = input_data.get("separator", "\n")
    
    lines = [line.strip() for line in items.strip().split("\n") if line.strip()]
    result = separator.join(lines)
    
    return ToolResult(ok=True, output=result)


text_join_tool = ToolDefinition(
    name="text_join",
    description="Join lines with a custom separator.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {"type": "string", "description": "Lines to join (one per line)"},
            "separator": {"type": "string", "description": "Separator (default: newline)"}
        },
        "required": ["items"]
    },
    validator=_validate_text_join,
    run=_run_text_join,
)


# ---------------------------------------------------------------------------
# Line Count
# ---------------------------------------------------------------------------

def _validate_line_count(input_data: dict) -> dict:
    validate_fields(input_data, required=("content",), content=str)
    return input_data


def _run_line_count(input_data: dict, context: ToolContext) -> ToolResult:
    content = input_data.get("content", "")
    
    lines = content.split("\n")
    total = len(lines)
    non_empty = len([line for line in lines if line.strip()])
    empty = total - non_empty
    
    output = f"""Lines:
  Total: {total}
  Non-empty: {non_empty}
  Empty: {empty}
  Characters: {len(content)}"""
    
    return ToolResult(ok=True, output=output)


line_count_tool = ToolDefinition(
    name="line_count",
    description="Count lines, characters in text.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "content": {"type": "string", "description": "Text to analyze"}
        },
        "required": ["content"]
    },
    validator=_validate_line_count,
    run=_run_line_count,
)


# ---------------------------------------------------------------------------
# Random String
# ---------------------------------------------------------------------------

def _validate_random_string(input_data: dict) -> dict:
    validate_fields(input_data, length=int, chars=str)
    length = input_data.get("length", 16)
    chars = input_data.get("chars", "alphanumeric")
    if not 1 <= length <= 1000:
        raise ValueError("length must be an integer between 1 and 1000")
    if not chars:
        raise ValueError("chars must not be empty")
    return {"length": length, "chars": chars}


def _run_random_string(input_data: dict, context: ToolContext) -> ToolResult:
    length = input_data.get("length", 16)
    chars = input_data.get("chars", "alphanumeric")
    
    if not 1 <= length <= 1000:
        return ToolResult(ok=False, output="length must be between 1 and 1000")
    
    import random
    import string
    
    if chars == "alphanumeric":
        alphabet = string.ascii_letters + string.digits
    elif chars == "alpha":
        alphabet = string.ascii_letters
    elif chars == "numeric":
        alphabet = string.digits
    elif chars == "hex":
        alphabet = string.hexdigits.lower()
    elif chars == "ascii":
        alphabet = string.printable
    else:
        alphabet = chars
    
    result = "".join(random.choice(alphabet) for _ in range(length))
    return ToolResult(ok=True, output=result)


random_string_tool = ToolDefinition(
    name="random_string",
    description="Generate random string.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "length": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "String length (1-1000)"},
            "chars": {"type": "string", "description": "Character set: alphanumeric, alpha, numeric, hex, ascii, or custom string"}
        }
    },
    validator=_validate_random_string,
    run=_run_random_string,
)
