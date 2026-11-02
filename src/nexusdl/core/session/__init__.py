"""Public API of the NexusDL HTTP session subsystem.

The :mod:`nexusdl.core.session` package is the pipeline's entire network
layer. It owns one request/response lifecycle per call, layered on top of
``httpx`` with every policy the pipeline needs to talk to a source site
politely, safely, and with consistent error semantics.

Ten modules cooperate, each with a single responsibility:

* :mod:`~nexusdl.core.session.http_session` — the **main entry point**.
  :class:`HttpSession` wraps a single :class:`httpx.AsyncClient` and
  composes all the collaborators below.
* :mod:`~nexusdl.core.session.rate_limiter` — per-site token-bucket
  rate limiter (:class:`RateLimiter`).
* :mod:`~nexusdl.core.session.circuit_breaker` — per-site state machine
  that short-circuits requests to a broken site
  (:class:`CircuitBreaker`, :class:`CircuitBreakerPool`).
* :mod:`~nexusdl.core.session.cookie_manager` — encrypted persistent
  cookie store with env-var overrides (:class:`CookieManager`).
* :mod:`~nexusdl.core.session.user_agents` — per-domain sticky UA
  rotation (:class:`UserAgentRotator`).
* :mod:`~nexusdl.core.session.proxy_manager` — proxy pool, per-site
  binding, health-based rotation (:class:`ProxyManager`).
* :mod:`~nexusdl.core.session.playwright_pool` — managed Chromium
  context pool for JS-rendered sites (:class:`PlaywrightPool`).
* :mod:`~nexusdl.core.session.flaresolverr` — external FlareSolverr
  client for Cloudflare challenges (:class:`FlareSolverrClient`).
* :mod:`~nexusdl.core.session.cache` — on-disk HTTP response cache with
  RFC 7234 semantics (:class:`HttpCache`).

Composition
-----------
The composition root wires the session with the collaborators it needs.
Every collaborator is **optional** — a session with no arguments works
with no rate limiting, no circuit breaking, no cookies, no proxies, no
cache, no Playwright, no FlareSolverr. Each feature is enabled by
supplying the corresponding instance::

    from pathlib import Path
    from nexusdl.core.session import (
        CircuitBreakerPool,
        CookieManager,
        HttpCache,
        HttpSession,
        ProxyManager,
        UserAgentRotator,
        load_proxy_configs,
    )

    session = HttpSession(
        user_agents=UserAgentRotator(),
        circuit_breaker=CircuitBreakerPool(failure_threshold=5),
        cookie_manager=CookieManager(
            key=settings.cookie_encryption_key,
            path=settings.paths.cookies_file,
            env=os.environ,
            known_sites=[s.id for s in registry.list_sites(
                include_disabled=True
            )],
        ),
        proxy_manager=ProxyManager(load_proxy_configs(settings.proxy)),
        rate_limit_source=lambda sid: site_rates.get(sid),
        cache=HttpCache(
            cache_dir=settings.paths.cache_dir / "http",
            per_site_ttl=settings.session.cache.per_site_ttl,
        ),
    )

    try:
        html = await session.get_html(
            "https://mangadex.org/title/xyz", site_id="mangadex"
        )
    finally:
        await session.close()

Three integration levels
------------------------
Different callers need different amounts of the session surface:

* **Parsers** use :meth:`HttpSession.get`,
  :meth:`~HttpSession.get_html`, :meth:`~HttpSession.get_json`,
  :meth:`~HttpSession.post`, and :meth:`~HttpSession.stream_download`.
  They never touch collaborators directly — the session composes them.
* **Composition roots** construct the collaborators and pass them to
  :class:`HttpSession`. They own the session's lifetime, closing it at
  shutdown.
* **Diagnostic endpoints and the CLI** read collaborator ``stats()``
  methods to render status panels (``GET /api/v1/sites/{id}/health``,
  ``nexusdl session stats``, …).

Protocols for injection
-----------------------
The session accepts three of its collaborators through **structural
protocols** so that:

* tests can inject stubs without instantiating the real collaborator,
* plugins can provide alternative implementations (a different cache
  backend, a different proxy manager) without subclassing the shipped
  ones.

Two of those protocols are defined in this package:

* :class:`CircuitBreakerProtocol` — ``allow_request(site_id)``,
  ``record_success(site_id)``, ``record_failure(site_id)``. Satisfied by
  :class:`CircuitBreakerPool`.
* :class:`CookieManagerProtocol` — ``get_cookies(site_id)``,
  ``update_cookies(site_id, cookies)``. Satisfied by
  :class:`CookieManager`.

Both are exported because a caller implementing a custom breaker or
cookie store needs the type to satisfy ``mypy --strict``. There is a
third collaborator — the rate limiter — which is injected indirectly
through a ``rate_limit_source`` callback rather than a protocol; that
indirection exists because the source needs to be per-request (the
limiter depends on ``site_id``, which the composition root knows but the
session does not).

No retry, by design
-------------------
The session performs **exactly one request per call**. Retry is the
caller's responsibility and lives in
:mod:`nexusdl.core.downloader.retry`. See
:mod:`nexusdl.core.session.http_session` for the rationale — putting
retry inside the session would multiply attempts and slow the worst
case with no benefit.

Cancellation
------------
``asyncio.CancelledError`` propagates untouched from every public
method. Partial files left by a cancelled :meth:`HttpSession.stream_download`
call are the caller's responsibility; the canonical consumer,
:class:`~nexusdl.core.image.downloader.ImageDownloader`, deletes them in
its own ``finally``.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are ambiguous across
submodules or private to their module and are allowed to change without
notice:

* ``RotationStrategy`` — defined in both ``user_agents.py`` and
  ``proxy_manager.py`` with the same ``Literal["random", "round_robin"]``
  members. The user_agents version is re-exported here because it is the
  first defined; a caller who needs the proxy_manager version imports it
  from that submodule. ``mypy`` treats both as the same type, so a
  caller passing a ``RotationStrategy`` from one module to a constructor
  in the other checks cleanly.
* ``DEFAULT_TIMEOUT_MS`` — ``30_000`` in ``playwright_pool.py``,
  ``60_000`` in ``flaresolverr.py``. Different values, different units of
  meaning. Import from the specific submodule.
* ``DEFAULT_FAILURE_THRESHOLD`` — ``5`` in ``circuit_breaker.py``, ``3``
  in ``proxy_manager.py``. Two different tuning defaults. Import from the
  specific submodule.
* Every private ``_*`` helper — module internals.
* ``_DEFAULT_HEADERS``, ``_STREAM_CHUNK_SIZE``, ``_PROGRESS_THROTTLE_SECONDS``
  in ``http_session.py``; ``_DEFAULT_BROWSER_ARGS`` in
  ``playwright_pool.py``; ``_CACHEABLE_METHODS``, ``_CACHEABLE_CONTENT_PREFIXES``
  in ``cache.py`` — tuning constants, not part of the contract.
* The ``DEFAULT_*`` module constants that would collide with a
  same-named constant in a sibling submodule: see the three names above.

Dependency graph (no cycles)::

    core.exceptions
         ▲
         │
    ┌────┴──────────────────────────────────────────────────────────┐
    │                                                                │
    user_agents   rate_limiter   proxy_manager   playwright_pool    │
    flaresolverr  cookie_manager circuit_breaker cache              │
         ▲             ▲             ▲             ▲                  │
         │             │             │             │                  │
         └─────────────┴──────┬──────┴─────────────┴──────────────────┘
                              │
                        http_session
                              │
                        (composition root)

``http_session`` depends on ``user_agents``, ``rate_limiter``, and
``proxy_manager`` directly (imports for construction of defaults and for
its per-call integration); it depends on ``circuit_breaker``,
``cookie_manager``, ``playwright_pool``, ``flaresolverr``, and ``cache``
only through Protocols or optional parameters, so those modules can
evolve independently. Every submodule is importable on its own — nothing
in this package requires another module at import time except
``http_session``.
"""

