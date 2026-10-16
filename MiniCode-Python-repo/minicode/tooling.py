from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, List, Literal, Mapping, Protocol

from minicode.candidate_write_scope import (
    CandidateWriteRefused,
    tool_candidate_write_boundary,
)
from minicode.logging_config import get_logger, log_tool_execution
from minicode.runtime_cancellation import ToolApprovalCanceled
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
    """Bound even a single long line and explicitly label incomplete output."""
    limit = max_chars or _TOOL_OUTPUT_LIMITS.get(tool_name, _DEFAULT_MAX_OUTPUT)
    if len(output) <= limit:
        return output
    notice = (f"\n\n[OUTPUT_TRUNCATED: original {len(output):,} characters; this excerpt is incomplete. "
              "Do not treat omitted content as inspected.]\n")
    rendered = _truncate_output_lines(output, tool_name, limit)
    if len(rendered) + len(notice) <= limit:
        return rendered + notice
    # Line selection cannot bound minified JSON, Base64 or other long lines.
    budget = max(0, limit - len(notice))
    head = budget * 2 // 3
    tail = budget - head
    return (output[:head] + notice + (output[-tail:] if tail else ""))[:limit]


def _truncate_output_lines(output: str, tool_name: str, max_chars: int | None = None) -> str:
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
    max_lines = max(1, int(limit / max(40, avg_line_len)))
    
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

def steering_interruption(runtime, tool_name: str) -> ToolResult | None:
    """Preserve native admission semantics at both request and effect gates."""
    guard = (runtime or {}).get("_userSteeringGuard")
    if callable(guard):
        blocked = guard(tool_name)
        if blocked:
            return ToolResult(ok=False, output=blocked, failureKind="human_steering", inputRejected=True,
                toolRecovery={"status": "interrupted", "kind": "human_steering"})
    return None


class ToolCapability(str, Enum):
    """Tool capability flags."""
    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"
    CONCURRENCY_SAFE = "concurrency_safe"
    REQUIRES_PERMISSION = "requires_permission"
    NETWORK_ACCESS = "network_access"


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
    # Explicit data semantics, not permission/replay authority. Checks and
    # opaque receipts stay inline even when their tool is read-only.
    context_result_kind: Literal["opaque", "read_data", "candidate_check"] = "opaque"
    # Native candidate control changes orchestration state, not a model-selected file.
    candidate_control: bool = False
    # Native orchestration authority is checked independently of workspace effects.
    runtime_control: bool = False
    
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
    waitKind: Literal["input", "approval"] | None = None
    waitReason: str = ""
    timedOut: bool = False
    # Executor-confirmed native deadline: execution has settled and the error
    # can be handed back to the model. This never authorizes automatic replay.
    recoverableTimeout: bool = False
    recoveryGuidance: str = ""
    # Executor observation: a watchdog returned while its tool thread was still
    # alive. A read-only capability alone does not prove execution has exited.
    unsettledTool: bool = False
    # Display/diagnostic metadata, never execution or acceptance authority.
    toolRecovery: dict[str, Any] | None = None
    retryAfterSeconds: float | None = None
    ambiguousSideEffect: bool = False
    runtimeYield: bool = False
    # Native dispatch finished with durable registration/refusal receipts, even
    # if its clock expired. Only the exact native producer may attest this.
    runtimeControlSettled: bool = False
    runtimeStopReason: Literal["consultation_input_exhausted", "tool_input_exhausted", "candidate_submitted", "builder_submitted", "consultation_submitted"] | None = None
    inputRejected: bool = False
    # Producer-confirmed argument rejection before effects. Repeated failures
    # still revalidate live state; this is never permission to replay an effect.
    inputValidationFailed: bool = False
    # Executor guidance is separate from the original immutable tool receipt.
    inputCorrectionHint: str = ""
    inputSchemaRequired: bool = False
    inputIssues: list[dict[str, Any]] = field(default_factory=list)
    # A completed check found a defect; this is still an unsuccessful result,
    # not a transient tool crash or authority to bypass a completion gate.
    verificationFailed: bool = False
    # Advisory producer semantics, never execution/success/approval authority.
    # Optional so older receipts retain their original meaning.
    failureKind: Literal[
        "unavailable_tool", "policy_denied", "dependency_pending", "permission_denied", "invalid_input",
        "not_found", "unsupported_content", "canceled", "transient_error", "remote_rejected", "context_blocked",
        "human_steering", "invalid_check_report",
    ] | None = None
    # Trusted check producer's unchanged before/after workspace revision. This
    # permits a historical request view only, never submission or acceptance.
    contextCandidateRevision: str | None = None
    # Native read producer's raw file hash. This survives the tool thread's
    # binding scope; it is evidence for a request view, never write authority.
    contextReadSha256: str | None = None
    # Native file producer's changed path/content fingerprint. No-op writes have
    # no revision; this only bounds recovery, never authorizes a tool or replay.
    contextWriteRevision: str | None = None
    # A foreground native process was joined and has a known exit status.
    # This is not proof of successful checks or of absence of side effects.
    commandExitCode: int | None = None
    # Bounded native stream evidence for display; never overrides result.ok.
    commandResult: dict[str, Any] | None = None
    # Native command receipt: the foreground process and output readers exited;
    # on interruption, process-tree termination also succeeded. Partial writes
    # may remain. Only the exact native command may attest this to the executor.
    commandExecutionSettled: bool = False
    # Declared script report, distinct from successful process termination.
    # This never certifies independent acceptance or unreported coverage.
    commandCheckReport: dict[str, Any] | None = None
    # True means the delivered text is an excerpt, independently of execution success.
    outputTruncated: bool = False
    # Native image-reader output. Persist references, materialize pixels only
    # when dispatching a model request; ordinary tool text cannot inject them.
    imageInputIds: list[str] = field(default_factory=list)


