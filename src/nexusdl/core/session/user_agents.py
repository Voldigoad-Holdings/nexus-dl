"""User-Agent rotation for HTTP requests.

Every request NexusDL makes carries a ``User-Agent`` header. Some sites
behave differently based on it (returning different markup, throttling
differently, or blocking known-bot agents outright), and a session that
sends the *same* UA for every request is trivially fingerprintable even
when every other header rotates. This module provides the abstraction
the :class:`~nexusdl.core.session.http_session.HttpSession` uses to pick
a UA per request.

Two strategies, one class
-------------------------
:class:`UserAgentRotator` is a small, thread-safe pool with two rotation
strategies:

* ``"random"`` (default) — picks a UA uniformly at random on every call.
  Unpredictable, good for evading per-UA rate limits when the site
  silently throttles known agents.
* ``"round_robin"`` — cycles through the pool in submission order. Fully
  deterministic, which makes tests trivial and gives even coverage of
  the pool when the caller wants to exercise every UA exactly once.

Per-domain stickiness
---------------------
In addition to the per-request strategy, the rotator supports an
optional **sticky** behaviour via :meth:`UserAgentRotator.for_domain`:
the same UA is returned for the same domain for up to
:data:`DEFAULT_STICKY_TTL` seconds. This matters for sites that
fingerprint by *consistency* within a session (a UA that suddenly changes
mid-session is a bot signal), while still rotating across the pool over
longer time windows.

The :class:`~nexusdl.core.session.http_session.HttpSession` uses
``for_domain()`` by default. Callers that want pure per-request rotation
call :meth:`UserAgentRotator.next`.

Pool sources
------------
Three sources, in priority order:

1. An explicit sequence passed to the constructor — used by tests and by
   a caller that wants its own pool.
2. The bundled ``data/user_agents.txt`` inside :mod:`nexusdl.data` —
   loaded by :func:`load_user_agents` on first use. Contains a large
   realistic pool (dozens of recent Chrome / Firefox / Safari / Edge UAs
   across Windows / macOS / Linux / Android / iOS).
3. :data:`DEFAULT_USER_AGENTS` — a small hardcoded fallback (8 UAs) used
   when the bundled file is unreadable. This lets the rotator work even
   in a broken installation; it exists so that a single missing package
   data file does not take down the whole pipeline.

Format of the file
------------------
One UA per line. Empty lines and lines starting with ``#`` are ignored.
Whitespace around each line is stripped. Duplicates are removed while
preserving the first occurrence's position.

Thread safety
-------------
The rotator uses a single :class:`threading.Lock` to protect the
round-robin counter and the per-domain cache. Every critical section is
a handful of dictionary operations and one RNG call — microseconds.
This makes the class safe to share across coroutines in a single event
loop **and** across worker threads when a caller forwards the rotator
through :func:`asyncio.to_thread`. It does not make the underlying
session thread-safe; that is a separate concern the session owns.

Dependency graph (no cycles)::

    loguru  ──► core.session.user_agents
    importlib.resources
    random, threading, time (stdlib)

No ``nexusdl.core`` import. This module is a leaf and could be lifted
into a standalone library without modification.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Final, Literal

from loguru import logger


__all__ = [
    "DEFAULT_STICKY_TTL",
    "DEFAULT_USER_AGENTS",
    "RotationStrategy",
    "USER_AGENTS_FILENAME",
    "UserAgentRotator",
    "load_user_agents",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the bundled user-agent pool inside :data:`_DATA_PACKAGE`.
USER_AGENTS_FILENAME: Final[str] = "user_agents.txt"

#: Python package that ships the bundled UA file.
_DATA_PACKAGE: Final[str] = "nexusdl.data"

#: Default duration, in seconds, during which :meth:`UserAgentRotator.for_domain`
#: returns the same UA for a given domain. Five minutes is long enough to
#: survive a single chapter download (typically 30 s – 2 min) and short
#: enough that a long-running process still cycles through the pool.
DEFAULT_STICKY_TTL: Final[float] = 300.0

#: Rotation strategy accepted by :class:`UserAgentRotator`.
#:
#: Using a :class:`~typing.Literal` rather than a :class:`~enum.StrEnum`
#: keeps the surface small — this is a session-internal knob, not a
#: domain concept. ``mypy --strict`` catches a typo in the caller at
#: compile time, and the constructor validates at runtime for
#: dynamically typed callers.
RotationStrategy = Literal["random", "round_robin"]

#: Hardcoded fallback pool, used when the bundled ``user_agents.txt``
#: cannot be loaded. Eight realistic, recent UAs across the four major
#: browser engines and three operating systems.
DEFAULT_USER_AGENTS: Final[tuple[str, ...]] = (
    # Chrome 124 on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox 125 on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Firefox 125 on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Firefox 125 on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    # Safari 17 on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4.1 Safari/605.1.15",
    # Edge 124 on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "Edg/124.0.2478.97",
)


_log = logger.bind(component="session.user_agents")


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #


def load_user_agents(
    path: Path | None = None,
    *,
    fallback_to_defaults: bool = True,
) -> tuple[str, ...]:
    """Load a User-Agent pool from a file.

    In both modes, the file is parsed with :func:`_parse_lines` — one UA
    per line, ``#`` comments and blank lines ignored, duplicates removed.

    Args:
        path: Path to a ``.txt`` file, one UA per line. When ``None``,
            the bundled ``data/user_agents.txt`` inside
            :mod:`nexusdl.data` is loaded. A caller passing an explicit
            path typically expects that file to exist; use
            ``fallback_to_defaults=False`` to make a missing file an
            error rather than a silent fallback.
        fallback_to_defaults: When ``True`` (the default) and the file
            cannot be read, log a warning and return
            :data:`DEFAULT_USER_AGENTS` instead of raising. When
            ``False``, a read failure raises :class:`FileNotFoundError`
            or :class:`OSError` from the underlying :meth:`Path.read_text`.

    Returns:
        A non-empty tuple of User-Agent strings. Never empty — if the
        file is empty or contains only comments, the fallback logic
        applies (or :class:`ValueError` is raised if
        ``fallback_to_defaults=False``).

    Raises:
        FileNotFoundError, OSError: When the file cannot be read and
            ``fallback_to_defaults=False``.
        ValueError: When the file is read successfully but contains no
            usable UA and ``fallback_to_defaults=False``. A caller who
            disabled fallback explicitly asked for the file's content —
            an empty file is a configuration bug worth surfacing.
    """
    try:
        if path is None:
            resource = _resource_files(_DATA_PACKAGE) / USER_AGENTS_FILENAME
            text = resource.read_text(encoding="utf-8")
        else:
            text = path.read_text(encoding="utf-8")
    except (ModuleNotFoundError, OSError) as exc:
        if not fallback_to_defaults:
            raise
        _log.warning(
            "Could not load user agents from {}: {}: {} — using "
            "the built-in fallback pool",
            "bundle" if path is None else str(path),
            type(exc).__name__,
            exc,
        )
        return DEFAULT_USER_AGENTS

    parsed = _parse_lines(text)
    if parsed:
        return parsed

    if not fallback_to_defaults:
        raise ValueError(
            f"user-agent file {path or 'bundle'} contains no usable entries"
        )
    _log.warning(
        "User-agent file {} is empty after parsing; using the built-in "
        "fallback pool",
        "bundle" if path is None else str(path),
    )
    return DEFAULT_USER_AGENTS


def _parse_lines(text: str) -> tuple[str, ...]:
    """Parse a UA pool file into a deduplicated tuple, preserving order.

    Args:
        text: Raw file content.

    Returns:
        The list of unique, trimmed, non-comment, non-empty lines.
        Duplicates are collapsed to their first occurrence; the order is
        the order of first appearance.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        unique.append(line)
    return tuple(unique)


