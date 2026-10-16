"""Explicit, idempotent recovery of failed candidates into new directories only."""
from __future__ import annotations

import hashlib
import json
import io
import os
from copy import deepcopy
from pathlib import Path
import zipfile

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool

from minicode.candidate_recovery import CandidateRecoveryStore, RECOVERY_MEDIA_TYPE
from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import ArtifactRef
from minicode.runtime_state import RuntimeStateStore
from minicode.session_contract import atomic_write_json
from minicode.store_lock import store_lock
from minicode.workspace_lock import WorkspaceWriteLease, normalize_workspace_path
from minicode.workspace_provider import TemporaryWorkspaceProvider, WorkspaceConflict, WorkspaceHandle, workspace_snapshot, snapshot_revision, _OWNER_MARKER


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool


def candidate_recovery_result(state, node_id):
    """A failed result or a fenced, fully settled exit; never a revived node."""
    node = state.get("nodes", {}).get(node_id, {})
    result = node.get("result") or {}
    if result.get("metrics", {}).get("candidate_recovery", {}).get("status") == "archived_unsubmitted":
        return result
    attempt_id = node.get("current_attempt_id")
    completion = state.get("worker_completions", {}).get(attempt_id, {})
    retained = completion.get("recovery_result") or {}
    if (node.get("state") in {"CANCELED", "FAILED_TERMINAL", "FAILED_RETRYABLE"}
        and completion.get("node_id") == node_id and completion.get("run_id") == state.get("run_id")
        and completion.get("ambiguous_side_effect") is False
        and retained.get("attempt_id") == attempt_id and retained.get("node_id") == node_id
        and retained.get("run_id") == state.get("run_id")
        and retained.get("artifact_refs") == completion.get("artifact_refs")
        and retained.get("metrics", {}).get("candidate_recovery") == completion.get("recovery")):
        return retained
    return result


def _candidates(session):
    for trace in session.runtime_runs:
        state = trace.get("state", {}) if isinstance(trace, dict) else {}
        for node_id, node in state.get("nodes", {}).items():
            result = candidate_recovery_result(state, node_id)
            diagnostic = result.get("metrics", {}).get("candidate_recovery", {})
            if diagnostic.get("status") != "archived_unsubmitted" or any(
                diagnostic.get(key) is not False for key in ("submitted", "verified", "integrated")
            ):
                continue
            for raw in result.get("artifact_refs", []):
                if raw.get("media_type") == RECOVERY_MEDIA_TYPE and raw.get("artifact_id") == diagnostic.get("artifact_id"):
                    yield state.get("run_id"), node_id, result, ArtifactRef(**raw)


