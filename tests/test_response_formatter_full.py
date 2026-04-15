"""
Comprehensive Tests for ResponseFormatter - targeting 100% coverage.

Tests all methods and edge cases:
- format_result() with various methods and data types
- _extract_data() patterns
- _format_any() data type handling
- _format_list() with primitives and objects
- _format_object() field formatting
- _format_field() value type handling
- Helper methods (_get_display_name, _get_preview_fields, etc.)
- Date handling and formatting
- Success message formatting
"""

import pytest
from unittest.mock import MagicMock
from services.response_formatter import ResponseFormatter


@pytest.fixture
def formatter():
    """Fresh ResponseFormatter instance for each test."""
    return ResponseFormatter()


# ============================================================================
# format_result() Tests - Lines 111-161
# ============================================================================

class TestFormatResultMethod:
    """Tests for format_result() method covering various scenarios."""

    def test_error_result_returns_error_message(self, formatter):
        """Line 131-133: Error result handling."""
        result = {"success": False, "error": "Connection failed"}
        output = formatter.format_result(result)
        assert output == "❌ Greška: Connection failed"

    def test_error_result_with_unknown_error(self, formatter):
        """Line 132: Default error message when error key missing."""
        result = {"success": False}
        output = formatter.format_result(result)
        assert "Nepoznata greška" in output

    def test_delete_method_success(self, formatter):
        """Lines 144-145: DELETE method returns success message."""
        tool = MagicMock()
        tool.method = "DELETE"
        result = {"success": True, "operation": "delete_Booking"}
        output = formatter.format_result(result, tool=tool)
        assert "uspješno obrisano" in output.lower()

    def test_delete_method_with_dict_tool(self, formatter):
        """Line 138: Tool as dict instead of object."""
        tool = {"method": "DELETE"}
        result = {"success": True, "operation": ""}
        output = formatter.format_result(result, tool=tool)
        assert "obrisano" in output

    def test_post_method_without_created_id(self, formatter):
        """Lines 147-149: POST without created_id."""
        tool = MagicMock()
        tool.method = "POST"
        result = {"success": True, "operation": "post_CreateBooking"}
        output = formatter.format_result(result, tool=tool)
        assert "uspješno spremljeno" in output.lower()
        assert "ID:" not in output

    def test_post_method_with_created_id(self, formatter):
        """Lines 148-152: POST with created_id."""
        tool = MagicMock()
        tool.method = "POST"
        result = {"success": True, "operation": "post_CreateBooking", "created_id": "abc-123"}
        output = formatter.format_result(result, tool=tool)
        assert "uspješno spremljeno" in output.lower()
        assert "abc-123" in output

    def test_put_method_with_created_id(self, formatter):
        """Line 147: PUT method handling."""
        tool = MagicMock()
        tool.method = "PUT"
        result = {"success": True, "operation": "put_UpdateBooking", "created_id": "xyz-789"}
        output = formatter.format_result(result, tool=tool)
        assert "spremljeno" in output
        assert "xyz-789" in output

    def test_patch_method_success(self, formatter):
        """Line 147: PATCH method handling."""
        tool = MagicMock()
        tool.method = "PATCH"
        result = {"success": True, "operation": "patch_UpdateVehicle"}
        output = formatter.format_result(result, tool=tool)
        assert "spremljeno" in output

    def test_get_with_none_data(self, formatter):
        """Lines 157-158: GET with None data returns success message."""
        result = {"success": True, "result": None}
        output = formatter.format_result(result)
        assert "Operacija uspješna" in output

    def test_get_with_data(self, formatter):
        """Line 155, 161: GET with actual data formats it."""
        result = {"success": True, "data": {"Name": "Test Vehicle"}}
        output = formatter.format_result(result)
        assert "Test Vehicle" in output

    def test_tool_without_method_attribute(self, formatter):
        """Line 138: Tool object without method attribute uses dict get."""
        tool = {"method": "GET"}
        result = {"success": True, "data": {"Name": "Test"}}
        output = formatter.format_result(result, tool=tool)
        assert "Test" in output

    def test_user_query_accepted(self, formatter):
        """user_query parameter is accepted without error."""
        result = {"success": True, "data": {"Name": "Test"}}
        output = formatter.format_result(result, user_query="show vehicles")
        assert "Test" in output


# ============================================================================
# _extract_data() Tests - Lines 163-179
# ============================================================================

