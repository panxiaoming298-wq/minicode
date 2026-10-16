from __future__ import annotations

from dataclasses import asdict
import json

from minicode.skills import discover_skills
from minicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    name = input_data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if set(input_data) - {"name", "arguments", "branch"}:
        raise ValueError("Unsupported load_skill argument; caller identity is host-owned")
    arguments, branch = input_data.get("arguments", ""), input_data.get("branch", "")
    if not isinstance(arguments, str) or not isinstance(branch, str):
        raise ValueError("arguments and branch must be text")
    return {"name": name.strip(), "arguments": arguments, "branch": branch}


def create_load_skill_tool(cwd: str) -> ToolDefinition:
    def _run(input_data: dict, context) -> ToolResult:
        from minicode.skill_runtime import SkillRuntime
        from minicode.tooling import ToolContext
        context = context or ToolContext(cwd=cwd)
        runtime = context._runtime if context._runtime is not None else {}
        manager = runtime.get("_skillRuntime")
        if manager is None:
            manager = SkillRuntime(cwd=cwd, runtime=runtime, session=context.session, permissions=context.permissions)
        try:
            item = manager.invoke(input_data["name"], arguments=input_data.get("arguments", ""),
                branch=input_data.get("branch", ""), caller="user" if context.skill_user_invocation else "model")
            return ToolResult(ok=True, output=manager.receipt(item))
        except (OSError, ValueError, RuntimeError) as error:
            return ToolResult(ok=False, output="Skill invocation refused: " + str(error), failureKind="invalid_input")

    return ToolDefinition(
        name="load_skill",
        description="Select a local Skill workflow. Make load_skill the LAST call in this response, then end the response: complete required rules enter the NEXT model request. Its receipt is not the rules. Only after reading those rules, propose dependent tools in a new response; same-batch calls cannot use them. If a listed branch matches the requested output, include it in this call to select its required references. Arguments are literal text.",
        input_schema={"type": "object", "properties": {"name": {"type": "string"},
            "arguments": {"type": "string"}, "branch": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
        validator=_validate,
        run=_run,
    )


def create_discover_skills_tool(cwd: str) -> ToolDefinition:
    def validate(value):
        if not isinstance(value, dict) or set(value) - {"query", "offset", "limit"}:
            raise ValueError("Expected query, offset and limit")
        query, offset, limit = value.get("query", ""), value.get("offset", 0), value.get("limit", 8)
        if not isinstance(query, str) or len(query) > 1024 or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("Invalid Skill discovery parameters")
        return {"query": query, "offset": offset, "limit": limit}

    def run(value, context):
        from Package.AgentRuntime.Src.Boot.Skills import createApp
        from minicode.context_manager import estimate_tokens
        found = createApp().discover([asdict(s) for s in discover_skills(cwd)], measure=estimate_tokens, **value)
        return ToolResult(ok=True, output=json.dumps(found, ensure_ascii=False))
    return ToolDefinition(name="discover_skills", description="Search Skill names/descriptions locally. Empty query pages all model-callable Skills. Results are candidates; call load_skill to activate.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 32}}, "additionalProperties": False},
        validator=validate, run=run)
