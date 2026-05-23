"""L7 — Tool Executor with per-service Circuit Breaker.

Resolves service URL + tenant + auth header from registry/identity,
issues HTTP via the gateway, and returns the structured response.

Circuit breaker per MobilityOne service (`automation`, `tenantmgt`,
`vehiclemgt`):
  - 3 consecutive 5xx or timeout > 5s → circuit OPEN for 30s
  - During OPEN: every call returns immediately with "service down"
    error — no LLM cost wasted on unfinishable queries
  - After 30s: HALF_OPEN; one trial call decides — pass closes,
    fail re-opens for 60s

Council Pass 4 SECURITY+PRAGMATIST critique addressed: failure isolation
without per-query LLM expense.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)


# Circuit breaker constants.
FAIL_THRESHOLD = 3
OPEN_SECONDS = 30
HALF_OPEN_RE_FAIL_SECONDS = 60
TIMEOUT_SECONDS = 5.0

# Circuit states.
STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


@dataclass
class CircuitState:
    state: str = STATE_CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    # How long the current OPEN window lasts. Initial OPEN: OPEN_SECONDS.
    # Re-open after a failed HALF_OPEN trial: HALF_OPEN_RE_FAIL_SECONDS.
    open_duration: float = 0.0


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    data: Any = None
    status_code: Optional[int] = None
    error: Optional[str] = None
    # Raw API response body for failed calls — used by ApiErrorTranslator
    # (Faza 2 Filip 2026-05-17) to generate Croatian explanation for the user
    # instead of generic "Tehnički problem". May be dict, str, or None.
    error_body: Any = None
    circuit_open: bool = False


class ToolExecutor:
    """Wraps gateway with circuit breaker + auth header injection."""

    def __init__(self, gateway, tool_registry):
        self._gateway = gateway
        self._registry = tool_registry
        self._circuits: dict[str, CircuitState] = {}
        # NOTE (Filip 2026-05-20 cleanup): the param-validation hook (#85) was
        # removed. param_validator.py was deleted in the 2026-05-09 "simplify
        # pass" — its required-check is covered by llm_router + param-asking,
        # ISO-date by param_ui (Faza 4). MobilityOne API + ApiErrorTranslator
        # validate the rest. The dead extension point is gone to avoid
        # implying validation that doesn't happen.

    def method_of(self, tool_id: Optional[str]) -> Optional[str]:
        """Public accessor — keeps registry private to this layer."""
        if not tool_id:
            return None
        return self._registry.method_of(tool_id)

    async def execute(
        self,
        tool_id: str,
        params: dict,
        identity_summary: dict,
    ) -> ExecutionResult:
        """Run the registered tool with given params + identity context."""
        spec = self._registry.spec_for(tool_id)
        if spec is None:
            return ExecutionResult(
                success=False, error=f"unknown_tool:{tool_id}"
            )

        # Tenant isolation — refuse if tenant_id missing for tenant-scoped tools.
        # Prevents cross-tenant data leak from buggy router or stale cache.
        tenant_id = (
            identity_summary.get("tenant_id")
            or spec.get("default_tenant_id")
        )
        if not tenant_id and spec.get("tenant_scoped", True):
            return ExecutionResult(
                success=False,
                error="missing_tenant_id",
            )

        service = spec.get("service")
        path = spec.get("path") or ""
        method = (spec.get("method") or "GET").upper()

        # Inject `context` params from identity (Filip 2026-05-23 fix #3). The
        # registry marks params like VehicleId/personId dependency_source=
        # "context" with a context_key (vehicle_id/person_id/...). These are
        # NEVER asked from the user and NEVER LLM-extracted, so without this the
        # generic path sent e.g. post_AddMileage with no VehicleId → 422.
        # tenant_id is handled separately (x-tenant header). company_id/
        # org_unit_id aren't in identity → left unfilled (API reports if needed).
        params = dict(params or {})
        for pname, ckey in (spec.get("context_params") or {}).items():
            if params.get(pname) in (None, "") and ckey:
                val = identity_summary.get(ckey)
                if val is not None:
                    params[pname] = val

        # Route each param to its HTTP location (path/query/body/header). The
        # registry marks every param's `location` (derived from Swagger).
        # Filip 2026-05-23 fix #2: previously ALL params were dumped into query
        # (GET) or body (else) and {id} path placeholders were NEVER substituted
        # → every by-id tool built a URL like /Roles/{id} → MobilityOne 404.
        locations = spec.get("param_locations") or {}
        default_loc = "query" if method == "GET" else "body"
        path_vals: dict = {}
        query: dict = {}
        body: dict = {}
        for name, value in (params or {}).items():
            loc = locations.get(name, default_loc)
            if loc == "path":
                path_vals[name] = value
            elif loc == "query":
                query[name] = value
            elif loc == "header":
                # gateway.call has no header passthrough; send as query (rare).
                query[name] = value
            else:  # body
                body[name] = value

        for name, value in path_vals.items():
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        if "{" in path and "}" in path:
            # A required path placeholder was not filled — refuse rather than
            # send a malformed URL (would 404 at MobilityOne).
            return ExecutionResult(
                success=False, error="missing_path_param",
            )

        # Circuit gate
        circuit = self._get_or_init_circuit(service)
        gate = self._check_circuit(circuit)
        if gate is not None:
            return gate

        try:
            response = await asyncio.wait_for(
                self._gateway.call(
                    method=method,
                    service=service,
                    path=path,
                    query_params=query or None,
                    body=body or None,
                    tenant_id=tenant_id,
                ),
                timeout=TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self._record_failure(service)
            return ExecutionResult(
                success=False, error="timeout", status_code=None
            )
        except Exception as e:  # noqa: BLE001
            self._record_failure(service)
            return ExecutionResult(
                success=False, error=f"gateway_error:{type(e).__name__}",
            )

        if not response.success:
            if response.status_code and response.status_code >= 500:
                self._record_failure(service)
            # Surface the raw body so the engine's ApiErrorTranslator can
            # turn it into a Croatian explanation. Gateway puts the parsed
            # body in `error_message` (string) for failed responses, and
            # may also leave `data` populated for some 4xx with JSON body.
            error_body = (
                getattr(response, "error_message", None)
                or getattr(response, "data", None)
            )
            return ExecutionResult(
                success=False, status_code=response.status_code,
                error=f"http_{response.status_code}",
                error_body=error_body,
            )

        self._record_success(service)
        return ExecutionResult(
            success=True, data=response.data,
            status_code=response.status_code,
        )

    # ---- circuit logic --------------------------------------------------

    def _get_or_init_circuit(self, service: str) -> CircuitState:
        return self._circuits.setdefault(service, CircuitState())

    def _check_circuit(self, circuit: CircuitState) -> Optional[ExecutionResult]:
        """If circuit is OPEN, refuse fast. If HALF_OPEN, let through."""
        now = time.time()
        if circuit.state == STATE_CLOSED:
            return None
        if circuit.state == STATE_OPEN:
            if now - circuit.opened_at >= circuit.open_duration:
                circuit.state = STATE_HALF_OPEN
                return None  # let one trial call through
            return ExecutionResult(
                success=False, circuit_open=True,
                error="MobilityOne servis trenutno nedostupan. "
                      "Pokušaj za nekoliko minuta.",
            )
        # HALF_OPEN — allow this call; success/failure reshapes state
        return None

    def _record_failure(self, service: str) -> None:
        circuit = self._get_or_init_circuit(service)
        circuit.consecutive_failures += 1
        if circuit.state == STATE_HALF_OPEN:
            circuit.state = STATE_OPEN
            circuit.opened_at = time.time()
            circuit.open_duration = HALF_OPEN_RE_FAIL_SECONDS
            logger.warning("circuit %s re-opened after half-open failure", service)
        elif circuit.consecutive_failures >= FAIL_THRESHOLD:
            circuit.state = STATE_OPEN
            circuit.opened_at = time.time()
            circuit.open_duration = OPEN_SECONDS
            logger.warning("circuit %s OPEN after %d failures",
                           service, circuit.consecutive_failures)

    def _record_success(self, service: str) -> None:
        circuit = self._get_or_init_circuit(service)
        circuit.consecutive_failures = 0
        circuit.state = STATE_CLOSED
