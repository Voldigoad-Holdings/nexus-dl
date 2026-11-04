"""NexusDL web interface package.

This package groups the two halves of NexusDL's web interface, which are
built, tested and deployed independently:

* **Backend** — :mod:`nexusdl.interfaces.web.backend`, a FastAPI application
  whose composition root lives in ``backend/main.py``.  It exposes the REST
  API under ``/api/v1/``, the WebSocket event stream under ``/ws/v1/events``,
  and the operational endpoints under ``/health``, ``/health/ready`` and
  ``/api/version``.
* **Frontend** — :mod:`nexusdl.interfaces.web.frontend`, a Next.js 14 App
  Router application written in TypeScript and built with pnpm.  It is
  shipped as static output from a separate build pipeline and is **not** a
  Python package — there is no ``__init__.py`` inside it, and no Python
  code path ever imports it.

This file, therefore, exists to do three things:

1. **Mark the package.**  ``nexusdl.interfaces.web`` is a Python package, so
   it needs an ``__init__.py`` even though there is very little to put in
   it.
2. **Expose the ASGI application entry point.**  So that the composition
   root of any consumer — Uvicorn, Gunicorn, ASGI lifespan tests, an
   in-process test client, a deployment script — can write
   ``nexusdl.interfaces.web:app`` instead of reaching into the backend's
   internal structure at ``nexusdl.interfaces.web.backend.main:app``.
3. **Stay lazy.**  Importing ``nexusdl.interfaces.web`` must **not** trigger
   a full backend bootstrap — no reading ``data/default_config.yaml``, no
   creating an ``httpx.AsyncClient``, no registering sixty parsers, no
   connecting to the SQLite library, no initialising Playwright.  All of
   that belongs to the moment the app is actually run, and any consumer that
   merely wants to check "is the web interface installed?" must not pay for
   it.

The lazy-loading contract
-------------------------
Both ``app`` and ``create_app`` are resolved through PEP 562's
``__getattr__`` mechanism.  That means:

.. code-block:: python

    from nexusdl.interfaces.web import app  # triggers backend boot on first use
    from nexusdl.interfaces.web import create_app  # same

    import nexusdl.interfaces.web as web
    web.app  # triggers backend boot on first attribute access

    hasattr(web, "app")  # triggers backend boot — see below
    hasattr(web, "create_app")  # triggers backend boot — see below

Attribute access is the trigger, not module import.  This is what makes it
safe for a caller to do::

    from nexusdl.interfaces import web as _web_module  # no bootstrap
    if hasattr(_web_module, "app"):
        ...  # bootstrap on the ``hasattr`` call, once, at the point of use

without committing to running a web server.

The backend is expected to expose both an ``app`` instance and a
``create_app`` factory:

* ``app`` — the module-level ASGI application.  This is what ``uvicorn`` and
  ``gunicorn`` are pointed at when the app is run as a single process:
  ``uvicorn nexusdl.interfaces.web:app``.
* ``create_app`` — the factory function.  Preferred for testing (each test
  gets a fresh app with its own lifespan, its own dependency overrides, and
  its own ``TestClient``) and for multi-process deployments where each
  worker needs its own instance:
  ``gunicorn nexusdl.interfaces.web:create_app --worker-class uvicorn.workers.UvicornWorker``.

Both names are declared in :data:`__all__`, so they appear in
``from nexusdl.interfaces.web import *``, and they are annotated for
``mypy --strict`` via the ``TYPE_CHECKING`` block below.  If the backend
does not in fact export one of them, accessing it raises
:class:`AttributeError` with a message that names the missing name and the
module that should provide it — a clean, actionable failure, not an opaque
``ImportError``.

How the backend is expected to be invoked
-----------------------------------------
The canonical deployments use the composed form:

.. code-block:: shell

    # Development (reload enabled, factory not needed)
    uvicorn nexusdl.interfaces.web:app --reload

    # Production (single worker, preload disabled)
    uvicorn nexusdl.interfaces.web:app --host 0.0.0.0 --port 8000 --workers 4

    # Production with a process manager
    gunicorn nexusdl.interfaces.web:create_app \\
        --worker-class uvicorn.workers.UvicornWorker \\
        --workers 4

The ``docker/docker-compose.yml`` and ``docker/Dockerfile.web`` files
reference one of these forms; the ``nexusdl.dl`` launcher script uses the
``create_app`` factory so that it can build an app with a custom lifespan
for the local single-user flow.  None of those choices affect this file —
both names resolve correctly regardless of which one is used.

Why the frontend is not exported
--------------------------------
The Next.js frontend has no Python-side entry point.  It is built to a
static bundle (``out/`` or ``.next/standalone/``, depending on whether
``output: "standalone"`` is enabled in ``next.config.js``) and served by
one of:

* the FastAPI app itself, when the operator configures
  ``StaticFiles`` mounting in the backend's composition root,
* Caddy / nginx, when the operator runs the frontend and the backend as two
  separate processes behind a reverse proxy (the default production
  layout),
* the Node.js runtime of the Next.js standalone build, when the operator
  uses the multi-process Docker Compose profile.

In every case the frontend is a **deployment concern**, not a Python
import — nothing is exposed here because nothing needs to be.

What this module deliberately does **not** do
---------------------------------------------
* **No eager imports.**  As explained above, importing this module is a
  no-op side-effect-wise.
* **No version re-export.**  The project version lives in
  :mod:`nexusdl.version` (``__version__``).  Exposing it here as well would
  create a second import path for the same symbol, which is exactly the
  ambiguity the project's import discipline forbids.
* **No re-export of backend submodules.**  Consumers that need something
  from the backend — a dependency override, a WebSocket manager, a router —
  import it from the backend directly.  This file's job is to be the
  package boundary, not a convenience layer for the backend.
* **No configuration.**  No env-var reads, no YAML loading, no
  ``Settings`` construction.  Those belong to the composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    # Declared under ``TYPE_CHECKING`` so that ``mypy --strict`` sees the
    # types produced by the lazy ``__getattr__`` below.  These declarations
    # are **not** executed at runtime — the runtime implementation is
    # ``__getattr__``, which imports the backend on first access.
    app: FastAPI

    def create_app() -> FastAPI: ...


__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    """Lazily resolve ``app`` and ``create_app`` from the backend package.

    Called by the import machinery whenever an attribute is accessed on this
    module that is not found in the module's ``__dict__`` (PEP 562).  The
    only names we handle are the two declared in :data:`__all__`; anything
    else raises :class:`AttributeError` so that ``hasattr`` and
    introspection behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The FastAPI application instance (for ``"app"``) or the
        ``create_app`` factory function (for ``"create_app"``), exactly as
        exposed by :mod:`nexusdl.interfaces.web.backend.main`.

    Raises:
        AttributeError: When *name* is not one of the two public exports,
            when the backend module cannot be imported (a broken install or
            a mid-migration state), or when the backend module does not
            expose the requested name.  ``AttributeError`` — rather than
            ``ImportError`` — is required by PEP 562 so that ``hasattr`` and
            :func:`getattr` with a default argument work correctly.
    """
    if name not in __all__:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available attributes: {', '.join(__all__)}."
        )

    # Import the backend lazily.  Once imported, ``sys.modules`` caches it,
    # so the "from nexusdl.interfaces.web.backend import main" statement
    # below is a fast dict lookup on every subsequent access.
    try:
        from nexusdl.interfaces.web.backend import main
    except ImportError as exc:
        # The backend package exists as a directory (the tree guarantees
        # it), but importing it failed — typically a missing dependency, a
        # syntax error in a backend module, or a broken installation.  We
        # convert this into ``AttributeError`` so that ``hasattr`` still
        # works, and we chain the original ``ImportError`` as ``__cause__``
        # so the operator sees the real reason in a traceback.
        raise AttributeError(
            f"Cannot access {name!r} on "
            f"{__name__!r}: the backend module "
            f"'nexusdl.interfaces.web.backend.main' is not importable. "
            f"Underlying error: {exc}"
        ) from exc

    try:
        return getattr(main, name)
    except AttributeError:
        raise AttributeError(
            f"The NexusDL web backend module "
            f"'nexusdl.interfaces.web.backend.main' does not export "
            f"{name!r}.  Expected a FastAPI application named ``app`` "
            f"and/or a factory function named ``create_app`` in that "
            f"module."
        ) from None


def __dir__() -> list[str]:
    """Return the public names for interactive introspection.

    Python's default :func:`dir` implementation collects a module's
    ``__dict__`` and, when the module defines ``__getattr__``, calls it with
    each name returned here to decide what to display.  Without this
    override, ``dir(nexusdl.interfaces.web)`` would not list ``app`` or
    ``create_app`` — they are never present in ``__dict__`` — which would
    make the module appear empty in an interactive session.

    The returned list is the union of :data:`__all__` and the standard
    module dunders a user typically expects to see.
    """
    return sorted(set(__all__) | {"__all__", "__doc__", "__name__", "__package__"})
