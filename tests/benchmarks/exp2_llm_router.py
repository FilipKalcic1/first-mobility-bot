"""
Exp 2: LLM-as-router upper bound test.

Sends ALL 950 tool_ids + purpose[:50] to gpt-4o and asks it to pick the
right tool for each of the 176 adversarial queries.

This measures the theoretical ceiling of LLM routing — if gpt-4o can't
pick the right tool from a full list, no amount of pipeline tuning will help.

Cost estimate: ~$9 (176 × ~20K input tokens)
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import logging
logging.basicConfig(level=logging.WARNING)

from openai import AsyncOpenAI

# Paths
TOOL_DOCS = PROJECT_ROOT / "config" / "tool_documentation.json"
PARAPHRASES = PROJECT_ROOT / "tests" / "benchmarks" / "tool_recognition_paraphrases.seed99.json"
RESULTS_PATH = PROJECT_ROOT / "tests" / "benchmarks" / "exp2_llm_router_results.json"


async def main():
    # Load tool documentation
    with open(TOOL_DOCS, "r", encoding="utf-8") as f:
        tools = json.load(f)
    print(f"Loaded {len(tools)} tools", flush=True)

    # Build compact tool list: tool_id + purpose[:50]
    tool_lines = []
    for tool_id, doc in sorted(tools.items()):
        purpose = doc.get("purpose", "")[:50]
        tool_lines.append(f"{tool_id}: {purpose}")

    tool_list_text = "\n".join(tool_lines)
    print(f"Tool list: {len(tool_lines)} tools, ~{len(tool_list_text)} chars", flush=True)

    # Load paraphrases
    with open(PARAPHRASES, "r", encoding="utf-8") as f:
        data = json.load(f)

    queries = []
    for tool_entry in data["tools"]:
        for para in tool_entry["paraphrases"]:
            queries.append((tool_entry["tool_id"], para))
    print(f"Loaded {len(queries)} queries")

    # Initialize client
    api_key = os.getenv("OPENAI_EMBEDDING_API_KEY")  # Same key used for embeddings
    if not api_key:
        print("FATAL: OPENAI_EMBEDDING_API_KEY not set")
        sys.exit(1)

    client = AsyncOpenAI(api_key=api_key, max_retries=2, timeout=60.0)

    system_prompt = f"""Ti si router koji na temelju korisničkog upita odabire točan API tool.

Ispod je potpuna lista svih dostupnih tool-ova (tool_id: opis):

{tool_list_text}

Za svaki korisnički upit, odgovori SAMO s JSON-om:
{{"tool_id": "<exact tool_id from the list>", "confidence": <0.0-1.0>}}

PRAVILA:
- tool_id MORA biti točan iz gornje liste (copy-paste)
- Ako nisi siguran, odaberi najbliži mogući tool
- Odgovori SAMO JSON, bez teksta"""

    # Estimate tokens
    # ~4 chars per token for mixed Croatian/English
    est_input_tokens = len(system_prompt) / 4
    print(f"Estimated input tokens per query: ~{est_input_tokens:.0f}")
    print(f"Estimated total input: ~{est_input_tokens * len(queries) / 1e6:.2f}M tokens")
    print(f"Estimated cost: ~${est_input_tokens * len(queries) / 1e6 * 2.5 + len(queries) * 50 / 1e6 * 10:.2f}")

    # Run queries
    results = {
        "experiment": "Exp 2: LLM-as-router",
        "model": "gpt-4o",
        "total_queries": len(queries),
        "top1_hits": 0,
        "details": [],
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }

    # Process sequentially (rate limits + cost tracking)
    t0 = time.time()
    for i, (expected, query) in enumerate(queries):
        try:
            response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                temperature=0,
                max_tokens=100,
            )

            if response.usage:
                results["total_input_tokens"] += response.usage.prompt_tokens
                results["total_output_tokens"] += response.usage.completion_tokens

            text = response.choices[0].message.content.strip()

            # Parse response
            predicted = None
            try:
                parsed = json.loads(text)
                predicted = parsed.get("tool_id", "")
            except json.JSONDecodeError:
                # Try to extract tool_id from text
                if '"tool_id"' in text:
                    import re
                    m = re.search(r'"tool_id"\s*:\s*"([^"]+)"', text)
                    if m:
                        predicted = m.group(1)

            hit = predicted == expected
            if hit:
                results["top1_hits"] += 1

            results["details"].append({
                "expected": expected,
                "predicted": predicted,
                "query": query,
                "hit": hit,
                "raw_response": text[:200],
            })

        except Exception as e:
            results["details"].append({
                "expected": expected,
                "predicted": None,
                "query": query,
                "hit": False,
                "error": str(e),
            })

        # Progress every 10
        if (i + 1) % 10 == 0:
            elapsed = time.time() - t0
            hits = results["top1_hits"]
            print(f"  [{i+1}/{len(queries)}] Top-1={hits}/{i+1} = {hits/(i+1):.1%} "
                  f"({elapsed:.0f}s, {elapsed/(i+1)*1000:.0f}ms/q)")

    elapsed = time.time() - t0

    # Calculate cost
    input_cost = results["total_input_tokens"] / 1e6 * 2.5
    output_cost = results["total_output_tokens"] / 1e6 * 10
    total_cost = input_cost + output_cost
    results["cost_usd"] = round(total_cost, 2)
    results["elapsed_seconds"] = round(elapsed, 1)
    results["avg_ms_per_query"] = round(elapsed / len(queries) * 1000, 0)

    # Save results
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Report
    n = len(queries)
    print(f"\n{'='*70}")
    print(f"EXP 2: LLM-AS-ROUTER RESULTS")
    print(f"{'='*70}")
    print(f"Model:    gpt-4o")
    print(f"Queries:  {n}")
    print(f"Top-1:    {results['top1_hits']}/{n} = {results['top1_hits']/n:.1%}")
    print(f"Cost:     ${total_cost:.2f} ({results['total_input_tokens']:,} in, {results['total_output_tokens']:,} out)")
    print(f"Time:     {elapsed:.0f}s ({elapsed/n*1000:.0f}ms/q)")
    print(f"\nResults saved to {RESULTS_PATH}")

    # Top-5 analysis (check if expected is in top confusion targets)
    misses = [d for d in results["details"] if not d["hit"]]
    if misses:
        print(f"\nMisses: {len(misses)}/{n}")
        # Show first 10 misses
        for d in misses[:10]:
            print(f"  Expected: {d['expected']}")
            print(f"  Got:      {d['predicted']}")
            print(f"  Query:    {d['query'][:80]}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
