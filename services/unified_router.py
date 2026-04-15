"""
Unified Router - Single LLM makes ALL routing decisions.

Architecture:
1. Gather context (current state, user info, tools)
2. Detect ambiguity in search results
3. Single LLM call decides everything (with disambiguation hints if needed)
4. Execute based on decision OR ask clarification
"""

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING

import redis as _redis_mod

from config import get_settings
from services.openai_client import get_openai_client, get_llm_circuit_breaker
from services.circuit_breaker import CircuitOpenError
from services.query_router import QueryRouter, RouteResult
from services.unified_search import get_unified_search
from services.llm_reranker import rerank_with_llm
from services.ambiguity_detector import (
    AmbiguityDetector, AmbiguityResult, get_ambiguity_detector
)
from services.context import UserContextManager
from services.flow_phrases import (
    matches_show_more,
    matches_confirm_yes,
    matches_confirm_no,
    matches_exit_signal,
    matches_item_selection,
    matches_greeting,
)
from services.text_normalizer import sanitize_for_llm
from services.tracing import get_tracer, trace_span
from services.domain_models import RoutingTrace, RoutingTier
from services.errors import RoutingError, ErrorCode
from prometheus_client import Counter as PromCounter, Histogram as PromHistogram

ROUTING_DECISIONS = PromCounter(
    'routing_decisions_total',
    'Routing decisions by tier',
    ['tier'],
)

CLARIFY_ACTIONS = PromCounter(
    'routing_clarify_total',
    'Clarify actions triggered when query is too ambiguous',
    ['detected_entity'],  # 'none' if no entity detected
)

CLARIFY_SIMILAR_TOOLS = PromHistogram(
    'routing_clarify_similar_tools',
    'Number of similar tools when clarify is triggered',
    buckets=[0, 2, 5, 10, 20, 50],
)

if TYPE_CHECKING:
    from services.registry import ToolRegistry

logger = logging.getLogger(__name__)
_tracer = get_tracer("unified_router")
def _get_settings():
    return get_settings()


_clarify_redis_client: Optional["_redis_mod.Redis"] = None
_clarify_redis_lock = threading.Lock()


def _get_clarify_redis() -> "_redis_mod.Redis":
    """Module-level pooled sync Redis client for clarify-log writes.

    Reuses one ConnectionPool across all clarify calls so we don't pay a
    TCP handshake per ambiguous query.
    """
    global _clarify_redis_client
    if _clarify_redis_client is None:
        with _clarify_redis_lock:
            if _clarify_redis_client is None:
                _clarify_redis_client = _redis_mod.from_url(
                    os.environ.get("REDIS_URL", "redis://localhost:6379"),
                    max_connections=4,
                )
    return _clarify_redis_client


def _push_clarify_log(entry: str) -> None:
    """Push a clarify-log entry to Redis and trim the list.

    Runs synchronously; callers dispatch via asyncio.to_thread.
    """
    client = _get_clarify_redis()
    client.lpush("clarify:log", entry)
    client.ltrim("clarify:log", 0, 499)


@dataclass
class RouterDecision:
    """Result of unified routing decision."""
    action: str  # continue_flow, exit_flow, start_flow, simple_api, direct_response, clarify
    tool: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    flow_type: Optional[str] = None  # booking, mileage, case
    response: Optional[str] = None  # For direct_response
    clarification: Optional[str] = None  # For clarify action
    reasoning: str = ""
    confidence: float = 0.0
    ambiguity_detected: bool = False
    routing_tier: str = "full_search"  # Which tier handled this query


# Single source of truth: tool_routing.py
from tool_routing import PRIMARY_TOOLS, INTENT_CONFIG as INTENT_METADATA

# Exit signals moved to services/flow_phrases.py (centralized)


