"""Bounded optional learning from existing history, charged to its owning run."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from minicode.memory import MemoryManager, MemoryScope, _memory_write_lock
from minicode.memory_learning import (
    LearningPolicy,
    evidence_version,
    insight_id,
    protected,
)
from minicode.model_registry import build_provider_config
from minicode.path_safety import is_unsafe_stored_path
from minicode.provider_usage import ProviderCall, bind_usage

logger = logging.getLogger(__name__)


def claim_learning_attempt(manager, evidence, *, now=None):
    """One project attempt per hour, one evidence/confirmed-knowledge set per day.

    Claim before networking so overlapping chats, retries and process restarts
    cannot multiply calls. An unreadable state is not permission to reset it.
    """
    now = time.time() if now is None else now
    policy = LearningPolicy(manager)
    confirmed = sorted((e.id, evidence_version(e)) for e in policy.entries.values() if protected(e))
    key = hashlib.sha256(json.dumps([insight_id(evidence), confirmed]).encode()).hexdigest()
    root = manager.paths.local_memory
    if is_unsafe_stored_path(root, boundary=root.parent):
        return False
    manager._ensure_scope_path(MemoryScope.LOCAL)
    path = root / "learning-state.json"
    if is_unsafe_stored_path(root / ".learning.lock", boundary=root.parent, reject_hardlinks=True):
        return False
    with _memory_write_lock(root / ".learning.lock"):
        if is_unsafe_stored_path(path, boundary=root.parent, reject_hardlinks=True):
            return False
        if path.exists() and path.stat().st_size > 65536:
            return False
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict):
            return False
        attempts = state.get("attempts", {})
        if not isinstance(attempts, dict):
            return False
        if now < float(state.get("next_attempt_after", 0)) or now < float(attempts.get(key, 0)):
            return False
        attempts = {k: v for k, v in attempts.items() if isinstance(v, (int, float)) and v > now}
        attempts = dict(sorted(attempts.items(), key=lambda item: item[1], reverse=True)[:127])
        attempts[key] = now + 86400
        manager._atomic_write(path, json.dumps({"next_attempt_after": now + 3600, "attempts": attempts}))
        return True


class LearningModel:
    """One non-streaming, tool-free request; no retry and no synthetic fallback."""

    def __init__(self, runtime):
        from minicode.openai_adapter import (
            DEFAULT_OPENAI_USER_AGENT,
            _get_openai_chat_completions_url,
        )

        provider = build_provider_config(str(runtime.get("model") or ""), runtime)
        parsed = urlsplit(provider.base_url)
        if not provider.api_key or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("learning provider configuration unavailable")
        self.runtime = {**runtime, "model": provider.model, "provider": provider.provider.value,
                        "openaiBaseUrl": provider.base_url}
        self.protocol = "openai" if provider.is_openai_compatible else "anthropic"
        self.headers = {"User-Agent": DEFAULT_OPENAI_USER_AGENT, **provider.extra_headers, "Content-Type": "application/json"}
        self.extra = dict(provider.extra_params)
        if self.protocol == "openai":
            if provider.chat_completions_path:
                self.runtime["_openai_chat_completions_path"] = provider.chat_completions_path
            self.url = _get_openai_chat_completions_url(self.runtime)
            self.headers["Authorization"] = "Bearer " + provider.api_key
            if provider.provider.value == "deepseek":
                self.extra["thinking"] = {"type": "disabled"}
                self.extra.pop("reasoning_effort", None)
        else:
            from minicode.anthropic_adapter import _messages_endpoint
            self.url = _messages_endpoint(provider.base_url)
            self.headers["anthropic-version"] = "2023-06-01"
            token = self.extra.pop("auth_token", "")
            self.headers["Authorization" if token else "x-api-key"] = ("Bearer " + token) if token else provider.api_key

    def generate(self, prompt):
        # Full evidence and corrections must fit; never truncate their exceptions.
        if len(prompt) > 12000:
            return ""
        body = {**self.extra, "model": self.runtime["model"], "stream": False,
                "messages": [{"role": "user", "content": prompt}], "max_tokens": 1200}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=self.headers, method="POST")
        deadline = time.monotonic() + 15
        with ProviderCall(self.runtime, self.protocol, purpose="memory_consolidate") as call:
            try:
                response = call.open(request, timeout=15)
            except urllib.error.HTTPError as exc:
                # These statuses reject before generation. Unknown server errors
                # keep their real unresolved reservation for normal reconciliation.
                if exc.code in {401, 403, 404, 405, 422} and not call.raw_usage:
                    call.observe({"input_tokens": 0, "output_tokens": 0} if self.protocol == "anthropic"
                                 else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, complete=True)
                exc.close()
                raise
            chunks, size = [], 0
            while True:
                call.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError("memory learning deadline")
                chunk = response.read1(16384)
                if not chunk:
                    break
                size += len(chunk)
                if size > 131072:
                    raise ValueError("memory learning response too large")
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
            call.observe(data.get("usage"), complete=True)
            if self.protocol == "openai":
                choice = data["choices"][0]
                return choice["message"]["content"] if choice.get("finish_reason") == "stop" else ""
            return "".join(part.get("text", "") for part in data["content"] if part.get("type") == "text") if data.get("stop_reason") == "end_turn" else ""


def learn_from_history(manager, *, runtime, budget, invocation, outcome):
    """Learn only after an accepted project delivery has published its history.

    Bind the auxiliary call to the actual primary attempt for accounting. Native
    request projection is intentionally absent from this separate model call.
    """
    from minicode.memory_curator_agent import MemoryCuratorAgent
    from minicode.paged_memory.config import _bool
    from minicode.runtime_memory import accepted_project_changes
    from minicode.runtime_protocol import ToolEffect

    if (invocation is None or not accepted_project_changes(outcome)
            or (outcome.memory_receipt or {}).get("status") not in {"stored", "already_recorded"}
            or invocation.run_id != outcome.contract.envelope.run_id
            or invocation.attempt_id != outcome.runtime_snapshot["nodes"]["primary"]["result"].get("attempt_id")):
        return 0
    if _bool(os.environ.get("MEMORY_SHADOW_WRITE") or runtime.get("memoryShadowWrite"), False):
        return 0
    if (not isinstance(manager, MemoryManager) or budget is None
            or runtime.get("runtimeOrchestration") != "primary" or invocation.role != "primary"
            or ToolEffect.WORKSPACE_WRITE not in invocation.allowed_effects
            or budget.cancellation.is_set() or budget._provider_balance_failure is not None):
        return 0
    policy = runtime.get("_modelCallPolicy")
    if policy is not None and "memory_consolidate" not in policy:
        return 0
    total = budget.reducer.usage_summary()
    local = budget.reducer.usage_summary(invocation.attempt_id)
    for cap, used in ((budget.budget, total), (invocation.budget, local)):
        if (cap.deadline is not None and cap.deadline - time.time() < 30
            or cap.max_tokens is not None and cap.max_tokens - used["tokens"] - used["reserved_tokens"] < 24000
            or cap.max_cost is not None and used["known_cost"] + used["reserved_cost"] >= cap.max_cost * 0.5):
            return 0
    calls = budget.reducer.snapshot()["usage_ledger"]["calls"].values()
    if any((c.get("usage") or {}).get("prepared_request", {}).get("call_identity", {}).get("purpose") == "memory_consolidate" for c in calls):
        return 0
    try:
        model = LearningModel(runtime)
        curator = MemoryCuratorAgent(manager, model_adapter=model, max_insights_per_cycle=1,
            max_attempts_per_cycle=1, attempt_gate=lambda evidence: claim_learning_attempt(manager, evidence))
        with bind_usage(budget, invocation, post_delivery_learning=True):
            return curator._consolidate_insights()
    except Exception as error:  # noqa: BLE001 - keep the real ledger; no fabricated memory on optional failure
        logger.info("Automatic learning unavailable (%s)", type(error).__name__)
        return 0
