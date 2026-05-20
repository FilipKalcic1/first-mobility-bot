"""L1 — Special Intents.

Hardcoded by design — these are policy / UX boundaries that must work
regardless of registry state, LLM availability, or anything else:

  - WELCOME      first contact OR plain greeting -> personalized hello
  - GDPR_DELETE  GDPR right-to-erasure request
  - GDPR_EXPORT  GDPR data portability request
  - HANDOVER     "want to talk to human"
  - HELP         "what can you do"

Match algorithm: longest-trigger-wins on diacritic-normalized query.
First-contact welcome is detected via IdentitySnapshot.is_first_contact
(not by phrase) so a returning user typing "bok" gets full welcome only
on first message of session.

Returns: SpecialIntentMatch | None — single source of decision; the
router takes it as terminal (no further routing).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Optional


# --------------------------------------------------------------------------
# Trigger sets (HARDCODED BY DESIGN — see CLAUDE.md §1.4)
# --------------------------------------------------------------------------

# Greetings — only act as WELCOME if first contact, otherwise ignored.
_GREETING_TRIGGERS = (
    "bok", "pozdrav", "hej", "hi", "hello", "dobar dan", "dobro jutro",
    "dobra vecer", "dobra vecer", "ciao", "zdravo",
)

_GDPR_DELETE_TRIGGERS = (
    "obrisi me", "izbrisi me", "obrisi profil", "izbrisi profil",
    "obrisi sve moje podatke", "obrisi moje podatke",
    "ne zelim biti u sustavu", "gdpr brisanje", "uklonite moj profil",
    "delete me", "delete my account", "delete my data",
    "right to be forgotten", "gdpr delete", "remove my data",
    "erase my data",
)

_GDPR_EXPORT_TRIGGERS = (
    "posalji mi sve moje podatke", "izvezi moje podatke", "izvoz podataka",
    "posalji mi kopiju mojih podataka", "gdpr izvoz",
    "export my data", "data export", "gdpr export", "send me my data",
)

_HANDOVER_TRIGGERS = (
    "operater", "covjek", "pravi covjek", "zelim s coecom",
    "razgovor s osobom", "trebam pomoc covjeka", "predaj coecu",
    "human", "agent", "real person", "talk to human", "speak to agent",
)

_HELP_TRIGGERS = (
    "pomoc", "help", "sto mozes", "sto sve mozes",
    "kako koristiti", "kako te koristim", "kako radis",
    "what can you do", "how do i use you",
)


_INTENT_TABLE: list[tuple[str, tuple[str, ...]]] = [
    # name → triggers — order matters ONLY for ties; longest-match wins
    # within and across intents.
    ("GDPR_DELETE", _GDPR_DELETE_TRIGGERS),
    ("GDPR_EXPORT", _GDPR_EXPORT_TRIGGERS),
    ("HANDOVER",    _HANDOVER_TRIGGERS),
    ("HELP",        _HELP_TRIGGERS),
    ("GREETING",    _GREETING_TRIGGERS),
]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecialIntentMatch:
    intent: str           # WELCOME | GDPR_DELETE | GDPR_EXPORT | HANDOVER | HELP
    response: str         # Croatian, ready to send to user
    side_effects: tuple   # tuple of {action, ...} for the caller to execute
                          # e.g. ({"action": "audit_log_gdpr_delete", "person_id": ...})


def detect_special_intent(
    query: str,
    *,
    is_first_contact: bool = False,
    first_name: Optional[str] = None,
    vehicle_name: Optional[str] = None,
    licence_plate: Optional[str] = None,
    last_mileage: Optional[int] = None,
    company_name: Optional[str] = None,
) -> Optional[SpecialIntentMatch]:
    """Match `query` against special intents.

    Returns the match (terminal — caller does NOT route further) or None.

    Welcome-on-first-contact: if `is_first_contact` is True, ANY query
    triggers a welcome (the user's first message of the session). The
    welcome includes name, company, vehicle, mileage from context and
    explicitly invites the user to send their actual query in the next
    message.

    Otherwise: exact greeting phrase ("bok") gets a short ack but does
    NOT re-trigger the full welcome flow.
    """
    # First-contact welcome supersedes everything except GDPR (legal
    # priority).
    q_norm = _normalize(query)

    # Run GDPR first — legal precedence.
    for name, triggers in (
        ("GDPR_DELETE", _GDPR_DELETE_TRIGGERS),
        ("GDPR_EXPORT", _GDPR_EXPORT_TRIGGERS),
    ):
        if _matches_any(q_norm, triggers):
            return _build_match(name, first_name=first_name)

    # First contact — welcome takes precedence over greeting/help.
    if is_first_contact:
        return _build_match(
            "WELCOME",
            first_name=first_name,
            vehicle_name=vehicle_name,
            licence_plate=licence_plate,
            last_mileage=last_mileage,
            company_name=company_name,
        )

    # Otherwise, longest-trigger across remaining intents.
    best_intent: Optional[str] = None
    best_len = 0
    for name, triggers in _INTENT_TABLE:
        if name in ("GDPR_DELETE", "GDPR_EXPORT"):
            continue  # already checked
        for t in triggers:
            t_norm = _normalize(t)
            if t_norm and t_norm in q_norm and len(t_norm) > best_len:
                # Whole-word boundary check
                if _has_word_boundary(q_norm, t_norm):
                    best_len = len(t_norm)
                    best_intent = name

    if not best_intent:
        return None

    return _build_match(best_intent, first_name=first_name)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """Lowercase + strip Croatian diacritics. Đ handled manually."""
    if not s:
        return ""
    s = s.translate(str.maketrans({"đ": "d", "Đ": "D"}))
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _matches_any(q_norm: str, triggers: tuple[str, ...]) -> bool:
    for t in triggers:
        t_norm = _normalize(t)
        if t_norm and t_norm in q_norm and _has_word_boundary(q_norm, t_norm):
            return True
    return False


def _has_word_boundary(haystack: str, needle: str) -> bool:
    """needle appears in haystack on word boundaries (alphanumeric)."""
    idx = 0
    while True:
        pos = haystack.find(needle, idx)
        if pos < 0:
            return False
        before_ok = pos == 0 or not haystack[pos - 1].isalnum()
        end = pos + len(needle)
        after_ok = end == len(haystack) or not haystack[end].isalnum()
        if before_ok and after_ok:
            return True
        idx = pos + 1


def _build_match(
    name: str,
    *,
    first_name: Optional[str] = None,
    vehicle_name: Optional[str] = None,
    licence_plate: Optional[str] = None,
    last_mileage: Optional[int] = None,
    company_name: Optional[str] = None,
) -> SpecialIntentMatch:
    """Generate SpecialIntentMatch with Croatian response + side-effects."""
    name_part = f" {first_name}" if first_name else ""

    if name == "WELCOME":
        # EU AI Act compliance (effective Aug 2026): bot MUST disclose
        # that user is interacting with an AI system at first contact.
        # Article 52 — transparency for AI systems intended to interact
        # with natural persons. Welcome moment is the natural place.
        ai_disclosure = (
            "ℹ️ _Razgovaraš s AI asistentom (umjetnom inteligencijom). "
            "Ne čuvam osobne razgovore izvan trenutne sesije. "
            "Za živu osobu napiši 'pomoć' ili kontaktiraj managera._"
        )

        lines: list[str] = [f"Bok{name_part}! 👋"]
        lines.append("Pronašao sam te u sustavu.")

        # Context lines — only include what we know about the user.
        if company_name:
            lines.append(f"Pripadaš tvrtki **{company_name}**.")

        if vehicle_name:
            vehicle_line = f"Tvoje dodijeljeno vozilo je **{vehicle_name}**"
            if licence_plate:
                vehicle_line += f" (registracija {licence_plate})"
            if last_mileage:
                vehicle_line += f", trenutno stanje **{last_mileage:,} km**".replace(",", ".")
            vehicle_line += "."
            lines.append(vehicle_line)
        else:
            lines.append(
                "Vidim da trenutno nemaš dodijeljeno vozilo — "
                "pošalji mi 'rezerviraj' ako želiš rezervirati."
            )

        lines.append("")
        lines.append(
            "Pošalji mi svoj upit u **sljedećoj poruci**. "
            "Mogu ti pomoći s rezervacijama, prijavama kvarova, "
            "unosom kilometraže, troškovima i sl."
        )
        lines.append("")
        lines.append(ai_disclosure)
        response = "\n".join(lines)
        return SpecialIntentMatch(intent="WELCOME", response=response, side_effects=())

    if name == "GREETING":
        # Returning user just saying hi — short ack only.
        response = f"Bok{name_part}! Kako ti mogu pomoći?"
        return SpecialIntentMatch(intent="GREETING", response=response, side_effects=())

    if name == "GDPR_DELETE":
        response = (
            "Razumijem — proslijedio sam tvoj zahtjev za brisanje "
            "podataka administratoru. Po GDPR-u zahtjev mora biti "
            "obrađen u 48 sati. Za dodatna pitanja kontaktiraj svog "
            "administratora flote."
        )
        return SpecialIntentMatch(
            intent="GDPR_DELETE",
            response=response,
            side_effects=(
                {"action": "audit_log_gdpr_delete"},
            ),
        )

    if name == "GDPR_EXPORT":
        response = (
            "Tvoj zahtjev za izvoz podataka proslijeđen je administratoru. "
            "Dobit ćeš kopiju svojih podataka u roku od 48 sati."
        )
        return SpecialIntentMatch(
            intent="GDPR_EXPORT",
            response=response,
            side_effects=(
                {"action": "audit_log_gdpr_export"},
            ),
        )

    if name == "HANDOVER":
        response = (
            "U redu, prebacujem te na operatera. Javi se za nekoliko "
            "trenutaka."
        )
        return SpecialIntentMatch(
            intent="HANDOVER",
            response=response,
            side_effects=(
                {"action": "queue_human_handover"},
            ),
        )

    if name == "HELP":
        response = (
            "Mogu ti pomoći s ovim:\n"
            "• Podaci o tvom vozilu (registracija, kilometraža, lizing)\n"
            "• Popis tvojih rezervacija ili putovanja\n"
            "• Rezervacija novog vozila\n"
            "• Unos kilometraže\n"
            "• Prijava kvara ili štete\n\n"
            "Probaj nešto kao: \"koji je moj auto\", \"upiši km\", "
            "\"rezerviraj auto sutra 9-15\"."
        )
        return SpecialIntentMatch(intent="HELP", response=response, side_effects=())

    raise ValueError(f"unknown intent name: {name}")
