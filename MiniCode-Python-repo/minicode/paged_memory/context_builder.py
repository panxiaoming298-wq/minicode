"""Build source-linked context packets for INLINE/WORKING_SET/PAGED_MEMORY."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .config import PagedMemoryConfig
from .models import (
    MEMORY_NAVIGATION_TOOL_NAMES,
    ContextItem,
    ContextPacket,
    EventType,
    MemoryMode,
    MemorySearchRequest,
    TaskMetrics,
    content_hash_text,
)
from .policy import MemoryModePolicy, TokenBudgetManager
from .retrieval import (
    DependencyResolver,
    HybridMemoryRetriever,
    MinimumCoverSelector,
    WorkingSetManager,
    memory_to_item,
    merge_state_dependencies,
)


class PagedContextBuilder:
    def __init__(
        self,
        *,
        authority: Any,
        config: PagedMemoryConfig,
        mode_policy: MemoryModePolicy,
        token_budget: TokenBudgetManager,
        retriever: HybridMemoryRetriever,
        dependency_resolver: DependencyResolver,
        working_set: WorkingSetManager,
    ) -> None:
        self.authority = authority
        self.config = config
        self.mode_policy = mode_policy
        self.token_budget = token_budget
        self.retriever = retriever
        self.dependency_resolver = dependency_resolver
        self.working_set = working_set

    def build(
        self,
        *,
        tenant_id: str,
        task_id: str,
        run_id: str,
        step_id: str,
        user_input: str,
        metrics: TaskMetrics,
        current_mode: MemoryMode,
        model_context_limit: int,
        system_context: str = "",
        acceptance_criteria: Mapping[str, Any] | None = None,
        required_facets: Sequence[str] = (),
        requested_mode: MemoryMode | None = None,
        phase_boundary: bool = False,
    ) -> ContextPacket:
        self.retriever.reset_observation()
        state = self.authority.get_state(tenant_id=tenant_id, task_id=task_id)
        selection = self.mode_policy.choose_mode(
            metrics,
            current_mode,
            task_id=task_id,
            manual_override=requested_mode,
            phase_boundary=phase_boundary,
        )
        mode = selection.mode
        available_budget = max(
            512,
            model_context_limit
            - self.config.output_token_reserve
            - self.config.tool_token_reserve
            - self.config.safety_token_reserve,
        )

        system_items: list[ContextItem] = []
        if system_context:
            system_items.append(
                ContextItem(
                    item_id="system_contract",
                    item_type="SYSTEM",
                    text=system_context,
                    priority=120,
                    required=True,
                    content_hash=content_hash_text(system_context),
                    reason_loaded="agent contract",
                )
            )
        state_items: list[ContextItem] = []
        if state is not None:
            state_view = state.to_dict()
            goal = str(state_view.get("goal", ""))
            state_view["goal"] = goal[:1_000]
            state_view["goal_content_hash"] = content_hash_text(goal)
            state_view["goal_characters"] = len(goal)
            state_view["goal_preview_truncated"] = len(goal) > 1_000
            for field_name in (
                "completed_steps",
                "pending_steps",
                "active_artifacts",
                "blocked_by",
            ):
                values = list(state_view.pop(field_name, []) or [])
                state_view[f"{field_name}_count"] = len(values)
                state_view[f"{field_name}_recent"] = values[-20:]
            state_view["active_constraints"] = [
                {
                    key: value
                    for key, value in constraint.items()
                    if key in {"memory_id", "source_event_id", "status"}
                }
                for constraint in state.active_constraints
                if isinstance(constraint, dict)
            ]
            state_view["active_decisions"] = {
                key: {
                    field: value
                    for field, value in decision.items()
                    if field in {"id", "decision_id", "source_event_id", "status"}
                }
                for key, decision in state.active_decisions.items()
                if isinstance(decision, dict)
            }
            state_text = json.dumps(state_view, ensure_ascii=False, sort_keys=True)
            state_items.append(
                ContextItem(
                    item_id=f"task_state:{state.version}",
                    item_type="TASK_STATE",
                    text=state_text,
                    priority=115,
                    required=True,
                    content_hash=content_hash_text(state_text),
                    reason_loaded="authoritative current task state",
                )
            )
        step_text = json.dumps(
            {
                "step_id": step_id,
                # The current user message remains verbatim in the provider
                # tail.  Repeating it here wastes the hard input budget.
                "user_input_hash": content_hash_text(user_input),
                "user_input_characters": len(user_input),
                "acceptance_criteria": dict(acceptance_criteria or {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        step_items = [
            ContextItem(
                item_id=step_id,
                item_type="CURRENT_STEP",
                text=step_text,
                priority=115,
                required=True,
                content_hash=content_hash_text(step_text),
                reason_loaded="current step",
            )
        ]

        dependencies = self.dependency_resolver.resolve_closure(
            tenant_id=tenant_id, task_id=task_id, step_id=step_id
        )
        dependencies = merge_state_dependencies(
            dependencies,
            state=state,
            tenant_id=tenant_id,
            task_id=task_id,
            step_id=step_id,
        )
        mandatory, missing_dependencies, unresolved_sources = (
            self.dependency_resolver.load_items(
                tenant_id=tenant_id, task_id=task_id, dependencies=dependencies
            )
        )
        paged_required_sources = 0
        for item in mandatory:
            if (
                self.token_budget.estimate_text(item.text)
                <= self.config.raw_page_token_limit
            ):
                continue
            if item.artifact_id and item.source_range is not None:
                read_hint = f"memory_get(memory_id={item.item_id!r}, start=0)"
            elif item.artifact_id:
                read_hint = f"artifact_read(artifact_id={item.artifact_id!r}, start=0)"
            elif item.source_event_id:
                read_hint = (
                    f"memory_get(source_event_id={item.source_event_id!r}, start=0)"
                )
            else:
                # A required value with no resolvable source will already fail
                # dependency coverage/preflight; retaining its text here makes
                # the diagnosis explicit.
                continue
            original_tokens = self.token_budget.estimate_text(item.text)
            item.text = (
                "Required source verified but paged out of the hot window; "
                f"content_hash={item.content_hash}; estimated_tokens={original_tokens}; "
                f"read exact ranges with {read_hint}."
            )
            item.item_type = f"{item.item_type}_SOURCE_REF"
            item.estimated_tokens = 0
            item.reason_loaded = (
                "required source reference; raw text remains authoritative"
            )
            paged_required_sources += 1

        recent_items: list[ContextItem] = []
        recent_events = self.authority.recent_events(
            tenant_id=tenant_id,
            task_id=task_id,
            limit=100,
            event_types=(
                EventType.USER_INPUT.value,
                EventType.MODEL_OUTPUT.value,
                EventType.TOOL_RESULT.value,
                EventType.TOOL_ERROR.value,
            ),
        )
        recent_budget = (
            self.config.inline_recent_token_budget
            if mode == MemoryMode.INLINE
            else self.config.recent_event_token_budget
        )
        used_recent = 0
        for event in reversed(recent_events):
            if (
                event.event_type in {EventType.TOOL_RESULT, EventType.TOOL_ERROR}
                and str(event.content_json.get("tool_name", ""))
                in MEMORY_NAVIGATION_TOOL_NAMES
            ):
                continue
            text = event.content_text or (
                f"{event.event_type.value}: artifact={event.artifact_uri}; hash={event.content_hash}"
            )
            tokens = self.token_budget.estimate_text(text)
            item_type = event.event_type.value
            reason_loaded = "recent high-fidelity event"
            if used_recent + tokens > recent_budget:
                memory_id = str(event.content_json.get("memory_id", ""))
                text = (
                    f"raw event is outside the hot-page budget; event_id={event.id}; "
                    f"memory_id={memory_id or 'search-required'}; "
                    f"content_hash={event.content_hash}; token_count={event.token_count}"
                )
                tokens = self.token_budget.estimate_text(text)
                item_type = f"{event.event_type.value}_SOURCE_REF"
                reason_loaded = (
                    "recent source reference; raw text remains authoritative"
                )
                if used_recent + tokens > recent_budget:
                    continue
            recent_items.append(
                ContextItem(
                    item_id=event.id,
                    item_type=item_type,
                    text=text,
                    priority=75,
                    required=False,
                    estimated_tokens=tokens,
                    source_event_id=event.id,
                    content_hash=event.content_hash,
                    reason_loaded=reason_loaded,
                )
            )
            used_recent += tokens
            if used_recent >= recent_budget:
                break
        recent_items.reverse()

        working_items: list[ContextItem] = []
        supplemental_items: list[ContextItem] = []
        phase = state.phase if state else metrics.current_phase
        if mode in {MemoryMode.WORKING_SET, MemoryMode.PAGED_MEMORY}:
            working_records = self.working_set.get(
                tenant_id=tenant_id, task_id=task_id, phase=phase
            )
            for record in working_records:
                working_items.append(
                    memory_to_item(record, priority=70, reason="phase working set")
                )

            request = MemorySearchRequest(
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                query="\n".join(
                    filter(None, [state.goal if state else "", phase, user_input])
                ),
                phase=phase,
                candidate_limit=self.config.candidate_limit,
                required_facets=tuple(required_facets),
            )
            candidates = self.retriever.search(
                request,
                use_vector=mode == MemoryMode.PAGED_MEMORY,
            )
            selected_records = MinimumCoverSelector(self.token_budget).select(
                candidates,
                required_facets=required_facets,
                token_budget=self.config.supplemental_token_budget,
            )
            supplemental_items = [
                memory_to_item(
                    record,
                    priority=45,
                    reason=(
                        "Milvus + lexical supplemental retrieval"
                        if mode == MemoryMode.PAGED_MEMORY
                        else "authority lexical supplemental retrieval"
                    ),
                )
                for record in selected_records
            ]
            if state and selected_records:
                self.working_set.touch(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    phase=phase,
                    memory_ids=[record.id for record in selected_records],
                    state_version=state.version,
                )

        working_candidate_count = len(working_items)
        working_omitted: list[dict[str, Any]] = []
        if working_items:
            working_items, working_omitted, _working_budget = self.token_budget.pack(
                working_items,
                token_budget=self.config.working_set_token_budget,
            )

        artifacts = self.authority.list_artifacts(
            tenant_id=tenant_id, task_id=task_id, statuses=("ACTIVE", "PENDING_UPLOAD")
        )[:50]
        artifact_manifest_entries = [
            {
                "artifact_id": artifact.id,
                "type": artifact.artifact_type,
                "media_type": artifact.media_type,
                "size": artifact.size_bytes,
                "content_hash": artifact.content_hash,
                "status": artifact.status.value,
            }
            for artifact in artifacts
        ]
        artifact_items = [
            ContextItem(
                item_id=f"artifact:{entry['artifact_id']}",
                item_type="ARTIFACT_REF",
                text=json.dumps(entry, ensure_ascii=False, sort_keys=True),
                priority=35,
                required=False,
                artifact_id=str(entry["artifact_id"]),
                content_hash=str(entry["content_hash"]),
                reason_loaded="active Artifact manifest",
            )
            for entry in artifact_manifest_entries
        ]

        all_items = [
            *system_items,
            *state_items,
            *step_items,
            *mandatory,
            *recent_items,
            *working_items,
            *supplemental_items,
            *artifact_items,
        ]
        selected_items, omitted, budget_metadata = self.token_budget.pack(
            all_items, token_budget=available_budget
        )
        omitted = [*working_omitted, *omitted]
        selected_ids = {id(item) for item in selected_items}

        def selected_context_items(
            values: Sequence[ContextItem],
        ) -> list[ContextItem]:
            return [item for item in values if id(item) in selected_ids]

        required_count = sum(1 for dependency in dependencies if dependency.required)
        failed_required = set(missing_dependencies) | set(unresolved_sources)
        resolved_count = max(0, required_count - len(failed_required))
        coverage = 1.0 if required_count == 0 else resolved_count / required_count
        required_facet_set = set(required_facets)
        covered_facets = (
            set().union(
                *(
                    set(item.facets)
                    for item in [*mandatory, *working_items, *supplemental_items]
                )
            )
            if (mandatory or working_items or supplemental_items)
            else set()
        )
        facet_coverage = (
            1.0
            if not required_facet_set
            else len(required_facet_set & covered_facets) / len(required_facet_set)
        )
        selected_artifact_ids = {
            item.artifact_id for item in selected_context_items(artifact_items)
        }
        packet = ContextPacket(
            memory_mode=mode,
            system_context=selected_context_items(system_items),
            task_state=selected_context_items(state_items),
            current_step=selected_context_items(step_items),
            mandatory_dependencies=selected_context_items(mandatory),
            recent_messages=selected_context_items(recent_items),
            working_set=selected_context_items(working_items),
            supplemental_memories=selected_context_items(supplemental_items),
            artifact_manifest=[
                entry
                for entry in artifact_manifest_entries
                if entry["artifact_id"] in selected_artifact_ids
            ],
            estimated_input_tokens=budget_metadata["selected_tokens"],
            token_budget=available_budget,
            omitted_items=omitted,
            source_references=[
                {
                    "item_id": item.item_id,
                    "source_event_id": item.source_event_id,
                    "artifact_id": item.artifact_id,
                    "content_hash": item.content_hash,
                }
                for item in selected_items
                if item.source_event_id or item.artifact_id
            ],
            debug_metadata={
                "mode_reason": selection.reason,
                "mode_changed": selection.changed,
                "required_dependency_count": required_count,
                "resolved_required_dependency_count": resolved_count,
                "required_dependency_coverage_ratio": coverage,
                "missing_dependency_ids": missing_dependencies,
                "unresolved_source_ids": unresolved_sources,
                "paged_required_source_count": paged_required_sources,
                "required_facet_coverage_ratio": facet_coverage,
                "retrieved_candidate_count": self.retriever.last_candidate_count,
                "selected_memory_count": len(supplemental_items),
                "duplicate_memory_count": sum(
                    1 for item in omitted if item.get("reason") == "duplicate"
                ),
                "working_set_candidate_count": working_candidate_count,
                "working_set_item_count": len(working_items),
                "milvus_latency_ms": self.retriever.last_vector_latency_ms,
                "lexical_latency_ms": self.retriever.last_lexical_latency_ms,
                "supplemental_retrieval_degraded": bool(
                    self.retriever.last_vector_error
                ),
                "supplemental_retrieval_error": self.retriever.last_vector_error,
                "vector_candidate_mismatch_count": len(
                    self.retriever.last_vector_mismatch_ids
                ),
                "vector_candidate_mismatch_ids": list(
                    self.retriever.last_vector_mismatch_ids[:20]
                ),
                "token_estimated": self.token_budget.estimated,
                **budget_metadata,
            },
        )
        return packet

    def render_memory_context(
        self,
        packet: ContextPacket,
        *,
        excluded_recent_content_hashes: Sequence[str] = (),
    ) -> str:
        excluded_hashes = set(excluded_recent_content_hashes)
        visible_recent = [
            item
            for item in packet.recent_messages
            if item.content_hash not in excluded_hashes
        ]
        sections: list[str] = [
            "[Paged memory context]",
            f"mode={packet.memory_mode.value}",
            "Summaries and cards below are navigation aids. Source IDs are authoritative.",
            (
                "For omitted detail, use memory_search, memory_get, artifact_read, "
                "or checkpoint_restore; reads are bounded and scoped to this task."
            ),
            (
                "Reuse memory evidence already present. Never repeat an equivalent "
                "memory_search or reread the same source range."
            ),
        ]
        groups = (
            ("Task state", packet.task_state),
            ("Current step", packet.current_step),
            ("Required dependencies", packet.mandatory_dependencies),
            ("Recent source events", visible_recent),
            ("Working set", packet.working_set),
            ("Supplemental memories", packet.supplemental_memories),
        )
        for title, items in groups:
            if not items:
                continue
            sections.append(f"\n## {title}")
            for item in items:
                source = item.source_event_id or item.artifact_id or "structured-state"
                sections.append(
                    f"- {item.item_type} {item.item_id} source={source}: {item.text}"
                )
        if packet.artifact_manifest:
            sections.append("\n## Artifact manifest")
            for artifact in packet.artifact_manifest:
                sections.append(
                    "- " + json.dumps(artifact, ensure_ascii=False, sort_keys=True)
                )
        missing = packet.debug_metadata.get("missing_dependency_ids", [])
        if missing:
            sections.append("\nMISSING REQUIRED DEPENDENCIES: " + ", ".join(missing))
        return "\n".join(sections)

    def model_messages(
        self,
        packet: ContextPacket,
        messages: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if packet.memory_mode == MemoryMode.INLINE:
            return [dict(message) for message in messages]
        system = [
            dict(message) for message in messages if message.get("role") == "system"
        ]
        non_system = [
            dict(message) for message in messages if message.get("role") != "system"
        ]
        tail = self.token_budget.recent_messages(
            non_system,
            token_budget=self.config.recent_event_token_budget,
        )
        tail_hashes = {
            content_hash_text(str(message.get("content", "")))
            for message in tail
            if message.get("role") in {"user", "assistant", "assistant_progress"}
        }
        memory_message = {
            "role": "system",
            # Do not inject a persisted event a second time when its exact raw
            # message is already present in the provider-valid recent tail.
            "content": self.render_memory_context(
                packet, excluded_recent_content_hashes=tuple(tail_hashes)
            ),
        }
        return [*system, memory_message, *tail]
