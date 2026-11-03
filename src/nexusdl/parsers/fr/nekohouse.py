"""Parser for Nekohouse — Kemono-style creator archive (adult, French UI).

Nekohouse (``nekohouse.su``, with the ``nekohouse.online`` mirror) is **not**
a manga scanlation site in the classical sense: it is a **Kemono/Coomer
clone**.  It archives posts published by creators on subscription platforms
(Patreon, Pixiv Fanbox, Fantia, Gumroad, SubscribeStar, DLsite, etc.) and
exposes them through the same public API and page layout as Kemono.

.. note::
   This parser is placed under ``parsers/fr`` because the project tree
   declares it there, and it is registered with ``language = "fr"`` so the
   registry can surface it when French content is requested.  Nekohouse
   itself is **multi-language**: creators publish in Japanese, English,
   French, Korean, Chinese, etc.  The ``fr`` declaration is a registry hint,
   not a content restriction — the parser returns every post it finds, and
   the caller is free to filter by ``SearchResult.language`` downstream.

Domain mapping
--------------
Because Nekohouse is not manga-shaped, the four :class:`BaseParser`
operations are mapped onto its natural hierarchy:

====================  =======================================================
BaseParser concept     Nekohouse concept
====================  =======================================================
``Manga``              A creator profile (``/{service}/user/{user_id}``)
``Chapter``            A post (``/{service}/user/{user_id}/post/{post_id}``)
``Page``               An image attachment inside a post
``search``             ``/posts?q={query}`` — the site-wide post search
====================  =======================================================

This mapping lets the downloader, the packager and the library treat a
Nekohouse creator exactly like a manga series: each post becomes a chapter,
each image attachment becomes a page, and packaging produces one archive
per post.

Architecture
------------
Nekohouse is server-rendered HTML with a plain JSON API — it is **not** a
JavaScript SPA and does **not** require Playwright.  It may, however, sit
behind a DDoS-protection layer (Cloudflare or equivalent), so the parser
inherits from
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` to transparently
upgrade a request to a browser-backed solve when a challenge is detected,
then replay the original request with the obtained cookies.

To remain resilient against the layout drift that Kemono forks frequently
exhibit (Nekohouse tracks the upstream Kemono codebase loosely), the four
operations are implemented with **multi-selector fallbacks** and an **API
fallback**:

* ``search`` tries ``/posts?q=`` and parses post cards.
* ``get_manga`` parses the creator page header.
* ``get_chapters`` parses post cards on the creator page, and falls back to
  ``/api/v1/{service}/user/{user_id}`` when the HTML yields nothing.
* ``get_pages`` parses attachment links on the post page, and falls back to
  ``/api/v1/{service}/post/{post_id}`` when the HTML yields nothing.

Route patterns
--------------
* Search           — ``GET  /posts?q={query}&o={offset}``
* Creator          — ``GET  /{service}/user/{user_id}``
* Post             — ``GET  /{service}/user/{user_id}/post/{post_id}``
* Creator API      — ``GET  /api/v1/{service}/user/{user_id}?o={offset}``
* Post API         — ``GET  /api/v1/{service}/post/{post_id}``
* File CDN         — ``{base}/data/{path}``

The site is adult-rated by declaration (``adult = True``), because the vast
majority of archived content is NSFW.  The registry can filter it via
``Settings.content.allow_adult`` and the ``language`` filter of
``SiteRegistry.list_sites``.

Domain note
-----------
The ``domains`` list in ``data/sites.yaml`` is the single source of truth
for the base URL.  Nekohouse has operated under ``nekohouse.su`` and
``nekohouse.online``; if the primary domain changes again, only
``sites.yaml`` needs updating — this file stays untouched.
"""
from __future__ import annotations

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

__all__ = ["NekohouseParser"]

# ---------------------------------------------------------------------------
# URL patterns — used to extract service / creator / post identifiers from
# any of the URL shapes Nekohouse accepts.
# ---------------------------------------------------------------------------

# /{service}/user/{user_id}
_CREATOR_URL_RE = re.compile(r"/([^/?#]+)/user/([^/?#]+)")

# /{service}/user/{user_id}/post/{post_id}
_POST_URL_RE = re.compile(r"/([^/?#]+)/user/([^/?#]+)/post/([^/?#]+)")

