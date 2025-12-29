"""
Retry Logic and Circuit Breaker for Seedr-Sonarr Proxy
Provides resilient API calls with exponential backoff and graceful degradation.
"""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, Awaitable, TypeVar, Dict, List
from enum import Enum

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"    # Normal operation
    OPEN = "open"        # Failing, reject all calls
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    jitter_factor: float = 0.5  # Random factor 0.5-1.5x

    # Specific operation limits
    seedr_api_max_attempts: int = 5
    download_max_attempts: int = 3
    queue_process_max_attempts: int = 10

    # Retryable error patterns (substrings in error messages)
    retryable_errors: List[str] = field(default_factory=lambda: [
        "timeout",
        "connection",
        "temporary",
        "rate limit",
        "503",
        "502",
        "504",
        "network",
        "reset",
        "refused",
    ])

    # Non-retryable error patterns
    non_retryable_errors: List[str] = field(default_factory=lambda: [
        "invalid",
        "unauthorized",
        "forbidden",
        "not found",
        "bad request",
        "400",
        "401",
        "403",
        "404",
    ])


@dataclass
class RetryStats:
    """Statistics for retry operations."""
    total_attempts: int = 0
    successful_attempts: int = 0
    failed_attempts: int = 0
    retried_operations: int = 0
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None


