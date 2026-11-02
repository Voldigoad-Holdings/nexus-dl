"""Parser for Asura Scans — English manhwa aggregator (MangaThemesia).

Asura Scans runs on the **MangaThemesia** stack, the same generic WordPress
theme used by Flame Comics and several other English scanlation sites.  The
site is additionally fronted by Cloudflare, so requests may be challenged
and must be routed through FlareSolverr / Playwright when needed.

All four required operations (``search``, ``get_manga``, ``get_chapters``,
``get_pages``) are supplied by two cooperating mixins:

* :class:`~nexusdl.parsers.mixins.mangathemesia.MangaThemesiaMixin`
  understands the standard MangaThemesia routes:

  * Search           — ``GET  /?s={query}&post_type=wp-manga``
  * Manga details    — ``GET  /manga/{slug}/``
  * Chapter list     — ``GET  /manga/{slug}/`` (links embedded in the page)
  * Page list        — ``GET  /manga/{slug}/{chapter-slug}/`` (images in
    the reader container)

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge is
  detected, then replays the original request with the obtained cookies.

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``asurascans`` site id from ``data/sites.yaml``.

The site is English-language and general-audience (non-adult), declared on
the class so the registry can filter it via ``Settings.content.allow_adult``
and the ``language`` filter of ``SiteRegistry.list_sites``.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.mangathemesia import MangaThemesiaMixin

__all__ = ["AsuraScansParser"]


class AsuraScansParser(MangaThemesiaMixin, CloudflareMixin, BaseParser):
    """Parser for Asura Scans (MangaThemesia + Cloudflare, English)."""

    site_id: str = "asurascans"
    language: str = "en"
    adult: bool = False


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("asurascans", AsuraScansParser)
