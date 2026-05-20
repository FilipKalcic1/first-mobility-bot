"""Tests for clarify_ui (Top-3 cards UX)."""
from __future__ import annotations

from dataclasses import dataclass

from services.v2.clarify_ui import (
    ClarifyOptions,
    build_clarify_options,
    build_from_router_candidates,
    parse_clarify_reply,
    render_infobip_list_message,
    render_text,
)


@dataclass
class _FakeCand:
    tool_id: str
    purpose: str = ""


def test_build_clarify_options_top3_cards():
    cands = [
        _FakeCand("delete_VehicleCalendar_id", "Otkaži pojedinačnu rezervaciju vozila."),
        _FakeCand("get_VehicleCalendar", "Lista mojih rezervacija."),
        _FakeCand("put_VehicleCalendar_id", "Promijeni rezervaciju."),
        _FakeCand("get_LatestVehicleCalendar", "Aktivne rezervacije."),
    ]
    opts = build_clarify_options(cands, max_cards=3)
    assert len(opts.cards) == 3
    assert opts.cards[0].index == 1
    assert opts.cards[0].tool_id == "delete_VehicleCalendar_id"


def test_short_label_uses_croatian_verb_entity():
    cands = [_FakeCand("delete_VehicleCalendar_id", "Otkaži")]
    opts = build_clarify_options(cands)
    assert "Obriši" in opts.cards[0].short_label
    assert "rezervaciju" in opts.cards[0].short_label


def test_render_text_includes_emoji_numbers():
    cands = [
        _FakeCand("get_VehicleCalendar", "Pokaži rezervacije."),
        _FakeCand("post_AddMileage", "Unesi km."),
    ]
    opts = build_clarify_options(cands)
    text = render_text(opts)
    assert "1️⃣" in text
    assert "2️⃣" in text
    assert "❌" in text


def test_render_text_handles_empty_cards():
    opts = ClarifyOptions(cards=[])
    text = render_text(opts)
    assert "Nisam siguran" in text or "kontaktiraj" in text


def test_render_infobip_list_format():
    cands = [
        _FakeCand("delete_VehicleCalendar_id", "Otkaži pojedinačnu."),
        _FakeCand("delete_VehicleCalendar", "Otkaži sve."),
    ]
    opts = build_clarify_options(cands)
    payload = render_infobip_list_message(opts, header="Pojašnjenje", body="Što?")
    assert payload["type"] == "INTERACTIVE_LIST"
    rows = payload["content"]["action"]["sections"][0]["rows"]
    # 2 cards + 1 'none of above' = 3 rows
    assert len(rows) == 3
    assert rows[0]["id"].startswith("clarify::")
    # Title length capped to 24 (Infobip limit)
    for r in rows:
        assert len(r["title"]) <= 24


def test_parse_reply_numeric():
    cands = [_FakeCand("a", ""), _FakeCand("b", ""), _FakeCand("c", "")]
    opts = build_clarify_options(cands)
    assert parse_clarify_reply("1", opts) == "a"
    assert parse_clarify_reply("2", opts) == "b"
    assert parse_clarify_reply("3", opts) == "c"


def test_parse_reply_emoji():
    cands = [_FakeCand("a", ""), _FakeCand("b", "")]
    opts = build_clarify_options(cands)
    assert parse_clarify_reply("1️⃣", opts) == "a"
    assert parse_clarify_reply("2️⃣", opts) == "b"


def test_parse_reply_negative_returns_none():
    cands = [_FakeCand("a", ""), _FakeCand("b", "")]
    opts = build_clarify_options(cands)
    assert parse_clarify_reply("ne", opts) is None
    assert parse_clarify_reply("ništa", opts) is None
    assert parse_clarify_reply("nešto drugo", opts) is None


