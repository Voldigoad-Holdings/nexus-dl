# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for core.utils.retry.

Retry helpers built on `tenacity`:

    - with_retry (async, exponential backoff + jitter)
    - with_retry_sync (sync equivalent)
    - RetryPolicy (dataclass config)
    - RetryStats (attempts, total_delay, last_error)
    - is_retryable_http (5xx, 429, 408)
    - is_retryable_exception (network errors)
    - RetryError, RetryTimeoutError (exceptions)

What is tested:
    - with_retry success (no retry)
    - with_retry retry (succeeds on 2nd attempt)
    - with_retry exhaustion (raises RetryError)
    - with_retry custom retry_on
    - with_retry no retry_on (raises immediately)
    - with_retry timeout (raises RetryTimeoutError)
    - with_retry_sync
    - on_retry callback
    - RetryPolicy validation
    - RetryStats
    - is_retryable_http
    - is_retryable_exception
    - CancelledError propagation
    - Concurrency (no shared state)
"""
from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from nexusdl.core.exceptions import RetryError, RetryTimeoutError
from nexusdl.core.utils.retry import (
    RetryPolicy,
    RetryStats,
    is_retryable_exception,
    is_retryable_http,
    with_retry,
    with_retry_sync,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def policy() -> RetryPolicy:
    """Fast RetryPolicy for tests (no long sleeps)."""
    return RetryPolicy(
        max_attempts=3,
        backoff_base=0.01,
        backoff_factor=2.0,
        jitter=0.1,
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: Success (no retry)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_immediate(policy: RetryPolicy) -> None:
    """with_retry() returns immediately on success."""
    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        return "ok"

    result = await with_retry(_func, policy=policy)
    assert result == "ok"
    assert call_count["n"] == 1


# ────────────────────────────────────────────────────────────────────
# Test 2: Retry on failure
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_2nd(policy: RetryPolicy) -> None:
    """with_retry() succeeds on 2nd attempt after 1 failure."""
    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("first fail")
        return "ok"

    result = await with_retry(
        _func,
        policy=policy,
        retry_on=(ValueError,),
    )
    assert result == "ok"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_with_retry_exhausted(policy: RetryPolicy) -> None:
    """with_retry() raises RetryError after max_attempts."""
    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        raise ValueError("always fail")

    with pytest.raises(RetryError):
        await with_retry(
            _func,
            policy=policy,
            retry_on=(ValueError,),
        )

    assert call_count["n"] == policy.max_attempts


# ────────────────────────────────────────────────────────────────────
# Test 3: Custom retry_on
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_custom_exception(policy: RetryPolicy) -> None:
    """with_retry(retry_on=(SpecificError,)) retries on that error only."""
    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("match")
        return "ok"

    result = await with_retry(
        _func,
        policy=policy,
        retry_on=(ValueError,),
    )
    assert result == "ok"


@pytest.mark.asyncio
async def test_with_retry_no_match(policy: RetryPolicy) -> None:
    """with_retry(retry_on=(SpecificError,)) does NOT retry other errors."""
    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        raise TypeError("not in retry_on")

    with pytest.raises(TypeError):
        await with_retry(
            _func,
            policy=policy,
            retry_on=(ValueError,),
        )

    assert call_count["n"] == 1  # no retry


@pytest.mark.asyncio
async def test_with_retry_empty_retry_on(policy: RetryPolicy) -> None:
    """with_retry(retry_on=()) does not retry anything."""
    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        raise ValueError("fail")

    with pytest.raises(ValueError):
        await with_retry(
            _func,
            policy=policy,
            retry_on=(),
        )

    assert call_count["n"] == 1


# ────────────────────────────────────────────────────────────────────
# Test 4: Timeout
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_timeout() -> None:
    """with_retry(timeout=X) raises RetryTimeoutError."""
    policy = RetryPolicy(
        max_attempts=10,
        backoff_base=0.1,
        backoff_factor=2.0,
        jitter=0.0,
    )

    async def _func() -> str:
        raise ValueError("always fail")

    with pytest.raises(RetryTimeoutError):
        await with_retry(
            _func,
            policy=policy,
            retry_on=(ValueError,),
            timeout=0.15,
        )


# ────────────────────────────────────────────────────────────────────
# Test 5: Sync version
# ────────────────────────────────────────────────────────────────────


def test_with_retry_sync_success(policy: RetryPolicy) -> None:
    """with_retry_sync() works synchronously."""
    call_count = {"n": 0}

    def _func() -> str:
        call_count["n"] += 1
        return "ok"

    result = with_retry_sync(_func, policy=policy)
    assert result == "ok"
    assert call_count["n"] == 1


def test_with_retry_sync_retries(policy: RetryPolicy) -> None:
    """with_retry_sync() retries on failure."""
    call_count = {"n": 0}

    def _func() -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("fail")
        return "ok"

    result = with_retry_sync(
        _func,
        policy=policy,
        retry_on=(ValueError,),
    )
    assert result == "ok"
    assert call_count["n"] == 2


# ────────────────────────────────────────────────────────────────────
# Test 6: on_retry callback
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_retry_callback(policy: RetryPolicy) -> None:
    """on_retry() is called at each retry."""
    retries: list[tuple[int, Exception]] = []

    def _on_retry(attempt: int, exc: Exception) -> None:
        retries.append((attempt, exc))

    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ValueError(f"fail {call_count['n']}")
        return "ok"

    result = await with_retry(
        _func,
        policy=policy,
        retry_on=(ValueError,),
        on_retry=_on_retry,
    )
    assert result == "ok"
    assert len(retries) == 2  # 2 retries before success


# ────────────────────────────────────────────────────────────────────
# Test 7: RetryPolicy validation
# ────────────────────────────────────────────────────────────────────


def test_policy_defaults() -> None:
    """RetryPolicy has sensible defaults."""
    p = RetryPolicy()
    assert p.max_attempts >= 1
    assert p.backoff_base >= 0
    assert 0 <= p.jitter <= 1


def test_policy_invalid_max_attempts() -> None:
    """max_attempts < 1 raises ValueError."""
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


def test_policy_invalid_jitter() -> None:
    """jitter outside [0, 1] raises ValueError."""
    with pytest.raises(ValueError):
        RetryPolicy(jitter=1.5)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=-0.1)


# ────────────────────────────────────────────────────────────────────
# Test 8: RetryStats
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats(policy: RetryPolicy) -> None:
    """RetryStats tracks attempts, total_delay, last_error."""
    stats = RetryStats()
    assert stats.attempts == 0
    assert stats.total_delay == 0.0
    assert stats.last_error is None

    call_count = {"n": 0}

    async def _func() -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("fail")
        return "ok"

    await with_retry(
        _func,
        policy=policy,
        retry_on=(ValueError,),
        stats=stats,
    )

    assert stats.attempts >= 1
    assert stats.total_delay >= 0


# ────────────────────────────────────────────────────────────────────
# Test 9: is_retryable_http
# ────────────────────────────────────────────────────────────────────


def test_retryable_http_5xx() -> None:
    """5xx status codes are retryable."""
    for code in (500, 502, 503, 504):
        assert is_retryable_http(code) is True


def test_retryable_http_429() -> None:
    """429 is retryable."""
    assert is_retryable_http(429) is True


def test_retryable_http_408() -> None:
    """408 is retryable."""
    assert is_retryable_http(408) is True


def test_retryable_http_4xx() -> None:
    """4xx (except 408, 429) is NOT retryable."""
    for code in (400, 401, 403, 404, 410):
        assert is_retryable_http(code) is False


def test_retryable_http_2xx() -> None:
    """2xx is not retryable (success)."""
    for code in (200, 201, 204, 301, 302):
        assert is_retryable_http(code) is False


# ────────────────────────────────────────────────────────────────────
# Test 10: is_retryable_exception
# ────────────────────────────────────────────────────────────────────


def test_retryable_exception_timeout() -> None:
    """TimeoutError is retryable."""
    assert is_retryable_exception(TimeoutError("timeout")) is True


def test_retryable_exception_connect() -> None:
    """ConnectError is retryable."""
    import httpx

    assert is_retryable_exception(httpx.ConnectError("refused")) is True


def test_retryable_exception_read() -> None:
    """ReadTimeout is retryable."""
    import httpx

    assert is_retryable_exception(httpx.ReadTimeout("timeout")) is True


def test_retryable_exception_value_error() -> None:
    """ValueError is not retryable."""
    assert is_retryable_exception(ValueError("bad")) is False


def test_retryable_exception_runtime_error() -> None:
    """RuntimeError is not retryable."""
    assert is_retryable_exception(RuntimeError("bad")) is False


# ────────────────────────────────────────────────────────────────────
# Test 11: CancelledError propagation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_error_propagates() -> None:
    """CancelledError is NOT caught by with_retry()."""
    async def _func() -> str:
        raise asyncio.CancelledError("cancelled")

    with pytest.raises(asyncio.CancelledError):
        await with_retry(
            _func,
            retry_on=(ValueError,),  # CancelledError not in retry_on
        )


# ────────────────────────────────────────────────────────────────────
# Test 12: Concurrency (no shared state)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_retries(policy: RetryPolicy) -> None:
    """Concurrent with_retry() calls do not share state."""

    async def _func(i: int) -> int:
        if i % 2 == 0:
            raise ValueError(f"fail {i}")
        return i

    results = await asyncio.gather(
        *[
            with_retry(
                lambda i=i: _func(i),
                policy=policy,
                retry_on=(ValueError,),
            )
            for i in range(5)
        ],
        return_exceptions=True,
    )

    # Some succeed, some fail (after retries)
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) >= 1
    assert len(failures) >= 1
