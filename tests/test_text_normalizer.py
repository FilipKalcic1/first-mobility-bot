"""Tests for text_normalizer — diacritics, synonyms, and query preprocessing."""

import pytest

from services.text_normalizer import (
    CROATIAN_STOPWORDS,
    clean_european_number,
    extract_query_patterns,
    extract_tool_from_text,
    normalize_diacritics,
    normalize_synonyms,
    sanitize_for_llm,
)


class TestSanitizeForLlm:
    def test_removes_carriage_return(self):
        assert sanitize_for_llm("hello\r\nworld") == "hello  world"

    def test_removes_newline(self):
        assert sanitize_for_llm("line1\nline2") == "line1 line2"

    def test_replaces_double_quotes(self):
        assert sanitize_for_llm('say "hello"') == "say 'hello'"

    def test_truncates_to_max_len(self):
        result = sanitize_for_llm("a" * 1000)
        assert len(result) == 500

    def test_custom_max_len(self):
        result = sanitize_for_llm("abcdefgh", max_len=5)
        assert result == "abcde"

    def test_none_returns_empty(self):
        assert sanitize_for_llm(None) == ""

    def test_empty_returns_empty(self):
        assert sanitize_for_llm("") == ""

    def test_strips_control_characters(self):
        assert sanitize_for_llm("hello\x00world\x1f") == "helloworld"

    def test_strips_zero_width_and_rtl_override(self):
        assert sanitize_for_llm("test\u200bhidden\u202eflip") == "testhiddenflip"

    def test_replaces_braces_to_prevent_json_injection(self):
        result = sanitize_for_llm('{"system": "ignore"}')
        assert "{" not in result
        assert "}" not in result
        assert result == "('system': 'ignore')"

    def test_replaces_brackets(self):
        result = sanitize_for_llm("[SYSTEM] new instructions")
        assert "[" not in result
        assert result == "(SYSTEM) new instructions"


class TestCleanEuropeanNumber:
    def test_dot_thousands(self):
        assert clean_european_number("45.000") == 45000

    def test_multiple_dot_separators(self):
        assert clean_european_number("1.234.567") == 1234567

    def test_comma_thousands(self):
        assert clean_european_number("45,000") == 45000

    def test_plain_number(self):
        assert clean_european_number("12345") == 12345

    def test_no_number_returns_none(self):
        assert clean_european_number("nema broja") is None

    def test_number_in_sentence(self):
        assert clean_european_number("Kilometraža je 120.000 km") == 120000

    def test_small_number_with_dot_stripped_to_integer(self):
        # "3.14" — dot followed by only 2 digits, not a thousands separator.
        # Function returns the integer part (3) because mileage values must be integers.
        assert clean_european_number("3.14") == 3


class TestExtractToolFromText:
    def test_get_prefix(self):
        assert extract_tool_from_text("koristi get_vehicles za popis") == "get_vehicles"

    def test_post_prefix(self):
        assert extract_tool_from_text("trebao post_mileage_entry") == "post_mileage_entry"

    def test_put_prefix(self):
        assert extract_tool_from_text("pozovi put_vehicle_update") == "put_vehicle_update"

    def test_delete_prefix(self):
        assert extract_tool_from_text("delete_record iz baze") == "delete_record"

    def test_patch_prefix(self):
        assert extract_tool_from_text("patch_user_data treba") == "patch_user_data"

    def test_no_tool_returns_none(self):
        assert extract_tool_from_text("nema alata ovdje") is None

    def test_empty_returns_none(self):
        assert extract_tool_from_text("") is None

    def test_none_returns_none(self):
        assert extract_tool_from_text(None) is None

    def test_case_insensitive(self):
        assert extract_tool_from_text("GET_VEHICLES popis") == "get_vehicles"

    def test_croatian_koristiti_pattern(self):
        result = extract_tool_from_text("koristiti get_vehicles za dohvat")
        assert result == "get_vehicles"

    def test_croatian_trebao_pattern(self):
        result = extract_tool_from_text("trebao post_mileage_entry za unos")
        assert result == "post_mileage_entry"


class TestExtractQueryPatterns:
    def test_basic_extraction(self):
        patterns = extract_query_patterns("pokaži mi sva vozila u Zagrebu")
        assert len(patterns) > 0
        # stopwords removed, meaningful words remain
        assert any("vozila" in p for p in patterns)
        assert any("zagrebu" in p for p in patterns)

    def test_empty_returns_empty(self):
        assert extract_query_patterns("") == []

    def test_max_patterns_limit(self):
        long_query = "ovo je jedan dugačak upit s mnogo raznih riječi za testiranje"
        patterns = extract_query_patterns(long_query, max_patterns=3)
        assert len(patterns) <= 3

    def test_stopwords_excluded(self):
        patterns = extract_query_patterns("ja sam u gradu")
        # "ja", "sam", "u" are stopwords; "gradu" is 5 chars so it stays
        for p in patterns:
            assert "ja" not in p.split()
            assert "sam" not in p.split()

    def test_min_word_len_filter(self):
        # Words shorter than min_word_len (default 3) are excluded
        patterns = extract_query_patterns("ab cd efghij klmnop")
        for p in patterns:
            for word in p.split():
                assert len(word) >= 3


class TestCroatianStopwords:
    def test_is_frozenset(self):
        assert isinstance(CROATIAN_STOPWORDS, frozenset)

    def test_contains_common_words(self):
        expected = {"ja", "je", "da", "ne", "su", "mi", "se", "za", "na", "u"}
        assert expected.issubset(CROATIAN_STOPWORDS)

    def test_contains_bot_specific_words(self):
        assert "molim" in CROATIAN_STOPWORDS
        assert "hvala" in CROATIAN_STOPWORDS
        assert "pokaži" in CROATIAN_STOPWORDS

    def test_not_empty(self):
        assert len(CROATIAN_STOPWORDS) > 50


class TestNormalizeDiacritics:
    def test_lowercase_diacritics(self):
        assert normalize_diacritics("čćđšž") == "ccdsz"

    def test_uppercase_diacritics(self):
        assert normalize_diacritics("ČĆĐŠŽ") == "CCDSZ"

    def test_mixed_text(self):
        assert normalize_diacritics("Šibenik čeka đake") == "Sibenik ceka dake"

    def test_no_diacritics_unchanged(self):
        assert normalize_diacritics("hello world") == "hello world"

    def test_empty_string(self):
        assert normalize_diacritics("") == ""


class TestNormalizeSynonyms:
    def test_auto_to_vozilo(self):
        assert normalize_synonyms("prikaži mi auto") == "prikaži mi vozilo"

    def test_auta_to_vozila(self):
        assert normalize_synonyms("prikaži mi sva auta") == "prikaži mi sva vozila"

    def test_mobitel_to_telefon(self):
        assert normalize_synonyms("daj mi mobitel") == "daj mi telefon"

    def test_km_to_kilometara(self):
        assert normalize_synonyms("koliko km") == "koliko kilometara"

    def test_typo_correction(self):
        assert normalize_synonyms("telfon ne radi") == "telefon ne radi"

    def test_no_synonyms_unchanged(self):
        assert normalize_synonyms("ovo je test") == "ovo je test"

    def test_preserves_non_synonym_words(self):
        result = normalize_synonyms("moj auto je plavi")
        assert result == "moj vozilo je plavi"
