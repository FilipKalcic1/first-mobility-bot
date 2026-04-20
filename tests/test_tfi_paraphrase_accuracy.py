"""
TFI Pipeline Paraphrase Accuracy Test

Tests the full deterministic routing pipeline:
  query -> entity_detection -> method_detection -> query_type -> TFI -> tool_id

Three tiers:
  1. PRODUCTION - How real Croatian users type on WhatsApp
  2. PARAPHRASE - Same intent, different wording
  3. ADVERSARIAL - No obvious keywords (expected FAISS territory)
"""

import json
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.entity_detector import detect_entity
from services.tool_family_index import ToolFamilyIndex
from services.types.query_type import QueryType
from services.text_normalizer import normalize_diacritics
from services.config_loader import load_json

_VERB_METHOD_MAP = load_json("domain", "http_verbs_hr.json")["verb_to_method"]
_SUFFIX_STEMS = load_json("per_tool", "suffix_boosts.json")["by_suffix"]


def detect_method(query: str):
    q = normalize_diacritics(query.lower())
    for verb, method in _VERB_METHOD_MAP.items():
        if verb in q:
            return method
    return None


def detect_query_type(query: str, method):
    q = normalize_diacritics(query.lower())
    if method == "delete":
        criteria = ["kriterij", "uvjet", "po filtr", "bulk", "masovno",
                     "vise zapisa", "prema filter", "prema kriterij"]
        if any(s in q for s in criteria):
            return QueryType.DELETE_CRITERIA
    suffix_map = {
        "_agg": QueryType.AGGREGATION,
        "_groupby": QueryType.AGGREGATION,
        "_projectto": QueryType.PROJECTION,
        "_multipatch": QueryType.BULK_UPDATE,
    }
    for sfx_key, stems_data in _SUFFIX_STEMS.items():
        if any(s in q for s in stems_data["keywords"]):
            mapped = suffix_map.get(sfx_key)
            if mapped:
                return mapped
    return QueryType.LIST


# ---------------------------------------------------------------------------
# Test queries — format: (query, expected_tool, tier)
# expected=None means TFI can't resolve (FAISS territory)
# ---------------------------------------------------------------------------

