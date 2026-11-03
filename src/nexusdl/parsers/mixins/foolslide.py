"""Parser mixin for FoolSlide sites.

FoolSlide is an open-source comic management system used by many scanlation
groups to publish their own releases on a self-hosted reader.  It is
**not** a WordPress theme: the site is a small PHP application that ships a
consistent set of routes across every installation, which makes a mixin the
ideal way to support it.

The only FoolSlide site in this project is **Kirei Cake**
(``kireicake``), but the mixin is written to be reusable for any other
FoolSlide installation that may be added later.

Shared routes and markup
------------------------
FoolSlide has a small, stable set of routes:

* **Search** — ``POST /search/`` with a form field named ``search``.  The
  response is a paginated HTML page whose result cards link to
  ``/series/{slug}/``.
* **Series page** — ``GET /series/{slug}/``.  The page renders the manga
  metadata (title, cover, author, genres, description) and the full chapter
  list as anchors to
  ``/read/{manga_slug}/{lang}/{chapter_slug}/``.
* **Reader page** — ``GET /read/{manga_slug}/{lang}/{chapter_slug}/``.
  Unlike most reader themes, FoolSlide does **not** render page images in
  the DOM: it embeds them as a JavaScript variable::

      var pages = [
          {"id":"abc123","url":"/read/...","name":"01.png"},
          ...
      ];

  The variable is always present in the initial HTML — no JavaScript
  execution is required to read it, only a regex / JSON extraction step.
  Some installations wrap the array in ``var pages = [...]`` exactly as
  above; others emit ``window.pages = [...]``.  The mixin accepts both.

Pagination
----------
FoolSlide paginates the **search results** (usually 20 per page) and, on
some installations, the **chapter list** of a series.  The mixin follows
both paginations transparently:

* :meth:`search` accepts a ``page`` argument and appends ``&page={n}`` to
  the query string.
* :meth:`get_chapters` collects every chapter on the series page.  FoolSlide
  installations that paginate the chapter list expose a "next page" link on
  the series page; the mixin follows it until it disappears (bounded by a
  hard safety cap to avoid infinite loops on a broken response).

Reader page images
------------------
The reader page's ``pages`` JavaScript variable holds, for each page, the
**path segment** of the image (not the full URL).  The actual image is
served from a per-chapter directory whose base is itself exposed in the
same page as ``var directory = "/path/to/chapter";``.  The full image URL is
therefore ``{base}/{{directory}}/{{page.url}}`` — or, when ``directory`` is
absent, ``{page.url}`` is used as-is (some installations emit absolute URLs
directly).  Both shapes are handled by the mixin.

A concrete FoolSlide parser is therefore just an identity declaration::

    class MyParser(FoolSlideMixin, BaseParser):
        site_id = "my_site"
        language = "en"
        adult = False

    register("my_site", MyParser)

Sites that need Cloudflare handling combine this mixin with
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, AsyncIterator

from selectolax.parser import HTMLParser

from nexusdl.core.exceptions import NexusDLError, ParserParseError
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.session.http_session import HttpSession


__all__ = ["FoolSlideMixin"]


# ---------------------------------------------------------------------------
# Selectors — kept as module-level constants for clarity and to make it easy
# for a concrete parser to override a specific operation without touching
# the mixin.
# ---------------------------------------------------------------------------

# Search result cards.  FoolSlide installations emit several variants across
# versions; we accept the two most common ones (``group`` on recent versions,
# ``list-item`` on older themes) plus a generic ``.media`` fallback used by a
# few customised installations.
_SEARCH_CARD_SELECTOR = (
    "div.group, div.list-item, div.media, article.group"
)

# Anchor inside a search card pointing to the series.  The slug lives under
# ``/series/`` on every FoolSlide installation.
_SEARCH_LINK_SELECTOR = "a[href*='/series/']"

# Title inside a search card.
_SEARCH_TITLE_SELECTOR = (
    ".title a, .group-title a, h3 a, h4 a, .media-heading a"
)

# Cover image inside a search card.
_SEARCH_COVER_SELECTOR = "img"

# Series page — title, cover, genres.
_MANGA_TITLE_SELECTOR = "h1, .list-title, .series-title, .page-header h1"
_MANGA_COVER_SELECTOR = ".thumbnail img, .series-cover img, img.cover"
_MANGA_TAG_SELECTOR = ".tag-list a, .tags a, a[href*='/tag/']"

# Series page — description (used as the manga summary).
_MANGA_DESCRIPTION_SELECTOR = ".list-description, .series-description, .description"

# Series page — chapter list anchors.
_CHAPTER_SELECTOR = "a[href*='/read/']"

# Series page — pagination "next" link for paginated chapter lists.
_CHAPTER_NEXT_PAGE_SELECTOR = "a[rel='next'], .pagination a.next"

# Search result pagination "next" link.
_SEARCH_NEXT_PAGE_SELECTOR = "a[rel='next'], .pagination a.next"

# Maximum number of pages the mixin will follow when collecting chapters.
# Bounded to avoid unbounded loops on a broken response.
_MAX_CHAPTER_PAGES = 100


# ---------------------------------------------------------------------------
# Regexes for the ``pages`` / ``directory`` JavaScript variables.
# ---------------------------------------------------------------------------

# ``var pages = [ ... ];`` — also matches ``window.pages = [ ... ];``.
# The lazy ``.*?`` stops at the first closing ``]`` followed by optional
# whitespace and a semicolon, which is the shape every FoolSlide reader
# emits.  DOTALL is required because the array spans multiple lines.
_PAGES_VAR_RE = re.compile(
    r"(?:var|window\.)\s+pages\s*=\s*(\[.*?\])\s*;",
    re.DOTALL,
)

# ``var directory = "/path/to/chapter";`` — also matches
# ``window.directory = '...';``.  Both quote styles are accepted.
_DIRECTORY_VAR_RE = re.compile(
    r"""(?:var|window\.)\s+directory\s*=\s*(['"])(.*?)\1\s*;""",
)


class FoolSlideMixin:
    """Mixin implementing the four BaseParser operations for FoolSlide sites.

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
        """Search the FoolSlide site and yield results page by page.

        FoolSlide's search is a **POST** endpoint that expects a form field
        named ``search``.  The response is a paginated HTML page; each page
        contains up to twenty result cards.  This method therefore issues
        the POST request, parses the first result page, and yields the
        :class:`SearchResult` objects for it.  Callers that need further
        pages can call ``search(..., page=n)``.

        Args:
            query: Search terms.
            page: 1-based page number.  ``page == 1`` is the initial page;
                subsequent values are appended to the search URL as
                ``?page={n}``.

        Yields:
            One :class:`SearchResult` per matching series.
        """
        search_url = f"{self._base_url}/search/"
        form_data = {"search": query}
        if page > 1:
            search_url = f"{search_url}?page={page}"

        html = await self._post_html(search_url, form_data)
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
        """Return the :class:`Manga` for a FoolSlide series URL or slug.

        Fetches the series page and extracts the title, cover, genres and
        description.  The ``Manga.id`` is derived via
        :meth:`BaseParser._make_manga_id` so that the id is stable across
        sessions and matches the library database.

        FoolSlide does not expose a status field.  ``Manga.status`` is
        therefore always ``None``; the library will fall back to insertion
        order when sorting.

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

        return Manga(
            id=self._make_manga_id(url),
            site_id=self.site_id,
            title=title,
            url=url,
            cover_url=self._resolve_url(cover_url) if cover_url else None,
            language=self.language,
            adult=self.adult,
            tags=tags,
            status=None,
        )

    # ------------------------------------------------------------------ #
    #  get_chapters                                                      #
    # ------------------------------------------------------------------ #
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the complete ordered chapter list for *manga*.

        FoolSlide renders the chapter list inline on the series page as
        anchors to ``/read/{manga_slug}/{lang}/{chapter_slug}/``.  When the
        series has many chapters, the installation paginates the list and
        exposes a "next" link on the page; this method follows that link
        until it disappears, deduplicating chapters by
        :meth:`BaseParser._extract_chapter_id`.

        Chapters are returned in the order they appear in the HTML
        (FoolSlide lists them oldest-first by default, but a few
        installations reverse the order — the mixin does **not** reorder,
        so the downloader's sort logic remains authoritative).

        Args:
            manga: The manga whose chapters to enumerate.

        Returns:
            The ordered list of :class:`Chapter` objects.
        """
        chapters: dict[str, Chapter] = {}

        current_url = manga.url
        for _ in range(_MAX_CHAPTER_PAGES):
            html = await self.session.get_html(current_url)
            tree = HTMLParser(html)

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

            next_link = tree.css_first(_CHAPTER_NEXT_PAGE_SELECTOR)
            if next_link is None:
                break
            next_href = next_link.attributes.get("href")
            if not next_href:
                break
            current_url = self._resolve_url(next_href)

        return list(chapters.values())

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        FoolSlide does not render page images in the DOM.  Instead, the
        reader page embeds the page list as a JavaScript variable::

            var directory = "/path/to/chapter";
            var pages = [
                {"id":"abc123","url":"01.png","name":"01.png"},
                ...
            ];

        This method extracts both variables from the page source, composes
        the full image URL for each entry, and returns the ordered
        :class:`Page` list.  When ``directory`` is absent (some
        installations emit absolute URLs directly), the per-page ``url``
        is used as-is.

        Args:
            chapter: The chapter whose pages to fetch.

        Returns:
            The ordered list of :class:`Page` objects, indexed from 0.

        Raises:
            ParserParseError: If the page does not contain a recognisable
                ``pages`` variable.  This is a genuine parse failure — the
                page was reachable but did not look like a FoolSlide reader
                — and should surface to the caller rather than being
                silently swallowed.
        """
        html = await self.session.get_html(chapter.url)

        directory = self._extract_directory(html)
        raw_pages = self._extract_pages_array(html)

        if raw_pages is None:
            raise ParserParseError(
                f"FoolSlide reader page for site {self.site_id!r} at "
                f"{chapter.url!r} did not contain a `pages` variable."
            )

        pages: list[Page] = []
        for index, entry in enumerate(raw_pages):
            if not isinstance(entry, dict):
                continue

            raw_url = entry.get("url") or entry.get("name")
            if not isinstance(raw_url, str) or not raw_url:
                continue

            full_url = self._compose_page_url(directory, raw_url)

            pages.append(
                Page(
                    index=index,
                    url=full_url,
                    chapter_id=chapter.id,
                )
            )

        return pages

    # ------------------------------------------------------------------ #
    #  helpers — HTTP                                                    #
    # ------------------------------------------------------------------ #
    async def _post_html(
        self,
        url: str,
        form_data: dict[str, str],
    ) -> str:
        """POST *form_data* to *url* and return the response body as ``str``.

        :class:`HttpSession` exposes a generic ``post`` method that returns
        the underlying HTTP response.  Different versions of the session
        wrapper return either the response object (with a ``.text``
        attribute) or the body directly as a string, so this helper
        normalises both shapes.

        Raises:
            NexusDLError: If the request fails at the transport level.
                The mixin does **not** swallow the error: a failed search
                is a genuine failure the caller should see, not a silent
                empty result.
        """
        try:
            response = await self.session.post(url, data=form_data)
        except NexusDLError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise third-party exceptions
            raise NexusDLError(
                f"FoolSlide search POST to {url!r} failed for site "
                f"{self.site_id!r}: {exc}"
            ) from exc

        if isinstance(response, str):
            return response
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text
        raise NexusDLError(
            f"FoolSlide search POST to {url!r} for site {self.site_id!r} "
            f"returned an unsupported response of type "
            f"{type(response).__name__!r}."
        )

    # ------------------------------------------------------------------ #
    #  helpers — page extraction                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_pages_array(html: str) -> list[Any] | None:
        """Extract and parse the ``var pages = [...]`` array.

        Returns the parsed list, or ``None`` when the variable is absent or
        the JSON is malformed.  FoolSlide emits strict JSON (double-quoted
        keys and string values), so :func:`json.loads` parses the array
        directly.
        """
        match = _PAGES_VAR_RE.search(html)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None

    @staticmethod
    def _extract_directory(html: str) -> str | None:
        """Extract the ``var directory = "..."`` string from the reader page.

        Returns the raw directory path (without a trailing slash) or
        ``None`` when the variable is absent.  Both single- and double-
        quoted values are accepted, because FoolSlide installations differ
        on this point.
        """
        match = _DIRECTORY_VAR_RE.search(html)
        if match is None:
            return None
        value = match.group(2).strip()
        return value.rstrip("/") if value else None

    def _compose_page_url(
        self,
        directory: str | None,
        raw_url: str,
    ) -> str:
        """Return the absolute image URL for a single FoolSlide page.

        Three cases:

        * ``directory`` is present and ``raw_url`` is a bare filename —
          the result is ``{base}{directory}/{raw_url}``.
        * ``directory`` is absent and ``raw_url`` is a bare filename —
          the result is ``{base}/{raw_url}``.  This is unusual but has
          been observed on minimal FoolSlide forks that serve images from
          the site root.
        * ``raw_url`` is already absolute — it is returned unchanged.

        ``self._resolve_url`` is used for every relative case so that
        protocol-relative and path-relative URLs are normalised uniformly.
        """
        if raw_url.startswith("http://") or raw_url.startswith("https://"):
            return raw_url
        if directory is not None:
            return self._resolve_url(f"{directory}/{raw_url}")
        return self._resolve_url(f"/{raw_url.lstrip('/')}")

    # ------------------------------------------------------------------ #
    #  helpers — chapter numbering                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_chapter_number(label: str) -> float | None:
        """Extract a numeric chapter value from a free-form label.

        FoolSlide chapter labels are free-form and vary widely per group:
        ``"Chapter 48"``, ``"Chapitre 48"``, ``"48"``, ``"Ch. 48 - Title"``,
        ``"Vol. 3 Ch. 12"``, and many more.  The first numeric token in the
        label is returned; labels without a number (rare, but possible for
        one-shots) yield ``None`` so the library can fall back to insertion
        order when sorting.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None
