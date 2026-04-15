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
from openai import AsyncAzureOpenAI

from config import get_settings
from services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)
def _get_settings():
    return get_settings()

_shared_client = None
_shared_embedding_client = None
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


def get_embedding_client() -> AsyncAzureOpenAI:
    """Get shared AsyncAzureOpenAI client for embedding calls.

    Uses the same endpoint/key as chat but separate instance because
    embedding calls have different timeout/retry characteristics.
    Shared across: faiss_vector_store, intent_classifier.
    """
    global _shared_embedding_client
    if _shared_embedding_client is None:
        with _singleton_lock:
            if _shared_embedding_client is None:
                _shared_embedding_client = AsyncAzureOpenAI(
                    azure_endpoint=_get_settings().AZURE_OPENAI_ENDPOINT,
                    api_key=_get_settings().AZURE_OPENAI_API_KEY,
                    api_version=_get_settings().AZURE_OPENAI_API_VERSION,
                    max_retries=1,
                    timeout=10.0  # Embeddings are fast
                )
    return _shared_embedding_client


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
