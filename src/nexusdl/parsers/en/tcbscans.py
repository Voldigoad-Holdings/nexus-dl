"""Parser for TCB Scans — English manga scanlation group (custom + Cloudflare).

TCB Scans (``tcbscans.com``) is a custom Laravel-based manga reader — **not**
a WordPress Madara site — fronted by **Cloudflare**.  It has a long history of
domain churn (``tcbscans.com`` → ``tcb-backup.bihar-mirchi.com`` →
``tcbscans.me``) and aggressive anti-scraping measures, including deliberately
injected junk images at the end of popular chapters[reference:0].

Because of this, the parser combines two mixins:

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge is
  detected, then replays the original request with the obtained cookies.
* :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` renders pages
  through the shared Playwright pool, which is the only reliable way to obtain
  the chapter list and reader pages on a site that actively breaks pure-HTTP
  scrapers[reference:1][reference:2].

Route patterns (observed across the site's active domains)
----------------------------------------------------------
* Manga list       — ``GET  /projects``
* Search           — ``GET  /search?q={query}``
* Manga details    — ``GET  /mangas/{id}/{slug}``
* Chapter list     — embedded in the manga page as ``a[href*='/chapters/']``
* Reader           — ``GET  /chapters/{id}/{slug}``
  Images live inside ``<picture>`` elements with an ``<img>`` child; the
  real URL is in ``src`` or ``data-src``.

The site is English-language and general-audience (non-adult), declared on
the class so the registry can filter it via ``Settings.content.allow_adult``
and the ``language`` filter of ``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  TCB Scans has migrated several times; if it moves again, only
``sites.yaml`` needs updating — this file stays untouched.
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

__all__ = ["TCBScansParser"]


class TCBScansParser(JsRenderedMixin, CloudflareMixin, BaseParser):
    """Parser for TCB Scans (custom Laravel + Cloudflare, English)."""

    site_id: str = "tcbscans"
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
        """Search TCB Scans and yield results page by page.

        The site's search route accepts a ``q`` query parameter.  Results are
        rendered as a grid of manga cards, each linking to
        ``/mangas/{id}/{slug}`` with a cover image.  The site has no
        meaningful pagination, so ``page > 1`` yields nothing.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        if page > 1:
            # TCB Scans returns all matches on a single page.
            return

        url = f"{self._base_url}/search?q={quote(query)}"
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        # The search results grid uses anchor cards.  Select any anchor
        # pointing to a manga page; the title and cover are extracted from
        # the surrounding block.
        for link in tree.css("a[href*='/mangas/']"):
            href = link.attributes.get("href", "")
            if not href:
                continue

            # The title is either the anchor text or the alt text of the
            # cover image inside the anchor.
            title = link.text(strip=True)
            cover_node = link.css_first("img")
            cover_url = ""
            if cover_node is not None:
                if not title:
                    title = cover_node.attributes.get("alt", "").strip()
                cover_url = (
                    cover_node.attributes.get("data-src")
                    or cover_node.attributes.get("src")
                    or ""
                )

            if not title:
                title = self._extract_id(href)

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
        """Return the :class:`Manga` for a TCB Scans URL or slug.

        The manga page exposes the title in an ``h1`` (or ``.manga-title``),
        the cover in a nearby ``img``, and the metadata (status, genres) in
        a detail block.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare slug (as stored in the local library).
        """
        url = self._resolve_url(url_or_id)
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        # Title — the site uses a plain h1, sometimes wrapped in a heading
        # block.
        title_node = tree.css_first("h1, .manga-title, .series-title")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        # Cover — usually the first substantial image on the page.  We look
        # for an image inside a cover container, then fall back to any img
        # whose src looks like a CDN asset.
        cover_node = tree.css_first(
            ".manga-cover img, .series-cover img, .cover img, article img"
        )
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        # Tags / genres — rendered as anchors in the metadata block.
        tags: list[str] = []
        for tag_node in tree.css("a[href*='/genre/'], a[href*='/genres/']"):
            text = tag_node.text(strip=True)
            if text:
                tags.append(text)

        # Status — exposed as free text in the detail block.
        status: str | None = None
        for node in tree.css(".manga-status, .status, .series-status"):
            text = node.text(strip=True).lower()
            if "ongoing" in text:
                status = "ongoing"
                break
            if "completed" in text:
                status = "completed"
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
        """Return the ordered chapter list for *manga*.

        TCB Scans embeds the chapter list directly in the manga page as
        anchors pointing to ``/chapters/{id}/{slug}``.  The list may contain
        duplicates (e.g. in “latest” widgets), so we deduplicate by chapter
        id.  Chapters are listed newest-first in the DOM and are reversed
        before being returned so the downloader processes them in
        chronological order.
        """
        html = await self._get_rendered_html(manga.url)
        tree = HTMLParser(html)

        chapters: dict[str, Chapter] = {}
        for link in tree.css("a[href*='/chapters/']"):
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

        # TCB Scans lists chapters newest-first; reverse to chronological
        # order.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page wraps each page in a ``<picture>`` element with an
        ``<img>`` child — this is the pattern the reference Rust scraper
        relies on[reference:3].  The real URL is in ``src`` or ``data-src``.
        We collect every ``<img>`` inside a ``<picture>`` (falling back to
        any ``img`` on the page if no ``<picture>`` is present) and build one
        :class:`Page` per entry.

        TCB Scans sometimes injects junk images at the end of popular
        chapters[reference:4].  Because those images are indistinguishable
        from real pages by URL alone, they are kept in the list and
        discarded downstream by the image validator if they fail to
        download or decode.
        """
        html = await self._get_rendered_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []

        # Preferred path: images inside <picture> elements.
        picture_imgs = tree.css("picture img")
        if picture_imgs:
            candidates = picture_imgs
        else:
            # Fallback: any image inside the reader container.
            candidates = tree.css(".reader img, .chapter-reader img, .reading-content img, article img")

        for img in candidates:
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
    def _parse_chapter_number(label: str) -> float | None:
        """Extract a numeric chapter value from a free-form label.

        TCB Scans labels look like ``"Chapter 1089"``, ``"1089"`` or
        ``"One Piece - Chapter 1089"``.  The first numeric token is used.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("tcbscans", TCBScansParser)
