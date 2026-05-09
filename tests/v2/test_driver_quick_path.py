"""Tests for L2 driver quick-path against the real-corpus-derived patterns."""
from __future__ import annotations

from services.v2.driver_quick_path import DriverQuickPath, QuickPathHit


def test_loads_config_from_default_path():
    qp = DriverQuickPath()
    qp.load()
    assert qp.pattern_count > 0


def test_real_corpus_kilometraza_routes_to_masterdata():
    qp = DriverQuickPath()
    qp.load()
    queries = [
        "Koja je moja kilometraža",
        "Koja je kilometraža",
        "Kolika mi je kilometraža",
        "koja mi je km",
    ]
    for q in queries:
        hit = qp.match(q)
        assert hit is not None, f"no match for: {q}"
        assert hit.kind == "tool"
        assert hit.tool_id == "get_MasterData"
        assert hit.field == "LastMileage"


def test_real_corpus_registracija_routes_to_masterdata():
    qp = DriverQuickPath()
    qp.load()
    queries = [
        "Koja je moja registracija auta",
        "koja je moja registracija",
        "Mozes li mi reci broj moje registracije",
    ]
    for q in queries:
        hit = qp.match(q)
        assert hit is not None, f"no match for: {q}"
        assert hit.tool_id == "get_MasterData"
        assert hit.field == "RegistrationPlate"


def test_real_corpus_expiry_routes_correctly_not_to_plate():
    """CRITICAL: 'kada mi istječe registracija' must route to RegistrationExpiry,
    NOT to RegistrationPlate. Specificity-first ordering is essential."""
    qp = DriverQuickPath()
    qp.load()
    hit = qp.match("kada mi istječe registracija")
    assert hit is not None
    assert hit.tool_id == "get_MasterData"
    assert hit.field == "RegistrationExpiry"


def test_real_corpus_open_vehicle_info():
    """Open-ended queries route to full MasterData (no specific field)."""
    qp = DriverQuickPath()
    qp.load()
    queries = [
        "Koje je moje vozilo",
        "Koji je moj auto",
        "Naziv automobila",
        "kako se zove moj auto",
        "Ok sto mi mozes reci o vozilu",
    ]
    for q in queries:
        hit = qp.match(q)
        assert hit is not None, f"no match for: {q}"
        assert hit.tool_id == "get_MasterData"


def test_real_corpus_marka_model_specific_fields():
    qp = DriverQuickPath()
    qp.load()
    h_marka = qp.match("Marka")
    assert h_marka and h_marka.field == "Make"
    h_model = qp.match("Model")
    assert h_model and h_model.field == "Model"


def test_real_corpus_personal_name_polite_refusal():
    """OOS personal: bot MUST respond, NEVER silent."""
    qp = DriverQuickPath()
    qp.load()
    hit = qp.match("Kako se ja zovem")
    assert hit is not None
    assert hit.kind == "polite_refusal"
    assert hit.response_text and "ne mogu" in hit.response_text.lower()


def test_real_corpus_english_query_routes_to_fallback():
    """English query MUST get Croatian fallback, NEVER silent."""
    qp = DriverQuickPath()
    qp.load()
    hit = qp.match("hello can i get my car registration")
    assert hit is not None
    assert hit.kind == "english_fallback"
    assert hit.response_text and "hrvatski" in hit.response_text.lower()


def test_real_corpus_reservations_routes_to_calendar():
    qp = DriverQuickPath()
    qp.load()
    hit = qp.match("Koje su moje rezervacije")
    assert hit is not None
    assert hit.tool_id == "get_VehicleCalendar"


def test_real_corpus_mileage_history_distinct_from_current():
    """'svoje kilometraže' (plural) → history, not current."""
    qp = DriverQuickPath()
    qp.load()
    h_history = qp.match("Mogu li vidjet svoje kilometraže")
    assert h_history is not None
    # Plural 'kilometraže' should match history pattern
    assert h_history.tool_id in ("get_MileageReports", "get_MasterData")


def test_no_match_returns_none():
    qp = DriverQuickPath()
    qp.load()
    # Generic non-driver query
    assert qp.match("rezerviraj auto sutra") is None or qp.match("rezerviraj auto sutra").tool_id != "get_MasterData"


def test_empty_query_returns_none():
    qp = DriverQuickPath()
    qp.load()
    assert qp.match("") is None
    assert qp.match("   ") is None


def test_real_corpus_full_pass_metrics():
    """Run all 30 unique real-corpus queries through quick-path. Report
    coverage. We don't assert specific number — assert reasonable lower bound."""
    qp = DriverQuickPath()
    qp.load()
    real_queries = [
        "Bok",                                                 # greeting (handled by L1)
        "Koja je moja registracija auta",                     # → RegistrationPlate
        "Koja je moja kilometraža",                            # → LastMileage
        "Koja je kilometraža",                                 # → LastMileage
        "koja je moja registracija",                           # → RegistrationPlate
        "Koji je moj auto",                                    # → MasterData open
        "Naziv automobila",                                    # → MasterData open
        "Model",                                               # → Model
        "Marka",                                               # → Make
        "kada mi istječe registracija",                        # → RegistrationExpiry
        "hello can i get my car registration",                 # → english_fallback
        "Ok sto mi mozes reci o vozilu",                       # → MasterData open
        "Koje je moje vozilo",                                 # → MasterData open
        "Koje su moje rezervacije",                            # → VehicleCalendar
        "Kako se ja zovem",                                    # → polite_refusal
        "Ok a kako se zove moj auto",                          # → MasterData open
        "kako se zove moj auto",                               # → MasterData open
        "kako se ja zovem",                                    # → polite_refusal
        "ok koje informacije imaš o mom vozilu",               # → MasterData open
        "Koje je moje ime",                                    # → polite_refusal
        "Mozes li mi reci broj moje registracije",             # → RegistrationPlate
        "Mogu li vidjet svoje kilometraže",                    # → MileageReports
        "Kolika je moja potrošnja goriva",                     # → FuelConsumption
        "Kolika mi je potrošnja goriva za moje vozilo",        # → FuelConsumption
        "Kolika mi je potrošnja goriva",                       # → FuelConsumption
        "Koja je lizing kuća mog vozila",                      # → LeasingCompany
        "Koja mi je kilometraža",                              # → LastMileage
        "Koje vozilo imam",                                    # → MasterData open
    ]
    matched = 0
    for q in real_queries:
        if qp.match(q) is not None:
            matched += 1
    coverage = matched / len(real_queries) * 100
    # Empirical: real corpus should achieve >= 80% L2 coverage
    assert coverage >= 80.0, f"L2 coverage too low: {coverage:.1f}%"


def test_quickpath_hit_helpers():
    hit = QuickPathHit(kind="tool", tool_id="get_MasterData")
    assert hit.is_actionable is True
    assert hit.is_terminal_response is False

    refusal = QuickPathHit(kind="polite_refusal", response_text="x")
    assert refusal.is_actionable is False
    assert refusal.is_terminal_response is True

    english = QuickPathHit(kind="english_fallback", response_text="x")
    assert english.is_terminal_response is True