class ToolInputRejected(ValueError):
    """Expected scoped-input rejection, not a tool crash or execution failure."""

    def __init__(self, output: str, *, exhausted: bool = False) -> None:
        super().__init__(output)
        self.result = ToolResult(ok=False, output=output, inputRejected=True,
            runtimeStopReason="consultation_input_exhausted" if exhausted else None)


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
    recovery_observer: Callable[[dict[str, Any]], None] | None = None
    image_observer: Callable[[list[dict[str, Any]]], None] | None = None
    # Set only by the product user-input adapter, never by tool arguments.
    skill_user_invocation: bool = False


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
    def requires_network(self) -> bool:
        return self.name in {"web_fetch", "web_search", "http_request"} or bool(
            self.metadata and ToolCapability.NETWORK_ACCESS in self.metadata.capabilities
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
    "regex_replace": ToolEffect.READ_ONLY,
    "base64_encode": ToolEffect.READ_ONLY,
    "base64_decode": ToolEffect.READ_ONLY,
    "url_encode": ToolEffect.READ_ONLY,
    "url_decode": ToolEffect.READ_ONLY,
    "current_time": ToolEffect.READ_ONLY,
    "timestamp_convert": ToolEffect.READ_ONLY,
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
    "gzip_compress": ToolEffect.WORKSPACE_WRITE,
    "gzip_decompress": ToolEffect.WORKSPACE_WRITE,
    "tar_create": ToolEffect.WORKSPACE_WRITE,
    "tar_extract": ToolEffect.WORKSPACE_WRITE,
    "zip_create": ToolEffect.WORKSPACE_WRITE,
    "zip_extract": ToolEffect.WORKSPACE_WRITE,
    # Network access is an independent permission from external mutation.
    "web_fetch": ToolEffect.READ_ONLY,
    "web_search": ToolEffect.READ_ONLY,
    "http_request": ToolEffect.EXTERNAL_SIDE_EFFECT,
    # Shell, git, user interaction, state mutation, skills and delegation.
    "run_command": ToolEffect.PRIVILEGED,
    "test_runner": ToolEffect.PRIVILEGED,
    "git": ToolEffect.PRIVILEGED,
    "ask_user": ToolEffect.PRIVILEGED,
    "todo_write": ToolEffect.PRIVILEGED,
    "load_skill": ToolEffect.PRIVILEGED,
    "discover_skills": ToolEffect.READ_ONLY,
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


_model_tool_scope: ContextVar[ToolRegistry | None] = ContextVar("model_tool_scope", default=None)


@contextmanager
def bind_model_tools(tools: ToolRegistry):
    token = _model_tool_scope.set(tools)
    try:
        yield
    finally:
        _model_tool_scope.reset(token)


def model_tool_registry(fallback: ToolRegistry | None) -> ToolRegistry | None:
    """The registry the model is currently being shown.

    ``bind_model_tools`` marks the authoritative view for one activation, so it
    always wins. With nothing bound the caller's own registry is the answer, and a
    caller that passes ``None`` must get ``None``: returning a *different* registry
    as a "fallback" let an unbound context silently shadow the scoped registry an
    adapter was built with -- the model was then shown a tool list that had nothing
    to do with what the Runtime authorised for that node.
    """
    return _model_tool_scope.get() or fallback


def network_access_allowed(runtime: Mapping[str, Any] | None) -> bool:
    value = (runtime or {}).get("runtimeNetworkAccess", True)
    return value is True or isinstance(value, str) and value.strip().lower() == "true"


@contextmanager
def bind_candidate_tool_runtime(runtime: Mapping[str, Any] | None):
    """Install the candidate write boundary, read obligations and read set.

    All three belong to the same side-effect boundary. They are bound here -- on
    the thread that is really about to run the tool -- so a Worker's own gates are
    scoped to its own activation rather than to the process, and the native read
    set records what THIS activation's tools really observed.
    """
    if not isinstance(runtime, Mapping):
        yield
        return
    from minicode.candidate_read_obligations import bind_read_obligations
    from minicode.native_read_set import bind_native_read_set
    from minicode.candidate_write_scope import bind_candidate_write_access

    resolver = runtime.get("_candidateReadObligationsResolver")
    obligations = resolver() if callable(resolver) else runtime.get("_candidateReadObligations")
    binding = runtime.get("_candidateWriteBinding") or (None, None)
    with bind_candidate_write_access(*binding), bind_read_obligations(obligations), \
            bind_native_read_set(runtime.get("_candidateNativeReadSet")):
        yield


class ToolRegistry:
    def __init__(
        self,
        tools: list[ToolDefinition],
        skills: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        disposer: Callable[[], Any] | None = None,
        skills_provider: Callable[[], list[dict[str, Any]]] | None = None,
        _unavailable_reasons: Mapping[str, str] | None = None,
    ) -> None:
        self._tools = tools
        self._skills = skills or []
        self._skills_provider = skills_provider
        self._mcp_servers = mcp_servers or []
        self._disposer = disposer
        # 工具查找缓存 - O(1) 查找代替 O(n) 遍历
        self._tool_index: dict[str, ToolDefinition] = {t.name: t for t in tools}
        # Keep only the reason, never hidden definitions or executable handlers.
        self._unavailable_reasons = {name: reason for name, reason in (_unavailable_reasons or {}).items()
                                     if name not in self._tool_index}

    def list(self) -> list[ToolDefinition]:
        return list(self._tools)

    def list_all(self) -> List[str]:
        return list(self._tool_index.keys())

    def unavailable_reason(self, name: str) -> str:
        return self._unavailable_reasons.get(name, "unknown_tool")

    def filtered_by_effects(self, allowed_effects: set[ToolEffect] | frozenset[ToolEffect]) -> "ToolRegistry":
        """Create a capability view without transferring disposer ownership."""
        allowed = frozenset(allowed_effects)
        return ToolRegistry(
            [tool for tool in self._tools if tool.effect in allowed],
            skills=self._skills,
            mcp_servers=self._mcp_servers,
            skills_provider=self._skills_provider,
            _unavailable_reasons={**self._unavailable_reasons,
                **{tool.name: "effect_restricted" for tool in self._tools if tool.effect not in allowed}},
        )

    def filtered_without_names(self, excluded_names: set[str] | frozenset[str], *,
                               _reason: str = "not_in_scope") -> "ToolRegistry":
        """Create a non-owning view that hides selected tools from the model and executor."""

        excluded = frozenset(excluded_names)
        return ToolRegistry(
            [tool for tool in self._tools if tool.name not in excluded],
            skills=self._skills,
            mcp_servers=self._mcp_servers,
            skills_provider=self._skills_provider,
            _unavailable_reasons={**self._unavailable_reasons,
                **{tool.name: _reason for tool in self._tools if tool.name in excluded}},
        )

    def filtered_by_network(self, runtime: Mapping[str, Any] | None) -> "ToolRegistry":
        if network_access_allowed(runtime):
            return self
        return self.filtered_without_names(frozenset(tool.name for tool in self._tools if tool.requires_network),
                                           _reason="network_disabled")

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
            skills_provider=self._skills_provider,
            _unavailable_reasons=self._unavailable_reasons,
        )

    def effect_manifest(self) -> dict[str, dict[str, Any]]:
        return {
            tool.name: {
                "effect": tool.effect.value,
                "replay_policy": tool.replay_policy.value,
                "requires_network": tool.requires_network,
            }
            for tool in self._tools
        }

    def get_skills(self) -> List[dict[str, Any]]:
        return self._skills_provider() if self._skills_provider is not None else list(self._skills)

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
            reason = self.unavailable_reason(tool_name)
            if reason != "unknown_tool":
                import json
                return ToolResult(ok=False, failureKind="unavailable_tool", output=json.dumps({
                    "error": "Tool unavailable in the current scope; no operation was executed.",
                    "unavailable_names": [tool_name], "unavailable_reasons": {tool_name: reason},
                    "retryable": False,
                    "hint": "Do not repeat this call or bypass the restriction. Use permitted evidence tools; "
                            "return missing verification evidence and the required check to the primary.",
                }))
            return ToolResult(ok=False, output=f"Unknown tool: {tool_name}", failureKind="unavailable_tool")

        # Preserve the historical direct-registry API used by integrations and
        # tests while still giving Runtime policy a concrete context object.
        context = context or ToolContext(cwd=".")

        interrupted = steering_interruption(context._runtime, tool_name)
        if interrupted is not None:
            return interrupted

        skill_runtime = (context._runtime or {}).get("_skillRuntime")
        if skill_runtime is not None:
            try:
                blocked = skill_runtime.guard_tool(tool_name)
            except (OSError, ValueError) as error:
                blocked = "Skill state is unavailable: " + str(error)
            if blocked:
                return ToolResult(ok=False, output=blocked, failureKind="policy_denied", inputRejected=True)

        from minicode.provider_usage import verifier_tool_blocker
        judgment_blocker = verifier_tool_blocker()
        if judgment_blocker is not None:
            return judgment_blocker

        if tool.requires_network and not network_access_allowed(context._runtime):
            return ToolResult(ok=False, output="PolicyDenied: network access is disabled for this Runtime",
                              failureKind="policy_denied")

        if context.cancellation_event is not None and context.cancellation_event.is_set():
            return ToolResult(ok=False, output="Canceled: parent Runtime canceled this tool call", failureKind="canceled")
        runtime_authorized = False
        gateway = getattr(context.runtime_context, "delegation_gateway", context.runtime_context)
        coordination_guard = getattr(gateway, "coordinator_tool_blocker", None)
        if callable(coordination_guard):
            blocker = coordination_guard(context.node_id, context.attempt_id, tool)
            if blocker:
                return ToolResult(ok=False, output=f"PolicyDenied: {blocker}", failureKind="policy_denied")
        if tool.metadata and tool.metadata.runtime_control:
            from minicode.delegation import SchedulerDelegationGateway
            gateway = getattr(context.runtime_context, "delegation_gateway", context.runtime_context)
            if not isinstance(gateway, SchedulerDelegationGateway):
                return ToolResult(ok=False, output="PolicyDenied: native Runtime control binding required", failureKind="policy_denied")
            blocker = gateway.agent_control_blocker(context.node_id, context.attempt_id, tool)
            if blocker:
                return ToolResult(ok=False, output=f"PolicyDenied: {blocker}", failureKind="policy_denied")
            runtime_authorized = True
        if context.allowed_effects is not None and tool.effect not in context.allowed_effects and not runtime_authorized:
            return ToolResult(
                ok=False,
                output=(
                    "PolicyDenied: tool effect "
                    f"{tool.effect.value!r} is outside this Worker's capability scope"
                ),
                failureKind="policy_denied",
            )

        _logger = get_logger("tools")
        _start = time.monotonic()
        from minicode.tools._validation import InputContractError
        try:
            gateway = getattr(context.runtime_context, "delegation_gateway", context.runtime_context)
            process_guard = getattr(gateway, "consultation_tool_blocker", None)
            if callable(process_guard):
                blocker = process_guard(context.node_id, context.attempt_id, tool_name, tool.effect)
                if blocker:
                    return ToolResult(ok=False, output=blocker, failureKind="policy_denied")
            # Phase 1: Input validation (with error context)
            try:
                parsed = tool.validator(input_data)
            except ToolInputRejected as rejected:
                log_tool_execution(tool_name, False, (time.monotonic() - _start) * 1000,
                                   error=str(rejected))
                return rejected.result
            except (ValueError, TypeError, KeyError) as ve:
                log_tool_execution(
                    tool_name, False, (time.monotonic() - _start) * 1000,
                    error=f"input validation: {ve}",
                )
                issues = getattr(ve, "issues", None)
                return ToolResult(
                    ok=False,
                    output=f"Input validation error in {tool_name}: {ve}\n"
                           f"Input was: {str(input_data)[:200]}",
                    failureKind="invalid_input",
                    inputValidationFailed=True,
                    inputIssues=[issue for issue in issues if isinstance(issue, dict)][:4] if isinstance(issues, list) else [],
                )

            # Admit research only after validation, immediately before execution.
            # The turn-local guard retains the registry and atomically enforces
            # its budget across parallel calls; denied proposals spend no quota.
            research_admission = (context._runtime or {}).get("_networkResearchAdmission")
            if callable(research_admission):
                rejection = research_admission(tool_name, context.tool_call_id or "")
                if rejection is not None:
                    return rejection

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
            # The candidate boundary runs immediately before the real write and on
            # the thread that performs it: it re-checks the SAME CandidateRegistry
            # the Runtime's apply uses AND holds that candidate's short mutation
            # lock for the duration of the write, so a file tool and an apply can
            # never both be inside their write. With no boundary bound the context
            # manager yields immediately and behaviour is unchanged.
            try:
                with bind_candidate_tool_runtime(context._runtime):
                    from contextlib import nullcontext
                    from minicode.tools.ask_user import ask_user_tool
                    from minicode.tools.run_command import run_command_tool
                    from minicode.tools.development_services import is_native_service_tool
                    # Native user input has no workspace effect. It retains the
                    # privileged capability/replay policy checked above, but must
                    # never acquire a file mutation lock while waiting for a human.
                    # A same-name extension cannot inherit this classification.
                    native_user_input = tool is ask_user_tool
                    # Only the installed native handler, with its permission gate,
                    # may lease a whole candidate for a process. A same-name
                    # extension or an ungoverned call keeps the file-target gate.
                    command_cwd = (context.cwd if (tool is run_command_tool or is_native_service_tool(tool, "start"))
                                   and context.permissions is not None else None)
                    # These exact native handlers mutate owner-scoped SQLite
                    # state, not a candidate file. Still require the live writer
                    # lease and mutation lock; a same-name extension must name
                    # its file target and cannot inherit this classification.
                    paged_state_control = False
                    if tool_name in {"memory_note_update", "context_rotate"}:
                        from minicode.paged_memory.tools import is_native_paged_state_tool
                        paged_state_control = is_native_paged_state_tool(tool)
                    # Native agent controls acquire their own short ownership locks.
                    # Holding a candidate mutation lock across dispatch would deadlock
                    # snapshot capture; only the exact authenticated handler bypasses it.
                    shared_boundary = getattr(gateway, "shared_write_boundary", None)
                    shared_guard = (shared_boundary(context, tool, parsed)
                        if callable(shared_boundary) and not (runtime_authorized or native_user_input) else nullcontext())
                    with shared_guard, (nullcontext() if runtime_authorized or native_user_input else tool_candidate_write_boundary(
                            tool_name, tool.effect, parsed,
                            candidate_control=paged_state_control or is_native_service_tool(tool, "stop") or bool(tool.metadata and tool.metadata.candidate_control),
                            command_cwd=command_cwd)):
                        result = tool.run(parsed, context)
                    # Record the observation while the activation's read set is
                    # still bound: a real read that returned real bytes is a
                    # native fact the patch's admission argument depends on.
                    from minicode.native_read_set import observe_tool_observation

                    # Native questions and authenticated inbox handlers exchange
                    # conversation state, not candidate-file observations. A
                    # same-name extension cannot inherit this classification.
                    if not (native_user_input or paged_state_control or (runtime_authorized and tool_name == "send_agent_message")):
                        observe_tool_observation(tool_name, parsed, result)
            except CandidateWriteRefused as refused:
                from minicode.shared_writes import SharedWriterWaitRequired
                log_tool_execution(tool_name, False, (time.monotonic() - _start) * 1000,
                                   error=f"candidate write boundary: {refused}")
                return ToolResult(
                    ok=False,
                    output=f"CandidateWriteRefused: {refused}",
                    failureKind="dependency_pending" if isinstance(refused, SharedWriterWaitRequired) else "policy_denied",
                    inputRejected=True,
                )

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
                result.outputTruncated = True

            log_tool_execution(
                tool_name, bool(result.ok), (time.monotonic() - _start) * 1000,
                error=None if result.ok else (result.output or "")[:200],
            )
            return result

        except (KeyboardInterrupt, SystemExit):
            # These should always propagate upward
            raise
        except ToolApprovalCanceled as canceled:
            # A stop during a permission prompt is an expected control outcome.
            # Keep it unsuccessful, but do not report a tool implementation crash.
            return ToolResult(ok=False, output=f"Canceled: {canceled}", failureKind="canceled")
        except InputContractError as rejected:
            # Some read constraints depend on source metadata (e.g. a filtered
            # view's length). They are validated before the requested read,
            # but after admission, and are not tool implementation crashes.
            log_tool_execution(tool_name, False, (time.monotonic() - _start) * 1000,
                               error=f"input constraint: {rejected}")
            return ToolResult(ok=False, output=f"Input constraint error in {tool_name}: {rejected}",
                failureKind="invalid_input", inputValidationFailed=True, inputIssues=rejected.issues[:4])
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
                       f"Traceback (most recent):\n{tb_excerpt}",
                # Retain the original exception and notes (including Runtime
                # workspace recovery diagnostics); only the advice is typed.
                failureKind="permission_denied" if isinstance(error, PermissionError) else None,
            )

    def dispose(self) -> None:
        if self._disposer is not None:
            self._disposer()
