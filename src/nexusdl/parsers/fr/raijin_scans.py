"""Parser for Raijin Scans — French manga scanlation aggregator.

Raijin Scans (``raijinscan.fr`` / ``raijin-scans.fr``) is a French-language
manga scanlation platform.  The site runs on the **Madara** WordPress theme
and is fronted by **Cloudflare**, as confirmed by Cloudflare Radar scans and
public issue reports from the Komikku/Mihon ecosystem.

The site has a history of domain churn (``raijinscans.com`` →
``raijinscan.fr`` → ``raijin-scans.fr``) and has changed its visual theme at
least once, but the underlying Madara structure has remained in place.  All
four required operations (``search``, ``get_manga``, ``get_chapters``,
``get_pages``) are therefore supplied by
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`,
which understands the standard Madara routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
* Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

Because the site is Cloudflare-protected,
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
upgrades a request to a browser-backed solve when a challenge is detected,
then replays the original request with the obtained cookies.  Some reader
pages additionally expose Madara's ``window._rmd`` / ``window._rmk``
JavaScript variables; the Madara mixin's reader selectors already handle
the standard markup, and the Cloudflare mixin ensures the page is fully
rendered before extraction.

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``raijin_scans`` site id from ``data/sites.yaml``.

The site is French-language and general-audience (non-adult) by declaration.
Adult filtering is governed by ``Settings.content.allow_adult`` at the
session level, exactly like every other general-purpose parser.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  Raijin Scans has migrated across several domains; if it
moves again, only ``sites.yaml`` needs updating — this file stays untouched.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["RaijinScansParser"]


class RaijinScansParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
    """Parser for Raijin Scans (WordPress Madara + Cloudflare, French)."""

    site_id: str = "raijin_scans"
    language: str = "fr"
    adult: bool = False


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("raijin_scans", RaijinScansParser)
