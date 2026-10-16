"""Opt-in genuine-model repair after a disclosed deterministic failed candidate.

Default execution is a credential-free preflight. No model is substituted and
no old run is resumed: each live probe prepares a new, isolated, verifiably
failing fixture, then uses run_runtime_turn(runtimeResume=True) without hooks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from minicode.runtime_live_cases import EXPORT
from minicode.runtime_live_eval import (
    SYSTEM_PROMPT, PilotToolObserver, bounded_tools, check_command, content_hash, prepare_workspace,
    protocol as execution_protocol, provider_runtime, require_live_opt_in, run_checks, sanitize, score_trace, write_report,
)
from minicode.runtime_repair_seed import SEED_FILES, SEED_ID, SEED_NOTICE, prepare_repair_seed

DATA_ROOT = Path(__file__).resolve().parents[1] / "Package/AgentRuntime/Data/RepairEvaluation"
PROTOCOL = "minicode-live-repair-pilot-v2"


def probe_messages() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": EXPORT.prompt + "\n\n" + SEED_NOTICE}]


def protocol(profile: str, max_tokens: int, deadline_seconds: int) -> dict:
    if profile not in {"deep", "ultra"}:
        raise ValueError("repair probe requires Deep or Ultra")
    base = execution_protocol(profile, max_tokens, deadline_seconds, case_id=EXPORT.case_id, arm="primary")
    return {**base, "parent_execution_protocol": base["protocol"], "protocol": PROTOCOL,
        "scenario": "deterministic_failed_candidate_then_genuine_model_repair",
        "fixture_id": SEED_ID, "fixture_origin": "hand_authored_not_model_output",
        "fixture_source_sha256": content_hash(SEED_FILES), "prompt_sha256": content_hash(probe_messages()),
        "repair_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "repair_seed_sha256": hashlib.sha256(Path(__file__).with_name("runtime_repair_seed.py").read_bytes()).hexdigest(),
        "seed_model_calls": 0, "max_repair_rounds": 1, "max_active_workers": 1,
        "old_run_resume": False, "preloaded_consultation_reports": False,
        "deadline_includes_seed_preparation": True,
        "scope": "targeted repair of a disclosed fixture; not autonomous full-chain quality or rollout evidence"}


def probe_runtime(selected: dict, plan: dict, root: Path, *, deadline: float) -> dict:
    """One policy identity for both fixture preparation and model restoration."""
    return {**selected, "runtimeV2": True, "executionProfile": plan["profile"],
        "runtimeOrchestration": "primary", "runtimeDurableConsultation": True,
        "runtimeMaxRepairRounds": 1, "runtimeMaxContinuations": 2,
        "runtimeMaxTokens": plan["max_tokens_per_arm"], "maxOutputTokens": plan["max_output_tokens_per_request"],
        "runtimeDeadline": deadline, "runtimeRetryBudget": 0, "runtimeVerifierBudget": 0,
        "runtimeMaxAgents": plan["max_agents"], "runtimeMaxActiveWorkers": 1,
        "runtimeNetworkAccess": False, "memoryV2": False, "memoryShadowWrite": False,
        "runtimeStatePath": str(root / "state.json"), "runtimeTemporaryRoot": str(root / "temporary"),
        "runtimeLockRoot": str(root / "locks"), "runtimeRunId": "repair-probe-" + root.name,
        "runtimeRepairProbe": SEED_ID, "taskRequiresWorkspaceWrite": True,
        "acceptanceCriteria": list(EXPORT.criteria), "expectedOutputs": list(EXPORT.writable),
        "taskConstraints": [EXPORT.initial_files["SPEC.md"]],
        "runtimeVerificationCommands": [{"name": "pilot-contract", "kind": "unit_test",
            "command": list(check_command(EXPORT.case_id)), "timeout": 8, "covers": [1, 2]}]}


def run_probe(payload: dict) -> dict:
    from minicode.model_registry import create_model_adapter
    from minicode.permissions import PermissionManager
    from minicode.runtime_artifacts import RuntimeArtifactStore
    from minicode.unified_runtime import run_runtime_turn

    selected, plan = payload["selected"], payload["protocol"]
    root = Path(payload["root"]).resolve()
    if plan != protocol(plan["profile"], plan["max_tokens_per_arm"], plan["deadline_seconds_per_arm"]):
        raise ValueError("repair probe differs from its frozen protocol")
    if not root.is_relative_to(DATA_ROOT.resolve()) or root == DATA_ROOT.resolve():
        raise ValueError("repair probe must stay in its dedicated Data directory")
    if any((root / name).exists() for name in ("workspace", "state.json", "seed-state.json", "result.json")):
        raise ValueError("repair probe never overwrites or resets an existing run")
    started = time.monotonic()
    runtime = probe_runtime(selected, plan, root, deadline=time.time() + plan["deadline_seconds_per_arm"])
    workspace = root / "workspace"
    initial = prepare_workspace(workspace, EXPORT.case_id)
    baseline = run_checks(workspace, plan["checker_sha256"], EXPORT.case_id, support_hashes=plan["checker_support_sha256"])
    if baseline["passed"]:
        raise ValueError("repair probe baseline unexpectedly passes")
    tools = bounded_tools(plan["checker_sha256"], EXPORT.case_id, support_hashes=plan["checker_support_sha256"])
    store = RuntimeArtifactStore(root / "artifacts")
    messages = probe_messages()
    seed = prepare_repair_seed(root=root, runtime=runtime, tools=tools, messages=messages, artifact_store=store)
    seed_acceptance = run_checks(Path(seed["candidate_workspace"]), plan["checker_sha256"], EXPORT.case_id,
                                support_hashes=plan["checker_support_sha256"])
    write_report(root / "seed-acceptance.json", seed_acceptance)
    if seed_acceptance["passed"] or seed_acceptance.get("checks", 0) <= 0:
        raise ValueError("the prepared candidate must fail actual completed checks before any model call")
    seeded_at = time.monotonic()
    # Creation here does not replace a fixture with a model-shaped object. The
    # fixture is already over; normal Runtime restore builds actual AgentLoops.
    model = create_model_adapter(selected["model"], tools, runtime)
    if not getattr(model, "supports_runtime_usage", False):
        raise ValueError("repair probe requires genuine request-level model accounting")
    permissions = PermissionManager(str(workspace))
    permissions.turn_allow_all_edits = True  # Exact pilot file guards remain authoritative.
    observer = PilotToolObserver()
    runtime["runtimeResume"] = True
    error, output_messages = None, []
    try:
        output_messages = run_runtime_turn(model=model, tools=tools, messages=messages, cwd=str(workspace),
            runtime=runtime, permissions=permissions, artifact_store=store, callbacks=observer, enable_work_chain=False)
    except Exception as caught:
        error = {"type": type(caught).__name__, "message": str(caught)}
    finished = time.monotonic()
    elapsed = finished - started
    acceptance = run_checks(workspace, plan["checker_sha256"], EXPORT.case_id, support_hashes=plan["checker_support_sha256"])
    trace = runtime.get("lastRuntimeTrace", {})
    score = score_trace(trace, acceptance, pinned_model=selected["model"],
        initial_hash_matches=initial == plan["initial_files_sha256"],
        checker_unchanged=hashlib.sha256(Path(__file__).with_name(EXPORT.checker).read_bytes()).hexdigest() == plan["checker_sha256"]
            and all(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() == sha for name, sha in plan["checker_support_sha256"].items()),
        runtime_error=error is not None)
    calls = list(trace.get("state", {}).get("usage_ledger", {}).get("calls", {}).values())
    phases = [(call.get("usage") or {}).get("prepared_request", {}).get("thinking_output_budget") for call in calls]
    score.update({"fixture_origin": seed["origin"], "seed_model_calls": seed["model_calls"],
        "repair_phase_model_calls": sum(bool(p and p.get("phase") == "candidate_repair") for p in phases),
        "automatic_repair_thinking_calls": sum(bool(p and p.get("phase") == "candidate_repair"
            and p.get("source") == "runtime_primary") for p in phases),
        "reasoning_only_length_calls": sum(bool((shape := (call.get("usage") or {}).get("response_shape", {}))
            and shape.get("stop_reason") == "length" and shape.get("reasoning_utf8_bytes", 0) > 0
            and shape.get("content_utf8_bytes") == 0 and shape.get("tool_call_count") == 0) for call in calls),
        "eligible_for_default_switch": False,
        "limitation": "Genuine model repair after a deterministic failed fixture; no autonomous initial candidate or full collaboration-chain evidence."})
    result = {"protocol": plan, "protocol_sha256": content_hash(plan), "elapsed_seconds": elapsed,
        "preparation_seconds": seeded_at - started, "model_phase_seconds": finished - seeded_at,
        "baseline": baseline, "seed_acceptance": seed_acceptance, "acceptance": acceptance,
        "seed": seed, "score": score, "error": error}
    for name, value in (("trace.json", trace), ("messages.json", output_messages),
                        ("tool-events.json", observer.records), ("control-events.json", observer.control_records),
                        ("result.json", result)):
        write_report(root / name, sanitize(value, selected))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--profile", choices=("deep", "ultra"), default="deep")
    parser.add_argument("--max-tokens", type=int, default=64000)
    parser.add_argument("--deadline-seconds", type=int, default=240)
    parser.add_argument("--model", help="Explicit model override for this probe only; saved settings are unchanged")
    args = parser.parse_args(argv)
    if args.model is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]{0,159}", args.model):
        parser.error("--model requires a bounded model ID without whitespace")
    if args.worker and args.model is not None:
        parser.error("worker model must come from the frozen parent deployment")
    plan = protocol(args.profile, args.max_tokens, args.deadline_seconds)
    if not args.run_live:
        if args.worker:
            parser.error("worker requires live opt-in")
        print(json.dumps({"status": "preflight_only", "requested_model": args.model, **plan}, ensure_ascii=False, indent=2))
        return 0
    require_live_opt_in(args.run_live, os.environ)
    if args.worker:
        run_probe(json.loads(sys.stdin.read()))
        return 0
    from minicode.config import load_runtime_config
    config = load_runtime_config(Path.cwd(), trust_project_mcp=False)
    selected, descriptor = provider_runtime({**config, **({"model": args.model} if args.model else {})})
    root = DATA_ROOT / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=False)
    write_report(root / "protocol.json", {**plan, "deployment": descriptor})
    print(f"Repair probe artifacts: {root}", flush=True)
    try:
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "minicode.runtime_repair_eval", "--run-live", "--worker"],
            input=json.dumps({"selected": selected, "protocol": plan, "root": str(root)}),
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            timeout=plan["deadline_seconds_per_arm"] + 30)
        write_report(root / "process.json", sanitize({"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, selected))
        if completed.returncode != 0 or not (root / "result.json").is_file():
            if not (root / "result.json").exists():
                write_report(root / "result.json", {"protocol": plan, "error": "worker_process_failed",
                    "score": {"valid_live_measurement": False, "verified_delivery": False, "eligible_for_default_switch": False}})
            print("Repair probe worker failed; original state and process diagnostics are preserved.", flush=True)
            return 1
    except subprocess.TimeoutExpired as error:
        write_report(root / "process.json", sanitize({"timeout": True, "stdout": str(error.stdout or ""), "stderr": str(error.stderr or "")}, selected))
        if not (root / "result.json").exists():
            write_report(root / "result.json", {"protocol": plan, "error": "outer_process_timeout",
                "score": {"valid_live_measurement": False, "verified_delivery": False, "eligible_for_default_switch": False}})
        print("Repair probe exceeded its outer deadline; any uncertain usage remains in the original state.", flush=True)
        return 1
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    print(json.dumps(result["score"], ensure_ascii=False), flush=True)
    return 0 if result["score"]["verified_delivery"] and result["score"]["repair_phase_model_calls"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
