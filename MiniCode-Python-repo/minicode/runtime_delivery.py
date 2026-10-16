"""Small, read-only delivery receipts shared by product entry surfaces.

This projects the Runtime's verdict; it does not replace CompletionGate or
infer success from an assistant message, a Done event, or a memory write.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class EntryCompletion:
    completed: bool
    reason: str

    @property
    def message(self) -> str:
        return {
            "runtime_completion_gate": "Runtime 验收通过。" if self.completed else "Runtime 验收未通过，任务尚未完成。",
            "missing_runtime_trace": "缺少本轮 Runtime 验收回执，不能确认任务完成。",
            "invalid_runtime_trace": "本轮 Runtime 验收回执无效，不能确认任务完成。",
            "mismatched_runtime_trace": "Runtime 验收回执与当前运行不匹配，不能确认任务完成。",
        }[self.reason]


def entry_completion(trace: Any, *, expected_run_id: str | None = None) -> EntryCompletion:
    if trace is None:
        return EntryCompletion(False, "missing_runtime_trace")
    delivery = trace.get("delivery") if isinstance(trace, Mapping) else None
    if not isinstance(delivery, Mapping) or type(delivery.get("completed")) is not bool:
        return EntryCompletion(False, "invalid_runtime_trace")
    state = trace.get("state")
    if (not isinstance(state, Mapping) or not isinstance(state.get("run_id"), str) or not state["run_id"]
            or not isinstance(delivery.get("run_id"), str) or not delivery["run_id"]):
        return EntryCompletion(False, "invalid_runtime_trace")
    if state["run_id"] != delivery["run_id"] or (expected_run_id and state["run_id"] != expected_run_id):
        return EntryCompletion(False, "mismatched_runtime_trace")
    return EntryCompletion(delivery["completed"], "runtime_completion_gate")


def _current_review_rejection(state: Mapping, node_id: str, node: Mapping) -> Mapping:
    attempt_id = node.get("current_attempt_id")
    events = state.get("events", ())
    if not attempt_id or not isinstance(events, (list, tuple)):
        return {}
    for event in reversed(events):
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        if (event.get("event_type") == "VerificationFailed" and event.get("run_id") == state.get("run_id")
                and isinstance(payload, Mapping) and payload.get("node_id") == node_id
                and payload.get("attempt_id") == attempt_id):
            return payload
    return {}


def _interrupted_skill_review(state: Mapping) -> Mapping:
    """Keep an authenticated prior verdict when native Skill replay stopped repair."""
    nodes, history, events = state.get("nodes"), state.get("repair_history"), state.get("events")
    if not isinstance(nodes, Mapping) or not isinstance(history, (list, tuple)) or not history or not isinstance(events, (list, tuple)):
        return {}
    primary = nodes.get("primary") or {}
    if not isinstance(primary, Mapping) or primary.get("state") != "FAILED_TERMINAL":
        return {}
    result = primary.get("result") or {}
    failure = result.get("failure") or {}
    if failure.get("kind") != "ToolFailed" or failure.get("ambiguous_side_effect"):
        return {}
    try:
        receipt = json.loads(failure.get("message", ""))
    except (ValueError, TypeError):
        return {}
    if (not isinstance(receipt, Mapping)
            or receipt.get("error") != "Tool unavailable in the current scope; no operation was executed."
            or receipt.get("unavailable_names") != ["load_skill"]
            or receipt.get("unavailable_reasons") != {"load_skill": "effect_restricted"}):
        return {}
    repair = history[-1]
    if not isinstance(repair, Mapping) or repair.get("run_id") != state.get("run_id") or repair.get("node_id") != "primary":
        return {}
    review = repair.get("review_result") or {}
    if (not isinstance(review, Mapping) or review.get("run_id") != state.get("run_id")
            or review.get("node_id") != "final-verifier" or review.get("failure", {}).get("kind") != "VerificationFailed"):
        return {}
    repair_event = next((event for event in events if isinstance(event, Mapping)
        and event.get("event_type") == "RepairRequested" and event.get("run_id") == state.get("run_id")
        and event.get("payload", {}).get("repair") == repair), None)
    if repair_event is None or not any(isinstance(event, Mapping) and event.get("event_type") == "ResultAccepted"
        and event.get("run_id") == state.get("run_id") and event.get("sequence", 0) > repair_event.get("sequence", 0)
        and event.get("payload", {}).get("node_id") == "primary"
        and event.get("payload", {}).get("attempt_id") == primary.get("current_attempt_id")
        and event.get("payload", {}).get("reason") == failure.get("message") for event in events):
        return {}
    return review


def _interrupted_consultation_review(state: Mapping) -> Mapping:
    """Bind a failed primary resume to the review actually delivered to it."""
    nodes, events = state.get("nodes"), state.get("events")
    if not isinstance(nodes, Mapping) or not isinstance(events, (list, tuple)):
        return {}
    primary = nodes.get("primary")
    if not isinstance(primary, Mapping) or primary.get("state") != "FAILED_TERMINAL":
        return {}
    result = primary.get("result") or {}
    if (not isinstance(result, Mapping) or result.get("run_id") != state.get("run_id")
            or result.get("node_id") != "primary"
            or result.get("attempt_id") != primary.get("current_attempt_id")):
        return {}
    for event in reversed(events):
        if not isinstance(event, Mapping) or event.get("run_id") != state.get("run_id"):
            continue
        payload = event.get("payload") or {}
        if (event.get("event_type") != "ConsultationDelivered" or not isinstance(payload, Mapping)
                or payload.get("node_id") != "primary" or payload.get("attempt_id") != result.get("attempt_id")
                or not isinstance(payload.get("children"), Mapping)):
            continue
        for node_id, attempt_id in payload["children"].items():
            node = nodes.get(node_id)
            if (not isinstance(node, Mapping) or node.get("state") != "FAILED_TERMINAL"
                    or node.get("current_attempt_id") != attempt_id
                    or (node.get("definition") or {}).get("criticality", "required") != "required"
                    or (node.get("definition") or {}).get("role") not in {"reviewer", "verifier"}):
                continue
            review = node.get("result") or {}
            if (isinstance(review, Mapping) and review.get("run_id") == state.get("run_id")
                    and review.get("node_id") == node_id and review.get("attempt_id") == attempt_id
                    and any(isinstance(accepted, Mapping) and accepted.get("event_type") == "ResultAccepted"
                            and accepted.get("run_id") == state.get("run_id")
                            and accepted.get("sequence", 0) > event.get("sequence", 0)
                            and accepted.get("payload", {}).get("node_id") == "primary"
                            and accepted.get("payload", {}).get("attempt_id") == result.get("attempt_id")
                            and accepted.get("payload", {}).get("reason") == (result.get("failure") or {}).get("message")
                            for accepted in events)):
                return review
    return {}


def review_pending_reason(trace: Any, *, expected_run_id: str | None = None) -> str:
    """Recognize a native review stop, never a helper's claim or a passed task."""
    completion = entry_completion(trace, expected_run_id=expected_run_id)
    if completion.completed or completion.reason != "runtime_completion_gate":
        return ""
    state = trace["state"]
    if state.get("canceled") or state.get("paused"):
        return ""
    prefix = "Review recovery pending: "
    raw_reasons = trace["delivery"].get("reasons", ())
    nodes = state.get("nodes", {})
    if not isinstance(raw_reasons, (list, tuple)) or not isinstance(nodes, Mapping):
        return ""
    reasons = list(raw_reasons)
    from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
    policy = createApp()
    for node_id, node in nodes.items():
        if not isinstance(node, Mapping):
            return ""
        if node.get("state") != "FAILED_TERMINAL":
            continue
        result = node.get("result") or {}
        if not isinstance(result, Mapping):
            return ""
        failure = result.get("failure") or {}
        definition = node.get("definition") or {}
        if not isinstance(failure, Mapping) or not isinstance(definition, Mapping):
            return ""
        kind = failure.get("kind") or ("VerificationFailed" if _current_review_rejection(state, node_id, node) else "")
        if not isinstance(kind, str):
            return ""
        if not policy.review_failure_is_recoverable(
            kind, ambiguous=bool(failure.get("ambiguous_side_effect")),
        ):
            return ""
        if definition.get("role") == "primary" and kind == "VerificationFailed":
            reasons.append(failure.get("message", ""))
    return next((reason for reason in reasons if isinstance(reason, str) and reason.startswith(prefix)), "") or (
        prefix + "selected Skill reload blocked during repair" if _interrupted_skill_review(state) else
        prefix + "consultation resume failed" if _interrupted_consultation_review(state) else "")


