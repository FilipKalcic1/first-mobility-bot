"""Template-driven anchor generator (Faza 7B).

For each non-internal tool in `config/tool_data.json`, generates 6 anchors
using vocabulary from `services/router/anchor_vocab.py`:

  1. imperative:        "{verb} {entity}"           (e.g. "briši rezervaciju")
  2. imperative_id:     "{verb} {entity} po id"     (only if path has {id})
  3. context:           "{verb} {entity} {modifier}" (with secondary entity)
  4. indirect:          "treba mi {verb_inf} {entity}"
  5. ultra_short:       "{entity_singular}"         (1-word fallback)
  6. action_specific:   varies by method/entity (POST: "novi {entity}",
                                                 DELETE: "{entity} ne treba")

Deterministic — no LLM. Idempotent — re-run produces same output.

Usage:
  python scripts/regenerate_anchors_v2.py [--staging] [--limit N]
                                          [--method GET,POST,...]

  --staging:  write to config/tool_data.json.staging instead of in-place
  --limit:    process only first N non-internal tools (debug)
  --method:   only regenerate tools with given HTTP method(s)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TOOL_DATA = REPO / "config" / "tool_data.json"

INTERNAL_HELPER_RE = re.compile(
    r"_(Distinct[A-Z][A-Za-z0-9]*|Agg|GroupBy|ProjectTo|metadata|"
    r"DeleteByCriteria|multipatch)$"
)


def is_internal(op_id: str) -> bool:
    return bool(INTERNAL_HELPER_RE.search(op_id))


# Path-tokens that mean "an id placeholder" — not entities
_ID_TOKENS = {"id", "Id", "ID", "documentId", "personId", "vehicleId",
              "companyId", "tenantId", "caseId", "tripId", "poolId",
              "expenseId", "equipmentId"}


def parse_operation(op_id: str, path: str) -> dict:
    """Extract (method, primary_entity, modifier_entity, has_id).

    Examples:
      "delete_VehicleCalendar_id"
        → {method: DELETE, primary: VehicleCalendar, modifier: None, has_id: True}
      "delete_VehicleCalendar_id_documents_documentId"
        → {method: DELETE, primary: documents, modifier: VehicleCalendar, has_id: True}
      "get_Persons"
        → {method: GET, primary: Persons, modifier: None, has_id: False}
      "post_Persons_id_documents"
        → {method: POST, primary: documents, modifier: Persons, has_id: True}
      "delete_LatestVehicleCalendar"
        → {method: DELETE, primary: LatestVehicleCalendar, ...}
    """
    # Method = first underscore-token (always lowercase verb)
    parts = op_id.split("_", 1)
    method = parts[0].upper() if parts else "GET"

    # Path components — skip leading slash, skip {placeholder}
    path_parts = [p for p in (path or "").split("/")
                  if p and not (p.startswith("{") and p.endswith("}"))]

    # Filter to entity tokens (start uppercase, not in ID_TOKENS)
    entity_tokens = [
        p for p in path_parts
        if p and p[0].isupper() and p not in _ID_TOKENS
    ]

    # Also include lowercase tokens that aren't id markers — "documents", "thumb"
    secondary = [
        p for p in path_parts
        if p and not p[0].isupper() and p not in _ID_TOKENS
        and not (p.startswith("{") or p.endswith("}"))
    ]

    # Combine: entities + secondary, in path order
    all_entities = entity_tokens + secondary

    primary = all_entities[-1] if all_entities else ""
    modifier = all_entities[0] if len(all_entities) > 1 else ""

    has_id = "{id}" in (path or "") or any(
        f"{{{t}}}" in (path or "") for t in _ID_TOKENS
    )

    return {
        "method": method,
        "primary": primary,
        "modifier": modifier if modifier != primary else "",
        "has_id": has_id,
    }


# Capitalize first-letter helper for ENTITY_SYNONYMS lookup
def canonicalize_entity_key(token: str) -> str:
    """Convert path token to ENTITY_SYNONYMS key.
    "documents" → "Documents", "thumb" → "Thumb", "Vehicles" → "Vehicles"."""
    if not token:
        return ""
    return token[0].upper() + token[1:]


# Crude infinitive map for indirect template ("treba mi {verb_inf}").
# Croatian infinitive endings are nuanced; cover common imperatives.
_VERB_INFINITIVE_MAP = {
    "briši":          "obrisati",
    "obriši":         "obrisati",
    "ukloni":         "ukloniti",
    "otkaži":         "otkazati",
    "izbriši":        "izbrisati",
    "makni":          "maknuti",
    "izbaci":         "izbaciti",
    "dodaj":          "dodati",
    "stavi":          "staviti",
    "kreiraj":        "kreirati",
    "novi":           "dodati",
    "upiši":          "upisati",
    "registriraj":    "registrirati",
    "evidentiraj":    "evidentirati",
    "uploadaj":       "uploadati",
    "prikači":        "prikačiti",
    "ubaci":          "ubaciti",
    "promijeni":      "promijeniti",
    "ažuriraj":       "ažurirati",
    "izmijeni":       "izmijeniti",
    "korigiraj":      "korigirati",
    "ispravi":        "ispraviti",
    "prepravi":       "prepraviti",
    "pokaži":         "pokazati",
    "prikaži":        "prikazati",
    "dohvati":        "dohvatiti",
    "izlistaj":       "izlistati",
}


def verb_to_infinitive(verb: str) -> str:
    """Return Croatian infinitive form, falling back to verb itself
    if not mapped."""
    # Strip multi-word verbs to first word ("daj mi" → "daj")
    head = verb.split()[0]
    return _VERB_INFINITIVE_MAP.get(head, verb)


def generate_anchors_for_tool(
    op_id: str,
    tool_data: dict,
) -> list:
    """Generate 6 anchors for one tool using vocabulary templates.

    Returns list of strings — deterministic given (op_id, vocab).
    """
    sys.path.insert(0, str(REPO))
    from services.router.anchor_vocab import (
        get_entity_synonyms, get_verb_synonyms,
    )

    parsed = parse_operation(op_id, tool_data.get("path", ""))
    method = parsed["method"]
    primary_key = canonicalize_entity_key(parsed["primary"])
    modifier_key = canonicalize_entity_key(parsed["modifier"])

    verbs = get_verb_synonyms(method)
    if not verbs:
        return []

    entity_syns = get_entity_synonyms(primary_key)
    modifier_syns = (
        get_entity_synonyms(modifier_key) if modifier_key else []
    )

    primary_entity = entity_syns[0]
    primary_alt = entity_syns[1] if len(entity_syns) > 1 else primary_entity
    primary_short = entity_syns[0]  # for ultra_short, may be multi-word

    anchors: list = []
    seen: set = set()

    def _add(text: str) -> None:
        text = " ".join(text.split())  # collapse whitespace
        if text and text not in seen:
            seen.add(text)
            anchors.append(text)

    has_id = parsed["has_id"]
    # "Sve / listu" collection hint je IMPLICITAN only za GET (jer GET-no-id
    # je obično "list all"). Za POST/PUT/DELETE bez ID-a tool je obično
    # bulk operacija — koristimo "više" mjesto "sve" da ne zvuči kao 100k.
    is_get = method == "GET"

    # Skip "novi" prefix if primary_entity already starts with action-like
    # word (e.g. "novi km" — avoids "novi novi km" duplication).
    def _has_action_prefix(text: str) -> bool:
        first = text.split()[0].lower() if text else ""
        return first in {
            "novi", "nova", "novo", "novu", "dodaj", "stavi", "kreiraj",
            "briši", "obriši", "ukloni", "promijeni", "ažuriraj",
            "pošalji", "pokaži", "daj", "treba", "trebam",
        }

    # ---- Template 1: imperative ----
    if has_id:
        _add(f"{verbs[0]} {primary_entity} po id")
    else:
        # GET collection: "pokaži sve X"; mutation collection: "X svih"
        if is_get:
            _add(f"{verbs[0]} sve {primary_entity}")
        else:
            _add(f"{verbs[0]} {primary_entity}")

    # ---- Template 2: alternative entity synonym ----
    _add(f"{verbs[1 % len(verbs)]} {primary_alt}")

    # ---- Template 3: context (with modifier or extra hint) ----
    if modifier_syns:
        mod = modifier_syns[0]
        _add(f"{verbs[2 % len(verbs)]} {primary_entity} {mod}")
    else:
        # Variant verb + same entity for additional dimension
        verb3 = verbs[2 % len(verbs)]
        if verb3 != verbs[0] and verb3 != verbs[1 % len(verbs)]:
            _add(f"{verb3} {primary_entity}")

    # ---- Template 4: indirect (treba mi + infinitive) ----
    inf = verb_to_infinitive(verbs[3 % len(verbs)])
    _add(f"treba mi {inf} {primary_entity}")

    # ---- Template 5: ultra_short (entity keyword for 1-word query fallback) ----
    _add(primary_short)

    # ---- Template 6: action_specific by method + cardinality ----
    if method == "POST":
        # Avoid "novi novi km" — only add prefix if primary doesn't already have it
        if not _has_action_prefix(primary_entity):
            _add(f"novi {primary_entity}")
        else:
            _add(f"trebam {primary_entity}")
    elif method == "DELETE":
        if has_id:
            _add(f"ovaj {primary_entity} ne treba")
        else:
            _add(f"{primary_entity} obriši")
    elif method in ("PUT", "PATCH"):
        _add(f"krivo je {primary_entity}")
    else:  # GET
        if has_id:
            _add(f"detalji {primary_entity}")
        else:
            _add(f"popis {primary_alt}")

    # Dedup again and ensure ≤6 anchors
    return anchors[:6]


def regenerate_all(
    *,
    staging: bool,
    limit: int | None,
    methods: set | None,
) -> dict:
    """Returns stats dict. Mutates registry (in-place or staging file)."""
    sys.stdout.reconfigure(encoding="utf-8")

    if not TOOL_DATA.exists():
        print(f"ERROR: {TOOL_DATA} not found", file=sys.stderr)
        sys.exit(1)

    data = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    tools = data.get("tools", {})

    touched = 0
    skipped_internal = 0
    skipped_method = 0
    total_anchors_before = 0
    total_anchors_after = 0

    for tid, tool in tools.items():
        if not isinstance(tool, dict):
            continue
        if is_internal(tid):
            skipped_internal += 1
            continue
        method = (tool.get("method") or "").upper()
        if methods and method not in methods:
            skipped_method += 1
            continue

        total_anchors_before += len(tool.get("anchors") or [])

        new_anchors = generate_anchors_for_tool(tid, tool)
        if not new_anchors:
            continue

        tool["anchors"] = new_anchors
        total_anchors_after += len(new_anchors)
        touched += 1

        if limit and touched >= limit:
            break

    # Write
    target = TOOL_DATA.with_suffix(".json.staging") if staging else TOOL_DATA
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)

    return {
        "touched": touched,
        "skipped_internal": skipped_internal,
        "skipped_method": skipped_method,
        "anchors_before": total_anchors_before,
        "anchors_after": total_anchors_after,
        "output_path": str(target),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--staging", action="store_true",
        help="Write to config/tool_data.json.staging instead of in-place",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Process only first N non-internal tools (debug)",
    )
    ap.add_argument(
        "--method", default="",
        help="Comma-separated methods to include (GET,POST,...). Default: all",
    )
    args = ap.parse_args()

    methods = None
    if args.method:
        methods = {m.strip().upper() for m in args.method.split(",") if m.strip()}

    stats = regenerate_all(
        staging=args.staging,
        limit=args.limit,
        methods=methods,
    )
    print("\n=== Anchor regeneration complete ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
