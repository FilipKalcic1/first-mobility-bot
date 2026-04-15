"""
Structured Error Hierarchy for MobilityOne WhatsApp Bot.

ALL errors in the system inherit from BotError and carry:
  - ErrorCode enum (machine-readable, never a raw string)
  - Human-readable message (Croatian where user-facing)
  - Optional metadata dict (structured context for observability)

Usage:
    from services.errors import (
        ClassificationError, ErrorCode, SearchError,
        RoutingError, GatewayError, CircuitOpenError,
    )

    raise ClassificationError(
        ErrorCode.MODEL_NOT_LOADED,
        "TF-IDF model not found on disk",
        metadata={"model_path": "/app/models/intent/tfidf_vectorizer.pkl"},
    )

Design decisions:
    - ErrorCode is a str enum so it serializes cleanly to JSON/OpenTelemetry attributes.
    - Every concrete exception records its code in self.code for structured logging.
    - BotError.__str__ includes the code prefix for grep-ability in logs.
    - Metadata is always a dict (never None) to avoid defensive checks downstream.
"""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Error codes — exhaustive, str-backed for serialization
# ---------------------------------------------------------------------------

@unique
class ErrorCode(str, Enum):
    """Machine-readable error codes.

    Naming: {DOMAIN}_{SPECIFIC_FAILURE}
    """

    # ── Classification ─────────────────────────────────────────────
    MODEL_NOT_LOADED = "CLASSIFICATION_MODEL_NOT_LOADED"
    MODEL_LOAD_FAILED = "CLASSIFICATION_MODEL_LOAD_FAILED"
    PREDICTION_FAILED = "CLASSIFICATION_PREDICTION_FAILED"
    LABEL_ENCODER_MISMATCH = "CLASSIFICATION_LABEL_ENCODER_MISMATCH"
    TRAINING_DATA_INVALID = "CLASSIFICATION_TRAINING_DATA_INVALID"
    CALIBRATION_MISSING = "CLASSIFICATION_CALIBRATION_MISSING"
    ENSEMBLE_ALL_FAILED = "CLASSIFICATION_ENSEMBLE_ALL_FAILED"
    MODEL_INTEGRITY_FAILED = "CLASSIFICATION_MODEL_INTEGRITY_FAILED"

    # ── Security ──────────────────────────────────────────────────
    SECURITY_VALIDATION_FAILED = "SECURITY_VALIDATION_FAILED"

    # ── Search ─────────────────────────────────────────────────────
    FAISS_NOT_INITIALIZED = "SEARCH_FAISS_NOT_INITIALIZED"
    FAISS_INDEX_CORRUPT = "SEARCH_FAISS_INDEX_CORRUPT"
    EMBEDDING_GENERATION_FAILED = "SEARCH_EMBEDDING_GENERATION_FAILED"
    TOOL_DOCS_NOT_LOADED = "SEARCH_TOOL_DOCS_NOT_LOADED"
    SEARCH_PIPELINE_FAILED = "SEARCH_PIPELINE_FAILED"
    EXACT_MATCH_INDEX_FAILED = "SEARCH_EXACT_MATCH_INDEX_FAILED"
    BOOST_OVERFLOW = "SEARCH_BOOST_OVERFLOW"

    # ── Routing ────────────────────────────────────────────────────
    ROUTER_NOT_INITIALIZED = "ROUTING_NOT_INITIALIZED"
    ML_FAST_PATH_FAILED = "ROUTING_ML_FAST_PATH_FAILED"
    MEDIATION_FAILED = "ROUTING_MEDIATION_FAILED"
    LLM_ROUTING_FAILED = "ROUTING_LLM_FAILED"
    RERANK_FAILED = "ROUTING_RERANK_FAILED"
    AMBIGUITY_DETECTION_FAILED = "ROUTING_AMBIGUITY_DETECTION_FAILED"
    TOOL_NOT_FOUND = "ROUTING_TOOL_NOT_FOUND"
    INVALID_ROUTE_ACTION = "ROUTING_INVALID_ACTION"

    # ── API Gateway ────────────────────────────────────────────────
    CIRCUIT_OPEN = "GATEWAY_CIRCUIT_OPEN"
    RETRY_EXHAUSTED = "GATEWAY_RETRY_EXHAUSTED"
    TOKEN_REFRESH_FAILED = "GATEWAY_TOKEN_REFRESH_FAILED"
    SSRF_BLOCKED = "GATEWAY_SSRF_BLOCKED"
    HTML_RESPONSE_LEAKED = "GATEWAY_HTML_RESPONSE"
    BAD_REQUEST = "GATEWAY_BAD_REQUEST"
    UNAUTHORIZED = "GATEWAY_UNAUTHORIZED"
    FORBIDDEN = "GATEWAY_FORBIDDEN"
    NOT_FOUND = "GATEWAY_NOT_FOUND"
    METHOD_NOT_ALLOWED = "GATEWAY_METHOD_NOT_ALLOWED"
    VALIDATION_ERROR = "GATEWAY_VALIDATION_ERROR"
    RATE_LIMITED = "GATEWAY_RATE_LIMITED"
    SERVER_ERROR = "GATEWAY_SERVER_ERROR"
    BAD_GATEWAY = "GATEWAY_BAD_GATEWAY"
    SERVICE_UNAVAILABLE = "GATEWAY_SERVICE_UNAVAILABLE"
    TIMEOUT = "GATEWAY_TIMEOUT"
    MISSING_TENANT_CONTEXT = "GATEWAY_MISSING_TENANT_CONTEXT"

    # ── Conversation ───────────────────────────────────────────────
    INVALID_STATE_TRANSITION = "CONVERSATION_INVALID_STATE"
    FLOW_NOT_FOUND = "CONVERSATION_FLOW_NOT_FOUND"
    CONTEXT_MISSING = "CONVERSATION_CONTEXT_MISSING"
    CONSENT_REQUIRED = "CONVERSATION_CONSENT_REQUIRED"
    CONSENT_EXPIRED = "CONVERSATION_CONSENT_EXPIRED"
    USER_NOT_REGISTERED = "CONVERSATION_USER_NOT_REGISTERED"
    USER_NOT_FOUND = "CONVERSATION_USER_NOT_FOUND"
    FLOW_TIMEOUT = "CONVERSATION_FLOW_TIMEOUT"
    DUPLICATE_MESSAGE = "CONVERSATION_DUPLICATE_MESSAGE"

    # ── Messaging ──────────────────────────────────────────────────
    MESSAGE_TOO_LONG = "MESSAGING_MESSAGE_TOO_LONG"
    UNSUPPORTED_MEDIA = "MESSAGING_UNSUPPORTED_MEDIA"

    # ── Validation ─────────────────────────────────────────────────
    PHONE_INVALID = "VALIDATION_PHONE_INVALID"
    TENANT_MISMATCH = "VALIDATION_TENANT_MISMATCH"
    PARAMETER_MISSING = "VALIDATION_PARAMETER_MISSING"
    PARAMETER_INVALID = "VALIDATION_PARAMETER_INVALID"

    # ── Tool Execution ─────────────────────────────────────────────
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_SCHEMA_INVALID = "TOOL_SCHEMA_INVALID"

    # ── Infrastructure ─────────────────────────────────────────────
    REDIS_UNAVAILABLE = "INFRA_REDIS_UNAVAILABLE"
    DATABASE_UNAVAILABLE = "INFRA_DATABASE_UNAVAILABLE"
    AZURE_OPENAI_UNAVAILABLE = "INFRA_AZURE_OPENAI_UNAVAILABLE"
    DLQ_WRITE_FAILED = "INFRA_DLQ_WRITE_FAILED"