# --------------------------------------------------------------------------- #
# UserAgentRotator                                                            #
# --------------------------------------------------------------------------- #


class UserAgentRotator:
    """Pick a User-Agent per request, with optional per-domain stickiness.

    Stateless callers construct one instance per session and reuse it for
    the session's lifetime. The instance is safe to share across
    coroutines and worker threads.

    See the module docstring for the strategies, the pool sources, and
    the ``for_domain`` stickiness contract.

    Example:
        >>> rotator = UserAgentRotator()
        >>> rotator.next()                       # random pick
        'Mozilla/5.0 (Windows NT 10.0; ...'
        >>> rotator.for_domain("mangadex.org")   # sticky for 5 min
        'Mozilla/5.0 (Macintosh; Intel Mac OS X ...'
        >>> rotator.for_domain("mangadex.org")   # same as above
        'Mozilla/5.0 (Macintosh; Intel Mac OS X ...'

    Args:
        user_agents: Explicit pool. When ``None`` (the default), the pool
            is loaded from the bundled ``data/user_agents.txt`` via
            :func:`load_user_agents` — which falls back to
            :data:`DEFAULT_USER_AGENTS` if the bundled file is unreadable.
            Passing an explicit sequence (including
            ``DEFAULT_USER_AGENTS``) skips the file read entirely and is
            the right choice in tests.
        strategy: ``"random"`` (default) or ``"round_robin"``. Any other
            value raises :class:`ValueError`.
        sticky_ttl: Duration, in seconds, that :meth:`for_domain` keeps
            the same UA for a given domain. ``None`` disables stickiness
            — :meth:`for_domain` then behaves like :meth:`next`. ``0.0``
            is effectively the same as ``None`` (every call sees an
            expired entry and picks a fresh UA). Must be ``>= 0`` when
            not ``None``.
        rng: Optional :class:`random.Random` instance. Injecting one
            gives deterministic sequences in tests via a fixed seed.
            Defaults to a module-private ``Random()`` seeded by the OS.
        clock: Callable returning a monotonic float, used to expire
            per-domain sticky entries. Defaults to
            :func:`time.monotonic`. Injectable for tests that want to
            manipulate time without sleeping.

    Raises:
        ValueError: If *strategy* is unknown, if *sticky_ttl* is negative,
            or if the pool (after loading and deduplication) is empty.
    """

    __slots__ = (
        "_clock",
        "_domain_cache",
        "_lock",
        "_pool",
        "_rng",
        "_round_robin_index",
        "_sticky_ttl",
        "_strategy",
    )

    def __init__(
        self,
        user_agents: Sequence[str] | None = None,
        *,
        strategy: RotationStrategy = "random",
        sticky_ttl: float | None = DEFAULT_STICKY_TTL,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if strategy not in ("random", "round_robin"):
            raise ValueError(
                f"strategy must be 'random' or 'round_robin', "
                f"got {strategy!r}"
            )
        if sticky_ttl is not None and sticky_ttl < 0:
            raise ValueError(
                f"sticky_ttl must be >= 0 or None, got {sticky_ttl}"
            )

        raw_pool: Sequence[str] = (
            user_agents if user_agents is not None else load_user_agents()
        )
        # Validate and normalise in one pass. An empty pool after
        # normalisation is a programming error — even an explicit pool
        # passed by a caller must contain at least one UA.
        normalised: list[str] = []
        seen: set[str] = set()
        for ua in raw_pool:
            if not isinstance(ua, str):
                raise TypeError(
                    f"user_agents entries must be str, got "
                    f"{type(ua).__name__}"
                )
            stripped = ua.strip()
            if not stripped:
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            normalised.append(stripped)

        if not normalised:
            raise ValueError(
                "user-agent pool is empty after validation and deduplication"
            )

        self._pool: tuple[str, ...] = tuple(normalised)
        self._strategy: RotationStrategy = strategy
        self._sticky_ttl: float | None = sticky_ttl
        self._rng: random.Random = rng if rng is not None else random.Random()
        self._clock: Callable[[], float] = clock
        self._lock = threading.Lock()
        self._round_robin_index: int = 0
        #: Map domain → (ua, expiry_deadline_monotonic).
        self._domain_cache: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def next(self) -> str:
        """Return the next User-Agent per the configured strategy.

        With ``"round_robin"``, cycles through :attr:`pool` in order.
        With ``"random"``, picks uniformly at random.

        Returns:
            A non-empty User-Agent string, always a member of the pool.
        """
        with self._lock:
            return self._next_locked()

    def for_domain(self, domain: str) -> str:
        """Return a User-Agent that is sticky for *domain* for ``sticky_ttl``.

        Within the TTL window, returns the same UA on every call for the
        same *domain*. After the TTL expires, picks a fresh UA and starts
        a new window. When ``sticky_ttl`` is ``None`` or ``0.0``, this
        method is equivalent to :meth:`next`.

        Args:
            domain: The domain key. Conventionally the request URL's
                hostname (``urlparse(url).netloc``), but any string works
                — the rotator treats it as an opaque cache key. An empty
                string disables stickiness for that call (delegates to
                :meth:`next`) so that a caller who could not extract a
                domain still gets a UA.

        Returns:
            A User-Agent string, stable for the domain during the TTL.
        """
        if not domain or self._sticky_ttl is None or self._sticky_ttl == 0:
            return self.next()

        now = self._clock()
        with self._lock:
            cached = self._domain_cache.get(domain)
            if cached is not None and cached[1] > now:
                return cached[0]
            ua = self._next_locked()
            self._domain_cache[domain] = (ua, now + self._sticky_ttl)
            return ua

    def reset(self) -> None:
        """Reset the round-robin counter and clear all sticky entries.

        Intended for tests and for an explicit "start a new session"
        action. The pool, strategy, RNG, and clock are preserved.
        """
        with self._lock:
            self._round_robin_index = 0
            self._domain_cache.clear()

    @property
    def pool(self) -> tuple[str, ...]:
        """The current User-Agent pool, in deduplicated order.

        Exposed read-only so that a caller (a diagnostics endpoint, a
        test asserting rotation coverage) can inspect the pool without
        reaching into a private attribute. The tuple is stable; the
        rotator never mutates ``self._pool`` after construction.
        """
        return self._pool

    def __len__(self) -> int:
        """Return the number of User-Agents in the pool."""
        return len(self._pool)

    def __repr__(self) -> str:
        """Return a compact debug representation.

        The pool contents are deliberately omitted — a UA is a few
        hundred characters, and printing eight of them floods log lines.
        Only the strategy, size, and stickiness setting are shown.
        """
        return (
            f"UserAgentRotator(size={len(self._pool)}, "
            f"strategy={self._strategy!r}, "
            f"sticky_ttl={self._sticky_ttl})"
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _next_locked(self) -> str:
        """Return the next UA, assuming the caller holds the lock."""
        if self._strategy == "round_robin":
            ua = self._pool[self._round_robin_index % len(self._pool)]
            self._round_robin_index = (self._round_robin_index + 1) % len(
                self._pool
            )
            return ua
        # "random" strategy.
        return self._rng.choice(self._pool)
