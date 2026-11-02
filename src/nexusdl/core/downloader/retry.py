"""Retry policy and error classification for the download pipeline.

Unlike :mod:`nexusdl.core.utils.retry` — the generic retry primitives usable
anywhere — this module is **specific to the download pipeline** and encodes
domain knowledge about which download exceptions are transient (and
therefore worth retrying) and which are permanent (and should fail fast).

Three responsibilities:

1. **Policy** — :class:`RetryPolicy` is the immutable bundle of retry
   parameters (attempt count, backoff bounds). Built from
   ``Settings.downloader.retry`` via :func:`policy_from_settings`, with
   per-field defaults so a partially-configured or absent section still
   yields a valid policy.

2. **Classification** — :func:`is_retryable` decides, per exception, whether
   retrying has any chance of succeeding. The classification is closed and
   explicit: a new exception subclass is **not** retried by default. The
   conservative choice is intentional — retrying a permanent error wastes
   time and, on rate-limited sites, actively makes things worse.

3. **Controller** — :func:`build_retry` produces a fully-configured
   :class:`tenacity.AsyncRetrying` whose ``retry`` predicate is
   :func:`is_retryable`, whose ``wait`` is exponential + jitter bounded by
   the policy, and whose ``reraise`` is ``True`` so that the original
   exception propagates when retries are exhausted. Callers never see a
   :class:`tenacity.RetryError` — they see the exception that caused the
   final failure, which is what their ``except`` clauses expect.

Why ``reraise=True`` and no ``RetryTimeoutError`` here
------------------------------------------------------
The :class:`~nexusdl.core.exceptions.RetryTimeoutError` in the exception
hierarchy is raised by :mod:`nexusdl.core.utils.retry` when a generic retry
loop exceeds a wall-clock budget. This module never imposes one: the
downloader pipeline budgets time at the chapter level (via
``Settings.downloader`` and the manager), not at the individual retry level.
Propagating the original exception keeps the worker's ``except`` clauses
simple — it catches :class:`~nexusdl.core.exceptions.PageDownloadError`, not
:class:`~nexusdl.core.exceptions.RetryError`.

Cancellation
------------
:class:`asyncio.CancelledError` is a :class:`BaseException` (Python 3.8+),
not an :class:`Exception`. Tenacity never retries it, and neither does
:func:`is_retryable`. Cancellation always propagates untouched.

Dependency graph
----------------
``core.exceptions`` (for the exception types being classified) and two
third-party runtime dependencies — ``tenacity`` and ``loguru``. No
``core.session``, no ``parsers``, no ``plugins``. The classifier knows only
the exception *types*, not their implementations.

See also
--------
* :mod:`nexusdl.core.utils.retry` — the generic retry primitives, when they
  are written. This module is not a replacement; it is the downloader's
  opinionated layer on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from nexusdl.core.exceptions import (
    CircuitBreakerOpenError,
    ComplianceError,
    ConfigError,
    ImageValidationError,
    PageDownloadError,
    ParserNotFoundError,
    ParserParseError,
    ParserResponseError,
    SessionCloudflareError,
    SessionRateLimitError,
    SessionTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tenacity import RetryCallState


__all__ = [
    "DEFAULT_INITIAL_DELAY",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY",
    "RetryPolicy",
    "build_retry",
    "is_retryable",
    "policy_from_settings",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default number of total attempts (initial + retries). Chosen to balance
#: reliability against wasted bandwidth on flaky pages: three attempts catch
#: the vast majority of transient blips without hammering a struggling site.
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

#: Default delay before the first retry, in seconds.
DEFAULT_INITIAL_DELAY: Final[float] = 0.5

#: Default ceiling on the delay between two consecutive retries, in seconds.
#: Exponential backoff rarely exceeds this; the cap protects against pathological
#: configurations (e.g. ``initial_delay=60``).
DEFAULT_MAX_DELAY: Final[float] = 30.0

#: Module logger. Bound once at import; per-attempt logging goes through
#: :func:`_log_retry` which is passed to :class:`tenacity.AsyncRetrying` as
#: its ``before_sleep`` callback.
_log = logger.bind(component="downloader.retry")


# --------------------------------------------------------------------------- #
# Error classification                                                        #
# --------------------------------------------------------------------------- #
# Two closed sets. Adding a new transient exception requires editing this
# list — the conservative default (do not retry) is intentional: a missed
# classification fails fast and is visible in logs, whereas an accidental
# retry of a permanent error silently wastes time and can trigger rate
# limits on the target site.
#
# Order does not matter: :func:`is_retryable` checks membership, not
# inheritance. Base classes are deliberately **absent** from both sets —
# classifying ``DownloadError`` (the base) as retryable would also retry
# ``ChapterDownloadError`` and ``EmptyChapterError``, which is not what the
# page-level retry loop should do.
# --------------------------------------------------------------------------- #

#: Exceptions that represent a transient failure: retrying has a real chance
#: of succeeding. The download pipeline considers these retryable.
_TRANSIENT_ERRORS: Final[tuple[type[Exception], ...]] = (
    # Session-level transience: timeout, rate-limit, Cloudflare interstitial,
    # and an open circuit breaker (which is expected to close after its own
    # recovery timeout — the retry's backoff usually spans that window).
    SessionTimeoutError,
    SessionRateLimitError,
    SessionCloudflareError,
    CircuitBreakerOpenError,
    # Parser got a response but could not process it. Frequently means a 5xx,
    # a truncated body, or a WAF interstitial. Retrying is worth it.
    ParserResponseError,
    # Page download failed. Almost always a network blip or a partial write.
    PageDownloadError,
    # Validation rejected a file — commonly because HTML / a Cloudflare page
    # was served instead of an image. This is typically transient.
    ImageValidationError,
)

#: Exceptions that represent a permanent failure: retrying wastes time.
_PERMANENT_ERRORS: Final[tuple[type[Exception], ...]] = (
    # Configuration and compliance — retrying cannot help.
    ConfigError,
    ComplianceError,
    # The parser does not exist, or the site's markup has changed to the
    # point that parsing fails deterministically. Code must be updated.
    ParserNotFoundError,
    ParserParseError,
)


def is_retryable(exc: BaseException) -> bool:
    """Return ``True`` iff *exc* represents a transient failure worth retrying.

    The function is the ``retry`` predicate for every retry controller built
    by :func:`build_retry`. It is intentionally closed: an exception class
    not listed in either :data:`_TRANSIENT_ERRORS` or
    :data:`_PERMANENT_ERRORS` returns ``False``. This makes the default
    safe — adding a new exception type without classifying it will fail
    fast rather than silently retry.

    ``BaseException`` subclasses that are *not* ``Exception`` (notably
    :class:`asyncio.CancelledError`, :class:`KeyboardInterrupt`, and
    :class:`SystemExit`) always return ``False``. Cancellation must
    propagate through every retry loop untouched.

    Args:
        exc: The exception to classify.

    Returns:
        ``True`` for exceptions in :data:`_TRANSIENT_ERRORS`, ``False`` for
        exceptions in :data:`_PERMANENT_ERRORS` and for anything else.
    """
    if not isinstance(exc, Exception):
        # BaseException that is not an Exception — cancellation, interrupt,
        # or exit. Never retry.
        return False
    if isinstance(exc, _PERMANENT_ERRORS):
        return False
    if isinstance(exc, _TRANSIENT_ERRORS):
        return True
    # Unclassified: be conservative and fail fast.
    return False


# --------------------------------------------------------------------------- #
# Policy                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Immutable bundle of retry parameters for the download pipeline.

    Instances are built once (typically from ``Settings.downloader.retry``)
    and passed to :func:`build_retry`. They are safe to share across tasks,
    cache, and serialise.

    Attributes:
        max_attempts: Total number of attempts, including the first. Must be
            ``>= 1``. ``1`` means "no retry" (the first failure propagates
            immediately).
        initial_delay: Delay before the first retry, in seconds. Must be
            ``>= 0``.
        max_delay: Upper bound on the delay between two consecutive retries,
            in seconds. Must be ``>= initial_delay``. Exponential backoff is
            capped here so a pathological configuration cannot produce
            minute-long sleeps.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_delay: float = DEFAULT_INITIAL_DELAY
    max_delay: float = DEFAULT_MAX_DELAY

    def __post_init__(self) -> None:
        """Validate field invariants.

        Raises:
            ValueError: If ``max_attempts < 1``, ``initial_delay < 0``, or
                ``max_delay < initial_delay``. Raised at construction time
                so the invalid policy cannot reach :func:`build_retry`.
        """
        if self.max_attempts < 1:
            raise ValueError(
                f"max_attempts must be >= 1, got {self.max_attempts}"
            )
        if self.initial_delay < 0:
            raise ValueError(
                f"initial_delay must be >= 0, got {self.initial_delay}"
            )
        if self.max_delay < self.initial_delay:
            raise ValueError(
                f"max_delay ({self.max_delay}) must be >= initial_delay "
                f"({self.initial_delay})"
            )


def policy_from_settings(
    retry_config: object | None,
    *,
    default: RetryPolicy | None = None,
) -> RetryPolicy:
    """Build a :class:`RetryPolicy` from a settings section, defensively.

    The loader, tests, and CLI all create :class:`RetryPolicy` from
    ``Settings.downloader.retry``. That section may be absent (fresh
    install), partially populated (user override), or fully populated (site
    override). This helper handles all three without forcing callers to
    write ``getattr`` chains.

    Every field falls back independently: a settings section that provides
    ``max_attempts`` but not ``max_delay`` yields a policy with the custom
    attempt count and the default delay. ``None`` for the whole section
    yields the module defaults.

    Args:
        retry_config: Any object exposing ``max_attempts``, ``initial_delay``,
            and/or ``max_delay`` attributes (typically
            ``Settings.downloader.retry``). ``None`` disables all overrides.
        default: Base policy whose values fill the gaps. When ``None``, the
            module defaults (:data:`DEFAULT_MAX_ATTEMPTS`,
            :data:`DEFAULT_INITIAL_DELAY`, :data:`DEFAULT_MAX_DELAY`) are
            used.

    Returns:
        A validated :class:`RetryPolicy`. Invalid values in *retry_config*
        (e.g. ``max_attempts=0``) raise :class:`ValueError` from
        :class:`RetryPolicy`'s post-init check rather than being silently
        coerced.
    """
    fallback = default if default is not None else RetryPolicy()
    return RetryPolicy(
        max_attempts=int(
            getattr(retry_config, "max_attempts", fallback.max_attempts)
        ),
        initial_delay=float(
            getattr(retry_config, "initial_delay", fallback.initial_delay)
        ),
        max_delay=float(
            getattr(retry_config, "max_delay", fallback.max_delay)
        ),
    )


# --------------------------------------------------------------------------- #
# Retry controller factory                                                    #
# --------------------------------------------------------------------------- #


def _log_retry(retry_state: RetryCallState) -> None:
    """Log a retry attempt at ``DEBUG`` level.

    Passed as the default ``before_sleep`` callback to every controller
    produced by :func:`build_retry`. Silent unless the operator enables
    ``DEBUG`` for the ``downloader.retry`` logger, so leaving it on by
    default costs nothing in production and gives full visibility when
    diagnosing a flaky site.

    Args:
        retry_state: Tenacity's snapshot of the retry loop. Provides
            ``attempt_number`` (1-based) and the failed ``outcome``.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if exc is None:
        _log.debug("Retrying after attempt {}", retry_state.attempt_number)
        return
    _log.debug(
        "Retrying after attempt {}: {}: {}",
        retry_state.attempt_number,
        type(exc).__name__,
        exc,
    )


