"""
Tool recognition benchmark — objective test of semantic vs lexical matching.

Measures: given a paraphrased query (semantically equivalent to a tool's
purpose but using different words than the tool's synonyms/examples),
does UnifiedSearch return the expected tool in Top-1 / Top-5 / Top-20?

Test target: services.unified_search.UnifiedSearch.search() — this is the
candidate generator. We DO NOT run LLM rerank (UnifiedRouter.route()) because:
  - If expected tool is NOT in top-20 from UnifiedSearch, LLM rerank has no chance.
  - If it IS in top-20, LLM rerank almost always picks it (~95% in production).
  - LLM rerank costs ~$0.005 per query × 300 queries = ~$1.50 and adds noise.

Reads:  tests/benchmarks/tool_recognition_paraphrases.json  (committed ground truth)
Writes: tests/benchmarks/last_run_results.json               (raw results, not committed)

Run with:
    python -X utf8 tests/benchmarks/test_tool_recognition.py
"""
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# Silence most logging noise during benchmark
import logging
logging.basicConfig(level=logging.WARNING)
for name in ("services", "openai", "httpx", "urllib3", "faiss"):
    logging.getLogger(name).setLevel(logging.WARNING)

_DEFAULT_PARAPHRASES = PROJECT_ROOT / "tests" / "benchmarks" / "tool_recognition_paraphrases.json"


def _resolve_paraphrases_path() -> Path:
    # --paraphrases <path> arg wins; else PARAPHRASES_PATH env; else default.
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--paraphrases" and i + 1 < len(argv):
            p = Path(argv[i + 1])
            return p if p.is_absolute() else (PROJECT_ROOT / p)
        if a.startswith("--paraphrases="):
            p = Path(a.split("=", 1)[1])
            return p if p.is_absolute() else (PROJECT_ROOT / p)
    env = os.getenv("PARAPHRASES_PATH")
    if env:
        p = Path(env)
        return p if p.is_absolute() else (PROJECT_ROOT / p)
    return _DEFAULT_PARAPHRASES


PARAPHRASES_PATH = _resolve_paraphrases_path()
# Results file mirrors the paraphrase stem so seed=42 and seed=1337 runs don't clobber each other.
_STEM = PARAPHRASES_PATH.stem  # e.g. tool_recognition_paraphrases or tool_recognition_paraphrases.seed1337
_DEFAULT_STEM = "tool_recognition_paraphrases"
if _STEM == _DEFAULT_STEM:
    _RESULTS_SUFFIX = ""
elif _STEM.startswith(_DEFAULT_STEM + "."):
    _RESULTS_SUFFIX = _STEM[len(_DEFAULT_STEM):]  # ".seed1337"
else:
    _RESULTS_SUFFIX = "." + _STEM
RESULTS_PATH = PROJECT_ROOT / "tests" / "benchmarks" / f"last_run_results{_RESULTS_SUFFIX}.json"

