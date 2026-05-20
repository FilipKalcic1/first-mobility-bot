"""
Tests for SwaggerParser - services/registry/swagger_parser.py
Comprehensive coverage of all public and private methods.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, mock_open

from services.registry.swagger_parser import SwaggerParser, CONFIG_PATH
from services.tool_contracts import (
    UnifiedToolDefinition,
    ParameterDefinition,
    DependencySource,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def parser_with_defaults():
    """Create parser with default patterns (no config file)."""
    with patch.object(SwaggerParser, "_load_context_param_schemas") as mock_load:
        parser = SwaggerParser()
        mock_load.assert_called_once()
    # Manually initialize defaults
    parser._init_default_patterns()
    return parser


@pytest.fixture
def minimal_spec():
    """Minimal valid OpenAPI 3 spec."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0"},
        "servers": [{"url": "https://api.example.com/svc"}],
        "paths": {
            "/api/v2/items": {
                "get": {
                    "operationId": "get_Items",
                    "summary": "List items",
                    "description": "Returns all items",
                    "tags": ["Items"],
                    "parameters": [
                        {
                            "name": "Status",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Filter by status",
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {
                                            "$ref": "#/components/schemas/Item"
                                        },
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "Id": {"type": "string", "format": "uuid"},
                        "Name": {"type": "string"},
                        "Status": {"type": "string"},
                    },
                }
            }
        },
    }


@pytest.fixture
def spec_with_post():
    """Spec with a POST endpoint and request body."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0"},
        "servers": [{"url": "https://api.example.com/svc"}],
        "paths": {
            "/api/v2/items": {
                "post": {
                    "operationId": "create_Item",
                    "summary": "Create item",
                    "description": "Creates a new item",
                    "tags": ["Items"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/CreateItemRequest"
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/Item"
                                    }
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "CreateItemRequest": {
                    "type": "object",
                    "required": ["Name", "PersonId"],
                    "properties": {
                        "Name": {
                            "type": "string",
                            "description": "Item name",
                        },
                        "PersonId": {
                            "type": "string",
                            "format": "uuid",
                            "description": "The person/user who owns this item",
                        },
                        "Notes": {
                            "type": "string",
                            "description": "Optional notes",
                        },
                    },
                },
                "Item": {
                    "type": "object",
                    "properties": {
                        "Id": {"type": "string"},
                        "Name": {"type": "string"},
                        "PersonId": {"type": "string"},
                    },
                },
            }
        },
    }


@pytest.fixture
def build_embedding_text_fn():
    """Simple embedding text builder for tests."""
    def _build(operation_id, service_name, path, method, description, parameters, output_keys):
        return f"{operation_id} {service_name} {method} {path} {description}"
    return _build


# =============================================================================
# 1. __init__ and configuration loading
# =============================================================================

class TestInit:
    """Tests for SwaggerParser.__init__ and config loading."""

    def test_init_loads_default_patterns_when_config_missing(self):
        """When CONFIG_PATH does not exist, defaults are initialized."""
        with patch("services.registry.swagger_parser.CONFIG_PATH") as mock_path:
            mock_path.exists.return_value = False
            parser = SwaggerParser()

        assert "person_id" in parser.context_param_patterns
        assert "vehicle_id" in parser.context_param_patterns
        assert "tenant_id" in parser.context_param_patterns
        assert "personid" in parser.context_param_fallback

    def test_init_loads_config_from_file(self, tmp_path):
        """Config file is loaded and patterns are parsed correctly."""
        config = {
            "context_types": {
                "custom_id": {
                    "schema_hints": {
                        "formats": ["uuid"],
                        "types": ["string"],
                    },
                    "classification_rules": {
                        "name_patterns": ["^custom"],
                        "description_keywords": ["custom", "special"],
                    },
                    "fallback_names": ["CustomId", "custom_id"],
                }
            }
        }
        config_file = tmp_path / "context_param_schemas.json"
        config_file.write_text(json.dumps(config))

        with patch("services.registry.swagger_parser.CONFIG_PATH", config_file):
            parser = SwaggerParser()

        assert "custom_id" in parser.context_param_patterns
        assert "uuid" in parser.context_param_patterns["custom_id"]["schema_formats"]
        assert "customid" in parser.context_param_fallback
        assert len(parser.compiled_name_patterns["custom_id"]) == 1

    def test_init_falls_back_on_corrupt_config(self, tmp_path):
        """Corrupt JSON config falls back to defaults."""
        config_file = tmp_path / "context_param_schemas.json"
        config_file.write_text("{invalid json!!")

        with patch("services.registry.swagger_parser.CONFIG_PATH", config_file):
            parser = SwaggerParser()

        # Should have defaults
        assert "person_id" in parser.context_param_patterns

    def test_default_patterns_content(self, parser_with_defaults):
        """Verify the content of default patterns."""
        p = parser_with_defaults
        assert "uuid" in p.context_param_patterns["person_id"]["schema_formats"]
        assert "person" in p.context_param_patterns["person_id"]["description_keywords"]
        assert p.context_param_fallback["driverid"] == "person_id"
        assert p.context_param_fallback["tenantid"] == "tenant_id"


# =============================================================================
# 2. fetch_spec
# =============================================================================

class TestFetchSpec:
    """Tests for async spec fetching."""

    async def test_fetch_spec_success(self, parser_with_defaults):
        """Successful fetch returns parsed JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"openapi": "3.0.0"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.registry.swagger_parser.httpx.AsyncClient", return_value=mock_client):
            result = await parser_with_defaults.fetch_spec("https://example.com/swagger.json")

        assert result == {"openapi": "3.0.0"}

    async def test_fetch_spec_http_error(self, parser_with_defaults):
        """Non-200 status returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.registry.swagger_parser.httpx.AsyncClient", return_value=mock_client):
            result = await parser_with_defaults.fetch_spec("https://example.com/swagger.json")

        assert result is None

    async def test_fetch_spec_network_error(self, parser_with_defaults):
        """Network exception returns None."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.registry.swagger_parser.httpx.AsyncClient", return_value=mock_client):
            result = await parser_with_defaults.fetch_spec("https://example.com/swagger.json")

        assert result is None


