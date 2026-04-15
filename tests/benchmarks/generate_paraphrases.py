"""
Generate paraphrased query benchmark for objective tool recognition testing.

Goal: Create 100 tools x 3 paraphrases where each paraphrase is SEMANTICALLY
equivalent to the tool's purpose but LEXICALLY different from all synonyms_hr
and example_queries_hr (avoids the system's lexical shortcuts).

Output: tests/benchmarks/tool_recognition_paraphrases.json

Run ONCE and commit the result. Subsequent benchmark runs use the committed file.
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 stdout on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from openai import AzureOpenAI

# Load .env from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Add project root to path so we can import entity_detector stems
sys.path.insert(0, str(PROJECT_ROOT))

from services.entity_detector import (  # noqa: E402
    CURATED_STEMS,
    ENTITY_STEMS,
    load_auto_stems,
    detect_entity,
)


TOOL_DOC_PATH = PROJECT_ROOT / "config" / "tool_documentation.json"
_DEFAULT_OUT = PROJECT_ROOT / "tests" / "benchmarks" / "tool_recognition_paraphrases.json"
OUT_PATH = Path(os.getenv("PARAPHRASE_OUT", str(_DEFAULT_OUT)))
if not OUT_PATH.is_absolute():
    OUT_PATH = PROJECT_ROOT / OUT_PATH
SEED = int(os.getenv("PARAPHRASE_SEED", "42"))
SAMPLE_SIZE = 100
MUST_INCLUDE_INDICES = [134, 314, 453]
PARAPHRASES_PER_TOOL = 3
MAX_RETRIES_PER_TOOL = 5

# Load auto-generated entity stems from tool_documentation.json so that
# ENTITY_STEMS contains both curated + auto entries (same as pipeline).
# This MUST happen before build_forbidden_words is called.
load_auto_stems()


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics (matches entity_detector._normalize)."""
    mapping = str.maketrans({
        "č": "c", "ć": "c", "đ": "d", "š": "s", "ž": "z",
        "Č": "c", "Ć": "c", "Đ": "d", "Š": "s", "Ž": "z",
    })
    return text.lower().translate(mapping)


def _tokenize(text: str) -> set:
    """Normalize and split on non-word characters."""
    return set(re.findall(r"\w+", _normalize(text)))


def _relevant_entities_for_tool(tool_id: str, doc: dict) -> set:
    """
    Determine entity keys from ENTITY_STEMS that the pipeline would
    associate with this tool. Only CURATED_STEMS entities are considered —
    auto-generated stems are too noisy (they include generic verbs like
    "azuri"/"pregl" that leak across 70+ entities).

    Two signals:
    1. Direct parse: tool_id parts[1].lower() — but ONLY if it's in CURATED_STEMS.
       For compound names (e.g., "latestvehiclecalendar"), find the longest
       CURATED_STEMS key that is a substring of the compound name.
    2. Substring-scan tool's own synonyms_hr + example_queries_hr phrases
       against ALL CURATED_STEMS stems. Any entity whose stem appears in
       any of the tool's phrases is relevant (matches exactly how the
       pipeline's detect_entity would fire on those phrases).
    """
    entities: set = set()

    parts = tool_id.split("_")
    if len(parts) >= 2:
        compound = parts[1].lower()
        # Direct exact match
        if compound in CURATED_STEMS:
            entities.add(compound)
        else:
            # Longest CURATED_STEMS key that is a substring of the compound name
            # e.g. "latestvehiclecalendar" -> "vehiclecalendar"
            best = None
            for key in CURATED_STEMS.keys():
                if key in compound and (best is None or len(key) > len(best)):
                    best = key
            if best:
                entities.add(best)

    # Signal 2: substring scan against ALL CURATED_STEMS stems
    for phrase in doc.get("synonyms_hr", []) + doc.get("example_queries_hr", []):
        norm = _normalize(phrase)
        for entity, stems in CURATED_STEMS.items():
            for stem in stems:
                if _normalize(stem) in norm:
                    entities.add(entity)
                    break

    return entities


