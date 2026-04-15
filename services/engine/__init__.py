"""
Message Engine - Public API facade.

Coordinates message processing through:
- User identification and context
- Conversation state management (Redis-backed)
- AI routing and tool execution
- Cost tracking and observability

This module has been refactored into smaller components:
- user_handler.py: User identification and greeting
- hallucination_handler.py: Hallucination feedback detection
- deterministic_executor.py: Fast path execution without LLM
- flow_executors.py: Multi-step flow handling
- tool_handler.py: Tool execution with validation
- flow_handler.py: Flow state management
"""

import asyncio
import atexit
import logging
import re
import time as _time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional

_SENDER_KEY_SAFE = re.compile(r"[^A-Za-z0-9+]")


def _safe_redis_sender(sender: str) -> str:
    """Strip Redis-key-hostile characters from a webhook sender id.

    Whitespace, colons, and newlines in `from` would corrupt keyspace
    partitioning and could collide across users. Keep digits, letters,
    and `+` (E.164 prefix). Empty result means the sender is unusable.
    """
    return _SENDER_KEY_SAFE.sub("", sender or "")

from config import get_settings
from services.conversation_manager import ConversationManager, ConversationState
from services.tool_executor import ToolExecutor
from services.ai_orchestrator import AIOrchestrator
from services.response_formatter import ResponseFormatter
from services.dependency_resolver import DependencyResolver
from services.error_learning import ErrorLearningService
from services.cost_tracker import CostTracker
from services.response_extractor import get_response_extractor
from services.query_router import get_query_router, RouteResult
from services.unified_router import get_unified_router, RouterAction

from .tool_handler import ToolHandler
from .flow_handler import FlowHandler
from .user_handler import UserHandler
from .hallucination_handler import HallucinationHandler
from .deterministic_executor import DeterministicExecutor
from .flow_executors import FlowExecutors
from services.context import (
    UserContextManager,
    MissingContextError,
    VehicleSelectionRequired,
)
from services.flow_phrases import (
    matches_show_more,
    matches_confirm_yes,
    matches_confirm_no,
    matches_item_selection,
    GDPR_CONSENT_MESSAGE,
    GDPR_CONSENT_DECLINED,
    GDPR_CONSENT_REPEAT,
    CONSENT_ACCEPT_KEYWORDS,
    CONSENT_DECLINE_KEYWORDS,
)
from services.user_service import UserService
from services.errors import ConversationError, ErrorCode, InfrastructureError
from services.tracing import get_tracer, trace_span

logger = logging.getLogger(__name__)
_tracer = get_tracer("message_engine")
# Lazy settings access — avoid module-level get_settings() which
# forces config parsing at import time (before env vars may be set).
def _get_settings():
    return get_settings()

# Bounded pool for CPU-bound ML predictions (TF-IDF predict_proba).
# 2 threads is enough — the GIL serializes numpy anyway, but releasing
# the event loop prevents coroutine starvation at concurrency > 5.
_ml_thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ml_route")
atexit.register(_ml_thread_pool.shutdown, wait=False)

__all__ = ['MessageEngine']


