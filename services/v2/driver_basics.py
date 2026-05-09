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
from dataclasses import dataclass

from services.utils.vector import cosine_similarity

logger = logging.getLogger(__name__)


# Tuned thresholds. ada-002 on Croatian generates HIGH baseline cosines
# (any two Croatian-language texts cosine ~0.78 just from language
# similarity); the original 0.08 gap floor was too tight given that
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


class DriverBasicsAnchor:
    def __init__(self, embedder):
        self._embedder = embedder
        self._initialized = False
        self._positive_vecs: list[list[float]] = []
        self._negative_vecs: list[list[float]] = []

    async def initialize(self) -> None:
        if self._initialized:
            return
        for sentence in DRIVER_BASICS_ANCHORS:
            v = await self._embedder.embed(sentence)
            if v is not None:
                self._positive_vecs.append(v)
        for sentence in NEGATIVE_ANCHORS:
            v = await self._embedder.embed(sentence)
            if v is not None:
                self._negative_vecs.append(v)
        self._initialized = True
        logger.info(
            "driver basics anchor initialized: %d positive, %d negative",
            len(self._positive_vecs), len(self._negative_vecs),
        )

    async def match(self, query: str) -> BasicsMatch:
        if not self._initialized or not self._positive_vecs:
            return BasicsMatch(matched=False, reasoning="not_initialized")
        if not query or not query.strip():
            return BasicsMatch(matched=False, reasoning="empty_query")

        q = await self._embedder.embed(query)
        if q is None:
            return BasicsMatch(matched=False, reasoning="embed_failed")

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
        return BasicsMatch(
            matched=False, score=pos_top, gap=gap,
            reasoning=f"below_threshold pos={pos_top:.3f} neg={neg_top:.3f}",
        )


