"""Tests for anchor quality audit detectors."""
from __future__ import annotations

from services.v2.anchor_audit import (
    AnchorFlag,
    FLAG_CAPITALIZED_FORMAL,
    FLAG_DOC_STYLE,
    FLAG_PARAPHRASE,
    FLAG_REPETITIVE_VERBS,
    FLAG_VERBOSE,
    ToolAuditResult,
    aggregate_stats,
    audit_tool,
    find_doc_style_match,
    find_repetitive_verbs,
    is_capitalized_formal,
    is_paraphrase_of_intent,
    is_verbose,
    render_markdown_report,
)


# --------------------------------------------------------------------------
# is_verbose
# --------------------------------------------------------------------------


def test_verbose_flags_long_anchors():
    long_anchor = " ".join(["riječ"] * 15)
    assert is_verbose(long_anchor) is True


def test_verbose_does_not_flag_short_anchors():
    assert is_verbose("kolika mi je km") is False
    assert is_verbose("dodaj 100 km") is False


def test_verbose_threshold_boundary():
    """Exactly 10 words = NOT verbose. 11+ = verbose."""
    ten_words = " ".join(["a"] * 10)
    eleven_words = " ".join(["a"] * 11)
    assert is_verbose(ten_words) is False
    assert is_verbose(eleven_words) is True


def test_verbose_empty_returns_false():
    assert is_verbose("") is False


# --------------------------------------------------------------------------
# find_doc_style_match
# --------------------------------------------------------------------------


def test_doc_style_matches_korisnik_zeli():
    assert find_doc_style_match("Korisnik želi dodati dokument") != ""
    assert find_doc_style_match("korisnik zeli izvršiti") != ""


def test_doc_style_matches_mogu_li_pattern():
    assert find_doc_style_match("Mogu li dodati dokument za firmu") != ""
    assert find_doc_style_match("možeš li mi pokazati km") != ""


def test_doc_style_matches_sto_trebam():
    assert find_doc_style_match("Što trebam učiniti da X") != ""
    assert find_doc_style_match("sto moram napraviti") != ""


def test_doc_style_matches_kako_mogu():
    assert find_doc_style_match("Kako mogu dodati dokument") != ""
    assert find_doc_style_match("kako da promijenim ime") != ""


def test_doc_style_does_not_flag_natural_speech():
    assert find_doc_style_match("dodaj dokument firmi 123") == ""
    assert find_doc_style_match("obriši rezervaciju") == ""
    assert find_doc_style_match("kolika mi je km") == ""


def test_doc_style_empty_returns_empty():
    assert find_doc_style_match("") == ""


# --------------------------------------------------------------------------
# is_capitalized_formal
# --------------------------------------------------------------------------


def test_capitalized_formal_flags_imperative():
    """Driver tipiše lowercase. 'Dodaj' / 'Ažuriraj' / 'Postavi' = formal."""
    assert is_capitalized_formal("Dodaj novo vozilo") is True
    assert is_capitalized_formal("Ažuriraj postojeću dodjelu") is True
    assert is_capitalized_formal("Učitaj novi dokument") is True
    assert is_capitalized_formal("Postavi nove dokumente") is True


def test_capitalized_formal_does_not_flag_lowercase():
    assert is_capitalized_formal("dodaj novo vozilo") is False
    assert is_capitalized_formal("kolika km") is False


def test_capitalized_formal_does_not_flag_questions():
    """'Koja mi je tablica?' OK — pitanje, ne imperative."""
    assert is_capitalized_formal("Koja mi je tablica?") is False
    assert is_capitalized_formal("Što piše na registraciji?") is False


def test_capitalized_formal_does_not_flag_capitalized_nouns():
    """'Auto mi ne radi' — Auto je imenica, ne imperative."""
    # First word "Auto" doesn't end in imperative suffix
    assert is_capitalized_formal("Auto mi je u kvaru") is False
    assert is_capitalized_formal("Vozilo treba popraviti") is False


