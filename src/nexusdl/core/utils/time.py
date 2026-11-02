"""Canonical time utilities for the entire NexusDL pipeline.

Every layer of NexusDL deals with time: the worker measures durations,
the library stores timestamps, the cache compares freshness deadlines,
the CLI formats durations for human output, and the API serialises
timestamps as ISO 8601. Historically each of those call sites did its
own arithmetic — ``datetime.now(UTC)``, ``.strftime("%Y-%m-%dT%H:%M:%S.%fZ")``,
``.timestamp()``, manual unit conversions — which worked until two call
sites diverged and produced different output for the same input.

This module centralises that logic behind a **small, focused API**.
Every function exists because a call site in the delivered code either
already uses the equivalent inline logic or is about to (a CLI helper
formatting a duration, a test mocking "now", the migration that will
replace ``database.py``'s private ``_utcnow_iso`` / ``_parse_iso``).

Guiding principles
------------------
* **UTC everywhere.** Every ``datetime`` produced by this module is
  timezone-aware and in UTC. Naive datetimes are rejected at the
  boundary — a naive datetime is ambiguous, and the ambiguity always
  masks a bug (the caller assumed local time, or assumed UTC without
  checking).
* **ISO 8601 with ``Z`` suffix.** Both :func:`utcnow_iso` and
  :func:`format_iso` produce the compact form
  ``"2024-01-15T10:30:00.123456Z"`` — the same form ``database.py``
  writes to SQLite and the form the API returns in JSON. The
  ``+00:00`` variant of ``datetime.isoformat()`` is accepted on input
  (via :func:`parse_iso`) but never produced on output.
* **Pure and synchronous.** No I/O, no logging, no async, no external
  dependencies. Every function is a pure transformation. This is what
  makes the module importable from anywhere — parsers, workers, CLI
  helpers, tests — without pulling in httpx or a database.
* **Inject clocks, don't mock modules.** Functions that conceptually
  need "now" accept a ``clock`` parameter (see :class:`Stopwatch`,
  :func:`format_relative`) so tests can advance time deterministically
  without ``freezegun`` or monkeypatching ``datetime``. The module-level
  :func:`utcnow` exists for the common case; tests inject their own
  clock into the classes they exercise.
* **Lenient parsing, strict formatting.** :func:`parse_iso` accepts
  every ISO 8601 variant ``datetime.fromisoformat`` handles, plus the
  ``Z`` suffix, plus date-only strings (treated as UTC midnight). The
  formatting functions raise on out-of-range input — a broken datetime
  reaching :func:`format_iso` or :func:`format_duration` is a caller
  bug, not a condition to silently absorb.

What this module deliberately does **not** do
---------------------------------------------
* **No local-time handling.** ``datetime.now()`` without ``tz=UTC`` is
  a bug that this module will never help mask. Callers that need local
  time convert at the presentation layer, not here.
* **No calendar arithmetic.** "One month later" is ambiguous (28–31
  days). Callers that need it use :mod:`dateutil.relativedelta`. This
  module provides only exact-second conversions and calendar-agnostic
  approximations for display.
* **No ``freezegun``.** Tests inject a ``clock`` callable rather than
  patching ``datetime`` globally.
* **No parsing of arbitrary human date formats.** Only ISO 8601.
  Callers with looser input formats (a config file using
  ``"2024/01/15"``) parse it themselves.

Migration note for ``database.py``
----------------------------------
``nexusdl.core.library.database`` currently defines private
``_utcnow_iso()`` and ``_parse_iso()`` helpers that duplicate the logic
here. When ``database.py`` is next opened:

* replace ``_utcnow_iso()`` with ``utcnow_iso()``
* replace ``_parse_iso(x)`` with ``parse_iso_or_none(x) or UNIX_EPOCH``

Both substitutions are pure behaviour-preserving — the on-disk format
and the lenient-on-invalid-input contract are identical.

Dependency graph (no cycles)::

    datetime, time (stdlib)

This module is a **leaf**: it imports nothing from ``nexusdl`` and
nothing from third parties. It could be lifted into a standalone
utility library without modification.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final


__all__ = [
    "SECONDS_PER_DAY",
    "SECONDS_PER_HOUR",
    "SECONDS_PER_MINUTE",
    "SECONDS_PER_WEEK",
    "Stopwatch",
    "UNIX_EPOCH",
    "format_duration",
    "format_iso",
    "format_relative",
    "from_unix",
    "parse_iso",
    "parse_iso_or_none",
    "to_unix",
    "utcnow",
    "utcnow_iso",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: The UNIX epoch as a timezone-aware UTC datetime. Used as the canonical
#: "unknown / missing timestamp" sentinel by callers (notably
#: ``database.py``'s timestamp parser, which returns this on malformed
#: input). Having a shared constant avoids dozens of inline
#: ``datetime(1970, 1, 1, tzinfo=UTC)`` constructions — and, more
#: importantly, ensures any comparison against "epoch" compares against
#: the same object everywhere.
UNIX_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

#: Seconds in one minute. Exposed so callers writing
#: ``datetime.now(UTC) + timedelta(minutes=15)`` or
#: ``now + 30 * SECONDS_PER_MINUTE`` share a single source of truth.
SECONDS_PER_MINUTE: Final[int] = 60

#: Seconds in one hour.
SECONDS_PER_HOUR: Final[int] = 3_600

#: Seconds in one day.
SECONDS_PER_DAY: Final[int] = 86_400

#: Seconds in one week.
SECONDS_PER_WEEK: Final[int] = 604_800

# Note: months and years are **not** exposed as constants because they are
# not exact in seconds. :func:`format_relative` uses internal approximations
# (30-day months, 365-day years) for display only — documented at the call
# site — but a public ``SECONDS_PER_MONTH`` constant would tempt callers
# into using it for arithmetic, which is wrong. See the module docstring.


# --------------------------------------------------------------------------- #
# "Now" and canonical formatting                                              #
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    This is the canonical "now" for the pipeline. A caller that needs a
    deterministic time (a test, a snapshot comparator) should either
    inject a ``clock`` into the class it exercises or mock this function
    — never mock ``datetime.now`` globally.

    Returns:
        ``datetime.now(UTC)``. Equivalent to ``datetime.now(tz=UTC)``
        but spelled as a single named call so that grepping for it finds
        every "current time" call site in one place.
    """
    return datetime.now(UTC)


