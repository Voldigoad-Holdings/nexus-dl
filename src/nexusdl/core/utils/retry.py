"""Generic retry primitives usable anywhere in NexusDL.

Two retry modules already exist in the codebase with **different scopes**,
and this is the third member of the family — the **generic primitives**
that any caller can use without inheriting a domain-specific policy:

* :mod:`nexusdl.core.downloader.retry` — download-pipeline-specific. It
  classifies the session and parser exceptions of the downloader
  (``SessionTimeoutError`` retryable, ``ParserParseError`` not, …) and
  builds the tenacity controller the worker uses. It **does not** impose
  a wall-clock budget — the downloader's retry policy is bounded by
  attempt count only.
* **This module** — domain-agnostic. It knows nothing about source
  sites, download tasks, or which exceptions are retryable. The caller
  supplies a ``retry_on`` predicate (or accepts the "retry all
  ``Exception`` subclasses" default) and, when a wall-clock budget is
  needed, sets ``max_elapsed_seconds`` — which this module **does**
  enforce, raising :class:`~nexusdl.core.exceptions.RetryTimeoutError`
  when the budget is exceeded.
* :mod:`tenacity` — the third-party library underneath both. Neither
  module exposes it to callers; if you need tenacity's full feature set
  (custom stop/wait combinations, `before_log`/`after_log` hooks, retry
  statistics), reach for it directly.

When to use which
-----------------
Use **this module** when:

* Retrying a database operation, a Playwright launch, a FlareSolverr
  call, a filesystem operation, a test helper — anything that is *not*
  the download pipeline.
* You want a **wall-clock budget** ("retry for up to 30 s, then give
  up with a specific error").
* You want a **one-liner** — ``await retry_async(fetch, policy=...)``
  — instead of constructing a tenacity controller.

Use **``core.downloader.retry``** when:

* The caller is part of the download pipeline and wants the shipped
  exception classification (``SessionTimeoutError`` retryable,
  ``ConfigError`` not).
* You want the tenacity ``async for attempt in ...: with attempt: ...``
  pattern with the downloader's policy shape.

Use **tenacity directly** when you need finer control than either
module exposes.

Design decisions
----------------
* **Function-style API.** ``retry_async(op)`` is a plain coroutine — no
  decorator machinery, no context-manager ceremony, no wrapper types.
  Most callers want "run this operation, retry if it fails, return the
  result or raise the final exception," and this module does exactly
  that.
* **Both async and sync.** The async variant is the primary one, but
  :func:`retry_sync` covers the small number of call sites that are not
  in an event loop (scripts, tests, CLI helpers running before the
  event loop starts).
* **Backoff is a callable Protocol, not a closed enum.** The four
  factories (:func:`constant_backoff`, :func:`linear_backoff`,
  :func:`exponential_backoff`, :func:`exponential_jitter_backoff`)
  cover every case in the pipeline, and a caller with a custom strategy
  implements ``__call__(attempt) -> float`` directly — no
  ``register_custom_backoff`` registry, no subclass of an ABC, no
  ``isinstance`` check.
* **``retry_on`` is a predicate.** No exception classification is
  hardcoded. The default ``None`` means "retry any ``Exception``
  subclass"; a caller whose operation raises a mix of transient and
  permanent exceptions supplies ``retry_on=lambda e: isinstance(e,
  TransientError)``. This inverts the classification — the caller knows
  their exceptions, the retry loop does not.
* **``RetryTimeoutError`` is the only exception this module raises for
  its own reasons.** Everything else (the operation's exceptions, an
  early-giveup signal) is either re-raised unchanged or wrapped in
  ``RetryTimeoutError`` with the last error chained as ``__cause__``.
* **Cancellation is untouched.** ``asyncio.CancelledError`` is a
  ``BaseException`` subclass, so the ``except Exception`` clause does
  not catch it. Cancellation always propagates. No special-casing
  required.

Non-goals
---------
* **No retry statistics.** The caller who wants to count attempts wraps
  the operation. Adding stats to this module would either duplicate
  tenacity's own statistics object or diverge from it.
* **No ``retry_until`` (result-based retry).** Retrying *until a result
  matches a predicate* ("poll until ready") is a different operation
  from retrying *on exception*. Callers who need it write their own
  loop; the abstraction is not worth the surface.
* **No jitter-seeding policy.** :func:`exponential_jitter_backoff`
  accepts an optional ``rng`` for deterministic tests; production
  callers use the default (the module-level :class:`random.Random`).
  No global seed, no environment variable.

Migration notes
---------------
The downloader's ``build_retry`` and this module's ``retry_async`` are
independent. A future refactor could make ``build_retry`` return a
``RetryPolicy`` (this module's) plus a thin tenacity adapter, but that
is not proposed today — the downloader's policy is shaped by the
exception classification it carries, and flattening that into a generic
policy would be a regression in readability.

Dependency graph (no cycles)::

    core.exceptions  ──►  core.utils.retry
    asyncio, random, time (stdlib)

This module imports ``core.exceptions`` for ``RetryTimeoutError`` — the
one non-stdlib dependency. No other ``nexusdl`` import.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol, TypeVar

from nexusdl.core.exceptions import RetryTimeoutError


__all__ = [
    "BackoffStrategy",
    "RetryPolicy",
    "constant_backoff",
    "exponential_backoff",
    "exponential_jitter_backoff",
    "linear_backoff",
    "retry_async",
    "retry_sync",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #
# Values are deliberately identical to the downloader's defaults — a caller
# moving from one module to the other sees the same "3 attempts, 0.5s
# initial, 30s max" behaviour out of the box. The constants are private
# (``_DEFAULT_*``) so the two modules cannot drift through someone editing
# one and not the other; a caller who cares about the value writes it
# explicitly.
# --------------------------------------------------------------------------- #

_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_INITIAL_DELAY: Final[float] = 0.5
_DEFAULT_MAX_DELAY: Final[float] = 30.0
_DEFAULT_JITTER_FACTOR: Final[float] = 0.1

#: Type variable for the operation's return type. The retry loop is
#: transparent with respect to ``T``: ``retry_async(something_returning_x)``
#: has return type ``X``, checked by mypy --strict.
_T = TypeVar("_T")


# --------------------------------------------------------------------------- #
# Backoff strategies                                                          #
# --------------------------------------------------------------------------- #


class BackoffStrategy(Protocol):
    """Callable computing the delay, in seconds, before retry number *attempt*.

    Called once per retry, immediately after an attempt fails. The return
    value is used as the argument to ``asyncio.sleep`` (async path) or
    ``time.sleep`` (sync path). A negative return value is treated as
    zero — the caller that constructs a strategy has no reason to
    produce one, but the loop does not reject it.

    The argument is the **1-based number of the attempt that just
    failed** — not the upcoming attempt, and not a zero-based index. The
    first retry is therefore ``backoff(1)``, the second ``backoff(2)``,
    and so on. Every factory in this module follows the same convention;
    custom implementations should too.

    A ``BackoffStrategy`` may be any callable satisfying this signature;
    the ``Protocol`` is not marked ``@runtime_checkable`` because no
    call site needs ``isinstance`` checks.
    """

    def __call__(self, attempt: int) -> float:
        """Return the delay, in seconds, before the retry after *attempt*."""
        ...


def constant_backoff(delay: float) -> BackoffStrategy:
    """Return a strategy that always waits *delay* seconds between retries.

    Used when the caller wants predictable pacing (a fixed retry cadence
    for a rate-limited API) rather than escalating delays.

    Args:
        delay: The constant wait, in seconds. Must be ``>= 0``.

    Returns:
        A :class:`BackoffStrategy` returning *delay* regardless of the
        attempt number.

    Raises:
        ValueError: If *delay* is negative. A negative wait would either
            raise inside ``asyncio.sleep`` (async path) or be silently
            clamped to zero (sync path) — better to reject it at
            construction time so the caller sees the bug immediately.
    """
    if delay < 0:
        raise ValueError(
            f"constant_backoff() requires delay >= 0, got {delay}"
        )
    _delay = float(delay)

    def _backoff(_attempt: int) -> float:
        return _delay

    return _backoff


def linear_backoff(
    initial: float,
    increment: float,
    *,
    max_delay: float | None = None,
) -> BackoffStrategy:
    """Return a strategy whose wait grows by a fixed increment per attempt.

    Attempt *n* waits ``initial + increment * (n - 1)`` seconds. For
    example, ``linear_backoff(1.0, 2.0)`` yields waits of 1.0s, 3.0s,
    5.0s, … The growth is slower than :func:`exponential_backoff` and is
    the right choice when the caller knows approximately how long a
    transient failure takes to clear.

    Args:
        initial: The wait before the first retry, in seconds. Must be
            ``>= 0``.
        increment: The amount added to the wait for each subsequent
            retry, in seconds. Must be ``>= 0``.
        max_delay: Optional ceiling. When provided, the strategy returns
            ``min(computed, max_delay)``. Must be ``>= 0`` and, when
            combined with ``initial``, ``>= initial``.

    Returns:
        A :class:`BackoffStrategy` implementing linear growth.

    Raises:
        ValueError: If any argument is negative, or if ``max_delay`` is
            less than ``initial``.
    """
    if initial < 0:
        raise ValueError(
            f"linear_backoff() requires initial >= 0, got {initial}"
        )
    if increment < 0:
        raise ValueError(
            f"linear_backoff() requires increment >= 0, got {increment}"
        )
    if max_delay is not None:
        if max_delay < 0:
            raise ValueError(
                f"linear_backoff() requires max_delay >= 0, got "
                f"{max_delay}"
            )
        if max_delay < initial:
            raise ValueError(
                f"linear_backoff() requires max_delay >= initial "
                f"({initial}), got {max_delay}"
            )

    _initial = float(initial)
    _increment = float(increment)
    _max = float(max_delay) if max_delay is not None else None

    def _backoff(attempt: int) -> float:
        delay = _initial + _increment * (attempt - 1)
        if _max is not None:
            delay = min(delay, _max)
        return max(0.0, delay)

    return _backoff


def exponential_backoff(
    initial: float,
    *,
    multiplier: float = 2.0,
    max_delay: float | None = None,
) -> BackoffStrategy:
    """Return a strategy whose wait multiplies by *multiplier* per attempt.

    Attempt *n* waits ``initial * multiplier ** (n - 1)`` seconds. The
    default multiplier of ``2.0`` is the standard exponential backoff:
    ``exponential_backoff(1.0)`` yields 1.0s, 2.0s, 4.0s, 8.0s, …

    Args:
        initial: The wait before the first retry, in seconds. Must be
            ``>= 0``.
        multiplier: The per-retry growth factor. Must be ``>= 1.0`` —
            a value below 1.0 would produce a *shrinking* wait, which is
            never what a caller wants.
        max_delay: Optional ceiling.

    Returns:
        A :class:`BackoffStrategy` implementing exponential growth.

    Raises:
        ValueError: If *initial* is negative, *multiplier* is less than
            ``1.0``, or ``max_delay`` is negative or less than *initial*.
    """
    if initial < 0:
        raise ValueError(
            f"exponential_backoff() requires initial >= 0, got {initial}"
        )
    if multiplier < 1.0:
        raise ValueError(
            f"exponential_backoff() requires multiplier >= 1.0, got "
            f"{multiplier}"
        )
    if max_delay is not None:
        if max_delay < 0:
            raise ValueError(
                f"exponential_backoff() requires max_delay >= 0, got "
                f"{max_delay}"
            )
        if max_delay < initial:
            raise ValueError(
                f"exponential_backoff() requires max_delay >= initial "
                f"({initial}), got {max_delay}"
            )

    _initial = float(initial)
    _multiplier = float(multiplier)
    _max = float(max_delay) if max_delay is not None else None

    def _backoff(attempt: int) -> float:
        delay = _initial * (_multiplier ** (attempt - 1))
        if _max is not None:
            delay = min(delay, _max)
        return max(0.0, delay)

    return _backoff


def exponential_jitter_backoff(
    initial: float = _DEFAULT_INITIAL_DELAY,
    *,
    multiplier: float = 2.0,
    max_delay: float = _DEFAULT_MAX_DELAY,
    jitter_factor: float = _DEFAULT_JITTER_FACTOR,
    rng: random.Random | None = None,
) -> BackoffStrategy:
    """Return a strategy adding multiplicative jitter to exponential growth.

    Identical to :func:`exponential_backoff` except that the computed
    delay is multiplied by a uniform random factor in
    ``[1 - jitter_factor, 1 + jitter_factor]``. This "smears" retries
    that would otherwise happen in lockstep, avoiding the thundering-herd
    pattern where a dozen workers wake at the same instant and hammer a
    recovering site.

    The default jitter factor of ``0.1`` (10%) is what the downloader's
    default backoff uses. A caller that wants more variance sets it
    higher (``0.5`` means delays can vary by ±50%); a caller that wants
    deterministic delays uses :func:`exponential_backoff` directly.

    Args:
        initial: The wait before the first retry, in seconds. Must be
            ``>= 0``.
        multiplier: The per-retry growth factor. Must be ``>= 1.0``.
        max_delay: Optional ceiling. Applied **before** jitter, so the
            jittered delay can exceed ``max_delay`` by up to
            ``max_delay * jitter_factor``. Must be ``>= initial``.
        jitter_factor: The multiplicative variance applied to each
            delay, in ``[0.0, 1.0]``. ``0.0`` disables jitter (identical
            to :func:`exponential_backoff`); ``1.0`` varies between
            ``0.0`` and ``2.0`` times the computed delay. Values
            greater than ``1.0`` are rejected because they could produce
            delays below zero after clamping.
        rng: Optional :class:`random.Random` instance used to draw
            jitter values. Pass a seeded instance for deterministic
            tests; ``None`` uses the module-level
            :class:`random.Random` (which is seeded by the OS and
            safe for concurrent use).

    Returns:
        A :class:`BackoffStrategy` implementing exponential growth with
        multiplicative jitter.

    Raises:
        ValueError: If *initial* is negative, *multiplier* is less than
            ``1.0``, *max_delay* is negative or less than *initial*, or
            *jitter_factor* is outside ``[0.0, 1.0]``.
    """
    if initial < 0:
        raise ValueError(
            f"exponential_jitter_backoff() requires initial >= 0, got "
            f"{initial}"
        )
    if multiplier < 1.0:
        raise ValueError(
            f"exponential_jitter_backoff() requires multiplier >= 1.0, "
            f"got {multiplier}"
        )
    if max_delay < 0:
        raise ValueError(
            f"exponential_jitter_backoff() requires max_delay >= 0, got "
            f"{max_delay}"
        )
    if max_delay < initial:
        raise ValueError(
            f"exponential_jitter_backoff() requires max_delay >= initial "
            f"({initial}), got {max_delay}"
        )
    if not (0.0 <= jitter_factor <= 1.0):
        raise ValueError(
            f"exponential_jitter_backoff() requires jitter_factor in "
            f"[0.0, 1.0], got {jitter_factor}"
        )

    _initial = float(initial)
    _multiplier = float(multiplier)
    _max = float(max_delay)
    _jitter = float(jitter_factor)
    _rng = rng if rng is not None else random

    def _backoff(attempt: int) -> float:
        delay = _initial * (_multiplier ** (attempt - 1))
        delay = min(delay, _max)
        if _jitter > 0.0:
            factor = 1.0 + _rng.uniform(-_jitter, _jitter)
            delay *= factor
        return max(0.0, delay)

    return _backoff


#: The default backoff used by :class:`RetryPolicy`. Exponential growth
#: from 0.5s to 30s with 10% jitter — the same profile the downloader's
#: retry controller uses, chosen because it works well for the kind of
#: transient failures the pipeline sees (connection resets, brief DNS
#: blips, source sites recovering from overload).
_DEFAULT_BACKOFF: Final[BackoffStrategy] = exponential_jitter_backoff()


# --------------------------------------------------------------------------- #
# RetryPolicy                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Configuration for :func:`retry_async` / :func:`retry_sync`.

    Immutable and safe to share across call sites. Every field has a
    default that matches the downloader's retry defaults — a caller who
    just writes ``RetryPolicy()`` gets the profile the rest of the
    pipeline uses.

    Attributes:
        max_attempts: Total number of attempts, **including the first**.
            ``3`` means "try, then retry up to twice" — three attempts
            in total. Must be ``>= 1``. There is no "unbounded" value;
            a caller who wants effectively unbounded retries passes a
            large number and relies on ``max_elapsed_seconds`` or on
            external cancellation.
        max_elapsed_seconds: Optional wall-clock budget, from the start
            of the first attempt. When the budget is exhausted (checked
            **before** sleeping between attempts — see
            :func:`retry_async`), the loop raises
            :class:`~nexusdl.core.exceptions.RetryTimeoutError` with the
            last exception chained as ``__cause__``. ``None`` means "no
            wall-clock budget" — the loop is bounded only by
            ``max_attempts``. Must be ``> 0`` when set.
        backoff: The :class:`BackoffStrategy` used to compute the delay
            between attempts. Defaults to
            ``exponential_jitter_backoff(0.5, max_delay=30.0, jitter_factor=0.1)``
            — matching the downloader's default profile.
        retry_on: Predicate deciding whether a failed attempt is worth
            retrying. Called with the exception raised by the operation.
            Return ``True`` to retry, ``False`` to give up and propagate
            the exception immediately. ``None`` (the default) retries
            every ``Exception`` subclass — which is the right behaviour
            for operations with a single dominant failure mode. Callers
            whose operations raise a mix of transient and permanent
            exceptions **must** supply a predicate; without one, a
            ``ValueError`` from a caller bug would be retried three
            times before propagating, which wastes time and hides the
            bug in log noise.
    """

    max_attempts: int = _DEFAULT_MAX_ATTEMPTS
    max_elapsed_seconds: float | None = None
    backoff: BackoffStrategy = _DEFAULT_BACKOFF
    retry_on: Callable[[Exception], bool] | None = None

    def __post_init__(self) -> None:
        """Validate field values.

        Raises:
            ValueError: If ``max_attempts`` is less than ``1``, or if
                ``max_elapsed_seconds`` is set to a non-positive value.
                Both are programming errors at the composition root; a
                caller who passes them by accident gets a clear message
                at construction time, not an obscure failure inside the
                retry loop.
        """
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be >= 1, got {self.max_attempts}"
            )
        if self.max_elapsed_seconds is not None and self.max_elapsed_seconds <= 0:
            raise ValueError(
                f"max_elapsed_seconds must be > 0 or None, got "
                f"{self.max_elapsed_seconds}"
            )