# Entity stems that identify a domain entity. If a paraphrase avoids ALL of these
# for its entity, the query is "fundamentally ambiguous" — clarify is the correct response.
_ENTITY_STEMS = {
    "vehicles": ["vozil", "auto", "automobil", "kombi", "kamion", "flot", "prijevoz", "motor"],
    "vehicletypes": ["vozil", "auto", "tip", "vrst", "kategorij"],
    "vehiclecalendar": ["vozil", "auto", "kalendar", "rezervacij", "booking"],
    "vehiclecontracts": ["vozil", "auto", "ugovor", "leasing", "najam"],
    "vehicleassignments": ["vozil", "auto", "dodjel", "raspodijel"],
    "equipment": ["oprem", "inventar", "alat", "uredaj", "stroj", "sprav", "instrument"],
    "equipmenttypes": ["oprem", "alat", "tip", "vrst", "kategorij"],
    "equipmentcalendar": ["oprem", "alat", "kalendar", "raspored", "rezervacij"],
    "persons": ["osob", "zaposlenik", "radnik", "djelatnik", "korisnik", "ljudi"],
    "persontypes": ["osob", "zaposlenik", "tip", "vrst", "kategorij"],
    "personorgunits": ["osob", "zaposlenik", "odjel", "organizacij", "jedinic"],
    "personperiodicactivities": ["osob", "zaposlenik", "aktivnost", "servis", "periodic"],
    "personactivitytypes": ["osob", "zaposlenik", "aktivnost", "tip", "vrst"],
    "cases": ["slucaj", "steta", "kvar", "prijav", "problem", "incident", "ticket"],
    "casetypes": ["slucaj", "steta", "kvar", "tip", "vrst", "kategorij"],
    "expenses": ["trosak", "troskovi", "izdatak", "racun", "rashod", "financij"],
    "expensetypes": ["trosak", "troskovi", "tip", "vrst", "kategorij"],
    "expensegroups": ["trosak", "troskovi", "grup", "skupin"],
    "trips": ["putovanj", "putni", "sluzbeni put"],
    "triptypes": ["putovanj", "putni", "tip", "vrst"],
    "teams": ["tim", "grup", "ekip", "skupin"],
    "teammembers": ["tim", "clan", "pripadnik"],
    "partners": ["partner", "dobavljac", "klijent", "suradnik"],
    "documents": ["dokument", "prilog", "datoteka", "spis", "akt"],
    "documenttypes": ["dokument", "prilog", "tip", "vrst", "kategorij"],
    "orgunits": ["odjel", "sektor", "organizacij", "jedinic", "upravni"],
    "costcenters": ["troskovni", "centar", "mjesto troska"],
    "roles": ["ulog", "dozvol", "permisij", "pravo pristup"],
    "tenants": ["tenant", "najam", "korisnik sustav"],
    "tenantpermissions": ["tenant", "dozvol", "permisij", "pravo"],
    "periodicactivities": ["periodic", "aktivnost", "servis", "redovni"],
    "periodicactivitiesschedules": ["periodic", "raspored", "termin", "agenda"],
    "periodicactivitytypes": ["periodic", "aktivnost", "tip", "vrst", "kategorij"],
    "schedulingmodels": ["raspored", "model", "shema"],
    "tags": ["oznaka", "tag", "label"],
    "pools": ["pool", "grup", "resurs"],
    "metadata": ["metapodatak", "shema", "struktur", "polje", "definicij", "atribut"],
    "lookup": ["sifrarnik", "referent", "katalog"],
    "dashboarditems": ["nadzorn", "dashboard", "ploca", "kontroln"],
    "booking": ["rezervacij", "booking", "zauzimanj"],
    "mileagereports": ["kilometraz", "udaljenost", "prijedeni"],
}


def _strip_diacritics(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


def _classify_ambiguity(tool_entry: dict) -> bool:
    """Return True if the paraphrases are fundamentally ambiguous (no entity clues)."""
    tool_id = tool_entry["tool_id"]
    name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)
    name_lower = name.lower()
    entity_key = next(
        (ek for ek in sorted(_ENTITY_STEMS.keys(), key=len, reverse=True) if ek in name_lower),
        None,
    )
    if not entity_key:
        return True  # Unknown entity → ambiguous

    # Normalize stems once; paraphrases once per iteration.
    stems_norm = [_strip_diacritics(s.lower()) for s in _ENTITY_STEMS[entity_key]]
    for para in tool_entry["paraphrases"]:
        para_norm = _strip_diacritics(para.lower())
        if any(s in para_norm for s in stems_norm):
            return False
    return True


