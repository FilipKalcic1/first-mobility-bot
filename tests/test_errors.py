"""
Tests for services/errors.py

Covers:
- ErrorCode enum completeness and str-backed serialization
- BotError construction, __str__, to_dict, with_metadata
- GatewayError with status_code, from_status, is_retryable
- CircuitOpenError convenience constructor
- ConversationError, InfrastructureError
- HTTP_STATUS_TO_ERROR_CODE mapping
"""

import pytest

from services.errors import (
    ErrorCode,
    BotError,
    GatewayError,
    CircuitOpenError,
    ConversationError,
    InfrastructureError,
    HTTP_STATUS_TO_ERROR_CODE,
)


# ============================================================================
# ErrorCode enum
# ============================================================================

class TestErrorCode:
    def test_is_str_enum(self):
        """ErrorCode values are strings for JSON/OTel serialization."""
        assert isinstance(ErrorCode.MODEL_NOT_LOADED.value, str)
        assert isinstance(ErrorCode.CIRCUIT_OPEN.value, str)

    def test_domain_prefix(self):
        """Every code follows {DOMAIN}_{SPECIFIC} naming."""
        for code in ErrorCode:
            parts = code.value.split("_", 1)
            assert len(parts) == 2, f"{code.value} missing domain prefix"

    def test_all_domains_present(self):
        """All expected domains have at least one code."""
        domains = {c.value.split("_", 1)[0] for c in ErrorCode}
        expected = {"CLASSIFICATION", "SEARCH", "ROUTING", "GATEWAY",
                    "CONVERSATION", "INFRA"}
        assert expected.issubset(domains)

    def test_unique_values(self):
        """No duplicate enum values."""
        values = [c.value for c in ErrorCode]
        assert len(values) == len(set(values))

    def test_str_conversion(self):
        """ErrorCode can be used as plain string."""
        assert "MODEL_NOT_LOADED" in str(ErrorCode.MODEL_NOT_LOADED)
        assert ErrorCode.MODEL_NOT_LOADED.value == "CLASSIFICATION_MODEL_NOT_LOADED"


# ============================================================================
# BotError base
# ============================================================================

class TestBotError:
    def test_construction(self):
        err = BotError(ErrorCode.MODEL_NOT_LOADED, "model missing")
        assert err.code is ErrorCode.MODEL_NOT_LOADED
        assert err.message == "model missing"
        assert err.metadata == {}

    def test_str_includes_code(self):
        err = BotError(ErrorCode.FAISS_NOT_INITIALIZED, "no index")
        assert "[SEARCH_FAISS_NOT_INITIALIZED]" in str(err)
        assert "no index" in str(err)

    def test_metadata(self):
        err = BotError(
            ErrorCode.MODEL_LOAD_FAILED, "load fail",
            metadata={"path": "/models/intent"},
        )
        assert err.metadata["path"] == "/models/intent"

    def test_with_metadata_fluent(self):
        err = BotError(ErrorCode.PREDICTION_FAILED, "oops")
        result = err.with_metadata(query="test", attempts=3)
        assert result is err  # fluent API returns self
        assert err.metadata["query"] == "test"
        assert err.metadata["attempts"] == 3

    def test_to_dict(self):
        err = BotError(
            ErrorCode.ENSEMBLE_ALL_FAILED, "all failed",
            metadata={"algo": "tfidf_lr"},
        )
        d = err.to_dict()
        assert d["error_code"] == "CLASSIFICATION_ENSEMBLE_ALL_FAILED"
        assert d["error_domain"] == "CLASSIFICATION"
        assert d["message"] == "all failed"
        assert d["metadata"]["algo"] == "tfidf_lr"

    def test_to_dict_with_cause(self):
        cause = ValueError("bad input")
        err = BotError(ErrorCode.PREDICTION_FAILED, "predict fail", cause=cause)
        d = err.to_dict()
        assert "cause" in d
        assert "bad input" in d["cause"]

    def test_cause_chaining(self):
        cause = RuntimeError("disk full")
        err = BotError(ErrorCode.MODEL_LOAD_FAILED, "load fail", cause=cause)
        assert err.__cause__ is cause

    def test_is_exception(self):
        err = BotError(ErrorCode.PREDICTION_FAILED, "test")
        assert isinstance(err, Exception)
        with pytest.raises(BotError):
            raise err

    def test_metadata_default_empty_dict(self):
        err = BotError(ErrorCode.MODEL_NOT_LOADED, "test")
        assert err.metadata is not None
        assert isinstance(err.metadata, dict)
        assert len(err.metadata) == 0


