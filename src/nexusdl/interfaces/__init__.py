"""Interface adapters — the primary side of the hexagonal architecture.

This package groups every **inbound** adapter of NexusDL — the code that
sits on the boundary between the outside world and the application's
domain core.  Each interface translates user input (keyboard, mouse, HTTP
request, WebSocket frame) into calls on the core's ports, and translates
the core's outbound events back into whatever format the interface
presents.

The four interfaces
-------------------
Per the project's architecture (PROMPT MASTER §"Architecture globale"), the
interface layer contains exactly four adapters:

* **CLI** — :mod:`nexusdl.interfaces.cli`.  A Typer-based command-line
  interface (``nexusdl search``, ``nexusdl download``, …).  Fast path for
  scripted and headless use.
* **TUI** — also under :mod:`nexusdl.interfaces.cli`, as a Textual
  application launched by ``nexusdl tui``.  Shares the CLI package because
  the two share argument parsing, configuration loading and the same
  terminal environment.
* **GUI** — :mod:`nexusdl.interfaces.gui`.  A CustomTkinter desktop
  application, launched by ``nexusdl gui``.  Cross-platform native
  experience for users who prefer a window over a shell.
* **Web** — :mod:`nexusdl.interfaces.web`.  A FastAPI backend serving a
  REST API and a WebSocket event stream, paired with a Next.js frontend.
  Launched by ``nexusdl web`` or by ``uvicorn``/``gunicorn`` directly via
  the factories exposed by the ``web`` package
  (:func:`~nexusdl.interfaces.web.create_app_from_env` for the zero-arg
  deployment factory, :func:`~nexusdl.interfaces.web.create_app` for
  injected composition in tests and alternate hosts).

Dependency rule (R4)
--------------------
Unlike the core and parsers layers — which are constrained by strict
import rules — the interfaces layer is allowed to depend on **everything**:
the core, the parsers, the plugins, the shared models and any third-party
library it needs to present the UI (Typer, Textual, CustomTkinter, FastAPI,
Uvicorn, …).  This is exactly what makes the layer a set of **composition
roots**: each interface is where concrete implementations are wired
together and handed to the core as abstract ports.

The composition roots are:

* CLI — ``nexusdl.interfaces.cli.cli_entry``
* TUI — ``nexusdl.interfaces.cli.app``
* GUI — ``nexusdl.interfaces.gui.app``
* Web — ``nexusdl.interfaces.web.backend.main``

No other module in the project is allowed to be a composition root.

Why nothing is imported eagerly
-------------------------------
Each interface pulls in a heavy dependency tree when it is imported:

* ``cli`` imports **Typer** and (for the TUI) **Textual**, plus Rich.
* ``gui`` imports **CustomTkinter**, which in turn imports **Tkinter** and
  requires a running display server on Linux.
* ``web`` imports **FastAPI**, **Uvicorn**, the entire backend bootstrap
  (Settings loading, session creation, parser registration, SQLite
  library, Playwright pool), and starts the ASGI application.

Eagerly importing them all in ``nexusdl.interfaces.__init__`` would mean
that:

* Every ``import nexusdl.interfaces`` pays for every interface, even when
  the caller only wants one of them — or none.
* On a headless server (Docker, CI, a Raspberry Pi), importing the
  ``gui`` package would try to load Tkinter and fail — because the
  ``gui`` extras were not installed — **even though the caller never asked
  for the GUI**.
* The CLI's startup time would balloon from tens of milliseconds to
  hundreds, purely from FastAPI and Textual being imported for no reason.

The fix is the same pattern used by :mod:`nexusdl.interfaces.web`: PEP 562
lazy attribute access.  Nothing is imported at module load; the subpackage
is imported the first time it is accessed through this package's namespace.

Lazy-loading contract
---------------------
Both of these forms are supported and behave identically:

.. code-block:: python

    # Attribute access — resolves through ``__getattr__`` below.
    from nexusdl.interfaces import web as web_interface
    web_interface.create_app_from_env  # imports ``nexusdl.interfaces.web`` here.

    # Direct submodule import — Python's own import machinery.
    from nexusdl.interfaces.web import create_app_from_env

The **trigger** is attribute access on the package namespace, not module
import.  As a direct consequence:

.. code-block:: python

    import nexusdl.interfaces  # does not import cli / gui / web
    hasattr(nexusdl.interfaces, "web")  # imports ``nexusdl.interfaces.web``
    # → True, and now ``nexusdl.interfaces.web`` is populated in sys.modules.

If a subpackage fails to import (missing optional dependency, broken
installation, syntax error, runtime initialisation error), the failure is
raised as :class:`AttributeError` — required by PEP 562 so that
``hasattr`` and ``getattr(x, name, default)`` behave correctly — with the
original exception chained as ``__cause__`` so the operator sees the real
reason in a traceback.

Which interface should a caller use?
------------------------------------
* ``from nexusdl.interfaces import cli`` — scripted, headless, terminal
  driven.
* ``from nexusdl.interfaces import gui`` — desktop, requires a display and
  the ``gui`` extras.
* ``from nexusdl.interfaces import web`` — HTTP / WebSocket driven, usable
  from a browser or another service.  The zero-argument ASGI factory is
  ``nexusdl.interfaces.web.create_app_from_env``, designed for
  ``uvicorn nexusdl.interfaces.web:create_app_from_env --factory``.  For
  injected composition (tests, embedded hosts), use
  ``nexusdl.interfaces.web.create_app(services)`` directly.

The ``nexusdl.__main__`` module dispatches between them based on the
sub-command the user runs (``nexusdl``, ``nexusdl tui``, ``nexusdl gui``,
``nexusdl web``) and chooses the appropriate composition root accordingly.
That dispatch is the *only* code in the project that is expected to import
more than one interface at a time, and it does so **lazily** — one branch,
one import, at the point of dispatch.

What this module deliberately does **not** do
---------------------------------------------
* **No eager imports.**  Covered above.
* **No re-export of interface symbols.**  A caller that needs
  ``Settings``, ``DownloadManager``, ``BaseParser`` or any other core
  symbol imports it from :mod:`nexusdl.core`.  The interface package does
  not act as a convenience layer for the core — that would be a second
  import path for every symbol the core already exports cleanly.
* **No version re-export.**  The project version lives in
  :mod:`nexusdl.version` (``__version__``).  One import path per symbol.
* **No configuration or shared state.**  Environment variables, YAML
  config, session objects — each interface builds its own.  The interfaces
  package is a name, not a runtime context.
* **No plugin / extension mechanism.**  Plugins attach to the **core**
  (see :mod:`nexusdl.plugins`), not to the interface layer.  Adding an
  interface does not require a plugin; adding a plugin does not require an
  interface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Declared under ``TYPE_CHECKING`` so that ``mypy --strict`` and IDEs
    # see the three subpackages as typed modules when resolving
    # ``from nexusdl.interfaces import web`` and similar forms.  These
    # declarations are **not** executed at runtime — the runtime
    # implementation is ``__getattr__`` below, which imports the
    # subpackage on first attribute access.
    from . import cli, gui, web


__all__ = ["cli", "gui", "web"]


def __getattr__(name: str) -> Any:
    """Lazily import and return one of the interface subpackages.

    Called by Python's import machinery whenever an attribute is accessed
    on this package that is not already bound in the module's ``__dict__``
    (PEP 562).  The only names we handle are the three declared in
    :data:`__all__`; anything else raises :class:`AttributeError` so that
    ``hasattr``, ``getattr`` with a default, and interactive introspection
    all behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The imported subpackage module (``nexusdl.interfaces.cli``,
        ``nexusdl.interfaces.gui`` or ``nexusdl.interfaces.web``), with the
        side effect of importing it into ``sys.modules`` and binding it as
        an attribute of this package for subsequent accesses.

    Raises:
        AttributeError: When *name* is not one of the three public
            subpackages, when the subpackage cannot be imported (a missing
            optional dependency such as CustomTkinter for ``gui``, a syntax
            error in the subpackage, a runtime initialisation error, or a
            broken installation), or when the import succeeds but does not
            bind a module of the expected name.  ``AttributeError`` —
            rather than the underlying exception — is required by PEP 562
            so that ``hasattr`` and ``getattr(x, name, default)`` behave
            correctly.  The original exception is chained as ``__cause__``
            so the real reason is visible in a traceback.
    """
    if name not in __all__:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available interfaces: {', '.join(__all__)}."
        )

    # Import lazily.  ``importlib`` is imported inside the function rather
    # than at module level so that even the cost of loading the import
    # machinery is deferred to the moment an interface is actually used.
    import importlib

    dotted_name = f"{__name__}.{name}"
    try:
        module = importlib.import_module(dotted_name)
    except Exception as exc:
        # Broad catch — not just ``ImportError`` — by design.
        #
        # PEP 562 requires ``__getattr__`` to raise ``AttributeError``
        # (and nothing else) for names it cannot provide, so that
        # ``hasattr`` and ``getattr(x, name, default)`` behave correctly.
        # A subpackage import can fail for reasons that are *not* an
        # ``ImportError``:
        #
        #   * a genuine ``SyntaxError`` in a subpackage module
        #   * a ``RuntimeError`` / ``ValueError`` raised by a module-level
        #     initialiser (e.g. a dependency version check, a "Chromium
        #     not installed" guard in the playwright bootstrap, an
        #     environment probe in the web backend)
        #   * any ``Exception`` from a broken installation (missing data
        #     file, corrupt package manifest, …)
        #
        # In all cases the correct behaviour is to surface the failure as
        # ``AttributeError`` (with the original exception chained as
        # ``__cause__``), never to let a raw ``SyntaxError`` /
        # ``RuntimeError`` escape — otherwise ``hasattr(interfaces, "gui")``
        # would raise instead of returning ``False``, defeating the whole
        # point of the lazy-loading contract.
        raise AttributeError(
            f"Cannot access interface {name!r}: the subpackage "
            f"{dotted_name!r} is not importable.  Underlying error: {exc}"
        ) from exc

    # ``importlib.import_module`` normally returns the module whose
    # canonical name is ``dotted_name``.  Defensive check: an import hook
    # (PEP 302 / PEP 451), a namespace-package edge case, or a stale
    # ``sys.modules`` entry could theoretically return a different object.
    # If that happens, treat it as a broken import rather than return a
    # module the caller cannot safely use.
    if getattr(module, "__name__", None) != dotted_name:
        raise AttributeError(
            f"Cannot access interface {name!r}: importing "
            f"{dotted_name!r} returned an object whose ``__name__`` is "
            f"{getattr(module, '__name__', None)!r} instead of "
            f"{dotted_name!r}."
        )

    # ``importlib.import_module`` normally binds the imported module as an
    # attribute of the parent package, but only if the import goes through
    # the standard machinery.  In the presence of namespace-package edge
    # cases or a partially initialised subpackage, that binding may be
    # missing or point elsewhere.  Bind it explicitly to make the
    # invariant hold: after this call, ``nexusdl.interfaces.<name>`` is
    # always the freshly imported module, and it is cached so subsequent
    # accesses return the identical object.
    globals()[name] = module
    return module


def __dir__() -> list[str]:
    """Return the package's visible attributes, including lazy subpackages.

    Python's default :func:`dir` implementation for modules already lists
    ``__dict__`` keys together with any ``__all__`` entries that are not
    yet bound — so without this override, ``cli`` / ``gui`` / ``web``
    would still appear.  Keeping the override explicit serves two
    purposes:

    1. It documents that the three lazy interfaces are *intentional*
       attributes of this package, not accidental names.
    2. It guarantees the same output even if a future refactor changes how
       ``__all__`` is built or removes it entirely.

    The result is the union of everything actually present in the module's
    namespace (dunders, ``TYPE_CHECKING``, ``Any``, ``__all__`` itself,
    and any subpackage already imported) plus the still-unimported
    subpackages declared in ``__all__``.  Ordering is alphabetical for
    stability across Python versions.
    """
    return sorted(set(globals()) | set(__all__))
