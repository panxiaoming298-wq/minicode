"""Immutable conversation events, separate from per-request runtime snapshots.

Only the journal accepts new facts. Request views may be rebuilt from its verified
events; rebuilding never executes tools or certifies an unobserved result.
"""
from __future__ import annotations

from copy import deepcopy

from minicode.runtime_messages import is_runtime_snapshot, runtime_context_kind
from .models import content_hash_text, stable_json

HISTORY_BASIS = "event-log-v1"
REFERENCE = "_historyEvent"
# Presentation annotations are not the original tool receipt or instruction.
# Unknown fields remain protected, including attachments and future evidence.
PRESENTATION_FIELDS = frozenset({REFERENCE, "reasoningContent", "toolRecovery", "toolInput"})


class HistoryIntegrityError(ValueError):
    def __init__(self, reason, *, sequence=None, event_id=""):
        self.reason, self.sequence, self.event_id = reason, sequence, event_id
        super().__init__(f"history integrity: {reason}; event={event_id or 'unbound'}; sequence={sequence}")

    def diagnostic(self):
        return {"reason": self.reason, "sequence": self.sequence, "event_id": self.event_id,
                "action": "restore the original event/checkpoint; do not retry tools or reseal changed evidence"}


def durable_indices(messages):
    return [i for i, m in enumerate(messages) if m.get("role") != "system" and not is_runtime_snapshot(m)]


def payload(message):
    return {k: v for k, v in message.items() if k not in PRESENTATION_FIELDS}


def matches(record, message):
    # Dict equality conflates JSON booleans and numbers (True == 1).
    return content_hash_text(stable_json(payload(message))) == record["payload_hash"]


def stream_key(tenant, task, owner):
    return content_hash_text(stable_json([HISTORY_BASIS, tenant, task, owner]))


def genesis(stream):
    return content_hash_text(stable_json([HISTORY_BASIS, stream]))


def event_record(message, stream, sequence, previous):
    message = payload(message)
    digest = content_hash_text(stable_json(message))
    chain = content_hash_text(stable_json([stream, sequence, previous, digest]))
    return {"sequence": sequence, "event_id": "history:" + chain.removeprefix("sha256:"),
            "payload_hash": digest, "chain_hash": chain, "message": message}


def records_for(messages, stream):
    previous = genesis(stream)
    records = []
    for sequence, message in enumerate(messages, 1):
        record = event_record(message, stream, sequence, previous)
        records.append(record)
        previous = record["chain_hash"]
    return records


def boundary(messages, stream):
    records = records_for(messages, stream)
    return {"history_basis": HISTORY_BASIS, "history_stream": stream, "prefix_count": len(records),
            "prefix_hash": records[-1]["chain_hash"] if records else genesis(stream),
            "head_event_id": records[-1]["event_id"] if records else ""}


def verify_boundary(messages, anchor):
    count = anchor.get("prefix_count")
    if type(count) is not int or count <= 0 or count > len(messages):
        raise HistoryIntegrityError("boundary_unavailable", sequence=count)
    stream = anchor.get("history_stream")
    if not isinstance(stream, str) or not stream.startswith("sha256:"):
        raise HistoryIntegrityError("invalid_stream")
    observed = boundary(messages[:count], stream)
    if any(observed[k] != anchor.get(k) for k in ("prefix_hash", "head_event_id")):
        raise HistoryIntegrityError("boundary_changed", sequence=count, event_id=anchor.get("head_event_id", ""))


def verify_records(records, stream, head):
    expected = records_for([r["message"] for r in records], stream)
    for actual, wanted in zip(records, expected):
        if actual != wanted:
            raise HistoryIntegrityError("stored_event_changed", sequence=wanted["sequence"], event_id=actual.get("event_id", ""))
    if head != {"count": len(records), "hash": records[-1]["chain_hash"] if records else genesis(stream)}:
        raise HistoryIntegrityError("stored_tail_missing", sequence=len(records))


