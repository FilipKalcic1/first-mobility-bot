"""HTTP middleware classes for the FastAPI app.

Extracted from main.py 2026-05-08 to keep main.py focused on app
construction + lifespan. Each middleware here is independent — order of
registration in main.py matters but the classes themselves are pure
request/response handlers with no cross-class state.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


# Payload size guard
# JSON parsing a 10MB malicious payload at 20 concurrent requests = 200MB spike.
# Combined with baseline 280MB = OOM kill. Reject before parsing.
MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # 1MB


class PayloadSizeGuardMiddleware(BaseHTTPMiddleware):
    """Reject requests >1MB before JSON parsing to prevent OOM at 1GB RAM."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except (ValueError, OverflowError):
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
            if length > MAX_REQUEST_BODY_BYTES:
                logger.warning(
                    f"Payload rejected: {length} bytes > {MAX_REQUEST_BODY_BYTES} limit "
                    f"from {request.client.host if request.client else 'unknown'}"
                )
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large (max 1MB)"},
                )
        return await call_next(request)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add unique request ID to each request for log correlation."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:12]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect HTTP to HTTPS in production. Reads `is_production` from settings
    via a closure (set by `make_https_redirect`)."""

    def __init__(self, app, is_production_fn):
        super().__init__(app)
        self._is_production = is_production_fn

    async def dispatch(self, request: Request, call_next):
        proto = request.headers.get("X-Forwarded-Proto", "https")
        if proto == "http" and self._is_production():
            url = request.url.replace(scheme="https")
            return PlainTextResponse(
                content="Redirecting to HTTPS",
                status_code=301,
                headers={"Location": str(url)},
            )
        response = await call_next(request)
        if self._is_production():
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response
