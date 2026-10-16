"""Safe, content-light projections for the local web workbench.

The persisted runtime/session objects deliberately contain enough information
to resume execution.  The browser needs a much smaller inspection surface: no
credentials, executable MCP configuration, instruction bodies, checkpoint
contents, artifact store keys, or external evidence resource locations cross this
boundary.
"""

from __future__ import annotations

import math
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, cast
from urllib.parse import urlsplit

from minicode.release_readiness import redact_sensitive_text
from minicode.runtime_delivery import EntryCompletion, entry_completion, review_pending_reason, review_pending_closeout, task_memory_view
from minicode.entry_task import public_task_file
from minicode.web_runtime_settings import public_run_budget


def _mapping(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(cast(Any, value))
    return dict(value) if isinstance(value, Mapping) else {}


def _review_policy_fields(value: Any) -> dict[str, Any]:
    policy = _mapping(value)
    if policy.get("scope") not in {"basic", "focused", "full"} or type(policy.get("required")) is not bool:
        return {}
    return {"reviewPolicy": {"scope": policy["scope"], "required": policy["required"]}}


def _text(value: Any, limit: int = 600) -> str:
    if value is None:
        return ""
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "…"


def _raw_text(value: Any, limit: int = 12_000) -> str:
    """Preserve line breaks for user-reviewed commands and diffs."""

    if value is None:
        return ""
    raw = str(value).replace("\x00", "")
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)] + "…"


