"""Reference NexusDL plugin package — a complete, self-contained example.

This package is a **template**, not a plugin that the framework activates by
default:

* The directory name starts with ``_``, and
  :meth:`nexusdl.plugins.loader.PluginLoader._iter_plugin_dirs` skips every
  such directory (the underscore prefix is reserved for internal and
  reference packages). The example is therefore never loaded at runtime —
  it is shipped inside the wheel purely as package data so users can copy
  it as a starting point.
* To activate the example, copy the whole directory into your user plugin
  directory (see ``docs/source/development/plugins.md`` for the platform-
  specific location) and rename the copy so it no longer starts with ``_``.
  Then update ``name:`` / ``version:`` in ``nexus.plugin.yaml`` **and** the
  matching class attributes in ``plugin.py`` — the loader cross-checks both.

Contents
--------
* ``nexus.plugin.yaml`` — the manifest. Parsed and validated by
  :func:`nexusdl.plugins.validators.load_manifest` *before* ``plugin.py`` is
  imported; a schema violation aborts the load with
  :class:`~nexusdl.core.exceptions.PluginError`.
* ``plugin.py`` — the implementation of :class:`ExamplePlugin`, a
  strictly-transparent passthrough that exercises every hook of the
  :class:`~nexusdl.plugins.api.NexusPlugin` protocol.

Import behaviour
----------------
This ``__init__.py`` exists so the example can be imported as a normal
Python package::

    from nexusdl.plugins._example import ExamplePlugin

The loader itself **never** executes this file. When it loads a plugin it
imports ``plugin.py`` as a standalone module under a namespaced name
(``_nexusdl_plugin_<name>``) via :mod:`importlib.util`, bypassing package
import machinery entirely — see
:meth:`nexusdl.plugins.loader.PluginLoader._import_plugin_module`. The
result is that a user copying this directory gets their own independent
module instance, with no risk of collision with the reference example.

The re-export below uses a **relative** import (``from .plugin import
ExamplePlugin``), not an absolute one (``from
nexusdl.plugins._example.plugin import ExamplePlugin``). This matters for
the "copy to activate" workflow: when a user copies this directory to
``my_plugins/my_plugin/``, the copied ``__init__.py`` now resolves
``.plugin`` against **the copy**, not against the reference example. The
"own independent module instance" promise above therefore holds on every
import path — through the loader (via ``importlib.util.spec``) *and*
through ``from my_plugins.my_plugin import ExamplePlugin``. An absolute
import would have made the copy's ``__init__.py`` a silent redirect to
the original reference, which is exactly the kind of footgun a template
must not ship.

The re-export below is therefore intended for **tests and documentation**,
which want a stable import path to assert against the reference
implementation. Production code never imports from this package.

Public API
----------
Only :class:`ExamplePlugin` is exported. The package deliberately hides
everything else so that future internal additions (helper modules, type
stubs, tests) do not widen the public surface.
"""

from __future__ import annotations

# Relative import (deliberate — see "Import behaviour" above).
from .plugin import ExamplePlugin

__all__ = ["ExamplePlugin"]