# Default service used when a bare creator id is supplied without a URL.
# Nekohouse is dominated by Fanbox and Fantia; Fanbox is the safest default
# because it is the service the upstream Kemono project was originally built
# for.
_DEFAULT_SERVICE = "fanbox"

# Number of posts the site returns per page.  Used to compute the ``o``
# offset for search and API pagination.
_PAGE_SIZE = 50

# Image extensions accepted as ``Page`` entries.  Non-image attachments
# (videos, archives, PDFs) are kept out of the page list so that the
# downloader and the packagers operate on a homogeneous image stream.
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"}
)

# ---------------------------------------------------------------------------
# Selector sets — tried in order.  The first set that yields a non-empty
# result wins.  This is what makes the parser resilient to Nekohouse's
# layout drift over time.
# ---------------------------------------------------------------------------

# Search / creator page post cards
_POST_CARD_SELECTORS: tuple[str, ...] = (
    "article.post-card",
    "div.post-card",
    "article.post",
    "div.post",
    "div.card",
    "article",
)

# Anchor inside a post card that points to the post
_POST_LINK_SELECTORS: tuple[str, ...] = (
    "a[href*='/post/']",
    "a.post-card__link",
    "a",
)

# Title inside a post card
_POST_TITLE_SELECTORS: tuple[str, ...] = (
    ".post-card__title",
    ".post__title",
    "h2",
    "h3",
    "h4",
    ".title",
)

# Thumbnail inside a post card
_POST_THUMB_SELECTORS: tuple[str, ...] = (
    ".post-card__image img",
    ".post__thumbnail img",
    "img",
)

# Creator page title
_CREATOR_TITLE_SELECTORS: tuple[str, ...] = (
    ".user-header h1",
    ".user-header__name",
    "h1",
    ".user-name",
)

# Creator page avatar
_CREATOR_AVATAR_SELECTORS: tuple[str, ...] = (
    ".user-header__avatar img",
    ".user-header img",
    ".avatar img",
    "img",
)

# Attachment anchors on a post page
_ATTACHMENT_SELECTORS: tuple[str, ...] = (
    ".post__attachment a[href]",
    ".post__files a[href]",
    "a.fileThumb[href]",
    ".post__body a[href*='/data/']",
    "a[href*='/data/']",
)


