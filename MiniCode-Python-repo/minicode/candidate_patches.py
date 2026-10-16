"""Version evidence and the patch acceptance/application transaction.

Why this exists
---------------
Two Builders legitimately start from the same primary revision. After the first
one's patch lands, the second one's base is no longer current. A "base must equal
current" rule makes parallel work useless; "git says there is no conflict" is not a
correctness argument. Neither is a caller-supplied revision string, or a boolean
that asserts "not canceled".

What this module decides and records
------------------------------------
* ``decide_promotion`` answers one question for one exact patch: may it be applied
  onto the target candidate as it is right now? Authority is checked FIRST and on
  EVERY path -- cancellation, owner attempt/generation/fence, plan and contract,
  read-set completeness, then the dependency vector -- and only then the version
  comparison. Producing an artifact and having it applied are different facts.
* ``PatchLedger`` is a three-phase transaction: ``accept`` binds the immutable
  patch hash and identity to an operation, ``intent`` persists an applying
  intention, ``complete`` records applied ONLY after the caller verified the bytes
  that really landed, and ``fail`` records failure or uncertainty. Nothing is ever
  declared applied before I/O, and a repeat application never calls the writer
  again.

What it cannot prove
--------------------
Compatibility is an admission condition, not a semantic guarantee: a promoted
patch still needs the combination check and independent verification. This module
also cannot recover a read set that was never captured, and it cannot prove that a
filesystem write was atomic -- it records what was verified after the write.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PATCH_SCHEMA = "minicode-candidate-patch-v1"
VECTOR_SCHEMA = "minicode-dependency-vector-v1"
# The per-entry evidence a dependency pin carries. Bumping it is how a consumer
# records which fields it actually pinned, so an older pin can be recognized
# instead of being trusted as if it had pinned everything.
DEPENDENCY_PIN_SCHEMA = "minicode-dependency-pin-v1"
DECISION_SCHEMA = "minicode-promotion-decision-v1"
RECEIPT_SCHEMA = "minicode-patch-acceptance-v1"
APPLICATION_SCHEMA = "minicode-patch-application-v1"
APPLIED_SCHEMA = "minicode-patch-decision-v1"

SAME_REVISION = "same_revision"
EVIDENCE_COMPATIBLE = "evidence_compatible"
REJECTED = "rejected"

PATCH_ACCEPTED = "accepted"
PATCH_APPLYING = "applying"
PATCH_APPLIED = "applied"
PATCH_FAILED = "failed"
PATCH_UNCERTAIN = "uncertain"

# Files whose change is never "just another file": a shared interface, a lock
# file or a build/config file invalidates an unrelated patch's assumptions.
SHARED_SURFACE_MARKERS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "poetry.lock",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "docker-compose.yml", "dockerfile", "__init__.py", "conftest.py",
)


class PatchConflict(RuntimeError):
    """A patch may not be accepted, promoted or applied as requested."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _relative(value: object) -> str:
    """A lexical repository-relative path, never a filesystem lookup."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise PatchConflict("patch paths must be plain repository-relative strings")
    if value.startswith("/") or value.startswith("//"):
        raise PatchConflict("patch paths must not be absolute")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PatchConflict("patch paths must not traverse or contain empty segments")
    return value


@dataclass(frozen=True, slots=True)
class NativeReadWriteSet:
    """What a candidate really read and really changed, as native facts.

    ``read_hashes`` maps a relative path to the sha256 observed, or to the empty
    string when the tool observed that the file does NOT exist. ``read_directories``
    records directories that were enumerated, so a later new file in one of them
    invalidates the patch instead of being missed. ``complete`` is False whenever
    a dynamic read (an arbitrary shell command, for example) could not be
    captured; an incomplete set is never eligible for cross-version admission.
    """

    read_hashes: Mapping[str, str] = field(default_factory=dict)
    read_directories: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    complete: bool = True
    omissions: tuple[str, ...] = ()
    directory_hashes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "read_hashes",
                           {_relative(k): str(v) for k, v in dict(self.read_hashes).items()})
        object.__setattr__(self, "read_directories",
                           tuple(sorted({"." if item == "." else _relative(item) for item in self.read_directories})))
        object.__setattr__(self, "write_set",
                           tuple(sorted({_relative(item) for item in self.write_set})))
        if self.complete and self.omissions:
            raise PatchConflict("a complete read set cannot carry omissions")
        hashes = dict(self.directory_hashes)
        if (set(hashes) - set(self.read_directories) or any(
                not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest) for digest in hashes.values())):
            raise PatchConflict("directory membership hashes need an observed directory and sha256")
        object.__setattr__(self, "directory_hashes", hashes)

    def read_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.read_hashes))

    def covers(self, path: str) -> bool:
        if path in self.read_hashes or path in self.write_set:
            return True
        return any(directory == "." or path == directory or path.startswith(directory + "/")
                   for directory in self.read_directories)

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_hashes": dict(self.read_hashes),
            "read_directories": list(self.read_directories),
            "write_set": list(self.write_set),
            "complete": bool(self.complete),
            "omissions": list(self.omissions),
            "missing_reads": sorted(path for path, digest in self.read_hashes.items() if not digest),
            **({"directory_hashes": dict(self.directory_hashes)} if self.directory_hashes else {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NativeReadWriteSet":
        return cls(read_hashes=dict(payload.get("read_hashes", {})),
                   read_directories=tuple(payload.get("read_directories", ())),
                   write_set=tuple(payload.get("write_set", ())),
                   complete=bool(payload.get("complete", True)),
                   omissions=tuple(payload.get("omissions", ())),
                   directory_hashes=dict(payload.get("directory_hashes", {})))


@dataclass(frozen=True, slots=True)
class DependencyVector:
    """Every dependency this patch was built against, pinned exactly.

    An entry records the evidence that actually released the consumer's declared
    edge when the consumer gained execution rights -- not merely that some task
    succeeded. ``pinned`` carries that evidence (identity, predicate and the
    predicate's own fact: artifact hash, patch revision, or terminal event). An
    entry without it is a legacy or unevaluated record and is treated as
    unverifiable rather than as a satisfied dependency.
    """

    entries: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        normalized = []
        for entry in self.entries:
            if not isinstance(entry, Mapping):
                raise PatchConflict("a dependency entry must be a mapping")
            task_id = entry.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise PatchConflict("a dependency entry needs a task id")
            normalized.append({
                "task_id": task_id,
                "generation": int(entry.get("generation", 1)),
                "artifact_id": entry.get("artifact_id"),
                "artifact_sha256": entry.get("artifact_sha256"),
                "predicate": entry.get("predicate"),
                "applied_revision": entry.get("applied_revision"),
                **({"pinned": dict(entry["pinned"])} if isinstance(entry.get("pinned"), Mapping) else {}),
            })
        normalized.sort(key=lambda item: item["task_id"])
        object.__setattr__(self, "entries", tuple(normalized))

    def __bool__(self) -> bool:
        return bool(self.entries)

    def digest(self) -> str:
        return _digest(list(self.entries))

    def identity(self) -> dict[str, Any]:
        """The comparable projection of this vector: only what the pins assert.

        ``observed`` records published event ids and mailbox positions, which a
        legitimate republish reassigns; including them would report a dependency
        as "changed" for a reason the predicate never claimed.
        """

        projected = {}
        for entry in self.entries:
            pin = entry.get("pinned")
            projected[entry["task_id"]] = (
                {key: value for key, value in pin.items() if key not in {"schema", "observed"}}
                if isinstance(pin, Mapping) else
                {key: entry.get(key) for key in ("generation", "artifact_id", "artifact_sha256",
                                                 "predicate", "applied_revision")}
            )
        return projected

    def changed_since(self, other: "DependencyVector") -> tuple[str, ...]:
        here, there = self.identity(), other.identity()
        return tuple(sorted(task_id for task_id in set(here) | set(there)
                            if here.get(task_id) != there.get(task_id)))

    def pinned_tasks(self) -> tuple[str, ...]:
        return tuple(entry["task_id"] for entry in self.entries if entry.get("predicate"))

    def unpinned_tasks(self) -> tuple[str, ...]:
        return tuple(entry["task_id"] for entry in self.entries if not entry.get("pinned"))

    def to_dict(self) -> dict[str, Any]:
        return {"schema": VECTOR_SCHEMA, "digest": self.digest(),
                "entries": [dict(entry) for entry in self.entries]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "DependencyVector":
        if not payload:
            return cls()
        return cls(tuple(dict(entry) for entry in payload.get("entries", ())))


@dataclass(frozen=True, slots=True)
class CandidatePatch:
    """One candidate's exact bytes, scope, origin and authority binding.

    ``source_candidate_id`` is where the bytes were produced (a Builder's own
    candidate) and ``target_candidate_id`` is where they must land (the primary
    candidate P). Reusing one ambiguous id for both is exactly how a patch from
    one candidate gets applied into another.
    """

    patch_id: str
    source_candidate_id: str
    target_candidate_id: str
    owner_node_id: str
    owner_attempt_id: str
    owner_generation: int
    owner_fencing_token: int
    plan_version: int
    contract_revision: int
    origin_base_revision: str
    current_revision: str
    patch_sha256: str
    changed_paths: tuple[str, ...]
    read_write_set: NativeReadWriteSet
    # The producer's cancellation epoch when this patch was produced. Any later
    # cancellation of that producer makes this patch ineligible to be applied.
    producer_cancel_epoch: int = 0
    # The immutable content the patch really is: per changed path, the digest of
    # the version to publish (``None`` for a deletion) and the digest the path had
    # at the origin base. Applying re-reads the PRODUCER against these, never the
    # producer's current bytes as if they were the accepted ones.
    content_hashes: Mapping[str, str | None] = field(default_factory=dict)
    base_hashes: Mapping[str, str | None] = field(default_factory=dict)
    dependency_vector: DependencyVector = field(default_factory=DependencyVector)
    checks: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PATCH_SCHEMA

    def __post_init__(self) -> None:
        for name in ("patch_id", "source_candidate_id", "target_candidate_id", "owner_node_id",
                     "owner_attempt_id", "origin_base_revision", "current_revision", "patch_sha256"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise PatchConflict(f"a candidate patch needs a non-blank {name}")
        if self.source_candidate_id == self.target_candidate_id:
            raise PatchConflict("a patch must name a distinct source and target candidate")
        for name in ("owner_generation", "owner_fencing_token", "plan_version", "contract_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise PatchConflict(f"a candidate patch needs a positive {name}")
        if type(self.producer_cancel_epoch) is not int or self.producer_cancel_epoch < 0:
            raise PatchConflict("a candidate patch needs a non-negative producer_cancel_epoch")
        object.__setattr__(self, "changed_paths",
                           tuple(sorted({_relative(item) for item in self.changed_paths})))
        if set(self.changed_paths) != set(self.read_write_set.write_set):
            raise PatchConflict("patch changed paths must equal the native write set exactly")
        content = {_relative(key): value for key, value in dict(self.content_hashes).items()}
        for key, value in content.items():
            if value is not None and (not isinstance(value, str) or len(value) != 64):
                raise PatchConflict("patch content hashes must be sha256 hex or None")
        object.__setattr__(self, "content_hashes", content)
        object.__setattr__(self, "base_hashes",
                           {_relative(key): value for key, value in dict(self.base_hashes).items()})
        if content and set(content) != set(self.changed_paths):
            raise PatchConflict("patch content hashes must cover exactly the changed paths")

    def identity(self) -> dict[str, Any]:
        """The immutable identity an acceptance is bound to."""
        return {
            "patch_id": self.patch_id, "patch_sha256": self.patch_sha256,
            "source_candidate_id": self.source_candidate_id,
            "target_candidate_id": self.target_candidate_id,
            "owner_node_id": self.owner_node_id, "owner_attempt_id": self.owner_attempt_id,
            "owner_generation": self.owner_generation,
            "owner_fencing_token": self.owner_fencing_token,
            "plan_version": self.plan_version, "contract_revision": self.contract_revision,
            "origin_base_revision": self.origin_base_revision,
            "producer_cancel_epoch": self.producer_cancel_epoch,
            "dependency_vector_digest": self.dependency_vector.digest(),
        }

    def identity_digest(self) -> str:
        return _digest(self.identity())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema_version, **self.identity(),
            "current_revision": self.current_revision,
            "changed_paths": list(self.changed_paths),
            "producer_cancel_epoch": self.producer_cancel_epoch,
            "content_hashes": dict(self.content_hashes),
            "base_hashes": dict(self.base_hashes),
            "read_write_set": self.read_write_set.to_dict(),
            "dependency_vector": self.dependency_vector.to_dict(),
            "checks": dict(self.checks),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidatePatch":
        return cls(
            patch_id=str(payload["patch_id"]),
            source_candidate_id=str(payload["source_candidate_id"]),
            target_candidate_id=str(payload["target_candidate_id"]),
            owner_node_id=str(payload["owner_node_id"]),
            owner_attempt_id=str(payload["owner_attempt_id"]),
            owner_generation=int(payload["owner_generation"]),
            owner_fencing_token=int(payload["owner_fencing_token"]),
            plan_version=int(payload["plan_version"]),
            contract_revision=int(payload["contract_revision"]),
            origin_base_revision=str(payload["origin_base_revision"]),
            current_revision=str(payload["current_revision"]),
            patch_sha256=str(payload["patch_sha256"]),
            changed_paths=tuple(payload.get("changed_paths", ())),
            read_write_set=NativeReadWriteSet.from_dict(payload.get("read_write_set", {})),
            producer_cancel_epoch=int(payload.get("producer_cancel_epoch", 0) or 0),
            content_hashes=dict(payload.get("content_hashes") or {}),
            base_hashes=dict(payload.get("base_hashes") or {}),
            dependency_vector=DependencyVector.from_dict(payload.get("dependency_vector")),
            checks=dict(payload.get("checks", {})),
        )


@dataclass(frozen=True, slots=True)
class AuthoritativeFacts:
    """What the Runtime really observed at the write boundary.

    Two identities are read, because they answer different questions: the
    PRODUCER (the Builder attempt whose bytes these are) decides whether the patch
    is still the current work of a live attempt, and the TARGET candidate decides
    whether the write may happen at all. Every field is read from native state --
    the reducer, the candidate lease, the filesystem snapshot -- never from a
    caller's assertion. In particular there is no "not canceled" boolean.
    """

    native_revision: str | None
    producer_attempt_id: str | None
    producer_generation: int | None
    producer_fencing_token: int | None
    plan_version: int
    contract_revision: int
    cancel_epoch: int
    canceled: bool
    dependency_vector: DependencyVector | None
    intervening_changes: Mapping[str, str] | None
    target_owner_attempt_id: str | None = None
    # The alternative-group selection that authorized this producer, read from
    # native state. Accepting is not applying, so the selection is re-read at the
    # write boundary: a patch whose producer stopped being the group's winner must
    # not land, even though it was legitimately accepted while it still was.
    selection_winner: str | None = None
    selection_operation_id: str | None = None
    selection_group: str | None = None
    # The PRODUCER's own cancellation, read from the same native state as the
    # target's. Cancelling a Builder does not have to change its attempt id, so
    # the target being uncancelled says nothing about whether this patch is still
    # the current work of a live, uncancelled producer.
    producer_canceled: bool = False
    producer_cancel_epoch: int = 0
    facts_complete: bool = True
    unavailable_reason: str = ""
    # Per declared dependency, why the pinned evidence no longer describes the
    # current dependency. Non-empty means the patch must be re-planned, not
    # silently rebound to whatever the dependency looks like now.
    revoked_dependencies: Mapping[str, str] = field(default_factory=dict)
    directory_hashes: Mapping[str, str] = field(default_factory=dict)
    projected_directory_hashes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    accepted: bool
    path: str
    reason: str
    requires_rebase: bool
    conflicting_paths: tuple[str, ...] = ()
    changed_dependencies: tuple[str, ...] = ()
    schema_version: str = DECISION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema_version, "accepted": self.accepted, "path": self.path,
                "reason": self.reason, "requires_rebase": self.requires_rebase,
                "conflicting_paths": list(self.conflicting_paths),
                "changed_dependencies": list(self.changed_dependencies)}


def is_shared_surface(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].casefold()
    return name in SHARED_SURFACE_MARKERS


def _reject(reason: str, *, rebase: bool = True, conflicts: Iterable[str] = (),
            changed: Iterable[str] = ()) -> PromotionDecision:
    return PromotionDecision(False, REJECTED, reason, rebase,
                             tuple(sorted(set(conflicts))), tuple(sorted(set(changed))))


def decide_promotion(patch: CandidatePatch, facts: AuthoritativeFacts) -> PromotionDecision:
    """Whether this exact patch may be applied onto the target candidate now.

    Order is the contract. Authority is evaluated before any version comparison,
    and the dependency check runs on EVERY path -- including the identical-revision
    fast path, because identical workspace bytes do not mean the dependency
    artifacts and execution protocol are unchanged.
    """
    if not facts.facts_complete:
        return _reject(f"authoritative facts unavailable: {facts.unavailable_reason}")
    if facts.canceled:
        return _reject("the target candidate is canceled")
    if facts.producer_attempt_id is None:
        return _reject("the patch producer has no live attempt")
    # The producer's OWN cancellation/epoch is checked before the target's, because
    # a cancelled Builder must not have its late output applied just because the
    # candidate it would land in is still healthy.
    if facts.producer_canceled:
        return _reject("the patch producer was canceled; its produced artefact is only "
                       "auditable and can no longer be applied")
    if facts.producer_cancel_epoch != patch.producer_cancel_epoch:
        return _reject(
            "the patch producer's cancellation epoch advanced after this patch was "
            "produced, so it is no longer the work of a live producer")
    if (facts.producer_attempt_id != patch.owner_attempt_id
            or facts.producer_generation != patch.owner_generation
            or facts.producer_fencing_token != patch.owner_fencing_token):
        return _reject(
            "the patch producer attempt, generation or fencing token is no longer the live one")
    if facts.plan_version != patch.plan_version or facts.contract_revision != patch.contract_revision:
        return _reject("the plan version or contract revision changed since the patch was produced")
    if not patch.read_write_set.complete:
        return _reject(
            "the native read set is incomplete, so no cross-version admission is possible")

    # Group selection identity, on every path. Once a member's application has
    # started the group's winner is frozen; re-selecting the same winner stays a
    # legal idempotent replay, and a different winner must be refused rather than
    # let the previously discarded work land.
    if facts.selection_winner is not None and facts.selection_winner != patch.owner_node_id:
        return _reject(
            "the alternative group now selects a different winner, so this producer's "
            "patch is no longer authorised to land",
            changed=(patch.owner_node_id,), rebase=False)

    # Dependency identity, on every path.
    if patch.dependency_vector:
        if facts.dependency_vector is None:
            return _reject("the patch declares dependencies but no current dependency vector "
                           "was supplied")
        # A pinned entry whose evidence no longer describes the current dependency
        # is a REVOKED pin: the consumer built against something that has moved.
        # Refusing here is the whole point -- re-reading the current dependency
        # instead would silently rebind the consumer to work it never saw.
        if facts.revoked_dependencies:
            return _reject(
                "a dependency this patch pinned is no longer the one it was built against; "
                "the consumer must be re-planned instead of rebound",
                changed=facts.revoked_dependencies)
        unpinned = patch.dependency_vector.unpinned_tasks()
        if unpinned and len(unpinned) != len(patch.dependency_vector.entries):
            # A vector that pins SOME dependencies but records no evidence for
            # others is a gap in an otherwise evidence-carrying vector, and the
            # gap is never treated as satisfied. A wholly unpinned vector is a
            # legacy record from before evidence was captured; it keeps its old
            # comparison semantics and is never upgraded into trusted evidence.
            return _reject(
                "this patch carries dependency entries without the evidence that released "
                "them, so the pin cannot be verified", changed=unpinned)
        changed = patch.dependency_vector.changed_since(facts.dependency_vector)
        if changed:
            return _reject("a dependency artefact or generation this patch was built against "
                           "has changed", changed=changed)
    if facts.native_revision is None:
        return _reject("the native target revision could not be read")

    changed_directories = [name for name, digest in patch.read_write_set.directory_hashes.items()
                           if digest not in {facts.directory_hashes.get(name),
                                             facts.projected_directory_hashes.get(name)}]
    if changed_directories:
        return _reject("observed directory membership changed or could not be verified",
                       conflicts=changed_directories)

    if facts.native_revision == patch.origin_base_revision:
        if facts.intervening_changes:
            return _reject("the base revision equals the current revision but intervening "
                           "changes were reported",
                           conflicts=facts.intervening_changes)
        return PromotionDecision(True, SAME_REVISION, "base equals the current revision", False)

    if facts.intervening_changes is None:
        return _reject("intervening changes between the origin base and the current revision "
                       "could not be computed")
    conflicts = []
    for path in sorted(facts.intervening_changes):
        if path in patch.read_write_set.write_set or path in patch.read_write_set.read_hashes:
            conflicts.append(path)
        elif any((directory == "." or path == directory or path.startswith(directory + "/"))
                 and directory not in patch.read_write_set.directory_hashes
                 for directory in patch.read_write_set.read_directories):
            conflicts.append(path)
        elif is_shared_surface(path):
            conflicts.append(path)
    for path, digest in patch.read_write_set.read_hashes.items():
        if not digest and path in facts.intervening_changes:
            conflicts.append(path)
    if conflicts:
        return _reject("intervening changes touch files this patch read, wrote, enumerated or "
                       "depends on through a shared interface", conflicts=conflicts)
    return PromotionDecision(
        True, EVIDENCE_COMPATIBLE,
        "every intervening change is outside this patch's native read/write set, enumerated "
        "directories, dependency artefacts and shared interfaces", False)


class PatchLedger:
    """Three-phase acceptance/application records, bound to immutable identity.

    ``accepted`` -> ``applying`` -> ``applied`` (or ``failed`` / ``uncertain``).
    A repeat application of the same identity returns the recorded receipt and
    never calls the writer again. Changing the patch bytes, the owner attempt, the
    operation or the target revision is refused BEFORE any write.
    """

    def __init__(self, *, restore: Mapping[str, Any] | None = None) -> None:
        self._records: dict[str, dict[str, Any]] = {
            str(key): dict(value) for key, value in (restore or {}).items()
            if isinstance(value, Mapping) and value.get("state") in {
                PATCH_ACCEPTED, PATCH_APPLYING, PATCH_APPLIED, PATCH_FAILED, PATCH_UNCERTAIN}
        }
        for record in self._records.values():
            if record.get("state") == PATCH_APPLYING:
                record.update(state=PATCH_UNCERTAIN, applied=False,
                              reason="process restored before the application receipt; reconcile native bytes")

    # ---- persistence ---------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._records.items()}

    def record(self, patch_id: str) -> dict[str, Any] | None:
        found = self._records.get(patch_id)
        return dict(found) if found else None

    def _write(self, patch_id: str, record: dict[str, Any]) -> dict[str, Any]:
        self._records[patch_id] = record
        return dict(record)

    # ---- transaction ---------------------------------------------------

    def accept(self, patch: CandidatePatch, *, decision: PromotionDecision,
               operation_id: str, cancel_epoch: int,
               facts: "AuthoritativeFacts | None" = None) -> dict[str, Any]:
        if not decision.accepted:
            raise PatchConflict("a rejected promotion cannot be accepted")
        if not isinstance(operation_id, str) or not operation_id:
            raise PatchConflict("acceptance requires an operation id")
        existing = self._records.get(patch.patch_id)
        if existing is not None:
            if (existing.get("operation_id") != operation_id
                    or existing.get("patch_sha256") != patch.patch_sha256
                    or existing.get("identity") != patch.identity()):
                raise PatchConflict(
                    "this patch id is already bound to a different operation or content")
            return dict(existing)
        return self._write(patch.patch_id, {
            "schema": RECEIPT_SCHEMA, "state": PATCH_ACCEPTED,
            "patch_id": patch.patch_id, "operation_id": operation_id,
            "patch_sha256": patch.patch_sha256, "identity": patch.identity(),
            "identity_digest": patch.identity_digest(),
            "origin_base_revision": patch.origin_base_revision,
            # The admission decision is recorded as the reason for accepting, and
            # separately from the revision fields: an acceptance decision is not a
            # revision, and storing it AS the expected revision made the applied
            # transition unreadable.
            "acceptance_decision": decision.to_dict(), "path": decision.path,
            # The group selection that authorized this producer, pinned at
            # acceptance so the write boundary can prove it is still the winner.
            "selection_winner": facts.selection_winner if facts is not None else None,
            "selection_operation_id": (facts.selection_operation_id
                                       if facts is not None else None),
            "selection_group": facts.selection_group if facts is not None else None,
            # Neither revision is known until the write boundary observes them.
            "expected_revision": None, "applied_revision": None,
            "cancel_epoch": int(cancel_epoch), "applied": False, "verified": False,
            "integrated": False,
            # Accepting grants no write right: the apply phase re-reads native
            # facts and re-decides before touching a byte.
            "grants_write_right": False,
        })

    def reusable_application(self, patch: CandidatePatch, *, operation_id: str) -> dict[str, Any] | None:
        """The recorded success for this exact identity, if any.

        A caller that gets a receipt here MUST NOT call the writer again.
        """
        record = self._records.get(patch.patch_id)
        if record is None:
            return None
        if (record.get("operation_id") != operation_id
                or record.get("patch_sha256") != patch.patch_sha256
                or record.get("identity") != patch.identity()):
            return None
        return dict(record) if record.get("state") == PATCH_APPLIED else None

    def begin_apply(self, patch: CandidatePatch, *, operation_id: str,
                    native_revision: str, cancel_epoch: int,
                    facts: "AuthoritativeFacts | None" = None) -> dict[str, Any]:
        """Persist the apply intent for one accepted patch.

        Any state that is not a clean previous refusal is a STOP: an application
        that is in progress, or one whose outcome was never established, cannot be
        replayed from the record alone. ``reconcile`` is the only way out of
        ``uncertain``, and it has to look at the candidate's real bytes.
        """
        record = self._records.get(patch.patch_id)
        if record is None:
            raise PatchConflict("the patch was never accepted")
        if record.get("state") == PATCH_APPLIED:
            raise PatchConflict("the patch is already applied")
        if record.get("state") == PATCH_APPLYING:
            raise PatchConflict("an application of this patch is already in progress")
        if record.get("state") == PATCH_UNCERTAIN:
            raise PatchConflict(
                "this patch's application outcome is uncertain; the candidate's real "
                "bytes must be reconciled before it can be applied again")
        if record.get("operation_id") != operation_id:
            raise PatchConflict("the operation id does not match the acceptance")
        if record.get("patch_sha256") != patch.patch_sha256:
            raise PatchConflict("the patch content changed after it was accepted")
        if record.get("identity") != patch.identity():
            raise PatchConflict("the patch identity changed after it was accepted")
        if int(record.get("cancel_epoch", 0)) != int(cancel_epoch):
            raise PatchConflict("the candidate cancellation epoch advanced after acceptance")
        if not isinstance(native_revision, str) or not native_revision:
            raise PatchConflict("applying a patch requires the observed target revision")
        return self._write(patch.patch_id, {
            **record, "state": PATCH_APPLYING, "applying_at_revision": native_revision,
            "expected_revision": native_revision,
            "applied_revision": None, "applied": False,
            # The winner observed at the write boundary, recorded so recovery can
            # tell "the group's winner really applied this" from a spliced record.
            "applied_selection_winner": (facts.selection_winner
                                         if facts is not None else record.get("selection_winner")),
            "applied_selection_operation_id": (facts.selection_operation_id
                                               if facts is not None else None),
        })

    def reconcile(self, patch: CandidatePatch, *, operation_id: str,
                  target_revision: str, byte_identity: Mapping[str, str | None] | None) -> dict[str, Any]:
        """Resolve an uncertain application from the candidate's REAL bytes.

        ``byte_identity`` is the re-hash of the patch's changed paths as they are
        on disk right now (``None`` for a path that is absent). Three outcomes are
        possible and each is recorded explicitly:

        * every path already holds the accepted version -- the write did land, so
          the record is completed rather than repeated;
        * no path holds it -- nothing landed, so the intent is cleared for a retry;
        * some do -- a partial publication, which is never silently retried.

        A caller that cannot supply real bytes cannot reconcile, and the record
        stays uncertain so the patch cannot be blindly replayed.
        """
        record = self._records.get(patch.patch_id)
        if record is None:
            raise PatchConflict("no record exists for this patch")
        if record.get("operation_id") != operation_id:
            raise PatchConflict("the operation id does not match the acceptance")
        if record.get("patch_sha256") != patch.patch_sha256 \
                or record.get("identity") != patch.identity():
            raise PatchConflict("the patch identity changed since it was accepted")
        if record.get("state") != PATCH_UNCERTAIN:
            raise PatchConflict("only an uncertain application needs reconciliation")
        if not isinstance(target_revision, str) or not target_revision:
            raise PatchConflict("reconciliation requires the observed target revision")
        if not isinstance(byte_identity, Mapping):
            raise PatchConflict(
                "reconciliation requires the real bytes of every path the patch changes")
        missing = sorted(set(patch.changed_paths) - set(byte_identity))
        if missing:
            raise PatchConflict(
                "reconciliation requires the real bytes of every path the patch "
                f"changes; missing: {', '.join(missing)}")
        landed = [name for name in patch.changed_paths
                  if byte_identity.get(name) == patch.content_hashes.get(name)]
        if len(landed) == len(patch.changed_paths):
            return self._write(patch.patch_id, {
                **record, "state": PATCH_APPLIED, "applied": True,
                "applied_revision": target_revision,
                "expected_revision": record.get("expected_revision"),
                "observed_sha256": patch.patch_sha256,
                "reconciled": "already_landed", "verified": False, "integrated": False,
                "invalidates_prior_checks": True,
            })
        if landed:
            raise PatchConflict(
                "the previous application landed only part of this patch "
                f"({', '.join(sorted(landed))}); the candidate must be repaired before "
                "the patch can be applied again")
        if any(byte_identity.get(name) != patch.base_hashes.get(name) for name in patch.changed_paths):
            raise PatchConflict("uncertain target differs from both the original and proposed bytes; reconciliation cannot authorize replay")
        return self._write(patch.patch_id, {
            **record, "state": PATCH_ACCEPTED, "applying_at_revision": None,
            "expected_revision": None, "applied_revision": None, "applied": False,
            "reconciled": "nothing_landed",
        })

    def complete(self, patch: CandidatePatch, *, observed_sha256: str,
                 native_revision: str) -> dict[str, Any]:
        record = self._records.get(patch.patch_id)
        if record is None or record.get("state") != PATCH_APPLYING:
            raise PatchConflict("no application is in progress for this patch")
        if observed_sha256 != patch.patch_sha256:
            raise PatchConflict(
                "the bytes that landed do not match the accepted patch hash")
        if not isinstance(native_revision, str) or not native_revision:
            raise PatchConflict("completing an application requires the observed new revision")
        return self._write(patch.patch_id, {
            **record, "state": PATCH_APPLIED, "applied": True,
            # TWO revisions, because the P0 -> P1 transition is the auditable fact:
            # what the target was when the write began, and what it became.
            "expected_revision": record.get("applying_at_revision"),
            "applied_revision": native_revision, "observed_sha256": observed_sha256,
            # Applying is not verification and not integration.
            "verified": False, "integrated": False,
            # Every check or review concluded before this point is now void.
            "invalidates_prior_checks": True,
        })

    def record_checks(self, patch_id: str, checks: Mapping[str, Any]) -> dict[str, Any]:
        """Attach the combination-check outcome to an applied patch.

        The result is what lets a code dependency be released: ``patch_applied``
        alone means the bytes landed, not that they were checked.
        """
        record = self._records.get(patch_id)
        if record is None:
            raise PatchConflict("no record exists for this patch")
        if record.get("state") != PATCH_APPLIED:
            raise PatchConflict("only an applied patch can carry a combination check")
        merged = dict(record.get("checks") or {})
        merged.update({str(key): dict(value) if isinstance(value, Mapping) else value
                       for key, value in dict(checks).items()})
        return self._write(patch_id, {**record, "checks": merged})

    def fail(self, patch: CandidatePatch, *, reason: str, uncertain: bool = False) -> dict[str, Any]:
        record = self._records.get(patch.patch_id)
        if record is None:
            raise PatchConflict("no record exists for this patch")
        if record.get("state") == PATCH_APPLIED:
            raise PatchConflict("an applied patch cannot be marked failed")
        state = PATCH_UNCERTAIN if uncertain else PATCH_FAILED
        return self._write(patch.patch_id, {
            **record, "state": state, "applied": False, "failure_reason": reason,
            "invalidates_prior_checks": True,
        })

    def applications(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._records.values()
                     if item.get("state") == PATCH_APPLIED)

    def invalidated_patch_ids(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, item in self._records.items()
                            if item.get("invalidates_prior_checks")))


def read_set_conflicts(
    read_write_set: NativeReadWriteSet, intervening_changes: Mapping[str, str],
) -> tuple[str, ...]:
    """The auditable diff a compatible promotion was allowed to cross."""
    return tuple(sorted(path for path in intervening_changes
                        if read_write_set.covers(path) or is_shared_surface(path)))


def combine_patches(
    patches: Sequence[CandidatePatch], *, current_revision: str,
) -> dict[str, Any]:
    """Whether two independently produced patches may be combined in order.

    A/B from the same base are combined by promoting A to P1 and then B onto P1 on
    A's evidence. Two patches that both changed the same path, or that changed a
    path the other read, are a genuine conflict and need a new bounded repair --
    a clean text merge is not that proof.
    """
    written: dict[str, str] = {}
    conflicts: list[tuple[str, str, str]] = []
    for patch in patches:
        if not patch.read_write_set.complete:
            return {"schema": "minicode-patch-combination-v1",
                    "current_revision": current_revision,
                    "order": [item.patch_id for item in patches], "combinable": False,
                    "conflicts": [], "requires_bounded_repair": True,
                    "reason": f"{patch.patch_id} has an incomplete native read set",
                    "grants_verification": False}
        for path in patch.changed_paths:
            owner = written.get(path)
            if owner is not None:
                conflicts.append((owner, patch.patch_id, path))
            else:
                written[path] = patch.patch_id
        for path in patch.read_write_set.read_hashes:
            owner = written.get(path)
            if owner is not None and owner != patch.patch_id:
                conflicts.append((owner, patch.patch_id, path))
    return {
        "schema": "minicode-patch-combination-v1",
        "current_revision": current_revision,
        "order": [patch.patch_id for patch in patches],
        "combinable": not conflicts,
        "conflicts": [{"earlier": a, "later": b, "path": path} for a, b, path in conflicts],
        "requires_bounded_repair": bool(conflicts),
        "grants_verification": False,
    }