# =============================================================================
# 3. _extract_service_name
# =============================================================================

class TestExtractServiceName:
    """Tests for service name extraction from URL."""

    def test_extracts_name_before_swagger(self, parser_with_defaults):
        url = "https://api.example.com/automation/swagger/v1/swagger.json"
        # "swagger" first found at index 4, so parts[3] = "automation"
        assert parser_with_defaults._extract_service_name(url) == "automation"

    def test_extracts_name_typical_url(self, parser_with_defaults):
        url = "https://api.example.com/vehiclemgt/swagger/swagger.json"
        assert parser_with_defaults._extract_service_name(url) == "vehiclemgt"

    def test_unknown_when_no_swagger_in_url(self, parser_with_defaults):
        url = "https://api.example.com/api/v1/spec.json"
        assert parser_with_defaults._extract_service_name(url) == "unknown"

    def test_swagger_at_first_position(self, parser_with_defaults):
        """Edge case: swagger is the first segment (index 0 in parts)."""
        url = "swagger/something"
        # "swagger" is at index 0, i-1 would be -1 which wraps
        # Actually let's check: parts = ["swagger", "something"], i=0, i>0 is False
        # so it returns "unknown"
        assert parser_with_defaults._extract_service_name(url) == "unknown"


# =============================================================================
# 4. _extract_base_url
# =============================================================================

class TestExtractBaseUrl:
    """Tests for base URL extraction from spec."""

    def test_openapi3_servers(self, parser_with_defaults):
        spec = {"servers": [{"url": "https://api.example.com/svc/"}]}
        assert parser_with_defaults._extract_base_url(spec) == "https://api.example.com/svc"

    def test_skips_dev_servers(self, parser_with_defaults):
        spec = {
            "servers": [
                {"url": "https://dev.example.com/svc", "description": "Dev server"},
                {"url": "https://prod.example.com/svc", "description": "Production"},
            ]
        }
        result = parser_with_defaults._extract_base_url(spec)
        assert result == "https://prod.example.com/svc"

    def test_skips_test_and_staging_servers(self, parser_with_defaults):
        spec = {
            "servers": [
                {"url": "https://test.example.com/svc", "description": "Test environment"},
                {"url": "https://staging.example.com/svc", "description": "Staging env"},
                {"url": "https://api.example.com/svc", "description": "Live"},
            ]
        }
        assert parser_with_defaults._extract_base_url(spec) == "https://api.example.com/svc"

    def test_falls_back_to_first_server_if_all_dev(self, parser_with_defaults):
        spec = {
            "servers": [
                {"url": "/relative-only", "description": "Dev server"},
            ]
        }
        assert parser_with_defaults._extract_base_url(spec) == "/relative-only"

    def test_swagger2_basepath_with_host(self, parser_with_defaults):
        spec = {
            "host": "api.example.com",
            "basePath": "/v2",
            "schemes": ["https"],
        }
        assert parser_with_defaults._extract_base_url(spec) == "https://api.example.com/v2"

    def test_swagger2_basepath_without_host(self, parser_with_defaults):
        spec = {"basePath": "/v2"}
        # No host, basePath doesn't start with http -> returns basePath as-is
        assert parser_with_defaults._extract_base_url(spec) == "/v2"

    def test_swagger2_basepath_http_url(self, parser_with_defaults):
        spec = {"basePath": "https://api.example.com/v2/"}
        # basePath starts with http -> returned as-is with rstrip("/")
        assert parser_with_defaults._extract_base_url(spec) == "https://api.example.com/v2"

    def test_empty_spec(self, parser_with_defaults):
        assert parser_with_defaults._extract_base_url({}) == ""

    def test_empty_servers_list(self, parser_with_defaults):
        spec = {"servers": []}
        assert parser_with_defaults._extract_base_url(spec) == ""


