
import asyncio
import json
import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from openai import RateLimitError, APIStatusError, APITimeoutError
from prometheus_client import Counter as PromCounter, Histogram, Gauge

from config import get_settings
from services.utils.pattern_registry import PatternRegistry
from services.utils.llm_json import parse_llm_json
from services.context import UserContextManager
from services.openai_client import get_openai_client, get_llm_circuit_breaker
from services.circuit_breaker import CircuitOpenError
from services.errors import GatewayError, ErrorCode
from services.retry_utils import calculate_backoff
from services.text_normalizer import sanitize_for_llm
from services.tracing import get_tracer, trace_span

logger = logging.getLogger(__name__)
_tracer = get_tracer("ai_orchestrator")
def _get_settings():
    return get_settings()

# Prometheus LLM metrics
LLM_REQUEST_DURATION = Histogram(
    'llm_request_duration_seconds',
    'LLM API call duration in seconds',
    ['model', 'operation'],
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 30.0]
)
LLM_TOKENS_USED = PromCounter(
    'llm_tokens_total',
    'Total LLM tokens used',
    ['model', 'type']  # type: prompt | completion
)
LLM_REQUESTS_TOTAL = PromCounter(
    'llm_requests_total',
    'Total LLM API requests',
    ['model', 'status']  # status: success | error | rate_limit | timeout
)
LLM_COST_USD = PromCounter(
    'llm_cost_usd_total',
    'Estimated LLM cost in USD',
    ['model']
)
LLM_RATE_LIMIT_HITS = PromCounter(
    'llm_rate_limit_hits_total',
    'Total LLM rate limit hits',
    ['model']
)
LLM_CIRCUIT_OPEN = Gauge(
    'llm_circuit_breaker_open',
    'Whether LLM circuit breaker is open (1=open, 0=closed)',
    ['model']
)

try:
    import tiktoken
except ImportError:
    tiktoken = None


# Token budgeting constants
MAX_TOOLS_FOR_LLM = 25  # Maximum tools to include in LLM context
MIN_TOOLS_FOR_LLM = 5
# History budget: 10 messages max, 2000 tokens max for conversation history.
# At 20 concurrent requests, worst case = 20 × 2000 tokens × ~4 bytes = 160KB.
# Remaining token budget (6000) for system prompt + tools + response.
MAX_HISTORY_MESSAGES = 10
MAX_HISTORY_TOKEN_LIMIT = 2000  # Token cap for conversation history portion only
MAX_TOKEN_LIMIT = 8000          # Total budget including system prompt + tools

# Token counting overhead constants
CROATIAN_CHARS_PER_TOKEN = 4.6
MESSAGE_TOKEN_OVERHEAD = 3  # Tokens added per message (OpenAI format overhead)
FINAL_TOKEN_OVERHEAD = 3    # Final overhead added to total count

# System prompts
DEFAULT_SYSTEM_PROMPT = "Ti si MobilityOne AI asistent. Odgovaraj na hrvatskom. Budi koncizan."
RATE_LIMIT_ERROR_MSG = "Sustav je trenutno preopterećen. Pokušajte ponovno za minutu."
TIMEOUT_ERROR_MSG = "Sustav nije odgovorio na vrijeme. Pokušajte ponovno."



