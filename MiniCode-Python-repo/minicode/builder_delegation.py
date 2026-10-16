"""Production binding for asynchronous Builder candidates.

What this is
------------
The primary Agent's *native* path to parallel, WRITABLE work: a bound gateway
behind six tools (``spawn_builders``, ``poll_task_results``, ``wait_dependencies``,
``select_builder_candidate``, ``accept_builder_patch``, ``cancel_task``) plus the Runtime-side lifecycle that
gives every Builder its own isolated candidate, exports a native patch from it and
applies an accepted patch into the primary candidate P.

Ownership is unchanged from the scheduler's model: exactly one live writer per
candidate, the Runtime briefly owning P's short mutation lock for the apply, and
the final unique integration still going through the existing
submit/check/verify/integrate chain. A Builder never writes the user's target
directory.

What it cannot prove
--------------------
An isolated directory is not an OS sandbox, and this module grants no authority by
itself: every call re-reads live ownership, cancellation and revision facts. A
patch produced here is a proposal; it becomes a change to P only after the primary
accepts it and the Runtime applies it under CAS.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicode.candidate_ownership import CandidateBusy
from minicode.candidate_patches import (
    CandidatePatch,
    DependencyVector,
    NativeReadWriteSet,
    PatchConflict,
)
from minicode.dependency_waits import (
    ARTIFACT_VALIDATED,
    PATCH_APPLIED_AND_CHECKED,
    TERMINAL_OBSERVED,
)
from minicode.native_read_set import NativeReadSet
from minicode.path_safety import is_unsafe_stored_path
from minicode.runtime_protocol import NodeState, ToolEffect
from minicode.workspace_provider import (
    TemporaryWorkspaceProvider,
    WorkspaceConflict,
    WorkspaceHandle,
    snapshot_revision,
    workspace_link_aliases,
    workspace_snapshot,
)

BUILDER_TOOL_NAMES = ("spawn_builders", "poll_task_results", "wait_dependencies",
                      "accept_builder_patch", "cancel_task", "select_builder_candidate")
PATCH_STATE_PRODUCED = "produced"
PATCH_STATE_APPLIED = "applied"
# The single file a zero-model capability preparation writes inside its own
# candidate. It is removed before the receipt is returned and never published as
# a patch, so a preparation cannot be mistaken for produced work.
_PREPARE_PROBE_NAME = ".minicode-builder-capability-probe"
_PREPARE_PROBE_BODY = "minicode builder capability preparation"

_MAX_PATCH_FILES = 256
_MAX_PATCH_BYTES = 32 * 1024 * 1024


@dataclass(slots=True)
class StagedPublication:
    """One verified patch, staged outside the target and ready to publish.

    Staging is what separates "the producer still holds the accepted bytes" from
    "the target now holds them". Every version here was re-hashed against the
    accepted patch content before the target was touched, so a conflict found at
    this point costs nothing; a conflict found later is an uncertain application.
    """

    patch_id: str
    container: Path
    content: Mapping[str, Path]
    published: Mapping[str, str | None]
    patch_sha256: str


@dataclass(frozen=True, slots=True)
class BuilderRequest:
    """One bounded Builder task as the primary declared it."""

    idempotency_key: str
    objective: str
    depends_on: tuple[str, ...] = ()
    predicate: str = PATCH_APPLIED_AND_CHECKED
    required: bool = True
    acceptance_criteria: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    alternative_group: str | None = None

    def to_spawn_spec(self) -> dict[str, Any]:
        return {"idempotency_key": self.idempotency_key, "objective": self.objective,
                "depends_on": list(self.depends_on), "predicate": self.predicate,
                "required": self.required, "acceptance_criteria": list(self.acceptance_criteria),
                "write_scope": list(self.write_scope), "alternative_group": self.alternative_group}


def validate_builder_request(payload: Mapping[str, Any]) -> BuilderRequest:
    if not isinstance(payload, Mapping):
        raise ValueError("a builder task must be an object")
    key = payload.get("idempotency_key") or payload.get("key")
    objective = payload.get("objective") or payload.get("prompt")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("a builder task needs a non-blank idempotency_key")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("a builder task needs a non-blank objective")
    depends = payload.get("depends_on") or ()
    if isinstance(depends, str) or not isinstance(depends, (list, tuple)):
        raise ValueError("depends_on must be an array of task ids")
    predicate = str(payload.get("predicate") or PATCH_APPLIED_AND_CHECKED)
    if predicate not in {ARTIFACT_VALIDATED, PATCH_APPLIED_AND_CHECKED, TERMINAL_OBSERVED}:
        raise ValueError("unknown dependency predicate")
    criteria = payload.get("acceptance_criteria") or ()
    if isinstance(criteria, str) or not isinstance(criteria, (list, tuple)):
        raise ValueError("acceptance_criteria must be an array of strings")
    scope = payload.get("write_scope", payload.get("allowed_paths", ()))
    if not isinstance(scope, (list, tuple)):
        raise ValueError("write_scope must be an array of relative paths")
    return BuilderRequest(idempotency_key=key.strip(), objective=objective.strip(),
                          depends_on=tuple(str(item) for item in depends),
                          predicate=predicate,
                          required=bool(payload.get("required", True)),
                          acceptance_criteria=tuple(str(item) for item in criteria), write_scope=tuple(scope),
                          alternative_group=payload.get("alternative_group"))


def export_candidate_patch(
    handle: WorkspaceHandle, *,
    patch_id: str,
    owner_node_id: str,
    owner_attempt_id: str,
    owner_generation: int,
    owner_fencing_token: int,
    plan_version: int,
    contract_revision: int,
    target_candidate_id: str,
    producer_cancel_epoch: int = 0,
    dependency_vector: DependencyVector | None = None,
    checks: Mapping[str, Any] | None = None,
    native_read_set: "NativeReadSet | None" = None,
) -> CandidatePatch:
    """Turn an isolated candidate's real bytes into an exact, hashable patch.

    Only regular files inside the candidate are read, the same path-safety rules
    the rest of the Runtime uses are applied, and the changed set is a real
    before/after diff rather than a claimed one.

    The exported patch is IMMUTABLE: it records each changed path's new content
    hash together with the content hash the path had at the Builder's origin base,
    and the two are bound to ``patch_sha256``. Applying it never re-reads the still
    mutable Builder directory to decide what "the patch" is.
    """
    if not handle.isolated or handle.closed or handle.integrated:
        raise WorkspaceConflict("only an open unintegrated isolated candidate can be exported")
    if workspace_link_aliases(handle.workspace):
        raise WorkspaceConflict("a candidate with filesystem aliases cannot be exported")
    current = workspace_snapshot(handle.workspace)
    revision = snapshot_revision(current)
    changed = sorted(name for name in current.keys() | handle.base_hashes.keys()
                     if current.get(name) != handle.base_hashes.get(name))
    if not changed:
        raise WorkspaceConflict("a Builder produced no changes; there is nothing to export")
    if len(changed) > _MAX_PATCH_FILES:
        raise WorkspaceConflict("candidate exceeds the patch file limit")
    payload: dict[str, str | None] = {}
    base_payload: dict[str, str | None] = {}
    total = 0
    for name in changed:
        path = handle.workspace / name
        if is_unsafe_stored_path(path, boundary=handle.workspace, reject_hardlinks=True):
            raise WorkspaceConflict("a candidate file escapes or aliases the candidate")
        base_payload[name] = handle.base_hashes.get(name)
        digest = current.get(name)
        if digest is None:
            payload[name] = None
            continue
        size = path.stat().st_size
        total += size
        if total > _MAX_PATCH_BYTES:
            raise WorkspaceConflict("candidate exceeds the patch byte limit")
        with path.open("rb") as stream:
            data = stream.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise WorkspaceConflict("candidate changed during patch export")
        payload[name] = digest
    if workspace_snapshot(handle.workspace) != current:
        raise WorkspaceConflict("candidate changed during patch export")
    # The read set is what the Builder's tools REALLY observed. Deriving it from
    # the changed paths would be circular -- it would claim the Builder read
    # exactly the files it wrote -- so an absent observation is recorded as an
    # incomplete read set rather than as "read nothing".
    writes = tuple(changed)
    if native_read_set is None:
        observations = NativeReadSet(root=handle.workspace)
        observations.record_dynamic("no-native-read-set-captured")
    else:
        observations = native_read_set
    reads = dict(observations.read_hashes())
    # A written path was necessarily observed by the tool that wrote it, so it is
    # part of the write set rather than an unknown read; anything else the Builder
    # touched without an observation stays in ``omissions``.
    omissions = tuple(item for item in observations.omissions if item not in reads)
    complete = observations.complete
    return CandidatePatch(
        patch_id=patch_id,
        source_candidate_id=f"candidate-{owner_node_id}",
        target_candidate_id=target_candidate_id,
        owner_node_id=owner_node_id,
        owner_attempt_id=owner_attempt_id,
        owner_generation=max(1, int(owner_generation)),
        owner_fencing_token=max(1, int(owner_fencing_token)),
        plan_version=plan_version,
        contract_revision=contract_revision,
        origin_base_revision=snapshot_revision(handle.base_hashes),
        current_revision=revision,
        patch_sha256=_patch_digest(payload),
        changed_paths=writes,
        producer_cancel_epoch=max(0, int(producer_cancel_epoch)),
        content_hashes={name: digest for name, digest in payload.items()},
        base_hashes={name: digest for name, digest in base_payload.items()},
        read_write_set=NativeReadWriteSet(
            read_hashes=reads, read_directories=observations.directories(),
            directory_hashes=observations.directory_hashes(),
            write_set=writes, complete=complete, omissions=omissions),
        dependency_vector=dependency_vector or DependencyVector(),
        checks=dict(checks or {}),
    )


def _patch_digest(payload: Mapping[str, str | None]) -> str:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_patch_content(
    patch: CandidatePatch, *, source_root: Path | None,
) -> dict[str, str | None]:
    """Re-hash the producer's real bytes and return the payload that was exported.

    This is the WRITE-FREE half of an apply. It either reproduces the accepted
    ``patch_sha256`` exactly -- every changed path present with the accepted digest
    and every deletion really absent -- or it raises without any target byte having
    moved. A producer that changed after the export is refused here, before the
    target is touched, instead of part-way through the copy.
    """
    payload: dict[str, str | None] = {}
    if source_root is None:
        raise PatchConflict(
            "the patch producer is no longer available, so its exact bytes cannot be verified")
    source_root = Path(source_root)
    for name in patch.changed_paths:
        expected = patch.content_hashes.get(name)
        source = source_root / name
        if is_unsafe_stored_path(source, boundary=source_root, reject_hardlinks=True):
            raise PatchConflict(f"the producer path for {name} escapes its candidate")
        if expected is None:
            if source.is_file():
                raise PatchConflict(
                    f"the patch deletes {name} but the producer still has it")
            payload[name] = None
            continue
        if not source.is_file():
            raise PatchConflict(
                f"the patch carries {name} but the producer no longer has that file")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise PatchConflict(
                f"the producer's bytes for {name} changed after the patch was exported")
        payload[name] = expected
    observed = _patch_digest(payload)
    if observed != patch.patch_sha256:
        raise PatchConflict(
            "the producer's bytes do not reproduce the accepted patch hash")
    return payload


def _atomic_publish(source: Path, target: Path) -> None:
    """Replace ``target`` with ``source``'s bytes; the target sees all or nothing."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.minicode-", suffix=".tmp", dir=str(target.parent))
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def apply_candidate_bytes(
    handle: WorkspaceHandle, patch: CandidatePatch, *, target_root: Path,
) -> str:
    """Copy an exported candidate's changed bytes into the target candidate.

    Order is the safety property: the producer's bytes are re-hashed against the
    accepted patch content FIRST, the resulting versions are staged OUTSIDE the
    target, and only then is anything published into the target. A producer that
    moved after acceptance, or a file that changed while being staged, is refused
    with the target byte-for-byte unchanged -- never left holding content that was
    not approved.

    Returns the composite hash of what actually landed, which the caller compares
    against the accepted patch hash before any success is recorded. This is the
    only place a Builder's bytes reach P, and the caller holds P's short mutation
    lock for the whole call.
    """
    target_root = Path(target_root)
    staged = stage_candidate_bytes(handle, patch)
    return publish_staged_bytes(staged, target_root=target_root)