# --------------------------------------------------------------------------- #
# Core loop                                                                   #
# --------------------------------------------------------------------------- #


def _should_retry(policy: RetryPolicy, exc: Exception) -> bool:
    """Return ``True`` iff *exc* warrants a retry under *policy*.

    Args:
        policy: The active policy.
        exc: The exception raised by the operation.
    """
    if policy.retry_on is None:
        return True
    return policy.retry_on(exc)


def _check_time_budget(
    policy: RetryPolicy,
    started_at: float,
    delay: float,
    last_exc: Exception,
) -> None:
    """Raise :class:`RetryTimeoutError` if sleeping *delay* would exceed the budget.

    Called **after** an attempt has failed and **before** sleeping.
    Sleeping past the budget only to raise immediately wastes the wait;
    raising at the point where the wait would exceed the budget gives
    the caller the same error message without the wasted time.

    Args:
        policy: The active policy.
        started_at: Monotonic timestamp of the loop's start.
        delay: The delay that would be slept.
        last_exc: The exception that just failed (chained on the raised
            :class:`RetryTimeoutError`).

    Raises:
        RetryTimeoutError: If ``elapsed + delay >= max_elapsed_seconds``.
    """
    if policy.max_elapsed_seconds is None:
        return
    elapsed = time.monotonic() - started_at
    if elapsed + delay >= policy.max_elapsed_seconds:
        raise RetryTimeoutError(
            f"retry budget of {policy.max_elapsed_seconds}s exceeded "
            f"after {elapsed:.2f}s of elapsed time "
            f"(next delay {delay:.2f}s would push past the budget)"
        ) from last_exc


