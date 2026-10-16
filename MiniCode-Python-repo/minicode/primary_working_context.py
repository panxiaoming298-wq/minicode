"""Primary-authored working notes, not verified findings or Runtime authority.

Use the existing Worker's WorkingMemoryTracker. A fresh native tool receipt and
complete observed source decisions are required before a request may use these
notes in place of inline full reports. All original reports remain readable.
Semantic completeness is a model-quality question, not proved by this protocol.
"""
from copy import deepcopy
import hashlib
import json
from threading import RLock

from minicode.runtime_artifacts import ArtifactIntegrityError
from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.runtime_state import ReducerError
from minicode.tooling import ToolDefinition, ToolMetadata, ToolResult
from minicode.working_memory import WorkingMemoryTracker
from minicode.workspace_provider import WorkspaceConflict

POLICY = "primary-authored-working-context-v1"
TOOL = "record_working_context"
ADVICE_POLICY = "primary-working-context-advice-v1"


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _sources(decisions):
    return [{key: deepcopy(item[key]) for key in ("child_id", "child_attempt_id", "artifact_ref", "delivery_event_id")}
        for item in decisions]


def project_handoff(handoff, selected):
    """Explicit model-authored projection; never call this lossless summarizing.

    The phase's exact archive and artifact tools retain full source reports.
    Existing non-summary warnings, diagnoses and verifier records stay visible.
    """
    prepared = deepcopy(handoff)
    ids = {source["artifact_ref"]["artifact_id"] for source in selected["data"]["sources"]}
    replaced = set()
    for position in prepared["delivery_positions"]:
        message = prepared["messages"][position]
        header, raw = message["content"].split("\n", 1)
        payload = json.loads(raw)
        for child in payload["children"]:
            ref = child["result"].get("consultation_summary_ref")
            if ref and ref["artifact_id"] in ids:
                child["result"]["output_summary"] = (
                    "Full decision in consultation_summary_ref. Current primary-authored assessment is in working_context; not verified findings."
                )
                child["result"]["full_summary_inline"] = False
                replaced.add(ref["artifact_id"])
        payload["metadata"] = (
            "Full source reports remain readable; only their inline summaries are replaced by the primary's explicit working notes. "
            "Non-summary warnings/diagnoses/verdicts remain. scope=this_child means this node; audit metadata is in full_delivery. "
            "Semantic completeness of working notes is NOT verified."
        )
        message["content"] = header + "\n" + _encode(payload).decode("utf-8")
    if replaced != ids:
        raise WorkspaceConflict("working context cannot replace a missing native source delivery")
    prepared["working_context"] = {"policy": POLICY, "sha256": selected["sha256"],
        "source_revision": selected["data"]["source_revision"], "notes": selected["data"]["notes"],
        "authority": "primary_assessment_only", "semantic_completeness_verified": False}
    prepared["diagnostic"].update(full_decisions_preserved=False, full_decisions_available=True,
        decision_context="primary_authored_working_notes", semantic_completeness_verified=False)
    return prepared


