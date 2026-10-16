"""Deterministic previews and a frozen, source-verified tool result projection."""
from __future__ import annotations

import json
import re
import time

from minicode.runtime_messages import runtime_message_kind
from minicode.source_coordinates import read_file_body

from .models import MEMORY_NAVIGATION_TOOL_NAMES, EventType, MemoryMode, content_hash_text
from .lexical import query_tokens
from .source_excerpt import expand_source
from .result_reclamation import completed_results, projection_key

_IMPORTANT = re.compile(r"error|fatal|fail(?:ed|ure)?|exception|traceback|exit[ _-]?(?:code|status)|错误|失败", re.I)
_FIELDS = ("status", "ok", "exit_code", "exitCode", "code", "error", "message", "summary", "path", "file", "count", "total")
_PROTECTED = {"user_instruction", "task_contract", "acceptance_criteria", "required_evidence"}


def file_read_window(output):
    """Retain declared native file coordinates as navigation, never read authority."""
    from minicode.tools.read_file import MAX_READ_LIMIT
    lines = output.splitlines(keepends=True)
    if not lines or not lines[0].startswith("FILE: "):
        return None
    boundary = next((i for i, line in enumerate(lines[:12]) if line.startswith("TRUNCATED: ")), None)
    if boundary is None:
        return None
    fields = dict(line.rstrip("\r\n").split(": ", 1) for line in lines[:boundary + 1] if ": " in line)
    try:
        start, end, total = (int(fields[key]) for key in ("OFFSET", "END", "TOTAL_CHARS"))
        body = read_file_body(output)
        if not 0 <= start <= end <= total or body is None or len(body) != end - start:
            return None
        line_start = int(fields["START_LINE"]) if "START_LINE" in fields else (1 if start == 0 else None)
        if line_start is not None and line_start < 1:
            return None
    except (KeyError, ValueError):
        return None
    return {"path": fields["FILE"], "coordinate_system": "FILE_RELATIVE_CHAR", "start": start,
            "end": end, "total": total, "start_line": line_start,
            "has_more_file": end < total, "body_header_lines": boundary + 1,
            "next_read": {"tool": "read_file", "arguments": {"path": fields["FILE"], "offset": end,
                "limit": min(total - end, MAX_READ_LIMIT)}} if end < total else None}


