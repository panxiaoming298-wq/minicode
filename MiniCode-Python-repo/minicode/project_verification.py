"""Bounded project checks available to a primary coordinator without a shell tool.

The recipe is owned by Runtime, uses the current workspace by default, observes the ordinary
command permission policy, and makes no claim to cover arbitrary task criteria.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from collections import OrderedDict
from contextlib import ExitStack
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import weakref

from minicode.permissions import PermissionManager, _normalize_path
from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_artifacts import RuntimeArtifactStore, ArtifactIntegrityError
from minicode.runtime_cancellation import ToolApprovalCanceled
from minicode.runtime_protocol import ArtifactRef, EvidenceRef, VerificationRecord
from minicode.tooling import network_access_allowed
from minicode.tools.run_command import _build_execution_command, _process_arguments
from minicode.verifier_registry import CommandVerifier, VerificationContext, _run_verification_command
from minicode.workspace_provider import TemporaryWorkspaceProvider, snapshot_revision, workspace_snapshot
from minicode.verification_policy import verification_environment, verification_isolated
from minicode.verification_scope import ProjectCheckScope, observed_changes, select_project_scope


def coordinator_checks_enabled(runtime) -> bool:
    return runtime.get("runtimeOrchestration") == "primary" and runtime.get("runtimePrimaryRole") == "coordinator"


def project_check_required(runtime, workspace: Path | None, contract=None, *, changed_paths=None) -> bool:
    # Explicit task checks take precedence. An invalid/missing build script in
    # an npm project must fail diagnostics, not silently remove its verifier.
    eligible = (coordinator_checks_enabled(runtime) and not runtime.get("runtimeVerificationCommands")
                and workspace is not None and (Path(workspace) / "package.json").is_file())
    return bool(eligible and (contract is None or
        select_project_scope(workspace, contract, changed_paths=changed_paths).build))


def has_project_recipe(workspace: Path | None) -> bool:
    if workspace is None:
        return False
    try:
        package = json.loads((Path(workspace) / "package.json").read_text(encoding="utf-8-sig"))
        return isinstance(package.get("scripts", {}).get("build"), str) and bool(package["scripts"]["build"].strip())
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def _protected(snapshot):
    # These are outputs/dependencies of this specific static frontend recipe.
    return {name: digest for name, digest in snapshot.items()
            if name.split("/", 1)[0] not in {"node_modules", "dist"} and not name.endswith(".tsbuildinfo")}


# Only in-process native receipts can enter this cache. Files/model-supplied
# reports cannot seed it, and a different run/store cannot inherit a verdict.
_receipt_caches = weakref.WeakKeyDictionary()
_receipt_cache_lock = threading.Lock()


def _receipt_slot(store, key):
    with _receipt_cache_lock:
        entries = _receipt_caches.setdefault(store, OrderedDict())
        if key not in entries:
            entries[key] = {"lock": threading.Lock(), "record": None}
        entries.move_to_end(key)
        while len(entries) > 32:
            oldest = next(iter(entries))
            if entries[oldest]["lock"].locked():
                break
            entries.pop(oldest)
        return entries[key]


def _recipe_key(context, revision, network_allowed, isolate, check_scope=None):
    tools = []
    for name in ("node", "npm.cmd" if sys.platform == "win32" else "npm", sys.executable):
        path = shutil.which(name)
        stat = Path(path).stat() if path else None
        tools.append((path, stat.st_size if stat else None, stat.st_mtime_ns if stat else None))
    recipe = hashlib.sha256(b"".join(Path(__file__).with_name(name).read_bytes()
        for name in ("project_verification.py", "project_browser.py", "browser_capture.py", "verification_scope.py"))).hexdigest()
    # Hash environment values; no credentials or environment contents are stored.
    return hashlib.sha256(json.dumps([context.contract.to_dict(), revision, network_allowed, isolate,
        recipe, check_scope, tools, sorted(os.environ.items())], sort_keys=True, default=str).encode()).hexdigest()


def _build_revision(workspace):
    # Direct checks retain dist. A changed/deleted output must invalidate native
    # receipt reuse even though generated output is outside the source revision.
    output = Path(workspace) / "dist"
    if not output.is_dir() or is_unsafe_stored_path(output, boundary=Path(workspace)):
        return ""
    return snapshot_revision(workspace_snapshot(output, reject_aliases=True))


@dataclass(slots=True)
class ProjectVerifier:
    name: str = "npm-build-and-browser"
    kind: str = "browser_render"
    deterministic: bool = True
    timeout: float = 600.0
    temporary_root: Path | None = None
    network_allowed: bool = True
    artifact_store: RuntimeArtifactStore | None = None
    isolate: bool = False
    check_scope: ProjectCheckScope | None = None

    def verify(self, context: VerificationContext) -> VerificationRecord:
        store = self.artifact_store or context.artifact_store
        if store is None or not isinstance(context.permissions, PermissionManager):
            return self._execute(context)
        try:
            context.check_active()
            revision = snapshot_revision(workspace_snapshot(context.workspace))
            slot = _receipt_slot(store, _recipe_key(context, revision, self.network_allowed, self.isolate,
                                                  self.check_scope.to_dict() if self.check_scope else None))
            while not slot["lock"].acquire(timeout=.1):
                context.check_active()
            try:
                context.check_active()
                record = slot["record"]
                if record is not None:
                    payload = json.loads(record.details)
                    refs = [ArtifactRef(**item["ref"]) for item in payload["artifacts"]]
                    try:
                        intact = bool(refs) and all(store.owned_reference(ref.artifact_id,
                            run_id=context.contract.envelope.run_id, node_id=self.name) == ref for ref in refs)
                    except (ArtifactIntegrityError, OSError, ValueError):
                        intact = False
                    if (intact and (self.isolate or payload.get("build_revision") == _build_revision(context.workspace))
                        and time.time() - payload["checked_at"] < 3600
                        and snapshot_revision(workspace_snapshot(context.workspace)) == revision):
                        context.check_active()
                        payload["execution"] = "reused"
                        payload["reuse_reason"] = "Same run, source revision and recipe; native artifacts verified. No commands rerun."
                        serialized = json.dumps(payload, ensure_ascii=False)
                        return replace(record, scope="final" if context.final else "node", details=serialized,
                            evidence_refs=tuple(replace(ref, content_hash=hashlib.sha256(serialized.encode()).hexdigest())
                                                for ref in record.evidence_refs))
                record = self._execute(context)
                # Failed, canceled, partial or mutated candidates are never cached.
                if record.passed and json.loads(record.details)["candidate_revision"] == revision:
                    slot["record"] = record
                return record
            finally:
                slot["lock"].release()
        except ToolApprovalCanceled:
            raise
        except Exception as error:
            return VerificationRecord(verifier=self.name, passed=False, scope="final" if context.final else "node",
                details=json.dumps({"recipe": self.name, "passed": False, "execution": "not-started",
                    "failure": f"Project receipt validation failed: {type(error).__name__}: {error}",
                    "stages": [], "artifacts": [], "interaction_coverage": False}), deterministic=True)

    def _execute(self, context: VerificationContext) -> VerificationRecord:
        execution_deadline = time.monotonic() + self.timeout

        def check_active():
            context.check_active()
            if time.monotonic() >= execution_deadline:
                raise RuntimeError("Project verification execution timeout exhausted")
        provider = TemporaryWorkspaceProvider(self.temporary_root) if self.isolate else None
        handle = None
        revision = ""
        stages = []
        artifacts = []
        failure = ""
        browser_report = None
        build_revision = ""
        output_directory = None
        cleanup_warnings = []
        check_scope = self.check_scope or ProjectCheckScope(True, True, "all", "Explicit native build/browser check")
        try:
            check_active()
            if not isinstance(context.permissions, PermissionManager):
                raise RuntimeError("Project checks need the active command permission manager")
            if not has_project_recipe(context.workspace):
                raise RuntimeError("No npm build script found; configure task checks for this project type")
            workspace = context.workspace
            if provider is not None:
                handle = provider.create(context.workspace, owner_token="project-verification",
                                         cancellation_event=context.cancellation_event)
                workspace = handle.workspace
                baseline = handle.base_hashes
            else:
                baseline = workspace_snapshot(workspace, reject_aliases=True)
            revision = snapshot_revision(baseline)
            package = json.loads((workspace / "package.json").read_text(encoding="utf-8-sig"))
            manager = str(package.get("packageManager", "npm"))
            if not (manager == "npm" or manager.startswith("npm@")) or any(
                (workspace / name).exists() for name in ("pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
            ):
                raise RuntimeError("Automatic checks currently support npm only; configure commands for this package manager")
            scoped = context.permissions.fork_for_operation(hashlib.sha256(
                f"{context.contract.envelope.run_id}:{self.name}:{revision}".encode()).hexdigest())
            scoped.workspace_root = _normalize_path(str(workspace))
            before = _protected(baseline)
            environment, output_directory = verification_environment(workspace)

            def command(stage, argv):
                nonlocal execution_deadline
                check_active()
                executable, args = _build_execution_command(argv[0], argv[0], argv[1:],
                                                            use_shell=False, background_shell=False)
                approval_started = time.monotonic()
                try:
                    scoped.ensure_command(executable, args, str(workspace),
                        force_prompt_reason=f"Project {stage} check in {'isolated' if self.isolate else 'current'} workspace, source revision {revision}")
                finally:
                    execution_deadline += time.monotonic() - approval_started
                # An approval may have waited while cancellation or a source change occurred.
                check_active()
                if snapshot_revision(workspace_snapshot(context.workspace)) != revision:
                    raise RuntimeError("Candidate changed while approving project checks")
                with ExitStack() as transient:
                    execution_environment = environment
                    if stage == "browser" and os.name == "nt":
                        # Chromium's profile path does not support Windows'
                        # extended namespace. Keep that disposable profile in
                        # a short owned directory; reports still use output_directory.
                        browser_temp = transient.enter_context(tempfile.TemporaryDirectory(
                            prefix="minicode-browser-", dir=tempfile.gettempdir(), ignore_cleanup_errors=True))
                        execution_environment = {**environment, "TEMP": browser_temp, "TMP": browser_temp,
                                                 "TMPDIR": browser_temp}
                    completed = _run_verification_command(_process_arguments(executable, args),
                        cwd=str(workspace), env=execution_environment, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                        errors="replace", timeout=context.remaining_timeout(max(0.001, execution_deadline - time.monotonic())),
                        cancellation_event=context.cancellation_event)
                output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
                stages.append({"stage": stage, "command": argv, "exit_code": completed.returncode,
                               "output": output[-12000:]})
                check_active()
                if completed.returncode:
                    raise RuntimeError(f"{stage} failed (exit {completed.returncode}); repair the reported output")

            npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
            if not npm:
                raise RuntimeError("npm is unavailable; install Node.js and restart MiniCode")
            dependencies = bool(package.get("dependencies") or package.get("devDependencies"))
            if dependencies:
                if not self.network_allowed:
                    raise RuntimeError("Network access is disabled; dependency installation was not run")
                install = "ci" if (workspace / "package-lock.json").is_file() else "install"
                command("dependencies", [npm, install, "--ignore-scripts", "--no-audit", "--no-fund", "--package-lock=false"])
            command("build", [npm, "--ignore-scripts", "run", "build"])
            if _protected(workspace_snapshot(workspace, reject_aliases=True)) != before:
                raise RuntimeError("Build/install changed source files; their output cannot verify the original candidate")
            build_revision = _build_revision(workspace)
            if check_scope.browser:
                report_path = output_directory / "browser"
                try:
                    command("browser", [sys.executable, "-I", str(Path(__file__).with_name("project_browser.py")),
                        "--root", str(workspace / "dist"), "--output", str(report_path),
                        "--source-revision", revision, "--source-binding", "built-candidate",
                        "--screenshots", check_scope.screenshots, "--trace", "failure"])
                finally:
                    store = self.artifact_store or context.artifact_store or RuntimeArtifactStore()
                    report_file = report_path / "report.json"
                    if report_file.is_file():
                        browser_report = json.loads(report_file.read_bytes())
                    names = ["report.json", "desktop.png", "mobile.png", "desktop-trace.zip", "mobile-trace.zip"]
                    names.extend(item["name"] for item in (browser_report or {}).get("images", [])
                        if isinstance(item, dict) and re.fullmatch(r"(desktop|mobile)(?:-[A-Za-z0-9_-]{1,64})?\.png", str(item.get("name", ""))))
                    for filename in dict.fromkeys(names):
                        path = report_path / filename
                        if path.is_file() and not path.is_symlink():
                            data = path.read_bytes()
                            ref = store.put_bytes(data, run_id=context.contract.envelope.run_id, node_id=self.name,
                                media_type="image/png" if path.suffix == ".png" else
                                           "application/zip" if path.suffix == ".zip" else "application/json")
                            artifacts.append({"name": filename, "ref": asdict(ref)})
                            if filename == "report.json":
                                browser_report = json.loads(data)
                if not browser_report or browser_report.get("passed") is not True:
                    raise RuntimeError("Browser check did not produce a passing native report")
            else:
                store = self.artifact_store or context.artifact_store or RuntimeArtifactStore()
                ref = store.put_text(json.dumps({"passed": True, "candidate_revision": revision,
                    "scope": "build-only", "check_scope": check_scope.to_dict(), "stages": stages}),
                    run_id=context.contract.envelope.run_id, node_id=self.name, media_type="application/json")
                artifacts.append({"name": "report.json", "ref": asdict(ref)})
            if _protected(workspace_snapshot(workspace, reject_aliases=True)) != before:
                raise RuntimeError("Verification workspace source changed during browser checks")
            if check_scope.browser and (not build_revision or _build_revision(workspace) != build_revision):
                raise RuntimeError("Build output changed during browser checks; rerun for the new output")
            if snapshot_revision(workspace_snapshot(context.workspace)) != revision:
                raise RuntimeError("Candidate changed during project checks; rerun for the new version")
            check_active()
        except ToolApprovalCanceled:
            raise
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
        finally:
            if handle is not None:
                try:
                    provider.cleanup(handle)
                except Exception as error:
                    cleanup_warnings.append(f"Verification cleanup deferred at {handle.workspace}: {type(error).__name__}: {error}")
        payload = {"recipe": self.name, "candidate_revision": revision, "passed": not failure,
                   "checked_at": time.time(), "execution": "executed",
                   "workspace_mode": "isolated" if self.isolate else "direct",
                   "build_revision": build_revision, "output_directory": str(output_directory) if output_directory else None,
                   "failure": failure, "stages": stages, "artifacts": artifacts, "cleanup_warnings": cleanup_warnings,
                   "browser": browser_report, "scope": "build-and-browser-smoke" if check_scope.browser else "build-only",
                   "check_scope": check_scope.to_dict(), "interaction_coverage": False}
        serialized = json.dumps(payload, ensure_ascii=False)
        evidence = ()
        if revision and stages:
            evidence = (EvidenceRef(evidence_type="command-exit", resource="runtime:npm-build-and-browser",
                content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
                claim="npm build" + (" and browser smoke checks" if check_scope.browser else "") + "; no claim of full interaction coverage",
                producer_node_id=self.name, base_revision=revision),)
        return VerificationRecord(verifier=self.name, passed=not failure,
            scope="final" if context.final else "node", details=serialized,
            deterministic=True, evidence_refs=evidence, covered_criteria_ids=())


def register_project_check(registry, runtime, workspace, contract=None, *, changed_paths=None):
    scope = select_project_scope(workspace, contract, changed_paths=changed_paths) if workspace is not None and contract is not None else None
    if project_check_required(runtime, workspace, contract, changed_paths=changed_paths):
        registry.register(ProjectVerifier(network_allowed=network_access_allowed(runtime),
            temporary_root=Path(runtime["runtimeTemporaryRoot"]) if runtime.get("runtimeTemporaryRoot") else None,
            isolate=verification_isolated(runtime),
            check_scope=scope))
    if workspace is not None and not runtime.get("runtimeVerificationCommands"):
        if scope is not None and not scope.unit:
            return
        root = Path(workspace)
        # A Python test suite is an executable project check even when the
        # primary is working directly, rather than through the npm coordinator.
        # Discovery does not import project code or claim criterion coverage.
        test_roots = (root, root / "tests")
        has_tests = any(path.is_file() for directory in test_roots
                        for pattern in ("test_*.py", "*_test.py") for path in directory.glob(pattern))
        if has_tests:
            interpreter = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            registry.register(CommandVerifier(name="python-pytest", kind="unit_test",
                command=(str(interpreter) if interpreter.is_file() else sys.executable, "-B", "-m", "pytest", "-q"),
                temporary_root=Path(runtime["runtimeTemporaryRoot"]) if runtime.get("runtimeTemporaryRoot") else None,
                require_command_permissions=True, isolate=verification_isolated(runtime)))
