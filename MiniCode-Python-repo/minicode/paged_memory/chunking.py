"""Lossless UTF-8 chunk boundaries for searchable text artifacts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
            while (
                end < len(payload)
                and payload[end] & 0b1100_0000 == 0b1000_0000
            ):
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
            latter_half = len(candidate) // 2
            newline = candidate.rfind("\n", latter_half)
            if newline >= latter_half:
                candidate = candidate[: newline + 1]
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


__all__ = ["TextChunk", "artifact_chunk_records", "chunk_utf8_text"]
