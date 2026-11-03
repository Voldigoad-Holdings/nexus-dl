"""Parser mixin for WordPress Madara sites.

WordPress Madara is the most common manga-reading theme in the scanlation
ecosystem.  It ships the standard routes:

* Search           — ``GET  /?s={query}&post_type=wp-manga&page={n}``
* Manga details    — ``GET  /manga/{slug}/``
* Chapter list     — ``POST {manga_url}ajax/chapters/``
  (fallback: inline ``li.wp-manga-chapter`` on the manga page)
* Reader pages     — ``GET  /manga/{slug}/{chapter-slug}/``

The mixin implements all four :class:`BaseParser` abstract operations using
those routes.  A concrete Madara parser is therefore just an identity
declaration::

    class MyParser(WordPressMadaraMixin, BaseParser):
        site_id = "my_site"
        language = "en"
        adult = False

    register("my_site", MyParser)

Sites that need Cloudflare handling combine this mixin with
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`::

    class MyParser(WordPressMadaraMixin, CloudflareMixin, BaseParser):
        ...

Sites with a non-standard URL prefix (e.g. ``/webtoon/`` instead of
``/manga/``, as on ToonFR) override :meth:`search` only; the other three
operations remain path-prefix agnostic because they walk the URL returned
by :meth:`search` and :meth:`get_manga`.

Sites that ship a non-standard chapter list (e.g. Reaper Scans with
Livewire) override :meth:`get_chapters` while still benefiting from the
other three operations.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, AsyncIterator
from urllib.parse import quote

from selectolax.parser import HTMLParser

from nexusdl.core.exceptions import NexusDLError
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.session.http_session import HttpSession


__all__ = ["WordPressMadaraMixin"]


# ---------------------------------------------------------------------------
# Madara selectors — kept as module-level constants so the intent is clear
# and any site that needs a slight variation can override the affected
# operation in its parser class rather than monkey-patching these defaults.
# ---------------------------------------------------------------------------

# Search result cards.  ``page-item-detail`` is the standard search card;
# ``c-tabs-item__content`` is used on home-page tabs and appears in some
# search results too (when a site reuses the tab markup for search).
_SEARCH_CARD_SELECTOR = "div.page-item-detail, div.c-tabs-item__content"

# Anchor inside a search card pointing to the manga.
_SEARCH_LINK_SELECTOR = "a[href*='/manga/']"

# Title inside a search card — the anchor, not the wrapping element, so
# sub-elements (chapter count, status badge) do not pollute the title.
_SEARCH_TITLE_SELECTOR = ".post-title a, h3 a, h4 a"

# First image inside a search card is the cover in standard Madara.
_SEARCH_COVER_SELECTOR = "img"

# Manga page — title, cover, genre tags, status.
_MANGA_TITLE_SELECTOR = "div.post-title h1, h1.manga-title, h1"
_MANGA_COVER_SELECTOR = (
    "div.summary_image img, div.manga-cover img, div.cover img"
)
_MANGA_TAG_SELECTOR = "div.genres a, div.genres-content a"
_MANGA_STATUS_SELECTOR = "div.post-status div.summary-content"

# Chapter list — inline and AJAX-fetched pages share the same markup.
_CHAPTER_SELECTOR = "li.wp-manga-chapter a, div.listing-chapters_wrap a"

# Reader page images.
_PAGE_SELECTOR = "div.reading-content img, div.page-break img"


class WordPressMadaraMixin:
    """Mixin implementing the four BaseParser operations for Madara sites.

    The mixin relies on the following attributes provided by
    :class:`~nexusdl.parsers.base.BaseParser` in the concrete parser:

    * ``site_id``, ``language``, ``adult`` — site identity.
    * ``_base_url`` — the site's base URL, loaded from ``sites.yaml``.
    * ``session`` — the shared :class:`HttpSession`.
    * ``_log`` — the bound logger.
    * ``_resolve_url``, ``_extract_id``, ``_extract_chapter_id``,
      ``_make_manga_id``, ``_make_chapter_id`` — ID helpers.

    These are declared under :data:`TYPE_CHECKING` so mypy resolves
    ``self.<attr>`` inside this file without importing BaseParser at
    runtime and without creating a circular import.
    """

    if TYPE_CHECKING:
        site_id: str
        language: str
        adult: bool
        _base_url: str
        session: "HttpSession"
        _log: "Logger"

        def _resolve_url(self, url_or_id: str) -> str: ...
        def _extract_id(self, url: str) -> str: ...
        def _extract_chapter_id(self, url: str) -> str: ...
        def _make_manga_id(self, url: str) -> str: ...
        def _make_chapter_id(self, manga_id: str, url: str) -> str: ...

    # ------------------------------------------------------------------ #
    #  search                                                            #
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator[SearchResult]:
        """Search the Madara site and yield results page by page.

        Uses the standard Madara search route and parses the standard
        search-result cards.  Home-page tab cards
        (``div.c-tabs-item__content``) are also matched, because some
        Madara forks reuse that markup for search results.

        Args:
            query: Search terms.
            page: 1-based page number.

        Yields:
            One :class:`SearchResult` per matching manga.
        """
        url = (
            f"{self._base_url}/?s={quote(query)}"
            f"&post_type=wp-manga&page={page}"
        )
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        for card in tree.css(_SEARCH_CARD_SELECTOR):
            link = card.css_first(_SEARCH_LINK_SELECTOR)
            if link is None:
                continue
            href = link.attributes.get("href", "")
            if not href:
                continue

            title_link = card.css_first(_SEARCH_TITLE_SELECTOR)
            title = (
                title_link.text(strip=True)
                if title_link is not None
                else ""
            )

            cover_node = card.css_first(_SEARCH_COVER_SELECTOR)
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
        """Return the :class:`Manga` for a Madara URL or slug.

        Fetches the manga page and extracts the title, cover, genre tags
        and status.  The ``Manga.id`` is derived via
        :meth:`BaseParser._make_manga_id` so that the id is stable across
        sessions and matches the library database.

        Args:
            url_or_id: Full URL (as returned by :meth:`search`) or bare
                slug (as stored in the library).

        Returns:
            The populated :class:`Manga`.
        """
        url = self._resolve_url(url_or_id)
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        title_node = tree.css_first(_MANGA_TITLE_SELECTOR)
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        cover_node = tree.css_first(_MANGA_COVER_SELECTOR)
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        tags: list[str] = []
        for tag_node in tree.css(_MANGA_TAG_SELECTOR):
            text = tag_node.text(strip=True)
            if text:
                tags.append(text)

        status: str | None = None
        status_node = tree.css_first(_MANGA_STATUS_SELECTOR)
        if status_node is not None:
            status_text = status_node.text(strip=True).lower()
            if "ongoing" in status_text or "en cours" in status_text:
                status = "ongoing"
            elif "completed" in status_text or "terminé" in status_text:
                status = "completed"
            elif "hiatus" in status_text or "pause" in status_text:
                status = "hiatus"
            elif "cancelled" in status_text or "annulé" in status_text:
                status = "cancelled"

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

        Madara serves the chapter list in one of two ways depending on
        the site's configuration:

        * **Inline** — the full list is rendered directly in the manga
          page as ``li.wp-manga-chapter`` elements.  Most sites use this
          mode for their initial page view.
        * **AJAX** — the full list is fetched on demand from
          ``POST {manga_url}ajax/chapters/``.  Some sites use this mode
          for every manga; others use inline for the first few chapters
          and AJAX for the rest.

        This method tries inline first (a cheap GET that is already
        cached by the HTTP cache layer), then falls back to the AJAX
        endpoint if the inline list is empty.  This ordering means sites
        that serve inline chapters never pay the cost of an extra POST,
        while AJAX-only sites still work correctly.

        Chapters are deduplicated by
        :meth:`BaseParser._extract_chapter_id` and returned in
        chronological order (Madara lists newest-first, so the final list
        is reversed).
        """
        html = await self.session.get_html(manga.url)
        chapters = self._chapters_from_html(html, manga)
        if not chapters:
            chapters = await self._chapters_from_ajax(manga)
        return list(reversed(chapters))

    async def _chapters_from_ajax(self, manga: Manga) -> list[Chapter]:
        """Fetch and parse the chapter list from the Madara AJAX endpoint.

        On failure (network error, Cloudflare challenge that could not be
        solved, HTTP error, etc.) the method returns an empty list so that
        the caller ends up with no chapters rather than a raised exception.
        The failure is logged at DEBUG level; it is not propagated because
        a missing AJAX endpoint is a normal Madara configuration, not an
        error.
        """
        ajax_url = manga.url.rstrip("/") + "/ajax/chapters/"
        try:
            response = await self.session.post(ajax_url)
        except NexusDLError as exc:
            self._log.debug(
                "Madara AJAX chapters failed for {}: {}", ajax_url, exc
            )
            return []

        # ``session.post`` returns the underlying HTTP response.  Use its
        # ``text`` attribute; if a future HttpSession wrapper returns the
        # text directly (as ``get_html`` does for GET), fall back to that.
        if isinstance(response, str):
            html = response
        else:
            html = getattr(response, "text", "") or ""

        if not html:
            return []
        return self._chapters_from_html(html, manga)

    def _chapters_from_html(
        self, html: str, manga: Manga
    ) -> list[Chapter]:
        """Parse ``li.wp-manga-chapter`` anchors into :class:`Chapter` objects.

        Shared by the inline and AJAX paths, which use identical markup.
        Returns the chapters in the order they appear in the HTML
        (newest-first on Madara).
        """
        tree = HTMLParser(html)
        chapters: dict[str, Chapter] = {}

        for link in tree.css(_CHAPTER_SELECTOR):
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

        return list(chapters.values())

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        Madara renders the reader page as a sequence of ``img`` elements
        inside ``div.reading-content`` (newer themes) or
        ``div.page-break`` (older themes).  Images are lazy-loaded via
        ``data-src``, so this method prefers ``data-src`` over ``src`` to
        avoid downloading placeholder images.

        Args:
            chapter: The chapter whose pages to fetch.

        Returns:
            The ordered list of :class:`Page` objects, indexed from 0.
        """
        html = await self.session.get_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        for img in tree.css(_PAGE_SELECTOR):
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

        Madara chapter labels vary widely: ``"Chapter 48"``,
        ``"Chapitre 48"``, ``"48"``, ``"Vol. 3 Chapter 12"``, and many
        more.  The first numeric token in the label is returned; labels
        without a number yield ``None`` so the library can fall back to
        insertion order when sorting.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None