# --------------------------------------------------------------------------- #
# Public retry functions                                                      #
# --------------------------------------------------------------------------- #


async def retry_async(
    operation: Callable[[], Awaitable[_T]],
    *,
    policy: RetryPolicy | None = None,
) -> _T:
    """Run *operation* with retries per *policy*, returning its result.

    The operation is called as ``await operation()`` — it must be a
    callable returning an awaitable (a zero-argument lambda, a
    ``functools.partial``, a bound method, or any callable that returns
    a coroutine). This lets the caller close over whatever arguments
    the operation needs without forcing this function to know about
    them: ``retry_async(lambda: client.get(url), policy=...)``.

    Retry semantics:

    * On success, the result is returned immediately.
    * On failure, :attr:`RetryPolicy.retry_on` is consulted (when set).
      If it returns ``False``, the exception is re-raised unchanged.
    * If ``max_attempts`` is reached, the last exception is re-raised.
    * If the wall-clock budget (``max_elapsed_seconds``) would be
      exceeded by sleeping the computed backoff, a
      :class:`~nexusdl.core.exceptions.RetryTimeoutError` is raised
      with the last exception chained as ``__cause__``.
    * ``asyncio.CancelledError`` is never caught — cancellation always
      propagates.

    Args:
        operation: The operation to run. Called once per attempt.
        policy: Retry configuration. Defaults to a fresh
            :class:`RetryPolicy` with the standard profile — three
            attempts, exponential-with-jitter backoff, no wall-clock
            budget.

    Returns:
        The value returned by the first successful call to *operation*.

    Raises:
        RetryTimeoutError: If the wall-clock budget is exceeded.
        Exception: The last exception raised by *operation*, unchanged,
            when retries are exhausted or ``retry_on`` returns ``False``.
        asyncio.CancelledError: Always propagates.
    """
    pol = policy if policy is not None else RetryPolicy()
    started_at = time.monotonic()
    attempt = 0

    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            if not _should_retry(pol, exc):
                raise
            if attempt >= pol.max_attempts:
                raise
            delay = pol.backoff(attempt)
            _check_time_budget(pol, started_at, delay, exc)
            await asyncio.sleep(delay)