def test_capitalized_formal_empty_returns_false():
    assert is_capitalized_formal("") is False
    assert is_capitalized_formal("a") is False


# --------------------------------------------------------------------------
# find_repetitive_verbs
# --------------------------------------------------------------------------


def test_repetitive_verbs_detects_three_plus_occurrences():
    """Same verb stem ≥3 times across anchors → flag."""
    anchors = [
        "Ažuriraj dodjelu vozila",
        "Ažuriraj postojeće vozilo",
        "Ažuriraj kontakt podatke",
        "Dodaj novo vozilo",
    ]
    repeats = find_repetitive_verbs(anchors)
    # "ažuri" stem appears 3x
    assert any("ažuri" in v.lower() for v in repeats)


def test_repetitive_verbs_does_not_flag_under_threshold():
    """Same verb 2x is OK (not flag)."""
    anchors = [
        "Ažuriraj dodjelu",
        "Ažuriraj kontakt",
        "Dodaj vozilo",
        "Obriši zapisi",
    ]
    repeats = find_repetitive_verbs(anchors)
    assert repeats == []  # nothing reaches 3 occurrences


def test_repetitive_verbs_diverse_anchors_clean():
    anchors = [
        "dodaj km",
        "ukloni rezervaciju",
        "promijeni ime",
        "pokaži status",
        "evidentiraj putovanje",
    ]
    assert find_repetitive_verbs(anchors) == []


def test_repetitive_verbs_empty_list_returns_empty():
    assert find_repetitive_verbs([]) == []


def test_repetitive_verbs_skips_short_words():
    """Words shorter than verb-stem length (5 chars) get skipped."""
    anchors = ["da", "da", "da", "dodaj nešto"]
    # "da" is too short, no flag
    assert find_repetitive_verbs(anchors) == []


# --------------------------------------------------------------------------
# is_paraphrase_of_intent — cosine semantics
# --------------------------------------------------------------------------


def test_paraphrase_identical_vectors_flagged():
    v = [1.0, 0.0, 0.0]
    assert is_paraphrase_of_intent(v, v) is True


def test_paraphrase_orthogonal_vectors_not_flagged():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert is_paraphrase_of_intent(a, b) is False


def test_paraphrase_threshold_respected():
    # Vectors with cosine ~0.9
    a = [1.0, 0.0, 0.0]
    b = [0.9, 0.43589, 0.0]  # angle ~25°, cosine ~0.9
    assert is_paraphrase_of_intent(a, b, threshold=0.85) is True
    assert is_paraphrase_of_intent(a, b, threshold=0.95) is False


def test_paraphrase_none_vectors_return_false():
    """If either vector missing, can't compute → not flagged."""
    assert is_paraphrase_of_intent(None, [1.0, 0.0]) is False
    assert is_paraphrase_of_intent([1.0, 0.0], None) is False
    assert is_paraphrase_of_intent(None, None) is False


# --------------------------------------------------------------------------
# audit_tool — integration of all detectors
# --------------------------------------------------------------------------


def test_audit_tool_flags_known_bad_post_pattern():
    """Real-world bad POST tool: capitalized + repetitive verbs."""
    tool_data = {
        "method": "POST",
        "intent_summary": "Alat za izmjenu dodjele vozila.",
        "anchors": [
            "Dodijeli novo vozilo",
            "Ažuriraj dodjelu vozila",
            "Izbriši vozilo iz sustava",
            "Kreiraj novo vozilo",
            "Ažuriraj postojeće vozilo",
            "Promijeni dodjelu za automobil",
        ],
    }
    result = audit_tool("post_TestBad", tool_data)
    # All 6 anchors start uppercase + imperative → 6 CAPITALIZED_FORMAL flags
    cap_flags = [f for f in result.flags if f.flag == FLAG_CAPITALIZED_FORMAL]
    assert len(cap_flags) >= 4  # most should flag


