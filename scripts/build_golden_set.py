"""Build the routing golden set from Redis telemetry.

Measure-first foundation (Filip 2026-06-10). Harvests labeled
(query → expected_tool) pairs from the live telemetry log so that any
future routing change is decided by DATA, not by assertion. This is the
metar — it produces real numbers only once the bot is live and has real
traffic. Until then it returns an empty set (no crash).

Label sources, by confidence:
  - corrected (HIGH): a "nije točno" reoffer that the user resolved by
    picking a DIFFERENT tool. The engine logs this as a TelemetryEvent with
    a `correction` field {wrong_tool, correct_tool}. This is the gold: the
    user explicitly told us the first pick was wrong AND which one was right.
    Yields a positive label (correct_tool) + a negative label (wrong_tool).
  - accepted_weak (LOW): a routing event with a tool_picked, no execution
    error, and is_negation=False, with no correction. The user did NOT
    immediately reject — but we CANNOT confirm satisfaction, because
    telemetry stores no phone/session id (privacy decision, see telemetry.py
    + FAZA 5 caveat) so we cannot link turn N's pick to turn N+1's reaction.
    Treat as a weak positive; manual review required before using as truth.

Why no "rejected-only" cross-turn labels: linking a "nije točno" turn back
to the tool picked on the PREVIOUS turn needs session linking, which the
telemetry shape deliberately lacks. The `correction` field sidesteps this by
capturing wrong+correct in a SINGLE event at correction time.

KNOWN BIAS (read before trusting numbers): reoffer state is intentionally NOT
saved after mutations (only after GET auto-execute / L2B), so the golden set
is GET-biased — a wrong routing on a POST/PUT/DELETE does NOT generate a
correction label. A mutation-routing failure mode will be ABSENT from these
numbers, not because it doesn't happen.

Usage:
  python scripts/build_golden_set.py
      [--redis-url redis://localhost:6379/0]
      [--output tests/benchmarks/golden_set.json]
      [--tenant t1,t2]          # restrict to specific tenants
      [--generated-at 2026-06-10]  # optional stamp (else omitted)

Read-only against Redis (SCAN + LRANGE only). Idempotent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("build_golden_set")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "benchmarks" / "golden_set.json"
ROUTING_LOG_KEY_BASE = "routing:accuracy_log"


async def fetch_events(redis_client, tenants_filter: list | None) -> list:
    """SCAN all routing-log keys, LRANGE + JSON-decode. Returns flat list.

    Mirrors scripts/run_active_learning.py::fetch_events (read-only).
    """
    events: list = []
    pattern = f"{ROUTING_LOG_KEY_BASE}*"
    keys: list = []
    try:
        async for k in redis_client.scan_iter(match=pattern, count=100):
            if isinstance(k, bytes):
                k = k.decode("utf-8")
            keys.append(k)
    except Exception as e:  # noqa: BLE001
        logger.error("redis SCAN failed: %s", e)
        return []

    logger.info("Found %d routing-log keys matching %s", len(keys), pattern)
    for key in keys:
        # routing:accuracy_log:{tenant_id}  (or legacy unscoped: no suffix)
        tenant_from_key = ""
        if ":" in key[len(ROUTING_LOG_KEY_BASE):]:
            tenant_from_key = key[len(ROUTING_LOG_KEY_BASE) + 1:]
        if tenants_filter and tenant_from_key and tenant_from_key not in tenants_filter:
            continue
        try:
            raw_list = await redis_client.lrange(key, 0, -1)
        except Exception as e:  # noqa: BLE001
            logger.warning("LRANGE %s failed: %s", key, e)
            continue
        for raw in raw_list:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                events.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue
    return events


def build_labels(events: list) -> dict:
    """Turn raw telemetry events into golden-set labels.

    Returns {"golden": [...], "negatives": [...], "counts": {...}}.

    Chained corrections are handled (Filip review 2026-06-10): for the same
    (tenant, query), a sequence A→nije točno→B→nije točno→C produces two
    correction events (wrong=A/correct=B, then wrong=B/correct=C). Treating
    each event as an independent HIGH positive would poison the set: B is a
    positive in event 1 but a negative in event 2. So we GROUP corrections by
    (tenant, query), compute the rejected set (all wrong_tools), and take the
    LAST surviving correct_tool (one not later rejected) as the single golden
    positive. Every rejected tool — including intermediate picks — becomes a
    negative.

    Confidence (epistemics, per review): a negative ("A is wrong") is the
    user's explicit signal → HIGH. A positive correct_tool is only
    "picked-from-cards-and-no-further-complaint" → MEDIUM (corrected) or
    LOW (accepted_weak, never even reoffer-touched). Negatives are the
    stronger label.

    L2B sentinel (__L2B_DRIVER_BASICS__) is never written as a tool_id: as a
    rejected origin it becomes a categorized negative {category:'l2b_rejected'};
    the real label is the correct_tool of the resolving pick.

    Time-window caveat: with no session id in telemetry (privacy), we group by
    (tenant, query). The same query asked weeks apart with a genuinely
    different correct answer would be merged (latest pick wins). Rare; flagged.
    """
    from collections import defaultdict

    L2B_SENTINEL = "__L2B_DRIVER_BASICS__"

    # 1) Group correction events by (tenant, query); collect weak candidates.
    corr_groups: dict[tuple, list] = defaultdict(list)
    weak: dict[tuple, dict] = {}          # (query, tool) -> entry
    rejected_by_query: dict[str, set] = defaultdict(set)
    n_corrected = 0

    for ev in events:
        query = (ev.get("query_scrubbed") or "").strip()
        if not query:
            continue
        tenant = ev.get("tenant_id") or ""
        corr = ev.get("correction")
        if isinstance(corr, dict) and corr.get("correct_tool"):
            n_corrected += 1
            corr_groups[(tenant, query)].append({
                "wrong": corr.get("wrong_tool"),
                "correct": corr["correct_tool"],
            })
            if corr.get("wrong_tool"):
                rejected_by_query[query].add(corr["wrong_tool"])
            continue
        tool = ev.get("tool_picked")
        if tool and not ev.get("error") and not ev.get("is_negation"):
            weak[(query, tool)] = {
                "query": query, "expected_tool": tool,
                "source": "accepted_weak", "label_confidence": "low",
            }

    golden: dict[tuple, dict] = {}
    negatives: list[dict] = []

    # 2) Resolve each correction chain → 1 positive + N negatives.
    for (tenant, query), chain in corr_groups.items():
        rejected = {s["wrong"] for s in chain if s["wrong"]}
        corrects = [s["correct"] for s in chain]
        survivors = [c for c in corrects if c not in rejected]
        final = survivors[-1] if survivors else None  # latest non-rejected pick
        if final:
            golden[(query, final)] = {
                "query": query, "expected_tool": final,
                "source": "corrected", "label_confidence": "medium",
                "chain_len": len(chain),
                "rejected_in_chain": sorted(t for t in rejected
                                            if t and t != L2B_SENTINEL),
            }
        for w in sorted(rejected):
            if w == L2B_SENTINEL:
                negatives.append({"query": query, "wrong_tool": None,
                                  "category": "l2b_rejected",
                                  "confidence": "high"})
            else:
                negatives.append({"query": query, "wrong_tool": w,
                                  "confidence": "high"})

    # 3) Weak positives — only if NOT contradicted by a correction for that
    #    query and not already a corrected golden entry.
    n_weak = 0
    for (query, tool), entry in weak.items():
        if tool in rejected_by_query.get(query, set()):
            continue  # this tool was explicitly rejected for this query
        if (query, tool) in golden:
            continue  # corrected label already covers it
        golden[(query, tool)] = entry
        n_weak += 1

    return {
        "golden": sorted(golden.values(), key=lambda d: (d["source"], d["query"])),
        "negatives": negatives,
        "counts": {
            "corrected_events": n_corrected,
            "correction_chains": len(corr_groups),
            "accepted_weak_kept": n_weak,
            "golden_unique": len(golden),
            "negatives": len(negatives),
        },
    }


async def main_async(redis_url: str, output_path: Path,
                     tenants_filter: list | None, generated_at: str | None):
    try:
        from redis import asyncio as aioredis
    except ImportError:
        logger.error("redis package not installed. Run: pip install redis")
        sys.exit(1)

    redis_client = aioredis.from_url(redis_url, decode_responses=True)
    try:
        events = await fetch_events(redis_client, tenants_filter)
    finally:
        try:
            await redis_client.aclose()
        except Exception:  # noqa: BLE001
            pass

    logger.info("Fetched %d telemetry events", len(events))
    result = build_labels(events)
    result["source_events"] = len(events)
    if generated_at:
        result["generated_at"] = generated_at

    counts = result["counts"]
    logger.info(
        "Golden: %d unique (corrected=%d events in %d chains, "
        "accepted_weak_kept=%d) | negatives=%d",
        counts["golden_unique"], counts["corrected_events"],
        counts["correction_chains"], counts["accepted_weak_kept"],
        counts["negatives"],
    )
    if counts["golden_unique"] == 0:
        logger.warning(
            "Empty golden set — no real traffic yet. This is expected before "
            "go-live; the harvester is ready to capture labels once the bot "
            "is live and users send 'nije točno' / pick tools."
        )
    elif counts["corrected_events"] == 0:
        logger.warning(
            "0 'corrected' labels — only weak (low-confidence) positives. "
            "Reoffer corrections are the gold; they appear once users actually "
            "say 'nije točno' and re-pick. Manual review REQUIRED before using "
            "this as eval ground truth."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    logger.info("Wrote %s", output_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        help="Redis URL (default: $REDIS_URL or redis://localhost:6379/0)",
    )
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                    help=f"Output JSON (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})")
    ap.add_argument("--tenant", default="",
                    help="Comma-separated tenant_ids to include (default: all)")
    ap.add_argument("--generated-at", default="",
                    help="Optional ISO date stamp to embed in output")
    args = ap.parse_args()

    tenants_filter = [t.strip() for t in args.tenant.split(",") if t.strip()] or None
    sys.path.insert(0, str(REPO_ROOT))
    asyncio.run(main_async(
        args.redis_url, args.output, tenants_filter, args.generated_at or None,
    ))


if __name__ == "__main__":
    main()
