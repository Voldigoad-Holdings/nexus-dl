"""Parser for Galaxy Scans — English manga/manhwa aggregator (MangaThemesia).

Galaxy Scans runs on the **MangaThemesia** stack, the same generic WordPress
theme used by Asura Scans, Flame Scans and several other English scanlation
sites.  All four required operations (``search``, ``get_manga``,
``get_chapters``, ``get_pages``) are supplied by
:class:`~nexusdl.parsers.mixins.mangathemesia.MangaThemesiaMixin`, which
understands the standard MangaThemesia routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``GET  /manga/{slug}/`` (links embedded in the page)
* Page list        — ``GET  /manga/{slug}/{chapter-slug}/`` (images in
  the reader container)

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``galaxy_scans`` site id from ``data/sites.yaml``.

The site is English-language and general-audience (non-adult), declared on
the class so the registry can filter it via ``Settings.content.allow_adult``
and the ``language`` filter of ``SiteRegistry.list_sites``.

Notes
-----
Galaxy Scans has historically been reachable at ``galaxyscans.com`` and
``galaxy-scans.com``.  The ``domains`` list in ``data/sites.yaml`` is the
single source of truth for the site's base URL; this parser never hardcodes
a domain.  If the site migrates (as scanlation sites frequently do), only
``sites.yaml`` needs updating — this file stays untouched.

No Cloudflare mixin is inherited: Galaxy Scans does not normally sit behind
a Cloudflare challenge.  If that ever changes, add
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` to the base
class list without touching any of the four operation methods.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.mangathemesia import MangaThemesiaMixin

__all__ = ["GalaxyScansParser"]


class GalaxyScansParser(MangaThemesiaMixin, BaseParser):
    """Parser for Galaxy Scans (MangaThemesia, English)."""

    site_id: str = "galaxy_scans"
    language: str = "en"
    adult: bool = False



# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("galaxy_scans", GalaxyScansParser)
