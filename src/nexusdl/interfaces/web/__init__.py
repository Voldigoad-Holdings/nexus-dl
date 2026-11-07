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

This file exists to do exactly three things:

1. **Mark the package.**  ``nexusdl.interfaces.web`` is a Python package, so
   it needs an ``__init__.py`` even though there is very little to put in
   it.
2. **Expose the two ASGI entry points** that the backend actually provides:
   :func:`create_app` (dependency-injected factory, for tests and custom
   composition) and :func:`create_app_from_env` (zero-argument factory, for
   ``uvicorn --factory`` and Gunicorn).  Both names are what the deployment
   tooling references, so they are the two names this package re-exports.
3. **Stay lazy.**  Importing ``nexusdl.interfaces.web`` must **not** trigger
   a full backend bootstrap — no reading ``data/default_config.yaml``, no
   creating an ``httpx.AsyncClient``, no registering sixty parsers, no
   connecting to the SQLite library, no initialising Playwright.  All of
   that belongs to the moment the app is actually run, and any consumer that
   merely wants to check "is the web interface installed?" must not pay for
   it.

Why ``app`` is **not** exposed
------------------------------
The backend's ``main.py`` deliberately does not construct a module-level
FastAPI instance.  Doing so would share a single ``AppServices`` container
— a single :class:`~.websocket.ConnectionManager`, a single
:class:`~.session.http_session.HttpSession`, a single rate-limit store —
across every forked worker in a multi-worker deployment.  That is exactly
the bug ``main.py``'s own docstring warns against, and it is why the
project's prescribed deployment pattern uses ``--factory``.

The consequence for callers is a single, explicit flag in the ``uvicorn``
command line:

.. code-block:: shell

    # Production (multi-worker — the prescribed pattern):
    uvicorn nexusdl.interfaces.web:create_app_from_env \\
        --factory --workers 4 --host 0.0.0.0 --port 8000

    # Production with gunicorn:
    gunicorn nexusdl.interfaces.web:create_app_from_env \\
        --worker-class uvicorn.workers.UvicornWorker \\
        --workers 4

    # Development (single process, reload):
    uvicorn nexusdl.interfaces.web:create_app_from_env \\
        --factory --reload

    # In-process test (each test gets its own app + services):
    from nexusdl.interfaces.web import create_app
    app = create_app(FakeServices())

The ``--factory`` flag tells uvicorn to **call** the callable instead of
treating it as an ASGI app instance.  This is uvicorn's standard convention
— not a NexusDL-specific quirk — and it is what makes each worker build
its own services container.

The lazy-loading contract
-------------------------
Both names are resolved through PEP 562's ``__getattr__`` mechanism.  That
means:

.. code-block:: python

    from nexusdl.interfaces.web import create_app         # ← import ok, no boot
    from nexusdl.interfaces.web import create_app_from_env  # ← import ok, no boot

    import nexusdl.interfaces.web as web
    web.create_app          # ← backend imported on first access
    web.create_app_from_env # ← backend imported on first access

    hasattr(web, "create_app")          # ← False on broken install, no crash
    hasattr(web, "create_app_from_env") # ← same

Attribute access is the trigger, not module import.  This is what makes it
safe for a caller to do::

    from nexusdl.interfaces import web as _web_module   # no bootstrap
    if hasattr(_web_module, "create_app_from_env"):
        ...  # bootstrap on the ``hasattr`` call, once, at the point of use

without committing to running a web server.

How the backend is expected to be invoked
-----------------------------------------
Every deployment form documented in ``docker/`` and ``nexusdl.dl`` calls
one of the two exposed names:

.. code-block:: shell

    # Development (reload enabled)
    uvicorn nexusdl.interfaces.web:create_app_from_env --factory --reload

    # Production (single worker)
    uvicorn nexusdl.interfaces.web:create_app_from_env \\
        --factory --host 0.0.0.0 --port 8000

    # Production (multi-worker via uvicorn)
    uvicorn nexusdl.interfaces.web:create_app_from_env \\
        --factory --host 0.0.0.0 --port 8000 --workers 4

    # Production (multi-worker via gunicorn)
    gunicorn nexusdl.interfaces.web:create_app_from_env \\
        --worker-class uvicorn.workers.UvicornWorker \\
        --workers 4

None of those choices affect this file — both exposed names resolve
correctly regardless of which command is used.

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
* **No module-level ``app``.**  See the section above — it would silently
  share state across forked workers.

Known limitations
-----------------
* If the backend cannot be imported (missing optional dependency, syntax
  error, broken install), the first attribute access on ``create_app`` or
  ``create_app_from_env`` raises :class:`AttributeError` with the
  underlying :class:`ImportError` chained as ``__cause__``.  This keeps
  ``hasattr`` and ``getattr(obj, name, default)`` working as Python users
  expect, while still surfacing the real error in a traceback.  A CI smoke
  test that does ``from nexusdl.interfaces.web import create_app_from_env``
  will therefore fail with a clear message, not a mysterious ``None``.
