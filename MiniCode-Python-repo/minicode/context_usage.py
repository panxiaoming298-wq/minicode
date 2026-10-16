"""Content-free snapshots of the main Agent's latest HTTP input.

Display estimates are separate from admission reservations and billed totals.
Only native memory sections can move text out of the system category; headings
in user text never confer provenance. Snapshots never store prompt/tool content.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import logging
import math
from pathlib import Path
import time
from typing import Any

from minicode.context_manager import compute_context_stats, estimate_tokens, resolve_context_limit
from minicode.path_safety import is_unsafe_stored_path
from minicode.session_contract import _valid_session_id, atomic_write_json
from minicode.store_lock import store_lock

SCHEMA = "minicode-context-usage-v1"
CATEGORIES = ("system", "tools", "conversation", "memory", "other")
_MEDIA = {"image", "image_url", "input_image", "input_audio", "audio", "file", "document"}


def _number(value: Any) -> bool:
    return type(value) is int and value >= 0


def _text_parts(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str):
                yield item["text"]


def _memory_parts(text: str, sections: Sequence[str]) -> list[str]:
    # Longest native spans win. A character is attributed once even when a
    # memory packet contains an older memory section or the same span repeats.
    remaining = [text]
    found = []
    for section in sorted(set(sections), key=len, reverse=True):
        if not section:
            continue
        next_remaining = []
        for part in remaining:
            pieces = part.split(section)
            found.extend([section] * (len(pieces) - 1))
            next_remaining.extend(pieces)
        remaining = next_remaining
    return found


def estimate_request(body: Mapping[str, Any], *, memory_sections: Sequence[str] = (),
                     provider: str = "", endpoint: str = "") -> dict[str, Any]:
    """Partition the final input; never include output caps or byte reservations."""
    count = estimate_tokens
    method = "text_heuristic"
    # Compatible routes may use a known model ID/alias, but their framing and
    # tokenizer identity are not verified. Label that count as a reference.
    # Even the official route can add hidden chat framing.
    from minicode.model_registry import MODEL_ID_ALIASES
    from minicode.runtime_budget import official_deepseek_flash
    model = str(body.get("model", ""))
    if provider == "deepseek" and MODEL_ID_ALIASES.get(model, model) == "deepseek-flash":
        try:
            from minicode.token_estimation import _tokenizer
            tokenizer, special = _tokenizer()
            def tokenize(text):
                # Literal special-token text must not collapse to one added
                # token, or make the rest of a large message use chars / 4.
                total, start = 0, 0
                for match in special.finditer(text):
                    total += len(tokenizer.encode(text[start:match.start()], add_special_tokens=False).ids)
                    total += len(match.group().encode("utf-8"))
                    start = match.end()
                return total + len(tokenizer.encode(text[start:], add_special_tokens=False).ids)
            count = tokenize
            method = "local_tokenizer" if official_deepseek_flash(model, endpoint) else "reference_tokenizer"
        except (ImportError, OSError, ValueError):
            pass
    has_media = False
    image_tokens, image_count = 0, 0
    try:
        from minicode.image_input import image_reservation
        body, image_tokens, image_count = image_reservation(dict(body), endpoint)
    except ValueError:
        pass  # Unknown external media remains explicitly unmeasured.
    def text_only(value):
        nonlocal has_media
        if isinstance(value, Mapping):
            if isinstance(value.get("type"), str) and value["type"] in _MEDIA:
                has_media = True
                return {"type": value["type"]}
            return {key: text_only(item) for key, item in value.items()}
        if isinstance(value, list):
            return [text_only(item) for item in value]
        return value
    def cost(value):
        if value in (None, "", [], {}):
            return 0
        return count(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    totals = dict.fromkeys(CATEGORIES, 0)
    def message(value, category, content):
        cleaned = text_only(value)
        amount = cost(cleaned)
        memory = min(amount, sum(count(part) for text in _text_parts(content)
                                 for part in _memory_parts(text, memory_sections)))
        totals[category] += amount - memory
        totals["memory"] += memory
    if body.get("system"):
        message(body["system"], "system", body["system"])
    for item in body.get("messages", []):
        if isinstance(item, Mapping):
            category = "system" if item.get("role") in {"system", "developer"} else "conversation"
            message(item, category, item.get("content"))
    totals["tools"] = cost(body.get("tools"))
    # Output-format schemas also consume input, unlike generation controls.
    totals["other"] = cost(body.get("response_format"))
    totals["conversation"] += image_tokens
    return {"estimated_tokens": sum(totals.values()), "method": method,
            "image_count": image_count, "image_token_reservation": image_tokens,
            "has_unmeasured_media": has_media,
            "breakdown": [{"key": key, "estimated_tokens": value} for key, value in totals.items()
                          if key != "other" or value]}


def _capacity(model: str, runtime: Mapping[str, Any], output: Any = None, *,
              planning: bool = False, output_policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from minicode.paged_memory.config import PagedMemoryConfig
    from minicode.output_budget import context_capacity, planning_capacity, select_output_budget
    memory = PagedMemoryConfig.from_sources(runtime)
    window = resolve_context_limit(model, dict(runtime))
    policy = output_policy
    if policy is None and (not _number(output) or planning):
        endpoint = str(runtime.get("openaiBaseUrl") or runtime.get("baseUrl") or "")
        body = {"model": model, **(runtime.get("_provider_params") or {})}
        policy_runtime = runtime
        protocol = "anthropic" if runtime.get("provider") == "anthropic" else "openai"
        if runtime.get("provider"):
            # Settings contain defaults for several providers simultaneously.
            # Resolve the same adapter as dispatch instead of taking the first URL
            # (e.g. openaiBaseUrl while the selected provider is DeepSeek).
            from minicode.model_registry import create_model_adapter
            from minicode.tooling import ToolRegistry
            adapter = create_model_adapter(model, ToolRegistry([]), dict(runtime))
            # Conversation controls also add _provider_params to raw settings;
            # their presence does not prove the provider route is normalized.
            # Keep effective request overrides when resolving that route again.
            parameters = runtime.get("_provider_params")
            if isinstance(parameters, Mapping) and isinstance(getattr(adapter, "runtime", None), dict):
                adapter.runtime["_provider_params"] = {
                    **(adapter.runtime.get("_provider_params") or {}), **parameters,
                }
            if callable(getattr(adapter, "context_request", None)):
                body, endpoint, protocol = adapter.context_request([])
                policy_runtime = adapter.runtime
        if endpoint:
            if policy_runtime is runtime and "reasoning_effort" not in body and runtime.get("reasoningEffort"):
                body["reasoning_effort"] = runtime["reasoningEffort"]
            policy = select_output_budget(policy_runtime, body, endpoint=endpoint, protocol=protocol)
            if not _number(output):
                output = policy["requested_tokens"]
    if planning:
        if policy is None:
            policy = {"mode": "manual" if _number(output) and output > 0 else "auto",
                      "requested_tokens": output if _number(output) else 0}
        return planning_capacity(context_window=window, output_policy=policy, config=memory)
    return context_capacity(context_window=window, output_tokens=output if _number(output) else 0,
        output_floor=memory.output_token_reserve, safety_tokens=memory.safety_token_reserve)


def _capacity_error(value: Mapping[str, Any]) -> str:
    if value.get("capacity_error"):
        return str(value["capacity_error"])
    # Older snapshots have already lost the actual reserve due to max(1,...).
    # Do not reconstruct a supposedly exact historical capacity from today's
    # settings, or continue displaying a fabricated percentage.
    if value.get("effective_input") == 1 and "reserved_tokens" not in value:
        return "legacy_capacity_unavailable"
    return ""


def stats_from_history(messages: list[dict[str, Any]], model: str, runtime: Mapping[str, Any]) -> dict[str, Any]:
    stats = compute_context_stats(messages, model, runtime=dict(runtime))
    capacity = _capacity(model, runtime, runtime.get("maxOutputTokens"), planning=True)
    policy = capacity["budget_policy"]
    ratio = min(1.0, stats["total_tokens"] / capacity["context_window"]) if capacity["context_window"] else 0.0
    return {**stats, **capacity, "utilization": ratio,
            "budget_policy": policy,
            "warning_level": _warning_level(stats["total_tokens"], policy, capacity["capacity_error"])}


def _warning_level(tokens, policy, error=""):
    return ("configuration" if error else "blocked" if tokens >= policy["protect_at_tokens"] else
            "critical" if tokens >= policy["compact_at_tokens"] else
            "warning" if tokens >= policy["note_at_tokens"] else "normal")


def capture_request(body: Mapping[str, Any], *, runtime: Mapping[str, Any], request_id: str,
                    node_id: str, memory_sections: Sequence[str] = (), endpoint: str = "",
                    captured_at: float | None = None, admission_input_tokens: int | None = None,
                    output_policy: Mapping[str, Any] | None = None,
                    context_input: Mapping[str, Any] | None = None) -> dict[str, Any]:
    model = str(body.get("model") or runtime.get("model") or "default")
    capacity = _capacity(model, runtime, body.get("max_completion_tokens", body.get("max_tokens")))
    measured = context_input or {}
    return {"schema": SCHEMA, "request_id": request_id, "node_id": node_id,
            "run_id": str(runtime.get("runtimeRunId") or ""), "model": model,
            "captured_at": captured_at if captured_at is not None else time.time(),
            **capacity,
            "budget_policy": _capacity(model, runtime, runtime.get("maxOutputTokens"),
                planning=True, output_policy=output_policy)["budget_policy"],
            "provider_input_tokens": None,
            "admission_input_tokens": admission_input_tokens,
            "context_input_tokens": measured.get("estimated_input_tokens"),
            "context_guard_tokens": measured.get("guard_input_tokens"),
            "context_measurement_method": measured.get("method"),
            **estimate_request(body, memory_sections=memory_sections,
                               provider=str(runtime.get("provider") or ""), endpoint=endpoint)}


def validate_snapshot(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("node_id") not in {"root", "primary"}:
        return False
    if not all(isinstance(value.get(key), str) and value[key] for key in ("request_id", "model")):
        return False
    stamp = value.get("captured_at")
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        return False
    if not all(_number(value.get(key)) for key in ("context_window", "effective_input", "estimated_tokens")):
        return False
    effective, window = value["effective_input"], value["context_window"]
    if not 0 <= effective <= window or window <= 0:
        return False
    if "reserved_tokens" in value:
        reserve = value["reserved_tokens"]
        if (not _number(reserve) or effective != max(0, window - reserve)
            or value.get("capacity_error") != ("output_reserve_exceeds_window" if reserve >= window else "")):
            return False
    elif effective == 0 or value.get("capacity_error"):
        return False
    split_keys = ("output_reserve_tokens", "safety_reserve_tokens")
    if any(key in value for key in split_keys):
        if not all(_number(value.get(key)) for key in split_keys):
            return False
        if sum(value[key] for key in split_keys) != value.get("reserved_tokens"):
            return False
    policy = value.get("budget_policy")
    if policy is not None:
        keys = ("output_limit", "input_limit", "pressure_window", "note_at_tokens",
                "compact_at_tokens", "protect_at_tokens")
        if (not isinstance(policy, dict) or policy.get("version") != "total-window-v1"
            or policy.get("output_mode") not in {"auto", "manual"}
            or not all(_number(policy.get(key)) for key in keys)):
            return False
        if not (0 <= policy["note_at_tokens"] <= policy["compact_at_tokens"]
                <= policy["protect_at_tokens"] <= policy["input_limit"] <= window
                and policy["protect_at_tokens"] <= policy["pressure_window"] <= window):
            return False
    parts = value.get("breakdown")
    if not isinstance(parts, list) or not 4 <= len(parts) <= 5:
        return False
    if any(not isinstance(part, dict) or part.get("key") not in CATEGORIES
           or not _number(part.get("estimated_tokens")) for part in parts):
        return False
    if {part["key"] for part in parts} != set(CATEGORIES[:4]) | ({"other"} if len(parts) == 5 else set()):
        return False
    return (sum(part["estimated_tokens"] for part in parts) == value["estimated_tokens"]
            and all(value.get(key) is None or _number(value[key]) for key in
                    ("context_input_tokens", "context_guard_tokens"))
            and (value.get("admission_input_tokens") is None or _number(value["admission_input_tokens"]))
            and (value.get("provider_input_tokens") is None or _number(value["provider_input_tokens"])))


def _snapshot_path(session_id: str, sessions_dir: Path | None) -> tuple[Path, Path]:
    if not _valid_session_id(session_id):
        raise ValueError("invalid context snapshot session")
    if sessions_dir is None:
        from minicode.session import SESSIONS_DIR
        sessions_dir = SESSIONS_DIR
    root = Path(sessions_dir)
    path = root / session_id / "context-usage.json"
    if is_unsafe_stored_path(path, boundary=root, reject_hardlinks=True):
        raise OSError("unsafe context snapshot path")
    return root, path


def load_snapshot(session_id: str, sessions_dir: Path | None = None) -> dict[str, Any] | None:
    try:
        _, path = _snapshot_path(session_id, sessions_dir)
        if not path.is_file() or path.stat().st_size > 16_384:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if validate_snapshot(value) and value.get("session_id") == session_id else None
    except (OSError, ValueError, TypeError):
        return None


def save_snapshot(session_id: str, snapshot: Mapping[str, Any], sessions_dir: Path | None = None) -> None:
    if not validate_snapshot(snapshot):
        raise ValueError("invalid context snapshot")
    root, path = _snapshot_path(session_id, sessions_dir)
    with store_lock(root / ".store.lock", timeout=0.2):
        previous = load_snapshot(session_id, root)
        # A late provider response must not replace a newer request's estimate.
        if previous and previous["request_id"] != snapshot["request_id"] and previous["captured_at"] >= snapshot["captured_at"]:
            return
        atomic_write_json(path, {**snapshot, "session_id": session_id})


def stats_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    exact = snapshot.get("provider_input_tokens") is not None
    measured = snapshot.get("context_input_tokens")
    total = snapshot["provider_input_tokens"] if exact else measured if _number(measured) else snapshot["estimated_tokens"]
    admission = snapshot.get("admission_input_tokens")
    capacity_error = _capacity_error(snapshot)
    policy = snapshot.get("budget_policy")
    denominator = snapshot["context_window"] if policy else snapshot["effective_input"]
    ratio = min(1.0, total / denominator) if denominator and not capacity_error else 0.0
    return {"total_tokens": total, "estimated_tokens": snapshot["estimated_tokens"],
            "provider_usage_tokens": snapshot.get("provider_input_tokens") or 0,
            "context_window": snapshot["context_window"], "effective_input": snapshot["effective_input"],
            "reserved_tokens": snapshot.get("reserved_tokens"), "capacity_error": capacity_error,
            "output_reserve_tokens": snapshot.get("output_reserve_tokens"),
            "safety_reserve_tokens": snapshot.get("safety_reserve_tokens"),
            "budget_policy": policy,
            "admission_input_tokens": admission if _number(admission) else None,
            "context_guard_tokens": snapshot.get("context_guard_tokens"),
            "utilization": ratio, "warning_level": _warning_level(total, policy, capacity_error) if policy
                else "configuration" if capacity_error else "blocked" if ratio >= .9 else "critical" if ratio >= .85 else "warning" if ratio >= .8 else "normal",
            "accounting": {"source": "last_request_provider" if exact else "last_request_estimate",
                           "is_exact": exact, "stale": False, "reason": ""},
            "request": {**{key: snapshot.get(key) for key in ("request_id", "node_id", "model", "captured_at", "has_unmeasured_media")},
                        "method": snapshot.get("context_measurement_method") or snapshot.get("method")},
            "breakdown": snapshot["breakdown"]}


def record_for_call(call: Any, body: Mapping[str, Any], endpoint: str, *,
                    sessions_dir: Path | None = None) -> dict[str, Any] | None:
    """Called only at the HTTP boundary, after every request projection."""
    binding = call.binding
    runtime = {**call.runtime, **(binding.budget.runtime if binding else {})}
    session_id = runtime.get("_contextUsageSessionId")
    node = binding.invocation.node_id if binding else runtime.get("_runtimeNodeId", "root")
    if not isinstance(session_id, str) or node not in {"root", "primary"} or call.purpose != "agent":
        return None
    sections = list(runtime.get("_contextUsageMemorySections") or ())
    view = getattr(binding.request_context, "_paged_memory_view", None) if binding else None
    if view is not None:
        sections.extend(view.memory_sections)
    snapshot = capture_request(body, runtime=runtime, request_id=call.record["call_id"],
                               node_id=node, memory_sections=sections, endpoint=endpoint,
                               admission_input_tokens=call.record.get("input_reservation"),
                               output_policy=call.request_diagnostics.get("output_budget"),
                               context_input=call.request_diagnostics.get("context_input"))
    save_snapshot(session_id, snapshot, sessions_dir)
    try:
        from minicode.context_sources import capture_sources, record_sources
        receipt = capture_sources(body, runtime, request_id=call.record["call_id"], view=view)
        receipt["capturedAt"] = snapshot["captured_at"]
        record_sources(session_id, receipt, sessions_dir)
    except (OSError, ValueError, TypeError, KeyError) as error:
        report_capture_error(error)
    return {**snapshot, "session_id": session_id}


def observe_input(snapshot: dict[str, Any] | None, usage: Mapping[str, Any], *, complete: bool,
                  sessions_dir: Path | None = None) -> None:
    if snapshot is None or not complete or not usage.get("tokens_known") or not _number(usage.get("input_tokens")):
        return
    snapshot["provider_input_tokens"] = usage["input_tokens"]
    save_snapshot(snapshot["session_id"], snapshot, sessions_dir)


def report_capture_error(error: Exception) -> None:
    # Display telemetry cannot interrupt a user's model request or expose content.
    logging.getLogger(__name__).warning("Context usage snapshot unavailable (%s)", type(error).__name__)
