"""Optional ``robots.txt`` compliance for HTTP fetching.

Some source sites publish a ``robots.txt`` that lists paths automated
clients must not visit. NexusDL's default posture is to **respect it**
when the operator has not explicitly disabled compliance — the same
default every well-behaved scraper (``scrapy``, ``httpx-robots``,
``requests-robots``) ships with. A deployment that owns the site, or that
has obtained explicit permission, sets ``NEXUSDL_ROBOTS_TXT=false`` and
the checker becomes a no-op.

What this module implements
---------------------------
* **RFC 9309 ``robots.txt`` parser** — ``User-agent``, ``Allow``,
  ``Disallow``, ``Crawl-delay`` directives, with the "most specific
  match wins, ``Allow`` breaks ties" precedence rule. Wildcards ``*``
  and end-anchor ``$`` are supported (Googlebot / Bingbot common
  extensions). ``Sitemap`` and other directives are ignored.
* **Per-site cache** with a configurable TTL (default 24 hours,
  matching the RFC's "MAY cache" guidance). The cache key is the
  ``site_id`` when provided, the URL's hostname otherwise — so a caller
  that knows the site identifier gets cache stability across URL
  rotations and mirror domains, while a caller that does not still
  benefits from per-host caching.
* **RFC 9309 fetch semantics**:

  - ``200`` → parse and enforce.
  - ``401`` / ``403`` → **disallow all** (RFC 9309 §2.3.1.3
    "Unauthorized").
  - Other ``4xx`` → **allow all** (RFC 9309 §2.3.1.3 "Unavailable").
  - ``5xx``, network failure, timeout → **allow all** (RFC 9309
    §2.3.1.4 "Unreachable"). A site whose ``robots.txt`` is broken is
    not the site that wants to be left alone; it is the site whose
    operators have a bug to fix. This case is also tracked as a
    **soft-fail** and surfaced as :attr:`ComplianceStatus.UNKNOWN`
    by :meth:`ComplianceChecker.get_status`.
  - Parse failure on a 200 response → **allow all** (soft-fail), with
    a log line.

* **The :class:`ComplianceError` raise is opt-in via** :meth:`check`.
  Callers that want strict refusal call :meth:`check`; callers that
  want to inspect the verdict themselves call :meth:`is_allowed`.
  Both are async — the first call for a site triggers a ``robots.txt``
  fetch.

What this module deliberately does **not** do
---------------------------------------------
* **No ``Crawl-delay`` enforcement.** The delay is **exposed** via
  :meth:`get_crawl_delay` so the composition root can wire it into the
  session's rate limiter, but the checker itself never waits. Waiting
  is a scheduling concern that belongs to
  :mod:`nexusdl.core.session.rate_limiter`, not to a stateless
  compliance check.
* **No automatic HTTP session construction.** The checker is injected
  with a session that satisfies :class:`HttpSessionProtocol` — a
  narrow three-method contract, satisfied by
  :class:`~nexusdl.core.session.http_session.HttpSession` and by
  trivial test doubles. This module never imports ``httpx``, and could
  not construct a client even if it wanted to.
* **No request-level integration.** The checker is **not** wired into
  :class:`~nexusdl.core.session.http_session.HttpSession`. Every HTTP
  call site that wants compliance checking calls :meth:`check` itself
  before issuing the request — typically the parser's ``get_manga`` /
  ``get_chapters`` / ``get_pages`` entry points. An automatic
  integration would either require modifying ``HttpSession`` (out of
  scope for a module that ships after it) or add coupling between the
  session and a compliance policy the session has no business knowing
  about.
* **No global policy.** Each :class:`ComplianceChecker` instance is
  scoped to the caller that constructs it. A library that wants
  compliance has one checker; a library that explicitly opts out
  constructs one with ``enabled=False``. No global variable, no
  environment read at import time.
* **No ``Sitemap``, ``Host``, ``Request-rate``, or other non-RFC-9309
  directives.** They are parsed and ignored. A caller who needs them
  inspects the raw text via :meth:`get_robots_txt_text`.

Design invariants
-----------------
* **Soft fail, never crash.** A ``robots.txt`` that cannot be fetched,
  times out, returns a ``5xx``, or contains garbage is treated as
  "allow all" and logged. A compliance check that brings the pipeline
  down is worse than no compliance check at all.
* **Cancellation propagates.** ``asyncio.CancelledError`` from the
  underlying session fetch propagates untouched — a cancelled
  compliance check leaves the cache unchanged and re-raises to the
  caller.
* **The cache is shared across coroutines.** A single
  :class:`asyncio.Lock` guards the cache dict. The fetch itself runs
  **outside** the lock, so a slow fetch for one site does not block
  checks for another. The trade-off is a possible duplicate fetch when
  two coroutines race on the same site — accepted, because
  ``robots.txt`` is idempotent and the duplicate costs one request per
  cache miss.
* **Deterministic parsing.** :meth:`RobotsTxt.parse` is a pure function
  from text to rules; no I/O, no exceptions on malformed input. A
  caller that wants to test the parser does not need a running HTTP
  session.

Dependency graph (no cycles)::

    core.exceptions  ──►  core.compliance
    loguru
    asyncio, re, time, urllib.parse (stdlib)

``core.exceptions`` is imported for the one exception class
:meth:`ComplianceChecker.check` raises —
:class:`~nexusdl.core.exceptions.ComplianceError`. That class is
documented as non-retryable in
:mod:`nexusdl.core.downloader.retry`'s classification:
``_PERMANENT_ERRORS`` includes ``ComplianceError``. Retrying a
disallowed URL wastes time and, worse, continues to violate the site's
declared policy. Callers must not catch and retry
:class:`ComplianceError` — if the URL is disallowed, the correct
response is to skip it, not to hammer it.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol
from urllib.parse import ParseResult, urlparse

from loguru import logger

from nexusdl.core.exceptions import ComplianceError


__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "ComplianceChecker",
    "ComplianceStats",
    "ComplianceStatus",
    "HttpSessionProtocol",
    "RobotsRule",
    "RobotsTxt",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default TTL for a cached ``robots.txt``, in seconds. 24 hours matches
#: the RFC 9309 §2.4 guidance ("MUST consider the cached version stale
#: after a reasonable period") and the de-facto standard set by every
#: major crawler.
DEFAULT_CACHE_TTL_SECONDS: Final[float] = 86_400.0

#: Default timeout for the ``robots.txt`` fetch, in seconds. Shorter than
#: the pipeline's normal request timeout because a compliance check is a
#: *gate* — a slow fetch delays every request the caller wants to make.
#: Five seconds is enough for a normal ``robots.txt`` and short enough
#: that a broken site does not stall the pipeline.
DEFAULT_FETCH_TIMEOUT_SECONDS: Final[float] = 5.0

#: User-Agent string the checker sends when fetching ``robots.txt`` and
#: matches rules against when none is explicitly configured. ``"*"``
#: matches the wildcard group, which is what every ``robots.txt`` must
#: contain. Callers that want specific-group enforcement pass a bot name
#: (``"NexusDL"``) so that a site which lists ``User-agent: NexusDL`` can
#: grant NexusDL access that it denies to ``*``.
DEFAULT_USER_AGENT: Final[str] = "*"

#: Directives the parser recognises. Any other directive is ignored
#: silently — the RFC allows extensions the parser does not need to
#: understand.
_DIRECTIVE_USER_AGENT: Final[str] = "user-agent"
_DIRECTIVE_ALLOW: Final[str] = "allow"
_DIRECTIVE_DISALLOW: Final[str] = "disallow"
_DIRECTIVE_CRAWL_DELAY: Final[str] = "crawl-delay"

_log = logger.bind(component="compliance")


# --------------------------------------------------------------------------- #
# ComplianceStatus                                                            #
# --------------------------------------------------------------------------- #


class ComplianceStatus(StrEnum):
    """The verdict of a compliance check.

    Not returned by :meth:`ComplianceChecker.check` (which raises on
    ``DISALLOWED`` or returns ``None`` on ``ALLOWED``) — that is the
    primary API. The status is exposed so that a caller inspecting the
    result of :meth:`ComplianceChecker.get_status` can branch on the
    three-way distinction without inferring it from a bool plus a
    "have we fetched yet?" flag.
    """

    ALLOWED = "ALLOWED"
    DISALLOWED = "DISALLOWED"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# RobotsRule / RobotsTxt                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RobotsRule:
    """One ``Allow`` or ``Disallow`` line from a ``robots.txt`` group.

    Immutable. The parsed rule carries both the raw pattern (for
    logging and debugging) and the compiled regex (for matching). The
    ``specificity`` field is precomputed so the tie-breaking logic in
    :meth:`RobotsTxt.is_allowed` is a single comparison.

    Attributes:
        allow: ``True`` for an ``Allow`` line, ``False`` for ``Disallow``.
        pattern: The pattern as written in the file, without any
            normalisation. ``"/"`` and ``"/foo/*"`` are preserved
            verbatim. A bare ``Disallow:`` (empty pattern) is
            represented as ``""`` and matches nothing — see
            :func:`_compile_pattern`.
        pattern_regex: The compiled regex derived from :attr:`pattern`.
            A pattern containing ``*`` becomes a wildcard; a pattern
            ending in ``$`` is end-anchored. Every other character is
            escaped.
        specificity: A numeric tie-breaker, computed as the length of
            :attr:`pattern` with all wildcards and the ``$`` anchor
            removed. Higher is more specific. The RFC 9309 §2.2.2 rule
            — "the most specific match, based on the length of the
            pattern" — is implemented on this value.
    """

    allow: bool
    pattern: str
    pattern_regex: re.Pattern[str]
    specificity: int

    @classmethod
    def build(cls, allow: bool, pattern: str) -> RobotsRule:
        """Construct a :class:`RobotsRule` from raw input, precompiling.

        Args:
            allow: ``True`` for an ``Allow`` directive, ``False`` for
                ``Disallow``.
            pattern: The raw pattern string.

        Returns:
            A fully-constructed rule.
        """
        regex = _compile_pattern(pattern)
        specificity = len(pattern.replace("*", "").replace("$", ""))
        return cls(
            allow=allow,
            pattern=pattern,
            pattern_regex=regex,
            specificity=specificity,
        )

    def matches(self, path: str) -> bool:
        """Return ``True`` iff *path* matches this rule's pattern.

        Args:
            path: The request path, **including** any query string.
                A URL ``https://example.org/search?q=x`` has path
                ``"/search?q=x"``. The rule's regex is anchored at the
                beginning; the ``$`` anchor additionally requires the
                match to consume the entire path.
        """
        return self.pattern_regex.match(path) is not None


@dataclass(frozen=True, slots=True)
class RobotsTxt:
    """A parsed ``robots.txt`` group for one ``User-agent``.

    Immutable. Produced by :meth:`RobotsTxt.parse`. Empty when the file
    contained no group matching the requested user-agent (which
    :meth:`parse` turns into :meth:`allow_all`, the correct default
    per RFC 9309).

    Attributes:
        rules: The ordered tuple of Allow/Disallow rules applicable to
            the user-agent. Empty means "no rule matched", which is
            equivalent to :meth:`allow_all` behaviour.
        crawl_delay: The ``Crawl-delay`` value, in seconds, from the
            same group, or ``None`` if absent. Exposed to callers via
            :meth:`ComplianceChecker.get_crawl_delay` but **not**
            enforced by this module — the rate limiter is the right
            place for enforcement.
    """

    rules: tuple[RobotsRule, ...]
    crawl_delay: float | None = None

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> RobotsTxt:
        """Parse a ``robots.txt`` document, extracting the matching group.

        The parser honours RFC 9309 §2.2.1 group semantics:

        * Groups are separated by a ``User-agent`` line followed by at
          least one non-``User-agent`` directive.
        * A group applies to the request if its user-agent token
          matches (case-insensitive, with ``"*"`` as a wildcard) — or if
          the token is a **substring** of the request's UA, matching the
          RFC's "product token" semantics.
        * If multiple groups match, the **most specific** group wins: a
          group with an explicit token beats ``"*"``. When multiple
          explicit tokens match, the one with the **longest** token
          wins. (Real-world ``robots.txt`` files rarely contain more
          than one explicit token; the tie-break exists for
          correctness, not for common use.)

        Directive names and values are case-insensitive; the parser
        lowercases directive names and preserves values verbatim. Lines
        are split on ``\\r\\n``, ``\\n``, or ``\\r``. Blank lines and
        lines starting with ``#`` are comments and are ignored.
        Directives other than ``Allow``, ``Disallow``, and
        ``Crawl-delay`` are silently dropped.

        The parser is **total** — it never raises. A syntactically
        broken file yields an empty :class:`RobotsTxt`, which is
        equivalent to "allow all", the safe default.

        Args:
            text: The raw ``robots.txt`` body.
            user_agent: The user-agent to match groups against.
                ``"*"`` (the default) matches only the wildcard group.
                A specific token (``"NexusDL"``) prefers the matching
                group when present, falling back to ``"*"``.

        Returns:
            A :class:`RobotsTxt` with the rules applicable to
            *user_agent* and the matching group's ``crawl_delay``.
        """
        if not text:
            return cls.allow_all()

        groups = _iter_groups(text)
        selected = _select_group(groups, user_agent)
        if selected is None:
            return cls.allow_all()

        rules: list[RobotsRule] = []
        crawl_delay: float | None = None
        for directive, value in selected:
            if directive == _DIRECTIVE_ALLOW:
                rules.append(RobotsRule.build(allow=True, pattern=value))
            elif directive == _DIRECTIVE_DISALLOW:
                # ``Disallow:`` with an empty value is technically
                # "allow all" per RFC 9309 §2.2.2 — an empty pattern
                # matches nothing. Skip it rather than building a rule
                # that never fires.
                if not value:
                    continue
                rules.append(
                    RobotsRule.build(allow=False, pattern=value)
                )
            elif directive == _DIRECTIVE_CRAWL_DELAY:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if parsed >= 0:
                    crawl_delay = parsed

        return cls(rules=tuple(rules), crawl_delay=crawl_delay)

    @classmethod
    def allow_all(cls) -> RobotsTxt:
        """Return a :class:`RobotsTxt` with no rules (equivalent to ``Allow: /``)."""
        return cls(rules=(), crawl_delay=None)

    @classmethod
    def disallow_all(cls) -> RobotsTxt:
        """Return a :class:`RobotsTxt` that refuses every path.

        Used by the checker when the server returns ``401`` or ``403``
        for ``robots.txt`` — the RFC's "Unauthorized" state, treated as
        a blanket refusal.
        """
        return cls(
            rules=(RobotsRule.build(allow=False, pattern="/"),),
            crawl_delay=None,
        )

    def is_allowed(self, path: str) -> bool:
        """Return ``True`` iff *path* is allowed by this group's rules.

        Implements RFC 9309 §2.2.2:

        * Among the rules that match *path*, the one with the highest
          :attr:`RobotsRule.specificity` wins.
        * Ties are broken in favour of ``Allow`` (an explicit grant
          overrides a broader denial of the same specificity).
        * When no rule matches, *path* is allowed — the "no rule means
          allowed" default.

        Args:
            path: The request path, including any query string.

        Returns:
            ``True`` iff the effective verdict is "allowed".
        """
        if not self.rules:
            return True
        matching = [r for r in self.rules if r.matches(path)]
        if not matching:
            return True
        # Highest specificity wins; on a tie, Allow beats Disallow.
        best = max(
            matching,
            key=lambda r: (r.specificity, r.allow),
        )
        return best.allow


# --------------------------------------------------------------------------- #
# Stats                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ComplianceStats:
    """Immutable snapshot of a :class:`ComplianceChecker`'s counters.

    Returned by :meth:`ComplianceChecker.stats`. Counters are cumulative
    for the lifetime of the checker; ``cached_sites`` is point-in-time.

    Attributes:
        checked_urls: Cumulative count of :meth:`ComplianceChecker.is_allowed`
            / :meth:`ComplianceChecker.check` calls.
        disallowed_urls: Subset of :attr:`checked_urls` that were
            disallowed. Reported separately so an operator can see how
            often compliance is actually blocking traffic.
        cached_sites: Number of sites whose ``robots.txt`` is currently
            cached and fresh.
        fetch_failures: Cumulative count of ``robots.txt`` fetches that
            failed (network, timeout, ``5xx`` server error, malformed
            body) and fell back to "allow all". Every increment is a
            soft-fail — reported here so an operator who trusts
            compliance can notice the site they are checking against is
            not actually protecting them.
    """

    checked_urls: int
    disallowed_urls: int
    cached_sites: int
    fetch_failures: int


# --------------------------------------------------------------------------- #
# HTTP session protocol                                                       #
# --------------------------------------------------------------------------- #


class HttpSessionProtocol(Protocol):
    """The HTTP surface :class:`ComplianceChecker` requires.

    Satisfied by :class:`~nexusdl.core.session.http_session.HttpSession`
    and by any test double that exposes a ``get()`` coroutine returning
    a response object with ``status_code`` and ``text`` attributes.

    Deliberately minimal — the checker only fetches ``robots.txt``. It
    does not use the pipeline's UA rotation, cookie store, proxy pool,
    or cache; a ``robots.txt`` fetch is a single static file that should
    be retrieved with the plainest possible request, so that the
    compliance layer is not accidentally bypassed by an aggressive
    policy the caller applied for a different purpose.
    """

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> object:
        """Perform a GET request and return an object exposing
        ``status_code: int`` and ``text: str``.

        The return type is ``object`` rather than a specific response
        class so that the protocol does not depend on ``httpx``. Any
        object satisfying the two-attribute contract works.
        """
        ...


# --------------------------------------------------------------------------- #
# ComplianceChecker                                                           #
# --------------------------------------------------------------------------- #


class ComplianceChecker:
    """Checks URLs against ``robots.txt`` and caches the per-site policies.

    One instance per composition root, constructed with the same
    :class:`~nexusdl.core.session.http_session.HttpSession` the rest of
    the pipeline uses (or a thin wrapper around it). See the module
    docstring for the full contract.

    Example::

        from nexusdl.core.compliance import ComplianceChecker

        checker = ComplianceChecker(
            http_session=session,
            enabled=settings.compliance.respect_robots_txt,
            user_agent="NexusDL",
        )
        await checker.check(
            "https://example.org/manga/xyz", site_id="example"
        )
        response = await session.get(
            "https://example.org/manga/xyz", site_id="example"
        )

    Args:
        http_session: Session used to fetch ``robots.txt``. Must
            satisfy :class:`HttpSessionProtocol`. Not retained — the
            checker calls it and keeps no reference.
        enabled: Master switch. When ``False``, every
            :meth:`is_allowed` returns ``True`` and every
            :meth:`check` returns ``None`` without touching the
            network. Intended for deployments that own the target
            sites or that have obtained explicit permission.
        cache_ttl_seconds: How long a fetched ``robots.txt`` stays
            fresh. Defaults to :data:`DEFAULT_CACHE_TTL_SECONDS`
            (24 hours). Must be ``>= 0`` — ``0`` disables caching
            (every check triggers a fetch, which is rarely what a
            caller wants but is a valid debugging configuration).
        user_agent: The user-agent token to match groups against.
            Defaults to :data:`DEFAULT_USER_AGENT` (``"*"``). A
            deployment that has negotiated a specific ``User-agent``
            block with a site passes that token so the site's explicit
            rules are honoured.
        fetch_timeout_seconds: Timeout for the ``robots.txt`` fetch.
            Defaults to :data:`DEFAULT_FETCH_TIMEOUT_SECONDS`. Must be
            ``> 0``.
        clock: Monotonic clock, injectable for tests. Defaults to
            :func:`time.monotonic`.

    Raises:
        ValueError: If any numeric argument is out of range.
    """

    __slots__ = (
        "_cache",
        "_cache_lock",
        "_cache_ttl",
        "_checked_urls",
        "_clock",
        "_disallowed_urls",
        "_enabled",
        "_fetch_failures",
        "_fetch_timeout",
        "_http_session",
        "_user_agent",
    )

    def __init__(
        self,
        *,
        http_session: HttpSessionProtocol,
        enabled: bool = True,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        fetch_timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cache_ttl_seconds < 0:
            raise ValueError(
                f"cache_ttl_seconds must be >= 0, got {cache_ttl_seconds}"
            )
        if fetch_timeout_seconds <= 0:
            raise ValueError(
                f"fetch_timeout_seconds must be > 0, got "
                f"{fetch_timeout_seconds}"
            )

        self._http_session = http_session
        self._enabled = enabled
        self._cache_ttl = float(cache_ttl_seconds)
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        self._fetch_timeout = float(fetch_timeout_seconds)
        self._clock = clock

        #: Cache key → (parsed robots, expiry timestamp, was soft-fail).
        #: The soft-fail flag lets :meth:`get_status` distinguish
        #: "explicitly allowed" from "we could not fetch a policy and
        #: fell back to allow-all".
        self._cache: dict[str, tuple[RobotsTxt, float, bool]] = {}
        self._cache_lock = asyncio.Lock()

        self._checked_urls = 0
        self._disallowed_urls = 0
        self._fetch_failures = 0

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def check(
        self,
        url: str,
        *,
        site_id: str | None = None,
    ) -> None:
        """Raise :class:`ComplianceError` if *url* is disallowed.

        The strict entry point. Returns ``None`` (silently) when the
        URL is allowed or when compliance is disabled. Raises on the
        first disallowed URL — the caller is expected to catch and
        either skip the URL or surface it to the user.

        Note that ``ComplianceError`` is classified as
        **non-retryable** in
        :mod:`nexusdl.core.downloader.retry`. A caller that catches
        it and retries anyway is deliberately bypassing the site's
        declared policy — do not do that without an explicit override.

        Args:
            url: The URL about to be fetched.
            site_id: Optional site identifier, used as the cache key
                when provided. Falls back to the URL's hostname
                otherwise.

        Raises:
            ComplianceError: If ``robots.txt`` disallows *url*.
        """
        allowed = await self.is_allowed(url, site_id=site_id)
        if not allowed:
            suffix = f" (site_id={site_id!r})" if site_id else ""
            raise ComplianceError(
                f"robots.txt disallows fetching {url!r}{suffix}"
            )

    async def is_allowed(
        self,
        url: str,
        *,
        site_id: str | None = None,
    ) -> bool:
        """Return ``True`` iff *url* may be fetched.

        Never raises for a compliance verdict — a disallowed URL
        returns ``False``, a fetch failure returns ``True`` (soft
        fail), and a disabled checker returns ``True`` without
        touching the network.

        Args:
            url: The URL about to be fetched.
            site_id: Optional site identifier, used as the cache key
                when provided. Falls back to the URL's hostname
                otherwise.

        Returns:
            ``True`` if the URL is allowed (or compliance is disabled,
            or the ``robots.txt`` could not be fetched and soft-fail
            kicked in), ``False`` if a rule explicitly disallows it.

        Raises:
            asyncio.CancelledError: If the surrounding task is
                cancelled while waiting on the ``robots.txt`` fetch.
        """
        self._checked_urls += 1

        if not self._enabled:
            return True

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            # robots.txt only governs HTTP(S). A ``file://`` URL is not
            # subject to a network policy.
            return True

        host = parsed.hostname
        if not host:
            return True

        cache_key = site_id if site_id is not None else host
        robots, _was_soft_fail = await self._get_robots(
            parsed, cache_key
        )

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        allowed = robots.is_allowed(path)
        if not allowed:
            self._disallowed_urls += 1
        return allowed

    async def get_crawl_delay(
        self,
        url: str,
        *,
        site_id: str | None = None,
    ) -> float | None:
        """Return the ``Crawl-delay`` for *url*'s site, or ``None``.

        Fetches ``robots.txt`` on first call for a site, exactly like
        :meth:`is_allowed`. The returned value is the delay in
        seconds from the matching ``User-agent`` group; the caller
        decides whether to apply it (typically by passing it to
        :class:`~nexusdl.core.session.rate_limiter.RateLimiter`).

        This module **never waits**. A caller that ignores the delay
        is a caller that decided to ignore it — a decision made at a
        call site, not hidden inside a compliance primitive.

        Args:
            url: Any URL on the site whose ``Crawl-delay`` is wanted.
            site_id: Optional site identifier, used as the cache key.

        Returns:
            The delay in seconds, or ``None`` if the site's
            ``robots.txt`` does not specify one, or if compliance is
            disabled, or if the fetch failed (soft fail).
        """
        if not self._enabled:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        host = parsed.hostname
        if not host:
            return None
        cache_key = site_id if site_id is not None else host
        robots, _was_soft_fail = await self._get_robots(
            parsed, cache_key
        )
        return robots.crawl_delay

    async def get_status(
        self,
        url: str,
        *,
        site_id: str | None = None,
    ) -> ComplianceStatus:
        """Return the three-way verdict for *url*.

        Mirrors :meth:`is_allowed` but preserves the distinction
        between "explicitly allowed", "explicitly disallowed", and
        "no policy available" (soft-fail). A caller rendering a
        diagnostic table uses this; a caller issuing a request uses
        :meth:`is_allowed` (the behaviour is identical for
        ``UNKNOWN`` — soft fail means allowed).

        Args:
            url: The URL to inspect.
            site_id: Optional site identifier, used as the cache key.

        Returns:
            :attr:`ComplianceStatus.DISALLOWED` if a rule disallows
            the URL, :attr:`ComplianceStatus.UNKNOWN` if the
            ``robots.txt`` could not be fetched (network failure,
            ``5xx``, timeout, or parse error) and the verdict was
            soft-failed, :attr:`ComplianceStatus.ALLOWED` otherwise.
        """
        self._checked_urls += 1

        if not self._enabled:
            return ComplianceStatus.ALLOWED
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ComplianceStatus.ALLOWED
        host = parsed.hostname
        if not host:
            return ComplianceStatus.ALLOWED

        cache_key = site_id if site_id is not None else host
        robots, was_soft_fail = await self._get_robots(
            parsed, cache_key
        )

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        if not robots.is_allowed(path):
            self._disallowed_urls += 1
            return ComplianceStatus.DISALLOWED
        if was_soft_fail:
            return ComplianceStatus.UNKNOWN
        return ComplianceStatus.ALLOWED

    async def get_robots_txt_text(
        self,
        url: str,
        *,
        site_id: str | None = None,
    ) -> str | None:
        """Return the raw ``robots.txt`` text for *url*'s site, or ``None``.

        Fetches on cache miss and returns the body verbatim — useful
        for a diagnostic CLI (``nexusdl compliance inspect <url>``)
        and for a caller that wants to inspect directives this module
        does not parse (``Sitemap``, custom extensions). The fetch
        result is **not** cached; only the parsed form is. A caller
        that needs the raw body repeatedly should cache it itself.

        Returns ``None`` when compliance is disabled, when the URL
        has no hostname, or when the fetch failed.

        Args:
            url: Any URL on the site whose ``robots.txt`` is wanted.
            site_id: Optional site identifier, used for logging only
                (the raw text is not cached, so the key does not
                apply).
        """
        if not self._enabled:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        host = parsed.hostname
        if not host:
            return None
        return await self._fetch_raw(parsed, site_id)

    async def clear_cache(self, *, site_id: str | None = None) -> None:
        """Drop cached ``robots.txt`` entries.

        With ``site_id=None`` (the default), clears the entire cache.
        With a specific ``site_id``, drops only that site's entry —
        useful when an operator knows a site has published a new
        ``robots.txt`` and does not want to wait for the TTL.

        Args:
            site_id: The site whose entry to drop, or ``None`` to
                clear everything.
        """
        async with self._cache_lock:
            if site_id is None:
                self._cache.clear()
            else:
                self._cache.pop(site_id, None)

    async def stats(self) -> ComplianceStats:
        """Return an immutable snapshot of the checker's state."""
        now = self._clock()
        async with self._cache_lock:
            fresh_count = sum(
                1
                for _robots, expiry, _soft_fail in self._cache.values()
                if expiry > now
            )
        return ComplianceStats(
            checked_urls=self._checked_urls,
            disallowed_urls=self._disallowed_urls,
            cached_sites=fresh_count,
            fetch_failures=self._fetch_failures,
        )

    # ------------------------------------------------------------------ #
    # Internals — cache and fetch                                        #
    # ------------------------------------------------------------------ #

    async def _get_robots(
        self, parsed: ParseResult, cache_key: str
    ) -> tuple[RobotsTxt, bool]:
        """Return the parsed ``robots.txt`` for *cache_key*, fetching on miss.

        Returns a ``(robots, was_soft_fail)`` tuple. The boolean is
        ``True`` when the fetch failed (network, timeout, ``5xx``,
        parse error) and the returned ``robots`` is the allow-all
        fallback — the caller uses it to surface
        :attr:`ComplianceStatus.UNKNOWN` when appropriate.

        The fetch runs outside the cache lock so a slow fetch for one
        site does not block checks for another; two concurrent misses
        for the same site may issue two fetches (accepted —
        ``robots.txt`` is idempotent).
        """
        async with self._cache_lock:
            entry = self._cache.get(cache_key)
            if entry is not None and entry[1] > self._clock():
                return entry[0], entry[2]

        robots, was_soft_fail = await self._fetch_robots(
            parsed, cache_key
        )

        # A zero TTL disables caching; skip the write so the cache
        # does not grow unbounded in a debug configuration.
        if self._cache_ttl > 0:
            async with self._cache_lock:
                self._cache[cache_key] = (
                    robots,
                    self._clock() + self._cache_ttl,
                    was_soft_fail,
                )
        return robots, was_soft_fail

    async def _fetch_robots(
        self, parsed: ParseResult, cache_key: str
    ) -> tuple[RobotsTxt, bool]:
        """Fetch and parse ``robots.txt``, translating status codes per RFC 9309.

        Returns a ``(robots, was_soft_fail)`` tuple. The boolean is
        ``True`` when the fetch failed and the returned ``robots`` is
        the allow-all fallback, ``False`` when the server returned a
        definitive answer (``200``, ``401``/``403``, or another
        ``4xx`` meaning "no robots.txt").
        """
        raw, status = await self._fetch_with_status(parsed, cache_key)

        if status is None:
            # Network failure or timeout — RFC 9309 §2.3.1.4
            # "Unreachable": soft-fail to allow-all.
            self._fetch_failures += 1
            return RobotsTxt.allow_all(), True

        if 200 <= status < 300:
            try:
                return (
                    RobotsTxt.parse(raw or "", user_agent=self._user_agent),
                    False,
                )
            except Exception as exc:  # noqa: BLE001 — parser is total but defensive
                self._fetch_failures += 1
                _log.warning(
                    "Failed to parse robots.txt for {!r}: {}: {}",
                    cache_key,
                    type(exc).__name__,
                    exc,
                )
                return RobotsTxt.allow_all(), True

        if status in (401, 403):
            # RFC 9309 §2.3.1.3 "Unauthorized" — disallow all.
            _log.debug(
                "robots.txt for {!r} returned HTTP {}; treating as "
                "disallow-all",
                cache_key,
                status,
            )
            return RobotsTxt.disallow_all(), False

        if 400 <= status < 500:
            # Other 4xx — RFC 9309 §2.3.1.3 "Unavailable": allow all.
            # A site whose robots.txt is missing has no policy; this
            # is a definitive answer, not a soft-fail.
            return RobotsTxt.allow_all(), False

        # 5xx — RFC 9309 §2.3.1.4 "Unreachable": soft-fail to allow-all.
        self._fetch_failures += 1
        _log.debug(
            "robots.txt for {!r} returned HTTP {}; treating as "
            "unreachable, allow-all",
            cache_key,
            status,
        )
        return RobotsTxt.allow_all(), True

    async def _fetch_raw(
        self, parsed: ParseResult, site_id: str | None
    ) -> str | None:
        """Fetch ``robots.txt`` and return the raw text, or ``None`` on failure."""
        log_key = site_id or parsed.hostname or "unknown"
        raw, status = await self._fetch_with_status(parsed, log_key)
        if status is None or not (200 <= status < 300):
            return None
        return raw

    async def _fetch_with_status(
        self, parsed: ParseResult, log_key: str
    ) -> tuple[str | None, int | None]:
        """Fetch ``robots.txt`` and return ``(text, status_code)``.

        ``(None, None)`` on transport failure or timeout — the caller
        interprets that as RFC 9309's "Unreachable" and soft-fails.
        Cancellation from the underlying session propagates untouched.
        """
        robots_url = (
            f"{parsed.scheme}://{parsed.hostname}/robots.txt"
        )
        try:
            response = await self._http_session.get(
                robots_url,
                headers={"User-Agent": self._user_agent},
                timeout=self._fetch_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure is soft-fail
            _log.debug(
                "Failed to fetch robots.txt from {!r} for {!r}: "
                "{}: {}",
                robots_url,
                log_key,
                type(exc).__name__,
                exc,
            )
            return None, None

        # Duck-typed access — no httpx dependency, no isinstance check.
        status_code = getattr(response, "status_code", None)
        text = getattr(response, "text", None)
        if not isinstance(status_code, int):
            return None, None
        if status_code != 200:
            return None, status_code
        if not isinstance(text, str):
            return None, status_code
        return text, status_code

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"ComplianceChecker(enabled={self._enabled}, "
            f"cached={len(self._cache)}, "
            f"checked={self._checked_urls}, "
            f"disallowed={self._disallowed_urls})"
        )


