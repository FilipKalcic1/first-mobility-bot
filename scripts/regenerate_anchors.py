"""DEPRECATED 2026-05-15 — replaced by editing config/tool_data.json directly.

After Phase 2 consolidation, anchors live inside tool_data.json. Running this
script writes to config/tool_anchor_enrichments.json, which is now an
archived stub and is NOT read by the runtime. Edits made here will have ZERO
effect on routing.

Also see: a Phase 3 attempt to regenerate all 4 content fields from op_id +
path + sibling list produced literal-name slop (e.g. anchors mentioning
"MasterData" instead of "kilometraža") and regressed accuracy on a 5-tool
smoke test. The hand-curated content currently in tool_data.json is better
than what context-blind LLM regen produces. Improve tool_data.json by hand,
or feed real domain context (OpenAPI descriptions, example responses) into a
future regen script.

Run: python -X utf8 scripts/regenerate_anchors.py [--concurrency 20]
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from openai import AsyncAzureOpenAI
from config import get_settings

REGISTRY_PATH = REPO / "config" / "processed_tool_registry.json"
TKB_PATH = REPO / "config" / "tool_knowledge_base.json"
OUT_PATH = REPO / "config" / "tool_anchor_enrichments.json"
PROGRESS_PATH = REPO / "config" / "tool_anchor_enrichments.progress.json"

SYSTEM_PROMPT = (
    "Ti generiraš anchor phrases (sidrene fraze) za Croatian fleet-management WhatsApp bot. "
    "Anchor je kratka Croatian fraza (3-12 riječi) koja oponaša ono što bi korisnik "
    "stvarno upisao da pozove dani tool. Tool je backend endpoint MobilityOne sustava. "
    "Vector embedding tih anchora se koristi za cosine-similarity retrieval — "
    "korisnikov upit se uspoređuje s anchorima i top-K toolova ide u sljedeću "
    "fazu LLM dispatch-a. "
    "PRAVILA: "
    "1) Generiraj 12 raznolikih fraza pokrivajući akciju (GET/POST/...), entitet i kardinalnost. "
    "2) Miješaj formalne i kolokvijalne fraze. "
    "3) Uključi i kratke (3-5 riječi) i duge (8-12 riječi). "
    "4) Koristi prirodni Croatian — bez literalnog prevoda engl. termina. "
    "5) Pokrij sinonime entiteta (vozilo/auto/automobil, vozač/driver, rezervacija/booking, etc.). "
    "6) Razlikuj single (jedan) od list (više) od by_id (po ID-u) varijante u frazama. "
    "7) Bez znakova navoda, bez markdown, bez objašnjenja — samo JSON lista stringova. "
    "OUTPUT: čisti JSON array od 12 stringa, npr. "
    '["fraza 1", "fraza 2", ...]'
)


def build_user_prompt(tool: dict, tkb_entry: dict) -> str:
    """Compose tool context for the anchor prompt."""
    op_id = tool["operation_id"]
    method = tool.get("method", "?")
    path = tool.get("path", "")
    description = (tool.get("description") or "")[:300]
    intent = (tkb_entry.get("intent_summary") or "")[:200]
    use_when_raw = tkb_entry.get("use_when") or []
    use_when_lines = []
    for item in use_when_raw[:5]:
        if isinstance(item, str):
            use_when_lines.append(f"- {item}")
        elif isinstance(item, dict):
            txt = item.get("text") or item.get("scenario") or str(item)
            use_when_lines.append(f"- {txt}")
    use_when = "\n".join(use_when_lines) or "(none)"

    do_not_raw = tkb_entry.get("do_not_use_when") or []
    do_not_lines = []
    for item in do_not_raw[:3]:
        if isinstance(item, dict):
            alt = item.get("alt_tool", "")
            reason = item.get("razlog") or item.get("reason") or ""
            do_not_lines.append(f"- ne kad: {reason} (umjesto: {alt})")
        elif isinstance(item, str):
            do_not_lines.append(f"- {item}")
    do_not = "\n".join(do_not_lines) or "(none)"

    is_mutation = tool.get("is_mutation", False)
    cardinality_hint = ""
    if "_id" in op_id and not op_id.endswith("_metadata"):
        cardinality_hint = " | Kardinalnost: BY_ID (specifična stavka po ID-u)"
    elif "_Agg" in op_id or "_GroupBy" in op_id:
        cardinality_hint = " | Kardinalnost: AGGREGATE/GROUPBY"
    elif "_metadata" in op_id:
        cardinality_hint = " | Kardinalnost: METADATA (struktura/polja)"

    return (
        f"Tool: {op_id}\n"
        f"Method: {method} {path}\n"
        f"Mutating: {'DA' if is_mutation else 'NE'}{cardinality_hint}\n"
        f"Backend description: {description}\n"
        f"Intent (Croatian): {intent}\n"
        f"Use when:\n{use_when}\n"
        f"Do NOT use when:\n{do_not}\n\n"
        "Generiraj 12 anchor fraza koje pokrivaju ono što bi korisnik stvarno rekao "
        "za ovaj endpoint. Odgovori SAMO JSON array."
    )


async def generate_anchors_for_tool(
    client, deployment: str, tool: dict, tkb_entry: dict,
    max_retries: int = 2,
) -> tuple[str, list[str], str | None]:
    """Returns (tool_id, anchors, error). On error, anchors is empty."""
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
                temperature=0.7,  # higher for diversity across phrases
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            raw = r.choices[0].message.content or ""
            # response_format=json_object enforces dict output; we asked for
            # bare array. Handle three shapes:
            #   {"anchors": [...]} / {"phrases": [...]}  — list-valued field
            #   {"phrase_1": "...", "phrase_2": "..."}   — flat dict of strings
            #   {bare list} — would only happen if SDK didn't enforce dict
            parsed = json.loads(raw)
            phrases: list[str] = []
            if isinstance(parsed, list):
                phrases = [str(p).strip() for p in parsed if str(p).strip()]
            elif isinstance(parsed, dict):
                # First try: a single list-valued field
                for v in parsed.values():
                    if isinstance(v, list):
                        phrases = [str(p).strip() for p in v if str(p).strip()]
                        if phrases:
                            break
                if not phrases:
                    # Fallback: collect all string values from the dict
                    phrases = [
                        str(v).strip()
                        for v in parsed.values()
                        if isinstance(v, str) and v.strip()
                    ]
            if not phrases:
                return tool_id, [], "json_not_array"
            phrases = phrases[:15]  # cap
            return tool_id, phrases, None
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
    # Fresh AsyncAzureOpenAI (skip the rate-guarded singleton — it's
    # designed for production traffic, and the singleton's event-loop
    # binding causes hangs in standalone scripts).
    client = AsyncAzureOpenAI(
        azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
        api_key=s.AZURE_OPENAI_API_KEY,
        api_version=s.AZURE_OPENAI_API_VERSION,
        max_retries=0,
        timeout=20.0,
    )
    deployment = s.AZURE_OPENAI_DEPLOYMENT_NAME

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    tkb = json.loads(TKB_PATH.read_text(encoding="utf-8"))["tools"]

    # Load existing output to resume
    if OUT_PATH.exists():
        existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    else:
        existing = {}

    if PROGRESS_PATH.exists():
        progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        completed = set(progress.get("completed", []))
        errors = progress.get("errors", {})
    else:
        completed = set()
        errors = {}

    tools = registry["tools"]
    todo = [t for t in tools if t["operation_id"] not in completed]
    print(f"Tools total: {len(tools)} | completed: {len(completed)} | todo: {len(todo)}")

    sem = asyncio.Semaphore(concurrency)
    results: dict[str, list[str]] = dict(existing)
    counter = {"done": 0, "err": 0}
    t_start = time.time()

    async def worker(tool):
        async with sem:
            tkb_entry = tkb.get(tool["operation_id"], {})
            tid, phrases, err = await generate_anchors_for_tool(
                client, deployment, tool, tkb_entry,
            )
            if err:
                counter["err"] += 1
                errors[tid] = err
            else:
                counter["done"] += 1
                results[tid] = phrases
                completed.add(tid)

            if (counter["done"] + counter["err"]) % 25 == 0:
                # Periodic checkpoint
                _checkpoint(results, completed, errors, t_start, counter, len(todo))

    await asyncio.gather(*[worker(t) for t in todo])

    _checkpoint(results, completed, errors, t_start, counter, len(todo), final=True)
    print(f"\nDone. {counter['done']} ok, {counter['err']} errors.")
    if errors:
        print(f"First 5 error tools: {list(errors.items())[:5]}")
    print(f"Wrote {OUT_PATH}")


def _checkpoint(results, completed, errors, t_start, counter, total, final=False):
    OUT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