# =============================================================================
# 5. _extract_swagger_name
# =============================================================================

class TestExtractSwaggerName:
    """Tests for swagger name extraction from service URL."""

    def test_extracts_first_path_segment(self, parser_with_defaults):
        assert parser_with_defaults._extract_swagger_name("https://api.example.com/automation") == "automation"

    def test_handles_trailing_slash(self, parser_with_defaults):
        assert parser_with_defaults._extract_swagger_name("https://api.example.com/vehiclemgt/") == "vehiclemgt"

    def test_empty_url(self, parser_with_defaults):
        assert parser_with_defaults._extract_swagger_name("") == ""

    def test_relative_url(self, parser_with_defaults):
        assert parser_with_defaults._extract_swagger_name("/myservice") == "myservice"

    def test_no_path(self, parser_with_defaults):
        assert parser_with_defaults._extract_swagger_name("https://api.example.com") == ""

    def test_leading_slash_relative(self, parser_with_defaults):
        # lstrip("/") removes leading slash, then not starting with http -> returns as-is
        assert parser_with_defaults._extract_swagger_name("/svc/api") == "svc/api"


# =============================================================================
# 6. _generate_operation_id
# =============================================================================

class TestGenerateOperationId:
    """Tests for operation ID generation."""

    def test_simple_path(self, parser_with_defaults):
        result = parser_with_defaults._generate_operation_id("/api/v2/items", "GET")
        assert result == "get_api_v2_items"

    def test_path_with_params(self, parser_with_defaults):
        result = parser_with_defaults._generate_operation_id("/api/v2/items/{id}", "DELETE")
        assert result == "delete_api_v2_items_id"

    def test_consecutive_special_chars(self, parser_with_defaults):
        result = parser_with_defaults._generate_operation_id("/api//v2--items", "POST")
        assert result == "post_api_v2_items"


# =============================================================================
# 7. _is_blacklisted
# =============================================================================

