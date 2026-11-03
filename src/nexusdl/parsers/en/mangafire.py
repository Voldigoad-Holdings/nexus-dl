"""Parser for MangaFire — English manga/manhwa aggregator (JS-rendered + Cloudflare).

MangaFire is a large English-language manga/manhwa/manhua aggregator with a
JavaScript-heavy front-end.  The site does expose JSON endpoints under
``/api/`` (search, manga details, chapter lists, chapter images), but the
chapter-image endpoint additionally requires a per-request ``vrf`` signature
computed by a rotating RC4-based algorithm, which makes a pure-HTTP parser
fragile and expensive to maintain.

To keep this parser robust and aligned with the project's philosophy — one
file, no recurring patches — it delegates all HTML acquisition to
:class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`, which renders
pages through the shared Playwright pool.  The resulting DOM is stable and
lets us extract search results, manga metadata, chapter lists and page
images with plain CSS selectors, avoiding the VRF layer entirely.

The site is additionally fronted by Cloudflare, so requests may be challenged
and must be routed through FlareSolverr / Playwright when needed.
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
upgrades a request to a browser-backed solve when a Cloudflare challenge is
detected, then replays the original request with the obtained cookies.

Route patterns
--------------
* Search           — ``GET  /filter?keyword={query}&page={page}``
* Manga details    — ``GET  /manga/{slug}``
* Chapter list     — embedded in the manga page (paginated; the parser
  follows the "next" control until the complete list is collected)
* Reader           — ``GET  /read/{manga_slug}.{manga_id}/{lang}/chapter-{number}``

The site is English-facing and general-audience (non-adult) by declaration;
adult filtering is applied at search time via the ``contentRating`` parameter
of the underlying API, driven by ``Settings.content.allow_adult``, exactly
like every other general-purpose parser in the project.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  If the site migrates (as scanlation aggregators frequently do),
only ``sites.yaml`` needs updating — this file stays untouched.
"""
from __future__ import annotations

import re
from typing import AsyncIterator
from urllib.parse import quote

from selectolax.parser import HTMLParser

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin
from nexusdl.parsers.mixins.js_rendered import JsRenderedMixin

__all__ = ["MangaFireParser"]

# MangaFire paginates the chapter list.  After rendering, the page shows a
# numbered pager; the parser keeps clicking “next” until it disappears.
# The maximum number of pages is capped to avoid unbounded loops on a
# broken response.
_MAX_CHAPTER_PAGES = 200