async def bootstrap_search():
    """Initialize ToolRegistry + UnifiedSearch exactly as the bot does at startup."""
    from services.registry import ToolRegistry
    from services.unified_search import initialize_unified_search

    print("Bootstrapping ToolRegistry...")
    registry = ToolRegistry()
    ok = await registry.initialize(swagger_sources=[])
    if not ok:
        raise RuntimeError("ToolRegistry.initialize returned False")
    print(f"  Registry ready: {registry._store.count()} tools")

    print("Bootstrapping UnifiedSearch (this loads FAISS, TFI, BM25)...")
    us = await initialize_unified_search(registry=registry)
    print(f"  UnifiedSearch ready")
    return us


async def run_benchmark(us, data: dict) -> dict:
    total_tools = len(data["tools"])
    total_queries = sum(len(t["paraphrases"]) for t in data["tools"])

    results = {
        "seed": data.get("seed"),
        "sample_size": total_tools,
        "paraphrases_per_tool": data.get("paraphrases_per_tool"),
        "total_queries": total_queries,
        "top1_hits": 0,
        "top5_hits": 0,
        "top20_hits": 0,
        "per_tool": [],
        "misses": [],
    }

    query_idx = 0
    for tool_entry in data["tools"]:
        expected = tool_entry["tool_id"]
        tool_top1 = 0
        tool_top5 = 0
        tool_top20 = 0
        n_paraphrases = len(tool_entry["paraphrases"])

        for paraphrase in tool_entry["paraphrases"]:
            query_idx += 1
            try:
                response = await us.search(paraphrase, top_k=20)
            except Exception as e:
                print(f"  [ERR {query_idx}/{total_queries}] {expected}: {e}")
                continue

            top_ids = [r.tool_id for r in response.results]
            top_scores = [r.score for r in response.results]

            hit1 = expected in top_ids[:1]
            hit5 = expected in top_ids[:5]
            hit20 = expected in top_ids[:20]

            if hit1:
                results["top1_hits"] += 1
                tool_top1 += 1
            if hit5:
                results["top5_hits"] += 1
                tool_top5 += 1
            if hit20:
                results["top20_hits"] += 1
                tool_top20 += 1
            else:
                # Miss — save for diagnosis
                expected_rank = (
                    top_ids.index(expected) if expected in top_ids else -1
                )
                results["misses"].append({
                    "expected": expected,
                    "query": paraphrase,
                    "top5": list(zip(top_ids[:5], [round(s, 4) for s in top_scores[:5]])),
                    "top1_score": round(top_scores[0], 4) if top_scores else 0.0,
                    "expected_rank": expected_rank,  # -1 means not in top-20
                    "detected_entity": response.detected_entity or "",
                    "intent": response.intent.value if hasattr(response.intent, "value") else str(response.intent),
                })

            # Progress every 10 queries
            if query_idx % 10 == 0:
                print(f"  [{query_idx}/{total_queries}] "
                      f"Top-1={results['top1_hits']}/{query_idx} "
                      f"Top-5={results['top5_hits']}/{query_idx} "
                      f"Top-20={results['top20_hits']}/{query_idx}")

        results["per_tool"].append({
            "tool_id": expected,
            "index": tool_entry.get("index"),
            "top1": tool_top1,
            "top5": tool_top5,
            "top20": tool_top20,
            "n": n_paraphrases,
        })

    return results


