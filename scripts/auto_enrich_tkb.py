"""Auto-enrich Tool Knowledge Base for ALL 950 tools.

Strategy: for each tool, gather metadata (method, entity, swagger desc,
parameters, sibling tools with same entity), send to gpt-4o-mini, get
back structured TKB JSON. Merges with hand-curated entries (manual ones
take precedence).

Concurrency: 3. ~20 min for 950 tools at ~$0.50 total.
Resumable: saves progress every 50 tools to .partial.json.

Run: python -X utf8 scripts/auto_enrich_tkb.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config import get_settings  # noqa: E402
from services.openai_client import get_openai_client  # noqa: E402

REG_PATH = REPO_ROOT / "config" / "processed_tool_registry.json"
EXISTING_TKB = REPO_ROOT / "config" / "tool_knowledge_base.json"
RICH_DOCS = REPO_ROOT / "config" / "rich_tool_docs.json"
OUT_PATH = REPO_ROOT / "config" / "tool_knowledge_base.json"
PARTIAL_PATH = REPO_ROOT / "config" / "tool_knowledge_base.partial.json"


def parse_op(op_id: str):
    m = re.match(r"^(get|post|put|patch|delete)_([A-Z][A-Za-z0-9]+)(.*)$", op_id)
    if not m:
        return None, None, ""
    return m.group(1).upper(), m.group(2), m.group(3)


PROMPT_TEMPLATE = """Generiraj TKB (Tool Knowledge Base) zapis za API alat MobilityOne fleet system-a.

ALAT: {op_id}
METHOD: {method}
PATH: {path}
SUMMARY (Swagger): {summary}
DESCRIPTION (Swagger): {description}
REQUIRED PARAMETERS: {required_params}
TAGS: {tags}

SIBLING ALATI (isti entity, za disambiguation):
{siblings_block}

POSTOJEĆI rich_doc (ako postoji):
{rich_doc_block}

Tvoj zadatak: vrati JSON s ovim strukturama:
{{
  "intent_summary": "1 rečenica na hrvatskom — što alat radi",
  "returns": {{"field_name": "kratak opis", ...}},
  "use_when": ["uvjet 1", "uvjet 2", "uvjet 3"],
  "do_not_use_when": [
    {{"alt_tool": "drugi_alat_id", "razlog": "kratki opis razlike"}}
  ],
  "examples": [
    {{"input": "primjer hrvatskog upita", "field_hint": "polje ili null", "category": "kratka_kategorija"}}
  ]
}}

PRAVILA:
- Sve tekstualno polje na hrvatskom
- intent_summary < 100 chars
- 2-4 use_when uvjeta
- do_not_use_when: 1-2 najsličnija sibling alata, razlog kratki
- 2-4 examples — natural Croatian, varied tone (formalno + neformalno)
- Ako method nije GET, examples često imaju "želim", "trebam", "obriši", "stavi"
- BEZ quoted user phrases u use_when (samo abstract opis)
- returns: ako endpoint vraća listu, returns ima "items" + 2-3 polja po itemu
- Ako Swagger description je prazan, izvedi smisao iz operation_id (npr. delete_X_DeleteByCriteria = bulk delete s filter-om)

