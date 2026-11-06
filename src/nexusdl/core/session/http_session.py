"""Centralised HTTP client for every network request in the pipeline.

:class:`HttpSession` wraps a single :class:`httpx.AsyncClient` and layers
on top of it everything the parsers, the image downloader, and any other
component need in order to hit a source site politely, safely, and with
consistent error semantics:

* **Per-site rate limiting** — a :class:`RateLimiter` per ``site_id``,
  driven by ``SiteConfig.rate_limit``. Requests are paced without ever
  busy-waiting.
* **Per-site circuit breaking** — when a site is broken (5xx storm,
  repeated connection failures), the circuit opens and subsequent calls
  fail fast with :class:`CircuitBreakerOpenError`, sparing the site (and
  the user's bandwidth) a pointless retry storm.
* **Per-site cookies** — optional, injected via a
  :class:`CookieManagerProtocol`. The session updates the manager with
  every ``Set-Cookie`` the site sends, and reads the manager before
  every request so authenticated sites see their cookies.
* **Per-request User-Agent rotation** — sticky per domain for the
  configured TTL, so a site never sees the UA change mid-session.
* **Per-site proxy selection** — the :class:`ProxyManager` decides which
  proxy (or none) a request should use; failures and successes are
  reported back so unhealthy proxies are benched.
* **Consistent error translation** — every httpx exception, every 4xx
  and 5xx status, and every Cloudflare challenge becomes a member of the
  :class:`~nexusdl.core.exceptions.SessionError` hierarchy. Callers
  never import httpx to catch a network failure.

All collaborators are **injected**, never imported:

* :class:`~nexusdl.core.session.user_agents.UserAgentRotator`
* :class:`~nexusdl.core.session.rate_limiter.RateLimiter` (via a
  ``rate_limit_source`` callable that returns a per-site rate or ``None``)
* :class:`~nexusdl.core.session.proxy_manager.ProxyManager`
* :class:`CircuitBreakerProtocol`, :class:`CookieManagerProtocol`
  (structural protocols defined in this module — the concrete
  implementations, when they ship, satisfy them automatically)

A caller that does not want a feature simply omits the corresponding
argument. Every collaborator is optional; the session works with none of
them.

Retry policy
------------
**The session never retries.** Every call is one-shot: it either
succeeds or raises. Retrying is the caller's responsibility, and the
pipeline's retry controller lives in
:mod:`nexusdl.core.downloader.retry` — it wraps the parser or worker
call, not the individual HTTP request. This split matters:

* The session's job is to describe *what happened* (a timeout, a rate
  limit, a circuit-breaker rejection) with precision.
* The caller's job is to decide *what to do about it* — back off,
  retry with a longer window, switch parser, give up.

Putting retry inside the session would produce
``retry_outer × retry_inner`` attempts, exponentially slowing the
worst case with no benefit.

Cancellation
------------
``asyncio.CancelledError`` propagates untouched from every method.
Partial state (a half-written file from :meth:`stream_download`) is
cleaned up by the caller — see
:mod:`nexusdl.core.image.downloader.ImageDownloader`, which is the
canonical consumer of that method.

Lazy client creation and reuse
------------------------------
The underlying :class:`httpx.AsyncClient` is **not** created at
construction time — it is created lazily on first use, so a
:class:`HttpSession` instance is cheap to build in a test that never
makes a request. Once created, the client lives until :meth:`close`.
The pool reuses connections across calls, which is critical: a chapter
with 60 pages uses 60 requests over the same TLS connection.

Client creation is serialised by an internal lock. Without it, two
concurrent first-requests would each construct an ``AsyncClient`` and
one would be silently leaked — the httpx pool holds its connections
until garbage collection, and ``httpx.AsyncClient`` does not guarantee
``__del__`` cleanup. The serialisation costs nothing in steady state
(the lock is acquired once per session lifetime) and eliminates the
leak.

Session lifecycle
-----------------
::

    session = HttpSession(...)
    async with session:
        await session.get(...)
    # __aexit__ closes the underlying httpx client

Or explicitly::

    session = HttpSession(...)
    try:
        await session.get(...)
    finally:
        await session.close()

Both forms are supported. A session is **single-use**: after
:meth:`close` every method raises :class:`SessionError` with a clear
message. The composition root creates one per application lifetime.

Per-site rate limiter creation
------------------------------
:attr:`_rate_limiters` is populated on first use, guarded by a
dedicated ``asyncio.Lock``. Two coroutines hitting the same new site
concurrently would otherwise each construct a fresh
:class:`RateLimiter`, one of which would be overwritten by the other
— and the site would receive two parallel requests during the window,
breaking its rate ceiling exactly once at startup. The lock serialises
the creation; after the first call the dict lookup is lock-free (the
lock is only held during creation).

Proxy-health updates
--------------------
Failures and successes are reported to the
:class:`~nexusdl.core.session.proxy_manager.ProxyManager` through
**awaited** ``mark_failure`` / ``mark_success`` calls, not scheduled
tasks. The calls are O(1) and the session is already in a failure path
where a few extra microseconds are irrelevant; awaiting makes the
sequence "attempt → mark → re-raise" linear and auditable, and avoids
the fire-and-forget task that would otherwise surface as an unhandled
exception on the next event-loop tick if the caller cancels.

Dependency graph (no cycles)::

    core.exceptions               ─┐
    core.session.user_agents       │
    core.session.rate_limiter      ├──► core.session.http_session
    core.session.proxy_manager     │
    httpx, aiofiles, loguru        │
                                   │

No dependency on ``core.config``, ``core.models``, ``parsers``,
``plugins``, or ``interfaces``. The rate-limit source is a caller-
supplied callable, so the session never needs to know about
``SiteConfig``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol
from urllib.parse import urlparse

import aiofiles
import httpx
from loguru import logger

from nexusdl.core.exceptions import (
    CircuitBreakerOpenError,
    SessionCloudflareError,
    SessionError,
    SessionRateLimitError,
    SessionTimeoutError,
)
from nexusdl.core.session.proxy_manager import ProxyConfig, ProxyManager
from nexusdl.core.session.rate_limiter import RateLimiter
from nexusdl.core.session.user_agents import UserAgentRotator


__all__ = [
    "CircuitBreakerProtocol",
    "CookieManagerProtocol",
    "DEFAULT_TIMEOUTS",
    "HttpSession",
    "HttpTimeouts",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default headers sent with every request. Deliberately minimal — a
#: realistic UA is added per request by the
#: :class:`~nexusdl.core.session.user_agents.UserAgentRotator`; everything
#: else is per-parser (``SiteConfig.default_headers``) or per-call.
_DEFAULT_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        # Deliberately no ``Accept-Encoding`` — httpx negotiates this itself
        # and we must let it (brotli/zstd availability varies).
    }
)

#: Chunk size used by :meth:`HttpSession.stream_download`. 64 KiB is the
#: same default httpx uses for `aiter_bytes`; here it is explicit so a
#: future tweak is a single-place edit.
_STREAM_CHUNK_SIZE: Final[int] = 64 * 1024

#: Minimum delay between two progress callbacks in
#: :meth:`HttpSession.stream_download`. Prevents a page of 60 images from
#: generating tens of thousands of callbacks while still producing visible
#: progress at roughly 5 Hz.
_PROGRESS_THROTTLE_SECONDS: Final[float] = 0.2

#: HTTP status codes that mean "the site is throttling us" and should be
#: translated into :class:`~nexusdl.core.exceptions.SessionRateLimitError`.
_RATE_LIMIT_STATUS: Final[int] = 429

#: Header names a Cloudflare challenge response carries. Presence of any
#: of these on a 403 or 503 is a strong signal that the challenge is
#: solvable via FlareSolverr rather than a genuine auth failure.
_CLOUDFLARE_HEADERS: Final[tuple[str, ...]] = (
    "cf-ray",
    "cf-mitigated",
    "cf-chl-bypass",
)

_log = logger.bind(component="session.http")


# --------------------------------------------------------------------------- #
# Timeouts                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HttpTimeouts:
    """The four httpx timeout values, in one immutable object.

    httpx separates connect, read, write, and pool timeouts because each
    protects against a different failure mode. Exposing them individually
    is deliberate — a slow site needs a longer read timeout without
    loosening the connect timeout that protects against unreachable
    hosts.

    Attributes:
        connect: Maximum seconds to establish the TCP/TLS connection.
        read: Maximum seconds to wait for a chunk of data from the
            server. When a site is slow but functional, this is the knob
            that matters.
        write: Maximum seconds to upload a request body. Irrelevant for
            GET-heavy scraping, relevant for the rare POST.
        pool: Maximum seconds to wait for a connection from the pool
            when all are busy. A saturated pool is a session bug, not a
            site bug, so this should fail fast.
    """

    connect: float = 10.0
    read: float = 30.0
    write: float = 30.0
    pool: float = 10.0

    def to_httpx(self) -> httpx.Timeout:
        """Project onto an :class:`httpx.Timeout` suitable for ``AsyncClient``."""
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )


#: Default timeout bundle. Connect is short — an unreachable host is
#: almost always a permanent problem; read is long enough to cover a slow
#: manga page render; write and pool match read.
DEFAULT_TIMEOUTS: Final[HttpTimeouts] = HttpTimeouts()


# --------------------------------------------------------------------------- #
# Injection protocols                                                         #
# --------------------------------------------------------------------------- #
# Both protocols are defined here because the modules that will provide
# the concrete implementations are not yet delivered. When
# ``circuit_breaker.py`` and ``cookie_manager.py`` ship, their classes
# satisfy these protocols structurally and the composition root wires
# them in without touching this module.
# --------------------------------------------------------------------------- #


class CircuitBreakerProtocol(Protocol):
    """Minimal circuit-breaker surface required by :class:`HttpSession`.

    The concrete :class:`~nexusdl.core.session.circuit_breaker.CircuitBreaker`
    (future) will implement a full state machine (CLOSED / OPEN /
    HALF_OPEN) with per-site tracking. This protocol captures only what
    the session needs to call:

    * :meth:`allow_request` — ``True`` when a request may proceed
      (CLOSED or HALF_OPEN), ``False`` when the site is considered down
      (OPEN).
    * :meth:`record_success` / :meth:`record_failure` — feed the state
      machine with the outcome.
    """

    def allow_request(self, site_id: str) -> bool:
        """Return ``True`` iff a request to *site_id* may proceed."""
        ...

    def record_success(self, site_id: str) -> None:
        """Record a successful request to *site_id*."""
        ...

    def record_failure(self, site_id: str) -> None:
        """Record a failed request to *site_id*."""
        ...


class CookieManagerProtocol(Protocol):
    """Minimal cookie-store surface required by :class:`HttpSession`.

    The concrete
    :class:`~nexusdl.core.session.cookie_manager.CookieManager` (future)
    will implement Fernet encryption at rest, filesystem persistence,
    and merge semantics with ``.env`` overrides. This protocol captures
    only what the session needs:

    * :meth:`get_cookies` — pull the stored cookies for a site before a
      request.
    * :meth:`update_cookies` — push any ``Set-Cookie`` values back.
    """

    def get_cookies(self, site_id: str) -> Mapping[str, str]:
        """Return every stored cookie for *site_id*, as ``name → value``."""
        ...

    def update_cookies(
        self, site_id: str, cookies: Mapping[str, str]
    ) -> None:
        """Merge *cookies* into the store for *site_id*."""
        ...


# --------------------------------------------------------------------------- #
# HttpSession                                                                 #
# --------------------------------------------------------------------------- #


class HttpSession:
    """Centralised HTTP client with rate limiting, circuit breaking, etc.

    One instance per composition root, created at startup and closed at
    shutdown. Safe to share across coroutines.

    See the module docstring for the design contract, the retry policy,
    and the injected collaborators.

    Example:
        >>> session = HttpSession(
        ...     rate_limit_source=lambda sid: 1.0 if sid == "mangadex" else None,
        ...     proxy_manager=ProxyManager(load_proxy_configs(settings.proxy)),
        ... )
        >>> try:
        ...     html = await session.get_html(
        ...         "https://mangadex.org/title/xyz",
        ...         site_id="mangadex",
        ...     )
        ... finally:
        ...     await session.close()

    Args:
        base_headers: Extra headers applied to every request (in addition
            to :data:`_DEFAULT_HEADERS` and the rotated ``User-Agent``).
            Typically ``SiteConfig.default_headers`` for a single-site
            session, or an empty mapping for a shared session.
        timeouts: HTTP timeout bundle. See :class:`HttpTimeouts`.
        http2: Enable HTTP/2 (requires the ``httpx[http2]`` extra —
            included in the project's dependency list).
        follow_redirects: Follow 3xx responses automatically.
        max_connections: Maximum concurrent connections in the pool.
        max_keepalive_connections: Connections kept alive between
            requests.
        user_agents: Rotator for the per-request ``User-Agent``. When
            ``None``, a default rotator loading the bundled pool is
            created.
        proxy_manager: Proxy pool and health tracker. When ``None``, no
            proxies are used — the session connects directly.
        rate_limit_source: Callable taking a ``site_id`` and returning
            the sustained rate, in requests per second, that the site
            allows, or ``None`` for "no limit". The session creates a
            :class:`~nexusdl.core.session.rate_limiter.RateLimiter` per
            site lazily, on first use. When ``None``, no rate limiting
            is applied.
        circuit_breaker: Circuit-breaker instance. When ``None``, no
            circuit breaking is applied.
        cookie_manager: Cookie store. When ``None``, no cookies are
            persisted across calls (only ``Set-Cookie`` within a single
            ``httpx.AsyncClient`` is honoured, which is the httpx
            default).

    Raises:
        ValueError: If any numeric argument is out of range.
    """

    __slots__ = (
        "_base_headers",
        "_circuit_breaker",
        "_client",
        "_client_lock",
        "_closed",
        "_cookie_manager",
        "_follow_redirects",
        "_http2",
        "_max_connections",
        "_max_keepalive_connections",
        "_proxy_manager",
        "_rate_limit_lock",
        "_rate_limit_source",
        "_rate_limiters",
        "_timeouts",
        "_user_agents",
    )

    def __init__(
        self,
        *,
        base_headers: Mapping[str, str] | None = None,
        timeouts: HttpTimeouts = DEFAULT_TIMEOUTS,
        http2: bool = True,
        follow_redirects: bool = True,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        user_agents: UserAgentRotator | None = None,
        proxy_manager: ProxyManager | None = None,
        rate_limit_source: Callable[[str], float | None] | None = None,
        circuit_breaker: CircuitBreakerProtocol | None = None,
        cookie_manager: CookieManagerProtocol | None = None,
    ) -> None:
        if max_connections < 1:
            raise ValueError(
                f"max_connections must be >= 1, got {max_connections}"
            )
        if max_keepalive_connections < 0:
            raise ValueError(
                f"max_keepalive_connections must be >= 0, got "
                f"{max_keepalive_connections}"
            )

        # Merge the fixed defaults with any caller-supplied base headers.
        merged_headers: dict[str, str] = dict(_DEFAULT_HEADERS)
        if base_headers is not None:
            merged_headers.update(base_headers)

        self._base_headers: Mapping[str, str] = MappingProxyType(
            merged_headers
        )
        self._timeouts: HttpTimeouts = timeouts
        self._http2: bool = http2
        self._follow_redirects: bool = follow_redirects
        self._max_connections: int = max_connections
        self._max_keepalive_connections: int = max_keepalive_connections

        self._user_agents: UserAgentRotator = (
            user_agents if user_agents is not None else UserAgentRotator()
        )
        self._proxy_manager: ProxyManager | None = proxy_manager
        self._rate_limit_source = rate_limit_source
        self._circuit_breaker = circuit_breaker
        self._cookie_manager = cookie_manager

        # Lazy per-site rate limiters, keyed by site_id. Populated on
        # first use by ``_get_rate_limiter``, guarded by
        # ``_rate_limit_lock`` so two concurrent first-requests for the
        # same site cannot each construct a limiter and one silently
        # overwrite the other (which would let the site receive two
        # parallel requests, breaking its rate ceiling exactly once at
        # startup).
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._rate_limit_lock = asyncio.Lock()

        # Lazy client. Created on first use, guarded by
        # ``_client_lock`` so two concurrent first-requests cannot each
        # construct an ``AsyncClient`` — httpx does not guarantee
        # ``__del__`` closes a lost client, so the loser would leak
        # connections until garbage collection.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._closed: bool = False

        self._log = logger.bind(component="session.http")

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> HttpSession:
        """Enter the context, ensuring the underlying client exists."""
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit the context, closing the underlying client. Never suppresses."""
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client and refuse further calls.

        Idempotent. After ``close()`` returns, every method that issues a
        request raises :class:`~nexusdl.core.exceptions.SessionError`
        with a message that names the reason. The composition root calls
        this exactly once per application lifetime.
        """
        if self._closed:
            return
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — cleanup, log and continue
                self._log.warning(
                    "Error closing HTTP client: {}: {}",
                    type(exc).__name__,
                    exc,
                )

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the underlying client, creating it lazily on first use.

        Creation is serialised by ``_client_lock`` so concurrent
        first-requests cannot each build an ``AsyncClient`` — the loser
        of such a race would leak its connection pool. After the first
        successful creation the lock is uncontended and the dict
        lookup is atomic; the fast path (``self._client is not None``)
        avoids acquiring the lock at all in steady state.

        Raises:
            SessionError: If the session has been closed.
        """
        # Fast path — no lock needed once the client exists and the
        # session is open. ``self._client`` is only ever set under the
        # lock, and only cleared under the lock, so a non-``None`` value
        # here is safe to use.
        if self._closed:
            raise SessionError(
                "HttpSession is closed; create a new instance to "
                "continue making requests"
            )
        client = self._client
        if client is not None:
            return client

        async with self._client_lock:
            # Double-check — another coroutine may have built it while
            # we were waiting.
            if self._closed:
                raise SessionError(
                    "HttpSession is closed; create a new instance to "
                    "continue making requests"
                )
            if self._client is None:
                self._client = httpx.AsyncClient(
                    headers=dict(self._base_headers),
                    timeout=self._timeouts.to_httpx(),
                    http2=self._http2,
                    follow_redirects=self._follow_redirects,
                    limits=httpx.Limits(
                        max_connections=self._max_connections,
                        max_keepalive_connections=(
                            self._max_keepalive_connections
                        ),
                    ),
                )
                self._log.debug(
                    "HTTP client initialised (http2={}, max_conn={})",
                    self._http2,
                    self._max_connections,
                )
            return self._client

    # ------------------------------------------------------------------ #
    # Public request API                                                 #
    # ------------------------------------------------------------------ #

    async def get(
        self,
        url: str,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a GET request and return the response.

        Applies, in order: circuit-breaker check, rate-limit wait,
        user-agent + cookie + caller-header merge, proxy selection, the
        actual request, and post-response health/cookie updates. Raises
        on non-2xx responses (unless ``raise_for_status=False`` is passed
        via ``**kwargs``).

        Args:
            url: Absolute URL. Must use ``http://`` or ``https://``.
            site_id: Identifier of the source site, used for rate
                limiting, circuit breaking, cookie association, and
                proxy stickiness. ``None`` disables every per-site
                behaviour — useful for one-off requests to sites the
                pipeline does not otherwise know about.
            headers: Caller-supplied headers, merged on top of the base
                headers. The rotated ``User-Agent`` always wins over a
                caller-supplied ``User-Agent`` key, because the point of
                the rotator is that it decides.
            timeout: Per-request total timeout, in seconds. Overrides the
                configured :class:`HttpTimeouts` for this call only.
            **kwargs: Forwarded to :meth:`httpx.AsyncClient.get`.

        Returns:
            The :class:`httpx.Response`. The caller owns it and is
            responsible for reading the body if it needs to (the response
            is fully buffered by default; use :meth:`stream_download` or
            :meth:`get_stream` for streaming).

        Raises:
            CircuitBreakerOpenError: If the site's circuit breaker is
                open.
            SessionRateLimitError: On a 429 response.
            SessionCloudflareError: On a 403 or 503 with Cloudflare
                headers.
            SessionTimeoutError: On a request timeout or connection
                error.
            SessionError: On any other HTTP/network failure, or a
                non-2xx status that is not otherwise classified.
        """
        return await self._request(
            "GET",
            url,
            site_id=site_id,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )

    async def post(
        self,
        url: str,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        content: Any = None,
        data: Any = None,
        json: Any = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Perform a POST request. Same layering as :meth:`get`.

        Raises:
            Same as :meth:`get`.
        """
        return await self._request(
            "POST",
            url,
            site_id=site_id,
            headers=headers,
            content=content,
            data=data,
            json=json,
            timeout=timeout,
            **kwargs,
        )

    async def get_html(
        self,
        url: str,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        encoding: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Perform a GET and return the body decoded as text.

        The default for scraping. Delegates to :meth:`get`, then reads
        the body with an optional explicit encoding override.

        Args:
            encoding: Force a specific text encoding, overriding httpx's
                charset detection. Rarely needed; provided because some
                sites ship broken ``Content-Type`` headers.

        Raises:
            Same as :meth:`get`.
        """
        response = await self._request(
            "GET",
            url,
            site_id=site_id,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )
        if encoding is not None:
            return response.content.decode(encoding, errors="replace")
        return response.text

    async def get_json(
       
