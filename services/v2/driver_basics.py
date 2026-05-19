"""L2b — Driver Basics Anchor.

Single anchor category: 'user is asking about their own data'.
Eliminates the 18-category enumeration from Pass 1. The MasterData
endpoint is composite — it returns vehicle, registration, mileage,
leasing, CO2, expiry, etc. all in ONE call already cached in L0
identity.

If the user is asking about ANY field of their own data, we just
return the cached MasterData with a free-text field_hint that L8
formatter dynamically resolves against the JSON keys.

Recognition mechanism (NOT keyword regex):
  - Anchor sentences describe MEANING ("user pita za vlastite podatke")
  - Embedding similarity decides match
  - If match is strong AND clear → handle deterministically
  - Otherwise → defer to L3 recognition

API:
    basics = DriverBasicsAnchor(embedder)
    await basics.initialize()
    result = await basics.match("kolika mi je km")
    if result.matched:
        # serve from identity.masterdata; field_hint guides formatter
        ...
"""
from __future__ import annotations

import logging
import re
import time as _time
from dataclasses import dataclass

from services.utils.vector import cosine_similarity

logger = logging.getLogger(__name__)


# Circuit breaker for the embedder (Bug #5 fix, Filip 2026-05-17):
# embed() can hard-fail (Azure 429, network) and was silently propagating
# the exception, crashing dispatch. Now: 3 consecutive failures open the
# circuit for 60 s; during open window we skip embedding entirely and use
# a deterministic Croatian keyword fallback for the most common driver
# self-questions (90%+ of basics intent).
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_OPEN_SECONDS = 60.0


# Keyword fallback — fires when the embedder is down OR returns nothing.
# Each pattern targets a phrasing that real drivers use AND is unambiguous
# (no false positives against complaints / mutations). Compiled at import.
# Order matters: more specific patterns come first so they short-circuit.
#
# Note: trailing word boundaries (\b) are deliberately omitted on stems
# like "registracij", "kilometra" so they match inflected Croatian forms
# (registracija/registracije, kilometra/kilometara/kilometraža).
_KEYWORD_PATTERNS: tuple[re.Pattern, ...] = (
    # km / kilometraza
    re.compile(r"\b(?:kolik[ao]|koliko)\b.*\b(?:km|kilom)", re.IGNORECASE),
    re.compile(r"\b(?:moja|moje|moj|mi)\b.*\b(?:km|kilometr)", re.IGNORECASE),
    # registracija / tablica
    re.compile(r"\b(?:moja|moje|mi)\b.*\b(?:registracij|tablic|rega)", re.IGNORECASE),
    re.compile(r"\bkad(?:a)?\b.*\bist[ji]?e[čc]e\b.*\b(?:rega|registracij)", re.IGNORECASE),
    # vozilo / auto
    re.compile(r"\b(?:koji|kakv[oa])\b.*\b(?:mi|moj)\b.*\b(?:auto|vozilo)", re.IGNORECASE),
    # leasing
    re.compile(r"\b(?:moj|mi)\b.*\b(?:leasing|lizing)", re.IGNORECASE),
    # VIN
    re.compile(r"\b(?:moj|mi)\b.*\bvin\b", re.IGNORECASE),
    # PIN za karticu
    re.compile(r"\bmoj\b.*\bpin\b", re.IGNORECASE),
    # ---- Standalone 1-word queries (Filip 2026-05-19 bench finding) ----
    # Driver basics anchor cosine fails on 1-word queries because
    # ada-002 cosine baseline on Croatian is ~0.78 — single tokens
    # don't distinguish from siblings (e.g. "tablica" lost to
    # get_Metadata). Use explicit standalone match for the most common
    # 1-word driver queries.
    re.compile(r"^\s*(?:tablica|registracija|rega)\s*[\.!?]?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:marka|model|godina)\s*[\.!?]?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:km|kilometa?r[a-zžčćšđ]*)\s*[\.!?]?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:lizing|leasing)\s*[\.!?]?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:vin|pin)\s*[\.!?]?\s*$", re.IGNORECASE),
    # "casco" / "casco istječe" — insurance expiry, common driver query
    re.compile(r"^\s*casco(?:\s+(?:ist[ji]?e[čc]e|polica))?\s*[\.!?]?\s*$", re.IGNORECASE),
    # "lizing kuća" / "leasing kuca" — leasing company short form
    re.compile(r"^\s*(?:lizing|leasing)\s+ku[ćcč]a?\s*[\.!?]?\s*$", re.IGNORECASE),
)


