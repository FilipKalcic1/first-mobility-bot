"""
BM25 Index — exact term matching complement to FAISS semantic search.

FAISS uses dense embeddings (cosine similarity in 1536-dim space).
BM25 uses sparse term frequencies (exact word matching).

When a user says an exact term from documentation (e.g., "VehicleTypes"),
FAISS might rank it lower than a semantically similar but wrong tool.
BM25 catches these exact matches.

Score merge in unified_search.py:
    final_score = faiss_cosine + bm25_boost
    where bm25_boost = BM25_WEIGHT * normalized_bm25_score

No external dependencies — uses a simple TF-IDF based BM25 implementation.
"""


import logging
import math
import re
import threading
from typing import Dict, List, Optional, Tuple

from services.text_normalizer import normalize_diacritics

logger = logging.getLogger(__name__)

# BM25 parameters
_K1 = 1.5   # Term frequency saturation
_B = 0.75   # Length normalization


def _tokenize(text: str) -> List[str]:
    """Tokenize text: lowercase, split on non-alphanumeric, remove short tokens."""
    text = normalize_diacritics(text.lower())
    tokens = re.split(r'[^a-z0-9]+', text)
    return [t for t in tokens if len(t) >= 2]


class BM25Index:
    """
    Lightweight BM25 index for tool documentation.

    Built from tool_documentation.json fields:
    - purpose
    - when_to_use
    - example_queries_hr
    - synonyms_hr
    - tool_id itself (split on underscores)
    """

    def __init__(self):
        self._tool_ids: List[str] = []
        self._tool_id_to_idx: Dict[str, int] = {}
        # Per-doc token frequencies and lengths (replaces stored token lists).
        self._doc_tfs: List[Dict[str, int]] = []
        self._doc_lengths: List[int] = []
        # Inverted index: token -> list of (doc_idx, tf). Lets search() skip docs that
        # don't contain the token instead of scanning the full corpus per query token.
        self._postings: Dict[str, List[Tuple[int, int]]] = {}
        self._doc_freq: Dict[str, int] = {}     # document frequency per term (= len(postings[token]))
        self._avg_dl: float = 0.0               # average document length
        self._n_docs: int = 0
        self._built = False

    def build(self, tool_docs: dict):
        """Build BM25 index from tool_documentation.json."""
        self._tool_ids = []
        self._tool_id_to_idx = {}
        self._doc_tfs = []
        self._doc_lengths = []
        self._postings = {}
        self._doc_freq = {}

        for tool_id, doc in tool_docs.items():
            idx = len(self._tool_ids)
            self._tool_ids.append(tool_id)
            self._tool_id_to_idx[tool_id.lower()] = idx

            # Build document text from all relevant fields.
            parts: List[str] = [tool_id.replace("_", " ")]
            if doc.get("purpose"):
                parts.append(doc["purpose"])
            parts.extend(doc.get("when_to_use", []) or [])
            parts.extend(doc.get("example_queries_hr", []) or [])
            parts.extend(doc.get("synonyms_hr", []) or [])

            tokens = _tokenize(" ".join(parts))
            self._doc_lengths.append(len(tokens))

            # Collapse to tf map once, then append to postings. Cheaper than recounting
            # per query in search().
            tf_map: Dict[str, int] = {}
            for tok in tokens:
                tf_map[tok] = tf_map.get(tok, 0) + 1
            self._doc_tfs.append(tf_map)

            for tok, tf in tf_map.items():
                self._postings.setdefault(tok, []).append((idx, tf))
                self._doc_freq[tok] = self._doc_freq.get(tok, 0) + 1

        self._n_docs = len(self._tool_ids)
        total_tokens = sum(self._doc_lengths)
        # Guard: if all tools tokenize to empty (malformed docs), avg_dl would be 0
        # causing ZeroDivisionError in _bm25_term_score(). Default to 1.0.
        self._avg_dl = (total_tokens / max(self._n_docs, 1)) if total_tokens > 0 else 1.0
        self._built = True

        logger.info(
            f"BM25Index: Built index for {self._n_docs} tools, "
            f"vocab={len(self._doc_freq)}, avg_dl={self._avg_dl:.1f}"
        )

    def _score_from_tf(self, tf: int, dl: int, df: int) -> float:
        """BM25 score for a term given its tf in the doc, the doc length, and its df."""
        if tf == 0 or df == 0:
            return 0.0
        idf = math.log((self._n_docs - df + 0.5) / (df + 0.5) + 1.0)
        numerator = tf * (_K1 + 1)
        denominator = tf + _K1 * (1 - _B + _B * dl / self._avg_dl)
        return idf * numerator / denominator

    def _bm25_term_score(self, token: str, idx: int) -> float:
        """Calculate BM25 score for a single term in a document (by lookup, not rescan)."""
        df = self._doc_freq.get(token, 0)
        if df == 0:
            return 0.0
        tf = self._doc_tfs[idx].get(token, 0)
        return self._score_from_tf(tf, self._doc_lengths[idx], df)

    def search(self, query: str, top_k: int = 100) -> List[Tuple[str, float]]:
        """Search for tools matching query terms via inverted-index lookup.

        Returns list of (tool_id, bm25_score) sorted by score descending.
        """
        if not self._built:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores: Dict[int, float] = {}

        for token in query_tokens:
            postings = self._postings.get(token)
            if not postings:
                continue
            df = len(postings)
            for idx, tf in postings:
                term_score = self._score_from_tf(tf, self._doc_lengths[idx], df)
                if term_score > 0:
                    scores[idx] = scores.get(idx, 0.0) + term_score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [(self._tool_ids[idx], score) for idx, score in ranked]

    def get_score(self, query: str, tool_id: str) -> float:
        """Get BM25 score for a specific tool given a query."""
        if not self._built:
            return 0.0

        idx = self._tool_id_to_idx.get(tool_id.lower())
        if idx is None:
            return 0.0

        query_tokens = _tokenize(query)
        if not query_tokens:
            return 0.0

        return sum(self._bm25_term_score(token, idx) for token in query_tokens)

    def get_scores_batch(self, query: str, tool_ids: List[str]) -> Dict[str, float]:
        """Get BM25 scores for a batch of tools (used during boost pipeline)."""
        if not self._built:
            return {}

        query_tokens = _tokenize(query)
        if not query_tokens:
            return {}

        result: Dict[str, float] = {}
        for tool_id in tool_ids:
            idx = self._tool_id_to_idx.get(tool_id.lower())
            if idx is None:
                continue
            score = sum(self._bm25_term_score(token, idx) for token in query_tokens)
            if score > 0:
                result[tool_id] = score
        return result

    @property
    def is_built(self) -> bool:
        return self._built


# Singleton
_bm25_index: Optional[BM25Index] = None
_singleton_lock = threading.Lock()


def get_bm25_index() -> BM25Index:
    """Get singleton BM25Index."""
    global _bm25_index
    if _bm25_index is None:
        with _singleton_lock:
            if _bm25_index is None:
                _bm25_index = BM25Index()
    return _bm25_index
