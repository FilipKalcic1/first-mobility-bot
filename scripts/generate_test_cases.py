"""
Generate 500 stratified test cases from tool_documentation.json.

5-Layer permutation engine:
  L1: Base queries from example_queries_hr (gold standard)
  L2: Synonym variations from synonyms_hr
  L3: Diacritic removal (č→c, ć→c, ž→z, š→s, đ→d)
  L4: Possessive signals (moj/moja/moje + entity)
  L5: Verb mutations (prikaži/obriši/ažuriraj variants)

Output: test_tool_discovery_generated.py (500 test cases, stratified)

Usage:
    python scripts/generate_test_cases.py
"""
import json
import random
import os
import sys
from collections import defaultdict
from pathlib import Path

# Resolve paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
TOOL_DOCS_PATH = PROJECT_DIR / "config" / "tool_documentation.json"
OUTPUT_PATH = PROJECT_DIR / "test_tool_discovery_generated.py"

# Seed for reproducibility
random.seed(42)

# --- Diacritic normalization ---

_DIACRITIC_MAP = str.maketrans({
    'č': 'c', 'ć': 'c', 'ž': 'z', 'š': 's', 'đ': 'd',
    'Č': 'C', 'Ć': 'C', 'Ž': 'Z', 'Š': 'S', 'Đ': 'D',
})


def strip_diacritics(text: str) -> str:
    return text.translate(_DIACRITIC_MAP)


def has_diacritics(text: str) -> bool:
    return any(c in text for c in 'čćžšđČĆŽŠĐ')


# --- Tool ID parsing ---

def parse_tool_id(tool_id: str):
    """Parse tool_id into (method, entity, suffix)."""
    parts = tool_id.split("_")
    if len(parts) < 2:
        return None, None, None
    method = parts[0].lower()
    if method not in ("get", "post", "put", "patch", "delete"):
        return None, None, None
    entity = parts[1]
    suffix = "_".join(parts[2:]) if len(parts) > 2 else ""
    return method, entity, suffix


# --- Entity → HR noun forms (for possessive generation) ---

_POSSESSIVE_ENTITIES = {
    "Vehicles": [("moje", "vozilo"), ("moj", "auto"), ("moj", "automobil")],
    "Expenses": [("moj", "trošak"), ("moji", "troškovi"), ("moj", "rashod")],
    "Trips": [("moje", "putovanje"), ("moja", "vožnja"), ("moj", "putni nalog")],
    "Cases": [("moj", "slučaj"), ("moja", "šteta"), ("moj", "kvar")],
    "Equipment": [("moja", "oprema"), ("moj", "uređaj")],
    "Companies": [("moja", "tvrtka"), ("moja", "firma")],
    "Partners": [("moj", "partner"), ("moj", "dobavljač")],
    "Persons": [("moj", "zaposlenik"), ("moja", "osoba")],
    "Teams": [("moj", "tim"), ("moja", "ekipa")],
}

# --- Verb mutation maps ---

_GET_VERBS = ["dohvati", "prikaži", "pokaži", "daj mi", "lista"]
_DELETE_VERBS = ["obriši", "izbriši"]
_PUT_VERBS = ["ažuriraj", "promijeni", "izmijeni"]
_POST_VERBS = ["dodaj", "kreiraj", "napravi", "unesi"]


def _get_verb_list(method: str):
    return {
        "get": _GET_VERBS,
        "delete": _DELETE_VERBS,
        "put": _PUT_VERBS,
        "post": _POST_VERBS,
    }.get(method, _GET_VERBS)


# --- Layer generators ---

def layer1_base_queries(tool_docs: dict) -> list:
    """L1: Extract (query, tool_id) from example_queries_hr."""
    pairs = []
    for tool_id, doc in tool_docs.items():
        method, entity, suffix = parse_tool_id(tool_id)
        if not method or not entity:
            continue
        examples = doc.get("example_queries_hr", [])
        for ex in examples:
            ex = ex.strip().rstrip(".")
            if len(ex) >= 3:
                pairs.append((ex, tool_id, f"T1:{method.upper()}:{entity}"))
    return pairs


