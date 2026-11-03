"""Parser for MangaSee123 — English manga archive (Cloudflare + JSON-in-JS).

MangaSee123 (also reachable as Manga4Life) is a large English-language manga
archive.  Its pages are server-rendered HTML with a critical twist: the
chapter list and the reader's page list are **not** in the DOM as anchors or
``<img>`` tags — they live inside ``vm.Chapters`` and ``vm.CHAPTERS``
JavaScript variables embedded in the page source.  On top of that, the site
sits behind **Cloudflare**, which means a plain HTTP client is regularly
challenged and must be routed through FlareSolverr / Playwright.

To keep this parser robust and aligned with the project's one-file
philosophy, it combines two mixins:

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge is
  detected, then replays the original request with the obtained cookies.
* :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` renders pages
  through the shared Playwright pool, which is what makes the embedded
  ``vm.*`` variables reliable to extract (the site sometimes injects them
  after an additional JavaScript round-trip).

Route patterns
--------------
* Search           — ``GET  /search/?name={query}``
* Manga details    — ``GET  /manga/{slug}``
* Chapter list     — embedded in the manga page as ``vm.Chapters``
* Reader           — ``GET  /read-online/{slug}-chapter-{n}-page-{p}.html``
  The reader page exposes ``vm.CHAPTERS`` (per-chapter page count and
  chapter name) and ``vm.CurPathName`` (the image host).  Image URLs follow
  ``https://{CurPathName}/manga/{slug}/{chapter:0>4}-{page:0>3}.png``.

Cover images are served from a dedicated CDN:
``https://cover.nep.li/cover/{slug}.jpg``.

Adult content
-------------
MangaSee123 is a general-purpose archive.  The parser is declared
``adult = False`` because the *site* itself is not adult-only.  When
``Settings.content.allow_adult`` is enabled, the session layer applies the
site's mature-content cookie centrally, so adult series become reachable
without any site-specific logic in this file.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  If the site migrates (as manga archives frequently do), only
``sites.yaml`` needs updating — this file stays untouched.
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator
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

__all__ = ["MangaSee123Parser"]

# Cover CDN — stable across the site's history.
_COVER_BASE = "https://cover.nep.li/cover"

# Regexes for extracting the embedded JavaScript variables.  MangaSee123
# emits them as ``vm.Chapters = [ ... ];`` and ``vm.CHAPTERS = [ ... ];``
# with optional whitespace around the ``=`` and the semicolon.
_VM_CHAPTERS_RE = re.compile(
    r"vm\.Chapters\s*=\s*(\[.*?\])\s*;",
    re.DOTALL,
)
_VM_CHAPTERS_READER_RE = re.compile(
    r"vm\.CHAPTERS\s*=\s*(\[.*?\])\s*;",
    re.DOTALL,
)
_VM_CUR_PATH_NAME_RE = re.compile(
    r'vm\.CurPathName\s*=\s*"([^"]+)"\s*;',
)
_VM_MANGA_NAME_RE = re.compile(
    r'vm\.MangaName\s*=\s*"([^"]+)"\s*;',
)

# MangaSee123 chapter numbers carry a leading sentinel (``1``) and trailing
# zeros (``100010`` → ``1``, ``100020`` → ``2``).  The ``ChapterName`` field
# usually holds a human-readable label; when it is empty we fall back to the
# numeric value.
_CHAPTER_SENTINEL = "1"


class MangaSee123Parser(JsRenderedMixin, CloudflareMixin, BaseParser):
    """Parser for MangaSee123 (Cloudflare + JSON-in-JS, English)."""

    site_id: str = "mangasee123"
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
        """Search MangaSee123 and yield results page by page.

        The site's search route accepts a ``name`` query parameter.  Results
        are rendered as ``div.search-result`` blocks, each holding a link to
        ``/manga/{slug}`` and a cover image served from the dedicated CDN.
        The site has no meaningful pagination (it returns every match on a
        single page), so the ``page`` argument is accepted for interface
        compatibility but only ``page == 1`` produces results.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        if page > 1:
            # MangaSee123 does not paginate search results.
            return

        url = f"{self._base_url}/search/?name={quote(query)}"
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        for card in tree.css("div.search-result, .search-result-item, .list-group-item"):
            link = card.css_first("a[href*='/manga/']")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title_node = card.css_first(".search-result-name, .manga-name, h3, h4")
            title = title_node.text(strip=True) if title_node is not None else ""

            # Cover images are lazy-loaded via data-src.
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
        """Return the :class:`Manga` for a MangaSee123 URL or slug.

        The manga page exposes the title in an ``h1`` and the metadata
        (author, genre, status, description) in a ``ul.list-group`` block.
        The cover URL is deterministic from the slug and is built directly
        from the dedicated CDN.
        """
        url = self._resolve_url(url_or_id)
        slug = self._extract_id(url)

        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        title_node = tree.css_first("h1")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else slug
        )

        # Cover is deterministic from the slug.
        cover_url = f"{_COVER_BASE}/{slug}.jpg"

        # Metadata — MangaSee123 renders it as a list of <li> entries, each
        # containing a <span> label (Author, Genre, Status, Description)
        # followed by the value.
        tags: list[str] = []
        status: str | None = None
        for li in tree.css("ul.list-group li"):
            span = li.css_first("span")
            if span is None:
                continue
            label = span.text(strip=True)
            # Remove the label from the node so that li.text() contains only
            # the value.
            span.decompose()
            value = li.text(strip=True)

            if label.startswith("Genre"):
                tags = [g.strip() for g in value.split(",") if g.strip()]
            elif label.startswith("Status"):
                lowered = value.lower()
                if "ongoing" in lowered:
                    status = "ongoing"
                elif "complete" in lowered:
                    status = "completed"
                elif "hiatus" in lowered:
                    status = "hiatus"
                elif "cancelled" in lowered or "discontinued" in lowered:
                    status = "cancelled"

        return Manga(
            id=self._make_manga_id(url),
            site_id=self.site_id,
            title=title,
            url=url,
            cover_url=cover_url,
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

        MangaSee123 embeds the chapter list as a ``vm.Chapters`` JavaScript
        array.  Each entry carries ``Chapter`` (a sentinel-prefixed numeric
        string) and ``ChapterName``.  We extract the array, normalise the
        numeric value, and build one :class:`Chapter` per entry.

        The array is already in chronological order (oldest first).
        """
        html = await self._get_rendered_html(manga.url)
        raw_chapters = self._extract_vm_array(html, _VM_CHAPTERS_RE)
        if raw_chapters is None:
            return []

        chapters: list[Chapter] = []
        for entry in raw_chapters:
            if not isinstance(entry, dict):
                continue

            raw_number = entry.get("Chapter")
            if not isinstance(raw_number, str) or not raw_number:
                continue

            number = self._normalise_chapter_number(raw_number)
            if number is None:
                continue

            chapter_name = entry.get("ChapterName") or ""
            label = (
                f"Chapter {number}"
                if not chapter_name
                else f"Chapter {number} - {chapter_name}"
            )

            # Chapter URLs follow the canonical
            # /read-online/{slug}-chapter-{n}-page-1.html pattern.  We
            # always point to page 1; the reader handles pagination.
            chapter_url = (
                f"{self._base_url}/read-online/"
                f"{self._extract_id(manga.url)}-chapter-{self._format_chapter_slug(number)}-page-1.html"
            )

            chapters.append(
                Chapter(
                    id=self._make_chapter_id(manga.id, chapter_url),
                    manga_id=manga.id,
                    site_id=self.site_id,
                    title=label,
                    url=chapter_url,
                    number=number,
                    language=self.language,
                )
            )

        return chapters

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page exposes two variables:

        * ``vm.CHAPTERS`` — array of per-chapter entries with a ``Page``
          field giving the page count.
        * ``vm.CurPathName`` — the image host.

        The current chapter's page count is obtained by matching the
        chapter's numeric value against the ``Chapter`` field of each
        entry.  Image URLs follow the pattern
        ``https://{CurPathName}/manga/{slug}/{chapter:0>4}-{page:0>3}.png``.
        """
        html = await self._get_rendered_html(chapter.url)

        raw_chapters = self._extract_vm_array(html, _VM_CHAPTERS_READER_RE)
        cur_path_name = self._extract_vm_string(html, _VM_CUR_PATH_NAME_RE)
        if raw_chapters is None or cur_path_name is None:
            return []

        # Locate the entry matching the current chapter number.
        target_number = chapter.number
        page_count: int | None = None
        for entry in raw_chapters:
            if not isinstance(entry, dict):
                continue
            raw_number = entry.get("Chapter")
            if not isinstance(raw_number, str):
                continue
            normalised = self._normalise_chapter_number(raw_number)
            if normalised is not None and normalised == target_number:
                raw_pages = entry.get("Page")
                if isinstance(raw_pages, str) and raw_pages.isdigit():
                    page_count = int(raw_pages)
                elif isinstance(raw_pages, int):
                    page_count = raw_pages
                break

        if page_count is None:
            return []

        slug = self._extract_id(chapter.url)
        chapter_segment = f"{int(target_number):04d}"

        pages: list[Page] = []
        for page_number in range(1, page_count + 1):
            page_segment = f"{page_number:03d}"
            pages.append(
                Page(
                    index=page_number - 1,
                    url=(
                        f"https://{cur_path_name}/manga/{slug}/"
                        f"{chapter_segment}-{page_segment}.png"
                    ),
                    chapter_id=chapter.id,
                )
            )

        return pages

    # ------------------------------------------------------------------ #
    #  helpers                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_vm_array(html: str, pattern: re.Pattern[str]) -> list[Any] | None:
        """Extract and parse a ``vm.* = [ ... ];`` JavaScript array.

        The regex captures the JSON-ish array literal.  MangaSee123 emits
        plain JSON (double-quoted keys and string values), so ``json.loads``
        parses it directly.  Returns ``None`` when the variable is absent or
        malformed.
        """
        match = pattern.search(html)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None

    @staticmethod
    def _extract_vm_string(html: str, pattern: re.Pattern[str]) -> str | None:
        """Extract a string-valued ``vm.* = "...";`` JavaScript variable."""
        match = pattern.search(html)
        if match is None:
            return None
        value = match.group(1).strip()
        return value or None

    @staticmethod
    def _normalise_chapter_number(raw: str) -> float | None:
        """Convert a sentinel-prefixed chapter string to a float.

        MangaSee123 encodes ``1`` as ``"100010"``, ``2`` as ``"100020"``,
        and so on.  The leading ``1`` is a sentinel and the trailing zeros
        are padding.  We strip the sentinel, divide by the padding factor,
        and return the resulting float.  Returns ``None`` for malformed
        inputs.
        """
        if not raw.startswith(_CHAPTER_SENTINEL):
            return None
        try:
            value = int(raw[len(_CHAPTER_SENTINEL):])
        except ValueError:
            return None
        # The encoded value is the chapter number multiplied by 10.
        return value / 10.0

    @staticmethod
    def _format_chapter_slug(number: float) -> str:
        """Format a chapter number for use in the canonical reader URL.

        Integer chapters are rendered without a decimal part (``1`` rather
        than ``1.0``); fractional chapters keep their decimal part.  The
        site accepts both forms, but this matches the URLs it generates
        itself.
        """
        if number.is_integer():
            return str(int(number))
        return str(number)


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("mangasee123", MangaSee123Parser)