def test_audit_tool_clean_anchors_no_flags():
    """User-style anchors should pass clean."""
    tool_data = {
        "method": "POST",
        "intent_summary": "Unos km zapisa.",
        "anchors": [
            "upiši 100 km",
            "dodaj 250 km vozaču",
            "evidentiraj 75 km",
            "stavi 150 km u sustav",
        ],
    }
    result = audit_tool("post_TestGood", tool_data)
    assert result.flagged_anchors == 0
    assert result.total_flag_count == 0


def test_audit_tool_doc_style_flagged():
    tool_data = {
        "method": "POST",
        "intent_summary": "X",
        "anchors": [
            "Korisnik želi dodati dokument",
            "Mogu li dodati dokument za firmu",
            "stavi dokument firmi 123",  # OK
        ],
    }
    result = audit_tool("post_X", tool_data)
    doc_flags = [f for f in result.flags if f.flag == FLAG_DOC_STYLE]
    assert len(doc_flags) == 2  # first 2 anchors


def test_audit_tool_paraphrase_with_embeddings():
    """Paraphrase flag fires only when anchor_vecs + intent_vec provided."""
    tool_data = {
        "method": "GET",
        "intent_summary": "Dodaje dokument partneru",
        "anchors": ["Dodaj dokument partneru", "stavi prilog firmi"],
    }
    # Pretend anchor 0 has identical vector to intent (high cosine)
    intent_vec = [1.0, 0.0, 0.0]
    anchor_vecs = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]  # 1st identical, 2nd orthogonal
    result = audit_tool(
        "get_X", tool_data,
        intent_vec=intent_vec, anchor_vecs=anchor_vecs,
    )
    para_flags = [f for f in result.flags if f.flag == FLAG_PARAPHRASE]
    assert len(para_flags) == 1
    assert para_flags[0].anchor_index == 0


def test_audit_tool_paraphrase_skipped_without_embeddings():
    """Without vectors, paraphrase detector is silent (other flags still fire)."""
    tool_data = {
        "method": "POST",
        "intent_summary": "Dodaje dokument",
        "anchors": ["Dodaj dokument"],  # capitalized formal, but no paraphrase check
    }
    result = audit_tool("post_X", tool_data)  # no vecs
    cap_flags = [f for f in result.flags if f.flag == FLAG_CAPITALIZED_FORMAL]
    para_flags = [f for f in result.flags if f.flag == FLAG_PARAPHRASE]
    assert len(cap_flags) == 1
    assert len(para_flags) == 0


def test_audit_tool_tool_level_repetitive_flag():
    tool_data = {
        "method": "POST",
        "intent_summary": "X",
        "anchors": [
            "Ažuriraj dodjelu",
            "Ažuriraj kontakt",
            "Ažuriraj postojeće vozilo",
            "Dodaj novo",
        ],
    }
    result = audit_tool("post_X", tool_data)
    rep = [t for t in result.tool_level_flags if FLAG_REPETITIVE_VERBS in t]
    assert len(rep) >= 1
    # Stem "ažuri" should be flagged
    assert any("ažuri" in t.lower() for t in rep)


def test_audit_tool_empty_anchors_returns_clean():
    tool_data = {"method": "GET", "intent_summary": "X", "anchors": []}
    result = audit_tool("get_X", tool_data)
    assert result.anchor_count == 0
    assert result.flagged_anchors == 0
    assert result.total_flag_count == 0


def test_audit_tool_handles_non_string_anchors():
    """Defensive: registry might have malformed anchors (None, dict). Skip."""
    tool_data = {
        "method": "GET",
        "intent_summary": "X",
        "anchors": ["dodaj km", None, {"weird": "shape"}, "stavi 100 km"],
    }
    result = audit_tool("get_X", tool_data)
    # Should not crash; only the 2 string anchors evaluated, both clean
    assert result.flagged_anchors == 0


