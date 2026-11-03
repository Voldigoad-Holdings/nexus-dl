"""Parser for Poseidon Scans — French manga/manhwa/manhua/webtoon aggregator.

.. warning::
   Poseidon Scans has migrated **away from WordPress**.  The site was
   originally a Madara-based platform at ``poseidonscans.fr`` (theme
   declared as ``madara`` with ``isNsfw = true`` in the Keiyoushi/Mihon
   extension registry), but in April 2025 an upstream issue reported that
   ``poseidonscans.fr`` no longer uses WordPress and that its UI and
   developer are now the same as ``ortegascans.fr``.  The Komikku/Mihon
   source is currently **disabled** for this reason.

   The site has also churned through several domains
   (``poseidonscans.fr`` → ``poseidon-scans.com`` → ``poseidon-scans.co``
   → ``poseidon-scans.net``).  The exact theme in use today could not be
   verified during the audit, so this parser is deliberately written as a
   **universal** reader that auto-adapts to the layouts most commonly seen
   on French scanlation platforms.

Architecture
------------
Because the site is now a **JavaScript-rendered SPA** (the same stack as
``ortegascans.fr``, which uses a ``/serie/{slug}/`` catalogue layout), a
pure-HTTP scraper cannot obtain the chapter list or reader pages reliably.
The parser therefore combines two mixins:

* :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` renders pages
  through the shared Playwright pool, which executes the SPA and injects the
  chapter list and reader images into the DOM before scraping.
* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge
  is detected, then replays the original request with the obtained cookies.

The four operations (``search``, ``get_manga``, ``get_chapters``,
``get_pages``) are implemented directly with **multi-selector fallbacks**
covering:

* the ``ortegascans.fr``-style catalogue layout (``/serie/{slug}/``),
* the legacy Madara layout (``/manga/{slug}/``), which some mirrors still
  serve,
* the MangaThemesia layout,
* and a set of generic selectors.

Each operation tries the catalogue selectors first, then the Madara
selectors, then the generic ones.  The first match wins.  This makes the
parser resilient to the theme changes that have historically broken
third-party scrapers for this site.

Route patterns
--------------
* Search           — ``/search?q={query}``             (SPA)
                     ``/?s={query}&post_type=wp-manga`` (legacy Madara)
* Manga details    — ``/serie/{slug}/``                 (catalogue)
                     ``/manga/{slug}/``                 (legacy Madara)
* Chapter list     — embedded in the manga page         (all layouts)
* Reader           — ``/serie/{slug}/{chapter-slug}/``  (catalogue)
                     ``/manga/{slug}/{chapter-slug}/``  (legacy Madara)

The site is French-language and **adult** (NSFW), declared on the class so
the registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  Poseidon Scans has migrated across several domains and
may migrate again; if it does, only ``sites.yaml`` needs updating — this
file stays untouched.
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

__all__ = ["PoseidonScansParser"]

# ---------------------------------------------------------------------------
# Selector sets — tried in order.  The first set that yields a non-empty
# result wins.  This is what makes the parser resilient to the theme
# changes that have historically broken Poseidon Scans scrapers.
# ---------------------------------------------------------------------------

# Search result cards
_SEARCH_CARD_SELECTORS: tuple[str, ...] = (
    # Catalogue (ortegascans-style SPA)
    "div.serie-card",
    "div.manga-card",
    "div.comic-card",
    "div.card",
    "article.serie",
    # Legacy Madara
    "div.c-tabs-item__content",
    "div.page-item-detail",
    # MangaThemesia
    "div.bsx",
    "div.bs",
    # Generic
    "div.manga-item",
    "div.comic-item",
    "div.search-result",
    "article.manga",
)

# Anchor inside a search card that points to the manga
_SEARCH_LINK_SELECTORS: tuple[str, ...] = (
    "a[href*='/serie/']",
    "a[href*='/manga/']",
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
    ".serie-title",
    ".manga-title",
    ".card-title",
    ".post-title",
    ".title",
)

# Cover inside a search card
_SEARCH_COVER_SELECTORS: tuple[str, ...] = (
    "img",
    "div.c-tabs-item__content img",
)

# Manga page title
_MANGA_TITLE_SELECTORS: tuple[str, ...] = (
    "h1.serie-title",
    "h1.manga-title",
    "div.post-title h1",
    "h1.entry-title",
    "h1",
)

# Manga page cover
_MANGA_COVER_SELECTORS: tuple[str, ...] = (
    "div.serie-cover img",
    "div.manga-cover img",
    "div.summary_image img",
    "div.series-cover img",
    "div.cover img",
    "article img",
)

# Manga page tags / genres
_MANGA_TAG_SELECTORS: tuple[str, ...] = (
    "div.serie-genres a",
    "div.genres a",
    "div.manga-genres a",
    "div.genres-content a",
    "a[href*='/genre/']",
    "a[href*='/manga-genre/']",
)

# Manga page status
_MANGA_STATUS_SELECTORS: tuple[str, ...] = (
    "div.serie-status",
    "div.post-status div.summary-content",
    "div.manga-status",
    "div.status",
    ".post-content_item .summary-content",
)

# Chapter list anchors
_CHAPTER_SELECTORS: tuple[str, ...] = (
    # Catalogue (ortegascans-style SPA)
    "a[href*='/serie/'][href*='/chapter-']",
    "a[href*='/serie/'][href*='-chapitre-']",
    "div.chapter-list a",
    "div.chapters a",
    # Legacy Madara
    "li.wp-manga-chapter a",
    "div.listing-chapters_wrap a",
    # MangaThemesia
    "div.eplister li a",
    # Generic
    "a[href*='/chapter/']",
    "a[href*='/chapitre/']",
    "a[href*='-chapter-']",
    "a[href*='-chapitre-']",
    "a.chapter-link",
)

# Reader page images
_PAGE_SELECTORS: tuple[str, ...] = (
    # Catalogue (ortegascans-style SPA)
    "div.reader-content img",
    "div.chapter-content img",
    "div.chapter-images img",
    # Legacy Madara
    "div.reading-content img",
    "div.page-break img",
    # MangaThemesia
    "div#chapter-content img",
    # Generic
    "div.reader img",
    "div.chapter-reader img",
    "article img",
)


class PoseidonScansParser(JsRenderedMixin, CloudflareMixin, BaseParser):
    """Universal parser for Poseidon Scans (French, adult, JS-rendered)."""

    site_id: str = "poseidon_scans"
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
        """Search Poseidon Scans and yield results page by page.

        The parser tries the SPA search route first
        (``/search?q={query}``), then falls back to the legacy Madara
        route (``/?s={query}&post_type=wp-manga``).  Each result card is
        matched against a list of known selectors; the first card selector
        that yields matches wins.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        # SPA search first, then legacy Madara search.
        urls = (
            f"{self._base_url}/search?q={quote(query)}&page={page}",
            f"{self._base_url}/?s={quote(query)}&post_type=wp-manga&page={page}",
            f"{self._base_url}/?s={quote(query)}&page={page}",
        )

        cards = []
        for url in urls:
            html = await self._get_rendered_html(url)
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
        """Return the :class:`Manga` for a Poseidon Scans URL or slug.

        The manga page title, cover, genres and status are each extracted
        through a selector chain that covers the catalogue layout, the
        legacy Madara layout and the common custom layouts.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare slug (as stored in the local library).
        """
        url = self._resolve_url(url_or_id)
        html = await self._get_rendered_html(url)
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

        The chapter list is embedded directly in the manga page on all
        supported layouts, but it is injected by JavaScript on the SPA
        catalogue.  Because the parser inherits
        :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`, the
        page is rendered through Playwright and the anchors are present in
        the resulting DOM.  The parser tries every chapter selector in turn
        and collects all matching anchors from the first selector that
        yields results.  Chapters are deduplicated by
        :meth:`BaseParser._extract_chapter_id` and returned in chronological
        order (the site lists newest first, so the final list is reversed).
        """
        html = await self._get_rendered_html(manga.url)
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

        # Poseidon Scans lists chapters newest-first; reverse to
        # chronological order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page images are extracted through a selector chain that
        covers the catalogue layout, the legacy Madara layout and the
        common custom layouts.  Images are lazy-loaded on every layout, so
        ``data-src`` is preferred over ``src`` to avoid downloading
        placeholder images.

        Because the reader is JavaScript-rendered,
        :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`
        produces the final DOM before extraction.  Any Cloudflare challenge
        on the reader page is handled by
        :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`.
        """
        html = await self._get_rendered_html(chapter.url)
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

        Poseidon Scans labels may be French (``"Chapitre 48"``) or English
        (``"Chapter 48"``); the first numeric token is used in both cases.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("poseidon_scans", PoseidonScansParser)
