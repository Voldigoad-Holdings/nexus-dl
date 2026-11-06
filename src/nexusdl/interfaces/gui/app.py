"""GUI composition root for NexusDL.

This is the **only** module allowed to know about both the concrete
application services (:class:`EventBus`, :class:`DownloadManager`,
:class:`SiteRegistry`, :class:`LibraryDatabase`, :class:`ImageCache`,
:class:`StateStore`) and the concrete GUI toolkit
(:mod:`customtkinter`). Every other GUI module is either a component, a
view, or a pure utility.

Architecture:

    * :class:`AsyncBridge` runs a dedicated :mod:`asyncio` event loop on a
      background thread, and marshals Tk-facing callbacks back to the main
      thread via a thread-safe queue drained by ``after(30ms, ...)``. This
      is the **only** safe pattern for calling CTk from another thread.
    * :class:`AppServices` is a structural Protocol — the shell never
      references a concrete service class. Tests substitute a fake.
    * :class:`AppHooks` carries pure-UX decisions (open a manga, show a
      download dialog, restart the process). ``app.py`` never makes a UX
      decision on its own; it only forwards.
    * :class:`GUILabels` bundles every translated string used by the shell
      and the views. Adding a view = adding one field here.
    * Views are **not** lazy: every :class:`ViewSpec` is instantiated at
      boot so the shell always has a valid target for events. Startup cost
      is a few tens of milliseconds.
    * Sites are loaded asynchronously right after boot; views are rebuilt
      with the real site list once the registry answers.

Non-goals:

    * ``app.py`` does not implement search pagination, library queries, or
      download orchestration — those live behind :class:`AppServices`.
    * ``app.py`` does not build menus, dialogs, or sub-windows — those are
      host-provided :class:`AppHooks`.
"""
from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine, Iterable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import customtkinter as ctk
from loguru import logger as _logger

from nexusdl.core.models.enums import DownloadStatus
from nexusdl.core.models.site import SiteConfig

from .components import MangaCardData, MangaCardLabels, ProgressLabels
from .theme import apply_appearance_mode, set_theme, theme_provider
from .views import (
    ApplyResult,
    DownloadLabels,
    DownloadTaskData,
    DownloadView,
    LibraryCardData,
    LibraryLabels,
    LibraryPage,
    LibraryQuery,
    LibraryView,
    MainView,
    MainViewLabels,
    SearchLabels,
    SearchPage,
    SearchRequest,
    SearchView,
    SettingsLabels,
    SettingsSchema,
    SettingsView,
    ViewSpec,
)

_log = _logger.bind(component="gui.app")


# ===========================================================================
# View identifiers (single source of truth)
# ===========================================================================

VIEW_SEARCH = "search"
VIEW_LIBRARY = "library"
VIEW_DOWNLOADS = "downloads"
VIEW_SETTINGS = "settings"


# ===========================================================================
# Labels bundle
# ===========================================================================

@dataclass(frozen=True, slots=True)
class GUILabels:
    """Every translated string the shell and its views need.

    The host builds this once from its i18n backend and hands it over as
    :attr:`AppServices.labels`. Adding a view = adding one field here.
    """

    window_title: str = "NexusDL"
    # Nav labels (shown in the MainView sidebar / topbar).
    nav_search: str = "Search"
    nav_library: str = "Library"
    nav_downloads: str = "Downloads"
    nav_settings: str = "Settings"
    # Per-view labels (forwarded to each view's ``labels`` kwarg).
    main_view: MainViewLabels = field(default_factory=MainViewLabels)
    search: SearchLabels = field(default_factory=SearchLabels)
    library: LibraryLabels = field(default_factory=LibraryLabels)
    download: DownloadLabels = field(default_factory=DownloadLabels)
    settings: SettingsLabels = field(default_factory=SettingsLabels)
    # Component labels (forwarded to MangaCard / ProgressWidget).
    manga_card: MangaCardLabels = field(default_factory=MangaCardLabels)
    progress: ProgressLabels = field(default_factory=ProgressLabels)
    # Shell-level messages (status bar, errors).
    error_async: str = "Background task failed"
    error_startup: str = "Failed to start"
    error_shutdown: str = "Failed to shut down cleanly"
    error_unknown: str = "Unexpected error"


# ===========================================================================
# Service protocols
# ===========================================================================
#
# Every concrete service is behind a Protocol so ``app.py`` never depends on
# the actual implementation class. Tests can substitute fakes trivially.