# --------------------------------------------------------------------------
# aggregate_stats
# --------------------------------------------------------------------------


def test_aggregate_stats_counts_per_flag_type():
    results = [
        ToolAuditResult(
            tool_id="t1", method="POST", intent_summary="x",
            anchor_count=12, flagged_anchors=3,
            flags=[
                AnchorFlag(0, "Dodaj X", FLAG_CAPITALIZED_FORMAL),
                AnchorFlag(1, "Dodaj Y", FLAG_CAPITALIZED_FORMAL),
                AnchorFlag(2, "Long " * 12, FLAG_VERBOSE),
            ],
            tool_level_flags=[f"{FLAG_REPETITIVE_VERBS}:dodaj"],
        ),
    ]
    stats = aggregate_stats(results)
    assert stats["total_tools"] == 1
    assert stats["total_anchors"] == 12
    assert stats["flag_counts"][FLAG_CAPITALIZED_FORMAL] == 2
    assert stats["flag_counts"][FLAG_VERBOSE] == 1
    assert stats["flag_counts"][FLAG_REPETITIVE_VERBS] == 1


def test_aggregate_stats_method_breakdown():
    results = [
        ToolAuditResult(tool_id="t1", method="POST", intent_summary="x",
                        anchor_count=12, flagged_anchors=5,
                        flags=[], tool_level_flags=[]),
        ToolAuditResult(tool_id="t2", method="GET", intent_summary="x",
                        anchor_count=12, flagged_anchors=0,
                        flags=[], tool_level_flags=[]),
        ToolAuditResult(tool_id="t3", method="POST", intent_summary="x",
                        anchor_count=12, flagged_anchors=3,
                        flags=[], tool_level_flags=[]),
    ]
    stats = aggregate_stats(results)
    assert stats["method_stats"]["POST"]["tools"] == 2
    assert stats["method_stats"]["POST"]["flagged_anchors"] == 8
    assert stats["method_stats"]["GET"]["tools"] == 1
    assert stats["method_stats"]["GET"]["flagged_anchors"] == 0


# --------------------------------------------------------------------------
# render_markdown_report — smoke tests
# --------------------------------------------------------------------------


def test_render_report_includes_all_sections():
    results = [
        ToolAuditResult(
            tool_id="post_X", method="POST", intent_summary="X tool",
            anchor_count=2, flagged_anchors=1,
            flags=[AnchorFlag(0, "Dodaj X", FLAG_CAPITALIZED_FORMAL)],
            tool_level_flags=[],
        ),
    ]
    md = render_markdown_report(results, today="2026-05-17")
    assert "2026-05-17" in md
    assert "Summary po flagu" in md
    assert "Po HTTP method" in md
    assert "POST" in md
    assert "post_X" in md
    assert "CAPITALIZED_FORMAL" in md
    assert "checklist" in md.lower()


def test_render_report_empty_results_does_not_crash():
    md = render_markdown_report([], today="2026-05-17")
    assert "0 tools" in md or "Nema flagged" in md


def test_render_report_caps_top_n():
    """Many flagged tools — render only top_n_tools."""
    results = [
        ToolAuditResult(
            tool_id=f"post_t{i}", method="POST", intent_summary="x",
            anchor_count=12, flagged_anchors=2,
            flags=[
                AnchorFlag(0, f"Dodaj X{i}", FLAG_CAPITALIZED_FORMAL),
                AnchorFlag(1, f"Dodaj Y{i}", FLAG_CAPITALIZED_FORMAL),
            ],
            tool_level_flags=[],
        )
        for i in range(100)
    ]
    md = render_markdown_report(results, today="2026-05-17", top_n_tools=5)
    # 100 tools total, but only 5 in the report
    assert "post_t0" in md
    assert "post_t4" in md
    assert "post_t99" not in md  # not in top-5