# ============================================================================
# GatewayError
# ============================================================================

class TestGatewayError:
    def test_status_code(self):
        err = GatewayError(
            ErrorCode.SERVER_ERROR, "500",
            status_code=500,
        )
        assert err.status_code == 500

    def test_from_status(self):
        err = GatewayError.from_status(429, "rate limited")
        assert err.code is ErrorCode.RATE_LIMITED
        assert err.status_code == 429

    def test_from_status_unknown(self):
        err = GatewayError.from_status(418, "teapot")
        assert err.code is ErrorCode.SERVER_ERROR  # fallback
        assert err.status_code == 418

    def test_is_retryable(self):
        for status in [408, 429, 500, 502, 503, 504]:
            err = GatewayError(ErrorCode.SERVER_ERROR, "err", status_code=status)
            assert err.is_retryable, f"Status {status} should be retryable"

    def test_not_retryable(self):
        for status in [400, 401, 403, 404, 405, 422]:
            err = GatewayError(ErrorCode.BAD_REQUEST, "err", status_code=status)
            assert not err.is_retryable, f"Status {status} should NOT be retryable"

    def test_to_dict_includes_status(self):
        err = GatewayError(ErrorCode.NOT_FOUND, "404", status_code=404)
        d = err.to_dict()
        assert d["status_code"] == 404


# ============================================================================
# CircuitOpenError
# ============================================================================

class TestCircuitOpenError:
    def test_construction(self):
        err = CircuitOpenError("/api/vehicles", cooldown_seconds=30.0)
        assert err.code is ErrorCode.CIRCUIT_OPEN
        assert err.status_code == 503
        assert err.endpoint == "/api/vehicles"
        assert err.cooldown_seconds == 30.0

    def test_metadata_includes_endpoint(self):
        err = CircuitOpenError("/api/booking")
        assert err.metadata["endpoint"] == "/api/booking"

    def test_is_gateway_error(self):
        err = CircuitOpenError("/test")
        assert isinstance(err, GatewayError)
        assert isinstance(err, BotError)


# ============================================================================
# ConversationError, InfrastructureError
# ============================================================================

class TestConversationError:
    def test_is_bot_error(self):
        err = ConversationError(ErrorCode.FLOW_NOT_FOUND, "no flow")
        assert isinstance(err, BotError)


class TestInfrastructureError:
    def test_is_bot_error(self):
        err = InfrastructureError(ErrorCode.REDIS_UNAVAILABLE, "redis down")
        assert isinstance(err, BotError)


# ============================================================================
# HTTP_STATUS_TO_ERROR_CODE mapping
# ============================================================================

class TestHTTPStatusMapping:
    def test_all_common_statuses_mapped(self):
        # 409 added 2026-05-08 (audit fix — was falling through to
        # generic SERVER_ERROR despite v2/http_status_handler having
        # the right Croatian message ready)
        expected = {400, 401, 403, 404, 405, 408, 409, 422, 429, 500, 502, 503, 504}
        assert expected == set(HTTP_STATUS_TO_ERROR_CODE.keys())

    def test_correct_mapping(self):
        assert HTTP_STATUS_TO_ERROR_CODE[400] is ErrorCode.BAD_REQUEST
        assert HTTP_STATUS_TO_ERROR_CODE[401] is ErrorCode.UNAUTHORIZED
        assert HTTP_STATUS_TO_ERROR_CODE[404] is ErrorCode.NOT_FOUND
        assert HTTP_STATUS_TO_ERROR_CODE[409] is ErrorCode.CONFLICT
        assert HTTP_STATUS_TO_ERROR_CODE[429] is ErrorCode.RATE_LIMITED
        assert HTTP_STATUS_TO_ERROR_CODE[500] is ErrorCode.SERVER_ERROR
        assert HTTP_STATUS_TO_ERROR_CODE[503] is ErrorCode.SERVICE_UNAVAILABLE