# ---------------------------------------------------------------------------
# HTTP status → ErrorCode mapping (for api_gateway.py)
# ---------------------------------------------------------------------------

HTTP_STATUS_TO_ERROR_CODE: Dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    408: ErrorCode.TIMEOUT,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.SERVER_ERROR,
    502: ErrorCode.BAD_GATEWAY,
    503: ErrorCode.SERVICE_UNAVAILABLE,
    504: ErrorCode.TIMEOUT,
}


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------

class BotError(Exception):
    """Base exception for all MobilityOne bot errors.

    Carries a structured ErrorCode and optional metadata dict.
    Subclasses specialize by domain (Classification, Search, Routing, Gateway).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.code: ErrorCode = code
        self.message: str = message
        self.metadata: Dict[str, Any] = metadata or {}
        if cause is not None:
            self.__cause__ = cause
        super().__init__(f"[{code.value}] {message}")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON logging / OpenTelemetry event."""
        result: Dict[str, Any] = {
            "error_code": self.code.value,
            "error_domain": self.code.value.split("_", 1)[0],
            "message": self.message,
        }
        if self.metadata:
            result["metadata"] = self.metadata
        if self.__cause__ is not None:
            result["cause"] = str(self.__cause__)
        return result

    def with_metadata(self, **kwargs: Any) -> "BotError":
        """Return self with additional metadata (fluent API)."""
        self.metadata.update(kwargs)
        return self


