"""Structural protocols at the parser / HTTP-session boundary.

This module is the **contract layer** between the framework's core and
the two subsystems it depends on without importing: the parsers
(``nexusdl.parsers.*``) and the HTTP session
(``nexusdl.core.session.http_session``). It exists so that:

* :mod:`nexusdl.core.registry.site_registry` can hold a
  :class:`ParserFactory` and return parser objects **without importing**
  the parser package at runtime (PROMPT MASTER rule R1).
* The composition root can inject any implementation that satisfies the
  structural contract — the shipped
  :class:`~nexusdl.parsers.factory.DefaultParserFactory`, a test stub, a
  plugin-provided factory — and the registry never notices.
* Every consumer in :mod:`nexusdl.core` shares a **single, canonical**
  description of "what a parser is", instead of each module inventing
  its own narrow view that can drift.

ADR-003 is the source of the "inject a Protocol, not a class" rule.
:class:`ParserFactory` in particular is pinned to this location by the
PROMPT MASTER; the other protocols live here so they can be shared
without duplication.

Three protocols, three concerns
-------------------------------
* :class:`HttpSessionProtocol` — the **minimal HTTP surface** parsers
  need. A concrete
  :class:`~nexusdl.core.session.http_session.HttpSession` satisfies it
  structurally; so does every test double. It declares only the five
  methods that a parser realistically uses — ``get``, ``post``,
  ``get_html``, ``get_json``, ``stream_download`` — so a test double
  only has to implement what it actually exercises.
* :class:`ParserProtocol` — the **complete parser surface** as consumed
  by :mod:`nexusdl.core`. It is the union of every method that any core
  module calls:

  * :meth:`ParserProtocol.search` — used by
    :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.search_all`
  * :meth:`ParserProtocol.get_manga` — used by the manager when the
    caller omits the manga argument of ``download_manga``
  * :meth:`ParserProtocol.get_chapters` — used by the manager's
    ``download_manga`` for the same reason
  * :meth:`ParserProtocol.get_pages` — used by
    :meth:`~nexusdl.core.downloader.worker.DownloadWorker._download_one_page`
  * :meth:`ParserProtocol.download_page` — same call site
  * :meth:`ParserProtocol.health_check` — used by
    :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.health_check`

  A parser author who wants ``mypy --strict`` conformance imports this
  protocol and asserts ``_: ParserProtocol = MyParser()`` under
  ``TYPE_CHECKING`` — exactly the pattern used by
  :class:`~nexusdl.plugins.api.NexusPlugin`.
* :class:`ParserFactory` — the **factory contract**. Its sole
  responsibility is to turn a :class:`SiteConfig` and an HTTP session
  into a fresh parser instance. Defined as a protocol so the registry
  can accept any implementation without a runtime import of the parser
  package.

Name aliases
------------
Two names are exported for the HTTP protocol:

* :class:`HttpSessionProtocol` (canonical) and :data:`HttpSession`
  (alias, imported by :mod:`nexusdl.core.registry.site_registry` under
  ``TYPE_CHECKING``).
* :class:`ParserProtocol` (canonical, no alias needed).
* :class:`ParserFactory` (canonical, no alias needed).

The alias :data:`HttpSession` exists solely so that consumer modules
can use the shorter, more natural name in type annotations without a
cross-module dependency on this file's naming conventions. Both names
resolve to the same underlying class object — ``HttpSession is
HttpSessionProtocol`` evaluates to ``True``.

Why narrow per-module protocols still exist
-------------------------------------------
:mod:`nexusdl.core.downloader.worker` and
:mod:`nexusdl.core.downloader.manager` each define their **own** local
``ParserProtocol`` — a narrow subset of this one. That is not
duplication: it is deliberate isolation. The worker needs
``get_pages`` and ``download_page`` only; declaring those two in the
worker's own module means the worker cannot accidentally depend on
``search`` or ``get_chapters``. A concrete
:class:`~nexusdl.parsers.base.BaseParser` (or any parser satisfying
this module's full :class:`ParserProtocol`) **also** satisfies the
worker's narrow protocol via structural subtyping, so passing one
through the pipeline works without any cast.

The rule of thumb across the codebase:

* **Leaf-injected dependencies** (worker, manager) declare narrow
  protocols locally — the smallest surface that satisfies the leaf.
* **Shared registry-facing contracts** (the parser factory, the full
  parser surface) live here in ``protocols.py`` so multiple consumers
  can share them and a parser author has one canonical target to
  satisfy.

.. warning::
   **Contract note on :meth:`ParserProtocol.search`.**

   The signature below declares ``search`` as returning a
   :class:`~nexusdl.core.models.search.SearchResultPage` object
   (``await parser.search(...)``). However,
   :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.search_all`
   in ``site_registry.py`` was written against an **async-generator**
   contract (``async for hit in parser.search(...)``). The two cannot
   both be right for the same implementation:

   * If the shipped ``BaseParser.search`` returns a
     ``SearchResultPage``, then ``site_registry.search_all`` must
     ``await`` the call before iterating its ``.items`` — and the
     adapter ``_call_parser_search`` there (added to support both
     shapes defensively) is doing work that is not necessary.
   * If the shipped ``BaseParser.search`` is an ``async generator``
     (yielding individual ``SearchResult`` objects), then this
     protocol's return type is **wrong** and should be
     ``AsyncIterator[SearchResult]`` — a change that would make
     ``site_registry.search_all`` (as shipped) the correct
     implementation and this docstring the one that is behind.

   **Verify against ``parsers/base.py::BaseParser.search`` in the next
   audit and align the two files in the same commit.** The rest of this
   module is unaffected by the choice — only ``search``'s return
   annotation and the corresponding code path in ``search_all`` depend
   on it.

Why the protocols are not ``@runtime_checkable``
------------------------------------------------
:class:`ParserProtocol` declares data members (``site``, ``site_id``)
that would cause ``issubclass()`` to raise ``TypeError`` on a
``@runtime_checkable`` protocol. The framework does not need
``isinstance`` checks — the loader, the factory, and the worker all
access members via ``getattr`` or via the type system at check time.
Consequently none of the three protocols is decorated, and no consumer
relies on a runtime check. A plugin that wants to verify conformance at
import time does so via ``mypy --strict``, not via ``isinstance``.

Dependency graph (no cycles)::

    core.models.chapter
    core.models.manga
    core.models.page
    core.models.search
    core.models.site
    (all leaf models)
         ▲
         │
    core.registry.protocols  ← you are here (TYPE_CHECKING imports only)
         ▲
         │
    core.registry.site_registry
    (and the composition root)

``protocols.py`` itself has **no runtime imports**. Every dependency is
under ``TYPE_CHECKING`` so the module imports in microseconds, which
matters because it sits on the critical path of
:class:`~nexusdl.core.registry.site_registry`'s own import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.page import Page
    from nexusdl.core.models.search import SearchResultPage
    from nexusdl.core.models.site import SiteConfig


__all__ = [
    "HttpSession",
    "HttpSessionProtocol",
    "ParserFactory",
    "ParserProtocol",
]


#: Signature of the progress callback accepted by
#: :meth:`HttpSessionProtocol.stream_download`. Called once per chunk
#: with ``(bytes_downloaded, bytes_total_or_-1)`` — the same convention
#: :mod:`tqdm` and :mod:`rich.progress` use, and the one the image
#: downloader and the worker already expect.
ProgressCallback = Callable[[int, int], None]


# --------------------------------------------------------------------------- #
# HttpSessionProtocol                                                         #
# --------------------------------------------------------------------------- #


class HttpSessionProtocol(Protocol):
    """The HTTP surface a parser needs from an :class:`HttpSession`.

    Satisfied structurally by
    :class:`~nexusdl.core.session.http_session.HttpSession` and by any
    test double that implements the five methods below. Parsers accept an
    ``HttpSessionProtocol`` — never a concrete ``HttpSession`` — so a
    test that constructs a parser against a fake session does not have to
    stand up the full session machinery (rate limiter, circuit breaker,
    cookie manager, cache, …).

    The five methods are the only operations a parser realistically
    performs on a session. Anything else (circuit-breaker introspection,
    cookie mutation, cache control) is a session-layer concern that a
    parser must not reach into — exposing those would be a leak of the
    session's internal state into the parser's interface.

    Not ``@runtime_checkable`` — see the module docstring.
    """

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Perform a GET request and return the raw response.

        Used when a parser needs to inspect status code, headers, or
        stream the response body itself (Cloudflare mixin, image
        streaming with per-chunk progress, …).

        The four keyword arguments mirror the corresponding parameters
        of :meth:`httpx.AsyncClient.get`, minus the ones that would
        let a parser bypass the session's own policy (rate limiting,
        circuit breaker, retries). Anything else a caller needs is a
        session concern, not a parser concern.
        """
        ...

    async def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Perform a POST request and return the raw response.

        Used by parsers that drive a form login, a JSON-based API, or a
        search endpoint that requires a body. The session layer applies
        the same rate limiting, circuit breaker, and retry policy as
        :meth:`get` — the choice of HTTP verb does not change the
        pipeline's safety envelope.
        """
        ...

    async def get_html(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Perform a GET request and return the response body as text.

        The default for the vast majority of parser scraping calls.
        """
        ...

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Perform a GET request and decode the body as JSON.

        Used by the API-based mixin and by API-first parsers
        (MangaDex, Comick, …).
        """
        ...

    async def stream_download(
        self,
        url: str,
        dest: Path,
        *,
        on_progress: ProgressCallback | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Stream *url* into the file at *dest*.

        ``on_progress`` follows the :data:`ProgressCallback` convention
        — ``(bytes_downloaded, bytes_total_or_-1)`` — the same shape
        :mod:`tqdm` and :mod:`rich.progress` expect, and the one the
        image downloader and the worker already consume. A caller that
        does not care about progress passes ``None``.

        ``headers``, ``cookies``, and ``timeout`` mirror
        :meth:`get` — a streaming download often needs the same auth
        headers and per-site timeout as a regular GET.
        """
        ...