def query_excerpts(entries, query):
    """Prefer rare task terms, not every repeated log/comment keyword."""
    if not query or not entries:
        return []
    terms = [term for term in query_tokens(query) if len(term) >= 3 or
             len(term) >= 2 and any("\u3400" <= c <= "\u9fff" for c in term)][:128]
    matches = [(entry, {term for term in terms if term in entry[1].casefold()}) for entry in entries]
    frequency = {term: sum(term in found for _, found in matches) for term in terms}
    rare = {term for term, count in frequency.items() if 0 < count <= max(2, len(entries) // 20)}
    ranked = [(sum(1 / frequency[term] for term in found & rare), entry) for entry, found in matches]
    return [entry for score, entry in sorted(ranked, key=lambda pair: (-pair[0], pair[1][0])) if score > 0][:1]


def output_summary(output, *, tool_name, ok, enhanced=True, query=""):
    lines = output.splitlines()
    non_empty = [(i + 1, line.strip()) for i, line in enumerate(lines) if line.strip()]
    stats = {"bytes": len(output.encode("utf-8")), "characters": len(output), "lines": len(lines)}
    window = file_read_window(output) if tool_name == "read_file" and ok else None
    navigation = []
    if window:
        stats["file_window"] = window
        navigation = ["[File navigation] " + json.dumps({k: v for k, v in window.items()
                      if k not in {"start_line", "body_header_lines"}}, ensure_ascii=False, separators=(",", ":")),
                      "Stored tool-result offsets include headers; they are not original-file offsets. "
                      "Read its missing body via artifact_read/memory_get; use next_read for the remaining file."]
    parsed = None
    try:
        parsed = json.loads(output)
        stats["json_type"] = type(parsed).__name__
        if isinstance(parsed, (list, dict)):
            stats["json_items"] = len(parsed)
    except (ValueError, TypeError):
        pass
    if not enhanced:
        preview = "\n".join([*navigation, *[line for _, line in non_empty[:6]]])[:1200]
        sample = [line[:240] for _, line in non_empty[:3]]
    else:
        pieces = navigation[:]
        priority_count = 0
        if isinstance(parsed, dict):
            for key in _FIELDS:
                if key in parsed:
                    value = json.dumps(parsed[key], ensure_ascii=False)
                    pieces.append(f"{key}={value[:160]}" + ("…" if len(value) > 160 else ""))
        priority_count = len(pieces)
        if non_empty:
            important = [entry for entry in non_empty if _IMPORTANT.search(entry[1])][-2:]
            # Header length depends on character/line mode. Navigation remains
            # in the minimum receipt even when the body is only an excerpt.
            body_start = (next((i + 1 for i, line in enumerate(lines[:12]) if line.startswith("TRUNCATED: ")), 0)
                          if tool_name == "read_file" and lines[0].startswith("FILE: ") else 0)
            file_body = [(n, line) for n, line in non_empty if n > body_start] if body_start else []
            content_lines = file_body or non_empty
            core = [*important, *query_excerpts(content_lines, query), *content_lines[:2]]
            priority_count += len({number for number, _ in core})
            selected = [*core, *content_lines[-2:]]
            seen = set()
            for number, line in selected:
                if number in seen:
                    continue
                seen.add(number)
                # Preserve both ends even when the tool emitted one giant line.
                excerpt = line if len(line) <= 180 else line[:85] + " … " + line[-85:]
                if window and window["start_line"] is not None and number > body_start:
                    label = f"file line {window['start_line'] + number - body_start - 1}"
                else:
                    label = f"tool-result line {number}"
                pieces.append(f"[{label} excerpt] {excerpt}")
        kept, used = [], 0
        for piece in pieces:
            if used + len(piece) + 1 > 800:
                continue
            kept.append(piece)
            used += len(piece) + 1
        preview = "\n".join(kept)
        sample = kept[:3]
        stats["preview_is_excerpt"] = bool(preview)
        stats["priority_excerpt_count"] = min(priority_count, len(kept))
    summary = f"{tool_name} {'succeeded' if ok else 'failed'}; {stats['bytes']} bytes, {len(lines)} lines"
    return summary + ("\n" + preview if preview else ""), stats, sample


def protected_item(item):
    return (item.required or item.item_type.startswith(("CONSTRAINT", "DECISION", "USER_INPUT", "TOOL_ERROR"))
            or bool(_PROTECTED.intersection(item.facets)))


def prepare_projection(runtime, packet, tail, *, user_input=""):
    """Resolve raw sources once. Renderers consume only this frozen value."""
    started = time.perf_counter()
    budget = runtime.token_budget
    costs = {}

    def cost(text):
        if text not in costs:
            costs[text] = budget.estimate_text(text)
        return costs[text]
    results = [m for m in tail if m.get("role") == "tool_result"]
    original_tokens = sum(cost(str(m.get("content", ""))) for m in results)
    other_tokens = sum(budget.estimate_message(m) for m in tail if m.get("role") != "tool_result")
    # Message wrappers still count even when only their bodies are projected.
    wrappers = sum(max(0, budget.estimate_message(m) - cost(str(m.get("content", "")))) for m in results)
    recent_limit = packet.debug_metadata.get("context_allocation", {}).get(
        "recent_conversation_budget", runtime.config.recent_event_token_budget)
    limit = max(0, recent_limit - other_tokens - wrappers)
    info = {"policy": "source-verified-result-preview-v1", "status": "within_budget",
            "body_budget": limit, "original_body_tokens_estimated": original_tokens,
            "projected_body_tokens_estimated": original_tokens, "replacements": [], "unverified_results": 0}
    policy = runtime.result_reclamation if runtime.config.tool_result_reclaim_policy_enabled else None
    if policy is not None:
        info["reclamation"] = policy.begin(packet, user_input)
        info["reclamation"]["decision"] = "within_budget"
    else:
        runtime.result_reclamation.clear()
    if packet.memory_mode == MemoryMode.INLINE:
        runtime.result_reclamation.clear()
        return info
    if original_tokens <= limit and not (policy is not None and policy.entries):
        return info
    required_facets = set(packet.debug_metadata.get("required_facets", ()))
    if required_facets:
        # A required card can be outside its optional card budget. Absence
        # from packet.all_items() cannot license omission of its raw evidence.
        info["status"] = "required_facets_preserved"
        if policy is not None:
            policy.clear()
            info["reclamation"]["decision"] = "required_facets_preserved"
        return info
    protected_sources = {item.source_event_id for item in packet.all_items()
                         if protected_item(item) or required_facets.intersection(item.facets)}
    completed = completed_results(tail) if policy is not None else None
    eligible = [m for m in results if isinstance(m.get("content"), str)
        and not m.get("isError") and runtime_message_kind(m) != "control"
        and m.get("toolName") not in MEMORY_NAVIGATION_TOOL_NAMES and m.get("toolUseId")
        and (completed is None or m["toolUseId"] in completed)]
    events = runtime.authority.tool_result_events(tenant_id=runtime.tenant_id, task_id=runtime.task_id,
                                                call_ids=[str(m["toolUseId"]) for m in eligible])
    by_call = {}
    for event in events:
        by_call.setdefault(event.correlation_id, []).append(event)
    candidates, previous = [], {}
    for message in eligible:
        raw = message["content"]
        digest = content_hash_text(raw)
        event = next((e for e in by_call.get(message["toolUseId"], [])
            if e.event_type == EventType.TOOL_RESULT and e.content_hash == digest
            and e.content_json.get("tool_name") == message.get("toolName")
            and e.content_json.get("context_projection_allowed") is True
            and e.id not in protected_sources and not e.artifact_uri
            and content_hash_text(e.content_text) == digest), None)
        if event is None:
            info["unverified_results"] += 1
            continue
        size = len(raw.encode("utf-8"))
        header = (f"[Tool result preview; full source preserved]\n{message['toolName']} succeeded; "
            f"{size} bytes; content_hash={digest}\n"
            f"Read exact source: memory_get(source_event_id=\"{event.id}\", unit=\"BYTE\", start=0).")
        summary, statistics, _ = output_summary(raw, tool_name=message["toolName"], ok=True, query=user_input)
        preview = summary.split("\n")[1:]
        # A useful minimum receipt includes the first evidence excerpts, not
        # just a locator. If these minima exceed the soft budget, report that
        # explicitly instead of replacing all new evidence by empty headers.
        priority_count = max(2, statistics.get("priority_excerpt_count", 2))
        minimum = header + ("\n" + "\n".join(preview[:priority_count]) if preview else "")
        original = cost(raw)
        if cost(minimum) >= original:
            continue
        candidates.append({"tool_use_id": message["toolUseId"], "tool_name": message["toolName"],
            "expected_content_hash": digest, "source_event_id": event.id, "content": minimum,
            "original_tokens": original, "preview": preview[priority_count:], "raw": raw})
        if policy is not None:
            cached = policy.previous(candidates[-1])
            if cached is not None:
                previous[projection_key(candidates[-1])] = cached
    if policy is not None:
        policy.invalidate_current(results, {projection_key(item) for item in candidates})
    verified_candidate_count = len(candidates)
    used = original_tokens - sum(item["original_tokens"] for item in candidates)
    used += sum(cost(item["content"]) for item in candidates)
    full_results = 0
    if runtime.config.elastic_context_budget_enabled or policy is not None:
        # Water-fill independent result shares. Short complete sources release
        # their unused share to the remaining, larger results.
        targets = [cost(item["content"]) for item in candidates]
        caps = [cost(previous[projection_key(item)]["content"]) if projection_key(item) in previous
                else item["original_tokens"] for item in candidates]
        if policy is not None:
            frozen = [i for i, item in enumerate(candidates) if projection_key(item) in previous]
            extra_to_freeze = sum(caps[i] - targets[i] for i in frozen)
            if used + extra_to_freeze <= limit:
                # New results surrender optional detail before rewriting the
                # previously selected bodies. Larger budgets never re-expand them.
                for i in frozen:
                    targets[i] = caps[i]
                used += extra_to_freeze
        remaining = max(0, limit - used)
        active = [i for i in range(len(candidates)) if targets[i] < caps[i]]
        while remaining and active:
            share = max(1, remaining // len(active))
            for i in active:
                extra = min(share, remaining, caps[i] - targets[i])
                targets[i] += extra
                remaining -= extra
            active = [i for i in active if targets[i] < caps[i]]
        retained = []
        for item, target in zip(candidates, targets):
            cached = previous.get(projection_key(item))
            if cached is not None and target >= cost(cached["content"]):
                item["content"], item["source_ranges"] = cached["content"], cached.get("source_ranges", [])
            else:
                item["content"], item["source_ranges"] = expand_source(item["raw"], item["content"],
                    tool_name=item["tool_name"], query=user_input, token_limit=target, cost=cost)
            if item["content"] == item["raw"]:
                full_results += 1
            else:
                retained.append(item)
        used = original_tokens - sum(item["original_tokens"] - cost(item["content"]) for item in retained)
        candidates = retained
        info.update(policy="source-verified-stable-windows-v3" if policy is not None
                    else "source-verified-result-windows-v2", full_results_preserved=full_results)
    # Fair, bounded allocation of complete excerpt lines; source headers are
    # never cut. Protected bodies and minimum receipts may exceed the soft
    # budget, in which case the final provider guard remains authoritative.
    for index in range(0 if runtime.config.elastic_context_budget_enabled or policy is not None else
                       max((len(item["preview"]) for item in candidates), default=0)):
        for item in candidates:
            if index >= len(item["preview"]):
                continue
            candidate = item["content"] + "\n" + item["preview"][index]
            extra = cost(candidate) - cost(item["content"])
            if used + extra <= limit and cost(candidate) < item["original_tokens"]:
                item["content"] = candidate
                used += extra
    for item in candidates:
        item.pop("preview")
        item.pop("raw")
        item["projected_tokens"] = cost(item["content"])
    if policy is not None:
        candidates, used = policy.select(candidates, previous, original_tokens=original_tokens,
            cost=cost, packet=packet, diagnostic=info["reclamation"])
        # The benefit gate can restore raw bodies after allocation chose previews.
        info["full_results_preserved"] = verified_candidate_count - len(candidates)
    info.update(status="projected" if candidates else "no_verified_optional_sources",
        replacements=candidates, projected_body_tokens_estimated=used,
        minimum_exceeds_budget=used > limit, latency_ms=(time.perf_counter()-started)*1000)
    return info


def project_results(messages, info):
    """No database reads or writes here: reusable by shared budget rendering."""
    replacements = {(item["tool_use_id"], item["tool_name"], item["expected_content_hash"]): item
                    for item in info.get("replacements", ())}
    result, sources = [], set()
    for message in messages:
        item = None
        if message.get("role") == "tool_result" and isinstance(message.get("content"), str):
            item = replacements.get((message.get("toolUseId"), message.get("toolName"), content_hash_text(message["content"])))
        if item is not None:
            result.append({**message, "content": item["content"]})
            sources.add(item["source_event_id"])
        else:
            result.append(dict(message))
    return result, sources
