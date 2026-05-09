"""V2 Engine — orchestrates L-1 → L8 in order.

Single entry: `await engine.process_message(phone, query)` → reply text.

Wiring (top-down):
    L-1 RateLimiter        → blocked? short-circuit with cooldown msg
    L0.5 PIIScrubber       → safe text for LLM downstream
    L0   IdentityContext   → personId + masterData (cached)
    L1   SpecialIntents    → terminal if matched (welcome/GDPR/help)
    L4   Flow continuation → if mid-flow, route through engine
    L2a  IntentType        → type bucket (or safe fallback)
    L2b  DriverBasics      → if intent_type=question_about_self
                              and anchor matches, serve from cached masterData
    L3   Recognition       → otherwise top-K + LLM Judge
    L5   Confidence Gate   → high/med/low decision
    L6   Mutation Safeguard→ confirm dialog if mutating
    L7   Executor          → API call (with circuit breaker)
    L8   Formatter         → Croatian response

Failure-mode rule: every layer either returns or short-circuits. The
engine never crashes — it returns a Croatian fallback message instead.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from services.v2 import (
    confidence_gate, formatter, mutation_gate,
)
from services.v2.flow_engine import (
    FlowEngine, FlowStateStore, FLOWS,
    OUTCOME_CANCELLED, OUTCOME_DONE, OUTCOME_EXECUTE,
    OUTCOME_INVALID, OUTCOME_PROMPT,
)
from services.v2.identity import IdentityContext, IdentitySnapshot
from services.v2.intent_type import (
    IntentTypeClassifier, KIND_FLOW_REQUEST, KIND_QUESTION_ABOUT_SELF,
)
from services.v2.driver_basics import DriverBasicsAnchor
from services.v2.driver_quick_path import DriverQuickPath, QuickPathHit
from services.v2.recognition import RecognitionEngine
from services.v2.domain_picker import DomainPicker
from services.v2.domain_scoped_picker import DomainScopedToolPicker
from services.v2.telemetry import (
    TelemetryEvent, TelemetryLogger, hash_phone, set_request_context,
)
import uuid as _uuid
from services.v2.pii_scrubber import PIIScrubber
from services.v2.rate_limiter import RateLimiter
from services.v2.special_intents import detect_special_intent
from services.v2.executor import ToolExecutor
from services.v2.pending_mutation import (
    PendingMutationStore, STAGE_DOUBLE_FIRST, STAGE_DOUBLE_SECOND,
    STAGE_SINGLE, parse_reply,
)
from services.v2.pending_clarify import PendingClarifyStore
from services.v2.conversation_history import (
    ConversationHistoryStore, ConversationTurn,
)
from services.v2.query_normalizer import QueryNormalizer, NormalizedQuery
from services.v2.unified_responder import UnifiedResponder, UnifiedDecision
from services.v2.tool_use_responder import ToolUseResponder, ToolUseResult
from services.v2 import crisis_detector
from services.v2 import output_sanitizer
from services.v2 import input_sanitizer
from services.v2 import multi_intent_detector
from services.v2 import reference_resolver
from services.v2 import negation_handler
from services.v2 import meta_intents

logger = logging.getLogger(__name__)


@dataclass
class V2Engine:
    rate_limiter: RateLimiter
    pii: PIIScrubber
    identity: IdentityContext
    intent_type: IntentTypeClassifier
    basics: DriverBasicsAnchor
    recognition: RecognitionEngine
    flow_engine: FlowEngine
    flow_store: FlowStateStore
    executor: ToolExecutor
    pending_mut_store: PendingMutationStore
    # L2 deterministic driver quick-path (real-corpus-driven regex routing).
    # Optional: if None, layer is skipped. When provided, runs after L1
    # special intents and before L2a intent type — short-circuits ~80%
    # of driver traffic to MasterData fields with 0 LLM cost.
    quick_path: Optional[DriverQuickPath] = None
    # Telemetry sink for production observability (active learning input).
    # Optional: if None, no logging. Failures here NEVER block user request.
    telemetry: Optional[TelemetryLogger] = None
    # V3 hierarchical router (Stage 1 + Stage 2). When provided AND
    # the V2_USE_V3_ROUTER env flag is set, replaces the L3
    # RecognitionEngine + L5 confidence_gate path with the V3 flow:
    #   Stage 1: domain picker → top-3 of 9 domains (rich descriptions)
    #   Stage 2: tool picker within chosen domain (~20-80 candidates)
    # Empirical: +21pp domain accuracy on objective 100-q benchmark.
    # Default OFF — opt-in via flag, 0 risk to existing path.
    domain_picker: Optional[DomainPicker] = None
    scoped_picker: Optional[DomainScopedToolPicker] = None
    # Top-3 cards UX state — persists candidates between turns so user's
    # "1"/"2"/"3"/"ne" reply maps to the correct tool. When None, Top-3
    # cards still render but user must re-issue query verbosely on choice.
    pending_clarify_store: Optional[PendingClarifyStore] = None
    # Multi-turn context — last 3-5 turns passed to V3 pickers so the
    # router can resolve follow-ups like "a prošli mjesec" referencing
    # the previous turn's tool intent. Optional; pickers gracefully
    # accept empty list.
    conversation_history_store: Optional[ConversationHistoryStore] = None
    # L1.5 Query Normalizer — rewrites formal/manager queries into
    # canonical natural form before V3 router. Closes the adversarial
    # paraphrase gap (where Stage 1 collapses on vocab-stripped input).
    # Optional; if None, V3 router runs on raw query.
    query_normalizer: Optional[QueryNormalizer] = None
    # Unified LLM Responder — replaces V3 multi-stage routing with ONE
    # frontier-LLM call that does routing + param filling + response
    # generation given top-30 retrieved candidates. When provided AND
    # V2_USE_UNIFIED_RESPONDER=1 env flag is set, fully bypasses V3
    # DomainPicker → DomainScopedToolPicker chain. See
    # services/v2/unified_responder.py for rationale.
    unified_responder: Optional[UnifiedResponder] = None
    # Real tool_use 2-pass loop — Pass 1 picks tool, executor runs API,
    # Pass 2 LLM sees real JSON and writes natural Croatian response.
    # No template fill, no field_hint guess, no placeholder replace.
    # Edge cases (null fields, empty lists, errors) handled natively.
    # Opt-in via V2_USE_TOOL_USE=1; takes precedence over Unified and V3
    # when wired. See services/v2/tool_use_responder.py.
    tool_use_responder: Optional[ToolUseResponder] = None

    # ---- Internal helpers (Tier-A simplification 2026-05-08) ----
    @classmethod
    def _minimal_identity(cls, identity: IdentitySnapshot) -> dict:
        """Smallest identity dict accepted by `executor.execute()` —
        tenant_id only. Used by quick-path / V3 / unified execute call
        sites that don't need full identity context."""
        return {
            "tenant_id": identity.tenant_id,
        }

    @staticmethod
    def _stage_from_decision(decision: str) -> str:
        """Map mutation_gate decision → pending stage. DOUBLE confirms
        require an extra "TRAJNO" word; everything else is SINGLE."""
        if decision == mutation_gate.DECISION_DOUBLE:
            return STAGE_DOUBLE_FIRST
        return STAGE_SINGLE

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
        if self.conversation_history_store is not None and response:
            try:
                await self.conversation_history_store.append(
                    phone,
                    ConversationTurn(
                        user=query[:200],
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
            return rl.user_message

        # ---- L0.5 PII Scrubber ----
        scrubbed = self.pii.scrub(query)
        safe_query = scrubbed.scrubbed_text

        # ---- Negation flag (Damir-feedback signal) ----
        # User explicitly says "nije točno" → mark this turn so the bot
        # operator can spot wrong-routing patterns in KQL. Exact match
        # only; no heuristics, no synonym lists. Phrase is taught by
        # the formatter hint appended to read/mutate execute responses.
        self._current_is_negation = (
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
                phone_hash=hash_phone(phone),
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

        # ---- Pending mutation continuation? ----
        # If a confirm prompt is outstanding for this phone, the user's
        # next message is the reply to it. Must run BEFORE flow / L1 /
        # L2a so "Da" is interpreted as "execute pending", not as a
        # new request.
        pending = await self.pending_mut_store.load(phone)
        if pending is not None:
            return await self._continue_pending_mutation(
                phone, pending, safe_query, identity,
            )

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
                return await self._continue_flow(
                    phone, existing_flow, safe_query
                )

        # ---- L0.7 Crisis detection (ETHICAL OBLIGATION) ----
        # MUST run before special intents and routing. Drivers are a
        # high-stress profession; bot must redirect to crisis hotline
        # (Plavi telefon 116 123) on suicidal/self-harm signals and NOT
        # continue to fleet API calls. False-positive rate is near-zero
        # (deterministic Croatian phrase patterns + false-positive guards
        # for figurative usage like "ubit ću tu lozinku").
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
                phone_hash=hash_phone(phone),
                tenant_id=identity.tenant_id or "",
                query=safe_query,
            )
            return neg.response

        # ---- L0.8 Multi-intent detection ----
        # If user packed 2+ intents in one msg ("pokaži km i rezerviraj
        # sutra"), V3/Unified can only handle one. Render clarify prompt
        # asking which goes first. See services/v2/multi_intent_detector.
        multi = multi_intent_detector.detect(safe_query)
        if multi.detected:
            await self._log_telemetry(
                kind="multi_intent",
                phone_hash=hash_phone(phone),
                tenant_id=identity.tenant_id or "",
                query=safe_query,
                extra={"parts": multi.parts[:3]},
            )
            return multi.clarify_message

        # ---- L0.85 Meta-intents (self-reference / handoff / bug report / OOS) ----
        # "tko si ti" / "hoću pravog čovjeka" / "kako si" — answered inline,
        # no LLM/API call. See services/v2/meta_intents.py.
        meta = meta_intents.detect(safe_query)
        if meta.detected:
            await self._log_telemetry(
                kind=f"meta_intent:{meta.kind}",
                phone_hash=hash_phone(phone),
                tenant_id=identity.tenant_id or "",
                query=safe_query,
            )
            return meta.response

        crisis = crisis_detector.detect(safe_query)
        if crisis.detected:
            await self._log_telemetry(
                kind="crisis_signal",
                phone_hash=hash_phone(phone),
                tenant_id=identity.tenant_id or "",
                query="(scrubbed for privacy)",  # do NOT log raw text
                extra={"severity": crisis.severity},
            )
            return crisis.response

        # ---- L1 Special Intents ----
        special = detect_special_intent(
            safe_query,
            is_first_contact=identity.is_first_contact,
            first_name=identity.first_name,
            vehicle_name=identity.vehicle_name,
        )
        if special is not None:
            return special.response

        # ---- L1.5 Unknown phone gate ----
        # If identity could not resolve a person_id AND it's not the
        # first contact (welcome would have handled that), the user
        # cannot proceed — no tenant_id means downstream API calls fail.
        # Surface a clear enrollment message instead of silent failure.
        if not identity.is_known and not identity.is_first_contact:
            await self._log_telemetry(
                kind="unknown_phone_gate",
                phone_hash=hash_phone(phone),
                tenant_id="",
                query=safe_query,
            )
            return (
                "Bok! Tvoj broj još nije povezan s računom u sustavu MobilityOne.\n\n"
                "Da bismo nastavili, kontaktiraj svog managera ili podršku — "
                "trebaju te dodati u sustav s ovim brojem telefona.\n\n"
                "Kad to bude gotovo, javim ti se opet."
            )

        # ---- L2 Driver Quick-Path (regex deterministic, BEFORE LLM routes) ----
        # Real-corpus-driven: ~40% of driver traffic matches one of the
        # patterns in config/driver_quick_path.json (km, registracija,
        # tablica, marka, model, expiry dates, reservations, ...).
        # 0 LLM cost, ~50ms latency, deterministic accuracy.
        # CRITICAL: must run BEFORE V3/Unified/ToolUse routes, otherwise
        # short canonical queries pay 3 LLM calls when they could pay 0.
        if self.quick_path is not None:
            qp_hit = self.quick_path.match(safe_query)
            if qp_hit is not None:
                qp_response = await self._handle_quick_path_hit(
                    qp_hit, identity, safe_query,
                )
                if qp_response is not None:
                    await self._log_telemetry(
                        kind="quick_path_hit",
                        phone_hash=hash_phone(phone),
                        tenant_id=identity.tenant_id or "",
                        query=safe_query,
                        tool_picked=qp_hit.tool_id,
                        executed_tool=qp_hit.tool_id,
                        executed_success=True,
                        extra={
                            "pattern_id": qp_hit.pattern_id,
                            "kind": qp_hit.kind,
                            "field": qp_hit.field,
                        },
                    )
                    return qp_response

        # ---- Tool-Use Loop Responder (opt-in, highest precedence) ----
        # When V2_USE_TOOL_USE=1 AND tool_use_responder is wired, runs the
        # 2-pass tool_use loop: LLM picks tool → execute API → LLM sees
        # real JSON and writes natural Croatian response. Mutation gate
        # intercepts BEFORE Pass 1 fires the tool by short-circuiting the
        # injected executor. See services/v2/tool_use_responder.py.
        if (
            os.environ.get("V2_USE_TOOL_USE", "0") == "1"
            and self.tool_use_responder is not None
        ):
            recent_turns: list[dict] = []
            if self.conversation_history_store is not None:
                recent_turns = await self.conversation_history_store.load(phone)
            tu_response = await self._tool_use_route(
                phone, safe_query, identity, recent_turns=recent_turns,
            )
            if tu_response is not None:
                # F5.1: outer process_message wrapper appends to conversation_history.
                return tu_response

        # ---- Unified Responder (opt-in via flag) ----
        # When V2_USE_UNIFIED_RESPONDER=1 AND unified_responder is wired,
        # bypasses V3 hierarchical chain. ONE LLM call does routing +
        # param filling + response generation. Architecture pivot
        # 2026-05-07.
        if (
            os.environ.get("V2_USE_UNIFIED_RESPONDER", "0") == "1"
            and self.unified_responder is not None
        ):
            recent_turns: list[dict] = []
            if self.conversation_history_store is not None:
                recent_turns = await self.conversation_history_store.load(phone)
            unified_response = await self._unified_route(
                phone, safe_query, identity, recent_turns=recent_turns,
            )
            if unified_response is not None:
                # F5.1: outer process_message wrapper appends to conversation_history.
                return unified_response

        # ---- V3 Hierarchical Router (opt-in via flag) ----
        # When V2_USE_V3_ROUTER=1 AND domain_picker is wired, the engine
        # bypasses L3 RecognitionEngine + L5 confidence_gate and uses
        # V3 Stage 1 + Stage 2 instead. Mutation gate (L6), anti-loop,
        # telemetry — all UNCHANGED, still enforced.
        if (
            os.environ.get("V2_USE_V3_ROUTER", "0") == "1"
            and self.domain_picker is not None
            and self.scoped_picker is not None
        ):
            recent_turns: list[dict] = []
            if self.conversation_history_store is not None:
                recent_turns = await self.conversation_history_store.load(phone)
            # L0.9 reference resolver — handle "a što s onim drugim" follow-ups.
            ref = reference_resolver.resolve(safe_query, recent_turns)
            if ref.detected and ref.clarify_question:
                return ref.clarify_question
            v3_query = ref.rewritten_query if ref.detected and ref.rewritten_query else safe_query
            # L1.5 — normalize formal queries into canonical form. Cheap
            # skip-gate suppresses LLM call for short/canonical inputs.
            normalized: Optional[NormalizedQuery] = None
            if self.query_normalizer is not None:
                normalized = await self.query_normalizer.normalize(safe_query)
                if not normalized.error and not normalized.skipped:
                    # Pass canonical+original combined to V3 pickers
                    v3_query = normalized.both_for_routing
                    await self._log_telemetry(
                        kind="v3_query_normalized",
                        phone_hash=hash_phone(phone),
                        tenant_id=identity.tenant_id or "",
                        query=safe_query,
                        extra={
                            "canonical": normalized.canonical,
                            "style": normalized.style,
                            "intent_action": normalized.intent_action,
                            "intent_entity": normalized.intent_entity,
                        },
                    )
            v3_response = await self._v3_route(
                phone, v3_query, identity, recent_turns=recent_turns,
            )
            if v3_response is not None:
                # F5.1: outer process_message wrapper appends to conversation_history.
                return v3_response

        # NOTE: L2 quick-path moved BEFORE routing branches above (line ~305).
        # Falling through here means quick-path didn't match → continue to
        # L2a intent classifier.

        # ---- L2a Intent Type ----
        itype = await self.intent_type.classify(safe_query)

        # ---- L2b Driver Basics ----
        if itype.kind == KIND_QUESTION_ABOUT_SELF and identity.is_known:
            basics_match = await self.basics.match(safe_query)
            if basics_match.matched:
                return self._format_basics(identity, safe_query)

        # ---- Flow request? Start flow directly ----
        if itype.kind == KIND_FLOW_REQUEST:
            flow_name = self._guess_flow_name(safe_query)
            if flow_name and flow_name in FLOWS:
                return await self._start_flow(phone, flow_name, identity)

        # ---- L3 Recognition ----
        identity_summary = self._identity_summary(identity)
        recognized = await self.recognition.recognize(
            safe_query, identity_summary=identity_summary,
        )

        # ---- L5 Confidence Gate ----
        gate = confidence_gate.decide(recognized, identity.is_known)

        await self._log_telemetry(
            kind="recognize_and_gate",
            phone_hash=hash_phone(phone),
            tenant_id=identity.tenant_id or "",
            query=safe_query,
            tool_picked=recognized.tool_id,
            anchor_top1=(
                recognized.candidates[0].tool_id
                if recognized.candidates else None
            ),
            anchor_score=recognized.anchor_score or 0.0,
            llm_confidence=recognized.llm_confidence or 0.0,
            candidates_top5=[c.tool_id for c in (recognized.candidates or [])][:5],
            gate_decision=gate.decision,
            gate_reason=gate.log_reason,
            error=recognized.error,
        )

        if gate.decision == confidence_gate.DECISION_FALLBACK:
            return gate.fallback_message

        # Flow detected by recognition?
        if recognized.flow_name and recognized.flow_name in FLOWS:
            return await self._start_flow(phone, recognized.flow_name, identity)

        # ---- L6 Mutation Gate ----
        method = self.executor.method_of(recognized.tool_id) or "GET"
        mut = mutation_gate.decide_mutation(
            tool_id=recognized.tool_id,
            method=method,
            params=recognized.params,
            last_known_values={"last_mileage": identity.last_mileage},
            entity_label=identity.vehicle_name or "zapis",
        )
        if mut.decision != mutation_gate.DECISION_AUTO:
            # Persist what we want to execute on user "Da" so the next
            # turn finds it and runs it, not re-classifies the reply.
            stage = (
                STAGE_DOUBLE_FIRST
                if mut.decision == mutation_gate.DECISION_DOUBLE
                else STAGE_SINGLE
            )
            await self.pending_mut_store.save(
                phone,
                tool_id=recognized.tool_id,
                params=recognized.params,
                stage=stage,
            )
            return self._render_confirm_pending(mut)

        # ---- L7 Executor ----
        exec_result = await self.executor.execute(
            tool_id=recognized.tool_id,
            params=recognized.params,
            identity_summary=self._minimal_identity(identity),
        )

        if exec_result.circuit_open:
            return exec_result.error
        if not exec_result.success:
            # Internal error code (e.g. "http_500", "timeout") goes to log
            # only — user gets a generic Croatian message.
            logger.warning(
                "executor failure tool=%s err=%s",
                recognized.tool_id, exec_result.error,
            )
            return (
                "Tehnički problem. Pokušaj ponovo za nekoliko trenutaka."
            )

        # ---- L8 Formatter ----
        result = formatter.format_response(
            template_id=recognized.template_id,
            api_response_data=exec_result.data,
            field_hint=None,
            extra_context={"entity_label": identity.vehicle_name or "rezultata"},
        )
        return result.text

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _tool_use_route(
        self,
        phone: str,
        query: str,
        identity: IdentitySnapshot,
        recent_turns: Optional[list[dict]] = None,
    ) -> Optional[str]:
        """Real tool_use 2-pass loop entry. Mutation gate intercepts
        BEFORE the API actually fires by shimming the executor passed to
        the responder.

        Returns response string from LLM Pass 2, or None to fall through
        to V3/V2 path (only on hard errors).
        """
        identity_summary = {
            "first_name": identity.first_name,
            "vehicle_name": identity.vehicle_name,
            "last_mileage": identity.last_mileage,
            "tenant_id": identity.tenant_id,
            # F1 fix: pass IDs so the LLM can fill personId/vehicleId
            # params concretely instead of emitting placeholders that
            # runtime back-fills blind.
            "person_id": identity.person_id,
            "vehicle_id": identity.vehicle_id,
        }

        # Capture mutation interception: when responder's executor runs,
        # check method first. If mutating, save pending state and return
        # a sentinel result so Pass 2 LLM produces a confirm-style reply.
        intercepted: dict = {"saved_pending": False, "tool_id": None, "params": None}

        async def _gated_executor(tool_id: str, params: dict, _ident: dict):
            from types import SimpleNamespace
            method = self.executor.method_of(tool_id) or "GET"
            mut = mutation_gate.decide_mutation(
                tool_id=tool_id, method=method, params=params,
                last_known_values={"last_mileage": identity.last_mileage},
                entity_label=identity.vehicle_name or "zapis",
            )
            if mut.decision != mutation_gate.DECISION_AUTO:
                stage_kind = self._stage_from_decision(mut.decision)
                await self.pending_mut_store.save(
                    phone, tool_id=tool_id, params=params, stage=stage_kind,
                )
                intercepted["saved_pending"] = True
                intercepted["tool_id"] = tool_id
                intercepted["params"] = params
                # Return sentinel — Pass 2 will see this and write a
                # confirm-style HR message. We override that with our
                # canonical confirm prompt below.
                return SimpleNamespace(
                    success=False,
                    data={"_mutation_pending": True, "tool": tool_id, "params": params},
                    error="mutation_pending_confirm",
                    circuit_open=False,
                )
            # Read path — execute normally
            return await self.executor.execute(
                tool_id=tool_id, params=params,
                identity_summary=self._minimal_identity(identity),
            )

        # Per-call executor injection — preserves architecture
        # invariant (no private attribute swap across modules).
        result = await self.tool_use_responder.respond(
            query=query,
            identity_summary=identity_summary,
            recent_turns=recent_turns or [],
            top_k=30,
            executor=_gated_executor,
        )

        await self._log_telemetry(
            kind="tool_use_decision",
            phone_hash=hash_phone(phone),
            tenant_id=identity.tenant_id or "",
            query=query,
            tool_picked=result.tool_called,
            extra={
                "trace": result.reasoning_trace,
                "needs_clarify": result.needs_clarify,
                "error": result.error,
                "intercepted_mutation": intercepted["saved_pending"],
            },
        )

        if result.error and not intercepted["saved_pending"]:
            # Hard error — fall through to V3 path
            return None

        # If mutation was intercepted, override LLM response with our
        # canonical confirm prompt (deterministic for safety per CLAUDE.md §1.3).
        if intercepted["saved_pending"]:
            from services.v2 import mutation_gate as mg
            method = self.executor.method_of(intercepted["tool_id"]) or "POST"
            mut = mg.decide_mutation(
                tool_id=intercepted["tool_id"],
                method=method,
                params=intercepted["params"] or {},
                last_known_values={"last_mileage": identity.last_mileage},
                entity_label=identity.vehicle_name or "zapis",
            )
            return self._render_confirm_pending(mut)

        return result.response_text

    async def _unified_route(
        self,
        phone: str,
        query: str,
        identity: IdentitySnapshot,
        recent_turns: Optional[list[dict]] = None,
    ) -> Optional[str]:
        """Unified LLM Responder path — ONE LLM call for routing + params
        + response generation. Replaces V3 hierarchical chain.

        Returns response string, or None to fall through to V3/V2 path
        (when LLM errors out).

        Mutation gate (L6) STILL absolute — UnifiedResponder may set
        needs_confirm=true; engine renders confirm prompt and persists
        pending mutation, just like V3.
        """
        identity_summary = {
            "first_name": identity.first_name,
            "vehicle_name": identity.vehicle_name,
            "last_mileage": identity.last_mileage,
            "tenant_id": identity.tenant_id,
            # F1 fix: pass IDs so the LLM can fill personId/vehicleId
            # params concretely instead of emitting placeholders that
            # runtime back-fills blind.
            "person_id": identity.person_id,
            "vehicle_id": identity.vehicle_id,
        }

        decision = await self.unified_responder.respond(
            query=query,
            identity_summary=identity_summary,
            recent_turns=recent_turns or [],
            top_k=30,
        )

        await self._log_telemetry(
            kind="unified_decision",
            phone_hash=hash_phone(phone),
            tenant_id=identity.tenant_id or "",
            query=query,
            tool_picked=decision.tool_id,
            extra={
                "confidence": decision.confidence,
                "needs_confirm": decision.needs_confirm,
                "needs_clarify": decision.needs_clarify,
                "reasoning": decision.reasoning[:200],
                "error": decision.error,
            },
        )

        if decision.error or not decision.tool_id:
            # Fall through to V3 / V2 paths
            return None

        # Clarify request → return question to user
        if decision.needs_clarify and decision.clarify_question:
            return decision.clarify_question

        # Mutation gate (CLAUDE.md §1.3 absolute)
        method = self.executor.method_of(decision.tool_id) or "GET"
        mut = mutation_gate.decide_mutation(
            tool_id=decision.tool_id,
            method=method,
            params=decision.params,
            last_known_values={"last_mileage": identity.last_mileage},
            entity_label=identity.vehicle_name or "zapis",
        )
        if mut.decision != mutation_gate.DECISION_AUTO:
            stage_kind = self._stage_from_decision(mut.decision)
            await self.pending_mut_store.save(
                phone,
                tool_id=decision.tool_id,
                params=decision.params,
                stage=stage_kind,
            )
            return self._render_confirm_pending(mut)

        # Read-only execute
        exec_result = await self.executor.execute(
            tool_id=decision.tool_id,
            params=decision.params,
            identity_summary=self._minimal_identity(identity),
        )
        if exec_result.circuit_open:
            return exec_result.error
        if not exec_result.success:
            logger.warning(
                "unified executor failure tool=%s err=%s",
                decision.tool_id, exec_result.error,
            )
            return "Tehnički problem. Pokušaj ponovo za nekoliko trenutaka."

        # If LLM provided response_text with placeholders, fill them.
        # Otherwise fall back to default formatter.
        if decision.response_text:
            filled = self._fill_response_placeholders(
                decision.response_text, exec_result.data,
            )
            return filled

        result = formatter.format_response(
            template_id=None,
            api_response_data=exec_result.data,
            field_hint=None,
            extra_context={"entity_label": identity.vehicle_name or "rezultata"},
        )
        return result.text

    @staticmethod
    def _fill_response_placeholders(template: str, data: dict) -> str:
        """Replace {field_name} placeholders in LLM-generated response with
        actual values from API response. Missing fields render as '-'.
        Defensive: never raises, never leaks raw exceptions to user.
        """
        if not data or not isinstance(data, dict):
            return template
        try:
            import re
            def _repl(m):
                key = m.group(1).strip()
                # Support nested keys like {Vehicle.Plate}
                cur = data
                for part in key.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        cur = None
                        break
                if cur is None:
                    return "-"
                if isinstance(cur, (int, float)):
                    return f"{cur:,}".replace(",", " ")
                return str(cur)
            return re.sub(r"\{([^{}]+)\}", _repl, template)
        except (re.error, AttributeError, TypeError) as e:
            # C5 fix: narrowed from bare except. Regex failure or callback
            # type error are the only expected failures here. Log so we
            # surface broken templates instead of silently emitting raw
            # `{placeholder}` text to the user.
            logger.warning(
                "template fill failed (returning raw template): %s", e,
            )
            return template

    async def _v3_route(
        self,
        phone: str,
        query: str,
        identity: IdentitySnapshot,
        recent_turns: Optional[list[dict]] = None,
    ) -> Optional[str]:
        """V3 hierarchical routing path: Stage 1 (domain) → Stage 2 (tool).

        Returns response string, or None to fall through to V2 path
        (when V3 fails or is uncertain enough that legacy is safer).

        Stage 1: pick top-3 of 9 domains. If special_intents → return polite
        response (greeting, English fallback, OOS personal — handled inline).
        If confident: route to Stage 2.
        If not confident: render Top-3 cards via clarify_ui (NOT YET fully
        wired — currently falls through to V2 for low confidence).

        Stage 2: pick tool within chosen domain. Run mutation gate.
        Execute via L7. Format via L8.
        """
        s1 = await self.domain_picker.pick(
            query, recent_turns=recent_turns,
        )

        await self._log_telemetry(
            kind="v3_stage1",
            phone_hash=hash_phone(phone),
            tenant_id=identity.tenant_id or "",
            query=query,
            tool_picked=None,
            extra={
                "top_picks": [
                    {"domain": h.domain_id, "conf": h.confidence}
                    for h in s1.top_picks
                ],
                "is_confident": s1.is_confident,
                "needs_user_confirm": s1.needs_user_confirm,
            },
        )

        if s1.error or not s1.top_domain:
            # V3 failed; let V2 try
            return None

        # special_intents domain → handle inline (greeting/english/OOS)
        if s1.top_domain.domain_id == "special_intents":
            # Inline polite responses without API calls.
            q_lower = query.lower()
            if any(w in q_lower for w in ("bok", "pozdrav", "zdravo", "ćao", "cao", "dobar dan")):
                return "Bok! Kako vam mogu pomoći?"
            if any(w in q_lower for w in ("hello", "hi ", "hey", "where is", "i need", "tell me", "give me", "thanks")):
                return (
                    "Bok! Koristim hrvatski jezik. Možeš li pitati na hrvatskom?\n\n"
                    "Npr.: \"koja je moja registracija\", "
                    "\"kolika je moja kilometraža\", \"moje rezervacije\"."
                )
            if "ime" in q_lower or "zovem" in q_lower or "telefon" in q_lower:
                return (
                    "Tvoje osobne podatke ne mogu prikazati — to su privatni "
                    "podaci. Mogu li pomoći s vozilom ili rezervacijom?"
                )
            if "obriš" in q_lower and ("profil" in q_lower or "account" in q_lower or "nalog" in q_lower):
                return (
                    "Brisanje korisničkog profila nije dostupno preko bota. "
                    "Kontaktiraj svog administratora."
                )
            return "Razumio sam, ali ne mogu odgovoriti na to. Pokušaj drugačije ili kontaktiraj managera."

        # Low Stage-1 confidence → ASK USER which domain (Top-3 cards UX).
        # This is the "korisnik nosi smart load" mechanism Filip described:
        # bot is not silent, not wrong — it asks ONE clarification with
        # 3 button-friendly options. Empirical projection: clarify-rescuable
        # = 95% domain accuracy after one user tap.
        if not s1.is_confident:
            top3 = s1.top_picks[:3] if s1.top_picks else []
            if len(top3) >= 2:
                # Persist as DOMAIN-pick candidates. Resolver reroutes
                # into Stage 2 of the chosen domain on user tap.
                domain_cards = [
                    {
                        "kind": "domain",
                        "domain_id": h.domain_id,
                        "label": h.label,
                        "tool_id": "",  # filled after Stage 2
                        "original_query": query,
                    }
                    for h in top3
                ]
                if self.pending_clarify_store is not None:
                    await self.pending_clarify_store.save(
                        phone, candidates=domain_cards, original_query=query,
                    )

                lines = ["Razumio sam da ti treba nešto vezano uz:"]
                emoji = ["1️⃣", "2️⃣", "3️⃣"]
                for i, h in enumerate(top3):
                    lines.append(f"  {emoji[i]} {h.label}")
                lines.append("  ❌ Nešto drugo")
                lines.append("")
                lines.append("Odgovori brojem (1, 2, 3) ili 'ne'.")
                await self._log_telemetry(
                    kind="v3_clarify_top3",
                    phone_hash=hash_phone(phone),
                    tenant_id=identity.tenant_id or "",
                    query=query,
                    extra={
                        "top_picks": [
                            {"domain": h.domain_id, "conf": h.confidence}
                            for h in top3
                        ],
                    },
                )
                return "\n".join(lines)
            # Single weak pick — let V2 handle it
            return None

        # Stage 2: pick tool within chosen domain
        s2 = await self.scoped_picker.pick(
            query, s1.top_domain.domain_id,
            recent_turns=recent_turns,
        )

        await self._log_telemetry(
            kind="v3_stage2",
            phone_hash=hash_phone(phone),
            tenant_id=identity.tenant_id or "",
            query=query,
            tool_picked=s2.top_pick.tool_id if s2.top_pick else None,
            extra={
                "domain": s1.top_domain.domain_id,
                "is_mutating": s2.is_mutating,
                "needs_clarify": s2.needs_clarify,
            },
        )

        # Stage 2 MEDIUM confidence (0.5 ≤ conf < 0.85) AND multiple
        # plausible candidates → render Tool-Top-3 cards as PRIMARY UX.
        # User picks 1/2/3 → engine maps via pending_clarify_store and
        # executes. Empirical projection: turns 64% strict → ~85% effective.
        if (
            not s2.error and s2.top_pick
            and not s2.has_high_confidence
            and len(s2.top_picks) >= 2
            and s2.top_pick.confidence >= 0.5
        ):
            # Build candidate cards with intent_summary from TKB if available
            cards = []
            for pick in s2.top_picks[:3]:
                tkb_entry = (
                    self.scoped_picker.tkb_entry_for(pick.tool_id) or {}
                    if self.scoped_picker else {}
                )
                summary = tkb_entry.get("intent_summary") or pick.reasoning or pick.tool_id
                cards.append({
                    "tool_id": pick.tool_id,
                    "label": summary[:80],
                    "field_hint": pick.field_hint,
                    "params": {},
                })

            if self.pending_clarify_store is not None:
                await self.pending_clarify_store.save(
                    phone, candidates=cards, original_query=query,
                )

            await self._log_telemetry(
                kind="v3_stage2_clarify_top3",
                phone_hash=hash_phone(phone),
                tenant_id=identity.tenant_id or "",
                query=query,
                extra={
                    "candidates": [
                        {"tool": c["tool_id"], "label": c["label"]}
                        for c in cards
                    ],
                },
            )

            lines = ["Razumio sam, ali nisam 100% siguran. Što ti treba:"]
            emoji = ["1️⃣", "2️⃣", "3️⃣"]
            for i, c in enumerate(cards):
                lines.append(f"  {emoji[i]} {c['label']}")
            lines.append("  ❌ Ništa od toga — reci drugačije")
            lines.append("")
            lines.append("Odgovori brojem (1, 2, 3) ili 'ne'.")
            return "\n".join(lines)

        if s2.error or not s2.top_pick or not s2.has_high_confidence:
            # V3 Stage 2 failed or uncertain (and no Top-3 viable); fall to V2
            return None

        # Need clarify? Render question, save state for next turn.
        if s2.needs_clarify and s2.clarify_question:
            return s2.clarify_question

        # Got a confident tool → run mutation gate + execute directly.
        # NEVER bypass mutation gate — that's CLAUDE.md §1.3 invariant.
        chosen_tool = s2.top_pick.tool_id
        method = self.executor.method_of(chosen_tool) or "GET"
        # Build minimal params dict — Stage 2 didn't extract params yet.
        # If params are missing for a mutating tool, mutation gate will
        # surface this as out-of-range / missing required → confirm gate.
        params: dict = {}
        if s2.top_pick.field_hint:
            params["field_hint"] = s2.top_pick.field_hint

        mut = mutation_gate.decide_mutation(
            tool_id=chosen_tool,
            method=method,
            params=params,
            last_known_values={"last_mileage": identity.last_mileage},
            entity_label=identity.vehicle_name or "zapis",
        )
        if mut.decision != mutation_gate.DECISION_AUTO:
            stage_kind = self._stage_from_decision(mut.decision)
            await self.pending_mut_store.save(
                phone,
                tool_id=chosen_tool,
                params=params,
                stage=stage_kind,
            )
            await self._log_telemetry(
                kind="v3_mutation_gate",
                phone_hash=hash_phone(phone),
                tenant_id=identity.tenant_id or "",
                query=query,
                tool_picked=chosen_tool,
                mutation_decision=mut.decision,
            )
            return self._render_confirm_pending(mut)

        # Read-only execute (only GETs hit AUTO).
        exec_result = await self.executor.execute(
            tool_id=chosen_tool,
            params=params,
            identity_summary=self._minimal_identity(identity),
        )

        await self._log_telemetry(
            kind="v3_execute",
            phone_hash=hash_phone(phone),
            tenant_id=identity.tenant_id or "",
            query=query,
            executed_tool=chosen_tool,
            executed_success=bool(exec_result.success),
        )

        if exec_result.circuit_open:
            return exec_result.error
        if not exec_result.success:
            logger.warning(
                "v3 executor failure tool=%s err=%s",
                chosen_tool, exec_result.error,
            )
            # Fall through to V2 — maybe its anchor cosine finds something
            # the LLM-picked tool couldn't deliver.
            return None

        # Format with field_hint preserved for slicing.
        result = formatter.format_response(
            template_id=None,
            api_response_data=exec_result.data,
            field_hint=s2.top_pick.field_hint,
            extra_context={"entity_label": identity.vehicle_name or "rezultata"},
        )
        return result.text

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
        kwargs.setdefault(
            "is_negation",
            getattr(self, "_current_is_negation", False),
        )
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

    async def _handle_quick_path_hit(
        self,
        hit: QuickPathHit,
        identity: IdentitySnapshot,
        query: str,
    ) -> Optional[str]:
        """Resolve a deterministic L2 quick-path hit.

        Returns response string for the user, OR None to fall through to
        L2a/L3 (e.g. tool requires data we can't serve from identity cache).
        """
        # Terminal responses (English fallback / polite refusal): always
        # return immediately. Bot must NEVER stay silent on these.
        if hit.is_terminal_response:
            return hit.response_text or (
                "Razumio sam, ali ne mogu odgovoriti na to. Pokušaj drugačije "
                "ili kontaktiraj managera."
            )

        # Tool hit: serve from cached MasterData when possible (zero API call,
        # zero latency beyond regex match). If hit.field is set, format that
        # specific field; otherwise full snapshot.
        if hit.is_actionable and hit.tool_id == "get_MasterData" and identity.is_known:
            return self._format_basics(identity, hit.field or query)

        # For other tools (VehicleCalendar, MileageReports), defer to the
        # full L3 → L7 path which knows how to make the API call. Returning
        # None tells process_message to continue normal routing — the regex
        # match still ensured we DETECTED the right intent without LLM.
        return None

    def _format_basics(
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
        result = formatter.format_response(
            template_id="vehicle_data_field",
            api_response_data=data,
            field_hint=query,  # match against natural-language query
        )
        return result.text

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

    @staticmethod
    def _guess_flow_name(query: str) -> Optional[str]:
        # Keyword short-circuit only for high-confidence flow requests —
        # avoids an L3 LLM call when the trigger is unambiguous. L3 is
        # still the real path for everything else.
        q = query.lower()
        if any(w in q for w in ("rezerv", "booking", "auto sutra", "vozilo za")):
            return "booking"
        if any(w in q for w in ("upis", "unesi", "stanje", "evo km")):
            return "mileage"
        if any(w in q for w in ("prijav", "kvar", "stet", "ošteti")):
            return "case"
        return None

    async def _start_flow(
        self, phone: str, flow_name: str, identity: IdentitySnapshot,
    ) -> str:
        ctx = {
            "person_id":     identity.person_id,
            "vehicle_id":    identity.vehicle_id,
            "vehicle_name":  identity.vehicle_name,
        }
        outcome = self.flow_engine.start(flow_name, ctx)
        if outcome.new_state is not None:
            await self.flow_store.save(phone, outcome.new_state)
        return outcome.response or "Pokrećem postupak."

    async def _continue_flow(
        self, phone: str, state, user_input: str,
    ) -> str:
        outcome = self.flow_engine.handle(state, user_input)

        if outcome.kind == OUTCOME_INVALID:
            if outcome.new_state is not None:
                await self.flow_store.save(phone, outcome.new_state)
            return outcome.response

        if outcome.kind == OUTCOME_PROMPT:
            if outcome.new_state is not None:
                await self.flow_store.save(phone, outcome.new_state)
            # If the engine wants a tool lookup (EXEC_LOOKUP step),
            # the caller can run it and feed lookup_result back in
            # the next call. For POC v2 this is single-turn UI.
            return outcome.response or "..."

        if outcome.kind == OUTCOME_CANCELLED:
            await self.flow_store.clear(phone)
            return outcome.response or "Odustao sam."

        if outcome.kind == OUTCOME_EXECUTE:
            await self.flow_store.clear(phone)
            exec_result = await self.executor.execute(
                tool_id=outcome.tool_id,
                params=outcome.params,
                identity_summary={},
            )
            if not exec_result.success:
                return f"Akcija nije uspjela: {exec_result.error}"
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

    def _render_confirm_pending(self, mut) -> str:
        if mut.decision == mutation_gate.DECISION_DOUBLE:
            return mut.double_first_message
        return mut.confirm_message

    async def _resolve_pending_clarify(
        self, phone: str, pending, user_input: str,
        identity: IdentitySnapshot,
    ) -> Optional[str]:
        """Map user's reply ('1' / '2' / '3' / 'ne') to one of the saved
        Top-3 candidates. Returns response if resolved; None to fall
        through to fresh routing (user typed a brand new query).

        Candidate format expected: list of dicts with at least 'tool_id',
        'label' and optional 'description'. Saved when V3 Stage 2 had
        medium confidence and engine rendered choice cards.
        """
        text = (user_input or "").strip().lower()

        # Negative reply: cancel clarify, fall through
        if text in {"ne", "nista", "ništa", "drugo", "❌", "x", "n"}:
            await self.pending_clarify_store.clear(phone)
            return "U redu, reci drugačije što tražiš."

        # Numeric pick: 1/2/3 (or with emoji 1️⃣ 2️⃣ 3️⃣)
        digit_map = {
            "1": 0, "2": 1, "3": 2,
            "1️⃣": 0, "2️⃣": 1, "3️⃣": 2,
            "prvo": 0, "drugo": 1, "treće": 2, "trece": 2,
        }
        idx = digit_map.get(text)
        if idx is None:
            # Word-boundary check for short emoji-only or first-char digit
            if text and text[0] in "123":
                idx = int(text[0]) - 1
        if idx is None or idx >= len(pending.candidates):
            return None  # not a valid pick — re-route as new query

        chosen = pending.candidates[idx]
        await self.pending_clarify_store.clear(phone)

        # DOMAIN-pick path — user chose a domain from Stage 1 cards.
        # Re-run Stage 2 within that domain using original query, render
        # tool-Top-3 cards or execute confidently.
        if chosen.get("kind") == "domain" and chosen.get("domain_id"):
            domain_id = chosen["domain_id"]
            original = chosen.get("original_query") or pending.original_query
            if self.scoped_picker is None:
                return None
            s2 = await self.scoped_picker.pick(
                original, domain_id,
            )
            if s2.error or not s2.top_pick:
                return (
                    "Razumio sam domenu, ali nisam siguran o kojem alatu se "
                    "radi. Pokušaj opisati specifičnije."
                )
            # If high-conf direct execute; if medium → render tool cards
            if s2.has_high_confidence:
                tool_id = s2.top_pick.tool_id
                method = self.executor.method_of(tool_id) or "GET"
                params = {}
                mut = mutation_gate.decide_mutation(
                    tool_id=tool_id, method=method, params=params,
                    last_known_values={"last_mileage": identity.last_mileage},
                    entity_label=identity.vehicle_name or "zapis",
                )
                if mut.decision != mutation_gate.DECISION_AUTO:
                    stage = (
                        STAGE_DOUBLE_FIRST
                        if mut.decision == mutation_gate.DECISION_DOUBLE
                        else STAGE_SINGLE
                    )
                    await self.pending_mut_store.save(
                        phone, tool_id=tool_id, params=params, stage=stage,
                    )
                    return self._render_confirm_pending(mut)
                exec_result = await self.executor.execute(
                    tool_id=tool_id, params=params,
                    identity_summary=self._minimal_identity(identity),
                )
                if not exec_result.success:
                    return "Tehnički problem. Pokušaj ponovo."
                result = formatter.format_response(
                    template_id=None, api_response_data=exec_result.data,
                    field_hint=s2.top_pick.field_hint,
                    extra_context={"entity_label": identity.vehicle_name or "rezultata"},
                )
                return result.text
            # Stage 2 medium confidence → render tool cards
            tool_cards = []
            for pick in s2.top_picks[:3]:
                tkb_entry = (
                    self.scoped_picker.tkb_entry_for(pick.tool_id) or {}
                )
                summary = tkb_entry.get("intent_summary") or pick.reasoning or pick.tool_id
                tool_cards.append({
                    "tool_id": pick.tool_id,
                    "label": summary[:80],
                    "field_hint": pick.field_hint,
                    "params": {},
                })
            if self.pending_clarify_store is not None:
                await self.pending_clarify_store.save(
                    phone, candidates=tool_cards, original_query=original,
                )
            lines = [f"Razumio sam ({domain_id}). Što ti treba:"]
            emoji = ["1️⃣", "2️⃣", "3️⃣"]
            for i, c in enumerate(tool_cards):
                lines.append(f"  {emoji[i]} {c['label']}")
            lines.append("  ❌ Ništa od toga — pišem drugačije")
            lines.append("")
            lines.append("Odgovori brojem (1, 2, 3) ili 'ne'.")
            return "\n".join(lines)

        # If chosen is a tool — run mutation gate then execute.
        tool_id = chosen.get("tool_id")
        if not tool_id:
            return "Nešto je krenulo krivo s odabirom. Pokušaj opet."

        method = self.executor.method_of(tool_id) or "GET"
        params = chosen.get("params") or {}
        mut = mutation_gate.decide_mutation(
            tool_id=tool_id, method=method, params=params,
            last_known_values={"last_mileage": identity.last_mileage},
            entity_label=identity.vehicle_name or "zapis",
        )
        if mut.decision != mutation_gate.DECISION_AUTO:
            stage = (
                STAGE_DOUBLE_FIRST
                if mut.decision == mutation_gate.DECISION_DOUBLE
                else STAGE_SINGLE
            )
            await self.pending_mut_store.save(
                phone, tool_id=tool_id, params=params, stage=stage,
            )
            return self._render_confirm_pending(mut)

        exec_result = await self.executor.execute(
            tool_id=tool_id, params=params,
            identity_summary=self._minimal_identity(identity),
        )
        if not exec_result.success:
            return "Tehnički problem. Pokušaj ponovo za nekoliko trenutaka."
        result = formatter.format_response(
            template_id=None, api_response_data=exec_result.data,
            field_hint=chosen.get("field_hint"),
            extra_context={"entity_label": identity.vehicle_name or "rezultata"},
        )
        return result.text

    async def _continue_pending_mutation(
        self, phone: str, pending, user_input: str,
        identity: IdentitySnapshot,
    ) -> str:
        """Apply user's reply to a saved confirm-dialog state.

        Outcomes:
          execute   → run the mutation, clear state
          advance   → DOUBLE-stage 1 passed; ask for "TRAJNO" stage 2
          cancel    → user said no; clear state
          ambiguous → re-prompt; keep state
        """
        action = parse_reply(user_input, pending.stage)

        if action == "cancel":
            await self.pending_mut_store.clear(phone)
            return "U redu, odustajem."

        if action == "advance":
            # DOUBLE confirm: stage 1 (Da) → stage 2 (require TRAJNO)
            await self.pending_mut_store.save(
                phone,
                tool_id=pending.tool_id,
                params=pending.params,
                stage=STAGE_DOUBLE_SECOND,
            )
            return (
                "Za potvrdu ovog TRAJNOG brisanja upiši riječ "
                "TRAJNO velikim slovima."
            )

        if action == "ambiguous":
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
                    "Što hoćeš:\n"
                    "  1️⃣ Izvrši pending\n"
                    "  2️⃣ Odustani i napravi novo (tvoju zadnju poruku)\n"
                    "  3️⃣ Samo otkaži\n\n"
                    "Odgovori brojem 1, 2 ili 3."
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
        if pending_age_s > STALE_WARN_THRESHOLD and pending.stage == STAGE_SINGLE:
            # Bump to DOUBLE-style re-confirm requiring TRAJNO/POTVRDA word
            await self.pending_mut_store.save(
                phone,
                tool_id=pending.tool_id,
                params=pending.params,
                stage=STAGE_DOUBLE_FIRST,
            )
            return (
                f"Tvoja potvrda je stara ({int(pending_age_s)} sek). "
                "Za sigurnost: napiši još jednom 'Da' ili otkaži."
            )

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
                return (
                    "Tehnički problem prilikom izvršavanja akcije. "
                    "Pokušaj ponovo."
                )
            # Success — clear pending so the next "Da" doesn't replay it.
            await self.pending_mut_store.clear(phone)
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


async def make_v2_engine_for_production(
    *,
    redis_client,
    gateway,
    tool_registry,
    settings,
) -> V2EngineBundle:
    """Build a fully-wired V2Engine from infrastructure that main.lifespan
    has already constructed.

    Construction is conservative:
      - Lightweight layer guards (rate limiter, PII, intent type, flows,
        executor, pending stores, conversation history, quick path) are
        always instantiated.
      - V3 hierarchical router (domain_picker + scoped_picker) is wired
        when its config files exist; otherwise left None and engine
        falls back to the recognition path. RecognitionEngine itself
        is NOT yet wired here (1381 LOC, complex anchor/cache loading
        path) — when V3 is unavailable the engine returns its safe
        fallback message rather than crashing.
      - Failures during initialize() are logged and re-raised. The
        caller decides whether v2 init failure should fail the whole
        lifespan or just disable V2 traffic.

    Stores returned in the bundle are the same instances the engine uses,
    so cache-invalidation operations affect the live engine state.
    """
    from services.openai_client import (
        get_openai_client, get_embedding_client,
    )
    from services.v2.driver_quick_path import DriverQuickPath
    from services.v2.unified_responder import UnifiedResponder
    from services.v2.tool_use_responder import ToolUseResponder
    from services.v2.unified_retriever import UnifiedRetriever
    from services.v2 import hallucination_guard
    from pathlib import Path as _Path

    llm_client = get_openai_client()
    embedder = get_embedding_client()
    deployment = settings.AZURE_OPENAI_DEPLOYMENT_NAME

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
    identity = IdentityContext(redis_client, gateway, settings)
    intent_type = IntentTypeClassifier(llm_client, deployment)
    basics = DriverBasicsAnchor(embedder)
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
    conv_history = ConversationHistoryStore(redis_client)

    quick_path: Optional[DriverQuickPath] = None
    try:
        quick_path = DriverQuickPath()
        quick_path.load()  # idempotent; raises only on missing config
    except Exception as e:  # noqa: BLE001
        logger.warning("DriverQuickPath load failed (continuing without): %s", e)
        quick_path = None

    # --- V3 hierarchical router (Stage 1 + Stage 2) — opt-in via flag,
    # but we attempt construction so engine is ready when V2_USE_V3_ROUTER
    # is set on the env. Construction failures degrade gracefully to None
    # (engine.process_message routes through recognition fallback then).
    domain_picker_obj: Optional[DomainPicker] = None
    scoped_picker_obj: Optional[DomainScopedToolPicker] = None
    try:
        repo_root = _Path(__file__).resolve().parents[2]
        domains_path = repo_root / "config" / "tool_domains.json"
        rich_docs = repo_root / "config" / "rich_tool_docs.json"
        if domains_path.exists():
            domain_picker_obj = DomainPicker(
                llm_client=llm_client,
                deployment_name=deployment,
                domains_path=domains_path,
            )
            domain_picker_obj.load()
            scoped_picker_obj = DomainScopedToolPicker(
                llm_client=llm_client,
                deployment_name=deployment,
                registry=tool_registry,
                domain_picker=domain_picker_obj,
                rich_docs_path=rich_docs if rich_docs.exists() else None,
            )
            scoped_picker_obj.load()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "V3 router init failed (engine will fall back): %s", e,
        )
        domain_picker_obj = None
        scoped_picker_obj = None

    # --- Recognition (per-tool anchor + LLM Judge) ---
    # 1381 LOC retrieval engine. async initialize() builds the anchor
    # index from the tool registry (cached on first build, ~10-30s).
    # When initialize() fails (no embedder credentials, anchor cache
    # corruption, etc.) we fall back to a stub so the engine still
    # constructs and the rest of v2 traffic can proceed via quick-path
    # / V3 / unified.
    recognition_obj: object
    try:
        recognition_real = RecognitionEngine(
            embedder=embedder,
            llm_client=llm_client,
            deployment_name=deployment,
            tool_registry=tool_registry,
        )
        await recognition_real.initialize()
        recognition_obj = recognition_real
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "RecognitionEngine initialize failed (using stub): %s", e,
        )

        class _StubRecognition:
            async def recognize(self, *_args, **_kwargs):
                from services.v2.recognition import RecognitionResult
                return RecognitionResult(
                    tool_id=None,
                    rationale="recognition initialize failed",
                    error="recognition_init_failed",
                )

        recognition_obj = _StubRecognition()

    # --- UnifiedResponder + ToolUseResponder (opt-in via env flags) ---
    # Both share a UnifiedRetriever that adapts the (now-initialized)
    # RecognitionEngine into a top-K candidates feed. Constructed only
    # if recognition is real (stub recognition produces no candidates).
    unified_resp: Optional[UnifiedResponder] = None
    tool_use_resp: Optional[ToolUseResponder] = None
    try:
        if isinstance(recognition_obj, RecognitionEngine):
            retriever = UnifiedRetriever(
                recognition_engine=recognition_obj,
                registry=tool_registry,
            )
            unified_resp = UnifiedResponder(
                llm_client=llm_client,
                deployment_name=deployment,
                retriever=retriever,
            )

            async def _executor_adapter(tool_id, params, identity_summary):
                return await executor.execute(
                    tool_id=tool_id, params=params,
                    identity_summary=identity_summary,
                )

            tool_use_resp = ToolUseResponder(
                llm_client=llm_client,
                deployment_name=deployment,
                retriever=retriever,
                default_executor=_executor_adapter,
                sanitize_fn=output_sanitizer.sanitize,
                hallucination_check_fn=hallucination_guard.check,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Unified/ToolUse responders init failed: %s", e,
        )
        unified_resp = None
        tool_use_resp = None

    engine = V2Engine(
        rate_limiter=rate_limiter,
        pii=pii,
        identity=identity,
        intent_type=intent_type,
        basics=basics,
        recognition=recognition_obj,  # type: ignore[arg-type]
        flow_engine=flow_engine,
        flow_store=flow_store,
        executor=executor,
        pending_mut_store=pending_mut,
        quick_path=quick_path,
        telemetry=telemetry,
        domain_picker=domain_picker_obj,
        scoped_picker=scoped_picker_obj,
        pending_clarify_store=pending_clarify,
        conversation_history_store=conv_history,
        unified_responder=unified_resp,
        tool_use_responder=tool_use_resp,
    )

    return V2EngineBundle(
        engine=engine,
        identity=identity,
        conversation_history=conv_history,
        pending_mutation=pending_mut,
        pending_clarify=pending_clarify,
    )
