"""Scoped model tools for on-demand reads from paged memory."""

from __future__ import annotations

import base64
import json
from typing import Any
from types import FunctionType
from weakref import WeakSet, WeakKeyDictionary

from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.tooling import (
    ToolCapability,
    ToolDefinition,
    ToolMetadata,
    ToolResult,
)

from .models import MemoryMode, MemoryRecord
from .handoff import (HandoffNotes, HandoffNoteInputError, NOTE_FIELDS, NOTE_REASONS,
                      NOTE_MAX_ITEMS, NOTE_MAX_TEXT_CHARS, validate_note_update)
from .rotation import ContextRotation, ROTATION_REASONS, validate_rotation
from .history_journal import HistoryIntegrityError


_STATE_TOOL_RUNNERS: WeakSet = WeakSet()
_PAGED_TOOL_RUNNERS: WeakKeyDictionary = WeakKeyDictionary()


def is_native_paged_tool(tool: ToolDefinition) -> bool:
    """Match an installed scoped memory handler, never its advertised name."""
    return (isinstance(tool.run, FunctionType)
        and _PAGED_TOOL_RUNNERS.get(tool.run) == (tool.name, tool.effect))


def is_native_paged_state_tool(tool: ToolDefinition) -> bool:
    """Recognize installed owner-bound handlers, never a tool name/metadata claim."""
    return (tool.name in {"memory_note_update", "context_rotate"}
            and isinstance(tool.run, FunctionType) and tool.run in _STATE_TOOL_RUNNERS)


def _integer(value: Any, *, name: str, default: int, minimum: int = 0) -> int:
    from minicode.tools._validation import InputContractError
    parsed = default if value is None else value
    if type(parsed) is not int:
        raise InputContractError(f"{name} must be an integer; do not coerce the value", path=[name],
                                 expected="integer", actual=type(parsed).__name__)
    if parsed < minimum:
        raise InputContractError(f"{name} must be >= {minimum}", path=[name],
                                 expected=f"minimum={minimum}", actual=str(parsed))
    return parsed


def _read_metadata(name: str, description: str) -> ToolMetadata:
    return ToolMetadata(
        name=name,
        description=description,
        capabilities={ToolCapability.READ_ONLY, ToolCapability.CONCURRENCY_SAFE},
        effect=ToolEffect.READ_ONLY,
        replay_policy=ReplayPolicy.SAFE,
        tags=["memory", "paged", "read"],
    )


def _memory_card(record: MemoryRecord, *, preview_limit: int = 800) -> dict[str, Any]:
    preview = record.preview_text[: max(0, preview_limit)]
    return {
        "memory_id": record.id,
        "type": record.memory_type.value,
        "status": record.status.value,
        "title": record.title,
        "preview": preview,
        "preview_truncated": len(preview) < len(record.preview_text),
        "source_event_id": record.source_event_id,
        "artifact_id": record.artifact_id,
        "source_range": record.source_range.to_dict(),
        "content_hash": record.content_hash,
        "facets": record.facets,
        "run_id": record.run_id,
        "step_id": record.step_id,
        "created_at": record.created_at,
    }


