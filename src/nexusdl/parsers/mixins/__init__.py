"""Parser mixins package — re-exports every strategy mixin.

Each mixin in this package implements one reusable parsing strategy that
concrete parsers combine with :class:`~nexusdl.parsers.base.BaseParser`:

* :class:`ApiBasedMixin` — helpers for API-driven sites (nhentai, Comick,
  MangaDex, Nekohouse).
* :class:`CloudflareMixin` — transparent Cloudflare challenge solving via
  FlareSolverr / Playwright, with cookie injection and single-retry.
* :class:`FoolSlideMixin` — the four operations for FoolSlide readers
  (Kirei Cake).
* :class:`JsRenderedMixin` — Playwright-backed DOM acquisition for
  JavaScript-rendered sites, with transparent delegation for Madara-style
  parsers.
* :class:`MangaThemesiaMixin` — the four operations for MangaThemesia sites
  (Asura Scans, Flame Scans, Galaxy Scans, Leviathan Scans, Luminous Scans).
* :class:`WordPressMadaraMixin` — the four operations for WordPress Madara
  sites (most of the FR and EN catalogues, plus hentaizone, toonily, etc.).

MRO convention
--------------
When a concrete parser combines several mixins, the order matters.  The
project-wide convention is::

    class MyParser(JsRenderedMixin, CloudflareMixin, WordPressMadaraMixin, BaseParser):
        ...

The rule is: **rendering before Cloudflare before theme before base**.
Rationale:

* ``JsRenderedMixin`` first — its overrides of the four operations call
  ``super()``, so the next mixin in the MRO receives the delegated call
  with the rendering session already installed.
* ``CloudflareMixin`` second — it patches ``session.get_html`` at ``__init__``
  time; placing it before the theme mixins ensures the patch is in place
  before any operation runs.
* Theme mixin (Madara / MangaThemesia / FoolSlide) next — provides the
  default implementations that the earlier mixins delegate to.
* ``BaseParser`` last — provides the abstract contract and shared helpers.

The mixins are re-exported here so that concrete parsers import from a
single, stable path::

    from nexusdl.parsers.mixins import (
        CloudflareMixin,
        JsRenderedMixin,
        WordPressMadaraMixin,
    )
"""
from __future__ import annotations

from nexusdl.parsers.mixins.api_based import ApiBasedMixin
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.foolslide import FoolSlideMixin
from nexusdl.parsers.mixins.js_rendered import JsRenderedMixin
from nexusdl.parsers.mixins.mangathemesia import MangaThemesiaMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = [
    "ApiBasedMixin",
    "CloudflareMixin",
    "FoolSlideMixin",
    "JsRenderedMixin",
    "MangaThemesiaMixin",
    "WordPressMadaraMixin",
]