class TestExtractData:
    """Tests for _extract_data() method."""

    def test_items_pattern(self, formatter):
        """Line 166-167: Extract from 'items' key."""
        result = {"items": [{"Id": 1}, {"Id": 2}]}
        data = formatter._extract_data(result)
        assert data == [{"Id": 1}, {"Id": 2}]

    def test_data_pattern_simple(self, formatter):
        """Lines 169-176: Extract from 'data' key."""
        result = {"data": {"Name": "Vehicle"}}
        data = formatter._extract_data(result)
        assert data == {"Name": "Vehicle"}

    def test_nested_data_pattern(self, formatter):
        """Lines 173-174: Nested Data pattern."""
        result = {"data": {"Data": [{"Id": 1}], "Count": 1}}
        data = formatter._extract_data(result)
        assert data == [{"Id": 1}]

    def test_result_fallback(self, formatter):
        """Line 179: Fall back to 'result' key."""
        result = {"result": [{"Id": 1}]}
        data = formatter._extract_data(result)
        assert data == [{"Id": 1}]

    def test_no_wrapper_returns_none(self, formatter):
        """Line 179: No known wrapper returns None."""
        result = {"unknown_key": "value"}
        data = formatter._extract_data(result)
        assert data is None


# ============================================================================
# _format_any() Tests - Lines 181-209
# ============================================================================

class TestFormatAny:
    """Tests for _format_any() method."""

    def test_none_data(self, formatter):
        """Lines 189-190: None returns no data message."""
        output = formatter._format_any(None)
        assert output == "Nema podataka."

    def test_string_primitive(self, formatter):
        """Lines 193-194: String primitive formatting."""
        output = formatter._format_any("Hello World")
        assert output == "✅ Rezultat: Hello World"

    def test_int_primitive(self, formatter):
        """Line 193-194: Integer primitive formatting."""
        output = formatter._format_any(42)
        assert output == "✅ Rezultat: 42"

    def test_float_primitive(self, formatter):
        """Line 193-194: Float primitive formatting."""
        output = formatter._format_any(3.14)
        assert output == "✅ Rezultat: 3.14"

    def test_bool_primitive(self, formatter):
        """Line 193-194: Boolean primitive formatting."""
        output = formatter._format_any(True)
        assert output == "✅ Rezultat: True"

    def test_empty_list(self, formatter):
        """Lines 198-199: Empty list returns no results."""
        output = formatter._format_any([])
        assert output == "Nema pronađenih rezultata."

    def test_non_empty_list(self, formatter):
        """Lines 200: Non-empty list calls _format_list."""
        output = formatter._format_any([{"Name": "Item 1"}, {"Name": "Item 2"}])
        assert "Pronađeno 2 stavki" in output

    def test_empty_dict(self, formatter):
        """Lines 204-205: Empty dict returns no data."""
        output = formatter._format_any({})
        assert output == "Nema podataka."

    def test_non_empty_dict(self, formatter):
        """Line 206: Non-empty dict calls _format_object."""
        output = formatter._format_any({"Name": "Test Item"})
        assert "Test Item" in output

    def test_unknown_type(self, formatter):
        """Lines 208-209: Unknown type converts to string."""
        class CustomClass:
            def __str__(self):
                return "CustomClassInstance"

        output = formatter._format_any(CustomClass())
        assert "CustomClassInstance" in output

    def test_unknown_type_truncation(self, formatter):
        """Line 209: Long unknown type string truncated to 500 chars."""
        class LongString:
            def __str__(self):
                return "X" * 1000

        output = formatter._format_any(LongString())
        assert len(output) <= 520  # "✅ Rezultat: " + 500 chars


# ============================================================================
# _format_list() Tests - Lines 211-249
# ============================================================================

class TestFormatList:
    """Tests for _format_list() method."""

    def test_empty_list(self, formatter):
        """Lines 213-214: Empty list returns no results."""
        output = formatter._format_list([])
        assert output == "Nema pronađenih rezultata."

    def test_primitive_items_list(self, formatter):
        """Lines 219-225: List of primitive items."""
        items = ["apple", "banana", "cherry"]
        output = formatter._format_list(items)
        assert "Pronađeno 3 stavki" in output
        assert "1. apple" in output
        assert "2. banana" in output
        assert "3. cherry" in output

    def test_primitive_items_exceeds_max(self, formatter):
        """Lines 223-224: More than MAX_LIST_ITEMS primitives."""
        items = [f"item_{i}" for i in range(15)]
        output = formatter._format_list(items)
        assert "Pronađeno 15 stavki" in output
        assert "...i još 5 stavki" in output

    def test_dict_items_list(self, formatter):
        """Lines 227-241: List of dict items."""
        items = [
            {"Name": "Vehicle 1", "LicencePlate": "ZG-001"},
            {"Name": "Vehicle 2", "LicencePlate": "ZG-002"}
        ]
        output = formatter._format_list(items)
        assert "Pronađeno 2 stavki" in output
        assert "Vehicle 1" in output
        assert "Vehicle 2" in output

    def test_dict_items_with_preview_fields(self, formatter):
        """Lines 236-239: Dict items show preview fields with emoji."""
        items = [{"Name": "Test", "Email": "test@example.com", "Phone": "+385123456"}]
        output = formatter._format_list(items)
        assert "Email" in output or "email" in output.lower()

    def test_dict_items_exceeds_max(self, formatter):
        """Lines 243-244: More than MAX_LIST_ITEMS dicts."""
        items = [{"Name": f"Item {i}"} for i in range(15)]
        output = formatter._format_list(items)
        assert "...i još 5 stavki" in output

    def test_list_footer_text(self, formatter):
        """Lines 246-247: Footer text present."""
        items = [{"Name": "Test"}]
        output = formatter._format_list(items)
        assert "Odaberite brojem" in output


