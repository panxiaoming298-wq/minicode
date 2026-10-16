"""Unified Standard/Deep/Ultra Runtime orchestration.

Standard, Deep, and Ultra share every protocol and authority in this module. The
Standard fast path uses deterministic zero-model planning/validation/gating and
one existing AgentLoop Worker; Deep fixes deep-single, while Ultra may select
deep-single or read fan-out.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

from minicode.auto_mode import PermissionMode
from minicode.delegation import SchedulerDelegationGateway
from minicode.runtime_messages import is_user_input
from minicode.provider_usage import RunUsageBudget
from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore
from minicode.runtime_budget import (
    apply_budget_to_existing_adapter,
    apply_reasoning_budget,
)
from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    BudgetEnvelope,
    ContextManifest,
    DeliveryManifest,
    ExecutionFailureKind,
    ExecutionProfile,
    NodeState,
    PlanNode,
    PlanVersion,
    PrimaryRepair,
    RequestedMode,
    RequiredConsultation,
    RuntimeCommand,
    RuntimeCommandType,
    RuntimeRunIdentity,
    SelectedRoute,
    TaskAmendment,
    TaskContract,
    TaskEnvelope,
    ToolEffect,
    TopologyDecision,
    VerificationLevel,
    VerificationRecord,
    WriteIsolation,
    acceptance_criterion_ids,
    new_id,
)
from minicode.runtime_scheduler import SchedulerReport, TaskScheduler
from minicode.runtime_shutdown import ExecutionDrain
from minicode.run_ownership import RuntimeRunOwnerLease
from minicode.runtime_state import (
    ReducerError,
    RuntimeReducer,
    RuntimeStateLoadError,
    RuntimeStateStore,
    command,
)
from minicode.task_graph import CanonicalPlanGraph
from minicode.tooling import ToolRegistry
from minicode.turn_events import TurnEvent
from minicode.types import ChatMessage
from minicode.verifier_registry import (
    CallableVerifier,
    CompletionGate,
    IsolatedCommandVerifier,
    NodeResultVerifier,
    VerificationContext,
    VerifierRegistry,
    workspace_snapshot_evidence,
)
from minicode.worker_runtime import AgentLoopWorkerRunner
from minicode.workspace_lock import WorkspaceWriteLease, normalize_workspace_path
from minicode.workspace_provider import WorkspaceConflict, snapshot_revision, workspace_snapshot


class PlanValidationError(ValueError):
    pass


_INTEGRATED_VERIFICATION_TASK = re.compile(
    r"(?ix)"
    r"\b(?:implement|modify|change|fix|debug|diagnos(?:e|is)|refactor|migrate|"
    r"build|create|delete|install|configure|audit|secure|test|root[ -]?cause)\b"
    # Here 实现 is a change verb; in 阅读实现 / 分析实现 it names source code.
    r"|(?<!阅读)(?<!分析)(?<!解释)(?<!说明)(?<!梳理)(?<!查看)实现"
    r"|修改|修复|新增|添加|删除|重构|迁移|创建|构建|开发|安装|配置|"
    r"排查|诊断|审计|安全检查|根因|竞态|测试|验收"
)

# Mask only explicitly negated change verbs, not the rest of their clause:
# "do not modify files; audit authentication" still needs an independent review.
# Ambiguous language and positive instructions keep the conservative classifier.
_VERIFICATION_CHANGE_EN = r"(?:implement|modify|edit|write|change|fix|refactor|migrate|build|create|delete|install|configure)"
_VERIFICATION_CHANGE_ZH = r"(?:实现|修改|改动|写入|修复|新增|添加|删除|生成|重构|迁移|创建|构建|开发|安装|配置)"
_NEGATED_VERIFICATION_CHANGE = re.compile(
    rf"(?ix)\b(?:do\s+not|don['’]t|never|without)\s+{_VERIFICATION_CHANGE_EN}"
    rf"(?:\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+){_VERIFICATION_CHANGE_EN})*\b"
    rf"|(?<!不)(?<!不能)(?<!不得)(?<!不要)(?<!并非)(?<!不是)"
    rf"(?:不允许|不需要|无需|无须|不要|禁止|不得|不能|不会|不)\s*{_VERIFICATION_CHANGE_ZH}"
    rf"(?:\s*[、和或及]\s*{_VERIFICATION_CHANGE_ZH})*"
)

# State reports such as 未配置 and 已安装 are not requests to perform those
# changes. Mask only the state phrase: later change verbs and audit signals
# must remain visible. Keep double negatives conservative, including 尚未/还未.
_REPORTED_VERIFICATION_CHANGE = re.compile(
    r"(?<!不)(?<!不能)(?<!不得)(?<!不要)(?<!并非)(?<!不是)(?<!尚)(?<!还)(?<!未)"
    rf"(?:尚未|还未|未曾|没有|未|已经|已)\s*{_VERIFICATION_CHANGE_ZH}"
    rf"(?:\s*[、和或及]\s*{_VERIFICATION_CHANGE_ZH})*"
)

_WORKSPACE_WRITE_TASK = re.compile(
    r"(?ix)"
    r"\b(?:implement|modify|change|fix|debug|refactor|migrate|build|create|"
    r"delete|remove|install|configure|write|update|patch|generate|scaffold|"
    r"commit|rename|move)\b"
    r"|实现|修改|修复|新增|添加|删除|重构|迁移|创建|构建|开发|安装|配置|"
    r"写入|更新|补丁|生成|提交|重命名|移动"
)


_WRITE_ACTION_ZH = r"(?:修改|改动|写入|删除|生成|创建|新增|添加)"
_WRITE_ACTION_EN = r"(?:modify|edit|write|change|delete|create)"
_NO_WORKSPACE_WRITE = re.compile(
    rf"(?ix)\b(?:do\s+not|don't|never)\s+{_WRITE_ACTION_EN}"
    rf"(?:\s*(?:,\s*(?:or|and)?|or|and)\s*{_WRITE_ACTION_EN})*\s+"
    r"(?:(?:any|the|project|workspace)\s+)*(?:files?|code|workspace)\b"
    r"|\b(?:read[ -]only\s+(?:analysis|audit|review|inspection|task)|(?:only|just)\s+(?:analyze|review|inspect))\b"
    rf"|(?:不要|禁止|不得|不允许){_WRITE_ACTION_ZH}(?:\s*[、，,和或及]\s*{_WRITE_ACTION_ZH})*"
    r"(?:任何|所有|当前|本)?(?:文件|代码|工作区|项目)"
    r"|只读(?=[）)；;。！!\n]|\s*$)"
    r"|只读(?:分析|审查|检查|任务)|(?:只|仅)(?:做)?(?:分析|审查|检查)"
)
_ALLOW_WORKSPACE_WRITE = re.compile(
    r"(?ix)\b(?:now|you\s+may|you\s+can|go\s+ahead\s+and)\s+(?:modify|edit|write|change|implement|fix)\b"
    r"|(?<!不)(?<!未)(?<!没有)(?:现在可以|允许|授权)(?:修改|改动|写入|实现|修复)|现在(?:开始)?(?:修改|实现|修复)"
)
_READ_ONLY_SOURCE_OPERATION = re.compile(
    r"只读(?:检查|分析|审查)(?:并复制)?(?:旧目录|原目录|旧候选|参考文件|历史产物)"
    r"|只读(?:检查|分析|审查)并复制(?:这些|其中的|已有的)(?:配置|依赖|文件)"
    r"|\bread[ -]only\s+(?:inspection|review|analysis)\s+of\s+(?:the\s+)?(?:old|source|reference)\s+(?:directory|files?|candidate)\b",
    re.IGNORECASE,
)
_NEGATED_READ_ONLY_OPERATION = re.compile(
    r"(?:不能|不要|不得|不可|不应|不该|不许|别|勿|并非|不是|不)\s*"
    r"(?:只读(?:分析|审查|检查|任务)?|(?:只|仅仅?)(?:做)?(?:分析|审查|检查))"
    r"|\b(?:do\s+not|don['’]t|never|cannot|can't|must\s+not|should\s+not|not)\s+"
    r"(?:only|just)\s+(?:analyze|review|inspect)\b",
    re.IGNORECASE,
)


def _runtime_state_path(runtime: Mapping[str, Any], run_id: str) -> Path:
    """The one durable state file this run owns, from the same inputs everywhere."""

    configured = runtime.get("runtimeStatePath")
    if configured:
        return Path(str(configured))
    from minicode.config import MINI_CODE_DIR

    return MINI_CODE_DIR / "runtime-v2" / "runs" / f"{run_id}.json"


def _capability_probe_invocation(reducer, node_id: str, profile):
    """A stand-in invocation for capability enumeration only; it authorises nothing.

    It carries the registered node's own role and effects, so the tool view it
    enumerates is the one that node's real invocation is filtered to. It is never
    given to a Worker, so it can never become an execution capability.
    """

    node = next((item for item in reducer.current_plan.nodes if item.node_id == node_id), None)
    if node is None:
        return None
    return AgentInvocationSpec(
        run_id=reducer.run_id,
        plan_version=reducer.current_plan.plan_version,
        node_id=node.node_id,
        attempt_id=f"capability-probe-{node.node_id}",
        role=node.role,
        objective="capability enumeration only",
        # The same manifest shape a real invocation carries, built from the node's
        # own definition so the tool view is filtered exactly as the live one is.
        context_manifest=ContextManifest(
            contract_revision=reducer.current_plan.contract_revision,
            plan_version=reducer.current_plan.plan_version,
            node_id=node.node_id,
            base_revision="capability-probe",
            included_files={},
            tool_capability_scope=node.allowed_effects,
            budget_allocation=profile.budget,
        ),
        allowed_effects=node.allowed_effects,
        budget=profile.budget,
        idempotency_key=f"capability-probe-{node.node_id}",
    )


def _live_capability_receipt(*, runtime: Mapping[str, Any], reducer, scheduler,
                             worker, profile, preferred_node: str) -> dict[str, Any]:
    """What the real binding gave the live request, measured not declared.

    ``tools`` is the tool view the preferred node's invocation will really carry,
    read through the same Worker method that builds it. A refusal to enumerate is
    reported as a refusal, never as an empty capability that looks identical to
    "the tools are missing".
    """

    names: list[str] = []
    initial_names: list[str] = []
    refusal = ""
    try:
        invocation = _capability_probe_invocation(reducer, preferred_node, profile)
        if invocation is None:
            refusal = "no live attempt is registered for the preferred node yet"
        else:
            registry = worker._tools_for_invocation(invocation)
            names = sorted(registry.list_all())
            from minicode.tool_discovery import ToolDiscovery
            discovery = ToolDiscovery()
            initial_names = sorted(discovery.model_tools(
                registry.with_additional_tools([discovery.tool()]), provider_runtime=runtime).list_all())
    except Exception as error:  # noqa: BLE001 - a refusal is the honest report here
        refusal = f"{type(error).__name__}: {error}"
    gateway = getattr(getattr(worker, "runtime_context", None), "builder_gateway", None)
    return {
        "schema": "minicode-live-capability-v1",
        "stage": "gateways_bound_before_first_model_request",
        "run_id": reducer.run_id,
        "preferred_node": preferred_node,
        "parallel_candidates": bool(profile.parallel_candidates),
        "gateway_bound": gateway is not None,
        "gateway": None if gateway is None else {
            "type": type(gateway).__name__,
            "scheduler_bound": getattr(gateway, "scheduler", None) is scheduler,
            "target_candidate_id": getattr(gateway, "target_candidate_id", None),
        },
        "tools": names,
        "tool_count": len(names),
        "initial_model_tools": initial_names,
        "initial_model_tool_count": len(initial_names),
        "initial_model_tool_evidence": "scoped_registry_projection_before_dispatch",
        "enumerated": not refusal,
        "refusal": refusal,
        "model_requests_at_capture": int(
            (reducer.usage_summary() or {}).get("model_calls", 0) or 0),
        "max_output_tokens": runtime.get("maxOutputTokens"),
    }


def _workspace_write_prohibitions(text: str) -> tuple[re.Match[str], ...]:
    # "Cannot only inspect one road" requests broader work; its embedded
    # "only inspect" is not a read-only task restriction, including in history.
    scoped = (*_READ_ONLY_SOURCE_OPERATION.finditer(text),
              *_NEGATED_READ_ONLY_OPERATION.finditer(text))
    return tuple(match for match in _NO_WORKSPACE_WRITE.finditer(text)
                 if not any(source.start() <= match.start() and match.end() <= source.end()
                            for source in scoped))


def _workspace_write_forbidden(
    request: str, runtime: Mapping[str, Any], messages: Iterable[Mapping[str, Any]] = (),
) -> bool:
    forbidden = False
    user_texts = [str(m.get("content", "")) for m in messages if is_user_input(m)]
    for text in (*user_texts, request):
        # A restriction on reading an old input directory is not a prohibition
        # on producing a new project. Keep the original user text for the Worker;
        # only omit exact, scoped input operations from the global signal list.
        signals = [(match.start(), True) for match in _workspace_write_prohibitions(text)]
        signals += [(match.start(), False) for match in _ALLOW_WORKSPACE_WRITE.finditer(text)]
        if signals:
            forbidden = max(signals)[1]
    # Declared contract constraints remain active until amended by the caller.
    constraints = runtime.get("taskConstraints", ())
    if isinstance(constraints, str):
        constraints = (constraints,)
    return forbidden or any(_workspace_write_prohibitions(str(item)) for item in constraints)


def requires_workspace_write(
    request: str, runtime: Mapping[str, Any] | None, messages: Iterable[Mapping[str, Any]] = (),
) -> bool:
    """Resolve execution-owner scope, preserving explicit read-only restrictions."""

    runtime = runtime or {}
    if _workspace_write_forbidden(request, runtime, messages):
        return False
    if "taskRequiresWorkspaceWrite" in runtime:
        # An explicit task contract is stronger evidence than keywords such as
        # "modify" inside a negative instruction or topology heuristics.
        return _strict_bool(runtime["taskRequiresWorkspaceWrite"], default=True)
    hints = runtime.get("topologyHints")
    if isinstance(hints, Mapping) and "requires_workspace_write" in hints:
        return _strict_bool(hints.get("requires_workspace_write"), default=True)
    if runtime.get("expectedOutputs"):
        return True
    # A primary owner must be able to discover and invoke execution tools even
    # when "start the project" or a follow-up contains no file-write keywords.
    # Approval policy governs those invocations; only an explicit task scope
    # removes capabilities. Legacy fixed plans retain their intent classifier.
    if _primary_orchestration(runtime):
        return True
    return bool(_WORKSPACE_WRITE_TASK.search(request))


def _runtime_with_review_directive(
    request: str, runtime: Mapping[str, Any], mode: RequestedMode,
    messages: Iterable[Mapping[str, Any]] = (),
) -> Mapping[str, Any]:
    """Latest trusted human preference overrides this turn's review defaults.

    Keep saved settings unchanged. Runtime feedback and Agent/tool messages do
    not have human authority. History retains conversation preferences only;
    single-run exceptions come exclusively from the current human request.
    """
    from Package.AgentRuntime.Src.Application.Dto.ReviewDirective import ReviewDirectiveRequest
    from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

    app = createApp()
    preference = None
    texts = (str(message.get("content", "")) for message in messages if is_user_input(message))
    for text in texts:
        directive = app.resolve_review_directive(ReviewDirectiveRequest(text, mode.value, include_run_scoped=False))
        if directive is not None:
            preference = directive
    current = app.resolve_review_directive(ReviewDirectiveRequest(request, mode.value))
    if current is not None:
        preference = current
    if preference is None:
        return runtime
    resolved = dict(runtime)
    resolved["runtimeRequireIndependentVerifier"] = preference
    if "runtimeVerifierBudget" in resolved:
        resolved["runtimeVerifierBudget"] = int(preference)
    if not preference:
        resolved["runtimeReviewScope"] = "basic"
    elif resolved.get("runtimeReviewScope") == "basic":
        resolved.pop("runtimeReviewScope")
    return resolved


def _requires_independent_verifier(request: str, runtime: Mapping[str, Any]) -> bool:
    """Return whether a deep-single task has an integrated result to verify.

    Deep and Ultra give conversational and explanatory requests the deep Worker,
    but a second model verifier is only useful when the turn produces or audits
    an external deliverable.  Callers can override the conservative classifier.
    """

    if "runtimeRequireIndependentVerifier" in runtime:
        return _strict_bool(
            runtime.get("runtimeRequireIndependentVerifier"),
            default=False,
        )
    hints = runtime.get("topologyHints")
    if isinstance(hints, Mapping) and "requires_independent_verification" in hints:
        return _strict_bool(
            hints.get("requires_independent_verification"),
            default=False,
        )
    if runtime.get("runtimeVerificationCommands"):
        return True
    if runtime.get("acceptanceCriteria") or runtime.get("expectedOutputs"):
        return True
    return bool(_INTEGRATED_VERIFICATION_TASK.search(
        _NEGATED_VERIFICATION_CHANGE.sub(" ", request)
    ))


def _resolve_review_policy(request: str, runtime: Mapping[str, Any], mode: RequestedMode):
    """Admission signals describe work, rather than treating every change verb as risk.

    The initial policy is frozen in the run identity. Post-candidate escalation
    is recorded separately, so recovery retains the observed review requirement.
    Registered commands, output checks and acceptance criteria remain mandatory.
    """
    from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicyRequest
    from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

    hints = runtime.get("topologyHints")
    hints = hints if isinstance(hints, Mapping) else {}
    explicit = None
    if "runtimeRequireIndependentVerifier" in runtime:
        explicit = _strict_bool(runtime["runtimeRequireIndependentVerifier"], default=False)
    elif "requires_independent_verification" in hints:
        explicit = _strict_bool(hints["requires_independent_verification"], default=False)
    if "runtimeVerifierBudget" in runtime:
        enabled = _nonnegative_budget(runtime, "runtimeVerifierBudget", default=0) > 0
        if explicit is not None and explicit != enabled:
            raise ValueError("runtimeVerifierBudget conflicts with the independent review requirement")
        explicit = enabled

    text = _REPORTED_VERIFICATION_CHANGE.sub(" ", _NEGATED_VERIFICATION_CHANGE.sub(" ", request))
    # A double negative such as 不是不要修改代码 requests a change. Preserve
    # scoped read-only restrictions, without treating that embedded phrase as one.
    writing = bool(_WORKSPACE_WRITE_TASK.search(text)) and not any(
        not re.search(r"(?:不是|并非|不能|不得|不要|不)$", text[:match.start()])
        for match in _workspace_write_prohibitions(text)
    )
    auditing = bool(re.search(r"(?i)\b(?:audit|security review)\b|审计|安全检查", text))
    code_change = writing and bool(re.search(
        r"(?i)\b(?:code|bug|function|component|endpoint|handler|exception|module|api|logic|feature|parser|tool|registry)\b"
        r"|\.(?:py|tsx?|jsx?|vue|go|rs|java|cpp|cs)\b|代码|函数|组件|接口|异常|逻辑|模块|功能|工具|注册|解析器", text))
    change_scope = hints.get("change_scope", "unknown")
    if change_scope == "unknown" and writing:
        if re.search(r"(?i)\b(?:cross[ -]module|cross[ -]service|across\s+(?:(?:multiple|several)\s+)?modules?)\b|跨模块|跨服务", text):
            change_scope = "cross-module"
        elif re.search(r"(?i)\b(?:copy|typo|wording|documentation|readme)\b|文案|错别字|拼写|说明文档", text):
            change_scope = "documentation"
            code_change = False

    risks = []
    for name, pattern in (
        ("security-sensitive behavior", r"(?i)\b(?:auth|authentication|authorization|encryption|permission|credential|injection)\b|鉴权|认证|授权|加密|权限|凭据|注入"),
        ("persistent data migration", r"(?i)\b(?:schema|database|data)\s+migrat|\bmigrat\w*\s+(?:the\s+)?(?:schema|database|data)\b|数据迁移|数据库迁移|表结构迁移"),
        ("concurrency-sensitive behavior", r"(?i)\b(?:race condition|deadlock|concurrency)\b|竞态|死锁|并发"),
    ):
        if ((writing and change_scope != "documentation") or auditing) and re.search(pattern, text):
            risks.append(name)
    for flag in ("security_sensitive", "data_migration", "concurrency_sensitive"):
        if flag in hints and _strict_bool(hints[flag], default=False):
            risks.append(flag)
    multi_agent = _strict_bool(hints.get("multi_agent_integration"), default=False)
    return createApp().resolve_review_policy(ReviewPolicyRequest(
        mode=mode.value, code_change=code_change, change_scope=change_scope,
        risk_signals=tuple(dict.fromkeys(risks)), multi_agent_integration=multi_agent,
        explicit_review=explicit, requested_scope=runtime.get("runtimeReviewScope"),
        reasoning_effort=runtime.get("runtimeReasoningEffort") or runtime.get("deepseekReasoningEffort") or None,
    ))


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    contract: TaskContract
    profile: ExecutionProfile
    topology: TopologyDecision
    plan: PlanVersion
    scheduler_report: SchedulerReport
    delivery: Any
    runtime_snapshot: Mapping[str, Any]
    context_manifests: tuple[ContextManifest, ...]
    reasoning_budget_decisions: tuple[Mapping[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    memory_receipt: Mapping[str, Any] | None = None
    live_capability: Mapping[str, Any] | None = None

    def trace(self) -> dict[str, Any]:
        return {
            "schema_version": "minicode-runtime-trace-v2",
            "contract": self.contract.to_dict(),
            "profile": self.profile.to_dict(),
            "topology_decision": self.topology.to_dict(),
            "plan": self.plan.to_dict(),
            "scheduler": {
                "run_id": self.scheduler_report.run_id,
                "completed": self.scheduler_report.completed,
                "canceled": self.scheduler_report.canceled,
                "claimed_attempts": self.scheduler_report.claimed_attempts,
                "peak_active_workers": self.scheduler_report.peak_active_workers,
                "peak_active_writers": self.scheduler_report.peak_active_writers,
                "active_leases": self.scheduler_report.active_leases,
                "tokens_used": self.scheduler_report.tokens_used,
                "cost_used": self.scheduler_report.cost_used,
                "model_calls": self.scheduler_report.model_calls,
                "tokens_known": self.scheduler_report.tokens_known,
                "cost_known": self.scheduler_report.cost_known,
                "reserved_tokens": self.scheduler_report.reserved_tokens,
                "reserved_cost": self.scheduler_report.reserved_cost,
                "result_node_ids": list(self.scheduler_report.result_node_ids),
                "reasons": list(self.scheduler_report.reasons),
                "unsettled_task_ids": list(self.scheduler_report.unsettled_task_ids),
            },
            "delivery": self.delivery.to_dict(),
            "state": dict(self.runtime_snapshot),
            "context_manifests": [manifest.to_dict() for manifest in self.context_manifests],
            "acceptance_criteria": [
                {"id": criterion_id, "text": text}
                for criterion_id, text in zip(
                    acceptance_criterion_ids(self.contract.acceptance_criteria),
                    self.contract.acceptance_criteria,
                    strict=True,
                )
            ],
            "reasoning_budget_decisions": [
                dict(decision) for decision in self.reasoning_budget_decisions
            ],
            **({"task_memory": dict(self.memory_receipt)} if self.memory_receipt is not None else {}),
            **({"live_capability": deepcopy(dict(self.live_capability))} if self.live_capability is not None else {}),
        }


def _primary_orchestration(runtime: Mapping[str, Any]) -> bool:
    selected = runtime.get("runtimeOrchestration", "legacy")
    if not isinstance(selected, str) or selected not in {"legacy", "primary"}:
        raise ValueError("runtimeOrchestration must be 'legacy' or 'primary'")
    return selected == "primary"


def _durable_consultation(runtime: Mapping[str, Any]) -> bool:
    enabled = runtime.get("runtimeDurableConsultation", False)
    if type(enabled) is not bool:
        raise ValueError("runtimeDurableConsultation must be a boolean")
    if enabled and not _primary_orchestration(runtime):
        raise ValueError("durable consultation requires runtimeOrchestration=primary")
    limit = _positive_limit(runtime, "runtimeMaxContinuations", default=16)
    if limit > 128:
        raise ValueError("runtimeMaxContinuations must be <= 128")
    return enabled


def _repair_limit(runtime: Mapping[str, Any]) -> int:
    limit = runtime.get("runtimeMaxRepairRounds", 0)
    if type(limit) is not int or not 0 <= limit <= 4:
        raise ValueError("runtimeMaxRepairRounds must be an integer between 0 and 4")
    if limit and not _durable_consultation(runtime):
        raise ValueError("candidate repair requires durable primary execution")
    return limit


class ProfileResolver:
    @staticmethod
    def requested_mode(runtime: Mapping[str, Any] | None) -> RequestedMode:
        value = str(
            (runtime or {}).get("executionProfile")
            or (runtime or {}).get("profile")
            or "standard"
        ).strip().lower()
        try:
            return RequestedMode(value)
        except ValueError as error:
            raise ValueError("execution profile must be 'standard', 'deep', or 'ultra'") from error

    def resolve(
        self,
        *,
        runtime: Mapping[str, Any] | None,
        selected_route: SelectedRoute,
        request: str,
        now: float | None = None,
        messages: Iterable[Mapping[str, Any]] = (),
    ) -> ExecutionProfile:
        runtime = runtime or {}
        mode = self.requested_mode(runtime)
        runtime = _runtime_with_review_directive(request, runtime, mode, messages)
        try:
            isolation = WriteIsolation(runtime.get("runtimeWriteIsolation") or (
                "direct" if mode is RequestedMode.STANDARD else "temporary-workspace"))
        except (ValueError, TypeError) as error:
            raise ValueError("runtimeWriteIsolation must be direct or temporary-workspace") from error
        _durable_consultation(runtime)
        _repair_limit(runtime)
        async_agents = runtime.get("runtimeAsyncAgents", False)
        if type(async_agents) is not bool:
            raise ValueError("runtimeAsyncAgents must be a boolean")
        if async_agents and not _durable_consultation(runtime):
            raise ValueError("runtimeAsyncAgents requires durable primary execution")
        agent_messages = runtime.get("runtimeAgentMessages", False)
        if type(agent_messages) is not bool or (agent_messages and not async_agents):
            raise ValueError("runtimeAgentMessages must be a boolean and requires async agents")
        agent_resume = runtime.get("runtimeAgentResume", False)
        if type(agent_resume) is not bool or (agent_resume and not agent_messages):
            raise ValueError("runtimeAgentResume must be a boolean and requires checkpointed agent messages")
        agent_teams = runtime.get("runtimeAgentTeams", False)
        if type(agent_teams) is not bool or (agent_teams and (not agent_resume or self.requested_mode(runtime) is not RequestedMode.ULTRA)):
            raise ValueError("runtimeAgentTeams requires Ultra, async agents, messages and controlled resume")
        agent_fork = runtime.get("runtimeAgentFork", False)
        if type(agent_fork) is not bool or (agent_fork and not agent_messages):
            raise ValueError("runtimeAgentFork requires checkpointed async agents")
        if runtime.get("runtimePromptCache", "off") not in {"off", "auto", "stable-prefix"}:
            raise ValueError("runtimePromptCache must be off, auto or stable-prefix")
        if runtime.get("runtimePrimaryRole", "worker") != "worker" and not async_agents:
            raise ValueError("Coordinator requires primary-led async execution")
        raw_deadline = runtime.get("runtimeDeadline")
        deadline = _finite_deadline(raw_deadline)
        max_tokens = _optional_positive_int(runtime, "runtimeMaxTokens")
        max_cost = _optional_positive_float(runtime, "runtimeMaxCost")
        if _primary_orchestration(runtime):
            from Package.AgentRuntime.Src.Application.Dto.ExecutionPolicy import PolicyRequest
            from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

            preference = runtime.get("runtimeAutonomousDelegation")
            if preference is not None and not isinstance(preference, bool):
                raise ValueError("runtimeAutonomousDelegation must be a boolean")
            # An entry-owned capability, deliberately absent from saved settings
            # and Web request DTOs. A prompt preference is not an admission rule.
            delegation_allowed = runtime.get("_genericDelegationAllowed", True)
            if type(delegation_allowed) is not bool:
                raise ValueError("_genericDelegationAllowed must be a boolean")
            if not delegation_allowed and runtime.get("requiredConsultation") is not None:
                raise ValueError("general delegation is disabled but the task requires consultation")
            if async_agents and not delegation_allowed:
                raise ValueError("async agents require dynamic worker delegation")
            parallel_preference = runtime.get("runtimeParallelCandidates")
            if parallel_preference is not None and type(parallel_preference) is not bool:
                raise ValueError("runtimeParallelCandidates must be a boolean")
            # Ultra can run the primary plus two isolated Builders. Explicit
            # serial/admission restrictions take precedence over this default.
            parallel_candidates = (parallel_preference if parallel_preference is not None else
                mode is RequestedMode.ULTRA and isolation is WriteIsolation.TEMPORARY_WORKSPACE
                and delegation_allowed and preference is not False)
            if parallel_candidates and isolation is WriteIsolation.DIRECT:
                raise ValueError("parallel candidates require temporary-workspace isolation")
            shared_writes = runtime.get("runtimeSharedWorkspaceWrites", (
                mode is RequestedMode.ULTRA and async_agents and isolation is WriteIsolation.DIRECT
                and (not agent_teams or runtime.get("runtimeAgentTeamWrites") is True)))
            if type(shared_writes) is not bool or (shared_writes and (
                mode is not RequestedMode.ULTRA or not async_agents or not delegation_allowed
                or isolation is not WriteIsolation.DIRECT or runtime.get("runtimePrimaryRole", "worker") != "worker"
                or (agent_teams and runtime.get("runtimeAgentTeamWrites") is not True))):
                raise ValueError("shared workspace writers require Ultra async direct execution with worker primary and team write permission")
            collaboration_policy = createApp().resolve_collaboration_mode(runtime, async_enabled=async_agents,
                teams_enabled=agent_teams, fork_enabled=agent_fork, parallel_enabled=parallel_candidates,
                shared_enabled=shared_writes)
            policy = createApp().resolve_policy(PolicyRequest(
                mode=mode.value,
                max_active_workers=(_optional_positive_int(runtime, "runtimeMaxActiveWorkers")
                                    or (3 if parallel_candidates else None)),
                max_agents=_optional_positive_int(runtime, "runtimeMaxAgents"),
                reasoning_effort=(runtime.get("runtimeReasoningEffort") or runtime.get("deepseekReasoningEffort")),
                autonomous_delegation=preference,
            ))
            review = _resolve_review_policy(request, runtime, mode)
            verifier_budget = int(review.scope != "basic")
            if verifier_budget and policy.max_agents < 2:
                if review.required:
                    raise ValueError("independent verification requires runtimeMaxAgents >= 2")
                from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
                review = ReviewPolicy("basic", False, ("optional review omitted: runtimeMaxAgents has no reviewer slot",))
                verifier_budget = 0
            from minicode.runtime_profiles import get_runtime_profile
            primary_runtime_profile = (get_runtime_profile(runtime["runtimeProfile"]).name
                if runtime.get("runtimeProfile") else "single" if mode is RequestedMode.STANDARD else "single-deep")
            max_active_writers = (_optional_positive_int(runtime, "runtimeMaxActiveWriters")
                                  or (3 if parallel_candidates or shared_writes else 1))
            if max_active_writers > 1 and not (parallel_candidates or shared_writes):
                raise ValueError(
                    "runtimeMaxActiveWriters above 1 requires parallel candidates or shared workspace writes")
            if parallel_candidates and not delegation_allowed:
                raise ValueError(
                    "parallel candidates require dynamic worker delegation to be allowed")
            return ExecutionProfile(
                requested_mode=mode,
                selected_route=SelectedRoute.PRIMARY_LED,
                reasoning_budget=policy.reasoning_effort,
                planner_budget=_nonnegative_budget(runtime, "runtimePlannerBudget", default=0),
                verifier_budget=verifier_budget,
                max_active_workers=policy.max_active_workers,
                max_agents=policy.max_agents,
                max_tokens=max_tokens,
                max_cost=max_cost,
                deadline=deadline,
                retry_budget=_nonnegative_budget(runtime, "runtimeRetryBudget", default=0 if mode is RequestedMode.STANDARD else 1),
                max_delegation_depth=policy.max_delegation_depth,
                verification_level=VerificationLevel.LIGHT if mode is RequestedMode.STANDARD else VerificationLevel.STRICT,
                write_isolation=isolation,
                dynamic_worker_delegation=delegation_allowed,
                autonomous_delegation=delegation_allowed and policy.autonomous_delegation,
                parallel_candidates=parallel_candidates,
                shared_workspace_writes=shared_writes,
                max_active_writers=max_active_writers,
                async_agents=async_agents,
                agent_messages=agent_messages,
                agent_resume=agent_resume,
                agent_teams=agent_teams,
                agent_fork=agent_fork,
                coordinator=collaboration_policy["coordinator"],
                agent_team_writes=collaboration_policy["team_writes"],
                primary_runtime_profile=primary_runtime_profile,
                review_policy=review.to_dict(),
                dynamic_review=True,
            )
        if mode is RequestedMode.STANDARD:
            reasoning = runtime.get("runtimeReasoningEffort") or ("medium" if len(request) > 500 else "low")
            return ExecutionProfile(
                requested_mode=mode,
                selected_route=SelectedRoute.STANDARD_SINGLE,
                reasoning_budget=reasoning,
                planner_budget=0,
                verifier_budget=0,
                max_active_workers=1,
                max_agents=_positive_limit(runtime, "runtimeMaxAgents", default=4),
                max_tokens=max_tokens,
                max_cost=max_cost,
                deadline=deadline,
                retry_budget=_nonnegative_budget(runtime, "runtimeRetryBudget", default=0),
                max_delegation_depth=1,
                verification_level=VerificationLevel.LIGHT,
                write_isolation=isolation,
                dynamic_worker_delegation=True,
            )
        hints = runtime.get("topologyHints")
        explicit_review = (runtime["runtimeRequireIndependentVerifier"]
            if "runtimeRequireIndependentVerifier" in runtime else
            hints.get("requires_independent_verification") if isinstance(hints, Mapping) else None)
        review_disabled = explicit_review is not None and not _strict_bool(explicit_review, default=False)
        raw_verifier_budget = runtime.get("runtimeVerifierBudget")
        if review_disabled:
            verifier_budget = 0
        elif raw_verifier_budget is None:
            verifier_budget = 1 if _requires_independent_verifier(request, runtime) else 0
        else:
            verifier_budget = _nonnegative_budget(
                runtime,
                "runtimeVerifierBudget",
                default=0,
            )
        if selected_route is SelectedRoute.ULTRA_READ_FANOUT and not review_disabled:
            verifier_budget = max(1, verifier_budget)
        deep = mode is RequestedMode.DEEP
        max_active_workers = _positive_limit(
            runtime, "runtimeMaxActiveWorkers", default=1 if deep else 4,
        )
        max_agents = _positive_limit(runtime, "runtimeMaxAgents", default=2 if deep else 6)
        return ExecutionProfile(
            requested_mode=mode,
            selected_route=selected_route,
            reasoning_budget=runtime.get("runtimeReasoningEffort") or "high",
            planner_budget=_nonnegative_budget(runtime, "runtimePlannerBudget", default=0),
            verifier_budget=verifier_budget,
            max_active_workers=min(max_active_workers, 1) if deep else max_active_workers,
            max_agents=min(max_agents, 2) if deep else max_agents,
            max_tokens=max_tokens,
            max_cost=max_cost,
            deadline=deadline,
            retry_budget=_nonnegative_budget(runtime, "runtimeRetryBudget", default=1),
            max_delegation_depth=1,
            verification_level=VerificationLevel.STRICT,
            write_isolation=isolation,
            dynamic_worker_delegation=False,
        )


class TopologyResolver:
    """Fixed Standard/Deep routes and auditable adaptive Ultra selection."""

    _STRONG_COUPLING = re.compile(
        r"(?i)root[ -]?cause|race condition|strongly coupled|single invariant|根因|竞态|强耦合|整体迁移"
    )
    _HIGH_WRITE_CONFLICT = re.compile(
        r"(?i)parallel writes?|same files?|merge conflict|高写冲突|并行写|同一文件"
    )

    def decide(
        self,
        *,
        requested_mode: RequestedMode,
        request: str,
        runtime: Mapping[str, Any] | None,
        messages: Iterable[Mapping[str, Any]] = (),
        permission_mode: PermissionMode | None = None,
    ) -> TopologyDecision:
        runtime = runtime or {}
        # This decision also controls the execution owner's tool scope. Full
        # access is an explicit grant, independent of task keywords or a read-only
        # classification. The original request and constraints still govern use.
        if permission_mode is PermissionMode.BYPASS:
            write_required = True
        else:
            write_required = (
                not _workspace_write_forbidden(request, runtime, messages)
                if requested_mode is RequestedMode.STANDARD and "taskRequiresWorkspaceWrite" not in runtime
                else requires_workspace_write(request, runtime, messages)
            )
        if _primary_orchestration(runtime):
            return TopologyDecision(
                requested_profile=requested_mode,
                selected_route=SelectedRoute.PRIMARY_LED,
                independent_subproblems=(),
                shared_context_dependency="unknown",
                write_conflict_risk="none",
                expected_parallel_gain=0.0,
                deterministic_verifiability=True,
                estimated_extra_cost=0.0,
                requires_workspace_write=write_required,
                reasons=("one primary Agent owns the task; helpers are requested during execution",),
                rejected_routes={},
            )
        if requested_mode is RequestedMode.STANDARD:
            return TopologyDecision(
                requested_profile=requested_mode,
                selected_route=SelectedRoute.STANDARD_SINGLE,
                independent_subproblems=(),
                shared_context_dependency="low",
                write_conflict_risk="none",
                expected_parallel_gain=0.0,
                deterministic_verifiability=True,
                estimated_extra_cost=0.0,
                requires_workspace_write=write_required,
                reasons=("Standard always uses the deterministic single-Worker route",),
                rejected_routes={
                    SelectedRoute.DEEP_SINGLE.value: "not requested",
                    SelectedRoute.ULTRA_READ_FANOUT.value: "not requested",
                },
            )

        if requested_mode is RequestedMode.DEEP:
            return TopologyDecision(
                requested_profile=requested_mode,
                selected_route=SelectedRoute.DEEP_SINGLE,
                independent_subproblems=(),
                shared_context_dependency="low",
                write_conflict_risk="none",
                expected_parallel_gain=0.0,
                deterministic_verifiability=True,
                estimated_extra_cost=0.0,
                requires_workspace_write=write_required,
                reasons=("Deep uses one deep Worker with optional sequential verification",),
                rejected_routes={
                    SelectedRoute.STANDARD_SINGLE.value: "Deep requests high-budget execution",
                    SelectedRoute.ULTRA_READ_FANOUT.value: "Deep disables parallel fan-out",
                },
            )

        hints = runtime.get("topologyHints")
        hints = hints if isinstance(hints, Mapping) else {}
        explicit = runtime.get("ultraSubproblems")
        subproblems = tuple(
            str(item).strip()
            for item in explicit
            if str(item).strip()
        ) if isinstance(explicit, (list, tuple)) else self._extract_subproblems(request)
        shared = str(hints.get("shared_context_dependency", "high" if self._STRONG_COUPLING.search(request) else "low")).lower()
        conflict = str(hints.get("write_conflict_risk", "high" if self._HIGH_WRITE_CONFLICT.search(request) else "none")).lower()
        deterministic = _strict_bool(
            hints.get("deterministic_verifiability"),
            default=True,
        )
        gain = _topology_score(
            hints.get("expected_parallel_gain"),
            default=0.35 if len(subproblems) >= 2 else 0.0,
            invalid=0.0,
        )
        extra_cost = _topology_score(
            hints.get("estimated_extra_cost"),
            default=0.10 if len(subproblems) >= 2 else 0.0,
            invalid=1.0,
        )
        max_agents = _positive_limit(runtime, "runtimeMaxAgents", default=6)
        max_active_workers = _positive_limit(
            runtime,
            "runtimeMaxActiveWorkers",
            default=4,
        )
        max_tokens = _optional_positive_int(runtime, "runtimeMaxTokens")

        checks = {
            "at least two independent subproblems": len(subproblems) >= 2,
            "subproblems have explicit objectives": all(len(item) >= 3 for item in subproblems),
            "results are independently verifiable": deterministic,
            "parallel work has material gain": gain >= 0.20,
            "shared context dependency is low": shared == "low",
            "no unisolated parallel writes": conflict in {"none", "low"},
            "gain exceeds coordination cost": gain > extra_cost,
            "agent/token budget permits fan-out": max_agents >= len(subproblems) + 2 and (max_tokens is None or max_tokens >= 4_000 * len(subproblems)),
            "runtimeMaxActiveWorkers >= 2 (otherwise fan-out has no parallel benefit)": max_active_workers >= 2,
        }
        failed = [name for name, passed in checks.items() if not passed]
        if not failed:
            return TopologyDecision(
                requested_profile=requested_mode,
                selected_route=SelectedRoute.ULTRA_READ_FANOUT,
                independent_subproblems=subproblems,
                shared_context_dependency=shared,
                write_conflict_risk=conflict,
                expected_parallel_gain=gain,
                deterministic_verifiability=deterministic,
                estimated_extra_cost=extra_cost,
                requires_workspace_write=write_required,
                reasons=tuple(name for name, passed in checks.items() if passed),
                rejected_routes={SelectedRoute.DEEP_SINGLE.value: "fan-out passed every safety/benefit condition"},
            )
        return TopologyDecision(
            requested_profile=requested_mode,
            selected_route=SelectedRoute.DEEP_SINGLE,
            independent_subproblems=subproblems,
            shared_context_dependency=shared,
            write_conflict_risk=conflict,
            expected_parallel_gain=gain,
            deterministic_verifiability=deterministic,
            estimated_extra_cost=extra_cost,
            requires_workspace_write=write_required,
            reasons=("Ultra selected deep-single because: " + "; ".join(failed),),
            rejected_routes={SelectedRoute.ULTRA_READ_FANOUT.value: "; ".join(failed)},
        )

    @staticmethod
    def _extract_subproblems(request: str) -> tuple[str, ...]:
        if not re.search(r"(?i)\bindependen(?:t|tly)\b|互不依赖|相互独立|独立(?:检查|审查|分析)", request):
            return ()
        if re.search(r"(?i)\b(?:then|after|depends?\s+on|based\s+on|using\s+the\s+.+\s+above)\b|上一步|然后|再根据|先.+再", request):
            return ()
        candidates: list[str] = []
        for line in request.splitlines():
            match = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$", line)
            if match:
                item = match.group(1).strip()
                if item and len(item) >= 8:
                    candidates.append(item)
        # Avoid treating a long implementation checklist as independently
        # verifiable unless the caller provides explicit topology hints.
        return tuple(candidates[:4]) if 2 <= len(candidates) <= 4 else ()


class PlanCompiler:
    def compile(
        self,
        contract: TaskContract,
        profile: ExecutionProfile,
        topology: TopologyDecision,
    ) -> PlanVersion:
        retry_attempts = max(1, profile.retry_budget + 1)
        read = (ToolEffect.READ_ONLY,)
        # Mutating execution nodes have broader effects. Network reads belong
        # to the read scope; they never require external mutation authority.
        write = (
            ToolEffect.READ_ONLY,
            ToolEffect.WORKSPACE_WRITE,
            ToolEffect.EXTERNAL_SIDE_EFFECT,
            ToolEffect.PRIVILEGED,
        )
        nodes: tuple[PlanNode, ...]
        if profile.primary_led:
            primary = PlanNode(
                node_id="primary",
                objective=contract.envelope.request,
                role="primary",
                allowed_effects=write if topology.requires_workspace_write else read,
                acceptance_criteria=contract.acceptance_criteria,
                max_attempts=retry_attempts,
                runtime_profile=profile.primary_runtime_profile,
                review_policy=profile.review_policy,
            )
            nodes = (primary,)
            if profile.verifier_budget:
                nodes += (PlanNode(
                    node_id="final-verifier",
                    objective=("Independently verify the delivered candidate for:\n" if profile.review_policy is None else
                               ("Perform a focused review of the changed behavior and its acceptance evidence for:\n"
                                if profile.review_policy["scope"] == "focused" else
                                "Review the delivered behavior, affected module boundaries and integration evidence for:\n"))
                              + contract.envelope.request,
                    role="verifier",
                    dependencies=(primary.node_id,),
                    allowed_effects=read,
                    acceptance_criteria=contract.acceptance_criteria or ("verify the delivered result addresses the original request",),
                    max_attempts=retry_attempts if profile.review_policy is None or profile.review_policy["required"] else 1,
                    review_policy=profile.review_policy,
                ),)
        elif topology.selected_route is SelectedRoute.STANDARD_SINGLE:
            nodes = (
                PlanNode(
                    node_id="root",
                    objective=contract.envelope.request,
                    role="standard",
                    allowed_effects=write if topology.requires_workspace_write else read,
                    acceptance_criteria=contract.acceptance_criteria,
                    max_attempts=retry_attempts,
                ),
            )
        elif topology.selected_route is SelectedRoute.DEEP_SINGLE:
            deep_worker = PlanNode(
                node_id="deep-worker",
                objective=contract.envelope.request,
                role="deep",
                allowed_effects=(write if topology.requires_workspace_write else read),
                acceptance_criteria=contract.acceptance_criteria,
                max_attempts=retry_attempts,
            )
            nodes = (deep_worker,) if profile.max_agents < 2 or profile.verifier_budget == 0 else (
                deep_worker,
                PlanNode(
                    node_id="final-verifier",
                    objective=(
                        "Independently verify the integrated result for this original request:\n"
                        + contract.envelope.request
                    ),
                    role="verifier",
                    dependencies=("deep-worker",),
                    allowed_effects=read,
                    acceptance_criteria=(
                        contract.acceptance_criteria
                        or ("verify the delivered result addresses the original request",)
                    ),
                    max_attempts=retry_attempts,
                ),
            )
        else:
            explorers = tuple(
                PlanNode(
                    node_id=f"explorer-{index}",
                    objective=objective,
                    role="explorer" if index % 2 else "reviewer",
                    allowed_effects=read,
                    acceptance_criteria=("return independently verifiable read-only evidence",),
                    max_attempts=retry_attempts,
                )
                for index, objective in enumerate(topology.independent_subproblems, start=1)
            )
            dependencies = tuple(node.node_id for node in explorers)
            aggregator = PlanNode(
                node_id=("exclusive-writer" if topology.requires_workspace_write else "read-synthesizer"),
                objective=contract.envelope.request,
                role=("writer" if topology.requires_workspace_write else "synthesizer"),
                dependencies=dependencies,
                allowed_effects=(write if topology.requires_workspace_write else read),
                acceptance_criteria=contract.acceptance_criteria,
                max_attempts=retry_attempts,
            )
            nodes = (*explorers, aggregator)
            if profile.verifier_budget:
                nodes += (PlanNode(
                    node_id="final-verifier",
                    objective=(
                        "Independently verify the integrated result for this original request:\n"
                        + contract.envelope.request
                    ),
                    role="verifier",
                    dependencies=(aggregator.node_id,),
                    allowed_effects=read,
                    acceptance_criteria=(
                        contract.acceptance_criteria
                        or ("verify the delivered result addresses the original request",)
                    ),
                    max_attempts=retry_attempts,
                ),)
        return PlanVersion(
            run_id=contract.envelope.run_id,
            plan_version=1,
            contract_revision=contract.contract_revision,
            nodes=nodes,
        )


class PlanValidator:
    def validate(
        self,
        plan: PlanVersion,
        profile: ExecutionProfile,
        *,
        requires_workspace_write: bool | None = None,
        builder_registrations: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        if not plan.nodes:
            raise PlanValidationError("PlanGraph must contain at least one PlanNode")
        for node in plan.nodes:
            if node.criticality not in {"required", "advisory"} or (node.criticality == "advisory" and (
                not profile.primary_led or not node.node_id.startswith("consult-")
                or node.role not in {"explorer", "planner"} or node.dependencies
                or node.allowed_effects != (ToolEffect.READ_ONLY,)
            )):
                raise PlanValidationError("only independent primary-owned exploration/planning advice may be advisory")
            if any(dependency.criticality == "advisory" and dependency.node_id in node.dependencies for dependency in plan.nodes):
                raise PlanValidationError("required DAG dependencies cannot be advisory consultations")
        if profile.primary_led:
            from minicode.runtime_protocol import validate_review_plan
            try:
                validate_review_plan(plan.nodes)
            except ValueError as error:
                raise PlanValidationError(str(error)) from error
            self._validate_primary_plan(plan, profile, requires_workspace_write, builder_registrations)
            return
        if (
            profile.requested_mode is RequestedMode.STANDARD
            and profile.selected_route is not SelectedRoute.STANDARD_SINGLE
        ):
            raise PlanValidationError("Standard profile must use standard-single")
        if profile.requested_mode is RequestedMode.DEEP:
            if profile.selected_route is not SelectedRoute.DEEP_SINGLE:
                raise PlanValidationError("Deep profile must use deep-single")
            if profile.max_active_workers != 1 or profile.dynamic_worker_delegation:
                raise PlanValidationError("Deep requires serial execution without dynamic delegation")
        if (
            profile.requested_mode is RequestedMode.ULTRA
            and profile.selected_route is SelectedRoute.STANDARD_SINGLE
        ):
            raise PlanValidationError("Ultra profile may not use standard-single")
        ids = [node.node_id for node in plan.nodes]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("PlanNode ids must be unique")
        known = set(ids)
        for node in plan.nodes:
            missing = set(node.dependencies) - known
            if missing:
                raise PlanValidationError(f"node {node.node_id} has missing dependencies: {sorted(missing)}")
            if node.role in {"explorer", "reviewer", "verifier"} and ToolEffect.WORKSPACE_WRITE in node.allowed_effects:
                raise PlanValidationError(
                    f"{node.role} node must be read-only and must not write the workspace"
                )
        self._assert_acyclic(plan)
        writers = [node for node in plan.nodes if node.is_writer]
        if requires_workspace_write is True and not writers:
            raise PlanValidationError(
                "write-capable topology requires exactly one logical Writer"
            )
        if requires_workspace_write is False:
            unsafe_nodes = [
                node.node_id
                for node in plan.nodes
                if any(effect is not ToolEffect.READ_ONLY for effect in node.allowed_effects)
            ]
            if unsafe_nodes:
                raise PlanValidationError(
                    "read-only topology contains non-read-only capabilities on nodes: "
                    f"{unsafe_nodes}"
                )
        if profile.requested_mode in {RequestedMode.DEEP, RequestedMode.ULTRA} and len(writers) > 1:
            raise PlanValidationError(
                "Deep and Ultra require exactly one logical Writer for write-capable plans "
                "and permits none for read-only plans"
            )
        if profile.selected_route is SelectedRoute.ULTRA_READ_FANOUT:
            explorers = [node for node in plan.nodes if node.role in {"explorer", "reviewer"}]
            if len(explorers) < 2:
                raise PlanValidationError("ultra-read-fanout requires at least two read-only nodes")
            aggregators = [
                node for node in plan.nodes if node.role in {"writer", "synthesizer"}
            ]
            if len(aggregators) != 1:
                raise PlanValidationError("fan-out requires exactly one result aggregator")
            aggregator = aggregators[0]
            explorer_ids = {node.node_id for node in explorers}
            if not explorer_ids.issubset(set(aggregator.dependencies)):
                raise PlanValidationError("fan-out aggregator must depend on every read-only explorer")
            verifiers = [node for node in plan.nodes if node.role == "verifier"]
            if (len(verifiers) != int(profile.verifier_budget > 0)
                or (verifiers and aggregator.node_id not in verifiers[0].dependencies)):
                raise PlanValidationError("fan-out must retain its admitted optional Verifier after the aggregator")
        if profile.selected_route is SelectedRoute.STANDARD_SINGLE:
            roots = [
                node
                for node in plan.nodes
                if node.node_id == "root" and node.role == "standard"
            ]
            unexpected = [
                node.node_id
                for node in plan.nodes
                if node not in roots and not node.node_id.startswith("delegated-")
            ]
            if len(roots) != 1 or unexpected:
                raise PlanValidationError(
                    "standard-single requires exactly one root PlanNode and only "
                    "governed delegated nodes"
                )
        if profile.requested_mode is RequestedMode.DEEP:
            workers = [node for node in plan.nodes if node.role == "deep"]
            verifiers = [node for node in plan.nodes if node.role == "verifier"]
            expected_verifiers = int(profile.max_agents >= 2 and profile.verifier_budget > 0)
            if (
                len(workers) != 1
                or workers[0].dependencies
                or len(verifiers) != expected_verifiers
                or len(plan.nodes) != 1 + expected_verifiers
            ):
                raise PlanValidationError("Deep requires one deep Worker and its optional Verifier")
            if verifiers and (
                verifiers[0].dependencies != (workers[0].node_id,)
                or any(effect is not ToolEffect.READ_ONLY for effect in verifiers[0].allowed_effects)
            ):
                raise PlanValidationError("Deep Verifier must be read-only and depend on the deep Worker")
        if len(plan.nodes) > profile.max_agents:
            raise PlanValidationError("Plan exceeds max_agents")

    @staticmethod
    def _validate_primary_plan(
        plan: PlanVersion,
        profile: ExecutionProfile,
        requires_workspace_write: bool | None,
        builder_registrations: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        ids = {node.node_id for node in plan.nodes}
        if len(ids) != len(plan.nodes):
            raise PlanValidationError("PlanNode ids must be unique")
        primaries = [node for node in plan.nodes if node.role == "primary"]
        if len(primaries) != 1 or primaries[0].node_id != "primary":
            raise PlanValidationError("primary-led execution requires one primary owner")
        if profile.review_policy is not None and (
            primaries[0].review_policy != profile.review_policy
            or primaries[0].runtime_profile != profile.primary_runtime_profile
            or profile.verifier_budget != int(profile.review_policy["scope"] != "basic")
        ):
            raise PlanValidationError("primary assignment must retain the admitted execution depth and review policy")
        for node in plan.nodes:
            if set(node.dependencies) - ids:
                raise PlanValidationError(f"node {node.node_id} has missing dependencies")
            if node.max_attempts < 1 or not node.allowed_effects:
                raise PlanValidationError("nodes require bounded attempts and explicit capabilities")
            registered_builder = (profile.parallel_candidates and node.node_id.startswith("builder-")
                and node.role == "writer" and node.allowed_effects == (ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE)
                and any(item.get("delegated") is True and item.get("creation_parent") == "primary"
                    and item.get("definition") == node.to_dict() for item in builder_registrations))
            registered_shared = (profile.shared_workspace_writes and node.node_id.startswith("agent-")
                and node.role == "shared_writer" and bool(node.write_scope)
                and node.allowed_effects == (ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE)
                and any(item.get("delegated") is True and item.get("creation_parent") == "primary"
                    and item.get("definition") == node.to_dict() for item in builder_registrations))
            if node.role != "primary" and not registered_builder and not registered_shared and (
                node.role not in {"explorer", "planner", "reviewer", "verifier", "synthesizer"}
                or any(effect is not ToolEffect.READ_ONLY for effect in node.allowed_effects)
            ):
                raise PlanValidationError("primary-led helpers must have an allowed read-only role")
        PlanValidator._assert_acyclic(plan)
        if requires_workspace_write is True and not primaries[0].is_writer:
            raise PlanValidationError("write-capable task requires its primary Writer")
        if requires_workspace_write is False and any(
            effect is not ToolEffect.READ_ONLY for node in plan.nodes for effect in node.allowed_effects
        ):
            raise PlanValidationError("read-only task contains effectful capabilities")
        if len(plan.nodes) > profile.max_agents:
            raise PlanValidationError("Plan exceeds max_agents")

    @staticmethod
    def _assert_acyclic(plan: PlanVersion) -> None:
        edges = {node.node_id: set(node.dependencies) for node in plan.nodes}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise PlanValidationError("PlanGraph contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in edges[node_id]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in edges:
            visit(node_id)


class RunCoordinator:
    def __init__(self, *, artifact_store: RuntimeArtifactStore | None = None) -> None:
        self.artifact_store = artifact_store or RuntimeArtifactStore()

    def run(
        self,
        *,
        contract: TaskContract,
        profile: ExecutionProfile,
        topology: TopologyDecision,
        plan: PlanVersion,
        model: Any,
        tools: ToolRegistry,
        messages: list[dict[str, Any]],
        cwd: str,
        runtime: dict[str, Any],
        permissions: Any | None,
        session: Any | None,
        agent_turn_kwargs: dict[str, Any],
    ) -> RuntimeOutcome:
        PlanValidator().validate(
            plan,
            profile,
            requires_workspace_write=topology.requires_workspace_write,
        )
        amended_plan = runtime.get("_amendedPlan")
        raw_runtime_commands = runtime.get("_runtimeCommands", ())
        if not isinstance(raw_runtime_commands, (list, tuple)):
            raise TypeError("_runtimeCommands must be a list or tuple")
        runtime_command_plans = tuple(
            candidate
            for pending_command in raw_runtime_commands
            if isinstance(pending_command, RuntimeCommand)
            for candidate in (pending_command.payload.get("plan"),)
            if isinstance(candidate, PlanVersion)
        )
        contains_writer = any(node.is_writer for node in plan.nodes) or (
            isinstance(amended_plan, PlanVersion)
            and any(node.is_writer for node in amended_plan.nodes)
        ) or any(
            node.is_writer
            for command_plan in runtime_command_plans
            for node in command_plan.nodes
        )
        if contains_writer:
            lock_root = (
                Path(str(runtime["runtimeLockRoot"]))
                if runtime.get("runtimeLockRoot")
                else None
            )
            ownership = WorkspaceWriteLease(
                cwd,
                run_id=contract.envelope.run_id,
                lock_root=lock_root,
            )
        else:
            ownership = nullcontext()
        # The DURABLE state file has exactly one authoritative writer: this run.
        # Holding the claim makes "a live process owns this state" an executable
        # fact for every operator surface, instead of a comment asking callers to
        # pass the real Scheduler. It is released when the run really ends, and it
        # lives outside the user's workspace so it never becomes part of one.
        execution_drain = ExecutionDrain()
        with execution_drain.ownership(ownership, RuntimeRunOwnerLease(
            _runtime_state_path(runtime, contract.envelope.run_id),
            run_id=contract.envelope.run_id,
            lock_root=runtime.get("runtimeLockRoot"),
        )) as (_, run_owner):
            return self._run_unlocked(
                contract=contract,
                profile=profile,
                topology=topology,
                plan=plan,
                model=model,
                tools=tools,
                messages=messages,
                cwd=cwd,
                runtime=runtime,
                permissions=permissions,
                session=session,
                agent_turn_kwargs=agent_turn_kwargs,
                owner_lease=run_owner,
                execution_drain=execution_drain,
            )

    def _run_unlocked(
        self,
        *,
        contract: TaskContract,
        profile: ExecutionProfile,
        topology: TopologyDecision,
        plan: PlanVersion,
        model: Any,
        tools: ToolRegistry,
        messages: list[dict[str, Any]],
        cwd: str,
        runtime: dict[str, Any],
        permissions: Any | None,
        session: Any | None,
        agent_turn_kwargs: dict[str, Any],
        owner_lease: RuntimeRunOwnerLease,
        execution_drain: ExecutionDrain,
    ) -> RuntimeOutcome:
        from minicode.runtime_stage_context import stage_context_enabled

        stage_context_enabled(runtime)  # Validate before state creation; non-private policy is identity-bound.
        identity = _build_run_identity(
            contract=contract,
            profile=profile,
            topology=topology,
            messages=messages,
            cwd=cwd,
            runtime=runtime,
        )
        state_path = _runtime_state_path(runtime, contract.envelope.run_id)
        amendment = runtime.get("_taskAmendment")
        amended_plan = runtime.get("_amendedPlan")
        state_store = RuntimeStateStore(state_path, owner_lease=owner_lease)
        resume_requested = _as_bool(runtime.get("runtimeResume", False))
        if resume_requested:
            persisted = state_store.load_required()
            try:
                reducer = RuntimeReducer.restore(persisted, store=state_store)
            except RuntimeStateLoadError:
                raise
            except Exception as error:
                raise RuntimeStateLoadError(
                    f"Runtime resume state is structurally invalid: {error}"
                ) from error
            if reducer.contract is None or reducer.identity is None:
                raise RuntimeStateLoadError(
                    "Runtime snapshot is missing TaskContract or RunIdentity"
                )
            if reducer.continuations_enabled != _durable_consultation(runtime):
                raise RuntimeStateLoadError("Runtime continuation schema does not match the requested policy")
            if reducer.repair_limit != _repair_limit(runtime):
                raise RuntimeStateLoadError("Runtime repair policy does not match the persisted run")
            expected_revision = (
                amendment.base_contract_revision
                if isinstance(amendment, TaskAmendment)
                else contract.contract_revision
            )
            if reducer.snapshot()["contract_revision"] != expected_revision:
                raise RuntimeStateLoadError(
                    "persisted Runtime contract revision does not match TaskContract"
                )
            if profile.primary_led:
                # Only frozen scope/depth fields are recovered. Settings and
                # capabilities still have to match the persisted RunIdentity.
                frozen_primary = next(n for n in reducer.current_plan.nodes if n.node_id == "primary")
                frozen_policy = reducer.admitted_review_policy
                frozen_budget = int(frozen_policy["scope"] != "basic") if frozen_policy else int(any(
                    n.node_id == "final-verifier" for n in reducer.current_plan.nodes))
                if frozen_policy is None:
                    frozen_budget = _nonnegative_budget(runtime, "runtimeVerifierBudget", default=frozen_budget)
                profile = replace(profile, primary_runtime_profile=frozen_primary.runtime_profile,
                                  review_policy=frozen_policy, verifier_budget=frozen_budget,
                                  dynamic_review=reducer.dynamic_review_enabled)
                identity = _build_run_identity(contract=contract, profile=profile,
                    topology=topology, messages=messages, cwd=cwd, runtime=runtime)
            if (profile.parallel_candidates and runtime.get("runtimeParallelCandidates") is None
                and profile.requested_mode is RequestedMode.ULTRA
                and identity.profile_hash != reducer.identity.profile_hash):
                # Only recover the exact historical default profile. All other
                # identity fields and contract checks still run unchanged. A
                # resumed serial task must not gain parallel write authority.
                historical = ProfileResolver().resolve(
                    runtime={**runtime, "runtimeParallelCandidates": False},
                    selected_route=topology.selected_route, request=contract.envelope.request)
                historical = replace(historical, primary_runtime_profile=profile.primary_runtime_profile,
                                     review_policy=profile.review_policy, verifier_budget=profile.verifier_budget,
                                     dynamic_review=profile.dynamic_review)
                historical_identity = _build_run_identity(contract=contract, profile=historical,
                    topology=topology, messages=messages, cwd=cwd, runtime=runtime)
                if historical_identity.profile_hash == reducer.identity.profile_hash:
                    profile, identity = historical, historical_identity
            if (profile.shared_workspace_writes and not reducer.shared_workspace_writes_enabled
                and identity.profile_hash != reducer.identity.profile_hash):
                # The new direct-write default must never upgrade a persisted
                # read-only delegation policy. Accept only its exact old identity.
                historical = replace(profile, shared_workspace_writes=False, max_active_writers=1,
                                     agent_team_writes=False)
                historical_identity = _build_run_identity(contract=contract, profile=historical,
                    topology=topology, messages=messages, cwd=cwd, runtime=runtime)
                if historical_identity.profile_hash == reducer.identity.profile_hash:
                    profile, identity = historical, historical_identity
            _validate_resume_identity(
                persisted=reducer.identity,
                current=identity,
            )
            _validate_resume_contract(
                persisted=reducer.contract,
                current=contract,
                amendment=amendment if isinstance(amendment, TaskAmendment) else None,
            )
        else:
            from minicode.agent_collaboration import resolve_policy as resolve_agent_collaboration
            collaboration = resolve_agent_collaboration(runtime, enabled=profile.agent_messages, max_agents=profile.max_agents)
            state_store.reserve_new()
            reducer = RuntimeReducer(
                plan,
                store=state_store,
                contract=contract,
                identity=identity,
                continuations_enabled=_durable_consultation(runtime),
                advisory_enabled=_durable_consultation(runtime),
                closure_reserve_enabled=_durable_consultation(runtime),
                # New bounded primary runs share the protected run pool. Resume
                # uses the persisted schema instead; v3-v8 never gain credit.
                shared_consultation_budget=(_durable_consultation(runtime) and contract.budget.max_tokens is not None),
                async_agents_enabled=profile.async_agents,
                shared_workspace_writes_enabled=profile.shared_workspace_writes,
                agent_messages_enabled=profile.agent_messages,
                agent_resume_enabled=profile.agent_resume,
                agent_teams_enabled=profile.agent_teams,
                agent_fork_enabled=profile.agent_fork,
                coordinator_enabled=profile.coordinator,
                agent_team_writes_enabled=profile.agent_team_writes,
                agent_collaboration=collaboration,
                continuation_limit=_positive_limit(runtime, "runtimeMaxContinuations", default=16),
                repair_limit=_repair_limit(runtime),
                candidate_first=(profile.primary_led and _durable_consultation(runtime)
                                 and profile.write_isolation is WriteIsolation.TEMPORARY_WORKSPACE
                                 and any(node.is_writer for node in plan.nodes)),
                review_baseline=workspace_snapshot(Path(cwd)) if profile.dynamic_review else None,
                review_reasoning_effort=runtime.get("runtimeReasoningEffort") or runtime.get("deepseekReasoningEffort") or None,
                review_max_agents=profile.max_agents,
            )

        owner_lease.bind(reducer)
        validation_profile = (replace(profile, review_policy=reducer.effective_review_policy,
            verifier_budget=int(reducer.effective_review_policy["scope"] != "basic"))
            if reducer.dynamic_review_enabled else profile)
        if (runtime.get("webRunId") and session is not None and getattr(session, "session_id", None)
            and not runtime.get("runtimeResume")):
            # A killed process cannot execute _record/finally. Save the native
            # run identity and requirements before admitting its first worker.
            from minicode.session import save_session
            from minicode.web_run_continuation import retain_continuation_settings
            trace = {"schema_version": "minicode-runtime-trace-v2", "recovery_pending": True,
                "web_run_id": runtime.get("webRunId", contract.envelope.run_id),
                "contract": contract.to_dict(), "profile": profile.to_dict(),
                "topology_decision": topology.to_dict(), "plan": plan.to_dict(),
                "state": reducer.snapshot(), "scheduler": {"run_id": contract.envelope.run_id, "completed": False},
                "delivery": {"run_id": contract.envelope.run_id, "completed": False},
                "context_manifests": [], "reasoning_budget_decisions": [],
                "acceptance_criteria": [{"id": key, "text": text} for key, text in zip(
                    acceptance_criterion_ids(contract.acceptance_criteria), contract.acceptance_criteria, strict=True)]}
            retain_continuation_settings(trace, runtime)
            session.runtime_runs.append(trace)
            save_session(session, force_full=True)
            runtime["_crashRecoveryEnabled"] = True
        if amendment is not None or amended_plan is not None:
            if not isinstance(amendment, TaskAmendment) or not isinstance(amended_plan, PlanVersion):
                raise TypeError("_taskAmendment and _amendedPlan must be supplied together")
            if contract.contract_revision != amendment.next_contract_revision:
                raise ValueError("TaskContract does not match amendment next revision")
            PlanValidator().validate(
                amended_plan,
                validation_profile,
                requires_workspace_write=topology.requires_workspace_write,
            )
            reducer.install_plan(
                amended_plan,
                amendment,
                contract=contract,
                identity=cast(RuntimeRunIdentity, reducer.identity),
            )
        for pending_command in runtime.get("_runtimeCommands", ()):
            if not isinstance(pending_command, RuntimeCommand):
                raise TypeError("_runtimeCommands entries must be RuntimeCommand objects")
            if pending_command.command_type is RuntimeCommandType.AMEND_TASK:
                command_plan = pending_command.payload.get("plan")
                if not isinstance(command_plan, PlanVersion):
                    raise TypeError("AmendTask RuntimeCommand must include a PlanVersion")
                PlanValidator().validate(
                    command_plan,
                    validation_profile,
                    requires_workspace_write=topology.requires_workspace_write,
                )
            reducer.apply(pending_command)
            if pending_command.command_type is RuntimeCommandType.AMEND_TASK:
                updated_contract = reducer.contract
                if updated_contract is None:
                    raise RuntimeStateLoadError(
                        "AmendTask did not persist the updated TaskContract"
                    )
                contract = updated_contract
        PlanValidator().validate(
            reducer.current_plan,
            validation_profile,
            requires_workspace_write=topology.requires_workspace_write,
            builder_registrations=tuple(event["payload"] for event in reducer.snapshot()["events"]
                if event["event_type"] == "NodeReady"),
        )
        injected_cancellation = runtime.get("_cancellationEvent")
        cancellation = (
            injected_cancellation
            if callable(getattr(injected_cancellation, "is_set", None))
            and callable(getattr(injected_cancellation, "set", None))
            else threading.Event()
        )
        from minicode.runtime_activity import ActivityPublisher

        activity = ActivityPublisher(
            reducer, profile, topology,
            getattr(agent_turn_kwargs.get("callbacks"), "on_execution_snapshot", None),
        )
        activity.publish("preparing")
        scheduler = TaskScheduler(reducer, profile, cancellation_event=cancellation,
                                  on_activity=activity.publish, execution_drain=execution_drain)
        usage_budget = RunUsageBudget(reducer, profile.budget, cancellation, runtime)

        reasoning_decisions: list[dict[str, Any]] = []
        reasoning_decisions_lock = threading.Lock()
        adapter_runtime = getattr(model, "runtime", {})
        model_name = str(
            runtime.get("model")
            or getattr(model, "model_id", "")
            or (adapter_runtime.get("model", "") if isinstance(adapter_runtime, dict) else "")
        )
        _standard_runtime, standard_budget_decision = apply_reasoning_budget(
            model=model_name,
            runtime=runtime,
            requested=profile.reasoning_budget,
        )
        if profile.selected_route is SelectedRoute.STANDARD_SINGLE or profile.primary_led:
            if (
                standard_budget_decision.applied is not None
                and not apply_budget_to_existing_adapter(model, standard_budget_decision,
                    reasoning_owner=_standard_runtime.get("_runtimeReasoningEffort"))
            ):
                standard_budget_decision = replace(
                    standard_budget_decision,
                    applied=None,
                    degraded=True,
                    reason="existing model adapter cannot apply the declared reasoning budget",
                )
            reasoning_decisions.append(
                {"node_id": "primary" if profile.primary_led else "root", **standard_budget_decision.to_dict()}
            )

        def model_factory(
            worker_tools: ToolRegistry,
            invocation: Any,
        ):
            configured_runtime, budget_decision = apply_reasoning_budget(
                model=model_name,
                runtime=runtime,
                requested=str(invocation.reasoning_budget),
            )
            injected_factory = runtime.get("_workerModelFactory")
            if callable(injected_factory):
                worker_model = injected_factory(worker_tools)
                if (
                    budget_decision.applied is not None
                    and not apply_budget_to_existing_adapter(worker_model, budget_decision,
                        reasoning_owner=configured_runtime.get("_runtimeReasoningEffort"))
                ):
                    budget_decision = replace(
                        budget_decision,
                        applied=None,
                        degraded=True,
                        reason=(
                            "injected Worker model cannot apply the declared "
                            "reasoning budget"
                        ),
                    )
                with reasoning_decisions_lock:
                    reasoning_decisions.append(
                        {
                            "node_id": str(invocation.node_id),
                            **budget_decision.to_dict(),
                        }
                    )
                return worker_model
            # Web/test mock mode is selected when the root adapter is created;
            # it is not a provider setting in the Runtime mapping.  Preserve
            # that execution mode for non-Standard Workers as well.
            from minicode.mock_model import MockModelAdapter

            if isinstance(model, MockModelAdapter):
                if budget_decision.applied is not None:
                    budget_decision = replace(
                        budget_decision,
                        applied=None,
                        degraded=True,
                        reason="mock model does not apply provider reasoning budgets",
                    )
                with reasoning_decisions_lock:
                    reasoning_decisions.append(
                        {
                            "node_id": str(invocation.node_id),
                            **budget_decision.to_dict(),
                        }
                    )
                return MockModelAdapter()
            from minicode.model_registry import create_model_adapter

            worker_model = create_model_adapter(
                model=model_name,
                tools=worker_tools,
                runtime=configured_runtime,
            )
            with reasoning_decisions_lock:
                reasoning_decisions.append(
                    {
                        "node_id": str(invocation.node_id),
                        **budget_decision.to_dict(),
                    }
                )
            return worker_model

        runtime_context = SimpleNamespace(delegation_gateway=None)
        runtime["_runtimeAutonomousDelegation"] = profile.autonomous_delegation
        runtime["_runtimePrimaryProfile"] = profile.primary_runtime_profile
        runtime["_runtimeReviewPolicy"] = profile.review_policy
        if profile.primary_led:
            runtime["_genericDelegationAllowed"] = profile.dynamic_worker_delegation
            # The resolved (or restored) profile also owns Worker tool assembly;
            # omitted preferences must not hide a capability enabled by Ultra.
            runtime["_runtimeParallelCandidates"] = profile.parallel_candidates
        runtime["_taskContextConstraints"] = contract.constraints
        runtime["_memoryTaskContract"] = {
            "request": contract.envelope.request,
            "constraints": contract.constraints,
            "acceptance_criteria": contract.acceptance_criteria,
            "expected_outputs": contract.expected_outputs,
            "contract_revision": contract.contract_revision,
            "user_instructions": [str(message.get("content", "")) for message in messages
                                  if is_user_input(message) and message.get("content")],
        }
        nodes_by_id = {node.node_id: node for node in reducer.current_plan.nodes}
        depth_cache: dict[str, int] = {}

        def node_depth(node_id: str) -> int:
            cached = depth_cache.get(node_id)
            if cached is not None:
                return cached
            dependencies = nodes_by_id[node_id].dependencies
            depth = (
                0
                if not dependencies
                else 1 + max(node_depth(dependency) for dependency in dependencies)
            )
            depth_cache[node_id] = depth
            return depth

        layer_widths: dict[int, int] = {}
        for planned_node in reducer.current_plan.nodes:
            depth = node_depth(planned_node.node_id)
            layer_widths[depth] = layer_widths.get(depth, 0) + 1
        runtime["_memoryParallelSubtasks"] = max(layer_widths.values(), default=1)
        runtime["_memoryRestartRecoveryRequired"] = _as_bool(
            runtime.get("runtimeResume", False)
        )
        runtime["_memoryAuditRequired"] = (
            profile.verification_level is VerificationLevel.STRICT
        )
        worker = AgentLoopWorkerRunner(
            model=model,
            tools=tools,
            messages=messages,
            cwd=cwd,
            runtime=runtime,
            permissions=permissions,
            session=session,
            artifact_store=self.artifact_store,
            cancellation_event=cancellation,
            model_factory=model_factory,
            runtime_context=runtime_context,
            isolate_writes=profile.write_isolation is WriteIsolation.TEMPORARY_WORKSPACE,
            temporary_root=Path(str(runtime["runtimeTemporaryRoot"])) if runtime.get("runtimeTemporaryRoot") else None,
            agent_turn_kwargs=agent_turn_kwargs,
            usage_budget=usage_budget,
            candidate_first=reducer.candidate_first,
        )
        for agent_record in reducer.agent_sessions().values():
            if reducer.node_state(agent_record["task_id"]) not in {
                NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.CANCELED, NodeState.SUPERSEDED}:
                worker.restore_async_workspace(agent_record)
        for restored_node in reducer.current_plan.nodes:
            restored_result = reducer.node_result(restored_node.node_id)
            if restored_result is not None:
                worker.hydrate_result(restored_result)
                if (reducer.candidate_first and restored_node.role == "primary" and reducer.integration_status == "prepared"
                    and reducer.node_state(restored_node.node_id) in {NodeState.VERIFYING, NodeState.SUCCEEDED}):
                    worker.hydrate_candidate(restored_result)
            continuation = reducer.continuation_for(restored_node.node_id)
            if continuation is not None and reducer.node_state(restored_node.node_id) not in {NodeState.SUCCEEDED, NodeState.VERIFYING, NodeState.FAILED_TERMINAL, NodeState.CANCELED}:
                worker.hydrate_continuation(restored_node.node_id, continuation, run_id=reducer.run_id, contract_revision=contract.contract_revision)
        pending_repair = reducer.pending_primary_repair()
        if pending_repair is not None and reducer.node_state(pending_repair.node_id) not in {
            NodeState.FAILED_TERMINAL, NodeState.CANCELED,
        }:
            worker.hydrate_primary_repair(pending_repair)

        node_result_verifier = NodeResultVerifier(
            self.artifact_store,
            strict=profile.verification_level is VerificationLevel.STRICT,
        )

        def verify_node(result: AgentResult) -> VerificationRecord:
            context = VerificationContext(
                contract=reducer.contract or contract,
                plan=reducer.current_plan,
                workspace=worker.workspace_for_node(result.node_id) if reducer.candidate_first or result.node_id != "primary" else Path(cwd),
                result=result,
            )
            return node_result_verifier.verify(context)

        def context_builder(node: PlanNode, attempt_id: str, budget: BudgetEnvelope) -> ContextManifest:
            continuation = reducer.continuation_for(node.node_id)
            # Context visibility is not a DAG dependency: the primary already
            # yielded, and only terminal consultation results are delivered.
            dependencies = tuple(dict.fromkeys((*node.dependencies, *(
                (child.node_id for child in continuation.children) if continuation else ()
            ))))
            if profile.selected_route is SelectedRoute.STANDARD_SINGLE and node.node_id == "root":
                snapshot = {}
                base_revision = _quick_base_revision(Path(cwd))
            else:
                snapshot = worker.prepare_context_snapshot(node.node_id, attempt_id)
                base_revision = worker.shared_input_revision(node.node_id) or snapshot_revision(snapshot)
            assessment = reducer.current_review_assessment
            if node.node_id == "final-verifier" and assessment and base_revision != assessment["candidate_revision"]:
                raise WorkspaceConflict("candidate changed after its observed review assessment")
            artifacts = tuple(
                artifact
                for dependency in dependencies
                for result in [worker.result_for(dependency)]
                if result is not None
                for artifact in result.artifact_refs
            )
            return ContextManifest(
                contract_revision=contract.contract_revision,
                plan_version=reducer.current_plan.plan_version,
                node_id=node.node_id,
                base_revision=base_revision,
                included_files=snapshot,
                dependency_result_refs=dependencies,
                artifact_refs=artifacts,
                memory_record=(
                    str(getattr(session, "session_id", "")) or None
                    if session is not None
                    else None
                ),
                tool_capability_scope=node.allowed_effects,
                budget_allocation=budget,
            )

        scheduler.review_snapshot = lambda result: workspace_snapshot(worker.workspace_for_node("primary"))
        runtime_context.delegation_gateway = SchedulerDelegationGateway(
            scheduler=scheduler,
            worker=worker,
            context_builder=context_builder,
            node_verifier=verify_node,
        )
        if profile.agent_resume and session is not None:
            from minicode.agent_recovery import retained_agents
            runtime_context.delegation_gateway.retained_agents = retained_agents(session,
                current_run_id=reducer.run_id, workspace=cwd, state_directory=state_path.parent,
                artifacts=self.artifact_store, generation_limit=reducer.agent_generation_limit)
        runtime_context._scheduler = scheduler
        bind_agent_controls = runtime.get("_bindAgentControls")
        if callable(bind_agent_controls):
            bind_agent_controls(scheduler)
        runtime_context.builder_gateway = None
        runtime.pop("_builderGatewayBound", None)
        if profile.parallel_candidates:
            # Bind the real writable-parallel path. Without this the primary's tool
            # view has no Builder tools at all, so the mode would be capacity the
            # model cannot reach; `_tools_for_invocation` refuses that combination
            # and the preflight reports it instead of an unused parallel arm.
            from minicode.builder_delegation import BUILDER_TOOL_NAMES, BuilderGateway
            from minicode.workspace_provider import TemporaryWorkspaceProvider

            builder_provider = worker.temporary_provider
            from minicode.registered_checks import run_combination_checks
            runtime_context.builder_gateway = BuilderGateway(
                scheduler=scheduler,
                parent_node_id="primary",
                parent_attempt_id="",
                resolve_attempt=lambda: (
                    reducer.current_attempt("primary").attempt_id
                    if reducer.current_attempt("primary") is not None else ""),
                source=Path(cwd),
                provider=builder_provider,
                target_candidate_id="primary",
                # P is resolved at the write boundary from the LIVE primary
                # candidate. There is deliberately no fallback to `cwd`: a patch
                # that cannot be applied into an isolated primary candidate is
                # refused rather than written into the user's target directory.
                target_root=worker.live_primary_candidate,
                recover_candidate=worker.recover_builder_workspace,
                combination_check=lambda patch: run_combination_checks(
                    workspace=worker.live_primary_candidate(), runtime=runtime,
                    reducer=reducer, cancellation=cancellation),
            )
            scheduler.bind_builder_lifecycle(runtime_context.builder_gateway)
            runtime["_builderGatewayBound"] = True
            # Reachability is a fact about what the primary's tool view will carry,
            # so record the names the real registration gives it and the handler
            # bound to them -- at assembly time, before any Worker runs.
            runtime["_builderCapabilityTools"] = tuple(BUILDER_TOOL_NAMES)
            runtime["_builderCapabilityHandler"] = {
                "type": type(runtime_context.builder_gateway).__name__,
                "target_candidate_id": runtime_context.builder_gateway.target_candidate_id,
                "scheduler_bound": runtime_context.builder_gateway.scheduler is scheduler,
            }
            # Zero-model capability preparation: exercise the real candidate
            # lifecycle (open, write inside the boundary, export, release) so the
            # receipt states what was done rather than what was configured. It is
            # bounded, it publishes no patch, and it is a no-op in a live run.
            if _as_bool(runtime.get("_runtimeBuilderCapabilityPrepared", False)):
                runtime["_builderCapabilityPrepare"] = (
                    runtime_context.builder_gateway.prepare_and_verify_candidate(
                        node_id="builder-capability-prepare",
                        attempt_id="builder-capability-prepare-attempt"))
        # Capability evidence, captured HERE: after the real delegation and Builder
        # gateways are bound and before any Worker can run, so the receipt describes
        # the objects a live model request will really carry. An earlier assembly
        # receipt (the Web `on_prepared` callback runs before this point) reports
        # `gateway_bound=false, tools=[]` for a run that does carry them, which is
        # why capability is measured at the binding instead of at preparation.
        runtime["_liveCapabilityReceipt"] = _live_capability_receipt(
            runtime=runtime, reducer=reducer, scheduler=scheduler, worker=worker,
            profile=profile, preferred_node=(
                "primary" if profile.primary_led else "root"),
        )
        preferred_node = (
            "primary"
            if profile.primary_led
            else
            "root"
            if profile.selected_route is SelectedRoute.STANDARD_SINGLE
            else (
                "exclusive-writer"
                if topology.requires_workspace_write
                else "read-synthesizer"
            )
            if profile.selected_route is SelectedRoute.ULTRA_READ_FANOUT
            else "deep-worker"
        )
        if _as_bool(runtime.get("_runtimeAssembleOnly", False)):
            # Zero-model preparation: the REAL assembly above has run -- profile,
            # reducer, scheduler, Worker, delegation gateway and the Builder
            # gateway are all bound exactly as they are for a live run -- and the
            # dispatch loop is not entered, so no model request is made. This is
            # what lets a preflight report a capability receipt it can be held to,
            # instead of restating a configuration value.
            usage = reducer.usage_summary()
            prepared_report = SchedulerReport(
                run_id=reducer.run_id,
                completed=False,
                canceled=False,
                claimed_attempts=0,
                peak_active_workers=scheduler.capacity.peak_workers,
                peak_active_writers=scheduler.capacity.peak_writers,
                active_leases=0,
                tokens_used=usage["tokens"],
                cost_used=usage["known_cost"],
                result_node_ids=(),
                reasons=("prepared without dispatching a Worker",),
                model_calls=usage["model_calls"],
                tokens_known=usage["tokens_known"],
                cost_known=usage["cost_known"],
                reserved_tokens=usage["reserved_tokens"],
                reserved_cost=usage["reserved_cost"],
            )
            prepared_delivery = CompletionGate().evaluate(
                contract=contract,
                plan=reducer.current_plan,
                runtime_snapshot=reducer.snapshot(),
                workspace=Path(cwd),
                verification_records=reducer.final_verification_records,
                level=profile.verification_level,
                output_summary="Runtime assembly prepared; no Worker was dispatched",
                conflicts=("preparation is not delivery: no Worker ran and no model request was made",),
            )
            prepared_manifest = context_builder(
                next(node for node in reducer.current_plan.nodes if node.node_id == preferred_node),
                "",
                profile.budget,
            )
            runtime["_runtimeAssemblyPrepared"] = True
            return RuntimeOutcome(
                contract=contract,
                profile=profile,
                topology=topology,
                plan=reducer.current_plan,
                scheduler_report=prepared_report,
                delivery=prepared_delivery,
                runtime_snapshot=reducer.snapshot(),
                context_manifests=(prepared_manifest,),
                reasoning_budget_decisions=tuple(
                    sorted(reasoning_decisions, key=lambda item: str(item.get("node_id", "")))
                ),
                messages=tuple(messages),
                live_capability=deepcopy(runtime["_liveCapabilityReceipt"]),
            )
        worker.public_node_id = preferred_node
        bind_user_controls = runtime.get("_bindUserControls")
        if callable(bind_user_controls):
            bind_user_controls(scheduler, worker, preferred_node)
        worker.public_closeout_callback = lambda: activity.publish("verifying", detail="正在核对工作区变化")
        report, delivery, preferred = self._verification_cycle(
            contract=contract, profile=profile, reducer=reducer, scheduler=scheduler,
            worker=worker, context_builder=context_builder, node_verifier=verify_node,
            workspace=Path(cwd), runtime=runtime, activity=activity, preferred_node=preferred_node,
        )
        runtime["lastSessionContextState"] = worker.context_state_for(preferred_node)
        reducer.finish_agent_team(delivery_completed=delivery.completed)
        summary = preferred.output_summary if preferred else "Runtime did not produce a deliverable Worker result"
        snapshot = reducer.snapshot()
        from minicode.runtime_delivery import review_pending_closeout
        closeout = review_pending_closeout({"state": snapshot, "delivery": delivery.to_dict()})
        if closeout:
            output_messages = [*(worker.messages_for(preferred_node) or messages), {
                "role": "assistant", "content": closeout,
                "messageId": f"review-closeout:{reducer.run_id}", "phase": "final_answer",
            }]
        elif profile.selected_route is SelectedRoute.STANDARD_SINGLE or profile.primary_led:
            if delivery.completed:
                output_messages = self._worker_delivery_messages(worker, preferred_node, preferred, messages)
                if preferred is not None and (reducer.candidate_first or any(
                    ref.evidence_type == "workspace-submission" for ref in preferred.evidence_refs)):
                    output_messages = self._candidate_delivery_messages(output_messages, preferred, delivery)
            else:
                output_messages = (
                    worker.messages_for(preferred_node)
                    if preferred is not None
                    and preferred.status is not AgentResultStatus.SUCCEEDED
                    else None
                )
                if output_messages is None:
                    output_messages = [
                        *messages,
                        {
                            "role": "assistant",
                            "content": (
                                "Runtime completion gate rejected delivery: "
                                + "; ".join(delivery.reasons)
                            ),
                        },
                    ]
        else:
            final_text = summary
            if not delivery.completed:
                final_text = "Runtime completion gate rejected delivery: " + "; ".join(delivery.reasons)
            output_messages = [*messages, {"role": "assistant", "content": final_text}]
        if delivery.completed and any(node.get("review_status") == "unavailable" for node in snapshot["nodes"].values()):
            # Qualification is coordinator-owned, never a retrospective model claim.
            for index in range(len(output_messages) - 1, -1, -1):
                if output_messages[index].get("role") == "assistant":
                    output_messages[index] = {**output_messages[index], "content": str(output_messages[index].get("content", ""))
                        + "\n\n可选独立复核未完成；本轮交付依据必要检查，尚无独立复核结论。"}
                    break
        outcome = RuntimeOutcome(
            contract=reducer.contract or contract,
            profile=profile,
            topology=topology,
            plan=reducer.current_plan,
            scheduler_report=report,
            delivery=delivery,
            runtime_snapshot=snapshot,
            context_manifests=worker.context_manifests(),
            reasoning_budget_decisions=tuple(
                sorted(reasoning_decisions, key=lambda item: str(item.get("node_id", "")))
            ),
            messages=tuple(output_messages),
            live_capability=deepcopy(runtime["_liveCapabilityReceipt"]),
        )
        from minicode.runtime_memory import publish_runtime_memory

        outcome = replace(outcome, memory_receipt=publish_runtime_memory(
            outcome, manager=agent_turn_kwargs.get("memory_manager"), runtime=runtime))
        worker.learn_from_delivery(outcome, manager=agent_turn_kwargs.get("memory_manager"))
        # The auxiliary request belongs to this run and must appear in its final ledger.
        usage = reducer.usage_summary()
        outcome = replace(outcome, runtime_snapshot=reducer.snapshot(), scheduler_report=replace(report,
            tokens_used=usage["tokens"], cost_used=usage["cost"], model_calls=usage["model_calls"],
            tokens_known=usage["tokens_known"], cost_known=usage["cost_known"],
            reserved_tokens=usage["reserved_tokens"], reserved_cost=usage["reserved_cost"]))
        self._record(outcome, runtime=runtime, session=session)
        activity.publish("delivering" if delivery.completed else "failed")
        self._publish_delivery(
            agent_turn_kwargs=agent_turn_kwargs,
            messages=output_messages,
            completed=delivery.completed,
            preferred=preferred,
            run_id=reducer.run_id,
            review_pending=bool(closeout),
        )
        return outcome

    def _worker_delivery_messages(self, worker, node_id, result, messages):
        """Recover completed primary prose independently of its submission summary."""
        output = worker.messages_for(node_id)
        if output is not None:
            return output
        if result is None:
            return list(messages)
        checkpoint = result.session_checkpoint
        if (checkpoint is not None and result.node_id == node_id
                and checkpoint.media_type == "application/vnd.minicode.primary-repair-checkpoint+json"
                and checkpoint.store_key.replace("\\", "/").startswith(f"{result.run_id}/{result.node_id}/")):
            try:
                payload = json.loads(self.artifact_store.get_text(checkpoint))
                if (isinstance(payload, dict) and payload.get("schema") == "primary-repair-checkpoint-v1"
                        and all(payload.get(key) == getattr(result, key) for key in
                                ("run_id", "node_id", "attempt_id", "plan_version", "contract_revision"))
                        and isinstance(payload.get("messages"), list)
                        and all(isinstance(message, dict) for message in payload["messages"])):
                    return deepcopy(payload["messages"])
            except (ArtifactIntegrityError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        content = result.output_summary
        if result.artifact_refs:
            try:
                content = self.artifact_store.get_text(result.artifact_refs[0])
            except (ArtifactIntegrityError, UnicodeDecodeError):
                pass
        return [*messages, {"role": "assistant", "content": content}]

    @staticmethod
    def _candidate_delivery_messages(messages, result, delivery):
        """Accept the current Worker's complete answer after Gate, preserving its body."""
        if not delivery.completed:
            return list(messages)
        from minicode.public_messages import answer_message_id

        submission = next((index for index in range(len(messages) - 1, -1, -1)
                           if messages[index].get("role") == "assistant_tool_call"
                           and messages[index].get("toolName") == "submit_candidate"), None)
        if submission is not None:
            # The native receipt authenticates these arguments, and the owned
            # checkpoint preserves them across restart. Tool-only providers can
            # deliver a complete answer without reusing the internal summary.
            arguments = messages[submission].get("input")
            if (isinstance(arguments, dict)
                    and str(arguments.get("summary", "")).strip() == result.output_summary
                    and isinstance(arguments.get("answer"), str) and arguments["answer"].strip()):
                return [*messages, {"role": "assistant", "content": arguments["answer"].strip(),
                    "messageId": answer_message_id(result.run_id), "phase": "final_answer",
                    "executionId": result.run_id, "nodeId": result.node_id, "attemptId": result.attempt_id,
                    "deliveryState": "accepted"}]
        for message in reversed(messages[:submission] if submission is not None else []):
            if message.get("role") not in {"assistant", "assistant_progress"}:
                continue
            if (message.get("role") == "assistant" and message.get("phase") == "final_answer"
                    and message.get("executionId") == result.run_id
                    and message.get("nodeId") == result.node_id
                    and message.get("attemptId") == result.attempt_id
                    and isinstance(message.get("content"), str) and message["content"].strip()):
                # The native submission summary is for Runtime, not a replacement
                # for the answer already streamed by this exact accepted attempt.
                return [*messages, {**message, "messageId": answer_message_id(result.run_id),
                                   "deliveryState": "accepted"}]
            # A later response supersedes earlier prose even within one attempt.
            # Never resurrect an earlier draft after a repair or new commentary.
            break
        # Legacy submissions may contain only internal process prose. Keep it
        # in the audit rather than presenting it as an answer or a quality pass.
        return [*messages, {"role": "assistant",
            "content": "本轮执行已结束，但未收到最终答复。可查看运行详情。",
            "messageId": answer_message_id(result.run_id), "phase": "final_answer",
            "executionId": result.run_id, "nodeId": result.node_id, "attemptId": result.attempt_id,
            "deliveryState": "accepted"}]

    def _verification_cycle(
        self, *, contract: TaskContract, profile: ExecutionProfile, reducer: RuntimeReducer,
        scheduler: TaskScheduler, worker: Any, context_builder: Any, node_verifier: Any,
        workspace: Path, runtime: Mapping[str, Any], activity: Any, preferred_node: str,
    ) -> tuple[SchedulerReport, Any, AgentResult | None]:
        """Re-enter the same primary only after an authoritative candidate-check failure."""
        while True:
            report = scheduler.execute(worker, context_builder=context_builder, node_verifier=node_verifier)
            contract = reducer.contract or contract
            if profile.selected_route is SelectedRoute.STANDARD_SINGLE:
                standard_error = worker.exception_for("root")
                if standard_error is not None:
                    raise standard_error
            # Reducer results are authoritative: a Worker cache may still contain
            # the original self-reported success after validation rejected it.
            results = [result for node in reducer.current_plan.nodes
                       if (result := reducer.node_result(node.node_id)) is not None]
            preferred = reducer.node_result(preferred_node)
            if report.unsettled_task_ids:
                # Final checks must not read or launch commands against a tree
                # whose canceled writer can still change it. Keep both OS leases
                # through late receipt collection, and return an incomplete run.
                detail = ("取消后的底层执行尚未退出，清理待完成：" + ", ".join(report.unsettled_task_ids)
                          + "；运行状态锁和工作区写锁将保留到实际退出。")
                # Even CompletionGate's expected-output check reads file bytes.
                # Reject delivery directly without touching an unsettled tree.
                delivery = DeliveryManifest(run_id=contract.envelope.run_id,
                    plan_version=reducer.current_plan.plan_version, completed=False,
                    output_summary=detail, reasons=(detail,))
                return report, delivery, preferred
            if reducer.candidate_first and reducer.integration_status in {"applying", "failed"}:
                snapshot = reducer.snapshot()
                reason = ("previous candidate integration was not durably confirmed; automatic replay is forbidden"
                          if reducer.integration_status == "applying" else
                          "previous candidate integration failed; " + snapshot.get("integration_error", "manual resolution required"))
                delivery = CompletionGate().evaluate(
                    contract=contract, plan=reducer.current_plan, runtime_snapshot=snapshot, workspace=workspace,
                    verification_records=reducer.final_verification_records, level=profile.verification_level,
                    output_summary=preferred.output_summary if preferred else "candidate delivery is blocked", conflicts=(reason,),
                )
                return report, delivery, preferred
            candidate_workspace = workspace
            candidate_revision = None
            if (reducer.candidate_first and reducer.integration_status == "prepared" and preferred is not None
                and reducer.node_state(preferred_node) is NodeState.SUCCEEDED):
                try:
                    candidate_revision = worker.validate_candidate(preferred)
                    candidate_workspace = worker.workspace_for_node(preferred_node)
                except (WorkspaceConflict, ArtifactIntegrityError, ValueError, OSError) as error:
                    delivery = CompletionGate().evaluate(
                        contract=contract, plan=reducer.current_plan, runtime_snapshot=reducer.snapshot(),
                        workspace=workspace, verification_records=(), level=profile.verification_level,
                        output_summary=preferred.output_summary, conflicts=(f"candidate unavailable: {error}",),
                    )
                    return report, delivery, preferred
            activity.publish("verifying", detail="正在确认运行状态与检查结果")
            records = self._final_verification(
                contract=contract, plan=reducer.current_plan, workspace=candidate_workspace,
                report=report, runtime=runtime, results=results, level=profile.verification_level,
                candidate_first=reducer.candidate_first,
                cancellation_event=scheduler.cancellation_event,
                permissions=getattr(worker, "permissions", None),
                artifact_store=self.artifact_store,
            )
            if scheduler.cancellation_event.is_set() and not reducer.snapshot().get("canceled"):
                scheduler.cancel("user canceled during final verification")
                report = replace(report, completed=False, canceled=True)
            gate = CompletionGate()
            gate_args = dict(
                contract=contract, plan=reducer.current_plan, runtime_snapshot=reducer.snapshot(),
                workspace=candidate_workspace, verification_records=records, level=profile.verification_level,
                output_summary=preferred.output_summary if preferred else "Runtime did not produce a deliverable Worker result",
                result_refs=tuple(result.node_id for result in results),
                artifact_refs=tuple(ref for result in results for ref in result.artifact_refs),
            )
            if candidate_revision is not None:
                accepted, candidate_records, _ = gate.check_candidate(**gate_args)
                reducer.set_final_verification_records(candidate_records)
                if accepted:
                    try:
                        activity.publish("integrating")
                        reducer.commit_candidate(preferred, revision=candidate_revision,
                            commit=lambda: worker.integrate_candidate(preferred, candidate_revision))
                    except (ReducerError, WorkspaceConflict, ArtifactIntegrityError, ValueError, OSError) as error:
                        if scheduler.cancellation_event.is_set():
                            scheduler.cancel("user canceled before candidate integration")
                            report = replace(report, completed=False, canceled=True)
                        gate_args["runtime_snapshot"] = reducer.snapshot()
                        delivery = gate.evaluate(**gate_args, conflicts=(f"candidate integration stopped: {error}",))
                        return report, delivery, preferred
                    # Cleanup only after the committed marker has been persisted.
                    worker.discard_result(preferred)
                    if scheduler.cancellation_event.is_set():
                        scheduler.cancel("user canceled after candidate integration")
                        report = replace(report, completed=False, canceled=True)
                    gate_args.update(runtime_snapshot=reducer.snapshot(), workspace=workspace,
                                     verification_records=candidate_records)
            delivery = gate.evaluate(**gate_args)
            reducer.set_final_verification_records(delivery.verification_records)
            if delivery.completed or not reducer.repair_limit:
                return report, delivery, preferred
            candidate = ""
            try:
                candidate = snapshot_revision(workspace_snapshot(candidate_workspace))
                decision = reducer.decide_primary_repair(candidate)
                if not decision.allowed:
                    raise ReducerError(decision.reason)
                if preferred is None or preferred.session_checkpoint is None:
                    raise ReducerError("completed primary has no durable repair checkpoint")
                repair = PrimaryRepair(
                    repair_id=new_id("repair"), run_id=reducer.run_id, node_id=preferred_node,
                    prior_attempt_id=preferred.attempt_id, plan_version=reducer.current_plan.plan_version,
                    contract_revision=contract.contract_revision, candidate_revision=candidate,
                    checkpoint=preferred.session_checkpoint,
                    diagnostics=tuple(record for record in delivery.verification_records if not record.passed),
                    review_result=reducer.repairable_review_result(candidate),
                )
                worker.validate_primary_repair(repair)
                reducer.apply(command(reducer.run_id, RuntimeCommandType.REQUEST_REPAIR, {"repair": repair}))
                worker.hydrate_primary_repair(repair)
            except (ReducerError, WorkspaceConflict, ArtifactIntegrityError, ValueError, OSError) as error:
                reason = ("Review recovery pending: " if reducer.repairable_review_result(candidate)
                          else "candidate repair stopped: ") + str(error)
                reducer.record_repair_stop(reason)
                return report, replace(delivery, reasons=(*delivery.reasons, reason)), preferred
            activity.publish("repairing")

    @staticmethod
    def _publish_delivery(
        *,
        agent_turn_kwargs: Mapping[str, Any],
        messages: list[dict[str, Any]],
        completed: bool,
        preferred: AgentResult | None,
        run_id: str = "",
        review_pending: bool = False,
    ) -> None:
        """Publish exactly one coordinator-owned terminal event sequence."""

        content = ""
        for message in reversed(messages):
            if message.get("role") == "assistant" and str(message.get("content", "")).strip():
                content = str(message["content"])
                break
        callbacks = agent_turn_kwargs.get("callbacks")

        def publish(event: TurnEvent) -> None:
            if callbacks is None:
                return
            try:
                on_event = getattr(callbacks, "on_event", None)
                if callable(on_event):
                    on_event(event)
                    return
                if event.kind == "assistant":
                    handler = getattr(callbacks, "on_assistant_message", None)
                    if callable(handler):
                        handler(event.content)
            except Exception:  # noqa: BLE001 - UI callbacks never own Runtime success
                return

        publishes_non_success_content = preferred is not None and preferred.status in {
            AgentResultStatus.WAITING_INPUT,
            AgentResultStatus.WAITING_APPROVAL,
        }
        from minicode.public_messages import answer_message_id
        lifecycle = bool(getattr(callbacks, "include_message_lifecycle", False))
        owner = {"node_id": preferred.node_id, "attempt_id": preferred.attempt_id} if preferred else {}
        if lifecycle and not completed:
            publish(TurnEvent(kind="assistant_status", message_id=answer_message_id(run_id),
                              phase="final_answer", delivery_state="rejected", **owner))
        if content and (completed or publishes_non_success_content or review_pending):
            publish(TurnEvent(kind="assistant", content=content,
                message_id=(f"review-closeout:{run_id}" if review_pending else
                    answer_message_id(run_id) if completed else f"response:{run_id}") if lifecycle else "",
                phase="final_answer" if completed or review_pending else "commentary",
                delivery_state="accepted" if completed else None, **({} if review_pending else owner)))
            legacy_assistant = agent_turn_kwargs.get("on_assistant_message")
            if callable(legacy_assistant):
                legacy_assistant(content)
        stop_reason = "done" if completed else "review_pending" if review_pending else "verification_failed"
        wait_kind: Literal["input", "approval"] | None = None
        if preferred is not None and not completed:
            if preferred.status is AgentResultStatus.WAITING_APPROVAL:
                stop_reason = "await_user"
                wait_kind = "approval"
            elif preferred.status is AgentResultStatus.WAITING_INPUT:
                stop_reason = "await_user"
                wait_kind = "input"
            elif preferred.status is AgentResultStatus.CANCELED:
                stop_reason = "canceled"
            elif preferred.failure is not None and preferred.failure.kind is ExecutionFailureKind.BUDGET_EXHAUSTED:
                stop_reason = "budget_exhausted"
            elif preferred.failure is not None and preferred.failure.kind is ExecutionFailureKind.PROVIDER_BALANCE:
                stop_reason = "provider_balance"
            elif preferred.failure is not None and preferred.failure.kind is ExecutionFailureKind.PROVIDER_ERROR:
                stop_reason = "provider_error"
            elif preferred.failure is not None and preferred.failure.kind is ExecutionFailureKind.MODEL_USAGE_UNRECONCILED:
                stop_reason = "model_usage_unreconciled"
            elif (
                preferred.failure is not None
                and preferred.failure.kind is ExecutionFailureKind.TOOL_TIMEOUT
            ):
                stop_reason = "tool_timeout"
        publish(
            TurnEvent.completed(
                step=None,
                messages=cast(list[ChatMessage], messages),
                stop_reason=stop_reason,
                wait_kind=wait_kind,
                timed_out=stop_reason == "tool_timeout",
                ambiguous_side_effect=(
                    preferred is not None
                    and preferred.failure is not None
                    and preferred.failure.ambiguous_side_effect
                ),
            )
        )

    @staticmethod
    def _final_verification(
        *,
        contract: TaskContract,
        plan: PlanVersion,
        workspace: Path,
        report: SchedulerReport,
        runtime: Mapping[str, Any],
        results: Iterable[AgentResult],
        level: VerificationLevel,
        candidate_first: bool = False,
        cancellation_event=None,
        permissions=None,
        artifact_store=None,
    ) -> tuple[VerificationRecord, ...]:
        results = tuple(results)
        # Revision matching is only needed for existing final evidence. The
        # registry below still captures fresh workspace evidence and executes
        # every configured check, including on read-only tasks.
        has_final_evidence = any(record.scope == "final" and record.passed
                                 for result in results for record in result.verification_records)
        current_revision = (snapshot_revision(workspace_snapshot(workspace))
                            if has_final_evidence and (_repair_limit(runtime) or candidate_first) else None)
        model_records: list[VerificationRecord] = []
        for result in results:
            for record in result.verification_records:
                if record.scope != "final":
                    continue
                if current_revision is None or not record.passed or (
                    record.evidence_refs and all(e.base_revision == current_revision for e in record.evidence_refs)
                ):
                    model_records.append(record)
                elif result.node_id == "final-verifier":
                    # A historical helper pass can be discarded as stale proof,
                    # but the required final verifier must prove this candidate.
                    model_records.append(replace(record, passed=False,
                        details="required final verifier evidence is missing or stale for the candidate"))
        injected = runtime.get("_finalVerifierRegistry")
        if isinstance(injected, VerifierRegistry):
            return (
                *model_records,
                *injected.run(
                    VerificationContext(
                        contract=contract,
                        plan=plan,
                        workspace=workspace,
                        final=True,
                        cancellation_event=cancellation_event,
                        deadline=contract.budget.deadline,
                    ),
                    allow_model=False,
                ),
            )
        registry = VerifierRegistry(
            (
                CallableVerifier(
                    name="integrated-runtime-state",
                    kind="exit_code_schema",
                    callback=lambda context: (
                        report.completed,
                        "runtime state check only: all required work is resolved; task acceptance is checked separately"
                        if report.completed
                        else "; ".join(report.reasons)
                        or "one or more current Plan nodes did not succeed",
                    ),
                    evidence_factory=workspace_snapshot_evidence,
                ),
            )
        )
        from minicode.registered_checks import register_configured_checks
        configuration_failures = register_configured_checks(registry, runtime, contract,
            workspace=workspace, project_checks=any(node.is_writer for node in plan.nodes),
            changed_paths=tuple(sorted({name for result in results for name in result.changed_resources}))
                          if results else None)
        return (
            *model_records,
            *configuration_failures,
            *registry.run(
                VerificationContext(
                    contract=contract,
                    plan=plan,
                    workspace=workspace,
                    final=True,
                    cancellation_event=cancellation_event,
                    deadline=contract.budget.deadline,
                    permissions=permissions,
                    artifact_store=artifact_store,
                ),
                allow_model=False,
            ),
        )

    @staticmethod
    def _record(outcome: RuntimeOutcome, *, runtime: dict[str, Any], session: Any | None) -> None:
        trace = outcome.trace()
        runtime["lastRuntimeTrace"] = trace
        if session is not None:
            runs = getattr(session, "runtime_runs", None)
            if isinstance(runs, list):
                runs[:] = [old for old in runs if not (isinstance(old, dict)
                    and old.get("recovery_pending") and old.get("state", {}).get("run_id") == outcome.contract.envelope.run_id)]
                runs.append(trace)