class SettingsLike(Protocol):
    """The subset of :class:`Settings` used by the GUI shell."""

    @property
    def gui(self) -> Any:
        """Must expose ``theme_id``, ``appearance_mode``."""

    @property
    def content(self) -> Any:
        """Must expose ``allow_adult``."""


class EventBusLike(Protocol):
    def subscribe(
        self, event_type: str, handler: Callable[[Any], Any]
    ) -> Any: ...


class SiteRegistryLike(Protocol):
    async def list_sites(self, *, include_adult: bool = False) -> list[SiteConfig]:
        ...


class DownloadManagerLike(Protocol):
    async def pause(self, task_id: str) -> None: ...
    async def resume(self, task_id: str) -> None: ...
    async def cancel(self, task_id: str) -> None: ...
    async def retry(self, task_id: str) -> None: ...
    async def forget(self, task_id: str) -> None: ...
    async def pause_all(self) -> None: ...
    async def resume_all(self) -> None: ...
    async def cancel_all(self) -> None: ...


class LibraryLike(Protocol):
    async def query(self, query: LibraryQuery) -> LibraryPage: ...
    async def delete(self, manga_ids: Iterable[str]) -> None: ...


class ImageCacheLike(Protocol):
    async def get(self, url: str) -> Any | None:
        """Return a PIL ``Image`` or ``None``."""


class StateStoreLike(Protocol):
    """Persistent key-value store for UI state (window geometry, nav…).

    Distinct from :class:`Settings` — this is *ephemeral UI state*, not
    business configuration. Keys are opaque strings; values are JSON-ish
    dicts.
    """

    def get(self, key: str, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
        ...

    def set(self, key: str, value: Mapping[str, Any]) -> None: ...

    def flush(self) -> None:
        """Persist any pending writes synchronously."""


class AppServices(Protocol):
    """Everything the GUI shell needs from the application.

    Implementations must be constructed and warmed-up *before* being
    passed to :class:`NexusDLApp`. The shell calls
    ``on_startup_async`` / ``on_shutdown_async`` if present.
    """

    settings: SettingsLike
    event_bus: EventBusLike
    registry: SiteRegistryLike
    downloader: DownloadManagerLike
    library: LibraryLike
    image_cache: ImageCacheLike
    state_store: StateStoreLike
    labels: GUILabels

    async def search(self, request: SearchRequest) -> SearchPage:
        """Execute a search and return a paginated result."""

    def build_settings_schema(self) -> SettingsSchema:
        """Build the schema displayed by :class:`SettingsView`."""

    def snapshot_settings(self) -> Mapping[str, Any]:
        """Return a snapshot of the current settings values (dotted keys).

        Secrets must be provided as ``True``/``False`` (is-set / unset).
        """

    def apply_settings(self, changes: Mapping[str, Any]) -> ApplyResult:
        """Validate and persist ``changes``, return the outcome.

        The GUI calls this synchronously from the Tk thread. Hosts that
        need async work should schedule it on their own loop and return a
        result with ``requires_restart=True`` (settings applied on next
        boot) rather than blocking the GUI.
        """


# ===========================================================================
# Host hooks (pure UX decisions)
# ===========================================================================

@dataclass(frozen=True, slots=True)
class AppHooks:
    """Pure-UX callbacks injected by the host.

    Every hook is optional. Missing hooks cause the corresponding
    affordance to be hidden (not to crash).
    """

    open_manga: Callable[[MangaCardData], None] | None = None
    continue_reading: Callable[[LibraryCardData], None] | None = None
    download_manga: Callable[[MangaCardData], None] | None = None
    right_click_manga: Callable[[LibraryCardData, object], None] | None = None
    restart_app: Callable[[], None] | None = None
    open_path: Callable[[Path], None] | None = None


# ===========================================================================
# Async bridge
# ===========================================================================

class AsyncBridge:
    """Owns a dedicated :mod:`asyncio` loop on a background thread.

    Marshals Tk-facing callbacks back to the main thread through a
    thread-safe queue drained by ``after(interval_ms, ...)``. This is the
    only portable, thread-safe pattern for calling Tk from another thread.
    """

    def __init__(self, *, name: str = "nexusdl-gui-async") -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._name = name
        self._stopped = False
        self._tk_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._pump_after_id: str | None = None
        self._tk_root: Any = None
        self._pump_interval_ms: int = 30

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("AsyncBridge is not running")
        return self._loop

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.call_soon(ready.set)
            try:
                loop.run_forever()
            finally:
                try:
                    loop.run_until_complete(self._drain_pending())
                except Exception:
                    _log.debug("AsyncBridge: drain during shutdown raised")
                loop.close()

        self._thread = threading.Thread(
            target=_runner, name=self._name, daemon=True
        )
        self._thread.start()
        ready.wait(timeout=5.0)

    def attach_tk_root(
        self, tk_root: Any, *, pump_interval_ms: int = 30
    ) -> None:
        """Start pumping marshalled callbacks on the Tk main thread."""
        self._tk_root = tk_root
        self._pump_interval_ms = max(10, pump_interval_ms)
        self._schedule_pump()

    def stop(self, *, timeout: float = 3.0) -> None:
        """Stop the loop, join the thread (best effort)."""
        self._stopped = True
        if self._pump_after_id is not None and self._tk_root is not None:
            try:
                self._tk_root.after_cancel(self._pump_after_id)
            except Exception:
                pass
            self._pump_after_id = None

        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return

        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass

        thread.join(timeout=timeout)
        if thread.is_alive():
            _log.warning(
                "AsyncBridge thread did not stop within {:.1f}s", timeout
            )
        self._thread = None
        self._loop = None

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def post(self, callback: Callable[..., None], *args: Any) -> None:
        """Schedule ``callback(*args)`` on the Tk main thread."""
        if self._stopped:
            return
        if args:
            self._tk_queue.put(lambda: callback(*args))
        else:
            self._tk_queue.put(callback)

    def run(self, coro: Coroutine[Any, Any, Any]) -> ConcurrentFuture:
        """Schedule ``coro`` on the async loop; return a concurrent Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run_and_forget(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Schedule ``coro`` and forget about it. Exceptions are logged."""
        def _done(fut: ConcurrentFuture) -> None:
            try:
                fut.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                _log.exception("AsyncBridge: background task failed")
                if on_error is not None:
                    try:
                        on_error(exc)
                    except Exception:
                        _log.exception("AsyncBridge: on_error callback raised")

        fut = self.run(coro)
        fut.add_done_callback(_done)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _schedule_pump(self) -> None:
        if self._tk_root is None or self._stopped:
            return
        try:
            self._pump_after_id = self._tk_root.after(
                self._pump_interval_ms, self._pump
            )
        except Exception:
            self._pump_after_id = None

    def _pump(self) -> None:
        self._pump_after_id = None
        if self._tk_root is None or self._stopped:
            return
        while True:
            try:
                cb = self._tk_queue.get_nowait()
            except queue.Empty:
                break
            try:
                cb()
            except Exception:
                _log.exception("AsyncBridge: marshalled callback raised")
        self._schedule_pump()

    async def _drain_pending(self) -> None:
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        for t in pending:
            t.cancel()
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "AsyncBridge: {} task(s) ignored cancellation during drain",
                len(pending),
            )
        except Exception:
            _log.debug("AsyncBridge: drain raised")


