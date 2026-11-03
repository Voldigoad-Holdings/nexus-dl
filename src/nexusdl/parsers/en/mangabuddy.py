"""Parser for MangaBuddy — English manga/manhwa aggregator (MadTheme + Cloudflare).

MangaBuddy runs on **MadTheme**, a WordPress manga theme that is derived from
Madara but diverges in several important ways.  In particular:

* Search uses ``GET /search?q={query}`` instead of Madara's
  ``/?s={query}&post_type=wp-manga``.
* The search results container is ``.book-detailed-item`` instead of Madara's
  ``.c-tabs-item__content``.
* The chapter list is embedded directly in the manga page as
  ``ul#chapter-list li > a`` instead of being fetched through the
  ``/ajax/chapters/`` endpoint.
* The reader uses ``#chapter-images > .chapter-image > img`` instead of
  Madara's ``.reading-content .page-break img``.

Because of these divergences this parser does **not** inherit from
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`.
Instead it implements the four operations directly, while inheriting from
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` to transparently
route requests through FlareSolverr / Playwright when a Cloudflare challenge
is detected — MangaBuddy is known to sit behind Cloudflare and to require a
cookie workaround for some clients.

Route patterns
--------------
* Search           — ``GET  /search?q={query}&page={page}``
* Manga details    — ``GET  /{slug}``
* Chapter list     — embedded in the manga page (``ul#chapter-list li > a``)
* Reader           — ``GET  /{slug}/chapter-{number}``

The site is English-language and general-audience (non-adult), declared on
the class so the registry can filter it via ``Settings.content.allow_adult``
and the ``language`` filter of ``SiteRegistry.list_sites``.

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

__all__ = ["MangaBuddyParser"]


class MangaBuddyParser(CloudflareMixin, BaseParser):
    """Parser for MangaBuddy (MadTheme + Cloudflare, English)."""

    site_id: str = "mangabuddy"
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
        """Search MangaBuddy and yield results page by page.

        The search route accepts ``q`` and ``page`` query parameters.  Each
        result is a ``.book-detailed-item`` card containing a link to the
        manga and a cover image.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        url = f"{self._base_url}/search?q={quote(query)}&page={page}"
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        for card in tree.css(".book-detailed-item"):
            link = card.css_first("a")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title_node = card.css_first(".title, h3, .book-title")
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
        """Return the :class:`Manga` for a series URL or slug.

        MangaBuddy series pages live at ``/{slug}`` (no ``/manga/`` prefix).
        The page exposes the title, cover, synopsis and metadata block.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare slug (as stored in the local library).
        """
        url = self._resolve_url(url_or_id)
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        # Title — the MadTheme manga page renders it in an <h1> inside the
        # book details block.
        title_node = tree.css_first(".book-detailed-title h1, .book-detail h1, h1")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        # Cover — the main cover image sits inside the details block.
        cover_node = tree.css_first(".book-detailed-cover img, .book-cover img, .detail-cover img")
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        # Tags / genres — MadTheme renders them as anchors in the meta block.
        tags: list[str] = []
        for tag_node in tree.css(".book-detailed-item .meta a, .book-detail .meta a"):
            text = tag_node.text(strip=True)
            if text:
                tags.append(text)

        # Status — MadTheme exposes it as text in the meta block.
        status: str | None = None
        status_node = tree.css_first(".book-detailed-item .meta .status, .book-detail .meta .status")
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
        """Return the ordered chapter list for *manga*.

        MadTheme embeds the complete chapter list directly in the manga
        page as ``ul#chapter-list li > a``.  The list may be split into
        multiple ``ul`` blocks (one per volume or batch); we collect them
        all and deduplicate by chapter id.

        Chapters are listed newest-first in the DOM, so we reverse the
        result to chronological order before returning.
        """
        html = await self.session.get_html(manga.url)
        tree = HTMLParser(html)

        chapters: dict[str, Chapter] = {}
        # MadTheme may paginate the chapter list; when it does, the first
        # ul#chapter-list holds the most recent chapters and older ones are
        # loaded via a separate endpoint.  The in-page list is authoritative
        # for the current state of the series.
        for link in tree.css("ul#chapter-list li > a, .chapter-list li > a"):
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

        # MadTheme lists chapters newest-first; reverse to chronological
        # order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The MadTheme reader renders pages as
        ``#chapter-images > .chapter-image > img``.  Images are lazy-loaded
        via ``data-src``, so we prefer that attribute over ``src`` to avoid
        downloading placeholder images.
        """
        html = await self.session.get_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        for index, container in enumerate(tree.css("#chapter-images > .chapter-image")):
            img = container.css_first("img")
            if img is None:
                continue

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

        MangaBuddy chapter labels look like ``"Chapter 48"`` or simply
        ``"48"``; both forms are handled.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("mangabuddy", MangaBuddyParser)
