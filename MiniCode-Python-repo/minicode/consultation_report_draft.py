"""One immutable, unsubmitted report draft owned by one helper activation.

Only a native, standalone input rejection can create this recovery capability.
It is not checkpoint authority, a successful tool execution, or a handoff.
The full rejected input is persisted before exposing a summary-only retry.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import hashlib
import json
from typing import Any

from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore
from minicode.runtime_budget import _bailian_compatible_endpoint
from minicode.runtime_protocol import new_id
from minicode.tooling import ToolContext
from minicode.workspace_provider import WorkspaceConflict


class ConsultationReportDraft:
    def __init__(self, artifacts: RuntimeArtifactStore, authorize: Callable[[ToolContext], str]) -> None:
        self._artifacts, self._authorize = artifacts, authorize
        self._context: ToolContext | None = None
        self._revision = ""
        self._id = ""
        self._artifact = None
        self._input: dict[str, str] = {}
        self._rejection = ""
        self._retention_attempted = False

    def retain(self, call: dict, output: str, context: ToolContext) -> str:
        """Caller has validated report and isolated summary overflow; no replay."""
        if self._retention_attempted:
            return output  # At most one bounded draft/artifact per activation.
        revision = self._authorize(context)
        raw = json.dumps(call["input"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > 128_256:
            return output
        self._retention_attempted = True
        artifact = self._artifacts.put_bytes(raw, run_id=context.run_id, node_id=context.node_id,
            media_type="application/json; charset=utf-8")
        if self._artifacts.get_bytes(artifact) != raw or self._authorize(context) != revision:
            raise WorkspaceConflict("report draft source changed while retaining rejected input")
        draft_id = new_id("report-draft")
        report = call["input"]["report"].strip().encode("utf-8")
        rejection = output + "\n" + json.dumps({"status": "summary_rejected_report_retained", "submitted": False,
            "draft_id": draft_id, "report_sha256": hashlib.sha256(report).hexdigest(),
            "report_bytes": len(report), "rejected_summary_bytes": len(call["input"]["summary"].strip().encode("utf-8")),
            "original_input_handle": artifact.content_hash,
            "retry": "Call submit_consultation with draft_id and a corrected summary within 2000 UTF-8 bytes. "
                     + ("Include the same verdict as the retained report. " if "verdict" in call["input"] else "")
                     + "Do not resend report. Preserve decisions, source locations, failed checks and unresolved risks. "
                     "The complete rejected input is available through runtime_context_read; this is NOT a submission."},
            ensure_ascii=False, separators=(",", ":"))
        self._context, self._revision, self._id = context, revision, draft_id
        self._artifact, self._input, self._rejection = artifact, deepcopy(call["input"]), rejection
        return rejection

    def resolve(self, draft_id: str, *, submitting_call_id: str | None = None) -> str:
        if self._context is None or draft_id != self._id:
            raise WorkspaceConflict("report draft is not owned by this helper activation")
        if self._authorize(self._context) != self._revision:
            raise WorkspaceConflict("report draft source changed")
        if submitting_call_id == self._context.tool_call_id:
            raise WorkspaceConflict("report correction requires a new explicit call ID")
        try:
            raw = self._artifacts.get_bytes(self._artifact)
        except ArtifactIntegrityError as error:
            raise WorkspaceConflict("retained report failed its integrity check") from error
        if json.loads(raw) != self._input:
            raise WorkspaceConflict("report draft no longer matches rejected input")
        return self._input["report"].strip()

    def verdict(self, draft_id: str) -> str | None:
        self.resolve(draft_id)
        return self._input.get("verdict")

    def prepare(self, body: Mapping[str, Any], *, endpoint: str, contents: dict[str, bytes]) -> tuple[dict, dict | None]:
        """Lossless historical-argument view; leave error and full summary inline.

        Native authority, exact input/error pairing and a closed singleton wire
        batch are all required. Unsupported provider continuations stay intact.
        A restart may read the artifact but cannot resurrect its submit handle.
        Exact Alibaba GLM-5.2 preserves reasoning_content by default: page only
        the rejected report argument, never remove/reorder messages or rewrite
        any reasoning field. Do not set clear_thinking or infer Qwen semantics.
        Verified 2026-09-11: https://help.aliyun.com/zh/model-studio/glm
        """
        unchanged = dict(body), None
        glm = body.get("model") == "glm-5.2"
        if (self._context is None or body.get("model") not in {"qwen3.8-flash", "glm-5.2"}
            or not _bailian_compatible_endpoint(endpoint)
            or ("preserve_thinking" in body and body["preserve_thinking"] is not False)
            or (glm and "clear_thinking" in body and type(body["clear_thinking"]) is not bool)
            or any(key in body for key in ("previous_response_id", "conversation", "context_management"))):
            return unchanged
        messages, tools = body.get("messages"), body.get("tools")
        if not isinstance(messages, list) or not isinstance(tools, list):
            return unchanged
        native = [i for i, tool in enumerate(tools) if isinstance(tool, Mapping)
            and tool.get("type") == "function" and isinstance(tool.get("function"), Mapping)
            and tool["function"].get("name") == "submit_consultation"]
        readers = [tool for tool in tools if isinstance(tool, Mapping) and tool.get("type") == "function"
            and isinstance(tool.get("function"), Mapping) and tool["function"].get("name") == "runtime_context_read"]
        if len(native) != 1 or len(readers) != 1:
            return unchanged
        # No open, duplicate, reordered or interleaved tool protocol anywhere.
        seen: set[str] = set()
        pending: set[str] = set()
        location = None
        receipt_index = None
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                return unchanged
            if message.get("role") == "assistant" and "tool_calls" in message:
                batch = message["tool_calls"]
                if pending or not isinstance(batch, list) or not batch:
                    return unchanged
                for call in batch:
                    if not isinstance(call, Mapping):
                        return unchanged
                    call_id, function = call.get("id"), call.get("function")
                    if (not isinstance(call_id, str) or not call_id or call_id in seen
                        or call.get("type") != "function" or not isinstance(function, Mapping)):
                        return unchanged
                    seen.add(call_id)
                    pending.add(call_id)
                    if call_id == self._context.tool_call_id:
                        if (len(batch) != 1 or function.get("name") != "submit_consultation"
                            or function.get("arguments") != json.dumps(self._input)):
                            return unchanged
                        location = index
            elif message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    return unchanged
                pending.remove(call_id)
                if call_id == self._context.tool_call_id:
                    if location is None or message.get("content") != self._rejection:
                        return unchanged
                    receipt_index = index
            elif pending:
                return unchanged
        if pending or location is None or receipt_index is None:
            return unchanged
        # Recheck the live source and persisted bytes, not a hash in model prose.
        self.resolve(self._id)
        raw = self._artifacts.get_bytes(self._artifact)
        contents[self._artifact.content_hash] = raw
        prepared = deepcopy(dict(body))
        old = prepared["messages"][location]["tool_calls"][0]["function"]["arguments"]
        replacement = json.dumps({"summary": self._input["summary"],
            **({"verdict": self._input["verdict"]} if "verdict" in self._input else {}), "runtime_rejected_report": {
            "draft_id": self._id, "original_input_handle": self._artifact.content_hash,
            "status": "not_submitted", "notice": "Historical rejected report, not executable arguments. "
                "Full original input: runtime_context_read. Correct summary only; see unchanged rejection."}},
            ensure_ascii=False, separators=(",", ":"))
        paged = len(json.dumps(replacement, ensure_ascii=False).encode("utf-8")) < len(json.dumps(old, ensure_ascii=False).encode("utf-8"))
        if paged:
            prepared["messages"][location]["tool_calls"][0]["function"]["arguments"] = replacement
        definition = prepared["tools"][native[0]]["function"]
        summary_schema = definition.get("parameters", {}).get("properties", {}).get("summary")
        if not isinstance(summary_schema, dict):
            return unchanged
        definition["parameters"] = {"type": "object", "properties": {
            "draft_id": {"type": "string", "enum": [self._id]}, "summary": summary_schema},
            "required": ["draft_id", "summary"], "additionalProperties": False}
        if "verdict" in self._input:
            definition["parameters"]["properties"]["verdict"] = {"type": "string", "enum": [self._input["verdict"]]}
            definition["parameters"]["required"].append("verdict")
        definition["description"] = ("Explicitly submit the retained report with a corrected, self-contained summary within 2000 UTF-8 bytes. "
            "Do not resend report. Preserve source locations, decisions, failed checks and unresolved risks. "
            "Call alone or LAST after successful reads. Runtime revalidates ownership, source and full report; this does not finish the parent task.")
        return prepared, {"policy": "glm-consultation-summary-repair-v1" if glm else "consultation-summary-repair-v1",
            **({"reasoning_history_unchanged": True} if glm else {}), "projected_report_inputs": int(paged),
            "draft_id": self._id, "original_input_handle": self._artifact.content_hash,
            "artifact_ref": self._artifact.to_dict(), "canonical_history_unchanged": True,
            "summary_and_rejection_unchanged": True, "changes_authority": False,
            "input_bytes_before": len(json.dumps(body, ensure_ascii=False).encode("utf-8")),
            "input_bytes_after": len(json.dumps(prepared, ensure_ascii=False).encode("utf-8"))}
