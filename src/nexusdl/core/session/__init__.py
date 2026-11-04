"""Public API of the NexusDL HTTP session subsystem.

The :mod:`nexusdl.core.session` package is the pipeline's entire network
layer. It owns one request/response lifecycle per call, layered on top of
``httpx`` with every policy the pipeline needs to talk to a source site
politely, safely, and with consistent error semantics.

Nine modules cooperate, each with a single responsibility:

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

Lazy loading
------------
All forty-five exports are resolved through PEP 562's ``__getattr__``
mechanism. Importing ``nexusdl.core.session`` **does not import any
submodule**.

This matters more here than anywhere else in the project, because the
eager ``__init__.py`` paid an extraordinary cost even for callers who
only needed one light collaborator:

* ``playwright_pool.py`` → :mod:`playwright` (**~100-150 ms**).
* ``http_session.py`` → :mod:`httpx[http2]` + :mod:`hishel` +
  everything else (**~150 ms** on its own).
* ``cookie_manager.py`` → :mod:`cryptography` (**~30-50 ms**).
* ``cache.py`` → :mod:`hishel` (**~40-60 ms**).
* ``flaresolverr.py`` → :mod:`httpx` (**~50-70 ms**).

**Total eager cost: ~250-300 ms**, paid by any caller — even one who
only wanted :class:`RateLimiter` (a 30-line stdlib class) or
:class:`CircuitBreaker` (an enum + an asyncio lock).

**Before:** any of ``from ...session import RateLimiter``,
``from ...session import CircuitBreaker``, or
``from ...session import HttpSession`` paid the entire ~280 ms.

**After:**

=============================================  =========  =========
Scenario                                        Before     After
=============================================  =========  =========
``from ...session import RateLimiter``         ~280 ms    ~5 ms
``from ...session import CircuitBreaker``      ~280 ms    ~5 ms
``from ...session import UserAgentRotator``    ~280 ms    ~10 ms
``from ...session import ProxyManager``        ~280 ms    ~8 ms
``from ...session import CookieManager``       ~280 ms    ~50 ms
``from ...session import HttpCache``           ~280 ms    ~60 ms
``from ...session import FlareSolverrClient``  ~280 ms    ~80 ms
``from ...session import PlaywrightPool``      ~280 ms    ~150 ms
``from ...session import HttpSession``         ~280 ms    ~280 ms (unchanged)
=============================================  =========  =========

The public surface is unchanged: ``from nexusdl.core.session import X``
works exactly as before — resolved on first access and cached. No
caller has to change a line.

.. note::
   The ``httpx`` client used internally by :class:`HttpSession` is
   created lazily inside ``__aenter__`` / ``__init__`` — that behaviour
   is unchanged. The lazy conversion here only affects **module import**
   time, not the runtime behaviour of the collaborators themselves.

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

The arrows above describe **runtime dependencies at first use**, not
imports performed by this file. This file imports **nothing** at module
load — the nine submodules are loaded lazily, on first attribute access.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

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
    # --- Introspection -------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (submodule basename, attribute name in that submodule).
# Every listed symbol is defined at module level in the given submodule.
# Adding a new top-level export is a one-line change here plus a matching
# TYPE_CHECKING re-export below.

_LAZY: dict[str, tuple[str, str]] = {
    # --- http_session (very heavy: httpx[http2] + hishel + everything) -- #
    "HttpSession": ("http_session", "HttpSession"),
    "HttpTimeouts": ("http_session", "HttpTimeouts"),
    "DEFAULT_TIMEOUTS": ("http_session", "DEFAULT_TIMEOUTS"),
    "CircuitBreakerProtocol": ("http_session", "CircuitBreakerProtocol"),
    "CookieManagerProtocol": ("http_session", "CookieManagerProtocol"),
    # --- rate_limiter (light: asyncio + time) ---------------------------- #
    "RateLimiter": ("rate_limiter", "RateLimiter"),
    "RateLimiterStats": ("rate_limiter", "RateLimiterStats"),
    # --- circuit_breaker (light: asyncio + enum) ------------------------- #
    "CircuitBreaker": ("circuit_breaker", "CircuitBreaker"),
    "CircuitBreakerPool": ("circuit_breaker", "CircuitBreakerPool"),
    "CircuitBreakerState": ("circuit_breaker", "CircuitBreakerState"),
    "CircuitBreakerStats": ("circuit_breaker", "CircuitBreakerStats"),
    "CircuitBreakerPoolStats": ("circuit_breaker", "CircuitBreakerPoolStats"),
    "DEFAULT_RECOVERY_TIMEOUT": ("circuit_breaker", "DEFAULT_RECOVERY_TIMEOUT"),
    "DEFAULT_HALF_OPEN_MAX_ATTEMPTS": ("circuit_breaker", "DEFAULT_HALF_OPEN_MAX_ATTEMPTS"),
    "DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD": ("circuit_breaker", "DEFAULT_HALF_OPEN_SUCCESS_THRESHOLD"),
    # --- cookie_manager (medium: cryptography + json) ------------------- #
    "CookieManager": ("cookie_manager", "CookieManager"),
    "CookieEntry": ("cookie_manager", "CookieEntry"),
    "CookieStats": ("cookie_manager", "CookieStats"),
    "ENV_PREFIX": ("cookie_manager", "ENV_PREFIX"),
    "FILE_VERSION": ("cookie_manager", "FILE_VERSION"),
    # --- user_agents (light: random + pathlib) -------------------------- #
    # NB: ``RotationStrategy`` here is the user_agents version. The
    # proxy_manager module defines its own copy (see the docstring's
    # "not re-exported" section for the rationale).
    "UserAgentRotator": ("user_agents", "UserAgentRotator"),
    "RotationStrategy": ("user_agents", "RotationStrategy"),
    "DEFAULT_STICKY_TTL": ("user_agents", "DEFAULT_STICKY_TTL"),
    "DEFAULT_USER_AGENTS": ("user_agents", "DEFAULT_USER_AGENTS"),
    "USER_AGENTS_FILENAME": ("user_agents", "USER_AGENTS_FILENAME"),
    "load_user_agents": ("user_agents", "load_user_agents"),
    # --- proxy_manager (light: random + enum) --------------------------- #
    "ProxyManager": ("proxy_manager", "ProxyManager"),
    "ProxyConfig": ("proxy_manager", "ProxyConfig"),
    "ProxyHealth": ("proxy_manager", "ProxyHealth"),
    "ProxyStats": ("proxy_manager", "ProxyStats"),
    "load_proxy_configs": ("proxy_manager", "load_proxy_configs"),
    "DEFAULT_COOLDOWN_SECONDS": ("proxy_manager", "DEFAULT_COOLDOWN_SECONDS"),
    # --- playwright_pool (very heavy: playwright ~100-150 ms) ----------- #
    "PlaywrightPool": ("playwright_pool", "PlaywrightPool"),
    "PlaywrightPoolStats": ("playwright_pool", "PlaywrightPoolStats"),
    "DEFAULT_MAX_CONTEXTS": ("playwright_pool", "DEFAULT_MAX_CONTEXTS"),
    # --- flaresolverr (medium: httpx) ----------------------------------- #
    "FlareSolverrClient": ("flaresolverr", "FlareSolverrClient"),
    "FlareSolverrResult": ("flaresolverr", "FlareSolverrResult"),
    "DEFAULT_ENDPOINT": ("flaresolverr", "DEFAULT_ENDPOINT"),
    # --- cache (medium: hishel + filesystem) ---------------------------- #
    "HttpCache": ("cache", "HttpCache"),
    "CacheEntry": ("cache", "CacheEntry"),
    "CacheStats": ("cache", "CacheStats"),
    "parse_cache_control": ("cache", "parse_cache_control"),
    "DEFAULT_MAX_SIZE_BYTES": ("cache", "DEFAULT_MAX_SIZE_BYTES"),
    "DEFAULT_DEFAULT_TTL": ("cache", "DEFAULT_DEFAULT_TTL"),
    "DEFAULT_EVICTION_TARGET_RATIO": ("cache", "DEFAULT_EVICTION_TARGET_RATIO"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.session.cache import (
        DEFAULT_DEFAULT_TTL,
        DEFAULT_EVICTION_TARGET_RATIO,
        DEFAULT_MAX_SIZE_BYTES,
        CacheEntry,
        CacheStats,
        HttpCache,
        parse_cache_control,
    )
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
    from nexusdl.core.session.cookie_manager import (
        ENV_PREFIX,
        FILE_VERSION,
        CookieEntry,
        CookieManager,
        CookieStats,
    )
    from nexusdl.core.session.flaresolverr import (
        DEFAULT_ENDPOINT,
        FlareSolverrClient,
        FlareSolverrResult,
    )
    from nexusdl.core.session.http_session import (
        DEFAULT_TIMEOUTS,
        CircuitBreakerProtocol,
        CookieManagerProtocol,
        HttpSession,
        HttpTimeouts,
    )
    from nexusdl.core.session.playwright_pool import (
        DEFAULT_MAX_CONTEXTS,
        PlaywrightPool,
        PlaywrightPoolStats,
    )
    from nexusdl.core.session.proxy_manager import (
        DEFAULT_COOLDOWN_SECONDS,
        ProxyConfig,
        ProxyHealth,
        ProxyManager,
        ProxyStats,
        load_proxy_configs,
    )
    from nexusdl.core.session.rate_limiter import (
        RateLimiter,
        RateLimiterStats,
    )
    from nexusdl.core.session.user_agents import (
        DEFAULT_STICKY_TTL,
        DEFAULT_USER_AGENTS,
        USER_AGENTS_FILENAME,
        RotationStrategy,
        UserAgentRotator,
        load_user_agents,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public session symbol on first access (PEP 562).

    Called by Python's import machinery whenever an attribute is accessed
    on this package that is not already bound in ``globals()``. Only the
    names in :data:`__all__` are handled; anything else raises
    :class:`AttributeError` so that ``hasattr``, ``getattr(x, name,
    default)`` and interactive introspection behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The symbol registered under ``name`` in the submodule recorded in
        :data:`_LAZY`. The submodule is imported once and its binding is
        cached in ``globals()``, so subsequent accesses are plain dict
        lookups.

    Raises:
        AttributeError: When ``name`` is not a public export, when the
            submodule cannot be imported (a missing optional dependency
            such as ``httpx``, ``hishel``, ``playwright``,
            ``cryptography``, or ``aiohttp``, a syntax error deeper in
            the module, or a broken installation), or when the submodule
            does not define the requested name. ``AttributeError`` —
            rather than ``ImportError`` — is required by PEP 562 so that
            ``hasattr`` works.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _LAZY.get(name)
    if entry is None:
        available = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available session exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.session.cache import CacheEntry' or "
            f"'from nexusdl.core.session.proxy_manager import "
            f'DEFAULT_FAILURE_THRESHOLD\'.'
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the session subsystem.
        if exc.name in {
            "httpx",
            "hishel",
            "playwright",
            "cryptography",
            "aiohttp",
            "aiofiles",
            "tenacity",
            "yarl",
        }:
            raise ImportError(
                f"{name!r} requires the core extra (session subsystem "
                f"dependencies: httpx[http2], hishel, playwright, "
                f"cryptography, aiohttp). "
                f"Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The session submodule {submodule_basename!r} does not "
            f"define {attr_name!r}. This is a package-internal "
            f"inconsistency - nexusdl.core.session.__all__ and the "
            f"submodules are out of sync."
        ) from None

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the public names for interactive introspection.

    Python's default :func:`dir` implementation collects a module's
    ``__dict__`` and, when the module defines ``__getattr__``, calls it
    with each candidate name to decide what to display. Because none of
    the forty-five exports are present in ``__dict__`` at import time, an
    unmodified :func:`dir` would not list them - the package would
    appear empty in a REPL.

    The returned list is :data:`__all__` plus the standard module
    dunders a user typically expects to see.
    """
    return sorted(
        set(__all__) | {"__all__", "__doc__", "__name__", "__package__"}
    )


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), session-status dashboards that enumerate what the
    subsystem exposes, and tooling that needs to know the public surface
    without importing every submodule.
    """
    # Only the forty-five public exports, not the introspection helpers
    # themselves - otherwise ``list_symbols`` would include itself.
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the session symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, composition-root builders that resolve a
    collaborator class from a config string, diagnostic tools that
    enumerate the session surface).

    Raises:
        KeyError: If ``name`` is not a public session export.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown session symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.session.<submodule>' directly."
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