def utcnow_iso() -> str:
    """Return the current time formatted as ISO 8601 with ``Z`` suffix.

    Produces exactly the format that
    :mod:`nexusdl.core.library.database` writes to SQLite:
    ``"2024-01-15T10:30:00.123456Z"``. The trailing ``Z`` (as opposed to
    ``+00:00``) is preferred for its brevity and for matching the
    common convention of HTTP ``Last-Modified`` / ``If-Modified-Since``
    headers.

    Returns:
        The current UTC time as an ISO 8601 string with millisecond-or-
        finer precision and a ``Z`` suffix. Always exactly the format
        produced by :func:`format_iso`.
    """
    return format_iso(utcnow())


def format_iso(dt: datetime) -> str:
    """Format a timezone-aware datetime as ISO 8601 with ``Z`` suffix.

    Accepts any tz-aware datetime regardless of its offset; the output
    is always in UTC. Naive datetimes are rejected — a naive datetime is
    ambiguous, and the ambiguity always masks a caller bug.

    Args:
        dt: The datetime to format. Must be timezone-aware.

    Returns:
        The datetime in canonical form:
        ``"2024-01-15T10:30:00.123456Z"``. Microseconds are always
        present (zero-padded to six digits) so that two formatted
        datetimes can be compared lexicographically.

    Raises:
        TypeError: If *dt* is not a :class:`datetime.datetime`.
        ValueError: If *dt* is timezone-naive.
    """
    if not isinstance(dt, datetime):
        raise TypeError(
            f"format_iso() expects a datetime, got "
            f"{type(dt).__name__}"
        )
    if dt.tzinfo is None:
        raise ValueError(
            "format_iso() requires a timezone-aware datetime; use "
            "dt.replace(tzinfo=UTC) if you know the value is UTC"
        )
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string into a timezone-aware UTC datetime.

    Handles every variant a well-behaved ISO 8601 producer emits:

    * ``"2024-01-15T10:30:00Z"`` — UTC, ``Z`` suffix.
    * ``"2024-01-15T10:30:00z"`` — lowercase ``z`` accepted.
    * ``"2024-01-15T10:30:00+00:00"`` — UTC with explicit offset.
    * ``"2024-01-15T10:30:00+05:30"`` — any offset; converted to UTC.
    * ``"2024-01-15T10:30:00"`` — no offset; treated as UTC.
    * ``"2024-01-15"`` — date-only; treated as UTC midnight.
    * ``"2024-01-15T10:30:00.123456Z"`` — fractional seconds, any
      precision up to microseconds.

    The result is **always** UTC — a ``+05:30`` input is converted, not
    preserved with its offset, so that every datetime in the pipeline is
    comparable via ``==`` / ``<`` / ``>`` without per-comparison
    ``.astimezone()`` calls.

    Unlike :func:`parse_iso_or_none`, this function **raises** on
    malformed input. Callers who have already validated the input (a
    database column with a ``CHECK`` constraint, an API request body
    after Pydantic validation) use this; callers who must tolerate a
    broken upstream use the lenient variant.

    Args:
        value: The ISO 8601 string to parse.

    Returns:
        A timezone-aware datetime in UTC.

    Raises:
        TypeError: If *value* is not a string.
        ValueError: If *value* is empty or not a recognised ISO 8601
            format.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"parse_iso() expects a string, got {type(value).__name__}"
        )
    if not value.strip():
        raise ValueError("cannot parse an empty ISO 8601 string")

    # ``datetime.fromisoformat`` in 3.11+ handles ``Z`` natively, but the
    # behaviour has varied across Python versions. Normalising the suffix
    # here keeps the parsing path independent of interpreter version.
    normalised = value.strip()
    if normalised.endswith("Z") or normalised.endswith("z"):
        normalised = normalised[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise ValueError(
            f"cannot parse {value!r} as ISO 8601: {exc}"
        ) from exc

    if dt.tzinfo is None:
        # ISO 8601 permits naive local times; the project rejects them
        # everywhere. Treat the naive input as UTC (the only sane
        # assumption for a library that deals exclusively in UTC) rather
        # than raising, because the caller already specified a string —
        # and a string without an offset is universally interpreted as
        # "the producer's local time", which for every source we support
        # is UTC.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_iso_or_none(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string, returning ``None`` on any failure.

    Lenient counterpart to :func:`parse_iso`. Intended for two use cases:

    * Database reads, where a historical row may contain a malformed
      timestamp that should not crash the whole query. The caller (see
      ``database.py``'s ``_parse_iso``) chooses the fallback via
      ``parse_iso_or_none(x) or UNIX_EPOCH``.
    * Optional fields, where ``None`` and "unparseable string" both mean
      "no timestamp", and the caller has no reason to distinguish.

    A caller that wants a specific fallback for malformed values uses
    :func:`parse_iso` inside a ``try`` — this function is the "I don't
    care, just give me ``None``" convenience, not the strict parser.

    Args:
        value: The ISO 8601 string to parse, or ``None``.

    Returns:
        A timezone-aware UTC datetime when *value* parses cleanly;
        ``None`` for ``None``, empty strings, whitespace, or any
        unparseable input.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    try:
        return parse_iso(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Unix epoch conversions                                                      #
# --------------------------------------------------------------------------- #


def to_unix(dt: datetime) -> float:
    """Convert a timezone-aware datetime to a UNIX timestamp (seconds).

    The returned value is a :class:`float` and includes fractional
    seconds. The input must be timezone-aware — a naive datetime's
    ``.timestamp()`` call assumes the *local* system timezone, which is
    almost never what a caller wants and, when it is, should be explicit
    (``dt.replace(tzinfo=local_tz).timestamp()``).

    Args:
        dt: The datetime to convert. Must be timezone-aware.

    Returns:
        Seconds since 1970-01-01T00:00:00Z, as a float.

    Raises:
        TypeError: If *dt* is not a :class:`datetime.datetime`.
        ValueError: If *dt* is timezone-naive.
    """
    if not isinstance(dt, datetime):
        raise TypeError(
            f"to_unix() expects a datetime, got {type(dt).__name__}"
        )
    if dt.tzinfo is None:
        raise ValueError(
            "to_unix() requires a timezone-aware datetime; use "
            "dt.replace(tzinfo=UTC) if you know the value is UTC"
        )
    return dt.timestamp()


def from_unix(timestamp: float) -> datetime:
    """Convert a UNIX timestamp to a timezone-aware UTC datetime.

    The inverse of :func:`to_unix`, with a narrower input type: only
    :class:`int` and :class:`float` are accepted. A string that happens
    to contain a number (``"1705326600"``) is rejected — the caller
    should parse it first, and doing so explicitly makes the intent
    visible.

    Args:
        timestamp: Seconds since 1970-01-01T00:00:00Z. May be negative
            (dates before the epoch) or fractional (sub-second
            precision).

    Returns:
        A timezone-aware UTC datetime.

    Raises:
        TypeError: If *timestamp* is not an int or float.
        OverflowError, OSError: From :func:`datetime.fromtimestamp` for
            values outside the representable range.
    """
    if isinstance(timestamp, bool) or not isinstance(
        timestamp, (int, float)
    ):
        raise TypeError(
            f"from_unix() expects a number, got "
            f"{type(timestamp).__name__}"
        )
    return datetime.fromtimestamp(float(timestamp), tz=UTC)


# --------------------------------------------------------------------------- #
# Duration formatting                                                         #
# --------------------------------------------------------------------------- #


def format_duration(seconds: float) -> str:
    """Format a duration as a compact human-readable string.

    Picks a unit scale based on the magnitude and shows at most two
    components. The output is designed for log lines, CLI progress
    output, and the ``duration_seconds`` field of
    :class:`~nexusdl.core.models.download.DownloadResult`.

    The magnitude-to-format mapping:

    ==================  ====================  ==========================
    Magnitude           Example output        Notes
    ==================  ====================  ==========================
    ``0``               ``"0s"``              Explicit zero.
    ``0 < s < 1``       ``"500ms"``           Integer milliseconds.
    ``1 ≤ s < 10``      ``"1.5s"``            One decimal place.
    ``10 ≤ s < 60``     ``"45s"``             Integer seconds.
    ``60 ≤ s < 3600``   ``"1m 30s"``          Minutes and seconds.
    ``1h ≤ s < 24h``    ``"2h 15m"``          Hours and minutes.
    ``1d ≤ s < 7d``     ``"3d 5h"``           Days and hours.
    ``7d ≤ s < 365d``   ``"2w 4d"``           Weeks and days.
    ``365d+``           ``"2y 3w"``           Years and weeks.
    ==================  ====================  ==========================

    A component that would be zero is omitted entirely (``"1h"`` rather
    than ``"1h 0m"``). For magnitudes above one minute the input is
    first rounded to the nearest whole second; sub-second precision is
    not meaningful at that scale and keeping it would either bloat the
    output or introduce off-by-one errors at unit boundaries.

    **Duration vs. relative time.** This function describes a *span*
    ("the task took 1h 30m"). For "when did this happen", use
    :func:`format_relative`, which describes a *point in time* relative
    to now.

    Args:
        seconds: The duration in seconds. Must be non-negative.

    Returns:
        A compact human-readable string, e.g. ``"1m 30s"``.

    Raises:
        TypeError: If *seconds* is not a number.
        ValueError: If *seconds* is negative.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError(
            f"format_duration() expects a number, got "
            f"{type(seconds).__name__}"
        )
    if seconds < 0:
        raise ValueError(
            f"format_duration() requires non-negative input, got "
            f"{seconds}"
        )

    # --- Zero and sub-second -------------------------------------- #
    if seconds == 0:
        return "0s"
    if seconds < 1:
        return f"{int(round(seconds * 1000))}ms"

    # --- Sub-10s: show one decimal place ------------------------- #
    if seconds < 10:
        if seconds == int(seconds):
            return f"{int(seconds)}s"
        return f"{seconds:.1f}s"

    # --- Round once, then compute every unit from the integer --- #
    total = int(round(seconds))

    if total < 60:
        return f"{total}s"

    minutes, secs = divmod(total, 60)
    if total < 3_600:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"

    hours, minutes = divmod(minutes, 60)
    if total < 86_400:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"

    days, hours = divmod(hours, 24)
    if total < 604_800:
        return f"{days}d" if hours == 0 else f"{days}d {hours}h"

    weeks, days = divmod(days, 7)
    if total < 31_536_000:
        return f"{weeks}w" if days == 0 else f"{weeks}w {days}d"

    # --- Multi-year: 52 weeks per year (approximate, display only) #
    years, weeks = divmod(weeks, 52)
    return f"{years}y" if weeks == 0 else f"{years}y {weeks}w"


def format_relative(
    dt: datetime,
    *,
    now: datetime | None = None,
) -> str:
    """Format a datetime relative to *now*: ``"5 minutes ago"`` / ``"in 2 hours"``.

    Picks a single-unit approximation. "About a year" is a coarser
    approximation than a caller who needs calendar arithmetic would
    want — that caller uses :mod:`dateutil.relativedelta`. This
    function is for UI: the human-readable "when" of a timestamp, not
    for precision.

    The unit-magnitude mapping (with ``Δ = |now − dt|``):

    * ``Δ < 1s`` → ``"just now"`` (past) / ``"in a moment"`` (future).
    * ``1s ≤ Δ < 60s`` → ``"N seconds ago"`` / ``"in N seconds"``.
    * ``1m ≤ Δ < 1h`` → ``"N minutes ago"`` / ``"in N minutes"``.
    * ``1h ≤ Δ < 1d`` → ``"N hours ago"`` / ``"in N hours"``.
    * ``1d ≤ Δ < 7d`` → ``"N days ago"`` / ``"in N days"``.
    * ``1w ≤ Δ < 30d`` → ``"N weeks ago"`` / ``"in N weeks"``.
    * ``30d ≤ Δ < 365d`` → ``"N months ago"`` / ``"in N months"``.
    * ``Δ ≥ 365d`` → ``"N years ago"`` / ``"in N years"``.

    Months are approximated as 30 days, years as 365 days. Documented
    and deliberate: the alternative — showing exact calendar
    arithmetic — would require a date library for a feature whose only
    consumer is a UI label.

    Args:
        dt: The datetime to describe.
        now: The reference time. Defaults to :func:`utcnow`. Passing an
            explicit value makes the output deterministic; used by
            tests and by callers producing a snapshot.

    Returns:
        A human-readable relative-time string.

    Raises:
        TypeError: If *dt* or *now* is not a :class:`datetime.datetime`.
        ValueError: If *dt* or *now* is timezone-naive.
    """
    if not isinstance(dt, datetime):
        raise TypeError(
            f"format_relative() expects a datetime, got "
            f"{type(dt).__name__}"
        )
    if dt.tzinfo is None:
        raise ValueError(
            "format_relative() requires a timezone-aware datetime"
        )
    if now is None:
        now = utcnow()
    elif not isinstance(now, datetime):
        raise TypeError(
            f"format_relative() expects 'now' to be a datetime, got "
            f"{type(now).__name__}"
        )
    if now.tzinfo is None:
        raise ValueError(
            "format_relative() requires 'now' to be timezone-aware"
        )

    delta = (now - dt).total_seconds()
    future = delta < 0
    magnitude = abs(delta)

    if magnitude < 1:
        return "in a moment" if future else "just now"

    if magnitude < 60:
        value, unit = int(magnitude), "second"
    elif magnitude < 3_600:
        value, unit = int(magnitude // 60), "minute"
    elif magnitude < 86_400:
        value, unit = int(magnitude // 3_600), "hour"
    elif magnitude < 604_800:
        value, unit = int(magnitude // 86_400), "day"
    elif magnitude < 2_592_000:  # 30 days
        value, unit = int(magnitude // 604_800), "week"
    elif magnitude < 31_536_000:  # 365 days
        value, unit = int(magnitude // 2_592_000), "month"
    else:
        value, unit = int(magnitude // 31_536_000), "year"

    # Guard against the ``int()`` floor producing 0 just past a unit
    # boundary (e.g. magnitude=59.99 → 0 minutes). The correct answer is
    # "1 minute ago", which the branch above catches only for
    # magnitude < 60; this keeps the fallback safe.
    if value == 0:
        value = 1

    plural = "" if value == 1 else "s"
    if future:
        return f"in {value} {unit}{plural}"
    return f"{value} {unit}{plural} ago"


# --------------------------------------------------------------------------- #
# Stopwatch                                                                   #
# --------------------------------------------------------------------------- #


class Stopwatch:
    """A monotonic stopwatch with reset and human-readable output.

    Wraps a monotonic clock with a minimal API, so call sites that need
    to measure a duration do not each construct their own
    ``time.monotonic()`` pair and each invent their own formatting. The
    default clock is :func:`time.monotonic` — wall-clock changes (NTP
    adjustments, DST) do not affect measurements.

    Usage as a context manager::

        with Stopwatch() as sw:
            await do_work()
        logger.info("Work took {}", sw.elapsed_human())

    Usage as an explicit object::

        sw = Stopwatch()
        await step_one()
        logger.debug("step one: {}", sw.elapsed_human())
        sw.reset()
        await step_two()
        logger.debug("step two: {}", sw.elapsed_human())

    Args:
        clock: Callable returning a monotonic float in seconds. Defaults
            to :func:`time.monotonic`. Inject a fake clock in tests to
            measure durations deterministically without sleeping.
    """

    __slots__ = ("_clock", "_start")

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._start = clock()

    # ------------------------------------------------------------------ #
    # Context manager protocol                                           #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> Stopwatch:
        """Return ``self`` so the caller can read ``elapsed`` on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Do nothing — the elapsed time is read after the ``with`` block."""
        # No cleanup required; the elapsed value is available via
        # ``elapsed()`` / ``elapsed_human()`` for as long as the caller
        # holds the reference.

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def reset(self) -> None:
        """Reset the start time to the current clock reading.

        The elapsed time restarts at zero. Safe to call mid-measurement.
        """
        self._start = self._clock()

    def elapsed(self) -> float:
        """Return the seconds since the last reset (or construction).

        Returns:
            A non-negative float. The clock is monotonic, so the value
            is guaranteed to increase between calls (up to the clock's
            resolution).
        """
        return self._clock() - self._start

    def elapsed_ms(self) -> int:
        """Return the elapsed time in whole milliseconds."""
        return int(self.elapsed() * 1000)

    def elapsed_human(self) -> str:
        """Return the elapsed time formatted by :func:`format_duration`."""
        return format_duration(self.elapsed())

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return f"Stopwatch(elapsed={self.elapsed():.3f}s)"