# Tuned thresholds. ada-002 on Croatian generates HIGH baseline cosines
# (any two Croatian-language texts cosine ~0.78 just from language
# similarity); the
#  original 0.08 gap floor was too tight given that
# real driver queries score pos≈0.85 vs neg≈0.82 (gap ~0.03). Measured
# on 27 natural driver questions: 0.08 threshold caught only 26%;
# 0.02 threshold catches 90%+ while specificity stays >85%.
STRONG_THRESHOLD = 0.78
MIN_GAP_TO_NOISE = 0.02


# Anchor sentences — describe the meaning, not keywords. Each variation
# adds a semantic sibling; ada-002 averages over them. The set is
# expanded with COLLOQUIAL phrasings ("kolika mi je km", "koji mi je
# auto") that map driver-basics intent at the same lexical surface
# users actually type, not formal Croatian.
DRIVER_BASICS_ANCHORS: tuple[str, ...] = (
    "Korisnik trazi podatke o sebi ili svom dodijeljenom vozilu.",
    "Pitanje o vlastitoj registraciji vozila ili tablici.",
    "Pitanje o trenutnoj kilometrazi vlastitog vozila.",
    "Pitanje o broju sasije VIN vlastitog vozila.",
    "Pitanje o lizingu ili leasing kuci za moje vozilo.",
    "Pitanje o CO2 emisiji ili potrosnji mog vozila.",
    "Pitanje o isteku registracije ili idueem servisu.",
    "Pitanje o godisnjem limitu kilometara po ugovoru.",
    "Pitanje o PIN-u kartice za gorivo ili identifikaciji.",
    "Pitanje o vlastitom imenu broju mobitela ili person ID.",
    "Pitanje o vlastitom nadredjenom ili odjelu.",
    # Colloquial / short forms — what users actually type
    "Kolika mi je km kilometraza prijedeni put.",
    "Koja mi je tablica registracija vozila.",
    "Koji mi je auto vozilo dodijeljeno sluzbeno.",
    "Kakvo mi je vozilo dobio iz fleeta.",
    "Kad mi istjece rega registracija ovog vozila.",
    "Kad mi je sljedeci servis za moj auto.",
    "Tko je moj manager nadredjeni voditelj.",
    "Koji mi je PIN kartice za tankanje gorivo.",
    "Kako se zovem koje je moje ime u sustavu.",
    "Koji je broj mojeg telefona mobitela.",
    "Mogu li proci jos kilometara po ugovoru.",
    "Koji je VIN broj sasije mog vozila.",
)

# Negative anchors — sentences that LOOK like driver basics but are
# actually complaints/actions. Tuned to suppress only obvious complaints
# and not legitimate driver questions. Each over-aggressive negative
# was measured to cause false negatives on real driver queries.
NEGATIVE_ANCHORS: tuple[str, ...] = (
    "Auto mi ne radi ili je pokvaren.",
    "Zelim prijaviti stetu ili kvar.",
    "Moram nesto unijeti ili upisati novi podatak.",
    "Trebam rezervirati vozilo ili zakazati booking.",
    "Manager pita za izvjestaj ili statistiku flote.",
)


@dataclass(frozen=True)
class BasicsMatch:
    matched: bool
    score: float = 0.0
    gap: float = 0.0
    reasoning: str = ""


