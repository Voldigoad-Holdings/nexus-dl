"""Parser for hentaizone.xyz — adult manga aggregator (WordPress Madara).

hentaizone.xyz runs on the **Madara** WordPress theme, the same stack used by
x-manga.org, x-manga.net and scan-hentai.fr.  The site is additionally fronted
by Cloudflare, so requests may be challenged and must be routed through
FlareSolverr / Playwright when needed.

All four required operations (``search``, ``get_manga``, ``get_chapters``,
``get_pages``) are supplied by two cooperating mixins:

* :class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`
  understands the standard Madara routes:

  * Search           — ``GET  /?s={query}&post_type=wp-manga``
  * Manga details    — ``GET  /manga/{slug}/``
  * Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
  * Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge is
  detected, then replays the original request with the obtained cookies.

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``hentaizone`` site id from ``data/sites.yaml``.

The site is adult-rated and English-facing, declared on the class so the
registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["HentaiZoneParser"]


class HentaiZoneParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
    """Parser for hentaizone.xyz (WordPress Madara + Cloudflare, adult)."""

    site_id: str = "hentaizone"
    language: str = "en"
    adult: bool = True


# Auto-registration — triggered when ``nexusdl.parsers.adult`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("hentaizone", HentaiZoneParser)