from __future__ import annotations

# --- HTTP session (main entry point + injection protocols) ---------------- #
from nexusdl.core.session.http_session import (
    DEFAULT_TIMEOUTS,
    CircuitBreakerProtocol,
    CookieManagerProtocol,
    HttpSession,
    HttpTimeouts,
)

# --- Rate limiter ------------------------------------------------------- #
from nexusdl.core.session.rate_limiter import (
    RateLimiter,
    RateLimiterStats,
)

# --- Circuit breaker ---------------------------------------------------- #
from nexusdl.core.session.circuit_breaker import (
    DEFAULT_HALF_OPEN_MAX_ATTEMPTS,
    DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD,
    DEFAULT_RECOVERY_TIMEOUT,
    CircuitBreaker,
    CircuitBreakerPool,
    CircuitBreakerPoolStats,
    CircuitBreakerState,
    CircuitBreakerStats,
)

# --- Cookie manager ----------------------------------------------------- #
from nexusdl.core.session.cookie_manager import (
    ENV_PREFIX,
    FILE_VERSION,
    CookieEntry,
    CookieManager,
    CookieStats,
)

# --- User-Agent rotation ------------------------------------------------ #
from nexusdl.core.session.user_agents import (
    DEFAULT_STICKY_TTL,
    DEFAULT_USER_AGENTS,
    USER_AGENTS_FILENAME,
    RotationStrategy,
    UserAgentRotator,
    load_user_agents,
)

