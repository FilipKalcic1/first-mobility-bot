"""Anchor cosine-similarity retrieval over 950 tools.

Each tool has ~12 short Croatian anchor phrases (from
`config/tool_anchor_enrichments.json`, regenerated 2026-05-12 by
`scripts/regenerate_anchors.py`). We embed every phrase once on init,
cache the matrix to disk fingerprinted by content hash, and at query
time return the top-K tool_ids by max-cosine over their phrases.

The "max over phrases" aggregation is intentional: a query like
"obriši rezervaciju 123" should match the BY_ID delete tool only on the
phrase that explicitly mentions IDs — averaging would dilute that
signal under the more generic phrases. The trade-off is recall vs
precision; max maximizes per-tool best match.

API:
    idx = AnchorIndex(anchors_path, cache_path)
    await idx.build(embed_fn)        # one-shot init; reads cache if valid
    top = await idx.top_k(query, embed_fn, k=50)  # → [(tool_id, score), ...]
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Callable signature for the embedding function injected by the caller.
# Returns a list of float vectors, one per input text. Single-call batch is
# used during build (all 950 * 12 phrases) and per-query during routing.
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


@dataclass
class AnchorIndex:
    # Either `anchors_path` (legacy flat JSON) or `anchors_data` (pre-loaded
    # dict from tool_data.json) — exactly ONE must be supplied. The dict
    # path is what the new factory uses; the file path is kept for tests +
    # the `diagnose_anchor_recall.py` standalone script.
    anchors_path: Optional[Path] = None
    anchors_data: Optional[dict[str, list[str]]] = None
    cache_path: Optional[Path] = None
    embedding_deployment: str = ""  # used only in fingerprint

    # Populated by build():
    _anchors_by_tool: dict[str, list[str]] = field(default_factory=dict)
    _vectors_by_tool: dict[str, list[list[float]]] = field(default_factory=dict)
    _initialized: bool = False

    async def build(self, embed_fn: EmbedFn) -> None:
        """Embed every anchor phrase once. Reads cache if fingerprint matches."""
        if self._initialized:
            return
        if self.anchors_data is not None:
            data = self.anchors_data
        elif self.anchors_path is not None:
            data = json.loads(self.anchors_path.read_text(encoding="utf-8"))
        else:
            raise ValueError(
                "AnchorIndex requires either anchors_path or anchors_data"
            )
        # data shape: {tool_id: [phrase, phrase, ...]}
        self._anchors_by_tool = {
            tid: list(phrases)
            for tid, phrases in data.items()
            if phrases
        }

        if self.cache_path and self._load_from_cache():
            self._initialized = True
            logger.info(
                "anchor_index: loaded %d tools from cache (%s)",
                len(self._vectors_by_tool), self.cache_path,
            )
            return

        # Embed all phrases. Flatten to one big list, then re-split by tool.
        flat_phrases: list[str] = []
        offsets: list[tuple[str, int, int]] = []  # (tool_id, start, end_exclusive)
        for tid, phrases in self._anchors_by_tool.items():
            start = len(flat_phrases)
            flat_phrases.extend(phrases)
            offsets.append((tid, start, len(flat_phrases)))

        logger.info(
            "anchor_index: embedding %d phrases across %d tools...",
            len(flat_phrases), len(self._anchors_by_tool),
        )
        # Batch in chunks of 256 to respect Azure embedding limits.
        vectors: list[list[float]] = []
        chunk = 256
        for i in range(0, len(flat_phrases), chunk):
            batch = flat_phrases[i : i + chunk]
            batch_vecs = await embed_fn(batch)
            if len(batch_vecs) != len(batch):
                raise RuntimeError(
                    f"embed batch length mismatch: in={len(batch)} out={len(batch_vecs)}"
                )
            vectors.extend(batch_vecs)

        for tid, start, end in offsets:
            self._vectors_by_tool[tid] = vectors[start:end]

        self._save_to_cache()
        self._initialized = True
        logger.info(
            "anchor_index: built %d tools, %d vectors (dim=%d)",
            len(self._vectors_by_tool), len(vectors),
            len(vectors[0]) if vectors else 0,
        )

    async def top_k(
        self,
        query: str,
        embed_fn: EmbedFn,
        k: int = 50,
        allowed_tool_ids: Optional[frozenset[str]] = None,
    ) -> list[tuple[str, float]]:
        """Embed query, score each tool by max-cosine over its phrases.

        ``allowed_tool_ids`` — when provided (Phase E persona scoping),
        only score tools whose id is in this set. Drastically narrows
        sibling competition for short queries (e.g. driver "marka" no
        longer competes with `get_VehicleInputHelper_DistinctMakes`).
        """
        if not self._initialized:
            raise RuntimeError("AnchorIndex not built — call .build() first")
        if not query or not query.strip():
            return []
        q_vecs = await embed_fn([query])
        q_vec = q_vecs[0]
        q_norm = math.sqrt(sum(x * x for x in q_vec)) or 1.0

        results: list[tuple[str, float]] = []
        for tid, phrase_vecs in self._vectors_by_tool.items():
            if allowed_tool_ids is not None and tid not in allowed_tool_ids:
                continue
            best = -2.0
            for v in phrase_vecs:
                v_norm = math.sqrt(sum(x * x for x in v)) or 1.0
                dot = sum(x * y for x, y in zip(q_vec, v))
                sim = dot / (q_norm * v_norm)
                if sim > best:
                    best = sim
            results.append((tid, best))

        results.sort(key=lambda r: -r[1])
        return results[:k]

    # ---- cache ----

    def _fingerprint(self) -> str:
        """Identity hash for the anchor data + embedding deployment.
        Mismatch invalidates the cache."""
        h = hashlib.sha256()
        h.update(self.embedding_deployment.encode("utf-8"))
        for tid in sorted(self._anchors_by_tool):
            for phrase in self._anchors_by_tool[tid]:
                h.update(tid.encode("utf-8"))
                h.update(b"\0")
                h.update(phrase.encode("utf-8"))
                h.update(b"\0")
        return h.hexdigest()

    def _load_from_cache(self) -> bool:
        if not self.cache_path or not self.cache_path.exists():
            return False
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("anchor cache unreadable: %s", e)
            return False
        if data.get("fingerprint") != self._fingerprint():
            logger.info("anchor cache stale; rebuilding")
            return False
        self._vectors_by_tool = {
            tid: vectors
            for tid, vectors in data.get("vectors_by_tool", {}).items()
        }
        return bool(self._vectors_by_tool)

    def _save_to_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({
                    "fingerprint": self._fingerprint(),
                    "vectors_by_tool": self._vectors_by_tool,
                }),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("anchor cache save failed: %s", e)