class TestIsBlacklisted:
    """Tests for blacklist checking."""

    def test_blacklisted_operation_id(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("exportReport", "/api/reports") is True

    def test_blacklisted_path(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("getStuff", "/api/batch/process") is True

    def test_not_blacklisted(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("get_Vehicles", "/api/v2/vehicles") is False

    def test_blacklisted_swagger(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("swaggerDoc", "/swagger/v1") is True

    def test_blacklisted_health(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("healthCheck", "/health") is True

    def test_blacklisted_odata(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("get_Items", "/odata/items") is True

    def test_blacklisted_internal(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("internalSync", "/api/sync") is True

    def test_blacklisted_count(self, parser_with_defaults):
        assert parser_with_defaults._is_blacklisted("get_ItemsCount", "/api/items") is True


# =============================================================================
# 8. _resolve_ref
# =============================================================================

class TestResolveRef:
    """Tests for $ref resolution."""

    def test_resolves_component_ref(self, parser_with_defaults):
        schema = {"$ref": "#/components/schemas/Item"}
        spec = {
            "components": {
                "schemas": {
                    "Item": {
                        "type": "object",
                        "properties": {"Id": {"type": "string"}},
                    }
                }
            }
        }
        resolved = parser_with_defaults._resolve_ref(schema, spec)
        assert resolved["type"] == "object"
        assert "Id" in resolved["properties"]

    def test_returns_schema_if_no_ref(self, parser_with_defaults):
        schema = {"type": "string"}
        result = parser_with_defaults._resolve_ref(schema, {})
        assert result == {"type": "string"}

    def test_returns_schema_for_non_dict(self, parser_with_defaults):
        result = parser_with_defaults._resolve_ref("not a dict", {})
        assert result == "not a dict"

    def test_returns_schema_for_external_ref(self, parser_with_defaults):
        schema = {"$ref": "external.json#/definitions/Foo"}
        result = parser_with_defaults._resolve_ref(schema, {})
        assert result == schema  # External refs are not resolved

    def test_missing_ref_returns_empty(self, parser_with_defaults):
        schema = {"$ref": "#/components/schemas/NonExistent"}
        spec = {"components": {"schemas": {}}}
        resolved = parser_with_defaults._resolve_ref(schema, spec)
        assert resolved == {}

    def test_deeply_nested_ref(self, parser_with_defaults):
        schema = {"$ref": "#/a/b/c/d"}
        spec = {"a": {"b": {"c": {"d": {"type": "integer"}}}}}
        resolved = parser_with_defaults._resolve_ref(schema, spec)
        assert resolved == {"type": "integer"}


# =============================================================================
# 9. _classify_context_parameter
# =============================================================================

class TestClassifyContextParameter:
    """Tests for context parameter classification."""

    def test_classify_by_description_keywords_person(self, parser_with_defaults):
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "SomeId", "string", "uuid", "The person who owns this"
        )
        assert is_ctx is True
        assert context_key == "person_id"

    def test_classify_by_description_keywords_vehicle(self, parser_with_defaults):
        # Use "integer" type so person_id (type_hints=["string"]) doesn't reach threshold
        # vehicle_id: description "vehicle" = 3 points -> matches
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "AssetId", "integer", None, "The vehicle asset identifier"
        )
        assert is_ctx is True
        assert context_key == "vehicle_id"

    def test_classify_by_description_keywords_tenant(self, parser_with_defaults):
        # Use "integer" type so earlier patterns don't reach threshold
        # tenant_id: description "tenant" = 3 points -> matches
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "OrgId", "integer", None, "The tenant organization"
        )
        assert is_ctx is True
        assert context_key == "tenant_id"

    def test_classify_by_fallback_name(self, parser_with_defaults):
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "PersonId", "string", None, "Some ID"
        )
        assert is_ctx is True
        assert context_key == "person_id"

    def test_classify_by_fallback_driverid(self, parser_with_defaults):
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "DriverId", "string", None, ""
        )
        assert is_ctx is True
        assert context_key == "person_id"

    def test_not_classified_regular_param(self, parser_with_defaults):
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "PageSize", "integer", "int32", "Number of results per page"
        )
        assert is_ctx is False
        assert context_key is None

    def test_format_plus_type_alone_does_not_classify(self, parser_with_defaults):
        """Format uuid (2) + type string (1) WITHOUT description-keyword or
        name-pattern is rejected. Old behavior was a false-positive engine —
        every uuid-string param landed in person_id (the first dict key)."""
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "RandomId", "string", "uuid", "some random identifier"
        )
        # No keyword in description ("random" is not a person/vehicle/tenant
        # keyword), no name pattern matches "RandomId" → classifier rejects
        # despite format+type score totalling 3.
        assert is_ctx is False, (
            "Generic uuid-string params must NOT auto-classify — they were "
            "the source of 130+ mistagged tools."
        )

    def test_no_score_below_threshold(self, parser_with_defaults):
        """Only type match (1 point) should not classify."""
        context_key, is_ctx = parser_with_defaults._classify_context_parameter(
            "RandomField", "string", None, "no relevant keywords here"
        )
        assert is_ctx is False


# =============================================================================
# 10. _parse_parameter
# =============================================================================

class TestParseParameter:
    """Tests for parameter parsing."""

    def test_parse_query_parameter(self, parser_with_defaults):
        param = {
            "name": "Status",
            "in": "query",
            "required": True,
            "schema": {"type": "string", "enum": ["active", "inactive"]},
            "description": "Filter by status",
        }
        result = parser_with_defaults._parse_parameter(param)
        assert result is not None
        assert result.name == "Status"
        assert result.location == "query"
        assert result.required is True
        assert result.param_type == "string"
        assert result.enum_values == ["active", "inactive"]
        assert result.dependency_source == DependencySource.FROM_USER

    def test_parse_path_parameter(self, parser_with_defaults):
        param = {
            "name": "id",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
            "description": "Resource ID",
        }
        result = parser_with_defaults._parse_parameter(param)
        assert result is not None
        assert result.location == "path"

    def test_skip_header_parameter(self, parser_with_defaults):
        param = {
            "name": "Authorization",
            "in": "header",
            "schema": {"type": "string"},
        }
        result = parser_with_defaults._parse_parameter(param)
        assert result is None

    def test_skip_parameter_without_name(self, parser_with_defaults):
        param = {"in": "query", "schema": {"type": "string"}}
        result = parser_with_defaults._parse_parameter(param)
        assert result is None

    def test_skip_blacklisted_parameter(self, parser_with_defaults):
        param = {
            "name": "MaintenanceMileagePeriod",
            "in": "query",
            "schema": {"type": "integer"},
        }
        result = parser_with_defaults._parse_parameter(param)
        assert result is None

    def test_skip_blacklisted_parameter_days(self, parser_with_defaults):
        param = {
            "name": "MaintenanceDaysPeriod",
            "in": "query",
            "schema": {"type": "integer"},
        }
        result = parser_with_defaults._parse_parameter(param)
        assert result is None

    def test_context_parameter_sets_preferred_operator(self, parser_with_defaults):
        """person_id context params get preferred_operator = (contains)."""
        param = {
            "name": "PersonId",
            "in": "query",
            "schema": {"type": "string", "format": "uuid"},
            "description": "The person owner",
        }
        result = parser_with_defaults._parse_parameter(param)
        assert result is not None
        assert result.dependency_source == DependencySource.FROM_CONTEXT
        assert result.context_key == "person_id"
        assert result.preferred_operator == "(contains)"
        assert result.is_filterable is True

    def test_description_truncated_to_200(self, parser_with_defaults):
        param = {
            "name": "Notes",
            "in": "query",
            "schema": {"type": "string"},
            "description": "A" * 300,
        }
        result = parser_with_defaults._parse_parameter(param)
        assert len(result.description) == 200

    def test_defaults_for_missing_schema(self, parser_with_defaults):
        param = {"name": "Simple", "in": "query"}
        result = parser_with_defaults._parse_parameter(param)
        assert result is not None
        assert result.param_type == "string"
        assert result.format is None


