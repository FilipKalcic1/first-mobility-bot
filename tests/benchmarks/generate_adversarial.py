"""
Adversarial benchmark generator — "torturer" mode.

Generates N extremely hard, ambiguous, sarcastic, and edge-case Croatian
queries per tool. Unlike generate_paraphrases.py (which builds clean
semantically-equivalent paraphrases), this targets the 5% failure modes:

  - Ambiguity between sibling tools (latest vs list, PUT vs PATCH, singular vs multipatch)
  - Sarcasm / negation that flips action intent
  - Heavy typos, dialect, colloquial mashups
  - Pronoun-only references requiring conversation context
  - Telegraphic / over-verbose / distracted phrasings
  - HTTP-verb confusion ("potpuno zamijeni" vs "malo popravi")
  - Generic nouns where a specific entity is expected

Usage:
    python tests/benchmarks/generate_adversarial.py           # seed=42, 20/tool
    ADVERSARIAL_SEED=1337 python .../generate_adversarial.py   # held-out seed
    ADVERSARIAL_PER_TOOL=5 python .../generate_adversarial.py  # smoke run

Output: tests/benchmarks/tool_recognition_adversarial.json
        tests/benchmarks/tool_recognition_adversarial.seed1337.json
"""
import json
import os
import random
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
from openai import AzureOpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

TOOL_DOC_PATH = PROJECT_ROOT / "config" / "tool_documentation.json"
SEED = int(os.getenv("ADVERSARIAL_SEED", "42"))
PER_TOOL = int(os.getenv("ADVERSARIAL_PER_TOOL", "20"))
MAX_RETRIES = 3
_OUT_DEFAULT = "tool_recognition_adversarial.json" if SEED == 42 else f"tool_recognition_adversarial.seed{SEED}.json"
OUT_PATH = PROJECT_ROOT / "tests" / "benchmarks" / os.getenv("ADVERSARIAL_OUT", _OUT_DEFAULT)


PROMPT = """Ti si "mučitelj" — hrvatski lingvist koji piše NAJGORE moguće upite za RAG/FAISS sustav.
Tvoj cilj je napisati {n} upita koji ciljano PADAJU sustav koji pokušava odabrati točan API alat.

CILJNI ALAT (korisnik ipak želi UPRAVO ovaj):
  tool_id: {tool_id}
  HTTP metoda: {method}
  Svrha: {purpose}

Napiši {n} različitih hrvatskih upita. Svaki upit mora UISTINU ciljati ovaj alat (čovjek-anotator bi potvrdio), ali mora biti:

ADVERZARIJALNI STILOVI (koristi MIX, ne sve iste):
  1. DVOSMISLEN prema susjednom alatu (npr. ako je "latest", upit zvuči kao "lista")
  2. SARKASTIČAN / NEGACIJA ("baš me briga što ovo radi, samo mi reci...")
  3. TIPOGRAFSKE GREŠKE, dijalekt, ž→z, č→c, razmaci, lowercase
  4. TELEGRAFSKI (3 riječi, bez glagola)
  5. PRETRPAN kontekstom koji distraira ("jučer sam pio kavu i... uzgred...")
  6. ZAMJENICE umjesto imenica ("ono što sam prije gledao, napravi to opet")
  7. HTTP METODA zbunjujuća ("možda promijeni, ili ne, ok promijeni" — za PATCH)
  8. GENERIČKE IMENICE ("ovo", "ta stvar", "taj podatak") gdje treba entitet
  9. KOLOKVIJALNO / ŽARGON koji nije u sinonimima
 10. VIŠESTRUKI JEZIK u upitu (HR+EN miks)

PRAVILA:
  A) Svaki upit MORA semantički ciljati TOČNO {tool_id}, ne susjedni alat.
  B) Ne koristi riječi iz sljedeće liste (preočito): {synonyms}
  C) Različiti upiti — različite strategije, ne 20 varijanti iste stvari.
  D) Prirodan hrvatski govor, kakav stvarno čuješ u chatu/WhatsAppu.
  E) Ne objašnjavaj. Vrati TOČNO {n} redova, po jedan upit, bez brojeva.

Upiti (točno {n}, jedan po liniji):"""


def generate(client: AzureOpenAI, tool_id: str, doc: dict) -> list:
    synonyms = ", ".join(doc.get("synonyms_hr", [])[:8]) or "(nema)"
    prompt = PROMPT.format(
        n=PER_TOOL,
        tool_id=tool_id,
        method=tool_id.split("_", 1)[0].upper(),
        purpose=doc.get("purpose", "").strip()[:300],
        synonyms=synonyms,
    )

    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": "Ti si adversarijalni lingvist. Odgovaraš samo traženim rečenicama."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
                max_tokens=900,
                seed=SEED + hash(tool_id) % 100000,
            )
            text = resp.choices[0].message.content or ""
            lines = [l.strip().lstrip("-*0123456789.) ").strip() for l in text.splitlines() if l.strip()]
            lines = [l for l in lines if len(l) >= 4]
            if len(lines) >= PER_TOOL:
                return lines[:PER_TOOL]
        except Exception as e:
            print(f"  attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    return []


def main():
    random.seed(SEED)
    with TOOL_DOC_PATH.open("r", encoding="utf-8") as f:
        docs = json.load(f)

    tool_ids = sorted(docs.keys())
    sample_size = int(os.getenv("ADVERSARIAL_SAMPLE", str(len(tool_ids))))
    if sample_size < len(tool_ids):
        tool_ids = random.sample(tool_ids, sample_size)

    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    )

    out = {}
    for i, tid in enumerate(tool_ids, 1):
        print(f"[{i}/{len(tool_ids)}] {tid}", flush=True)
        queries = generate(client, tid, docs[tid])
        if queries:
            out[tid] = queries
        else:
            print(f"  SKIPPED (no queries produced)", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(
            {"seed": SEED, "per_tool": PER_TOOL, "tools": out},
            f, ensure_ascii=False, indent=2,
        )
    print(f"Wrote {OUT_PATH} ({len(out)} tools, {sum(len(v) for v in out.values())} queries)")


if __name__ == "__main__":
    main()