# --------------------------------------------------------------------------- #
# Module helpers — parser internals                                          #
# --------------------------------------------------------------------------- #


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a ``robots.txt`` pattern into an anchored regex.

    The RFC's pattern grammar is::

        pattern = "/" *("?" | "*" | [^?#$]*)

    The common extension adds ``$`` as an end anchor. This function
    converts the pattern to a regex that:

    * anchors the match at the start of the path,
    * converts ``*`` to ``.*`` (any run of characters),
    * anchors the match at the end when the pattern ends with ``$``,
    * escapes every other character so ``.``, ``?``, ``+``, and other
      regex metacharacters are treated literally.

    An **empty** pattern compiles to a regex that matches nothing —
    matching the RFC's "empty pattern is a no-op" rule for
    ``Disallow:``. (The parser skips empty ``Disallow`` lines, but a
    defensive compile keeps the invariant even if a future caller
    constructs the rule directly.)

    Args:
        pattern: The raw pattern from a ``robots.txt`` directive.

    Returns:
        A compiled regex. Never raises — a pathological pattern is
        still a valid regex once every non-wildcard character is
        escaped.
    """
    if not pattern:
        # Regex that matches nothing (an anchored empty with a negative
        # lookahead on the empty string).
        return re.compile(r"(?!)")

    end_anchor = pattern.endswith("$")
    body = pattern[:-1] if end_anchor else pattern

    parts: list[str] = []
    for char in body:
        if char == "*":
            parts.append(".*")
        else:
            parts.append(re.escape(char))

    regex = "^" + "".join(parts)
    if end_anchor:
        regex += "$"
    return re.compile(regex)


def _iter_groups(
    text: str,
) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Split a ``robots.txt`` body into ``(user_agents, directives)`` groups.

    A group is a run of ``User-agent`` lines followed by a run of
    non-``User-agent`` directives. A ``User-agent`` line that
    interrupts another group starts a new group. Blank lines and
    comments are ignored. Directive names are lowercased; directive
    values are preserved verbatim (with trailing whitespace trimmed,
    which is the only normalisation every reference implementation
    applies).

    Args:
        text: The raw body.

    Returns:
        A list of ``(agents, directives)`` pairs. ``agents`` is the
        list of user-agent tokens in the group (possibly more than one
        when multiple ``User-agent`` lines precede the directives);
        ``directives`` is a list of ``(lowercased name, verbatim
        value)`` pairs, in file order.
    """
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    current_agents: list[str] = []
    current_directives: list[tuple[str, str]] = []

    def _flush() -> None:
        nonlocal current_agents, current_directives
        if current_agents:
            groups.append((current_agents, current_directives))
        current_agents = []
        current_directives = []

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # Strip the comment first (a ``#`` anywhere begins a comment),
        # then trim.
        hash_index = raw_line.find("#")
        if hash_index >= 0:
            raw_line = raw_line[:hash_index]
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == _DIRECTIVE_USER_AGENT:
            # A ``User-agent`` line after directives has started a new
            # group.
            if current_directives:
                _flush()
            current_agents.append(value.lower())
        else:
            if not current_agents:
                # Directives without a preceding User-agent line are
                # orphans — RFC 9309 §2.2.1 says they belong to no
                # group. Ignore them.
                continue
            current_directives.append((name, value))
    _flush()
    return groups


def _select_group(
    groups: list[tuple[list[str], list[tuple[str, str]]]],
    user_agent: str,
) -> list[tuple[str, str]] | None:
    """Select the most-specific matching group for *user_agent*.

    Implements RFC 9309 §2.2.1:

    * A group whose agent token equals ``"*"`` matches any UA.
    * A group whose agent token is a substring of the UA's product
      token matches. The comparison is done on lowercased values.
    * Among matching groups, the one with the **longest explicit
      token** wins; ``"*"`` is treated as length 0 (the least
      specific).
    * If multiple groups tie on specificity, their directives are
      concatenated (RFC 9309 requires the merge).

    The specificity of a *group* is computed as the maximum over its
    agents — a group that lists ``User-agent: foo`` and
    ``User-agent: bar`` contributes its directives **once** when
    either token matches, not twice.

    Args:
        groups: The output of :func:`_iter_groups`.
        user_agent: The UA token to match. Passed lowercased by the
            caller or normalised here.

    Returns:
        The selected group's directives, or ``None`` when no group
        matched.
    """
    ua_lower = user_agent.lower()
    best_specificity = -1
    best_directives: list[tuple[str, str]] | None = None

    for agents, directives in groups:
        # Best specificity for THIS group across its agents.
        group_specificity = -1
        for agent in agents:
            if agent == "*":
                spec = 0
            elif agent in ua_lower:
                spec = len(agent)
            else:
                continue
            if spec > group_specificity:
                group_specificity = spec

        if group_specificity < 0:
            # This group does not match the requested UA at all.
            continue

        if group_specificity > best_specificity:
            best_specificity = group_specificity
            best_directives = list(directives)
        elif group_specificity == best_specificity:
            # RFC 9309 §2.2.1: multiple groups at the same
            # specificity are combined. The per-group loop ensures
            # each group contributes exactly once, even when several
            # of its agents match.
            if best_directives is not None:
                best_directives.extend(directives)

    return best_directives
