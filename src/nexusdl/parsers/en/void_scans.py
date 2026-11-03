"""Parser for Void Scans — English manga/manhwa aggregator (WordPress Madara).

Void Scans runs on the **Madara** WordPress theme, the same stack used by
x-manga.org, x-manga.net, scan-hentai.fr, hentaizone.xyz, Disaster Scans and
Zenith Scans.  All four required operations (``search``, ``get_manga``,
``get_chapters``, ``get_pages``) are supplied by
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`, which
understands the standard Madara routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
* Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``void_scans`` site id from ``data/sites.yaml``.

The site is English-language and general-audience (non-adult), declared on
the class so the registry can filter it via ``Settings.content.allow_adult``
and the ``language`` filter of ``SiteRegistry.list_sites``.

Notes
-----
Void Scans has historically been reachable at ``void-scans.com`` and
``voidscans.com``.  The ``domains`` list in ``data/sites.yaml`` is the
single source of truth for the site's base URL; this parser never hardcodes
a domain.  If the site migrates (as scanlation sites frequently do), only
``sites.yaml`` needs updating — this file stays untouched.

No Cloudflare mixin is inherited: Void Scans does not normally sit behind a
Cloudflare challenge.  If that ever changes, add
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` to the base
class list without touching any of the four operation methods.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["VoidScansParser"]


class VoidScansParser(WordPressMadaraMixin, BaseParser):
    """Parser for Void Scans (WordPress Madara, English)."""

    site_id: str = "void_scans"
    language: str = "en"
    adult: bool = False


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("void_scans", VoidScansParser)