TEST_QUERIES = [
    # =========================================================================
    # PRODUCTION — Real WhatsApp messages (60 queries)
    # =========================================================================

    # GET list
    ("Pokazi mi sva vozila", "get_Vehicles", "production"),
    ("Daj mi popis troskova", "get_Expenses", "production"),
    ("Koji zaposlenici postoje?", "get_Persons", "production"),
    ("Prikazi putovanja", "get_Trips", "production"),
    ("Pokazi opremu", "get_Equipment", "production"),
    ("Daj mi partnere", "get_Partners", "production"),
    ("Prikazi timove", "get_Teams", "production"),
    ("Pokazi slucajeve", "get_Cases", "production"),
    ("Daj ugovore o vozilima", "get_VehicleContracts", "production"),
    ("Prikazi organizacijske jedinice", "get_OrgUnits", "production"),
    ("Pokazi tipove vozila", "get_VehicleTypes", "production"),
    ("Daj tipove troskova", "get_ExpenseTypes", "production"),
    ("Prikazi tipove putovanja", "get_TripTypes", "production"),
    ("Pokazi kilometrazu", "get_MileageReports", "production"),
    ("Prikazi kalendar vozila", "get_VehicleCalendar", "production"),
    ("Daj kalendar opreme", "get_EquipmentCalendar", "production"),
    ("Prikazi tipove dokumenata", "get_DocumentTypes", "production"),
    ("Prikazi dashboard", "get_DashboardItems", "production"),
    ("Moj profil", "get_PersonData_personIdOrEmail", "production"),
    ("Rasporedi aktivnosti", "get_PeriodicActivitiesSchedules", "production"),
    ("Prikazi periodicne aktivnosti", "get_PeriodicActivities", "production"),
    ("Pokazi modele rasporeda", "get_SchedulingModels", "production"),
    ("Prikazi troskovne centre", "get_CostCenters", "production"),
    ("Daj grupe troskova", "get_ExpenseGroups", "production"),
    ("Prikazi tvrtke", "get_Companies", "production"),
    ("Clanovi tima", "get_TeamMembers", "production"),
    ("Tipovi slucajeva", "get_CaseTypes", "production"),
    ("Tipovi aktivnosti", "get_PersonActivityTypes", "production"),
    ("Pokazi role", "get_Roles", "production"),
    ("Prikazi tagove", "get_Tags", "production"),
    ("Pokazi pool vozila", "get_Pools", "production"),

    # POST create
    ("Dodaj novo vozilo", "post_Vehicles", "production"),
    ("Kreiraj trosak", "post_Expenses", "production"),
    ("Dodaj novog zaposlenika", "post_Persons", "production"),
    ("Napravi novo putovanje", "post_Trips", "production"),
    ("Dodaj novi tim", "post_Teams", "production"),
    ("Kreiraj novi slucaj", "post_Cases", "production"),
    ("Dodaj opremu", "post_Equipment", "production"),
    ("Unesi partnera", "post_Partners", "production"),

    # PUT update
    ("Azuriraj vozilo", "put_Vehicles_id", "production"),
    ("Izmijeni zaposlenika", "put_Persons_id", "production"),
    ("Promijeni trosak", "put_Expenses_id", "production"),
    ("Azuriraj putovanje", "put_Trips_id", "production"),

    # DELETE
    ("Obrisi trosak", "delete_Expenses", "production"),
    ("Izbrisi vozilo", "delete_Vehicles", "production"),
    ("Makni zaposlenika", "delete_Persons", "production"),
    ("Obrisi putovanje", "delete_Trips", "production"),

    # Aggregation / GroupBy
    ("Grupiraj troskove", "get_Expenses_GroupBy", "production"),
    ("Statistika troskova", "get_Expenses_GroupBy", "production"),
    ("Zbirni prikaz putovanja", "get_Trips_GroupBy", "production"),

    # Projection
    ("Projiciraj zaposlenike", "get_Persons_ProjectTo", "production"),
    ("Odabrana polja vozila", "get_Vehicles_ProjectTo", "production"),

    # Bulk
    ("Masovno azuriraj troskove", "post_Expenses_multipatch", "production"),
    ("Bulk azuriranje vozila", "post_Vehicles_multipatch", "production"),

    # Delete by criteria
    ("Obrisi troskove prema kriterijima", "delete_Expenses_DeleteByCriteria", "production"),
    ("Izbrisi vozila prema kriterijima", "delete_Vehicles_DeleteByCriteria", "production"),

    # =========================================================================
    # PARAPHRASE — Different wording (40 queries)
    # =========================================================================

    ("Koje aute imamo u floti?", "get_Vehicles", "paraphrase"),
    ("Trebam listu svih radnika", "get_Persons", "paraphrase"),
    ("Mogu li vidjeti racune?", "get_Expenses", "paraphrase"),
    ("Imam li kakvih voznji?", "get_Trips", "paraphrase"),
    ("Sto imamo od alata?", "get_Equipment", "paraphrase"),
    ("Zelim unijeti novi rashod", "post_Expenses", "paraphrase"),
    ("Trebam dodati djelatnika", "post_Persons", "paraphrase"),
    ("Zelim promijeniti auto", "put_Vehicles_id", "paraphrase"),
    ("Treba mi pregled kvarova", "get_Cases", "paraphrase"),
    ("Daj mi pregled leasinga", "get_VehicleContracts", "paraphrase"),
    ("Zelim vidjeti periodicne servise", "get_PeriodicActivities", "paraphrase"),
    ("Pokazi izvjestaje o kilometrazi", "get_MileageReports", "paraphrase"),
    ("Zelim izbaciti stari racun", "delete_Expenses", "paraphrase"),
    ("Makni to putovanje", "delete_Trips", "paraphrase"),
    ("Grupiraj zaposlenike po polju", "get_Persons_GroupBy", "paraphrase"),
    ("Trebam samo neka polja od troskova", "get_Expenses_ProjectTo", "paraphrase"),
    ("Grupno azuriraj zaposlenike", "post_Persons_multipatch", "paraphrase"),
    ("Prikazi kontrolnu plocu", "get_DashboardItems", "paraphrase"),
    ("Treba mi nadzorna ploca", "get_DashboardItems", "paraphrase"),
    ("Koji su tipovi steta?", "get_CaseTypes", "paraphrase"),
    ("Prikazi vrste aktivnosti", "get_PersonActivityTypes", "paraphrase"),
    ("Kakve tipove troskova imamo?", "get_ExpenseTypes", "paraphrase"),
    ("Moji podaci", "get_PersonData_personIdOrEmail", "paraphrase"),
    ("O meni", "get_PersonData_personIdOrEmail", "paraphrase"),
    ("Statistika vozila", "get_Vehicles_GroupBy", "paraphrase"),
    ("Trebam ugovore o vozilima", "get_VehicleContracts", "paraphrase"),
    ("Rezervacije vozila", "get_VehicleCalendar", "paraphrase"),
    ("Prikazi booking", "get_VehicleCalendar", "paraphrase"),
    ("Koliko sam presao kilometara", "get_MileageReports", "paraphrase"),
    ("Prijedeni put", "get_MileageReports", "paraphrase"),
    ("Daj firme", "get_Companies", "paraphrase"),
    ("Pokazi dobavljace", "get_Partners", "paraphrase"),
    ("Prikazi klijente", "get_Partners", "paraphrase"),
    ("Ukloni nepotrebnu stavku iz rashoda", "delete_Expenses", "paraphrase"),
    ("Trebam vidjeti clanove tima", "get_TeamMembers", "paraphrase"),
    ("Registriraj novi uredaj", "post_Equipment", "paraphrase"),
    ("Pokazi grupe troskova", "get_ExpenseGroups", "paraphrase"),
    ("Modele rasporeda", "get_SchedulingModels", "paraphrase"),
    ("Dodjele vozila", "post_VehicleAssignments", "paraphrase"),
    ("Pokazi tipove slucajeva", "get_CaseTypes", "paraphrase"),

    # =========================================================================
    # ADVERSARIAL — No entity keywords (FAISS territory, expected=None)
    # =========================================================================

    ("Koliko ima svega u voznom parku?", None, "adversarial"),
    ("Mogu li pogledati financijsku evidenciju?", None, "adversarial"),
    ("Tko radi kod nas?", None, "adversarial"),
    ("Prebaci me na pocetni zaslon", None, "adversarial"),
    ("Ocistiti odredene zapise iz sustava", None, "adversarial"),
    ("Trebam se rijesiti nekih stavki", None, "adversarial"),
    # These have partial keywords — TFI might resolve
    ("Zelim vidjeti sve uredaje", "get_Equipment", "adversarial"),
    ("Koji odjeli postoje?", "get_OrgUnits", "adversarial"),
]