def reconcile(records, messages, stream):
    """Validate incoming facts; recover omitted, referenced events from the journal."""
    referenced = any(isinstance(m.get(REFERENCE), dict) and m[REFERENCE].get("stream") == stream for m in messages)
    observed, additions, last = {}, [], 0
    identities = {(r["message"].get("role"), r["message"].get("toolUseId")): r
                  for r in records if r["message"].get("toolUseId")}
    for message in messages:
        reference = message.get(REFERENCE)
        if isinstance(reference, dict) and reference.get("stream") == stream:
            sequence = reference.get("sequence")
            if type(sequence) is not int or not last < sequence <= len(records) or additions:
                raise HistoryIntegrityError("event_order_changed", sequence=sequence, event_id=reference.get("event_id", ""))
            record = records[sequence - 1]
            if reference.get("event_id") != record["event_id"] or not matches(record, message):
                raise HistoryIntegrityError("event_content_changed", sequence=sequence, event_id=record["event_id"])
            last = sequence
            observed[last] = message
        elif not additions and last < len(records) and matches(records[last], message):
            last += 1
            observed[last] = message
        else:
            if not referenced and last < len(records):
                record = records[last]
                raise HistoryIntegrityError("unreferenced_history_changed", sequence=last + 1, event_id=record["event_id"])
            identity = (message.get("role"), message.get("toolUseId"))
            if message.get("toolUseId") and identity in identities:
                record = identities[identity]
                if not matches(record, message) or record["sequence"] <= last or record["sequence"] > len(records):
                    raise HistoryIntegrityError("tool_identity_reused", sequence=record["sequence"], event_id=record["event_id"])
                last = record["sequence"]
                observed[last] = message
                continue
            previous = (additions[-1] if additions else records[-1] if records else {}).get("chain_hash", genesis(stream))
            record = event_record(message, stream, len(records) + len(additions) + 1, previous)
            additions.append(record)
            if message.get("toolUseId"):
                identities[identity] = record
            observed[record["sequence"]] = message
    return additions, observed


class HistoryJournal:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stream = stream_key(runtime.tenant_id, runtime.task_id, runtime.handoff_notes.owner)
        self.records = []
        self.rebuilt = False

    def sync(self, messages, *, bind=False):
        indices = set(durable_indices(messages))
        # A bound event cannot evade validation by changing its role/provenance
        # to look like a replaceable snapshot or a system instruction.
        incoming = [message for i, message in enumerate(messages) if i in indices
                    or (isinstance(message.get(REFERENCE), dict) and message[REFERENCE].get("stream") == self.stream)]
        records, observed = self.runtime.authority.sync_history(
            tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id, stream=self.stream, messages=incoming)
        self.records = records
        self.rebuilt = len(observed) != len(records)
        # Preserve a complete caller's view verbatim. Integrity lives in the
        # journal, not in rendering order or optional diagnostic annotations.
        if not bind and not self.rebuilt:
            return list(messages)
        history = []
        for record in records:
            message = deepcopy(observed.get(record["sequence"], record["message"]))
            if bind:
                message[REFERENCE] = {"stream": self.stream, "sequence": record["sequence"], "event_id": record["event_id"]}
            history.append(message)
        states = {}
        for message in messages:
            if is_runtime_snapshot(message):
                states[runtime_context_kind(message)] = message
        return [*[m for m in messages if m.get("role") == "system"], *history, *states.values()]

    def anchor(self, messages):
        return boundary([messages[i] for i in durable_indices(messages)], self.stream)

    def migration(self, anchor, *, proof=None):
        # Cutoff and retained positions are presentation details. A saved note
        # and its rotation share the same original immutable prefix identity.
        key = content_hash_text(stable_json([self.stream, anchor.get("history_basis"), anchor.get("prefix_count"), anchor.get("prefix_hash")]))
        return self.runtime.authority.history_migration(
            tenant_id=self.runtime.tenant_id, task_id=self.runtime.task_id, stream=self.stream, key=key, proof=proof)
