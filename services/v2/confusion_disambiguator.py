"""Per-cluster negative anchors for confused entity disambiguation.

For known confused entity pairs (CostCenters↔Expenses, OrgUnits↔Companies, ...)
build PAIRWISE DISCRIMINATOR anchors that explicitly state when query points
to one vs the other. At retrieval time, subtract negative match scores
from candidates whose entity is contradicted by the discriminator.

Differs from `when_not_to_use` (banned -3.7pp): that was per-TOOL penalty
("don't pick X if not Y"). This is per-CLUSTER discriminator that exploits
the SAME signal both directions ("amounts → Expenses, buckets → CostCenters").
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from services.utils.vector import cosine_similarity

logger = logging.getLogger(__name__)


# Pairwise discriminators for known confusion clusters.
# Each entry: signal phrase → entity it points TO (other entity in pair
# is implicitly the "loser" candidate when this signal fires).
DISCRIMINATORS: list[dict] = [
    {
        "cluster": ["CostCenters", "Expenses"],
        "signals": [
            ("konkretni iznos / specifični iznos / amount / financijska stavka", "Expenses"),
            ("centar troškova / cost center / budget bucket / financijska jedinica", "CostCenters"),
            ("trošak / izdatak / rashod / račun", "Expenses"),
        ],
    },
    {
        "cluster": ["CostCenters", "OrgUnits"],
        "signals": [
            ("financijska alokacija / cost allocation / budget", "CostCenters"),
            ("organizacijska jedinica / odjel / sektor / ustrojstvo", "OrgUnits"),
            ("poslovna lokacija / business location / fizička jedinica", "OrgUnits"),
        ],
    },
    {
        "cluster": ["OrgUnits", "Companies"],
        "signals": [
            ("interni odjel / sektor / ustrojstvena jedinica", "OrgUnits"),
            ("tvrtka / firma / kompanija / korporacija / poduzeće / pravna osoba", "Companies"),
        ],
    },
    {
        "cluster": ["MileageReports", "LatestMileageReports"],
        "signals": [
            ("povijest / sve / svi zapisi / cijela history", "MileageReports"),
            ("zadnji / najnoviji / aktualni / trenutni / latest", "LatestMileageReports"),
        ],
    },
    {
        "cluster": ["PeriodicActivities", "PersonPeriodicActivities"],
        "signals": [
            ("servis vozila / pregled vozila / aktivnost na vozilu", "PeriodicActivities"),
            ("medicinski pregled / edukacija / osobno / moja", "PersonPeriodicActivities"),
        ],
    },
    {
        "cluster": ["PeriodicActivitiesSchedules", "PersonPeriodicActivities"],
        "signals": [
            ("raspored / kalendar / hodogram / kad se sljedeći put radi", "PeriodicActivitiesSchedules"),
            ("aktivnost osobe / moja periodička / osobni zadatak", "PersonPeriodicActivities"),
        ],
    },
    {
        "cluster": ["Lookup", "Companies"],
        "signals": [
            ("informacije o tvrtkama / popis tvrtki / detalji firme", "Companies"),
            ("dropdown / select izbor / form polje / lookup za odabir", "Lookup"),
        ],
    },
    {
        "cluster": ["DocumentTypes", "OrgUnits"],
        "signals": [
            ("tip dokumenta / kategorija isprave / vrsta akta", "DocumentTypes"),
            ("organizacijska jedinica / odjel", "OrgUnits"),
        ],
    },
    {
        "cluster": ["PersonOrgUnits", "OrgUnits"],
        "signals": [
            ("kome pripada / pripadnost / mapiranje osoba", "PersonOrgUnits"),
            ("sama organizacijska jedinica / odjel kao entitet", "OrgUnits"),
        ],
    },
]


def extract_entity_from_tool_id(tool_id: str) -> Optional[str]:
    if not tool_id:
        return None
    m = re.match(r"^(?:get|post|put|patch|delete)_([A-Z][A-Za-z0-9]+)", tool_id)
    return m.group(1) if m else None


@dataclass(frozen=True)
class DiscriminatorVector:
    cluster: tuple[str, str]      # the two entities being disambiguated
    signal_text: str              # phrase that anchors this discriminator
    favored_entity: str           # entity the signal points TO
    vector: list[float]


class ConfusionDisambiguator:
    """Pairwise discriminator vectors for known confused entity clusters.

    On every query, compute cosine to all discriminator vectors. If a
    discriminator scores above threshold (i.e., the query semantics match
    the signal), boost candidates whose entity = favored, demote candidates
    whose entity = the opposing entity in the cluster.
    """

    def __init__(self):
        self._discriminators: list[DiscriminatorVector] = []
        self._initialized = False

    async def initialize(self, embedder) -> None:
        if self._initialized:
            return
        all_signals: list[tuple[tuple[str, str], str, str]] = []
        for entry in DISCRIMINATORS:
            cluster = tuple(entry["cluster"])  # type: ignore[arg-type]
            for signal_text, favored in entry["signals"]:
                all_signals.append((cluster, signal_text, favored))

        texts = [s[1] for s in all_signals]
        vectors = await embedder.embed_batch(texts)
        for (cluster, sig, favored), vec in zip(all_signals, vectors):
            if vec is None:
                continue
            self._discriminators.append(DiscriminatorVector(
                cluster=cluster,  # type: ignore[arg-type]
                signal_text=sig,
                favored_entity=favored,
                vector=vec,
            ))
        self._initialized = True
        logger.info(
            "ConfusionDisambiguator initialized: %d discriminators across %d clusters",
            len(self._discriminators),
            len({d.cluster for d in self._discriminators}),
        )

    def size(self) -> int:
        return len(self._discriminators)

    def score_query(
        self,
        query_vector: list[float],
        threshold: float = 0.78,
    ) -> dict[str, float]:
        """For a query, return per-entity bias score.

        A discriminator that scores >= threshold against the query
        contributes +(score) to favored_entity and -(score) to the
        opposing entity in its cluster.

        Returns: dict {entity_name: bias_score}. Positive = boost,
        negative = demote.
        """
        if not self._initialized:
            return {}
        bias: dict[str, float] = {}
        for d in self._discriminators:
            sim = cosine_similarity(query_vector, d.vector)
            if sim < threshold:
                continue
            other = d.cluster[0] if d.cluster[1] == d.favored_entity else d.cluster[1]
            bias[d.favored_entity] = bias.get(d.favored_entity, 0.0) + sim
            bias[other] = bias.get(other, 0.0) - sim
        return bias


def apply_disambiguator_bias(
    candidates: list,
    bias: dict[str, float],
    bias_weight: float = 0.05,
) -> list:
    """Re-score candidates using per-entity bias map. Returns re-sorted list.

    Candidates is a list of objects with `tool_id` and `anchor_score` attrs
    (e.g. ToolCandidate). The function does NOT drop candidates — it
    only re-orders. Recall is preserved.
    """
    if not bias or not candidates:
        return candidates
    rescored = []
    for i, c in enumerate(candidates):
        ent = extract_entity_from_tool_id(c.tool_id)
        b = bias.get(ent, 0.0) if ent else 0.0
        new_score = c.anchor_score + b * bias_weight
        rescored.append((-new_score, i, c))
    rescored.sort()
    return [c for _, _, c in rescored]
