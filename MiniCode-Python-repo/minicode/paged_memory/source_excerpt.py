"""Expand a verified receipt with exact, contiguous source lines."""
from __future__ import annotations

import ast

from minicode.source_coordinates import read_file_body

from .lexical import query_tokens


def expand_source(raw, minimum, *, tool_name, query, token_limit, cost):
    if cost(raw) <= token_limit:
        return raw, []
    lines = raw.splitlines()
    body_start = 0
    if tool_name == "read_file" and lines and lines[0].startswith("FILE: "):
        body_start = next((i + 1 for i, line in enumerate(lines[:12]) if line.startswith("TRUNCATED: ")), 0)
    if body_start >= len(lines) or cost(minimum) >= token_limit:
        return minimum, []
    terms = set(query_tokens(query)[:128])
    matches = [{term for term in terms if term in line.casefold()} for line in lines[body_start:]]
    frequency = {term: sum(term in found for found in matches) for term in terms}
    rare = {term for term, count in frequency.items() if 0 < count <= max(2, len(matches) // 20)}
    scores = [(sum(1 / frequency[t] for t in found & rare), body_start + i) for i, found in enumerate(matches)]
    anchors = [i for score, i in sorted(scores, key=lambda pair: (-pair[0], pair[1])) if score > 0][:3]
    anchors = anchors or [body_start]
    definitions = []
    if tool_name == "read_file" and lines[0].endswith(".py"):
        try:
            source = read_file_body(raw)
            tree = ast.parse(source if source is not None else "\n".join(lines[body_start:]))
            definitions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        except (SyntaxError, ValueError, RecursionError):
            pass
    blocks, related = [], []
    for anchor in anchors:
        containing = [n for n in definitions if body_start + n.lineno - 1 <= anchor < body_start + n.end_lineno]
        if containing:
            node = min(containing, key=lambda n: n.end_lineno - n.lineno)
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1 + body_start
            blocks.append((start, body_start + node.end_lineno, anchor))
            calls = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            related.extend(n for n in definitions if n.name in calls)
        else:
            blocks.append((max(body_start, anchor - 8), min(len(lines), anchor + 9), anchor))
    for node in related:
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)]) - 1 + body_start
        blocks.append((start, body_start + node.end_lineno, body_start + node.lineno - 1))

    def merge(ranges):
        result = []
        for start, end in sorted(set(ranges)):
            if result and start <= result[-1][1]:
                result[-1] = (result[-1][0], max(end, result[-1][1]))
            else:
                result.append((start, end))
        return result

    def render(ranges):
        return minimum + "".join(
            f"\n[Exact tool-result lines {start + 1}-{end}; includes header offsets; indentation preserved]\n" + "\n".join(lines[start:end])
            for start, end in merge(ranges))

    ranges = []
    for start, end, anchor in blocks:
        candidate = merge([*ranges, (start, end)])
        if cost(render(candidate)) <= token_limit:
            ranges = candidate
            continue
        # A large function is an explicitly bounded contiguous window, never a
        # fabricated complete function. Existing important excerpts remain.
        low, high, best = 0, max(anchor - start, end - anchor), ranges
        while low <= high:
            radius = (low + high) // 2
            candidate = merge([*ranges, (max(start, anchor - radius), min(end, anchor + radius + 1))])
            if cost(render(candidate)) <= token_limit:
                best, low = candidate, radius + 1
            else:
                high = radius - 1
        ranges = best
    # Modest neighboring context is useful; spare capacity is not a reason to
    # fill a request with hundreds of unrelated comments or log lines.
    if ranges:
        seed = ranges
        low, high, best = 0, 12, ranges
        while low <= high:
            radius = (low + high) // 2
            candidate = merge([(max(body_start, start - radius), min(len(lines), end + radius)) for start, end in seed])
            if cost(render(candidate)) <= token_limit:
                best, low = candidate, radius + 1
            else:
                high = radius - 1
        ranges = best
    return render(ranges), [{"start_line": start + 1, "end_line": end} for start, end in ranges]
