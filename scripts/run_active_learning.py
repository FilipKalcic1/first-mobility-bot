"""Generate active learning report from Redis telemetry.

Reads all `routing:accuracy_log:*` Redis lists, decodes JSON events,
runs analysis functions from `services.v2.active_learning`, and writes
a Croatian markdown report Filip can read to identify improvement
opportunities.

Usage:
  python scripts/run_active_learning.py
      [--redis-url redis://localhost:6379/0]
      [--output docs/ACTIVE_LEARNING_{date}.md]
      [--tenant t1,t2,t3]        # restrict to specific tenants
      [--min-events 50]          # warn if total < threshold

Read-only against Redis (LRANGE only, no LPUSH/DEL/EXPIRE). Idempotent —
re-runnable any time.

Privacy note: events do NOT contain phone/phone_hash (see telemetry.py).
This report aggregates over tenants, no per-user data leaks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("run_active_learning")


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
TOOL_DATA = REPO_ROOT / "config" / "tool_data.json"

ROUTING_LOG_KEY_BASE = "routing:accuracy_log"


async def fetch_events(redis_client, tenants_filter: list | None) -> list:
    """LRANGE all routing-log keys, decode JSON. Returns flat list."""
    events: list = []
    # SCAN for matching keys (cheaper than KEYS pattern on large dbs).
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
        # Per-tenant key shape: routing:accuracy_log:{tenant_id}
        # Legacy single-key shape: routing:accuracy_log (no tenant suffix)
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


def load_all_tool_ids() -> set:
    """Load all tool operation_ids from registry — used for coverage analysis."""
    if not TOOL_DATA.exists():
        logger.warning("tool_data.json not found — tool coverage will be skipped")
        return set()
    try:
        data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to load tool_data.json: %s", e)
        return set()
    return set((data.get("tools") or {}).keys())


async def main_async(redis_url: str, output_path: Path,
                     tenants_filter: list | None, min_events: int):
    # Lazy import — keeps script importable even when redis isn't installed
    try:
        from redis import asyncio as aioredis
    except ImportError:
        logger.error("redis package not installed. Run: pip install redis")
        sys.exit(1)

    from services.v2.active_learning import (
        extract_confidence_distribution,
        extract_negation_per_tool,
        extract_query_patterns,
        extract_tool_coverage,
        render_markdown_report,
    )

    redis_client = aioredis.from_url(redis_url, decode_responses=True)
    try:
        events = await fetch_events(redis_client, tenants_filter)
    finally:
        try:
            await redis_client.aclose()
        except Exception:  # noqa: BLE001
            pass

    logger.info("Fetched %d telemetry events", len(events))
    if len(events) < min_events:
        logger.warning(
            "Only %d events — report may be statistically unreliable "
            "(threshold: %d). Continuing anyway.",
            len(events), min_events,
        )

    neg_rates = extract_negation_per_tool(events)
    query_patterns = extract_query_patterns(events)
    confidence_dist = extract_confidence_distribution(events)

    all_tools = load_all_tool_ids()
    tool_coverage = extract_tool_coverage(events, all_tools) if all_tools else None

    today = date.today().isoformat()
    report = render_markdown_report(
        total_events=len(events),
        today=today,
        negation_rates=neg_rates,
        query_patterns=query_patterns,
        confidence_dist=confidence_dist,
        tool_coverage=tool_coverage,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    logger.info("Wrote %s (%d bytes)", output_path, len(report))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        help="Redis connection URL (default: $REDIS_URL or redis://localhost:6379/0)",
    )
    today = date.today().isoformat()
    default_output = DOCS_DIR / f"ACTIVE_LEARNING_{today}.md"
    ap.add_argument(
        "--output", type=Path, default=default_output,
        help=f"Output markdown path (default: {default_output.relative_to(REPO_ROOT)})",
    )
    ap.add_argument(
        "--tenant", default="",
        help="Comma-separated tenant_ids to include (default: all)",
    )
    ap.add_argument(
        "--min-events", type=int, default=50,
        help="Warn if total events < this threshold (default: 50)",
    )
    args = ap.parse_args()

    tenants_filter = [t.strip() for t in args.tenant.split(",") if t.strip()] or None
    sys.path.insert(0, str(REPO_ROOT))
    asyncio.run(main_async(
        args.redis_url, args.output, tenants_filter, args.min_events,
    ))


if __name__ == "__main__":
    main()
