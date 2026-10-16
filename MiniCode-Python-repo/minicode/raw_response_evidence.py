"""Exact, bounded evidence for provider HTTP responses.

This module records the bytes the HTTP client actually exposes to the adapter,
once, at the boundary where the response is consumed. It does not re-read the
stream, does not change the caller's streaming choice, does not widen any
logging surface, and never grants authority.

What it can prove
-----------------
Exactness is bounded, never guessed. ``observed_bytes`` and ``sha256`` always
describe the complete byte stream of one HTTP attempt; ``stored_bytes`` and
``stored_complete`` describe how much of that stream is replayable from local
evidence. A capture with ``capture_complete`` false is explicitly incomplete and
must not be used to claim that the provider did or did not produce some shape.

This is not a network trace: ``raw_bytes`` here are the response bytes the HTTP
library handed to the adapter (decompressed, transfer-decoded), not TLS records
or a packet capture.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from minicode.runtime_artifacts import RuntimeArtifactStore
from minicode.runtime_protocol import ArtifactRef

SCHEMA = "raw-response-evidence-v1"
CHUNK_MEDIA_TYPE = "application/vnd.minicode.raw-response-chunk+octet-stream"
INDEX_MEDIA_TYPE = "application/vnd.minicode.raw-response-index+json"
DEFAULT_MAX_BYTES = 262_144
DEFAULT_CHUNK_BYTES = 65_536
DEFAULT_MAX_LINE_BYTES = 16_384
DEFAULT_LINE_PREFIX_BYTES = 512
# Metadata bounds are separate from the payload-byte bound. A stream of tiny
# events can stay far below ``max_bytes`` while still producing unbounded
# per-event metadata, so the retained event list needs its own hard ceiling.
DEFAULT_MAX_EVENTS = 4_096
DEFAULT_MAX_EVENT_METADATA_BYTES = 1_048_576
POLICY = "raw-http-response-evidence-v1"

_LIMIT_NAMES = ("max_bytes", "chunk_bytes", "max_line_bytes", "line_prefix_bytes",
                "max_events", "max_event_metadata_bytes")


@dataclass(frozen=True, slots=True)
class RawCaptureLimits:
    """Hard bounds for one HTTP attempt's evidence; exceeding them is visible."""

    max_bytes: int = DEFAULT_MAX_BYTES
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    line_prefix_bytes: int = DEFAULT_LINE_PREFIX_BYTES
    max_events: int = DEFAULT_MAX_EVENTS
    max_event_metadata_bytes: int = DEFAULT_MAX_EVENT_METADATA_BYTES

    def __post_init__(self) -> None:
        for name in _LIMIT_NAMES:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"raw capture limit {name} must be a positive integer")
        if self.chunk_bytes > self.max_bytes:
            raise ValueError("raw capture chunk cannot exceed its total byte bound")
        if self.line_prefix_bytes > self.max_line_bytes:
            raise ValueError("raw capture line prefix cannot exceed its line bound")

    def to_dict(self) -> dict[str, int]:
        """The exact limits in force; every public field names its own bound."""
        return {name: getattr(self, name) for name in _LIMIT_NAMES}