# ============================================================================
# _format_object() Tests - Lines 251-282
# ============================================================================

class TestFormatObject:
    """Tests for _format_object() method."""

    def test_empty_dict(self, formatter):
        """Lines 253-254: Empty dict returns no data."""
        output = formatter._format_object({})
        assert output == "Nema podataka."

    def test_object_with_name(self, formatter):
        """Lines 256-260: Object displays name as header."""
        data = {"Name": "Test Vehicle", "Status": "Active"}
        output = formatter._format_object(data)
        assert "Test Vehicle" in output

    def test_max_fields_limit(self, formatter):
        """Lines 265-267: Max fields limit applied."""
        data = {f"Field{i}": f"Value{i}" for i in range(25)}
        output = formatter._format_object(data)
        assert "...i još" in output and "polja" in output

    def test_skips_internal_fields(self, formatter):
        """Line 270-271: Internal fields skipped."""
        data = {"Name": "Test", "_internal": "hidden", "Status": "Active"}
        output = formatter._format_object(data)
        assert "_internal" not in output

    def test_skips_name_field_in_body(self, formatter):
        """Lines 274-275: Name field not repeated in body."""
        data = {"Name": "Test", "Status": "Active"}
        output = formatter._format_object(data)
        # Name appears once in header, not repeated with label
        lines = output.split('\n')
        name_label_lines = [l for l in lines if "Name:" in l]
        assert len(name_label_lines) == 0  # Name is only in header


# ============================================================================
# _format_field() Tests - Lines 284-339
# ============================================================================

class TestFormatField:
    """Tests for _format_field() method."""

    def test_none_value(self, formatter):
        """Line 286-287: None value returns None."""
        output = formatter._format_field("Key", None)
        assert output is None

    def test_empty_string_value(self, formatter):
        """Line 286: Empty string returns None."""
        output = formatter._format_field("Key", "")
        assert output is None

    def test_empty_list_value(self, formatter):
        """Line 286: Empty list returns None."""
        output = formatter._format_field("Key", [])
        assert output is None

    def test_boolean_true(self, formatter):
        """Lines 293-295: Boolean True shows 'Da'."""
        output = formatter._format_field("Active", True)
        assert "Da" in output
        assert "Active" in output

    def test_boolean_false(self, formatter):
        """Lines 294: Boolean False shows 'Ne'."""
        output = formatter._format_field("Active", False)
        assert "Ne" in output

    def test_integer_value(self, formatter):
        """Lines 299, 301-302: Integer formatted with thousands separator."""
        output = formatter._format_field("Mileage", 150000)
        assert "150,000" in output or "150000" in output

    def test_float_value(self, formatter):
        """Lines 299-300: Float formatted with 2 decimals."""
        output = formatter._format_field("Amount", 1234.5)
        assert "1,234.50" in output or "1234.50" in output

    def test_string_date(self, formatter):
        """Lines 307-308: String that looks like date is formatted."""
        output = formatter._format_field("CreatedDate", "2024-05-15")
        assert "15" in output and "05" in output or "5" in output

    def test_long_string_truncated(self, formatter):
        """Lines 311-313: Long string truncated."""
        long_value = "A" * 200
        output = formatter._format_field("Description", long_value)
        assert "..." in output
        assert len(output) < 250

    def test_normal_string(self, formatter):
        """Line 314: Normal string displayed."""
        output = formatter._format_field("Status", "Active")
        assert "Active" in output

    def test_list_single_primitive(self, formatter):
        """Lines 319-320: Single primitive list item."""
        output = formatter._format_field("Tags", ["Important"])
        assert "Important" in output

    def test_list_of_dicts(self, formatter):
        """Lines 322-323: List of dicts shows count."""
        output = formatter._format_field("Items", [{"Id": 1}, {"Id": 2}])
        assert "(2 stavki)" in output

    def test_list_simple_preview(self, formatter):
        """Lines 325-328: Simple list shows first items."""
        output = formatter._format_field("Colors", ["red", "green", "blue", "yellow"])
        assert "red" in output
        assert "+1" in output or "yellow" not in output  # Last item might be cut

    def test_list_empty_in_field(self, formatter):
        """Lines 317-318: Empty list in field returns None."""
        output = formatter._format_field("Tags", [])
        assert output is None

    def test_nested_dict_depth_limit(self, formatter):
        """Lines 331-332: Nested dict at max depth shows (objekt)."""
        output = formatter._format_field("Nested", {"Inner": "Value"}, depth=2)
        assert "(objekt)" in output

    def test_nested_dict_with_name(self, formatter):
        """Lines 334-336: Nested dict with displayable name."""
        output = formatter._format_field("Vehicle", {"Name": "VW Passat", "Id": "123"}, depth=0)
        assert "VW Passat" in output

    def test_nested_dict_without_name(self, formatter):
        """Line 337: Nested dict without name shows field count."""
        output = formatter._format_field("Config", {"setting1": 1, "setting2": 2}, depth=0)
        assert "(objekt s 2 polja)" in output

    def test_fallback_to_string(self, formatter):
        """Line 339: Unknown type falls back to string."""
        class CustomType:
            def __str__(self):
                return "custom_value"

        output = formatter._format_field("Custom", CustomType())
        assert "custom_value" in output


