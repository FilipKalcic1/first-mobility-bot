"""L5.5 — Clarify UI: Top-3 candidate cards as user-facing disambiguation.

When confidence_gate returns DECISION_FALLBACK or borderline EXECUTE_WITH_CONFIRM,
this module renders the top-3 candidate tools as an interactive list so the
user can disambiguate with a single tap (or text reply).

Two output modes:
  - TEXT: numbered list ("1️⃣ X / 2️⃣ Y / 3️⃣ Z") — safe fallback when
    interactive messaging unavailable.
  - INTERACTIVE: Infobip-compatible structured payload (list message rows)
    for transports that support it.

The user reply to a clarify message is interpreted as choosing one of the
shown candidates (1/2/3 or button id). The chosen tool then runs through
the standard confirm + execute path — clarify NEVER bypasses mutation gate.

Empirical motivation: A+B+C (clarify-rescuable) = 43.5% on entity_hybrid
config vs A (strict 1-shot) = 22.8% — almost 2x the user-perceived accuracy
gain comes from offering the right entity in 3 choices when the bot is
unsure, instead of executing a confident-wrong pick.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ClarifyCard:
    """Single disambiguation option shown to the user."""
    index: int                 # 1-based, for "1️⃣" etc.
    tool_id: str
    short_label: str           # Croatian, < 60 chars (button-friendly)
    description: str           # Croatian, < 150 chars (row description)


@dataclass(frozen=True)
class ClarifyOptions:
    """Rendered top-3 + 'Ne znam' option."""
    cards: list[ClarifyCard]
    none_of_above_label: str = "Nešto drugo"


def build_clarify_options(
    candidates: list,
    max_cards: int = 3,
    persona: Optional[str] = None,  # noqa: ARG001 — reserved for future per-persona phrasing
) -> ClarifyOptions:
    """Build top-3 cards from RecognitionResult candidates.

    Each card gets:
      - short_label: a Croatian-friendly button label (entity + verb)
      - description: tool's purpose string (truncated)
    """
    cards: list[ClarifyCard] = []
    for i, c in enumerate(candidates[:max_cards], start=1):
        short = _build_short_label(c.tool_id, c.purpose)
        desc = (c.purpose or c.tool_id)[:140]
        cards.append(ClarifyCard(
            index=i,
            tool_id=c.tool_id,
            short_label=short,
            description=desc,
        ))
    return ClarifyOptions(cards=cards)


def _build_short_label(tool_id: str, purpose: Optional[str]) -> str:
    """Heuristic Croatian button label from tool_id + purpose.

    Best-effort: tries to convert technical tool_id into user-friendly text.
    """
    import re
    # extract verb and entity
    m = re.match(r"^(get|post|put|patch|delete)_([A-Z][A-Za-z0-9]+)", tool_id)
    if not m:
        return (purpose or tool_id)[:50]
    verb_map = {
        "get": "Pokaži",
        "post": "Dodaj",
        "put": "Promijeni",
        "patch": "Ažuriraj",
        "delete": "Obriši",
    }
    verb = verb_map.get(m.group(1), "?")
    entity_raw = m.group(2)
    entity_label_map = {
        "VehicleCalendar": "rezervaciju",
        "LatestVehicleCalendar": "aktivnu rezervaciju",
        "MileageReports": "km zapis",
        "LatestMileageReports": "zadnji km",
        "MonthlyMileages": "mjesečni km",
        "AddMileage": "novi km",
        "Cases": "prijavu/štetu",
        "AddCase": "novu prijavu",
        "Vehicles": "vozilo",
        "Persons": "osobu",
        "OrgUnits": "organizacijsku jedinicu",
        "Companies": "tvrtku",
        "CostCenters": "centar troškova",
        "Expenses": "trošak",
        "Equipment": "opremu",
        "Trips": "putovanje",
        "VehicleContracts": "ugovor vozila",
        "MasterData": "moje podatke",
        "VehicleAssignments": "dodjelu vozila",
    }
    entity = entity_label_map.get(entity_raw, entity_raw.lower())
    return f"{verb} {entity}"[:50]


def render_text(options: ClarifyOptions, header: str = "") -> str:
    """Plain-text rendering — universal fallback."""
    emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    if not options.cards:
        return (
            "Nisam siguran što tražiš. Pokušaj napisati drugačije ili "
            "kontaktiraj managera."
        )
    head = header or "Razumio sam da ti treba jedno od ovog:"
    lines = [head, ""]
    for c in options.cards:
        e = emoji[c.index - 1] if c.index <= len(emoji) else f"{c.index}."
        lines.append(f"{e} {c.short_label}")
    lines.append(f"❌ {options.none_of_above_label}")
    lines.append("")
    lines.append("Odgovori brojem (1, 2, 3) ili \"ne\".")
    return "\n".join(lines)


def render_infobip_list_message(
    options: ClarifyOptions,
    header: str = "",
    body: str = "",
) -> dict:
    """Infobip WhatsApp interactive list message payload.

    Structure per Infobip API docs:
      content.action.sections[].rows[].id/title/description

    The caller embeds this dict into the Infobip outbound message
    request. If interactive messages are NOT supported on the
    account, fall back to render_text().
    """
    rows = []
    for c in options.cards:
        rows.append({
            "id": f"clarify::{c.tool_id}",
            "title": c.short_label[:24],   # Infobip max title 24 chars
            "description": c.description[:72],
        })
    rows.append({
        "id": "clarify::none",
        "title": options.none_of_above_label[:24],
        "description": "Nije ništa od ponuđenog",
    })
    return {
        "type": "INTERACTIVE_LIST",
        "content": {
            "header": {"type": "TEXT", "text": (header or "Pojašnjenje")[:60]},
            "body": {"text": (body or "Što točno tražiš?")[:1024]},
            "action": {
                "title": "Odaberi",
                "sections": [{"rows": rows}],
            },
        },
    }


def parse_clarify_reply(
    reply_text: str, options: ClarifyOptions,
) -> Optional[str]:
    """Map user reply to chosen tool_id. Returns None if reply is
    'none of above' or unrecognized.

    Accepts:
      - Numeric: "1", "2", "3"
      - Emoji: "1️⃣", "2️⃣", ...
      - Infobip interactive id: "clarify::tool_id"
      - Negative: "ne", "nista", "nesto drugo", "drugo" → None
    """
    t = (reply_text or "").strip().lower()
    if not t:
        return None

    # Negative responses → None (caller should treat as 'unknown')
    NEGATIVE = {"ne", "nista", "ništa", "nesto drugo", "nešto drugo",
                "drugo", "none", "no", "ništa od navedenog", "kontaktiraj"}
    if any(t == n or t.startswith(n + " ") for n in NEGATIVE):
        return None

    # Infobip id form
    if t.startswith("clarify::"):
        chosen_id = reply_text.strip()[len("clarify::"):]
        if chosen_id == "none":
            return None
        for c in options.cards:
            if c.tool_id == chosen_id:
                return c.tool_id
        return None

    # Numeric / emoji form
    NUM_TO_INDEX = {
        "1": 1, "1️⃣": 1, "jedan": 1, "prvo": 1,
        "2": 2, "2️⃣": 2, "dva": 2, "drugo": 2,
        "3": 3, "3️⃣": 3, "tri": 3, "treće": 3, "trece": 3,
        "4": 4, "4️⃣": 4,
        "5": 5, "5️⃣": 5,
    }
    # Strip emoji & punctuation
    stripped = t.replace(".", "").replace(")", "").replace("(", "").strip()
    idx = NUM_TO_INDEX.get(stripped) or NUM_TO_INDEX.get(t)
    if idx and 1 <= idx <= len(options.cards):
        return options.cards[idx - 1].tool_id
    return None
