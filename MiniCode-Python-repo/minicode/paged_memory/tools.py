"""Scoped model tools for on-demand reads from paged memory."""

from __future__ import annotations

import base64
import json
from typing import Any

from minicode.runtime_protocol import ReplayPolicy, ToolEffect
from minicode.tooling import (
    ToolCapability,
    ToolDefinition,
    ToolMetadata,
    ToolResult,
)

from .models import MemoryRecord


def _integer(value: Any, *, name: str, default: int, minimum: int = 0) -> int:
    parsed = default if value in (None, "") else int(value)
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
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
                        "use memory_get only for a source range that is not already present."
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
        artifact_id = str(value.get("artifact_id", "")).strip()
        if not artifact_id:
            raise ValueError("artifact_id is required")
        unit = str(value.get("unit", "BYTE")).upper()
        if unit not in {"BYTE", "CHAR", "LINE", "PAGE"}:
            raise ValueError("unit must be BYTE, CHAR, LINE, or PAGE")
        start = _integer(value.get("start"), name="start", default=0)
        default_span = (
            max_text_units if unit in {"BYTE", "CHAR"} else max_structured_units
        )
        end = _integer(
            value.get("end"), name="end", default=start + default_span, minimum=start
        )
        if end == start:
            raise ValueError("end must be greater than start")
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
                        "requested_end": value["end"],
                        "max_page_tokens": max_page_tokens,
                        "request_clamped": request_clamped,
                        "has_more": has_more,
                        "next_start": page_end if has_more is not False else None,
                    },
                    "encoding": encoding,
                    "truncated_to_page_budget": False,
                    "content": content,
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
    return [
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
            description="Read a bounded byte/character/line/page range from a current-task Artifact.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    **range_properties,
                    "query": {"type": "string"},
                    "log_level": {"type": "string"},
                },
                "required": ["artifact_id"],
            },
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


__all__ = ["create_paged_memory_tools"]
