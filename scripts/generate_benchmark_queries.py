"""FAZA 10 (Filip 2026-05-17) — Honest expanded benchmark generator.

The current `tests/benchmarks/objective_benchmark_100.json` benchmark is
brutally biased: 25/58 queries (43%) are `get_MasterData` driver basics,
91% are GET, zero PUT/PATCH/DELETE. Bot accuracy reported on it
(43% p@1 / 86% r@50) tells us NOTHING about mutation tools or
manager/admin scope — only about driver read-only basics.

This script generates a stratified expanded benchmark:
  - 16 tools per HTTP method (GET, POST, PUT, PATCH, DELETE) → 80 tools
  - 2 WhatsApp-style Croatian queries per tool via gpt-4o-mini → ~160 queries
  - LLM PROMPT NEVER SEES ANCHORS (avoid memorization bias — only sees
    tool_id, method, path, intent_summary, target persona)
  - Skips tools already in the old benchmark to avoid duplication
  - Output schema matches existing benchmark for `bench_router_accuracy.py`

Cost: ~$0.02 (160 LLM calls × ~$0.0001 each, gpt-4o-mini).

Usage:
  python scripts/generate_benchmark_queries.py
      [--per-method 16]   # tools per HTTP method (default 16)
      [--queries-per-tool 2]
      [--seed 42]
      [--dry-run]         # print prompts without LLM call
      [--output PATH]     # default tests/benchmarks/expanded_benchmark_v2.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TOOL_DATA = REPO / "config" / "tool_data.json"
EXISTING_BENCH = REPO / "tests" / "benchmarks" / "objective_benchmark_100.json"
DEFAULT_OUT = REPO / "tests" / "benchmarks" / "expanded_benchmark_v2.json"

INTERNAL_HELPER_RE = re.compile(
    r"_(Distinct[A-Z][A-Za-z0-9]*|GroupBy|ProjectTo|metadata|"
    r"DeleteByCriteria|multipatch)$"
)

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


_SYSTEM_PROMPT = (
    "Ti si simulator WhatsApp poruka za fleet management bot. "
    "Dobivaš opis jednog API endpointa koji korisnik (driver/manager/admin) "
    "pokušava pozvati. Tvoj zadatak: generiraj 2 RAZLIČITE kratke Croatian "
    "WhatsApp poruke (3-8 riječi) koje bi taj korisnik realno tipkao da "
    "pozove ovaj endpoint. "
    "STIL: lowercase, kolokvijalno, kao stvarna WhatsApp poruka. "
    "Smije imati typo ili nedostatak diakritike (š→s, č→c, ć→c). "
    "ZABRANJENO: formalne fraze ('Molim Vas', 'Korisnik želi'), engleske "
    "riječi, doslovno prepisan intent_summary. "
    'Vrati SAMO JSON: {"queries": ["...", "..."]}'
)


def is_internal(op_id: str) -> bool:
    return bool(INTERNAL_HELPER_RE.search(op_id))


def infer_persona(tool: dict) -> str:
    """Pick the 'target' persona for the LLM prompt. Prefer personas_strict
    list (first entry), else default to driver for GETs and manager for
    mutating methods (rough heuristic — manager is the one most often
    creating/editing/deleting things in a fleet system)."""
    personas = tool.get("personas_strict") or []
    if personas:
        return personas[0]
    method = (tool.get("method") or "GET").upper()
    return "driver" if method == "GET" else "manager"


def build_user_prompt(tool_id: str, tool: dict, persona: str) -> str:
    method = (tool.get("method") or "GET").upper()
    path = tool.get("path", "")
    intent = (tool.get("intent_summary") or "").strip()
    return (
        f"Tool ID: {tool_id}\n"
        f"Method: {method}\n"
        f"Path: {path}\n"
        f"Intent summary: {intent}\n"
        f"Target persona: {persona}\n"
        f"\n"
        f"Generiraj 2 različite WhatsApp poruke koje bi {persona} tipkao "
        f"da pozove ovaj endpoint."
    )


def stratified_sample(
    tools: dict, per_method: int, excluded_tool_ids: set, seed: int,
) -> list:
    """Returns list of (tool_id, tool_dict) — `per_method` tools for each
    of the 5 HTTP methods, randomly sampled (seeded). Skips internal
    helpers and any tool in `excluded_tool_ids`."""
    rng = random.Random(seed)
    by_method: dict = defaultdict(list)
    for tid, tool in tools.items():
        if is_internal(tid):
            continue
        if tid in excluded_tool_ids:
            continue
        if not isinstance(tool, dict):
            continue
        method = (tool.get("method") or "GET").upper()
        if method in METHODS:
            by_method[method].append((tid, tool))

    sampled: list = []
    for method in METHODS:
        pool = by_method[method]
        rng.shuffle(pool)
        take = min(per_method, len(pool))
        chosen = pool[:take]
        sampled.extend(chosen)
        print(f"  {method}: sampled {take} of {len(pool)} available")
    return sampled


async def generate_for_tool(
    client, deployment: str,
    tool_id: str, tool: dict, persona: str,
    dry_run: bool,
) -> list:
    """Single LLM call → list of 2 query strings. Returns [] on any failure."""
    user = build_user_prompt(tool_id, tool, persona)
    if dry_run:
        print(f"\n--- DRY RUN ({tool_id}) ---")
        print(user)
        return []

    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.4,  # slight variety, not deterministic
            max_tokens=120,
            response_format={"type": "json_object"},
        )
    except Exception as e:  # noqa: BLE001
        print(f"  LLM error for {tool_id}: {e}", file=sys.stderr)
        return []

    try:
        raw = response.choices[0].message.content or ""
        data = json.loads(raw.strip())
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError) as e:
        print(f"  parse error for {tool_id}: {e}", file=sys.stderr)
        return []

    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list):
        return []

    # Validate + clean
    cleaned: list = []
    for q in queries:
        if not isinstance(q, str):
            continue
        q = q.strip()
        words = q.split()
        if not (2 <= len(words) <= 15):
            continue
        cleaned.append(q)
    return cleaned[:2]  # max 2 per tool


def primary_entity(path: str) -> str:
    """Crude entity extractor for benchmark `expected_domain` field —
    first capitalized non-placeholder segment of the path."""
    if not path:
        return ""
    for part in path.split("/"):
        if part and not part.startswith("{") and part[0].isupper():
            return part
    return ""


async def main_async(
    per_method: int, queries_per_tool: int, seed: int,
    dry_run: bool, output: Path,
):
    sys.stdout.reconfigure(encoding="utf-8")

    if not TOOL_DATA.exists():
        print(f"ERROR: {TOOL_DATA} not found", file=sys.stderr)
        sys.exit(1)
    if not EXISTING_BENCH.exists():
        print(f"ERROR: {EXISTING_BENCH} not found", file=sys.stderr)
        sys.exit(1)

    data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    tools = data.get("tools", {})

    existing = json.loads(EXISTING_BENCH.read_text(encoding="utf-8"))
    excluded = {
        q["expected_tool"] for q in existing.get("queries", [])
        if q.get("expected_tool")
    }
    print(f"Excluding {len(excluded)} tools already in old benchmark.")

    print(f"\nStratified sample ({per_method} per method, seed={seed}):")
    sampled = stratified_sample(tools, per_method, excluded, seed)
    print(f"Total sampled tools: {len(sampled)}")

    if dry_run:
        # Just print first 3 prompts to inspect
        for tid, tool in sampled[:3]:
            await generate_for_tool(
                None, "n/a", tid, tool, infer_persona(tool), dry_run=True,
            )
        return

    # Lazy import — only when actually calling LLM
    sys.path.insert(0, str(REPO))
    try:
        from services.openai_client import get_openai_client
    except ImportError as e:
        print(f"Cannot import openai client: {e}", file=sys.stderr)
        sys.exit(1)

    client = get_openai_client()
    deployment = (
        os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or "gpt-4o-mini"
    )

    all_queries: list = []
    counter_id = 1
    failed_tools: list = []
    for i, (tid, tool) in enumerate(sampled, 1):
        persona = infer_persona(tool)
        queries = await generate_for_tool(
            client, deployment, tid, tool, persona, dry_run=False,
        )
        if not queries:
            failed_tools.append(tid)
            continue
        for q in queries[:queries_per_tool]:
            all_queries.append({
                "id": f"v2_{counter_id:04d}",
                "query": q,
                "persona": persona,
                "expected_domain": primary_entity(tool.get("path", "")),
                "expected_tool": tid,
                "expected_field": "",
            })
            counter_id += 1
        if i % 20 == 0:
            print(f"  [{i}/{len(sampled)}] tools processed, "
                  f"{len(all_queries)} queries so far")

    out_data = {
        "name": "expanded_benchmark_v2",
        "generated_by": "scripts/generate_benchmark_queries.py",
        "seed": seed,
        "per_method": per_method,
        "queries": all_queries,
    }
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(
        json.dumps(out_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, output)

    print(f"\n=== Generation complete ===")
    print(f"  Tools sampled:    {len(sampled)}")
    print(f"  Tools failed:     {len(failed_tools)}")
    print(f"  Queries written:  {len(all_queries)}")
    print(f"  Output:           {output}")
    if failed_tools:
        print(f"  Failed tool IDs:  {failed_tools[:10]}"
              f"{' (...)' if len(failed_tools) > 10 else ''}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-method", type=int, default=16,
                    help="Tools per HTTP method (default 16 → 80 total)")
    ap.add_argument("--queries-per-tool", type=int, default=2,
                    help="Queries per tool (default 2)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed for stratified sample (default 42)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print sample prompts without LLM call")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT,
                    help=f"Output path (default {DEFAULT_OUT})")
    args = ap.parse_args()
    asyncio.run(main_async(
        args.per_method, args.queries_per_tool, args.seed,
        args.dry_run, args.output,
    ))


if __name__ == "__main__":
    main()
