"""Public API of the NexusDL plugin subsystem.

This package is the boundary between the framework and the plugin ecosystem.
It exposes four cooperating submodules, each with a well-scoped role:

* :mod:`nexusdl.plugins.api` - the **contract**. :class:`NexusPlugin` (the
  structural protocol every plugin satisfies) and :class:`PluginContext`
  (the immutable bundle of framework services handed to ``on_load``).
* :mod:`nexusdl.plugins.hooks` - the **single source of truth** for every
  hook: name, sync/async flavour, and one-line summary. No other module in
  the codebase may reference a hook by string literal.
* :mod:`nexusdl.plugins.validators` - validation of ``nexus.plugin.yaml``
  manifests and of plugin classes against their manifest.
* :mod:`nexusdl.plugins.loader` - discovery, import, lifecycle, and hook
  dispatch. The only module allowed to execute plugin ``plugin.py`` files.

A reference plugin implementation lives in :mod:`nexusdl.plugins._example`.
The underscore prefix is reserved: the loader skips every directory whose
name starts with ``_``, so the example is never activated at runtime - it
is shipped as a copyable template only.

Two audiences
-------------
**Plugin authors** import only the contract::

    from nexusdl.plugins import NexusPlugin, PluginContext

    class MyPlugin:
        name = "my_plugin"
        version = "0.1.0"

        def on_load(self, context: PluginContext) -> None: ...
        def on_unload(self) -> None: ...

        # ...optional async pipeline hooks, declared in nexus.plugin.yaml...

Static conformance is checked by ``mypy --strict`` against
:class:`NexusPlugin` (use the ``if TYPE_CHECKING:`` assertion pattern shown
in ``_example/plugin.py``).

**Host code** (composition roots, CLI/TUI/GUI/Web) imports the loader and
the introspection surface::

    from nexusdl.plugins import PluginLoader, LoadReport, PluginContext
    from nexusdl.plugins import HOOK_SPECS, HookSpec, HookName

    loader = PluginLoader(settings=settings, event_bus=bus, http_session=session)
    report = await loader.load_all()

Lazy loading
------------
Importing ``nexusdl.plugins`` **does not** import any submodule. The four
submodules are resolved on first attribute access, exactly like the rest
of the project (see :mod:`nexusdl.interfaces`, :mod:`nexusdl.interfaces.web`,
:mod:`nexusdl.interfaces.gui`, …). Concretely:

.. code-block:: python

    import nexusdl.plugins               # ← instant, no submodule loaded
    from nexusdl.plugins import PluginContext   # ← loads plugins.api only
    from nexusdl.plugins import PluginLoader    # ← loads plugins.loader

This matters because:

* A **plugin author** writing ``from nexusdl.plugins import NexusPlugin``
  in their own ``plugin.py`` should not pay for :mod:`nexusdl.plugins.loader`
  (which imports :mod:`yaml`, :mod:`jsonschema`, :mod:`importlib` machinery)
  or for :mod:`nexusdl.plugins.validators` (which imports :mod:`yaml` and
  :mod:`jsonschema`). They only need the protocol.
* The **CLI** command ``nexusdl plugins list`` needs the loader, not the
  ``NexusPlugin`` protocol. It gets the loader alone.
* A **test** that wants to introspect the hook registry (``list_symbols()``)
  does not trigger any import at all.

The error handling follows the same pattern as the rest of the project:
missing optional dependencies (``pyyaml``, ``jsonschema``) are converted
from :class:`ImportError` to :class:`AttributeError` (as PEP 562 requires)
with the underlying error chained as ``__cause__`` and a hint pointing at
the ``[plugins]`` extra.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following helpers are **not** re-exported here, on purpose. They exist
for tooling and tests, are allowed to change between minor versions, and
must be imported from their submodule:

* ``nexusdl.plugins.hooks.get_hook_spec``, ``get_hook_flavour``,
  ``is_known_hook``, ``is_sync_hook``, ``is_async_hook``,
  ``check_hook_flavour``, ``verify_protocol_consistency``
* ``nexusdl.plugins.validators.validate_manifest_compatibility``,
  ``validate_class_matches_manifest``, ``export_json_schema``
* ``nexusdl.plugins.loader._find_plugin_class`` and all other ``_``-prefixed
  helpers

If an interface genuinely needs one of these, importing it from its
submodule is the intended escape hatch - it signals that the code is
tooling, not part of the stable public API.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- Contract (plugin authors) ---------------------------------------- #
    "NexusPlugin",
    "PluginContext",
    # --- Hook name constants --------------------------------------------- #
    "HOOK_ON_LOAD",
    "HOOK_ON_UNLOAD",
    "HOOK_ON_BEFORE_DOWNLOAD",
    "HOOK_ON_AFTER_DOWNLOAD",
    "HOOK_ON_PAGE_DOWNLOADED",
    "HOOK_ON_ERROR",
    # --- Hook types and aggregated views --------------------------------- #
    "HookFlavour",
    "HookName",
    "HookSpec",
    "ALL_HOOK_NAMES",
    "ASYNC_HOOK_NAMES",
    "SYNC_HOOK_NAMES",
    "HOOK_SPECS",
    # --- Manifest (validators) -------------------------------------------- #
    "MANIFEST_FILENAME",
    "PluginManifest",
    "load_manifest",
    "validate_plugin",
    # --- Loader (composition roots, CLI) --------------------------------- #
    "LoadReport",
    "LoadedPlugin",
    "PluginLoader",
    # --- Introspection ---------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> submodule basename. Every listed symbol is defined
# at module level in the given submodule. Adding a new top-level export is
# a one-line change here plus a matching TYPE_CHECKING re-export below.

_LAZY: dict[str, str] = {
    # --- plugins.api — the contract ---------------------------------------
    "NexusPlugin": "api",
    "PluginContext": "api",
    # --- plugins.hooks — hook names, flavours, aggregated views -----------
    "HOOK_ON_LOAD": "hooks",
    "HOOK_ON_UNLOAD": "hooks",
    "HOOK_ON_BEFORE_DOWNLOAD": "hooks",
    "HOOK_ON_AFTER_DOWNLOAD": "hooks",
    "HOOK_ON_PAGE_DOWNLOADED": "hooks",
    "HOOK_ON_ERROR": "hooks",
    "HookFlavour": "hooks",
    "HookName": "hooks",
    "HookSpec": "hooks",
    "ALL_HOOK_NAMES": "hooks",
    "ASYNC_HOOK_NAMES": "hooks",
    "SYNC_HOOK_NAMES": "hooks",
    "HOOK_SPECS": "hooks",
    # --- plugins.validators — manifest validation -------------------------
    "MANIFEST_FILENAME": "validators",
    "PluginManifest": "validators",
    "load_manifest": "validators",
    "validate_plugin": "validators",
    # --- plugins.loader — discovery, import, lifecycle, dispatch ----------
    "LoadReport": "loader",
    "LoadedPlugin": "loader",
    "PluginLoader": "loader",
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from .api import NexusPlugin, PluginContext
    from .hooks import (
        ALL_HOOK_NAMES,
        ASYNC_HOOK_NAMES,
        HOOK_ON_AFTER_DOWNLOAD,
        HOOK_ON_BEFORE_DOWNLOAD,
        HOOK_ON_ERROR,
        HOOK_ON_LOAD,
        HOOK_ON_PAGE_DOWNLOADED,
        HOOK_ON_UNLOAD,
        HOOK_SPECS,
        SYNC_HOOK_NAMES,
        HookFlavour,
        HookName,
        HookSpec,
    )
    from .loader import LoadReport, LoadedPlugin, PluginLoader
    from .validators import (
        MANIFEST_FILENAME,
        PluginManifest,
        load_manifest,
        validate_plugin,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public plugin symbol on first access (PEP 562).

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
            such as ``pyyaml`` or ``jsonschema``, a syntax error deeper
            in the module, or a broken installation), or when the
            submodule does not define the requested name. ``AttributeError``
            - rather than ``ImportError`` - is required by PEP 562 so that
            ``hasattr`` works.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    submodule = _LAZY.get(name)
    if submodule is None:
        available = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available plugin symbols: {available}"
        )

    try:
        module = importlib.import_module(f".{submodule}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the plugin extras.
        if exc.name in {"yaml", "jsonschema"}:
            raise ImportError(
                f"{name!r} requires the plugins extra. "
                f"Install it with: pip install 'nexusdl[plugins]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, name)
    except AttributeError:
        raise AttributeError(
            f"The plugins submodule {submodule!r} does not define "
            f"{name!r}. This is a package-internal inconsistency - "
            f"nexusdl.plugins.__all__ and the submodules are out of sync."
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
    the plugin symbols are present in ``__dict__`` at import time, an
    unmodified :func:`dir` would not list them - the package would appear
    empty in a REPL.

    The returned list is :data:`__all__` plus the standard module dunders
    a user typically expects to see.
    """
    return sorted(
        set(__all__) | {"__all__", "__doc__", "__name__", "__package__"}
    )


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy symbols.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and future tooling that needs
    to enumerate the plugin API without importing every submodule.
    """
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the plugin symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (plugin introspection tools, parametrized tests, plugin sandboxes).

    Raises:
        KeyError: If ``name`` is not a public plugin symbol.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(__all__))
        raise KeyError(
            f"Unknown plugin symbol {name!r}. Available: {available}"
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
