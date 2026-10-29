# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for CircuitBreaker.

CircuitBreaker isolates failing sites:

    - CLOSED (normal): requests pass through
    - OPEN (failing): requests are blocked
    - HALF_OPEN (recovering): 1 request allowed to test
    - Transitions: CLOSED → OPEN after N failures
    - Transitions: OPEN → HALF_OPEN after timeout
    - Transitions: HALF_OPEN → CLOSED on success
    - Transitions: HALF_OPEN → OPEN on failure

This prevents "thundering herd" and lets failing sites recover.

What is tested:
    - Initial state (CLOSED)
    - record_success() resets counter
    - record_failure() increments counter
    - CLOSED → OPEN after failure_threshold
    - OPEN: allow_request() returns False
    - OPEN → HALF_OPEN after recovery_timeout
    - HALF_OPEN: 1 request allowed
    - HALF_OPEN → CLOSED on success
    - HALF_OPEN → OPEN on failure
    - reset() forces CLOSED
    - force_open() forces OPEN
    - state property
    - failure_count property
    - Stats (total_failures, total_successes)
    - Concurrency (asyncio.Lock)
    - Multi-site isolation
    - Invalid init (failure_threshold=0, recovery_timeout=0)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from nexusdl.core.exceptions import CircuitBreakerOpenError
