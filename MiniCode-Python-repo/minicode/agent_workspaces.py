"""Optional, owned Git checkouts for assistants; ordinary assistants share files.

A worktree contains the selected repository's committed HEAD. Use a snapshot
when the assignment needs the parent's uncommitted files or installed packages.
Neither mode grants write permissions or integrates changes into the project.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from minicode.path_safety import is_unsafe_stored_path
from minicode.workspace_provider import (
    WorkspaceConflict, WorkspaceHandle, WorkspacePreparationCanceled,
    _check_preparation, snapshot_revision, workspace_snapshot,
)


def shared_revision(node_id: str) -> str:
    """An observation identity, deliberately not a content revision/hash."""
    return "shared:" + node_id


def valid_agent_assignment(node, record, *, shared_writes=False):
    from minicode.runtime_protocol import ToolEffect
    from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
    if "name" in record or "checks" in record or "optional" in record:
        try:
            createApp().validate_agent_dispatch({"tasks": [{"idempotency_key": "validate", "objective": node.objective,
                "agent_type": {"explorer": "explore", "planner": "plan", "reviewer": "review", "verifier": "verify", "shared_writer": "worker"}[node.role],
                "name": record.get("name", ""), "checks": record.get("checks", []),
                "optional": record.get("optional", False),
                "write_scope": list(node.write_scope), "isolation": record.get("workspace", {}).get("mode", "shared"),
                "acceptance_criteria": list(node.acceptance_criteria)}]})
        except (ValueError, KeyError):
            return False
    if node.role != "shared_writer":
        return (node.role in {"explorer", "planner", "reviewer", "verifier"}
                and node.allowed_effects == (ToolEffect.READ_ONLY,) and not node.write_scope)
    if (not shared_writes or node.allowed_effects != (ToolEffect.READ_ONLY, ToolEffect.WORKSPACE_WRITE)
        or record.get("workspace", {}).get("mode") != "shared"
        or record.get("workspace", {}).get("parent_isolated") is not False):
        return False
    from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
    try:
        createApp().validate_agent_dispatch({"tasks": [{"idempotency_key": "validate", "objective": node.objective,
            "agent_type": "worker", "context_mode": record.get("context_mode", "isolated"),
            "isolation": "shared", "write_scope": list(node.write_scope)}]})
    except ValueError:
        return False
    return True


def valid_agent_workspace(record: dict) -> bool:
    workspace = record.get("workspace")
    if not isinstance(workspace, dict):
        return False
    mode = workspace.get("mode")
    revision = record.get("input_revision")
    if mode == "shared":
        return (workspace.get("isolated") is False and workspace.get("base_hashes") == {}
                and workspace.get("parent_node_id") == record.get("parent_node_id") == "primary"
                and type(workspace.get("parent_isolated")) is bool
                and revision == shared_revision(record.get("task_id", ""))
                and record.get("definition", {}).get("role") != "verifier")
    return (mode in {"snapshot", "worktree"} and workspace.get("isolated") is True
            and isinstance(revision, str) and len(revision) == 64
            and all(c in "0123456789abcdef" for c in revision)
            and isinstance(workspace.get("base_hashes"), dict)
            and snapshot_revision(workspace["base_hashes"]) == revision)


class AgentWorktreeProvider:
    def __init__(self, temporary_root: Path | None = None):
        self.root = Path(temporary_root or tempfile.gettempdir()).resolve()

    @staticmethod
    def _git(root, *args, cancellation_event=None):
        # Git hooks are project code; creating an assistant must not execute them.
        process = subprocess.Popen(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 60
        try:
            while True:
                _check_preparation(cancellation_event)
                if time.monotonic() >= deadline:
                    raise WorkspaceConflict("Git worktree operation exceeded 60 seconds")
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            from minicode.tools.run_command import _kill_process_tree
            _kill_process_tree(process)
            process.communicate()
            raise
        if process.returncode:
            raise WorkspaceConflict(stderr.decode("utf-8", "replace").strip() or "Git worktree operation failed")
        return stdout.decode("utf-8").strip()

    def create(self, source: Path, *, owner_token: str, cancellation_event=None):
        _check_preparation(cancellation_event)
        source = Path(source).resolve()
        try:
            repo = Path(self._git(source, "rev-parse", "--show-toplevel", cancellation_event=cancellation_event)).resolve()
            head = self._git(repo, "rev-parse", "--verify", "HEAD", cancellation_event=cancellation_event)
        except WorkspacePreparationCanceled:
            raise
        except (WorkspaceConflict, OSError) as error:
            raise WorkspaceConflict("worktree requires a Git repository with a commit; use isolation=snapshot for current files") from error
        if self.root == repo or repo in self.root.parents:
            raise WorkspaceConflict("assistant worktrees must be stored outside the Git repository")
        common = self._git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", cancellation_event=cancellation_event)
        offset = source.relative_to(repo).as_posix()
        self.root.mkdir(parents=True, exist_ok=True)
        container = Path(tempfile.mkdtemp(prefix="minicode-agent-worktree-", dir=self.root)).resolve()
        checkout = container / "checkout"
        descriptor = {"container": str(container), "repo": str(repo), "head": head, "common_dir": common,
                      "offset": offset, "owner": owner_token}
        (container / ".minicode-agent-worktree").write_text(json.dumps(descriptor), encoding="utf-8")
        try:
            self._git(repo, "worktree", "add", "--detach", str(checkout), head, cancellation_event=cancellation_event)
            workspace = checkout / offset
            if not workspace.is_dir():
                raise WorkspaceConflict("the selected directory is absent from committed HEAD; use isolation=snapshot")
            baseline = workspace_snapshot(workspace, reject_aliases=True, cancellation_event=cancellation_event)
            return WorkspaceHandle(source, workspace, baseline, True, owner_token), descriptor
        except BaseException:
            self.cleanup_descriptor(descriptor)
            raise

    def _validate(self, descriptor):
        container = Path(descriptor["container"])
        if (not container.is_absolute() or container.parent.resolve() != self.root
                or not container.name.startswith("minicode-agent-worktree-")
                or is_unsafe_stored_path(container, boundary=self.root)):
            raise WorkspaceConflict("assistant worktree is outside its owned root")
        marker = container / ".minicode-agent-worktree"
        if (is_unsafe_stored_path(marker, boundary=self.root, reject_hardlinks=True)
                or json.loads(marker.read_text(encoding="utf-8")) != descriptor):
            raise WorkspaceConflict("assistant worktree ownership does not match")
        offset = Path(descriptor["offset"])
        if offset.is_absolute() or ".." in offset.parts or not descriptor.get("owner"):
            raise WorkspaceConflict("invalid assistant worktree descriptor")
        checkout = container / "checkout"
        if is_unsafe_stored_path(checkout / offset, boundary=container):
            raise WorkspaceConflict("assistant worktree contains an unsafe directory")
        return container, checkout, checkout / offset

    def reopen(self, descriptor, *, source, path, owner, base_hashes, expected_revision):
        _, checkout, workspace = self._validate(descriptor)
        if workspace.resolve() != Path(path).resolve() or owner != descriptor["owner"]:
            raise WorkspaceConflict("assistant worktree binding does not match")
        if self._git(checkout, "rev-parse", "HEAD") != descriptor["head"]:
            raise WorkspaceConflict("assistant worktree revision changed")
        if self._git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir") != descriptor["common_dir"]:
            raise WorkspaceConflict("assistant worktree repository identity changed")
        if snapshot_revision(workspace_snapshot(workspace, reject_aliases=True)) != expected_revision:
            raise WorkspaceConflict("assistant worktree files changed")
        return WorkspaceHandle(Path(source), workspace, dict(base_hashes), True, owner)

    def cleanup_descriptor(self, descriptor):
        container, checkout, _ = self._validate(descriptor)
        repo = Path(descriptor["repo"])
        entries = self._git(repo, "worktree", "list", "--porcelain", "-z").split("\0")
        registered = any(line.startswith("worktree ") and Path(line[9:]).resolve() == checkout.resolve() for line in entries)
        if registered:
            self._git(repo, "worktree", "remove", "--force", str(checkout))
        elif checkout.exists():
            # A failed creation may leave an unregistered partial checkout. Only
            # the marker-owned container, validated above, is ever removed.
            if is_unsafe_stored_path(checkout, boundary=container):
                raise WorkspaceConflict("unsafe partial assistant checkout")
        shutil.rmtree(container)

    def cleanup(self, handle, descriptor):
        if not handle.closed:
            _, checkout, _ = self._validate(descriptor)
            if self._git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir") != descriptor["common_dir"]:
                raise WorkspaceConflict("assistant worktree repository identity changed")
            if self._git(checkout, "rev-parse", "HEAD") != descriptor["head"]:
                raise WorkspaceConflict("assistant worktree revision changed; retained for inspection")
            if self._git(checkout, "status", "--porcelain", "--untracked-files=all", "--ignored=matching"):
                raise WorkspaceConflict("assistant worktree has changes; retained for inspection")
            self.cleanup_descriptor(descriptor)
            handle.closed = True
