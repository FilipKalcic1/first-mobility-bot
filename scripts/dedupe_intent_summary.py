"""FAZA P2.1 (Filip 2026-05-19): de-duplicate intent_summary across registry.

Registry audit (FAZA 15) found 271/950 tools share intent_summary with at
least one other tool. The worst offender: 35 distinct tools all describe
themselves as "Postavlja dokument kao zadani za entitet." — bot has no
way to disambiguate among them by description alone.

This script applies a deterministic, template-based rewrite:
  1. For each duplicate group, identify the underlying *template* (the
     intent_summary that repeats).
  2. Map each tool to its underlying entity (path's first capitalized
     segment, e.g. "Companies" / "Vehicles" / "Metadata").
  3. Look up Croatian entity word in config/entity_translations_hr.json.
  4. Apply template-specific rewrite using the entity word.

NO LLM. Deterministic, idempotent, atomic write. Safe to re-run.

Templates handled (top 10 from FAZA 15 audit, ~150 tools total):

| Template (before)                                                | Rewrite (after)                                       |
|------------------------------------------------------------------|-------------------------------------------------------|
| Postavlja dokument kao zadani za entitet.                        | Postavlja zadani dokument za {sg_acc}.                |
| Vraća metapodatke entiteta s određenim ID-jem.                   | Vraća metapodatke za {sg_acc} po ID-u.                |
| Vraća metapodatke entiteta prema zadanom ID-u.                   | Vraća metapodatke za {sg_acc} po ID-u.                |
| Djelomično ažurira više stavki u sustavu.                        | Djelomično ažurira više {pl_acc} odjednom.            |
| Djelomično ažurira stavku u sustavu.                             | Djelomično ažurira jedan {sg_acc} po ID-u.            |
| Ovaj alat briše stavku na temelju primarnog ključa (id).         | Briše jedan {sg_acc} po ID-u.                         |
| Dohvaća sve stavke prema parametrima učitavanja.                 | Dohvaća listu {pl_acc} s filtrima.                    |
| Dohvaća sve stavke na temelju parametara učitavanja.             | Dohvaća listu {pl_acc} s filtrima.                    |
| Alat omogućuje brisanje više stavki prema filtriranim kriterijima.| Briše više {pl_acc} prema kriterijima.               |
| Alat vraća agregirane vrijednosti na temelju zadanih filtara.    | Vraća agregirane vrijednosti za {pl_acc}.             |

Usage:
  python scripts/dedupe_intent_summary.py [--dry-run]

After run:
  - tool_data.json (Git LFS tracked) updated in-place via atomic_write_json
  - tests/ should remain green (no logic change, just metadata)
  - Re-run audit: python -c "..." to confirm duplicate count dropped
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TOOL_DATA = REPO / "config" / "tool_data.json"
ENTITY_TRANSLATIONS = REPO / "config" / "entity_translations_hr.json"

sys.path.insert(0, str(REPO))
from services.v2.atomic_io import atomic_write_json  # noqa: E402


# Path segment is the first capitalized component after "/", ignoring
# placeholder segments like {id}, {documentId}, etc.
_PLACEHOLDER_RE = re.compile(r"^\{[^}]+\}$")


def primary_entity(path: str) -> str:
    """Returns first capitalized non-placeholder segment of path.

    Examples:
      /Companies/{id}/documents/{documentId}/SetAsDefault → "Companies"
      /Metadata/multipatch → "Metadata"
      /VehicleInputHelper/PreviousVehicle/{model} → "VehicleInputHelper"
    """
    for part in (path or "").split("/"):
        if part and not _PLACEHOLDER_RE.match(part) and part[0].isupper():
            return part
    return ""


# Template → rewrite function. Each rewrite returns the new intent_summary
# given the entity's HR forms dict.
def _set_as_default(forms: dict) -> str:
    return f"Postavlja zadani dokument za {forms['sg_acc']}."


def _metadata_by_id(forms: dict) -> str:
    return f"Vraća metapodatke za {forms['sg_acc']} po ID-u."


def _multipatch(forms: dict) -> str:
    return f"Djelomično ažurira više {forms['pl_acc']} odjednom."


def _partial_update_one(forms: dict) -> str:
    return f"Djelomično ažurira jedan {forms['sg_acc']} po ID-u."


def _delete_by_id(forms: dict) -> str:
    return f"Briše jedan {forms['sg_acc']} po ID-u."


def _list_with_filters(forms: dict) -> str:
    return f"Dohvaća listu {forms['pl_acc']} s filtrima."


def _delete_by_criteria(forms: dict) -> str:
    return f"Briše više {forms['pl_acc']} prema kriterijima."


def _agg(forms: dict) -> str:
    return f"Vraća agregirane vrijednosti za {forms['pl_acc']}."


def _get_one_by_id(forms: dict) -> str:
    return f"Vraća jedan {forms['sg_acc']} po ID-u."


def _update_doc_meta(forms: dict) -> str:
    return f"Ažurira metapodatke dokumenta za {forms['sg_acc']}."


def _fleet_update(forms: dict) -> str:
    return f"Ažurira jedan {forms['sg_acc']} u sustavu."


# Map of duplicate intent_summary → rewrite function.
# Add new entries here if registry regen produces new generic templates.
DUPLICATE_TEMPLATES: dict[str, callable] = {
    "Postavlja dokument kao zadani za entitet.": _set_as_default,
    "Vraća metapodatke entiteta s određenim ID-jem.": _metadata_by_id,
    "Vraća metapodatke entiteta prema zadanom ID-u.": _metadata_by_id,
    "Vraća metapodatke entiteta s određenim ID-om.": _metadata_by_id,
    "Vraća metapodatke entiteta s određenom primarnom ključnom vrijednošću.": _metadata_by_id,
    "Vraća metapodatke entiteta s određenim primarnim ključem.": _metadata_by_id,
    "Djelomično ažurira više stavki u sustavu.": _multipatch,
    "Djelomično ažurira više stavki odjednom.": _multipatch,
    "Djelomično ažurira stavku u sustavu.": _partial_update_one,
    "Djelomično ažurira stavku prema zadanom ID-u.": _partial_update_one,
    "Ovaj alat briše stavku na temelju primarnog ključa (id).": _delete_by_id,
    "Dohvaća sve stavke prema parametrima učitavanja.": _list_with_filters,
    "Dohvaća sve stavke na temelju parametara učitavanja.": _list_with_filters,
    "Dohvaća sve stavke prema parametrima učitavanja s projekcijom rezultata.": _list_with_filters,
    "Alat omogućuje brisanje više stavki prema filtriranim kriterijima.": _delete_by_criteria,
    "Alat omogućuje brisanje više stavki prema filter kriterijima.": _delete_by_criteria,
    "Alat omogućuje brisanje više stavki prema zadanim kriterijima.": _delete_by_criteria,
    "Alat briše više stavki prema zadanim kriterijima.": _delete_by_criteria,
    "Ovaj alat briše više stavki prema zadanim kriterijima.": _delete_by_criteria,
    "Alat briše više stavki na temelju njihovih primarnih ključeva.": _delete_by_criteria,
    "Alat vraća agregirane vrijednosti na temelju zadanih filtara.": _agg,
    "Vraća pojedinačni zapis na temelju primarnog ključa.": _get_one_by_id,
    "Vraća pojedinačni zapis temeljen na ID-u.": _get_one_by_id,
    "Ažurira informacije o dokumentu na temelju ID-a entiteta i dokumenta.": _update_doc_meta,
    "Ažurira informacije o dokumentu u sustavu MobilityOne.": _update_doc_meta,
    "Ažurira stavku u sustavu za upravljanje flotom.": _fleet_update,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing.")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")

    if not TOOL_DATA.exists():
        print(f"ERROR: {TOOL_DATA} not found", file=sys.stderr)
        sys.exit(1)
    if not ENTITY_TRANSLATIONS.exists():
        print(f"ERROR: {ENTITY_TRANSLATIONS} not found", file=sys.stderr)
        sys.exit(1)

    tool_data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    entity_map = json.loads(ENTITY_TRANSLATIONS.read_text(encoding="utf-8"))
    entities = entity_map.get("entities", {})

    tools = tool_data.get("tools") or {}
    if not isinstance(tools, dict) or not tools:
        print("ERROR: tool_data.json has no 'tools' key", file=sys.stderr)
        sys.exit(1)

    rewrites: list[tuple[str, str, str]] = []  # (tool_id, old, new)
    unmatched_entities: dict[str, list[str]] = {}  # entity → tool_ids

    for tid, tool in tools.items():
        intent = tool.get("intent_summary", "")
        rewrite_fn = DUPLICATE_TEMPLATES.get(intent)
        if rewrite_fn is None:
            continue  # not a known duplicate template

        path = tool.get("path", "")
        entity = primary_entity(path)
        if not entity or entity not in entities:
            unmatched_entities.setdefault(entity or "<empty>", []).append(tid)
            continue

        forms = entities[entity]
        # Validate that the form keys this template needs are present.
        try:
            new_intent = rewrite_fn(forms)
        except KeyError as e:
            print(f"  SKIP {tid}: entity {entity} missing form {e}", file=sys.stderr)
            continue

        if new_intent != intent:
            rewrites.append((tid, intent, new_intent))

    # Report
    print(f"Rewrites prepared: {len(rewrites)}")
    print(f"Tools with unmatched entity (skipped): "
          f"{sum(len(v) for v in unmatched_entities.values())}")
    if unmatched_entities:
        print(f"  Unmatched entities (add to entity_translations_hr.json):")
        for entity, tids in sorted(unmatched_entities.items()):
            print(f"    {entity} ({len(tids)} tools): {tids[:3]}...")

    print()
    print("Sample of 10 rewrites:")
    for tid, old, new in rewrites[:10]:
        print(f"  {tid}")
        print(f"    OLD: {old}")
        print(f"    NEW: {new}")

    if args.dry_run:
        print("\nDRY RUN — no file written.")
        return

    # Apply rewrites
    for tid, _, new in rewrites:
        tools[tid]["intent_summary"] = new

    atomic_write_json(TOOL_DATA, tool_data)
    print(f"\n✓ Wrote {TOOL_DATA} ({len(rewrites)} intent_summary updated)")


if __name__ == "__main__":
    main()