def review_pending_closeout(trace: Any) -> str:
    """Bounded evidence-based closeout that works even when model budget is gone."""
    reason = review_pending_reason(trace)
    if not reason:
        return ""
    if "selected Skill reload blocked" in reason:
        opening = "复核意见已交回主 Agent，但工作流恢复被工具范围限制拦截，修复未能继续。"
    elif "consultation resume failed" in reason:
        opening = "复核意见已交回主 Agent，但恢复执行时发生错误，自动处理未能继续。"
    elif "round budget exhausted" in reason:
        opening = "本轮自动修复与复核已达到次数上限，仍有问题未解决。"
    elif "no workspace progress" in reason or "repeated a previous candidate" in reason:
        opening = "再次修复后仍未形成有效的新修改，自动复核已停止。"
    else:
        opening = "本轮复核仍缺少继续所需的条件，自动处理已停止。"
    findings = []
    if "consultation resume failed" in reason:
        failure = trace["state"]["nodes"]["primary"]["result"].get("failure") or {}
        findings.append("恢复错误：" + str(failure.get("message") or "主 Agent 未能恢复执行"))
    for node_id, node in trace["state"].get("nodes", {}).items():
        if (node.get("state") != "FAILED_TERMINAL"
                or node.get("definition", {}).get("role") not in {"reviewer", "verifier"}):
            continue
        result = node.get("result") or {}
        failure = result.get("failure") or {}
        rejection = _current_review_rejection(trace["state"], node_id, node)
        record = rejection.get("record")
        details = record.get("details") if isinstance(record, Mapping) else ""
        text = str(failure.get("message") or details or rejection.get("reason") or result.get("output_summary") or "").strip()
        text = text.replace("<verification_failed>", "").strip()
        if text and text not in findings:
            findings.append(text)
    prior_review = _interrupted_skill_review(trace["state"])
    if prior_review:
        text = str(prior_review.get("failure", {}).get("message") or prior_review.get("output_summary") or "").strip()
        if text and text not in findings:
            findings.append(text.replace("<verification_failed>", "").strip())
    details = "\n\n".join(
        "> " + (item[:1000] + ("…（完整内容见复核记录）" if len(item) > 1000 else "")).replace("\n", "\n> ")
        for item in findings[:6]
    ) or "本轮没有取得可用的复核结论，不能据此确认任务完成。"
    if len(findings) > 6:
        details += f"\n\n另有 {len(findings) - 6} 项复核记录，可展开查看。"
    return (opening + "本轮尚未通过验收，已有工作记录和复核意见已保留。\n\n"
            + "待处理的复核意见：\n\n" + details
            + "\n\n处理上述问题或补齐缺少的条件后，可以从当前进度继续修改并重新复核。")