def retry_sync(
    operation: Callable[[], _T],
    *,
    policy: RetryPolicy | None = None,
) -> _T:
    """Run *operation* with retries per *policy*, returning its result.

    The synchronous counterpart to :func:`retry_async`. Identical in
    semantics — same policy, same backoff, same retry predicate, same
    :class:`RetryTimeoutError` on budget exhaustion — with
    :func:`time.sleep` in place of :func:`asyncio.sleep`.

    Intended for call sites that are **not** running inside an event
    loop: CLI helpers that run before the loop starts, tests written as
    plain functions, initialization code in the composition root.
    Calling :func:`retry_sync` from inside a coroutine would block the
    event loop during the sleep — use :func:`retry_async` instead.

    Args:
        operation: The operation to run. Called once per attempt.
        policy: Retry configuration. Defaults to a fresh
            :class:`RetryPolicy` with the standard profile.

    Returns:
        The value returned by the first successful call to *operation*.

    Raises:
        RetryTimeoutError: If the wall-clock budget is exceeded.
        Exception: The last exception raised by *operation*, unchanged,
            when retries are exhausted or ``retry_on`` returns ``False``.
    """
    pol = policy if policy is not None else RetryPolicy()
    started_at = time.monotonic()
    attempt = 0

    while True:
        attempt += 1
        try:
            return operation()
        except Exception as exc:
            if not _should_retry(pol, exc):
                raise
            if attempt >= pol.max_attempts:
                raise
            delay = pol.backoff(attempt)
            _check_time_budget(pol, started_at, delay, exc)
            time.sleep(delay)
