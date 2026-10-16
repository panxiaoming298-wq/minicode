"""Execute a parent's declared check using the configured workspace policy."""
import json
import time
import threading

from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_cancellation import ActivationCancellation
from minicode.runtime_protocol import operation_digest
from minicode.tools.run_command import _build_execution_command, _run_with_bounded_pipes
from minicode.tools._command_preflight import check_command
from minicode.tools._validation import InputContractError
from minicode.workspace_provider import snapshot_revision, workspace_snapshot
from minicode.verification_policy import verification_environment, verification_isolated


def run_check(gateway, context, *, check_id, operation_id):
    reducer, worker = gateway.scheduler.reducer, gateway.worker
    claimed = reducer.claim_agent_check(node_id=context.node_id, attempt_id=context.attempt_id,
        check_id=check_id, operation_id=operation_id)
    if claimed["reused"]:
        return claimed["record"]
    record = claimed["record"]
    check, handle = record["check"], None
    started = time.monotonic()
    # Capturing and executing consume one bounded operation; no file lock is
    # held while capture or the command runs. Concurrent source changes refuse capture.
    deadline = started + check["timeout_seconds"] + 60
    cancellation = ActivationCancellation(context.cancellation_event or threading.Event(),
        lambda: time.monotonic() >= deadline or not reducer.agent_check_activation_live(context.node_id, context.attempt_id))
    report = {"check_id": check_id, "operation_id": operation_id, "task_id": context.node_id,
        "command": check["command"], "args": check["args"], "cwd": check["cwd"],
        "passed": False, "exit_code": None, "counts_as_acceptance": False,
        "workspace_mode": "direct", "input_revision": None}
    try:
        source = worker.workspace_for_node(context.node_id)
        workspace = source
        # Shared-workspace checks run directly unless trusted configuration
        # explicitly requests isolation, just like the primary's final checks.
        if verification_isolated(worker.runtime):
            handle = worker.temporary_provider.create_verified(source,
                owner_token=f"agent-check:{context.node_id}:{operation_id}", cancellation_event=cancellation)
            workspace, baseline = handle.workspace, handle.base_hashes
            report["workspace_mode"] = "verified_copy"
        else:
            baseline = workspace_snapshot(source, reject_aliases=True, cancellation_event=cancellation)
        report["input_revision"] = snapshot_revision(baseline)
        cwd = workspace / check["cwd"]
        if (is_unsafe_stored_path(cwd, boundary=workspace) or not cwd.is_dir()
            or not cwd.resolve().is_relative_to(workspace.resolve())):
            raise ValueError("local check cwd is outside its workspace")
        executable, arguments = _build_execution_command(check["command"], check["command"],
            check["args"], use_shell=False, background_shell=False, cwd=str(cwd))
        permissions = worker._permissions_for(workspace)
        if permissions is not None:
            if callable(getattr(permissions, "fork_for_operation", None)):
                permissions = permissions.fork_for_operation(operation_digest(
                    run_id=reducer.run_id, node_id=context.node_id, attempt_id=context.attempt_id,
                    tool_call_id=context.tool_call_id or operation_id, cwd=str(cwd), arguments=check,
                    environment_scope={"tool": "run_agent_check", "operation_id": operation_id},
                    base_revision=report["input_revision"]))
            permissions.ensure_command(executable, arguments, str(cwd))
        if cancellation.is_set():
            raise ValueError("local check canceled before process start")
        if workspace_snapshot(source, reject_aliases=True, cancellation_event=cancellation) != baseline:
            raise ValueError("local check source changed while approving execution")
        check_command(executable, arguments, cwd=str(cwd))
        environment, output_directory = verification_environment(workspace)
        report["output_directory"] = str(output_directory)
        result = _run_with_bounded_pipes(executable, arguments, cwd=str(cwd),
            timeout=check["timeout_seconds"], cancellation_event=cancellation, env=environment)
        report.update(passed=result.ok and not cancellation.is_set(), exit_code=result.commandExitCode,
                      output=result.output)
        report["source_still_matches"] = workspace_snapshot(source, reject_aliases=True,
            cancellation_event=cancellation) == baseline
        if not report["source_still_matches"]:
            report.update(passed=False, output=report["output"] + "\nWorkspace source changed during the local check; repair and rerun.")
    except (OSError, ValueError, RuntimeError, TypeError) as error:
        report.update(passed=False, output=f"{type(error).__name__}: {error}\n{report.get('output', '')}")
        if isinstance(error, InputContractError):
            report.update(command_executed=False, launch_issues=error.issues)
    finally:
        if handle is not None:
            try:
                worker.temporary_provider.cleanup(handle)
            except (OSError, ValueError, RuntimeError) as error:
                report["cleanup_error"] = str(error)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    artifact = worker.artifact_store.put_text(json.dumps(report, ensure_ascii=False),
        run_id=reducer.run_id, node_id=context.node_id, media_type="application/json")
    return reducer.finish_agent_check(node_id=context.node_id, operation_id=operation_id,
        report={**report, "output": report.get("output", "")[:6000], "report_artifact": artifact.to_dict()})
