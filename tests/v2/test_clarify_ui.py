"""Tests for clarify_ui (Top-3 cards UX)."""
from __future__ import annotations

from dataclasses import dataclass

from services.v2.clarify_ui import (
    ClarifyOptions,
    build_clarify_options,
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
