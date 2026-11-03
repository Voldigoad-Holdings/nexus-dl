"""Textual screen for searching mangas across every enabled source site.

The search screen is the pipeline's discovery surface: the user types a
query, results stream in from every site that advertises the ``search``
capability, and each result is displayed as a :class:`~nexusdl.interfaces.cli.widgets.manga_card.MangaCard`.
The screen is **the UI's search entry point** — it does not browse a
manga's chapters, does not download anything, and does not persist any
state. When the user activates a card, the screen posts a
:class:`SearchScreen.SearchResultSelected` message and lets the parent
screen decide what to do (typically: fetch the full manga and push the
download screen).

Streaming search
----------------
The screen consumes
:meth:`~nexusdl.core.registry.site_registry.SiteRegistry.search_all`,
which is an **async generator** yielding results in completion order.
The screen runs the generator inside a Textual worker
(``@work(exclusive=True, group="search")``), so:

* A new query submitted while a previous search is in flight cancels
  the previous worker and starts a fresh one — the user gets a fresh
  result set, not a merged one.
* A popped search screen cancels its worker automatically (Textual's
  worker ownership semantics), so no orphaned task outlives the screen.
* The generator's own ``finally`` block cancels and awaits every
  pending per-site search, so a rapid sequence of submissions does not
  leak network activity.

Results are **mounted incrementally** — a card appears as soon as its
:class:`SearchResult` is yielded — so a slow site does not block the
faster ones. The status bar updates the count as results arrive.

Adult-content filter
--------------------
The screen's initial value of the adult-content filter is
``settings.content.allow_adult``. A deployment that has already
disabled adult content starts with the filter on; a deployment that
allows it starts with the filter on **and** the user can turn it off to
search the general catalogue. This is the only "extra" filter the
screen exposes — language, status, and rating filtering belong to a
different screen (the library's advanced search) whose data model is
fully loaded mangas, not search results.

SearchResult → Manga
--------------------
A :class:`~nexusdl.core.models.search.SearchResult` is a **partial
view** of a manga — a listing row, not a fully-fetched entity. It
carries only what a search endpoint reliably provides (title, URL,
source id, cover URL, and optionally author, description, language,
year, rating). It is explicitly **not** a :class:`~nexusdl.core.models.manga.Manga`
— see :mod:`nexusdl.core.models.search` for the rationale.

The :class:`~nexusdl.interfaces.cli.widgets.manga_card.MangaCard`
widget, however, requires a :class:`~nexusdl.core.models.manga.Manga`.
The screen therefore builds a **display stub** from the search result
via :func:`_manga_from_search_result`: it fills the fields
``SearchResult`` provides, uses safe defaults (``UNKNOWN`` language and
status, ``SAFE`` rating) for the rest, and preserves the
deterministic ``manga_id`` (both formulas are
``sha256(f"{site_id}:{source_id}")[:16]`` — they agree by construction).

**The stub is for display only.** When the user activates a card, the
parent screen must fetch the full manga through
:meth:`~nexusdl.core.registry.protocols.ParserProtocol.get_manga` —
the stub's defaults would otherwise leak into the library database as
if they were real values.

Localisation
------------
The screen introduces ten translation keys under ``search.*``:

* ``search.input.placeholder`` — the query input's placeholder.
* ``search.button.clear`` — the clear button's label.
* ``search.button.adult.unchecked`` — the adult filter, unchecked state.
* ``search.button.adult.checked`` — the adult filter, checked state.
* ``search.status.idle`` — the status line before a first search.
* ``search.status.searching`` — status while a search is in flight.
* ``search.status.count_one`` / ``.count_other`` — result count.
* ``search.status.empty`` — the "no results" message.
* ``search.status.error`` — the "search failed" message, with the
  exception text interpolated.

A missing key renders the key verbatim, matching the contract every
other widget and screen in this package follows. See
:mod:`nexusdl.core.i18n`.

Message handler name
--------------------
The parent screen handles the activation message with::

    def on_search_screen_search_result_selected(
        self, event: SearchScreen.SearchResultSelected
    ) -> None:
        result = event.result
        ...

The long handler name is the correct outcome of Textual's nested-
message convention.

Dependency graph (no cycles)::

    nexusdl.core.i18n                       ─┐
    nexusdl.core.models.*                    │
    nexusdl.core.registry.site_registry      ├──► interfaces.cli.screens.search
    nexusdl.core.session.http_session        │
    nexusdl.interfaces.cli.widgets.manga_card│
    textual                                  │

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

from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
from nexusdl.core.models.manga import Manga
from nexusdl.interfaces.cli.widgets.manga_card import MangaCard

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.models.search import SearchResult
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.core.session.http_session import HttpSession


__all__ = ["SearchScreen"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so the CSS below and the
#: ``query_one`` calls in the code cannot drift apart.
_ID_INPUT: Final[str] = "search-input"
_ID_ACTIONS: Final[str] = "search-actions"
_ID_ADULT: Final[str] = "search-adult"
_ID_CLEAR: Final[str] = "search-clear"
_ID_RESULTS: Final[str] = "search-results"
_ID_STATUS: Final[str] = "search-status"


# --------------------------------------------------------------------------- #
# SearchScreen                                                                #
# --------------------------------------------------------------------------- #


class SearchScreen(Screen[None]):
    """A Textual screen for searching mangas across every enabled site.

    See the module docstring for the streaming search contract, the
    adult-content filter behaviour, the display-stub policy, and the
    handler name for the activation message.

    Args:
        registry: The loaded site registry. Its
            :meth:`~nexusdl.core.registry.site_registry.SiteRegistry.search_all`
            method is the sole source of results.
        session: The HTTP session, passed through to ``search_all`` for
            every per-site search.
        settings: The application settings. Only
            ``settings.content.allow_adult`` is read — it seeds the
            initial state of the adult filter.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        name: Optional Textual screen name.
        id: Optional Textual screen id.
        classes: Optional Textual CSS classes.
    """

    #: ``Escape`` and ``q`` pop the screen. The search screen is never
    #: the last screen on the stack — it is pushed by the main screen
    #: (or the download screen) on demand — so the action is always
    #: valid.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
    ]

    #: Default CSS. The application's ``theme.tcss`` may override any
    #: rule by targeting ``SearchScreen`` or the ``#search-*`` ids.
    DEFAULT_CSS: ClassVar[str] = f"""
    SearchScreen {{
        background: $surface;
    }}
    #{_ID_INPUT} {{
        dock: top;
        height: 3;
        margin: 0 1;
    }}
    #{_ID_ACTIONS} {{
        dock: top;
        height: 3;
        padding: 0 1;
    }}
    #{_ID_ACTIONS} Button {{
        margin-right: 1;
    }}
    #{_ID_RESULTS} {{
        height: 1fr;
        padding: 0 1;
    }}
    #{_ID_STATUS} {{
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }}
    """

    # ------------------------------------------------------------------ #
    # Message                                                            #
    # ------------------------------------------------------------------ #

    class SearchResultSelected(Message):
        """Emitted when the user activates a result card.

        Carries the full :class:`~nexusdl.core.models.search.SearchResult`
        — not the display stub the card was built from — so the parent
        screen has the site's URL and identifiers intact and can fetch
        the full manga without a second search. The search result is
        immutable, so the message holds a safe snapshot.
        """

        def __init__(self, result: SearchResult) -> None:
            """Store the selected result on the message.

            Args:
                result: The search result the user activated.
            """
            self.result = result
            super().__init__()

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        *,
        registry: SiteRegistry,
        session: HttpSession,
        settings: Settings,
        translator: Translator | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._registry = registry
        self._session = session
        self._translator = (
            translator if translator is not None else get_default_translator()
        )
        self._include_adult: bool = settings.content.allow_adult
        self._query: str = ""
        #: ``manga_id`` → the :class:`SearchResult` the display stub was
        #: built from. Populated as results stream in; cleared on every
        #: new search. Used by :meth:`on_manga_card_selected` to map an
        #: activation back to the original result.
        self._results_by_manga_id: dict[str, SearchResult] = {}

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the input, the action row, the results container, the status."""
        yield Input(
            placeholder=self._translator.translate(
                "search.input.placeholder"
            ),
            id=_ID_INPUT,
        )
        with Horizontal(id=_ID_ACTIONS):
            yield Button(
                self._adult_label(),
                id=_ID_ADULT,
                variant="warning" if self._include_adult else "default",
            )
            yield Button(
                self._translator.translate("search.button.clear"),
                id=_ID_CLEAR,
                variant="error",
            )
        yield VerticalScroll(id=_ID_RESULTS)
        yield Label(
            self._translator.translate("search.status.idle"),
            id=_ID_STATUS,
        )

    def on_mount(self) -> None:
        """Focus the query input and pre-populate the status line."""
        self.query_one(f"#{_ID_INPUT}", Input).focus()

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Start a search when the user presses Enter in the input.

        A blank or whitespace-only submission is a no-op — the status
        line keeps whatever it was showing (typically the previous
        query's results).
        """
        query = event.value.strip()
        if not query:
            return
        self._start_search(query)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to the corresponding action."""
        if event.button.id == _ID_ADULT:
            self._toggle_adult()
        elif event.button.id == _ID_CLEAR:
            self._clear_all()

    def on_manga_card_selected(self, event: MangaCard.Selected) -> None:
        """Map the activated card back to its search result and post the message.

        The card carries the display stub (a :class:`Manga`); the
        screen resolves it back to the original
        :class:`~nexusdl.core.models.search.SearchResult` — the object
        the parent actually needs — and posts
        :class:`SearchResultSelected` with it.

        A card whose stub id no longer maps to a result is ignored —
        possible only if the results were cleared between the card
        being mounted and the user pressing Enter, which the current
        UI flow does not produce, but which the widget can survive.
        """
        result = self._results_by_manga_id.get(event.manga.id)
        if result is None:
            return
        self.post_message(self.SearchResultSelected(result))

    def action_back(self) -> None:
        """Pop this screen off the stack, returning to the previous one."""
        self.app.pop_screen()

    # ------------------------------------------------------------------ #
    # Search orchestration                                               #
    # ------------------------------------------------------------------ #

    def _start_search(self, query: str) -> None:
        """Clear previous results and start a new search worker.

        Extracted so the initial submission (from ``on_input_submitted``)
        and the adult-filter re-run (from ``_toggle_adult``) share the
        same sequence: reset state, update the status line, then hand
        off to the worker. ``@work(exclusive=True)`` handles canceling
        any previous search.
        """
        self._query = query
        self._clear_results()
        self._set_status(
            self._translator.translate(
                "search.status.searching", query=query
            )
        )
        self._run_search(query)

    @work(exclusive=True, group="search")
    async def _run_search(self, query: str) -> None:
        """Consume ``search_all`` and mount a card for every yielded result.

        Runs on the app's event loop. The ``@work(exclusive=True)``
        decorator cancels a previous worker in the same group before
        starting this one, so a rapid sequence of submissions cannot
        produce two concurrent searches against the same session.

        A ``CancelledError`` propagates untouched — the worker is
        cancelled by Textual's ownership rules when a new search
        starts or the screen is popped, and the generator's own
        ``finally`` block cleans up the in-flight per-site tasks.
        Any other exception is caught and shown in the status line.
        """
        results_container = self.query_one(f"#{_ID_RESULTS}", VerticalScroll)

        try:
            async for result in self._registry.search_all(
                query,
                session=self._session,
                include_adult=self._include_adult,
            ):
                self._mount_result(result, results_container)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced via status line
            self._set_status(
                self._translator.translate(
                    "search.status.error", error=str(exc)
                )
            )
            return

        count = len(self._results_by_manga_id)
        if count == 0:
            self._set_status(
                self._translator.translate(
                    "search.status.empty", query=query
                )
            )
            return

        self._set_status(
            self._translator.plural(
                "search.status.count", count, query=query
            )
        )

    def _mount_result(
        self, result: SearchResult, container: VerticalScroll
    ) -> None:
        """Convert *result* to a display stub, register it, mount its card."""
        manga = _manga_from_search_result(result)
        # Guard against a duplicate id — a site returning the same
        # manga twice across pagination is possible in principle.
        # The card is still mounted (it renders correctly), but the
        # map keeps the latest result; the count and message both
        # remain consistent with what the user sees.
        self._results_by_manga_id[manga.id] = result
        container.mount(MangaCard(manga, translator=self._translator))

    # ------------------------------------------------------------------ #
    # Filter and reset actions                                           #
    # ------------------------------------------------------------------ #

    def _toggle_adult(self) -> None:
        """Flip the adult filter, update the button, re-run the search."""
        self._include_adult = not self._include_adult
        self._update_adult_button()
        if self._query:
            self._start_search(self._query)

    def _clear_all(self) -> None:
        """Clear the input, the results, the query, and the status."""
        self.query_one(f"#{_ID_INPUT}", Input).value = ""
        self._query = ""
        self._clear_results()
        self._set_status(
            self._translator.translate("search.status.idle")
        )

    def _clear_results(self) -> None:
        """Remove every mounted card and reset the id → result map."""
        container = self.query_one(f"#{_ID_RESULTS}", VerticalScroll)
        container.remove_children()
        self._results_by_manga_id.clear()

    # ------------------------------------------------------------------ #
    # Status and button helpers                                          #
    # ------------------------------------------------------------------ #

    def _set_status(self, message: str) -> None:
        """Replace the status line with *message*."""
        self.query_one(f"#{_ID_STATUS}", Label).update(message)

    def _adult_label(self) -> str:
        """Return the adult filter button label for the current state."""
        key = (
            "search.button.adult.checked"
            if self._include_adult
            else "search.button.adult.unchecked"
        )
        return self._translator.translate(key)

    def _update_adult_button(self) -> None:
        """Update the adult filter button's label and variant."""
        button = self.query_one(f"#{_ID_ADULT}", Button)
        button.label = self._adult_label()
        button.variant = "warning" if self._include_adult else "default"


# --------------------------------------------------------------------------- #
# Module-level helpers                                                        #
# --------------------------------------------------------------------------- #


def _manga_from_search_result(result: SearchResult) -> Manga:
    """Build a **display stub** manga from a search result.

    Every field the search result provides is copied; every field it
    does not provide uses a safe default. The stub's :attr:`Manga.id`
    is preserved verbatim (``SearchResult.manga_id`` and
    ``Manga.make_id`` compute the same value from the same inputs), so
    a card's ``Selected.manga.id`` matches the search result's
    ``manga_id`` and the widget can map back to the original result.

    The stub is **for display only** — see the module docstring's
    "SearchResult → Manga" section. A caller that persists the stub
    would silently leak the ``UNKNOWN`` language, ``UNKNOWN`` status,
    and ``SAFE`` rating defaults into the library database.

    Args:
        result: The search result to build a display stub for.

    Returns:
        A :class:`~nexusdl.core.models.manga.Manga` populated with the
        search result's fields and safe defaults elsewhere.
    """
    return Manga(
        id=result.manga_id,
        site_id=result.site_id,
        source_id=result.source_id,
        title=result.title,
        url=result.url,
        description=result.description,
        author=result.author,
        language=result.language or Language.UNKNOWN,
        year=result.year,
        status=result.status or MangaStatus.UNKNOWN,
        content_rating=result.content_rating or ContentRating.SAFE,
        cover_url=result.cover_url,
    )
