"""Lossless wire representation of a helper's Runtime-owned file observations.

Only audit metadata is paged. Every original output/error remains inline; a
later, independently observed identical successful read may reference that
inline value. This never creates read, submission or acceptance authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from typing import Any

from minicode.runtime_artifacts import ArtifactIntegrityError, RuntimeArtifactStore
from minicode.runtime_protocol import AgentInvocationSpec, ToolEffect


POLICY = "consultation-source-view-v1"


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _closed_calls(messages: list) -> dict[str, tuple[int, Mapping]] | None:
    calls, pending = {}, set()
    for index, message in enumerate(messages):
        if (not isinstance(message, Mapping) or not isinstance(message.get("role"), str)
            or message["role"] not in {"system", "developer", "user", "assistant", "tool"}
            or not isinstance(message.get("content", ""), (str, type(None)))
            or ("reasoning_content" in message and not isinstance(message["reasoning_content"], str))):
            return None
        if message["role"] == "assistant" and "tool_calls" in message:
            batch = message["tool_calls"]
            if pending or not isinstance(batch, list) or not batch:
                return None
            for call in batch:
                if not isinstance(call, Mapping):
                    return None
                identifier, function = call.get("id"), call.get("function")
                if (not isinstance(identifier, str) or not identifier or identifier in calls
                    or call.get("type") != "function" or not isinstance(function, Mapping)):
                    return None
                calls[identifier] = index, function
                pending.add(identifier)
        elif message["role"] == "tool":
            identifier = message.get("tool_call_id")
            if not isinstance(identifier, str) or identifier not in pending:
                return None
            pending.remove(identifier)
        elif pending:
            return None
    return None if pending else calls


class ConsultationSourceView:
    def __init__(self, initial, *, artifacts: RuntimeArtifactStore, invocation: AgentInvocationSpec) -> None:
        raw = artifacts.get_bytes(initial.artifact)
        payload = json.loads(raw)
        if (initial.interrupted or not invocation.node_id.startswith("consult-")
            or initial.artifact.media_type != "application/vnd.minicode.consultation-context+json"
            or invocation.role not in {"explorer", "planner", "reviewer", "verifier"}
            or invocation.allowed_effects != (ToolEffect.READ_ONLY,)
            or invocation.context_manifest.tool_capability_scope != (ToolEffect.READ_ONLY,)
            or invocation.context_manifest.node_id != invocation.node_id
            or invocation.context_manifest.plan_version != invocation.plan_version
            or payload.get("schema") != "consultation-initial-context-v1"
            or payload.get("origin") != "runtime_explicit_initial_reads"
            or payload.get("changes_authority") is not False or payload.get("counts_as_acceptance") is not False
            or payload.get("interrupted") is not False
            or any(payload.get(key) != value for key, value in (
                ("run_id", invocation.run_id), ("node_id", invocation.node_id), ("attempt_id", invocation.attempt_id),
                ("base_revision", invocation.context_manifest.base_revision),
                ("requested_files", list(invocation.context_manifest.context_files))))):
            raise ValueError("initial observations are not owned by this read-only helper activation")
        content = initial.message.get("content")
        if (set(initial.message) != {"role", "content"} or initial.message["role"] != "user"
            or not isinstance(content, str) or "\n" not in content or content.split("\n", 1)[1].encode("utf-8") != raw):
            raise ValueError("initial message no longer matches its complete recorded artifact")
        self._message, self._raw = deepcopy(initial.message), raw
        self._artifacts, self._artifact = artifacts, initial.artifact
        self._handle = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._outputs: dict[str, int] = {}
        records = []
        for index, record in enumerate(payload["records"]):
            records.append({key: value for key, value in record.items()
                            if key not in {"tool_call_id", "manifest_file_sha256", "output_sha256"}})
            output = record.get("output")
            if (record.get("status") == "observed" and record.get("ok") is True and record.get("interrupted") is False
                and isinstance(output, str) and "TRUNCATED: yes" not in output
                and hashlib.sha256(output.encode("utf-8")).hexdigest() == record.get("output_sha256")):
                self._outputs.setdefault(output, index)
        view = {key: value for key, value in payload.items() if key not in {"run_id", "node_id", "attempt_id", "records"}}
        view.update(records=records, audit_metadata={"reader": "runtime_context_read", "handle": self._handle,
            "size_bytes": len(content.encode("utf-8")), "notice": "Audit-only metadata, not extra source evidence; read only if needed. All observed outputs/statuses are verbatim below."})
        self._view = {"role": "user", "content": content.split("\n", 1)[0] + "\n" + _encode(view).decode("utf-8")}

    def prepare(self, body: Mapping, *, contents: dict[str, bytes], settled_reads: Mapping) -> tuple[dict, dict | None]:
        unchanged = dict(body), None
        if any(key in body for key in ("previous_response_id", "conversation", "context_management")):
            return unchanged
        messages, tools = body.get("messages"), body.get("tools")
        if not isinstance(messages, list) or not isinstance(tools, list):
            return unchanged
        names = [tool.get("function", {}).get("name") for tool in tools if isinstance(tool, Mapping)
                 and tool.get("type") == "function" and isinstance(tool.get("function"), Mapping)]
        if names.count("runtime_context_read") != 1 or names.count("submit_consultation") != 1:
            return unchanged
        matches = [i for i, message in enumerate(messages) if message == self._message]
        calls = _closed_calls(messages)
        if len(matches) != 1 or calls is None:
            return unchanged
        try:
            if self._artifacts.get_bytes(self._artifact) != self._raw:
                return unchanged
            prepared = deepcopy(dict(body))
            location, shared = matches[0], 0
            prepared["messages"][location] = deepcopy(self._view)
            for message in prepared["messages"]:
                if message.get("role") != "tool" or message.get("is_error") is True:
                    continue
                identifier, output = message["tool_call_id"], message.get("content")
                position, function = calls[identifier]
                observed = settled_reads.get(identifier, {})
                if (position <= location or function.get("name") != "read_file" or observed.get("tool") != "read_file"
                    or output not in self._outputs or observed.get("receipt_sha256") != hashlib.sha256(output.encode("utf-8")).hexdigest()):
                    continue
                arguments = json.loads(function.get("arguments", ""))
                if not isinstance(arguments, dict) or hashlib.sha256(_encode(arguments)).hexdigest() != observed.get("input_sha256"):
                    continue
                replacement = _encode({"runtime_context_source_ref": {"handle": self._handle,
                    "json_pointer": f"/records/{self._outputs[output]}/output",
                    "notice": "Byte-identical output is inline in the initial observations JSON; this is the completed read receipt, not a new read or acceptance."}}).decode("utf-8")
                if len(_encode(replacement)) < len(_encode(output)):
                    message["content"] = replacement
                    shared += 1
            before, after = len(_encode(body)), len(_encode(prepared))
            if after >= before:
                return unchanged
        except (ArtifactIntegrityError, OSError, ValueError, TypeError, UnicodeError):
            return unchanged
        contents[self._handle] = self._message["content"].encode("utf-8")
        return prepared, {"policy": POLICY, "original_context_handle": self._handle,
            "artifact_ref": self._artifact.to_dict(), "shared_read_receipts": shared,
            "canonical_history_unchanged": True, "reasoning_history_unchanged": True,
            "source_outputs_unchanged": True, "changes_authority": False, "counts_as_acceptance": False,
            "input_bytes_before": before, "input_bytes_after": after}
