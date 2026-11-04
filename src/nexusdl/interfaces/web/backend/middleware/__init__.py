"""Middleware package for the NexusDL web backend.

Groups the four ASGI middlewares that wrap the FastAPI application and
provides a **composition helper** that sidesteps Starlette's LIFO
middleware trap.

Why this file exists
====================

Starlette's ``app.add_middleware()`` pushes onto a stack: **the last
middleware added is the first one to run** at request time. Combined
with the fact that middleware author order in source code reads
top-down (``logging`` before ``cors`` before ``rate_limit`` before
``auth``), this produces a classic bug: the developer writes:

    app.add_middleware(LoggingMiddleware, ...)
    app.add_middleware(CORSMiddleware, ...)
    app.add_middleware(RateLimitMiddleware, ...)
    app.add_middleware(AuthMiddleware, ...)

and gets runtime order:

    Auth → RateLimit → CORS → Logging → app   (WRONG)

The rate limiter then sees every request as unauthenticated (auth has
not run yet), the CORS middleware cannot add headers to logging's
output (logging runs last), and the logging middleware cannot log
``user_id`` (auth has not run at that point in the stack).

By contrast, FastAPI's ``middleware=[Middleware(...), ...]`` parameter
applies the list **top-to-bottom at runtime** — exactly the mental
model the developer expects.

:func:`build_middleware_stack` is the single sanctioned entry point.
It returns a list of :class:`starlette.middleware.Middleware` objects
in **runtime order**, ready to be passed to ``FastAPI(middleware=...)``.
Every deployment *must* use it — the individual ``add_middleware``
calls are explicitly discouraged in this codebase.

The canonical runtime order
===========================

    Logging   →   CORS   →   RateLimit   →   Auth   →   app

Justification, outermost to innermost:

* **Logging** wraps everything so it captures 401s, 429s, preflights,
  and unexpected 500s with the same structure.
* **CORS** must sit inside logging (so preflights are logged) but
  outside rate-limit and auth (so preflights never consume a rate-limit
  token and never require authentication — browsers don't send
  credentials on preflights).
* **RateLimit** must sit inside auth (so it can bucket by ``user``
  instead of falling back to IP) — but that requires auth to have run
  first, which means auth must be **outside** rate-limit. Contradiction?
  No: auth is inside rate-limit in the *call stack* sense, but rate-limit
  reads ``scope["state"]["user_id"]`` which auth populates. In the
  correct ordering above, auth runs *inside* rate-limit's dispatch — so
  rate-limit executes **before** auth and would see no user.
  Correction below.

Wait — the actual mechanism matters. Rate-limit and auth both run
inside the ASGI dispatch chain. Rate-limit reads
``scope["state"]["user_id"]`` at *its* entry point. If auth runs
*after* rate-limit, the state is empty when rate-limit reads it.

The correct runtime order therefore is:

    Logging   →   CORS   →   Auth   →   RateLimit   →   app

This means **auth runs before rate-limit**, populating the state that
rate-limit consumes. Logging still wraps everything (it sees the
final status after both), and CORS still sits outside both (it
short-circuits preflights).

**This is the order :func:`build_middleware_stack` returns**, and it
differs from an intuitive "rate-limit first" reading. Trust the
helper, not the intuition.

Loading strategy
================

All exports are **lazy** via :pep:`562` (``__getattr__``). Importing
this package never imports a middleware module, never imports
``starlette``, never imports ``fastapi``. Symbols are resolved on first
attribute access and cached in ``globals()``.

Why lazy
--------

* ``middleware/__init__.py`` may be imported by tests that only check
  discovery (``list_middlewares()``) or by plugin code that wants a
  subset. Paying for four module imports that import ``loguru``,
  ``urllib.parse``, ``fnmatch``, ``re``… is a waste.
* :func:`build_middleware_stack` itself is **callable without any
  middleware being instantiated**. It imports ``starlette`` lazily and
  returns the ``Middleware(...)`` list for FastAPI to instantiate later.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# --- Package version (soft read from nexusdl.version) -----------------------

try:
    from nexusdl.version import __version__ as __version__
except Exception:  # pragma: no cover - defensive fallback
    __version__ = "3.0.0"


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # ----- Logging -----
    "LoggingMiddleware",
    "LoggingConfig",
    "RedactionConfig",
    "RequestCompletedEvent",
    "LogLevel",
    # ----- CORS -----
    "CORSMiddleware",
    "CORSConfig",
    "CORSRule",
    "CORSAllowedEvent",
    "CORSRejectedEvent",
    "single_origin_cors",
    "dev_cors",
    "disabled_cors",
    # ----- Rate limit -----
    "RateLimitMiddleware",
    "RateLimitConfig",
    "RateLimitRule",
    "RateLimitResult",
    "RateLimitEvent",
    "RateLimitStore",
    "RateLimitAlgorithm",
    "InMemoryRateLimitStore",
    "default_rules",
    # ----- Auth -----
    "AuthMiddleware",
    "AuthConfig",
    "AuthSuccessEvent",
    "AuthFailureEvent",
    "AuthServiceLike",
    # ----- Composition -----
    "build_middleware_stack",
    "default_middleware_stack",
    "MIDDLEWARE_ORDER",
    # ----- Introspection -----
    "list_middlewares",
    "get_middleware",
]


# ===========================================================================
# Canonical runtime order
# ===========================================================================
#
# This is the order in which the middlewares must appear in the list
# returned by ``build_middleware_stack`` (and therefore in
# ``FastAPI(middleware=[...])``). See the module docstring for the
# rationale — especially why auth comes BEFORE rate_limit despite the
# intuitive reading.

MIDDLEWARE_ORDER: tuple[str, ...] = (
    "logging",
    "cors",
    "auth",
    "rate_limit",
)


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported attribute name -> (submodule basename, attribute in that
# submodule).
#
# Adding a new middleware:
#   1. create ``middleware/<name>.py`` with its own ``__all__``,
#   2. add the entries below,
#   3. add the symbols to the ``__all__`` list above and to the
#      TYPE_CHECKING block,
#   4. add the middleware key to ``MIDDLEWARE_ORDER`` (in the right
#      runtime position) and to ``_MODULE_FOR_KEY``.

_LAZY: dict[str, tuple[str, str]] = {
    # ----- logging -----
    "LoggingMiddleware": ("logging", "LoggingMiddleware"),
    "LoggingConfig": ("logging", "LoggingConfig"),
    "RedactionConfig": ("logging", "RedactionConfig"),
    "RequestCompletedEvent": ("logging", "RequestCompletedEvent"),
    "LogLevel": ("logging", "LogLevel"),
    # ----- cors -----
    "CORSMiddleware": ("cors", "CORSMiddleware"),
    "CORSConfig": ("cors", "CORSConfig"),
    "CORSRule": ("cors", "CORSRule"),
    "CORSAllowedEvent": ("cors", "CORSAllowedEvent"),
    "CORSRejectedEvent": ("cors", "CORSRejectedEvent"),
    "single_origin_cors": ("cors", "single_origin_cors"),
    "dev_cors": ("cors", "dev_cors"),
    "disabled_cors": ("cors", "disabled_cors"),
    # ----- rate_limit -----
    "RateLimitMiddleware": ("rate_limit", "RateLimitMiddleware"),
    "RateLimitConfig": ("rate_limit", "RateLimitConfig"),
    "RateLimitRule": ("rate_limit", "RateLimitRule"),
    "RateLimitResult": ("rate_limit", "RateLimitResult"),
    "RateLimitEvent": ("rate_limit", "RateLimitEvent"),
    "RateLimitStore": ("rate_limit", "RateLimitStore"),
    "RateLimitAlgorithm": ("rate_limit", "RateLimitAlgorithm"),
    "InMemoryRateLimitStore": ("rate_limit", "InMemoryRateLimitStore"),
    "default_rules": ("rate_limit", "default_rules"),
    # ----- auth -----
    "AuthMiddleware": ("auth", "AuthMiddleware"),
    "AuthConfig": ("auth", "AuthConfig"),
    "AuthSuccessEvent": ("auth", "AuthSuccessEvent"),
    "AuthFailureEvent": ("auth", "AuthFailureEvent"),
    "AuthServiceLike": ("auth", "AuthServiceLike"),
}


#: Map a middleware key (from :data:`MIDDLEWARE_ORDER`) to its submodule
#: and the concrete middleware class name. Used by
#: :func:`build_middleware_stack` to resolve configs into instances.
_MODULE_FOR_KEY: dict[str, str] = {
    "logging": "logging",
    "cors": "cors",
    "auth": "auth",
    "rate_limit": "rate_limit",
}

_CLASS_FOR_KEY: dict[str, str] = {
    "logging": "LoggingMiddleware",
    "cors": "CORSMiddleware",
    "auth": "AuthMiddleware",
    "rate_limit": "RateLimitMiddleware",
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs.

if TYPE_CHECKING:
    from .auth import (
        AuthConfig,
        AuthFailureEvent,
        AuthMiddleware,
        AuthServiceLike,
        AuthSuccessEvent,
    )
    from .cors import (
        CORSAllowedEvent,
        CORSConfig,
        CORSMiddleware,
        CORSRejectedEvent,
        CORSRule,
        dev_cors,
        disabled_cors,
        single_origin_cors,
    )
    from .logging import (
        LoggingConfig,
        LoggingMiddleware,
        LogLevel,
        RedactionConfig,
        RequestCompletedEvent,
    )
    from .rate_limit import (
        InMemoryRateLimitStore,
        RateLimitAlgorithm,
        RateLimitConfig,
        RateLimitEvent,
        RateLimitMiddleware,
        RateLimitResult,
        RateLimitRule,
        RateLimitStore,
        default_rules,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public symbol on first access (:pep:`562`).

    Raises:
        AttributeError: If ``name`` is not a known public symbol.
        ImportError: If the symbol's module cannot be imported because
            an optional dependency is missing. The message points at the
            ``[web]`` extra.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _LAZY.get(name)
    if entry is None:
        available = ", ".join(sorted(_LAZY))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available symbols: {available}"
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name in {"fastapi", "pydantic", "python_jose", "jose"}:
            raise ImportError(
                f"{name!r} requires the Web extra. "
                f"Install it with: pip install 'nexusdl[web]'"
            ) from exc
        raise

    value = getattr(module, attr_name)

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the full public API to ``dir()``, IDEs and tab-completion."""
    return sorted(list(globals().keys()) + list(__all__))


