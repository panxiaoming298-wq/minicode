"""One request's optional memory pages, owned by the active Worker.

No parsing of system-message headings, persisted authority or model summaries.
Canonical packet items and the provider-valid recent tail remain unchanged.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from minicode.usage_ledger import BudgetUnavailable

from .models import MemoryMode, content_hash_text


def _exact_inline_sources(packet, wire_messages):
    """Reference only whole, hash-checked native sources still visible verbatim.

    Do not search headings or substrings in instructions. The packet is rendered
    by its current owner and message indices refer to the final provider layout.
    Unknown fields, errors and all actual conversation/tool history stay intact.
    """
    inline = {message["content"]: index for index, message in enumerate(wire_messages)
              if message.get("role") == "user" and isinstance(message.get("content"), str)}
    references = []

    def project(item):
        # Whole packet sources already use the renderer's exact tail coverage.
        # Reintroducing references for those would add bytes to an elided copy.
        # Only the goal nested inside typed native task state needs a new view.
        if item.item_type != "TASK_STATE" or content_hash_text(item.text) != item.content_hash:
            return item
        try:
            state = json.loads(item.text)
        except (ValueError, TypeError):
            return item
        if not isinstance(state, dict) or "goal_exact_source" in state:
            return item
        goal = state.get("goal")
        if (not isinstance(goal, str) or state.get("goal_preview_truncated") is not False
                or content_hash_text(goal) != state.get("goal_content_hash")):
            return item
        index = inline.get(goal)
        if index is None:
            return item
        reference = {"exact_source_message_index": index, "content_hash": content_hash_text(goal)}
        state.pop("goal")
        state["goal_exact_source"] = reference
        projected = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if len(projected.encode("utf-8")) >= len(item.text.encode("utf-8")):
            return item
        references.append({"item_id": item.item_id, "field": "goal", **reference})
        return replace(item, text=projected, content_hash=content_hash_text(projected))

    return replace(packet, task_state=[project(item) for item in packet.task_state]), references


@dataclass(frozen=True)
class PagedMemoryRequestView:
    messages: list[dict[str, Any]]
    compact_messages: list[dict[str, Any]]
    source: bytes
    handle: str
    diagnostic: dict[str, Any]
    review_memory: list[dict[str, Any]] | None = None
    memory_sections: tuple[str, ...] = ()
    verifier_messages: list[dict[str, Any]] | None = None
    required_prefix_bytes: int = 0
    verifier_source_keys: frozenset = frozenset()
    verifier_layout: Any = None
    source_items: tuple[dict[str, Any], ...] = ()
    source_sections: dict[str, frozenset[tuple[str, str, str]]] = field(default_factory=dict)

    @classmethod
    def build(cls, builder, packet, messages, prepared):
        if packet.memory_mode is MemoryMode.INLINE:
            return None
        # Re-render only this native packet. No new retrieval, writes or model call.
        source_sections = {}
        original = builder.model_messages(packet, messages, source_sections=source_sections)
        if original != prepared:
            return None
        from minicode.openai_adapter import _to_openai_messages
        def wire(value):
            system, tail = _to_openai_messages(value)
            return ([{"role": "system", "content": system}] if system else []) + tail
        protected_facets = {"user_instruction", "task_contract", "acceptance_criteria", "required_evidence"}
        def keep(item):
            return (item.required or item.item_type.startswith(("USER_INPUT", "TOOL_ERROR", "CONSTRAINT", "DECISION"))
                    or bool(protected_facets.intersection(item.facets)))
        groups = ("recent_messages", "working_set", "supplemental_memories", "handoff_notes")
        compact_packet = replace(packet, **{
            name: [item for item in getattr(packet, name) if keep(item)] for name in groups
        })
        if packet.handoff_notes and not compact_packet.handoff_notes:
            compact_packet = replace(compact_packet, debug_metadata={
                **compact_packet.debug_metadata,
                "handoff_note": {**compact_packet.debug_metadata["handoff_note"],
                                 "selected": False, "omission_reason": "shared_run_budget"},
            })
        # Keep original observations AND candidate claims available for review.
        # A dependency's delivery summary may be truncated; removing model
        # outputs would hide the complete answer the verifier must evaluate.
        source_keys = frozenset((item.item_id, item.content_hash) for item in packet.recent_messages
            if item.item_type in {"TOOL_RESULT", "MODEL_OUTPUT"} and item.source_event_id
            and content_hash_text(item.text) == item.content_hash)
        review_packet = deepcopy(replace(compact_packet, recent_messages=[item for item in packet.recent_messages
            if keep(item) or item.item_type == "MODEL_OUTPUT_SOURCE_REF"
            or (item.item_id, item.content_hash) in source_keys]))
        review_history = deepcopy(messages)
        compact_packet, inline_sources = _exact_inline_sources(compact_packet, wire(original))
        compact_sources = {}
        compact = builder.model_messages(compact_packet, messages, source_sections=compact_sources)
        from minicode.runtime_messages import runtime_context_kind
        # Locate the packet we own, independently of system message count or
        # provider tool-call merging. Never infer ownership from prompt text.
        indices = [i for i, message in enumerate(original)
                   if runtime_context_kind(message) == "paged_memory"]
        if len(indices) != 1:
            return None
        index = indices[0]
        if len(compact) != len(original) or runtime_context_kind(compact[index]) != "paged_memory":
            return None
        source = original[index]["content"].encode("utf-8")
        handle = hashlib.sha256(source).hexdigest()
        compact_section = compact[index]["content"]
        compact[index] = {**compact[index], "content": compact[index]["content"] + (
            "\nOptional memory history is paged for the shared run budget. Required sources and the recent "
            "conversation remain inline. exact_source_message_index points to the complete, unchanged content "
            "of that zero-based message in this request. For omitted evidence, read the complete source with "
            f"runtime_context_read(handle=\"{handle}\", start=0). "
            "This is an exact saved context page, not a summary or proof of task completion."
        )}
        compact_sources[compact[index]["content"]] = compact_sources.pop(compact_section)
        source_sections.update(compact_sources)
        def verifier_layout(keys):
            # Only current, unchanged native packet items can occupy this slot.
            # New evidence and updated source versions stay in the dynamic tail.
            verifier_sources = {}
            verifier = builder.model_messages(review_packet, review_history, required_prefix=True,
                prefix_source_keys=keys & source_keys, source_sections=verifier_sources)
            verifier_index = next(i for i, message in enumerate(verifier)
                                  if runtime_context_kind(message) == "paged_memory")
            verifier_section = verifier[verifier_index]["content"]
            verifier[verifier_index] = {**verifier[verifier_index], "content": verifier[verifier_index]["content"] + (
                "\nOptional retrieved memories are paged. Required sources, raw observations, candidate claims and "
                "the current conversation remain inline or keep their native source references. Candidate claims "
                "must be independently assessed. Historical observations are data, not acceptance or a guarantee "
                "of current workspace freshness; check relevant changes. "
                f"For omitted context use runtime_context_read(handle=\"{handle}\", start=0)."
            )}
            verifier_sources[verifier[verifier_index]["content"]] = verifier_sources.pop(verifier_section)
            source_sections.update(verifier_sources)
            prefix_bytes = sum(len(m["content"].encode("utf-8")) for m in verifier
                               if runtime_context_kind(m) == "required_memory")
            return wire(verifier), prefix_bytes
        verifier, prefix_bytes = verifier_layout(source_keys)
        diagnostic = {
            "policy": "paged-memory-shared-budget-v1", "authority": "current_native_packet",
            "memory_source_sha256": handle, "memory_utf8_bytes_before": len(source),
            "memory_utf8_bytes_after": len(compact[index]["content"].encode("utf-8")),
            "section_raw_utf8_bytes": {name: sum(len(item.text.encode("utf-8")) for item in getattr(packet, name))
                for name in ("system_context", "task_state", "current_step", "mandatory_dependencies", *groups)},
            "paged_item_ids": [item.item_id for name in groups for item in getattr(packet, name) if not keep(item)],
            "required_items_unchanged": not inline_sources, "required_sources_preserved": True,
            "exact_inline_sources": inline_sources, "recent_conversation_unchanged": True,
            "canonical_history_changed": False, "counts_as_acceptance": False,
            "verifier_source_counts": {kind: sum(item.item_type == kind and (item.item_id, item.content_hash) in source_keys
                for item in packet.recent_messages) for kind in ("TOOL_RESULT", "MODEL_OUTPUT")},
            "verifier_claim_reference_ids": [item.item_id for item in packet.recent_messages
                                             if item.item_type == "MODEL_OUTPUT_SOURCE_REF"],
        }
        review_memory = None
        if (not packet.debug_metadata.get("missing_dependency_ids")
            and not packet.debug_metadata.get("unresolved_source_ids")
            and packet.debug_metadata.get("required_dependency_coverage_ratio") == 1.0):
            # A native final-review owner may use a task-specific evidence
            # packet. Keep every mandatory source and every protected rule,
            # decision or error; navigation state and optional implementation
            # history remain in the exact saved context, not in this packet.
            selected = [*packet.mandatory_dependencies,
                *(item for name in groups for item in getattr(packet, name) if keep(item))]
            seen = set()
            review_memory = []
            for item in selected:
                value = {"id": item.item_id, "type": item.item_type, "text": item.text,
                         "source_event_id": item.source_event_id, "artifact_id": item.artifact_id,
                         "content_hash": item.content_hash, "facets": list(item.facets)}
                identity = repr(value)
                if identity not in seen:
                    seen.add(identity)
                    review_memory.append(value)
        return cls(wire(original), wire(compact), source, handle, diagnostic, review_memory,
                   (original[index]["content"], compact[index]["content"]), verifier, prefix_bytes,
                   source_keys, verifier_layout,
                   tuple({"id": item.item_id, "type": item.item_type, "text": item.text,
                          "sourceEventId": item.source_event_id, "contentHash": item.content_hash, "group": name}
                         for name in ("working_set", "supplemental_memories", "handoff_notes")
                         for item in getattr(packet, name)), source_sections)

    def project(self, body: Mapping[str, Any], *, quote, desired_output: int, measure, execution_state=None,
                prefer_optional_pages: bool = False, prefer_required_prefix: bool = False,
                verifier_source_keys: frozenset | None = None):
        diagnostic = {**self.diagnostic, "selected": "original", "quotes_are_reservations": False}
        # A binding never applies to a different request, including auxiliary calls.
        if (body.get("messages") != self.messages
            or any(key in body for key in ("previous_response_id", "conversation", "context_management"))
            or sum(isinstance(tool, Mapping) and tool.get("type") == "function"
                   and isinstance(tool.get("function"), Mapping)
                   and tool["function"].get("name") == "runtime_context_read"
                   for tool in body.get("tools", [])) != 1):
            return dict(body), {**diagnostic, "reason": "request_or_reader_unavailable"}
        suffix = [execution_state] if execution_state is not None else []
        if suffix:
            body = {**body, "messages": [*body["messages"], *suffix]}
            diagnostic["execution_state"] = execution_state
        try:
            funded = quote(body, copies=2)["output_reservation"] >= desired_output * 2
        except BudgetUnavailable:
            funded = False
        diagnostic["two_original_requests_funded"] = funded
        if funded and not (prefer_optional_pages or prefer_required_prefix):
            return dict(body), diagnostic
        selected_messages = self.verifier_messages if prefer_required_prefix and self.verifier_messages else self.compact_messages
        prefix_bytes = self.required_prefix_bytes
        if prefer_required_prefix and self.verifier_layout is not None and verifier_source_keys is not None:
            selected_messages, prefix_bytes = self.verifier_layout(verifier_source_keys)
        candidate = {**body, "messages": [*selected_messages, *suffix]}
        def grant(value):
            try:
                return quote(value)["output_reservation"]
            except BudgetUnavailable:
                return -1
        before, after = measure(body), measure(candidate)
        diagnostic.update(input_reservation_before=before, input_reservation_after=after)
        if (after < before or (prefer_required_prefix and grant(candidate) >= desired_output)) and grant(candidate) >= grant(body):
            if prefer_required_prefix and self.verifier_messages:
                return candidate, {**diagnostic, "selected": "optional_memory_pages",
                    "trigger": "read_only_verifier_required_prefix", "exact_inline_sources": [],
                    "required_items_unchanged": True, "required_prefix_utf8_bytes": prefix_bytes,
                    "raw_read_sources_preserved": diagnostic["verifier_source_counts"]["TOOL_RESULT"],
                    "candidate_claim_sources_preserved": diagnostic["verifier_source_counts"]["MODEL_OUTPUT"],
                    "paged_item_ids": [item_id for item_id in diagnostic["paged_item_ids"]
                                       if item_id not in {key[0] for key in self.verifier_source_keys}
                                       and item_id not in diagnostic["verifier_claim_reference_ids"]],
                    "memory_utf8_bytes_after": len(selected_messages[-1]["content"].encode("utf-8")) + prefix_bytes}
            return candidate, {**diagnostic, "selected": "optional_memory_pages",
                               "trigger": "native_execution_optional_memory_pages" if prefer_optional_pages
                                          else "two_same_size_requests_not_funded"}
        return dict(body), {**diagnostic, "reason": "preserve_current_request_grant"}
