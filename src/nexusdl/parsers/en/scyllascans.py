"""Parser for Scylla Scans — English manga/manhwa aggregator.

.. warning::
   Scylla Scans announced its **disbandment on 4 November 2025**.  The
   group has been marked inactive on MangaDex and ComicK, and the original
   ``scyllascans.org`` domain no longer serves the site.  A successor
   domain ``scyllacomics.xyz`` was announced before the shutdown, but the
   project is no longer maintained.  This parser is kept for historical
   completeness and for the day the site (or a community mirror under the
   same name) returns.

Architecture
------------
Historically Scylla Scans ran on the **Madara** WordPress theme, the same
stack used by x-manga.org, Disaster Scans, Zenith Scans, Void Scans and
Toonily.  All four required operations (``search``, ``get_manga``,
``get_chapters``, ``get_pages``) are supplied by
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`,
which understands the standard Madara routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
* Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``scyllascans`` site id from ``data/sites.yaml``.

The site is English-language and general-audience (non-adult), declared on
the class so the registry can filter it via ``Settings.content.allow_adult``
and the ``language`` filter of ``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  Scylla Scans migrated from ``scyllascans.org`` to
``scyllacomics.xyz`` before shutting down; if the site ever returns under
a new domain, only ``sites.yaml`` needs updating — this file stays
untouched.

No Cloudflare mixin is inherited: Scylla Scans did not normally sit behind
a Cloudflare challenge.  If that ever changes, add
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` to the base
class list without touching any of the four operation methods.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["ScyllaScansParser"]


class ScyllaScansParser(WordPressMadaraMixin, BaseParser):
    """Parser for Scylla Scans (WordPress Madara, English)."""

    site_id: str = "scyllascans"
    language: str = "en"
    adult: bool = False


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("scyllascans", ScyllaScansParser)
