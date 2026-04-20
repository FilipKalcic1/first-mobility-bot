"""
HyDE (Hypothetical Document Embeddings) fallback for low-confidence FAISS retrieval.

Triggered only when base FAISS search returns top-1 score below HYDE_THRESHOLD.
The LLM generates a hypothetical technical description that mirrors the indexed
embed_text format: "{glagol} {entiteti} — {svrha}. Sinonimi: ... Kada: ...".
The original query is concatenated ("{query} | {hyde}") so user-specific tokens
(IDs, names) are preserved in the embedding alongside the enriched description.

Cache: sha256(normalized_query) in Redis, 7-day TTL. Tool docs don't change often.
Fail-open: any error returns None so caller falls back to the original query.
"""

import hashlib
import logging
import os
from typing import Optional

from prometheus_client import Counter as PromCounter, Histogram as PromHistogram

from services.text_normalizer import normalize_query

logger = logging.getLogger(__name__)

HYDE_TRIGGER_TOTAL = PromCounter(
    "hyde_trigger_total",
    "HyDE fallback invocations, by outcome",
    ["outcome"],  # triggered | skipped_disabled | skipped_short | skipped_confident
)

HYDE_CACHE_TOTAL = PromCounter(
    "hyde_cache_total",
    "HyDE Redis cache lookups",
    ["result"],  # hit | miss | unavailable
)

HYDE_SCORE_UPLIFT = PromHistogram(
    "hyde_score_uplift",
    "Delta between HyDE top-1 score and baseline FAISS top-1 score",
    buckets=[-0.2, -0.05, 0.0, 0.02, 0.05, 0.1, 0.2, 0.4],
)

HYDE_LATENCY = PromHistogram(
    "hyde_generate_latency_seconds",
    "Wall time spent in HyDE LLM generation (cache-miss path)",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)

HYDE_ENABLED = os.environ.get("HYDE_ENABLED", "true").lower() == "true"
HYDE_THRESHOLD = float(os.environ.get("HYDE_THRESHOLD", "0.72"))
HYDE_CACHE_TTL = int(os.environ.get("HYDE_CACHE_TTL", str(7 * 86400)))
HYDE_MAX_TOKENS = int(os.environ.get("HYDE_MAX_TOKENS", "180"))
HYDE_MIN_QUERY_LEN = int(os.environ.get("HYDE_MIN_QUERY_LEN", "4"))

HYDE_SYSTEM_PROMPT = """Vi ste tehnički asistent za Mobility One API dokumentaciju.
Vaš zadatak: NEMOJTE odgovarati korisniku. Umjesto toga, napišite HIPOTETSKI tehnički opis idealnog API alata koji bi riješio korisnički upit.

OPIS MORA BITI NA HRVATSKOM JEZIKU I PRATITI OVU STRIKTNU STRUKTURU:
[Glagol radnje] [Entiteti/Koncepti] — [Sažeta svrha operacije]. Sinonimi: [Tehnički sinonimi]. Kada: [Kontekst upotrebe].

Pravila:
1. Koristite stručne termine (npr. 'evidencija', 'agregacija', 'povijest', 'status').
2. Nemojte doslovno ponavljati riječi iz upita ako postoje bolji tehnički sinonimi.
3. Fokusirajte se na sistemsku logiku (npr. 'dohvaćanje podataka' umjesto 'vidjeti').
4. Budite kratki i precizni (max 150 tokena).

Primjer:
Upit: "koji auti su slobodni sutra ujutro?"
Output: provjera dostupnosti vozila — Vraća listu aktivnih resursa filtriranih prema vremenskom slotu i statusu rezervacije. Sinonimi: slobodni kapaciteti, inventar, status zauzetosti. Kada: kod provjere slobodnih termina za najam ili dodjelu."""


def _cache_key(query: str) -> str:
    normalized = normalize_query(query).strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"hyde:{digest}"


def should_trigger(top_score: Optional[float], query: str) -> bool:
    """Decide whether HyDE fallback is worth running for this query."""
    if not HYDE_ENABLED:
        HYDE_TRIGGER_TOTAL.labels(outcome="skipped_disabled").inc()
        return False
    if not query or len(query.strip()) < HYDE_MIN_QUERY_LEN:
        HYDE_TRIGGER_TOTAL.labels(outcome="skipped_short").inc()
        return False  # Skip greetings and single-word navigation
    if top_score is None:
        HYDE_TRIGGER_TOTAL.labels(outcome="triggered").inc()
        return True  # No FAISS signal — HyDE can only help
    if top_score < HYDE_THRESHOLD:
        HYDE_TRIGGER_TOTAL.labels(outcome="triggered").inc()
        return True
    HYDE_TRIGGER_TOTAL.labels(outcome="skipped_confident").inc()
    return False


async def generate_hypothetical_doc(
    query: str,
    openai_client,
    redis_client=None,
    cost_tracker=None,
    deployment_name: Optional[str] = None,
) -> Optional[str]:
    """
    Produce a hypothetical tool description for `query`.

    Returns the HyDE text (not concatenated) or None on failure. Callers concat
    with the original query before embedding so user-specific tokens survive.
    """
    if not query or not openai_client:
        return None

    cache_key = _cache_key(query)

    if redis_client is not None:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                text = cached.decode("utf-8") if isinstance(cached, bytes) else cached
                HYDE_CACHE_TOTAL.labels(result="hit").inc()
                logger.debug(f"HyDE cache hit: {cache_key}")
                return text
            HYDE_CACHE_TOTAL.labels(result="miss").inc()
        except Exception as e:
            HYDE_CACHE_TOTAL.labels(result="unavailable").inc()
            logger.debug(f"HyDE cache read failed: {e}")
    else:
        HYDE_CACHE_TOTAL.labels(result="unavailable").inc()

    try:
        import time
        from config import get_settings
        model = deployment_name or get_settings().AZURE_OPENAI_DEPLOYMENT_NAME

        _t0 = time.perf_counter()
        response = await openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": HYDE_SYSTEM_PROMPT},
                {"role": "user", "content": f"Upit: {query}"},
            ],
            temperature=0,
            max_tokens=HYDE_MAX_TOKENS,
        )
        HYDE_LATENCY.observe(time.perf_counter() - _t0)

        if cost_tracker is not None and getattr(response, "usage", None):
            try:
                await cost_tracker.record_usage(
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                )
            except Exception as e:
                logger.debug(f"HyDE cost tracking failed: {e}")

        content = (response.choices[0].message.content or "").strip()
        if not content:
            return None

        if redis_client is not None:
            try:
                await redis_client.setex(cache_key, HYDE_CACHE_TTL, content)
            except Exception as e:
                logger.debug(f"HyDE cache write failed: {e}")

        logger.info(f"HyDE generated ({len(content)} chars) for query={query[:60]!r}")
        return content

    except Exception as e:
        logger.warning(f"HyDE generation failed, falling back to raw query: {e}")
        return None


def build_expanded_query(query: str, hyde_text: Optional[str]) -> str:
    """Concat original query with HyDE text using ' | ' separator."""
    if not hyde_text:
        return query
    return f"{query} | {hyde_text}"
