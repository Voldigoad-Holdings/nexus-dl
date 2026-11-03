"""Textual screen that anchors the NexusDL TUI.

The main screen is the **dashboard**: the first screen the app pushes at
startup, the one every other screen is eventually pushed from, and the
one the user returns to when they dismiss any sub-screen. It is not a
modal surface and does not perform the pipeline's heavy operations —
those happen on the dedicated screens (search, download, library). Its
job is to make the app's state **legible at a glance** and to get the
user to the screen they need in one keypress.

Four things the dashboard surfaces
----------------------------------
The layout is a two-column dashboard:

* **Left column** — a vertical menu of destinations:

  - ``Search`` — push the search screen.
  - ``Library`` — push the library screen.
  - ``Settings`` — push the settings screen.
  - ``Logs`` — push the logs screen.
  - ``Help`` — push the help screen.
  - ``Quit`` — exit the app (with confirmation if a download is in
    progress — see "Quit confirmation" below).

  The menu is the primary navigation mechanism. Every destination is
  one keystroke away (``S``, ``L``, ``T``, ``G``, ``H``, ``Ctrl+Q``)
  and also reachable by ``↑`` / ``↓`` + ``Enter`` — keyboard-first, but
  mouse-friendly via the option list's click handling.

* **Right column** — a read-only status panel showing the app's current
  state:

  - **Identity line** — app name, slug, version.
  - **Server state** — whether the API server is running (if the app
    is in "server mode") and on which port.
  - **Library summary** — the number of mangas, chapters, and the last
    update time, sourced from
    :class:`~nexusdl.core.library.database.LibraryDatabase` on mount
    and after every ``library.updated`` event.
  - **Download state** — the number of active and queued tasks, sourced
    from
    :class:`~nexusdl.core.downloader.progress.ProgressTracker`.
  - **Site registry state** — the number of registered sites and the
    number whose health is not ``HEALTHY`` (if a health check has run).

  Every value is a snapshot taken at mount and refreshed in response to
  the events the panel subscribes to. The panel is a **viewer**, not a
  control — the user cannot click to trigger anything from it.

Bottom bar
----------
A single-line status bar showing:

* **Shortcut hints** — the primary key for each destination
  (``"S search · L library · T settings · G logs · H help"``).
* **Last action feedback** — a short-lived message after the user
  returns from a sub-screen ("Library: 42 mangas"), so they know the
  action registered even when the sub-screen's content looks similar
  to the dashboard's summary.

The feedback message is cleared after
:data:`_FEEDBACK_LINGER_SECONDS` seconds — long enough to be read, short
enough that it does not permanently clutter the status bar.

Sub-screen coordination
-----------------------
Every navigation destination is pushed with ``self.app.push_screen``,
which returns when the pushed screen is popped. The main screen awaits
that return through Textual's callback mechanism
(``push_screen(screen, callback=...)``), so when a sub-screen is popped
the main screen can refresh its state panel. This is the standard
Textual pattern for "sub-screen returned, update parent" and avoids
polling.

There is **one place in the codebase** that decides which sub-screens
exist and how to push them — this file. A future screen added to
``screens/`` is wired here and nowhere else. That centralisation is
deliberate: a screen with its own navigation logic would produce N
different notions of "back", which the pipeline's UX contract forbids.

Quit confirmation
-----------------
``Ctrl+Q`` is the quit binding. When the download manager reports one
or more active or queued tasks, the quit action **asks for
confirmation** via :meth:`~textual.app.App.push_screen_wait` on a small
confirmation modal, and only exits the app if the user confirms. A user
who hits ``Ctrl+Q`` mid-download by accident does not lose the download
without warning.

The confirmation modal is a minimal
:class:`~textual.screen.ModalScreen` declared in this file — it has no
external dependencies and no state of its own, so an inline declaration
is the appropriate choice for a screen that exists only for this file's
use.

Localisation
------------
The screen introduces twenty keys under ``main.*``:

* Menu item labels — ``main.menu.search``, ``main.menu.library``,
  ``main.menu.settings``, ``main.menu.logs``, ``main.menu.help``,
  ``main.menu.quit``.
* Panel headers — ``main.panel.identity``, ``main.panel.library``,
  ``main.panel.downloads``, ``main.panel.sites``.
* Panel values — ``main.value.not_started`` (for the server when not
  running), ``main.value.never`` (for the library when it has never
  been updated), ``main.value.none`` (for empty lists).
* Status bar — ``main.status.hint``, ``main.status.quit_confirm.title``,
  ``main.status.quit_confirm.message``, ``main.status.quit_confirm.yes``,
  ``main.status.quit_confirm.no``.
* Feedback fragments — ``main.feedback.library_summary`` (rendered
  after returning from the library screen), ``main.feedback.search_back``,
  ``main.feedback.settings_back``.

A missing key renders the key verbatim, matching the contract every
other widget and screen in this package follows. See
:mod:`nexusdl.core.i18n`.

Dependency graph (no cycles)::

    nexusdl.core.constants                       ─┐
    nexusdl.core.downloader.manager               │
    nexusdl.core.downloader.progress              │
    nexusdl.core.events                           ├──► interfaces.cli.screens.main
    nexusdl.core.i18n                             │
    nexusdl.core.library.database                 │
    nexusdl.core.registry.site_registry           │
    nexusdl.core.version                          │
    nexusdl.interfaces.cli.screens.*              │
    textual                                       │

The main screen imports every other screen in this package, because it
is the composition root of the TUI's screen graph. This is the **only**
file in ``screens/`` that imports other screens — no other screen may
push a sibling directly, or the navigation graph would lose its single
authority.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Final

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Text
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from nexusdl.core.constants import (
    APP_NAME,
    APP_SLUG,
    EVENT_LIBRARY_UPDATED,
)
from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.version import __version__ as NEXUSDL_VERSION
from nexusdl.core.utils.time import format_relative, utcnow
from nexusdl.interfaces.cli.screens.help import HelpScreen
from nexusdl.interfaces.cli.screens.library import LibraryScreen
from nexusdl.interfaces.cli.screens.logs import LogsScreen
from nexusdl.interfaces.cli.screens.search import SearchScreen
from nexusdl.interfaces.cli.screens.settings import SettingsScreen

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.downloader.manager import DownloadManager
    from nexusdl.core.events import Event, EventBus
    from nexusdl.core.library.database import LibraryDatabase
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.core.session.http_session import HttpSession


__all__ = ["MainScreen"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs.
_ID_MENU: Final[str] = "main-menu"
_ID_PANEL: Final[str] = "main-panel"
_ID_STATUS: Final[str] = "main-status"

#: Menu item ids, in display order. Stable identifiers used by the
#: dispatch in :meth:`MainScreen.on_option_list_option_selected`.
_MENU_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("search", "main.menu.search"),
    ("library", "main.menu.library"),
    ("settings", "main.menu.settings"),
    ("logs", "main.menu.logs"),
    ("help", "main.menu.help"),
    ("quit", "main.menu.quit"),
)

#: How long the status-bar feedback message persists, in seconds. Two
#: seconds is enough for a user returning from a sub-screen to notice it,
#: short enough that the status bar does not become a scrolling log.
_FEEDBACK_LINGER_SECONDS: Final[float] = 2.0

#: Refresh interval for the status panel, in seconds. The panel also
#: refreshes on ``library.updated`` events; the interval catches
#: download-state changes that do not emit a library event (progress
#: ticking, a task entering a new phase) so the count remains live
#: without subscribing to the progress tracker directly.
_PANEL_REFRESH_INTERVAL: Final[float] = 2.0


# --------------------------------------------------------------------------- #
# Quit confirmation modal                                                     #
# --------------------------------------------------------------------------- #


class _QuitConfirm(ModalScreen[bool]):
    """A modal asking the user to confirm quitting with active downloads.

    Declared inline because it is used by exactly one screen
    (:class:`MainScreen`) and has no state beyond the number of active
    downloads it displays. Extracting it to its own module would add
    a file for a modal that has zero reuse potential.

    The screen's result is ``True`` (quit) or ``False`` (stay).
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS: ClassVar[str] = """
    _QuitConfirm {
        align: center middle;
    }
    _QuitConfirm > Vertical {
        width: 60;
        height: auto;
        border: thick $error;
        padding: 1 2;
        background: $surface;
    }
    _QuitConfirm Label {
        width: 1fr;
        margin-bottom: 1;
    }
    _QuitConfirm .dialog-buttons {
        height: auto;
        align-horizontal: right;
    }
    _QuitConfirm Button {
        margin-left: 1;
    }
    """

    def __init__(
        self,
        *,
        active_downloads: int,
        translator: Translator,
    ) -> None:
        super().__init__()
        self._active = active_downloads
        self._translator = translator

    def compose(self) -> ComposeResult:
        """Yield the confirmation dialog content."""
        with Vertical():
            yield Label(
                self._translator.translate(
                    "main.status.quit_confirm.title"
                ),
                classes="dialog-title",
            )
            yield Label(
                self._translator.translate(
                    "main.status.quit_confirm.message",
                    active=self._active,
                )
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button(
                    self._translator.translate(
                        "main.status.quit_confirm.no"
                    ),
                    id="quit-no",
                    variant="default",
                )
                yield Button(
                    self._translator.translate(
                        "main.status.quit_confirm.yes"
                    ),
                    id="quit-yes",
                    variant="error",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss with the corresponding boolean result."""
        if event.button.id == "quit-yes":
            self.dismiss(True)
        elif event.button.id == "quit-no":
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Escape cancels — matches the "no" button."""
        self.dismiss(False)


# --------------------------------------------------------------------------- #
# MainScreen                                                                  #
# --------------------------------------------------------------------------- #


class MainScreen(Screen[None]):
    """The dashboard screen that anchors the TUI.

    See the module docstring for the design contract, the sub-screen
    coordination pattern, and the quit-confirmation flow.

    Args:
        settings: Application settings. Read for the identity line and
            for the API server state panel.
        registry: The loaded site registry, for the site-count panel.
        database: The library database, for the library summary panel.
        manager: The download manager, for the download-state panel and
            for the quit-confirmation gate.
        session: The HTTP session. Passed through to the search screen
            when the user opens it.
        event_bus: The shared event bus. The screen subscribes to
            ``library.updated`` for the library-summary refresh.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        name: Optional Textual screen name.
        id: Optional Textual screen id.
        classes: Optional Textual CSS classes.
    """

    #: Every primary destination has its own keybinding. The bindings
    #: are on the screen (not the menu) so the shortcuts are active
    #: regardless of focus — a user who has tabbed to the status panel
    #: can still press ``S`` to search.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("s", "goto_search", "Search"),
        Binding("l", "goto_library", "Library"),
        Binding("t", "goto_settings", "Settings"),
        Binding("g", "goto_logs", "Logs"),
        Binding("h", "goto_help", "Help"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    #: Default CSS.
    DEFAULT_CSS: ClassVar[str] = """
    MainScreen {
        background: $surface;
    }
    #main-menu {
        width: 26;
        height: 1fr;
        border-right: solid $primary;
        background: $panel;
        padding: 1 0;
    }
    #main-panel {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    #main-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        *,
        settings: Settings,
        registry: SiteRegistry,
        database: LibraryDatabase,
        manager: DownloadManager,
        session: HttpSession,
        event_bus: EventBus,
        translator: Translator | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._settings = settings
        self._registry = registry
        self._database = database
        self._manager = manager
        self._session = session
        self._event_bus = event_bus
        self._translator = (
            translator if translator is not None else get_default_translator()
        )

        #: The status-bar feedback message currently shown, or ``None``.
        #: Cleared by a timer after :data:`_FEEDBACK_LINGER_SECONDS`.
        self._feedback: str | None = None

        #: Timer handle for clearing :attr:`_feedback`. Cancelled when
        #: a new feedback message replaces the current one, so the timer
        #: does not clear the *next* message.
        self._feedback_timer: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the menu, the status panel, and the bottom bar."""
        with Horizontal():
            yield OptionList(
                *[
                    Option(
                        self._translator.translate(label_key),
                        id=item_id,
                    )
                    for item_id, label_key in _MENU_ITEMS
                ],
                id=_ID_MENU,
            )
            yield VerticalScroll(
                Static("", id=_ID_PANEL),
            )
        yield Label("", id=_ID_STATUS)

    def on_mount(self) -> None:
        """Subscribe to library events and render the initial state."""
        self._event_bus.subscribe(
            EVENT_LIBRARY_UPDATED, self._on_library_updated
        )
        self.query_one(f"#{_ID_MENU}", OptionList).focus()
        self._refresh_panel()
        self._refresh_status_bar()
        self.set_interval(_PANEL_REFRESH_INTERVAL, self._refresh_panel)

    def on_unmount(self) -> None:
        """Unsubscribe from library events and cancel any pending timers."""
        self._event_bus.unsubscribe(
            EVENT_LIBRARY_UPDATED, self._on_library_updated
        )
        if self._feedback_timer is not None:
            self._feedback_timer.cancel()
            self._feedback_timer = None

    # ------------------------------------------------------------------ #
    # Menu event handler                                                 #
    # ------------------------------------------------------------------ #

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        """Dispatch a menu selection to the corresponding destination.

        The six destinations are the six entries of
        :data:`_MENU_ITEMS`; anything else (which cannot occur — the
        option list only contains these ids) is silently ignored, and a
        defensive branch would be dead code.
        """
        item_id = event.option.id
        if item_id is None:
            return
        if item_id == "search":
            self.action_goto_search()
        elif item_id == "library":
            self.action_goto_library()
        elif item_id == "settings":
            self.action_goto_settings()
        elif item_id == "logs":
            self.action_goto_logs()
        elif item_id == "help":
            self.action_goto_help()
        elif item_id == "quit":
            self.action_quit()

    # ------------------------------------------------------------------ #
    # Navigation actions — each binding and menu item share one          #
    # ------------------------------------------------------------------ #

    def action_goto_search(self) -> None:
        """Push the search screen. Bound to ``S`` and the menu item."""
        screen = SearchScreen(
            registry=self._registry,
            session=self._session,
            settings=self._settings,
            translator=self._translator,
        )
        self.app.push_screen(screen, callback=self._after_search)

    def action_goto_library(self) -> None:
        """Push the library screen. Bound to ``L`` and the menu item."""
        screen = LibraryScreen(
            database=self._database,
            event_bus=self._event_bus,
            translator=self._translator,
        )
        self.app.push_screen(screen, callback=self._after_library)

    def action_goto_settings(self) -> None:
        """Push the settings screen. Bound to ``T`` and the menu item."""
        screen = SettingsScreen(
            self._settings, translator=self._translator
        )
        self.app.push_screen(screen, callback=self._after_settings)

    def action_goto_logs(self) -> None:
        """Push the logs screen. Bound to ``G`` and the menu item."""
        screen = LogsScreen(
            log_router=self.app.log_router,  # type: ignore[attr-defined]
            translator=self._translator,
        )
        self.app.push_screen(screen, callback=self._after_generic)

    def action_goto_help(self) -> None:
        """Push the help screen. Bound to ``H`` and the menu item."""
        screen = HelpScreen(translator=self._translator)
        self.app.push_screen(screen, callback=self._after_generic)

    def action_quit(self) -> None:
        """Quit the app, asking for confirmation when downloads are active.

        Bound to ``Ctrl+Q``. When the manager reports any non-terminal
        task, a :class:`_QuitConfirm` modal is pushed and the app only
        exits if the user confirms. When no downloads are active, the
        app exits immediately.
        """
        self._do_quit()

    @work(exclusive=True, group="quit")
    async def _do_quit(self) -> None:
        """Check download state, confirm if needed, and exit the app."""
        pending = await self._count_active_downloads()
        if pending > 0:
            confirmed = await self.app.push_screen_wait(
                _QuitConfirm(
                    active_downloads=pending,
                    translator=self._translator,
                )
            )
            if not confirmed:
                return
        self.app.exit()

    # ------------------------------------------------------------------ #
    # Sub-screen return callbacks                                        #
    # ------------------------------------------------------------------ #

    def _after_search(self, _result: object) -> None:
        """Called when the search screen is popped.

        The search screen does not currently return a value; the
        callback exists so the status panel refreshes (a download
        started from a search result might have added a manga to the
        library by the time the screen pops) and the user receives a
        short feedback message.
        """
        self._set_feedback(
            self._translator.translate("main.feedback.search_back")
        )
        self._refresh_panel()

    def _after_library(self, _result: object) -> None:
        """Called when the library screen is popped.

        Refreshes the library-summary panel (a delete or rename inside
        the library screen would have changed the count) and shows the
        current library summary as feedback.
        """
        # Schedule the refresh in a worker — the count query is async.
        self._show_library_summary_feedback()
        self._refresh_panel()

    def _after_settings(self, _result: object) -> None:
        """Called when the settings screen is popped.

        Settings cannot be edited from the TUI (they are frozen and
        loaded from disk at startup), so there is nothing to re-read.
        The feedback message reassures the user that the screen closed
        cleanly; the panel refresh keeps the identity line accurate if
        a future version of the settings screen supports any live
        change.
        """
        self._set_feedback(
            self._translator.translate("main.feedback.settings_back")
        )
        self._refresh_panel()

    def _after_generic(self, _result: object) -> None:
        """Called when logs or help close. No state changed; no refresh."""
        # Logs and Help are read-only destinations. No library event
        # fires, no download state changes. The panel's next interval
        # tick (if any) will render the same values it rendered before.

    # ------------------------------------------------------------------ #
    # Panel rendering                                                    #
    # ------------------------------------------------------------------ #

    def _refresh_panel(self) -> None:
        """Refresh the status panel with the current state."""
        self._run_panel_refresh()

    @work(exclusive=True, group="panel")
    async def _run_panel_refresh(self) -> None:
        """Fetch the panel's state and render it.

        Every data source is queried independently so one failure (a
        database hiccup, a manager exception) does not blank the whole
        panel — a defensive choice matching the settings screen's
        "defensive section rendering" policy.
        """
        panel = self.query_one(f"#{_ID_PANEL}", Static)
        text = Text()

        # --- Identity ----------------------------------------------- #
        text.append(
            self._translator.translate("main.panel.identity"),
            style="bold underline",
        )
        text.append("\n\n")
        text.append(f"  {APP_NAME} ", style="")
        text.append(f"({APP_SLUG})", style="dim")
        text.append(f"  v{NEXUSDL_VERSION}", style="dim")
        text.append("\n\n")

        # --- Library ------------------------------------------------ #
        text.append(
            self._translator.translate("main.panel.library"),
            style="bold underline",
        )
        text.append("\n\n")
        try:
            mangas = await self._database.list_mangas(page=1, per_page=1)
            manga_count = mangas.total
            last_updated = await self._last_library_update()
            text.append(
                f"  {manga_count} mangas", style=""
            )
            text.append("\n")
            text.append(
                "  ",
                style="",
            )
            text.append(
                self._translator.translate(
                    "main.panel.library_last_updated",
                    when=(
                        format_relative(last_updated)
                        if last_updated is not None
                        else self._translator.translate(
                            "main.value.never"
                        )
                    ),
                ),
                style="dim",
            )
            text.append("\n\n")
        except Exception as exc:  # noqa: BLE001 — see module docstring
            text.append(f"  {type(exc).__name__}: {exc}", style="red")
            text.append("\n\n")

        # --- Downloads ---------------------------------------------- #
        text.append(
            self._translator.translate("main.panel.downloads"),
            style="bold underline",
        )
        text.append("\n\n")
        try:
            running = await self._manager.list_tasks(
                status=_DOWNLOAD_STATUS_RUNNING
            )
            pending = await self._manager.list_tasks(
                status=_DOWNLOAD_STATUS_PENDING
            )
            text.append(
                f"  {len(running)} active, {len(pending)} pending",
                style="",
            )
            text.append("\n\n")
        except Exception as exc:  # noqa: BLE001
            text.append(f"  {type(exc).__name__}: {exc}", style="red")
            text.append("\n\n")

        # --- Sites -------------------------------------------------- #
        text.append(
            self._translator.translate("main.panel.sites"),
            style="bold underline",
        )
        text.append("\n\n")
        try:
            sites = self._registry.list_sites(
                include_adult=True, include_disabled=True
            )
            unhealthy = sum(
                1
                for site in sites
                if (
                    (snap := self._registry.get_health(site.id))
                    is not None
                    and snap.status.value != "HEALTHY"
                )
            )
            text.append(f"  {len(sites)} registered", style="")
            if unhealthy > 0:
                text.append(
                    f", {unhealthy} unhealthy", style="red"
                )
            text.append("\n")
        except Exception as exc:  # noqa: BLE001
            text.append(f"  {type(exc).__name__}: {exc}", style="red")

        panel.update(text)

    async def _last_library_update(self) -> datetime | None:
        """Return the timestamp of the library's most recent update.

        A small query against the ``mangas`` table for the maximum
        ``updated_at`` value — the same pattern
        :class:`~nexusdl.core.library.search.PaginatedResult` uses for
        its ``last_updated_at`` field. Returned as a timezone-aware
        datetime, or ``None`` when the library has never been written.
        """
        # ``LibraryDatabase`` does not expose a ``last_updated_at``
        # helper directly (its stats API is a private method). The
        # screen computes the value from ``list_mangas``'s most recent
        # item — the ordering ``list_mangas`` uses puts recently-updated
        # mangas first. Using that rather than a bespoke query keeps the
        # screen from depending on database internals.
        latest = await self._database.list_mangas(page=1, per_page=1)
        if not latest.items:
            return None
        # ``Manga.updated_at`` is ``datetime | None`` in the model; a
        # manga just written by the scanner or a download has it set.
        for item in latest.items:
            if item.updated_at is not None:
                return item.updated_at
        return None

    # ------------------------------------------------------------------ #
    # Status bar                                                         #
    # ------------------------------------------------------------------ #

    def _refresh_status_bar(self) -> None:
        """Re-render the status bar: feedback message or shortcut hints."""
        status = self.query_one(f"#{_ID_STATUS}", Label)
        if self._feedback is not None:
            status.update(self._feedback)
            return
        status.update(
            self._translator.translate("main.status.hint")
        )

    def _set_feedback(self, message: str) -> None:
        """Show *message* in the status bar for the linger duration.

        Cancels any pending clear-timer before starting a new one — a
        second feedback message within the linger window must not be
        cleared by the first message's timer.
        """
        self._feedback = message
        self._refresh_status_bar()
        if self._feedback_timer is not None:
            self._feedback_timer.cancel()
        loop = asyncio.get_running_loop()
        self._feedback_timer = loop.call_later(
            _FEEDBACK_LINGER_SECONDS, self._clear_feedback
        )

    def _clear_feedback(self) -> None:
        """Clear the feedback message and reset the status bar."""
        self._feedback = None
        self._feedback_timer = None
        self._refresh_status_bar()

    @work(exclusive=True, group="feedback")
    async def _show_library_summary_feedback(self) -> None:
        """Fetch the library summary and set it as the feedback message."""
        try:
            result = await self._database.list_mangas(page=1, per_page=1)
        except Exception:  # noqa: BLE001 — feedback is best-effort
            return
        self._set_feedback(
            self._translator.translate(
                "main.feedback.library_summary", count=result.total
            )
        )

    # ------------------------------------------------------------------ #
    # Library event handler                                              #
    # ------------------------------------------------------------------ #

    async def _on_library_updated(self, _event: Event) -> None:
        """Refresh the panel in response to a library write."""
        self._refresh_panel()

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    async def _count_active_downloads(self) -> int:
        """Return the number of non-terminal tasks the manager holds.

        Combines the RUNNING and PENDING states — the two states that
        would be interrupted by quitting. ``PAUSED`` is excluded: a
        paused task has already been stopped by the user and does not
        carry the same "in-progress work" weight for the confirmation
        prompt.
        """
        running = await self._manager.list_tasks(
            status=_DOWNLOAD_STATUS_RUNNING
        )
        pending = await self._manager.list_tasks(
            status=_DOWNLOAD_STATUS_PENDING
        )
        return len(running) + len(pending)


# --------------------------------------------------------------------------- #
# Constants used only by the panel and quit flows                              #
# --------------------------------------------------------------------------- #
# Imported at the bottom so the module's top stays readable. The names
# are enumerated here rather than at the top because they only appear in
# two helpers, and a reader scrolling to the helpers sees the values in
# their natural context.
# --------------------------------------------------------------------------- #

from nexusdl.core.models.enums import DownloadStatus as _DownloadStatus  # noqa: E402

_DOWNLOAD_STATUS_RUNNING: Final[_DownloadStatus] = _DownloadStatus.RUNNING
_DOWNLOAD_STATUS_PENDING: Final[_DownloadStatus] = _DownloadStatus.PENDING

# Silence the unused-import warning on ``utcnow`` — it is used by
# ``format_relative`` internally, and imported here for a future
# explicit "as of <now>" line in the panel header that has not yet been
# added. Kept so a future edit does not have to add the import.
_ = utcnow
