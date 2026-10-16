"""Deterministic, single-writer state reducer for the unified Runtime."""

from __future__ import annotations

import json
import logging
import hashlib
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from minicode.path_safety import is_link_or_reparse, is_unsafe_stored_path
from minicode.store_lock import store_lock
from minicode.runtime_resilience import child_recovery, transient_store_error
from minicode.runtime_state_chunks import FILE_SCHEMA, MIN_STATE_BYTES, StateChunks, sync_directory
from minicode.runtime_protocol import (
    AgentInvocationSpec,
    AgentResult,
    AgentResultStatus,
    ArtifactRef,
    BudgetEnvelope,
    EvidenceRef,
    ExecutionFailure,
    ExecutionFailureKind,
    FencingToken,
    Lease,
    NodeAttempt,
    NodeState,
    PlanNode,
    PlanVersion,
    PrimaryRepair,
    RequestedMode,
    RequiredConsultation,
    RuntimeCommand,
    RuntimeCommandType,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRunIdentity,
    SelectedRoute,
    TaskAmendment,
    TaskContract,
    TaskEnvelope,
    ToolEffect,
    VerificationRecord,
    WorkerContinuation,
    acceptance_criterion_ids,
    new_id,
    _jsonable,
)
from minicode.usage_ledger import (
    BudgetUnavailable,
    ConsultationBudgetLimited,
    ModelUsageUnreconciled,
    TokenPricing,
    UsageLedger,
    add_amounts,
)
from minicode.workspace_lock import normalize_workspace_path
from minicode.agent_collaboration_state import AgentCollaborationState


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class RuntimeStateIntegrityError(RuntimeError):
    """The configured state path aliases data outside its logical store."""


class StaleStateWrite(RuntimeError):
    """A writer tried to persist a snapshot that is no longer the current one.

    Its own copy was read before another writer advanced the durable state, so
    saving it would silently reinstate every field the other writer changed. The
    write is refused instead; the caller must re-read and re-decide.
    """

    def __init__(self, message: str, *, expected: int | None, found: int | None) -> None:
        super().__init__(message)
        self.expected = expected
        self.found = found


class RuntimeStateLoadError(RuntimeError):
    """A requested resume state is absent, corrupt, or incompatible."""


@dataclass(slots=True)
class NodeRuntime:
    definition: PlanNode
    state: NodeState
    attempts: list[NodeAttempt] = field(default_factory=list)
    current_attempt_id: str | None = None
    result: AgentResult | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    resume_credits: int = 0
    workspace_committed: bool = True
    continuation: WorkerContinuation | None = None
    continuation_count: int = 0
    advisory_acknowledgment: dict[str, Any] | None = None


@dataclass(slots=True)
class RuntimeState:
    run_id: str
    contract_revision: int
    current_plan_version: int
    plans: dict[int, PlanVersion]
    nodes: dict[str, NodeRuntime]
    contract: TaskContract | None = None
    identity: RuntimeRunIdentity | None = None
    final_verification_records: list[VerificationRecord] = field(default_factory=list)
    events: list[RuntimeEvent] = field(default_factory=list)
    canceled: bool = False
    paused: bool = False
    processed_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    consumed_approvals: set[str] = field(default_factory=set)
    next_fencing_token: int = 1
    tokens_used: int = 0
    cost_used: float = 0.0
    usage_ledger: UsageLedger = field(default_factory=UsageLedger)
    continuations_enabled: bool = False
    continuation_limit: int = 16
    repair_limit: int = 0
    repair_history: list[PrimaryRepair] = field(default_factory=list)
    pending_repair_id: str | None = None
    candidate_first: bool = False
    integration_status: str = "idle"
    integration_revision: str | None = None
    integration_error: str = ""
    advisory_enabled: bool = False
    closure_budget: dict[str, Any] | None = None
    shared_consultation_budget: bool = False
    # The CREATION relation (who registered whom), which is a different graph from
    # the dependency DAG. A subtree cancellation follows creation edges; a
    # dependency failure propagates along `PlanNode.dependencies`. Conflating the
    # two either over-cancels unrelated work or leaves a real descendant running.
    delegation_parents: dict[str, str] = field(default_factory=dict)
    # Per-node cancellation epochs. Cancelling B must not invalidate a wait that
    # only declares A, so the epoch a wait re-checks is the one belonging to the
    # tasks that wait actually declared.
    node_cancel_epoch: dict[str, int] = field(default_factory=dict)
    # Cancellation is an operation, not a counter: the same operation id is
    # idempotent and never advances an epoch a second time.
    cancel_operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Durable per-edge dependency predicates declared when a Builder is
    # registered, so READY and CLAIM can both re-check the real predicate.
    dependency_predicates: dict[str, dict[str, str]] = field(default_factory=dict)
    # Durable child results, waits and patch receipts. They live in the persisted
    # state so a rebuilt Scheduler over the same Reducer recovers them.
    child_outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    dependency_waits: dict[str, dict[str, Any]] = field(default_factory=dict)
    wait_resumes: dict[str, int] = field(default_factory=dict)
    patch_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    worker_completions: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidate_quarantines: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidate_selections: dict[str, dict[str, Any]] = field(default_factory=dict)
    async_agents_enabled: bool = False
    shared_workspace_writes_enabled: bool = False
    agent_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    agent_messages_enabled: bool = False
    agent_resume_enabled: bool = False
    agent_team: dict[str, Any] | None = None
    agent_fork_enabled: bool = False
    coordinator_enabled: bool = False
    agent_team_writes_enabled: bool = False
    agent_messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    agent_checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    user_steering: dict[str, Any] | None = None
    agent_finishing: dict[str, str] = field(default_factory=dict)
    agent_collaboration: dict[str, Any] | None = None
    review_runtime: dict[str, Any] | None = None
    # Monotonic revision of the durable snapshot. It is part of the persisted
    # document, so any writer can tell whether the file it is about to replace is
    # still the one it read. Without it, two processes that both restored the same
    # snapshot each persist a complete copy and the later one silently erases the
    # other's transitions.
    revision: int = 0


class ReducerError(RuntimeError):
    """A command was structurally invalid for the current Runtime state."""


# Which recorded facts a dependency pin actually asserts, per predicate. A field
# outside this set (event_id, sequence) is bookkeeping that a legitimate
# republish reassigns, so comparing it would revoke pins for no real reason.
_PIN_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "artifact_validated": ("generation", "artifact_sha256", "revision", "terminal",
                           "attempt_id", "candidate_id"),
    "patch_applied_and_checked": ("generation", "artifact_sha256", "revision",
                                  "applied_revision", "patch_sha256", "terminal",
                                  "attempt_id", "candidate_id"),
    "terminal_observed": ("generation", "terminal", "attempt_id"),
}


