"""Isolated paired Ultra evaluation through the production Web worker.

Default: prepare both arms, compare receipts, make zero chat-model requests.
Live runs require --run-live and MINICODE_LIVE_COLLABORATION_EVAL=1. A batch
contains at most two registered tasks / four runs; failures are never retried
by this launcher. Runtime's normal bounded repair policy remains in force.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from minicode.collaboration_evaluation import (
    ARMS, COMPARISON_ARMS, LEGACY_ARMS, PARALLEL_ARM, SCHEMA, arm_capacity_expectation,
    arm_runtime_overrides, assembly_receipt, bounded_limits, compare_assemblies,
    compare_runs, digest, file_digest, normalized, request_output_limit, trace_metrics,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "Package/AgentRuntime/Data/CollaborationTrials"
IGNORED = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
                     ".mypy_cache", ".ruff_cache", ".temp", ".mini-code-memory-v2"})


def select_model(runtime: dict, *, model: str | None = None, provider: str | None = None) -> dict:
    """Override this frozen batch only; never save settings or move credentials."""
    selected = deepcopy(runtime)
    if model is not None:
        if not model.strip() or model != model.strip():
            raise ValueError("select an exact nonblank model id")
        # Explicit provider routing applies only to configuredModel. Updating
        # model alone could route a DeepSeek alias to the direct API instead.
        selected.update(model=model, configuredModel=model)
    if provider is not None:
        selected["provider"] = provider
    return selected


def freeze_model_calls(runtime: dict, workspace: Path) -> dict:
    """Use the real adapter/config paths without sending a model request."""
    from minicode.model_registry import create_model_adapter
    from minicode.openai_adapter import OpenAIModelAdapter, _get_openai_chat_completions_url
    from minicode.paged_memory.config import PagedMemoryConfig
    from minicode.paged_memory.rerank_api import ExternalRerankApi
    from minicode.provider_usage import model_call_identity
    from minicode.tooling import ToolRegistry
    adapter = create_model_adapter(runtime["model"], ToolRegistry([]), runtime)
    if not isinstance(adapter, OpenAIModelAdapter):
        raise ValueError("frozen model-route trials currently require an OpenAI-compatible transport")
    values = adapter.runtime
    policy = {"agent": model_call_identity(values["model"], values["provider"], _get_openai_chat_completions_url(values))}
    memory = PagedMemoryConfig.from_sources(runtime, cwd=str(workspace))
    if memory.rerank_enabled or memory.rerank_shadow:
        reranker = ExternalRerankApi(memory, runtime)
        policy["memory_rerank"] = model_call_identity(reranker.runtime["model"], reranker.runtime["provider"], reranker.url)
    return policy


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _safe_path_text(value: Any) -> str:
    """Render a path for a diagnostic without leaking anything but the path."""
    return "" if value is None else str(value)
def freeze_tree(source: Path, destination: Path) -> dict[str, str]:
    """Copy regular source/input files; reject aliases and credential configs."""
    from minicode.path_safety import is_unsafe_stored_path
    source, destination = source.absolute(), destination.absolute()
    if not source.is_dir() or destination.exists() or destination.is_relative_to(source):
        raise ValueError("snapshot requires an existing source and a new, separate destination")
    files, total = {}, 0
    for directory, names, filenames in os.walk(source, followlinks=False):
        current = Path(directory)
        for name in names:
            if name not in IGNORED and is_unsafe_stored_path(current / name, boundary=source):
                raise ValueError("snapshot contains a filesystem alias")
        names[:] = [name for name in names if name not in IGNORED]
        for name in filenames:
            path = current / name
            relative = path.relative_to(source).as_posix()
            if name == ".env" or name == ".mcp.json" or relative in {".mini-code/settings.json", ".claude/settings.json"}:
                raise ValueError("credential configuration must come from the config loader, not a source snapshot")
            if is_unsafe_stored_path(path, boundary=source, reject_hardlinks=True) or not path.is_file():
                raise ValueError("snapshot contains a non-regular file")
            total += path.stat().st_size
            if total > 256 * 1024 * 1024 or len(files) >= 25_000:
                raise ValueError("snapshot exceeds the initial trial's 256 MiB / 25000-file limit")
            files[relative] = file_digest(path)
    destination.mkdir(parents=True)
    for relative, expected in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
        if file_digest(target) != expected or file_digest(source / relative) != expected:
            raise ValueError("source changed while freezing the snapshot")
    return files


def register_tasks(manifest: Path, batch: Path, *, arms: tuple[str, ...] | None = None) -> list[dict]:
    from minicode.entry_task import task_packet
    from minicode.headless_task import load_headless_task
    order = tuple(arms or ARMS)
    if not order or len(set(order)) != len(order) or any(arm not in COMPARISON_ARMS for arm in order):
        raise ValueError("register only registered comparison arms")
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    tasks = raw.get("tasks")
    if raw.get("schemaVersion") != "minicode-collaboration-tasks-v1" or not isinstance(tasks, list) or not 1 <= len(tasks) <= 2:
        raise ValueError("register one or two provenance-backed tasks")
    registered, ids = [], set()
    for number, item in enumerate(tasks):
        identity = item.get("id", "")
        provenance = item.get("provenance", {})
        if (not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", identity) or identity in ids
                or provenance.get("kind") not in {"historical", "current"}
                or any(not isinstance(provenance.get(key), str) or not provenance[key].strip() for key in ("source", "revision"))):
            raise ValueError("each task requires a unique id and explicit source/revision provenance")
        if item.get("category") not in {"small_fix", "independent_multi_file", "coupled_change", "repair_after_real_check_failure"}:
            raise ValueError("task category is not registered")
        ids.add(identity)
        source = (manifest.parent / item["workspace"]).resolve()
        seed = batch / "seeds" / identity
        initial_files = freeze_tree(source, seed)
        prompt = (manifest.parent / item["prompt_file"]).read_text(encoding="utf-8")
        if not prompt.strip() or len(prompt) > 64_000:
            raise ValueError("task prompt must be nonblank and bounded")
        task = load_headless_task(str((manifest.parent / item["task_file"]).resolve()), seed)
        if any("browser" in check for check in task.fields()["runtimeVerificationCommands"]):
            raise ValueError("The independent evaluation checker supports command declarations only; "
                             "declare an explicit browser test command instead of a native browser scenario")
        checker_files = {}
        for raw_path in item.get("checker_files", []):
            path = (manifest.parent / raw_path).resolve()
            checker_files[str(path)] = file_digest(path)
        if not checker_files:
            raise ValueError("list the immutable checker and its input/support files")
        registered.append({"id": identity, "category": item["category"], "provenance": provenance,
                           "seed": str(seed), "initial_files": initial_files,
                           "prompt": prompt, "task_snapshot": task_packet(task), "checker_files": checker_files,
                           "order": list(order)})
    return registered


def _copy_state_inputs(destination: Path) -> dict:
    from minicode.config import MINI_CODE_DIR
    destination.mkdir(parents=True)
    files = {}
    # Sessions, settings, provider credentials and mutable stores are excluded.
    # This is a fresh-session trial with a frozen copy of file-memory inputs.
    for name in ("USER.md", "MANAGED.md", "permissions.json", "skills", "memory"):
        source = MINI_CODE_DIR / name
        target = destination / name
        if source.is_dir():
            copied = freeze_tree(source, target)
            files.update({name + "/" + key: value for key, value in copied.items()})
        elif source.is_file():
            from minicode.path_safety import is_unsafe_stored_path
            if is_unsafe_stored_path(source, boundary=MINI_CODE_DIR, reject_hardlinks=True):
                raise ValueError("state inputs contain filesystem aliases")
            shutil.copyfile(source, target)
            files[name] = file_digest(target)
            if file_digest(source) != files[name]:
                raise ValueError("state input changed during snapshot")
    return files


def _external_checks(task: dict, workspace: Path) -> dict:
    """Run the same frozen commands independently against the integrated tree."""
    from minicode.entry_task import restore_task
    if any(file_digest(Path(path)) != expected for path, expected in task["checker_files"].items()):
        return {"passed": False, "error": "immutable_checker_changed", "commands": []}
    contract = restore_task(task["task_snapshot"], str(workspace))
    if any("browser" in check for check in contract.fields()["runtimeVerificationCommands"]):
        return {"passed": False, "error": "unsupported_browser_scenario", "commands": []}
    results = []
    for command in contract.fields()["runtimeVerificationCommands"]:
        try:
            result = subprocess.run(command["command"], cwd=workspace, capture_output=True, timeout=command["timeout"],
                                    text=True, encoding="utf-8", errors="replace", check=False)
            results.append({"name": command["name"], "exit_code": result.returncode,
                            "stdout_sha256": digest(result.stdout), "stderr_sha256": digest(result.stderr)})
        except subprocess.TimeoutExpired:
            results.append({"name": command["name"], "exit_code": None, "error": "timeout"})
    intact = all(file_digest(Path(path)) == expected for path, expected in task["checker_files"].items())
    return {"passed": intact and bool(results) and all(item["exit_code"] == 0 for item in results),
            "checker_unchanged": intact, "commands": results}


def worker(payload: dict) -> dict:
    # Environment is selected by the parent before importing modules with
    # import-time state paths. This child never loads the user's session store.
    from minicode.config import MINI_CODE_DIR, ISOLATED_STATE_DIRECTORY
    from minicode.session import create_new_session, load_session, save_session
    from minicode.web_run_probe import WebRunProbe
    from minicode.web_worker import _run
    from minicode.workspace_provider import snapshot_revision, workspace_snapshot
    root = Path(payload["root"]).resolve()
    if not root.is_relative_to(DATA.resolve()) or not ISOLATED_STATE_DIRECTORY or MINI_CODE_DIR != root / "state":
        raise ValueError(
            "worker requires its own evaluation state directory: "
            f"MINI_CODE_STATE_DIR={_safe_path_text(os.environ.get('MINI_CODE_STATE_DIR'))} "
            f"MINI_CODE_DIR={_safe_path_text(MINI_CODE_DIR)} isolated={ISOLATED_STATE_DIRECTORY} "
            f"root={_safe_path_text(root)} under_data={root.is_relative_to(DATA.resolve())}")
    if payload["arm"] not in COMPARISON_ARMS:
        raise ValueError("worker requires a registered comparison arm")
    workspace, runtime = root / "workspace", payload["runtime"]
    before = workspace_snapshot(workspace)
    if any(file_digest(workspace / name) != value for name, value in payload["task"]["initial_files"].items()):
        raise ValueError("initial workspace differs from the frozen source")
    limits = payload["limits"]
    if limits["saved_deadline"] is not None and limits["saved_deadline"] < time.time() + limits["duration_seconds"]:
        raise ValueError("saved deadline cannot accommodate this run without changing its duration")
    runtime["runtimeMaxTokens"] = limits["max_tokens"]
    runtime["runtimeDeadline"] = time.time() + limits["duration_seconds"]
    # The single-response output allowance is set EXPLICITLY for a bounded trial.
    # Leaving it to the adapter's fallback is what produced a request that spent
    # its whole cap on reasoning and returned neither a tool call nor an answer.
    runtime["maxOutputTokens"] = request_output_limit(
        limits["max_tokens"], limits.get("max_output_tokens_per_request"))
    # The arm's own configuration is applied HERE, to the runtime the run really
    # uses. A receipt that reports an arm without its settings is a relabelled
    # run, which `compare_assemblies` refuses.
    runtime.update(arm_runtime_overrides(payload["arm"]))
    run_id = "collaboration-" + uuid.uuid4().hex
    session = create_new_session(str(workspace))
    save_session(session, force_full=True)
    replacements = {str(workspace): "${WORKSPACE}", str(root / "state"): "${STATE}", str(root): "${RUN}",
                    runtime["memoryTenantId"]: "${MEMORY_TENANT}", runtime["memoryWorkspaceId"]: "${MEMORY_WORKSPACE}",
                    run_id: "${RUN_ID}", session.session_id: "${SESSION_ID}"}
    observations = {"approval_requests": 0, "denied_requests": 0}
    receipt = {}

    def approve(request):
        observations["approval_requests"] += 1
        # Use the production PermissionManager. Authorize only edits within
        # this run (including its Runtime-owned temporary candidate). Shell
        # and external side effects require an ordinary interactive review.
        target = Path(str(request.get("scope", "")))
        allowed = request.get("kind") == "edit" and target.is_absolute() and target.resolve().is_relative_to(root)
        if not allowed:
            observations["denied_requests"] += 1
        return {"decision": "allow_once" if allowed else "deny_once"}

    def prepared(**kwargs):
        from minicode.workspace_lock import WorkspaceWriteLease
        # Exercise the actual lock and atomic owner receipt during preparation,
        # including Windows path limits. No model is needed for this boundary.
        with WorkspaceWriteLease(workspace, run_id=run_id):
            pass
        receipt.update(assembly_receipt(**kwargs, replacements=replacements, limits=limits, initial_files=payload["task"]["initial_files"]))
        receipt["state_input_hash"] = payload["state_input_hash"]
        expected = payload.get("expected_assembly")
        if expected is not None:
            # Compare the comparability INPUTS, not assembled state observed at a
            # different point in the two phases; see ASSEMBLY_INPUT_FIELDS.
            from minicode.collaboration_evaluation import (
                ASSEMBLY_INPUT_FIELDS, assembly_input_drift,
            )

            drift = assembly_input_drift(expected, receipt)
            if drift:
                # Record BOTH sides before refusing. A drift report that only says
                # "they differ" leaves the reader to guess which field moved, and the
                # whole point of the comparison is to name it.
                write_json(root / "assembly-actual.json", receipt)
                write_json(root / "assembly-expected.json", expected)
                write_json(root / "assembly-drift.json", {
                    "schema": "minicode-assembly-drift-v1", "equal": False,
                    "compared_fields": list(ASSEMBLY_INPUT_FIELDS),
                    "differences": drift,
                })
                raise ValueError(
                    "actual Web assembly drifted after preflight; no model request "
                    "admitted (fields: " + ", ".join(item["field"] for item in drift) + ")")
        write_json(root / "assembly.json", receipt)

    started = time.monotonic()
    exit_code = _run(str(workspace), session.session_id, payload["task"]["prompt"], "ultra", run_id,
                     task_snapshot=payload["task"]["task_snapshot"],
                     web_runtime_policy={"orchestration": "primary", "ultra": "primary"},
                     _probe=WebRunProbe(runtime_snapshot=runtime, allow_generic_delegation=payload["arm"] != "primary_only",
                                        prepare_only=payload["prepare_only"], assemble_only=payload.get("assemble_only", False),
                                        on_prepared=prepared, approval_prompt=approve))
    saved = load_session(session.session_id)
    trace = saved.runtime_runs[-1] if saved and saved.runtime_runs else None
    result = {"arm": payload["arm"], "run_id": run_id, "prepare_only": payload["prepare_only"], "exit_code": exit_code,
              "wall_seconds": round(time.monotonic() - started, 3), "assembly": receipt,
              "preflight_assembly": deepcopy(payload.get("expected_assembly")),
              "live_capability": deepcopy((trace or {}).get("live_capability")),
              "session_status": saved.metadata.status if saved else None, **observations,
              "metrics": trace_metrics(trace, run_id=run_id, arm=payload["arm"], model=runtime["model"],
                                       model_policy=runtime["_modelCallPolicy"]),
              "workspace_changed": workspace_snapshot(workspace) != before}
    if not payload["prepare_only"]:
        result["acceptance"] = _external_checks(payload["task"], workspace)
        result["integrated_tree_matches"] = result["metrics"]["integration_revision"] == snapshot_revision(workspace_snapshot(workspace))
        result["verified_delivery"] = (exit_code == 0 and result["metrics"]["completed"] and
                                       not result["metrics"]["protocol_violations"] and result["acceptance"]["passed"]
                                       and result["integrated_tree_matches"])
    return result


def launch(payload: dict, *, state_seed: Path, config_workspace: Path, config_state: Path, environment: dict) -> dict:
    from minicode.paged_memory.config import PagedMemoryConfig
    root = Path(payload["root"])
    root.mkdir(parents=True)
    freeze_tree(Path(payload["task"]["seed"]), root / "workspace")
    freeze_tree(state_seed, root / "state")
    (root / "temporary").mkdir()
    runtime = normalized(deepcopy(payload["runtime"]), {str(config_workspace): str(root / "workspace"),
                                                       str(config_state): str(root / "state")})
    memory = PagedMemoryConfig.from_sources(payload["runtime"], cwd=str(config_workspace))
    namespace = "eval-" + uuid.uuid4().hex
    runtime.update(memoryTenantId=namespace, memoryWorkspaceId=namespace + "-workspace")
    paths = {"memoryDataRoot": ("MINI_CODE_DATA_ROOT", root / "state"),
             "memoryExportRoot": ("MEMORY_EXPORT_ROOT", root / "state/exports"),
             "memorySqlitePath": ("MEMORY_SQLITE_PATH", root / "state/memory-v2/state.db"),
             "artifactStoreRoot": ("ARTIFACT_STORE_ROOT", root / "state/artifacts"),
             "memoryLocalSpoolRoot": ("MEMORY_LOCAL_SPOOL_ROOT", root / "state/spool"),
             "memoryBackupRoot": ("MEMORY_BACKUP_ROOT", root / "state/backups")}
    env = dict(environment)
    for key, (env_key, path) in paths.items():
        runtime[key] = str(path)
        env[env_key] = str(path)
    env.update(MINI_CODE_STATE_DIR=str(root / "state"), MEMORY_TENANT_ID=namespace,
               MEMORY_WORKSPACE_ID=namespace + "-workspace", MEMORY_DATABASE_URL=memory.database_url,
               MEMORY_BACKEND=memory.backend, TEMP=str(root / "temporary"), TMP=str(root / "temporary"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    # Preserve the production history backend, with a new per-state store id.
    env["MINI_CODE_HISTORY_BACKEND"] = str(payload["runtime"].get("sessionHistoryBackend") or
                                           environment.get("MINI_CODE_HISTORY_BACKEND") or "auto")
    payload = {**payload, "runtime": runtime}
    started = time.monotonic()
    try:
        process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "minicode.runtime_collaboration_eval", "--worker"],
                                 cwd=ROOT, input=json.dumps(payload), env=env, text=True, encoding="utf-8", errors="replace",
                                 capture_output=True, timeout=60 if payload["prepare_only"] else payload["limits"]["duration_seconds"] + 45,
                                 check=False)
        marker = "COLLABORATION_RESULT "
        if marker in process.stdout:
            result = json.loads(process.stdout.rsplit(marker, 1)[1])
        else:
            result = {"arm": payload["arm"], "exit_code": process.returncode, "error": "worker_failed_before_receipt"}
        from minicode.runtime_live_eval import sanitize
        write_json(root / "process.json", sanitize({"exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr}, runtime))
    except subprocess.TimeoutExpired:
        result = {"arm": payload["arm"], "error": "worker_timeout", "verified_delivery": False}
    result["process_wall_seconds"] = round(time.monotonic() - started, 3)
    write_json(root / "result.json", result)
    return result


def evaluate(args) -> dict:
    from minicode.config import MINI_CODE_DIR, load_runtime_config
    from minicode.extensions import discover_extensions
    config_workspace = Path(args.config_workspace).resolve()
    runtime = load_runtime_config(config_workspace, runtime_orchestration="primary")
    runtime = select_model(runtime, model=args.model, provider=args.provider)
    if (runtime.get("model") == "mock" or os.environ.get("MINI_CODE_MODEL_MODE") == "mock"
            or os.environ.get("MINI_CODE_WEB_FORCE_MOCK", "").lower() in {"1", "true", "yes", "on"}):
        raise ValueError("a collaboration trial cannot use demo/model substitution")
    if args.run_live and os.environ.get("MINICODE_LIVE_COLLABORATION_EVAL") != "1":
        raise ValueError("live execution also requires MINICODE_LIVE_COLLABORATION_EVAL=1")
    runtime["_modelCallPolicy"] = freeze_model_calls(runtime, config_workspace)
    limits = bounded_limits(runtime, args.max_tokens, args.duration_seconds,
                            getattr(args, "max_output_tokens_per_request", None))
    batch = DATA / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    batch.mkdir(parents=True)
    # External integrations need their own side-effect isolation contract;
    # silently deleting their tools would invalidate product coverage.
    if runtime.get("mcpServers") or discover_extensions(config_workspace) or (Path.home() / ".claude/skills").is_dir():
        raise ValueError("external MCP/extensions/compatibility skills require an explicit isolation snapshot; none were disabled")
    selected_arms = tuple(args.arm) if args.arm else tuple(ARMS)
    if len(selected_arms) < 2:
        raise ValueError("a comparison needs at least two arms; use --arm twice or omit it")
    tasks = register_tasks(Path(args.manifest).resolve(), batch, arms=selected_arms)
    state_seed = batch / "seeds/state"
    state_inputs = _copy_state_inputs(state_seed)
    state_hash = digest(state_inputs)
    source_hashes = {str(path.relative_to(ROOT)): file_digest(path) for path in (ROOT / "minicode").rglob("*.py")}
    source_hashes.update({str(path.relative_to(ROOT)): file_digest(path) for path in (ROOT / "Package/AgentRuntime/Src").rglob("*.py")})
    plan = {"schema": SCHEMA, "batch": str(batch), "status": "preparing", "model": runtime.get("model"),
            "arms": list(selected_arms), "arm_capacities": {arm: arm_capacity_expectation(arm) for arm in selected_arms},
            "model_call_policy": runtime["_modelCallPolicy"],
            "model_selection_scope": "this batch only; saved Web settings unchanged",
            "provider": runtime.get("provider"), "limits": limits,
            "max_runs": len(tasks) * (len(selected_arms) - 1) * 2,
            "max_batch_tokens": limits["max_tokens"] * len(tasks) * (len(selected_arms) - 1) * 2,
            "automatic_expansion": False,
            "state_input_hash": state_hash, "runtime_source_hash": digest(source_hashes),
            "tasks": tasks, "coverage": {"entry": "production web_worker._run",
                "tools": "production default factory; A removes only general delegation",
                "history": "new session", "paged_memory": "fresh isolated tenant/workspace, cold start",
                "file_memory": "frozen user inputs plus registered workspace snapshot",
                "approval_policy": "existing saved policy plus in-run edit approval; other prompts denied equally",
                "ignored_snapshot_directories": sorted(IGNORED),
                "claims": "assembly and feasibility; no statistical quality or full-workload claim"},
            "preflight": {}, "runs": {}}
    write_json(batch / "plan.json", plan)
    environment = dict(os.environ)
    for index, task in enumerate(tasks):
        results = {}
        for arm in task["order"]:
            results[arm] = launch({"root": str(batch / f"p{index}{selected_arms.index(arm)}"), "arm": arm,
                                   "runtime": runtime, "limits": limits, "task": task, "prepare_only": True,
                                   "assemble_only": True,
                                   "state_input_hash": state_hash}, state_seed=state_seed, config_workspace=config_workspace,
                                  config_state=MINI_CODE_DIR, environment=environment)
        # Arm 0 is the baseline; every other arm is compared against it on its own,
        # so an added arm never changes how the historical pair is interpreted.
        baseline_arm, comparisons = selected_arms[0], {}
        for arm in selected_arms[1:]:
            pair = compare_assemblies(results[baseline_arm].get("assembly", {}),
                                      results[arm].get("assembly", {}), right_arm=arm, left_arm=baseline_arm)
            if results[baseline_arm].get("exit_code") != 3 or results[arm].get("exit_code") != 3:
                pair = {"valid": False, "reasons": [*pair["reasons"], "preparation_failed"],
                        "right_arm": arm}
            comparisons[arm] = pair
        comparison = {"valid": all(item["valid"] for item in comparisons.values()),
                      "arm_comparisons": comparisons, "baseline_arm": baseline_arm}
        plan["preflight"][task["id"]] = {"comparison": comparison, "arms": results}
        write_json(batch / "plan.json", plan)
    ready = all(item["comparison"]["valid"] for item in plan["preflight"].values())
    plan["status"] = "ready" if ready else "invalid_preflight"
    write_json(batch / "plan.json", plan)
    if args.run_live and ready:
        plan["status"] = "running"
        write_json(batch / "plan.json", plan)
        for index, task in enumerate(tasks):
            # Verify the actual historical defect before spending model tokens.
            baseline = _external_checks(task, Path(task["seed"]))
            plan["runs"][task["id"]] = {"baseline": baseline, "arms": {}}
            if baseline.get("passed") or not baseline.get("checker_unchanged"):
                plan["status"] = "invalid_baseline"
                break
            for arm in task["order"]:
                if any(file_digest(ROOT / name) != sha for name, sha in source_hashes.items()):
                    plan["status"] = "runtime_source_changed"
                    break
                result = launch({"root": str(batch / f"r{index}{selected_arms.index(arm)}"), "arm": arm,
                                 "runtime": runtime, "limits": limits, "task": task, "prepare_only": False,
                                 "state_input_hash": state_hash,
                                 "expected_assembly": plan["preflight"][task["id"]]["arms"][arm]["assembly"]},
                                state_seed=state_seed, config_workspace=config_workspace, config_state=MINI_CODE_DIR, environment=environment)
                plan["runs"][task["id"]]["arms"][arm] = result
                write_json(batch / "plan.json", plan)
            # Each autonomous arm is paired with the baseline on its own, using
            # the arm it actually was, so an added arm never reinterprets the
            # historical pair.
            baseline_arm = selected_arms[0]
            run_comparisons = {}
            for arm in selected_arms[1:]:
                run_comparisons[arm] = compare_runs(
                    plan["runs"][task["id"]]["arms"].get(baseline_arm, {}),
                    plan["runs"][task["id"]]["arms"].get(arm, {}), right_arm=arm, left_arm=baseline_arm)
            plan["runs"][task["id"]]["comparison"] = {
                "valid": bool(run_comparisons) and all(
                    item["valid"] for item in run_comparisons.values()),
                "baseline_arm": baseline_arm, "arm_comparisons": run_comparisons}
            if plan["status"] != "running":
                break
        if plan["status"] == "running":
            plan["status"] = "finished"
    write_json(batch / "plan.json", plan)
    return {"status": plan["status"], "report": str(batch / "plan.json"), "preflight_valid": ready,
            "executed_runs": sum(len(item["arms"]) for item in plan["runs"].values()),
            "valid_pairs": sum(item.get("comparison", {}).get("valid") is True for item in plan["runs"].values()),
            "verified_deliveries": sum(result.get("verified_delivery") is True for item in plan["runs"].values() for result in item["arms"].values())}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    parser.add_argument("--config-workspace", default=str(ROOT))
    parser.add_argument("--model", help="exact main/helper model id for this batch only")
    parser.add_argument("--provider", choices=["openai", "custom", "bailian", "deepseek", "openrouter"],
                        help="existing authenticated channel for this batch; does not copy credentials")
    parser.add_argument("--max-tokens", type=int, default=256_000,
                        help="cumulative token ceiling across all model calls (default: 256000)")
    parser.add_argument("--max-output-tokens-per-request", type=int,
                        help="explicit single-response cap; omit to derive it from the run ceiling")
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--arm", action="append", choices=list(COMPARISON_ARMS),
                        help="comparison arm, repeatable; the first is the baseline")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        print("COLLABORATION_RESULT " + json.dumps(worker(json.load(sys.stdin)), ensure_ascii=False))
        return
    if not args.manifest:
        parser.error("--manifest is required; no synthetic tasks are selected automatically")
    result = evaluate(args)
    print(json.dumps(result, ensure_ascii=False))
    if (not result["preflight_valid"] or (args.run_live and
            (result["status"] != "finished" or result["valid_pairs"] * 2 != result["executed_runs"]))):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
