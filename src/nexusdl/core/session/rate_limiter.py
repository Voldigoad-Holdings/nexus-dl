"""Token-bucket rate limiting for HTTP requests.

Every source site has a rate ceiling. Some declare it (Cloudflare limits,
API quotas); most enforce it silently with a 429 or a soft block once a
pattern of requests exceeds what a human would plausibly produce. The
session hits a **single** site at a rate that respects that ceiling by
gating every request through a per-site :class:`RateLimiter`.

One limiter per site
--------------------
:class:`RateLimiter` is **not** a global singleton. The
:class:`~nexusdl.core.session.http_session.HttpSession` maintains one
instance per ``site_id`` — see ``HttpSession._rate_limiters`` — so a
download from a slow site never starves a download from a fast one. The
per-site ceiling comes from
:attr:`~nexusdl.core.models.site.SiteConfig.rate_limit` and is also
respected by the download queue's per-site concurrency limit
(``SiteConfig.max_concurrent_downloads``). The two limits are
complementary: the rate limiter caps requests over time, the queue caps
them in flight.

Algorithm — token bucket
------------------------
A bucket holds up to ``burst`` tokens and refills continuously at
``rate_per_second`` tokens per second:

* Every :meth:`RateLimiter.acquire` / :meth:`RateLimiter.try_acquire`
  consumes one token when available.
* When the bucket is empty, :meth:`acquire` computes the time until the
  next token is available and waits exactly that long (or until the
  caller-supplied timeout expires, whichever comes first).
* The bucket starts full: the first ``burst`` requests go through
  immediately, then subsequent ones are paced at ``rate_per_second``.
  This matches how a real browser behaves (a burst of parallel
  resources, then a steady trickle) and is the reason a burst capacity
  is exposed at all — a purely serialised limiter would throttle the
  first page's concurrent image fetches to ``1/rate_per_second``, which
  would be pathological for a 60-page chapter.

``rate_per_second`` and ``burst`` are independent. ``burst=1`` gives
strict pacing — every :meth:`acquire` after the first waits for the next
refill tick. ``burst=N`` where ``N > 1`` allows ``N`` back-to-back
requests before pacing kicks in. The default is
``max(1, ceil(rate_per_second))`` — one second of burst at the
configured rate, which is what a well-behaved client looks like to a
server that computes rate over one-second windows.

Wait loop and fairness
----------------------
:meth:`acquire` runs a bounded loop: check for a token, then sleep
exactly as long as needed for the next refill. This is the standard
"poll-with-computed-sleep" pattern, not a queue-based one. The trade-off
is that waiters are not strictly FIFO — when the sleepers wake, they
race for the lock and whichever wakes first consumes the token. In
practice this is effectively FIFO because :mod:`asyncio` orders timer
callbacks by deadline and, at equal deadlines, by insertion. For a
rate limiter whose typical rates are 0.5–10 requests per second, the
difference is irrelevant to throughput, and the code stays small,
correct, and debuggable.

A queue-based design (:class:`asyncio.Condition` + wakeups on refill)
would be strictly fairer at the cost of a notification mechanism that
must be woken on every refill tick — a timer per limiter that exists
even when no one is waiting, and that has to be cancelled on
``reset()`` and during shutdown. Not worth it.

Timeout semantics
-----------------
:meth:`acquire` takes an optional ``timeout`` (seconds):

* ``None`` — wait indefinitely (bounded by refill; the wait is always
  finite because ``rate_per_second > 0``).
* ``0`` — fail immediately if no token is available. Equivalent to
  ``if not await try_acquire(): raise ...`` but with a uniform error.
* Any positive value — wait at most ``timeout`` seconds, then raise
  :class:`~nexusdl.core.exceptions.RateLimitTimeoutError`.

Callers that want to *skip* rather than *fail* use
:meth:`try_acquire` — non-blocking, returns ``True`` / ``False``.
The two methods share the same bucket and are interchangeable at the
semantic level: ``try_acquire()`` is equivalent to
``acquire(timeout=0)`` modulo the exception.

Retryability
------------
:class:`~nexusdl.core.exceptions.RateLimitTimeoutError` is a transient
failure — retrying after a longer wait will succeed, and the retry loop
in :mod:`nexusdl.core.downloader.retry` is expected to treat it as
retryable. **Integration note**: that module's ``is_retryable`` currently
recognises :class:`~nexusdl.core.exceptions.SessionRateLimitError` but
not ``RateLimitTimeoutError``. ``exceptions.py`` should declare
``RateLimitTimeoutError`` as a **subclass** of
``SessionRateLimitError`` — the two share the same semantics (the site
throttled us, try again later) and subtyping is the smallest change that
makes the retry layer work without reopening it.

Thread safety
-------------
Every public method takes an :class:`asyncio.Lock`. The critical sections
are O(1) — one float comparison, one subtraction, one dict update. The
lock is therefore never contended in a way that matters: even a thousand
concurrent :meth:`try_acquire` calls complete in microseconds each.

:class:`RateLimiter` is **not** thread-safe across operating-system
threads. It is designed for a single :mod:`asyncio` event loop, which is
how the session uses it. A caller that genuinely needs to call it from
``asyncio.to_thread`` should wrap it in a ``threading.Lock`` at its own
boundary, not add one here.

Dependency graph (no cycles)::

    core.exceptions  ──►  core.session.rate_limiter
    loguru
    asyncio, math, time (stdlib)

No dependency on any other ``core.session.*`` module — the rate limiter
is a leaf. It could be lifted into a standalone library with only the
:class:`RateLimitTimeoutError` import replaced by a local exception.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from loguru import logger

from nexusdl.core.exceptions import RateLimitTimeoutError


__all__ = [
    "RateLimiter",
    "RateLimiterStats",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Minimum refill gap, in seconds, used to prevent a busy loop on the
#: pathological case where ``rate_per_second`` is so large that the
#: computed wait is smaller than the event loop's timer resolution. The
#: loop yields for at least this long when no token is available, even if
#: the mathematical wait is smaller. 1 ms is ten times finer than any
#: rate limiter needs — a limit of 1000 req/s still works — and coarse
#: enough to avoid scheduler thrash.
_MIN_WAIT_SECONDS: Final[float] = 0.001


_log = logger.bind(component="session.rate_limiter")


# --------------------------------------------------------------------------- #
# Stats                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RateLimiterStats:
    """Immutable snapshot of a :class:`RateLimiter`'s state.

    Returned by :meth:`RateLimiter.stats`. Useful for the diagnostic
    endpoint (``GET /api/v1/sites/{id}/health`` could include the
    current rate-limit state) and for tests that assert the limiter
    behaves as expected over a sequence of acquisitions.

    Attributes:
        rate_per_second: Configured refill rate.
        burst_capacity: Configured bucket capacity (``burst``).
        tokens_available: Current number of tokens, in ``[0, burst_capacity]``.
            A fractional value is normal — tokens accrue continuously.
        total_acquired: Cumulative count of successful acquisitions
            (both :meth:`RateLimiter.acquire` and
            :meth:`RateLimiter.try_acquire`). Reset only by constructing
            a new limiter.
        total_timeouts: Cumulative count of :meth:`RateLimiter.acquire`
            calls that gave up due to ``timeout``. Excludes timeouts
            inside :meth:`RateLimiter.try_acquire` (which never blocks).
        total_denied: Cumulative count of :meth:`RateLimiter.try_acquire`
            calls that returned ``False`` because no token was
            available.
    """

    rate_per_second: float
    burst_capacity: int
    tokens_available: float
    total_acquired: int
    total_timeouts: int
    total_denied: int


# --------------------------------------------------------------------------- #
# RateLimiter                                                                 #
# --------------------------------------------------------------------------- #


class RateLimiter:
    """A token-bucket rate limiter for a single source site.

    Stateless across instances (each one owns its own bucket and
    counters), asyncio-native, and safe to await concurrently from any
    number of coroutines.

    See the module docstring for the algorithm, the timeout semantics,
    and the fairness considerations.

    Example:
        >>> limiter = RateLimiter(rate_per_second=2.0, burst=4, name="md")
        >>> await limiter.acquire()              # immediate (bucket full)
        >>> await limiter.acquire()              # immediate
        >>> await limiter.acquire()              # immediate
        >>> await limiter.acquire()              # immediate
        >>> await limiter.acquire()              # waits ~0.5 s
        >>> if await limiter.try_acquire():
        ...     pass  # got a token
        >>> else:
        ...     pass  # no token — either back off or skip

    Args:
        rate_per_second: Sustained refill rate, in requests per second.
            Must be strictly positive.
        burst: Bucket capacity — the maximum number of back-to-back
            tokens that can be consumed before pacing kicks in. Must be
            ``>= 1``. When ``None``, defaults to
            ``max(1, ceil(rate_per_second))`` — one second of burst at
            the configured rate.
        name: Optional identifier (typically a ``site_id``) used in log
            lines and in :class:`RateLimitTimeoutError` messages. Empty
            by default.
        clock: Callable returning a monotonic float in seconds. Defaults
            to :func:`time.monotonic`. Injectable so that tests can
            advance time deterministically without sleeping.

    Raises:
        ValueError: If ``rate_per_second <= 0``, ``burst < 1``, or
            ``burst`` is present but not an integer. All three are
            programming errors at the composition root; failing at
            construction time makes them one-line fixes rather than
            subtle runtime surprises.
    """

    __slots__ = (
        "_acquired_count",
        "_burst",
        "_clock",
        "_denied_count",
        "_last_refill",
        "_lock",
        "_name",
        "_rate",
        "_timeout_count",
        "_tokens",
    )

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int | None = None,
        name: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError(
                f"rate_per_second must be > 0, got {rate_per_second!r}"
            )
        if burst is None:
            effective_burst = max(1, math.ceil(rate_per_second))
        else:
            if not isinstance(burst, int) or isinstance(burst, bool):
                raise TypeError(
                    f"burst must be an int or None, got "
                    f"{type(burst).__name__}"
                )
            if burst < 1:
                raise ValueError(
                    f"burst must be >= 1, got {burst}"
                )
            effective_burst = burst

        self._rate: float = float(rate_per_second)
        self._burst: int = effective_burst
        self._name: str = name
        self._clock: Callable[[], float] = clock
        self._lock = asyncio.Lock()

        # Bucket starts full.
        self._tokens: float = float(effective_burst)
        self._last_refill: float = self._clock()

        # Cumulative counters, exposed by ``stats()``.
        self._acquired_count: int = 0
        self._timeout_count: int = 0
        self._denied_count: int = 0

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def acquire(self, timeout: float | None = None) -> None:
        """Wait for a token and consume it.

        Returns as soon as a token is available. When the bucket is empty,
        sleeps exactly as long as needed for the next refill, then
        re-checks.

        Args:
            timeout: Maximum wait, in seconds. ``None`` waits
                indefinitely (the wait is still finite — refill is
                guaranteed by ``rate_per_second > 0``). ``0`` fails
                immediately if no token is available. Any positive
                value is an upper bound on the wait.

        Raises:
            ValueError: If ``timeout`` is negative.
            RateLimitTimeoutError: If ``timeout`` is set and expires
                before a token becomes available. The message names the
                limiter (when :attr:`_name` was provided) and the
                configured rate.
        """
        if timeout is not None and timeout < 0:
            raise ValueError(
                f"timeout must be >= 0 or None, got {timeout!r}"
            )

        deadline: float | None = (
            self._clock() + timeout if timeout is not None else None
        )

        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._acquired_count += 1
                    return
                # How long until the bucket has >= 1.0 token?
                wait_time = (1.0 - self._tokens) / self._rate

            # Outside the lock: honour the deadline, then sleep.
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    async with self._lock:
                        self._timeout_count += 1
                    raise RateLimitTimeoutError(
                        self._timeout_message(timeout)
                    )
                wait_time = min(wait_time, remaining)

            # Floor the sleep at a small positive value to avoid a tight
            # loop when ``rate_per_second`` is large enough that the
            # mathematical wait is below the event loop's timer
            # resolution. ``asyncio.sleep(0)`` would still yield, but
            # this documents the intent.
            await asyncio.sleep(max(wait_time, _MIN_WAIT_SECONDS))

    async def try_acquire(self) -> bool:
        """Attempt to consume a token without blocking.

        Returns immediately: no token wait, no timeout to honour. The
        caller decides what to do on ``False`` (retry after a delay,
        skip, surface a "rate limited" response, …).

        Returns:
            ``True`` iff a token was consumed, ``False`` otherwise.
        """
        async with self._lock:
            self._refill_locked()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._acquired_count += 1
                return True
            self._denied_count += 1
            return False

    async def reset(self) -> None:
        """Refill the bucket to ``burst`` tokens.

        Cumulative counters are preserved — this is a "top up the bucket"
        operation, not a full reset. Intended for tests and for an
        explicit "the site just told us to back off for a while, then
        go again" flow (though the standard behaviour — refill at
        ``rate_per_second`` — already handles the normal case).

        Does **not** cancel pending :meth:`acquire` waiters. A waiter
        that was sleeping for 0.5 s when :meth:`reset` fires will still
        sleep 0.5 s, then wake to find the bucket full — one call gets
        through immediately, the rest go back to pacing. Correct, if not
        optimal.
        """
        async with self._lock:
            self._tokens = float(self._burst)
            self._last_refill = self._clock()

    async def stats(self) -> RateLimiterStats:
        """Return an immutable snapshot of the limiter's current state.

        Refills the bucket before reading so that ``tokens_available``
        reflects the true state at the moment of the call, not the last
        refill tick.

        Returns:
            A :class:`RateLimiterStats` populated from the current state
            of the bucket and the cumulative counters.
        """
        async with self._lock:
            self._refill_locked()
            return RateLimiterStats(
                rate_per_second=self._rate,
                burst_capacity=self._burst,
                tokens_available=self._tokens,
                total_acquired=self._acquired_count,
                total_timeouts=self._timeout_count,
                total_denied=self._denied_count,
            )

    # ------------------------------------------------------------------ #
    # Read-only properties                                               #
    # ------------------------------------------------------------------ #

    @property
    def rate_per_second(self) -> float:
        """The configured sustained refill rate."""
        return self._rate

    @property
    def burst(self) -> int:
        """The configured bucket capacity."""
        return self._burst

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"RateLimiter(name={self._name!r}, "
            f"rate={self._rate}/s, burst={self._burst})"
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _refill_locked(self) -> None:
        """Refill the bucket based on elapsed time. Holds :attr:`_lock`.

        Uses :attr:`_clock` so that injected clocks advance consistently
        across tests. The refill is capped at :attr:`_burst` — a bucket
        that was idle for an hour is still at most ``burst`` tokens, not
        ``3600 * rate_per_second`` tokens.
        """
        now = self._clock()
        elapsed = now - self._last_refill
        if elapsed <= 0.0:
            # Guard against a non-monotonic clock (should not happen
            # with ``time.monotonic``, but the clock is injectable and a
            # caller could supply something that regresses). Skip the
            # refill rather than adding negative tokens.
            return
        self._tokens = min(
            float(self._burst),
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

    def _timeout_message(self, timeout: float | None) -> str:
        """Build the :class:`RateLimitTimeoutError` message.

        Includes the limiter's ``name`` when one was provided, and the
        configured rate so an operator can tell at a glance whether the
        rate is misconfigured for the site.

        Args:
            timeout: The timeout value the caller passed, used verbatim
                in the message. ``None`` cannot reach this method (only
                ``acquire`` with a deadline raises), but the signature
                is defensive — a future code path could raise a timeout
                for a different reason.

        Returns:
            A human-readable message.
        """
        base = (
            f"rate limit timeout after {timeout:.3f}s "
            f"(rate={self._rate}/s, burst={self._burst})"
            if timeout is not None
            else f"rate limit timeout (rate={self._rate}/s, burst={self._burst})"
        )
        if self._name:
            base = f"{base} [{self._name}]"
        return base