# =============================================================================
# 11. _parse_request_body
# =============================================================================

class TestParseRequestBody:
    """Tests for request body parsing."""

    def test_parse_body_with_ref(self, parser_with_defaults, spec_with_post):
        request_body = spec_with_post["paths"]["/api/v2/items"]["post"]["requestBody"]
        params = parser_with_defaults._parse_request_body(request_body, spec_with_post)

        assert "Name" in params
        assert "PersonId" in params
        assert "Notes" in params
        assert params["Name"].required is True
        assert params["PersonId"].required is True
        assert params["Notes"].required is False
        assert params["Name"].location == "body"

    def test_person_id_classified_in_body(self, parser_with_defaults, spec_with_post):
        request_body = spec_with_post["paths"]["/api/v2/items"]["post"]["requestBody"]
        params = parser_with_defaults._parse_request_body(request_body, spec_with_post)

        assert params["PersonId"].dependency_source == DependencySource.FROM_CONTEXT
        assert params["PersonId"].context_key == "person_id"
        assert params["PersonId"].preferred_operator == "(contains)"
        assert params["PersonId"].is_filterable is True

    def test_empty_request_body(self, parser_with_defaults):
        request_body = {}
        params = parser_with_defaults._parse_request_body(request_body, {})
        assert params == {}

    def test_body_with_no_properties(self, parser_with_defaults):
        request_body = {
            "content": {
                "application/json": {
                    "schema": {"type": "object"}
                }
            }
        }
        params = parser_with_defaults._parse_request_body(request_body, {})
        assert params == {}

    def test_body_skips_blacklisted_params(self, parser_with_defaults):
        request_body = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "MaintenanceMileagePeriod": {"type": "integer"},
                            "ValidField": {"type": "string"},
                        },
                    }
                }
            }
        }
        params = parser_with_defaults._parse_request_body(request_body, {})
        assert "MaintenanceMileagePeriod" not in params
        assert "ValidField" in params

    def test_body_with_array_items_type(self, parser_with_defaults):
        request_body = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "Tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of tags",
                            }
                        },
                    }
                }
            }
        }
        params = parser_with_defaults._parse_request_body(request_body, {})
        assert params["Tags"].items_type == "string"
        assert params["Tags"].param_type == "array"

    def test_body_with_enum(self, parser_with_defaults):
        request_body = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "Priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            }
                        },
                    }
                }
            }
        }
        params = parser_with_defaults._parse_request_body(request_body, {})
        assert params["Priority"].enum_values == ["low", "medium", "high"]


# =============================================================================
# 12. _infer_output_keys
# =============================================================================

