"""Meta-intents — handle conversational queries about the bot itself.

Brain-dump #71-77, 147:
- "tko si ti" / "ti si AI" → identity disclosure
- "hoću pravog čovjeka" → human-handoff (return manager contact)
- "jučer si mi rekao krivo" → bug-report acknowledgement
- "kako si?" / out-of-scope personal → graceful decline
- "vrati to što si napravio" → undo-not-supported message
- Profanity/harassment → set boundary

These are NOT API-routing intents. Bot responds inline without LLM
or tool call. Detected via keyword patterns; lightweight.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MetaIntent:
    detected: bool
    kind: str = ""
    response: Optional[str] = None


_PATTERNS = {
    "self_identity": [
        re.compile(r"\b(?:tko\s+si\s+ti|sto\s+si\s+ti|što\s+si\s+ti|jesi\s+li\s+(?:ti\s+)?ai|ti\s+si\s+ai|kojim?\s+si\s+modelom?|si\s+li\s+robot)\b", re.IGNORECASE),
        re.compile(r"\bare\s+you\s+(?:an?\s+)?(?:ai|bot|robot|chatgpt|claude|llm)\b", re.IGNORECASE),
        re.compile(r"\bwhat\s+(?:are|is)\s+(?:you|this)\b", re.IGNORECASE),
    ],
    "human_handoff": [
        re.compile(r"\b(?:hoću|zelim|želim|trebam)\s+(?:pravog?\s+)?(?:čovjeka|coveka|ljudsku\s+osobu|ljudsku\s+podršku|operatera)\b", re.IGNORECASE),
        re.compile(r"\bspoji\s+me\s+s\s+(?:čovjekom|covjekom|operaterom|managerom)\b", re.IGNORECASE),
        re.compile(r"\bhuman\s+(?:agent|support|operator)\b", re.IGNORECASE),
        re.compile(r"\btalk\s+to\s+(?:a\s+)?(?:human|person|agent)\b", re.IGNORECASE),
    ],
    "bug_report": [
        re.compile(r"\bjučer\s+si\s+mi\s+rekao\s+krivo\b", re.IGNORECASE),
        re.compile(r"\b(?:rekao|rekla|rekli)\s+(?:si|ste)?\s+(?:mi\s+)?krivo\b", re.IGNORECASE),
        re.compile(r"\b(?:to\s+nije|nisi\s+u\s+pravu|to\s+je\s+greška|krivo\s+ti\s+je)\b", re.IGNORECASE),
        re.compile(r"\bbug\b", re.IGNORECASE),
    ],
    "undo_request": [
        re.compile(r"\b(?:vrati|undo|poništi|ponisti)\s+(?:to|ono|prošlo|prosto)\b", re.IGNORECASE),
        re.compile(r"\b(?:promijeni|izmijeni)\s+nazad\b", re.IGNORECASE),
        re.compile(r"\bcancel\s+(?:my\s+)?last\s+(?:action|operation)\b", re.IGNORECASE),
    ],
    "personal_chat": [
        re.compile(r"\b(?:kako\s+si|kako\s+ide|sta\s+ima|šta\s+ima)\s*\??$", re.IGNORECASE),
        re.compile(r"\bvolim\s+te\b", re.IGNORECASE),
        re.compile(r"\bhow\s+are\s+you\b", re.IGNORECASE),
    ],
    "harassment": [
        re.compile(r"\b(?:jebi\s+se|jbg|sranje|kreten|glup\s+si|glupi\s+bot)\b", re.IGNORECASE),
        re.compile(r"\b(?:fuck|shit|stupid)\b", re.IGNORECASE),
    ],
    "weather_time": [
        re.compile(r"\b(?:kakvo\s+je\s+vrijeme|koliko\s+je\s+sati|koji\s+je\s+datum)\b", re.IGNORECASE),
        re.compile(r"\b(?:what(?:'s|\s+is)\s+the\s+(?:time|weather|date))\b", re.IGNORECASE),
    ],
}


_RESPONSES = {
    "self_identity": (
        "Da, ja sam AI asistent (model je gpt-4o-mini, hosted na Azure). "
        "Pomažem oko fleet-management upita — kilometraža, rezervacije, "
        "prijave kvarova. Ne čuvam osobne razgovore izvan ove sesije."
    ),
    "human_handoff": (
        "Razumijem. Za živu osobu kontaktiraj svog managera ili podršku "
        "kroz redovne kanale — ja kao AI ne mogu te direktno povezati. "
        "Ako trebaš nešto vezano uz fleet, samo reci."
    ),
    "bug_report": (
        "Žao mi je ako je odgovor bio kriv. Možeš li mi reći koje pitanje "
        "i koji odgovor? Filip i tim će provjeriti. Za sad: postavi "
        "pitanje ponovno, drugačije formulirano."
    ),
    "undo_request": (
        "Trenutno ne podržavam undo. Ako trebaš poništiti nešto specifično "
        "(rezervaciju, km zapis), reci 'otkaži [nešto]' ili 'obriši "
        "[nešto]' — bot će napraviti novu mutaciju s confirm dialogom."
    ),
    "personal_chat": (
        "Dobro sam, hvala — ali samo sam bot. Ako trebaš nešto oko fleet-a, "
        "reci."
    ),
    "harassment": (
        "Razumijem da si frustriran/a. Ako mi pomogneš preformulirati "
        "pitanje, pokušat ću dati bolji odgovor. Ako trebaš živu osobu, "
        "kontaktiraj managera."
    ),
    "weather_time": (
        "Nemam pristup vremenu/datumu/meteorologiji — to nije u mom scope-u. "
        "Ja sam fleet-management asistent (km, rezervacije, kvarovi)."
    ),
}


def detect(query: str) -> MetaIntent:
    """Detect meta-intent. Returns MetaIntent."""
    if not query or not query.strip():
        return MetaIntent(detected=False)

    text = query.strip()
    for kind, patterns in _PATTERNS.items():
        for pat in patterns:
            if pat.search(text):
                return MetaIntent(
                    detected=True,
                    kind=kind,
                    response=_RESPONSES.get(kind, ""),
                )
    return MetaIntent(detected=False)
