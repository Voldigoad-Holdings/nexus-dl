"""Parser for ToonGod — English manhwa/webtoon aggregator (custom + Cloudflare).

ToonGod (``toongod.cc`` / ``toongod.org``) is a custom WordPress-based
aggregator that hosts manhwa and webtoon chapters.  It is **not** a stock
Madara site: the URL pattern is ``/webtoon/{slug}/`` for series and
``/webtoon/{slug}/chapter-{number}/`` for chapters, and the chapter list is
rendered client-side by JavaScript.

The site is additionally fronted by **Cloudflare**, which means a plain HTTP
client is regularly challenged and must be routed through FlareSolverr /
Playwright.

To keep this parser robust and aligned with the project's one-file
philosophy, it combines two mixins:

* :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` transparently
  upgrades a request to a browser-backed solve when a Cloudflare challenge is
  detected, then replays the original request with the obtained cookies.
* :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` renders pages
  through the shared Playwright pool, which executes the site's JavaScript and
  injects the chapter list and reader images into the DOM before scraping.

Route patterns
--------------
* Search           — ``GET  /search/?s={query}``
* Manga details    — ``GET  /webtoon/{slug}/``
* Chapter list     — embedded in the manga page as ``a[href*="/chapter-"]``
* Reader           — ``GET  /webtoon/{slug}/chapter-{number}/``

Images live inside ``div.reading-img > img``.  They are lazy-loaded via
``data-src``, so the parser prefers that attribute over ``src`` to avoid
downloading placeholder images.

The site is English-language and general-audience (non-adult) by declaration;
adult filtering is governed by ``Settings.content.allow_adult`` at the session
level, exactly like every other general-purpose parser.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the base URL.  ToonGod has migrated between ``.cc`` and ``.org``; if it moves
again, only ``sites.yaml`` needs updating — this file stays untouched.
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

__all__ = ["ToonGodParser"]


class ToonGodParser(JsRenderedMixin, CloudflareMixin, BaseParser):
    """Parser for ToonGod (custom WordPress + Cloudflare, English)."""

    site_id: str = "toongod"
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
        """Search ToonGod and yield results page by page.

        The search form submits to ``/search/`` with an ``s`` GET parameter.
        Results are rendered as a grid of comic cards; each card links to a
        series under ``/webtoon/`` and contains a cover image.  The site does
        not expose a meaningful pagination for search, so ``page > 1`` yields
        nothing.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        if page > 1:
            # ToonGod returns every match on a single page.
            return

        url = f"{self._base_url}/search/?s={quote(query)}"
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        # The results grid uses comic cards.  We target any anchor that
        # points to a /webtoon/ series and extract the surrounding block.
        for link in tree.css("a[href*='/webtoon/']"):
            href = link.attributes.get("href", "")
            if not href or href.endswith("/webtoon/"):
                # Skip the /webtoon/ index page itself.
                continue

            # Title — either the anchor text or the alt text of the cover.
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
        """Return the :class:`Manga` for a ToonGod URL or slug.

        The series page exposes the title in an ``h1`` and the cover in a
        nearby ``img`` (usually inside a ``.cover`` or ``.manga-cover``
        block).  Genres and status are available as free text in the
        metadata section.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare slug (as stored in the local library).
        """
        url = self._resolve_url(url_or_id)
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        # Title — the site uses a plain h1, sometimes with a .title class.
        title_node = tree.css_first("h1, .title, .manga-title")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        # Cover — usually the first substantial image on the page.
        cover_node = tree.css_first(
            ".cover img, .manga-cover img, .series-cover img, article img"
        )
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        # Tags / genres — rendered as anchors pointing to genre pages.
        tags: list[str] = []
        for tag_node in tree.css("a[href*='/genre/']"):
            text = tag_node.text(strip=True)
            if text:
                tags.append(text)

        # Status — exposed as free text in the metadata block.
        status: str | None = None
        for node in tree.css(".status, .manga-status, .series-status"):
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

        ToonGod injects the chapter list via JavaScript.  Because the parser
        inherits :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`,
        the page is rendered through Playwright and the anchors are present
        in the resulting DOM.  Every chapter anchor points to
        ``/webtoon/{slug}/chapter-{number}/``; we deduplicate by chapter id
        and return the list in chronological order.

        The site lists chapters newest-first, so the final list is reversed
        before being returned.
        """
        html = await self._get_rendered_html(manga.url)
        tree = HTMLParser(html)

        chapters: dict[str, Chapter] = {}
        for link in tree.css("a[href*='/chapter-']"):
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

        # ToonGod lists chapters newest-first; reverse to chronological
        # order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader page renders images inside ``div.reading-img > img``.
        Images are lazy-loaded via ``data-src``, so the parser prefers that
        attribute over ``src`` to avoid downloading placeholder images.
        Because the page is JavaScript-rendered, the parser relies on
        :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` to
        produce the final DOM.
        """
        html = await self._get_rendered_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        for index, img in enumerate(tree.css("div.reading-img > img")):
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

        ToonGod labels look like ``"Chapter 99"``, ``"99"`` or
        ``"Chapter 99 - Title"``.  The first numeric token is used.
        """
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("toongod", ToonGodParser)
