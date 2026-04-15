"""
API Gateway

Enterprise HTTP client for MobilityOne API.
DEPENDS ON: token_manager.py, config.py
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Any, Optional
from urllib.parse import quote, urlparse

import httpx

from config import get_settings
from services.errors import (
    ErrorCode, GatewayError as StructuredGatewayError,
    HTTP_STATUS_TO_ERROR_CODE, CircuitOpenError,
)
from services.retry_utils import calculate_backoff
from services.token_manager import TokenManager
from services.tracing import get_tracer, trace_span

logger = logging.getLogger(__name__)
_tracer = get_tracer("api_gateway")


def _get_settings():
    """Lazy settings accessor to avoid module-level initialization."""
    return get_settings()

class HttpMethod(Enum):
    """HTTP methods."""
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"

@dataclass
class APIResponse:
    """Structured API response."""
    success: bool
    status_code: int
    data: Any
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    headers: Optional[Dict[str, str]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        if self.success:
            return {
                "success": True,
                "data": self.data,
                "status_code": self.status_code
            }
        return {
            "success": False,
            "error": self.error_message,
            "error_code": self.error_code,
            "status_code": self.status_code
        }

class APIGateway:
    """
    Enterprise API Gateway.

    Features:
    - Automatic authentication
    - Retry with exponential backoff
    - Tenant header management
    - Connection pooling
    """

    DEFAULT_MAX_RETRIES = 2
    DEFAULT_TIMEOUT = 15.0
    RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504}

    # Circuit breaker: after N consecutive failures, skip API calls for COOLDOWN seconds.
    # Uses jittered exponential backoff to prevent thundering herd on recovery.
    CIRCUIT_FAILURE_THRESHOLD = 3
    CIRCUIT_BASE_COOLDOWN_SECONDS = 15
    CIRCUIT_MAX_COOLDOWN_SECONDS = 120

    def __init__(
        self,
        base_url: Optional[str] = None,
        tenant_id: Optional[str] = None,
        redis_client=None
    ):
        """
        Initialize API Gateway.

        Args:
            base_url: Base URL (defaults to settings)
            tenant_id: Tenant ID (defaults to settings)
            redis_client: Redis client for token caching
        """
        self.base_url = (base_url or _get_settings().MOBILITY_API_URL).rstrip("/")
        self.tenant_id = tenant_id or _get_settings().tenant_id

        self.token_manager = TokenManager(redis_client)

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.DEFAULT_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            follow_redirects=True
        )

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        logger.info(f"APIGateway initialized: {self.base_url}")

    async def execute(
        self,
        method: HttpMethod,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        tenant_id: Optional[str] = None,
        max_retries: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> APIResponse:
        """
        Execute HTTP request.

        Args:
            method: HTTP method
            path: API path
            params: Query parameters
            body: Request body
            headers: Additional headers
            tenant_id: Override tenant ID (explicit override, highest priority)
            max_retries: Override retry count
            context: Session context. If it carries "TenantId" and neither
                `tenant_id` nor `headers['x-tenant']` are set, the context value
                wins over the env-var default.

        Returns:
            APIResponse
        """
        with trace_span(_tracer, "api_gateway.execute", {
            "http.method": method.value,
            "http.path": path,
        }):
            return await self._execute_inner(method, path, params, body, headers, tenant_id, max_retries, context)

    async def _execute_inner(
        self,
        method: HttpMethod,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        tenant_id: Optional[str] = None,
        max_retries: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> APIResponse:
        """Inner implementation of execute, wrapped by tracing."""
        # Ensure params is a dict
        if params is None:
            params = {}

        # For GET requests, set a sensible default Rows limit if not specified
        # Using 100 to reduce chance of missing data; response formatter
        # should indicate if more results exist
        if method == HttpMethod.GET:
            # Case-insensitive check for 'Rows'
            if not any(k.lower() == 'rows' for k in params):
                params['Rows'] = 100
                logger.debug("Default 'Rows=100' added for GET request.")

        url = self._build_url(path, params)
        ctx_tenant = None
        if context:
            ctx_tenant = context.get("TenantId") or context.get("tenant_id")
        # STRICT TENANT ISOLATION (V6.2):
        # The .env MOBILITY_TENANT_ID is the dev-test tenant only. Falling back
        # to it for an arbitrary user would route their data into the wrong
        # tenant (cross-tenant write). In production, every call MUST carry an
        # explicit tenant sourced from the user's authenticated session (the
        # value get_Persons wrote into context). If neither an explicit
        # `tenant_id` override nor a session `context.TenantId` is present,
        # we refuse the call.
        effective_tenant = tenant_id or ctx_tenant
        if not effective_tenant:
            err = StructuredGatewayError(
                ErrorCode.MISSING_TENANT_CONTEXT,
                "Missing Tenant Context: refusing call without session TenantId.",
                metadata={"path": path, "method": method.value},
            )
            logger.error(f"{err}")
            return APIResponse(
                success=False,
                status_code=0,
                data=None,
                error_message=(
                    "Missing Tenant Context: session has no TenantId. "
                    "Bootstrap (get_Persons) must populate it before any "
                    "tenant-scoped call."
                ),
                error_code=ErrorCode.MISSING_TENANT_CONTEXT.value,
            )
        retries = max_retries if max_retries is not None else self.DEFAULT_MAX_RETRIES

        # Circuit breaker: fail fast if API has been consistently failing.
        # Half-open: allow one probe request when cooldown expires to test recovery.
        now = time.monotonic()
        if self._consecutive_failures >= self.CIRCUIT_FAILURE_THRESHOLD and now < self._circuit_open_until:
            remaining = int(self._circuit_open_until - now)
            err = CircuitOpenError(
                path,
                cooldown_seconds=float(remaining),
                metadata={"consecutive_failures": self._consecutive_failures},
            )
            logger.warning(f"{err}")
            return APIResponse(
                success=False,
                status_code=0,
                data=None,
                error_message=f"Circuit breaker open: API unavailable (retry in {remaining}s)",
                error_code=ErrorCode.CIRCUIT_OPEN.value,
            )

        last_error = None

        for attempt in range(retries + 1):
            try:
                # Get token
                token = await self.token_manager.get_token()

                # Build headers
                request_headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "text/plain, application/json",
                }

                # CRITICAL: x-tenant header
                if effective_tenant:
                    request_headers["x-tenant"] = effective_tenant

                if headers:
                    request_headers.update(headers)

                logger.debug(f"API: {method.value} {url} params={params}")

                # Execute
                response = await self._do_request(method, url, request_headers, body)

                # Handle 401 - refresh token immediately (no delay)
                if response.status_code == 401 and attempt < retries:
                    err = StructuredGatewayError(
                        ErrorCode.TOKEN_REFRESH_FAILED,
                        "401 - Refreshing token",
                        status_code=401,
                        metadata={"url": url, "attempt": attempt + 1},
                    )
                    logger.warning(f"{err}")
                    await self.token_manager.invalidate()
                    continue

                # Handle retryable errors
                if response.status_code in self.RETRY_STATUS_CODES and attempt < retries:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(f"Retryable error {response.status_code}, delay={delay}s")
                    await asyncio.sleep(delay)
                    continue

                # Reset circuit breaker on non-server-error responses
                # 4xx are client errors (not API failures), so don't count them
                if response.status_code < 500:
                    self._consecutive_failures = 0
                return self._parse_response(response)

            except httpx.TimeoutException as e:
                last_error = f"Timeout: {e}"
                err = StructuredGatewayError(
                    ErrorCode.TIMEOUT,
                    f"API timeout on attempt {attempt + 1}/{retries + 1}",
                    metadata={"url": url, "attempt": attempt + 1},
                    cause=e,
                )
                logger.warning(f"{err}")
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.CIRCUIT_FAILURE_THRESHOLD:
                    cooldown = self._calculate_circuit_cooldown()
                    self._circuit_open_until = time.monotonic() + cooldown
                    logger.warning(f"Circuit OPENED: {self._consecutive_failures} consecutive failures, cooldown {cooldown:.0f}s")
                if attempt < retries:
                    await asyncio.sleep(self._calculate_backoff(attempt))
                    continue

            except httpx.RequestError as e:
                last_error = f"Network error: {e}"
                err = StructuredGatewayError(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    f"API network error on attempt {attempt + 1}/{retries + 1}",
                    metadata={"url": url, "attempt": attempt + 1},
                    cause=e,
                )
                logger.warning(f"{err}")
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.CIRCUIT_FAILURE_THRESHOLD:
                    cooldown = self._calculate_circuit_cooldown()
                    self._circuit_open_until = time.monotonic() + cooldown
                    logger.warning(f"Circuit OPENED: {self._consecutive_failures} consecutive failures, cooldown {cooldown:.0f}s")
                if attempt < retries:
                    await asyncio.sleep(self._calculate_backoff(attempt))
                    continue

            except Exception as e:
                last_error = f"Error: {e}"
                err = StructuredGatewayError(
                    ErrorCode.SERVER_ERROR,
                    f"API call error: {e}",
                    metadata={"url": url, "attempt": attempt + 1},
                    cause=e,
                )
                logger.error(f"{err}")
                if attempt < retries:
                    await asyncio.sleep(self._calculate_backoff(attempt))
                    continue

        err = StructuredGatewayError(
            ErrorCode.RETRY_EXHAUSTED,
            f"All {retries} retries exhausted" if retries > 0 else "Request failed (no retries configured)",
            metadata={"url": url, "retries": retries, "last_error": last_error},
        )
        if retries > 0:
            logger.error(f"{err}")
        else:
            logger.warning(f"{err}")
        return APIResponse(
            success=False,
            status_code=0,
            data=None,
            error_message=last_error or "Request failed",
            error_code=ErrorCode.RETRY_EXHAUSTED.value,
        )

    async def _do_request(
        self,
        method: HttpMethod,
        url: str,
        headers: Dict[str, str],
        body: Optional[Dict[str, Any]]
    ) -> httpx.Response:
        """Execute raw HTTP request using dispatch pattern."""
        with trace_span(_tracer, "api_gateway.http_request", {
            "http.method": method.value,
            "http.url_preview": url[:80],
        }):
            return await self._do_request_inner(method, url, headers, body)

    async def _do_request_inner(
        self,
        method: HttpMethod,
        url: str,
        headers: Dict[str, str],
        body: Optional[Dict[str, Any]]
    ) -> httpx.Response:
        """Inner implementation of _do_request."""
        # Dispatch table - maps HttpMethod to client method
        dispatch = {
            HttpMethod.GET: lambda: self.client.get(url, headers=headers),
            HttpMethod.POST: lambda: self.client.post(url, headers=headers, json=body),
            HttpMethod.PUT: lambda: self.client.put(url, headers=headers, json=body),
            HttpMethod.PATCH: lambda: self.client.patch(url, headers=headers, json=body),
            HttpMethod.DELETE: lambda: self.client.delete(url, headers=headers, **({"json": body} if body else {})),
        }

        handler = dispatch.get(method)
        if not handler:
            raise ValueError(f"Unsupported HTTP method: {method}")

        return await handler()

    def _get_base_parsed(self):
        parsed = getattr(self, "_base_parsed", None)
        if parsed is None:
            parsed = urlparse(self.base_url)
            self._base_parsed = parsed
        return parsed

    def _build_url(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        """
        Build URL with smart detection.

        CRITICAL FIX: If path is already a complete URL (starts with http),
        don't prepend base_url - just use it as-is.
        """
        # If path is already a complete URL, validate it matches our base domain.
        # SSRF hardening: compare on .netloc (host[:port]) and enforce scheme/userinfo.
        # Relying on .hostname alone lets attackers smuggle other hosts via userinfo,
        # e.g. https://api.example.com@evil.com/ parses .hostname == "evil.com"
        # but accepts the URL because the attacker crafted it to look legitimate.
        if path.startswith("http://") or path.startswith("https://"):
            base = self._get_base_parsed()
            target = urlparse(path)

            # Reject any URL carrying userinfo (user:pass@host) — always an SSRF
            # smuggling attempt against a plain-host allowlist.
            if target.username or target.password:
                err = StructuredGatewayError(
                    ErrorCode.SSRF_BLOCKED,
                    f"SSRF blocked: userinfo present in URL ({path})",
                    metadata={"path_host": target.hostname, "base_host": base.hostname},
                )
                logger.warning(f"{err}")
                raise ValueError("URL userinfo not allowed")

            # Scheme must match (no http→https downgrade or vice versa).
            if target.scheme != base.scheme:
                err = StructuredGatewayError(
                    ErrorCode.SSRF_BLOCKED,
                    f"SSRF blocked: scheme mismatch ({target.scheme} vs {base.scheme})",
                    metadata={"path_scheme": target.scheme, "base_scheme": base.scheme},
                )
                logger.warning(f"{err}")
                raise ValueError("URL scheme mismatch")

            # netloc comparison (host[:port]) — case-insensitive per RFC 3986.
            if (target.netloc or "").lower() != (base.netloc or "").lower():
                err = StructuredGatewayError(
                    ErrorCode.SSRF_BLOCKED,
                    f"SSRF blocked: {path} does not match base netloc {base.netloc}",
                    metadata={"path_netloc": target.netloc, "base_netloc": base.netloc},
                )
                logger.warning(f"{err}")
                raise ValueError(f"URL host mismatch: expected {base.netloc}")
            url = path
        else:
            # Relative path - prepend base_url
            if not path.startswith("/"):
                path = "/" + path
            url = f"{self.base_url}{path}"

        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                parts = []
                for k, v in clean.items():
                    encoded_key = quote(str(k), safe='')
                    if k == "Filter":
                        # Don't encode '=' as '%3D' because the API rejects it
                        # safe='=' keeps the equals sign unencoded in the value
                        encoded_val = quote(str(v), safe='=')
                    else:
                        encoded_val = quote(str(v), safe='')
                    parts.append(f"{encoded_key}={encoded_val}")
                url = f"{url}?{'&'.join(parts)}"
        return url

    def _parse_response(self, response: httpx.Response) -> APIResponse:
        """
        Parse HTTP response with FIREWALL protection.

        JSON ENFORCEMENT
        - Only JSON responses allowed (Content-Type: application/json)
        - HTML responses BLOCKED (auth redirects, nginx errors)
        - User NEVER sees HTML tags or raw error codes
        """
        headers_dict = dict(response.headers)
        content_type = response.headers.get("content-type", "").lower()

        # FIREWALL GATE 1: Detect HTML response
        is_html = (
            "text/html" in content_type or
            response.text.strip().startswith("<!DOCTYPE") or
            response.text.strip().startswith("<html")
        )

        if is_html:
            err = StructuredGatewayError(
                ErrorCode.HTML_RESPONSE_LEAKED,
                f"HTML LEAKAGE BLOCKED: Status={response.status_code}, Content-Type={content_type}",
                status_code=response.status_code,
                metadata={"content_type": content_type},
            )
            logger.error(f"{err}")

            # User-facing clean error messages
            if response.status_code == 200:
                # Even with 200, HTML means auth redirect or wrong endpoint
                error_msg = (
                    "Trenutno ne mogu dohvatiti te podatke zbog tehničkih poteškoća sa servisom. "
                    "API je vratio UI/Login stranicu umjesto podataka."
                )
                error_code = ErrorCode.HTML_RESPONSE_LEAKED.value
                status = 401  # Treat as auth error
            elif response.status_code == 404:
                error_msg = (
                    "Trenutno ne mogu dohvatiti te podatke zbog tehničkih poteškoća sa servisom. "
                    "Traženi resurs nije pronađen."
                )
                error_code = ErrorCode.NOT_FOUND.value
                status = 404
            elif response.status_code == 405:
                error_msg = (
                    "Trenutno ne mogu dohvatiti te podatke zbog tehničkih poteškoća sa servisom. "
                    "Greška u konfiguraciji API zahtjeva."
                )
                error_code = ErrorCode.METHOD_NOT_ALLOWED.value
                status = 405
            else:
                error_msg = (
                    "Trenutno ne mogu dohvatiti te podatke zbog tehničkih poteškoća sa servisom."
                )
                error_code = ErrorCode.HTML_RESPONSE_LEAKED.value
                status = response.status_code

            return APIResponse(
                success=False,
                status_code=status,
                data=None,
                error_message=error_msg,
                error_code=error_code,
                headers=headers_dict
            )

        # FIREWALL GATE 2: Normal error handling (non-HTML)
        if response.status_code >= 400:
            error_msg = self._extract_error_message(response)
            structured_code = HTTP_STATUS_TO_ERROR_CODE.get(
                response.status_code, ErrorCode.SERVER_ERROR
            )
            error_code = structured_code.value

            err = StructuredGatewayError(
                structured_code,
                f"API error: {response.status_code} - {error_msg[:200]}",
                status_code=response.status_code,
            )
            logger.warning(f"{err}")

            return APIResponse(
                success=False,
                status_code=response.status_code,
                data=None,
                error_message=error_msg,
                error_code=error_code,
                headers=headers_dict
            )

        # FIREWALL GATE 3: Success response - parse JSON
        try:
            data = response.json()
        except Exception as e:
            # JSON parsing failed - might be empty response or plain text
            logger.warning(f"JSON parsing failed: {e}")
            data = response.text if response.text else {}

        # Ensure data is never None (downstream code expects dict/list)
        if data is None:
            data = {}

        return APIResponse(
            success=True,
            status_code=response.status_code,
            data=data,
            headers=headers_dict
        )

    def _extract_error_message(self, response: httpx.Response) -> str:
        """Extract error message from response."""
        try:
            data = response.json()
            for field in ["message", "error", "detail", "title", "Message", "Error"]:
                if field in data and data[field]:
                    return str(data[field])
            if isinstance(data, dict):
                return str(data)[:500]
            return response.text[:500]
        except Exception as e:
            logger.debug(f"Error message extraction failed (non-JSON response): {e}")
            return f"HTTP {response.status_code}: {response.text[:500]}"

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff with jitter for retries."""
        return calculate_backoff(attempt, base_delay=1.0, max_delay=30.0, jitter=0.5)

    def _calculate_circuit_cooldown(self) -> float:
        """
        Calculate jittered exponential cooldown for circuit breaker.

        Uses 'decorrelated jitter' to spread recovery probes across time,
        preventing thundering herd when multiple workers' circuits close simultaneously.
        Backoff grows with consecutive failures: 15s, 30s, 60s, 120s max.
        """
        failures_above_threshold = self._consecutive_failures - self.CIRCUIT_FAILURE_THRESHOLD
        base = self.CIRCUIT_BASE_COOLDOWN_SECONDS * (2 ** max(0, failures_above_threshold))
        capped = min(base, self.CIRCUIT_MAX_COOLDOWN_SECONDS)
        # Jitter: 0.5x to 1.5x of capped cooldown, hard-capped at max
        return min(
            self.CIRCUIT_MAX_COOLDOWN_SECONDS * 1.5,
            random.uniform(capped * 0.5, capped * 1.5),
        )

    # === CONVENIENCE METHODS ===

    async def get(self, path: str, params: Optional[Dict] = None, **kwargs) -> APIResponse:
        """GET request."""
        return await self.execute(HttpMethod.GET, path, params=params, **kwargs)

    async def post(self, path: str, body: Optional[Dict] = None, **kwargs) -> APIResponse:
        """POST request."""
        return await self.execute(HttpMethod.POST, path, body=body, **kwargs)

    async def put(self, path: str, body: Optional[Dict] = None, **kwargs) -> APIResponse:
        """PUT request."""
        return await self.execute(HttpMethod.PUT, path, body=body, **kwargs)

    async def delete(self, path: str, **kwargs) -> APIResponse:
        """DELETE request."""
        return await self.execute(HttpMethod.DELETE, path, **kwargs)

    async def close(self) -> None:
        """Close HTTP client and token manager."""
        if self.client:
            await self.client.aclose()
        await self.token_manager.close()
        logger.info("APIGateway closed")
