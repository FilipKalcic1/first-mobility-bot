"""
Confirmation Handler - Extracted confirmation-related logic from FlowHandler.

Single responsibility: Handle confirmation dialogs, selection flows,
and parameter modification during confirmation.

Extracted methods are standalone async functions that accept the needed
dependencies as parameters instead of `self`.
"""

import logging
from typing import Dict, Any, Tuple, Union

from services.booking_contracts import AssigneeType, EntryType
from services.error_translator import get_error_translator
from services.context.user_context_manager import UserContextManager
from services.errors import ConversationError, ErrorCode, InfrastructureError
from services.tracing import get_tracer, trace_span

logger = logging.getLogger(__name__)
_tracer = get_tracer("confirmation_handler")


def _get_vehicle_display(selected: Dict[str, Any]) -> Tuple[str, str]:
    """Extract vehicle name and plate from a selected item dict."""
    name = (
        selected.get("FullVehicleName") or
        selected.get("DisplayName") or
        selected.get("Name") or
        "Vozilo"
    )
    plate = selected.get("LicencePlate") or selected.get("Plate", "N/A")
    return name, plate


async def request_confirmation_flow(
    registry,
    confirmation_dialog,
    tool_name: str,
    parameters: Dict[str, Any],
    user_context: Dict[str, Any],
    conv_manager
) -> Dict[str, Any]:
    """
    Request confirmation for critical operation.

    Uses ConfirmationDialog for human-readable parameter display.
    - Vehicle IDs shown as names with plates
    - Dates shown in Croatian format
    - Users can modify params with "Biljeska: tekst" syntax
    """
    await conv_manager.add_parameters(parameters)

    tool = registry.get_tool(tool_name)

    # Build context data for better formatting
    context_data = {}
    if hasattr(conv_manager.context, 'tool_outputs'):
        # Get selected vehicle for display
        selected = conv_manager.get_selected_item()
        if selected:
            context_data['selected_vehicle'] = selected
        # Also check tool_outputs for vehicle info
        vehicles = conv_manager.context.tool_outputs.get("all_available_vehicles", [])
        if vehicles:
            context_data['vehicle'] = vehicles[0]

    # Format parameters using ConfirmationDialog
    param_displays = confirmation_dialog.format_parameters(
        tool_name=tool_name,
        parameters=parameters,
        tool_definition=tool,
        context_data=context_data
    )

    # Generate the confirmation message
    message = confirmation_dialog.generate_confirmation_message(
        tool_name=tool_name,
        parameters=param_displays,
        operation_description=tool.description[:100] if tool and tool.description else ""
    )

    await conv_manager.request_confirmation(message)
    conv_manager.context.current_tool = tool_name
    await conv_manager.save()

    return {
        "success": True,
        "data": {},
        "needs_input": True,
        "prompt": message
    }


async def handle_selection_flow(
    registry,
    formatter,
    sender: str,
    text: str,
    user_context: Dict[str, Any],
    conv_manager,
    handle_new_request_fn
) -> str:
    """Handle item selection."""
    with trace_span(_tracer, "flow.handle_selection", {
        "flow.sender_suffix": sender[-4:] if sender else "",
        "flow.input_preview": (text or "")[:80],
    }) as span:
        result = await _handle_selection_inner(
            registry, formatter,
            sender, text, user_context, conv_manager, handle_new_request_fn
        )
        span.set_attribute("flow.response_length", len(result) if isinstance(result, str) else 0)
        return result


async def _handle_selection_inner(
    registry,
    formatter,
    sender: str,
    text: str,
    user_context: Dict[str, Any],
    conv_manager,
    handle_new_request_fn
) -> str:
    """Inner implementation of handle_selection(), wrapped by OTel span."""
    selected = conv_manager.parse_item_selection(text)

    if not selected:
        return "Nisam razumio odabir.\nMolimo navedite broj (npr. '1') ili naziv."

    await conv_manager.select_item(selected)

    # Delete flows: show delete confirmation
    current_flow = conv_manager.get_current_flow()
    if current_flow and current_flow.startswith("delete_"):
        return await show_delete_confirmation(selected, conv_manager)

    params = conv_manager.get_parameters()

    vehicle_name, plate = _get_vehicle_display(selected)

    from_time = params.get("from") or params.get("FromTime", "N/A")
    to_time = params.get("to") or params.get("ToTime", "N/A")

    message = (
        f"**Potvrdite rezervaciju:**\n\n"
        f"Vozilo: {vehicle_name} ({plate})\n"
        f"Od: {from_time}\n"
        f"Do: {to_time}\n\n"
        f"_Potvrdite s 'Da' ili odustanite s 'Ne'._"
    )

    await conv_manager.request_confirmation(message)
    conv_manager.context.current_tool = "post_VehicleCalendar"
    await conv_manager.save()

    return message


