"""Parser for nhentai.net — adult doujinshi gallery (API-driven).

nhentai exposes a reverse-engineered JSON API that powers both the website
and third-party clients.  Unlike HTML-scraping parsers, this module talks
directly to the API, which gives structured metadata and predictable image
URLs.

API surface used
----------------
* Search           — ``GET  https://nhentai.net/api/galleries/search``
  Query parameters: ``query``, ``page``, ``sort`` (``popular``, ``date``).
* Gallery details  — ``GET  https://nhentai.net/api/gallery/{id}``

Image URL patterns
------------------
* Page   — ``https://i.nhentai.net/galleries/{media_id}/{page}.{ext}``
* Thumb  — ``https://t.nhentai.net/galleries/{media_id}/thumb.{ext}``
* Cover  — ``https://t.nhentai.net/galleries/{media_id}/cover.{ext}``

The ``ext`` is derived from the per-page ``t`` field (``j`` → jpg,
``p`` → png, ``g`` → gif).

Cloudflare
----------
nhentai.net sits behind Cloudflare.  The parser therefore inherits from
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` so that requests
are routed through FlareSolverr / Playwright when a challenge is detected.

A gallery on nhentai is a self-contained doujinshi: there are no nested
chapters.  :meth:`get_chapters` returns a single :class:`Chapter` that
represents the whole gallery, which keeps the downloader’s chapter-oriented
pipeline unchanged.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register
from nexusdl.parsers.mixins.api_based import ApiBasedMixin
from nexusdl.parsers.mixins.cloudflare import CloudflareMixin

__all__ = ["NHentaiParser"]

_API_BASE = "https://nhentai.net/api"
_IMAGE_BASE = "https://i.nhentai.net/galleries"
_THUMB_BASE = "https://t.nhentai.net/galleries"

_EXT_MAP: dict[str, str] = {
    "j": "jpg",
    "p": "png",
    "g": "gif",
}


class NHentaiParser(ApiBasedMixin, CloudflareMixin, BaseParser):
    """Parser for nhentai.net (API-driven, adult, English UI)."""

    site_id: str = "nhentai"
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
        """Search nhentai and yield results page by page.

        The API returns a ``result`` array where each entry is a gallery
        summary.  We yield one :class:`SearchResult` per gallery.

        Args:
            query: Search terms (e.g. ``"language:english artist:xyz"``).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        url = f"{_API_BASE}/galleries/search"
        params: dict[str, Any] = {"query": query, "page": page, "sort": "popular"}
        data = await self.session.get_json(url, params=params)

        for item in data.get("result", []):
            gallery_id = str(item["id"])
            title = item.get("title", {}).get("pretty") or item.get("title", {}).get("english") or gallery_id

            media_id = str(item.get("media_id", ""))
            cover_ext = _EXT_MAP.get(item.get("images", {}).get("cover", {}).get("t", "j"), "jpg")
            cover_url = f"{_THUMB_BASE}/{media_id}/cover.{cover_ext}" if media_id else None

            tags = [
                tag.get("name", "")
                for tag in item.get("tags", [])
                if tag.get("type") in ("tag", "artist", "group", "parody")
            ]

            yield SearchResult(
                site_id=self.site_id,
                title=title,
                url=f"https://nhentai.net/g/{gallery_id}/",
                cover_url=cover_url,
                language=self.language,
                adult=self.adult,
            )

    # ------------------------------------------------------------------ #
    #  Manga details                                                     #
    # ------------------------------------------------------------------ #
    async def get_manga(self, url_or_id: str) -> Manga:
        """Return the :class:`Manga` for a gallery URL or numeric id.

        A gallery URL looks like ``https://nhentai.net/g/177013/``; the
        numeric id is extracted via :meth:`BaseParser._extract_id`.
        """
        gallery_id = self._extract_id(url_or_id)
        url = f"{_API_BASE}/gallery/{gallery_id}"
        data = await self.session.get_json(url)

        title = data.get("title", {}).get("pretty") or data.get("title", {}).get("english") or gallery_id
        media_id = str(data.get("media_id", ""))

        cover_ext = _EXT_MAP.get(data.get("images", {}).get("cover", {}).get("t", "j"), "jpg")
        cover_url = f"{_THUMB_BASE}/{media_id}/cover.{cover_ext}" if media_id else None

        tags = [
            tag.get("name", "")
            for tag in data.get("tags", [])
            if tag.get("type") in ("tag", "artist", "group", "parody", "category")
        ]

        return Manga(
            id=self._make_manga_id(f"https://nhentai.net/g/{gallery_id}/"),
            site_id=self.site_id,
            title=title,
            url=f"https://nhentai.net/g/{gallery_id}/",
            cover_url=cover_url,
            language=self.language,
            adult=self.adult,
            tags=tags,
            status=None,  # nhentai has no ongoing/completed status.
        )

    # ------------------------------------------------------------------ #
    #  Chapter list                                                      #
    # ------------------------------------------------------------------ #
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return a single synthetic chapter for the gallery.

        nhentai galleries are monolithic doujinshi; the API does not expose
        sub-chapters.  We therefore emit exactly one :class:`Chapter` whose
        id is derived from the manga id so that downstream packaging and
        library code see the usual chapter-oriented structure.
        """
        chapter_url = manga.url
        chapter_id = self._make_chapter_id(manga.id, chapter_url)

        return [
            Chapter(
                id=chapter_id,
                manga_id=manga.id,
                site_id=self.site_id,
                title=manga.title,
                url=chapter_url,
                number=1.0,
                language=self.language,
            )
        ]

    # ------------------------------------------------------------------ #
    #  Page list                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of a gallery chapter.

        The gallery id is extracted from the chapter URL, then the API is
        queried once to obtain the ``media_id`` and the per-page image
        metadata.  Image URLs are built deterministically from those
        values.
        """
        gallery_id = self._extract_id(chapter.url)
        url = f"{_API_BASE}/gallery/{gallery_id}"
        data = await self.session.get_json(url)

        media_id = str(data.get("media_id", ""))
        if not media_id:
            return []

        pages: list[Page] = []
        for index, page_meta in enumerate(data.get("images", {}).get("pages", [])):
            ext = _EXT_MAP.get(page_meta.get("t", "j"), "jpg")
            # nhentai page numbering starts at 1.
            page_url = f"{_IMAGE_BASE}/{media_id}/{index + 1}.{ext}"
            pages.append(
                Page(
                    index=index,
                    url=page_url,
                    chapter_id=chapter.id,
                )
            )

        return pages


# Auto-registration — triggered when ``nexusdl.parsers.adult`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("nhentai", NHentaiParser)
