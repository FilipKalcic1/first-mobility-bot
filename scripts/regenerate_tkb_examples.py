"""DEPRECATED 2026-05-15 — `examples` field was dropped in Phase 1 union-merge.

`examples` had ZERO runtime readers (62% of TKB by size, pure dead data —
the regen scripts were its only consumer). It is not present in the new
config/tool_data.json. Running this script writes to
config/tool_knowledge_base.json, which is now an archived stub and is NOT
read by the runtime. Edits here have ZERO effect on routing.

Kept for git-history / archaeology only.

Run: python -X utf8 -u scripts/regenerate_tkb_examples.py [--concurrency 20]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from config import get_settings
from openai import AsyncAzureOpenAI

REGISTRY_PATH = REPO / "config" / "processed_tool_registry.json"
TKB_PATH = REPO / "config" / "tool_knowledge_base.json"
PROGRESS_PATH = REPO / "config" / "tool_knowledge_base.examples.progress.json"

SYSTEM_PROMPT = (
    "Ti generiraš realistic example queries za Croatian fleet-management WhatsApp bot. "
    "Korisnik (vozač ili manager u tvrtki s vozilima) tipka prirodni Croatian. "
    "Za zadani backend tool, generiraj 5 raznolikih Croatian upita koji bi se "
    "trebali rutirati na taj tool. "
    "PRAVILA: "
    "1) Miješaj formalno + kolokvijalno + chat-style + kratke fraze. "
    "2) Pokrij različite kuteve istog intenta (npr. 'kolika km', 'reci mi kilometražu', 'koliko je prešao auto'). "
    "3) Bez objašnjenja — samo realističan upit kakav bi korisnik napisao. "
    "4) Za GET tool: koristi fraze 'pokaži', 'koliko', 'reci mi', 'koji je', 'lista', 'svi'. "
    "5) Za POST/PUT/PATCH tool: koristi fraze 'dodaj', 'unesi', 'azuriraj', 'promijeni', 'evidentiraj'. "
    "6) Za DELETE tool: 'obriši', 'otkaži', 'makni', 'skini'. "
    "7) Razlikuj SINGLE od LIST od BY_ID — ako je tool po ID-u, upiti moraju spomenuti ID/specifičnu stavku. "
    "8) field_hint: ako se upit odnosi na specifično polje (npr. 'kilometraža' → field_hint='LastMileage'), zapiši ga; inače null. "
    "9) category: kratak tag (max 3 riječi snake_case): 'current_value', 'broad_summary', 'specific_date', 'alias', 'with_filter', 'mutation_intent', 'aggregate', itd. "
    'OUTPUT (čisti JSON): {"examples": [{"input": "...", "field_hint": "..." or null, "category": "..."}, ...]} '
    "Točno 5 elemenata u listi."
)


def build_user_prompt(tool: dict, tkb_entry: dict) -> str:
    op_id = tool["operation_id"]
    method = tool.get("method", "?")
    path = tool.get("path", "")
    description = (tool.get("description") or "")[:300]
    intent = (tkb_entry.get("intent_summary") or "")[:200]
    is_mutation = tool.get("is_mutation", False)
    output_keys = tool.get("output_keys", [])[:12]

    cardinality_hint = ""
    if op_id.endswith("_id") and not op_id.endswith("_metadata"):
        cardinality_hint = "Kardinalnost: BY_ID — upiti MORAJU referencirati ID/specifičnu stavku ('rezervacija 123', 'vozilo s ID-om X'). "
    elif "_Agg" in op_id or "_GroupBy" in op_id:
        cardinality_hint = "Kardinalnost: AGGREGATE/GROUPBY — upiti uključuju 'ukupno', 'prosjek', 'po grupama', 'koliko je X'. "
    elif "_metadata" in op_id:
        cardinality_hint = "Kardinalnost: METADATA — upiti pitaju o STRUKTURI tool-a, ne podacima ('koja polja', 'koje vrste'). "
    elif "_tree" in op_id:
        cardinality_hint = "Kardinalnost: HIERARCHY — upiti spominju 'stablo', 'hijerarhija'. "

    use_when_raw = tkb_entry.get("use_when") or []
    use_when_lines = []
    for item in use_when_raw[:4]:
        if isinstance(item, str):
            use_when_lines.append(f"- {item}")
        elif isinstance(item, dict):
            txt = item.get("text") or item.get("scenario") or str(item)
            use_when_lines.append(f"- {txt}")
    use_when = "\n".join(use_when_lines) or "(none)"

    out_keys = ", ".join(output_keys) if output_keys else "(none)"

    return (
        f"Tool: {op_id}\n"
        f"Method: {method} {path}\n"
        f"Mutating: {'DA' if is_mutation else 'NE'}\n"
        f"{cardinality_hint}\n"
        f"Backend description: {description}\n"
        f"Intent (Croatian): {intent}\n"
        f"Use when:\n{use_when}\n"
        f"Output keys (potential field_hint values): {out_keys}\n\n"
        "Generiraj 5 example queries za ovaj tool. Odgovori SAMO JSON objektom oblika "
        '{"examples": [...]}'
    )


async def generate_examples_for_tool(
    client, deployment: str, tool: dict, tkb_entry: dict,
    max_retries: int = 2,
) -> tuple[str, list[dict], str | None]:
    """Returns (tool_id, examples_list, error)."""
    tool_id = tool["operation_id"]
    user = build_user_prompt(tool, tkb_entry)
    for attempt in range(max_retries + 1):
        try:
            r = await client.chat.completions.create(
                model=deployment,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                temperature=0.7,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            raw = r.choices[0].message.content or ""
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                return tool_id, [], "json_not_object"
            examples = parsed.get("examples") or parsed.get("queries") or []
            if not isinstance(examples, list):
                return tool_id, [], "examples_not_list"
            # Normalize each example
            normalized = []
            for ex in examples[:5]:
                if isinstance(ex, str):
                    normalized.append({"input": ex.strip(), "field_hint": None, "category": "general"})
                elif isinstance(ex, dict):
                    inp = ex.get("input") or ex.get("query") or ex.get("q") or ""
                    if not isinstance(inp, str) or not inp.strip():
                        continue
                    fh = ex.get("field_hint")
                    if fh is not None and not isinstance(fh, str):
                        fh = str(fh)
                    cat = ex.get("category") or "general"
                    if not isinstance(cat, str):
                        cat = str(cat)
                    normalized.append({
                        "input": inp.strip(),
                        "field_hint": fh,
                        "category": cat[:40],
                    })
            if not normalized:
                return tool_id, [], "empty_examples"
            return tool_id, normalized, None
        except json.JSONDecodeError:
            if attempt == max_retries:
                return tool_id, [], "json_parse_failed"
            await asyncio.sleep(0.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            if attempt == max_retries:
                return tool_id, [], f"llm_error:{type(e).__name__}"
            await asyncio.sleep(1.0 * (attempt + 1))
    return tool_id, [], "exhausted_retries"


async def run(concurrency: int):
    s = get_settings()
    client = AsyncAzureOpenAI(
        azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
        api_key=s.AZURE_OPENAI_API_KEY,
        api_version=s.AZURE_OPENAI_API_VERSION,
        max_retries=0,
        timeout=20.0,
    )
    deployment = s.AZURE_OPENAI_DEPLOYMENT_NAME

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    tkb = json.loads(TKB_PATH.read_text(encoding="utf-8"))

    if PROGRESS_PATH.exists():
        progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        completed = set(progress.get("completed", []))
        errors = progress.get("errors", {})
    else:
        completed = set()
        errors = {}

    tools = registry["tools"]
    todo = [t for t in tools if t["operation_id"] not in completed]
    print(f"Tools total: {len(tools)} | completed: {len(completed)} | todo: {len(todo)}", flush=True)

    sem = asyncio.Semaphore(concurrency)
    counter = {"done": 0, "err": 0}
    t_start = time.time()

    async def worker(tool):
        async with sem:
            tkb_entry = tkb["tools"].get(tool["operation_id"], {})
            tid, examples, err = await generate_examples_for_tool(
                client, deployment, tool, tkb_entry,
            )
            if err:
                counter["err"] += 1
                errors[tid] = err
            else:
                counter["done"] += 1
                # Mutate TKB in-place — preserve all other fields
                if tid in tkb["tools"]:
                    tkb["tools"][tid]["examples"] = examples
                completed.add(tid)

            if (counter["done"] + counter["err"]) % 25 == 0:
                _checkpoint(tkb, completed, errors, t_start, counter, len(todo))

    await asyncio.gather(*[worker(t) for t in todo])

    _checkpoint(tkb, completed, errors, t_start, counter, len(todo), final=True)
    # Update metadata
    tkb["_meta"]["examples_regenerated_at"] = time.strftime("%Y-%m-%d")
    tkb["_meta"]["examples_source"] = "regenerated with gpt-4o-mini, Croatian-realistic prompts"
    TKB_PATH.write_text(json.dumps(tkb, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. {counter['done']} ok, {counter['err']} errors.", flush=True)
    if errors:
        print(f"First 5 error tools: {list(errors.items())[:5]}", flush=True)
    print(f"Wrote {TKB_PATH}", flush=True)


def _checkpoint(tkb, completed, errors, t_start, counter, total, final=False):
    TKB_PATH.write_text(json.dumps(tkb, ensure_ascii=False, indent=2), encoding="utf-8")
    PROGRESS_PATH.write_text(
        json.dumps({
            "completed": sorted(completed),
            "errors": errors,
            "stats": {"done": counter["done"], "err": counter["err"]},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not final:
        elapsed = time.time() - t_start
        rate = (counter["done"] + counter["err"]) / max(elapsed, 0.01)
        remaining = total - (counter["done"] + counter["err"])
        eta = remaining / max(rate, 0.01)
        print(
            f"  progress: {counter['done']} ok / {counter['err']} err / "
            f"{remaining} todo | {rate:.1f}/s | ETA {eta:.0f}s",
            flush=True,
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=20)
    args = ap.parse_args()
    asyncio.run(run(args.concurrency))


if __name__ == "__main__":
    main()
