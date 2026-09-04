from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, List, Mapping, Protocol

from minicode.logging_config import get_logger, log_tool_execution
from minicode.runtime_protocol import ReplayPolicy, ToolEffect, operation_digest


# ---------------------------------------------------------------------------
# Constants for smart truncation
# ---------------------------------------------------------------------------

# Default max output size (characters) — per tool type
_DEFAULT_MAX_OUTPUT = 30_000       # ~8K tokens, safe for context
_LARGE_OUTPUT_THRESHOLD = 50_000   # Trigger smart truncation above this

# Tool-specific output limits (characters)
_TOOL_OUTPUT_LIMITS: dict[str, int] = {
    "read_file": 40_000,
    "grep_files": 20_000,
    "run_command": 30_000,
    "web_fetch": 20_000,
    "web_search": 15_000,
    "list_files": 15_000,
    "file_tree": 15_000,
    "code_review": 20_000,
    "diff_viewer": 20_000,
    "test_runner": 25_000,
}


def _smart_truncate_output(output: str, tool_name: str, max_chars: int | None = None) -> str:
    """Intelligently truncate large tool output to preserve context window.
    
    Strategy:
    1. If output fits within limit, return as-is
    2. For file reads: keep head + tail (beginning and end of file)
    3. For command output: keep head + tail + error lines
    4. For grep/search: keep first N matches + summary
    5. Generic: keep head + tail with line count summary
    """
    if not output:
        return output
    
    limit = max_chars or _TOOL_OUTPUT_LIMITS.get(tool_name, _DEFAULT_MAX_OUTPUT)
    
    if len(output) <= limit:
        return output
    
    lines = output.split("\n")
    total_lines = len(lines)
    total_chars = len(output)
    
    # Calculate how many lines we can keep (rough estimate)
    avg_line_len = total_chars / max(1, total_lines)
    max_lines = int(limit / max(40, avg_line_len))
    
    if tool_name == "read_file":
        # Keep head + tail — most important for understanding file structure
        head_lines = max(1, int(max_lines * 0.6))
        tail_lines = max(1, max_lines - head_lines)
        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        omitted = total_lines - head_lines - tail_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...\n\n"
            f"{tail}"
        )
    
    if tool_name == "run_command":
        # Keep head + error lines + tail
        head_lines = max(1, int(max_lines * 0.4))
        tail_lines = max(1, int(max_lines * 0.4))
        
        # Also extract error/warning lines
        error_pattern = re.compile(r'(?i)(error|fail|exception|traceback|warning)', re.IGNORECASE)
        error_lines = [
            (i, line) for i, line in enumerate(lines)
            if error_pattern.search(line) and head_lines <= i < total_lines - tail_lines
        ]
        error_text = ""
        if error_lines:
            error_text = "\n\n[Key errors/warnings from omitted section:]\n" + "\n".join(
                f"L{i+1}: {line[:200]}" for i, line in error_lines[:20]
            )
        
        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        omitted = total_lines - head_lines - tail_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...{error_text}\n\n"
            f"{tail}"
        )
    
    if tool_name in ("grep_files", "web_search"):
        # Keep first N matches + summary
        head = "\n".join(lines[:max_lines])
        omitted = total_lines - max_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} more lines omitted (output too large: {total_chars:,} chars, {total_lines} total lines)] ..."
        )
    
    # Generic: head + tail
    head_lines = max(1, int(max_lines * 0.5))
    tail_lines = max(1, max_lines - head_lines)
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    omitted = total_lines - head_lines - tail_lines
    return (
        f"{head}\n"
        f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...\n\n"
        f"{tail}"
    )


# ---------------------------------------------------------------------------
# Tool metadata (inspired by Claude Code's Tool type)
# ---------------------------------------------------------------------------

class ToolCapability(str, Enum):
    """Tool capability flags."""
    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"
    CONCURRENCY_SAFE = "concurrency_safe"
    REQUIRES_PERMISSION = "requires_permission"


