"""
Query router — ML stub after Phase D cull.

The legacy router used a TF-IDF/SBERT ensemble (`predict_with_ensemble`) to
short-circuit to a single tool before hitting the LLM. V6.2 / Phase D removed
that classifier and its training data. The router is kept only as a thin,
always-defer shim so downstream call sites (engine, deterministic_executor,
unified_router) keep importing stable symbols without the ML surface.

Behaviour:
  - `QueryRouter.route(...)` always returns `RouteResult(matched=False)`.
  - The engine falls through to semantic search / LLM orchestration.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.dynamic_threshold import ClassificationSignal, PredictionSet

logger = logging.getLogger(__name__)


@dataclass
class RouteResult:
    matched: bool
    tool_name: Optional[str] = None
    extract_fields: List[str] = field(default_factory=list)
    response_template: Optional[str] = None
    flow_type: Optional[str] = None
    confidence: float = 0.0
    signal: Optional[ClassificationSignal] = None
    prediction_set: Optional[PredictionSet] = None
    reason: str = ""


# Preserved for call sites that still reference these names.
INTENT_METADATA: Dict[str, Any] = {}
ML_CONFIDENCE_THRESHOLD: float = 0.0


class QueryRouter:
    """Always-defer stub. Real routing happens via unified_router + semantic search."""

    def route(
        self,
        query: str,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> RouteResult:
        return RouteResult(
            matched=False,
            confidence=0.0,
            reason="ml_router_disabled",
        )


_singleton: Optional[QueryRouter] = None
_singleton_lock = threading.Lock()


def get_query_router() -> QueryRouter:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = QueryRouter()
    return _singleton


__all__ = [
    "RouteResult",
    "QueryRouter",
    "get_query_router",
    "INTENT_METADATA",
    "ML_CONFIDENCE_THRESHOLD",
]