def layer2_synonym_variations(tool_docs: dict) -> list:
    """L2: Generate queries from synonyms_hr."""
    pairs = []
    # Only use synonyms from base list tools (get_Entity with no suffix)
    # to avoid generating tests for obscure nested tools
    for tool_id, doc in tool_docs.items():
        method, entity, suffix = parse_tool_id(tool_id)
        if not method or not entity:
            continue
        # Only generate synonym tests for base tools (no suffix)
        if suffix and suffix not in ("id",):
            continue
        synonyms = doc.get("synonyms_hr", [])
        for syn in synonyms:
            syn = syn.strip()
            if len(syn) < 4:
                continue
            # Use synonym as-is if it looks like a full phrase (has a verb)
            verb_prefixes = ("dohvati", "prikaži", "obriši", "izbriši", "dodaj",
                             "ažuriraj", "kreiraj", "želim")
            is_phrase = any(syn.lower().startswith(v) for v in verb_prefixes)
            if is_phrase:
                pairs.append((syn, tool_id, f"T2:SYN:{entity}"))
            else:
                # It's a noun — wrap in a simple query template
                if method == "get":
                    pairs.append((f"dohvati {syn}", tool_id, f"T2:SYN:{entity}"))
                elif method == "delete":
                    pairs.append((f"obriši {syn}", tool_id, f"T2:SYN:{entity}"))
                elif method in ("put", "patch"):
                    pairs.append((f"ažuriraj {syn}", tool_id, f"T2:SYN:{entity}"))
                elif method == "post":
                    pairs.append((f"dodaj {syn}", tool_id, f"T2:SYN:{entity}"))
    return pairs


def layer3_diacritic_removal(base_pairs: list) -> list:
    """L3: For each pair with diacritics, generate a stripped version."""
    pairs = []
    for query, tool_id, tag in base_pairs:
        if has_diacritics(query):
            stripped = strip_diacritics(query)
            pairs.append((stripped, tool_id, f"T3:DIACRIT:{tag.split(':')[-1]}"))
    return pairs


def layer4_possessive(tool_docs: dict) -> list:
    """L4: Generate possessive queries for primary entities.

    'moje vozilo' → expected: get_{Entity}_id (SINGLE_ENTITY).
    """
    pairs = []
    for entity, noun_variants in _POSSESSIVE_ENTITIES.items():
        # Find the base GET tool for this entity
        base_tool = f"get_{entity}"
        id_tool = f"get_{entity}_id"

        # Only generate if the base tool exists
        if base_tool not in tool_docs and id_tool not in tool_docs:
            continue

        target = id_tool if id_tool in tool_docs else base_tool

        for possessive, noun_hr in noun_variants:
            # Generate possessive variants
            variants = [
                f"{possessive} {noun_hr}",
                f"koji je {possessive} {noun_hr}",
                f"pokaži mi {possessive} {noun_hr}",
                f"gdje je {possessive} {noun_hr}",
            ]
            # Also: "moj" forms for different genders
            for poss_form in ["moj", "moja", "moje", "moji"]:
                variants.append(f"{poss_form} {noun_hr}")

            for query in variants:
                pairs.append((query, target, f"T4:POSS:{entity}"))

    return pairs