class RuntimeStateStore:
    """Atomic JSON manifest with incremental pages for large snapshots.

    It is intentionally small and local.  Vector databases may index evidence,
    but never become authoritative for this state.
    """

    # Bounded retry for a transient Windows sharing violation on the atomic
    # replace. It changes no state semantics: either the complete new snapshot
    # lands or the original file is left untouched.
    #
    # The window is deliberately generous. A separate reader that only ever
    # OPENS the destination (a host polling progress, an operator inventory, a
    # filesystem scanner) makes the replace fail with a sharing violation for as
    # long as it holds the handle, and a reader that reopens every few
    # milliseconds can starve a 75ms window indefinitely. Retrying for up to
    # ~2.5 seconds with backoff rides out that contention; past it the failure is
    # real and is raised with the attempts that were made.
    _STATE_REPLACE_ATTEMPTS = 12
    _STATE_REPLACE_BACKOFF_SECONDS = 0.01
    _STATE_REPLACE_MAX_BACKOFF_SECONDS = 0.25

    def __init__(self, path: Path, *, owner_lease=None) -> None:
        self.path = Path(path)
        if os.name == "nt":
            # State names and their atomic sibling temporaries can exceed
            # MAX_PATH. Preserve lexical aliases for the safety checks below;
            # resolving here would hide a final symlink before validation.
            value = str(self.path.absolute())
            if not value.startswith("\\\\?\\"):
                self.path = Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
        # None is either an unobserved store or a legacy snapshot without a
        # revision. _observed plus the content digest distinguish these cases;
        # neither permits blindly replacing an existing state file.
        self.expected_revision: int | None = None
        self._expected_digest: str | None = None
        self._observed = False
        self._expected_manifest_digest: str | None = None
        self._loaded_manifest_digest: str | None = None
        self._loaded_payload_digest: str | None = None
        self._chunks = StateChunks(self.path)
        self.last_write_bytes = 0
        self.owner_lease = owner_lease
        # How many atomic replace attempts the last save needed. Recorded so a
        # contention diagnosis rests on a measurement rather than on a guess.
        self.replace_attempts = 0
        self._replace_started_at = 0.0

    def _state_replace_elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._replace_started_at)

    @staticmethod
    def payload_revision(payload: Mapping[str, Any] | None) -> int | None:
        """The revision recorded inside a snapshot, or ``None`` for a legacy one."""

        if not isinstance(payload, Mapping):
            return None
        value = payload.get("state_revision")
        if type(value) is not int or value < 0:
            return None
        return value

    def read_revision(self) -> int | None:
        """Read only the durable revision, without trusting an in-memory copy."""

        return self.payload_revision(self._load_without_adoption())

    @staticmethod
    def _payload_digest(payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()

    def adopt(self, payload: Mapping[str, Any]) -> None:
        """Remember the exact snapshot read, including legacy snapshots without a revision."""
        self.expected_revision = self.payload_revision(payload)
        self._expected_digest = self._payload_digest(payload)
        self._expected_manifest_digest = (self._loaded_manifest_digest
            if self._expected_digest == self._loaded_payload_digest else None)
        self._observed = True

    @classmethod
    def decode_payload(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Unwrap a durable file; old readers see no plan they could execute."""
        if payload.get("schema_version") == FILE_SCHEMA:
            raise RuntimeStateLoadError("Paged Runtime state must be read with RuntimeStateStore.load_required()")
        if payload.get("schema_version") == "minicode-runtime-file-v1":
            state = payload.get("runtime_state")
            if (set(payload) != {"schema_version", "run_id", "state_revision", "payload_sha256", "runtime_state"}
                or not isinstance(state, dict)
                or state.get("schema_version") not in ({f"minicode-runtime-state-v{i}" for i in range(12, 19)} | {"minicode-runtime-state-v20", "minicode-runtime-state-v21"})
                or state.get("run_id") != payload.get("run_id")
                or cls.payload_revision(state) != cls.payload_revision(payload)
                or cls._payload_digest(state) != payload.get("payload_sha256")):
                raise RuntimeStateLoadError("Runtime state file envelope failed integrity validation")
            return state
        if "runtime_state" in payload or str(payload.get("schema_version", "")).startswith("minicode-runtime-file-"):
            raise RuntimeStateLoadError("Unsupported Runtime state file envelope")
        return dict(payload)

    @classmethod
    def _storage_payload(cls, payload: Mapping[str, Any], *, digest: str | None = None) -> dict[str, Any]:
        if payload.get("schema_version") in ({f"minicode-runtime-state-v{i}" for i in range(12, 19)} | {"minicode-runtime-state-v20", "minicode-runtime-state-v21"}):
            return {"schema_version": "minicode-runtime-file-v1", "run_id": payload["run_id"],
                "state_revision": cls.payload_revision(payload), "payload_sha256": digest or cls._payload_digest(payload),
                "runtime_state": dict(payload)}
        return dict(payload)

    def _commit_lock(self):
        from minicode.run_ownership import owner_lock_path
        # Refuse a destination alias before creating any lock metadata beside
        # the resolved destination. The check is repeated inside the transaction.
        self._ensure_safe(self.path, reject_hardlinks=True)
        return store_lock(owner_lock_path(self.path).with_suffix(".commit.lock"))

    def _assert_writer(self, payload: Mapping[str, Any]) -> None:
        from minicode.run_ownership import live_holder, normalize_state_path, RunStateOwnerMismatch
        if self.owner_lease is not None:
            self.owner_lease.validate(run_id=payload.get("run_id"))
            if normalize_state_path(self.path) != normalize_state_path(self.owner_lease.state_path):
                raise RunStateOwnerMismatch("the writer's lease names a different state path")
        elif live_holder(self.path) is not None:
            raise RunStateOwnerMismatch("state has a live owner; route writes through its bound Reducer")

    def _check_revision(self, payload: Mapping[str, Any]) -> None:
        if self._expected_manifest_digest is not None:
            try:
                manifest = json.loads(self._read_with_replace_tolerance())
                if (isinstance(manifest, dict) and manifest.get("manifest_sha256") == self._expected_manifest_digest
                        and self.payload_revision(manifest) == self.expected_revision):
                    self._chunks.validate(manifest)
                    return
                found = self.payload_revision(manifest)
            except (OSError, ValueError, TypeError):
                found = None
            raise StaleStateWrite("the durable Runtime manifest changed or failed integrity validation",
                                  expected=self.expected_revision, found=found)
        current, current_digest = self._load_with_digest()
        if current is None and not self.path.exists() and not self._observed:
            return
        found = self.payload_revision(current)
        if (self._observed and current is not None and found == self.expected_revision
                and current_digest == self._expected_digest):
            return
        raise StaleStateWrite(
            "the durable Runtime state advanced to revision "
            f"{found!r} after this writer read revision {self.expected_revision!r}; "
            "refusing to persist a stale snapshot over it",
            expected=self.expected_revision,
            found=found,
        )

    def save(self, payload: Mapping[str, Any]) -> None:
        # All Store instances and processes share this lock. Checking before
        # taking it would still permit two writers to pass the same comparison.
        with self._commit_lock():
            self._assert_writer(payload)
            self._save_locked(payload)

    def _save_locked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_safe(self.path, reject_hardlinks=True)
        self._check_revision(payload)
        temporary = self.path.with_name(f".{self.path.name}.{new_id('tmp')}.tmp")
        # Runtime snapshots are machine state on a synchronous durability path;
        # compact encoding materially reduces every reducer transition without
        # changing the JSON contract.
        eligible = (re.fullmatch(r"minicode-runtime-state-v(?:[3-9]|1[0-9]|20)", str(payload.get("schema_version")))
                    and isinstance(payload.get("run_id"), str) and self.payload_revision(payload) is not None)
        state_encoded = None
        paged = bool(eligible and self._expected_manifest_digest is not None)
        if not paged:
            state_encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            paged = bool(eligible and len(state_encoded.encode("utf-8")) >= MIN_STATE_BYTES)
        if paged:
            envelope = self._chunks.pack(payload)
            encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = None
        else:
            digest = hashlib.sha256(state_encoded.encode("utf-8")).hexdigest()
            envelope = self._storage_payload(payload, digest=digest)
            encoded = state_encoded
        if envelope.get("schema_version") == "minicode-runtime-file-v1":
            # Insert the already encoded state into the unchanged envelope;
            # avoid encoding the growing history again for hash and adoption.
            encoded = "{" + ",".join(json.dumps(key) + ":" + (
                state_encoded if key == "runtime_state" else json.dumps(envelope[key], ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"))) for key in sorted(envelope)) + "}"
        try:
            # The parent and destination were validated above.  Exclusive
            # creation makes the random temporary name fail rather than follow
            # a pre-created alias, avoiding a second expensive path traversal.
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            # On Windows the replace can fail with a sharing violation when any
            # other handle on the destination is still open -- including another
            # process that is only reading. That is a filesystem artefact, not a
            # reasoning failure, so retry with backoff instead of aborting a run;
            # the failure is reported with its attempt count if it outlasts it.
            self._replace_started_at = time.monotonic()
            for attempt in range(self._STATE_REPLACE_ATTEMPTS):
                try:
                    os.replace(temporary, self.path)
                    self.replace_attempts = attempt + 1
                    break
                except PermissionError as error:
                    if attempt == self._STATE_REPLACE_ATTEMPTS - 1:
                        raise PermissionError(
                            f"{error} (the atomic state replace did not succeed after "
                            f"{self._STATE_REPLACE_ATTEMPTS} attempts over "
                            f"{self._state_replace_elapsed():.2f}s; another handle on "
                            f"{self.path} kept the destination open)"
                        ) from error
                    time.sleep(min(self._STATE_REPLACE_MAX_BACKOFF_SECONDS,
                                   self._STATE_REPLACE_BACKOFF_SECONDS * (2 ** attempt)))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        # Only a completed replacement advances this writer's view of the file.
        self.expected_revision = self.payload_revision(payload)
        self._expected_digest = digest
        self._expected_manifest_digest = envelope.get("manifest_sha256") if paged else None
        self._loaded_manifest_digest = self._loaded_payload_digest = None
        self._observed = True
        self.last_write_bytes = len((encoded + "\n").encode("utf-8")) + (self._chunks.written_bytes if paged else 0)
        if paged:
            try:
                # Make the new reference set durable before reclaiming old
                # pages. Readers hold the same lock through manifest expansion.
                sync_directory(self.path.parent)
                self._chunks.prune(payload["run_id"], self._chunks.references(envelope))
            except (OSError, ValueError) as error:
                logging.getLogger(__name__).warning("Runtime page reclamation deferred: %s", error)

    def _read_with_replace_tolerance(self) -> str:
        """Read the snapshot, tolerating a concurrent atomic replacement.

        ``os.replace`` is atomic for the CONTENT, but on Windows a reader that opens
        the destination at the wrong instant can still be refused, because replacing
        a file requires a moment where no other handle has it open. That is the mirror
        image of the writer-side retry, and it matters for the same reason: a reader
        (a host polling progress, an operator inventory, a second Scheduler) failing
        with a bare `PermissionError` would look like a state corruption instead of a
        filesystem race. The retry is bounded and does not change what a snapshot is.
        """

        for attempt in range(self._STATE_REPLACE_ATTEMPTS):
            try:
                return self.path.read_text(encoding="utf-8")
            except PermissionError:
                if attempt == self._STATE_REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(min(self._STATE_REPLACE_MAX_BACKOFF_SECONDS,
                               self._STATE_REPLACE_BACKOFF_SECONDS * (2 ** attempt)))
        raise AssertionError("unreachable")

    def reserve_new(self) -> None:
        """Atomically claim a never-before-used state path for a new run."""
        with self._commit_lock():
            self._assert_writer({})
            self._reserve_new_locked()

    def _reserve_new_locked(self) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_safe(self.path, reject_hardlinks=True)
        reservation = {
            "schema_version": "minicode-runtime-state-v3",
            "initializing": True,
        }
        try:
            with self.path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(reservation, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise RuntimeStateLoadError(
                "Runtime state already exists; use a new run ID or runtimeResume=true"
            ) from error
        self.adopt(reservation)

    def load(self) -> dict[str, Any] | None:
        payload = self._load_without_adoption()
        if payload is not None:
            self.adopt(payload)
        return payload

    def _load_without_adoption(self) -> dict[str, Any] | None:
        return self._load_with_digest()[0]

    def _load_with_digest(self) -> tuple[dict[str, Any] | None, str | None]:
        if (not self.path.is_file() or is_unsafe_stored_path(self.path,
                boundary=self.path.parent, reject_hardlinks=True)):
            return None, None
        with self._commit_lock():
            return self._load_with_digest_locked()

    def _decode_durable(self, payload):
        if payload.get("schema_version") == FILE_SCHEMA:
            try:
                decoded = self._chunks.read(payload)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise RuntimeStateLoadError(f"Runtime state pages failed integrity validation: {error}") from error
        else:
            decoded = self.decode_payload(payload)
        digest = (payload["payload_sha256"] if payload.get("schema_version") == "minicode-runtime-file-v1"
                  else self._payload_digest(decoded))
        self._loaded_payload_digest = digest
        self._loaded_manifest_digest = payload.get("manifest_sha256") if payload.get("schema_version") == FILE_SCHEMA else None
        return decoded, digest

    def _load_with_digest_locked(self) -> tuple[dict[str, Any] | None, str | None]:
        if (
            is_unsafe_stored_path(
                self.path,
                boundary=self.path.parent,
                reject_hardlinks=True,
            )
            or not self.path.is_file()
        ):
            return None, None
        try:
            payload = json.loads(self._read_with_replace_tolerance())
        except (OSError, json.JSONDecodeError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        try:
            return self._decode_durable(payload)
        except RuntimeStateLoadError:
            return None, None

    def load_required(self) -> dict[str, Any]:
        """Load resume state without collapsing corruption into a fresh run."""

        if is_unsafe_stored_path(
            self.path,
            boundary=self.path.parent,
            reject_hardlinks=True,
        ):
            raise RuntimeStateIntegrityError(
                "Runtime state path escapes or aliases its configured store"
            )
        if not self.path.exists():
            raise RuntimeStateLoadError(f"Runtime resume state does not exist: {self.path}")
        if not self.path.is_file():
            raise RuntimeStateLoadError(f"Runtime resume state is not a regular file: {self.path}")
        with self._commit_lock():
            return self._load_required_locked()

    def _load_required_locked(self):
        try:
            raw = self._read_with_replace_tolerance()
        except OSError as error:
            raise RuntimeStateLoadError(
                f"Runtime resume state could not be read: {error}"
            ) from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeStateLoadError(
                f"Runtime resume state is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeStateLoadError("Runtime resume state root must be an object")
        payload, _digest = self._decode_durable(payload)
        self.adopt(payload)
        return payload

    def _ensure_safe(self, path: Path, *, reject_hardlinks: bool = False) -> None:
        candidate = Path(path)
        if candidate == self.path:
            # The configured store directory is the trust boundary, so for the
            # fixed destination the generic containment/resolution walk is
            # equivalent to checking the leaf for aliases.  Keep doing this on
            # every save to detect a destination swapped after construction.
            if is_link_or_reparse(candidate):
                raise RuntimeStateIntegrityError(
                    "Runtime state path escapes or aliases its configured store"
                )
            if reject_hardlinks:
                try:
                    metadata = candidate.lstat()
                except FileNotFoundError:
                    return
                except OSError as error:
                    raise RuntimeStateIntegrityError(
                        "Runtime state path escapes or aliases its configured store"
                    ) from error
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                    raise RuntimeStateIntegrityError(
                        "Runtime state path escapes or aliases its configured store"
                    )
            return
        if is_unsafe_stored_path(
            candidate,
            boundary=self.path.parent,
            reject_hardlinks=reject_hardlinks,
        ):
            raise RuntimeStateIntegrityError(
                "Runtime state path escapes or aliases its configured store"
            )


def _initial_node_state(node: PlanNode) -> NodeState:
    return NodeState.PENDING if node.dependencies else NodeState.READY


class _ReducerLock:
    """A failed durable commit fences the entire old in-memory authority."""

    def __init__(self, owner) -> None:
        self.owner = owner
        self.lock = threading.RLock()

    def __enter__(self):
        self.lock.acquire()
        if self.owner._persistence_error is not None:
            self.lock.release()
            raise RuntimeStateIntegrityError(
                "this Reducer failed to persist; reload durable state before further use"
            ) from self.owner._persistence_error
        return self

    def __exit__(self, *_args):
        self.lock.release()


class RuntimeReducer(AgentCollaborationState):
    """The only object allowed to mutate global Runtime state.

    Scheduler, workers, policy, and verifiers submit typed commands.  The
    reducer validates version/attempt/lease/fencing constraints, appends an
    event, then optionally persists the whole state under the same lock.
    """

    # One bounded recovery request per distinct rejected-argument root fault, and
    # ONE bounded total for the whole logical primary execution. Both counts live
    # in the persisted event log, so a restart, resume, checkpoint, new call ID,
    # repeated argument text or a reordered rejected set cannot refresh them and
    # cannot silently multiply the total number of provider calls. Root faults
    # are deduplicated identities, so repeating the same broken arguments does not
    # look like a new fault.
    primary_error_recovery_limit = 1
    primary_error_recovery_total_limit = 1

    def __init__(
        self,
        plan: PlanVersion,
        *,
        clock: Clock | None = None,
        store: RuntimeStateStore | None = None,
        contract: TaskContract | None = None,
        identity: RuntimeRunIdentity | None = None,
        continuations_enabled: bool = False,
        continuation_limit: int = 16,
        repair_limit: int = 0,
        candidate_first: bool = False,
        advisory_enabled: bool = False,
        closure_reserve_enabled: bool = False,
        shared_consultation_budget: bool = False,
        async_agents_enabled: bool = False,
        shared_workspace_writes_enabled: bool = False,
        agent_messages_enabled: bool = False,
        agent_resume_enabled: bool = False,
        agent_teams_enabled: bool = False,
        agent_fork_enabled: bool = False,
        coordinator_enabled: bool = False,
        agent_team_writes_enabled: bool = False,
        agent_collaboration: Mapping | None = None,
        review_baseline: Mapping[str, str] | None = None,
        review_reasoning_effort: str | None = None,
        review_max_agents: int = 4,
    ) -> None:
        self._clock = clock or SystemClock()
        self._store = store
        self._persistence_error: BaseException | None = None
        self._lock = _ReducerLock(self)
        if agent_collaboration is not None:
            from minicode.agent_collaboration import validate_config
            validate_config(agent_collaboration)
            if not agent_messages_enabled:
                raise ValueError("continuous collaboration requires checkpointed messages")
        if type(shared_workspace_writes_enabled) is not bool or (shared_workspace_writes_enabled and (
            not async_agents_enabled or candidate_first or coordinator_enabled
            or (agent_teams_enabled and not agent_team_writes_enabled))):
            raise ReducerError("shared writers require direct async execution with team write permission")
        if type(coordinator_enabled) is not bool or (coordinator_enabled and (not async_agents_enabled or agent_fork_enabled)):
            raise ReducerError("Coordinator requires async delegation and cannot Fork")
        if type(agent_team_writes_enabled) is not bool or (agent_team_writes_enabled and not agent_teams_enabled):
            raise ReducerError("team write policy requires an owned team")
        if type(agent_fork_enabled) is not bool or (agent_fork_enabled and not agent_messages_enabled):
            raise ReducerError("agent fork requires recoverable context checkpoints")
        if type(agent_teams_enabled) is not bool or (agent_teams_enabled and not agent_resume_enabled):
            raise ReducerError("agent teams require controlled resume")
        if type(agent_resume_enabled) is not bool or (agent_resume_enabled and not agent_messages_enabled):
            raise ReducerError("agent resume requires checkpointed agent messages")
        if type(agent_messages_enabled) is not bool or (agent_messages_enabled and not async_agents_enabled):
            raise ValueError("agent messages require async agents")
        if type(async_agents_enabled) is not bool or (async_agents_enabled and (
            not continuations_enabled or contract is None
            or [(n.node_id, n.role) for n in plan.nodes if n.role == "primary"] != [("primary", "primary")]
        )):
            raise ValueError("async agents require a contracted durable primary")
        if type(advisory_enabled) is not bool or (advisory_enabled and not continuations_enabled):
            raise ValueError("advisory consultations require durable primary execution")
        if type(closure_reserve_enabled) is not bool or (closure_reserve_enabled and (
            not advisory_enabled or contract is None
            or [(n.node_id, n.role) for n in plan.nodes if n.role == "primary"] != [("primary", "primary")]
        )):
            raise ValueError("closure reserves require a contracted durable primary with advisory policy")
        if contract and contract.required_consultation and not closure_reserve_enabled:
            raise ValueError("required consultation needs the contracted durable primary policy")
        if type(shared_consultation_budget) is not bool or (shared_consultation_budget and (
            not closure_reserve_enabled or contract is None or contract.budget.max_tokens is None
        )):
            raise ValueError("shared consultation budget requires bounded tokens and closure protection")
        for node in plan.nodes:
            self._validate_criticality(node, advisory_enabled)
            if any(other.criticality == "advisory" and other.node_id in node.dependencies for other in plan.nodes):
                raise ReducerError("DAG dependencies cannot be advisory consultations")
        from minicode.runtime_protocol import validate_review_plan
        validate_review_plan(plan.nodes)
        if type(continuation_limit) is not int or not 1 <= continuation_limit <= 128:
            raise ValueError("continuation_limit must be an integer between 1 and 128")
        if type(repair_limit) is not int or not 0 <= repair_limit <= 4 or (repair_limit and not continuations_enabled):
            raise ValueError("repair_limit must be 0..4 and requires durable primary execution")
        if type(candidate_first) is not bool or (candidate_first and (
            not continuations_enabled or [n.node_id for n in plan.nodes if n.is_writer] != ["primary"]
            or next(n for n in plan.nodes if n.node_id == "primary").role != "primary"
        )):
            raise ValueError("candidate-first delivery requires a durable primary as the only writer")
        self._state = RuntimeState(
            run_id=plan.run_id,
            contract_revision=plan.contract_revision,
            current_plan_version=plan.plan_version,
            plans={plan.plan_version: plan},
            nodes={
                node.node_id: NodeRuntime(node, _initial_node_state(node))
                for node in plan.nodes
            },
            contract=contract,
            identity=identity,
            continuations_enabled=continuations_enabled,
            continuation_limit=continuation_limit,
            repair_limit=repair_limit,
            candidate_first=candidate_first,
            advisory_enabled=advisory_enabled,
            closure_budget=self._resolve_closure_budget(plan, contract) if closure_reserve_enabled else None,
            shared_consultation_budget=shared_consultation_budget,
            async_agents_enabled=async_agents_enabled,
            shared_workspace_writes_enabled=shared_workspace_writes_enabled,
            agent_messages_enabled=agent_messages_enabled,
            agent_resume_enabled=agent_resume_enabled,
            agent_collaboration=({"config": deepcopy(dict(agent_collaboration)), "results": {}, "checks": {}}
                                 if agent_collaboration is not None else None),
            agent_fork_enabled=agent_fork_enabled,
            coordinator_enabled=coordinator_enabled,
            agent_team_writes_enabled=agent_team_writes_enabled,
            agent_team=({"team_id": "team-" + hashlib.sha256(plan.run_id.encode()).hexdigest()[:24],
                "run_id": plan.run_id, "lead_id": "primary", "policy": "parent-child-team-v1",
                "state": "active", "member_limit": agent_collaboration["max_sessions"] if agent_collaboration else 2,
                "retired_members": {}, "resource_disposition": {}}
                if agent_teams_enabled else None),
            revision=1,
        )
        if review_baseline is not None:
            primary = self._node("primary").definition
            if primary.review_policy is None or primary.role != "primary" or contract is None:
                raise ReducerError("dynamic review requires a contracted admitted primary policy")
            self._validate_review_hashes(review_baseline)
            self._state.review_runtime = {"policy": "observed-review-v1", "admission": deepcopy(primary.review_policy),
                "baseline": dict(review_baseline), "reasoning_effort": review_reasoning_effort,
                "max_agents": review_max_agents, "records": []}
            self._validate_dynamic_review()
        self._persist()

    @classmethod
    def restore(
        cls,
        payload: Mapping[str, Any],
        *,
        clock: Clock | None = None,
        store: RuntimeStateStore | None = None,
    ) -> RuntimeReducer:
        """Rehydrate a persisted snapshot without replaying side effects."""
        payload = RuntimeStateStore.decode_payload(payload)
        # Schema wrappers below are projected into their base schema for
        # validation. CAS must remember the durable document, not that projection.
        durable_payload = deepcopy(payload) if store is not None else None
        dynamic_review = payload.get("schema_version") == "minicode-runtime-state-v21"
        review_runtime = payload.get("review_runtime")
        if dynamic_review:
            if (not isinstance(review_runtime, dict) or review_runtime.get("policy") != "observed-review-v1"
                or payload.get("review_base_schema") not in {f"minicode-runtime-state-v{i}" for i in range(3, 21)}):
                raise ReducerError("invalid observed review snapshot wrapper")
            payload = {**payload, "schema_version": payload["review_base_schema"]}
        elif "review_runtime" in payload or "review_base_schema" in payload:
            raise ReducerError("observed review state cannot be downgraded")
        continuous = payload.get("schema_version") == "minicode-runtime-state-v20"
        if continuous:
            if (not isinstance(payload.get("agent_collaboration"), dict)
                or payload.get("agent_collaboration_base_schema") not in {f"minicode-runtime-state-v{i}" for i in range(13, 20)}):
                raise ReducerError("invalid continuous collaboration snapshot wrapper")
            payload = {**payload, "schema_version": payload["agent_collaboration_base_schema"]}
        elif "agent_collaboration" in payload or "agent_collaboration_base_schema" in payload:
            raise ReducerError("continuous collaboration state cannot be downgraded")
        human_steering = payload.get("schema_version") == "minicode-runtime-state-v19"
        if human_steering:
            if (not isinstance(payload.get("user_steering"), dict)
                or payload.get("user_steering_base_schema") not in
                    {f"minicode-runtime-state-v{i}" for i in range(3, 19)}):
                raise ReducerError("invalid human steering snapshot wrapper")
            payload = {**payload, "schema_version": payload["user_steering_base_schema"]}
        elif "user_steering" in payload or "user_steering_base_schema" in payload:
            raise ReducerError("human steering state cannot be downgraded")
        raw_messages = payload.get("agent_messages", {})
        if not isinstance(raw_messages, Mapping):
            raise ReducerError("agent messages must be a mapping")
        artifact_messages = any(isinstance(record, Mapping) and "body_artifact" in record
            for record in raw_messages.values())
        if payload.get("schema_version") == "minicode-runtime-state-v18":
            if (not artifact_messages or payload.get("agent_message_artifact_policy") != "owned-message-body-v1"
                or payload.get("agent_message_artifact_base_schema") not in {f"minicode-runtime-state-v{i}" for i in range(13, 18)}):
                raise ReducerError("invalid message artifact policy")
            payload = {**payload, "schema_version": payload["agent_message_artifact_base_schema"]}
        elif artifact_messages or "agent_message_artifact_policy" in payload or "agent_message_artifact_base_schema" in payload:
            raise ReducerError("message artifacts cannot be downgraded")
        collaboration_policy = payload.get("schema_version") == "minicode-runtime-state-v17"
        capabilities = payload.get("agent_capabilities", {})
        if collaboration_policy:
            if (capabilities.get("policy") != "parent-child-capabilities-v1"
                or set(capabilities) != {"policy", "coordinator", "team_writes"}
                or any(type(capabilities[k]) is not bool for k in ("coordinator", "team_writes"))
                or payload.get("agent_capabilities_base_schema") not in {f"minicode-runtime-state-v{i}" for i in range(12, 17)}):
                raise ReducerError("invalid persisted agent capability policy")
            payload = {**payload, "schema_version": payload["agent_capabilities_base_schema"]}
        elif "agent_capabilities" in payload or "agent_capabilities_base_schema" in payload:
            raise ReducerError("agent capabilities require their versioned schema")
        agent_fork = payload.get("schema_version") == "minicode-runtime-state-v16"
        if agent_fork:
            if (payload.get("agent_fork_policy") != "readonly-context-fork-v1"
                or payload.get("agent_fork_base_schema") not in {f"minicode-runtime-state-v{i}" for i in range(13, 16)}):
                raise ReducerError("invalid agent fork policy")
            payload = {**payload, "schema_version": payload["agent_fork_base_schema"]}
        elif "agent_fork_policy" in payload or "agent_fork_base_schema" in payload:
            raise ReducerError("fork state cannot be downgraded")
        agent_teams = payload.get("schema_version") == "minicode-runtime-state-v15"
        if agent_teams:
            if not isinstance(payload.get("agent_team"), dict) or payload["agent_team"].get("policy") != "parent-child-team-v1":
                raise ReducerError("invalid agent team policy")
            payload = {**payload, "schema_version": "minicode-runtime-state-v14"}
        elif "agent_team" in payload:
            raise ReducerError("agent team state cannot be downgraded")
        agent_resume = payload.get("schema_version") == "minicode-runtime-state-v14"
        if agent_resume:
            if payload.get("agent_resume_policy") != "bounded-session-continuation-v1":
                raise ReducerError("invalid agent resume policy")
            payload = {**payload, "schema_version": "minicode-runtime-state-v13"}
        elif "agent_resume_policy" in payload:
            raise ReducerError("agent resume state cannot be downgraded")
        agent_messages = payload.get("schema_version") == "minicode-runtime-state-v13"
        if agent_messages:
            if (payload.get("agent_message_policy") != "parent-child-inbox-v1"
                or not all(isinstance(payload.get(key), dict) for key in ("agent_messages", "agent_checkpoints", "agent_finishing"))):
                raise ReducerError("invalid agent message policy")
            payload = {**payload, "schema_version": "minicode-runtime-state-v12"}
        elif any(key in payload for key in ("agent_message_policy", "agent_messages", "agent_checkpoints", "agent_finishing")):
            raise ReducerError("agent message state cannot be downgraded")
        async_agents = payload.get("schema_version") == "minicode-runtime-state-v12"
        if async_agents:
            if (payload.get("agent_session_policy") not in {"async-agents-v1", "async-agents-shared-writes-v1"}
                or payload.get("agent_base_schema") not in {f"minicode-runtime-state-v{i}" for i in range(4, 12)}
                or not isinstance(payload.get("agent_sessions"), dict)):
                raise ReducerError("invalid async agent state policy")
            payload = {**payload, "schema_version": payload["agent_base_schema"]}
        elif any(key in payload for key in ("agent_session_policy", "agent_base_schema", "agent_sessions")):
            raise ReducerError("async agent state cannot be downgraded")
        orchestration_data = any(payload.get(key) for key in (
            "worker_completions", "candidate_quarantines", "candidate_selections")) or any(
            node.get("definition", {}).get("alternative_group") for node in payload.get("nodes", {}).values())
        if payload.get("schema_version") == "minicode-runtime-state-v11":
            if (not orchestration_data or payload.get("candidate_lifecycle_policy") != "candidate-lifecycle-v1"
                or payload.get("orchestration_base_schema") not in {f"minicode-runtime-state-v{i}" for i in range(3, 11)}):
                raise ReducerError("invalid candidate lifecycle state policy")
            payload = {**payload, "schema_version": payload["orchestration_base_schema"]}
        elif orchestration_data or "candidate_lifecycle_policy" in payload or "orchestration_base_schema" in payload:
            raise ReducerError("candidate lifecycle state cannot be downgraded")
        paged_state = payload.get("schema_version") == "minicode-runtime-state-v10"
        has_paged_data = any((node.get("result") or {}).get("consultation_summary_ref") is not None
                            for node in payload.get("nodes", {}).values()) or any(
            event.get("event_type") == "ConsultationSummaryRead"
            or (event.get("payload") or {}).get("consultation_summary_ref") is not None
            for event in payload.get("events", []))
        if paged_state:
            if (not has_paged_data or payload.get("consultation_read_policy") != "consultation-paged-decision-v1"
                or payload.get("base_state_schema_version") not in {f"minicode-runtime-state-v{i}" for i in range(6, 10)}):
                raise ReducerError("invalid paged consultation state policy")
            payload = {**payload, "schema_version": payload["base_state_schema_version"]}
        elif has_paged_data or "consultation_read_policy" in payload or "base_state_schema_version" in payload:
            raise ReducerError("paged consultation state cannot be downgraded")
        if payload.get("schema_version") not in {"minicode-runtime-state-v3", "minicode-runtime-state-v4", "minicode-runtime-state-v5", "minicode-runtime-state-v6", "minicode-runtime-state-v7", "minicode-runtime-state-v8", "minicode-runtime-state-v9"}:
            raise RuntimeStateLoadError(
                "Runtime resume requires a minicode-runtime-state-v3, v4, v5, v6, v7, v8 or v9 snapshot; "
                "start a new run instead"
            )
        plans = {
            int(key): _plan_from_dict(value)
            for key, value in dict(payload.get("plans", {})).items()
            if isinstance(value, Mapping)
        }
        current_version = int(payload.get("current_plan_version", 0))
        if current_version not in plans:
            raise ReducerError("snapshot has no current PlanVersion")
        for version, restored_plan in plans.items():
            if restored_plan.plan_version != version:
                raise ReducerError("snapshot PlanVersion key does not match its payload")
        snapshot_run_id = str(payload.get("run_id", ""))
        if not snapshot_run_id or any(
            restored_plan.run_id != snapshot_run_id for restored_plan in plans.values()
        ):
            raise ReducerError("snapshot run_id does not match its PlanVersions")
        snapshot_contract_revision = int(payload.get("contract_revision", 0))
        if snapshot_contract_revision != plans[current_version].contract_revision:
            raise ReducerError("snapshot contract revision does not match current PlanVersion")
        shared_budget = payload.get("schema_version") == "minicode-runtime-state-v9"
        if ((shared_budget and payload.get("consultation_budget_policy") != "shared-pool-v1")
            or (not shared_budget and ("consultation_budget_policy" in payload or "required_consultation_enabled" in payload))):
            raise ReducerError("consultation budget policy does not match the snapshot schema")
        if shared_budget and type(payload.get("required_consultation_enabled")) is not bool:
            raise ReducerError("shared budget snapshot requires an explicit consultation contract marker")
        process_enabled = payload["required_consultation_enabled"] if shared_budget else payload.get("schema_version") == "minicode-runtime-state-v8"
        closure_enabled = shared_budget or process_enabled or payload.get("schema_version") == "minicode-runtime-state-v7"
        if (closure_enabled and not isinstance(payload.get("closure_budget"), dict)) or (not closure_enabled and "closure_budget" in payload):
            raise ReducerError("closure budget policy does not match the snapshot schema")
        advisory_enabled = shared_budget or process_enabled or payload.get("schema_version") in {"minicode-runtime-state-v6", "minicode-runtime-state-v7"}
        if (advisory_enabled and payload.get("advisory_enabled") is not True) or (not advisory_enabled and "advisory_enabled" in payload):
            raise ReducerError("advisory consultation policy does not match the snapshot schema")
        candidate_first = payload.get("schema_version") == "minicode-runtime-state-v5" or (advisory_enabled and payload.get("candidate_first") is True)
        if (candidate_first and payload.get("candidate_first") is not True) or (not candidate_first and "candidate_first" in payload):
            raise ReducerError("candidate delivery policy does not match the snapshot schema")
        enabled = shared_budget or process_enabled or payload.get("schema_version") in {"minicode-runtime-state-v4", "minicode-runtime-state-v5", "minicode-runtime-state-v6", "minicode-runtime-state-v7"}
        restored_contract = _contract_from_dict(payload["contract"]) if isinstance(payload.get("contract"), Mapping) else None
        if process_enabled != bool(restored_contract and restored_contract.required_consultation):
            raise ReducerError("required consultation contract does not match the snapshot schema")
        limit = payload.get("continuation_limit", 16)
        repair_limit = payload.get("repair_limit", 0)
        initial_plan = plans[current_version]
        if candidate_first:
            builders = [node for node in initial_plan.nodes if node.is_writer and node.node_id != "primary"]
            for node in builders:
                registrations = [event.get("payload", {}) for event in payload.get("events", [])
                    if event.get("event_type") == RuntimeEventType.NODE_READY.value
                    and event.get("payload", {}).get("node_id") == node.node_id
                    and event.get("payload", {}).get("delegated") is True]
                if (not node.node_id.startswith("builder-") or node.role != "writer"
                    or payload.get("delegation_parents", {}).get(node.node_id) != "primary"
                    or not any(r.get("definition") == node.to_dict() and r.get("creation_parent") == "primary" for r in registrations)):
                    raise ReducerError("restored Builder lacks its exact durable primary registration")
            initial_plan = replace(initial_plan, nodes=tuple(n for n in initial_plan.nodes if n not in builders))
        reducer = cls(initial_plan, clock=clock, store=None, continuations_enabled=enabled,
                      contract=restored_contract, continuation_limit=limit, repair_limit=repair_limit,
                      candidate_first=candidate_first, advisory_enabled=advisory_enabled, closure_reserve_enabled=closure_enabled,
                      shared_consultation_budget=shared_budget)
        if closure_enabled and json.dumps(payload["closure_budget"], sort_keys=True) != json.dumps(reducer._state.closure_budget, sort_keys=True):
            raise ReducerError("persisted closure reserves do not match the TaskContract and final verifier")
        nodes: dict[str, NodeRuntime] = {}
        raw_nodes = payload.get("nodes", {})
        if not isinstance(raw_nodes, Mapping):
            raise ReducerError("snapshot nodes must be a mapping")
        for node_id, raw in raw_nodes.items():
            if not isinstance(raw, Mapping):
                continue
            definition = _node_from_dict(raw.get("definition", {}))
            attempts = [
                _attempt_from_dict(item)
                for item in raw.get("attempts", [])
                if isinstance(item, Mapping)
            ]
            raw_result = raw.get("result")
            if type(raw.get("continuation_count", 0)) is not int:
                raise ReducerError("persisted continuation count must be an integer")
            state = NodeState(str(raw.get("state", NodeState.PENDING.value)))
            nodes[str(node_id)] = NodeRuntime(
                definition=definition,
                state=state,
                attempts=attempts,
                current_attempt_id=(
                    str(raw.get("current_attempt_id"))
                    if raw.get("current_attempt_id") is not None
                    else None
                ),
                result=_result_from_dict(raw_result) if isinstance(raw_result, Mapping) else None,
                rejection_reasons=[str(item) for item in raw.get("rejection_reasons", [])],
                resume_credits=int(raw.get("resume_credits", 0)),
                continuation=_continuation_from_dict(raw["continuation"]) if isinstance(raw.get("continuation"), Mapping) else None,
                continuation_count=int(raw.get("continuation_count", 0)),
                advisory_acknowledgment=raw.get("advisory_acknowledgment"),
                workspace_committed=bool(
                    raw.get(
                        "workspace_committed",
                        not (state is NodeState.VERIFYING and definition.is_writer),
                    )
                ),
            )
        current_definitions = {
            node.node_id: node for node in plans[current_version].nodes
        }
        if set(nodes) != set(current_definitions) or any(
            runtime.definition != current_definitions[node_id]
            for node_id, runtime in nodes.items()
        ):
            raise ReducerError("snapshot nodes do not match current PlanVersion")
        restored_contract = (
            _contract_from_dict(payload["contract"])
            if isinstance(payload.get("contract"), Mapping)
            else None
        )
        restored_identity = (
            _identity_from_dict(payload["identity"])
            if isinstance(payload.get("identity"), Mapping)
            else None
        )
        if restored_contract is not None and (
            restored_contract.envelope.run_id != snapshot_run_id
            or restored_contract.contract_revision != snapshot_contract_revision
        ):
            raise ReducerError("snapshot TaskContract does not match Runtime state")
        if restored_identity is not None and restored_identity.run_id != snapshot_run_id:
            raise ReducerError("snapshot RunIdentity does not match Runtime state")
        events = [
            _event_from_dict(item)
            for item in payload.get("events", [])
            if isinstance(item, Mapping)
        ]
        reducer._state = RuntimeState(
            run_id=snapshot_run_id,
            contract_revision=snapshot_contract_revision,
            current_plan_version=current_version,
            plans=plans,
            nodes=nodes,
            contract=restored_contract,
            identity=restored_identity,
            final_verification_records=[
                _verification_from_dict(item)
                for item in payload.get("final_verification_records", [])
                if isinstance(item, Mapping)
            ],
            events=events,
            canceled=bool(payload.get("canceled", False)),
            paused=bool(payload.get("paused", False)),
            processed_commands={
                str(key): tuple(str(item) for item in value)
                for key, value in dict(payload.get("processed_commands", {})).items()
                if isinstance(value, list)
            },
            consumed_approvals={
                str(item) for item in payload.get("consumed_approvals", [])
            },
            next_fencing_token=int(payload.get("next_fencing_token", 1)),
            tokens_used=max(0, int(payload.get("tokens_used", 0) or 0)),
            cost_used=max(0.0, float(payload.get("cost_used", 0.0) or 0.0)),
            usage_ledger=UsageLedger.restore(payload.get("usage_ledger", {})),
            continuations_enabled=enabled,
            continuation_limit=limit,
            repair_limit=repair_limit,
            repair_history=[_repair_from_dict(item) for item in payload.get("repair_history", [])],
            pending_repair_id=payload.get("pending_repair_id"),
            candidate_first=candidate_first,
            integration_status=payload.get("integration_status", "idle"),
            integration_revision=payload.get("integration_revision"),
            integration_error=payload.get("integration_error", ""),
            advisory_enabled=advisory_enabled,
            closure_budget=reducer._state.closure_budget,
            shared_consultation_budget=shared_budget,
            delegation_parents={str(k): str(v) for k, v in
                                dict(payload.get("delegation_parents", {})).items()},
            node_cancel_epoch={str(k): int(v) for k, v in
                               dict(payload.get("node_cancel_epoch", {})).items()},
            cancel_operations={str(k): dict(v) for k, v in
                               dict(payload.get("cancel_operations", {})).items()
                               if isinstance(v, Mapping)},
            dependency_predicates={str(k): {str(a): str(b) for a, b in dict(v).items()}
                                   for k, v in dict(payload.get("dependency_predicates", {})).items()
                                   if isinstance(v, Mapping)},
            child_outcomes={str(k): dict(v) for k, v in
                            dict(payload.get("child_outcomes", {})).items()
                            if isinstance(v, Mapping)},
            dependency_waits={str(k): dict(v) for k, v in
                              dict(payload.get("dependency_waits", {})).items()
                              if isinstance(v, Mapping)},
            wait_resumes={str(k): int(v) for k, v in dict(payload.get("wait_resumes", {})).items()},
            patch_records={str(k): dict(v) for k, v in
                           dict(payload.get("patch_records", {})).items()
                           if isinstance(v, Mapping)},
            worker_completions=deepcopy(dict(payload.get("worker_completions", {}))),
            candidate_quarantines=deepcopy(dict(payload.get("candidate_quarantines", {}))),
            candidate_selections=deepcopy(dict(payload.get("candidate_selections", {}))),
            async_agents_enabled=async_agents,
            shared_workspace_writes_enabled=payload.get("agent_session_policy") == "async-agents-shared-writes-v1",
            agent_sessions=deepcopy(payload.get("agent_sessions", {})),
            agent_messages_enabled=agent_messages,
            agent_resume_enabled=agent_resume,
            agent_collaboration=deepcopy(payload.get("agent_collaboration")) if continuous else None,
            agent_fork_enabled=agent_fork,
            coordinator_enabled=capabilities.get("coordinator", False),
            agent_team_writes_enabled=capabilities.get("team_writes", False),
            agent_team=deepcopy(payload.get("agent_team")) if agent_teams else None,
            agent_messages=deepcopy(payload.get("agent_messages", {})),
            agent_checkpoints=deepcopy(payload.get("agent_checkpoints", {})),
            user_steering=deepcopy(payload.get("user_steering")) if human_steering else None,
            agent_finishing=deepcopy(payload.get("agent_finishing", {})),
            review_runtime=deepcopy(review_runtime) if dynamic_review else None,
            # A legacy snapshot carries no revision. It is adopted as revision 1
            # rather than invented as "current": the next save stamps revision 2
            # and is still CAS-checked against whatever is really on disk.
            revision=RuntimeStateStore.payload_revision(payload) or 1,
        )
        # Older snapshots have already charged their accepted AgentResults.
        reducer._state.usage_ledger.result_attempts.update(
            runtime.result.attempt_id for runtime in nodes.values() if runtime.result is not None
        )
        for runtime in nodes.values():
            result = runtime.result
            if result is not None and not reducer._state.usage_ledger.has_calls(result.attempt_id):
                reducer._state.usage_ledger.result_usage.setdefault(result.attempt_id, dict(result.usage))
        if closure_enabled:
            totals = reducer._state.usage_ledger.summary()
            raw_tokens, raw_cost = payload.get("tokens_used"), payload.get("cost_used")
            if (type(raw_tokens) is not int or raw_tokens != totals["tokens"]
                or type(raw_cost) not in {int, float} or not math.isfinite(raw_cost)
                or raw_cost != totals["known_cost"]):
                raise ReducerError("v7 cumulative usage counters do not match the durable ledger")
        reducer._validate_restored_continuations()
        reducer._validate_restored_repairs()
        reducer._validate_restored_consultation_recoveries()
        reducer._validate_dynamic_review()
        reducer._validate_restored_advisory()
        reducer._validate_restored_integration()
        reducer._validate_candidate_lifecycle()
        from minicode.builder_recovery import validate_record
        for node_id, recovery in reducer.builder_recoveries().items():
            validate_record(recovery, node=reducer._node(node_id).definition, nodes=reducer._state.nodes,
                patches=reducer._state.patch_records, recoveries=reducer.builder_recoveries())
        reducer._validate_agent_sessions()
        reducer.validate_collaboration_state()
        reducer._validate_agent_messages()
        reducer._validate_agent_team()
        if human_steering:
            from minicode.runtime_steering import validate_state
            validate_state(reducer._state.user_steering, run_id=reducer.run_id, nodes=nodes,
                           events=reducer._state.events, parents=reducer._state.delegation_parents)
        reducer._store = store
        # Adopt the durable revision as the one this writer last observed, so the
        # very first persist of a resumed run is compare-and-swap checked instead
        # of blindly replacing whatever another process wrote in the meantime.
        if store is not None:
            store.adopt(durable_payload)
        return reducer

    @property
    def continuations_enabled(self) -> bool:
        return self._state.continuations_enabled

    @property
    def async_agents_enabled(self) -> bool:
        return self._state.async_agents_enabled

    @property
    def shared_workspace_writes_enabled(self) -> bool:
        return self._state.shared_workspace_writes_enabled

    def agent_sessions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._state.agent_sessions)

    @property
    def agent_resume_enabled(self) -> bool:
        return self._state.agent_resume_enabled

    def _agent_member_tasks(self) -> dict[str, list[str]]:
        members: dict[str, list[str]] = {}
        for task_id, parent in self._state.delegation_parents.items():
            if parent == "primary" and (task_id in self._state.agent_sessions or task_id.startswith("builder-")):
                session_id = self._state.agent_sessions.get(task_id, {}).get("session_id", self.builder_lineage(task_id)[0])
                members.setdefault(session_id, []).append(task_id)
        return members

    def agent_team_view(self) -> dict | None:
        with self._lock:
            team = self._state.agent_team
            if team is None:
                return None
            members = []
            for session_id, tasks in self._agent_member_tasks().items():
                latest = max(tasks, key=lambda t: self._state.agent_sessions.get(t, {}).get("generation", self.builder_lineage(t)[1]))
                node = self._node(latest)
                members.append({"session_id": session_id, "task_ids": tasks, "current_task_id": latest,
                    "generation": self._state.agent_sessions.get(latest, {}).get("generation", self.builder_lineage(latest)[1]),
                    "role": node.definition.role,
                    "state": "retired" if session_id in team["retired_members"] else "idle" if node.state is NodeState.SUCCEEDED else node.state.value,
                    "worker_exited": bool(self.agent_worker_settlement(latest))})
            return {**deepcopy(team), "members": members}

    @property
    def coordinator_enabled(self) -> bool:
        return self._state.coordinator_enabled

    @property
    def agent_team_writes_enabled(self) -> bool:
        return self._state.agent_team_writes_enabled

    def validate_team_admission(self, session_id: str) -> None:
        with self._lock:
            team = self._state.agent_team
            if team is None:
                return
            if team["state"] != "active" or session_id in team["retired_members"]:
                raise ReducerError("team admission is closed or member is retired")
            active = set(self._agent_member_tasks()) - set(team["retired_members"])
            if session_id not in active and len(active) >= team["member_limit"]:
                raise ReducerError("team member limit reached; retire an exited idle member before replacing it")

    def control_agent_team(self, *, parent_id, attempt_id, action, task_id=None) -> dict:
        with self._lock:
            self.validate_agent_activation(parent_id, attempt_id)
            team = self._state.agent_team
            if parent_id != "primary" or team is None:
                raise ReducerError("only this team's live lead may control it")
            if action == "status":
                return self.agent_team_view()
            if team["state"] not in {"active", "draining"}:
                raise ReducerError("closed teams cannot be reopened")
            if action == "drain":
                team["state"] = "draining"
            elif action == "retire":
                members = self._agent_member_tasks()
                session_id = self._state.agent_sessions.get(task_id, {}).get("session_id", self.builder_lineage(task_id)[0])
                if session_id not in members:
                    raise ReducerError("unknown team member")
                for member_task in members[session_id]:
                    node = self._node(member_task)
                    settlement = self.agent_worker_settlement(member_task)
                    dismissed = member_task in self.dismissed_agent_ids()
                    if ((node.state is not NodeState.SUCCEEDED and member_task not in self.resolved_agent_failures(include_dismissed=True) and not dismissed)
                        or (not settlement and not (dismissed and not node.attempts))):
                        raise ReducerError("retirement requires resolved work and actual worker exit")
                    if (member_task.startswith("builder-") and member_task not in self.replaced_builder_ids()
                        and self._state.patch_records.get("patch-" + member_task, {}).get("state") != "applied"):
                        raise ReducerError("Builder retirement requires its patch disposition")
                team["retired_members"][session_id] = {"by_attempt": attempt_id, "reason": "lead retired idle member"}
            else:
                raise ReducerError("unknown team action")
            self._emit(RuntimeEventType.AGENT_TEAM_CHANGED, deepcopy(team))
            self._persist()
            return self.agent_team_view()

    def finish_agent_team(self, *, delivery_completed: bool) -> None:
        with self._lock:
            team = self._state.agent_team
            if team is None or team["state"] in {"completed", "canceled", "failed"}:
                return
            if delivery_completed:
                if (self.completion_blockers() or self.pending_candidate_quarantines()
                    or not self._state.usage_ledger.settlement_ready(self.contract.budget)):
                    raise ReducerError("team cannot complete with unresolved work or usage")
                for tasks in self._agent_member_tasks().values():
                    for task_id in tasks:
                        # A controlled interruption fences off SUBMIT_RESULT;
                        # exit evidence lives in the settlement ledger even
                        # when a successful successor has resolved that task.
                        settlement = self.agent_worker_settlement(task_id)
                        # Unresolved effects were checked above; a natively
                        # reconciled/retired quarantine must not block forever.
                        if not settlement and not (task_id in self.dismissed_agent_ids() and not self._node(task_id).attempts):
                            raise ReducerError("team cannot complete before worker exit")
                if team["state"] == "active":
                    team["state"] = "draining"
                    self._emit(RuntimeEventType.AGENT_TEAM_CHANGED, deepcopy(team))
                team["state"] = "completed"
            elif self._state.canceled:
                team["state"] = "canceled"
            elif self._node("primary").state in {
                NodeState.FAILED_TERMINAL, NodeState.CANCELED, NodeState.SUPERSEDED,
            } or self.terminal():
                # A failed lead can leave dependent verification pending forever.
                # Close admission without inventing completion for that work.
                team["state"] = "failed"
            else:
                return  # Waiting for user input is not a request to close admission.
            team["resource_disposition"] = {"readonly_snapshots": "release_unchanged_terminal_views_retain_unexpected_edits",
                "checkpoints": "retained_in_artifact_store", "candidates": self._state.integration_status,
                "pending_side_effects": len(self.pending_candidate_quarantines())}
            self._emit(RuntimeEventType.AGENT_TEAM_CHANGED, deepcopy(team))
            self._persist()

    def _validate_agent_team(self) -> None:
        team = self._state.agent_team
        if team is None:
            return
        if (not self.agent_resume_enabled or team.get("run_id") != self.run_id or team.get("lead_id") != "primary"
            or team.get("team_id") != "team-" + hashlib.sha256(self.run_id.encode()).hexdigest()[:24]
            or team.get("state") not in {"active", "draining", "completed", "canceled", "failed"}
            or team.get("member_limit") != (self.collaboration_config or {}).get("max_sessions", 2)
            or not isinstance(team.get("retired_members"), dict)
            or set(team["retired_members"]) - set(self._agent_member_tasks())):
            raise ReducerError("invalid persisted team ownership or lifecycle")

    @property
    def agent_messages_enabled(self) -> bool:
        return self._state.agent_messages_enabled

    def agent_message_records(self) -> tuple[dict, ...]:
        with self._lock:
            return tuple(deepcopy(list(self._state.agent_messages.values())))

    def agent_checkpoint(self, node_id: str) -> dict | None:
        with self._lock:
            return deepcopy(self._state.agent_checkpoints.get(node_id))

    def validate_agent_activation(self, node_id: str, attempt_id: str) -> None:
        attempt = self.current_attempt(node_id)
        if (not self.agent_messages_enabled or self._state.canceled or self._state.paused
            or self.node_cancel_epoch(node_id) or self._node(node_id).state is not NodeState.RUNNING
            or attempt is None or attempt.attempt_id != attempt_id or not attempt.lease.is_valid(self._clock.now())
            or (self._state.agent_team is not None and self._state.agent_team["state"] in {"completed", "canceled", "failed"})
            or (node_id != "primary" and node_id not in self._state.agent_sessions
                and not (self._state.agent_team is not None and node_id.startswith("builder-")
                         and self._state.delegation_parents.get(node_id) == "primary"))):
            raise ReducerError("agent operation requires the current live activation")

    def queue_agent_message(self, *, sender_id, attempt_id, recipient_id, message_id, kind, content, content_hash,
                            make_body_artifact=None, reply_to=None) -> dict:
        with self._lock:
            self.validate_agent_activation(sender_id, attempt_id)
            recipient_handle = recipient_id
            if self.collaboration_config:
                original = self._state.agent_messages.get(f"{sender_id}:{message_id}")
                if original is not None:
                    if not self.agent_message_route_allowed(sender_id, original["recipient_id"]):
                        raise ReducerError("independent verifiers cannot replay collaboration messages")
                    if (original.get("recipient_handle", original["recipient_id"]) != recipient_handle
                        or original["kind"] != kind or original["content_hash"] != content_hash or original.get("reply_to") != reply_to):
                        raise ReducerError("message id conflicts with its original content or route")
                    return deepcopy(original)
                recipient_id = self.latest_agent_task(recipient_handle)
            if not self.agent_message_route_allowed(sender_id, recipient_id):
                raise ReducerError("messages require an authorized parent-child or peer route; independent verifiers remain isolated")
            if reply_to is not None:
                previous_message = self._state.agent_messages.get(reply_to)
                session = lambda key: self._state.agent_sessions.get(key, {}).get("session_id", key)
                if (not self.collaboration_config or previous_message is None or previous_message["state"] != "consumed"
                    or session(previous_message["sender_id"]) != session(recipient_id)
                    or session(previous_message["recipient_id"]) != session(sender_id)):
                    raise ReducerError("reply_to must identify a consumed message between these two logical sessions")
            if kind not in ({"instruction"} if sender_id == "primary" else {"progress", "question", "result"}):
                raise ReducerError("message kind does not match its sender role")
            if (not isinstance(content, str) or not content.strip() or len(content) > 60000
                or not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 128):
                raise ReducerError("message body or identity exceeds its bounded input limits")
            key = f"{sender_id}:{message_id}"
            identity = {"sender_id": sender_id, "recipient_id": recipient_id,
                "message_id": message_id, "kind": kind, "content_hash": content_hash}
            existing = self._state.agent_messages.get(key)
            if existing:
                if any(existing.get(k) != v for k, v in identity.items()):
                    raise ReducerError("message id conflicts with its original content or route")
                return deepcopy(existing)
            target = self._node(recipient_id)
            if self.node_cancel_epoch(recipient_id) or target.state in {NodeState.CANCELED, NodeState.SUPERSEDED}:
                raise ReducerError("recipient is canceled or superseded")
            finished = not self.agent_message_recipient_available(recipient_id)
            records = self._state.agent_messages
            pending = sum(r["recipient_id"] == recipient_id and r["state"] == "queued" for r in records.values())
            limits = self.collaboration_config or {}
            if len(records) >= limits.get("max_messages", 128):
                raise ReducerError("cumulative agent message quota exhausted; consuming messages does not reset the run budget")
            if pending >= limits.get("max_pending_messages", 32):
                raise ReducerError("recipient pending message quota exhausted; wait for consumption before sending more")
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash:
                raise ReducerError("message content hash mismatch")
            body_artifact = None
            if len(content) > 6000:
                from minicode.runtime_artifacts import RuntimeArtifactStore
                if make_body_artifact is None:
                    raise ReducerError("long message requires an owned body artifact")
                body_artifact = make_body_artifact()
                if (not RuntimeArtifactStore.owns_reference(body_artifact, run_id=self.run_id, node_id=sender_id)
                    or body_artifact.content_hash != content_hash or body_artifact.size != len(content.encode("utf-8"))
                    or body_artifact.media_type != "text/plain; charset=utf-8"):
                    raise ReducerError("message body artifact does not match its sender and content")
                content = "Message body stored as an artifact. Read every page with runtime_artifact_read before acting."
            record = {**identity, "run_id": self.run_id, "content": content, "state": "queued", "sequence": len(records) + 1,
                "team_id": self._state.agent_team["team_id"] if self._state.agent_team else None,
                "created_at": self._clock.now(), "recipient_attempt_id": target.current_attempt_id,
                "sender_attempt_id": attempt_id, "contract_revision": self.current_plan.contract_revision}
            if self.collaboration_config:
                record["recipient_handle"] = recipient_handle
                if reply_to is not None:
                    record["reply_to"] = reply_to
            if body_artifact is not None:
                record["body_artifact"] = body_artifact.to_dict()
            if finished:
                record.update(state="rejected", reason="recipient finished; an explicit resume is required")
            records[key] = record
            self._emit(RuntimeEventType.AGENT_MESSAGE_REJECTED if finished else RuntimeEventType.AGENT_MESSAGE_QUEUED, deepcopy(record))
            self._wake_agent_questions()
            self._persist()
            return deepcopy(record)

    def agent_boundary(self, *, node_id, attempt_id, make_checkpoint, final=False) -> dict:
        """Order inbox acceptance, recoverable consumption and success sealing."""
        from minicode.runtime_artifacts import RuntimeArtifactStore
        with self._lock:
            self.validate_agent_activation(node_id, attempt_id)
            # Keep old queued routes audited, but apply today's communication
            # boundary before accepting their bodies into a new checkpoint.
            self._reject_pending_agent_messages({node_id}, "agent communication route is disabled", invalid_routes_only=True)
            pending = [r for r in self._state.agent_messages.values()
                if r["recipient_id"] == node_id and r["state"] == "queued"]
            notices = self.pending_agent_notices(node_id)
            checkpoint = make_checkpoint(deepcopy(pending + notices))
            if (not isinstance(checkpoint, Mapping) or not isinstance(checkpoint.get("artifact"), Mapping)
                or not RuntimeArtifactStore.owns_reference(_artifact_from_dict(checkpoint["artifact"]),
                    run_id=self.run_id, node_id=node_id)):
                raise ReducerError("message consumption needs its owned recoverable checkpoint")
            self._state.agent_checkpoints[node_id] = {**deepcopy(dict(checkpoint)), "attempt_id": attempt_id}
            for record in pending:
                record.update(state="consumed", consumed_attempt_id=attempt_id, checkpoint=deepcopy(checkpoint["artifact"]))
                self._emit(RuntimeEventType.AGENT_MESSAGE_CONSUMED, {"node_id": node_id,
                    "message_id": record["message_id"], "sequence": record["sequence"], "attempt_id": attempt_id})
            self.record_agent_notices(notices, checkpoint, attempt_id)
            sealed = final and not pending and not notices
            if sealed:
                self._state.agent_finishing[node_id] = attempt_id
            self._emit(RuntimeEventType.AGENT_CHECKPOINT_RECORDED, {"node_id": node_id,
                "attempt_id": attempt_id, "checkpoint": deepcopy(dict(checkpoint)), "sealed": sealed})
            self._persist()
            return {"consumed": len(pending) + len(notices), "sealed": sealed}

    def _reject_pending_agent_messages(self, node_ids, reason, *, invalid_routes_only=False):
        for record in self._state.agent_messages.values():
            if record["recipient_id"] in node_ids and record["state"] == "queued":
                if invalid_routes_only and self.agent_message_route_allowed(record["sender_id"], record["recipient_id"]):
                    continue
                record.update(state="rejected", reason=reason)
                self._emit(RuntimeEventType.AGENT_MESSAGE_REJECTED, {
                    "node_id": record["recipient_id"], "message_id": record["message_id"], "reason": reason})

    def seal_agent_handoff(self, *, node_id: str, attempt_id: str) -> None:
        """Order an authenticated closing tool against message admission."""
        with self._lock:
            if not self.agent_messages_enabled:
                return
            self.validate_agent_activation(node_id, attempt_id)
            if (any(r["recipient_id"] == node_id and r["state"] == "queued" for r in self._state.agent_messages.values())
                or self.pending_agent_notices(node_id)):
                raise ReducerError("accepted agent messages must enter the next safe context boundary before handoff")
            self._state.agent_finishing[node_id] = attempt_id
            self._emit(RuntimeEventType.AGENT_CHECKPOINT_RECORDED, {"node_id": node_id,
                "attempt_id": attempt_id, "sealed": True, "reason": "native_candidate_handoff"})
            self._persist()

    def _validate_agent_messages(self) -> None:
        from minicode.runtime_artifacts import RuntimeArtifactStore
        if self.agent_messages_enabled and not self.async_agents_enabled:
            raise ReducerError("message state requires async agent authority")
        sequences = set()
        members = {task for tasks in self._agent_member_tasks().values() for task in tasks} if self._state.agent_team else set(self._state.agent_sessions)
        for key, record in self._state.agent_messages.items():
            sender, recipient = record.get("sender_id"), record.get("recipient_id")
            body = record.get("body_artifact")
            if body is not None:
                ref = _artifact_from_dict(body)
                if (not RuntimeArtifactStore.owns_reference(ref, run_id=self.run_id, node_id=sender)
                    or ref.size > 240000 or ref.media_type != "text/plain; charset=utf-8"
                    or ref.content_hash != record.get("content_hash")
                    or record.get("content") != "Message body stored as an artifact. Read every page with runtime_artifact_read before acting."
                    or not any(event.event_type in {RuntimeEventType.AGENT_MESSAGE_QUEUED, RuntimeEventType.AGENT_MESSAGE_REJECTED}
                        and event.payload.get("sender_id") == sender and event.payload.get("message_id") == record.get("message_id")
                        and event.payload.get("body_artifact") == body for event in self._state.events)):
                    raise ReducerError("invalid owned message body artifact")
            if (key != f"{sender}:{record.get('message_id')}" or record.get("state") not in {"queued", "consumed", "rejected"}
                or record.get("run_id") != self.run_id
                or not isinstance(record.get("content"), str)
                or (body is None and hashlib.sha256(record["content"].encode("utf-8")).hexdigest() != record.get("content_hash"))
                or not isinstance(record.get("message_id"), str) or not 1 <= len(record["message_id"]) <= 128
                or not 1 <= len(record["content"]) <= 6000
                or type(record.get("sequence")) is not int or record["sequence"] <= 0
                or record.get("sequence") in sequences
                or record.get("kind") not in ({"instruction"} if sender == "primary" else {"progress", "question", "result"})
                or record.get("team_id") != (self._state.agent_team["team_id"] if self._state.agent_team else None)
                or not ((sender == "primary" and recipient in members)
                    or (recipient == "primary" and sender in members) or self.peer_route_allowed(sender, recipient))
                or (record["state"] == "consumed" and not record.get("checkpoint"))):
                raise ReducerError("invalid persisted parent-child message")
            sequences.add(record["sequence"])
            if self.collaboration_config:
                handle = record.get("recipient_handle", recipient)
                session = lambda target: self._state.agent_sessions.get(target, {}).get("session_id", target)
                prior = self._state.agent_messages.get(record.get("reply_to"))
                if ((handle != recipient and handle != session(recipient))
                    or (record.get("reply_to") is not None and (prior is None or prior["state"] != "consumed"
                        or prior["sequence"] >= record["sequence"]
                        or session(prior["sender_id"]) != session(recipient)
                        or session(prior["recipient_id"]) != session(sender)))
                    or not any(event.event_type in {RuntimeEventType.AGENT_MESSAGE_QUEUED, RuntimeEventType.AGENT_MESSAGE_REJECTED}
                        and all(event.payload.get(field) == record.get(field) for field in (
                            "sender_id", "recipient_id", "message_id", "kind", "content_hash", "sender_attempt_id",
                            "recipient_handle", "reply_to", "sequence")) for event in self._state.events)):
                    raise ReducerError("message route and reply metadata lack their native delivery event")
            if not any(a.attempt_id == record.get("sender_attempt_id") for a in self._node(sender).attempts):
                raise ReducerError("message sender activation is not registered")
            if record["state"] == "consumed":
                if (not any(a.attempt_id == record.get("consumed_attempt_id") for a in self._node(recipient).attempts)
                    or not RuntimeArtifactStore.owns_reference(_artifact_from_dict(record["checkpoint"]), run_id=self.run_id, node_id=recipient)
                    or not any(event.event_type is RuntimeEventType.AGENT_MESSAGE_CONSUMED
                        and event.payload.get("node_id") == recipient and event.payload.get("sequence") == record["sequence"]
                        and event.payload.get("attempt_id") == record["consumed_attempt_id"] for event in self._state.events)):
                    raise ReducerError("message consumption lacks its owned checkpoint and registered event")
            if record["state"] == "queued" and self._node(recipient).state in {NodeState.CANCELED, NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.SUPERSEDED}:
                raise ReducerError("terminal recipient cannot retain queued instructions")
        for node_id, checkpoint in self._state.agent_checkpoints.items():
            if (node_id not in self._state.nodes or not any(a.attempt_id == checkpoint.get("attempt_id") for a in self._node(node_id).attempts)
                or not RuntimeArtifactStore.owns_reference(_artifact_from_dict(checkpoint["artifact"]), run_id=self.run_id, node_id=node_id)
                or not any(event.event_type is RuntimeEventType.AGENT_CHECKPOINT_RECORDED
                    and event.payload.get("node_id") == node_id and event.payload.get("attempt_id") == checkpoint["attempt_id"]
                    and event.payload.get("checkpoint") == {k: v for k, v in checkpoint.items() if k != "attempt_id"}
                    for event in self._state.events)):
                raise ReducerError("checkpoint does not identify a registered activation")
        for node_id, attempt_id in self._state.agent_finishing.items():
            if node_id not in self._state.nodes or not any(event.event_type is RuntimeEventType.AGENT_CHECKPOINT_RECORDED
                and event.payload.get("node_id") == node_id and event.payload.get("attempt_id") == attempt_id
                and event.payload.get("sealed") is True for event in self._state.events):
                raise ReducerError("agent handoff seal lacks an authenticated boundary event")

    def _validate_agent_sessions(self) -> None:
        if self.shared_workspace_writes_enabled and (not self.async_agents_enabled or self.candidate_first
            or self.coordinator_enabled or (self._state.agent_team is not None and not self.agent_team_writes_enabled)):
            raise ReducerError("invalid persisted shared writer policy")
        if self.coordinator_enabled and (not self.async_agents_enabled or self._state.agent_fork_enabled):
            raise ReducerError("persisted Coordinator has incompatible capabilities")
        if self.agent_team_writes_enabled and self._state.agent_team is None:
            raise ReducerError("persisted team write authority has no team")
        if self.async_agents_enabled and (not self.continuations_enabled or self.contract is None):
            raise ReducerError("async agent state requires a durable contracted run")
        sessions = self._state.agent_sessions
        if {n for n in self._state.nodes if n.startswith("agent-")} != set(sessions):
            raise ReducerError("async agents require exact persisted session registrations")
        from minicode.agent_workspaces import valid_agent_workspace, valid_agent_assignment
        for node_id, record in sessions.items():
            node = self._node(node_id).definition
            if not self.collaboration_config and ("name" in record or "checks" in record or "optional" in record):
                raise ReducerError("continuous agent assignment cannot be downgraded")
            if (not isinstance(record, dict) or record.get("task_id") != node_id
                or record.get("parent_node_id") != self._state.delegation_parents.get(node_id)
                or record.get("parent_node_id") != "primary"
                or record.get("definition") != node.to_dict()
                or record.get("contract_revision") != self.current_plan.contract_revision
                or not valid_agent_assignment(node, record, shared_writes=self.shared_workspace_writes_enabled)
                or (node.role == "shared_writer" and not self._node("primary").definition.is_writer)
                or not record.get("idempotency_key")
                or not valid_agent_workspace(record)
                or not any(event.payload.get("agent_session") == record
                    for event in self._state.events if event.event_type is RuntimeEventType.NODE_READY)):
                raise ReducerError("invalid async agent registration")
            previous_id = record.get("predecessor_task_id") or record.get("replaces_task_id")
            if record.get("context_mode") == "fork":
                if (not self._state.agent_fork_enabled or not record.get("fork_checkpoint")
                    or self._node("primary").definition.allowed_effects != (ToolEffect.READ_ONLY,)
                    or node.role == "verifier"):
                    raise ReducerError("fork context requires a compatible read-only primary")
            if previous_id:
                previous = sessions.get(previous_id, {})
                if (not self.agent_resume_enabled or record.get("session_id") != previous.get("session_id")
                    or record.get("optional", False) != previous.get("optional", False)
                    or record.get("generation") != previous.get("generation", 0) + 1
                    or record.get("generation", 999) > self.agent_generation_limit
                    or sum((r.get("predecessor_task_id") or r.get("replaces_task_id")) == previous_id for r in sessions.values()) != 1):
                    raise ReducerError("invalid continued agent generation")
                if record.get("replaces_task_id"):
                    self.validate_agent_reassignment(previous_id)
                    if record.get("resume_checkpoint") or record.get("predecessor_task_id") or node.objective != previous["definition"]["objective"]:
                        raise ReducerError("failed work requires fresh context and the unchanged obligation")
                elif not record.get("resume_checkpoint"):
                    raise ReducerError("continued context requires an owned checkpoint")
            elif record.get("recovery_source"):
                from minicode.agent_recovery import validate_recovery_record
                if not self.agent_resume_enabled:
                    raise ReducerError("retained agents require continuation support")
                validate_recovery_record(record, run_id=self.run_id, generation_limit=self.agent_generation_limit)
                if sum(r.get("recovery_source", {}).get("session_id") == record["session_id"] for r in sessions.values()) != 1:
                    raise ReducerError("retained agent session was imported twice")
            elif record.get("context_mode") == "retained" or record.get("session_id") != node_id or record.get("generation") != 1:
                raise ReducerError("invalid initial agent session identity")

    @property
    def advisory_enabled(self) -> bool:
        return self._state.advisory_enabled

    @property
    def closure_reserve_enabled(self) -> bool:
        return self._state.closure_budget is not None

    @property
    def shared_consultation_budget(self) -> bool:
        return self._state.shared_consultation_budget

    def uses_shared_consultation_budget(self, node_id: str) -> bool:
        """Only durable read-only consultations use the persisted pool policy.

        Synchronous task delegations retain their explicit child budget. This
        policy also never raises a caller-supplied attempt limit in the ledger.
        """
        with self._lock:
            if not self.shared_consultation_budget or not node_id.startswith("consult-"):
                return False
            node = self._node(node_id).definition
            return node.role in {"explorer", "planner", "reviewer", "verifier"} and node.allowed_effects == (ToolEffect.READ_ONLY,)

    def consultation_worker_budget(self, budget: BudgetEnvelope) -> BudgetEnvelope:
        from minicode.consultation_forecast import CONSULTATION_TOKEN_CAP

        # The run ledger, including all previous attempts and held requests,
        # bounds pooled work. No per-helper allocation is minted or refunded.
        run_limit = self._state.contract.budget.max_tokens
        # An unbounded run must not acquire the legacy 16k helper ceiling.
        # Keep explicit attempt caps, cost/deadline limits, and the shared ledger;
        # bounded historical runs still retain their original helper policy.
        limit = run_limit if self.shared_consultation_budget or run_limit is None else CONSULTATION_TOKEN_CAP
        ceilings = [value for value in (budget.max_tokens, limit) if value is not None]
        return replace(budget, max_tokens=min(ceilings) if ceilings else None,
                       max_active_workers=1, max_delegation_depth=0)

    @staticmethod
    def _resolve_closure_budget(plan: PlanVersion, contract: TaskContract) -> dict[str, Any]:
        from Package.AgentRuntime.Src.Application.Dto.ClosureBudget import ClosureBudgetRequest
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

        verifier = next((node for node in plan.nodes if node.node_id == "final-verifier"), None)
        if verifier is not None and (verifier.role != "verifier" or verifier.criticality != "required"
                                     or verifier.allowed_effects != (ToolEffect.READ_ONLY,)):
            raise ReducerError("closure reserves require a read-only required final verifier")
        return asdict(createApp().resolve_closure_budget(ClosureBudgetRequest(
            contract.budget.max_tokens, contract.budget.max_cost,
            verifier is not None and (verifier.review_policy is None or verifier.review_policy["required"]),
        )))

    def effective_model_budget(self, node_id: str, fallback: BudgetEnvelope) -> BudgetEnvelope:
        """Absolute run-level ceilings; held concurrent calls count against them."""
        with self._lock:
            budget = self._state.contract.budget if self._state.contract else fallback
            reserve = self._state.closure_budget
            if reserve is None or node_id == "final-verifier":
                return budget
            helper = node_id != "primary"
            tokens = (reserve["verification_tokens"] or 0) + ((reserve["primary_tokens"] or 0) if helper else 0)
            cost = add_amounts(reserve["verification_cost"] or 0, (reserve["primary_cost"] or 0) if helper else 0)
            return replace(budget,
                max_tokens=None if budget.max_tokens is None else max(0, budget.max_tokens - tokens),
                max_cost=None if budget.max_cost is None else max(0.0, add_amounts(budget.max_cost, -cost)))

    def closure_budget_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._state.closure_budget) if self.closure_reserve_enabled else None

    def admit_consultation_budget(self, parent_id: str, attempt_id: str, objectives: tuple[str, ...],
                                  *, helper_token_limits: tuple[int | None, ...] | None = None,
                                  shared_pool: bool = False) -> dict[str, Any]:
        """Check the live ledger; absent attempt ceilings inherit the run pool.

        This is not a spending reservation. Explicit ceilings and the persisted
        durable-consultation policy remain authoritative for bounded callers.
        """
        from Package.AgentRuntime.Src.Application.Dto.ClosureBudget import ClosureBudget, ConsultationBudgetRequest
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
        from minicode.consultation_forecast import CONSULTATION_TOKEN_CAP, consultation_forecast

        with self._lock:
            if type(shared_pool) is not bool or (shared_pool and not self.shared_consultation_budget):
                raise ReducerError("shared consultation admission requires the persisted pool policy")
            # A synchronous request with no attempt ceiling shares the run's
            # remaining credit. Do not invent a ceiling or reject that request
            # as unbounded while the actual ledger enforces the run limit.
            inherited_pool = bool(objectives) and helper_token_limits == (None,) * len(objectives)
            pooled_admission = shared_pool or inherited_pool
            if not self.closure_reserve_enabled:
                return {"allowed": True}
            attempt = self.current_attempt(parent_id)
            if (parent_id != "primary" or attempt is None or attempt.attempt_id != attempt_id
                or attempt.state is not NodeState.RUNNING or not attempt.lease.is_valid(self._clock.now())
                or self._state.canceled or self._state.paused):
                raise ReducerError("consultation admission requires the live primary attempt")
            budget, ledger = self._state.contract.budget, self._state.usage_ledger
            held_tokens, held_cost = ledger.pending()
            decision = asdict(createApp().admit_consultation_budget(ConsultationBudgetRequest(
                budget.max_tokens, budget.max_cost, ClosureBudget(**self._state.closure_budget),
                self._state.tokens_used, self._state.cost_used, held_tokens, held_cost,
                sum(len(objective.encode("utf-8")) + 1280 for objective in objectives),
                ledger.budget_reconciled(budget),
            )))
            if budget.max_tokens is not None:
                forecast = consultation_forecast(
                    list(ledger.calls.values()), attempt_id=attempt_id, objectives=objectives,
                    helper_token_limits=helper_token_limits if helper_token_limits is not None and not inherited_pool else
                        tuple(min(budget.max_tokens, CONSULTATION_TOKEN_CAP) for _ in objectives),
                    writes_workspace=self._node(parent_id).definition.is_writer,
                )
                if forecast is not None:
                    if pooled_admission:
                        # These are startup planning targets, not disjoint
                        # lifetime ceilings or guaranteed future liabilities.
                        forecast.update(version="consultation-token-forecast-v3",
                            helper_estimate_authority="advisory_startup_scenario")
                    decision["forecast"] = forecast
                    if decision["allowed"] and not forecast["known"]:
                        decision.update(allowed=False, reason="closure reserve forecast unavailable: " + forecast["reason"])
                    elif forecast["known"]:
                        # Explicit-budget callers must fund their helper
                        # ceilings. Pooled requests use the helper amounts only
                        # as a scenario; fixed closure protection still applies.
                        planned = forecast["helper_tokens"] + max(0,
                            forecast["primary_tokens"] - self._state.closure_budget["primary_tokens"])
                        forecast["estimated_outside_fixed_reserves"] = planned
                        forecast["exceeds_available"] = planned > decision["available_tokens"]
                        if not pooled_admission:
                            decision["hard_required_tokens"] = forecast["helper_tokens"]
                        if not pooled_admission and decision["allowed"] and forecast["helper_tokens"] > decision["available_tokens"]:
                            decision.update(allowed=False, reason=
                                "consultation cannot fund bounded helper ceilings; closure reserves protected")
                        if forecast["exceeds_available"]:
                            decision["warning"] = (
                                "The conservative primary-closure scenario exceeds the available budget. "
                                + ("Shared admission protects fixed closure reserves, not guaranteed completion or equal helper shares. "
                                   if pooled_admission else "Admission covers helper ceilings and fixed closure protection, not guaranteed completion. ") +
                                "Keep results concise and avoid redundant reads; every actual request is still hard-budgeted.")
            if pooled_admission:
                decision["allocation_policy"] = "inherited-run-budget-v1" if inherited_pool else "shared-pool-v1"
                decision["allocation_authority"] = "atomic_request_ledger"
            if shared_pool:
                decision["minimum_request_output"] = "full_configured_output"
            if budget.deadline is not None and self._clock.now() >= budget.deadline:
                decision.update(allowed=False, reason="run deadline exhausted")
            if not decision["allowed"]:
                self._emit(RuntimeEventType.CONSULTATION_BUDGET_DECLINED, {
                    "node_id": parent_id, "attempt_id": attempt_id, "scope": "before_dispatch", **decision,
                })
                self._persist()
            return decision

    @property
    def candidate_first(self) -> bool:
        return self._state.candidate_first

    @property
    def integration_status(self) -> str:
        return self._state.integration_status

    @property
    def final_verification_records(self) -> tuple[VerificationRecord, ...]:
        with self._lock:
            return tuple(self._state.final_verification_records)

    @staticmethod
    def _validate_criticality(node: PlanNode, enabled: bool) -> None:
        if node.criticality not in {"required", "advisory"}:
            raise ReducerError("invalid node criticality")
        if node.criticality == "advisory" and (
            not enabled or not node.node_id.startswith("consult-")
            or node.role not in {"explorer", "planner"} or node.dependencies
            or node.allowed_effects != (ToolEffect.READ_ONLY,)
        ):
            raise ReducerError("only v6 independent read-only exploration/planning consultations may be advisory")

    def node_diagnosis(self, node_id: str) -> dict[str, Any]:
        """Project authoritative node/verification state, never just Worker claims."""
        from Package.AgentRuntime.Src.Application.Dto.FailureDiagnosis import FailureDiagnosisRequest
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

        with self._lock:
            runtime = self._node(node_id)
            result = runtime.result
            failure = result.failure if result else None
            rejected_verification = any(
                event.event_type is RuntimeEventType.VERIFICATION_FAILED
                and event.payload.get("node_id") == node_id
                and event.payload.get("attempt_id") == runtime.current_attempt_id
                for event in self._state.events
            ) or bool(result and any(not record.passed for record in result.verification_records))
            kind = failure.kind.value if failure else "VerificationFailed" if rejected_verification else ""
            scoped_limit = kind != "ConsultationBudgetLimited" or (self.closure_reserve_enabled and any(
                event.event_type is RuntimeEventType.CONSULTATION_BUDGET_DECLINED
                and event.payload.get("scope") == "helper_request"
                and event.payload.get("node_id") == node_id
                and event.payload.get("attempt_id") == runtime.current_attempt_id
                for event in self._state.events
            ))
            decision = createApp().classify_failure(FailureDiagnosisRequest(
                runtime.state.value, kind, runtime.definition.role,
                tuple(effect.value for effect in runtime.definition.allowed_effects),
                runtime.definition.criticality, node_id.startswith("consult-") and self.advisory_enabled and scoped_limit,
                bool(failure and failure.ambiguous_side_effect), rejected_verification,
            ))
            return {"category": decision.category, "failure_kind": kind,
                    "fallback_allowed": decision.fallback_allowed,
                    "acknowledged": runtime.advisory_acknowledgment is not None}

    def validate_agent_reassignment(self, task_id: str) -> None:
        with self._lock:
            record = self._state.agent_sessions.get(task_id)
            node = self._node(task_id)
            if (self._state.agent_team is None or record is None or node.state is not NodeState.FAILED_TERMINAL
                or self.node_cancel_epoch(task_id) or node.definition.role == "verifier"
                or record["contract_revision"] != self.current_plan.contract_revision
                or node.result is None or node.result.failure is None or node.result.failure.kind not in {
                    ExecutionFailureKind.TOOL_FAILED, ExecutionFailureKind.TOOL_TIMEOUT, ExecutionFailureKind.WORKER_TIMED_OUT}
                or node.definition.allowed_effects != (ToolEffect.READ_ONLY,)):
                raise ReducerError("only settled read-only execution failures can be reassigned; cancellation, policy, budget and verification remain blocking")
            settlement = self._state.worker_completions.get(node.result.attempt_id)
            if not settlement or settlement.get("ambiguous_side_effect"):
                raise ReducerError("old worker must exit without ambiguous side effects before reassignment")

    def resolved_agent_failures(self, *, include_dismissed=False) -> frozenset[str]:
        with self._lock:
            replacements = {r["replaces_task_id"]: task for task, r in self._state.agent_sessions.items() if r.get("replaces_task_id")}
            replacements.update({r["predecessor_task_id"]: task for task, r in self._state.agent_sessions.items()
                if r.get("continuation_kind") == "interrupted" and self.is_agent_interrupted(r["predecessor_task_id"])})
            resolved = set()
            dismissed = self.dismissed_agent_ids() if include_dismissed else frozenset()
            for original in replacements:
                current, seen = original, set()
                while current in replacements and current not in seen:
                    seen.add(current)
                    current = replacements[current]
                if self._node(current).state is NodeState.SUCCEEDED or current in dismissed:
                    resolved.add(original)
            return frozenset(resolved)

    def completion_blockers(self) -> tuple[str, ...]:
        """Failed advisory work stays failed; only an audited takeover resolves it."""
        with self._lock:
            discarded = self.discarded_builder_ids() | self.dismissed_agent_ids() | frozenset(
                (self._state.user_steering or {}).get("superseded_nodes", ()))
            resolved_agents = self.resolved_agent_failures(include_dismissed=True)
            unresolved = {item["node_id"] for item in self.pending_candidate_quarantines().values()}
            unselected = {node.node_id for node in self.current_plan.nodes if node.alternative_group
                          and node.alternative_group not in self._state.candidate_selections}
            blocked = tuple(node_id for node_id, runtime in self._state.nodes.items()
                         if node_id in unresolved or node_id in unselected or (
                         node_id not in discarded and node_id not in resolved_agents and runtime.state is not NodeState.SUCCEEDED
                         and not self.optional_review_unavailable(node_id) and not (
                             self.advisory_enabled and runtime.advisory_acknowledgment is not None
                             and self.node_diagnosis(node_id)["fallback_allowed"])))
            process = self.required_consultation_status()
            unread = not self.consultation_read_status()["satisfied"]
            return tuple(dict.fromkeys((*blocked, "primary"))) if unread or (process and not process["satisfied"]) else blocked

    def optional_review_unavailable(self, node_id: str) -> bool:
        """Keep the failed review and its cause; only an optional opinion can be absent."""
        from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

        with self._lock:
            if node_id != "final-verifier":
                return False
            runtime = self._node(node_id)
            result = runtime.result
            if result is None or result.failure is None or runtime.definition.review_policy is None:
                return False
            rejected = any(not record.passed for record in result.verification_records) or any(
                event.event_type is RuntimeEventType.VERIFICATION_FAILED
                and event.payload.get("node_id") == node_id
                and event.payload.get("attempt_id") == runtime.current_attempt_id
                for event in self._state.events)
            return createApp().review_unavailable_is_optional(ReviewPolicy.from_dict(runtime.definition.review_policy),
                state=runtime.state.value, failure_kind=result.failure.kind.value,
                ambiguous_side_effect=result.failure.ambiguous_side_effect, failed_verification=rejected)

    def consultation_read_status(self) -> dict[str, Any]:
        from minicode.consultation_reading import reading_status

        with self._lock:
            return reading_status(self.run_id, self._state.nodes, self._state.events,
                                  self._state.contract_revision)

    @property
    def dynamic_review_enabled(self) -> bool:
        return self._state.review_runtime is not None

    @property
    def admitted_review_policy(self):
        with self._lock:
            if self.dynamic_review_enabled:
                from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
                return ReviewPolicy.from_dict(self._state.review_runtime["admission"]).to_dict()
            primary = self._state.nodes.get("primary")
            return deepcopy(primary.definition.review_policy) if primary else None

    @property
    def effective_review_policy(self):
        with self._lock:
            primary = self._state.nodes.get("primary")
            return deepcopy(primary.definition.review_policy) if primary else None

    @property
    def current_review_assessment(self):
        with self._lock:
            primary = self._state.nodes.get("primary")
            if not self.dynamic_review_enabled or primary is None or primary.result is None:
                return None
            return next((deepcopy(record) for record in reversed(self._state.review_runtime["records"])
                if record["attempt_id"] == primary.result.attempt_id
                and record["contract_revision"] == self._state.contract_revision), None)

    @staticmethod
    def _validate_review_hashes(hashes):
        from Package.AgentRuntime.Src.Application.Dto.ReviewObservation import ReviewObservation
        from minicode.workspace_provider import snapshot_revision
        if not isinstance(hashes, Mapping) or any(not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values()):
            raise ReducerError("observed review requires native workspace content hashes")
        ReviewObservation(snapshot_revision(dict(hashes)), tuple(hashes))

    def _integrated_review_nodes(self, *, before_sequence=None):
        integrated = set()
        for event in self._state.events:
            if before_sequence is not None and event.sequence >= before_sequence:
                continue
            node_id = None
            if event.event_type is RuntimeEventType.PATCH_RECORDED and event.payload.get("state") == "applied":
                patch_id = event.payload.get("patch_id", "")
                node_id = self._state.patch_records.get(patch_id, {}).get("node_id")
                if node_id is None and patch_id.startswith("patch-"):
                    node_id = patch_id[6:]
            elif event.event_type is RuntimeEventType.WORKSPACE_COMMITTED:
                node_id = event.payload.get("node_id")
            node = self._state.nodes.get(node_id)
            if node_id != "primary" and node is not None and node.definition.is_writer:
                integrated.add(node_id)
        return tuple(sorted(integrated))

    def _observed_review_policy(self, previous, record):
        from Package.AgentRuntime.Src.Application.Dto.ReviewObservation import ReviewObservation, ReviewEscalationRequest
        from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp
        config = self._state.review_runtime
        return json.loads(json.dumps(createApp().escalate_review_policy(ReviewEscalationRequest(
            ReviewPolicy.from_dict(config["admission"]), ReviewPolicy.from_dict(previous),
            ReviewObservation(record["candidate_revision"], tuple(record["delta"]), tuple(record["integrated_nodes"])),
            config["max_agents"], config["reasoning_effort"])).to_dict()))

    def assess_delivery_review(self, result: AgentResult, current_hashes: Mapping[str, str]):
        """Native scheduler assessment before primary acceptance opens its reviewer.

        The admitted profile and run identity stay frozen. Only the effective
        Plan's review requirement may increase, with a candidate-bound audit.
        """
        from minicode.workspace_provider import snapshot_revision
        with self._lock:
            if not self.dynamic_review_enabled:
                return self.effective_review_policy
            primary = self._node("primary")
            if (self._state.canceled or self._state.paused or primary.result != result
                or result.node_id != "primary" or result.status is not AgentResultStatus.SUCCEEDED
                or primary.state not in {NodeState.VERIFYING, NodeState.SUCCEEDED}
                or result.contract_revision != self._state.contract_revision):
                raise ReducerError("observed review requires the current accepted primary result")
            self._validate_review_hashes(current_hashes)
            revision = snapshot_revision(dict(current_hashes))
            previous_record = self.current_review_assessment
            if previous_record:
                if previous_record["candidate_revision"] != revision:
                    raise ReducerError("candidate changed after its observed review assessment")
                return self.effective_review_policy
            reviewer = self._state.nodes.get("final-verifier")
            if reviewer is not None and (reviewer.current_attempt_id is not None or reviewer.result is not None
                                         or reviewer.state is not NodeState.PENDING):
                raise ReducerError("observed review cannot reuse an already dispatched reviewer")
            config = self._state.review_runtime
            baseline = config["baseline"]
            delta = {path: current_hashes.get(path) for path in sorted(set(baseline) | set(current_hashes))
                     if baseline.get(path) != current_hashes.get(path)}
            previous = json.loads(json.dumps(self.effective_review_policy))
            record = {"attempt_id": result.attempt_id, "plan_version": self.current_plan.plan_version,
                "contract_revision": result.contract_revision, "candidate_revision": revision, "delta": delta,
                "integrated_nodes": list(self._integrated_review_nodes()), "previous_policy": previous}
            policy = self._observed_review_policy(previous, record)
            record["policy"] = policy
            if policy != previous:
                plan = self._state.plans[self._state.current_plan_version]
                primary.definition = replace(primary.definition, review_policy=policy)
                nodes = tuple(replace(node, review_policy=policy) if node.node_id in {"primary", "final-verifier"}
                              else node for node in plan.nodes)
                if policy["scope"] != "basic" and reviewer is None:
                    definition = PlanNode("final-verifier", "Independently review the observed candidate changes for:\n"
                        + self.contract.envelope.request, "verifier", dependencies=("primary",),
                        acceptance_criteria=primary.definition.acceptance_criteria or ("verify the delivered result addresses the original request",),
                        max_attempts=primary.definition.max_attempts if policy["required"] else 1, review_policy=policy)
                    nodes = (*nodes, definition)
                    self._state.nodes[definition.node_id] = NodeRuntime(definition, NodeState.PENDING)
                elif reviewer is not None:
                    reviewer.definition = replace(reviewer.definition, review_policy=policy,
                        objective="Independently review the observed candidate changes and integration evidence for:\n"
                                  + self.contract.envelope.request,
                        max_attempts=primary.definition.max_attempts if policy["required"] else 1)
                    nodes = tuple(reviewer.definition if node.node_id == "final-verifier" else node for node in nodes)
                self._state.plans[plan.plan_version] = replace(plan, nodes=nodes)
                if self.closure_reserve_enabled:
                    self._state.closure_budget = self._resolve_closure_budget(self.current_plan, self.contract)
            event = self._emit(RuntimeEventType.REVIEW_ASSESSED, record)
            config["records"].append({**deepcopy(record), "event_id": event.event_id})
            self._persist()
            return deepcopy(policy)

    def _review_policy_for_plan(self, version):
        from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
        config = self._state.review_runtime
        policy = next((record["policy"] for record in reversed(config["records"])
                       if record["plan_version"] <= version), config["admission"])
        return ReviewPolicy.from_dict(policy).to_dict()

    def _validate_dynamic_review(self):
        """Replay the candidate audit; changing a policy alone never lowers a gate."""
        if not self.dynamic_review_enabled:
            if any(event.event_type is RuntimeEventType.REVIEW_ASSESSED for event in self._state.events):
                raise ReducerError("observed review audit cannot be stripped from a snapshot")
            return
        from Package.AgentRuntime.Src.Application.Dto.ReviewPolicy import ReviewPolicy
        from minicode.workspace_provider import snapshot_revision
        config = self._state.review_runtime
        if (set(config) != {"policy", "admission", "baseline", "reasoning_effort", "max_agents", "records"}
            or config["policy"] != "observed-review-v1" or not isinstance(config["records"], list)):
            raise ReducerError("invalid observed review runtime contract")
        previous = json.loads(json.dumps(ReviewPolicy.from_dict(config["admission"]).to_dict()))
        if config["reasoning_effort"] is not None:
            replace(ReviewPolicy.from_dict(previous), reasoning_budget=config["reasoning_effort"])
        self._validate_review_hashes(config["baseline"])
        # Validate fixed capacity/effort even before the first submission.
        seed = {"candidate_revision": snapshot_revision(config["baseline"]), "delta": {}, "integrated_nodes": []}
        self._observed_review_policy(previous, seed)
        events = {event.event_id: event for event in self._state.events}
        audited = set()
        audit_events = set()
        for record in config["records"]:
            if not isinstance(record, dict) or set(record) != {"attempt_id", "plan_version", "contract_revision",
                "candidate_revision", "delta", "integrated_nodes", "previous_policy", "policy", "event_id"}:
                raise ReducerError("invalid observed candidate review record")
            event = events.get(record["event_id"])
            if (event is None or event.event_type is not RuntimeEventType.REVIEW_ASSESSED
                or event.payload != {key: value for key, value in record.items() if key != "event_id"}
                or record["previous_policy"] != previous or record["attempt_id"] in audited
                or record["plan_version"] not in self._state.plans
                or not any(e.event_type is RuntimeEventType.RESULT_ACCEPTED and e.sequence < event.sequence
                    and e.payload.get("node_id") == "primary" and e.payload.get("attempt_id") == record["attempt_id"]
                    and e.payload.get("status") == AgentResultStatus.SUCCEEDED.value for e in self._state.events)):
                raise ReducerError("observed review has no matching accepted candidate audit")
            image = dict(config["baseline"])
            if not isinstance(record["delta"], dict):
                raise ReducerError("observed review delta must be a mapping")
            for path, digest in record["delta"].items():
                if image.get(path) == digest:
                    raise ReducerError("observed review delta contains an unchanged resource")
                if digest is None:
                    image.pop(path, None)
                else:
                    image[path] = digest
            self._validate_review_hashes(image)
            if (snapshot_revision(image) != record["candidate_revision"]
                or tuple(record["integrated_nodes"]) != self._integrated_review_nodes(before_sequence=event.sequence)
                or record["policy"] != self._observed_review_policy(previous, record)):
                raise ReducerError("observed review policy differs from its native candidate evidence")
            previous = record["policy"]
            audited.add(record["attempt_id"])
            audit_events.add(event.event_id)
        if audit_events != {event.event_id for event in self._state.events if event.event_type is RuntimeEventType.REVIEW_ASSESSED}:
            raise ReducerError("observed review records do not cover the persisted assessment events")
        if json.loads(json.dumps(self.effective_review_policy)) != previous:
            raise ReducerError("effective review policy does not match its persisted upgrade audit")
        current = self._state.nodes.get("primary")
        if current and current.state is NodeState.SUCCEEDED and not self.current_review_assessment:
            raise ReducerError("accepted primary lacks its observed review assessment")

    def validate_consultation_read(self, invocation: AgentInvocationSpec, ref: ArtifactRef) -> dict:
        with self._lock:
            attempt = self.current_attempt("primary")
            if (self._state.canceled or self._state.paused or invocation.role != "primary"
                or invocation.node_id != "primary" or invocation.run_id != self.run_id
                or invocation.plan_version != self.current_plan.plan_version
                or invocation.context_manifest.contract_revision != self._state.contract_revision
                or attempt is None or attempt.attempt_id != invocation.attempt_id
                or attempt.idempotency_key != invocation.idempotency_key
                or attempt.state is not NodeState.RUNNING or self._node("primary").state is not NodeState.RUNNING
                or not attempt.lease.is_valid(self._clock.now())):
                raise ReducerError("consultation reading requires the current live primary activation")
            for obligation in self.consultation_read_status()["obligations"]:
                if obligation["valid"] and obligation["artifact_ref"] == ref.to_dict():
                    return obligation
            raise ReducerError("consultation summary is not a verified delivered dependency")

    def record_consultation_read(self, invocation, ref, start, end, tool_call_id, receipt_sha256) -> None:
        """Runtime-only native reader callback, not an exposed model command."""
        with self._lock:
            obligation = self.validate_consultation_read(invocation, ref)
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= ref.size
                or not isinstance(tool_call_id, str) or not tool_call_id
                or not isinstance(receipt_sha256, str) or len(receipt_sha256) != 64
                or any(c not in "0123456789abcdef" for c in receipt_sha256)):
                raise ReducerError("invalid consultation page receipt")
            for event in self._state.events:
                if (event.event_type is RuntimeEventType.CONSULTATION_SUMMARY_READ
                    and event.payload.get("attempt_id") == invocation.attempt_id
                    and event.payload.get("tool_call_id") == tool_call_id):
                    raise ReducerError("consultation read receipt was already recorded")
            self._emit(RuntimeEventType.CONSULTATION_SUMMARY_READ, {
                "node_id": "primary", "attempt_id": invocation.attempt_id,
                "child_id": obligation["child_id"], "child_attempt_id": obligation["child_attempt_id"],
                "delivery_event_id": obligation["delivery_event_id"], "artifact_ref": ref.to_dict(),
                "contract_revision": self._state.contract_revision,
                "start": start, "end": end, "tool_call_id": tool_call_id, "receipt_sha256": receipt_sha256,
            })
            self._persist()

    def required_consultation_status(self) -> dict[str, Any] | None:
        from minicode.consultation_requirement import consultation_requirement_status

        with self._lock:
            return consultation_requirement_status(self.contract, self._state.nodes, self._state.events)

    def validate_primary_candidate_context(self, invocation: AgentInvocationSpec) -> None:
        """Authorize current source inspection, never submission or budget use.

        A request may inspect its bound source after reserving its own output.
        Submission separately requires settled usage; request admission remains
        the shared ledger's responsibility.
        """
        self._validate_primary_submission_context(invocation, isolated=True)

    def _validate_primary_submission_context(self, invocation: AgentInvocationSpec, *, isolated: bool) -> None:
        with self._lock:
            attempt = self.current_attempt(invocation.node_id)
            if (self.candidate_first != isolated or self.contract is None or invocation.node_id != "primary"
                or invocation.role != "primary" or invocation.run_id != self.run_id
                or invocation.plan_version != self.current_plan.plan_version
                or invocation.context_manifest.plan_version != invocation.plan_version
                or invocation.context_manifest.node_id != invocation.node_id
                or invocation.context_manifest.contract_revision != self.contract.contract_revision
                or self._state.canceled or self._state.paused or self.node_cancel_epoch(invocation.node_id) or attempt is None
                or attempt.attempt_id != invocation.attempt_id or attempt.state is not NodeState.RUNNING
                or attempt.idempotency_key != invocation.idempotency_key
                or attempt.plan_version != invocation.plan_version or not attempt.lease.is_valid(self._clock.now())
                or self._node(invocation.node_id).state is not NodeState.RUNNING
                or self._node(invocation.node_id).definition.role != "primary"
                or not self._node(invocation.node_id).definition.is_writer
                or not set(invocation.allowed_effects).issubset(self._node(invocation.node_id).definition.allowed_effects)
                or ToolEffect.WORKSPACE_WRITE not in invocation.allowed_effects):
                kind = "isolated" if isolated else "direct"
                raise ReducerError(f"candidate submission requires the current live {kind} primary writer")
            process = self.required_consultation_status()
            if process and not process["satisfied"]:
                raise ReducerError(process["reason"])
            reading = self.consultation_read_status()
            if not reading["satisfied"]:
                raise ReducerError(reading["reason"])
            blocked = set(self.completion_blockers()) - {"primary", "final-verifier"}
            if blocked:
                raise ReducerError("candidate submission requires resolved consultations: " + ", ".join(sorted(blocked))
                    + "; read the findings, repair them and use retry_consultation for the original batch")
            if any(cap.deadline is not None and self._clock.now() >= cap.deadline
                   for cap in (self.contract.budget, invocation.budget)):
                raise ReducerError("primary candidate context has an expired deadline")

    def validate_candidate_submission(self, invocation: AgentInvocationSpec) -> None:
        """Read-only authority check; submission cannot mark a node or run done."""
        with self._lock:
            self.validate_primary_candidate_context(invocation)
            self._validate_submission_usage(invocation)

    def validate_direct_submission(self, invocation: AgentInvocationSpec) -> None:
        """Authorize a direct primary handoff, never candidate integration or task success."""
        with self._lock:
            self._validate_primary_submission_context(invocation, isolated=False)
            self._validate_submission_usage(invocation)

    def _validate_submission_usage(self, invocation: AgentInvocationSpec) -> None:
        with self._lock:
            for budget, attempt_id in ((self.contract.budget, None), (invocation.budget, invocation.attempt_id)):
                usage = self.usage_summary(attempt_id)
                if (not self._state.usage_ledger.settlement_ready(budget, attempt_id)
                    or (budget.max_tokens is not None and (not usage["tokens_known"] or usage["tokens"] > budget.max_tokens))
                    or (budget.max_cost is not None and (not usage["cost_known"] or usage["known_cost"] > budget.max_cost))
                    or (budget.deadline is not None and self._clock.now() >= budget.deadline)):
                    raise ReducerError("candidate submission has unsettled usage, exceeded budget or expired deadline")

    def validate_builder_candidate_context(self, invocation: AgentInvocationSpec) -> None:
        """Inspect this live Builder's source, including after request reservation.

        This grants no submission, patch acceptance or additional budget.
        """
        with self._lock:
            attempt = self.current_attempt(invocation.node_id)
            node = self._node(invocation.node_id)
            if (not self.candidate_first or self.contract is None
                or not invocation.node_id.startswith("builder-") or invocation.role != "writer"
                or invocation.run_id != self.run_id or invocation.plan_version != self.current_plan.plan_version
                or invocation.context_manifest.plan_version != invocation.plan_version
                or invocation.context_manifest.node_id != invocation.node_id
                or invocation.context_manifest.contract_revision != self.contract.contract_revision
                or self._state.delegation_parents.get(invocation.node_id) != "primary"
                or self._state.canceled or self._state.paused or self.node_cancel_epoch(invocation.node_id)
                or attempt is None or attempt.attempt_id != invocation.attempt_id
                or attempt.state is not NodeState.RUNNING or node.state is not NodeState.RUNNING
                or attempt.plan_version != invocation.plan_version or attempt.idempotency_key != invocation.idempotency_key
                or not attempt.lease.is_valid(self._clock.now()) or not node.definition.is_writer
                or node.definition.role != invocation.role or node.definition.allowed_effects != invocation.allowed_effects
                or ToolEffect.WORKSPACE_WRITE not in invocation.allowed_effects
                or not self.builder_may_execute(invocation.node_id)):
                raise ReducerError("Builder submission requires the current registered live writer")
            if any(cap.deadline is not None and self._clock.now() >= cap.deadline
                   for cap in (self.contract.budget, invocation.budget)):
                raise ReducerError("Builder candidate context has an expired deadline")

    def validate_builder_submission(self, invocation: AgentInvocationSpec) -> None:
        """Finish only after local settlement; sibling reservations may remain."""
        with self._lock:
            self.validate_builder_candidate_context(invocation)
            local = self.usage_summary(invocation.attempt_id)
            if any(not self._state.usage_ledger.settlement_ready(cap, invocation.attempt_id)
                   for cap in (self.contract.budget, invocation.budget)):
                raise ReducerError("Builder submission has unsettled local model usage")
            for cap, usage in ((self.contract.budget, self.usage_summary()), (invocation.budget, local)):
                if ((cap.max_tokens is not None and usage["tokens"] > cap.max_tokens)
                    or (cap.max_cost is not None and usage["known_cost"] > cap.max_cost)
                    or (cap.deadline is not None and self._clock.now() >= cap.deadline)):
                    raise ReducerError("Builder submission has exceeded its budget or deadline")
            if ((invocation.budget.max_tokens is not None and not local["tokens_known"])
                or (invocation.budget.max_cost is not None and not local["cost_known"])):
                raise ReducerError("Builder submission requires reconciled local usage")

    def primary_error_recovery_status(self, fault_sha256: str) -> dict[str, Any]:
        """Durable bounded-recovery allowance for one root fault and the execution.

        Counted from the persisted Runtime event log, so a restart, resume or
        checkpoint cannot refresh the allowance for the same fault identity.
        ``remaining`` is the tighter of the per-fault and the logical-execution
        allowance, so a second, different fault cannot open a second full budget.
        Never a capability: it only answers how many remain.
        """
        if not isinstance(fault_sha256, str) or not fault_sha256:
            raise ReducerError("primary error recovery requires a fault identity")
        with self._lock:
            granted = [
                event for event in self._state.events
                if event.event_type is RuntimeEventType.PRIMARY_ERROR_RECOVERY_RECORDED
            ]
            for_fault = [event for event in granted
                         if (event.payload or {}).get("fault_sha256") == fault_sha256]
            total = len(granted)
            fault_remaining = max(0, self.primary_error_recovery_limit - len(for_fault))
            total_remaining = max(0, self.primary_error_recovery_total_limit - total)
            return {"fault_sha256": fault_sha256, "granted": len(for_fault),
                    "limit": self.primary_error_recovery_limit,
                    "remaining": min(fault_remaining, total_remaining),
                    "fault_remaining": fault_remaining,
                    "total_granted": total,
                    "total_limit": self.primary_error_recovery_total_limit,
                    "total_remaining": total_remaining,
                    "generation": self.run_id}

    def record_primary_error_recovery(self, fault_sha256: str, diagnostics: Mapping[str, Any]) -> dict[str, Any]:
        """Consume one bounded recovery credit for a real admitted request.

        Called only after the recovery request was actually admitted, so a
        refused or abandoned attempt never spends the allowance. Both the
        per-fault and the logical-execution allowances must still have room.
        """
        if not isinstance(fault_sha256, str) or not fault_sha256:
            raise ReducerError("primary error recovery requires a fault identity")
        with self._lock:
            status = self.primary_error_recovery_status(fault_sha256)
            if status["remaining"] <= 0:
                raise ReducerError(
                    "primary error recovery allowance is already exhausted for this fault "
                    "or for the logical execution")
            attempt = self.current_attempt("primary")
            self._emit(RuntimeEventType.PRIMARY_ERROR_RECOVERY_RECORDED, {
                "node_id": "primary",
                "attempt_id": attempt.attempt_id if attempt is not None else None,
                "fault_sha256": fault_sha256, "granted": status["granted"] + 1,
                "limit": self.primary_error_recovery_limit,
                "total_granted": status["total_granted"] + 1,
                "total_limit": self.primary_error_recovery_total_limit,
                "generation": self.run_id,
                "diagnostics": deepcopy(dict(diagnostics)),
            })
            self._persist()
            return self.primary_error_recovery_status(fault_sha256)

    def validate_initial_primary_source(self, invocation: AgentInvocationSpec) -> None:
        """Authorize initial reads without satisfying any consultation/write gate."""
        with self._lock:
            attempt = self.current_attempt(invocation.node_id)
            node = self._state.nodes.get(invocation.node_id)
            if (not self.candidate_first or self.contract is None
                or invocation.node_id != "primary" or invocation.role != "primary"
                or invocation.run_id != self.run_id or invocation.plan_version != self.current_plan.plan_version
                or invocation.context_manifest.plan_version != invocation.plan_version
                or invocation.context_manifest.node_id != invocation.node_id
                or invocation.context_manifest.contract_revision != self.contract.contract_revision
                or self._state.canceled or self._state.paused or attempt is None or node is None
                or attempt.attempt_id != invocation.attempt_id or attempt.state is not NodeState.RUNNING
                or attempt.idempotency_key != invocation.idempotency_key
                or attempt.plan_version != invocation.plan_version or not attempt.lease.is_valid(self._clock.now())
                or node.state is not NodeState.RUNNING or node.definition.role != invocation.role
                or node.definition.allowed_effects != invocation.allowed_effects
                or invocation.context_manifest.tool_capability_scope != invocation.allowed_effects
                or ToolEffect.READ_ONLY not in invocation.allowed_effects
                or not self._invocation_goal_matches(node.definition, invocation)
                or any(cap.deadline is not None and self._clock.now() >= cap.deadline
                       for cap in (self.contract.budget, invocation.budget))):
                raise ReducerError("initial source observation requires the current live primary scope")

    def validate_candidate_review(self, invocation: AgentInvocationSpec) -> AgentResult:
        """Authorize source observation, never the verifier's decision or delivery."""
        with self._lock:
            attempt = self.current_attempt(invocation.node_id)
            node = self._state.nodes.get(invocation.node_id)
            primary = self._state.nodes.get("primary")
            if (not self.candidate_first or self.contract is None
                or invocation.node_id != "final-verifier" or invocation.role != "verifier"
                or invocation.run_id != self.run_id or invocation.plan_version != self.current_plan.plan_version
                or invocation.context_manifest.plan_version != invocation.plan_version
                or invocation.context_manifest.node_id != invocation.node_id
                or invocation.context_manifest.contract_revision != self.contract.contract_revision
                or invocation.allowed_effects != (ToolEffect.READ_ONLY,)
                or invocation.context_manifest.tool_capability_scope != (ToolEffect.READ_ONLY,)
                or self._state.canceled or self._state.paused or attempt is None or node is None
                or attempt.attempt_id != invocation.attempt_id or attempt.state is not NodeState.RUNNING
                or attempt.idempotency_key != invocation.idempotency_key
                or attempt.plan_version != invocation.plan_version or not attempt.lease.is_valid(self._clock.now())
                or node.state is not NodeState.RUNNING or node.definition.role != invocation.role
                or node.definition.allowed_effects != invocation.allowed_effects
                or not self._invocation_goal_matches(node.definition, invocation)
                or "primary" not in node.definition.dependencies
                or invocation.context_manifest.dependency_result_refs != node.definition.dependencies
                or primary is None or primary.state is not NodeState.SUCCEEDED or primary.result is None
                or primary.result.attempt_id != primary.current_attempt_id
                or primary.result.session_checkpoint is None
                or self._state.integration_status != "prepared"
                or any(cap.deadline is not None and self._clock.now() >= cap.deadline
                       for cap in (self.contract.budget, invocation.budget))):
                raise ReducerError("candidate review requires the current live independent verifier and prepared primary")
            return primary.result

    def _invocation_goal_matches(self, definition, invocation) -> bool:
        # Existing activations retain their original typed assignment. A new
        # activation/review may receive the authenticated human projection.
        candidates = [definition]
        if self._state.user_steering is not None:
            candidates.extend(n for n in self.current_plan.nodes if n.node_id == definition.node_id)
        return any(n.objective == invocation.objective and n.acceptance_criteria == invocation.acceptance_criteria
                   for n in candidates)

    def validate_consultation_submission(self, invocation: AgentInvocationSpec) -> None:
        """Check one active helper; accepting its report cannot finish the run.

        A parallel sibling may still have an active model reservation. Do not
        require that sibling to settle before this read-only report can return;
        global delivery/unknown-usage checks remain with the Scheduler and Gate.
        """
        with self._lock:
            attempt = self.current_attempt(invocation.node_id)
            if (self.contract is None or not invocation.node_id.startswith(("consult-", "delegated-"))
                or invocation.role not in {"explorer", "planner", "reviewer", "verifier"}
                or invocation.run_id != self.run_id or invocation.plan_version != self.current_plan.plan_version
                or invocation.context_manifest.plan_version != invocation.plan_version
                or invocation.context_manifest.node_id != invocation.node_id
                or invocation.context_manifest.contract_revision != self.contract.contract_revision
                or self._state.canceled or self._state.paused or attempt is None
                or attempt.attempt_id != invocation.attempt_id or attempt.state is not NodeState.RUNNING
                or attempt.idempotency_key != invocation.idempotency_key or attempt.plan_version != invocation.plan_version
                or not attempt.lease.is_valid(self._clock.now())
                or self._node(invocation.node_id).state is not NodeState.RUNNING
                or self._node(invocation.node_id).definition.role != invocation.role
                or self._node(invocation.node_id).definition.objective != invocation.objective
                or self._node(invocation.node_id).definition.acceptance_criteria != invocation.acceptance_criteria
                or self._node(invocation.node_id).definition.context_files != invocation.context_manifest.context_files
                or self._node(invocation.node_id).definition.allowed_effects != (ToolEffect.READ_ONLY,)
                or invocation.allowed_effects != (ToolEffect.READ_ONLY,)):
                raise ReducerError("consultation submission requires the current live read-only helper")
            usage = self.usage_summary(invocation.attempt_id)
            budget = invocation.budget
            if (any(not self._state.usage_ledger.settlement_ready(cap, invocation.attempt_id)
                    for cap in (budget, self.contract.budget))
                or (budget.max_tokens is not None and usage["tokens"] > budget.max_tokens)
                or (budget.max_cost is not None and (not usage["cost_known"] or usage["known_cost"] > budget.max_cost))
                or any(cap.deadline is not None and self._clock.now() >= cap.deadline
                       for cap in (budget, self.contract.budget))):
                raise ReducerError("consultation submission has unsettled usage, exceeded budget or expired deadline")

    def consultation_tool_blocker(self, node_id: str, attempt_id: str, tool_name: str, effect: ToolEffect) -> str:
        with self._lock:
            if node_id == "primary":
                reading = self.consultation_read_status()
                if not reading["satisfied"] and (effect is not ToolEffect.READ_ONLY
                    or tool_name in {"task", "consult_agents", "retry_consultation", "submit_candidate"}):
                    return reading["reason"]
                if tool_name in {"task", "consult_agents"}:
                    recovery = self.consultation_recovery_status(node_id, attempt_id)
                    if recovery["children"]:
                        return "Required review is pending; read its findings and use retry_consultation for the original batch"
            process = self.required_consultation_status()
            if node_id != "primary" or process is None:
                return ""
            attempt = self.current_attempt(node_id)
            if (attempt is None or attempt.attempt_id != attempt_id or attempt.state is not NodeState.RUNNING
                or not attempt.lease.is_valid(self._clock.now()) or self._state.canceled or self._state.paused):
                return "Required consultation policy: no live primary lease for this tool"
            if process["blocked"]:
                return process["reason"]
            if not process["satisfied"] and (tool_name == "task" or (
                process["before_workspace_write"] and effect is not ToolEffect.READ_ONLY
            )):
                return process["reason"] + "; use consult_agents before writing; synchronous task is not a substitute"
            return ""

    def advisory_takeover_available(self, parent_id: str, attempt_id: str) -> bool:
        """Read-only tool visibility, never permission to acknowledge a failure.

        Keep an already acknowledged delivery visible in its activation for
        idempotent calls. The command still checks live ownership, budget,
        diagnosis and delivery independently at execution time.
        """
        with self._lock:
            parent = self._state.nodes.get(parent_id)
            attempt = self.current_attempt(parent_id) if parent is not None else None
            if (not self.advisory_enabled or parent is None or parent.definition.role != "primary"
                or parent.state is not NodeState.RUNNING or self._state.canceled or self._state.paused
                or attempt is None or attempt.attempt_id != attempt_id
                or not attempt.lease.is_valid(self._clock.now())):
                return False
            for event in reversed(self._state.events):
                if (event.event_type is not RuntimeEventType.CONSULTATION_DELIVERED
                    or event.payload.get("node_id") != parent_id or event.payload.get("attempt_id") != attempt_id):
                    continue
                for child_id, delivered_attempt in event.payload.get("children", {}).items():
                    child = self._state.nodes.get(child_id)
                    if (child is not None and delivered_attempt is not None
                        and child.current_attempt_id == delivered_attempt
                        and self.node_diagnosis(child_id)["fallback_allowed"]):
                        return True
            return False

    def record_consultation_delivery(self, parent_id: str, attempt_id: str) -> None:
        """Persist which failed child attempts were actually supplied on resume."""
        with self._lock:
            if not self.continuations_enabled:
                return
            parent = self._node(parent_id)
            continuation = parent.continuation
            attempt = self.current_attempt(parent_id)
            if (parent.definition.role != "primary" or parent.state is not NodeState.RUNNING
                or continuation is None or attempt is None or attempt.attempt_id != attempt_id
                or not attempt.lease.is_valid(self._clock.now())):
                raise ReducerError("consultation delivery requires the live resumed primary")
            self._emit(RuntimeEventType.CONSULTATION_DELIVERED, {
                "node_id": parent_id, "attempt_id": attempt_id,
                "continuation_id": continuation.continuation_id,
                "children": {child.node_id: self._node(child.node_id).current_attempt_id for child in continuation.children},
            })
            self._persist()

    def consultation_recovery_status(self, parent_id: str, attempt_id: str) -> dict[str, Any]:
        """Retry an owned read-only batch; it remains blocking until it passes."""
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

        with self._lock:
            rounds = sum(e.event_type is RuntimeEventType.CONSULTATION_RECOVERY_REQUESTED
                         for e in self._state.events)
            status = {"allowed": False, "rounds": rounds, "limit": 2,
                      "reason": "no recoverable delivered consultation", "children": []}
            parent = self._state.nodes.get(parent_id)
            attempt = self.current_attempt(parent_id) if parent else None
            continuation = parent.continuation if parent else None
            if (self._state.canceled or self._state.paused or parent is None or parent.definition.role != "primary"
                or parent.state is not NodeState.RUNNING or attempt is None or attempt.attempt_id != attempt_id
                or not attempt.lease.is_valid(self._clock.now()) or continuation is None
                or continuation.kind != "consultation"):
                return status
            children = [self._node(n.node_id) for n in continuation.children]
            if not children or any(n.state not in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL} for n in children):
                return status
            failed = [n for n in children if n.state is NodeState.FAILED_TERMINAL]
            if not any(n.definition.criticality == "required" for n in failed):
                return status
            status["children"] = [n.definition.node_id for n in children]
            if not any(e.event_type is RuntimeEventType.CONSULTATION_DELIVERED
                       and e.payload.get("attempt_id") == attempt_id
                       and e.payload.get("continuation_id") == continuation.continuation_id
                       and e.payload.get("children") == {n.definition.node_id: n.current_attempt_id for n in children}
                       for e in self._state.events):
                return status
            for node in failed:
                failure = node.result.failure if node.result else None
                rejected = any(e.event_type is RuntimeEventType.VERIFICATION_FAILED
                               and e.payload.get("node_id") == node.definition.node_id
                               and e.payload.get("attempt_id") == node.current_attempt_id for e in self._state.events)
                kind = failure.kind.value if failure else "VerificationFailed" if rejected else ""
                if (node.definition.allowed_effects != (ToolEffect.READ_ONLY,)
                    or not createApp().review_failure_is_recoverable(kind, ambiguous=bool(failure and failure.ambiguous_side_effect))):
                    status["reason"] = "consultation needs external resolution: " + (kind or "unknown cause")
                    return status
            if rounds >= status["limit"]:
                status["reason"] = "consultation recovery round budget exhausted"
                return status
            status.update(allowed=True, reason="repair the findings, then retry the original consultation batch")
            return status

    def _validate_restored_consultation_recoveries(self) -> None:
        history = [e for e in self._state.events if e.event_type is RuntimeEventType.CONSULTATION_RECOVERY_REQUESTED]
        if len(history) > 2:
            raise ReducerError("consultation recovery limit exceeded")
        for event in history:
            payload = event.payload
            children = payload.get("children", {})
            if (not children or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("input_revision", "")))
                or not any(e.event_type is RuntimeEventType.CONSULTATION_DELIVERED and e.sequence < event.sequence
                    and e.payload.get("attempt_id") == payload.get("attempt_id")
                    and e.payload.get("children") == children for e in self._state.events)
                or not any(e.event_type is RuntimeEventType.PRIMARY_SUSPENDED and e.sequence > event.sequence
                    and e.payload.get("attempt_id") == payload.get("attempt_id")
                    and e.payload.get("continuation_id") == payload.get("continuation_id")
                    and e.payload.get("input_revision") == payload.get("input_revision")
                    and set(e.payload.get("children", ())) == set(children) for e in self._state.events)):
                raise ReducerError("consultation recovery lacks its owned delivery and checkpoint audit")
            for child_id, prior_attempt in children.items():
                child = self._node(child_id)
                if (child.definition.allowed_effects != (ToolEffect.READ_ONLY,)
                    or not any(a.attempt_id == prior_attempt for a in child.attempts)):
                    raise ReducerError("consultation recovery references a foreign child attempt")
        for node_id, node in self._state.nodes.items():
            if node_id.startswith("consult-") and node.resume_credits != sum(
                node_id in e.payload.get("children", {}) for e in history):
                raise ReducerError("consultation recovery credits lack their persisted audit")

    def _acknowledge_advisory_failure(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        payload = command.payload
        parent_id = str(payload.get("node_id", ""))
        parent = self._node(parent_id)
        attempt = self.current_attempt(parent_id)
        if (not self.advisory_enabled or self._state.canceled or self._state.paused
            or parent.definition.role != "primary" or parent.state is not NodeState.RUNNING
            or attempt is None or attempt.attempt_id != payload.get("attempt_id")
            or payload.get("fencing_token") != attempt.lease.fencing_token.value
            or not attempt.lease.is_valid(self._clock.now())):
            raise ReducerError("only a live fenced primary may acknowledge advisory failure")
        budget = self.contract.budget if self.contract else None
        usage = self.usage_summary()
        if budget is None or (
            (budget.max_tokens is not None and (not usage["tokens_known"] or self._state.tokens_used >= budget.max_tokens))
            or (budget.max_cost is not None and (not usage["cost_known"] or self._state.cost_used >= budget.max_cost))
            or (budget.deadline is not None and self._clock.now() >= budget.deadline)
        ):
            raise ReducerError("budget or deadline does not permit advisory takeover")
        child_id = str(payload.get("child_id", ""))
        child = self._node(child_id)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ReducerError("advisory takeover requires a bounded reason and replacement plan")
        if not self.node_diagnosis(child_id)["fallback_allowed"]:
            raise ReducerError("this failure is required, unsafe, unverified, or not eligible for advisory takeover")
        deliveries = [event for event in self._state.events
                      if event.event_type is RuntimeEventType.CONSULTATION_DELIVERED
                      and event.payload.get("node_id") == parent_id
                      and event.payload.get("attempt_id") == attempt.attempt_id
                      and event.payload.get("children", {}).get(child_id) == child.current_attempt_id]
        if not deliveries or child.current_attempt_id is None:
            raise ReducerError("failed child attempt has not been delivered to this primary activation")
        if child.advisory_acknowledgment is not None:
            return []
        acknowledgment = {"child_id": child_id, "child_attempt_id": child.current_attempt_id,
                          "node_id": parent_id, "attempt_id": attempt.attempt_id,
                          "delivery_event_id": deliveries[-1].event_id, "reason": reason.strip()}
        child.advisory_acknowledgment = acknowledgment
        return [self._emit(RuntimeEventType.ADVISORY_FAILURE_ACKNOWLEDGED, acknowledgment, command_id=command.command_id)]

    def _validate_restored_advisory(self) -> None:
        from minicode.runtime_protocol import validate_review_plan
        original = self._state.plans[min(self._state.plans)]
        admission = {n.node_id: (n.runtime_profile, n.review_policy) for n in original.nodes
                     if n.node_id in {"primary", "final-verifier"}}
        if self.dynamic_review_enabled:
            # Depth remains frozen; review scope follows the replayed native audit.
            admission = {key: (value[0], self.effective_review_policy) for key, value in admission.items()}
        for plan in self._state.plans.values():
            validate_review_plan(plan.nodes)
            expected = self._review_policy_for_plan(plan.plan_version) if self.dynamic_review_enabled else None
            for node in plan.nodes:
                frozen = admission.get(node.node_id)
                if frozen and (node.runtime_profile, node.review_policy) != (frozen[0], expected or frozen[1]):
                    raise ReducerError("persisted review scope differs from its frozen admission policy")
        events = {event.event_id: event for event in self._state.events}
        for node_id, runtime in self._state.nodes.items():
            if node_id in admission and (runtime.definition.runtime_profile, runtime.definition.review_policy) != admission[node_id]:
                raise ReducerError("node review policy differs from its frozen admission assignment")
            self._validate_criticality(runtime.definition, self.advisory_enabled)
            if runtime.definition.criticality == "advisory" and not any(
                event.event_type is RuntimeEventType.PRIMARY_SUSPENDED
                and event.run_id == self.run_id and runtime.definition.to_dict() in event.payload.get("definitions", [])
                for event in self._state.events
            ):
                raise ReducerError("advisory criticality lacks its immutable pre-dispatch registration")
            ack = runtime.advisory_acknowledgment
            if ack is None:
                continue
            if not self.advisory_enabled or not isinstance(ack, dict) or not self.node_diagnosis(node_id)["fallback_allowed"]:
                raise ReducerError("invalid persisted advisory acknowledgment")
            delivery = events.get(ack.get("delivery_event_id"))
            parent = self._state.nodes.get(ack.get("node_id"))
            reason = ack.get("reason")
            if (ack.get("child_id") != node_id or ack.get("child_attempt_id") != runtime.current_attempt_id
                or runtime.result is None or runtime.result.attempt_id != runtime.current_attempt_id
                or runtime.result.status not in {AgentResultStatus.FAILED_TERMINAL, AgentResultStatus.FAILED_RETRYABLE}
                or runtime.result.run_id != self.run_id or runtime.result.node_id != node_id
                or runtime.result.plan_version != self._state.current_plan_version
                or runtime.result.contract_revision != self._state.contract_revision
                or not any(a.attempt_id == runtime.current_attempt_id and a.node_id == node_id and a.run_id == self.run_id
                           and a.plan_version == self._state.current_plan_version for a in runtime.attempts)
                or parent is None or parent.definition.role != "primary"
                or not any(a.attempt_id == ack.get("attempt_id") for a in parent.attempts)
                or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000
                or delivery is None or delivery.event_type is not RuntimeEventType.CONSULTATION_DELIVERED
                or delivery.run_id != self.run_id or delivery.payload.get("node_id") != ack.get("node_id")
                or delivery.payload.get("attempt_id") != ack.get("attempt_id")
                or delivery.payload.get("children", {}).get(node_id) != runtime.current_attempt_id
                or not any(event.event_type is RuntimeEventType.RESULT_ACCEPTED and event.sequence < delivery.sequence
                           and event.payload.get("node_id") == node_id
                           and event.payload.get("attempt_id") == runtime.current_attempt_id
                           and event.payload.get("status") == runtime.result.status.value for event in self._state.events)
                or not any(event.event_type is RuntimeEventType.ADVISORY_FAILURE_ACKNOWLEDGED
                           and event.run_id == self.run_id and event.payload == ack and event.sequence > delivery.sequence
                           for event in self._state.events)):
                raise ReducerError("advisory acknowledgment does not match its durable delivery and primary decision")

    def _validate_restored_integration(self) -> None:
        status = self.integration_status
        if not self.candidate_first:
            if status != "idle" or self._state.integration_revision is not None or self._state.integration_error:
                raise ReducerError("legacy state cannot contain candidate delivery transitions")
            return
        primary = self._node("primary")
        if status not in {"idle", "prepared", "applying", "committed", "failed"}:
            raise ReducerError("invalid persisted candidate integration status")
        if status != "idle":
            result = primary.result
            attempt = self.current_attempt("primary")
            if (result is None or result.status is not AgentResultStatus.SUCCEEDED or result.session_checkpoint is None
                or result.run_id != self.run_id or result.node_id != "primary"
                or result.attempt_id != primary.current_attempt_id
                or result.plan_version != self.current_plan.plan_version or result.contract_revision != self._state.contract_revision
                or primary.workspace_committed != (status == "committed")
                or attempt is None or attempt.run_id != self.run_id or attempt.node_id != "primary"
                or attempt.plan_version != self.current_plan.plan_version):
                raise ReducerError("candidate integration does not match its accepted primary result")
        if status in {"applying", "committed", "failed"} and (
            bool(self.completion_blockers())
            or not isinstance(self._state.integration_revision, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self._state.integration_revision)
        ):
            raise ReducerError("candidate integration lacks a verified revision or successful plan")
        if status in {"applying", "committed", "failed"}:
            starts = [e for e in self._state.events if e.event_type is RuntimeEventType.CANDIDATE_INTEGRATION_STARTED]
            if (not starts or starts[-1].payload.get("attempt_id") != primary.current_attempt_id
                or starts[-1].payload.get("candidate_revision") != self._state.integration_revision):
                raise ReducerError("candidate integration has no matching durable start marker")
            if status == "committed" and not any(
                e.event_type is RuntimeEventType.WORKSPACE_COMMITTED
                and e.payload.get("attempt_id") == primary.current_attempt_id
                and e.payload.get("candidate_revision") == self._state.integration_revision for e in self._state.events
            ):
                raise ReducerError("candidate integration has no matching durable commit marker")
        if status in {"idle", "prepared"} and self._state.integration_revision is not None:
            raise ReducerError("unaccepted candidate contains an integration revision")
        if status == "idle" and primary.result and primary.result.status is AgentResultStatus.SUCCEEDED:
            raise ReducerError("successful candidate is missing its delivery disposition")

    def continuation_for(self, node_id: str) -> WorkerContinuation | None:
        with self._lock:
            return self._node(node_id).continuation

    @property
    def repair_limit(self) -> int:
        return self._state.repair_limit

    @property
    def repair_count(self) -> int:
        return len(self._state.repair_history)

    def pending_primary_repair(self) -> PrimaryRepair | None:
        with self._lock:
            return next((item for item in self._state.repair_history if item.repair_id == self._state.pending_repair_id), None)

    def decide_primary_repair(self, candidate_revision: str):
        from Package.AgentRuntime.Src.Application.Dto.RepairPolicy import RepairPolicyRequest
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

        with self._lock:
            failures = tuple(record for record in self._state.final_verification_records if not record.passed)
            review = self.repairable_review_result(candidate_revision)
            kinds = tuple(self._repair_record_kind(record, candidate_revision, review) for record in failures)
            return createApp().decide_repair(RepairPolicyRequest(
                self.repair_limit, self.repair_count, candidate_revision,
                tuple(item.candidate_revision for item in self._state.repair_history),
                not (set(self.completion_blockers()) - ({"final-verifier"} if review else set())),
                self._state.canceled, self._state.paused, kinds,
            ))

    def _review_result_kind(self, result: AgentResult, revision: str) -> str:
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

        node = self._state.nodes.get("final-verifier")
        if (node is None or node.definition.role != "verifier" or node.definition.is_writer
            or result.node_id != "final-verifier" or result.run_id != self.run_id
            or result.plan_version != self.current_plan.plan_version or result.contract_revision != self._state.contract_revision
            or result.base_revision != revision or not any(a.attempt_id == result.attempt_id for a in node.attempts)
            or not any(e.event_type is RuntimeEventType.RESULT_ACCEPTED
                and e.payload.get("node_id") == result.node_id and e.payload.get("attempt_id") == result.attempt_id
                and e.payload.get("status") == result.status.value
                and e.payload.get("reason", "") == (result.failure.message if result.failure else "") for e in self._state.events)):
            return "not_repairable"
        failure = result.failure
        rejected = any(e.event_type is RuntimeEventType.VERIFICATION_FAILED
                       and e.payload.get("node_id") == result.node_id and e.payload.get("attempt_id") == result.attempt_id
                       for e in self._state.events)
        kind = failure.kind.value if failure else "VerificationFailed" if rejected else ""
        if not createApp().review_failure_is_recoverable(kind, ambiguous=bool(failure and failure.ambiguous_side_effect)):
            return "not_repairable"
        return "candidate_review_failed" if kind == "VerificationFailed" else "review_unavailable"

    def repairable_review_result(self, revision: str) -> AgentResult | None:
        with self._lock:
            node = self._state.nodes.get("final-verifier")
            if (node and node.state is NodeState.FAILED_TERMINAL and node.result
                and node.result.attempt_id == node.current_attempt_id
                and self._review_result_kind(node.result, revision) != "not_repairable"):
                return node.result
            return None

    def _repair_record_kind(self, record: VerificationRecord, revision: str, review: AgentResult | None) -> str:
        if self._candidate_check_failed(record, revision):
            return "candidate_check_failed"
        if review is not None and not record.passed and record.scope == "final":
            if (record.verifier == "integrated-runtime-state" or record in review.verification_records):
                return self._review_result_kind(review, revision)
        return "not_repairable"

    def record_repair_stop(self, reason: str) -> None:
        with self._lock:
            self._emit(RuntimeEventType.REPAIR_STOPPED, {"reason": reason, "repair_count": self.repair_count})
            self._persist()

    @staticmethod
    def _candidate_check_failed(record: VerificationRecord, revision: str) -> bool:
        exit_line = record.details.partition("\n")[0]
        if record.passed or not record.deterministic or record.scope != "final" or not re.fullmatch(r"exit_code=[1-9][0-9]*", exit_line):
            return False
        return any(e.evidence_type == "command-exit" and e.base_revision == revision
                   and e.producer_node_id == record.verifier and e.resource.startswith("command:")
                   and re.fullmatch(r"[0-9a-f]{64}", e.content_hash)
                   and e.claim == f"verification command exited with {exit_line.split('=')[1]}"
                   for e in record.evidence_refs)

    def _validate_repair_identity(self, repair: PrimaryRepair) -> None:
        ref = repair.checkpoint
        if (repair.schema_version != "minicode-primary-repair-v1" or not repair.repair_id
            or repair.run_id != self.run_id or repair.plan_version != self._state.current_plan_version
            or repair.contract_revision != self._state.contract_revision
            or self._node(repair.node_id).definition.role != "primary"
            or not re.fullmatch(r"[0-9a-f]{64}", repair.candidate_revision)
            or ref.size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", ref.content_hash)
            or not ref.store_key.replace("\\", "/").startswith(f"{self.run_id}/{repair.node_id}/")
            or ".." in ref.store_key.replace("\\", "/").split("/")
            or not any(a.attempt_id == repair.prior_attempt_id for a in self._node(repair.node_id).attempts)
            or not repair.diagnostics or not all(self._repair_record_kind(r, repair.candidate_revision, repair.review_result)
                != "not_repairable" for r in repair.diagnostics)):
            raise ReducerError("invalid primary repair identity, checkpoint or diagnostics")

    def _request_repair(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        if self.candidate_first and self.integration_status != "prepared":
            raise ReducerError("only an unintegrated candidate may enter candidate-first repair")
        repair = command.payload.get("repair")
        if not isinstance(repair, PrimaryRepair) or self.contract is None:
            raise ReducerError("candidate repair requires a complete PrimaryRepair and TaskContract")
        self._validate_repair_identity(repair)
        decision = self.decide_primary_repair(repair.candidate_revision)
        if not decision.allowed:
            raise ReducerError(decision.reason)
        primary = self._node(repair.node_id)
        previous = primary.result
        if (primary.definition.role != "primary" or previous is None or previous.session_checkpoint != repair.checkpoint
            or previous.attempt_id != repair.prior_attempt_id or repair.run_id != self.run_id
            or repair.plan_version != self._state.current_plan_version or repair.contract_revision != self._state.contract_revision
            or repair.schema_version != "minicode-primary-repair-v1" or not repair.repair_id
            or any(item.repair_id == repair.repair_id for item in self._state.repair_history)):
            raise ReducerError("candidate repair references a stale or unowned primary checkpoint")
        failures = tuple(record for record in self._state.final_verification_records if not record.passed)
        if repair.diagnostics != failures:
            raise ReducerError("repair cannot replace the authoritative failed verification records")
        if repair.review_result != self.repairable_review_result(repair.candidate_revision):
            raise ReducerError("repair must retain the authoritative failed review")
        budget = self.contract.budget
        usage = self.usage_summary()
        if ((budget.max_tokens is not None and (not usage["tokens_known"] or self._state.tokens_used >= budget.max_tokens))
            or (budget.max_cost is not None and (not usage["cost_known"] or self._state.cost_used >= budget.max_cost))
            or (budget.deadline is not None and self._clock.now() >= budget.deadline)):
            raise ReducerError("run budget or deadline does not allow candidate repair")
        reset_ids = [repair.node_id]
        if "final-verifier" in self._state.nodes:
            reset_ids.append("final-verifier")
        for node_id in reset_ids:
            runtime = self._node(node_id)
            runtime.state = _initial_node_state(runtime.definition)
            runtime.current_attempt_id = None
            runtime.result = None
            runtime.resume_credits += 1
            runtime.workspace_committed = True
            runtime.continuation = None
        self._state.final_verification_records = []
        self._state.repair_history.append(repair)
        self._state.pending_repair_id = repair.repair_id
        if self.candidate_first:
            self._state.integration_status = "idle"
        return [self._emit(RuntimeEventType.REPAIR_REQUESTED, {
            "repair": repair.to_dict(), "repair_count": self.repair_count,
            "invalidated_nodes": reset_ids, "invalidated_final_evidence": True,
        }, command_id=command.command_id)]

    def _validate_restored_repairs(self) -> None:
        history = self._state.repair_history
        if (len(history) > self.repair_limit or len({item.repair_id for item in history}) != len(history)
            or len({item.prior_attempt_id for item in history}) != len(history)):
            raise ReducerError("invalid persisted primary repair history")
        if self._state.pending_repair_id is not None:
            if not history or self._state.pending_repair_id != history[-1].repair_id:
                raise ReducerError("pending primary repair must be the latest history entry")
            primary = self._node(history[-1].node_id)
            if primary.state in {NodeState.SUCCEEDED, NodeState.VERIFYING, NodeState.WAITING_CHILDREN} or primary.continuation:
                raise ReducerError("pending repair conflicts with a completed or superseding continuation")
        previous_attempt = -1
        for item in history:
            self._validate_repair_identity(item)
            if item.review_result is not None and not any(e.event_type is RuntimeEventType.REPAIR_REQUESTED
                and e.payload.get("repair") == item.to_dict() for e in self._state.events):
                raise ReducerError("review repair lacks its authoritative request audit")
            earlier = history[:history.index(item)]
            if (item.candidate_revision in {r.candidate_revision for r in earlier}
                and not all(self._repair_record_kind(r, item.candidate_revision, item.review_result) == "review_unavailable"
                            for r in item.diagnostics)):
                raise ReducerError("candidate repair repeated a candidate without progress")
            primary = self._node(item.node_id)
            index = next(i for i, a in enumerate(primary.attempts) if a.attempt_id == item.prior_attempt_id)
            if index <= previous_attempt or primary.resume_credits < len(history):
                raise ReducerError("repair history does not match primary activation credits")
            previous_attempt = index

    def validate_consultation(self, parent_id: str, children: tuple[PlanNode, ...], *, restoring: bool = False) -> None:
        from Package.AgentRuntime.Src.Application.Dto.ConsultationPlan import ConsultationChild, ConsultationPlan
        from Package.AgentRuntime.Src.Boot.AgentRuntime import createApp

        with self._lock:
            parent = self._node(parent_id)
            if not self.continuations_enabled or self.contract is None:
                raise ReducerError("durable consultation requires an enabled Runtime with a TaskContract")
            registered = tuple(self._state.nodes)
            if self.collaboration_config:
                incoming = {child.node_id for child in children} - self._state.nodes.keys()
                if len(self._state.nodes) + len(incoming) > self.collaboration_config["max_tasks"]:
                    raise ReducerError("consultation exceeds cumulative agent task budget")
                registered = tuple(dict.fromkeys(self._state.agent_sessions.get(task, {}).get("session_id", task)
                    for task in registered))
            createApp().validate_consultation(ConsultationPlan(
                parent_id, parent.definition.role,
                tuple(effect.value for effect in parent.definition.allowed_effects),
                registered,
                tuple(ConsultationChild(n.node_id, n.role, tuple(e.value for e in n.allowed_effects), n.dependencies, n.criticality) for n in children),
                self.contract.budget.max_agents, parent.continuation_count - int(restoring), self._state.continuation_limit,
            ))
            for child in children:
                self._validate_criticality(child, self.advisory_enabled)
                if not child.objective.strip() or not 1 <= child.max_attempts <= 2:
                    raise ReducerError("consultation requires a bounded objective and attempts")
                existing = self._state.nodes.get(child.node_id)
                if existing is not None and existing.definition != child:
                    raise ReducerError("consultation id conflicts with a registered node")
                if any(n.continuation and child.node_id in {c.node_id for c in n.continuation.children}
                       for n in self._state.nodes.values() if n.definition.node_id != parent_id):
                    raise ReducerError("consultation child already belongs to another primary")

    def _validate_restored_continuations(self) -> None:
        for runtime in self._state.nodes.values():
            context_definitions = [definition for event in self._state.events
                if event.event_type is RuntimeEventType.PRIMARY_SUSPENDED and event.run_id == self.run_id
                for definition in event.payload.get("definitions", [])
                if isinstance(definition, Mapping) and definition.get("node_id") == runtime.definition.node_id
                and definition.get("context_files")]
            if (runtime.definition.context_files or context_definitions) and runtime.definition.to_dict() not in context_definitions:
                raise ReducerError("consultation context_files lacks its immutable pre-dispatch registration")
            continuation = runtime.continuation
            if not self.continuations_enabled and (continuation or runtime.state is NodeState.WAITING_CHILDREN):
                raise ReducerError("v3 snapshots cannot contain durable continuations")
            if not 0 <= runtime.continuation_count <= self._state.continuation_limit:
                raise ReducerError("invalid persisted continuation count")
            if runtime.state is NodeState.WAITING_CHILDREN and continuation is None:
                raise ReducerError("waiting primary has no continuation checkpoint")
            if continuation:
                if runtime.definition.role != "primary" or runtime.continuation_count < 1:
                    raise ReducerError("invalid continuation owner or children")
                for child in continuation.children:
                    if child.node_id not in self._state.nodes or self._node(child.node_id).definition != child:
                        raise ReducerError("continuation children do not match the canonical plan")
                if continuation.kind == "consultation":
                    self.validate_consultation(runtime.definition.node_id, continuation.children, restoring=True)
                else:
                    self._validate_dependency_continuation(runtime.definition.node_id, continuation, restoring=True)
                self._validate_checkpoint_record(runtime.definition.node_id, continuation)

    def _validate_dependency_continuation(self, node_id, continuation, *, restoring=False):
        from minicode.dependency_waits import WaitSpec
        wait = continuation.dependency_wait
        if (not self.continuations_enabled or continuation.kind != "dependency" or continuation.children
            or not isinstance(wait, Mapping) or not wait.get("wait_id")
            or wait != self._state.dependency_waits.get(wait["wait_id"])
            or wait.get("owner_node_id") != node_id or self._node(node_id).definition.role != "primary"):
            raise ReducerError("dependency continuation requires its exact registered primary wait")
        if not restoring or self.node_state(node_id) in {NodeState.READY, NodeState.RUNNING, NodeState.WAITING_CHILDREN}:
            self._validate_wait_targets(WaitSpec.from_dict(wait))
        if not restoring and (self._node(node_id).continuation_count >= self._state.continuation_limit
                              or self.wait_resumes(wait["wait_id"])):
            raise ReducerError("dependency continuation is exhausted or already consumed")

    def _validate_checkpoint_record(self, node_id: str, continuation: WorkerContinuation) -> None:
        if (continuation.schema_version != "minicode-continuation-v1"
            or not continuation.continuation_id
            or len(continuation.input_revision) != 64
            or any(c not in "0123456789abcdef" for c in continuation.input_revision)):
            raise ReducerError("continuation identity and SHA-256 candidate revision are required")
        ref = continuation.checkpoint
        if ref.size <= 0 or not ref.store_key.replace("\\", "/").startswith(f"{self.run_id}/{node_id}/"):
            raise ReducerError("checkpoint must be a retained artifact owned by this run and primary")

    @property
    def run_id(self) -> str:
        return self._state.run_id

    @property
    def state_path(self) -> Path | None:
        """The durable file this Reducer persists to, or ``None`` when memory-only.

        An operator surface must compare this against the file it was pointed at:
        operating on "a" state file while claiming to act on this run is exactly
        how a stale snapshot gets written over a live one.
        """

        return None if self._store is None else Path(self._store.path)

    @property
    def revision(self) -> int:
        """The revision of the snapshot this Reducer currently holds."""

        return self._state.revision

    @property
    def current_plan(self) -> PlanVersion:
        if self._state.user_steering is None:
            return self._state.plans[self._state.current_plan_version]
        from minicode.runtime_steering import effective_plan
        return effective_plan(self._state.plans[self._state.current_plan_version], self._state.user_steering)

    @property
    def contract(self) -> TaskContract | None:
        if self._state.user_steering is None:
            return self._state.contract
        from minicode.runtime_steering import effective_contract
        return effective_contract(self._state.contract, self._state.user_steering)

    def enable_user_steering(self, node_id: str) -> None:
        """Bind the authenticated human inbox only to the public task owner."""
        from minicode.runtime_steering import initial_state
        with self._lock:
            self._node(node_id)
            if self._state.user_steering is not None:
                if self._state.user_steering["node_id"] != node_id:
                    raise ReducerError("human steering is already bound to another owner")
                return
            self._state.user_steering = initial_state(node_id)
            self._persist()

    def wake_for_user_input(self) -> bool:
        """Resume an owned checkpoint for dialogue; dependency facts stay intact."""
        with self._lock:
            state = self._state.user_steering
            if (state is None or self._state.canceled or self._state.paused
                or not any(r["status"] == "accepted" for r in state["inputs"].values())):
                return False
            owner = self._node(state["node_id"])
            if owner.state is not NodeState.WAITING_CHILDREN or owner.continuation is None:
                return False
            owner.state = NodeState.READY
            owner.resume_credits += 1
            owner.current_attempt_id = None
            owner.result = None
            self._emit(RuntimeEventType.PRIMARY_RESUMABLE, {"node_id": state["node_id"],
                "reason": "human_input", "original_wait_preserved": True})
            self._persist()
            return True

    @property
    def identity(self) -> RuntimeRunIdentity | None:
        return self._state.identity

    def node_state(self, node_id: str) -> NodeState:
        with self._lock:
            return self._node(node_id).state

    def node_result(self, node_id: str) -> AgentResult | None:
        with self._lock:
            return self._node(node_id).result

    def usage_summary(self, attempt_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            summary = self._state.usage_ledger.summary(attempt_id)
            if attempt_id is None:
                summary["tokens"] = self._state.tokens_used
                summary["known_cost"] = self._state.cost_used
                summary["cost"] = self._state.cost_used if summary["cost_known"] else None
            return summary

    def quote_model_call(self, **request: Any) -> dict[str, Any]:
        """Side-effect-free admission estimate; never creates calls or events."""
        return self._model_call_admission(commit=False, **request)

    def reserve_model_call(self, **request: Any) -> dict[str, Any]:
        return self._model_call_admission(commit=True, **request)

    def _model_call_admission(
        self,
        *,
        commit: bool,
        node_id: str,
        attempt_id: str,
        call_id: str,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        pricing: TokenPricing | None,
        budget: BudgetEnvelope,
        attempt_budget: BudgetEnvelope,
        minimum_output_tokens: int = 1,
        post_delivery_learning: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            attempt = self.current_attempt(node_id)
            if (
                self._state.canceled or self._state.paused or attempt is None
                or attempt.attempt_id != attempt_id
                or attempt.plan_version != self._state.current_plan_version
            ):
                raise BudgetUnavailable("model request has no live current Worker lease")
            if post_delivery_learning:
                from minicode.workspace_files import is_internal_state_path, workspace_changes_integrated

                result = self._node(node_id).result
                if (node_id != "primary" or attempt.state is not NodeState.SUCCEEDED
                        or self._node(node_id).state is not NodeState.SUCCEEDED
                        or not workspace_changes_integrated(candidate_first=self.candidate_first,
                            status=self.integration_status, primary_committed=self._node(node_id).workspace_committed)
                        or not self._state.final_verification_records
                        or any(record.passed is not True for record in self._state.final_verification_records)
                        or result is None or result.attempt_id != attempt_id
                        or not any(not is_internal_state_path(name) for name in result.changed_resources)
                        or any(call.get("purpose") == "memory_consolidate" or
                            (call.get("usage") or {}).get("prepared_request", {}).get("call_identity", {}).get("purpose") == "memory_consolidate"
                            for call in self._state.usage_ledger.calls.values())):
                    raise BudgetUnavailable("post-delivery learning requires an accepted project change and an unused learning allowance")
            elif attempt.state is not NodeState.RUNNING or not attempt.lease.is_valid(self._clock.now()):
                raise BudgetUnavailable("model request has no live current Worker lease")
            process = self.required_consultation_status() if node_id == "primary" else None
            if process and process["blocked"]:
                raise BudgetUnavailable(process["reason"])
            global_budget = self._state.contract.budget if self._state.contract else budget
            ledger = self._state.usage_ledger
            if self.closure_reserve_enabled:
                if any(not ledger.budget_reconciled(cap) for cap in (global_budget, attempt_budget)):
                    raise ModelUsageUnreconciled("bounded usage requires reconciliation before another request",
                        diagnostics=ledger.reconciliation_diagnostics())
                if pricing is None and any(cap.max_cost is not None for cap in (global_budget, attempt_budget)):
                    raise BudgetUnavailable("max_cost requires explicit pricing for this provider/model")
            request = dict(
                call_id=call_id, node_id=node_id, attempt_id=attempt_id, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens, pricing=pricing,
                minimum_output_tokens=minimum_output_tokens,
                budget=self.effective_model_budget(node_id, global_budget),
                attempt_budget=attempt_budget, tokens_used=self._state.tokens_used,
                cost_used=self._state.cost_used, now=self._clock.now(),
            )
            if not commit:
                return ledger.quote(**request)
            try:
                record = ledger.reserve(**request)
            except BudgetUnavailable as error:
                error.diagnostics["run_limit_kind"] = (
                    "closure_protected" if request["budget"] != global_budget else "contract"
                )
                error.diagnostics["contract_token_limit"] = global_budget.max_tokens
                error.diagnostics["contract_cost_limit"] = global_budget.max_cost
                if not self.closure_reserve_enabled or node_id in {"primary", "final-verifier"}:
                    raise
                # A local helper ceiling is not a waiver of a global refusal.
                # Quote without mutation: failed admission never creates a call.
                try:
                    ledger.quote(**{**request, "budget": global_budget,
                                    "attempt_budget": replace(global_budget, deadline=attempt_budget.deadline)})
                except BudgetUnavailable as global_error:
                    global_error.diagnostics["run_limit_kind"] = "contract"
                    global_error.diagnostics["contract_token_limit"] = global_budget.max_tokens
                    global_error.diagnostics["contract_cost_limit"] = global_budget.max_cost
                    raise
                denial = {"node_id": node_id, "attempt_id": attempt_id, "scope": "helper_request", "reason": str(error)}
                # The admission controller may retry while another in-flight
                # request settles. One identical diagnosis per attempt suffices.
                if not any(event.event_type is RuntimeEventType.CONSULTATION_BUDGET_DECLINED
                           and event.payload == denial for event in self._state.events):
                    self._emit(RuntimeEventType.CONSULTATION_BUDGET_DECLINED, denial)
                    self._persist()
                raise ConsultationBudgetLimited(str(error), diagnostics=error.diagnostics) from error
            if post_delivery_learning:
                # Reserve the once-per-run allowance atomically, before HTTP.
                record["purpose"] = "memory_consolidate"
                ledger.calls[call_id]["purpose"] = "memory_consolidate"
            self._emit(RuntimeEventType.MODEL_CALL_RESERVED, record)
            self._persist()
            return record

    def record_model_request_declined(self, node_id: str, attempt_id: str, diagnostics: Mapping[str, Any]) -> None:
        """Persist a terminal pre-HTTP refusal separately from admitted usage.

        The admission controller calls this only after waiting for active calls;
        pure quotes and temporary refusals do not create diagnostic events.
        """
        with self._lock:
            if not any(attempt.attempt_id == attempt_id for attempt in self._node(node_id).attempts):
                raise ReducerError("request refusal does not identify a registered attempt")
            self._emit(RuntimeEventType.MODEL_REQUEST_DECLINED, {
                "node_id": node_id, "attempt_id": attempt_id,
                "diagnostics": deepcopy(dict(diagnostics)),
            })
            self._persist()

    def record_model_usage(
        self, call_id: str, usage: Mapping[str, Any], *, complete: bool, outcome: str,
    ) -> None:
        # Billing outlives leases, cancellation, and plan replacement. Only a
        # previously admitted call can settle; duplicate settlements are idempotent.
        with self._lock:
            ledger = self._state.usage_ledger
            previous = dict(ledger.calls[call_id])
            tokens, cost = ledger.settle(call_id, usage, complete=complete, outcome=outcome)
            self._state.tokens_used += tokens
            self._state.cost_used = add_amounts(self._state.cost_used, cost)
            if previous != ledger.calls[call_id]:
                self._emit(RuntimeEventType.MODEL_USAGE_RECORDED, {
                    "call_id": call_id, "usage": dict(usage), "complete": complete, "outcome": outcome,
                })
            self._persist()

    def record_result_usage(self, result: AgentResult) -> None:
        """Compatibility accounting for Workers without a model-call ledger."""
        with self._lock:
            if result.run_id != self.run_id:
                raise ReducerError("usage result run_id does not match Runtime")
            runtime = self._node(result.node_id)
            if not any(a.attempt_id == result.attempt_id for a in runtime.attempts):
                raise ReducerError("usage result does not identify a registered attempt")
            self._record_result_usage_unlocked(result)
            self._persist()

    def _record_result_usage_unlocked(self, result: AgentResult) -> None:
        ledger = self._state.usage_ledger
        if result.attempt_id in ledger.result_attempts or ledger.has_calls(result.attempt_id):
            return
        self._state.tokens_used += max(0, int(result.usage.get("tokens", 0) or 0))
        self._state.cost_used = add_amounts(
            self._state.cost_used, max(0.0, float(result.usage.get("cost", 0.0) or 0.0)),
        )
        ledger.result_attempts.add(result.attempt_id)
        ledger.result_usage[result.attempt_id] = dict(result.usage)

    def current_attempt(self, node_id: str) -> NodeAttempt | None:
        with self._lock:
            runtime = self._node(node_id)
            if runtime.current_attempt_id is None:
                return None
            return next(
                (item for item in runtime.attempts if item.attempt_id == runtime.current_attempt_id),
                None,
            )

    def ready_node_ids(self) -> tuple[str, ...]:
        with self._lock:
            self._refresh_ready_nodes()
            return tuple(
                node_id
                for node_id, runtime in self._state.nodes.items()
                if runtime.state is NodeState.READY and self.builder_may_execute(node_id)
            )

    def terminal(self) -> bool:
        terminal_states = {
            NodeState.SUCCEEDED,
            NodeState.FAILED_TERMINAL,
            NodeState.CANCELED,
            NodeState.SUPERSEDED,
        }
        with self._lock:
            discarded = self.discarded_builder_ids()
            return all(node.state in terminal_states or node_id in discarded for node_id, node in self._state.nodes.items())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def primary_builder_context(self, invocation: AgentInvocationSpec) -> dict[str, Any]:
        """Read one consistent current task inventory for a live primary request."""
        from minicode.builder_request_context import packet
        with self._lock:
            self._validate_primary_context_owner(invocation)
            return packet(self._snapshot_unlocked(), invocation)

    def primary_agent_context(self, invocation: AgentInvocationSpec) -> dict[str, Any]:
        """Read async task facts without copying the event log or checkpoint bodies."""
        from minicode.agent_request_context import packet
        with self._lock:
            self._validate_primary_context_owner(invocation)
            if not self.async_agents_enabled:
                raise ReducerError("async agent context requires enabled native async agents")
            verified = self.resolved_agent_failures()
            return packet(self._state, invocation, resolved_failures=verified,
                dismissed_predecessors=self.resolved_agent_failures(include_dismissed=True) - verified,
                blocking_tasks=self.completion_blockers())

    def _validate_primary_context_owner(self, invocation: AgentInvocationSpec) -> None:
        """Caller holds the reducer lock for validation and projection together."""
        node = self._state.nodes.get(invocation.node_id)
        attempt = self.current_attempt(invocation.node_id) if node is not None else None
        if (invocation.node_id != "primary" or invocation.role != "primary"
            or invocation.run_id != self.run_id or invocation.plan_version != self.current_plan.plan_version
            or invocation.context_manifest.contract_revision != self._state.contract_revision
            or self._state.canceled or self._state.paused or self._persistence_error is not None
            or self._state.node_cancel_epoch.get(invocation.node_id, 0) or attempt is None
            or attempt.attempt_id != invocation.attempt_id or attempt.state is not NodeState.RUNNING
            or node.state is not NodeState.RUNNING
            or attempt.idempotency_key != invocation.idempotency_key
            or not attempt.lease.is_valid(self._clock.now())):
            raise ReducerError("Runtime context requires the current live primary attempt")
        owner = getattr(self._store, "owner_lease", None)
        if owner is not None:
            owner.validate_binding(self)

    def agent_wait_projection(self) -> list[dict[str, Any]]:
        """Expose the recorded return reason, never reevaluate a past timeout."""
        with self._lock:
            returned = {event.payload["wait_id"]: event.payload
                for event in self._state.events
                if event.event_type is RuntimeEventType.PRIMARY_RESUMABLE and "wait_id" in event.payload}
            return [{"waitId": wait_id, "nodeId": wait["owner_node_id"],
                     "status": returned[wait_id].get("wait_status", "returned") if wait_id in returned else "waiting"}
                    for wait_id, wait in self._state.dependency_waits.items()]

    def activity_snapshot(self) -> dict[str, Any]:
        """Small public progress projection; never copy the event log or context."""
        with self._lock:
            return {
                "executionId": self.run_id,
                "planVersion": self._state.current_plan_version,
                "agentWaits": self.agent_wait_projection(),
                **({"agentTeam": self.agent_team_view()} if self._state.agent_team is not None else {}),
                **({"agentTasks": [
                    self.agent_task_projection(record)
                    for record in self._state.agent_sessions.values()
                ]} if self.async_agents_enabled else {}),
                **({"agentMessages": [{k: record[k] for k in ("message_id", "sender_id", "recipient_id", "kind", "state", "sequence", "reply_to") if k in record}
                    for record in self._state.agent_messages.values()]} if self.agent_messages_enabled else {}),
                "nodes": [
                    {
                        "nodeId": node.node_id,
                        "objective": node.objective[:1200],
                        "role": node.role,
                        "dependencies": list(node.dependencies),
                        "state": runtime.state.value,
                        **({"reviewPolicy": {"scope": node.review_policy["scope"], "required": node.review_policy["required"]}}
                           if node.review_policy is not None else {}),
                        **({"reviewStatus": "unavailable"} if self.optional_review_unavailable(node.node_id) else {}),
                        **({"criticality": node.criticality,
                            "diagnosis": self._public_node_diagnosis(node.node_id),
                            "advisoryReason": (runtime.advisory_acknowledgment or {}).get("reason", "")}
                           if self.advisory_enabled else {}),
                        "attemptId": runtime.current_attempt_id,
                        "attemptCount": len(runtime.attempts),
                        "recovery": child_recovery(node.node_id, runtime.state.value,
                            [effect.value for effect in node.allowed_effects],
                            [{"attempt_id": a.attempt_id, "state": a.state.value} for a in runtime.attempts]),
                        **({"repairCount": self.repair_count} if self.repair_limit and runtime.definition.role == "primary" else {}),
                        **({"continuationCount": runtime.continuation_count, "resumeCredits": runtime.resume_credits} if self.continuations_enabled else {}),
                        "summary": runtime.result.output_summary[:2000] if runtime.result else "",
                    }
                    for node in self.current_plan.nodes
                    for runtime in [self._state.nodes[node.node_id]]
                ],
            }

    def retry_feedback(self, node_id: str) -> tuple[str, ...]:
        """Return bounded diagnostics for this node's current-plan attempts."""
        with self._lock:
            attempt_ids = {
                attempt.attempt_id for attempt in self._node(node_id).attempts
                if attempt.plan_version == self._state.current_plan_version
            }
            diagnostics = [
                f"Attempt {event.payload['attempt_id']}: {event.payload['reason']}"[:4000]
                for event in self._state.events
                if event.event_type in {
                    RuntimeEventType.VERIFICATION_FAILED, RuntimeEventType.RESULT_ACCEPTED,
                }
                and event.payload.get("node_id") == node_id
                and event.payload.get("attempt_id") in attempt_ids
                and event.payload.get("reason")
            ]
            return tuple(diagnostics[-3:])

    def _public_node_diagnosis(self, node_id: str) -> dict[str, Any]:
        diagnosis = self.node_diagnosis(node_id)
        return {"category": diagnosis["category"], "failureKind": diagnosis["failure_kind"],
                "fallbackAllowed": diagnosis["fallback_allowed"], "acknowledged": diagnosis["acknowledged"]}

    def set_final_verification_records(
        self,
        records: tuple[VerificationRecord, ...],
    ) -> None:
        """Durably replace the final-gate evidence for the current snapshot."""

        with self._lock:
            self._state.final_verification_records = list(records)
            self._persist()

    def apply(self, command: RuntimeCommand) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            if command.run_id != self._state.run_id:
                raise ReducerError("command run_id does not match reducer run")
            prior_ids = self._state.processed_commands.get(command.command_id)
            if prior_ids is not None:
                by_id = {event.event_id: event for event in self._state.events}
                return tuple(by_id[event_id] for event_id in prior_ids if event_id in by_id)

            handlers: dict[RuntimeCommandType, Callable[[RuntimeCommand], list[RuntimeEvent]]] = {
                RuntimeCommandType.CLAIM_NODE: self._claim_node,
                RuntimeCommandType.START_ATTEMPT: self._start_attempt,
                RuntimeCommandType.RENEW_LEASE: self._renew_lease,
                RuntimeCommandType.SUBMIT_RESULT: self._submit_result,
                RuntimeCommandType.BEGIN_VERIFICATION: self._begin_verification,
                RuntimeCommandType.ACCEPT_VERIFICATION: self._accept_verification,
                RuntimeCommandType.FAIL_VERIFICATION: self._fail_verification,
                RuntimeCommandType.CANCEL_RUN: self._cancel_run,
                RuntimeCommandType.CANCEL_TASK: self._cancel_task_command,
                RuntimeCommandType.RESUME_RUN: self._resume_run,
                RuntimeCommandType.AMEND_TASK: self._amend_task,
                RuntimeCommandType.APPROVE_TOOL_CALL: self._approval,
                RuntimeCommandType.REJECT_TOOL_CALL: self._approval,
                RuntimeCommandType.REQUEST_REPAIR: self._request_repair,
                RuntimeCommandType.ACKNOWLEDGE_ADVISORY_FAILURE: self._acknowledge_advisory_failure,
            }
            handler = handlers.get(command.command_type)
            if handler is None:
                raise ReducerError(f"unsupported command: {command.command_type.value}")
            # Retry only persistence of a heartbeat, while the previously
            # durable lease is valid. Keep the lock so no Worker can observe
            # an uncommitted lease or mutate the snapshot between attempts.
            renewal = (self.current_attempt(str(command.payload.get("node_id", "")))
                       if command.command_type is RuntimeCommandType.RENEW_LEASE else None)
            events = handler(command)
            events.extend(self._wake_continuations())
            self._state.processed_commands[command.command_id] = tuple(
                event.event_id for event in events
            )
            self._refresh_ready_nodes()
            self._persist(retry_until=renewal.lease.lease_expiry if renewal is not None else None)
            return tuple(events)

    def install_plan(
        self,
        plan: PlanVersion,
        amendment: TaskAmendment,
        *,
        contract: TaskContract,
        identity: RuntimeRunIdentity,
    ) -> RuntimeEvent:
        """Install a replan atomically through the reducer."""
        with self._lock:
            event = self._install_plan_unlocked(
                plan,
                amendment,
                contract=contract,
                identity=identity,
            )
            self._refresh_ready_nodes()
            self._persist()
            return event

    def _install_plan_unlocked(
        self,
        plan: PlanVersion,
        amendment: TaskAmendment,
        *,
        contract: TaskContract,
        identity: RuntimeRunIdentity,
        command_id: str | None = None,
    ) -> RuntimeEvent:
        current_contract = self._state.contract
        if self._state.agent_sessions:
            raise ReducerError("amending a run with registered async sessions requires a new run")
        if self.candidate_first and (self.integration_status != "idle" or any(n.attempts for n in self._state.nodes.values())):
            raise ReducerError("amending an activated candidate-first run requires a new run")
        if self._state.repair_history or any(n.continuation is not None for n in self._state.nodes.values()):
            raise ReducerError("replanning a checkpointed run requires a new run until scoped continuation amendments are supported")
        current_identity = self._state.identity
        if current_contract is None or current_identity is None:
            raise ReducerError("TaskAmendment requires persisted TaskContract and RunIdentity")
        if plan.run_id != self.run_id or amendment.run_id != self.run_id:
            raise ReducerError("replan run_id mismatch")
        if identity != current_identity:
            raise ReducerError("immutable RunIdentity mismatch")
        if plan.plan_version <= self._state.current_plan_version:
            raise ReducerError("new plan version must increase")
        if plan.supersedes != self._state.current_plan_version:
            raise ReducerError("new plan must supersede the current PlanVersion")
        if amendment.base_contract_revision != self._state.contract_revision:
            raise ReducerError("amendment base revision is stale")
        if amendment.next_contract_revision <= amendment.base_contract_revision:
            raise ReducerError("amendment next revision must increase")
        if plan.contract_revision != amendment.next_contract_revision:
            raise ReducerError("plan and amendment revisions differ")
        if contract.envelope.run_id != self.run_id:
            raise ReducerError("amended TaskContract run_id mismatch")
        if contract.contract_revision != plan.contract_revision:
            raise ReducerError("amended TaskContract revision mismatch")
        immutable_envelope_fields = (
            "run_id",
            "request",
            "requested_profile",
            "session_id",
        )
        changed_identity_fields = [
            name
            for name in immutable_envelope_fields
            if getattr(contract.envelope, name) != getattr(current_contract.envelope, name)
        ]
        if normalize_workspace_path(contract.envelope.workspace) != normalize_workspace_path(
            current_contract.envelope.workspace
        ):
            changed_identity_fields.append("workspace")
        if contract.budget != current_contract.budget:
            changed_identity_fields.append("budget")
        if contract.required_consultation != current_contract.required_consultation:
            changed_identity_fields.append("required_consultation")
        if changed_identity_fields:
            raise ReducerError(
                "TaskAmendment changes immutable RunIdentity fields: "
                + ", ".join(changed_identity_fields)
            )

        if self.closure_reserve_enabled and (
            [(n.node_id, n.role) for n in plan.nodes if n.role == "primary"] != [("primary", "primary")]
            or self._resolve_closure_budget(plan, contract) != self._state.closure_budget
        ):
            raise ReducerError("changing the primary/final-verifier reserve policy requires a new run")

        from minicode.runtime_protocol import validate_review_plan
        validate_review_plan(plan.nodes)
        frozen_review = {n.node_id: (n.runtime_profile, n.review_policy) for n in self.current_plan.nodes
                         if n.node_id in {"primary", "final-verifier"}}
        for node in plan.nodes:
            if node.node_id in frozen_review and (node.runtime_profile, node.review_policy) != frozen_review[node.node_id]:
                raise ReducerError("changing the admitted execution depth or review policy requires a new run")

        affected = set(amendment.affected_node_ids)
        for node_id in affected:
            runtime = self._state.nodes.get(node_id)
            if runtime is not None:
                runtime.state = NodeState.SUPERSEDED
        self._state.plans[plan.plan_version] = plan
        self._state.current_plan_version = plan.plan_version
        self._state.contract_revision = plan.contract_revision
        self._state.contract = contract

        next_nodes: dict[str, NodeRuntime] = {}
        for node in plan.nodes:
            prior = self._state.nodes.get(node.node_id)
            if (
                prior is not None
                and node.node_id not in affected
                and prior.state is NodeState.SUCCEEDED
                and prior.definition == node
            ):
                next_nodes[node.node_id] = prior
            else:
                next_nodes[node.node_id] = NodeRuntime(node, _initial_node_state(node))
        # ``nodes`` is always the current PlanVersion projection.  Prior
        # versions remain durable in ``plans`` and the event log, but may
        # not keep a hidden runnable node in the active state machine.
        self._state.nodes = next_nodes
        return self._emit(
            RuntimeEventType.TASK_AMENDED,
            {
                "amendment": amendment.to_dict(),
                "plan_version": plan.plan_version,
                "superseded_nodes": sorted(affected),
            },
            command_id=command_id,
        )

    def register_delegated_node(
        self,
        node: PlanNode,
        *,
        parent_node_id: str,
        dependency_predicates: Mapping[str, str] | None = None,
        builder_idempotency_key: str | None = None,
        agent_session: Mapping[str, Any] | None = None,
        parent_attempt_id: str | None = None,
        builder_recovery: Mapping[str, Any] | None = None,
    ) -> RuntimeEvent | None:
        """Append a compatibility delegation to the one canonical PlanGraph.

        This is not a replan: the contract and dependencies are unchanged and
        the parent is synchronously suspended.  The append is nevertheless
        serialized and audited by the Reducer, never held in a shadow graph.

        The CREATION parent is recorded separately from `node.dependencies`, so a
        subtree cancellation can follow who registered whom without pretending
        that a dependency edge is an ownership edge.
        """
        with self._lock:
            if builder_recovery is not None:
                from minicode.builder_recovery import validate_record
                attempt = self.current_attempt(parent_node_id)
                previous_id = builder_recovery.get("replaces_task_id")
                if (parent_node_id != "primary" or attempt is None or attempt.attempt_id != parent_attempt_id
                    or self.node_state(parent_node_id) is not NodeState.RUNNING
                    or not attempt.lease.is_valid(self._clock.now()) or self._state.canceled or self._state.paused
                    or self.node_cancel_epoch(previous_id) or not self.builder_may_promote(previous_id)):
                    raise ReducerError("Builder recovery requires the live primary and an owned uncanceled producer")
                validate_record(builder_recovery, node=node, nodes=self._state.nodes,
                    patches=self._state.patch_records, recoveries=self.builder_recoveries())
            if agent_session is not None:
                from minicode.agent_workspaces import valid_agent_assignment
                if "optional" in agent_session and not self.collaboration_config:
                    raise ReducerError("optional assignments require continuous collaboration")
                previous_id = agent_session.get("predecessor_task_id") or agent_session.get("replaces_task_id")
                if previous_id and (previous_id in self.dismissed_agent_ids()
                    or agent_session.get("optional", False) != self._state.agent_sessions.get(previous_id, {}).get("optional", False)):
                    raise ReducerError("continued work cannot change or revive a dismissed obligation")
                parent = self._node(parent_node_id)
                attempt = self.current_attempt(parent_node_id)
                if (not self.async_agents_enabled or self._state.canceled or self._state.paused
                    or parent_node_id != "primary" or parent.state is not NodeState.RUNNING
                    or attempt is None or attempt.attempt_id != parent_attempt_id
                    or not attempt.lease.is_valid(self._clock.now())
                    or not valid_agent_assignment(node, agent_session, shared_writes=self.shared_workspace_writes_enabled)
                    or (node.role == "shared_writer" and not parent.definition.is_writer)
                    or not node.node_id.startswith("agent-")
                    or agent_session.get("definition") != node.to_dict()
                    or agent_session.get("parent_node_id") != parent_node_id
                    or agent_session.get("contract_revision") != self.current_plan.contract_revision):
                    raise ReducerError("async registration requires the live owning primary and an exact authorized assignment")
                if node.node_id not in self._state.nodes:
                    self.validate_agent_budget(agent_session["session_id"])
            if node.criticality != "required":
                raise ReducerError("synchronous delegations are always required")
            if node.node_id in self._state.nodes:
                existing = self._state.nodes[node.node_id]
                if existing.definition != node:
                    raise ReducerError("delegation id collides with a different node")
                if agent_session is not None and dict(agent_session) != self._state.agent_sessions.get(node.node_id):
                    raise ReducerError("agent registration conflicts with an existing operation")
                if dependency_predicates is not None:
                    recorded = self._state.dependency_predicates.get(node.node_id, {})
                    if dict(dependency_predicates) != recorded:
                        raise ReducerError("delegation predicates collide with a different registration")
                return None
            if agent_session is not None and agent_session.get("predecessor_task_id"):
                previous_id = agent_session["predecessor_task_id"]
                previous = self._state.agent_sessions.get(previous_id, {})
                interrupted = self.is_agent_interrupted(previous_id)
                if (not self.agent_resume_enabled or (self._node(previous_id).state is not NodeState.SUCCEEDED and not interrupted)
                    or (self.node_cancel_epoch(previous_id) and not interrupted)
                    or (interrupted and agent_session.get("continuation_kind") != "interrupted")
                    or agent_session.get("session_id") != previous.get("session_id")
                    or agent_session.get("generation") != previous.get("generation", 0) + 1
                    or agent_session["generation"] > self.agent_generation_limit
                    or any(r.get("predecessor_task_id") == previous_id for r in self._state.agent_sessions.values())
                    or agent_session.get("resume_checkpoint") != self._state.agent_checkpoints.get(previous_id)):
                    raise ReducerError("continued session requires its idle current generation and owned checkpoint")
            if agent_session is not None and agent_session.get("recovery_source"):
                from minicode.agent_recovery import validate_recovery_record
                if not self.agent_resume_enabled or any(r["session_id"] == agent_session["session_id"] for r in self._state.agent_sessions.values()):
                    raise ReducerError("retained session requires enabled continuation and a unique current owner")
                validate_recovery_record(agent_session, run_id=self.run_id, generation_limit=self.agent_generation_limit)
            if agent_session is not None and agent_session.get("replaces_task_id"):
                previous_id = agent_session["replaces_task_id"]
                self.validate_agent_reassignment(previous_id)
                previous = self._state.agent_sessions[previous_id]
                if (agent_session.get("session_id") != previous["session_id"]
                    or agent_session.get("generation") != previous["generation"] + 1 or agent_session["generation"] > self.agent_generation_limit
                    or node.objective != previous["definition"]["objective"]
                    or any((r.get("predecessor_task_id") or r.get("replaces_task_id")) == previous_id for r in self._state.agent_sessions.values())
                    or agent_session.get("resume_checkpoint") or agent_session.get("predecessor_task_id")):
                    raise ReducerError("reassignment must advance the sole current generation without weakening failed work")
            if agent_session is not None or node.node_id.startswith("builder-"):
                if node.node_id.startswith("builder-") and self._state.agent_team is not None and not self.agent_team_writes_enabled:
                    raise ReducerError("team writes are disabled; enable runtimeAgentTeamWrites explicitly")
                self.validate_team_admission(agent_session["session_id"] if agent_session else
                    self.builder_lineage(builder_recovery["replaces_task_id"])[0] if builder_recovery else node.node_id)
            parent = self._node(parent_node_id)
            if parent.state not in {NodeState.RUNNING, NodeState.WAITING_INPUT, NodeState.WAITING_APPROVAL,
                                    NodeState.WAITING_CHILDREN}:
                raise ReducerError("delegation parent is not active")
            if node.alternative_group in self._state.candidate_selections:
                raise ReducerError("cannot add alternatives after their group has a selection")
            if set(node.dependencies) - self._state.nodes.keys() or node.node_id in node.dependencies:
                raise ReducerError("delegation dependencies require distinct registered tasks")
            if any(not self.builder_may_execute(task_id) for task_id in node.dependencies):
                raise ReducerError("cannot depend on a discarded alternative")
            if any(task_id not in node.dependencies or predicate not in {
                "artifact_validated", "patch_applied_and_checked", "terminal_observed"}
                for task_id, predicate in (dependency_predicates or {}).items()):
                raise ReducerError("invalid delegation dependency predicate")
            plan = self._state.plans[self._state.current_plan_version]
            self._state.plans[plan.plan_version] = replace(plan, nodes=(*plan.nodes, node))
            # A delegated node with declared dependencies starts PENDING and is
            # released by `_refresh_ready_nodes` when its own edges are satisfied,
            # so one child finishing does not wait for its siblings.
            self._state.nodes[node.node_id] = NodeRuntime(node, _initial_node_state(node))
            self._state.delegation_parents[node.node_id] = parent_node_id
            if agent_session is not None:
                self._state.agent_sessions[node.node_id] = deepcopy(dict(agent_session))
            if dependency_predicates:
                for task_id, predicate in dependency_predicates.items():
                    if task_id not in node.dependencies:
                        raise ReducerError("a dependency predicate must name a declared dependency")
                    if predicate not in {"artifact_validated", "patch_applied_and_checked",
                                         "terminal_observed"}:
                        raise ReducerError("unknown dependency predicate")
                self._state.dependency_predicates[node.node_id] = dict(dependency_predicates)
            event = self._emit(
                RuntimeEventType.NODE_READY,
                {"node_id": node.node_id, "parent_node_id": parent_node_id, "delegated": True,
                 "definition": node.to_dict(),
                 "creation_parent": parent_node_id,
                 **({"agent_session": deepcopy(dict(agent_session))} if agent_session is not None else {}),
                 **({"builder_idempotency_key": builder_idempotency_key} if builder_idempotency_key is not None else {}),
                 **({"builder_recovery": deepcopy(dict(builder_recovery))} if builder_recovery is not None else {}),
                 "dependency_predicates": dict(dependency_predicates or {}),
                 "state": self._state.nodes[node.node_id].state.value},
            )
            self._persist()
            return event

    # ---- durable child results, waits and patch receipts -----------------

    def record_worker_completion(self, result: AgentResult) -> None:
        """Persist settlement even when cancellation fences off SUBMIT_RESULT."""
        with self._lock:
            runtime = self._node(result.node_id)
            if result.run_id != self.run_id or not any(a.attempt_id == result.attempt_id for a in runtime.attempts):
                raise ReducerError("worker settlement does not identify a registered attempt")
            recovery = dict(result.metrics.get("candidate_recovery") or {})
            uncertain = bool(result.failure and result.failure.ambiguous_side_effect) or (
                recovery.get("reason") == "uncertain_side_effect_or_inflight_tool")
            record = {"node_id": result.node_id, "attempt_id": result.attempt_id, "run_id": result.run_id,
                "candidate_id": "primary" if result.node_id == "primary" else f"candidate-{result.node_id}",
                "result_status": result.status.value, "ambiguous_side_effect": uncertain,
                "recovery": recovery, "artifact_refs": [ref.to_dict() for ref in result.artifact_refs]}
            if recovery.get("status") == "archived_unsubmitted" and not uncertain:
                # User cancellation must fence SUBMIT_RESULT immediately. Keep
                # the exited Worker's recovery evidence separately, without
                # reviving its node or granting permission to publish its bytes.
                record["recovery_result"] = result.to_dict()
            existing = self._state.worker_completions.get(result.attempt_id)
            if existing is not None:
                if existing != record:
                    raise ReducerError("worker settlement cannot change for an existing attempt")
                return
            self._state.worker_completions[result.attempt_id] = record
            if uncertain:
                self._state.candidate_quarantines[result.attempt_id] = {**record, "status": "pending"}
            self._emit(RuntimeEventType.WORKER_SETTLED, record)
            self._persist()

    def pending_candidate_quarantines(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy({key: record for key, record in self._state.candidate_quarantines.items()
                             if record["status"] == "pending"})

    def candidate_quarantines(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._state.candidate_quarantines)

    def retire_candidate_quarantine(self, attempt_id: str, evidence: Mapping[str, Any]) -> None:
        """Native-only receipt; never rehabilitates the failed result or candidate."""
        with self._lock:
            record = self._state.candidate_quarantines[attempt_id]
            if record["status"] == "retired":
                return
            if (evidence.get("disposition") != "retired_isolated_candidate"
                or evidence.get("attempt_id") != attempt_id
                or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("candidate_revision", "")))):
                raise ReducerError("retirement needs native isolated candidate evidence")
            record = {**record, "status": "retired", "evidence": dict(evidence)}
            self._state.candidate_quarantines[attempt_id] = record
            self._emit(RuntimeEventType.CANDIDATE_RETIRED, record)
            self._persist()

    def builder_may_execute(self, node_id: str) -> bool:
        with self._lock:
            if node_id in self.replaced_builder_ids():
                return False
            group = self._node(node_id).definition.alternative_group
            selection = self._state.candidate_selections.get(group)
            return selection is None or selection["selected_node_id"] == node_id

    def node_group(self, node_id: str) -> str | None:
        """The alternative group a node competes in, or ``None`` for a lone task."""

        with self._lock:
            runtime = self._state.nodes.get(node_id)
            return None if runtime is None else runtime.definition.alternative_group

    def candidate_selection(self, group: str | None) -> dict[str, Any] | None:
        """The current winner record for one alternative group, read natively."""

        if not group:
            return None
        with self._lock:
            selection = self._state.candidate_selections.get(group)
            return deepcopy(selection) if selection else None

    def candidate_selection_for(self, node_id: str) -> dict[str, Any] | None:
        """The selection governing one node, or ``None`` when it has no group."""

        return self.candidate_selection(self.node_group(node_id))

    def application_started_members(self, group: str | None) -> tuple[str, ...]:
        """Group members whose application has begun; the winner is frozen then."""

        if not group:
            return ()
        with self._lock:
            members = sorted(node.node_id for node in self.current_plan.nodes
                             if node.alternative_group == group)
            return tuple(member for member in members
                         if (self._state.patch_records.get(f"patch-{member}", {}).get("state")
                             in {"applying", "applied", "uncertain"}))

    def builder_may_promote(self, node_id: str) -> bool:
        with self._lock:
            if node_id in self.replaced_builder_ids():
                return False
            node = self._node(node_id).definition
            if any(record["node_id"] == node_id for record in self._state.candidate_quarantines.values()):
                return False
            return node.alternative_group is None or self._state.candidate_selections.get(
                node.alternative_group, {}).get("selected_node_id") == node_id

    def builder_recoveries(self) -> dict[str, dict[str, Any]]:
        from minicode.builder_recovery import records
        with self._lock:
            return records(self._state.events)

    def builder_lineage(self, node_id: str) -> tuple[str, int]:
        from minicode.builder_recovery import lineage
        return lineage(self.builder_recoveries(), node_id)

    def replaced_builder_ids(self) -> frozenset[str]:
        return frozenset(value["replaces_task_id"] for value in self.builder_recoveries().values())

    def discarded_builder_ids(self) -> frozenset[str]:
        with self._lock:
            pending = {record["node_id"] for record in self.pending_candidate_quarantines().values()}
            discarded = set()
            for node_id, runtime in self._state.nodes.items():
                group = runtime.definition.alternative_group
                selection = self._state.candidate_selections.get(group)
                if selection is None or selection["selected_node_id"] == node_id or node_id in pending:
                    continue
                # CANCELED is only an intent until the real Worker exits. Queued
                # alternatives with no attempt need no execution settlement.
                if runtime.attempts and runtime.attempts[-1].attempt_id not in self._state.worker_completions:
                    continue
                record = self._state.patch_records.get(f"patch-{node_id}", {})
                if record.get("state") in {"applying", "applied", "uncertain"}:
                    continue
                discarded.add(node_id)
            return frozenset(discarded)

    def select_builder_candidate(self, *, node_id: str, parent_attempt_id: str,
                                 reason: str, operation_id: str) -> dict[str, Any]:
        with self._lock:
            node = self._node(node_id).definition
            primary = self.current_attempt("primary")
            if (not self.candidate_first or not node.alternative_group or primary is None
                or primary.attempt_id != parent_attempt_id or self.node_state("primary") is not NodeState.RUNNING
                or self._state.canceled or self.node_cancel_epoch("primary") or not primary.lease.is_valid(self._clock.now())
                or not isinstance(reason, str) or not reason.strip() or not isinstance(operation_id, str) or not operation_id):
                raise ReducerError("selection requires a live primary, an alternative group and an explicit reason")
            for event in self._state.events:
                if event.event_type is RuntimeEventType.BUILDER_SELECTED and event.payload.get("operation_id") == operation_id:
                    if event.payload["selected_node_id"] != node_id or event.payload["reason"] != reason.strip():
                        raise ReducerError("selection operation id was reused with different parameters")
                    if self._state.candidate_selections.get(node.alternative_group) != event.payload:
                        raise ReducerError("selection operation has been superseded")
                    return deepcopy(event.payload)
            members = sorted(n.node_id for n in self.current_plan.nodes if n.alternative_group == node.alternative_group)
            if len(members) < 2 or self.node_cancel_epoch(node_id) or self.node_state(node_id) in {
                NodeState.CANCELED, NodeState.FAILED_TERMINAL, NodeState.SUPERSEDED}:
                raise ReducerError("selection requires at least two registered alternatives and a viable winner")
            discarded = set(members) - {node_id}
            if any(set(n.dependencies) & discarded for n in self.current_plan.nodes if n.node_id not in discarded):
                raise ReducerError("a discarded alternative still has a required dependency consumer")
            started = self.application_started_members(node.alternative_group)
            if started and node_id not in started:
                # The winner is frozen once any member's application begins.
                # Selecting the member that already started stays legal: it is the
                # idempotent replay the contract requires, whatever operation id
                # the caller uses, and whatever already happened to its record.
                raise ReducerError(
                    "cannot discard or replace a candidate whose application has started")
            for member in discarded:
                record = self._state.patch_records.get(f"patch-{member}", {})
                if record.get("state") in {"applying", "applied", "uncertain"}:
                    raise ReducerError("cannot discard or replace a candidate whose application has started")
            selection = {"group": node.alternative_group, "selected_node_id": node_id, "members": members,
                "discarded_node_ids": sorted(discarded), "reason": reason.strip(), "operation_id": operation_id,
                "parent_attempt_id": parent_attempt_id, "plan_version": self.current_plan.plan_version,
                "contract_revision": self.current_plan.contract_revision}
            self._state.candidate_selections[node.alternative_group] = selection
            self._emit(RuntimeEventType.BUILDER_SELECTED, selection)
            self._persist()
            return deepcopy(selection)

    def _validate_candidate_lifecycle(self) -> None:
        settled, selected, retired = {}, {}, {}
        for event in self._state.events:
            if event.event_type is RuntimeEventType.WORKER_SETTLED:
                settled[event.payload.get("attempt_id")] = event.payload
            elif event.event_type is RuntimeEventType.BUILDER_SELECTED:
                selected[event.payload.get("group")] = event.payload
            elif event.event_type is RuntimeEventType.CANDIDATE_RETIRED:
                retired[event.payload.get("attempt_id")] = event.payload
        if settled != self._state.worker_completions or selected != self._state.candidate_selections:
            raise ReducerError("candidate lifecycle maps must retain their exact native events")
        for attempt_id, record in self._state.worker_completions.items():
            runtime = self._state.nodes.get(record.get("node_id"))
            if (record.get("attempt_id") != attempt_id or record.get("run_id") != self.run_id
                or runtime is None or not any(a.attempt_id == attempt_id for a in runtime.attempts)
                or not any(e.event_type is RuntimeEventType.WORKER_SETTLED and e.payload == record for e in self._state.events)):
                raise ReducerError("worker settlement lacks its exact native event")
            if record.get("ambiguous_side_effect") and attempt_id not in self._state.candidate_quarantines:
                raise ReducerError("uncertain worker settlement is missing quarantine")
        for attempt_id, record in self._state.candidate_quarantines.items():
            completion = self._state.worker_completions.get(attempt_id)
            if not completion or not completion.get("ambiguous_side_effect") or any(record.get(k) != v for k, v in completion.items()):
                raise ReducerError("candidate quarantine does not match worker settlement")
            if record.get("status") not in {"pending", "retired"} or (record["status"] == "retired" and not any(
                e.event_type is RuntimeEventType.CANDIDATE_RETIRED and e.payload == record for e in self._state.events)):
                raise ReducerError("candidate retirement lacks native evidence")
            if attempt_id in retired and retired[attempt_id] != record:
                raise ReducerError("candidate retirement cannot be removed or downgraded")
        if set(retired) - self._state.candidate_quarantines.keys():
            raise ReducerError("native candidate retirement is missing its quarantine")
        for group, selection in self._state.candidate_selections.items():
            events = [e.payload for e in self._state.events if e.event_type is RuntimeEventType.BUILDER_SELECTED and e.payload.get("group") == group]
            members = sorted(n.node_id for n in self.current_plan.nodes if n.alternative_group == group)
            if (len(members) < 2 or not events or selection != events[-1] or selection.get("members") != members
                or selection.get("selected_node_id") not in members
                or selection.get("discarded_node_ids") != sorted(set(members) - {selection.get("selected_node_id")})
                or selection.get("plan_version") != self.current_plan.plan_version
                or selection.get("contract_revision") != self.current_plan.contract_revision):
                raise ReducerError("candidate selection does not match native registration and event")
            # A discarded member that reached applying/applied is an inconsistent
            # combination: its bytes are in the target while the group says it lost.
            winner = selection.get("selected_node_id")
            for member in selection.get("discarded_node_ids", ()):
                state = (self._state.patch_records.get(f"patch-{member}", {}) or {}).get("state")
                if state in {"applying", "applied", "uncertain"}:
                    raise ReducerError(
                        "a discarded alternative has begun applying to the target; the "
                        "selection and the application receipt disagree")
            if winner not in members:
                raise ReducerError("candidate selection names a winner outside the group")

    def record_child_outcome(self, outcome: Mapping[str, Any]) -> bool:
        """Persist one child result idempotently by its event id."""
        event_id = outcome.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ReducerError("a child outcome needs an event id")
        with self._lock:
            if event_id in self._state.child_outcomes:
                return False
            self._state.child_outcomes[event_id] = dict(outcome)
            self._emit(RuntimeEventType.CHILD_OUTCOME_RECORDED, {"event_id": event_id,
                                                                 "task_id": outcome.get("task_id")})
            self._persist()
            return True

    def child_outcomes(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._state.child_outcomes.values())

    def mailbox_sequence(self) -> int:
        """Monotonic publish counter for the durable child-result mailbox."""
        with self._lock:
            return len(self._state.child_outcomes)

    def register_dependency_wait(self, wait: Mapping[str, Any]) -> bool:
        from minicode.dependency_waits import WaitSpec
        spec = WaitSpec.from_dict(wait)
        wait_id = spec.wait_id
        with self._lock:
            existing = self._state.dependency_waits.get(wait_id)
            if existing is not None:
                # A retry uses the original timestamp/checkpoint and cannot reset
                # its timeout or replace the durable dependency set.
                semantic = lambda item: {key: value for key, value in item.items() if key != "created_at"}
                if semantic(existing) != semantic(spec.to_dict()):
                    raise ReducerError("a wait id cannot be reused with different parameters")
                return False
            self._validate_wait_targets(spec)
            self._state.dependency_waits[wait_id] = spec.to_dict()
            self._emit(RuntimeEventType.DEPENDENCY_WAIT_REGISTERED, {"wait_id": wait_id,
                                                                     "owner_node_id": wait.get("owner_node_id")})
            self._persist()
            return True

    def _validate_wait_targets(self, spec) -> None:
        if spec.timeout_policy == "return":
            def owned_observation(edge):
                agent = self._state.agent_sessions.get(edge.task_id)
                if (self.async_agents_enabled and agent is not None
                        and agent["parent_node_id"] == spec.owner_node_id and edge.predicate == "terminal_observed"):
                    return True
                node = self._state.nodes.get(edge.task_id)
                # Returning control to the creating primary does not satisfy
                # the edge, publish a patch, or release a dependent Builder.
                return (node is not None and node.definition.role == "writer"
                    and edge.task_id.startswith("builder-")
                    and self._state.delegation_parents.get(edge.task_id) == "primary")
            if spec.owner_node_id != "primary" or not all(owned_observation(edge) for edge in spec.edges):
                raise ReducerError("return-on-timeout only observes owned assistants; it cannot release code dependencies")
        current = {node.node_id: node for node in self.current_plan.nodes}
        targets = {edge.task_id for edge in spec.edges}
        missing = targets - current.keys()
        human = self._state.user_steering
        # A human amendment retains an already registered wait as audit history.
        # Its canceled old children are absent from the effective plan; this
        # does not satisfy any dependency or authorize a new wait on old work.
        historical = (human is not None and human["node_id"] == spec.owner_node_id
            and self._state.dependency_waits.get(spec.wait_id) == spec.to_dict()
            and missing <= set(human["superseded_nodes"])
            and all(node_id in self._state.nodes for node_id in missing))
        if spec.owner_node_id not in current or (missing and not historical):
            raise ReducerError("dependency waits require a known owner and registered current Plan tasks")
        if any(self._state.nodes[node_id].state is NodeState.SUPERSEDED for node_id in targets | {spec.owner_node_id}):
            raise ReducerError("dependency waits cannot reference superseded tasks")
        graph = {node_id: set(node.dependencies) for node_id, node in current.items()}
        for wait_id, other in self._state.dependency_waits.items():
            if not self._state.wait_resumes.get(wait_id):
                graph.setdefault(other["owner_node_id"], set()).update(edge["task_id"] for edge in other["edges"])
        graph.setdefault(spec.owner_node_id, set()).update(targets)
        pending, visited = list(targets), set()
        while pending:
            node_id = pending.pop()
            if node_id == spec.owner_node_id:
                raise ReducerError("dependency wait would create a self dependency or cycle")
            if node_id not in visited:
                visited.add(node_id)
                pending.extend(graph.get(node_id, ()))

    def dependency_wait(self, wait_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state.dependency_waits[wait_id])

    def dependency_waits(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._state.dependency_waits.values())

    def record_wait_resume(self, wait_id: str) -> int:
        """Count one durable release for a wait. Idempotent per wait id."""
        with self._lock:
            current = int(self._state.wait_resumes.get(wait_id, 0))
            if current >= 1:
                return current
            self._state.wait_resumes[wait_id] = current + 1
            self._emit(RuntimeEventType.DEPENDENCY_WAIT_RESUMED, {"wait_id": wait_id})
            self._persist()
            return current + 1

    def wait_resumes(self, wait_id: str) -> int:
        with self._lock:
            return int(self._state.wait_resumes.get(wait_id, 0))

    def resume_from_wait(self, *, owner_node_id: str, wait_id: str,
                         consumed_cursor: int = 0) -> tuple[bool, str]:
        """Release a suspended owner for ONE satisfied dependency wait.

        The decision is taken under the reducer's own lock against DURABLE state,
        so it is idempotent across a rebuild: the wait must be registered, it must
        not have resumed before, and the owner must currently be suspended on its
        children. A second attempt -- or an owner that never suspended -- reports
        why instead of creating a second activation.
        """
        with self._lock:
            wait = self._state.dependency_waits.get(wait_id)
            if wait is None:
                return False, "the wait is not registered durably"
            if int(self._state.wait_resumes.get(wait_id, 0)) > 0:
                return False, "this wait already resumed its owner"
            if str(wait.get("owner_node_id") or "") != owner_node_id:
                return False, "the wait belongs to a different owner"
            runtime = self._state.nodes.get(owner_node_id)
            if runtime is None:
                return False, "the owner node does not exist"
            if runtime.state is not NodeState.WAITING_CHILDREN:
                return False, f"the owner is not suspended ({runtime.state.value})"
            continuation = runtime.continuation
            if (continuation is None or continuation.kind != "dependency"
                or (continuation.dependency_wait or {}).get("wait_id") != wait_id):
                return False, "the owner is suspended on a different checkpoint"
            decision = self.dependency_wait_decision(wait_id)
            if not decision.get("resume_allowed"):
                return False, "the durable dependency facts no longer allow resume"
            runtime.state = NodeState.READY
            runtime.resume_credits += 1
            runtime.current_attempt_id = None
            runtime.result = None
            self._state.wait_resumes[wait_id] = 1
            self._emit(RuntimeEventType.PRIMARY_RESUMABLE, {
                "node_id": owner_node_id, "wait_id": wait_id,
                "consumed_cursor": int(consumed_cursor),
                "wait_status": decision.get("status", "returned"),
            })
            self._persist()
            return True, "resumed"

    def fail_dependency_wait(self, wait_id: str) -> bool:
        """Persist a terminal wait failure; never turn failed dependencies into success."""
        with self._lock:
            spec = self._state.dependency_waits.get(wait_id)
            if spec is None:
                return False
            runtime = self._state.nodes.get(spec.get("owner_node_id"))
            if runtime is None or runtime.state is not NodeState.WAITING_CHILDREN or runtime.continuation is None:
                return False
            if (runtime.continuation.dependency_wait or {}).get("wait_id") != wait_id:
                return False
            decision = self.dependency_wait_decision(wait_id)
            if decision.get("status") != "blocked":
                return False
            reason = f"dependency wait {wait_id} blocked: {decision.get('reason')}"
            runtime.state = NodeState.FAILED_TERMINAL
            if runtime.result is not None:
                runtime.result = replace(runtime.result, status=AgentResultStatus.FAILED_TERMINAL,
                    failure=ExecutionFailure(ExecutionFailureKind.VERIFICATION_FAILED, reason),
                    continuation=None, output_summary=reason)
            attempt = self.current_attempt(runtime.definition.node_id)
            if attempt is not None:
                runtime.attempts[-1] = replace(attempt, state=NodeState.FAILED_TERMINAL,
                    lease=replace(attempt.lease, lease_expiry=self._clock.now()))
            self._emit(RuntimeEventType.VERIFICATION_FAILED, {"node_id": runtime.definition.node_id,
                "wait_id": wait_id, "reason": reason, "dependency_decision": decision})
            self._persist()
            return True

    def dependency_wait_decision(self, wait_id):
        """Evaluate persisted facts under the same lock used for wake and claim."""
        from minicode.dependency_waits import DEFAULT_AGENT_WAIT_SECONDS, ResultMailbox, TaskOutcome, WaitSpec
        with self._lock:
            spec = WaitSpec.from_dict(self._state.dependency_waits[wait_id])
            human = self._state.user_steering
            if (human is not None and human["node_id"] == spec.owner_node_id
                and not self._state.canceled and not self._state.paused
                and any(r["status"] == "accepted" for r in human["inputs"].values())):
                return {"resume_allowed": True, "satisfied": False, "status": "interrupted",
                        "reason": "human_input", "cursor": self.mailbox_sequence()}
            owned_agent_wait = (spec.timeout_policy == "return" and spec.owner_node_id == "primary" and all(
                edge.task_id in self._state.agent_sessions for edge in spec.edges))
            if owned_agent_wait and spec.timeout_seconds is None:
                # Bound legacy native agent waits after restart as well. Keep
                # their immutable stored identity and original start time.
                from dataclasses import replace
                spec = replace(spec, timeout_seconds=DEFAULT_AGENT_WAIT_SECONDS)
            mailbox = ResultMailbox()
            generations = {}
            for event in self._state.events:
                if event.event_type is RuntimeEventType.CANDIDATE_LEASE_OBSERVED:
                    node_id = event.payload["owner_node_id"]
                    generations[node_id] = max(generations.get(node_id, 1), int(event.payload["generation"]))
            changed = {edge.task_id for edge in spec.edges
                       if generations.get(edge.task_id, 1) != (edge.generation or spec.generation)}
            for item in self._state.child_outcomes.values():
                if item["task_id"] not in changed:
                    mailbox.publish(TaskOutcome.from_dict(item))
            decision = mailbox.consume(spec, resume=False, now=self._clock.now(),
                # Child cancellation is interpreted by each predicate. Only
                # cancellation of the owner/run revokes the right to resume.
                cancel_epoch=spec.cancel_epoch)
            if self._state.canceled or self.node_cancel_epoch(spec.owner_node_id):
                decision.update(resume_allowed=False, status="blocked", reason="owner canceled")
                return decision
            if changed:
                if owned_agent_wait:
                    # Native owned-agent waits observe outcomes; replacing a
                    # child invalidates this wait, not the parent's activation.
                    decision.update(resume_allowed=True, satisfied=False, status="interrupted",
                                    reason="dependency generation changed", changed_task_ids=sorted(changed))
                elif spec.mode == "all" or all(edge.task_id in changed for edge in spec.edges):
                    decision.update(resume_allowed=False, status="blocked", reason="dependency generation changed")
            if (owned_agent_wait and not self._state.paused
                    and decision.get("status") == "waiting"
                    and all(edge.predicate == "terminal_observed" for edge in spec.edges)):
                questions = [record["message_id"] for record in self._state.agent_messages.values()
                    if record["state"] == "queued" and record["kind"] == "question"
                    and record["recipient_id"] == spec.owner_node_id
                    and self._state.delegation_parents.get(record["sender_id"]) == spec.owner_node_id]
                if questions:
                    # A pending question authorizes a parent activation to reply,
                    # not completion of its original wait or any write scope.
                    # Recheck this durable fact at claim as well as wake.
                    decision.update(resume_allowed=True, satisfied=False, status="interrupted",
                                    reason="child_question", message_ids=questions)
            return decision

    def publication_guard(self):
        """Serialize short candidate publications with cancel, plan and attempt changes.

        Acquire the candidate mutation guard first; never hold this across model or check requests.
        """
        return self._lock

    def record_candidate_lease(self, lease, *, released=False):
        with self._lock:
            self._emit(RuntimeEventType.CANDIDATE_LEASE_OBSERVED,
                {**lease.to_dict(), "released": released})
            self._persist()

    def record_patch_record(self, patch_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a patch acceptance/application record by patch id."""
        if not isinstance(patch_id, str) or not patch_id:
            raise ReducerError("a patch record needs a patch id")
        with self._lock:
            self._state.patch_records[patch_id] = dict(record)
            self._emit(RuntimeEventType.PATCH_RECORDED, {"patch_id": patch_id,
                                                         "state": record.get("state")})
            self._persist()
            return dict(self._state.patch_records[patch_id])

    def patch_records(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._state.patch_records.items()}

    def dependency_predicates_for(self, node_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._state.dependency_predicates.get(node_id, {}))

    def _node(self, node_id: str) -> NodeRuntime:
        try:
            return self._state.nodes[node_id]
        except KeyError as error:
            raise ReducerError(f"unknown node: {node_id}") from error

    def _emit(
        self,
        event_type: RuntimeEventType,
        payload: Mapping[str, Any],
        *,
        command_id: str | None = None,
    ) -> RuntimeEvent:
        event = RuntimeEvent(
            event_id=new_id("event"),
            run_id=self.run_id,
            sequence=len(self._state.events) + 1,
            event_type=event_type,
            payload=dict(payload),
            command_id=command_id,
            created_at=self._clock.now(),
        )
        self._state.events.append(event)
        return event

    def _reject(
        self,
        command: RuntimeCommand,
        node_id: str,
        reason: str,
    ) -> list[RuntimeEvent]:
        runtime = self._state.nodes.get(node_id)
        if runtime is not None:
            runtime.rejection_reasons.append(reason)
        return [
            self._emit(
                RuntimeEventType.RESULT_REJECTED,
                {"node_id": node_id, "reason": reason},
                command_id=command.command_id,
            )
        ]

    def _delegation_parent_terminated(self, node_id: str) -> bool:
        parent_id = self._state.delegation_parents.get(node_id)
        return parent_id is not None and self._node(parent_id).state in {
            NodeState.FAILED_TERMINAL, NodeState.CANCELED, NodeState.SUPERSEDED}

    def _claim_node(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if self._delegation_parent_terminated(node_id):
            return self._reject(command, node_id, "delegation parent terminated before child claim")
        if not self.builder_may_execute(node_id) or any(
            record["node_id"] == node_id for record in self._state.candidate_quarantines.values()):
            return self._reject(command, node_id, "candidate is discarded or quarantined")
        if self._state.canceled:
            return self._reject(command, node_id, "run is canceled")
        if runtime.state is not NodeState.READY:
            return self._reject(command, node_id, f"node is not READY: {runtime.state.value}")
        if int(command.payload.get("plan_version", 0)) != self._state.current_plan_version:
            return self._reject(command, node_id, "claim references a non-current plan")
        predicates = self.dependency_predicates_for(node_id)
        if any(not self.dependency_edge_satisfied(task_id, predicate) for task_id, predicate in predicates.items()):
            return self._reject(command, node_id, "a declared dependency changed before claim")
        if runtime.continuation is not None and runtime.continuation.kind == "dependency":
            wait_id = runtime.continuation.dependency_wait["wait_id"]
            if not self.dependency_wait_decision(wait_id).get("resume_allowed"):
                return self._reject(command, node_id, "dependency wait changed between wake and claim")

        attempt_number = len(runtime.attempts) + 1
        budgeted_attempt_number = attempt_number - runtime.resume_credits
        if budgeted_attempt_number > runtime.definition.max_attempts:
            runtime.state = NodeState.FAILED_TERMINAL
            return self._reject(command, node_id, "attempt budget exhausted")
        lease_seconds = max(0.001, float(command.payload.get("lease_seconds", 30.0)))
        owner = str(command.payload.get("lease_owner") or "local-worker")
        token = self._state.next_fencing_token
        self._state.next_fencing_token += 1
        attempt = NodeAttempt(
            attempt_id=new_id("attempt"),
            run_id=self.run_id,
            plan_version=self._state.current_plan_version,
            node_id=node_id,
            attempt_number=attempt_number,
            lease=Lease(owner, self._clock.now() + lease_seconds, FencingToken(token)),
            state=NodeState.CLAIMED,
            idempotency_key=str(command.payload.get("idempotency_key") or new_id("idempotency")),
        )
        runtime.attempts.append(attempt)
        runtime.current_attempt_id = attempt.attempt_id
        runtime.state = NodeState.CLAIMED
        return [
            self._emit(
                RuntimeEventType.NODE_CLAIMED,
                {"node_id": node_id, "attempt": attempt.to_dict()},
                command_id=command.command_id,
            )
        ]

    def _start_attempt(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if self._delegation_parent_terminated(node_id):
            return self._reject(command, node_id, "delegation parent terminated before child start")
        attempt = self.current_attempt(node_id)
        if attempt is None or attempt.attempt_id != command.payload.get("attempt_id"):
            return self._reject(command, node_id, "attempt is not current")
        if runtime.state is not NodeState.CLAIMED:
            return self._reject(command, node_id, "node is not CLAIMED")
        if not attempt.lease.is_valid(self._clock.now()):
            return self._reject(command, node_id, "lease expired before worker start")
        started = replace(attempt, state=NodeState.RUNNING, started_at=self._clock.now())
        runtime.attempts[-1] = started
        runtime.state = NodeState.RUNNING
        return [
            self._emit(
                RuntimeEventType.ATTEMPT_STARTED,
                {"node_id": node_id, "attempt_id": attempt.attempt_id},
                command_id=command.command_id,
            )
        ]

    def _renew_lease(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        """Extend only a live, currently owned attempt without changing its fence."""
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        attempt = self.current_attempt(node_id)
        if self._state.canceled:
            return self._reject(command, node_id, "run is canceled")
        if attempt is None or attempt.attempt_id != command.payload.get("attempt_id"):
            return self._reject(command, node_id, "attempt is not current")
        if attempt.plan_version != self._state.current_plan_version:
            return self._reject(command, node_id, "renewal references a non-current plan")
        if runtime.state not in {NodeState.CLAIMED, NodeState.RUNNING, NodeState.VERIFYING}:
            return self._reject(command, node_id, "attempt is not active")
        if command.payload.get("lease_owner") != attempt.lease.lease_owner:
            return self._reject(command, node_id, "lease owner does not match")
        if int(command.payload.get("fencing_token", -1)) != attempt.lease.fencing_token.value:
            return self._reject(command, node_id, "stale fencing token")
        now = self._clock.now()
        if not attempt.lease.is_valid(now):
            return self._reject(command, node_id, "expired lease cannot be renewed")
        duration = float(command.payload.get("lease_seconds", 0.0))
        if not math.isfinite(duration) or duration <= 0:
            raise ReducerError("renewal lease_seconds must be finite and positive")
        expiry = max(attempt.lease.lease_expiry, now + duration)
        runtime.attempts[-1] = replace(
            attempt, lease=replace(attempt.lease, lease_expiry=expiry),
        )
        return [self._emit(
            RuntimeEventType.LEASE_RENEWED,
            {"node_id": node_id, "attempt_id": attempt.attempt_id, "lease_expiry": expiry},
            command_id=command.command_id,
        )]

    def _submit_result(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        result = command.payload.get("result")
        if not isinstance(result, AgentResult):
            raise ReducerError("SubmitResult payload must contain AgentResult")
        result = _terminalize_unsafe_result(result)
        node_id = result.node_id
        if result.run_id != self.run_id:
            return self._reject(command, node_id, "result run_id does not match Runtime")
        if result.plan_version != self._state.current_plan_version:
            return self._reject(command, node_id, "result references a non-current plan")
        if result.contract_revision != self._state.contract_revision:
            return self._reject(command, node_id, "result references a stale TaskContract revision")
        runtime = self._node(node_id)
        attempt = self.current_attempt(node_id)
        if runtime.state is NodeState.SUPERSEDED:
            return self._reject(command, node_id, "node is superseded")
        if attempt is None or result.attempt_id != attempt.attempt_id:
            return self._reject(command, node_id, "result is not for the current attempt")
        if runtime.result is not None:
            return self._reject(command, node_id, "attempt already has an accepted result")
        submitted_token = int(command.payload.get("fencing_token", -1))
        if submitted_token != attempt.lease.fencing_token.value:
            return self._reject(command, node_id, "stale fencing token")
        if not attempt.lease.is_valid(self._clock.now()):
            return self._reject(command, node_id, "expired lease")
        if runtime.state is not NodeState.RUNNING:
            return self._reject(command, node_id, f"node is not RUNNING: {runtime.state.value}")

        process = self.required_consultation_status() if node_id == "primary" else None
        reading = self.consultation_read_status() if node_id == "primary" else None
        if reading and not reading["satisfied"] and result.status is AgentResultStatus.SUCCEEDED:
            result = replace(result, status=AgentResultStatus.FAILED_TERMINAL,
                failure=ExecutionFailure(ExecutionFailureKind.VERIFICATION_FAILED, reading["reason"]))
        if process and not process["satisfied"] and result.status is AgentResultStatus.SUCCEEDED:
            result = replace(result, status=AgentResultStatus.FAILED_TERMINAL,
                failure=ExecutionFailure(ExecutionFailureKind.VERIFICATION_FAILED, process["reason"]))

        if (self.candidate_first and runtime.definition.role == "primary"
            and result.status is AgentResultStatus.SUCCEEDED and result.session_checkpoint is None):
            result = replace(result, status=AgentResultStatus.FAILED_TERMINAL,
                failure=ExecutionFailure(ExecutionFailureKind.VERIFICATION_FAILED,
                    "candidate delivery requires a durable completed primary checkpoint"))
        human = self._state.user_steering
        if (human is not None and node_id == human["node_id"] and result.status is AgentResultStatus.SUCCEEDED
            and (not human["sealed"] or any(r["status"] in {"accepted", "applied"} for r in human["inputs"].values()))):
            result = replace(result, status=AgentResultStatus.FAILED_TERMINAL,
                failure=ExecutionFailure(ExecutionFailureKind.VERIFICATION_FAILED,
                    "public Worker finished with unresolved or unsealed human input"))

        if result.continuation is not None and result.status is not AgentResultStatus.WAITING_CHILDREN:
            return self._reject(command, node_id, "continuation is only valid on a waiting result")
        if result.status is AgentResultStatus.WAITING_CHILDREN:
            continuation = result.continuation
            recovering = False
            try:
                if result.failure is not None:
                    raise ReducerError("waiting result requires a complete checkpoint without failure")
                if continuation is None:
                    raise ReducerError("internal suspension requires an owned continuation checkpoint")
                else:
                    if continuation.kind == "dependency":
                        self._validate_dependency_continuation(node_id, continuation)
                    else:
                        self.validate_consultation(node_id, continuation.children)
                        recovering = any(child.node_id in self._state.nodes
                            and self._node(child.node_id).state is NodeState.FAILED_TERMINAL
                            for child in continuation.children)
                        if recovering:
                            recovery = self.consultation_recovery_status(node_id, attempt.attempt_id)
                            if (not recovery["allowed"] or runtime.continuation is None
                                or continuation.children != runtime.continuation.children):
                                raise ReducerError(recovery["reason"])
                    self._validate_checkpoint_record(node_id, continuation)
                    if continuation.checkpoint not in result.artifact_refs or continuation.checkpoint.size <= 0:
                        raise ReducerError("checkpoint must be a retained artifact")
                    if runtime.continuation and runtime.continuation.continuation_id == continuation.continuation_id:
                        raise ReducerError("continuation has already been consumed")
            except (ValueError, ReducerError) as error:
                return self._reject(command, node_id, str(error))
            if continuation is not None:
                if recovering:
                    # Reset only after the new candidate/checkpoint was accepted.
                    # Attempts, reports and verdict events remain in the audit log.
                    prior = {child.node_id: self._node(child.node_id).current_attempt_id for child in continuation.children}
                    self._emit(RuntimeEventType.CONSULTATION_RECOVERY_REQUESTED, {
                        "node_id": node_id, "attempt_id": attempt.attempt_id, "children": prior,
                        "continuation_id": continuation.continuation_id, "input_revision": continuation.input_revision,
                    })
                    for child in continuation.children:
                        registered = self._node(child.node_id)
                        registered.state = NodeState.READY
                        registered.current_attempt_id = None
                        registered.result = None
                        registered.resume_credits += 1
                added = tuple(n for n in continuation.children if n.node_id not in self._state.nodes)
                plan = self._state.plans[self._state.current_plan_version]
                self._state.plans[plan.plan_version] = replace(plan, nodes=(*plan.nodes, *added))
                for child in added:
                    self._state.nodes[child.node_id] = NodeRuntime(child, NodeState.READY)
                runtime.continuation = continuation
                runtime.continuation_count += 1

        if runtime.definition.role == "primary" and result.status in {AgentResultStatus.SUCCEEDED, AgentResultStatus.WAITING_CHILDREN}:
            self._state.pending_repair_id = None
        self._record_result_usage_unlocked(result)
        if (result.status is AgentResultStatus.SUCCEEDED and self.agent_messages_enabled
            and any(r["recipient_id"] == node_id and r["state"] == "queued" for r in self._state.agent_messages.values())):
            result = replace(result, status=AgentResultStatus.FAILED_TERMINAL,
                failure=ExecutionFailure(ExecutionFailureKind.PROTOCOL_ERROR, "success cannot discard queued agent messages"),
                output_summary="agent finished without consuming an accepted message")
        runtime.result = result
        if result.status in {AgentResultStatus.FAILED_TERMINAL, AgentResultStatus.CANCELED} or (
            result.status is AgentResultStatus.FAILED_RETRYABLE
            and len(runtime.attempts) - runtime.resume_credits >= runtime.definition.max_attempts
        ):
            self._reject_pending_agent_messages({node_id}, result.output_summary or result.status.value)
        runtime.workspace_committed = not (
            runtime.definition.is_writer and result.status is AgentResultStatus.SUCCEEDED
        )
        if self.candidate_first and runtime.definition.role == "primary":
            self._state.integration_status = "prepared" if result.status is AgentResultStatus.SUCCEEDED else "idle"
        terminal_map = {
            AgentResultStatus.FAILED_RETRYABLE: NodeState.FAILED_RETRYABLE,
            AgentResultStatus.FAILED_TERMINAL: NodeState.FAILED_TERMINAL,
            AgentResultStatus.WAITING_APPROVAL: NodeState.WAITING_APPROVAL,
            AgentResultStatus.WAITING_INPUT: NodeState.WAITING_INPUT,
            AgentResultStatus.WAITING_CHILDREN: NodeState.WAITING_CHILDREN,
            AgentResultStatus.CANCELED: NodeState.CANCELED,
        }
        runtime.state = terminal_map.get(result.status, NodeState.VERIFYING)
        released_lease = attempt.lease
        if runtime.state in {
            NodeState.FAILED_RETRYABLE,
            NodeState.FAILED_TERMINAL,
            NodeState.WAITING_APPROVAL,
            NodeState.WAITING_INPUT,
            NodeState.WAITING_CHILDREN,
            NodeState.CANCELED,
        }:
            released_lease = replace(attempt.lease, lease_expiry=self._clock.now())
        runtime.attempts[-1] = replace(
            attempt,
            state=runtime.state,
            lease=released_lease,
            completed_at=self._clock.now() if runtime.state is not NodeState.VERIFYING else None,
        )
        events = [
            self._emit(
                RuntimeEventType.RESULT_ACCEPTED,
                {
                    "node_id": node_id, "attempt_id": attempt.attempt_id,
                    "status": result.status.value,
                    "reason": result.failure.message if result.failure else "",
                    **({"consultation_summary_ref": result.consultation_summary_ref.to_dict()}
                       if result.consultation_summary_ref is not None else {}),
                },
                command_id=command.command_id,
            )
        ]
        if runtime.state is NodeState.WAITING_CHILDREN:
            events.append(self._emit(RuntimeEventType.PRIMARY_SUSPENDED, {
                "node_id": node_id, "attempt_id": attempt.attempt_id,
                "continuation_id": runtime.continuation.continuation_id,
                "input_revision": runtime.continuation.input_revision,
                "children": [child.node_id for child in runtime.continuation.children],
                "definitions": [child.to_dict() for child in runtime.continuation.children],
            }))
        return events

    def _wake_continuations(self) -> list[RuntimeEvent]:
        events = self._wake_agent_questions()
        if self._state.canceled or self._state.paused:
            return events
        terminal = {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.CANCELED}
        for runtime in self._state.nodes.values():
            continuation = runtime.continuation
            if runtime.state is not NodeState.WAITING_CHILDREN or continuation is None or continuation.kind != "consultation":
                continue
            if all(self._node(child.node_id).state in terminal for child in continuation.children):
                runtime.state = NodeState.READY
                runtime.resume_credits += 1
                runtime.current_attempt_id = None
                runtime.result = None
                events.append(self._emit(RuntimeEventType.PRIMARY_RESUMABLE, {
                    "node_id": runtime.definition.node_id, "continuation_id": continuation.continuation_id,
                }))
        return events

    def _wake_agent_questions(self) -> list[RuntimeEvent]:
        if self._state.canceled or self._state.paused:
            return []
        events = []
        for record in self._state.agent_messages.values():
            if record["state"] != "queued" or record["kind"] != "question":
                continue
            target = self._node(record["recipient_id"])
            if target.state is not NodeState.WAITING_CHILDREN or target.continuation is None:
                continue
            if target.continuation.kind == "dependency":
                wait_id = target.continuation.dependency_wait["wait_id"]
                if self.dependency_wait_decision(wait_id).get("reason") != "child_question":
                    # Messages cannot bypass artifact or patch acceptance waits.
                    continue
            # Includes a question committed just before the parent's wait checkpoint.
            # Never consume or satisfy the original dependency predicate here.
            target.state = NodeState.READY
            target.resume_credits += 1
            target.current_attempt_id = None
            target.result = None
            events.append(self._emit(RuntimeEventType.PRIMARY_RESUMABLE, {
                "node_id": target.definition.node_id, "reason": "child_question",
                "message_id": record["message_id"], "original_wait_preserved": True}))
        return events

    def commit_workspace(
        self,
        node_id: str,
        *,
        attempt_id: str,
        fencing_token: int,
        commit: Callable[[], tuple[str, ...]],
    ) -> tuple[str, ...]:
        """Serialize a Writer's filesystem commit with cancel and fencing decisions.

        The Worker prepares changes outside the canonical workspace.  Holding the
        reducer lock while revalidating and applying the commit gives cancellation,
        replanning, and stale attempts a deterministic order relative to the write.
        """
        with self._lock:
            if self.candidate_first:
                raise ReducerError("candidate-first writers cannot use eager workspace commit")
            runtime = self._node(node_id)
            attempt = self.current_attempt(node_id)
            if self._state.canceled:
                raise ReducerError("run was canceled before workspace commit")
            if runtime.state is not NodeState.VERIFYING:
                raise ReducerError("node is not VERIFYING")
            if runtime.result is None or runtime.result.status is not AgentResultStatus.SUCCEEDED:
                raise ReducerError("node has no successful accepted result")
            if attempt is None or attempt.attempt_id != attempt_id:
                raise ReducerError("workspace commit is not for the current attempt")
            if runtime.result.attempt_id != attempt_id:
                raise ReducerError("workspace commit does not match accepted result")
            if attempt.lease.fencing_token.value != fencing_token:
                raise ReducerError("stale fencing token before workspace commit")
            if not attempt.lease.is_valid(self._clock.now()):
                raise ReducerError("lease expired before workspace commit")
            if runtime.workspace_committed:
                return runtime.result.changed_resources

            changed = tuple(commit())
            runtime.workspace_committed = True
            self._emit(
                RuntimeEventType.WORKSPACE_COMMITTED,
                {"node_id": node_id, "attempt_id": attempt_id, "changed_resources": list(changed)},
            )
            self._persist()
            return changed

    def commit_candidate(self, result: AgentResult, *, revision: str,
                         commit: Callable[[], tuple[str, ...]]) -> tuple[str, ...]:
        """Coordinator-owned commit, under the run's WorkspaceWriteLease.

        Worker attempt leases have already ended. The accepted result identity,
        terminal plan and durable applying marker fence this later delivery step.
        An interrupted applying marker is never automatically replayed.
        """
        with self._lock:
            primary = self._node("primary")
            if not self.candidate_first or self.integration_status != "prepared" or primary.workspace_committed:
                raise ReducerError("candidate is not awaiting its first controlled integration")
            if self._state.canceled or self._state.paused:
                raise ReducerError("canceled or paused runs cannot integrate a candidate")
            if primary.result != result or self.completion_blockers():
                raise ReducerError("candidate integration requires the current result and all nodes succeeded")
            records = self._state.final_verification_records
            required = acceptance_criterion_ids(self.contract.acceptance_criteria) if self.contract else ()
            covered = {c for record in records if record.passed and record.scope == "final" for c in record.covered_criteria_ids}
            if (not re.fullmatch(r"[0-9a-f]{64}", revision) or not records or any(not r.passed for r in records)
                or set(required) - covered
                or not any(r.deterministic and r.scope == "final" and any(
                    e.base_revision == revision and e.evidence_type in {"command-exit", "workspace-snapshot"}
                    for e in r.evidence_refs) for r in records)):
                raise ReducerError("candidate integration requires complete successful verification for this revision")
            if self.contract is not None:
                budget, usage = self.contract.budget, self.usage_summary()
                if ((budget.max_tokens is not None and (not usage["tokens_known"] or self._state.tokens_used > budget.max_tokens))
                    or (budget.max_cost is not None and (not usage["cost_known"] or self._state.cost_used > budget.max_cost))
                    or (budget.deadline is not None and self._clock.now() >= budget.deadline)):
                    raise ReducerError("run budget or deadline does not allow candidate integration")
            self._state.integration_status = "applying"
            self._state.integration_revision = revision
            self._emit(RuntimeEventType.CANDIDATE_INTEGRATION_STARTED, {
                "node_id": "primary", "attempt_id": result.attempt_id, "candidate_revision": revision,
            })
            self._persist()
            try:
                changed = tuple(commit())
            except Exception as error:
                self._state.integration_status = "failed"
                self._state.integration_error = str(error)
                self._emit(RuntimeEventType.CANDIDATE_INTEGRATION_FAILED, {"reason": str(error)})
                self._persist()
                raise
            primary.workspace_committed = True
            self._state.integration_status = "committed"
            self._emit(RuntimeEventType.WORKSPACE_COMMITTED, {
                "node_id": "primary", "attempt_id": result.attempt_id,
                "candidate_revision": revision, "changed_resources": list(changed),
            })
            self._persist()
            return changed

    def _begin_verification(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if runtime.state is not NodeState.VERIFYING:
            return self._reject(command, node_id, "node is not VERIFYING")
        if runtime.definition.is_writer and not runtime.workspace_committed and not (
            self.candidate_first and (self.integration_status == "prepared" or
                (node_id.startswith("builder-") and runtime.result is not None and runtime.result.session_checkpoint is not None
                 and runtime.result.session_checkpoint.media_type == "application/vnd.minicode.builder-candidate+json"))
            and runtime.result is not None and runtime.result.session_checkpoint is not None
        ):
            return self._reject(command, node_id, "writer workspace commit is not confirmed")
        return [
            self._emit(
                RuntimeEventType.VERIFICATION_STARTED,
                {"node_id": node_id},
                command_id=command.command_id,
            )
        ]

    def _accept_verification(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if runtime.state is not NodeState.VERIFYING:
            return self._reject(command, node_id, "node is not VERIFYING")
        if node_id == "primary" and self.dynamic_review_enabled and not self.current_review_assessment:
            return self._reject(command, node_id, "primary needs an observed candidate review assessment")
        runtime.state = NodeState.SUCCEEDED
        if runtime.attempts:
            runtime.attempts[-1] = replace(
                runtime.attempts[-1],
                state=NodeState.SUCCEEDED,
                lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                completed_at=self._clock.now(),
            )
        return [
            self._emit(
                RuntimeEventType.VERIFICATION_ACCEPTED,
                {"node_id": node_id, "attempt_id": runtime.current_attempt_id, "record": command.payload.get("record")},
                command_id=command.command_id,
            )
        ]

    def _fail_verification(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._node(node_id)
        if runtime.state is not NodeState.VERIFYING:
            return self._reject(command, node_id, "node is not VERIFYING")
        can_retry = (
            len(runtime.attempts) - runtime.resume_credits
            < runtime.definition.max_attempts
        )
        if self.candidate_first and runtime.definition.role == "primary" and can_retry:
            self._state.integration_status = "idle"
        runtime.state = NodeState.FAILED_RETRYABLE if can_retry else NodeState.FAILED_TERMINAL
        runtime.result = None if can_retry else runtime.result
        if runtime.attempts:
            runtime.attempts[-1] = replace(
                runtime.attempts[-1],
                state=runtime.state,
                lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                completed_at=self._clock.now(),
            )
        return [
            self._emit(
                RuntimeEventType.VERIFICATION_FAILED,
                {
                    "node_id": node_id,
                    "retryable": can_retry,
                    "attempt_id": runtime.current_attempt_id,
                    "record": command.payload.get("record"),
                    "reason": command.payload.get("reason", "verification failed"),
                },
                command_id=command.command_id,
            )
        ]

    def prepare_retry(self, node_id: str) -> bool:
        """Move a bounded retryable node back to READY through the reducer lock."""
        with self._lock:
            runtime = self._node(node_id)
            if runtime.state is not NodeState.FAILED_RETRYABLE:
                return False
            if len(runtime.attempts) - runtime.resume_credits >= runtime.definition.max_attempts:
                runtime.state = NodeState.FAILED_TERMINAL
                self._wake_continuations()
                self._persist()
                return False
            runtime.state = NodeState.READY
            runtime.current_attempt_id = None
            runtime.result = None
            runtime.workspace_committed = True
            if self.candidate_first and runtime.definition.role == "primary":
                self._state.integration_status = "idle"
            self._persist()
            return True

    def expire_attempt(self, node_id: str, *, allow_retry: bool = True) -> bool:
        """Fence an expired claim and make a bounded retry available."""
        with self._lock:
            runtime = self._node(node_id)
            attempt = self.current_attempt(node_id)
            if attempt is None or attempt.lease.is_valid(self._clock.now()):
                return False
            can_retry = allow_retry and (
                len(runtime.attempts) - runtime.resume_credits
                < runtime.definition.max_attempts
            )
            if not allow_retry:
                runtime.rejection_reasons.append(
                    "effectful attempt expired with uncertain side effects; replay forbidden"
                )
            runtime.state = NodeState.FAILED_RETRYABLE if can_retry else NodeState.FAILED_TERMINAL
            runtime.attempts[-1] = replace(
                attempt,
                state=runtime.state,
                completed_at=self._clock.now(),
            )
            runtime.current_attempt_id = None
            runtime.result = None
            runtime.workspace_committed = True
            if can_retry:
                runtime.state = NodeState.READY
            self._wake_continuations()
            self._persist()
            return True

    def _cancel_run(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        self._reject_pending_agent_messages(set(self._state.nodes), "run canceled")
        self._state.canceled = True
        for runtime in self._state.nodes.values():
            if runtime.state not in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL, NodeState.SUPERSEDED}:
                runtime.state = NodeState.CANCELED
                if runtime.attempts and runtime.current_attempt_id:
                    runtime.attempts[-1] = replace(
                        runtime.attempts[-1],
                        state=NodeState.CANCELED,
                        lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                        completed_at=self._clock.now(),
                    )
        return [
            self._emit(
                RuntimeEventType.RUN_CANCELED,
                {"reason": command.payload.get("reason", "canceled")},
                command_id=command.command_id,
            )
        ]

    def _cancel_task_command(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        """Protocol entry for a task/subtree cancellation, never a run cancel."""
        return self.cancel_task(
            str(command.payload.get("node_id", "")),
            reason=str(command.payload.get("reason", "canceled by the Runtime")),
            subtree=bool(command.payload.get("subtree", True)),
            operation_id=command.payload.get("operation_id"),
            dependency_successors=bool(command.payload.get("dependency_successors", False)),
        )

    def _resume_run(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        if self._state.canceled:
            raise ReducerError("a canceled run cannot be resumed")
        self._state.paused = False
        return [self._emit(RuntimeEventType.RUN_RESUMED, {}, command_id=command.command_id)]

    def task_cancel_epoch(self) -> int:
        """The run-scoped count of distinct task-cancel operations.

        Kept for compatibility only. It is NOT the epoch a task or a wait should
        re-check: cancelling an unrelated task must not invalidate a wait that
        never declared it. Use ``node_cancel_epoch`` / ``wait_cancel_epoch``.
        """
        with self._lock:
            return len(self._state.cancel_operations)

    def node_cancel_epoch(self, node_id: str) -> int:
        """How many distinct cancellation operations covered this node."""
        with self._lock:
            return int(self._state.node_cancel_epoch.get(node_id, 0))

    def wait_cancel_epoch(self, task_ids: Sequence[str]) -> int:
        """The epoch a wait must re-check: only the tasks it declared.

        Cancelling B therefore leaves a wait that declares only A untouched, and
        that wait can still be released by A's result.
        """
        with self._lock:
            return max((int(self._state.node_cancel_epoch.get(task_id, 0))
                        for task_id in task_ids), default=0)

    def candidate_cancel_epoch(self, candidate_id: str) -> int:
        """The cancellation epoch of the node that owns a candidate."""
        node_id = "primary" if candidate_id == "primary" else candidate_id.removeprefix("candidate-")
        return self.node_cancel_epoch(node_id)

    def candidate_canceled(self, candidate_id: str) -> bool:
        """Whether the node owning this candidate is canceled.

        An unknown candidate is not "canceled": it simply has no live owner, and
        that is reported by the owner check instead of pretending a cancellation.
        """
        node_id = "primary" if candidate_id == "primary" else candidate_id.removeprefix("candidate-")
        with self._lock:
            runtime = self._state.nodes.get(node_id)
            return runtime is not None and runtime.state is NodeState.CANCELED

    def creation_children(self, node_id: str) -> tuple[str, ...]:
        """Direct children in the CREATION relation, not the dependency DAG."""
        with self._lock:
            return tuple(sorted(child for child, parent in self._state.delegation_parents.items()
                                if parent == node_id))

    def creation_descendants(self, node_id: str) -> tuple[str, ...]:
        """The whole creation subtree, in a stable order."""
        with self._lock:
            found: list[str] = []
            pending = list(self.creation_children(node_id))
            while pending:
                current = pending.pop(0)
                if current in found:
                    continue
                found.append(current)
                pending.extend(self.creation_children(current))
            return tuple(found)

    def creation_ancestors(self, node_id: str) -> tuple[str, ...]:
        with self._lock:
            found: list[str] = []
            current = self._state.delegation_parents.get(node_id)
            while current is not None and current not in found:
                found.append(current)
                current = self._state.delegation_parents.get(current)
            return tuple(found)

    def cancel_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            recorded = self._state.cancel_operations.get(operation_id)
            return dict(recorded) if recorded else None

    def cancel_task(self, task_id: str, *, reason: str = "canceled by the Runtime",
                    subtree: bool = True, operation_id: str | None = None,
                    dependency_successors: bool = False) -> list[RuntimeEvent]:
        """Cancel one task, its CREATION subtree, and optionally its dependents.

        ``subtree`` follows the creation relation. ``dependency_successors`` is a
        separate, explicit request: a node that merely depends on the canceled
        task is not part of its subtree, and an optional dependency must be able
        to choose a different policy. The same ``operation_id`` is idempotent and
        never advances an epoch twice.
        """
        with self._lock:
            if not isinstance(task_id, str) or task_id not in self._state.nodes:
                raise ReducerError("cancel_task requires a known node id")
            if operation_id is not None:
                recorded = self._state.cancel_operations.get(operation_id)
                if recorded is not None:
                    if (recorded.get("task_id") != task_id or bool(recorded.get("subtree")) != bool(subtree)
                        or bool(recorded.get("dependency_successors", False)) != bool(dependency_successors)):
                        raise ReducerError("a cancel operation id cannot be reused for another target")
                    return [self._emit(RuntimeEventType.TASK_CANCEL_INTENT, {
                        "node_id": task_id, "subtree": bool(subtree), "reason": recorded.get("reason", reason),
                        "targets": list(recorded.get("targets", [])),
                        "cancel_epoch": self.node_cancel_epoch(task_id),
                        "operation_id": operation_id, "replayed": True,
                    })]
            targets = [task_id]
            if subtree:
                targets.extend(self.creation_descendants(task_id))
            if dependency_successors:
                pending = [task_id]
                while pending:
                    current = pending.pop()
                    for node in self.current_plan.nodes:
                        if current in node.dependencies and node.node_id not in targets:
                            targets.append(node.node_id)
                            pending.append(node.node_id)
            targets = sorted(set(targets))
            # Which targets this command really cancels. A node that already SETTLED
            # successfully (or terminally failed) is not canceled by a later command,
            # and its cancellation epoch must not advance either: the epoch is the
            # native record of "this task really was stopped", and advancing it would
            # erase the outcome and the usage a Worker really produced, and would
            # revoke dependency pins and wait edges for a stop that never happened.
            # An already-CANCELED node stays cancellable: a distinct operation on it
            # still advances the epoch, which is what makes re-cancellation observable.
            canceled_targets = [node_id for node_id in targets
                                if self._state.nodes[node_id].state not in {
                                    NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL,
                                    NodeState.SUPERSEDED}]
            self._reject_pending_agent_messages(set(canceled_targets), reason)
            for node_id in canceled_targets:
                self._state.node_cancel_epoch[node_id] = self.node_cancel_epoch(node_id) + 1
            if operation_id is not None:
                self._state.cancel_operations[operation_id] = {
                    "operation_id": operation_id, "task_id": task_id, "subtree": bool(subtree),
                    "reason": reason, "targets": list(targets),
                    "canceled_targets": list(canceled_targets),
                    "dependency_successors": bool(dependency_successors),
                }
            events = [self._emit(RuntimeEventType.TASK_CANCEL_INTENT, {
                "node_id": task_id, "subtree": bool(subtree), "reason": reason,
                "targets": list(targets), "canceled_targets": list(canceled_targets),
                "cancel_epoch": self.node_cancel_epoch(task_id),
                "operation_id": operation_id,
                "dependency_successors": bool(dependency_successors),
            })]
            for node_id in canceled_targets:
                runtime = self._state.nodes[node_id]
                if runtime.state is NodeState.CANCELED:
                    # Already canceled by an earlier operation: this distinct operation
                    # still advanced the epoch above, but there is no second transition
                    # to report. Re-emitting NodeCanceled would claim a state change
                    # that did not happen and would double-count the cancellation.
                    continue
                runtime.state = NodeState.CANCELED
                if runtime.attempts and runtime.current_attempt_id:
                    runtime.attempts[-1] = replace(
                        runtime.attempts[-1], state=NodeState.CANCELED,
                        lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                        completed_at=self._clock.now(),
                    )
                events.append(self._emit(RuntimeEventType.NODE_CANCELED, {
                    "node_id": node_id, "reason": reason, "root_task_id": task_id,
                    "subtree": bool(subtree), "operation_id": operation_id,
                }))
            self._persist()
            return events

    def _amend_task(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        amendment = command.payload.get("amendment")
        if not isinstance(amendment, TaskAmendment):
            raise ReducerError("AmendTask requires TaskAmendment")
        plan = command.payload.get("plan")
        contract = command.payload.get("contract")
        identity = command.payload.get("identity")
        if not isinstance(plan, PlanVersion) or not isinstance(contract, TaskContract):
            raise ReducerError("AmendTask requires complete TaskContract and PlanVersion")
        if not isinstance(identity, RuntimeRunIdentity):
            raise ReducerError("AmendTask requires the immutable RunIdentity")
        return [
            self._install_plan_unlocked(
                plan,
                amendment,
                contract=contract,
                identity=identity,
                command_id=command.command_id,
            )
        ]

    def _approval(self, command: RuntimeCommand) -> list[RuntimeEvent]:
        approval_id = str(command.payload.get("approval_id", ""))
        approved = command.command_type is RuntimeCommandType.APPROVE_TOOL_CALL
        if approved and approval_id in self._state.consumed_approvals:
            raise ReducerError("approval already consumed")
        if approved:
            self._state.consumed_approvals.add(approval_id)
        node_id = str(command.payload.get("node_id", ""))
        runtime = self._state.nodes.get(node_id)
        if runtime is not None and runtime.state is NodeState.WAITING_APPROVAL:
            if approved:
                # A suspended process cannot be assumed to still exist after
                # restart.  Resume by creating a fresh fenced Attempt for the
                # same logical Node; its persisted idempotency key is reused by
                # the Scheduler for any replay-safe operation.
                runtime.state = NodeState.READY
                runtime.current_attempt_id = None
                runtime.result = None
                runtime.resume_credits += 1
            else:
                runtime.state = NodeState.FAILED_TERMINAL
                if runtime.attempts:
                    runtime.attempts[-1] = replace(
                        runtime.attempts[-1],
                        state=NodeState.FAILED_TERMINAL,
                        lease=replace(runtime.attempts[-1].lease, lease_expiry=self._clock.now()),
                        completed_at=self._clock.now(),
                    )
        return [
            self._emit(
                RuntimeEventType.TOOL_CALL_APPROVED if approved else RuntimeEventType.TOOL_CALL_REJECTED,
                dict(command.payload),
                command_id=command.command_id,
            )
        ]

    def dependency_pin(self, task_id: str, predicate: str) -> dict[str, Any]:
        """The exact evidence that releases one declared dependency edge, or why not.

        ``dependency_edge_satisfied`` answers yes/no. A consumer that will later be
        admitted or applied needs the *evidence* itself, because "the dependency was
        satisfied at some point" is not the same claim as "the dependency is still
        the one I built against". The returned ``released``/``reason`` pair is the
        single source both the release check and the later pin verification use, so
        they cannot disagree.

        Legacy snapshots that predate an evidence field return ``released=False``
        with a reason naming the missing field. Missing evidence is never upgraded
        into trusted current evidence.
        """

        with self._lock:
            runtime = self._state.nodes.get(task_id)
            if runtime is None:
                return {"task_id": task_id, "predicate": predicate, "released": False,
                        "reason": "no such task in the current plan"}
            cancel_epoch = int(self._state.node_cancel_epoch.get(task_id, 0))
            terminal_state = runtime.state.value
            if predicate == "terminal_observed":
                if runtime.state not in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL,
                                         NodeState.FAILED_RETRYABLE, NodeState.CANCELED}:
                    return {"task_id": task_id, "predicate": predicate, "released": False,
                            "reason": f"the bound version has not reached a terminal state "
                                      f"({runtime.state.value})"}
            else:
                if runtime.state is not NodeState.SUCCEEDED:
                    return {"task_id": task_id, "predicate": predicate, "released": False,
                            "reason": f"the producing task is {runtime.state.value}"}
                if cancel_epoch:
                    return {"task_id": task_id, "predicate": predicate, "released": False,
                            "reason": "the producing task was canceled; its late output is "
                                      "audit data, not a dependency"}
            outcomes = [item for item in self._state.child_outcomes.values()
                        if item.get("task_id") == task_id]
            if predicate != "terminal_observed" and not outcomes:
                return {"task_id": task_id, "predicate": predicate, "released": False,
                        "reason": "no published result exists for the producing task"}
            latest = max(outcomes, key=lambda item: int(item.get("sequence", 0))) if outcomes else {}
            generation = int(latest.get("generation", 0)) if latest else 0
            generations = [int(event.payload["generation"]) for event in self._state.events
                           if event.event_type is RuntimeEventType.CANDIDATE_LEASE_OBSERVED
                           and event.payload.get("owner_node_id") == task_id]
            if generations and generation != max(generations):
                return {"task_id": task_id, "predicate": predicate, "released": False,
                        "reason": f"the published result is from generation {generation}, but "
                                  f"the task is now at generation {max(generations)}"}
            revision = str(latest.get("revision") or "")
            artifact_sha256 = latest.get("artifact_sha256")
            record = self._state.patch_records.get(f"patch-{task_id}", {})
            # Which attempt produced this evidence. The published outcome is the
            # authority when it names one; older outcomes recorded before that
            # field existed fall back to the attempt the node is really at.
            producing_attempt = str(latest.get("attempt_id") or "") or (
                runtime.current_attempt_id or (
                    runtime.attempts[-1].attempt_id if runtime.attempts else ""))
            if predicate == "artifact_validated":
                if not artifact_sha256 or not revision or latest.get("terminal") == "canceled":
                    return {"task_id": task_id, "predicate": predicate, "released": False,
                            "reason": "the published result carries no validated artifact"}
                return {"task_id": task_id, "predicate": predicate, "released": True,
                        "generation": generation, "terminal": latest.get("terminal"),
                        "attempt_id": producing_attempt,
                        "artifact_id": record.get("operation_id"),
                        "artifact_sha256": str(artifact_sha256), "revision": revision,
                        "candidate_id": str(latest.get("candidate_id") or ""),
                        "event_id": str(latest.get("event_id") or ""),
                        "sequence": int(latest.get("sequence", 0))}
            if predicate == "patch_applied_and_checked":
                checks = record.get("checks") or {}
                if not (latest.get("patch_applied") and latest.get("checked") and revision
                        and latest.get("terminal") != "canceled" and record.get("state") == "applied"
                        and checks and all(isinstance(item, Mapping) and item.get("passed") is True
                                           for item in checks.values())):
                    return {"task_id": task_id, "predicate": predicate, "released": False,
                            "reason": "the durable patch receipt is not applied-and-checked"}
                return {"task_id": task_id, "predicate": predicate, "released": True,
                        "generation": generation, "terminal": latest.get("terminal"),
                        "attempt_id": producing_attempt,
                        "artifact_id": record.get("operation_id"),
                        "artifact_sha256": str(record.get("observed_sha256")
                                               or record.get("patch_sha256") or ""),
                        "revision": revision,
                        "applied_revision": str(record.get("applied_revision") or ""),
                        "patch_sha256": str(record.get("patch_sha256") or ""),
                        "candidate_id": str(latest.get("candidate_id") or ""),
                        "event_id": str(latest.get("event_id") or ""),
                        "sequence": int(latest.get("sequence", 0))}
            if predicate == "terminal_observed":
                if latest:
                    return {"task_id": task_id, "predicate": predicate, "released": True,
                            "generation": generation, "terminal": latest.get("terminal"),
                            "attempt_id": producing_attempt,
                            "artifact_id": record.get("operation_id"),
                            "artifact_sha256": artifact_sha256,
                            "revision": revision,
                            "candidate_id": str(latest.get("candidate_id") or ""),
                            "event_id": str(latest.get("event_id") or ""),
                            "sequence": int(latest.get("sequence", 0))}
                # A terminal NODE state without a published outcome is a real
                # terminal observation; its identity is the node state itself.
                return {"task_id": task_id, "predicate": predicate, "released": True,
                        "generation": max(generations) if generations else 0,
                        "terminal": terminal_state, "attempt_id": "",
                        "artifact_id": record.get("operation_id"), "artifact_sha256": None,
                        "revision": "", "candidate_id": "",
                        "event_id": "", "sequence": 0}
            raise ReducerError("unknown dependency predicate")

    def verify_dependency_pin(self, pin: Mapping[str, Any]) -> str:
        """Whether a pinned dependency entry still describes the current dependency.

        Returns ``""`` when the pin is still exact, or a human-readable revocation
        reason. A pin is compared against a FRESH resolution of the same predicate,
        so a changed attempt, generation, artifact hash, patch revision, terminal
        state or cancellation epoch all revoke it.

        Only the facts the predicate itself asserts are compared. A published
        outcome is legitimately REPUBLISHED when the same child's facts change
        (produced -> applied-and-checked), which reassigns purely positional
        bookkeeping such as ``event_id`` and ``sequence``. Treating that
        reassignment as a changed dependency would revoke pins for a reason the
        predicate never claimed, so those fields are recorded for audit but are
        not identity-bearing here.
        """

        if not isinstance(pin, Mapping) or not pin.get("task_id") or not pin.get("predicate"):
            return "the dependency pin is malformed"
        if pin.get("run_id") and str(pin["run_id"]) != self.run_id:
            return "the run id changed since this dependency was pinned"
        predicate = str(pin["predicate"])
        current = self.dependency_pin(str(pin["task_id"]), predicate)
        if not current.get("released"):
            return str(current.get("reason") or "the dependency no longer satisfies its predicate")
        # ``pinned`` is the identity slice the producer recorded; older pins kept
        # those facts at the top level, so read through both shapes.
        identity = pin.get("pinned") if isinstance(pin.get("pinned"), Mapping) else pin
        for field in _PIN_IDENTITY_FIELDS.get(predicate, ()):
            if field not in identity:
                continue
            if identity.get(field) != current.get(field):
                return (f"the dependency's {field} changed from {identity.get(field)!r} to "
                        f"{current.get(field)!r}")
        if int(pin.get("contract_revision", 0) or 0) not in (0, self.current_plan.contract_revision):
            return "the contract revision changed since this dependency was pinned"
        if int(pin.get("plan_version", 0) or 0) not in (0, self.current_plan.plan_version):
            return "the plan version changed since this dependency was pinned"
        return ""

    def dependency_edge_satisfied(self, task_id: str, predicate: str) -> bool:
        """Whether one declared dependency edge really holds, not just "succeeded".

        ``patch_applied_and_checked`` reads the durable application receipt, not a
        writer's success: producing an artifact is a different fact from having it
        applied to the target candidate and passing the combination check.
        """
        with self._lock:
            runtime = self._state.nodes.get(task_id)
            if runtime is None:
                return False
            if predicate == "terminal_observed":
                return runtime.state in {NodeState.SUCCEEDED, NodeState.FAILED_TERMINAL,
                                         NodeState.FAILED_RETRYABLE, NodeState.CANCELED}
            if task_id in self.replaced_builder_ids():
                return False
            if runtime.state is not NodeState.SUCCEEDED:
                return False
            # A cancelled producer never satisfies a SUCCESS edge, whatever its node
            # state still says: a late artefact from a cancelled task is audit data,
            # not permission for a dependent write.
            if int(self._state.node_cancel_epoch.get(task_id, 0)) > 0:
                return False
            outcomes = [item for item in self._state.child_outcomes.values()
                        if item.get("task_id") == task_id]
            if not outcomes:
                return False
            latest = max(outcomes, key=lambda item: int(item.get("sequence", 0)))
            generations = [int(event.payload["generation"]) for event in self._state.events
                if event.event_type is RuntimeEventType.CANDIDATE_LEASE_OBSERVED
                and event.payload.get("owner_node_id") == task_id]
            if generations and int(latest.get("generation", 0)) != max(generations):
                return False
            if predicate == "artifact_validated":
                return bool(latest.get("artifact_sha256")) and bool(latest.get("revision")) \
                    and latest.get("terminal") != "canceled"
            if predicate == "patch_applied_and_checked":
                record = self._state.patch_records.get(f"patch-{task_id}", {})
                checks = record.get("checks") or {}
                return (bool(latest.get("patch_applied")) and bool(latest.get("checked"))
                        and bool(latest.get("revision")) and latest.get("terminal") != "canceled"
                        and record.get("state") == "applied" and bool(checks)
                        and all(isinstance(check, Mapping) and check.get("passed") is True for check in checks.values()))
            raise ReducerError("unknown dependency predicate")

    def _refresh_ready_nodes(self) -> None:
        for runtime in self._state.nodes.values():
            if runtime.state is not NodeState.PENDING:
                continue
            dependencies = [self._state.nodes.get(item) for item in runtime.definition.dependencies]
            if not dependencies or any(item is None for item in dependencies):
                continue
            predicates = self._state.dependency_predicates.get(runtime.definition.node_id, {})
            if any(not self.dependency_edge_satisfied(task_id, predicates[task_id]) if task_id in predicates
                   else self._state.nodes[task_id].state is not NodeState.SUCCEEDED for task_id in runtime.definition.dependencies):
                continue
            # A declared predicate is a real gate, not a label: a code dependency
            # is released only by a durable applied-and-checked receipt.
            if any(not self.dependency_edge_satisfied(task_id, predicate)
                   for task_id, predicate in predicates.items()):
                continue
            runtime.state = NodeState.READY
            self._emit(RuntimeEventType.NODE_READY, {"node_id": runtime.definition.node_id})

    def _snapshot_unlocked(self) -> dict[str, Any]:
        snapshot = {
            "schema_version": "minicode-runtime-state-v9" if self.shared_consultation_budget else "minicode-runtime-state-v8" if self.contract and self.contract.required_consultation else "minicode-runtime-state-v7" if self.closure_reserve_enabled else "minicode-runtime-state-v6" if self.advisory_enabled else "minicode-runtime-state-v5" if self.candidate_first else "minicode-runtime-state-v4" if self.continuations_enabled else "minicode-runtime-state-v3",
            **({"consultation_budget_policy": "shared-pool-v1",
                "required_consultation_enabled": bool(self.contract and self.contract.required_consultation)}
               if self.shared_consultation_budget else {}),
            **({"closure_budget": self.closure_budget_snapshot()} if self.closure_reserve_enabled else {}),
            **({"advisory_enabled": True} if self.advisory_enabled else {}),
            **({"candidate_first": True, "integration_status": self.integration_status,
                "integration_revision": self._state.integration_revision,
                "integration_error": self._state.integration_error} if self.candidate_first else {}),
            **({"continuation_limit": self._state.continuation_limit} if self.continuations_enabled else {}),
            **({"repair_limit": self.repair_limit, "repair_history": [item.to_dict() for item in self._state.repair_history],
                "pending_repair_id": self._state.pending_repair_id} if self.repair_limit else {}),
            "run_id": self._state.run_id,
            "contract_revision": self._state.contract_revision,
            "current_plan_version": self._state.current_plan_version,
            # The durable revision travels with the document so a second writer
            # can prove the file it is about to replace is the one it read.
            "state_revision": self._state.revision,

            "plans": {str(key): value.to_dict() for key, value in self._state.plans.items()},
            "contract": self._state.contract.to_dict() if self._state.contract else None,
            "identity": self._state.identity.to_dict() if self._state.identity else None,
            "final_verification_records": [
                record.to_dict() for record in self._state.final_verification_records
            ],
            "nodes": {
                node_id: {
                    "definition": runtime.definition.to_dict(),
                    "state": runtime.state.value,
                    "attempts": [attempt.to_dict() for attempt in runtime.attempts],
                    "current_attempt_id": runtime.current_attempt_id,
                    "result": runtime.result.to_dict() if runtime.result else None,
                    "rejection_reasons": list(runtime.rejection_reasons),
                    "resume_credits": runtime.resume_credits,
                    "workspace_committed": runtime.workspace_committed,
                    **({"review_status": "unavailable"} if self.optional_review_unavailable(node_id) else {}),
                    **({"failure_diagnosis": self._public_node_diagnosis(node_id)} if self.advisory_enabled else {}),
                    **({"advisory_acknowledgment": dict(runtime.advisory_acknowledgment)} if runtime.advisory_acknowledgment is not None else {}),
                    **({
                        "continuation": runtime.continuation.to_dict() if runtime.continuation else None,
                        "continuation_count": runtime.continuation_count,
                    } if self.continuations_enabled else {}),
                }
                for node_id, runtime in self._state.nodes.items()
            },
            # RuntimeEvent has a fixed scalar header and a JSON payload. Avoid
            # dataclasses.asdict's deepcopy plus a second generic traversal of
            # that same growing history; _jsonable still detaches containers.
            "events": [{"schema_version": event.schema_version, "event_id": event.event_id,
                "run_id": event.run_id, "sequence": event.sequence, "event_type": event.event_type.value,
                "payload": _jsonable(event.payload), "command_id": event.command_id,
                "created_at": event.created_at} for event in self._state.events],
            "canceled": self._state.canceled,
            "paused": self._state.paused,
            "processed_commands": {
                key: list(value) for key, value in self._state.processed_commands.items()
            },
            "consumed_approvals": sorted(self._state.consumed_approvals),
            "next_fencing_token": self._state.next_fencing_token,
            "tokens_used": self._state.tokens_used,
            "cost_used": self._state.cost_used,
            "usage_ledger": self._state.usage_ledger.to_dict(),
            "usage_summary": self.usage_summary(),
            # Durable orchestration state. It lives in the persisted snapshot so a
            # rebuilt Scheduler over the same Reducer recovers child results,
            # waits, resumes, cancellation identity and patch receipts instead of
            # silently starting empty.
            "delegation_parents": dict(self._state.delegation_parents),
            "node_cancel_epoch": dict(self._state.node_cancel_epoch),
            "cancel_operations": {key: dict(value) for key, value in self._state.cancel_operations.items()},
            "dependency_predicates": {key: dict(value) for key, value in self._state.dependency_predicates.items()},
            "child_outcomes": {key: dict(value) for key, value in self._state.child_outcomes.items()},
            "dependency_waits": {key: dict(value) for key, value in self._state.dependency_waits.items()},
            "wait_resumes": dict(self._state.wait_resumes),
            "patch_records": {key: dict(value) for key, value in self._state.patch_records.items()},
        }
        if any(node.result and node.result.consultation_summary_ref for node in self._state.nodes.values()) or any(
            e.event_type is RuntimeEventType.CONSULTATION_SUMMARY_READ or e.payload.get("consultation_summary_ref") is not None
            for e in self._state.events):
            snapshot.update(base_state_schema_version=snapshot["schema_version"],
                schema_version="minicode-runtime-state-v10", consultation_read_policy="consultation-paged-decision-v1")
        if (self._state.worker_completions or self._state.candidate_quarantines or self._state.candidate_selections
            or any(node.definition.alternative_group for node in self._state.nodes.values())):
            snapshot.update(orchestration_base_schema=snapshot["schema_version"],
                schema_version="minicode-runtime-state-v11", candidate_lifecycle_policy="candidate-lifecycle-v1",
                worker_completions=deepcopy(self._state.worker_completions),
                candidate_quarantines=deepcopy(self._state.candidate_quarantines),
                candidate_selections=deepcopy(self._state.candidate_selections))
        if self.async_agents_enabled:
            snapshot.update(agent_base_schema=snapshot["schema_version"],
                schema_version="minicode-runtime-state-v12",
                agent_session_policy="async-agents-shared-writes-v1" if self.shared_workspace_writes_enabled else "async-agents-v1",
                agent_sessions=deepcopy(self._state.agent_sessions))
        if self.agent_messages_enabled:
            snapshot.update(schema_version="minicode-runtime-state-v13", agent_message_policy="parent-child-inbox-v1",
                agent_messages=deepcopy(self._state.agent_messages), agent_checkpoints=deepcopy(self._state.agent_checkpoints),
                agent_finishing=deepcopy(self._state.agent_finishing))
        if self.agent_resume_enabled:
            snapshot.update(schema_version="minicode-runtime-state-v14", agent_resume_policy="bounded-session-continuation-v1")
        if self._state.agent_team is not None:
            snapshot.update(schema_version="minicode-runtime-state-v15", agent_team=deepcopy(self._state.agent_team))
        if self._state.agent_fork_enabled:
            snapshot.update(agent_fork_base_schema=snapshot["schema_version"], schema_version="minicode-runtime-state-v16",
                agent_fork_policy="readonly-context-fork-v1")
        if self.coordinator_enabled or self.agent_team_writes_enabled:
            snapshot.update(agent_capabilities_base_schema=snapshot["schema_version"], schema_version="minicode-runtime-state-v17",
                agent_capabilities={"policy": "parent-child-capabilities-v1", "coordinator": self.coordinator_enabled,
                    "team_writes": self.agent_team_writes_enabled})
        if any("body_artifact" in record for record in self._state.agent_messages.values()):
            snapshot.update(agent_message_artifact_base_schema=snapshot["schema_version"],
                schema_version="minicode-runtime-state-v18", agent_message_artifact_policy="owned-message-body-v1")
        if self._state.user_steering is not None:
            snapshot.update(user_steering_base_schema=snapshot["schema_version"],
                schema_version="minicode-runtime-state-v19", user_steering=deepcopy(self._state.user_steering))
        if self._state.agent_collaboration is not None:
            snapshot.update(agent_collaboration_base_schema=snapshot["schema_version"],
                schema_version="minicode-runtime-state-v20", agent_collaboration=deepcopy(self._state.agent_collaboration))
        if self.dynamic_review_enabled:
            snapshot.update(review_base_schema=snapshot["schema_version"], schema_version="minicode-runtime-state-v21",
                review_runtime=deepcopy(self._state.review_runtime))
        return snapshot

    def _persist(self, *, retry_until: float | None = None) -> None:
        if self._store is None:
            return
        self._state.revision += 1
        try:
            payload = self._snapshot_unlocked()
            for attempt in range(3):
                try:
                    self._store.save(payload)
                    break
                except Exception as error:
                    remaining = 0.0 if retry_until is None else retry_until - self._clock.now()
                    if attempt == 2 or remaining <= 0 or not transient_store_error(error):
                        raise
                    delay = min(0.1 * 2 ** attempt, remaining / 4)
                    logging.getLogger(__name__).warning("Lease persistence temporarily unavailable; retry %s in %.3fs (%s)",
                                                        attempt + 1, delay, type(error).__name__)
                    time.sleep(delay)
                    if self._clock.now() >= retry_until:
                        raise
        except BaseException as error:
            # The file was not replaced, so the in-memory revision must not claim
            # a durable advance that never happened.
            self._state.revision -= 1
            self._persistence_error = error
            raise


def command(
    run_id: str,
    command_type: RuntimeCommandType,
    payload: Mapping[str, Any] | None = None,
    *,
    command_id: str | None = None,
) -> RuntimeCommand:
    return RuntimeCommand(
        command_id=command_id or new_id("command"),
        run_id=run_id,
        command_type=command_type,
        payload=dict(payload or {}),
    )


def _terminalize_unsafe_result(result: AgentResult) -> AgentResult:
    """Fail closed when a Worker reports an internally inconsistent success."""

    failure = result.failure
    if failure is None:
        return result
    unsafe_failure = (
        failure.kind
        in {
            ExecutionFailureKind.TOOL_TIMEOUT,
            ExecutionFailureKind.WORKER_TIMED_OUT,
            ExecutionFailureKind.UNCERTAIN_SIDE_EFFECT,
        }
        or failure.ambiguous_side_effect
    )
    if result.status is not AgentResultStatus.SUCCEEDED and not unsafe_failure:
        return result
    return replace(
        result,
        status=AgentResultStatus.FAILED_TERMINAL,
        failure=replace(
            failure,
            retryable=False,
            ambiguous_side_effect=(
                failure.ambiguous_side_effect
                or failure.kind is ExecutionFailureKind.UNCERTAIN_SIDE_EFFECT
            ),
        ),
    )


def _node_from_dict(raw: Mapping[str, Any]) -> PlanNode:
    return PlanNode(
        node_id=str(raw.get("node_id", "")),
        objective=str(raw.get("objective", "")),
        role=str(raw.get("role", "")),
        dependencies=tuple(str(item) for item in raw.get("dependencies", [])),
        allowed_effects=tuple(ToolEffect(str(item)) for item in raw.get("allowed_effects", [])),
        acceptance_criteria=tuple(str(item) for item in raw.get("acceptance_criteria", [])),
        max_attempts=int(raw.get("max_attempts", 1)),
        criticality=raw.get("criticality", "required"),
        context_files=raw.get("context_files", ()),
        write_scope=raw.get("write_scope", ()),
        alternative_group=raw.get("alternative_group"),
        runtime_profile=raw.get("runtime_profile"),
        review_policy=raw.get("review_policy"),
    )


def _plan_from_dict(raw: Mapping[str, Any]) -> PlanVersion:
    return PlanVersion(
        run_id=str(raw.get("run_id", "")),
        plan_version=int(raw.get("plan_version", 1)),
        contract_revision=int(raw.get("contract_revision", 1)),
        nodes=tuple(
            _node_from_dict(item)
            for item in raw.get("nodes", [])
            if isinstance(item, Mapping)
        ),
        created_at=float(raw.get("created_at", 0.0)),
        supersedes=int(raw["supersedes"]) if raw.get("supersedes") is not None else None,
    )


def _attempt_from_dict(raw: Mapping[str, Any]) -> NodeAttempt:
    lease_raw = raw.get("lease", {})
    if not isinstance(lease_raw, Mapping):
        lease_raw = {}
    token_raw = lease_raw.get("fencing_token", {})
    token_value = token_raw.get("value", 0) if isinstance(token_raw, Mapping) else token_raw
    return NodeAttempt(
        attempt_id=str(raw.get("attempt_id", "")),
        run_id=str(raw.get("run_id", "")),
        plan_version=int(raw.get("plan_version", 1)),
        node_id=str(raw.get("node_id", "")),
        attempt_number=int(raw.get("attempt_number", 1)),
        lease=Lease(
            lease_owner=str(lease_raw.get("lease_owner", "")),
            lease_expiry=float(lease_raw.get("lease_expiry", 0.0)),
            fencing_token=FencingToken(int(token_value)),
        ),
        state=NodeState(str(raw.get("state", NodeState.CLAIMED.value))),
        idempotency_key=str(raw.get("idempotency_key", "")),
        started_at=float(raw["started_at"]) if raw.get("started_at") is not None else None,
        completed_at=float(raw["completed_at"]) if raw.get("completed_at") is not None else None,
    )


def _evidence_from_dict(raw: Mapping[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        evidence_type=str(raw.get("evidence_type", "")),
        resource=str(raw.get("resource", "")),
        content_hash=str(raw.get("content_hash", "")),
        claim=str(raw.get("claim", "")),
        producer_node_id=str(raw.get("producer_node_id", "")),
        base_revision=str(raw.get("base_revision", "")),
        line_start=int(raw["line_start"]) if raw.get("line_start") is not None else None,
        line_end=int(raw["line_end"]) if raw.get("line_end") is not None else None,
        page=int(raw["page"]) if raw.get("page") is not None else None,
    )


def _artifact_from_dict(raw: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=str(raw.get("artifact_id", "")),
        content_hash=str(raw.get("content_hash", "")),
        size=int(raw.get("size", 0)),
        media_type=str(raw.get("media_type", "application/octet-stream")),
        store_key=str(raw.get("store_key", "")),
    )


def _verification_from_dict(raw: Mapping[str, Any]) -> VerificationRecord:
    return VerificationRecord(
        verifier=str(raw.get("verifier", "")),
        passed=bool(raw.get("passed", False)),
        scope=str(raw.get("scope", "")),
        details=str(raw.get("details", "")),
        evidence_refs=tuple(
            _evidence_from_dict(item)
            for item in raw.get("evidence_refs", [])
            if isinstance(item, Mapping)
        ),
        deterministic=bool(raw.get("deterministic", True)),
        covered_criteria_ids=tuple(
            str(item) for item in raw.get("covered_criteria_ids", [])
        ),
        created_at=float(raw.get("created_at", 0.0)),
    )


def _budget_from_dict(raw: Mapping[str, Any]) -> BudgetEnvelope:
    return BudgetEnvelope(
        max_agents=int(raw.get("max_agents", 1)),
        max_active_workers=int(raw.get("max_active_workers", 1)),
        max_delegation_depth=int(raw.get("max_delegation_depth", 1)),
        max_tokens=(int(raw["max_tokens"]) if raw.get("max_tokens") is not None else None),
        max_cost=(float(raw["max_cost"]) if raw.get("max_cost") is not None else None),
        deadline=(float(raw["deadline"]) if raw.get("deadline") is not None else None),
        retry_budget=int(raw.get("retry_budget", 0)),
    )


def _contract_from_dict(raw: Mapping[str, Any]) -> TaskContract:
    envelope_raw = raw.get("envelope", {})
    if not isinstance(envelope_raw, Mapping):
        raise RuntimeStateLoadError("Runtime snapshot TaskContract envelope is invalid")
    budget_raw = raw.get("budget", {})
    if not isinstance(budget_raw, Mapping):
        raise RuntimeStateLoadError("Runtime snapshot TaskContract budget is invalid")
    return TaskContract(
        envelope=TaskEnvelope(
            run_id=str(envelope_raw.get("run_id", "")),
            request=str(envelope_raw.get("request", "")),
            workspace=str(envelope_raw.get("workspace", "")),
            requested_profile=RequestedMode(
                str(envelope_raw.get("requested_profile", RequestedMode.STANDARD.value))
            ),
            created_at=float(envelope_raw.get("created_at", 0.0)),
            session_id=(
                str(envelope_raw["session_id"])
                if envelope_raw.get("session_id") is not None
                else None
            ),
        ),
        contract_revision=int(raw.get("contract_revision", 1)),
        constraints=tuple(str(item) for item in raw.get("constraints", [])),
        acceptance_criteria=tuple(
            str(item) for item in raw.get("acceptance_criteria", [])
        ),
        expected_outputs=tuple(str(item) for item in raw.get("expected_outputs", [])),
        budget=_budget_from_dict(budget_raw),
        required_consultation=RequiredConsultation.parse(raw.get("required_consultation")),
    )


def _identity_from_dict(raw: Mapping[str, Any]) -> RuntimeRunIdentity:
    return RuntimeRunIdentity(
        run_id=str(raw.get("run_id", "")),
        workspace=str(raw.get("workspace", "")),
        request_hash=str(raw.get("request_hash", "")),
        messages_hash=str(raw.get("messages_hash", "")),
        profile_hash=str(raw.get("profile_hash", "")),
        policy_hash=str(raw.get("policy_hash", "")),
        session_id=(str(raw["session_id"]) if raw.get("session_id") is not None else None),
        requested_profile=RequestedMode(str(raw.get("requested_profile", "standard"))),
        selected_route=SelectedRoute(str(raw.get("selected_route", "standard-single"))),
        policy_version=str(raw.get("policy_version", "runtime-policy-v1")),
        prompt_template_version=str(
            raw.get("prompt_template_version", "agent-loop-v1")
        ),
    )


def _result_from_dict(raw: Mapping[str, Any]) -> AgentResult:
    summary_ref = raw.get("consultation_summary_ref")
    if summary_ref is not None and not isinstance(summary_ref, Mapping):
        raise ReducerError("invalid consultation summary reference")
    failure_raw = raw.get("failure")
    failure = None
    if isinstance(failure_raw, Mapping):
        failure = ExecutionFailure(
            kind=ExecutionFailureKind(str(failure_raw.get("kind"))),
            message=str(failure_raw.get("message", "")),
            retryable=bool(failure_raw.get("retryable", False)),
            ambiguous_side_effect=bool(failure_raw.get("ambiguous_side_effect", False)),
        )
    return AgentResult(
        run_id=str(raw.get("run_id", "")),
        plan_version=int(raw.get("plan_version", 1)),
        node_id=str(raw.get("node_id", "")),
        attempt_id=str(raw.get("attempt_id", "")),
        status=AgentResultStatus(str(raw.get("status"))),
        base_revision=str(raw.get("base_revision", "")),
        output_summary=str(raw.get("output_summary", "")),
        contract_revision=int(raw.get("contract_revision", 1)),
        evidence_refs=tuple(
            _evidence_from_dict(item)
            for item in raw.get("evidence_refs", [])
            if isinstance(item, Mapping)
        ),
        artifact_refs=tuple(
            _artifact_from_dict(item)
            for item in raw.get("artifact_refs", [])
            if isinstance(item, Mapping)
        ),
        changed_resources=tuple(str(item) for item in raw.get("changed_resources", [])),
        verification_records=tuple(
            _verification_from_dict(item)
            for item in raw.get("verification_records", [])
            if isinstance(item, Mapping)
        ),
        usage=dict(raw.get("usage", {})),
        metrics=dict(raw.get("metrics", {})),
        warnings=tuple(str(item) for item in raw.get("warnings", [])),
        failure=failure,
        continuation=_continuation_from_dict(raw["continuation"]) if isinstance(raw.get("continuation"), Mapping) else None,
        session_checkpoint=_artifact_from_dict(raw["session_checkpoint"]) if isinstance(raw.get("session_checkpoint"), Mapping) else None,
        consultation_summary_ref=_artifact_from_dict(summary_ref) if summary_ref is not None else None,
    )


def _repair_from_dict(raw: Mapping[str, Any]) -> PrimaryRepair:
    if raw.get("schema_version") != "minicode-primary-repair-v1":
        raise ReducerError("unsupported primary repair checkpoint version")
    return PrimaryRepair(
        repair_id=str(raw["repair_id"]), run_id=str(raw["run_id"]), node_id=str(raw["node_id"]),
        prior_attempt_id=str(raw["prior_attempt_id"]), plan_version=int(raw["plan_version"]),
        contract_revision=int(raw["contract_revision"]), candidate_revision=str(raw["candidate_revision"]),
        checkpoint=_artifact_from_dict(raw["checkpoint"]),
        diagnostics=tuple(_verification_from_dict(item) for item in raw["diagnostics"]),
        review_result=_result_from_dict(raw["review_result"]) if raw.get("review_result") is not None else None,
    )


def _continuation_from_dict(raw: Mapping[str, Any]) -> WorkerContinuation:
    if raw.get("schema_version") != "minicode-continuation-v1":
        raise ReducerError("unsupported continuation checkpoint version")
    return WorkerContinuation(
        continuation_id=str(raw["continuation_id"]),
        checkpoint=_artifact_from_dict(raw["checkpoint"]),
        children=tuple(_node_from_dict(node) for node in raw["children"]),
        input_revision=str(raw["input_revision"]),
        kind=str(raw.get("kind", "consultation")),
        dependency_wait=raw.get("dependency_wait"),
    )


def _event_from_dict(raw: Mapping[str, Any]) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=str(raw.get("event_id", "")),
        run_id=str(raw.get("run_id", "")),
        sequence=int(raw.get("sequence", 0)),
        event_type=RuntimeEventType(str(raw.get("event_type"))),
        payload=dict(raw.get("payload", {})),
        command_id=str(raw["command_id"]) if raw.get("command_id") is not None else None,
        created_at=float(raw.get("created_at", 0.0)),
    )
