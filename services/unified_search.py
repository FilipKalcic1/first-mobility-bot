"""Unified Search — single entry point for all tool discovery."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any, TYPE_CHECKING

from tool_routing import PRIMARY_ACTION_TOOLS as _PRIMARY_ACTION_TOOLS

from services.types.action_intent import detect_action_intent, ActionIntent
from services.text_normalizer import normalize_diacritics
from services.faiss_vector_store import get_faiss_store, SearchResult
from services.entity_detector import detect_entity, detect_possessive, detect_user_profile_query
from services.types.query_type import QueryType
from services.dynamic_threshold import (
    get_engine as _get_engine, DecisionEngine,
)

from services.search.config import (
    VERB_METHOD_MAP as _VERB_METHOD_MAP,
    METHOD_VERBS as _METHOD_VERBS,
    SUFFIX_DESCRIPTIONS as _SUFFIX_DESCRIPTIONS,
    BM25_WEIGHT as _BM25_WEIGHT,
)
from services.search.boost_engine import (
    BoostContext,
    apply_boosts as _apply_boosts_fn,
)
from services.tracing import get_tracer, trace_span
from services.errors import SearchError, ErrorCode

if TYPE_CHECKING:
    from services.registry import ToolRegistry

logger = logging.getLogger(__name__)
_tracer = get_tracer("unified_search")


@dataclass
class UnifiedSearchResult:
    """Result from unified search."""
    tool_id: str
    score: float  # Final score after all boosts
    method: str   # HTTP method
    description: str  # Tool description
    origin_guide: Dict[str, str] = field(default_factory=dict)  # Parameter origin guide
    boosts_applied: list = field(default_factory=list)  # Debug: (name, additive_delta, score_after) tuples
    base_score: float = 0.0  # Debug: score before boosts


@dataclass
class UnifiedSearchResponse:
    """Complete response from unified search."""
    results: List[UnifiedSearchResult]
    intent: ActionIntent
    intent_confidence: float
    query: str
    total_candidates: int  # Before filtering
    detected_entity: Optional[str] = None  # Surfaced so callers don't re-run detect_entity


# FAISS candidate pool sizing
_POOL_SIZE_MIN = 80           # Floor for regular queries
_POOL_SIZE_MULT = 3           # top_k × this, clamped by MIN

# Exact-match injection score — must exceed any possible FAISS+boost total.
_EXACT_MATCH_SCORE = 15.0

class UnifiedSearch:
    """Single interface for tool discovery."""

    def __init__(self, registry: Optional["ToolRegistry"] = None) -> None:
        """Initialize unified search."""
        self._registry = registry
        self._tool_documentation: Optional[Dict] = None
        self._tool_categories: Optional[Dict] = None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def set_registry(self, registry: "ToolRegistry") -> None:
        """Set tool registry (allows late binding)."""
        self._registry = registry

    async def initialize(self) -> None:
        """Load all configuration files."""
        if self._initialized:
            return

        async with self._init_lock:
            # Double-check after acquiring lock (another coroutine may have finished init)
            if self._initialized:
                return

            config_dir = Path(__file__).parent.parent / "config"

            # Load tool documentation — shared cache (single source of truth)
            from services.schema_sanitizer import get_tool_documentation
            self._tool_documentation = get_tool_documentation()
            if self._tool_documentation:
                logger.info(f"Loaded {len(self._tool_documentation)} tool docs")

            # Load tool categories
            cat_path = config_dir / "tool_categories.json"
            try:
                with open(cat_path, 'r', encoding='utf-8') as f:
                    self._tool_categories = json.load(f)
                logger.info("Loaded tool categories")
            except FileNotFoundError:
                # Missing categories file silently degrades ranking; surface
                # loudly so prod misconfigs don't get masked.
                logger.error(
                    f"tool_categories.json missing at {cat_path} — ranking will degrade"
                )
                self._tool_categories = {}
            except Exception as e:
                err = SearchError(ErrorCode.TOOL_DOCS_NOT_LOADED, f"Failed to load tool categories: {e}")
                logger.error(str(err), exc_info=True)
                self._tool_categories = {}

            # Pre-compute duplicate purposes for entity-specific description generation
            self._duplicate_purposes: set = set()
            if self._tool_documentation:
                from collections import Counter
                purpose_counts = Counter(
                    doc.get("purpose", "").strip()
                    for doc in self._tool_documentation.values()
                    if doc.get("purpose", "").strip()
                )
                self._duplicate_purposes = {p for p, c in purpose_counts.items() if c > 1}
                if self._duplicate_purposes:
                    logger.info(f"Detected {len(self._duplicate_purposes)} duplicate purposes across tools")

            # Pre-compute exact match lookup: normalized_query → tool_id (O(1) per search)
            self._exact_match_index: Dict[str, str] = {}
            if self._tool_documentation:
                for tool_id, doc in self._tool_documentation.items():
                    for example in doc.get("example_queries_hr", []):
                        # Normalize diacritics to match search path normalization
                        key = normalize_diacritics(example.lower().strip().rstrip('.'))
                        # First tool wins for duplicate queries
                        if key not in self._exact_match_index:
                            self._exact_match_index[key] = tool_id
                if self._exact_match_index:
                    logger.info(f"Built exact match index: {len(self._exact_match_index)} entries")

            # Load auto-generated entity stems from tool_documentation.json
            if self._tool_documentation:
                from services.entity_detector import load_auto_stems
                load_auto_stems(self._tool_documentation)

            # Build Tool Family Index from registry
            if self._registry:
                from services.tool_family_index import get_family_index
                family_index = get_family_index()
                # registry.tools is a dict keyed by tool_id
                tool_ids = list(self._registry.tools.keys()) if isinstance(self._registry.tools, dict) else [t.tool_id for t in self._registry.tools if hasattr(t, 'tool_id')]
                if tool_ids:
                    family_index.build(tool_ids)

            # Build BM25 index from tool documentation
            if self._tool_documentation:
                from services.bm25_index import get_bm25_index
                self._bm25_index = get_bm25_index()
                self._bm25_index.build(self._tool_documentation)
            else:
                self._bm25_index = None

            self._initialized = True

    async def search(
        self,
        query: str,
        top_k: int = 20
    ) -> UnifiedSearchResponse:
        """
        Unified search entry point.

        All routing paths should call this method.

        Args:
            query: User query in Croatian
            top_k: Number of results to return

        Returns:
            UnifiedSearchResponse with ranked results
        """
        await self.initialize()

        with trace_span(_tracer, "search.unified", {
            "search.top_k": top_k,
            "query.preview": query[:80],
        }) as span:
            # Step 1: ACTION INTENT DETECTION
            intent_result = detect_action_intent(query)

            # Only filter by intent if confidence is very high
            # Otherwise, show all tools and let LLM decide
            # This prevents filtering out valid tools when ML is uncertain
            _engine = _get_engine()
            _intent_signal = intent_result.signal  # Full distribution signal
            action_filter = (
                intent_result.intent.value
                if intent_result.intent != ActionIntent.UNKNOWN
                and _engine.decide(_intent_signal, DecisionEngine.INTENT_FILTER).is_accept
                else None  # Don't filter - show all tools, let LLM decide
            )

            # Step 2: QUERY TYPE — ML classifier removed (Zlatni Put v1). LLM decides downstream.
            logger.info(
                f"Intent={intent_result.intent.value} "
                f"(conf={intent_result.confidence:.2f}), "
                f"ActionFilter={'YES: ' + action_filter if action_filter else 'NO - LLM decides'}"
            )

            # Step 2.5: EXACT MATCH DETECTION (O(1) lookup via pre-computed index)
            query_normalized = normalize_diacritics(query.lower().strip().rstrip('.'))
            exact_match_tool = self._exact_match_index.get(query_normalized)
            if exact_match_tool:
                logger.info(f"UnifiedSearch: EXACT MATCH found: {exact_match_tool}")

            # Step 2.7: ENTITY DETECTION (early, for TFI override)
            detected_entity = detect_entity(query)

            # Step 2.7a: POSSESSIVE SIGNAL — "moj auto" vs "sva vozila"
            is_possessive, poss_entity = detect_possessive(query)
            if is_possessive and poss_entity and not detected_entity:
                detected_entity = poss_entity

            # Step 2.7b: USER PROFILE QUERY — "koji je moj broj telefona?"
            is_user_profile = detect_user_profile_query(query)

            # SHORT QUERY OVERRIDE + MUTATION CORRECTION
            query_lower = query.lower()
            query_lower_norm = normalize_diacritics(query_lower)
            effective_query_type = QueryType.UNKNOWN

            # Verb-based method detection — more reliable than ML for common Croatian verbs
            detected_method = None
            for verb, method in _VERB_METHOD_MAP.items():
                if verb in query_lower_norm:
                    detected_method = method
                    break

            is_mutation = (
                (intent_result.intent in (ActionIntent.DELETE, ActionIntent.UPDATE, ActionIntent.CREATE)
                 and not _engine.decide(_intent_signal, DecisionEngine.MUTATION).is_defer)
                or (detected_method in ("delete", "put", "patch", "post"))
            )

            # Entity + no-mutation → LIST. "radnik" → persons+LIST → get_Persons.
            # Mutations (delete/post/put) keep UNKNOWN so TFI skips (mutation routing
            # picks the id/criteria variant via method-qualified family).
            if detected_entity and not is_mutation:
                effective_query_type = QueryType.LIST

            # Possessive ("moj auto") → SINGLE_ENTITY, not LIST.
            if is_possessive and effective_query_type == QueryType.LIST:
                effective_query_type = QueryType.SINGLE_ENTITY

            # Step 2.7c: USER PROFILE FLAG (defence-in-depth for ML fallback)
            # Profile tools are injected into results after boost pipeline (Step 4.7).

            # Step 2.8: TFI HARD OVERRIDE — deterministic path (~70% of queries)
            # When entity + queryType are both known, skip FAISS entirely.
            # Return the whole entity family so LLM has context for final pick.
            # NOTE: Only fires for substring-detected entities (high confidence).
            from services.tool_family_index import get_family_index
            family_index = get_family_index()
            tfi_override_result = None

            # Bypass TFI when query carries analytical/structural intent
            # (agg/groupby/projectto/lookup/thumb/setasdefault/multipatch) — TFI would
            # collapse these to plain-list tools and hide the correct variant.
            from services.search.config import SUFFIX_INTENT_BOOST as _SIB, LOOKUP_INTENT_BOOST as _LIB
            _suffix_intent_hit = any(
                s in query_lower_norm
                for _stems, _ in _SIB.values()
                for s in _stems
            ) or any(s in query_lower_norm for s in _LIB[0])

            if detected_entity and effective_query_type != QueryType.UNKNOWN and not _suffix_intent_hit:
                # Prefer verb-detected method over ML intent (more reliable for Croatian)
                tfi_method = detected_method or (
                    intent_result.intent.value.lower()
                    if intent_result.intent not in (ActionIntent.UNKNOWN, ActionIntent.NONE)
                    and not _engine.decide(_intent_signal, DecisionEngine.MUTATION).is_defer
                    else None
                )
                tfi_tool = family_index.resolve(
                    detected_entity, effective_query_type,
                    method=tfi_method,
                )
                family_tools = family_index.get_family_tools(detected_entity)

                if tfi_tool and family_tools:
                    # Build results from entire family, TFI match gets highest score
                    family_results = []
                    seen_tools = set()

                    for _variant, tool_id in family_tools.items():
                        score = 2.0 if tool_id.lower() == tfi_tool.lower() else 1.0
                        parts = tool_id.split("_")
                        method = parts[0].upper() if parts else "GET"
                        family_results.append(SearchResult(
                            tool_id=tool_id,
                            score=score,
                            method=method
                        ))
                        seen_tools.add(tool_id.lower())

                    # Inject TFI resolved tool if not in GET-preferred family
                    # (e.g., delete_Expenses_id won't be in simple family)
                    if tfi_tool.lower() not in seen_tools:
                        parts = tfi_tool.split("_")
                        family_results.append(SearchResult(
                            tool_id=tfi_tool,
                            score=2.0,
                            method=parts[0].upper() if parts else "GET"
                        ))

                    # Also inject exact match if found
                    if exact_match_tool:
                        found_exact = any(r.tool_id == exact_match_tool for r in family_results)
                        if not found_exact:
                            parts = exact_match_tool.split("_")
                            family_results.append(SearchResult(
                                tool_id=exact_match_tool,
                                score=_EXACT_MATCH_SCORE,
                                method=parts[0].upper() if parts else "GET"
                            ))
                        else:
                            for r in family_results:
                                if r.tool_id == exact_match_tool:
                                    r.score = _EXACT_MATCH_SCORE

                    family_results.sort(key=lambda r: r.score, reverse=True)

                    tfi_override_result = family_results
                    logger.info(
                        f"UnifiedSearch TFI OVERRIDE: entity={detected_entity}, "
                        f"queryType={effective_query_type.value}, "
                        f"resolved={tfi_tool}, family_size={len(family_results)}"
                    )

            # Step 3: FAISS search (skipped if TFI override succeeded)
            if tfi_override_result is not None:
                faiss_results = tfi_override_result
                total_candidates = len(faiss_results)
                # Skip boost pipeline — TFI results are already scored
                boosted_results = faiss_results
            else:
                faiss_store = get_faiss_store()

                if not faiss_store.is_initialized():
                    logger.warning("UnifiedSearch: FAISS not initialized, returning empty results")
                    return UnifiedSearchResponse(
                        results=[],
                        intent=intent_result.intent,
                        intent_confidence=intent_result.confidence,
                        query=query,
                        total_candidates=0,
                        detected_entity=detected_entity,
                    )

                # Specialized-suffix pool-widening was gated on the removed ML classifier.
                pool_size = max(top_k * _POOL_SIZE_MULT, _POOL_SIZE_MIN)

                # Search 1: BASE query (always)
                faiss_results = await faiss_store.search(
                    query=query,
                    top_k=pool_size,
                    action_filter=action_filter,
                    entity_filter=detected_entity,
                    auto_detect_entity=False,  # Entity already detected above
                )

                total_candidates = len(faiss_results)

            if not faiss_results:
                logger.info("UnifiedSearch: FAISS returned no results")
                return UnifiedSearchResponse(
                    results=[],
                    intent=intent_result.intent,
                    intent_confidence=intent_result.confidence,
                    query=query,
                    total_candidates=0,
                    detected_entity=detected_entity,
                )

            # Step 4: Apply boosts (FAISS path only; TFI path is already scored)
            # Boosts + BM25 are entity-gated: without a detected entity they inject
            # noise (raw FAISS beats boosted FAISS on ambiguous queries).
            if tfi_override_result is None:
                if detected_entity:
                    # Guard the entire boost+BM25 stage so any bug falls back to
                    # raw FAISS ordering rather than failing the whole search.
                    try:
                        boosted_results = self._apply_boosts(
                            query, faiss_results,
                            detected_entity=detected_entity,
                            effective_query_type=effective_query_type,
                            is_possessive=is_possessive,
                            detected_method=detected_method,
                        )

                        if self._bm25_index and self._bm25_index.is_built:
                            bm25_tool_ids = [r.tool_id for r in boosted_results]
                            bm25_scores = self._bm25_index.get_scores_batch(query, bm25_tool_ids)
                            if bm25_scores:
                                max_bm25 = max(bm25_scores.values()) or 1.0
                                for r in boosted_results:
                                    bm25_raw = bm25_scores.get(r.tool_id, 0.0)
                                    if bm25_raw > 0:
                                        r.score += _BM25_WEIGHT * (bm25_raw / max_bm25)
                    except Exception as e:
                        logger.warning(f"Boost pipeline failed (FAISS results unaffected): {e}")
                        boosted_results = faiss_results
                else:
                    # No entity: trust raw FAISS ranking
                    boosted_results = faiss_results

                # Step 4.5: EXACT MATCH INJECTION
                if exact_match_tool:
                    found = False
                    for result in boosted_results:
                        if result.tool_id == exact_match_tool:
                            result.score = _EXACT_MATCH_SCORE
                            found = True
                            break
                    if not found:
                        boosted_results.insert(0, SearchResult(
                            tool_id=exact_match_tool,
                            score=_EXACT_MATCH_SCORE,
                            method="GET"
                        ))
            else:
                boosted_results = faiss_results

            # Step 4.6: LLM ENTITY BOOST — disabled. Reserved for future LLM query expansion (v2).

            # Step 4.7: USER PROFILE INJECTION (D1 fix)
            # Inject profile tools so LLM fallback has the right candidates.
            if is_user_profile and not is_mutation:
                _profile_tools = [
                    ("get_PersonData_personIdOrEmail", 2.0),
                    ("get_Persons_id", 1.5),
                ]
                for pt_id, pt_score in _profile_tools:
                    if self._registry and self._registry.get_tool(pt_id):
                        if not any(r.tool_id == pt_id for r in boosted_results):
                            boosted_results.append(SearchResult(tool_id=pt_id, score=pt_score, method="GET"))

            # Step 5: Sort by final score (boost system already applied entity/suffix boosts)
            boosted_results.sort(key=lambda r: r.score, reverse=True)

            # Step 5.5: LLM RERANK for fully ambiguous queries
            # When NO entity was detected (not even by LLM), the top-20 contains
            # tools from random entities with near-identical scores.
            # One LLM call to reorder top-10 significantly improves Top-1 accuracy.
            if not detected_entity:
                try:
                    from services.llm_reranker import rerank_with_llm
                    candidates = []
                    for r in boosted_results[:20]:
                        doc = (self._tool_documentation or {}).get(r.tool_id, {})
                        desc = doc.get('purpose', '')
                        entity_desc = self._build_entity_description(r.tool_id, r.method)
                        if entity_desc:
                            desc = f"{entity_desc}. {desc}"
                        candidates.append({
                            'tool_id': r.tool_id,
                            'score': r.score,
                            'description': desc,
                        })
                    reranked = await rerank_with_llm(
                        query=query,
                        candidates=candidates,
                        top_k=20,
                        tool_documentation=self._tool_documentation,
                    )
                    if reranked and reranked[0].tool_id:
                        reranked_ids = [r.tool_id for r in reranked]
                        id_to_result = {r.tool_id: r for r in boosted_results}
                        new_results = []
                        for rid in reranked_ids:
                            if rid in id_to_result:
                                new_results.append(id_to_result[rid])
                        for r in boosted_results:
                            if r.tool_id not in reranked_ids:
                                new_results.append(r)
                        boosted_results = new_results
                        logger.info(f"UnifiedSearch: LLM rerank -> top={reranked[0].tool_id}")
                except Exception as e:
                    logger.warning(f"UnifiedSearch: LLM rerank failed (using FAISS order): {e}")

            # Step 6: Convert to final format
            final_results = []
            for r in boosted_results[:top_k]:
                # Get description from registry or documentation
                description = ""
                origin_guide = {}

                if self._registry:
                    tool = self._registry.get_tool(r.tool_id)
                    if tool:
                        description = tool.summary or tool.description or ""

                if self._tool_documentation and r.tool_id in self._tool_documentation:
                    doc = self._tool_documentation[r.tool_id]
                    if not description:
                        description = doc.get("purpose", "")
                    origin_guide = doc.get("parameter_origin_guide", {})

                # If purpose is shared by multiple tools, prepend entity-specific prefix
                # so the LLM can differentiate (e.g., "Obriši Expenses po ID-u" vs "Obriši VehicleContracts po ID-u")
                if description.strip() in self._duplicate_purposes:
                    entity_desc = self._build_entity_description(r.tool_id, r.method)
                    if entity_desc:
                        description = f"{entity_desc}. {description}"

                final_results.append(UnifiedSearchResult(
                    tool_id=r.tool_id,
                    score=r.score,
                    method=r.method,
                    description=description[:250],  # Truncate for efficiency
                    origin_guide=origin_guide,
                    boosts_applied=getattr(r, 'boosts_applied', [])
                ))

            logger.info(
                f"Query '{query[:30]}...' -> {len(final_results)} results "
                f"(top: {final_results[0].tool_id if final_results else 'none'})"
            )

            span.set_attribute("search.intent", intent_result.intent.value)
            span.set_attribute("search.result_count", len(final_results))

            return UnifiedSearchResponse(
                results=final_results,
                intent=intent_result.intent,
                intent_confidence=intent_result.confidence,
                query=query,
                total_candidates=total_candidates,
                detected_entity=detected_entity,
            )

    # Single source of truth: config/tool_routing.py
    PRIMARY_ACTION_TOOLS = _PRIMARY_ACTION_TOOLS

    def _apply_boosts(
        self,
        query: str,
        results: List[SearchResult],
        detected_entity: Optional[str] = None,
        effective_query_type: Optional[QueryType] = None,
        is_possessive: bool = False,
        detected_method: Optional[str] = None,
    ) -> List[SearchResult]:
        """Apply additive boosts to FAISS results via boost_engine.apply_boosts()."""
        from services.tool_family_index import get_family_index

        family_match_tool = None
        if detected_entity and effective_query_type not in (None, QueryType.UNKNOWN):
            family_match_tool = get_family_index().resolve(detected_entity, effective_query_type)

        ctx = BoostContext(
            tool_documentation=self._tool_documentation or {},
            tool_categories=self._tool_categories or {},
            primary_action_tools=self.PRIMARY_ACTION_TOOLS,
        )

        return _apply_boosts_fn(
            query, results, ctx,
            detected_entity=detected_entity,
            effective_query_type=effective_query_type,
            is_possessive=is_possessive,
            family_match_tool=family_match_tool,
            detected_method=detected_method,
        )

    def _build_entity_description(self, tool_id: str, method: str) -> str:
        """Build entity-specific description for tools with duplicate purposes."""
        verb = _METHOD_VERBS.get(method, "")
        parts = tool_id.split("_", 1)
        if len(parts) < 2:
            return ""
        entity = parts[1]
        suffix_desc = ""
        for suffix, desc in _SUFFIX_DESCRIPTIONS.items():
            if entity.lower().endswith(suffix.lstrip("_").lower()):
                suffix_desc = desc
                entity = entity[:len(entity) - len(suffix.lstrip("_"))]
                break
        if entity.endswith("_id"):
            suffix_desc = suffix_desc or " po ID-u"
            entity = entity[:-3]
        if entity.endswith("_"):
            entity = entity[:-1]
        return f"{verb} {entity}{suffix_desc}".strip()

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about the search system."""
        faiss_store = get_faiss_store()

        return {
            "version": "2.0",
            "initialized": self._initialized,
            "faiss_initialized": faiss_store.is_initialized() if faiss_store else False,
            "faiss_stats": faiss_store.get_stats() if faiss_store else {},
            "tool_docs_loaded": len(self._tool_documentation) if self._tool_documentation else 0,
            "categories_loaded": bool(self._tool_categories),
            "query_type_classifier": "disabled_zlatni_put_v1",
        }


# Singleton instance — constructed synchronously under the GIL from the
# single asyncio event loop. No threading lock needed: the system is
# purely async and this sync function runs without awaits between the
# None check and the assignment. Actual initialization work is gated
# by `UnifiedSearch._init_lock` (asyncio.Lock) in `initialize()`.
_unified_search: Optional[UnifiedSearch] = None


def get_unified_search() -> UnifiedSearch:
    """Get singleton UnifiedSearch instance."""
    global _unified_search
    if _unified_search is None:
        _unified_search = UnifiedSearch()
    return _unified_search


async def initialize_unified_search(
    registry: Optional["ToolRegistry"] = None
) -> UnifiedSearch:
    """
    Initialize and return the unified search.

    Call this during application startup.
    """
    search = get_unified_search()
    if registry:
        search.set_registry(registry)
    await search.initialize()
    return search
