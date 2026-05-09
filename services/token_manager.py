"""
Token Manager

OAuth2 token management.
DEPENDS ON: config.py, services/errors.py
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from config import get_settings
from services.errors import GatewayError, ErrorCode

logger = logging.getLogger(__name__)
def _get_settings():
    return get_settings()


class TokenManager:
    """
    Thread-safe OAuth2 token manager.

    Features:
    - Automatic refresh before expiry
    - Lock to prevent concurrent refreshes
    - Redis caching for distributed systems (token + expiry stored atomically)
    - Redis double-check inside lock (prevents thundering herd across pods)
    - Failure cooldown to avoid hammering unreachable auth server
    """

    FAILURE_COOLDOWN_SECONDS = 15  # Don't retry for 15s after a failure

    def __init__(self, redis_client=None) -> None:
        """
        Initialize token manager.

        Args:
            redis_client: Optional Redis client for caching
        """
        self._token: Optional[str] = None
        self._expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._refresh_lock = asyncio.Lock()
        self._redis = redis_client
        self._last_failure: Optional[datetime] = None

        # Configuration
        self.auth_url = _get_settings().MOBILITY_AUTH_URL
        self.client_id = _get_settings().MOBILITY_CLIENT_ID
        self.client_secret = _get_settings().MOBILITY_CLIENT_SECRET

        self._cache_key = "mobility:access_token"
        self._http_client: Optional[httpx.AsyncClient] = None

        logger.info(f"TokenManager initialized: {self.auth_url}")

    async def get_token(self) -> str:
        """
        Get valid access token.

        Returns:
            Valid access token

        Raises:
            GatewayError if unable to obtain token
        """
        # Check in-memory cache (with 60s buffer)
        buffer = timedelta(seconds=60)
        if self._token and datetime.now(timezone.utc) < self._expires_at - buffer:
            return self._token

        # Try Redis cache (atomic: token + expiry stored together)
        if await self._try_load_from_redis():
            return self._token

        # Fail fast if auth server was recently unreachable
        if self._last_failure:
            elapsed = (datetime.now(timezone.utc) - self._last_failure).total_seconds()
            if elapsed < self.FAILURE_COOLDOWN_SECONDS:
                raise GatewayError(
                    ErrorCode.TOKEN_REFRESH_FAILED,
                    f"Auth server unreachable (cooldown {self.FAILURE_COOLDOWN_SECONDS - elapsed:.0f}s remaining)"
                )

        # Refresh token (with lock)
        async with self._refresh_lock:
            # Double-check in-memory after lock
            if self._token and datetime.now(timezone.utc) < self._expires_at - buffer:
                return self._token

            # Double-check Redis after lock — another pod may have refreshed
            if await self._try_load_from_redis():
                return self._token

            return await self._fetch_new_token()

    async def _try_load_from_redis(self) -> bool:
        """Try to load token from Redis. Returns True if valid token found.

        Token is stored as JSON {"token": "...", "expires_at": <timestamp>}
        to avoid the GET/TTL race condition (single atomic read).
        """
        if not self._redis:
            return False

        try:
            cached = await self._redis.get(self._cache_key)
            if not cached:
                return False

            cached_str = cached.decode() if isinstance(cached, bytes) else cached
            data = json.loads(cached_str)
            expires_at = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)

            # Only use if not expired (with 60s safety buffer)
            if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
                self._token = data["token"]
                self._expires_at = expires_at
                self._last_failure = None
                logger.debug(f"Token loaded from Redis, expires_at={expires_at.isoformat()}")
                return True

            return False
        except Exception as e:
            logger.warning(f"Redis cache read failed: {e}")
            return False

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create cached HTTP client for token requests.

        Design: client is lazily created and reused for connection pooling.
        If a connection error occurs, httpx handles retries internally.
        The client is closed on shutdown via close().
        """
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=10.0)
            )
        return self._http_client

    async def _fetch_new_token(self) -> str:
        """Fetch new token from auth server."""
        logger.info("Fetching new OAuth2 token...")

        # IMPORTANT: Form-encoded, NOT JSON
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "audience": "none"
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        try:
            client = await self._get_http_client()
            response = await client.post(
                self.auth_url,
                data=payload,
                headers=headers
            )

            if response.status_code != 200:
                error_text = response.text[:500]
                logger.error(f"Token fetch failed: {response.status_code} - {error_text}")
                raise GatewayError(
                    ErrorCode.TOKEN_REFRESH_FAILED,
                    f"Auth failed ({response.status_code}): {error_text}"
                )

            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError) as e:
                self._last_failure = datetime.now(timezone.utc)
                logger.error(f"Auth server returned invalid JSON: {response.text[:200]}")
                raise GatewayError(
                    ErrorCode.TOKEN_REFRESH_FAILED,
                    f"Auth server returned invalid response: {e}"
                )

            access_token = data.get("access_token")
            if not access_token or not isinstance(access_token, str):
                self._last_failure = datetime.now(timezone.utc)
                raise GatewayError(
                    ErrorCode.TOKEN_REFRESH_FAILED,
                    "Auth server response missing 'access_token'"
                )
            self._token = access_token
            expires_in = int(data.get("expires_in", 3600))
            self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            self._last_failure = None  # Clear failure state on success

            logger.info(f"Token acquired, expires in {expires_in}s")

            # Cache in Redis (token + expiry as JSON — atomic read on get_token)
            if self._redis:
                try:
                    cache_ttl = max(expires_in - 120, 60)
                    cache_data = json.dumps({
                        "token": self._token,
                        "expires_at": self._expires_at.timestamp()
                    })
                    await self._redis.setex(self._cache_key, cache_ttl, cache_data)
                    logger.debug(f"Token cached in Redis, TTL={cache_ttl}")
                except Exception as e:
                    logger.warning(f"Redis cache write failed: {e}")

            return self._token

        except httpx.TimeoutException:
            self._last_failure = datetime.now(timezone.utc)
            logger.error("Token fetch timeout")
            raise GatewayError(ErrorCode.TIMEOUT, "Authentication timeout")
        except httpx.RequestError as e:
            self._last_failure = datetime.now(timezone.utc)
            logger.error(f"Token fetch network error: {e}")
            raise GatewayError(
                ErrorCode.TOKEN_REFRESH_FAILED,
                f"Authentication network error: {e}"
            )

    async def invalidate(self) -> None:
        """Invalidate current token (call on 401)."""
        logger.info("Token invalidated")
        self._token = None
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

        # Clear Redis cache synchronously to avoid race with get_token()
        if self._redis:
            await self._clear_redis_cache()

    async def _clear_redis_cache(self) -> None:
        """Clear Redis cache."""
        try:
            await self._redis.delete(self._cache_key)
        except Exception as e:
            logger.debug(f"Failed to clear Redis token cache: {e}")

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    @property
    def is_valid(self) -> bool:
        """Check if current token is valid."""
        if not self._token:
            return False
        return datetime.now(timezone.utc) < self._expires_at - timedelta(seconds=60)
