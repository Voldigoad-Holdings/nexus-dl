"""Parser for pururin.to — adult doujinshi gallery.

Pururin is a JavaScript-heavy SPA (Vue.js + Laravel) where the gallery,
chapter and reader views are rendered client-side.  Static HTTP requests
return almost no useful markup, so this parser delegates all page
acquisition to :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`,
which renders pages through the shared Playwright pool before scraping.

Route shapes (observed on ``pururin.to``):

* Gallery  — ``/gallery/{id}/{slug}``          → manga details + chapter list
* Reader   — ``/read/{id}/{chapter}/{page}``   → page images

Search is performed against the site's public search endpoint.  Because the
front-end is an SPA, the mixin waits for the results container to appear
before extracting cards.

This module only declares the site identity and registers the parser.  All
selector logic lives in the mixin overrides below so that if the site's
markup changes, only this file needs updating.
"""
from __future__ import annotations

from typing import AsyncIterator

from selectolax.parser import HTMLParser

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.js_rendered import JsRenderedMixin

__all__ = ["PururinParser"]


class PururinParser(JsRenderedMixin, BaseParser):
    """Parser for pururin.to (JavaScript-rendered, adult, English UI)."""

    site_id: str = "pururin"
    language: str = "en"
    adult: bool = True

    # ------------------------------------------------------------------ #
    #  Search                                                            #
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator[SearchResult]:
        """Search pururin and yield results page by page.

        The front-end exposes a search route that accepts a ``q`` query
        parameter and a ``page`` parameter.  Results are rendered into a
        grid of ``article`` elements; each card contains a link to
        ``/gallery/{id}/{slug}`` and a cover image.
        """
        url = f"{self._base_url}/search?q={query}&page={page}"
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        # The results container is a grid of article elements.  Each article
        # holds an anchor to the gallery and a cover image.
        for card in tree.css("article.gallery-card, div.gallery-card"):
            link = card.css_first("a[href*='/gallery/']")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            title_node = card.css_first("h2, h3, .title")
            title = title_node.text(strip=True) if title_node else ""

            cover_node = card.css_first("img")
            cover_url = ""
            if cover_node is not None:
                # Lazy-loaded images use data-src; eager images use src.
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
    #  Manga details                                                     #
    # ------------------------------------------------------------------ #
    async def get_manga(self, url_or_id: str) -> Manga:
        """Return the manga represented by a gallery URL or id.

        A gallery page exposes the title, cover, artist, tags and the list
        of chapters (``/read/{id}/{chapter}/...``).
        """
        url = self._resolve_url(url_or_id)
        html = await self._get_rendered_html(url)
        tree = HTMLParser(html)

        title_node = tree.css_first("h1.gallery-title, h1.title")
        title = title_node.text(strip=True) if title_node else self._extract_id(url)

        cover_node = tree.css_first(".gallery-cover img, .cover img")
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        # Tags / artists — the site groups metadata in a definition list.
        tags: list[str] = []
        for tag_node in tree.css("a.tag, .tags a"):
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
    #  Chapter list                                                      #
    # ------------------------------------------------------------------ #
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the ordered list of chapters for *manga*.

        Chapter links follow the pattern ``/read/{id}/{chapter_slug}/...``.
        The first page of each chapter is sufficient to identify it.
        """
        html = await self._get_rendered_html(manga.url)
        tree = HTMLParser(html)

        chapters: dict[str, Chapter] = {}
        for link in tree.css("a[href*='/read/']"):
            href = link.attributes.get("href", "")
            chapter_id = self._extract_chapter_id(href)
            if not chapter_id:
                continue

            # Deduplicate — the same chapter may appear multiple times.
            if chapter_id in chapters:
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

        # Pururin lists chapters newest-first; reverse to chronological order.
        return list(reversed(list(chapters.values())))

    # ------------------------------------------------------------------ #
    #  Page list                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The reader view renders one image per page inside a container.  The
        image URL is usually lazy-loaded via ``data-src``.
        """
        html = await self._get_rendered_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        for index, img in enumerate(tree.css(".reader img, .page-image img, img.page")):
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
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_chapter_number(label: str) -> float | None:
        """Extract a numeric chapter number from a label, if present."""
        import re

        match = re.search(r"(\d+(?:\.\d+)?)", label)
        return float(match.group(1)) if match else None


# Auto-registration — triggered when ``nexusdl.parsers.adult`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("pururin", PururinParser)