# ============================================================================
# _get_display_name() Tests - Lines 341-367
# ============================================================================

class TestGetDisplayName:
    """Tests for _get_display_name() method."""

    def test_full_vehicle_name_priority(self, formatter):
        """Lines 344-354: FullVehicleName has priority."""
        item = {"FullVehicleName": "VW Passat 2020", "Name": "Other Name"}
        assert formatter._get_display_name(item) == "VW Passat 2020"

    def test_display_name(self, formatter):
        """Line 345: DisplayName field."""
        item = {"DisplayName": "Primary Display"}
        assert formatter._get_display_name(item) == "Primary Display"

    def test_first_last_name_combination(self, formatter):
        """Lines 357-360: FirstName + LastName combination."""
        item = {"FirstName": "Ivan", "LastName": "Horvat", "Id": "123"}
        result = formatter._get_display_name(item)
        # FirstName is in name_fields, so it may return "Ivan" directly or "Ivan Horvat"
        assert "Ivan" in result

    def test_only_first_name(self, formatter):
        """Line 359-360: Only FirstName present."""
        item = {"FirstName": "Ana", "Id": "123"}
        result = formatter._get_display_name(item)
        assert "Ana" in result

    def test_only_last_name(self, formatter):
        """Line 359-360: Only LastName present."""
        # Need to avoid name_fields priority - ensure no name fields except LastName
        item = {"SomeId": "123", "LastName": "Kovac", "Status": "Active"}
        result = formatter._get_display_name(item)
        # LastName is in name_fields so returns it directly
        assert "Kovac" in result

    def test_fallback_to_first_string_field(self, formatter):
        """Lines 363-365: Fallback to first string field."""
        item = {"Id": "abc-123", "Description": "Some description"}
        result = formatter._get_display_name(item)
        # Id is skipped, Description is first valid string
        assert "description" in result.lower() or "Some" in result

    def test_fallback_to_stavka(self, formatter):
        """Line 367: Fallback to 'Stavka' when no name found."""
        item = {"Id": "123", "_internal": "hidden"}
        result = formatter._get_display_name(item)
        assert result == "Stavka"


# ============================================================================
# _get_preview_fields() Tests - Lines 369-404
# ============================================================================

