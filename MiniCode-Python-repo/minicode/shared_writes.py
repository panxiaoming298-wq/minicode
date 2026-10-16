"""Scoped direct writers sharing one parent directory.

Assignments and their order live in the Reducer, so restarting never invents
ownership or replays an effectful task. This gate serializes short tool writes
and dispatch, not model turns. It is a cooperative tool boundary, not an OS
sandbox; shared children therefore cannot launch arbitrary mutation commands.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, TYPE_CHECKING

from minicode.candidate_write_scope import CandidateWriteRefused
from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_protocol import AgentInvocationSpec, NodeState, PlanNode, ToolEffect

if TYPE_CHECKING:
    from minicode.runtime_scheduler import TaskScheduler
    from minicode.tooling import ToolContext, ToolDefinition
    from minicode.worker_runtime import AgentLoopWorkerRunner


TERMINAL = {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.CANCELED, NodeState.SUPERSEDED}


class SharedWriterWaitRequired(CandidateWriteRefused):
    """A native pre-execution refusal whose prerequisite is live scope release."""


def _key(path: str) -> str:
    return os.path.normcase(path).replace("\\", "/")


def _contains(scope: Sequence[str], path: str) -> bool:
    return any(_key(path) == _key(p) or _key(path).startswith(_key(p) + "/") for p in scope)


def _overlaps(left: Sequence[str], right: Sequence[str]) -> bool:
    return any(_contains(left, p) for p in right) or any(_contains(right, p) for p in left)


class SharedWorkspaceWrites:
    def __init__(self, scheduler: TaskScheduler) -> None:
        self.scheduler = scheduler
        self.lock = threading.RLock()
        self.active: dict[str, str] = {}
        self.changes: dict[tuple[str, str], set[str]] = {}
        self.hashes: dict[tuple[str, str], dict[str, str | None]] = {}

    @property
    def enabled(self) -> bool:
        return (self.scheduler.profile.shared_workspace_writes
                and self.scheduler.reducer.shared_workspace_writes_enabled)

    def assignments(self) -> list[dict[str, Any]]:
        records = self.scheduler.reducer.agent_sessions()
        return [records[n.node_id] for n in self.scheduler.reducer.current_plan.nodes
                if n.node_id in records and n.role == "shared_writer"]

    def pending(self) -> list[dict[str, Any]]:
        quarantined = {r["node_id"] for r in self.scheduler.reducer.candidate_quarantines().values()
                       if r.get("status") == "pending"}
        return [r for r in self.assignments() if r["task_id"] in self.active
                or r["task_id"] in quarantined
                or self.scheduler.reducer.node_state(r["task_id"]) not in TERMINAL]

    def coordination(self, attempt_id: str) -> dict[str, Any]:
        """Fresh advisory state; the tool boundary remains the authority to write."""
        from minicode.dependency_waits import DEFAULT_AGENT_WAIT_SECONDS
        acquired = self.lock.acquire(blocking=False)
        try:
            # An in-flight file tool must not hold model input preparation
            # hostage. Without the lock, scope release remains unresolved.
            pending = self.pending() if acquired else self.assignments()
            quarantined = {r["node_id"] for r in self.scheduler.reducer.candidate_quarantines().values()
                           if r.get("status") == "pending"}
            tasks = [{"task_id": r["task_id"], "state": self.scheduler.reducer.node_state(r["task_id"]).value,
                      "write_scope": list(r["definition"]["write_scope"]),
                      "activation_running": r["task_id"] in self.active,
                      "effect_unresolved": r["task_id"] in quarantined} for r in pending]
            waitable = [r["task_id"] for r in pending
                        if self.scheduler.reducer.node_state(r["task_id"]) not in TERMINAL]
            result = {"schema": "minicode-shared-write-coordination-v1", "pending": tasks,
                      "scopes_released": acquired and not pending,
                      "scope_status": "available" if acquired else "write_in_progress",
                      "notice": ("Tools remain available. Shared writers own only their assigned file scopes and use "
                          "native read_file/edit_file/write_file. The primary owns commands and final checks. "
                          "Before commands, wait_agents(mode=all) for every unfinished shared writer. "
                          "A native wait checkpoints this conversation and releases the execution slot. "
                          "On timeout, failure, cancellation or reassignment, inspect poll_agents and current scopes. "
                          "A timeout does not stop a child or release its scope. Use a new wait_id for another bounded wait. "
                          "After scopes release, reread changed files and explicitly issue the needed command; "
                          "no refused command is executed or replayed automatically. "
                          "Terminal tasks with running activations or unresolved effects still block mutations: "
                          "inspect their status, report the blocker if it persists, and do not repeatedly wait on terminal IDs.")}
            if waitable:
                digest = hashlib.sha256(json.dumps(waitable).encode()).hexdigest()[:12]
                result["wait_arguments"] = {"task_ids": waitable, "mode": "all",
                    "wait_id": f"shared-{attempt_id}-{digest}"[:128], "timeout_seconds": DEFAULT_AGENT_WAIT_SECONDS}
            return result
        finally:
            if acquired:
                self.lock.release()

    def _wait_required(self, context: ToolContext, reason: str) -> SharedWriterWaitRequired:
        return SharedWriterWaitRequired(reason + "; this call was not executed.\n"
            + json.dumps(self.coordination(context.attempt_id), ensure_ascii=False))

    def can_start(self, node: PlanNode) -> bool:
        if node.role != "shared_writer":
            return True
        if not self.lock.acquire(blocking=False):
            return False
        try:
            for record in self.pending():
                if record["task_id"] == node.node_id:
                    return True
                if _overlaps(node.write_scope, record["definition"]["write_scope"]):
                    return False
        finally:
            self.lock.release()
        return False

    @contextmanager
    def activation(self, invocation: AgentInvocationSpec) -> Iterator[None]:
        if not self.enabled or invocation.role != "shared_writer":
            yield
            return
        with self.lock:
            self.active[invocation.node_id] = invocation.attempt_id
            self.changes[(invocation.node_id, invocation.attempt_id)] = set()
            self.hashes[(invocation.node_id, invocation.attempt_id)] = {}
        try:
            yield
        finally:
            with self.lock:
                if self.active.get(invocation.node_id) == invocation.attempt_id:
                    self.active.pop(invocation.node_id, None)

    def changed_paths(self, node_id: str, attempt_id: str) -> tuple[str, ...]:
        with self.lock:
            return tuple(sorted(self.changes.get((node_id, attempt_id), ())))

    def receipt(self, node_id: str, attempt_id: str, root: Path) -> dict[str, Any]:
        with self.lock:
            return {"workspace": str(root.resolve()), "writes_already_in_parent": True,
                    "content_hashes": dict(self.hashes.get((node_id, attempt_id), {}))}

    def require_settled(self) -> None:
        if not self.lock.acquire(blocking=False):
            raise ValueError("a shared file tool is still executing; poll_agents or wait_agents before final checks or submission")
        try:
            if self.pending():
                raise ValueError("shared writers still own file scopes; wait_agents before final checks or submission")
        finally:
            self.lock.release()

    @contextmanager
    def _mutation_lock(self, context: ToolContext) -> Iterator[None]:
        if context.node_id == "primary":
            if not self.lock.acquire(blocking=False):
                self._live(context)
                raise self._wait_required(context, "a shared file tool is still executing; the primary must wait for scope release")
        else:
            self.lock.acquire()
        try:
            yield
        finally:
            self.lock.release()

    def _live(self, context: ToolContext) -> None:
        reducer = self.scheduler.reducer
        attempt = reducer.current_attempt(context.node_id)
        if (self.scheduler.cancellation_event.is_set() or reducer.node_cancel_epoch(context.node_id)
            or reducer.node_state(context.node_id) is not NodeState.RUNNING
            or attempt is None or attempt.attempt_id != context.attempt_id
            or not attempt.lease.is_valid(self.scheduler.clock.now())):
            raise CandidateWriteRefused("shared write requires the current uncanceled running activation")
        if context.node_id != "primary" and self.active.get(context.node_id) != context.attempt_id:
            raise CandidateWriteRefused("shared writer has not acquired its activation")

    @contextmanager
    def tool_boundary(self, context: ToolContext, tool: ToolDefinition, arguments: Mapping[str, Any],
                      worker: AgentLoopWorkerRunner) -> Iterator[None]:
        if not self.enabled or tool.effect is ToolEffect.READ_ONLY:
            yield
            return
        node = self.scheduler._node_definition(context.node_id)
        if context.node_id != "primary" and node.role != "shared_writer":
            raise CandidateWriteRefused("read-only assistants cannot obtain shared write authority")
        from minicode.tools.write_file import write_file_tool
        from minicode.tools.edit_file import edit_file_tool
        from minicode.paged_memory.tools import is_native_paged_state_tool

        # State-only controls retain their existing native authorization. Agent
        # dispatch takes this same lock separately, through the Scheduler.
        if is_native_paged_state_tool(tool):
            yield
            return
        with self._mutation_lock(context):
            self._live(context)
            records = self.assignments()
            pending = self.pending()
            native_file = tool is write_file_tool or tool is edit_file_tool
            if not native_file:
                if node.role == "shared_writer":
                    raise CandidateWriteRefused(
                        "shared writers use native read_file/edit_file/write_file only; "
                        "report required commands to the primary for execution after all writers exit")
                if pending:
                    raise self._wait_required(context,
                        "the primary must wait for all shared writers before commands or other mutations")
                yield
                return
            root = worker.workspace_for_node(context.node_id).resolve()
            if Path(context.cwd).resolve() != root:
                raise CandidateWriteRefused("shared tool cwd does not match the live parent directory")
            path = arguments.get("path")
            if (not isinstance(path, str) or not path or path.startswith(("/", "\\"))
                or any(c in path for c in "\\:*?<>|\"") or any(ord(c) < 32 for c in path)
                or any(PureWindowsPath(p).is_reserved() for p in path.split("/"))
                or any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in path.split("/"))
                or any(p.lower() == ".git" for p in path.split("/"))):
                raise CandidateWriteRefused("shared writes require a safe workspace-relative path")
            target = root / path
            if is_unsafe_stored_path(target, boundary=root, reject_hardlinks=True):
                raise CandidateWriteRefused("shared write path contains a filesystem alias")
            if _key(target.resolve().relative_to(root).as_posix()) != _key(path):
                raise CandidateWriteRefused("shared write path must use its canonical relative spelling")
            if node.role == "shared_writer":
                if not _contains(node.write_scope, path):
                    raise CandidateWriteRefused(f"{path!r} is outside this worker's write_scope")
            elif any(_contains(r["definition"]["write_scope"], path) for r in pending):
                raise self._wait_required(context, f"{path!r} is assigned to a shared writer; wait for scope release")
            before = target.read_bytes() if target.exists() else None
            from minicode.native_read_set import current_native_read_set
            reads = current_native_read_set()
            # A fresh observation is required once shared writing starts. Tools'
            # own implementation reads cannot retroactively authorize an edit.
            if records:
                observed = {} if reads is None else {_key(p): h for p, h in reads.read_hashes().items()}
                digest = hashlib.sha256(before).hexdigest() if before is not None else ""
                if (before is not None and observed.get(_key(path)) != digest
                    or before is None and observed.get(_key(path)) not in {None, ""}):
                    raise CandidateWriteRefused(f"{path!r} changed or was not read; read_file again before writing")
            self._live(context)
            try:
                yield
            finally:
                after = target.read_bytes() if target.exists() else None
                if before != after:
                    self.changes.setdefault((context.node_id, context.attempt_id), set()).add(path)
                    self.hashes.setdefault((context.node_id, context.attempt_id), {})[path] = (
                        hashlib.sha256(after).hexdigest() if after is not None else None)
                if reads is not None and after is not None:
                    reads.record_read(target, after)