class NekohouseParser(CloudflareMixin, BaseParser):
    """Parser for Nekohouse (Kemono-style archive, adult, multi-language)."""

    site_id: str = "nekohouse"
    language: str = "fr"
    adult: bool = True

    # ------------------------------------------------------------------ #
    #  search                                                            #
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator[SearchResult]:
        """Search Nekohouse and yield one result per post.

        The site-wide search lives at ``/posts?q={query}&o={offset}``.  Each
        result is a post card whose link points to a specific post; the
        :class:`SearchResult` therefore carries the **post** URL, and
        :meth:`get_manga` is able to accept a post URL by extracting the
        creator part.

        Args:
            query: Search terms (creator name, post title, tags, etc.).
            page: 1-based page number (translated to an ``o`` offset).

        Yields:
            SearchResult instances for the current page.
        """
        offset = max(page - 1, 0) * _PAGE_SIZE
        url = f"{self._base_url}/posts?q={quote(query)}&o={offset}"
        html = await self.session.get_html(url)
        tree = HTMLParser(html)

        for card in self._first_nodes(tree, _POST_CARD_SELECTORS):
            link = self._first_anchor(card, _POST_LINK_SELECTORS)
            if link is None:
                continue

            href = link.attributes.get("href", "")
            if not href or "/post/" not in href:
                continue

            title = self._first_text(card, _POST_TITLE_SELECTORS) or href
            cover_url = self._first_img_src(card, _POST_THUMB_SELECTORS)

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
        """Return the :class:`Manga` representing a creator (or a post).

        ``url_or_id`` may be:

        * a full creator URL (``/{service}/user/{user_id}``),
        * a full post URL (``/{service}/user/{user_id}/post/{post_id}``) —
          the creator part is extracted and used,
        * a bare creator id — the default service (``fanbox``) is assumed.

        The creator page header supplies the title and avatar.  Nekohouse
        does not expose a creator-level status or tag list, so those fields
        are left empty.
        """
        service, user_id = self._resolve_creator(url_or_id)
        creator_url = f"{self._base_url}/{service}/user/{user_id}"

        html = await self.session.get_html(creator_url)
        tree = HTMLParser(html)

        title = (
            self._first_text(tree, _CREATOR_TITLE_SELECTORS)
            or user_id
        )
        avatar_url = self._first_img_src(tree, _CREATOR_AVATAR_SELECTORS)

        return Manga(
            id=self._make_manga_id(creator_url),
            site_id=self.site_id,
            title=title,
            url=creator_url,
            cover_url=self._resolve_url(avatar_url) if avatar_url else None,
            language=self.language,
            adult=self.adult,
            tags=[],
            status=None,
        )

    # ------------------------------------------------------------------ #
    #  get_chapters                                                      #
    # ------------------------------------------------------------------ #
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the creator's posts, ordered chronologically.

        The creator page lists posts newest-first.  The parser collects every
        post link, deduplicates by post id, and reverses the list so the
        downloader processes posts in publication order (oldest first).

        When the HTML page yields no post cards — which happens when the
        creator page is paginated by JavaScript or when the front-end is
        temporarily broken — the parser falls back to the public JSON API
        (``/api/v1/{service}/user/{user_id}``) and pages through it until
        the server returns an empty batch.
        """
        service, user_id = self._parse_creator_url(manga.url)

        chapters = self._chapters_from_html(manga, service, user_id)
        if not chapters:
            chapters = await self._chapters_from_api(manga, service, user_id)

        # Nekohouse lists posts newest-first; reverse to chronological order.
        return list(reversed(chapters))

    # ------------------------------------------------------------------ #
    #  get_pages                                                         #
    # ------------------------------------------------------------------ #
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the image attachments of a post, in post order.

        The post page renders attachments as anchors pointing to
        ``/data/{path}``.  Non-image attachments (videos, archives, PDFs) are
        filtered out so the packagers receive a homogeneous image stream.

        When the HTML page yields no attachment anchors — which happens when
        the post body is collapsed behind JavaScript or when the front-end
        is temporarily broken — the parser falls back to the public JSON API
        (``/api/v1/{service}/post/{post_id}``) and extracts the
        ``attachments`` array.
        """
        pages = self._pages_from_html(chapter)
        if not pages:
            pages = await self._pages_from_api(chapter)
        return pages

    # ------------------------------------------------------------------ #
    #  HTML strategies                                                   #
    # ------------------------------------------------------------------ #
    def _chapters_from_html(
        self,
        manga: Manga,
        service: str,
        user_id: str,
    ) -> list[Chapter]:
        """Collect chapters from the creator page HTML.

        Called synchronously because :meth:`get_chapters` already fetched
        the page indirectly through :meth:`_first_nodes`; the fetch is
        performed here for clarity.
        """
        # ``_first_nodes`` needs the parsed tree, so we fetch the page once
        # and reuse it across the selector chain.
        import asyncio  # local import — only used for the sync bridge below

        html = asyncio.get_event_loop().run_until_complete(
            self.session.get_html(manga.url)
        ) if not asyncio.get_event_loop().is_running() else ""
        # The synchronous bridge above is intentionally avoided; the real
        # implementation lives in the async wrapper below.
        raise RuntimeError("unreachable")

    async def _chapters_from_api(
        self,
        manga: Manga,
        service: str,
        user_id: str,
    ) -> list[Chapter]:
        """Collect chapters from the public JSON API.

        The API returns ``{"posts": [...]}`` and is paginated by ``o``
        offset.  We follow every page until the server returns an empty
        batch, deduplicating by post id.
        """
        chapters: dict[str, Chapter] = {}
        offset = 0

        while True:
            url = f"{self._base_url}/api/v1/{service}/user/{user_id}"
            data = await self.session.get_json(url, params={"o": offset})
            batch = data.get("posts") if isinstance(data, dict) else None
            if not batch:
                break

            for item in batch:
                post_id = str(item.get("id") or "")
                if not post_id or post_id in chapters:
                    continue

                title = item.get("title") or f"Post {post_id}"
                post_url = (
                    f"{self._base_url}/{service}/user/{user_id}/post/{post_id}"
                )
                chapters[post_id] = Chapter(
                    id=self._make_chapter_id(manga.id, post_url),
                    manga_id=manga.id,
                    site_id=self.site_id,
                    title=str(title),
                    url=post_url,
                    number=None,
                    language=self.language,
                )

            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        return list(chapters.values())

    def _pages_from_html(self, chapter: Chapter) -> list[Page]:
        """Collect image attachments from the post page HTML.

        The post page is fetched asynchronously in :meth:`get_pages`; this
        helper only transforms an already-fetched tree.  It is therefore
        unused in the current flow and kept for symmetry with the API
        fallback.  The real implementation lives in the async wrapper.
        """
        raise RuntimeError("unreachable")

    async def _pages_from_api(self, chapter: Chapter) -> list[Page]:
        """Collect image attachments from the public JSON API.

        The API returns ``{"post": {...}, "attachments": [...]}``.  Each
        attachment carries a ``path`` relative to the ``/data/`` route.
        We build one absolute URL per image attachment, filtering out
        non-image extensions.
        """
        service, user_id, post_id = self._parse_post_url(chapter.url)
        url = f"{self._base_url}/api/v1/{service}/post/{post_id}"
        data = await self.session.get_json(url)

        raw_attachments: list[dict[str, Any]] = []
        if isinstance(data, dict):
            attachments = data.get("attachments")
            if isinstance(attachments, list):
                raw_attachments = [
                    a for a in attachments if isinstance(a, dict)
                ]

        pages: list[Page] = []
        for attachment in raw_attachments:
            path = attachment.get("path")
            if not isinstance(path, str) or not path:
                continue

            name = attachment.get("name") or path
            ext = self._extension_of(str(name)) or self._extension_of(path)
            if ext not in _IMAGE_EXTENSIONS:
                continue

            pages.append(
                Page(
                    index=len(pages),
                    url=self._absolute_file_url(path),
                    chapter_id=chapter.id,
                )
            )

        return pages

    # ------------------------------------------------------------------ #
    #  URL helpers                                                       #
    # ------------------------------------------------------------------ #
    def _resolve_creator(self, url_or_id: str) -> tuple[str, str]:
        """Return ``(service, user_id)`` for a creator URL or bare id.

        A bare id is interpreted as a Fanbox creator, the dominant service
        on Nekohouse.  A full URL is parsed by :meth:`_parse_creator_url`.
        """
        candidate = url_or_id.strip()
        if candidate.startswith("http://") or candidate.startswith("https://") or "/" in candidate:
            parsed = self._parse_creator_url(candidate)
            if parsed[1]:
                return parsed

        # Bare id — assume the default service.
        return _DEFAULT_SERVICE, candidate

    def _parse_creator_url(self, url: str) -> tuple[str, str]:
        """Extract ``(service, user_id)`` from a creator or post URL.

        A post URL is accepted too: the creator part is extracted and the
        post id is discarded.  Returns ``("", "")`` when the URL does not
        match the Nekohouse creator or post shape.
        """
        post_match = _POST_URL_RE.search(url)
        if post_match:
            return post_match.group(1), post_match.group(2)

        creator_match = _CREATOR_URL_RE.search(url)
        if creator_match:
            return creator_match.group(1), creator_match.group(2)

        return "", ""

    def _parse_post_url(self, url: str) -> tuple[str, str, str]:
        """Extract ``(service, user_id, post_id)`` from a post URL.

        Returns ``("", "", "")`` when the URL does not match the Nekohouse
        post shape.
        """
        match = _POST_URL_RE.search(url)
        if match:
            return match.group(1), match.group(2), match.group(3)
        return "", "", ""

    def _absolute_file_url(self, path: str) -> str:
        """Turn an attachment path into an absolute ``/data/`` URL.

        Nekohouse serves files from ``{base}/data/{path}``.  The API returns
        bare paths (``abc123.png``), while the HTML renders full ``/data/``
        links; this helper accepts both shapes and never doubles the
        ``/data/`` prefix.
        """
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("/data/"):
            return f"{self._base_url}{path}"
        if path.startswith("data/"):
            return f"{self._base_url}/{path}"
        return f"{self._base_url}/data/{path}"

    @staticmethod
    def _extension_of(name: str) -> str:
        """Return the lowercase extension of a filename, or an empty string."""
        if "." not in name:
            return ""
        return name.rsplit(".", 1)[-1].lower()

    # ------------------------------------------------------------------ #
    #  Node helpers                                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _first_nodes(
        tree: HTMLParser,
        selectors: tuple[str, ...],
    ) -> list[Any]:
        """Return the first non-empty match for any selector in *selectors*."""
        for selector in selectors:
            nodes = tree.css(selector)
            if nodes:
                return list(nodes)
        return []

    @staticmethod
    def _first_anchor(
        node: Any,
        selectors: tuple[str, ...],
    ) -> Any | None:
        """Return the first anchor matching any selector in *selectors*."""
        for selector in selectors:
            anchor = node.css_first(selector)
            if anchor is not None:
                return anchor
        return None

    @staticmethod
    def _first_text(
        node: Any,
        selectors: tuple[str, ...],
    ) -> str:
        """Return the text of the first node matching any selector."""
        for selector in selectors:
            found = node.css_first(selector)
            if found is not None:
                text = found.text(strip=True)
                if text:
                    return text
        return ""

    @staticmethod
    def _first_img_src(
        node: Any,
        selectors: tuple[str, ...],
    ) -> str:
        """Return the ``data-src`` / ``src`` of the first matching image."""
        for selector in selectors:
            img = node.css_first(selector)
            if img is None:
                continue
            src = (
                img.attributes.get("data-src")
                or img.attributes.get("src")
                or ""
            )
            if src:
                return src
        return ""


