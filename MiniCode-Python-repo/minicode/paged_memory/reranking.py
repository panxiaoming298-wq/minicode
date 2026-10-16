"""Optional external reranking with retained fallback, bounded work and a circuit."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeout
from contextvars import copy_context
from dataclasses import replace
from threading import BoundedSemaphore, Lock

from minicode.provider_usage import current_usage_observation, execution_memory_rerank_calls

from .config import PagedMemoryConfig
from .models import MemoryHit
from .policy import TokenBudgetManager
from .query import retrieval_query
from .rerank_api import RERANK_PROMPT, ExternalRerankApi, RerankFailure
from .rerank_coordination import SharedRerankCoordinator
from .rerank_selection import balanced_candidates, rerank_route
from .request_lane import RequestLane


class RerankCircuit:
    def __init__(self, threshold: int, cooldown: float) -> None:
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.blocked_until = 0.0
        self.inflight = False
        self._lock = Lock()

    def acquire(self, now: float) -> str:
        with self._lock:
            if now < self.blocked_until:
                return "configuration_disabled" if self.blocked_until == float("inf") else "circuit_open"
            if self.inflight:
                return "busy"
            self.inflight = True
            return ""

    def release(self) -> None:
        with self._lock:
            self.inflight = False

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.blocked_until = 0.0
            self.inflight = False

    def failure(
        self, error: RerankFailure, now: float, *, release: bool = True
    ) -> None:
        with self._lock:
            self.failures += 1
            permanent = error.kind in {"quota", "authentication", "configuration"}
            if (
                permanent
                or error.kind == "rate_limit"
                or self.failures >= self.threshold
            ):
                self.blocked_until = now + max(
                    self.cooldown, error.retry_after, 300.0 if permanent else 0.0
                )
                if error.kind == "configuration":
                    self.blocked_until = float("inf")
            if release:
                self.inflight = False


_CIRCUITS: OrderedDict[str, RerankCircuit] = OrderedDict()
_CIRCUITS_LOCK = Lock()
_RERANK_CAPACITY = BoundedSemaphore(4)


def _request_identity(api: ExternalRerankApi, config: PagedMemoryConfig) -> str:
    # A disabled configuration must not outlive a correction to the request
    # shape (including its size), or a new version of the ranking prompt.
    return hashlib.sha256(json.dumps([
        api.url, api.runtime["model"], api._api_key, api.extra_params, api.headers,
        RERANK_PROMPT, config.rerank_candidate_limit, config.rerank_input_token_budget,
    ], sort_keys=True).encode()).hexdigest()


def _circuit(api: ExternalRerankApi, config: PagedMemoryConfig) -> RerankCircuit:
    identity = json.dumps(
        [
            _request_identity(api, config),
            config.rerank_failure_threshold,
            config.rerank_cooldown_seconds,
        ], sort_keys=True
    )
    key = hashlib.sha256(identity.encode()).hexdigest()
    with _CIRCUITS_LOCK:
        if key not in _CIRCUITS:
            if len(_CIRCUITS) >= 256:
                expired = next(
                    (
                        key
                        for key, value in _CIRCUITS.items()
                        if not value.inflight
                        and value.blocked_until <= time.monotonic()
                    ),
                    None,
                )
                if expired is None:
                    raise RerankFailure("busy")
                del _CIRCUITS[expired]
            _CIRCUITS[key] = RerankCircuit(
                config.rerank_failure_threshold, config.rerank_cooldown_seconds
            )
        return _CIRCUITS[key]


def apply_grades(
    hits: Sequence[MemoryHit], scored: Sequence[MemoryHit], grades: Sequence[int]
) -> list[MemoryHit]:
    grade_by_id = {
        hit.record.id: grade for hit, grade in zip(scored, grades, strict=True)
    }
    scale = max((abs(hit.score) for hit in hits), default=1.0) or 1.0
    ranked = [
        replace(
            hit,
            score=grade_by_id[hit.record.id] + 0.01 * hit.score / scale,
            rerank_score=float(grade_by_id[hit.record.id]),
            reason=hit.reason + "+api-rerank",
        )
        for hit in hits
        if hit.record.id in grade_by_id
    ]
    ranked.sort(key=lambda hit: hit.score, reverse=True)
    # Retain unscored cards for required-facet coverage. The selector excludes
    # them from ordinary supplements after a successful model evaluation.
    return ranked + [hit for hit in hits if hit.record.id not in grade_by_id]


class OptionalMemoryReranker:
    def __init__(
        self,
        config: PagedMemoryConfig,
        runtime: Mapping | None = None,
        *,
        authority=None,
    ) -> None:
        self.config = config
        self.api = None
        self.circuit = None
        self.setup_error = ""
        self.coordinator = (
            SharedRerankCoordinator(authority)
            if config.rerank_shared_coordination
            and authority is not None
            and hasattr(authority, "_transaction")
            else None
        )
        self.coordination_key = ""
        self._lane = RequestLane(capacity=_RERANK_CAPACITY, name="paged-memory-rerank")
        self._cache: OrderedDict[str, tuple[float, list[int]]] = OrderedDict()
        # Grades are independent relevance judgments, not relative ranks. Reuse
        # unchanged cards even when fresh retrieval reorders/extends the set.
        # This memo is local to this runtime; the shared exact-batch cache stays
        # unchanged. Keys still bind tenant/task, question, route and sources.
        self._grade_cache: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self.tokens = TokenBudgetManager(model_name=config.rerank_model)
        if config.rerank_enabled:
            try:
                self.api = ExternalRerankApi(config, runtime)
                self.circuit = _circuit(self.api, config)
                self.coordination_key = _request_identity(self.api, config)
            except Exception as exc:  # noqa: BLE001 - optional configuration cannot block authority access
                self.setup_error = (
                    exc.kind if isinstance(exc, RerankFailure) else "configuration"
                )
        self.reset_observation()

    def reset_observation(self) -> None:
        self.observation = {
            "status": "not_requested" if self.config.rerank_enabled else "disabled",
            "applied": False,
            "error": "",
            "latency_ms": 0.0,
            "candidate_count": 0,
            "input_tokens_estimated": 0,
            "model_calls": 0,
            "attempted": False,
            "tokens": 0,
            "cost": None,
            "reused_candidate_count": 0,
            "api_candidate_count": 0,
        }

    def close(self) -> None:
        self._lane.close()

    def _prepare(
        self, query: str, hits: Sequence[MemoryHit], *, required_facets=()
    ) -> tuple[str, list[MemoryHit], int]:
        payload = {
            "question": query if len(query) <= 400 else retrieval_query(query),
            "cards": [],
        }
        selected = []
        size = 0
        for hit in balanced_candidates(hits, required_facets):
            if len(selected) >= self.config.rerank_candidate_limit:
                break
            card = {
                "index": len(selected),
                "type": hit.record.memory_type.value,
                "title": hit.record.title,
                "text": hit.record.preview_text,
            }
            payload["cards"].append(card)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            estimated = self.tokens.estimate_text(RERANK_PROMPT + encoded) + 32
            if estimated > self.config.rerank_input_token_budget:
                payload["cards"].pop()
                continue
            selected.append(hit)
            size = estimated
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            selected,
            size,
        )

    def rerank(
        self,
        query: str,
        hits: Sequence[MemoryHit],
        *,
        tenant_id: str,
        task_id: str,
        required_facets=(),
        deadline_monotonic=None,
        force=False,
    ) -> list[MemoryHit]:
        self.reset_observation()
        baseline = list(hits)
        if not self.config.rerank_enabled or not baseline:
            return baseline
        if (
            deadline_monotonic is not None
            and deadline_monotonic - time.monotonic() <= 0.3
        ):
            self.observation.update(status="fallback", error="retrieval_deadline")
            return baseline
        should_rerank, route = rerank_route(
            query,
            baseline,
            required_facets=required_facets,
            policy="always" if force else self.config.rerank_policy,
        )
        self.observation["route"] = route
        if not should_rerank:
            self.observation.update(status="skipped", reason=route)
            return baseline
        if self.api is None or self.circuit is None:
            self.observation.update(
                status="fallback", error=self.setup_error or "configuration"
            )
            return baseline
        if current_usage_observation().get("budget_pressure", 0) >= 0.85:
            self.observation.update(status="fallback", error="budget_pressure")
            return baseline
        payload, scored, size = self._prepare(
            query, baseline, required_facets=required_facets
        )
        self.observation.update(
            candidate_count=len(scored), input_tokens_estimated=size
        )
        if not scored:
            self.observation.update(status="fallback", error="input_budget")
            return baseline
        key = self.cache_key(payload, scored, tenant_id=tenant_id, task_id=task_id)
        now = time.monotonic()
        cached = self._cache.get(key)
        if (cached is None or cached[0] <= now) and self.coordinator is not None:
            try:
                shared = self.coordinator.get_cached(
                    key,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    deadline=deadline_monotonic,
                    include_ttl=True,
                )
                if shared is not None:
                    shared, ttl = shared
                    if len(shared) != len(scored) or any(
                        type(g) is not int or not 0 <= g <= 3 for g in shared
                    ):
                        raise ValueError("invalid shared grades")
                    cached = (now + ttl, shared)
                    self.observation["cache_scope"] = "database"
            except Exception:  # noqa: BLE001 - optional shared storage must not block the answer
                self.observation.update(
                    status="fallback", error="coordination_unavailable"
                )
                return baseline
        if cached is not None and cached[0] > now:
            self._cache[key] = cached
            self._cache.move_to_end(key)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
            self.observation["status"] = "cached"
            grades = cached[1]
        else:
            question = json.loads(payload)["question"]
            known = self._known_grades(question, scored, tenant_id=tenant_id, task_id=task_id)
            self.observation["reused_candidate_count"] = len(known)
            if len(known) == len(scored):
                self.observation.update(status="cached", cache_scope="runtime_card")
                return self._rank(baseline, scored, [known[h.record.id] for h in scored])
            execution_calls = execution_memory_rerank_calls()
            if execution_calls is not None and execution_calls >= 1:
                # Native execution workers and read-only primaries receive an
                # explicit contract. Keep fresh local retrieval and all
                # required-facet selection, but do not pay to rerank optional
                # memories on every tool turn. Exact cached grades above remain
                # usable. Independent verifiers keep their existing policy.
                self.observation.update(status="local", reason="execution_external_rerank_limit",
                    native_attempt_calls=execution_calls)
                return baseline
            pending = [hit for hit in scored if hit.record.id not in known]
            request_payload = self._card_payload(question, pending)
            self.observation["api_candidate_count"] = len(pending)
            self.observation["input_tokens_estimated"] = self.tokens.estimate_text(RERANK_PROMPT + request_payload) + 32
            ranked = self._call_and_rank(
                query,
                baseline,
                request_payload,
                pending,
                self.cache_key(request_payload, pending, tenant_id=tenant_id, task_id=task_id),
                tenant_id=tenant_id,
                task_id=task_id,
                deadline_monotonic=deadline_monotonic,
            )
            if known and self.observation["applied"]:
                grades_by_id = {**known, **{h.record.id: int(h.rerank_score) for h in ranked
                    if h.record.id in {p.record.id for p in pending} and h.rerank_score is not None}}
                return self._rank(baseline, scored, [grades_by_id[h.record.id] for h in scored])
            return ranked
        self.observation["reused_candidate_count"] = len(scored)
        self._remember_grades(json.loads(payload)["question"], scored, grades,
            tenant_id=tenant_id, task_id=task_id, expires=cached[0])
        return self._rank(baseline, scored, grades)

    @staticmethod
    def _card_payload(question, hits):
        return json.dumps({"question": question, "cards": [
            {"index": index, "type": hit.record.memory_type.value, "title": hit.record.title,
             "text": hit.record.preview_text} for index, hit in enumerate(hits)
        ]}, ensure_ascii=False, separators=(",", ":"))

    def _known_grades(self, question, scored, *, tenant_id, task_id):
        known, now = {}, time.monotonic()
        for hit in scored:
            key = self.cache_key(self._card_payload(question, [hit]), [hit], tenant_id=tenant_id, task_id=task_id)
            cached = self._grade_cache.get(key)
            if cached is not None:
                if cached[0] > now:
                    known[hit.record.id] = cached[1]
                    self._grade_cache.move_to_end(key)
                else:
                    del self._grade_cache[key]
        return known

    def _remember_grades(self, question, scored, grades, *, tenant_id, task_id, expires):
        if len(scored) != len(grades) or any(type(g) is not int or not 0 <= g <= 3 for g in grades):
            raise ValueError("invalid relevance grades")
        for hit, grade in zip(scored, grades, strict=True):
            key = self.cache_key(self._card_payload(question, [hit]), [hit], tenant_id=tenant_id, task_id=task_id)
            self._grade_cache[key] = (expires, grade)
            self._grade_cache.move_to_end(key)
        while len(self._grade_cache) > 512:
            self._grade_cache.popitem(last=False)

    def cache_key(self, payload, scored, *, tenant_id, task_id):
        identity = json.dumps(
            [
                tenant_id,
                task_id,
                self.coordination_key,
                RERANK_PROMPT,
                payload,
                [
                    (
                        h.record.id,
                        h.record.content_hash,
                        h.record.source_event_id,
                        h.record.artifact_id,
                        h.record.source_range.unit,
                        h.record.source_range.start,
                        h.record.source_range.end,
                        h.record.facets,
                    )
                    for h in scored
                ],
            ]
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    def _call_and_rank(
        self,
        query,
        baseline,
        payload,
        scored,
        key,
        *,
        tenant_id,
        task_id,
        deadline_monotonic,
    ):
        if self.circuit is None:
            return baseline
        now = time.monotonic()
        if self.circuit is not None:
            reason = self.circuit.acquire(now)
            if reason:
                self.observation.update(status="fallback", error=reason)
                return baseline
            started = time.monotonic()
            deadline = min(
                started + self.config.rerank_timeout_seconds,
                deadline_monotonic - 0.3
                if deadline_monotonic is not None
                else float("inf"),
            )
            context = (
                copy_context()
            )  # Preserve the run's real usage ledger in the worker.
            future = None
            lease = ""
            try:
                if self.coordinator is not None:
                    try:
                        lease, reason = self.coordinator.acquire(
                            self.coordination_key,
                            lease_seconds=max(1, deadline - time.monotonic()) + 5,
                            deadline=deadline,
                        )
                    except Exception as exc:
                        raise RerankFailure("coordination_unavailable") from exc
                    if reason:
                        raise RerankFailure(reason)
                if time.monotonic() >= deadline:
                    raise RerankFailure("retrieval_deadline")
                future = self._lane.submit(
                    lambda: context.run(
                        self.api.score, payload, len(scored), deadline=deadline
                    )
                )
                if future is None:
                    raise RerankFailure("busy")
                self.observation["attempted"] = True
                grades, usage = future.result(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            except FutureTimeout:
                self._finish_shared(
                    lease,
                    RerankFailure("timeout"),
                    release=False,
                    deadline=deadline_monotonic,
                )
                self.circuit.failure(
                    RerankFailure("timeout"), time.monotonic(), release=False
                )
                future.add_done_callback(lambda _done: self.circuit.release())
                self.observation.update(
                    status="fallback",
                    error="timeout",
                    model_calls=None,
                    tokens=None,
                    usage_pending=True,
                )
                return baseline
            except Exception as exc:  # noqa: BLE001 - SDK, budget and parsing failures are optional
                error = (
                    exc
                    if isinstance(exc, RerankFailure)
                    else RerankFailure("provider_unavailable")
                )
                no_provider_failure = error.kind in {
                    "configuration_disabled",
                    "busy",
                    "budget_unavailable",
                    "circuit_open",
                    "coordination_unavailable",
                    "retrieval_deadline",
                }
                self._finish_shared(
                    lease,
                    None if no_provider_failure else error,
                    cancel=no_provider_failure,
                    deadline=deadline_monotonic,
                )
                if no_provider_failure:
                    self.circuit.release()
                else:
                    self.circuit.failure(error, time.monotonic())
                self.observation.update(status="fallback", error=error.kind)
                if error.usage is not None:
                    self.observation.update(error.usage)
                return baseline
            finally:
                self.observation["latency_ms"] = (time.monotonic() - started) * 1000
            self.circuit.success()
            self._finish_shared(lease, deadline=deadline_monotonic)
            self.observation.update(status="succeeded", **usage)
            self._cache[key] = (
                time.monotonic() + self.config.rerank_cache_ttl_seconds,
                grades,
            )
            self._remember_grades(json.loads(payload)["question"], scored, grades,
                tenant_id=tenant_id, task_id=task_id, expires=self._cache[key][0])
            self._cache.move_to_end(key)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
            if self.coordinator is not None:
                try:
                    self.coordinator.put_cached(
                        key,
                        grades,
                        tenant_id=tenant_id,
                        task_id=task_id,
                        ttl=self.config.rerank_cache_ttl_seconds,
                        deadline=deadline_monotonic,
                    )
                except Exception:  # noqa: BLE001 - a cache write cannot invalidate a valid score
                    self.observation["cache_write_error"] = "coordination_unavailable"
        return self._rank(baseline, scored, grades)

    def _rank(self, baseline, scored, grades):
        ranked = apply_grades(baseline, scored, grades)
        self.observation["order_changed"] = [h.record.id for h in ranked] != [
            h.record.id for h in baseline
        ]
        if self.config.rerank_shadow:
            self.observation["status"] = "shadow"
            return baseline
        self.observation["applied"] = True
        return ranked

    def _finish_shared(
        self, lease, error=None, *, release=True, cancel=False, deadline=None
    ):
        if self.coordinator is None or not lease:
            return
        try:
            self.coordinator.finish(
                self.coordination_key,
                lease,
                error=error,
                threshold=self.config.rerank_failure_threshold,
                cooldown=self.config.rerank_cooldown_seconds,
                release=release,
                cancel=cancel,
                deadline=deadline,
            )
        except Exception:  # noqa: BLE001 - an abandoned lease is recovered by its expiry
            # The fenced lease still expires if this worker/database disappears.
            self.observation["coordination_error"] = "coordination_unavailable"
