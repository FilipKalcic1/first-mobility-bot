"""
Offline: enrich `example_queries_hr` in tool_documentation.json.

Hypothesis under test
---------------------
Ada-002 is weak on Croatian tooling queries because our indexed text is keyword
stuffing (synonyms_hr word-lists) while real user queries are natural sentences.
Generating 6–10 realistic user paraphrases per tool and indexing those as the
primary embedding text closes the query↔doc surface-form gap.

Method
------
For each tool, prompt gpt-4o-mini with: tool_id, HTTP method, purpose, entity
key, existing example_queries_hr. Ask for 6–10 diverse natural Croatian user
phrasings — polite/informal, imperative/question, short/long, with and without
the literal entity noun (synonym coverage happens through sentences, not word
lists). Write back to tool_documentation.json.

Idempotent: skip tools that already have ≥target_count entries.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = PROJECT_ROOT / "config" / "tool_documentation.json"
BACKUP_PATH = DOC_PATH.with_suffix(".pre_enrichment_backup.json")

load_dotenv(PROJECT_ROOT / ".env")

_METHOD_HR = {
    "get": "GET (čitanje/dohvat podataka)",
    "post": "POST (kreiranje novog resursa)",
    "put": "PUT (potpuno ažuriranje)",
    "patch": "PATCH (djelomično ažuriranje)",
    "delete": "DELETE (brisanje)",
}


def _entity_key(tool_id: str) -> str:
    name = re.sub(r"^(get|post|put|patch|delete)_", "", tool_id, flags=re.IGNORECASE)
    return name.split("_")[0].lower() if name else ""


def _method(tool_id: str) -> str:
    return tool_id.split("_")[0].lower()


def _build_prompt(tool_id: str, doc: Dict, target_count: int) -> str:
    method = _method(tool_id)
    entity = _entity_key(tool_id)
    purpose = (doc.get("purpose") or "").strip()
    existing = doc.get("example_queries_hr") or []
    existing_block = "\n".join(f"- {q}" for q in existing) if existing else "(nema)"

    return f"""Generiraj {target_count} različitih, prirodnih hrvatskih rečenica kojima bi korisnik tražio ovaj API alat u razgovoru s chatbotom.

Alat: {tool_id}
HTTP metoda: {_METHOD_HR.get(method, method.upper())}
Entitet (ključ): {entity}
Svrha (HR): {purpose}
Postojeće rečenice:
{existing_block}

PRAVILA:
1. Svaka rečenica mora jasno implicirati {method.upper()} operaciju (npr. za DELETE koristi "obriši/izbriši/makni/ukloni/riješi se", za GET "pokaži/dohvati/daj/koji/koliko", itd).
2. Svaka rečenica mora pokrivati taj entitet, ali varira jezik: koristi sinonime u REČENICAMA (ne popis riječi), kolokvijalne oblike, različita stilska registra (formalno/neformalno), upitne i imperativne forme, duge i kratke.
3. Ne koristi engleske riječi. Koristi hrvatsku dijakritiku (š, đ, č, ć, ž).
4. Ako alat ima ID ili parametar, povremeno ga spomeni konkretno ("s ID-om 5", "broj 12").
5. Ne ponavljaj postojeće rečenice.
6. NE pišeš komentare, objašnjenja, numeraciju — samo rečenice, svaka u novom redu.

Odgovori SAMO s rečenicama (bez numeracije, bez crtica, bez JSON-a)."""


async def _generate_for_tool(
    client: AsyncAzureOpenAI,
    deployment: str,
    tool_id: str,
    doc: Dict,
    target_count: int,
    semaphore: asyncio.Semaphore,
) -> Optional[List[str]]:
    async with semaphore:
        try:
            resp = await client.chat.completions.create(
                model=deployment,
                messages=[
                    {"role": "system", "content": "Ti si hrvatski jezični ekspert za korisničke upite prema API alatima. Generiraš raznolike, prirodne rečenice."},
                    {"role": "user", "content": _build_prompt(tool_id, doc, target_count)},
                ],
                temperature=0.8,
                max_tokens=800,
            )
        except Exception as e:
            print(f"  [ERR] {tool_id}: {e}", flush=True)
            return None

    text = (resp.choices[0].message.content or "").strip()
    lines = [ln.strip(" -*•\t") for ln in text.splitlines() if ln.strip()]
    lines = [re.sub(r"^\d+[\.\)]\s*", "", ln) for ln in lines]
    lines = [ln for ln in lines if 3 <= len(ln.split()) <= 30]
    seen = set()
    out = []
    for ln in lines:
        k = ln.lower().rstrip(".!?")
        if k not in seen:
            seen.add(k)
            out.append(ln)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-count", type=int, default=8,
                    help="Desired example_queries_hr per tool after enrichment")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="Only process first N tools (0=all)")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate even if tool already has enough")
    args = ap.parse_args()

    with open(DOC_PATH, "r", encoding="utf-8") as f:
        docs = json.load(f)

    if not BACKUP_PATH.exists():
        BACKUP_PATH.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Backup written: {BACKUP_PATH.name}", flush=True)

    candidates = []
    for tid, d in docs.items():
        current = len(d.get("example_queries_hr") or [])
        if args.force or current < args.target_count:
            candidates.append(tid)
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"Tools to enrich: {len(candidates)}/{len(docs)} (target ≥{args.target_count} queries each)", flush=True)
    if not candidates:
        return 0

    client = AsyncAzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    )
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]

    sem = asyncio.Semaphore(args.concurrency)
    tasks = [
        asyncio.create_task(_generate_for_tool(client, deployment, tid, docs[tid], args.target_count, sem))
        for tid in candidates
    ]

    t0 = time.time()
    done = 0
    errored = 0
    last_checkpoint = time.time()
    for coro, tid in zip(asyncio.as_completed(tasks), candidates):
        new_queries = await coro
        done += 1
        if new_queries is None:
            errored += 1
        else:
            existing = docs[tid].get("example_queries_hr") or []
            seen_norm = {q.lower().rstrip(".!?") for q in existing}
            merged = list(existing)
            for q in new_queries:
                k = q.lower().rstrip(".!?")
                if k not in seen_norm:
                    seen_norm.add(k)
                    merged.append(q)
            if len(merged) > args.target_count:
                merged = merged[: args.target_count]
            docs[tid]["example_queries_hr"] = merged

        if done % 25 == 0 or done == len(candidates):
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(candidates) - done) / rate if rate > 0 else 0
            print(f"  [{done}/{len(candidates)}] errored={errored}  {rate:.1f}/s  ETA {eta:.0f}s", flush=True)

        if time.time() - last_checkpoint > 60:
            with open(DOC_PATH, "w", encoding="utf-8") as f:
                json.dump(docs, f, ensure_ascii=False, indent=2)
            last_checkpoint = time.time()
            print(f"  [checkpoint] saved at {done}/{len(candidates)}", flush=True)

    with open(DOC_PATH, "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False, indent=2)
    print(f"Done. Updated {len(candidates) - errored} tools. Wall: {time.time() - t0:.0f}s", flush=True)
    return 0 if errored == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