class RetryHandler:
    """
    Handle retries with exponential backoff.
    Provides configurable retry logic for API operations.
    """

    def __init__(self, config: RetryConfig = None):
        self.config = config or RetryConfig()
        self._failure_counts: Dict[str, int] = {}
        self._last_attempt: Dict[str, float] = {}
        self._stats = RetryStats()

    async def with_retry(
        self,
        operation: Callable[[], Awaitable[T]],
        operation_id: str = None,
        max_attempts: int = None,
        on_retry: Callable[[Exception, int], Awaitable] = None,
        on_error: Callable[[Exception, int], Awaitable] = None,
        should_retry: Callable[[Exception], bool] = None,
    ) -> T:
        """
        Execute operation with retry logic.

        Args:
            operation: Async callable to execute
            operation_id: Identifier for tracking (optional)
            max_attempts: Override max attempts (optional)
            on_retry: Callback before each retry (optional)
            on_error: Callback on final failure (optional)
            should_retry: Custom function to determine if error is retryable

        Returns:
            Result from operation

        Raises:
            Last exception if all retries fail
        """
        max_attempts = max_attempts or self.config.max_attempts
        operation_id = operation_id or f"op_{id(operation)}"
        last_exception = None

        for attempt in range(1, max_attempts + 1):
            try:
                self._stats.total_attempts += 1
                result = await operation()

                # Success - reset failure count
                self._failure_counts[operation_id] = 0
                self._stats.successful_attempts += 1

                if attempt > 1:
                    logger.info(
                        f"Operation {operation_id} succeeded on attempt {attempt}"
                    )

                return result

            except Exception as e:
                last_exception = e
                self._stats.failed_attempts += 1
                self._failure_counts[operation_id] = attempt
                self._last_attempt[operation_id] = datetime.now().timestamp()
                self._stats.last_error = str(e)
                self._stats.last_error_time = datetime.now().timestamp()

                # Check if error is retryable
                is_retryable = self._is_retryable(e, should_retry)

                if not is_retryable:
                    logger.warning(
                        f"Operation {operation_id} failed with non-retryable error: {e}"
                    )
                    if on_error:
                        await on_error(e, attempt)
                    raise

                if attempt >= max_attempts:
                    logger.error(
                        f"Operation {operation_id} failed after {attempt} attempts: {e}"
                    )
                    if on_error:
                        await on_error(e, attempt)
                    raise

                # Calculate delay
                delay = self._calculate_delay(attempt)
                self._stats.retried_operations += 1

                logger.warning(
                    f"Operation {operation_id} failed (attempt {attempt}/{max_attempts}), "
                    f"retrying in {delay:.1f}s: {e}"
                )

                if on_retry:
                    await on_retry(e, attempt)

                await asyncio.sleep(delay)

        # Should not reach here, but just in case
        raise last_exception

    def _is_retryable(
        self,
        error: Exception,
        custom_check: Callable[[Exception], bool] = None,
    ) -> bool:
        """Determine if an error is retryable."""
        if custom_check:
            return custom_check(error)

        error_str = str(error).lower()

        # Check non-retryable patterns first
        for pattern in self.config.non_retryable_errors:
            if pattern.lower() in error_str:
                return False

        # Check retryable patterns
        for pattern in self.config.retryable_errors:
            if pattern.lower() in error_str:
                return True

        # Default: retry on connection/IO errors
        if isinstance(error, (ConnectionError, TimeoutError, OSError)):
            return True

        # Don't retry by default
        return False

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay with exponential backoff and optional jitter."""
        delay = self.config.initial_delay * (
            self.config.exponential_base ** (attempt - 1)
        )
        delay = min(delay, self.config.max_delay)

        if self.config.jitter:
            jitter_range = self.config.jitter_factor
            jitter = 1.0 + (random.random() * 2 - 1) * jitter_range
            delay = delay * jitter

        return max(0.1, delay)  # Minimum 100ms

    def get_failure_count(self, operation_id: str) -> int:
        """Get current failure count for an operation."""
        return self._failure_counts.get(operation_id, 0)

    def reset_failure_count(self, operation_id: str) -> None:
        """Reset failure count for an operation."""
        self._failure_counts.pop(operation_id, None)
        self._last_attempt.pop(operation_id, None)

    def get_stats(self) -> dict:
        """Get retry statistics."""
        return {
            "total_attempts": self._stats.total_attempts,
            "successful_attempts": self._stats.successful_attempts,
            "failed_attempts": self._stats.failed_attempts,
            "retried_operations": self._stats.retried_operations,
            "success_rate": (
                self._stats.successful_attempts / self._stats.total_attempts * 100
                if self._stats.total_attempts > 0
                else 0
            ),
            "last_error": self._stats.last_error,
            "last_error_time": self._stats.last_error_time,
        }


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5      # Failures before opening
    success_threshold: int = 2      # Successes in half-open to close
    reset_timeout: float = 60.0     # Seconds before half-open
    half_open_max_calls: int = 3    # Max concurrent calls in half-open


class CircuitBreaker:
    """
    Circuit breaker for Seedr API calls.
    Prevents cascade failures and allows graceful degradation.
    """

    def __init__(self, config: CircuitBreakerConfig = None, name: str = "default"):
        self.config = config or CircuitBreakerConfig()
        self.name = name
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

        # Statistics
        self._total_calls = 0
        self._rejected_calls = 0
        self._state_changes: List[tuple] = []  # (timestamp, old_state, new_state)

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (rejecting calls)."""
        return self._state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        return self._state == CircuitState.CLOSED

    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            self._total_calls += 1

            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                self._half_open_calls -= 1

                if self._success_count >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)

            elif self._state == CircuitState.CLOSED:
                # Reset failure count on success
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self._total_calls += 1
            self._failure_count += 1
            self._last_failure_time = datetime.now().timestamp()

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls -= 1
                # Any failure in half-open goes back to open
                self._transition_to(CircuitState.OPEN)

            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self.config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    async def can_execute(self) -> bool:
        """Check if a call is allowed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if reset timeout has passed
                elapsed = datetime.now().timestamp() - self._last_failure_time
                if elapsed >= self.config.reset_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    self._half_open_calls = 1
                    return True
                self._rejected_calls += 1
                return False

            if self._state == CircuitState.HALF_OPEN:
                # Allow limited calls in half-open
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                self._rejected_calls += 1
                return False

            return False

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state

        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
            logger.info(f"Circuit breaker '{self.name}' closed (normal operation)")

        elif new_state == CircuitState.OPEN:
            self._success_count = 0
            self._half_open_calls = 0
            logger.warning(
                f"Circuit breaker '{self.name}' opened "
                f"(failures: {self._failure_count}, "
                f"reset in {self.config.reset_timeout}s)"
            )

        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0
            logger.info(f"Circuit breaker '{self.name}' half-open (testing)")

        self._state_changes.append((
            datetime.now().timestamp(),
            old_state.value,
            new_state.value,
        ))

        # Keep only last 100 state changes
        if len(self._state_changes) > 100:
            self._state_changes = self._state_changes[-100:]

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        fallback: Callable[[], Awaitable[T]] = None,
    ) -> T:
        """
        Execute operation with circuit breaker protection.

        Args:
            operation: Async callable to execute
            fallback: Optional fallback if circuit is open

        Returns:
            Result from operation or fallback

        Raises:
            CircuitOpenError if circuit is open and no fallback
        """
        can_proceed = await self.can_execute()

        if not can_proceed:
            if fallback:
                logger.warning(
                    f"Circuit breaker '{self.name}' is open, using fallback"
                )
                return await fallback()
            raise CircuitOpenError(
                f"Circuit breaker '{self.name}' is open, "
                f"retry in {self.config.reset_timeout}s"
            )

        try:
            result = await operation()
            await self.record_success()
            return result
        except Exception as e:
            await self.record_failure()
            raise

    def get_stats(self) -> dict:
        """Get circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "total_calls": self._total_calls,
            "rejected_calls": self._rejected_calls,
            "last_failure_time": self._last_failure_time,
            "recent_state_changes": self._state_changes[-10:],
        }

    async def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        async with self._lock:
            self._transition_to(CircuitState.CLOSED)
            logger.info(f"Circuit breaker '{self.name}' manually reset")


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


