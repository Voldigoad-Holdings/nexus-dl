"""Template for adding a new site parser to NexusDL.

Copy this file to ``src/nexusdl/parsers/custom/<your_site>.py`` and follow
these steps:

1. Rename the file to a snake_case version of your site id
   (``my_site.py`` for ``site_id = "my_site"``).
2. Rename ``TemplateParser`` to ``<PascalCaseName>Parser``.
3. Fill in ``site_id``, ``language`` and ``adult`` on the class.  The
   ``site_id`` must match the entry you add to ``data/sites.yaml`` and
   satisfy ``^[a-z][a-z0-9_-]*$`` (enforced by ``sites_schema.json``).
4. Add the site entry to ``data/sites.yaml``.  Required fields are listed
   in ``sites_schema.json``; the minimum is ``id``, ``name``, ``domains``,
   ``parser_class``, ``language``, ``adult`` and ``capabilities``.
5. Replace the selectors and URLs in the four methods below with the
   site's actual markup.  All network I/O must go through
   ``self.session`` — never use ``httpx`` / ``aiohttp`` directly, so that
   retries, rate limiting, circuit breaking, proxy rotation and cookie
   persistence are applied uniformly.
6. Register the parser at the bottom of your file::

       from nexusdl.parsers.factory import register
       register("<your_site_id>", <YourParserClass>)

7. Import your module from ``src/nexusdl/parsers/custom/__init__.py`` so
   the ``register(...)`` call fires at import time (the top-level
   ``nexusdl.parsers`` package imports ``custom`` automatically).

This template is **not** auto-registered: it contains placeholder values
and must never be imported by ``nexusdl.parsers.custom.__init__``.

Refer to ``docs/source/development/adding_parsers.md`` for the full guide,
and to any shipped parser under ``parsers/fr``, ``parsers/en``, ``parsers/kr``
or ``parsers/adult`` for concrete implementations of each strategy
(WordPress Madara, MangaThemesia, FoolSlide, API-driven, JS-rendered,
Cloudflare-protected).  If your site matches one of those stacks, you do
not need to write the four methods by hand — inherit from the
corresponding mixin under ``parsers/mixins`` and let it do the work.
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

__all__ = ["TemplateParser"]


class TemplateParser(BaseParser):
    """Parser skeleton for a new HTML-scraped site.

    The implementation below is fully functional against a hypothetical
    site whose search, manga, chapter and reader pages follow the markup
    patterns used throughout the shipped parsers.  Replace the URLs and
    CSS selectors with the actual site markup and you have a working
    parser.
    """

    site_id: str = "template"
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
        """Yield one :class:`SearchResult` per match for *query*.

        Replace the URL template and CSS selectors with the site's actual
        search route and card markup.  The pattern shown is the most common
        one: a GET request with ``q`` and ``page`` query parameters, cards
        inside a results container, each card holding a link and a cover
        image.
        """
        url = f"{self._base_url}/search?q={quote(query)}&page={page}"
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        for card in tree.css("div.search-result"):
            link = card.css_first("a.result-link")
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href:
                continue

            title = link.text(strip=True) or href

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
        """Return the :class:`Manga` for *url_or_id*.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare source id (as stored in the local
        library).  :meth:`BaseParser._resolve_url` normalises both cases.

        The returned ``Manga.id`` must be derived via
        :meth:`BaseParser._make_manga_id` so that the id stays stable
        across sessions and matches what the library database stores.
        """
        url = self._resolve_url(url_or_id)
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        title_node = tree.css_first("h1.manga-title")
        title = (
            title_node.text(strip=True)
            if title_node is not None
            else self._extract_id(url)
        )

        cover_node = tree.css_first(".manga-cover img")
        cover_url = ""
        if cover_node is not None:
            cover_url = (
                cover_node.attributes.get("data-src")
                or cover_node.attributes.get("src")
                or ""
            )

        tags = [
            node.text(strip=True)
            for node in tree.css("a.tag")
            if node.text(strip=True)
        ]

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

        The same chapter may appear multiple times in the DOM (for example
        in a "latest updates" sidebar).  Deduplicate by
        :meth:`BaseParser._extract_chapter_id` and derive each
        ``Chapter.id`` via :meth:`BaseParser._make_chapter_id`.

        ``number`` should be the numeric chapter value when the site
        exposes it; return ``None`` otherwise so the library can fall back
        to insertion order when sorting.
        """
        html = await self.session.get_html(manga.url)
        tree = HTMLParser(html)

        chapters: dict[str, Chapter] = {}
        for link in tree.css("a.chapter-link"):
            href = link.attributes.get("href", "")
            if not href:
                continue

            source_id = self._extract_chapter_id(href)
            if not source_id or source_id in chapters:
                continue

            label = link.text(strip=True) or source_id
            chapters[source_id] = Chapter(
                id=self._make_chapter_id(manga.id, href),
                manga_id=manga.id,
                site_id=self.site_id,
                title=label,
                url=self._resolve_url(href),
                number=self._parse_number(label),
                language=self.language,
            )

        return list(chapters.values())

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        ``index`` is 0-based and must reflect the reading order of the
        chapter.  Image URLs must be absolute; if the site serves
        lazy-loaded images, prefer ``data-src`` over ``src`` so we do not
        download placeholder images.
        """
        html = await self.session.get_html(chapter.url)
        tree = HTMLParser(html)

        pages: list[Page] = []
        for index, img in enumerate(tree.css("div.reader img")):
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