def review_pending_error_explained(trace: Any, message: Any) -> bool:
    """Only an authenticated native resume error is covered by its closeout."""
    if not review_pending_reason(trace) or not (
            _interrupted_skill_review(trace["state"]) or _interrupted_consultation_review(trace["state"])):
        return False
    return message == trace["state"]["nodes"]["primary"]["result"]["failure"]["message"]


def runtime_failure_message(trace: Any, *, expected_run_id: str | None = None) -> str:
    """Report native failure diagnostics, never an earlier assistant progress line."""
    completion = entry_completion(trace, expected_run_id=expected_run_id)
    if completion.completed or completion.reason != "runtime_completion_gate":
        return completion.message
    state = trace["state"]
    nodes = state.get("nodes", {})
    if isinstance(nodes, Mapping):
        ordered = sorted(nodes.items(), key=lambda item: item[0] != "primary")
        for node_id, node in ordered:
            if not isinstance(node, Mapping) or node.get("state") != "FAILED_TERMINAL":
                continue
            result = node.get("result") or {}
            failure = result.get("failure") if isinstance(result, Mapping) else None
            if (isinstance(failure, Mapping) and result.get("run_id") == state.get("run_id")
                    and result.get("node_id") == node_id and result.get("attempt_id") == node.get("current_attempt_id")
                    and isinstance(failure.get("message"), str) and failure["message"].strip()):
                return f"{failure.get('kind') or 'RuntimeError'}: {failure['message']}"
    reasons = trace["delivery"].get("reasons", ())
    details = "; ".join(item for item in reasons if isinstance(item, str) and item) if isinstance(reasons, (list, tuple)) else ""
    return completion.message + (" " + details if details else "")


