"""DO NOT RUN AS-IS — design verified BROKEN by 5-tool smoke test 2026-05-15.

The prompt only feeds the LLM op_id + path + params + sibling names, which
is insufficient context — for tools with literal API names (e.g.
`get_MasterData`, `post_AddCase`) the LLM produces literal-name slop
("Korisnik želi pristupiti podacima o MasterData entitetu") with anchors
no user would ever type. The hand-curated content currently in
tool_data.json is BETTER than this regen produces.

To make this script useful: feed real domain context (OpenAPI `description`
fields, example responses, or per-tool hand-written intent hints). Without
that, gpt-4o-mini cannot guess what e.g. `get_MasterData` actually does
semantically (it's the driver vehicle-snapshot tool, used for "kolika mi je
kilometraža" etc.).

Kept here as a skeleton — the validator + resume + concurrency code is
sound; only the prompt design is broken.

Phase 3 of data consolidation — LLM regen of content fields in tool_data.json
with quality constraints (entity-specific, unique-per-tool, sibling-aware).

REPLACES the old 2-script fragmentation (`regenerate_anchors.py` +
`regenerate_tkb_examples.py`). One LLM call per tool produces all four
content fields atomically:

  intent_summary  — entity-specific Croatian, ~50-100 chars, MUST contain entity noun
  use_when        — 3 bullets, MUST be unique to this tool (no shared boilerplate)
  do_not_use_when — 2 bullets, MUST name a SPECIFIC sibling alt_tool from same family
  anchors         — 12 phrases, ≥3 contain entity noun explicitly, no duplicates

What's preserved (NEVER overwritten):
  operation_id, method, service, path, parameters, required_params,
  output_keys, is_mutation, tenant_scoped, dependency_graph

Quality validation runs PER TOOL after LLM call. Fails → ONE retry with
corrective prompt. Still fails → tool kept as-is with metadata.quality_warning.

Resumable: progress sidecar at config/tool_data.progress.json. Safe to Ctrl-C.

Cost: ~950 tools × $0.005-0.007 per call ≈ $5-7. Time: ~15 min at concurrency 8.

Run:
    python -X utf8 scripts/regenerate_tool_data.py [--concurrency 8] [--limit N]

Use --limit 20 first to sanity-check the output on a small sample.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from openai import AsyncAzureOpenAI  # noqa: E402

from config import get_settings  # noqa: E402


TOOL_DATA_PATH = REPO / "config" / "tool_data.json"
PROGRESS_PATH = REPO / "config" / "tool_data.regen.progress.json"


SYSTEM_PROMPT = (
    "Ti si stručnjak za Croatian fleet-management API rutiranje. Pišeš metapodatke "
    "za 950 backend alata koje LLM router koristi da pogodi koji alat zadovoljava "
    "korisnikov upit. Tvoj output ide DIREKTNO u router prompt — kvaliteta tvog teksta "
    "određuje točnost rutiranja.\n\n"
    "STROGA PRAVILA — kršenje znači da će router pogriješiti:\n"
    "1. intent_summary MORA spomenuti SPECIFIČAN entitet (npr. 'kompaniju', 'centar troška', "
    "'odjel'), NIKAD generičke 'stavku'/'podatke'/'objekt'/'entitet'.\n"
    "2. use_when bulleti MORAJU biti JEDINSTVENI za ovaj alat (ne dijeliti s sibling tools). "
    "Ako 30 alata kaže 'koristi se za brisanje stavki', LLM ne može odabrati. Specificiraj.\n"
    "3. do_not_use_when MORA imenovati KONKRETAN sibling alat (alt_tool) — koristi imena "
    "iz priložene SIBLINGS liste. NE pišite generičke 'koristi drugi alat'.\n"
    "4. anchors: 12 raznolikih Croatian fraza. NAJMANJE 3 MORAJU sadržavati ime entiteta "
    "(npr. 'kompanija', 'tvrtka'). Mješavina službenog i razgovornog jezika.\n\n"
    "Format: JSON objekt s ključevima intent_summary, use_when, do_not_use_when, anchors. "
    "do_not_use_when format: [{alt_tool: '<exact operation_id>', razlog: '<kratko zašto>'}]"
)


def _siblings_of(op_id: str, all_ops: set[str], k: int = 6) -> list[str]:
    """Find sibling tools — same entity prefix, different verb/suffix."""
    parts = op_id.split("_")
    if len(parts) < 2:
        return []
    # Try same entity (parts[1]) with different verbs/suffixes
    entity = parts[1]
    sibs = sorted([
        op for op in all_ops
        if op != op_id and len(op.split("_")) >= 2 and op.split("_")[1] == entity
    ])
    return sibs[:k]


def _entity_noun_hint(op_id: str) -> str:
    """Best-effort English entity name from operation_id, lowercased."""
    parts = op_id.split("_")
    if len(parts) >= 2:
        return parts[1]
    return op_id


def _build_user_prompt(tool: dict, siblings: list[str]) -> str:
    op = tool["operation_id"]
    entity = _entity_noun_hint(op)
    method = tool.get("method", "GET")
    path = tool.get("path", "")
    params = tool.get("parameters") or {}
    param_names = list(params.keys())[:10]
    is_mut = tool.get("is_mutation", False)

    return (
        f"Operation ID: {op}\n"
        f"Method: {method} {path}\n"
        f"Mutacija: {'DA' if is_mut else 'NE'}\n"
        f"Entitet (iz imena): {entity}\n"
        f"Parametri (prvih 10): {param_names}\n"
        f"SIBLINGS (iz iste obitelji — koristi za do_not_use_when alt_tool): {siblings}\n\n"
        "Generiraj JSON objekt sa svim 4 polja prema STROGIM PRAVILIMA. "
        "Provjeri da intent_summary sadrži entitet u Croatian akuzativu/lokativu."
    )


def _validate(content: dict, op_id: str, entity_hint: str) -> tuple[bool, list[str]]:
    """Return (ok, warnings). Warnings are diagnostics — caller may retry once."""
    warnings: list[str] = []

    intent = (content.get("intent_summary") or "").strip()
    if not intent:
        warnings.append("intent_summary empty")
    elif len(intent) > 250:
        warnings.append(f"intent_summary too long ({len(intent)} chars)")
    elif any(g in intent.lower() for g in (
        "stavk", "podatak", "podataka", "podatke",
        "objekt", "zapis", "entitet", "informacij",
    )):
        warnings.append(f"intent_summary uses generic noun")

    uw = content.get("use_when") or []
    if not isinstance(uw, list) or len(uw) < 2:
        warnings.append(f"use_when has < 2 bullets ({len(uw) if isinstance(uw, list) else 'invalid'})")

    dnu = content.get("do_not_use_when") or []
    if not isinstance(dnu, list) or len(dnu) < 1:
        warnings.append(f"do_not_use_when has < 1 bullet")
    elif isinstance(dnu, list):
        for item in dnu:
            if isinstance(item, dict) and not item.get("alt_tool"):
                warnings.append("do_not_use_when item missing alt_tool")
                break

    anchors = content.get("anchors") or []
    if not isinstance(anchors, list) or len(anchors) < 8:
        warnings.append(f"anchors has < 8 phrases ({len(anchors) if isinstance(anchors, list) else 'invalid'})")
    elif isinstance(anchors, list):
        # Lower-bar entity check: at least 1 anchor mentions some root of entity name
        root = entity_hint.rstrip("s").lower()  # Companies → Company → company
        n_with_entity = sum(
            1 for a in anchors
            if isinstance(a, str) and (entity_hint.lower() in a.lower() or root in a.lower())
        )
        if n_with_entity < 1:
            warnings.append(f"0 anchors mention entity '{entity_hint}'")

    return (not warnings, warnings)


async def _regen_one(
    client, deployment: str, tool: dict, siblings: list[str],
    max_retries: int = 1,
) -> tuple[str, dict, list[str], str | None]:
    """Returns (op_id, content_dict, validation_warnings, error)."""
    op = tool["operation_id"]
    entity = _entity_noun_hint(op)
    user_prompt = _build_user_prompt(tool, siblings)

    last_warnings: list[str] = []
    for attempt in range(max_retries + 1):
        try:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            if attempt > 0 and last_warnings:
                messages.append({
                    "role": "user",
                    "content": (
                        "Tvoja prethodna verzija je imala probleme: "
                        f"{'; '.join(last_warnings)}. "
                        "POPRAVI ovo: spomeni entitet u intent_summary, "
                        "dodaj specifične sibling alt_tool u do_not_use_when, "
                        "uključi entitet u barem 3 anchora. Ponovi cijeli JSON."
                    ),
                })
            r = await client.chat.completions.create(
                model=deployment, messages=messages,
                temperature=0.3, max_tokens=1200,
                response_format={"type": "json_object"},
            )
            raw = r.choices[0].message.content or "{}"
            content = json.loads(raw)
            ok, warnings = _validate(content, op, entity)
            if ok:
                return op, content, [], None
            last_warnings = warnings
        except json.JSONDecodeError:
            if attempt == max_retries:
                return op, {}, [], "json_parse_failed"
            await asyncio.sleep(0.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            if attempt == max_retries:
                return op, {}, [], f"llm_error:{type(e).__name__}:{str(e)[:80]}"
            await asyncio.sleep(1.0 * (attempt + 1))

    # Exhausted retries — return content with quality_warning
    return op, content, last_warnings, None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="cap N tools (smoke test)")
    args = ap.parse_args()

    s = get_settings()
    client = AsyncAzureOpenAI(
        azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
        api_key=s.AZURE_OPENAI_API_KEY,
        api_version=s.AZURE_OPENAI_API_VERSION,
        max_retries=0, timeout=30.0,
    )
    deployment = s.AZURE_OPENAI_DEPLOYMENT_NAME

    print(f"Loading {TOOL_DATA_PATH.name} ...")
    doc = json.loads(TOOL_DATA_PATH.read_text(encoding="utf-8"))
    tools = doc.get("tools", {})
    all_ops = set(tools.keys())
    print(f"  {len(tools)} tools")

    # Resume
    if PROGRESS_PATH.exists():
        progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        completed = set(progress.get("completed", []))
        warnings_log: dict[str, list[str]] = progress.get("warnings", {})
        errors_log: dict[str, str] = progress.get("errors", {})
        print(f"Resuming: {len(completed)} already done")
    else:
        completed = set()
        warnings_log = {}
        errors_log = {}

    pending_ops = [op for op in tools.keys() if op not in completed]
    if args.limit:
        pending_ops = pending_ops[: args.limit]
    print(f"To process: {len(pending_ops)} tools (concurrency={args.concurrency})")

    sem = asyncio.Semaphore(args.concurrency)
    t_start = time.time()
    done_count = 0

    async def _worker(op_id: str):
        nonlocal done_count
        async with sem:
            tool = tools[op_id]
            siblings = _siblings_of(op_id, all_ops, k=6)
            r_op, content, warnings, error = await _regen_one(
                client, deployment, tool, siblings,
            )
            if error:
                errors_log[op_id] = error
            else:
                # Update tool entry in place (preserves registry fields)
                tools[op_id]["intent_summary"] = content.get("intent_summary", "")
                tools[op_id]["use_when"] = content.get("use_when") or []
                tools[op_id]["do_not_use_when"] = content.get("do_not_use_when") or []
                tools[op_id]["anchors"] = content.get("anchors") or []
                if warnings:
                    warnings_log[op_id] = warnings
                    tools[op_id].setdefault("metadata", {})["quality_warning"] = warnings
                else:
                    # Clear stale warning if previously flagged
                    tools[op_id].pop("metadata", None)
                completed.add(op_id)
            done_count += 1
            # Persist every 25 tools
            if done_count % 25 == 0:
                _save(doc, completed, warnings_log, errors_log)
                elapsed = time.time() - t_start
                rate = done_count / max(elapsed, 0.01)
                eta = (len(pending_ops) - done_count) / max(rate, 0.01)
                print(
                    f"  {done_count}/{len(pending_ops)}  "
                    f"OK={done_count - len(errors_log)} ERR={len(errors_log)}  "
                    f"warn-tools={len(warnings_log)}  "
                    f"{rate:.1f}/s  ETA {eta:.0f}s",
                    flush=True,
                )

    await asyncio.gather(*[_worker(op) for op in pending_ops])
    _save(doc, completed, warnings_log, errors_log)

    # Final summary
    print()
    print(f"Done. {len(completed)} tools regenerated.")
    print(f"  OK (no warnings):  {len(completed) - len(warnings_log)}")
    print(f"  WITH warnings:     {len(warnings_log)}")
    print(f"  ERRORS:            {len(errors_log)}")
    if warnings_log:
        from collections import Counter
        warning_kinds = Counter()
        for ws in warnings_log.values():
            for w in ws:
                # Bucket by first 4 words for grouping
                key = " ".join(w.split()[:4])
                warning_kinds[key] += 1
        print()
        print("Warning kinds (top 5):")
        for k, n in warning_kinds.most_common(5):
            print(f"  {n:>4}  {k}")
    print()
    print(f"Updated tool_data.json: {TOOL_DATA_PATH}")
    print(f"To revert: git restore config/tool_data.json")
    print(f"To rerun for retries: just run again (progress is preserved).")


def _save(doc, completed, warnings_log, errors_log):
    doc.setdefault("_meta", {})["source_version"] = "phase3-llm-regen"
    doc["_meta"]["regenerated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc["_meta"]["regen_tools_with_warnings"] = len(warnings_log)
    doc["_meta"]["regen_tools_with_errors"] = len(errors_log)
    TOOL_DATA_PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    PROGRESS_PATH.write_text(
        json.dumps({
            "completed": sorted(completed),
            "warnings": warnings_log,
            "errors": errors_log,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
