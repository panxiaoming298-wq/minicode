"""Per-edge dependency waits and a persistent, idempotent result mailbox.

This module is the persistence-free core of per-edge dependency wakeups.  Today
the runtime resumes a suspended primary only when *every* child is terminal
(``runtime_state.py::_wake_continuations`` applies one ``all(children terminal)``
batch barrier).  A :class:`WaitSpec` replaces that batch barrier with declared
``all``/``any`` edge sets plus ``required``/``optional`` edges, so a primary that
only needs child ``A`` can resume as soon as ``A``'s patch is applied and
checked while ``B`` keeps running.  Results travel through
:class:`ResultMailbox`, an append-only inbox consumed at a safe protocol
boundary.

What this module CAN prove (offline, in-process)

* Predicate truth for one published :class:`TaskOutcome`: which of
  ``artifact_validated``, ``patch_applied_and_checked`` and
  ``terminal_observed`` it satisfies, and that the three are never conflated.
* Wait decisions for one :class:`WaitSpec`: ``all``/``any`` over the *declared*
  edge set only, required/optional failure handling, fail-closed timeout and
  cancel-epoch behaviour, and generation gating of stale child results.
* Mailbox mechanics: publish order, ``event_id`` idempotency, integer cursor
  reads, and resume accounting that can never exceed one resume per
  ``(wait_id, generation)``.
* Wire round-tripping of both value objects and rejection of unknown schema
  versions.
* Thread safety of publication (asserted with real threads and a barrier).

What this module CANNOT prove

* That the runtime actually suspends and resumes the primary at a safe
  boundary, or that a resumed primary reads the mailbox.  No scheduler, no
  reducer, no runtime state machine, no session or ledger is imported here.
* Durability.  Nothing is written to disk or to a store: ``register_wait``
  remembers a spec for the lifetime of this process only.  A deployment must
  persist :meth:`WaitSpec.to_dict` and the cursor the decision reports.
* Delivery.  The mailbox is coordination, not exactly-once transport;
  idempotency is only as good as the caller-supplied ``event_id``.
* Time.  ``created_at`` and the ``now`` of an evaluation come from the caller,
  so timeout decisions are only as truthful as the clock the caller passes.
* Global ordering.  Only arrival order inside one mailbox is defined; a task's
  authoritative order is its protocol ``sequence``.
* Quality.  A satisfied predicate reports a protocol fact, never that the
  artifact or the patch is correct.

Decision vocabulary (returned by :meth:`ResultMailbox.evaluate`,
:meth:`ResultMailbox.register_wait` and :meth:`ResultMailbox.consume`):
``status`` is ``"satisfied"``, ``"waiting"`` or ``"blocked"``; ``reason`` is
``"satisfied"``, ``"pending"``, ``"failed_dependency"``, ``"timeout"`` or
``"canceled"``. The explicit agent return policy also allows ``"timed_out"``
to resume, with ``satisfied`` false and the child still running. ``failed_task_ids`` always names
every declared edge whose task is failed or canceled and whose predicate the
outcome cannot satisfy; ``blocking_task_ids`` names the failed tasks that
actually produced a ``"failed_dependency"`` block, so it is empty for a
satisfied decision even when a declared edge failed (``"any"`` was satisfied by
another edge); ``tolerated_task_ids`` names the failed optional edges a
``"continue"`` policy proceeded without.  Each entry of the ``edges`` tuple
carries the same facts per edge (``satisfied``, ``failed``, ``tolerated``,
``blocking``, ``pending``) plus the observed outcome's terminal, generation and
event id.

Evaluation is history-based and independent per wait.  Every outcome ever
published remains visible to every wait whose own ``generation`` and
``cancel_epoch`` gating admits it -- registering, evaluating or consuming one
wait never changes another wait's view, and ``publish``/``cursor`` are append
only and never touched by evaluation.  Generation gating is therefore also per
wait, and it is reported rather than hidden: ``gated_by_generation`` with a
non-empty ``gated_generations`` (and ``gated_task_ids`` at decision level) means
the child did report, but only for an older attempt than this wait is about, so
an edge that reports no ``event_id`` is not mistaken for a child that never
reported.  The same reasoning applies to a wait whose ``generation`` is higher
than the attempt it means to observe: raise the child's generation, not the
wait's, when a fresh attempt must satisfy an existing wait.

Failure policy.  ``"block"`` blocks on any failed declared edge.  ``"continue"``
blocks only on failed edges that are themselves ``required``; a failed
``required=False`` edge is tolerated, which means it stops being part of the
satisfaction requirement (so an ``all`` wait whose every edge failed optionally
is vacuously satisfied, while an ``any`` wait still needs one observed
predicate).  A failed edge never satisfies anything by itself: only
``terminal_observed`` is ever satisfied by a failed or canceled task.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

WAIT_SCHEMA = "minicode-dependency-wait-v1"
RETURN_WAIT_SCHEMA = "minicode-agent-wait-v1"
DEFAULT_AGENT_WAIT_SECONDS = 60
MAILBOX_SCHEMA = "minicode-result-mailbox-v1"

ARTIFACT_VALIDATED = "artifact_validated"
PATCH_APPLIED_AND_CHECKED = "patch_applied_and_checked"
TERMINAL_OBSERVED = "terminal_observed"
PREDICATES = (ARTIFACT_VALIDATED, PATCH_APPLIED_AND_CHECKED, TERMINAL_OBSERVED)
MODES = ("all", "any")
FAILURE_POLICIES = ("block", "continue")

_TERMINAL_FAILURES = frozenset({"failed", "canceled"})
_CANCELED = "canceled"
_MODE_ALL = "all"
_POLICY_CONTINUE = "continue"

_STATUS_SATISFIED = "satisfied"
_STATUS_WAITING = "waiting"
_STATUS_BLOCKED = "blocked"
_REASON_SATISFIED = "satisfied"
_REASON_PENDING = "pending"
_REASON_FAILED_DEPENDENCY = "failed_dependency"
_REASON_TIMEOUT = "timeout"
_REASON_CANCELED = "canceled"

_WAIT_FIELDS = frozenset({
    "timeout_policy",
    "wait_id",
    "generation",
    "owner_node_id",
    "edges",
    "mode",
    "failure_policy",
    "timeout_seconds",
    "cancel_epoch",
    "consumed_cursor",
    "checkpoint_ref",
    "created_at",
    "schema_version",
})
_WAIT_REQUIRED_FIELDS = frozenset({"wait_id", "generation", "owner_node_id", "edges"})
_EDGE_FIELDS = frozenset({"task_id", "predicate", "required", "generation"})
_OUTCOME_FIELDS = frozenset({
    "task_id",
    "generation",
    "candidate_id",
    "revision",
    "artifact_sha256",
    "patch_applied",
    "checked",
    "terminal",
    "event_id",
    "sequence",
    "cause_event_id",
    "schema_version",
})
_OUTCOME_REQUIRED_FIELDS = frozenset({
    "task_id",
    "generation",
    "candidate_id",
    "revision",
    "artifact_sha256",
    "patch_applied",
    "checked",
    "terminal",
    "event_id",
    "sequence",
})


class WaitError(ValueError):
    """Malformed wait spec, malformed outcome, or unknown schema version."""


class WaitBlocked(RuntimeError):
    """A required edge failed and the failure policy blocks the resume.

    :class:`ResultMailbox` reports blocking through decision dicts so the caller
    keeps the concrete failing task ids; a scheduler that refuses to resume
    raises this error with that decision attached.
    """

    def __init__(self, message: str, decision: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.decision: dict[str, Any] | None = None if decision is None else dict(decision)


def _plain_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WaitError(f"{field} must be an integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    number = _plain_int(value, field)
    if number < 0:
        raise WaitError(f"{field} must not be negative")
    return number


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WaitError(f"{field} must be a string")
    if not allow_empty and not value:
        raise WaitError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field, allow_empty=True)


def _predicate_holds(predicate: str, outcome: TaskOutcome) -> bool:
    """Whether one published outcome satisfies one dependency predicate.

    The three predicates are deliberately not conflations of each other:
    ``terminal_observed`` accepts any terminal outcome (including failures),
    while the two success predicates require their own protocol facts and
    refuse a canceled task.
    """

    if predicate == TERMINAL_OBSERVED:
        return outcome.terminal is not None
    if predicate == ARTIFACT_VALIDATED:
        return (
            outcome.artifact_sha256 is not None
            and bool(outcome.revision)
            and outcome.terminal not in {_CANCELED, "failed"}
        )
    if predicate == PATCH_APPLIED_AND_CHECKED:
        return (
            outcome.patch_applied is True
            and outcome.checked is True
            and bool(outcome.revision)
            and outcome.terminal not in {_CANCELED, "failed"}
        )
    raise WaitError(f"unknown dependency predicate: {predicate!r}")


def _failure_blocks(policy: str, edge: DependencyEdge) -> bool:
    """Whether a failed edge blocks under this policy.

    ``"block"`` blocks on any failed declared edge; ``"continue"`` blocks only
    on failed edges that are themselves required, tolerating an optional one.
    """

    if policy == _POLICY_CONTINUE:
        return edge.required
    return True


def _collapse_edges(edges: tuple[DependencyEdge, ...]) -> tuple[DependencyEdge, ...]:
    """Collapse duplicate ``(task_id, predicate)`` edges without double counting.

    Two edges on one task with different predicates stay distinct.  Identical
    edges collapse to one; when duplicates disagree about ``required`` the
    strictest wins so a declaration can never silently weaken itself.
    """

    order: list[tuple[str, str]] = []
    collapsed: dict[tuple[str, str], DependencyEdge] = {}
    for edge in edges:
        key = (edge.task_id, edge.predicate)
        existing = collapsed.get(key)
        if existing is None:
            order.append(key)
            collapsed[key] = edge
        elif edge.required and not existing.required:
            collapsed[key] = edge
    return tuple(collapsed[key] for key in order)


def _task_ids(reports: Sequence[Mapping[str, Any]], field: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for report in reports:
        if report[field]:
            seen.setdefault(str(report["task_id"]), None)
    return tuple(seen)


def _declared_task_ids(reports: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for report in reports:
        seen.setdefault(str(report["task_id"]), None)
    return tuple(seen)


def _edge_from_dict(payload: Mapping[str, Any]) -> DependencyEdge:
    if not isinstance(payload, Mapping):
        raise WaitError("dependency edge payload must be a mapping")
    unknown = set(payload) - _EDGE_FIELDS
    if unknown:
        raise WaitError(f"unknown dependency edge fields: {sorted(unknown)}")
    missing = {"task_id", "predicate"} - set(payload)
    if missing:
        raise WaitError(f"missing dependency edge fields: {sorted(missing)}")
    return DependencyEdge(
        task_id=payload["task_id"],
        predicate=payload["predicate"],
        required=payload.get("required", True),
        generation=None if payload.get("generation") is None else payload["generation"],
    )


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """One declared edge: how the wait observes one child task.

    ``generation`` belongs to the EDGE, not to the whole wait. Fencing every edge
    with one shared number -- the minimum over all declared tasks -- means a task
    whose generation has moved on still reads its own older result as fresh, so a
    superseded attempt can satisfy an edge it should have been fenced out of.
    """

    task_id: str
    predicate: str
    required: bool = True
    generation: int | None = None

    def __post_init__(self) -> None:
        _text(self.task_id, "dependency edge task_id")
        if self.predicate not in PREDICATES:
            raise WaitError(f"unknown dependency predicate: {self.predicate!r}")
        if not isinstance(self.required, bool):
            raise WaitError("dependency edge required must be a boolean")
        if self.generation is not None and (
                isinstance(self.generation, bool) or not isinstance(self.generation, int)
                or self.generation < 1):
            raise WaitError("dependency edge generation must be an integer >= 1 when supplied")

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "predicate": self.predicate, "required": self.required,
                **({} if self.generation is None else {"generation": self.generation})}


@dataclass(frozen=True, slots=True)
class WaitSpec:
    """Declared per-edge dependency set for one suspended owner node.

    ``generation`` fences the wait against results of an older child attempt,
    ``cancel_epoch`` lets a wait registered before a cancellation fail closed,
    ``consumed_cursor`` is the mailbox position the owner already acknowledged,
    and ``checkpoint_ref`` names the durable checkpoint the owner would resume
    from.  A ``timeout_seconds`` wait that is not satisfied by ``created_at +
    timeout_seconds`` is blocked rather than left hanging.
    """

    wait_id: str
    generation: int
    owner_node_id: str
    edges: tuple[DependencyEdge, ...]
    mode: str = "all"
    failure_policy: str = "block"
    timeout_seconds: float | None = None
    cancel_epoch: int = 0
    consumed_cursor: int = 0
    checkpoint_ref: str | None = None
    created_at: float = 0.0
    schema_version: str = WAIT_SCHEMA
    timeout_policy: str = "block"

    def __post_init__(self) -> None:
        if self.schema_version not in {WAIT_SCHEMA, RETURN_WAIT_SCHEMA}:
            raise WaitError(f"unsupported dependency wait schema: {self.schema_version!r}")
        if (self.timeout_policy not in {"block", "return"}
            or (self.schema_version == RETURN_WAIT_SCHEMA) != (self.timeout_policy == "return")):
            raise WaitError("return-on-timeout requires the agent wait schema")
        _text(self.wait_id, "dependency wait wait_id")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int) or self.generation < 1:
            raise WaitError("dependency wait generation must be an integer >= 1")
        _text(self.owner_node_id, "dependency wait owner_node_id", allow_empty=True)
        if self.mode not in MODES:
            raise WaitError(f"unknown dependency wait mode: {self.mode!r}")
        if self.failure_policy not in FAILURE_POLICIES:
            raise WaitError(f"unknown dependency wait failure_policy: {self.failure_policy!r}")
        edges = self.edges
        if not isinstance(edges, tuple):
            try:
                edges = tuple(edges)
            except TypeError as exc:
                raise WaitError("dependency wait edges must be a sequence of DependencyEdge") from exc
            object.__setattr__(self, "edges", edges)
        if not edges:
            raise WaitError("dependency wait requires at least one edge")
        for edge in edges:
            if not isinstance(edge, DependencyEdge):
                raise WaitError("dependency wait edges must be DependencyEdge values")
            if edge.predicate not in PREDICATES:
                raise WaitError(f"unknown dependency predicate: {edge.predicate!r}")
        timeout = self.timeout_seconds
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise WaitError("dependency wait timeout_seconds must be a number or None")
            if timeout <= 0:
                raise WaitError("dependency wait timeout_seconds must be positive")
        _non_negative_int(self.cancel_epoch, "dependency wait cancel_epoch")
        _non_negative_int(self.consumed_cursor, "dependency wait consumed_cursor")
        _optional_text(self.checkpoint_ref, "dependency wait checkpoint_ref")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)):
            raise WaitError("dependency wait created_at must be a number")

    def to_dict(self) -> dict[str, Any]:
        return {
            "wait_id": self.wait_id,
            "generation": self.generation,
            "owner_node_id": self.owner_node_id,
            "edges": [edge.to_dict() for edge in self.edges],
            "mode": self.mode,
            "failure_policy": self.failure_policy,
            "timeout_seconds": self.timeout_seconds,
            "cancel_epoch": self.cancel_epoch,
            "consumed_cursor": self.consumed_cursor,
            "checkpoint_ref": self.checkpoint_ref,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            **({"timeout_policy": self.timeout_policy} if self.timeout_policy != "block" else {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WaitSpec:
        if not isinstance(payload, Mapping):
            raise WaitError("dependency wait payload must be a mapping")
        version = payload.get("schema_version")
        if version not in {WAIT_SCHEMA, RETURN_WAIT_SCHEMA}:
            raise WaitError(f"unsupported dependency wait schema: {version!r}")
        unknown = set(payload) - _WAIT_FIELDS
        if unknown:
            raise WaitError(f"unknown dependency wait fields: {sorted(unknown)}")
        missing = _WAIT_REQUIRED_FIELDS - set(payload)
        if missing:
            raise WaitError(f"missing dependency wait fields: {sorted(missing)}")
        raw_edges = payload["edges"]
        if not isinstance(raw_edges, (list, tuple)):
            raise WaitError("dependency wait edges must be a sequence")
        edges = tuple(
            item if isinstance(item, DependencyEdge) else _edge_from_dict(item) for item in raw_edges
        )
        try:
            return cls(
                wait_id=payload["wait_id"],
                generation=payload["generation"],
                owner_node_id=payload["owner_node_id"],
                edges=edges,
                mode=payload.get("mode", "all"),
                failure_policy=payload.get("failure_policy", "block"),
                timeout_seconds=payload.get("timeout_seconds"),
                cancel_epoch=payload.get("cancel_epoch", 0),
                consumed_cursor=payload.get("consumed_cursor", 0),
                checkpoint_ref=payload.get("checkpoint_ref"),
                created_at=payload.get("created_at", 0.0),
                schema_version=version,
                timeout_policy=payload.get("timeout_policy", "block"),
            )
        except WaitError:
            raise
        except (TypeError, ValueError) as exc:
            raise WaitError(f"malformed dependency wait: {exc}") from exc


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """One child result as the mailbox stores it.

    ``terminal`` is ``"succeeded"``, ``"failed"``, ``"canceled"`` or ``None``
    while the task is still running.  Only ``"failed"`` and ``"canceled"`` mark
    an edge as a hard failure here; any other terminal vocabulary (for example
    a retryable failure) is treated as non-blocking and must be mapped by the
    caller before publishing.
    """

    task_id: str
    generation: int
    candidate_id: str
    revision: str
    artifact_sha256: str | None
    patch_applied: bool
    checked: bool
    terminal: str | None
    event_id: str
    sequence: int
    cause_event_id: str | None = None
    schema_version: str = MAILBOX_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != MAILBOX_SCHEMA:
            raise WaitError(f"unsupported result mailbox schema: {self.schema_version!r}")
        _text(self.task_id, "task outcome task_id")
        _text(self.event_id, "task outcome event_id")
        _text(self.candidate_id, "task outcome candidate_id", allow_empty=True)
        _text(self.revision, "task outcome revision", allow_empty=True)
        _optional_text(self.artifact_sha256, "task outcome artifact_sha256")
        _optional_text(self.terminal, "task outcome terminal")
        _optional_text(self.cause_event_id, "task outcome cause_event_id")
        if type(self.patch_applied) is not bool:
            raise WaitError("task outcome patch_applied must be a boolean")
        if type(self.checked) is not bool:
            raise WaitError("task outcome checked must be a boolean")
        # A stale attempt is reported with a lower generation than the wait, so
        # generation 0 must stay publishable here.
        _non_negative_int(self.generation, "task outcome generation")
        _non_negative_int(self.sequence, "task outcome sequence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "generation": self.generation,
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "artifact_sha256": self.artifact_sha256,
            "patch_applied": self.patch_applied,
            "checked": self.checked,
            "terminal": self.terminal,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "cause_event_id": self.cause_event_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TaskOutcome:
        if not isinstance(payload, Mapping):
            raise WaitError("task outcome payload must be a mapping")
        version = payload.get("schema_version")
        if version != MAILBOX_SCHEMA:
            raise WaitError(f"unsupported result mailbox schema: {version!r}")
        unknown = set(payload) - _OUTCOME_FIELDS
        if unknown:
            raise WaitError(f"unknown task outcome fields: {sorted(unknown)}")
        missing = _OUTCOME_REQUIRED_FIELDS - set(payload)
        if missing:
            raise WaitError(f"missing task outcome fields: {sorted(missing)}")
        try:
            return cls(
                task_id=payload["task_id"],
                generation=payload["generation"],
                candidate_id=payload["candidate_id"],
                revision=payload["revision"],
                artifact_sha256=payload["artifact_sha256"],
                patch_applied=payload["patch_applied"],
                checked=payload["checked"],
                terminal=payload["terminal"],
                event_id=payload["event_id"],
                sequence=payload["sequence"],
                cause_event_id=payload.get("cause_event_id"),
                schema_version=version,
            )
        except WaitError:
            raise
        except (TypeError, ValueError) as exc:
            raise WaitError(f"malformed task outcome: {exc}") from exc


@dataclass(frozen=True, slots=True)
class _EdgeLookup:
    """Private per-wait history lookup: best eligible outcome plus stale ones."""

    outcome: TaskOutcome | None
    gated_generations: tuple[int, ...]


class ResultMailbox:
    """Append-only, idempotent, cursor-consumed inbox for child results.

    The mailbox is process-local and thread-safe: publication is idempotent by
    ``event_id``, readers advance an integer cursor, and each
    ``(wait_id, generation)`` counts at most one resume no matter how many
    times a boundary consumes the same wait.

    Evaluation is history-based and per wait.  Every outcome ever published
    stays visible to every :class:`WaitSpec` whose own ``generation`` and
    ``cancel_epoch`` gating admits it, so registering, evaluating or consuming
    one wait can never change another wait's view, and neither ``publish`` nor
    ``cursor()`` is touched by evaluation.  Only ``_resumes`` is written, keyed
    by ``(wait_id, generation)``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: list[TaskOutcome] = []
        self._by_event: dict[str, TaskOutcome] = {}
        self._by_task: dict[str, list[TaskOutcome]] = {}
        self._waits: dict[tuple[str, int], WaitSpec] = {}
        self._resumes: dict[tuple[str, int], int] = {}

    def publish(self, outcome: TaskOutcome) -> bool:
        """Append one outcome, or report ``False`` when its event is a duplicate."""

        if not isinstance(outcome, TaskOutcome):
            raise WaitError("publish requires a TaskOutcome")
        with self._lock:
            if outcome.event_id in self._by_event:
                return False
            self._by_event[outcome.event_id] = outcome
            self._results.append(outcome)
            self._by_task.setdefault(outcome.task_id, []).append(outcome)
            return True

    def results_since(
        self,
        cursor: int,
        *,
        task_ids: Collection[str] | None = None,
    ) -> tuple[tuple[TaskOutcome, ...], int]:
        """Return results published at or after ``cursor`` plus the mailbox cursor.

        The returned cursor is the mailbox cursor, so it acknowledges everything
        published so far even when ``task_ids`` filtered the view; results are
        never lost because :meth:`all_results` and :meth:`latest_for` replay
        them.  Calling this again with the returned cursor yields an empty tuple
        until something new is published.
        """

        start = _non_negative_int(cursor, "cursor")
        wanted = None if task_ids is None else {str(task_id) for task_id in task_ids}
        with self._lock:
            current = len(self._results)
            window = self._results[min(start, current):]
            if wanted is None:
                selected = tuple(window)
            else:
                selected = tuple(item for item in window if item.task_id in wanted)
            return selected, current

    def all_results(self) -> tuple[TaskOutcome, ...]:
        with self._lock:
            return tuple(self._results)

    def cursor(self) -> int:
        with self._lock:
            return len(self._results)

    def latest_for(self, task_id: str) -> TaskOutcome | None:
        """Most recently published outcome for one task, or ``None``."""

        with self._lock:
            published = self._by_task.get(task_id)
            return None if not published else published[-1]

    def register_wait(
        self,
        wait: WaitSpec,
        *,
        now: float | None = None,
        cancel_epoch: int = 0,
    ) -> dict[str, Any]:
        """Remember a wait spec and report the decision it has right now.

        Registration itself never counts a resume: the resume gate stays in
        :meth:`consume` so the runtime resumes exactly once, at a boundary it
        chooses.  Registering after the satisfying results already exist is
        therefore the same as publishing after registration.
        """

        key = (wait.wait_id, wait.generation)
        with self._lock:
            already = key in self._waits
            self._waits[key] = wait
        decision = self.evaluate(wait, now=now, cancel_epoch=cancel_epoch)
        decision["registered"] = True
        decision["already_registered"] = already
        return decision

    def evaluate(
        self,
        wait: WaitSpec,
        *,
        now: float | None = None,
        cancel_epoch: int = 0,
    ) -> dict[str, Any]:
        """Decide a wait without touching resume accounting.

        ``now`` defaults to the wall clock and ``cancel_epoch`` is the caller's
        current cancellation epoch, so that a wait registered before a
        cancellation fails closed instead of resuming into a canceled run.
        """

        with self._lock:
            return self._evaluate_locked(wait, now=now, cancel_epoch=cancel_epoch)

    def consume(
        self,
        wait: WaitSpec,
        *,
        resume: bool = True,
        now: float | None = None,
        cancel_epoch: int = 0,
    ) -> dict[str, Any]:
        """Evaluate at a protocol boundary and count at most one resume.

        ``resume=False`` is a pure evaluation.  With ``resume=True`` the
        decision counts a resume only when ``resume_allowed`` is true and this
        ``(wait_id, generation)`` has not resumed yet, so duplicate or
        out-of-order publishes can never resume the owner twice.
        """

        decision = self.evaluate(wait, now=now, cancel_epoch=cancel_epoch)
        results, cursor = self.results_since(wait.consumed_cursor)
        allowed = bool(decision["resume_allowed"])
        key = (wait.wait_id, wait.generation)
        with self._lock:
            already_resumed = self._resumes.get(key, 0) > 0
            resumed = bool(resume and allowed and not already_resumed)
            if resumed:
                self._resumes[key] = 1
            total = sum(count for (wait_id, _generation), count in self._resumes.items() if wait_id == wait.wait_id)
        decision["results"] = results
        decision["cursor"] = cursor
        decision["next_cursor"] = cursor if allowed else wait.consumed_cursor
        decision["resumed"] = resumed
        decision["already_resumed"] = already_resumed
        decision["resumed_count"] = total
        return decision

    def resumed_count(self, wait_id: str) -> int:
        """Resumes counted so far for ``wait_id`` across its generations.

        Each ``(wait_id, generation)`` contributes at most one, so a single
        generation can never report more than one resume.
        """

        with self._lock:
            return sum(
                count for (candidate, _generation), count in self._resumes.items() if candidate == wait_id
            )

    def _edge_lookup_locked(self, task_id: str, generation: int) -> _EdgeLookup:
        """Freshest non-stale outcome for one task, plus the stale ones it saw.

        Staleness is per wait, never shared: an outcome whose generation is
        lower than *this* wait's generation belongs to a superseded attempt, so
        it is reported as gated rather than silently missing.  That lets a
        caller tell "the child never reported" apart from "the child reported
        for an older attempt than this wait is about".  Among eligible outcomes
        the highest protocol ``sequence`` wins, with arrival order as the
        tie-break.  Nothing here mutates mailbox state, so one wait's lookup can
        never change what another wait observes.
        """

        published = self._by_task.get(task_id)
        if not published:
            return _EdgeLookup(None, ())
        best: TaskOutcome | None = None
        best_key: tuple[int, int] | None = None
        gated: list[int] = []
        for arrival, outcome in enumerate(published):
            if outcome.generation < generation:
                gated.append(outcome.generation)
                continue
            key = (outcome.sequence, arrival)
            if best_key is None or key > best_key:
                best = outcome
                best_key = key
        return _EdgeLookup(best, tuple(dict.fromkeys(gated)))

    def _evaluate_locked(
        self,
        wait: WaitSpec,
        *,
        now: float | None,
        cancel_epoch: int,
    ) -> dict[str, Any]:
        current_now = time.time() if now is None else float(now)
        current_epoch = _plain_int(cancel_epoch, "cancel_epoch")
        reports: list[dict[str, Any]] = []
        for edge in _collapse_edges(wait.edges):
            # Each edge is fenced by its OWN generation; only an edge that did not
            # carry one falls back to the wait's.
            lookup = self._edge_lookup_locked(edge.task_id,
                                              edge.generation or wait.generation)
            outcome = lookup.outcome
            gated_generations = lookup.gated_generations
            if edge.generation is not None and outcome is not None and outcome.generation != edge.generation:
                gated_generations = (*gated_generations, outcome.generation)
                outcome = None
            satisfied = outcome is not None and _predicate_holds(edge.predicate, outcome)
            failed = (
                outcome is not None
                and not satisfied
                and outcome.terminal in _TERMINAL_FAILURES
            )
            tolerated = failed and not _failure_blocks(wait.failure_policy, edge)
            reports.append({
                "task_id": edge.task_id,
                "predicate": edge.predicate,
                "required": edge.required,
                "satisfied": satisfied,
                "failed": failed,
                "tolerated": tolerated,
                "blocking": failed and not tolerated,
                "pending": not satisfied and not tolerated,
                "gated_by_generation": outcome is None and bool(gated_generations),
                "gated_generations": gated_generations,
                "terminal": None if outcome is None else outcome.terminal,
                "outcome_generation": None if outcome is None else outcome.generation,
                "event_id": None if outcome is None else outcome.event_id,
                "sequence": None if outcome is None else outcome.sequence,
            })
        required_reports = [report for report in reports if not report["tolerated"]]
        if wait.mode == _MODE_ALL:
            satisfied = all(report["satisfied"] for report in required_reports)
        else:
            satisfied = any(report["satisfied"] for report in required_reports)
        blocking = [report for report in reports if report["blocking"]]
        timed_out = (
            wait.timeout_seconds is not None
            and (current_now - wait.created_at) >= wait.timeout_seconds
        )
        if current_epoch > wait.cancel_epoch and not (satisfied and any(report["tolerated"] for report in reports) and all(
                report["satisfied"] or report["tolerated"] for report in reports)):
            status, reason, resume_allowed = _STATUS_BLOCKED, _REASON_CANCELED, False
        elif satisfied:
            status, reason, resume_allowed = _STATUS_SATISFIED, _REASON_SATISFIED, True
        elif blocking:
            status, reason, resume_allowed = _STATUS_BLOCKED, _REASON_FAILED_DEPENDENCY, False
        elif timed_out:
            status, reason, resume_allowed = (("timed_out", _REASON_TIMEOUT, True)
                if wait.timeout_policy == "return" else (_STATUS_BLOCKED, _REASON_TIMEOUT, False))
        else:
            status, reason, resume_allowed = _STATUS_WAITING, _REASON_PENDING, False
        blocked_ids = (
            _task_ids(blocking, "blocking")
            if reason == _REASON_FAILED_DEPENDENCY
            else ()
        )
        return {
            "schema_version": WAIT_SCHEMA,
            "wait_id": wait.wait_id,
            "generation": wait.generation,
            "owner_node_id": wait.owner_node_id,
            "mode": wait.mode,
            "failure_policy": wait.failure_policy,
            "status": status,
            "reason": reason,
            "satisfied": satisfied,
            "resume_allowed": resume_allowed,
            "declared_task_ids": _declared_task_ids(reports),
            "satisfied_task_ids": _task_ids(reports, "satisfied"),
            "unsatisfied_task_ids": _task_ids(reports, "pending"),
            "failed_task_ids": _task_ids(reports, "failed"),
            "tolerated_task_ids": _task_ids(reports, "tolerated"),
            "blocking_task_ids": blocked_ids,
            "gated_task_ids": _task_ids(reports, "gated_by_generation"),
            "edge_count": len(reports),
            "satisfied_edge_count": sum(1 for report in reports if report["satisfied"]),
            "edges": tuple(reports),
            "timeout_seconds": wait.timeout_seconds,
            "elapsed_seconds": current_now - wait.created_at,
            "timed_out": timed_out,
            "cancel_epoch": wait.cancel_epoch,
            "current_cancel_epoch": current_epoch,
            "checkpoint_ref": wait.checkpoint_ref,
            "consumed_cursor": wait.consumed_cursor,
            "cursor": len(self._results),
        }