# ---------------------------------------------------------------------------
# Domain-specific exceptions
# ---------------------------------------------------------------------------

class ClassificationError(BotError):
    """ML classification failures (intent, query type, action intent)."""
    pass


class SearchError(BotError):
    """FAISS / BM25 / exact-match search failures."""
    pass


class RoutingError(BotError):
    """3-tier routing pipeline failures (ML fast path, mediation, LLM)."""
    pass


class GatewayError(BotError):
    """MobilityOne API gateway failures (HTTP, auth, circuit breaker).

    Extends BotError with status_code for HTTP-aware error handling.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(code, message, metadata=metadata, cause=cause)
        self.status_code: int = status_code

    @classmethod
    def from_status(
        cls,
        status_code: int,
        message: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "GatewayError":
        """Create GatewayError from HTTP status code."""
        code = HTTP_STATUS_TO_ERROR_CODE.get(status_code, ErrorCode.SERVER_ERROR)
        return cls(code, message, status_code=status_code, metadata=metadata)

    @property
    def is_retryable(self) -> bool:
        """Whether this error warrants a retry."""
        return self.status_code in {408, 429, 500, 502, 503, 504}

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if self.status_code:
            result["status_code"] = self.status_code
        return result


class CircuitOpenError(GatewayError):
    """Raised when circuit breaker is open for an endpoint.

    Drop-in replacement for circuit_breaker.CircuitOpenError with
    structured error code and metadata.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        cooldown_seconds: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta = {"endpoint": endpoint, "cooldown_seconds": cooldown_seconds}
        if metadata:
            meta.update(metadata)
        super().__init__(
            ErrorCode.CIRCUIT_OPEN,
            f"Circuit breaker open for endpoint: {endpoint}",
            status_code=503,
            metadata=meta,
        )
        self.endpoint: str = endpoint
        self.cooldown_seconds: float = cooldown_seconds


class SecurityError(BotError):
    """Security-related errors (integrity check failures, unauthorized access)."""
    pass


class ConversationError(BotError):
    """Conversation state machine and flow errors."""
    pass


class InfrastructureError(BotError):
    """Redis, PostgreSQL, Azure OpenAI infrastructure errors."""
    pass


# ---------------------------------------------------------------------------
# Convenience factory for the most common gateway errors
# ---------------------------------------------------------------------------

def gateway_error_from_response(
    status_code: int,
    url: str,
    body: Optional[str] = None,
) -> GatewayError:
    """Create a GatewayError from an HTTP response.

    Extracts the appropriate ErrorCode from the status code and includes
    the URL and response body as metadata for debugging.
    """
    code = HTTP_STATUS_TO_ERROR_CODE.get(status_code, ErrorCode.SERVER_ERROR)
    message = f"HTTP {status_code} from {url}"
    metadata: Dict[str, Any] = {"url": url}
    if body:
        # Truncate to prevent huge log entries
        metadata["response_body"] = body[:500]
    return GatewayError(code, message, status_code=status_code, metadata=metadata)