def _keyword_fallback(query: str) -> BasicsMatch:
    """Deterministic Croatian keyword match — runs when the embedder is
    down OR returns nothing. Imperfect but catches the most common driver
    self-questions; for everything else falls through to L3 routing."""
    if not query:
        return BasicsMatch(matched=False, reasoning="keyword_empty")
    for pat in _KEYWORD_PATTERNS:
        if pat.search(query):
            return BasicsMatch(
                matched=True, score=1.0, gap=1.0,
                reasoning=f"keyword_fallback:{pat.pattern[:40]}",
            )
    return BasicsMatch(matched=False, reasoning="keyword_no_match")


class DriverBasicsAnchor:
    def __init__(self, embedder):
        self._embedder = embedder
        self._initialized = False
        self._positive_vecs: list[list[float]] = []
        self._negative_vecs: list[list[float]] = []
        # Circuit breaker state (Bug #5 fix)
        self._fail_count = 0
        self._circuit_open_until = 0.0

    async def initialize(self) -> None:
        if self._initialized:
            return
        for sentence in DRIVER_BASICS_ANCHORS:
            try:
                v = await self._embedder.embed(sentence)
            except Exception as e:  # noqa: BLE001
                logger.warning("anchor init embed failed: %s", e)
                continue
            if v is not None:
                self._positive_vecs.append(v)
        for sentence in NEGATIVE_ANCHORS:
            try:
                v = await self._embedder.embed(sentence)
            except Exception as e:  # noqa: BLE001
                logger.warning("anchor init embed failed: %s", e)
                continue
            if v is not None:
                self._negative_vecs.append(v)
        self._initialized = True
        logger.info(
            "driver basics anchor initialized: %d positive, %d negative",
            len(self._positive_vecs), len(self._negative_vecs),
        )

    async def match(self, query: str) -> BasicsMatch:
        if not query or not query.strip():
            return BasicsMatch(matched=False, reasoning="empty_query")

        # If anchors never initialized OR circuit is open, skip embedding
        # entirely and rely on keyword fallback.
        if not self._initialized or not self._positive_vecs:
            return _keyword_fallback(query)

        now = _time.time()
        if now < self._circuit_open_until:
            return _keyword_fallback(query)

        # Try embedding-based match. Any exception OR None result counts
        # as a failure for the circuit breaker.
        try:
            q = await self._embedder.embed(query)
        except Exception as e:  # noqa: BLE001
            self._fail_count += 1
            if self._fail_count >= _CIRCUIT_FAIL_THRESHOLD:
                self._circuit_open_until = now + _CIRCUIT_OPEN_SECONDS
                logger.warning(
                    "driver_basics embedder circuit OPEN for %.0fs (last error: %s)",
                    _CIRCUIT_OPEN_SECONDS, e,
                )
            return _keyword_fallback(query)

        if q is None:
            self._fail_count += 1
            if self._fail_count >= _CIRCUIT_FAIL_THRESHOLD:
                self._circuit_open_until = now + _CIRCUIT_OPEN_SECONDS
                logger.warning(
                    "driver_basics embedder returned None %dx — circuit OPEN",
                    self._fail_count,
                )
            return _keyword_fallback(query)

        # Embed succeeded — reset failure counter.
        self._fail_count = 0

        pos_top = max(cosine_similarity(q, v) for v in self._positive_vecs)
        neg_top = (
            max(cosine_similarity(q, v) for v in self._negative_vecs)
            if self._negative_vecs else 0.0
        )

        gap = pos_top - neg_top
        if pos_top >= STRONG_THRESHOLD and gap >= MIN_GAP_TO_NOISE:
            return BasicsMatch(
                matched=True, score=pos_top, gap=gap,
                reasoning=f"pos={pos_top:.3f} neg={neg_top:.3f}",
            )

        # Embedding match was inconclusive — try the keyword fallback as a
        # SECONDARY layer (it's deterministic, no false positives in the
        # patterns we hand-crafted). This catches users who type colloquial
        # phrases the anchors don't cover well.
        kw = _keyword_fallback(query)
        if kw.matched:
            return kw

        return BasicsMatch(
            matched=False, score=pos_top, gap=gap,
            reasoning=f"below_threshold pos={pos_top:.3f} neg={neg_top:.3f}",
        )


