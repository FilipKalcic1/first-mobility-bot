"""Top-K candidate retriever for UnifiedResponder.

Wraps RecognitionEngine's FAISS index. Returns list of dicts in shape
expected by UnifiedResponder._format_tools:
  {tool_id, method, intent_summary, returns, required_params}

Pulls intent_summary + returns from TKB if available; otherwise from
registry.purpose / parameters.

This is the ONLY pre-LLM gate — narrows 950 candidates to top-30 via
embedding similarity, persona filter applied. Cheap (~50ms per query
once anchor index is warm).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TKB_PATH = Path(__file__).resolve().parents[2] / "config" / "tool_knowledge_base.json"


class UnifiedRetriever:
    """Adapter from RecognitionEngine + TKB + registry into the
    candidate-list contract UnifiedResponder expects.

    Constructor takes a recognition_engine (must already have
    initialize() called) plus optional TKB path. Loads TKB once.
    """

    def __init__(
        self,
        recognition_engine,
        registry,
        tkb_path: Optional[Path] = None,
    ):
        self._rec = recognition_engine
        self._registry = registry
        self._tkb_path = tkb_path or DEFAULT_TKB_PATH
        self._tkb: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        if self._tkb_path.exists():
            try:
                with self._tkb_path.open(encoding="utf-8") as f:
                    self._tkb = json.load(f).get("tools", {})
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("TKB load failed: %s", e)
        self._loaded = True
        logger.info("UnifiedRetriever loaded: %d TKB entries", len(self._tkb))

    async def __call__(
        self, query: str, top_k: int = 30,
    ) -> list[dict]:
        if not self._loaded:
            self.load()
        # FAISS retrieve via existing RecognitionEngine — privacy-respecting
        # (works on anchor_text embeddings already cached on disk).
        rec_result = await self._rec.recognize(
            query=query,
        )
        # rec_result.candidates is list of ToolCandidate-like objects with
        # tool_id, anchor_score, purpose, method
        cands = list(rec_result.candidates or [])[:top_k]
        out: list[dict] = []
        for c in cands:
            tid = c.tool_id
            tkb = self._tkb.get(tid, {})
            tool = self._registry_lookup(tid)
            method = (
                getattr(c, "method", None)
                or (tool.get("method") if tool else None)
                or "GET"
            )
            entry = {
                "tool_id": tid,
                "method": method,
                "intent_summary": tkb.get("intent_summary") or self._fallback_intent(tool, tid),
                "returns": tkb.get("returns") or {},
                "required_params": (tool.get("required_params") if tool else None) or [],
                "anchor_score": getattr(c, "anchor_score", 0.0),
            }
            out.append(entry)
        return out

    def _registry_lookup(self, tool_id: str) -> Optional[dict]:
        for t in self._registry.tools:
            if self._registry.tool_id_of(t) == tool_id:
                return t if isinstance(t, dict) else None
        return None

    @staticmethod
    def _fallback_intent(tool: Optional[dict], tool_id: str) -> str:
        if tool:
            return (
                tool.get("summary")
                or tool.get("description", "")[:120]
                or tool.get("purpose", "")[:120]
                or tool_id
            )
        return tool_id