#: Alias for :class:`HttpSessionProtocol`.
#:
#: Consumers (notably :mod:`nexusdl.core.registry.site_registry`, which
#: imports ``HttpSession`` under ``TYPE_CHECKING``) can use either name;
#: both resolve to the same class object. The alias exists so that the
#: more natural short form is available without introducing a second,
#: independently defined protocol that could drift from this one.
HttpSession = HttpSessionProtocol


# --------------------------------------------------------------------------- #
# ParserProtocol                                                              #
# --------------------------------------------------------------------------- #


class ParserProtocol(Protocol):
    """The complete parser surface as consumed by the framework's core.

    Every method and attribute below is accessed by at least one core
    module. A concrete parser (typically a subclass of
    :class:`~nexusdl.parsers.base.BaseParser`) must satisfy all of them
    to be usable across the pipeline; a **test double** used for a
    specific module (e.g. a fake parser that only needs
    ``get_pages``/``download_page`` for the worker tests) can satisfy
    that module's narrow local protocol instead.

    Static conformance is verified by ``mypy --strict`` in the parser's
    own file via the pattern shown in
    :mod:`nexusdl.plugins._example.plugin`:

    .. code-block:: python

        if TYPE_CHECKING:
            from nexusdl.core.registry.protocols import ParserProtocol
            _: ParserProtocol = MyParser()

    Attributes
    ----------
    site_id:
        Identifier of the site this parser handles. Stable, lowercase,
        matching :attr:`~nexusdl.core.models.site.SiteConfig.id`. Used
        by the registry for logging and by diagnostic messages.
    site:
        The full :class:`~nexusdl.core.models.site.SiteConfig` the
        parser was constructed against. Read-only; the parser must not
        mutate it (the config is a frozen dataclass, so mutation
        attempts raise anyway).
    language:
        The parser's primary content language, as an ISO 639-1 code.
        Informational — the registry already knows the language from
        :attr:`site`, and no code path uses this attribute to make
        decisions. Declared so that a future language-aware router can
        consult it without a ``getattr`` chain.
    adult:
        Whether the parser serves adult content. Informational, same
        contract as :attr:`language`.
    """

    site_id: str
    site: SiteConfig
    language: str
    adult: bool

    # ------------------------------------------------------------------ #
    # Search                                                             #
    # ------------------------------------------------------------------ #

    async def search(
        self, query: str, *, page: int = 1
    ) -> SearchResultPage:
        """Return one page of search results for *query*.

        Called by :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.search_all`
        with ``page=1``; the registry does not paginate itself. A parser
        whose :attr:`~nexusdl.core.models.site.SiteCapabilities.search`
        is ``False`` may raise
        :class:`~nexusdl.core.exceptions.ParserResponseError` — the
        registry's eligibility filter normally prevents the call, but a
        defensive raise is allowed.

        .. warning::
           The return type and the ``await``-vs-``async for``
           consumption differ between this protocol and the current
           implementation of
           :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.search_all`.
           Verify against ``parsers/base.py::BaseParser.search`` before
           relying on either shape. See the module docstring's contract
           note.

        Args:
            query: The user-supplied query string, verbatim.
            page: 1-indexed page number. Must be ``>= 1``.

        Returns:
            A :class:`~nexusdl.core.models.search.SearchResultPage`
            whose ``site_id`` matches :attr:`site_id`.
        """
        ...

    # ------------------------------------------------------------------ #
    # Manga, chapters, pages                                             #
    # ------------------------------------------------------------------ #

    async def get_manga(self, url_or_id: str) -> Manga:
        """Fetch a manga by its canonical URL or by the site's opaque id.

        Called by the manager's ``download_manga`` when the caller omits
        the manga argument, and by any code that needs to upgrade a
        search result to a full
        :class:`~nexusdl.core.models.manga.Manga`.

        Args:
            url_or_id: The manga's absolute URL on the site, or the
                site's opaque source id. The parser accepts both.

        Returns:
            A fully populated :class:`~nexusdl.core.models.manga.Manga`.
        """
        ...

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the ordered list of chapters for *manga*.

        Called by the manager's ``download_manga`` when the caller omits
        the chapter list. Ordering is the parser's responsibility; the
        library's ``get_chapters`` API applies its own sort afterwards
        if needed.

        Args:
            manga: The manga whose chapters to fetch.

        Returns:
            A list of :class:`~nexusdl.core.models.chapter.Chapter`
            instances. May be empty.
        """
        ...

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered list of pages for *chapter*.

        Called by the worker's ``_download_pages``. The list is used
        to determine page count, filenames, and download URLs. An empty
        list raises ``EmptyChapterError`` in the worker.

        Args:
            chapter: The chapter whose pages to fetch.

        Returns:
            A list of :class:`~nexusdl.core.models.page.Page` instances,
            in reading order (``page.index`` ascending).
        """
        ...

    async def download_page(self, page: Page, dest: Path) -> None:
        """Download *page* to the file path *dest*.

        Called by the worker's ``_download_one_page``. The parser is
        responsible for the network I/O; the worker is responsible for
        validation and retry policy (which is why this method should
        raise on failure rather than retry internally — a parser-internal
        retry would fight the worker's retry controller).

        Args:
            page: The page to download.
            dest: Destination file path. The file is created or truncated.
        """
        ...

    # ------------------------------------------------------------------ #
    # Health                                                             #
    # ------------------------------------------------------------------ #

    async def health_check(self) -> None:
        """Check that the site is reachable and its markup is parseable.

        Called by
        :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.health_check`.
        The contract is **raise on failure, return on success** — a
        parser that returns ``None`` without raising declares itself
        healthy. This is why no return type is used to encode the
        outcome: it would be ambiguous whether an empty page or an empty
        result set counts as healthy.

        A parser that cannot meaningfully probe the site (e.g. an
        integration-test stub) may implement this as a no-op — the
        registry treats a successful no-op return as ``HEALTHY``.
        """
        ...


