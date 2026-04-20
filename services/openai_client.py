"""
Shared OpenAI client singleton with circuit breaker.

All services MUST use this to share rate limiting and connection pooling.
Having separate AsyncAzureOpenAI instances per service causes:
- No shared rate limit tracking (each client hits limits independently)
- Wasted connection pools (3x connections to same endpoint)

Added circuit breaker for fail-fast when Azure OpenAI is down.
"""

import logging
import threading
from openai import AsyncAzureOpenAI, AsyncOpenAI

from config import get_settings
from services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)
def _get_settings():
    return get_settings()

_shared_client = None
_shared_embedding_client = None
_shared_reranker_client = None
_circuit_breaker = None
_singleton_lock = threading.Lock()


def get_openai_client() -> AsyncAzureOpenAI:
    """Get shared AsyncAzureOpenAI client.

    SDK retries disabled - each service handles retries explicitly.
    AIOrchestrator: exponential backoff (1-4s)
    UnifiedRouter: fallback to QueryRouter regex
    ChainPlanner: fallback to top-scored tool plan
    """
    global _shared_client
    if _shared_client is None:
        with _singleton_lock:
            if _shared_client is None:
                _shared_client = AsyncAzureOpenAI(
                    azure_endpoint=_get_settings().AZURE_OPENAI_ENDPOINT,
                    api_key=_get_settings().AZURE_OPENAI_API_KEY,
                    api_version=_get_settings().AZURE_OPENAI_API_VERSION,
                    max_retries=0,
                    timeout=15.0  # Reduced from 30s - gpt-4o-mini responds in 1-5s typical
                )
    return _shared_client


def get_embedding_client():
    """Get shared embedding client.

    If OPENAI_EMBEDDING_API_KEY is set, uses direct OpenAI API
    (for text-embedding-3-large). Otherwise falls back to Azure.
    Shared across: faiss_vector_store, intent_classifier.
    """
    global _shared_embedding_client
    if _shared_embedding_client is None:
        with _singleton_lock:
            if _shared_embedding_client is None:
                settings = _get_settings()
                if settings.OPENAI_EMBEDDING_API_KEY:
                    _shared_embedding_client = AsyncOpenAI(
                        api_key=settings.OPENAI_EMBEDDING_API_KEY,
                        max_retries=1,
                        timeout=30.0,
                    )
                    logger.info("Embedding client: direct OpenAI API (text-embedding-3-large)")
                else:
                    _shared_embedding_client = AsyncAzureOpenAI(
                        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                        api_key=settings.AZURE_OPENAI_API_KEY,
                        api_version=settings.AZURE_OPENAI_API_VERSION,
                        max_retries=1,
                        timeout=10.0,
                    )
                    logger.info("Embedding client: Azure OpenAI")
    return _shared_embedding_client


def get_reranker_client():
    """Get shared reranker client.

    If OPENAI_RERANKER_API_KEY is set, uses direct OpenAI API (gpt-4o).
    Otherwise falls back to the shared Azure client (gpt-4o-mini).
    """
    global _shared_reranker_client
    if _shared_reranker_client is None:
        with _singleton_lock:
            if _shared_reranker_client is None:
                settings = _get_settings()
                if settings.OPENAI_RERANKER_API_KEY:
                    _shared_reranker_client = AsyncOpenAI(
                        api_key=settings.OPENAI_RERANKER_API_KEY,
                        max_retries=1,
                        timeout=30.0,
                    )
                    logger.info(f"Reranker client: direct OpenAI API ({settings.OPENAI_RERANKER_MODEL})")
                else:
                    _shared_reranker_client = get_openai_client()
                    logger.info("Reranker client: Azure OpenAI (shared)")
    return _shared_reranker_client


def get_llm_circuit_breaker() -> CircuitBreaker:
    """Get shared circuit breaker for LLM calls.

    After 3 consecutive failures, LLM calls are blocked for 60 seconds.
    This prevents cascading failures when Azure OpenAI is down.
    """
    global _circuit_breaker
    if _circuit_breaker is None:
        with _singleton_lock:
            if _circuit_breaker is None:
                _circuit_breaker = CircuitBreaker()
                logger.info("LLM CircuitBreaker initialized")
    return _circuit_breaker
