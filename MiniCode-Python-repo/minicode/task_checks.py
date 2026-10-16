"""Separate model check findings from native evidence of execution/image delivery.

These are scoped check notes, not Runtime acceptance certificates. Evidence is
collected inside one agent activation and cannot be supplied by tool arguments.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time

from minicode.runtime_protocol import ToolEffect

REPORT_TOOL = "record_task_checks"
FUNCTIONAL_TOOLS = frozenset({"run_command", "test_runner", "request_verification", "run_acceptance_checks", "run_agent_check"})


class TaskCheckEvidence:
    def __init__(self):
        self.receipts: dict[str, dict] = {}
        self.delivered_images: set[str] = set()
        self.revision = 0

    def observe_tool(self, call, result, *, effect=ToolEffect.READ_ONLY):
        name = call["toolName"]
        # Explicit file writes invalidate earlier evidence. Shell checks remain
        # snapshots of their execution; this does not certify a whole workspace.
        if effect is ToolEffect.WORKSPACE_WRITE and not result.inputRejected:
            self.revision += 1
        if name not in FUNCTIONAL_TOOLS and name != "view_image":
            return
        if result.unsettledTool or result.ambiguousSideEffect or result.inputRejected or result.inputValidationFailed:
            return
        if name == "run_command" and not result.commandExecutionSettled:
            return
        if name in {"run_acceptance_checks", "request_verification"}:
            try:
                if json.loads(result.output).get("status") == "not_required":
                    return  # Scope selection without an executed check proves no functional pass.
            except (ValueError, TypeError, AttributeError):
                pass
        record = {"toolCallId": call["id"], "toolName": name, "ok": result.ok,
                  "createdAt": round(time.time() * 1000), "revision": self.revision,
                  "outputSha256": hashlib.sha256(result.output.encode()).hexdigest(),
                  "outputPreview": result.output[:1200]}
        if result.commandCheckReport is not None:
            record["checkReport"] = deepcopy(result.commandCheckReport)
        if name == "view_image":
            if not result.ok or not result.imageInputIds:
                return
            try:
                receipt = json.loads(result.output)
                record.update(image=receipt["image"], modelImage=receipt["modelImage"],
                              imageInputIds=list(result.imageInputIds))
            except (ValueError, KeyError, TypeError):
                return
        self.receipts[call["id"]] = record

    def model_observed(self, image_ids):
        # A fallback/text-only response cannot claim another model's vision.
        self.delivered_images = set(image_ids)

    def context(self):
        available = {"functional": [], "visual": []}
        for receipt in reversed(list(self.receipts.values())):
            if receipt["revision"] != self.revision:
                continue
            kind = "visual" if receipt["toolName"] == "view_image" else "functional"
            if len(available[kind]) >= 8:
                continue
            available[kind].append({"toolCallId": receipt["toolCallId"], "toolName": receipt["toolName"],
                "ok": receipt["ok"], "evidence": receipt.get("checkReport") or receipt.get("image", {}).get("name") or receipt["outputPreview"][:240]})
        if not any(available.values()):
            return ""
        return ("Native evidence index for record_task_checks. Copy exact toolCallId values into the matching "
                "evidence_call_ids; never guess IDs or use visual evidence for functional checks. A successful call "
                "does not establish coverage. This index does not require a recording call or checks of unrelated categories. "
                "Visual findings still require seeing the actual pixels.\n"
                + json.dumps(available, ensure_ascii=False))

    def _record_check(self, kind, check):
        evidence = []
        if check["status"] not in {"not_checked", "not_applicable"}:
            ids = check["evidence_call_ids"]
            if not ids:
                raise ValueError("没有关联本次实际执行的检查")
            for call_id in ids:
                receipt = self.receipts.get(call_id)
                if receipt is None:
                    raise ValueError(f"未找到本次检查的执行记录（{call_id}）")
                if receipt["revision"] != self.revision:
                    raise ValueError("检查后文件已有改动，当前版本尚未复查")
                if kind == "functional":
                    if receipt["toolName"] not in FUNCTIONAL_TOOLS:
                        raise ValueError("图片读取记录不能证明功能检查已执行")
                    if check["status"] == "passed" and not receipt["ok"]:
                        raise ValueError("关联的检查执行未成功，尚不能确认通过")
                elif receipt["toolName"] != "view_image" or not set(receipt.get("imageInputIds", [])) <= self.delivered_images:
                    raise ValueError("模型尚未收到这张图片的实际像素，无法确认视觉结论")
                evidence.append({key: deepcopy(val) for key, val in receipt.items() if key not in {"revision", "imageInputIds"}})
            if kind == "visual":
                # A successful read + an explicit model finding is still an
                # AI judgement; never label it a deterministic verifier pass.
                for item in evidence:
                    item.pop("outputPreview", None)
        return {"status": check["status"], "summary": check["summary"].strip(), "evidence": evidence}

    def record(self, value, context):
        checks = {}
        for kind in ("functional", "visual"):
            if kind not in value:
                continue
            try:
                checks[kind] = self._record_check(kind, value[kind])
            except ValueError as error:
                # Keep the other category's valid findings. Evidence gaps are
                # recorded limitations, never a reason to reject the whole task.
                checks[kind] = {"status": "not_checked", "summary": f"检查未完成：{error}。", "evidence": []}
        runtime = context._runtime or {}
        return {"schema": "minicode.task-checks.v2", "id": context.tool_call_id,
                "createdAt": round(time.time() * 1000), "runId": context.run_id or runtime.get("runtimeRunId", ""),
                "nodeId": context.node_id or "primary", "attemptId": context.attempt_id or "",
                "turnId": runtime.get("_webTurnId", ""), "assessment": "model_with_native_evidence",
                "scope": "recorded_checks", **checks}
