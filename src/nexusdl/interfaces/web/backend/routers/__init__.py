"""Routers package for the NexusDL web backend.

Exposes the ten :class:`fastapi.APIRouter` instances that make up the HTTP
and WebSocket API, plus a :func:`mount_all` helper for the composition
root.

Loading strategy
================

All exports are **lazy** via :pep:`562` (``__getattr__``). Importing this
package never imports :mod:`fastapi`, :mod:`pydantic`, or any router
module. Symbols are resolved on first attribute access and cached in
``globals()`` — one-time cost, then indistinguishable from a top-level
import.

Why lazy
--------

* ``routers/__init__.py`` may be imported by tests that only want to
  check discovery (``list_routers()``, ``mount_all`` with ``only=``) or
  by plugin code that mounts a subset. Paying for ten FastAPI module
  imports and every transitive Pydantic schema is a waste.
* Tests can inspect the router registry without ever booting FastAPI.
* Deployments that only expose a subset (health-only, WS-disabled) can
  skip importing the modules they will not mount.

Naming convention
=================

Every router module exports an :class:`APIRouter` under the name
``router`` (FastAPI convention). At the package level, they are exposed
as ``<name>_router`` to avoid a ten-way collision:

* ``auth_router`` → ``routers.auth.router``
* ``sites_router`` → ``routers.sites.router``
* …

:func:`list_routers` returns the **canonical** names (without the
``_router`` suffix) because that is what a developer wants when
enumerating the API surface. :func:`get_router` accepts both forms.

Mounting
========

The :func:`mount_all` helper is the intended composition-root entry
point::

    from fastapi import FastAPI
    from nexusdl.interfaces.web.backend.routers import mount_all

    app = FastAPI(lifespan=lifespan)
    mounted = mount_all(app)

    # Subset (e.g. in tests):
    mount_all(app, only={"health", "auth"})

    # Deployments that disable WebSocket:
    mount_all(app, skip={"ws"})

``mount_all`` returns the list of canonical names actually mounted, in
the order they were mounted. That makes it trivially testable:

    assert "auth" in mount_all(fake_app)

Mount order
===========

:data:`ROUTER_ORDER` is deterministic — it defines both the mount order
in :func:`mount_all` and the ordering used by :func:`list_routers`.
``health`` first (so the readiness probe is always registered even when
other imports fail loudly in development) and ``ws`` last (so the
WebSocket upgrade does not race with the ``/api/v1/*`` REST paths).

**Do not reorder** without auditing route collisions. FastAPI matches
in registration order; a router with a `/{id}` dynamic path placed
before a router with a fixed path will shadow it.
"""
from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

# --- Package version (soft read from nexusdl.version) -----------------------

try:
    from nexusdl.version import __version__ as __version__
except Exception:  # pragma: no cover - defensive fallback
    __version__ = "3.0.0"


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # Router instances (lazy)
    "auth_router",
    "sites_router",
    "settings_router",
    "search_router",
    "manga_router",
    "library_router",
    "download_router",
    "chapters_router",
    "health_router",
    "ws_router",
    # Introspection
    "list_routers",
    "get_router",
    "mount_all",
    "ROUTER_ORDER",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps the exported attribute name -> submodule basename.
#
# Every router module exports an ``APIRouter`` under the name ``router``
# (FastAPI convention). To avoid ten collisions at the package level, the
# exported attribute is ``<name>_router`` and is populated from
# ``<submodule>.router``.
#
# Insertion order = mount order in ``mount_all``. Deterministic so
# openapi.json output and route introspection are reproducible across
# runs.

_LAZY_ROUTERS: dict[str, str] = {
    "health_router": "health",
    "auth_router": "auth",
    "sites_router": "sites",
    "settings_router": "settings",
    "search_router": "search",
    "manga_router": "manga",
    "chapters_router": "chapters",
    "library_router": "library",
    "download_router": "download",
    "ws_router": "ws",
}

#: Canonical (suffix-free) names, in mount order.
#: ``("health", "auth", "sites", ...)``.
ROUTER_ORDER: tuple[str, ...] = tuple(
    name.removesuffix("_router") for name in _LAZY_ROUTERS
)


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs.

