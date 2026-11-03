"""Parser for Sushiscan — French manga scanlation aggregator.

Sushiscan (``sushiscan.fr``, with mirror domains ``sushiscan.net`` and
``sushiscan.st``) is one of the largest French-language manga scanlation
platforms.  It runs on the **MangaReader** WordPress theme (version 2.1.8,
published by Themesia Studio), which is **not** Madara and **not**
MangaThemesia, although it shares many markup conventions with them.

The site is fronted by **Cloudflare**, and its anti-bot measures are
aggressive enough that public scrapers (e.g. ``SushiscanScraper``) have
historically failed to bypass them without FlareSolverr.  This parser
therefore inherits from
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`, which
transparently upgrades a request to a browser-backed solve when a
Cloudflare challenge is detected, then replays the original request with
the obtained cookies.

Because the exact MangaReader markup is not publicly documented and varies
slightly across versions (2.1.x vs 2.2.x), the four operations
(``search``, ``get_manga``, ``get_chapters``, ``get_pages``) are implemented
with **multi-selector fallbacks** covering:

* the MangaReader conventions observed on the live site,
* the Madara conventions (which MangaReader borrows from),
* the MangaThemesia conventions,
* and a set of generic selectors.

This makes the parser resilient to minor theme updates without requiring a
rewrite — exactly the design goal of this project.

Route patterns
--------------
* Catalogue index    — ``GET  /catalogue/``
* Series page        — ``GET  /catalogue/{slug}/``
* Chapter reader     — ``GET  /catalogue/{slug}/{chapter-slug}/``
  (MangaReader also accepts ``/lecture/{slug}/{chapter-slug}/`` on some
  installations; both are handled by following the anchors present on the
  series page.)
* Search             — ``GET  /?s={query}&post_type=wp-manga`` (Madara-style)
  or ``GET  /?s={query}`` (generic WordPress search)

The site is French-language and **adult** (NSFW), declared on the class so
the registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  Sushiscan operates under several mirror domains
(``sushiscan.fr``, ``sushiscan.net``, ``sushiscan.st``); if the primary
domain changes, only ``sites.yaml`` needs updating — this file stays
untouched.
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

__all__ = ["SushiScanParser"]

# ---------------------------------------------------------------------------
# Selector sets — tried in order.  The first set that yields a non-empty
# result wins.  This is what makes the parser theme-version-agnostic.
# ---------------------------------------------------------------------------

# Search result cards
_SEARCH_CARD_SELECTORS: tuple[str, ...] = (
    # MangaReader (observed)
    "div.c-tabs-item__content",
    "div.page-item-detail",
    "div.manga-item",
    "div.comic-item",
    "div.bsx",
    "div.bs",
    "article.manga",
    "div.search-result",
    # Madara / MangaThemesia fallbacks
    "div.row.c-tabs-item",
    "div.item",
)

# Anchor inside a search card that points to the manga
_SEARCH_LINK_SELECTORS: tuple[str, ...] = (
    "a[href*='/catalogue/']",
    "a[href*='/manga/']",
    "a[href*='/serie/']",
    "a[href*='/series/']",
    "a[href*='/comic/']",
    "a",
)

# Title inside a search card
_SEARCH_TITLE_SELECTORS: tuple[str, ...] = (
    "h3 a",
    "h4 a",
    "h3",
    "h4",
    ".post-title",
    ".manga-title",
    ".title",
    ".entry-title",
)

# Cover inside a search card
_SEARCH_COVER_SELECTORS: tuple[str, ...] = (
    "img",
    "div.c-tabs-item__content img",
)

# Manga page title
_MANGA_TITLE_SELECTORS: tuple[str, ...] = (
    "div.post-title h1",
    "h1.manga-title",
    "h1.entry-title",
    "h1",
)

# Manga page cover
_MANGA_COVER_SELECTORS: tuple[str, ...] = (
    "div.summary_image img",
    "div.manga-cover img",
    "div.series-cover img",
    "div.cover img",
    "article img",
)

# Manga page tags / genres
_MANGA_TAG_SELECTORS: tuple[str, ...] = (
    "div.genres a",
    "div.manga-genres a",
    "div.genres-content a",
    "a[href*='/manga-genres/']",
    "a[href*='/genre/']",
)

# Manga page status
_MANGA_STATUS_SELECTORS: tuple[str, ...] = (
    "div.post-status div.summary-content",
    "div.manga-status",
    "div.status",
    ".post-content_item .summary-content",
)

# Chapter list anchors
_CHAPTER_SELECTORS: tuple[str, ...] = (
    # MangaReader
    "li.wp-manga-chapter a",
    "div.listing-chapters_wrap a",
    "div.eplister li a",
    "div.chapter-list a",
    # Generic / Madara
    "a[href*='/catalogue/'][href*='/chapitre-']",
    "a[href*='/catalogue/'][href*='-chapter-']",
    "a[href*='/lecture/']",
    "a[href*='/chapter/']",
    "a[href*='/chapitre/']",
    "a.chapter-link",
)

# Reader page images
_PAGE_SELECTORS: tuple[str, ...] = (
    # MangaReader / Madara / MangaThemesia
    "div.reading-content img",
    "div.page-break img",
    "div#chapter-content img",
    "div.chapter-content img",
    "div.reader-content img",
    "div.chapter-images img",
    "div.reader img",
    "div.chapter-reader img",
    "article img",
)


class SushiScanParser(CloudflareMixin, BaseParser):
    """Parser for Sushiscan (MangaReader + Cloudflare, adult, French)."""

    site_id: str = "sushiscan"
    language: str = "fr"
    adult: bool = True

    # ------------------------------------------------------------------ #
    #  search                                                            #
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator[SearchResult]:
        """Search Sushiscan and yield results page by page.

        The parser tries the Madara-style search route first
        (``/?s={query}&post_type=wp-manga``), then falls back to the
        generic WordPress search route (``/?s={query}``).  Each result
        card is matched against a list of known selectors; the first card
        selector that yields matches wins.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        # Madara-style search URL first, then generic WordPress search.
        urls = (
            f"{self._base_url}/?s={quote(query)}&post_type=wp-manga&page={page}",
            f"{self._base_url}/?s={quote(query)}&page={page}",
        )

        cards = []
        for url in urls:
            html = await self.session.get_html(url)
            tree = HTMLParser(html)
            for selector in _SEARCH_CARD_SELECTORS:
                cards = tree.css(selector)
                if cards:
                    break
            if cards:
                break

        for card in cards:
            link = self._first_anchor(card, _SEARCH_LINK_SELECTORS)
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title = self._first_text(card, _SEARCH_TITLE_SELECTORS) or href
            cover_url = self._first_img_src(card, _SEARCH_COVER_SELECTORS)

            yield SearchResult(
                site_id=self.site_id,
                title=title,
                url=self._resolve_url(href),
                cover_url=self._resolve_url(cover_url) if cover_url else None,
                language=self.language,
                adult=self.adult,
            )

    # ------------------------------------------------------------------ #
    #  get_manga                                                         #
    # ------------------------------------------------------------------ #
    async def get_manga(self, url_or_id: str) -> Manga:
        """Return the :class:`Manga` for a Sushiscan URL or slug.

        The manga page title, cover, genres and status are each extracted
        through a selector chain that covers MangaReader, Madara and the
        common custom layouts.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare slug (as stored in the local library).
        """
        url = self._resolve_url(url_or_id)
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        title = (
            self._first_text(tree, _MANGA_TITLE_SELECTORS)
            or self._extract_id(url)
        )
        cover_url = self._first_img_src(tree, _MANGA_COVER_SELECTORS)

        tags: list[str] = []
        for selector in _MANGA_TAG_SELECTORS:
            nodes = tree.css(selector)
            if nodes:
                tags = [n.text(strip=True) for n in nodes if n.text(strip=True)]
                break

        status: str | None = None
        for selector in _MANGA_STATUS_SELECTORS:
            node = tree.css_first(selector)
            if node is None:
                continue
            text = node.text(strip=True).lower()
            if "ongoing" in text or "en cours" in text:
                status = "ongoing"
                break
            if "completed" in text or "terminé" in text:
                status = "completed"
                break
            if "hiatus" in text or "pause" in text:
                status = "hiatus"
                break
            if "cancelled" in text or "annulé" in text:
                status = "cancelled"
                break

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

        The chapter list is embedded directly in the series page on
        MangaReader.  The parser tries every chapter selector in turn and
        collects all matching anchors from the first selector that yields
        results.  Chapters are deduplicated by
        :meth:`BaseParser._extract_chapter_id` and returned in chronological
        order (the site lists newest first, so the final list is reversed).
        """
        html = await self.session.get_html(manga.url)
        tree = HTMLParser(html)

        anchors = []
        for selector in _CHAPTER_SELECTORS:
            anchors = tree.css(selector)
            if anchors:
                break

        chapters: dict[str, Chapter] = {}
        for link in anchors:
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

        # Sushiscan lists chapters newest-first; reverse to chronological
        # order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page images are extracted through a selector chain that
        covers MangaReader, Madara and the common custom layouts.  Images
        are lazy-loaded on every theme, so ``data-src`` is preferred over
        ``src`` to avoid downloading placeholder images.
        """
        html = await self.session.get_html(chapter.url)
        tree = HTMLParser(html)

        images = []
        for selector in _PAGE_SELECTORS:
            images = tree.css(selector)
            if images:
                break

        pages: list[Page] = []
        for img in images:
            src = (
                img.attributes.get("data-src")
                or img.attributes.get("src")
                or ""
            )
            if not src:
                continue

            pages.append(
                Page(
                    index=len(pages),
                    url=self._resolve_url(src),
                    chapter_id=chapter.id,
                )
            )

        return pages

    # ------------------------------------------------------------------ #
    #  helpers                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _first_anchor(
        node: HTMLParser | object,
        selectors: tuple[str, ...],
    ) -> object | None:
        """Return the first anchor matching any selector in *selectors*.

        ``node`` may be an :class:`HTMLParser` (whole tree) or a single
        :class:`selectolax.parser.Node` (card).  The helper works for both
        because both expose ``css_first``.
        """
        for selector in selectors:
            anchor = node.css_first(selector)  # type: ignore[union-attr]
            if anchor is not None:
                return anchor
        return None

    @staticmethod
    def _first_text(
        node: HTMLParser | object,
        selectors: tuple[str, ...],
    ) -> str:
        """Return the text of the first node matching any selector."""
        for selector in selectors:
            found = node.css_first(selector)  # type: ignore[union-attr]
            if found is not None:
                text = found.text(strip=True)
                if text:
                    return text
        return ""

    @staticmethod
    def _first_img_src(
        node: HTMLParser | object,
        selectors: tuple[str, ...],
    ) -> str:
        """Return the ``data-src`` / ``src`` of the first matching image."""
        for selector in selectors:
            img = node.css_first(selector)  # type: ignore[union-attr]
            if img is None:
                continue
            src = (
                img.attributes.get("data-src")
                or img.attributes.get("src")
                or ""
            )
            if src:
                return src
        return ""

    @staticmethod
    def _parse_chapter_number(label: str) -> float | None:
        """Extract a numeric chapter value from a free-form label.

        Sushiscan labels may be French (``"Chapitre 48"``) or English
        (``"Chapter 48"``); the first numeric token is used in both cases.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("sushiscan", SushiScanParser)