class MessageEngine:
    """
    Main message processing engine.

    Coordinates:
    - User identification
    - Conversation state (Redis-backed)
    - AI interactions with error feedback
    - Tool execution with validation

    This is a facade that coordinates the refactored components.
    """

    def __init__(
        self,
        gateway,
        registry,
        context_service,
        queue_service,
        cache_service,
        db_session
    ):
        """Initialize engine with all services."""
        self.gateway = gateway
        self.registry = registry
        self.executor = ToolExecutor(gateway, registry=registry)
        self.context = context_service
        self.queue = queue_service
        self.cache = cache_service
        self.db = db_session
        self.redis = context_service.redis if context_service else None

        # Core services
        self.dependency_resolver = DependencyResolver(registry)
        self.error_learning = ErrorLearningService(redis_client=self.redis)

        # Model drift detection removed in Phase D ML cull.
        self.drift_detector = None

        # Cost tracking
        self.cost_tracker: Optional[CostTracker] = None
        if self.redis:
            try:
                self.cost_tracker = CostTracker(redis_client=self.redis)
                logger.info("CostTracker initialized")
            except Exception as e:
                err = InfrastructureError(
                    ErrorCode.REDIS_UNAVAILABLE,
                    f"CostTracker init failed: {e}",
                    cause=e,
                )
                logger.warning(f"{err}")

        self.ai = AIOrchestrator(cost_tracker=self.cost_tracker)
        self.formatter = ResponseFormatter()
        # Response extraction for API results
        self.response_extractor = get_response_extractor()

        # Deterministic query router
        self.query_router = get_query_router()

        # Unified LLM router
        self.unified_router = None
        self._unified_router_initialized = False
        self._unified_router_lock = asyncio.Lock()

        # Refactored handlers
        self._tool_handler = ToolHandler(
            registry=registry,
            executor=self.executor,
            dependency_resolver=self.dependency_resolver,
            error_learning=self.error_learning,
            formatter=self.formatter
        )
        self._flow_handler = FlowHandler(
            registry=registry,
            executor=self.executor,
            ai=self.ai,
            formatter=self.formatter
        )

        # New modular handlers
        self._user_handler = UserHandler(db_session, gateway, cache_service)
        self._hallucination_handler = HallucinationHandler(
            context_service=context_service,
            error_learning=self.error_learning,
            ai_model=self.ai.model
        )
        self._deterministic_executor = DeterministicExecutor(
            registry=registry,
            executor=self.executor,
            formatter=self.formatter,
            query_router=self.query_router,
            response_extractor=self.response_extractor,
            dependency_resolver=self.dependency_resolver,
            flow_handler=self._flow_handler
        )
        self._flow_executors = FlowExecutors(
            gateway=gateway,
            flow_handler=self._flow_handler
        )

        logger.info("MessageEngine initialized (v20.0 modular)")

    async def process(
        self,
        sender: str,
        text: str,
        message_id: Optional[str] = None,
        db_session=None,
        tenant_id: str = "",
    ) -> str:
        """
        Process incoming message.

        Args:
            sender: User phone number
            text: Message text
            message_id: Optional message ID
            db_session: Database session for this request (concurrency-safe)
            tenant_id: Tenant ID resolved at the edge from sender phone
                       (Option C, V6.2 strict isolation). Overrides any env
                       default in user_context — empty string means caller had
                       no resolution and we must NOT fabricate one.

        Returns:
            Response text
        """
        with trace_span(_tracer, "engine.process", {
            "engine.sender_suffix": sender[-4:] if sender else "",
            "engine.query_preview": (text or "")[:80],
        }) as span:
            result = await self._process_inner(sender, text, message_id, db_session, tenant_id)
            span.set_attribute("engine.response_length", len(result))
            return result

    async def _process_inner(
        self,
        sender: str,
        text: str,
        message_id: Optional[str] = None,
        db_session=None,
        tenant_id: str = "",
    ) -> str:
        """Inner implementation of process(), wrapped by OTel span."""

        _t0 = _time.perf_counter()
        logger.info(f"Processing: {sender[-4:]} - {text[:50]}")

        # Early validation: empty/whitespace-only messages
        if not text or not text.strip():
            return "Molim napišite poruku."

        try:
            # 1. Identify user (delegated to UserHandler)
            # Always returns a context (guest context if not in MobilityOne)
            user_context = await self._user_handler.identify_user(sender, db_session=db_session)
            # Edge-resolved tenant (Option C) MUST win over any env default
            # baked into identify_user. Without this override the V6.2 strict
            # tenant precedence at api_gateway sees an env-leaked value.
            if tenant_id and isinstance(user_context, dict):
                user_context["tenant_id"] = tenant_id
            if not user_context:
                err = ConversationError(
                    ErrorCode.CONTEXT_MISSING,
                    f"identify_user returned None for {sender[-4:]}",
                    metadata={"sender_suffix": sender[-4:]},
                )
                logger.error(f"{err}")
                user_context = {
                    "person_id": None, "phone": sender,
                    "tenant_id": _get_settings().tenant_id,
                    "display_name": "Korisnik", "vehicle": {},
                    "is_new": True, "is_guest": True
                }
            _t1 = _time.perf_counter()
            logger.info(f"TIMING identify_user: {int((_t1-_t0)*1000)}ms")

            # 1.5 GDPR Consent Gate — ALL users must accept before any processing
            consent_result = await self._check_gdpr_consent(
                sender, text, user_context, db_session
            )
            if consent_result is not None:
                return consent_result

            # 2. Load conversation state
            conv_manager = await ConversationManager.load_for_user(sender, self.redis)

            # 3. Check timeout
            if conv_manager.is_timed_out():
                err = ConversationError(ErrorCode.FLOW_TIMEOUT, f"Flow timed out for {sender[-4:]} after {conv_manager.FLOW_TIMEOUT_SECONDS}s")
                logger.warning(str(err))
                await conv_manager.reset()

            # 4. Add to history
            await self.context.add_message(sender, "user", text)

            # 4.5 Check for hallucination feedback (delegated to HallucinationHandler)
            hallucination_result = await self._hallucination_handler.check_hallucination_feedback(
                text=text,
                sender=sender,
                user_context=user_context,
                conv_manager=conv_manager
            )
            if hallucination_result:
                return hallucination_result

            _t2 = _time.perf_counter()
            logger.info(f"TIMING pre-processing: {int((_t2-_t0)*1000)}ms")

            # 5. Handle new user greeting (delegated to UserHandler)
            if UserContextManager(user_context).is_new:
                greeting = self._user_handler.build_greeting(user_context)

                response = await self._process_with_state(
                    sender, text, user_context, conv_manager
                )

                _t3 = _time.perf_counter()
                logger.info(f"TIMING total (new user): {int((_t3-_t0)*1000)}ms")

                full_response = f"{greeting}\n\n---\n\n{response}"
                await self.context.add_message(sender, "assistant", full_response)
                return full_response

            # 6. Process based on state
            response = await self._process_with_state(
                sender, text, user_context, conv_manager
            )

            _t3 = _time.perf_counter()
            logger.info(f"TIMING _process_with_state: {int((_t3-_t2)*1000)}ms")
            logger.info(f"TIMING total: {int((_t3-_t0)*1000)}ms")

            # 7. Save response to history
            await self.context.add_message(sender, "assistant", response)

            return response

        except MissingContextError as e:
            logger.info(f"Missing context: {e.param} - prompting user")
            await self.context.add_message(sender, "assistant", e.prompt_hr)
            return e.prompt_hr

        except VehicleSelectionRequired as e:
            logger.info(f"Vehicle selection needed: {len(e.vehicles)} vehicles")
            vehicles_list = "\n".join([
                f"* {v.get('LicencePlate', 'N/A')} - {v.get('FullVehicleName', v.get('DisplayName', 'Vozilo'))}"
                for v in e.vehicles[:5]
            ])
            prompt = f"{e.prompt_hr}\n\n{vehicles_list}"
            await self.context.add_message(sender, "assistant", prompt)
            return prompt

        except Exception as e:
            err = ConversationError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"Processing error for {sender[-4:]}: {e}",
                cause=e,
            )
            logger.error(f"{err}", exc_info=True)
            return "Došlo je do greške. Molimo pokušajte ponovno."

    async def _check_gdpr_consent(
        self,
        sender: str,
        text: str,
        user_context: Dict[str, Any],
        db_session
    ) -> Optional[str]:
        """Check GDPR consent status and handle consent flow.

        Uses user_context (already resolved by identify_user) to determine
        if user is a valid MobilityOne user. Does NOT re-query the API.
        Only queries DB for consent flag status.

        Returns None if consent is already given (continue processing).
        Returns a response string if consent is pending (block processing).
        """
        # Guest = not found in MobilityOne API → block immediately
        sender_key = _safe_redis_sender(sender)
        if user_context.get("is_guest"):
            consent_key = f"gdpr_consent:{sender_key}"
            try:
                await self.redis.set(consent_key, "unknown", ex=300)
            except Exception as e:
                logger.debug(f"Redis best-effort consent cache write failed (guest): {e}")
            err = ConversationError(
                ErrorCode.CONSENT_REQUIRED,
                f"Unregistered user blocked at consent gate: ***{sender[-4:]}",
                metadata={"sender_suffix": sender[-4:]},
            )
            logger.info(f"{err}")
            return (
                "Pozdrav! Ja sam MobilityOne AI asistent — automatizirani sustav, ne čovjek.\n\n"
                "Vaš broj nije registriran u MobilityOne sustavu.\n"
                "Za korištenje ovog bota morate biti registrirani korisnik.\n\n"
                "Kontaktirajte vašeg administratora za registraciju."
            )

        # User IS in MobilityOne. Check consent status via Redis cache first.
        consent_key = f"gdpr_consent:{sender_key}"
        try:
            cached = await self.redis.get(consent_key)
        except Exception as e:
            err = InfrastructureError(
                ErrorCode.REDIS_UNAVAILABLE,
                f"Redis error during consent check: {e}",
                cause=e,
            )
            logger.warning(f"{err}")
            cached = None
        if cached == b"1" or cached == "1":
            return None  # Consent already given, continue

        # Check DB for consent flag
        db = db_session or self._user_handler.db
        user_service = UserService(db, self._user_handler.gateway, self._user_handler.cache)
        user = await user_service.get_active_identity(sender)

        if user and user.gdpr_consent_given:
            # Cache consent in Redis (24h TTL) to avoid future DB lookups
            try:
                await self.redis.set(consent_key, "1", ex=86400)
            except Exception as e:
                logger.debug(f"Redis best-effort consent cache write failed: {e}")
            return None  # Already consented

        if not user:
            # DB lookup failed but user IS in MobilityOne (not guest).
            # This can happen if DB was down during auto-onboard.
            # Let them through — consent will be checked again next message.
            err = InfrastructureError(
                ErrorCode.DATABASE_UNAVAILABLE,
                f"Consent DB lookup failed but user is valid MobilityOne user ***{sender[-4:]}",
            )
            logger.warning(f"{err}")
            return None

        # User exists in DB but hasn't consented — check if this message IS a consent response
        normalized = text.strip().lower().rstrip(".!?")

        if normalized in CONSENT_ACCEPT_KEYWORDS:
            await user_service.record_consent(sender, given=True)
            try:
                await self.redis.set(consent_key, "1", ex=86400)
                await self.redis.delete(f"gdpr_consent_pending:{sender_key}")
            except Exception as e:
                logger.debug(f"Redis best-effort consent accept cache write failed: {e}")
            logger.info(f"GDPR consent accepted by ***{sender[-4:]}")
            greeting = self._user_handler.build_greeting(user_context)
            return f"Hvala na pristanku!\n\n{greeting}"

        if normalized in CONSENT_DECLINE_KEYWORDS:
            logger.info(f"GDPR consent declined by ***{sender[-4:]}")
            return GDPR_CONSENT_DECLINED

        # Check if we already sent the consent message (via Redis flag)
        pending_key = f"gdpr_consent_pending:{sender_key}"
        try:
            already_asked = await self.redis.get(pending_key)
        except Exception as e:
            logger.debug(f"Redis best-effort consent pending check failed: {e}")
            already_asked = None

        if already_asked:
            return GDPR_CONSENT_REPEAT

        # First interaction — send consent message
        err = ConversationError(
            ErrorCode.CONSENT_EXPIRED,
            f"Consent not yet given, prompting user ***{sender[-4:]}",
            metadata={"sender_suffix": sender[-4:]},
        )
        logger.info(f"{err}")
        try:
            await self.redis.set(pending_key, "1", ex=86400)
        except Exception as e:
            logger.debug(f"Redis best-effort consent pending flag write failed: {e}")
        return GDPR_CONSENT_MESSAGE

    async def _process_with_state(
        self,
        sender: str,
        text: str,
        user_context: Dict[str, Any],
        conv_manager: ConversationManager
    ) -> str:
        """Process message based on conversation state using Unified Router."""

        _pws_start = _time.perf_counter()

        state = conv_manager.get_state()
        is_in_flow = conv_manager.is_in_flow()

        logger.info(
            f"STATE CHECK: user={sender[-4:]}, state={state.value}, "
            f"is_in_flow={is_in_flow}, flow={conv_manager.get_current_flow()}, "
            f"tool={conv_manager.get_current_tool()}, "
            f"missing={conv_manager.get_missing_params()}, "
            f"has_items={len(conv_manager.get_displayed_items())}"
        )

        # Direct state-based handling for in-flow messages
        if is_in_flow:
            if state == ConversationState.CONFIRMING:
                if matches_show_more(text):
                    logger.info("DIRECT HANDLER: 'show more' in CONFIRMING state")
                    return await self._flow_handler.handle_confirmation(
                        sender, text, user_context, conv_manager
                    )
                if matches_confirm_yes(text) or matches_confirm_no(text):
                    logger.info("DIRECT HANDLER: confirmation response in CONFIRMING state")
                    return await self._flow_handler.handle_confirmation(
                        sender, text, user_context, conv_manager
                    )

            if state == ConversationState.SELECTING_ITEM:
                if matches_item_selection(text):
                    logger.info("DIRECT HANDLER: item selection in SELECTING state")
                    return await self._flow_handler.handle_selection(
                        sender, text, user_context, conv_manager, self._handle_new_request
                    )

        # Initialize unified router lazily (lock prevents double-init under concurrency)
        if not self._unified_router_initialized:
            async with self._unified_router_lock:
                if not self._unified_router_initialized:
                    self.unified_router = await get_unified_router(cost_tracker=self.cost_tracker)
                    if self.registry and self.registry.is_ready:
                        self.unified_router.set_registry(self.registry)
                    self._unified_router_initialized = True

        # Build conversation state for router
        conv_state = None
        if conv_manager.is_in_flow():
            conv_state = {
                "flow": conv_manager.get_current_flow(),
                "state": state.value,
                "tool": conv_manager.get_current_tool(),
                "missing_params": conv_manager.get_missing_params()
            }

        # Get unified routing decision (with fallback on failure)
        _rt0 = _time.perf_counter()
        try:
            decision = await self.unified_router.route(text, user_context, conv_state)
        except Exception as e:
            err = ConversationError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"Unified router failed: {e}",
                metadata={"sender_suffix": sender[-4:], "text_preview": text[:50]},
                cause=e,
            )
            logger.error(f"{err}", exc_info=True)
            # Fall through to _handle_new_request which has its own routing
            return await self._handle_new_request(sender, text, user_context, conv_manager)
        _rt1 = _time.perf_counter()

        logger.info(
            f"UNIFIED ROUTER: action={decision.action}, tool={decision.tool}, "
            f"flow={decision.flow_type}, conf={decision.confidence:.2f}"
        )
        logger.info(f"TIMING router: {int((_rt1-_rt0)*1000)}ms (since pws_start: {int((_rt1-_pws_start)*1000)}ms)")

        # Handle routing decisions
        if decision.action == RouterAction.DIRECT_RESPONSE:
            return decision.response or "Kako vam mogu pomoći?"

        if decision.action == RouterAction.CLARIFY:
            logger.info(f"UNIFIED ROUTER: Clarification needed - '{decision.clarification}'")
            return decision.clarification or "Možete li mi reći više detalja o tome što tražite?"

        if decision.action == RouterAction.EXIT_FLOW:
            if conv_manager.is_in_flow():
                logger.info("UNIFIED ROUTER: Exiting flow, resetting state")
                await conv_manager.reset()
                new_decision = await self.unified_router.route(text, user_context, None)

                if new_decision.action == RouterAction.DIRECT_RESPONSE:
                    return new_decision.response or "Kako vam mogu pomoći?"
                if new_decision.action == RouterAction.START_FLOW:
                    return await self._handle_flow_start(new_decision, text, user_context, conv_manager)
                if new_decision.action == RouterAction.SIMPLE_API and new_decision.tool:
                    route = RouteResult(
                        matched=True,
                        tool_name=new_decision.tool,
                        confidence=new_decision.confidence,
                        flow_type="simple"
                    )
                    result = await self._deterministic_executor.execute(
                        route, user_context, conv_manager, sender, text
                    )
                    if result:
                        return result
                # After exiting a flow, if re-route doesn't match anything useful,
                # confirm cancellation instead of showing confusing fallback
                return "U redu, odustali ste. Kako vam još mogu pomoći?"
            else:
                logger.warning("UNIFIED ROUTER: exit_flow received but not in flow - ignoring")
                return "Kako vam mogu pomoći?"

        if decision.action == RouterAction.CONTINUE_FLOW:
            if state == ConversationState.SELECTING_ITEM:
                return await self._flow_handler.handle_selection(
                    sender, text, user_context, conv_manager, self._handle_new_request
                )
            if state == ConversationState.CONFIRMING:
                result = await self._flow_handler.handle_confirmation(
                    sender, text, user_context, conv_manager
                )
                if isinstance(result, dict) and result.get("mid_flow_question"):
                    question = result.get("question", text)
                    logger.info(f"P1: Handling mid-flow question: '{question[:50]}'")
                    # Per-sender guard to prevent nested mid-flow recursion
                    # Uses conv_manager.context (per-user) instead of self (shared singleton)
                    mid_flow_key = "_handling_mid_flow"
                    already_handling = conv_manager.context.tool_outputs.get(mid_flow_key, False)
                    if not already_handling:
                        conv_manager.context.tool_outputs[mid_flow_key] = True
                        try:
                            answer = await self._handle_new_request(sender, question, user_context, conv_manager)
                        finally:
                            conv_manager.context.tool_outputs.pop(mid_flow_key, None)
                        return f"{answer}\n\n---\n_Čeka se potvrda prethodne operacije. Potvrdite s **Da** ili **Ne**._"
                    else:
                        logger.warning("P1: Skipping nested mid-flow question to prevent recursion")
                        return "Potvrdite prethodnu operaciju s **Da** ili **Ne**."
                return result if isinstance(result, str) else str(result)
            if state == ConversationState.GATHERING_PARAMS:
                return await self._flow_handler.handle_gathering(
                    sender, text, user_context, conv_manager, self._handle_new_request
                )
            if state == ConversationState.IDLE and conv_manager.get_current_flow():
                err = ConversationError(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    "is_in_flow but state=IDLE",
                    metadata={"flow": conv_manager.get_current_flow()},
                )
                logger.warning(f"STATE MISMATCH: {err}")
                await conv_manager.reset()

        if decision.action == RouterAction.START_FLOW:
            # Guest users cannot use flows (booking, mileage, case) - require registration
            if user_context.get("is_guest") and not UserContextManager(user_context).person_id:
                return (
                    "Za ovu operaciju trebate biti registrirani u MobilityOne sustavu.\n"
                    "Kontaktirajte svog administratora za pristup."
                )
            return await self._handle_flow_start(decision, text, user_context, conv_manager)

        if decision.action == RouterAction.SIMPLE_API and decision.tool:
            # Guest users: check if tool needs PersonId before calling API
            if user_context.get("is_guest") and not UserContextManager(user_context).person_id:
                tool = self.registry.get_tool(decision.tool) if self.registry else None
                needs_person = False
                if tool:
                    for param_def in tool.parameters.values():
                        if getattr(param_def, 'context_key', None) == "person_id":
                            needs_person = True
                            break
                if needs_person:
                    return (
                        "Za dohvat osobnih podataka trebate biti registrirani u MobilityOne sustavu.\n"
                        "Kontaktirajte svog administratora za pristup."
                    )
            route = RouteResult(
                matched=True,
                tool_name=decision.tool,
                confidence=decision.confidence,
                flow_type="simple"
            )
            result = await self._deterministic_executor.execute(
                route, user_context, conv_manager, sender, text
            )
            if result:
                return result
            # Tool execution failed — inform user directly instead of expensive fallback chain
            logger.warning(f"Simple API execution failed for {decision.tool}")
            return "Došlo je do greške pri dohvatu podataka. Pokušajte ponovo."

        # UnifiedRouter returned an action we couldn't handle — should not happen
        logger.warning(f"UNHANDLED action={decision.action} tool={decision.tool}")
        return (
            "Nisam siguran što tražite. Možete pitati za:\n"
            "* Rezervaciju vozila\n"
            "* Kilometražu\n"
            "* Prijavu štete\n"
            "* Informacije o vozilu"
        )

    async def _handle_flow_start(self, decision, text: str, user_context: Dict, conv_manager) -> str:
        """Handle flow start from router decision."""
        if decision.flow_type == "booking":
            return await self._flow_executors.handle_booking_flow(
                text, user_context, conv_manager, decision.params
            )
        if decision.flow_type == "mileage":
            return await self._flow_executors.handle_mileage_input_flow(
                text, user_context, conv_manager, decision.params
            )
        if decision.flow_type == "case":
            return await self._flow_executors.handle_case_creation_flow(
                text, user_context, conv_manager, decision.params
            )
        if decision.flow_type in ("delete_booking", "delete_case", "delete_trip"):
            return await self._flow_executors.handle_delete_flow(
                decision.flow_type, text, user_context, conv_manager
            )
        # ── GENERIC: Any flow_type not handled above (covers all 950 tools) ──
        logger.info(f"GENERIC FLOW START: type={decision.flow_type}, tool={decision.tool}")

        tool_name = decision.tool
        if not tool_name:
            return "Nisam mogao odrediti operaciju. Možete li preformulirati?"

        tool = self.registry.get_tool(tool_name) if self.registry else None
        if not tool:
            err = ConversationError(
                ErrorCode.FLOW_NOT_FOUND,
                f"Tool '{tool_name}' not found in registry",
                metadata={"tool_name": tool_name, "flow_type": decision.flow_type},
            )
            logger.warning(f"{err}")
            return f"Alat '{tool_name}' nije pronađen u sustavu."

        # Determine which params are missing (exclude context-injectable ones)
        ctx = UserContextManager(user_context)
        injected = set()
        for p_name in tool.get_context_params():
            injected.add(p_name)
        if ctx.person_id:
            for p_name, p_def in tool.parameters.items():
                if getattr(p_def, 'context_key', None) == 'person_id':
                    injected.add(p_name)
        if ctx.vehicle_id:
            injected.add("VehicleId")

        provided = set(decision.params.keys()) if decision.params else set()
        missing = [p for p in tool.required_params if p not in injected and p not in provided]

        is_mutation = tool.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        flow_name = f"generic_{'mutation' if is_mutation else 'query'}"

        await conv_manager.start_flow(flow_name=flow_name, tool=tool_name, required_params=missing)

        # Store context-injected + LLM-extracted params
        initial_params = dict(decision.params or {})
        if ctx.person_id:
            for p_name, p_def in tool.parameters.items():
                if getattr(p_def, 'context_key', None) == 'person_id':
                    initial_params[p_name] = ctx.person_id
        if ctx.vehicle_id and "VehicleId" in tool.parameters:
            initial_params["VehicleId"] = ctx.vehicle_id
        if initial_params:
            await conv_manager.add_parameters(initial_params)
        await conv_manager.save()

        if not missing:
            # All params already available — execute or confirm
            if is_mutation:
                result = await self._flow_handler.request_confirmation(
                    tool_name=tool_name,
                    parameters=conv_manager.get_parameters(),
                    user_context=user_context,
                    conv_manager=conv_manager
                )
                return result.get("prompt", "Potvrdite operaciju s 'Da' ili 'Ne'.")
            else:
                return await self._flow_handler._execute_generic_tool(
                    tool_name, conv_manager.get_parameters(), user_context, conv_manager
                )

        from services.context import get_multiple_missing_prompts
        return get_multiple_missing_prompts(missing)

    async def _handle_new_request(
        self,
        sender: str,
        text: str,
        user_context: Dict[str, Any],
        conv_manager: ConversationManager
    ) -> str:
        """Handle new request via QueryRouter deterministic routing (exception recovery / mid-flow callback)."""

        # DETERMINISTIC ROUTING - Try rules FIRST
        # predict_proba() is CPU-bound (matrix mul). On 0.5 CPU it blocks the
        # event loop for 1-5ms, starving all other coroutines at concurrency >5.
        # Bounded pool (2 threads) releases the loop without unbounded thread creation.
        loop = asyncio.get_running_loop()
        route = await loop.run_in_executor(_ml_thread_pool, self.query_router.route, text, user_context)

        if route.matched:
            logger.info(f"ROUTER: Deterministic match -> {route.tool_name or route.flow_type}")

            # Direct response (greetings, thanks, help, context queries)
            if route.flow_type == "direct_response":
                if route.response_template:
                    ctx = UserContextManager(user_context)
                    if 'person_id' in route.response_template:
                        return f"**Person ID:** {ctx.person_id or 'N/A'}"
                    elif 'phone' in route.response_template:
                        return f"**Telefon:** {ctx.phone or 'N/A'}"
                    elif 'tenant_id' in route.response_template:
                        return f"**Tenant ID:** {ctx.tenant_id or 'N/A'}"

                    simple_context = {
                        k: v for k, v in user_context.items()
                        if not isinstance(v, (dict, list))
                    }
                    try:
                        return route.response_template.format(**simple_context)
                    except (KeyError, ValueError):
                        return route.response_template
                return route.response_template

            # Flow-based routing
            if route.flow_type == "booking":
                return await self._flow_executors.handle_booking_flow(text, user_context, conv_manager, {})
            if route.flow_type == "mileage_input":
                return await self._flow_executors.handle_mileage_input_flow(text, user_context, conv_manager, {})
            if route.flow_type == "case_creation" and route.tool_name:
                return await self._flow_executors.handle_case_creation_flow(text, user_context, conv_manager, {})

            # Simple or list query - execute deterministically
            if route.flow_type in ("simple", "list") and route.tool_name:
                result = await self._deterministic_executor.execute(
                    route, user_context, conv_manager, sender, text
                )
                if result:
                    return result
                logger.warning(f"Deterministic execution failed for {route.tool_name}")

        # No deterministic match — return helpful message
        logger.info(f"_handle_new_request: No deterministic match for '{text[:50]}'")
        return (
            "Nisam siguran što tražite. Možete pitati za:\n"
            "* Rezervaciju vozila\n"
            "* Kilometražu\n"
            "* Prijavu štete\n"
            "* Informacije o vozilu"
        )
