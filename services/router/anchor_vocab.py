"""Croatian vocabulary for anchor generation.

Hardcoded baseline + optional LLM augmentation (see
`scripts/augment_vocab_with_llm.py`). Pure data; consumed by
`scripts/regenerate_anchors_v2.py` which generates anchors per tool
without LLM at runtime.

Filip 2026-05-17 FAZA 7A spec:
  - Anchors must match how WhatsApp drivers actually type.
  - Lowercase. No doc-style. Multiple verb synonyms per HTTP method.
  - Entity synonyms cover Croatian + anglicisms + slang.
"""
from __future__ import annotations


# ---- Verb synonyms per HTTP method ---------------------------------------
# Each verb is one cosine-search dimension. Drivers use different verbs
# for the same intent; coverage of synonyms = coverage of user queries.

VERB_SYNONYMS: dict[str, list[str]] = {
    "GET": [
        "pokaži",
        "daj mi",
        "reci mi",
        "prikaži",
        "trebam vidjeti",
        "dohvati",
        "izlistaj",
        "vidim",
        "ima li",
        "gdje su",
        "popis",
        "lista",
    ],
    "POST": [
        "dodaj",
        "stavi",
        "kreiraj",
        "novi",
        "upiši",
        "registriraj",
        "evidentiraj",
        "uploadaj",
        "prikači",
        "treba mi novi",
        "ubaci",
    ],
    "PUT": [
        "promijeni",
        "ažuriraj",
        "izmijeni",
        "korigiraj",
        "ispravi",
        "update",
        "prepravi",
        "mijenjam",
        "krivo je",
    ],
    "PATCH": [
        "promijeni",
        "izmijeni",
        "patch",
        "ažuriraj samo",
        "korigiraj",
        "ispravi dio",
        "samo to popravi",
    ],
    "DELETE": [
        "briši",
        "obriši",
        "ukloni",
        "otkaži",
        "izbriši",
        "makni",
        "izbaci",
        "ne treba mi",
    ],
}


# ---- Entity synonyms ----------------------------------------------------
# Map path-component (PascalCase from /Vehicles/{id}) → Croatian variants.
# Single keys (Vehicle) AND plural keys (Vehicles) — both appear in paths.
# Plus compound keys (VehicleCalendar, MileageReports) for direct match.

