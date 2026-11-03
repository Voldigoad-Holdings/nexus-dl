"""Parser for Toonily — English manhwa/webtoon aggregator (WordPress Madara + Cloudflare).

Toonily runs on the **Madara** WordPress theme, the same stack used by
x-manga.org, x-manga.net, scan-hentai.fr, hentaizone.xyz, Disaster Scans,
Zenith Scans and Void Scans.  All four required operations (``search``,
``get_manga``, ``get_chapters``, ``get_pages``) are supplied by
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`, which
understands the standard Madara routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
* Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

The site is additionally fronted by **Cloudflare**, so requests may be
challenged and must be routed through FlareSolverr / Playwright when needed.
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
upgrades a request to a browser-backed solve when a Cloudflare challenge is
detected, then replays the original request with the obtained cookies.

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``toonily`` site id from ``data/sites.yaml``.

The site is English-language and general-audience (non-adult), declared on
the class so the registry can filter it via ``Settings.content.allow_adult``
and the ``language`` filter of ``SiteRegistry.list_sites``.

Notes
-----
Toonily has historically been reachable at ``toonily.com``.  The ``domains``
list in ``data/sites.yaml`` is the single source of truth for the site's base
URL; this parser never hardcodes a domain.  If the site migrates (as
scanlation sites frequently do), only ``sites.yaml`` needs updating — this
file stays untouched.

Toonily serves mature content behind an optional age-gate cookie
(``toonily-mature=1``).  The parser does not set that cookie itself: adult
access is governed by ``Settings.content.allow_adult`` and, when enabled, is
handled centrally by the session layer so that every Madara-based adult site
benefits from the same mechanism.  This keeps the parser free of any
site-specific cookie logic.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["ToonilyParser"]


class ToonilyParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
    """Parser for Toonily (WordPress Madara + Cloudflare, English)."""

    site_id: str = "toonily"
    language: str = "en"
    adult: bool = False


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("toonily", ToonilyParser)