def test_parse_reply_infobip_id():
    cands = [_FakeCand("delete_VehicleCalendar_id", "")]
    opts = build_clarify_options(cands)
    assert parse_clarify_reply("clarify::delete_VehicleCalendar_id", opts) == "delete_VehicleCalendar_id"
    assert parse_clarify_reply("clarify::none", opts) is None


def test_parse_reply_unknown_returns_none():
    cands = [_FakeCand("a", "")]
    opts = build_clarify_options(cands)
    assert parse_clarify_reply("hello world", opts) is None
    assert parse_clarify_reply("9", opts) is None  # out of range
    assert parse_clarify_reply("", opts) is None


def test_parse_reply_word_form():
    cands = [_FakeCand("a", ""), _FakeCand("b", ""), _FakeCand("c", "")]
    opts = build_clarify_options(cands)
    assert parse_clarify_reply("jedan", opts) == "a"
    assert parse_clarify_reply("Prvo", opts) == "a"
    assert parse_clarify_reply("tri", opts) == "c"


# --- build_from_router_candidates (new builder used by engine FALLBACK) ----


def test_build_from_router_candidates_uses_tkb_intent_for_description():
    """Engine passes (tool_id, anchor_score) tuples + a TKB lookup callback.
    The builder must produce ClarifyOptions with cards that have a sensible
    short_label AND the TKB intent_summary as description."""
    intents = {
        "delete_VehicleCalendar_id": "Otkazuje pojedinačnu rezervaciju vozila po ID-u.",
        "get_VehicleCalendar":        "Vraća listu rezervacija u kalendaru vozila.",
        "put_VehicleCalendar_id":     "Mijenja postojeću rezervaciju.",
    }
    candidates = [
        ("delete_VehicleCalendar_id", 0.62),
        ("get_VehicleCalendar",        0.48),
        ("put_VehicleCalendar_id",     0.43),
    ]
    opts = build_from_router_candidates(
        candidates, tkb_lookup=lambda tid: intents.get(tid, ""),
    )
    assert len(opts.cards) == 3
    assert opts.cards[0].tool_id == "delete_VehicleCalendar_id"
    assert opts.cards[0].short_label.startswith("Obriši ")
    assert "rezervaciju" in opts.cards[0].description
    assert opts.cards[2].index == 3


def test_build_from_router_candidates_truncates_to_max_cards():
    candidates = [(f"get_X{i}", 0.5) for i in range(10)]
    opts = build_from_router_candidates(
        candidates, tkb_lookup=lambda tid: "purpose", max_cards=3,
    )
    assert len(opts.cards) == 3


def test_build_from_router_candidates_skips_empty_tool_ids():
    """Defensive: gate output shouldn't have empty tool_ids, but if it does
    we skip them rather than producing a malformed card."""
    candidates = [("", 0.9), ("get_X", 0.5), ("", 0.3), ("get_Y", 0.2)]
    opts = build_from_router_candidates(
        candidates, tkb_lookup=lambda tid: "",
    )
    assert [c.tool_id for c in opts.cards] == ["get_X", "get_Y"]
    # Indices are 1-based and contiguous (not skipped along with skipped tools)
    assert [c.index for c in opts.cards] == [1, 2]


def test_build_from_router_candidates_empty_input_returns_no_cards():
    opts = build_from_router_candidates([], tkb_lookup=lambda tid: "")
    assert opts.cards == []
    # render_text on empty options should fall through to the polite-fail message
    txt = render_text(opts)
    assert "kontaktiraj managera" in txt.lower() or "drugačije" in txt


def test_build_from_router_candidates_missing_tkb_uses_tool_id_as_description():
    """If TKB lookup returns empty (e.g. new tool not yet in TKB), description
    falls back to the operation_id — readable, never blank."""
    opts = build_from_router_candidates(
        [("get_SomethingNotInTKB", 0.7)],
        tkb_lookup=lambda tid: "",  # nothing in TKB
    )
    assert len(opts.cards) == 1
    assert opts.cards[0].description == "get_SomethingNotInTKB"
