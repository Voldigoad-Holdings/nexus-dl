"""Parser for Kirei Cake — English scanlation group (FoolSlide).

Kirei Cake publishes its releases through a self-hosted **FoolSlide** reader
at ``reader.kireicake.com``[reference:0].  The main site (``kireicake.com``)
acts as a project index and links directly into the reader, so all scraping
can be delegated to the FoolSlide backend without touching the WordPress
front-end.

FoolSlide is an open-source comic management system that exposes a
consistent set of routes across every site that runs it:

* Search           — ``POST /search/``  (form field ``search``)
* Manga details    — ``GET  /series/{slug}/``
* Chapter list     — embedded in the manga page (links to
  ``/read/{manga_slug}/{lang}/{chapter_slug}/``)
* Page list        — embedded in the reader page as a JavaScript variable
  (``var pages = [...]``) containing a JSON array of page objects[reference:1]

All four required operations (``search``, ``get_manga``, ``get_chapters``,
``get_pages``) are supplied by
:class:`~nexusdl.parsers.mixins.foolslide.FoolSlideMixin`, which knows these
routes and handles the reader’s page extraction automatically.

This module therefore only declares the site identity and registers the
parser with the global factory so that
:class:`~nexusdl.core.registry.site_registry.SiteRegistry` can resolve the
``kireicake`` site id from ``data/sites.yaml``.

The site is English-language and general-audience (non-adult).  Kirei Cake
does not sit behind Cloudflare, so no Cloudflare mixin is inherited.  The
FoolSlide reader may require a lightweight ``adult=true`` POST confirmation
for mature series; the mixin transparently handles that step when needed.

Domain note
-----------
The FoolSlide reader is served from a **sub-domain**
(``reader.kireicake.com``) while the main site lives at ``kireicake.com``.
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  If the reader ever moves, only ``sites.yaml`` needs
updating — this file stays untouched.
"""
from __future__ import annotations

from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.foolslide import FoolSlideMixin

__all__ = ["KireiCakeParser"]


class KireiCakeParser(FoolSlideMixin, BaseParser):
    """Parser for Kirei Cake (FoolSlide reader, English)."""

    site_id: str = "kireicake"
    language: str = "en"
    adult: bool = False


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("kireicake", KireiCakeParser)
