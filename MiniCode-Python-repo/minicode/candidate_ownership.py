"""Per-candidate writer ownership plus a global execution-capacity ceiling.

This module is the ownership/capacity primitive that a multi-candidate Runtime
needs once the single non-keyed writer lock is removed.  It provides three
things and nothing else:

* :class:`CandidateRegistry` - at most one *live* writer lease per candidate,
  a per-candidate fencing token that is strictly monotonic and never reused,
  and a *short* per-candidate mutation lock that a lease owner (or the Runtime
  patch path) can take to mutate one candidate while other candidates keep
  running.
* :class:`CandidateLease` - the immutable value object that names the single
  writer of a candidate.
* :class:`ExecutionCapacity` - a global ledger that counts *queued or starting*
  work as an occupied slot, so a batch of callers released by one fence cannot
  all observe "0 active" and over-admit itself.

The module imports no scheduler, no runtime state machine and no other
``minicode.runtime_*`` module: it is a leaf primitive.

What this module can prove
==========================

* Only one live lease exists per candidate at any instant, and each issuance of
  a lease for a candidate carries a strictly greater fencing token than every
  token that candidate has ever issued, so a superseded writer is detectable
  after a re-claim.
* ``mutation`` gives mutual exclusion per candidate: two entrants - including
  two ``as_runtime=True`` entrants - can never be inside the lock for the same
  candidate at the same time, while two *different* candidates can be inside
  their own locks simultaneously.
* The registry never acquires a candidate mutation lock while holding its
  internal bookkeeping lock, so a caller waiting for a mutation lock cannot
  block ``claim``/``renew``/``release``/``lease_for``/``snapshot`` for every
  other candidate.  (The reverse order - a held mutation lock briefly taking
  the bookkeeping lock to re-confirm ownership - is used and is acyclic.)
* A capacity slot is occupied at ``reserve`` time, before any work starts, and
  is freed exactly once by ``release`` - including for work that is cancelled
  while still queued.
* ``scope_allows`` is a purely lexical decision: it never touches the
  filesystem, so it cannot be fooled by a symlink, a mount point or a race with
  a concurrent rename, and it equally cannot confirm that a path exists.

What this module cannot prove
=============================

* It cannot prove that a *caller* keeps the mutation lock short.  The module
  guarantees that taking the lock does not require any bookkeeping lock and
  that it is exclusive and non-blocking at ``timeout=0.0``; whether a caller
  holds it across a provider round-trip is a property of the caller, not of
  this primitive.
* It cannot prove anything about another process, another interpreter or a
  crashed writer: leases live in memory, and ``release`` is an orderly
  hand-back, not a lease expiry.  Recovery from a dead process is the caller's
  problem.
* It cannot prove that a caller *obeys* ``scope_allows``.  The check is offered
  at the mutation boundary; enforcing it on every write is the writer's job.
* It cannot prove that a patch, revision or file content is correct.  Neither
  ``base_revision`` nor ``write_scope`` is verified against the filesystem.
* It cannot prove fairness.  A mutation lock is exclusive and is *not* a queue:
  a refused entrant may lose the next race as well.

Ownership tokens
================

:attr:`CandidateLease.owner_token` is the capability a caller must present to
release a lease, take its mutation lock or pass its scope check.  It is derived
with a process-local pepper that is never persisted, so a token cannot be
recomputed from a serialised lease - "unforgeable-enough" for an in-process
capability string, and explicitly not a cryptographic cross-process guarantee.
``CandidateLease.to_dict`` and :meth:`CandidateRegistry.snapshot` therefore
never contain it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any


CANDIDATE_SCHEMA = "minicode-candidate-ownership-v1"

# Process-local pepper for owner tokens.  Immutable bytes, never persisted and
# never logged; see the module docstring.
_OWNER_TOKEN_PEPPER = secrets.token_bytes(32)

_CAPACITY_SCHEMA = "minicode-execution-capacity-v1"
_CAPACITY_KINDS = ("read", "write")


class CandidateConflict(RuntimeError):
    """Raised when a caller would break single-writer candidate ownership.

    It covers both halves of the rule: another writer already holds the
    candidate, and the caller's ``owner_token`` is not the current owner (which
    includes being refused the per-candidate mutation lock, because that lock
    is exclusive rather than a queue).
    """


class CapacityExhausted(RuntimeError):
    """Raised when a reservation would exceed a worker or writer ceiling."""


class CandidateBusy(CandidateConflict):
    """The mutex is occupied; ownership itself has not been refused."""


def _require_text(value: object, field: str) -> str:
    """Return ``value`` when it is a non-blank string, else raise."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("generation must be an integer >= 1")
    return value


