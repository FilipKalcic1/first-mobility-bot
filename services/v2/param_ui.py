"""Param-asking UI: Croatian questions + answer parsing.

Used by engine's `_resolve_pending_params` to ask the user for required
(or opted-in optional) parameters one at a time. Pure / sync — no I/O.

Question text:
  Engine resolves the Croatian label upstream (via `ParamLabeler` LLM call
  + Redis cache + preloaded JSON) and passes it as `label_override`. If not
  provided, this module falls back to `humanize_param_name()` — which is
  best-effort English→Croatian mush ("vehicle id" instead of "ID vozila").
  The engine should always provide an override in production.

Filip 2026-05-17: removed curated `PARAM_LABELS` dict — it limited the bot
to ~33 manually-tracked params (53.6% coverage). Labels now come from
`ParamLabeler` which works for any param name in any of the 950 tools.

Answer parsing is type-driven from registry `param_type`. None return →
caller re-asks with a typed example.
"""
from __future__ import annotations

import re
from datetime import datetime, time as dt_time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# Resolve relative dates ("danas"/"sutra") against Croatia, not the host's TZ
# (prod host may be UTC → off-by-one near midnight). Fall back to naive local
# if the tz db is ever unavailable. tzdata ships in requirements.
try:
    _ZAGREB = ZoneInfo("Europe/Zagreb")
except Exception:  # noqa: BLE001
    _ZAGREB = None


# Set of strings interpreted as "cancel the current param collection".
CANCEL_TOKENS = frozenset({
    "odustani", "otkazi", "otkaži", "cancel", "stop", "prestani",
})


# Set of strings interpreted as "skip optional params, go execute".
NEGATIVE_TOKENS = frozenset({
    "ne", "nista", "ništa", "preskoči", "preskoci", "n", "no", "skip",
})


def humanize_param_name(name: str) -> str:
    """Best-effort fallback: 'VehicleId' → 'vehicle id', 'start_date' →
    'start date'. Used when no `label_override` is provided (LLM labeler
    failed or wasn't wired). Lowercased so the template 'Trebam {label}.'
    reads naturally in Croatian."""
    if not name:
        return ""
    # CamelCase / PascalCase → spaces
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    # snake_case → spaces
    spaced = spaced.replace("_", " ")
    # collapse multiple spaces
    spaced = re.sub(r"\s+", " ", spaced).strip()
    return spaced.lower()


def render_param_question(
    param_name: str,
    param_def: Optional[dict] = None,
    label_override: Optional[str] = None,
) -> str:
    """Build a Croatian one-line question for a single missing param.

    param_def is the registry entry (dict with optional 'description',
    'param_type', 'format'). None / missing fields are tolerated.
    label_override: pre-resolved Croatian label from engine's ParamLabeler.
    """
    label = (label_override or "").strip() or humanize_param_name(param_name)
    extra = ""
    if param_def:
        desc = (param_def.get("description") or "").strip()
        if desc and len(desc) < 80 and desc.lower() not in label.lower():
            extra = f" ({desc})"
    return (
        f"Trebam {label}{extra}. "
        f"Napiši vrijednost ili 'odustani' za otkaz."
    )


def render_optional_offer(
    optional_names: list,
    max_shown: int = 5,
    label_overrides: Optional[dict] = None,
) -> str:
    """Build the Croatian prompt asking which optional fields the user wants.

    Filip 2026-05-17: free-text reply gets LLM-extracted by
    OptionalParamExtractor, so we instruct the user to type naturally
    rather than answer yes/no. Long lists get capped with "(i još N polja)"
    so the UI doesn't dump 54 names on the user.

    label_overrides: optional {param_name: croatian_label} from ParamLabeler.
    Missing entries fall back to `humanize_param_name`.
    """
    if not optional_names:
        return ""
    overrides = label_overrides or {}
    labels = [
        (overrides.get(n) or humanize_param_name(n))
        for n in optional_names
    ]
    if len(labels) <= max_shown:
        listing = ", ".join(labels)
    else:
        shown = ", ".join(labels[:max_shown])
        rest = len(labels) - max_shown
        listing = f"{shown} (i još {rest} polja)"
    return (
        f"Imam sve potrebno. Opcionalno možeš dodati: {listing}.\n"
        f"Napiši slobodno što hoćeš (npr. 'napomena: kasnim, tip: hitno') "
        f"ili 'ne' za bez."
    )


def render_param_reask(
    param_name: str,
    param_def: Optional[dict] = None,
    label_override: Optional[str] = None,
) -> str:
    """Build the re-ask message after a failed parse. Includes a typed
    example so user knows what shape to give next.
    label_override: pre-resolved Croatian label from ParamLabeler.
    """
    label = (label_override or "").strip() or humanize_param_name(param_name)
    ptype = (param_def or {}).get("param_type") or "string"
    fmt = (param_def or {}).get("format") or ""
    example = ""
    if ptype == "integer":
        example = " (npr. 42500)"
    elif ptype == "number":
        example = " (npr. 12.5)"
    elif ptype == "boolean":
        example = " (da/ne)"
    elif fmt == "date-time":
        example = " (npr. '17.05.2026 09:00')"
    elif fmt == "date":
        example = " (npr. '17.05.2026')"
    return (
        f"Nisam razumio. Trebam {label}{example}. "
        f"Pokušaj opet ili napiši 'odustani'."
    )