def stage_candidate_bytes(
    handle: WorkspaceHandle, patch: CandidatePatch, *, staging_root: Path | None = None,
) -> "StagedPublication":
    """Verify a patch against its producer and stage the exact versions to publish."""
    verified = validate_patch_content(patch, source_root=handle.workspace)
    container = Path(staging_root) if staging_root is not None else Path(
        tempfile.mkdtemp(prefix="minicode-patch-staging-"))
    container.mkdir(parents=True, exist_ok=True)
    content: dict[str, Path] = {}
    published: dict[str, str | None] = {}
    try:
        for name in patch.changed_paths:
            expected = verified[name]
            published[name] = expected
            if expected is None:
                continue
            source = handle.workspace / name
            target = container / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise PatchConflict(
                    f"the staged version of {name} does not match the accepted patch")
            content[name] = target
    except BaseException:
        shutil.rmtree(container, ignore_errors=True)
        raise
    observed = _patch_digest(published)
    if observed != patch.patch_sha256:
        shutil.rmtree(container, ignore_errors=True)
        raise PatchConflict("the staged patch does not reproduce the accepted patch hash")
    return StagedPublication(patch_id=patch.patch_id, container=container,
                             content=content, published=published,
                             patch_sha256=observed)


def publish_staged_bytes(staged: "StagedPublication", *, target_root: Path) -> str:
    """Move a staged version into the target and report what really landed.

    Each path is replaced atomically, so no reader ever observes a half-written
    file. A multi-file publication is deliberately NOT claimed to be atomic across
    files: a failure part-way is reported as a conflict and the caller records an
    uncertain application, because the true state of the remaining paths is only
    knowable by re-reading them.
    """
    target_root = Path(target_root)
    landed: dict[str, str | None] = {}
    try:
        for name in sorted(staged.published):
            target = target_root / name
            if is_unsafe_stored_path(target, boundary=target_root, reject_hardlinks=True):
                raise WorkspaceConflict("the patch target escapes the target candidate")
            if staged.published[name] is None:
                if target.exists():
                    target.unlink()
                landed[name] = None
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_publish(staged.content[name], target)
            landed[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    finally:
        shutil.rmtree(staged.container, ignore_errors=True)
    observed = _patch_digest(landed)
    if observed != staged.patch_sha256:
        raise PatchConflict("the bytes copied into the target do not reproduce the patch hash")
    return observed


@dataclass(slots=True)
class BuilderLifecycle:
    """Runtime-side per-Builder state: its own isolated candidate and patch."""

    node_id: str
    candidate_id: str
    handle: WorkspaceHandle | None = None
    patch: CandidatePatch | None = None
    released: bool = False
    provider: TemporaryWorkspaceProvider | None = None
    dependency_vector: DependencyVector | None = None

    def close(self) -> None:
        if self.released:
            return
        self.released = True
        if self.handle is not None and self.provider is not None and not self.handle.integrated:
            try:
                self.provider.cleanup(self.handle)
            except Exception:  # noqa: BLE001 - cleanup failure must not hide the result
                pass


class BuilderGateway:
    """The primary's native handle on parallel writable work.

    Every method re-reads live ownership and cancellation from the Scheduler; none
    of them accepts a capability from the model's own text. ``spawn`` returns as
    soon as the tasks are REGISTERED and never claims they ran.
    """

    def __init__(
        self,
        *,
        scheduler: Any,
        parent_node_id: str,
        parent_attempt_id: str,
        resolve_attempt: Callable[[], str],
        source: Path,
        provider: TemporaryWorkspaceProvider,
        target_candidate_id: str = "primary",
        target_root: Callable[[], Path | None] | None = None,
        combination_check: Callable[[CandidatePatch], Mapping[str, Any]] | None = None,
        owner_token_factory: Callable[[str], str] | None = None,
        recover_candidate=None,
    ) -> None:
        self.scheduler = scheduler
        self.parent_node_id = parent_node_id
        self.parent_attempt_id = parent_attempt_id
        self._resolve_attempt = resolve_attempt
        self.source = Path(source)
        self.provider = provider
        self.target_candidate_id = target_candidate_id
        self._target_root = target_root
        self._combination_check = combination_check
        self._owner_token_factory = owner_token_factory or (
            lambda node_id: f"builder:{node_id}")
        self.builders: dict[str, BuilderLifecycle] = {}
        self.patches: dict[str, str] = {}   # patch_id -> builder node id
        self._base_snapshots: dict[str, dict[str, str]] = {}
        self._suspensions: dict[tuple[str, str], dict[str, Any]] = {}
        # The Runtime reads P's real bytes at the write boundary. Binding that here
        # is what lets accept/apply decide on native facts instead of a caller's
        # revision string.
        scheduler.bind_candidate_evidence(revision=self._revision, changes=self._changes,
            root=lambda candidate_id: self._target_root_path() if candidate_id == self.target_candidate_id else None)
        for node in scheduler.reducer.current_plan.nodes:
            result = scheduler.reducer.node_result(node.node_id)
            if not node.node_id.startswith("builder-") or result is None or result.status.value != "succeeded":
                continue
            if recover_candidate is None:
                raise WorkspaceConflict("restored Builder requires its Runtime-owned candidate descriptor")
            handle, descriptor = recover_candidate(result)
            lifecycle = BuilderLifecycle(node.node_id, f"candidate-{node.node_id}", handle=handle,
                provider=self.provider, dependency_vector=DependencyVector.from_dict(descriptor.get("dependency_vector")))
            self.builders[node.node_id] = lifecycle
            self._base_snapshots[node.node_id] = dict(handle.base_hashes)
            record = scheduler.reducer.patch_records().get(f"patch-{node.node_id}", {})
            if record.get("patch"):
                lifecycle.patch = CandidatePatch.from_dict(record["patch"])
                self.patches[lifecycle.patch.patch_id] = node.node_id

    # ---- native evidence -----------------------------------------------

    def _target_root_path(self) -> Path | None:
        """The primary candidate P, or ``None`` when there is no separate P.

        A patch is applied into P and nowhere else. When the target resolver reports
        no live primary candidate -- a fully synchronous primary is working directly
        in the user's directory -- there is nothing to apply into, so every native
        read below reports "unavailable" and the write boundary refuses instead of
        writing the user's target directory.
        """
        if self._target_root is None:
            return None
        resolved = self._target_root()
        return Path(resolved) if resolved is not None else None

    def _revision(self, candidate_id: str) -> str | None:
        if candidate_id == self.target_candidate_id:
            target = self._target_root_path()
            return None if target is None else snapshot_revision(workspace_snapshot(target))
        lifecycle = self.builders.get(candidate_id.removeprefix("candidate-"))
        if lifecycle is not None and lifecycle.handle is not None:
            return snapshot_revision(workspace_snapshot(lifecycle.handle.workspace))
        return None

    def _changes(self, source_candidate_id: str, target_candidate_id: str):
        """What really differs in the target since this Builder's origin base."""
        if target_candidate_id != self.target_candidate_id:
            return None
        base = self._base_snapshots.get(source_candidate_id.removeprefix("candidate-"))
        if base is None:
            return None
        target = self._target_root_path()
        if target is None:
            return None
        current = workspace_snapshot(target)
        return {name: current.get(name, "")
                for name in sorted(base.keys() | current.keys())
                if base.get(name) != current.get(name)}

    # ---- lifecycle used by the Scheduler -------------------------------

    def _capture_authority(self, node_id: str):
        """Read a small authority/dependency pin while holding publication_guard."""
        reducer = self.scheduler.reducer
        if self.target_candidate_id in self.scheduler.candidates.snapshot().get("quarantined", ()):
            raise WorkspaceConflict("Builder snapshot source is quarantined")
        if (self.scheduler.cancellation_event.is_set()
                or reducer.node_cancel_epoch(node_id)
                or reducer.node_cancel_epoch(self.parent_node_id)):
            raise WorkspaceConflict("Builder snapshot canceled before publication")
        node = self.scheduler._node_definition(node_id)
        if reducer.node_state(node_id) in {NodeState.CANCELED, NodeState.SUPERSEDED, NodeState.FAILED_TERMINAL}:
            raise WorkspaceConflict("Builder snapshot has no live owner")
        attempt = reducer.current_attempt(node_id)
        vector = self.scheduler._builder_dependency_vector(node)
        for dependency in node.dependencies:
            predicate = reducer.dependency_predicates_for(node_id).get(dependency, "patch_applied_and_checked")
            if not reducer.dependency_edge_satisfied(dependency, predicate):
                raise WorkspaceConflict("dependency no longer satisfies its registered predicate")
        pin = (reducer.current_plan.plan_version, reducer.current_plan.contract_revision,
               attempt.attempt_id if attempt else None,
               self.scheduler._fencing_tokens.get(node_id), node, vector)
        return pin, vector

    def open_candidate(self, *, node_id: str, attempt_id: str) -> BuilderLifecycle:
        lifecycle = self.builders.get(node_id)
        if lifecycle is None:
            lifecycle = BuilderLifecycle(node_id=node_id,
                                         candidate_id=f"candidate-{node_id}",
                                         provider=self.provider)
            self.builders[node_id] = lifecycle
        if lifecycle.handle is None or lifecycle.handle.closed:
            # Keep P stable, but never hold the run-wide publication lock over
            # filesystem scans/copies: cancellation, leases and UI need it too.
            with self._snapshot_mutation(node_id):
                # Resolve after admission: a waiting snapshot must not copy the
                # previous activation's workspace after it has been retired.
                target = self._target_root_path()
                if target is None:
                    raise WorkspaceConflict("opening a Builder requires a live primary candidate")
                if lifecycle.handle is not None and not lifecycle.handle.closed:
                    return lifecycle
                reducer = self.scheduler.reducer
                with reducer.publication_guard():
                    authority, dependency_vector = self._capture_authority(node_id)
                    node = self.scheduler._node_definition(node_id)
                    dependency_hashes = []
                    for dependency in node.dependencies:
                        predicate = reducer.dependency_predicates_for(node_id).get(dependency, "patch_applied_and_checked")
                        if predicate == "patch_applied_and_checked":
                            record = reducer.patch_records().get(f"patch-{dependency}", {})
                            hashes = (record.get("patch") or {}).get("content_hashes")
                            if not hashes:
                                raise WorkspaceConflict("dependency patch bytes unavailable before candidate capture")
                            dependency_hashes.append(dict(hashes))
                baseline = workspace_snapshot(target)
                if any(baseline.get(path) != digest for hashes in dependency_hashes for path, digest in hashes.items()):
                    raise WorkspaceConflict("dependency patch bytes changed before candidate capture")
                handle = self.provider.create(
                    target, owner_token=self._owner_token_factory(f"{node_id}:{attempt_id}"))
                try:
                    if baseline != handle.base_hashes or baseline != workspace_snapshot(handle.workspace) \
                            or baseline != workspace_snapshot(target):
                        raise WorkspaceConflict("primary candidate changed while creating the Builder snapshot")
                    with reducer.publication_guard():
                        current_authority, _ = self._capture_authority(node_id)
                        if current_authority != authority or self._target_root_path() != target:
                            raise WorkspaceConflict("Builder authority changed while creating its snapshot")
                        lifecycle.handle = handle
                        self._base_snapshots[node_id] = dict(baseline)
                        lifecycle.dependency_vector = dependency_vector
                        lifecycle.released = False
                except BaseException:
                    self.provider.cleanup(handle)
                    raise
        return lifecycle

    @contextmanager
    def _snapshot_mutation(self, node_id: str):
        """Serialize preparatory copies without treating normal contention as failure.

        Only mutex contention is retried. Ownership, cancellation and dependency
        failures still refuse the snapshot, and no model or write is replayed.
        """
        from minicode.workspace_provider import ASYNC_PREPARATION_SECONDS
        deadline = time.monotonic() + ASYNC_PREPARATION_SECONDS
        while True:
            with self.scheduler.reducer.publication_guard():
                self._capture_authority(node_id)
            guard = self.scheduler.candidates.mutation(
                candidate_id=self.target_candidate_id, as_runtime=True, timeout=0.1)
            try:
                guard.__enter__()
            except CandidateBusy:
                if time.monotonic() >= deadline:
                    raise WorkspaceConflict("Builder candidate preparation lock wait timed out; no model was started")
                continue
            break
        try:
            yield
        finally:
            guard.__exit__(None, None, None)

    def publish_patch(self, *, node_id: str, patch: CandidatePatch) -> dict[str, Any]:
        """Persist a produced patch so the primary can find it by id."""
        lifecycle = self.open_candidate(node_id=node_id, attempt_id=patch.owner_attempt_id)
        lifecycle.patch = patch
        self.patches[patch.patch_id] = node_id
        record = {
            "schema": "minicode-builder-patch-v1", "state": PATCH_STATE_PRODUCED,
            "patch_id": patch.patch_id, "node_id": node_id,
            "candidate_id": lifecycle.candidate_id,
            "patch_sha256": patch.patch_sha256,
            "origin_base_revision": patch.origin_base_revision,
            "candidate_revision": patch.current_revision,
            "changed_paths": list(patch.changed_paths),
            "patch": patch.to_dict(),
            "grants_write_right": False, "applied": False, "verified": False,
            "integrated": False,
        }
        self.scheduler.reducer.record_patch_record(patch.patch_id, record)
        return record

    # ---- primary-facing operations -------------------------------------

    def spawn(self, tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        requests = [validate_builder_request(item) for item in tasks]
        if not requests:
            raise ValueError("spawn_builders needs at least one task")
        keys = [item.idempotency_key for item in requests]
        if len(set(keys)) != len(keys):
            raise ValueError("builder idempotency keys must be unique within one call")
        # The live attempt is read from the Runtime at call time. A captured id
        # would let a stale activation register Builders after it lost the lease.
        attempt_id = self.parent_attempt_id or self._resolve_attempt()
        if not attempt_id:
            raise ValueError("spawn_builders requires a live primary attempt")
        return self.scheduler.spawn_builders(
            parent_node_id=self.parent_node_id,
            parent_attempt_id=attempt_id,
            tasks=[item.to_spawn_spec() for item in requests],
        )

    def poll(self, *, task_ids: Sequence[str] | None = None, cursor: int = 0) -> dict[str, Any]:
        return self.scheduler.poll_task_results(task_ids=task_ids, cursor=cursor)

    def review_patch(self, *, patch_id: str, path: str | None = None,
                     offset: int = 0, max_chars: int = 12000) -> dict[str, Any]:
        """Read bounded, hash-checked producer content without accepting a patch.

        Candidate IDs are not filesystem paths. Only the current primary may
        inspect a declared changed file from its own settled Builder's export.
        """
        if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 16000:
            raise ValueError("offset must be non-negative; max_chars must be 1..16000")
        with self.scheduler.reducer.publication_guard():
            if self.scheduler._live_primary(self.parent_node_id, self.parent_attempt_id or self._resolve_attempt()) is not None:
                raise PatchConflict("patch review requires the live owning primary")
            record = self.scheduler.reducer.patch_records().get(patch_id)
            if not record or "patch" not in record:
                task_id = next((node for node in self._required_builders() if patch_id == "patch-" + node), None)
                if task_id is None:
                    self._patch(patch_id)  # Unknown identifiers remain errors.
                state = self.scheduler.reducer.node_state(task_id)
                result = self.scheduler.reducer.node_result(task_id)
                unavailable = state in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.CANCELED, NodeState.SUPERSEDED}
                return {"requested_patch_id": patch_id, "patch_id": None, "task_id": task_id,
                    "status": "unavailable" if unavailable else "pending",
                    "task_state": state.value, "ready_for_review": False,
                    "failure": result.failure.to_dict() if result is not None and result.failure else None,
                    "message": "No native patch exists yet. Poll the registered task_id or wait_dependencies; "
                               "do not accept or recover an inferred patch ID.",
                    "accepted_by_this_read": False, "applied_by_this_read": False,
                    "counts_as_final_acceptance": False}
        with self._review_mutation() as ready:
            if not ready:
                return {"patch_id": patch_id, "status": "candidate_busy", "ready_for_review": False,
                    "message": "Candidate preparation or mutation is in progress. Wait for existing work, then review again.",
                    "accepted_by_this_read": False, "applied_by_this_read": False,
                    "counts_as_final_acceptance": False}
            return self._review_ready_patch(patch_id=patch_id, path=path, offset=offset, max_chars=max_chars)

    @contextmanager
    def _review_mutation(self):
        guard = self.scheduler.candidates.mutation(candidate_id=self.target_candidate_id, as_runtime=True)
        try:
            guard.__enter__()
        except CandidateBusy:
            yield False
            return
        try:
            yield True
        finally:
            guard.__exit__(None, None, None)

    def _review_ready_patch(self, *, patch_id, path, offset, max_chars):
        # The caller owns the candidate guard. Recheck authority after admission;
        # a pending/busy observation never bypasses settled-producer validation.
        with self.scheduler.reducer.publication_guard():
            if self.scheduler._live_primary(self.parent_node_id, self.parent_attempt_id or self._resolve_attempt()) is not None:
                raise PatchConflict("patch review requires the live owning primary")
            patch = self._patch(patch_id)
            lifecycle = self.builders.get(patch.owner_node_id)
            facts = self.scheduler._authoritative_facts(patch)
            if (lifecycle is None or lifecycle.handle is None or lifecycle.handle.closed
                or patch.target_candidate_id != self.target_candidate_id
                or self.scheduler.reducer.node_state(patch.owner_node_id) is not NodeState.SUCCEEDED
                or not facts.facts_complete or facts.canceled or facts.producer_canceled
                or facts.producer_attempt_id != patch.owner_attempt_id
                or facts.producer_generation != patch.owner_generation
                or facts.producer_fencing_token != patch.owner_fencing_token
                or facts.producer_cancel_epoch != patch.producer_cancel_epoch
                or facts.plan_version != patch.plan_version or facts.contract_revision != patch.contract_revision):
                raise PatchConflict("patch review lost the settled producer or its current authority")
            validate_patch_content(patch, source_root=lifecycle.handle.workspace)
            selected = path if path is not None else patch.changed_paths[0]
            if selected not in patch.changed_paths:
                raise PatchConflict("review path must be an exact declared changed path")
            expected = patch.content_hashes[selected]
            text = ""
            if expected is not None:
                source = lifecycle.handle.workspace / selected
                raw = source.read_bytes()
                if (hashlib.sha256(raw).hexdigest() != expected
                    or is_unsafe_stored_path(source, boundary=lifecycle.handle.workspace, reject_hardlinks=True)):
                    raise PatchConflict("producer content changed during review")
                text = raw.decode("utf-8")
            if offset > len(text):
                raise ValueError("offset exceeds this file's character length")
            end = min(len(text), offset + max_chars)
            return {"patch_id": patch_id, "patch_sha256": patch.patch_sha256,
                "producer_revision": patch.current_revision, "path": selected,
                "changed_paths": list(patch.changed_paths), "base_sha256": patch.base_hashes.get(selected),
                "content_sha256": expected, "deleted": expected is None, "encoding": "utf-8",
                "offset": offset, "end": end, "total_chars": len(text), "truncated": end < len(text),
                "content": text[offset:end], "untrusted_source_content": True,
                "accepted_by_this_read": False, "applied_by_this_read": False,
                "counts_as_final_acceptance": False}

    def select(self, *, task_id: str, reason: str, operation_id: str) -> dict[str, Any]:
        return self.scheduler.select_builder_candidate(task_id=task_id, reason=reason,
            operation_id=operation_id, parent_attempt_id=self.parent_attempt_id or self._resolve_attempt())

    def wait(self, *, edges: Sequence[Mapping[str, Any]], mode: str = "all",
             failure_policy: str = "block", timeout_seconds: float | None = None,
             wait_id: str | None = None) -> dict[str, Any]:
        outcome = self.scheduler.wait_dependencies(
            owner_node_id=self.parent_node_id, edges=edges, mode=mode,
            failure_policy=failure_policy, timeout_seconds=timeout_seconds if timeout_seconds is not None else 120.0,
            wait_id=wait_id, timeout_policy="return")
        if outcome.get("registered") and outcome["decision"].get("status") == "waiting":
            self._suspensions[(self.parent_node_id, self._resolve_attempt())] = outcome["wait"]
        return outcome

    def suspension_for(self, node_id: str, attempt_id: str) -> dict[str, Any]:
        wait = self._suspensions.pop((node_id, attempt_id), None)
        if wait is None:
            raise WorkspaceConflict("dependency suspension has no wait registered by this activation")
        return dict(wait)

    def accept(self, *, patch_id: str, operation_id: str) -> dict[str, Any]:
        patch = self._patch(patch_id)
        return self.scheduler.accept_patch(patch, operation_id=operation_id)

    def recover(self, *, patch_id: str) -> dict[str, Any]:
        """Register at most one fresh generation for this rejected, unmerged patch.

        No old observations, output bytes or permissions are promoted. The new
        Builder starts from P's current snapshot and produces its own evidence.
        """
        scheduler, reducer = self.scheduler, self.scheduler.reducer
        attempt_id = self.parent_attempt_id or self._resolve_attempt()
        with scheduler._dispatch_lock, reducer.publication_guard():
            refusal = scheduler._live_primary(self.parent_node_id, attempt_id)
            if refusal or scheduler._budget_exhausted():
                raise PatchConflict(refusal or "remaining run budget is unavailable")
            patch = self._patch(patch_id)
            for task_id, record in reducer.builder_recoveries().items():
                if record["patch_id"] == patch_id:
                    return {"task_id": task_id, "patch_id": "patch-" + task_id, "reused": True,
                        "replaces_task_id": patch.owner_node_id, "registration_is_not_execution": True}
            from minicode.candidate_patches import decide_promotion
            decision = decide_promotion(patch, scheduler._authoritative_facts(patch))
            if decision.accepted or not decision.requires_rebase:
                raise PatchConflict("recovery requires a rejected patch with new native work, not an applicable patch")
            node = scheduler._node_definition(patch.owner_node_id)
            admission = reducer.admit_consultation_budget(self.parent_node_id, attempt_id, (node.objective,),
                shared_pool=reducer.shared_consultation_budget)
            if not admission["allowed"]:
                raise PatchConflict(admission["reason"])
            predicates = reducer.dependency_predicates_for(node.node_id)
            if len(set(predicates.values())) > 1:
                raise PatchConflict("mixed dependency predicates require an explicit replan")
            result = scheduler._register_builder({
                "idempotency_key": "recover-" + patch_id, "objective": node.objective,
                "depends_on": list(node.dependencies), "predicate": next(iter(predicates.values()), PATCH_APPLIED_AND_CHECKED),
                "acceptance_criteria": list(node.acceptance_criteria), "write_scope": list(node.write_scope),
                "alternative_group": node.alternative_group,
            }, parent_node_id=self.parent_node_id, parent_attempt_id=attempt_id,
                known={n.node_id for n in reducer.current_plan.nodes}, builder_recovery={
                    "replaces_task_id": node.node_id, "patch_id": patch_id, "patch_sha256": patch.patch_sha256})
            if result.get("error"):
                raise PatchConflict(result["error"])
            scheduler._dispatch_wake.set()
            return {**result, "patch_id": "patch-" + result["task_id"], "replaces_task_id": node.node_id,
                "reused": False, "registration_is_not_execution": True, "budget": admission}

    def apply(self, *, patch_id: str, operation_id: str) -> dict[str, Any]:
        """Apply an accepted Builder patch into P, then run the combination check.

        The target is resolved NOW, at the write boundary, from the live primary
        candidate -- never from a path captured earlier and never from the user's
        target directory. When the primary has no separate candidate there is
        nothing to apply into, and the apply is refused before any byte moves.
        """
        patch = self._patch(patch_id)
        node_id = self.patches.get(patch_id)
        lifecycle = self.builders.get(node_id or "")
        if lifecycle is None or lifecycle.handle is None:
            raise PatchConflict("the Builder candidate for this patch is no longer open")
        target_root = self._target_root_path()
        if target_root is None:
            raise PatchConflict(
                "no live primary candidate is bound; a Builder patch is applied into "
                "the primary candidate and never into the user's target directory")

        def write() -> str:
            return apply_candidate_bytes(lifecycle.handle, patch, target_root=target_root)

        result = self.scheduler.apply_patch(patch, operation_id=operation_id, apply=write)
        if not result.get("receipt") or not result["receipt"].get("applied"):
            return result
        if result.get("replayed"):
            with self.scheduler.candidates.mutation(candidate_id=self.target_candidate_id, as_runtime=True, timeout=5), self.scheduler.reducer.publication_guard():
                previous = self.scheduler.patch_ledger.record(patch_id) or {}
                checks = previous.get("checks") or {}
                snapshot = workspace_snapshot(target_root)
                revision = snapshot_revision(snapshot)
                if (self._applied_patch_current(patch, snapshot) and checks
                    and all(check.get("checked_revision") == revision for check in checks.values())):
                    return {**result, "checks": checks, "checks_reused": True}
        checks = self._combination_checks(patch)
        refreshed = {patch_id: node_id}
        with self.scheduler.candidates.mutation(candidate_id=self.target_candidate_id, as_runtime=True, timeout=5), self.scheduler.reducer.publication_guard():
            check = checks["combination"]
            current = self._revision(self.target_candidate_id)
            if check.get("checked_revision") != current or self.scheduler.reducer.candidate_canceled(self.target_candidate_id):
                check["passed"] = False
                check["candidate_unchanged"] = False
            self.scheduler.patch_ledger.record_checks(patch_id, checks)
            self.scheduler._persist_patch(patch_id)
            # A registered whole-candidate check after the final merge also
            # checks earlier patches still present in those exact bytes. Never
            # reuse a patch-specific callback, changed bytes or retired owner.
            if check.get("passed") is True and check.get("candidate_wide") is True:
                snapshot = workspace_snapshot(target_root)
                if snapshot_revision(snapshot) == current:
                    for prior_id, record in self.scheduler.reducer.patch_records().items():
                        if prior_id == patch_id or not record.get("patch"):
                            continue
                        prior = CandidatePatch.from_dict(record["patch"])
                        if self._applied_patch_current(prior, snapshot):
                            self.scheduler.patch_ledger.record_checks(prior_id, {"combination": {
                                **check, "observed_after_patch": patch_id}})
                            self.scheduler._persist_patch(prior_id)
                            refreshed[prior_id] = prior.owner_node_id
        # The durable outcome must now say applied-and-checked, or no code
        # dependency on this Builder could ever be released.
        for owner in refreshed.values():
            if owner:
                self.scheduler.refresh_task_outcome(owner)
        return {**result, "checks": checks, "checked_patch_ids": sorted(refreshed)}

    def _applied_patch_current(self, patch: CandidatePatch, snapshot: Mapping[str, str]) -> bool:
        """Validate bytes and producer authority; callers hold publication/mutation locks."""
        record = self.scheduler.patch_ledger.record(patch.patch_id)
        if (not record or not record.get("applied") or record.get("state") != PATCH_STATE_APPLIED
                or self.scheduler.reducer.node_state(patch.owner_node_id) is not NodeState.SUCCEEDED
                or record.get("identity") != patch.identity()
                or patch.target_candidate_id != self.target_candidate_id
                or not patch.content_hashes
                or any(snapshot.get(path) != digest for path, digest in patch.content_hashes.items())):
            return False
        facts = self.scheduler._authoritative_facts(patch)
        return (facts.facts_complete and not facts.canceled and not facts.producer_canceled
            and facts.producer_attempt_id == patch.owner_attempt_id
            and facts.producer_generation == patch.owner_generation
            and facts.producer_fencing_token == patch.owner_fencing_token
            and facts.producer_cancel_epoch == patch.producer_cancel_epoch
            and facts.cancel_epoch == record.get("cancel_epoch")
            and facts.plan_version == patch.plan_version
            and facts.contract_revision == patch.contract_revision
            and facts.selection_winner in {None, patch.owner_node_id}
            and not facts.revoked_dependencies
            and (not patch.dependency_vector or (facts.dependency_vector is not None
                and not patch.dependency_vector.changed_since(facts.dependency_vector))))

    def record_submission_checks(self, check: Mapping[str, Any]) -> None:
        """Bind native whole-candidate submission checks to still-valid applied patches.

        Called only by the primary's native submission preflight, never a model
        tool argument. A parent edit after the last merge needs current evidence
        without reapplying any patch. Final independent acceptance still reruns.
        """
        target = self._target_root_path()
        if target is None:
            raise PatchConflict("submission checks require the live primary candidate")
        reducer = self.scheduler.reducer
        owners = []
        from contextlib import nullcontext
        from minicode.candidate_write_scope import holds_candidate_control_mutation
        # submit_candidate already holds this exact mutex on the tool thread.
        # Reuse that live guard; ordinary callers still acquire it normally.
        guard = (nullcontext() if holds_candidate_control_mutation(self.scheduler.candidates, self.target_candidate_id)
            else self.scheduler.candidates.mutation(candidate_id=self.target_candidate_id, as_runtime=True, timeout=5))
        with guard, reducer.publication_guard():
            refusal = self.scheduler._live_primary(self.parent_node_id, self.parent_attempt_id or self._resolve_attempt())
            snapshot = workspace_snapshot(target)
            revision = snapshot_revision(snapshot)
            if (refusal or check.get("scope") != "registered-combination" or check.get("candidate_wide") is not True
                or check.get("candidate_unchanged") is not True or check.get("candidate_revision") != revision
                or reducer.candidate_canceled(self.target_candidate_id)):
                raise PatchConflict(refusal or "submission check does not describe the live candidate")
            excluded = reducer.replaced_builder_ids() | reducer.discarded_builder_ids()
            records = reducer.patch_records()
            patches = []
            for node in reducer.current_plan.nodes:
                if not node.node_id.startswith("builder-") or node.node_id in excluded:
                    continue
                record = records.get("patch-" + node.node_id, {})
                patch = CandidatePatch.from_dict(record["patch"]) if record.get("patch") else None
                if patch is None or not self._applied_patch_current(patch, snapshot):
                    raise PatchConflict("required Builder has no current applied patch: " + node.node_id)
                patches.append(patch)
            for patch in patches:
                prior_check = (records.get(patch.patch_id, {}).get("checks") or {}).get("combination", {})
                provenance = ({"observed_after_patch": prior_check["observed_after_patch"]}
                    if prior_check.get("checked_revision") == revision and "observed_after_patch" in prior_check else {})
                self.scheduler.patch_ledger.record_checks(patch.patch_id, {"combination": {
                    **dict(check), **provenance, "checked_revision": revision, "observed_at_submission": True}})
                self.scheduler._persist_patch(patch.patch_id)
                owners.append(patch.owner_node_id)
        for owner in owners:
            self.scheduler.refresh_task_outcome(owner)

    def parallel_closure_ready(self) -> bool:
        """Presentation hint from current native facts, never a capability grant."""
        return self.parallel_handoff_evidence() is not None

    def parallel_handoff_evidence(self) -> dict[str, Any] | None:
        """Current applied bytes and combination checks for primary deliberation.

        This observes the same live guards as closure presentation. It neither
        accepts a patch nor substitutes for primary submission/final checks.
        """
        target = self._target_root_path()
        if target is None:
            return None
        # This is a read-only presentation hint, often requested before a model
        # call. A Builder copying P is not a provider failure or closure proof.
        try:
            with self.scheduler.candidates.mutation(candidate_id=self.target_candidate_id, as_runtime=True), self.scheduler.reducer.publication_guard():
                builders = self._required_builders()
                records = self.scheduler.reducer.patch_records()
                if not builders or any(not records.get("patch-" + node, {}).get("applied") for node in builders):
                    return None
                return self._parallel_handoff_evidence(workspace_snapshot(target))
        except CandidateBusy:
            return None

    def _required_builders(self) -> list[str]:
        excluded = self.scheduler.reducer.replaced_builder_ids() | self.scheduler.reducer.discarded_builder_ids()
        return [node.node_id for node in self.scheduler.reducer.current_plan.nodes
                if node.node_id.startswith("builder-") and node.node_id not in excluded]

    def _parallel_handoff_evidence(self, snapshot: Mapping[str, str]) -> dict[str, Any] | None:
        """Caller holds the candidate mutation and reducer publication guards."""
        excluded = self.scheduler.reducer.replaced_builder_ids() | self.scheduler.reducer.discarded_builder_ids()
        builders = [node.node_id for node in self.scheduler.reducer.current_plan.nodes
                        if node.node_id.startswith("builder-") and node.node_id not in excluded]
        if not builders:
            return None
        revision = snapshot_revision(snapshot)
        records = self.scheduler.reducer.patch_records()
        paths, evidence = set(), []
        for node_id in builders:
            record = records.get("patch-" + node_id, {})
            if not record.get("patch"):
                return None
            patch = CandidatePatch.from_dict(record["patch"])
            check = (record.get("checks") or {}).get("combination", {})
            if (check.get("passed") is not True or check.get("checked_revision") != revision
                    or not self._applied_patch_current(patch, snapshot)):
                return None
            paths.update(patch.changed_paths)
            evidence.append({"patch_id": patch.patch_id, "identity": patch.identity(),
                "changed_paths": list(patch.changed_paths), "combination_check": check})
        return {"candidate_revision": revision, "changed_paths": sorted(paths),
            "patches": evidence, "counts_as_final_acceptance": False}

    def merge_progress(self) -> dict[str, Any] | None:
        """Revision-bound guidance, never a submission or permission receipt.

        An apply failure and a failed check after a successful apply are different
        facts. Keep both visible, including a later check on the combined bytes.
        """
        target = self._target_root_path()
        if target is None:
            return None
        try:
            guard = self.scheduler.candidates.mutation(candidate_id=self.target_candidate_id, as_runtime=True)
            guard.__enter__()
        except CandidateBusy:
            return {"candidate_revision": None, "ready_to_submit": False,
                    "status": "candidate_busy", "counts_as_final_acceptance": False}
        try:
            with self.scheduler.reducer.publication_guard():
                builders = self._required_builders()
                if not builders:
                    return None
                # A primary with no Builder work has no merge progress to inspect.
                # Decide that under the same guards before hashing its dependency tree.
                records = self.scheduler.reducer.patch_records()
                # No applied bytes means there is nothing to validate against P.
                # Avoid rehashing thousands of dependencies on every model request.
                has_applied = any(records.get("patch-" + node, {}).get("applied") for node in builders)
                snapshot = workspace_snapshot(target) if has_applied else None
                revision = snapshot_revision(snapshot) if snapshot is not None else None
                pending, patches = [], []
                for node_id in builders:
                    patch_id = "patch-" + node_id
                    record = records.get(patch_id, {})
                    applied = record.get("state") == PATCH_STATE_APPLIED and bool(record.get("applied"))
                    if not applied:
                        pending.append(node_id)
                    if "patch" not in record:
                        # A task ID is not a produced patch. Advertising an
                        # inferred patch ID here induced premature review/recovery.
                        continue
                    check = (record.get("checks") or {}).get("combination", {})
                    patches.append({"patch_id": patch_id, "applied": applied,
                        "checked_revision": check.get("checked_revision"),
                        "check_passed": check.get("passed"),
                        "check_status": check.get("status"),
                        "check_is_current": bool(check.get("checked_revision")) and check.get("checked_revision") == revision})
                proof = self._parallel_handoff_evidence(snapshot) if snapshot is not None else None
                return {"candidate_revision": revision, "ready_to_submit": proof is not None,
                    **({"status": "checks_not_configured"} if not pending and patches
                       and all(patch["check_status"] == "not_configured" for patch in patches) else {}),
                    "pending_application_task_ids": pending, "patches": patches,
                    "changed_paths": proof["changed_paths"] if proof else [],
                    "counts_as_final_acceptance": False}
        finally:
            guard.__exit__(None, None, None)

    def _combination_checks(self, patch: CandidatePatch) -> dict[str, Any]:
        """Run the REGISTERED combination check on the applied revision.

        Copying bytes faithfully proves the copy, not the combination. A check is
        only recorded as passed when the registered checker explicitly says so in
        the contract shape; without a checker, with an unknown shape, or on a crash
        the result says it did not check, so a dependency that needs
        ``patch_applied_and_checked`` cannot be released by it.
        """
        target = self._target_root_path()
        before_revision = snapshot_revision(workspace_snapshot(target)) if target is not None else None
        if self._combination_check is None:
            return {"combination": {
                "passed": False,
                "scope": "no-combination-checker-registered",
                "error": "no combination checker is registered for this Runtime; "
                         "byte identity was verified but the combination was not checked",
                "byte_identity_verified": True}}
        try:
            observed = self._combination_check(patch)
        except Exception as error:  # noqa: BLE001 - a broken checker is not a pass
            return {"combination": {
                "passed": False,
                "scope": "combination-checker-failed",
                "error": f"{type(error).__name__}: {error}",
                "byte_identity_verified": True}}
        malformed = (
            not isinstance(observed, Mapping)
            or not isinstance(observed.get("passed"), bool)
            or not isinstance(observed.get("scope"), str)
            or not observed["scope"].strip())
        if malformed:
            return {"combination": {
                "passed": False,
                "scope": "combination-checker-result-malformed",
                "error": "a registered combination checker must return "
                         "{'passed': bool, 'scope': non-blank str}",
                "reported": observed if isinstance(observed, Mapping) else repr(observed),
                "byte_identity_verified": True}}
        checked_revision = (snapshot_revision(workspace_snapshot(target))
                            if target is not None else None)
        return {"combination": {**observed,
            "passed": observed["passed"] and before_revision is not None and before_revision == checked_revision,
            "checked_revision": before_revision, "candidate_unchanged": before_revision == checked_revision}}

    def cancel(self, *, task_id: str, reason: str = "canceled by the primary",
               subtree: bool = True, operation_id: str | None = None,
               dependency_successors: bool = False) -> dict[str, Any]:
        return self.scheduler.cancel_task(task_id=task_id, reason=reason, subtree=subtree,
            operation_id=operation_id, dependency_successors=dependency_successors)

    def _patch(self, patch_id: str) -> CandidatePatch:
        record = self.scheduler.reducer.patch_records().get(patch_id)
        if not record or "patch" not in record:
            raise PatchConflict("unknown Builder patch id; use only a patch returned by native results. "
                "A task that failed during candidate preparation has no patch to accept or recover.")
        return CandidatePatch.from_dict(record["patch"])

    # ---- zero-model capability preparation ---------------------------------

    def prepare_and_verify_candidate(self, *, node_id: str, attempt_id: str) -> dict[str, Any]:
        """Exercise isolated preparation boundaries WITHOUT a model call.

        A capability receipt that only restates configuration proves nothing. This
        creates an owned probe primary and a probe Builder, runs the real file tool
        inside its write boundary, exports the exact patch, and releases both.
        This receipt proves preparation only. It does not prove Worker execution,
        dependency scheduling, registered checks, or formal delivery.

        The produced patch is deliberately NOT published: a preparation must not
        leave an acceptable artefact behind, or a preflight would look like work.
        """
        from minicode.candidate_write_scope import (
            CandidateWriteAccess,
            bind_candidate_write_access,
        )
        from minicode.tooling import ToolContext, ToolRegistry
        from minicode.tools.write_file import write_file_tool

        receipt: dict[str, Any] = {"schema": "minicode-builder-capability-prepare-v1",
                                   "candidate_created": False, "steps": []}
        target_root = self._target_root_path()
        receipt["target_candidate_id"] = self.target_candidate_id
        receipt["target_root_bound"] = target_root is not None
        before_user = workspace_snapshot(self.source)
        probe_primary = self.provider.create(self.source, owner_token=f"prepare-primary:{node_id}:{attempt_id}")
        lifecycle = BuilderLifecycle(node_id=node_id, candidate_id=f"prepare-candidate-{node_id}",
            handle=self.provider.create(probe_primary.workspace, owner_token=f"prepare-builder:{node_id}:{attempt_id}"),
            provider=self.provider)
        receipt["scope"] = "isolated-preparation-only"
        handle = lifecycle.handle
        receipt["candidate_created"] = handle is not None
        receipt["candidate_id"] = lifecycle.candidate_id
        receipt["candidate_workspace"] = str(handle.workspace) if handle is not None else None
        if handle is None:
            receipt["error"] = "the builder candidate could not be created"
            return receipt

        # The candidate's single writer lease, exactly as the Scheduler grants it
        # before a real Builder runs. Without a live lease the write boundary
        # refuses, so holding one here is what makes the probe a real exercise.
        lease = self.scheduler.candidates.claim(
            candidate_id=lifecycle.candidate_id,
            owner_node_id=node_id,
            owner_attempt_id=attempt_id,
            owner_kind="builder",
            base_revision=snapshot_revision(handle.base_hashes),
        )
        receipt["lease_fenced"] = lease.fencing_token
        probe = handle.workspace / _PREPARE_PROBE_NAME
        registry = ToolRegistry([write_file_tool])
        context = ToolContext(cwd=str(handle.workspace))
        try:
            with bind_candidate_write_access(
                    self.scheduler.candidates,
                    CandidateWriteAccess(candidate_id=lifecycle.candidate_id,
                                         owner_token=lease.owner_token,
                                         root=handle.workspace)):
                written = registry.execute("write_file",
                    {"path": _PREPARE_PROBE_NAME, "content": _PREPARE_PROBE_BODY}, context)
                if not written.ok:
                    raise WorkspaceConflict(str(written.output))
            receipt["steps"].append("real-write-inside-candidate-boundary")
            receipt["probe_written"] = probe.read_text(encoding="utf-8") == _PREPARE_PROBE_BODY
            # The same boundary must still refuse a write that leaves the candidate
            # or belongs to a superseded writer; an escape probe is part of proving
            # the gate is wired, not merely present.
            escape = handle.workspace.parent / (_PREPARE_PROBE_NAME + ".escape")
            try:
                with bind_candidate_write_access(
                        self.scheduler.candidates,
                        CandidateWriteAccess(candidate_id=lifecycle.candidate_id,
                                             owner_token=lease.owner_token,
                                             root=handle.workspace)):
                    escaped = registry.execute("write_file",
                        {"path": f"../{escape.name}", "content": "escape"}, context)
                receipt["escape_refused"] = not escaped.ok and not escape.exists()
            except Exception:  # noqa: BLE001 - a refusal is the expected outcome
                receipt["escape_refused"] = not escape.exists()
            patch = export_candidate_patch(
                handle, patch_id=f"prepare-{node_id}", owner_node_id=node_id,
                owner_attempt_id=attempt_id, owner_generation=1,
                owner_fencing_token=lease.fencing_token,
                plan_version=self.scheduler.reducer.current_plan.plan_version,
                contract_revision=self.scheduler.reducer.current_plan.contract_revision,
                target_candidate_id=self.target_candidate_id)
            receipt["patch_exported"] = True
            receipt["patch_sha256"] = patch.patch_sha256
            receipt["patch_changed_paths"] = list(patch.changed_paths)
            receipt["steps"].append("patch-exported-from-the-same-candidate")
        except Exception as error:  # noqa: BLE001 - a failed preparation is the receipt
            receipt["patch_exported"] = False
            receipt["error"] = f"{type(error).__name__}: {error}"
        finally:
            try:
                if probe.exists():
                    probe.unlink()
            except OSError:
                receipt["probe_cleanup"] = "failed"
            receipt["candidate_released"] = bool(
                self.scheduler.candidates.release(candidate_id=lifecycle.candidate_id,
                                                  owner_token=lease.owner_token))
            lifecycle.close()
            self.provider.cleanup(probe_primary)
        receipt["user_target_untouched"] = workspace_snapshot(self.source) == before_user
        return receipt
