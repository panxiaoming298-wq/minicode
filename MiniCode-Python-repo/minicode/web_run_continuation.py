"""Explicit new runs seeded from owned failed primary work; never rewrite history."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from fastapi import HTTPException

from minicode.entry_task import restore_task
from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_state import RuntimeReducer, RuntimeStateStore
from minicode.usage_ledger import UsageLedger
from minicode.web_candidate_recovery import WebCandidateRecovery, _candidates, candidate_recovery_result
from minicode.workspace_provider import WorkspaceConflict, workspace_snapshot
from minicode.workspace_lock import normalize_workspace_path
from minicode.runtime_protocol import BudgetEnvelope, ArtifactRef
from minicode.web_runtime_settings import RunBudget

CONTINUATION_SETTINGS = ("taskConstraints", "acceptanceCriteria", "expectedOutputs", "taskRequiresWorkspaceWrite",
    "runtimeVerificationCommands", "runtimeVerificationIsolation", "requiredConsultation")


def retain_continuation_settings(trace, runtime):
    """Keep only requirements and explicit non-secret entry settings."""
    contract = continuation_contract(trace["state"])
    settings = {key: deepcopy(runtime.get(key)) for key in CONTINUATION_SETTINGS}
    for public, native in (("taskConstraints", "constraints"), ("acceptanceCriteria", "acceptance_criteria"),
                           ("expectedOutputs", "expected_outputs")):
        settings[public] = contract.get(native, [])
    trace["web_continuation_settings"] = settings
    for private, public in (("_webTaskSnapshot", "web_task_snapshot"), ("_webRunBudget", "web_run_budget"),
                            ("_webContinuationLineage", "web_continuation")):
        if runtime.get(private) is not None:
            trace[public] = deepcopy(runtime[private])
    if runtime.get("_headlessTaskFile") is not None:
        trace["task_file"] = deepcopy(runtime["_headlessTaskFile"])


def continuation_contract(state):
    """Recover the latest requirements only from the native validated history."""
    if state.get("user_steering") is None:
        return state.get("contract") or {}
    try:
        contract = RuntimeReducer.restore(state).contract
        if contract is None:
            raise ValueError("missing task contract")
        return contract.to_dict()
    except (ValueError, RuntimeError, TypeError, KeyError) as error:
        raise WorkspaceConflict("原任务的引导记录无法验证，不能自动恢复需求。") from error


def prepare_continuation(session, selection, task=None, *, run_budget=None):
    """Validate both durable task ownership and the exact retained after-image."""
    run_id, artifact_id = selection["runId"], selection["artifactId"]
    service = WebCandidateRecovery()
    matches = [(node, result, ref) for run, node, result, ref in _candidates(session)
               if run == run_id and node == "primary" and ref.artifact_id == artifact_id]
    submitted = False
    if not matches:
        try:
            prior_state, _, _ = service._submitted(session, run_id)
            prior_result = prior_state["nodes"]["primary"]["result"]
            ref = ArtifactRef(**prior_result["session_checkpoint"])
            if ref.artifact_id == artifact_id:
                matches = [("primary", prior_result, ref)]
                submitted = True
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass
        except HTTPException as error:
            if error.status_code != 404:
                raise
    if len(matches) != 1:
        raise WorkspaceConflict("没有可继续的主任务进度；请先查看候选恢复记录。")
    node_id, result, ref = matches[0]
    service.artifacts._validate_component(run_id)
    state_path = service.runs / (run_id + ".json")
    if is_unsafe_stored_path(state_path, boundary=service.state_root, reject_hardlinks=True):
        raise WorkspaceConflict("原任务状态路径不可用。")
    state = RuntimeStateStore(state_path).load_required()
    node = state.get("nodes", {}).get(node_id, {})
    if (state.get("run_id") != run_id or candidate_recovery_result(state, node_id) != result
        or node.get("state") not in ({"SUCCEEDED"} if submitted else {"FAILED_TERMINAL", "FAILED_RETRYABLE", "CANCELED"})
        or any(item.get("state") in {"RUNNING", "CLAIMED", "WAITING_CHILDREN", "WAITING_APPROVAL", "WAITING_INPUT"}
               for item in state.get("nodes", {}).values())
        or state.get("integration_status") == "committed"
        or any(item.get("status") == "pending" for item in state.get("candidate_quarantines", {}).values())):
        raise WorkspaceConflict("原任务状态已改变或仍有未确认的写入；暂不能继续。")
    ledger = UsageLedger.restore(state.get("usage_ledger", {}))
    usage = ledger.summary()
    if not ledger.settlement_ready(BudgetEnvelope()):
        raise WorkspaceConflict("原任务仍有未结束的模型请求，请等待请求结束后继续。")
    unlimited = run_budget is not None and RunBudget.model_validate(run_budget).unlimited
    payload = json.loads(service.artifacts.get_text(ref))
    source = Path(session.workspace)
    workspace_payload = payload.get("workspace", {}) if submitted else payload
    if (normalize_workspace_path(workspace_payload.get("source", "")) != normalize_workspace_path(source)
        or any(payload.get(key) != expected for key, expected in
            (("run_id", run_id), ("node_id", node_id), ("attempt_id", result["attempt_id"])))
        or (not submitted and any(payload.get(key) is not False for key in ("submitted", "verified", "integrated")))
        or workspace_snapshot(source) != workspace_payload.get("base_hashes")):
        raise WorkspaceConflict("原目录或进度归属已变化；请先处理冲突，再继续。")
    if submitted:
        from minicode.workspace_provider import TemporaryWorkspaceProvider
        TemporaryWorkspaceProvider().reopen(source=source, workspace=Path(workspace_payload["path"]),
            owner_token=workspace_payload["owner"], base_hashes=workspace_payload["base_hashes"],
            expected_revision=payload["candidate_revision"])
    trace = next((trace for trace in session.runtime_runs if trace.get("state", {}).get("run_id") == run_id), None)
    if trace is None:
        raise WorkspaceConflict("原任务验收记录不可用。")
    saved_task = trace.get("web_task_snapshot")
    if saved_task is not None:
        original_task = restore_task(saved_task, str(source))
        if task is not None and task.source_sha256 != original_task.source_sha256:
            raise WorkspaceConflict("继续任务必须保留原验收契约。")
        task = original_task
    elif trace.get("task_file"):
        if task is None or task.summary()["sha256"] != trace["task_file"]["sha256"]:
            raise WorkspaceConflict("历史任务没有保存完整契约；请重新选择并确认原契约后继续。")
    profile = trace.get("profile", {}).get("requested_mode")
    if (profile not in {"standard", "deep", "ultra"}
        or trace.get("profile", {}).get("write_isolation") == "direct"
        or (profile == "standard" and trace.get("profile", {}).get("write_isolation") != "temporary-workspace")):
        raise WorkspaceConflict("只有隔离执行的主任务支持从候选继续。")
    contract = continuation_contract(state)
    saved_settings = trace.get("web_continuation_settings")
    if saved_settings is not None:
        if (not isinstance(saved_settings, dict) or set(saved_settings) - set(CONTINUATION_SETTINGS)
            or any(saved_settings.get(public, []) != contract.get(native, []) for public, native in (
                ("taskConstraints", "constraints"), ("acceptanceCriteria", "acceptance_criteria"), ("expectedOutputs", "expected_outputs")))):
            raise WorkspaceConflict("原任务的继续契约与持久化条件不一致。")
    if not task and saved_settings is None and (contract.get("acceptance_criteria") or contract.get("expected_outputs") or contract.get("constraints")):
        raise WorkspaceConflict("原任务的验收条件缺少可恢复契约，不能自动简化后继续。")
    prior = trace.get("web_continuation") or {}
    lineage = list(prior.get("ancestors", [])) + [{"runId": run_id, "tokens": usage["tokens"],
        "tokensKnown": usage["tokens_known"], "unsettledCalls": usage["unsettled_calls"],
        "knownCost": usage["known_cost"], "costKnown": usage["cost_known"], "modelCalls": usage["model_calls"]}]
    if len(lineage) > 32 or len({entry["runId"] for entry in lineage}) != len(lineage):
        raise WorkspaceConflict("继续记录过长或包含重复任务，请检查历史用量。")
    if not unlimited and any(not item.get("tokensKnown", True) or item.get("unsettledCalls", 0) for item in lineage):
        raise WorkspaceConflict("历史请求的最终用量尚未返回；可选择不限额继续，有限预算需先对账。历史用量不会清零。")
    return {"selection": {"runId": run_id, "artifactId": artifact_id}, "artifact": ref.to_dict(),
        "ancestors": lineage, "prompt": contract["envelope"]["request"], "profile": profile,
        "task": task, "settings": saved_settings,
        "directory": service._directory(session.session_id, run_id, artifact_id)}


def public_continuation(prepared):
    return {"sourceRunId": prepared["selection"]["runId"], "ancestors": prepared["ancestors"],
        "retainedTokens": sum(item["tokens"] for item in prepared["ancestors"]),
        "retainedTokensKnown": all(item.get("tokensKnown", True) for item in prepared["ancestors"]),
        "unsettledCalls": sum(item.get("unsettledCalls", 0) for item in prepared["ancestors"]),
        "notice": "从保留的主任务修改开始新的执行；历史用量保留，本轮预算单独生效，所有验收仍须通过。"}


def continuation_view(value):
    """Public allowlist: no store keys, contract commands or recovery file paths."""
    import re
    if (not isinstance(value, dict) or not isinstance(value.get("sourceRunId"), str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value["sourceRunId"])
        or type(value.get("retainedTokens")) is not int or value["retainedTokens"] < 0):
        return None
    return {"sourceRunId": value["sourceRunId"], "retainedTokens": value["retainedTokens"],
        "retainedTokensKnown": value.get("retainedTokensKnown", True) is True,
        "unsettledCalls": value.get("unsettledCalls", 0) if type(value.get("unsettledCalls", 0)) is int and value.get("unsettledCalls", 0) >= 0 else None,
        "notice": "历史用量保留，本轮预算单独生效，仍须完成原验收。"}