# ---------------------------------------------------------------------------
# The two synchronous stubs declared on the class (``_chapters_from_html``
# and ``_pages_from_html``) exist only to document the intended HTML-first
# strategy.  The actual asynchronous extraction is performed directly in
# ``get_chapters`` and ``get_pages`` below, which override the stubs at
# runtime.  See the ``# --- async overrides ---`` block.
# ---------------------------------------------------------------------------

async def _chapters_from_html_impl(
    self: NekohouseParser,
    manga: Manga,
    service: str,
    user_id: str,
) -> list[Chapter]:
    """Async implementation of the creator-page HTML strategy."""
    html = await self.session.get_html(manga.url)
    tree = HTMLParser(html)

    chapters: dict[str, Chapter] = {}
    for card in self._first_nodes(tree, _POST_CARD_SELECTORS):
        link = self._first_anchor(card, _POST_LINK_SELECTORS)
        if link is None:
            continue

        href = link.attributes.get("href", "")
        if not href or "/post/" not in href:
            continue

        post_id = self._parse_post_url(self._resolve_url(href))[2]
        if not post_id or post_id in chapters:
            continue

        title = self._first_text(card, _POST_TITLE_SELECTORS) or post_id
        chapters[post_id] = Chapter(
            id=self._make_chapter_id(manga.id, self._resolve_url(href)),
            manga_id=manga.id,
            site_id=self.site_id,
            title=str(title),
            url=self._resolve_url(href),
            number=None,
            language=self.language,
        )

    return list(chapters.values())


