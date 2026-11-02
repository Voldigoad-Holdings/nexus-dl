"""Adult parser package — auto-registers every adult site with the factory.

Importing this package imports each adult parser module, which in turn calls
:func:`nexusdl.parsers.factory.register` at module scope.  The aggregate
effect is that every adult site id declared under
``data/sites.yaml`` with ``adult: true`` becomes resolvable through
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
from nexusdl.parsers.adult.hentaizone import HentaiZoneParser
from nexusdl.parsers.adult.nhentai import NHentaiParser
from nexusdl.parsers.adult.pururin import PururinParser
from nexusdl.parsers.adult.scan_hentai import ScanHentaiParser
from nexusdl.parsers.adult.x_manga_net import XMangaNetParser
from nexusdl.parsers.adult.x_manga_org import XMangaOrgParser

__all__ = [
    "HentaiZoneParser",
    "NHentaiParser",
    "PururinParser",
    "ScanHentaiParser",
    "XMangaNetParser",
    "XMangaOrgParser",
]