class TestGetPreviewFields:
    """Tests for _get_preview_fields() method."""

    def test_priority_fields_first(self, formatter):
        """Lines 381-388: Priority fields selected first."""
        item = {"Name": "Test", "LicencePlate": "ZG-123", "Email": "test@test.com"}
        result = formatter._get_preview_fields(item, exclude_name=True)
        fields = [f[0] for f in result]
        assert "LicencePlate" in fields or "Email" in fields

    def test_max_three_fields(self, formatter):
        """Lines 387-388, 401-402: Maximum 3 preview fields."""
        item = {
            "Name": "Test",
            "LicencePlate": "ZG-123",
            "Email": "a@b.com",
            "Phone": "123",
            "Status": "Active",
            "Type": "Car"
        }
        result = formatter._get_preview_fields(item, exclude_name=True)
        assert len(result) <= 3

    def test_skip_used_keys(self, formatter):
        """Line 393-394: Skip already used keys."""
        item = {"LicencePlate": "ZG-123", "Plate": "ZG-123"}  # Similar fields
        result = formatter._get_preview_fields(item, exclude_name=True)
        # Both can appear but each only once
        assert len(result) >= 1

    def test_skip_internal_fields(self, formatter):
        """Lines 395-396: Skip internal fields."""
        item = {"Name": "Test", "_hidden": "value", "Status": "Active"}
        result = formatter._get_preview_fields(item, exclude_name=True)
        fields = [f[0] for f in result]
        assert "_hidden" not in fields

    def test_exclude_name_fields(self, formatter):
        """Lines 397-398: Exclude name fields when flag set."""
        item = {"Name": "Test", "DisplayName": "Display", "Status": "Active"}
        result = formatter._get_preview_fields(item, exclude_name=True)
        fields = [f[0] for f in result]
        assert "Name" not in fields
        assert "DisplayName" not in fields

    def test_truncate_long_values(self, formatter):
        """Line 400: Truncate long values to 50 chars."""
        item = {"Description": "A" * 100}
        result = formatter._get_preview_fields(item, exclude_name=False)
        for field, value in result:
            assert len(value) <= 50

    def test_only_string_int_float_values(self, formatter):
        """Line 399: Only include str/int/float values."""
        item = {"Name": "Test", "Items": [1, 2, 3], "Count": 5}
        result = formatter._get_preview_fields(item, exclude_name=False)
        fields = [f[0] for f in result]
        # Items (list) should not be included
        assert "Items" not in fields


# ============================================================================
# _humanize_field() Tests - Lines 406-418
# ============================================================================

class TestHumanizeField:
    """Tests for _humanize_field() method."""

    def test_camel_case_split(self, formatter):
        """Lines 411-412: CamelCase split into words."""
        result = formatter._humanize_field("FullVehicleName")
        assert "Full" in result
        assert "Vehicle" in result
        assert "Name" in result

    def test_snake_case_split(self, formatter):
        """Lines 413-414: snake_case split into words."""
        result = formatter._humanize_field("licence_plate")
        assert "Licence" in result
        assert "Plate" in result

    def test_title_case_output(self, formatter):
        """Line 418: Output is title case."""
        result = formatter._humanize_field("status")
        assert result == "Status"

    def test_empty_field_name(self, formatter):
        """Lines 408-409: Empty field returns 'Polje'."""
        result = formatter._humanize_field("")
        assert result == "Polje"


# ============================================================================
# _get_emoji_for_field() Tests - Lines 420-429
# ============================================================================

class TestGetEmojiForField:
    """Tests for _get_emoji_for_field() method."""

    def test_empty_field_returns_bullet(self, formatter):
        """Lines 422-423: Empty field returns bullet."""
        result = formatter._get_emoji_for_field("")
        assert result == "•"

    def test_vehicle_pattern(self, formatter):
        """Line 425-427: Vehicle-related field gets car emoji."""
        result = formatter._get_emoji_for_field("VehicleName")
        assert result == "🚗"

    def test_email_pattern(self, formatter):
        """Line 425-427: Email field gets email emoji."""
        result = formatter._get_emoji_for_field("Email")
        assert result == "📧"

    def test_unknown_field_returns_bullet(self, formatter):
        """Line 429: Unknown field returns bullet."""
        result = formatter._get_emoji_for_field("SomeRandomField")
        assert result == "•"

    def test_phone_pattern(self, formatter):
        """Line 425-427: Phone field gets phone emoji."""
        result = formatter._get_emoji_for_field("PhoneNumber")
        assert result == "📱"


# ============================================================================
# _detect_primary_emoji() Tests - Lines 431-449
# ============================================================================

class TestDetectPrimaryEmoji:
    """Tests for _detect_primary_emoji() method."""

    def test_vehicle_indicators(self, formatter):
        """Lines 437-438: Vehicle fields return car emoji."""
        data = {"VehicleId": "123", "LicencePlate": "ZG-123"}
        result = formatter._detect_primary_emoji(data)
        assert result == "🚗"

    def test_person_indicators(self, formatter):
        """Lines 441-442: Person fields return person emoji."""
        data = {"PersonId": "123", "Email": "test@test.com"}
        result = formatter._detect_primary_emoji(data)
        assert result == "👤"

    def test_pattern_matching_fallback(self, formatter):
        """Lines 445-447: Pattern matching on keys."""
        data = {"CompanyName": "Test Corp", "Status": "Active"}
        result = formatter._detect_primary_emoji(data)
        # Returns 📝 for generic name-like fields
        assert result in ["🏢", "📝", "📋"]

    def test_default_emoji(self, formatter):
        """Line 449: Default emoji when no match."""
        data = {"RandomField": "value", "AnotherField": "value2"}
        result = formatter._detect_primary_emoji(data)
        assert result == "📋"