def create_paged_memory_tools(runtime: Any) -> list[ToolDefinition]:
    max_page_tokens = max(1, int(runtime.config.raw_page_token_limit))
    max_text_units = max(256, max_page_tokens * 4)
    max_structured_units = max(1, min(256, max_page_tokens))

    def encoded_content(data: bytes) -> tuple[str, str]:
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            return base64.b64encode(data).decode("ascii"), "base64"

    def page_tokens(data: bytes) -> int:
        content, _encoding = encoded_content(data)
        return runtime.token_budget.estimate_text(content)

    def bounded_read(
        reader: Any,
        *,
        start: int,
        requested_end: int,
        unit: str,
        source_end: int | None,
    ) -> tuple[bytes, int, bool]:
        if source_end is not None and start > source_end:
            from minicode.tools._validation import InputContractError
            raise InputContractError(f"start {start} exceeds this source view's exclusive end {source_end}; "
                "use its own next_start, or start=0 for a new filtered view", path=["start"],
                expected="within the current source view; use its next_start or 0 after changing filters", actual=str(start))
        if source_end is not None and start >= source_end:
            return b"", start, False
        target_end = (
            min(requested_end, source_end) if source_end is not None else requested_end
        )
        probe_units = (
            max_text_units if unit in {"BYTE", "CHAR"} else max_structured_units
        )
        candidate_end = min(target_end, start + probe_units)
        if candidate_end <= start:
            return b"", start, candidate_end < target_end
        data = reader(candidate_end)
        if page_tokens(data) <= max_page_tokens:
            if not data and source_end is None:
                return b"", start, False
            return data, candidate_end, candidate_end < target_end

        span = candidate_end - start
        decoded = data.decode("utf-8") if unit != "BYTE" else ""
        logical_units = (
            decoded.split("\n")
            if unit == "LINE"
            else decoded.split("\f")
            if unit == "PAGE"
            else []
        )

        def prefix_for_span(count: int) -> bytes:
            if unit == "BYTE":
                return data[:count]
            if unit == "CHAR":
                return decoded[:count].encode("utf-8")
            separator = "\n" if unit == "LINE" else "\f"
            return separator.join(logical_units[:count]).encode("utf-8")

        low = 1
        high = span - 1
        best_data: bytes | None = None
        best_span = 0
        while low <= high:
            midpoint = (low + high) // 2
            candidate = prefix_for_span(midpoint)
            if page_tokens(candidate) <= max_page_tokens:
                best_data = candidate
                best_span = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best_data is None:
            raise ValueError(
                f"one {unit} unit exceeds the {max_page_tokens}-token page budget; "
                "retry the same source with BYTE or CHAR units"
            )
        return best_data, start + best_span, True

    def validate_search(value: dict[str, Any]) -> dict[str, Any]:
        query = str(value.get("query", "")).strip()
        if not query:
            raise ValueError("query is required")
        limit = _integer(value.get("limit"), name="limit", default=10, minimum=1)
        return {"query": query, "limit": min(limit, runtime.config.candidate_limit)}

    def run_search(value: dict[str, Any], _context: Any) -> ToolResult:
        records = runtime.search(value["query"], limit=value["limit"])
        cards: list[dict[str, Any]] = []
        used = 0
        for record in records:
            card = _memory_card(record)
            cost = runtime.token_budget.estimate_text(
                json.dumps(card, ensure_ascii=False, sort_keys=True)
            )
            if cards and used + cost > max_page_tokens:
                break
            cards.append(card)
            used += cost
        return ToolResult(
            ok=True,
            output=json.dumps(
                {
                    "task_id": runtime.task_id,
                    "mode": runtime.current_mode.value,
                    "count": len(records),
                    "returned_count": len(cards),
                    "results_clamped_to_page_budget": len(cards) < len(records),
                    "memories": cards,
                    "guidance": (
                        "Reuse these cards. Do not repeat an equivalent memory_search; "
                        "use memory_get only for a source range that is not already present. "
                        "These cards describe historical observations. run_id/step_id locate their records; "
                        "created_at is the card creation time, not proof of current file or requirement state."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    def validate_memory_get(value: dict[str, Any]) -> dict[str, Any]:
        memory_id = str(value.get("memory_id", "")).strip()
        source_event_id = str(value.get("source_event_id", "")).strip()
        if bool(memory_id) == bool(source_event_id):
            raise ValueError("provide exactly one of memory_id or source_event_id")
        unit = str(value.get("unit", "BYTE")).upper()
        if unit not in {"BYTE", "CHAR", "LINE", "PAGE"}:
            raise ValueError("unit must be BYTE, CHAR, LINE, or PAGE")
        start = _integer(value.get("start"), name="start", default=0)
        default_span = (
            max_text_units if unit in {"BYTE", "CHAR"} else max_structured_units
        )
        requested_end = _integer(
            value.get("end"),
            name="end",
            default=start + default_span,
            minimum=start,
        )
        if requested_end == start:
            raise ValueError("end must be greater than start")
        return {
            "memory_id": memory_id,
            "source_event_id": source_event_id,
            "start": start,
            "end": requested_end,
            "unit": unit,
            "requested_end": requested_end,
        }

    def run_memory_get(value: dict[str, Any], _context: Any) -> ToolResult:
        record = None
        event = None
        if value["memory_id"]:
            records = runtime.authority.get_memories(
                tenant_id=runtime.tenant_id,
                task_id=runtime.task_id,
                memory_ids=[value["memory_id"]],
            )
            if not records:
                return ToolResult(
                    ok=False, output="memory not found in the current task scope"
                )
            record = records[0]
            source_end = runtime.memory_source_end(
                value["memory_id"], unit=value["unit"]
            )

            def read(end: int) -> bytes:
                return runtime.read_memory_range(
                    memory_id=value["memory_id"],
                    start=value["start"],
                    end=end,
                    unit=value["unit"],
                )

        else:
            try:
                event = runtime.read_event(value["source_event_id"])
            except KeyError:
                return ToolResult(
                    ok=False, output="event not found in the current task scope"
                )
            source_end = runtime.event_source_end(
                value["source_event_id"], unit=value["unit"]
            )

            def read(end: int) -> bytes:
                return runtime.read_event_range(
                    value["source_event_id"],
                    start=value["start"],
                    end=end,
                    unit=value["unit"],
                )

        data, page_end, request_clamped = bounded_read(
            read,
            start=value["start"],
            requested_end=value["requested_end"],
            unit=value["unit"],
            source_end=source_end,
        )
        content, encoding = encoded_content(data)
        has_more = (
            page_end < source_end
            if source_end is not None
            else True
            if request_clamped
            else None
        )
        return ToolResult(
            ok=True,
            output=json.dumps(
                {
                    "memory": _memory_card(record) if record is not None else None,
                    "source_event": (
                        {
                            "event_id": event.id,
                            "type": event.event_type.value,
                            "content_hash": event.content_hash,
                            "token_count": event.token_count,
                        }
                        if event is not None
                        else None
                    ),
                    "page": {
                        "start": value["start"],
                        "end": page_end,
                        "unit": value["unit"],
                        "coordinate_system": (
                            "MEMORY_RELATIVE"
                            if record is not None
                            else "EVENT_RELATIVE"
                        ),
                        "source_end": source_end,
                        "requested_end": value["requested_end"],
                        "max_page_tokens": max_page_tokens,
                        "request_clamped": request_clamped,
                        "content_truncated_to_page_budget": False,
                        "has_more": has_more,
                        "next_start": page_end if has_more is not False else None,
                    },
                    "encoding": encoding,
                    "content": content,
                    "guidance": (
                        "Do not reread this range. Continue from next_start only when "
                        "has_more is true and the omitted source is required."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    def validate_artifact_read(value: dict[str, Any]) -> dict[str, Any]:
        from minicode.tools._validation import InputContractError, validate_contract
        validate_contract(value, artifact_read_input)
        artifact_id = str(value.get("artifact_id", "")).strip()
        if not artifact_id:
            raise ValueError("artifact_id is required")
        filtered = bool(value.get("query") or value.get("log_level"))
        unit = str(value.get("unit", "CHAR" if filtered else "BYTE")).upper()
        if unit not in {"BYTE", "CHAR", "LINE", "PAGE"}:
            raise ValueError("unit must be BYTE, CHAR, LINE, or PAGE")
        start = _integer(value.get("start"), name="start", default=0)
        default_span = (
            max_text_units if unit in {"BYTE", "CHAR"} else max_structured_units
        )
        end = _integer(
            value.get("end"), name="end", default=start + default_span
        )
        if end <= start:
            raise InputContractError("end must be greater than start", path=["end"],
                                     expected="greater than start", actual=str(end))
        return {
            "artifact_id": artifact_id,
            "start": start,
            "end": end,
            "unit": unit,
            "query": str(value.get("query", "")),
            "log_level": str(value.get("log_level", "")),
        }

    def run_artifact_read(value: dict[str, Any], _context: Any) -> ToolResult:
        artifact = runtime.authority.get_artifact(
            tenant_id=runtime.tenant_id,
            task_id=runtime.task_id,
            artifact_id=value["artifact_id"],
        )
        if artifact is None:
            return ToolResult(
                ok=False, output="artifact not found in the current task scope"
            )
        source_end = runtime.artifact_source_end(
            value["artifact_id"],
            unit=value["unit"],
            query=value["query"],
            log_level=value["log_level"],
        )
        data, page_end, request_clamped = bounded_read(
            lambda end: runtime.read_artifact_range(
                artifact_id=value["artifact_id"],
                start=value["start"],
                end=end,
                unit=value["unit"],
                query=value["query"],
                log_level=value["log_level"],
            ),
            start=value["start"],
            requested_end=value["end"],
            unit=value["unit"],
            source_end=source_end,
        )
        content, encoding = encoded_content(data)
        has_more = (
            page_end < source_end
            if source_end is not None
            else True
            if request_clamped
            else None
        )
        return ToolResult(
            ok=True,
            output=json.dumps(
                {
                    "artifact_id": value["artifact_id"],
                    "range": {
                        "start": value["start"],
                        "end": page_end,
                        "unit": value["unit"],
                        "coordinate_system": "FILTERED_ARTIFACT_RELATIVE" if value["query"] or value["log_level"] else "ARTIFACT_RELATIVE",
                        "source_end": source_end,
                        "requested_end": value["end"],
                        "max_page_tokens": max_page_tokens,
                        "request_clamped": request_clamped,
                        "has_more": has_more,
                        "next_start": page_end if has_more is not False else None,
                    },
                    "encoding": encoding,
                    "truncated_to_page_budget": False,
                    "content": content,
                    "filters": {"query": value["query"], "log_level": value["log_level"]},
                    "guidance": (
                        "Offsets address this stored tool result, including any headers, not the original file. "
                        "Filters create a new coordinate system: start at 0 when changing them. "
                        "Continue from next_start only while has_more is true; reaching this result's end "
                        "does not prove the original file was read completely."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    def validate_restore(value: dict[str, Any]) -> dict[str, Any]:
        return {"checkpoint_id": str(value.get("checkpoint_id", "")).strip()}

    def run_restore(value: dict[str, Any], _context: Any) -> ToolResult:
        checkpoint = runtime.restore_checkpoint(
            value["checkpoint_id"], preserve_active_step=True
        )
        if checkpoint is None:
            return ToolResult(
                ok=False, output="checkpoint not found in current task scope"
            )
        return ToolResult(
            ok=True,
            output=json.dumps(
                {
                    "checkpoint_id": checkpoint.id,
                    "state_version": checkpoint.state_version,
                    "current_step_id": checkpoint.current_step_id,
                    "memory_mode": checkpoint.memory_mode.value,
                    "content_hash": checkpoint.content_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    range_properties = {
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 0},
        "unit": {"type": "string", "enum": ["BYTE", "CHAR", "LINE", "PAGE"]},
    }
    artifact_read_input = {
        "type": "object", "additionalProperties": False, "required": ["artifact_id"],
        "properties": {
            "artifact_id": {"type": "string", "minLength": 1, "description":
                "Exact saved-tool-result ID (artifact_...). Runtime report/dependency IDs (artifact-...) use runtime_artifact_read."},
            **range_properties,
            "unit": {**range_properties["unit"], "description":
                "Omit for CHAR when query/log_level filters are present, otherwise BYTE. Explicit BYTE cannot be combined with filters."},
            "query": {"type": "string"}, "log_level": {"type": "string"},
        },
        "allOf": [{"if": {"anyOf": [
            {"required": [name], "properties": {name: {"type": "string", "minLength": 1}}}
            for name in ("query", "log_level")]},
            "then": {"properties": {"unit": {"enum": ["CHAR", "LINE", "PAGE"], "description":
                "Filtered artifact views require CHAR, LINE or PAGE; BYTE addresses the unfiltered bytes."}}}}],
    }
    definitions = [
        ToolDefinition(
            name="memory_search",
            description=(
                "Search source-linked memory cards in the current tenant/task. "
                "Use once per information gap and reuse returned cards; do not rephrase "
                "and repeat equivalent searches. Explicit dependencies remain authoritative."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["query"],
            },
            validator=validate_search,
            run=run_search,
            metadata=_read_metadata("memory_search", "Scoped memory-card search"),
        ),
        ToolDefinition(
            name="memory_get",
            description=(
                "Read one bounded source range for either a current-task memory card or "
                "an explicit source Event. Memory coordinates are relative to that card; "
                "Artifact coordinates belong to artifact_read. Provide exactly one ID. "
                "Oversized requests are safely paged and return next_start; never reread "
                "a returned range."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "source_event_id": {"type": "string"},
                    **range_properties,
                },
                "oneOf": [
                    {"required": ["memory_id"]},
                    {"required": ["source_event_id"]},
                ],
            },
            validator=validate_memory_get,
            run=run_memory_get,
            metadata=_read_metadata("memory_get", "Scoped exact memory read"),
        ),
        ToolDefinition(
            name="artifact_read",
            description=("Read a bounded range of a stored tool result in the current task, including earlier runs. "
                          "Stored tool-result IDs use artifact_...; Runtime dependency/message/report IDs "
                          "use runtime_artifact_read. This reads the saved result, not the original file. "
                         "Use its next_start to page; offsets include tool headers. Query/log_level filter first "
                         "and then page the filtered view from 0 in CHAR/LINE/PAGE units (omitted unit defaults to CHAR for filters, BYTE otherwise). "
                         "Never reuse original-file offsets for a filtered Artifact."),
            input_schema=artifact_read_input,
            validator=validate_artifact_read,
            run=run_artifact_read,
            metadata=_read_metadata("artifact_read", "Scoped Artifact range read"),
        ),
        ToolDefinition(
            name="checkpoint_restore",
            description="Restore the current run view from a verified current-task checkpoint.",
            input_schema={
                "type": "object",
                "properties": {"checkpoint_id": {"type": "string"}},
            },
            validator=validate_restore,
            run=run_restore,
            metadata=ToolMetadata(
                name="checkpoint_restore",
                description="Verified checkpoint restore",
                effect=ToolEffect.PRIVILEGED,
                replay_policy=ReplayPolicy.NEVER_AUTO_RETRY,
                tags=["memory", "checkpoint", "state"],
            ),
        ),
    ]
    from .history_search import create_history_search_tool
    definitions.append(create_history_search_tool(runtime, _read_metadata("history_search", "Search original task history")))
    notes = getattr(runtime, "handoff_notes", None)
    # Read-only Workers may maintain their OWN advisory context. These native
    # handlers only write owner-scoped memory, never candidate/project files.
    allowed = getattr(runtime, "context_tool_effects", None)
    readonly_context = allowed == frozenset({ToolEffect.READ_ONLY})
    if notes is not None and notes.enabled and runtime.current_mode != MemoryMode.INLINE:
        definitions.append(create_handoff_note_tool(notes, readonly_context=readonly_context))
    rotation = getattr(runtime, "context_rotation", None)
    if rotation is not None and rotation.enabled and runtime.current_mode != MemoryMode.INLINE:
        definitions.append(create_context_rotation_tool(rotation, readonly_context=readonly_context))
    scoped = [tool for tool in definitions if allowed is None or tool.effect in allowed]
    for tool in scoped:
        _PAGED_TOOL_RUNNERS[tool.run] = (tool.name, tool.effect)
    return scoped


def create_handoff_note_tool(notes: Any, *, readonly_context: bool = False) -> ToolDefinition:
    def update(value: dict[str, Any], _context: Any) -> ToolResult:
        # Validation and CAS failures are actionable tool feedback, not an
        # invitation to silently retry an update based on an obsolete context.
        try:
            result = notes.update(value)
        except HandoffNoteInputError as exc:
            return ToolResult(ok=False, output=str(exc), failureKind="invalid_input", inputValidationFailed=True)
        except HistoryIntegrityError as exc:
            return ToolResult(ok=False, output=str(exc), failureKind="context_blocked", inputRejected=True)
        except (ValueError, KeyError) as exc:
            return ToolResult(ok=False, output=str(exc))
        return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False, sort_keys=True))

    if type(notes) is HandoffNotes:
        _STATE_TOOL_RUNNERS.add(update)
    text_item = {"type": "string", "minLength": 1, "maxLength": NOTE_MAX_TEXT_CHARS}
    finding = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "text": {**text_item, "description": "One concise finding with its supporting reason and conditions. For a ruled-out approach, say why and when to reconsider it."},
            "source_event_ids": {"type": "array", "minItems": 1, "maxItems": 4,
                                 "description": "Original current-task source_event_id values, not memory IDs, tool-call IDs or navigation receipts.",
                                 "items": {"type": "string", "minLength": 1, "maxLength": 200}},
        },
        "required": ["text", "source_event_ids"],
    }
    description = (
        "Replace your bounded working handoff note in the current task. Use the expected_revision "
        "shown in Handoff note status. Findings and ruled-out approaches need source Event IDs; "
        "notes are advisory and cannot change user requirements. Save at meaningful stage/pause "
        "boundaries or budget pressure, not every turn. Supply all four arrays; use [] when empty. "
        f"Each array allows at most {NOTE_MAX_ITEMS} items; each text at most {NOTE_MAX_TEXT_CHARS} characters. "
        "Prefer 3-5 short findings/actions with file paths instead of copying full interfaces or code. "
        "Keep unverified claims in open_questions; source integrity does not prove a claim. "
        "For check results, retain the command, observed outcome, checked revision when available and "
        "any later changes in text, citing the original check event. Do not invent fields or revision IDs. "
        "Make next_steps executable with a file, symbol or source pointer when useful. "
        "Each finding has only text and source_event_ids; never add text_2 or other fields. "
        "The entire note must fit the token_budget in Handoff note status, even when each entry "
        "meets its character limit. After a rejection, correct the reported fields before resubmitting; "
        "after a conflict, refresh context first."
    )
    return ToolDefinition(
        name="memory_note_update", description=description,
        input_schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "expected_revision": {"type": "integer", "minimum": 0},
                "reason": {"type": "string", "enum": list(NOTE_REASONS)},
                **{key: {"type": "array", "maxItems": NOTE_MAX_ITEMS,
                         "items": finding if key in {"confirmed_findings", "ruled_out"} else text_item}
                   for key in NOTE_FIELDS},
            },
            "required": ["expected_revision", *NOTE_FIELDS],
        },
        validator=validate_note_update, run=update,
        metadata=ToolMetadata(
            name="memory_note_update", description=description,
            capabilities={ToolCapability.CONCURRENCY_SAFE},
            effect=ToolEffect.READ_ONLY if readonly_context and type(notes) is HandoffNotes else ToolEffect.WORKSPACE_WRITE,
            replay_policy=ReplayPolicy.NEVER_AUTO_RETRY,
            tags=["memory", "paged", "handoff", "state"],
        ),
    )


def create_context_rotation_tool(rotation: Any, *, readonly_context: bool = False) -> ToolDefinition:
    def rotate(value: dict[str, Any], _context: Any) -> ToolResult:
        try:
            result = rotation.rotate(value)
        except HistoryIntegrityError as exc:
            return ToolResult(ok=False, output=str(exc), failureKind="context_blocked", inputRejected=True)
        except (ValueError, KeyError) as exc:
            return ToolResult(ok=False, output=str(exc))
        return ToolResult(ok=True, output=json.dumps(result, ensure_ascii=False, sort_keys=True))

    if type(rotation) is ContextRotation:
        _STATE_TOOL_RUNNERS.add(rotate)
    description = (
        "Start the next working context within this task after saving a fresh verified handoff note "
        "with next_steps. Use the context epoch from status and note revision from the saved receipt. "
        "The next request omits completed optional history; sources remain in SQLite for memory_get/search. "
        "Current tool calls, user instructions and required evidence are preserved. "
        "Check active rotation status first: native pressure recovery may already have applied the saved "
        "note_revision, in which case no repeat call or replacement note is needed. "
        "Repeated requests confirm the durable receipt without another commit. Revision races are "
        "reconciled against verified state with at most 3 commit attempts. Later results remain verbatim "
        "outside the note's covered boundary; they are not summarized or discarded by this tool. "
        "Use at meaningful stage boundaries or budget pressure, not every turn."
    )
    return ToolDefinition(
        name="context_rotate", description=description,
        input_schema={
            "type": "object", "additionalProperties": False,
            "properties": {
                "expected_epoch": {"type": "integer", "minimum": 0},
                "expected_note_revision": {"type": "integer", "minimum": 1},
                "reason": {"type": "string", "enum": list(ROTATION_REASONS)},
            },
            "required": ["expected_epoch", "expected_note_revision"],
        },
        validator=validate_rotation, run=rotate,
        metadata=ToolMetadata(
            name="context_rotate", description=description,
            effect=ToolEffect.READ_ONLY if readonly_context and type(rotation) is ContextRotation else ToolEffect.WORKSPACE_WRITE,
            replay_policy=ReplayPolicy.NEVER_AUTO_RETRY, tags=["memory", "paged", "context", "state"],
        ),
    )


__all__ = ["create_paged_memory_tools"]