class WebCandidateRecovery:
    def __init__(self):
        from minicode.config import MINI_CODE_DIR
        self.state_root = MINI_CODE_DIR
        self.root = MINI_CODE_DIR / "runtime-v2" / "candidate-recoveries"
        self.runs = MINI_CODE_DIR / "runtime-v2" / "runs"
        # Do not repeat the 64-character logical identity in every source path.
        # Ownership stays in the provider's marker, not a long directory name.
        self.workspaces = self.root / "workspaces"
        self.artifacts = RuntimeArtifactStore(MINI_CODE_DIR / "memory-v2/runtime-artifacts")

    def reconcile_interrupted(self, session, *, web_run_id=None):
        """Fence dead executions and archive owned bytes; never run any tools.

        Caller owns the Web receipt lease. Native ownership is separately
        acquired so a live worker can never be mistaken for an interrupted one.
        Pending effects remain quarantined; no completion/acceptance is forged.
        """
        from minicode.candidate_recovery import CandidateCrashJournal
        from minicode.run_ownership import RuntimeRunOwnerLease
        from minicode.runtime_state import RuntimeReducer, command
        from minicode.runtime_protocol import (AgentInvocationSpec, AgentResult, AgentResultStatus,
            BudgetEnvelope, ContextManifest, RuntimeCommandType)
        from minicode.usage_ledger import UsageLedger
        from minicode.web_run_continuation import continuation_contract

        for trace in session.runtime_runs:
            if not isinstance(trace, dict) or not trace.get("recovery_pending"):
                continue
            if web_run_id is not None and trace.get("web_run_id") != web_run_id:
                continue
            run_id = trace.get("state", {}).get("run_id")
            self.artifacts._validate_component(run_id)
            path = self.runs / (run_id + ".json")
            if is_unsafe_stored_path(path, boundary=self.state_root, reject_hardlinks=True):
                raise WorkspaceConflict("中断任务状态路径不安全。")
            with RuntimeRunOwnerLease(path, run_id=run_id) as lease:
                store = RuntimeStateStore(path, owner_lease=lease)
                state = store.load_required()
                identity = state.get("identity") or {}
                if (identity.get("session_id") != session.session_id or
                    normalize_workspace_path(identity.get("workspace", "")) != normalize_workspace_path(session.workspace)):
                    raise WorkspaceConflict("中断任务的会话归属不匹配。")
                reducer = RuntimeReducer.restore(state, store=store)
                lease.bind(reducer)
                if not reducer.terminal():
                    reducer.apply(command(run_id, RuntimeCommandType.CANCEL_RUN,
                        {"reason": "owning process exited before delivery was recorded"},
                        command_id="crash-recovery-cancel-" + run_id))
                for call_id, call in state.get("usage_ledger", {}).get("calls", {}).items():
                    if call["status"] == "pending":
                        reducer.record_model_usage(call_id, {**call.get("usage", {}),
                            "tokens_known": False, "cost_known": False}, complete=False, outcome="interrupted")
                journals = CandidateCrashJournal.directory(self.artifacts, run_id)
                # Read all descriptors before archiving any candidate. One
                # unacknowledged child effect also blocks primary continuation.
                entries = []
                for journal in journals.glob("*.json"):
                    if (is_unsafe_stored_path(journal, boundary=self.artifacts.root, reject_hardlinks=True)
                        or journal.stat().st_size > 16_000_000):
                        raise WorkspaceConflict("中断任务的工具记录不可读取。")
                    entry = json.loads(journal.read_text(encoding="utf-8"))
                    node = state.get("nodes", {}).get(entry.get("node_id"), {})
                    if (entry.get("schema") != "candidate-crash-journal-v1" or entry.get("run_id") != run_id
                        or journal.stem != entry.get("attempt_id")
                        or not any(a.get("attempt_id") == entry.get("attempt_id") for a in node.get("attempts", []))):
                        raise WorkspaceConflict("中断任务的工具记录归属不匹配。")
                    entries.append(entry)
                uncertain = any(e["pending_tools"] or e["external_effects"] for e in entries
                    if e["attempt_id"] not in state.get("worker_completions", {}))
                for entry in entries:
                    attempt = entry["attempt_id"]
                    node = state["nodes"][entry["node_id"]]
                    if (attempt in state.get("worker_completions", {}) or node.get("result")
                        or attempt != node.get("current_attempt_id")):
                        continue
                    details = {"status": "workspace_retained", "workspace": entry["workspace"],
                        "submitted": False, "verified": False, "integrated": False}
                    ref = None
                    if uncertain:
                        details["reason"] = "uncertain_side_effect_or_inflight_tool"
                    elif entry.get("archive_complete"):
                        if entry.get("archive") is None:
                            continue
                        ref = ArtifactRef(**entry["archive"])
                        payload = json.loads(self.artifacts.get_text(ref))
                        if (ref.media_type != RECOVERY_MEDIA_TYPE or payload.get("schema") != "unsubmitted-candidate-v1"
                            or any(payload.get(key) != entry[key] for key in
                                ("run_id", "node_id", "attempt_id", "plan_version", "contract_revision", "source", "base_hashes"))
                            or any(payload.get(key) is not False for key in ("submitted", "verified", "integrated"))):
                            raise WorkspaceConflict("中断任务的归档记录不匹配。")
                        if normalize_workspace_path(entry["source"]) != normalize_workspace_path(session.workspace):
                            if entry["node_id"] == "primary":
                                raise WorkspaceConflict("中断候选不属于当前工作区。")
                            details.update(reason="child_candidate_retained", artifact_id=ref.artifact_id)
                        else:
                            details.update(status="archived_unsubmitted", artifact_id=ref.artifact_id)
                    elif entry["isolated"]:
                        handle = WorkspaceHandle(Path(entry["source"]), Path(entry["workspace"]),
                            entry["base_hashes"], True, entry["owner"])
                        provider = TemporaryWorkspaceProvider(handle.workspace.parent.parent)
                        provider.validate_owner(handle)
                        manifest = ContextManifest(entry["contract_revision"], entry["plan_version"],
                            entry["node_id"], snapshot_revision(entry["base_hashes"]), {})
                        invocation = AgentInvocationSpec(run_id, entry["plan_version"], entry["node_id"],
                            attempt, "recovery", "retain interrupted work", manifest, (), BudgetEnvelope())
                        # Preserve the original directory even after archival;
                        # only explicit cleanup may remove crash leftovers.
                        if normalize_workspace_path(handle.source) != normalize_workspace_path(session.workspace):
                            if entry["node_id"] == "primary":
                                raise WorkspaceConflict("中断候选不属于当前工作区。")
                            # A builder may be based on its parent's candidate,
                            # not the user's source. Preserve it for inspection;
                            # only already-integrated work in the primary can
                            # seed continuation against the user's baseline.
                            details["reason"] = "child_candidate_retained"
                        else:
                            with WorkspaceWriteLease(session.workspace, run_id="recover-" + run_id):
                                ref = CandidateRecoveryStore(self.artifacts).archive(handle, invocation, provider)
                            if ref is not None:
                                details.update(status="archived_unsubmitted", artifact_id=ref.artifact_id)
                            else:
                                details.update(status="no_changes")
                    reducer.record_worker_completion(AgentResult(run_id, entry["plan_version"], entry["node_id"],
                        attempt, AgentResultStatus.CANCELED, snapshot_revision(entry["base_hashes"]),
                        "执行进程已退出，保留中断前的工作；尚未验收或交付。", contract_revision=entry["contract_revision"],
                        artifact_refs=(ref,) if ref else (), metrics={"candidate_recovery": details}))
                current = reducer.snapshot()
                usage = UsageLedger.restore(current["usage_ledger"]).summary()
                trace["state"] = current
                trace["contract"] = continuation_contract(current)
                settings = trace.get("web_continuation_settings")
                if isinstance(settings, dict):
                    for public, native in (("taskConstraints", "constraints"), ("acceptanceCriteria", "acceptance_criteria"),
                                           ("expectedOutputs", "expected_outputs")):
                        settings[public] = deepcopy(trace["contract"].get(native, []))
                trace["scheduler"].update(canceled=True, completed=False, tokens_used=usage["tokens"],
                    tokens_known=usage["tokens_known"], cost_known=usage["cost_known"], cost_used=usage["known_cost"],
                    model_calls=usage["model_calls"], active_leases=0)
                trace["delivery"].update(completed=False)
                trace["recovery_pending"] = False
                trace["interrupted"] = True

    def _directory(self, session_id, run_id, artifact_id):
        identity = hashlib.sha256(json.dumps([session_id, run_id, artifact_id]).encode()).hexdigest()
        directory = self.root / identity
        if is_unsafe_stored_path(directory, boundary=self.state_root):
            raise WorkspaceConflict("恢复记录路径不安全。")
        return directory

    def _receipt(self, directory):
        path = directory / "receipt.json"
        if is_unsafe_stored_path(path, boundary=self.state_root, reject_hardlinks=True):
            raise WorkspaceConflict("恢复记录路径不安全。")
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _public_receipt(self, directory, receipt):
        value = {"status": receipt["status"]}
        if receipt["status"] == "restored":
            workspace = Path(receipt["workspace"])
            # Never expose a substituted receipt's unrelated host path.
            if (not workspace.is_relative_to(self.workspaces) or
                is_unsafe_stored_path(workspace, boundary=self.state_root)):
                raise WorkspaceConflict("恢复目录路径不安全。")
            if not workspace.is_dir():
                return {"status": "needs_attention"}
            marker = workspace.parent / _OWNER_MARKER
            if (is_unsafe_stored_path(marker, boundary=self.state_root, reject_hardlinks=True) or
                not marker.is_file() or marker.read_text(encoding="utf-8") != directory.name):
                raise WorkspaceConflict("恢复目录的归属不匹配。")
            value.update(workspace=str(workspace), submitted=False, verified=False, integrated=False)
        return value

    def list(self, session):
        values = []
        for run_id, node_id, result, ref in _candidates(session):
            directory = self._directory(session.session_id, run_id, ref.artifact_id)
            receipt = self._receipt(directory)
            values.append({"runId": run_id, "nodeId": node_id, "attemptId": result["attempt_id"],
                "artifactId": ref.artifact_id, "contentHash": ref.content_hash,
                "submitted": False, "verified": False, "integrated": False,
                "recovery": self._public_receipt(directory, receipt) if receipt else None})
        return values

    def _submitted(self, session, run_id):
        """Resolve only the current accepted primary checkpoint of an owned run.

        No browser-supplied host path or artifact store key is accepted. Viewing
        retained work does not grant submission, verification or integration.
        """
        matches = [trace.get("state", {}) for trace in session.runtime_runs
                   if trace.get("state", {}).get("run_id") == run_id]
        if len(matches) != 1:
            raise HTTPException(404, "此会话没有这份候选。")
        state = matches[0]
        node = state.get("nodes", {}).get("primary", {})
        result = node.get("result") or {}
        raw = result.get("session_checkpoint")
        # Direct runs also retain repair checkpoints. Only candidate-first runs
        # have isolated files awaiting integration, even if a checkpoint expires.
        if (state.get("candidate_first") is not True or node.get("state") != "SUCCEEDED"
                or not raw or state.get("integration_status") == "committed"):
            raise HTTPException(404, "没有待交付的已提交候选。")
        ref = ArtifactRef(**raw)
        if (ref.media_type != "application/vnd.minicode.primary-repair-checkpoint+json"
                or not ref.store_key.replace("\\", "/").startswith(f"{run_id}/primary/")):
            raise WorkspaceConflict("候选检查点归属不匹配。")
        payload = json.loads(self.artifacts.get_text(ref))
        if (payload.get("schema") != "primary-repair-checkpoint-v1"
                or any(payload.get(key) != result.get(key) for key in
                       ("run_id", "node_id", "attempt_id", "plan_version", "contract_revision"))
                or payload.get("run_id") != run_id or payload.get("node_id") != "primary"):
            raise WorkspaceConflict("候选检查点与当前结果不匹配。")
        descriptor = payload.get("workspace") or {}
        if (descriptor.get("isolated") is not True
                or normalize_workspace_path(descriptor.get("source", "")) != normalize_workspace_path(session.workspace)):
            raise WorkspaceConflict("候选不属于此项目。")
        handle = WorkspaceHandle(Path(session.workspace).resolve(), Path(descriptor["path"]),
                                 descriptor["base_hashes"], True, descriptor["owner"])
        TemporaryWorkspaceProvider().validate_owner(handle)
        return state, payload, handle

    def submitted(self, session):
        values = []
        for trace in reversed(session.runtime_runs):
            state = trace.get("state", {})
            result = state.get("nodes", {}).get("primary", {}).get("result") or {}
            if not result.get("session_checkpoint") or state.get("integration_status") == "committed":
                continue
            try:
                _, payload, handle = self._submitted(session, state["run_id"])
                values.append({"runId": state["run_id"], "revision": payload["candidate_revision"],
                    "artifactId": result["session_checkpoint"]["artifact_id"],
                    "workspace": str(handle.workspace), "submitted": True, "integrated": False,
                    "verificationStatus": state.get("nodes", {}).get("final-verifier", {}).get("state", "PENDING"),
                    "available": True})
            except HTTPException as error:
                if error.status_code != 404:
                    raise
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                values.append({"runId": state.get("run_id", ""), "available": False,
                    "submitted": True, "integrated": False, "verificationStatus": "UNKNOWN"})
        return values

    def candidate_files(self, session, run_id, path, *, content=False):
        from minicode.web_api import _list_workspace_entries, _read_workspace_file, _resolve_workspace_member
        _, _, handle = self._submitted(session, run_id)
        member = _resolve_workspace_member(handle.workspace, path, allow_root=not content)
        if is_unsafe_stored_path(member, boundary=handle.workspace, reject_hardlinks=True):
            raise WorkspaceConflict("候选文件路径不安全。")
        if content:
            return _read_workspace_file(handle.workspace, path)
        listing = _list_workspace_entries(handle.workspace, path)
        listing["entries"] = [entry for entry in listing["entries"] if not is_unsafe_stored_path(
            handle.workspace / entry["path"], boundary=handle.workspace, reject_hardlinks=True)]
        return listing

    def candidate_archive(self, session, run_id):
        """Export source files only, with exact revision checks before and after.

        Generated dependency/cache directories are omitted, as in the file UI.
        The archive is a download, never a write-back to the user's project.
        """
        from minicode.web_api import _IGNORED_BROWSER_DIRECTORIES
        _, payload, handle = self._submitted(session, run_id)
        provider = TemporaryWorkspaceProvider()
        provider.reopen(source=handle.source, workspace=handle.workspace, owner_token=handle.owner_token,
                        base_hashes=handle.base_hashes, expected_revision=payload["candidate_revision"])
        buffer = io.BytesIO()
        total = count = 0
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for root, directories, files in os.walk(handle.workspace, followlinks=False):
                directories[:] = [name for name in directories if name.casefold() not in _IGNORED_BROWSER_DIRECTORIES
                    and not is_unsafe_stored_path(Path(root) / name, boundary=handle.workspace)]
                for name in sorted(files):
                    path = Path(root) / name
                    if is_unsafe_stored_path(path, boundary=handle.workspace, reject_hardlinks=True):
                        raise WorkspaceConflict("候选包含不安全的文件别名。")
                    size = path.stat().st_size
                    count += 1
                    total += size
                    if count > 10000 or size > 16 * 1024 * 1024 or total > 128 * 1024 * 1024:
                        raise WorkspaceConflict("候选超过下载大小限制，请使用显示的候选目录。")
                    with path.open("rb") as stream:
                        data = stream.read(size + 1)
                    if len(data) != size:
                        raise WorkspaceConflict("导出时文件发生变化。")
                    archive.writestr(path.relative_to(handle.workspace).as_posix(), data)
        provider.reopen(source=handle.source, workspace=handle.workspace, owner_token=handle.owner_token,
                        base_hashes=handle.base_hashes, expected_revision=payload["candidate_revision"])
        return buffer.getvalue()

    def restore(self, session, run_id, artifact_id):
        matches = [(node, result, ref) for run, node, result, ref in _candidates(session)
                   if run == run_id and ref.artifact_id == artifact_id]
        if len(matches) != 1:
            raise HTTPException(404, "此会话没有这份未提交候选。")
        node_id, result, ref = matches[0]
        # Load a fixed, server-owned path, never a browser-supplied store key.
        RuntimeArtifactStore._validate_component(run_id)
        state_path = self.runs / f"{run_id}.json"
        if is_unsafe_stored_path(state_path, boundary=self.state_root, reject_hardlinks=True):
            raise WorkspaceConflict("原任务状态路径不安全。")
        state = RuntimeStateStore(state_path).load_required()
        node = state.get("nodes", {}).get(node_id, {})
        if (state.get("run_id") != run_id or candidate_recovery_result(state, node_id) != result or
            node.get("state") not in {"FAILED_TERMINAL", "CANCELED", "FAILED_RETRYABLE"} or
            state.get("integration_status") == "committed"):
            raise WorkspaceConflict("原任务状态已改变或不是可恢复的失败候选。")
        source = Path(session.workspace)
        payload = json.loads(self.artifacts.get_text(ref))
        if (not source.is_dir() or is_unsafe_stored_path(source, boundary=source.parent) or
            any(payload.get(key) != expected for key, expected in (
                ("run_id", run_id), ("node_id", node_id), ("attempt_id", result["attempt_id"]),
                ("plan_version", result["plan_version"]), ("contract_revision", result["contract_revision"]))) or
            normalize_workspace_path(payload.get("source", "")) != normalize_workspace_path(source)):
            raise WorkspaceConflict("候选与原会话、工作区或尝试的归属不匹配。")
        directory = self._directory(session.session_id, run_id, artifact_id)
        if is_unsafe_stored_path(self.workspaces, boundary=self.state_root):
            raise WorkspaceConflict("恢复工作区路径不安全。")
        with store_lock(directory / ".restore.lock"), WorkspaceWriteLease(source, run_id="recovery-" + directory.name):
            if RuntimeStateStore(state_path).load_required() != state:
                raise WorkspaceConflict("取得恢复锁前原任务状态发生变化，请重新检查。")
            receipt = self._receipt(directory)
            if receipt:
                if receipt.get("status") != "restored":
                    raise WorkspaceConflict("上次恢复未完成；记录与目录已保留，请人工检查，未自动重试。")
                provider = TemporaryWorkspaceProvider(self.workspaces)
                provider.reopen(source=source, workspace=Path(receipt["workspace"]), owner_token=directory.name,
                    base_hashes=payload["base_hashes"], expected_revision=payload["candidate_revision"])
                return self._public_receipt(directory, receipt)
            if workspace_snapshot(source) != payload.get("base_hashes"):
                # A user can resolve a baseline conflict and explicitly click
                # again; no restore was started, so do not poison idempotency.
                raise WorkspaceConflict("原目录已变化；未创建恢复副本。")
            provider = TemporaryWorkspaceProvider(self.workspaces)
            # A crash must not silently create another candidate on the next click.
            atomic_write_json(directory / "receipt.json", {"status": "preparing"})
            try:
                handle = CandidateRecoveryStore(self.artifacts).restore_isolated(ref, source=source,
                    provider=provider, owner_token=directory.name)
                receipt = {"status": "restored", "workspace": str(handle.workspace)}
                atomic_write_json(directory / "receipt.json", receipt)
                return self._public_receipt(directory, receipt)
            except Exception as error:
                # Archive, source and any newly created owned directory survive.
                # The core restore cleans only its own partial copy on failure.
                atomic_write_json(directory / "receipt.json", {"status": "needs_attention",
                    "errorType": type(error).__name__, "errno": getattr(error, "errno", None),
                    "winerror": getattr(error, "winerror", None)})
                raise