def is_cancel(text: str) -> bool:
    """User wants to abort the whole param collection."""
    return (text or "").strip().lower() in CANCEL_TOKENS


def is_negative(text: str) -> bool:
    """User wants to skip optional params and execute now."""
    return (text or "").strip().lower() in NEGATIVE_TOKENS


def parse_param_value(
    raw: str,
    param_def: Optional[dict] = None,
) -> Optional[object]:
    """Coerce user's text to the param's typed value. None on failure.

    Reads `param_type` and `format` from registry param_def.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if param_def is None:
        return raw

    ptype = (param_def.get("param_type") or "string").lower()

    if ptype == "integer":
        # A comma between digits is either a decimal ('12,5') or two separate
        # numbers ('5, 30000') — both mean "don't silently merge digits into one
        # integer". Re-ask. Allow whitespace so '5, 30000' is caught too
        # (Filip 2026-05-26). Dot stays a thousand-separator below.
        if re.search(r"\d\s*,\s*\d", raw):
            return None
        # Strip non-digit/minus chars (handles '42.500 km' → 42500 — common
        # Croatian thousand-separator with dot).
        cleaned = re.sub(r"[^\d-]", "", raw)
        if not cleaned or cleaned == "-":
            return None
        try:
            return int(cleaned)
        except ValueError:
            return None

    if ptype == "number":
        # A lone '.' grouping exactly 3 trailing digits and NO comma ('1.500')
        # is almost certainly an HR thousands separator (1500), not a decimal
        # 1.5 — too ambiguous to guess, so re-ask (Filip 2026-05-26). '3.14'
        # (2 digits) and '1.500,75' (has comma) are unaffected.
        if "," not in raw and re.search(r"\d\.\d{3}(?!\d)", raw):
            return None
        # HR numbers: with BOTH separators, '.' is the thousands grouping and
        # ',' is the decimal ('1.500,75' → 1500.75). With only ',', it's the
        # decimal ('12,5' → 12.5). A lone '.' stays a decimal point
        # ('3.14' → 3.14) — ambiguous vs thousands, but decimal is the safer
        # default for these fields. Multiple dots left over → re-ask.
        if "." in raw and "," in raw:
            cleaned = raw.replace(".", "").replace(",", ".")
        else:
            cleaned = raw.replace(",", ".")
        cleaned = re.sub(r"[^\d.\-]", "", cleaned)
        if not cleaned or cleaned.count(".") > 1:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    if ptype == "boolean":
        # Explicit yes/no sets. Anything else → None (re-ask), NOT silently
        # False — the old `raw in {affirmatives}` returned False for garbage
        # AND for "ne", so a typo silently became False (Filip 2026-05-24).
        low = raw.lower().rstrip(".!?")
        if low in {"da", "yes", "true", "1", "ok", "može", "moze", "točno", "tocno"}:
            return True
        if low in {"ne", "no", "false", "0", "n", "nije", "netočno", "netocno"}:
            return False
        return None  # ambiguous → re-ask

    # String types — date-time / date formats get parsed to ISO 8601 so the
    # API gets the shape it expects. Plain strings pass through.
    # Faza 4 (Filip 2026-05-17): replaces raw passthrough that was causing
    # 41 tools with format=date-time to fail API validation.
    fmt = (param_def.get("format") or "").lower()
    if fmt == "date-time":
        parsed = parse_datetime_hr(raw, want_time=True)
        if parsed is not None:
            return parsed
        # Parser couldn't make sense of it — accept raw and let API decide
        return raw
    if fmt == "date":
        parsed = parse_datetime_hr(raw, want_time=False)
        if parsed is not None:
            return parsed
        return raw

    return raw


# --------------------------------------------------------------------------
# Croatian date/datetime parser (single point in time)
# --------------------------------------------------------------------------
# Note: flow_engine._parse_period parses a RANGE (e.g. "sutra 9-15" → from+to)
# for booking flows. This parser handles a SINGLE point (e.g. "sutra 9:00" or
# "17.05.2026") for param-asking flow where each datetime is its own param.
# Both intentionally support the same Croatian relative-day keywords
# ("danas", "sutra", "prekosutra") so users see consistent behavior.

_DATE_DMY_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\.?")
_TIME_HM_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(?:h|sat[ai]?)?\b")
_ISO_RE = re.compile(
    r"\s*(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*$",
    re.IGNORECASE,
)

# Weekday + part-of-day maps mirror services/v2/flow_engine.py (kept in sync by
# hand — both small, rarely change) so the param-ask path resolves the SAME
# Croatian phrases as the booking-flow range parser (NALAZ 4, Filip 2026-05-25).
_WEEKDAYS_HR = {
    "ponedjeljak": 0, "pon": 0, "utorak": 1, "uto": 1,
    "srijed": 2, "sri": 2, "četvrt": 3, "cetvrt": 3, "čet": 3, "cet": 3,
    "petak": 4, "pet": 4, "subot": 5, "sub": 5, "nedjelj": 6, "ned": 6,
}
# Part of day → start hour (single point in time; flow_engine uses ranges).
_PART_OF_DAY_HR = {
    "ujutro": 9, "prijepodne": 9, "popodne": 13,
    "navečer": 18, "naveer": 18, "uvece": 18,
}


def _resolve_weekday_hr(today_date, lower: str):
    """Date of the upcoming weekday named in `lower`, or None. 'idući'/'sljedeći'
    skips to the week after. Mirrors flow_engine._resolve_weekday."""
    next_week = bool(re.search(r"\b(?:iduć|iduci|sljedeć|sljedeci|drugi)\b", lower))
    wd = today_date.weekday()
    for kw, target in _WEEKDAYS_HR.items():
        if kw in lower:
            delta = (target - wd) % 7
            if delta == 0 and not next_week:
                delta = 7
            if next_week:
                delta = 7 if delta == 0 else delta + 7
            return today_date + timedelta(days=delta)
    return None


def parse_datetime_hr(text: str, want_time: bool = True) -> Optional[str]:
    """Parse a single Croatian date or datetime into ISO 8601.

    Supported forms:
      "17.05.2026"             → "2026-05-17" (or "2026-05-17T00:00:00")
      "17.05.2026 09:00"       → "2026-05-17T09:00:00"
      "17.05.2026 9"           → "2026-05-17T09:00:00"
      "sutra"                  → tomorrow midnight (date only if want_time=False)
      "sutra 9:00"             → tomorrow 09:00
      "sutra u 14:30"          → tomorrow 14:30
      "prekosutra 8"           → +2 days 08:00
      "danas 16"               → today 16:00

    Returns ISO 8601 string (no timezone — assumed Europe/Zagreb).
    Returns None on failure; caller should accept raw input as fallback.

    `want_time`: if False, only date is returned ("2026-05-17"). Time
    portion of input is ignored.
    """
    if not text:
        return None
    lower = text.lower().strip()

    # Already ISO 8601 — normalize + pass through. Lets the coercion-pass keep
    # LLM-emitted ISO unchanged, and accepts ISO in param-ask too.
    iso = _ISO_RE.match(lower)
    if iso:
        y, mo, d, hh, mi, ss = iso.groups()
        try:
            if not want_time:
                return datetime(int(y), int(mo), int(d)).date().isoformat()
            return datetime(
                int(y), int(mo), int(d),
                int(hh or 0), int(mi or 0), int(ss or 0),
            ).isoformat(timespec="seconds")
        except ValueError:
            return None

    # Resolve date component
    today = datetime.now(_ZAGREB).date()
    date = None

    if "prekosutra" in lower:
        date = today + timedelta(days=2)
    elif "sutra" in lower:
        date = today + timedelta(days=1)
    elif "danas" in lower:
        date = today
    else:
        date = _resolve_weekday_hr(today, lower)  # "u petak", "idući utorak"
        if date is None:
            m = _DATE_DMY_RE.search(lower)
            if m:
                d, mo, y = m.groups()
                try:
                    date = datetime(int(y), int(mo), int(d)).date()
                except ValueError:
                    return None

    if date is None:
        # No date hint anywhere — can't construct ISO output
        return None

    if not want_time:
        return date.isoformat()  # "2026-05-17"

    # Resolve time component (optional — defaults to 00:00:00)
    hour, minute = 0, 0
    # Strip date / relative-day fragments from text before time match so
    # numbers in the date (e.g. "17.05.2026 9") don't get re-matched.
    stripped = _DATE_DMY_RE.sub("", lower)
    for kw in ("prekosutra", "sutra", "danas", "u "):
        stripped = stripped.replace(kw, " ")
    tm = _TIME_HM_RE.search(stripped)
    matched_numeric = False
    if tm:
        h, mm = tm.groups()
        try:
            hi = int(h)
            mi = int(mm) if mm else 0
            if 0 <= hi <= 23 and 0 <= mi <= 59:
                hour, minute = hi, mi
                matched_numeric = True
        except ValueError:
            pass

    # Part-of-day ("ujutro"/"popodne"/"navečer"). With no numeric hour it sets
    # the default; with one ("u 2 popodne") it shifts a 1-11 hour into PM.
    part = next((v for k, v in _PART_OF_DAY_HR.items() if k in lower), None)
    if part is not None:
        if matched_numeric:
            if part >= 12 and 1 <= hour <= 11:
                hour += 12  # "u 2 popodne" → 14:00
        else:
            hour = part      # "popodne" → 13:00

    return datetime.combine(date, dt_time(hour, minute)).isoformat(
        timespec="seconds",
    )
