"""Tests for param_ui: question rendering + answer parsing."""
from __future__ import annotations

from datetime import datetime, timedelta

from services.v2 import param_ui


# --------------------------------------------------------------------------
# humanize_param_name
# --------------------------------------------------------------------------

def test_humanize_camelcase_splits_words():
    assert param_ui.humanize_param_name("VehicleId") == "vehicle id"
    assert param_ui.humanize_param_name("LastMileage") == "last mileage"


def test_humanize_snake_case_splits_words():
    assert param_ui.humanize_param_name("vehicle_id") == "vehicle id"
    assert param_ui.humanize_param_name("from_time") == "from time"


def test_humanize_empty_returns_empty():
    assert param_ui.humanize_param_name("") == ""


def test_humanize_already_lowercased_passes_through():
    assert param_ui.humanize_param_name("id") == "id"


# --------------------------------------------------------------------------
# render_param_question — Faza 3 (Filip 2026-05-17): label_override from
# ParamLabeler (LLM). Curated PARAM_LABELS dict has been removed.
# --------------------------------------------------------------------------

def test_render_question_uses_label_override_when_provided():
    q = param_ui.render_param_question("id", label_override="ID rezervacije")
    assert "ID rezervacije" in q
    assert "odustani" in q.lower()


def test_render_question_falls_back_to_humanize_when_no_override():
    """No label_override = LLM labeler returned None / not wired. Use
    best-effort humanize. Output reads 'Trebam vehicle id...' — ugly but
    functional (engine should always provide override in production)."""
    q = param_ui.render_param_question("VehicleId")
    assert "vehicle id" in q.lower()


def test_render_question_empty_override_falls_back_to_humanize():
    """Empty/whitespace override is treated the same as None."""
    q = param_ui.render_param_question("WeirdParam", label_override="   ")
    assert "weird param" in q.lower()


def test_render_question_appends_short_description():
    q = param_ui.render_param_question(
        "Mileage",
        {"description": "očitanje brojača"},
        label_override="broj kilometara",
    )
    assert "broj kilometara" in q
    assert "očitanje brojača" in q


def test_render_question_skips_long_description():
    long_desc = "x" * 100
    q = param_ui.render_param_question(
        "Notes", {"description": long_desc}, label_override="napomenu",
    )
    assert long_desc not in q  # too long, omitted


def test_render_question_skips_description_when_redundant_with_label():
    q = param_ui.render_param_question(
        "Notes", {"description": "napomenu"}, label_override="napomenu",
    )
    # 'napomenu' is already in the label so no extra parenthetical
    assert q.count("napomenu") == 1


# --------------------------------------------------------------------------
# render_optional_offer
# --------------------------------------------------------------------------

def test_render_optional_offer_lists_labels_with_overrides():
    msg = param_ui.render_optional_offer(
        ["Notes", "Description"],
        label_overrides={"Notes": "napomenu", "Description": "opis"},
    )
    assert "napomenu" in msg
    assert "opis" in msg
    # Free-text instruction (Model A LLM extract — no more yes/no)
    assert "slobodno" in msg.lower() or "napiši" in msg.lower()
    assert "'ne'" in msg  # explicit skip token


def test_render_optional_offer_falls_back_to_humanize_without_overrides():
    """No label_overrides → humanize fallback (ugly but functional)."""
    msg = param_ui.render_optional_offer(["Notes", "Description"])
    assert "notes" in msg.lower()
    assert "description" in msg.lower()


def test_render_optional_offer_partial_overrides():
    """Some overrides provided, others fall back to humanize."""
    msg = param_ui.render_optional_offer(
        ["Notes", "Description"],
        label_overrides={"Notes": "napomenu"},  # Description missing
    )
    assert "napomenu" in msg
    assert "description" in msg.lower()  # humanize fallback


def test_render_optional_offer_empty_returns_empty():
    assert param_ui.render_optional_offer([]) == ""


