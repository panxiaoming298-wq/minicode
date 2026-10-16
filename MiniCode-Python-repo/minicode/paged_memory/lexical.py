"""Deterministic search text preparation shared by authority adapters.

The authority database owns exact/full-text retrieval.  This module keeps the
consistent token stream in SQLite so keyword retrieval ranks the same
Chinese, English, and source-code identifiers instead of falling back to a
bounded Python scan.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

_RAW_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[0-9]+|[\u3400-\u9fff]+")
_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+")


def lexical_tokens(value: str) -> list[str]:
    """Return stable lexemes, including Chinese bigrams and code sub-tokens."""

    tokens: list[str] = []
    for match in _RAW_TOKEN.finditer(value):
        raw = match.group(0)
        if "\u3400" <= raw[0] <= "\u9fff":
            characters = list(raw)
            tokens.extend(characters)
            tokens.extend(
                "".join(characters[index : index + 2])
                for index in range(len(characters) - 1)
            )
            if len(characters) > 2:
                tokens.append(raw)
            continue

        normalized = raw.casefold()
        tokens.append(normalized)
        parts = [part.casefold() for part in _CAMEL_PART.findall(raw)]
        for part in parts:
            if part and part != normalized:
                tokens.append(part)
        if "_" in normalized:
            tokens.extend(part for part in normalized.split("_") if part)
    return tokens


def memory_search_document(
    *, title: str, preview_text: str, facets: Sequence[str]
) -> str:
    """Build the persisted FTS document with modest title/facet weighting."""

    title_tokens = lexical_tokens(title)
    preview_tokens = lexical_tokens(preview_text)
    facet_tokens = lexical_tokens(" ".join(facets))
    return " ".join(title_tokens * 3 + facet_tokens * 2 + preview_tokens)


def query_tokens(value: str) -> list[str]:
    """Prefer Chinese words/bigrams; keep explicit single-character queries.

    Existing persisted documents already contain these lexemes, so improving
    query precision does not require rewriting authoritative records or FTS.
    """
    terms: list[str] = []
    for match in _RAW_TOKEN.finditer(value):
        raw = match.group(0)
        tokens = lexical_tokens(raw)
        if "\u3400" <= raw[0] <= "\u9fff" and len(raw) > 1:
            tokens = [token for token in tokens if len(token) > 1]
        terms.extend(tokens)
    return list(dict.fromkeys(terms))


def sqlite_match_expression(value: str) -> str:
    """Render safe FTS5 OR terms; lexical_tokens emits no query operators."""

    return " OR ".join(
        f'"{token.replace(chr(34), chr(34) * 2)}"' for token in query_tokens(value)
    )


def scope_boost(
    *, record_phase: str, record_run_id: str, phase: str, run_id: str
) -> float:
    """Prefer the current scope without making older phases/runs invisible."""

    score = 0.0
    if phase:
        score += 0.04 if record_phase == phase else 0.01 if not record_phase else 0.0
    if run_id:
        score += 0.06 if record_run_id == run_id else 0.01 if not record_run_id else 0.0
    return score


def bm25_scores(documents: Iterable[str], query: str) -> list[float]:
    """Small fallback used only when a Python SQLite build lacks FTS5."""

    material = [lexical_tokens(document) for document in documents]
    terms = query_tokens(query)
    if not material or not terms:
        return [0.0 for _ in material]
    average_length = sum(len(document) for document in material) / len(material)
    document_frequency = Counter(
        term for document in material for term in set(document) if term in terms
    )
    k1 = 1.2
    b = 0.75
    scores: list[float] = []
    for document in material:
        frequencies = Counter(document)
        length_ratio = len(document) / max(1.0, average_length)
        score = 0.0
        for term in terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            occurrences = document_frequency.get(term, 0)
            inverse_frequency = math.log(
                1.0 + (len(material) - occurrences + 0.5) / (occurrences + 0.5)
            )
            denominator = frequency + k1 * (1.0 - b + b * length_ratio)
            score += inverse_frequency * frequency * (k1 + 1.0) / denominator
        scores.append(score)
    return scores


__all__ = [
    "bm25_scores",
    "lexical_tokens",
    "memory_search_document",
    "query_tokens",
    "scope_boost",
    "sqlite_match_expression",
]