@pytest.fixture(scope="module")
def tfi():
    doc_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "tool_documentation.json"
    )
    with open(doc_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    index = ToolFamilyIndex()
    index.build(list(data.keys()))
    return index


def _resolve(tfi_index, query):
    entity = detect_entity(query)
    method = detect_method(query)
    qt = detect_query_type(query, method) if entity else QueryType.UNKNOWN
    tool = tfi_index.resolve(entity, qt, method=method) if entity else None
    return tool, entity, method, qt


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------

_RESOLVABLE = [(q, e, t) for q, e, t in TEST_QUERIES if e is not None]
_FAISS_TERRITORY = [(q, e, t) for q, e, t in TEST_QUERIES if e is None]


@pytest.mark.parametrize(
    "query,expected_tool,tier",
    _RESOLVABLE,
    ids=[f"{t[2]}_{i:03d}" for i, t in enumerate(_RESOLVABLE)],
)
def test_tfi_resolves(tfi, query, expected_tool, tier):
    tool, entity, method, qt = _resolve(tfi, query)
    assert tool is not None, (
        f"TFI=None: '{query}' (entity={entity}, method={method}, qt={qt})"
    )
    assert tool.lower() == expected_tool.lower(), (
        f"Wrong: '{query}' -> {tool} (expected {expected_tool}) "
        f"[entity={entity}, method={method}, qt={qt}]"
    )


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def test_accuracy_summary(tfi, capsys):
    tiers = {"production": [], "paraphrase": [], "adversarial": []}

    for query, expected_tool, tier in TEST_QUERIES:
        tool, entity, method, qt = _resolve(tfi, query)
        if expected_tool is None:
            tiers[tier].append({
                "query": query, "expected": "FAISS", "got": tool or "FAISS",
                "correct": True, "entity": entity,
            })
        else:
            correct = tool is not None and tool.lower() == expected_tool.lower()
            tiers[tier].append({
                "query": query, "expected": expected_tool, "got": tool,
                "correct": correct, "entity": entity,
                "method": method, "qt": qt.value if qt else None,
            })

    with capsys.disabled():
        print("\n")
        print("=" * 72)
        print("  TFI PIPELINE PARAPHRASE ACCURACY REPORT")
        print("=" * 72)

        total_correct = 0
        total_queries = 0

        for tier_name in ["production", "paraphrase", "adversarial"]:
            items = tiers[tier_name]
            resolvable = [r for r in items if r["expected"] != "FAISS"]
            faiss_items = [r for r in items if r["expected"] == "FAISS"]

            correct = sum(1 for r in resolvable if r["correct"])
            total = len(resolvable)
            total_correct += correct
            total_queries += total
            pct = 100 * correct / total if total else 0

            print(f"\n  {tier_name.upper()} -- {correct}/{total} = {pct:.1f}%")

            failures = [r for r in resolvable if not r["correct"]]
            if failures:
                for f in failures:
                    got = f["got"] or "None"
                    print(f"    MISS: {f['query']}")
                    print(f"          got={got}, expected={f['expected']}")
                    print(f"          entity={f['entity']}, method={f.get('method')}, qt={f.get('qt')}")
            else:
                print(f"    ALL {total} CORRECT")

            if faiss_items:
                bonus = [r for r in faiss_items if r["got"] != "FAISS"]
                print(f"    ({len(faiss_items)} FAISS-territory, {len(bonus)} bonus TFI hits)")

        overall = 100 * total_correct / total_queries if total_queries else 0
        print(f"\n{'=' * 72}")
        print(f"  OVERALL (TFI-resolvable): {total_correct}/{total_queries} = {overall:.1f}%")
        print(f"  FAISS territory: {sum(1 for _, e, _ in TEST_QUERIES if e is None)} queries")
        print(f"  Total queries tested: {len(TEST_QUERIES)}")
        print(f"{'=' * 72}")

    # Thresholds
    prod = [r for r in tiers["production"] if r["expected"] != "FAISS"]
    prod_pct = sum(1 for r in prod if r["correct"]) / len(prod)
    para = [r for r in tiers["paraphrase"] if r["expected"] != "FAISS"]
    para_pct = sum(1 for r in para if r["correct"]) / len(para)

    assert prod_pct >= 0.90, f"Production accuracy {prod_pct:.1%} < 90%"
    assert para_pct >= 0.80, f"Paraphrase accuracy {para_pct:.1%} < 80%"