class RawResponseCapture:
    """Append-only, bounded recorder for one HTTP attempt's raw response.

    The caller feeds bytes in the same order the parser consumes them, so the
    recorded prefix is byte-identical to the parsed prefix. No method here
    retries, re-reads or reorders anything.
    """

    def __init__(
        self,
        *,
        artifacts: RuntimeArtifactStore | None,
        run_id: str,
        node_id: str,
        attempt_id: str,
        model_call_id: str | None,
        http_attempt: int,
        transport: str,
        limits: RawCaptureLimits | None = None,
    ) -> None:
        if transport not in {"stream", "non_stream", "error_body"}:
            raise ValueError("raw capture requires a recognized transport mode")
        if type(http_attempt) is not int or http_attempt < 1:
            raise ValueError("raw capture requires a positive HTTP attempt number")
        self.artifacts = artifacts
        self.run_id, self.node_id, self.attempt_id = run_id, node_id, attempt_id
        self.model_call_id, self.http_attempt, self.transport = model_call_id, http_attempt, transport
        self.limits = limits or RawCaptureLimits()
        self.started_at = time.time()
        self._digest = hashlib.sha256()
        self._observed_bytes = 0
        self._events: list[dict[str, Any]] = []
        self._events_dropped = 0
        self._event_metadata_bytes = 0
        self._stored_bytes = 0
        self._chunks: list[ArtifactRef] = []
        self._buffer = bytearray()
        self._sequence = 0
        self._store_failed = False
        self._truncated_input = False
        self._line_overflow = False
        self._event_overflow = False
        self._first_interesting: int | None = None
        self._last_interesting: int | None = None
        self._body_decode_ok: bool | None = None
        self._body_decode_error: str | None = None

    # ---- ingestion -----------------------------------------------------

    def _feed(self, data: bytes) -> None:
        """Hash the complete stream; store only within the declared bounds."""
        self._digest.update(data)
        self._observed_bytes += len(data)
        remaining = self.limits.max_bytes - self._stored_bytes
        if remaining <= 0:
            self._truncated_input = True
            return
        kept = data if len(data) <= remaining else data[:remaining]
        if len(kept) != len(data):
            self._truncated_input = True
        self._buffer += kept
        self._stored_bytes += len(kept)
        self._flush_chunks(full_only=True)

    def _flush_chunks(self, *, full_only: bool) -> None:
        if self.artifacts is None:
            self._store_failed = True
            return
        while len(self._buffer) >= self.limits.chunk_bytes or (
            not full_only and self._buffer
        ):
            take = self.limits.chunk_bytes if len(self._buffer) > self.limits.chunk_bytes else len(self._buffer)
            if full_only and take < self.limits.chunk_bytes:
                return
            block = bytes(self._buffer[:take])
            del self._buffer[:take]
            try:
                self._chunks.append(self.artifacts.put_bytes(
                    block, run_id=self.run_id, node_id=self.node_id, media_type=CHUNK_MEDIA_TYPE))
            except Exception:  # noqa: BLE001 - evidence loss must stay observable, never fatal
                self._store_failed = True
                return

    def observe_line(self, line: bytes, *, interesting: bool = False) -> dict[str, Any]:
        """Record one transport event exactly as the caller received it.

        ``redacted`` means the stored prefix is shorter than the event; the byte
        count and hash still describe the complete event.
        """
        if not isinstance(line, (bytes, bytearray)):
            raise TypeError("raw capture requires bytes as delivered by the HTTP client")
        data = bytes(line)
        self._feed(data)
        self._sequence += 1
        prefix = data[:self.limits.line_prefix_bytes]
        redacted = len(prefix) != len(data)
        if len(data) > self.limits.max_line_bytes:
            self._line_overflow = True
        if interesting:
            if self._first_interesting is None:
                self._first_interesting = self._sequence
            self._last_interesting = self._sequence
        event = {
            "sequence": self._sequence,
            "observed_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "stored_prefix_bytes": len(prefix),
            "redacted": redacted,
            "interesting": interesting,
        }
        self._retain_event(event)
        return event

    def _retain_event(self, event: dict[str, Any]) -> None:
        """Keep per-event metadata only within its own declared bounds.

        The event count and the stream hash keep advancing whether or not the
        metadata is retained, so ``event_count`` stays the true number of
        transport events while ``events_dropped`` states exactly how much
        metadata was not kept. Overflow is never silent: it is a failure reason
        on the receipt and clears ``event_index_complete``.
        """
        size = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if (len(self._events) >= self.limits.max_events
                or self._event_metadata_bytes + size > self.limits.max_event_metadata_bytes):
            self._events_dropped += 1
            self._event_overflow = True
            return
        self._events.append(event)
        self._event_metadata_bytes += size

    def observe_text(self, text: str) -> int:
        """Record a non-streaming body in one step; returns stored chunk count."""
        self._feed(text.encode("utf-8", errors="surrogatepass"))
        return len(self._chunks)

    def observe_bytes(self, data: bytes) -> int:
        """Record a whole body from the exact bytes that were read.

        Decoding a body with a replacement error handler and then re-encoding it
        produces different bytes whenever the body is not valid UTF-8, so the
        stored prefix would no longer be the provider's response and its hash
        would not be the raw hash. Callers that hold bytes pass those bytes here.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("raw capture requires bytes as delivered by the HTTP client")
        self._feed(bytes(data))
        return len(self._chunks)

    def note_body_decode(self, *, ok: bool, error_type: str | None = None) -> None:
        """Record whether the body decoded as UTF-8, without altering the bytes."""
        self._body_decode_ok = bool(ok)
        self._body_decode_error = None if ok else (error_type or "UnicodeDecodeError")

    def begin(self) -> dict[str, Any]:
        """Reset per-request state for one HTTP attempt.

        One recorder can outlive several attempts; the header keeps each
        attempt's identity so a later attempt can never overwrite an earlier
        one's evidence.
        """
        self.started_at = time.time()
        self._digest = hashlib.sha256()
        self._observed_bytes = 0
        self._events = []
        self._events_dropped = 0
        self._event_metadata_bytes = 0
        self._stored_bytes = 0
        self._chunks = []
        self._buffer = bytearray()
        self._sequence = 0
        self._store_failed = False
        self._truncated_input = False
        self._line_overflow = False
        self._event_overflow = False
        self._first_interesting = None
        self._last_interesting = None
        self._body_decode_ok = None
        self._body_decode_error = None
        return self.header()

    def header(self) -> dict[str, Any]:
        """Attempt identity recorded before the HTTP client is called."""
        return {
            "schema": SCHEMA,
            "policy": POLICY,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "model_call_id": self.model_call_id,
            "http_attempt": self.http_attempt,
            "transport": self.transport,
            "raw_bytes_scope": "http_client_response_bytes_not_tls_capture",
            "started_at": self.started_at,
            "limits": self.limits.to_dict(),
            "grants_authority": False,
            "counts_as_acceptance": False,
        }

    # ---- finalization --------------------------------------------------

    def _failure_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.artifacts is None:
            reasons.append("no_local_artifact_store")
        if self._store_failed:
            reasons.append("artifact_write_failed")
        if self._truncated_input:
            reasons.append("byte_bound_exceeded")
        if self._line_overflow:
            reasons.append("event_bound_exceeded")
        if self._event_overflow:
            reasons.append("event_metadata_bound_exceeded")
        return reasons

    def finalize(self, *, transport_termination: str, error_type: str | None = None,
                 complete: bool | None = None) -> dict[str, Any]:
        """Freeze the receipt; an incomplete capture is never silently complete.

        ``transport_termination`` describes what actually ended the read:
        ``done_marker``, ``eof``, ``exception``, ``abandoned`` or ``unknown``.
        """
        if transport_termination not in {
            "done_marker", "eof", "exception", "abandoned", "unknown"
        }:
            raise ValueError("raw capture requires a recognized transport termination")
        self._flush_chunks(full_only=False)
        reasons = self._failure_reasons()
        stored_complete = not reasons
        if complete is not None and complete is False and "caller_reported_incomplete" not in reasons:
            reasons = [*reasons, "caller_reported_incomplete"]
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "policy": POLICY,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "model_call_id": self.model_call_id,
            "http_attempt": self.http_attempt,
            "transport": self.transport,
            "transport_termination": transport_termination,
            "error_type": error_type,
            "body_decode_ok": self._body_decode_ok,
            "body_decode_error": self._body_decode_error,
            "raw_bytes_scope": "http_client_response_bytes_not_tls_capture",
            "observed_bytes": self._observed_bytes,
            "observed_sha256": self._digest.hexdigest(),
            "stored_bytes": self._stored_bytes,
            "stored_complete": stored_complete,
            "capture_complete": stored_complete,
            "incomplete_reasons": reasons,
            "event_count": self._sequence,
            "events_stored": len(self._events),
            "events_dropped": self._events_dropped,
            "event_metadata_bytes": self._event_metadata_bytes,
            "event_index_complete": self._events_dropped == 0,
            "interesting_event_range": (
                [self._first_interesting, self._last_interesting]
                if self._first_interesting is not None else None
            ),
            "line_prefix_bytes": self.limits.line_prefix_bytes,
            "limits": self.limits.to_dict(),
            "chunk_refs": [ref.to_dict() for ref in self._chunks],
            "started_at": self.started_at,
            "finished_at": time.time(),
            "grants_authority": False,
            "counts_as_acceptance": False,
        }
        return receipt

    def event_index(self) -> dict[str, Any]:
        """Per-event metadata only; never the payload bytes themselves.

        ``events`` is bounded by the declared metadata limits. When it is not the
        complete list, ``event_index_complete`` is false and ``events_dropped``
        states how many events were not retained; ``event_count`` remains the
        true number of transport events observed.
        """
        return {
            "schema": SCHEMA,
            "policy": POLICY,
            "model_call_id": self.model_call_id,
            "http_attempt": self.http_attempt,
            "transport": self.transport,
            "events": list(self._events),
            "event_count": self._sequence,
            "events_stored": len(self._events),
            "events_dropped": self._events_dropped,
            "event_metadata_bytes": self._event_metadata_bytes,
            "event_index_complete": self._events_dropped == 0,
            "limits": self.limits.to_dict(),
            "observed_bytes": self._observed_bytes,
            "observed_sha256": self._digest.hexdigest(),
        }

    @property
    def stored_chunks(self) -> tuple[ArtifactRef, ...]:
        return tuple(self._chunks)


def put_evidence(
    store: RuntimeArtifactStore,
    capture: RawResponseCapture,
    *,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Persist the receipt and its event index; return the linked references.

    Storage failures are reported, not raised: losing the index must not change
    execution, and the receipt stays visibly unusable for replay.
    """
    linked = dict(receipt)
    try:
        index_ref = store.put_text(
            json.dumps(capture.event_index(), ensure_ascii=False, separators=(",", ":")),
            run_id=capture.run_id, node_id=capture.node_id, media_type=INDEX_MEDIA_TYPE)
        linked["event_index_ref"] = index_ref.to_dict()
    except Exception as error:  # noqa: BLE001 - evidence loss must stay observable
        linked["event_index_ref"] = None
        linked["capture_complete"] = False
        linked["stored_complete"] = False
        linked["incomplete_reasons"] = [
            *linked.get("incomplete_reasons", []), "index_write_failed"]
        linked["index_error_type"] = type(error).__name__
    return linked