def layer5_verb_mutations(tool_docs: dict) -> list:
    """L5: Verb swapping for primary entities."""
    pairs = []

    # Find primary entity tools that exist
    primary_entities = [
        "Vehicles", "Expenses", "Trips", "Cases", "Equipment",
        "Companies", "Persons", "Partners", "Teams",
    ]

    # Entity → HR accusative/genitive form
    entity_hr = {
        "Vehicles": "vozila",
        "Expenses": "troškove",
        "Trips": "putovanja",
        "Cases": "slučajeve",
        "Equipment": "opremu",
        "Companies": "tvrtke",
        "Persons": "zaposlenike",
        "Partners": "partnere",
        "Teams": "timove",
    }

    for entity in primary_entities:
        hr_name = entity_hr.get(entity)
        if not hr_name:
            continue

        # GET variants
        get_tool = f"get_{entity}"
        if get_tool in tool_docs:
            for verb in _GET_VERBS:
                pairs.append((f"{verb} {hr_name}", get_tool, f"T5:VERB:{entity}"))

        # DELETE variants
        delete_tool = f"delete_{entity}_id"
        delete_tool_alt = f"delete_{entity}"
        target_del = delete_tool if delete_tool in tool_docs else (
            delete_tool_alt if delete_tool_alt in tool_docs else None
        )
        if target_del:
            for verb in _DELETE_VERBS:
                pairs.append((f"{verb} {hr_name}", target_del, f"T5:VERB:{entity}"))

        # PUT/UPDATE variants — plural nouns ("ažuriraj vozila") route to multipatch
        multipatch_tool = f"post_{entity}_multipatch"
        put_tool = f"put_{entity}_id"
        # Plural → multipatch, singular would go to put_id
        target_put = multipatch_tool if multipatch_tool in tool_docs else (
            put_tool if put_tool in tool_docs else None
        )
        if target_put:
            for verb in _PUT_VERBS:
                pairs.append((f"{verb} {hr_name}", target_put, f"T5:VERB:{entity}"))

        # POST variants
        post_tool = f"post_{entity}"
        if post_tool in tool_docs:
            for verb in _POST_VERBS:
                pairs.append((f"{verb} {hr_name}", post_tool, f"T5:VERB:{entity}"))

    return pairs


# --- Stratified sampling ---

