# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for RateLimiter.

RateLimiter is an async **token bucket**:

    - `rate_per_second` tokens are added per second
    - `burst` is the max tokens in the bucket
    - `acquire()` blocks until a token is available
    - `try_acquire()` returns immediately (bool)
    - `timeout` raises `RateLimitTimeoutError` if exceeded

Contract:
    - rate_per_second > 0
    - burst >= 1
    - acquire() (async, blocks)
    - try_acquire() (sync, non-blocking)
    - stats() -> RateLimitStats (tokens_available, total_acquired, total_waited)
    - reset() (empties the bucket and counters)
    - Concurrency-safe (asyncio.Lock)
    - One limiter per site (no shared state)

What is tested:
    - First acquire (immediate, burst)
    - Second acquire (waits 1/rate)
    - N acquires (waits N/rate)
    - Burst (N immediate, then wait)
    - Timeout (raises RateLimitTimeoutError)
    - try_acquire (non-blocking)
    - Stats
    - reset
    - Concurrency
    - Multi-site isolation
    - Invalid init (rate=0, burst=0)
    - Refill continuity
    - Refill cap
"""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from nexusdl.core.exceptions import RateLimitTimeoutError
from nexusdl.core.session.rate_limiter import RateLimiter, RateLimitStats


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def limiter() -> RateLimiter:
    """RateLimiter at 10 req/s with burst 2."""
    return RateLimiter(rate_per_second=10.0, burst=2)


# ────────────────────────────────────────────────────────────────────
# Test 1: First acquire (immediate)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_acquire_immediate(limiter: RateLimiter) -> None:
    """First acquire is immediate (burst)."""
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed < 0.05, f"First acquire took {elapsed:.3f}s (expected < 0.05s)"


@pytest.mark.asyncio
async def test_burst_allows_n_immediate(limiter: RateLimiter) -> None:
    """Burst allows N immediate acquires."""
    start = time.monotonic()
    for _ in range(2):  # burst = 2
        await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, f"Burst of 2 took {elapsed:.3f}s (expected < 0.1s)"


# ────────────────────────────────────────────────────────────────────
# Test 2: Second acquire (waits 1/rate)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_acquire_waits(limiter: RateLimiter) -> None:
    """Second acquire after burst waits 1/rate."""
    # Exhaust burst
    await limiter.acquire()
    await limiter.acquire()

    # Next acquire should wait ~100ms (1/10s)
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert 0.05 <= elapsed <= 0.2, f"Wait was {elapsed:.3f}s (expected ~0.1s)"


# ────────────────────────────────────────────────────────────────────
# Test 3: N acquires
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n_acquires_total_time() -> None:
    """N acquires at rate R take approximately (N-burst)/R seconds."""
    # 5 req/s, burst 1 → 5 acquires should take ~0.8s
    limiter = RateLimiter(rate_per_second=5.0, burst=1)

    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - start

    # (5-1) / 5 = 0.8s minimum, allow up to 1.5s for CI variance
    assert 0.6 <= elapsed <= 1.5, f"5 acquires took {elapsed:.3f}s (expected ~0.8s)"


# ────────────────────────────────────────────────────────────────────
# Test 4: Timeout
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_raises() -> None:
    """acquire(timeout=X) raises RateLimitTimeoutError if exceeded."""
    # 1 req/s, burst 1, timeout 100ms → 2nd acquire times out
    limiter = RateLimiter(rate_per_second=1.0, burst=1)

    await limiter.acquire()  # consumes the only token

    with pytest.raises(RateLimitTimeoutError):
        await limiter.acquire(timeout=0.1)


@pytest.mark.asyncio
async def test_timeout_sufficient() -> None:
    """acquire(timeout=X) with sufficient X succeeds."""
    limiter = RateLimiter(rate_per_second=10.0, burst=1)

    await limiter.acquire()  # consumes the only token

    # 100ms wait should be enough for 1/10s
    start = time.monotonic()
    await limiter.acquire(timeout=1.0)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5


# ────────────────────────────────────────────────────────────────────
# Test 5: try_acquire (non-blocking)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_acquire_available(limiter: RateLimiter) -> None:
    """try_acquire() returns True if a token is available."""
    assert limiter.try_acquire() is True


@pytest.mark.asyncio
async def test_try_acquire_unavailable(limiter: RateLimiter) -> None:
    """try_acquire() returns False if no token available."""
    # Exhaust burst
    limiter.try_acquire()
    limiter.try_acquire()

    # No token left
    assert limiter.try_acquire() is False


@pytest.mark.asyncio
async def test_try_acquire_immediate(limiter: RateLimiter) -> None:
    """try_acquire() is immediate (non-blocking)."""
    start = time.monotonic()
    limiter.try_acquire()
    limiter.try_acquire()
    limiter.try_acquire()  # no more tokens
    elapsed = time.monotonic() - start

    assert elapsed < 0.01, f"try_acquire was blocking ({elapsed:.3f}s)"


# ────────────────────────────────────────────────────────────────────
# Test 6: Stats
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats(limiter: RateLimiter) -> None:
    """stats() tracks tokens, acquisitions, and waits."""
    stats = limiter.stats()
    assert isinstance(stats, RateLimitStats)
    assert stats.total_acquired == 0
    assert stats.tokens_available >= 0

    await limiter.acquire()
    stats = limiter.stats()
    assert stats.total_acquired == 1


@pytest.mark.asyncio
async def test_stats_tokens_available(limiter: RateLimiter) -> None:
    """stats().tokens_available reflects the current bucket."""
    stats = limiter.stats()
    initial = stats.tokens_available

    await limiter.acquire()
    stats = limiter.stats()
    assert stats.tokens_available < initial


# ────────────────────────────────────────────────────────────────────
# Test 7: reset()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset(limiter: RateLimiter) -> None:
    """reset() empties the bucket and resets counters."""
    await limiter.acquire()
    await limiter.acquire()

    limiter.reset()

    stats = limiter.stats()
    assert stats.total_acquired == 0
    assert stats.tokens_available >= 0


# ────────────────────────────────────────────────────────────────────
# Test 8: Concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_acquire(limiter: RateLimiter) -> None:
    """Concurrent acquires are serialized correctly."""
    # 3 concurrent acquires at 10 req/s, burst 2
    async def _acquire() -> None:
        await limiter.acquire()

    start = time.monotonic()
    await asyncio.gather(*[_acquire() for _ in range(3)])
    elapsed = time.monotonic() - start

    # 3 acquires, burst 2 → ~1 wait of 100ms
    assert 0.05 <= elapsed <= 0.3


@pytest.mark.asyncio
async def test_concurrent_try_acquire(limiter: RateLimiter) -> None:
    """try_acquire() is thread-safe."""
    results = await asyncio.gather(
        *[asyncio.to_thread(limiter.try_acquire) for _ in range(10)]
    )

    # At most burst (2) should succeed
    assert results.count(True) <= 2


# ────────────────────────────────────────────────────────────────────
# Test 9: Multi-site isolation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_site_isolation() -> None:
    """Two RateLimiters are independent."""
    l1 = RateLimiter(rate_per_second=1.0, burst=1)
    l2 = RateLimiter(rate_per_second=1.0, burst=1)

    # Exhaust l1
    await l1.acquire()
    assert l1.try_acquire() is False

    # l2 should still have its token
    assert l2.try_acquire() is True


# ────────────────────────────────────────────────────────────────────
# Test 10: Invalid init
# ────────────────────────────────────────────────────────────────────


def test_invalid_rate() -> None:
    """rate_per_second=0 raises ValueError."""
    with pytest.raises(ValueError, match="rate"):
        RateLimiter(rate_per_second=0, burst=1)


def test_invalid_burst() -> None:
    """burst=0 raises ValueError."""
    with pytest.raises(ValueError, match="burst"):
        RateLimiter(rate_per_second=1.0, burst=0)


# ────────────────────────────────────────────────────────────────────
# Test 11: Refill continuity
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refill_continuity() -> None:
    """Tokens refill continuously (not just at boundaries)."""
    limiter = RateLimiter(rate_per_second=10.0, burst=10)

    # Consume all tokens
    for _ in range(10):
        await limiter.acquire()

    # Wait 150ms → ~1.5 tokens should be available
    await asyncio.sleep(0.15)

    assert limiter.try_acquire() is True  # 1 token


@pytest.mark.asyncio
async def test_refill_cap() -> None:
    """Tokens do not exceed burst."""
    limiter = RateLimiter(rate_per_second=100.0, burst=2)

    # Consume all tokens
    await limiter.acquire()
    await limiter.acquire()

    # Wait long enough to refill 10 tokens (would exceed burst)
    await asyncio.sleep(0.1)

    stats = limiter.stats()
    assert stats.tokens_available <= 2
