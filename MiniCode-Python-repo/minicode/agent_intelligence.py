from enum import Enum
from dataclasses import dataclass
from typing import Any
from minicode.runtime_protocol import ReplayPolicy


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minicode.agent_metrics import AgentMetricsCollector
    from minicode.tooling import ToolResult

class ErrorCategory(Enum):
    VERIFICATION = "verification"
    NETWORK = "network"
    PERMISSION = "permission"
    RESOURCE = "resource"
    LOGIC = "logic"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class RecoveryStrategy(Enum):
    REPAIR_AND_VERIFY = "repair_and_verify"
    RETRY_EXPONENTIAL_BACKOFF = "retry_exponential_backoff"
    RETRY_IMMEDIATE = "retry_immediate"
    FALLBACK_ALTERNATIVE = "fallback_alternative"
    REQUEST_PERMISSION = "request_permission"
    WAIT_AND_RETRY = "wait_and_retry"
    SKIP_AND_CONTINUE = "skip_and_continue"
    ABORT = "abort"


@dataclass
class ClassifiedError:
    category: ErrorCategory
    strategy: RecoveryStrategy
    confidence: float  # 0.0 - 1.0
    context: dict[str, Any]


@dataclass
class ToolSchedulingSignal:
    """Observed state used by the tool scheduling controller."""

    call_count: int = 0
    write_count: int = 0
    command_count: int = 0
    error_rate: float = 0.0
    avg_latency: float = 0.0
    conflict_count: int = 0
    recent_failures: int = 0


@dataclass
class ToolSchedulingDecision:
    """Controller output for tool execution scheduling."""

    max_workers: int
    concurrency_multiplier: float
    cooldown_seconds: float = 0.0
    retry_backoff_multiplier: float = 1.0
    reasons: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_workers": self.max_workers,
            "concurrency_multiplier": round(self.concurrency_multiplier, 3),
            "cooldown_seconds": round(self.cooldown_seconds, 3),
            "retry_backoff_multiplier": round(self.retry_backoff_multiplier, 3),
            "reasons": list(self.reasons or []),
        }


class ToolSchedulerController:
    """Feedback controller for tool concurrency and retry pressure.

    Existing ToolScheduler decides which calls are concurrency-safe. This
    controller decides how aggressively to run that safe batch, using runtime
    pressure signals such as error rate, latency, conflicts, and recent
    failures.
    """

    def decide(self, signal: ToolSchedulingSignal) -> ToolSchedulingDecision:
        if signal.call_count <= 0:
            return ToolSchedulingDecision(
                max_workers=1,
                concurrency_multiplier=0.0,
                reasons=["no tool calls"],
            )

        multiplier = 1.0
        cooldown = 0.0
        backoff = 1.0
        reasons: list[str] = []

        if signal.write_count > 0:
            multiplier *= 0.65
            reasons.append("write tools present")

        if signal.command_count > 0:
            multiplier *= 0.55
            backoff *= 1.5
            reasons.append("command tools present")

        if signal.error_rate >= 0.5:
            multiplier *= 0.35
            cooldown += 1.0
            backoff *= 2.0
            reasons.append("high tool error rate")
        elif signal.error_rate >= 0.2:
            multiplier *= 0.65
            cooldown += 0.25
            backoff *= 1.3
            reasons.append("elevated tool error rate")

        if signal.avg_latency >= 30.0:
            multiplier *= 0.50
            cooldown += 0.5
            reasons.append("high tool latency")
        elif signal.avg_latency >= 10.0:
            multiplier *= 0.75
            reasons.append("elevated tool latency")

        if signal.conflict_count > 0:
            multiplier *= max(0.35, 1.0 - 0.15 * signal.conflict_count)
            reasons.append("known tool conflicts")

        if signal.recent_failures > 0:
            multiplier *= max(0.40, 1.0 - 0.10 * signal.recent_failures)
            cooldown += min(2.0, 0.25 * signal.recent_failures)
            backoff *= min(3.0, 1.0 + 0.25 * signal.recent_failures)
            reasons.append("recent tool failures")

        multiplier = max(0.15, min(1.0, multiplier))
        max_workers = max(1, min(signal.call_count, int(round(signal.call_count * multiplier))))

        return ToolSchedulingDecision(
            max_workers=max_workers,
            concurrency_multiplier=multiplier,
            cooldown_seconds=cooldown,
            retry_backoff_multiplier=backoff,
            reasons=reasons or ["healthy scheduling pressure"],
        )


