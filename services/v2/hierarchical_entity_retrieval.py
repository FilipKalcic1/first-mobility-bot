"""Hierarchical entity-level retrieval (Stage 1: entity, Stage 2: tool).

Trenutni flat retrieval na 4012 anchorima daje 25% strict / 92% entity-in-top-50.
Razmak: judge ne uspijeva razlikovati 50 sličnih tools u flat space-u.

Ovaj modul implementira 2-stage retrieval:

  Stage 1 — Entity classification via embedding cosine
    query embedding × 47 entity descriptions (curated Croatian) → top-N entities

  Stage 2 — Per-entity tool retrieval
    For each Stage 1 entity, cosine query × that entity's tool anchors only
    → top-K candidates per entity, merged with rank fusion

  Stage 3 — Pass to LLM judge
    Smaller, entity-coherent candidate set → easier disambiguation

Why this is structurally different from existing service/category hierarchy:
  - service/category hierarchy operates at coarse level (~10 services × ~100
    categories), embedding cosine on category TEXT
  - this operates at ENTITY level (47 entiteta), embedding cosine on
    curated Croatian entity DESCRIPTIONS that explicitly disambiguate
    confusion clusters (e.g. "CostCenters = financijske jedinice za
    alokaciju troškova, NIJE Expenses (ti su iznosi)")

Theoretical ceiling on this corpus: 67.8% strict (oracle entity + perfect
within-entity selection). Empirically aim: 30-35% strict, 50-60% user-perceived.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from services.utils.vector import cosine_similarity

logger = logging.getLogger(__name__)


# Curated Croatian entity descriptions with explicit disambiguation hints
# for known confusion clusters. Each description is anchor for embedding
# at Stage 1 of hierarchical retrieval.
ENTITY_DESCRIPTIONS: dict[str, str] = {
    'VehicleCalendar': (
        "Rezervacije vozila — kad netko rezervira auto za neki termin. "
        "Booking calendar entries. Aktivne i prošle rezervacije."
    ),
    'LatestVehicleCalendar': (
        "Aktivne / trenutne / zadnje rezervacije vozila — filtriran view "
        "samo na one koje su trenutno relevantne, ne povijest."
    ),
    'VehicleCalendarOn': (
        "Stanje rezervacija vozila na specifičan datum — snapshot za "
        "određeni dan."
    ),
    'MileageReports': (
        "Pojedinačni zapisi o pređenim kilometrima. Mileage entries — "
        "svaki red je jedan zapis km u određenom trenutku."
    ),
    'LatestMileageReports': (
        "Najnoviji / aktualni kilometarski zapisi — filtriran view, "
        "ne cijela povijest."
    ),
    'MonthlyMileages': (
        "Mjesečni agregati kilometraže po vozilima. Month-level summaries, "
        "ne pojedinačni zapisi."
    ),
    'AddMileage': (
        "Akcija unosa novog kilometarskog zapisa — driver upisuje novu "
        "vrijednost km svog vozila."
    ),
    'Cases': (
        "Slučajevi — prijave šteta, kvarova, incidenata. Tickets / cases / "
        "report štete. Ne tipovi slučajeva (CaseTypes), nego stvarne prijave."
    ),
    'AddCase': (
        "Akcija otvaranja nove prijave (case) — driver prijavljuje štetu "
        "ili kvar."
    ),
    'CaseTypes': (
        "Tipovi / kategorije prijava (npr. udes, kvar, parking). NIJE "
        "stvarni slučaj nego catalog kategorija."
    ),
    'Persons': (
        "Osobe — zaposlenici, vozači, korisnici sustava. People records "
        "u sustavu."
    ),
    'PersonOrgUnits': (
        "Veza osoba ↔ organizacijska jedinica — kome (osobi) pripada koja "
        "OrgUnit. Mapping table, NIJE OrgUnit sam."
    ),
    'OrgUnits': (
        "Organizacijske jedinice — odjeli, sektori, ustrojstvo poduzeća. "
        "Business units. NIJE Companies (tvrtke) i NIJE CostCenters (financije)."
    ),
    'Companies': (
        "Tvrtke / kompanije / firme / korporacije — pravne osobe. "
        "Top-level legal entities. NIJE OrgUnit (interni odjeli)."
    ),
    'CostCenters': (
        "Troškovni centri — financijske jedinice za alokaciju troškova "
        "unutar tvrtke. Cost centers = budget buckets. NIJE Expenses "
        "(stvarni troškovi s iznosima) i NIJE OrgUnits (organizacijske jedinice)."
    ),
    'Expenses': (
        "Troškovi / izdaci / rashodi — pojedinačne stavke s konkretnim "
        "iznosom. Expense entries with amounts. NIJE CostCenter (to su "
        "buckets) i NIJE ExpenseTypes (to su kategorije)."
    ),
    'ExpenseTypes': (
        "Tipovi troškova — kategorije kao gorivo, servis, parking. "
        "Catalog, NIJE pojedinačan trošak."
    ),
    'ExpenseGroups': (
        "Grupe troškova — viša razina kategorizacije, nadkategorija "
        "ExpenseTypes."
    ),
    'Vehicles': (
        "Vozila — fizički auti u floti. Cars / vehicles records. NIJE "
        "VehicleTypes (to su kategorije)."
    ),
    'VehicleTypes': (
        "Tipovi vozila (kombi, kamion, putnički auto) — catalog, "
        "NIJE pojedinačno vozilo."
    ),
    'Equipment': (
        "Oprema — vrijedna materijalna imovina, ne vozila. Equipment "
        "records (npr. uređaji, alati)."
    ),
    'EquipmentCalendar': (
        "Rezervacije opreme — booking calendar za equipment, kao "
        "VehicleCalendar ali za alate/uređaje."
    ),
    'LatestEquipmentCalendar': (
        "Aktivne / zadnje rezervacije opreme — filtriran view."
    ),
    'EquipmentCalendarOn': (
        "Stanje rezervacija opreme na datum."
    ),
    'EquipmentTypes': (
        "Tipovi opreme — catalog kategorija, NIJE pojedinačan komad opreme."
    ),
    'Trips': (
        "Putovanja / zabilježene vožnje — actual driving trips, "
        "service / private. NIJE TripTypes."
    ),
    'TripTypes': (
        "Tipovi putovanja (službeno, privatno) — catalog kategorija."
    ),
    'Pools': (
        "Poolovi vozila — grupe vozila za zajedničku rezervaciju. "
        "Vehicle pools."
    ),
    'Tags': (
        "Oznake / labele za kategorizaciju entiteta. Tag system."
    ),
    'Teams': (
        "Timovi — organizacijska podgrupa. Team records."
    ),
    'TeamMembers': (
        "Članovi tima — veza osoba ↔ tim. Team membership records, "
        "NIJE Teams sam."
    ),
    'PeriodicActivities': (
        "Periodičke aktivnosti — servis, registracija, redoviti pregledi. "
        "Recurring activities ON ENTITIES (vozila, oprema), NIJE osobne "
        "aktivnosti i NIJE rasporedi."
    ),
    'PeriodicActivitiesSchedules': (
        "Rasporedi periodičkih aktivnosti — kad se sljedeći put rade. "
        "Schedule entries, NIJE aktivnosti same."
    ),
    'PersonPeriodicActivities': (
        "Periodičke aktivnosti vezane ZA OSOBU — medicinski pregled, "
        "edukacija, zdravstveno. Person-level recurring tasks. RAZLIČITO "
        "od PeriodicActivities (koje su za vozila/opremu)."
    ),
    'LatestPersonPeriodicActivities': (
        "Aktivni / zadnji zapisi osobnih periodičkih aktivnosti."
    ),
    'PeriodicActivityTypes': (
        "Tipovi periodičkih aktivnosti — catalog."
    ),
    'PersonActivityTypes': (
        "Tipovi aktivnosti za osobu — catalog."
    ),
    'VehicleContracts': (
        "Ugovori vezani za vozila — leasing, najam, kupoprodaja. "
        "Contract records."
    ),
    'LatestVehicleContracts': (
        "Aktivni ugovori vozila — filtered view."
    ),
    'DocumentTypes': (
        "Tipovi dokumenata (prometna, polica osiguranja, prometna dozvola). "
        "Document catalog, NIJE pojedinačan dokument."
    ),
    'Tenants': (
        "Tenanti — klijenti sustava (multitenancy). System-level tenant "
        "records."
    ),
    'TenantPermissions': (
        "Ovlasti / prava pristupa unutar tenanta. Permission system."
    ),
    'Roles': (
        "Korisničke uloge (driver, manager, admin). Role definitions."
    ),
    'WhatCanIDo': (
        "Specijalni endpoint — što logirani korisnik smije. User capability "
        "introspection."
    ),
    'Lookup': (
        "Meta-endpoint za dropdown / enum izbore — kad treba lista "
        "Companies / OrgUnits / itd. ZA POPUNJAVANJE FORMI, ne za prikaz "
        "entiteta korisniku."
    ),
    'Metadata': (
        "Metapodaci / sheme / strukture — forma, obrazac. Schema definitions."
    ),
    'SchedulingModels': (
        "Modeli rasporeda — template-i za scheduling."
    ),
    'AvailabilityProjection': (
        "Projekcija dostupnosti vozila u budućnosti."
    ),
    'Upsert': (
        "Akcija paralelnog ažuriraj-ili-dodaj — specijalni mass operation."
    ),
    'SendEmail': (
        "Akcija slanja emaila — notifications."
    ),
    'VehicleAssignments': (
        "Dodjele / predaje vozila osobama. Vehicle handover / assignment "
        "records."
    ),
    'VehiclesHistoricalEntries': (
        "Povijesni zapisi vezani uz vozila — historical timeline."
    ),
    'Partners': (
        "Poslovni partneri / pružatelji usluga (servisi, snabdjevači). "
        "Partner records."
    ),
}


def extract_entity_from_tool_id(tool_id: str) -> Optional[str]:
    """Extract entity name from tool_id like 'delete_VehicleCalendar_id'."""
    if not tool_id:
        return None
    m = re.match(r"^(?:get|post|put|patch|delete)_([A-Z][A-Za-z0-9]+)", tool_id)
    return m.group(1) if m else None


@dataclass(frozen=True)
class EntityHit:
    entity: str
    score: float


@dataclass(frozen=True)
class HierarchicalCandidate:
    tool_id: str
    entity: str
    entity_score: float          # Stage 1 cosine
    anchor_score: float          # Stage 2 cosine within entity
    combined_score: float        # weighted sum


class EntityIndex:
    """Stage 1 entity classifier index.

    Each entity has 1 description embedded — query embedding is matched
    against all 47 entity embeddings via cosine.
    """

    def __init__(self):
        self._entity_vectors: dict[str, list[float]] = {}
        self._initialized = False

    async def initialize(self, embedder) -> None:
        """Embed all entity descriptions. Idempotent."""
        if self._initialized:
            return
        # Batch-embed for efficiency
        entities = list(ENTITY_DESCRIPTIONS.keys())
        descriptions = [ENTITY_DESCRIPTIONS[e] for e in entities]
        vectors = await embedder.embed_batch(descriptions)
        for ent, vec in zip(entities, vectors):
            self._entity_vectors[ent] = vec
        self._initialized = True
        logger.info(
            "EntityIndex initialized: %d entities, %d-dim embeddings",
            len(self._entity_vectors),
            len(vectors[0]) if vectors else 0,
        )

    def size(self) -> int:
        """Public accessor — number of entity vectors held."""
        return len(self._entity_vectors)

    def classify(self, query_vector: list[float], top_k: int = 3) -> list[EntityHit]:
        """Return top-K entities by cosine similarity to query."""
        if not self._initialized:
            raise RuntimeError("EntityIndex not initialized")
        scored = [
            EntityHit(entity=ent, score=cosine_similarity(query_vector, vec))
            for ent, vec in self._entity_vectors.items()
        ]
        scored.sort(key=lambda h: -h.score)
        return scored[:top_k]


class PerEntityToolIndex:
    """Stage 2 per-entity sub-indices.

    Anchora se grupiraju po entity-u. Za query, samo se računa cosine
    nad anchorima entiteta koji su prošli Stage 1.
    """

    def __init__(self):
        # entity → list of (tool_id, anchor_text, vector)
        self._buckets: dict[str, list[tuple[str, str, list[float]]]] = {}
        self._initialized = False

    def build(self, anchors: list[tuple[str, str, list[float]]]) -> None:
        """Group anchors by extracted entity. anchors is the engine's
        primary anchor list (tool_id, anchor_text, vector)."""
        self._buckets = {}
        unbucketed = 0
        for tool_id, text, vec in anchors:
            entity = extract_entity_from_tool_id(tool_id)
            if entity is None:
                unbucketed += 1
                continue
            self._buckets.setdefault(entity, []).append((tool_id, text, vec))
        self._initialized = True
        logger.info(
            "PerEntityToolIndex built: %d entities, %d total anchors, %d unbucketed",
            len(self._buckets),
            sum(len(v) for v in self._buckets.values()),
            unbucketed,
        )

    def retrieve_within_entities(
        self,
        query_vector: list[float],
        entities: list[str],
        per_entity_top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Cosine query against tool anchors within given entities only.

        Returns merged list of (tool_id, anchor_score) sorted desc.
        Per-entity top-K limits how many tools per entity bucket compete.
        """
        if not self._initialized:
            raise RuntimeError("PerEntityToolIndex not built")
        merged: list[tuple[str, float]] = []
        for ent in entities:
            bucket = self._buckets.get(ent, [])
            if not bucket:
                continue
            scored = [
                (tool_id, cosine_similarity(query_vector, vec))
                for tool_id, _text, vec in bucket
            ]
            scored.sort(key=lambda x: -x[1])
            merged.extend(scored[:per_entity_top_k])
        merged.sort(key=lambda x: -x[1])
        return merged


