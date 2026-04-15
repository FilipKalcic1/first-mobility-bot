"""
Security Headers Middleware - OWASP recommended HTTP security headers.

Adds protective headers to all responses to prevent common web attacks:
- XSS protection
- Clickjacking prevention
- Content type sniffing prevention
- Referrer leakage prevention
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all HTTP responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # XSS protection (legacy browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Control referrer information leakage
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # HSTS — only effective over HTTPS (browsers ignore it over HTTP)
        response.headers["Strict-Transport-Security"] = "max-age=31536000"

        # Content Security Policy - restrict resource loading
        # Relax CSP for Swagger/ReDoc UI paths (they load CSS/JS from CDN)
        normalized_path = request.url.path.rstrip("/")
        is_docs = normalized_path in ("/docs", "/redoc", "/openapi.json")
        if is_docs:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://cdn.jsdelivr.net; "
                "frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"

        # Permissions Policy - disable unused browser features
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        # Prevent caching of sensitive API responses (skip docs — they are static)
        if not is_docs:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"

        # Remove server identification header
        if "server" in response.headers:
            del response.headers["server"]

        return response