# --- Proxy manager ------------------------------------------------------ #
from nexusdl.core.session.proxy_manager import (
    DEFAULT_COOLDOWN_SECONDS,
    ProxyConfig,
    ProxyHealth,
    ProxyManager,
    ProxyStats,
    load_proxy_configs,
)

# --- Playwright pool ---------------------------------------------------- #
from nexusdl.core.session.playwright_pool import (
    DEFAULT_MAX_CONTEXTS,
    PlaywrightPool,
    PlaywrightPoolStats,
)

# --- FlareSolverr client ------------------------------------------------ #
from nexusdl.core.session.flaresolverr import (
    DEFAULT_ENDPOINT,
    FlareSolverrClient,
    FlareSolverrResult,
)

# --- HTTP cache --------------------------------------------------------- #
from nexusdl.core.session.cache import (
    DEFAULT_DEFAULT_TTL,
    DEFAULT_EVICTION_TARGET_RATIO,
    DEFAULT_MAX_SIZE_BYTES,
    CacheEntry,
    CacheStats,
    HttpCache,
    parse_cache_control,
)

__all__ = [
    # --- HTTP session --------------------------------------------------- #
    "HttpSession",
    "HttpTimeouts",
    "DEFAULT_TIMEOUTS",
    "CircuitBreakerProtocol",
    "CookieManagerProtocol",
    # --- Rate limiter --------------------------------------------------- #
    "RateLimiter",
    "RateLimiterStats",
    # --- Circuit breaker ------------------------------------------------ #
    "CircuitBreaker",
    "CircuitBreakerPool",
    "CircuitBreakerState",
    "CircuitBreakerStats",
    "CircuitBreakerPoolStats",
    "DEFAULT_RECOVERY_TIMEOUT",
    "DEFAULT_HALF_OPEN_MAX_ATTEMPTS",
    "DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD",
    # --- Cookie manager ------------------------------------------------- #
    "CookieManager",
    "CookieEntry",
    "CookieStats",
    "ENV_PREFIX",
    "FILE_VERSION",
    # --- User-Agent rotation -------------------------------------------- #
    "UserAgentRotator",
    "RotationStrategy",
    "DEFAULT_STICKY_TTL",
    "DEFAULT_USER_AGENTS",
    "USER_AGENTS_FILENAME",
    "load_user_agents",
    # --- Proxy manager -------------------------------------------------- #
    "ProxyManager",
    "ProxyConfig",
    "ProxyHealth",
    "ProxyStats",
    "load_proxy_configs",
    "DEFAULT_COOLDOWN_SECONDS",
    # --- Playwright pool ------------------------------------------------ #
    "PlaywrightPool",
    "PlaywrightPoolStats",
    "DEFAULT_MAX_CONTEXTS",
    # --- FlareSolverr client -------------------------------------------- #
    "FlareSolverrClient",
    "FlareSolverrResult",
    "DEFAULT_ENDPOINT",
    # --- HTTP cache ----------------------------------------------------- #
    "HttpCache",
    "CacheEntry",
    "CacheStats",
    "parse_cache_control",
    "DEFAULT_MAX_SIZE_BYTES",
    "DEFAULT_DEFAULT_TTL",
    "DEFAULT_EVICTION_TARGET_RATIO",
]
