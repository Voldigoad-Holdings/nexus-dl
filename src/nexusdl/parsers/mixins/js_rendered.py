"""Mixin for parsers that require a JavaScript-rendered DOM.

Several sites in this project — Bato.to, Pururin, MangaFire, MangaSee123,
ToonGod, TCB Scans, Scan-Manga, Reaper Scans, Poseidon Scans, Phenix Scans,
Ortega Scans, Anime-Sama, Blossom Scans, Karma Scans, Genkan Scans and
Asura Scans KR — serve their catalogue, chapter list or reader pages from a
client-side application (Vue, Nuxt, Livewire, custom vanilla JS, RC4-signed
image endpoints, JSON-in-JS variables, etc.).  A plain HTTP GET returns an
empty shell, and no amount of CSS selector tuning can recover the data.

This mixin solves that by providing one primary entry point::

    html = await self._get_rendered_html(url)

which acquires a page from the shared
:class:`~nexusdl.core.session.playwright_pool.PlaywrightPool`, navigates to
the URL, waits for the document to stabilise, and returns the fully-rendered
``str`` HTML — exactly like :meth:`HttpSession.get_html` but with the
browser execution step in between.

Transparent delegation
----------------------
Parsers that override the four :class:`BaseParser` operations directly
(as ``batoto``, ``mangafire``, ``tcbscans`` and others do) simply call
``_get_rendered_html`` instead of ``self.session.get_html`` inside their
own methods.  Nothing else changes.

Parsers that delegate to an existing mixin — most notably the Madara-style
parsers such as ``reaperscans`` — call ``super().search(...)`` and expect
the underlying Madara implementation to fetch the page.  Because that
implementation uses ``self.session.get_html``, this mixin transparently
**patches** ``self.session.get_html`` for the duration of the call so that
every GET performed by the delegated operation is routed through Playwright
instead of plain HTTP.

The patch is installed by the :meth:`_rendering_session` context manager,
which is ``async with``-scoped and always restores the original method in a
``finally`` block.  Multiple concurrent operations on the same parser
instance therefore do not interfere with each other: each operation runs
inside its own context manager, and the patch is installed and removed
atomically within a single event-loop task.

Fallback behaviour
------------------
When the Playwright pool is unavailable (``self._playwright is None``), the
patched ``session.get_html`` falls back to the **original** HTTP method.
This keeps a parser usable — albeit without JavaScript rendering — instead
of raising an error mid-operation.  Direct calls to ``_get_rendered_html``
from a parser that genuinely requires rendering still raise
:class:`~nexusdl.core.exceptions.NexusDLPlaywrightError`, which is the
correct behaviour: those parsers cannot function without Playwright and
should fail loudly rather than silently return useless HTML.

Combining with other mixins
---------------------------
This mixin is designed to be combined with
:class:`~nexusdl.parsers.mixins.cloudflare.CloudflareMixin` whenever the
target site is both JS-rendered **and** Cloudflare-protected::

    class MyParser(JsRenderedMixin, CloudflareMixin, BaseParser):
        ...

The two mixins do not conflict: ``CloudflareMixin`` handles the challenge
at the HTTP level for any request that is *not* routed through Playwright,
and Playwright's own browser execution naturally solves basic JS-based
Cloudflare challenges without any extra coordination.  For interactive
challenges (Turnstile), ``CloudflareMixin`` is responsible for the browser
round-trip and cookie hand-off; the parser may then call
``_get_rendered_html`` as usual with the freshly-obtained cookies already
present on the shared context.

Tuning
------
Two class attributes control the default wait behaviour and can be
overridden on any concrete parser:

* ``_JS_RENDER_WAIT_UNTIL`` — the Playwright ``wait_until`` value; defaults
  to ``"networkidle"``, which is correct for SPAs that fetch their data
  after page load.  Sites with long-polling or websockets that never reach
  ``networkidle`` should override this to ``"domcontentloaded"`` and use a
  ``wait_for_selector`` argument on the call site instead.
* ``_JS_RENDER_TIMEOUT_MS`` — per-navigation timeout in milliseconds;
  defaults to 30 000 ms (Playwright's own default).
* ``_JS_RENDER_WAIT_FOR_SELECTOR`` — optional CSS selector that must appear
  before the HTML is returned; ``None`` (default) disables the extra wait.
  This is the most reliable way to wait for content on SPAs whose network
  activity is dominated by websockets.

All three can also be overridden per call via keyword arguments, which is
useful for one-off pages (e.g. a reader page that loads images slower than
the rest of the site).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from nexusdl.core.exceptions import NexusDLError, NexusDLPlaywrightError

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.config import Settings
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.page import Page
    from nexusdl.core.models.search import SearchResult
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.core.session.playwright_pool import PlaywrightPool


__all__ = ["JsRenderedMixin"]


class JsRenderedMixin:
    """Mixin providing `_get_rendered_html` and transparent Playwright delegation.

    Concrete parsers rely on the following attributes provided by
    :class:`~nexusdl.parsers.base.BaseParser`:

    * ``session`` — the shared :class:`HttpSession`.
    * ``_playwright`` — the shared :class:`PlaywrightPool`, or ``None`` if
      Playwright was not configured.
    * ``_settings`` — the resolved :class:`Settings`.
    * ``_log`` — the bound logger.
    * ``site_id`` — the parser's site id, used in diagnostics.

    These are declared under :data:`TYPE_CHECKING` so mypy resolves
    ``self.<attr>`` inside this file without importing ``BaseParser`` at
    runtime and without creating a circular import.
    """

    #: Playwright ``wait_until`` value used for every navigation.
    _JS_RENDER_WAIT_UNTIL: str = "networkidle"

    #: Per-navigation timeout, in milliseconds.
    _JS_RENDER_TIMEOUT_MS: int = 30_000

    #: Optional CSS selector that must appear before the HTML is returned.
    _JS_RENDER_WAIT_FOR_SELECTOR: str | None = None

    if TYPE_CHECKING:
        site_id: str
        session: "HttpSession"
        _playwright: "PlaywrightPool" | None
        _settings: "Settings"
        _log: "Logger"

    # ------------------------------------------------------------------ #
    #  Public rendering entry point                                      #
    # ------------------------------------------------------------------ #
    async def _get_rendered_html(
        self,
        url: str,
        *,
        wait_until: str | None = None,
        timeout_ms: int | None = None,
        wait_for_selector: str | None = None,
    ) -> str:
        """Return the fully rendered HTML of *url* as a string.

        The method acquires a page from the shared Playwright pool, navigates
        to ``url``, optionally waits for a specific CSS selector to appear,
        and returns ``page.content()``.  The page is always closed in a
        ``finally`` block when it was created by this method; when the pool
        yields a page that it owns, the pool is responsible for closing it
        and this method does not interfere.

        Args:
            url: Absolute URL to render.  Relative URLs are not resolved
                here — the caller is expected to pass a fully-qualified URL
                (typically the result of :meth:`BaseParser._resolve_url`).
            wait_until: Playwright ``wait_until`` value.  Defaults to
                :attr:`_JS_RENDER_WAIT_UNTIL`.
            timeout_ms: Per-navigation timeout in milliseconds.  Defaults
                to :attr:`_JS_RENDER_TIMEOUT_MS`.
            wait_for_selector: Optional CSS selector that must appear
                before the HTML is returned.  Defaults to
                :attr:`_JS_RENDER_WAIT_FOR_SELECTOR`.

        Returns:
            The rendered HTML as a ``str``.

        Raises:
            NexusDLPlaywrightError: If the Playwright pool was not injected
                into this parser, or if the browser fails to acquire a page
                or to navigate to ``url``.
        """
        if self._playwright is None:
            raise NexusDLPlaywrightError(
                f"Playwright is required to render pages for site "
                f"{self.site_id!r}, but no PlaywrightPool was injected. "
                f"Ensure `settings.playwright.enabled` is true and the "
                f"composition root passes the pool to the parser."
            )

        resolved_wait_until = wait_until or self._JS_RENDER_WAIT_UNTIL
        resolved_timeout = timeout_ms or self._JS_RENDER_TIMEOUT_MS
        resolved_selector = (
            wait_for_selector
            if wait_for_selector is not None
            else self._JS_RENDER_WAIT_FOR_SELECTOR
        )

        self._log.debug(
            "Rendering {} (wait_until={}, timeout={}ms, selector={})",
            url,
            resolved_wait_until,
            resolved_timeout,
            resolved_selector,
        )

        async with self._playwright.acquire() as acquired:
            page, owns_page = await self._page_from_acquired(acquired)
            try:
                try:
                    await page.goto(
                        url,
                        wait_until=resolved_wait_until,
                        timeout=resolved_timeout,
                    )
                except Exception as exc:
                    raise NexusDLPlaywrightError(
                        f"Failed to navigate to {url!r} for site "
                        f"{self.site_id!r}: {exc}"
                    ) from exc

                if resolved_selector is not None:
                    try:
                        await page.wait_for_selector(
                            resolved_selector,
                            timeout=resolved_timeout,
                        )
                    except Exception as exc:
                        raise NexusDLPlaywrightError(
                            f"Timed out waiting for selector "
                            f"{resolved_selector!r} on {url!r} for site "
                            f"{self.site_id!r}: {exc}"
                        ) from exc

                return await page.content()
            finally:
                if owns_page:
                    try:
                        await page.close()
                    except Exception as exc:  # noqa: BLE001 — cleanup must never raise
                        self._log.debug(
                            "Ignoring error while closing rendered page: {}",
                            exc,
                        )

    # ------------------------------------------------------------------ #
    #  Transparent delegation for Madara-style parsers                   #
    # ------------------------------------------------------------------ #
    async def search(
        self,
        query: str,
        *,
        page: int = 1,
    ) -> AsyncIterator["SearchResult"]:
        """Delegate :meth:`search` to the next mixin with rendering enabled.

        This override is only reached when a concrete parser calls
        ``super().search(...)``.  Parsers that define ``search`` directly
        (as most do) bypass this method entirely and use
        ``_get_rendered_html`` themselves.
        """
        async with self._rendering_session():
            async for result in super().search(query, page=page):  # type: ignore[misc]
                yield result

    async def get_manga(self, url_or_id: str) -> "Manga":
        """Delegate :meth:`get_manga` to the next mixin with rendering enabled.

        See :meth:`search` for the rationale.
        """
        async with self._rendering_session():
            return await super().get_manga(url_or_id)  # type: ignore[misc]

    async def get_chapters(self, manga: "Manga") -> list["Chapter"]:
        """Delegate :meth:`get_chapters` to the next mixin with rendering enabled.

        See :meth:`search` for the rationale.
        """
        async with self._rendering_session():
            return await super().get_chapters(manga)  # type: ignore[misc]

    async def get_pages(self, chapter: "Chapter") -> list["Page"]:
        """Delegate :meth:`get_pages` to the next mixin with rendering enabled.

        See :meth:`search` for the rationale.
        """
        async with self._rendering_session():
            return await super().get_pages(chapter)  # type: ignore[misc]

    # ------------------------------------------------------------------ #
    #  Session patching                                                  #
    # ------------------------------------------------------------------ #
    @asynccontextmanager
    async def _rendering_session(self):
        """Route ``self.session.get_html`` through Playwright for the block.

        Installs a replacement for ``self.session.get_html`` that renders
        pages via :meth:`_get_rendered_html`, then restores the original
        method in a ``finally`` block.  When the Playwright pool is absent,
        the replacement transparently falls back to the original HTTP
        method so that delegated operations still complete (without JS
        rendering) instead of raising.

        The replacement only affects ``get_html``.  Other session methods
        (``get``, ``post``, ``get_json``, ``stream_download``) keep their
        normal HTTP behaviour, which is what the Madara-family mixins
        expect for the AJAX chapter endpoint and for image downloads.
        """
        original_get_html = self.session.get_html

        async def _rendered_get_html(url: str, **kwargs: Any) -> str:
            if self._playwright is None:
                # Playwright was not injected — fall back to plain HTTP so
                # the delegated operation still completes as far as it can.
                self._log.debug(
                    "Playwright unavailable; falling back to HTTP for {} "
                    "on site {}",
                    url,
                    self.site_id,
                )
                return await original_get_html(url, **kwargs)
            return await self._get_rendered_html(url)

        # Bind the replacement onto the session instance.  Restored in the
        # ``finally`` block below.
        self.session.get_html = _rendered_get_html  # type: ignore[method-assign]
        try:
            yield
        finally:
            self.session.get_html = original_get_html  # type: ignore[method-assign]

    # ------------------------------------------------------------------ #
    #  Page acquisition helper                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _page_from_acquired(acquired: Any) -> tuple[Any, bool]:
        """Return ``(page, owns_page)`` from the value yielded by the pool.

        :class:`~nexusdl.core.session.playwright_pool.PlaywrightPool` is
        contractually allowed to yield either a ``BrowserContext`` (from
        which we create a fresh page) or a ready-to-use ``Page``.  This
        helper normalises both shapes:

        * If *acquired* already exposes ``goto`` and ``content``, it is a
          page — use it directly and report ``owns_page = False`` so the
          caller does not close it (the pool owns its lifecycle).
        * Otherwise, treat *acquired* as a context and create a new page
          from it.  Report ``owns_page = True`` so the caller closes the
          page in ``finally``.

        Raises:
            NexusDLPlaywrightError: If *acquired* supports neither shape.
        """
        if hasattr(acquired, "goto") and hasattr(acquired, "content"):
            return acquired, False
        if hasattr(acquired, "new_page"):
            page = await acquired.new_page()
            return page, True
        raise NexusDLPlaywrightError(
            f"PlaywrightPool.acquire() yielded an unsupported object of "
            f"type {type(acquired).__name__!r}; expected a BrowserContext "
            f"or a Page."
        )
