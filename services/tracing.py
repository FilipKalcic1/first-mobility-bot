"""
OpenTelemetry Distributed Tracing Setup.

Provides end-to-end request tracing across:
  Webhook → Redis Queue → Worker → AI Orchestrator → MobilityOne API → Response

Usage:
    from services.tracing import get_tracer, trace_span

    tracer = get_tracer(__name__)

    async def my_function():
        with trace_span(tracer, "my_operation", {"key": "value"}):
            ...

Environment variables:
    OTEL_ENABLED=true              Enable tracing (default: false)
    OTEL_EXPORTER_ENDPOINT=...     Jaeger/OTLP endpoint
    OTEL_SERVICE_NAME=...          Service name (auto-detected from SERVICE_ROLE)
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Lazy imports - OpenTelemetry is optional
_tracer_provider = None
_initialized = False


def _is_enabled() -> bool:
    """Check if OpenTelemetry tracing is enabled."""
    return os.environ.get("OTEL_ENABLED", "false").lower() in ("true", "1", "yes")


def _init_tracing():
    """Initialize OpenTelemetry tracing (called once)."""
    global _tracer_provider, _initialized

    if _initialized:
        return
    _initialized = True

    if not _is_enabled():
        logger.info("OpenTelemetry tracing disabled (set OTEL_ENABLED=true to enable)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        service_name = os.environ.get(
            "OTEL_SERVICE_NAME",
            f"mobility-bot-{os.environ.get('SERVICE_ROLE', 'unknown')}"
        )

        resource = Resource.create({
            SERVICE_NAME: service_name,
            "service.version": os.environ.get("APP_VERSION", "11.0"),
            "deployment.environment": os.environ.get("APP_ENV", "development"),
        })

        _tracer_provider = TracerProvider(resource=resource)

        endpoint = os.environ.get("OTEL_EXPORTER_ENDPOINT", "http://jaeger:4317")
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(_tracer_provider)

        logger.info(f"OpenTelemetry tracing initialized: service={service_name}, endpoint={endpoint}")

    except ImportError:
        logger.info("OpenTelemetry packages not installed, tracing disabled")
    except Exception as e:
        logger.warning(f"OpenTelemetry initialization failed: {e}")


def get_tracer(name: str):
    """
    Get a tracer instance. Returns a no-op tracer if OTEL is disabled.

    Args:
        name: Module name (usually __name__)

    Returns:
        Tracer instance (real or no-op)
    """
    _init_tracing()

    if _tracer_provider:
        return _tracer_provider.get_tracer(name)

    # Return no-op tracer singleton
    return _NOOP_TRACER


@contextmanager
def trace_span(tracer, name: str, attributes: Optional[Dict[str, Any]] = None):
    """
    Context manager for creating a trace span.

    Works with both real and no-op tracers.

    Args:
        tracer: Tracer instance from get_tracer()
        name: Span name
        attributes: Optional span attributes
    """
    if isinstance(tracer, _NoOpTracer):
        yield _NOOP_SPAN
        return

    try:
        with tracer.start_as_current_span(name) as span:
            if attributes:
                for key, value in attributes.items():
                    span.set_attribute(key, str(value) if not isinstance(value, (str, int, float, bool)) else value)
            yield span
    except Exception as e:
        logger.debug(f"Tracing span creation failed, using no-op: {e}")
        yield _NOOP_SPAN


class _NoOpSpan:
    """No-op span for when tracing is disabled."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status) -> None:
        pass

    def record_exception(self, exception: Exception) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[Dict] = None) -> None:
        pass


# Singletons to avoid allocating new objects on every span
_NOOP_SPAN = _NoOpSpan()


class _NoOpTracer:
    """No-op tracer for when OpenTelemetry is not installed."""

    def start_as_current_span(self, name: str, **kwargs):
        return _NoOpSpanContext()

    def start_span(self, name: str, **kwargs):
        return _NOOP_SPAN


class _NoOpSpanContext:
    """Context manager for no-op spans."""

    def __enter__(self):
        return _NOOP_SPAN

    def __exit__(self, *args):
        pass


_NOOP_TRACER = _NoOpTracer()


async def shutdown_tracing():
    """Shutdown the tracer provider (call on app shutdown)."""
    if _tracer_provider:
        try:
            _tracer_provider.shutdown()
            logger.info("OpenTelemetry tracing shut down")
        except Exception as e:
            logger.warning(f"OpenTelemetry shutdown error: {e}")