class ErrorClassifier:
    """Classifies errors and recommends recovery strategies."""

    # Keyword patterns for each error category
    PATTERNS = {
        ErrorCategory.NETWORK: [
            "connection", "timeout", "network", "refused", "unreachable",
            "reset", "closed", "dns", "ssl", "certificate",
        ],
        ErrorCategory.PERMISSION: [
            "permission", "access denied", "unauthorized", "forbidden",
            "privilege", "not allowed", "restricted", "admin",
        ],
        ErrorCategory.RESOURCE: [
            "memory", "disk", "space", "resource", "quota", "limit",
            "exceeded", "out of", "no space", "too large",
        ],
        ErrorCategory.TIMEOUT: [
            "timeout", "timed out", "deadline", "expired", "took too long",
        ],
        ErrorCategory.LOGIC: [
            "invalid", "not found", "does not exist", "already exists",
            "bad request", "syntax", "parse", "format", "type error",
        ],
    }

    # Strategy mapping based on category
    STRATEGY_MAP = {
        ErrorCategory.VERIFICATION: RecoveryStrategy.REPAIR_AND_VERIFY,
        ErrorCategory.NETWORK: RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF,
        ErrorCategory.TIMEOUT: RecoveryStrategy.WAIT_AND_RETRY,
        ErrorCategory.PERMISSION: RecoveryStrategy.REQUEST_PERMISSION,
        ErrorCategory.RESOURCE: RecoveryStrategy.WAIT_AND_RETRY,
        ErrorCategory.LOGIC: RecoveryStrategy.FALLBACK_ALTERNATIVE,
        ErrorCategory.UNKNOWN: RecoveryStrategy.FALLBACK_ALTERNATIVE,
    }

    FAILURE_STRATEGIES = {
        "context_blocked": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "unavailable_tool": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "policy_denied": (ErrorCategory.PERMISSION, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "dependency_pending": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "permission_denied": (ErrorCategory.PERMISSION, RecoveryStrategy.REQUEST_PERMISSION),
        "invalid_input": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "human_steering": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "invalid_check_report": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "not_found": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "unsupported_content": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "canceled": (ErrorCategory.UNKNOWN, RecoveryStrategy.ABORT),
        "uncertain_effect": (ErrorCategory.UNKNOWN, RecoveryStrategy.ABORT),
        "waiting_user": (ErrorCategory.UNKNOWN, RecoveryStrategy.ABORT),
        "runtime_control": (ErrorCategory.UNKNOWN, RecoveryStrategy.ABORT),
        "transient_error": (ErrorCategory.NETWORK, RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF),
        "remote_rejected": (ErrorCategory.LOGIC, RecoveryStrategy.FALLBACK_ALTERNATIVE),
        "replay_restricted": (ErrorCategory.UNKNOWN, RecoveryStrategy.FALLBACK_ALTERNATIVE),
    }

    @classmethod
    def _failure(cls, kind: str, output: str, tool_name: str, *, confidence: float = 1.0) -> ClassifiedError:
        category, strategy = cls.FAILURE_STRATEGIES[kind]
        return ClassifiedError(category, strategy, confidence,
            {"tool_name": tool_name, "error_snippet": output[:200], "failure_kind": kind})

    @classmethod
    def classify_tool_result(cls, result: "ToolResult", tool_name: str = "", *,
                             replay_policy: ReplayPolicy | None = None) -> ClassifiedError:
        classified = cls._classify_tool_result(result, tool_name)
        if (result.ok is False and classified.strategy in {
                RecoveryStrategy.RETRY_IMMEDIATE, RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF,
                RecoveryStrategy.WAIT_AND_RETRY} and replay_policy is not ReplayPolicy.SAFE):
            # A connectivity diagnosis is never proof that an operation did not run.
            # Keyed/verify-before-retry tools need their own reconciliation authority.
            classified = ClassifiedError(classified.category, RecoveryStrategy.FALLBACK_ALTERNATIVE,
                classified.confidence, {**classified.context, "replay_restricted": True,
                    "replay_policy": replay_policy.value if replay_policy is not None else "unknown"})
        return classified

    @classmethod
    def _classify_tool_result(cls, result: "ToolResult", tool_name: str = "") -> ClassifiedError:
        """Use explicit producer semantics before heuristic text classification.

        A name, assertion-looking output or nonzero shell exit cannot establish
        that the checker ran. Operational/control failures take precedence.
        This classification changes advice only; result.ok remains authoritative
        for failed observations and ordered handoff barriers.
        """
        if result.ok is False:
            # A typed operational hint cannot override an unresolved effect,
            # user wait, Runtime handoff, timeout or rejected control input.
            if result.ambiguousSideEffect:
                return cls._failure("uncertain_effect", result.output, tool_name)
            if result.awaitUser or result.waitKind:
                return cls._failure("waiting_user", result.output, tool_name)
            if result.runtimeYield or result.runtimeStopReason or result.backgroundTask is not None:
                return cls._failure("runtime_control", result.output, tool_name)
            if result.timedOut:
                return ClassifiedError(ErrorCategory.TIMEOUT, RecoveryStrategy.WAIT_AND_RETRY, 1.0,
                    {"tool_name": tool_name, "error_snippet": result.output[:200]})
            if result.inputRejected and result.failureKind in {"dependency_pending", "context_blocked", "human_steering"}:
                return cls._failure(result.failureKind, result.output, tool_name)
            if result.inputRejected:
                return cls._failure("invalid_input", result.output, tool_name)
            kind = result.failureKind
            if isinstance(kind, str) and kind in cls.FAILURE_STRATEGIES:
                return cls._failure(kind, result.output, tool_name)
        if (result.verificationFailed is True and result.ok is False
            and not (result.awaitUser or result.waitKind or result.timedOut
                     or result.ambiguousSideEffect or result.inputRejected
                     or result.runtimeYield or result.runtimeStopReason)):
            return ClassifiedError(
                category=ErrorCategory.VERIFICATION,
                strategy=RecoveryStrategy.REPAIR_AND_VERIFY,
                confidence=1.0,
                context={"tool_name": tool_name, "error_snippet": result.output[:200]},
            )
        return cls.classify(result.output, tool_name=tool_name)

    @classmethod
    def classify(cls, error_message: str, tool_name: str = "") -> ClassifiedError:
        """Classify an error message and recommend a strategy."""
        # Compatibility advice for old built-in receipts. These exact prefixes
        # never establish success, evidence, permissions or execution authority.
        for prefix, kind in (
            ("Context protection: this operation was not executed.", "context_blocked"),
            ("The whole tool batch may exceed the context protection limit.", "context_blocked"),
            ("[Memory preflight blocked]", "context_blocked"),
            ("Unknown tool: ", "unavailable_tool"),
            ("PolicyDenied: ", "policy_denied"),
            ("Pilot file boundary: ", "policy_denied"),
            ("Input validation error in ", "invalid_input"),
            ("Canceled: parent Runtime canceled this tool call", "canceled"),
        ):
            if error_message.startswith(prefix):
                return cls._failure(kind, error_message, tool_name, confidence=0.95)
        error_lower = error_message.lower()

        scores: dict[ErrorCategory, int] = {}
        for category, patterns in cls.PATTERNS.items():
            score = sum(1 for p in patterns if p in error_lower)
            if score > 0:
                scores[category] = score

        if scores:
            best_category = max(scores, key=scores.get)
            confidence = min(0.95, 0.5 + max(scores.values()) * 0.15)
        else:
            best_category = ErrorCategory.UNKNOWN
            confidence = 0.3

        strategy = cls.STRATEGY_MAP.get(best_category, RecoveryStrategy.FALLBACK_ALTERNATIVE)

        return ClassifiedError(
            category=best_category,
            strategy=strategy,
            confidence=confidence,
            context={"tool_name": tool_name, "error_snippet": error_message[:200]},
        )


class NudgeGenerator:
    """Generates intelligent nudge messages based on failure context."""

    FAILURE_TEMPLATES = {
        "context_blocked": (
            "This operation was refused before execution; it produced no new side effects. "
            "Repair the reported context coverage or save a current, verified memory_note_update and rotate. "
            "After context recovery succeeds, explicitly issue the needed operation. "
            "Waiting, disk cleanup and changing command tools do not resolve a context admission failure."
        ),
        "dependency_pending": (
            "This call was stopped before execution because shared writers still own scopes. "
            "Use the receipt's native wait_agents arguments with mode=all and a bounded timeout, "
            "then inspect current child outcomes and scope release. Do not repeat commands or bypass "
            "the gate through another shell. A timeout returns control without stopping children. "
            "For another wait use a new wait_id; terminal/unresolved activations require status inspection, "
            "not repeated waits on terminal IDs. Once scopes release, reread changed files and issue "
            "the command explicitly. Failed or canceled children preserve partial work and are not success."
        ),
        "unavailable_tool": (
            "This tool is not available in the current tool set. Do not repeat the same call. "
            "Check the tools actually provided and use a supported operation within your assigned scope. "
            "If none can perform required work, report the missing capability; do not claim completion."
        ),
        "policy_denied": (
            "The current Runtime policy rejected this operation. Do not repeat it unchanged or bypass "
            "the boundary through another tool, shell, path or agent. Follow any stated prerequisite "
            "using available authorized operations. If required work remains blocked, report the boundary "
            "and the missing result; do not weaken acceptance or claim success."
        ),
        "permission_denied": (
            "Permission was denied. Respect the denial; do not repeat the operation unchanged or bypass "
            "it with elevated privileges, another tool or another path. Use the product's approval flow "
            "only if this operation is eligible and approval has not already been denied. Otherwise "
            "report the blocker and leave required work incomplete."
        ),
        "invalid_input": (
            "The input does not meet this operation's contract. Inspect the tool schema and the exact "
            "error, then correct the arguments or reduce the requested input within the stated limits. "
            "Do not repeat unchanged arguments or assume that waiting will fix them."
        ),
        "not_found": (
            "The requested path was not found. Check its spelling, the workspace root and the provided "
            "file inventory using available authorized tools. Do not repeatedly read the same absent "
            "path or invent file contents. Required evidence is still missing: report the gap if it "
            "cannot be resolved, rather than treating the step as optional."
        ),
        "unsupported_content": (
            "This tool cannot decode the requested content in the declared format. Repeating the same read will "
            "not change its format. Use an available, authorized reader for that format, or report "
            "that the required content cannot be inspected."
        ),
        "canceled": "The Runtime canceled this operation. Stop; do not issue replacement calls or claim completion.",
        "uncertain_effect": (
            "The operation's side effects are unresolved. Do not repeat it or assume it was not executed. "
            "Follow the Runtime's reconciliation/status process and report uncertainty until resolved."
        ),
        "waiting_user": (
            "This operation is waiting for user input or approval. Preserve that wait; do not repeat "
            "the call, infer permission or use another tool to bypass it."
        ),
        "runtime_control": (
            "The Runtime has requested a yield or stop. Follow that control outcome; do not retry "
            "the operation or treat the handoff as proof of final completion."
        ),
        "remote_rejected": (
            "The remote service rejected this request. Inspect its status and response, then correct "
            "the request or resolve access through the authorized workflow. Do not repeat unchanged "
            "requests or assume that waiting will fix a permanent rejection."
        ),
        "replay_restricted": (
            "Automatic replay is not authorized for this operation. A failure response does not prove "
            "that nothing was executed. Inspect the existing output or remote operation status first. "
            "Use the tool's verification/idempotency workflow where supported; do not repeat the call "
            "or switch tools to repeat the same effect while its outcome is unresolved."
        ),
    }

    TEMPLATES = {
        ErrorCategory.VERIFICATION: {
            RecoveryStrategy.REPAIR_AND_VERIFY: (
                "The check completed and reported failures. This is not evidence of a transient tool outage. "
                "Inspect the reported failures; repair the implementation only within your assigned scope, "
                "then rerun the unchanged checks. If you are read-only, return the findings and limitations. "
                "Do not repeat unchanged checks without a reason, weaken acceptance, or claim success."
            ),
        },
        ErrorCategory.NETWORK: {
            RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF: (
                "A transient connectivity failure is possible. Retry only when the tool's replay "
                "policy permits it, with bounded attempts and backoff (honor Retry-After when present). "
                "A network error alone does not prove that the request had no effect."
            ),
            RecoveryStrategy.RETRY_IMMEDIATE: (
                "Check the operation's replay policy before any retry; do not immediately repeat "
                "an operation whose effects are unknown."
            ),
        },
        ErrorCategory.PERMISSION: {
            RecoveryStrategy.REQUEST_PERMISSION: (
                FAILURE_TEMPLATES["permission_denied"]
            ),
            RecoveryStrategy.FALLBACK_ALTERNATIVE: (
                FAILURE_TEMPLATES["policy_denied"]
            ),
        },
        ErrorCategory.RESOURCE: {
            RecoveryStrategy.WAIT_AND_RETRY: (
                "Resource limit reached (memory/disk/quota). Consider: "
                "(1) freeing up resources before retrying, "
                "(2) processing in smaller batches, or "
                "(3) using a more efficient approach."
            ),
        },
        ErrorCategory.TIMEOUT: {
            RecoveryStrategy.WAIT_AND_RETRY: (
                "The operation timed out. This may be due to heavy load or a long-running process. "
                "Check the replay policy and operation status before (1) retrying after a brief wait, "
                "(2) breaking the task into smaller steps, or "
                "(3) using a more efficient approach."
            ),
        },
        ErrorCategory.LOGIC: {
            RecoveryStrategy.FALLBACK_ALTERNATIVE: (
                "Inspect the exact error, tool schema and current workspace before correcting the "
                "operation. Use only available tools within your scope. A failed lookup does not "
                "make required evidence optional; report unresolved gaps without claiming completion."
            ),
            RecoveryStrategy.SKIP_AND_CONTINUE: (
                "Check the task contract before omitting this step. Required work and evidence cannot "
                "be skipped just because a tool failed; report any unresolved requirement."
            ),
        },
        ErrorCategory.UNKNOWN: {
            RecoveryStrategy.FALLBACK_ALTERNATIVE: (
                "The cause is not established. Inspect the original error and current operation state "
                "before deciding a recovery action. Do not repeat an operation blindly, assume it had "
                "no side effects, or claim missing work is complete."
            ),
        },
    }

    @classmethod
    def generate(cls, classified_error: ClassifiedError, retry_count: int = 0) -> str:
        """Generate a nudge message based on classified error."""
        if classified_error.context.get("replay_restricted"):
            return cls.FAILURE_TEMPLATES["replay_restricted"]
        category = classified_error.category
        strategy = classified_error.strategy
        failure_message = cls.FAILURE_TEMPLATES.get(classified_error.context.get("failure_kind"))
        if failure_message is not None:
            return failure_message

        # Get base template
        category_templates = cls.TEMPLATES.get(category, cls.TEMPLATES[ErrorCategory.UNKNOWN])
        base_message = category_templates.get(
            strategy,
            cls.TEMPLATES[ErrorCategory.UNKNOWN][RecoveryStrategy.FALLBACK_ALTERNATIVE],
        )

        # Add retry context
        if retry_count > 0 and strategy in {
            RecoveryStrategy.RETRY_IMMEDIATE, RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF,
            RecoveryStrategy.WAIT_AND_RETRY,
        }:
            base_message += f" (This is retry attempt {retry_count + 1})"

        # Add tool-specific hints
        tool_name = classified_error.context.get("tool_name", "")
        if tool_name in ["write_file", "edit_file"] and category == ErrorCategory.LOGIC:
            base_message += " For file operations, verify the path exists and you have write permissions."
        elif tool_name == "grep_files" and category == ErrorCategory.LOGIC:
            base_message += " Verify the pattern against the known file inventory within your allowed scope."
        elif tool_name == "read_file" and category in (ErrorCategory.LOGIC, ErrorCategory.RESOURCE):
            base_message += " Verify the path against the known file inventory within your allowed scope."
        elif tool_name == "edit_file" and category == ErrorCategory.LOGIC:
            base_message += " The search string must match the current file content exactly."
        elif category == ErrorCategory.TIMEOUT:
            base_message += " Try breaking this into smaller steps or reducing the scope."

        return base_message

    @classmethod
    def generate_progress_nudge(cls, tool_results: list[tuple[str, bool]]) -> str | None:
        """Generate a nudge when tools have been executed but model returns empty/progress."""
        if not tool_results:
            return None

        success_count = sum(1 for _, ok in tool_results if ok)
        failure_count = len(tool_results) - success_count

        if failure_count == 0:
            return (
                f"All {success_count} tool(s) executed successfully. "
                "Continue with the next concrete step or provide a <final> answer if complete."
            )
        elif failure_count == len(tool_results):
            return (
                f"All {failure_count} tool(s) failed. "
                "Review the errors, adjust your approach, and try again with corrected parameters."
            )
        else:
            return (
                f"{success_count} tool(s) succeeded, {failure_count} failed. "
                "Address the failures first, then continue with remaining tasks."
            )


class ToolScheduler:
    """Intelligently schedules tool execution based on historical performance."""

    def __init__(
        self,
        metrics_collector: "AgentMetricsCollector | None" = None,
        controller: ToolSchedulerController | None = None,
    ):
        self._metrics = metrics_collector
        self._controller = controller or ToolSchedulerController()
        self._conflict_history: dict[frozenset[str], int] = {}  # Track tool pair conflicts
        self._last_decision: ToolSchedulingDecision | None = None

    def schedule_calls(self, calls: list[dict], tools: Any) -> tuple[list[dict], list[dict]]:
        """Run an independent read prefix concurrently, then an ordered suffix.

        A write, unresolved tool, or known read conflict is an ordering barrier.
        Never pull a later check before its write or rank writes by reliability.
        The two-phase executor joins the prefix before starting the suffix.
        """
        if len(calls) <= 1:
            return calls, []

        concurrent_calls: list[dict] = []
        serial_calls: list[dict] = []

        for call in calls:
            tool_name = call["toolName"]
            tool_def = tools.find(tool_name)

            # Only pure reads may leave the AgentLoop Worker thread.  A tool
            # can be concurrency-safe in its own implementation while still
            # mutating the workspace or an external system; those calls must
            # remain serial so timeout/cancellation can join the real effect.
            if (
                serial_calls
                or not tool_def
                or not tool_def.is_read_only
                or not tool_def.is_concurrency_safe
            ):
                serial_calls.append(call)
                continue

            # Check if this tool conflicts with already-selected concurrent tools
            conflicts = self._has_conflicts(tool_name, concurrent_calls)
            if conflicts:
                serial_calls.append(call)
            else:
                concurrent_calls.append(call)

        return concurrent_calls, serial_calls

    def _get_tool_score(self, tool_name: str) -> float:
        """Get reliability score for a tool (0.0 - 1.0)."""
        if self._metrics is None:
            return 1.0
        stats = self._metrics.get_tool_stats(tool_name)
        return stats.success_rate

    def _has_conflicts(self, tool_name: str, concurrent_calls: list[dict]) -> bool:
        """Check if tool has known conflicts with concurrent calls."""
        for other_call in concurrent_calls:
            other_name = other_call["toolName"]
            pair = frozenset({tool_name, other_name})
            conflict_count = self._conflict_history.get(pair, 0)
            if conflict_count >= 2:  # Known conflict threshold
                return True
        return False

    def record_conflict(self, tool1: str, tool2: str) -> None:
        """Record that two tools had a conflict when run concurrently."""
        pair = frozenset({tool1, tool2})
        self._conflict_history[pair] = self._conflict_history.get(pair, 0) + 1

    def get_recommended_max_workers(
        self,
        concurrent_calls: list[dict],
        *,
        error_rate: float = 0.0,
        avg_latency: float = 0.0,
        recent_failures: int = 0,
    ) -> int:
        """Recommend max workers based on call characteristics."""
        if not concurrent_calls:
            self._last_decision = ToolSchedulingDecision(
                max_workers=1,
                concurrency_multiplier=0.0,
                reasons=["no concurrent calls"],
            )
            return 1

        base = min(len(concurrent_calls), 8)

        # Reduce workers if we have file write operations
        write_tools = {"write_file", "edit_file", "patch_file", "modify_file"}
        write_count = sum(1 for c in concurrent_calls if c["toolName"] in write_tools)
        if write_count > 0:
            base = min(base, 4)

        # Reduce further if we have command executions
        command_tools = {"run_command", "execute_command", "bash"}
        cmd_count = sum(1 for c in concurrent_calls if c["toolName"] in command_tools)
        if cmd_count > 0:
            base = min(base, 3)

        signal = ToolSchedulingSignal(
            call_count=base,
            write_count=write_count,
            command_count=cmd_count,
            error_rate=error_rate,
            avg_latency=avg_latency,
            conflict_count=self._count_conflicts(concurrent_calls),
            recent_failures=recent_failures,
        )
        self._last_decision = self._controller.decide(signal)
        return max(1, min(base, self._last_decision.max_workers))

    @property
    def last_decision(self) -> ToolSchedulingDecision | None:
        return self._last_decision

    def _count_conflicts(self, calls: list[dict]) -> int:
        count = 0
        for i, call in enumerate(calls):
            for other in calls[i + 1:]:
                pair = frozenset({call["toolName"], other["toolName"]})
                if self._conflict_history.get(pair, 0) >= 2:
                    count += 1
        return count
