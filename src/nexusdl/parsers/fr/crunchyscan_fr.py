"""Parser for CrunchyScan — French manga/manhwa/webtoon scanlation platform.

CrunchyScan (``crunchyscan.fr``, with mirror ``crunchyscan.org``) is a large
French-language scanlation platform that publishes manga, manhwa and webtoon
chapters translated by volunteer teams.  The site runs on the **Madara**
WordPress theme and is fronted by **Cloudflare**.  A Keiyoushi extension
request from January 2024 confirms both facts explicitly: “18+/NFSW = Yes,
Cloudflare protection between each chapter before reading it, Wordpress
Madara theme”[reference:0].

The site is additionally known to inject **captchas inside the chapters**
themselves — the Keiyoushi “Add captcha interceptor option to Madara” issue
lists ``https://crunchyscan.fr`` as one of the two canonical examples of
Madara sites doing this[reference:1].  This parser therefore inherits from two
mixins:

* :class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`
  supplies all four required operations (``search``, ``get_manga``,
  ``get_chapters``, ``get_pages``) using the standard Madara routes:

  * Search           — ``GET  /?s={query}&post_type=wp-manga``
  * Manga details    — ``GET  /manga/{slug}/``
  * Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
  * Page list        — ``GET  /manga/{slug}/{chapter-slug}/``

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge
  is detected, then replays the original request with the obtained cookies.
  This is what makes the “Cloudflare protection between each chapter”
  behaviour transparent to the caller.

Captcha handling
----------------
CrunchyScan’s in-chapter captchas are a site-specific anti-scraping measure
that cannot be solved statically.  Rather than embedding a fragile captcha
solver in this parser, the project’s session layer is expected to route
reader pages through the same Playwright pool that the
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` already uses.
When the captcha is presented as an interactive challenge (the common case),
Playwright’s default behaviour of waiting for the challenge to clear is
sufficient, and the page images become available in the rendered DOM.  This
keeps the parser free of any captcha-specific code, in line with the
project’s “no site-specific hacks” philosophy.

Custom reader path
------------------
Some CrunchyScan mirrors expose a custom reader path such as
``/lecture-en-ligne/{slug}/read/chapitre-{n}`` instead of the standard
Madara ``/manga/{slug}/{chapter-slug}/``.  The Madara mixin’s ``get_pages``
follows whatever URL the chapter list provides, so this difference is
handled automatically as long as the chapter list itself is scraped from
the standard Madara manga page.  No override is required.

Route patterns
--------------
* Search           — ``GET  /?s={query}&post_type=wp-manga``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST /manga/{slug}/ajax/chapters/``
* Reader           — ``GET  /manga/{slug}/{chapter-slug}/``
                     ``GET  /lecture-en-ligne/{slug}/read/chapitre-{n}``

The site is French-language and **adult** (NSFW), declared on the class so
the registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  CrunchyScan has migrated between ``.fr`` and ``.org``
over the years[reference:2]; if it moves again, only ``sites.yaml`` needs
updating — this file stays untouched.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["CrunchyScanFRParser"]


class CrunchyScanFRParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
    """Parser for CrunchyScan (WordPress Madara + Cloudflare, adult, French)."""

    site_id: str = "crunchyscan_fr"
    language: str = "fr"
    adult: bool = True


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("crunchyscan_fr", CrunchyScanFRParser)