class AIOrchestrator:
    """
    Orchestrates AI interactions.

    Features:
    - Tool calling with forced execution
    - Parameter extraction
    - Response generation
    - Token budgeting & tracking
    - Exponential backoff for rate limits
    - Smart history management
    """

    # Retry configuration
    MAX_RETRIES = 3
    BASE_DELAY = 1.0
    MAX_JITTER = 0.5

    def __init__(self, cost_tracker=None) -> None:
        """Initialize AI orchestrator."""
        # Shared client: rate limiting + connection pooling across all services
        # SDK retries disabled - we use our own exponential backoff (1-4 seconds)
        self.client = get_openai_client()
        self._circuit_breaker = get_llm_circuit_breaker()

        self.model = _get_settings().AZURE_OPENAI_DEPLOYMENT_NAME

        # Redis-backed cost tracking (optional — graceful if None)
        self._cost_tracker = cost_tracker

        # Token tracking — serialized via asyncio.Lock so concurrent analyze()
        # calls don't corrupt counters under Python's non-atomic += on ints.
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_requests = 0
        self._rate_limit_hits = 0
        self._current_retry_status = None

        self.tokenizer = None
        if tiktoken:
            try:
                self.tokenizer = tiktoken.get_encoding("cl100k_base")
            except Exception as e:
                logger.warning(f"Tokenizer initialization error: {e}")
                logger.info("Falling back to approximate token counting.")

    def _count_tokens(self, messages: List[Dict[str, str]]) -> int:
        """Azure-safe token counting."""

        if not self.tokenizer:
            # Fallback token counting for Croatian language (when tiktoken unavailable)
            total_chars = sum(len(m.get("content") or "") for m in messages)
            return int(total_chars / CROATIAN_CHARS_PER_TOKEN) + len(messages) * MESSAGE_TOKEN_OVERHEAD

        num_tokens = 0
        for message in messages:
            num_tokens += MESSAGE_TOKEN_OVERHEAD
            for key, value in message.items():
                if value:
                    num_tokens += len(self.tokenizer.encode(str(value)))

        num_tokens += FINAL_TOKEN_OVERHEAD
        return num_tokens



    async def analyze(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        system_prompt: Optional[str] = None,
        tool_scores: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:

        # Apply Smart History (sliding window)
        filtered_conversation = self._apply_smart_history(messages)

        # Build final message list with system prompt first
        final_messages = []

        has_system_in_filtered = (
            filtered_conversation and
            len(filtered_conversation) > 0 and
            filtered_conversation[0].get("role") == "system"
        )

        if system_prompt and not has_system_in_filtered:
            final_messages.append({"role": "system", "content": system_prompt})

        final_messages.extend(filtered_conversation)

        trimmed_tools = self._apply_token_budgeting(tools, tool_scores)

        call_args = {
            "model": self.model,
            "messages": final_messages,
            "temperature": _get_settings().AI_TEMPERATURE,
            "max_tokens": _get_settings().AI_MAX_TOKENS
        }

        if trimmed_tools:
            call_args["tools"] = trimmed_tools
            call_args["tool_choice"] = "auto"

        # Retry with exponential backoff + circuit breaker
        last_error = None

        for attempt in range(self.MAX_RETRIES):
            _start = time.monotonic()
            try:
                # Circuit breaker: fail-fast if LLM is down
                response = await self._circuit_breaker.call(
                    f"llm:{self.model}",
                    self.client.chat.completions.create,
                    **call_args
                )
                _elapsed = time.monotonic() - _start
                self._total_requests += 1

                # Prometheus metrics
                LLM_REQUEST_DURATION.labels(model=self.model, operation="analyze").observe(_elapsed)
                LLM_REQUESTS_TOTAL.labels(model=self.model, status="success").inc()
                LLM_CIRCUIT_OPEN.labels(model=self.model).set(0)

                usage_data = None
                if hasattr(response, 'usage') and response.usage:
                    self._total_prompt_tokens += response.usage.prompt_tokens
                    self._total_completion_tokens += response.usage.completion_tokens

                    # Token and cost metrics
                    LLM_TOKENS_USED.labels(model=self.model, type="prompt").inc(response.usage.prompt_tokens)
                    LLM_TOKENS_USED.labels(model=self.model, type="completion").inc(response.usage.completion_tokens)
                    cost = (response.usage.prompt_tokens * _get_settings().LLM_INPUT_PRICE_PER_1K +
                            response.usage.completion_tokens * _get_settings().LLM_OUTPUT_PRICE_PER_1K) / 1000
                    LLM_COST_USD.labels(model=self.model).inc(cost)

                    usage_data = {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.prompt_tokens + response.usage.completion_tokens
                    }

                    # Persist to Redis via CostTracker
                    if self._cost_tracker:
                        try:
                            await self._cost_tracker.record_usage(
                                response.usage.prompt_tokens,
                                response.usage.completion_tokens,
                            )
                        except Exception as ct_err:
                            logger.warning(f"CostTracker record failed: {ct_err}")

                    logger.debug(
                        f"Tokens: prompt={response.usage.prompt_tokens}, "
                        f"completion={response.usage.completion_tokens}, "
                        f"total={self._total_prompt_tokens + self._total_completion_tokens}"
                    )

                if not response.choices:
                    err = GatewayError(ErrorCode.SERVER_ERROR, "Empty AI response")
                    logger.error(str(err))
                    return {"type": "error", "content": "AI sustav nije vratio odgovor. Pokusajte ponovno."}

                message = response.choices[0].message

                if message.tool_calls and len(message.tool_calls) > 0:
                    # Parse ALL tool calls from LLM response
                    all_calls = []
                    for tc in message.tool_calls:
                        try:
                            arguments = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            err = GatewayError(ErrorCode.VALIDATION_ERROR, f"Invalid tool arguments: {tc.function.arguments[:100]}")
                            logger.warning(str(err))
                            arguments = {}
                        all_calls.append({
                            "tool": tc.function.name,
                            "parameters": arguments,
                            "tool_call_id": tc.id,
                        })

                    if len(all_calls) > 1:
                        logger.info(
                            f"LLM returned {len(all_calls)} parallel tool calls: "
                            f"{[c['tool'] for c in all_calls]}"
                        )

                    # Always return first call in legacy format for compatibility
                    # Additional calls included in "additional_calls" field
                    first = all_calls[0]
                    logger.debug(f"Tool call: {first['tool']}")

                    result = {
                        "type": "tool_call",
                        "tool": first["tool"],
                        "parameters": first["parameters"],
                        "tool_call_id": first["tool_call_id"],
                        "raw_message": message,
                        "usage": usage_data
                    }

                    if len(all_calls) > 1:
                        result["additional_calls"] = all_calls[1:]

                    return result

                content = message.content or ""
                logger.debug(f"Text response: {len(content)} chars")

                return {"type": "text", "content": content, "usage": usage_data}

            except RateLimitError as e:
                LLM_REQUESTS_TOTAL.labels(model=self.model, status="rate_limit").inc()
                LLM_RATE_LIMIT_HITS.labels(model=self.model).inc()
                LLM_REQUEST_DURATION.labels(model=self.model, operation="analyze").observe(time.monotonic() - _start)
                last_error = e
                result = await self._handle_rate_limit(attempt, "RateLimitError")
                if result:
                    return result
                continue

            except APIStatusError as e:
                if e.status_code == 429:
                    LLM_REQUESTS_TOTAL.labels(model=self.model, status="rate_limit").inc()
                    LLM_RATE_LIMIT_HITS.labels(model=self.model).inc()
                    LLM_REQUEST_DURATION.labels(model=self.model, operation="analyze").observe(time.monotonic() - _start)
                    last_error = e
                    result = await self._handle_rate_limit(attempt, "APIStatusError 429")
                    if result:
                        return result
                    continue

                LLM_REQUESTS_TOTAL.labels(model=self.model, status="error").inc()
                LLM_REQUEST_DURATION.labels(model=self.model, operation="analyze").observe(time.monotonic() - _start)
                err = GatewayError.from_status(e.status_code, f"API error {e.status_code}: {e.message}")
                logger.error(str(err))
                return {"type": "error", "content": "Doslo je do greske u komunikaciji. Pokusajte ponovno."}

            except APITimeoutError as e:
                LLM_REQUESTS_TOTAL.labels(model=self.model, status="timeout").inc()
                LLM_REQUEST_DURATION.labels(model=self.model, operation="analyze").observe(time.monotonic() - _start)
                last_error = e
                result = await self._handle_timeout(attempt)
                if result:
                    return result
                continue

            except CircuitOpenError as e:
                LLM_REQUESTS_TOTAL.labels(model=self.model, status="circuit_open").inc()
                LLM_CIRCUIT_OPEN.labels(model=self.model).set(1)
                logger.warning(f"Circuit breaker OPEN - LLM unavailable: {e}")
                return {
                    "type": "error",
                    "content": "AI sustav je trenutno nedostupan. Pokusajte ponovno za minutu."
                }

            except Exception as e:
                err = GatewayError(ErrorCode.SERVER_ERROR, f"AI error: {e}")
                logger.error(str(err), exc_info=True)
                return {"type": "error", "content": "Doslo je do neocekivane greske. Pokusajte ponovno."}

        # Should not reach here, but just in case
        err = GatewayError(ErrorCode.RETRY_EXHAUSTED, f"All {self.MAX_RETRIES} AI retries exhausted, last_error={last_error}")
        logger.error(str(err))
        return {"type": "error", "content": "Doslo je do greske. Pokusajte ponovno."}

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff with jitter."""
        return calculate_backoff(attempt, base_delay=self.BASE_DELAY, max_delay=60.0, jitter=self.MAX_JITTER)

    async def _handle_rate_limit(self, attempt: int, error_type: str) -> Optional[Dict[str, Any]]:
        """
        Handle rate limit errors with retry logic.

        P1 FIX: Now includes retry_status in return for caller visibility.
        """
        self._rate_limit_hits += 1
        self._current_retry_status = f"⏳ Provjeravam dostupnost... (pokušaj {attempt + 1}/{self.MAX_RETRIES})"

        if attempt < self.MAX_RETRIES - 1:
            delay = self._calculate_backoff(attempt)
            logger.warning(
                f"Rate limit hit ({error_type}). "
                f"Retry {attempt + 1}/{self.MAX_RETRIES} after {delay:.2f}s. "
                f"Total: {self._rate_limit_hits}"
            )
            await asyncio.sleep(delay)
            return None

        logger.error(f"Rate limit exceeded after {self.MAX_RETRIES} retries")
        self._current_retry_status = None
        return {"type": "error", "content": RATE_LIMIT_ERROR_MSG}

    async def _handle_timeout(self, attempt: int) -> Optional[Dict[str, Any]]:
        """
        Handle timeout errors with retry logic.

        P1 FIX: Now includes retry_status for caller visibility.
        """
        self._current_retry_status = f"⏳ Sustav je zauzet... (pokušaj {attempt + 1}/{self.MAX_RETRIES})"

        if attempt < self.MAX_RETRIES - 1:
            delay = self._calculate_backoff(attempt)
            logger.warning(f"API timeout. Retry {attempt + 1}/{self.MAX_RETRIES} after {delay:.2f}s")
            await asyncio.sleep(delay)
            return None

        logger.error(f"API timeout after {self.MAX_RETRIES} retries")
        self._current_retry_status = None
        return {"type": "error", "content": TIMEOUT_ERROR_MSG}

    def get_retry_status(self) -> Optional[str]:
        """Get current retry status message (P1 FIX for user feedback)."""
        return getattr(self, '_current_retry_status', None)

    def _apply_token_budgeting(
        self,
        tools: Optional[List[Dict]],
        tool_scores: Optional[List[Dict]],
    ) -> Optional[List[Dict]]:
        """
        Apply token budgeting - trim tool list to MAX_TOOLS_FOR_LLM.

        Args:
            tools: List of tool schemas (sorted by score DESC)
            tool_scores: List of {name, score, ...} dicts (sorted by score DESC)

        Returns:
            Trimmed list of tools (maintains sort order)
        """
        if not tools:
            return tools

        if not tool_scores:
            if len(tools) > MAX_TOOLS_FOR_LLM:
                logger.info(
                    f"Token budget: Trimming {len(tools)} -> {MAX_TOOLS_FOR_LLM} tools"
                )
                return tools[:MAX_TOOLS_FOR_LLM]
            return tools

        if len(tools) != len(tool_scores):
            logger.error(
                f"Token budgeting: tools count ({len(tools)}) != "
                f"tool_scores count ({len(tool_scores)}). "
                f"Returning tools without budgeting to avoid mismatch."
            )
            return tools

        if len(tools) > MAX_TOOLS_FOR_LLM:
            logger.info(
                f"Token budget: Trimming {len(tools)} → {MAX_TOOLS_FOR_LLM} tools "
                f"(keeping top {MAX_TOOLS_FOR_LLM} by score)"
            )
            return tools[:MAX_TOOLS_FOR_LLM]

        return tools


    def _apply_smart_history(
        self,
        messages: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """
        Sliding window on conversation history.

        Strategy (middle-out truncation):
        1. Keep system prompt intact
        2. Keep first message (establishes user identity/intent)
        3. Keep last N messages (recent context)
        4. If middle exceeds MAX_HISTORY_TOKEN_LIMIT, summarize it
        5. Hard cap: conversation history never exceeds 2000 tokens

        This prevents linear RAM growth from long-running chat sessions
        at 1GB pod limit.
        """
        # 1. Extract system prompt
        system_message = None
        if messages and messages[0]["role"] == "system":
            system_message = messages[0]
            conversation = messages[1:]
        else:
            conversation = messages

        # 2. Enforce message count cap (keep first + last N-1)
        if len(conversation) > MAX_HISTORY_MESSAGES:
            first_msg = conversation[0]
            conversation = [first_msg] + conversation[-(MAX_HISTORY_MESSAGES - 1):]

        # 3. Check token budget for conversation portion only
        conv_tokens = self._count_tokens(conversation)

        if conv_tokens <= MAX_HISTORY_TOKEN_LIMIT:
            # Under budget — return as-is
            result = []
            if system_message:
                result.append(system_message)
            result.extend(conversation)
            return result

        # 4. Over budget — middle-out truncation
        # Keep first message + last 4 messages, summarize the middle
        keep_recent = min(4, len(conversation))
        first_message = conversation[0] if conversation else None
        recent_history = conversation[-keep_recent:]
        # Summarize everything between first message and recent messages
        to_summarize = conversation[1:-keep_recent] if keep_recent < len(conversation) - 1 else []

        final_messages = []
        if system_message:
            final_messages.append(system_message)

        if first_message and first_message not in recent_history:
            final_messages.append(first_message)

        if to_summarize:
            summary_text = self._summarize_conversation(to_summarize)
            final_messages.append({
                "role": "system",
                "content": f"Sažetak prethodnog razgovora: {summary_text}"
            })

        final_messages.extend(recent_history)

        # 5. Final safety check — if still over total budget, hard-trim
        final_tokens = self._count_tokens(final_messages)
        if final_tokens > MAX_TOKEN_LIMIT:
            logger.warning(
                f"History still over total limit ({final_tokens} > {MAX_TOKEN_LIMIT}). "
                f"Hard-trimming to last 3 messages"
            )
            final_messages = []
            if system_message:
                final_messages.append(system_message)
            final_messages.extend(recent_history[-3:])

        return final_messages

    def _extract_entities(self, messages: List[Dict[str, str]]) -> Dict[str, List[str]]:
        """
        Extract entity references from messages.

        Prevents data loss when multiple UUIDs/plates are mentioned.

        Returns:
            Dict mapping entity types to lists of values
        """
        # Initialize with empty lists to prevent data loss
        entities = {
            "VehicleId": [],
            "PersonId": [],
            "BookingId": [],
            "LicencePlate": []
        }

        for msg in messages:
            content = msg.get("content", "")
            if not content:
                continue

            # PERFORMANCE FIX: Compute content_lower once per message
            content_lower = content.lower()

            # Use centralized PatternRegistry for consistent pattern matching
            uuids = PatternRegistry.find_uuids(content)
            for uuid in uuids:
                # CRITICAL FIX: Append to list instead of overwriting
                if "vehicle" in content_lower or "vozil" in content_lower:
                    entities["VehicleId"].append(uuid)
                elif "person" in content_lower or "osob" in content_lower:
                    entities["PersonId"].append(uuid)
                elif "booking" in content_lower or "rezerv" in content_lower:
                    entities["BookingId"].append(uuid)

            # Use centralized PatternRegistry for Croatian plates
            plates = PatternRegistry.find_plates(content)
            # CRITICAL FIX: Append all plates, not just last one
            entities["LicencePlate"].extend(plates)

        return entities

    def _format_entity_context(self, entities: Dict[str, List[str]]) -> str:
        """
        Format extracted entities as context string.


        Args:
            entities: Dict mapping entity types to lists of values

        Returns:
            Formatted string like "VehicleId=uuid1,uuid2, PersonId=uuid3"
        """
        parts = []
        for key, values in entities.items():
            if values:  # Only include non-empty lists
                parts.append(f"{key}={','.join(values)}")
        return ", ".join(parts)

    def _summarize_conversation(self, messages: List[Dict[str, str]]) -> str:
        """Summarize old conversation messages into a concise context."""
        entities = self._extract_entities(messages)
        summary_parts = []

        if any(entities.values()):
            summary_parts.append(f"Ranije entiteti: {self._format_entity_context(entities)}")

        role_counts = Counter(m.get("role") for m in messages)
        summary_parts.append(
            f"Prethodnih {len(messages)} poruka "
            f"({role_counts.get('user', 0)} user, {role_counts.get('assistant', 0)} assistant, {role_counts.get('tool', 0)} tool calls)"
        )

        return ". ".join(summary_parts)

    def get_token_stats(self) -> Dict[str, Any]:
        """Get token usage statistics."""
        return {
            "total_prompt_tokens": self._total_prompt_tokens,
            "total_completion_tokens": self._total_completion_tokens,
            "total_tokens": self._total_prompt_tokens + self._total_completion_tokens,
            "total_requests": self._total_requests,
            "rate_limit_hits": self._rate_limit_hits,
            "avg_tokens_per_request": (
                (self._total_prompt_tokens + self._total_completion_tokens) / self._total_requests
                if self._total_requests > 0 else 0
            )
        }

    async def extract_parameters(
        self,
        user_input: str,
        required_params: List[Dict[str, str]],
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Extract parameters from user input.

        Args:
            user_input: User message
            required_params: [{name, type, description}]
            context: Additional context

        Returns:
            Extracted parameters
        """
        with trace_span(_tracer, "ai.extract_parameters", {
            "ai.param_count": len(required_params),
            "ai.input_length": len(user_input),
        }):
            return await self._extract_parameters_inner(
                user_input, required_params, context
            )

    async def _extract_parameters_inner(
        self,
        user_input: str,
        required_params: List[Dict[str, str]],
        context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Inner implementation of extract_parameters."""
        param_desc = "\n".join([
            f"- {p['name']} ({p['type']}): {p.get('description', '')}"
            for p in required_params
        ])

        today = datetime.now(timezone.utc)
        tomorrow = today + timedelta(days=1)

        system = f"""Izvuci parametre iz korisnikove poruke.
Vrati JSON objekt s vrijednostima. Koristi null za nedostajuće parametre.

⚠️ KRITIČNO - NE IZMIŠLJAJ DATUME:
- AKO korisnik NIJE NAVEO datum/vrijeme - vrati null!
- NE pretpostavljaj današnji datum!
- NE dodavaj default vrijednosti za from/to/FromTime/ToTime!
- Samo ako korisnik EKSPLICITNO kaže "danas" ili "sutra" - onda koristi datum

Parametri:
{param_desc}

Datumski kontekst (KORISTI SAMO ako korisnik EKSPLICITNO spomene):
- Danas: {today.strftime('%Y-%m-%d')} ({today.strftime('%A')})
- Sutra: {tomorrow.strftime('%Y-%m-%d')}

Format vremena: ISO 8601 (YYYY-MM-DDTHH:MM:SS)

Hrvatske riječi za vrijeme:
- "sutra" = tomorrow (SAMO ako korisnik kaže "sutra")
- "danas" = today (SAMO ako korisnik kaže "danas")
- "prekosutra" = day after tomorrow (+2 dana od danas)
- "od X do Y" = from X to Y
- "ujutro" = 08:00
- "popodne" = 14:00
- "navečer" = 18:00
- "cijeli dan" = 08:00 do 18:00

VAŽNO: Ako korisnik daje samo vrijeme/datum kao odgovor (npr. "17:00" ili "prekosutra 9:00"),
to je vjerojatno odgovor na prethodno pitanje. Koristi taj datum/vrijeme za traženi parametar.

Vrati SAMO JSON, bez drugog teksta."""

        if context:
            sanitized_context = sanitize_for_llm(str(context))
            system += f"\n\nDodatni kontekst: {sanitized_context}"

        sanitized_input = sanitize_for_llm(user_input) if user_input else ""

        # Retry loop with backoff for rate limits + circuit breaker
        for attempt in range(self.MAX_RETRIES):
            try:
                response = await self._circuit_breaker.call(
                    f"llm:{self.model}",
                    self.client.chat.completions.create,
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": sanitized_input}
                    ],
                    temperature=0.1,
                    max_tokens=300
                )

                # Track extract_parameters usage in CostTracker
                if self._cost_tracker and hasattr(response, 'usage') and response.usage:
                    try:
                        await self._cost_tracker.record_usage(
                            response.usage.prompt_tokens,
                            response.usage.completion_tokens,
                        )
                    except Exception as e:
                        logger.warning(f"CostTracker record failed in extract_parameters: {e}")

                content = response.choices[0].message.content or "{}"
                parsed = parse_llm_json(content)
                if parsed is None:
                    raise json.JSONDecodeError("No JSON object found", content, 0)
                return parsed

            except json.JSONDecodeError:
                err = GatewayError(ErrorCode.VALIDATION_ERROR, "Parameter extraction JSON error")
                logger.warning(str(err))
                return {}
            except CircuitOpenError:
                logger.warning("Circuit breaker OPEN - skipping parameter extraction")
                return {}
            except RateLimitError:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(f"Rate limit in extract_parameters. Retry {attempt + 1}/{self.MAX_RETRIES} after {delay:.2f}s")
                    await asyncio.sleep(delay)
                    continue
                logger.error("Rate limit exceeded in extract_parameters")
                return {}
            except APITimeoutError:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self._calculate_backoff(attempt)
                    logger.warning(f"Timeout in extract_parameters. Retry {attempt + 1}/{self.MAX_RETRIES} after {delay:.2f}s")
                    await asyncio.sleep(delay)
                    continue
                logger.error("Timeout exceeded in extract_parameters")
                return {}
            except Exception as e:
                err = GatewayError(ErrorCode.SERVER_ERROR, f"Parameter extraction error: {e}")
                logger.error(str(err))
                return {}

        return {}

    def build_system_prompt(
        self,
        user_context: Dict[str, Any],
        flow_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Build system prompt with context.

        Args:
            user_context: User info
            flow_context: Current flow state

        Returns:
            System prompt
        """
        ctx = UserContextManager(user_context)
        name = ctx.display_name
        person_id = ctx.person_id or ""
        vehicle = ctx.vehicle

        today = datetime.now(timezone.utc)

        prompt = f"""Ti si MobilityOne AI asistent za upravljanje voznim parkom.
        Komuniciraj na HRVATSKOM jeziku. Budi KONCIZAN i JASAN.

        ---
        KORISNIK
        ---
        - Ime: {name}
        - ID: {person_id[:12]}...
        - Datum: {today.strftime('%d.%m.%Y')} ({today.strftime('%A')})
        """

        if vehicle and vehicle.plate:
            prompt += f"""- Vozilo: {vehicle.name or 'N/A'} ({vehicle.plate or 'N/A'})
        - Kilometraža: {vehicle.mileage or 'N/A'} km
        """

        prompt += """
        ---
        TVOJ POSAO
        ---
        Sustav automatski odabire pravi alat. Tvoj posao je:
        1. IZVUĆI parametre iz korisnikove poruke
        2. POZVATI alat s ispravnim parametrima
        3. FORMATIRATI odgovor korisniku

        ---
        PRAVILA ZA DATUME
        ---
        - "sutra" = sutrašnji datum
        - "danas" = današnji datum
        - ISO 8601 format: YYYY-MM-DDTHH:MM:SS
        - "od 9 do 17" = FromTime: ...T09:00:00, ToTime: ...T17:00:00

        ---
        KRITIČNO: ZABRANJENO IZMIŠLJANJE PODATAKA!
        ---
        NIKADA ne izmišljaj NIŠTA - SVE mora doći iz API-ja!

        ZABRANJENO izmišljati:
        -ime automobila/vozila
        -registracija vozila
        -datum istijeka registracije
        - Nazive tvrtki (leasing kuće, dobavljači, itd.)
        - Email adrese
        - Telefonske brojeve
        - Adrese
        - Bilo kakve kontakt podatke
        - UUID-ove ili ID-eve
        - Imena osoba
        - Registracijske oznake
        - Bilo kakve poslovne podatke
        - bilo šta drugo ...

        podaci su doslovni.

        AKO NEMAŠ PODATAK IZ API ODGOVORA:
        → RECI: "Nemam tu informaciju u sustavu."
        → NE izmišljaj nazive tvrtki kao "LeasingCo", "HighwaysInc", itd.!
        → NE koristi generičke placeholder nazive!
        → PITAJ korisnika ili pozovi odgovarajući API alat!

        PRIMJER ISPRAVNOG PONAŠANJA:
        - Korisnik pita: "Koja je moja leasing kuća?"
        - Ti MORAŠ pozvati API alat za dohvat podataka
        - Ako API ne vrati polje "LeasingProvider" → reci "Nemam tu informaciju"
        - NIKADA ne izmišljaj naziv leasing kuće!

        ---
        REZERVACIJA VOZILA (BOOKING FLOW)
        ---
        Kada korisnik traži vozilo ili želi rezervirati:

        !!! KRITIČNO - ZABRANJENO IZMIŠLJANJE !!!
        - NIKADA ne izmišljaj broj slobodnih vozila (npr. "3 vozila")
        - NIKADA ne izmišljaj registracijske oznake (npr. "ZG-1234-AB")
        - NIKADA ne generiraj odgovor o vozilima BEZ poziva get_AvailableVehicles!
        - Broj vozila MORA biti len(API_response.Data) - stvarni broj!

        PRIMJER GREŠKE (ZABRANJENO):
        - Korisnik: "Trebam vozilo sutra"
        - Ti: "Pronašao sam 3 slobodna vozila..." ← KRIVO! Nisi pozvao API!

        ISPRAVNO:
        - Korisnik: "Trebam vozilo sutra"
        - Ti: Prvo pozovi get_AvailableVehicles(from=..., to=...)
        - Tek nakon što dobiješ odgovor, reci: "Pronašao sam {len(Data)} vozila..."

        POTREBNI PARAMETRI:
        1. FromTime - datum i vrijeme polaska (obavezno)
        2. ToTime - datum i vrijeme povratka (obavezno)

        FLOW:
        1. Ako korisnik nije naveo FromTime/ToTime → PITAJ GA
        Primjer: "Za kada vam treba vozilo? (npr. sutra od 8:00 do 17:00)"

        2. Kada imaš FromTime i ToTime → OBAVEZNO pozovi get_AvailableVehicles
        Parametri: from=YYYY-MM-DDTHH:MM:SS, to=YYYY-MM-DDTHH:MM:SS

        3. Ako nema slobodnih vozila → javi korisniku i predloži drugi termin

        4. Ako ima slobodnih → prikaži PRVO slobodno vozilo i pitaj:
        "Pronašao sam slobodno vozilo: [naziv] ([registracija]).
            Želite li potvrditi rezervaciju?"
        Napomena: [naziv] i [registracija] MORAJU biti iz API odgovora!

        5. Ako korisnik potvrdi → pozovi post_VehicleCalendar s:
        - AssignedToId: korisnikov PersonId (iz konteksta)
        - VehicleId: ID odabranog vozila
        - FromTime: vrijeme polaska
        - ToTime: vrijeme povratka
        - AssigneeType: 1
        - EntryType: 0

        6. Potvrdi uspješnu rezervaciju ili javi grešku

        ---
        STIL
        ---
        - KRATKI odgovori na hrvatskom
        - SVE informacije MORAJU doći iz API odgovora!
        - NE izmišljaj podatke - koristi alate!
        - Ako nedostaju parametri, PITAJ korisnika
        - Ako API ne vrati podatak, reci "Nemam tu informaciju"
        """

        if flow_context and flow_context.get("current_flow"):
            prompt += f"""
        ---
        TRENUTNI TOK
        ---
        - Flow: {flow_context.get('current_flow')}
        - Stanje: {flow_context.get('state')}
        - Parametri: {flow_context.get('parameters', {})}
        - Nedostaju: {flow_context.get('missing_params', [])}
        """

        return prompt
