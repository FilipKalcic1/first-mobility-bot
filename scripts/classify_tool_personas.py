"""Phase A2 — Strict-prompt persona classification of all 950 tools.

The archived `personas` field tagged 87% of tools as "driver" because the
old prompt asked "could a driver ever invoke this in some flow." That's
too permissive — it doesn't help the router narrow the candidate set.

This script re-classifies with a TIGHTER question:
  "Bi li ne-tehnički korisnik (vozač / manager / admin) direktno pitao
   o ovom alatu preko WhatsApp poruke u svom prirodnom jeziku?"

Output: writes `personas_strict` (subset of {driver, manager, admin, internal})
and `persona_reason` (1-sentence) per tool into config/tool_data.json.

Resumable via config/personas_classify.progress.json.

Cost: ~$3 (gpt-4o-mini @ ~500 tokens per tool × 950). Time: ~10-15 min @
concurrency 10.

Run:
    python -X utf8 scripts/classify_tool_personas.py [--limit N] [--concurrency 10] [--dry-run]
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

from openai import AsyncAzureOpenAI  # noqa: E402

from config import get_settings  # noqa: E402

TOOL_DATA_PATH = REPO / "config" / "tool_data.json"
PROGRESS_PATH = REPO / "config" / "personas_classify.progress.json"

ALLOWED = {"driver", "manager", "admin", "internal"}

SYSTEM_PROMPT = (
    "Ti si stručnjak za fleet-management API. Za svaki alat odlučuješ koje "
    "PERSONE bi ga DIREKTNO pitale preko WhatsApp poruke u prirodnom jeziku.\n\n"
    "Persone:\n"
    "- driver: vozač, ne-tehnički, pita o SVOM vlastitom autu/kilometraži/troškovima, "
    "prijavljuje kvar, traži rezervaciju\n"
    "- manager: voditelj flote, gleda agregirane statistike za skupinu vozila, "
    "odobrava/odbija zahtjeve, mijenja postavke vozila\n"
    "- admin: tenant-admin, mijenja korisnike/role/team-ove, postavlja master-data "
    "(kategorije, grupe), GDPR operacije\n"
    "- internal: alat NIJE namijenjen direktnom WhatsApp pitanju — koristi se samo "
    "interno (form-dropdown values, validation helpers, paginators, _ProjectTo "
    "varijante, _DistinctXxx helpers, _Agg interno korišteni od managera ali "
    "user nikad ne pita to izravno)\n\n"
    "STROGA PRAVILA:\n"
    "1. Pojedini alat može biti u VIŠE persona (npr. [driver, manager]) — ako "
    "OBJE persone realno pitaju to PRIRODNO.\n"
    "2. Ako alat ima riječi 'Distinct', 'Helper', 'ProjectTo', '_Agg', '_metadata' u "
    "imenu → vjerojatno 'internal' osim ako jasno user-facing.\n"
    "3. NE budi permisivan. 'Mogao bi se koristiti od driver-a u nekom flow-u' nije "
    "isto kao 'driver bi to pitao'. Ako sumnjaš — 'internal'.\n"
    "4. Razlog mora biti 1 rečenica, konkretan (npr. 'driver pita o svojoj registraciji', "
    "ne 'opće informacije o vozilu').\n\n"
    'Output: JSON {"personas_strict": ["driver"], "persona_reason": "..."}. '
    'personas_strict mora biti podskup od {driver, manager, admin, internal}.'
)


def _build_user_prompt(tool: dict) -> str:
    op = tool["operation_id"]
    method = tool.get("method", "GET")
    path = tool.get("path", "")
    is_mut = tool.get("is_mutation", False)
    intent = (tool.get("intent_summary") or "").strip()
    use_when = tool.get("use_when") or []
    params = list((tool.get("parameters") or {}).keys())[:8]

    return (
        f"Operation: {op}\n"
        f"HTTP: {method} {path}\n"
        f"Mutacija: {'DA' if is_mut else 'NE'}\n"
        f"Parametri (prvih 8): {params}\n"
        f"intent_summary: {intent}\n"
        f"use_when: {use_when}\n\n"
        "Koje persone direktno pitaju o ovom alatu preko WhatsApp poruke? "
        "Vrati JSON s personas_strict + persona_reason."
    )


def _normalize_classification(raw: dict) -> tuple[list[str], str]:
    """Coerce LLM output into validated (personas, reason) tuple."""
    personas = raw.get("personas_strict") or raw.get("personas") or []
    if not isinstance(personas, list):
        personas = [personas] if isinstance(personas, str) else []
    # Lowercase, dedupe, filter to allowed set
    seen = []
    for p in personas:
        if not isinstance(p, str):
            continue
        p_norm = p.strip().lower()
        if p_norm in ALLOWED and p_norm not in seen:
            seen.append(p_norm)
    if not seen:
        seen = ["internal"]  # safest default
    reason = raw.get("persona_reason") or raw.get("reason") or ""
    if not isinstance(reason, str):
        reason = ""
    return seen, reason.strip()[:300]


async def _classify_one(client, deployment: str, tool: dict) -> tuple[str, list[str], str, str | None]:
    """Returns (op_id, personas, reason, error)."""
    op = tool["operation_id"]
    try:
        r = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(tool)},
            ],
            temperature=0.1,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = r.choices[0].message.content or "{}"
        data = json.loads(raw)
        personas, reason = _normalize_classification(data)
        return op, personas, reason, None
    except json.JSONDecodeError:
        return op, ["internal"], "json_parse_failed", "json_parse_failed"
    except Exception as e:  # noqa: BLE001
        return op, ["internal"], f"error: {type(e).__name__}", f"{type(e).__name__}:{str(e)[:80]}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="cap N tools (smoke test)")
    ap.add_argument("--dry-run", action="store_true", help="print outputs without writing tool_data.json")
    args = ap.parse_args()

    s = get_settings()
    client = AsyncAzureOpenAI(
        azure_endpoint=s.AZURE_OPENAI_ENDPOINT,
        api_key=s.AZURE_OPENAI_API_KEY,
        api_version=s.AZURE_OPENAI_API_VERSION,
        max_retries=0,
        timeout=30.0,
    )
    deployment = s.AZURE_OPENAI_DEPLOYMENT_NAME

    print(f"Loading {TOOL_DATA_PATH.name} ...")
    doc = json.loads(TOOL_DATA_PATH.read_text(encoding="utf-8"))
    tools = doc.get("tools", {})
    print(f"  {len(tools)} tools  (deployment: {deployment})")

    # Resume
    if PROGRESS_PATH.exists() and not args.dry_run:
        progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        completed = set(progress.get("completed", []))
        errors_log = progress.get("errors", {})
        print(f"Resuming: {len(completed)} already done")
    else:
        completed = set()
        errors_log = {}

    pending = [op for op in tools.keys() if op not in completed]
    if args.limit:
        pending = pending[: args.limit]
    print(f"To process: {len(pending)} tools (concurrency={args.concurrency}, dry_run={args.dry_run})")

    sem = asyncio.Semaphore(args.concurrency)
    t_start = time.time()
    done_count = 0
    persona_counter = {p: 0 for p in ALLOWED}

    async def _worker(op_id: str):
        nonlocal done_count
        async with sem:
            op, personas, reason, error = await _classify_one(client, deployment, tools[op_id])
            if error:
                errors_log[op_id] = error
            else:
                if args.dry_run:
                    print(f"  {op:<55} → {personas}  | {reason[:80]}")
                else:
                    tools[op_id]["personas_strict"] = personas
                    tools[op_id]["persona_reason"] = reason
                    completed.add(op_id)
                for p in personas:
                    persona_counter[p] = persona_counter.get(p, 0) + 1
            done_count += 1
            if done_count % 50 == 0 and not args.dry_run:
                _save(doc, completed, errors_log)
                elapsed = time.time() - t_start
                rate = done_count / max(elapsed, 0.01)
                eta = (len(pending) - done_count) / max(rate, 0.01)
                print(
                    f"  {done_count}/{len(pending)}  ERR={len(errors_log)}  "
                    f"{rate:.1f}/s  ETA {eta:.0f}s",
                    flush=True,
                )

    await asyncio.gather(*[_worker(op) for op in pending])
    if not args.dry_run:
        _save(doc, completed, errors_log)

    print()
    print(f"Done. Processed: {len(pending)}")
    print(f"  errors: {len(errors_log)}")
    print()
    print("Persona distribution (multi-tag possible):")
    for p in ("driver", "manager", "admin", "internal"):
        n = persona_counter.get(p, 0)
        pct = n * 100 // max(len(pending), 1)
        print(f"  {p:<10} {n:>4} ({pct}%)")
    if args.dry_run:
        print()
        print("DRY RUN — tool_data.json NOT modified.")
    else:
        print()
        print(f"Updated tool_data.json: {TOOL_DATA_PATH}")
        print(f"To revert: cp config/tool_data.json.bak.pre-phase-A config/tool_data.json")


def _save(doc, completed, errors_log):
    doc.setdefault("_meta", {})["personas_strict_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    doc["_meta"]["personas_strict_errors"] = len(errors_log)
    TOOL_DATA_PATH.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    PROGRESS_PATH.write_text(
        json.dumps({
            "completed": sorted(completed),
            "errors": errors_log,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
