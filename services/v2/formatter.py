"""L8 — Template-driven Response Formatter.

Council Pass 4 DEVIL critique: 'LLMs love to embellish'. Solution:
mostly STRICT JSON-PATH templates with sanity validation. LLM only
called for irreducibly free-form responses (manager analytics
narratives), and even then output is validated to contain only values
present in the source JSON.

~12 generic shape templates (per AI Insight: shape-based, not domain-
specific), plus a strict-LLM mode for the few cases templates can't
handle.

Templates (Croatian, JSON-path placeholders):
  vehicle_data_summary           — "Vozilo: {VehicleName}, registracija {LicencePlate}, km {LastMileage}."
  vehicle_data_field             — "Tvoj/tvoja {field_label} je {value}."
  list_with_count                — "Imaš {count} {entity_label}: ..."
  empty_result                   — "Nemam podataka za {entity_label}."
  mutation_pending               — "Akcija u tijeku..."
  mutation_success               — "{action} uspješno izvršena."
  mutation_failed                — "{action} nije uspjela: {reason}"
  generic_value                  — "{label}: {value}"
  date_value                     — "{label} je {date_formatted}"
  ack                            — short ack
  fallback                       — "Nisam siguran..."
  raw_passthrough                — for cases where API already returns prose

Custom Croatian field labels per JSON key (small map; doesn't grow
with tools, only with user-facing field names):
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# Croatian labels for common JSON keys. Adding a new field = 1 line.
# This IS hardcoded knowledge but it's translation, not domain logic.
FIELD_LABELS_HR = {
    "VehicleName":           "auto",
    "FullVehicleName":       "auto",
    "LicencePlate":          "registracija",
    "VIN":                   "VIN broj",
    "LastMileage":           "kilometraža",
    "LeasingCompany":        "leasing kuća",
    "SupplierName":          "leasing kuća",
    "Co2Emission":           "CO2 emisija",
    "RegistrationExpiry":    "datum isteka registracije",
    "YearlyMileage":         "godišnji limit kilometara",
    "FuelCardPin":           "PIN kartice za gorivo",
    "MaintenanceComment":    "datum idućeg servisa",
    "FullName":              "ime",
    "Phone":                 "broj mobitela",
    "Id":                    "ID",
    "PersonId":              "person ID",
    "TenantId":              "tenant ID",
}


@dataclass(frozen=True)
class FormatResult:
    text: str
    template_used: str


# Templates where appending the "nije točno" feedback hint makes sense.
# READ_TEMPLATES = data was shown to the user; user can validate visually.
# MUTATE_TEMPLATES = irreversible action confirmation; explicit hint warranted.
# Everything else (empty/fallback/failed) skips the hint — would be noise.
_READ_TEMPLATES = frozenset({
    "vehicle_data_summary", "vehicle_data_field",
    "list_with_count", "generic_value", "date_value", "raw_passthrough",
})
_MUTATE_TEMPLATES = frozenset({"mutation_success"})

_HINT_READ = "\n\nAko nije točno, napiši 'nije točno'."
_HINT_MUTATE = (
    "\n\nAko ovo nije ono što ste tražili, "
    "napiši 'nije točno' prije potvrde."
)


def _maybe_append_hint(result: FormatResult) -> FormatResult:
    """Add Damir-feedback hint to user-validatable responses.

    The hint is the explicit ground-truth signal we ask the user for —
    they say "nije točno" when the bot mis-routed. Without that prompt,
    we'd be left guessing from heuristic negation phrases.
    """
    if result.template_used in _READ_TEMPLATES:
        return FormatResult(
            text=result.text + _HINT_READ,
            template_used=result.template_used,
        )
    if result.template_used in _MUTATE_TEMPLATES:
        return FormatResult(
            text=result.text + _HINT_MUTATE,
            template_used=result.template_used,
        )
    return result


def format_response(
    template_id: Optional[str],
    api_response_data: Any,
    *,
    field_hint: Optional[str] = None,
    extra_context: Optional[dict] = None,
) -> FormatResult:
    """Render a Croatian response using a strict template.

    template_id selects shape; api_response_data fills slots.
    Falls back to generic_value or raw_passthrough on unknown templates.
    """
    return _maybe_append_hint(
        _format_core(template_id, api_response_data, field_hint, extra_context)
    )


def _format_core(
    template_id: Optional[str],
    api_response_data: Any,
    field_hint: Optional[str],
    extra_context: Optional[dict],
) -> FormatResult:
    extra = extra_context or {}

    if template_id is None or template_id == "":
        return _format_smart_default(api_response_data, field_hint, extra)

    if template_id == "vehicle_data_summary":
        return FormatResult(
            text=_render_vehicle_summary(api_response_data, extra),
            template_used="vehicle_data_summary",
        )
    if template_id == "vehicle_data_field":
        return FormatResult(
            text=_render_field(api_response_data, field_hint, extra),
            template_used="vehicle_data_field",
        )
    if template_id == "list_with_count":
        return FormatResult(
            text=_render_list_with_count(
                api_response_data, extra.get("entity_label", "stavki")
            ),
            template_used="list_with_count",
        )
    if template_id == "empty_result":
        return FormatResult(
            text=f"Nemam podataka za {extra.get('entity_label', 'tvoj upit')}.",
            template_used="empty_result",
        )
    if template_id == "mutation_success":
        return FormatResult(
            text=f"{extra.get('action', 'Akcija')} uspješno izvršena.",
            template_used="mutation_success",
        )
    if template_id == "mutation_failed":
        reason = extra.get("reason", "neuspjelo")
        return FormatResult(
            text=f"{extra.get('action', 'Akcija')} nije uspjela: {reason}",
            template_used="mutation_failed",
        )
    if template_id == "generic_value":
        return FormatResult(
            text=_render_generic_value(api_response_data, extra),
            template_used="generic_value",
        )
    if template_id == "fallback":
        return FormatResult(
            text=extra.get(
                "message",
                "Nisam siguran kako odgovoriti. Pokušaj reformulirati."
            ),
            template_used="fallback",
        )

    # Unknown template — fall back to smart default.
    return _format_smart_default(api_response_data, field_hint, extra)


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def _render_vehicle_summary(data: Any, extra: dict) -> str:
    if not isinstance(data, dict):
        return "Nemam podataka o tvom vozilu."
    parts = []
    if data.get("VehicleName") or data.get("FullVehicleName"):
        parts.append(f"Vozilo: {data.get('VehicleName') or data['FullVehicleName']}")
    if data.get("LicencePlate"):
        parts.append(f"registracija {data['LicencePlate']}")
    if data.get("LastMileage") is not None:
        parts.append(f"kilometraža {data['LastMileage']} km")
    if data.get("LeasingCompany") or data.get("SupplierName"):
        parts.append(f"lizing {data.get('LeasingCompany') or data['SupplierName']}")
    if not parts:
        return "Nemam podataka o tvom vozilu."
    return ". ".join(parts) + "."


def _render_field(data: Any, field_hint: Optional[str], extra: dict) -> str:
    if not isinstance(data, dict):
        return "Nemam podataka."

    # Resolve which JSON key matches the user's natural-language hint.
    target_key = _resolve_field_hint_to_key(field_hint, list(data.keys()))
    if target_key is None or target_key not in data:
        return _render_vehicle_summary(data, extra)

    value = data[target_key]
    label = FIELD_LABELS_HR.get(target_key, target_key)
    if value is None or value == "":
        return f"Nemam podataka o {label}."
    return f"Tvoj/tvoja {label}: {value}"


def _render_list_with_count(data: Any, entity_label: str) -> str:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        items = data["data"]
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    else:
        items = []

    if not items:
        return f"Nemaš {entity_label}."

    n = len(items)
    if n == 1:
        return f"Imaš 1 {entity_label}."
    # First few items if they have a recognizable label.
    lines = [f"Imaš {n} {entity_label}:"]
    for i, item in enumerate(items[:5], start=1):
        label = (
            (isinstance(item, dict) and (
                item.get("Name") or item.get("Title") or
                item.get("Subject") or item.get("Description") or
                item.get("VehicleName")
            )) or f"#{i}"
        )
        lines.append(f"{i}. {label}")
    if n > 5:
        lines.append(f"... i još {n - 5}.")
    return "\n".join(lines)


def _render_generic_value(data: Any, extra: dict) -> str:
    label = extra.get("label", "Rezultat")
    if isinstance(data, dict) and len(data) == 1:
        only_value = next(iter(data.values()))
        return f"{label}: {only_value}"
    if isinstance(data, (str, int, float, bool)):
        return f"{label}: {data}"
    return f"{label}: {data}"


def _format_smart_default(
    data: Any, field_hint: Optional[str], extra: dict
) -> FormatResult:
    """No template_id given. Try to do something sensible."""
    if data is None:
        return FormatResult(
            text="Nemam podataka.", template_used="empty_result"
        )
    if isinstance(data, list):
        return FormatResult(
            text=_render_list_with_count(
                data, extra.get("entity_label", "stavki")
            ),
            template_used="list_with_count",
        )
    if isinstance(data, dict):
        if field_hint:
            return FormatResult(
                text=_render_field(data, field_hint, extra),
                template_used="vehicle_data_field",
            )
        return FormatResult(
            text=_render_vehicle_summary(data, extra),
            template_used="vehicle_data_summary",
        )
    return FormatResult(
        text=_render_generic_value(data, extra),
        template_used="generic_value",
    )


# --------------------------------------------------------------------------
# Field-hint → JSON key resolution (FREE-TEXT hint → real key match)
# --------------------------------------------------------------------------


# Croatian phrase fragments → JSON key. Order matters: SPECIFIC
# combinations (with cardinality markers like "godisnj limit") MUST
# come BEFORE generic single-keyword matches ("km") so a query like
# "koliko km godišnje mogu prijeći" resolves to YearlyMileage rather
# than the broader LastMileage.
#
# All fragments are written ASCII-diacritic-less; queries get the
# same normalization in `_resolve_field_hint_to_key` so "istječe"
# matches "istjece".
_HINT_ALIASES: list[tuple[tuple[str, ...], str]] = [
    # Specific cardinality / context combos FIRST
    (("godisnj limit", "limit kilome", "limit km"),  "YearlyMileage"),
    (("godisnj km", "godisnj kilome"),               "YearlyMileage"),
    (("istjec",),                                    "RegistrationExpiry"),
    (("istek",),                                     "RegistrationExpiry"),
    (("sljedec servis", "sledec servis", "kada idem na servis",
      "kad mi je servis", "datum servis"),           "MaintenanceComment"),
    # Identity / contact fields
    (("kako se zove", "moje ime", "koje mi je ime",
      "koje je moje ime"),                            "FullName"),
    (("broj mobitel", "broj telefon", "telefon",
      "mobitel"),                                    "Phone"),
    (("person", "personid"),                         "PersonId"),
    (("tenant",),                                    "TenantId"),
    # Vehicle data fields
    (("registrac", "tablica", "rege", "rega"),       "LicencePlate"),
    (("kilomet", "km", "prijed", "presao"),          "LastMileage"),
    (("vin", "sasij", "broj sasij"),                 "VIN"),
    (("leasing", "lizing"),                          "LeasingCompany"),
    (("co2", "emisij", "potros"),                    "Co2Emission"),
    (("pin", "kartic"),                              "FuelCardPin"),
    (("servis", "odrzav"),                           "MaintenanceComment"),
    # Generic vehicle / driver fallbacks LAST
    (("auto", "vozilo", "kakav auto", "kakvo vozil"), "VehicleName"),
    (("ime",),                                        "FullName"),
]


def _strip_diacritics(text: str) -> str:
    """Lower-case + drop Croatian diacritics so HINT_ALIASES (ASCII)
    can match raw user queries.

    B2 fix (consolidation): delegates to the canonical implementation
    in `services.text_normalizer.normalize_diacritics`. The previous
    inline NFKD-only impl had a latent bug — Unicode `đ` (U+0111) does
    NOT decompose under NFKD, so "đ" survived the strip. The canonical
    `str.translate()` table handles it explicitly.
    """
    from services.text_normalizer import normalize_diacritics
    return normalize_diacritics(text.lower())


def field_hint_resolves(field_hint: Optional[str], available_keys: list) -> bool:
    """Public: True if `field_hint` maps to one of `available_keys` via the
    curated alias map. Callers outside this module use this (not the private
    resolver) to decide the deterministic fast-path vs the LLM fallback."""
    return _resolve_field_hint_to_key(field_hint, available_keys) is not None


def _resolve_field_hint_to_key(
    field_hint: Optional[str], available_keys: list,
) -> Optional[str]:
    if not field_hint:
        return None
    hint_norm = _strip_diacritics(field_hint)

    # Combination rules first — when BOTH terms are present anywhere
    # in the query (not necessarily adjacent), the more specific
    # field wins. Otherwise "koliko km godisnje" matches "km" against
    # LastMileage before we even check YearlyMileage.
    combo_rules: list[tuple[tuple[str, ...], str]] = [
        (("godisnj", "km"),               "YearlyMileage"),
        (("godisnj", "kilom"),            "YearlyMileage"),
        (("godisnj", "limit"),            "YearlyMileage"),
        (("istjec", "registrac"),         "RegistrationExpiry"),
        (("istek", "registrac"),          "RegistrationExpiry"),
    ]
    for required_terms, key in combo_rules:
        if all(t in hint_norm for t in required_terms):
            if key in available_keys:
                return key

    for fragments, key in _HINT_ALIASES:
        if any(f in hint_norm for f in fragments):
            if key in available_keys:
                return key
    return None