class TestInferOutputKeys:
    """Tests for output key inference from response schemas."""

    def test_infer_from_array_response_with_ref(self, parser_with_defaults, minimal_spec):
        operation = minimal_spec["paths"]["/api/v2/items"]["get"]
        keys = parser_with_defaults._infer_output_keys(operation, minimal_spec)
        assert set(keys) == {"Id", "Name", "Status"}

    def test_infer_from_direct_object_response(self, parser_with_defaults):
        spec = {
            "components": {
                "schemas": {
                    "Detail": {
                        "type": "object",
                        "properties": {
                            "Id": {"type": "string"},
                            "Description": {"type": "string"},
                        },
                    }
                }
            }
        }
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Detail"}
                        }
                    }
                }
            }
        }
        keys = parser_with_defaults._infer_output_keys(operation, spec)
        assert set(keys) == {"Id", "Description"}

    def test_infer_from_201_response(self, parser_with_defaults, spec_with_post):
        operation = spec_with_post["paths"]["/api/v2/items"]["post"]
        keys = parser_with_defaults._infer_output_keys(operation, spec_with_post)
        assert set(keys) == {"Id", "Name", "PersonId"}

    def test_infer_from_wrapper_items(self, parser_with_defaults):
        """Response with Items wrapper array."""
        spec = {
            "components": {
                "schemas": {
                    "Inner": {
                        "type": "object",
                        "properties": {
                            "Id": {"type": "string"},
                            "Value": {"type": "integer"},
                        },
                    }
                }
            }
        }
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "Items": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Inner"},
                                    },
                                    "TotalCount": {"type": "integer"},
                                },
                            }
                        }
                    }
                }
            }
        }
        keys = parser_with_defaults._infer_output_keys(operation, spec)
        assert set(keys) == {"Id", "Value"}

    def test_infer_from_wrapper_data(self, parser_with_defaults):
        """Response with Data wrapper that has properties."""
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "Data": {
                                        "type": "object",
                                        "properties": {
                                            "Name": {"type": "string"},
                                            "Age": {"type": "integer"},
                                        },
                                    }
                                },
                            }
                        }
                    }
                }
            }
        }
        keys = parser_with_defaults._infer_output_keys(operation, {})
        assert set(keys) == {"Name", "Age"}

    def test_no_responses(self, parser_with_defaults):
        operation = {}
        assert parser_with_defaults._infer_output_keys(operation, {}) == []

    def test_no_success_response(self, parser_with_defaults):
        operation = {"responses": {"400": {"description": "Bad request"}}}
        assert parser_with_defaults._infer_output_keys(operation, {}) == []

    def test_no_content_in_response(self, parser_with_defaults):
        operation = {"responses": {"200": {"description": "OK"}}}
        assert parser_with_defaults._infer_output_keys(operation, {}) == []

    def test_empty_schema(self, parser_with_defaults):
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {"schema": {}}
                    }
                }
            }
        }
        assert parser_with_defaults._infer_output_keys(operation, {}) == []

    def test_204_response(self, parser_with_defaults):
        """204 No Content typically has no schema."""
        operation = {"responses": {"204": {"description": "No Content"}}}
        assert parser_with_defaults._infer_output_keys(operation, {}) == []

    def test_text_json_content_type(self, parser_with_defaults):
        """text/json content type is also supported."""
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "text/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "Result": {"type": "string"},
                                },
                            }
                        }
                    }
                }
            }
        }
        keys = parser_with_defaults._infer_output_keys(operation, {})
        assert keys == ["Result"]


# =============================================================================
# 13. _parse_operation
# =============================================================================

