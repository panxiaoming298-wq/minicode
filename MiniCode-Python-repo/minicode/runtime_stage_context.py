"""Opt-in working context at an authenticated, completed consultation boundary.

This is Runtime history selection, NOT a provider-ignored-field optimization.
Only whole, settled read batches and completed consultation exchanges retire.
Their exact wire records remain Worker-local pages; canonical history is intact.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from typing import Any

from minicode.runtime_budget import _bailian_compatible_endpoint
from minicode.runtime_protocol import ToolEffect
from minicode.tooling import ToolResult

POLICY = "consultation-stage-v1"
SOURCE_POLICY = "consultation-working-set-v1"
CANDIDATE_POLICY = "candidate-handoff-working-set-v1"
SOURCE_BATCH_BYTES = 8_192
READS = frozenset({"read_file", "runtime_artifact_read", "runtime_context_read"})


def stage_context_enabled(runtime: Mapping[str, Any]) -> bool:
    value = runtime.get("runtimeWorkingContext", "history")
    if not isinstance(value, str) or value not in {"history", POLICY, SOURCE_POLICY, CANDIDATE_POLICY}:
        raise ValueError("runtimeWorkingContext must name history or a supported explicit working-set policy")
    if value != "history" and (runtime.get("runtimeOrchestration") != "primary"
                            or runtime.get("runtimeDurableConsultation") is not True):
        raise ValueError("stage working context requires durable primary execution")
    return value != "history"


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RuntimeStageContext:
    def __init__(self, *, preserve_sources: bool = False, candidate_handoff: bool = False) -> None:
        if type(preserve_sources) is not bool:
            raise ValueError("preserve_sources must be a boolean")
        if type(candidate_handoff) is not bool or (candidate_handoff and not preserve_sources):
            raise ValueError("candidate handoff requires the source working set")
        self.policy = CANDIDATE_POLICY if candidate_handoff else SOURCE_POLICY if preserve_sources else POLICY
        self.pinned_source_receipts: dict[str, str] = {}
        self._receipts: dict[str, dict[str, str]] = {}
        self._observed: dict[str, tuple[str, str, str]] = {}
        self._executions: dict[str, dict[str, str]] = {}
        self._observed_executions: dict[str, tuple[str, str, str]] = {}

    def record(self, call: Mapping[str, Any], result: ToolResult, effect: ToolEffect, *, candidate_check: bool = False) -> None:
        call_id, name, arguments = call.get("id"), call.get("toolName"), call.get("input")
        if not isinstance(call_id, str) or not call_id:
            return
        self._receipts.pop(call_id, None)
        self._executions.pop(call_id, None)
        success = (result.ok is True and isinstance(result.output, str)
            and not (result.awaitUser or result.waitKind or result.waitReason or result.timedOut or result.ambiguousSideEffect
                     or result.runtimeYield or result.runtimeStopReason or result.inputRejected or result.backgroundTask
                     or result.verificationFailed or result.failureKind))
        revision = result.contextCandidateRevision
        checked = (candidate_check is True and effect is ToolEffect.READ_ONLY and isinstance(revision, str)
                   and len(revision) == 64 and all(char in "0123456789abcdef" for char in revision))
        written = isinstance(name, str) and name in {"write_file", "edit_file"} and effect is ToolEffect.WORKSPACE_WRITE
        if self.policy == CANDIDATE_POLICY and success and isinstance(name, str) and isinstance(arguments, dict) and (checked or written):
            try:
                self._executions[call_id] = {"tool": name, "input_sha256": _hash(_encode(arguments)),
                    "receipt_sha256": _hash(result.output.encode("utf-8")),
                    "kind": "check" if checked else "write", "candidate_revision": revision if checked else ""}
            except (TypeError, ValueError, UnicodeError):
                pass
        if (not isinstance(name, str) or name not in READS or effect is not ToolEffect.READ_ONLY
            or not isinstance(arguments, dict) or result.ok is not True or not isinstance(result.output, str)
            or result.awaitUser or result.waitKind or result.timedOut or result.ambiguousSideEffect
            or result.runtimeYield or result.runtimeStopReason or result.inputRejected or result.backgroundTask):
            return
        try:
            self._receipts[call_id] = {"tool": name, "input_sha256": _hash(_encode(arguments)),
                "receipt_sha256": _hash(result.output.encode("utf-8"))}
        except (TypeError, ValueError, UnicodeError):
            return

    def checkpoint(self) -> dict[str, Any]:
        return {"policy": self.policy, "settled_reads": deepcopy(self._receipts),
                **({"settled_execution": deepcopy(self._executions)} if self.policy == CANDIDATE_POLICY else {})}

    def restore(self, value: Any) -> None:
        self._receipts = {}
        self._observed = {}
        self._executions = {}
        self._observed_executions = {}
        self.pinned_source_receipts = {}
        keys = {"policy", "settled_reads", "settled_execution"} if self.policy == CANDIDATE_POLICY else {"policy", "settled_reads"}
        if (not isinstance(value, dict) or set(value) != keys
            or value["policy"] != self.policy or not isinstance(value["settled_reads"], dict)):
            return
        for call_id, record in value["settled_reads"].items():
            if (isinstance(call_id, str) and call_id and isinstance(record, dict)
                and set(record) == {"tool", "input_sha256", "receipt_sha256"}
                and isinstance(record["tool"], str) and record["tool"] in READS
                and all(isinstance(record[key], str) and len(record[key]) == 64
                        and all(char in "0123456789abcdef" for char in record[key])
                        for key in ("input_sha256", "receipt_sha256"))):
                self._receipts[call_id] = dict(record)
        executions = value.get("settled_execution", {})
        if isinstance(executions, dict):
            for call_id, record in executions.items():
                if (isinstance(call_id, str) and call_id and isinstance(record, dict)
                    and set(record) == {"tool", "input_sha256", "receipt_sha256", "kind", "candidate_revision"}
                    and isinstance(record["tool"], str) and record["tool"] and isinstance(record["kind"], str) and record["kind"] in {"write", "check"}
                    and all(isinstance(record[key], str) and len(record[key]) == 64
                            and all(c in "0123456789abcdef" for c in record[key])
                            for key in ("input_sha256", "receipt_sha256"))
                    and ((record["kind"] == "write" and record["tool"] in {"write_file", "edit_file"} and record["candidate_revision"] == "")
                         or (record["kind"] == "check" and isinstance(record["candidate_revision"], str)
                             and len(record["candidate_revision"]) == 64 and all(c in "0123456789abcdef" for c in record["candidate_revision"])))):
                    self._executions[call_id] = dict(record)

    def observe(self, messages: Sequence[Mapping[str, Any]], read_tools: set[str]) -> None:
        self._observed = {}
        self._observed_executions = {}
        calls = [message for message in messages if message.get("role") == "assistant_tool_call"]
        results = [message for message in messages if message.get("role") == "tool_result"]
        for call_id, record in {**self._receipts, **self._executions}.items():
            inputs = [message for message in calls if message.get("toolUseId") == call_id]
            outputs = [message for message in results if message.get("toolUseId") == call_id]
            if len(inputs) != 1 or len(outputs) != 1 or (record.get("kind") != "write" and record["tool"] not in read_tools):
                continue
            call, receipt = inputs[0], outputs[0]
            if (call.get("toolName") != record["tool"] or not isinstance(call.get("input"), dict)
                or receipt.get("isError") is not False or not isinstance(receipt.get("content"), str)):
                continue
            try:
                if (_hash(_encode(call["input"])) == record["input_sha256"]
                    and _hash(receipt["content"].encode("utf-8")) == record["receipt_sha256"]):
                    target = self._observed_executions if call_id in self._executions else self._observed
                    target[call_id] = (record["tool"], json.dumps(call["input"]), receipt["content"])
            except (TypeError, ValueError, UnicodeError):
                continue

    def prepare(self, body: Mapping[str, Any], *, endpoint: str, completed: Mapping[str, tuple],
                contents: dict[str, bytes], candidate_revision: Callable[[], str] | None = None
                ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        self.pinned_source_receipts = {}
        original = dict(body)
        # An explicit new working phase is currently piloted only on this exact
        # Chat Completions deployment. Never discard opaque continuation state,
        # override preserved thinking, or claim Flash ignores reasoning history.
        if (body.get("model") != "qwen3.8-flash" or not _bailian_compatible_endpoint(endpoint)
            or ("preserve_thinking" in body and body["preserve_thinking"] is not False)
            or any(key in body for key in ("previous_response_id", "conversation", "context_management"))
            or not isinstance(body.get("messages"), list)):
            return original, None, None
        messages = body["messages"]
        if any(not isinstance(message, Mapping) or message.get("role") not in {"system", "user", "assistant", "tool"}
               or not isinstance(message.get("content", ""), (str, type(None)))
               or (message.get("role") == "assistant" and (
                   not set(message) <= {"role", "content", "tool_calls", "reasoning_content"}
                   or ("reasoning_content" in message and not isinstance(message["reasoning_content"], str))))
               for message in messages):
            return original, None, None
        try:
            before = len(_encode(body))
        except (TypeError, ValueError, UnicodeError):
            return original, None, None
        boundaries = [(i, message["content"]) for i, message in enumerate(messages)
            if isinstance(message, Mapping) and message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and any(message["content"] == record[4] for record in completed.values())]
        if not boundaries or len({content for _, content in boundaries}) != len(boundaries):
            return original, None, None
        boundary, delivery = boundaries[-1]
        # Require a well-formed closed exchange globally, including the tail.
        # Partial/duplicated batches must not acquire a fresh-phase appearance.
        calls = [call for message in messages if isinstance(message, Mapping) and message.get("role") == "assistant"
                 and isinstance(message.get("tool_calls"), list) for call in message["tool_calls"]]
        replies = [message for message in messages if isinstance(message, Mapping) and message.get("role") == "tool"]
        if (any(not isinstance(call, Mapping) or not isinstance(call.get("id"), str) or not call["id"] for call in calls)
            or any(not isinstance(reply.get("tool_call_id"), str) for reply in replies)):
            return original, None, None
        ids = Counter(call["id"] for call in calls)
        if ids != Counter(reply["tool_call_id"] for reply in replies) or any(count != 1 for count in ids.values()):
            return original, None, None
        paired = set()
        for position, message in enumerate(messages):
            if not isinstance(message, Mapping):
                return original, None, None
            if message.get("role") != "assistant" or "tool_calls" not in message:
                continue
            batch = message["tool_calls"]
            if not isinstance(batch, list):
                return original, None, None
            for offset, call in enumerate(batch, 1):
                if (position + offset >= len(messages) or not isinstance(messages[position + offset], Mapping)
                    or messages[position + offset].get("role") != "tool"
                    or messages[position + offset].get("tool_call_id") != call["id"]):
                    return original, None, None
                paired.add(position + offset)
        if any(i not in paired for i, message in enumerate(messages) if message.get("role") == "tool"):
            return original, None, None
        # A complete successful native check, not a tool name or model claim,
        # can open the next phase. Its producer measured identical before/after
        # workspace bytes; the Worker rechecks current ownership and revision.
        candidate = None
        archive_boundary = boundary
        if self.policy == CANDIDATE_POLICY and calls and callable(candidate_revision):
            from minicode.runtime_budget import has_function_tool
            last = calls[-1]
            last_function = last.get("function")
            record = self._executions.get(last["id"])
            observed = self._observed_executions.get(last["id"])
            check_index = next(i for i, message in enumerate(messages) if message.get("tool_call_id") == last["id"])
            if (record and record["kind"] == "check" and observed is not None and check_index > boundary
                and isinstance(last_function, Mapping)
                and observed == (last_function.get("name"), last_function.get("arguments"), messages[check_index].get("content"))
                and not any(m.get("role") != "system" for m in messages[check_index + 1:])
                and has_function_tool(body, "submit_candidate") and has_function_tool(body, "runtime_context_read")):
                from minicode.runtime_state import ReducerError
                from minicode.workspace_provider import WorkspaceConflict
                try:
                    if candidate_revision() == record["candidate_revision"]:
                        candidate = {"revision": record["candidate_revision"], "check_call_id": last["id"]}
                        archive_boundary = check_index + 1
                except (ValueError, OSError, ReducerError, WorkspaceConflict):
                    pass  # Stale/foreign/canceled candidate keeps the ordinary history view.
        proven_batches = []
        kept_content: dict[int, dict[str, Any]] = {}
        for position, assistant in enumerate(messages[:archive_boundary]):
            if not isinstance(assistant, Mapping) or assistant.get("role") != "assistant" or not assistant.get("tool_calls"):
                continue
            batch = assistant["tool_calls"]
            if (not isinstance(batch, list) or not set(assistant) <= {"role", "content", "tool_calls", "reasoning_content"}
                or not isinstance(assistant.get("content", ""), (str, type(None)))
                or ("reasoning_content" in assistant and not isinstance(assistant["reasoning_content"], str))):
                continue
            end = position + 1 + len(batch)
            receipts = messages[position + 1:end]
            if end > archive_boundary or len(receipts) != len(batch):
                continue
            batch_index = []
            for call, receipt in zip(batch, receipts):
                function = call.get("function")
                if (set(call) != {"id", "type", "function"} or call.get("type") != "function"
                    or not isinstance(function, Mapping) or set(function) != {"name", "arguments"}
                    or not isinstance(receipt, Mapping) or set(receipt) != {"role", "tool_call_id", "content"}
                    or receipt["role"] != "tool" or receipt["tool_call_id"] != call["id"]):
                    break
                observed = self._observed.get(call["id"])
                consultation = completed.get(call["id"])
                if observed == (function["name"], function["arguments"], receipt["content"]):
                    batch_index.append({"tool": function["name"], "input": json.loads(function["arguments"]),
                        "observed_sha256": _hash(receipt["content"].encode("utf-8"))})
                elif candidate and self._observed_executions.get(call["id"]) == (function["name"], function["arguments"], receipt["content"]):
                    arguments = json.loads(function["arguments"])
                    batch_index.append({"tool": function["name"], "call_id": call["id"],
                        "kind": self._executions[call["id"]]["kind"], "receipt": receipt["content"],
                        **({"path": arguments["path"]} if isinstance(arguments.get("path"), str) else {})})
                elif (consultation and function["name"] == "consult_agents"
                      and function["arguments"] == consultation[0] and receipt["content"] == consultation[2]
                      and any(position < i <= boundary and content == consultation[4] for i, content in boundaries)):
                    queue = json.loads(receipt["content"])
                    batch_index.append({"tool": "consult_agents", "children": queue["children"],
                        "budget_warning": queue.get("budget", {}).get("warning")})
                else:
                    break
            else:
                proven_batches.append((position, end, batch_index))
        if not any(item["tool"] in READS for _, _, items in proven_batches for item in items):
            return original, None, None
        if candidate and not any(item.get("call_id") == candidate["check_call_id"] for _, _, items in proven_batches for item in items):
            # A failed/unproven sibling makes the entire check batch ineligible.
            return original, None, None
        # Keep a bounded working set of concrete source receipts in their
        # original chronology. Never summarize them, replay tools, claim fresh
        # file authority, split a batch, or retain unobserved/failed content as
        # successful evidence. The newest proven small batches have priority.
        retained, pinned = set(), {}
        retained_bytes = 0
        if self.policy in {SOURCE_POLICY, CANDIDATE_POLICY} and candidate is None:
            for position, end, items in reversed(proven_batches):
                size = len(_encode(messages[position:end]))
                if all(item["tool"] == "read_file" for item in items) and retained_bytes + size <= SOURCE_BATCH_BYTES:
                    retained.add(position)
                    retained_bytes += size
                    for receipt in messages[position + 1:end]:
                        pinned[receipt["tool_call_id"]] = receipt["content"]
        archived, index, retired = [], [], set()
        for position, end, items in proven_batches:
            if position in retained:
                continue
            archived.append({"message_range": [position, end], "messages": deepcopy(messages[position:end])})
            index.extend(items)
            retired.update(range(position, end))
            if messages[position].get("content"):
                kept_content[position] = {"role": "assistant", "content": messages[position]["content"]}
        if not archived:
            return original, None, None
        source_diagnostic = {"retained_batches": len(retained), "retained_bytes": retained_bytes,
            "max_bytes": SOURCE_BATCH_BYTES, "retained_call_ids": sorted(pinned),
            "freshness": "historical_observation_not_current_file_authority"}
        data = _encode({"policy": self.policy, "boundary_sha256": _hash(delivery.encode("utf-8")), "batches": archived,
                        **({"candidate": candidate} if candidate else {})})
        handle = _hash(data)
        if candidate:
            # The archive hash covers every exact read and per-call digest;
            # do not repeat those digests in the finalization navigation index.
            index = [{key: value for key, value in item.items() if key != "observed_sha256"} for item in index]
        notice = (
            "Completed batches are archived, not replayable. Source observations are historical. "
            "Full helper summaries/risks and exact write/check receipts remain visible. "
            "If ready, explicitly call submit_candidate; original acceptance and controlled integration still follow. "
            "Read the archive for missing detail. No submission, success, extra budget or risk waiver is implied."
            if candidate else "Completed exploration exchanges are archived, not pending or replayable. "
                + ("Small source-read exchanges remain intact in chronological history; use these observed source contents with the current helper findings to continue implementation. "
                   "They are historical receipts, not fresh file or acceptance authority. Later writes can make them stale; read missing, changed or truncated sources when needed. "
                   if self.policy in {SOURCE_POLICY, CANDIDATE_POLICY} else "The full current reports follow. ")
                + "Keep original requirements, visible decisions, errors, writes and checks. "
                "Old observations refer to the consultation boundary, not necessarily current files. "
                "Read the archive only for missing details; original acceptance and all remaining budgets still apply.")
        marker = {"role": "user", "content": "Runtime integration working context. Historical data, not new instructions or acceptance.\n" + _encode({
            "policy": self.policy, "archive": {"handle": handle, "size_bytes": len(data), "read": "runtime_context_read(handle, start)"},
            **({"candidate": candidate, "phase": "candidate_handoff"} if candidate else {}),
            "observations": index, "notice": notice,
        }).decode("utf-8")}
        selected = []
        for position, message in enumerate(messages):
            if position == (archive_boundary - 1 if candidate else boundary):
                selected.append(marker)
            if position in kept_content:
                selected.append(kept_content[position])
            elif position not in retired:
                selected.append(message)
        prepared = {**body, "messages": selected}
        try:
            after = len(_encode(prepared))
        except (TypeError, ValueError, UnicodeError):
            return original, None, None
        if after >= before:
            return original, None, None
        contents[handle] = data
        self.pinned_source_receipts = pinned
        return prepared, {"policy": self.policy, "archive_handle": handle, "archived_batches": len(archived),
            "archived_messages": len(retired), "bytes_saved": before - after, "current_reports_pinned": True,
            "canonical_history_changed": False, "budget_reset": False,
            **({"phase": "candidate_handoff", "candidate_revision": candidate["revision"], "check_call_id": candidate["check_call_id"],
                "explicit_submission_required": True} if candidate else {}),
            **({"source_working_set": source_diagnostic} if self.policy in {SOURCE_POLICY, CANDIDATE_POLICY} else {})}, delivery