# ===========================================================================
# Composition — the sanctioned entry point
# ===========================================================================

def build_middleware_stack(
    *,
    logging: "LoggingConfig | None" = None,
    cors: "CORSConfig | None" = None,
    auth: "AuthConfig | None" = None,
    auth_service: "AuthServiceLike | None" = None,
    rate_limit: "RateLimitConfig | None" = None,
) -> list[Any]:
    """Build the middleware list for ``FastAPI(middleware=[...])``.

    Each parameter is a config object; passing ``None`` skips that
    middleware entirely (useful for tests, partial deployments, or when
    a deployment terminates a middleware at the edge — e.g. CORS handled
    by Caddy).

    The returned list is in **runtime order** (:data:`MIDDLEWARE_ORDER`),
    ready to pass to ``FastAPI(middleware=...)``. Do not re-order it and
    do not use ``add_middleware`` on top of it.

    Parameters:
        logging: :class:`LoggingConfig` — request/response logging.
        cors: :class:`CORSConfig` — Cross-Origin Resource Sharing.
        auth: :class:`AuthConfig` — JWT extraction into ``scope["state"]``.
        auth_service: Required when ``auth`` is provided. The
            :class:`AuthServiceLike` instance used to verify tokens.
        rate_limit: :class:`RateLimitConfig` — per-IP / per-user limits.

    Returns:
        A list of ``starlette.middleware.Middleware`` objects, in
        runtime order: ``logging → cors → auth → rate_limit``.

    Raises:
        ValueError: If ``auth`` is provided without ``auth_service``.
        ImportError: If :mod:`starlette` is not installed (web extra
            missing).
    """
    if auth is not None and auth_service is None:
        raise ValueError(
            "build_middleware_stack: 'auth' config provided but "
            "'auth_service' is missing. Pass the service instance or "
            "set 'auth=None' to skip the auth middleware."
        )

    # Lazy starlette import — keeps this module importable in headless
    # environments (only callers of this function need starlette).
    try:
        from starlette.middleware import Middleware  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "build_middleware_stack requires starlette. "
            "Install the web extra with: pip install 'nexusdl[web]'"
        ) from exc

    # Map each enabled key to its ``Middleware(...)`` instance.
    entries: dict[str, Any] = {}

    if logging is not None:
        entries["logging"] = Middleware(
            _resolve_class("logging"), config=logging,
        )
    if cors is not None:
        entries["cors"] = Middleware(
            _resolve_class("cors"), config=cors,
        )
    if auth is not None:
        entries["auth"] = Middleware(
            _resolve_class("auth"),
            config=auth,
            auth_service=auth_service,
        )
    if rate_limit is not None:
        entries["rate_limit"] = Middleware(
            _resolve_class("rate_limit"), config=rate_limit,
        )

    # Return in canonical runtime order, omitting disabled entries.
    return [entries[key] for key in MIDDLEWARE_ORDER if key in entries]