def _require_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("timeout must be a non-negative number of seconds")
    return float(value)


def _require_limit(value: object, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _lexical_parts(value: object) -> tuple[str, ...] | None:
    """Split a relative path into parts, or return ``None`` when unsafe.

    The check is purely lexical and never touches the filesystem.  Rejected:
    empty values, NUL bytes, backslashes, POSIX-absolute paths, UNC prefixes,
    drive-letter prefixes (``C:``, ``C:/x``) and any ``..`` segment anywhere in
    the path.  ``.`` and empty segments (``a//b``) are dropped, because they do
    not change which file the path names.
    """

    if not isinstance(value, str) or not value:
        return None
    if "\x00" in value or "\\" in value or value.startswith("/"):
        return None
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return None
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return tuple(parts)


def _require_scope(write_scope: object) -> tuple[str, ...]:
    """Validate a ``write_scope`` argument and return it verbatim.

    The tuple is stored exactly as given (order and spelling preserved) so a
    lease reports what its writer asked for; matching normalises both sides.
    """

    if isinstance(write_scope, (str, bytes)) or not isinstance(write_scope, Sequence):
        raise ValueError("write_scope must be a sequence of relative path prefixes")
    scope: list[str] = []
    for entry in write_scope:
        if not isinstance(entry, str) or _lexical_parts(entry) is None:
            raise ValueError(
                "write_scope entries must be relative path prefixes without "
                f"backslashes, drive prefixes or '..': {entry!r}"
            )
        scope.append(entry)
    return tuple(scope)


def _tokens_match(expected: str, candidate: object) -> bool:
    """Compare an owner token without leaking length/prefix timing."""

    if not isinstance(candidate, str):
        return False
    try:
        return hmac.compare_digest(expected, candidate)
    except TypeError:  # non-ASCII candidate: compare_digest refuses str input
        return False


@dataclass(frozen=True, slots=True)
class CandidateLease:
    """One writer's claim on one candidate.

    ``to_dict`` is JSON-safe and deliberately omits :attr:`owner_token`: that
    string is the capability a caller presents to release the lease, take its
    mutation lock or pass its scope check, so serialising it would spread the
    capability into snapshots, logs and transcripts.
    """

    candidate_id: str
    owner_node_id: str
    owner_attempt_id: str
    owner_kind: str
    generation: int
    fencing_token: int
    base_revision: str
    write_scope: tuple[str, ...]
    created_at: float
    schema_version: str = CANDIDATE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema_version,
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "owner_node_id": self.owner_node_id,
            "owner_attempt_id": self.owner_attempt_id,
            "owner_kind": self.owner_kind,
            "generation": self.generation,
            "fencing_token": self.fencing_token,
            "base_revision": self.base_revision,
            "write_scope": list(self.write_scope),
            "created_at": self.created_at,
        }

    @property
    def owner_token(self) -> str:
        """Stable capability string for this lease; never serialised.

        The payload is the lease's stable identity (candidate, owner, attempt,
        generation, fencing token) - never ``created_at`` - so a renewed lease
        keeps the same token and the caller's capability does not churn.
        """

        payload = "\x1f".join(
            (
                self.candidate_id,
                self.owner_node_id,
                self.owner_attempt_id,
                self.owner_kind,
                str(self.generation),
                str(self.fencing_token),
            )
        )
        return hmac.new(
            _OWNER_TOKEN_PEPPER, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()


class _MutationGuard:
    """Single-use short-lock guard returned by :meth:`CandidateRegistry.mutation`.

    Acquisition order is fixed: the bookkeeping lock is taken briefly to
    validate ownership and fetch the per-candidate lock, released, and only
    then is the mutation lock acquired.  The registry therefore never holds its
    bookkeeping lock while waiting for a mutation lock.
    """

    def __init__(
        self,
        registry: CandidateRegistry,
        *,
        candidate_id: str,
        owner_token: str | None,
        as_runtime: bool,
        timeout: float,
        canceled: Callable[[], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._candidate_id = candidate_id
        self._owner_token = owner_token
        self._as_runtime = as_runtime
        self._timeout = timeout
        self._canceled = canceled
        self._lock: threading.Lock | None = None
        self._held = False
        self._used = False

    @property
    def acquired(self) -> bool:
        """Whether this guard currently holds the candidate's mutation lock."""

        return self._held

    def __enter__(self) -> CandidateLease | None:
        if self._used:
            raise RuntimeError("mutation guard is single-use; take a fresh lock")
        self._used = True
        if self._canceled is not None and self._canceled():
            raise CandidateConflict("candidate mutation canceled before acquiring the lock")
        lock, expected_token = self._registry._prepare_mutation(
            candidate_id=self._candidate_id,
            owner_token=self._owner_token,
            as_runtime=self._as_runtime,
        )
        if self._timeout <= 0.0:
            acquired = lock.acquire(blocking=False)
        else:
            deadline = time.monotonic() + self._timeout
            while True:
                if self._canceled is not None and self._canceled():
                    raise CandidateConflict("candidate mutation canceled while waiting for the lock")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    acquired = False
                    break
                acquired = lock.acquire(timeout=min(0.1, remaining))
                if acquired:
                    break
        if not acquired:
            raise CandidateBusy(
                f"mutation lock for candidate {self._candidate_id!r} is held "
                "elsewhere; the bounded lock wait expired without writing"
            )
        self._lock, self._held = lock, True
        try:
            if self._canceled is not None and self._canceled():
                raise CandidateConflict("candidate mutation canceled before confirming the acquired lock")
            return self._registry._confirm_mutation(
                candidate_id=self._candidate_id,
                expected_fencing_token=expected_token,
                as_runtime=self._as_runtime,
            )
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        lock, self._lock, self._held = self._lock, None, False
        if lock is not None:
            lock.release()


class CandidateRegistry:
    """In-memory single-writer leases with short per-candidate mutation locks.

    Every public method is safe to call from several threads.  The internal
    bookkeeping lock is only ever held for dictionary-sized work.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        """Create an empty registry.

        ``clock`` is an optional time source for ``created_at``; it defaults to
        :func:`time.time` and exists so a caller (including a test) can supply a
        real, monotonic-in-its-own-terms clock instead of patching the module.
        """

        self._clock: Callable[[], float] = clock or time.time
        self._book = threading.Lock()
        self._leases: dict[str, CandidateLease] = {}
        self._fencing_tokens: dict[str, int] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._quarantined: set[str] = set()

    def quarantine(self, candidate_id: str) -> None:
        """Revoke new writes without transferring the existing capability."""
        with self._book:
            self._quarantined.add(_require_text(candidate_id, "candidate_id"))

    def restore_fencing_floor(self, candidate_id: str, fencing_token: int) -> None:
        """Restore a monotonic counter only; old ownership never survives restart."""
        candidate = _require_text(candidate_id, "candidate_id")
        if type(fencing_token) is not int or fencing_token < 1:
            raise ValueError("restored fencing token must be positive")
        with self._book:
            if candidate in self._leases:
                raise CandidateConflict("cannot restore a fencing counter over live ownership")
            self._fencing_tokens[candidate] = max(self._fencing_tokens.get(candidate, 0), fencing_token)

    def claim(
        self,
        *,
        candidate_id: str,
        owner_node_id: str,
        owner_attempt_id: str,
        owner_kind: str,
        base_revision: str,
        write_scope: Sequence[str] = (),
        generation: int = 1,
    ) -> CandidateLease:
        """Claim the single writer lease of ``candidate_id``.

        The lease is keyed by ``(candidate_id, owner_node_id, owner_attempt_id,
        generation)``.  Repeating that exact key returns the original lease
        unchanged - same fencing token, same scope, same ``created_at`` - so a
        retried claim can never widen a live writer's rights.  Any other key for
        a candidate that already has a live lease raises
        :class:`CandidateConflict`.
        """

        candidate = _require_text(candidate_id, "candidate_id")
        node = _require_text(owner_node_id, "owner_node_id")
        attempt = _require_text(owner_attempt_id, "owner_attempt_id")
        kind = _require_text(owner_kind, "owner_kind")
        if not isinstance(base_revision, str):
            raise ValueError("base_revision must be a string")
        scope = _require_scope(write_scope)
        generation_value = _require_generation(generation)

        with self._book:
            if candidate in self._quarantined:
                raise CandidateConflict("candidate is quarantined or permanently retired")
            current = self._leases.get(candidate)
            if current is not None:
                if (
                    current.owner_node_id == node
                    and current.owner_attempt_id == attempt
                    and current.generation == generation_value
                ):
                    return current
                raise CandidateConflict(
                    f"candidate {candidate!r} is already owned by "
                    f"{current.owner_node_id!r}/{current.owner_attempt_id!r}"
                    f"@generation {current.generation}; "
                    "one live writer per candidate"
                )
            fencing_token = self._fencing_tokens.get(candidate, 0) + 1
            lease = CandidateLease(
                candidate_id=candidate,
                owner_node_id=node,
                owner_attempt_id=attempt,
                owner_kind=kind,
                generation=generation_value,
                fencing_token=fencing_token,
                base_revision=base_revision,
                write_scope=scope,
                created_at=float(self._clock()),
            )
            self._fencing_tokens[candidate] = fencing_token
            self._leases[candidate] = lease
            return lease

    def renew(self, lease: CandidateLease) -> CandidateLease:
        """Re-issue the caller's own lease with a fresh ``created_at``.

        The fencing token, owner identity, revision and scope are copied from
        the *stored* lease, never from the argument, so a caller cannot widen
        its rights by handing in a doctored lease object.
        """

        if not isinstance(lease, CandidateLease):
            raise TypeError("renew requires a CandidateLease")
        with self._book:
            current = self._leases.get(lease.candidate_id)
            if current is None or not _tokens_match(current.owner_token, lease.owner_token):
                raise CandidateConflict(
                    f"cannot renew candidate {lease.candidate_id!r}: the presented "
                    "owner token is not the current owner"
                )
            renewed = replace(current, created_at=float(self._clock()))
            self._leases[lease.candidate_id] = renewed
            return renewed

    def release(self, *, candidate_id: str, owner_token: str) -> bool:
        """Drop the lease when, and only when, the token is the current owner.

        Returns ``True`` when this call ended the lease, ``False`` for a wrong
        or superseded token, an unknown candidate, or a repeated release.  A
        failed release never disturbs the standing lease.  ``release`` is not a
        revoke: a mutation already inside the lock keeps running.
        """

        with self._book:
            current = self._leases.get(candidate_id)
            if current is None or not _tokens_match(current.owner_token, owner_token):
                return False
            del self._leases[candidate_id]
            return True

    def lease_for(self, candidate_id: str) -> CandidateLease | None:
        """Return the live lease of ``candidate_id``, or ``None``."""

        with self._book:
            return self._leases.get(candidate_id)

    def owned_by(self, candidate_id: str, owner_token: str) -> bool:
        """Return whether ``owner_token`` is the live owner of ``candidate_id``."""

        with self._book:
            current = self._leases.get(candidate_id)
            if current is None:
                return False
            return _tokens_match(current.owner_token, owner_token)

    def mutation(
        self,
        *,
        candidate_id: str,
        owner_token: str | None = None,
        as_runtime: bool = False,
        timeout: float = 0.0,
        canceled: Callable[[], bool] | None = None,
    ) -> _MutationGuard:
        """Return the short per-candidate mutation lock as a context manager.

        The lock must not be held across a model call: enter it, apply the
        mutation, leave it.  ``timeout=0.0`` never blocks - a second concurrent
        entrant raises :class:`CandidateConflict` immediately - and a positive
        timeout waits at most that long for the lock, still never while holding
        the registry's bookkeeping lock.

        ``owner_token`` must be the current owner's token.  ``as_runtime=True``
        is the Runtime patch path: the Runtime is not the writer, so it presents
        no token and may take the lock anyway.  It is still the *same* exclusive
        lock, so two ``as_runtime`` entrants exclude each other and a runtime
        entrant cannot enter while the writer holds it.

        The context manager yields the candidate's live lease at the moment the
        lock was taken (``None`` when the Runtime takes the lock of a candidate
        nobody owns).
        """

        candidate = _require_text(candidate_id, "candidate_id")
        if not as_runtime and not isinstance(owner_token, str):
            raise CandidateConflict(
                f"candidate {candidate!r}: an owner token is required to mutate; "
                "use as_runtime=True for the Runtime patch path"
            )
        return _MutationGuard(
            self,
            candidate_id=candidate,
            owner_token=owner_token,
            as_runtime=as_runtime,
            timeout=_require_timeout(timeout),
            canceled=canceled,
        )

    def scope_allows(self, candidate_id: str, owner_token: str, path: str) -> bool:
        """Return whether ``path`` is inside the lease's ``write_scope``.

        Purely lexical, fail-closed and never filesystem-touching: a foreign or
        unknown owner gets ``False``, an empty scope allows the whole candidate,
        and absolute paths, drive/UNC prefixes, ``..`` segments and backslashes
        are refused even when the scope is empty.
        """

        if not isinstance(path, str):
            return False
        with self._book:
            current = self._leases.get(candidate_id)
            if current is None or not _tokens_match(current.owner_token, owner_token):
                return False
            scope = current.write_scope
        parts = _lexical_parts(path)
        if parts is None or not parts:
            return False
        if not scope:
            return True
        for entry in scope:
            prefix = _lexical_parts(entry)
            if prefix and parts[: len(prefix)] == prefix:
                return True
        return False

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe view of the live leases; never the capabilities.

        Reports ``schema``, ``leases`` (one serialised entry per live lease,
        ordered by candidate id), ``candidate_count`` and ``writers`` - exactly
        one writer per owned candidate.  No owner token is included.
        """

        with self._book:
            candidate_ids = sorted(self._leases)
            leases = [self._leases[key].to_dict() for key in candidate_ids]
            quarantined = sorted(self._quarantined)
        return {
            "schema": CANDIDATE_SCHEMA,
            "leases": leases,
            "candidate_count": len(candidate_ids),
            "writers": {key: 1 for key in candidate_ids},
            **({"quarantined": quarantined} if quarantined else {}),
        }

    def _prepare_mutation(
        self,
        *,
        candidate_id: str,
        owner_token: str | None,
        as_runtime: bool,
    ) -> tuple[threading.Lock, int | None]:
        """Validate ownership and fetch the lock, holding ``_book`` briefly.

        Returns the candidate's mutation lock and the fencing token ownership
        was validated against.  Never acquires the mutation lock here, so a
        waiting entrant cannot block the registry's bookkeeping.
        """

        with self._book:
            current = self._leases.get(candidate_id)
            expected: int | None = None
            if not as_runtime and candidate_id in self._quarantined:
                raise CandidateConflict("candidate is quarantined or permanently retired")
            if not as_runtime:
                if current is None or owner_token is None or not _tokens_match(
                    current.owner_token, owner_token
                ):
                    raise CandidateConflict(
                        f"candidate {candidate_id!r}: the presented owner token is "
                        "not the current owner of the mutation lock"
                    )
                expected = current.fencing_token
            elif current is not None:
                expected = current.fencing_token
            lock = self._locks.get(candidate_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[candidate_id] = lock
            return lock, expected

    def _confirm_mutation(
        self,
        *,
        candidate_id: str,
        expected_fencing_token: int | None,
        as_runtime: bool,
    ) -> CandidateLease | None:
        """Re-check ownership after the lock was taken.

        A lease released or replaced while the caller waited means the caller
        lost the right to mutate; the guard releases the lock and the caller
        sees :class:`CandidateConflict` instead of a silent stale write.
        """

        with self._book:
            current = self._leases.get(candidate_id)
            if as_runtime:
                return current
            if candidate_id in self._quarantined:
                raise CandidateConflict("candidate is quarantined or permanently retired")
            if current is None or current.fencing_token != expected_fencing_token:
                raise CandidateConflict(
                    f"candidate {candidate_id!r}: ownership changed while waiting "
                    "for the mutation lock"
                )
            return current


@dataclass(frozen=True, slots=True)
class CapacityReservation:
    """One occupied slot in :class:`ExecutionCapacity`.

    ``sequence`` is the ledger's own issuance counter, so reservations can be
    ordered even though ``reservation_id`` is opaque.
    """

    reservation_id: str
    node_id: str
    kind: str
    candidate_id: str | None
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "node_id": self.node_id,
            "kind": self.kind,
            "candidate_id": self.candidate_id,
            "sequence": self.sequence,
        }


class ExecutionCapacity:
    """Global worker/writer ceiling that counts queued work as occupied.

    A reservation occupies its slot from :meth:`reserve` until
    :meth:`release`; there is no separate "started" transition, which is the
    point: work that is merely queued already holds capacity, so a batch of
    callers released by one fence cannot all read "0 active" and over-admit.
    Cancelling queued work is therefore just ``release`` of the reservation
    that was never used - the slot cannot leak.
    """

    def __init__(self, *, max_workers: int, max_writers: int) -> None:
        self._lock = threading.Lock()
        self._instance = secrets.token_hex(4)
        self._max_workers = _require_limit(max_workers, "max_workers", minimum=1)
        self._max_writers = _require_limit(max_writers, "max_writers", minimum=0)
        self._held: dict[str, CapacityReservation] = {}
        self._sequence = 0
        self._peak_workers = 0
        self._peak_writers = 0

    def reserve(
        self,
        *,
        node_id: str,
        kind: str,
        candidate_id: str | None = None,
    ) -> CapacityReservation:
        """Occupy a slot immediately or raise :class:`CapacityExhausted`.

        ``kind`` must be ``"read"`` or ``"write"``.  Every reservation consumes
        a worker slot; a write additionally consumes a writer slot.  The
        accounting happens under the ledger lock at reserve time, so concurrent
        callers cannot over-admit by observing stale counts.
        """

        node = _require_text(node_id, "node_id")
        if kind not in _CAPACITY_KINDS:
            raise ValueError(f"kind must be one of {_CAPACITY_KINDS}, got {kind!r}")
        if candidate_id is not None:
            _require_text(candidate_id, "candidate_id")

        with self._lock:
            if len(self._held) >= self._max_workers:
                raise CapacityExhausted(
                    f"worker ceiling reached: {len(self._held)}/{self._max_workers} "
                    f"slots occupied (node {node!r} was not admitted)"
                )
            if kind == "write" and self._active_writers_locked() >= self._max_writers:
                raise CapacityExhausted(
                    f"writer ceiling reached: {self._active_writers_locked()}"
                    f"/{self._max_writers} writers occupied (node {node!r} was "
                    "not admitted)"
                )
            self._sequence += 1
            reservation = CapacityReservation(
                reservation_id=f"cap-{self._instance}-{self._sequence:08d}",
                node_id=node,
                kind=kind,
                candidate_id=candidate_id,
                sequence=self._sequence,
            )
            self._held[reservation.reservation_id] = reservation
            self._peak_workers = max(self._peak_workers, len(self._held))
            self._peak_writers = max(self._peak_writers, self._active_writers_locked())
            return reservation

    def release(self, reservation: CapacityReservation | str) -> bool:
        """Free the slot held by ``reservation`` (or by its id).

        Returns ``True`` only for the call that actually freed a held slot;
        a repeated release, a foreign reservation, an unknown id or any other
        argument returns ``False`` and leaves the counters untouched.  Peaks are
        never reduced.
        """

        if isinstance(reservation, CapacityReservation):
            reservation_id = reservation.reservation_id
        elif isinstance(reservation, str):
            reservation_id = reservation
        else:
            return False
        with self._lock:
            return self._held.pop(reservation_id, None) is not None

    def set_limits(
        self,
        *,
        max_workers: int | None = None,
        max_writers: int | None = None,
    ) -> None:
        """Change the ceilings, refusing to undercut work that already holds a slot.

        Lowering a ceiling below current usage raises :class:`ValueError` - a
        programming error, not a silent overrun - and neither limit is applied
        when either one is rejected.  ``max_writers`` above ``max_workers`` is
        accepted (it can only ever admit fewer writers than the worker ceiling).
        """

        workers = (
            None
            if max_workers is None
            else _require_limit(max_workers, "max_workers", minimum=1)
        )
        writers = (
            None
            if max_writers is None
            else _require_limit(max_writers, "max_writers", minimum=0)
        )
        with self._lock:
            if workers is not None and workers < len(self._held):
                raise ValueError(
                    f"max_workers={workers} is below the {len(self._held)} slots "
                    "already occupied"
                )
            if writers is not None and writers < self._active_writers_locked():
                raise ValueError(
                    f"max_writers={writers} is below the "
                    f"{self._active_writers_locked()} writers already occupied"
                )
            if workers is not None:
                self._max_workers = workers
            if writers is not None:
                self._max_writers = writers

    @property
    def max_workers(self) -> int:
        with self._lock:
            return self._max_workers

    @property
    def max_writers(self) -> int:
        with self._lock:
            return self._max_writers

    @property
    def occupied(self) -> int:
        """Slots currently held, queued work included."""

        with self._lock:
            return len(self._held)

    @property
    def active_writers(self) -> int:
        """Currently held ``"write"`` reservations; reads never count."""

        with self._lock:
            return self._active_writers_locked()

    @property
    def peak_workers(self) -> int:
        """Monotonic high-water mark of :attr:`occupied`."""

        with self._lock:
            return self._peak_workers

    @property
    def peak_writers(self) -> int:
        """Monotonic high-water mark of :attr:`active_writers`."""

        with self._lock:
            return self._peak_writers

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe view of the ledger, ordered by reservation id."""

        with self._lock:
            reservation_ids = sorted(self._held)
            return {
                "schema": _CAPACITY_SCHEMA,
                "max_workers": self._max_workers,
                "max_writers": self._max_writers,
                "occupied": len(self._held),
                "active_writers": self._active_writers_locked(),
                "peak_workers": self._peak_workers,
                "peak_writers": self._peak_writers,
                "reservations": [self._held[key].to_dict() for key in reservation_ids],
            }

    def _active_writers_locked(self) -> int:
        """Count held write reservations; caller must hold ``self._lock``."""

        return sum(1 for held in self._held.values() if held.kind == "write")
