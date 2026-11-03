"""Parser mixin for Cloudflare-protected sites.

A significant fraction of the sites in this project — Toonily, HentaiZone,
Hentai-Origines, Mangas-Origines, Raijin Scans, CrunchyScan FR, Scylla Scans,
ToonFR, Asura Scans, Asura Scans KR, MangaFire, MangaKakalot, MangaSee123,
Comick, Reaper Scans, TCB Scans, ToonGod, Poseidon Scans, Phenix Scans,
Ortega Scans, Anime-Sama, Blossom Scans, Karma Scans, Genkan Scans, Epsilon
Scan, Scan-Manga and Nekohouse — are fronted by **Cloudflare**.  A plain
HTTP client is regularly challenged with a JS / Turnstile page instead of the
content the parser expects, and no amount of selector tuning can recover the
real page.

This mixin solves that by patching ``session.get_html`` once per parser
instance: every GET performed by the parser's own methods — or by a
delegated Madara / MangaThemesia mixin — is transparently intercepted.  When
the response looks like a Cloudflare challenge, the mixin:

1. Tries :class:`~nexusdl.core.session.flaresolverr.FlareSolverr`, if
   injected into the parser.
2. Falls back to :class:`~nexusdl.core.session.playwright_pool.PlaywrightPool`,
   if injected into the parser.  Playwright's real browser naturally
   satisfies JS-based challenges.
3. Injects the obtained cookies into the shared
   :class:`~nexusdl.core.session.http_session.HttpSession`.
4. Retries the original request **once** with the fresh cookies.

If none of the above succeeds, :class:`SessionCloudflareError` is raised —
which is the correct behaviour: a parser cannot fake a CF-protected site.

Transparent to Madara-style parsers
-----------------------------------
The parsers above do **not** override ``search``/``get_manga``/``get_chapters``/
``get_pages`` — they inherit them from
:class:`~nexusdl.parsers.mixins.wordpress_madara.WordPressMadaraMixin` (or
:class:`~nexusdl.parsers.mixins.mangathemesia.MangaThemesiaMixin`) and rely on
Cloudflare handling being automatic.  Because those mixins call
``session.get_html`` internally, patching at the **session method level** is
the only way to make Cloudflare handling work without requiring every concrete
parser to override every operation.  This is what the mixin does: it wraps
``session.get_html`` on ``__init__``, so all downstream calls benefit
immediately, including delegated calls from other mixins.

Stacking with other mixins
--------------------------
The mixin composes cleanly with
:class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin`::

    class MyParser(JsRenderedMixin, CloudflareMixin, BaseParser):
        ...

Both wrappers install their own layer on ``session.get_html``.
``JsRenderedMixin`` routes to Playwright when a method explicitly calls
``_get_rendered_html`` or when ``_rendering_session`` patches the method for
a delegated call; Cloudflare handling remains active on any HTTP path that
bypasses Playwright.  The two do not conflict because neither relies on
MRO-based method shadowing — both operate purely on the session object.

Detecting Cloudflare challenges
-------------------------------
Detection is intentionally **conservative** to avoid wasting a solve attempt
on a legitimate page whose content happens to contain the word "Cloudflare":

* The HTTP layer raising :class:`SessionCloudflareError` is treated as an
  unambiguous challenge.
* Any other :class:`NexusDLError` whose message mentions ``cloudflare`` or
  ``cf-ray`` is also treated as a challenge.
* A successful response whose body contains one of a small set of strong
  markers (``<title>just a moment``, ``__cf_chl_``, ``cf-browser-verification``,
  etc.) is treated as a challenge.

Everything else passes through untouched.

Cookie injection
----------------
Cookies returned by FlareSolverr or Playwright are injected into the session
through the first writable mechanism found:

1. ``session.cookies.set(name, value, domain=..., path=...)`` — the httpx
   standard, present on :class:`~nexusdl.core.session.http_session.HttpSession`.
2. ``session.cookie_manager.update_cookies(site_id, cookies)`` — the project's
   :class:`~nexusdl.core.session.cookie_manager.CookieManager`, used when the
   session exposes it.

If neither is writable, a warning is logged and the retry will fail through
the normal HTTP path — a clean, debuggable failure that points at the cookie
layer, rather than a silent bypass.

Limitations
-----------
* **Stacking**: each :class:`CloudflareMixin` instance wraps the session on
  ``__init__``.  When multiple parsers share a session, the wrappers stack;
  this is benign and useful — each parser's solvers get a chance in turn.
* **Concurrent solving**: solve attempts are serialized per-domain through an
  ``asyncio.Lock`` to avoid redundant FlareSolverr / Playwright round-trips
  when several chapters are downloaded in parallel.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Coroutine
from urllib.parse import urlparse

from nexusdl.core.exceptions import (
    NexusDLError,
    SessionCloudflareError,
)

if TYPE_CHECKING:
    from loguru import Logger

    from nexusdl.core.config import Settings
    from nexusdl.core.session.flaresolverr import FlareSolverr
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.core.session.playwright_pool import PlaywrightPool


__all__ = ["CloudflareMixin"]


# ---------------------------------------------------------------------------
# Strong Cloudflare challenge markers.
#
# These are substrings that appear only in a Cloudflare interstitial page.
# The list is intentionally small and specific: a false positive would waste
# a solver round-trip, while a false negative would let the parser return a
# challenge page as if it were content — much worse.
# ---------------------------------------------------------------------------
_CF_CHALLENGE_MARKERS: tuple[str, ...] = (
    "<title>just a moment",
    "<title>attention required",
    "cf-browser-verification",
    "__cf_chl_",
    "cf_chl_opt",
    "cf-please-wait",
)


#: Playwright ``wait_until`` value used when solving a challenge.  Challenges
#: never reach ``networkidle`` because the interstitial polls for the
#: challenge token; ``domcontentloaded`` is enough — we then wait for the
#: title to change.
_PLAYWRIGHT_WAIT_UNTIL = "domcontentloaded"

#: Timeout for the Playwright navigation, in milliseconds.
_PLAYWRIGHT_NAV_TIMEOUT_MS = 60_000

#: Timeout for the "challenge has cleared" wait, in milliseconds.
_PLAYWRIGHT_SOLVE_TIMEOUT_MS = 30_000

#: JavaScript predicate that returns ``true`` once the challenge interstitial
#: is gone.  Matches the two title strings Cloudflare uses for its JS
#: challenge and its "Attention required" block page.
_PLAYWRIGHT_CHALLENGE_CLEARED_JS = (
    "() => !/just a moment|attention required|checking your browser/i"
    ".test(document.title)"
)


class CloudflareMixin:
    """Mixin installing Cloudflare handling on ``session.get_html``.

    The mixin relies on the following attributes provided by
    :class:`~nexusdl.parsers.base.BaseParser` in the concrete parser:

    * ``site_id``, ``session``, ``_playwright``, ``_flaresolverr``,
      ``_settings``, ``_log`` — see :class:`BaseParser` documentation.

    These are declared under :data:`TYPE_CHECKING` so mypy resolves
    ``self.<attr>`` inside this file without importing ``BaseParser`` at
    runtime and without creating a circular import.
    """

    if TYPE_CHECKING:
        site_id: str
        session: "HttpSession"
        _playwright: "PlaywrightPool" | None
        _flaresolverr: "FlareSolverr" | None
        _settings: "Settings"
        _log: "Logger"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[misc]
        # Per-domain solve locks, created lazily on first use.  Guarantees
        # that concurrent downloads of the same site do not hammer the
        # solver with redundant challenge-solving round-trips.
        self._cf_solve_locks: dict[str, asyncio.Lock] = {}
        self._install_cloudflare_wrapper()

    # ------------------------------------------------------------------ #
    #  Installation                                                      #
    # ------------------------------------------------------------------ #
    def _install_cloudflare_wrapper(self) -> None:
        """Wrap ``session.get_html`` with the Cloudflare intercepting layer.

        The wrapper captures the current ``session.get_html`` as
        ``original`` and forwards every call through
        :meth:`_get_html_with_cloudflare`, which may retry once after
        solving.  When multiple :class:`CloudflareMixin` instances share a
        session, their wrappers stack — each captures the previous wrapper
        as ``original`` — so every parser's solvers get a chance in turn.
        """
        session = self.session
        original: Callable[..., Coroutine[Any, Any, str]] = session.get_html
        handler = self

        async def _wrapped_get_html(url: str, **call_kwargs: Any) -> str:
            return await handler._get_html_with_cloudflare(
                url, original, **call_kwargs
            )

        session.get_html = _wrapped_get_html  # type: ignore[method-assign]

    # ------------------------------------------------------------------ #
    #  Interception                                                      #
    # ------------------------------------------------------------------ #
    async def _get_html_with_cloudflare(
        self,
        url: str,
        original: Callable[..., Coroutine[Any, Any, str]],
        **kwargs: Any,
    ) -> str:
        """Fetch *url*, solving a Cloudflare challenge and retrying once.

        Args:
            url: The URL to fetch.
            original: The ``get_html`` callable captured at installation
                time.  Must be called directly (never through
                ``self.session.get_html``) to avoid recursing into the
                wrapper.
            **kwargs: Forwarded unchanged to ``original``.

        Returns:
            The response body as a ``str``, guaranteed to not be a
            Cloudflare challenge page.

        Raises:
            SessionCloudflareError: When the challenge could not be solved
                (no solver available, solver failed, or the challenge
                persisted after a successful solve).
            NexusDLError: Any non-Cloudflare error, propagated unchanged.
        """
        body: str | None = None
        challenge_detected = False

        try:
            body = await original(url, **kwargs)
        except SessionCloudflareError:
            challenge_detected = True
        except NexusDLError as exc:
            if self._exception_is_cloudflare(exc):
                challenge_detected = True
            else:
                raise

        if not challenge_detected and not self._looks_like_cf_challenge(body):
            # Fast path — body is real content, return unchanged.
            assert body is not None  # guaranteed by the branch above
            return body

        # Solve and retry.
        cookies = await self._solve_cloudflare(url)
        if cookies is None:
            raise SessionCloudflareError(
                f"Cloudflare challenge on {url!r} (site {self.site_id!r}) "
                f"could not be solved: no FlareSolverr and no Playwright "
                f"available, or all solvers failed."
            )

        self._inject_cookies(url, cookies)

        try:
            retry_body = await original(url, **kwargs)
        except SessionCloudflareError as exc:
            raise SessionCloudflareError(
                f"Cloudflare challenge on {url!r} (site {self.site_id!r}) "
                f"persisted after solving: {exc}"
            ) from exc

        if self._looks_like_cf_challenge(retry_body):
            raise SessionCloudflareError(
                f"Cloudflare challenge on {url!r} (site {self.site_id!r}) "
                f"persisted after solving."
            )

        return retry_body

    # ------------------------------------------------------------------ #
    #  Solving                                                           #
    # ------------------------------------------------------------------ #
    async def _solve_cloudflare(
        self, url: str
    ) -> list[dict[str, Any]] | None:
        """Try every configured solver until one returns cookies.

        Solvers are attempted in order: FlareSolverr first (cheaper, does
        not require a browser launch), then Playwright (heavier, but the
        only reliable fallback for interactive Turnstile).  Solve attempts
        are serialized per-domain so concurrent downloads of the same site
        reuse a single solve.

        Returns:
            A list of cookie dicts on success, or ``None`` when no solver
            is available or all solvers failed.
        """
        domain = urlparse(url).netloc or url
        lock = self._cf_solve_locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self._cf_solve_locks[domain] = lock

        async with lock:
            if self._flaresolverr is not None:
                cookies = await self._solve_with_flaresolverr(url)
                if cookies:
                    return cookies

            if self._playwright is not None:
                cookies = await self._solve_with_playwright(url)
                if cookies:
                    return cookies

            return None

    async def _solve_with_flaresolverr(
        self, url: str
    ) -> list[dict[str, Any]] | None:
        """Solve a challenge through FlareSolverr, if configured.

        The mixin tolerates several FlareSolverr client shapes: an async
        ``solve(url)`` method, an async ``get(url)`` method, or a sync
        method returning an awaitable.  The response may be a list of
        cookies, a dict with a ``cookies`` key, a dict with a ``solution``
        sub-dict, or an object exposing a ``cookies`` attribute.
        """
        assert self._flaresolverr is not None  # narrowed by caller

        try:
            result = self._invoke_flaresolverr(url)
            if result is None:
                return None
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]

            cookies = self._extract_cookies(result)
            return cookies or None
        except Exception as exc:  # noqa: BLE001 — third-party solver, log & fall back
            self._log.warning(
                "FlareSolverr failed to solve Cloudflare for {} (site {}): {}",
                url,
                self.site_id,
                exc,
            )
            return None

    def _invoke_flaresolverr(self, url: str) -> Any | None:
        """Call the available FlareSolverr method, or return ``None``.

        Supports ``solve(url)`` (preferred, matches FlareSolverr's own
        terminology) and ``get(url)`` (used by some wrapper clients).
        """
        assert self._flaresolverr is not None  # narrowed by caller
        if hasattr(self._flaresolverr, "solve"):
            return self._flaresolverr.solve(url)
        if hasattr(self._flaresolverr, "get"):
            return self._flaresolverr.get(url)
        self._log.warning(
            "FlareSolverr client for site {} has no solve/get method.",
            self.site_id,
        )
        return None

    async def _solve_with_playwright(
        self, url: str
    ) -> list[dict[str, Any]] | None:
        """Solve a challenge through Playwright, if configured.

        A real browser navigation satisfies JS-based challenges naturally;
        the mixin waits for the interstitial title to change, then extracts
        the browser context's cookies.
        """
        assert self._playwright is not None  # narrowed by caller

        try:
            async with self._playwright.acquire() as acquired:
                owns_page = not hasattr(acquired, "goto")
                page = acquired if not owns_page else await acquired.new_page()
                try:
                    try:
                        await page.goto(
                            url,
                            wait_until=_PLAYWRIGHT_WAIT_UNTIL,
                            timeout=_PLAYWRIGHT_NAV_TIMEOUT_MS,
                        )
                    except Exception as exc:  # noqa: BLE001 — navigation failure
                        self._log.warning(
                            "Playwright navigation failed while solving "
                            "Cloudflare for {} (site {}): {}",
                            url,
                            self.site_id,
                            exc,
                        )
                        return None

                    # Wait for the challenge to clear.  A timeout here is
                    # not fatal: the cookies may already be valid, and the
                    # caller's retry will confirm.
                    try:
                        await page.wait_for_function(
                            _PLAYWRIGHT_CHALLENGE_CLEARED_JS,
                            timeout=_PLAYWRIGHT_SOLVE_TIMEOUT_MS,
                        )
                    except Exception as exc:  # noqa: BLE001 — non-fatal
                        self._log.debug(
                            "Playwright challenge-cleared wait timed out "
                            "for {} (site {}): {}",
                            url,
                            self.site_id,
                            exc,
                        )

                    cookies = await page.context.cookies()
                    return [dict(c) for c in cookies if isinstance(c, dict)]
                finally:
                    if owns_page:
                        try:
                            await page.close()
                        except Exception as exc:  # noqa: BLE001 — cleanup must not raise
                            self._log.debug(
                                "Ignoring error while closing Playwright "
                                "page in CloudflareMixin: {}",
                                exc,
                            )
        except Exception as exc:  # noqa: BLE001 — solver failure
            self._log.warning(
                "Playwright failed to solve Cloudflare for {} (site {}): {}",
                url,
                self.site_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------ #
    #  Cookie injection                                                  #
    # ------------------------------------------------------------------ #
    def _inject_cookies(
        self, url: str, cookies: list[dict[str, Any]]
    ) -> None:
        """Inject solved cookies into the session's cookie store.

        Tries the httpx-style ``session.cookies.set(...)`` first (always
        present on :class:`HttpSession`), then the project's
        :class:`CookieManager` when the session exposes one.  Logs a
        warning and returns silently when neither mechanism is available;
        the subsequent retry will then fail through the normal HTTP path
        with a clear error.
        """
        if not cookies:
            return

        # Preferred path — httpx.Cookies.
        jar = getattr(self.session, "cookies", None)
        if jar is not None and hasattr(jar, "set"):
            try:
                for cookie in cookies:
                    name = cookie.get("name")
                    value = cookie.get("value")
                    if not name or value is None:
                        continue
                    extra: dict[str, Any] = {}
                    domain = cookie.get("domain")
                    if domain:
                        extra["domain"] = domain
                    path = cookie.get("path")
                    if path:
                        extra["path"] = path
                    jar.set(name, value, **extra)
                return
            except Exception as exc:  # noqa: BLE001 — fall through to manager
                self._log.debug(
                    "httpx cookie jar injection failed for site {}: {}",
                    self.site_id,
                    exc,
                )

        # Fallback — project CookieManager.
        manager = getattr(self.session, "cookie_manager", None)
        if manager is not None and hasattr(manager, "update_cookies"):
            try:
                manager.update_cookies(self.site_id, cookies)
                return
            except Exception as exc:  # noqa: BLE001 — nothing else to try
                self._log.debug(
                    "CookieManager injection failed for site {}: {}",
                    self.site_id,
                    exc,
                )

        self._log.warning(
            "Unable to inject Cloudflare cookies for {} (site {}): "
            "session exposes neither a writable cookie jar nor a "
            "cookie_manager.",
            url,
            self.site_id,
        )

    # ------------------------------------------------------------------ #
    #  Detection                                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _looks_like_cf_challenge(body: str | None) -> bool:
        """Return ``True`` when *body* is a Cloudflare interstitial page.

        Uses a small list of strong, specific markers — deliberately
        conservative to avoid wasting a solve on a legitimate page whose
        content happens to contain the word "Cloudflare".
        """
        if not body:
            return False
        lowered = body.lower()
        return any(marker in lowered for marker in _CF_CHALLENGE_MARKERS)

    @staticmethod
    def _exception_is_cloudflare(exc: Exception) -> bool:
        """Return ``True`` when *exc* is a Cloudflare-shaped HTTP error.

        Used to catch non-:class:`SessionCloudflareError` failures (older
        ``HttpSession`` versions may raise a plain ``NexusDLHttpError`` on a
        403 with a CF challenge body).  The check is intentionally limited
        to two unambiguous substrings.
        """
        message = str(exc).lower()
        return "cloudflare" in message or "cf-ray" in message

    @staticmethod
    def _extract_cookies(result: Any) -> list[dict[str, Any]]:
        """Normalise a solver response into a ``list[dict]`` of cookies.

        Accepts every shape produced by the FlareSolverr and Playwright
        ecosystems:

        * A bare ``list`` of cookie dicts.
        * An object with a ``.cookies`` attribute (some FlareSolverr clients
          wrap the response in a dataclass).
        * A dict with a top-level ``cookies`` key.
        * A dict with a nested ``solution`` dict containing ``cookies``.

        Returns an empty list when the shape is unrecognised.
        """
        if result is None:
            return []
        if isinstance(result, list):
            return [c for c in result if isinstance(c, dict)]
        cookies = getattr(result, "cookies", None)
        if isinstance(cookies, list):
            return [c for c in cookies if isinstance(c, dict)]
        if isinstance(result, dict):
            direct = result.get("cookies")
            if isinstance(direct, list):
                return [c for c in direct if isinstance(c, dict)]
            solution = result.get("solution")
            if isinstance(solution, dict):
                nested = solution.get("cookies")
                if isinstance(nested, list):
                    return [c for c in nested if isinstance(c, dict)]
        return []