def stratified_sample(all_pairs: dict, tier_quotas: dict, total: int) -> list:
    """
    Sample from each tier according to quotas.

    all_pairs: {tier_name: [(query, tool_id, tag), ...]}
    tier_quotas: {tier_name: max_count}
    """
    selected = []
    used_queries = set()

    for tier_name in ["T1", "T4", "T5", "T3", "T2"]:  # Priority order
        pool = all_pairs.get(tier_name, [])
        quota = tier_quotas.get(tier_name, 0)

        # Deduplicate within pool
        unique_pool = []
        for item in pool:
            q_norm = item[0].lower().strip()
            if q_norm not in used_queries:
                unique_pool.append(item)
                used_queries.add(q_norm)

        # Stratify by entity within tier
        by_entity = defaultdict(list)
        for item in unique_pool:
            entity = item[2].split(":")[-1]
            by_entity[entity].append(item)

        # Round-robin across entities
        tier_selected = []
        entities = sorted(by_entity.keys())
        if not entities:
            continue

        per_entity = max(1, quota // len(entities))
        for entity in entities:
            items = by_entity[entity]
            random.shuffle(items)
            tier_selected.extend(items[:per_entity])

        # If we still have quota left, fill from remaining
        remaining_quota = quota - len(tier_selected)
        if remaining_quota > 0:
            remaining_pool = [
                item for item in unique_pool
                if item not in tier_selected
            ]
            random.shuffle(remaining_pool)
            tier_selected.extend(remaining_pool[:remaining_quota])

        # Final cap
        tier_selected = tier_selected[:quota]
        selected.extend(tier_selected)

    return selected[:total]


# --- Output generator ---

def generate_test_file(test_cases: list, output_path: Path):
    """Generate the test_tool_discovery_generated.py file."""
    # Group by tier for comments
    by_tier = defaultdict(list)
    for query, tool_id, tag in test_cases:
        tier = tag.split(":")[0]
        by_tier[tier].append((query, tool_id, tag))

    # Build the test data section
    test_data_lines = []
    tier_labels = {
        "T1": "Tier 1: Base queries from example_queries_hr",
        "T2": "Tier 2: Synonym variations from synonyms_hr",
        "T3": "Tier 3: Diacritic removal permutations",
        "T4": "Tier 4: Possessive signals (moj/moja/moje)",
        "T5": "Tier 5: Verb mutations",
    }

    for tier in ["T1", "T4", "T5", "T3", "T2"]:
        items = by_tier.get(tier, [])
        if not items:
            continue
        test_data_lines.append(f'    # --- {tier_labels.get(tier, tier)} ({len(items)}) ---')
        for query, tool_id, tag in items:
            q_escaped = query.replace('\\', '\\\\').replace('"', '\\"')
            test_data_lines.append(f'    ("{q_escaped}", "{tool_id}", 5, "{tag}"),')
        test_data_lines.append('')

    test_data_block = '\n'.join(test_data_lines)

    # Write the complete file using a template
    template = f'''"""
Auto-generated end-to-end test for tool discovery pipeline.
Generated: {len(test_cases)} test cases from tool_documentation.json
Generator: scripts/generate_test_cases.py

DO NOT EDIT MANUALLY — regenerate with:
    python scripts/generate_test_cases.py

Run inside container:
    docker exec mobility_api python /app/test_tool_discovery_generated.py
"""
import asyncio
import sys
import logging
from collections import defaultdict

# Suppress all logging to keep output clean
logging.disable(logging.CRITICAL)


# (query, expected_tool_id, top_k, tag)
GENERATED_TESTS = [
{test_data_block}
]


async def do_search(search, query, top_k=5):
    """Search and return results list."""
    resp = await search.search(query, top_k=top_k)
    if hasattr(resp, "results"):
        return resp.results
    return resp


async def test():
    from config import get_settings
    from services.registry import ToolRegistry
    from services.unified_search import UnifiedSearch

    settings = get_settings()
    registry = ToolRegistry(redis_client=None)
    await registry.initialize(settings.swagger_sources)

    W = sys.stdout.write
    W(f"=== GENERATED TOOL DISCOVERY TEST ({{len(GENERATED_TESTS)}} cases) ===\\n")
    W(f"Registry: {{len(registry.tools)}} tools loaded\\n\\n")

    search = UnifiedSearch(registry)
    await search.initialize()

    # Track results by tier
    tier_ok = defaultdict(int)
    tier_total = defaultdict(int)
    tier_misses = defaultdict(list)
    total_ok = 0

    for i, (query, expected, top_k, tag) in enumerate(GENERATED_TESTS):
        tier = tag.split(":")[0]
        tier_total[tier] += 1

        results = []
        try:
            results = await do_search(search, query, top_k=top_k)
            found = any(
                r.tool_id.lower() == expected.lower()
                for r in results[:top_k]
            )
        except Exception as e:
            found = False
            W(f"  [ERR] #{{i+1}} \\"{{query}}\\" -> {{e}}\\n")

        if found:
            tier_ok[tier] += 1
            total_ok += 1
        else:
            top3 = [(r.tool_id, round(r.score, 3)) for r in results[:3]] if results else []
            tier_misses[tier].append((query, expected, top3))
            W(f"  [MISS] \\"{{query}}\\" -> expected {{expected}}\\n")
            for tid, score in top3:
                marker = " <<<" if tid.lower() == expected.lower() else ""
                W(f"         {{tid}}: {{score}}{{marker}}\\n")

        # Progress indicator every 50 tests
        if (i + 1) % 50 == 0:
            W(f"  ... {{i+1}}/{{len(GENERATED_TESTS)}} done ({{total_ok}} OK)\\n")

    # --- Summary ---
    sep = "=" * 60
    W(f"\\n{{sep}}\\n")
    W("SUMMARY\\n")
    W(f"{{sep}}\\n")

    tier_display = {{
        "T1": "Base queries  ",
        "T2": "Synonyms      ",
        "T3": "Diacritics    ",
        "T4": "Possessive    ",
        "T5": "Verb mutations",
    }}

    for tier in ["T1", "T2", "T3", "T4", "T5"]:
        ok = tier_ok.get(tier, 0)
        total = tier_total.get(tier, 0)
        if total == 0:
            continue
        pct = ok / total * 100
        label = tier_display.get(tier, tier)
        W(f"  {{label}}: {{ok:3d}}/{{total:3d}} ({{pct:5.1f}}%)\\n")

    total_tests = len(GENERATED_TESTS)
    pct_total = total_ok / total_tests * 100 if total_tests else 0
    W(f"\\n  TOTAL: {{total_ok}}/{{total_tests}} ({{pct_total:.1f}}%)\\n")

    # Show worst misses per tier
    for tier in ["T1", "T2", "T3", "T4", "T5"]:
        misses = tier_misses.get(tier, [])
        if misses:
            W(f"\\n  {{tier}} misses ({{len(misses)}}): \\n")
            for q, exp, top3 in misses[:5]:
                W(f"    \\"{{q}}\\" -> expected {{exp}}\\n")

    # Per-tier minimum thresholds (T2/T3 are stress tests, lower bar)
    tier_thresholds = {{"T1": 95, "T4": 90, "T5": 75, "T2": 15, "T3": 20}}
    tier_pass = True
    for tier, min_pct in tier_thresholds.items():
        ok = tier_ok.get(tier, 0)
        total = tier_total.get(tier, 0)
        if total > 0 and (ok / total * 100) < min_pct:
            tier_pass = False
            W(f"  TIER FAIL: {{tier}} below {{min_pct}}%\\n")

    # Overall threshold: 70% (includes stress-test tiers)
    threshold_pct = 70.0
    passed = pct_total >= threshold_pct and tier_pass
    result_str = "PASS" if passed else "FAIL"
    W(f"\\nRESULT: {{result_str}} (overall: {{threshold_pct:.0f}}%, per-tier gates active)\\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(test())
'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(template)


# --- Main ---

def main():
    if not TOOL_DOCS_PATH.exists():
        print(f"ERROR: {TOOL_DOCS_PATH} not found")
        sys.exit(1)

    with open(TOOL_DOCS_PATH, 'r', encoding='utf-8') as f:
        tool_docs = json.load(f)

    print(f"Loaded {len(tool_docs)} tools from tool_documentation.json")

    # Generate all layers
    l1 = layer1_base_queries(tool_docs)
    print(f"  L1 (base queries):    {len(l1)}")

    l2 = layer2_synonym_variations(tool_docs)
    print(f"  L2 (synonym vars):    {len(l2)}")

    l3 = layer3_diacritic_removal(l1)  # Only base queries — synonym+diacritic has too many confounds
    print(f"  L3 (diacritic strip): {len(l3)}")

    l4 = layer4_possessive(tool_docs)
    print(f"  L4 (possessive):      {len(l4)}")

    l5 = layer5_verb_mutations(tool_docs)
    print(f"  L5 (verb mutations):  {len(l5)}")

    total_raw = len(l1) + len(l2) + len(l3) + len(l4) + len(l5)
    print(f"  Total raw:            {total_raw}")

    # Group by tier
    all_pairs = defaultdict(list)
    for item in l1:
        all_pairs["T1"].append(item)
    for item in l2:
        all_pairs["T2"].append(item)
    for item in l3:
        all_pairs["T3"].append(item)
    for item in l4:
        all_pairs["T4"].append(item)
    for item in l5:
        all_pairs["T5"].append(item)

    # Stratified sampling: 500 total
    tier_quotas = {
        "T1": 200,   # Base queries — highest priority
        "T4": 80,    # Possessive — critical for possessive signal testing
        "T5": 70,    # Verb mutations — tests action detection
        "T3": 80,    # Diacritic removal — tests normalization
        "T2": 70,    # Synonyms — tests vocabulary breadth
    }
    total_target = 500

    selected = stratified_sample(all_pairs, tier_quotas, total_target)
    print(f"\nSelected {len(selected)} test cases (target: {total_target})")

    # Stats
    tier_counts = defaultdict(int)
    entity_counts = defaultdict(int)
    for _, _, tag in selected:
        parts = tag.split(":")
        tier_counts[parts[0]] += 1
        entity_counts[parts[-1]] += 1

    print("\nPer-tier breakdown:")
    for tier in sorted(tier_counts.keys()):
        print(f"  {tier}: {tier_counts[tier]}")

    print(f"\nEntity coverage: {len(entity_counts)} unique entities")
    print(f"Top entities: {sorted(entity_counts.items(), key=lambda x: -x[1])[:10]}")

    # Generate output file
    generate_test_file(selected, OUTPUT_PATH)
    print(f"\nGenerated: {OUTPUT_PATH}")
    print(f"Run with: docker exec mobility_api python /app/test_tool_discovery_generated.py")


if __name__ == "__main__":
    main()
