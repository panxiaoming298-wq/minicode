"""Opt-in, paired genuine-provider pilot through the product Runtime entry.

This is deliberately a small diagnostic, not a quality benchmark or rollout
gate. No synthetic model, user checkout, external memory, MCP, shell or global
setting mutations. Process stdin carries credentials; reports never persist
the runtime config. Default invocation only prints the frozen protocol.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from minicode.runtime_live_cases import CASE_IDS, EXPORT, PAGINATION, case_prompt, get_case


PROTOCOL = "minicode-live-runtime-pilot-v51"
DATA_ROOT = Path(__file__).resolve().parents[1] / "Package/AgentRuntime/Data/LiveEvaluation"
CHECKER = Path(__file__).with_name("runtime_live_eval_checks.py").resolve()
INITIAL_FILES = PAGINATION.initial_files
PROMPT = PAGINATION.prompt
SYSTEM_PROMPT = "You are a coding assistant. Inspect the provided files, implement the request, and verify the result using available tools. Do not claim a test passed unless its tool result confirms it."


def content_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def protocol(profile: str, max_tokens: int, deadline_seconds: int, primary_first: bool = False,
             *, case_id: str = PAGINATION.case_id, arm: str = "both", consultation_probe: bool = False,
             stage_context: bool = False) -> dict:
    if profile not in {"standard", "deep", "ultra"}:
        raise ValueError("unsupported profile")
    if type(max_tokens) is not int or not 8_000 <= max_tokens <= 64_000:
        raise ValueError("pilot token cap must be an integer from 8000 to 64000 per arm")
    if type(deadline_seconds) is not int or not 30 <= deadline_seconds <= 240:
        raise ValueError("pilot deadline must be an integer from 30 to 240 seconds per arm")
    case = get_case(case_id)
    if arm not in {"both", "primary", "legacy"}:
        raise ValueError("unknown pilot arm")
    if type(consultation_probe) is not bool or (consultation_probe and (
        case_id != EXPORT.case_id or arm != "primary" or profile not in {"deep", "ultra"}
    )):
        raise ValueError("consultation probes require export-policies-v1, primary only, and Deep/Ultra")
    arms = ["primary", "legacy"] if primary_first else ["legacy", "primary"]
    if type(stage_context) is not bool or (stage_context and not consultation_probe):
        raise ValueError("stage context pilots require an explicit consultation probe")
    checker = Path(__file__).with_name(case.checker)
    return {
        "protocol": PROTOCOL, "case": case_id, "profile": profile,
        "arms": arms if arm == "both" else [arm], "consultation_probe": consultation_probe,
        "stage_context": stage_context, "working_context_policy": "candidate-handoff-working-set-v1" if stage_context else "history",
        "required_consultation": {"min_helpers": 2, "before_workspace_write": True, "single_batch": True}
                                 if consultation_probe else None,
        "max_tokens_per_arm": max_tokens, "deadline_seconds_per_arm": deadline_seconds,
        "max_output_tokens_per_request": 4096 if case_id == EXPORT.case_id else 2048,
        "primary_consultation_budget_policy": "shared-pool-v1",
        "primary_consultation_output_admission": "full_configured_output",
        "budget_comparison": "same run cap; helper allocation/output admission differ from fixed-16k v19",
        "consultation_preview_diagnostics": "per-request delivery count; no request or acceptance policy change",
        "consultation_instruction_policy": "focused-assignment-and-observed-sources-v1",
        "consultation_handoff_policy": "decision-summary-optional-report-v2" if consultation_probe else "decision-summary-and-full-report-v1",
        "consultation_summary_repair_policy": "consultation-summary-repair-v1",
        "glm_consultation_summary_repair_policy": "glm-consultation-summary-repair-v1",
        "consultation_source_view_policy": "consultation-source-view-v1",
        "consultation_paged_summary_policy": "consultation-paged-decision-v1" if consultation_probe else None,
        "consultation_audit_view_policy": "consultation-audit-view-v1" if consultation_probe else None,
        "primary_phase_context_policy": "bailian-glm-primary-phase-v3" if consultation_probe else None,
        "primary_request_compiler_policy": "final-request-context-selection-v1" if consultation_probe else None,
        "primary_control_context_policy": "native-primary-control-packing-v1" if consultation_probe else None,
        "primary_working_context_policy": "primary-authored-working-context-v1" if consultation_probe else None,
        "primary_working_advice_policy": "primary-working-context-advice-v1" if consultation_probe else None,
        "primary_thinking_policy": "bailian-primary-phase-thinking-v1",
        "glm_thinking_policies": {"consultation": "bailian-glm-consultation-thinking-v1",
                                  "primary": "bailian-glm-primary-phase-thinking-v1"},
        "consultation_closure_advice_policy": "consultation-closure-advice-v1",
        "consultation_closure_advice_timing": "refresh_before_each_atomic_admission_attempt",
        "tool_failure_advice_policy": "producer-semantics-v1",
        "tool_diagnostics_policy": "bounded-public-failure-shape-v1",
        "submission_rejection_diagnostics": "explicit-not-executed-events-v1",
        "model_output_truncation_policy": "bounded-text-continuation-and-no-tool-prefix-v1",
        "primary_tool_view_policy": "delivered-advice-and-candidate-handoff-v1",
        "max_agents": 3 if case_id == EXPORT.case_id else 2,
        "max_active_workers": 1 if profile != "ultra" else 2,
        "runtime_retry_budget": 0, "model_verifier_budget": 0,
        "primary_repair_limit": 1, "legacy_repair_limit": 0,
        "initial_files_sha256": content_hash(case.initial_files),
        "writable_files": list(case.writable),
        "prompt_sha256": content_hash([SYSTEM_PROMPT, case_prompt(case, consultation_probe=consultation_probe)]),
        "checker_sha256": hashlib.sha256(checker.read_bytes()).hexdigest(),
        "checker_support_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                   for name in case.checker_support},
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(Path(__file__).with_name("runtime_live_cases.py").read_bytes()).hexdigest(),
        "runtime_implementation_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ("runtime_scheduler.py", "unified_runtime.py", "delegation.py", "runtime_protocol.py",
                         "runtime_artifacts.py", "runtime_request_context.py", "runtime_request_compiler.py", "runtime_progress.py", "candidate_working_snapshot.py", "runtime_stage_context.py", "runtime_workspace_context.py", "consultation_context.py", "consultation_forecast.py", "consultation_requirement.py",
                         "tooling.py", "tool_execution.py", "types.py", "tools/read_file.py", "candidate_submission.py", "candidate_closure_context.py", "primary_phase_context.py", "primary_handoff_context.py", "primary_control_context.py", "primary_working_context.py", "working_memory.py", "consultation_submission.py", "consultation_reading.py", "consultation_report_draft.py", "consultation_source_view.py", "primary_closure.py", "agent_intelligence.py", "verifier_registry.py", "workspace_provider.py", "model_registry.py", "openai_adapter.py", "agent_loop.py", "turn_kernel.py", "turn_events.py")},
        "accounting_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                              for name in ("provider_usage.py", "runtime_budget.py", "usage_ledger.py", "runtime_state.py")},
        "prompt_implementation_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                        for name in ("worker_runtime.py", "tools/task.py")},
        "output_policy": "recognized public Bailian Qwen3.7 / Qwen3.8 Flash / GLM-5.2: thinking+answer cap and 10 output-token tolerance; other capabilities and providers retain their semantics",
        "work_chain": False, "memory": False, "network_tools": False,
        "scope": "hand-authored coding pilot; not production-task quality or default-rollout evidence",
    }


def require_live_opt_in(enabled: bool, environment: dict | os._Environ) -> None:
    if not enabled or environment.get("MINICODE_LIVE_RUNTIME_EVAL") != "1":
        raise ValueError("live requests require --run-live and MINICODE_LIVE_RUNTIME_EVAL=1")
    if environment.get("MINI_CODE_MODEL_MODE", "").strip().lower() == "mock":
        raise ValueError("live evaluation cannot run in mock model mode")
    for name in ("MEMORY_V2_ENABLED", "MEMORY_SHADOW_WRITE"):
        if environment.get(name, "").lower() not in {"", "0", "false", "no", "off"}:
            raise ValueError(f"live evaluation requires inactive {name}")


def provider_runtime(config: dict) -> tuple[dict, dict]:
    """Freeze the selected custom deployment only, not unrelated credentials/config."""
    from minicode.model_registry import build_provider_config

    model = str(config.get("model") or "")
    provider = build_provider_config(model, config)
    if provider.provider.value != "custom" or not model or not provider.api_key:
        raise ValueError("this first pilot supports only the configured, authenticated custom provider")
    from urllib.parse import urlsplit
    endpoint = urlsplit(provider.base_url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query:
        raise ValueError("pilot custom endpoint must be HTTPS without embedded credentials/query")
    selected = {
        "provider": "custom", "model": provider.model,
        "customBaseUrl": provider.base_url, "customApiKey": provider.api_key,
        "customExtraHeaders": dict(provider.extra_headers),
    }
    # Keep only the exact selected-model price, if explicitly configured.
    prices = config.get("runtimeModelPricing") or {}
    price_key = f"custom:{provider.model}"
    exact_price = prices.get(price_key) if isinstance(prices, dict) else None
    if exact_price is not None:
        selected["runtimeModelPricing"] = {price_key: exact_price}
    descriptor = {
        "provider": "custom", "model": provider.model, "endpoint_host": endpoint.hostname,
        "endpoint_sha256": content_hash(provider.base_url),
        "explicit_model_price": exact_price is not None,
    }
    return selected, descriptor


def write_report(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # No caller supplies the runtime mapping: only protocol, sanitized traces and results.
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sanitize(value: object, selected: dict) -> object:
    from minicode.release_readiness import redact_sensitive_payload
    encoded = json.dumps(redact_sensitive_payload(value), ensure_ascii=False)
    secrets = [selected.get("customApiKey", ""), *selected.get("customExtraHeaders", {}).values()]
    for secret in secrets:
        if isinstance(secret, str) and secret:
            encoded = encoded.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
    return json.loads(encoded)


def prepare_workspace(path: Path, case_id: str = PAGINATION.case_id) -> str:
    initial_files = get_case(case_id).initial_files
    path.mkdir(parents=True, exist_ok=False)
    for name, content in initial_files.items():
        (path / name).write_text(content, encoding="utf-8")
    return content_hash({name: (path / name).read_text(encoding="utf-8") for name in initial_files})


def check_command(case_id: str = PAGINATION.case_id) -> tuple[str, ...]:
    return (sys.executable, "-I", "-B", str(Path(__file__).with_name(get_case(case_id).checker).resolve()), ".")


def run_checks(workspace: Path, checker_hash: str, case_id: str = PAGINATION.case_id,
               *, support_hashes: dict[str, str] | None = None) -> dict:
    from minicode.path_safety import is_unsafe_stored_path

    case = get_case(case_id)
    checker = Path(__file__).with_name(case.checker)
    if hashlib.sha256(checker.read_bytes()).hexdigest() != checker_hash:
        return {"passed": False, "exit_code": None, "error": "immutable checker changed"}
    if set(support_hashes or {}) != set(case.checker_support) or any(
        hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest
        for name, digest in (support_hashes or {}).items()
    ):
        return {"passed": False, "exit_code": None, "error": "immutable checker support changed or missing"}
    for name, content in case.files:
        path = workspace / name
        if name not in case.writable and (not path.is_file()
            or is_unsafe_stored_path(path, boundary=workspace, reject_hardlinks=True)
            or path.read_text(encoding="utf-8") != content):
            return {"passed": False, "exit_code": None, "error": "immutable task specification changed"}
    completed = subprocess.run(check_command(case_id), cwd=workspace, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
    try:
        report = json.loads(completed.stdout)
    except (TypeError, ValueError):
        report = {"passed": False, "error": "invalid checker result"}
    return {**report, "passed": completed.returncode == 0 and report.get("passed") is True,
            "exit_code": completed.returncode, "stderr": completed.stderr[:2000]}


def bounded_tools(checker_hash: str, case_id: str = PAGINATION.case_id, *, support_hashes: dict[str, str] | None = None):
    """Wrap real file tools with an exact file allowlist, independent of saved permissions."""
    from minicode.runtime_protocol import ToolEffect, ReplayPolicy
    from minicode.tooling import ToolRegistry, ToolDefinition, ToolMetadata, ToolResult
    from minicode.tools.read_file import read_file_tool
    from minicode.tools.write_file import write_file_tool
    from minicode.tools.edit_file import edit_file_tool
    from minicode.tools.task import task_tool
    from minicode.path_safety import is_unsafe_stored_path

    case = get_case(case_id)

    def guarded(definition):
        def run(args, context):
            root = Path(context.cwd).resolve()
            raw = str(args["path"])
            path = (root / raw).resolve()
            permitted = {root / name for name in (case.initial_files if definition.name == "read_file" else case.writable)}
            if path not in permitted or is_unsafe_stored_path(root / raw, boundary=root, reject_hardlinks=True):
                return ToolResult(ok=False, output="Pilot file boundary: only the declared sample files are accessible.",
                                  failureKind="policy_denied")
            if definition.name != "read_file" and len(str(args.get("content", args.get("replace", ""))).encode("utf-8")) > 20_000:
                return ToolResult(ok=False, output="Pilot source limit is 20 KB.", failureKind="invalid_input")
            return definition.run(args, context)
        return replace(definition, run=run)

    def validate_checks(args):
        if not isinstance(args, dict) or args:
            raise ValueError("run_acceptance_checks accepts no arguments")
        return {}

    def execute_checks(args, context):
        from minicode.workspace_provider import snapshot_revision, workspace_snapshot
        before_revision = snapshot_revision(workspace_snapshot(Path(context.cwd)))
        result = run_checks(Path(context.cwd), checker_hash, case_id, support_hashes=support_hashes)
        after_revision = snapshot_revision(workspace_snapshot(Path(context.cwd)))
        # Only a completed, well-formed failing suite earns this semantic tag.
        # Invalid input, tampered checkers/specifications, load errors (zero
        # checks), timeouts and crashes must retain ordinary failure handling.
        verification_failed = (
            result.get("passed") is False and result.get("exit_code") == 1
            and type(result.get("checks")) is int and result["checks"] > 0
            and isinstance(result.get("failures"), list) and bool(result["failures"])
            and all(isinstance(item, str) and item for item in result["failures"])
            and "error" not in result
        )
        return ToolResult(ok=result["passed"], output=json.dumps(result, ensure_ascii=False),
                          verificationFailed=verification_failed,
                          contextCandidateRevision=(after_revision if result["passed"] is True
                              and before_revision == after_revision else None))

    check_tool = ToolDefinition(
        name="run_acceptance_checks", description=("Run the immutable pagination acceptance suite against the current workspace. No arguments or arbitrary commands."
            if case_id == PAGINATION.case_id else "Run both immutable export-policy acceptance suites against the current workspace. No arguments or arbitrary commands."),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        validator=validate_checks, run=execute_checks,
        metadata=ToolMetadata(name="run_acceptance_checks", description="Fixed pure-function checks",
                              effect=ToolEffect.READ_ONLY, replay_policy=ReplayPolicy.SAFE,
                              context_result_kind="candidate_check"),
    )
    return ToolRegistry([guarded(tool) for tool in (read_file_tool, write_file_tool, edit_file_tool)] + [task_tool, check_tool])


class PilotToolObserver:
    """Read-only, thread-safe observer of actual production tool events.

    Tool records keep identifiers, file names and outcomes, not arguments or
    output bodies. A separate diagnostic timeline keeps bounded public control
    messages and evidence summaries, never model thinking or stream chunks.
    No tool, model response, scheduling decision or Runtime status is replaced.
    """
    thread_safe_tool_events = True
    include_stream_chunks = False
    include_thinking_chunks = False

    def __init__(self):
        self._lock = threading.Lock()
        self._started = {}
        self.records = []
        self.control_records = []

    @staticmethod
    def _input_shape(value):
        # Only fixed, public schema names; no values or attacker-chosen keys.
        def shape(item):
            if item is None: return {"type": "null"}
            if isinstance(item, bool): return {"type": "boolean"}
            if isinstance(item, str): return {"type": "string", "utf8_bytes": len(item.encode("utf-8", errors="replace"))}
            if isinstance(item, dict): return {"type": "object", "fields": len(item)}
            if isinstance(item, (list, tuple)): return {"type": "array", "items": len(item)}
            if isinstance(item, (int, float)): return {"type": "number"}
            return {"type": "other"}
        result = shape(value)
        if isinstance(value, dict):
            names = {"path", "offset", "limit", "content", "find", "replace", "report", "summary", "draft_id", "tasks"}
            known = sorted(names.intersection(value))
            result.update(known_fields={key: shape(value[key]) for key in known}, unknown_field_count=len(value) - len(known))
        return result

    @staticmethod
    def _failure_diagnostic(tool, output, input_shape):
        # Diagnostic interpretation of public output, never producer semantics
        # or authority used by scheduling, retries, acceptance or model prompts.
        encoded = output.encode("utf-8", errors="replace")
        diagnostic = {"policy": "bounded-public-failure-shape-v1", "source": "public_tool_output_shape",
            "changes_authority": False, "category": "unclassified", "input_shape": input_shape,
            "output_utf8_bytes": len(encoded), "output_sha256": hashlib.sha256(encoded).hexdigest()}
        prefixes = (("ModelToolArgumentsRejected:", "invalid_json"),
            (f"Input validation error in {tool}:", "invalid_input"),
            (f"Unknown tool: {tool}", "unavailable_tool"), ("NotExecuted:", "not_executed"),
            ("PolicyDenied:", "policy_denied"), ("Pilot file boundary:", "policy_denied"))
        for prefix, category in prefixes:
            if output.startswith(prefix):
                diagnostic["category"] = category
                return diagnostic
        # Parse only bounded checker results. A partial failure sample is not
        # the full failure count; old checkers leave that number unknown.
        if tool == "run_acceptance_checks" and len(encoded) <= 65536:
            try:
                report = json.loads(output)
            except (ValueError, TypeError, RecursionError):
                report = None
            if isinstance(report, dict):
                failures, count = report.get("failures"), report.get("checks")
                if (report.get("passed") is False and report.get("exit_code") == 1
                    and type(count) is int and count > 0 and "error" not in report
                    and isinstance(failures, list) and failures and all(isinstance(item, str) and item for item in failures)):
                    total = report.get("failure_count")
                    diagnostic.update(category="acceptance_failed", checks={"count": count,
                        "failure_sample_count": len(failures),
                        "failure_count": total if type(total) is int and len(failures) <= total <= count else None})
                elif report.get("passed") is False and ("error" in report or count == 0):
                    diagnostic["category"] = "checker_error"
        return diagnostic

    def on_event(self, event):
        if event.kind in {"runtime", "progress", "tool_start", "tool_result", "tool_rejected"}:
            # Observe only already-public Worker activity. Do not bypass the
            # coordinator's suppression of assistant/Done events or collect
            # model thinking/stream chunks. A Worker stop is NOT Gate success.
            runtime_event = event.runtime_event
            content = event.content
            evidence = runtime_event.evidence_summary if runtime_event else ""
            with self._lock:
                self.control_records.append({
                    "sequence": len(self.control_records) + 1,
                    "node_id": event.node_id, "attempt_id": event.attempt_id,
                    "kind": event.kind, "step": event.step,
                    "category": runtime_event.category if runtime_event else None,
                    "phase": runtime_event.phase if runtime_event else None,
                    "stop_reason": event.stop_reason,
                    "tool": event.tool_name or None, "tool_call_id": event.tool_call_id or None,
                    "is_error": event.is_error if event.kind in {"tool_result", "tool_rejected"} else None,
                    "content": content[:1200], "content_truncated": len(content) > 1200,
                    "evidence_summary": evidence[:280], "evidence_truncated": len(evidence) > 280,
                })
        if event.kind == "tool_rejected":
            # Self-contained admission receipt: no matching start exists.
            raw = event.tool_input.get("path") if isinstance(event.tool_input, dict) else None
            with self._lock:
                self.records.append({"node_id": event.node_id, "attempt_id": event.attempt_id,
                    "tool_call_id": event.tool_call_id, "tool": event.tool_name,
                    "file": Path(raw).name if isinstance(raw, str) else None,
                    "ok": False, "execution": "not_executed",
                    "rejection_reason": event.content if event.content in {
                        "invalid_batch", "invalid_submission_input", "prior_call_incomplete",
                        "model_output_truncated", "transport_incomplete",
                        "response_parse_incomplete"} else "unknown",
                    "failure_diagnostic": self._failure_diagnostic(event.tool_name, event.output, self._input_shape(event.tool_input))})
            return
        if event.kind not in {"tool_start", "tool_result"}:
            return
        identity = (event.node_id, event.attempt_id, event.tool_call_id)
        with self._lock:
            if event.kind == "tool_start":
                raw = event.tool_input.get("path") if isinstance(event.tool_input, dict) else None
                self._started[identity] = {
                    "node_id": event.node_id, "attempt_id": event.attempt_id,
                    "tool_call_id": event.tool_call_id, "tool": event.tool_name,
                    "file": Path(raw).name if isinstance(raw, str) else None,
                    "input_shape": self._input_shape(event.tool_input),
                }
            else:
                started = self._started.pop(identity, None)
                if started is not None and started["tool"] == event.tool_name:
                    shape = started.pop("input_shape")
                    self.records.append({**started, "ok": event.is_error is False,
                        **({"failure_diagnostic": self._failure_diagnostic(event.tool_name, event.output, shape)}
                           if event.is_error is True else {})})


def consultation_observations(state: dict, tool_records: list[dict] | None = None) -> dict:
    """Project real reducer events; absence is no coverage, never a simulated run."""
    nodes = state.get("nodes", {})
    helpers = {key: value for key, value in nodes.items() if key.startswith("consult-")}
    calls = state.get("usage_ledger", {}).get("calls", {})
    helper_calls = {key for key, value in calls.items() if value.get("node_id") in helpers}
    active_workers, active_calls = set(), set()
    primary_active = overlap = False
    worker_peak = call_peak = suspensions = resumable = deliveries = 0
    for event in state.get("events", []):
        kind, payload = event.get("event_type"), event.get("payload", {})
        if payload.get("node_id") == "primary":
            if kind == "AttemptStarted":
                primary_active = True
            elif kind == "ResultAccepted":
                primary_active = False
        if payload.get("node_id") in helpers:
            if kind == "AttemptStarted":
                active_workers.add(payload.get("attempt_id"))
            elif kind == "ResultAccepted":
                active_workers.discard(payload.get("attempt_id"))
        call_id = payload.get("call_id")
        if call_id in helper_calls:
            if kind == "ModelCallReserved":
                active_calls.add(call_id)
            elif kind == "ModelUsageRecorded" and (payload.get("complete") is True or payload.get("outcome") not in {None, "pending"}):
                active_calls.discard(call_id)
        worker_peak = max(worker_peak, len(active_workers))
        call_peak = max(call_peak, len(active_calls))
        overlap = overlap or (primary_active and bool(active_workers))
        suspensions += kind == "PrimarySuspended"
        resumable += kind == "PrimaryResumable"
        deliveries += kind == "ConsultationDelivered"
    return {
        "helper_nodes": len(helpers), "helper_model_calls": len(helper_calls),
        "helper_model_nodes": len({calls[key]["node_id"] for key in helper_calls}),
        "helper_worker_peak": worker_peak, "helper_admitted_call_peak": call_peak,
        "primary_helper_overlap": overlap,
        "primary_activations": len(nodes.get("primary", {}).get("attempts", [])),
        "suspensions": suspensions, "resumable_events": resumable, "deliveries": deliveries,
        "helpers_succeeded": bool(helpers) and all(node.get("state") == "SUCCEEDED" for node in helpers.values()),
        "helpers_required": bool(helpers) and all(node.get("definition", {}).get("criticality", "required") == "required" for node in helpers.values()),
        "successful_reads": {node_id: sorted({item["file"] for item in (tool_records or [])
            if item.get("node_id") == node_id and item.get("tool") == "read_file" and item.get("ok") is True and item.get("file")})
            for node_id in helpers},
        "helpers_read_only": bool(helpers) and all(
            node.get("definition", {}).get("allowed_effects") == ["read_only"]
            and node.get("result") is not None and not node["result"].get("changed_resources")
            for node in helpers.values()),
    }


def probe_passed(score: dict, profile: str) -> bool:
    observed = score.get("consultation", {})
    required_peak = {"deep": 1, "ultra": 2}.get(profile)
    reads = observed.get("successful_reads", {})
    window_readers = {node for node, files in reads.items() if {"SPEC.md", "WINDOW_SPEC.md", "batch_window.py"} <= set(files)}
    retry_readers = {node for node, files in reads.items() if {"SPEC.md", "RETRY_SPEC.md", "retry_delay.py"} <= set(files)}
    return bool(required_peak and score.get("verified_delivery") is True
        and any(left != right for left in window_readers for right in retry_readers)
        and observed.get("helper_nodes") == observed.get("helper_model_nodes") == 2
        and observed.get("helper_worker_peak") == observed.get("helper_admitted_call_peak") == required_peak
        and observed.get("primary_activations") == 2
        and observed.get("suspensions") == observed.get("resumable_events") == observed.get("deliveries") == 1
        and observed.get("helpers_succeeded") is True and observed.get("helpers_read_only") is True
        and observed.get("helpers_required") is True and observed.get("primary_helper_overlap") is False)


def score_trace(trace: dict, acceptance: dict, *, pinned_model: str, initial_hash_matches: bool,
                checker_unchanged: bool, runtime_error: bool = False) -> dict:
    """Fail closed: a good file alone is not successful Runtime delivery."""
    scheduler = trace.get("scheduler", {})
    state = trace.get("state", {})
    calls = list(state.get("usage_ledger", {}).get("calls", {}).values())
    from minicode.consultation_forecast import input_reservation_observations
    decisions = trace.get("reasoning_budget_decisions", [])
    pinned = bool(calls) and all(call.get("model") == pinned_model for call in calls)
    settled = bool(calls) and all(call.get("status") == "settled" for call in calls)
    reasoning = [call.get("usage", {}).get("reasoning_tokens") for call in calls]
    prepared = [call.get("usage", {}).get("prepared_request", {}) for call in calls]
    # Gate status is an enum string, not the model's final prose.
    delivered = trace.get("delivery", {}).get("completed") is True
    valid = (initial_hash_matches and checker_unchanged and pinned and settled
             and scheduler.get("tokens_known") is True and bool(decisions) and not runtime_error)
    return {
        "valid_live_measurement": valid,
        "verified_delivery": valid and delivered and acceptance.get("passed") is True,
        "gate_success": delivered, "acceptance_passed": acceptance.get("passed") is True,
        "model_pinned": pinned, "all_calls_settled": settled,
        "model_calls": len(calls), "tokens": scheduler.get("tokens_used"),
        "tokens_known": scheduler.get("tokens_known") is True,
        "cost": scheduler.get("cost_used") if scheduler.get("cost_known") is True else None,
        "cost_known": scheduler.get("cost_known") is True,
        "reasoning_applied": sorted({str(item.get("applied")) for item in decisions}),
        # Keep the existing display field for old report readers, but never
        # confuse its stringified None with an actually applied budget.
        "reasoning_applied_known": bool(decisions) and all(
            isinstance(item.get("applied"), str) and bool(item["applied"].strip())
            and item["applied"] not in {"None", "null"} for item in decisions
        ),
        "reasoning_degraded": any(item.get("degraded") for item in decisions),
        "reasoning_tokens": sum(reasoning) if reasoning and all(type(n) is int for n in reasoning) else None,
        "reasoning_counter_calls": sum(type(n) is int for n in reasoning),
        "prepared_complete_output_calls": sum(item.get("output_scope") == "thinking_and_answer" for item in prepared),
        "prepared_input_bytes": {
            kind: sum(item[kind] for item in prepared) if prepared and all(type(item.get(kind)) is int for item in prepared) else None
            for kind in ("messages_utf8_bytes", "tools_utf8_bytes")
        },
        "input_reservation_observations": input_reservation_observations(calls),
        "peak_active_workers": scheduler.get("peak_active_workers"),
        "claimed_attempts": scheduler.get("claimed_attempts"),
        "logical_nodes": len(state.get("nodes", {})),
        "consultation": consultation_observations(state),
        "reasons": trace.get("delivery", {}).get("reasons", []),
    }


def run_arm(payload: dict) -> dict:
    from minicode.model_registry import create_model_adapter
    from minicode.permissions import PermissionManager
    from minicode.runtime_artifacts import RuntimeArtifactStore
    from minicode.unified_runtime import run_runtime_turn

    selected, plan = payload["selected"], payload["protocol"]
    arm, root = payload["arm"], Path(payload["root"]).resolve()
    expected = protocol(plan["profile"], plan["max_tokens_per_arm"],
                        plan["deadline_seconds_per_arm"], plan["arms"][0] == "primary",
                        case_id=plan["case"], arm="both" if len(plan["arms"]) == 2 else plan["arms"][0],
                        consultation_probe=plan["consultation_probe"], stage_context=plan["stage_context"])
    if plan != expected or arm not in plan["arms"]:
        raise ValueError("worker protocol differs from the frozen bounded pilot")
    if not root.is_relative_to(DATA_ROOT.resolve()) or root == DATA_ROOT.resolve():
        raise ValueError("worker artifacts must stay inside the live evaluation data directory")
    workspace = root / "workspace"
    case = get_case(plan["case"])
    initial = prepare_workspace(workspace, case.case_id)
    baseline = run_checks(workspace, plan["checker_sha256"], case.case_id, support_hashes=plan["checker_support_sha256"])
    if baseline["passed"]:
        raise ValueError("pilot baseline unexpectedly passes; do not measure a no-op task")
    tools = bounded_tools(plan["checker_sha256"], case.case_id, support_hashes=plan["checker_support_sha256"])
    permissions = PermissionManager(str(workspace))
    permissions.turn_allow_all_edits = True  # Only exact sample paths survive the tool boundary.
    runtime = {
        **selected, "runtimeV2": True, "executionProfile": plan["profile"],
        **({"requiredConsultation": plan["required_consultation"]} if plan["required_consultation"] else {}),
        "runtimeOrchestration": arm, "runtimeDurableConsultation": arm == "primary",
        "runtimeConsultationPagedSummary": plan.get("consultation_paged_summary_policy") == "consultation-paged-decision-v1",
        "runtimeConsultationHandoff": plan.get("consultation_handoff_policy"),
        "runtimeConsultationAuditView": plan.get("consultation_audit_view_policy") == "consultation-audit-view-v1",
        **({"runtimePrimaryPhaseContext": plan["primary_phase_context_policy"]}
           if plan.get("primary_phase_context_policy") is not None else {}),
        **({"runtimeWorkingContext": plan["working_context_policy"]} if plan["stage_context"] else {}),
        "runtimeMaxRepairRounds": plan[f"{arm}_repair_limit"], "runtimeMaxContinuations": 2,
        "runtimeMaxTokens": plan["max_tokens_per_arm"], "maxOutputTokens": plan["max_output_tokens_per_request"],
        "runtimeDeadline": time.time() + plan["deadline_seconds_per_arm"],
        "runtimeRetryBudget": 0, "runtimeVerifierBudget": 0,
        "runtimeMaxAgents": plan["max_agents"], "runtimeMaxActiveWorkers": plan["max_active_workers"],
        "runtimeNetworkAccess": False, "memoryV2": False, "memoryShadowWrite": False,
        "runtimeStatePath": str(root / "state.json"), "runtimeTemporaryRoot": str(root / "temporary"),
        "runtimeLockRoot": str(root / "locks"), "taskRequiresWorkspaceWrite": True,
        "acceptanceCriteria": list(case.criteria),
        "expectedOutputs": list(case.writable), "taskConstraints": [case.initial_files["SPEC.md"]],
        "runtimeVerificationCommands": [{"name": "pilot-contract", "kind": "unit_test",
                                          "command": list(check_command(case.case_id)), "timeout": 8,
                                          "covers": list(range(1, len(case.criteria) + 1))}],
    }
    model = create_model_adapter(selected["model"], tools, runtime)
    if not getattr(model, "supports_runtime_usage", False):
        raise ValueError("genuine adapter with request-level accounting is required")
    started = time.monotonic()
    observer = PilotToolObserver()
    error = None
    messages = []
    try:
        messages = run_runtime_turn(
            model=model, tools=tools, messages=[{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": case_prompt(case, consultation_probe=plan["consultation_probe"])}],
            cwd=str(workspace), runtime=runtime, permissions=permissions, enable_work_chain=False,
            callbacks=observer,
            artifact_store=RuntimeArtifactStore(root / "artifacts"),
        )
    except Exception as caught:
        error = {"type": type(caught).__name__, "message": str(caught)}
    elapsed = time.monotonic() - started
    acceptance = run_checks(workspace, plan["checker_sha256"], case.case_id, support_hashes=plan["checker_support_sha256"])
    trace = runtime.get("lastRuntimeTrace", {})
    result = {
        "arm": arm, "elapsed_seconds": elapsed, "baseline": baseline, "acceptance": acceptance,
        "protocol_sha256": content_hash(plan),
        "initial_files_sha256": initial, "error": error,
        "score": score_trace(trace, acceptance, pinned_model=selected["model"],
                             initial_hash_matches=initial == plan["initial_files_sha256"],
                             checker_unchanged=hashlib.sha256(Path(__file__).with_name(case.checker).read_bytes()).hexdigest() == plan["checker_sha256"]
                             and all(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() == digest
                                     for name, digest in plan["checker_support_sha256"].items()),
                             runtime_error=error is not None),
    }
    result["score"]["consultation"] = consultation_observations(trace.get("state", {}), observer.records)
    result["score"]["consultation_probe_passed"] = probe_passed(result["score"], plan["profile"]) if plan["consultation_probe"] else None
    write_report(root / "trace.json", sanitize(trace, selected))
    write_report(root / "messages.json", sanitize(messages, selected))
    write_report(root / "tool-events.json", sanitize(observer.records, selected))
    write_report(root / "control-events.json", sanitize(observer.control_records, selected))
    write_report(root / "result.json", sanitize(result, selected))
    return result


def summarize_pair(results: list[dict]) -> dict:
    by_arm = {item["arm"]: item for item in results}
    complete = set(by_arm) == {"legacy", "primary"} and len(results) == 2
    valid = complete and all(item.get("score", {}).get("valid_live_measurement") is True for item in results)
    efforts = [item.get("score", {}).get("reasoning_applied") for item in results]
    frozen = complete and bool(results[0].get("protocol_sha256")) and (
        results[0].get("protocol_sha256") == results[1].get("protocol_sha256")
    )
    comparable = (valid and frozen
                  and all(item.get("score", {}).get("reasoning_applied_known") is True for item in results)
                  and bool(efforts[0]) and efforts[0] == efforts[1])
    return {
        "complete_pair": complete, "comparable_applied_reasoning": comparable,
        "same_frozen_protocol": frozen,
        "verified_deliveries": {arm: item.get("score", {}).get("verified_delivery", False) for arm, item in by_arm.items()},
        "consultation_probes": {arm: item.get("score", {}).get("consultation_probe_passed") for arm, item in by_arm.items()},
        "eligible_for_default_switch": False,
        "limitation": "One hand-authored pilot, no statistical quality claim; work-chain/memory disabled. Unknown cost is not zero. Failures are retained.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--profile", choices=("standard", "deep", "ultra"), default="deep")
    parser.add_argument("--max-tokens", type=int, default=32_000)
    parser.add_argument("--deadline-seconds", type=int, default=180)
    parser.add_argument("--primary-first", action="store_true")
    parser.add_argument("--case", choices=CASE_IDS, default=PAGINATION.case_id)
    parser.add_argument("--arm", choices=("both", "primary", "legacy"), default="both")
    parser.add_argument("--consultation-probe", action="store_true", help="Explicit two-helper protocol coverage, separate from autonomous task quality")
    parser.add_argument("--stage-context", action="store_true", help="Opt-in completed-consultation working context; keep saved settings unchanged")
    parser.add_argument("--model", help="Explicit model ID for this pilot only, using the existing custom deployment; never changes saved settings")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.model is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]{0,159}", args.model):
        parser.error("--model requires a nonempty bounded model ID without whitespace")
    if args.worker and args.model is not None:
        parser.error("worker model must come from the frozen parent deployment")
    plan = protocol(args.profile, args.max_tokens, args.deadline_seconds, args.primary_first,
                    case_id=args.case, arm=args.arm, consultation_probe=args.consultation_probe, stage_context=args.stage_context)
    if not args.run_live:
        if args.worker:
            parser.error("worker requires live opt-in")
        print(json.dumps({"status": "preflight_only", "requested_model": args.model, **plan}, ensure_ascii=False, indent=2))
        return 0
    require_live_opt_in(args.run_live, os.environ)
    if args.worker:
        payload = json.loads(sys.stdin.read())
        run_arm(payload)
        return 0
    from minicode.config import load_runtime_config
    configured = load_runtime_config(str(Path.cwd()))
    selected, descriptor = provider_runtime({**configured, **({"model": args.model} if args.model is not None else {})})
    run_root = DATA_ROOT / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run_root.mkdir(parents=True, exist_ok=False)
    write_report(run_root / "protocol.json", {**plan, "deployment": descriptor})
    results = []
    print(f"Pilot artifacts: {run_root}", flush=True)
    for arm in plan["arms"]:
        root = run_root / arm
        root.mkdir(exist_ok=False)
        print(f"Starting {arm}: {plan['max_tokens_per_arm']} tokens, {plan['deadline_seconds_per_arm']}s runtime deadline", flush=True)
        payload = {"selected": selected, "protocol": plan, "arm": arm, "root": str(root)}
        try:
            completed = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "minicode.runtime_live_eval", "--run-live", "--worker"],
                input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=plan["deadline_seconds_per_arm"] + 30, check=False,
            )
            write_report(root / "process.json", sanitize({"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, selected))
            if completed.returncode != 0 or not (root / "result.json").is_file():
                result = {"arm": arm, "error": "worker_process_failed", "score": {"valid_live_measurement": False, "verified_delivery": False}}
                write_report(root / "result.json", result)
            else:
                result = json.loads((root / "result.json").read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired as error:
            # subprocess.run kills/waits for this worker. No arbitrary subprocess tools are exposed.
            result = {"arm": arm, "error": "outer_process_timeout", "score": {"valid_live_measurement": False, "verified_delivery": False}}
            write_report(root / "process.json", sanitize({"stdout": str(error.stdout or ""), "stderr": str(error.stderr or ""), "timeout": True}, selected))
            write_report(root / "result.json", result)
        results.append(result)
        write_report(run_root / "results.json", {"protocol": plan, "deployment": descriptor, "arms": results, "summary": summarize_pair(results)})
        print(f"Finished {arm}: {json.dumps(result.get('score', {}), ensure_ascii=False)}", flush=True)
    summary = summarize_pair(results)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if all(item.get("score", {}).get("verified_delivery") is True
                    and (not plan["consultation_probe"] or item["score"].get("consultation_probe_passed") is True)
                    for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