def run_runtime_turn(**kwargs) -> list[dict[str, Any]]:
    """Compatibility-shaped product entry for the unified Runtime."""
    model = kwargs.pop("model")
    tools = kwargs.pop("tools")
    messages = list(kwargs.pop("messages"))
    cwd = str(kwargs.pop("cwd"))
    permissions = kwargs.pop("permissions", None)
    session = kwargs.pop("session", None)
    artifact_store = kwargs.pop("artifact_store", None)
    supplied_runtime = kwargs.pop("runtime", None)
    runtime = supplied_runtime if supplied_runtime is not None else {}
    from minicode.image_input import bind_turn_images
    bind_turn_images(runtime, messages)
    # Adapters may have been created before this turn's attachment binding.
    if isinstance(getattr(model, "runtime", None), dict):
        model.runtime["_inputImageIds"] = runtime["_inputImageIds"]
    required_consultation = RequiredConsultation.parse(runtime.get("requiredConsultation"))
    if required_consultation:
        if (not _as_bool(runtime.get("runtimeV2", True)) or not _primary_orchestration(runtime)
            or not _durable_consultation(runtime) or tools.find("task") is None):
            raise PlanValidationError("required consultation requires primary + durable execution and the consultation tool")
        runtime["requiredConsultation"] = required_consultation.to_dict()

    if not _as_bool(runtime.get("runtimeV2", True)):
        from minicode.agent_loop import run_agent_turn

        return [
            dict(message)
            for message in run_agent_turn(
                model=model,
                tools=tools,
                messages=messages,
                cwd=cwd,
                permissions=permissions,
                session=session,
                runtime=runtime,
                **kwargs,
            )
        ]

    mode = ProfileResolver.requested_mode(runtime)
    topology = TopologyResolver().decide(
        requested_mode=mode,
        request=_last_user_request(messages),
        runtime=runtime,
        messages=messages,
        permission_mode=getattr(getattr(permissions, "auto_checker", None), "mode", None),
    )
    profile = ProfileResolver().resolve(
        runtime=runtime,
        selected_route=topology.selected_route,
        request=_last_user_request(messages),
        messages=messages,
    )
    envelope = TaskEnvelope(
        run_id=str(runtime.get("runtimeRunId") or new_id("run")),
        request=_last_user_request(messages),
        workspace=str(Path(cwd).resolve()),
        requested_profile=mode,
        session_id=str(getattr(session, "session_id", "") or "") or None,
    )
    if (
        envelope.session_id is None
        and not str(runtime.get("_memoryTaskId") or runtime.get("memoryTaskId") or "").strip()
    ):
        # Fan-out Workers copy the Runtime mapping before paged memory starts.
        # Seed one task identity on the coordinator-owned mapping so every copy
        # writes to the same authority scope instead of allocating per Worker.
        runtime["_memoryTaskId"] = f"task_{envelope.run_id}"
    contract = TaskContract(
        envelope=envelope,
        contract_revision=max(1, int(runtime.get("runtimeContractRevision", 1) or 1)),
        constraints=tuple(str(item) for item in runtime.get("taskConstraints", ()) if str(item)),
        acceptance_criteria=tuple(
            str(item) for item in runtime.get("acceptanceCriteria", ()) if str(item)
        ),
        expected_outputs=tuple(str(item) for item in runtime.get("expectedOutputs", ()) if str(item)),
        budget=profile.budget,
        required_consultation=required_consultation,
    )
    plan = PlanCompiler().compile(contract, profile, topology)
    if contract.required_consultation and len(plan.nodes) + contract.required_consultation.min_helpers > profile.max_agents:
        raise PlanValidationError("max_agents cannot accommodate the required consultation and existing plan")
    canonical_graph = CanonicalPlanGraph(plan)
    PlanValidator().validate(
        canonical_graph.current,
        profile,
        requires_workspace_write=topology.requires_workspace_write,
    )
    progress = kwargs.get("on_progress_message")
    if callable(progress):
        progress(
            f"Runtime profile={profile.requested_mode.value}; route={profile.selected_route.value}; "
            f"workers<={profile.max_active_workers}; verification={profile.verification_level.value}"
        )
        if mode is RequestedMode.ULTRA and topology.selected_route is SelectedRoute.DEEP_SINGLE:
            progress(topology.reasons[0])
    outcome = RunCoordinator(artifact_store=artifact_store).run(
        contract=contract,
        profile=profile,
        topology=topology,
        plan=canonical_graph.current,
        model=model,
        tools=tools,
        messages=messages,
        cwd=cwd,
        runtime=runtime,
        permissions=permissions,
        session=session,
        agent_turn_kwargs=kwargs,
    )
    if outcome.delivery.completed:
        from minicode.skill_runtime import release_skill_run
        release_skill_run(cwd=cwd, runtime=runtime, run_id=envelope.run_id, reason="completed")
    elif outcome.scheduler_report.canceled:
        from minicode.skill_runtime import release_skill_run
        release_skill_run(cwd=cwd, runtime=runtime, run_id=envelope.run_id, reason="canceled")
    runtime.pop("_userSkillInvocation", None)
    return list(outcome.messages)


