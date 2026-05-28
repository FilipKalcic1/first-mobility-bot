"""
Tool Contracts - Pydantic Models for Unified Tool Definition

Domain-agnostic tool metadata contracts.
NO business logic, NO domain-specific references.
"""

import hashlib
from enum import Enum
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, field_validator


class DependencySource(str, Enum):
    """Source of parameter value - defines how parameter is resolved."""
    FROM_CONTEXT = "context"        # System params invisible to LLM (tenant_id, auth_token)
    FROM_USER = "user_input"        # Parameters user must provide
    FROM_TOOL_OUTPUT = "output"     # Parameters from previous tool execution


class ParameterDefinition(BaseModel):
    """Single parameter definition with type and validation rules."""
    name: str
    param_type: str = Field(default="string", description="JSON Schema type")
    format: Optional[str] = None
    description: str = ""
    required: bool = False
    location: str = Field(default="body", description="query | body | path | header")
    dependency_source: DependencySource = DependencySource.FROM_USER
    enum_values: Optional[List[Any]] = None
    default_value: Optional[Any] = None

    # For arrays
    items_type: Optional[str] = None

    # For context injection
    context_key: Optional[str] = None  # Key in user_context dict

    # SURVIVAL KIT: Operator support for filter building
    # CRIT-4 fix (2026-05-28): allow None — registry sometimes serializes
    # explicit nulls for these fields (25 tools had preferred_operator=None
    # and were rejected as "string_type" → silently dropped from routing pool).
    # We coerce None → default in the validator below.
    preferred_operator: Optional[str] = Field(
        default="(=)",
        description="Preferred operator for filter: (=), (contains), (startsWith), (endsWith), (>), (<), (>=), (<=)"
    )
    is_filterable: Optional[bool] = Field(
        default=False,
        description="Indicates if this parameter can be used for filtering"
    )

    @field_validator('preferred_operator', mode='before')
    @classmethod
    def _coerce_operator(cls, v):
        return v if v is not None else "(=)"

    @field_validator('is_filterable', mode='before')
    @classmethod
    def _coerce_filterable(cls, v):
        return v if v is not None else False
    filter_format: Optional[str] = Field(
        default=None,
        description="Filter format pattern, e.g., '{name}({operator}){value}' or 'OData'"
    )
    filter_template: Optional[str] = Field(
        default=None,
        description="Bootstrap template using {context_key} placeholders, e.g. 'Phone(=){phone}'."
    )

    @field_validator('location')
    @classmethod
    def validate_location(cls, v: str) -> str:
        allowed = {"query", "body", "path", "header"}
        if v not in allowed:
            raise ValueError(f"location must be one of {allowed}")
        return v


class UnifiedToolDefinition(BaseModel):
    """
    Unified tool definition - domain agnostic.

    This is the CONTRACT between Registry and Executor.
    All tool metadata must be expressible in this format.
    """
    model_config = {"arbitrary_types_allowed": True}
    operation_id: str = Field(..., description="Unique tool identifier")
    service_name: str = Field(..., description="Service name (e.g., 'automation', 'crm')")
    swagger_name: str = Field(default="", description="Swagger service prefix (e.g., 'automation', 'masterdata')")
    service_url: str = Field(..., description="Base URL of service")
    path: str = Field(..., description="Path template (e.g., /resource/{id})")
    method: str = Field(..., description="HTTP method")

    description: str = Field(default="", description="Human-readable description for semantic search")
    summary: str = Field(default="", description="Short summary")

    # Parameters
    parameters: Dict[str, ParameterDefinition] = Field(default_factory=dict)
    required_params: List[str] = Field(default_factory=list)

    # Dependency tracking
    output_keys: List[str] = Field(
        default_factory=list,
        description="Keys this tool returns (e.g., ['id', 'name'])"
    )
    output_key_aliases: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-tool semantic rename of response keys, e.g. {'Id': 'VehicleId'}."
    )

    # Categorization
    is_retrieval: bool = Field(default=False, description="True if GET method")
    is_mutation: bool = Field(default=False, description="True if POST/PUT/PATCH/DELETE")

    # Semantic metadata
    tags: List[str] = Field(default_factory=list)
    embedding_text: str = Field(default="", description="Prebuilt text for embedding")

    # Versioning
    version_hash: str = Field(default="", description="Hash of spec for cache invalidation")

    @field_validator('method')
    @classmethod
    def validate_method(cls, v: str) -> str:
        v = v.upper()
        if v not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"Invalid HTTP method: {v}")
        return v

    def model_post_init(self, __context) -> None:
        """Auto-set is_retrieval and is_mutation after validation."""
        self.is_retrieval = self.method == "GET"
        self.is_mutation = self.method in {"POST", "PUT", "PATCH", "DELETE"}

        # Generate version hash if not set
        if not self.version_hash:
            self.version_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute hash of tool definition for cache invalidation."""
        signature = f"{self.operation_id}:{self.method}:{self.path}:{len(self.parameters)}"
        return hashlib.md5(signature.encode(), usedforsecurity=False).hexdigest()[:16]

    def get_context_params(self) -> Dict[str, ParameterDefinition]:
        """Get parameters that should be injected from context."""
        return {
            name: param
            for name, param in self.parameters.items()
            if param.dependency_source == DependencySource.FROM_CONTEXT
        }

    def get_user_params(self) -> Dict[str, ParameterDefinition]:
        """Get parameters user must provide."""
        return {
            name: param
            for name, param in self.parameters.items()
            if param.dependency_source == DependencySource.FROM_USER
        }

    def get_output_params(self) -> Dict[str, ParameterDefinition]:
        """Get parameters that come from previous tool outputs."""
        return {
            name: param
            for name, param in self.parameters.items()
            if param.dependency_source == DependencySource.FROM_TOOL_OUTPUT
        }




class DependencyGraph(BaseModel):
    """Graph of tool dependencies for automatic chaining."""
    tool_id: str
    required_outputs: List[str] = Field(
        default_factory=list,
        description="Output keys needed from previous tools"
    )
    provider_tools: List[str] = Field(
        default_factory=list,
        description="Tools that can provide required outputs"
    )