async def _pages_from_html_impl(
    self: NekohouseParser,
    chapter: Chapter,
) -> list[Page]:
    """Async implementation of the post-page HTML strategy."""
    html = await self.session.get_html(chapter.url)
    tree = HTMLParser(html)

    pages: list[Page] = []
    for selector in _ATTACHMENT_SELECTORS:
        anchors = tree.css(selector)
        if not anchors:
            continue

        for anchor in anchors:
            href = anchor.attributes.get("href", "")
            if not href:
                continue

            ext = self._extension_of(href)
            if ext not in _IMAGE_EXTENSIONS:
                continue

            pages.append(
                Page(
                    index=len(pages),
                    url=self._absolute_file_url(href),
                    chapter_id=chapter.id,
                )
            )

        if pages:
            break

    return pages


# ---------------------------------------------------------------------------
# Bind the async implementations onto the class.  This replaces the two
# ``RuntimeError`` stubs declared above with the real logic, while keeping
# the public method signatures (``get_chapters`` / ``get_pages``) unchanged.
# ---------------------------------------------------------------------------

NekohouseParser._chapters_from_html = _chapters_from_html_impl  # type: ignore[method-assign]
NekohouseParser._pages_from_html = _pages_from_html_impl  # type: ignore[method-assign]


# Auto-registration — triggered when ``nexusdl.parsers.fr`` is imported,
# which in turn is imported by ``nexusdl.parsers``.  This keeps the factory
# populated without any manual wiring in the composition root.
register("nekohouse", NekohouseParser)
