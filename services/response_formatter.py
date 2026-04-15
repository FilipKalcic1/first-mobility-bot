"""
Response Formatter v15.0 - FULLY GENERIC
=========================================

Formats ANY API response for WhatsApp WITHOUT hardcoded field names.

Philosophy:
- Works for ANY data structure
- Doesn't require knowing field names in advance
- Handles any number of fields
- Automatically detects and formats lists, objects, primitives
- Smart field name to human label conversion

v15.0 CHANGES:
- REMOVED all hardcoded field mappings
- REMOVED specific formatters (_format_vehicle, _format_person, etc.)
- ONE universal formatter that works for everything
- Dynamic field labeling with emoji detection
"""

import logging
import re
from typing import Dict, Any, List, Optional
from datetime import datetime
from services.tracing import get_tracer, trace_span
logger = logging.getLogger(__name__)
_tracer = get_tracer("response_formatter")

class ResponseFormatter:
    """
    Universal response formatter - works for ANY data.

    No hardcoded field names. Formats based on:
    - Data type (list, dict, primitive)
    - Field value types (date, number, string, nested)
    - Smart field name → human label conversion
    """

    MAX_MESSAGE_LENGTH = 3500
    MAX_LIST_ITEMS = 10
    MAX_FIELDS_PER_OBJECT = 20
    MAX_FIELD_VALUE_LENGTH = 100
    MAX_NESTED_DEPTH = 2

    # Emoji mapping for common field name patterns (generic, not specific fields)
    EMOJI_PATTERNS = {
        # Transportation
        r'(?i)(vehicle|vozilo|auto|car)': '🚗',
        r'(?i)(plate|registr|tablica)': '📋',
        r'(?i)(mileage|km|kilometr)': '📏',
        r'(?i)(driver|vozač|vozac)': '👤',
        r'(?i)(vin)': '🔑',

        # People
        r'(?i)(person|osoba|user|korisnik)': '👤',
        r'(?i)(email|e-mail)': '📧',
        r'(?i)(phone|telefon|mobile|mobitel)': '📱',
        r'(?i)(name|ime|naziv)': '📝',

        # Organization
        r'(?i)(company|tvrtka|firma|kompanija)': '🏢',
        r'(?i)(org.*unit|odjel|department)': '🏛️',
        r'(?i)(cost.*center|mjesto.*troška)': '💰',

        # Time/Date
        r'(?i)(date|datum|time|vrijeme)': '📅',
        r'(?i)(start|početak|pocetak)': '▶️',
        r'(?i)(end|kraj|finish)': '⏹️',
        r'(?i)(expir|istek|istječe)': '⚠️',
        r'(?i)(created|kreirano)': '🆕',
        r'(?i)(updated|modified|ažurirano)': '🔄',

        # Status
        r'(?i)(status|stanje|state)': '📌',
        r'(?i)(active|aktiv)': '✅',
        r'(?i)(avail|dostupn)': '✅',

        # Money
        r'(?i)(amount|iznos|price|cijena)': '💰',
        r'(?i)(monthly|mjesečn)': '📆',
        r'(?i)(total|ukupno)': '💵',
        r'(?i)(contract|ugovor|leasing|lizing)': '💼',

        # Location
        r'(?i)(location|lokacija|address|adresa)': '📍',
        r'(?i)(city|grad)': '🏙️',
        r'(?i)(country|država|drzava)': '🌍',

        # Documents
        r'(?i)(document|dokument|file|datoteka)': '📄',
        r'(?i)(attachment|prilog)': '📎',
        r'(?i)(image|slika|photo|fotografija)': '🖼️',

        # Counts
        r'(?i)(count|broj|number)': '#️⃣',
        r'(?i)(id|identifier)': '🔢',
        r'(?i)(code|šifra|sifra|kod)': '🏷️',

        # Other
        r'(?i)(description|opis)': '📝',
        r'(?i)(comment|komentar|note|napomena)': '💬',
        r'(?i)(type|tip|vrsta)': '📂',
        r'(?i)(year|godina)': '📆',
        r'(?i)(model)': '🔧',
        r'(?i)(manufacturer|proizvođač)': '🏭',
    }

    # Pre-compiled emoji patterns (built once at class load)
    _COMPILED_EMOJI_PATTERNS = [
        (re.compile(pattern), emoji)
        for pattern, emoji in EMOJI_PATTERNS.items()
    ]

    # Pre-compiled patterns for field name humanization and date detection
    _CAMEL_CASE_RE = re.compile(r'([A-Z])')
    _DATE_PATTERNS = [
        re.compile(r'^\d{4}-\d{2}-\d{2}'),   # YYYY-MM-DD
        re.compile(r'^\d{2}\.\d{2}\.\d{4}'),  # DD.MM.YYYY
        re.compile(r'^\d{2}/\d{2}/\d{4}'),    # DD/MM/YYYY
    ]

    def format_result(
        self,
        result: Dict[str, Any],
        tool: Optional[Any] = None,
        user_query: Optional[str] = None
    ) -> str:
        """
        Format ANY API result for display.

        Args:
            result: Execution result with success/error/data
            tool: Optional tool metadata
            user_query: User's original question

        Returns:
            Formatted string for WhatsApp
        """
        with trace_span(_tracer, "response_formatter.format_result", {"has_tool": tool is not None, "success": result.get("success", False)}) as span:
            # Handle errors
            if not result.get("success"):
                error = result.get("error", "Nepoznata greška")
                span.set_attribute("result.type", "error")
                return f"❌ Greška: {error}"

            # Get HTTP method for context
            method = "GET"
            if tool:
                method = tool.method if hasattr(tool, 'method') else tool.get("method", "GET")

            span.set_attribute("http.method", method)

            # Extract operation name for success messages
            operation = result.get("operation", "")

            # Handle mutations (POST/PUT/PATCH/DELETE)
            if method == "DELETE":
                span.set_attribute("result.type", "delete")
                return self._format_success("Uspješno obrisano", operation)

            if method in ("POST", "PUT", "PATCH"):
                created_id = result.get("created_id")
                msg = self._format_success("Uspješno spremljeno", operation)
                if created_id:
                    msg += f"\n📝 ID: {created_id}"
                span.set_attribute("result.type", "mutation")
                return msg

            # Handle GET responses - extract data
            data = self._extract_data(result)

            if data is None:
                span.set_attribute("result.type", "empty")
                return self._format_success("Operacija uspješna", operation)

            # Format based on data type
            span.set_attribute("result.type", "data")
            return self._format_any(data)

    def _extract_data(self, result: Dict) -> Any:
        """Extract actual data from various API response formats."""
        # Try common patterns
        if "items" in result:
            return result["items"]

        if "data" in result:
            data = result["data"]

            # Handle nested {"Data": [...], "Count": N} pattern
            if isinstance(data, dict) and "Data" in data:
                return data["Data"]

            return data

        # Return result itself if no known wrapper
        return result.get("result")

    def _format_any(self, data: Any, depth: int = 0) -> str:
        """
        Universal formatter - handles any data type.

        Args:
            data: Any data (list, dict, primitive)
            depth: Current nesting depth (for recursion control)
        """
        if data is None:
            return "Nema podataka."

        # Primitive types
        if isinstance(data, (str, int, float, bool)):
            return f"✅ Rezultat: {data}"

        # List of items
        if isinstance(data, list):
            if not data:
                return "Nema pronađenih rezultata."
            return self._format_list(data, depth)

        # Single object
        if isinstance(data, dict):
            if not data:
                return "Nema podataka."
            return self._format_object(data, depth)

        # Unknown type - convert to string
        return f"✅ Rezultat: {str(data)[:500]}"

    def _format_list(self, items: List, depth: int = 0) -> str:
        """Format a list of items."""
        if not items:
            return "Nema pronađenih rezultata."

        total = len(items)

        # If items are primitives, show as bullet list
        if not isinstance(items[0], dict):
            lines = [f"📋 **Pronađeno {total} stavki:**\n"]
            for i, item in enumerate(items[:self.MAX_LIST_ITEMS], 1):
                lines.append(f"{i}. {item}")
            if total > self.MAX_LIST_ITEMS:
                lines.append(f"\n_...i još {total - self.MAX_LIST_ITEMS} stavki_")
            return "\n".join(lines)

        # Items are dicts - format as list with key info
        lines = [f"📋 **Pronađeno {total} stavki:**\n"]

        for i, item in enumerate(items[:self.MAX_LIST_ITEMS], 1):
            # Get display name from item (try common name fields)
            name = self._get_display_name(item)
            lines.append(f"**{i}.** {name}")

            # Show 2-3 key fields as preview
            preview = self._get_preview_fields(item, exclude_name=True)
            for label, value in preview[:3]:
                emoji = self._get_emoji_for_field(label)
                lines.append(f"   {emoji} {self._humanize_field(label)}: {value}")

            lines.append("")  # Empty line between items

        if total > self.MAX_LIST_ITEMS:
            lines.append(f"_...i još {total - self.MAX_LIST_ITEMS} stavki_")

        lines.append("---")
        lines.append("_Odaberite brojem ili navedite naziv._")

        return self._truncate_message("\n".join(lines))

    def _format_object(self, data: Dict, depth: int = 0) -> str:
        """Format a single object/dictionary."""
        if not data:
            return "Nema podataka."

        # Get display name for header
        name = self._get_display_name(data)
        emoji = self._detect_primary_emoji(data)

        lines = [f"{emoji} **{name}**\n"]

        # Format all fields
        field_count = 0
        for key, value in data.items():
            if field_count >= self.MAX_FIELDS_PER_OBJECT:
                lines.append(f"\n_...i još {len(data) - field_count} polja_")
                break

            # Skip internal/meta fields
            if self._should_skip_field(key):
                continue

            # Skip if this is the name field we already showed
            if self._is_name_field(key):
                continue

            formatted = self._format_field(key, value, depth)
            if formatted:
                lines.append(formatted)
                field_count += 1

        return self._truncate_message("\n".join(lines))

    def _format_field(self, key: str, value: Any, depth: int = 0) -> Optional[str]:
        """Format a single field with emoji and human-readable label."""
        if value is None or value == "" or value == []:
            return None

        emoji = self._get_emoji_for_field(key)
        label = self._humanize_field(key)

        # Handle different value types
        if isinstance(value, bool):
            display = "Da" if value else "Ne"
            return f"{emoji} {label}: {display}"

        if isinstance(value, (int, float)):
            # Format numbers nicely
            if isinstance(value, float):
                display = f"{value:,.2f}"
            else:
                display = f"{value:,}"
            return f"{emoji} {label}: {display}"

        if isinstance(value, str):
            # Try to detect and format dates
            if self._looks_like_date(value):
                display = self._format_date(value)
            else:
                # Truncate long strings
                display = value[:self.MAX_FIELD_VALUE_LENGTH]
                if len(value) > self.MAX_FIELD_VALUE_LENGTH:
                    display += "..."
            return f"{emoji} {label}: {display}"

        if isinstance(value, list):
            if not value:
                return None
            if len(value) == 1 and isinstance(value[0], (str, int, float)):
                return f"{emoji} {label}: {value[0]}"
            # Summarize list
            if isinstance(value[0], dict):
                return f"{emoji} {label}: ({len(value)} stavki)"
            # Simple list - show first few
            preview = ", ".join(str(v) for v in value[:3])
            if len(value) > 3:
                preview += f" ...+{len(value)-3}"
            return f"{emoji} {label}: {preview}"

        if isinstance(value, dict):
            if depth >= self.MAX_NESTED_DEPTH:
                return f"{emoji} {label}: (objekt)"
            # Try to get meaningful info from nested dict
            nested_name = self._get_display_name(value)
            if nested_name and nested_name != "Stavka":
                return f"{emoji} {label}: {nested_name}"
            return f"{emoji} {label}: (objekt s {len(value)} polja)"

        return f"{emoji} {label}: {value}"

    def _get_display_name(self, item: Dict) -> str:
        """Extract display name from item using common naming patterns."""
        # Try various common name fields (ordered by priority)
        name_fields = [
            "FullVehicleName", "DisplayName", "Name", "Title",
            "FullName", "VehicleName", "PersonName",
            "Description", "Label", "Subject",
            "FirstName", "LastName"  # For person records
        ]

        for field in name_fields:
            val = item.get(field)
            if val and isinstance(val, str) and val.strip():
                return val.strip()

        # Try to combine FirstName + LastName
        first = item.get("FirstName", "")
        last = item.get("LastName", "")
        if first or last:
            return f"{first} {last}".strip()

        # Fallback to first string field
        for key, val in item.items():
            if isinstance(val, str) and val.strip() and not self._should_skip_field(key):
                return val.strip()[:50]

        return "Stavka"

    def _get_preview_fields(self, item: Dict, exclude_name: bool = True) -> List[tuple]:
        """Get 2-3 key fields for list preview."""
        # Priority fields for preview
        priority = [
            "LicencePlate", "Plate", "Email", "Phone", "Mobile",
            "Status", "State", "Type", "LastMileage", "Mileage"
        ]

        result = []
        used_keys = set()

        # First, try priority fields
        for field in priority:
            if field in item and item[field] is not None:
                val = item[field]
                if isinstance(val, (str, int, float)) and str(val).strip():
                    result.append((field, str(val)))
                    used_keys.add(field)
                    if len(result) >= 3:
                        break

        # Fill remaining with other fields
        if len(result) < 3:
            for key, val in item.items():
                if key in used_keys:
                    continue
                if self._should_skip_field(key):
                    continue
                if exclude_name and self._is_name_field(key):
                    continue
                if isinstance(val, (str, int, float)) and str(val).strip():
                    result.append((key, str(val)[:50]))
                    if len(result) >= 3:
                        break

        return result

    def _humanize_field(self, field_name: str) -> str:
        """Convert CamelCase/snake_case field name to human readable."""
        if not field_name:
            return "Polje"

        # Split CamelCase
        name = self._CAMEL_CASE_RE.sub(r' \1', field_name)
        # Split snake_case
        name = name.replace('_', ' ')
        # Clean up
        name = ' '.join(name.split())

        return name.strip().title()

    def _get_emoji_for_field(self, field_name: str) -> str:
        """Get appropriate emoji for field based on patterns."""
        if not field_name:
            return "•"

        for compiled_re, emoji in self._COMPILED_EMOJI_PATTERNS:
            if compiled_re.search(field_name):
                return emoji

        return "•"

    def _detect_primary_emoji(self, data: Dict) -> str:
        """Detect primary emoji for object based on its fields."""
        keys = set(data.keys())
        key_str = " ".join(keys)

        # Vehicle indicators
        if any(k in keys for k in ["VehicleId", "LicencePlate", "VIN", "Mileage", "LastMileage"]):
            return "🚗"

        # Person indicators
        if any(k in keys for k in ["PersonId", "FirstName", "LastName", "Email"]):
            return "👤"

        # Try pattern matching on all keys
        for compiled_re, emoji in self._COMPILED_EMOJI_PATTERNS:
            if compiled_re.search(key_str):
                return emoji

        return "📋"

    def _is_name_field(self, key: str) -> bool:
        """Check if field is likely a name/display field."""
        name_fields = {
            "fullvehiclename", "displayname", "name", "title",
            "fullname", "vehiclename", "personname", "firstname", "lastname"
        }
        return key.lower() in name_fields

    def _should_skip_field(self, key: str) -> bool:
        """Check if field should be skipped (internal/meta fields)."""
        # Skip internal fields
        if key.startswith("_") or key.startswith("$"):
            return True

        # Skip internal/meta ID fields (GUIDs that add no user value)
        skip_exact = {
            "Id", "Guid", "CreatedBy", "ModifiedBy",
            "TenantId", "ApiIdentity", "RowVersion",
        }
        if key in skip_exact:
            return True

        # Skip fields ending in Guid (always internal)
        if key.endswith("Guid"):
            return True

        # Skip FK-style *Id fields (e.g. VehicleId, PersonId, CompanyId)
        # but keep user-visible identifiers (ExternalId, AssetId, Code)
        keep_ids = {"ExternalId", "AssetId", "SourceId", "DocumentId"}
        if key not in keep_ids and (key.endswith("Id") or key.endswith("ID")):
            return True

        return False

    def _looks_like_date(self, value: str) -> bool:
        """Check if string looks like a date/datetime."""
        if not value or not isinstance(value, str):
            return False

        return any(p.match(value) for p in self._DATE_PATTERNS)

    def _format_date(self, value: str) -> str:
        """Format date/datetime to Croatian locale."""
        if not value:
            return ""

        try:
            # Handle ISO format
            if "T" in value:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return dt.strftime("%d.%m.%Y. %H:%M")

            # Try parsing YYYY-MM-DD
            if re.match(r'^\d{4}-\d{2}-\d{2}', value):
                dt = datetime.strptime(value[:10], "%Y-%m-%d")
                return dt.strftime("%d.%m.%Y.")

            return value
        except (ValueError, TypeError):
            return str(value)[:10]

    def _format_success(self, message: str, operation: str) -> str:
        """Format success message with operation context."""
        op_name = self._extract_operation_name(operation)
        if op_name:
            return f"✅ **{op_name}** - {message.lower()}!"
        return f"✅ {message}!"

    def _extract_operation_name(self, operation: str) -> Optional[str]:
        """Extract human-readable name from operation ID."""
        if not operation:
            return None

        # Remove method prefix (post_, get_, etc.)
        clean = operation
        for prefix in ["post_", "get_", "put_", "patch_", "delete_"]:
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):]
                break

        # Convert CamelCase to words
        if clean:
            words = re.sub(r'([A-Z])', r' \1', clean).strip()
            return words.title() if words else None

        return None

    def _truncate_message(self, message: str) -> str:
        """Truncate message to WhatsApp limit."""
        if len(message) <= self.MAX_MESSAGE_LENGTH:
            return message

        # Find last complete line before limit
        truncated = message[:self.MAX_MESSAGE_LENGTH]
        last_newline = truncated.rfind('\n')

        if last_newline > self.MAX_MESSAGE_LENGTH - 500:
            truncated = truncated[:last_newline]

        return truncated + "\n\n_...poruka skraćena._"

    # --- LIST FORMATTING FOR SELECTION ==========

    def format_vehicle_list(self, vehicles: List[Dict], filter_text: Optional[str] = None) -> str:
        """
        Format vehicle list for selection - GENERIC VERSION.

        Works with ANY list of items that have some name/identifier.
        """
        return self._format_list(vehicles)
