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

The re-export is **lazy** — see :func:`__getattr__` below — for the same
reason every other lazy ``__init__.py`` in this project is lazy: importing
``nexusdl.plugins._example`` must not require ``plugin.py`` to be importable.
A future refactor of ``core.events``, ``core.models.download`` or any other
module that ``plugin.py`` imports would otherwise break the reference
example's import even for callers who only want to inspect the manifest.

The loader itself **never** executes this file. When it loads a plugin it
imports ``plugin.py`` as a standalone module under a namespaced name
(``_nexusdl_plugin_<name>``) via :mod:`importlib.util`, bypassing package
import machinery entirely — see
:meth:`nexusdl.plugins.loader.PluginLoader._import_plugin_module`. The
result is that a user copying this directory gets their own independent
module instance, with no risk of collision with the reference example.

Why the re-export is a **relative** import
------------------------------------------
The lazy loader below uses ``from .plugin import ExamplePlugin`` — a
relative import, not an absolute one. This matters for the "copy to
activate" workflow: when a user copies this directory to
``my_plugins/my_plugin/``, the copied ``__init__.py`` resolves ``.plugin``
against **the copy**, not against the reference example. The "own
independent module instance" promise above therefore holds on every import
path — through the loader (via ``importlib.util.spec``) *and* through
``from my_plugins.my_plugin import ExamplePlugin``. An absolute import
would have made the copy's ``__init__.py`` a silent redirect to the
original reference, which is exactly the kind of footgun a template must
not ship.

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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Declared under ``TYPE_CHECKING`` so that ``mypy --strict`` sees the
    # re-export as a concrete attribute of this package. The runtime
    # implementation is ``__getattr__`` below.
    from .plugin import ExamplePlugin

__all__ = ["ExamplePlugin"]


def __getattr__(name: str) -> Any:
    """Lazily re-export :class:`ExamplePlugin` from ``plugin.py``.

    Called by Python's import machinery whenever an attribute is accessed on
    this package that is not already bound in the module's ``__dict__``
    (PEP 562). Handles exactly one name — :class:`ExamplePlugin` — and
    rejects everything else with the standard ``AttributeError``.

    The lazy pattern is deliberate:

    * Importing ``nexusdl.plugins._example`` must not pull in ``plugin.py``
      and its transitive imports (``core.events``, ``core.models.download``,
      …). A future refactor of any of those modules would otherwise break
      the reference example's import even for callers who only want to
      inspect the manifest or the docstring.
    * Once ``ExamplePlugin`` has been resolved, it is cached in the module's
      ``__dict__`` so subsequent accesses skip this function entirely — the
      standard PEP 562 optimisation.

    Args:
        name: The attribute name being accessed.

    Returns:
        :class:`ExamplePlugin` when *name* is ``"ExamplePlugin"``.

    Raises:
        AttributeError: For any other *name*, or if ``plugin.py`` cannot be
            imported. ``AttributeError`` (rather than the underlying
            exception) is required by PEP 562 so that ``hasattr`` and
            ``getattr(x, name, default)`` behave correctly; the original
            exception is chained as ``__cause__`` so the real reason is
            visible in a traceback.
    """
    # Dunders are probed by the import machinery, by ``pickle``, by
    # ``copy``, by ``inspect``, and by every framework that touches a
    # module.  When one of those probes asks for ``__wrapped__``,
    # ``__path__``, ``__file__`` or any other standard dunder, the
    # answer must be the plain Python ``AttributeError`` — a long
    # explanatory message about ``ExamplePlugin`` is not just unhelpful,
    # it can break tooling that pattern-matches on ``str(exc)``.  The
    # custom message is reserved for *real* attribute lookups that miss.
    if name.startswith("__") and name.endswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    if name != "ExamplePlugin":
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available attributes: {', '.join(__all__)}."
        )

    try:
        from .plugin import ExamplePlugin
    except Exception as exc:  # noqa: BLE001 — PEP 562 requires AttributeError
        raise AttributeError(
            f"Cannot access {name!r} on {__name__!r}: "
            f"'nexusdl.plugins._example.plugin' is not importable. "
            f"Underlying error: {exc}"
        ) from exc

    # PEP 562 caching: bind the attribute in the module's ``__dict__`` so
    # subsequent accesses skip ``__getattr__`` entirely.
    globals()["ExamplePlugin"] = ExamplePlugin
    return ExamplePlugin


def __dir__() -> list[str]:
    """Return the package's visible attributes, including the lazy re-export.

    Python's default :func:`dir` implementation for modules already lists
    ``__all__`` entries that are not yet bound, so this override is not
    strictly necessary. It is kept explicit for the same reason as the
    override in :mod:`nexusdl.interfaces`: it documents that
    :class:`ExamplePlugin` is an *intentional* attribute of this package,
    and it guarantees the same output even if a future refactor changes how
    ``__all__`` is built.
    """
    return sorted(set(__all__) | {"__all__", "__doc__", "__name__", "__package__"})