@dataclass
class ToolMetadata:
    """Tool metadata for classification and discovery.
    
    Inspired by Claude Code's Tool type definition.
    """
    name: str
    description: str
    capabilities: set[ToolCapability] = field(default_factory=set)
    input_schema: dict[str, Any] = field(default_factory=dict)
    is_enabled: bool = True
    max_result_size_chars: int = 10_000
    tags: list[str] = field(default_factory=list)
    effect: ToolEffect | None = None
    replay_policy: ReplayPolicy | None = None
    
    @property
    def is_read_only(self) -> bool:
        """Check if tool is read-only."""
        return ToolCapability.READ_ONLY in self.capabilities
    
    @property
    def is_destructive(self) -> bool:
        """Check if tool can modify/delete data."""
        return ToolCapability.DESTRUCTIVE in self.capabilities
    
    @property
    def is_concurrency_safe(self) -> bool:
        """Check if tool is safe for concurrent execution."""
        return ToolCapability.CONCURRENCY_SAFE in self.capabilities


# ---------------------------------------------------------------------------
# Tool Protocol (inspired by Claude Code's Tool interface)
# ---------------------------------------------------------------------------

class Tool(Protocol):
    """Tool protocol defining a complete tool lifecycle.
    
    Inspired by Claude Code's Tool type which includes:
    - call: Execution logic
    - description: Dynamic description generation
    - validate_input: Input validation
    - check_permissions: Permission checking
    - Metadata: is_read_only, is_destructive, etc.
    """
    
    @property
    def name(self) -> str: ...
    
    @property
    def description_template(self) -> str: ...
    
    def get_description(self, args: dict[str, Any], options: dict[str, Any] | None = None) -> str: ...
    def validate_input(self, args: dict[str, Any]) -> tuple[bool, str]: ...
    def check_permissions(self, args: dict[str, Any], context: ToolContext) -> tuple[bool, str]: ...
    def call(
        self,
        args: dict[str, Any],
        context: ToolContext,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> ToolResult: ...
    def is_enabled(self) -> bool: ...
    def is_read_only(self, args: dict[str, Any]) -> bool: ...
    def is_destructive(self, args: dict[str, Any]) -> bool: ...


@dataclass(slots=True)
class BackgroundTaskResult:
    taskId: str
    type: str
    command: str
    pid: int
    status: str
    startedAt: int


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: str
    backgroundTask: BackgroundTaskResult | None = None
    awaitUser: bool = False


@dataclass(slots=True)
class ToolContext:
    cwd: str
    permissions: Any | None = None
    session: Any | None = None
    _runtime: dict | None = None
    runtime_context: Any | None = None
    allowed_effects: frozenset[ToolEffect] | None = None
    cancellation_event: Any | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    idempotency_key: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    base_revision: str | None = None
    raw_result_observer: Callable[[str, ToolResult], ToolResult | None] | None = None


Validator = Callable[[Any], Any]
Runner = Callable[[Any, ToolContext], ToolResult]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    validator: Validator
    run: Runner
    metadata: ToolMetadata | None = None

    @property
    def effect(self) -> ToolEffect:
        """Return the explicit Runtime effect for policy and worker isolation.

        Unknown tools fail closed as privileged.  The legacy read-only name
        set is retained only as a compatibility input to this central mapping;
        Runtime policy never infers write safety from concurrency safety.
        """
        if self.metadata and self.metadata.effect is not None:
            return self.metadata.effect
        if self.metadata and ToolCapability.READ_ONLY in self.metadata.capabilities:
            return ToolEffect.READ_ONLY
        return _EXPLICIT_TOOL_EFFECTS.get(self.name, ToolEffect.PRIVILEGED)

    @property
    def replay_policy(self) -> ReplayPolicy:
        if self.metadata and self.metadata.replay_policy is not None:
            return self.metadata.replay_policy
        return _EXPLICIT_REPLAY_POLICIES.get(
            self.name,
            ReplayPolicy.SAFE if self.effect is ToolEffect.READ_ONLY else ReplayPolicy.NEVER_AUTO_RETRY,
        )
    
    @property
    def is_read_only(self) -> bool:
        """Check if this tool is read-only (safe for concurrent execution)."""
        return self.effect is ToolEffect.READ_ONLY
    
    @property
    def is_concurrency_safe(self) -> bool:
        """Check if this tool is safe for concurrent execution."""
        if self.metadata:
            return self.metadata.is_concurrency_safe or self.metadata.is_read_only
        return self.is_read_only


# Heuristic: tool names that are known to be read-only
_READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_files", "grep_files", "file_tree",
    "find_symbols", "find_references", "get_ast_info",
    "code_review", "diff_viewer",
    "web_fetch", "web_search",
    "ask_user", "todo_write",
})