async def handle_confirmation_flow(
    registry,
    executor,
    formatter,
    confirmation_dialog,
    is_question_fn,
    extract_filter_text_fn,
    sender: str,
    text: str,
    user_context: Dict[str, Any],
    conv_manager
) -> Union[str, Dict[str, Any]]:
    """Handle confirmation response. Returns str or dict (mid_flow_question marker)."""
    text_lower = text.lower()

    # NEW: Detect filter commands like "pokazi Passat" or "pokazi ZG"
    filter_text = extract_filter_text_fn(text)
    if filter_text and hasattr(conv_manager.context, 'tool_outputs'):
        all_vehicles = conv_manager.context.tool_outputs.get("all_available_vehicles", [])
        if all_vehicles:
            message = formatter.format_vehicle_list(all_vehicles, filter_text=filter_text)
            # Stay in SELECTING_ITEM state to allow selection
            await conv_manager.request_selection(message)
            await conv_manager.save()
            return message

    if any(keyword in text_lower for keyword in ["pokaz", "ostala", "druga", "jos vozila", "vise", "sva"]):
        # User wants to see all available vehicles
        if hasattr(conv_manager.context, 'tool_outputs'):
            all_vehicles = conv_manager.context.tool_outputs.get("all_available_vehicles", [])

            if len(all_vehicles) > 1:
                # Use formatter with filter instructions
                message = formatter.format_vehicle_list(all_vehicles)

                # Switch to SELECTING_ITEM state
                await conv_manager.request_selection(message)
                await conv_manager.save()

                return message

        return "Trenutno nema drugih dostupnih vozila."

    # Check for parameter modifications
    # Examples: "Biljeska: sluzbeni put", "Od: 10:00", "Note: xyz"
    modification = confirmation_dialog.parse_modification(text)
    if modification:
        param_name, new_value = modification
        params = conv_manager.get_parameters()

        # Update the parameter
        params[param_name] = new_value
        await conv_manager.add_parameters({param_name: new_value})
        await conv_manager.save()

        # Generate update message
        display_name = confirmation_dialog.DISPLAY_NAMES.get(param_name, param_name)
        message = (
            f"\u270f\ufe0f **Ažurirano!**\n"
            f"{display_name}: {new_value}\n\n"
            f"Potvrdite s **Da** ili nastavite s izmjenama."
        )
        return message

    confirmation = conv_manager.parse_confirmation(text)

    if confirmation is None:
        # P1 FIX: Detect if user is asking a question mid-flow
        # Allow them to get information without losing confirmation state
        if is_question_fn(text):
            logger.info(f"Mid-flow question detected: '{text[:50]}'")
            # Return special marker to let message_engine handle the question
            # The flow state is preserved so user can still confirm after
            return {"mid_flow_question": True, "question": text}

        return "Molim potvrdite s 'Da' ili odustanite s 'Ne'.\n_Ili dodajte parametar, npr: 'Bilješka: tekst'_"

    if not confirmation:
        await conv_manager.cancel()
        return "Operacija otkazana. Kako vam još mogu pomoći?"

    await conv_manager.confirm()

    tool_name = conv_manager.get_current_tool()
    if not tool_name:
        err = ConversationError(
            ErrorCode.CONTEXT_MISSING,
            "tool_name is None after confirm (state expired?)",
        )
        logger.warning(f"CONFIRMATION: {err}")
        await conv_manager.reset()
        return "Vaša sesija je istekla. Molimo pokrenite operaciju ponovno."
    params = conv_manager.get_parameters()
    selected = conv_manager.get_selected_item()

    # Build booking payload
    if tool_name == "post_VehicleCalendar":
        vehicle_id = None
        if selected:
            vehicle_id = selected.get("Id") or selected.get("VehicleId")
        if not vehicle_id and hasattr(conv_manager.context, 'tool_outputs'):
            vehicle_id = conv_manager.context.tool_outputs.get("VehicleId")

        if not vehicle_id:
            await conv_manager.reset()
            return "Greška: Nije odabrano vozilo. Pokušajte ponovno."

        from_time = params.get("from") or params.get("FromTime")
        to_time = params.get("to") or params.get("ToTime")

        if not from_time or not to_time:
            await conv_manager.reset()
            return "Greška: Nedostaje vrijeme rezervacije. Pokušajte ponovno."

        # Use UserContextManager for validated access
        ctx = UserContextManager(user_context)
        params = {
            "AssignedToId": ctx.person_id,
            "VehicleId": vehicle_id,
            "FromTime": from_time,
            "ToTime": to_time,
            "AssigneeType": int(AssigneeType.PERSON),  # Explicit int conversion
            "EntryType": int(EntryType.BOOKING),  # Explicit int conversion
            **({"Description": desc} if (desc := params.get("Description") if params.get("Description") is not None else params.get("description")) is not None else {})
        }
    else:
        if selected:
            item_id = selected.get("Id") or selected.get("VehicleId")
            if item_id:
                if tool_name and "delete_" in tool_name:
                    params["id"] = item_id
                else:
                    params["VehicleId"] = item_id

        if "from" in params and "FromTime" not in params:
            params["FromTime"] = params.pop("from")
        if "to" in params and "ToTime" not in params:
            params["ToTime"] = params.pop("to")

    # Execute
    tool = registry.get_tool(tool_name)
    if not tool:
        err = ConversationError(
            ErrorCode.FLOW_NOT_FOUND,
            f"Tool '{tool_name}' not found during confirmation execution",
            metadata={"tool_name": tool_name},
        )
        logger.warning(f"{err}")
        await conv_manager.reset()
        return f"Tehnički problem - alat '{tool_name}' nije pronađen."

    # FIX: Inject EntryType and AssigneeType into user_context for booking
    # These are required context params that need default values
    booking_context = user_context.copy()
    if tool_name == "post_VehicleCalendar":
        booking_context["entrytype"] = int(EntryType.BOOKING)  # 0
        booking_context["assigneetype"] = int(AssigneeType.PERSON)  # 1

    from services.tool_contracts import ToolExecutionContext
    execution_context = ToolExecutionContext.from_conv_manager(booking_context, conv_manager)

    result = await executor.execute(tool, params, execution_context)

    await cleanup_flow_state(conv_manager, context="confirmation execution")

    if result.success:
        if tool_name == "post_VehicleCalendar":
            vehicle_name = ""
            if selected:
                vehicle_name, plate = _get_vehicle_display(selected)
                if vehicle_name == "Vozilo":
                    vehicle_name = ""
                if plate and plate != "N/A":
                    vehicle_name = f"{vehicle_name} ({plate})"

            return (
                f"**Rezervacija uspješna!**\n\n"
                f"Vozilo: {vehicle_name}\n"
                f"Period: {params.get('FromTime')} -> {params.get('ToTime')}\n\n"
                f"Sretno na putu!"
            )

        # Case creation success message
        if tool_name == "post_AddCase":
            case_id = ""
            if isinstance(result.data, dict):
                case_id = result.data.get("Id", "") or result.data.get("CaseId", "")

            subject = params.get("Subject", "Slučaj")
            return (
                f"**Prijava uspješno kreirana!**\n\n"
                f"Naslov: {subject}\n"
                f"{'Broj slučaja: ' + str(case_id) if case_id else ''}\n\n"
                f"Naš tim će pregledati vašu prijavu i javiti vam se.\n"
                f"Kako vam još mogu pomoći?"
            )

        # Mileage update success message
        if tool_name == "post_AddMileage":
            value = params.get("Value", "")
            return (
                f"**Kilometraža uspješno unesena!**\n\n"
                f"Nova kilometraža: {value} km\n\n"
                f"Kako vam još mogu pomoći?"
            )

        # Delete operation success message
        if tool_name and "delete_" in tool_name:
            item_name = ""
            if selected:
                item_name = (
                    selected.get("Subject") or selected.get("VehicleName") or
                    selected.get("DisplayName") or selected.get("Name") or ""
                )
            return (
                f"**Uspješno obrisano!**\n\n"
                f"{item_name}\n\n"
                f"Kako vam još mogu pomoći?"
            )

        created_id = ""
        if isinstance(result.data, dict):
            created_id = result.data.get("created_id", "") or result.data.get("Id", "")

        return (
            f"**Operacija uspješna!**\n\n"
            f"{'ID: ' + str(created_id) if created_id else ''}\n\n"
            f"Kako vam još mogu pomoći?"
        )
    else:
        error = result.error_message or "Nepoznata greška"
        translator = get_error_translator()
        return translator.get_user_message(error, tool_name)