def default_middleware_stack(
    *,
    auth_service: "AuthServiceLike | None" = None,
    include_auth: bool = True,
    include_rate_limit: bool = True,
    include_cors: bool = True,
    include_logging: bool = True,
    cors_config: "CORSConfig | None" = None,
    slow_threshold_ms: float = 1000.0,
    trust_forwarded_for: bool = False,
    trusted_proxies: frozenset[str] = frozenset(),
) -> list[Any]:
    """Build the standard middleware list with default configurations.

    Parameters mirror the "everything on with defaults" case, which is
    what most deployments want. Anything unusual — different rate-limit
    rules, custom CORS origins, body capture — should call
    :func:`build_middleware_stack` with hand-built configs instead.

    Parameters:
        auth_service: Required unless ``include_auth=False``. The
            service passed to :class:`AuthMiddleware`.
        include_auth: Include the auth middleware. Default ``True``.
        include_rate_limit: Include the rate-limit middleware.
            Default ``True``.
        include_cors: Include the CORS middleware. Default ``True``.
            When included without an explicit ``cors_config``, the
            CORS config is chosen by ``cors_config`` fallback: if not
            provided, a ``disabled_cors()`` config is used — deployers
            **should** pass their real origin here.
        include_logging: Include the logging middleware. Default ``True``.
        cors_config: Explicit :class:`CORSConfig`. When ``None`` and
            ``include_cors=True``, ``disabled_cors()`` is used (safe
            default; logs at warning if actually used to serve traffic).
        slow_threshold_ms: Passed to :class:`LoggingConfig`.
        trust_forwarded_for: Passed to :class:`LoggingConfig`'s...
            actually it belongs to :class:`RateLimitConfig`. Applied to
            the rate-limit config when included.
        trusted_proxies: Same — applied to the rate-limit config.

    Raises:
        ValueError: If ``include_auth=True`` and ``auth_service`` is
            ``None``.
    """
    if include_auth and auth_service is None:
        raise ValueError(
            "default_middleware_stack: 'include_auth=True' requires an "
            "'auth_service'. Pass the AuthService instance, or call with "
            "'include_auth=False'."
        )

    logging_config = None
    if include_logging:
        logging_config = __getattr__("LoggingConfig")(
            slow_threshold_ms=slow_threshold_ms,
        )

    cors_config_effective = None
    if include_cors:
        cors_config_effective = (
            cors_config
            if cors_config is not None
            else __getattr__("disabled_cors")()
        )

    auth_config = None
    if include_auth:
        auth_config = __getattr__("AuthConfig")()

    rate_limit_config = None
    if include_rate_limit:
        rate_limit_config = __getattr__("RateLimitConfig")(
            rules=__getattr__("default_rules")(),
            trust_forwarded_for=trust_forwarded_for,
            trusted_proxies=trusted_proxies,
        )

    return build_middleware_stack(
        logging=logging_config,
        cors=cors_config_effective,
        auth=auth_config,
        auth_service=auth_service if include_auth else None,
        rate_limit=rate_limit_config,
    )


