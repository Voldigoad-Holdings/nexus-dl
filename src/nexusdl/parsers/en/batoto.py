"""Parser for Bato.to — English manga aggregator (JS-rendered reader).

Bato.to (formerly Batoto) is a large English-language manga aggregator with a
modern Vue/Nuxt front-end.  Unlike classic WordPress sites, the reader pages
embed image URLs inside obfuscated JavaScript (JSFuck + AES) and only inject
the real ``<img>`` elements after client-side execution.  Static HTTP scraping
therefore cannot obtain the page list reliably.

To keep the parser maintainable and robust, this module delegates all HTML
acquisition to
:class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`, which renders
pages through the shared Playwright pool before handing the resulting DOM to
the selectors below.  This mirrors how the reference Go scraper
(``mangoprovider``) handles the same site[reference:0][reference:1].

Route patterns
--------------
* Search           — ``GET  /search?word={query}``[reference:2]
* Series details   — ``GET  /series/{id}``[reference:3]
* Chapter list     — embedded in the series page (links to ``/chapter/…``)
* Reader           — ``GET  /chapter/{id}``

The site is general-audience and English-facing, declared on the class so the
registry can filter it via ``Settings.content.allow_adult`` and the
``language`` filter of ``SiteRegistry.list_sites``.

No Cloudflare mixin is inherited: bato.to does not sit behind a Cloudflare
challenge in normal operation.  If a challenge is ever encountered, the
``JsRenderedMixin`` already routes through Playwright, which solves standard
JS challenges as a side effect.
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
from nexusdl.parsers.mixins.js_rendered import JsRenderedMixin

__all__ = ["BatoToParser"]


class BatoToParser(JsRenderedMixin, BaseParser):
    """Parser for Bato.to (JavaScript-rendered reader, English)."""

    site_id: str = "batoto"
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
        """Search Bato.to and yield results page by page.

        The search route accepts a ``word`` query parameter and returns a
        grid of ``div.item`` cards inside ``div.series-list``[reference:4].
        Each card holds the series link, title and cover image.
        """
        url = f"{self._base_url}/search?word={quote(query)}&page={page}"
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        for card in tree.css("div.series-list > div.item"):
            link = card.css_first("div.item-text > a.item-title")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title = link.text(strip=True) or href

            cover_node = card.css_first("a.item-cover > img")
            cover_url = ""
            if cover_node is not None:
                cover_url = (
                    cover_node.attributes.get("data-src")
                    or cover_node.attributes.get("src")
                    or ""
                )

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
        """Return the :class:`Manga` for a series URL or id.

        The series page exposes the title inside
        ``div.title-set > h3.item-title > a`` and the cover inside
        ``div.detail-set > div.attr-cover > img``[reference:5].
        """
        url = self._resolve_url(url_or_id)
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        title_node = tree.css_first("div.title-set > h3.item-title > a")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        cover_node = tree.css_first("div.detail-set > div.attr-cover > img")
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        # Tags / genres are rendered as anchor tags in the detail block.
        tags: list[str] = []
        for tag_node in tree.css("div.detail-set a[href*='/genre/']"):
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
        """Return the ordered chapter list for *manga*.

        Chapter links are embedded in the series page and follow the
        pattern ``/chapter/{id}``.  They may appear multiple times (e.g. in
        a “latest” widget), so we deduplicate by chapter id.
        """
        html = await self._get_rendered_html(manga.url)
        tree = HTMLParser(html)

        chapters: dict[str, Chapter] = {}
        for link in tree.css("a[href*='/chapter/']"):
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
                number=self._parse_number(label),
                language=self.language,
            )

        # Bato.to lists chapters newest-first in the DOM; reverse to
        # chronological order so the downloader processes them sensibly.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader is a JavaScript application that injects the actual
        image elements after decoding the page URLs.  Rendering the page
        with Playwright gives us a DOM where each page is an ``<img>``
        inside the viewer container.
        """
        html = await self._get_rendered_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        # The viewer container class is stable across reader versions.
        for index, img in enumerate(tree.css("div#viewer img, div.reader img, img.page-img")):
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
    def _parse_number(label: str) -> float | None:
        """Extract a numeric chapter value from a free-form label."""
        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("batoto", BatoToParser)