async def show_mileage_confirmation(params: Dict[str, Any], conv_manager) -> str:
    """Show mileage input confirmation."""
    vehicle_name = params.get("_vehicle_name", "Vozilo")
    plate = params.get("_vehicle_plate", "")
    value = params.get("Value") or params.get("mileage") or params.get("Mileage")

    message = (
        f"**Potvrda unosa kilometraže:**\n\n"
        f"Vozilo: {vehicle_name} ({plate})\n"
        f"Kilometraža: {value} km\n\n"
        f"_Potvrdite s 'Da' ili odustanite s 'Ne'._"
    )

    await conv_manager.request_confirmation(message)
    await conv_manager.save()

    return message


async def show_case_confirmation(
    params: Dict[str, Any], user_context: Dict[str, Any], conv_manager
) -> str:
    """Show case creation confirmation."""
    subject = params.get("Subject", "Prijava slučaja")
    description = params.get("Description") or params.get("Message", "")

    # Use UserContextManager for validated access
    ctx = UserContextManager(user_context)
    vehicle = ctx.vehicle
    vehicle_name = vehicle.name if vehicle else ""
    plate = vehicle.plate if vehicle else ""
    vehicle_line = f"Vozilo: {vehicle_name} ({plate})\n" if vehicle_name else ""

    message = (
        f"**Potvrda prijave slučaja:**\n\n"
        f"Naslov: {subject}\n"
        f"{vehicle_line}"
        f"Opis: {description}\n\n"
        f"_Potvrdite s 'Da' ili odustanite s 'Ne'._"
    )

    await conv_manager.request_confirmation(message)
    await conv_manager.save()

    return message


