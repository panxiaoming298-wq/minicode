"""Private, immutable Worker observations; never a checkpoint or capability.

Only the native runner binds this recorder. References live in result metrics,
outside dependency-readable artifacts. Request bodies exclude transport secrets;
message/tool content is retained locally, just like canonical session history.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
import json
import threading
import time
from typing import Any

from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import AgentInvocationSpec, AgentResult, ArtifactRef, new_id
from minicode.turn_events import AgentTurnOutcome, TurnEvent

RECORD_MEDIA_TYPE = "application/vnd.minicode.worker-audit-record+json"
INDEX_MEDIA_TYPE = "application/vnd.minicode.worker-audit-index+json"
_current: ContextVar[WorkerAudit | None] = ContextVar("native_worker_audit", default=None)
# These are transport/envelope fields, not keys inside user messages or schemas.
_TRANSPORT_FIELDS = frozenset({"apikey", "authorization", "headers", "password", "credentials",
    "secret", "token", "accesstoken", "refreshtoken", "proxy", "baseurl", "apibase", "endpoint"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class WorkerAudit:
    def __init__(self, artifacts: RuntimeArtifactStore, invocation: AgentInvocationSpec):
        self.artifacts = artifacts
        self.invocation = invocation
        self.audit_id = new_id("worker-audit")
        self._lock = threading.RLock()
        self._records: list[dict] = []
        self._errors: list[dict] = []
        self._sequence = 0
        self._closed = False
        self._content_refs: dict[str, dict] = {}

    def _intern_values(self, values: list) -> dict:
        entries = []
        for value in values:
            encoded = _json(value)
            if len(encoded) < 1024:
                entries.append({"inline": value})
                continue
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            ref = self._content_refs.get(digest)
            if ref is None:
                ref = self.artifacts.put_text(encoded, run_id=self.invocation.run_id,
                    node_id=self.invocation.node_id, media_type="application/vnd.minicode.worker-audit-value+json").to_dict()
                if len(self._content_refs) >= 4096:
                    self._content_refs.pop(next(iter(self._content_refs)))
                self._content_refs[digest] = ref
            entries.append({"ref": ref})
        return {"entries": entries}

    def _compact_payload(self, payload: dict) -> tuple[dict, list[list[str]]]:
        compact = dict(payload)
        paths = []
        for name in ("canonical_messages", "input_messages"):
            if isinstance(compact.get(name), list):
                compact[name] = self._intern_values(compact[name])
                paths.append([name])
        if isinstance(compact.get("body"), dict):
            body = compact["body"] = dict(compact["body"])
            for name in ("messages", "tools", "input"):
                if isinstance(body.get(name), list):
                    body[name] = self._intern_values(body[name])
                    paths.append(["body", name])
        return compact, paths

    def _identity(self) -> dict:
        invocation = self.invocation
        return {"audit_id": self.audit_id, "run_id": invocation.run_id,
            "node_id": invocation.node_id, "attempt_id": invocation.attempt_id,
            "plan_version": invocation.plan_version, "role": invocation.role,
            "contract_revision": invocation.context_manifest.contract_revision,
            "base_revision": invocation.context_manifest.base_revision,
            "grants_authority": False, "counts_as_acceptance": False}

    def record(self, kind: str, **payload: Any) -> None:
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            sequence = self._sequence
            try:
                compact, paths = self._compact_payload(payload)
                ref = self.artifacts.put_text(_json({"schema": "worker-audit-record-v2" if paths else "worker-audit-record-v1",
                    **self._identity(), "sequence": sequence, "kind": kind, "recorded_at": time.time(),
                    "previous_sha256": self._records[-1]["ref"]["content_hash"] if self._records else None,
                    **({"value_ref_paths": paths} if paths else {}),
                    "payload": compact}), run_id=self.invocation.run_id, node_id=self.invocation.node_id,
                    media_type=RECORD_MEDIA_TYPE)
                self._records.append({"sequence": sequence, "kind": kind, "ref": ref.to_dict()})
            except Exception as error:
                # An audit I/O failure neither changes execution nor disappears.
                # Do not log exception text: it may contain private file content.
                self._errors.append({"sequence": sequence, "kind": kind, "error_type": type(error).__name__})

    def request(self, kind: str, body: dict, **payload: Any) -> None:
        try:
            redacted = [key for key in body
                if str(key).replace("_", "").replace("-", "").lower() in _TRANSPORT_FIELDS]
            body_hash = hashlib.sha256(_json(body).encode("utf-8")).hexdigest()
            self.record(kind, body={key: value for key, value in body.items() if key not in redacted},
                body_sha256=body_hash, body_hash_encoding="compact-json-utf8",
                redacted_fields=redacted, **payload)
        except Exception as error:
            with self._lock:
                self._errors.append({"kind": kind, "error_type": type(error).__name__})

    def on_event(self, event: TurnEvent) -> None:
        try:
            if event.kind == "done":
                self.record("turn_done", **asdict(AgentTurnOutcome.from_event(event)))
            elif event.kind in {"tool_start", "tool_result", "tool_rejected", "runtime"}:
                self.record("turn_event", event=asdict(event))
        except Exception as error:
            with self._lock:
                self._errors.append({"kind": "turn_event", "error_type": type(error).__name__})

    def tool_result(self, call, result) -> None:
        try:
            self.record("tool_result_observed", call=call, result=asdict(result))
        except Exception as error:
            with self._lock:
                self._errors.append({"kind": "tool_result_observed", "error_type": type(error).__name__})

    def finish(self, result: AgentResult) -> dict:
        with self._lock:
            self.record("worker_result", status=result.status.value,
                failure=asdict(result.failure) if result.failure else None,
                verification_records=[asdict(record) for record in result.verification_records],
                continuation=result.continuation is not None,
                usage=dict(result.usage))
            index = {"schema": "worker-audit-index-v1", **self._identity(),
                "complete": not self._errors, "records": self._records, "errors": self._errors,
                "terminal_status": result.status.value}
            ref = None
            try:
                ref = self.artifacts.put_text(_json(index), run_id=self.invocation.run_id,
                    node_id=self.invocation.node_id, media_type=INDEX_MEDIA_TYPE)
            except Exception as error:
                self._errors.append({"kind": "index", "error_type": type(error).__name__})
            self._closed = True
            return {"schema": "worker-audit-summary-v1", "audit_id": self.audit_id,
                "complete": not self._errors, "record_count": len(self._records),
                "errors": list(self._errors), "index_ref": ref.to_dict() if ref else None,
                "grants_authority": False, "counts_as_acceptance": False}


@contextmanager
def bind_worker_audit(audit: WorkerAudit | None):
    token = _current.set(audit)
    try:
        yield
    finally:
        _current.reset(token)


def current_worker_audit() -> WorkerAudit | None:
    return _current.get()


def read_worker_audit_record(store: RuntimeArtifactStore, ref: ArtifactRef) -> dict:
    """Read either audit format and verify every referenced immutable value."""
    record = json.loads(store.get_text(ref))
    if record.get("schema") == "worker-audit-record-v1":
        return record
    if record.get("schema") != "worker-audit-record-v2":
        raise ValueError("unsupported worker audit record")
    for path in record["value_ref_paths"]:
        if path not in [["canonical_messages"], ["input_messages"], ["body", "messages"], ["body", "tools"], ["body", "input"]]:
            raise ValueError("invalid worker audit reference path")
        parent = record["payload"]
        for name in path[:-1]:
            parent = parent[name]
        values = []
        for entry in parent[path[-1]]["entries"]:
            if set(entry) == {"inline"}:
                values.append(entry["inline"])
            elif set(entry) == {"ref"}:
                value_ref = ArtifactRef(**entry["ref"])
                if not store.owns_reference(value_ref, run_id=record["run_id"], node_id=record["node_id"]):
                    raise ValueError("worker audit value belongs to another producer")
                values.append(json.loads(store.get_text(value_ref)))
            else:
                raise ValueError("invalid worker audit value")
        parent[path[-1]] = values
    return record


def record_worker_audit(kind: str, **payload: Any) -> None:
    audit = _current.get()
    if audit is not None:
        audit.record(kind, **payload)
