"""Trusted task declarations executed by the owned Playwright runner."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import EvidenceRef, VerificationRecord
from minicode.runtime_cancellation import ToolApprovalCanceled
from minicode.verification_policy import verification_environment
from minicode.verification_workspace import VerificationWorkspaceSession
from minicode.verifier_registry import _run_verification_command
from minicode.workspace_provider import snapshot_revision, workspace_snapshot

ARTIFACT_OWNER = "npm-build-and-browser"


def _static_manifest(root, context, *, copy_to=None):
    """Observe every served file, including npm outputs omitted from source snapshots."""
    root = Path(root).absolute()
    if not root.is_dir() or is_unsafe_stored_path(root, boundary=Path(root.anchor)):
        raise ValueError("Browser static root is missing or aliased")
    manifest, total = {}, 0
    if copy_to is not None:
        copy_to = Path(copy_to)
        if copy_to.exists():
            raise ValueError("Static build copy requires an unused destination")
        copy_to.mkdir(parents=True)
    def failed(error):
        raise error
    for directory, children, files in os.walk(root, followlinks=False, onerror=failed):
        context.check_active()
        current = Path(directory)
        for name in (*children, *files):
            path = current / name
            if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
                raise ValueError("Browser static root contains filesystem aliases")
        if copy_to is not None:
            (copy_to / current.relative_to(root)).mkdir(parents=True, exist_ok=True)
        for name in files:
            path = current / name
            if not path.is_file():
                raise ValueError("Browser static root contains a non-regular file")
            relative = path.relative_to(root)
            digest = hashlib.sha256()
            writer = (copy_to / relative).open("xb") if copy_to is not None else None
            try:
                with path.open("rb") as reader:
                    while chunk := reader.read(1024 * 1024):
                        context.check_active()
                        total += len(chunk)
                        if total > 512 * 1024 * 1024:
                            raise ValueError("Browser static root exceeds 512 MiB")
                        digest.update(chunk)
                        if writer is not None:
                            writer.write(chunk)
                manifest[relative.as_posix()] = digest.hexdigest()
                if len(manifest) > 10000:
                    raise ValueError("Browser static root exceeds 10000 files")
            finally:
                if writer is not None:
                    writer.close()
    if "index.html" not in manifest:
        raise ValueError("Browser static root needs index.html")
    return manifest


def browser_declaration(value):
    fields = {"scenario", "url", "root", "views", "screenshots", "readySelector", "allowedOrigins", "allowedWebSockets"}
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError("browser check contains unsupported fields")
    result = dict(value)
    for name in ("scenario", "root"):
        if name not in result:
            continue
        item = result[name]
        if (not isinstance(item, str) or not item or len(item) > 240 or "\\" in item or ":" in item
                or PurePosixPath(item).is_absolute() or PureWindowsPath(item).drive
                or any(part in {"", ".", ".."} for part in item.split("/"))):
            raise ValueError(f"browser {name} must be a relative workspace path")
    if "scenario" not in result or ("url" in result) == ("root" in result):
        raise ValueError("browser check requires scenario and exactly one of url/root")
    if "url" in result:
        targets = [result["url"]]
    else:
        targets = []
    for key, schemes in (("allowedOrigins", {"http", "https"}), ("allowedWebSockets", {"ws", "wss"})):
        entries = result.get(key, [])
        if not isinstance(entries, list) or len(entries) > 8:
            raise ValueError(f"{key} must contain at most eight origins")
        for address in entries:
            _address(address, schemes)
        result[key] = entries
    for address in targets:
        _address(address, {"http", "https"})
    views = result.get("views", ["desktop", "mobile"])
    if (not isinstance(views, list) or not views
            or any(not isinstance(view, str) or view not in {"desktop", "mobile"} for view in views)
            or len(set(views)) != len(views)):
        raise ValueError("browser views must select desktop/mobile once each")
    policy = result.get("screenshots", "failure")
    if not isinstance(policy, str) or policy not in {"all", "failure", "none"}:
        raise ValueError("browser screenshots must be all, failure or none")
    if "readySelector" in result and (not isinstance(result["readySelector"], str) or not 1 <= len(result["readySelector"]) <= 1000):
        raise ValueError("readySelector must be a bounded selector")
    result.update(views=views, screenshots=policy)
    return result


def _address(address, schemes):
    if not isinstance(address, str) or len(address) > 2048:
        raise ValueError("browser endpoint must be a bounded URL")
    parsed = urlsplit(address)
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("browser endpoint requires a supported URL without embedded credentials")
    parsed.port


@dataclass(slots=True)
class BrowserScenarioVerifier:
    name: str
    declaration: dict
    required_numbers: tuple[int, ...]
    covered_criteria_ids: tuple[str, ...]
    timeout: float = 300.0
    isolate: bool = False
    temporary_root: Path | None = None
    kind: str = "browser_render"
    deterministic: bool = True

    def verify(self, context):
        session = (context._workspace_session or VerificationWorkspaceSession(context)) if self.isolate else None
        artifacts, stages, report, revision = [], [], None, ""
        static_revision, started = "", False
        failure, handle = "", None
        try:
            context.check_active()
            if session:
                handle = session.acquire(self.temporary_root, self.name)
            workspace = handle.workspace if handle else context.workspace
            before = workspace_snapshot(workspace, reject_aliases=True)
            revision = snapshot_revision(before)
            declaration = browser_declaration(self.declaration)
            scenario = workspace / declaration["scenario"]
            if not scenario.is_file() or is_unsafe_stored_path(scenario, boundary=workspace, reject_hardlinks=True):
                raise ValueError("Registered browser scenario is missing or aliased")
            static_source, static_target, static_files = None, None, None
            if "root" in declaration:
                static_source = context.workspace / declaration["root"]
                static_target = workspace / declaration["root"]
                # Isolation copies source files only. Preserve the explicitly
                # declared build separately, then bind checks to its exact bytes.
                static_files = _static_manifest(static_source, context,
                    copy_to=static_target if handle and not static_target.exists() else None)
                static_revision = snapshot_revision(static_files)
                if static_target != static_source and _static_manifest(static_target, context) != static_files:
                    raise ValueError("Browser static build changed while preparing verification")
            def unchanged_static():
                return static_files is None or (
                    _static_manifest(static_source, context) == static_files
                    and (static_target == static_source or _static_manifest(static_target, context) == static_files))
            from minicode.permissions import PermissionManager
            if not isinstance(context.permissions, PermissionManager):
                raise ValueError("Browser scenarios need the active command permission manager")
            environment, output = verification_environment(workspace)
            criteria_file = output / "criteria.json"
            criteria_file.write_text(json.dumps({"acceptanceCriteria": context.contract.acceptance_criteria,
                "requiredCriteria": self.required_numbers}, ensure_ascii=False), encoding="utf-8")
            captures = output / "browser"
            argv = [sys.executable, "-I", str(Path(__file__).with_name("project_browser.py")),
                "--scenario", str(scenario), "--output", str(captures), "--source-root", str(workspace),
                "--criteria-file", str(criteria_file), "--screenshots", declaration["screenshots"], "--trace", "failure"]
            argv.extend(["--url", declaration["url"]] if "url" in declaration else ["--root", str(workspace / declaration["root"])])
            for view in declaration["views"]:
                argv.extend(["--view", view])
            for key, flag in (("allowedOrigins", "--allow-origin"), ("allowedWebSockets", "--allow-websocket")):
                for address in declaration[key]:
                    argv.extend([flag, address])
            if declaration.get("readySelector"):
                argv.extend(["--ready-selector", declaration["readySelector"]])
            permissions = context.permissions.fork_for_operation(hashlib.sha256(
                json.dumps([argv, revision, static_revision, context.contract.contract_revision]).encode()).hexdigest())
            permissions.ensure_command(argv[0], argv[1:], str(workspace))
            if (snapshot_revision(workspace_snapshot(workspace)) != revision
                    or snapshot_revision(workspace_snapshot(context.workspace)) != revision):
                raise ValueError("Browser source changed while approving the scenario")
            if not unchanged_static():
                raise ValueError("Browser static build changed while approving the scenario")
            context.check_active()
            try:
                # Windows Chromium cannot use an extended-namespace TEMP path.
                with tempfile.TemporaryDirectory(prefix="minicode-browser-") as browser_temp:
                    started = True
                    completed = _run_verification_command(argv, cwd=str(workspace),
                        env={**environment, "TEMP": browser_temp, "TMP": browser_temp, "TMPDIR": browser_temp},
                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", capture_output=True,
                        timeout=context.remaining_timeout(self.timeout), cancellation_event=context.cancellation_event)
                if session:
                    session.settled()
                stages.append({"stage": "browser", "exit_code": completed.returncode,
                    "output": "\n".join((completed.stdout or "", completed.stderr or ""))[-12000:]})
            finally:
                store = context.artifact_store or RuntimeArtifactStore()
                if captures.is_dir():
                    paths = sorted(captures.iterdir(), key=lambda path: (
                        0 if path.name == "report.json" else 1 if path.suffix == ".zip" else 2, path.name))
                    for path in paths:
                        if not re.fullmatch(r"report\.json|(desktop|mobile)(?:-[A-Za-z0-9_-]{1,64})?\.png|(desktop|mobile)-trace\.zip", path.name):
                            continue
                        if is_unsafe_stored_path(path, boundary=captures, reject_hardlinks=True) or not path.is_file():
                            raise ValueError("Browser artifact is aliased")
                        context.check_active()
                        if path.stat().st_size > (64 * 1024 * 1024 if path.suffix == ".zip" else
                            256 * 1024 if path.suffix == ".json" else 16 * 1024 * 1024):
                            raise ValueError("Browser artifact exceeds its size limit")
                        data = path.read_bytes()
                        ref = store.put_bytes(data, run_id=context.contract.envelope.run_id, node_id=ARTIFACT_OWNER,
                            media_type="image/png" if path.suffix == ".png" else
                                       "application/zip" if path.suffix == ".zip" else "application/json")
                        artifacts.append({"name": path.name, "ref": asdict(ref)})
                        if path.name == "report.json":
                            report = json.loads(data)
            if completed.returncode or not report or not report.get("completed") or not report.get("passed"):
                raise ValueError("Browser scenario did not produce a completed passing report")
            actual = {item["id"] for item in report.get("criterion_results", [])
                      if item["required"] and item["status"] == "passed"}
            if actual != set(self.covered_criteria_ids) or report.get("source_revision") != revision:
                raise ValueError("Browser scenario coverage/source does not match the frozen task declaration")
            if (snapshot_revision(workspace_snapshot(workspace)) != revision
                    or snapshot_revision(workspace_snapshot(context.workspace)) != revision):
                raise ValueError("Browser source changed during verification")
            if not unchanged_static():
                raise ValueError("Browser static build changed during verification")
            context.check_active()
        except ToolApprovalCanceled:
            raise
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
        finally:
            if session and context._workspace_session is None:
                session.close()
        payload = {"recipe": ARTIFACT_OWNER, "scope": "browser-scenario", "candidate_revision": revision,
            "passed": not failure, "failure": failure, "execution": "executed" if started else "not-started", "browser": report,
            "static_revision": static_revision,
            "stages": stages, "artifacts": artifacts, "required_criteria": self.required_numbers,
            "interaction_coverage": bool(not failure and self.covered_criteria_ids)}
        serialized = json.dumps(payload, ensure_ascii=False)
        evidence = () if not revision or not stages else (EvidenceRef(evidence_type="command-exit",
            resource="runtime:" + self.name, content_hash=hashlib.sha256(serialized.encode()).hexdigest(),
            claim="Executed registered browser assertions for the declared task criteria",
            producer_node_id=self.name, base_revision=revision),)
        return VerificationRecord(verifier=self.name, passed=not failure,
            scope="final" if context.final else "node", details=serialized, evidence_refs=evidence,
            covered_criteria_ids=self.covered_criteria_ids if not failure else ())
