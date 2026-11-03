"""Parser for Reaper Scans — English manga/manhwa aggregator.

.. warning::
   Reaper Scans announced its permanent shutdown on 9 May 2025 after
   receiving a cease-and-desist notice from Kakao Entertainment.  The
   ``reaperscans.com`` domain may still resolve (to a placeholder, a
   redirect, or a parked page), but the original content is no longer
   served.  This parser is kept for completeness and for the day the site
   (or a successor under the same domain) returns.

Architecture
------------
Historically Reaper Scans ran on the **Madara** WordPress theme and was
fronted by **Cloudflare**.  However, it diverged from stock Madara in one
critical way: the chapter list was rendered by **Livewire** (Laravel), not
by the standard ``/ajax/chapters/`` endpoint.  Stock Madara parsers
therefore could not enumerate chapters without executing JavaScript or
replaying Livewire requests.

To remain robust without embedding fragile Livewire handshakes, this
parser combines three mixins:

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge
  is detected, then replays the original request with the obtained cookies.
* :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` renders pages
  through the shared Playwright pool, which lets Livewire execute and
  inject the chapter list into the DOM before we scrape it.
* :class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`
  provides the standard Madara routes for search, manga details and reader
  pages, which remain compatible with the site’s front-end.

``get_chapters`` is overridden because the Madara mixin’s default
selector (``.wp-manga-chapter a``) does not match Livewire’s output.  The
override uses the selector observed in the wild:
``li[wire\\:key^="comic-chapter-list"] > a``.

Route patterns (observed while the site was active)
---------------------------------------------------
* Search           — Livewire-driven; handled by the Madara mixin when
  rendered.
* Manga details    — ``GET  /comics/{slug}``
* Chapter list     — Livewire-paginated; collected from the rendered DOM.
* Reader           — ``GET  /comics/{slug}/chapters/{chapter_slug}``
  Images live inside ``img.max-w-full`` and are lazy-loaded via
  ``data-src``, ``data-lazy-src`` or ``data-cfsrc``.

Adult content
-------------
Reaper Scans was a general-audience aggregator.  ``adult`` is therefore
``False``; adult filtering is governed by ``Settings.content.allow_adult``
at the session level, exactly like every other parser.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  If the site ever returns under a different domain, only
``sites.yaml`` needs updating — this file stays untouched.
"""
from __future__ import annotations

import re
from typing import AsyncIterator

from selectolax.parser import HTMLParser

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.search import SearchResult
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.js_rendered import JsRenderedMixin
from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

__all__ = ["ReaperScansParser"]


class ReaperScansParser(
    JsRenderedMixin,
    CloudflareMixin,
    WordPressMadaraMixin,
    BaseParser,
):
    """Parser for Reaper Scans (Madara + Livewire + Cloudflare, English)."""

    site_id: str = "reaperscans"
    language: str = "en"
    adult: bool = False

    # ------------------------------------------------------------------ #
    #  search                                                            #
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator[SearchResult]:
        """Search Reaper Scans and yield results page by page.

        The site’s search is Livewire-driven: the results grid is rendered
        client-side after the page loads.  Because this parser inherits
        :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`, the
        page is rendered through Playwright and the results are present in
        the resulting DOM.  The standard Madara selector
        (``.c-tabs-item__content``) is used to extract the cards.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        # Delegate to the Madara mixin’s search implementation.  It builds
        # the Madara search URL, calls ``self.session.get_html`` (which is
        # intercepted by JsRenderedMixin and rendered via Playwright), and
        # parses the standard Madara result cards.
        async for result in super().search(query, page=page):
            yield result

    # ------------------------------------------------------------------ #
    #  get_chapters                                                      #
    # ------------------------------------------------------------------ #
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the complete ordered chapter list for *manga*.

        Reaper Scans rendered chapters via Livewire, not through the
        standard Madara ``/ajax/chapters/`` endpoint.  The rendered DOM
        contains one anchor per chapter inside
        ``li[wire\\:key^="comic-chapter-list"]``.  This override collects
        every such anchor, deduplicates by chapter id, and returns the list
        in chronological order.

        The Livewire pagination (“next page”) is handled automatically by
        Playwright during rendering: by the time
        :meth:`_get_rendered_html` returns, the fully paginated chapter
        list is present in the DOM.
        """
        html = await self._get_rendered_html(manga.url)
        tree = HTMLParser(html)

        chapters: dict[str, Chapter] = {}
        for link in tree.css('li[wire\\:key^="comic-chapter-list"] > a'):
            href = link.attributes.get("href", "")
            if not href:
                continue

            chapter_id = self._extract_chapter_id(href)
            if not chapter_id or chapter_id in chapters:
                continue

            # The anchor contains two <p> elements: the chapter title and
            # the release date.  The first one is the human-readable label.
            p_nodes = link.css("p")
            label = p_nodes[0].text(strip=True) if p_nodes else chapter_id

            chapters[chapter_id] = Chapter(
                id=self._make_chapter_id(manga.id, href),
                manga_id=manga.id,
                site_id=self.site_id,
                title=label,
                url=self._resolve_url(href),
                number=self._parse_chapter_number(label),
                language=self.language,
            )

        # Reaper Scans listed chapters newest-first; reverse to
        # chronological order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  helpers                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_chapter_number(label: str) -> float | None:
        """Extract a numeric chapter value from a free-form label.

        Reaper Scans chapter labels look like ``"Chapter 48"`` or simply
        ``"48"``; both forms are handled.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("reaperscans", ReaperScansParser)