if TYPE_CHECKING:
    from fastapi import APIRouter

    from .auth import router as auth_router
    from .chapters import router as chapters_router
    from .download import router as download_router
    from .health import router as health_router
    from .library import router as library_router
    from .manga import router as manga_router
    from .search import router as search_router
    from .settings import router as settings_router
    from .sites import router as sites_router
    from .ws import router as ws_router


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public router instance on first access (:pep:`562`).

    Raises:
        AttributeError: If ``name`` is not a known router attribute.
        ImportError: If the router's module cannot be imported because an
            optional dependency is missing (e.g. ``python-jose`` for
            ``auth``). The message points at the ``[web]`` extra.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    submodule = _LAZY_ROUTERS.get(name)
    if submodule is None:
        available = ", ".join(sorted(_LAZY_ROUTERS))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available routers: {available}"
        )

    try:
        module = importlib.import_module(f".{submodule}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name in {"fastapi", "pydantic", "python_jose", "jose"}:
            raise ImportError(
                f"{name!r} requires the Web extra. "
                f"Install it with: pip install 'nexusdl[web]'"
            ) from exc
        raise

    value = getattr(module, "router")

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behaviour as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the full public API to ``dir()``, IDEs and tab-completion.

    Merges the module's actual namespace with ``__all__`` so that
    lazy-loaded routers appear even before their first access
    (``dir()`` does **not** trigger ``__getattr__`` — PEP 562 — so
    listing them here is the only way to make them discoverable
    without eagerly importing them).
    """
    return sorted(set(globals()) | set(__all__))


# ===========================================================================
# Introspection
# ===========================================================================

def list_routers() -> list[str]:
    """Return the sorted list of canonical router names.

    Canonical names are suffix-free (``"auth"``, not ``"auth_router"``)
    because that is what a developer wants when enumerating the API
    surface. The sort is alphabetical for user-facing output; use
    :data:`ROUTER_ORDER` for the mount order.
    """
    return sorted(ROUTER_ORDER)


def get_router(name: str) -> Any:
    """Return the :class:`APIRouter` registered under ``name``.

    Accepts both the canonical form (``"auth"``) and the suffixed form
    (``"auth_router"``) — autocompletion tends to suggest the latter.

    The router is loaded through :func:`__getattr__` so that missing
    optional dependencies produce the same ``ImportError`` with the
    ``pip install 'nexusdl[web]'`` hint — not a raw
    ``ModuleNotFoundError``.

    Raises:
        KeyError: If ``name`` is not a known router.
        ImportError: If the router's module cannot be imported because
            an optional dependency is missing.
    """
    canonical = name.removesuffix("_router")
    if canonical not in ROUTER_ORDER:
        available = ", ".join(ROUTER_ORDER)
        raise KeyError(
            f"Unknown router {name!r}. Available routers: {available}"
        )
    attribute_name = f"{canonical}_router"
    # Route through ``__getattr__`` (via module-level ``getattr``) so
    # that the ``ModuleNotFoundError`` → ``ImportError`` mapping is
    # applied consistently and the router is cached in ``globals()``
    # exactly like a normal attribute access would do.
    return __getattr__(attribute_name)


# ===========================================================================
# Mounting helper
# ===========================================================================

class _AppLike(Protocol):
    """Structural shape of :class:`fastapi.FastAPI` needed for mounting.

    Declared here (instead of importing FastAPI at module load) to keep
    this module headless-safe. Any object with an ``include_router``
    method satisfies the protocol — including the real FastAPI app, a
    test double, and alternate ASGI frameworks that follow the same
    convention.
    """

    def include_router(self, router: Any, **_kwargs: Any) -> Any: ...


def mount_all(
    app: _AppLike,
    *,
    only: Iterable[str] | None = None,
    skip: Iterable[str] | None = None,
    skip_errors: bool = False,
) -> list[str]:
    """Mount every router (or a subset) onto ``app``.

    Parameters:
        app: The FastAPI application (or any object exposing
            ``include_router``).
        only: If provided, mount **only** these canonical names. Cannot
            be combined with ``skip``.
        skip: If provided, mount every router **except** these canonical
            names. Cannot be combined with ``only``.
        skip_errors: When ``True``, a router whose **module fails to
            import** (missing optional dependency) is logged and
            skipped. Errors raised by :meth:`include_router` itself
            (route collisions, invalid APIRouter shapes, dependency
            misconfigurations) are **always** raised, regardless of this
            flag — they are composition-root bugs that must not be
            silently swallowed by a "best-effort" import fallback.

    Returns:
        The list of canonical names actually mounted, in mount order.

    Raises:
        ValueError: If both ``only`` and ``skip`` are provided.
        KeyError: If ``only`` or ``skip`` contains an unknown name.
        ImportError: If a router module fails to import and
            ``skip_errors`` is ``False``.
        Exception: Any exception from ``include_router`` itself —
            see the ``skip_errors`` note above.
    """
    if only is not None and skip is not None:
        raise ValueError("mount_all: 'only' and 'skip' are mutually exclusive")

    selected = _select_routers(only=only, skip=skip)
    mounted: list[str] = []

    for canonical in selected:
        attribute_name = f"{canonical}_router"
        try:
            router_instance = __getattr__(attribute_name)
        except ImportError:
            # Import failure = missing optional dep for THIS router's
            # module. The only class of error that ``skip_errors`` is
            # meant to swallow: a deployment that does not install
            # ``python-jose`` should still be able to run the health
            # router.
            if skip_errors:
                _log_skip(canonical)
                continue
            raise
        # ``include_router`` runs outside the try — a route collision,
        # a malformed APIRouter, or a dependency misconfiguration must
        # surface even when ``skip_errors=True``. Swallowing it would
        # produce an app that boots with a missing route and no
        # diagnostic.
        app.include_router(router_instance)
        mounted.append(canonical)

    return mounted


def _select_routers(
    *,
    only: Iterable[str] | None,
    skip: Iterable[str] | None,
) -> list[str]:
    """Return the canonical names to mount, in :data:`ROUTER_ORDER` order.

    Normalizes ``only`` / ``skip`` entries by stripping a trailing
    ``_router`` suffix so both ``{"auth"}`` and ``{"auth_router"}`` work.
    Rejects unknown names with the same clear error as :func:`get_router`.
    """
    if only is not None:
        requested = {name.removesuffix("_router") for name in only}
        unknown = requested - set(ROUTER_ORDER)
        if unknown:
            available = ", ".join(ROUTER_ORDER)
            raise KeyError(
                f"mount_all(only=...): unknown router(s) {sorted(unknown)!r}. "
                f"Available routers: {available}"
            )
        # Preserve mount order.
        return [name for name in ROUTER_ORDER if name in requested]

    if skip is not None:
        excluded = {name.removesuffix("_router") for name in skip}
        unknown = excluded - set(ROUTER_ORDER)
        if unknown:
            available = ", ".join(ROUTER_ORDER)
            raise KeyError(
                f"mount_all(skip=...): unknown router(s) {sorted(unknown)!r}. "
                f"Available routers: {available}"
            )
        return [name for name in ROUTER_ORDER if name not in excluded]

    return list(ROUTER_ORDER)


def _log_skip(canonical: str) -> None:
    """Log a skipped router at warning without importing ``loguru`` eagerly.

    If ``loguru`` is not available (should not happen in the web extra,
    but a stripped-down deployment might omit it), fall back to
    ``sys.stderr`` so the skip is **never** silent. A silent skip is a
    debugging trap: the operator wonders why a route 404s with no trace
    anywhere in the logs.
    """
    message = (
        f"mount_all: skipped router {canonical!r} "
        f"(its module could not be imported; skip_errors=True)"
    )
    try:
        from loguru import logger as _logger  # noqa: PLC0415
        _logger.bind(component="web.routers").warning(message)
        return
    except Exception:
        pass
    # Fallback — never silently drop the diagnostic.
    try:
        sys.stderr.write(message + "\n")
    except Exception:
        # If even stderr is broken, there is nothing else we can do.
        # The caller already chose ``skip_errors=True``, so swallowing
        # here matches their intent — but only as a last resort.
        pass
