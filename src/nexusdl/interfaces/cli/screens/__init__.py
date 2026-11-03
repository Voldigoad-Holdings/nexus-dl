"""Public API of the NexusDL TUI screens.

The :mod:`nexusdl.interfaces.cli.screens` package is the TUI's
**screen graph** — the collection of full-screen surfaces a user
navigates between while using the terminal interface. Each screen is a
:class:`textual.screen.Screen` subclass; each is composed of widgets
from :mod:`nexusdl.interfaces.cli.widgets` plus Textual's own
primitives; none reimplements what another screen or a widget
provides.

Seven screens, one root
-----------------------
* :class:`~nexusdl.interfaces.cli.screens.main.MainScreen` — the
  **root dashboard**. The first screen the app pushes at startup and
  the one every other screen is pushed from. It surfaces the app's
  state at a glance and is the **single authority for navigation** in
  the TUI (see below).
* :class:`~nexusdl.interfaces.cli.screens.search.SearchScreen` — the
  discovery surface. Types a query, streams results from every enabled
  site, and hands the selected result back to the caller for a full
  fetch.
* :class:`~nexusdl.interfaces.cli.screens.library.LibraryScreen` — the
  collection viewer. Browse and search across the local library,
  paginated, activated by any manga card the user opens.
* :class:`~nexusdl.interfaces.cli.screens.download.DownloadScreen` —
  the execution surface. Selects chapters, picks a packaging format,
  starts a download, and shows the live progress panel.
* :class:`~nexusdl.interfaces.cli.screens.settings.SettingsScreen` — a
  read-only inspector of the loaded :class:`~nexusdl.core.config.Settings`.
* :class:`~nexusdl.interfaces.cli.screens.logs.LogsScreen` — a
  full-screen host for the live log stream.
* :class:`~nexusdl.interfaces.cli.screens.help.HelpScreen` — the
  keyboard-shortcut catalog.

**Navigation is centralised.** ``MainScreen`` is the only file in this
package that imports another screen from the package. Every other
screen is a leaf — it does not push siblings, does not know which
screen comes before or after it, and does not coordinate with the
navigation graph. This is the same "one authority" discipline the rest
of the codebase applies: the ``main.py`` file is the composition root
of the TUI's screen graph, and adding a screen to the graph is a
single-file edit.

Screen composition
------------------
Every screen in this package follows the same contract:

* **Constructor injection.** A screen is constructed with the services
  it needs — the :class:`~nexusdl.core.registry.site_registry.SiteRegistry`,
  the :class:`~nexusdl.core.downloader.manager.DownloadManager`,
  the :class:`~nexusdl.core.library.database.LibraryDatabase`, the
  shared :class:`~nexusdl.core.events.EventBus`, the
  :class:`~nexusdl.core.session.http_session.HttpSession`, and the
  :class:`~nexusdl.core.config.Settings`. A screen never constructs
  its own service.
* **Translator injection.** Every screen accepts an optional
  ``translator: Translator | None`` argument; when ``None``, the
  process-wide default from :func:`~nexusdl.core.i18n.get_default_translator`
  is used. The composition root passes the same translator instance
  through every screen so the whole TUI shares one localisation state.
* **Text-based rendering.** No screen builds a label with
  :meth:`textual.content.Text.from_markup`. Every user-visible string
  is appended literally via :meth:`~textual.content.Text.append`.
  A title from ``sites.yaml``, a manga title from the library, a
  chapter title, or a log message — all render as-is, matching the
  policy every widget in :mod:`nexusdl.interfaces.cli.widgets` follows.
* **CSS override.** Each screen declares a ``DEFAULT_CSS`` that is
  self-sufficient out of the box. A deployment that wants a different
  theme overrides rules by targeting the screen class
  (``MainScreen``) or the screen's stable ``#id`` attributes.

The composition root owns the initial screen push and is the only
place that constructs a ``Screen`` instance for production code.
Every screen in this package is meant to be **pushed**, not
**instantiated and mounted directly** — the lifecycle hooks
(``on_mount``, ``on_unmount``) that wire the screen's subscriptions
and timers are only invoked when a screen is pushed onto the app's
screen stack.

Composition example
-------------------
The composition root wires the app and pushes the main screen at
startup::

    from nexusdl.core.config import Settings
    from nexusdl.core.library.database import LibraryDatabase
    from nexusdl.core.downloader.manager import DownloadManager
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.interfaces.cli.screens import MainScreen

    class NexusDLApp(App[None]):
        def __init__(
            self,
            *,
            settings: Settings,
            registry: SiteRegistry,
            database: LibraryDatabase,
            manager: DownloadManager,
            session: HttpSession,
        ) -> None:
            super().__init__()
            self._settings = settings
            self._registry = registry
            self._database = database
            self._manager = manager
            self._session = session
            # ``event_bus`` and ``log_router`` are also held by the app;
            # see :mod:`nexusdl.core.events` and
            # :mod:`nexusdl.interfaces.cli.screens.logs`.

        def on_mount(self) -> None:
            self.push_screen(MainScreen(
                settings=self._settings,
                registry=self._registry,
                database=self._database,
                manager=self._manager,
                session=self._session,
                event_bus=self._event_bus,
            ))

Import cost
-----------
Importing this package imports every screen in the graph, which imports
every widget each screen uses, which imports :mod:`textual` on the way
through. That cost is paid **once per process** and is bounded by
Textual's own import time — the screens add a small constant on top.

A non-TUI consumer that only needs one screen's type — a test that
asserts on :class:`SearchScreen`'s behaviour without launching the full
app, for example — can import from the specific submodule
(``from nexusdl.interfaces.cli.screens.search import SearchScreen``)
and pay only for that screen's dependencies. In practice every consumer
of this package is the TUI app, which pushes ``MainScreen`` and pulls
in the entire graph through its imports; the submodule path is a test
convenience.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change. The re-exported symbols
fall into two tiers:

* **Tier 1 — the seven screen classes.**
  :class:`~nexusdl.interfaces.cli.screens.main.MainScreen`,
  :class:`~nexusdl.interfaces.cli.screens.search.SearchScreen`,
  :class:`~nexusdl.interfaces.cli.screens.library.LibraryScreen`,
  :class:`~nexusdl.interfaces.cli.screens.download.DownloadScreen`,
  :class:`~nexusdl.interfaces.cli.screens.settings.SettingsScreen`,
  :class:`~nexusdl.interfaces.cli.screens.logs.LogsScreen`,
  :class:`~nexusdl.interfaces.cli.screens.help.HelpScreen`.
  Renaming any of them is a breaking change for the composition root
  and for the tests that exercise the TUI. Constructor signatures may
  grow (a new keyword argument with a default) across minor releases,
  but the existing parameters do not change. The **return type** is
  ``Screen[None]`` for all seven — a screen never returns a value to
  the caller that pushed it; results flow through messages and
  callbacks.
* **Tier 2 — the log-routing protocol.**
  :class:`~nexusdl.interfaces.cli.screens.logs.LogRouter`. A structural
  :class:`typing.Protocol` the composition root implements to route
  log entries to the currently attached viewers. Renaming it is a
  breaking change for the composition root; the method signature and
  the atomic seed-and-attach guarantee documented on
  :meth:`LogRouter.attach_with_seed` are part of the contract.

Every message class a screen emits
(``SearchScreen.SearchResultSelected``,
``LibraryScreen.MangaSelected``, ``MainScreen``'s callbacks, …) is a
**nested** class on the emitting screen and is referenced via the
screen — ``SearchScreen.SearchResultSelected``, not ``SearchResultSelected``.
The nested form is Textual's own convention for widget and screen
messages; renaming a message is also a breaking change, and the handler
name a parent implements is derived from the message name. But the
messages are **not** re-exported at this package's ``__all__`` because
a consumer always references them through the emitting screen, never
through the package.

What is not re-exported
-----------------------
Nothing else in this package is re-exported. Every private helper,
CSS-constant, and module-level function
(``_manga_from_search_result`` in :mod:`search`, ``_join_tags`` in
:mod:`manga_card` — which is a widget, not a screen, but follows the
same rule, ``_CATALOG`` in :mod:`help`, …) is available from its
specific submodule but is not re-exported here. This follows the
same rule the other ``__init__.py`` files in the codebase use: **the
package's public surface is what a composition root and a screen
consumer consume, and neither touches an underscore-prefixed helper.**

Two specific exclusions worth naming explicitly:

* ``help.BindingCategory`` and ``help.BindingInfo`` are declared in
  :mod:`help`'s own ``__all__`` (they are the data model for the help
  catalog). They are **not** re-exported at the package level because
  no external consumer needs them: the help catalog is a private
  constant on the screen's module, and a caller extending it edits
  :data:`help._CATALOG` directly rather than building instances of
  these types.
* ``main._QuitConfirm`` is a modal used by exactly one screen for
  exactly one purpose (confirming a quit with active downloads). It is
  private to :mod:`main` and no consumer outside that module constructs
  it.

Dependency graph (no cycles)::

    nexusdl.core.*                    ─┐
    nexusdl.interfaces.cli.widgets     ├──► interfaces.cli.screens
    textual                            │

``nexusdl.interfaces.cli.screens.main`` is the only module in this
package that imports other modules from the package; the other six are
independent leaves that never import each other. This is the "single
authority for navigation" rule documented above, enforced by the import
graph itself.
"""

from __future__ import annotations

# --- Root dashboard (the app's initial screen) --------------------------- #
from nexusdl.interfaces.cli.screens.main import MainScreen

# --- Destination screens -------------------------------------------------- #
from nexusdl.interfaces.cli.screens.download import DownloadScreen
from nexusdl.interfaces.cli.screens.help import HelpScreen
from nexusdl.interfaces.cli.screens.library import LibraryScreen
from nexusdl.interfaces.cli.screens.logs import LogRouter, LogsScreen
from nexusdl.interfaces.cli.screens.search import SearchScreen
from nexusdl.interfaces.cli.screens.settings import SettingsScreen

__all__ = [
    # --- Root ---------------------------------------------------------- #
    "MainScreen",
    # --- Destinations -------------------------------------------------- #
    "DownloadScreen",
    "HelpScreen",
    "LibraryScreen",
    "LogRouter",
    "LogsScreen",
    "SearchScreen",
    "SettingsScreen",
]