class PrimaryWorkingContext:
    def __init__(self, phase, memory, authorize):
        if not isinstance(memory, WorkingMemoryTracker) or not callable(authorize):
            raise ValueError("working context needs native Worker memory and authorization")
        self.phase, self.memory, self.authorize = phase, memory, authorize
        self._issued = {}
        self._latest = None
        self._lock = RLock()

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or set(value) != {"decisions", "next_steps"}:
            raise ValueError("provide decisions and next_steps only; no execution/acceptance flags")
        if not isinstance(value["decisions"], list) or not value["decisions"]:
            raise ValueError("provide a decision and explicit unresolved_risks for every observed source")
        if not isinstance(value["next_steps"], str) or not value["next_steps"].strip():
            raise ValueError("next_steps must be nonempty text")
        seen = set()
        for item in value["decisions"]:
            if not isinstance(item, dict) or set(item) != {"artifact_id", "decision", "unresolved_risks"}:
                raise ValueError("each decision needs artifact_id, decision and unresolved_risks")
            if any(not isinstance(item[key], str) or not item[key].strip() for key in ("artifact_id", "decision")):
                raise ValueError("artifact_id and decision must be nonempty text")
            if item["artifact_id"] in seen:
                raise ValueError("duplicate decision source")
            seen.add(item["artifact_id"])
            if (not isinstance(item["unresolved_risks"], list)
                or any(not isinstance(risk, str) or not risk.strip() for risk in item["unresolved_risks"])):
                raise ValueError("unresolved_risks must be an explicit list of nonempty strings")
        if len(_encode(value)) > 12000:
            raise ValueError("working notes exceed 12000 UTF-8 bytes; do not truncate risks to fit")
        return deepcopy(value)

    def record(self, value, context):
        with self._lock:
            try:
                revision = self.authorize(context)
                identifier = context.tool_call_id
                if not identifier or identifier in self._issued or identifier in self.phase._seen:
                    raise WorkspaceConflict("working context requires a new native call ID; no replay")
                decisions = self.phase._execution_decisions(self.phase._observed)
                if not decisions:
                    raise WorkspaceConflict("working context requires full native observed decision pages first")
                source_ids = {item["artifact_ref"]["artifact_id"] for item in decisions}
                if {item["artifact_id"] for item in value["decisions"]} != source_ids:
                    raise WorkspaceConflict("working notes must cover exactly the current delivered decision sources")
                sources = _sources(decisions)
                notes = deepcopy(value)
                old = self.memory.task_context()
                if old and old.get("run_id") == context.run_id and old.get("sources") != sources:
                    raise WorkspaceConflict("existing working risks belong to different sources; retain full history instead")
                if old and old.get("run_id") == context.run_id and old.get("sources") == sources:
                    # Explicit updates may refine decisions/next steps, but
                    # cannot silently drop previously recorded unresolved risks.
                    risks = {item["artifact_id"]: item["unresolved_risks"] for item in old["notes"]["decisions"]}
                    for item in notes["decisions"]:
                        item["unresolved_risks"] = list(dict.fromkeys([*risks[item["artifact_id"]], *item["unresolved_risks"]]))
                data = {"policy": POLICY, "run_id": context.run_id, "source_revision": revision, "sources": sources, "notes": notes,
                    "authority": "primary_assessment_only", "semantic_completeness_verified": False}
                token = _hash(data)
                receipt = {"working_context_sha256": token, "status": "recorded_not_verified",
                    "grants_authority": False, "previous_unresolved_risks_retained": True}
                output = _encode(receipt).decode("utf-8")
                if self.authorize(context) != revision:
                    raise WorkspaceConflict("workspace changed while recording working context")
                self.memory.set_task_context(data)  # Validates size before replacing any existing data.
                self._issued[identifier] = {"input": _hash(value), "output": hashlib.sha256(output.encode()).hexdigest(), "context": token}
                self._latest = identifier
                return ToolResult(ok=True, output=output)
            except (KeyError, ValueError, TypeError, OSError, ArtifactIntegrityError, WorkspaceConflict, ReducerError) as error:
                return ToolResult(ok=False, output=f"Working context not recorded: {error}")

    def select(self, proven, decisions):
        """Data from a checkpoint, another activation or unobserved call cannot select."""
        with self._lock:
            data = self.memory.task_context()
            accepted = {identifier for identifier, issued in self._issued.items() if (
                (record := proven.get(identifier)) is not None and record["tool"] == TOOL
                and record["input"] == issued["input"] and record["output"] == issued["output"])}
            if (self._latest not in accepted or data is None or data.get("sources") != _sources(decisions)
                or _hash(data) != self._issued[self._latest]["context"]):
                return None
            return {"data": data, "call_ids": sorted(accepted), "sha256": self._issued[self._latest]["context"]}

    def is_current(self, token):
        current = self.select(self.phase._observed, self.phase._execution_decisions(self.phase._observed))
        return current is not None and current["sha256"] == token

    def advice(self, stage):
        """Native readiness for a request-local hint, never authority or notes.

        Only a successfully prepared phase can supply the wire-view proof.
        Recheck current reading and workspace ownership rather than trusting
        a progress marker in caller text. Existing/stale notes are not silently
        replaced; their risks need the ordinary explicit update protocol.
        """
        from minicode.primary_phase_context import POLICY as PHASE_POLICY
        with self._lock:
            try:
                if (not isinstance(stage, dict) or stage.get("policy") != PHASE_POLICY or self.phase._policy != PHASE_POLICY
                    or self._latest is not None or self.memory.task_context() is not None
                    or stage.get("primary_working_context")):
                    return None
                revision = self.phase._current_revision()
                if revision != stage.get("workspace_revision"):
                    return None
                decisions = self.phase._execution_decisions(self.phase._observed)
                progress = stage.get("runtime_progress", {})
                expected = [{"artifact_id": item["artifact_ref"]["artifact_id"],
                    "sha256": item["artifact_ref"]["content_hash"],
                    "observed_byte_range": [0, item["artifact_ref"]["size"]],
                    "native_read_call_ids": item["native_read_calls"]} for item in decisions]
                if (not decisions or progress.get("source") != "current_activation_native_observed_pages"
                    or progress.get("status") != "complete" or progress.get("artifacts") != expected):
                    return None
                if self.phase._current_revision() != revision:
                    return None
                return {"policy": ADVICE_POLICY, "workspace_revision": revision,
                    "sources_sha256": _hash(_sources(decisions)), "source_count": len(decisions),
                    "source_bytes": sum(item["artifact_ref"]["size"] for item in decisions),
                    "authority": "advisory_only", "counts_as_acceptance": False}
            except (KeyError, ValueError, TypeError, OSError, ArtifactIntegrityError, WorkspaceConflict, ReducerError):
                return None

    def tool(self):
        return ToolDefinition(name=TOOL,
            description="After fully reading long consultation decisions, record your current decisions, source locations, unresolved risks and next steps to reduce future context. Cover every summary artifact. These are YOUR assessments, not verified evidence; original reports remain readable. Earlier unresolved risks cannot be removed by an update. Does not satisfy reading, checks or submission. Use only when useful; do not copy entire reports or omit critical risks for brevity.",
            input_schema={"type": "object", "properties": {
                "decisions": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                    "artifact_id": {"type": "string"}, "decision": {"type": "string"},
                    "unresolved_risks": {"type": "array", "items": {"type": "string"}}},
                    "required": ["artifact_id", "decision", "unresolved_risks"], "additionalProperties": False}},
                "next_steps": {"type": "string"}}, "required": ["decisions", "next_steps"], "additionalProperties": False},
            validator=self.validate, run=self.record,
            metadata=ToolMetadata(name=TOOL, description="Primary working notes, no execution authority",
                effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.NEVER_AUTO_RETRY))
