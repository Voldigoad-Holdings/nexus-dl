"""Reference NexusDL plugin package.

This directory is a *complete, self-contained* example of a NexusDL plugin.
It is shipped inside the wheel (as package data) so users can copy it as a
starting point, but it is **not** loaded by default — directories whose names
start with an underscore are skipped by :mod:`nexusdl.plugins.loader`.

Contents
--------
* ``nexus.plugin.yaml`` — the manifest (identity, compatibility, hooks,
  dependencies). See ``docs/source/development/plugins.md`` for the schema.
* ``plugin.py`` — the implementation of :class:`ExamplePlugin`, a
  strictly-transparent passthrough that exercises every hook of the
  :class:`~nexusdl.plugins.api.NexusPlugin` protocol.

The single public symbol re-exported here is :class:`ExamplePlugin`, so that
tests and documentation can ``from nexusdl.plugins._example import
ExamplePlugin`` without reaching into the private module path.

To use this as a template for a real plugin:

1. Copy the whole ``_example/`` directory to your plugin directory
   (see ``docs/source/development/plugins.md`` for the location on each
   platform).
2. Rename the directory so it no longer starts with ``_``.
3. Update ``name:`` and ``version:`` in ``nexus.plugin.yaml`` **and** the
   matching class attributes in ``plugin.py`` — the loader cross-checks them.
4. Replace the hook bodies with your own logic.
"""

from __future__ import annotations

from nexusdl.plugins._example.plugin import ExamplePlugin

__all__ = ["ExamplePlugin"]