def _last_user_request(messages: Iterable[Mapping[str, Any]]) -> str:
    materialized = list(messages)
    for message in reversed(materialized):
        if is_user_input(message):
            return str(message.get("content", ""))
    return ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _positive_limit(runtime: Mapping[str, Any], key: str, *, default: int) -> int:
    if key not in runtime or runtime.get(key) is None:
        return default
    parsed = _configured_integer(runtime[key], key=key, requirement="positive")
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def _optional_positive_int(runtime: Mapping[str, Any], key: str) -> int | None:
    if key not in runtime or runtime.get(key) is None:
        return None
    return _positive_limit(runtime, key, default=1)


def _nonnegative_budget(runtime: Mapping[str, Any], key: str, *, default: int) -> int:
    if key not in runtime or runtime.get(key) is None:
        return default
    parsed = _configured_integer(
        runtime[key],
        key=key,
        requirement="non-negative",
    )
    if parsed < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return parsed


def _configured_integer(value: Any, *, key: str, requirement: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{key} must be a {requirement} integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    raise TypeError(f"{key} must be a {requirement} integer")


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _optional_positive_float(runtime: Mapping[str, Any], key: str) -> float | None:
    if key not in runtime or runtime.get(key) is None:
        return None
    parsed = _positive_float(runtime.get(key))
    if parsed is None:
        raise ValueError(f"{key} must be a positive finite number")
    return parsed


def _finite_deadline(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("runtimeDeadline must be a finite timestamp") from error
    if not math.isfinite(parsed):
        raise ValueError("runtimeDeadline must be a finite timestamp")
    return parsed


def _strict_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _topology_score(value: Any, *, default: float, invalid: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return invalid
    return parsed if math.isfinite(parsed) and 0.0 <= parsed <= 1.0 else invalid


def _quick_base_revision(workspace: Path) -> str:
    # Standard is the latency-sensitive compatibility path.  Spawning Git for
    # every turn costs around 100 ms on Windows and HEAD alone would not bind
    # dirty workspace content anyway.  Tool calls that need exact mutation
    # safety carry their own operation digest; this marker only identifies the
    # fast-path workspace generation without adding a subprocess/model call.
    marker = f"{workspace.resolve()}:{workspace.stat().st_mtime_ns if workspace.exists() else 0}"
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_run_identity(
    *,
    contract: TaskContract,
    profile: ExecutionProfile,
    topology: TopologyDecision,
    messages: list[dict[str, Any]],
    cwd: str,
    runtime: Mapping[str, Any],
) -> RuntimeRunIdentity:
    return RuntimeRunIdentity(
        run_id=contract.envelope.run_id,
        workspace=normalize_workspace_path(cwd),
        request_hash=_stable_hash(contract.envelope.request),
        messages_hash=_stable_hash(messages),
        profile_hash=_stable_hash(profile.to_dict()),
        policy_hash=_stable_hash(
            {
                str(key): value
                for key, value in runtime.items()
                if not str(key).startswith("_")
                and key
                not in {
                    "lastRuntimeTrace",
                    # Output-only checkpoint projection, written by the
                    # coordinator after execution. It is not an input policy;
                    # hashing it makes the caller's own next resume invalid.
                    "lastSessionContextState",
                    "acceptanceCriteria",
                    "expectedOutputs",
                    "runtimeResume",
                    "runtimeRunId",
                    "runtimeContractRevision",
                    "runtimeStatePath",
                    # The effective selection is already bound by profile_hash
                    # and selected_route.  The Standard compatibility Worker
                    # may populate this legacy alias during execution.
                    "runtimeProfile",
                    "taskConstraints",
                    "webRunId",
                }
            }
        ),
        session_id=contract.envelope.session_id,
        requested_profile=contract.envelope.requested_profile,
        selected_route=topology.selected_route,
    )


def _validate_resume_identity(
    *,
    persisted: RuntimeRunIdentity,
    current: RuntimeRunIdentity,
) -> None:
    persisted_data = persisted.to_dict()
    current_data = current.to_dict()
    ignored = {"schema_version"}
    mismatches = sorted(
        key
        for key in persisted_data
        if key not in ignored and persisted_data.get(key) != current_data.get(key)
    )
    if mismatches:
        raise RuntimeStateLoadError(
            "Runtime resume identity mismatch: " + ", ".join(mismatches)
        )


def _validate_resume_contract(
    *,
    persisted: TaskContract,
    current: TaskContract,
    amendment: TaskAmendment | None,
) -> None:
    """Separate mutable Contract revisions from immutable run identity."""

    immutable_envelope_fields = (
        "run_id",
        "request",
        "requested_profile",
        "session_id",
    )
    changed_identity_fields = [
        name
        for name in immutable_envelope_fields
        if getattr(persisted.envelope, name) != getattr(current.envelope, name)
    ]
    if normalize_workspace_path(persisted.envelope.workspace) != normalize_workspace_path(
        current.envelope.workspace
    ):
        changed_identity_fields.append("workspace")
    if persisted.budget != current.budget:
        changed_identity_fields.append("budget")
    if persisted.required_consultation != current.required_consultation:
        changed_identity_fields.append("required_consultation")
    if changed_identity_fields:
        raise RuntimeStateLoadError(
            "Runtime resume TaskContract changes immutable identity fields: "
            + ", ".join(changed_identity_fields)
        )
    if amendment is not None:
        if current.contract_revision != amendment.next_contract_revision:
            raise RuntimeStateLoadError(
                "amended TaskContract does not match amendment next revision"
            )
        return
    persisted_material = {
        "contract_revision": persisted.contract_revision,
        "constraints": persisted.constraints,
        "acceptance_criteria": persisted.acceptance_criteria,
        "expected_outputs": persisted.expected_outputs,
    }
    current_material = {
        "contract_revision": current.contract_revision,
        "constraints": current.constraints,
        "acceptance_criteria": current.acceptance_criteria,
        "expected_outputs": current.expected_outputs,
    }
    if persisted_material != current_material:
        raise RuntimeStateLoadError("Runtime resume TaskContract does not match persisted state")
