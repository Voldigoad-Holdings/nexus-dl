"""Public API of the NexusDL plugin subsystem.

This package is the boundary between the framework and the plugin ecosystem.
It exposes four cooperating submodules, each with a well-scoped role:

* :mod:`nexusdl.plugins.api` — the **contract**. :class:`NexusPlugin` (the
  structural protocol every plugin satisfies) and :class:`PluginContext`
  (the immutable bundle of framework services handed to ``on_load``).
* :mod:`nexusdl.plugins.hooks` — the **single source of truth** for every
  hook: name, sync/async flavour, and one-line summary. No other module in
  the codebase may reference a hook by string literal.
* :mod:`nexusdl.plugins.validators` — validation of ``nexus.plugin.yaml``
  manifests and of plugin classes against their manifest.
* :mod:`nexusdl.plugins.loader` — discovery, import, lifecycle, and hook
  dispatch. The only module allowed to execute plugin ``plugin.py`` files.

A reference plugin implementation lives in :mod:`nexusdl.plugins._example`.
The underscore prefix is reserved: the loader skips every directory whose
name starts with ``_``, so the example is never activated at runtime — it
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
submodule is the intended escape hatch — it signals that the code is
tooling, not part of the stable public API.
"""

from __future__ import annotations

# --- Contract (plugin authors) --------------------------------------------- #
from nexusdl.plugins.api import (
    NexusPlugin,
    PluginContext,
)

# --- Hook names (constants) ------------------------------------------------ #
from nexusdl.plugins.hooks import (
    HOOK_ON_AFTER_DOWNLOAD,
    HOOK_ON_BEFORE_DOWNLOAD,
    HOOK_ON_ERROR,
    HOOK_ON_LOAD,
    HOOK_ON_PAGE_DOWNLOADED,
    HOOK_ON_UNLOAD,
)

# --- Hook types and aggregated views (CLI, docs, interfaces) --------------- #
from nexusdl.plugins.hooks import (
    ALL_HOOK_NAMES,
    ASYNC_HOOK_NAMES,
    HOOK_SPECS,
    SYNC_HOOK_NAMES,
    HookFlavour,
    HookName,
    HookSpec,
)

# --- Manifest (tools, CLI, tests) ------------------------------------------ #
from nexusdl.plugins.validators import (
    MANIFEST_FILENAME,
    PluginManifest,
    load_manifest,
    validate_plugin,
)

# --- Loader (composition roots, CLI) -------------------------------------- #
from nexusdl.plugins.loader import (
    LoadReport,
    LoadedPlugin,
    PluginLoader,
)

__all__ = [
    # --- Contract --------------------------------------------------------- #
    "NexusPlugin",
    "PluginContext",
    # --- Hook names ------------------------------------------------------- #
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
    # --- Manifest -------------------------------------------------------- #
    "MANIFEST_FILENAME",
    "PluginManifest",
    "load_manifest",
    "validate_plugin",
    # --- Loader ---------------------------------------------------------- #
    "LoadReport",
    "LoadedPlugin",
    "PluginLoader",
]
