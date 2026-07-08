from __future__ import annotations

import asyncio
import time

from .metrics import CIRCUIT_BREAKER_STATE


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open; callers should not retry."""


class CircuitBreaker:
    """
    Async circuit breaker: CLOSED → OPEN (fast-fail) → HALF-OPEN (probe) → CLOSED.

    failure_threshold  : consecutive whole-inference-chain failures to open the circuit.
    recovery_timeout   : seconds to wait in OPEN before allowing a probe request.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    def _state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.recovery_timeout:
            return "half_open"
        return "open"

    async def before_call(self) -> None:
        """Raise CircuitOpenError immediately if the circuit is open."""
        async with self._lock:
            if self._state() == "open":
                secs_left = self.recovery_timeout - (time.monotonic() - self._opened_at)
                raise CircuitOpenError(
                    f"Circuit breaker OPEN — VLM appears down. "
                    f"Will probe again in {max(secs_left, 0):.0f}s."
                )

    async def on_success(self) -> None:
        async with self._lock:
            if self._opened_at is not None:
                CIRCUIT_BREAKER_STATE.set(0)
            self._consecutive_failures = 0
            self._opened_at = None

    async def on_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                # Reset the timer on every qualifying failure, including a
                # failing HALF_OPEN probe — ensures a full recovery_timeout
                # wait after each probe failure instead of re-probing immediately.
                self._opened_at = time.monotonic()
                CIRCUIT_BREAKER_STATE.set(1)