class RateLimiter:
    """
    Token bucket rate limiter for API calls.
    Prevents overwhelming the Seedr API with too many requests.
    """

    def __init__(
        self,
        rate: float = 10.0,  # requests per second
        burst: int = 20,     # max burst size
    ):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_update = datetime.now().timestamp()
        self._lock = asyncio.Lock()
        self._total_requests = 0
        self._throttled_requests = 0

    async def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire a token, waiting if necessary.

        Args:
            timeout: Maximum time to wait for a token

        Returns:
            True if token acquired, False if timeout
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            async with self._lock:
                now = datetime.now().timestamp()
                elapsed = now - self._last_update
                self._last_update = now

                # Add tokens based on elapsed time
                self._tokens = min(
                    float(self.burst),
                    self._tokens + elapsed * self.rate
                )

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._total_requests += 1
                    return True

                # Calculate wait time
                wait_time = (1.0 - self._tokens) / self.rate

            # Check timeout
            if asyncio.get_event_loop().time() - start_time > timeout:
                self._throttled_requests += 1
                return False

            # Wait for tokens to refill
            await asyncio.sleep(min(wait_time, 0.1))

    def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        return {
            "rate_per_second": self.rate,
            "burst_size": self.burst,
            "available_tokens": self._tokens,
            "total_requests": self._total_requests,
            "throttled_requests": self._throttled_requests,
        }


class ResilientExecutor:
    """
    Combines retry logic and circuit breaker for resilient operations.
    """

    def __init__(
        self,
        retry_config: RetryConfig = None,
        circuit_config: CircuitBreakerConfig = None,
        name: str = "default",
    ):
        self.retry_handler = RetryHandler(retry_config)
        self.circuit_breaker = CircuitBreaker(circuit_config, name)
        self.name = name

    async def execute(
        self,
        operation: Callable[[], Awaitable[T]],
        operation_id: str = None,
        max_attempts: int = None,
        fallback: Callable[[], Awaitable[T]] = None,
        on_retry: Callable[[Exception, int], Awaitable] = None,
    ) -> T:
        """
        Execute operation with both retry and circuit breaker protection.

        Args:
            operation: Async callable to execute
            operation_id: Identifier for retry tracking
            max_attempts: Override max attempts
            fallback: Fallback if circuit is open
            on_retry: Callback before each retry

        Returns:
            Result from operation or fallback
        """
        # Check circuit breaker first
        can_proceed = await self.circuit_breaker.can_execute()

        if not can_proceed:
            if fallback:
                logger.warning(
                    f"Circuit '{self.name}' open, using fallback for {operation_id}"
                )
                return await fallback()
            raise CircuitOpenError(f"Circuit '{self.name}' is open")

        try:
            # Execute with retry
            result = await self.retry_handler.with_retry(
                operation=operation,
                operation_id=operation_id,
                max_attempts=max_attempts,
                on_retry=on_retry,
            )
            await self.circuit_breaker.record_success()
            return result

        except Exception as e:
            await self.circuit_breaker.record_failure()
            raise

    def get_stats(self) -> dict:
        """Get combined statistics."""
        return {
            "name": self.name,
            "retry": self.retry_handler.get_stats(),
            "circuit_breaker": self.circuit_breaker.get_stats(),
        }
