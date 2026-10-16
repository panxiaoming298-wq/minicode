"""Recognize optional search cards whose entire immutable source is in this request."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import time

from .models import EventType, MemoryType, content_hash_text
from .search_budget import authority_deadline


class InlineSourceCoverage:
    def __init__(self, authority, *, tenant_id: str, task_id: str,
                 content_hashes: Mapping[str, Sequence[str]], deadline: float | None,
                 mandatory_sources: Mapping[str, str] | None = None) -> None:
        self.authority = authority
        self.tenant_id, self.task_id = tenant_id, task_id
        self.content_hashes = {kind: frozenset(values) for kind, values in content_hashes.items()}
        self.deadline = deadline
        self.mandatory_sources = dict(mandatory_sources or {})
        self._events: dict[str, dict[str, str] | None] = {}
        self.omitted_ids: set[str] = set()
        self.omitted_sources: dict[str, str] = {}

    def covers(self, record, *, required_facets=()) -> bool:
        if (record.tenant_id != self.tenant_id or record.task_id != self.task_id
                or set(required_facets).intersection(record.facets)):
            return False
        # Full verified mandatory sources are retained independently of optional
        # retrieval. Never do this for a page pointer or partial source span.
        if (record.source_event_id and record.content_hash
                and self.mandatory_sources.get(record.source_event_id) == record.content_hash):
            self.omitted_ids.add(record.id)
            self.omitted_sources[record.id] = record.source_event_id
            return True
        return self._covers_projection(record.id, record.source_event_id,
                                       record.memory_type, record.facets)

    def _covers_projection(self, item_id, source_id, memory_type, facets):
        # Decisions, constraints, errors and semantic summaries are not simple
        # source projections. Facet obligations also retain their search cards.
        projection = (memory_type == MemoryType.TOOL_RESULT
                      or memory_type == MemoryType.FACT and "conversation" in facets)
        if not projection or not source_id or not self.content_hashes:
            return False
        proof = self.source_proof(source_id)
        if proof and proof["source_hash"] in self.content_hashes.get(proof["source_type"], ()):
            self.omitted_ids.add(item_id)
            self.omitted_sources[item_id] = source_id
            return True
        return False

    def source_proof(self, source_id):
        """Verify source identity once, independently of any request layout."""
        if source_id not in self._events:
            try:
                with authority_deadline(self.authority, self.deadline):
                    event = self.authority.get_event(tenant_id=self.tenant_id, task_id=self.task_id, event_id=source_id)
            except TimeoutError:
                return None  # No proof of full inline coverage: keep the card.
            valid = bool(
                event and event.content_text
                # Artifact-backed events keep a summary in content_text. Their
                # native full-source hash must match the complete live message,
                # not the summary/envelope hash. Required storage checks remain.
                and (content_hash_text(event.content_text) == event.content_hash
                     or bool(event.artifact_uri) and event.content_json.get("full_content_location") == "artifact")
            )
            self._events[source_id] = ({
                    "source_type": event.event_type.value, "source_hash": event.content_hash,
                } if valid else None)
        return self._events[source_id]


def prepare_render_coverage(runtime, packet):
    """Freeze bounded proofs once; request resizing/rendering performs no I/O.

    Keep cards in the packet. A later render with a shorter tail must restore
    them automatically, so a once-visible source is never a global exclusion.
    """
    from .tool_result_projection import protected_item

    if packet.debug_metadata.get("context_rotation", {}).get("status") != "active":
        return {}
    facets = packet.debug_metadata.get("required_facets", ())
    candidates = [item for item in [*packet.working_set, *packet.supplemental_memories]
                  if not protected_item(item) and not set(facets).intersection(item.facets)
                  and (item.item_type == MemoryType.TOOL_RESULT
                       or item.item_type == MemoryType.FACT and "conversation" in item.facets)]
    if not candidates:
        return {}
    coverage = InlineSourceCoverage(runtime.authority, tenant_id=runtime.tenant_id,
        task_id=runtime.task_id, content_hashes={},
        deadline=time.monotonic() + min(0.05, runtime.config.context_retrieval_timeout_seconds))
    proofs = {}
    for item in candidates:
        proof = coverage.source_proof(item.source_event_id) if item.source_event_id else None
        expected_type = EventType.TOOL_RESULT.value if item.item_type == MemoryType.TOOL_RESULT else EventType.USER_INPUT.value
        if proof and proof["source_type"] == expected_type:
            proofs[item.item_id] = {**proof,
                                   "source_event_id": item.source_event_id,
                                   "item_hash": item.content_hash, "item_type": item.item_type,
                                   "text_hash": content_hash_text(item.text)}
    return proofs


def covered_at_render(item, proofs, visible_hashes, *, required_facets=()):
    proof = proofs.get(item.item_id)
    return bool(proof and not item.required and not set(required_facets).intersection(item.facets)
                and proof["source_event_id"] == item.source_event_id
                and proof["item_hash"] == item.content_hash and proof["item_type"] == item.item_type
                and proof["text_hash"] == content_hash_text(item.text)
                and proof["source_hash"] in visible_hashes.get(proof["source_type"], ()))