def test_render_optional_offer_caps_long_lists():
    """When optional count > max_shown, render trims to max_shown + count of rest."""
    optionals = [f"Field{i}" for i in range(10)]
    msg = param_ui.render_optional_offer(optionals, max_shown=3)
    # First 3 humanized labels appear
    assert "field0" in msg.lower()
    assert "field1" in msg.lower()
    assert "field2" in msg.lower()
    # Remaining 7 are abbreviated
    assert "još 7 polja" in msg
    # 4th label NOT listed (only first 3 shown)
    assert "field3" not in msg.lower()


def test_render_optional_offer_exactly_at_cap_does_not_abbreviate():
    """Exactly max_shown items → no '(i još N polja)' suffix."""
    optionals = ["Notes", "Type", "Code"]
    msg = param_ui.render_optional_offer(optionals, max_shown=3)
    assert "još" not in msg


# --------------------------------------------------------------------------
# render_param_reask
# --------------------------------------------------------------------------

def test_reask_integer_includes_number_example():
    msg = param_ui.render_param_reask(
        "Mileage", {"param_type": "integer"},
        label_override="broj kilometara",
    )
    assert "42500" in msg


def test_reask_uses_label_override():
    msg = param_ui.render_param_reask(
        "Mileage", {"param_type": "integer"},
        label_override="broj kilometara",
    )
    assert "broj kilometara" in msg


def test_reask_falls_back_to_humanize_without_override():
    msg = param_ui.render_param_reask(
        "WeirdParam", {"param_type": "string"},
    )
    assert "weird param" in msg.lower()


def test_reask_datetime_includes_format_example():
    msg = param_ui.render_param_reask(
        "FromTime", {"param_type": "string", "format": "date-time"},
    )
    assert "2026" in msg or "09:00" in msg


def test_reask_boolean_includes_da_ne():
    msg = param_ui.render_param_reask(
        "Active", {"param_type": "boolean"},
    )
    assert "da/ne" in msg


# --------------------------------------------------------------------------
# is_cancel / is_negative
# --------------------------------------------------------------------------

def test_is_cancel_matches_croatian_words():
    assert param_ui.is_cancel("odustani")
    assert param_ui.is_cancel("OTKAŽI")
    assert param_ui.is_cancel("  cancel  ")
    assert not param_ui.is_cancel("ne")  # 'ne' is negative, not cancel
    assert not param_ui.is_cancel("R123")


def test_is_negative_matches_skip_words():
    assert param_ui.is_negative("ne")
    assert param_ui.is_negative("ništa")
    assert param_ui.is_negative("PRESKOČI")
    assert not param_ui.is_negative("da")
    assert not param_ui.is_negative("R123")


# --------------------------------------------------------------------------
# parse_param_value — type coercion
# --------------------------------------------------------------------------

def test_parse_integer_strips_units():
    """Croatian users write '42.500 km' — parser strips non-digits."""
    assert param_ui.parse_param_value("42500", {"param_type": "integer"}) == 42500
    assert param_ui.parse_param_value("42.500 km", {"param_type": "integer"}) == 42500
    assert param_ui.parse_param_value("  100  ", {"param_type": "integer"}) == 100


def test_parse_integer_returns_none_for_no_digits():
    assert param_ui.parse_param_value("abc", {"param_type": "integer"}) is None
    assert param_ui.parse_param_value("", {"param_type": "integer"}) is None


def test_parse_integer_decimal_comma_reasks():
    """'12,5' for an integer field is a DECIMAL → re-ask (None), not a silent
    125 (Filip 2026-05-25). Dot stays a thousand-separator ('42.500'→42500)."""
    assert param_ui.parse_param_value("12,5", {"param_type": "integer"}) is None
    assert param_ui.parse_param_value("3,75", {"param_type": "integer"}) is None
    assert param_ui.parse_param_value("42.500", {"param_type": "integer"}) == 42500
    assert param_ui.parse_param_value("42500", {"param_type": "integer"}) == 42500


