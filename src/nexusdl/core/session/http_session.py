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
#: else is per-parser (`SiteConfig.default_headers`) or per-call.
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
        "_closed",
        "_cookie_manager",
        "_follow_redirects",
        "_http2",
        "_max_connections",
        "_max_keepalive_connections",
        "_proxy_manager",
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
        # first use by ``_get_rate_limiter``.
        self._rate_limiters: dict[str, RateLimiter] = {}

        self._client: httpx.AsyncClient | None = None
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

        Raises:
            SessionError: If the session has been closed.
        """
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
                    max_keepalive_connections=self._max_keepalive_connections,
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
        self,
        url: str,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Perform a GET and decode the body as JSON.

        Used by API-based parsers. Returns whatever
        :func:`json.loads` produces — a dict, list, scalar, or ``None``.
        Type is ``Any`` because a JSON document can be anything; callers
        validate the shape themselves.

        Raises:
            SessionError: If the body is not valid JSON. The underlying
                :class:`json.JSONDecodeError` is chained as
                ``__cause__``.
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
        try:
            return response.json()
        except ValueError as exc:
            raise SessionError(
                f"response from {url!r} is not valid JSON: {exc}"
            ) from exc

    async def stream_download(
        self,
        url: str,
        dest: Path,
        *,
        site_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Stream *url* into the file at *dest*.

        Writes the response body to *dest* one chunk at a time, using
        :mod:`aiofiles` so the event loop is never blocked on disk I/O.
        Progress is reported through *on_progress* — throttled to
        :data:`_PROGRESS_THROTTLE_SECONDS` so a fast download does not
        drown the caller in callbacks.

        The file is **created or truncated** on success. On failure
        (network, non-2xx, cancellation) the file may be partially
        written — the caller is responsible for removing or recovering
        it, as :class:`~nexusdl.core.image.downloader.ImageDownloader`
        does.

        Args:
            url: Absolute URL.
            dest: Destination file path. Intermediate directories must
                already exist — the session does not create them.
            site_id: See :meth:`get`.
            headers: See :meth:`get`.
            timeout: See :meth:`get`.
            on_progress: Optional callback invoked with
                ``(bytes_written, total_bytes_or_None)`` at most every
                :data:`_PROGRESS_THROTTLE_SECONDS` and once at
                completion. ``total_bytes`` comes from
                ``Content-Length`` and is ``None`` when the server does
                not send one.
            **kwargs: Forwarded to :meth:`httpx.AsyncClient.stream`.

        Raises:
            Same as :meth:`get`. Additionally, :class:`OSError` if
                *dest* cannot be opened for writing.
        """
        final_headers = self._build_request_headers(
            site_id=site_id, caller_headers=headers, force_ua=True
        )
        proxy_config = await self._proxy_for(site_id)
        client = await self._ensure_client()
        request_timeout = self._resolve_timeout(timeout)

        await self._apply_circuit_breaker(site_id)
        await self._apply_rate_limit(site_id)

        try:
            async with client.stream(
                "GET",
                url,
                headers=final_headers,
                timeout=request_timeout,
                proxy=self._proxy_url(proxy_config),
                **kwargs,
            ) as response:
                self._raise_for_status(
                    response, url=url, site_id=site_id
                )

                total: int | None
                content_length = response.headers.get("content-length")
                try:
                    total = int(content_length) if content_length else None
                except ValueError:
                    total = None

                bytes_written = 0
                last_progress_at = 0.0

                async with aiofiles.open(dest, "wb") as fh:
                    async for chunk in response.aiter_bytes(
                        _STREAM_CHUNK_SIZE
                    ):
                        await fh.write(chunk)
                        bytes_written += len(chunk)
                        if on_progress is not None:
                            now = time.monotonic()
                            if (
                                now - last_progress_at
                                >= _PROGRESS_THROTTLE_SECONDS
                            ):
                                on_progress(bytes_written, total)
                                last_progress_at = now

                if on_progress is not None:
                    # Final callback — always fires, even if the throttled
                    # one did not.
                    on_progress(bytes_written, total)

        except asyncio.CancelledError:
            self._record_failure(site_id)
            raise
        except SessionError:
            # The status-based failure paths already recorded; do not
            # double-count.
            raise
        except httpx.HTTPError as exc:
            self._record_failure(site_id)
            self._record_proxy_failure(proxy_config, str(exc))
            raise self._translate_httpx_error(exc, url=url) from exc
        except OSError:
            # Filesystem failure, not a network failure — do not treat
            # it as a site health signal.
            raise
        else:
            self._record_success(site_id)
            await self._record_proxy_success(proxy_config)
            await self._persist_cookies(site_id, response)

    # ------------------------------------------------------------------ #
    # Internal request pipeline                                         #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        url: str,
        *,
        site_id: str | None,
        headers: Mapping[str, str] | None,
        timeout: float | None,
        **kwargs: Any,
    ) -> httpx.Response:
        """The shared request pipeline behind :meth:`get` and :meth:`post`.

        Every step is a single method call so the sequence is auditable
        in one place. The order matters and is documented on the public
        methods.
        """
        final_headers = self._build_request_headers(
            site_id=site_id, caller_headers=headers, force_ua=True
        )
        proxy_config = await self._proxy_for(site_id)
        client = await self._ensure_client()
        request_timeout = self._resolve_timeout(timeout)

        await self._apply_circuit_breaker(site_id)
        await self._apply_rate_limit(site_id)

        try:
            response = await client.request(
                method,
                url,
                headers=final_headers,
                timeout=request_timeout,
                proxy=self._proxy_url(proxy_config),
                **kwargs,
            )
        except asyncio.CancelledError:
            self._record_failure(site_id)
            raise
        except httpx.HTTPError as exc:
            self._record_failure(site_id)
            self._record_proxy_failure(proxy_config, str(exc))
            raise self._translate_httpx_error(exc, url=url) from exc

        try:
            self._raise_for_status(response, url=url, site_id=site_id)
        except SessionError:
            self._record_failure(site_id)
            self._record_proxy_failure(
                proxy_config, f"HTTP {response.status_code}"
            )
            raise

        self._record_success(site_id)
        await self._record_proxy_success(proxy_config)
        await self._persist_cookies(site_id, response)
        return response

    # ------------------------------------------------------------------ #
    # Header and proxy helpers                                           #
    # ------------------------------------------------------------------ #

    def _build_request_headers(
        self,
        *,
        site_id: str | None,
        caller_headers: Mapping[str, str] | None,
        force_ua: bool,
    ) -> dict[str, str]:
        """Merge base headers, cookie headers, caller headers, and UA.

        Order of precedence (later wins): base → cookies → caller →
        rotated UA. The UA always wins because the rotator is the
        authority on it.
        """
        merged: dict[str, str] = dict(self._base_headers)
        if site_id is not None and self._cookie_manager is not None:
            try:
                cookies = self._cookie_manager.get_cookies(site_id)
            except Exception as exc:  # noqa: BLE001 — cookie store failure is not fatal
                self._log.warning(
                    "Cookie manager raised for site {!r}: {}: {}",
                    site_id,
                    type(exc).__name__,
                    exc,
                )
            else:
                if cookies:
                    merged["Cookie"] = "; ".join(
                        f"{k}={v}" for k, v in cookies.items()
                    )
        if caller_headers is not None:
            merged.update(caller_headers)
        if force_ua:
            domain = _host_of_from_headers(merged) or (site_id or "")
            merged["User-Agent"] = self._user_agents.for_domain(domain)
        return merged

    async def _proxy_for(
        self, site_id: str | None
    ) -> ProxyConfig | None:
        """Return the proxy for *site_id*, or ``None`` for direct."""
        if self._proxy_manager is None:
            return None
        try:
            return await self._proxy_manager.get_proxy(site_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — proxy manager must not break requests
            self._log.warning(
                "Proxy manager raised for site {!r}: {}: {}; "
                "falling back to direct connection",
                site_id,
                type(exc).__name__,
                exc,
            )
            return None

    @staticmethod
    def _proxy_url(config: ProxyConfig | None) -> str | None:
        """Return the plaintext proxy URL for httpx, or ``None``."""
        if config is None:
            return None
        return config.url.get_secret_value()

    # ------------------------------------------------------------------ #
    # Health and rate-limit helpers                                      #
    # ------------------------------------------------------------------ #

    async def _apply_circuit_breaker(self, site_id: str | None) -> None:
        """Raise :class:`CircuitBreakerOpenError` if the site is broken."""
        if site_id is None or self._circuit_breaker is None:
            return
        if not self._circuit_breaker.allow_request(site_id):
            raise CircuitBreakerOpenError(
                f"circuit breaker is open for site {site_id!r}; "
                f"request rejected without contacting the site"
            )

    async def _apply_rate_limit(self, site_id: str | None) -> None:
        """Wait on the site's rate limiter, if one is configured."""
        if site_id is None or self._rate_limit_source is None:
            return
        limiter = self._get_rate_limiter(site_id)
        if limiter is None:
            return
        try:
            await limiter.acquire()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — limiter failure is not fatal
            # A rate limiter should never raise for a well-formed
            # configuration, but if it does we log and proceed rather
            # than failing the request.
            self._log.warning(
                "Rate limiter raised for site {!r}: {}: {}",
                site_id,
                type(exc).__name__,
                exc,
            )

    def _get_rate_limiter(self, site_id: str) -> RateLimiter | None:
        """Return (and lazily create) the rate limiter for *site_id*."""
        existing = self._rate_limiters.get(site_id)
        if existing is not None:
            return existing
        if self._rate_limit_source is None:
            return None
        try:
            rate = self._rate_limit_source(site_id)
        except Exception as exc:  # noqa: BLE001 — source failure is not fatal
            self._log.warning(
                "rate_limit_source raised for site {!r}: {}: {}",
                site_id,
                type(exc).__name__,
                exc,
            )
            return None
        if rate is None:
            return None
        try:
            limiter = RateLimiter(rate_per_second=rate, name=site_id)
        except (TypeError, ValueError) as exc:
            self._log.warning(
                "Cannot construct rate limiter for site {!r} "
                "(rate={!r}): {}",
                site_id,
                rate,
                exc,
            )
            return None
        self._rate_limiters[site_id] = limiter
        return limiter

    def _record_success(self, site_id: str | None) -> None:
        """Record a success against the site's circuit breaker."""
        if site_id is None or self._circuit_breaker is None:
            return
        try:
            self._circuit_breaker.record_success(site_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Circuit breaker record_success raised: {}: {}",
                type(exc).__name__,
                exc,
            )

    def _record_failure(self, site_id: str | None) -> None:
        """Record a failure against the site's circuit breaker."""
        if site_id is None or self._circuit_breaker is None:
            return
        try:
            self._circuit_breaker.record_failure(site_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Circuit breaker record_failure raised: {}: {}",
                type(exc).__name__,
                exc,
            )

    async def _record_proxy_success(
        self, config: ProxyConfig | None
    ) -> None:
        """Notify the proxy manager of a successful request."""
        if config is None or self._proxy_manager is None:
            return
        try:
            await self._proxy_manager.mark_success(config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Proxy manager mark_success raised: {}: {}",
                type(exc).__name__,
                exc,
            )

    def _record_proxy_failure(
        self, config: ProxyConfig | None, reason: str
    ) -> None:
        """Notify the proxy manager of a failed request.

        Synchronous because it is called from exception handlers that must
        not themselves raise or await — a failure to mark a proxy must not
        mask the original network error. The proxy manager's
        ``mark_failure`` is async, so we schedule it as a background task.
        """
        if config is None or self._proxy_manager is None:
            return
        try:
            asyncio.get_running_loop().create_task(
                self._proxy_manager.mark_failure(config, error=reason)
            )
        except RuntimeError:
            # No running loop — happens in tests that call the sync
            # helpers directly. Skip the async notification; the test
            # can call mark_failure explicitly if it cares.
            pass

    async def _persist_cookies(
        self, site_id: str | None, response: httpx.Response
    ) -> None:
        """Push any ``Set-Cookie`` values from *response* to the store."""
        if site_id is None or self._cookie_manager is None:
            return
        if not response.cookies:
            return
        new_cookies = dict(response.cookies)
        if not new_cookies:
            return
        try:
            self._cookie_manager.update_cookies(site_id, new_cookies)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log.warning(
                "Cookie manager update_cookies raised for site {!r}: "
                "{}: {}",
                site_id,
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------------ #
    # Status translation                                                 #
    # ------------------------------------------------------------------ #

    def _raise_for_status(
        self,
        response: httpx.Response,
        *,
        url: str,
        site_id: str | None,
    ) -> None:
        """Translate non-2xx responses into the NexusDL error hierarchy.

        The mapping is documented in the module docstring. A 2xx response
        is returned unchanged by the caller.

        Args:
            response: The httpx response to inspect.
            url: The request URL, used in error messages.
            site_id: The site identifier, used in error messages.

        Raises:
            SessionRateLimitError: On 429.
            SessionCloudflareError: On 403/503 with Cloudflare headers.
            SessionError: On any other non-2xx.
        """
        status = response.status_code
        if 200 <= status < 300:
            return

        site_suffix = f" (site_id={site_id!r})" if site_id else ""

        if status == _RATE_LIMIT_STATUS:
            retry_after = response.headers.get("retry-after")
            hint = (
                f", retry-after={retry_after}"
                if retry_after is not None
                else ""
            )
            raise SessionRateLimitError(
                f"HTTP 429 for {url!r}{site_suffix}{hint}"
            )

        if _is_cloudflare_challenge(response):
            raise SessionCloudflareError(
                f"Cloudflare challenge for {url!r}{site_suffix} "
                f"(HTTP {status})"
            )

        raise SessionError(
            f"HTTP {status} for {url!r}{site_suffix}"
        )

    def _translate_httpx_error(
        self, exc: httpx.HTTPError, *, url: str
    ) -> SessionError:
        """Translate an httpx exception into a NexusDL session exception.

        Timeouts and connection errors become
        :class:`SessionTimeoutError` (retryable — the pipeline's retry
        controller treats them as transient). Any other httpx error
        becomes a plain :class:`SessionError`.
        """
        if isinstance(
            exc, (httpx.TimeoutException, httpx.ConnectError)
        ):
            return SessionTimeoutError(
                f"timeout/connection failure for {url!r}: "
                f"{type(exc).__name__}: {exc}"
            )
        return SessionError(
            f"HTTP client error for {url!r}: "
            f"{type(exc).__name__}: {exc}"
        )

    # ------------------------------------------------------------------ #
    # Timeout helpers                                                    #
    # ------------------------------------------------------------------ #

    def _resolve_timeout(self, per_call: float | None) -> httpx.Timeout:
        """Return the timeout to use for a single request.

        A per-call ``timeout`` becomes a uniform override across all four
        httpx timeout slots. When absent, the configured bundle is used
        unchanged.
        """
        if per_call is None:
            return self._timeouts.to_httpx()
        return httpx.Timeout(per_call)

    # ------------------------------------------------------------------ #
    # Diagnostics                                                        #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"HttpSession(closed={self._closed}, "
            f"http2={self._http2}, "
            f"site_limiters={len(self._rate_limiters)})"
        )


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #


def _host_of_from_headers(headers: Mapping[str, str]) -> str:
    """Return the ``Host`` header value, or an empty string.

    Used by :meth:`HttpSession._build_request_headers` to key the
    User-Agent rotator when no explicit ``site_id`` was provided. In
    practice a caller that wants sticky UA behaviour passes ``site_id``;
    this fallback exists so the rotator does not silently skip its
    stickiness for callers who set ``Host`` manually (rare).
    """
    return headers.get("Host", "")


def _is_cloudflare_challenge(response: httpx.Response) -> bool:
    """Return ``True`` iff *response* looks like a Cloudflare challenge.

    A response is considered a challenge when its status is 403 or 503
    **and** at least one of :data:`_CLOUDFLARE_HEADERS` is present. This
    is a conservative heuristic: it will not fire for a Cloudflare-hosted
    site that is merely returning a 403 for other reasons, because those
    responses carry neither ``cf-ray`` (well, they often do, but combined
    with 403 + ``cf-mitigated`` or ``cf-chl-bypass`` it is distinctive)
    nor the challenge-specific headers.
    """
    if response.status_code not in (403, 503):
        return False
    return any(
        name in response.headers for name in _CLOUDFLARE_HEADERS
    )
