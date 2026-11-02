"""Circuit breaker for source-site resilience.

When a source site is down — a 5xx storm, a DNS failure, an endpoint
that has been returning 503 for five minutes straight — continuing to
hammer it with requests is counter-productive:

* It costs the user bandwidth and time.
* It costs the site operator resources that the site cannot spare.
* It delays the "give up" signal the pipeline needs in order to move on
  or surface a clear error to the caller.

A circuit breaker solves all three by short-circuiting requests to a
broken site. After ``failure_threshold`` consecutive failures the
breaker **opens**: subsequent calls to :meth:`CircuitBreaker.allow_request`
return ``False`` without touching the network, so the caller can fail
fast with :class:`~nexusdl.core.exceptions.CircuitBreakerOpenError`
instead of waiting on another timeout.

After ``recovery_timeout`` seconds the breaker enters **half-open**: one
trial request is allowed through. If it succeeds, the breaker closes and
normal traffic resumes. If it fails, the breaker re-opens — the site is
still down, and the caller is spared another failed request.

Two classes, two audiences
--------------------------
* :class:`CircuitBreaker` — the **per-site** state machine. A single
  instance tracks one site. Its API is the one specified by the PROMPT
  MASTER: ``allow_request()``, ``record_success()``, ``record_failure()``,
  ``reset()``, ``force_open()``, ``stats()``.
* :class:`CircuitBreakerPool` — the **multi-site** façade. It owns a
  ``dict[site_id, CircuitBreaker]``, creates breakers lazily on first
  use, and exposes the same three methods with a ``site_id`` argument.
  This is what
  :class:`~nexusdl.core.session.http_session.HttpSession` consumes —
  see ``CircuitBreakerProtocol`` in ``http_session.py``.

Why both exist: the state machine has no business knowing about site
ids, and the session has no business managing a dict. Splitting them
makes each independently testable — a test of the CLOSED→OPEN→HALF_OPEN→CLOSED
cycle needs only one breaker; a test of the pool's lifecycle needs only
a stub breaker.

Synchronous by design
---------------------
Every method is **synchronous**. The session calls
``allow_request(site_id)``, ``record_success(site_id)``, and
``record_failure(site_id)`` from synchronous helpers
(``_apply_circuit_breaker``, ``_record_success``, ``_record_failure`` in
``http_session.py``). Making these async would force the session's
helpers to be async, which would propagate asyncness into code that has
no I/O to await.

Concurrency is protected by a :class:`threading.RLock`. The critical
sections are a handful of attribute updates — nanoseconds — so the lock
is never contended in practice. ``RLock`` (rather than plain ``Lock``)
is used because :meth:`CircuitBreaker.stats` refreshes the state machine
(``_maybe_transition_from_open``) while holding the lock, and
``_open_locked`` / ``reset`` do the same. A plain ``Lock`` would
self-deadlock on those re-entrant paths.

No persistence
--------------
The circuit breaker is a **runtime** signal. It does not persist its
state across process restarts: a fresh process starts with every site
healthy, on the assumption that whatever was wrong has either been fixed
or will fail again on the first request. Persisting would complicate
shutdown (every open breaker would need a flush) for no diagnostic gain
over the `stats()` snapshot that the CLI already exposes.

Time is injectable
------------------
``clock`` is a monotonic-time callable (default :func:`time.monotonic`).
Tests inject a fake clock to exercise the OPEN→HALF_OPEN transition
without sleeping for ``recovery_timeout`` seconds.

Dependency graph (no cycles)::

    core.exceptions  ──►  core.session.circuit_breaker
    loguru
    threading, time, dataclasses (stdlib)

No dependency on ``httpx``, ``core.models``, or any other
``core.session`` module — this module is a leaf.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerPool",
    "CircuitBreakerPoolStats",
    "CircuitBreakerStats",
    "CircuitBreakerState",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_HALF_OPEN_MAX_ATTEMPTS",
    "DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD",
    "DEFAULT_RECOVERY_TIMEOUT",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: State type for the circuit breaker's state machine. Using a
#: :class:`~typing.Literal` rather than a :class:`~enum.StrEnum` keeps the
#: surface small — this is a session-internal state, not a domain concept,
#: and it never crosses the API boundary.
CircuitBreakerState: TypeAlias = Literal["closed", "open", "half_open"]

#: Default number of consecutive failures in CLOSED that trip the breaker
#: to OPEN. Five is the conventional value from the Netflix Hystrix and
#: resilience4j defaults — one failure is noise, five is a pattern.
DEFAULT_FAILURE_THRESHOLD: Final[int] = 5

#: Default duration, in seconds, a breaker stays OPEN before entering
#: HALF_OPEN. Thirty seconds balances two concerns: a transient outage
#: (server restart, network blip) is given time to clear; a genuinely
#: broken site is retried soon enough that a background health check can
#: recover it without the user seeing a stale "site down" state.
DEFAULT_RECOVERY_TIMEOUT: Final[float] = 30.0

#: Default number of consecutive successes in HALF_OPEN required to close
#: the breaker. ``1`` is the standard: one successful trial request is
#: sufficient evidence that the site is back.
DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD: Final[int] = 1

#: Default number of concurrent requests allowed through in HALF_OPEN.
#: ``1`` serialises trial requests — the goal is to probe, not to
#: pipeline. A caller that genuinely wants parallel probes sets a higher
#: value; most deployments do not.
DEFAULT_HALF_OPEN_MAX_ATTEMPTS: Final[int] = 1


# --------------------------------------------------------------------------- #
# Stats                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CircuitBreakerStats:
    """Immutable snapshot of a single :class:`CircuitBreaker`.

    Returned by :meth:`CircuitBreaker.stats`. Every field is a
    point-in-time observation; nothing here is ever mutated by the
    breaker after the snapshot is taken.

    Attributes:
        name: Optional identifier (typically a ``site_id``). Empty when
            the breaker was constructed without one.
        state: Current state — ``"closed"``, ``"open"``, or
            ``"half_open"``. Refreshed against the clock before the
            snapshot is taken, so an OPEN breaker whose
            ``recovery_timeout`` has just elapsed reports ``"half_open"``
            even if no request has gone through yet.
        consecutive_failures: Current run of failures since the last
            success. Reset to ``0`` on :meth:`CircuitBreaker.record_success`
            in the CLOSED state and on any transition to CLOSED.
        last_failure_at: Monotonic timestamp of the most recent failure,
            or ``None`` if the breaker has never seen one. The value is
            meaningful only relative to the breaker's own clock.
        opened_at: Monotonic timestamp of the most recent transition
            into OPEN, or ``None`` if the breaker has never been open.
        opened_until: Monotonic timestamp at which the OPEN breaker will
            transition to HALF_OPEN. ``0.0`` when the breaker is not
            open. Exposed so a diagnostic UI can render a countdown.
        total_requests: Cumulative count of successful
            :meth:`CircuitBreaker.allow_request` calls (i.e. requests
            that were actually allowed through).
        total_rejections: Cumulative count of :meth:`CircuitBreaker.allow_request`
            calls that returned ``False`` (either because the breaker
            was OPEN, or because HALF_OPEN's slot was busy).
        total_successes: Cumulative count of :meth:`CircuitBreaker.record_success`
            calls.
        total_failures: Cumulative count of :meth:`CircuitBreaker.record_failure`
            calls.
        total_state_changes: Cumulative count of state transitions since
            the breaker was constructed.
    """

    name: str
    state: CircuitBreakerState
    consecutive_failures: int
    last_failure_at: float | None
    opened_at: float | None
    opened_until: float
    total_requests: int
    total_rejections: int
    total_successes: int
    total_failures: int
    total_state_changes: int


@dataclass(frozen=True, slots=True)
class CircuitBreakerPoolStats:
    """Immutable snapshot of a :class:`CircuitBreakerPool`.

    Returned by :meth:`CircuitBreakerPool.pool_stats`. Breakers are
    listed in the sorted order of their site ids so two snapshots of the
    same pool are diffable by position.

    Attributes:
        total_breakers: Total number of breakers the pool has created.
        closed_count: Number of breakers currently in CLOSED.
        half_open_count: Number in HALF_OPEN (including those whose
            cooldown has just elapsed — same refresh rule as
            :class:`CircuitBreakerStats`).
        open_count: Number in OPEN.
        per_site: One :class:`CircuitBreakerStats` per breaker, ordered
            by ``site_id``.
    """

    total_breakers: int
    closed_count: int
    half_open_count: int
    open_count: int
    per_site: tuple[CircuitBreakerStats, ...]


# --------------------------------------------------------------------------- #
# CircuitBreaker — per-site state machine                                     #
# --------------------------------------------------------------------------- #


class CircuitBreaker:
    """The three-state circuit breaker for a single site.

    Lifecycle::

        CLOSED  ──(failure_threshold consecutive failures)──►  OPEN
           ▲                                                    │
           │                                                    │
           │                                (recovery_timeout elapsed)
           │                                                    │
           │                                                    ▼
        CLOSED  ◄──(half_open_success_threshold successes)──  HALF_OPEN
           ▲                                                    │
           │                                                    │
           └────────────────(any failure)──────────────────────┘
                                                                 │
                                                                 ▼
                                                               OPEN

    Every method is synchronous and thread-safe. See the module
    docstring for the full contract, the reason ``RLock`` is used, and
    the config defaults.

    Example:
        >>> breaker = CircuitBreaker(failure_threshold=3, name="mangadex")
        >>> if breaker.allow_request():
        ...     try:
        ...         await client.get(url)
        ...     except Exception:
        ...         breaker.record_failure()
        ...         raise
        ...     else:
        ...         breaker.record_success()

    Args:
        failure_threshold: Number of consecutive failures in CLOSED
            that trips the breaker to OPEN. Must be ``>= 1``.
        recovery_timeout: Seconds the breaker stays OPEN before entering
            HALF_OPEN. Must be ``>= 0``. A value of ``0`` disables
            the cooldown — the breaker enters HALF_OPEN on the next
            :meth:`allow_request` call after tripping. Useful for
            tests, rarely useful in production.
        half_open_success_threshold: Number of consecutive successes in
            HALF_OPEN required to close the breaker. Must be ``>= 1``.
        half_open_max_attempts: Number of concurrent requests allowed
            through while in HALF_OPEN. Must be ``>= 1``. With the
            default of ``1``, only one trial request is in flight at a
            time; a second concurrent call gets rejected until the
            first completes.
        name: Optional identifier (typically a ``site_id``) surfaced in
            :attr:`CircuitBreakerStats.name` and never anywhere else.
            Empty by default.
        clock: Callable returning a monotonic float in seconds. Defaults
            to :func:`time.monotonic`. Injectable for tests that want
            to exercise the OPEN→HALF_OPEN transition without sleeping.

    Raises:
        ValueError: If any numeric argument is out of range.
    """

    __slots__ = (
        "_clock",
        "_consecutive_failures",
        "_failure_threshold",
        "_half_open_in_flight",
        "_half_open_max_attempts",
        "_half_open_success_threshold",
        "_half_open_successes",
        "_last_failure_at",
        "_last_state_change_at",
        "_lock",
        "_name",
        "_opened_at",
        "_opened_until",
        "_recovery_timeout",
        "_state",
        "_total_failures",
        "_total_rejections",
        "_total_requests",
        "_total_state_changes",
        "_total_successes",
    )

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        half_open_success_threshold: int = DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD,
        half_open_max_attempts: int = DEFAULT_HALF_OPEN_MAX_ATTEMPTS,
        name: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(
                f"failure_threshold must be >= 1, got {failure_threshold}"
            )
        if recovery_timeout < 0:
            raise ValueError(
                f"recovery_timeout must be >= 0, got {recovery_timeout}"
            )
        if half_open_success_threshold < 1:
            raise ValueError(
                f"half_open_success_threshold must be >= 1, got "
                f"{half_open_success_threshold}"
            )
        if half_open_max_attempts < 1:
            raise ValueError(
                f"half_open_max_attempts must be >= 1, got "
                f"{half_open_max_attempts}"
            )

        self._failure_threshold: int = failure_threshold
        self._recovery_timeout: float = float(recovery_timeout)
        self._half_open_success_threshold: int = half_open_success_threshold
        self._half_open_max_attempts: int = half_open_max_attempts
        self._name: str = name
        self._clock: Callable[[], float] = clock
        self._lock = threading.RLock()

        # State machine.
        self._state: CircuitBreakerState = "closed"
        self._consecutive_failures: int = 0
        self._half_open_successes: int = 0
        self._half_open_in_flight: int = 0

        # Timestamps (monotonic, only meaningful via :attr:`_clock`).
        self._last_failure_at: float | None = None
        self._opened_at: float | None = None
        self._opened_until: float = 0.0
        self._last_state_change_at: float = self._clock()

        # Cumulative counters.
        self._total_requests: int = 0
        self._total_rejections: int = 0
        self._total_successes: int = 0
        self._total_failures: int = 0
        self._total_state_changes: int = 0

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def allow_request(self) -> bool:
        """Return ``True`` iff a request should be attempted.

        In CLOSED, always returns ``True``. In OPEN, returns ``False``
        until ``recovery_timeout`` has elapsed (at which point the
        breaker transitions to HALF_OPEN on this call and returns
        ``True`` — subject to the concurrency limit). In HALF_OPEN,
        returns ``True`` iff fewer than ``half_open_max_attempts`` are
        currently in flight.

        Every successful call increments :attr:`CircuitBreakerStats.total_requests`;
        every rejected call increments ``total_rejections``.

        Returns:
            ``True`` if the caller should proceed with the request,
            ``False`` if the caller must fail fast (typically by raising
            :class:`~nexusdl.core.exceptions.CircuitBreakerOpenError`).
        """
        with self._lock:
            self._maybe_transition_from_open_locked()

            if self._state == "closed":
                self._total_requests += 1
                return True
            if self._state == "open":
                self._total_rejections += 1
                return False
            # half_open
            if self._half_open_in_flight < self._half_open_max_attempts:
                self._half_open_in_flight += 1
                self._total_requests += 1
                return True
            self._total_rejections += 1
            return False

    def record_success(self) -> None:
        """Record a successful request.

        In CLOSED, resets the consecutive-failure counter. In HALF_OPEN,
        frees the trial slot and increments the half-open success
        counter; when it reaches ``half_open_success_threshold`` the
        breaker closes. In OPEN, the call is recorded in the cumulative
        counter but does not change state — a success while OPEN means
        the caller raced the transition (rare); the OPEN timer is the
        authority.
        """
        with self._lock:
            self._total_successes += 1
            if self._state == "closed":
                self._consecutive_failures = 0
                return
            if self._state == "open":
                return
            # half_open
            if self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1
            self._half_open_successes += 1
            if self._half_open_successes >= self._half_open_success_threshold:
                self._transition_to_closed_locked()

    def record_failure(self) -> None:
        """Record a failed request.

        In CLOSED, increments the consecutive-failure counter; when it
        reaches ``failure_threshold`` the breaker opens. In HALF_OPEN,
        frees the trial slot and immediately re-opens the breaker. In
        OPEN, the call is recorded in the cumulative counter but does
        not extend the cooldown — the breaker is already down.
        """
        with self._lock:
            self._total_failures += 1
            self._last_failure_at = self._clock()
            if self._state == "closed":
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._failure_threshold:
                    self._transition_to_open_locked()
                return
            if self._state == "open":
                return
            # half_open
            if self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1
            self._transition_to_open_locked()

    def reset(self) -> None:
        """Force the breaker back to CLOSED, clearing every counter.

        Idempotent. Intended for tests and for an operator that has
        verified the site is healthy again and wants to skip the
        recovery wait. The cumulative counters (``total_*``) are
        preserved — only the current-state counters (consecutive
        failures, half-open progress, open timestamps) are reset.
        """
        with self._lock:
            if self._state != "closed":
                self._state = "closed"
                self._total_state_changes += 1
                self._last_state_change_at = self._clock()
            self._consecutive_failures = 0
            self._half_open_successes = 0
            self._half_open_in_flight = 0
            self._opened_at = None
            self._opened_until = 0.0

    def force_open(self) -> None:
        """Force the breaker to OPEN from any state.

        Idempotent (opening an already-open breaker is a no-op for the
        timers). Intended for the same scenarios as :meth:`reset`, in
        the opposite direction — an operator who knows a site is down
        can pre-emptively stop requests without waiting for the failure
        threshold.
        """
        with self._lock:
            if self._state == "open":
                return
            self._transition_to_open_locked()

    def stats(self) -> CircuitBreakerStats:
        """Return an immutable snapshot of the breaker's state.

        The state is refreshed against the clock before the snapshot is
        taken, so an OPEN breaker whose cooldown has just elapsed
        reports ``"half_open"`` even if no request has gone through.

        Returns:
            A :class:`CircuitBreakerStats`.
        """
        with self._lock:
            self._maybe_transition_from_open_locked()
            return CircuitBreakerStats(
                name=self._name,
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                last_failure_at=self._last_failure_at,
                opened_at=self._opened_at,
                opened_until=self._opened_until,
                total_requests=self._total_requests,
                total_rejections=self._total_rejections,
                total_successes=self._total_successes,
                total_failures=self._total_failures,
                total_state_changes=self._total_state_changes,
            )

    # ------------------------------------------------------------------ #
    # Read-only properties                                               #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> CircuitBreakerState:
        """Return the current state, refreshing the OPEN timer first."""
        with self._lock:
            self._maybe_transition_from_open_locked()
            return self._state

    @property
    def name(self) -> str:
        """The breaker's optional name (typically a site id)."""
        return self._name

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        with self._lock:
            return (
                f"CircuitBreaker(name={self._name!r}, "
                f"state={self._state!r}, "
                f"failures={self._consecutive_failures}/"
                f"{self._failure_threshold})"
            )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _maybe_transition_from_open_locked(self) -> None:
        """Transition OPEN → HALF_OPEN if the recovery timeout elapsed.

        Caller must hold :attr:`_lock`. The transition is **lazy** — it
        happens on the first :meth:`allow_request` or :meth:`stats`
        call after the timer expires, not on a background timer. This
        keeps the breaker free of background tasks and makes its
        behaviour fully deterministic under an injected clock.
        """
        if self._state != "open":
            return
        if self._clock() < self._opened_until:
            return
        self._state = "half_open"
        self._half_open_successes = 0
        self._half_open_in_flight = 0
        self._last_state_change_at = self._clock()
        self._total_state_changes += 1

    def _transition_to_open_locked(self) -> None:
        """Transition to OPEN and arm the recovery timer.

        Caller must hold :attr:`_lock`. Idempotent with respect to the
        timers when already open — see :meth:`force_open` for the
        rationale.
        """
        self._state = "open"
        self._opened_at = self._clock()
        self._opened_until = self._opened_at + self._recovery_timeout
        self._last_state_change_at = self._opened_at
        self._total_state_changes += 1
        self._half_open_successes = 0
        self._half_open_in_flight = 0

    def _transition_to_closed_locked(self) -> None:
        """Transition to CLOSED, resetting all counters.

        Caller must hold :attr:`_lock`. Called when HALF_OPEN has
        accumulated ``half_open_success_threshold`` consecutive
        successes.
        """
        self._state = "closed"
        self._consecutive_failures = 0
        self._half_open_successes = 0
        self._half_open_in_flight = 0
        self._opened_at = None
        self._opened_until = 0.0
        self._last_state_change_at = self._clock()
        self._total_state_changes += 1


# --------------------------------------------------------------------------- #
# CircuitBreakerPool — multi-site façade                                     #
# --------------------------------------------------------------------------- #


class CircuitBreakerPool:
    """A pool of :class:`CircuitBreaker` instances, keyed by site id.

    Satisfies :class:`nexusdl.core.session.http_session.CircuitBreakerProtocol`:
    ``allow_request(site_id)``, ``record_success(site_id)``,
    ``record_failure(site_id)``. The pool is what
    :class:`~nexusdl.core.session.http_session.HttpSession` holds; it
    creates per-site breakers lazily and shares the same configuration
    across every site.

    Unlike :class:`CircuitBreaker`, the pool has no state of its own
    beyond the dict of breakers. It is safe to share across coroutines
    and threads (the dict is guarded by a lock; the breakers guard
    themselves).

    Example:
        >>> pool = CircuitBreakerPool(failure_threshold=5)
        >>> if pool.allow_request("mangadex"):
        ...     try:
        ...         await client.get(url, site_id="mangadex")
        ...     except Exception:
        ...         pool.record_failure("mangadex")
        ...         raise
        ...     else:
        ...         pool.record_success("mangadex")

    Args:
        failure_threshold: Passed to every breaker the pool creates.
        recovery_timeout: Passed to every breaker the pool creates.
        half_open_success_threshold: Passed to every breaker the pool
            creates.
        half_open_max_attempts: Passed to every breaker the pool
            creates.
        clock: Shared monotonic clock for every breaker the pool
            creates. Defaults to :func:`time.monotonic`. Injectable for
            tests.

    Raises:
        ValueError: If any numeric argument is out of range (validated
            eagerly by constructing one probe breaker at startup, then
            discarded — see :meth:`_make_breaker`).
    """

    __slots__ = (
        "_breakers",
        "_clock",
        "_failure_threshold",
        "_half_open_max_attempts",
        "_half_open_success_threshold",
        "_lock",
        "_recovery_timeout",
    )

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        half_open_success_threshold: int = DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD,
        half_open_max_attempts: int = DEFAULT_HALF_OPEN_MAX_ATTEMPTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Validate the configuration by constructing one probe breaker.
        # The probe is discarded — validation failure surfaces here, at
        # the pool's construction time, not at the first ``allow_request``
        # for a real site.
        CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_success_threshold=half_open_success_threshold,
            half_open_max_attempts=half_open_max_attempts,
            name="",
            clock=clock,
        )

        self._failure_threshold: int = failure_threshold
        self._recovery_timeout: float = float(recovery_timeout)
        self._half_open_success_threshold: int = half_open_success_threshold
        self._half_open_max_attempts: int = half_open_max_attempts
        self._clock: Callable[[], float] = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Protocol methods (CircuitBreakerProtocol)                          #
    # ------------------------------------------------------------------ #

    def allow_request(self, site_id: str) -> bool:
        """Return ``True`` iff a request to *site_id* should be attempted.

        Creates the site's breaker on first use. See
        :meth:`CircuitBreaker.allow_request` for the state-machine
        semantics.

        Args:
            site_id: The site identifier. An unknown id yields a fresh
                CLOSED breaker that immediately returns ``True``.
        """
        return self._get_or_create(site_id).allow_request()

    def record_success(self, site_id: str) -> None:
        """Record a successful request to *site_id*.

        Args:
            site_id: The site identifier.
        """
        self._get_or_create(site_id).record_success()

    def record_failure(self, site_id: str) -> None:
        """Record a failed request to *site_id*.

        Args:
            site_id: The site identifier.
        """
        self._get_or_create(site_id).record_failure()

    # ------------------------------------------------------------------ #
    # Per-site administrative API                                        #
    # ------------------------------------------------------------------ #

    def reset(self, site_id: str) -> None:
        """Reset *site_id*'s breaker to CLOSED.

        See :meth:`CircuitBreaker.reset`. A site id with no breaker is
        a no-op — there is nothing to reset.
        """
        breaker = self._get_existing(site_id)
        if breaker is not None:
            breaker.reset()

    def force_open(self, site_id: str) -> None:
        """Force *site_id*'s breaker to OPEN.

        See :meth:`CircuitBreaker.force_open`.
        """
        self._get_or_create(site_id).force_open()

    def stats(self, site_id: str) -> CircuitBreakerStats | None:
        """Return *site_id*'s breaker stats, or ``None`` if the site is unknown."""
        breaker = self._get_existing(site_id)
        return None if breaker is None else breaker.stats()

    def known_sites(self) -> tuple[str, ...]:
        """Return every site id that has a breaker, sorted alphabetically."""
        with self._lock:
            return tuple(sorted(self._breakers))

    def pool_stats(self) -> CircuitBreakerPoolStats:
        """Return an immutable aggregate snapshot of the pool.

        Breakers are listed in ``site_id`` order so two snapshots are
        position-comparable.
        """
        with self._lock:
            site_ids = sorted(self._breakers)
            breakers = [self._breakers[sid] for sid in site_ids]

        # Build stats outside the pool lock — each breaker takes its own
        # lock; holding the pool lock while walking breakers would create
        # a lock-ordering hazard if a future breaker needed to call back
        # into the pool. The breakers list is a stable snapshot taken
        # above.
        per_site = tuple(b.stats() for b in breakers)
        closed = sum(1 for s in per_site if s.state == "closed")
        half_open = sum(1 for s in per_site if s.state == "half_open")
        open_count = sum(1 for s in per_site if s.state == "open")
        return CircuitBreakerPoolStats(
            total_breakers=len(per_site),
            closed_count=closed,
            half_open_count=half_open,
            open_count=open_count,
            per_site=per_site,
        )

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        with self._lock:
            count = len(self._breakers)
        return f"CircuitBreakerPool(breakers={count})"

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _get_or_create(self, site_id: str) -> CircuitBreaker:
        """Return *site_id*'s breaker, creating it if absent."""
        with self._lock:
            breaker = self._breakers.get(site_id)
            if breaker is None:
                breaker = self._make_breaker(site_id)
                self._breakers[site_id] = breaker
            return breaker

    def _get_existing(self, site_id: str) -> CircuitBreaker | None:
        """Return *site_id*'s breaker, or ``None`` if it does not exist."""
        with self._lock:
            return self._breakers.get(site_id)

    def _make_breaker(self, site_id: str) -> CircuitBreaker:
        """Construct a fresh breaker with the pool's configuration."""
        return CircuitBreaker(
            failure_threshold=self._failure_threshold,
            recovery_timeout=self._recovery_timeout,
            half_open_success_threshold=self._half_open_success_threshold,
            half_open_max_attempts=self._half_open_max_attempts,
            name=site_id,
            clock=self._clock,
        )
