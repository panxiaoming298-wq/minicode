"""Vector-based memory search — parallel path to BM25.

Local TF-IDF keyword similarity, without a database or embedding model.

Results merged with BM25 via reciprocal rank fusion.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("vector_memory")


class SparseVectorStore:
    """Zero-dependency sparse TF-IDF vector store.

    Uses the same tokenization infrastructure as BM25. Each document is
    a sparse {term_id: tfidf_weight} dict. Cosine similarity provides
    a local term-overlap path parallel to BM25 keyword scoring.
    """

    def __init__(self):
        self._enabled = True
        self._doc_vectors: dict[str, dict[int, float]] = {}
        self._term_to_id: dict[str, int] = {}
        self._id_to_term: dict[int, str] = {}
        self._doc_freq: dict[int, int] = {}
        self._doc_count = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def index_entries(self, entries: list[Any]) -> int:
        from minicode.memory import _tokenize

        doc_term_counts: list[Counter[str]] = []
        for entry in entries:
            content = getattr(entry, 'content', '')
            if not content.strip():
                continue
            doc_term_counts.append(Counter(_tokenize(content)))

        for tc in doc_term_counts:
            for term in tc:
                if term not in self._term_to_id:
                    self._term_to_id[term] = len(self._term_to_id)
                    self._id_to_term[self._term_to_id[term]] = term

        self._doc_count = len(doc_term_counts)
        self._doc_freq.clear()
        for tc in doc_term_counts:
            for term in tc:
                self._doc_freq[self._term_to_id[term]] = self._doc_freq.get(self._term_to_id[term], 0) + 1

        count = 0
        for entry, tc in zip(entries, doc_term_counts):
            eid = getattr(entry, 'id', '')
            if eid in self._doc_vectors:
                continue
            vec: dict[int, float] = {}
            total = max(sum(tc.values()), 1)
            for term, freq in tc.items():
                tid = self._term_to_id[term]
                tf = freq / total
                df = self._doc_freq.get(tid, 1)
                idf = math.log((self._doc_count + 1) / (df + 1)) + 1
                vec[tid] = tf * idf
            self._doc_vectors[eid] = vec
            count += 1
        return count

    def search(self, query: str, candidate_ids: list[str] | None = None, top_k: int = 10) -> list[tuple[str, float]]:
        from minicode.memory import _tokenize

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        q_tc = Counter(q_tokens)
        q_vec: dict[int, float] = {}
        total = max(sum(q_tc.values()), 1)
        for term, freq in q_tc.items():
            tid = self._term_to_id.get(term)
            if tid is not None:
                df = self._doc_freq.get(tid, 1)
                idf = math.log((self._doc_count + 1) / (df + 1)) + 1
                q_vec[tid] = (freq / total) * idf
        if not q_vec:
            return []

        q_norm = math.sqrt(sum(w * w for w in q_vec.values()))
        if q_norm == 0:
            return []

        results: list[tuple[str, float]] = []
        for eid, d_vec in self._doc_vectors.items():
            if candidate_ids and eid not in candidate_ids:
                continue
            d_norm = math.sqrt(sum(w * w for w in d_vec.values()))
            if d_norm == 0:
                continue
            dot = sum(q_vec.get(tid, 0.0) * w for tid, w in d_vec.items())
            sim = dot / (q_norm * d_norm)
            if sim > 0.1:
                results.append((eid, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def clear(self) -> None:
        self._doc_vectors.clear()
        self._term_to_id.clear()
        self._id_to_term.clear()
        self._doc_freq.clear()
        self._doc_count = 0


def merge_bm25_vector(
    bm25_results: list[Any],
    vector_results: list[tuple[str, float]],
    k: int = 60,
) -> list[Any]:
    """Reciprocal rank fusion between BM25 and vector results."""
    if not vector_results:
        return bm25_results
    bm25_rank: dict[str, int] = {}
    for i, entry in enumerate(bm25_results):
        bm25_rank[getattr(entry, 'id', '')] = i + 1
    vector_rank: dict[str, int] = {}
    for i, (eid, _) in enumerate(vector_results):
        vector_rank[eid] = i + 1
    all_ids = set(bm25_rank.keys()) | set(vector_rank.keys())
    scores: dict[str, float] = {}
    for eid in all_ids:
        score = 0.0
        if eid in bm25_rank:
            score += 1.0 / (k + bm25_rank[eid])
        if eid in vector_rank:
            score += 1.0 / (k + vector_rank[eid])
        scores[eid] = score
    eid_to_entry = {getattr(e, 'id', ''): e for e in bm25_results}
    sorted_ids = sorted(scores.keys(), key=lambda eid: scores[eid], reverse=True)
    return [eid_to_entry[eid] for eid in sorted_ids if eid in eid_to_entry]
