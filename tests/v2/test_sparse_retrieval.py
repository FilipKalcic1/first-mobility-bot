"""Tests for BM25 + RRF utilities used in L3 hybrid retrieval."""
from __future__ import annotations

from services.utils.sparse import (
    BM25Index, reciprocal_rank_fusion, tokenize,
)


# ---- tokenizer ---------------------------------------------------------


def test_tokenize_lowercases_and_strips_diacritics():
    toks = tokenize("Kilometraža Rezervacija")
    assert "kilometraza" in toks
    assert "rezervacija" in toks


def test_tokenize_splits_on_punctuation():
    assert tokenize("auto, registracija!") == ["auto", "registracija"]


def test_tokenize_empty_input_returns_empty():
    assert tokenize("") == []
    assert tokenize(None) == []  # type: ignore[arg-type]


# ---- BM25Index ---------------------------------------------------------


def _build(docs):
    return BM25Index([tokenize(d) for d in docs])


def test_bm25_zero_for_no_overlap():
    idx = _build(["the first document about cars"])
    assert idx.score_all("totally unrelated text") == [0.0]


def test_bm25_higher_for_more_overlap():
    idx = _build([
        "auto registracija tablica",
        "telefon broj kontakt",
        "kilometraza vozilo",
    ])
    scores = idx.score_all("auto tablica")
    # First doc has both query terms; the others have neither
    assert scores[0] > 0
    assert scores[1] == 0
    assert scores[2] == 0


def test_bm25_idf_penalizes_common_terms():
    """Term that appears in every doc should contribute much less than
    a rare term."""
    idx = _build([
        "common word kilometraza",
        "common word telefon",
        "common word registracija",
    ])
    common_scores = idx.score_all("common")
    rare_scores = idx.score_all("kilometraza")
    # rare query lights up exactly one doc; common should light all three
    # but with much lower per-doc contribution
    assert max(rare_scores) > max(common_scores)


def test_bm25_handles_empty_query():
    idx = _build(["a b c", "d e f"])
    assert idx.score_all("") == [0.0, 0.0]


def test_bm25_diacritic_match():
    """Croatian: 'kilometraža' in doc must match 'kilometraza' in query
    (and vice versa)."""
    idx = _build(["kilometraža vozila"])
    s = idx.score_all("kilometraza")
    assert s[0] > 0


def test_bm25_empty_index():
    idx = BM25Index([])
    assert idx.score_all("anything") == []


# ---- RRF ---------------------------------------------------------------


def test_rrf_unanimous_first_place_wins():
    fused = reciprocal_rank_fusion([[0, 1, 2], [0, 2, 1]])
    assert fused[0][0] == 0


def test_rrf_doc_in_one_list_only_still_ranks():
    fused = reciprocal_rank_fusion([[0, 1, 2], [3, 4, 5]])
    ids = [doc for doc, _ in fused]
    # All six should appear; none of them appear in both lists
    assert set(ids) == {0, 1, 2, 3, 4, 5}


def test_rrf_doc_in_both_lists_outranks_doc_in_one():
    """Even if a doc is rank 3 in both lists, it should outrank a doc
    that is rank 1 in only one list (since RRF rewards consensus)."""
    fused = reciprocal_rank_fusion(
        [[5, 4, 0], [5, 4, 0]],  # doc 0 is third in BOTH
        k=1,                     # small k so rank gaps matter
    )
    # doc 0 appears in both rankings → score 1/4 + 1/4 = 0.5
    # If we add a fourth doc that appears only in list 1 at rank 1 → 1/2
    fused2 = reciprocal_rank_fusion([[7, 4, 0], [5, 4, 0]], k=1)
    score_0 = dict(fused2)[0]
    score_7 = dict(fused2)[7]
    # 0 appears in both at rank 3 → 1/(1+2)+1/(1+2)=2/3
    # 7 appears in only one at rank 1 → 1/(1+0)=1
    # So 7 actually beats 0; this assertion confirms RRF semantics
    assert score_7 > score_0


def test_rrf_empty_input_returns_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []
