"""L3 — Recognition Engine.

The "real recognition" backbone for everything that L1+L2 didn't catch
(manager surface, edge driver queries, anything novel). Replaces the
prior 18-category hardcoded classifier.

Pipeline:
  1. Per-tool anchor index — semantic match against 950 tools using
     anchor sentences derived from tool documentation (purpose +
     when_to_use + example_queries_hr from the registry). Returns top-K.
  2. LLM Judge prompt — receives query + top-K candidates, must:
     - Pick exactly one tool OR a flow name
     - Provide rationale ≥ 30 chars
     - Self-confidence 0-1
     - JSON output, strict schema
  3. Tool whitelist validation — picked tool MUST exist in registry.
  4. Disagreement detector — if top anchor != LLM choice, downgrade
     confidence and let L5 fallback decide.

Recognition vs hardcoding ratio:
  - Anchors built from REGISTRY DATA (purpose strings) — that's the
    Swagger contract, not domain hardcoding by us.
  - LLM judges on top-K from real semantic search.
  - No "intent X → tool Y" map in code.

Output: RecognitionResult — passed to L5 confidence gate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json as _json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from services.utils.llm_json import parse_llm_json
from services.utils.vector import cosine_similarity
from services.utils.sparse import reciprocal_rank_fusion
from services.utils.hierarchy import classify_category, classify_service
from services.v2.hierarchical_entity_retrieval import (
    EntityIndex, PerEntityToolIndex, hierarchical_retrieve,
)
from services.v2.confusion_disambiguator import (
    ConfusionDisambiguator, apply_disambiguator_bias,
)

logger = logging.getLogger(__name__)


def _shared_prefix_hint(candidates) -> str:
    """If multiple candidates share a long common prefix (e.g.
    delete_Tags, delete_Tags_id, delete_Tags_DeleteByCriteria), build
    a hint string that tells the Judge how to disambiguate them by
    suffix semantics. Empty string when no significant overlap.
    """
    by_prefix: dict[str, list[str]] = {}
    for c in candidates:
        tid = c.tool_id
        # Find the common stem (everything up to second underscore or
        # capital-letter boundary). For most v2 registry entries this
        # produces "delete_Tags", "get_Companies", etc.
        parts = tid.split("_")
        if len(parts) >= 2:
            stem = "_".join(parts[:2])
            by_prefix.setdefault(stem, []).append(tid)
    lines: list[str] = []
    for stem, tids in sorted(by_prefix.items()):
        if len(tids) < 2:
            continue
        hints = []
        for tid in tids:
            suffix = tid[len(stem):]
            if suffix == "":
                hints.append(f"  - {tid}: osnovna varijanta")
            elif suffix == "_id":
                hints.append(f"  - {tid}: po ID-u (točno jedan)")
            elif "_DeleteByCriteria" in suffix or "DeleteByCriteria" in suffix:
                hints.append(f"  - {tid}: filtriraj i obriši više")
            elif "_GroupBy" in suffix:
                hints.append(f"  - {tid}: grupiraj rezultate")
            elif "_Agg" in suffix:
                hints.append(f"  - {tid}: agregirana statistika")
            elif "_ProjectTo" in suffix:
                hints.append(f"  - {tid}: vrati podskup polja")
            elif "_multipatch" in suffix:
                hints.append(f"  - {tid}: parcijalno ažuriraj VIŠE")
            elif "_documents" in suffix:
                hints.append(f"  - {tid}: dokument povezan s entitetom")
            else:
                hints.append(f"  - {tid}: {suffix or '(specifičan)'}")
        lines.append(f"{stem}*:")
        lines.extend(hints)
    return "\n".join(lines)


TOP_K_CANDIDATES = int(os.environ.get("V2_TOP_K", "20"))
MIN_RATIONALE_LEN = 30

_QUERY_INTENT_SYSTEM = (
    "Klasificiraj korisnikov upit u JEDNU akciju i kardinalnost. "
    "Odgovori SAMO JSON-om: "
    '{"action": "DOHVAT|DODAVANJE|AŽURIRANJE|BRISANJE|NEPOZNATO", '
    '"cardinality": "JEDAN|VIŠE|NEPOZNATO"}'
)


@dataclass(frozen=True)
class ToolCandidate:
    tool_id: str
    anchor_score: float
    purpose: str = ""
    method: str = ""


@dataclass(frozen=True)
class RecognitionResult:
    tool_id: Optional[str] = None
    flow_name: Optional[str] = None
    params: dict = field(default_factory=dict)
    template_id: Optional[str] = None
    rationale: str = ""
    llm_confidence: float = 0.0
    anchor_score: float = 0.0
    combined_confidence: float = 0.0
    disagreement: bool = False
    error: Optional[str] = None
    candidates: list = field(default_factory=list)  # ToolCandidate list


class RecognitionEngine:
    """Two-stage recognition: per-tool anchor → LLM Judge."""

    def __init__(
        self,
        embedder,
        llm_client,
        deployment_name: str,
        tool_registry,
        *,
        anchor_cache_path: Optional[str] = None,
        # Pre-classify query intent (action + cardinality) and prepend
        # the resulting Croatian markers to the query before embedding.
        # Mirrors the markers we inject into anchor text — closes the
        # cosine gap on adversarial paraphrases that don't say
        # "obriši" but mean "delete one specific". ~$0.0001/query.
        enable_query_intent: bool = False,
        # LLM-driven hierarchical pre-filter — pick top-K services + top-K
        # categories via LLM (gpt-4o-mini is strong at coarse picking,
        # weak at fine 950-way disambiguation). Then dense retrieval
        # operates on the union of tools in those categories.
        # Default OFF until measured. Opt-in via V2_HIER=1 env in
        # the benchmark.
        enable_hierarchy: bool = False,
        hierarchy_service_top_k: int = 2,
        hierarchy_category_top_k: int = 3,
        # LLM-as-retriever: bypass dense retrieval, ask the LLM to
        # pick top candidates directly from the compressed registry
        # catalog. ~$0.003/query, ~5-8s latency. Use when dense
        # retrieval recall is the bottleneck (adversarial paraphrases).
        enable_llm_retrieve: bool = False,
        llm_retrieve_top_n: int = 10,
        # NOTE: enable_hyde, enable_sparse, enable_second_pass,
        # enable_category_prefilter were REMOVED 2026-05-08. All four were
        # empirically measured to REGRESS accuracy on the adversarial
        # paraphrase benchmark (HyDE 13.8%→5.8%, sparse RRF →8.0%,
        # category 23.2%→17.4%, second-pass 21.7%→17.8%). The constructor
        # docstring used to document them as "opt-in for re-measurement";
        # in practice no deploy ever turned them on, and their state +
        # helper methods + branch points added ~210 LOC of complexity for
        # zero production value. Falsification evidence in git history.
        # Self-consistency: run the Judge N times with non-zero temp
        # and majority-vote on tool_id. Closes near-duplicate confusion
        # at 3x Judge cost. N=1 disables.
        judge_self_consistency: int = 1,
        # Entity-level hierarchical retrieval: Stage 1 picks top-N
        # entities from 47 curated descriptions; Stage 2 retrieves tools
        # within those entities only. Empirically targets the 67.8%
        # oracle-entity ceiling. Off by default until benchmark validates.
        enable_entity_hierarchy: bool = False,
        entity_hierarchy_top_k: int = 3,
        entity_hierarchy_per_entity_k: int = 15,
    ):
        self._embedder = embedder
        self._llm = llm_client
        self._deployment = deployment_name
        self._registry = tool_registry
        self._initialized = False
        self._init_lock = asyncio.Lock()
        # Anchor entries: (tool_id, anchor_text, vector)
        self._anchors: list[tuple[str, str, list[float]]] = []
        self._anchor_cache_path = anchor_cache_path
        self._enable_llm_retrieve = enable_llm_retrieve
        self._llm_retrieve_top_n = llm_retrieve_top_n
        self._enable_query_intent = enable_query_intent
        self._enable_hierarchy = enable_hierarchy
        self._hierarchy_service_top_k = max(1, int(hierarchy_service_top_k))
        self._hierarchy_category_top_k = max(1, int(hierarchy_category_top_k))
        self._judge_self_consistency = max(1, int(judge_self_consistency))
        # Entity-level hierarchical retrieval state. Built lazily on
        # first recognize() call when enable_entity_hierarchy is True.
        self._enable_entity_hierarchy = (
            enable_entity_hierarchy
            or os.environ.get("V2_ENTITY_HIERARCHY", "0") == "1"
        )
        # Hybrid mode: keep flat retrieval recall, add entity-match boost
        self._enable_entity_hierarchy_hybrid = (
            os.environ.get("V2_ENTITY_HIERARCHY_HYBRID", "0") == "1"
        )
        # Hybrid implies hierarchy infrastructure must be built.
        if self._enable_entity_hierarchy_hybrid:
            self._enable_entity_hierarchy = True
        self._entity_hierarchy_top_k = max(1, int(entity_hierarchy_top_k))
        self._entity_hierarchy_per_entity_k = max(1, int(entity_hierarchy_per_entity_k))
        self._entity_hybrid_boost = float(
            os.environ.get("V2_ENTITY_HYBRID_BOOST", "0.05")
        )
        # Confusion disambiguator (opt-in): per-cluster negative anchors
        # that explicitly discriminate between confused entity pairs
        # (CostCenters↔Expenses, OrgUnits↔Companies, ...). When the
        # query semantically matches a discriminator signal, candidates
        # whose entity is the favored side get a small boost; opposing
        # entity gets a small demote.
        self._enable_confusion_disambiguator = (
            os.environ.get("V2_DISAMBIGUATOR", "0") == "1"
        )
        self._disambiguator_threshold = float(
            os.environ.get("V2_DISAMBIGUATOR_THRESHOLD", "0.78")
        )
        self._disambiguator_weight = float(
            os.environ.get("V2_DISAMBIGUATOR_WEIGHT", "0.05")
        )
        self._confusion_disambiguator: Optional[ConfusionDisambiguator] = None
        self._entity_index: Optional[EntityIndex] = None
        self._per_entity_tool_index: Optional[PerEntityToolIndex] = None

    async def initialize(self) -> None:
        """Build per-tool anchor index from registry purpose strings.

        Lock-guarded so 50 concurrent recognize() calls hitting an
        un-initialized engine don't all race to embed every anchor.

        If anchor_cache_path was supplied AND the cache fingerprint
        matches the current registry+deployment, load embeddings from
        disk instead of re-embedding (saves ~7 min on a 950-tool corpus).
        """
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            if self._load_anchors_from_cache():
                await self._build_entity_hierarchy_indices()
                self._initialized = True
                logger.info(
                    "recognition engine: %d tool anchors (from cache)%s",
                    len(self._anchors),
                    ", entity-hierarchy ON" if self._enable_entity_hierarchy else "",
                )
                return
            # ONE anchor entry per tool (concatenated enrichment phrases
            # + base description). Empirically: a single long
            # semantically-rich vector beats N short vectors with
            # group-by-tool — the long form averages out phrase noise
            # and lands closer to abstract paraphrases. Measured 9.4%
            # multi-entry vs 20.3% concat on the adversarial benchmark.
            pairs: list[tuple[str, str]] = []
            for tool in self._registry.tools:
                tool_id = self._registry.tool_id_of(tool)
                anchor_text = self._registry.anchor_text_for(tool)
                if anchor_text:
                    pairs.append((tool_id, anchor_text))

            batch_embed = getattr(self._embedder, "embed_batch", None)
            if batch_embed is not None and pairs:
                texts = [p[1] for p in pairs]
                vectors = await batch_embed(texts)
                for (tool_id, anchor_text), v in zip(pairs, vectors):
                    if v is None:
                        continue
                    self._anchors.append((tool_id, anchor_text, v))
            else:
                for tool_id, anchor_text in pairs:
                    v = await self._embedder.embed(anchor_text)
                    if v is None:
                        continue
                    self._anchors.append((tool_id, anchor_text, v))
            await self._build_entity_hierarchy_indices()
            self._initialized = True
            self._save_anchors_to_cache()
            logger.info(
                "recognition engine: %d tool anchors%s",
                len(self._anchors),
                ", entity-hierarchy ON" if self._enable_entity_hierarchy else "",
            )

    async def _build_entity_hierarchy_indices(self) -> None:
        """Build EntityIndex (Stage 1) + PerEntityToolIndex (Stage 2).

        Idempotent — only embeds the 47 entity descriptions once per
        engine instance. Skipped if entity hierarchy is disabled.

        Also builds the ConfusionDisambiguator if its flag is set.
        """
        # Confusion disambiguator (independent flag from entity hierarchy)
        if (
            self._enable_confusion_disambiguator
            and self._confusion_disambiguator is None
        ):
            cd = ConfusionDisambiguator()
            await cd.initialize(self._embedder)
            self._confusion_disambiguator = cd
            logger.info(
                "confusion-disambiguator ready: %d discriminators",
                cd.size(),
            )

        if not self._enable_entity_hierarchy:
            return
        if self._entity_index is not None:
            return
        ei = EntityIndex()
        await ei.initialize(self._embedder)
        ti = PerEntityToolIndex()
        ti.build(self._anchors)
        self._entity_index = ei
        self._per_entity_tool_index = ti
        logger.info(
            "entity-hierarchy indices ready: %d entity vectors, %d total anchors",
            ei.size(), len(self._anchors),
        )

    def _cache_fingerprint(self) -> str:
        """Identity hash for (registry tools + deployment). Mismatch
        invalidates the cache."""
        h = hashlib.sha256()
        h.update(self._deployment.encode("utf-8"))
        for tool in self._registry.tools:
            tid = self._registry.tool_id_of(tool)
            anchor = self._registry.anchor_text_for(tool) or ""
            h.update(f"{tid}\0{anchor}\0".encode("utf-8"))
        return h.hexdigest()

    def _load_anchors_from_cache(self) -> bool:
        if not self._anchor_cache_path:
            return False
        path = Path(self._anchor_cache_path)
        if not path.exists():
            return False
        try:
            with path.open(encoding="utf-8") as f:
                data = _json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("anchor cache unreadable: %s", e)
            return False
        if data.get("fingerprint") != self._cache_fingerprint():
            logger.info("anchor cache stale; rebuilding")
            return False
        self._anchors = [
            (e["tool_id"], e["anchor_text"], e["vector"])
            for e in data.get("entries", [])
        ]
        # Category anchors removed 2026-05-08; old caches with
        # `category_entries` are silently ignored.
        return bool(self._anchors)

    def _save_anchors_to_cache(self) -> None:
        if not self._anchor_cache_path or not self._anchors:
            return
        path = Path(self._anchor_cache_path)
        try:
            os.makedirs(path.parent, exist_ok=True)
            payload = {
                "fingerprint": self._cache_fingerprint(),
                "entries": [
                    {"tool_id": t, "anchor_text": a, "vector": v}
                    for (t, a, v) in self._anchors
                ],
            }
            with path.open("w", encoding="utf-8") as f:
                _json.dump(payload, f)
        except OSError as e:
            logger.warning("anchor cache save failed: %s", e)

    async def recognize(
        self,
        query: str,
        identity_summary: str = "",
        restrict_to: Optional[set[str]] = None,
    ) -> RecognitionResult:
        """Run anchor → LLM Judge. Never raises."""
        if not self._initialized or not self._anchors:
            return RecognitionResult(error="not_initialized")
        if not query or not query.strip():
            return RecognitionResult(error="empty_query")

        # Entity-level hierarchical retrieval — TWO MODES:
        #
        #   1. HARD (V2_ENTITY_HIERARCHY=1, default when enabled):
        #      Stage 1 → top-N entities; Stage 2 → tools within those only.
        #      Empirically REGRESSED (-4pp strict) because Stage 1 errors
        #      remove the right entity entirely. Disabled by default.
        #
        #   2. HYBRID (V2_ENTITY_HIERARCHY_HYBRID=1):
        #      Run flat retrieval (preserves 92% entity recall in top-50).
        #      Compute entity scores Stage-1 style. BOOST candidates
        #      whose entity matches top-3 entity classifier hits — by
        #      a small margin (0.05). This avoids HARD filter's loss
        #      of recall while still rewarding entity alignment.
        hybrid_mode = (
            self._enable_entity_hierarchy_hybrid
            and self._entity_index is not None
        )
        hard_mode = (
            self._enable_entity_hierarchy
            and not hybrid_mode
            and self._entity_index is not None
        )

        if hard_mode:
            candidates = await self._entity_hierarchical_top_k(
                query,
            )
        else:
            # Hierarchical pre-filter (LLM-driven): pick top-K services then
            # top-K categories before dense retrieval. Restricts the 950-tool
            # search space to ~30-200 tools the LLM thinks are likely.
            hierarchy_pool: Optional[set[str]] = None
            if self._enable_hierarchy:
                hierarchy_pool = await self._apply_hierarchy_filter(query)

            # If caller passed an explicit restrict_to set (e.g. V3 hybrid:
            # V3 Stage 1 picks domain → constrain retrieval to that domain's
            # tools), intersect with any hierarchy pool. Empty intersection
            # falls back to no-restriction so we don't crash on misalignment.
            effective_restrict = restrict_to
            if hierarchy_pool is not None and restrict_to is not None:
                effective_restrict = hierarchy_pool & restrict_to
                if not effective_restrict:
                    effective_restrict = restrict_to
            elif hierarchy_pool is not None:
                effective_restrict = hierarchy_pool

            candidates = await self._top_k(
                query, restrict_to=effective_restrict,
            )

            # HYBRID entity-hierarchy boost: keep flat retrieval recall,
            # add a small bonus to candidates whose entity matches the
            # entity-classifier top-K. Gentle re-rank that preserves the
            # right tool in candidate pool when classifier is wrong.
            if hybrid_mode:
                candidates = await self._apply_entity_hybrid_boost(
                    query, candidates,
                )

            # Confusion disambiguator (opt-in, ortogonalan): per-cluster
            # negative anchors. Re-ranks candidates whose entity is on
            # the favored or opposing side of a fired discriminator.
            if self._confusion_disambiguator is not None:
                candidates = await self._apply_confusion_disambiguator(
                    query, candidates,
                )

        # LLM-as-retriever: when enabled, augment dense candidates with
        # tools the LLM picks directly from the full catalog. The two
        # sources are unioned, with LLM-picked tools at the front of
        # the list (they are the best evidence for adversarial
        # paraphrases where dense retrieval misses).
        if self._enable_llm_retrieve:
            llm_picks = await self._llm_retrieve_candidates(
                query, top_n=self._llm_retrieve_top_n,
            )
            if llm_picks:
                existing = {c.tool_id: c for c in candidates}
                merged: list[ToolCandidate] = []
                for tid in llm_picks:
                    if tid in existing:
                        merged.append(existing[tid])
                        existing.pop(tid)
                    elif self._registry.has_tool(tid):
                        merged.append(ToolCandidate(
                            tool_id=tid,
                            anchor_score=0.0,
                            purpose=self._registry.purpose_of(tid),
                            method=self._registry.method_of(tid) or "GET",
                        ))
                merged.extend(existing.values())
                candidates = merged[:TOP_K_CANDIDATES]

        if not candidates:
            return RecognitionResult(error="no_candidates")

        # LLM Judge — optionally self-consistency over N samples
        try:
            if self._judge_self_consistency > 1:
                judge = await self._invoke_llm_judge_self_consistent(
                    query, identity_summary, candidates,
                    n_samples=self._judge_self_consistency,
                )
            else:
                judge = await self._invoke_llm_judge(
                    query, identity_summary, candidates,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM judge failed: %s", e)
            return RecognitionResult(
                error=f"judge_error:{type(e).__name__}",
                candidates=candidates,
            )

        # Validate tool against registry whitelist
        picked_tool = judge.get("tool_id")
        flow_name = judge.get("flow_name")
        if picked_tool:
            # Strip "[METHOD]" / "[GET]" / etc. that the Judge sometimes
            # echoes from the candidate-list format string.
            picked_tool = picked_tool.split(" ")[0].strip()
            if not self._registry.has_tool(picked_tool):
                return RecognitionResult(
                    error=f"invalid_tool:{picked_tool}",
                    candidates=candidates,
                )

        anchor_top_id = candidates[0].tool_id
        first_pass_conf = float(judge.get("confidence") or 0.0)
        first_pass_conf = max(0.0, min(1.0, first_pass_conf))
        rationale = (judge.get("rationale") or "").strip()

        # Second-pass disambiguation removed 2026-05-08 — empirically
        # regressed accuracy (21.7% → 17.8%); see git history.

        disagreement = bool(picked_tool and picked_tool != anchor_top_id)
        if len(rationale) < MIN_RATIONALE_LEN:
            llm_conf = min(first_pass_conf, 0.5)
        else:
            llm_conf = first_pass_conf
        llm_conf = max(0.0, min(1.0, llm_conf))

        anchor_score = candidates[0].anchor_score
        combined = 0.6 * anchor_score + 0.4 * llm_conf
        if disagreement:
            combined *= 0.7  # downgrade on disagreement

        return RecognitionResult(
            tool_id=picked_tool,
            flow_name=flow_name,
            params=judge.get("params") or {},
            template_id=judge.get("template_id"),
            rationale=rationale,
            llm_confidence=llm_conf,
            anchor_score=anchor_score,
            combined_confidence=combined,
            disagreement=disagreement,
            candidates=candidates,
        )

    # ---- internals -------------------------------------------------------

    async def _apply_hierarchy_filter(
        self, query: str,
    ) -> Optional[set[str]]:
        """LLM picks top-K services then top-K categories within them.
        Returns the set of tool_ids to restrict dense retrieval to,
        or None on any failure (caller falls back to full pool)."""
        # Step 1: services
        services = await classify_service(
            self._llm_chat, self._deployment, query,
            top_k=self._hierarchy_service_top_k,
        )
        if not services:
            return None  # fail-open

        # Collect categories that exist in those services
        cats = getattr(self._registry, "categories", {}) or {}
        t2c = getattr(self._registry, "_tool_to_category", {})
        # Build {category_name: service} via majority membership
        cat_in_service: dict[str, str] = {}
        for tool in self._registry.tools:
            tid = self._registry.tool_id_of(tool)
            cat = t2c.get(tid)
            if cat and cat not in cat_in_service:
                cat_in_service[cat] = tool.get("service_name", "")
        cats_in_picked_services: list[tuple[str, str]] = []
        for cat_name, cat_data in cats.items():
            if cat_in_service.get(cat_name) in services:
                desc = (
                    (cat_data or {}).get("description_hr")
                    or (cat_data or {}).get("description_en")
                    or cat_name.replace("_", " ")
                )
                cats_in_picked_services.append((cat_name, desc))

        if not cats_in_picked_services:
            return None  # nothing to narrow on; fail-open

        # Step 2: categories
        picked_categories = await classify_category(
            self._llm_chat, self._deployment, query,
            cats_in_picked_services,
            top_k=self._hierarchy_category_top_k,
        )
        if not picked_categories:
            return None  # fail-open

        # Step 3: collect tools in picked categories
        getter = getattr(self._registry, "tools_in_categories", None)
        if getter is None:
            return None
        pool = getter(picked_categories)
        return pool if pool else None

    async def _apply_confusion_disambiguator(
        self, query: str, candidates: list[ToolCandidate],
    ) -> list[ToolCandidate]:
        """Re-rank using per-cluster negative-anchor discriminators.

        Computes query embedding once, scores it against all
        discriminator vectors. Candidates whose entity is favored by a
        fired discriminator get a positive boost; opposing entities get
        a negative bias. Recall is preserved (no candidate is dropped).
        """
        if self._confusion_disambiguator is None or not candidates:
            return candidates
        q_vec = await self._embedder.embed(query)
        if q_vec is None:
            return candidates
        bias = self._confusion_disambiguator.score_query(
            q_vec, threshold=self._disambiguator_threshold,
        )
        if not bias:
            return candidates
        return apply_disambiguator_bias(
            candidates, bias, bias_weight=self._disambiguator_weight,
        )

    async def _apply_entity_hybrid_boost(
        self, query: str, candidates: list[ToolCandidate],
    ) -> list[ToolCandidate]:
        """Soft-boost candidates whose entity matches Stage-1 entity classifier.

        Preserves recall (no candidate is dropped). Re-orders so that
        candidates whose entity matches top-3 entity-classifier hits get
        a small score bonus, then re-sorts.
        """
        if self._entity_index is None or not candidates:
            return candidates
        q_vec = await self._embedder.embed(query)
        if q_vec is None:
            return candidates
        from services.v2.hierarchical_entity_retrieval import (
            extract_entity_from_tool_id,
        )
        hits = self._entity_index.classify(
            q_vec, top_k=self._entity_hierarchy_top_k,
        )
        if not hits:
            return candidates
        boosted_entities = {h.entity for h in hits}
        # Re-score and re-sort; preserve original order on ties.
        rescored: list[tuple[float, int, ToolCandidate]] = []
        for i, c in enumerate(candidates):
            ent = extract_entity_from_tool_id(c.tool_id)
            bonus = self._entity_hybrid_boost if ent in boosted_entities else 0.0
            rescored.append((-(c.anchor_score + bonus), i, c))
        rescored.sort()
        return [c for _, _, c in rescored]

    async def _entity_hierarchical_top_k(
        self, query: str,
    ) -> list[ToolCandidate]:
        """Stage-1+Stage-2 retrieval: entity classification, then per-entity tools.

        Replaces flat dense retrieval. The judge sees a smaller, entity-coherent
        candidate list which empirically should reduce confusion-cluster errors.
        """
        if self._entity_index is None or self._per_entity_tool_index is None:
            return []
        q_vec = await self._embedder.embed(query)
        if q_vec is None:
            return []

        hits = hierarchical_retrieve(
            query_vector=q_vec,
            entity_index=self._entity_index,
            tool_index=self._per_entity_tool_index,
            entity_top_k=self._entity_hierarchy_top_k,
            per_entity_tool_top_k=self._entity_hierarchy_per_entity_k,
            final_top_k=TOP_K_CANDIDATES,
        )

        candidates: list[ToolCandidate] = []
        for h in hits:
            if not self._registry.has_tool(h.tool_id):
                continue
            candidates.append(ToolCandidate(
                tool_id=h.tool_id,
                anchor_score=h.combined_score,
                purpose=self._registry.purpose_of(h.tool_id),
                method=self._registry.method_of(h.tool_id) or "GET",
            ))
        return candidates

    async def _top_k(
        self, query: str,
        restrict_to: Optional[set[str]] = None,
    ) -> list[ToolCandidate]:
        # Up to FOUR separate ranking signals fused via reciprocal-rank
        # fusion (RRF). Each signal contributes its own rank; RRF is
        # robust to score-scale and prevents one bad signal (e.g. a
        # confidently-wrong HyDE expansion) from dominating.
        #   - dense_raw  : cosine(embed(query), anchor)
        #   - dense_hyde : cosine(embed(HyDE(query)), anchor)
        #   - sparse_raw : BM25(query, anchor)
        #   - sparse_hyde: BM25(HyDE(query), anchor)
        # The reported anchor_score is dense_raw alone — keeps the L5
        # confidence-gate thresholds in their original semantic meaning.
        # Optionally pre-classify query intent into Croatian markers.
        # Markers prepended to query embed it into the same action-aware
        # subspace as anchors built with action markers — closes the
        # cosine gap on abstract paraphrases.
        intent_markers = await self._classify_query_intent(query)
        query_for_embed = (
            f"{intent_markers} {query}" if intent_markers else query
        )

        # Single-shot dense retrieval. HyDE expansion + BM25 sparse +
        # category prefilter were all REMOVED 2026-05-08 after empirical
        # benchmark showed all three REGRESSED accuracy on the production
        # adversarial paraphrase corpus. See git log for falsification
        # evidence; do not re-introduce without re-measuring.
        try:
            q_raw = await self._embedder.embed(query_for_embed)
        except Exception as e:  # noqa: BLE001
            logger.warning("primary embed failed: %s", e)
            return []
        if q_raw is None:
            return []

        n = len(self._anchors)

        def _allowed(i: int) -> bool:
            if restrict_to is None:
                return True
            return self._anchors[i][0] in restrict_to

        cos_raw = [
            cosine_similarity(q_raw, self._anchors[i][2])
            if _allowed(i) else 0.0
            for i in range(n)
        ]

        rankings = [sorted(range(n), key=lambda i: cos_raw[i], reverse=True)]
        fused = reciprocal_rank_fusion(rankings)

        scored: list[ToolCandidate] = []
        for idx, _rrf_score in fused:
            if len(scored) >= TOP_K_CANDIDATES:
                break
            tool_id, _anchor, _vec = self._anchors[idx]
            scored.append(ToolCandidate(
                tool_id=tool_id,
                anchor_score=cos_raw[idx],
                purpose=self._registry.purpose_of(tool_id),
                method=self._registry.method_of(tool_id),
            ))
        return scored

    async def _classify_query_intent(self, query: str) -> str:
        """Tiny LLM micro-call → Croatian action/cardinality markers
        prepended to query before embedding. Returns "" on disabled
        flag or any LLM failure (caller falls back to raw query)."""
        if not self._enable_query_intent:
            return ""
        try:
            response = await self._llm_chat(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": _QUERY_INTENT_SYSTEM},
                    {"role": "user", "content": query},
                ],
                temperature=0,
                max_tokens=40,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            parsed = parse_llm_json(raw)
            if not parsed:
                return ""
            action = (parsed.get("action") or "").upper()
            cardinality = (parsed.get("cardinality") or "").upper()
            valid_actions = {
                "DOHVAT", "DODAVANJE", "AŽURIRANJE", "BRISANJE",
            }
            valid_cards = {"JEDAN", "VIŠE"}
            parts = []
            if action in valid_actions:
                parts.append(action)
            if cardinality in valid_cards:
                parts.append(cardinality)
            return " ".join(parts)
        except Exception as e:  # noqa: BLE001
            logger.warning("query intent classify failed: %s", e)
            return ""

    async def _llm_chat(self, **kwargs):
        """Wraps LLM chat completion with retry on transient Azure
        errors (RateLimitError, APIConnectionError). Linear backoff so
        a single 429 doesn't poison a whole trial. All recognition
        layers go through this wrapper."""
        last_err = None
        for attempt in range(4):
            try:
                return await self._llm.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                last_err = e
                etype = type(e).__name__
                if "RateLimit" in etype or "APIConnection" in etype \
                        or "Timeout" in etype:
                    await asyncio.sleep(2 ** attempt + 1)
                    continue
                raise
        raise last_err if last_err else RuntimeError("LLM call failed")

    @staticmethod
    def _extract_entity_from_id(tool_id: str) -> str:
        m = re.match(r"^(?:get|post|put|patch|delete)_([A-Z][A-Za-z0-9]+)", tool_id or "")
        return m.group(1) if m else "Other"

    def _format_candidates_grouped(self, candidates: list) -> str:
        """Group candidates by entity, preserving anchor rank within group.

        Output format:
            ENTITY: VehicleCalendar (3 tools)
              - get_VehicleCalendar [GET] — list reservations
              - delete_VehicleCalendar_id [DELETE] — cancel one
              - delete_VehicleCalendar_DeleteByCriteria [DELETE] — bulk
            ENTITY: MileageReports (2 tools)
              - ...
        """
        from collections import OrderedDict
        groups = OrderedDict()
        for c in candidates:
            ent = self._extract_entity_from_id(c.tool_id)
            groups.setdefault(ent, []).append(c)
        lines = []
        for ent, members in groups.items():
            lines.append(f"ENTITY: {ent} ({len(members)} {'tool' if len(members)==1 else 'tools'})")
            for c in members:
                lines.append(f"  - {c.tool_id} [{c.method}] — {c.purpose}")
        return "\n".join(lines)

    async def _invoke_llm_judge(
        self,
        query: str,
        identity_summary: str,
        candidates: list[ToolCandidate],
        temperature: float = 0.0,
    ) -> dict:
        # Optionally group candidates by entity (V2_GROUPED_JUDGE=1).
        # This gives the Judge structural information about the candidate
        # space — first decide entity, then operation/subresource within entity.
        # The flat list of 50 similar tool IDs is overwhelming; grouping
        # reveals the (entity, operation, modifier) decomposition implicitly.
        grouped = os.environ.get("V2_GROUPED_JUDGE", "0") == "1"
        if grouped:
            cands_text = self._format_candidates_grouped(candidates)
        else:
            # Full purpose (not truncated) — at 20 candidates × ~250 chars
            # the prompt is ~5K chars, well within context budget. Truncating
            # was throwing away exactly the signal the Judge needs.
            cands_text = "\n".join(
                f"{i+1}. {c.tool_id} [{c.method}] — {c.purpose}"
                for i, c in enumerate(candidates)
            )

        # Find shared-prefix groups so we can explicitly remind the Judge
        # to disambiguate them. E.g. {delete_Tags, delete_Tags_id,
        # delete_Tags_DeleteByCriteria} means user wants 1 (DELETE all),
        # 1 (DELETE one specific), or N (DELETE matching criteria).
        prefix_groups = _shared_prefix_hint(candidates)
        if grouped:
            cands_header = (
                f"Kandidati grupirani po entitetu (top {len(candidates)} "
                f"semantic-rangirani, raspoređeni po entitetu radi lakše "
                f"disambiguacije):"
            )
            postupak_a = (
                " a) PRVO odaberi ENTITET (vidi 'ENTITY: X' headere). "
                "Koja entity grupa najbolje opisuje o čemu korisnik priča "
                "(npr. CostCenters vs Expenses vs OrgUnits)?"
            )
        else:
            cands_header = f"Kandidati (top {len(candidates)}, već semantic-rangirani):"
            postupak_a = (
                " a) Identificiraj što korisnik STVARNO želi: AKCIJA "
                "(GET/POST/PUT/PATCH/DELETE) + ENTITET (vozilo, vozač, "
                "rezervacija, ...) + KARDINALNOST (jedan vs više vs "
                "filtriraj)."
            )
        prompt = (
            "Tvoj GLAVNI zadatak: odaberi backend endpoint koji najbolje "
            "rješava korisnikov upit, IZ LISTE KANDIDATA dolje.\n\n"
            f"Identity: {identity_summary or '(unknown user)'}\n\n"
            f"Korisnikov upit:\n\"\"\"\n{query}\n\"\"\"\n\n"
            f"{cands_header}\n"
            f"{cands_text}\n\n"
            + (f"Pažnja — slični kandidati po imenu:\n{prefix_groups}\n\n"
               if prefix_groups else "")
            + "Postupak (mentalno):\n"
            f"{postupak_a}\n"
            " b) Eliminiraj kandidate koji rade pogrešnu akciju ili "
            "pogrešnu kardinalnost (npr. _DeleteByCriteria = obriši "
            "VIŠE; _id = obriši JEDAN; bez sufiksa = osnovna varijanta).\n"
            " c) Između preostalih, odaberi onog čiji opis najpreciznije "
            "pokriva entitet.\n\n"
            "Pravila:\n"
            " 1. Odaberi TOČNO JEDAN tool_id IZ LISTE iznad. Vrati ČISTI "
            "tool_id BEZ '[METHOD]' suffix-a.\n"
            " 2. rationale (min 30 znakova) — JEDNA rečenica koja "
            "objašnjava zašto baš taj kandidat (uključujući zašto NE "
            "neki srodni).\n"
            " 3. confidence: 0.95 ako si siguran; 0.6-0.8 ako su 2-3 "
            "kandidata blizu; <0.5 ako nijedan ne pristaje.\n"
            " 4. params/template_id: ostavi prazno ({} / null) ako nisi "
            "siguran. Ne pogađaj vrijednosti — tool_id je presudan.\n\n"
            'Odgovori SAMO JSON-om: {"tool_id": "...", "flow_name": null, '
            '"params": {}, "template_id": null, "rationale": "...", '
            '"confidence": 0.95}'
        )
        response = await self._llm_chat(
            model=self._deployment,
            messages=[
                {"role": "system",
                 "content": (
                     "Ti si stručnjak za mapiranje apstraktnih korisnikih "
                     "upita na konkretne backend endpointe u Croatian fleet "
                     "management sustavu. Kad je upit apstraktan (npr. "
                     "'trebam riješiti tu stavku'), prepoznaš podrazumijevanu "
                     "AKCIJU (Create/Read/Update/Delete), ENTITET (vozilo, "
                     "vozač, mjesto troška, dokument, ugovor, …) i "
                     "KARDINALNOST (jedan vs više vs filtriraj) prije "
                     "odabira kandidata. Najčešća greška: birati pregenerični "
                     "endpoint kad bi specifični (po ID-u, by-criteria) "
                     "bolje pristajao. Razmisli korak po korak. Odgovor "
                     "STRIKTNO JSON."
                 )},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        parsed = parse_llm_json(raw)
        if parsed is None:
            raise ValueError("LLM judge returned unparseable JSON")
        return parsed

    async def _invoke_llm_judge_self_consistent(
        self,
        query: str,
        identity_summary: str,
        candidates: list[ToolCandidate],
        n_samples: int,
        temperatures: tuple[float, ...] = (0.0, 0.3, 0.5),
    ) -> dict:
        """Run Judge multiple times with varying temperature, then
        majority-vote on tool_id. Ties broken by highest individual
        confidence. Returns the WINNING Judge response (the one whose
        tool_id matches the majority).

        For close-call disambiguation among similar tools, single-shot
        Judge can pick wrong on a coin-flip; self-consistency suppresses
        the noise by demanding agreement across runs.
        """
        # Build samples concurrently
        temps = list(temperatures[:n_samples]) or [0.0]
        while len(temps) < n_samples:
            temps.append(temps[-1])
        results = await asyncio.gather(
            *(
                self._invoke_llm_judge(
                    query, identity_summary, candidates, temperature=t,
                )
                for t in temps
            ),
            return_exceptions=True,
        )
        valid = [r for r in results if isinstance(r, dict)]
        if not valid:
            # All errored — re-raise the first
            for r in results:
                if isinstance(r, Exception):
                    raise r
            return {}

        # Tally tool_id votes (strip [METHOD] suffix, lowercase-equal)
        from collections import Counter
        votes: Counter = Counter()
        for r in valid:
            tid = (r.get("tool_id") or "").split(" ")[0].strip()
            if tid:
                votes[tid] += 1
        if not votes:
            return valid[0]

        # Pick winner: highest vote count, then highest avg confidence
        max_votes = max(votes.values())
        top_tool_ids = [t for t, v in votes.items() if v == max_votes]
        if len(top_tool_ids) == 1:
            winner_id = top_tool_ids[0]
        else:
            # Tie — pick the one with the highest single confidence
            best_conf = -1.0
            winner_id = top_tool_ids[0]
            for tid in top_tool_ids:
                for r in valid:
                    rid = (r.get("tool_id") or "").split(" ")[0].strip()
                    if rid != tid:
                        continue
                    try:
                        c = float(r.get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        c = 0.0
                    if c > best_conf:
                        best_conf = c
                        winner_id = tid

        # Return the response object that picked the winner (with the
        # highest confidence among ties on the same tool_id).
        winner = max(
            (r for r in valid
             if (r.get("tool_id") or "").split(" ")[0].strip() == winner_id),
            key=lambda r: float(r.get("confidence") or 0.0),
            default=valid[0],
        )
        return winner

    async def _llm_retrieve_candidates(
        self, query: str, top_n: int = 10,
    ) -> list[str]:
        """Use the LLM to retrieve candidates DIRECTLY from the full
        registry catalog. Bypasses dense retrieval entirely — good for
        adversarial paraphrases where ada-002 cosine fails.

        Returns up to `top_n` tool_ids the LLM thinks best match the
        query, drawn from the 950-tool catalog. Empty list on any
        failure (caller falls back to dense retrieval).
        """
        # Compressed catalog: tool_id + first sentence of purpose.
        # Aggressive truncation (40 chars) keeps the prompt under 12K
        # tokens — staying under per-request limits and reducing TPM
        # pressure when the benchmark runs many trials sequentially.
        catalog_lines = []
        for tool in self._registry.tools:
            tid = self._registry.tool_id_of(tool)
            purpose = self._registry.purpose_of(tid) or ""
            short = purpose.split(". ")[0][:40]
            catalog_lines.append(f"{tid}|{short}")
        catalog = "\n".join(catalog_lines)

        prompt = (
            "Imaš katalog backend endpointova fleet management bota "
            "(po jedan po retku, format `tool_id|kratki opis`):\n\n"
            f"{catalog}\n\n"
            f"Korisnikov upit:\n\"\"\"\n{query}\n\"\"\"\n\n"
            f"Vrati JSON s NAJVIŠE {top_n} tool_id-eva iz kataloga "
            "iznad, sortiranih po relevantnosti. Ako nijedan ne odgovara, "
            'vrati prazan niz. Format: {"tools": ["tool_id_1", ...]}'
        )
        try:
            response = await self._llm_chat(
                model=self._deployment,
                messages=[
                    {"role": "system", "content":
                     "Ti si retrieval modul. Odabireš tool_id iz "
                     "kataloga koji najbolje odgovaraju upitu. JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            parsed = parse_llm_json(raw)
            if not parsed:
                return []
            tools = parsed.get("tools") or []
            return [t for t in tools if isinstance(t, str)
                    and self._registry.has_tool(t)][:top_n]
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM retrieve failed: %s", e)
            return []

