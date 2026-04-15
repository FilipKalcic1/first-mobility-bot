"""Day 1 synonym injection.

Pattern-based additions to `synonyms_hr` in config/tool_documentation.json,
driven by miss analysis of seed=42 rank=-1 cohort.

Each pattern matches on `tool_id` substring. Phrases are appended, not replaced.
Idempotent: skips phrases already present.

Run:
    python scripts/inject_synonyms_day1.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = ROOT / "config" / "tool_documentation.json"
BACKUP_PATH = ROOT / "config" / "tool_documentation.json.pre-day1.bak"
EMBEDDINGS_CACHE = ROOT / ".cache" / "tool_embeddings.json"


# Pattern -> phrases to append. Order matters only for logging.
# Phrases cover HR-language terms used in failing paraphrases that aren't in
# any current synonyms_hr. Kept short (2-4 words) to avoid BM25 dilution.
PATTERNS: list[tuple[str, list[str]]] = [
    # Sub-document CRUD (covers both _id_documents and *_documentId variants)
    ("_documents", [
        "spis akt isprava potvrda",
        "zapisnik obrazac priložena datoteka",
        "privitak vezan uz zapis",
    ]),
    # Aggregation endpoints
    ("_Agg", [
        "agregacija sažetak zbroj",
        "ukupne vrijednosti prosjek",
        "analiza skupina statistika",
    ]),
    # GroupBy endpoints
    ("_GroupBy", [
        "grupiranje klasificirano po kategoriji",
        "podjela u skupine organizirano",
        "razvrstavanje prema kriteriju",
    ]),
    # Projection endpoints (select columns)
    ("_ProjectTo", [
        "projekcija odabranih atributa",
        "selektivne kolone polja",
        "prikaz s odabranim atributima",
    ]),
    # Multipatch (bulk update)
    ("_multipatch", [
        "masovno ažuriranje više odjednom",
        "istovremena izmjena više zapisa",
        "bulk update simultano",
    ]),
    # DeleteByCriteria (bulk delete)
    ("_DeleteByCriteria", [
        "masovno brisanje po kriteriju",
        "uklanjanje više zapisa prema uvjetu",
    ]),
    # SetAsDefault
    ("_SetAsDefault", [
        "postavi kao zadano glavno",
        "označi kao primarno osnovno",
        "definiraj kao prioritetno",
    ]),
    # Thumbnails
    ("_thumb", [
        "minijatura sličica preview",
        "vizualni pregled umanjenog prikaza",
    ]),
    # Metadata endpoints (suffix, distinguishes from _metadata path-segment variants)
    ("_metadata", [
        "metapodaci atributi svojstva",
        "konfiguracijski opis polja",
    ]),
    # Lookup endpoints (prefix). Add common "dropdown/izlistaj" terms.
    ("_Lookup_", [
        "izlistaj popis dropdown izbornik",
        "brzi izbor iz predefinirane liste",
    ]),
    # LatestX endpoints (latest/nedavni/novi)
    ("Latest", [
        "najnovije nedavno posljednje",
        "trenutni aktualni status",
    ]),
]


def main() -> int:
    if not DOC_PATH.exists():
        raise SystemExit(f"FATAL: {DOC_PATH} not found")

    # Backup
    if not BACKUP_PATH.exists():
        BACKUP_PATH.write_text(DOC_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backup written: {BACKUP_PATH}")

    docs = json.loads(DOC_PATH.read_text(encoding="utf-8"))

    # Track adds per pattern for reporting
    counts: dict[str, int] = {pat: 0 for pat, _ in PATTERNS}
    per_tool_adds: dict[str, int] = {}

    for tool_id, doc in docs.items():
        if not isinstance(doc, dict):
            continue
        syns = doc.setdefault("synonyms_hr", [])
        existing_norm = {s.strip().lower() for s in syns if isinstance(s, str)}

        for pattern, phrases in PATTERNS:
            if pattern not in tool_id:
                continue
            for phrase in phrases:
                if phrase.lower() in existing_norm:
                    continue
                syns.append(phrase)
                existing_norm.add(phrase.lower())
                counts[pattern] = counts.get(pattern, 0) + 1
                per_tool_adds[tool_id] = per_tool_adds.get(tool_id, 0) + 1

    DOC_PATH.write_text(json.dumps(docs, indent=2, ensure_ascii=False), encoding="utf-8")

    # Invalidate embedding cache — synonyms are baked into the embedding text,
    # so stale vectors would silently undo this run's gains.
    if EMBEDDINGS_CACHE.exists():
        EMBEDDINGS_CACHE.unlink()
        print(f"    invalidated embeddings cache: {EMBEDDINGS_CACHE}")

    total_adds = sum(counts.values())
    tools_touched = len(per_tool_adds)
    print(f"OK  wrote {DOC_PATH}")
    print(f"    total phrase additions: {total_adds}")
    print(f"    tools touched: {tools_touched}")
    print("    per-pattern:")
    for pattern, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"      {n:5d}  {pattern}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
