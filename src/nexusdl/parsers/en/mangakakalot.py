"""Parser for MangaKakalot — English manga/manhwa aggregator (Madara-like + Cloudflare).

MangaKakalot (also reachable as Manganelo, Manganato, Mangabat) runs on a
custom WordPress theme that shares most of its markup conventions with
**Madara** but diverges in a few critical ways.  More importantly, the site
is fronted by **Cloudflare** and has a history of aggressive anti-scraping
measures (JavaScript challenges, image hot-link protection, intermittent
chapter-list obfuscation).

To keep this parser robust and aligned with the project's one-file philosophy,
it combines two mixins:

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge
  is detected, then replays the original request with the obtained cookies.
* :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` renders pages
  through the shared Playwright pool when the site delivers a JavaScript-
  only shell.  This is the only reliable way to obtain the chapter list and
  reader pages on a site that has already broken pure-HTTP scrapers.

Route patterns (observed on ``mangakakalot.com`` / ``mangakakalot.gg`` /
``manganelo.com``)
-------------------------------------------------------------------------------
* Search           — ``GET  /search/story/{query}?page={page}``
* Manga details    — ``GET  /manga/{slug}``
* Chapter list     — embedded in the manga page as
  ``.chapter-list > li > a`` (the classic Manganelo layout) and, on newer
  themes, as ``ul.row-content-chapter li a``.
* Reader           — ``GET  /chapter/{manga_slug}/{chapter_slug}``
  Images live inside ``.container-chapter-reader > img`` and are lazy-loaded
  via ``data-src``.

Adult content
-------------
MangaKakalot is a general-purpose aggregator that indexes both general and
mature series.  The parser is declared ``adult = False`` because the *site*
itself is not adult-only.  When ``Settings.content.allow_adult`` is enabled,
the session layer applies the site's mature-content cookie centrally, so
adult chapters become reachable without any site-specific logic in this file.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  MangaKakalot has migrated across ``.com``, ``.gg`` and
``.fun`` over the years; if it moves again, only ``sites.yaml`` needs updating
— this file stays untouched.
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

__all__ = ["MangaKakalotParser"]

# The chapter list is sometimes paginated via “load more” on the manga page.
# We cap the number of pages to avoid unbounded loops on a broken response.
_MAX_CHAPTER_PAGES = 100


class MangaKakalotParser(JsRenderedMixin, CloudflareMixin, BaseParser):
    """Parser for MangaKakalot (Madara-like + Cloudflare, English)."""

    site_id: str = "mangakakalot"
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
        """Search MangaKakalot and yield results page by page.

        The search route accepts the query as a path segment and a ``page``
        query parameter.  Results are rendered as ``.story_item`` cards in
        the classic Manganelo layout; newer themes use ``.search-story-item``.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        url = f"{self._base_url}/search/story/{quote(query)}?page={page}"
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        # The two selectors cover the classic and the refreshed themes.
        for card in tree.css(".story_item, .search-story-item, .list-story-item"):
            link = card.css_first("a[href*='/manga/']")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title_node = card.css_first(".story_name, .item-title, h3")
            title = title_node.text(strip=True) if title_node is not None else ""

            cover_node = card.css_first("img")
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
        """Return the :class:`Manga` for a MangaKakalot URL or slug.

        The manga page exposes the title in ``.manga-info-text h1`` (classic
        layout) or ``h1`` (refreshed layout), the cover in
        ``.manga-info-pic img`` / ``.manga-cover img`` and the metadata in a
        ``ul.manga-info-text`` block.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare slug (as stored in the local library).
        """
        url = self._resolve_url(url_or_id)
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        # Title — classic layout wraps it in .manga-info-text h1; newer
        # layouts use a plain h1 inside the hero block.
        title_node = tree.css_first(".manga-info-text h1, h1")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        # Cover — classic layout uses .manga-info-pic img, refreshed layout
        # uses .manga-cover img or a generic .cover img.
        cover_node = tree.css_first(".manga-info-pic img, .manga-cover img, .cover img")
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        # Tags / genres — rendered as anchors inside the metadata block.
        tags: list[str] = []
        for tag_node in tree.css(".manga-info-text a[href*='/genre/'], .manga-info-text a[href*='/genres/']"):
            text = tag_node.text(strip=True)
            if text:
                tags.append(text)

        # Status — MangaKakalot exposes it as text in the metadata block.
        status: str | None = None
        status_node = tree.css_first(".manga-info-text .status, .manga-info-text li:contains('Status')")
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

        MangaKakalot embeds the chapter list directly in the manga page.
        Classic Manganelo layout uses ``.chapter-list > li > a``; the
        refreshed theme uses ``ul.row-content-chapter li a``.  The list may
        be paginated via a “load more” control on the same page; when that
        control is present, we follow it through the rendered DOM (Playwright
        executes the pagination JavaScript automatically).

        Chapters are listed newest-first in the DOM, so the final list is
        reversed to chronological order before being returned.
        """
        chapters: dict[str, Chapter] = {}

        html = await self._get_rendered_html(manga.url)
        tree = HTMLParser(html)

        # Collect every chapter anchor from both layouts.
        for link in tree.css(
            ".chapter-list > li > a, "
            "ul.row-content-chapter li a, "
            ".chapter-list a[href*='/chapter/']"
        ):
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

        # If the “load more” control exists, we trigger it once and re-parse.
        # In practice Playwright has already executed the pagination by the
        # time _get_rendered_html returns, so no extra click is required.
        # The loop below is a defensive fallback for themes that still
        # paginate server-side via a “next page” link on the manga page.
        next_link = tree.css_first("a.next_page, a[rel='next']")
        page_count = 1
        while next_link is not None and page_count < _MAX_CHAPTER_PAGES:
            next_href = next_link.attributes.get("href")
            if not next_href:
                break

            next_html = await self._get_rendered_html(self._resolve_url(next_href))
            next_tree = HTMLParser(next_html)

            for link in next_tree.css(
                ".chapter-list > li > a, "
                "ul.row-content-chapter li a, "
                ".chapter-list a[href*='/chapter/']"
            ):
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

            next_link = next_tree.css_first("a.next_page, a[rel='next']")
            page_count += 1

        # MangaKakalot lists chapters newest-first; reverse to chronological
        # order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page renders images inside
        ``.container-chapter-reader > img``.  Images are lazy-loaded via
        ``data-src``, and the site applies an anti-hot-link check that
        requires a ``Referer`` header matching the chapter URL — the shared
        :class:`~nexusdl.core.session.http_session.HttpSession` sets that
        header automatically for every download initiated through
        :meth:`BaseParser.download_page`.
        """
        html = await self._get_rendered_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        for index, img in enumerate(tree.css(".container-chapter-reader > img, .chapter-reader img, .reading-content img")):
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

        MangaKakalot labels look like ``"Chapter 48"``, ``"Vol. 3 Chapter
        12"`` or simply ``"48"``; the first numeric token is used.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("mangakakalot", MangaKakalotParser)
