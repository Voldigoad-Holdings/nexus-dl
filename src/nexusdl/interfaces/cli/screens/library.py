"""Textual screen for browsing the local library.

The library screen is the pipeline's **collection viewer**: it displays
the mangas the user has already downloaded, supports text search and
pagination, and lets the user activate a manga to inspect its stored
chapters. It is the entry point to a mode most users spend far more time
in than the download flow itself — after a few months of use, "search
and open an existing manga" is a more common action than "download a new
one".

Two data sources, one screen
----------------------------
The library screen renders a
:class:`~nexusdl.core.library.search.PaginatedResult` from either of
:class:`~nexusdl.core.library.database.LibraryDatabase`'s read methods:

* :meth:`~nexusdl.core.library.database.LibraryDatabase.list_mangas`
  — ordered by recency; the "browse" mode.
* :meth:`~nexusdl.core.library.database.LibraryDatabase.search`
  — FTS5-ranked; the "search" mode.

Both return the same `PaginatedResult[Manga]` shape, so the screen has a
single rendering path regardless of which is active. The mode is a
parameter of the fetch — it is not part of the screen's public state.
A user can search from the browse mode and clear the search to return
to it; the underlying query is what changes, not the screen's identity.

Pagination
----------
Two sources of pagination:
1. **Database pagination** — when the result set exceeds
   ``page_size`` items, the database returns only the current page.
   The screen offers "Next" / "Previous" controls, disabled at the
   bounds.
2. **Viewport scrolling** — the cards are mounted inside a
   :class:`~textual.containers.VerticalScroll`, so the user can
   scroll through the current page's items.

The two are orthogonal and deliberately so: a page of 50 items is
larger than any realistic viewport, so viewport scrolling is always
needed; a library with 2 000 mangas needs database pagination on top.
Conflating them (infinite scroll, virtualized list) would require the
screen to know the database's count and lazily fetch rows — a
significantly more complex rendering path for a scenario that is
not the pipeline's primary use case (a personal library rarely
exceeds a few hundred mangas).

Activation semantics
--------------------
A manga card emits :class:`MangaCard.Selected` when activated. The
library screen **re-emits** it as
:class:`LibraryScreen.MangaSelected` with the same payload so the
parent can handle a uniform message name regardless of which screen
the user opened the manga from. This is the same mapping pattern the
search screen uses.

The parent is expected to fetch the manga's chapters from the database
(not from a parser) — the manga is already in the local library.

Refresh
-------
The library is **dynamic**: a download that finishes while the library
screen is open should become visible without requiring the user to
close and reopen the screen. Two refresh sources:

1. **Explicit** — the ``R`` binding triggers a fresh fetch.
2. **Event-driven** — the screen subscribes to
   :data:`~nexusdl.core.constants.EVENT_LIBRARY_UPDATED` on the event
   bus and re-fetches on every emission. The database emits this event
   after every write that changes the library's content, so a finished
   download (which calls ``add_chapter``) triggers a refresh.

The event-driven refresh is debounced (300 ms) — a download pipeline
that adds 20 chapters in rapid succession would otherwise trigger 20
full-page fetches. The debounce timer coalesces bursts into a single
fetch.

Not a download surface
----------------------
The library screen does **not** start downloads, delete mangas, or
modify the library. It is a viewer and a picker. A user who wants to
delete a manga uses a settings action or a direct database operation
(a future "library management" screen); a user who wants to redownload
a manga uses the search-and-download flow. The library screen's job
ends at "the user picked a manga" — the same "does not dispatch"
principle the :mod:`chapter_list` widget follows.

Empty states
------------
Two distinct empty states, distinguished for the same reason the site
selector distinguishes them:

* **Empty library** — ``PaginatedResult.total == 0`` and no active
  search. The screen shows "your library is empty; search or import
  some mangas".
* **Empty search** — an active search returned no results. The screen
  shows "no mangas match <query>; clear the search to return to
  your library".

A user who sees the first empty state knows to import something; a user
who sees the second knows to change the query. Conflating them would
leave the user guessing which action to take.

Localisation
------------
The screen introduces twenty-two keys under ``library.*``:

* ``library.title`` — the header title.
* ``library.search.placeholder`` — the search input's placeholder.
* ``library.button.clear_search`` — the clear-search button label.
* ``library.button.refresh`` — the refresh button label.
* ``library.pagination.prev`` — the "previous" button label.
* ``library.pagination.next`` — the "next" button label.
* ``library.pagination.page`` — ``"page {page} / {total}"``.
* ``library.status.loading`` — before the first fetch completes.
* ``library.status.searching`` — while a search is in flight.
* ``library.status.count_one`` / ``.count_other`` — the item count, with
  ``{count}``.
* ``library.status.search_result`` — the search result count, with
  ``{query}`` and ``{count}``.
* ``library.status.error`` — a fetch failure, with ``{error}``.
* ``library.empty.library`` — the empty-library message.
* ``library.empty.search`` — the empty-search message, with ``{query}``.

A missing key renders the key verbatim, matching the contract every
other widget and screen in this package follows. See
:mod:`nexusdl.core.i18n`.

Dependency graph (no cycles)::

    nexusdl.core.constants                       ─┐
    nexusdl.core.events                           │
    nexusdl.core.i18n                             │
    nexusdl.core.library.database                 ├──► interfaces.cli.screens.library
    nexusdl.core.library.search                   │
    nexusdl.core.models.manga                     │
    nexusdl.interfaces.cli.widgets.manga_card     │
    textual                                       │

The screen imports from :mod:`nexusdl.core` through its public
sub-packages only, and from exactly one sibling widget
(:class:`~nexusdl.interfaces.cli.widgets.manga_card.MangaCard`).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, Final

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Input, Label
from textual.widgets import Static

from nexusdl.core.constants import EVENT_LIBRARY_UPDATED
from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.interfaces.cli.widgets.manga_card import MangaCard

if TYPE_CHECKING:
    from nexusdl.core.events import Event, EventBus
    from nexusdl.core.library.database import LibraryDatabase
    from nexusdl.core.library.search import PaginatedResult
    from nexusdl.core.models.manga import Manga


__all__ = ["LibraryScreen"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so the CSS below and the
#: ``query_one`` calls in the code cannot drift apart.
_ID_TITLE: Final[str] = "library-title"
_ID_SEARCH: Final[str] = "library-search"
_ID_CLEAR_SEARCH: Final[str] = "library-clear-search"
_ID_REFRESH: Final[str] = "library-refresh"
_ID_RESULTS: Final[str] = "library-results"
_ID_PREV: Final[str] = "library-prev"
_ID_NEXT: Final[str] = "library-next"
_ID_PAGE: Final[str] = "library-page"
_ID_STATUS: Final[str] = "library-status"

#: Number of mangas per page. Fifty is a natural fit for a card grid
#: that the user scrolls through — enough to make pagination rare for a
#: personal collection, small enough that a full page renders in well
#: under a second even on a slow terminal.
_PAGE_SIZE: Final[int] = 50

#: Debounce window for the ``library.updated`` event-driven refresh, in
#: seconds. A download that finishes and adds 20 chapters in rapid
#: succession triggers 20 database events; the debounce coalesces them
#: into a single fetch.
_REFRESH_DEBOUNCE_SECONDS: Final[float] = 0.3

#: Minimum query length, in characters, before the search actually
#: fires. A single-character query returns nearly the whole library
#: (every title containing that character), which is not what the user
#: is asking for; two characters is the shortest query that produces a
#: meaningful filtered list.
_MIN_SEARCH_LENGTH: Final[int] = 2


# --------------------------------------------------------------------------- #
# LibraryScreen                                                               #
# --------------------------------------------------------------------------- #


class LibraryScreen(Screen[None]):
    """A Textual screen for browsing the local library.

    See the module docstring for the design contract, the pagination
    model, the activation semantics, and the refresh policy.

    Args:
        database: The library database. Its
            ``list_mangas`` / ``search`` methods are the sole data
            source.
        event_bus: The shared event bus. The screen subscribes to
            :data:`~nexusdl.core.constants.EVENT_LIBRARY_UPDATED` for
            event-driven refreshes.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used.
        name: Optional Textual screen name.
        id: Optional Textual screen id.
        classes: Optional Textual CSS classes.
    """

    #: ``R`` triggers a manual refresh; ``Escape`` / ``q`` pop the
    #: screen. The library screen is never the last one on the stack —
    #: it is pushed by the main screen on demand.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    #: Default CSS.
    DEFAULT_CSS: ClassVar[str] = """
    LibraryScreen {
        background: $surface;
    }
    #library-title {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        text-style: bold;
    }
    #library-search-bar {
        dock: top;
        height: 3;
        padding: 0 1;
    }
    #library-search {
        width: 1fr;
        margin-right: 1;
    }
    #library-clear-search, #library-refresh {
        width: auto;
        min-width: 10;
        margin-right: 1;
    }
    #library-results {
        height: 1fr;
        padding: 0 1;
    }
    #library-pager {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }
    #library-prev, #library-next {
        width: auto;
        min-width: 10;
        margin-right: 1;
    }
    #library-page {
        width: 1fr;
        text-align: center;
        content-align: center middle;
    }
    #library-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    # ------------------------------------------------------------------ #
    # Message                                                            #
    # ------------------------------------------------------------------ #

    class MangaSelected(Message):
        """Emitted when the user activates a manga card.

        Carries the full :class:`~nexusdl.core.models.manga.Manga` the
        card was built from — not its id — so the parent screen can act
        on it (fetch its chapters from the database, display its
        metadata) without a second lookup. The manga model is
        immutable, so the message holds a safe snapshot.
        """

        def __init__(self, manga: Manga) -> None:
            """Store the selected manga on the message.

            Args:
                manga: The manga the user activated.
            """
            self.manga = manga
            super().__init__()

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        *,
        database: LibraryDatabase,
        event_bus: EventBus,
        translator: Translator | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._database = database
        self._event_bus = event_bus
        self._translator = (
            translator if translator is not None else get_default_translator()
        )

        #: Current page number, 1-indexed. Preserved across searches —
        #: a user who paginates to page 3 and then searches sees page 1
        #: of the search results, not page 3 of them (which usually has
        #: no relation to the previous page's content).
        self._page: int = 1

        #: Current page size. Fixed for the screen's lifetime.
        self._page_size: int = _PAGE_SIZE

        #: The active search query, empty when the screen is in browse
        #: mode. Set by :meth:`on_input_submitted` when the input has a
        #: search string, cleared by :meth:`_clear_search`.
        self._query: str = ""

        #: The most recent fetch result, retained so pagination and
        #: re-rendering do not have to re-fetch on every action.
        self._current_result: PaginatedResult[Manga] | None = None

        #: Pending refresh-timer handle. Used to debounce the
        #: ``library.updated`` event handler. ``None`` when no refresh
        #: is pending.
        self._refresh_timer: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the header, search bar, results, pager, and status line."""
        yield Label(
            self._translator.translate("library.title"),
            id=_ID_TITLE,
        )
        with Horizontal(id="library-search-bar"):
            yield Input(
                placeholder=self._translator.translate(
                    "library.search.placeholder"
                ),
                id=_ID_SEARCH,
            )
            yield Button(
                self._translator.translate(
                    "library.button.clear_search"
                ),
                id=_ID_CLEAR_SEARCH,
                variant="default",
            )
            yield Button(
                self._translator.translate("library.button.refresh"),
                id=_ID_REFRESH,
                variant="default",
            )
        yield VerticalScroll(id=_ID_RESULTS)
        with Horizontal(id="library-pager"):
            yield Button(
                self._translator.translate("library.pagination.prev"),
                id=_ID_PREV,
                variant="default",
                disabled=True,
            )
            yield Static("", id=_ID_PAGE)
            yield Button(
                self._translator.translate("library.pagination.next"),
                id=_ID_NEXT,
                variant="default",
                disabled=True,
            )
        yield Label(
            self._translator.translate("library.status.loading"),
            id=_ID_STATUS,
        )

    def on_mount(self) -> None:
        """Subscribe to library events and trigger the first fetch."""
        self._event_bus.subscribe(
            EVENT_LIBRARY_UPDATED, self._on_library_updated
        )
        self._fetch_page()

    def on_unmount(self) -> None:
        """Unsubscribe from library events and cancel any pending refresh."""
        self._event_bus.unsubscribe(
            EVENT_LIBRARY_UPDATED, self._on_library_updated
        )
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
            self._refresh_timer = None

    # ------------------------------------------------------------------ #
    # Fetch                                                              #
    # ------------------------------------------------------------------ #

    def _fetch_page(self) -> None:
        """Kick off a fetch worker for the current query and page."""
        self._run_fetch()

    @work(exclusive=True, group="library-fetch")
    async def _run_fetch(self) -> None:
        """Fetch the current page from the database and render it.

        Delegates to ``search`` when :attr:`_query` is non-empty,
        otherwise ``list_mangas``. Both return the same
        :class:`PaginatedResult` shape, so the rendering path below is
        mode-agnostic.

        A fetch failure is surfaced in the status line; the previous
        results, if any, remain visible so a transient error does not
        wipe what the user was looking at.
        """
        is_search = bool(self._query)
        if is_search:
            self._set_status(
                self._translator.translate("library.status.searching")
            )

        try:
            if is_search:
                result = await self._database.search(
                    self._query,
                    limit=self._page_size,
                    offset=(self._page - 1) * self._page_size,
                )
            else:
                result = await self._database.list_mangas(
                    page=self._page, per_page=self._page_size
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced via status line
            self._set_status(
                self._translator.translate(
                    "library.status.error", error=str(exc)
                )
            )
            return

        self._current_result = result
        self._render_result(result)
        self._update_pager(result)

    def _render_result(self, result: PaginatedResult[Manga]) -> None:
        """Replace the results pane with cards for *result*."""
        container = self.query_one(f"#{_ID_RESULTS}", VerticalScroll)
        container.remove_children()

        if result.total == 0:
            self._set_status(self._empty_message())
            return

        for manga in result.items:
            container.mount(
                MangaCard(manga, translator=self._translator)
            )

        self._set_status(self._result_status(result))

    def _update_pager(self, result: PaginatedResult[Manga]) -> None:
        """Update the pager buttons and the page indicator for *result*."""
        prev_btn = self.query_one(f"#{_ID_PREV}", Button)
        next_btn = self.query_one(f"#{_ID_NEXT}", Button)
        page_label = self.query_one(f"#{_ID_PAGE}", Static)

        prev_btn.disabled = not result.has_prev
        next_btn.disabled = not result.has_next

        if result.total_pages == 0:
            page_label.update("")
        else:
            page_label.update(
                self._translator.translate(
                    "library.pagination.page",
                    page=result.page,
                    total=result.total_pages,
                )
            )

    # ------------------------------------------------------------------ #
    # Rendering helpers                                                  #
    # ------------------------------------------------------------------ #

    def _result_status(self, result: PaginatedResult[Manga]) -> str:
        """Return the status line for a non-empty *result*."""
        if self._query:
            return self._translator.translate(
                "library.status.search_result",
                query=self._query,
                count=result.total,
            )
        return self._translator.plural(
            "library.status.count", result.total
        )

    def _empty_message(self) -> str:
        """Return the appropriate empty-state message.

        Distinguishes "the library has nothing" from "the search
        returned nothing". A user who sees the first message knows to
        import something; a user who sees the second knows to change
        the query.
        """
        if self._query:
            return self._translator.translate(
                "library.empty.search", query=self._query
            )
        return self._translator.translate("library.empty.library")

    def _set_status(self, message: str) -> None:
        """Replace the status line with *message*."""
        self.query_one(f"#{_ID_STATUS}", Label).update(message)

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Start a search when the user presses Enter in the input.

        A blank or too-short submission clears any active search and
        returns to the browse view. This is the standard "empty search
        box means 'everything'" behaviour — a user who deletes the query
        and presses Enter expects the browse view back.
        """
        query = event.value.strip()
        if len(query) < _MIN_SEARCH_LENGTH:
            self._query = ""
        else:
            self._query = query
        self._page = 1
        self._fetch_page()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to the corresponding action."""
        if event.button.id == _ID_CLEAR_SEARCH:
            self._clear_search()
        elif event.button.id == _ID_REFRESH:
            self.action_refresh()
        elif event.button.id == _ID_PREV:
            self._go_prev()
        elif event.button.id == _ID_NEXT:
            self._go_next()

    def on_manga_card_selected(self, event: MangaCard.Selected) -> None:
        """Re-emit the card's selection as a screen-level message.

        The card emits its own message; the screen re-emits a
        uniform :class:`LibraryScreen.MangaSelected` so a parent
        screen does not have to know which widget the user activated.
        This matches the pattern :mod:`search` uses for the same reason.
        """
        self.post_message(self.MangaSelected(event.manga))

    # ------------------------------------------------------------------ #
    # Actions and pagination                                             #
    # ------------------------------------------------------------------ #

    def action_refresh(self) -> None:
        """Re-fetch the current page. Bound to ``R``."""
        self._fetch_page()

    def action_back(self) -> None:
        """Pop this screen off the stack, returning to the previous one."""
        self.app.pop_screen()

    def _clear_search(self) -> None:
        """Clear the input and the query, return to the browse view."""
        self.query_one(f"#{_ID_SEARCH}", Input).value = ""
        self._query = ""
        self._page = 1
        self._fetch_page()

    def _go_prev(self) -> None:
        """Move to the previous page, if one exists."""
        if self._page > 1:
            self._page -= 1
            self._fetch_page()

    def _go_next(self) -> None:
        """Move to the next page, if one exists."""
        if (
            self._current_result is not None
            and self._current_result.has_next
        ):
            self._page += 1
            self._fetch_page()

    # ------------------------------------------------------------------ #
    # Library event handler                                              #
    # ------------------------------------------------------------------ #

    async def _on_library_updated(self, _event: Event) -> None:
        """Refresh the current page in response to a library write.

        Subscribed to :data:`~nexusdl.core.constants.EVENT_LIBRARY_UPDATED`.
        The handler **debounces** via an :class:`asyncio.TimerHandle`
        so a burst of writes (a downloading chapter that adds itself
        to the library, then adds its pages, then updates its
        ``page_count``) coalesces into a single refresh — see the
        module docstring's "Refresh" section.

        The delayed refresh runs through :meth:`action_refresh`, so it
        goes through the same worker machinery as a manual refresh
        (exclusive group, cancellation on re-entry, error surfacing).
        """
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()

        loop = asyncio.get_running_loop()
        self._refresh_timer = loop.call_later(
            _REFRESH_DEBOUNCE_SECONDS, self.action_refresh
        )