Vrati STRICT JSON, ne wrap u markdown."""


def format_siblings(siblings: list, max_n: int = 5) -> str:
    if not siblings:
        return "(nema sibling alata)"
    lines = []
    for s in siblings[:max_n]:
        lines.append(f"- {s['operation_id']} [{s['method']}] {s.get('summary', '')[:80]}")
    return "\n".join(lines)


def format_rich_doc(rich: dict | None) -> str:
    if not rich:
        return "(nema postojećeg rich_doc)"
    parts = []
    if rich.get("description"):
        parts.append(f"description: {rich['description'][:200]}")
    if rich.get("when_to_use"):
        parts.append(f"when_to_use: {rich['when_to_use'][:150]}")
    if rich.get("when_NOT_to_use"):
        parts.append(f"when_NOT_to_use: {rich['when_NOT_to_use'][:150]}")
    return "\n".join(parts)


async def generate_one(client, deployment, tool, siblings, rich_doc):
    op_id = tool["operation_id"]
    method, entity, modifier = parse_op(op_id)
    if method is None:
        return op_id, None
    required_params = tool.get("required_params", [])

    prompt = PROMPT_TEMPLATE.format(
        op_id=op_id,
        method=method,
        path=tool.get("path", ""),
        summary=tool.get("summary", "")[:200] or "(prazno)",
        description=tool.get("description", "")[:300] or "(prazno)",
        required_params=", ".join(required_params[:8]) or "(nema obaveznih)",
        tags=", ".join(tool.get("tags", [])) or "(nema tagova)",
        siblings_block=format_siblings(siblings),
        rich_doc_block=format_rich_doc(rich_doc),
    )

    try:
        r = await client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        raw = r.choices[0].message.content or ""
        data = json.loads(raw)
        # Inject mechanical fields
        data["method"] = method
        data["mutation_safe"] = (method == "GET")
        if method != "GET":
            data["requires_confirm"] = True
        return op_id, data
    except Exception as e:
        return op_id, {"_error": str(e)[:200]}


async def main():
    settings = get_settings()
    client = get_openai_client()
    deployment = settings.AZURE_OPENAI_DEPLOYMENT_NAME

    with REG_PATH.open(encoding="utf-8") as f:
        reg = json.load(f)
    tools = reg.get("tools", [])
    print(f"Loaded {len(tools)} tools from registry.")

    # Existing manual TKB — preserve
    existing_tkb: dict = {}
    if EXISTING_TKB.exists():
        with EXISTING_TKB.open(encoding="utf-8") as f:
            existing_tkb = json.load(f).get("tools", {})
    print(f"Existing manual TKB: {len(existing_tkb)} entries (will be preserved as-is).")

    # Existing rich_docs — feed into LLM as seed context
    rich_docs: dict = {}
    if RICH_DOCS.exists():
        with RICH_DOCS.open(encoding="utf-8") as f:
            rich_docs = json.load(f).get("tools", {})
    print(f"Existing rich_docs: {len(rich_docs)} entries (used as seed).")

    # Group tools by entity for sibling lookup
    by_entity: dict[str, list[dict]] = defaultdict(list)
    for t in tools:
        op = t["operation_id"]
        _, ent, _ = parse_op(op)
        if ent:
            by_entity[ent].append({
                "operation_id": op,
                "method": t["method"],
                "summary": t.get("summary", "") or t.get("description", "")[:80],
            })

    # Resume from partial
    progress: dict = {}
    if PARTIAL_PATH.exists():
        with PARTIAL_PATH.open(encoding="utf-8") as f:
            progress = json.load(f).get("tools", {})
        print(f"Resuming: {len(progress)} tools already enriched.")

    # Tools to process: those NOT in manual TKB and NOT in progress
    remaining = []
    for t in tools:
        op = t["operation_id"]
        if op in existing_tkb:
            progress[op] = existing_tkb[op]  # preserve as-is
            continue
        if op in progress and "_error" not in progress[op]:
            continue  # already done
        remaining.append(t)

    print(f"To enrich: {len(remaining)} tools")
    print(f"Estimated time: {len(remaining)*1.5/3/60:.1f} min at concurrency 3")

    sem = asyncio.Semaphore(3)
    save_lock = asyncio.Lock()
    counter = [0]

    async def process(t):
        op = t["operation_id"]
        _, ent, _ = parse_op(op)
        siblings = [s for s in by_entity.get(ent, []) if s["operation_id"] != op] if ent else []
        rich = rich_docs.get(op)

        async with sem:
            await asyncio.sleep(0.2)  # gentle pacing
            result_op, entry = await generate_one(client, deployment, t, siblings, rich)
            async with save_lock:
                progress[result_op] = entry
                counter[0] += 1
                if counter[0] % 25 == 0:
                    # Save partial
                    with PARTIAL_PATH.open("w", encoding="utf-8") as f:
                        json.dump({"tools": progress}, f, ensure_ascii=False, indent=2)
                    ok = sum(1 for v in progress.values() if isinstance(v, dict) and "_error" not in v)
                    print(f"  [{counter[0]}/{len(remaining)}] saved partial — {ok} ok entries total")

    t0 = time.time()
    await asyncio.gather(*(process(t) for t in remaining))
    elapsed = time.time() - t0
    print(f"\nEnrichment done in {elapsed/60:.1f} min")

    # Final write
    err_count = sum(1 for v in progress.values() if isinstance(v, dict) and "_error" in v)
    ok_count = len(progress) - err_count
    print(f"OK: {ok_count}, errors: {err_count}")

    output = {
        "_meta": {
            "version": "2.0",
            "generated_at": "2026-05-06",
            "purpose": "Auto-enriched TKB for ALL 950 tools. Manual entries preserved; auto-generated for rest. Per-call selective load by DomainScopedToolPicker.",
            "manual_count": len(existing_tkb),
            "auto_count": ok_count - len(existing_tkb),
            "errors": err_count,
        },
        "tools": progress,
    }
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\nWritten to: {OUT_PATH}")
    if PARTIAL_PATH.exists():
        PARTIAL_PATH.unlink()


if __name__ == "__main__":
    asyncio.run(main())