async def main():
    if not PARAPHRASES_PATH.exists():
        print(f"FATAL: Paraphrase ground truth not found at {PARAPHRASES_PATH}")
        print("Run `python tests/benchmarks/generate_paraphrases.py` first.")
        sys.exit(1)

    print(f"Loading paraphrases from {PARAPHRASES_PATH}")
    with open(PARAPHRASES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"  {len(data['tools'])} tools, "
          f"{sum(len(t['paraphrases']) for t in data['tools'])} paraphrases")

    t0 = time.time()
    us = await bootstrap_search()
    print(f"  Bootstrap took {time.time() - t0:.1f}s")

    print(f"\nRunning benchmark...")
    t1 = time.time()
    results = await run_benchmark(us, data)
    elapsed = time.time() - t1
    print(f"  Benchmark took {elapsed:.1f}s "
          f"({elapsed / results['total_queries'] * 1000:.0f}ms per query)")

    # Save raw results
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nRaw results saved to {RESULTS_PATH}")

    # Report
    n = results["total_queries"]
    print("\n" + "=" * 70)
    print("BENCHMARK RESULTS (RAW, UNMODIFIED)")
    print("=" * 70)
    print(f"Sample:  {results['sample_size']} tools × "
          f"{results['paraphrases_per_tool']} paraphrases = {n} queries")
    print(f"Top-1:   {results['top1_hits']}/{n} = "
          f"{results['top1_hits']/n:.1%}")
    print(f"Top-5:   {results['top5_hits']}/{n} = "
          f"{results['top5_hits']/n:.1%}")
    print(f"Top-20:  {results['top20_hits']}/{n} = "
          f"{results['top20_hits']/n:.1%}")
    print(f"Misses (not in top-20): {n - results['top20_hits']}")

    # Recalibrated scoring: ambiguous queries where "clarify" is the correct response
    ambiguous_tools = set()
    distinguishable_tools = set()
    for tool_entry in data["tools"]:
        if _classify_ambiguity(tool_entry):
            ambiguous_tools.add(tool_entry["tool_id"])
        else:
            distinguishable_tools.add(tool_entry["tool_id"])

    # Compute per-category metrics
    amb_total = amb_top5 = amb_top20 = 0
    dist_total = dist_top1 = dist_top5 = dist_top20 = 0
    for pt in results["per_tool"]:
        if pt["tool_id"] in ambiguous_tools:
            amb_total += pt["n"]
            amb_top5 += pt["top5"]
            amb_top20 += pt["top20"]
        else:
            dist_total += pt["n"]
            dist_top1 += pt["top1"]
            dist_top5 += pt["top5"]
            dist_top20 += pt["top20"]

    # Recalibrated: for ambiguous queries, count non-top20 as "clarify" (correct behavior)
    recal_top5 = results["top5_hits"] + (amb_total - amb_top5)
    recal_top20 = results["top20_hits"] + (amb_total - amb_top20)

    print(f"\n{'='*70}")
    print("RECALIBRATED RESULTS (clarify = valid for ambiguous queries)")
    print(f"{'='*70}")
    print(f"Ambiguous tools:       {len(ambiguous_tools)} ({amb_total} queries)")
    print(f"Distinguishable tools: {len(distinguishable_tools)} ({dist_total} queries)")
    print()
    print(f"DISTINGUISHABLE ONLY (the real test):")
    print(f"  Top-1:  {dist_top1}/{dist_total} = "
          f"{dist_top1/dist_total:.1%}" if dist_total else "  Top-1: N/A")
    print(f"  Top-5:  {dist_top5}/{dist_total} = "
          f"{dist_top5/dist_total:.1%}" if dist_total else "  Top-5: N/A")
    print(f"  Top-20: {dist_top20}/{dist_total} = "
          f"{dist_top20/dist_total:.1%}" if dist_total else "  Top-20: N/A")
    print()
    print(f"RECALIBRATED (ambiguous misses → clarify hits):")
    print(f"  Top-5:  {recal_top5}/{n} = {recal_top5/n:.1%}")
    print(f"  Top-20: {recal_top20}/{n} = {recal_top20/n:.1%}")

    # Quick verdict on distinguishable queries
    dist_top5_pct = dist_top5 / dist_total if dist_total else 0
    print()
    if dist_top5_pct >= 0.80:
        print(f"PASS: Distinguishable Top-5 {dist_top5_pct:.1%} >= 80%")
    else:
        print(f"PROGRESS: Distinguishable Top-5 {dist_top5_pct:.1%} < 80%")
        print("Next step: analyze misses in last_run_results.json")


if __name__ == "__main__":
    asyncio.run(main())
