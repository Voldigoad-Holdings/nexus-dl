"""English parser package — auto-registers every English site with the factory.

Importing this package imports each English parser module, which in turn
calls :func:`nexusdl.parsers.factory.register` at module scope.  The aggregate
effect is that every English site id declared under
``data/sites.yaml`` with ``language: en`` becomes resolvable through
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
from nexusdl.parsers.en.asurascans import AsuraScansParser
from nexusdl.parsers.en.batoto import BatoToParser
from nexusdl.parsers.en.comick import ComickParser
from nexusdl.parsers.en.disasterscans import DisasterScansParser
from nexusdl.parsers.en.flamescans import FlameScansParser
from nexusdl.parsers.en.galaxy_scans import GalaxyScansParser
from nexusdl.parsers.en.kireicake import KireiCakeParser
from nexusdl.parsers.en.leviathanscans import LeviathanScansParser
from nexusdl.parsers.en.luminousscans import LuminousScansParser
from nexusdl.parsers.en.mangabuddy import MangaBuddyParser
from nexusdl.parsers.en.mangadex import MangaDexParser
from nexusdl.parsers.en.mangafire import MangaFireParser
from nexusdl.parsers.en.mangakakalot import MangaKakalotParser
from nexusdl.parsers.en.mangasee123 import MangaSee123Parser
from nexusdl.parsers.en.reaperscans import ReaperScansParser
from nexusdl.parsers.en.scyllascans import ScyllaScansParser
from nexusdl.parsers.en.tcbscans import TCBScansParser
from nexusdl.parsers.en.toongod import ToonGodParser
from nexusdl.parsers.en.toonily import ToonilyParser
from nexusdl.parsers.en.void_scans import VoidScansParser
from nexusdl.parsers.en.zenith_scans import ZenithScansParser

__all__ = [
    "AsuraScansParser",
    "BatoToParser",
    "ComickParser",
    "DisasterScansParser",
    "FlameScansParser",
    "GalaxyScansParser",
    "KireiCakeParser",
    "LeviathanScansParser",
    "LuminousScansParser",
    "MangaBuddyParser",
    "MangaDexParser",
    "MangaFireParser",
    "MangaKakalotParser",
    "MangaSee123Parser",
    "ReaperScansParser",
    "ScyllaScansParser",
    "TCBScansParser",
    "ToonGodParser",
    "ToonilyParser",
    "VoidScansParser",
    "ZenithScansParser",
]
