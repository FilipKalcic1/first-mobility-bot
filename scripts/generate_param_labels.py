"""Pre-generate Croatian labels for the top-N most-frequent tool×param
combinations. Output: `config/param_labels_hr.json`.

Why pre-gen:
  Runtime LLM calls add ~300ms latency to the first turn of every
  param-asking flow. For high-frequency params (`id` appears in 475 tools,
  `documentId` in 185), a pre-generated dict gives zero-cost lookup at
  engine start. The runtime ParamLabeler still handles rare or new params
  via LLM fallback + Redis cache.

Idempotency:
  Re-running OVERWRITES the JSON. A hash of the registry's (tool_id ×
  param_name) keyset is stored at the top so unchanged registry = no
  regen needed (skip if hash matches).

Cost:
  Per Filip's directive, target the top-30 distinct param names across
  their top-50 tools each (≤1500 LLM calls, ~$0.15 on gpt-4o-mini).
  Real cost depends on how many distinct (tool_id, param_name) pairs
  appear — capped by --max-pairs.

Usage:
  python scripts/generate_param_labels.py [--max-pairs N] [--dry-run]

  --dry-run: prints the (tool_id, param_name) pairs that WOULD be sent
             to the LLM, but does NOT make any API calls. Useful to
             sanity-check the selection before spending tokens.

WARNING: This makes real LLM calls and costs money. Run once at registry
update, then commit the resulting `config/param_labels_hr.json`.
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
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("generate_param_labels")


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_DATA = REPO_ROOT / "config" / "tool_data.json"
OUTPUT = REPO_ROOT / "config" / "param_labels_hr.json"

INTERNAL_HELPER_RE = re.compile(
    r"_(Distinct[A-Z][A-Za-z0-9]*|Agg|GroupBy|ProjectTo|metadata|"
    r"DeleteByCriteria|multipatch)$"
)


def collect_pairs(tool_data: dict, top_param_names: int = 30, top_tools_per_param: int = 50) -> list:
    """Return list of (tool_id, param_name, param_def, tool_intent) tuples
    for the top-N most-frequent params, each restricted to their top-M
    tools by some stable ordering (alphabetical for determinism).

    Skips internal helpers and context-only params (no user_input → bot
    never asks for them anyway).
    """
    tools = tool_data.get("tools", {})

    # Count param occurrences across all non-internal tools.
    param_count: Counter = Counter()
    param_to_tools: dict[str, list] = {}
    for op_id, tool in tools.items():
        if INTERNAL_HELPER_RE.search(op_id):
            continue
        for pname, pdef in (tool.get("parameters") or {}).items():
            if not isinstance(pdef, dict):
                continue
            dep = (pdef.get("dependency_source") or "user_input").lower()
            if dep != "user_input":
                continue
            param_count[pname] += 1
            param_to_tools.setdefault(pname, []).append(op_id)

    top_names = [name for name, _ in param_count.most_common(top_param_names)]
    logger.info(
        "Top-%d param names cover %d occurrences",
        len(top_names),
        sum(param_count[n] for n in top_names),
    )

    pairs: list = []
    for pname in top_names:
        # Pick top-M tools for this param (alphabetical for determinism)
        candidates = sorted(param_to_tools.get(pname, []))[:top_tools_per_param]
        for op_id in candidates:
            tool = tools.get(op_id) or {}
            pdef = (tool.get("parameters") or {}).get(pname) or {}
            intent = tool.get("intent_summary") or ""
            pairs.append((op_id, pname, pdef, intent))
    return pairs


def fingerprint(pairs: list) -> str:
    """Stable hash over the pair keyset — used to skip regen when
    nothing has changed."""
    keys = sorted(f"{op}::{p}" for op, p, _, _ in pairs)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


async def generate_label(client, deployment: str, op_id: str, pname: str, pdef: dict, intent: str) -> str | None:
    """Single LLM call. Mirrors ParamLabeler._llm_generate but inlined
    here so the script has no runtime dependency on the engine module."""
    prompt_parts = [f"Parametar: {pname}"]
    ptype = (pdef.get("param_type") or "").lower()
    if ptype:
        prompt_parts.append(f"Tip: {ptype}")
    desc = (pdef.get("description") or "").strip()
    if desc and len(desc) < 200:
        prompt_parts.append(f"API opis: {desc}")
    if intent:
        prompt_parts.append(f"Kontekst alata: {intent[:160]}")
    user_msg = "\n".join(prompt_parts)

    system = (
        "Ti generiraš kratku hrvatsku oznaku (1-4 riječi) za API parametar koji "
        "WhatsApp bot pita korisnika. Vrati SAMO JSON: {\"label\": \"...\"} bez "
        "ničega drugog. Label mora biti prirodna hrvatska fraza u akuzativu ako "
        "je objekt (\"Trebam {label}.\" mora čitati prirodno)."
    )

    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=40,
            response_format={"type": "json_object"},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM error for %s::%s: %s", op_id, pname, e)
        return None

    content = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    label = data.get("label")
    if not isinstance(label, str):
        return None
    label = label.strip()
    return label[:80] if label else None


async def main_async(max_pairs: int, dry_run: bool):
    if not TOOL_DATA.exists():
        logger.error("Missing %s", TOOL_DATA)
        sys.exit(1)
    tool_data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))

    pairs = collect_pairs(tool_data)
    if max_pairs:
        pairs = pairs[:max_pairs]

    fp = fingerprint(pairs)
    logger.info("Selected %d (tool_id, param_name) pairs; fingerprint=%s",
                len(pairs), fp)

    # Skip if existing file matches fingerprint (idempotent re-run).
    if OUTPUT.exists():
        try:
            existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if existing.get("__fingerprint") == fp:
                logger.info("Fingerprint match — nothing to regenerate.")
                return
        except (json.JSONDecodeError, OSError):
            pass

    if dry_run:
        logger.info("--dry-run: skipping LLM calls. Sample of first 10 pairs:")
        for op_id, pname, pdef, intent in pairs[:10]:
            logger.info("  %s::%s (type=%s) intent=%r",
                        op_id, pname,
                        (pdef.get("param_type") or "string"),
                        (intent or "")[:60])
        return

    # Lazy import — only needed when not dry-run, keeps script importable
    # in environments without openai SDK configured.
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from services.openai_client import get_openai_client  # type: ignore
        from config import settings  # type: ignore
    except ImportError as e:
        logger.error("Cannot import openai client: %s", e)
        sys.exit(1)

    client = get_openai_client()
    deployment = getattr(settings, "AZURE_OPENAI_DEPLOYMENT_NAME", None) or os.environ.get(
        "AZURE_OPENAI_DEPLOYMENT_NAME"
    )
    if not deployment:
        logger.error("AZURE_OPENAI_DEPLOYMENT_NAME not configured")
        sys.exit(1)

    # Generate sequentially (cheap throughput; LLM rate limit per minute is
    # 60-180 RPM on gpt-4o-mini, and we have at most ~1500 calls).
    labels: dict = {"__fingerprint": fp}
    for i, (op_id, pname, pdef, intent) in enumerate(pairs, start=1):
        label = await generate_label(client, deployment, op_id, pname, pdef, intent)
        if label:
            labels[f"{op_id}::{pname}"] = label
        if i % 25 == 0:
            logger.info("  [%d/%d] %s", i, len(pairs), label or "(skipped)")

    OUTPUT.write_text(
        json.dumps(labels, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    logger.info(
        "Wrote %s with %d labels (fingerprint=%s)",
        OUTPUT, len(labels) - 1, fp,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-pairs", type=int, default=1500,
                    help="Hard cap on number of LLM calls (default 1500)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print selection but do not call LLM")
    args = ap.parse_args()
    asyncio.run(main_async(args.max_pairs, args.dry_run))


if __name__ == "__main__":
    main()
