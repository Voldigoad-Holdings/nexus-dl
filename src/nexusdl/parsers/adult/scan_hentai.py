"""Parser for scan-hentai.fr — French adult manga aggregator.

scan-hentai.fr runs on the WordPress Madara theme, exactly like its siblings
``x-manga.org`` and ``x-manga.net``.  All four required operations
(``search``, ``get_manga``, ``get_chapters``, ``get_pages``) are supplied by
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`, which
handles the standard Madara routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
* Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

This module only declares the site identity and registers the parser with
the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``scan_hentai`` site id from ``data/sites.yaml``.

The site is adult-rated and French-language, declared on the class so the
registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["ScanHentaiParser"]


class ScanHentaiParser(WordPressMadaraMixin, BaseParser):
    """Parser for scan-hentai.fr (WordPress Madara, adult, French)."""

    site_id: str = "scan_hentai"
    language: str = "fr"
    adult: bool = True


# Auto-registration — triggered when ``nexusdl.parsers.adult`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("scan_hentai", ScanHentaiParser)