# --------------------------------------------------------------------------- #
# ParserFactory                                                               #
# --------------------------------------------------------------------------- #


class ParserFactory(Protocol):
    """Resolve a :class:`SiteConfig` to a fresh parser instance.

    Satisfied by
    :class:`~nexusdl.parsers.factory.DefaultParserFactory` in production
    and by every test double. The registry holds one instance of this
    protocol and calls :meth:`create` on every
    :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.get_parser`
    — a fresh parser per call, so concurrent downloads do not share
    parser state.

    The factory's job stops at construction: it does not validate the
    site beyond what
    :class:`~nexusdl.core.models.site.SiteConfig` already guarantees, and
    it does not cache parser instances. Any state a parser needs comes
    from the ``SiteConfig`` and the session passed to :meth:`create`.

    Not ``@runtime_checkable`` — see the module docstring.
    """

    def create(
        self,
        site: SiteConfig,
        session: HttpSessionProtocol,
    ) -> ParserProtocol:
        """Return a parser for *site*, bound to *session*.

        Args:
            site: The site configuration the parser must handle. The
                factory is expected to resolve
                :attr:`~nexusdl.core.models.site.SiteConfig.parser_class`
                (``"module:ClassName"``) to the concrete class, instantiate
                it, and return it. A site whose ``parser_class`` cannot be
                resolved raises
                :class:`~nexusdl.core.exceptions.ParserNotFoundError` (or
                the factory's own error, which the registry wraps).

            session: The HTTP session the parser should use for network
                I/O. Passed through unchanged — the factory does not
                inspect it.

        Returns:
            A parser satisfying :class:`ParserProtocol`.
        """
        ...