ENTITY_SYNONYMS: dict[str, list[str]] = {
    # ---- Core fleet entities ----
    "Vehicle":   ["vozilo", "auto", "automobil", "kola"],
    "Vehicles":  ["vozila", "auti", "automobili", "fleet", "voznipark"],
    "VehicleTypes":    ["tip vozila", "vrsta vozila", "kategorija vozila"],
    "VehicleContracts": ["ugovor vozila", "leasing ugovor", "ugovor za auto"],
    "VehiclesHistoricalEntries": ["povijest vozila", "history vozila", "zapisi vozila"],
    "VehicleCalendar":   ["rezervacija vozila", "kalendar vozila", "booking auta"],
    "LatestVehicleCalendar": ["zadnje rezervacije", "nedavne rezervacije", "najnovije rezervacije"],
    "LatestVehicleContracts": ["zadnji ugovori vozila", "nedavni leasing"],

    # ---- Persons / Team ----
    "Person":  ["osoba", "vozač", "zaposlenik", "korisnik", "user"],
    "Persons": ["osobe", "vozači", "zaposlenici", "korisnici", "useri"],
    "PersonTypes": ["tip osobe", "vrsta korisnika", "kategorija zaposlenika"],
    "PersonOrgUnits": ["odjel osobe", "team osobe", "organizacija zaposlenika"],
    "PersonActivityTypes": ["tip aktivnosti osobe", "vrsta zadatka", "kategorija aktivnosti"],
    "PersonPeriodicActivities": ["periodične aktivnosti osobe", "ponavljajuće obveze"],
    "LatestPersonPeriodicActivities": ["zadnje periodične aktivnosti", "nedavne obveze osobe"],
    "TeamMember":   ["član tima", "zaposlenik", "kolega"],
    "TeamMembers":  ["članovi tima", "tim", "zaposlenici", "kolege"],
    "Team":   ["tim", "ekipa", "team"],
    "Teams":  ["timovi", "ekipe", "teams"],

    # ---- Calendar & reservations ----
    "Calendar":  ["kalendar", "raspored"],
    "Reservation": ["rezervacija", "booking", "termin"],

    # ---- Documents ----
    "Document":   ["dokument", "prilog", "fajl", "scan", "datoteka"],
    "Documents":  ["dokumenti", "prilozi", "fajlovi", "scani", "datoteke"],
    "DocumentType":  ["tip dokumenta", "vrsta priloga"],
    "DocumentTypes": ["tipovi dokumenata", "vrste priloga"],

    # ---- Companies / Partners / Org ----
    "Companies":  ["kompanije", "firme", "klijenti", "partneri"],
    "Company":    ["kompanija", "firma", "klijent", "partner"],
    "Partners":   ["partneri", "firme", "klijenti", "dobavljači"],
    "Partner":    ["partner", "firma", "klijent", "dobavljač"],
    "OrgUnits":   ["organizacijske jedinice", "odjeli", "departmani"],
    "OrgUnit":    ["organizacijska jedinica", "odjel", "department"],

    # ---- Equipment ----
    "Equipment":      ["oprema", "alat", "uređaj", "stvar"],
    "EquipmentTypes": ["tipovi opreme", "vrste opreme", "kategorije alata"],
    "EquipmentType":  ["tip opreme", "vrsta opreme"],
    "EquipmentCalendar": ["rezervacija opreme", "raspored opreme", "booking alata"],
    "LatestEquipmentCalendar": ["zadnje rezervacije opreme", "nedavne rezervacije alata"],

    # ---- Expenses ----
    "Expense":       ["trošak", "putni račun", "expense"],
    "Expenses":      ["troškovi", "putni računi", "expense", "računi"],
    "ExpenseGroup":  ["grupa troškova", "kategorija troška"],
    "ExpenseGroups": ["grupe troškova", "kategorije troškova"],
    "ExpenseType":   ["tip troška", "vrsta troška"],
    "ExpenseTypes":  ["tipovi troškova", "vrste troškova"],

    # ---- Trips ----
    "Trip":      ["putovanje", "vožnja", "trip"],
    "Trips":     ["putovanja", "vožnje", "tripovi"],
    "TripType":  ["tip putovanja", "vrsta vožnje"],
    "TripTypes": ["tipovi putovanja", "vrste vožnji"],

    # ---- Mileage ----
    "Mileage":         ["km", "kilometraža", "kilometri", "stanje brojača"],
    "MileageReports":  ["km zapisi", "km izvještaji", "kilometri zapisi"],
    "LatestMileageReports": ["zadnji km zapisi", "nedavni km", "najnoviji km"],
    "MonthlyMileages": ["mjesečni km", "km po mjesecu", "monthly mileage"],

    # ---- Cases (kvarovi, prijave) ----
    "Case":      ["prijava", "slučaj", "kvar", "incident"],
    "Cases":     ["prijave", "slučajevi", "kvarovi", "incidenti"],
    "CaseType":  ["tip prijave", "vrsta kvara", "kategorija slučaja"],
    "CaseTypes": ["tipovi prijava", "vrste kvarova"],

    # ---- Pools (grupe vozila) ----
    "Pool":   ["pool", "grupa vozila", "kolekcija vozila"],
    "Pools":  ["poolovi", "grupe vozila", "kolekcije vozila"],

    # ---- Cost centers ----
    "CostCenter":  ["centar troškova", "cost center", "mjesto troška"],
    "CostCenters": ["centri troškova", "cost centers", "mjesta troškova"],

    # ---- Tenants / permissions ----
    "Tenant":   ["tenant", "najmodavac", "klijent sustava"],
    "Tenants":  ["tenanti", "najmodavci"],
    "TenantPermission":  ["dozvola tenanta", "permission", "pravo"],
    "TenantPermissions": ["dozvole tenanta", "permissions", "prava"],

    # ---- Periodic activities ----
    "PeriodicActivity":  ["periodična aktivnost", "ponavljajuća obveza", "redovita stavka"],
    "PeriodicActivities": ["periodične aktivnosti", "ponavljajuće obveze", "redovite stavke"],
    "PeriodicActivityType":  ["tip periodične aktivnosti", "vrsta ponavljajuće obveze"],
    "PeriodicActivityTypes": ["tipovi periodičnih aktivnosti", "vrste ponavljajućih obveza"],
    "PeriodicActivitiesSchedules": ["rasporedi periodičnih aktivnosti", "scheduling obveza"],
    "LatestPeriodicActivities": ["zadnje periodične aktivnosti", "nedavne obveze"],

    # ---- Scheduling ----
    "SchedulingModel":  ["model rasporeda", "raspored", "scheduling model"],
    "SchedulingModels": ["modeli rasporeda", "rasporedi", "scheduling modeli"],

    # ---- Activities (general) ----
    "Activity":   ["aktivnost", "akcija", "događaj"],
    "Activities": ["aktivnosti", "akcije", "događaji"],

    # ---- Tags ----
    "Tag":  ["tag", "oznaka", "labela"],
    "Tags": ["tagovi", "oznake", "labele"],

    # ---- Types / categories (generic) ----
    "Type":     ["tip", "vrsta", "kategorija"],
    "Types":    ["tipovi", "vrste", "kategorije"],
    "Category": ["kategorija", "tip", "vrsta"],

    # ---- Reports / metadata ----
    "Report":   ["izvještaj", "report"],
    "Reports":  ["izvještaji", "reporti"],

    # ---- Misc terms drivers use ----
    "MasterData": ["moji podaci", "trenutni podaci", "snapshot vozila"],
    "PersonData":  ["podaci osobe", "info o osobi", "korisnik info"],
    "Notifications": ["obavijesti", "notifikacije"],
    "Notification":  ["obavijest", "notifikacija"],

    # ---- Quick action endpoints ----
    "AddCase":     ["nova prijava", "novi kvar", "novi case", "prijavi kvar"],
    "AddMileage":  ["novi km", "unos km", "upiši km", "stavi km"],
    "SendEmail":   ["pošalji email", "mail", "pošalji mail"],
    "Booking":     ["rezervacija", "booking", "rezerviraj"],

    # ---- Documents helpers ----
    "Thumb":       ["sličica dokumenta", "thumbnail", "preview", "mala slika"],

    # ---- Manager-facing dashboards / stats ----
    "DashboardItems": ["stavke dashboarda", "nadzorna ploča", "dashboard"],
    "Stats":        ["statistika", "stats", "izvještaj brojki"],
    "VehicleBoard": ["ploča vozila", "vehicle board", "pregled vozila"],

    # ---- Assignments / pools / config ----
    "VehicleAssignments":         ["dodjele vozila", "assignment vozila", "tko ima koji auto"],
    "VehiclesAssignmentsOverview": ["pregled dodjela vozila", "overview dodjela", "tko ima koji auto"],
    "VehicleInputHelper":          ["pomoćnik za unos vozila", "input helper", "izbornik vozila"],
    "VehicleCalendarOn":           ["rezervacija vozila na dan", "kalendar vozila na datum"],
    "EquipmentCalendarOn":         ["rezervacija opreme na dan", "kalendar opreme na datum"],
    "EquipmentCalendarOnPersonVehicle": ["rezervacija opreme na vozilu osobe", "oprema na autu osobe"],
    "AvailableVehicles":           ["slobodna vozila", "dostupna vozila", "auti na raspolaganju"],
    "AvailabilityProjection":      ["projekcija dostupnosti", "kad je auto slobodan"],
    "Availabilities":              ["dostupnosti", "kad je slobodno"],
    "Available":                   ["dostupno", "slobodno", "raspoloživo"],
    "AverageFuelExpensesAndMileages": ["prosječna potrošnja goriva i km", "prosjek goriva po km"],
    "MonthlyFuelExpensesAndMileages": ["mjesečna potrošnja goriva i km", "mjesečni gorivo i km"],
    "MonthlyMileagesAssigned":     ["mjesečni km dodijeljeni", "mjesečna kilometraža"],
    "VehicleAverageMonthlyExpenses": ["prosječni mjesečni troškovi vozila", "prosjek troškova po vozilu"],
    "FleetAverageMonthlyMileage":  ["prosječna mjesečna km fleeta", "prosjek km flote"],
    "FleetAverageMonthlyExpenses": ["prosječni mjesečni troškovi fleeta", "prosjek troškova flote"],
    "NoMileageSince":              ["nema km od datuma", "auta bez km"],
    "NumOfVehiclesWithoutMileageSince": ["broj vozila bez km", "vozila bez unesenog km"],
    "VehiclesMonthlyExpenses":     ["mjesečni troškovi vozila", "troškovi auta po mjesecu"],
    "CompanyVehicles":             ["vozila firme", "auti kompanije"],
    "PreviousVehicle":             ["prethodno vozilo", "stari auto"],

    # ---- Status / type lookups (manager admin) ----
    "Roles":        ["uloge", "permission roles", "rola"],
    "Settings":     ["postavke", "settings", "konfiguracija"],
    "TimeZones":    ["vremenske zone", "timezones"],
    "Currencies":   ["valute", "currencies"],
    "CaseSeverities": ["razine ozbiljnosti prijave", "severity"],
    "CaseStatuses": ["statusi prijava", "case statusi"],
    "GeneralStatuses": ["opći statusi", "statusi"],
    "ExpenseStatuses": ["statusi troškova", "expense statusi"],
    "ExpenseCategories": ["kategorije troškova", "expense categories"],
    "EquipmentStatuses": ["statusi opreme", "equipment statusi"],
    "EquipmentAvailabities": ["dostupnost opreme", "kad je oprema slobodna"],
    "PeriodicActivityStatuses": ["statusi periodičnih aktivnosti", "statusi ponavljajućih obveza"],
    "EntityTypes":  ["tipovi entiteta", "entity types"],
    "CalendarEntryTypes": ["tipovi unosa u kalendaru", "calendar entry types"],
    "ActivityCategories": ["kategorije aktivnosti", "activity categories"],
    "PersonActivityCategories": ["kategorije aktivnosti osobe", "tipovi aktivnosti za korisnika"],
    "MileageSources": ["izvori km", "kako su km uneseni"],
    "SearchTypes":  ["tipovi pretrage", "search types"],
    "AcquiringType": ["tip nabave", "kako je vozilo nabavljeno"],

    # ---- Sub-paths (lowercase tokens in operations) ----
    "Defleet":      ["povući vozilo iz flote", "defleet"],
    "Infleet":      ["uvesti vozilo u flotu", "infleet"],
    "Recalculate":  ["preračunaj", "recalculate"],
    "RecalculateAll": ["preračunaj sve", "recalculate sve"],
    "Linktenant":   ["poveži s tenantom", "link tenant"],
    "Unlinktenant": ["odveži od tenanta", "unlink tenant"],
    "ResendInvitation": ["ponovo pošalji pozivnicu", "resend invite"],
    "SetRolesForUser":  ["postavi role korisniku", "dodjeli role"],
    "RequestMileageEstimation": ["zatraži procjenu km", "request km estimation"],
    "Join":         ["spoji", "join", "pridruži"],
    "Upsert":       ["upsert", "dodaj ili izmijeni"],
    "Tree":         ["stablo", "tree", "hijerarhija"],
    "Order":        ["redoslijed", "order", "poredak"],
    "Master":       ["master", "glavni"],
    "Parse":        ["parsiraj", "parse"],
    "Demo":         ["demo", "primjer"],
    "WhatCanIDo":   ["što mogu", "moje opcije", "help", "pomoć"],
    "ExpensesInputHelper": ["pomoćnik za unos troškova", "expense input helper"],
    "People":       ["ljudi", "osobe", "people"],

    # ---- External info ----
    "External-info": ["external info", "vanjski podaci"],

    # ---- Internal-ish (rarely user-facing but covered) ----
    "Lookup":       ["lookup", "pretraga", "traži"],
    "SetAsDefault": ["postavi kao default", "set as default"],
    "Metadata":     ["metapodaci", "meta info"],
}


