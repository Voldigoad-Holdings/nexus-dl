"""Parser for x-manga.org — French adult manga aggregator.

x-manga.org runs on WordPress Madara. All four required operations
(``search``, ``get_manga``, ``get_chapters``, ``get_pages``) are supplied by
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`, which
understands the standard Madara routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
* Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``x_manga_org`` site id from ``data/sites.yaml``.

The site is adult-rated and French-language, which is declared on the class
so the registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["XMangaOrgParser"]


class XMangaOrgParser(WordPressMadaraMixin, BaseParser):
    """Parser for x-manga.org (WordPress Madara, adult, French)."""

    site_id: str = "x_manga_org"
    language: str = "fr"
    adult: bool = True


# Auto-registration — triggered when ``nexusdl.parsers.adult`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("x_manga_org", XMangaOrgParser)
