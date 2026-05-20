"""Generate anchor quality audit report from `config/tool_data.json`.

Runs 5 anti-pattern detectors over all 950 tools' anchor lists, optionally
embeds intent_summary-ja for paraphrase detection, writes hrvatski markdown
report to `docs/ANCHOR_AUDIT_{date}.md`.

Usage:
  python scripts/audit_anchor_quality.py
      [--output docs/ANCHOR_AUDIT_{date}.md]
      [--skip-paraphrase]    # don't embed intent_summary (faster, no Azure)
      [--top-n 30]           # how many worst tools to detail

Idempotent. Re-runnable. Read-only on registry; only writes report file.

Embedding (paraphrase detector):
  Requires Azure OpenAI access (same client as bot uses). One embedding
  call per non-internal tool's intent_summary (~655 calls, ~$0.001 cost).
  Result cached in `tests/benchmarks/intent_summary_embed_cache.json`
  so re-runs skip the API.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("audit_anchor_quality")


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_DATA = REPO_ROOT / "config" / "tool_data.json"
ANCHOR_CACHE = REPO_ROOT / "tests" / "benchmarks" / "router_anchor_cache.json"
INTENT_CACHE = REPO_ROOT / "tests" / "benchmarks" / "intent_summary_embed_cache.json"
DOCS_DIR = REPO_ROOT / "docs"


INTERNAL_HELPER_RE = re.compile(
    r"_(Distinct[A-Z][A-Za-z0-9]*|Agg|GroupBy|ProjectTo|metadata|"
    r"DeleteByCriteria|multipatch)$"
)


def is_internal(op_id: str) -> bool:
    return bool(INTERNAL_HELPER_RE.search(op_id))


def load_registry() -> dict:
    if not TOOL_DATA.exists():
        logger.error("Missing %s", TOOL_DATA)
        sys.exit(1)
    return json.loads(TOOL_DATA.read_text(encoding="utf-8"))


def load_anchor_cache() -> dict:
    """tool_id → list[list[float]] (vectors per anchor phrase, in order)."""
    if not ANCHOR_CACHE.exists():
        logger.warning(
            "No anchor cache at %s — paraphrase detector will be skipped.",
            ANCHOR_CACHE,
        )
        return {}
    try:
        data = json.loads(ANCHOR_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("anchor cache load failed: %s", e)
        return {}
    return data.get("vectors_by_tool") or {}


def load_intent_cache() -> dict:
    """{fingerprint -> {tool_id -> vector}} keyed by embedding deployment.
    Lets us skip API call if intent_summary text didn't change."""
    if not INTENT_CACHE.exists():
        return {}
    try:
        return json.loads(INTENT_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_intent_cache(cache: dict) -> None:
    INTENT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    INTENT_CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def cache_fingerprint(tools: dict, deployment: str) -> str:
    """Hash of (deployment, all intent_summary-ja). If any changes,
    cache is stale → re-embed."""
    h = hashlib.sha256()
    h.update(deployment.encode("utf-8"))
    for tid in sorted(tools.keys()):
        intent = (tools[tid].get("intent_summary") or "").strip()
        h.update(f"{tid}::{intent}".encode("utf-8"))
    return h.hexdigest()[:16]


async def embed_intent_summaries(
    tools: dict,
    deployment: str,
) -> dict:
    """Returns {tool_id: vector} for non-internal tools with non-empty
    intent_summary. Uses cache if fingerprint matches."""
    cache = load_intent_cache()
    fp = cache_fingerprint(tools, deployment)
    cached_vectors = (cache.get(fp) or {}).get("vectors_by_tool") or {}
    if cached_vectors:
        logger.info(
            "intent_summary embed cache hit (fingerprint=%s, %d vectors)",
            fp, len(cached_vectors),
        )
        return cached_vectors

    # Cache miss — embed via Azure
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from services.openai_client import get_embedding_client
    except ImportError as e:
        logger.error("Cannot import embedding client: %s", e)
        return {}

    embedder = get_embedding_client()
    vectors: dict = {}
    to_embed: list = []
    for tid, tool in tools.items():
        if is_internal(tid):
            continue
        intent = (tool.get("intent_summary") or "").strip()
        if not intent:
            continue
        to_embed.append((tid, intent))

    logger.info("Embedding %d intent_summary-ja via %s …",
                len(to_embed), deployment)
    # Batch in groups of 100 to respect Azure batch limits
    BATCH = 100
    for i in range(0, len(to_embed), BATCH):
        chunk = to_embed[i:i + BATCH]
        try:
            r = await embedder.embeddings.create(
                input=[text for _, text in chunk],
                model=deployment,
            )
            for (tid, _), d in zip(chunk, r.data):
                vectors[tid] = list(d.embedding)
        except Exception as e:  # noqa: BLE001
            logger.warning("embed batch %d-%d failed: %s — skipping",
                           i, i + BATCH, e)

    # Save cache for next run
    cache = {fp: {"vectors_by_tool": vectors, "deployment": deployment}}
    save_intent_cache(cache)
    logger.info("Cached %d intent embeds (fingerprint=%s)", len(vectors), fp)
    return vectors


async def main_async(
    output_path: Path, skip_paraphrase: bool, top_n: int,
):
    # Ensure repo root is on sys.path so we can import services.* without
    # running through pyproject install. Idempotent (sys.path dedup).
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from services.v2.anchor_audit import (
        audit_tool, render_markdown_report,
    )

    data = load_registry()
    tools = data.get("tools", {})
    non_internal = {k: v for k, v in tools.items() if not is_internal(k)}
    logger.info(
        "Loaded %d tools (%d non-internal)",
        len(tools), len(non_internal),
    )

    anchor_vecs_per_tool: dict = {}
    intent_vecs: dict = {}

    if not skip_paraphrase:
        anchor_vecs_per_tool = load_anchor_cache()
        deployment = (
            os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
            or "text-embedding-ada-002"
        )
        try:
            intent_vecs = await embed_intent_summaries(non_internal, deployment)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "intent embedding failed: %s — paraphrase detector skipped", e,
            )

    results: list = []
    for tid, tool in non_internal.items():
        result = audit_tool(
            tid, tool,
            intent_vec=intent_vecs.get(tid),
            anchor_vecs=anchor_vecs_per_tool.get(tid),
        )
        results.append(result)

    today = date.today().isoformat()
    report = render_markdown_report(results, today=today, top_n_tools=top_n)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    logger.info("Wrote %s (%d bytes)", output_path, len(report))

    # Console summary
    from services.v2.anchor_audit import aggregate_stats
    stats = aggregate_stats(results)
    print(f"\n=== Summary ===")
    print(f"Total tools: {stats['total_tools']}")
    print(f"Total anchors: {stats['total_anchors']}")
    for flag, count in stats["flag_counts"].items():
        pct = 100.0 * count / max(1, stats["total_anchors"])
        print(f"  {flag}: {count} ({pct:.1f}%)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    today = date.today().isoformat()
    ap.add_argument(
        "--output", type=Path,
        default=DOCS_DIR / f"ANCHOR_AUDIT_{today}.md",
        help=f"Output markdown path (default: docs/ANCHOR_AUDIT_{today}.md)",
    )
    ap.add_argument(
        "--skip-paraphrase", action="store_true",
        help="Skip cosine paraphrase detector (no Azure embedding call)",
    )
    ap.add_argument(
        "--top-n", type=int, default=30,
        help="How many worst tools to detail in report (default: 30)",
    )
    args = ap.parse_args()
    asyncio.run(main_async(args.output, args.skip_paraphrase, args.top_n))


if __name__ == "__main__":
    main()
