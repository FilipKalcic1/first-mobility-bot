"""
Circuit Breaker - Endpoint Failure Protection

Prevents cascading failures by disabling failing endpoints temporarily.
After 3 consecutive failures, endpoint is DISABLED for 60 seconds.

NO business logic - purely infrastructure pattern.
"""

import asyncio
import logging
import time
from typing import Dict, Optional
from enum import Enum
from dataclasses import dataclass

from services.errors import CircuitOpenError


logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    """Circuit breaker states."""
    CLOSED = "closed"                    # Normal operation
    OPEN = "open"                        # Failures detected - blocking calls
    HALF_OPEN = "half_open"              # Cooldown elapsed, next caller may probe
    HALF_OPEN_PROBING = "half_open_probing"  # A probe is in flight; block others


@dataclass
class CircuitMetrics:
    """Metrics for single endpoint."""
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    state: CircuitState = CircuitState.CLOSED
    opened_at: Optional[float] = None


class CircuitBreaker:
    """
    Circuit breaker for API endpoints.

    Pattern:
    - CLOSED: Normal operation, calls go through
    - OPEN: Too many failures, calls are blocked
    - HALF_OPEN: Testing if endpoint recovered

    Configuration:
    - Failure threshold: 3 consecutive failures
    - Open duration: 60 seconds
    - Reset after: 5 consecutive successes
    """

    FAILURE_THRESHOLD = 3
    OPEN_DURATION_SECONDS = 60
    SUCCESS_THRESHOLD_TO_RESET = 5

    def __init__(self) -> None:
        """Initialize circuit breaker."""
        self.circuits: Dict[str, CircuitMetrics] = {}
        self._lock = asyncio.Lock()
        logger.info("CircuitBreaker initialized")

    async def call(self, endpoint_key: str, func, *args, **kwargs):
        """
        Execute function through circuit breaker.

        Args:
            endpoint_key: Unique endpoint identifier (e.g., "POST /api/resource")
            func: Async function to execute
            *args, **kwargs: Function arguments

        Returns:
            Function result

        Raises:
            CircuitOpenError: If circuit is open
            Original exception: If function fails
        """
        async with self._lock:
            circuit = self._get_circuit(endpoint_key)

            if circuit.state == CircuitState.OPEN:
                if self._should_attempt_reset(circuit):
                    circuit.state = CircuitState.HALF_OPEN_PROBING
                    logger.info(f"Circuit HALF_OPEN (probing): {endpoint_key}")
                else:
                    raise CircuitOpenError(
                        endpoint_key,
                        cooldown_seconds=self._time_until_reset(circuit),
                    )
            elif circuit.state == CircuitState.HALF_OPEN:
                # Cooldown already elapsed — take the probe slot.
                circuit.state = CircuitState.HALF_OPEN_PROBING
            elif circuit.state == CircuitState.HALF_OPEN_PROBING:
                # A probe is already in flight; block subsequent callers.
                raise CircuitOpenError(endpoint_key, cooldown_seconds=5.0)
        try:
            result = await func(*args, **kwargs)
            await self._record_success(endpoint_key)
            return result

        except Exception:
            await self._record_failure(endpoint_key)
            raise

    async def _record_success(self, endpoint_key: str) -> None:
        """Record successful call."""
        async with self._lock:
            circuit = self._get_circuit(endpoint_key)
            circuit.success_count += 1
            circuit.failure_count = 0
            circuit.last_success_time = time.time()

            if circuit.state == CircuitState.HALF_OPEN_PROBING:
                if circuit.success_count >= self.SUCCESS_THRESHOLD_TO_RESET:
                    circuit.state = CircuitState.CLOSED
                    circuit.opened_at = None
                    logger.info(f"Circuit CLOSED: {endpoint_key}")
                else:
                    # Probe succeeded but threshold not yet met — release the slot.
                    circuit.state = CircuitState.HALF_OPEN

    async def _record_failure(self, endpoint_key: str) -> None:
        """Record failed call."""
        async with self._lock:
            circuit = self._get_circuit(endpoint_key)
            circuit.failure_count += 1
            circuit.success_count = 0
            circuit.last_failure_time = time.time()

            # A failure during probe means the endpoint is still unhealthy —
            # trip the breaker back to OPEN immediately.
            if circuit.state == CircuitState.HALF_OPEN_PROBING:
                circuit.state = CircuitState.OPEN
                circuit.opened_at = time.time()
                logger.warning(
                    f"🔴 Circuit re-OPEN after HALF_OPEN probe failure: {endpoint_key}"
                )
                return

            if circuit.failure_count >= self.FAILURE_THRESHOLD:
                circuit.state = CircuitState.OPEN
                circuit.opened_at = time.time()
                logger.warning(
                    f"🔴 Circuit OPEN: {endpoint_key} "
                    f"(failures: {circuit.failure_count})"
                )

    def _get_circuit(self, endpoint_key: str) -> CircuitMetrics:
        """Get or create circuit for endpoint."""
        if endpoint_key not in self.circuits:
            self.circuits[endpoint_key] = CircuitMetrics()
        return self.circuits[endpoint_key]

    def _should_attempt_reset(self, circuit: CircuitMetrics) -> bool:
        """Check if enough time passed to attempt reset."""
        if not circuit.opened_at:
            return False

        elapsed = time.time() - circuit.opened_at
        return elapsed >= self.OPEN_DURATION_SECONDS

    def _time_until_reset(self, circuit: CircuitMetrics) -> float:
        """Calculate time until circuit can be tested."""
        if not circuit.opened_at:
            return 0.0

        elapsed = time.time() - circuit.opened_at
        remaining = self.OPEN_DURATION_SECONDS - elapsed
        return max(0.0, remaining)

    async def get_status(self, endpoint_key: str) -> Dict:
        """Get circuit status for endpoint."""
        async with self._lock:
            if endpoint_key not in self.circuits:
                return {"state": "closed", "never_used": True}

            circuit = self.circuits[endpoint_key]
            return {
                "state": circuit.state,
                "failure_count": circuit.failure_count,
                "success_count": circuit.success_count,
                "time_until_reset": self._time_until_reset(circuit)
            }

    async def reset(self, endpoint_key: str) -> None:
        """Manually reset circuit."""
        async with self._lock:
            if endpoint_key in self.circuits:
                circuit = self.circuits[endpoint_key]
                circuit.state = CircuitState.CLOSED
                circuit.failure_count = 0
                circuit.success_count = 0
                circuit.opened_at = None
                logger.info(f"Circuit manually reset: {endpoint_key}")


# CircuitOpenError is imported from services.errors and re-exported here
# for backward compatibility with: from services.circuit_breaker import CircuitOpenError
__all__ = ["CircuitBreaker", "CircuitState", "CircuitMetrics", "CircuitOpenError"]