def install_candidate_recovery_routes(app, manager):
    from minicode import session as sessions
    from minicode.web_api import _require_unarchived_session
    service = WebCandidateRecovery()

    def current(identity):
        _require_unarchived_session(identity)
        manager.latest_for_session(identity)
        value = sessions.load_session(identity)
        if value is None:
            raise HTTPException(404, "会话不存在。")
        return value

    @app.get("/api/sessions/{identity}/submitted-candidates")
    def submitted_candidates(identity: str):
        return {"candidates": service.submitted(current(identity))}

    @app.get("/api/sessions/{identity}/submitted-candidates/{run_id}/files")
    def candidate_files(identity: str, run_id: str, path: str = "", content: bool = False):
        try:
            return service.candidate_files(current(identity), run_id, path, content=content)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise HTTPException(409, "候选文件不可用或归属检查失败；原项目未修改。") from exc

    @app.get("/api/sessions/{identity}/submitted-candidates/{run_id}/archive")
    def candidate_archive(identity: str, run_id: str):
        from fastapi.responses import Response
        try:
            data = service.candidate_archive(current(identity), run_id)
            return Response(data, media_type="application/zip", headers={
                "Content-Disposition": f'attachment; filename="candidate-{run_id}.zip"',
                "Cache-Control": "no-store"})
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise HTTPException(409, "候选已变化、不可用或超过导出限制；原项目未修改。") from exc

    @app.get("/api/sessions/{identity}/candidate-recoveries")
    def recoveries(identity: str):
        try:
            return {"candidates": service.list(current(identity))}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise HTTPException(409, "候选恢复记录不可用；原记录未修改。") from exc

    @app.post("/api/sessions/{identity}/candidate-recoveries/{run_id}/{artifact_id}/restore")
    def restore(identity: str, run_id: str, artifact_id: str, body: RestoreRequest):
        if not body.confirm:
            raise HTTPException(400, "请明确确认仅恢复到新隔离目录。")
        with manager._lock:
            if manager.session_is_active(identity):
                raise HTTPException(409, "任务运行中，请结束后再恢复候选。")
            try:
                return {"recovery": service.restore(current(identity), run_id, artifact_id)}
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                raise HTTPException(409, "恢复被拒绝：请检查原目录是否变化、归档是否完整，或上次恢复是否需要人工处理。原任务未重跑、未合入。") from exc
