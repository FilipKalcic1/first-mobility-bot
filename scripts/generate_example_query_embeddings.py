"""
Generate Example Query Embeddings (ada-002).

Embeds all ~7600 example_queries_hr from tool_documentation.json
using Azure ada-002 and saves to .cache/example_query_embeddings_ada002.json.

Format:
{
  "queries": ["query1", "query2", ...],
  "tool_ids": ["get_Vehicles", "get_Vehicles", ...],
  "embeddings": [[0.1, 0.2, ...], [0.3, 0.4, ...], ...],
  "model": "text-embedding-ada-002",
  "dim": 1536,
  "count": 7600
}

Usage:
    python scripts/generate_example_query_embeddings.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv()

from openai import AsyncAzureOpenAI
from config import get_settings

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_DIR = project_root / "config"
CACHE_DIR = project_root / ".cache"
TOOL_DOC_FILE = CONFIG_DIR / "tool_documentation.json"
OUTPUT_FILE = CACHE_DIR / "example_query_embeddings_ada002.json"

EMBEDDING_DIM = 1536
BATCH_SIZE = 100  # Azure ada-002 supports up to 2048 inputs per request


async def main():
    # Load tool documentation
    with open(TOOL_DOC_FILE, 'r', encoding='utf-8') as f:
        docs = json.load(f)

    # Collect all example queries with their parent tool_id
    queries = []
    tool_ids = []
    for tid, doc in docs.items():
        for q in doc.get("example_queries_hr", []):
            queries.append(q.strip())
            tool_ids.append(tid)

    logger.info(f"Collected {len(queries)} example queries from {len(docs)} tools")

    # Check for cached result
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            cached = json.load(f)
        if cached.get("count") == len(queries) and cached.get("dim") == EMBEDDING_DIM:
            logger.info(f"Cache hit: {OUTPUT_FILE} already has {cached['count']} embeddings")
            return

    # Initialize Azure client
    settings = get_settings()
    client = AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    )
    deployment = settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT
    logger.info(f"Using deployment: {deployment}")

    # Embed in batches
    all_embeddings = []
    total_batches = (len(queries) + BATCH_SIZE - 1) // BATCH_SIZE
    t0 = time.time()

    for batch_idx in range(total_batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(queries))
        batch = queries[start:end]

        for attempt in range(3):
            try:
                response = await client.embeddings.create(
                    input=batch,
                    model=deployment,
                )
                batch_embeddings = [d.embedding for d in response.data]
                all_embeddings.extend(batch_embeddings)

                elapsed = time.time() - t0
                rate = (start + len(batch)) / elapsed if elapsed > 0 else 0
                logger.info(
                    f"Batch {batch_idx+1}/{total_batches}: "
                    f"{start+len(batch)}/{len(queries)} "
                    f"({rate:.0f} queries/s)"
                )
                break
            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Batch {batch_idx+1} attempt {attempt+1} failed: {e}, retrying...")
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(f"Batch {batch_idx+1} failed after 3 attempts: {e}")
                    raise

        # Small delay to avoid rate limiting
        if batch_idx < total_batches - 1:
            await asyncio.sleep(0.1)

    # Validate dimensions
    for i, emb in enumerate(all_embeddings):
        if len(emb) != EMBEDDING_DIM:
            raise ValueError(f"Embedding {i} has dim {len(emb)}, expected {EMBEDDING_DIM}")

    # Save
    CACHE_DIR.mkdir(exist_ok=True)
    output = {
        "queries": queries,
        "tool_ids": tool_ids,
        "embeddings": all_embeddings,
        "model": "text-embedding-ada-002",
        "dim": EMBEDDING_DIM,
        "count": len(queries),
    }
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f)

    elapsed = time.time() - t0
    logger.info(f"Done! {len(all_embeddings)} embeddings saved to {OUTPUT_FILE}")
    logger.info(f"Total time: {elapsed:.1f}s ({len(queries)/elapsed:.0f} queries/s)")
    logger.info(f"File size: {OUTPUT_FILE.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())