def build_forbidden_words(tool_id: str, doc: dict) -> list:
    """
    Build list of forbidden substrings/stems for a tool.

    A paraphrase must NOT contain any of these (as substrings, after normalization)
    so the system can't win via lexical shortcuts.
    """
    forbidden = set()

    # 1. All words from synonyms_hr (split multi-word synonyms)
    for syn in doc.get("synonyms_hr", []):
        for word in _tokenize(syn):
            if len(word) >= 4:
                forbidden.add(word)

    # 2. All words from example_queries_hr
    for eq in doc.get("example_queries_hr", []):
        for word in _tokenize(eq):
            if len(word) >= 4:
                forbidden.add(word)

    # 3. Entity stems from CURATED_STEMS for all relevant entities.
    # Only curated — auto-generated stems are too noisy.
    for entity in _relevant_entities_for_tool(tool_id, doc):
        for stem in CURATED_STEMS.get(entity, []):
            forbidden.add(_normalize(stem).strip())

    # 4. Remove generic Croatian words that carry no tool-specific signal.
    # These leak into forbidden when multi-word synonyms are tokenized
    # (e.g. "želim obrisati dozvole tenanta" → adds "zelim"), but they are
    # generic modal/auxiliary verbs and quantifiers that the pipeline's
    # lexical shortcuts (CURATED_STEMS, exact match index) never match on.
    _GENERIC = {
        # Pronouns, particles, question words, quantifiers
        "sve", "svi", "sva", "jos", "samo", "koji", "koja", "koje",
        "ovaj", "ova", "ovo", "tako", "kako", "kada", "gdje", "zasto",
        "jedan", "jedna", "jedno", "ili", "ako", "jer", "vise", "manje",
        "puno", "malo", "jako", "vrlo", "svoj", "svoje", "svoja", "svih",
        # Modal/auxiliary verbs — "I want / need / must / can / have"
        "zelim", "zelis", "zeli", "zelimo", "zelite", "zele",
        "trebam", "trebas", "treba", "trebamo", "trebate", "trebaju",
        "moram", "moras", "mora", "moramo", "morate", "moraju",
        "mogu", "moze", "mozes", "mozemo", "mozete",
        "imam", "imas", "imamo", "imate", "imaju",
        "biti", "bila", "bilo", "bili", "bile", "budu",
    }
    forbidden = {w for w in forbidden if w not in _GENERIC and len(w) >= 4}

    return sorted(forbidden)


def validate_paraphrase(text: str, forbidden: list) -> tuple[bool, str]:
    """Return (is_valid, violating_word or '')."""
    normalized = _normalize(text)
    for word in forbidden:
        # Substring match (handles Croatian declensions - "vozil" matches "vozilo")
        if word in normalized:
            return False, word
    return True, ""


_HTTP_VERB_ACTIONS_HR = {
    "get": "dohvaćanje/prikaz/pregled",
    "post": "stvaranje/dodavanje",
    "put": "zamjena/potpuni update",
    "patch": "djelomični update",
    "delete": "brisanje/uklanjanje",
    "head": "provjera postojanja",
}

_OP_STOP_WORDS = {"by", "id", "of", "the", "a", "an"}


def parse_operation_id(tool_id: str) -> tuple[str, list[str]]:
    """
    Parse operation_id into (http_action_hr, concept_words).

    Example:
        get_LatestVehicleCalendar -> ("dohvaćanje/prikaz/pregled", ["latest", "vehicle", "calendar"])
        delete_TenantPermissions_id_documents_documentId
            -> ("brisanje/uklanjanje", ["tenant", "permissions", "documents", "document"])
    """
    parts = tool_id.split("_")
    http_verb = parts[0].lower() if parts else "get"
    action_hr = _HTTP_VERB_ACTIONS_HR.get(http_verb, "obrada")

    words = []
    seen = set()
    for part in parts[1:]:  # skip HTTP verb
        # Split CamelCase: "LatestVehicleCalendar" -> ["Latest", "Vehicle", "Calendar"]
        sub = re.findall(r"[A-Z][a-z]*|[a-z]+", part)
        for w in sub:
            wl = w.lower()
            if wl in _OP_STOP_WORDS or len(wl) < 3:
                continue
            if wl not in seen:
                seen.add(wl)
                words.append(wl)
    return action_hr, words