# The product tool surface is deliberately classified here in one auditable
# table.  Any extension/MCP tool not listed and without metadata is privileged,
# so an Explorer can never receive it by accident.
_EXPLICIT_TOOL_EFFECTS: dict[str, ToolEffect] = {
    # Workspace reads / deterministic local transforms
    "read_file": ToolEffect.READ_ONLY,
    "list_files": ToolEffect.READ_ONLY,
    "grep_files": ToolEffect.READ_ONLY,
    "file_tree": ToolEffect.READ_ONLY,
    "find_symbols": ToolEffect.READ_ONLY,
    "find_references": ToolEffect.READ_ONLY,
    "get_ast_info": ToolEffect.READ_ONLY,
    "code_review": ToolEffect.READ_ONLY,
    "diff_viewer": ToolEffect.READ_ONLY,
    "json_format": ToolEffect.READ_ONLY,
    "json_parse": ToolEffect.READ_ONLY,
    "regex_test": ToolEffect.READ_ONLY,
    "base64_encode": ToolEffect.READ_ONLY,
    "base64_decode": ToolEffect.READ_ONLY,
    "url_encode": ToolEffect.READ_ONLY,
    "url_decode": ToolEffect.READ_ONLY,
    "current_time": ToolEffect.READ_ONLY,
    "timestamp": ToolEffect.READ_ONLY,
    "hash": ToolEffect.READ_ONLY,
    "hmac": ToolEffect.READ_ONLY,
    "csv_parse": ToolEffect.READ_ONLY,
    "csv_create": ToolEffect.READ_ONLY,
    "uuid_generate": ToolEffect.READ_ONLY,
    "text_sort": ToolEffect.READ_ONLY,
    "text_dedupe": ToolEffect.READ_ONLY,
    "text_join": ToolEffect.READ_ONLY,
    "line_count": ToolEffect.READ_ONLY,
    "random_string": ToolEffect.READ_ONLY,
    # Workspace mutation
    "write_file": ToolEffect.WORKSPACE_WRITE,
    "edit_file": ToolEffect.WORKSPACE_WRITE,
    "patch_file": ToolEffect.WORKSPACE_WRITE,
    "batch_copy": ToolEffect.WORKSPACE_WRITE,
    "batch_move": ToolEffect.WORKSPACE_WRITE,
    "batch_delete": ToolEffect.WORKSPACE_WRITE,
    "regex_replace": ToolEffect.WORKSPACE_WRITE,
    "gzip_compress": ToolEffect.WORKSPACE_WRITE,
    "gzip_decompress": ToolEffect.WORKSPACE_WRITE,
    "tar_create": ToolEffect.WORKSPACE_WRITE,
    "tar_extract": ToolEffect.WORKSPACE_WRITE,
    "zip_create": ToolEffect.WORKSPACE_WRITE,
    "zip_extract": ToolEffect.WORKSPACE_WRITE,
    # Network reads still cross the workspace trust boundary and therefore
    # require an explicit side-effect-capable scope in Runtime V2.
    "web_fetch": ToolEffect.EXTERNAL_SIDE_EFFECT,
    "web_search": ToolEffect.EXTERNAL_SIDE_EFFECT,
    "http_request": ToolEffect.EXTERNAL_SIDE_EFFECT,
    # Shell, git, user interaction, state mutation, skills and delegation.
    "run_command": ToolEffect.PRIVILEGED,
    "test_runner": ToolEffect.PRIVILEGED,
    "git": ToolEffect.PRIVILEGED,
    "ask_user": ToolEffect.PRIVILEGED,
    "todo_write": ToolEffect.PRIVILEGED,
    "load_skill": ToolEffect.PRIVILEGED,
    "task": ToolEffect.PRIVILEGED,
}