async def show_delete_confirmation(selected: Dict, conv_manager) -> str:
    """Show confirmation for delete operation."""
    name = (
        selected.get("Subject") or selected.get("VehicleName") or
        selected.get("FullVehicleName") or selected.get("DisplayName") or
        selected.get("Name") or selected.get("Description") or "Stavka"
    )

    details = []
    for field, label in [("FromTime", "Od"), ("ToTime", "Do"),
                         ("CreatedDate", "Datum"), ("Status", "Status"),
                         ("StartDate", "Početak"), ("EndDate", "Kraj")]:
        val = selected.get(field)
        if val:
            details.append(f"{label}: {val}")

    detail_text = "\n".join(details)
    if detail_text:
        detail_text = f"\n{detail_text}\n"

    message = (
        f"**Jeste li sigurni da želite obrisati:**\n\n"
        f"**{name}**{detail_text}\n"
        f"_Potvrdite s 'Da' ili odustanite s 'Ne'._"
    )

    await conv_manager.request_confirmation(message)
    await conv_manager.save()

    return message


async def cleanup_flow_state(conv_manager, context: str = "flow") -> None:
    """Complete and reset conversation state after tool execution.

    Shared cleanup for confirmation_handler and flow_handler.
    Always resets in-memory context even if Redis persist fails.
    """
    try:
        await conv_manager.complete()
        await conv_manager.reset()
    except Exception as e:
        err = InfrastructureError(
            ErrorCode.REDIS_UNAVAILABLE,
            f"State cleanup failed after {context}: {e}",
            cause=e,
        )
        logger.error(f"{err}")
        conv_manager.context.reset()
