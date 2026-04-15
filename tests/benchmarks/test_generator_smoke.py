"""Smoke test: run paraphrase generation on just indices 134, 314, 453."""
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from openai import AzureOpenAI
from tests.benchmarks.generate_paraphrases import (
    build_forbidden_words,
    generate_paraphrases,
    validate_paraphrase,
)

TOOL_DOC_PATH = PROJECT_ROOT / "config" / "tool_documentation.json"

with open(TOOL_DOC_PATH, "r", encoding="utf-8") as f:
    tool_doc = json.load(f)
all_ids = sorted(tool_doc.keys())

client = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
)

for idx in [134, 314, 453]:
    tid = all_ids[idx]
    doc = tool_doc[tid]
    forbidden = build_forbidden_words(tid, doc)

    print(f"\n{'='*70}")
    print(f"Index {idx}: {tid}")
    print(f"Purpose: {doc.get('purpose', '')[:100]}")
    print(f"Synonyms: {doc.get('synonyms_hr', [])[:4]}")
    print(f"Examples: {doc.get('example_queries_hr', [])[:2]}")
    print(f"Forbidden ({len(forbidden)}): {forbidden[:15]}")
    print(f"\nGenerating paraphrases...")

    paraphrases = generate_paraphrases(client, tid, doc, forbidden, n=3, max_retries=3)

    print(f"\nGot {len(paraphrases)} paraphrases:")
    for i, p in enumerate(paraphrases, 1):
        ok, viol = validate_paraphrase(p, forbidden)
        status = "OK" if ok else f"VIOLATES '{viol}'"
        print(f"  {i}. [{status}] {p}")
