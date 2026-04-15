"""Pydantic domain models used by the routing layer."""

from __future__ import annotations

from enum import Enum, unique
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


@unique
class RoutingTier(str, Enum):
    """Which routing tier handled this query."""
    FAST_PATH = "fast_path"           # ML only, 0 LLM calls, <1ms
    MEDIATION = "mediation"           # CP set 2-5 → LLM reranker, ~200ms
    FULL_SEARCH = "full_search"       # FAISS + full LLM routing, 2-3s
    DETERMINISTIC = "deterministic"   # Pattern match (greeting, exit, flow signal)


class RoutingTrace(BaseModel):
    """Structured trace of a routing decision for OpenTelemetry."""
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    query: str = Field(default="")
    normalized_query: str = Field(default="")
    tier: RoutingTier = Field(default=RoutingTier.FULL_SEARCH)

    ml_intent: Optional[str] = Field(default=None)
    ml_confidence: float = Field(default=0.0)
    ml_algorithm: str = Field(default="tfidf_lr")

    cp_set_size: int = Field(default=0)
    cp_coverage: float = Field(default=0.0)
    cp_labels: List[str] = Field(default_factory=list)

    effective_score: float = Field(default=0.0)
    decision_action: str = Field(default="DEFER")
    boundary_name: str = Field(default="")

    faiss_candidates: int = Field(default=0)
    top_faiss_score: float = Field(default=0.0)
    exact_match_hit: bool = Field(default=False)

    rerank_winner: Optional[str] = Field(default=None)
    rerank_confidence: float = Field(default=0.0)

    ambiguity_detected: bool = Field(default=False)
    ambiguity_suffix: Optional[str] = Field(default=None)

    selected_tool: Optional[str] = Field(default=None)
    final_confidence: float = Field(default=0.0)
    latency_ms: float = Field(default=0.0)

    def to_span_attributes(self) -> Dict[str, Any]:
        """Flat dict for OpenTelemetry span attributes (no nested objects)."""
        attrs: Dict[str, Any] = {
            "routing.tier": self.tier.value,
            "routing.query_length": len(self.query),
            "routing.latency_ms": self.latency_ms,
        }
        if self.ml_intent:
            attrs["routing.ml.intent"] = self.ml_intent
            attrs["routing.ml.confidence"] = self.ml_confidence
            attrs["routing.ml.algorithm"] = self.ml_algorithm
        if self.cp_set_size > 0:
            attrs["routing.cp.set_size"] = self.cp_set_size
            attrs["routing.cp.coverage"] = self.cp_coverage
        attrs["routing.decision.action"] = self.decision_action
        attrs["routing.decision.effective_score"] = self.effective_score
        if self.boundary_name:
            attrs["routing.decision.boundary"] = self.boundary_name
        if self.faiss_candidates > 0:
            attrs["routing.search.candidates"] = self.faiss_candidates
            attrs["routing.search.top_score"] = self.top_faiss_score
            attrs["routing.search.exact_match"] = self.exact_match_hit
        if self.rerank_winner:
            attrs["routing.rerank.winner"] = self.rerank_winner
            attrs["routing.rerank.confidence"] = self.rerank_confidence
        attrs["routing.ambiguity.detected"] = self.ambiguity_detected
        if self.selected_tool:
            attrs["routing.result.tool"] = self.selected_tool
            attrs["routing.result.confidence"] = self.final_confidence
        return attrs
