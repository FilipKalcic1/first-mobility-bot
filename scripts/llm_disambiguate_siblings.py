"""FAZA 7C (Filip 2026-05-17): LLM-generated differentiating anchors
for sibling tool groups.

A "sibling group" = ≥2 tools that share (primary_entity, HTTP method).
The template generator produces decent anchors but can leave sibling
tools (e.g., get_Companies vs get_Companies_id vs get_Companies_id_documents)
too similar — bot router can confuse them on shortcut queries.

This script:
  1. Identifies all sibling groups in config/tool_data.json
  2. For each group, calls gpt-4o-mini with the tool descriptions
  3. LLM returns 1 additional Croatian anchor PER tool that disambiguates
  4. Appends new anchors to tool_data.json (in-place by default)

Cost estimate: ~157 LLM calls × $0.001 = ~$0.16 (gpt-4o-mini).
Idempotent: re-run produces same anchors (temperature=0, JSON output).

Usage:
  python scripts/llm_disambiguate_siblings.py
      [--staging]    # write to .staging instead of in-place
      [--limit N]    # process only first N groups (debug)
      [--dry-run]    # show prompts without LLM call
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TOOL_DATA = REPO / "config" / "tool_data.json"

INTERNAL_HELPER_RE = re.compile(
    r"_(Distinct[A-Z][A-Za-z0-9]*|Agg|GroupBy|ProjectTo|metadata|"
    r"DeleteByCriteria|multipatch)$"
)

_ID_TOKENS = {"id", "Id", "ID", "documentId", "personId", "vehicleId",
              "companyId", "tenantId", "caseId", "tripId", "poolId",
              "expenseId", "equipmentId", "personIdOrEmail"}


def is_internal(op_id: str) -> bool:
    return bool(INTERNAL_HELPER_RE.search(op_id))


def primary_entity(path: str) -> str:
    """Extract primary entity from path, stripping {placeholders} and
    'Latest' prefix so siblings group together."""
    if not path:
        return ""
    parts = [p for p in path.split("/") if p and not p.startswith("{")]
    entities = [p for p in parts if p and p[0].isupper() and p not in _ID_TOKENS]
    if entities and entities[0].startswith("Latest"):
        entities[0] = entities[0][6:]
    return entities[0] if entities else ""


def build_sibling_groups(tools: dict) -> dict:
    """Group tools by (primary_entity, HTTP method). Returns
    {(entity, method): [tool_ids]} where len(tool_ids) >= 2."""
    groups: dict = defaultdict(list)
    for tid, tool in tools.items():
        if is_internal(tid):
            continue
        if not isinstance(tool, dict):
            continue
        pe = primary_entity(tool.get("path", ""))
        method = (tool.get("method") or "").upper()
        if pe and method:
            groups[(pe, method)].append(tid)
    return {k: v for k, v in groups.items() if len(v) >= 2}


_SYSTEM_PROMPT = (
    "Ti si generator differentiating Croatian anchor fraza za WhatsApp "
    "fleet management bot router. Dobivaš grupu sibling tools koji dijele "
    "isti primary entity i HTTP method. Za svaki tool, generiraj JEDNU "
    "dodatnu Croatian anchor frazu (3-7 riječi, lowercase, conversational, "
    "kako vozač tipka u WhatsApp-u) koja JASNO razlikuje OVAJ tool od ostalih "
    "u grupi. Nemoj koristiti formal frazu, nemoj kapital, nemoj 'Korisnik "
    "želi'. Vrati SAMO JSON: {\"tool_id_1\": \"...\", \"tool_id_2\": \"...\", ...}"
)


def build_user_prompt(entity: str, method: str, tools_in_group: list, tool_data: dict) -> str:
    """Build LLM user message describing the sibling group."""
    lines = [f"Entity: {entity}  Method: {method}", "", "Sibling tools (postojeći anchori da znaš što već postoji):"]
    for i, tid in enumerate(tools_in_group, 1):
        tool = tool_data.get(tid, {})
        path = tool.get("path", "")
        intent = (tool.get("intent_summary") or "").strip()[:120]
        existing = (tool.get("anchors") or [])[:3]  # only first 3 to save tokens
        lines.append(f"{i}. {tid}")
        lines.append(f"   path: {path}")
        if intent:
            lines.append(f"   intent: {intent}")
        if existing:
            lines.append(f"   trenutni anchori: {existing}")
        lines.append("")
    lines.append(
        "Za svaki tool gore, vrati JEDNU dodatnu Croatian anchor frazu koja ga "
        "RAZLIKUJE od ostalih u grupi. Fraza mora biti lowercase, 3-7 riječi, "
        "kako bi WhatsApp driver stvarno tipkao."
    )
    return "\n".join(lines)


async def llm_generate_for_group(
    client, deployment: str,
    entity: str, method: str,
    tids: list, tool_data: dict,
    dry_run: bool = False,
) -> dict:
    """Single LLM call → dict {tool_id: new_anchor}. Returns {} on failure."""
    system = _SYSTEM_PROMPT
    user = build_user_prompt(entity, method, tids, tool_data)

    if dry_run:
        print("--- DRY RUN PROMPT ---")
        print(f"SYSTEM: {system[:100]}...")
        print(f"USER:\n{user}")
        return {}

    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
    except Exception as e:  # noqa: BLE001
        print(f"  LLM error for ({entity}, {method}): {e}", file=sys.stderr)
        return {}

    try:
        raw = response.choices[0].message.content or ""
        data = json.loads(raw.strip())
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError) as e:
        print(f"  parse error for ({entity}, {method}): {e}", file=sys.stderr)
        return {}

    if not isinstance(data, dict):
        return {}

    # Validate + clean: lowercase, 3-10 words, only known tool_ids
    cleaned: dict = {}
    valid_tids = set(tids)
    for tid, anchor in data.items():
        if tid not in valid_tids:
            continue
        if not isinstance(anchor, str):
            continue
        anchor = anchor.strip().lower()
        words = anchor.split()
        if not (2 <= len(words) <= 12):
            continue
        cleaned[tid] = anchor
    return cleaned


async def main_async(staging: bool, limit: int | None, dry_run: bool):
    sys.stdout.reconfigure(encoding="utf-8")

    if not TOOL_DATA.exists():
        print(f"ERROR: {TOOL_DATA} not found", file=sys.stderr)
        sys.exit(1)

    data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    tools = data.get("tools", {})
    groups = build_sibling_groups(tools)
    print(f"Identified {len(groups)} sibling groups covering "
          f"{sum(len(v) for v in groups.values())} tools.")

    items = list(groups.items())
    if limit:
        items = items[:limit]
        print(f"Limited to first {limit} groups.")

    if dry_run:
        # Just print first 2 prompts to inspect
        for (entity, method), tids in items[:2]:
            print(f"\n=== GROUP: {entity} / {method} ({len(tids)} tools) ===")
            await llm_generate_for_group(None, "n/a", entity, method, tids, tools, dry_run=True)
        return

    # Lazy import — only when actually calling LLM
    sys.path.insert(0, str(REPO))
    try:
        from services.openai_client import get_openai_client
    except ImportError as e:
        print(f"Cannot import openai client: {e}", file=sys.stderr)
        sys.exit(1)

    client = get_openai_client()
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME") or "gpt-4o-mini"

    total_added = 0
    failed_groups = 0
    for i, ((entity, method), tids) in enumerate(items, 1):
        new_anchors_by_tid = await llm_generate_for_group(
            client, deployment, entity, method, tids, tools,
        )
        if not new_anchors_by_tid:
            failed_groups += 1
            continue
        # Append new anchor to each tool's anchors list
        for tid, new_anchor in new_anchors_by_tid.items():
            existing = tools[tid].get("anchors") or []
            if new_anchor not in existing:
                tools[tid]["anchors"] = list(existing) + [new_anchor]
                total_added += 1
        if i % 10 == 0:
            print(f"  [{i}/{len(items)}] groups processed, "
                  f"{total_added} anchors added so far")

    # Write
    target = TOOL_DATA.with_suffix(".json.staging") if staging else TOOL_DATA
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)

    print(f"\n=== Disambig complete ===")
    print(f"  Groups processed: {len(items)}")
    print(f"  Groups failed:    {failed_groups}")
    print(f"  Anchors added:    {total_added}")
    print(f"  Output: {target}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staging", action="store_true",
                    help="Write to .staging instead of in-place")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only first N groups (debug)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print prompts without calling LLM")
    args = ap.parse_args()
    asyncio.run(main_async(args.staging, args.limit, args.dry_run))


if __name__ == "__main__":
    main()