_SKIP_REASONS = {
    "delivery_incomplete": "任务未完成，未记录成功历史。",
    "read_only_task": "本轮为只读任务，未新增任务历史。",
    "workspace_not_integrated": "工作区尚未合入，未记录成功历史。",
    "no_file_memory_manager": "本轮未提供对应的文件记忆管理器。",
    "paged_memory_owns_storage": "本轮采用分页或影子记忆，不由此文件记忆回执确认写入。",
}


def _identifier(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", value) else ""


def task_memory_view(trace: Any) -> dict[str, Any] | None:
    """Whitelist metadata, bind it to its run, omit filesystem paths and text."""
    if not isinstance(trace, Mapping) or "task_memory" not in trace:
        return None
    raw, state = trace.get("task_memory"), trace.get("state")
    invalid = {"status": "invalid", "reason": "invalid_receipt", "label": "历史记录回执无效",
        "detail": "不能据此确认任务历史已经保存。"}
    if (not isinstance(raw, Mapping) or not isinstance(state, Mapping)
            or raw.get("policy") != "verified-runtime-memory-v1"
            or not _identifier(raw.get("run_id")) or raw["run_id"] != state.get("run_id")):
        return invalid
    status = raw.get("status")
    if status == "skipped" and isinstance(raw.get("reason"), str) and raw["reason"] in _SKIP_REASONS:
        return {"status": status, "reason": raw["reason"], "runId": raw["run_id"],
            "label": "未新增任务历史", "detail": _SKIP_REASONS[raw["reason"]]}
    if status == "failed":
        return {"status": status, "reason": "storage_error", "runId": raw["run_id"],
            "label": "任务历史保存失败", "detail": "辅助记忆写入失败；代码交付是否完成仍以 Runtime 验收为准。请检查本地记忆存储与日志。"}
    if status not in ("stored", "already_recorded"):
        return invalid
    if (not entry_completion(trace, expected_run_id=raw["run_id"]).completed
            or state.get("integration_status") != "committed" or raw.get("scope") != "local"
            or not _identifier(raw.get("entry_id"))
            or any(not isinstance(raw.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", raw[key])
                   for key in ("content_sha256", "evidence_sha256"))):
        return invalid
    return {"status": status, "runId": raw["run_id"], "entryId": raw["entry_id"], "scope": "local",
        "contentSha256": raw["content_sha256"], "evidenceSha256": raw["evidence_sha256"],
        "label": "任务历史已保存" if status == "stored" else "任务历史已存在，未重复写入",
        "detail": "仅为已交付任务的历史记录，不代表完整记忆系统已验证，也不能证明当前文件仍然正确。"}


def task_memory_notice(trace: Any) -> str:
    receipt = task_memory_view(trace)
    return f"{receipt['label']}：{receipt['detail']}" if receipt else ""