def test_parse_number_handles_croatian_decimal_comma():
    assert param_ui.parse_param_value("12,5", {"param_type": "number"}) == 12.5
    assert param_ui.parse_param_value("3.14", {"param_type": "number"}) == 3.14


def test_parse_boolean_accepts_da():
    assert param_ui.parse_param_value("da", {"param_type": "boolean"}) is True
    assert param_ui.parse_param_value("YES", {"param_type": "boolean"}) is True
    assert param_ui.parse_param_value("1", {"param_type": "boolean"}) is True
    assert param_ui.parse_param_value("ne", {"param_type": "boolean"}) is False
    assert param_ui.parse_param_value("false", {"param_type": "boolean"}) is False


def test_parse_string_returns_raw():
    assert param_ui.parse_param_value("R-12345", {"param_type": "string"}) == "R-12345"


# ---- Faza 4 (Filip 2026-05-17): date-time params parsed to ISO 8601 ----


def test_parse_date_time_explicit_dmy():
    """17.05.2026 09:00 → 2026-05-17T09:00:00"""
    out = param_ui.parse_param_value(
        "17.05.2026 09:00",
        {"param_type": "string", "format": "date-time"},
    )
    assert out == "2026-05-17T09:00:00"


def test_parse_date_only_format():
    """format=date → no time component."""
    out = param_ui.parse_param_value(
        "31.12.2027",
        {"param_type": "string", "format": "date"},
    )
    assert out == "2027-12-31"


def test_parse_unparseable_date_falls_back_to_raw():
    """If parser can't make sense, return raw string so API can validate."""
    out = param_ui.parse_param_value(
        "neka cudna rijec bez datuma",
        {"param_type": "string", "format": "date-time"},
    )
    assert out == "neka cudna rijec bez datuma"


def test_parse_string_without_date_format_passes_through():
    """Plain string params (no format=date-time) should NOT be reformatted."""
    out = param_ui.parse_param_value(
        "17.05.2026 09:00", {"param_type": "string"},
    )
    assert out == "17.05.2026 09:00"


# ---- parse_datetime_hr direct tests ----


def test_parse_datetime_hr_explicit_dmy_with_time():
    assert param_ui.parse_datetime_hr("17.05.2026 09:00") == "2026-05-17T09:00:00"
    assert param_ui.parse_datetime_hr("31.12.2027 14:30") == "2027-12-31T14:30:00"


def test_parse_datetime_hr_explicit_dmy_no_time():
    """Date only → midnight."""
    assert param_ui.parse_datetime_hr("17.05.2026") == "2026-05-17T00:00:00"


def test_parse_datetime_hr_date_only_mode():
    """want_time=False → ISO date format."""
    assert param_ui.parse_datetime_hr("17.05.2026", want_time=False) == "2026-05-17"
    # Time portion ignored
    assert param_ui.parse_datetime_hr("17.05.2026 14:00", want_time=False) == "2026-05-17"


def test_parse_datetime_hr_relative_words():
    """'sutra 9:00', 'danas 14:30', 'prekosutra 8'."""
    from datetime import datetime, timedelta
    today = datetime.now().date()
    sutra = today + timedelta(days=1)
    prekosutra = today + timedelta(days=2)

    out = param_ui.parse_datetime_hr("sutra 9:00")
    assert out is not None
    assert out.startswith(sutra.isoformat())
    assert "09:00:00" in out

    out = param_ui.parse_datetime_hr("danas 14:30")
    assert out is not None
    assert out.startswith(today.isoformat())
    assert "14:30:00" in out

    out = param_ui.parse_datetime_hr("prekosutra 8")
    assert out is not None
    assert out.startswith(prekosutra.isoformat())
    assert "08:00:00" in out


def test_parse_datetime_hr_relative_without_time_uses_midnight():
    from datetime import datetime, timedelta
    sutra = datetime.now().date() + timedelta(days=1)
    out = param_ui.parse_datetime_hr("sutra")
    assert out is not None
    assert out.startswith(sutra.isoformat())
    assert "00:00:00" in out


