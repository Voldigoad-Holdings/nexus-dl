"""Parser for Comick.io — English manga aggregator (API-driven + Cloudflare).

Comick.io (formerly ComicK) is a large English-language manga/manhwa
aggregator with a well-documented public JSON API.  Unlike HTML-scraping
parsers, this module talks directly to the API, which gives structured
metadata, reliable pagination, and predictable image URLs.

API surface used
----------------
All endpoints are rooted at ``https://api.comick.fun``:

* Search           — ``GET  /v1.0/search/``
  Query parameters: ``q`` (query), ``page``, ``limit``.
* Manga details    — ``GET  /comic/{hid}`` (``hid`` is the comic's unique
  hexadecimal id; the ``slug`` can also be used with ``/comic/{slug}/``).
* Chapter list     — ``GET  /comic/{hid}/chapters``
  Query parameters: ``limit`` (max 100), ``page``, ``lang``.
* Chapter pages    — ``GET  /chapter/{hid}/get_images``
  Returns the ordered list of image keys for the chapter.

Image URL pattern
-----------------
All page images are served from a single CDN:

    ``https://meo.comick.pictures/{b2key}``

where ``b2key`` is the per-page key returned by the ``get_images`` endpoint.

Cloudflare
----------
Comick.io sits behind Cloudflare.  The parser therefore inherits from
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` so that requests
are routed through FlareSolverr / Playwright when a challenge is detected.

Pagination
----------
Both search and chapter listings are paginated.  ``search`` yields results
page by page via :class:`~collections.abc.AsyncIterator`.
:meth:`get_chapters` transparently follows every page of the chapter
listing until the API stops returning results, so callers always receive
the complete chapter list in a single call.

Language
--------
The site is English-facing (``language = "en"``) and general-audience
(``adult = False`` by default).  Adult content is opt-in through the
site's own settings and is governed by ``Settings.content.allow_adult``,
exactly like every other parser.
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

__all__ = ["ComickParser"]

_API_BASE = "https://api.comick.fun"
_IMAGE_BASE = "https://meo.comick.pictures"

# The API returns at most 100 chapters per page.  Larger values are
# silently clamped server-side, so we use the documented maximum.
_CHAPTERS_PER_PAGE = 100


class ComickParser(ApiBasedMixin, CloudflareMixin, BaseParser):
    """Parser for Comick.io (API-driven + Cloudflare, English)."""

    site_id: str = "comick"
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
        """Search Comick and yield results page by page.

        The search endpoint returns a JSON array where each entry describes
        one comic.  We yield one :class:`SearchResult` per comic.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        url = f"{_API_BASE}/v1.0/search/"
        params: dict[str, Any] = {"q": query, "page": page}
        data = await self.session.get_json(url, params=params)

        # The API wraps the results in a top-level list under normal
        # operation; some deployments return a dict with a ``result`` key.
        items: list[dict[str, Any]]
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("result", [])
        else:
            items = []

        for item in items:
            hid = item.get("hid") or item.get("slug")
            if not hid:
                continue

            title = (
                item.get("title")
                or item.get("name")
                or hid
            )

            cover_url = item.get("cover_url") or item.get("md_covers", [{}])[0].get("b2key")
            if cover_url and not cover_url.startswith("http"):
                cover_url = f"{_IMAGE_BASE}/{cover_url}"

            yield SearchResult(
                site_id=self.site_id,
                title=title,
                url=f"https://comick.io/comic/{hid}",
                cover_url=cover_url,
                language=self.language,
                adult=self.adult,
            )

    # ------------------------------------------------------------------ #
    #  get_manga                                                         #
    # ------------------------------------------------------------------ #
    async def get_manga(self, url_or_id: str) -> Manga:
        """Return the :class:`Manga` for a comic URL or id.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare ``hid`` (as stored in the local library).
        The ``hid`` is extracted via :meth:`BaseParser._extract_id`.
        """
        comic_id = self._extract_id(url_or_id)
        url = f"{_API_BASE}/comic/{comic_id}"
        data = await self.session.get_json(url)

        # The API returns the comic object directly.
        title = (
            data.get("title")
            or data.get("name")
            or comic_id
        )

        cover_key = data.get("cover_url") or data.get("md_covers", [{}])[0].get("b2key")
        cover_url = f"{_IMAGE_BASE}/{cover_key}" if cover_key else None

        tags: list[str] = []
        for tag in data.get("md_comic_md_genres", []):
            name = tag.get("md_genres", {}).get("name")
            if name:
                tags.append(name)

        return Manga(
            id=self._make_manga_id(f"https://comick.io/comic/{comic_id}"),
            site_id=self.site_id,
            title=title,
            url=f"https://comick.io/comic/{comic_id}",
            cover_url=cover_url,
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

        The API paginates chapters in blocks of at most
        ``_CHAPTERS_PER_PAGE``.  This method follows every page until the
        server returns an empty array, then returns the deduplicated list
        in the order provided by the API (which is chronological, oldest
        first).
        """
        comic_id = self._extract_id(manga.url)
        chapters: dict[str, Chapter] = {}
        page = 1

        while True:
            url = f"{_API_BASE}/comic/{comic_id}/chapters"
            params: dict[str, Any] = {
                "limit": _CHAPTERS_PER_PAGE,
                "page": page,
            }
            data = await self.session.get_json(url, params=params)

            # The API returns a dict with a ``chapters`` array.
            batch: list[dict[str, Any]]
            if isinstance(data, dict):
                batch = data.get("chapters", [])
            elif isinstance(data, list):
                batch = data
            else:
                batch = []

            if not batch:
                break

            for item in batch:
                chapter_id = item.get("hid")
                if not chapter_id or chapter_id in chapters:
                    continue

                label = item.get("title") or item.get("chap") or chapter_id
                number: float | None = None
                raw_number = item.get("chap")
                if raw_number is not None:
                    try:
                        number = float(raw_number)
                    except (TypeError, ValueError):
                        number = None

                chapters[chapter_id] = Chapter(
                    id=self._make_chapter_id(manga.id, f"https://comick.io/comic/{comic_id}/{chapter_id}"),
                    manga_id=manga.id,
                    site_id=self.site_id,
                    title=str(label),
                    url=f"https://comick.io/comic/{comic_id}/{chapter_id}",
                    number=number,
                    language=self.language,
                )

            page += 1

        # The API returns chapters oldest-first; preserve that order so
        # downstream packaging and library sorting behave predictably.
        return list(chapters.values())

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The chapter URL embeds the comic ``hid`` and the chapter ``hid``.
        We extract the chapter ``hid`` and query the ``get_images``
        endpoint, which returns an ordered list of ``b2key`` values.
        """
        chapter_id = self._extract_chapter_id(chapter.url)
        url = f"{_API_BASE}/chapter/{chapter_id}/get_images"
        data = await self.session.get_json(url)

        # The endpoint returns a dict with an ``images`` array, where each
        # entry is either a string (the b2key) or an object with a
        # ``b2key`` field.  Normalise both shapes.
        raw_images: list[Any]
        if isinstance(data, dict):
            raw_images = data.get("images", [])
        elif isinstance(data, list):
            raw_images = data
        else:
            raw_images = []

        pages: list[Page] = []
        for index, entry in enumerate(raw_images):
            if isinstance(entry, str):
                key = entry
            elif isinstance(entry, dict):
                key = entry.get("b2key") or entry.get("name")
            else:
                key = None

            if not key:
                continue

            pages.append(
                Page(
                    index=index,
                    url=f"{_IMAGE_BASE}/{key}",
                    chapter_id=chapter.id,
                )
            )

        return pages


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("comick", ComickParser)
