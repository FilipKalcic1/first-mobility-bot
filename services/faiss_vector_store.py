"""
FAISS Vector Store - In-memory semantic search for tool selection.

ZERO DATABASE IMPACT - All operations in memory.

Uses tool_documentation.json (ACCURATE) as the source.
Does NOT use training_queries.json (UNRELIABLE).

Performance:
- Search latency: ~1-5ms (vs ~50ms with O(n) cosine loop)
- Memory: ~50MB for 950 tools (1536 dims * 4 bytes * 950)
- Startup: ~2s if embeddings cached, ~5min if regenerating
"""

import json
import logging
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import threading

from services.tracing import get_tracer, trace_span
from services.errors import SearchError, ErrorCode

import re
import time

import numpy as np
import faiss
from prometheus_client import Histogram

from config import get_settings

logger = logging.getLogger(__name__)
_tracer = get_tracer("faiss_vector_store")

FAISS_SEARCH_DURATION = Histogram(
    'faiss_search_duration_seconds',
    'FAISS vector search duration',
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5],
)

EMBEDDING_API_DURATION = Histogram(
    'embedding_api_duration_seconds',
    'Azure OpenAI embedding API call duration',
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)


def _get_settings():
    """Lazy settings access — avoid module-level parsing before env vars are set."""
    return get_settings()

# Cache directory for embeddings
CACHE_DIR = Path(__file__).parent.parent / ".cache"
EMBEDDINGS_FILE = CACHE_DIR / "tool_embeddings.json"
EMBEDDING_DIM = 1536  # Azure text-embedding-ada-002

# Bump when `_build_embedding_text` template changes. Old caches with a different
# version are rejected on load so stale vectors don't produce stale rankings.
METHOD_VERB_TEXT = {
    "get": "dohvati prikaži pogledaj vrati",
    "post": "dodaj kreiraj napravi unesi",
    "put": "ažuriraj zamijeni izmijeni",
    "patch": "djelomično ažuriraj parcijalno promijeni",
    "delete": "obriši ukloni izbriši makni brisanje",
}

EMBED_TEXT_VERSION = "v8.0-ada002-azure"

@dataclass
class SearchResult:
    """Result from FAISS search (optionally annotated by boost_engine)."""
    tool_id: str
    score: float  # Cosine similarity (0-1), or post-boost score after boost_engine
    method: str   # HTTP method (GET/POST/PUT/DELETE)
    base_score: float = 0.0  # Pre-boost FAISS score; set by boost_engine
    boosts_applied: List[tuple] = field(default_factory=list)  # [(name, delta, score_after)]

