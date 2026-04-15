"""
Concept Mapper - Jargon to Standard Term Translation

Expands user queries by translating:
1. Croatian jargon -> standard Croatian terms
2. Colloquial expressions -> formal terms
3. Abbreviations -> full words

This improves FAISS search by ensuring the query embedding
captures related concepts even if the user uses informal language.

Examples:
- "daj mi auto" -> "daj mi auto vozilo vehicle prikaži dohvati get"
- "unesi km" -> "unesi km kilometraža mileage dodaj kreiraj post"
"""

import logging
import re
from typing import Dict, List, Set, Optional
import threading

from services.text_normalizer import normalize_diacritics

logger = logging.getLogger(__name__)

# Pre-compiled regex for stripping non-word characters in expand_query()
_NON_WORD_RE = re.compile(r'[^\w]')


class ConceptMapper:
    """
    Maps informal/jargon terms to standard vocabulary for better semantic search.

    Philosophy:
    - EXPAND, don't replace - keep original terms plus add synonyms
    - Language-aware - handles Croatian characters (č, ć, ž, š, đ)
    - Domain-specific - fleet management vocabulary
    """

    # Jargon -> Standard terms mapping
    # Key: informal term (lowercase, without Croatian diacritics for matching)
    # Value: list of standard terms to ADD to the query
    CONCEPT_MAP: Dict[str, List[str]] = {
        # Vehicle terms
        "auto": ["vozilo", "vehicle"],
        "auti": ["vozila", "vehicles"],
        "kola": ["vozilo", "vehicle"],
        "karavan": ["vozilo", "kombi", "vehicle"],
        "kombi": ["vozilo", "dostavno", "vehicle"],
        "kamion": ["vozilo", "teretno", "vehicle"],

        # Action terms - GET
        "daj": ["prikaži", "dohvati", "get", "prikazi"],
        "daj mi": ["prikaži", "dohvati", "get"],
        "pokaži": ["prikaži", "dohvati", "get", "list"],
        "pokazi": ["prikaži", "dohvati", "get", "list"],
        "kaj ima": ["prikaži", "dohvati", "lista", "popis", "get"],
        "sta ima": ["prikaži", "dohvati", "lista", "popis", "get"],
        "što ima": ["prikaži", "dohvati", "lista", "popis", "get"],
        "trebam": ["dohvati", "prikaži", "get", "potrebno"],
        "treba mi": ["dohvati", "prikaži", "get", "potrebno"],
        "treba": ["dohvati", "prikaži", "get"],
        "ima li": ["provjeri", "dostupnost", "available", "get"],

        # Action terms - CREATE/POST
        "unesi": ["dodaj", "kreiraj", "post", "add", "create"],
        "upiši": ["dodaj", "kreiraj", "post", "add", "create"],
        "upisi": ["dodaj", "kreiraj", "post", "add", "create"],
        "stavi": ["dodaj", "unesi", "post", "add"],
        "napravi": ["kreiraj", "dodaj", "post", "create"],
        "otvori": ["kreiraj", "dodaj", "post", "create"],
        "prijavi": ["kreiraj", "dodaj", "case", "post"],

        # Action terms - UPDATE
        "promijeni": ["ažuriraj", "update", "patch", "izmijeni"],
        "promjeni": ["ažuriraj", "update", "patch", "izmijeni"],
        "izmjeni": ["ažuriraj", "update", "patch"],
        "izmijeni": ["ažuriraj", "update", "patch"],
        "popravi": ["ažuriraj", "update", "patch", "ispravak"],

        # Action terms - DELETE
        "makni": ["obriši", "ukloni", "delete", "remove"],
        "briši": ["obriši", "delete", "remove"],
        "brisi": ["obriši", "delete", "remove"],
        "izbaci": ["obriši", "ukloni", "delete", "remove"],

        # Measurement terms
        "km": ["kilometraža", "mileage", "kilometri"],
        "kilasa": ["kilometraža", "mileage"],
        "prijeđeno": ["kilometraža", "mileage", "udaljenost"],
        "prijedeno": ["kilometraža", "mileage", "udaljenost"],
        "kolko je prešo": ["kilometraža", "mileage", "get"],
        "koliko ima km": ["kilometraža", "mileage", "get"],

        # Registration terms
        "reg": ["registracija", "registration", "tablice"],
        "tablice": ["registracija", "registration", "licencePlate"],
        "tablica": ["registracija", "registration", "licencePlate"],
        "registracija": ["registration", "licencePlate"],
        "istek": ["datum", "expiration", "istječe", "validnost"],

        # Case/Issue terms
        "šteta": ["slučaj", "case", "kvar", "oštećenje"],
        "steta": ["slučaj", "case", "kvar", "oštećenje"],
        "kvar": ["slučaj", "case", "problem", "defekt"],
        "problem": ["slučaj", "case", "kvar", "issue"],
        "guma": ["slučaj", "case", "servis", "gume"],
        "nesreća": ["slučaj", "case", "nezgoda", "accident"],
        "nesreca": ["slučaj", "case", "nezgoda", "accident"],
        "prometna": ["slučaj", "case", "nezgoda", "accident"],

        # Person/Role terms
        "šef": ["voditelj", "manager", "nadređeni"],
        "sef": ["voditelj", "manager", "nadređeni"],
        "gazda": ["voditelj", "manager", "vlasnik"],
        "vozač": ["driver", "osoba", "korisnik"],
        "vozac": ["driver", "osoba", "korisnik"],
        "ja": ["moje", "korisnik", "person", "moj"],
        "meni": ["moje", "korisnik", "person", "moj"],

        # Organization terms
        "firma": ["kompanija", "company", "tvrtka", "tenant"],
        "posao": ["kompanija", "company", "organizacija"],
        "odjel": ["organizacijska jedinica", "orgUnit", "department"],
        "služba": ["organizacijska jedinica", "orgUnit", "department"],
        "sluzba": ["organizacijska jedinica", "orgUnit", "department"],

        # Booking/Calendar terms
        "rezerviraj": ["rezervacija", "booking", "calendar", "post"],
        "zakaži": ["rezervacija", "booking", "calendar", "post"],
        "zakazi": ["rezervacija", "booking", "calendar", "post"],
        "zauzmi": ["rezervacija", "booking", "calendar", "post"],
        "slobodno": ["dostupno", "available", "slobodna vozila"],
        "slobodan": ["dostupan", "available"],
        "termin": ["vrijeme", "period", "calendar", "slot"],

        # Leasing/Contract terms
        "lizing": ["leasing", "najam", "ugovor", "contract"],
        "rata": ["mjesečna rata", "monthly", "payment"],
        "ugovor": ["contract", "leasing", "najam"],
        "najam": ["leasing", "rent", "contract"],

        # Document terms
        "dokument": ["documents", "file", "prilog", "attachment"],
        "papir": ["dokument", "documents", "file"],
        "prilog": ["dokument", "attachment", "file"],
        "slika": ["dokument", "image", "photo", "attachment"],
        "fotografija": ["dokument", "image", "photo", "attachment"],

        # Time terms
        "sutra": ["datum", "tomorrow", "time"],
        "danas": ["datum", "today", "time"],
        "ovaj tjedan": ["period", "this week", "time"],
        "sljedeći tjedan": ["period", "next week", "time"],
        "sljedeci tjedan": ["period", "next week", "time"],

        # Status terms
        "status": ["stanje", "state", "status"],
        "stanje": ["status", "state"],
        "gdje je": ["lokacija", "location", "status", "get"],

        # Suffix/Action concept mappings — bridge Croatian verbs to English tool suffixes
        # GroupBy
        "grupiraj": ["GroupBy", "grupiranje", "grupa", "po kategoriji"],
        "grupirane": ["GroupBy", "grupiranje", "grupa"],
        "grupirano": ["GroupBy", "grupiranje", "grupa"],
        "po grupama": ["GroupBy", "grupiranje"],
        # Agg (aggregation)
        "agregiraj": ["Agg", "agregacija", "statistika", "suma", "prosjek"],
        "agregirane": ["Agg", "agregacija", "statistika"],
        "ukupno": ["Agg", "agregacija", "suma", "total"],
        "prosjek": ["Agg", "agregacija", "average", "prosječno"],
        "prosječna": ["Agg", "agregacija", "average"],
        "prosjecna": ["Agg", "agregacija", "average"],
        "statistika": ["Agg", "agregacija", "GroupBy", "statistika"],
        # MileageReports
        "kilometraža": ["MileageReports", "km", "kilometri", "prijeđeni put"],
        "kilometraza": ["MileageReports", "km", "kilometri", "prijedeni put"],
        # PeriodicActivities
        "periodične": ["PeriodicActivities", "servis", "redovne aktivnosti"],
        "periodicne": ["PeriodicActivities", "servis", "redovne aktivnosti"],
        "periodičke": ["PeriodicActivities", "servis", "redovne aktivnosti"],
        # Documents/Metadata nested resources
        "metapodaci": ["metadata", "shema", "struktura", "polja"],
        "metapodatci": ["metadata", "shema", "struktura", "polja"],
        # Bulk operations
        "bulk": ["multipatch", "grupno ažuriranje", "više stavki"],
        "masovno": ["multipatch", "bulk", "grupno ažuriranje"],
        # Thumbnail
        "thumbnail": ["thumb", "sličica", "preview"],
        "slicica": ["thumb", "thumbnail", "preview"],
        "preview": ["thumb", "thumbnail", "sličica"],
        # Tree
        "stablo": ["tree", "hijerarhija", "struktura"],
        "hijerarhija": ["tree", "stablo", "struktura"],

        # Entity synonyms — legitimate Croatian vocabulary for fleet management concepts
        # Vehicle-related
        "motorno sredstvo": ["vozilo", "vehicle", "oprema", "equipment"],
        "prijevozno sredstvo": ["vozilo", "vehicle", "oprema", "equipment"],
        "prijevozno": ["vozilo", "vehicle"],
        "motorno": ["vozilo", "vehicle", "oprema", "equipment"],
        # Equipment-related
        "mehanicka jedinica": ["oprema", "equipment", "uređaj"],
        "mehanicke jedinice": ["oprema", "equipment", "uređaji"],
        "sredstvo": ["oprema", "equipment", "inventar"],
        "inventar": ["oprema", "equipment", "sredstva"],
        "instrument": ["oprema", "equipment", "alat", "uređaj"],
        "instrumenti": ["oprema", "equipment", "alati"],
        # Document synonyms
        "spis": ["dokument", "document", "datoteka", "prilog"],
        "isprava": ["dokument", "document", "certifikat"],
        "ispravu": ["dokument", "document", "certifikat"],
        "akt": ["dokument", "document", "spis"],
        "zapisnik": ["dokument", "document", "prilog"],
        "obrazac": ["dokument", "document", "formular"],
        "potvrda": ["dokument", "document", "certifikat"],
        # Calendar/schedule synonyms
        "hodogram": ["kalendar", "raspored", "calendar", "schedule"],
        "plan termina": ["kalendar", "raspored", "calendar", "schedule"],
        "agenda": ["raspored", "schedule", "periodicactivities"],
        "program": ["raspored", "schedule", "periodicactivities"],
        # Organization synonyms
        "upravna cjelina": ["organizacijska jedinica", "orgUnit", "odjel"],
        "upravne cjeline": ["organizacijske jedinice", "orgUnit", "odjeli"],
        "administrativni segment": ["organizacijska jedinica", "orgUnit", "odjel"],
        "radna skupina": ["tim", "team", "ekipa", "grupa"],
        "radne skupine": ["timovi", "teams", "ekipe", "grupe"],
        # Financial synonyms
        "financijski zapis": ["trošak", "expense", "izdatak"],
        "financijske stavke": ["troškovi", "expenses", "izdaci"],
        "potrosnja": ["trošak", "expense", "izdatak"],
        # Case synonyms
        "predmet": ["slučaj", "case", "stavka"],
        "stavka": ["slučaj", "case", "predmet", "item"],
        # Contract synonyms
        "akvizicija": ["ugovor", "contract", "leasing", "nabava"],
        "obveza": ["ugovor", "contract", "leasing"],
        "financijske obveze": ["ugovori", "contracts", "leasing"],
        # Activity synonyms
        "redovni posao": ["periodična aktivnost", "periodicactivities", "servis"],
        "redovni poslovi": ["periodične aktivnosti", "periodicactivities", "servisi"],
        "obaveza": ["aktivnost", "periodicactivities", "zadatak"],
        # Scheduling
        "model rasporedivanja": ["schedulingmodels", "raspored", "scheduling"],
        "model raspoređivanja": ["schedulingmodels", "raspored", "scheduling"],
        # Reference data
        "sifrarnik": ["lookup", "referentni podaci", "katalog"],
        "referentni podaci": ["lookup", "šifrarnik", "katalog"],
        # Metadata
        "karakteristike": ["metapodaci", "metadata", "polja", "atributi"],
        "atributi": ["metapodaci", "metadata", "polja", "karakteristike"],
        # Type/Category words
        "kategorija": ["tip", "vrsta", "type", "category"],
        "klasa": ["tip", "vrsta", "type", "category", "class"],
        "razred": ["tip", "vrsta", "type", "category"],
    }

    # Phrase patterns that should trigger concept expansion (pre-compiled)
    # These are regex patterns that capture common query structures
    PHRASE_PATTERNS = [
        # "daj mi [X]" patterns
        (re.compile(r'\bdaj\s+mi\b'), ["prikaži", "dohvati", "get"]),
        # "trebam [X]" patterns
        (re.compile(r'\btreba(m)?\s+mi?\b'), ["dohvati", "prikaži", "get", "potrebno"]),
        # "pokaži mi [X]" = "show me [X]"
        (re.compile(r'\bpoka[zž]i\s+mi\b'), ["prikaži", "dohvati", "get", "list"]),
        # "upiši/unesi [X]" = "enter/input [X]"
        (re.compile(r'\b(upi[sš]i|unesi)\b'), ["dodaj", "kreiraj", "post", "add"]),
        # "koliko [X]" patterns
        (re.compile(r'\bkoliko\b'), ["broj", "count", "get", "količina"]),
        # "koji/koja/koje [X]" = "which [X]"
        (re.compile(r'\bkoj[aei]\b'), ["lista", "popis", "get", "which"]),
        # "ima li" = "is there / does it have"
        (re.compile(r'\bima\s+li\b'), ["dostupnost", "provjeri", "check", "available"]),
    ]

    def __init__(self, enabled: bool = True):
        """
        Initialize concept mapper.

        Args:
            enabled: If False, expand_query returns original query unchanged
        """
        self.enabled = enabled
        self._build_normalized_map()
        logger.info(f"ConceptMapper initialized with {len(self.CONCEPT_MAP)} concept mappings")

    def _build_normalized_map(self) -> None:
        """Build a normalized version of the concept map for faster lookups."""
        self._normalized_map: Dict[str, List[str]] = {}

        for key, values in self.CONCEPT_MAP.items():
            # Normalize key (remove diacritics for matching)
            normalized_key = self._normalize(key)

            # Store both original and normalized keys
            if key not in self._normalized_map:
                self._normalized_map[key] = values
            if normalized_key not in self._normalized_map:
                self._normalized_map[normalized_key] = values

    def _normalize(self, text: str) -> str:
        """
        Normalize text by removing Croatian diacritics.

        This allows matching both "šef" and "sef", "čekaj" and "cekaj", etc.
        """
        return normalize_diacritics(text)

    def expand_query(self, query: str) -> str:
        """
        Expand query with related concepts.

        IMPORTANT: This ADDS terms, it doesn't replace.
        The original query is always preserved.

        Args:
            query: Original user query

        Returns:
            Expanded query with original + related terms
        """
        if not self.enabled:
            return query

        if not query or not query.strip():
            return query

        query_lower = query.lower()
        query_normalized = self._normalize(query_lower)

        # Collect all expansions
        expansions: Set[str] = set()

        # 1. Check phrase patterns first (multi-word patterns, pre-compiled)
        for pattern, terms in self.PHRASE_PATTERNS:
            if pattern.search(query_lower) or pattern.search(query_normalized):
                expansions.update(terms)

        # 2. Check individual terms
        words = query_lower.split()
        normalized_words = query_normalized.split()

        for word in words + normalized_words:
            # Remove punctuation for matching (pre-compiled at module level)
            clean_word = _NON_WORD_RE.sub('', word)

            if clean_word in self._normalized_map:
                expansions.update(self._normalized_map[clean_word])

        # 3. Check two-word phrases
        for i in range(len(words) - 1):
            two_word = f"{words[i]} {words[i+1]}"
            normalized_two_word = f"{normalized_words[i]} {normalized_words[i+1]}"

            if two_word in self._normalized_map:
                expansions.update(self._normalized_map[two_word])
            elif normalized_two_word in self._normalized_map:
                expansions.update(self._normalized_map[normalized_two_word])

        # 4. Build expanded query
        if not expansions:
            logger.debug(f"No concept expansions for: '{query}'")
            return query

        # Remove any expansions that are already in the query
        new_terms = [term for term in expansions
                     if term.lower() not in query_lower
                     and self._normalize(term.lower()) not in query_normalized]

        if not new_terms:
            logger.debug(f"All expansions already in query: '{query}'")
            return query

        # PHASE 3 OPTIMIZATION: Limit expansion to max 5 terms
        # to prevent query explosion and maintain embedding quality
        MAX_EXPANSION_TERMS = 5
        if len(new_terms) > MAX_EXPANSION_TERMS:
            logger.debug(f"Limiting expansions from {len(new_terms)} to {MAX_EXPANSION_TERMS}")
            new_terms = new_terms[:MAX_EXPANSION_TERMS]

        expanded = f"{query} {' '.join(new_terms)}"

        logger.debug(f"Expanded query: '{query}' -> '{expanded}'")
        return expanded

    def get_expansions_only(self, query: str) -> List[str]:
        """
        Get just the expansion terms without the original query.

        Useful for debugging or displaying what terms were added.
        """
        original = query
        expanded = self.expand_query(query)

        if expanded == original:
            return []

        # Extract added terms
        added_part = expanded[len(original):].strip()
        return added_part.split() if added_part else []


# Singleton instance
_concept_mapper: Optional[ConceptMapper] = None
_singleton_lock = threading.Lock()


def get_concept_mapper(enabled: bool = True) -> ConceptMapper:
    """Get or create singleton ConceptMapper instance."""
    global _concept_mapper
    if _concept_mapper is None:
        with _singleton_lock:
            if _concept_mapper is None:
                _concept_mapper = ConceptMapper(enabled=enabled)
    return _concept_mapper


def expand_query(query: str) -> str:
    """
    Convenience function to expand a query.

    Usage:
        from services.concept_mapper import expand_query
        expanded = expand_query("daj mi auto")
        # Returns: "daj mi auto vozilo vehicle prikaži dohvati get"
    """
    return get_concept_mapper().expand_query(query)
