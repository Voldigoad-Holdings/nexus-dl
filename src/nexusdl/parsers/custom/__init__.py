"""Custom parser package — auto-discovers user-added parsers.

Any ``*.py`` file dropped into this directory is imported when the package
is loaded, which triggers its module-level
``register("site_id", ParserClass)`` call and makes the parser resolvable
through :class:`~nexusdl.core.registry.site_registry.SiteRegistry`.

The following modules are skipped:

* ``__init__`` — this file.
* ``_template`` — the shipped template, not a real parser.
* Any name starting with ``_`` — private modules, by convention.

Failure semantics
-----------------
If any user-added module fails to import, the exception propagates and the
whole ``nexusdl.parsers`` package fails to load.  This is intentional:

* A broken custom parser is user code and must be fixed before the
  application starts, rather than silently disappearing from the factory.
* The traceback points directly at the offending module, so the fix is
  obvious and local.
* A partially-loaded factory would lead to runtime ``ParserNotFoundError``
  far from the real cause, which is exactly the kind of deferred bug this
  project forbids.

Adding a parser
---------------
Copy ``_template.py`` to ``<your_site>.py``, fill in the identity and the
four abstract methods (or inherit from an existing mixin), add the
``register("<your_site_id>", <YourParserClass>)`` call at the bottom, then
add the site entry to ``data/sites.yaml``.  Nothing else is required — the
dynamic discovery in this module picks the file up automatically.  Explicit
imports in this file are not necessary and are intentionally not used.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Final

__all__: list[str] = []

_SKIPPED_MODULES: Final[frozenset[str]] = frozenset({"__init__", "_template"})


def _iter_custom_module_names() -> list[str]:
    """Return the module names of every user-added custom parser.

    Excludes ``__init__``, ``_template`` and any private module whose name
    starts with an underscore.  Only Python modules and packages are
    returned (data files and directories without ``__init__.py`` are
    ignored by :func:`pkgutil.iter_modules`).
    """
    return [
        info.name
        for info in pkgutil.iter_modules(__path__)
        if info.name not in _SKIPPED_MODULES and not info.name.startswith("_")
    ]


def _import_custom_parsers() -> None:
    """Import every user-added custom parser module.

    Importing a module runs its ``register(...)`` call at module scope,
    which is the only registration mechanism used across
    ``nexusdl.parsers``.
    """
    for name in _iter_custom_module_names():
        importlib.import_module(f"{__name__}.{name}")


_import_custom_parsers()
