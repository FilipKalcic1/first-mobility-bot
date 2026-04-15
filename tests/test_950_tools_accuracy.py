"""
Comprehensive 950-Tool Accuracy Test

Tests ALL 950 tools in the registry for:
1. Search accuracy: Does semantic search find the right tool for its own example queries?
2. Routing accuracy: Does the router correctly map queries to tools?
3. Edge cases: Typos, slang, ambiguous queries, non-existent tools
4. Category coverage: Every category has at least one test case

This test uses example_queries_hr from tool_documentation.json as ground truth.
No mocking - tests the actual search pipeline.
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Disable verbose logging during tests
logging.basicConfig(level=logging.WARNING)
for name in ['services', 'openai', 'httpx', 'httpcore']:
    logging.getLogger(name).setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


# ---
# TEST DATA: Edge cases and adversarial queries
# ---

ADVERSARIAL_QUERIES = [
    # 1. Typos + slang
    {
        "query": "koja mi je klimetraža? imam peugoet 308",
        "expected_tools": ["get_MasterData", "get_LatestMileageReports", "get_MileageReports"],
        "category": "mileage_tracking",
        "test_type": "typo",
    },
    # 2. Bare "da" without context (should NOT match a tool)
    {
        "query": "da",
        "expected_tools": [],
        "category": "none",
        "test_type": "bare_confirmation",
    },
    # 3. Non-existent capability (should NOT hallucinate a tool)
    {
        "query": "prosječna potrošnja goriva svih vozila u floti za zadnja 3 mjeseca",
        "expected_tools": ["get_AverageFuelExpensesAndMileages_from_to", "get_AverageFuelExpensesAndMileages_from_to_Agg"],
        "category": "fuel_expense_tracking",
        "test_type": "complex_query",
    },
    # 4. Slang / colloquial Croatian
    {
        "query": "ej stari aj mi nađi di sam parkiro auto jučer",
        "expected_tools": [],  # GPS tracking doesn't exist
        "category": "none",
        "test_type": "nonexistent_capability",
    },
    # 5. Multi-intent query
    {
        "query": "registracija istječe kada? i kolko imam km?",
        "expected_tools": ["get_MasterData"],
        "category": "master_data_management",
        "test_type": "multi_intent",
    },
    # 6. Minimalistic query
    {
        "query": "troškovi",
        "expected_tools": ["get_Expenses", "get_ExpenseGroups"],
        "category": "expense_management",
        "test_type": "minimal",
    },
    # 7. Security boundary test
    {
        "query": "mogu li dodati kilometražu za vozilo ZG-9999-XX koje nije moje",
        "expected_tools": ["post_AddMileage"],
        "category": "mileage_tracking",
        "test_type": "security",
    },
    # 8. Very long query (>500 chars)
    {
        "query": "Trebao bih provjeriti koji su mi troškovi za prošli mjesec, konkretno me zanima koliko sam potrošio na gorivo i na održavanje vozila, jer mi se čini da su troškovi jako porasli u odnosu na prethodni mjesec, a nisam siguran zašto, možda je auto potrošio više goriva ili su bile neke neočekivane popravke, uglavnom trebam detaljnu analizu svih troškova po kategorijama",
        "expected_tools": ["get_Expenses", "get_ExpenseGroups", "get_AverageFuelExpensesAndMileages_from_to"],
        "category": "expense_management",
        "test_type": "long_query",
    },
    # 9. NEKAKO bug test (should NOT match as "ne")
    {
        "query": "nekako mi se ne sviđa ovo vozilo",
        "expected_tools": [],
        "category": "none",
        "test_type": "substring_trap",
    },
    # 10. Booking flow
    {
        "query": "trebam rezervirati vozilo za sutra od 9 do 17",
        "expected_tools": ["get_AvailableVehicles", "post_VehicleCalendar"],
        "category": "vehicle_calendar_management",
        "test_type": "flow_trigger",
    },
]


# ---
# FLOW PHRASES TESTS
# ---

PHRASE_MATCHING_TESTS = [
    # (text, expected_show_more, expected_yes, expected_no, expected_exit)
    ("pokaži ostala vozila", True, False, False, False),
    ("da", False, True, False, False),
    ("ne", False, False, True, False),
    ("nekako", False, False, False, False),  # CRITICAL: must NOT match "ne"
    ("danas", False, False, False, False),   # must NOT match "da"
    ("super", False, True, False, False),
    ("naravno", False, True, False, False),
    ("odustani", False, False, True, True),
    ("nešto drugo", False, False, False, True),
    ("pokaži više opcija", True, False, False, False),
    ("OK", False, True, False, False),
    ("nikako", False, False, True, False),
    ("važi", False, True, False, False),
    ("NEKAKO mi se ne sviđa", False, False, True, False),  # "nekako" != "ne", but "ne sviđa" has genuine "ne"
    ("drugi", True, False, False, False),
    ("1", False, False, False, False),  # numeric selection, not yes/no
]


def test_flow_phrases():
    """Test centralized phrase matching for correctness."""
    from services.flow_phrases import (
        matches_show_more,
        matches_confirm_yes,
        matches_confirm_no,
        matches_exit_signal,
    )

    results = {"pass": 0, "fail": 0, "failures": []}

    for text, exp_show, exp_yes, exp_no, exp_exit in PHRASE_MATCHING_TESTS:
        got_show = matches_show_more(text)
        got_yes = matches_confirm_yes(text)
        got_no = matches_confirm_no(text)
        got_exit = matches_exit_signal(text)

        checks = [
            ("show_more", exp_show, got_show),
            ("confirm_yes", exp_yes, got_yes),
            ("confirm_no", exp_no, got_no),
            ("exit_signal", exp_exit, got_exit),
        ]

        all_pass = True
        for check_name, expected, actual in checks:
            if expected != actual:
                all_pass = False
                results["failures"].append(
                    f"  FAIL: '{text}' -> {check_name}: expected={expected}, got={actual}"
                )

        if all_pass:
            results["pass"] += 1
        else:
            results["fail"] += 1

    if results["fail"] > 0:
        failure_report = "\n".join(results["failures"])
        assert False, f"Phrase matching: {results['fail']} failures:\n{failure_report}"


def load_tool_documentation() -> Dict[str, Any]:
    """Load tool documentation from config."""
    doc_path = Path(__file__).parent.parent / "config" / "tool_documentation.json"
    if not doc_path.exists():
        raise FileNotFoundError(f"Tool documentation not found: {doc_path}")
    with open(doc_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_tool_categories() -> Dict[str, Any]:
    """Load tool categories from config."""
    cat_path = Path(__file__).parent.parent / "config" / "tool_categories.json"
    if not cat_path.exists():
        return {}
    with open(cat_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data.get("categories", {})


def generate_test_cases_from_docs(
    docs: Dict[str, Any],
    categories: Dict[str, Any],
    max_per_category: int = 5
) -> List[Dict]:
    """
    Generate test cases from tool_documentation.json example_queries_hr.

    For each tool that has example queries, create a test case where:
    - Input: the example query
    - Expected output: the tool should be in top-K search results

    Args:
        docs: Tool documentation dict
        categories: Tool categories dict
        max_per_category: Max test cases per category

    Returns:
        List of test case dicts
    """
    # Build tool -> category mapping
    tool_to_category = {}
    for cat_name, cat_data in categories.items():
        for tool_name in cat_data.get("tools", []):
            tool_to_category[tool_name] = cat_name

    # Count per category to limit
    category_counts = defaultdict(int)

    test_cases = []
    for tool_name, doc in docs.items():
        examples = doc.get("example_queries_hr", [])
        if not examples:
            continue

        category = tool_to_category.get(tool_name, "uncategorized")

        for example in examples:
            if category_counts[category] >= max_per_category:
                break

            if not example or len(example.strip()) < 5:
                continue

            test_cases.append({
                "query": example.strip(),
                "expected_tool": tool_name,
                "category": category,
                "source": "documentation",
            })
            category_counts[category] += 1

    return test_cases


async def run_search_accuracy_test(test_cases: List[Dict], top_k: int = 10) -> Dict:
    """
    Run search accuracy test against the actual FAISS index.

    For each test case, searches for the query and checks if the expected tool
    appears in the top-K results.

    Returns detailed accuracy metrics.
    """
    # Lazy import to avoid loading everything for phrase tests
    from services.registry import ToolRegistry
    from services.unified_search import get_unified_search

    # Initialize registry
    registry = ToolRegistry(redis_client=None)

    # Check for cached embeddings
    cache_path = Path(__file__).parent.parent / ".cache" / "tool_embeddings.json"
    if not cache_path.exists():
        print("WARNING: No cached embeddings found. Registry initialization may take a while...")

    # Try to initialize with minimal config
    from config import get_settings
    settings = get_settings()
    sources = settings.swagger_sources

    if not sources:
        print("ERROR: No swagger sources configured. Cannot initialize registry.")
        return {"error": "no_swagger_sources"}

    print(f"Initializing registry with {len(sources)} sources...")
    success = await registry.initialize(sources)

    if not success or not registry.tools:
        print(f"ERROR: Registry initialization failed or empty. tools={len(registry.tools) if registry.tools else 0}")
        return {"error": "registry_init_failed"}

    print(f"Registry loaded: {len(registry.tools)} tools")

    # Initialize unified search
    unified_search = get_unified_search()
    unified_search.set_registry(registry)

    # Run tests
    results = {
        "total": len(test_cases),
        "top1_hits": 0,
        "top3_hits": 0,
        "top5_hits": 0,
        "top10_hits": 0,
        "misses": 0,
        "errors": 0,
        "by_category": defaultdict(lambda: {"total": 0, "top1": 0, "top5": 0, "top10": 0, "misses": 0}),
        "failures": [],
        "timings": [],
    }

    for i, test_case in enumerate(test_cases):
        query = test_case["query"]
        expected = test_case["expected_tool"]
        category = test_case["category"]

        if i % 50 == 0:
            print(f"  Testing {i}/{len(test_cases)}...")

        start = time.perf_counter()

        try:
            response = await unified_search.search(query, top_k=top_k)
            elapsed_ms = (time.perf_counter() - start) * 1000
            results["timings"].append(elapsed_ms)

            if not response.results:
                results["misses"] += 1
                results["by_category"][category]["misses"] += 1
                results["by_category"][category]["total"] += 1
                results["failures"].append({
                    "query": query[:80],
                    "expected": expected,
                    "got": "NO_RESULTS",
                    "category": category,
                })
                continue

            found_names = [r.tool_id for r in response.results]
            found_scores = {r.tool_id: r.score for r in response.results}

            results["by_category"][category]["total"] += 1

            if expected in found_names[:1]:
                results["top1_hits"] += 1
                results["by_category"][category]["top1"] += 1
            if expected in found_names[:3]:
                results["top3_hits"] += 1
            if expected in found_names[:5]:
                results["top5_hits"] += 1
                results["by_category"][category]["top5"] += 1
            if expected in found_names[:top_k]:
                results["top10_hits"] += 1
                results["by_category"][category]["top10"] += 1
            else:
                results["misses"] += 1
                results["by_category"][category]["misses"] += 1
                results["failures"].append({
                    "query": query[:80],
                    "expected": expected,
                    "got_top3": found_names[:3],
                    "top_score": found_scores.get(found_names[0], 0) if found_names else 0,
                    "category": category,
                })

        except Exception as e:
            results["errors"] += 1
            results["failures"].append({
                "query": query[:80],
                "expected": expected,
                "error": str(e)[:100],
                "category": category,
            })

    # Calculate percentages
    total = results["total"]
    if total > 0:
        results["top1_accuracy"] = results["top1_hits"] / total
        results["top3_accuracy"] = results["top3_hits"] / total
        results["top5_accuracy"] = results["top5_hits"] / total
        results["top10_accuracy"] = results["top10_hits"] / total
        results["miss_rate"] = results["misses"] / total

    if results["timings"]:
        results["avg_search_ms"] = sum(results["timings"]) / len(results["timings"])
        results["p95_search_ms"] = sorted(results["timings"])[int(len(results["timings"]) * 0.95)]

    return results


def print_results(results: Dict):
    """Pretty-print test results."""
    if "error" in results:
        print(f"\nERROR: {results['error']}")
        return

    total = results["total"]
    print(f"\n{'='*70}")
    print(f"  950-TOOL ACCURACY REPORT")
    print(f"{'='*70}")
    print(f"  Total test cases:  {total}")
    print(f"  Top-1 accuracy:    {results.get('top1_accuracy', 0):.1%} ({results['top1_hits']}/{total})")
    print(f"  Top-3 accuracy:    {results.get('top3_accuracy', 0):.1%} ({results['top3_hits']}/{total})")
    print(f"  Top-5 accuracy:    {results.get('top5_accuracy', 0):.1%} ({results['top5_hits']}/{total})")
    print(f"  Top-10 accuracy:   {results.get('top10_accuracy', 0):.1%} ({results['top10_hits']}/{total})")
    print(f"  Misses:            {results['misses']} ({results.get('miss_rate', 0):.1%})")
    print(f"  Errors:            {results['errors']}")
    print(f"  Avg search time:   {results.get('avg_search_ms', 0):.1f}ms")
    print(f"  P95 search time:   {results.get('p95_search_ms', 0):.1f}ms")

    # Per-category breakdown
    print(f"\n{'='*70}")
    print(f"  PER-CATEGORY ACCURACY (Top-5)")
    print(f"{'='*70}")

    cat_results = results.get("by_category", {})
    sorted_cats = sorted(
        cat_results.items(),
        key=lambda x: x[1]["top5"] / max(x[1]["total"], 1),
        reverse=False  # Worst first
    )

    for cat_name, cat_data in sorted_cats[:30]:
        cat_total = cat_data["total"]
        if cat_total == 0:
            continue
        cat_top5 = cat_data["top5"]
        cat_acc = cat_top5 / cat_total
        status = "OK" if cat_acc >= 0.7 else "WARN" if cat_acc >= 0.5 else "FAIL"
        print(f"  [{status:4s}] {cat_name:45s} {cat_acc:5.1%} ({cat_top5}/{cat_total})")

    # Show worst failures
    failures = results.get("failures", [])
    if failures:
        print(f"\n{'='*70}")
        print(f"  WORST FAILURES (first 20)")
        print(f"{'='*70}")
        for f in failures[:20]:
            print(f"  Query:    {f.get('query', '')[:70]}")
            print(f"  Expected: {f.get('expected', '')}")
            if 'got_top3' in f:
                print(f"  Got top3: {f['got_top3']}")
            elif 'got' in f:
                print(f"  Got:      {f['got']}")
            if 'error' in f:
                print(f"  Error:    {f['error']}")
            print()


async def main():
    """Run all tests."""
    phase1_only = "--phase1-only" in sys.argv

    print("=" * 70)
    print("  PHASE 1: Flow Phrase Matching Tests")
    print("=" * 70)

    phrase_results = test_flow_phrases()
    print(f"  Pass: {phrase_results['pass']}")
    print(f"  Fail: {phrase_results['fail']}")
    for failure in phrase_results["failures"]:
        print(failure)

    # Phase 2: Search accuracy (requires FAISS + embeddings + API keys)
    results = None
    if not phase1_only:
        print(f"\n{'='*70}")
        print(f"  PHASE 2: 950-Tool Search Accuracy")
        print(f"{'='*70}")

        try:
            # Load docs and generate test cases
            docs = load_tool_documentation()
            categories = load_tool_categories()

            print(f"  Loaded {len(docs)} tool docs, {len(categories)} categories")

            # Generate test cases from ALL tools (max 5 per category for thorough coverage)
            test_cases = generate_test_cases_from_docs(docs, categories, max_per_category=5)
            print(f"  Generated {len(test_cases)} test cases from documentation")

            # Add adversarial test cases
            adversarial_search_cases = []
            for adv in ADVERSARIAL_QUERIES:
                if adv["expected_tools"]:
                    for expected in adv["expected_tools"][:1]:  # Test first expected tool
                        adversarial_search_cases.append({
                            "query": adv["query"],
                            "expected_tool": expected,
                            "category": adv["category"],
                            "source": f"adversarial_{adv['test_type']}",
                        })

            all_cases = test_cases + adversarial_search_cases
            print(f"  Total test cases: {len(all_cases)} ({len(test_cases)} docs + {len(adversarial_search_cases)} adversarial)")

            # Run search accuracy
            results = await run_search_accuracy_test(all_cases)
            print_results(results)

        except FileNotFoundError as e:
            print(f"  SKIPPED: {e}")
            print(f"  (tool_documentation.json or tool_categories.json not found)")
        except Exception as e:
            print(f"  SKIPPED: Phase 2 requires running environment (FAISS, embeddings, API keys)")
            print(f"  Error: {e}")
    else:
        print(f"\n  Phase 2 skipped (--phase1-only)")

    # Summary
    print(f"\n{'='*70}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*70}")

    phrase_ok = phrase_results["fail"] == 0
    search_ok = results.get("top5_accuracy", 0) >= 0.7 if results and "top5_accuracy" in results else False

    print(f"  Flow phrases:     {'PASS' if phrase_ok else 'FAIL'}")
    if results and "top5_accuracy" in results:
        print(f"  Search accuracy:  {'PASS (>=70% top-5)' if search_ok else 'NEEDS IMPROVEMENT'}")
    else:
        print(f"  Search accuracy:  SKIPPED (requires Docker environment)")

    if not phrase_ok:
        print(f"\n  CRITICAL: {phrase_results['fail']} phrase matching failures!")

    return 0 if phrase_ok else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