* The signatures declared under :data:`typing.TYPE_CHECKING` are currently
  ``Any``-based.  They exist so that ``mypy --strict`` sees *something*
  rather than a bare ``__getattr__`` — a stricter declaration would require
  importing the concrete types from
  ``nexusdl.interfaces.web.backend.main``, which would create a cycle
  (``main`` imports services which import routers which import the web
  package).  When those types become importable without a cycle, replace
  the ``Any`` signatures with the concrete types.  Until then,
  ``mypy --strict`` reports no error, and the ``__getattr__`` runtime
  resolution is the single source of truth for what the attributes actually
  are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    # Declared under ``TYPE_CHECKING`` so that ``mypy --strict`` sees the
    # types produced by the lazy ``__getattr__`` below.  These declarations
    # are **not** executed at runtime — the runtime implementation is
    # ``__getattr__``, which imports the backend on first access.
    #
    # The ``Any`` types are a known limitation: the concrete types
    # (``AppServices``, ``CORSConfig``, ``AuthConfig``, …) live in the
    # backend package and importing them here would create a cycle
    # (main → services → routers → web).  When the cycle is broken (e.g.
    # by moving the config dataclasses into a leaf module), replace these
    # ``Any`` annotations with the real types.
    def create_app(
        services: Any,
        *,
        cors_config: Any = None,
        auth_config: Any = None,
        rate_limit_config: Any = None,
        logging_config: Any = None,
        ws_config: Any = None,
        static_dir: Any = None,
        mount_static: bool = True,
        routers_only: set[str] | None = None,
        routers_skip: set[str] | None = None,
        expose_openapi: bool = True,
    ) -> FastAPI: ...

    def create_app_from_env() -> FastAPI: ...


__all__ = ["create_app", "create_app_from_env"]


def __getattr__(name: str) -> Any:
    """Lazily resolve ``create_app`` and ``create_app_from_env``.

    Called by the import machinery whenever an attribute is accessed on
    this module that is not found in the module's ``__dict__`` (PEP 562).
    The only names we handle are the two declared in :data:`__all__`;
    anything else raises :class:`AttributeError` so that ``hasattr`` and
    introspection behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The ``create_app`` factory or the ``create_app_from_env``
        zero-argument factory, exactly as exposed by
        :mod:`nexusdl.interfaces.web.backend.main`.

    Raises:
        AttributeError: When ``name`` is not one of the two public exports,
            when the backend module cannot be imported (a broken install or
            a mid-migration state), or when the backend module does not
            expose the requested name.  ``AttributeError`` — rather than
            ``ImportError`` — is required by PEP 562 so that ``hasattr`` and
            :func:`getattr` with a default argument work correctly.
    """
    # Dunders are interrogated by the import machinery, by ``pickle``, by
    # ``copy``, by ``inspect`` and by every framework that touches a
    # module (Pytest, IPython, mypy's runtime helpers, …).  When one of
    # those probes asks for ``__wrapped__``, ``__path__``, ``__file__``
    # or any other standard dunder, the answer must be the plain Python
    # ``AttributeError`` — a long explanatory message about
    # ``create_app_from_env`` is not just unhelpful, it can break
    # tooling that pattern-matches on ``str(exc)``.  The custom message
    # is reserved for *real* attribute lookups that miss.
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    if name not in __all__:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available attributes: {', '.join(__all__)}. "
            f"Note: this package does not expose a module-level 'app' "
            f"instance — use 'create_app_from_env' with '--factory', "
            f"e.g. 'uvicorn nexusdl.interfaces.web:create_app_from_env "
            f"--factory'. See the package docstring for details."
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
        value = getattr(main, name)
    except AttributeError:
        raise AttributeError(
            f"The NexusDL web backend module "
            f"'nexusdl.interfaces.web.backend.main' does not export "
            f"{name!r}.  This is a composition-root bug — the backend "
            f"must always export both 'create_app' and "
            f"'create_app_from_env'."
        ) from None

    # Cache the resolved value in the module ``__dict__`` so that
    # subsequent accesses skip both the ``sys.modules`` lookup and the
    # ``getattr`` on ``main``.  This is the standard PEP 562 optimisation
    # recommended by the PEP's own "Example" section: after the first
    # access, Python finds the attribute in ``__dict__`` and never calls
    # ``__getattr__`` again.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the public names for interactive introspection.

    Python's default :func:`dir` implementation collects a module's
    ``__dict__`` and, when the module defines ``__getattr__``, calls it with
    each name returned here to decide what to display.  Without this
    override, ``dir(nexusdl.interfaces.web)`` would not list ``create_app``
    or ``create_app_from_env`` — they are never present in ``__dict__``
    until the first access — which would make the module appear empty in an
    interactive session.

    The returned list is the union of :data:`__all__` and the standard
    module dunders a user typically expects to see.
    """
    return sorted(
        set(__all__) | {"__all__", "__doc__", "__name__", "__package__"}
    )