from nexusdl.core.session.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CircuitStats,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def breaker() -> CircuitBreaker:
    """Default CircuitBreaker (3 failures, 0.2s recovery)."""
    return CircuitBreaker(
        name="test-site",
        failure_threshold=3,
        recovery_timeout_seconds=0.2,
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: Initial state
# ────────────────────────────────────────────────────────────────────


def test_initial_state(breaker: CircuitBreaker) -> None:
    """Initial state is CLOSED."""
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_initial_allow_request(breaker: CircuitBreaker) -> None:
    """CLOSED allows requests."""
    assert breaker.allow_request() is True


# ────────────────────────────────────────────────────────────────────
# Test 2: record_success / record_failure
# ────────────────────────────────────────────────────────────────────


def test_record_success_resets_counter(breaker: CircuitBreaker) -> None:
    """record_success() resets failure_count."""
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.failure_count == 2

    breaker.record_success()
    assert breaker.failure_count == 0
    assert breaker.state == CircuitState.CLOSED


def test_record_failure_increments_counter(breaker: CircuitBreaker) -> None:
    """record_failure() increments failure_count."""
    breaker.record_failure()
    assert breaker.failure_count == 1
    breaker.record_failure()
    assert breaker.failure_count == 2


# ────────────────────────────────────────────────────────────────────
# Test 3: CLOSED → OPEN
# ────────────────────────────────────────────────────────────────────


def test_closed_to_open(breaker: CircuitBreaker) -> None:
    """After failure_threshold failures, state becomes OPEN."""
    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False


def test_open_blocks_requests(breaker: CircuitBreaker) -> None:
    """OPEN blocks all requests."""
    for _ in range(3):
        breaker.record_failure()

    assert breaker.allow_request() is False
    assert breaker.allow_request() is False  # still blocked


# ────────────────────────────────────────────────────────────────────
# Test 4: OPEN → HALF_OPEN
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_to_half_open(breaker: CircuitBreaker) -> None:
    """After recovery_timeout, OPEN → HALF_OPEN."""
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    # Wait for recovery timeout
    await asyncio.sleep(0.25)

    # Should transition to HALF_OPEN
    assert breaker.allow_request() is True
    assert breaker.state == CircuitState.HALF_OPEN


# ────────────────────────────────────────────────────────────────────
# Test 5: HALF_OPEN behavior
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_half_open_allows_one_request(breaker: CircuitBreaker) -> None:
    """HALF_OPEN allows exactly ONE request."""
    for _ in range(3):
        breaker.record_failure()

    await asyncio.sleep(0.25)

    # First request: allowed
    assert breaker.allow_request() is True
    assert breaker.state == CircuitState.HALF_OPEN

    # Second request: blocked (only one test request allowed)
    assert breaker.allow_request() is False


@pytest.mark.asyncio
async def test_half_open_to_closed_on_success(breaker: CircuitBreaker) -> None:
    """HALF_OPEN → CLOSED on success."""
    for _ in range(3):
        breaker.record_failure()

    await asyncio.sleep(0.25)
    breaker.allow_request()  # enters HALF_OPEN
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


@pytest.mark.asyncio
async def test_half_open_to_open_on_failure(breaker: CircuitBreaker) -> None:
    """HALF_OPEN → OPEN on failure."""
    for _ in range(3):
        breaker.record_failure()

    await asyncio.sleep(0.25)
    breaker.allow_request()  # enters HALF_OPEN
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


# ────────────────────────────────────────────────────────────────────
# Test 6: reset() / force_open()
# ────────────────────────────────────────────────────────────────────


def test_reset(breaker: CircuitBreaker) -> None:
    """reset() forces CLOSED."""
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    breaker.reset()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_force_open(breaker: CircuitBreaker) -> None:
    """force_open() forces OPEN."""
    assert breaker.state == CircuitState.CLOSED
    breaker.force_open()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False


# ────────────────────────────────────────────────────────────────────
# Test 7: Context manager
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_manager_success(breaker: CircuitBreaker) -> None:
    """Context manager records success on clean exit."""
    try:
        async with breaker:
            pass
    except CircuitBreakerOpenError:
        pytest.fail("Should not raise")

    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


@pytest.mark.asyncio
async def test_context_manager_failure(breaker: CircuitBreaker) -> None:
    """Context manager records failure on exception."""
    with pytest.raises(ValueError):
        async with breaker:
            raise ValueError("boom")

    assert breaker.failure_count == 1


@pytest.mark.asyncio
async def test_context_manager_blocked(breaker: CircuitBreaker) -> None:
    """Context manager raises CircuitBreakerOpenError when OPEN."""
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    with pytest.raises(CircuitBreakerOpenError):
        async with breaker:
            pass


# ────────────────────────────────────────────────────────────────────
# Test 8: Stats
# ────────────────────────────────────────────────────────────────────


def test_stats(breaker: CircuitBreaker) -> None:
    """Stats track total failures and successes."""
    stats = breaker.stats()
    assert isinstance(stats, CircuitStats)
    assert stats.total_failures == 0
    assert stats.total_successes == 0

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_success()

    stats = breaker.stats()
    assert stats.total_failures == 2
    assert stats.total_successes == 2


# ────────────────────────────────────────────────────────────────────
# Test 9: Concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_failures(breaker: CircuitBreaker) -> None:
    """Concurrent record_failure() is thread-safe."""

    async def _fail() -> None:
        breaker.record_failure()

    await asyncio.gather(*[_fail() for _ in range(10)])

    # After 10 failures, state should be OPEN (threshold 3)
    assert breaker.state == CircuitState.OPEN
    assert breaker.failure_count >= 3


@pytest.mark.asyncio
async def test_concurrent_allow_request(breaker: CircuitBreaker) -> None:
    """Concurrent allow_request() respects HALF_OPEN single-request rule."""
    for _ in range(3):
        breaker.record_failure()
    await asyncio.sleep(0.25)

    results = await asyncio.gather(
        *[asyncio.to_thread(breaker.allow_request) for _ in range(10)]
    )

    # Exactly one should be True (HALF_OPEN allows 1)
    assert results.count(True) == 1


# ────────────────────────────────────────────────────────────────────
# Test 10: Multi-site isolation
# ────────────────────────────────────────────────────────────────────


def test_multi_site_isolation() -> None:
    """Two CircuitBreakers are independent."""
    b1 = CircuitBreaker(name="site1", failure_threshold=3, recovery_timeout_seconds=0.2)
    b2 = CircuitBreaker(name="site2", failure_threshold=3, recovery_timeout_seconds=0.2)

    # Fail b1
    for _ in range(3):
        b1.record_failure()

    assert b1.state == CircuitState.OPEN
    assert b2.state == CircuitState.CLOSED  # b2 is not affected
    assert b2.allow_request() is True


# ────────────────────────────────────────────────────────────────────
# Test 11: Invalid init
# ────────────────────────────────────────────────────────────────────


def test_invalid_failure_threshold() -> None:
    """failure_threshold=0 raises ValueError."""
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker(name="test", failure_threshold=0, recovery_timeout_seconds=0.2)


def test_invalid_recovery_timeout() -> None:
    """recovery_timeout_seconds=0 raises ValueError."""
    with pytest.raises(ValueError, match="recovery_timeout"):
        CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_seconds=0)


# ────────────────────────────────────────────────────────────────────
# Test 12: State property
# ────────────────────────────────────────────────────────────────────


def test_state_property(breaker: CircuitBreaker) -> None:
    """state property returns the current CircuitState."""
    assert breaker.state == CircuitState.CLOSED

    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == CircuitState.OPEN


def test_failure_count_property(breaker: CircuitBreaker) -> None:
    """failure_count property returns the current count."""
    assert breaker.failure_count == 0

    breaker.record_failure()
    breaker.record_failure()

    assert breaker.failure_count == 2
