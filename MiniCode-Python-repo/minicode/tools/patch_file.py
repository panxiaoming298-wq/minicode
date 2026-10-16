from __future__ import annotations

from minicode.file_review import apply_reviewed_file_change, load_existing_file
from minicode.tooling import ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path
from minicode.tools._validation import normalize_aliases, validate_contract


def _validate(input_data: dict) -> dict:
    input_data = normalize_aliases(input_data, {})
    if "patch" in input_data:
        if "replacements" in input_data:
            raise ValueError("$.patch: use replacements or the legacy patch shorthand, not both")
        patch = input_data.pop("patch")
        input_data["replacements"] = [{"search": patch, "replace": ""}]
    if isinstance(input_data.get("replacements"), list):
        input_data["replacements"] = [normalize_aliases(item, {"replaceAll": "replace_all"}, path=f"$.replacements[{index}]")
            for index, item in enumerate(input_data["replacements"])]
    validate_contract(input_data, PATCH_FILE_INPUT)
    path = input_data["path"]
    replacements = input_data["replacements"]
    normalized = []
    for replacement in replacements:
        search = replacement.get("search")
        replace = replacement.get("replace")
        replace_all = replacement.get("replace_all", False)
        # Normalize \r\n → \n so search/replace strings always match
        # file content (read_text uses universal newlines).
        search = search.replace("\r\n", "\n")
        replace = replace.replace("\r\n", "\n")
        normalized.append({"search": search, "replace": replace, "replace_all": replace_all})
    return {"path": path, "replacements": normalized}


def _run(input_data: dict, context):
    target = resolve_tool_path(context, input_data["path"], "write")
    content = load_existing_file(target)
    applied: list[str] = []
    for index, replacement in enumerate(input_data["replacements"], start=1):
        if replacement["search"] not in content:
            return ToolResult(ok=False, output=f"Replacement {index} not found in {input_data['path']}")
        replace_all = bool(replacement.get("replace_all", replacement.get("replaceAll", False)))
        if replace_all:
            content = replacement["replace"].join(content.split(replacement["search"]))
            applied.append(f"#{index} replaceAll")
        else:
            content = content.replace(replacement["search"], replacement["replace"], 1)
            applied.append(f"#{index} replaceOnce")
    result = apply_reviewed_file_change(context, input_data["path"], target, content)
    if not result.ok:
        return result
    return ToolResult(
        ok=True,
        output=f"Patched {input_data['path']} with {len(applied)} replacement(s): {', '.join(applied)}",
        contextWriteRevision=result.contextWriteRevision,
    )


PATCH_FILE_INPUT = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "replacements": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "search": {"type": "string", "minLength": 1},
                        "replace": {"type": "string"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["search", "replace"],
                },
            },
        },
        "required": ["path", "replacements"],
    }


patch_file_tool = ToolDefinition(
    name="patch_file",
    description=("Apply multiple exact-text replacements to one file. Each replacements item has search, replace "
                 "and optional replace_all (JSON boolean). Legacy replaceAll is accepted; conflicting aliases are rejected."),
    input_schema=PATCH_FILE_INPUT,
    validator=_validate,
    run=_run,
)
