"""
Flow Handler - Multi-turn conversation flows.

Single responsibility: Handle selection, confirmation, gathering, and availability flows.

v2.0 Changes:
- Integrated ConfirmationDialog for human-readable parameter display
- Added parameter modification support ("Bilješka: tekst", "Od: 10:00")
- Better Croatian formatting for dates, vehicles, etc.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    _HR_TZ = ZoneInfo("Europe/Zagreb")
except ZoneInfoNotFoundError:
    _HR_TZ = timezone(timedelta(hours=1))
from typing import Dict, Any, List, Optional, Union

from services.error_translator import get_error_translator
from services.confirmation_dialog import get_confirmation_dialog
from services.context import get_multiple_missing_prompts
from services.text_normalizer import clean_european_number
from services.errors import ConversationError, ErrorCode
from services.tracing import get_tracer, trace_span
from services.engine.confirmation_handler import (
    request_confirmation_flow,
    handle_selection_flow,
    handle_confirmation_flow,
    show_mileage_confirmation,
    show_case_confirmation,
)

logger = logging.getLogger(__name__)
_tracer = get_tracer("flow_handler")

# Pre-compiled regex patterns for _is_question() hot path
_QUESTION_PATTERNS = [re.compile(p) for p in [  # Applied to lowered text only
    r'\b(koliko|kolika|koliki)\b',   # How much/many
    r'\b(koji|koja|koje|kojih)\b',    # Which
    r'\b(što|sta|sto)\b',             # What
    r'\b(gdje|di)\b',                 # Where
    r'\b(kada|kad)\b',                # When
    r'\b(zašto|zasto)\b',             # Why
    r'\b(kako)\b',                    # How
    r'\b(tko|ko)\b',                  # Who
    r'\b(ima li|postoji li)\b',       # Is there
    r'\b(mogu li|može li|moze li)\b', # Can I/Can it
    r'\b(je li|jel)\b',               # Is it
]]

# Pre-compiled regex patterns for _extract_filter_text() hot path
_QUESTION_PHRASES = (
    "reci mi", "kaži mi", "kazi mi",
    "pokaži mi", "pokazi mi",
    "daj mi info", "trebam znati",
    "što je s", "sta je s", "sto je s",
)

_FILTER_PATTERNS = [re.compile(p) for p in [
    r'pokaži\s+(?:samo\s+)?(.+)',        # "pokaži Passat", "pokaži samo ZG"
    r'pokazi\s+(?:samo\s+)?(.+)',        # "pokazi Passat" (without č)
    r'filtriraj\s+(.+)',                  # "filtriraj Golf"
    r'samo\s+(.+)',                       # "samo Octavia"
    r'traži\s+(.+)',                      # "traži VW"
    r'trazi\s+(.+)',                      # "trazi VW" (without ž)
    r'nađi\s+(.+)',                       # "nađi Škoda"
    r'nadji\s+(.+)',                      # "nadji Skoda" (without đ)
]]

class FlowHandler:
    """
    Handles multi-turn conversation flows.

    Responsibilities:
    - Handle item selection
    - Handle confirmations
    - Handle parameter gathering
    - Handle availability checks
    """

    def __init__(self, registry, executor, ai, formatter) -> None:
        """Initialize flow handler."""
        self.registry = registry
        self.executor = executor
        self.ai = ai
        self.formatter = formatter
        self.confirmation_dialog = get_confirmation_dialog()

    async def handle_availability(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        user_context: Dict[str, Any],
        conv_manager
    ) -> Dict[str, Any]:
        """
        Handle vehicle availability check.

        Returns vehicles found and sets up confirmation flow.
        """
        # Check if time parameters are provided
        has_from = parameters.get("from") or parameters.get("FromTime")
        has_to = parameters.get("to") or parameters.get("ToTime")

        if not has_from or not has_to:
            # Need to gather time parameters first
            await conv_manager.start_flow(
                flow_name="booking",
                tool="get_AvailableVehicles",
                required_params=["from", "to"]
            )

            # Store any partial params we have
            if has_from or has_to:
                await conv_manager.add_parameters(parameters)

            await conv_manager.save()

            return {
                "needs_input": True,
                "prompt": (
                    "Za rezervaciju vozila trebam znati period.\n\n"
                    "Molim navedite **od kada do kada** trebate vozilo?\n"
                    "_Npr. 'sutra od 8 do 17' ili 'od ponedjeljka do srijede'_"
                )
            }

        tool = self.registry.get_tool(tool_name)
        if not tool:
            err = ConversationError(
                ErrorCode.FLOW_NOT_FOUND,
                f"Tool {tool_name} not found for availability check",
                metadata={"tool_name": tool_name},
            )
            logger.warning(f"{err}")
            return {
                "success": False,
                "error": f"Tool {tool_name} not found",
                "final_response": f"Tehnički problem - alat '{tool_name}' nije pronađen."
            }

        from services.tool_contracts import ToolExecutionContext
        execution_context = ToolExecutionContext.from_conv_manager(user_context, None)

        result = await self.executor.execute(tool, parameters, execution_context)

        if not result.success:
            return {
                "success": False,
                "data": {"error": result.error_message},
                "ai_feedback": result.ai_feedback or "Availability check failed",
                "final_response": "Došlo je do greške pri dohvatu podataka. Pokušajte ponovo."
            }

        # Extract items
        items = self._extract_items(result.data)

        if not items:
            return {
                "success": True,
                "data": result.data,
                "final_response": (
                    "Nažalost, nema slobodnih vozila za odabrani period.\n\n"
                    "Možete li odabrati drugi termin? Na primjer:\n"
                    "* 'Sutra od 8 do 17'\n"
                    "* 'Sljedeći tjedan od ponedjeljka do srijede'"
                )
            }

        # Show first vehicle - use Swagger field names only
        first_vehicle = items[0]

        vehicle_name = (
            first_vehicle.get("FullVehicleName") or
            first_vehicle.get("DisplayName") or
            "Vozilo"
        )
        plate = first_vehicle.get("LicencePlate", "N/A")
        vehicle_id = first_vehicle.get("Id")

        await conv_manager.set_displayed_items(items)
        await conv_manager.add_parameters(parameters)
        await conv_manager.select_item(first_vehicle)

        # to prevent partial state being saved if interrupted between updates
        if hasattr(conv_manager.context, 'tool_outputs'):
            # Build minimal vehicle data first (no async operations here)
            minimal_vehicles = []
            for v in items:
                minimal_vehicles.append({
                    "Id": v.get("Id") or v.get("VehicleId"),
                    "DisplayName": v.get("DisplayName") or v.get("FullVehicleName") or v.get("Name") or "Vozilo",
                    "LicencePlate": v.get("LicencePlate") or v.get("Plate") or "N/A"
                })

            # ATOMIC UPDATE: All tool_outputs changes in one dict.update() call
            conv_manager.context.tool_outputs.update({
                "VehicleId": vehicle_id,
                "vehicleId": vehicle_id,
                "all_available_vehicles": minimal_vehicles,
                "vehicle_count": len(items)
            })

            logger.info(f"Stored {len(minimal_vehicles)} vehicles for booking flow (atomic update)")

        conv_manager.context.current_tool = "post_VehicleCalendar"

        from_time = parameters.get("from") or parameters.get("FromTime")
        to_time = parameters.get("to") or parameters.get("ToTime")

        message = f"**Pronašao sam slobodno vozilo:**\n\n**{vehicle_name}** ({plate})\n\n"

        if from_time and to_time:
            message += f"Period: {from_time} -> {to_time}\n\n"

        if len(items) > 1:
            message += f"_(Ima jos {len(items) - 1} slobodnih vozila. Recite 'pokaži ostala' za listu)_\n\n"

        if from_time and to_time:
            message += "**Želite li potvrditi rezervaciju?** (Da/Ne)"
        else:
            message += "**Za nastavak trebam period rezervacije.** (npr. 'od sutra 9h do 17h')"

        await conv_manager.request_confirmation(message)
        await conv_manager.save()

        return {
            "success": True,
            "data": result.data,
            "needs_input": True,
            "prompt": message
        }

    async def request_confirmation(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        user_context: Dict[str, Any],
        conv_manager
    ) -> Dict[str, Any]:
        """Request confirmation for critical operation. Delegates to confirmation_handler."""
        return await request_confirmation_flow(
            self.registry, self.confirmation_dialog,
            tool_name, parameters, user_context, conv_manager
        )

    async def handle_selection(
        self,
        sender: str,
        text: str,
        user_context: Dict[str, Any],
        conv_manager,
        handle_new_request_fn
    ) -> str:
        """Handle item selection. Delegates to confirmation_handler."""
        return await handle_selection_flow(
            self.registry, self.formatter,
            sender, text, user_context, conv_manager, handle_new_request_fn
        )

    async def handle_confirmation(
        self,
        sender: str,
        text: str,
        user_context: Dict[str, Any],
        conv_manager
    ) -> Union[str, Dict[str, Any]]:
        """Handle confirmation response. Delegates to confirmation_handler."""
        with trace_span(_tracer, "flow_handler.handle_confirmation", {"sender_suffix": sender[-4:] if sender else "", "text_length": len(text)}) as span:
            result = await handle_confirmation_flow(
                self.registry, self.executor, self.formatter, self.confirmation_dialog,
                self._is_question, self._extract_filter_text,
                sender, text, user_context, conv_manager
            )
            span.set_attribute("result.response_length", len(result) if isinstance(result, str) else 0)
            return result

    # Semantic parameter descriptions for AI extraction
    PARAM_DESCRIPTIONS = {
        # Mileage parameters
        "Value": "Kilometraža vozila u km (npr. 14000, 50000)",
        "mileage": "Kilometraža vozila u km",
        "kilometraža": "Udaljenost u kilometrima",
        "Mileage": "Trenutna kilometraža vozila",
        # Vehicle parameters
        "VehicleId": "ID vozila (UUID format)",
        "vehicleId": "ID vozila",
        # Time parameters
        "from": "Početno vrijeme (datum i sat, npr. 'sutra u 9:00', 'prekosutra 10:00')",
        "to": "Završno vrijeme - do kada (npr. '17:00', 'prekosutra', 'petak')",
        "FromTime": "Početno vrijeme rezervacije (npr. 'sutra u 9:00')",
        "ToTime": "Završno vrijeme - DO KADA traje rezervacija (npr. '17:00', 'prekosutra 18:00', '28.12.2025.')",
        # Case/Support parameters
        "Description": "Opis problema, kvara ili situacije",
        "description": "Tekstualni opis",
        "Subject": "Naslov ili tema slučaja (npr. 'Prijava kvara', 'Problem s vozilom')",
        "subject": "Naslov slučaja",
    }

    async def handle_gathering(
        self,
        sender: str,
        text: str,
        user_context: Dict[str, Any],
        conv_manager,
        handle_new_request_fn
    ) -> str:
        """Handle parameter gathering with smart extraction."""
        with trace_span(_tracer, "flow_handler.handle_gathering", {"sender_suffix": sender[-4:] if sender else "", "text_length": len(text)}) as span:
            # Detect mid-flow questions before trying to extract parameters
            # Prevents "koliko je sati?" from being stored as a ToTime value
            if self._is_question(text):
                logger.info(f"GATHERING: Mid-flow question detected: '{text[:50]}'")
                span.set_attribute("result.type", "mid_flow_question")
                result = await handle_new_request_fn(sender, text, user_context, conv_manager)
                still_missing = conv_manager.get_missing_params()
                prompt = self._build_param_prompt(still_missing)
                return f"{result}\n\n---\n_{prompt}_"

            missing = conv_manager.get_missing_params()

            logger.info(f"GATHERING: missing={missing}, user_input='{text[:50]}'")
            span.set_attribute("missing_params_count", len(missing))

            # Build context for better extraction
            context = None
            if len(missing) == 1:
                param = missing[0]
                context = f"Bot je pitao korisnika za '{param}'. Korisnikov odgovor je vjerojatno vrijednost za taj parametar."

            # Add semantic context for each parameter
            # Use PARAM_DESCRIPTIONS if available, fall back to Swagger description
            param_specs = []
            for p in missing:
                desc = self.PARAM_DESCRIPTIONS.get(p)
                if not desc:
                    tool = self.registry.get_tool(conv_manager.get_current_tool())
                    if tool and p in tool.parameters:
                        desc = tool.parameters[p].description  # ParameterDefinition.description from Swagger
                param_specs.append({"name": p, "type": "string", "description": desc or p})

            extracted = await self.ai.extract_parameters(text, param_specs, context=context)

            logger.info(f"GATHERING: extracted={extracted}")

            # FALLBACK: If extraction failed and we need only one parameter,
            # use entire text as value (smart assumption)
            if len(missing) == 1 and not extracted.get(missing[0]):
                param = missing[0]
                # For time parameters, try basic Croatian date parsing before raw text
                if param.lower() in ['totime', 'fromtime', 'to', 'from']:
                    parsed = self._parse_croatian_date(text.strip())
                    if parsed:
                        extracted[param] = parsed
                        logger.info(f"GATHERING FALLBACK: Parsed Croatian date '{text.strip()}' → {parsed}")
                    elif text.strip() and len(text.strip()) < 50:
                        extracted[param] = text.strip()
                        logger.info(f"GATHERING FALLBACK: Using raw text '{text.strip()}' as {param}")
                # For Value (mileage), try to parse the number
                elif param.lower() in ['value', 'mileage']:
                    parsed = clean_european_number(text)
                    if parsed is not None:
                        extracted[param] = parsed
                        logger.info(f"GATHERING FALLBACK: Extracted number '{parsed}' as {param}")

            await conv_manager.add_parameters(extracted)

            if conv_manager.has_all_required_params():
                logger.info("GATHERING: All params collected, continuing flow")
                span.set_attribute("result.type", "all_params_collected")

                # CRITICAL: Handle flow completion based on flow type
                current_flow = conv_manager.get_current_flow()
                tool_name = conv_manager.get_current_tool()
                params = conv_manager.get_parameters()

                span.set_attribute("current_flow", current_flow or "")
                span.set_attribute("tool_name", tool_name or "")

                # MILEAGE INPUT: Show confirmation
                if current_flow == "mileage_input" and tool_name == "post_AddMileage":
                    return await self._show_mileage_confirmation(params, conv_manager)

                # CASE CREATION: Show confirmation
                if current_flow == "case_creation" and tool_name == "post_AddCase":
                    return await self._show_case_confirmation(params, user_context, conv_manager)

                # BOOKING FLOW: Execute availability check with collected params
                if current_flow == "booking" and tool_name == "get_AvailableVehicles":
                    logger.info("GATHERING: Booking flow - executing availability check")
                    result = await self.handle_availability(
                        tool_name=tool_name,
                        parameters=params,
                        user_context=user_context,
                        conv_manager=conv_manager
                    )
                    if result.get("needs_input"):
                        return result["prompt"]
                    if result.get("final_response"):
                        return result["final_response"]
                    return result.get("error", "Greška pri provjeri dostupnosti.")

                # ── GENERIC FLOW: Any tool not handled by specialized flows above ──
                if current_flow and current_flow.startswith("generic_"):
                    logger.info(f"GATHERING: Generic flow completion for {tool_name}")
                    is_mutation = current_flow == "generic_mutation"

                    if is_mutation:
                        # POST/PUT/PATCH/DELETE → show confirmation before executing
                        result = await self.request_confirmation(
                            tool_name=tool_name,
                            parameters=params,
                            user_context=user_context,
                            conv_manager=conv_manager
                        )
                        return result.get("prompt", "Potvrdite operaciju s 'Da' ili 'Ne'.")
                    else:
                        # GET with params → execute immediately, no confirmation needed
                        return await self._execute_generic_tool(
                            tool_name, params, user_context, conv_manager
                        )

                # Default: let new request handler continue
                return await handle_new_request_fn(sender, text, user_context, conv_manager)

            still_missing = conv_manager.get_missing_params()
            logger.info(f"GATHERING: Still missing {still_missing}")
            span.set_attribute("result.type", "still_missing")
            span.set_attribute("result.still_missing_count", len(still_missing))
            return self._build_param_prompt(still_missing)

    async def _show_mileage_confirmation(self, params: Dict[str, Any], conv_manager) -> str:
        """Show mileage input confirmation. Delegates to confirmation_handler."""
        return await show_mileage_confirmation(params, conv_manager)

    async def _show_case_confirmation(
        self, params: Dict[str, Any], user_context: Dict[str, Any], conv_manager
    ) -> str:
        """Show case creation confirmation. Delegates to confirmation_handler."""
        return await show_case_confirmation(params, user_context, conv_manager)

    async def _execute_generic_tool(self, tool_name: str, params: Dict[str, Any],
                                     user_context: Dict[str, Any], conv_manager) -> str:
        """Execute any tool generically and format the response.

        Used for generic_query flows (GET with user-provided params).
        Mutations go through request_confirmation → handle_confirmation instead.
        """
        tool = self.registry.get_tool(tool_name)
        if not tool:
            err = ConversationError(
                ErrorCode.FLOW_NOT_FOUND,
                f"Tool '{tool_name}' not found for generic execution",
                metadata={"tool_name": tool_name},
            )
            logger.warning(f"{err}")
            await conv_manager.reset()
            return f"Alat '{tool_name}' nije pronađen."

        from services.tool_contracts import ToolExecutionContext
        execution_context = ToolExecutionContext.from_conv_manager(user_context, conv_manager)

        result = await self.executor.execute(tool, params, execution_context)

        from services.engine.confirmation_handler import cleanup_flow_state
        await cleanup_flow_state(conv_manager, context="generic execution")

        if result.success:
            result_dict = {"success": True, "data": result.data, "operation": tool_name}
            return self.formatter.format_result(result_dict, tool)

        error = result.error_message or "Nepoznata greška"
        translator = get_error_translator()
        return translator.get_user_message(error, tool_name)

    def _extract_items(self, data: Any) -> List[Dict]:
        """Extract items from API response."""
        items = []
        if isinstance(data, dict):
            items = data.get("items", [])
            if not items:
                items = data.get("Data", [])
            if not items and "data" in data:
                nested = data["data"]
                if isinstance(nested, dict):
                    items = nested.get("Data", [])
                elif isinstance(nested, list):
                    items = nested
        return items

    def _build_param_prompt(self, missing: List[str]) -> str:
        """
        Build prompt for missing parameters.

        Uses centralized param_prompts from services.context module.
        Supports 30+ parameter types with Croatian user-friendly messages.
        """
        return get_multiple_missing_prompts(missing)

    @staticmethod
    def _parse_croatian_date(text: str) -> Optional[str]:
        """Parse Croatian date keywords into ISO datetime strings.

        Handles: danas, sutra, prekosutra, with optional time (od X do Y).
        Returns ISO format string or None if unparseable.
        """
        text_lower = text.lower().strip()
        now = datetime.now(_HR_TZ)

        # Map Croatian day keywords to date offsets
        day_map = {
            "danas": 0, "today": 0,
            "sutra": 1, "tomorrow": 1,
            "prekosutra": 2,
        }

        target_date = None
        for keyword, offset in day_map.items():
            if keyword in text_lower:
                target_date = now.date() + timedelta(days=offset)
                break

        if not target_date:
            # Try DD.MM.YYYY or DD.MM. format
            date_match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})?', text_lower)
            if date_match:
                day = int(date_match.group(1))
                month = int(date_match.group(2))
                year = int(date_match.group(3)) if date_match.group(3) else now.year
                try:
                    target_date = datetime(year, month, day).date()
                except ValueError as e:
                    logger.warning(
                        f"Invalid DD.MM.YYYY date in user input "
                        f"(d={day} m={month} y={year}): {e}"
                    )
                    return None

        if not target_date:
            return None

        # Extract time (e.g., "od 8", "u 14:30") — require keyword prefix
        # to avoid matching day numbers from dates like "20.03.2026"
        time_match = re.search(r'(?:od|u|at)\s+(\d{1,2})(?::(\d{2}))?', text_lower)
        hour = 8  # default if no time specified
        minute = 0
        if time_match:
            h = int(time_match.group(1))
            if 0 <= h <= 23:
                hour = h
                minute = int(time_match.group(2) or 0)

        return f"{target_date.isoformat()}T{hour:02d}:{minute:02d}:00"

    def _is_question(self, text: str) -> bool:
        """
        Detect if user input is a question (P1 FIX: mid-flow escape).

        Allows users to ask questions during confirmation flow
        without losing their booking state.

        Examples:
        - "Kolika je kilometraža?" → True
        - "Koja vozila imam?" → True
        - "Da" → False
        - "Bilješka: tekst" → False
        """
        text_lower = text.lower().strip()

        # Question word patterns (Croatian) — pre-compiled at module level
        for pattern in _QUESTION_PATTERNS:
            if pattern.search(text_lower):
                return True

        # Check for question mark
        if text.strip().endswith('?'):
            return True

        # Check for common question phrases
        for phrase in _QUESTION_PHRASES:
            if phrase in text_lower:
                return True

        return False

    def _extract_filter_text(self, text: str) -> Optional[str]:
        """
        Extract filter text from user input.

        Detects patterns like:
        - "pokaži Passat" → "Passat"
        - "pokaži samo ZG" → "ZG"
        - "filtriraj Golf" → "Golf"
        - "samo Octavia" → "Octavia"

        Returns filter text or None if no filter detected.
        """
        text_lower = text.lower().strip()

        # Filter patterns (Croatian) — pre-compiled at module level
        for pattern in _FILTER_PATTERNS:
            match = pattern.search(text_lower)
            if match:
                filter_val = match.group(1).strip()
                # Don't treat confirmation words as filters
                if filter_val not in ['da', 'ne', 'odustani', 'potvrdi', 'ostala', 'druga', 'vozila']:
                    return filter_val

        return None
