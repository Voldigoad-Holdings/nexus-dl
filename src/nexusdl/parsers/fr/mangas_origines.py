"""Parser for Mangas-Origines — French manga/manhwa scanlation platform.

Mangas-Origines (``mangas-origines.fr``, with the sibling site
``hentai-origines.com``) is a French-language scanlation platform covering
manga and manhwa.  It is operated by the same team as Hentai-Origines
(already supported in this project) and shares the same infrastructure.

The site runs on the **Madara** WordPress theme, the same stack used by
x-manga.org, x-manga.net, scan-hentai.fr, hentaizone.xyz, toonfr,
raijin_scans, scyllascans, void_scans, zenith_scans, Disaster Scans and
Hentai-Origines.  The site is fronted by **Cloudflare**, like most French
scanlation platforms.

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
``mangas_origines`` site id from ``data/sites.yaml``.

The site is French-language and general-audience (non-adult) by declaration.
Adult content is served by the sibling Hentai-Origines site, which is
registered separately.  Adult filtering on Mangas-Origines itself is governed
by ``Settings.content.allow_adult`` at the session level, exactly like every
other general-purpose parser.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  Mangas-Origines has operated under both ``.com`` and ``.fr``
over the years; if the primary domain changes again, only ``sites.yaml``
needs updating — this file stays untouched.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["MangasOriginesParser"]


class MangasOriginesParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
    """Parser for Mangas-Origines (WordPress Madara + Cloudflare, French)."""

    site_id: str = "mangas_origines"
    language: str = "fr"
    adult: bool = False


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("mangas_origines", MangasOriginesParser)
