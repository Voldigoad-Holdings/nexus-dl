"""Parser for ManhwaClub — English manhwa/webtoon aggregator.

ManhwaClub (``manhwaclub.net``) is an English-language aggregator that
publishes Korean manhwa and webtoon chapters translated from Korean.  It is
grouped under ``parsers/kr`` because the content originates from Korean
manhwa, even though the site UI and translations are English.

The site is fronted by **Cloudflare** — its DNS records point to
``bayan.ns.cloudflare.com`` and ``dns.cloudflare.com``, and it exposes a
valid ``sitemap.xml`` at ``/sitemap.xml`` as confirmed by Cloudflare Radar
scan.  A plain HTTP client is therefore regularly challenged and must be
routed through FlareSolverr / Playwright when needed.

Route patterns (observed via Cloudflare Radar scans and public URL indexes)
---------------------------------------------------------------------------
* Search           — ``GET  /?s={query}``               (WordPress-style)
                     ``GET  /search?q={query}``          (SPA-style)
* Manga details    — ``GET  /manga/{slug}/``            (confirmed pattern:
  ``https://manhwaclub.net/manga/smoking-hypnosis/``)
* Chapter list     — embedded in the manga page
* Reader           — ``GET  /manga/{slug}/{chapter-slug}/``

Because the exact theme in use today could not be fully verified (the public
domain was not reachable in its entirety during the audit, and third-party
source trackers report that ManhwaClub has migrated stacks repeatedly — the
Hakuneko connector had to be updated when ``manhwa.club`` redirected to
``manhwahentai.to``), this parser is deliberately written as a
**universal** reader that auto-adapts to the layouts most commonly seen on
manhwa aggregators:

* WordPress **Madara** — the stack used by x-manga.org, x-manga.net,
  scan-hentai.fr, hentaizone.xyz, toonily and many other sites already
  supported in this project.
* WordPress **MangaThemesia** — the stack used by Asura Scans, Flame Scans
  and several other aggregators.
* A **custom SPA** (Nuxt/Vue) with a ``/serie/{slug}/`` or ``/manga/{slug}/``
  catalogue layout — the pattern used by Ortega Scans and Poseidon Scans.
* A set of **generic** selectors for any remaining layout.

Rather than inheriting from a single mixin — which would lock the parser
into one stack and force a rewrite the next time ManhwaClub changes
platform — the four operations (``search``, ``get_manga``, ``get_chapters``,
``get_pages``) are implemented directly with **multi-selector fallbacks**.
Each operation tries the Madara selectors first, then the MangaThemesia
selectors, then the generic ones.  The first match wins.

The site is additionally fronted by **Cloudflare**, so requests may be
challenged and must be routed through FlareSolverr / Playwright when needed.
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
upgrades a request to a browser-backed solve when a Cloudflare challenge is
detected, then replays the original request with the obtained cookies.

The site is English-language and general-audience (non-adult) by declaration.
Some chapters may be mature, but the site itself is not adult-only.  Adult
filtering is governed by ``Settings.content.allow_adult`` at the session
level, exactly like every other general-purpose parser.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  ManhwaClub has operated under ``manhwa.club`` and
``manhwaclub.net``; if it migrates again, only ``sites.yaml`` needs
updating — this file stays untouched.
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

__all__ = ["ManhwaClubParser"]

# ---------------------------------------------------------------------------
# Selector sets — tried in order.  The first set that yields a non-empty
# result wins.  This is what makes the parser resilient to the stack changes
# that have historically broken ManhwaClub scrapers.
# ---------------------------------------------------------------------------

# Search result cards
_SEARCH_CARD_SELECTORS: tuple[str, ...] = (
    # Madara
    "div.c-tabs-item__content",
    "div.page-item-detail",
    # MangaThemesia
    "div.bsx",
    "div.bs",
    # Custom / SPA catalogue
    "div.manga-item",
    "div.comic-item",
    "div.serie-card",
    "div.manga-card",
    "div.card",
    "article.serie",
    # Generic
    "div.search-result",
    "article.manga",
)

# Anchor inside a search card that points to the manga
_SEARCH_LINK_SELECTORS: tuple[str, ...] = (
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
    ".serie-title",
    ".card-title",
    ".title",
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
    "h1.serie-title",
    "h1.entry-title",
    "h1",
)

# Manga page cover
_MANGA_COVER_SELECTORS: tuple[str, ...] = (
    "div.summary_image img",
    "div.manga-cover img",
    "div.serie-cover img",
    "div.series-cover img",
    "div.cover img",
    "article img",
)

# Manga page tags / genres
_MANGA_TAG_SELECTORS: tuple[str, ...] = (
    "div.genres a",
    "div.manga-genres a",
    "div.genres-content a",
    "div.serie-genres a",
    "a[href*='/genre/']",
    "a[href*='/manga-genre/']",
)

# Manga page status
_MANGA_STATUS_SELECTORS: tuple[str, ...] = (
    "div.post-status div.summary-content",
    "div.manga-status",
    "div.serie-status",
    "div.status",
    ".post-content_item .summary-content",
)

# Chapter list anchors
_CHAPTER_SELECTORS: tuple[str, ...] = (
    # Madara
    "li.wp-manga-chapter a",
    "div.listing-chapters_wrap a",
    # MangaThemesia
    "div.eplister li a",
    "div.chapter-list a",
    # SPA catalogue
    "div.chapters a",
    "a[href*='/manga/'][href*='/chapter-']",
    "a[href*='/manga/'][href*='-chapter-']",
    # Generic
    "a[href*='/chapter/']",
    "a[href*='/chapitre/']",
    "a[href*='-chapter-']",
    "a[href*='-chapitre-']",
    "a.chapter-link",
)

# Reader page images
_PAGE_SELECTORS: tuple[str, ...] = (
    # Madara
    "div.reading-content img",
    "div.page-break img",
    # MangaThemesia
    "div#chapter-content img",
    "div.reader-content img",
    # SPA catalogue
    "div.chapter-content img",
    "div.chapter-images img",
    # Generic
    "div.reader img",
    "div.chapter-reader img",
    "article img",
)


class ManhwaClubParser(CloudflareMixin, BaseParser):
    """Universal parser for ManhwaClub (English manhwa, Cloudflare-protected)."""

    site_id: str = "manhwaclub"
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
        """Search ManhwaClub and yield results page by page.

        The parser tries the WordPress search route first
        (``/?s={query}``), then falls back to the SPA route
        (``/search?q={query}``).  Each result card is matched against a list
        of known selectors; the first card selector that yields matches wins.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        # WordPress search first, then SPA search.
        urls = (
            f"{self._base_url}/?s={quote(query)}&page={page}",
            f"{self._base_url}/search?q={quote(query)}&page={page}",
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
        """Return the :class:`Manga` for a ManhwaClub URL or slug.

        The manga page title, cover, genres and status are each extracted
        through a selector chain that covers Madara, MangaThemesia, the SPA
        catalogue layout and the common custom layouts.

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

        The chapter list is embedded directly in the manga page on all
        supported layouts.  The parser tries every chapter selector in turn
        and collects all matching anchors from the first selector that
        yields results.  Chapters are deduplicated by
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

        # ManhwaClub lists chapters newest-first; reverse to chronological
        # order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page images are extracted through a selector chain that
        covers Madara, MangaThemesia, the SPA catalogue layout and the
        common custom layouts.  Images are lazy-loaded on every layout, so
        ``data-src`` is preferred over ``src`` to avoid downloading
        placeholder images.
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

        ManhwaClub labels look like ``"Chapter 48"``, ``"48"`` or
        ``"Chapter 48 - Title"``; the first numeric token is used in all
        cases.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.kr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("manhwaclub", ManhwaClubParser)
