"""Bounded model-visible tool schemas over the Worker's existing capability view.

Discovery changes presentation only. The executor keeps its complete scoped
registry and all original validators, permission checks and Runtime guards.
Selections are activation-local; recovery rebuilds the catalog from live tools.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import replace
from difflib import get_close_matches
from typing import Any

from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.runtime_profiles import parallel_candidates_enabled
from minicode.tooling import ToolContext, ToolDefinition, ToolMetadata, ToolRegistry, ToolResult


DISCOVER_TOOLS = "discover_tools"
MAX_SELECTED = 8
PAGE_SIZE = 12
# Discovery accepts capability vocabulary as well as exact tool names. These
# aliases only rank the already-permitted catalog; they never grant a tool.
_SEARCH_ALIASES = {
    "internet": ("internet", "web", "network", "http"),
    "network": ("network", "web", "http"),
    "browser": ("browser", "web", "browse"),
    "联网": ("network", "web", "http"),
    "网络": ("network", "web", "http"),
    "互联网": ("internet", "web", "network", "http"),
    "网页": ("web", "http"),
    "联网搜索": ("web_search", "web search"),
    "网络搜索": ("web_search", "web search"),
    "网页搜索": ("web_search", "web search"),
    "搜索": ("search", "grep", "find"),
    "文件": ("file", "filesystem"),
    "目录": ("directory", "file_tree", "list_files"),
    "读取文件": ("read_file",),
    "编辑文件": ("edit_file", "patch_file"),
    "写入文件": ("write_file",),
    "命令": ("command", "shell", "terminal"),
    "执行命令": ("run_command",),
    "终端": ("terminal", "shell", "command"),
    "测试": ("test", "check"),
    "版本控制": ("git", "version control"),
    "时间": ("time", "clock", "timestamp"),
    "当前时间": ("current_time",),
    "记忆": ("memory", "note"),
    "技能": ("skill",),
    "任务": ("task", "agent"),
    "压缩": ("compress", "archive", "zip", "tar", "gzip"),
    "解压": ("decompress", "extract"),
    "编码": ("encode", "encoding"),
    "解码": ("decode", "decoding"),
    "哈希": ("hash", "hmac", "checksum"),
    "正则": ("regex", "regular expression"),
}
# Entry/closure capabilities stay explicit, including B-arm consultation.
# Every name is intersected with the current scoped registry.
CORE_TOOLS = frozenset({
    "read_file", "display_image", "view_image", "record_task_checks", "list_files", "grep_files", "write_file", "edit_file", "run_command", "ask_user",
    "start_service", "service_status", "service_logs", "stop_service",
    "load_skill", "discover_skills",
    "submit_candidate", "submit_builder_candidate", "submit_consultation", "task", "consult_agents",
    "acknowledge_advisory_failure",
    # The human inbox blocks ordinary execution until this native response.
    # Its full schema must therefore be visible without a discovery call.
    "resolve_user_input",
    # Stored tool results and Runtime dependencies have different readers.
    # Keep both schemas visible whenever the scoped registry provides them.
    "runtime_context_read", "runtime_artifact_read", "artifact_read",
    # Handoff must remain callable when protection blocks ordinary work.
    # Deferring its schemas costs another model turn at the fullest boundary.
    "memory_note_update", "context_rotate",
    # Native async controls must carry their exact schema on the first request.
    # The executor registry already intersects these with live role/feature policy.
    "spawn_agents", "poll_agents", "wait_agents", "cancel_agent", "send_agent_message",
    "resume_agent", "reassign_agent", "team_control",
    "interrupt_agent", "dismiss_agent", "list_agents", "ack_agent_results", "run_agent_check",
    "request_verification",
})

# The writable-parallel action. These are shown to the model whenever the Runtime
# really bound them, because a capability that only appears after a discovery call
# is not a capability the mode can be reached through -- the preflight would report
# a parallel arm whose tools exist but were never visible. Intersecting them with
# the scoped registry keeps the rule intact: a name the Runtime did not authorise
# is still not shown.
PARALLEL_CANDIDATE_TOOLS = frozenset({
    "spawn_builders", "poll_task_results", "wait_dependencies",
    "accept_builder_patch", "cancel_task", "select_builder_candidate",
})


def core_tools(runtime: Mapping[str, Any] | None = None) -> frozenset[str]:
    """The always-visible tool set for one runtime configuration."""
    if parallel_candidates_enabled(runtime):
        return CORE_TOOLS | PARALLEL_CANDIDATE_TOOLS
    return CORE_TOOLS


def _validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"names", "query", "offset", "version"}:
        raise ValueError("Use names to select tools, or query/offset/version to browse the catalog.")
    if "names" in value:
        if set(value) - {"names"}:
            raise ValueError(
                "Selection and browsing are mutually exclusive: pass names alone to select tools; "
                'omit names and use {"query":"keyword"} to search, or {} to browse. '
                "Do not combine names with query, offset or version."
            )
        names = value["names"]
        if (not isinstance(names, list) or len(names) > MAX_SELECTED
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or len(set(names)) != len(names)):
            raise ValueError("names must be an array of up to eight unique, nonblank exact tool names from the current catalog.")
        return {"names": list(names)}
    query, offset, version = value.get("query", ""), value.get("offset", 0), value.get("version", "")
    if (not isinstance(query, str) or len(query) > 300 or type(offset) is not int or offset < 0
            or not isinstance(version, str) or (offset and not version)):
        raise ValueError("query must be at most 300 characters; positive offsets require the previous catalog version.")
    return {"query": query, "offset": offset, "version": version}


def _search_tools(tools: list[ToolDefinition], query: str) -> tuple[list[ToolDefinition], str]:
    exact = [tool for tool in tools if tool.name.casefold() == query.strip().casefold()]
    if exact:
        return exact, "exact_name"
    terms = list(dict.fromkeys(re.findall(r"\w+", query.casefold())))
    if not terms:
        return tools, "all"
    ranked = []
    for tool in tools:
        tags = " ".join(tool.metadata.tags) if tool.metadata else ""
        text = f"{tool.name} {tool.description} {tags}".casefold()
        score = sum(any(alias in text for alias in _SEARCH_ALIASES.get(term, (term,))) for term in terms)
        if score:
            ranked.append((score, tool))
    strict = [tool for score, tool in ranked if score == len(terms)]
    if strict:
        return strict, "all_terms"
    # A broad multi-capability query must not turn a partial vocabulary match
    # into false evidence that a tool is absent. Label the fallback explicitly.
    ranked.sort(key=lambda item: (-item[0], item[1].name))
    return [tool for _, tool in ranked], "partial_terms" if ranked else "none"


class ToolDiscovery:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._registry = ToolRegistry([])
        self._selected: frozenset[str] = frozenset()
        self._automatic: frozenset[str] = frozenset({DISCOVER_TOOLS})

    def require_schemas(self, names) -> None:
        """Make a rejected, permitted tool visible on the next request only.

        This alters presentation, never registry permissions or the rejected call.
        Multiple missing schemas share the same existing eight optional slots.
        """
        with self._lock:
            # Schemas already supplied by the current display policy consume
            # no optional slots. Keep explicit selections separate so policy
            # changes do not erase the model's selection unnecessarily.
            available = {tool.name for tool in self._registry.list()} - self._automatic
            requested = list(dict.fromkeys(name for name in names if name in available))[:MAX_SELECTED]
            retained = self._selected - self._automatic - set(requested)
            self._selected = (self._selected & self._automatic) | frozenset((requested + sorted(retained))[:MAX_SELECTED])

    def tool(self) -> ToolDefinition:
        return ToolDefinition(
            name=DISCOVER_TOOLS,
            description=(
                "Discover permitted tools for commands, tests, Git, web, skills, memory or other operations. "
                "Choose exactly one mode. BROWSE: omit names and pass query to search, or {} to list tools. "
                "SELECT: pass names ONLY (up to 8, replaces prior selection) to attach their FULL schemas to following model requests; "
                "then call those tools normally. This does not execute them. "
                "Discovery cannot grant permissions; do not repeatedly select a restricted tool. "
                "Never combine names with query, offset or version. "
                "Use exact names from the current catalog or deferred list; do not invent tool names from action names. "
                "Selections reset on Worker recovery; the deferred names below reflect the current view. "
                "Search exact names or capability keywords; broad queries may return labeled partial matches. "
                "catalog_names is the complete permitted name inventory; tools contains paginated descriptions. "
                "A zero-result query or an unfinished page is not evidence that a capability is missing. "
                "Follow next_call while has_more is true."
            ),
            input_schema={"type": "object", "properties": {
                "names": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_SELECTED, "uniqueItems": True,
                          "description": "SELECT mode only. Exact names returned by the current catalog; [] clears selection. "
                                         "When present, this must be the only field. Omit it entirely when browsing."},
                "query": {"type": "string", "maxLength": 300,
                          "description": "BROWSE mode only; omit names. Search exact names or capability keywords. "
                                         "All-keyword matches are preferred; partial matches are labeled. Empty query lists all tools."},
                "offset": {"type": "integer", "minimum": 0,
                           "description": "BROWSE mode only; omit names. Use next_offset from the previous page and its version."},
                "version": {"type": "string",
                            "description": "BROWSE mode only; omit names. Catalog version returned by the previous page; required for offset > 0."},
            }, "additionalProperties": False},
            validator=_validate, run=self._run,
            # Serial: selecting a schema set changes activation-local display
            # state. It is read-only with respect to the governed workspace.
            metadata=ToolMetadata(name=DISCOVER_TOOLS, description="Select tool schemas",
                                  effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.SAFE),
        )

    @staticmethod
    def _version(tools: list[ToolDefinition]) -> str:
        payload = [{"name": tool.name, "description": tool.description, "schema": tool.input_schema,
                    "effect": tool.effect.value, "network": tool.requires_network,
                    "tags": tool.metadata.tags if tool.metadata else []} for tool in tools]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def model_tools(self, registry: ToolRegistry, *, provider_runtime: Mapping[str, Any] | None = None,
                    parallel_closure=None, required_names=()) -> ToolRegistry:
        """Refresh from live capabilities, never recover a cached capability grant."""
        with self._lock:
            self._registry = registry
            available = sorted((tool for tool in registry.list() if tool.name != DISCOVER_TOOLS), key=lambda tool: tool.name)
            self._selected &= {tool.name for tool in available}
            core = core_tools(provider_runtime)
            if callable(parallel_closure):
                try:
                    if parallel_closure() is True:
                        core = core - PARALLEL_CANDIDATE_TOOLS
                except Exception:  # An uncertain observation keeps all controls visible.
                    pass
            automatic = core | {DISCOVER_TOOLS}
            params = {**((provider_runtime or {}).get("_openrouter_params") or {}),
                      **((provider_runtime or {}).get("_provider_params") or {})}
            choice = params.get("tool_choice")
            if isinstance(choice, Mapping):
                function = choice.get("function")
                name = (function.get("name") if isinstance(function, Mapping) else None) or choice.get("name")
                if isinstance(name, str):
                    automatic = automatic | {name}
            self._automatic = frozenset(automatic & {tool.name for tool in registry.list()})
            # A previously automatic selection may become optional after a
            # runtime/closure change. Reapply the same bound in the new view.
            self._selected = ((self._selected & self._automatic)
                              | frozenset(sorted(self._selected - self._automatic)[:MAX_SELECTED]))
            shown = self._automatic | self._selected
            # Active Skills already declare the tools needed by their workflow.
            # Share the eight optional-schema slots with explicit selections;
            # the current registry remains the sole capability authority.
            remaining = max(0, MAX_SELECTED - len(self._selected - self._automatic))
            required = sorted(set(required_names) & {tool.name for tool in available} - shown)
            shown |= frozenset(required[:remaining])
            deferred = [tool.name for tool in available if tool.name not in shown]
            description = registry.find(DISCOVER_TOOLS)
            if description is None or description.run != self._run:
                raise ValueError("native discovery tool was removed or replaced")
            catalog = replace(description, description=description.description
                              + (" All current Builder patches are applied and checked; parallel controls remain discoverable."
                                 if core != core_tools(provider_runtime) else "")
                              + f" Deferred names (complete, {len(deferred)}): " + json.dumps(deferred, ensure_ascii=False))
            return ToolRegistry([catalog if tool.name == DISCOVER_TOOLS else tool
                                 for tool in registry.list() if tool.name in shown or tool.name == DISCOVER_TOOLS],
                                skills=registry.get_skills(), mcp_servers=registry.get_mcp_servers(),
                                _unavailable_reasons=registry._unavailable_reasons)

    def _run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        with self._lock:
            scoped = self._registry.filtered_by_network(context._runtime)
            if context.allowed_effects is not None:
                scoped = scoped.filtered_by_effects(context.allowed_effects)
            tools = sorted((tool for tool in scoped.list() if tool.name != DISCOVER_TOOLS), key=lambda tool: tool.name)
            version = self._version(tools)
            if "names" in arguments:
                selected = frozenset(arguments["names"])
                permitted = {tool.name for tool in tools}
                unavailable = sorted(selected - permitted)
                if unavailable:
                    # Suggestions come only from this live scoped catalog. They
                    # are hints, never aliases, grants or a partial selection.
                    suggestions = {}
                    for name in unavailable:
                        prefixes = sorted((candidate for candidate in permitted if name.startswith(candidate + "_")),
                                          key=lambda candidate: (-len(candidate), candidate))
                        similar = get_close_matches(name, sorted(permitted), n=3, cutoff=0.6)
                        suggestions[name] = list(dict.fromkeys(prefixes + similar))[:3]
                    reasons = {name: scoped.unavailable_reason(name) for name in unavailable}
                    restricted = any(reason in {"effect_restricted", "network_disabled"} for reason in reasons.values())
                    next_call = ({"toolName": "request_verification", "input": {}}
                                 if "run_command" in unavailable and "request_verification" in permitted
                                 else {"toolName": DISCOVER_TOOLS, "input": {}})
                    return ToolResult(ok=False, output=json.dumps({
                        "error": "Unknown or unavailable tool names. Selection was not changed.",
                        "unavailable_names": unavailable,
                        "available_requested_names": sorted(selected & permitted),
                        "unavailable_reasons": reasons,
                        "retryable": False,
                        "suggestions": suggestions,
                        "next_call": next_call,
                        "hint": ("This capability is restricted by the current effect/network policy. Discovery cannot grant it. "
                                 "Do not retry the unchanged selection or use another tool to bypass the restriction. "
                                 "For missing execution evidence, use request_verification only when available; otherwise "
                                 "return the exact unverified criterion and required check to the primary in your report. "
                                 "Unrun checks must remain unverified. " if restricted else "")
                                + "Browse the current permitted catalog, then select exact names with names ONLY. "
                                "Suggestions are not aliases; select a tool and read its schema before calling it.",
                    }, ensure_ascii=False), failureKind="unavailable_tool")
                self._selected = selected
                # Schemas travel once, in the next request's real tools field.
                return ToolResult(ok=True, output=json.dumps({"selected": sorted(selected), "version": version,
                    "schemas": "attached to following model requests", "tools_executed": False}, ensure_ascii=False))
            if arguments["version"] and arguments["version"] != version:
                return ToolResult(ok=False, output="Catalog changed; restart browsing at offset 0.", failureKind="invalid_input")
            catalog_names = [tool.name for tool in tools]
            tools, match_mode = _search_tools(tools, arguments["query"])
            offset = arguments["offset"]
            if offset and offset >= len(tools):
                return ToolResult(ok=False, output=json.dumps({
                    "error": "Offset is outside the matching catalog; restart at offset 0.",
                    "next_call": {"toolName": DISCOVER_TOOLS, "input": {"query": arguments["query"]}},
                }, ensure_ascii=False), failureKind="invalid_input")
            page = tools[offset:offset + PAGE_SIZE]
            next_offset = offset + len(page) if offset + len(page) < len(tools) else None
            hint = ("catalog_names lists every permitted tool; tools contains only this page's matching descriptions. "
                    "Select exact names with names ONLY, then call the selected tools using their schemas.")
            if next_offset is not None:
                hint += " More descriptions remain; follow next_call before claiming the listing is complete."
            if match_mode == "partial_terms":
                hint += " No tool matched every query word; these are ranked partial matches."
            elif match_mode == "none":
                hint += " No keyword match does not prove a capability is missing; check catalog_names or simplify the query."
            return ToolResult(ok=True, output=json.dumps({"version": version, "total": len(tools),
                "catalog_total": len(catalog_names), "catalog_names": catalog_names,
                "match_mode": match_mode, "offset": offset, "returned": len(page),
                "tools": [{"name": tool.name, "description": tool.description[:200],
                           "description_truncated": len(tool.description) > 200} for tool in page],
                "has_more": next_offset is not None, "next_offset": next_offset,
                "next_call": ({"toolName": DISCOVER_TOOLS, "input": {
                    "query": arguments["query"], "offset": next_offset, "version": version,
                }} if next_offset is not None else None), "hint": hint,
            }, ensure_ascii=False))