PROMPT_TEMPLATE = """Ti si hrvatski lingvist specijaliziran za parafraziranje korisničkih upita za API endpoint.

CILJ: Napisati {n} različite hrvatske rečenice koje bi stvarni korisnik postavio asistentu ako želi pozvati OVAJ TOČAN endpoint. Rečenice moraju biti SEMANTIČKI JEDNOZNAČNE - iz njih mora biti jasno da korisnik traži UPRAVO ovu operaciju, a ne neku drugu.

ENDPOINT:
  HTTP akcija: {action_hr}
  Koncepti iz naziva endpointa: {concepts}

OPIS IZ DOKUMENTACIJE:
{purpose}

{when_to_use}

ZABRANJENE RIJEČI - NE SMIJEŠ koristiti nijednu, čak ni kao substring nakon uklanjanja dijakritike:
{forbidden}

PRAVILA (po važnosti):

A) JEDNOZNAČNOST (NAJVAŽNIJE): Svaka rečenica MORA sadržavati hrvatske sinonime za SVAKI koncept iz popisa "{concepts}". Ako je koncept "vehicle", rečenica mora SPOMENUTI vozilo/auto (koristeći sinonim koji nije zabranjen, npr. "prijevozno sredstvo"). Ako je koncept "calendar", rečenica mora SPOMENUTI kalendar/raspored koristeći sinonim. NIJE DOPUŠTENO zamijeniti koncept generičkim riječima kao "stavka", "podatak", "sadržaj", "materijal", "informacija", "zapis" - to čini rečenicu nejednoznačnom.

B) SINONIMI (ne doslovni prijevod): Pronađi hrvatske sinonime za svaki koncept koji NISU u zabranjenoj listi. Primjeri:
   - vehicle -> prijevozno sredstvo, motorno sredstvo, limuzina, kombi, dostavno vozilo (ali ne "vozilo" jer sadrži "vozil")
   - document -> isprava, potvrda, spis, akt, zapisnik, obrazac (ako su "dokument"/"datoteka"/"prilog" zabranjeni)
   - tenant -> stanar, iznajmljivač, korisnik sustava (ako su "tenant"/"najmoprimac" zabranjeni)
   - permission -> privilegija, ovlast, autorizacija, pravo korištenja (ako su "dozvol"/"permisij"/"prava" zabranjeni)
   - calendar -> raspored, plan termina, hodogram, agenda, rezervacijski plan (ako je "kalendar" zabranjen)

C) NE smiješ koristiti nijednu zabranjenu riječ (substring match).

D) Rečenica mora zvučati prirodno - kako bi stvarni korisnik pitao asistenta u chatu.

E) Svaka rečenica drugačija od ostalih (različita struktura + različiti sinonimi).

F) Ne objašnjavaj, vrati točno {n} rečenice, po jedna u liniji, bez brojeva, crtica ili komentara.

Hrvatske rečenice (točno {n}, jedna po liniji):"""


def generate_paraphrases(
    client: AzureOpenAI,
    tool_id: str,
    doc: dict,
    forbidden: list,
    n: int = PARAPHRASES_PER_TOOL,
    max_retries: int = MAX_RETRIES_PER_TOOL,
) -> list:
    """Call LLM to generate paraphrases, retrying if any contain forbidden words."""
    purpose = doc.get("purpose", "").strip()
    when_to_use = doc.get("when_to_use", [])
    when_str = ""
    if when_to_use:
        when_str = "DODATNI KONTEKST (kada se koristi):\n" + "\n".join(f"- {w}" for w in when_to_use[:3])

    # Limit forbidden list to avoid prompt bloat (show most important ones)
    forbidden_display = forbidden[:40] if len(forbidden) > 40 else forbidden
    forbidden_str = ", ".join(forbidden_display)

    # Parse operation_id to extract HTTP action + domain concepts
    action_hr, concept_words = parse_operation_id(tool_id)
    concepts_str = ", ".join(concept_words) if concept_words else "(nijedan koncept iz naziva)"

    # Ask for buffer=2 extra so we have margin if some violate
    request_n = n + 2
    prompt = PROMPT_TEMPLATE.format(
        n=request_n,
        action_hr=action_hr,
        concepts=concepts_str,
        purpose=purpose,
        when_to_use=when_str,
        forbidden=forbidden_str,
    )

    clean_paraphrases: list = []  # accumulate across retries

    for attempt in range(max_retries):
        if len(clean_paraphrases) >= n:
            break
        try:
            response = client.chat.completions.create(
                model=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,  # slightly higher for diversity across retries
                max_tokens=500,
            )
            raw = response.choices[0].message.content.strip()
        except Exception as e:
            print(f"  [{tool_id}] LLM error on attempt {attempt+1}: {e}")
            time.sleep(2)
            continue

        # Parse lines, strip bullets/numbers
        lines = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Remove leading bullets, numbers, quotes
            line = re.sub(r"^[-*0-9.)\s\"']+", "", line).strip()
            line = line.rstrip("\"'")
            if len(line) >= 5:
                lines.append(line)

        # Keep every clean line (accumulate across attempts)
        for line in lines:
            if line in clean_paraphrases:
                continue
            ok, violator = validate_paraphrase(line, forbidden)
            if ok:
                clean_paraphrases.append(line)
            else:
                print(f"  [{tool_id}] Attempt {attempt+1}: '{line[:60]}...' contains forbidden '{violator}'")
            if len(clean_paraphrases) >= n:
                break

        if len(clean_paraphrases) < n:
            print(f"  [{tool_id}] After attempt {attempt+1}: {len(clean_paraphrases)}/{n} clean, retry...")

    if len(clean_paraphrases) < n:
        print(f"  [{tool_id}] WARN: Could not generate {n} clean paraphrases after {max_retries} attempts ({len(clean_paraphrases)} clean)")

    return clean_paraphrases[:n]


