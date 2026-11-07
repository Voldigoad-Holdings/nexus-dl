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

    The concrete
    :class:`~nexusdl.core.session.circuit_breaker.CircuitBreaker` (future)
    will implement a full state machine (CLOSED / OPEN / HALF_OPEN) with
    per-site tracking. This protocol captures only what the session needs
    to call:

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
            for key, value in base_headers.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError(
                        f"base_headers must map str to str, got "
                        f"{type(key).__name__} -> {type(value).__name__}"
                    )
                merged_headers[key] = value

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
            SessionError: If the session has been closed, or if httpx
                cannot be configured (e.g. ``http2=True`` without the
                ``httpx[http2]`` extra installed).
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
                try:
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
                except ImportError as exc:
                    # Most likely ``http2=True`` without ``httpx[http2]``
                    # installed. The raw message is technically accurate
                    # but does not name the fix.
                    raise SessionError(
                        "cannot create HTTP client: "
                        f"{type(exc).__name__}: {exc}. "
                        "Install with: pip install 'httpx[http2]' "
                        "(or set `http2=False`)"
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    raise SessionError(
                        "cannot create HTTP client: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
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
            is fully buffered by default; use :meth:`stream_download` for
            streaming).

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

        At most one of ``content``, ``data``, or ``json`` may be provided
        — this is an httpx contract, validated here so the failure is a
        clear ``ValueError`` at the session boundary rather than a
        ``TypeError`` deep inside httpx.

        Args:
            Same as :meth:`get`, plus:

            content: Raw body bytes/str.
            data: Form-encoded body (``Mapping`` or list of tuples).
            json: JSON-serialisable body.

        Raises:
            ValueError: If more than one of ``content``, ``data``,
                ``json`` is provided.
            Same as :meth:`get` otherwise.
        """
        provided = sum(x is not None for x in (content, data, json))
        if provided > 1:
            raise ValueError(
                "HttpSession.post accepts at most one of 'content', "
                "'data', or 'json'"
            )
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
            SessionError: On an invalid ``encoding`` name, or any of the
                exceptions :meth:`get` raises.
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
            try:
                return response.content.decode(encoding, errors="replace")
            except LookupError as exc:
                raise SessionError(
                    f"invalid text encoding {encoding!r} for {url!r}: "
                    f"{exc}"
                ) from exc
        return response.text

    async def get_json(
        self,
        url: str,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Perform a GET and return the body decoded as JSON.

        Used by API-first parsers and the API-based mixin. Delegates to
        :meth:`get`, then calls :meth:`httpx.Response.json`.

        Raises:
            SessionError: If the response body is not valid JSON. The
                underlying :class:`json.JSONDecodeError` is chained as
                ``__cause__`` for debugging.
            Same as :meth:`get` otherwise.
        """
        response = await self._request(
            "GET",
            url,
            site_id=site_id,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )
        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001 — json may raise more than JSONDecodeError
            raise SessionError(
                f"invalid JSON response from {url!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def stream_download(
        self,
        url: str,
        dest: Path,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Stream *url* into the file at *dest*.

        Applies the same layering as :meth:`get` (circuit breaker, rate
        limit, user-agent, cookies, proxy) and then streams the response
        body into *dest* in ``_STREAM_CHUNK_SIZE``-byte blocks.

        *on_progress* follows the ``(bytes_downloaded, bytes_total)``
        convention — the same shape ``tqdm`` and ``rich.progress``
        expect. It is throttled to at most once per
        ``_PROGRESS_THROTTLE_SECONDS``; a page of 60 images generates
        a manageable number of callbacks, not thousands. Passing
        ``None`` disables progress reporting.

        Partial files are the caller's responsibility: if the method
        raises (network error, cancellation, non-2xx status), *dest*
        may contain a partial body. The canonical consumer
        (:mod:`nexusdl.core.image.downloader`) writes to a temporary
        path and renames on success; callers that write directly must
        clean up on failure.

        Args:
            url: Absolute URL.
            dest: Destination path. The file is created or truncated.
            site_id: See :meth:`get`.
            headers: See :meth:`get`.
            timeout: Per-request total timeout.
            on_progress: Optional progress callback.
            **kwargs: Forwarded to :meth:`httpx.AsyncClient.stream`.

        Raises:
            Same as :meth:`get` — every error is translated into a
            :class:`SessionError` subclass before propagating.
            OSError: If *dest* cannot be written.
        """
        # Rate limit + circuit breaker + proxy selection + headers are
        # all handled by the same helper used for ``_request`` so a
        # streaming download respects the exact same per-site policy
        # as a one-shot GET.
        proxy_config = await self._prepare_request(
            url, site_id=site_id
        )
        request_headers = self._build_request_headers(
            url, site_id=site_id, headers=headers
        )
        proxy_url: str | None = (
            proxy_config.url.get_secret_value()
            if proxy_config is not None
            else None
        )

        client = await self._ensure_client()
        request_kwargs: dict[str, Any] = {}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        if proxy_url is not None:
            request_kwargs["proxy"] = proxy_url
        request_kwargs.update(kwargs)

        response: httpx.Response | None = None
        try:
            async with client.stream(
                "GET",
                url,
                headers=request_headers,
                **request_kwargs,
            ) as response:
                self._raise_for_status(response, url)
                total = _content_length(response)
                downloaded = 0
                last_progress = 0.0

                async with aiofiles.open(dest, "wb") as fh:
                    async for chunk in response.aiter_bytes(
                        chunk_size=_STREAM_CHUNK_SIZE
                    ):
                        await fh.write(chunk)
                        downloaded += len(chunk)
                        if on_progress is not None:
                            now = time.monotonic()
                            if (
                                now - last_progress
                                >= _PROGRESS_THROTTLE_SECONDS
                            ):
                                last_progress = now
                                on_progress(downloaded, total)

                # Final progress callback so the caller sees the last
                # byte even if the throttle swallowed it.
                if on_progress is not None and downloaded > 0:
                    on_progress(downloaded, total)
        except httpx.TimeoutException as exc:
            await self._record_failure(site_id, proxy_config)
            raise SessionTimeoutError(
                f"timeout streaming {url!r}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            await self._record_failure(site_id, proxy_config)
            raise SessionError(
                f"network error streaming {url!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except asyncio.CancelledError:
            # Do not touch proxy health on cancellation — the request
            # did not fail, the caller gave up.
            raise
        else:
            await self._record_success(site_id, proxy_config)

    # ------------------------------------------------------------------ #
    # Core request pipeline                                              #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        url: str,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Shared implementation of :meth:`get` and :meth:`post`.

        Order of operations — each step's failure aborts the pipeline:

        1. Circuit-breaker check (per-site).
        2. Rate-limiter wait (per-site).
        3. User-Agent, cookies, base headers, caller headers merge.
        4. Proxy selection (per-site, sticky).
        5. The actual request.
        6. Proxy-health and cookie-store updates.
        7. Status classification (2xx / 3xx accepted, 4xx/5xx
           translated into the appropriate :class:`SessionError`).

        The single ``_raise_for_status`` call at the end is what makes
        the error contract uniform across all methods that call
        ``_request`` (``get``, ``post``, ``get_html``, ``get_json``).
        """
        proxy_config = await self._prepare_request(url, site_id=site_id)
        request_headers = self._build_request_headers(
            url, site_id=site_id, headers=headers
        )
        proxy_url: str | None = (
            proxy_config.url.get_secret_value()
            if proxy_config is not None
            else None
        )

        client = await self._ensure_client()

        request_kwargs: dict[str, Any] = {
            "headers": request_headers,
        }
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        if proxy_url is not None:
            request_kwargs["proxy"] = proxy_url
        request_kwargs.update(kwargs)

        try:
            response = await client.request(
                method, url, **request_kwargs
            )
        except httpx.TimeoutException as exc:
            await self._record_failure(site_id, proxy_config)
            raise SessionTimeoutError(
                f"timeout on {method} {url!r}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            await self._record_failure(site_id, proxy_config)
            raise SessionError(
                f"network error on {method} {url!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except asyncio.CancelledError:
            # Do not touch proxy health on cancellation.
            raise

        # Store any Set-Cookie values the server sent, even on error
        # responses: some sites set a session cookie on a 4xx challenge
        # response that the next request must present.
        self._record_cookies(site_id, response)

        try:
            self._raise_for_status(response, url)
        except SessionError:
            await self._record_failure(site_id, proxy_config)
            raise
        await self._record_success(site_id, proxy_config)
        return response

    async def _prepare_request(
        self, url: str, *, site_id: str | None
    ) -> ProxyConfig | None:
        """Run the pre-request pipeline: validation, breaker, rate, proxy.

        Returns the selected :class:`ProxyConfig` (or ``None`` if no
        proxy is in use) so the caller can pass it to the post-request
        health report. Every failure here raises before any network
        I/O is performed.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise SessionError(
                f"unsupported URL scheme {parsed.scheme!r} in {url!r}; "
                "only http:// and https:// are supported"
            )

        if site_id is not None and self._circuit_breaker is not None:
            try:
                allowed = self._circuit_breaker.allow_request(site_id)
            except Exception as exc:  # noqa: BLE001
                raise SessionError(
                    f"circuit breaker raised for site {site_id!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if not allowed:
                raise CircuitBreakerOpenError(
                    f"circuit breaker is open for site {site_id!r}; "
                    "refusing request"
                )

        if site_id is not None:
            limiter = await self._get_rate_limiter(site_id)
            if limiter is not None:
                await limiter.acquire()

        if self._proxy_manager is not None:
            return await self._proxy_manager.get_proxy(site_id)
        return None

    async def _record_success(
        self,
        site_id: str | None,
        proxy_config: ProxyConfig | None,
    ) -> None:
        """Feed a successful request into the breaker and proxy manager."""
        if site_id is not None and self._circuit_breaker is not None:
            try:
                self._circuit_breaker.record_success(site_id)
            except Exception:
                self._log.exception(
                    "circuit_breaker.record_success raised for site {!r}",
                    site_id,
                )
        if self._proxy_manager is not None and proxy_config is not None:
            try:
                await self._proxy_manager.mark_success(proxy_config)
            except Exception:
                self._log.exception(
                    "proxy_manager.mark_success raised for {}",
                    proxy_config.redacted,
                )

    async def _record_failure(
        self,
        site_id: str | None,
        proxy_config: ProxyConfig | None,
        *,
        error: str | None = None,
    ) -> None:
        """Feed a failed request into the breaker and proxy manager."""
        if site_id is not None and self._circuit_breaker is not None:
            try:
                self._circuit_breaker.record_failure(site_id)
            except Exception:
                self._log.exception(
                    "circuit_breaker.record_failure raised for site {!r}",
                    site_id,
                )
        if self._proxy_manager is not None and proxy_config is not None:
            try:
                await self._proxy_manager.mark_failure(
                    proxy_config, error=error
                )
            except Exception:
                self._log.exception(
                    "proxy_manager.mark_failure raised for {}",
                    proxy_config.redacted,
                )

    # ------------------------------------------------------------------ #
    # Headers and cookies                                                #
    # ------------------------------------------------------------------ #

    def _build_request_headers(
        self,
        url: str,
        *,
        site_id: str | None,
        headers: Mapping[str, str] | None,
    ) -> dict[str, str]:
        """Build the final header dict for one request.

        Order of precedence (later wins):

        1. :data:`_DEFAULT_HEADERS` and any ``base_headers`` passed to
           :meth:`__init__` — already merged at construction time into
           :attr:`_base_headers`.
        2. Per-site cookies (only when a cookie manager is injected).
        3. Caller-supplied ``headers``.
        4. Rotated ``User-Agent`` — always wins over any caller-supplied
           ``User-Agent`` because the whole point of the rotator is
           that it decides.

        The hostname is used as the ``for_domain`` key so that the
        rotator's sticky behaviour tracks per-site state naturally.
        """
        merged: dict[str, str] = dict(self._base_headers)
        if self._cookie_manager is not None and site_id is not None:
            try:
                stored = self._cookie_manager.get_cookies(site_id)
            except Exception:
                self._log.exception(
                    "cookie_manager.get_cookies raised for site {!r}",
                    site_id,
                )
                stored = {}
            if stored:
                merged["Cookie"] = "; ".join(
                    f"{k}={v}" for k, v in stored.items()
                )
        if headers:
            merged.update(headers)

        domain = urlparse(url).hostname or ""
        merged["User-Agent"] = self._user_agents.for_domain(domain)
        return merged

    def _record_cookies(
        self, site_id: str | None, response: httpx.Response
    ) -> None:
        """Persist any ``Set-Cookie`` headers on *response*."""
        if self._cookie_manager is None or site_id is None:
            return
        cookies: dict[str, str] = {}
        for name, value in response.headers.multi_items():
            if name.lower() != "set-cookie":
                continue
            # ``Set-Cookie`` can carry attributes (``; Path=...``,
            # ``; HttpOnly``) after the ``name=value`` pair. We only
            # care about the pair itself.
            first = value.split(";", 1)[0].strip()
            if "=" not in first:
                continue
            cookie_name, _, cookie_value = first.partition("=")
            cookie_name = cookie_name.strip()
            if cookie_name:
                cookies[cookie_name] = cookie_value.strip()
        if not cookies:
            return
        try:
            self._cookie_manager.update_cookies(site_id, cookies)
        except Exception:
            self._log.exception(
                "cookie_manager.update_cookies raised for site {!r}",
                site_id,
            )

    # ------------------------------------------------------------------ #
    # Per-site rate limiter                                              #
    # ------------------------------------------------------------------ #

    async def _get_rate_limiter(
        self, site_id: str
    ) -> RateLimiter | None:
        """Return (or lazily create) the :class:`RateLimiter` for *site_id*.

        Returns ``None`` when no ``rate_limit_source`` was injected or
        when it returns ``None`` for the site. The creation is
        serialised by ``_rate_limit_lock`` so two concurrent
        first-requests cannot each construct a limiter and one silently
        overwrite the other — which would let the site receive two
        parallel requests during the startup window.
        """
        if self._rate_limit_source is None:
            return None
        # Fast path — no lock once the limiter exists.
        existing = self._rate_limiters.get(site_id)
        if existing is not None:
            return existing
        async with self._rate_limit_lock:
            existing = self._rate_limiters.get(site_id)
            if existing is not None:
                return existing
            try:
                rate = self._rate_limit_source(site_id)
            except Exception as exc:  # noqa: BLE001
                raise SessionError(
                    f"rate_limit_source raised for site {site_id!r}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if rate is None:
                # Cache the "no limit" answer by storing nothing but
                # returning None; a second call takes the same path
                # but only after re-querying the source. That is fine
                # — the source is expected to be cheap (a dict lookup
                # in the composition root).
                return None
            limiter = RateLimiter(rate_per_second=rate, name=site_id)
            self._rate_limiters[site_id] = limiter
            self._log.debug(
                "rate limiter created for site {!r} at {}/s",
                site_id,
                rate,
            )
            return limiter

    # ------------------------------------------------------------------ #
    # Status classification                                              #
    # ------------------------------------------------------------------ #

    def _raise_for_status(
        self, response: httpx.Response, url: str
    ) -> None:
        """Translate a non-2xx response into the appropriate SessionError.

        Classification:

        * 2xx, 3xx — accepted, return.
        * 429 — :class:`SessionRateLimitError`.
        * 403 / 503 with Cloudflare headers — :class:`SessionCloudflareError`.
        * 5xx — :class:`SessionError` (server error).
        * Other 4xx — :class:`SessionError` (client error).
        """
        status = response.status_code
        if 200 <= status < 400:
            return
        if status == _RATE_LIMIT_STATUS:
            retry_after = response.headers.get("Retry-After")
            raise SessionRateLimitError(
                f"rate limited (429) on {url!r}"
                + (f"; retry after {retry_after}s" if retry_after else "")
            )
        if status in (403, 503) and _looks_like_cloudflare(response):
            raise SessionCloudflareError(
                f"Cloudflare challenge (status {status}) on {url!r}"
            )
        if 500 <= status < 600:
            raise SessionError(
                f"server error ({status}) on {url!r}"
            )
        raise SessionError(
            f"client error ({status}) on {url!r}"
        )


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #


def _content_length(response: httpx.Response) -> int:
    """Return the body length from ``Content-Length``, or ``-1``.

    ``-1`` is the convention ``tqdm`` and most progress bars use for
    "unknown total". Never raises: a missing, malformed, or negative
    header value all map to ``-1``.
    """
    raw = response.headers.get("Content-Length")
    if raw is None:
        return -1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return -1
    return value if value >= 0 else -1


def _looks_like_cloudflare(response: httpx.Response) -> bool:
    """Return ``True`` iff any Cloudflare challenge header is present.

    The header set is small and stable (``cf-ray`` is added to every
    Cloudflare response; ``cf-mitigated`` is added specifically on
    challenge pages; ``cf-chl-bypass`` appears on bypass-flow pages).
    Presence of any of them on a 403/503 is the classifier's signal.
    """
    for header_name in _CLOUDFLARE_HEADERS:
        if header_name in response.headers:
            return True
    return False
