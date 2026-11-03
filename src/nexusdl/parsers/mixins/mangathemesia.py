"""Parser mixin for WordPress MangaThemesia sites.

MangaThemesia is a WordPress manga-reading theme derived from Madara but with
several important divergences.  It is the stack used by Asura Scans, Flame
Scans, Galaxy Scans, Leviathan Scans and Luminous Scans in this project.

Shared routes and markup
------------------------
* Search           — ``GET  /?s={query}&post_type=wp-manga&page={n}``
* Manga details    — ``GET  /{manga_prefix}/{slug}/``  where
  ``manga_prefix`` is ``manga`` on most sites, but ``series`` or ``comics``
  on some forks (Asura, Flame, etc.).  The mixin follows whatever URL the
  search result or library call provides, so this difference is transparent.
* Chapter list     — rendered inline in the manga page as
  ``.eplister li a``.  MangaThemesia has **no AJAX chapter endpoint** —
  unlike Madara — so the entire list is always present in the initial HTML.
* Reader pages     — rendered inside ``#readerarea`` (or, on older forks,
  ``#chapter-content`` / ``.container-chapter-reader``).

Divergences from Madara that matter to this mixin
-------------------------------------------------
* Search cards use ``.bs`` / ``.bsx`` / ``.utao`` (not Madara's
  ``.c-tabs-item__content``).
* The search-result title lives in ``.tt`` (or ``.ntitle``), not in
  ``.post-title a``.
* Manga metadata is a ``.tsinfo .imptdt`` list, where each row contains a
  label followed by the value (often wrapped in ``<i>``).
* Genres are rendered as ``.genxed a`` (or ``.mgen a``).
* The chapter list selector is ``.eplister li a``, not
  ``li.wp-manga-chapter a``.

A concrete MangaThemesia parser is therefore just an identity declaration::

    class MyParser(MangaThemesiaMixin, BaseParser):
        site_id = "my_site"
        language = "en"
        adult = False

    register("my_site", MyParser)

Sites that need Cloudflare handling combine this mixin with
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`::

    class MyParser(MangaThemesiaMixin, CloudflareMixin, BaseParser):
        ...

Sites that ship a non-standard search URL (some MangaThemesia forks use
``/search?q=`` instead of ``/?s=``) override :meth:`search` only; the other
three operations remain path-prefix agnostic because they walk the URL
returned by :meth:`search` and :meth:`get_manga`.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, AsyncIterator
from urllib.parse import quote

from selectolax.parser import HTMLParser

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.session.http_session import HttpSession


__all__ = ["MangaThemesiaMixin"]


# ---------------------------------------------------------------------------
# MangaThemesia selectors — kept as module-level constants so the intent is
# clear and any site that needs a slight variation can override the affected
# operation in its parser class rather than monkey-patching these defaults.
# ---------------------------------------------------------------------------

# Search result cards.  ``.bs`` is the classic MangaThemesia card class;
# ``.bsx`` is its styled variant; ``.utao`` is the newer card used by some
# forks (e.g. the "latest updates" grid).
_SEARCH_CARD_SELECTOR = ".listupd .bs, .listupd .bsx, .listupd .utao"

# Anchor inside a search card pointing to the manga.  MangaThemesia does not
# standardise the manga prefix across forks, so we accept every common one.
_SEARCH_LINK_SELECTOR = (
    "a[href*='/manga/'], a[href*='/series/'], a[href*='/comics/']"
)

# Title inside a search card — ``.tt`` is the classic, ``.ntitle`` the newer
# variant.
_SEARCH_TITLE_SELECTOR = ".tt, .ntitle"

# First image inside a search card is the cover in standard MangaThemesia.
_SEARCH_COVER_SELECTOR = "img"

# Manga page — title, cover, genre tags.  The metadata block (status, type,
# released) is parsed separately because it requires per-row iteration.
_MANGA_TITLE_SELECTOR = "h1.entry-title, h1[itemprop='name'], h1"
_MANGA_COVER_SELECTOR = ".thumb img, .summary_image img, .manga-cover img"
_MANGA_TAG_SELECTOR = ".genxed a, .mgen a, .genres a"
_MANGA_META_SELECTOR = ".tsinfo .imptdt"

# Chapter list — always inline in MangaThemesia.
_CHAPTER_SELECTOR = ".eplister li a"

# Reader page images.  ``#readerarea`` is the classic reader container;
# ``#chapter-content`` and ``.container-chapter-reader`` are the newer and
# legacy variants used by some forks.
_PAGE_SELECTOR = (
    "#readerarea img, "
    "#chapter-content img, "
    ".container-chapter-reader img"
)


class MangaThemesiaMixin:
    """Mixin implementing the four BaseParser operations for MangaThemesia sites.

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
        """Search the MangaThemesia site and yield results page by page.

        Uses the standard MangaThemesia search route and parses the
        standard search-result cards.  Forks that use a different search
        URL (``/search?q=`` instead of ``/?s=``) override this method in
        their parser class.

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

            title_node = card.css_first(_SEARCH_TITLE_SELECTOR)
            title = (
                title_node.text(strip=True)
                if title_node is not None
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
        """Return the :class:`Manga` for a MangaThemesia URL or slug.

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

        status = self._extract_status(tree)

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

        MangaThemesia always renders the full chapter list inline in the
        manga page as ``.eplister li a`` — there is no AJAX endpoint, so a
        single GET is sufficient.  Chapters are deduplicated by
        :meth:`BaseParser._extract_chapter_id` and returned in
        chronological order (MangaThemesia lists newest-first, so the
        final list is reversed).

        Args:
            manga: The manga whose chapters to enumerate.

        Returns:
            The ordered list of :class:`Chapter` objects.
        """
        html = await self.session.get_html(manga.url)
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

        # MangaThemesia lists chapters newest-first; reverse to
        # chronological order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        MangaThemesia renders the reader page as a sequence of ``img``
        elements inside ``#readerarea`` (the classic container) or
        ``#chapter-content`` / ``.container-chapter-reader`` on forks that
        restyled the reader.  Images are lazy-loaded via ``data-src``, so
        this method prefers ``data-src`` over ``src`` to avoid downloading
        placeholder images.

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
    def _extract_status(self, tree: HTMLParser) -> str | None:
        """Return the normalised status of a manga, if present.

        MangaThemesia renders metadata as a list of ``.tsinfo .imptdt``
        rows, each containing a label followed by a value (often wrapped
        in ``<i>``).  We locate the row whose label starts with "Status"
        and extract the value from the ``<i>`` child when present, falling
        back to stripping the label from the row's text.

        Returns ``None`` when no status row is found, so the library can
        leave the field empty rather than guessing.
        """
        for row in tree.css(_MANGA_META_SELECTOR):
            full_text = row.text(strip=True)
            if not full_text.lower().startswith("status"):
                continue

            # The value usually lives in an <i> child; fall back to
            # stripping the "Status" prefix from the row text.
            value_node = row.css_first("i")
            if value_node is not None:
                value = value_node.text(strip=True)
            else:
                match = re.search(
                    r"status\s*(.+)$", full_text, re.IGNORECASE
                )
                if not match:
                    return None
                value = match.group(1).strip()

            return self._normalise_status(value)

        return None

    @staticmethod
    def _normalise_status(raw: str) -> str | None:
        """Map a free-form status label onto the project's vocabulary.

        Handles both English and French labels, since several French
        MangaThemesia forks (ToonFR, etc.) render French status text.
        Unknown values yield ``None`` so the library leaves the field
        empty rather than storing a raw label.
        """
        lowered = raw.strip().lower()
        if not lowered:
            return None
        if "ongoing" in lowered or "en cours" in lowered:
            return "ongoing"
        if "completed" in lowered or "terminé" in lowered or "complete" in lowered:
            return "completed"
        if "hiatus" in lowered or "pause" in lowered:
            return "hiatus"
        if "cancelled" in lowered or "canceled" in lowered or "annulé" in lowered:
            return "cancelled"
        return None

    @staticmethod
    def _parse_chapter_number(label: str) -> float | None:
        """Extract a numeric chapter value from a free-form label.

        MangaThemesia chapter labels vary widely: ``"Chapter 48"``,
        ``"Ch. 48"``, ``"48"``, ``"48 - Title"``, and many more.  The
        first numeric token in the label is returned; labels without a
        number yield ``None`` so the library can fall back to insertion
        order when sorting.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None