def select_tools(tool_doc: dict) -> list:
    """Deterministically select 100 tools including indices 134, 314, 453."""
    all_ids = sorted(tool_doc.keys())
    assert len(all_ids) == 950, f"Expected 950 tools, got {len(all_ids)}"

    random.seed(SEED)
    random_indices = set(MUST_INCLUDE_INDICES)
    while len(random_indices) < SAMPLE_SIZE:
        random_indices.add(random.randint(0, 949))

    indices = sorted(random_indices)
    return [(idx, all_ids[idx]) for idx in indices]


def main():
    print(f"Loading {TOOL_DOC_PATH}")
    with open(TOOL_DOC_PATH, "r", encoding="utf-8") as f:
        tool_doc = json.load(f)
    print(f"  Loaded {len(tool_doc)} tools")

    print(f"\nSelecting {SAMPLE_SIZE} tools (seed={SEED}, must_include={MUST_INCLUDE_INDICES})")
    selected = select_tools(tool_doc)
    assert all(idx in [s[0] for s in selected] for idx in MUST_INCLUDE_INDICES), \
        "Must-include indices missing from selection!"

    client = AzureOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )

    result = {
        "seed": SEED,
        "sample_size": SAMPLE_SIZE,
        "paraphrases_per_tool": PARAPHRASES_PER_TOOL,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "must_include_indices": MUST_INCLUDE_INDICES,
        "tools": [],
    }

    failures = 0
    for i, (idx, tool_id) in enumerate(selected, 1):
        doc = tool_doc[tool_id]
        forbidden = build_forbidden_words(tool_id, doc)

        print(f"[{i}/{SAMPLE_SIZE}] {tool_id} (idx={idx}, forbidden={len(forbidden)})")

        paraphrases = generate_paraphrases(client, tool_id, doc, forbidden)

        if len(paraphrases) < PARAPHRASES_PER_TOOL:
            failures += 1

        result["tools"].append({
            "tool_id": tool_id,
            "index": idx,
            "purpose": doc.get("purpose", ""),
            "synonyms_hr": doc.get("synonyms_hr", []),
            "example_queries_hr": doc.get("example_queries_hr", []),
            "forbidden_words": forbidden,
            "paraphrases": paraphrases,
        })

        # Save progress every 10 tools (to a sidecar path — never clobber OUT_PATH mid-run).
        if i % 10 == 0:
            partial = OUT_PATH.with_suffix(OUT_PATH.suffix + ".partial")
            with open(partial, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"  [progress saved: {i}/{SAMPLE_SIZE}]")

    # Final save — atomic rename so OUT_PATH appears only when complete.
    partial = OUT_PATH.with_suffix(OUT_PATH.suffix + ".partial")
    with open(partial, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    os.replace(partial, OUT_PATH)

    print(f"\nDONE. Saved to {OUT_PATH}")
    print(f"Total tools: {len(result['tools'])}")
    total_paraphrases = sum(len(t['paraphrases']) for t in result['tools'])
    print(f"Total paraphrases: {total_paraphrases}")
    print(f"Tools with < {PARAPHRASES_PER_TOOL} paraphrases: {failures}")


if __name__ == "__main__":
    main()