def _resolve_class(key: str) -> type:
    """Return the middleware class for the given key, importing lazily."""
    submodule = _MODULE_FOR_KEY[key]
    class_name = _CLASS_FOR_KEY[key]
    module = importlib.import_module(f".{submodule}", __name__)
    return getattr(module, class_name)


# ===========================================================================
# Introspection
# ===========================================================================

def list_middlewares() -> list[str]:
    """Return the canonical names of the available middlewares, sorted.

    For mount order, use :data:`MIDDLEWARE_ORDER`.
    """
    return sorted(MIDDLEWARE_ORDER)


def get_middleware(name: str) -> Any:
    """Return the middleware class registered under ``name``.

    ``name`` may be the canonical key (``"logging"``, ``"cors"``,
    ``"auth"``, ``"rate_limit"``) or the class name (``"LoggingMiddleware"``).

    Raises:
        KeyError: If ``name`` is not a known middleware.
    """
    # Class name → canonical key?
    reverse = {cls: key for key, cls in _CLASS_FOR_KEY.items()}
    canonical = reverse.get(name, name)

    if canonical not in _CLASS_FOR_KEY:
        available = ", ".join(sorted(_CLASS_FOR_KEY))
        raise KeyError(
            f"Unknown middleware {name!r}. Available: {available}"
        )
    return _resolve_class(canonical)