class UnifiedRouter:
    """
    Single LLM router that makes all routing decisions.

    This is the ONLY decision point - no keyword matching, no filtering.
    The LLM sees everything and decides.

    Uses semantic search to find relevant tools from ALL 950+ tools,
    not just hardcoded PRIMARY_TOOLS.
    """

    def __init__(self, registry: Optional["ToolRegistry"] = None, cost_tracker=None) -> None:
        """Initialize router with optional tool registry for semantic search."""
        # Shared client: rate limiting + connection pooling across all services
        self.client = get_openai_client()
        self._circuit_breaker = get_llm_circuit_breaker()
        self.model = _get_settings().AZURE_OPENAI_DEPLOYMENT_NAME

        # Redis-backed cost tracking (optional — graceful if None)
        self._cost_tracker = cost_tracker

        # Tool Registry for semantic search (injected)
        self._registry = registry

        # Query Router - brza staza za poznate patterne
        self.query_router = QueryRouter()

        # Ambiguity detector for disambiguation
        self._ambiguity_detector: Optional[AmbiguityDetector] = None

        self._initialized = False

    def set_registry(self, registry: "ToolRegistry") -> None:
        """Set tool registry for semantic search (allows late binding)."""
        self._registry = registry
        logger.info("UnifiedRouter: Registry set for semantic search")

    async def initialize(self) -> None:
        """Initialize router."""
        if self._initialized:
            return
        logger.info("UnifiedRouter: Initialized (uses tool_documentation.json via FAISS)")
        self._initialized = True

    async def _get_relevant_tools_with_ambiguity(
        self,
        query: str,
        user_context: Optional[Dict[str, Any]] = None,
        top_k: int = 20
    ) -> Tuple[Dict[str, str], Optional[AmbiguityResult]]:
        """
        Use UnifiedSearch to find relevant tools and detect ambiguity.

        Now also detects ambiguity in results for disambiguation.

        Returns:
            Tuple of (tools_dict, ambiguity_result)
            - tools_dict: {tool_name: description}
            - ambiguity_result: AmbiguityResult if ambiguity detected, else None
        """
        if not self._registry or not self._registry.is_ready:
            logger.debug("Registry not ready, using PRIMARY_TOOLS fallback")
            return PRIMARY_TOOLS, None

        try:
            # Use UnifiedSearch for consistent results
            unified = get_unified_search()
            unified.set_registry(self._registry)

            with trace_span(_tracer, "router.unified_search", {
                "search.top_k": top_k,
                "query.preview": query[:80],
            }) as search_span:
                response = await unified.search(query, top_k=top_k)
                search_span.set_attribute("search.result_count", len(response.results))
                search_span.set_attribute("search.intent", response.intent.value)
                if response.detected_entity:
                    search_span.set_attribute("search.detected_entity", response.detected_entity)
                if response.results:
                    search_span.set_attribute("search.top_tool", response.results[0].tool_id)
                    search_span.set_attribute("search.top_score", round(response.results[0].score, 3))

            if response.results:
                # Build dict of tool_name -> description
                relevant_tools = {}
                for r in response.results:
                    relevant_tools[r.tool_id] = r.description

                # Always include PRIMARY_TOOLS
                for tool_name, desc in PRIMARY_TOOLS.items():
                    if tool_name not in relevant_tools:
                        relevant_tools[tool_name] = desc

                # Detect ambiguity in results
                ambiguity_result = None
                if self._ambiguity_detector is None:
                    self._ambiguity_detector = get_ambiguity_detector()

                # Convert results to format expected by ambiguity detector
                search_results_for_ambiguity = [
                    {"tool_id": r.tool_id, "score": r.score}
                    for r in response.results
                ]

                ambiguity_result = self._ambiguity_detector.detect_ambiguity(
                    query=query,
                    search_results=search_results_for_ambiguity,
                    user_context=user_context
                )

                if ambiguity_result.is_ambiguous:
                    logger.info(
                        f"AMBIGUITY DETECTED: suffix={ambiguity_result.ambiguous_suffix}, "
                        f"similar_tools={len(ambiguity_result.similar_tools)}, "
                        f"detected_entity={ambiguity_result.detected_entity}"
                    )

                logger.info(
                    f"UnifiedSearch: {len(response.results)} tools "
                    f"(intent={response.intent.value}), "
                    f"total {len(relevant_tools)} with PRIMARY merge, "
                    f"ambiguous={ambiguity_result.is_ambiguous if ambiguity_result else False}"
                )
                return relevant_tools, ambiguity_result

            logger.debug("UnifiedSearch returned no results, using PRIMARY_TOOLS fallback")
            return PRIMARY_TOOLS, None

        except Exception as e:
            from services.errors import SearchError
            err = SearchError(
                ErrorCode.SEARCH_PIPELINE_FAILED,
                f"UnifiedSearch failed: {e}",
            )
            logger.error(f"{err}, using PRIMARY_TOOLS fallback")
            return PRIMARY_TOOLS, None

    async def _get_relevant_tools(self, query: str, top_k: int = 20) -> Dict[str, str]:
        """
        Use UnifiedSearch to find relevant tools for this query.

        Backwards-compatible wrapper around _get_relevant_tools_with_ambiguity.
        """
        tools, _ = await self._get_relevant_tools_with_ambiguity(query, None, top_k)
        return tools

    def _check_exit_signal(self, query: str) -> bool:
        """Check if query contains exit/cancellation signal (word-boundary safe)."""
        return matches_exit_signal(query)

    def _check_greeting(self, query: str) -> Optional[str]:
        """Check if query is a greeting and return response (centralized)."""
        return matches_greeting(query)

    async def route(
        self,
        query: str,
        user_context: Dict[str, Any],
        conversation_state: Optional[Dict] = None
    ) -> RouterDecision:
        """
        Make routing decision using LLM.

        Args:
            query: User's message
            user_context: User info (vehicle, person_id, etc.)
            conversation_state: Current flow state if any

        Returns:
            RouterDecision with action, tool, params, etc.
        """
        await self.initialize()

        logger.info(f"UNIFIED ROUTER START: query='{query[:50]}', has_user_context={user_context is not None}, in_flow={conversation_state is not None}")

        import time as _time
        _t0 = _time.perf_counter()

        with trace_span(_tracer, "router.route", {
            "query.preview": query[:80],
            "query.length": len(query),
            "has_user_context": user_context is not None,
            "in_flow": conversation_state is not None,
        }) as span:
            trace = {"tier": RoutingTier.FULL_SEARCH, "query": query}
            decision = await self._route_inner(query, user_context, conversation_state, trace)

            # Build structured RoutingTrace from accumulated data
            latency_ms = (_time.perf_counter() - _t0) * 1000
            routing_trace = RoutingTrace(
                query=query[:200],
                tier=trace.get("tier", RoutingTier.FULL_SEARCH),
                ml_intent=trace.get("ml_intent"),
                ml_confidence=trace.get("ml_confidence", 0.0),
                cp_set_size=trace.get("cp_set_size", 0),
                cp_labels=trace.get("cp_labels", []),
                faiss_candidates=trace.get("faiss_candidates", 0),
                top_faiss_score=trace.get("top_faiss_score", 0.0),
                ambiguity_detected=decision.ambiguity_detected,
                selected_tool=decision.tool,
                final_confidence=decision.confidence,
                latency_ms=round(latency_ms, 2),
            )

            # Emit structured OTel attributes
            for attr_key, attr_val in routing_trace.to_span_attributes().items():
                span.set_attribute(attr_key, attr_val)
            span.set_attribute("routing.action", decision.action)

            # Extended clarify telemetry for production forensics
            if decision.action == "clarify":
                span.set_attribute("routing.clarify.clarification", decision.clarification or "")
                span.set_attribute("routing.clarify.reasoning", decision.reasoning[:200])
                if trace.get("ambiguity_result"):
                    _amb = trace["ambiguity_result"]
                    span.set_attribute("routing.clarify.detected_entity", _amb.detected_entity or "none")
                    span.set_attribute("routing.clarify.similar_tools_count", len(_amb.similar_tools))
                    span.set_attribute("routing.clarify.similar_tools", ",".join(_amb.similar_tools[:10]))
            ROUTING_DECISIONS.labels(tier=routing_trace.tier.value).inc()

            decision.routing_tier = trace.get("tier", RoutingTier.FULL_SEARCH).value
            return decision

    async def _route_inner(
        self,
        query: str,
        user_context: Dict[str, Any],
        conversation_state: Optional[Dict],
        trace: dict,
    ) -> RouterDecision:
        """Inner routing logic wrapped by route() span."""

        # Quick checks before LLM

        # 1. Check for greeting
        greeting_response = self._check_greeting(query)
        if greeting_response:
            trace["tier"] = RoutingTier.DETERMINISTIC
            return RouterDecision(
                action="direct_response",
                response=greeting_response,
                reasoning="Greeting detected",
                confidence=1.0
            )

        # 2. Check for exit signal when in flow
        in_flow = conversation_state and conversation_state.get("flow")
        if in_flow and self._check_exit_signal(query):
            trace["tier"] = RoutingTier.DETERMINISTIC
            return RouterDecision(
                action="exit_flow",
                reasoning="Exit signal detected",
                confidence=1.0
            )

        # 3. CRITICAL: Handle in-flow continue signals explicitly
        # Uses centralized flow_phrases with word-boundary matching
        if in_flow:
            state = conversation_state.get("state", "")

            # "pokaži ostala" type requests in CONFIRMING/SELECTING state
            if matches_show_more(query):
                logger.info("UNIFIED ROUTER: 'show more' detected in flow, returning continue_flow")
                trace["tier"] = RoutingTier.DETERMINISTIC
                return RouterDecision(
                    action="continue_flow",
                    reasoning="Show more items request in active flow",
                    confidence=1.0
                )

            # Confirmation responses in CONFIRMING state (word-boundary safe)
            if state == "confirming":
                if matches_confirm_yes(query):
                    logger.info("UNIFIED ROUTER: Confirmation 'yes' detected, returning continue_flow")
                    trace["tier"] = RoutingTier.DETERMINISTIC
                    return RouterDecision(
                        action="continue_flow",
                        reasoning="User confirmed in confirming state",
                        confidence=1.0
                    )
                if matches_confirm_no(query):
                    logger.info("UNIFIED ROUTER: Confirmation 'no' detected, returning continue_flow")
                    trace["tier"] = RoutingTier.DETERMINISTIC
                    return RouterDecision(
                        action="continue_flow",
                        reasoning="User cancelled in confirming state",
                        confidence=1.0
                    )

            # Numeric/ordinal selection in SELECTING state
            if state == "selecting":
                if matches_item_selection(query):
                    logger.info("UNIFIED ROUTER: Item selection detected, returning continue_flow")
                    trace["tier"] = RoutingTier.DETERMINISTIC
                    return RouterDecision(
                        action="continue_flow",
                        reasoning="User selected item by number/ordinal",
                        confidence=1.0
                    )

        # 4. QUERY ROUTER - fast path for known patterns (0 tokens, <1ms)
        # Saves ~80% of LLM calls for simple queries
        logger.info(f"UNIFIED ROUTER: Trying QueryRouter for query='{query[:50]}'")

        with trace_span(_tracer, "router.query_router", {
            "query.preview": query[:80],
        }) as qr_span:
            qr_result = self.query_router.route(query, user_context)
            qr_span.set_attribute("qr.matched", qr_result.matched)
            qr_span.set_attribute("qr.confidence", qr_result.confidence)
            qr_span.set_attribute("qr.tool", qr_result.tool_name or "")
            qr_span.set_attribute("qr.flow_type", qr_result.flow_type or "")

            # Populate trace with ML classification data
            trace["ml_confidence"] = qr_result.confidence
            if qr_result.reason:
                # Extract intent from reason like "ML: check_vehicles"
                parts = qr_result.reason.split(": ", 1)
                if len(parts) > 1:
                    trace["ml_intent"] = parts[1].split(" ")[0]
            if qr_result.prediction_set:
                trace["cp_set_size"] = qr_result.prediction_set.size
                trace["cp_labels"] = list(qr_result.prediction_set.labels)

        logger.info(f"UNIFIED ROUTER: QR result: matched={qr_result.matched}, conf={qr_result.confidence}, flow={qr_result.flow_type if qr_result.matched else None}")

        if qr_result.matched:
            # 4a. FAST PATH — high confidence, CP set=1 or no CP
            if qr_result.flow_type != "mediation":
                logger.info(
                    f"UNIFIED ROUTER: Fast path via QueryRouter → "
                    f"{qr_result.tool_name or qr_result.flow_type} (conf={qr_result.confidence})"
                )
                trace["tier"] = RoutingTier.FAST_PATH
                return self._query_result_to_decision(qr_result, user_context)

            # 4b. MEDIATION — CP prediction set has 2-5 candidates
            if qr_result.prediction_set is not None:
                mediation_result = await self._mediation_route(
                    query, qr_result
                )
                if mediation_result is not None:
                    trace["tier"] = RoutingTier.MEDIATION
                    return mediation_result
                # Fallback to full LLM routing if mediation fails
                logger.info("UNIFIED ROUTER: Mediation failed, falling through to full LLM")

        # 5. LLM call - for complex queries that Query Router can't handle
        return await self._llm_route(query, user_context, conversation_state, trace=trace)

    @staticmethod
    def _format_vehicle_info(user_context: Dict[str, Any]) -> str:
        vehicle = UserContextManager(user_context).vehicle
        if vehicle and vehicle.id:
            return f"Korisnikovo vozilo: {vehicle.name or 'N/A'} ({vehicle.plate or 'N/A'})"
        return "Korisnik NEMA dodijeljeno vozilo"

    @staticmethod
    def _format_flow_info(conversation_state: Optional[Dict]) -> str:
        if not conversation_state or not conversation_state.get("flow"):
            return "Korisnik je u IDLE stanju (novi upit)"
        return (
            "Korisnik je U TIJEKU flow-a:\n"
            f"  - Flow: {conversation_state.get('flow')}\n"
            f"  - State: {conversation_state.get('state')}\n"
            f"  - Tool: {conversation_state.get('tool')}\n"
            f"  - Nedostaju parametri: {conversation_state.get('missing_params', [])}"
        )

    @staticmethod
    def _format_tools_catalog(relevant_tools: Dict[str, str]) -> str:
        entity_groups: Dict[str, list] = {}
        ungrouped: Dict[str, str] = {}
        for tool_name, description in relevant_tools.items():
            parts = tool_name.split("_", 2)
            if len(parts) >= 2:
                entity_groups.setdefault(parts[1], []).append((tool_name, description))
            else:
                ungrouped[tool_name] = description

        lines = [f"Dostupni alati ({len(relevant_tools)} relevantnih):"]
        for entity_key in sorted(entity_groups):
            lines.append(f"  === {entity_key} ===")
            for tool_name, description in entity_groups[entity_key]:
                method_tag = ""
                for prefix in ("get_", "post_", "put_", "patch_", "delete_"):
                    if tool_name.lower().startswith(prefix):
                        method_tag = f" [{prefix[:-1].upper()}]"
                        break
                lines.append(f"    - {tool_name}{method_tag}: {description}")
        for tool_name, description in ungrouped.items():
            lines.append(f"  - {tool_name}: {description}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format_ambiguity_sections(ambiguity_result) -> tuple:
        detected_context = ""
        if ambiguity_result and ambiguity_result.detected_entity:
            detected_context = (
                "DETEKTIRANI KONTEKST:\n"
                f"  - Entitet: {ambiguity_result.detected_entity}\n"
                "  Koristi ove informacije za odabir pravog alata.\n"
            )

        disambiguation_section = ""
        if ambiguity_result and ambiguity_result.is_ambiguous:
            disambiguation_section = f"""
        UPOZORENJE - DETEKTIRANA DVOSMISLENOST:
        {ambiguity_result.disambiguation_hint}

        Slični alati: {', '.join(ambiguity_result.similar_tools[:5])}

        PRAVILO ZA DVOSMISLENOST:
        - Ako upit NE SPOMINJE specifični entitet (npr. vozila, troškovi, osobe),
          koristi action="clarify" i pitaj korisnika koje podatke želi
        - Ako upit SPOMINJE entitet, odaberi alat za taj entitet
        - Primjer: "prosječna kilometraža" → entitet je vozila → get_Vehicles_Agg ili get_MasterData
        """
        return detected_context, disambiguation_section

    def _build_router_system_prompt(
        self,
        user_context: Dict[str, Any],
        conversation_state: Optional[Dict],
        relevant_tools: Dict[str, str],
        ambiguity_result,
    ) -> str:
        vehicle_info = self._format_vehicle_info(user_context)
        flow_info = self._format_flow_info(conversation_state)
        tools_desc = self._format_tools_catalog(relevant_tools)
        detected_context, disambiguation_section = self._format_ambiguity_sections(ambiguity_result)

        return f"""Ti si routing sustav za MobilityOne fleet management bot.

        TVOJ ZADATAK: Odluči što napraviti s korisnikovim upitom.

        {vehicle_info}

        {flow_info}

        {tools_desc}
        {detected_context}
        {disambiguation_section}

        PRAVILA:

        1. AKO je korisnik U TIJEKU flow-a:
        - Ako korisnik daje tražene parametre → action="continue_flow"
        - Ako korisnik potvrđuje (Da/Ne) → action="continue_flow"
        - Ako korisnik traži prikaz ostalih opcija ("pokaži ostala", "druga vozila") → action="continue_flow"
        - Ako korisnik bira broj ("1", "2", "prvi") → action="continue_flow"
        - SAMO ako korisnik EKSPLICITNO želi PREKINUTI flow → action="exit_flow"
        - PREPOZNAJ exit SAMO za: "ne želim ovo", "odustani od rezervacije", "zapravo nešto drugo"
        - "pokaži ostala", "koja još vozila", "više opcija" NIJE exit - to je continue_flow!

        2. AKO korisnik NIJE u flow-u:
        - Ako treba pokrenuti flow (rezervacija, unos km, prijava štete) → action="start_flow"
        - Ako je jednostavan upit (dohvat podataka) → action="simple_api"
        - Ako je pozdrav ili zahvala → action="direct_response"
        - Ako je upit PREVIŠE GENERIČAN (npr. "prosječna vrijednost" bez entiteta) → action="clarify"

        3. ODABIR ALATA:
        - "unesi km", "upiši kilometražu", "mogu li upisati" → post_AddMileage (WRITE!)
        - "koliko imam km", "moja kilometraža" → get_MasterData (READ)
        - "registracija", "tablica", "podaci o vozilu" → get_MasterData
        - "slobodna vozila", "dostupna vozila" → get_AvailableVehicles
        - "trebam auto", "rezerviraj" → get_AvailableVehicles (pa flow)
        - "moje rezervacije" → get_VehicleCalendar
        - "prijavi štetu", "kvar", "udario sam" → post_AddCase
        - "troškovi" → get_Expenses
        - "tripovi", "putovanja" → get_Trips

        4. FLOW TYPES:
        - booking: za rezervacije vozila (get_AvailableVehicles → post_VehicleCalendar)
        - mileage: za unos kilometraže (post_AddMileage)
        - case: za prijavu štete/kvara (post_AddCase)
        - generic: za BILO KOJI drugi alat koji zahtijeva korisničke parametre

        5. GENERIC FLOW:
        - POST/PUT/PATCH/DELETE alat koji NIJE booking/mileage/case → action="start_flow", flow_type="generic"
        - GET alat gdje korisnik NIJE naveo potrebne parametre → action="start_flow", flow_type="generic"
        - UVIJEK postavi "tool" polje kod generic flowa!
        - Primjeri: "kreiraj trošak" → flow_type="generic", tool="post_Expenses"
                    "ažuriraj servis" → flow_type="generic", tool="put_UpdateService"

        6. CLARIFY:
        - Koristi action="clarify" SAMO kada je upit previše generičan
        - U "clarification" polju postavi pitanje koje će pomoći identificirati pravi alat
        - Primjer: "Za koje podatke želite izračunati statistiku? (vozila, troškovi, putovanja)"

        ODGOVORI U JSON FORMATU:
        {{
            "action": "continue_flow|exit_flow|start_flow|simple_api|direct_response|clarify",
            "tool": "ime_alata ili null",
            "params": {{}},
            "flow_type": "booking|mileage|case|generic ili null",
            "response": "tekst odgovora za direct_response ili null",
            "clarification": "pitanje za korisnika (samo za action=clarify)",
            "reasoning": "kratko objašnjenje odluke",
            "confidence": 0.0-1.0
        }}"""

    async def _call_router_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        relevant_tools: Dict[str, str],
        ambiguity_result,
    ) -> Dict[str, Any]:
        with trace_span(_tracer, "router.llm_call", {
            "llm.model": self.model,
            "llm.temperature": 0.1,
            "llm.max_tokens": 500,
            "search.candidates": len(relevant_tools),
            "ambiguity.detected": ambiguity_result.is_ambiguous if ambiguity_result else False,
        }) as llm_span:
            response = await self._circuit_breaker.call(
                f"llm_router:{self.model}",
                self.client.chat.completions.create,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=500,
                response_format={"type": "json_object"},
            )

            if hasattr(response, "usage") and response.usage:
                llm_span.set_attribute("llm.prompt_tokens", response.usage.prompt_tokens)
                llm_span.set_attribute("llm.completion_tokens", response.usage.completion_tokens)
                llm_span.set_attribute("llm.total_tokens", response.usage.total_tokens)

                if self._cost_tracker:
                    try:
                        await self._cost_tracker.record_usage(
                            response.usage.prompt_tokens,
                            response.usage.completion_tokens,
                        )
                    except Exception as ct_err:
                        logger.warning(f"CostTracker record failed: {ct_err}")

        return json.loads(response.choices[0].message.content)

    def _validate_tool_selection(
        self,
        result: Dict[str, Any],
        relevant_tools: Dict[str, str],
    ) -> Dict[str, Any]:
        """Resolve the LLM-selected tool against the candidate set.

        Returns a *new* dict — never mutates the caller's one. The earlier
        in-place mutation pattern made it easy to miss a branch and leave
        ``result["action"]`` stale.
        """
        selected_tool = result.get("tool")
        if not selected_tool or selected_tool in relevant_tools:
            return result

        lower_index = {t.lower(): t for t in relevant_tools}
        match = lower_index.get(selected_tool.lower())
        if match:
            logger.info(f"LLM tool case-fixed: {selected_tool} → {match}")
            return {**result, "tool": match}

        if self._registry and self._registry.get_tool(selected_tool):
            logger.warning(f"LLM selected tool outside candidates: {selected_tool}")
            return result

        logger.warning(f"LLM hallucinated tool: {selected_tool}")
        return {
            **result,
            "action": "clarify",
            "tool": None,
            "clarification": (
                "Nisam siguran koji alat odgovara vašem upitu. "
                "Možete li malo preciznije opisati što trebate?"
            ),
            "reasoning": f"LLM returned non-existent tool '{selected_tool}'",
            "confidence": 0.2,
        }

    async def _handle_clarify_action(
        self,
        query: str,
        result: Dict[str, Any],
        ambiguity_result,
    ) -> RouterDecision:
        clarification_text = result.get("clarification")
        if not clarification_text and ambiguity_result:
            clarification_text = ambiguity_result.clarification_question

        entity = (
            ambiguity_result.detected_entity
            if ambiguity_result and ambiguity_result.detected_entity
            else "none"
        )
        similar = ambiguity_result.similar_tools if ambiguity_result else []
        CLARIFY_ACTIONS.labels(detected_entity=entity).inc()
        CLARIFY_SIMILAR_TOOLS.observe(len(similar))

        try:
            entry = json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "query": query[:200],
                    "entity": entity,
                    "similar_tools": similar[:10],
                    "clarification": (clarification_text or "")[:200],
                    "reasoning": result.get("reasoning", "")[:200],
                    "confidence": float(result.get("confidence", 0.3)),
                },
                ensure_ascii=False,
            )
            await asyncio.to_thread(_push_clarify_log, entry)
        except Exception as log_err:
            logger.debug(f"Clarify log to Redis failed (non-critical): {log_err}")

        return RouterDecision(
            action="clarify",
            clarification=clarification_text,
            reasoning=result.get("reasoning", "Query is ambiguous, need clarification"),
            confidence=float(result.get("confidence", 0.3)),
            ambiguity_detected=True,
        )

    async def _llm_route(
        self,
        query: str,
        user_context: Dict[str, Any],
        conversation_state: Optional[Dict],
        trace: Optional[dict] = None,
    ) -> RouterDecision:
        """Make routing decision using LLM with disambiguation support."""

        relevant_tools, ambiguity_result = await self._get_relevant_tools_with_ambiguity(
            query, user_context, top_k=25
        )
        if trace is not None:
            trace["ambiguity_result"] = ambiguity_result

        system_prompt = self._build_router_system_prompt(
            user_context, conversation_state, relevant_tools, ambiguity_result
        )
        sanitized_query = sanitize_for_llm(query) if query else ""
        user_prompt = f'Korisnikov upit: "{sanitized_query}"'

        try:
            result = await self._call_router_llm(
                system_prompt, user_prompt, relevant_tools, ambiguity_result
            )
            result = self._validate_tool_selection(result, relevant_tools)

            action = result.get("action", "simple_api")
            logger.info(
                f"UNIFIED ROUTER: '{query[:30]}...' → "
                f"action={action}, tool={result.get('tool')}, "
                f"flow={result.get('flow_type')}, "
                f"ambiguous={ambiguity_result.is_ambiguous if ambiguity_result else False}"
            )

            if action == "exit_flow" and not conversation_state:
                logger.warning(
                    f"LLM returned exit_flow but no active flow - "
                    f"converting to direct_response. Query: '{query[:40]}...'"
                )
                return RouterDecision(
                    action="direct_response",
                    response="Razumijem. Kako vam mogu pomoći?",
                    reasoning="LLM returned exit_flow but no active flow",
                    confidence=0.8,
                )

            if action == "clarify":
                return await self._handle_clarify_action(query, result, ambiguity_result)

            return RouterDecision(
                action=action,
                tool=result.get("tool"),
                params=result.get("params") if isinstance(result.get("params"), dict) else {},
                flow_type=result.get("flow_type"),
                response=result.get("response"),
                reasoning=result.get("reasoning", ""),
                confidence=float(result.get("confidence", 0.5)),
                ambiguity_detected=ambiguity_result.is_ambiguous if ambiguity_result else False,
            )

        except CircuitOpenError as e:
            logger.warning(f"Circuit breaker OPEN for router - falling back: {e}")
            return self._fallback_route(query, user_context)

        except Exception as e:
            err = RoutingError(
                ErrorCode.LLM_ROUTING_FAILED,
                f"LLM routing failed: {e}",
                metadata={"query_preview": query[:80]},
            )
            logger.error(str(err))
            return self._fallback_route(query, user_context)

    async def _mediation_route(
        self,
        query: str,
        qr_result: RouteResult,
    ) -> Optional[RouterDecision]:
        """Route via LLM reranker with CP-narrowed candidate set (2-5 tools).

        Instead of sending 25+ tools to LLM, sends only the CP prediction set
        candidates. This gives the LLM a focused choice, improving accuracy
        and reducing latency.

        Returns:
            RouterDecision if reranker succeeds, None to fall through to full search.
        """
        prediction_set = qr_result.prediction_set
        if prediction_set is None or prediction_set.size < 2:
            return None

        # Map CP labels (intent names) → tool candidates with descriptions
        candidates = []
        seen_tools = set()
        tool_metadata_map = {}  # tool_id → metadata (built during candidate loop)
        for label, prob in zip(prediction_set.labels, prediction_set.probabilities):
            metadata = INTENT_METADATA.get(label)
            if metadata is None:
                continue
            tool_id = metadata["tool"]
            if tool_id in seen_tools:
                continue
            seen_tools.add(tool_id)
            tool_metadata_map[tool_id] = metadata
            candidates.append({
                "tool_id": tool_id,
                "score": float(prob),
                "description": metadata.get("response_template", "") or f"Tool: {tool_id}",
            })

        if len(candidates) < 2:
            # Not enough unique tools to mediate — use top candidate directly
            return None

        cp_size = prediction_set.size
        logger.info(
            f"MEDIATION: CP set={cp_size}, {len(candidates)} unique tool candidates"
        )

        try:
            with trace_span(_tracer, "router.mediation", {
                "cp.set_size": cp_size,
                "cp.candidates": len(candidates),
                "cp.labels": ", ".join(prediction_set.labels[:5]),
            }):
                rerank_results = await rerank_with_llm(
                    query=query,
                    candidates=candidates,
                    top_k=1,
                    cost_tracker=self._cost_tracker,
                )
            if rerank_results:
                winner = rerank_results[0]
                logger.info(
                    f"MEDIATION: CP set={cp_size} → "
                    f"LLM chose {winner.tool_id} ({winner.confidence:.1%})"
                )

                winner_metadata = tool_metadata_map.get(winner.tool_id)
                flow_type = winner_metadata["flow_type"] if winner_metadata else "simple"
                return RouterDecision(
                    action="start_flow" if flow_type in (
                        "booking", "mileage_input", "case_creation",
                        "delete_booking", "delete_case", "delete_trip",
                    ) else "simple_api",
                    tool=winner.tool_id,
                    flow_type=flow_type,
                    reasoning=f"CP mediation: {winner.reasoning} (set={cp_size})",
                    confidence=winner.confidence,
                )
        except Exception as e:
            err = RoutingError(
                ErrorCode.MEDIATION_FAILED,
                f"Mediation rerank failed: {e}",
                metadata={"cp_set_size": prediction_set.size},
            )
            logger.warning(str(err))

        return None

    def _fallback_route(
        self,
        query: str,
        user_context: Dict[str, Any]
    ) -> RouterDecision:
        """Fallback routing when LLM fails - uses QueryRouter's regex rules."""
        logger.warning(f"LLM routing failed, using QueryRouter fallback for: '{query[:50]}...'")

        # Use Query Router
        qr_result = self.query_router.route(query, user_context)

        if qr_result.matched:
            logger.info(f"FALLBACK: QueryRouter matched -> {qr_result.tool_name or qr_result.flow_type}")
            return self._query_result_to_decision(qr_result, user_context, is_fallback=True)

        # Ultimate fallback - ask for clarification instead of guessing
        logger.warning("FALLBACK: QueryRouter no match, asking for clarification")
        return RouterDecision(
            action="direct_response",
            response=(
                "Nisam siguran što tražite. Možete pitati za:\n"
                "* Rezervaciju vozila\n"
                "* Kilometražu\n"
                "* Prijavu štete\n"
                "* Informacije o vozilu\n"
                "* Troškove\n"
                "* Putovanja\n\n"
                "Možete li pojasniti što točno trebate?"
            ),
            reasoning="Ultimate fallback: No match found, asking user to clarify",
            confidence=0.1
        )

    def _query_result_to_decision(
        self,
        qr_result: RouteResult,
        user_context: Optional[Dict[str, Any]] = None,
        is_fallback: bool = False
    ) -> RouterDecision:
        """
        Convert QueryRouter RouteResult to RouterDecision.

        Args:
            qr_result: Result from QueryRouter
            user_context: User context for template formatting
            is_fallback: True if called from fallback path (lower confidence)

        Returns:
            RouterDecision compatible with rest of system
        """
        # Confidence reduction for fallback path
        confidence = qr_result.confidence * (0.8 if is_fallback else 1.0)
        path_type = "fallback" if is_fallback else "fast path"

        flow_type = qr_result.flow_type

        # 1. Direct response (greetings, help, thanks, context queries)
        if flow_type == "direct_response":
            # FORMAT the template with user context
            response_text = qr_result.response_template
            if response_text and user_context:
                # Use UserContextManager for validated access
                ctx = UserContextManager(user_context)
                # Direct extraction for context queries (person_id, phone, tenant_id)
                if 'person_id' in response_text:
                    val = ctx.person_id or 'N/A'
                    response_text = f"👤 **Person ID:** {val}"
                elif 'phone' in response_text:
                    val = ctx.phone or 'N/A'
                    response_text = f"📱 **Telefon:** {val}"
                elif 'tenant_id' in response_text:
                    val = ctx.tenant_id or 'N/A'
                    response_text = f"🏢 **Tenant ID:** {val}"
                else:
                    # For other templates, use format() with simple context
                    simple_context = {
                        k: str(v) for k, v in user_context.items()
                        if not isinstance(v, (dict, list))
                    }
                    try:
                        from string import Template
                        response_text = Template(response_text).safe_substitute(simple_context)
                    except (KeyError, ValueError):
                        pass

            return RouterDecision(
                action="direct_response",
                response=response_text,
                reasoning=f"QueryRouter {path_type}: {qr_result.reason}",
                confidence=confidence
            )

        # 2. Flows that need multi-step interaction
        if flow_type in ("booking", "mileage_input", "case_creation",
                          "delete_booking", "delete_case", "delete_trip"):
            # Map flow_type to canonical names
            canonical_flow = {
                "booking": "booking",
                "mileage_input": "mileage",
                "case_creation": "case",
                "delete_booking": "delete_booking",
                "delete_case": "delete_case",
                "delete_trip": "delete_trip",
            }.get(flow_type, flow_type)

            return RouterDecision(
                action="start_flow",
                tool=qr_result.tool_name,
                flow_type=canonical_flow,
                reasoning=f"QueryRouter {path_type}: {qr_result.reason}",
                confidence=confidence
            )

        # 3. Simple API calls (get_MasterData, get_VehicleCalendar, etc.)
        # flow_type: "simple" or "list"
        return RouterDecision(
            action="simple_api",
            tool=qr_result.tool_name,
            flow_type=flow_type,
            reasoning=f"QueryRouter {path_type}: {qr_result.reason}",
            confidence=confidence
        )


# Singleton
_router: Optional[UnifiedRouter] = None
_router_lock = asyncio.Lock()


async def get_unified_router(cost_tracker=None) -> UnifiedRouter:
    """Get or create singleton router instance."""
    global _router
    if _router is not None:
        return _router
    async with _router_lock:
        if _router is None:
            router = UnifiedRouter(cost_tracker=cost_tracker)
            await router.initialize()
            _router = router
    return _router
