"""Host-side entry point for retiring quarantined isolated candidates.

The native API (``TaskScheduler.retire_quarantined_candidate``) is deliberately
not reachable by a model. This module is the operator side of that boundary: it
opens a DURABLE Runtime state, enumerates what is still quarantined, and retires
one candidate only when the caller supplies the real Runtime objects that can
prove the retired bytes are the ones the Runtime archived.

What it will not do, by construction:

* It never builds an ``AgentLoopWorkerRunner`` itself. The archive proof needs the
  real ``RuntimeArtifactStore`` and the real candidate provider, so the caller
  passes them in; a module that quietly constructed a fresh store would be able to
  "retire" a candidate it never archived.
* It refuses a node with external or privileged effects, and it refuses while the
  Worker that owns the candidate has not exited.
* It writes no user file and never turns a failed or canceled result into success.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicode.run_ownership import (
    RUN_OWNER_SCHEMA,
    RuntimeRunOwnerLease,
    live_holder,
    normalize_state_path,
    owner_summary,
    read_owner_record,
)

STATE_SCHEMA = "minicode-quarantine-retirement-v1"
RETIREABLE_EFFECTS = ("read_only", "workspace_write")


@dataclass(frozen=True, slots=True)
class QuarantineView:
    """One durable quarantine as the operator sees it, before anything is proved."""

    attempt_id: str
    status: str
    node_id: str
    candidate_id: str
    role: str
    is_writer: bool
    allowed_effects: tuple[str, ...]
    effect_scope: str
    retired_dir: str | None
    archive_refs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "status": self.status,
            "node_id": self.node_id,
            "candidate_id": self.candidate_id,
            "role": self.role,
            "is_writer": self.is_writer,
            "allowed_effects": list(self.allowed_effects),
            "effect_scope": self.effect_scope,
            "retired_dir": self.retired_dir,
            "archive_refs": self.archive_refs,
        }


def _effect_scope(effects: tuple[str, ...]) -> str:
    """``retireable`` only when every declared effect is confined to candidate bytes."""
    extra = sorted(set(effects) - set(RETIREABLE_EFFECTS))
    return "retireable" if not extra else "external:" + ",".join(extra)


class QuarantineRetirement:
    """Operator/ops surface over a durable Runtime state file.

    Two modes, and the difference is an executable fact, not a convention:

    ``scheduler=`` -- ONLINE. The caller passes the REAL Scheduler that currently
    owns this run. Retirement then travels through that object's serialized
    mutation channel, so it cannot race the live run's own transitions.

    ``scheduler_factory=`` -- OFFLINE. No live owner is passed, so this surface
    must first prove there is none: it takes the run's exclusive ownership claim
    and refuses while a live process still holds it. It commits against the exact
    revision it read, so a writer that appears in between is detected instead of
    overwritten.

    In both modes ``worker_factory``/the supplied worker must be the REAL Worker
    that owns the quarantined candidate handle: only that object can verify the
    retained archive against the candidate's actual bytes.
    """

    def __init__(
        self,
        *,
        state_path: str | Path,
        scheduler_factory: Callable[[Any], Any] | None = None,
        worker_factory: Callable[[Any, Any, Mapping[str, Any]], Any] | None = None,
        scheduler: Any | None = None,
        worker: Any | None = None,
        lock_root: str | Path | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        # Compatibility input only. The normalized state path now determines one
        # canonical ownership namespace for the Runtime and every operator.
        self.lock_root = Path(lock_root) if lock_root is not None else None
        if scheduler is None and scheduler_factory is None:
            raise TypeError(
                "quarantine retirement needs the live Scheduler (online) or a "
                "scheduler_factory (offline)")
        if worker is None and worker_factory is None:
            raise TypeError("quarantine retirement needs the real Worker that owns the candidate")
        if scheduler_factory is not None and not callable(scheduler_factory):
            raise TypeError("scheduler_factory must be callable")
        if worker_factory is not None and not callable(worker_factory):
            raise TypeError("worker_factory must be callable")
        self._scheduler_factory = scheduler_factory
        self._worker_factory = worker_factory
        self._scheduler = scheduler
        self._worker = worker
        # Serializes this surface's own retirements so two calls for one attempt
        # cannot both pass the "not retired yet" check and release the hold twice.
        self._retirement_lock = threading.Lock()

    # ---- ownership ------------------------------------------------------

    @property
    def online(self) -> bool:
        return self._scheduler is not None

    def ownership(self) -> dict[str, Any]:
        """Who really owns this state right now; read-only and side-effect free."""

        holder = live_holder(self.state_path, lock_root=self.lock_root)
        return {
            "state_path": str(self.state_path),
            "online_owner_supplied": self._scheduler is not None,
            "live_holder": owner_summary(holder),
            "offline_write_allowed": self._scheduler is None and holder is None,
            "recorded_claim": owner_summary(
                read_owner_record(self.state_path, lock_root=self.lock_root)),
        }

    def _assert_online_owner(self, reducer) -> Any:
        """Refuse a Scheduler that is not the real owner of this exact state file."""

        scheduler = self._scheduler
        owner = getattr(scheduler, "reducer", None)
        if owner is None or not hasattr(owner, "run_id"):
            raise TypeError("the supplied online owner is not a Scheduler with a Reducer")
        if not callable(getattr(scheduler, "retire_quarantined_candidate", None)):
            raise TypeError("the supplied online owner cannot retire a quarantined candidate")
        if owner.run_id != reducer.run_id:
            raise RuntimeError(
                "the supplied Scheduler belongs to run "
                f"{owner.run_id!r}, not to the state at {self.state_path} ({reducer.run_id!r})")
        owner_path = getattr(owner, "state_path", None)
        if owner_path is None:
            raise RuntimeError(
                "the supplied Scheduler's Reducer has no durable state file, so it cannot "
                "be the owner of " + str(self.state_path))
        if normalize_state_path(owner_path) != normalize_state_path(self.state_path):
            raise RuntimeError(
                "the supplied Scheduler owns a different state file "
                f"({owner_path}); it is not the owner of {self.state_path}")
        if reducer.revision != owner.revision:
            raise RuntimeError(
                "the operator's view of the state is already stale (revision "
                f"{reducer.revision} vs {owner.revision}); re-read before retiring")
        lease = getattr(getattr(owner, "_store", None), "owner_lease", None)
        if lease is None:
            raise RuntimeError("the supplied Scheduler has no bound live ownership claim")
        lease.validate_binding(owner)
        return scheduler

    def _assert_offline_ownership(self, run_id: str) -> RuntimeRunOwnerLease:
        """Take the run's exclusive claim, or refuse because a live owner exists."""

        holder = live_holder(self.state_path, lock_root=self.lock_root)
        if holder is not None:
            raise RuntimeError(
                "refusing an offline retirement: a live process still owns this Runtime state "
                f"(run {holder.get('run_id')!r}, pid {holder.get('pid')!r}). Route the retirement "
                "through that run's live Scheduler instead of writing the file from here.")
        if not run_id:
            raise RuntimeError("the durable Runtime state has no run id")
        lease = RuntimeRunOwnerLease(self.state_path, run_id=run_id, lock_root=self.lock_root)
        try:
            lease.acquire()
        except Exception as error:  # noqa: BLE001 - a refused claim is the safety property
            raise RuntimeError(
                f"refusing an offline retirement: the exclusive ownership claim was not "
                f"granted ({type(error).__name__}: {error})") from error
        return lease

    def _load_payload(self) -> dict[str, Any]:
        from minicode.runtime_state import RuntimeStateStore

        payload = RuntimeStateStore(self.state_path).load()
        if payload is None:
            raise RuntimeError(f"no durable Runtime state at {self.state_path}")
        return payload

    # ---- read paths -----------------------------------------------------

    def load(self):
        """Restore the durable state exactly as a resume would, or refuse.

        The restored Reducer keeps the SAME state store and the revision it read,
        so a retirement either really lands on disk or is refused as stale; a
        receipt that only existed in this process would make the durable
        quarantine status disagree with the operator's report.
        """
        from minicode.runtime_state import RuntimeReducer, RuntimeStateStore

        store = RuntimeStateStore(self.state_path)
        payload = store.load()
        if payload is None:
            raise RuntimeError(f"no durable Runtime state at {self.state_path}")
        return RuntimeReducer.restore(payload, store=store)

    def inspect(self) -> dict[str, Any]:
        """Read-only inventory: what is quarantined and what could ever be retired."""
        reducer = self.load()
        definitions = {node.node_id: node for node in reducer.current_plan.nodes}
        records = reducer.candidate_quarantines()
        views = []
        for attempt_id, record in sorted(records.items()):
            node = definitions.get(str(record.get("node_id")))
            effects = tuple(effect.value for effect in node.allowed_effects) if node else ()
            views.append(QuarantineView(
                attempt_id=attempt_id,
                status=str(record.get("status")),
                node_id=str(record.get("node_id")),
                candidate_id=str(record.get("candidate_id")),
                role=str(node.role) if node else "unknown",
                is_writer=bool(node.is_writer) if node else False,
                allowed_effects=effects,
                effect_scope=_effect_scope(effects) if node else "unknown-node",
                retired_dir=_retired_directory(record),
                archive_refs=sum(
                    1 for ref in (record.get("artifact_refs") or ())
                    if isinstance(ref, Mapping)
                    and ref.get("media_type") == "application/vnd.minicode.unsubmitted-candidate+json"),
            ).to_dict())
        return {
            "schema": STATE_SCHEMA,
            "state_path": str(self.state_path),
            "run_id": reducer.run_id,
            "state_revision": reducer.revision,
            "pending": sorted(item["attempt_id"] for item in views if item["status"] == "pending"),
            "quarantines": views,
            "ownership": self.ownership(),
            "model_callable": False,
            "note": "This surface is for the host/operator. It is not registered in any "
                    "ToolRegistry and no model-facing tool may call it.",
        }

    # ---- the retirement itself ------------------------------------------

    def retire(self, attempt_id: str) -> dict[str, Any]:
        """Permanently retire ONE quarantined candidate after native verification."""
        from minicode.runtime_state import StaleStateWrite

        reducer = self.load()
        mode = "online" if self.online else "offline"
        lease = None
        try:
            if self.online:
                records = reducer.candidate_quarantines()
                record = records.get(attempt_id)
                if record is None:
                    raise RuntimeError(f"no durable quarantine for attempt {attempt_id!r}")
                if record.get("status") == "retired":
                    return self._idempotent_receipt(attempt_id, record, mode)
                self._assert_retireable(reducer, record)
                return self._retire_through_owner(attempt_id, record, reducer, mode)
            lease = self._assert_offline_ownership(reducer.run_id)
            # Re-read INSIDE the ownership claim: the snapshot taken before the
            # claim proved nothing about who owns the file now.
            reducer = self.load()
            lease.bind(reducer)
            committed_revision = reducer.revision
            records = reducer.candidate_quarantines()
            record = records.get(attempt_id)
            if record is None:
                raise RuntimeError(f"no durable quarantine for attempt {attempt_id!r}")
            if record.get("status") == "retired":
                return self._idempotent_receipt(attempt_id, record, mode)
            self._assert_retireable(reducer, record)
            with self._retirement_lock:
                record = reducer.candidate_quarantines().get(attempt_id)
                if record is None:
                    raise RuntimeError(f"no durable quarantine for attempt {attempt_id!r}")
                if record.get("status") == "retired":
                    return self._idempotent_receipt(attempt_id, record, mode)
                return self._retire_offline(attempt_id, record, reducer, lease, mode,
                                            committed_revision)
        except StaleStateWrite as error:
            raise RuntimeError(
                "refusing to retire from a stale operator snapshot: "
                f"{error}. Re-read the durable state and retry.") from error
        finally:
            if lease is not None:
                lease.release()

    # ---- helpers --------------------------------------------------------

    def _idempotent_receipt(self, attempt_id: str, record: Mapping[str, Any],
                            mode: str) -> dict[str, Any]:
        """A repeat retirement releases nothing a second time."""

        return {"schema": STATE_SCHEMA, "attempt_id": attempt_id, "mode": mode,
                "retired": True, "already_retired": True, "capacity_released": False,
                "capacity_released_count": 0, "released_reservations": [],
                "release_scope": "none: already retired", "status": record.get("status")}

    def _assert_retireable(self, reducer, record: Mapping[str, Any]) -> None:
        definitions = {node.node_id: node for node in reducer.current_plan.nodes}
        node = definitions.get(str(record.get("node_id")))
        if node is None:
            raise RuntimeError("the quarantined node is not part of the durable current plan")
        scope = _effect_scope(tuple(effect.value for effect in node.allowed_effects))
        if scope != "retireable":
            raise RuntimeError(
                "this candidate declared effects outside read_only/workspace_write "
                f"({scope}); it needs separate operator reconciliation")

    def _assert_worker_identity(self, worker: Any, scheduler: Any, reducer) -> None:
        """The proof object must be the Worker of THIS run's Scheduler."""

        if worker is None:
            raise RuntimeError("the quarantined Worker is not available to prove the archive")
        if not callable(getattr(worker, "verify_quarantined_candidate", None)):
            raise TypeError("the supplied Worker cannot verify a quarantined candidate")
        context = getattr(worker, "runtime_context", None)
        bound = getattr(context, "_scheduler", None) if context is not None else None
        if bound is None:
            raise RuntimeError(
                "the supplied Worker is not bound to the Scheduler that owns this run")
        if bound is not scheduler:
            raise RuntimeError(
                "the supplied Worker belongs to a different Scheduler than the one "
                "operating on this state")

    @staticmethod
    def _capacity_view(scheduler: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = scheduler.capacity.snapshot()
        reservations = tuple(snapshot.get("reservations") or ())
        return snapshot, {str(item.get("reservation_id")): dict(item) for item in reservations}

    def _retire_through_owner(self, attempt_id: str, record: Mapping[str, Any],
                              reducer, mode: str) -> dict[str, Any]:
        """ONLINE: mutate through the live Scheduler that owns this run."""

        scheduler = self._assert_online_owner(reducer)
        worker = self._worker if self._worker is not None else self._worker_factory(
            scheduler.reducer, scheduler, record)
        self._assert_worker_identity(worker, scheduler, reducer)
        # Two operator calls for the same attempt must not both walk into the
        # native retirement and release the hold twice. The durable status is
        # re-read inside this serialization, after the first call committed.
        with self._retirement_lock:
            current = scheduler.reducer.candidate_quarantines().get(attempt_id)
            if current is None:
                raise RuntimeError(f"no durable quarantine for attempt {attempt_id!r}")
            if current.get("status") == "retired":
                return self._idempotent_receipt(attempt_id, current, mode)
            record = current
            before, before_reservations = self._capacity_view(scheduler)
            retired = scheduler.retire_quarantined_candidate(attempt_id=attempt_id, worker=worker)
            after, after_reservations = self._capacity_view(scheduler)
        released = sorted(set(before_reservations) - set(after_reservations))
        evidence = retired.get("evidence") or {}
        return {
            "schema": STATE_SCHEMA,
            "attempt_id": attempt_id,
            "mode": mode,
            "owner_run_id": reducer.run_id,
            "owner_state_path": str(self.state_path),
            "node_id": record.get("node_id"),
            "candidate_id": record.get("candidate_id"),
            "retired": retired.get("status") == "retired",
            "already_retired": False,
            "status": retired.get("status"),
            "disposition": evidence.get("disposition"),
            "candidate_revision": evidence.get("candidate_revision"),
            "capacity_before": before,
            "capacity_after": after,
            "capacity_released": bool(released),
            "capacity_released_count": len(released),
            "released_reservations": [before_reservations[key] for key in released],
            "release_scope": "live reservations of the owning Scheduler",
            "node_state": scheduler.reducer.node_state(str(record.get("node_id"))).value,
            "state_revision": scheduler.reducer.revision,
            "note": "Retirement frees the isolation hold. It never rewrites the failed or "
                    "canceled result and never applies these bytes anywhere.",
        }

    def _retire_offline(self, attempt_id: str, record: Mapping[str, Any], reducer,
                        lease: RuntimeRunOwnerLease, mode: str,
                        committed_revision: int) -> dict[str, Any]:
        """OFFLINE: no live owner exists, and this process now holds the claim."""

        from minicode.runtime_state import RuntimeStateStore

        scheduler = self._scheduler_factory(reducer) if self._scheduler_factory is not None else None
        if scheduler is None:
            raise RuntimeError("the offline scheduler factory returned no Scheduler")
        if not callable(getattr(scheduler, "retire_quarantined_candidate", None)):
            raise TypeError("the offline scheduler factory must return a real TaskScheduler")
        worker = self._worker if self._worker is not None else self._worker_factory(
            reducer, scheduler, record)
        self._assert_worker_identity(worker, scheduler, reducer)
        before, before_reservations = self._capacity_view(scheduler)
        retired = scheduler.retire_quarantined_candidate(attempt_id=attempt_id, worker=worker)
        after, after_reservations = self._capacity_view(scheduler)
        released = sorted(set(before_reservations) - set(after_reservations))
        # The claim is held, but the document this writer read must still be the
        # document on disk. A second writer that appeared in between is detected
        # here instead of having its transitions erased.
        if reducer.revision != committed_revision + 1:
            raise RuntimeError(
                "the offline retirement did not commit exactly one durable revision "
                f"({committed_revision!r} -> {reducer.revision!r}); the durable state was "
                "not advanced as expected. Re-read and retry.")
        durable = RuntimeStateStore(self.state_path).read_revision()
        if durable != reducer.revision:
            raise RuntimeError(
                "the durable Runtime state advanced to revision "
                f"{durable!r} while this offline retirement held revision "
                f"{reducer.revision!r}; the retirement was not committed. Re-read and retry.")
        lease.validate(run_id=reducer.run_id)
        evidence = retired.get("evidence") or {}
        return {
            "schema": STATE_SCHEMA,
            "attempt_id": attempt_id,
            "mode": mode,
            "owner_run_id": reducer.run_id,
            "owner_state_path": str(self.state_path),
            "node_id": record.get("node_id"),
            "candidate_id": record.get("candidate_id"),
            "retired": retired.get("status") == "retired",
            "already_retired": False,
            "status": retired.get("status"),
            "disposition": evidence.get("disposition"),
            "candidate_revision": evidence.get("candidate_revision"),
            "capacity_before": before,
            "capacity_after": after,
            "capacity_released": bool(released),
            "capacity_released_count": len(released),
            "released_reservations": [before_reservations[key] for key in released],
            "release_scope": "durable isolation holds rebuilt from the snapshot; no live "
                             "process held this run while the retirement ran",
            "node_state": reducer.node_state(str(record.get("node_id"))).value,
            "state_revision": reducer.revision,
            "note": "Retirement frees the isolation hold. It never rewrites the failed or "
                    "canceled result and never applies these bytes anywhere.",
        }


def _retired_directory(record: Mapping[str, Any]) -> str | None:
    """The isolated directory named by the recovery archive, if one is present."""
    for ref in record.get("artifact_refs") or ():
        if not isinstance(ref, Mapping):
            continue
        if ref.get("media_type") != "application/vnd.minicode.unsubmitted-candidate+json":
            continue
        resource = ref.get("resource")
        if isinstance(resource, str) and resource:
            return resource
    return None


def retirement_scheduler_factory(profile_factory: Callable[[Any], Any] | None = None):
    """Build the real Scheduler for a restored Reducer.

    ``profile_factory(reducer)`` lets the host reuse the profile it persisted with
    the run; the default derives one from the durable plan through the same
    resolver a live assembly uses.
    """
    from minicode.runtime_scheduler import TaskScheduler

    def factory(reducer):
        profile = profile_factory(reducer) if profile_factory is not None else None
        if profile is None:
            raise RuntimeError(
                "quarantine retirement needs the ExecutionProfile the run was assembled "
                "with; pass profile_factory so the retirement uses the real one")
        return TaskScheduler(reducer, profile)

    return factory