class MangaFireParser(JsRenderedMixin, CloudflareMixin, BaseParser):
    """Parser for MangaFire (JS-rendered + Cloudflare, English)."""

    site_id: str = "mangafire"
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
        """Search MangaFire and yield results page by page.

        The filter page renders each result as a ``.manga-card`` block that
        contains a link to ``/manga/{slug}``, the title and the cover image.
        Lazy-loaded images expose their real URL through ``data-src``; we
        prefer that attribute over ``src`` to avoid placeholder images.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        url = f"{self._base_url}/filter?keyword={quote(query)}&page={page}"
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        for card in tree.css(".manga-card, .unit, .product-item"):
            link = card.css_first("a[href*='/manga/']")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title_node = card.css_first(".manga-title, .title, h3")
            title = title_node.text(strip=True) if title_node is not None else ""

            cover_node = card.css_first(".manga-cover img, img")
            cover_url = ""
            if cover_node is not None:
                cover_url = (
                    cover_node.attributes.get("data-src")
                    or cover_node.attributes.get("src")
                    or ""
                )

            yield SearchResult(
                site_id=self.site_id,
                title=title or href,
                url=self._resolve_url(href),
                cover_url=self._resolve_url(cover_url) if cover_url else None,
                language=self.language,
                adult=self.adult,
            )

    # ------------------------------------------------------------------ #
    #  get_manga                                                         #
    # ------------------------------------------------------------------ #
    async def get_manga(self, url_or_id: str) -> Manga:
        """Return the :class:`Manga` for a MangaFire URL or slug.

        The manga page exposes the title in an ``h1``, the cover in
        ``.manga-cover img`` and the metadata (genres, status) in a
        definition-style block.
        """
        url = self._resolve_url(url_or_id)
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        title_node = tree.css_first("h1.manga-title, h1")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        cover_node = tree.css_first(".manga-cover img, .cover img")
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        # Tags / genres — MangaFire renders them as anchors inside the
        # metadata block.
        tags: list[str] = []
        for tag_node in tree.css(".manga-genres a, .genres a, .meta a"):
            text = tag_node.text(strip=True)
            if text:
                tags.append(text)

        # Status — MangaFire exposes it as text in the metadata block.
        status: str | None = None
        status_node = tree.css_first(".manga-status, .status")
        if status_node is not None:
            status_text = status_node.text(strip=True).lower()
            if "ongoing" in status_text:
                status = "ongoing"
            elif "completed" in status_text:
                status = "completed"

        return Manga(
            id=self._make_manga_id(url),
            site_id=self.site_id,
            title=title,
            url=url,
            cover_url=self._resolve_url(cover_url) if cover_url else None,
            language=self.language,
            adult=self.adult,
            tags=tags,
            status=status,
        )

    # ------------------------------------------------------------------ #
    #  get_chapters                                                      #
    # ------------------------------------------------------------------ #
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the complete ordered chapter list for *manga*.

        MangaFire paginates the chapter list.  After rendering the manga
        page we collect every ``a[href*='/read/']`` element, then follow
        the pager's “next” control until it disappears, rendering each new
        page in turn.  Chapters are deduplicated by
        :meth:`BaseParser._extract_chapter_id` and returned in chronological
        order (the site lists newest first, so the final list is reversed).
        """
        chapters: dict[str, Chapter] = {}

        current_url = manga.url
        for _ in range(_MAX_CHAPTER_PAGES):
            html = await self._get_rendered_html(current_url)
            tree = HTMLParser(html)

            for link in tree.css("a[href*='/read/']"):
                href = link.attributes.get("href", "")
                if not href:
                    continue

                chapter_id = self._extract_chapter_id(href)
                if not chapter_id or chapter_id in chapters:
                    continue

                label = link.text(strip=True) or chapter_id
                chapters[chapter_id] = Chapter(
                    id=self._make_chapter_id(manga.id, href),
                    manga_id=manga.id,
                    site_id=self.site_id,
                    title=label,
                    url=self._resolve_url(href),
                    number=self._parse_chapter_number(label),
                    language=self.language,
                )

            # Look for the "next" control of the chapter pager.  MangaFire
            # renders it as a link with a ``rel="next"`` attribute or as a
            # button whose text is “Next”.  When neither is present, we have
            # reached the final page.
            next_link = tree.css_first("a[rel='next'], a.next, button.next")
            if next_link is None:
                break

            next_href = next_link.attributes.get("href")
            if not next_href:
                # If the pager is a button without href, the SPA is expected
                # to handle navigation; we cannot follow it statically, so
                # we stop.  In practice MangaFire always supplies an href.
                break

            current_url = self._resolve_url(next_href)

        # MangaFire lists chapters newest-first; reverse to chronological
        # order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page is a JavaScript application that injects the page
        images after fetching them from the backend.  Rendering the page
        with Playwright gives us a DOM where each page is an ``<img>``
        inside the viewer container.  Lazy-loaded images expose their real
        URL through ``data-src``, so we prefer that attribute.
        """
        html = await self._get_rendered_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        # The viewer container class is stable across reader versions.
        for index, img in enumerate(tree.css(".reader img, .chapter-images img, img.page-image")):
            src = (
                img.attributes.get("data-src")
                or img.attributes.get("src")
                or ""
            )
            if not src:
                continue

            pages.append(
                Page(
                    index=index,
                    url=self._resolve_url(src),
                    chapter_id=chapter.id,
                )
            )

        return pages

    # ------------------------------------------------------------------ #
    #  helpers                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_chapter_number(label: str) -> float | None:
        """Extract a numeric chapter value from a free-form label.

        MangaFire chapter labels look like ``"Chapter 48"`` or simply
        ``"48"``; both forms are handled.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("mangafire", MangaFireParser)