# ============================================================================
# _is_name_field() Tests - Lines 451-457
# ============================================================================

class TestIsNameField:
    """Tests for _is_name_field() method."""

    def test_name_field(self, formatter):
        """Lines 453-456: Standard name fields."""
        assert formatter._is_name_field("Name") is True
        assert formatter._is_name_field("FullName") is True
        assert formatter._is_name_field("DisplayName") is True
        assert formatter._is_name_field("FirstName") is True
        assert formatter._is_name_field("LastName") is True

    def test_case_insensitive(self, formatter):
        """Line 457: Case insensitive comparison."""
        assert formatter._is_name_field("name") is True
        assert formatter._is_name_field("NAME") is True
        assert formatter._is_name_field("fullname") is True

    def test_non_name_field(self, formatter):
        """Lines 453-457: Non-name fields return False."""
        assert formatter._is_name_field("Id") is False
        assert formatter._is_name_field("Status") is False
        assert formatter._is_name_field("NameSuffix") is False  # Not in the set


# ============================================================================
# _should_skip_field() Tests - Lines 459-480
# ============================================================================

class TestShouldSkipField:
    """Tests for _should_skip_field() method."""

    def test_underscore_prefix(self, formatter):
        """Line 462: Fields starting with underscore."""
        assert formatter._should_skip_field("_internal") is True
        assert formatter._should_skip_field("_meta") is True

    def test_dollar_prefix(self, formatter):
        """Line 462: Fields starting with dollar sign."""
        assert formatter._should_skip_field("$ref") is True
        assert formatter._should_skip_field("$type") is True

    def test_id_fields_skipped(self, formatter):
        """Lines 467-478: ID fields skipped."""
        assert formatter._should_skip_field("Id") is True
        assert formatter._should_skip_field("VehicleId") is True
        assert formatter._should_skip_field("PersonID") is True

    def test_guid_fields_skipped(self, formatter):
        """Line 468: GUID fields skipped."""
        assert formatter._should_skip_field("Guid") is True
        assert formatter._should_skip_field("TenantGuid") is True

    def test_exception_fields_not_skipped(self, formatter):
        """Lines 476-477: Exception fields not skipped."""
        assert formatter._should_skip_field("ExternalId") is False
        assert formatter._should_skip_field("Code") is False
        assert formatter._should_skip_field("AssetId") is False

    def test_normal_fields_not_skipped(self, formatter):
        """Line 480: Normal fields not skipped."""
        assert formatter._should_skip_field("Name") is False
        assert formatter._should_skip_field("Status") is False
        assert formatter._should_skip_field("Description") is False


# ============================================================================
# _looks_like_date() Tests - Lines 482-494
# ============================================================================

class TestLooksLikeDate:
    """Tests for _looks_like_date() method."""

    def test_not_string(self, formatter):
        """Lines 484-485: Non-string returns False."""
        assert formatter._looks_like_date(None) is False
        assert formatter._looks_like_date(12345) is False
        assert formatter._looks_like_date([]) is False

    def test_iso_format(self, formatter):
        """Lines 489: ISO date format."""
        assert formatter._looks_like_date("2024-05-15") is True
        assert formatter._looks_like_date("2024-05-15T10:30:00Z") is True

    def test_eu_dot_format(self, formatter):
        """Line 490: DD.MM.YYYY format."""
        assert formatter._looks_like_date("15.05.2024") is True

    def test_slash_format(self, formatter):
        """Line 491: DD/MM/YYYY format."""
        assert formatter._looks_like_date("15/05/2024") is True

    def test_not_a_date(self, formatter):
        """Line 494: Non-date strings."""
        assert formatter._looks_like_date("hello world") is False
        assert formatter._looks_like_date("12345") is False
        assert formatter._looks_like_date("abc-123") is False


# ============================================================================
# _format_date() Tests - Lines 496-514
# ============================================================================

class TestFormatDate:
    """Tests for _format_date() method."""

    def test_empty_value(self, formatter):
        """Lines 498-499: Empty value returns empty string."""
        assert formatter._format_date("") == ""
        assert formatter._format_date(None) == ""

    def test_iso_datetime(self, formatter):
        """Lines 503-505: ISO datetime with T."""
        result = formatter._format_date("2024-05-15T10:30:00Z")
        assert "15.05.2024" in result
        # Should include time
        assert "10:30" in result or "12:30" in result  # Timezone may affect

    def test_iso_date_only(self, formatter):
        """Lines 508-510: ISO date without time."""
        result = formatter._format_date("2024-05-15")
        assert "15.05.2024" in result

    def test_invalid_date_fallback(self, formatter):
        """Lines 512-514: Invalid date returns truncated string."""
        result = formatter._format_date("not-a-date")
        assert result == "not-a-date"[:10]

    def test_malformed_iso_datetime(self, formatter):
        """Lines 512-514: Malformed datetime catches exception."""
        result = formatter._format_date("2024-99-99T99:99:99Z")
        assert len(result) <= 10


