"""
One-time script: generate tool embeddings with text-embedding-3-large via direct OpenAI API.

Usage:
    cd nova-verzija
    python scripts/generate_embeddings_3large.py

Reads tool_documentation.json, builds embedding text using the same logic as
FAISSVectorStore._build_embedding_text, calls text-embedding-3-large in batches,
and saves to .cache/tool_embeddings.json in the nested format.
"""
import asyncio
import json
import os
import sys
import re
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import AsyncOpenAI
from pathlib import Path

# --- Config ---
API_KEY = os.environ.get("OPENAI_EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY")
MODEL = "text-embedding-3-large"
EMBED_TEXT_VERSION = "v7.0-embed3large"
BATCH_SIZE = 64  # text-embedding-3-large supports up to 2048 inputs
CACHE_DIR = Path(__file__).parent.parent / ".cache"
EMBEDDINGS_FILE = CACHE_DIR / "tool_embeddings.json"
TOOL_DOC_FILE = Path(__file__).parent.parent / "config" / "tool_documentation.json"

# --- Embedding text builder (mirrors FAISSVectorStore._build_embedding_text) ---

METHOD_VERB_TEXT = {
    "get": "dohvati prikaži pogledaj vrati",
    "post": "dodaj kreiraj napravi unesi",
    "put": "ažuriraj zamijeni izmijeni",
    "patch": "djelomično ažuriraj parcijalno promijeni",
    "delete": "obriši ukloni izbriši makni brisanje",
}

# Copied from faiss_vector_store.py — must stay in sync
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

SUFFIX_STRIP = [
    '_id_documents_documentId_thumb', '_id_documents_documentId_SetAsDefault',
    '_id_documents_documentId', '_id_documents', '_id_metadata',
    '_DeleteByCriteria', '_multipatch', '_SetAsDefault',
    '_GroupBy', '_ProjectTo', '_Agg', '_tree', '_id',
]


def _get_entity_compound_prefix(tool_id: str) -> str:
    name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)
    name_lower = name.lower()
    for suffix in SUFFIX_STRIP:
        if name_lower.endswith(suffix.lower()):
            name = name[:len(name) - len(suffix)]
            break
    name_lower = name.lower()
    for entity_key in sorted(_ENTITY_COMPOUND_PREFIX.keys(), key=len, reverse=True):
        if entity_key in name_lower:
            return _ENTITY_COMPOUND_PREFIX[entity_key]
    return ""


def _extract_entity_key(tool_id: str) -> str:
    name = re.sub(r'^(get|post|put|patch|delete)_', '', tool_id, flags=re.IGNORECASE)
    name_lower = name.lower()
    for entity_key in sorted(_ENTITY_PURPOSE_NOUNS.keys(), key=len, reverse=True):
        if name_lower.startswith(entity_key):
            return entity_key
    parts = tool_id.split("_")
    if len(parts) >= 2:
        return parts[1].lower()
    return ""


def build_embedding_text(tool_id: str, doc: dict) -> str:
    """Mirrors FAISSVectorStore._build_embedding_text exactly."""
    parts = []

    entity_prefix = _get_entity_compound_prefix(tool_id)
    if entity_prefix:
        parts.append(entity_prefix)

    purpose = doc.get("purpose", "")
    entity_key = _extract_entity_key(tool_id)
    entity_nouns = _ENTITY_PURPOSE_NOUNS.get(entity_key, "")
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


async def main():
    if not API_KEY:
        # Try loading from .env file
        env_file = Path(__file__).parent.parent / ".env"
        if env_file.exists():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("OPENAI_EMBEDDING_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        if key:
                            os.environ["OPENAI_EMBEDDING_API_KEY"] = key
                            break

    api_key = os.environ.get("OPENAI_EMBEDDING_API_KEY") or API_KEY
    if not api_key:
        print("ERROR: Set OPENAI_EMBEDDING_API_KEY environment variable")
        sys.exit(1)

    # Load tool documentation
    print(f"Loading tool documentation from {TOOL_DOC_FILE}...")
    with open(TOOL_DOC_FILE, 'r', encoding='utf-8') as f:
        tool_docs = json.load(f)
    print(f"  {len(tool_docs)} tools loaded")

    # Build embedding texts
    batch_items = []
    for tool_id, doc in tool_docs.items():
        text = build_embedding_text(tool_id, doc)
        if not text:
            print(f"  WARNING: No text for {tool_id}, skipping")
            continue
        batch_items.append((tool_id, text[:8000]))

    print(f"  {len(batch_items)} tools to embed")

    # Call OpenAI
    client = AsyncOpenAI(api_key=api_key, max_retries=2, timeout=60.0)
    embeddings = {}
    total_tokens = 0
    t0 = time.time()

    for start in range(0, len(batch_items), BATCH_SIZE):
        chunk = batch_items[start:start + BATCH_SIZE]
        ids = [it[0] for it in chunk]
        texts = [it[1] for it in chunk]

        try:
            response = await client.embeddings.create(input=texts, model=MODEL)
            for item in response.data:
                embeddings[ids[item.index]] = item.embedding
            total_tokens += response.usage.total_tokens
            elapsed = time.time() - t0
            print(f"  [{start + len(chunk)}/{len(batch_items)}] "
                  f"{len(embeddings)} done, {total_tokens} tokens, {elapsed:.1f}s")
        except Exception as e:
            print(f"  ERROR batch {start}: {e}")
            # Retry individually
            for tool_id, text in chunk:
                if tool_id not in embeddings:
                    try:
                        resp = await client.embeddings.create(input=[text], model=MODEL)
                        embeddings[tool_id] = resp.data[0].embedding
                        total_tokens += resp.usage.total_tokens
                    except Exception as e2:
                        print(f"    FAILED {tool_id}: {e2}")

    elapsed = time.time() - t0
    dim = len(next(iter(embeddings.values()))) if embeddings else 0
    print(f"\nDone: {len(embeddings)} embeddings, dim={dim}, "
          f"{total_tokens} tokens, {elapsed:.1f}s")

    # Estimate cost: text-embedding-3-large = $0.13 / 1M tokens
    cost = total_tokens * 0.13 / 1_000_000
    print(f"Estimated cost: ${cost:.4f}")

    # Save
    CACHE_DIR.mkdir(exist_ok=True)
    payload = {
        "embed_text_version": EMBED_TEXT_VERSION,
        "embeddings": embeddings,
    }
    with open(EMBEDDINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    size_mb = os.path.getsize(EMBEDDINGS_FILE) / 1024 / 1024
    print(f"Saved to {EMBEDDINGS_FILE} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    asyncio.run(main())
