"""Build source-linked context packets for INLINE and PAGED_MEMORY."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from .config import PagedMemoryConfig
from .handoff import visible_note_content
from .rotation import project_history
from .inline_sources import InlineSourceCoverage, covered_at_render
from .context_retrieval import retrieve_supplemental
from .context_pressure import measure_pressure
from .context_utilization import pressure_status
from .context_allocation import allocate_sections, optional_capacity, section_bases
from .tool_result_projection import project_results, protected_item
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
from .query import retrieval_query
from .reranking import OptionalMemoryReranker
from .retrieval import (
    DependencyResolver,
    LexicalMemoryRetriever,
    WorkingSetManager,
    dependency_contract_hash,
    memory_to_item,
    merge_state_dependencies,
)
from .search_budget import authority_deadline


class PagedContextBuilder:
    def __init__(
        self,
        *,
        authority: Any,
        config: PagedMemoryConfig,
        mode_policy: MemoryModePolicy,
        token_budget: TokenBudgetManager,
        retriever: LexicalMemoryRetriever,
        dependency_resolver: DependencyResolver,
        working_set: WorkingSetManager,
        reranker: OptionalMemoryReranker | None = None,
        handoff_notes: Any | None = None,
        context_rotation: Any | None = None,
        code_focus: Any | None = None,
        context_tool_names: Any | None = None,
    ) -> None:
        self.authority = authority
        self.config = config
        self.mode_policy = mode_policy
        self.token_budget = token_budget
        self.retriever = retriever
        self.dependency_resolver = dependency_resolver
        self.working_set = working_set
        self.reranker = reranker
        self.handoff_notes = handoff_notes
        self.context_rotation = context_rotation
        self.code_focus = code_focus
        self.context_tool_names = context_tool_names
        self._query_cache = None

    def _retrieval_query(self, user_input, *, goal, phase, focus=()):
        # Cache only deterministic query construction, never search results or
        # source validity. One entry retains no growing history of user text.
        key = (user_input, goal, phase, tuple(focus))
        cached = self._query_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        query = retrieval_query(user_input, goal=goal, phase=phase, focus=focus)
        self._query_cache = (key, query)
        return query

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
        inline_content_hashes: Mapping[str, Sequence[str]] | None = None,
        history_messages: Sequence[Mapping[str, Any]] | None = None,
        tool_schema_tokens: int = 0,
        request_budget: Any | None = None,
    ) -> ContextPacket:
        self.retriever.reset_observation()
        adaptive_observation = {"enabled": self.config.adaptive_retrieval, "rounds": []}
        if self.reranker is not None:
            self.reranker.reset_observation()
        state = self.authority.get_state(tenant_id=tenant_id, task_id=task_id)
        selection = None if self.config.context_mode_policy_enabled else self.mode_policy.choose_mode(
            metrics,
            current_mode,
            task_id=task_id,
            manual_override=requested_mode,
            phase_boundary=phase_boundary,
        )
        # Verify one note/rotation snapshot before the new policy can downgrade.
        # Explicit configuration still has its original priority.
        mode = (selection.mode if selection is not None else
                requested_mode or self.config.manual_mode or
                (MemoryMode.PAGED_MEMORY if self.config.auto_mode else self.config.default_mode)).canonical
        note_items, note_info = (self.handoff_notes.prepare(
            state, user_input=user_input, mode=mode, phase_boundary=phase_boundary,
            context_usage_ratio=metrics.context_usage_ratio,
        ) if self.handoff_notes is not None else ([], {}))
        rotation_info = (self.context_rotation.prepare(state, note_items, note_info, mode)
                         if self.context_rotation is not None else {})
        if rotation_info.get("status") == "active":
            if self.context_rotation.historical_note_items:
                note_items = self.context_rotation.historical_note_items
            note_items = [replace(item, required=True) for item in note_items]
            # The caller measured the canonical tail. Do not claim an old source
            # is inline after projection; normal exact-body rendering still dedups.
            inline_content_hashes = None
        available_budget = max(
            0,
            model_context_limit
            - self.config.output_token_reserve
            - max(self.config.tool_token_reserve, tool_schema_tokens)
            - self.config.safety_token_reserve,
        )
        if request_budget is not None:
            available_budget = max(0, request_budget.input_limit
                - max(self.config.tool_token_reserve, tool_schema_tokens))

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
            # Never expose other Workers' note heads or duplicate the note body.
            state_view.pop("handoff_notes", None)
            state_view.pop("context_rotations", None)
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
            if item.item_type == "CONSTRAINT" or {
                "user_instruction",
                "task_contract",
            }.intersection(item.facets):
                # User rules are executable semantics, not pageable attachments.
                # The hard provider guard stops if their complete text cannot fit.
                continue
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

        effective_metrics, pressure_info = measure_pressure(
            metrics=metrics, messages=history_messages, rotation=rotation_info, state=state,
            required_items=[*(system_items if history_messages is None else []),
                            *state_items, *step_items, *mandatory, *note_items],
            config=self.config, token_budget=self.token_budget,
            model_context_limit=model_context_limit, tool_tokens=tool_schema_tokens,
            request_budget=request_budget,
        )
        utilization = pressure_status(self.config, pressure_info)
        if selection is None:
            selection = self.mode_policy.choose_mode(
                effective_metrics, current_mode, task_id=task_id,
                manual_override=requested_mode, phase_boundary=phase_boundary,
                observation_id=f"{run_id}:{step_id}:{state.phase if state else metrics.current_phase}",
                minimum_mode=(MemoryMode.PAGED_MEMORY if rotation_info.get("status") == "active"
                              else MemoryMode.INLINE),
            )
            metrics.mode_downgrade_streak = effective_metrics.mode_downgrade_streak
            metrics.mode_downgrade_observation_id = effective_metrics.mode_downgrade_observation_id
            metrics.rerank_eligible = effective_metrics.rerank_eligible
            metrics.rerank_downgrade_streak = effective_metrics.rerank_downgrade_streak
            metrics.rerank_downgrade_observation_id = effective_metrics.rerank_downgrade_observation_id
            mode = selection.mode
            if note_info:
                note_info["update_suggested"] = (note_info.get("status") == "stale" or phase_boundary
                    or (utilization.get("stage") in {"write_note", "rotate", "protect"}
                        if self.config.context_utilization_enabled else
                        effective_metrics.context_usage_ratio >= self.config.paged_context_ratio))
            if mode == MemoryMode.INLINE:
                note_items, note_info, rotation_info = [], {}, {}
                if self.handoff_notes is not None:
                    self.handoff_notes.prepared = False

        candidate_budget = None
        if self.config.context_utilization_enabled and mode != MemoryMode.INLINE:
            _, candidate_budget = optional_capacity(self.config, self.token_budget,
                fixed_items=[*system_items, *state_items, *step_items, *mandatory, *note_items],
                messages=history_messages, available_budget=available_budget, pressure=pressure_info)
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
            else (self.config.recent_memory_token_budget if self.config.elastic_context_budget_enabled
                  else self.config.recent_event_token_budget)
        )
        if candidate_budget is not None:
            # Preserve candidates until all sections can share the real window.
            # A 1500-token base must not turn a useful source into a locator
            # before the final allocator has an opportunity to admit its text.
            recent_budget = candidate_budget
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
        retrieval_deadline = None
        inline_coverage = None
        if mode is MemoryMode.PAGED_MEMORY:
            retrieval_deadline = (
                time.monotonic() + self.config.context_retrieval_timeout_seconds
            )
            inline_coverage = InlineSourceCoverage(
                self.authority, tenant_id=tenant_id, task_id=task_id,
                content_hashes=inline_content_hashes or {}, deadline=retrieval_deadline,
                mandatory_sources={item.source_event_id: item.content_hash for item in mandatory
                                   if item.source_event_id and content_hash_text(item.text) == item.content_hash},
            )
            try:
                with authority_deadline(self.authority, retrieval_deadline):
                    working_records = self.working_set.get(
                        tenant_id=tenant_id, task_id=task_id, phase=phase
                    )
            except TimeoutError:
                working_records = []
            for record in working_records:
                if inline_coverage.covers(record, required_facets=required_facets):
                    continue
                working_items.append(
                    memory_to_item(record, priority=70, reason="phase working set")
                )

            request = MemorySearchRequest(
                tenant_id=tenant_id,
                task_id=task_id,
                run_id=run_id,
                query=self._retrieval_query(
                    user_input, goal=state.goal if state else "", phase=phase,
                    focus=(getattr(self.handoff_notes, "retrieval_hints", ())
                           if note_info.get("status") == "current" else ()),
                ),
                phase=phase,
                candidate_limit=self.config.candidate_limit,
                required_facets=tuple(required_facets),
                deadline_monotonic=retrieval_deadline,
            )
            selected_records, adaptive_observation = retrieve_supplemental(
                authority=self.authority,
                retriever=self.retriever,
                reranker=self.reranker,
                config=self.config,
                token_budget=self.token_budget,
                request=request,
                user_input=user_input,
                rerank_allowed=selection.rerank_allowed,
                inline_coverage=inline_coverage,
                selection_token_budget=(candidate_budget if candidate_budget is not None else
                    min(available_budget, sum(section_bases(self.config).values()))
                    if self.config.elastic_context_budget_enabled else None),
            )
            supplemental_items = [
                memory_to_item(
                    record,
                    priority=45,
                    reason="SQLite keyword supplemental retrieval",
                )
                for record in selected_records
            ]
            if state and selected_records:
                try:
                    with authority_deadline(self.authority, retrieval_deadline):
                        self.working_set.touch(
                            tenant_id=tenant_id,
                            task_id=task_id,
                            phase=phase,
                            memory_ids=[record.id for record in selected_records],
                            state_version=state.version,
                        )
                except TimeoutError:
                    adaptive_observation["working_set_touch"] = "deadline"

        focus_items, focus_info = (self.code_focus.prepare(user_input)
            if self.code_focus is not None and mode != MemoryMode.INLINE else ([], {"status": "disabled"}))
        working_items = [*focus_items, *working_items]
        working_candidate_count = len(working_items)
        working_omitted: list[dict[str, Any]] = []
        if working_items and not self.config.elastic_context_budget_enabled:
            working_items, working_omitted, _working_budget = self.token_budget.pack(
                working_items,
                token_budget=self.config.working_set_token_budget,
            )

        try:
            with authority_deadline(self.authority, retrieval_deadline):
                artifacts = self.authority.list_artifacts(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    statuses=("ACTIVE", "PENDING_UPLOAD"),
                )[:50]
        except TimeoutError:
            artifacts = []
            adaptive_observation["artifact_manifest"] = "deadline"
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

        if self.context_rotation is not None:
            recent_items = self.context_rotation.filter_items(recent_items, rotation_info)
            working_items = self.context_rotation.filter_items(working_items, rotation_info)
            supplemental_items = self.context_rotation.filter_items(supplemental_items, rotation_info)
        allocation = {}
        if self.config.elastic_context_budget_enabled and mode != MemoryMode.INLINE:
            selected_sections, section_omitted, allocation = allocate_sections(
                self.config, self.token_budget,
                sections={"working_set": working_items, "supplemental_memories": supplemental_items,
                          "recent_messages": recent_items},
                fixed_items=[*system_items, *state_items, *step_items, *mandatory, *note_items, *artifact_items],
                messages=history_messages, rotation_info=rotation_info, available_budget=available_budget,
                pressure=pressure_info,
            )
            working_items = selected_sections["working_set"]
            supplemental_items = selected_sections["supplemental_memories"]
            recent_items = selected_sections["recent_messages"]
            working_omitted.extend(section_omitted)
        all_items = [
            *system_items,
            *state_items,
            *step_items,
            *mandatory,
            *recent_items,
            *working_items,
            *supplemental_items,
            *note_items,
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
            set().union(*(set(item.facets) for item in selected_items))
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
            handoff_notes=selected_context_items(note_items),
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
                "rerank_admission": {
                    "eligible": selection.rerank_allowed,
                    "enabled": self.config.rerank_enabled,
                    "reason": selection.rerank_reason,
                },
                "context_pressure": pressure_info,
                "context_utilization": utilization,
                "context_allocation": allocation,
                "code_focus": {**focus_info, "selected_body_ids": [item.item_id for item in
                    selected_context_items(working_items) if item.item_type == "CODE_FOCUS"]},
                "required_dependency_count": required_count,
                "resolved_required_dependency_count": resolved_count,
                "required_dependency_coverage_ratio": coverage,
                "missing_dependency_ids": missing_dependencies,
                "unresolved_source_ids": unresolved_sources,
                "paged_required_source_count": paged_required_sources,
                "required_facet_coverage_ratio": facet_coverage,
                "required_facets": list(required_facets),
                "retrieved_candidate_count": self.retriever.last_candidate_count,
                "selected_memory_count": len(
                    selected_context_items(supplemental_items)
                ),
                "inline_source_covered_memory_ids": sorted(inline_coverage.omitted_ids) if inline_coverage else [],
                "inline_source_covered_sources": dict(inline_coverage.omitted_sources) if inline_coverage else {},
                "duplicate_memory_count": sum(
                    1 for item in omitted if item.get("reason") == "duplicate"
                ),
                "working_set_candidate_count": working_candidate_count,
                "working_set_item_count": len(selected_context_items(working_items)),
                "required_contract_hash": dependency_contract_hash(dependencies, state),
                "lexical_latency_ms": self.retriever.last_lexical_latency_ms,
                "retrieval_refill_count": self.retriever.last_refill_count,
                "adaptive_retrieval": adaptive_observation,
                "rerank": dict(self.reranker.observation)
                if self.reranker is not None
                else {"status": "disabled"},
                "supplemental_retrieval_degraded": bool(
                    self.retriever.last_lexical_error
                    or adaptive_observation.get("stop_reason")
                    == "context_retrieval_deadline"
                ),
                "supplemental_retrieval_error": self.retriever.last_lexical_error,
                "token_estimated": self.token_budget.estimated,
                **({"handoff_note": {
                    **note_info, "selected": bool(selected_context_items(note_items)),
                }} if note_info else {}),
                **({"context_rotation": rotation_info} if rotation_info else {}),
                **budget_metadata,
            },
        )
        return packet

    def render_memory_context(
        self,
        packet: ContextPacket,
        *,
        excluded_recent_content_hashes: Sequence[str] = (),
        visible_tool_content: Mapping[str, Sequence[str]] | None = None,
        visible_handoff_content: Mapping[str, Sequence[str]] | None = None,
        projected_tool_sources: set[str] | None = None,
        visible_source_content: Mapping[str, Sequence[str]] | None = None,
        rendered_sources: set[tuple[str, str, str]] | None = None,
    ) -> str:
        excluded_hashes = set(excluded_recent_content_hashes)
        rendered_hashes = set(excluded_hashes)
        visible_recent = [
            item
            for item in packet.recent_messages
            if item.content_hash not in excluded_hashes
        ]
        available = self.context_tool_names() if self.context_tool_names is not None else None
        def permits(name):
            return available is None or name in available
        memory_readers = [name for name in ("history_search", "memory_search", "memory_get", "artifact_read", "checkpoint_restore") if permits(name)]
        no_handoff = ("Note writing/rotation is unavailable in this tool scope. No housekeeping call is needed to finish. "
            "Use available bounded reads for missing evidence; if essential evidence cannot fit, report that gap. "
            "Context and request budget limits still apply.")
        sections: list[str] = [
            "[Paged memory context]",
            f"mode={packet.memory_mode.value}",
            "Summaries and cards below are navigation aids. Source IDs are authoritative.",
            "User instruction sources retain their order; later user corrections amend earlier instructions.",
            (
                "For omitted detail, use " + ", ".join(memory_readers) + "; operations are bounded and scoped to this task."
                if memory_readers else "No memory navigation tool is available in this scope; do not invent one."
            ),
            (
                "Reuse memory evidence already present. Never repeat an equivalent "
                "memory_search or reread the same source range."
            ),
        ]
        if permits("history_search"):
            sections.append("Use note/source pointers when available; otherwise history_search finds literal "
                "original text or browses by role/tool/run. memory_search offers ranked keyword recall. "
                "Follow search cursors before concluding that evidence is absent; read only missing source ranges. "
                "History timestamps and user corrections determine whether an old statement still applies.")
        groups = (
            ("Task state", packet.task_state),
            ("Current step", packet.current_step),
            ("Required dependencies", packet.mandatory_dependencies),
            ("Working handoff note (advisory)", packet.handoff_notes),
            ("Recent source events", visible_recent),
            ("Working set", packet.working_set),
            ("Supplemental memories", packet.supplemental_memories),
        )
        note_info = packet.debug_metadata.get("handoff_note")
        utilization = packet.debug_metadata.get("context_utilization")
        if utilization:
            sections.extend([
                "\n## Context capacity status",
                json.dumps(utilization, ensure_ascii=False, sort_keys=True),
                "write_note: save a concise memory_note_update with verified sources and next_steps. "
                "rotate: finish the current bounded stage and hand off. protect: new business tools are blocked; "
                "save a fresh note after the latest results. The runtime rotates only the history covered by "
                "the verified note; later results stay verbatim for the next request. Bounded memory reads and poll/wait/cancel of existing agents remain "
                "available. Do not repeat blocked work or treat rotation as task completion. "
                "Forecasts are estimates; the final request budget remains enforced."
                if permits("memory_note_update") and permits("context_rotate") else no_handoff,
            ])
        if note_info:
            borrowing = packet.debug_metadata.get("context_borrowing")
            if borrowing and borrowing.get("status") in {"active", "handoff"}:
                preparing = borrowing.get("trigger") == "context_preparation"
                sections.extend(["\n## Context preparation" if preparing else "\n## Emergency handoff capacity",
                    json.dumps(borrowing, ensure_ascii=False, sort_keys=True),
                    ("Inherited or continued history has reached the compaction threshold. Prepare context before continuing the task. "
                     if preparing else "The business request could not be admitted after optional paging. ")
                    + "This request is only for handoff. "
                    "Use the working context limit first; any extra capacity shown above is temporary. "
                    "Save a short, complete memory_note_update with verified source IDs and next_steps. "
                    "Preserve the current user requirements, decisions, completed work and outstanding steps. "
                    "Source IDs inherited from another task are not automatically valid here: use current-task receipts "
                    "or bounded history reads to verify them; put unverified facts in open_questions. "
                    "After a successful note update the runtime attempts verified rotation automatically. "
                    "Do not start business work or finish the task instead of handing off. The runtime must verify rotation "
                    "and a request within the unchanged working context limit before business tools can resume."])
            sections.extend([
                "\n## Handoff note status",
                json.dumps(note_info, ensure_ascii=False, sort_keys=True),
                "Use memory_note_update with expected_revision from this status after a meaningful "
                "stage, before a planned pause, or when budget pressure calls for a handoff. "
                "Keep findings with source Event IDs and their conditions; record why an approach was ruled out "
                "and when to reconsider it. Put unverified claims in open_questions; make next_steps actionable. "
                "Respect the item, text and total token limits above; prefer 3-5 short entries with paths, "
                "not full code/interface inventories. This status's source_event_id only locates the saved "
                "advisory note; do not cite it as evidence in another note. Reuse the original source IDs "
                "inside that note after checking their relevance. Outcome/artifact/handle IDs are not source Event IDs. "
                "Do not rewrite an unchanged note every turn. Notes never override user instructions, "
                "task contracts or source evidence. Stale/invalid notes are omitted; reassess before replacing them."
                if permits("memory_note_update") else no_handoff,
            ])
        rotation_info = packet.debug_metadata.get("context_rotation")
        if rotation_info:
            if rotation_info.get("status") == "active" and rotation_info.get("note_scope") == "sealed_history":
                sections.append("Native recovery has already restored the verified historical summary. "
                    "The note's stale status describes its older authorship, not a failure of this recovery. "
                    "Use the current context utilization/protection status for admission, not historical errors. "
                    "Follow the latest request and the verbatim tail; continue the task when protection is false. "
                    "A later fresh note may summarize new progress; do not repeat recovery of this same boundary.")
            sections.extend([
                "\n## Context rotation status",
                json.dumps({key: value for key, value in rotation_info.items() if key not in {"anchor", "counter_baseline"}},
                           ensure_ascii=False, sort_keys=True),
                "At a completed stage or context pressure, save a fresh memory_note_update with next_steps, "
                "then use context_rotate(expected_epoch from this status, expected_note_revision from the "
                "successful note receipt). If deferred, discover_tools can select context_rotate. "
                "Rotation takes effect on the next request in this same task; original sources remain "
                "searchable. Required instructions and the verified advisory note remain present. "
                "Native pressure recovery may already have rotated before this request: active status with "
                "note_revision matching the saved note and a source_event_id confirms that commit. "
                "A repeat request returns that verified receipt without another commit. New results after "
                "the note boundary stay inline; read and act on them before the next handoff. "
                "Do not save another note or rotate again merely to repeat that completed handoff. "
                "Do not rotate every turn or repeat a committed rotation."
                if permits("memory_note_update") and permits("context_rotate") else no_handoff,
            ])
        inline_proofs = packet.debug_metadata.get("render_inline_source_proofs", {})
        required_facets = packet.debug_metadata.get("required_facets", ())
        for title, items in groups:
            if not items:
                continue
            sections.append(f"\n## {title}")
            for item in items:
                if inline_proofs and not protected_item(item) and covered_at_render(item, inline_proofs,
                        visible_source_content or {},
                        required_facets=required_facets):
                    continue
                if item.source_event_id in (projected_tool_sources or ()) and not protected_item(item):
                    # The bounded result receipt already carries this source's
                    # preview and exact read coordinates. Do not reinsert an
                    # optional raw body or call it byte-identical inline text.
                    continue
                raw_text_hash = content_hash_text(item.text)
                if (
                    raw_text_hash == item.content_hash
                    and raw_text_hash in rendered_hashes
                ):
                    continue
                source = item.source_event_id or item.artifact_id or "structured-state"
                # Receipt identities follow the renderer's actual selection,
                # including explicit source references, after all elision checks.
                if rendered_sources is not None:
                    rendered_sources.add(item.dedup_key())
                note_calls = (visible_handoff_content or {}).get(raw_text_hash, ())
                if item.item_type == "HANDOFF_NOTE" and note_calls:
                    sections.append(
                        f"- HANDOFF_NOTE {item.item_id} source={source}: exact note fields are "
                        f"in completed memory_note_update calls {json.dumps(list(note_calls))}; "
                        "this working note remains advisory."
                    )
                    continue
                tool_ids = (visible_tool_content or {}).get(raw_text_hash, ())
                if (
                    tool_ids and not item.required and item.source_event_id
                    and raw_text_hash == item.content_hash
                    and item.item_type in {"TOOL_RESULT", "TOOL_ERROR"}
                ):
                    # Only the complete, byte-identical raw result in this
                    # request can replace an optional duplicate body. Keep its
                    # provenance; previews and mandatory rules never qualify.
                    sections.append(
                        f"- {item.item_type} {item.item_id} source={source}: "
                        f"exact source is in current tool_result {json.dumps(list(tool_ids))}; "
                        f"content_hash={item.content_hash}"
                    )
                    rendered_hashes.add(raw_text_hash)
                    continue
                sections.append(
                    f"- {item.item_type} {item.item_id} source={source}: {item.text}"
                )
                rendered_hashes.add(raw_text_hash)
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

    def fit_request_budget(self, packet: ContextPacket, messages, request_budget) -> ContextPacket:
        """Calibrate optional paging against the actual serialized request.

        Tokenizer/framing differences must not strand handoff behind the final
        admission guard. Only existing optional shares change; required packet
        sources, controls, the latest request and whole exchanges stay pinned.
        No retrieval or source mutation occurs during these local measurements.
        """
        if (request_budget is None or packet.memory_mode is MemoryMode.INLINE
                or not packet.debug_metadata.get("context_allocation")):
            return packet
        measured = request_budget.measure(self.model_messages(packet, messages))
        if measured <= request_budget.input_limit:
            return packet
        allocation = packet.debug_metadata["context_allocation"]
        sections = {name: getattr(packet, name) for name in
                    ("working_set", "supplemental_memories", "recent_messages")}
        fixed = [*packet.system_context, *packet.task_state, *packet.current_step,
                 *packet.mandatory_dependencies, *packet.handoff_notes]

        def candidate(limit):
            selected, omitted, info = allocate_sections(self.config, self.token_budget,
                sections=sections, fixed_items=fixed, messages=messages,
                rotation_info=packet.debug_metadata.get("context_rotation", {}),
                available_budget=packet.token_budget,
                pressure=packet.debug_metadata.get("context_pressure"), optional_token_limit=limit)
            info["recent_conversation_budget"] = min(info["recent_conversation_budget"],
                                                     allocation["recent_conversation_budget"])
            changed = replace(packet, **selected, omitted_items=[*packet.omitted_items,
                *[{**item, "reason": "request_admission_budget"} for item in omitted]],
                debug_metadata={**packet.debug_metadata, "context_allocation": info})
            selected_ids = {item.item_id for items in selected.values() for item in items}
            original_ids = {item.item_id for items in sections.values() for item in items}
            changed.source_references = [ref for ref in packet.source_references
                if ref["item_id"] not in original_ids or ref["item_id"] in selected_ids]
            return changed, request_budget.measure(self.model_messages(changed, messages))

        best, minimum = candidate(0)
        if minimum > request_budget.input_limit:
            # Paging cannot make essential input fit. Preserve the packet and
            # let the hard guard report the real overage without false recovery.
            return packet
        growth = packet.debug_metadata.get("context_pressure", {}).get("growth_reserve_tokens", 0)
        target = max(minimum, request_budget.input_limit - growth)
        low, high, accepted, measurements = 0, allocation["pool_tokens"], minimum, 2
        # Keep the largest measured optional share that fits, including when
        # whole-card packing or exact-inline dedup makes costs non-linear.
        for _ in range(10):
            if high - low <= 1:
                break
            middle = (low + high) // 2
            fitted, tokens = candidate(middle)
            measurements += 1
            if tokens <= target:
                best, accepted, low = fitted, tokens, middle
            else:
                high = middle
        best.debug_metadata["request_budget_fit"] = {
            "policy": "actual-request-optional-paging-v1", "before_tokens": measured,
            "after_tokens": accepted, "input_limit": request_budget.input_limit,
            "target_tokens": target, "measurements": measurements,
            "canonical_history_changed": False,
        }
        return best

    def recent_history(self, packet: ContextPacket, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        from minicode.runtime_role_context import current_primary_instructions
        rotation_info = packet.debug_metadata.get("context_rotation", {})
        non_system = project_history([m for m in messages if m.get("role") != "system"], rotation_info)
        non_system = current_primary_instructions(non_system)
        return self.token_budget.recent_messages(non_system,
            token_budget=packet.debug_metadata.get("context_allocation", {}).get(
                "recent_conversation_budget", self.config.recent_event_token_budget), retain_latest_user=True,
            retain_all_users=rotation_info.get("status") == "active")

    def model_messages(
        self,
        packet: ContextPacket,
        messages: Sequence[Mapping[str, Any]],
        *, required_prefix: bool = False, prefix_source_keys: frozenset = frozenset(),
        source_sections: dict[str, frozenset[tuple[str, str, str]]] | None = None,
    ) -> list[dict[str, Any]]:
        from minicode.runtime_messages import is_user_input, runtime_context_message
        from minicode.runtime_role_context import current_primary_instructions

        if packet.memory_mode == MemoryMode.INLINE:
            original = [dict(message) for message in current_primary_instructions(messages)]
            existing_texts = [str(message.get("content", "")) for message in messages]
            missing_rules = []
            for item in packet.mandatory_dependencies:
                is_rule = item.item_type == "CONSTRAINT" or {
                    "user_instruction",
                    "task_contract",
                }.intersection(item.facets)
                if is_rule and not any(item.text in text for text in existing_texts):
                    missing_rules.append(item.text)
                    existing_texts.append(item.text)
            if missing_rules:
                original.insert(
                    0,
                    {
                        "role": "system",
                        "content": "[Task contract]\n" + "\n\n".join(missing_rules),
                    },
                )
            return original
        system = [
            dict(message) for message in messages if message.get("role") == "system"
        ]
        tail, projected_sources = project_results(self.recent_history(packet, messages),
            packet.debug_metadata.get("tool_result_projection", {}))
        # Keep missing mandatory instructions at their existing system priority.
        # Only typed rules qualify; tool results and runtime state do not.
        existing_texts = [str(message.get("content", "")) for message in [*system, *tail]]
        missing_rules = []
        for item in packet.mandatory_dependencies:
            is_rule = item.item_type == "CONSTRAINT" or {
                "user_instruction", "task_contract",
            }.intersection(item.facets)
            if is_rule and not any(item.text in text for text in existing_texts):
                missing_rules.append(item.text)
                existing_texts.append(item.text)
        if missing_rules:
            system.append({"role": "system", "content": "[Task contract]\n" + "\n\n".join(missing_rules)})
        tail_hashes = set()
        source_hashes = {}
        visible_tool_content: dict[str, list[str]] = {}
        for message in tail:
            role, content = message.get("role"), message.get("content", "")
            if role in {"user", "assistant", "assistant_progress"}:
                digest = content_hash_text(str(content))
                tail_hashes.add(digest)
                if is_user_input(message):
                    source_hashes.setdefault(EventType.USER_INPUT.value, set()).add(digest)
            elif role == "tool_result" and isinstance(content, str):
                digest = content_hash_text(content)
                if message.get("toolUseId"):
                    visible_tool_content.setdefault(digest, []).append(str(message["toolUseId"]))
                if not message.get("isError"):
                    source_hashes.setdefault(EventType.TOOL_RESULT.value, set()).add(digest)
        render_options = dict(
            excluded_recent_content_hashes=tuple(tail_hashes | {
                content_hash_text(text) for text in missing_rules}),
            visible_tool_content=visible_tool_content,
            visible_handoff_content=visible_note_content(tail) if packet.handoff_notes else None,
            projected_tool_sources=projected_sources,
            visible_source_content=source_hashes,
        )
        def render(section_packet, options):
            identities = set() if source_sections is not None else None
            text = self.render_memory_context(section_packet, **options, rendered_sources=identities)
            if source_sections is not None:
                source_sections[text] = frozenset(identities)
            return text
        prefix = []
        if required_prefix and (packet.mandatory_dependencies or prefix_source_keys):
            # Only the live final-review request view selects this layout.
            # Required sources retain their text, order and priority; changing
            # task state and capacity estimates remain after the conversation.
            anchored = [item for item in packet.recent_messages if (item.item_id, item.content_hash) in prefix_source_keys]
            required = replace(packet, task_state=[], current_step=[], recent_messages=anchored, working_set=[],
                supplemental_memories=[], handoff_notes=[], artifact_manifest=[], debug_metadata={})
            prefix = [runtime_context_message(render(required,
                {**render_options, "visible_tool_content": {}}),
                                              kind="required_memory")]
            packet = replace(packet, mandatory_dependencies=[],
                recent_messages=[item for item in packet.recent_messages if item not in anchored])
        memory_message = runtime_context_message(
            # Do not inject a persisted event a second time when its exact raw
            # message is already present in the provider-valid recent tail.
            render(packet, render_options),
            kind="paged_memory",
        )
        return [*system, *prefix, *tail, memory_message]