class TestParseOperation:
    """Tests for full operation parsing."""

    def test_parse_get_operation(self, parser_with_defaults, minimal_spec, build_embedding_text_fn):
        operation = minimal_spec["paths"]["/api/v2/items"]["get"]
        tool = parser_with_defaults._parse_operation(
            service_name="testservice",
            service_url="https://api.example.com/svc",
            path="/api/v2/items",
            method="GET",
            operation=operation,
            spec=minimal_spec,
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert tool is not None
        assert tool.operation_id == "get_Items"
        assert tool.method == "GET"
        assert tool.service_name == "testservice"
        assert "Status" in tool.parameters
        # GET methods should have Rows injected
        assert "Rows" in tool.parameters
        assert tool.parameters["Rows"].default_value == 100
        assert set(tool.output_keys) == {"Id", "Name", "Status"}

    def test_parse_post_operation(self, parser_with_defaults, spec_with_post, build_embedding_text_fn):
        operation = spec_with_post["paths"]["/api/v2/items"]["post"]
        tool = parser_with_defaults._parse_operation(
            service_name="testservice",
            service_url="https://api.example.com/svc",
            path="/api/v2/items",
            method="POST",
            operation=operation,
            spec=spec_with_post,
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert tool is not None
        assert tool.operation_id == "create_Item"
        assert tool.method == "POST"
        # POST should NOT get Rows param
        assert "Rows" not in tool.parameters
        assert "Name" in tool.required_params
        assert "PersonId" in tool.required_params

    def test_blacklisted_operation_returns_none(self, parser_with_defaults, build_embedding_text_fn):
        operation = {
            "operationId": "exportData",
            "summary": "Export data",
            "parameters": [],
            "responses": {},
        }
        tool = parser_with_defaults._parse_operation(
            service_name="svc",
            service_url="https://api.example.com/svc",
            path="/api/export",
            method="GET",
            operation=operation,
            spec={},
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert tool is None

    def test_generates_operation_id_when_missing(self, parser_with_defaults, build_embedding_text_fn):
        operation = {
            "summary": "Get stuff",
            "parameters": [],
            "responses": {},
        }
        tool = parser_with_defaults._parse_operation(
            service_name="svc",
            service_url="https://api.example.com/svc",
            path="/api/v2/stuff",
            method="GET",
            operation=operation,
            spec={},
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert tool is not None
        assert tool.operation_id == "get_api_v2_stuff"

    def test_description_combines_summary_and_description(self, parser_with_defaults, build_embedding_text_fn):
        operation = {
            "operationId": "get_Things",
            "summary": "Get things",
            "description": "Returns all the things",
            "parameters": [],
            "responses": {},
        }
        tool = parser_with_defaults._parse_operation(
            service_name="svc",
            service_url="https://api.example.com/svc",
            path="/api/things",
            method="GET",
            operation=operation,
            spec={},
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert "Get things" in tool.description
        assert "Returns all the things" in tool.description

    def test_rows_not_injected_when_already_present(self, parser_with_defaults, build_embedding_text_fn):
        operation = {
            "operationId": "get_Data",
            "summary": "Get data",
            "parameters": [
                {
                    "name": "Rows",
                    "in": "query",
                    "schema": {"type": "integer"},
                    "description": "Number of rows",
                }
            ],
            "responses": {},
        }
        tool = parser_with_defaults._parse_operation(
            service_name="svc",
            service_url="https://api.example.com/svc",
            path="/api/data",
            method="GET",
            operation=operation,
            spec={},
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert tool is not None
        # Rows should be present but from original params, not injected
        assert "Rows" in tool.parameters

    def test_tags_preserved(self, parser_with_defaults, minimal_spec, build_embedding_text_fn):
        operation = minimal_spec["paths"]["/api/v2/items"]["get"]
        tool = parser_with_defaults._parse_operation(
            service_name="svc",
            service_url="https://api.example.com/svc",
            path="/api/v2/items",
            method="GET",
            operation=operation,
            spec=minimal_spec,
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert tool.tags == ["Items"]

    def test_swagger_name_extracted(self, parser_with_defaults, build_embedding_text_fn):
        operation = {
            "operationId": "get_Records",
            "summary": "Get records",
            "parameters": [],
            "responses": {},
        }
        tool = parser_with_defaults._parse_operation(
            service_name="svc",
            service_url="https://api.example.com/masterdata/api",
            path="/api/records",
            method="GET",
            operation=operation,
            spec={},
            build_embedding_text_fn=build_embedding_text_fn,
        )
        assert tool.swagger_name == "masterdata"


# =============================================================================
# 14. parse_spec (full integration)
# =============================================================================

class TestParseSpec:
    """Integration tests for full spec parsing."""

    async def test_parse_spec_full(self, parser_with_defaults, minimal_spec, build_embedding_text_fn):
        with patch.object(parser_with_defaults, "fetch_spec", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = minimal_spec
            tools = await parser_with_defaults.parse_spec(
                "https://api.example.com/testservice/swagger/v1/swagger.json",
                build_embedding_text_fn,
            )
        assert len(tools) == 1
        assert tools[0].operation_id == "get_Items"

    async def test_parse_spec_empty_spec(self, parser_with_defaults, build_embedding_text_fn):
        with patch.object(parser_with_defaults, "fetch_spec", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = None
            tools = await parser_with_defaults.parse_spec(
                "https://api.example.com/svc/swagger/v1/swagger.json",
                build_embedding_text_fn,
            )
        assert tools == []

    async def test_parse_spec_skips_non_http_methods(self, parser_with_defaults, build_embedding_text_fn):
        """Methods like 'options', 'head', 'parameters' are skipped."""
        spec = {
            "servers": [{"url": "https://api.example.com/svc"}],
            "paths": {
                "/api/items": {
                    "options": {"operationId": "options_Items", "responses": {}},
                    "head": {"operationId": "head_Items", "responses": {}},
                    "get": {
                        "operationId": "get_Items",
                        "summary": "List items",
                        "parameters": [],
                        "responses": {},
                    },
                }
            },
        }
        with patch.object(parser_with_defaults, "fetch_spec", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = spec
            tools = await parser_with_defaults.parse_spec(
                "https://api.example.com/svc/swagger/v1/swagger.json",
                build_embedding_text_fn,
            )
        assert len(tools) == 1
        assert tools[0].operation_id == "get_Items"

    async def test_parse_spec_skips_failing_operations(self, parser_with_defaults, build_embedding_text_fn):
        """Operations that raise exceptions are skipped gracefully."""
        spec = {
            "servers": [{"url": "https://api.example.com/svc"}],
            "paths": {
                "/api/items": {
                    "get": {
                        "operationId": "get_Items",
                        "summary": "Get items",
                        "parameters": [],
                        "responses": {},
                    }
                }
            },
        }
        with patch.object(parser_with_defaults, "fetch_spec", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = spec
            with patch.object(
                parser_with_defaults, "_parse_operation", side_effect=Exception("boom")
            ):
                tools = await parser_with_defaults.parse_spec(
                    "https://api.example.com/svc/swagger/v1/swagger.json",
                    build_embedding_text_fn,
                )
        assert tools == []

    async def test_parse_spec_multiple_paths_and_methods(self, parser_with_defaults, build_embedding_text_fn):
        spec = {
            "servers": [{"url": "https://api.example.com/svc"}],
            "paths": {
                "/api/items": {
                    "get": {
                        "operationId": "get_Items",
                        "summary": "List items",
                        "parameters": [],
                        "responses": {},
                    },
                    "post": {
                        "operationId": "create_Item",
                        "summary": "Create item",
                        "parameters": [],
                        "responses": {},
                    },
                },
                "/api/items/{id}": {
                    "get": {
                        "operationId": "get_ItemById",
                        "summary": "Get item",
                        "parameters": [],
                        "responses": {},
                    },
                    "delete": {
                        "operationId": "delete_Item",
                        "summary": "Delete item",
                        "parameters": [],
                        "responses": {},
                    },
                },
            },
        }
        with patch.object(parser_with_defaults, "fetch_spec", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = spec
            tools = await parser_with_defaults.parse_spec(
                "https://api.example.com/svc/swagger/v1/swagger.json",
                build_embedding_text_fn,
            )
        assert len(tools) == 4
        op_ids = {t.operation_id for t in tools}
        assert op_ids == {"get_Items", "create_Item", "get_ItemById", "delete_Item"}


# =============================================================================
# 15. Edge cases and additional scenarios
# =============================================================================

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_blacklist_patterns_completeness(self):
        """Ensure all expected blacklist patterns are present."""
        expected = {"batch", "excel", "export", "import", "internal",
                    "count", "odata", "searchinfo", "swagger", "health"}
        assert SwaggerParser.BLACKLIST_PATTERNS == expected

    def test_blacklisted_parameters_set(self):
        """Ensure blacklisted parameter names are present."""
        assert "MaintenanceMileagePeriod" in SwaggerParser.BLACKLISTED_PARAMETERS
        assert "MaintenanceDaysPeriod" in SwaggerParser.BLACKLISTED_PARAMETERS

    def test_resolve_ref_with_empty_ref_path(self, parser_with_defaults):
        """Edge case: $ref is '#/' (root)."""
        schema = {"$ref": "#/"}
        spec = {"type": "object"}
        # parts = [""] after split("/"), spec.get("") -> {}
        resolved = parser_with_defaults._resolve_ref(schema, spec)
        assert resolved == {}

    def test_parse_parameter_defaults_location_to_query(self, parser_with_defaults):
        param = {"name": "Foo", "schema": {"type": "string"}}
        result = parser_with_defaults._parse_parameter(param)
        assert result.location == "query"

    def test_base_url_trailing_slash_stripped(self, parser_with_defaults):
        spec = {"servers": [{"url": "https://api.example.com/svc/"}]}
        result = parser_with_defaults._extract_base_url(spec)
        assert not result.endswith("/")

    def test_infer_output_keys_wrapper_value(self, parser_with_defaults):
        """Test 'value' wrapper key for OData-style responses."""
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "value": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "Id": {"type": "string"},
                                                "Title": {"type": "string"},
                                            },
                                        },
                                    },
                                    "@odata.count": {"type": "integer"},
                                },
                            }
                        }
                    }
                }
            }
        }
        keys = parser_with_defaults._infer_output_keys(operation, {})
        assert set(keys) == {"Id", "Title"}

    def test_infer_output_keys_results_wrapper(self, parser_with_defaults):
        """Test 'results' wrapper key."""
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "results": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "Key": {"type": "string"},
                                            },
                                        },
                                    }
                                },
                            }
                        }
                    }
                }
            }
        }
        keys = parser_with_defaults._infer_output_keys(operation, {})
        assert keys == ["Key"]

    def test_swagger2_base_url_default_scheme(self, parser_with_defaults):
        """Swagger 2 spec with host but no schemes defaults to https."""
        spec = {"host": "api.example.com", "basePath": "/api"}
        result = parser_with_defaults._extract_base_url(spec)
        assert result == "https://api.example.com/api"

    def test_extract_swagger_name_deep_path(self, parser_with_defaults):
        result = parser_with_defaults._extract_swagger_name("https://api.example.com/a/b/c")
        assert result == "a"