# Generic fallback when entity isn't in ENTITY_SYNONYMS
GENERIC_ENTITY_HINTS = ["zapis", "stavka"]


# ---- Sentence-structure templates ----
# Used by regenerate_anchors_v2.py — each template emits one anchor with
# different stylistic register. Mix imperative, indirect, question, ultra-short.

TEMPLATE_NAMES = (
    "imperative",
    "imperative_id_hint",
    "indirect",
    "question",
    "context",
    "ultra_short",
)


def get_verb_synonyms(method: str) -> list[str]:
    """Return verb list for HTTP method. Defaults to GET if unknown."""
    return list(VERB_SYNONYMS.get(method.upper(), VERB_SYNONYMS["GET"]))


def get_entity_synonyms(entity: str) -> list[str]:
    """Return Croatian synonyms for entity. Falls back to humanized
    snake_case if not curated."""
    if not entity:
        return list(GENERIC_ENTITY_HINTS)
    if entity in ENTITY_SYNONYMS:
        return list(ENTITY_SYNONYMS[entity])
    # Try singular if plural form was passed (Vehicles → Vehicle)
    if entity.endswith("s") and entity[:-1] in ENTITY_SYNONYMS:
        return list(ENTITY_SYNONYMS[entity[:-1]])
    # Fallback: humanize CamelCase → "vehicle calendar"
    import re
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", entity).lower()
    return [spaced]


def count_curated_entities() -> int:
    """Sanity helper for tests — how many entities are in our vocabulary."""
    return len(ENTITY_SYNONYMS)