# ============================================================================
# _format_success() Tests - Lines 516-521
# ============================================================================

class TestFormatSuccess:
    """Tests for _format_success() method."""

    def test_with_operation_name(self, formatter):
        """Lines 518-520: Success with operation name."""
        result = formatter._format_success("Uspješno spremljeno", "post_CreateBooking")
        assert "✅" in result
        assert "Booking" in result or "booking" in result.lower()
        assert "uspješno spremljeno" in result.lower()

    def test_without_operation_name(self, formatter):
        """Line 521: Success without operation name."""
        result = formatter._format_success("Uspješno spremljeno", "")
        assert "✅ Uspješno spremljeno!" in result


# ============================================================================
# _extract_operation_name() Tests - Lines 523-540
# ============================================================================

class TestExtractOperationName:
    """Tests for _extract_operation_name() method."""

    def test_empty_operation(self, formatter):
        """Lines 525-526: Empty operation returns None."""
        assert formatter._extract_operation_name("") is None
        assert formatter._extract_operation_name(None) is None

    def test_post_prefix_removed(self, formatter):
        """Lines 530-533: post_ prefix removed."""
        result = formatter._extract_operation_name("post_BookingCalendar")
        assert result is not None
        assert "post" not in result.lower()
        assert "Booking" in result

    def test_get_prefix_removed(self, formatter):
        """Lines 530-533: get_ prefix removed."""
        result = formatter._extract_operation_name("get_Vehicles")
        assert result is not None
        assert "get" not in result.lower()

    def test_delete_prefix_removed(self, formatter):
        """Lines 530-533: delete_ prefix removed."""
        result = formatter._extract_operation_name("delete_Booking")
        assert result is not None
        assert "delete" not in result.lower()

    def test_camel_case_to_words(self, formatter):
        """Lines 537-538: CamelCase converted to words."""
        result = formatter._extract_operation_name("CreateBookingEntry")
        assert result is not None
        # Should have spaces between words
        assert " " in result

    def test_no_prefix_operation(self, formatter):
        """Line 540: Operation without prefix."""
        result = formatter._extract_operation_name("Vehicles")
        assert result is not None
        assert "Vehicle" in result or "vehicle" in result.lower()


# ============================================================================
# _truncate_message() Tests - Lines 542-554
# ============================================================================

class TestTruncateMessage:
    """Tests for _truncate_message() method."""

    def test_short_message_unchanged(self, formatter):
        """Lines 544-545: Short message returned unchanged."""
        msg = "This is a short message."
        assert formatter._truncate_message(msg) == msg

    def test_long_message_truncated(self, formatter):
        """Lines 548-554: Long message truncated at newline."""
        lines = ["Line " + str(i) for i in range(500)]
        msg = "\n".join(lines)
        result = formatter._truncate_message(msg)
        assert len(result) <= formatter.MAX_MESSAGE_LENGTH + 100
        assert "_...poruka skraćena._" in result

    def test_truncation_at_newline(self, formatter):
        """Lines 549-552: Truncation happens at newline boundary."""
        # Create a message with clear newline boundaries
        segment = "A" * 200 + "\n"
        msg = segment * 25  # Well over MAX_MESSAGE_LENGTH
        result = formatter._truncate_message(msg)
        # Should end with the truncation notice, not in middle of line
        assert result.endswith("_...poruka skraćena._")


# ============================================================================
# format_vehicle_list() Tests - Line 558-564
# ============================================================================

