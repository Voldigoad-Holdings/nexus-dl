"""Managed pool of Playwright browser contexts for JS-rendered sites.

Some source sites cannot be scraped over plain HTTP: they render the
manga listing and chapter pages client-side (Angular, React, Vue SPA),
protect image URLs behind JavaScript challenges, or apply per-page
Cloudflare validation that a headless browser is far more reliable at
solving than FlareSolverr. For those sites,
:class:`~nexusdl.parsers.mixins.js_rendered.JsRenderedMixin` needs a
Playwright browser context.

Opening a fresh browser per request is prohibitively expensive — a
Chromium launch costs 500–2000 ms and 50–200 MB of memory. This module
provides a **pool**: one shared browser, bounded concurrent contexts,
lazy startup, deterministic teardown, and the invariant that every
context is closed on every exit path.

Lifecycle
---------
::

    pool = PlaywrightPool(max_contexts=4)
    await pool.start()          # launches Chromium (once)
    try:
        async with pool.acquire() as context:
            page = await context.new_page()
            await page.goto(url)
            html = await page.content()
    finally:
        await pool.stop()       # closes browser, stops Playwright

Or, as an async context manager on the pool itself::

    async with PlaywrightPool(max_contexts=4) as pool:
        async with pool.acquire() as context:
            ...

Both forms are supported; the second is idiomatic for composition roots
that own the pool for the duration of a command.

Guaranteed context cleanup
--------------------------
The core invariant of the pool is that **every browser context is closed
on every exit path** — normal completion, caller exception, and
``asyncio.CancelledError``. The implementation achieves this with a
nested ``try/finally`` structure inside :meth:`PlaywrightPool.acquire`:

1. The **inner** ``try/finally`` closes the context. A failure to close
   is logged at ``WARNING`` and swallowed (the outer structure still
   releases the semaphore and returns the caller's exception untouched).
2. The **outer** ``try/finally`` releases the concurrency semaphore.
   This runs even if the inner close raised ``CancelledError``, so a
   cancelled caller never permanently consumes a pool slot.

**Ordering matters.** The context's ``close()`` runs **before** the
context is removed from the tracked dict. If a caller's ``stop()`` runs
concurrently, it can see the context still tracked and force-close it —
even if the in-flight ``close()`` is blocked, hung, or cancelled. If the
order were reversed (pop-then-close), a concurrent ``stop()`` would
observe an untracked context that is not yet closed and leave it
leaked until the whole browser is closed.

The pool additionally tracks active contexts in a dict so that
:meth:`stop` can force-close any contexts a caller forgot to release —
this is defense-in-depth, not a normal path: the ``acquire()`` contract
already prevents leaks.

Lifecycle serialisation
-----------------------
:meth:`start` and :meth:`stop` are serialised by an internal
``_lifecycle_lock``. Without it, a concurrent ``start()`` running while
``stop()`` is midway through ``browser.close()`` could launch a fresh
browser only to have ``stop()`` finish by closing **that** browser and
nulling the reference, leaving the pool in an inconsistent state. The
lifecycle lock makes ``start`` and ``stop`` mutually exclusive; the
public state lock (``_lock``) still protects the state observations
that ``stats()`` and :meth:`acquire` make.

Startup checks (Chromium install)
---------------------------------
:meth:`start` verifies two things before declaring the pool ready:

1. **Playwright is importable.** The import is lazy — the module itself
   imports cleanly even when ``playwright`` is not installed, so a
   deployment that only uses HTTP parsers never pays the ~30 MB of
   Playwright dependencies at install time. When ``start()`` runs
   without Playwright, it raises
   :class:`~nexusdl.core.exceptions.NexusDLPlaywrightError` with an
   actionable message (the ``pip install`` and
   ``playwright install chromium`` commands).
2. **Chromium is installed.** The launch call is wrapped; a failure
   raises the same exception with the ``playwright install chromium``
   hint. The check is implicit (attempt the launch) rather than running
   ``playwright install --check`` as a subprocess — the launch failure
   is authoritative, fast, and produces the same actionable message.

Either failure leaves the pool in a clean stopped state — no leaked
``Playwright`` instance, no half-launched browser.

Configuration
-------------
Every knob is an explicit constructor argument. There is no coupling to
:class:`~nexusdl.core.config.Settings` here; the composition root reads
the settings and passes the values it wants. This mirrors the pattern
used by :class:`~nexusdl.core.session.rate_limiter.RateLimiter` and
:class:`~nexusdl.core.session.proxy_manager.ProxyManager`.

The defaults are tuned for scraping, not for interactive browsing:

* ``headless=True`` — a headless Chromium is 2× faster to start and
  far more resource-efficient. Set to ``False`` only for debugging.
* ``browser_args=("--disable-dev-shm-usage",)`` — the default ``/dev/shm``
  size in Docker containers is 64 MB, which Chromium exhausts on a
  single manga page. This flag redirects Chromium's shared memory to
  ``/tmp``; the effect is negligible on bare metal and prevents crashes
  in every containerised deployment.
* ``viewport=(1920, 1080)`` — matches the most common desktop resolution,
  which reduces the chance a site serves a mobile-specific layout.

Timeouts
--------
Every operation the caller performs on the returned context is the
caller's responsibility — the pool does not install per-operation
timeouts. It **does** bound two operations of its own:

* :meth:`acquire` accepts an optional ``timeout`` for waiting on a free
  slot. Timed out acquisitions raise
  :class:`~nexusdl.core.exceptions.SessionTimeoutError`.
* :meth:`start` has an implicit browser launch bound set by Playwright's
  own ``launch()`` timeout (30 s); a browser that cannot launch in that
  window is genuinely broken.

Diagnostic accuracy
-------------------
The counters exposed by :meth:`stats` and the ``active_contexts`` count
embedded in timeout error messages are read under the state lock.
A slightly stale ``active_contexts`` (one acquire racing another) is
acceptable — it is diagnostic information, not a correctness guarantee
— but it is **never** corrupted. The two counters
(``total_acquires``, ``total_timeouts``) are incremented inside the
state lock for the same reason: a counter read from :meth:`stats`
while an acquire is completing must be either the pre- or post-value,
not a half-updated one.

Dependency graph (no cycles)::

    core.exceptions      ─┐
    loguru                ├──► core.session.playwright_pool
    asyncio               │
    (playwright imported lazily at start())
                          │

No ``nexusdl.core.config``, ``nexusdl.parsers``, or
``nexusdl.interfaces`` import — this module is a leaf of the session
package and could be lifted out with only the ``exceptions`` import
replaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from loguru import logger

from nexusdl.core.exceptions import (
    NexusDLPlaywrightError,
    SessionTimeoutError,
)

if TYPE_CHECKING:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Playwright,
    )


__all__ = [
    "DEFAULT_MAX_CONTEXTS",
    "DEFAULT_TIMEOUT_MS",
    "PlaywrightPool",
    "PlaywrightPoolStats",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default ceiling on concurrent browser contexts. Four is a balance between
#: throughput (a chapter with 60 pages benefits from parallelism) and
#: memory (each Chromium context consumes 30–60 MB). A site that only
#: needs a couple of page renders never exceeds the limit; a site with
#: heavy per-chapter scraping does not swamp the host.
DEFAULT_MAX_CONTEXTS: Final[int] = 4

#: Default timeout, in milliseconds, for waiting on a free slot when the
#: pool is saturated. 30 s is generous — a slot frees up after every
#: context closes, which typically takes seconds, not minutes. The value
#: matches Playwright's own default navigation timeout, so operators have
#: a single number to reason about.
DEFAULT_TIMEOUT_MS: Final[float] = 30_000.0

#: Chromium launch arguments applied to every context. ``--disable-dev-shm-usage``
#: is essential in Docker (default ``/dev/shm`` is 64 MB, exhausted by a
#: single JS-heavy page). ``--disable-gpu`` prevents Chromium from trying
#: to initialise a GPU stack that does not exist on servers.
_DEFAULT_BROWSER_ARGS: Final[tuple[str, ...]] = (
    "--disable-dev-shm-usage",
    "--disable-gpu",
)

_log = logger.bind(component="session.playwright")


# --------------------------------------------------------------------------- #
# Stats                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlaywrightPoolStats:
    """Immutable snapshot of a :class:`PlaywrightPool`'s state.

    Returned by :meth:`PlaywrightPool.stats`. Useful for the diagnostic
    endpoint and for tests that assert concurrency behaviour over a
    sequence of acquisitions.

    Attributes:
        is_started: ``True`` iff :meth:`PlaywrightPool.start` has
            completed and :meth:`PlaywrightPool.stop` has not run since.
        max_contexts: Configured concurrency ceiling.
        active_contexts: Number of contexts currently held by callers.
            Never exceeds :attr:`max_contexts`.
        total_contexts_created: Cumulative count of contexts created
            since the pool started. Reset only by :meth:`stop` followed
            by :meth:`start`.
        total_acquires: Cumulative count of successful
            :meth:`PlaywrightPool.acquire` calls (i.e. acquisitions that
            obtained a slot). Excludes timeouts.
        total_timeouts: Cumulative count of :meth:`PlaywrightPool.acquire`
            calls that timed out waiting for a slot.
    """

    is_started: bool
    max_contexts: int
    active_contexts: int
    total_contexts_created: int
    total_acquires: int
    total_timeouts: int


# --------------------------------------------------------------------------- #
# PlaywrightPool                                                              #
# --------------------------------------------------------------------------- #


class PlaywrightPool:
    """Managed pool of Playwright browser contexts.

    One instance per composition root, started once at application startup
    and stopped once at shutdown. Safe to share across coroutines.

    See the module docstring for the lifecycle, the cleanup guarantee,
    and the startup checks.

    Example:
        >>> pool = PlaywrightPool(max_contexts=2)
        >>> await pool.start()
        >>> try:
        ...     async with pool.acquire() as ctx:
        ...         page = await ctx.new_page()
        ...         await page.goto("https://example.org")
        ... finally:
        ...     await pool.stop()

    Args:
        max_contexts: Maximum number of browser contexts the pool will
            hand out concurrently. Must be ``>= 1``. Callers that exceed
            this limit wait on an internal semaphore.
        headless: Whether Chromium runs without a visible window. The
            default (``True``) is correct for every production deployment
            — set ``False`` only for local debugging of a specific
            parser. Ignored if a full ``browser_args`` argument is
            provided and overrides ``--headless``.
        timeout_ms: Default timeout, in milliseconds, for
            :meth:`acquire` when waiting on a free slot. Overridable per
            call.
        user_agent: A fixed ``User-Agent`` string applied to every
            context. ``None`` (the default) uses Playwright's built-in
            Chromium UA, which is correct for most sites. Set explicitly
            when a site rejects the default Playwright UA (some
            Cloudflare deployments fingerprint it) or when the site
            expects a specific mobile UA.
        viewport: ``(width, height)`` in pixels for the browser context.
            Defaults to ``(1920, 1080)``. Reduced desktop-vs-mobile
            misrouting and keeps memory per context predictable.
        browser_args: Extra Chromium command-line arguments. When empty
            (the default), :data:`_DEFAULT_BROWSER_ARGS` is used.
            Providing an explicit tuple **replaces** the defaults, so a
            caller that overrides them takes responsibility for
            re-including the container-friendly flags it needs.

    Raises:
        ValueError: If ``max_contexts < 1``, ``timeout_ms < 0``, or the
            viewport dimensions are non-positive.
    """

    __slots__ = (
        "_active_contexts",
        "_browser",
        "_browser_args",
        "_headless",
        "_lifecycle_lock",
        "_lock",
        "_max_contexts",
        "_playwright",
        "_semaphore",
        "_started",
        "_timeout_ms",
        "_total_acquires",
        "_total_contexts_created",
        "_total_timeouts",
        "_user_agent",
        "_viewport",
    )

    def __init__(
        self,
        *,
        max_contexts: int = DEFAULT_MAX_CONTEXTS,
        headless: bool = True,
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
        user_agent: str | None = None,
        viewport: tuple[int, int] = (1920, 1080),
        browser_args: tuple[str, ...] = (),
    ) -> None:
        if max_contexts < 1:
            raise ValueError(
                f"max_contexts must be >= 1, got {max_contexts}"
            )
        if timeout_ms < 0:
            raise ValueError(
                f"timeout_ms must be >= 0, got {timeout_ms}"
            )
        if viewport[0] < 1 or viewport[1] < 1:
            raise ValueError(
                f"viewport dimensions must be positive, got {viewport!r}"
            )

        self._max_contexts: int = max_contexts
        self._headless: bool = headless
        self._timeout_ms: float = float(timeout_ms)
        self._user_agent: str | None = user_agent
        self._viewport: tuple[int, int] = viewport
        self._browser_args: tuple[str, ...] = (
            browser_args if browser_args else _DEFAULT_BROWSER_ARGS
        )

        # Playwright resources. Populated by ``start()``, cleared by
        # ``stop()``. ``None`` while stopped.
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

        #: Tracks every context currently handed out to a caller, keyed by
        #: ``id(context)``. Only used by :meth:`stop` to force-close
        #: contexts a caller forgot to release — the normal path is
        #: :meth:`acquire`'s ``finally``. Every read of this dict from a
        #: public method must be under :attr:`_lock`; the only exception
        #: is :meth:`acquire`'s own finally block, which mutates it and
        #: has already been holding the lock.
        self._active_contexts: dict[int, BrowserContext] = {}

        self._semaphore = asyncio.Semaphore(max_contexts)
        self._lock = asyncio.Lock()
        # Serialises ``start`` and ``stop`` so a concurrent start cannot
        # race a still-in-progress stop and overwrite the browser
        # reference the stop is about to null. Held for the full
        # duration of each lifecycle operation (which may include
        # ``await`` on Playwright I/O); the state lock protects only
        # short observations.
        self._lifecycle_lock = asyncio.Lock()
        self._started: bool = False

        # Cumulative counters, exposed by ``stats()``. Incremented under
        # ``_lock`` (see :meth:`stats` docstring and module docstring's
        # "Diagnostic accuracy" section).
        self._total_contexts_created: int = 0
        self._total_acquires: int = 0
        self._total_timeouts: int = 0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Launch Playwright and Chromium. Idempotent.

        The first call imports Playwright lazily, starts its event loop
        integration, and launches a headless Chromium. Subsequent calls
        return immediately without launching anything.

        Serialised against :meth:`stop` by an internal lifecycle lock:
        a ``start()`` concurrent with a ``stop()`` will run **after**
        the stop completes, not interleaved with it. This is what makes
        ``await pool.stop(); await pool.start()`` safe even when an
        ``acquire()`` is still unwinding in another task.

        Raises:
            NexusDLPlaywrightError: If Playwright is not installed, if
                Chromium is not installed (the message names the
                ``playwright install`` command), or if launch fails for
                any other reason. The pool is left in a clean stopped
                state — no leaked Playwright instance, no half-launched
                browser.
        """
        async with self._lifecycle_lock:
            async with self._lock:
                if self._started:
                    return

            try:
                from playwright.async_api import (
                    async_playwright,  # noqa: PLC0415 — intentional lazy import
                )
            except ImportError as exc:
                raise NexusDLPlaywrightError(
                    "playwright is not installed; install the "
                    "'nexusdl[browser]' extra or run "
                    "'pip install playwright' followed by "
                    "'playwright install chromium'"
                ) from exc

            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.launch(
                    headless=self._headless,
                    args=list(self._browser_args),
                )
            except Exception as exc:
                # The launch failure is authoritative: missing Chromium
                # binary, missing system libraries, no X server when
                # ``headless=False``, sandbox permission problems — all
                # surface here. We translate them into a single,
                # actionable error message so the operator does not have
                # to read Playwright's verbose output.
                try:
                    await playwright.stop()
                except Exception as cleanup_exc:  # noqa: BLE001
                    _log.warning(
                        "Failed to stop Playwright after launch failure: "
                        "{}: {}",
                        type(cleanup_exc).__name__,
                        cleanup_exc,
                    )
                raise NexusDLPlaywrightError(
                    f"cannot launch Chromium: {type(exc).__name__}: {exc}. "
                    f"If Chromium is not installed, run "
                    f"'playwright install chromium'"
                ) from exc

            async with self._lock:
                self._playwright = playwright
                self._browser = browser
                self._started = True
            _log.info(
                "Playwright pool started (headless={}, max_contexts={})",
                self._headless,
                self._max_contexts,
            )

    async def stop(self) -> None:
        """Close all contexts, close the browser, stop Playwright.

        Idempotent. Contexts that are still tracked (a caller forgot to
        release one) are force-closed before the browser itself is
        closed. Any exception during cleanup is logged at ``WARNING`` and
        swallowed — the pool is shutting down and raising cannot help
        anything.

        Serialised against :meth:`start` by the lifecycle lock, so a
        ``start()`` that runs concurrently will wait for the stop to
        complete before launching a new browser. The ``_started`` flag
        is flipped to ``False`` **after** cleanup, not before: a caller
        that inspects ``is_started`` mid-stop sees ``True`` (the pool is
        still tearing down), which matches what a "the pool is being
        stopped" observation should return.

        After ``stop()`` returns, the pool is in a stopped state and may
        be restarted with a fresh :meth:`start` call. The cumulative
        counters are preserved across restarts; only
        :attr:`PlaywrightPoolStats.total_contexts_created` continues to
        accumulate.
        """
        async with self._lifecycle_lock:
            async with self._lock:
                if not self._started:
                    return
                # Snapshot the resources so cleanup runs without holding
                # the state lock (cleanup awaits, and the lock must be
                # released to avoid blocking stats() for seconds).
                active = list(self._active_contexts.values())
                browser = self._browser
                playwright = self._playwright
                self._active_contexts.clear()

            # Close every leftover context.
            for context in active:
                try:
                    await context.close()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "Failed to close leftover context: {}: {}",
                        type(exc).__name__,
                        exc,
                    )

            # Close the browser.
            if browser is not None:
                try:
                    await browser.close()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "Failed to close Playwright browser: {}: {}",
                        type(exc).__name__,
                        exc,
                    )

            # Stop the Playwright driver.
            if playwright is not None:
                try:
                    await playwright.stop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "Failed to stop Playwright: {}: {}",
                        type(exc).__name__,
                        exc,
                    )

            # Only now clear the state — cleanup is done, resources are
            # closed, and a fresh ``start()`` (which cannot run until
            # the lifecycle lock is released) will find a clean slate.
            async with self._lock:
                self._browser = None
                self._playwright = None
                self._started = False
            _log.info("Playwright pool stopped")

    # ------------------------------------------------------------------ #
    # Acquisition                                                        #
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def acquire(
        self, *, timeout: float | None = None
    ) -> AsyncIterator[BrowserContext]:
        """Acquire a fresh browser context for the duration of the block.

        Waits on the internal semaphore if ``max_contexts`` is already
        reached. The returned context is **closed automatically** when
        the block exits — normal completion, exception, or cancellation.

        Example::

            async with pool.acquire() as context:
                page = await context.new_page()
                await page.goto(url)

        Args:
            timeout: Maximum wait, in seconds, for a free slot. ``None``
                (the default) uses :attr:`_timeout_ms` divided by 1000.
                A value of ``0`` fails immediately if no slot is
                available.

        Yields:
            A freshly created :class:`~playwright.async_api.BrowserContext`.
            Never ``None`` — a caller that needs "wait until available,
            do not time out" passes a large ``timeout``.

        Raises:
            NexusDLPlaywrightError: If the pool has not been started, if
                the underlying browser has disconnected unexpectedly, or
                if context creation fails for any reason. The error
                message names the specific condition.
            SessionTimeoutError: If ``timeout`` expires before a slot is
                available. The message includes the configured limit and
                the number of active contexts, so a caller that logs the
                exception immediately sees why it waited.
        """
        # Snapshot state under the lock so we do not race a concurrent
        # ``stop()``. The ``browser`` and ``is_started`` checks below
        # are the same checks the original code performed — the only
        # difference is that they are read as a coherent pair.
        async with self._lock:
            if not self._started:
                raise NexusDLPlaywrightError(
                    "PlaywrightPool.acquire() called before start(); "
                    "the composition root must await pool.start() during "
                    "startup"
                )
            browser = self._browser
            if browser is None:
                raise NexusDLPlaywrightError(
                    "PlaywrightPool internal error: browser is None after "
                    "start(); the pool is in an inconsistent state"
                )
            if not browser.is_connected():
                raise NexusDLPlaywrightError(
                    "Playwright browser has disconnected; call "
                    "pool.stop() then pool.start() to recover"
                )
            active_snapshot = len(self._active_contexts)

        effective_timeout = (
            timeout if timeout is not None else self._timeout_ms / 1000.0
        )

        # Wait for a semaphore slot. ``asyncio.wait_for`` is used instead
        # of ``asyncio.timeout`` so the timeout is unambiguously scoped
        # to the acquire, not to the caller's block.
        #
        # ``asyncio.TimeoutError`` is the canonical exception name across
        # every supported Python version — ``except TimeoutError`` works
        # on 3.11+ (they are the same class) but is clearer when spelled
        # explicitly, and it removes any doubt about which builtin is
        # being caught.
        if effective_timeout <= 0:
            if self._semaphore.locked():
                async with self._lock:
                    self._total_timeouts += 1
                    active_snapshot = len(self._active_contexts)
                raise SessionTimeoutError(
                    f"Playwright pool acquire timed out immediately "
                    f"(active={active_snapshot}, "
                    f"max={self._max_contexts})"
                )
            await self._semaphore.acquire()
        else:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), timeout=effective_timeout
                )
            except asyncio.TimeoutError as exc:
                async with self._lock:
                    self._total_timeouts += 1
                    active_snapshot = len(self._active_contexts)
                raise SessionTimeoutError(
                    f"Playwright pool acquire timed out after "
                    f"{effective_timeout:.1f}s "
                    f"(active={active_snapshot}, "
                    f"max={self._max_contexts})"
                ) from exc

        context: BrowserContext | None = None
        try:
            context = await browser.new_context(
                user_agent=self._user_agent,
                viewport={
                    "width": self._viewport[0],
                    "height": self._viewport[1],
                },
            )
            context_id = id(context)
            async with self._lock:
                self._active_contexts[context_id] = context
                self._total_contexts_created += 1
                self._total_acquires += 1

            try:
                yield context
            finally:
                # Close the context **before** removing it from the
                # tracked dict. This ordering is what makes a concurrent
                # ``stop()`` safe: while the close is in flight (or
                # blocked, or cancelled), the context is still visible
                # to ``stop()``'s force-close path. If we popped first,
                # a ``stop()`` that ran between the pop and the close
                # would see an untracked-but-not-yet-closed context and
                # would leave it leaked until the whole browser closed.
                try:
                    await context.close()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "Failed to close Playwright context: {}: {}",
                        type(exc).__name__,
                        exc,
                    )
                finally:
                    async with self._lock:
                        self._active_contexts.pop(context_id, None)
        finally:
            # Always release the semaphore — even if ``new_context`` or
            # ``context.close`` raised, a slot must return to the pool.
            self._semaphore.release()

    # ------------------------------------------------------------------ #
    # Context manager protocol                                           #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> PlaywrightPool:
        """Start the pool and return it. Enables ``async with`` usage."""
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop the pool. Never suppresses exceptions from the body."""
        await self.stop()

    # ------------------------------------------------------------------ #
    # Diagnostics                                                        #
    # ------------------------------------------------------------------ #

    async def stats(self) -> PlaywrightPoolStats:
        """Return an immutable snapshot of the pool's state.

        Returns:
            A :class:`PlaywrightPoolStats` reflecting the current
            state. Safe to call at any time, from any coroutine — the
            ``_active_contexts`` dict is read under the state lock so
            the counts are coherent with the rest of the snapshot.
        """
        async with self._lock:
            return PlaywrightPoolStats(
                is_started=self._started,
                max_contexts=self._max_contexts,
                active_contexts=len(self._active_contexts),
                total_contexts_created=self._total_contexts_created,
                total_acquires=self._total_acquires,
                total_timeouts=self._total_timeouts,
            )

    @property
    def is_started(self) -> bool:
        """``True`` iff :meth:`start` has completed and :meth:`stop` has not run."""
        return self._started

    @property
    def max_contexts(self) -> int:
        """The configured concurrency ceiling."""
        return self._max_contexts

    def __repr__(self) -> str:
        """Return a compact debug representation.

        ``active`` is read without a lock for two reasons: this is
        called by interactive debugging tools (``repr`` in a REPL),
        where awaiting a lock is not an option, and the value is
        diagnostic only — a stale count is acceptable, a corrupted one
        is not, and ``len()`` on a dict is atomic in CPython. See the
        module docstring's "Diagnostic accuracy" section.
        """
        return (
            f"PlaywrightPool(started={self._started}, "
            f"max_contexts={self._max_contexts}, "
            f"active={len(self._active_contexts)})"
        )