_EXPLICIT_REPLAY_POLICIES: dict[str, ReplayPolicy] = {
    name: ReplayPolicy.SAFE
    for name, effect in _EXPLICIT_TOOL_EFFECTS.items()
    if effect is ToolEffect.READ_ONLY
}
_EXPLICIT_REPLAY_POLICIES.update(
    {
        "write_file": ReplayPolicy.VERIFY_BEFORE_RETRY,
        "edit_file": ReplayPolicy.VERIFY_BEFORE_RETRY,
        "patch_file": ReplayPolicy.VERIFY_BEFORE_RETRY,
        "batch_copy": ReplayPolicy.VERIFY_BEFORE_RETRY,
        "batch_move": ReplayPolicy.VERIFY_BEFORE_RETRY,
        "batch_delete": ReplayPolicy.NEVER_AUTO_RETRY,
        "web_fetch": ReplayPolicy.SAFE,
        "web_search": ReplayPolicy.SAFE,
        "http_request": ReplayPolicy.NEVER_AUTO_RETRY,
        "run_command": ReplayPolicy.NEVER_AUTO_RETRY,
        "git": ReplayPolicy.NEVER_AUTO_RETRY,
        "task": ReplayPolicy.IDEMPOTENT_WITH_KEY,
    }
)


class ToolRegistry:
    def __init__(
        self,
        tools: list[ToolDefinition],
        skills: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        disposer: Callable[[], Any] | None = None,
    ) -> None:
        self._tools = tools
        self._skills = skills or []
        self._mcp_servers = mcp_servers or []
        self._disposer = disposer
        # 工具查找缓存 - O(1) 查找代替 O(n) 遍历
        self._tool_index: dict[str, ToolDefinition] = {t.name: t for t in tools}

    def list(self) -> list[ToolDefinition]:
        return list(self._tools)

    def list_all(self) -> List[str]:
        return list(self._tool_index.keys())

    def filtered_by_effects(self, allowed_effects: set[ToolEffect] | frozenset[ToolEffect]) -> "ToolRegistry":
        """Create a capability view without transferring disposer ownership."""
        allowed = frozenset(allowed_effects)
        return ToolRegistry(
            [tool for tool in self._tools if tool.effect in allowed],
            skills=self._skills,
            mcp_servers=self._mcp_servers,
        )

    def filtered_without_names(self, excluded_names: set[str] | frozenset[str]) -> "ToolRegistry":
        """Create a non-owning view that hides selected tools from the model and executor."""

        excluded = frozenset(excluded_names)
        return ToolRegistry(
            [tool for tool in self._tools if tool.name not in excluded],
            skills=self._skills,
            mcp_servers=self._mcp_servers,
        )

    def with_additional_tools(self, additional: list[ToolDefinition]) -> "ToolRegistry":
        """Create a non-owning per-turn view with validated unique tool names."""
        existing = set(self._tool_index)
        duplicates = [tool.name for tool in additional if tool.name in existing]
        if duplicates:
            raise ValueError(
                "cannot add duplicate tools: " + ", ".join(sorted(set(duplicates)))
            )
        return ToolRegistry(
            [*self._tools, *additional],
            skills=self._skills,
            mcp_servers=self._mcp_servers,
        )

    def effect_manifest(self) -> dict[str, dict[str, str]]:
        return {
            tool.name: {
                "effect": tool.effect.value,
                "replay_policy": tool.replay_policy.value,
            }
            for tool in self._tools
        }

    def get_skills(self) -> List[dict[str, Any]]:
        return list(self._skills)

    def get_mcp_servers(self) -> List[dict[str, Any]]:
        return list(self._mcp_servers)

    def find(self, name: str) -> ToolDefinition | None:
        # O(1) lookup via cached index
        return self._tool_index.get(name)

    def execute(self, tool_name: str, input_data: Any, context: ToolContext | None) -> ToolResult:
        """Execute a tool with comprehensive error protection.
        
        The global exception safety net catches ALL exceptions (except
        KeyboardInterrupt/SystemExit) and converts them to error ToolResults,
        preventing a single tool crash from cascading into a full session failure.
        
        Protection layers:
        1. Tool not found → error result
        2. Validation error → error result with input details
        3. Execution error → error result with traceback excerpt
        4. Output too large → smart truncation
        5. Unexpected errors → error result (never propagates to caller)
        """
        tool = self.find(tool_name)
        if tool is None:
            return ToolResult(ok=False, output=f"Unknown tool: {tool_name}")

        # Preserve the historical direct-registry API used by integrations and
        # tests while still giving Runtime policy a concrete context object.
        context = context or ToolContext(cwd=".")

        if context.cancellation_event is not None and context.cancellation_event.is_set():
            return ToolResult(ok=False, output="Canceled: parent Runtime canceled this tool call")
        if context.allowed_effects is not None and tool.effect not in context.allowed_effects:
            return ToolResult(
                ok=False,
                output=(
                    "PolicyDenied: tool effect "
                    f"{tool.effect.value!r} is outside this Worker's capability scope"
                ),
            )

        _logger = get_logger("tools")
        _start = time.monotonic()
        try:
            # Phase 1: Input validation (with error context)
            try:
                parsed = tool.validator(input_data)
            except (ValueError, TypeError, KeyError) as ve:
                log_tool_execution(
                    tool_name, False, (time.monotonic() - _start) * 1000,
                    error=f"input validation: {ve}",
                )
                return ToolResult(
                    ok=False,
                    output=f"Input validation error in {tool_name}: {ve}\n"
                           f"Input was: {str(input_data)[:200]}"
                )

            # Bind interactive one-shot permission decisions to the complete
            # Runtime operation.  Legacy/direct callers keep their historical
            # manager; governed Workers use an operation-scoped adapter so a
            # changed argument/cwd/base cannot inherit an old allow-once.
            if (
                context.permissions is not None
                and context.run_id
                and context.node_id
                and context.attempt_id
                and context.tool_call_id
                and callable(getattr(context.permissions, "fork_for_operation", None))
            ):
                arguments = (
                    dict(parsed)
                    if isinstance(parsed, Mapping)
                    else {"value": parsed}
                )
                environment_scope = {
                    "tool": tool_name,
                    "effect": tool.effect.value,
                    "allowed_effects": sorted(
                        effect.value for effect in (context.allowed_effects or ())
                    ),
                    "idempotency_key": context.idempotency_key or "",
                }
                digest = operation_digest(
                    run_id=context.run_id,
                    node_id=context.node_id,
                    attempt_id=context.attempt_id,
                    tool_call_id=context.tool_call_id,
                    cwd=context.cwd,
                    arguments=arguments,
                    environment_scope=environment_scope,
                    base_revision=context.base_revision or "unknown",
                )
                context = replace(
                    context,
                    permissions=context.permissions.fork_for_operation(digest),
                )

            # Phase 2: Execution (with crash protection)
            result = tool.run(parsed, context)

            # Phase 3: Output sanitization
            if result.output is None:
                result.output = ""

            # Lossless persistence hooks observe the exact tool receipt before
            # the legacy prompt-size truncator is allowed to alter it.
            if context.raw_result_observer is not None:
                observed = context.raw_result_observer(tool_name, result)
                if observed is not None:
                    result = observed

            # Smart truncation for large outputs
            if result.output and len(result.output) > _LARGE_OUTPUT_THRESHOLD:
                result.output = _smart_truncate_output(result.output, tool_name)

            log_tool_execution(
                tool_name, bool(result.ok), (time.monotonic() - _start) * 1000,
                error=None if result.ok else (result.output or "")[:200],
            )
            return result

        except (KeyboardInterrupt, SystemExit):
            # These should always propagate upward
            raise
        except Exception as error:  # noqa: BLE001
            # Global safety net: convert any unhandled exception to error result
            # This prevents a single buggy tool from crashing the entire session
            duration_ms = (time.monotonic() - _start) * 1000
            # Persist the crash to the log file (searchable) while still
            # returning a ToolResult to the caller (issue #5).
            _logger.exception("Tool %s crashed", tool_name)
            log_tool_execution(tool_name, False, duration_ms, error=str(error))
            import traceback
            tb_lines = traceback.format_exception(type(error), error, error.__traceback__)
            # Include last 5 lines of traceback for debugging
            tb_excerpt = "".join(tb_lines[-5:]).strip()
            error_type = type(error).__name__

            return ToolResult(
                ok=False,
                output=f"[{error_type}] Tool {tool_name} crashed: {error}\n"
                       f"Traceback (most recent):\n{tb_excerpt}"
            )

    def dispose(self) -> None:
        if self._disposer is not None:
            self._disposer()