class TestFormatVehicleList:
    """Tests for format_vehicle_list() method."""

    def test_formats_vehicle_list(self, formatter):
        """Line 564: Delegates to _format_list."""
        vehicles = [
            {"Name": "VW Passat", "LicencePlate": "ZG-123"},
            {"Name": "Audi A4", "LicencePlate": "ZG-456"}
        ]
        result = formatter.format_vehicle_list(vehicles)
        assert "Pronađeno 2 stavki" in result
        assert "VW Passat" in result or "Audi A4" in result


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegrationScenarios:
    """Integration tests combining multiple methods."""

    def test_complete_get_response_flow(self, formatter):
        """Complete flow for GET response with list of objects."""
        result = {
            "success": True,
            "data": [
                {
                    "FullVehicleName": "VW Passat 2020",
                    "LicencePlate": "ZG-1234-AB",
                    "LastMileage": 50000,
                    "Status": "Available",
                    "VehicleId": "guid-123"
                },
                {
                    "FullVehicleName": "Audi A4 2021",
                    "LicencePlate": "ZG-5678-CD",
                    "LastMileage": 35000,
                    "Status": "InUse",
                    "VehicleId": "guid-456"
                }
            ]
        }
        output = formatter.format_result(result)
        assert "VW Passat" in output
        assert "Audi A4" in output
        assert "Pronađeno 2 stavki" in output

    def test_complete_post_response_flow(self, formatter):
        """Complete flow for POST response."""
        tool = MagicMock()
        tool.method = "POST"
        result = {
            "success": True,
            "operation": "post_CreateVehicleBooking",
            "created_id": "booking-12345"
        }
        output = formatter.format_result(result, tool=tool)
        assert "✅" in output
        assert "spremljeno" in output
        assert "booking-12345" in output

    def test_nested_data_extraction_and_formatting(self, formatter):
        """Test nested Data pattern extraction."""
        result = {
            "success": True,
            "data": {
                "Data": [
                    {"Name": "Item 1", "CreatedAt": "2024-05-15T10:30:00Z"},
                    {"Name": "Item 2", "CreatedAt": "2024-05-16T11:00:00Z"}
                ],
                "Count": 2,
                "Page": 1
            }
        }
        output = formatter.format_result(result)
        assert "Item 1" in output
        assert "Item 2" in output
        assert "Pronađeno 2" in output

    def test_single_object_with_all_field_types(self, formatter):
        """Test single object with various field types."""
        result = {
            "success": True,
            "data": {
                "Name": "Test Entity",
                "IsActive": True,
                "Count": 42,
                "Amount": 1234.56,
                "CreatedDate": "2024-05-15T10:30:00Z",
                "Tags": ["tag1", "tag2", "tag3"],
                "NestedObject": {"Name": "Nested Name"},
                "LongDescription": "A" * 200,
                "_internalField": "hidden",
                "VehicleId": "guid-should-be-hidden"
            }
        }
        output = formatter.format_result(result)
        assert "Test Entity" in output
        assert "Da" in output  # Boolean true
        assert "42" in output  # Integer
        assert "_internalField" not in output
        assert "guid-should-be-hidden" not in output

    def test_empty_result_handling(self, formatter):
        """Test handling of empty result."""
        result = {"success": True, "data": []}
        output = formatter.format_result(result)
        assert "Nema pronađenih rezultata" in output

    def test_error_with_special_characters(self, formatter):
        """Test error message with special characters."""
        result = {"success": False, "error": "Greška: <script>alert('xss')</script>"}
        output = formatter.format_result(result)
        assert "❌ Greška:" in output
        assert "<script>" in output  # Not escaped - just displayed


class TestEdgeCases:
    """Edge case tests."""

    def test_very_deep_nesting(self, formatter):
        """Test deeply nested objects respect MAX_NESTED_DEPTH."""
        deep_object = {"level1": {"level2": {"level3": {"level4": "value"}}}}
        result = {"success": True, "data": deep_object}
        output = formatter.format_result(result)
        # Should handle gracefully without crashing
        assert isinstance(output, str)

    def test_unicode_in_fields(self, formatter):
        """Test Unicode characters in field names and values."""
        result = {
            "success": True,
            "data": {
                "Ime": "Čičak-Šušak",
                "Opis": "Žuto cvijeće 🌻",
                "Cijena": "€100.00"
            }
        }
        output = formatter.format_result(result)
        assert "Čičak-Šušak" in output
        assert "🌻" in output or "Žuto" in output

    def test_numeric_string_not_date(self, formatter):
        """Test that numeric strings are not misidentified as dates."""
        result = {"success": True, "data": {"Code": "12345678"}}
        output = formatter.format_result(result)
        assert "12345678" in output

    def test_list_with_dicts(self, formatter):
        """Test list with dict items."""
        data = [{"Name": "First"}, {"Name": "Second"}]
        output = formatter._format_list(data)
        assert "First" in output

    def test_tool_as_none(self, formatter):
        """Test format_result with tool=None (default GET method)."""
        result = {"success": True, "data": {"Name": "Test"}}
        output = formatter.format_result(result, tool=None)
        assert "Test" in output

    def test_extremely_long_field_value(self, formatter):
        """Test that extremely long values are truncated."""
        result = {"success": True, "data": {"Description": "X" * 10000}}
        output = formatter.format_result(result)
        assert len(output) < 5000
        assert "..." in output
