"""Korean parser package — auto-registers every Korean site with the factory.

Importing this package imports each Korean parser module, which in turn calls
:func:`nexusdl.parsers.factory.register` at module scope.  The aggregate
effect is that every Korean site id declared under
``data/sites.yaml`` with ``language: ko`` becomes resolvable through
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` as soon as this
package is imported.

This package is imported transitively by :mod:`nexusdl.parsers`, so no manual
wiring is required in any composition root.

The parser classes are also re-exported here so that tests, scripts and
plugins can reference them directly without reaching into the submodules.
"""
from __future__ import annotations

# Importing each module triggers its module-level ``register(...)`` call.
# The imports are intentionally unused at this scope — do not remove them.
from nexusdl.parsers.kr.asura_scans_kr import AsuraScansKrParser
from nexusdl.parsers.kr.manhwa_raw import ManhwaRawParser
from nexusdl.parsers.kr.manhwaclub import ManhwaClubParser

__all__ = [
    "AsuraScansKrParser",
    "ManhwaRawParser",
    "ManhwaClubParser",
]
