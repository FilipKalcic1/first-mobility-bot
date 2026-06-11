"""V2 Engine — orchestrates L-1 → L8 in order.

Single entry: `await engine.process_message(phone, query)` → reply text.

Wiring (top-down):
    L-1 RateLimiter        → blocked? short-circuit with cooldown msg
    L0.5 PIIScrubber       → safe text for LLM downstream
    L0   IdentityContext   → personId + masterData (cached)
    PENDING continuations  → params → mutation → reoffer → clarify → flow
                             (a saved state consumes the message FIRST)
    L0.7-L0.85 guards      → crisis / negation / multi-intent / meta
    L1   SpecialIntents    → terminal if matched (welcome/GDPR/help)
    L1.5 Unknown phone     → enrollment message for unregistered numbers
    L2a  IntentType        → type bucket (or safe fallback)
    L4   Flow start        → keyword-gated booking/mileage/case
    L2b  DriverBasics      → anchor match → serve from cached masterData
    Model A cascade        → Turn 1: action picker; Turn 2: scoped L3
                             router → top-3 tool picker; Turn 3: params →
                             L6 mutation gate → L7 executor → L8 formatter
                             (no LLM auto-execute)

Failure-mode rule: every layer either returns or short-circuits. The
engine never crashes — it returns a Croatian fallback message instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from services.v2 import formatter, mutation_gate
from services.v2.flow_engine import (
    FlowEngine, FlowStateStore, FLOWS,
    OUTCOME_CANCELLED, OUTCOME_DONE, OUTCOME_EXECUTE,
    OUTCOME_INVALID, OUTCOME_PROMPT,
)
from services.v2.identity import IdentityContext, IdentitySnapshot
from services.v2.intent_type import (
    IntentTypeClassifier, KIND_ACTION_OR_COMPLAINT,
    KIND_FLOW_REQUEST, KIND_QUESTION_ABOUT_SELF,
)
from services.v2.driver_basics import DriverBasicsAnchor
from services.router.catalog_scoper import CatalogScoper
from services.router.llm_router import LLMRouter
from services.formatter.llm_formatter import LLMFormatter
from services.v2.telemetry import (
    TelemetryEvent, TelemetryLogger,
    get_negation_flag, set_negation_flag, set_request_context,
)
import uuid as _uuid
from services.v2.pii_scrubber import PIIScrubber
from services.v2.rate_limiter import RateLimiter
from services.v2.special_intents import detect_special_intent
from services.v2.executor import ToolExecutor
from services.v2.pending_mutation import (
    PendingMutationStore,
    STAGE_SINGLE, parse_reply,
)
from services.v2.pending_clarify import (
    PendingClarifyStore,
    STAGE_ACTION as PENDING_STAGE_ACTION,
    STAGE_ACTION_GLOBAL as PENDING_STAGE_ACTION_GLOBAL,
    STAGE_TOOL as PENDING_STAGE_TOOL,
)
from services.v2.pending_params import PendingParams, PendingParamsStore
from services.v2.optional_extractor import OptionalParamExtractor
from services.v2.api_error_translator import ApiErrorTranslator
from services.v2.param_labeler import ParamLabeler
from services.v2 import param_ui
from services.v2 import type_resolver
from services.v2.conversation_history import (
    ConversationHistoryStore, ConversationTurn,
)
from services.v2 import crisis_detector
from services.v2 import input_sanitizer
from services.v2 import multi_intent_detector
from services.v2 import negation_handler
from services.v2 import meta_intents
from services.v2 import clarify_ui
from services.v2.gdpr_audit import GdprAuditStore

logger = logging.getLogger(__name__)


@dataclass
class V2Engine:
    rate_limiter: RateLimiter
    pii: PIIScrubber
    identity: IdentityContext
    intent_type: IntentTypeClassifier
    basics: DriverBasicsAnchor
    router: LLMRouter
    formatter_llm: LLMFormatter
    flow_engine: FlowEngine
    flow_store: FlowStateStore
    executor: ToolExecutor
    pending_mut_store: PendingMutationStore
    # Telemetry sink for production observability (active learning input).
    # Optional: if None, no logging. Failures here NEVER block user request.
    telemetry: Optional[TelemetryLogger] = None
    # Top-3 cards UX state — persists candidates between turns so user's
    # "1"/"2"/"3"/"ne" reply maps to the correct tool.
    pending_clarify_store: Optional[PendingClarifyStore] = None
    # Multi-turn context — last 3-5 turns. Optional.
    conversation_history_store: Optional[ConversationHistoryStore] = None
    # GDPR + handover audit log. Optional: if None, side_effects are skipped
    # with a loud warning (better than silently lying to the user).
    gdpr_audit_store: Optional[GdprAuditStore] = None
    # operation_id → intent_summary map for the clarify-cards UX. Built
    # once at factory time from tool_knowledge_base.json. Used by the
    # FALLBACK handler to render top-3 candidates with human-readable
    # descriptions. Empty dict disables clarify-card rendering (engine
    # falls back to the generic text fallback message).
    tkb_intents: dict = field(default_factory=dict)
    # Per-(tenant, persona) catalog narrowing (Phase E 2026-05-16). When
    # None, router sees the full 950-tool catalog (legacy behavior). When
    # set, every route() call passes tool_filter so anchor + LLM see only
    # ~50-150 candidates relevant to this user's role+tenant.
    catalog_scoper: Optional[CatalogScoper] = None
    # Param-asking state (Filip 2026-05-17): persists collected/remaining
    # params between turns when the engine asks for missing required (or
    # opted-in optional) values. None = feature disabled (skip param ask).
    pending_params_store: Optional[PendingParamsStore] = None
    # tool_id → {param_name: param_def_dict}. Built once at factory time
    # from the same registry the router uses. Used to (a) compute missing
    # required params, (b) list optional params, (c) parse user answers
    # by `param_type`. Empty dict disables param-asking entirely.
    tool_parameters: dict = field(default_factory=dict)
    # {param_name_lower: get_tool_id} for *TypeId FK resolution (Filip 2026-05-27)
    typeid_map: dict = field(default_factory=dict)
    # LLM extractor for OPTIONAL params (Filip 2026-05-17). When set, the
    # engine collects all optionals in ONE turn from a free-text reply
    # instead of iterating one-by-one. None = degrade to "skip all optionals"
    # after user reply (no iteration fallback — Filip rejected that UX).
    optional_extractor: Optional[OptionalParamExtractor] = None
    # LLM translator for 4xx API errors (Faza 2 Filip 2026-05-17). Converts
    # raw API error body into a Croatian WhatsApp message ("Nedostaje datoteka",
    # "Nemaš ovlasti", etc) instead of generic "Tehnički problem". None =
    # always show generic message (safe default for tests without LLM).
    api_error_translator: Optional[ApiErrorTranslator] = None
    # LLM-generated Croatian labels for tool parameters (Faza 3 Filip 2026-05-17).
    # Replaces curated PARAM_LABELS dict — works for ANY param in any of the
    # 950 tools via 3-tier resolution (preloaded JSON → Redis cache → LLM).
    # None = fall back to humanize_param_name (ugly but functional).
    param_labeler: Optional[ParamLabeler] = None
    # op_ids whose registry metadata is incomplete (MISSING_BODY or
    # LIKELY_MISSING_BODY from scripts/audit_registry_body_schemas.py).
    # When a tool from this set hits the mutation gate confirm, the bot
    # prefixes a Croatian warn line so user knows it may fail with 422.
    # Filip 2026-05-17 Faza 9. Empty set = no warn ever (safe default).
    risky_tool_ids: set = field(default_factory=set)

    # ---- Internal helpers (Tier-A simplification 2026-05-08) ----
    @classmethod
    def _minimal_identity(cls, identity: IdentitySnapshot) -> dict:
        """Identity context the executor needs at execute time:
          - tenant_id → x-tenant header (multi-tenant isolation)
          - person_id / vehicle_id → auto-injected into `context` params the
            registry marks dependency_source="context" (Filip 2026-05-23 fix #3;
            e.g. post_AddMileage's required VehicleId). Keyed by the param's
            context_key, so the keys here MUST match those context_key values.
        """
        return {
            "tenant_id": identity.tenant_id,
            "person_id": identity.person_id,
            "vehicle_id": identity.vehicle_id,
            # Filip 2026-05-24: close the 28-param gap — these were marked
            # context but identity never provided them → silent 422. Key MUST
            # be "orgunit_id" (no underscore) to match the registry context_key.
            "company_id": identity.company_id,
            "orgunit_id": identity.org_unit_id,
        }

    async def process_message_chunked(
        self, phone: str, query: str,
    ) -> list[str]:
        """Convenience wrapper: process_message + WhatsApp 4096-char split.

        Returns list of WhatsApp-deliverable chunks (1 element if the
        response fits in one message; (1/N)-suffixed parts otherwise).
        Use this from the worker/webhook layer instead of calling
        `process_message` + chunking manually — keeps the integration
        contract single-edged.
        """
        from services.v2.latency_ux import chunk_for_whatsapp
        full = await self.process_message(phone, query)
        return chunk_for_whatsapp(full)

    async def process_message(self, phone: str, query: str) -> str:
        """Top-level dispatcher. Always returns a Croatian string.

        Wraps `_dispatch_message` with two responsibilities:
          1. Set per-request telemetry context (correlation_id, turn_number)
             BEFORE dispatch so all downstream telemetry events have it.
          2. Append the (user, bot) turn to conversation_history AFTER
             dispatch so turn_number increments correctly across turns.
        """
        # ---- Per-request telemetry context ----
        # Fresh UUID per webhook (NOT chained via WhatsApp message_id).
        # Cross-turn linking happens in KQL via phone+timestamp ordering.
        # turn_number is conversation-history length + 1 (best-effort —
        # if history store is unavailable, we still log with turn=0).
        correlation_id = _uuid.uuid4().hex
        turn_number = 0
        if self.conversation_history_store is not None:
            try:
                hist = await self.conversation_history_store.load(phone)
                turn_number = (len(hist) if hist else 0) + 1
            except Exception:  # noqa: BLE001 — telemetry init must not fail request
                turn_number = 0
        set_request_context(
            correlation_id=correlation_id,
            turn_number=turn_number,
        )

        response = await self._dispatch_message(phone, query)

        # F5.1 fix: append every turn to conversation_history so
        # turn_number increments across turns. Best-effort — never blocks
        # the user response on telemetry persistence.
        # Faza 12.1 (Filip 2026-05-18): PII-scrub query before persisting.
        # Without this, OIB/IBAN/phone in user text leaks into Redis cache
        # under v2_conv_history:{phone} for 30 min — GDPR breach.
        if self.conversation_history_store is not None and response:
            try:
                scrubbed_user = self.pii.scrub(query).scrubbed_text
                await self.conversation_history_store.append(
                    phone,
                    ConversationTurn(
                        user=scrubbed_user[:200],
                        bot=response[:200],
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

        return response

    async def _dispatch_message(self, phone: str, query: str) -> str:
        """Routing dispatch (extracted from process_message for F5.1 wrapper).

        Pre-condition: process_message has set telemetry context. This
        method MUST NOT be called directly by webhooks — always go through
        process_message so the conversation_history append fires.
        """
        # ---- L-1 Rate Limiter ----
        rl = await self.rate_limiter.check(phone)
        if not rl.allowed:
            await self._log_telemetry(kind="layer_exit:rate_limit")
            return rl.user_message

        # ---- L0.5 PII Scrubber ----
        scrubbed = self.pii.scrub(query)
        safe_query = scrubbed.scrubbed_text
        # Persist the redaction kinds (NOT the original values) so the
        # operator can answer "did the bot ever see an OIB on date X".
        # In-memory scrubbed.redactions is per-request only — without
        # this telemetry write there is no audit trail.
        if scrubbed.redactions:
            await self._log_telemetry(
                kind="pii_redacted",
                redactions=[r.kind for r in scrubbed.redactions],
            )

        # ---- Negation flag (Damir-feedback signal) ----
        # User explicitly says "nije točno" → mark this turn so the bot
        # operator can spot wrong-routing patterns in KQL. Exact match
        # only; no heuristics, no synonym lists. Phrase is taught by
        # the formatter hint appended to read/mutate execute responses.
        set_negation_flag(
            safe_query.strip().casefold() == "nije točno"
        )

        # ---- L0.6 Input sanitizer (direct prompt injection guard) ----
        # Defends against role-injection markers, "ignore previous"
        # imperatives, mutation-gate framing bypass, token-flood DoS.
        # See services/v2/input_sanitizer.py.
        sanitized = input_sanitizer.sanitize(safe_query)
        if sanitized.should_block:
            await self._log_telemetry(
                kind="input_blocked",
                tenant_id="",
                query="(blocked for privacy)",
                extra={
                    "reason": sanitized.blocked_reason,
                    "warnings": sanitized.warnings[:5],
                },
            )
            return input_sanitizer.block_message()
        safe_query = sanitized.cleaned

        # ---- L0 Identity ----
        identity = await self.identity.resolve(phone)

        # ---- Pending param-collection continuation? ----
        # If we asked for a missing required param (or offered optionals) last
        # turn, this message is the answer. Run BEFORE pending_mutation /
        # pending_clarify so "R-123" or "ne" is interpreted as a param value /
        # optional-skip, NOT a confirm or a clarify pick.
        if self.pending_params_store is not None:
            pending_p = await self.pending_params_store.load(phone)
            if pending_p is not None:
                response = await self._resolve_pending_params(
                    phone, pending_p, safe_query, identity,
                )
                if response is not None:
                    await self._log_telemetry(
                        kind="layer_exit:pending_params_resolved",
                        tenant_id=identity.tenant_id or "",
                    )
                    return response
                # _resolve_pending_params returned None → state already
                # cleared, treat current message as fresh query.

        # ---- Pending mutation continuation? ----
        # If a confirm prompt is outstanding for this phone, the user's
        # next message is the reply to it. Must run BEFORE flow / L1 /
        # L2a so "Da" is interpreted as "execute pending", not as a
        # new request.
        pending = await self.pending_mut_store.load(phone)
        if pending is not None:
            await self._log_telemetry(
                kind="layer_exit:pending_mutation_continuation",
                tenant_id=identity.tenant_id or "",
            )
            return await self._continue_pending_mutation(
                phone, pending, safe_query, identity,
            )

        # ---- "Nije točno" reoffer handler (Filip 2026-06-05) ----
        # Fires when user signals dissatisfaction with the LAST executed
        # tool. We saved cosine top-50 + shown ids when the tool ran, so
        # we offer next 3 candidates instead of falling through to a fresh
        # action picker (which would just present "POGLEDATI/UNIJETI/..."
        # again — frustrating). Catches BOTH L3 and L2b paths via shared
        # pending_clarify.can_reoffer flag.
        _q_reoffer = safe_query.strip().lower().rstrip(".!?,;")
        if (_q_reoffer in self._REOFFER_PHRASES
                and self.pending_clarify_store is not None):
            _pc_reoffer = await self.pending_clarify_store.load(phone)
            if _pc_reoffer is not None and _pc_reoffer.can_reoffer:
                await self._log_telemetry(
                    kind="layer_exit:nije_tocno_reoffer",
                    tenant_id=identity.tenant_id or "",
                )
                return await self._handle_reoffer(phone, _pc_reoffer, identity)

        # ---- Pending clarify continuation? (Top-3 cards reply) ----
        # If we rendered Top-3 cards last turn and saved candidates,
        # interpret "1"/"2"/"3"/"ne" as a pick. Falls through to fresh
        # routing if user re-issues a different query.
        if self.pending_clarify_store is not None:
            pending_cl = await self.pending_clarify_store.load(phone)
            if pending_cl is not None:
                resolved = await self._resolve_pending_clarify(
                    phone, pending_cl, safe_query, identity,
                )
                if resolved is not None:
                    await self._log_telemetry(
                        kind="layer_exit:pending_clarify_resolved",
                        tenant_id=identity.tenant_id or "",
                    )
                    return resolved
                # User typed something else — clear stale pending and
                # treat current message as new query
                await self.pending_clarify_store.clear(phone)

        # ---- L4 Flow continuation? ----
        existing_flow = await self.flow_store.load(phone)
        if existing_flow is not None:
            if existing_flow.flow_name not in FLOWS:
                # Flow definition was removed since this state was saved.
                # Drop the orphaned state instead of crashing.
                await self.flow_store.clear(phone)
                logger.warning(
                    "dropped orphaned flow state phone=%s flow=%s",
                    phone[-4:], existing_flow.flow_name,
                )
            else:
                # DIO 2 fix (Filip 2026-05-29): if user types a query that
                # CLEARLY starts a DIFFERENT flow (e.g. pending booking + new
                # "prijavi kvar"), drop the stale flow and start fresh rather
                # than mis-routing the new query through old flow's parser.
                _new_flow = self._guess_flow_name(safe_query)
                # DIO 3 fix (Filip 2026-05-29): also abort the flow if the
                # message starts with an obvious non-flow action verb (obriši/
                # pokaži/promijeni/...). Otherwise the user stays trapped
                # in an ASK_PERIOD loop typing "obriši rezervaciju" forever.
                _q_lo = safe_query.lower().lstrip().replace("š","s").replace("ž","z").replace("č","c").replace("ć","c").replace("đ","d")
                _is_fresh_action = any(
                    _q_lo.startswith(v) for v in
                    ("obrisi", "brisi", "pokazi", "prikazi", "daj", "promijeni",
                     "izmijeni", "azuriraj", "izlistaj", "lista", "moje ", "moja ", "moj ")
                )
                if _new_flow and _new_flow != existing_flow.flow_name:
                    logger.info(
                        "user switched flows mid-stream: %s → %s (phone ****%s)",
                        existing_flow.flow_name, _new_flow, phone[-4:],
                    )
                    await self.flow_store.clear(phone)
                    # Fall through to fresh routing below (will hit _start_flow)
                elif _is_fresh_action:
                    logger.info(
                        "user abandoned flow with fresh action verb (phone ****%s, flow=%s)",
                        phone[-4:], existing_flow.flow_name,
                    )
                    await self.flow_store.clear(phone)
                    # Fall through to fresh routing
                else:
                    await self._log_telemetry(
                        kind="layer_exit:flow_continuation",
                        tenant_id=identity.tenant_id or "",
                        extra={"flow_name": existing_flow.flow_name},
                    )
                    return await self._continue_flow(
                        phone, existing_flow, safe_query, identity=identity,
                    )

        # ---- L0.7 Crisis detection (ETHICAL OBLIGATION) ----
        # MUST be the FIRST inline-detection layer — before negation/multi/meta —
        # so a query like "ne želim više živjeti, otkaži rezervaciju" hits crisis
        # response, not negation handler. Drivers are a high-stress profession;
        # bot must redirect to crisis hotline (Plavi telefon 116 123) on
        # suicidal/self-harm signals and NOT continue to fleet API calls.
        # False-positive rate is near-zero (deterministic Croatian phrase
        # patterns + figurative-usage guards like "ubit ću tu lozinku").
        crisis = crisis_detector.detect(safe_query)
        if crisis.detected:
            await self._log_telemetry(
                kind="crisis_signal",
                tenant_id=identity.tenant_id or "",
                query="(scrubbed for privacy)",  # do NOT log raw text
                extra={"severity": crisis.severity},
            )
            return crisis.response

        # ---- L0.75 Standalone negation handler ----
        # User says "nemoj rezervirati" / "ne, otkaži, odustajem" without
        # an active pending state. Acknowledge politely instead of
        # routing to a tool whose verb appears in the message.
        # Pending-state handler runs earlier and parses "Da/Ne" for
        # active confirmations — this layer is for STANDALONE negation.
        neg = negation_handler.detect(safe_query)
        if neg.detected:
            await self._log_telemetry(
                kind="negation_standalone",
                tenant_id=identity.tenant_id or "",
                query=safe_query,
            )
            return neg.response

        # ---- L0.8 Multi-intent detection ----
        # If user packed 2+ intents in one msg ("pokaži km i rezerviraj
        # sutra"), router can only handle one. Render clarify prompt
        # asking which goes first. See services/v2/multi_intent_detector.
        multi = multi_intent_detector.detect(safe_query)
        if multi.detected:
            await self._log_telemetry(
                kind="multi_intent",
                tenant_id=identity.tenant_id or "",
                query=safe_query,
                extra={"parts": multi.parts[:3]},
            )
            return multi.clarify_message

        # ---- L0.85 Meta-intents (self-reference / bug report / OOS) ----
        # "tko si ti" / "kako si" — answered inline, no LLM/API call.
        # NOTE: handoff/handover triggers handled by L1 special_intents
        # (which carries the queue_human_handover side-effect).
        meta = meta_intents.detect(safe_query)
        if meta.detected:
            await self._log_telemetry(
                kind=f"meta_intent:{meta.kind}",
                tenant_id=identity.tenant_id or "",
                query=safe_query,
            )
            return meta.response

        # ---- L1 Special Intents ----
        special = detect_special_intent(
            safe_query,
            is_first_contact=identity.is_first_contact,
            first_name=identity.first_name,
            vehicle_name=identity.vehicle_name,
            licence_plate=identity.licence_plate,
            last_mileage=identity.last_mileage,
            company_name=identity.company_name,
        )
        if special is not None:
            # Dispatch side_effects BEFORE returning. GDPR delete/export
            # and human-handover need an audit-trail entry — without it,
            # the "queued in 48h" message we send the user is a lie.
            await self._handle_special_side_effects(special, identity)
            await self._log_telemetry(
                kind=f"layer_exit:special_intent:{special.intent}",
                tenant_id=identity.tenant_id or "",
                query=safe_query,
            )
            return special.response

        # ---- L1.5 Unknown phone gate ----
        # If identity could not resolve a person_id AND it's not the
        # first contact (welcome would have handled that), the user
        # cannot proceed — no tenant_id means downstream API calls fail.
        # Surface a clear enrollment message instead of silent failure.
        if not identity.is_known and not identity.is_first_contact:
            await self._log_telemetry(
                kind="unknown_phone_gate",
                tenant_id="",
                query=safe_query,
            )
            return (
                "Bok! Tvoj broj još nije povezan s računom u sustavu MobilityOne.\n\n"
                "Da bismo nastavili, kontaktiraj svog managera ili podršku — "
                "trebaju te dodati u sustav s ovim brojem telefona.\n\n"
                "Kad to bude gotovo, javim ti se opet."
            )

        # Dormant V2_USE_TOOL_USE / V2_USE_UNIFIED_RESPONDER / V2_USE_V3_ROUTER
        # branches removed 2026-05-12. The new L3 LLM router (Phase 4) will
        # replace the recognition + confidence_gate path entirely.

        # ---- L2a Intent Type ----
        itype = await self.intent_type.classify(safe_query)

        # MED-7 (Filip 2026-05-29): orphan-confirm guard. If user sends just
        # "Da"/"Ne"/"Može"/... and there's NO pending mutation (TTL expired,
        # cache lost, or pure mistake), tell them clearly instead of letting
        # the LLM router try to route a 2-char message into some random tool.
        _bare = safe_query.strip().lower().rstrip(".!?,;")
        if _bare in {"da", "yes", "ok", "može", "moze", "potvrđujem", "potvrdjujem",
                     "ne", "no", "odustani", "otkaži", "otkazi"}:
            return (
                "Nemam aktivnu potvrdu za tebe (ili je istekla nakon 5 minuta). "
                "Pošalji upit ponovo, pa potvrdi nakon eha."
            )

        # ---- Flow request? Start flow directly ----
        # DIO 2 fix (Filip 2026-05-29): check flow keywords BEFORE driver_basics.
        # "upiši 50000 km" / "prijavi kvar" / "rezerviraj sutra" must start a
        # flow even if L2a mis-classified them (or if driver_basics anchor
        # picks up the "km"/"vozilo" word and would otherwise hijack them).
        # _guess_flow_name is keyword-deterministic so it's safe to gate here.
        flow_name = self._guess_flow_name(safe_query)
        if flow_name and flow_name in FLOWS and (
            itype.kind == KIND_FLOW_REQUEST
            or itype.kind == KIND_ACTION_OR_COMPLAINT  # mis-classified mutation
        ):
            return await self._start_flow(phone, flow_name, identity, safe_query)

        # ---- L2b Driver Basics ----
        # DIO 1 fix (Filip 2026-05-29): allow driver-basics match REGARDLESS of
        # L2a kind for KNOWN users — L2a sometimes mis-classifies short personal-
        # info queries ("tko sam ja", "moje ime") as OTHER. match() has a STRONG
        # cosine threshold + negative anchors, so false positives are unlikely.
        # DIO 2 fix (Filip 2026-05-29): but skip L2b entirely if the message
        # already triggered a flow keyword above — prevents "upiši 50000 km"
        # from being caught by the km anchor.
        # DIO 3 fix (Filip 2026-05-29): also skip L2b if the message starts
        # with a clear mutating action verb (dodaj/obriši/promijeni/upiši).
        # Otherwise "dodaj trošak goriva 50 eura" gets stolen by the vehicle
        # anchor → returns car info instead of routing to expense POST.
        _q_lo_b = safe_query.lower().lstrip().replace("š","s").replace("ž","z").replace("č","c").replace("ć","c").replace("đ","d")
        _starts_with_mut_verb = any(
            _q_lo_b.startswith(v) for v in
            ("dodaj", "obrisi", "brisi", "promijeni", "izmijeni",
             "azuriraj", "upisi", "unesi", "prijavi", "kreiraj",
             "rezervir", "otkazi")
        )
        if identity.is_known and not flow_name and not _starts_with_mut_verb:
            basics_match = await self.basics.match(safe_query)
            if basics_match.matched:
                await self._log_telemetry(
                    kind="layer_exit:driver_basics_match",
                    tenant_id=identity.tenant_id or "",
                    query=safe_query,
                )
                _basics_reply = await self._format_basics(identity, safe_query)
                # Filip 2026-06-05: save reoffer state — user who got L2b
                # match but wanted something else can send "nije točno" and
                # we'll re-route the original query through L3 router with
                # L2b excluded.
                if self.pending_clarify_store is not None:
                    try:
                        await self.pending_clarify_store.save(
                            phone, candidates=[], original_query=safe_query,
                            stage=PENDING_STAGE_TOOL,
                            all_candidate_ids=[],
                            shown_tool_ids=["__L2B_DRIVER_BASICS__"],
                            last_executed_tool="__L2B_DRIVER_BASICS__",
                            can_reoffer=True,
                        )
                    except Exception:  # noqa: BLE001 — never break the reply
                        pass
                return _basics_reply

        # ---- Model A: Universal action picker (Filip direktiva 2026-05-17) ----
        # Direct LLM auto-execute is gone — router accuracy isn't trustworthy
        # enough. Every message that wasn't caught by an earlier layer
        # (crisis/welcome/flow/basics/...) now goes through the 3-turn cascade:
        #   Turn 1 (this turn): save original query, render action picker
        #   Turn 2: user picks action → scoped L3 router → render tool picker
        #   Turn 3: user picks tool → mutation gate → execute (or confirm-Da)
        if self.pending_clarify_store is None:
            # Stripped-down test setups without clarify store: degrade to a
            # generic "tell me more" reply rather than auto-execute. Production
            # always wires the store (see make_v2_engine_for_production).
            await self._log_telemetry(
                kind="layer_exit:no_clarify_store",
                tenant_id=identity.tenant_id or "",
                query=safe_query,
            )
            return (
                "Nisam siguran kako pomoći. Reci jasnije što tražiš "
                "(npr. 'kolika mi je km', 'rezerviraj sutra', 'obriši rezervaciju 123')."
            )

        action_options = clarify_ui.build_action_picker_global()
        await self.pending_clarify_store.save(
            phone,
            candidates=[],  # filled after user picks action (Turn 2 → scoped router)
            original_query=safe_query,
            stage=PENDING_STAGE_ACTION_GLOBAL,
        )
        await self._log_telemetry(
            kind="clarify_action_global_shown",
            tenant_id=identity.tenant_id or "",
            query=safe_query,
        )
        return clarify_ui.render_text(
            action_options,
            header="Što želiš učiniti?",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _log_telemetry(self, **kwargs) -> None:
        """Best-effort structured log. Never raises — failures swallowed.

        Translates legacy call-site kwargs into the canonical 11-field
        TelemetryEvent shape. The 25+ call sites in this file still pass
        old field names (`kind`, `phone_hash`, `query`,
        `llm_confidence`, `candidates_top5`, etc.); this central
        translator maps them to the new shape so we don't have to edit
        every call site. `correlation_id` + `turn_number` are
        auto-injected from contextvars set at `process_message` entry.

        WARNING for future-readers: a call site that writes
        `phone_hash=...` is NOT logging that field — it gets dropped
        below. Anything passed must be in the post-translation kwargs
        accepted by `TelemetryEvent.__init__`.
        """
        if self.telemetry is None or not self.telemetry.enabled:
            return
        # candidates_top5 → competitors[:3] (preserves debug context in
        # single-event view; without this translation `competitors`
        # would always be empty since no call site writes it directly).
        candidates = kwargs.pop("candidates_top5", None)
        if candidates and "competitors" not in kwargs:
            kwargs["competitors"] = list(candidates)[:3]
        # Drop legacy fields with no equivalent in the canonical shape.
        for legacy in (
            "kind", "phone_hash", "extra",
            "anchor_top1", "anchor_score",
            "gate_decision", "gate_reason", "mutation_decision",
        ):
            kwargs.pop(legacy, None)
        # Renames
        if "query" in kwargs:
            kwargs["query_scrubbed"] = kwargs.pop("query")
        if "llm_confidence" in kwargs and "confidence" not in kwargs:
            kwargs["confidence"] = kwargs.pop("llm_confidence")
        else:
            kwargs.pop("llm_confidence", None)
        if "elapsed_ms" in kwargs and "latency_ms" not in kwargs:
            elapsed = kwargs.pop("elapsed_ms")
            kwargs["latency_ms"] = int(round(elapsed)) if elapsed else 0
        else:
            kwargs.pop("elapsed_ms", None)
        # executed_success → error: success means error stays None
        success = kwargs.pop("executed_success", None)
        if success is False and "error" not in kwargs:
            kwargs["error"] = "execution_failed"
        # executed_tool: if no tool_picked yet, use it; otherwise drop
        exec_tool = kwargs.pop("executed_tool", None)
        if exec_tool and not kwargs.get("tool_picked"):
            kwargs["tool_picked"] = exec_tool
        # clarify_options + clarify_chosen → clarify dict
        options = kwargs.pop("clarify_options", None)
        chosen = kwargs.pop("clarify_chosen", None)
        if options or chosen is not None:
            kwargs.setdefault("clarify", {
                "options": list(options or []),
                "picked": chosen,
            })
        # Inject is_negation flag (set by process_message entry on
        # exact-match "nije točno"). Default False if no request in
        # flight (e.g. background task logging).
        kwargs.setdefault("is_negation", get_negation_flag())
        try:
            await self.telemetry.log(TelemetryEvent(**kwargs))
        except Exception as e:  # noqa: BLE001 — telemetry must not affect user
            # First failure per process logs at warning; further failures
            # at debug to avoid log spam if the sink is hard-down.
            if not getattr(self, "_telemetry_warning_emitted", False):
                logger.warning("telemetry log dropped (further muted): %s", e)
                self._telemetry_warning_emitted = True
            else:
                logger.debug("telemetry log dropped: %s", e)
            return

    async def _handle_special_side_effects(
        self, special, identity: IdentitySnapshot,
    ) -> None:
        """Dispatch the side_effects tuple of a SpecialIntentMatch.

        GDPR delete/export and human-handover require an audit-trail entry.
        Without it, the "queued in 48h" message the user receives is a lie.
        Failures are logged but never block the user-facing response.
        """
        if not special.side_effects:
            return
        if self.gdpr_audit_store is None:
            logger.warning(
                "side_effects fired but gdpr_audit_store is None — "
                "audit trail missing for actions=%s, phone=%s",
                [se.get("action") for se in special.side_effects],
                identity.phone,
            )
            return
        from services.v2.telemetry import _correlation_id_var
        cid = _correlation_id_var.get() or ""
        for se in special.side_effects:
            action = se.get("action") or ""
            try:
                if action in ("audit_log_gdpr_delete", "audit_log_gdpr_export"):
                    await self.gdpr_audit_store.record_gdpr_request(
                        action=action,
                        tenant_id=identity.tenant_id,
                        phone=identity.phone,
                        person_id=identity.person_id,
                        correlation_id=cid,
                    )
                elif action == "queue_human_handover":
                    await self.gdpr_audit_store.record_handover_request(
                        tenant_id=identity.tenant_id,
                        phone=identity.phone,
                        person_id=identity.person_id,
                        correlation_id=cid,
                    )
                else:
                    logger.warning("unknown side_effect action=%s — skipping", action)
            except Exception as e:  # noqa: BLE001
                logger.warning("side_effect dispatch failed (%s): %s — action=%s",
                               type(e).__name__, e, action)

    async def _format_basics(
        self, identity: IdentitySnapshot, query: str,
    ) -> str:
        data = {
            "VehicleName":        identity.vehicle_name,
            "LicencePlate":       identity.licence_plate,
            "VIN":                identity.vin,
            "LastMileage":        identity.last_mileage,
            "LeasingCompany":     identity.leasing_company,
            "Co2Emission":        identity.co2_emission,
            "RegistrationExpiry": identity.registration_expiry,
            "FullName":           identity.full_name,
            "Phone":              identity.phone,
            "PersonId":           identity.person_id,
            "TenantId":           identity.tenant_id,
        }
        # Alias-first (fast, deterministic, no LLM) for the common driver
        # fields (km/registracija/vozilo…). On a miss (arbitrary field like
        # "boja"/"dobavljač"), LLM-format the full cached vehicle object so
        # Path-A answers ANY field (parity with Path-B), values verbatim.
        if formatter.field_hint_resolves(query, list(data.keys())):
            return formatter.format_response(
                template_id="vehicle_data_field",
                api_response_data=data,
                field_hint=query,
            ).text
        return await self._format_reply(
            query=query, tool_id="get_MasterData",
            api_data=identity.vehicle or data, identity=identity,
            field_hint=query, template_id="vehicle_data_field",
        )

    def _identity_summary(self, identity: IdentitySnapshot) -> str:
        if not identity.is_known:
            return "(unknown user)"
        bits = []
        if identity.first_name:
            bits.append(identity.first_name)
        if identity.vehicle_name:
            bits.append(f"vozilo {identity.vehicle_name}")
        if identity.licence_plate:
            bits.append(f"({identity.licence_plate})")
        return ", ".join(bits) or "(driver)"

    async def _format_reply(
        self, *, query: Optional[str], tool_id: str, api_data,
        identity: IdentitySnapshot,
        field_hint: Optional[str] = None,
        template_id: Optional[str] = None,
        extra_context: Optional[dict] = None,
    ) -> str:
        """Question-aware LLM formatting of a tool result, grounded in the
        JSON (values are taken verbatim from the source → no hallucination;
        the LLM only selects which field(s) answer the user's question). Falls
        back to the deterministic template formatter if the LLM call fails."""
        try:
            res = await self.formatter_llm.format(
                query=query or "",
                tool_id=tool_id,
                api_data=api_data,
                identity_summary=self._identity_summary(identity),
            )
            if res.error is None and res.text:
                return res.text
        except Exception as e:  # noqa: BLE001 — never let formatting break the reply
            logger.warning("LLM formatter failed (%s); template fallback", e)
        return formatter.format_response(
            template_id=template_id, api_response_data=api_data,
            field_hint=field_hint, extra_context=extra_context,
        ).text

    async def _invalidate_identity(self, phone: str) -> None:
        """Best-effort: drop the identity cache after a mutation so the next
        read refetches fresh. Never raises — invalidation is an optimization
        (the 30s TTL is the safety net), and some wirings lack the context."""
        ctx = getattr(self, "identity", None)
        if ctx is None:
            return
        try:
            await ctx.invalidate(phone)
        except Exception as e:  # noqa: BLE001
            logger.warning("identity invalidate failed: %s", e)

    @staticmethod
    def _guess_flow_name(query: str) -> Optional[str]:
        # Keyword short-circuit only for high-confidence flow requests —
        # avoids an L3 LLM call when the trigger is unambiguous. L3 is
        # still the real path for everything else.
        # DIO 2 fix (Filip 2026-05-29): normalize HR diacritics so "upiši"
        # matches "upis", "šteta" matches "steta", etc. Without this,
        # "upiši 50000 km" was missed and L2b stole it.
        q = (
            query.lower()
                 .replace("š", "s").replace("ž", "z").replace("č", "c")
                 .replace("ć", "c").replace("đ", "d")
        )
        # DIO 3 fix (Filip 2026-05-29): "rezerv" was too broad — matched
        # "rezerviraj" (verb=book) AND "obriši rezervaciju" (noun=delete it).
        # Use the verb prefix "rezervir" so we only catch booking-creation
        # intents; "rezervaciju/rezervacije" (noun) falls through to L3.
        if any(w in q for w in ("rezervir", "booking", "auto sutra", "vozilo za")):
            return "booking"
        if any(w in q for w in ("upis", "unesi", "stanje km", "evo km")):
            return "mileage"
        if any(w in q for w in ("prijav", "kvar", "stet", "osteti", "havar")):
            return "case"
        return None

    async def _start_flow(
        self, phone: str, flow_name: str, identity: IdentitySnapshot,
        query: str = "",
    ) -> str:
        ctx = {
            "person_id":     identity.person_id,
            "vehicle_id":    identity.vehicle_id,
            "vehicle_name":  identity.vehicle_name,
        }
        # HIGH-1 fix (Filip 2026-05-28): pre-populate flow slots from initial
        # query so user doesn't have to repeat info already given. Flow engine
        # auto-skips ASK_* steps whose slot is populated (see _advance_or_prompt).
        # Examples:
        #   "rezerviraj sutra 9-15"          → ctx.from_time + to_time
        #   "upiši 145000 km"                → ctx.mileage_value
        #   "prijavi kvar na bočnoj kameri"  → ctx.description
        if flow_name == "booking" and query:
            try:
                from services.v2.flow_engine import _parse_period
                parsed = _parse_period(query)
                if parsed and parsed.get("from_time") and parsed.get("to_time"):
                    ctx["period_text"] = parsed.get("period_text") or query
                    ctx["from_time"]   = parsed["from_time"]
                    ctx["to_time"]     = parsed["to_time"]
            except Exception as e:  # noqa: BLE001 — best effort
                logger.warning("booking period pre-fill failed: %s", e)
        elif flow_name == "mileage" and query:
            # Extract first plausible km number ("upiši 145000 km" → 145000)
            import re as _re
            m = _re.search(r"\b(\d{1,7})\s*(?:km|kilom)?\b", query.lower())
            if m:
                try:
                    ctx["mileage_value"] = int(m.group(1))
                except ValueError:
                    pass
        elif flow_name == "case" and query:
            # Treat remainder of query as case description (strip flow keyword)
            stripped = query
            for kw in ("prijavi kvar", "prijava kvara", "prijavi", "kvar", "šteta", "ošteti"):
                stripped = stripped.replace(kw, "").strip()
            if stripped and len(stripped) >= 3:
                ctx["description"] = stripped
        outcome = self.flow_engine.start(flow_name, ctx)
        # DIO 2 fix (Filip 2026-05-29): when start() auto-skips pre-filled
        # ASK_* slots and lands on EXEC_LOOKUP, the outcome has response=None
        # → would render "Pokrećem postupak." placeholder. Drive the lookup
        # loop here so the user gets the real next prompt (e.g. ASK_CHOICE
        # for vehicle selection in booking).
        outcome = await self._drive_flow_lookups(phone, outcome, identity)
        if outcome is None:
            # _drive_flow_lookups already cleared store + returned an error
            # message; here we re-build it because callers expect a str.
            return (
                "Trenutno ne mogu dohvatiti podatke za ovaj korak. "
                "Pokušaj ponovo malo kasnije."
            )
        if outcome.new_state is not None:
            await self.flow_store.save(phone, outcome.new_state)
        return outcome.response or "Pokrećem postupak."

    async def _drive_flow_lookups(
        self, phone: str, outcome, identity: Optional[IdentitySnapshot],
    ):
        """Loop through EXEC_LOOKUP steps (response=None) until the flow
        reaches a user-facing prompt or terminates. Returns the final outcome,
        OR None if a lookup failed/empty and store was already cleared.
        Bounded against malformed flow data (max 4 iterations).
        """
        for _ in range(4):
            if not (
                outcome.kind == OUTCOME_PROMPT
                and outcome.tool_id
                and outcome.response is None
            ):
                return outcome
            lookup_state = outcome.new_state
            if lookup_state is None:
                return outcome
            choices = await self._run_flow_lookup(
                outcome.tool_id, outcome.params, identity,
            )
            if choices is None:
                await self.flow_store.clear(phone)
                return None
            if not choices:
                await self.flow_store.clear(phone)
                # Sentinel: tell caller "no options"
                from types import SimpleNamespace
                return SimpleNamespace(
                    kind=OUTCOME_PROMPT, response=(
                        "Nema dostupnih opcija za traženo. "
                        "Pokušaj s drugim parametrima."
                    ), new_state=None, tool_id=None, params=None,
                )
            outcome = self.flow_engine.handle(
                lookup_state, "", lookup_result=choices,
            )
        return outcome

    async def _continue_flow(
        self, phone: str, state, user_input: str,
        identity: Optional[IdentitySnapshot] = None,
    ) -> str:
        outcome = self.flow_engine.handle(state, user_input)

        # Resolve EXEC_LOOKUP steps inline: the flow signals a read call with
        # OUTCOME_PROMPT + tool_id + response=None. We run it, shape the rows
        # into ASK_CHOICE options, feed them back, and loop until the flow asks
        # the USER something or finishes. Bounded against malformed flow data.
        # (This is what makes the booking lookup actually run — previously the
        # engine returned the literal "..." here, so booking died on this step.)
        for _ in range(4):
            if not (
                outcome.kind == OUTCOME_PROMPT
                and outcome.tool_id
                and outcome.response is None
            ):
                break
            lookup_state = outcome.new_state or state
            choices = await self._run_flow_lookup(
                outcome.tool_id, outcome.params, identity,
            )
            if choices is None:  # lookup call failed
                await self.flow_store.clear(phone)
                return (
                    "Trenutno ne mogu dohvatiti podatke za ovaj korak. "
                    "Pokušaj ponovo malo kasnije."
                )
            if not choices:  # call ok but nothing available
                await self.flow_store.clear(phone)
                return (
                    "Nema dostupnih opcija za traženo. "
                    "Pokušaj s drugim parametrima."
                )
            outcome = self.flow_engine.handle(
                lookup_state, "", lookup_result=choices,
            )

        if outcome.kind == OUTCOME_INVALID:
            if outcome.new_state is not None:
                await self.flow_store.save(phone, outcome.new_state)
            return outcome.response

        if outcome.kind == OUTCOME_PROMPT:
            if outcome.new_state is not None:
                await self.flow_store.save(phone, outcome.new_state)
            return outcome.response or "..."

        if outcome.kind == OUTCOME_CANCELLED:
            await self.flow_store.clear(phone)
            return outcome.response or "Odustao sam."

        if outcome.kind == OUTCOME_EXECUTE:
            # ORCH-1 fix (Filip 2026-05-20): pass real tenant identity, not {}.
            # executor.execute refuses tenant-scoped tools with missing_tenant_id
            # if identity_summary lacks tenant_id.
            identity_summary = (
                self._minimal_identity(identity) if identity is not None else {}
            )
            # Same coercion as the [C] path so flow params ship normalized too
            # (HR "12,5"→12.5, "17.05.2026"→ISO, whole-float→int).
            params = self._coerce_llm_params(outcome.tool_id, outcome.params)
            # Fix A (Filip 2026-05-28): anti-replay execution lock, mirroring the
            # general path (_continue_pending_mutation). Without it, a concurrent
            # double "Da" (Infobip retry / double-tap) on the flow confirm step
            # would run the write twice → two bookings / two mileage entries.
            if not await self.pending_mut_store.try_acquire_execution(phone):
                return (
                    "Operacija je već u tijeku — pričekaj sekundu i provjeri "
                    "potvrdu sljedeće poruke."
                )
            try:
                exec_result = await self.executor.execute(
                    tool_id=outcome.tool_id,
                    params=params,
                    identity_summary=identity_summary,
                )
                if not exec_result.success:
                    # ORCH-4 fix: do NOT clear flow state on failure — keep it so
                    # the user can retry the confirm instead of losing progress.
                    return await self._render_execution_failure(
                        exec_result, outcome.tool_id,
                        generic=f"Akcija nije uspjela: {exec_result.error}",
                    )
                # Success → now safe to clear flow state.
                await self.flow_store.clear(phone)
                # Driver data may have changed (e.g. mileage write) → drop
                # identity cache so the next read refetches fresh (2026-05-25).
                await self._invalidate_identity(phone)
            finally:
                await self.pending_mut_store.release_execution(phone)
            r = formatter.format_response(
                template_id="mutation_success",
                api_response_data=exec_result.data,
                extra_context={"action": "Akcija"},
            )
            return r.text

        if outcome.kind == OUTCOME_DONE:
            await self.flow_store.clear(phone)
            return "Postupak završen."

        return "Postupak."

    @staticmethod
    def _extract_rows(data: object) -> list:
        """Unwrap a MobilityOne list response (bare list or `{Data:[...]}` /
        other common envelope keys) into a list of row dicts. Local copy keeps
        the flow-lookup self-contained (no cross-module private access)."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("Data", "data", "Result", "Results", "Items", "items", "value"):
                if isinstance(data.get(k), list):
                    return data[k]
            if data.get("Id") is not None or data.get("id") is not None:
                return [data]
        return []

    async def _run_flow_lookup(
        self, tool_id: str, params: dict,
        identity: Optional[IdentitySnapshot],
    ) -> Optional[list]:
        """Run a flow EXEC_LOOKUP read call and shape the rows into ASK_CHOICE
        options. Returns a list of `{**row, "label", "Id"}` dicts, `[]` if the
        call succeeded but returned nothing, or `None` if the call failed.

        Defensive on shape (M1 response shape is verified at smoke, not here):
        envelope is unwrapped via the shared extractor; `Id` is taken with
        fallbacks (it's the load-bearing field for the follow-up mutation);
        `label` falls back through the common vehicle display fields."""
        try:
            res = await self.executor.execute(
                tool_id=tool_id, params=params or {},
                identity_summary=(
                    self._minimal_identity(identity) if identity is not None else {}
                ),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("flow lookup %s raised: %s", tool_id, e)
            return None
        if not res.success:
            logger.warning("flow lookup %s failed: %s", tool_id, res.error)
            return None
        rows = self._extract_rows(res.data)
        choices: list = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            rid = r.get("Id") or r.get("id") or r.get("VehicleId")
            if rid is None:
                continue
            # Field order verified against a live get_AvailableVehicles row
            # (Filip's dev creds 2026-05-27): DisplayName + LicencePlate exist;
            # FullVehicleName/Name do not — kept as harmless fallbacks for
            # other future lookup flows.
            label = (
                r.get("DisplayName") or r.get("LicencePlate")
                or r.get("FullVehicleName") or r.get("Name") or str(rid)
            )
            choices.append({**r, "label": str(label), "Id": rid})
        return choices

    _RISKY_WARN_HR = (
        "Napomena: ovaj alat je nedovoljno opisan u sustavu i možda neće "
        "raditi iz prve. Ako javi grešku, kontaktiraj managera.\n\n"
    )

    # Filip 2026-06-05: phrases that trigger the "nije točno" reoffer flow.
    # Exact-match (after lower + strip punctuation) to avoid false positives
    # like "nije ovo dosta novca" capturing.
    _REOFFER_PHRASES = frozenset({
        "nije točno", "nije tocno", "nije to", "nije ovo", "krivo",
        "ne to", "ne ovo", "nije pravi", "pogrešno", "pogresno",
    })
    _L2B_SENTINEL = "__L2B_DRIVER_BASICS__"

    async def _handle_reoffer(
        self, phone: str, pc, identity: IdentitySnapshot,
    ) -> str:
        """Handle 'nije točno' after an executed tool.

        Strategy:
          - If last_executed_tool was the L2B sentinel → re-route the original
            query through L3 router with L2B excluded. The router cascade
            will fire and present a top-3 picker.
          - Else (L3 path) → exclude shown_tool_ids from all_candidate_ids,
            take next 3 cosine candidates, render new picker. If 0 remain,
            send a graceful "out of options" message and clear state.
        """
        shown = set(pc.shown_tool_ids or [])
        if pc.last_executed_tool:
            shown.add(pc.last_executed_tool)
        # ---- L2B re-route: original query → fresh L3 cascade ----
        if pc.last_executed_tool == self._L2B_SENTINEL:
            # Mirror the action_global flow (engine.py:615-622): empty
            # candidates list, ACTION_GLOBAL stage, query stored so the
            # next picker resolves with full L3 routing.
            original = pc.original_query or ""
            options = clarify_ui.build_action_picker_global()
            await self.pending_clarify_store.save(
                phone, candidates=[],
                original_query=original,
                stage=PENDING_STAGE_ACTION_GLOBAL,
                # Carry the rejected L2B shortcut forward so the eventual
                # tool pick (after action_global → router) logs a correction.
                reoffer_origin_tool=pc.reoffer_origin_tool or pc.last_executed_tool,
            )
            return clarify_ui.render_text(
                options,
                header="U redu — evo opcija da preciziraš što tražiš:",
            )
        # ---- L3 path: exclude shown, take next 3 from cosine top-50 ----
        remaining_ids = [
            tid for tid in (pc.all_candidate_ids or [])
            if tid not in shown
        ]
        if not remaining_ids:
            await self.pending_clarify_store.clear(phone)
            return (
                "Nemam više relevantnih opcija za tvoj upit. "
                "Opiši ponovo što tražiš ili pošalji 'pomoć' za primjere."
            )
        next_three = remaining_ids[:3]
        # Build picker via existing helper (same look as primary clarify)
        score_pairs = [(tid, 0.0) for tid in next_three]
        tool_options = clarify_ui.build_from_router_candidates(
            score_pairs,
            tkb_lookup=lambda tid: self.tkb_intents.get(tid, ""),
        )
        enriched = [
            {
                "tool_id": c.tool_id,
                "method": (self.executor.method_of(c.tool_id) or "GET").upper(),
                "short_label": c.short_label,
                "description": c.description,
                "params": {},
                "field_hint": pc.original_query or None,
            }
            for c in tool_options.cards
        ]
        new_shown = list(shown) + next_three
        await self.pending_clarify_store.save(
            phone,
            candidates=enriched,
            original_query=pc.original_query,
            stage=PENDING_STAGE_TOOL,
            all_candidate_ids=pc.all_candidate_ids,
            shown_tool_ids=new_shown,
            last_executed_tool=None,   # nothing executed yet on this offer
            can_reoffer=False,          # will turn True after user picks + executes
            # Measure-first (Filip 2026-06-10): carry the rejected tool forward
            # so the eventual pick logs a (wrong, correct) golden-set label.
            # At reoffer time last_executed_tool IS the rejected tool (can_reoffer
            # only flips True after an execute).
            reoffer_origin_tool=pc.reoffer_origin_tool or pc.last_executed_tool,
        )
        return clarify_ui.render_text(
            tool_options,
            header="U redu, evo drugih opcija:",
        )

    @staticmethod
    def _fmt_echo_value(value) -> str:
        """Human-readable value for the pre-execute echo: ISO date →
        dd.mm.yyyy (+ HH:MM if present), bool → da/ne, else verbatim."""
        if isinstance(value, bool):
            return "da" if value else "ne"
        if (
            isinstance(value, str) and len(value) >= 10
            and value[4] == "-" and value[7] == "-" and value[:4].isdigit()
        ):
            out = f"{value[8:10]}.{value[5:7]}.{value[:4]}"
            if len(value) >= 16 and value[10] in ("T", " ") and value[13] == ":":
                out += f" {value[11:16]}"
            return out
        return str(value)

    # Context params the user never types but that ARE the action's target —
    # shown resolved to a human NAME (Vozilo: DA053F), never the raw UUID. Only
    # these are meaningful to surface; person_id (= you) and tenant_id are
    # implicit plumbing and stay hidden.
    # NOTE: keys MUST match the registry `context_key` exactly — org-unit is
    # "orgunit_id" (no underscore), same as _minimal_identity. A mismatched
    # "org_unit_id" silently dropped org-unit from the echo (Fix B 2026-05-28).
    _CONTEXT_ECHO_LABELS = {
        "vehicle_id": "Vozilo",
        "company_id": "Tvrtka",
        "orgunit_id": "Org. jedinica",
    }

    def _build_context_display(self, tool_id: str, identity) -> dict:
        """{HR-label: name} for the meaningful context params THIS tool injects
        (vehicle / company / org-unit), resolved to identity names. Lets the echo
        show 'Vozilo: DA053F' (the target) even though the UUID is injected later
        in the executor and is never in `params` at confirm time."""
        spec = self.tool_parameters.get(tool_id) or {}
        name_by_key = {
            "vehicle_id": getattr(identity, "vehicle_name", None),
            "company_id": getattr(identity, "company_name", None),
            "orgunit_id": getattr(identity, "org_unit_name", None),
        }
        out: dict = {}
        for pdef in spec.values():
            if not isinstance(pdef, dict):
                continue
            if (pdef.get("dependency_source") or "") != "context":
                continue
            label = self._CONTEXT_ECHO_LABELS.get(pdef.get("context_key"))
            value = name_by_key.get(pdef.get("context_key"))
            if label and value:
                out[label] = str(value)
        return out

    def _render_param_echo(
        self, tool_id: str, params: dict, type_display: Optional[dict] = None,
        context_display: Optional[dict] = None,
    ) -> str:
        """Echo the FULL human-readable input before a mutation confirm so the
        user can verify it (QB-style transparency):
          1) the target context as a NAME ('Vozilo: DA053F' — never the UUID),
          2) each user-provided value (`*TypeId` FKs shown as 'Gorivo', not 3).
        Hides plumbing (person/tenant ids, params the user never set). Field
        labels are humanized (HR label file deferred). Returns "" if empty."""
        spec = self.tool_parameters.get(tool_id) or {}
        disp = type_display or {}
        lines: list[str] = []
        # 1) Target context first — the WHO/WHERE of the action.
        for label, value in (context_display or {}).items():
            if value not in (None, ""):
                lines.append(f"• {label}: {value}")
        # 2) User-provided values — the WHAT.
        for name, value in (params or {}).items():
            if value is None or value == "":
                continue
            pdef = spec.get(name) or {}
            if (pdef.get("dependency_source") or "user_input") != "user_input":
                continue
            label = param_ui.humanize_param_name(name) or name
            label = label[:1].upper() + label[1:]
            shown = disp.get(name) or self._fmt_echo_value(value)
            lines.append(f"• {label}: {shown}")
        if not lines:
            return ""
        return "Provjeri prije slanja:\n" + "\n".join(lines) + "\n\n"

    def _render_confirm_pending(self, mut, tool_id: str = "", echo: str = "") -> str:
        base = (echo or "") + mut.confirm_message
        if tool_id and tool_id in self.risky_tool_ids:
            return self._RISKY_WARN_HR + base
        return base

    _GENERIC_EXECUTION_FAILURE = (
        "Tehnički problem. Pokušaj ponovo za nekoliko trenutaka."
    )

    async def _render_execution_failure(
        self, exec_result, tool_id: str,
        generic: Optional[str] = None,
    ) -> str:
        """Convert a failed ExecutionResult into a Croatian message.

        For 4xx with a body, try the LLM translator (returns Croatian
        explanation user can act on). For 5xx, missing body, or translator
        failure → fall back to the generic message.

        Filip 2026-05-17 Faza 2: replaces blanket "Tehnički problem" with
        actionable explanations like "Nedostaje datoteka." / "Nemaš ovlasti."
        """
        fallback = generic or self._GENERIC_EXECUTION_FAILURE
        if self.api_error_translator is None:
            return fallback
        status = getattr(exec_result, "status_code", None)
        body = getattr(exec_result, "error_body", None)
        if status is None or body is None:
            return fallback
        # EXE-1 fix (Filip 2026-05-20): scrub the API error body before it
        # reaches the LLM translator. MobilityOne validation errors can echo
        # back field values ("OIB 12345678901 already exists") — without this,
        # that PII would land in the Azure OpenAI prompt. Same GDPR rationale
        # as the conversation_history scrub (engine.py:209).
        try:
            body = self.pii.scrub(
                body if isinstance(body, str) else str(body)
            ).scrubbed_text
        except Exception:  # noqa: BLE001 — scrub must never break the error path
            pass
        try:
            tool_intent = (self.tkb_intents or {}).get(tool_id, "")
            translated = await self.api_error_translator.translate(
                status_code=status,
                response_body=body,
                tool_id=tool_id,
                tool_intent=tool_intent,
            )
        except Exception as e:  # noqa: BLE001 — never let translator break caller
            logger.warning("api_error_translator unexpected error: %s", e)
            return fallback
        return translated or fallback

    async def _resolve_pending_clarify(
        self, phone: str, pending, user_input: str,
        identity: IdentitySnapshot,
    ) -> Optional[str]:
        """Map user's reply ('1' / '2' / '3' / 'ne') to one of the saved
        candidates. Three stages exist (Filip 2026-05-17 Model A):

          stage=action_global → Model A Turn 2: user picked one of 4 universal
                          actions (POGLEDATI/UNIJETI/IZMIJENITI/IZBRISATI).
                          Run scoped L3 router on original_query (method +
                          persona + drop_internal), render top-3 tool picker.
          stage=action  → Legacy fallback-context picker: candidates already
                          retrieved by L3 but mixed-method. Filter by chosen
                          action's methods, then either resolve directly (1
                          left) or render tool picker (2+ left).
          stage=tool    → User picks specific tool. Mutation gate then execute.

        Returns response if resolved; None to fall through to fresh routing
        (user typed a brand new query).
        """
        text = (user_input or "").strip().lower()

        # Negative reply: cancel clarify, fall through (applies to both stages)
        if text in {"ne", "nista", "ništa", "drugo", "❌", "x", "n"}:
            await self.pending_clarify_store.clear(phone)
            return "U redu, reci drugačije što tražiš."

        # Numeric pick: 1/2/3 (or with emoji 1️⃣ 2️⃣ 3️⃣)
        # NOTE: "drugo" is INTENTIONALLY excluded (despite meaning "second"
        # in Croatian) because it also belongs to the negative-reply set
        # ("something else / cancel"). Treating it as a cancel-only token
        # avoids the ambiguity where user typing "drugo" thinking "the
        # second option" gets silently cancelled. User picking #2 types "2".
        # DIO 3 fix (Filip 2026-05-29): include 4 (IZBRISATI) — the action
        # picker has 4 cards (POGLEDATI/UNIJETI/IZMIJENITI/IZBRISATI) plus
        # the "Nešto drugo" cancel option. Previously 4 fell through to None
        # → re-rendered the picker indefinitely.
        digit_map = {
            "1": 0, "2": 1, "3": 2, "4": 3,
            "1️⃣": 0, "2️⃣": 1, "3️⃣": 2, "4️⃣": 3,
            "prvo": 0, "treće": 2, "trece": 2, "četvrto": 3, "cetvrto": 3,
        }
        # NOTE: "drugo" stays in cancel set (Croatian "something else" wins
        # over the "second" ordinal — avoids ambiguity in disambig context).
        idx = digit_map.get(text)
        if idx is None:
            if text and text[0] in "1234":
                idx = int(text[0]) - 1

        stage = getattr(pending, "stage", PENDING_STAGE_TOOL)
        # Measure-first (Filip 2026-06-10): if this pending carries a reoffer
        # origin (the tool the user rejected via "nije točno"), the eventual
        # tool pick below logs a (wrong, correct) golden-set label.
        _reoffer_origin = getattr(pending, "reoffer_origin_tool", None)

        # ---- Stage: universal action picker (Model A Turn 2) ----
        # User picked POGLEDATI/UNIJETI/IZMIJENITI/IZBRISATI on Turn 1.
        # Resolve action → allowed HTTP methods, run scoped L3 router on
        # the ORIGINAL query, then render top-3 tool picker (Turn 3 input).
        if stage == PENDING_STAGE_ACTION_GLOBAL:
            action_options = clarify_ui.build_action_picker_global()
            if idx is None or idx >= len(action_options.cards):
                return None  # invalid pick — fall through, treat as fresh query

            chosen_label = action_options.cards[idx].short_label
            allowed_methods = clarify_ui.methods_for_action_label(chosen_label)

            # Silent filters: tenant subset + chosen action's methods + internal
            # blacklist. Persona filter was removed 2026-05-28 (Filip rip):
            # backend OAuth scope (HTTP 403) is the real ACL; we have no
            # reliable role source to make per-user filtering meaningful.
            # Narrowing here is purely a routing-accuracy aid.
            tool_filter: Optional[frozenset[str]] = None
            if self.catalog_scoper is not None:
                tool_filter = self.catalog_scoper.scope(
                    tenant_id=identity.tenant_id,
                    methods=frozenset(allowed_methods) if allowed_methods else None,
                    drop_internal=True,
                )

            identity_summary = self._identity_summary(identity)
            recent_turns: list[dict] = []
            if self.conversation_history_store is not None:
                try:
                    recent_turns = await self.conversation_history_store.load(phone)
                except Exception:  # noqa: BLE001
                    recent_turns = []

            route_result = await self.router.route(
                query=pending.original_query or "",
                identity_summary=identity_summary,
                conversation_history=recent_turns,
                tool_filter=tool_filter,
            )

            await self._log_telemetry(
                kind="clarify_action_global_resolved",
                tenant_id=identity.tenant_id or "",
                query=pending.original_query or "",
                tool_picked=route_result.tool_id,
                candidates_top5=[
                    tid for tid, _ in (route_result.top_candidates or [])
                ][:5],
                extra={"action": chosen_label},
            )

            # LLM pick leads the picker: it read the candidates' intent_summaries
            # to disambiguate, so its choice (when valid) is a better card #1 than
            # raw anchor cosine. Anchor candidates fill the remaining slots, deduped.
            # Falls back to pure anchor order when the LLM declined/hallucinated
            # (tool_id absent → not in the anchor set).
            _score_by_id = dict(route_result.top_candidates or [])
            _llm_pick = route_result.tool_id
            _anchor_ids = [tid for tid, _ in (route_result.top_candidates or [])]
            _ordered_ids = (
                ([_llm_pick] if _llm_pick in _score_by_id else [])
                + [tid for tid in _anchor_ids if tid != _llm_pick]
            )
            top_cands = [(tid, _score_by_id.get(tid, 0.0)) for tid in _ordered_ids[:3]]
            if not top_cands:
                await self.pending_clarify_store.clear(phone)
                return (
                    f"Nisam našao prikladan alat za '{chosen_label}' "
                    f"za tvoj upit. Reci drugačije što tražiš."
                )

            # Build top-3 cards via the public helper (handles label/desc
            # rendering identically to the L5 fallback path).
            tool_options = clarify_ui.build_from_router_candidates(
                top_cands,
                tkb_lookup=lambda tid: self.tkb_intents.get(tid, ""),
            )
            if not tool_options.cards:
                # Tell the user instead of returning None — None would re-route
                # their pick digit ("2") as a fresh query, which lands back on
                # the action picker and reads like the bot ignored them.
                await self.pending_clarify_store.clear(phone)
                return (
                    f"Nisam našao prikladan alat za '{chosen_label}' "
                    f"za tvoj upit. Reci drugačije što tražiš."
                )

            # Preserve router's parsed params ONLY on the candidate the router
            # picked as top-1 — picking a different candidate means router was
            # wrong; that tool has a different schema and the params don't apply.
            router_top = route_result.tool_id
            router_params = route_result.params or {}
            enriched = [
                {
                    "tool_id": c.tool_id,
                    "method": (self.executor.method_of(c.tool_id) or "GET").upper(),
                    "short_label": c.short_label,
                    "description": c.description,
                    "params": (
                        dict(router_params) if c.tool_id == router_top else {}
                    ),
                    "field_hint": pending.original_query or None,
                }
                for c in tool_options.cards
            ]

            # Filip 2026-06-05: cache full cosine top-50 + shown top-3 so
            # "nije točno" can offer next 3 candidates without re-routing.
            shown_tool_ids = [c.tool_id for c in tool_options.cards]
            await self.pending_clarify_store.save(
                phone,
                candidates=enriched,
                original_query=pending.original_query,
                stage=PENDING_STAGE_TOOL,
                all_candidate_ids=_anchor_ids[:50],
                shown_tool_ids=shown_tool_ids,
                last_executed_tool=None,
                can_reoffer=False,  # only true after execute
                # Forward a reoffer origin (e.g. L2B sentinel) through the
                # action_global → tool transition so the pick logs a correction.
                reoffer_origin_tool=pending.reoffer_origin_tool,
            )
            return clarify_ui.render_text(
                tool_options,
                header=f"Razumio sam jedno od ovog ({chosen_label}):",
            )

        # ---- Stage: action picker (Step 1 of legacy fallback-context clarify) ----
        if stage == PENDING_STAGE_ACTION:
            # Build action options the same way we did when saving — needed
            # to know which label corresponds to which numeric pick.
            action_options = clarify_ui.build_action_picker(pending.candidates)
            if idx is None or idx >= len(action_options.cards):
                return None  # not a valid action pick — re-route as new query

            chosen_label = action_options.cards[idx].short_label
            allowed_methods = clarify_ui.methods_for_action_label(chosen_label)

            filtered = [
                c for c in pending.candidates
                if (c.get("method") or "").upper() in allowed_methods
            ]

            if not filtered:
                # No candidate matches — shouldn't happen if action picker
                # was built from the same set, but defensive.
                await self.pending_clarify_store.clear(phone)
                return None

            if len(filtered) == 1:
                # Single tool in chosen action → skip Step 2, resolve directly
                await self.pending_clarify_store.clear(phone)
                chosen = filtered[0]
            else:
                # Multiple tools → save Step 2 pending + render tool picker
                tool_options = clarify_ui.ClarifyOptions(
                    cards=[
                        clarify_ui.ClarifyCard(
                            index=i,
                            tool_id=c["tool_id"],
                            short_label=c.get("short_label") or c["tool_id"],
                            description=c.get("description") or "",
                        )
                        for i, c in enumerate(filtered, start=1)
                    ],
                )
                await self.pending_clarify_store.save(
                    phone,
                    candidates=filtered,
                    original_query=pending.original_query,
                    stage=PENDING_STAGE_TOOL,
                )
                return clarify_ui.render_text(
                    tool_options,
                    header=f"Razumio sam jedno od ovog ({chosen_label}):",
                )
        else:
            # ---- Stage: tool picker (Step 2, or single-step direct clarify) ----
            if idx is None or idx >= len(pending.candidates):
                return None  # not a valid pick — re-route as new query
            chosen = pending.candidates[idx]
            # Filip 2026-06-05: preserve reoffer context BEFORE clear so it
            # can be re-saved after execute (clear is required because the
            # picker is resolved; reoffer state is a fresh post-execute save).
            _reoffer_top50 = list(pending.all_candidate_ids or [])
            _reoffer_shown = list(pending.shown_tool_ids or [])
            await self.pending_clarify_store.clear(phone)

        # If chosen is a tool — run mutation gate then execute.
        tool_id = chosen.get("tool_id")
        if not tool_id:
            return "Nešto je krenulo krivo s odabirom. Pokušaj opet."

        # Measure-first (Filip 2026-06-10): a "nije točno" reoffer that the user
        # resolved by picking a DIFFERENT tool = a free golden-set label
        # (wrong_tool → correct_tool). Persisted via the TelemetryEvent
        # `correction` field; harvested by scripts/build_golden_set.py.
        if _reoffer_origin and tool_id != _reoffer_origin:
            await self._log_telemetry(
                tenant_id=identity.tenant_id or "",
                query=pending.original_query or "",
                tool_picked=tool_id,
                correction={
                    "wrong_tool": _reoffer_origin,
                    "correct_tool": tool_id,
                },
            )

        params = chosen.get("params") or {}

        # Param-asking gate (Filip 2026-05-17): before mutation_gate, check
        # whether the chosen tool has any required user_input params we don't
        # yet have. If yes, persist pending_params + ask. If all required
        # filled but optional exist, offer them. Otherwise fall through to
        # the existing mutation gate / execute path.
        type_display: dict = {}
        param_response = await self._maybe_start_param_collection(
            phone, tool_id, params, pending.original_query, identity,
            type_display=type_display,
        )
        if param_response is not None:
            return param_response

        return await self._run_gate_and_execute(
            phone, tool_id, params, identity,
            field_hint=chosen.get("field_hint"),
            query=pending.original_query,
            type_display=type_display,
            reoffer_top50=_reoffer_top50 if _reoffer_top50 else None,
            reoffer_shown=_reoffer_shown if _reoffer_shown else None,
        )

    # ------------------------------------------------------------------
    # Param-asking (Filip 2026-05-17) — Required + optional collection
    # ------------------------------------------------------------------

    def _compute_missing_required(
        self, tool_id: str, collected: dict,
    ) -> list:
        """Required user_input params that are not yet in `collected`.

        Mirrors the logic in llm_router._compute_missing_required, but
        reads from V2Engine.tool_parameters (which the factory populated
        from the same registry). Returns a stable-ordered list.
        """
        spec = self.tool_parameters.get(tool_id) or {}
        missing = []
        for pname, pdef in spec.items():
            if not isinstance(pdef, dict):
                continue
            if not pdef.get("required"):
                continue
            src = (pdef.get("dependency_source") or "user_input").lower()
            if src != "user_input":
                continue
            if pname in collected and collected[pname] not in (None, ""):
                continue
            missing.append(pname)
        return missing

    def _compute_optional(
        self, tool_id: str, collected: dict,
    ) -> list:
        """Optional user_input params not yet collected. Used to offer
        the user a chance to fill them after required are done."""
        spec = self.tool_parameters.get(tool_id) or {}
        optional = []
        for pname, pdef in spec.items():
            if not isinstance(pdef, dict):
                continue
            if pdef.get("required"):
                continue
            src = (pdef.get("dependency_source") or "user_input").lower()
            if src != "user_input":
                continue
            if pname in collected and collected[pname] not in (None, ""):
                continue
            optional.append(pname)
        return optional

    # Param types the optional offer + LLM extractor can reliably handle from
    # Croatian free-text. Array/object are EXCLUDED because LLM tool-use
    # routinely returns a string ("filtered by status") instead of a
    # schema-valid structure (`[{"field": "Status", "value": "..."}]`),
    # which the API then rejects with 400/422 ("Tehnički problem" for the user).
    # 278/950 tools have at least one array/object optional — for those, we
    # silently send the call without the structured filter; the API uses its
    # default (no filter / all rows / etc.) which is the right behavior for
    # a conversational WhatsApp bot.
    _FRIENDLY_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean"})

    # Framework-level pagination/sort params — NEVER offered to a WhatsApp
    # user (Filip 2026-05-23). They have sane API defaults; a fleet driver
    # asking "moje rezervacije" wants the list, not to configure paging. The
    # allowlist is intentionally narrow (these 4 exact names) so a genuine
    # domain optional hidden elsewhere still gets offered. Someone who really
    # wants a custom sort can ask explicitly.
    _NEVER_OFFERED_PARAMS = frozenset({"first", "rows", "sort", "sortorder"})

    def _user_friendly_optionals(
        self, tool_id: str, collected: dict,
    ) -> list:
        """Like _compute_optional but skips (a) array/object types the LLM
        extract can't reliably structure from Croatian free-text, and (b)
        pagination/sort params that are never useful to offer a user."""
        spec = self.tool_parameters.get(tool_id) or {}
        return [
            p for p in self._compute_optional(tool_id, collected)
            if p.lower() not in self._NEVER_OFFERED_PARAMS
            and (spec.get(p) or {})
                .get("param_type", "string").lower()
                in self._FRIENDLY_PARAM_TYPES
        ]

    async def _label_for(
        self, tool_id: str, param_name: str,
        param_def: Optional[dict] = None,
    ) -> Optional[str]:
        """Resolve Croatian label for a single param. Returns None if no
        labeler wired or LLM/cache miss — caller falls back to humanize."""
        if self.param_labeler is None:
            return None
        tool_intent = (self.tkb_intents or {}).get(tool_id, "")
        try:
            return await self.param_labeler.label_for(
                param_name=param_name,
                param_def=param_def,
                tool_id=tool_id,
                tool_intent=tool_intent,
            )
        except Exception as e:  # noqa: BLE001 — never let labeler break caller
            logger.warning("param_labeler unexpected error: %s", e)
            return None

    async def _labels_for(
        self, tool_id: str, param_names: list,
    ) -> dict:
        """Resolve Croatian labels for many params (optional offer flow).
        Returns dict {param_name: label} — missing keys mean labeler
        returned None for that param; caller falls back to humanize."""
        if self.param_labeler is None or not param_names:
            return {}
        spec = self.tool_parameters.get(tool_id) or {}
        out: dict = {}
        for pname in param_names:
            label = await self._label_for(tool_id, pname, spec.get(pname))
            if label:
                out[pname] = label
        return out

    async def _resolve_type_param(self, param_name, text, identity):
        """Resolve a `*TypeId` FK param from the user's words: fetch the
        `/…Types` list via the executor + match the text to a Name. Returns
        `(matched_id|None, pairs|None)`. `pairs is None` → not a resolvable
        *TypeId (caller falls back to normal param-ask). `pairs` set with id
        None → found the list but no unique match (use it as a pick-list)."""
        tmap = getattr(self, "typeid_map", None) or {}
        get_tool = tmap.get((param_name or "").lower())
        if not get_tool:
            return None, None
        try:
            res = await self.executor.execute(
                tool_id=get_tool, params={},
                identity_summary=self._minimal_identity(identity),
            )
            rows = res.data if getattr(res, "success", False) else None
        except Exception as e:  # noqa: BLE001
            logger.warning("type-resolve fetch failed for %s: %s", param_name, e)
            rows = None
        if rows is None:
            return None, None
        pairs = type_resolver.rows_to_pairs(rows)
        if not pairs:
            return None, None
        mid, _ = type_resolver.match(text or "", pairs)
        return mid, pairs

    async def _render_type_question(self, tool_id, param, pairs):
        """Ask for a *TypeId by LISTING the available type names (the user
        types a word, which the answer-matcher resolves to the id)."""
        pdef = (self.tool_parameters.get(tool_id) or {}).get(param)
        label = (await self._label_for(tool_id, param, pdef)) or param
        names = ", ".join(str(n) for _, n in pairs[:20])
        if len(pairs) > 20:
            # The matcher accepts ALL pairs — tell the user the list is cut
            # so they know an unlisted name can still be typed.
            names += f" (i još {len(pairs) - 20})"
        return (
            f"Koji {label}? Dostupno: {names}. "
            "Napiši naziv ili 'odustani' za otkaz."
        )

    async def _maybe_start_param_collection(
        self, phone: str, tool_id: str, collected: dict,
        original_query: str, identity=None, type_display: Optional[dict] = None,
    ) -> Optional[str]:
        """Decide whether the chosen tool needs to ask for missing params.

        Returns:
          None      → no asking needed; caller proceeds to mutation gate.
          str       → response to send the user; pending_params has been
                      saved (or it's the final optional-offer message).
        """
        if self.pending_params_store is None or not self.tool_parameters:
            return None  # feature disabled / no registry → skip
        missing = self._compute_missing_required(tool_id, collected)
        # *TypeId resolver (Filip 2026-05-27): auto-resolve FK type params from
        # the original query (fetch /…Types + match Name), and pre-fetch option
        # lists so the ask can show names. Mutates `collected` in place so an
        # auto-resolved id flows to execute. Degrades to normal ask if /…Types
        # is unreachable (e.g. M1 token down).
        type_options: dict = {}
        if missing and identity is not None:
            for pname in list(missing):
                mid, pairs = await self._resolve_type_param(pname, original_query, identity)
                if pairs:
                    if mid is not None:
                        collected[pname] = mid
                        if type_display is not None:
                            nm = next(
                                (n for i, n in pairs if str(i) == str(mid)), None
                            )
                            if nm:
                                type_display[pname] = nm
                    else:
                        type_options[pname] = pairs
            missing = self._compute_missing_required(tool_id, collected)
        # NALAZ 2 (Filip 2026-05-25): a required array/object param can't be
        # built from free-text — asking for it would ship a raw string where
        # the API wants a structured array (→ 422). Refuse honestly instead.
        # Affects *_multipatch bulk tools (not chat-drivable anyway).
        _spec = self.tool_parameters.get(tool_id) or {}
        if any(
            (_spec.get(n) or {}).get("param_type", "").lower() in ("array", "object")
            for n in missing
        ):
            return (
                "Ova akcija traži strukturiran unos (npr. popis stavki) koji "
                "ne mogu pouzdano složiti iz poruke. Za ovu radnju javi se timu."
            )
        optionals = self._user_friendly_optionals(tool_id, collected)
        if not missing and not optionals:
            return None  # nothing to ask, fall through

        if missing:
            await self.pending_params_store.save(
                phone,
                PendingParams(
                    phone=phone, tool_id=tool_id,
                    collected=dict(collected),
                    required_remaining=list(missing),
                    optional_remaining=list(optionals),
                    optional_offered=False,
                    original_query=original_query,
                    type_options=type_options,
                ),
            )
            first = missing[0]
            if first in type_options:
                return await self._render_type_question(tool_id, first, type_options[first])
            pdef = (self.tool_parameters.get(tool_id) or {}).get(first)
            label = await self._label_for(tool_id, first, pdef)
            return param_ui.render_param_question(
                first, pdef, label_override=label,
            )

        # No required missing, but there ARE optionals — offer them.
        await self.pending_params_store.save(
            phone,
            PendingParams(
                phone=phone, tool_id=tool_id,
                collected=dict(collected),
                required_remaining=[],
                optional_remaining=list(optionals),
                optional_offered=True,
                original_query=original_query,
            ),
        )
        overrides = await self._labels_for(tool_id, optionals)
        return param_ui.render_optional_offer(
            optionals, label_overrides=overrides,
        )

    async def _resolve_pending_params(
        self, phone: str, pending: PendingParams, user_input: str,
        identity: IdentitySnapshot,
    ) -> Optional[str]:
        """Handle one turn of param collection.

        Flow:
          1. Cancel? clear + acknowledge.
          2. Awaiting optional-offer answer (Filip 2026-05-17 LLM extract):
             - 'ne' / negative → skip all optionals, finalize.
             - anything else → ONE LLM extract over the offered set →
               merge whatever LLM filled (possibly nothing) → finalize.
             No more iteration — single shot.
          3. Otherwise it's an answer to a required param question:
             a. parse by registry param_type. None → re-ask.
             b. store in collected, drop from required_remaining.
             c. more required → ask next.
             d. required drained + optionals exist + not offered → offer.
             e. otherwise → finalize (mutation gate / execute).

        Returns response string, OR None when the message doesn't fit
        the expected shape (clears state, falls through to fresh routing).
        """
        text = (user_input or "").strip()

        # 1. Explicit cancel — abort whole collection regardless of stage.
        if param_ui.is_cancel(text):
            await self.pending_params_store.clear(phone)
            return "U redu, odustajem."

        tool_id = pending.tool_id
        spec = self.tool_parameters.get(tool_id) or {}

        # 2. Awaiting answer to the optional-offer prompt.
        if (
            pending.optional_offered
            and not pending.required_remaining
            and pending.optional_remaining
        ):
            if param_ui.is_negative(text):
                # User skipped optionals — finalize with required-only.
                # ORCH-5: clear AFTER finalize (retry-safe), not before.
                pending.optional_remaining = []
                result = await self._finalize_after_params(
                    phone, pending, identity,
                )
                await self.pending_params_store.clear(phone)
                return result

            # Free-text reply — single LLM extract over the offered set.
            # No iteration. LLM returns dict subset; we merge and finalize.
            # If extractor missing or LLM returns {} → execute with required
            # only (degraded UX, not a crash).
            if self.optional_extractor is not None:
                spec_subset = {
                    p: spec.get(p, {}) for p in pending.optional_remaining
                }
                try:
                    extracted = await self.optional_extractor.extract(
                        text, spec_subset,
                    )
                except Exception as e:  # noqa: BLE001 — defensive belt-and-braces
                    logger.warning(
                        "optional_extractor raised (should be silent): %s", e,
                    )
                    extracted = {}
                if extracted:
                    pending.collected.update(extracted)
                await self._log_telemetry(
                    kind="optional_extract_done",
                    tenant_id=identity.tenant_id or "",
                    tool_picked=tool_id,
                    extra={
                        "offered": list(pending.optional_remaining),
                        "filled": list(extracted.keys()),
                    },
                )
            # ORCH-5 fix (Filip 2026-05-20): clear AFTER finalize, not before —
            # same rationale as branch 3c. If finalize raises, the user keeps
            # collected params and can retry. (This optional-extraction path
            # was missed in the first ORCH-5 pass; caught while writing tests.)
            result = await self._finalize_after_params(phone, pending, identity)
            await self.pending_params_store.clear(phone)
            return result

        # 3. Answer to a required param question (one-by-one collection).
        if not pending.required_remaining:
            # Defensive — shouldn't happen since branch 2 handles the only
            # other state. Clear and fall through to fresh routing.
            await self.pending_params_store.clear(phone)
            return None

        param_name = pending.required_remaining[0]
        pdef = spec.get(param_name) or {}
        _opts = (pending.type_options or {}).get(param_name)
        if _opts:
            # *TypeId: match the user's word to a Name → id; else re-ask the list.
            mid, _ = type_resolver.match(text, _opts)
            if mid is None:
                return await self._render_type_question(tool_id, param_name, _opts)
            value = mid
        else:
            value = param_ui.parse_param_value(text, pdef)
            if value is None:
                # Re-ask (don't advance) — keep state intact.
                label = await self._label_for(tool_id, param_name, pdef)
                return param_ui.render_param_reask(
                    param_name, pdef, label_override=label,
                )

        # Store + advance.
        pending.collected[param_name] = value
        pending.required_remaining.pop(0)

        # 3a. More required → ask next.
        if pending.required_remaining:
            await self.pending_params_store.save(phone, pending)
            nxt = pending.required_remaining[0]
            if nxt in (pending.type_options or {}):
                return await self._render_type_question(
                    tool_id, nxt, pending.type_options[nxt],
                )
            nxt_pdef = spec.get(nxt)
            label = await self._label_for(tool_id, nxt, nxt_pdef)
            return param_ui.render_param_question(
                nxt, nxt_pdef, label_override=label,
            )

        # 3b. Required drained; optionals exist and not yet offered.
        if pending.optional_remaining and not pending.optional_offered:
            pending.optional_offered = True
            await self.pending_params_store.save(phone, pending)
            overrides = await self._labels_for(
                tool_id, pending.optional_remaining,
            )
            return param_ui.render_optional_offer(
                pending.optional_remaining, label_overrides=overrides,
            )

        # 3c. Required drained, no optionals or already offered → finalize.
        # ORCH-5 fix (Filip 2026-05-20): clear AFTER finalize succeeds, not
        # before. If finalize raises (e.g. Redis save of pending_mutation
        # fails), the user keeps their collected params and can retry instead
        # of silently losing all progress.
        result = await self._finalize_after_params(phone, pending, identity)
        await self.pending_params_store.clear(phone)
        return result

    async def _finalize_after_params(
        self, phone: str, pending: PendingParams,
        identity: IdentitySnapshot,
    ) -> str:
        """All params collected → run mutation gate (confirm or auto) +
        execute. Mirrors the tail of `_resolve_pending_clarify`."""
        tool_id = pending.tool_id
        # Strip internal markers before sending to executor.
        params = {
            k: v for k, v in pending.collected.items()
            if not k.startswith("__")
        }
        # U2 (Filip 2026-05-26): coerce here too so optional-extractor values
        # (which merge into collected without per-param coercion) get the same
        # normalization as the LLM path — no required/optional asymmetry.
        params = self._coerce_llm_params(tool_id, params)
        # Reverse-map asked *TypeId ids → names for the confirm echo (so it shows
        # "Gorivo", not the id). pending.type_options holds (id,name) pairs for
        # params that were asked with a pick-list.
        type_display: dict = {}
        for _p, _pairs in (getattr(pending, "type_options", None) or {}).items():
            _v = params.get(_p)
            if _v is not None:
                _nm = next((n for i, n in _pairs if str(i) == str(_v)), None)
                if _nm:
                    type_display[_p] = _nm
        method = self.executor.method_of(tool_id) or "GET"
        mut = mutation_gate.decide_mutation(
            method=method, entity_label=identity.vehicle_name or "zapis",
        )
        if mut.decision != mutation_gate.DECISION_AUTO:
            await self.pending_mut_store.save(
                phone, tool_id=tool_id, params=params, stage=STAGE_SINGLE,
            )
            ctx_display = self._build_context_display(tool_id, identity)
            echo = self._render_param_echo(
                tool_id, params, type_display, ctx_display,
            )
            return self._render_confirm_pending(mut, tool_id=tool_id, echo=echo)
        exec_result = await self.executor.execute(
            tool_id=tool_id, params=params,
            identity_summary=self._minimal_identity(identity),
        )
        if not exec_result.success:
            return await self._render_execution_failure(exec_result, tool_id)
        return await self._format_reply(
            query=pending.original_query, tool_id=tool_id,
            api_data=exec_result.data, identity=identity,
            field_hint=pending.original_query or None,
            extra_context={"entity_label": identity.vehicle_name or "rezultata"},
        )

    def _coerce_llm_params(self, tool_id: str, params: dict) -> dict:
        """Normalize LLM-extracted string values to their registry type before
        execute (NALAZ 1, Filip 2026-05-25). The LLM path skips param_ui (only
        param-ask coerces), so a HR-comma number ('12,5') or HR-format date
        ('17.05.2026') could ship raw. Reuse param_ui.parse_param_value,
        IMPROVE-ONLY: replace only when coercion succeeds AND the value is a
        string of a coercible type; otherwise keep the original (native ints/
        floats from the LLM and already-ISO dates pass through unchanged)."""
        spec = self.tool_parameters.get(tool_id) or {}
        if not spec:
            return params
        out = {}
        for name, val in params.items():
            pdef = spec.get(name)
            if isinstance(val, str) and isinstance(pdef, dict):
                ptype = (pdef.get("param_type") or "string").lower()
                fmt = (pdef.get("format") or "").lower()
                if ptype in ("integer", "number", "boolean") or fmt in ("date", "date-time"):
                    coerced = param_ui.parse_param_value(val, pdef)
                    if coerced is not None:
                        out[name] = coerced
                        continue
            # R1 (Filip 2026-05-26): LLM sometimes emits a native float (42.0)
            # for an integer field — normalize a WHOLE float to int. A non-whole
            # float (42.5) is a wrong value, NOT ours to round → leave it (API
            # rejects). bool is excluded (it's an int subclass, not float).
            elif (
                isinstance(pdef, dict)
                and isinstance(val, float)
                and (pdef.get("param_type") or "").lower() == "integer"
                and val.is_integer()
            ):
                out[name] = int(val)
                continue
            out[name] = val
        return out

    async def _run_gate_and_execute(
        self, phone: str, tool_id: str, params: dict,
        identity: IdentitySnapshot, field_hint: Optional[str] = None,
        query: str = "", type_display: Optional[dict] = None,
        reoffer_top50: Optional[list] = None,
        reoffer_shown: Optional[list] = None,
    ) -> str:
        """Mutation gate → confirm-or-execute → format. Shared execute tail.

        REOFFER (Filip 2026-06-05): for GET tools (auto-execute path), save
        pending_clarify state with full cosine top-50 + shown ids so that a
        subsequent "nije točno" can offer next 3 candidates without re-routing.
        For mutations (confirm gate), reoffer is intentionally NOT saved —
        user said "Da" so they consciously approved; "nije točno" makes no
        sense after a write.
        """
        params = self._coerce_llm_params(tool_id, params)
        method = self.executor.method_of(tool_id) or "GET"
        mut = mutation_gate.decide_mutation(
            method=method, entity_label=identity.vehicle_name or "zapis",
        )
        if mut.decision != mutation_gate.DECISION_AUTO:
            await self.pending_mut_store.save(
                phone, tool_id=tool_id, params=params, stage=STAGE_SINGLE,
            )
            ctx_display = self._build_context_display(tool_id, identity)
            echo = self._render_param_echo(
                tool_id, params, type_display, ctx_display,
            )
            return self._render_confirm_pending(mut, tool_id=tool_id, echo=echo)
        exec_result = await self.executor.execute(
            tool_id=tool_id, params=params,
            identity_summary=self._minimal_identity(identity),
        )
        if not exec_result.success:
            return await self._render_execution_failure(exec_result, tool_id)
        # Save reoffer state for "nije točno" handler (Filip 2026-06-05).
        # Only fires when called from clarify flow with the top-50 context.
        if (reoffer_top50 is not None
                and self.pending_clarify_store is not None):
            shown = list(reoffer_shown or [])
            if tool_id not in shown:
                shown.append(tool_id)
            try:
                await self.pending_clarify_store.save(
                    phone, candidates=[], original_query=query,
                    stage=PENDING_STAGE_TOOL,
                    all_candidate_ids=list(reoffer_top50),
                    shown_tool_ids=shown,
                    last_executed_tool=tool_id,
                    can_reoffer=True,
                )
            except Exception as e:  # noqa: BLE001 — never break the reply
                logger.warning("save reoffer state failed: %s", e)
        return await self._format_reply(
            query=query, tool_id=tool_id, api_data=exec_result.data,
            identity=identity, field_hint=field_hint,
            extra_context={"entity_label": identity.vehicle_name or "rezultata"},
        )

    async def _continue_pending_mutation(
        self, phone: str, pending, user_input: str,
        identity: IdentitySnapshot,
    ) -> str:
        """Apply user's reply to a saved confirm-dialog state.

        Outcomes (single-confirm policy, Filip 2026-05-16):
          execute   → run the mutation, clear state
          cancel    → user said no; clear state
          ambiguous → re-prompt; keep state
        """
        action = parse_reply(user_input, pending.stage)

        if action == "cancel":
            await self.pending_mut_store.clear(phone)
            return "U redu, odustajem."

        if action == "ambiguous":
            # Numeric replies: users answer menus with digits. NEVER map a
            # digit to execute (false-execute is the one unrecoverable
            # outcome — same asymmetry as parse_reply); "1" gets an explicit
            # 'napiši Da' re-prompt, 2/3 cancel safely.
            _digit = user_input.strip().rstrip(".!?,;")
            if _digit in ("2", "3", "2️⃣", "3️⃣"):
                await self.pending_mut_store.clear(phone)
                return "U redu, otkazao sam potvrdu. Pošalji novi upit."
            if _digit in ("1", "1️⃣"):
                return (
                    "Za izvršenje napiši izričito 'Da' "
                    "(ili 'Ne' za odustajanje)."
                )
            # Multi-pending guard (#66): if user sent something that
            # looks like a NEW query (long, contains action verbs),
            # they probably forgot the pending confirm. Surface both
            # options explicitly instead of generic "Da ili Ne".
            looks_like_new_query = (
                len(user_input.split()) >= 3
                and any(
                    v in user_input.lower()
                    for v in [
                        "rezerviraj", "obriši", "obrisi", "otkaži", "otkazi",
                        "unesi", "upiši", "upisi", "stavi", "dodaj", "pošalji",
                        "kolika", "moja", "moje", "moj", "trebam",
                    ]
                )
            )
            if looks_like_new_query:
                return (
                    f"Imaš nedovršenu potvrdu za: {pending.tool_id}.\n\n"
                    "Napiši 'Da' da je izvršim, ili 'Ne' da je otkažem — "
                    "pa mi nakon toga ponovno pošalji novi upit."
                )
            return (
                "Nisam siguran je li to bilo Da ili Ne. "
                "Odgovori s \"Da\" za potvrdu ili \"Ne\" za odustajanje."
            )

        # ---- Stale confirm guard (#64) ----
        # If pending is old (>90s), warn and re-ask before executing.
        # Redis TTL is 300s; we add an early warn-window because user's
        # context likely changed mid-typing.
        import time as _t
        pending_age_s = max(0.0, _t.time() - (pending.created_at or 0.0))
        STALE_WARN_THRESHOLD = 90.0
        if pending_age_s > STALE_WARN_THRESHOLD:
            # Refresh TTL on a stale pending and re-prompt with warning.
            # Keeps single-confirm policy (Filip 2026-05-16) — no escalation
            # to a stronger re-confirm stage, just a fresh ask.
            await self.pending_mut_store.save(
                phone,
                tool_id=pending.tool_id,
                params=pending.params,
                stage=STAGE_SINGLE,
            )
            return (
                f"Tvoja potvrda je stara ({int(pending_age_s)} sek). "
                "Za sigurnost: napiši još jednom 'Da' ili 'Ne' za otkaz."
            )

        # ---- Bug #1 fix (Faza 3 Filip 2026-05-17): re-validate identity ----
        # Before execute, re-resolve identity if pending is >30s old. If
        # tenant_id changed (admin re-assigned the phone, or user_mappings
        # was updated), DON'T blindly execute against stale context — that
        # would route the action into the wrong tenant. Clear pending and
        # ask the user to repeat.
        STALE_REVALIDATE_THRESHOLD = 30.0
        if pending_age_s > STALE_REVALIDATE_THRESHOLD:
            try:
                fresh_identity = await self.identity.resolve(phone)
            except Exception as e:  # noqa: BLE001 — fall through if resolve fails
                logger.warning("stale-confirm identity revalidate failed: %s", e)
                fresh_identity = identity
            # ORCH-6 fix (Filip 2026-05-20): also abort if vehicle_id changed,
            # not just tenant_id. The confirm message showed the OLD vehicle
            # ("Upisat ću X km na vozilo Golf"); if the phone was reassigned to
            # a different vehicle mid-confirm, blindly executing would write the
            # mutation against the wrong vehicle. Same safety rationale as tenant.
            tenant_changed = fresh_identity.tenant_id != identity.tenant_id
            # Fix C (Filip 2026-05-28): symmetric compare so None↔id transitions
            # are caught too (the old both-non-None form only caught id1→id2 and
            # silently missed None→id / id→None). None↔None stays no-op.
            vehicle_changed = (
                (identity.vehicle_id or "") != (fresh_identity.vehicle_id or "")
            )
            if tenant_changed or vehicle_changed:
                await self.pending_mut_store.clear(phone)
                logger.warning(
                    "stale confirm aborted: %s changed for phone=%s "
                    "(tenant %s→%s, vehicle %s→%s)",
                    "tenant" if tenant_changed else "vehicle", phone[-4:],
                    identity.tenant_id, fresh_identity.tenant_id,
                    identity.vehicle_id, fresh_identity.vehicle_id,
                )
                return (
                    "Konfiguracija ti se promijenila u međuvremenu. "
                    "Pošalji upit ponovo da nastavim s ispravnim podacima."
                )
            # Use fresh snapshot for execute (vehicle_name, last_mileage,
            # etc. may have changed even if tenant didn't).
            identity = fresh_identity

        # action == "execute"
        # CRITICAL FIX (idempotency #1, 0-error tolerance):
        # Previously: clear() → execute(). Race: if process crashes between
        # clear and execute, mutation is lost; if execute fails transient
        # and user retries "Da", clear-already-done means the retry has
        # no pending to act on.
        # New flow: atomic execution lock → execute → clear ONLY on success.
        # Concurrent "Da" (network double-tap, Infobip retry) sees the lock
        # and gets a friendly "u tijeku" message instead of double-executing.
        if not await self.pending_mut_store.try_acquire_execution(phone):
            return (
                "Operacija je već u tijeku — pričekaj sekundu i provjeri "
                "potvrdu sljedeće poruke."
            )
        try:
            exec_result = await self.executor.execute(
                tool_id=pending.tool_id,
                params=pending.params,
                identity_summary=self._minimal_identity(identity),
            )
            if exec_result.circuit_open:
                return exec_result.error
            if not exec_result.success:
                logger.warning(
                    "pending mutation exec failed tool=%s err=%s",
                    pending.tool_id, exec_result.error,
                )
                # NOTE: pending stays — user can retry by replying "Da" again.
                # No clear() here.
                return await self._render_execution_failure(
                    exec_result, pending.tool_id,
                    generic=(
                        "Tehnički problem prilikom izvršavanja akcije. "
                        "Pokušaj ponovo."
                    ),
                )
            # Success — clear pending so the next "Da" doesn't replay it.
            await self.pending_mut_store.clear(phone)
            # Mutation may have changed driver data → invalidate identity cache.
            await self._invalidate_identity(phone)
        finally:
            await self.pending_mut_store.release_execution(phone)
        r = formatter.format_response(
            template_id="mutation_success",
            api_response_data=exec_result.data,
            extra_context={"action": "Akcija"},
        )
        return r.text


# ---------------------------------------------------------------------------
# Production factory — construct V2Engine + supporting stores from the
# infrastructure already initialized in main.lifespan / worker init.
#
# Used by Faza B (mirror traffic) wiring. Default OFF — engine is built
# only when `V2_ENABLED=1`. Stores returned alongside so the cache-
# invalidation route can expose them on app.state.v2_*.
# ---------------------------------------------------------------------------

@dataclass
class V2EngineBundle:
    """Constructed V2Engine + the stores that the cache-invalidation
    HTTP route needs to publish on app.state.v2_*."""
    engine: "V2Engine"
    identity: "IdentityContext"
    conversation_history: Optional["ConversationHistoryStore"] = None
    pending_mutation: Optional["PendingMutationStore"] = None
    pending_clarify: Optional["PendingClarifyStore"] = None
    pending_params: Optional["PendingParamsStore"] = None
    gdpr_audit: Optional["GdprAuditStore"] = None


def _log_config_freshness(registry_path, tool_data_path) -> None:
    """Emit one structured log line covering the two routing-data configs.

    Post Phase 2 consolidation, the bot reads from a single tool_data.json
    that's derived from processed_tool_registry.json. If sync_tools.py
    regenerates the registry but build_tool_data.py / regenerate_tool_data.py
    isn't re-run, tool_data.json is stale and the router operates on an
    obsolete tool catalog.

    This helper surfaces that staleness in production logs the moment the
    engine boots. Best-effort: never raises (Docker volume mtime quirks
    shouldn't crash startup). Grep production logs for `config_freshness`
    or `tool_data_stale=True` to spot drift.
    """
    try:
        from datetime import datetime, timezone

        def _stat(p):
            s = p.stat()
            return s.st_mtime, datetime.fromtimestamp(s.st_mtime, tz=timezone.utc).isoformat()

        reg_ts, reg_iso = _stat(registry_path)
        td_ts, td_iso = _stat(tool_data_path)
        now = datetime.now(tz=timezone.utc).timestamp()

        tool_data_stale = reg_ts > td_ts

        logger.info(
            "config_freshness "
            "registry_mtime=%s tool_data_mtime=%s "
            "tool_data_stale=%s "
            "tool_data_age_days=%.1f registry_age_days=%.1f",
            reg_iso, td_iso,
            tool_data_stale,
            (now - td_ts) / 86400.0,
            (now - reg_ts) / 86400.0,
        )

        if tool_data_stale:
            logger.warning(
                "tool_data.json is STALE relative to registry — new tools may "
                "have no metadata and won't be retrieved correctly. "
                "FIX: python scripts/build_tool_data.py "
                "(or scripts/regenerate_tool_data.py for full LLM regen)"
            )
    except Exception as e:  # noqa: BLE001 — observability must never block startup
        logger.warning("config_freshness check failed (%s): %s", type(e).__name__, e)


async def make_v2_engine_for_production(
    *,
    redis_client,
    gateway,
    tool_registry,
    settings,
) -> V2EngineBundle:
    """Build a fully-wired V2Engine from infrastructure that main.lifespan
    has already constructed.

    Stores returned in the bundle are the same instances the engine uses,
    so cache-invalidation operations affect the live engine state.
    """
    from services.openai_client import (
        get_openai_client, get_embedding_client,
    )

    llm_client = get_openai_client()
    embedder = get_embedding_client()
    deployment = settings.AZURE_OPENAI_DEPLOYMENT_NAME

    # Adapter: DriverBasicsAnchor expects `.embed(text) -> list[float]`, but
    # get_embedding_client() returns AsyncAzureOpenAI directly (which only
    # exposes `.embeddings.create(...)`). Wrap it so the interface matches —
    # without this adapter, every anchor embed in driver_basics raises
    # "AsyncAzureOpenAI object has no attribute 'embed'" and the L2b
    # shortcut silently degrades to 0 positive / 0 negative vectors.
    class _EmbedAdapter:
        def __init__(self, client, model: str):
            self._client = client
            self._model = model

        async def embed(self, text: str):
            r = await self._client.embeddings.create(
                input=text, model=self._model,
            )
            return r.data[0].embedding

    basics_embedder = _EmbedAdapter(
        embedder, settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
    )

    # --- Telemetry (best-effort, never blocks) ---
    # Dual-sink in production: BufferedAsyncFileSink → logs/v2_telemetry-*.jsonl
    # for offline analysis + RedisSink → routing:accuracy_log for the live
    # Damir-export endpoint at webhook_simple.py:763. Env knobs: V2_TELEMETRY,
    # V2_TELEMETRY_BACKEND, V2_TELEMETRY_DIR. Logger is best-effort — failures
    # are swallowed at the sink boundary so user requests never block on it.
    telemetry = TelemetryLogger.from_env(redis_client=redis_client)

    # --- Required (always) ---
    rate_limiter = RateLimiter(redis_client)
    pii = PIIScrubber()
    # Wire the tenant_resolver so identity can lazy-onboard new phones into
    # user_mappings on first successful Persons resolve. Tests pass None.
    from services.tenant_resolver import get_tenant_resolver
    _tenant_resolver = await get_tenant_resolver()
    identity = IdentityContext(redis_client, gateway, settings, tenant_resolver=_tenant_resolver)
    intent_type = IntentTypeClassifier(llm_client, deployment)
    basics = DriverBasicsAnchor(basics_embedder)
    flow_engine = FlowEngine(flows=FLOWS)
    flow_store = FlowStateStore(redis_client)
    executor = ToolExecutor(gateway, tool_registry)
    pending_mut = PendingMutationStore(redis_client)

    # Driver basics anchor index — async build embedded vectors.
    # Failure is non-fatal; L2b anchor path simply skips matches.
    try:
        await basics.initialize()
    except Exception as e:  # noqa: BLE001
        logger.warning("DriverBasicsAnchor initialize failed: %s", e)

    # --- Optional but always cheap ---
    pending_clarify = PendingClarifyStore(redis_client)
    pending_params = PendingParamsStore(redis_client)
    optional_extractor = OptionalParamExtractor(
        llm_client=llm_client, deployment_name=deployment,
    )
    api_error_translator = ApiErrorTranslator(
        llm_client=llm_client, deployment_name=deployment,
        redis_client=redis_client,
    )
    # Pre-generated Croatian param labels (built by
    # scripts/generate_param_labels.py). File is optional; if missing, the
    # labeler skips the preload tier and uses Redis cache → LLM fallback.
    # Bug fix (2026-05-28): `import json as _json` MUST be outside the
    # `if labels_path.exists()` branch — risky_tools loader below also uses
    # `_json`, and if labels file is missing the import never happens →
    # UnboundLocalError on every startup.
    from pathlib import Path as _Path
    import json as _json
    preloaded_labels: dict = {}
    try:
        labels_path = _Path(__file__).resolve().parents[2] / "config" / "param_labels_hr.json"
        if labels_path.exists():
            preloaded_labels = _json.loads(labels_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — preload is optional
        logger.warning("failed to load param_labels_hr.json: %s", e)
    param_labeler = ParamLabeler(
        llm_client=llm_client, deployment_name=deployment,
        redis_client=redis_client, preloaded=preloaded_labels,
    )
    # Risky-tool set (Filip 2026-05-17 Faza 9). Generated by
    # scripts/audit_registry_body_schemas.py from the registry. Missing file
    # = empty set = no warn ever (safe).
    risky_tool_ids: set = set()
    try:
        risky_path = _Path(__file__).resolve().parents[2] / "config" / "risky_tools.json"
        if risky_path.exists():
            risky_data = _json.loads(risky_path.read_text(encoding="utf-8"))
            risky_tool_ids = set(risky_data.get("missing_body") or []) | set(
                risky_data.get("likely_missing_body") or []
            )
    except Exception as e:  # noqa: BLE001 — optional config
        logger.warning("failed to load risky_tools.json: %s", e)
    conv_history = ConversationHistoryStore(redis_client)
    gdpr_audit = GdprAuditStore(redis_client)

    # --- L3 LLM Router + L8 LLM Formatter (Phase 4 rewrite) ---
    # AnchorIndex embeds all anchor phrases once (cached to disk by content
    # fingerprint). ToolSchemaBuilder maps registry → OpenAI tools=[] schema.
    # LLMRouter runs anchor top-50 → gpt-4o-mini tool-call. LLMFormatter
    # turns backend JSON into Croatian replies (no per-tool templates).
    import json as _json
    from pathlib import Path as _Path

    from services.router.anchor_index import AnchorIndex
    from services.router.tool_schema_builder import ToolSchemaBuilder

    repo_root = _Path(__file__).resolve().parents[2]
    tool_data_path = repo_root / "config" / "tool_data.json"
    registry_json_path = repo_root / "config" / "processed_tool_registry.json"
    anchor_cache_path = repo_root / "tests" / "benchmarks" / "router_anchor_cache.json"

    # SINGLE SOURCE OF TRUTH (Phase 2 of data consolidation, 2026-05-15).
    # tool_data.json union-merges what used to be three fragmented files:
    #   processed_tool_registry.json + tool_knowledge_base.json + tool_anchor_enrichments.json
    # The factory derives the legacy shapes that downstream consumers (router,
    # schema builder, anchor index) still expect. Once those consumers are
    # also migrated (Phase 3), the registry-shape view here can go away too.
    #
    # Faza 11.1+11.3 (Filip 2026-05-18): fail-fast s razumljivom porukom ako
    # je file missing/corrupt — raw FileNotFoundError/JSONDecodeError teško
    # debugira u produkciji jer ide kroz worker startup chain.
    if not tool_data_path.exists():
        raise RuntimeError(
            f"tool_data.json not found at {tool_data_path}. "
            "Bot cannot start without registry — run scripts/sync_tools.py."
        )
    try:
        tool_data = _json.loads(tool_data_path.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as e:
        raise RuntimeError(
            f"tool_data.json corrupted at {tool_data_path}: {e}. "
            "Re-generate via scripts/sync_tools.py."
        ) from e
    tool_entries = tool_data.get("tools")
    if not isinstance(tool_entries, dict) or not tool_entries:
        raise RuntimeError(
            f"tool_data.json at {tool_data_path}: 'tools' key missing or empty. "
            "Re-generate via scripts/sync_tools.py."
        )
    _sample_tool = next(iter(tool_entries.values()))
    _required_keys = {"method", "path", "intent_summary"}
    _missing = _required_keys - set(_sample_tool.keys() if isinstance(_sample_tool, dict) else [])
    if _missing:
        raise RuntimeError(
            f"tool_data.json at {tool_data_path}: sample tool missing keys {_missing}. "
            "Schema is incomplete — re-generate via scripts/sync_tools.py."
        )

    # Derive REGISTRY-shape ({"tools": [list-of-tool-dicts]}). The router +
    # schema_builder iterate over this list.
    # CRIT-2 fix (2026-05-28): dependency_graph is empty in tool_data.json
    # (build_tool_data.py never copied it); read from processed_tool_registry
    # which has the real 144 deps. Single source of truth — same registry that
    # ToolRegistry facade already loads.
    _dep_graph: list = []
    try:
        _pr = _json.loads(registry_json_path.read_text(encoding="utf-8"))
        _dep_graph = _pr.get("dependency_graph") or []
    except Exception as e:  # noqa: BLE001 — non-fatal, deps are optional
        logger.warning("dependency_graph load failed: %s", e)
    registry_dict = {
        "tools": list(tool_entries.values()),
        "dependency_graph": _dep_graph,
    }

    # Derive TKB-shape ({op_id: {intent_summary, use_when, do_not_use_when, method}}).
    # tool_schema_builder._build_one() reads these per tool.
    tkb_dict = {
        op_id: {
            "intent_summary": entry.get("intent_summary", ""),
            "use_when": entry.get("use_when") or [],
            "do_not_use_when": entry.get("do_not_use_when") or [],
            "method": entry.get("method", "GET"),
        }
        for op_id, entry in tool_entries.items()
    }

    # Derive ANCHORS-shape ({op_id: [phrase, ...]}) for AnchorIndex.
    anchors_dict = {
        op_id: list(entry.get("anchors") or [])
        for op_id, entry in tool_entries.items()
        if entry.get("anchors")
    }

    # Flat operation_id → intent_summary map for the clarify-cards UX.
    tkb_intents_index = {
        op_id: entry.get("intent_summary", "")
        for op_id, entry in tool_entries.items()
    }

    # operation_id → {param_name: param_def} for param-asking. Engine reads
    # `required`, `dependency_source`, `param_type`, `description` per param
    # to compute missing required + render Croatian questions.
    tool_parameters_index = {
        op_id: (entry.get("parameters") or {})
        for op_id, entry in tool_entries.items()
    }

    _log_config_freshness(registry_json_path, tool_data_path)

    async def _embed_fn(texts: list[str]) -> list[list[float]]:
        # MED-3 (Filip 2026-05-29): per-batch timeout to prevent worker hang on
        # Azure throttling during init. AnchorIndex.build() embeds in chunks of
        # 256 — even at slow Azure latency, a single batch should not exceed
        # 60s. asyncio.wait_for raises TimeoutError → AnchorIndex retries with
        # cached partial data (graceful degradation). Without this, init could
        # hang forever (we saw 90s+ pre-fix during dev).
        import asyncio as _asyncio
        r = await _asyncio.wait_for(
            embedder.embeddings.create(
                input=texts,
                model=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
            ),
            timeout=60.0,
        )
        return [d.embedding for d in r.data]

    anchor_index = AnchorIndex(
        anchors_data=anchors_dict,
        cache_path=anchor_cache_path,
        embedding_deployment=settings.AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
    )
    schema_builder = ToolSchemaBuilder.from_registry(registry_dict)
    router = LLMRouter(
        anchor_index=anchor_index,
        schema_builder=schema_builder,
        registry=registry_dict,
        tkb=tkb_dict,
        llm_client=llm_client,
        embed_fn=_embed_fn,
        deployment_name=deployment,
    )
    try:
        await router.initialize()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "LLMRouter initialize failed (engine will return errors): %s", e,
        )

    formatter_llm = LLMFormatter(
        llm_client=llm_client,
        deployment_name=deployment,
        registry=registry_dict,
        pii_scrubber=pii,
    )

    # Phase E scoper — narrows the 950-tool catalog per (tenant, persona)
    # before anchor retrieval. Reads tenant configs under config/tenants/.
    catalog_scoper = CatalogScoper(
        tool_data=tool_data,
        tenants_dir=repo_root / "config" / "tenants",
    )

    engine = V2Engine(
        rate_limiter=rate_limiter,
        pii=pii,
        identity=identity,
        intent_type=intent_type,
        basics=basics,
        router=router,
        formatter_llm=formatter_llm,
        flow_engine=flow_engine,
        flow_store=flow_store,
        executor=executor,
        pending_mut_store=pending_mut,
        telemetry=telemetry,
        pending_clarify_store=pending_clarify,
        conversation_history_store=conv_history,
        gdpr_audit_store=gdpr_audit,
        tkb_intents=tkb_intents_index,
        catalog_scoper=catalog_scoper,
        pending_params_store=pending_params,
        tool_parameters=tool_parameters_index,
        typeid_map=type_resolver.build_typeid_map(tool_entries),
        optional_extractor=optional_extractor,
        api_error_translator=api_error_translator,
        param_labeler=param_labeler,
        risky_tool_ids=risky_tool_ids,
    )

    return V2EngineBundle(
        engine=engine,
        identity=identity,
        conversation_history=conv_history,
        pending_mutation=pending_mut,
        pending_clarify=pending_clarify,
        pending_params=pending_params,
        gdpr_audit=gdpr_audit,
    )
