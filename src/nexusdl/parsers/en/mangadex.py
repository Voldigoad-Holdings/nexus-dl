"""Parser for MangaDex — English manga aggregator (official API v5).

MangaDex is the largest open manga database and exposes a well-documented,
versioned REST API (v5).  Unlike HTML-scraping parsers, this module talks
directly to the API, which gives structured metadata, reliable pagination,
and geographically optimised image servers.

API surface used
----------------
All endpoints are rooted at ``https://api.mangadex.org``:

* Search           — ``GET  /manga``
  Query parameters: ``title``, ``limit`` (max 100), ``offset``,
  ``includes[]=cover_art``, ``availableTranslatedLanguage[]=en``,
  ``contentRating[]``.
* Manga details    — ``GET  /manga/{id}``
  Query parameters: ``includes[]=cover_art``.
* Chapter list     — ``GET  /manga/{id}/feed``
  Query parameters: ``translatedLanguage[]=en``, ``limit`` (max 500),
  ``offset``, ``order[chapter]=asc``.
* Chapter pages    — ``GET  /at-home/server/{chapterId}``
  Returns ``baseUrl``, ``chapter.hash`` and the ordered list of filenames
  under ``chapter.data`` (original quality) and ``chapter.dataSaver``
  (compressed).

Image URL patterns
------------------
* Page   — ``{baseUrl}/data/{chapterHash}/{filename}``
* Cover  — ``https://uploads.mangadex.org/covers/{mangaId}/{fileName}``

Authentication
--------------
All endpoints used by this parser are **public** and require no
authentication.  Personal API clients are only needed for write operations,
followed-manga lists, and other user-scoped resources, which are out of
scope for a downloader parser.  No authentication mixin is therefore
inherited.

Adult content
-------------
MangaDex hosts both general-audience and adult content.  The parser itself
is declared ``adult = False`` because the *site* is general-purpose — the
adult filter is applied at **search time** via the ``contentRating[]``
query parameter, driven by ``Settings.content.allow_adult``.  This mirrors
the behaviour of every other general-purpose parser in the project and
keeps the site available to users who disable adult content.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth for
the site's base URL.  If the site ever changes domains, only ``sites.yaml``
needs updating — this file stays untouched.
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

__all__ = ["MangaDexParser"]

_API_BASE = "https://api.mangadex.org"
_COVER_BASE = "https://uploads.mangadex.org/covers"

# API limits — larger values are silently clamped server-side.
_SEARCH_LIMIT = 100
_FEED_LIMIT = 500

# Content ratings exposed by the API, split by adult gate.
_SAFE_RATINGS: tuple[str, ...] = ("safe", "suggestive")
_ADULT_RATINGS: tuple[str, ...] = ("erotica", "pornographic")


class MangaDexParser(ApiBasedMixin, BaseParser):
    """Parser for MangaDex (official API v5, English)."""

    site_id: str = "mangadex"
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
        """Search MangaDex and yield results page by page.

        The ``/manga`` endpoint returns a paginated envelope whose ``data``
        array holds manga summaries.  Each entry carries a ``cover_art``
        relationship when ``includes[]=cover_art`` is supplied, which lets
        us build the cover URL without a second request.

        Args:
            query: Search terms (title, author, etc.).
            page: 1-based page number.

        Yields:
            SearchResult instances for the current page.
        """
        offset = (page - 1) * _SEARCH_LIMIT
        params: dict[str, Any] = {
            "title": query,
            "limit": _SEARCH_LIMIT,
            "offset": offset,
            "includes[]": ["cover_art"],
            "availableTranslatedLanguage[]": [self.language],
            "contentRating[]": self._allowed_content_ratings(),
        }
        data = await self.session.get_json(f"{_API_BASE}/manga", params=params)

        for item in self._unwrap_data(data):
            manga_id = item.get("id")
            if not manga_id:
                continue

            attributes = item.get("attributes") or {}
            title = self._pick_title(attributes.get("title"))
            cover_url = self._cover_url(manga_id, item.get("relationships") or [])

            yield SearchResult(
                site_id=self.site_id,
                title=title or manga_id,
                url=f"https://mangadex.org/title/{manga_id}",
                cover_url=cover_url,
                language=self.language,
                adult=self.adult,
            )

    # ------------------------------------------------------------------ #
    #  get_manga                                                         #
    # ------------------------------------------------------------------ #
    async def get_manga(self, url_or_id: str) -> Manga:
        """Return the :class:`Manga` for a MangaDex URL or UUID.

        ``url_or_id`` may be either a full URL (as returned by
        :meth:`search`) or a bare UUID (as stored in the local library).
        :meth:`BaseParser._extract_id` normalises both cases.
        """
        manga_id = self._extract_id(url_or_id)
        params: dict[str, Any] = {"includes[]": ["cover_art"]}
        data = await self.session.get_json(f"{_API_BASE}/manga/{manga_id}", params=params)

        # ``/manga/{id}`` returns a single object envelope, not an array.
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            payload = {}

        attributes = payload.get("attributes") or {}
        title = self._pick_title(attributes.get("title")) or manga_id
        cover_url = self._cover_url(manga_id, payload.get("relationships") or [])

        tags: list[str] = []
        for tag in attributes.get("tags") or []:
            tag_attrs = tag.get("attributes") or {}
            tag_name = self._pick_title(tag_attrs.get("name"))
            if tag_name:
                tags.append(tag_name)

        return Manga(
            id=self._make_manga_id(f"https://mangadex.org/title/{manga_id}"),
            site_id=self.site_id,
            title=title,
            url=f"https://mangadex.org/title/{manga_id}",
            cover_url=cover_url,
            language=self.language,
            adult=self.adult,
            tags=tags,
            status=self._normalise_status(attributes.get("status")),
        )

    # ------------------------------------------------------------------ #
    #  get_chapters                                                      #
    # ------------------------------------------------------------------ #
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the complete ordered chapter list for *manga*.

        The ``/manga/{id}/feed`` endpoint paginates chapters in blocks of
        at most ``_FEED_LIMIT``.  This method follows every page until the
        server returns fewer results than the page size, then returns the
        deduplicated list in the order provided by the API (chronological,
        oldest first, thanks to ``order[chapter]=asc``).

        Only chapters translated into the parser's language are returned.
        """
        manga_id = self._extract_id(manga.url)
        chapters: dict[str, Chapter] = {}
        offset = 0

        while True:
            params: dict[str, Any] = {
                "translatedLanguage[]": [self.language],
                "limit": _FEED_LIMIT,
                "offset": offset,
                "order[chapter]": "asc",
            }
            data = await self.session.get_json(
                f"{_API_BASE}/manga/{manga_id}/feed", params=params
            )
            batch = self._unwrap_data(data)
            if not batch:
                break

            for item in batch:
                chapter_id = item.get("id")
                if not chapter_id or chapter_id in chapters:
                    continue

                attributes = item.get("attributes") or {}
                raw_number = attributes.get("chapter")
                number: float | None = None
                if raw_number is not None:
                    try:
                        number = float(raw_number)
                    except (TypeError, ValueError):
                        number = None

                # Prefer the human-readable title; fall back to a synthetic
                # label built from the chapter number and volume.
                label = attributes.get("title") or ""
                if not label:
                    volume = attributes.get("volume")
                    if volume and raw_number:
                        label = f"Vol. {volume} Ch. {raw_number}"
                    elif raw_number:
                        label = f"Ch. {raw_number}"
                    else:
                        label = chapter_id

                chapters[chapter_id] = Chapter(
                    id=self._make_chapter_id(
                        manga.id, f"https://mangadex.org/chapter/{chapter_id}"
                    ),
                    manga_id=manga.id,
                    site_id=self.site_id,
                    title=str(label),
                    url=f"https://mangadex.org/chapter/{chapter_id}",
                    number=number,
                    language=self.language,
                )

            if len(batch) < _FEED_LIMIT:
                break
            offset += _FEED_LIMIT

        return list(chapters.values())

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        The ``/at-home/server/{chapterId}`` endpoint returns a
        geographically optimised ``baseUrl`` plus the chapter hash and the
        ordered filename list.  We build one absolute URL per page using
        the original-quality ``data`` array.
        """
        chapter_id = self._extract_chapter_id(chapter.url)
        data = await self.session.get_json(f"{_API_BASE}/at-home/server/{chapter_id}")

        base_url = data.get("baseUrl") if isinstance(data, dict) else None
        chapter_info = data.get("chapter") if isinstance(data, dict) else None
        if not base_url or not isinstance(chapter_info, dict):
            return []

        chapter_hash = chapter_info.get("hash")
        filenames = chapter_info.get("data") or []
        if not chapter_hash or not filenames:
            return []

        pages: list[Page] = []
        for index, filename in enumerate(filenames):
            if not filename:
                continue
            pages.append(
                Page(
                    index=index,
                    url=f"{base_url}/data/{chapter_hash}/{filename}",
                    chapter_id=chapter.id,
                )
            )

        return pages

    # ------------------------------------------------------------------ #
    #  helpers                                                           #
    # ------------------------------------------------------------------ #
    def _allowed_content_ratings(self) -> list[str]:
        """Return the content ratings permitted by the current settings.

        When ``Settings.content.allow_adult`` is true, all four ratings are
        returned.  Otherwise only the non-adult ratings are returned, which
        keeps adult results out of search without disabling the site.
        """
        ratings = list(_SAFE_RATINGS)
        if getattr(self._settings.content, "allow_adult", False):
            ratings.extend(_ADULT_RATINGS)
        return ratings

    @staticmethod
    def _unwrap_data(payload: Any) -> list[dict[str, Any]]:
        """Extract the ``data`` array from a MangaDex list envelope.

        The API always wraps collections in ``{"result": "ok", "data": [...]}``.
        This helper tolerates a bare list (used by some mirrors) and returns
        an empty list for anything else.
        """
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        elif isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    @staticmethod
    def _pick_title(title_map: Any) -> str:
        """Pick the best title from a MangaDex localised title map.

        MangaDex stores titles as ``{language_code: title}``.  We prefer
        English, then romanised Japanese, then the first available entry.
        """
        if not isinstance(title_map, dict) or not title_map:
            return ""

        for key in ("en", "ja-ro", "ja", "ko-ro", "ko", "zh-ro", "zh"):
            value = title_map.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for value in title_map.values():
            if isinstance(value, str) and value.strip():
                return value.strip()

        return ""

    @staticmethod
    def _cover_url(manga_id: str, relationships: list[Any]) -> str | None:
        """Build the cover URL from a manga's relationship list.

        The ``cover_art`` relationship holds the cover filename under
        ``attributes.fileName`` when ``includes[]=cover_art`` was requested.
        """
        for rel in relationships:
            if not isinstance(rel, dict):
                continue
            if rel.get("type") != "cover_art":
                continue
            attrs = rel.get("attributes") or {}
            filename = attrs.get("fileName")
            if isinstance(filename, str) and filename:
                return f"{_COVER_BASE}/{manga_id}/{filename}"
        return None

    @staticmethod
    def _normalise_status(raw: Any) -> str | None:
        """Map MangaDex status strings onto the project's vocabulary.

        MangaDex uses ``ongoing``, ``completed``, ``hiatus`` and
        ``cancelled`` — the same values the :class:`MangaStatus` enum
        exposes in lowercase.  Unknown values are returned as ``None``.
        """
        if not isinstance(raw, str):
            return None
        normalised = raw.strip().lower()
        if normalised in {"ongoing", "completed", "hiatus", "cancelled"}:
            return normalised
        return None


# Auto-registration — triggered when ``nexusdl.parsers.en`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("mangadex", MangaDexParser)
