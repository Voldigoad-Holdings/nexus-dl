"""Parser for Hentai-Origines — French adult manga/doujinshi scanlation platform.

Hentai-Origines (``hentai-origines.com``, with mirror ``hentai-origines.fr``)
is a French-language adult scanlation platform covering doujinshi and adult
manga.  The site runs on the **Madara** WordPress theme, the same stack used
by x-manga.org, x-manga.net, scan-hentai.fr, hentaizone.xyz, toonfr,
raijin_scans, scyllascans, void_scans, zenith_scans and Disaster Scans.
The site is fronted by **Cloudflare**, like most French adult scanlation
platforms.

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
``hentai_origines`` site id from ``data/sites.yaml``.

The site is French-language and **adult** (NSFW), declared on the class so
the registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  Hentai-Origines has operated under both ``.com`` and ``.fr``
over the years; if the primary domain changes again, only ``sites.yaml``
needs updating — this file stays untouched.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["HentaiOriginesParser"]


class HentaiOriginesParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
    """Parser for Hentai-Origines (WordPress Madara + Cloudflare, adult, French)."""

    site_id: str = "hentai_origines"
    language: str = "fr"
    adult: bool = True


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("hentai_origines", HentaiOriginesParser)