def build_retry(
    policy: RetryPolicy,
    *,
    retry_predicate: Callable[[BaseException], bool] | None = None,
    before_sleep: Callable[[RetryCallState], None] = _log_retry,
) -> AsyncRetrying:
    """Build a fully-configured :class:`tenacity.AsyncRetrying`.

    The returned controller:

    * stops after ``policy.max_attempts`` attempts;
    * waits with exponential backoff + jitter, bounded by
      ``policy.initial_delay`` (first wait) and ``policy.max_delay`` (cap);
    * retries an exception only if :func:`is_retryable` (or *retry_predicate*
      when provided) returns ``True``;
    * re-raises the original exception when retries are exhausted
      (``reraise=True``), so callers see the underlying failure, not a
      :class:`tenacity.RetryError`.

    Usage::

        async for attempt in build_retry(policy):
            with attempt:
                await do_something()

        # If we reach here, either the operation succeeded or an
        # un-retryable exception (or a retried-out exception) propagated.

    Args:
        policy: The retry parameters.
        retry_predicate: Optional replacement for :func:`is_retryable`. Pass
            this to extend or narrow the classification **for one specific
            call site only** — for example, to retry a plugin-defined
            exception that only one parser raises. The replacement is
            expected to be a superset or subset of the default, not
            orthogonal to it.
        before_sleep: Called synchronously between a failed attempt and the
            next one. Defaults to :func:`_log_retry`, which logs at
            ``DEBUG``. Pass ``lambda _: None`` to silence it entirely (useful
            in tests).

    Returns:
        A ready-to-iterate :class:`tenacity.AsyncRetrying`.
    """
    predicate = retry_predicate if retry_predicate is not None else is_retryable
    return AsyncRetrying(
        stop=stop_after_attempt(policy.max_attempts),
        wait=wait_exponential_jitter(
            initial=policy.initial_delay,
            max=policy.max_delay,
        ),
        retry=retry_if_exception(predicate),
        before_sleep=before_sleep,
        reraise=True,
    )
