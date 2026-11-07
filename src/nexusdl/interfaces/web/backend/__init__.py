"""NexusDL web backend package.

This is the **FastAPI layer** — the one place in the codebase where the
framework is expected. Everything under :mod:`nexusdl.core`,
:mod:`nexusdl.parsers`, and :mod:`nexusdl.interfaces.gui` is
framework-agnostic on purpose; only this package (and its sibling
``gui/``) may import FastAPI, Pydantic, or Starlette.

Public surface
==============

The top-level exports below cover what a hosting application, a test
suite, or a client SDK actually needs. **Everything else lives in a
submodule** and should be imported from there directly — the schemas are
a good example:

    # Correct — narrow import, small dependency surface:
    from nexusdl.interfaces.web.backend.schemas.responses import MangaDetailSchema

    # Not provided by this package (do not add):
    from nexusdl.interfaces.web.backend import MangaDetailSchema  # ← nope

    # The top-level surface is deliberately focused on:
    #   • entrypoints     → create_app, create_app_from_env
    #   • service contract → AppServices, ServicesDep, {Current,Optional,Admin}User
    #   • auth primitives → AuthService, TokenPayload, PasswordPolicy, …
    #   • WS engine       → ConnectionManager, BusBridge, WSCloseCode, …

Internal layout
===============

::

    backend/
    ├── main.py            ← composition root (create_app, lifespan, CLI)
    ├── dependencies.py    ← FastAPI Depends() contract (AppServices, CurrentUser)
    ├── security.py        ← JWT + password hashing + revocation (headless-safe)
    ├── websocket.py       ← ASGI WS engine + EventBus bridge (headless-safe)
    ├── schemas/           ← Pydantic v2 (requests, responses, common, ws)
    ├── routers/           ← APIRouter per resource (auth, sites, …)
    └── middleware/        ← ASGI middleware (logging, cors, rate_limit, auth)

Layering rules:

* ``main.py`` depends on everything — it is the composition root.
* ``dependencies.py`` depends on ``security.py`` and ``schemas/responses.py``.
* ``routers/`` depends on ``dependencies.py``, ``schemas/``, ``security.py``.
* ``middleware/`` and ``websocket.py`` are **self-contained** — no
  dependency on FastAPI. That is why they are safe to import in
  headless test runs and why they could be reused with any ASGI
  framework.

Entry points
============

**Programmatic construction** (tests, fixtures, alternate compositions):

    from nexusdl.interfaces.web.backend import create_app
    app = create_app(services, cors_config=..., expose_openapi=False)

**Zero-argument factory** (uvicorn ``--factory``, gunicorn, Docker):

    uvicorn nexusdl.interfaces.web.backend.main:create_app_from_env \\
        --factory --workers 4 --host 0.0.0.0 --port 8000

**Single-worker CLI** (development, staging):

    python -m nexusdl.interfaces.web.backend.main

Loading strategy
================

All exports are **lazy** via PEP 562 (``__getattr__``). Importing
``nexusdl.interfaces.web.backend`` never imports FastAPI, Pydantic,
python-jose, or any submodule. Symbols are resolved on first attribute
access and cached in ``globals()``.

The lazy loader intentionally catches ``ImportError`` — not just
``ModuleNotFoundError`` — so that a partially initialized submodule (a
missing symbol, a circular import resolved differently across Python
versions) also gets the ``[web]`` install hint instead of a raw
traceback with an ambiguous message.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# --- Constants (eager — no heavy imports) ----------------------------------

try:
    from nexusdl.version import __version__ as __version__
except Exception:  # pragma: no cover - defensive fallback
    __version__ = "3.0.0"

#: HTTP API contract version (independent from the package version).
#:
#: Duplicated here (rather than imported from
#: :mod:`~nexusdl.interfaces.web.backend.schemas.responses`) to keep this
#: package's import cost at zero when only the version is requested. The
#: value is kept in sync with ``schemas/responses.py::API_VERSION`` by
#: hand; if the API contract is ever bumped, both constants must change
#: together.
API_VERSION: str = "v1"


# --- Public API ------------------------------------------------------------

__all__ = [
    # --- Entry points ---
    "create_app",
    "create_app_from_env",
    # --- Service contract ---
    "AppServices",
    "ServicesDep",
    # --- Authentication types ---
    "CurrentUser",
    "OptionalUser",
    "AdminUser",
    "RequestIdDep",
    # --- Security primitives ---
    "AuthService",
    "AuthConfig",
    "PasswordPolicy",
    "TokenPayload",
    "TokenPair",
    "RevocationStore",
    "InMemoryRevocationStore",
    # --- WebSocket engine ---
    "ConnectionManager",
    "ConnectionManagerConfig",
    "BusBridge",
    "BusBridgeConfig",
    "default_bus_bridge_config",
    "AuthContext",
    "WSMessage",
    "WSErrorPayload",
    "WSCloseCode",
    "WSEventType",
    # --- Metadata ---
    "__version__",
    "API_VERSION",
    # --- Introspection ---
    "list_symbols",
    "get_symbol",
]


# --- Lazy import registry --------------------------------------------------
#
# Maps exported name -> submodule basename. Every listed symbol is defined
# at module level in the given submodule (no further indirection). Adding
# a new top-level export is one line here plus a matching TYPE_CHECKING
# re-export below.

_LAZY: dict[str, str] = {
    # --- main.py — entry points ---
    "create_app": "main",
    "create_app_from_env": "main",
    # --- dependencies.py — service contract + auth types ---
    "AppServices": "dependencies",
    "ServicesDep": "dependencies",
    "CurrentUser": "dependencies",
    "OptionalUser": "dependencies",
    "AdminUser": "dependencies",
    "RequestIdDep": "dependencies",
    # --- security.py — auth primitives ---
    "AuthService": "security",
    "AuthConfig": "security",
    "PasswordPolicy": "security",
    "TokenPayload": "security",
    "TokenPair": "security",
    "RevocationStore": "security",
    "InMemoryRevocationStore": "security",
    # --- websocket.py — WS engine ---
    "ConnectionManager": "websocket",
    "ConnectionManagerConfig": "websocket",
    "BusBridge": "websocket",
    "BusBridgeConfig": "websocket",
    "default_bus_bridge_config": "websocket",
    "AuthContext": "websocket",
    "WSMessage": "websocket",
    "WSErrorPayload": "websocket",
    "WSCloseCode": "websocket",
    "WSEventType": "websocket",
}


# --- Static re-exports for type checkers and IDEs --------------------------
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs.

if TYPE_CHECKING:
    from .dependencies import (
        AdminUser,
        AppServices,
        CurrentUser,
        OptionalUser,
        RequestIdDep,
        ServicesDep,
    )
    from .main import create_app, create_app_from_env
    from .security import (
        AuthConfig,
        AuthService,
        InMemoryRevocationStore,
        PasswordPolicy,
        RevocationStore,
        TokenPair,
        TokenPayload,
    )
    from .websocket import (
        AuthContext,
        BusBridge,
        BusBridgeConfig,
        ConnectionManager,
        ConnectionManagerConfig,
        WSCloseCode,
        WSErrorPayload,
        WSEventType,
        WSMessage,
        default_bus_bridge_config,
    )


# --- Lazy loader -----------------------------------------------------------

def __getattr__(name: str) -> Any:
    """Resolve a public symbol on first access (PEP 562).

    The catch is intentionally broadened from ``ModuleNotFoundError`` to
    ``ImportError`` — a partially-initialized submodule (missing symbol,
    circular import that resolves differently across Python versions)
    also deserves the ``[web]`` install hint rather than a raw
    traceback with an ambiguous message.

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

    submodule = _LAZY.get(name)
    if submodule is None:
        available = ", ".join(sorted(_LAZY))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available lazy symbols: {available}"
        )

    try:
        module = importlib.import_module(f".{submodule}", __name__)
    except ImportError as exc:
        # ``ImportError`` is broader than ``ModuleNotFoundError`` on
        # purpose — see the ``Loading strategy`` docstring section.
        if exc.name in {
            "fastapi",
            "pydantic",
            "python_jose",
            "jose",
            "starlette",
        }:
            raise ImportError(
                f"{name!r} requires the Web extra. "
                f"Install it with: pip install 'nexusdl[web]'"
            ) from exc
        raise

    value = getattr(module, name)

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the full public API to ``dir()``, IDEs and tab-completion.

    Uses a **set** union so that symbols already cached in ``globals()``
    by :func:`__getattr__` are not listed twice (a plain list
    concatenation would produce duplicates once the symbol has been
    accessed).
    """
    return sorted(set(globals()) | set(__all__))


# --- Introspection ---------------------------------------------------------

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy symbols.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and future admin tools.
    """
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (config-driven hosts, plugins, parametrized tests).

    The lookup goes through :func:`__getattr__` so the resolution is
    cached in ``globals()`` and subsequent calls do not pay the
    ``importlib`` overhead again — and so a missing optional dependency
    surfaces as an :class:`ImportError` with the ``[web]`` hint, not a
    raw ``ModuleNotFoundError``.

    Raises:
        KeyError: If ``name`` is not a registered symbol.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY))
        raise KeyError(
            f"Unknown symbol {name!r}. Available: {available}"
        )
    return __getattr__(name)
