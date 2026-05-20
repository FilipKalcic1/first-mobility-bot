"""
Shared Azure OpenAI client singleton with circuit breaker.

Azure-only by employer policy. Direct-OpenAI escape hatches removed in
12.3.0 — every client returned here is Azure-backed. Three accessors
(chat/embedding/reranker) keep the surface intact for callers but all
three resolve to AzureOpenAI under the hood.

All services MUST use this to share rate limiting and connection pooling.

Concurrency note: the chat/reranker accessors return a transparent proxy
that wraps every `chat.completions.create` call in `AzureRateGuard`.
Enforcement is by construction — there is no untracked path to Azure
chat completions through this module. Embeddings are unguarded
(separate Azure deployment, cheap, near-infinite quota).
"""

import logging
import os
import threading
from openai import AsyncAzureOpenAI

from config import get_settings
from services.v2.azure_rate_guard import AzureRateGuard

logger = logging.getLogger(__name__)
def _get_settings():
    return get_settings()

_shared_client = None
_shared_embedding_client = None
_rate_guard = None
_rate_guard_timeout_s = 30.0
_singleton_lock = threading.Lock()


def _max_concurrent_from_env() -> int:
    """Resolve AZURE_LLM_MAX_CONCURRENT.

    Audit fix: read raw env first so runtime env overrides work (the
    Settings singleton is cached at first import — useful for boot-time
    configuration but doesn't reflect later os.environ mutations). Hard
    floor 1. The Settings field exists in config.py for documentation,
    .env.example generation, and deploy-time validation.
    """
    raw = os.environ.get("AZURE_LLM_MAX_CONCURRENT")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            return 20
    # Fall back to Settings (validated default 20)
    try:
        return max(1, int(_get_settings().AZURE_LLM_MAX_CONCURRENT))
    except (AttributeError, ValueError, TypeError):
        return 20


def get_azure_rate_guard() -> AzureRateGuard:
    """Process-wide concurrency guard for Azure chat-completions calls.

    Configured via env `AZURE_LLM_MAX_CONCURRENT` (default 20). One
    semaphore per process — sufficient for single-pod deployment;
    fleet-wide caps require deployment-level Azure config.
    """
    global _rate_guard
    if _rate_guard is None:
        with _singleton_lock:
            if _rate_guard is None:
                _rate_guard = AzureRateGuard(max_concurrent=_max_concurrent_from_env())
                logger.info(
                    "AzureRateGuard initialized max_concurrent=%d",
                    _rate_guard._max,
                )
    return _rate_guard


class _GuardedCompletions:
    """Wraps `client.chat.completions` so every `.create(...)` call
    acquires a slot on the process-wide rate guard before hitting Azure."""

    def __init__(self, inner, guard: AzureRateGuard, timeout_s: float):
        self._inner = inner
        self._guard = guard
        self._timeout_s = timeout_s

    async def create(self, *args, **kwargs):
        async with self._guard.acquire(timeout_s=self._timeout_s):
            return await self._inner.create(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _GuardedChat:
    def __init__(self, inner, guard: AzureRateGuard, timeout_s: float):
        self._inner = inner
        self.completions = _GuardedCompletions(
            inner.completions, guard, timeout_s,
        )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class GuardedAzureClient:
    """Transparent proxy over AsyncAzureOpenAI that gates chat completions
    through AzureRateGuard. All other attributes (embeddings, models, ...)
    fall through unchanged — only chat is throttled."""

    def __init__(
        self,
        inner: AsyncAzureOpenAI,
        guard: AzureRateGuard,
        timeout_s: float = _rate_guard_timeout_s,
    ):
        self._inner = inner
        self._guard = guard
        self.chat = _GuardedChat(inner.chat, guard, timeout_s)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def get_openai_client():
    """Get shared chat client (Azure, rate-guarded).

    Returns a `GuardedAzureClient` proxy: every `chat.completions.create`
    call acquires a slot on `AzureRateGuard` before hitting Azure. Other
    attributes (models, files, ...) pass through to the underlying
    AsyncAzureOpenAI unchanged.

    SDK retries disabled — each service handles retries explicitly.
    AIOrchestrator: exponential backoff (1-4s)
    UnifiedRouter: fallback to QueryRouter regex
    ChainPlanner: fallback to top-scored tool plan
    """
    global _shared_client
    if _shared_client is None:
        with _singleton_lock:
            if _shared_client is None:
                inner = AsyncAzureOpenAI(
                    azure_endpoint=_get_settings().AZURE_OPENAI_ENDPOINT,
                    api_key=_get_settings().AZURE_OPENAI_API_KEY,
                    api_version=_get_settings().AZURE_OPENAI_API_VERSION,
                    max_retries=0,
                    timeout=15.0  # Reduced from 30s - gpt-4o-mini responds in 1-5s typical
                )
                _shared_client = GuardedAzureClient(inner, get_azure_rate_guard())
    return _shared_client


def get_embedding_client() -> AsyncAzureOpenAI:
    """Get shared Azure embedding client.

    Always Azure (employer policy). Deployment selected by
    `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (default ada-002). To swap to a
    different Azure embedding deployment, change that env var — not by
    introducing a direct-OpenAI bypass.

    Shared across: faiss_vector_store and any future embedding consumer.
    """
    global _shared_embedding_client
    if _shared_embedding_client is None:
        with _singleton_lock:
            if _shared_embedding_client is None:
                settings = _get_settings()
                _shared_embedding_client = AsyncAzureOpenAI(
                    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                    api_key=settings.AZURE_OPENAI_API_KEY,
                    api_version=settings.AZURE_OPENAI_API_VERSION,
                    max_retries=1,
                    timeout=10.0,
                )
                logger.info(
                    f"Embedding client: Azure OpenAI "
                    f"(deployment={settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT})"
                )
    return _shared_embedding_client