def hierarchical_retrieve(
    query_vector: list[float],
    entity_index: EntityIndex,
    tool_index: PerEntityToolIndex,
    *,
    entity_top_k: int = 3,
    per_entity_tool_top_k: int = 10,
    final_top_k: int = 50,
    entity_score_weight: float = 0.3,
    anchor_score_weight: float = 0.7,
) -> list[HierarchicalCandidate]:
    """End-to-end Stage 1 + Stage 2.

    Returns top-K HierarchicalCandidate entries ranked by combined_score.

    The combined_score blends entity-level cosine (Stage 1) and tool-level
    cosine (Stage 2). Empirical tuning may shift the weights — defaults
    favor anchor score (within-entity differentiation) but keep entity
    signal as a tiebreaker on close-call entities.
    """
    # Stage 1: pick top-N entities
    entity_hits = entity_index.classify(query_vector, top_k=entity_top_k)
    entity_score_map = {h.entity: h.score for h in entity_hits}
    if not entity_hits:
        return []

    # Stage 2: retrieve tools within those entities
    chosen_entities = [h.entity for h in entity_hits]
    tool_hits = tool_index.retrieve_within_entities(
        query_vector, chosen_entities, per_entity_top_k=per_entity_tool_top_k,
    )

    # Stage 3: combine scores
    candidates: list[HierarchicalCandidate] = []
    for tool_id, anchor_score in tool_hits:
        ent = extract_entity_from_tool_id(tool_id)
        ent_score = entity_score_map.get(ent, 0.0)
        combined = (
            entity_score_weight * ent_score
            + anchor_score_weight * anchor_score
        )
        candidates.append(HierarchicalCandidate(
            tool_id=tool_id,
            entity=ent or "",
            entity_score=ent_score,
            anchor_score=anchor_score,
            combined_score=combined,
        ))

    candidates.sort(key=lambda c: -c.combined_score)
    return candidates[:final_top_k]
