"""Lossless UTF-8 chunk boundaries for searchable text artifacts."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, replace

from .models import (
    AgentEvent,
    ArtifactRecord,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
    SourceRange,
    content_hash_bytes,
    content_hash_text,
)


@dataclass(frozen=True)
class TextChunk:
    start: int
    end: int
    text: str


def _structure_boundaries(text: str):
    boundaries = []
    fences = []
    offset = 0
    opened = None
    marker = ""
    for line in text.splitlines(keepends=True):
        end = offset + len(line.encode("utf-8"))
        match = re.match(r"\s*(`{3,}|~{3,})", line)
        if match:
            token = match.group(1)
            if opened is None:
                opened, marker = offset, token
                boundaries.append(offset)
            elif token[0] == marker[0] and len(token) >= len(marker):
                fences.append((opened, end))
                boundaries.append(end)
                opened = None
        elif opened is None:
            if not line.strip():
                boundaries.append(end)
            elif re.match(r"(?:#{1,6}\s|(?:async\s+)?def\s|class\s)", line):
                boundaries.append(offset)
        offset = end
    if opened is not None:
        fences.append((opened, offset))
    return sorted(set(boundaries)), fences


def chunk_utf8_text(
    text: str,
    *,
    max_bytes: int = 4_500,
    max_tokens: int | None = None,
    estimate_tokens: Callable[[str], int] | None = None,
) -> list[TextChunk]:
    """Split text into exact byte ranges without cutting UTF-8 code points.

    Newline boundaries are preferred in the latter half of a chunk.  There is
    deliberately no overlap: source ranges remain unambiguous and repeated log
    lines do not inflate retrieval scores.
    """

    if max_bytes < 256:
        raise ValueError("max_bytes must be at least 256")
    if max_tokens is not None and max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if max_tokens is not None and estimate_tokens is None:
        raise ValueError("estimate_tokens is required with max_tokens")
    payload = text.encode("utf-8")
    if not payload:
        return []
    chunks: list[TextChunk] = []
    boundaries, fences = _structure_boundaries(text)
    fence_starts = [span[0] for span in fences]
    start = 0
    while start < len(payload):
        hard_end = min(len(payload), start + max_bytes)
        end = hard_end
        while (
            end > start
            and end < len(payload)
            and payload[end] & 0b1100_0000 == 0b1000_0000
        ):
            end -= 1
        if end <= start:
            end = hard_end
            while end < len(payload) and payload[end] & 0b1100_0000 == 0b1000_0000:
                end += 1

        candidate = payload[start:end].decode("utf-8")
        if (
            max_tokens is not None
            and estimate_tokens is not None
            and estimate_tokens(candidate) > max_tokens
        ):
            low = 1
            high = len(candidate)
            best = 1
            while low <= high:
                middle = (low + high) // 2
                prefix = candidate[:middle]
                if estimate_tokens(prefix) <= max_tokens:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            candidate = candidate[:best]
            end = start + len(candidate.encode("utf-8"))
        if end < len(payload):
            structural_end = 0
            index = bisect_right(boundaries, end) - 1
            if index >= 0 and boundaries[index] > start + (end - start) // 2:
                structural_end = boundaries[index]
            fence_index = bisect_right(fence_starts, end) - 1
            if fence_index >= 0:
                opening, closing = fences[fence_index]
                if start < opening < end < closing:
                    # Start the code block in the next chunk so a block that
                    # fits the model window is not needlessly split in two.
                    structural_end = opening
            if structural_end > start:
                end = structural_end
                candidate = payload[start:end].decode("utf-8")
                chunks.append(TextChunk(start=start, end=end, text=candidate))
                start = end
                continue
            latter_half = len(candidate) // 2
            # Prefer paragraph/line boundaries, then complete natural-language
            # sentences. Every byte still belongs to exactly one source range.
            boundary = candidate.rfind("\n", latter_half)
            boundary = boundary + 1 if boundary >= latter_half else 0
            if not boundary:
                endings = list(
                    re.finditer(r"[。！？]|[.!?](?=\s)", candidate[latter_half:])
                )
                if endings:
                    boundary = latter_half + endings[-1].end()
            if boundary:
                candidate = candidate[:boundary]
                end = start + len(candidate.encode("utf-8"))
        chunks.append(TextChunk(start=start, end=end, text=candidate))
        start = end
    return chunks


def artifact_chunk_records(
    *,
    text: str,
    event: AgentEvent,
    artifact: ArtifactRecord,
    tool_name: str,
    phase: str,
    ok: bool,
    estimate_tokens: Callable[[str], int],
    max_bytes: int = 4_500,
    max_tokens: int = 384,
) -> list[MemoryRecord]:
    """Build deterministic, independently verifiable text-artifact cards."""

    if not artifact.media_type.casefold().startswith("text/"):
        return []
    chunks = chunk_utf8_text(
        text,
        max_bytes=max_bytes,
        max_tokens=max_tokens,
        estimate_tokens=estimate_tokens,
    )
    records: list[MemoryRecord] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_bytes = chunk.text.encode("utf-8")
        identity = content_hash_text(
            f"{event.id}:{artifact.id}:{chunk.start}:{chunk.end}"
        ).removeprefix("sha256:")
        records.append(
            MemoryRecord(
                id=f"memory_chunk_{identity[:32]}",
                tenant_id=event.tenant_id,
                task_id=event.task_id,
                run_id=event.run_id,
                step_id=event.step_id,
                memory_type=MemoryType.ARTIFACT_CHUNK,
                title=f"{tool_name} output {index}/{len(chunks)}",
                preview_text=chunk.text,
                status=MemoryStatus.ACTIVE if ok else MemoryStatus.OPEN,
                importance=0.68 if ok else 0.88,
                source_event_id=event.id,
                artifact_id=artifact.id,
                source_range=SourceRange(
                    unit="BYTE",
                    start=chunk.start,
                    end=chunk.end,
                ),
                content_hash=content_hash_bytes(chunk_bytes),
                token_count=estimate_tokens(chunk.text),
                phase=phase,
                facets=[
                    "artifact_chunk",
                    "tool_result",
                    tool_name,
                    "success" if ok else "error",
                ],
            )
        )
    return records


def event_chunk_records(
    *,
    event: AgentEvent,
    role: str,
    phase: str,
    estimate_tokens: Callable[[str], int],
    max_tokens: int = 384,
) -> list[MemoryRecord]:
    """Index the complete conversation source, not only its navigation preview."""
    chunks = chunk_utf8_text(
        event.content_text, max_tokens=max_tokens, estimate_tokens=estimate_tokens
    )
    return [
        MemoryRecord(
            id="memory_event_chunk_"
            + content_hash_text(f"{event.id}:{chunk.start}:{chunk.end}").removeprefix(
                "sha256:"
            )[:32],
            tenant_id=event.tenant_id,
            task_id=event.task_id,
            run_id=event.run_id,
            step_id=event.step_id,
            memory_type=MemoryType.FACT,
            title=f"{role} message {index}/{len(chunks)}",
            preview_text=chunk.text,
            importance=0.80 if role == "user" else 0.60,
            source_event_id=event.id,
            source_range=SourceRange(unit="BYTE", start=chunk.start, end=chunk.end),
            content_hash=content_hash_bytes(chunk.text.encode("utf-8")),
            token_count=estimate_tokens(chunk.text),
            phase=phase,
            facets=["conversation", "event_chunk", role],
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def tool_event_chunk_records(*, event, tool_name, phase, ok, estimate_tokens, max_tokens=384):
    """Index inline tool bytes with the same source coordinates as artifact chunks."""
    records = event_chunk_records(event=event, role="tool_result", phase=phase,
                                  estimate_tokens=estimate_tokens, max_tokens=max_tokens)
    return [replace(record, memory_type=MemoryType.TOOL_RESULT if ok else MemoryType.ERROR,
                    title=f"{tool_name} output {index}/{len(records)}",
                    status=MemoryStatus.ACTIVE if ok else MemoryStatus.OPEN,
                    importance=0.68 if ok else 0.88,
                    facets=["event_chunk", "tool_result", tool_name, "success" if ok else "error"])
            for index, record in enumerate(records, start=1)]


def tool_text_chunk_records(*, output, event, artifact, tool_name, phase, ok,
                            estimate_tokens, raw_page_token_limit):
    """The same projection policy for capture, replay and maintenance backfill."""
    max_tokens = max(1, min(384, raw_page_token_limit))
    if artifact is None:
        preview_bytes = max(800, min(8_000, raw_page_token_limit * 4))
        if len(output.encode("utf-8")) <= preview_bytes:
            return []
        return tool_event_chunk_records(event=event, tool_name=tool_name, phase=phase,
            ok=ok, estimate_tokens=estimate_tokens, max_tokens=max_tokens)
    return artifact_chunk_records(text=output, event=event, artifact=artifact,
        tool_name=tool_name, phase=phase, ok=ok, estimate_tokens=estimate_tokens,
        max_bytes=max(1_024, min(7_000, raw_page_token_limit * 3)), max_tokens=max_tokens)


__all__ = [
    "TextChunk",
    "artifact_chunk_records",
    "chunk_utf8_text",
    "event_chunk_records",
    "tool_event_chunk_records",
    "tool_text_chunk_records",
]
