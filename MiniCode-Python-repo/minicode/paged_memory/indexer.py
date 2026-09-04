"""Idempotent transactional-outbox consumer for the rebuildable vector index."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from .config import PagedMemoryConfig
from .models import (
    AgentEvent,
    EventType,
    MemoryRecord,
    MemoryStatus,
    OutboxClaimLostError,
    OutboxRecord,
    content_hash_text,
    now_epoch,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedIndexWork:
    outbox: OutboxRecord
    tenant_id: str
    task_id: str
    memory_id: str
    memory: MemoryRecord | None = None
    count_as: str = "processed"


def make_memory_index_outbox(
    memory: MemoryRecord, *, index_descriptor: Any
) -> OutboxRecord:
    """Build deterministic work targeted at exactly one compatible index."""

    index_id = str(index_descriptor.index_id)
    collection = str(index_descriptor.collection)
    if not index_id or not collection:
        raise ValueError("index descriptor must identify an index and collection")
    identity = content_hash_text(
        f"{memory.id}:{memory.content_hash}:{index_id}"
    ).removeprefix("sha256:")
    return OutboxRecord(
        aggregate_type="MEMORY",
        aggregate_id=memory.id,
        event_type="UPSERT_MEMORY_INDEX",
        id=f"outbox_{identity[:48]}",
        payload={
            "tenant_id": memory.tenant_id,
            "task_id": memory.task_id,
            "memory_id": memory.id,
            "content_hash": memory.content_hash,
            "index_id": index_id,
            "collection": collection,
            "index_descriptor": index_descriptor.to_dict(),
        },
    )


class OutboxIndexer:
    def __init__(
        self, *, authority: Any, vector_index: Any, config: PagedMemoryConfig
    ) -> None:
        self.authority = authority
        self.vector_index = vector_index
        self.config = config
        descriptor = getattr(vector_index, "index_descriptor", None)
        if descriptor is None:
            raise TypeError("vector index must expose an immutable index_descriptor")
        self.index_descriptor: Any = descriptor
        self.index_id = str(self.index_descriptor.index_id)

    def run_once(self, *, limit: int = 100) -> dict[str, int]:
        counts = {"processed": 0, "retried": 0, "failed": 0, "skipped": 0}
        claim = getattr(self.authority, "claim_outbox", None)
        batch_limit = max(1, limit)
        outboxes = (
            claim(
                limit=batch_limit,
                lease_seconds=max(
                    60.0, float(self.config.milvus_timeout_seconds) * 3.0
                ),
                index_id=self.index_id,
            )
            if callable(claim)
            else self.authority.pending_outbox(limit=batch_limit)
        )
        upserts: list[_PreparedIndexWork] = []
        deletes: list[_PreparedIndexWork] = []
        for outbox in outboxes:
            if outbox.event_type != "UPSERT_MEMORY_INDEX":
                try:
                    self.authority.complete_outbox(
                        outbox.id, claim_token=outbox.claim_token
                    )
                except OutboxClaimLostError:
                    logger.info("Outbox lease was superseded: %s", outbox.id)
                counts["skipped"] += 1
                continue
            tenant_id = ""
            task_id = ""
            memory_id = outbox.aggregate_id
            try:
                tenant_id = str(outbox.payload.get("tenant_id", ""))
                task_id = str(outbox.payload.get("task_id", ""))
                memory_id = str(outbox.payload.get("memory_id", outbox.aggregate_id))
                if not tenant_id or not task_id or memory_id != outbox.aggregate_id:
                    raise ValueError("outbox scope/aggregate mismatch")
                if str(outbox.payload.get("index_id", "")) != self.index_id:
                    raise ValueError("outbox targets a different vector index descriptor")
                target_collection = str(outbox.payload.get("collection", ""))
                if target_collection != self.index_descriptor.collection:
                    raise ValueError("outbox targets a different Milvus collection")
                if not self._task_is_indexable(tenant_id=tenant_id, task_id=task_id):
                    deletes.append(
                        _PreparedIndexWork(
                            outbox=outbox,
                            tenant_id=tenant_id,
                            task_id=task_id,
                            memory_id=memory_id,
                            count_as="skipped",
                        )
                    )
                    continue
                memories = self.authority.get_memories(
                    tenant_id=tenant_id, task_id=task_id, memory_ids=[memory_id]
                )
                if not memories:
                    # A task may be deleted after this row was leased. Remove a
                    # possible earlier vector upsert before acknowledging it.
                    deletes.append(
                        _PreparedIndexWork(
                            outbox=outbox,
                            tenant_id=tenant_id,
                            task_id=task_id,
                            memory_id=memory_id,
                            count_as="skipped",
                        )
                    )
                    continue
                memory = memories[0]
                expected_hash = str(outbox.payload.get("content_hash", ""))
                if expected_hash and expected_hash != memory.content_hash:
                    raise ValueError(
                        "outbox content hash differs from authority registry"
                    )
                work = _PreparedIndexWork(
                    outbox=outbox,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    memory_id=memory.id,
                    memory=memory,
                )
                if memory.status in {MemoryStatus.ACTIVE, MemoryStatus.OPEN}:
                    upserts.append(work)
                else:
                    deletes.append(work)
            except Exception as exc:  # noqa: BLE001 - adapter failures are retried.
                self._handle_failure(
                    outbox=outbox,
                    error=exc,
                    counts=counts,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    memory_id=memory_id,
                )
        self._process_deletes(deletes, counts)
        self._process_upserts(upserts, counts)
        return counts

    def _process_deletes(
        self, work: list[_PreparedIndexWork], counts: dict[str, int]
    ) -> None:
        if not work:
            return
        try:
            self.vector_index.delete(
                list(dict.fromkeys(item.memory_id for item in work))
            )
        except Exception as exc:  # noqa: BLE001 - adapter failures are retried.
            for item in work:
                self._retry_or_dead_letter(item.outbox, exc, counts)
            return
        for item in work:
            try:
                self.authority.complete_outbox(
                    item.outbox.id, claim_token=item.outbox.claim_token
                )
                counts[item.count_as] += 1
            except Exception as exc:  # noqa: BLE001 - authority adapters vary.
                self._retry_or_dead_letter(item.outbox, exc, counts)

    def _process_upserts(
        self, work: list[_PreparedIndexWork], counts: dict[str, int]
    ) -> None:
        if not work:
            return
        records = [item.memory for item in work if item.memory is not None]
        try:
            # Embedding and Milvus upsert are intentionally vectorized. The
            # primary key makes a partial external retry idempotent.
            self.vector_index.upsert(records)
        except ValueError as exc:
            # Isolate deterministic validation failures so one malformed card
            # cannot repeatedly poison every valid card in the claimed batch.
            if len(work) > 1:
                for item in work:
                    self._process_upserts([item], counts)
            else:
                self._retry_or_dead_letter(work[0].outbox, exc, counts)
            return
        except Exception as exc:  # noqa: BLE001 - transient SDK failures vary.
            for item in work:
                self._retry_or_dead_letter(item.outbox, exc, counts)
            return

        cleanup: list[_PreparedIndexWork] = []
        collection = self.index_descriptor.collection
        for item in work:
            memory = item.memory
            if memory is None:
                continue
            try:
                if not self._task_is_indexable(
                    tenant_id=item.tenant_id, task_id=item.task_id
                ):
                    cleanup.append(
                        _PreparedIndexWork(
                            outbox=item.outbox,
                            tenant_id=item.tenant_id,
                            task_id=item.task_id,
                            memory_id=item.memory_id,
                            count_as="skipped",
                        )
                    )
                    continue
                self.authority.update_memory_indexed(
                    tenant_id=item.tenant_id,
                    task_id=item.task_id,
                    memory_id=memory.id,
                    collection=collection,
                    primary_key=memory.id,
                )
                indexed_event = AgentEvent(
                    tenant_id=item.tenant_id,
                    task_id=item.task_id,
                    run_id=memory.run_id,
                    step_id=memory.step_id,
                    event_type=EventType.MEMORY_INDEXED,
                    content_text=(
                        f"memory={memory.id}; collection={collection}; "
                        f"index_id={self.index_id}; hash={memory.content_hash}"
                    ),
                    content_json={
                        "memory_id": memory.id,
                        "collection": collection,
                        "index_id": self.index_id,
                        "content_hash": memory.content_hash,
                    },
                    content_hash=memory.content_hash,
                    caused_by_event_id=memory.source_event_id,
                    correlation_id=item.outbox.id,
                    idempotency_key=f"outbox:{item.outbox.id}:indexed",
                )
                self.authority.persist_event(indexed_event)
                self.authority.complete_outbox(
                    item.outbox.id, claim_token=item.outbox.claim_token
                )
                counts["processed"] += 1
            except Exception as exc:  # noqa: BLE001 - authority adapters vary.
                self._handle_failure(
                    outbox=item.outbox,
                    error=exc,
                    counts=counts,
                    tenant_id=item.tenant_id,
                    task_id=item.task_id,
                    memory_id=item.memory_id,
                )
        self._process_deletes(cleanup, counts)

    def _handle_failure(
        self,
        *,
        outbox: OutboxRecord,
        error: Exception,
        counts: dict[str, int],
        tenant_id: str,
        task_id: str,
        memory_id: str,
    ) -> None:
        if tenant_id and task_id and memory_id:
            try:
                remaining = self.authority.get_memories(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    memory_ids=[memory_id],
                )
                if not remaining or not self._task_is_indexable(
                    tenant_id=tenant_id, task_id=task_id
                ):
                    self.vector_index.delete([memory_id])
                    self.authority.complete_outbox(
                        outbox.id, claim_token=outbox.claim_token
                    )
                    counts["skipped"] += 1
                    return
            except Exception:
                logger.debug(
                    "Could not classify outbox failure as a deletion race",
                    exc_info=True,
                )
        self._retry_or_dead_letter(outbox, error, counts)

    def _retry_or_dead_letter(
        self,
        outbox: OutboxRecord,
        error: Exception,
        counts: dict[str, int],
    ) -> None:
        if outbox.retry_count + 1 >= self.config.indexer_max_retries:
            try:
                self.authority.dead_letter_outbox(
                    outbox.id, claim_token=outbox.claim_token
                )
            except OutboxClaimLostError:
                counts["skipped"] += 1
                logger.info("Outbox lease was superseded: %s", outbox.id)
                return
            counts["failed"] += 1
            logger.error("Memory index outbox permanently failed: %s", outbox.id)
            return
        delay = self.config.indexer_retry_backoff_seconds * (
            2 ** min(outbox.retry_count, 8)
        )
        try:
            self.authority.fail_outbox(
                outbox.id,
                claim_token=outbox.claim_token,
                retry_at=now_epoch() + delay,
            )
        except OutboxClaimLostError:
            counts["skipped"] += 1
            logger.info("Outbox lease was superseded: %s", outbox.id)
            return
        counts["retried"] += 1
        logger.warning(
            "Memory index outbox retry %s after %s: %s",
            outbox.id,
            type(error).__name__,
            error,
        )

    def _task_is_indexable(self, *, tenant_id: str, task_id: str) -> bool:
        reader = getattr(self.authority, "task_status", None)
        if not callable(reader):
            return True
        return reader(tenant_id=tenant_id, task_id=task_id) == "ACTIVE"

    def drain(self, *, batch_size: int = 100, max_batches: int = 100) -> dict[str, int]:
        totals = {"processed": 0, "retried": 0, "failed": 0, "skipped": 0, "batches": 0}
        for _ in range(max_batches):
            counts = self.run_once(limit=batch_size)
            totals["batches"] += 1
            for key in ("processed", "retried", "failed", "skipped"):
                totals[key] += counts[key]
            if sum(counts.values()) == 0 or counts["retried"] or counts["failed"]:
                break
        return totals

    def run_forever(
        self,
        *,
        stop_event: threading.Event,
        poll_seconds: float = 1.0,
        batch_size: int = 100,
    ) -> None:
        while not stop_event.is_set():
            counts = self.run_once(limit=batch_size)
            if sum(counts.values()) == 0:
                stop_event.wait(max(0.05, poll_seconds))


__all__ = ["OutboxIndexer", "make_memory_index_outbox"]
