"""French parser package — auto-registers every French site with the factory.

Importing this package imports each French parser module, which in turn calls
:func:`nexusdl.parsers.factory.register` at module scope.  The aggregate
effect is that every French site id declared under
``data/sites.yaml`` with ``language: fr`` becomes resolvable through
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
from nexusdl.parsers.fr.anime_scans import AnimeScansParser
from nexusdl.parsers.fr.blossom_scans import BlossomScansParser
from nexusdl.parsers.fr.crunchyscan_fr import CrunchyScanFRParser
from nexusdl.parsers.fr.epsilonscan import EpsilonScanParser
from nexusdl.parsers.fr.genkan_scans import GenkanScansParser
from nexusdl.parsers.fr.hentai_origines import HentaiOriginesParser
from nexusdl.parsers.fr.karma_scans import KarmaScansParser
from nexusdl.parsers.fr.mangas_origines import MangasOriginesParser
from nexusdl.parsers.fr.nekohouse import NekohouseParser
from nexusdl.parsers.fr.ortegascans import OrtegaScansParser
from nexusdl.parsers.fr.phenix_scans import PhenixScansParser
from nexusdl.parsers.fr.poseidon_scans import PoseidonScansParser
from nexusdl.parsers.fr.raijin_scans import RaijinScansParser
from nexusdl.parsers.fr.rimuscan import RimuscanParser
from nexusdl.parsers.fr.scan_manga import ScanMangaParser
from nexusdl.parsers.fr.shinra_scans import ShinraScansParser
from nexusdl.parsers.fr.sushiscan import SushiScanParser
from nexusdl.parsers.fr.taisei_scans import TaiseiScansParser
from nexusdl.parsers.fr.toonfr import ToonFrParser
from nexusdl.parsers.fr.urano_scans import UranoScansParser
from nexusdl.parsers.fr.xanadu_scans import XanaduScansParser

__all__ = [
    "AnimeScansParser",
    "BlossomScansParser",
    "CrunchyScanFRParser",
    "EpsilonScanParser",
    "GenkanScansParser",
    "HentaiOriginesParser",
    "KarmaScansParser",
    "MangasOriginesParser",
    "NekohouseParser",
    "OrtegaScansParser",
    "PhenixScansParser",
    "PoseidonScansParser",
    "RaijinScansParser",
    "RimuscanParser",
    "ScanMangaParser",
    "ShinraScansParser",
    "SushiScanParser",
    "TaiseiScansParser",
    "ToonFrParser",
    "UranoScansParser",
    "XanaduScansParser",
]