def _strings(value: Any, *, limit: int = 12, item_limit: int = 300) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [_text(item, item_limit) for item in list(value)[:limit] if _text(item, item_limit)]


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _relative_resource(workspace: str | Path, value: Any) -> str:
    """Return a project-relative label without disclosing external paths."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    root = Path(workspace)
    try:
        if candidate.is_absolute():
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        normalized = Path(os.path.normpath(raw))
        if ".." in normalized.parts:
            return normalized.name
        return normalized.as_posix()
    except (OSError, RuntimeError, ValueError):
        return candidate.name or _text(raw, 180)


def public_skills(items: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        value = _mapping(item)
        name = _text(value.get("name"), 100)
        if not name:
            continue
        result.append(
            {
                "name": name,
                "description": _text(value.get("description"), 360),
                "source": _text(value.get("source") or value.get("scope") or "local", 60),
                "userInvocable": value.get("user_invocable", value.get("userInvocable", True)) is True
                and not bool(value.get("error")) and name == value.get("name"),
            }
        )
    return result


def public_mcp_servers(items: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(items, Mapping):
        items = [
            {
                "name": name,
                "status": "disabled" if _mapping(config).get("enabled") is False else "configured",
                "toolCount": 0,
                "protocol": _mapping(config).get("protocol"),
            }
            for name, config in items.items()
        ]
    if not isinstance(items, list):
        return result
    for item in items:
        value = _mapping(item)
        name = _text(value.get("name"), 100)
        if not name:
            continue
        result.append(
            {
                "name": name,
                "status": _text(value.get("status") or "configured", 40),
                "toolCount": _integer(value.get("toolCount", value.get("tool_count", 0))),
                "resourceCount": _integer(value.get("resourceCount", value.get("resource_count", 0))),
                "promptCount": _integer(value.get("promptCount", value.get("prompt_count", 0))),
                "protocol": _text(value.get("protocol"), 40),
                "error": (
                    "连接失败；请在本机配置中检查该服务器。"
                    if value.get("error") else ""
                ),
            }
        )
    return result


def public_tools(registry: Any) -> list[dict[str, Any]]:
    try:
        definitions = registry.list()
    except (AttributeError, TypeError):
        return []
    result: list[dict[str, Any]] = []
    for tool in definitions:
        name = _text(getattr(tool, "name", ""), 120)
        if not name:
            continue
        effect = getattr(getattr(tool, "effect", None), "value", getattr(tool, "effect", ""))
        replay = getattr(
            getattr(tool, "replay_policy", None),
            "value",
            getattr(tool, "replay_policy", ""),
        )
        source = "mcp" if name.startswith("mcp__") else "skill" if name == "load_skill" else "core"
        result.append(
            {
                "name": name,
                "description": _text(getattr(tool, "description", ""), 360),
                "effect": _text(effect, 60),
                "replayPolicy": _text(replay, 80),
                "source": source,
            }
        )
    return result


def public_tool_catalog(registry: Any, cwd: str) -> list[dict[str, Any]]:
    """Describe native and scoped tools without creating a live Worker or MCP client.

    This catalog is deliberately separate from ``tools``: a declaration is not
    an execution grant. Definitions with safe construction reuse their actual
    metadata; context-bound handlers are described without manufacturing a
    Runtime, memory store, candidate, or authorization callback.
    """
    from minicode.tool_discovery import ToolDiscovery
    from minicode.tooling import ToolRegistry
    from minicode.tools import create_builtin_tools
    from minicode.tools.builders import builder_tools
    from minicode.tools.session_history_read import session_history_read_tool
    from minicode.tools.session_history_search import session_history_search_tool
    from minicode.tools.task import (
        advisory_acknowledgment_tool, async_agent_tools,
        durable_consultation_tool, retry_consultation_tool,
    )

    rows = {tool["name"]: {**tool, "availability": "registered", "activation": ""}
            for tool in public_tools(registry)}

    def declare(definitions: list[Any], activation: str) -> None:
        for tool in public_tools(ToolRegistry(definitions)):
            rows.setdefault(tool["name"], {**tool, "availability": "conditional", "activation": activation})

    declare(create_builtin_tools(cwd), "由当前工具配置与 Agent 权限决定是否注册；实用工具需要完整工具模式。")
    declare([session_history_read_tool, session_history_search_tool], "运行会话时加载，仅访问当前会话保存的历史。")
    declare([ToolDiscovery().tool()], "运行时启用工具发现后加载，仅发现当前任务获准使用的工具。")
    declare(list(async_agent_tools()), "运行时启用多 Agent 协作后，按主任务或成员角色、消息、续接与团队配置加载。")
    declare(list(builder_tools()), "启用并行候选模式并绑定构建任务调度器后，提供给主任务。")
    declare([durable_consultation_tool, advisory_acknowledgment_tool, retry_consultation_tool],
            "主任务启用持久化咨询后，按咨询契约、失败接管或修复状态加载。")

    # These handlers require live, owner-scoped state. Catalog inspection must
    # never instantiate that state or make an executable substitute for it.
    context_bound = [
        ("memory_search", "搜索当前任务中带来源的记忆卡片。", "read_only", "safe", "启用分页记忆后加载，受当前任务和角色权限限制。"),
        ("memory_get", "按范围读取记忆卡片或来源事件的原文。", "read_only", "safe", "启用分页记忆后加载，受当前任务和角色权限限制。"),
        ("artifact_read", "分页读取当前任务保存的工具结果。", "read_only", "safe", "启用分页记忆后加载；读取已保存的结果，不代表当前文件内容。"),
        ("history_search", "搜索或浏览当前任务保存的原始历史。", "read_only", "safe", "启用分页记忆后加载，按当前任务范围查询。"),
        ("checkpoint_restore", "从经过核验的检查点恢复当前运行视图。", "privileged", "never_auto_retry", "启用分页记忆且当前角色允许恢复检查点时加载。"),
        ("memory_note_update", "更新当前任务的工作交接笔记。", "workspace_write", "never_auto_retry", "启用交接笔记且不处于内联记忆模式时加载；只读成员仅维护自己的记忆状态，实际权限由运行时决定。"),
        ("context_rotate", "保存交接笔记后切换到下一段工作上下文。", "workspace_write", "never_auto_retry", "启用上下文轮换且不处于内联记忆模式时加载；只读成员仅变更自己的上下文状态，实际权限由运行时决定。"),
        ("runtime_context_read", "分页读取当前 Worker 记录的上下文与工具输入输出。", "read_only", "safe", "运行时为当前 Worker 绑定上下文读取器后加载。"),
        ("runtime_artifact_read", "分页读取获准访问的依赖结果、成员消息或项目检查报告。", "read_only", "safe", "当前角色拥有显式依赖、消息或报告读取权限时加载。"),
        ("record_working_context", "记录主任务的当前决策、未解决风险与下一步。", "read_only", "never_auto_retry", "主任务启用工作上下文记录并拥有可引用的咨询来源时加载。"),
        ("resolve_user_input", "处理运行期间收到的用户指令并记录响应。", "read_only", "safe", "运行时为当前 Worker 绑定用户指令处理器后加载。"),
        ("submit_candidate", "将主任务的当前候选提交给运行时进行已配置的验收。", "workspace_write", "never_auto_retry", "当前主任务具有原生候选提交契约时加载；提交不等于验收通过。"),
        ("submit_builder_candidate", "将构建成员的独立候选交给主任务审查。", "workspace_write", "never_auto_retry", "独立构建成员具有原生候选提交契约时加载；交接不会自动合并。"),
        ("submit_consultation", "提交辅助任务的咨询发现、摘要与核验结论。", "read_only", "never_auto_retry", "只读辅助任务具有原生咨询提交契约时加载。"),
        ("run_acceptance_checks", "执行任务已配置的验收检查并保留真实结果。", "workspace_write", "never_auto_retry", "当前任务绑定了验收检查时加载；命令授权与输入版本检查仍然生效。"),
        ("request_verification", "请求运行时核验当前固定输入版本。", "read_only", "never_auto_retry", "独立核验成员绑定了原生核验请求器时加载。"),
        ("list_mcp_resources", "列出已连接 MCP 服务提供的资源。", "privileged", "never_auto_retry", "已连接的 MCP 服务提供资源后加载。"),
        ("read_mcp_resource", "读取指定 MCP 服务提供的资源内容。", "privileged", "never_auto_retry", "已连接的 MCP 服务提供资源后加载。"),
        ("list_mcp_prompts", "列出已连接 MCP 服务提供的提示模板。", "privileged", "never_auto_retry", "已连接的 MCP 服务提供提示模板后加载。"),
        ("get_mcp_prompt", "获取指定 MCP 提示模板及其内容。", "privileged", "never_auto_retry", "已连接的 MCP 服务提供提示模板后加载。"),
    ]
    for name, description, effect, replay, activation in context_bound:
        rows.setdefault(name, {"name": name, "description": description, "effect": effect,
                              "replayPolicy": replay, "source": "core",
                              "availability": "conditional", "activation": activation})
    return sorted(rows.values(), key=lambda tool: tool["name"])


_APPROVAL_CHOICE_LABELS = {
    "allow_once": "仅允许这一次",
    "allow_turn": "本轮允许这个文件",
    "allow_all_turn": "本轮允许所有修改",
    "allow_always": "以后自动允许此操作",
    "deny_once": "暂不允许",
    "deny_with_feedback": "拒绝并告诉它原因",
    "deny_always": "以后自动拒绝此操作",
}

_SAFE_DATE_COMMAND = re.compile(
    r"^(?:powershell|pwsh)(?:\.exe)?\s+"
    r"(?:(?:-NoProfile|-NonInteractive)\s+)*-Command\s+"
    r"(?:[\"']?Get-Date(?:\s+-Format\s+(?:\"[A-Za-z0-9 _:/.-]+\"|'[A-Za-z0-9 _:/.-]+'|[A-Za-z0-9_:/.\-]+))?[\"']?)$",
    re.IGNORECASE,
)

_CURL_COMMAND = re.compile(r"^(?:curl|curl\.exe)(?:\s|$)", re.IGNORECASE)
_CURL_WEB_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_CURL_URL_SCHEME = re.compile(r"\b([a-z][a-z0-9+.-]*)://", re.IGNORECASE)
_CURL_UNSAFE_LONG_OPTIONS = frozenset(
    {
        "--abstract-unix-socket",
        "--cacert",
        "--capath",
        "--cert",
        "--connect-to",
        "--config",
        "--cookie",
        "--cookie-jar",
        "--crlfile",
        "--dump-header",
        "--etag-compare",
        "--etag-save",
        "--form",
        "--form-string",
        "--ftp-create-dirs",
        "--json",
        "--key",
        "--mail-from",
        "--location-trusted",
        "--mail-rcpt",
        "--netrc",
        "--netrc-file",
        "--netrc-optional",
        "--output",
        "--oauth2-bearer",
        "--output-dir",
        "--proxy-cert",
        "--proxy-header",
        "--proxy-key",
        "--proxy-user",
        "--quote",
        "--referer",
        "--remote-header-name",
        "--remote-name",
        "--remote-name-all",
        "--request",
        "--resolve",
        "--trace",
        "--trace-ascii",
        "--unix-socket",
        "--upload-file",
        "--user",
        "--header",
    }
)
_CURL_UNSAFE_SHORT_OPTIONS = frozenset("DEFHKQOTUXbcdehnoux")


def _safe_curl_web_hosts(command: str) -> tuple[str, ...] | None:
    """Recognize direct curl invocations that only read HTTP(S) to stdout.

    The check is intentionally conservative.  Any option that can upload data,
    read credentials/files, write a local file, change the HTTP method, or talk
    to a local socket keeps the generic high-risk command presentation.
    """

    stripped = command.strip()
    if not _CURL_COMMAND.match(stripped):
        return None
    schemes = {match.casefold() for match in _CURL_URL_SCHEME.findall(stripped)}
    if not schemes or not schemes.issubset({"http", "https"}):
        return None
    if re.search(r"(?:^|\s)@", stripped):
        return None

    for token in stripped.split()[1:]:
        if token.startswith("--"):
            option = token.split("=", 1)[0].casefold()
            if (
                option in _CURL_UNSAFE_LONG_OPTIONS
                or option == "--data"
                or option.startswith("--data-")
            ):
                return None
        elif token.startswith("-") and token != "-":
            if any(flag in _CURL_UNSAFE_SHORT_OPTIONS for flag in token[1:]):
                return None

    hosts: list[str] = []
    for raw_url in _CURL_WEB_URL.findall(stripped):
        try:
            host = (urlsplit(raw_url).hostname or "").strip().casefold()
        except ValueError:
            return None
        if not host:
            return None
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts) if hosts else None


def _curl_host_label(hosts: tuple[str, ...]) -> str:
    if len(hosts) == 1:
        return hosts[0]
    if len(hosts) == 2:
        return "、".join(hosts)
    return f"{hosts[0]} 等 {len(hosts)} 个网站"


def _approval_detail(details: list[str], prefix: str) -> str:
    marker = prefix.casefold() + ":"
    for detail in details:
        if detail.casefold().startswith(marker):
            return detail.split(":", 1)[1].strip()
    return ""


def _approval_location_label(workspace: str | Path, raw_path: str) -> str:
    if not raw_path:
        return "当前任务目录"
    normalized = raw_path.replace("\\", "/").rstrip("/").casefold()
    if normalized.endswith("/.mini-code/standalone"):
        return "独立对话临时目录"
    try:
        if Path(raw_path).resolve() == Path(workspace).resolve():
            return "当前项目"
    except (OSError, RuntimeError, ValueError):
        pass
    return Path(raw_path).name or "本机目录"


def _public_approval_choices(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    choices: list[dict[str, str]] = []
    for raw in value:
        choice = _mapping(raw)
        decision = _text(choice.get("decision"), 60)
        if not decision:
            continue
        choices.append(
            {
                "key": _text(choice.get("key"), 20),
                "decision": decision,
                "label": _APPROVAL_CHOICE_LABELS.get(decision, _text(choice.get("label") or decision, 80)),
            }
        )
    return choices


_CONTEXTUAL_APPROVAL_LABELS: dict[str, dict[str, str]] = {
    "network_read": {
        "allow_once": "允许这次访问",
        "deny_once": "不访问",
        "allow_always": "以后自动允许这条命令",
        "deny_always": "以后自动拒绝这条命令",
    },
    "local_time_read": {
        "allow_once": "允许这次读取",
        "deny_once": "不读取",
    },
    "command": {
        "allow_once": "运行这一条",
        "deny_once": "不运行",
        "allow_always": "以后自动允许这条命令",
        "deny_always": "以后自动拒绝这条命令",
    },
    "edit": {
        "allow_once": "允许这次修改",
        "deny_once": "不修改",
    },
    "path": {
        "allow_once": "允许这次访问",
        "deny_once": "不访问",
    },
}


def _contextual_approval_choices(value: Any, operation_type: str) -> list[dict[str, str]]:
    choices = _public_approval_choices(value)
    labels = _CONTEXTUAL_APPROVAL_LABELS.get(operation_type, {})
    return [
        {**choice, "label": labels.get(choice["decision"], choice["label"])}
        for choice in choices
    ]


def public_approval_request(value: Any, workspace: str | Path) -> dict[str, Any]:
    """Turn an internal permission request into a plain-language review card.

    This only changes presentation.  Command classification and the resulting
    permission decision remain owned by ``PermissionManager``.
    """

    request = _mapping(value)
    kind = _text(request.get("kind") or "unknown", 40)
    raw_details = request.get("details", [])
    details = [_raw_text(item) for item in raw_details] if isinstance(raw_details, list) else []
    command = _approval_detail(details, "command")
    cwd = _approval_detail(details, "cwd")
    target = _approval_detail(details, "target")
    internal_reason = _approval_detail(details, "reason")
    digest = _raw_text(request.get("operationDigest") or _approval_detail(details, "operation digest"), 300)

    title = "需要你确认这项操作"
    action = "MiniCode 准备执行一项超出默认权限的操作。"
    impact = "具体影响取决于下方技术详情。"
    reason = "这项操作需要获得你的明确授权后才能继续。"
    location = _approval_location_label(workspace, cwd)
    target_label = "运行位置"
    operation_type = kind
    risk_level = "medium"
    technical: list[dict[str, str]] = []

    if kind == "command":
        curl_hosts = _safe_curl_web_hosts(command)
        if curl_hosts:
            host_label = _curl_host_label(curl_hosts)
            title = f"允许读取 {host_label} 的网页？"
            action = f"MiniCode 将通过 curl 访问 {host_label}，读取公开网页内容。"
            impact = "只会联网读取并把网页内容交给当前任务；不会修改本机文件。"
            reason = "这里做的是网页读取，但 curl 属于可执行的本机命令，所以安全策略仍要求你确认。"
            location = host_label
            target_label = "访问网站"
            operation_type = "network_read"
            risk_level = "low"
        elif _SAFE_DATE_COMMAND.fullmatch(command.strip()):
            title = "允许读取本机日期和时间？"
            action = "MiniCode 将读取这台电脑的当前日期和时间。"
            impact = "只读取系统时钟；不会写文件、改设置或联网。"
            reason = "调用的是 PowerShell；它具备执行任意本机代码的能力，所以安全策略仍会在运行前拦住它。"
            target_label = "读取位置"
            operation_type = "local_time_read"
            risk_level = "low"
        else:
            executable = command.split(maxsplit=1)[0] if command else "本机程序"
            title = "允许 MiniCode 运行这条本机命令？"
            action = f"MiniCode 将通过 {executable} 运行一条本机命令。"
            impact = "命令可能读取或修改本机内容；具体影响取决于完整命令。"
            reason = "这类命令具备较高的本机权限，安全策略要求在运行前确认。"
            risk_level = "high"
        if command:
            technical.append({"label": "完整命令", "value": command})
        if cwd:
            technical.append({"label": "运行目录", "value": cwd})
        if internal_reason:
            technical.append({"label": "内部风险判断", "value": internal_reason})
    elif kind == "edit":
        label = _relative_resource(workspace, target) or Path(target).name or "文件"
        title = "允许修改这个文件？"
        action = f"MiniCode 准备修改 {label}。"
        impact = "确认后会写入文件；你可以先查看下方改动预览。"
        reason = "文件内容会发生变化，所以运行前需要你的确认。"
        location = "当前项目" if label != Path(target).name else _approval_location_label(workspace, str(Path(target).parent))
        target_label = "作用位置"
        operation_type = "edit"
        risk_level = "medium"
        if target:
            technical.append({"label": "目标文件", "value": target})
        preview = "\n".join(
            item
            for item in details
            if item
            and not item.casefold().startswith(("target:", "operation digest:"))
        )
        if preview:
            technical.append({"label": "改动预览", "value": preview})
    elif kind == "path":
        title = "允许访问当前项目之外的位置？"
        action = "MiniCode 准备访问工作区边界外的文件或目录。"
        impact = "可能读取或写入项目之外的本机内容，具体取决于原始任务。"
        reason = "工作区之外的内容不在本次任务的默认权限内。"
        location = Path(target).name or "项目外位置"
        target_label = "目标位置"
        operation_type = "path"
        risk_level = "high"
        for label, prefix in (("当前工作区", "cwd"), ("目标位置", "target"), ("授权范围", "scope directory")):
            detail = _approval_detail(details, prefix)
            if detail:
                technical.append({"label": label, "value": detail})
    else:
        for index, detail in enumerate(details, 1):
            if detail:
                technical.append({"label": f"详情 {index}", "value": detail})

    if digest:
        technical.append({"label": "操作校验码", "value": digest})

    return {
        "kind": kind,
        "summary": _text(request.get("summary"), 300),
        "details": details,
        "scope": _text(request.get("scope"), 600),
        "choices": _contextual_approval_choices(request.get("choices", []), operation_type),
        "title": title,
        "action": action,
        "impact": impact,
        "reason": reason,
        "target": location,
        "targetLabel": target_label,
        "operationType": operation_type,
        "riskLevel": risk_level,
        "timeoutPolicy": "不确认时不会执行，也不占工具执行时限；任务总时长和停止操作仍然生效。",
        "technicalDetails": technical,
    }


def public_instruction_layers(items: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        value = _mapping(item)
        result.append(
            {
                "name": _text(value.get("name"), 100),
                "scope": _text(value.get("scope"), 40),
                "kind": _text(value.get("kind"), 40),
                "exists": bool(value.get("exists", False)),
                "path": _text(value.get("path"), 4096),
                "status": _text(value.get("status") or "unknown", 40),
                "preview": _text(value.get("preview"), 500),
                "contentHash": _text(value.get("content_hash"), 64),
                "updatedAt": value.get("updated_at") if type(value.get("updated_at")) in (int, float) else None,
                "error": _text(value.get("error"), 100),
            }
        )
    return [item for item in result if item["name"]]


def public_hook_status(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    return {
        "totalHooks": _integer(item.get("total_hooks", item.get("totalHooks", 0))),
        "enabledHooks": _integer(item.get("enabled_hooks", item.get("enabledHooks", 0))),
        "totalCalls": _integer(item.get("total_calls", item.get("totalCalls", 0))),
        "totalDurationMs": _integer(item.get("total_duration_ms", item.get("totalDurationMs", 0))),
        "summary": _text(item.get("summary"), 240),
    }


def public_delegation_status(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    return {
        "runningTasks": _integer(item.get("running_tasks", item.get("runningTasks", 0))),
        "totalTracked": _integer(item.get("total_tracked", item.get("totalTracked", 0))),
        "maxSlots": _integer(item.get("max_slots", item.get("maxSlots", 0))),
        "availableSlots": _integer(item.get("available_slots", item.get("availableSlots", 0))),
        "activeLabels": _strings(item.get("active_labels", item.get("activeLabels", [])), limit=8, item_limit=100),
        "summary": _text(item.get("summary"), 240),
    }


def public_extensions(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        value = _mapping(item)
        name = _text(value.get("name"), 100)
        if not name:
            continue
        raw_contributes = value.get("contributes", {})
        inferred_kinds = (
            [str(kind) for kind, contribution in raw_contributes.items() if contribution]
            if isinstance(raw_contributes, Mapping)
            else []
        )
        result.append(
            {
                "id": _text(value.get("id") or name, 128),
                "key": _text(
                    value.get("key")
                    or f"{value.get('scope', '')}:{value.get('id') or name}",
                    180,
                ),
                "name": name,
                "scope": _text(value.get("scope"), 40),
                "version": _text(value.get("version"), 60),
                "description": _text(value.get("description"), 260),
                "enabled": bool(value.get("enabled", True)),
                "trusted": bool(value.get("trusted", False)),
                "health": _text(value.get("health") or "unknown", 80),
                "healthDetail": _text(
                    value.get("health_detail", value.get("healthDetail", "")),
                    360,
                ),
                "permissions": _strings(value.get("permissions", []), limit=32, item_limit=100),
                "grantedPermissions": _strings(
                    value.get("granted_permissions", value.get("grantedPermissions", [])),
                    limit=32,
                    item_limit=100,
                ),
                "contributionKinds": _strings(
                    value.get(
                        "contribution_kinds",
                        value.get("contributionKinds", inferred_kinds),
                    ),
                    limit=16,
                    item_limit=60,
                ),
            }
        )
    return result


def public_readiness(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    checks: list[dict[str, str]] = []
    raw_checks = item.get("preflight_checks", item.get("preflightChecks", []))
    if isinstance(raw_checks, list):
        for raw in raw_checks[:12]:
            check = _mapping(raw)
            checks.append(
                {
                    "name": _text(check.get("name") or check.get("check") or check.get("label"), 100),
                    "status": _text(check.get("status"), 40),
                    "detail": _text(check.get("detail") or check.get("message"), 220),
                }
            )
    return {
        "status": _text(item.get("status") or "unknown", 40),
        "provider": _text(item.get("provider"), 80),
        "providerReady": bool(item.get("provider_ready", item.get("providerReady", False))),
        "providerChannel": _text(item.get("provider_channel", item.get("providerChannel", "")), 80),
        "fallbackReady": bool(item.get("fallback_ready", item.get("fallbackReady", False))),
        "fallbackCandidates": _strings(item.get("fallback_candidates", item.get("fallbackCandidates", [])), limit=8, item_limit=100),
        "viableFallbacks": _strings(item.get("viable_fallbacks", item.get("viableFallbacks", [])), limit=8, item_limit=100),
        "riskScope": _text(item.get("risk_scope", item.get("riskScope", "unknown")), 80),
        "nextActions": _strings(item.get("next_actions", item.get("nextActions", [])), limit=8, item_limit=240),
        "issues": _strings(item.get("issues", []), limit=8, item_limit=240),
        "preflightChecks": checks,
        "summary": _text(item.get("summary"), 360),
    }


def public_product_snapshot(snapshot: Any) -> dict[str, Any]:
    value = _mapping(snapshot)
    return {
        "instructionLayers": public_instruction_layers(value.get("instruction_layers", value.get("instructionLayers", []))),
        "instructionSummary": _text(value.get("instruction_summary", value.get("instructionSummary", "")), 240),
        "hookStatus": public_hook_status(value.get("hook_status", value.get("hookStatus", {}))),
        "delegationStatus": public_delegation_status(value.get("delegation_status", value.get("delegationStatus", {}))),
        "extensions": public_extensions(value.get("extension_manifests", value.get("extensions", []))),
        "extensionSummary": _text(value.get("extension_summary", value.get("extensionSummary", "")), 240),
        "readiness": public_readiness(value.get("readiness_report", value.get("readiness", {}))),
        "readinessSummary": _text(value.get("readiness_summary", value.get("readinessSummary", "")), 360),
    }


def public_memory(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    if item.get("schema") == "minicode-memory-inventory-v1":
        return dict(item)
    raw_stats = item.get("stats") if isinstance(item.get("stats"), Mapping) else item
    raw_scope_counts = item.get("scope_counts", item.get("scopeCounts", {}))
    scopes: list[dict[str, Any]] = []
    if isinstance(raw_stats, Mapping):
        for name, raw in raw_stats.items():
            if name in {"schema", "workspace", "captured_at", "state_paths", "scope_counts", "pipeline_stats"}:
                continue
            stats = _mapping(raw)
            if not stats:
                continue
            scopes.append(
                {
                    "name": _text(name, 60),
                    "entries": _integer(stats.get("entries", 0)),
                    "sizeBytes": _integer(stats.get("size_bytes", stats.get("sizeBytes", 0))),
                    "categories": _strings(stats.get("categories", []), limit=12, item_limit=60),
                }
            )
    if not scopes and isinstance(raw_scope_counts, Mapping):
        scopes = [
            {"name": _text(name, 60), "entries": _integer(count), "sizeBytes": 0, "categories": []}
            for name, count in raw_scope_counts.items()
        ]
    return {
        "schema": _text(item.get("schema"), 80),
        "capturedAt": _text(item.get("captured_at", item.get("capturedAt", "")), 80),
        "scopes": scopes,
    }


def public_context_stats(value: Any) -> dict[str, Any]:
    from minicode.session_context import public_budget_policy

    item = _mapping(value)
    accounting = _mapping(item.get("accounting"))
    request = _mapping(item.get("request"))
    return {
        "totalTokens": _integer(item.get("total_tokens", item.get("totalTokens", 0))),
        "estimatedTokens": _integer(item.get("estimated_tokens", item.get("estimatedTokens", 0))),
        "providerUsageTokens": _integer(item.get("provider_usage_tokens", item.get("providerUsageTokens", 0))),
        "contextWindow": _integer(item.get("context_window", item.get("contextWindow", 0))),
        "effectiveInput": _integer(item.get("effective_input", item.get("effectiveInput", 0))),
        "admissionInputTokens": _optional_number(item.get("admission_input_tokens")),
        "contextGuardTokens": _optional_number(item.get("context_guard_tokens")),
        "outputReserveTokens": _optional_number(item.get("output_reserve_tokens")),
        "safetyReserveTokens": _optional_number(item.get("safety_reserve_tokens")),
        "reservedTokens": _optional_number(item.get("reserved_tokens")),
        "capacityError": _text(item.get("capacity_error"), 80),
        "budgetPolicy": public_budget_policy(item.get("budget_policy")),
        "utilization": max(0.0, min(1.0, _number(item.get("utilization", 0)))),
        "warningLevel": _text(item.get("warning_level", item.get("warningLevel", "normal")), 40),
        "request": {
            "requestId": _text(request.get("request_id"), 160),
            "nodeId": _text(request.get("node_id"), 40),
            "model": _text(request.get("model"), 160),
            "capturedAt": _number(request.get("captured_at")) * 1000,
            "method": _text(request.get("method"), 40),
            "hasUnmeasuredMedia": bool(request.get("has_unmeasured_media")),
        } if request else None,
        "breakdown": [{"key": part["key"], "estimatedTokens": max(0, _integer(part.get("estimated_tokens")))}
                      for part in item.get("breakdown", []) if isinstance(part, dict)
                      and part.get("key") in {"system", "tools", "conversation", "memory", "other"}],
        "accounting": {
            "source": _text(accounting.get("source") or "estimate_only", 60),
            "isExact": bool(accounting.get("is_exact", accounting.get("isExact", False))),
            "stale": bool(accounting.get("stale", False)),
            "reason": _text(accounting.get("reason"), 180),
        },
    }


def _public_artifact(value: Any) -> dict[str, Any]:
    item = _mapping(value)
    return {
        "artifactId": _text(item.get("artifact_id", item.get("artifactId", "")), 140),
        "contentHash": _text(item.get("content_hash", item.get("contentHash", "")), 160),
        "size": _integer(item.get("size", 0)),
        "mediaType": _text(item.get("media_type", item.get("mediaType", "")), 100),
    }


def _optional_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _workspace_evidence_path(workspace: str | Path, value: Any) -> str | None:
    """Only expose local file evidence within the actual workspace boundary."""
    raw = str(value or "").strip()
    if not raw or ":" in raw.replace(":\\", "", 1).replace(":/", "", 1):
        return None
    try:
        root = Path(workspace).resolve()
        path = Path(raw)
        if ".." in raw.replace("\\", "/").split("/"):
            return None
        if not path.is_absolute() and PureWindowsPath(raw).drive:
            return None
        resolved = (path if path.is_absolute() else root / path).resolve()
        relative = resolved.relative_to(root).as_posix()
        return relative if relative != "." else None
    except (OSError, RuntimeError, ValueError):
        return None


def _public_cache_usage(value: Any) -> dict[str, Any] | None:
    item = _mapping(value)
    if not item:
        return None
    def totals(entry):
        rate = _optional_number(entry.get("hit_rate"))
        return {"inputTokens": _optional_number(entry.get("input_tokens")),
                "cacheReadTokens": _optional_number(entry.get("cache_read_tokens")),
                "hitRate": rate if rate is not None and 0 <= rate <= 1 else None,
                "reportedCalls": _optional_number(entry.get("reported_calls")),
                "modelCalls": _optional_number(entry.get("model_calls")),
                "complete": entry.get("complete") is True}
    return {**totals(item), "groups": [
        {"key": group["key"], **totals(group)} for group in item.get("groups", [])
        if isinstance(group, dict) and group.get("key") in {"primary", "agents", "memory_rerank", "other"}]}


def _public_usage(value: Any, *, ledger: Any = None) -> dict[str, Any] | None:
    item = _mapping(value)
    if not item:
        return None
    tokens = _optional_number(item.get("tokens", item.get("tokens_used")))
    cost = _optional_number(item.get("cost", item.get("cost_used")))
    cost_known = item.get("cost_known", item.get("costKnown")) is True and cost is not None
    cache = item.get("cache")
    if cache is None and isinstance(ledger, dict) and isinstance(ledger.get("calls"), dict):
        from minicode.usage_ledger import cache_usage_summary
        cache = cache_usage_summary(ledger["calls"], legacy_calls=sum(
            int(row.get("model_calls", 0) or 0) for row in ledger.get("result_usage", {}).values()))
    return {
        "tokens": tokens,
        "cost": cost if cost_known else None,
        "knownCost": _optional_number(item.get("known_cost", item.get("knownCost", cost))),
        "tokensKnown": item.get("tokens_known", item.get("tokensKnown")) is True and tokens is not None,
        "costKnown": cost_known,
        "modelCalls": _optional_number(item.get("model_calls", item.get("modelCalls"))),
        "reservedTokens": _optional_number(item.get("reserved_tokens", item.get("reservedTokens"))),
        "reservedCost": _optional_number(item.get("reserved_cost", item.get("reservedCost"))),
        "unsettledCalls": _optional_number(item.get("unsettled_calls", item.get("unsettledCalls"))),
        "cache": _public_cache_usage(cache),
    }


def _public_evidence(workspace: str | Path, value: Any) -> dict[str, Any]:
    item = _mapping(value)
    return {
        "evidenceType": _text(item.get("evidence_type", item.get("evidenceType", "")), 80),
        "contentHash": _text(item.get("content_hash", item.get("contentHash", "")), 160),
        "claim": _text(item.get("claim"), 300),
        "producerNodeId": _text(item.get("producer_node_id", item.get("producerNodeId", "")), 120),
        "baseRevision": _text(item.get("base_revision", item.get("baseRevision", "")), 160),
        "lineStart": item.get("line_start", item.get("lineStart")),
        "lineEnd": item.get("line_end", item.get("lineEnd")),
        "page": item.get("page"),
        "file": _workspace_evidence_path(workspace, item.get("resource"))
        if item.get("evidence_type", item.get("evidenceType")) == "file-content" else None,
    }


def _public_project_check(item: Mapping) -> dict[str, Any] | None:
    if item.get("verifier") != "npm-build-and-browser" and not str(item.get("verifier", "")).startswith("browser-scenario-"):
        return None
    try:
        payload = json.loads(item.get("details") or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("recipe") != "npm-build-and-browser":
        return None
    stages = payload.get("stages")
    artifacts = payload.get("artifacts")
    browser = _mapping(payload.get("browser"))
    return {
        "scope": payload.get("scope") if payload.get("scope") in {"build-only", "browser-scenario", "build-and-browser-smoke"} else "build-and-browser-smoke",
        "criteria": [{"number": value["number"], "text": _text(value.get("text"), 500), "status": value["status"]}
            for value in (browser.get("criterion_results", [])[:64] if isinstance(browser.get("criterion_results"), list) else [])
            if isinstance(value, dict) and value.get("required") is True and type(value.get("number")) is int
            and value.get("status") in {"passed", "failed", "not_checked"}],
        "candidateRevision": _text(payload.get("candidate_revision"), 128),
        "execution": payload.get("execution") if payload.get("execution") in {"executed", "reused", "not-started"} else "executed",
        "failure": _text(payload.get("failure"), 2000),
        "stages": [{"stage": _text(stage.get("stage"), 60), "exitCode": stage.get("exit_code") if type(stage.get("exit_code")) is int else None,
                    "output": _raw_text(stage.get("output"), 6000)} for stage in (stages[:3] if isinstance(stages, list) else []) if isinstance(stage, dict)],
        "browserErrors": _strings(browser.get("errors", []), limit=8, item_limit=1000),
        "artifacts": [{"name": artifact["name"], "artifactId": artifact["ref"]["artifact_id"]}
                      for artifact in (artifacts[:8] if isinstance(artifacts, list) else [])
                      if isinstance(artifact, dict) and isinstance(artifact.get("name"), str) and re.fullmatch(
                          r"report\.json|(desktop|mobile)(?:-[A-Za-z0-9_-]{1,64})?\.png|(desktop|mobile)-trace\.zip", artifact["name"])
                      and isinstance(artifact.get("ref"), dict)
                      and isinstance(artifact["ref"].get("artifact_id"), str)
                      and re.fullmatch(r"artifact-[a-f0-9]{32}", artifact["ref"]["artifact_id"])],
    }


def _public_verification(workspace: str | Path, value: Any) -> dict[str, Any]:
    item = _mapping(value)
    raw_evidence = item.get("evidence_refs", item.get("evidenceRefs", []))
    evidence = [_public_evidence(workspace, raw) for raw in raw_evidence[:50]] if isinstance(raw_evidence, list) else []
    return {
        "verifier": _text(item.get("verifier"), 140),
        "passed": bool(item.get("passed", False)),
        "scope": _text(item.get("scope"), 100),
        "details": _text(item.get("details"), 500),
        **({"projectCheck": project} if (project := _public_project_check(item)) is not None else {}),
        "evidence": evidence,
        "deterministic": bool(item.get("deterministic", True)),
        "coveredCriteriaIds": _strings(
            item.get("covered_criteria_ids", item.get("coveredCriteriaIds", [])),
            limit=50,
            item_limit=100,
        ),
        "createdAt": round(_number(item.get("created_at", item.get("createdAt", 0))) * 1000),
    }


def _public_result(workspace: str | Path, value: Any) -> dict[str, Any] | None:
    item = _mapping(value)
    if not item:
        return None
    raw_artifacts = item.get("artifact_refs", item.get("artifactRefs", []))
    raw_verifications = item.get("verification_records", item.get("verificationRecords", []))
    changed = item.get("changed_resources", item.get("changedResources", []))
    failure = _mapping(item.get("failure"))
    return {
        "status": _text(item.get("status"), 60),
        "outputSummary": _text(item.get("output_summary", item.get("outputSummary", "")), 900),
        "changedResources": [
            label
            for label in (_relative_resource(workspace, raw) for raw in changed[:30])
            if label
        ] if isinstance(changed, list) else [],
        "artifacts": [_public_artifact(raw) for raw in raw_artifacts[:20]] if isinstance(raw_artifacts, list) else [],
        "verificationRecords": [_public_verification(workspace, raw) for raw in raw_verifications[:20]] if isinstance(raw_verifications, list) else [],
        "usage": _public_usage(item.get("usage")),
        "metrics": {
            public: _optional_number(_mapping(item.get("metrics")).get(private))
            for public, private in (
                ("elapsedSeconds", "elapsed_seconds"), ("modelCalls", "model_calls"),
                ("modelSeconds", "model_seconds"), ("toolCalls", "tool_calls"),
                ("toolSeconds", "tool_seconds"),
            )
        } if _mapping(item.get("metrics")) else None,
        "warnings": _strings(item.get("warnings", []), limit=12, item_limit=300),
        "failure": {
            "kind": _text(failure.get("kind"), 80),
            "message": _text(failure.get("message"), 500),
            "retryable": bool(failure.get("retryable", False)),
            "ambiguousSideEffect": bool(failure.get("ambiguous_side_effect", failure.get("ambiguousSideEffect", False))),
        } if failure else None,
    }


def _public_agent_collaboration(state: Mapping[str, Any]) -> dict[str, Any] | None:
    if "agent_sessions" not in state:
        return None
    from minicode.runtime_state import RuntimeReducer
    try:
        reducer = RuntimeReducer.restore(state)
        return {"team": reducer.agent_team_view(), "waits": reducer.agent_wait_projection(), "agents": [
            {**reducer.agent_task_projection(record),
             **({"name": redact_sensitive_text(record["name"])} if "name" in record else {}),
             "objective": redact_sensitive_text(_text(record["definition"]["objective"], 1000))}
            for record in reducer.agent_sessions().values()],
            "messages": [{"messageId": record["message_id"], "senderId": record["sender_id"],
                "recipientId": record["recipient_id"], "kind": record["kind"], "state": record["state"],
                "sequence": record["sequence"], "content": redact_sensitive_text(_text(record["content"], 1200)),
                "reason": redact_sensitive_text(_text(record.get("reason"), 300)), "replyTo": record.get("reply_to")}
                for record in reducer.agent_message_records()]}
    except (ValueError, RuntimeError, TypeError, KeyError):
        return {"team": None, "agents": [], "messages": [], "error": "Agent collaboration state failed integrity validation"}


def public_runtime_trace(value: Any, workspace: str | Path) -> dict[str, Any]:
    from minicode.runtime_activity import collaboration_view
    from minicode.runtime_resilience import child_recovery
    from minicode.web_run_continuation import continuation_view
    trace = _mapping(value)
    pending_reason = review_pending_reason(trace)
    profile = _mapping(trace.get("profile"))
    collaboration = collaboration_view(profile)
    topology = _mapping(trace.get("topology_decision", trace.get("topologyDecision", {})))
    plan = _mapping(trace.get("plan"))
    scheduler = _mapping(trace.get("scheduler"))
    delivery = _mapping(trace.get("delivery"))
    state = _mapping(trace.get("state"))
    state_schema = state.get("schema_version")
    if state_schema == "minicode-runtime-state-v21":
        state_schema = state.get("review_base_schema")
        observed_nodes = plan.get("nodes", [])
        primary = next((_mapping(node) for node in (observed_nodes if isinstance(observed_nodes, list) else [])
            if isinstance(node, Mapping) and node.get("node_id") == "primary"), {})
        if _review_policy_fields(primary.get("review_policy")):
            profile = {**profile, "review_policy": primary["review_policy"]}
    if state_schema == "minicode-runtime-state-v20":
        state_schema = state.get("agent_collaboration_base_schema")
    if state_schema == "minicode-runtime-state-v19":
        state_schema = state.get("user_steering_base_schema")
    if state_schema in {f"minicode-runtime-state-v{i}" for i in range(12, 19)}:
        state_schema = state.get("agent_base_schema")
    if state_schema == "minicode-runtime-state-v11":
        state_schema = state.get("orchestration_base_schema")
    run_id = plan.get("run_id", state.get("run_id", scheduler.get("run_id", "")))
    completion = (
        entry_completion(trace, expected_run_id=run_id)
        if isinstance(run_id, str) and run_id
        else EntryCompletion(False, "invalid_runtime_trace")
    )
    delivery_reasons = _strings(delivery.get("reasons", []), limit=12, item_limit=300)
    if pending_reason:
        delivery_reasons = [pending_reason, *delivery_reasons][:12]
    if completion.reason != "runtime_completion_gate":
        delivery_reasons = [completion.message, *delivery_reasons][:12]
    contract = _mapping(trace.get("contract", state.get("contract")))
    reserve = _mapping(state.get("closure_budget")) if state_schema in {"minicode-runtime-state-v7", "minicode-runtime-state-v8", "minicode-runtime-state-v9", "minicode-runtime-state-v10"} else {}

    nodes: list[dict[str, Any]] = []
    raw_nodes = plan.get("nodes", [])
    state_nodes = _mapping(state.get("nodes"))
    if isinstance(raw_nodes, list):
        for raw in raw_nodes[:50]:
            definition = _mapping(raw)
            node_id = _text(definition.get("node_id", definition.get("nodeId", "")), 120)
            runtime_node = _mapping(state_nodes.get(node_id))
            attempts = runtime_node.get("attempts", [])
            nodes.append(
                {
                    "nodeId": node_id,
                    "objective": _text(definition.get("objective"), 700),
                    "role": _text(definition.get("role"), 80),
                    "dependencies": _strings(definition.get("dependencies", []), limit=20, item_limit=120),
                    "allowedEffects": _strings(definition.get("allowed_effects", definition.get("allowedEffects", [])), limit=12, item_limit=80),
                    "acceptanceCriteria": _strings(definition.get("acceptance_criteria", definition.get("acceptanceCriteria", [])), limit=12, item_limit=300),
                    "maxAttempts": _integer(definition.get("max_attempts", definition.get("maxAttempts", 1)), 1),
                    "state": _text(runtime_node.get("state") or "PENDING", 60),
                    "criticality": "advisory" if definition.get("criticality") == "advisory" else "required",
                    **_review_policy_fields(definition.get("review_policy", definition.get("reviewPolicy"))),
                    **({"reviewStatus": "unavailable"} if runtime_node.get("review_status") == "unavailable" else {}),
                    "diagnosis": {key: _mapping(runtime_node.get("failure_diagnosis")).get(key)
                                  for key in ("category", "failureKind", "fallbackAllowed", "acknowledged")},
                    "advisoryReason": redact_sensitive_text(_text(_mapping(runtime_node.get("advisory_acknowledgment")).get("reason"), 2000)),
                    "attemptCount": len(attempts) if isinstance(attempts, list) else 0,
                    "recovery": child_recovery(node_id, _text(runtime_node.get("state"), 60),
                        _strings(definition.get("allowed_effects", []), limit=12, item_limit=80),
                        [_mapping(attempt) for attempt in attempts] if isinstance(attempts, list) else []),
                    "rejectionReasons": _strings(runtime_node.get("rejection_reasons", runtime_node.get("rejectionReasons", [])), limit=8, item_limit=300),
                    "result": _public_result(workspace, runtime_node.get("result")),
                }
            )

    raw_delivery_artifacts = delivery.get("artifact_refs", delivery.get("artifactRefs", []))
    raw_delivery_verification = delivery.get("verification_records", delivery.get("verificationRecords", []))
    context_manifests = trace.get("context_manifests", trace.get("contextManifests", []))
    raw_reasoning_decisions = trace.get(
        "reasoning_budget_decisions",
        trace.get("reasoningBudgetDecisions", []),
    )
    raw_acceptance_criteria = trace.get(
        "acceptance_criteria",
        trace.get("acceptanceCriteria", []),
    )
    context: list[dict[str, Any]] = []
    if isinstance(context_manifests, list):
        for raw in context_manifests[:50]:
            manifest = _mapping(raw)
            included_files = manifest.get("included_files", manifest.get("includedFiles", {}))
            dependencies = manifest.get("dependency_result_refs", manifest.get("dependencyResultRefs", []))
            artifacts = manifest.get("artifact_refs", manifest.get("artifactRefs", []))
            context.append(
                {
                    "nodeId": _text(manifest.get("node_id", manifest.get("nodeId", "")), 120),
                    "baseRevision": _text(manifest.get("base_revision", manifest.get("baseRevision", "")), 160),
                    "includedFileCount": len(included_files) if isinstance(included_files, Mapping) else 0,
                    "dependencyCount": len(dependencies) if isinstance(dependencies, list) else 0,
                    "artifactCount": len(artifacts) if isinstance(artifacts, list) else 0,
                    "toolCapabilityScope": _strings(manifest.get("tool_capability_scope", manifest.get("toolCapabilityScope", [])), limit=12, item_limit=80),
                    "retryFeedback": [redact_sensitive_text(item) for item in _strings(manifest.get("retry_feedback", manifest.get("retryFeedback", [])), limit=3, item_limit=4_000)],
                }
            )

    reasoning_decisions: list[dict[str, Any]] = []
    if isinstance(raw_reasoning_decisions, list):
        for raw in raw_reasoning_decisions[:50]:
            decision = _mapping(raw)
            reasoning_decisions.append(
                {
                    "nodeId": _text(decision.get("node_id", decision.get("nodeId", "")), 120),
                    "model": _text(decision.get("model"), 160),
                    "provider": _text(decision.get("provider"), 80),
                    "requested": _text(decision.get("requested"), 40),
                    "applied": _text(decision.get("applied"), 40),
                    "degraded": bool(decision.get("degraded", False)),
                    "reason": _text(decision.get("reason"), 300),
                }
            )

    return {
        "schemaVersion": _text(trace.get("schema_version", trace.get("schemaVersion", "")), 80),
        "runId": _text(run_id, 160),
        "reviewPending": bool(pending_reason),
        "reviewCloseout": redact_sensitive_text(review_pending_closeout(trace)) if pending_reason else "",
        "webRunId": _text(trace.get("web_run_id", trace.get("webRunId", "")), 160),
        **({"agentCollaboration": _public_agent_collaboration(state)} if "agent_sessions" in state else {}),
        **({"consultationBudget": {"policy": "shared-pool-v1", "allocation": "atomic_request_ledger",
                                  "minimumRequestOutput": "full_configured_output"}}
           if (state_schema == "minicode-runtime-state-v9" or (
               state_schema == "minicode-runtime-state-v10"
               and state.get("base_state_schema_version") == "minicode-runtime-state-v9"))
           and state.get("consultation_budget_policy") == "shared-pool-v1" else {}),
        **({"closureBudget": {
            "primaryTokens": _optional_number(reserve.get("primary_tokens")),
            "verificationTokens": _optional_number(reserve.get("verification_tokens")),
            "primaryCost": _optional_number(reserve.get("primary_cost")),
            "verificationCost": _optional_number(reserve.get("verification_cost")),
        }} if reserve.get("version") == "closure-budget-v1" else {}),
        "profile": {
            **({"collaboration": collaboration} if collaboration is not None else {}),
            "requestedMode": _text(profile.get("requested_mode", profile.get("requestedMode", "")), 40),
            "selectedRoute": _text(profile.get("selected_route", profile.get("selectedRoute", "")), 80),
            "reasoningBudget": _text(profile.get("reasoning_budget", profile.get("reasoningBudget", "")), 40),
            "maxActiveWorkers": _integer(profile.get("max_active_workers", profile.get("maxActiveWorkers", 0))),
            "verificationLevel": _text(profile.get("verification_level", profile.get("verificationLevel", "")), 40),
            **({"primaryRuntimeProfile": _text(profile["primary_runtime_profile"], 40)}
               if profile.get("primary_runtime_profile") else {}),
            **_review_policy_fields(profile.get("review_policy", profile.get("reviewPolicy"))),
            "writeIsolation": _text(profile.get("write_isolation", profile.get("writeIsolation", "")), 60),
            "maxTokens": _optional_number(profile.get("max_tokens", profile.get("maxTokens"))),
            "maxCost": _optional_number(profile.get("max_cost", profile.get("maxCost"))),
            "deadline": _optional_number(profile.get("deadline")),
        },
        "topology": {
            "selectedRoute": _text(topology.get("selected_route", topology.get("selectedRoute", "")), 80),
            "expectedParallelGain": _number(topology.get("expected_parallel_gain", topology.get("expectedParallelGain", 0))),
            "writeConflictRisk": _text(topology.get("write_conflict_risk", topology.get("writeConflictRisk", "")), 80),
            "deterministicVerifiability": bool(topology.get("deterministic_verifiability", topology.get("deterministicVerifiability", False))),
            "requiresWorkspaceWrite": bool(topology.get("requires_workspace_write", topology.get("requiresWorkspaceWrite", False))),
            "reasons": _strings(topology.get("reasons", []), limit=10, item_limit=300),
        },
        "plan": {
            "planVersion": _integer(plan.get("plan_version", plan.get("planVersion", 0))),
            "contractRevision": _integer(plan.get("contract_revision", plan.get("contractRevision", 0))),
            "nodes": nodes,
        },
        "scheduler": {
            "completed": bool(scheduler.get("completed", False)),
            "canceled": bool(scheduler.get("canceled", False)),
            "claimedAttempts": _integer(scheduler.get("claimed_attempts", scheduler.get("claimedAttempts", 0))),
            "peakActiveWorkers": _integer(scheduler.get("peak_active_workers", scheduler.get("peakActiveWorkers", 0))),
            "peakActiveWriters": _integer(scheduler.get("peak_active_writers", scheduler.get("peakActiveWriters", 0))),
            "reasons": _strings(scheduler.get("reasons", []), limit=12, item_limit=300),
            "unsettledTaskIds": _strings(scheduler.get("unsettled_task_ids", scheduler.get("unsettledTaskIds", [])),
                                         limit=128, item_limit=200),
            "usage": _public_usage(state.get("usage_summary") or scheduler, ledger=state.get("usage_ledger")),
        },
        "delivery": {
            "completed": completion.completed,
            "outputSummary": _text(delivery.get("output_summary", delivery.get("outputSummary", "")), 900),
            "artifacts": [_public_artifact(raw) for raw in raw_delivery_artifacts[:30]] if isinstance(raw_delivery_artifacts, list) else [],
            "verificationRecords": [_public_verification(workspace, raw) for raw in raw_delivery_verification[:50]] if isinstance(raw_delivery_verification, list) else [],
            "reasons": delivery_reasons,
        },
        "contextManifests": context,
        "taskMemory": task_memory_view(trace),
        "taskFile": public_task_file(trace.get("task_file")),
        "runBudget": public_run_budget(trace.get("web_run_budget")),
        "continuationContractSaved": bool(trace.get("web_task_snapshot")),
        "continuation": continuation_view(trace.get("web_continuation")),
        "acceptanceCriteria": [
            {
                "id": _text(_mapping(raw).get("id"), 100),
                "text": _text(_mapping(raw).get("text"), 500),
            }
            for raw in raw_acceptance_criteria[:50]
        ] if isinstance(raw_acceptance_criteria, list) else [],
        "reasoningBudgetDecisions": reasoning_decisions,
        "expectedOutputs": [
            _workspace_evidence_path(workspace, raw)
            for raw in _strings(contract.get("expected_outputs", []), limit=50, item_limit=4_096)
        ],
    }


def public_checkpoints(items: Any, workspace: str | Path) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in items[-80:]:
        item = _mapping(raw)
        result.append(
            {
                "checkpointId": _text(item.get("checkpoint_id", item.get("checkpointId", "")), 160),
                "createdAt": round(_number(item.get("created_at", item.get("createdAt", 0))) * 1000),
                "file": _relative_resource(workspace, item.get("file_path", item.get("file", ""))),
                "existed": bool(item.get("existed", False)),
                "kind": _text(item.get("kind") or "edit", 60),
                "groupId": _text(item.get("group_id", item.get("groupId", "")), 160),
            }
        )
    return [item for item in result if item["checkpointId"]]


def capability_surface(
    registry: Any,
    *,
    product_snapshot: Any,
    memory: Any = None,
    mcp_servers: Any = None,
    source: str = "workspace",
) -> dict[str, Any]:
    product = public_product_snapshot(product_snapshot)
    return {
        "source": source,
        "skills": public_skills(getattr(registry, "get_skills", list)()),
        "mcpServers": public_mcp_servers(
            mcp_servers if mcp_servers is not None else getattr(registry, "get_mcp_servers", list)()
        ),
        "tools": public_tools(registry),
        "memory": public_memory(memory),
        **product,
    }