class FAISSVectorStore:
    """
    In-memory vector store using FAISS for fast similarity search.

    Key features:
    - Uses tool_documentation.json as source (ACCURATE)
    - Caches embeddings to disk for fast startup
    - FAISS IndexFlatIP for exact cosine similarity
    - Zero database impact

    IMPORTANT: This class does NOT use training_queries.json because
    it is unreliable (55% coverage, word overlap issues).
    """

    def __init__(self) -> None:
        """Initialize the vector store."""
        self._index: Optional[faiss.IndexFlatIP] = None
        self._tool_ids: List[str] = []  # Maps FAISS index to tool_id
        self._tool_methods: Dict[str, str] = {}  # tool_id -> HTTP method
        self._embeddings: Dict[str, List[float]] = {}
        self._initialized = False
        self._openai_client = None
        # Entity centroids for embedding-based entity classification
        self._entity_centroids: Dict[str, np.ndarray] = {}  # entity -> normalized centroid vector

        # Ensure cache directory exists
        CACHE_DIR.mkdir(exist_ok=True)

        logger.info("FAISSVectorStore created (not yet initialized)")

    async def initialize(
        self,
        tool_documentation: Dict,
        tool_registry_tools: Optional[Dict] = None
    ) -> None:
        """
        Initialize the vector store with tool documentation.

        Args:
            tool_documentation: Dict from tool_documentation.json
            tool_registry_tools: Optional dict of UnifiedToolDefinition for method info
        """
        if self._initialized:
            logger.info("FAISSVectorStore already initialized")
            return

        logger.info(f"Initializing FAISSVectorStore with {len(tool_documentation)} tools...")

        # Extract HTTP methods from registry if available
        if tool_registry_tools:
            for tool_id, tool in tool_registry_tools.items():
                self._tool_methods[tool_id] = getattr(tool, 'method', 'GET')
        else:
            # Fallback: Extract methods from tool_id prefix (get_, post_, put_, delete_)
            for tool_id in tool_documentation:
                tool_lower = tool_id.lower()
                if tool_lower.startswith("get_"):
                    self._tool_methods[tool_id] = "GET"
                elif tool_lower.startswith("post_"):
                    self._tool_methods[tool_id] = "POST"
                elif tool_lower.startswith("put_"):
                    self._tool_methods[tool_id] = "PUT"
                elif tool_lower.startswith("patch_"):
                    self._tool_methods[tool_id] = "PATCH"
                elif tool_lower.startswith("delete_"):
                    self._tool_methods[tool_id] = "DELETE"
                else:
                    self._tool_methods[tool_id] = "GET"  # Default
            logger.info(f"Extracted HTTP methods from tool_id prefixes: {len(self._tool_methods)} tools")

        # Try to load cached embeddings (offload blocking I/O to thread)
        cached = await asyncio.to_thread(self._load_cached_embeddings)

        # Determine which tools need embedding generation
        tools_to_embed = []
        for tool_id in tool_documentation:
            if tool_id not in cached:
                tools_to_embed.append(tool_id)

        logger.info(f"Cached: {len(cached)}, Need to generate: {len(tools_to_embed)}")

        # Seed with cached embeddings. _generate_embeddings appends the missing
        # ones; if there are none, this is just a plain assignment.
        self._embeddings = cached
        if tools_to_embed:
            await self._generate_embeddings(tools_to_embed, tool_documentation)

        # Build FAISS index
        self._build_index()

        # Compute entity centroids for embedding-based entity classification
        self._compute_entity_centroids()

        self._initialized = True
        logger.info(f"FAISSVectorStore initialized: {len(self._tool_ids)} tools indexed")

    def _load_cached_embeddings(self) -> Dict[str, List[float]]:
        """Load embeddings from cache file. Returns {} if cache is absent, unreadable, or stale."""
        try:
            with open(EMBEDDINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            err = SearchError(ErrorCode.FAISS_NOT_INITIALIZED, f"Failed to load cached embeddings: {e}")
            logger.warning(str(err))
            return {}

        # Nested format carries a schema version. Reject mismatches so a template
        # change in `_build_embedding_text` doesn't silently keep stale vectors.
        if "embeddings" in data and isinstance(data["embeddings"], dict):
            cached_version = data.get("embed_text_version")
            if cached_version and cached_version != EMBED_TEXT_VERSION:
                logger.info(
                    f"Embedding cache version mismatch "
                    f"(cached={cached_version}, current={EMBED_TEXT_VERSION}) — rebuilding"
                )
                return {}
            embeddings = data["embeddings"]
            logger.info(f"Loaded {len(embeddings)} cached embeddings (nested format)")
        else:
            # Flat legacy format — no version tag. Accept but log once; rebuild will
            # eventually upgrade it to the nested format with version on next save.
            embeddings = {
                k: v for k, v in data.items()
                if isinstance(v, list) and len(v) == EMBEDDING_DIM
            }
            logger.info(f"Loaded {len(embeddings)} cached embeddings (flat legacy format)")
        return embeddings

    def _save_embeddings_to_cache(self) -> None:
        """Save embeddings to cache file (atomic write via temp file + rename)."""
        try:
            cache_dir = str(EMBEDDINGS_FILE.parent)
            fd, tmp_path = tempfile.mkstemp(suffix='.tmp', dir=cache_dir)
            try:
                payload = {
                    "embed_text_version": EMBED_TEXT_VERSION,
                    "embeddings": self._embeddings,
                }
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp_path, str(EMBEDDINGS_FILE))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            logger.info(f"Saved {len(self._embeddings)} embeddings to cache")
        except (OSError, TypeError) as e:
            err = SearchError(ErrorCode.FAISS_INDEX_CORRUPT, f"Failed to save embeddings to cache: {e}")
            logger.warning(str(err))

    async def _generate_embeddings(
        self,
        tool_ids: List[str],
        tool_documentation: Dict
    ) -> None:
        """
        Generate embeddings for tools using tool_documentation.json.

        IMPORTANT: Uses purpose, when_to_use, and example_queries_hr
        from tool_documentation.json (ACCURATE source).

        Does NOT use training_queries.json (UNRELIABLE).
        """
        from services.openai_client import get_embedding_client

        if self._openai_client is None:
            self._openai_client = get_embedding_client()

        logger.info(f"Generating embeddings for {len(tool_ids)} tools...")

        # Collect (tool_id, text) pairs up-front. ada-002 accepts a list `input`,
        # so batching cuts cold-start from ~50s+ serial to a few seconds.
        batch_items: List[tuple] = []
        for tool_id in tool_ids:
            doc = tool_documentation.get(tool_id, {})
            text = self._build_embedding_text(tool_id, doc)
            if not text:
                logger.warning(f"No text for {tool_id}, skipping")
                continue
            batch_items.append((tool_id, text[:8000]))

        settings = _get_settings()
        deployment = (settings.OPENAI_EMBEDDING_MODEL
                      if settings.OPENAI_EMBEDDING_API_KEY
                      else settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT)
        batch_size = 32
        generated_count = 0

        for start in range(0, len(batch_items), batch_size):
            chunk = batch_items[start:start + batch_size]
            ids = [it[0] for it in chunk]
            texts = [it[1] for it in chunk]
            try:
                response = await self._openai_client.embeddings.create(
                    input=texts, model=deployment,
                )
                # Azure returns data in index order, but keying by .index is safer.
                for item in response.data:
                    self._embeddings[ids[item.index]] = item.embedding
                generated_count += len(chunk)
            except Exception as e:
                err = SearchError(
                    ErrorCode.EMBEDDING_GENERATION_FAILED,
                    f"Batch embedding failed ({len(chunk)} tools starting {ids[0]}): {e}",
                )
                logger.warning(str(err))
                continue

            if generated_count % 100 < batch_size:
                logger.info(f"Generated {generated_count}/{len(batch_items)} embeddings")
                await asyncio.to_thread(self._save_embeddings_to_cache)

        await asyncio.to_thread(self._save_embeddings_to_cache)
        logger.info(f"Generated {generated_count} new embeddings")

    # Compound Index: rich Croatian entity descriptions for embedding prefix.
    # Putting entity FIRST in text gives text-embedding-ada-002 strong
    # disambiguation between tools with identical purpose text.
    # e.g., "Vozila (vehicles) - upravljanje vozilima, automobilima, flotom."
    _ENTITY_COMPOUND_PREFIX = {
        "companies": "Kompanije tvrtke firme (companies) - upravljanje kompanijama, tvrtkama, poduzećima, poslovnim subjektima.",
        "vehicles": "Vozila automobili auti (vehicles) - upravljanje vozilima, automobilima, flotom, prijevoznim sredstvima.",
        "vehicletypes": "Tipovi vozila kategorije vozila (vehicletypes) - vrste i kategorije vozila u sustavu.",
        "vehiclecalendar": "Kalendar vozila raspored rezervacija vozila (vehiclecalendar) - rezervacije, raspoloživost i kalendar korištenja vozila.",
        "vehiclecontracts": "Ugovori vozila leasing najam (vehiclecontracts) - ugovori o leasingu, najmu i korištenju vozila.",
        "vehicleassignments": "Dodjele vozila raspodjela vozila (vehicleassignments) - tko koristi koje vozilo, dodjela vozača.",
        "persons": "Osobe zaposlenici radnici djelatnici (persons) - upravljanje zaposlenicima, radnicima, korisnicima sustava.",
        "persontypes": "Tipovi osoba kategorije zaposlenika (persontypes) - vrste i kategorije zaposlenika.",
        "personorgunits": "Organizacijske jedinice osoba odjeli zaposlenika (personorgunits) - pripadnost zaposlenika organizacijskim jedinicama.",
        "personperiodicactivities": "Periodične aktivnosti osoba servisi zaposlenika (personperiodicactivities) - redovne aktivnosti dodijeljene zaposlenicima.",
        "personactivitytypes": "Tipovi aktivnosti osoba (personactivitytypes) - vrste aktivnosti koje se dodjeljuju zaposlenicima.",
        "teams": "Timovi grupe ekipe (teams) - upravljanje timovima, radnim grupama, ekipama.",
        "teammembers": "Članovi tima zaposlenici u timu (teammembers) - članstvo u timovima, tko je u kojem timu.",
        "cases": "Slučajevi predmeti štete kvarovi prijave (cases) - upravljanje slučajevima, štetama, kvarovima, prijavama problema.",
        "casetypes": "Tipovi slučajeva vrste prijava (casetypes) - kategorije slučajeva, šteta i prijava.",
        "expenses": "Troškovi izdaci računi rashodi (expenses) - upravljanje troškovima, izdacima, financijskim stavkama.",
        "expensetypes": "Tipovi troškova vrste troškova (expensetypes) - kategorije i vrste troškova.",
        "expensegroups": "Grupe troškova skupine troškova (expensegroups) - grupiranje troškova po skupinama.",
        "trips": "Putovanja putni nalozi (trips) - upravljanje putovanjima, putnim nalozima, službenim putovima.",
        "triptypes": "Tipovi putovanja vrste putovanja (triptypes) - kategorije putovanja i putnih naloga.",
        "mileage": "Kilometraža prijeđeni kilometri (mileage) - praćenje kilometraže, prijeđenog puta vozila.",
        "equipment": "Oprema inventar alati (equipment) - upravljanje opremom, inventarom, alatima, sredstvima.",
        "equipmenttypes": "Tipovi opreme vrste opreme (equipmenttypes) - kategorije i vrste opreme.",
        "equipmentcalendar": "Kalendar opreme raspored opreme (equipmentcalendar) - rezervacije i raspoloživost opreme.",
        "partners": "Partneri dobavljači klijenti suradnici (partners) - upravljanje poslovnim partnerima, dobavljačima, klijentima.",
        "documents": "Dokumenti prilozi datoteke (documents) - upravljanje dokumentima, prilozima, datotekama.",
        "documenttypes": "Tipovi dokumenata vrste dokumenata (documenttypes) - kategorije dokumenata.",
        "orgunits": "Organizacijske jedinice odjeli sektori (orgunits) - upravljanje organizacijskim jedinicama, odjelima.",
        "costcenters": "Troškovni centri mjesta troška (costcenters) - upravljanje troškovnim centrima, mjestima troška.",
        "roles": "Uloge dozvole permisije (roles) - upravljanje ulogama, dozvolama, pravima pristupa.",
        "tenants": "Tenanti najmovi korisnici sustava (tenants) - upravljanje tenantima, najmovima.",
        "tenantpermissions": "Dozvole tenanta korisničke dozvole (tenantpermissions) - upravljanje dozvolama i pravima tenanta.",
        "periodicactivities": "Periodične aktivnosti servisi redovni poslovi (periodicactivities) - upravljanje periodičnim aktivnostima, servisima.",
        "periodicactivitiesschedules": "Rasporedi periodičnih aktivnosti kalendar servisa (periodicactivitiesschedules) - rasporedi i termini periodičnih aktivnosti.",
        "schedulingmodels": "Modeli raspoređivanja rasporedi sheme (schedulingmodels) - modeli i sheme za raspoređivanje aktivnosti.",
        "tags": "Oznake tagovi labele (tags) - upravljanje oznakama, tagovima.",
        "pools": "Poolovi grupe resursa (pools) - upravljanje poolovima, grupama resursa.",
        "settings": "Postavke konfiguracija (settings) - postavke i konfiguracija sustava.",
        "metadata": "Metapodaci shema struktura polja (metadata) - metapodaci, struktura i definicija polja.",
        "booking": "Rezervacija booking zauzimanje (booking) - rezervacije, zauzimanje resursa.",
        "masterdata": "Matični podaci profil korisnika (masterdata) - matični podaci, korisnički profil.",
        "persondata": "Osobni podaci profil zaposlenika (persondata) - osobni podaci korisnika.",
        "calendar": "Kalendar raspored (calendar) - kalendar, rasporedi, termini.",
        "lookup": "Šifrarnik referentni podaci katalog (lookup) - šifrarnici, lookup tablice, referentni podaci sustava.",
        "dashboarditems": "Elementi nadzorne ploče radni prikaz (dashboarditems) - stavke na kontrolnoj ploči, dashboard elementi.",
        "vehicleshistoricalentries": "Povijesni zapisi vozila (vehicleshistoricalentries) - povijest promjena i unosa za vozila.",
        "vehiclesmonthlyexpenses": "Mjesečni troškovi vozila (vehiclesmonthlyexpenses) - mjesečni pregled troškova po vozilima.",
        "vehicleboard": "Ploča vozila pregled vozila (vehicleboard) - vizualni pregled stanja voznog parka.",
        "equipmentcalendaronpersonvehicle": "Kalendar opreme po osobi i vozilu (equipmentcalendaronpersonvehicle) - raspored opreme vezan za osobu i vozilo.",
        "availablevehicles": "Dostupna vozila slobodna vozila (availablevehicles) - popis vozila dostupnih za rezervaciju.",
        "whatcanido": "Što mogu raditi mogućnosti (whatcanido) - popis dostupnih akcija i mogućnosti korisnika.",
        "latestvehiclecalendar": "Najnoviji kalendar vozila posljednje rezervacije (latestvehiclecalendar) - najnovije rezervacije i raspoloživost vozila.",
        "latestvehiclecontracts": "Najnoviji ugovori vozila posljednji leasinzi (latestvehiclecontracts) - najnoviji ugovori o korištenju vozila.",
        "latestperiodicactivities": "Najnovije periodične aktivnosti posljednji servisi (latestperiodicactivities) - najnovije periodične aktivnosti.",
        "latestpersonperiodicactivities": "Najnovije aktivnosti osobe posljednje aktivnosti zaposlenika (latestpersonperiodicactivities) - najnovije aktivnosti dodijeljene zaposlenicima.",
    }

    def _get_entity_compound_prefix(self, tool_id: str) -> str:
        """Extract entity from tool_id and return rich compound prefix.

        Returns a descriptive Croatian sentence that anchors the embedding
        to the correct entity family, disambiguating tools with identical
        purpose text (e.g., 123 tools share 'Brisanje stavke...').
        """
        # Strip HTTP method prefix
        name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)

        # Strip suffixes to get base entity
        SUFFIX_STRIP = [
            '_id_documents_documentId_thumb', '_id_documents_documentId_SetAsDefault',
            '_id_documents_documentId', '_id_documents', '_id_metadata',
            '_DeleteByCriteria', '_multipatch', '_SetAsDefault',
            '_GroupBy', '_ProjectTo', '_Agg', '_tree', '_id',
        ]
        name_lower = name.lower()
        for suffix in SUFFIX_STRIP:
            if name_lower.endswith(suffix.lower()):
                name = name[:len(name) - len(suffix)]
                break

        # Match entity (longest first)
        name_lower = name.lower()
        for entity_key in sorted(self._ENTITY_COMPOUND_PREFIX.keys(), key=len, reverse=True):
            if entity_key in name_lower:
                return self._ENTITY_COMPOUND_PREFIX[entity_key]

        return ""

    # Entity nouns for purpose enrichment — replace generic "stavka" with entity-specific noun
    _ENTITY_PURPOSE_NOUNS = {
        "vehicles": "vozilo vozila prijevozno sredstvo",
        "vehicletypes": "tip vozila vrsta vozila kategorija vozila",
        "vehiclecalendar": "kalendar vozila rezervacija vozila raspored vozila",
        "vehiclecontracts": "ugovor vozila leasing najam vozila",
        "vehicleassignments": "dodjela vozila raspodjela vozila",
        "vehicleshistoricalentries": "povijest vozila povijesni zapis vozila",
        "vehiclesmonthlyexpenses": "mjesečni trošak vozila",
        "vehicleboard": "ploča vozila pregled voznog parka",
        "latestvehiclecalendar": "najnoviji kalendar vozila posljednja rezervacija",
        "latestvehiclecontracts": "najnoviji ugovor vozila",
        "availablevehicles": "dostupno vozilo slobodno vozilo",
        "equipment": "oprema inventar alat uređaj stroj",
        "equipmenttypes": "tip opreme vrsta opreme kategorija opreme",
        "equipmentcalendar": "kalendar opreme rezervacija opreme raspored opreme",
        "equipmentcalendaron": "kalendar opreme po filteru",
        "equipmentcalendaronpersonvehicle": "kalendar opreme po osobi i vozilu",
        "latestequipmentcalendar": "najnoviji kalendar opreme posljednja rezervacija opreme",
        "persons": "osoba zaposlenik radnik djelatnik",
        "persontypes": "tip osobe vrsta zaposlenika kategorija osobe",
        "personorgunits": "organizacijska jedinica osobe odjel zaposlenika",
        "personperiodicactivities": "periodična aktivnost osobe zadatak zaposlenika",
        "personactivitytypes": "tip aktivnosti osobe vrsta zadatka zaposlenika",
        "latestpersonperiodicactivities": "najnovija aktivnost osobe",
        "teams": "tim grupa ekipa radna skupina",
        "teammembers": "član tima pripadnik grupe",
        "cases": "slučaj predmet šteta kvar prijava problem",
        "casetypes": "tip slučaja vrsta prijave kategorija štete",
        "expenses": "trošak izdatak račun rashod financijska stavka",
        "expensetypes": "tip troška vrsta troška kategorija rashoda",
        "expensegroups": "grupa troškova skupina troškova",
        "trips": "putovanje putni nalog službeni put",
        "triptypes": "tip putovanja vrsta putnog naloga",
        "partners": "partner dobavljač klijent suradnik",
        "documents": "dokument prilog datoteka",
        "documenttypes": "tip dokumenta vrsta dokumenta kategorija dokumenta",
        "orgunits": "organizacijska jedinica odjel sektor",
        "costcenters": "troškovni centar mjesto troška",
        "roles": "uloga dozvola pravo pristupa",
        "tenants": "tenant najam korisnik sustava",
        "tenantpermissions": "dozvola tenanta korisničko pravo",
        "periodicactivities": "periodična aktivnost servis redovni posao",
        "periodicactivitiesschedules": "raspored periodičnih aktivnosti termin servisa",
        "periodicactivitytypes": "tip periodične aktivnosti vrsta servisa",
        "schedulingmodels": "model raspoređivanja shema rasporeda",
        "tags": "oznaka tag labela",
        "pools": "pool grupa resursa",
        "metadata": "metapodatak shema struktura polje definicija",
        "lookup": "šifrarnik referentni podatak katalog",
        "dashboarditems": "element nadzorne ploče dashboard stavka",
        "booking": "rezervacija zauzimanje",
        "mileagereports": "izvještaj o kilometraži prijeđeni kilometri",
        "latestmileagereports": "najnoviji izvještaj o kilometraži",
        "master": "matični podatak profil",
        "persondata": "osobni podatak profil zaposlenika",
        "vehiclesassignmentsoverview": "pregled dodjela vozila",
        "mileage": "kilometraža prijeđeni put",
    }

    def _build_embedding_text(self, tool_id: str, doc: Dict) -> str:
        """Build text for embedding — V7.3: full baseline + enriched example queries."""
        parts = []

        entity_prefix = self._get_entity_compound_prefix(tool_id)
        if entity_prefix:
            parts.append(entity_prefix)

        purpose = doc.get("purpose", "")
        entity_key = self._extract_entity_key(tool_id)
        entity_nouns = self._ENTITY_PURPOSE_NOUNS.get(entity_key, "")
        if purpose:
            if entity_nouns:
                parts.append(f"{purpose} ({entity_nouns})")
            else:
                parts.append(purpose)

        when_to_use = doc.get("when_to_use", [])
        if when_to_use:
            parts.append(" ".join(when_to_use))

        method_prefix = tool_id.split("_")[0].lower()
        method_verbs = METHOD_VERB_TEXT.get(method_prefix, "")
        if method_verbs:
            parts.append(method_verbs)

        synonyms = doc.get("synonyms_hr", [])
        if synonyms:
            parts.append(" ".join(synonyms))

        if entity_prefix:
            parts.append(entity_prefix)

        return " ".join(parts)

    def _extract_entity_key(self, tool_id: str) -> str:
        """Extract entity key from tool_id for purpose enrichment."""
        name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)
        name_lower = name.lower()
        # Match longest entity key first
        for entity_key in sorted(self._ENTITY_PURPOSE_NOUNS.keys(), key=len, reverse=True):
            if name_lower.startswith(entity_key):
                return entity_key
        # Fallback: use parts[1]
        parts = tool_id.split("_")
        if len(parts) >= 2:
            return parts[1].lower()
        return ""

    def _build_index(self) -> None:
        """Build FAISS index from embeddings."""
        if not self._embeddings:
            logger.warning("No embeddings to index")
            return

        # Convert embeddings to numpy array
        self._tool_ids = list(self._embeddings.keys())
        embeddings_matrix = np.array(
            [self._embeddings[tool_id] for tool_id in self._tool_ids],
            dtype=np.float32
        )

        # Normalize for cosine similarity (IndexFlatIP = inner product)
        faiss.normalize_L2(embeddings_matrix)

        # Create FAISS index
        self._index = faiss.IndexFlatIP(EMBEDDING_DIM)
        self._index.add(embeddings_matrix)

        logger.info(f"FAISS index built: {self._index.ntotal} vectors")

    def _compute_entity_centroids(self) -> None:
        """Compute average embedding per entity for embedding-based entity classification.

        Groups all tool embeddings by entity (extracted from tool_id), computes
        the centroid (mean), and L2-normalizes it for cosine similarity lookup.
        """
        from collections import defaultdict

        entity_vectors: Dict[str, List[np.ndarray]] = defaultdict(list)

        for tool_id, embedding in self._embeddings.items():
            parts = tool_id.split("_")
            if len(parts) < 2:
                continue
            entity = parts[1].lower()
            entity_vectors[entity].append(np.array(embedding, dtype=np.float32))

        self._entity_centroids = {}
        for entity, vectors in entity_vectors.items():
            centroid = np.mean(vectors, axis=0).reshape(1, -1)
            faiss.normalize_L2(centroid)
            self._entity_centroids[entity] = centroid[0]

        logger.info(f"Entity centroids computed: {len(self._entity_centroids)} entities")

    def classify_entity_by_embedding(
        self, query_embedding: List[float], min_confidence: float = 0.45
    ) -> Tuple[Optional[str], float]:
        """Classify entity from query embedding using cosine similarity to entity centroids.

        Args:
            query_embedding: Raw query embedding from ada-002 (not yet normalized)
            min_confidence: Minimum cosine similarity to return a classification

        Returns:
            (entity_key, confidence) or (None, best_score) if below threshold
        """
        if not self._entity_centroids:
            return None, 0.0

        # Normalize query embedding
        qv = np.array(query_embedding, dtype=np.float32).reshape(1, -1)
        faiss.normalize_L2(qv)
        qv = qv[0]

        best_entity = None
        best_score = -1.0

        for entity, centroid in self._entity_centroids.items():
            score = float(np.dot(qv, centroid))
            if score > best_score:
                best_score = score
                best_entity = entity

        if best_score >= min_confidence:
            logger.debug(
                f"Embedding entity classifier: -> {best_entity} (score={best_score:.3f})"
            )
            return best_entity, best_score

        return None, best_score

    async def get_query_embedding(self, query: str) -> Optional[List[float]]:
        """Public wrapper for getting query embedding (for reuse across pipeline stages)."""
        from services.concept_mapper import get_concept_mapper
        concept_mapper = get_concept_mapper()
        expanded_query = concept_mapper.expand_query(query)
        return await self._get_query_embedding(expanded_query)

    async def search(
        self,
        query: str,
        top_k: int = 10,
        action_filter: Optional[str] = None,
        entity_filter: Optional[str] = None,
        auto_detect_entity: bool = False,  # Disabled by default - causes accuracy drop
        query_embedding: Optional[List[float]] = None,  # Pre-computed embedding to avoid double API call
    ) -> List[SearchResult]:
        """
        Search for similar tools using FAISS with optional hierarchical filtering.

        Args:
            query: User query text
            top_k: Number of results to return
            action_filter: Optional filter by action (GET/POST/PUT/DELETE)
            entity_filter: Optional filter by entity (companies, vehicles, etc.)
            auto_detect_entity: If True, auto-detect entity from query text
            query_embedding: Optional pre-computed embedding (skips ada-002 call)

        Returns:
            List of SearchResult sorted by similarity (highest first)
        """
        if not self._initialized or self._index is None:
            logger.warning("FAISSVectorStore not initialized, returning empty results")
            return []

        with trace_span(_tracer, "faiss.search", {
            "search.top_k": top_k,
            "query.preview": query[:80],
        }) as span:
            # Auto-detect entity from query if not provided
            detected_entity = None
            if auto_detect_entity and not entity_filter:
                from services.entity_detector import detect_entity
                detected_entity = detect_entity(query)
                if detected_entity:
                    logger.debug(f"Auto-detected entity: {detected_entity}")

            effective_entity_filter = entity_filter or detected_entity

            # Expand query with concept mapper (jargon -> standard terms)
            from services.concept_mapper import get_concept_mapper
            concept_mapper = get_concept_mapper()
            expanded_query = concept_mapper.expand_query(query)

            if expanded_query != query:
                logger.debug(f"ConceptMapper expanded: '{query}' -> '{expanded_query}'")

            # Get query embedding (using expanded query for better matching)
            # Use pre-computed embedding if provided (avoids double API call
            # when entity classification already computed it)
            if query_embedding is None:
                _emb_t0 = time.monotonic()
                query_embedding = await self._get_query_embedding(expanded_query)
                if query_embedding is not None:
                    EMBEDDING_API_DURATION.observe(time.monotonic() - _emb_t0)
            if query_embedding is None:
                return []

            # Convert to numpy and normalize
            query_vector = np.array([query_embedding], dtype=np.float32)
            faiss.normalize_L2(query_vector)

            # Search with larger k if filtering (need more candidates)
            has_filter = action_filter or effective_entity_filter
            search_k = top_k * 5 if has_filter else top_k

            # FAISS search (timed for Prometheus)
            t0 = time.monotonic()
            distances, indices = self._index.search(query_vector, min(search_k, len(self._tool_ids)))
            FAISS_SEARCH_DURATION.observe(time.monotonic() - t0)

            # Build results with filtering.
            # Action filter stays a hard exclude (DELETE query should not return GET tools).
            # Entity filter is a SOFT PENALTY: queries that mention ambiguous nouns
            # (e.g. "organizacija" can mean Companies or OrgUnits) used to have 30% of
            # benchmark misses caused by hard-excluding the correct tool. We keep the
            # entity signal as a ranking boost instead.
            ENTITY_MISMATCH_PENALTY = 1.0
            results = []
            for i, (distance, idx) in enumerate(zip(distances[0], indices[0])):
                if idx < 0:  # FAISS returns -1 for empty slots
                    continue

                tool_id = self._tool_ids[idx]
                score = float(distance)  # Already cosine similarity due to normalization
                method = self._tool_methods.get(tool_id, "GET")
                tool_lower = tool_id.lower()

                # Entity filter: soft penalty for mismatches (keeps candidate in pool).
                if effective_entity_filter:
                    parts = tool_lower.split('_')
                    tool_entity = parts[1] if len(parts) >= 2 else ''
                    entity_stem = effective_entity_filter[:-1] if len(effective_entity_filter) > 3 and effective_entity_filter.endswith('s') else effective_entity_filter
                    entity_match = (
                        effective_entity_filter in tool_entity
                        or tool_entity in effective_entity_filter
                        or tool_entity.startswith(entity_stem)
                    )
                    if not entity_match:
                        score *= ENTITY_MISMATCH_PENALTY

                # Apply action filter if specified (hard exclude — HTTP-verb correctness matters)
                if action_filter:
                    # GET filter: allow GET and search POSTs
                    if action_filter == "GET" and method != "GET":
                        if method == "POST" and any(x in tool_lower for x in ["search", "query", "filter", "list"]):
                            pass  # Allow search POSTs
                        else:
                            continue
                    # POST filter: only POST methods (excluding search POSTs)
                    elif action_filter == "POST" and method != "POST":
                        continue
                    # PUT filter: PUT or PATCH
                    elif action_filter in ("PUT", "PATCH") and method not in ("PUT", "PATCH"):
                        continue
                    # DELETE filter
                    elif action_filter == "DELETE" and method != "DELETE":
                        continue

                results.append(SearchResult(
                    tool_id=tool_id,
                    score=score,
                    method=method
                ))

            # Soft-penalty path may have shuffled the ordering relative to FAISS raw sort.
            # Re-sort by score and truncate to top_k so downstream sees strongest first.
            if effective_entity_filter and results:
                results.sort(key=lambda r: r.score, reverse=True)
            results = results[:top_k]

            # If entity filter was too restrictive and we got no results, retry without it.
            # Pass through cached query_embedding to avoid a second ada-002 round-trip.
            if not results and effective_entity_filter:
                logger.debug("Entity filter too restrictive, retrying without entity filter")
                return await self.search(
                    query=query,
                    top_k=top_k,
                    action_filter=action_filter,
                    entity_filter=None,
                    auto_detect_entity=False,
                    query_embedding=query_embedding,
                )

            span.set_attribute("faiss.result_count", len(results))
            return results

    async def _get_query_embedding(self, query: str) -> Optional[List[float]]:
        """Get embedding for query text. One retry on transient failure — ada-002
        occasionally throws 5xx under load, and a silent [] here kills the entire
        search for the user."""
        from services.openai_client import get_embedding_client

        if self._openai_client is None:
            self._openai_client = get_embedding_client()

        settings = _get_settings()
        deployment = (settings.OPENAI_EMBEDDING_MODEL
                      if settings.OPENAI_EMBEDDING_API_KEY
                      else settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT)
        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                response = await self._openai_client.embeddings.create(
                    input=[query[:8000]], model=deployment,
                )
                return response.data[0].embedding
            except Exception as e:
                last_err = e
                if attempt == 1:
                    await asyncio.sleep(0.2)
        logger.warning(f"Failed to get query embedding after retry: {last_err}")
        return None

    def get_tool_method(self, tool_id: str) -> str:
        """Get HTTP method for a tool."""
        return self._tool_methods.get(tool_id, "GET")

    def is_initialized(self) -> bool:
        """Check if vector store is initialized."""
        return self._initialized

    def get_stats(self) -> Dict:
        """Get statistics about the vector store."""
        return {
            "initialized": self._initialized,
            "total_tools": len(self._tool_ids),
            "total_embeddings": len(self._embeddings),
            "index_size": self._index.ntotal if self._index else 0,
            "cache_file": str(EMBEDDINGS_FILE),
            "cache_exists": EMBEDDINGS_FILE.exists()
        }

# Singleton instance
_faiss_store: Optional[FAISSVectorStore] = None
_singleton_lock = threading.Lock()

def get_faiss_store() -> FAISSVectorStore:
    """Get singleton FAISSVectorStore instance."""
    global _faiss_store
    if _faiss_store is None:
        with _singleton_lock:
            if _faiss_store is None:
                _faiss_store = FAISSVectorStore()
    return _faiss_store

async def initialize_faiss_store(
    tool_documentation: Dict,
    tool_registry_tools: Optional[Dict] = None
) -> FAISSVectorStore:
    """
    Initialize and return the FAISS vector store.

    Call this during application startup.
    """
    store = get_faiss_store()
    await store.initialize(tool_documentation, tool_registry_tools)
    return store
