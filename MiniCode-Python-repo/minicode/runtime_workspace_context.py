"""Ground file lookup in the active Worker handle, without changing authority.

An assignment is prose, not a mount table. This context view is rebuilt after
workspace binding (including recovery). It neither remaps paths nor exposes a
file inventory that the Worker's tools might not permit reading.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from minicode.runtime_protocol import ToolEffect
from minicode.runtime_messages import runtime_message
from minicode.tooling import ToolContext, ToolRegistry, ToolResult


def _annotate_read_failure(result: ToolResult, diagnostic: str) -> ToolResult:
    if (result.ok or result.awaitUser or result.waitKind or result.waitReason or result.backgroundTask
            or result.timedOut or result.ambiguousSideEffect or result.runtimeYield
            or result.runtimeStopReason or result.inputRejected):
        return result
    return replace(result, output=(result.output or "") + "\n\n" + diagnostic)


def workspace_message(workspace: Path) -> dict[str, str]:
    return runtime_message(
        "Runtime workspace binding: " + json.dumps({"tool_cwd": str(workspace.resolve()), "path_base": "."}, ensure_ascii=False)
        + ". Use workspace-relative paths in file tools and consultation assignments. "
        "Paths in assignments or earlier messages do not change this binding. "
        "Do not invent a host root. A failed lookup is not file evidence; locate the source before analyzing it."
    )


def bind_workspace_context(messages: list[dict], tools: ToolRegistry, workspace: Path) -> tuple[list[dict], ToolRegistry]:
    """Add location and failed-read feedback, retaining the original tool guards.

    Call only for primary-led Workers, after the owned workspace is selected.
    The original definition still performs the read, including custom guards,
    validation, permission checks, and its exact path interpretation.
    """
    root = workspace.resolve()
    message = workspace_message(root)
    # An owned continuation retains the same handle. A relocated handle gets
    # a fresh binding; identical metadata need not be charged repeatedly.
    located = messages if message in messages else [*messages, message]
    definition = tools.find("read_file")
    if definition is None or definition.effect is not ToolEffect.READ_ONLY:
        return located, tools
    discovery = [name for name in ("list_files", "file_tree", "grep_files") if (
        tools.find(name) is not None and tools.find(name).effect is ToolEffect.READ_ONLY
    )]
    guidance = (
        "Use the available discovery tools to locate a workspace-relative source: " + ", ".join(discovery) + "."
        if discovery else
        "Use a workspace-relative source already established by the task or a successful read; "
        "no file-discovery tool is available in this Worker."
    )
    diagnostic = (
        "Runtime read recovery: " + json.dumps({"tool_cwd": str(root), "path_base": "."}, ensure_ascii=False)
        + ". The read failed; no source evidence was obtained. " + guidance
        + " Do not repeat an unchanged failed path or invent a replacement. "
        "Running tests does not locate a missing source. If still unavailable, report that limitation. "
        "Tool permissions still apply; no path was remapped."
    )

    def read(args: dict, context: ToolContext) -> ToolResult:
        if Path(context.cwd).resolve() != root:
            return ToolResult(ok=False, output="WorkspaceBindingMismatch: this read tool belongs to another Worker workspace.")
        try:
            result = definition.run(args, context)
        except Exception as error:
            # Preserve exception type, traceback and the registry's existing
            # failure handling. No retry, prefix stripping or permission change.
            error.add_note(diagnostic)
            raise
        return _annotate_read_failure(result, diagnostic)

    wrapped = replace(definition, run=read)
    # Retain tool order, metadata, validator and non-owning registry semantics.
    return located, ToolRegistry([wrapped if tool is definition else tool for tool in tools.list()],
                                skills=tools.get_skills(), mcp_servers=tools.get_mcp_servers(),
                                _unavailable_reasons=tools._unavailable_reasons)