def test_parse_datetime_hr_returns_none_for_no_date_hint():
    """No relative word and no explicit DMY → None (caller falls back to raw)."""
    assert param_ui.parse_datetime_hr("nešto bez datuma") is None
    assert param_ui.parse_datetime_hr("") is None


def test_parse_datetime_hr_rejects_impossible_dates():
    """Invalid date components return None (e.g., 32.13.2026)."""
    assert param_ui.parse_datetime_hr("32.05.2026") is None
    assert param_ui.parse_datetime_hr("17.13.2026") is None


def test_parse_empty_returns_none():
    assert param_ui.parse_param_value("", {"param_type": "string"}) is None
    assert param_ui.parse_param_value("   ", {"param_type": "string"}) is None


def test_parse_no_param_def_returns_raw():
    """Defensive: missing param_def should fall back to raw text."""
    assert param_ui.parse_param_value("hello", None) == "hello"


# ---- NALAZ 3/4/5 (Filip 2026-05-25): HR number separators + weekday/parts ----


def test_parse_number_hr_thousands_and_decimal():
    """Dot=thousands + comma=decimal when both present; multiple dots → re-ask;
    a lone dot with 3 trailing digits is ambiguous (HR thousands) → re-ask;
    2-digit decimal stays."""
    assert param_ui.parse_param_value("1.500,75", {"param_type": "number"}) == 1500.75
    assert param_ui.parse_param_value("3.14", {"param_type": "number"}) == 3.14
    assert param_ui.parse_param_value("12,5", {"param_type": "number"}) == 12.5
    assert param_ui.parse_param_value("1.500.000", {"param_type": "number"}) is None
    # U1 (Filip 2026-05-26): "1.500" (lone dot, 3 digits) is ambiguous → re-ask
    assert param_ui.parse_param_value("1.500", {"param_type": "number"}) is None


def test_parse_integer_multiple_numbers_reasks():
    """U3 (Filip 2026-05-26): a comma-separated second number ('5, 30000') must
    not be merged into one integer (→530000) — re-ask instead. Dot thousands
    ('42.500'→42500) unaffected."""
    assert param_ui.parse_param_value("5, 30000", {"param_type": "integer"}) is None
    assert param_ui.parse_param_value("12,5", {"param_type": "integer"}) is None
    assert param_ui.parse_param_value("42.500", {"param_type": "integer"}) == 42500
    assert param_ui.parse_param_value("42500", {"param_type": "integer"}) == 42500


def test_parse_datetime_iso_passthrough():
    """Already-ISO input is normalized + returned (so coercion keeps LLM ISO)."""
    assert param_ui.parse_datetime_hr("2026-05-17T09:00:00") == "2026-05-17T09:00:00"
    assert param_ui.parse_datetime_hr("2026-05-17") == "2026-05-17T00:00:00"
    assert param_ui.parse_datetime_hr("2026-05-17", want_time=False) == "2026-05-17"


def test_parse_datetime_weekday_resolves_to_future():
    """'u petak' → the upcoming Friday (deterministic vs real 'today')."""
    today = datetime.now(param_ui._ZAGREB).date()
    out = param_ui.parse_datetime_hr("u petak", want_time=False)
    d = datetime.fromisoformat(out).date()
    assert d.weekday() == 4          # Friday
    assert today < d <= today + timedelta(days=7)


def test_parse_datetime_parts_of_day():
    """'popodne' → 13:00 default; 'u 2 popodne' → 14:00 (PM shift)."""
    assert param_ui.parse_datetime_hr("danas popodne").endswith("T13:00:00")
    assert param_ui.parse_datetime_hr("danas u 2 popodne").endswith("T14:00:00")
    assert param_ui.parse_datetime_hr("danas ujutro").endswith("T09:00:00")