# ===========================================================================
# NexusDLApp
# ===========================================================================

class NexusDLApp(ctk.CTk):
    """The NexusDL desktop application shell.

    This class owns the Tk root, the async bridge, the shell, and every
    view factory. It does not own any service implementation.
    """

    def __init__(
        self,
        services: AppServices,
        *,
        hooks: AppHooks | None = None,
        icon_path: Path | None = None,
        initial_size: tuple[int, int] = (1280, 800),
        min_size: tuple[int, int] = (960, 600),
    ) -> None:
        super().__init__()

        self._services = services
        self._hooks = hooks or AppHooks()
        self._labels = services.labels

        self._bridge = AsyncBridge()
        self._closing = False
        self._sites_cache: list[SiteConfig] = []
        self._shutdown_future: ConcurrentFuture | None = None
        self._shutdown_deadline: float = 0.0

        # --- Window setup ---
        self.title(self._labels.window_title)
        self.minsize(*min_size)
        self.geometry(self._restore_geometry(initial_size))
        if icon_path is not None:
            self._apply_icon(Path(icon_path))

        # --- Theme / appearance (before any widget) ---
        self._apply_theme_from_settings()

        # --- Shell ---
        self._main_view = self._build_main_view()
        self._main_view.pack(fill="both", expand=True)

        # --- Lifecycle wiring ---
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._bridge.start()
        self._bridge.attach_tk_root(self, pump_interval_ms=30)

        self._subscribe_events()
        self._schedule_startup()

    # ------------------------------------------------------------------
    # Public — properties
    # ------------------------------------------------------------------

    @property
    def main_view(self) -> MainView:
        """The shell (exposed so hosts can push status / change view)."""
        return self._main_view

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------

    def _apply_theme_from_settings(self) -> None:
        settings = self._services.settings

        mode: Literal["light", "dark"] = "dark"
        try:
            raw = str(settings.gui.appearance_mode).lower()
        except Exception:
            raw = "dark"
        if raw == "light":
            mode = "light"
        elif raw == "dark":
            mode = "dark"
        apply_appearance_mode(mode)

        try:
            theme_id = str(settings.gui.theme_id)
        except Exception:
            theme_id = "nexus"
        try:
            set_theme(theme_id)
        except KeyError:
            _log.warning("Unknown theme {!r}, falling back to default", theme_id)
            set_theme("nexus")

    # ------------------------------------------------------------------
    # Window setup
    # ------------------------------------------------------------------

    def _restore_geometry(self, default: tuple[int, int]) -> str:
        try:
            state = self._services.state_store.get("app", {})
        except Exception:
            state = {}
        geom = state.get("geometry")
        if isinstance(geom, str) and "x" in geom:
            return geom
        return f"{default[0]}x{default[1]}"

    def _apply_icon(self, icon_path: Path) -> None:
        if not icon_path.exists():
            return
        try:
            self.iconbitmap(str(icon_path))
        except Exception:
            _log.debug("iconbitmap failed on this platform")

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _schedule_startup(self) -> None:
        async def _startup() -> None:
            startup = getattr(self._services, "on_startup_async", None)
            if startup is not None and callable(startup):
                try:
                    await startup()
                except Exception:
                    _log.exception("services.on_startup_async raised")
                    self._bridge.post(
                        lambda: self._status(
                            f"{self._labels.error_startup}", kind="error"
                        )
                    )

        self._bridge.run_and_forget(_startup())
        self._bridge.run_and_forget(self._load_sites_async())

    async def _load_sites_async(self) -> None:
        try:
            include_adult = bool(
                getattr(self._services.settings.content, "allow_adult", False)
            )
        except Exception:
            include_adult = False

        try:
            sites = await self._services.registry.list_sites(
                include_adult=include_adult
            )
        except Exception:
            _log.exception("Failed to load sites")
            return

        self._bridge.post(self._apply_sites, list(sites))

    def _apply_sites(self, sites: list[SiteConfig]) -> None:
        self._sites_cache = sites
        view = self._safe_get_view(VIEW_SEARCH)
        if view is not None and hasattr(view, "set_sites"):
            try:
                view.set_sites(sites)
            except Exception:
                _log.exception("SearchView.set_sites raised")

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    def _build_main_view(self) -> MainView:
        specs = self._build_view_specs()
        return MainView(
            self,
            views=specs,
            labels=self._labels.main_view,
            nav_position="left",
            nav_collapsible=True,
            lazy=False,  # build every view at boot so events always have targets
            destroy_on_hide=False,
            show_status_bar=True,
            on_active_view_change=self._on_active_view_change,
        )

    def _build_view_specs(self) -> list[ViewSpec]:
        return [
            ViewSpec(
                id=VIEW_SEARCH,
                label=self._labels.nav_search,
                icon="🔍",
                factory=self._build_search_view,
            ),
            ViewSpec(
                id=VIEW_LIBRARY,
                label=self._labels.nav_library,
                icon="📚",
                factory=self._build_library_view,
            ),
            ViewSpec(
                id=VIEW_DOWNLOADS,
                label=self._labels.nav_downloads,
                icon="⬇",
                factory=self._build_download_view,
            ),
            ViewSpec(
                id=VIEW_SETTINGS,
                label=self._labels.nav_settings,
                icon="⚙",
                factory=self._build_settings_view,
                shortcut="Control-comma",
            ),
        ]

    # ------------------------------------------------------------------
    # View factories
    # ------------------------------------------------------------------

    def _build_search_view(self, master: ctk.CTkFrame) -> ctk.CTkFrame:
        return SearchView(
            master,
            sites=list(self._sites_cache),
            labels=self._labels.search,
            manga_card_labels=self._labels.manga_card,
            on_search=self._on_search_requested,
            on_download_manga=self._hooks.download_manga,
            on_view_manga=self._hooks.open_manga,
            on_cover_needed=self._on_search_cover_needed,
            selectable=False,
        )

    def _build_library_view(self, master: ctk.CTkFrame) -> ctk.CTkFrame:
        return LibraryView(
            master,
            labels=self._labels.library,
            manga_card_labels=self._labels.manga_card,
            on_load=self._on_library_load,
            sort_options=[
                # Standard library sort options. Hosts can override by
                # rebuilding the view if they want custom ones.
                # (See: default sort ids used by `_on_library_load` host
                # side — the view emits them verbatim.)
            ],
            batch_actions=[
                # Host defines its own via app overrides. Left empty here so
                # the shell has no UX opinion.
            ],
            on_cover_needed=self._on_library_cover_needed,
            on_open_manga=self._hooks.continue_reading,
            on_continue_reading=self._hooks.continue_reading,
            on_right_click_manga=self._hooks.right_click_manga,
            reload_on_activate=False,
        )

    def _build_download_view(self, master: ctk.CTkFrame) -> ctk.CTkFrame:
        return DownloadView(
            master,
            labels=self._labels.download,
            progress_labels=self._labels.progress,
            on_pause=self._on_download_pause,
            on_resume=self._on_download_resume,
            on_cancel=self._on_download_cancel,
            on_retry=self._on_download_retry,
            on_remove=self._on_download_remove,
            on_open_folder=self._on_download_open_folder,
            on_open_output_dir=self._on_download_open_output_dir,
            on_pause_all=self._on_download_pause_all,
            on_resume_all=self._on_download_resume_all,
            on_cancel_all=self._on_download_cancel_all,
            on_clear_finished=self._on_download_clear_finished,
            on_resume_queue=self._on_download_resume_queue,
        )

    def _build_settings_view(self, master: ctk.CTkFrame) -> ctk.CTkFrame:
        schema = self._services.build_settings_schema()
        snapshot = dict(self._services.snapshot_settings())
        return SettingsView(
            master,
            schema=schema,
            current_values=snapshot,
            on_apply=self._on_settings_apply,
            on_reload=self._on_settings_reload,
            on_restart=self._on_settings_restart,
            labels=self._labels.settings,
        )

    # ------------------------------------------------------------------
    # Shell events
    # ------------------------------------------------------------------

    def _on_active_view_change(self, view_id: str, label: str) -> None:
        try:
            self.title(f"{self._labels.window_title} — {label}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search_requested(self, request: SearchRequest) -> None:
        self._bridge.run_and_forget(self._run_search(request))

    async def _run_search(self, request: SearchRequest) -> None:
        try:
            page = await self._services.search(request)
        except Exception as exc:
            _log.exception("Search failed")
            self._bridge.post(self._apply_search_error, str(exc))
            return
        self._bridge.post(self._apply_search_page, page)

    def _apply_search_page(self, page: SearchPage) -> None:
        view = self._safe_get_view(VIEW_SEARCH)
        if view is not None and hasattr(view, "set_results"):
            try:
                view.set_results(page)
            except Exception:
                _log.exception("SearchView.set_results raised")

    def _apply_search_error(self, message: str) -> None:
        view = self._safe_get_view(VIEW_SEARCH)
        if view is not None and hasattr(view, "set_error"):
            try:
                view.set_error(message)
            except Exception:
                _log.exception("SearchView.set_error raised")

    def _on_search_cover_needed(self, data: MangaCardData) -> None:
        if not data.cover_url:
            return
        self._request_cover(data.cover_url, VIEW_SEARCH, data.manga_id)

    # ------------------------------------------------------------------
    # Library
    # ------------------------------------------------------------------

    def _on_library_load(self, query: LibraryQuery) -> None:
        self._bridge.run_and_forget(self._run_library_load(query))

    async def _run_library_load(self, query: LibraryQuery) -> None:
        try:
            page = await self._services.library.query(query)
        except Exception as exc:
            _log.exception("Library query failed")
            self._bridge.post(self._apply_library_error, str(exc))
            return
        self._bridge.post(self._apply_library_page, page)

    def _apply_library_page(self, page: LibraryPage) -> None:
        view = self._safe_get_view(VIEW_LIBRARY)
        if view is not None and hasattr(view, "set_page"):
            try:
                view.set_page(page)
            except Exception:
                _log.exception("LibraryView.set_page raised")

    def _apply_library_error(self, message: str) -> None:
        view = self._safe_get_view(VIEW_LIBRARY)
        if view is not None and hasattr(view, "set_error"):
            try:
                view.set_error(message)
            except Exception:
                _log.exception("LibraryView.set_error raised")

    def _on_library_cover_needed(self, data: LibraryCardData) -> None:
        if not data.card.cover_url:
            return
        self._request_cover(data.card.cover_url, VIEW_LIBRARY, data.manga_id)

    def _trigger_library_reload(self) -> None:
        """Tk-thread callback: ask the library view to re-fetch its page."""
        view = self._safe_get_view(VIEW_LIBRARY)
        if view is not None and hasattr(view, "trigger_load"):
            try:
                view.trigger_load()
            except Exception:
                _log.exception("LibraryView.trigger_load raised")

    # ------------------------------------------------------------------
    # Covers
    # ------------------------------------------------------------------

    def _request_cover(self, url: str, view_id: str, manga_id: str) -> None:
        async def _fetch() -> None:
            try:
                img = await self._services.image_cache.get(url)
            except Exception:
                _log.exception("Cover fetch failed for {}", url)
                img = None
            self._bridge.post(self._apply_cover, view_id, manga_id, img)

        self._bridge.run_and_forget(_fetch())

    def _apply_cover(self, view_id: str, manga_id: str, img: Any) -> None:
        view = self._safe_get_view(view_id)
        if view is not None and hasattr(view, "set_cover"):
            try:
                view.set_cover(manga_id, img)
            except Exception:
                _log.exception("View.set_cover raised for {}", view_id)

    # ------------------------------------------------------------------
    # Download operations (user intents)
    # ------------------------------------------------------------------

    def _on_download_pause(self, task_id: str) -> None:
        self._run_downloader_op("pause", task_id)

    def _on_download_resume(self, task_id: str) -> None:
        self._run_downloader_op("resume", task_id)

    def _on_download_cancel(self, task_id: str) -> None:
        self._run_downloader_op("cancel", task_id)

    def _on_download_retry(self, task_id: str) -> None:
        self._run_downloader_op("retry", task_id)

    def _on_download_remove(self, task_id: str) -> None:
        forget = getattr(self._services.downloader, "forget", None)

        async def _forget() -> None:
            if forget is not None:
                try:
                    await forget(task_id)
                except Exception:
                    _log.exception("downloader.forget raised for {}", task_id)
            self._bridge.post(self._remove_download_task, task_id)

        self._bridge.run_and_forget(_forget())

    def _remove_download_task(self, task_id: str) -> None:
        view = self._safe_get_view(VIEW_DOWNLOADS)
        if view is not None and hasattr(view, "remove_task"):
            try:
                view.remove_task(task_id)
            except Exception:
                _log.exception("DownloadView.remove_task raised")

    def _on_download_open_folder(self, task_id: str) -> None:
        view = self._safe_get_view(VIEW_DOWNLOADS)
        task: DownloadTaskData | None = None
        if view is not None:
            try:
                task = view._tasks.get(task_id)  # type: ignore[attr-defined]
            except Exception:
                task = None
        if task is None or not task.output_path:
            return
        self._open_path(Path(task.output_path))

    def _on_download_open_output_dir(self) -> None:
        try:
            path = self._services.settings.storage.downloads_dir
        except Exception:
            path = None
        if path is None:
            return
        self._open_path(Path(path))

    def _on_download_pause_all(self) -> None:
        self._run_downloader_op("pause_all")

    def _on_download_resume_all(self) -> None:
        self._run_downloader_op("resume_all")

    def _on_download_cancel_all(self) -> None:
        self._run_downloader_op("cancel_all")

    def _on_download_clear_finished(self) -> None:
        # Local UI-only operation: drop every terminal task from the view.
        view = self._safe_get_view(VIEW_DOWNLOADS)
        if view is None:
            return
        try:
            tasks = list(view.get_tasks())  # type: ignore[attr-defined]
        except Exception:
            tasks = []
        for t in tasks:
            if t.status in (
                DownloadStatus.DONE,
                DownloadStatus.FAILED,
                DownloadStatus.CANCELLED,
            ):
                if hasattr(view, "remove_task"):
                    try:
                        view.remove_task(t.task_id)
                    except Exception:
                        _log.exception(
                            "DownloadView.remove_task raised for {}", t.task_id
                        )

    def _on_download_resume_queue(self) -> None:
        self._run_downloader_op("resume_all")

    def _run_downloader_op(self, op_name: str, *args: Any) -> None:
        op = getattr(self._services.downloader, op_name, None)
        if op is None:
            _log.warning("downloader.{} not implemented", op_name)
            return

        async def _run() -> None:
            try:
                await op(*args)
            except Exception as exc:
                _log.exception("downloader.{} failed", op_name)
                self._bridge.post(
                    lambda: self._status(f"{op_name}: {exc}", kind="error")
                )

        self._bridge.run_and_forget(_run())

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _on_settings_apply(self, changes: dict[str, Any]) -> ApplyResult:
        try:
            result = self._services.apply_settings(changes)
        except Exception as exc:
            _log.exception("apply_settings raised")
            return ApplyResult(
                success=False, message=f"Failed to save settings: {exc}"
            )

        if result.success and not result.requires_restart:
            try:
                self._apply_theme_from_settings()
            except Exception:
                _log.exception("Failed to reapply theme after settings save")
        return result

    def _on_settings_reload(self) -> dict[str, Any]:
        try:
            return dict(self._services.snapshot_settings())
        except Exception:
            _log.exception("snapshot_settings raised")
            return {}

    def _on_settings_restart(self) -> None:
        restart = getattr(self._hooks, "restart_app", None)
        if restart is not None:
            try:
                restart()
                return
            except Exception:
                _log.exception("restart_app hook raised")
        _log.warning("No restart_app hook; closing the app instead")
        self._on_close()

    # ------------------------------------------------------------------
    # Event subscriptions
    # ------------------------------------------------------------------

    def _subscribe_events(self) -> None:
        bus = self._services.event_bus

        async def _on_started(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            task_id = payload.get("task_id")
            if not task_id:
                return
            task = DownloadTaskData(
                task_id=task_id,
                manga_title=payload.get("manga_title", ""),
                chapter_label=payload.get("chapter_label", ""),
                site_name=payload.get("site_name", ""),
                status=DownloadStatus.PENDING,
            )
            self._bridge.post(self._apply_download_add, task)

        async def _on_progress(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            task_id = payload.get("task_id")
            if not task_id:
                return

            raw_status = payload.get("status", DownloadStatus.RUNNING.value)
            try:
                status = DownloadStatus(raw_status)
            except ValueError:
                _log.warning(
                    "download.progress: unknown status {!r}, defaulting to RUNNING",
                    raw_status,
                )
                status = DownloadStatus.RUNNING

            try:
                completed = int(payload.get("completed") or 0)
            except (TypeError, ValueError):
                completed = 0
            try:
                sub_completed = int(payload.get("page_index") or 0)
            except (TypeError, ValueError):
                sub_completed = 0

            changes: dict[str, Any] = {
                "status": status,
                "completed": completed,
                "total": payload.get("total"),
                "bytes_completed": int(payload.get("bytes_completed") or 0),
                "bytes_total": payload.get("bytes_total"),
                "speed": payload.get("speed_bps"),
                "eta_seconds": payload.get("eta_seconds"),
                "sub_label": payload.get("chapter_label", ""),
                "sub_completed": sub_completed,
                "sub_total": payload.get("page_count"),
                "error": payload.get("error"),
            }
            self._bridge.post(self._apply_download_update, task_id, changes)

        async def _on_completed(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            task_id = payload.get("task_id")
            if not task_id:
                return
            self._bridge.post(
                self._apply_download_update,
                task_id,
                {
                    "status": DownloadStatus.DONE,
                    "output_path": payload.get("path", ""),
                },
            )

        async def _on_failed(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            task_id = payload.get("task_id")
            if not task_id:
                return
            self._bridge.post(
                self._apply_download_update,
                task_id,
                {
                    "status": DownloadStatus.FAILED,
                    "error": payload.get("error", "Unknown error"),
                },
            )

        async def _on_paused(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            task_id = payload.get("task_id")
            if not task_id:
                return
            self._bridge.post(
                self._apply_download_update,
                task_id,
                {"status": DownloadStatus.PAUSED},
            )

        async def _on_resumed(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            task_id = payload.get("task_id")
            if not task_id:
                return
            self._bridge.post(
                self._apply_download_update,
                task_id,
                {"status": DownloadStatus.RUNNING},
            )

        async def _on_cancelled(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            task_id = payload.get("task_id")
            if not task_id:
                return
            self._bridge.post(
                self._apply_download_update,
                task_id,
                {
                    "status": DownloadStatus.CANCELLED,
                    "error": payload.get("error"),
                },
            )

        async def _on_queue_paused(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            paused = bool(payload.get("paused", True))
            self._bridge.post(self._apply_queue_paused, paused)

        async def _on_health_changed(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            site_id = payload.get("site_id")
            health = payload.get("health")
            if not site_id or health is None:
                return
            self._bridge.post(self._apply_site_health, site_id, health)

        async def _on_library_updated(_event: Any) -> None:
            # Must NOT touch Tk here — we're on the async loop thread.
            # Marshal to the Tk thread through the bridge.
            self._bridge.post(self._trigger_library_reload)

        _subscribe(bus, "download.started", _on_started)
        _subscribe(bus, "download.progress", _on_progress)
        _subscribe(bus, "download.completed", _on_completed)
        _subscribe(bus, "download.failed", _on_failed)
        _subscribe(bus, "download.paused", _on_paused)
        _subscribe(bus, "download.resumed", _on_resumed)
        _subscribe(bus, "download.cancelled", _on_cancelled)
        _subscribe(bus, "queue.paused", _on_queue_paused)
        _subscribe(bus, "site.health.changed", _on_health_changed)
        _subscribe(bus, "library.updated", _on_library_updated)

    # -- Tk-thread event appliers --------------------------------------

    def _apply_download_add(self, task: DownloadTaskData) -> None:
        view = self._safe_get_view(VIEW_DOWNLOADS)
        if view is not None and hasattr(view, "add_task"):
            try:
                view.add_task(task)
            except Exception:
                _log.exception("DownloadView.add_task raised")

    def _apply_download_update(
        self, task_id: str, changes: dict[str, Any]
    ) -> None:
        view = self._safe_get_view(VIEW_DOWNLOADS)
        if view is not None and hasattr(view, "update_task"):
            try:
                view.update_task(task_id, **changes)
            except Exception:
                _log.exception("DownloadView.update_task raised")

    def _apply_queue_paused(self, paused: bool) -> None:
        view = self._safe_get_view(VIEW_DOWNLOADS)
        if view is not None and hasattr(view, "set_queue_paused"):
            try:
                view.set_queue_paused(paused)
            except Exception:
                _log.exception("DownloadView.set_queue_paused raised")

    def _apply_site_health(self, site_id: str, health: Any) -> None:
        view = self._safe_get_view(VIEW_SEARCH)
        if view is None:
            return
        # SearchView doesn't expose a per-site health update directly; the
        # SiteDropdown does. Reach in via duck-typed access.
        dropdown = getattr(view, "_site_dropdown", None)
        if dropdown is not None and hasattr(dropdown, "update_health"):
            try:
                dropdown.update_health(site_id, health)
            except Exception:
                _log.exception("SiteDropdown.update_health raised")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True

        # Hide the window immediately for better UX while we tear down.
        try:
            self.withdraw()
        except Exception:
            pass

        # 1) Persist UI state (still on Tk thread, safe).
        try:
            state = self._services.state_store.get("app", {})
            state["geometry"] = self.geometry()
            if self._main_view is not None:
                state["main_view"] = self._main_view.get_state()
            self._services.state_store.set("app", state)
            self._services.state_store.flush()
        except Exception:
            _log.exception("Failed to persist app state")

        # 2) Schedule graceful async shutdown (non-blocking), then poll.
        shutdown = getattr(self._services, "on_shutdown_async", None)
        if shutdown is not None and callable(shutdown):
            try:
                self._shutdown_future = self._bridge.run(shutdown())  # type: ignore[arg-type]
            except Exception:
                _log.exception("Failed to schedule on_shutdown_async")
                self._shutdown_future = None
        else:
            self._shutdown_future = None

        self._shutdown_deadline = time.monotonic() + 3.0
        self._poll_shutdown()

    def _poll_shutdown(self) -> None:
        fut = self._shutdown_future
        if fut is not None:
            if fut.done():
                try:
                    fut.result()
                except Exception:
                    _log.exception("services.on_shutdown_async raised")
                self._shutdown_future = None
            elif time.monotonic() > self._shutdown_deadline:
                _log.warning(
                    "on_shutdown_async timed out after 3s; cancelling"
                )
                try:
                    fut.cancel()
                except Exception:
                    pass
                self._shutdown_future = None

        if self._shutdown_future is not None:
            try:
                self.after(50, self._poll_shutdown)
                return
            except Exception:
                self._shutdown_future = None

        # 3) Stop the async bridge.
        try:
            self._bridge.stop(timeout=3.0)
        except Exception:
            _log.exception("AsyncBridge.stop raised")

        # 4) Destroy the Tk root.
        try:
            self.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_get_view(self, view_id: str) -> Any:
        try:
            return self._main_view.get_view(view_id)
        except Exception:
            return None

    def _status(self, message: str, *, kind: str = "info") -> None:
        if self._main_view is None:
            return
        try:
            self._main_view.set_status(message, kind=kind)
        except Exception:
            _log.exception("MainView.set_status raised")

    def _open_path(self, path: Path) -> None:
        opener = self._hooks.open_path
        if opener is not None:
            try:
                opener(path)
                return
            except Exception:
                _log.exception("open_path hook raised")
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            _log.exception("Failed to open path {}", path)


# ===========================================================================
# Internal helpers
# ===========================================================================

def _subscribe(
    bus: EventBusLike, event_type: str, handler: Callable[[Any], Any]
) -> None:
    try:
        bus.subscribe(event_type, handler)
    except Exception:
        _log.exception("Failed to subscribe to event {!r}", event_type)
