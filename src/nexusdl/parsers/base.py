"""Abstract base class for every site parser in the project.

:class:`BaseParser` defines the **contract** that every concrete parser must
satisfy, and provides the shared helpers that all parsers — and all
mixins — rely on.  It is the single point where the following cross-cutting
concerns are resolved:

* **Site identity** — ``site_id``, ``language`` and ``adult`` class
  attributes, matching the corresponding entry in ``data/sites.yaml``.
* **Session wiring** — the parser receives the shared
  :class:`~nexusdl.core.session.http_session.HttpSession` and, optionally,
  a shared :class:`~nexusdl.core.session.playwright_pool.PlaywrightPool` and
  :class:`~nexusdl.core.session.flaresolverr.FlareSolverr`.
* **Base URL** — derived from the site's primary domain in
  ``data/sites.yaml``; parsers never hardcode a domain.
* **Stable identifiers** — ``Manga.id`` and ``Chapter.id`` are 16-hex
  SHA-256 digests derived from the site id, the manga source id, and the
  chapter source id, per the project's ID rules.  The helpers guarantee
  that the same source URL always produces the same id across sessions
  and across runs.
* **Logging** — a bound :class:`loguru.Logger` instance available as
  ``self._log``, tagged with the site id and parser class.
* **Common operations** — :meth:`download_page` and :meth:`health_check`.

The four abstract operations — :meth:`search`, :meth:`get_manga`,
:meth:`get_chapters` and :meth:`get_pages` — are the only methods a
concrete parser **must** implement.  In practice most parsers do not
implement them directly: they inherit them from a mixin
(:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin`,
:class:`~nexusdl.parsers.mixins.mangathemesia.MangaThemesiaMixin`,
:class:`~nexusdl.parsers.mixins.foolslide.FoolSlideMixin`) and only declare
their identity::

    class MyParser(WordPressMadaraMixin, BaseParser):
        site_id = "my_site"
        language = "en"
        adult = False

Abstract method forms
---------------------
:meth:`search` is an **async generator** in every concrete parser (``async
def`` with ``yield``), so its abstract declaration is a plain ``def``
returning :class:`AsyncIterator`.  This mirrors the caller-facing signature
exactly: callers use ``async for`` and never ``await`` the result.

:meth:`get_manga`, :meth:`get_chapters` and :meth:`get_pages` are regular
**async coroutines** (no ``yield``), so their abstract declarations are
``async def``.

Session contract
----------------
The session must expose a ``settings`` attribute (or, failing that, a
``_settings`` attribute) holding the resolved :class:`Settings` instance.
This is guaranteed in the composition root, where the session is built with
the resolved settings.  Parsers that access ``self._settings`` (most
notably the API-driven ones that read ``settings.content.allow_adult``) can
therefore rely on it being non-``None``.

Identifier stability
--------------------
``_make_manga_id`` and ``_make_chapter_id`` hash the site/entity ids with
SHA-256 and truncate to 16 hex characters.  This is the project-wide rule;
using anything else would produce ids that do not match the library
database and would break incremental downloads.  Parsers that need a
different source-id extraction may override ``_extract_id`` or
``_extract_chapter_id`` — the hashing scheme itself must not change.

Concrete parsers are instantiated by
:class:`~nexusdl.parsers.factory.DefaultParserFactory` via
:meth:`~nexusdl.parsers.factory.DefaultParserFactory.create`, which passes
``site`` and ``session`` positionally, plus ``playwright_pool`` and
``flaresolverr`` as keyword arguments.  The signature below reflects that
contract exactly.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Callable,
)
from urllib.parse import urlparse

from loguru import logger

from nexusdl.core.exceptions import NexusDLError
from nexusdl.core.models.enums import SiteHealth

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.config import Settings
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.page import Page
    from nexusdl.core.models.search import SearchResult
    from nexusdl.core.models.site import SiteConfig
    from nexusdl.core.session.flaresolverr import FlareSolverr
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.core.session.playwright_pool import PlaywrightPool


__all__ = ["BaseParser"]


class BaseParser(ABC):
    """Abstract base class for every site parser in the project.

    See the module docstring for the overall design and the session /
    identifier / logging contracts.

    Attributes:
        site_id: The site's canonical id, matching ``data/sites.yaml``.
            Must match ``^[a-z][a-z0-9_-]*$``; enforced by
            :func:`nexusdl.parsers.factory.register`.
        language: The site's primary language, matching
            :class:`~nexusdl.core.models.enums.Language` values
            (``"fr"``, ``"en"``, ``"ko"``, ``"ja"``, etc.).  Used by
            :meth:`SiteRegistry.list_sites` for language filtering.
        adult: Whether the site serves adult content.  Used by
            :meth:`SiteRegistry.list_sites` for the ``include_adult``
            filter.  Must match the ``adult`` field of the site's entry
            in ``data/sites.yaml``.
        site: The :class:`SiteConfig` loaded from ``data/sites.yaml``.
        session: The shared :class:`HttpSession`.
    """

    # ------------------------------------------------------------------ #
    #  Class-level identity                                              #
    # ------------------------------------------------------------------ #
    site_id: str = ""
    """The site's canonical id — must be overridden by every concrete parser."""

    language: str = ""
    """The site's primary language — must be overridden by every concrete parser."""

    adult: bool = False
    """Whether the site is adult-rated — defaults to ``False``."""

    # ------------------------------------------------------------------ #
    #  Construction                                                      #
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        site: "SiteConfig",
        session: "HttpSession",
        *,
        playwright_pool: "PlaywrightPool | None" = None,
        flaresolverr: "FlareSolverr | None" = None,
    ) -> None:
        """Initialise the parser with its site, session and optional solvers.

        Args:
            site: The :class:`SiteConfig` from
                :class:`~nexusdl.core.registry.site_registry.SiteRegistry`.
                Its ``id`` must match the parser's ``site_id`` class
                attribute — this is checked at construction time and
                raises :class:`NexusDLError` on mismatch.
            session: The shared :class:`HttpSession`.  Must expose a
                ``settings`` attribute (or ``_settings`` fallback) holding
                the resolved :class:`Settings`.
            playwright_pool: The shared Playwright pool, or ``None`` when
                Playwright is not configured.  Forwarded to mixins and
                used by :class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`
                and :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`.
            flaresolverr: The shared FlareSolverr client, or ``None`` when
                FlareSolverr is not configured.  Used by
                :class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin`.

        Raises:
            NexusDLError: If a required class attribute is missing, if the
                site's id does not match the parser's ``site_id``, if the
                site has no domains, or if the session does not expose a
                ``settings`` attribute.
        """
        cls = type(self)

        # Validate the parser's own class-level identity.  Empty strings
        # would silently break registration, filtering and logging.
        if not cls.site_id:
            raise NexusDLError(
                f"{cls.__name__} must define a non-empty `site_id` class "
                f"attribute."
            )
        if not cls.language:
            raise NexusDLError(
                f"{cls.__name__} must define a non-empty `language` class "
                f"attribute."
            )

        # The site's id must match the parser's — a mismatch always
        # indicates a bug in the composition root (wrong parser handed the
        # wrong SiteConfig) and would silently corrupt the library
        # database if allowed through.
        if site.id != cls.site_id:
            raise NexusDLError(
                f"{cls.__name__}.site_id={cls.site_id!r} does not match "
                f"SiteConfig.id={site.id!r}.  The parser registered for a "
                f"site must declare the same site_id as the SiteConfig it "
                f"is instantiated with."
            )

        # The base URL is derived from the primary domain.  Parsers never
        # hardcode domains — the sites.yaml file is the single source of
        # truth.
        domains = getattr(site, "domains", None)
        if not domains:
            raise NexusDLError(
                f"Site {cls.site_id!r} has no domains configured in "
                f"sites.yaml."
            )
        base_url = str(domains[0]).rstrip("/")
        if not base_url:
            raise NexusDLError(
                f"Site {cls.site_id!r} has an empty primary domain in "
                f"sites.yaml."
            )

        # The session must expose Settings, either publicly or via the
        # private attribute.  This is guaranteed by the composition root
        # but a clear error here is far better than an obscure
        # AttributeError on the first parser that reads settings.
        settings = getattr(session, "settings", None)
        if settings is None:
            settings = getattr(session, "_settings", None)
        if settings is None:
            raise NexusDLError(
                f"Cannot construct parser for site {cls.site_id!r}: the "
                f"provided HttpSession does not expose a `settings` "
                f"attribute.  Ensure the session is constructed with the "
                f"resolved Settings instance in the composition root."
            )

        self.site: SiteConfig = site
        self.session: HttpSession = session
        self._settings: Settings = settings
        self._playwright: PlaywrightPool | None = playwright_pool
        self._flaresolverr: FlareSolverr | None = flaresolverr
        self._base_url: str = base_url
        self._log: Logger = logger.bind(
            site=cls.site_id,
            parser=cls.__qualname__,
        )

        self._log.debug(
            "Initialised parser for site {} (language={}, adult={}, base_url={})",
            cls.site_id,
            cls.language,
            cls.adult,
            base_url,
        )

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} site={self.site_id!r} "
            f"language={self.language!r} adult={self.adult}>"
        )

    # ------------------------------------------------------------------ #
    #  Abstract contract                                                 #
    # ------------------------------------------------------------------ #
    @abstractmethod
    def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator[SearchResult]:
        """Search the site and yield results page by page.

        Implementations are **async generators** (``async def`` with
        ``yield``), which is why the abstract declaration here is a plain
        ``def``: the caller-facing signature is
        ``search(...) -> AsyncIterator[SearchResult]``, and callers
        iterate with ``async for`` — never with ``await``.

        Args:
            query: Search terms.
            page: 1-based page number.

        Yields:
            One :class:`SearchResult` per matching manga.
        """
        ...

    @abstractmethod
    async def get_manga(self, url_or_id: str) -> Manga:
        """Return the :class:`Manga` identified by *url_or_id*.

        Args:
            url_or_id: Either a full URL (as returned by :meth:`search`)
                or a bare source id (as stored in the library).

        Returns:
            The populated :class:`Manga`.
        """
        ...

    @abstractmethod
    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the complete ordered chapter list for *manga*.

        Args:
            manga: The manga whose chapters to enumerate.

        Returns:
            The ordered list of :class:`Chapter` objects, oldest first.
        """
        ...

    @abstractmethod
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered pages of *chapter*.

        Args:
            chapter: The chapter whose pages to fetch.

        Returns:
            The ordered list of :class:`Page` objects, indexed from 0.
        """
        ...

    # ------------------------------------------------------------------ #
    #  URL helpers                                                       #
    # ------------------------------------------------------------------ #
    def _resolve_url(self, url_or_id: str) -> str:
        """Return the absolute URL of *url_or_id*.

        Accepts a full URL (``http://`` / ``https://`` / protocol-relative
        ``//``), an absolute path (``/manga/slug/``), or a bare id
        (``slug``).  In the bare-id case, the id is appended to the
        parser's base URL; parsers that need a specific path prefix
        (``/manga/``, ``/serie/``, etc.) should not call this helper with
        a bare id — they should construct the URL themselves.

        An empty input returns the base URL, so callers can safely call
        ``_resolve_url("")`` when they have nothing to resolve.

        Args:
            url_or_id: The URL or id to resolve.

        Returns:
            An absolute URL.
        """
        candidate = (url_or_id or "").strip()
        if not candidate:
            return self._base_url
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
        if candidate.startswith("//"):
            return f"https:{candidate}"
        if candidate.startswith("/"):
            return f"{self._base_url}{candidate}"
        return f"{self._base_url}/{candidate}"

    def normalize_url(self, url: str) -> str:
        """Return the normalized form of *url*.

        Normalization is deliberately conservative: the URL is made
        absolute (via :meth:`_resolve_url`) and the fragment is stripped.
        Query strings, path segments and trailing slashes are preserved,
        because stripping them could break sites whose routing depends on
        them.

        Args:
            url: The URL to normalize.

        Returns:
            The normalized absolute URL.
        """
        resolved = self._resolve_url(url)
        parsed = urlparse(resolved)
        return parsed._replace(fragment="").geturl()

    # ------------------------------------------------------------------ #
    #  Identifier helpers                                                #
    # ------------------------------------------------------------------ #
    def _extract_id(self, url: str) -> str:
        """Return the source id of a manga URL.

        The default implementation returns the last non-empty path
        segment, which is correct for every Madara-derived, MangaThemesia,
        API-driven and FoolSlide URL in the project (``/manga/{slug}/``,
        ``/serie/{slug}/``, ``/title/{uuid}``, ``/g/{id}``, etc.).  Parsers
        whose manga URLs do not end in the source id — a rare case, but
        possible for sites that embed the id in a query parameter — must
        override this method.

        Args:
            url: The manga URL (or bare id).

        Returns:
            A non-empty source id.
        """
        return self._last_segment(url)

    def _extract_chapter_id(self, url: str) -> str:
        """Return the source id of a chapter URL.

        The default implementation returns the last non-empty path
        segment.  Parsers whose chapter URLs do not end in the source id
        must override this method.

        Args:
            url: The chapter URL (or bare id).

        Returns:
            A non-empty source id.
        """
        return self._last_segment(url)

    def _make_manga_id(self, url: str) -> str:
        """Return the 16-hex identifier of the manga at *url*.

        The identifier is ``sha256(f"{site_id}:{source_id}")[:16]``, where
        ``source_id`` is :meth:`_extract_id` applied to *url*.  Two calls
        with URLs that resolve to the same source id always produce the
        same 16-hex identifier, which is what makes the library database
        stable across sessions.

        Args:
            url: The manga URL (or bare id).

        Returns:
            The 16-character lowercase hex identifier.
        """
        source_id = self._extract_id(url)
        return self._hash_id(f"{self.site_id}:{source_id}")

    def _make_chapter_id(self, manga_id: str, url: str) -> str:
        """Return the 16-hex identifier of the chapter at *url*.

        The identifier is ``sha256(f"{manga_id}:{source_id}")[:16]``,
        where ``source_id`` is :meth:`_extract_chapter_id` applied to
        *url*.  The chapter identifier is namespaced under its manga, so
        that two chapters with the same source id in different manga get
        different identifiers — which is essential for the library
        database's foreign key constraints.

        Args:
            manga_id: The 16-hex manga identifier returned by
                :meth:`_make_manga_id`.
            url: The chapter URL (or bare id).

        Returns:
            The 16-character lowercase hex identifier.
        """
        source_id = self._extract_chapter_id(url)
        return self._hash_id(f"{manga_id}:{source_id}")

    # ------------------------------------------------------------------ #
    #  Concrete shared helpers                                           #
    # ------------------------------------------------------------------ #
    async def download_page(
        self,
        page: Page,
        dest: Path,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download *page* to *dest* and return the destination path.

        The download goes through
        :meth:`HttpSession.stream_download`, which applies retry, rate
        limiting, circuit breaking, proxy rotation and cookie persistence
        according to the site's configuration.  Any per-site headers (e.g.
        ``Referer`` for sites with hot-link protection) are supplied by
        the session layer based on the site's ``default_headers`` in
        ``data/sites.yaml``; the parser does not add site-specific headers
        here.

        Args:
            page: The :class:`Page` to download.
            dest: The destination file path.  Parent directories are
                expected to exist — creating them is the downloader's
                responsibility, not the parser's, so that a missing parent
                is surfaced as an error rather than silently created.
            on_progress: Optional callback invoked with
                ``(bytes_downloaded, total_bytes)`` during the download.
                ``total_bytes`` is ``0`` when the server does not send a
                ``Content-Length`` header.

        Returns:
            The destination path (same as *dest*), for chaining.
        """
        await self.session.stream_download(
            page.url,
            dest,
            on_progress=on_progress,
        )
        return dest

    async def health_check(self) -> SiteHealth:
        """Return the current health of the site.

        Performs a single ``GET`` of the site's base URL and maps the
        outcome onto :class:`SiteHealth`:

        * ``2xx`` / ``3xx`` → ``HEALTHY``
        * ``4xx`` → ``DEGRADED`` (site responds but rejects our request —
          typically a login wall, a rate limit or a bot check that the
          session's Cloudflare layer could not clear)
        * ``5xx`` → ``DOWN``
        * Transport error → ``DOWN``
        * Unrecognized response shape → ``UNKNOWN``

        The method **never raises**.  A health check that throws is worse
        than useless — the caller would not know the site's status at all.
        Every failure is logged at WARNING level so that repeated failures
        show up in the operator's logs.

        Returns:
            The site's current :class:`SiteHealth`.
        """
        try:
            response = await self.session.get(self._base_url)
        except NexusDLError as exc:
            self._log.warning(
                "Health check failed for site {}: {}", self.site_id, exc
            )
            return SiteHealth.DOWN
        except Exception as exc:  # noqa: BLE001 — health check must not raise
            self._log.warning(
                "Unexpected error during health check for site {}: {}",
                self.site_id,
                exc,
            )
            return SiteHealth.DOWN

        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            self._log.debug(
                "Health check for site {} returned an unrecognized "
                "response type {}.",
                self.site_id,
                type(response).__name__,
            )
            return SiteHealth.UNKNOWN
        if 200 <= status_code < 400:
            return SiteHealth.HEALTHY
        if 400 <= status_code < 500:
            self._log.info(
                "Health check for site {} returned HTTP {} — marking "
                "site as degraded.",
                self.site_id,
                status_code,
            )
            return SiteHealth.DEGRADED
        self._log.warning(
            "Health check for site {} returned HTTP {} — marking site "
            "as down.",
            self.site_id,
            status_code,
        )
        return SiteHealth.DOWN

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hash_id(key: str) -> str:
        """Return the first 16 hex chars of the SHA-256 of *key*.

        This is the canonical identifier derivation scheme used by
        :meth:`_make_manga_id` and :meth:`_make_chapter_id`.  Exposed as a
        static method so that tests, scripts and plugins can derive the
        same identifiers without instantiating a parser.
        """
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _last_segment(url: str) -> str:
        """Return the last non-empty path segment of *url*.

        The trailing slash, the query string and the fragment are ignored.
        When the URL has no path segment (e.g. a bare domain), the URL
        itself — with scheme, netloc and path only — is returned, so that
        the caller always gets a non-empty string to hash.
        """
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        segments = [segment for segment in path.split("/") if segment]
        if segments:
            return segments[-1]
        # Fallback: return the URL without query or fragment.  This covers
        # bare domains and — very unlikely — URLs whose path is just "/".
        return (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            or url
        )
