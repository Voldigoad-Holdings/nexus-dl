"""Library view for the NexusDL GUI.

Presentation + orchestration for the *local library*. Does **not** query any
database, does **not** know about :class:`LibraryDatabase`, SQL, or the
:class:`Manga` domain model. The hosting screen injects:

* ``sort_options`` / ``filter_specs`` / ``batch_actions`` — data-driven
  affordances. Adding a new sort, filter, or batch action is a single
  entry in the composition root.
* ``on_load(query) -> None`` — the host queries its library and calls back
  :meth:`LibraryView.set_page` / :meth:`LibraryView.set_loading` /
  :meth:`LibraryView.set_error`.
* card callbacks (``on_open_manga``, ``on_continue_reading``,
  ``on_right_click_manga``, ``on_cover_needed``) — the host decides what
  "open", "continue", or "delete" mean.

Universal design notes:
    * :class:`LibraryCardData` wraps :class:`MangaCardData` and adds
      library-specific fields (reading progress, timestamps). The card is
      rendered through the untouched :class:`MangaCard` component.
    * Each card lives inside an internal :class:`_LibraryCell` that adds a
      thin reading-progress bar under the card. Reflow grids cells, not
      cards, so :class:`MangaCard` stays generic.
    * Sort / filter / batch affordances are **data-driven**. No ``if/elif``
      branches on their meaning anywhere in the rendering code.
    * Search is debounced (300 ms), layout reflow is debounced (100 ms),
      and callbacks are isolated.
    * The view is race-safe: :attr:`current_query` echoes the latest
      :class:`LibraryQuery`, so the host can drop stale responses.
    * Empty states distinguish *library empty* from *no filter match*.
      The host can force this via :attr:`LibraryPage.library_empty`;
      otherwise a heuristic is used (no query, no filters, zero results).
    * Selection is owned by the view (it needs to render the batch bar) but
      the *meaning* of the selection belongs to the host — the view only
      emits :attr:`on_selection_change` with the selected
      :class:`LibraryCardData` list.
    * ``on_activate`` / ``on_deactivate`` are implemented so
      :class:`MainView` can wire view lifecycle (optional reload).
    * Cleans up after itself on ``<Destroy>``.

Pagination races and load serialisation
---------------------------------------
The host runs its library query asynchronously; the response arrives
some time after :meth:`LibraryView._load` fires :attr:`LibraryQuery`.
While a query is in flight, the user can:
    * click "Next" on the pagination controls,
    * change the sort,
    * click a filter chip,
    * change the search text.
Each of those actions calls :meth:`LibraryView._load` again and overwrites
:attr:`current_query`, leaving the previous query "orphaned" — its response,
when it arrives, refers to a page index that no longer matches the visible
state.

The view therefore ignores every load-triggering action while
``self._loading`` is ``True``. The alternative — queueing the actions and
replaying them once the response arrives — is more complex than the
benefit warrants: the user who clicks "Next" twice in 200 ms expects to
end on page 2, not page 3, and the "ignore while loading" behaviour
produces exactly that outcome.

The loading flag is cleared in :meth:`set_page` and :meth:`set_error`;
both are host callbacks that mark the response as received.

Reflow column bookkeeping
-------------------------
:meth:`LibraryView._reflow` recomputes the number of visible columns on
every resize, and configures the grid accordingly. The previous
implementation updated ``self._current_cols`` *before* iterating the
grid configuration, so the iteration range was the **new** column count
— meaning columns from a wider layout (``weight=1``) were never reset to
``weight=0`` when the window shrank. The grid then kept reserving space
for invisible columns, and the visible cells clustered to the left.

The current implementation tracks the *actually configured* columns in a
set (``self._configured_cols``), and iterates over the union of the old
and new sets so a shrinking window resets the now-unused columns to
``weight=0``.

Card build failures do not blank the page
-----------------------------------------
:meth:`LibraryView._make_cell` builds a :class:`MangaCard` inside a
:class:`_LibraryCell`. If the card construction raises (a malformed
``MangaCardData``, a bad cover size, a host callback that failed at
import time), the previous implementation propagated the exception to
:meth:`_render_cells` and blanked the whole page — one broken card hid
the other 39. The current implementation catches the exception, logs
it, and renders a small ``⚠ <title>`` placeholder inside the cell. The
page still renders; only the broken card is degraded.

Runtime sort-option changes
---------------------------
:meth:`LibraryView.set_sort_options` replaces the sort dropdown's
values. The sort UI is built once in :meth:`_build`; the previous
implementation destroyed the dropdown and the direction button on a
runtime change but never recreated them, so the sort controls simply
disappeared from the layout.

The current implementation updates the existing dropdown in place
(when it was built at construction time). A ``set_sort_options`` on a
view that was constructed **without** any sort options is a no-op
(logged at DEBUG): adding a sort dropdown that did not exist at
construction would require rebuilding the entire top bar, which is out
of scope for a runtime change.
"""
from __future__ import annotations

import gc
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

import customtkinter as ctk
from loguru import logger as _logger

from ..components.manga_card import (
    CardOrientation,
    Labels as MangaCardLabels,
    MangaCard,
    MangaCardData,
)

if TYPE_CHECKING:
    from PIL import Image

_log = _logger.bind(component="gui.library_view")


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

class LibraryLayout(str, Enum):
    """Rendering layout of the library grid."""

    GRID = "grid"
    LIST = "list"


@dataclass(slots=True)
class LibraryCardData:
    """A library entry = a :class:`MangaCardData` projection + library metadata.

    Wrapping (rather than subclassing) keeps :class:`MangaCardData` generic
    and lets this structure evolve independently.
    """

    card: MangaCardData
    reading_progress: float = 0.0       # 0.0 .. 1.0; 0 or 1 = no bar shown
    last_read_at: datetime | None = None
    added_at: datetime | None = None

    @property
    def manga_id(self) -> str:
        return self.card.manga_id


@dataclass(frozen=True, slots=True)
class LibrarySortOption:
    """Description of one sort option shown in the sort dropdown."""

    id: str
    label: str
    default_descending: bool = False


@dataclass(frozen=True, slots=True)
class LibraryFilterSpec:
    """Description of one filter chip.

    The ``id`` is opaque to the view — its *meaning* (e.g. ``"unread"``,
    ``"site:fr"``, ``"lang:ja"``) belongs entirely to the host.
    """

    id: str
    label: str
    tooltip: str = ""


@dataclass(frozen=True, slots=True)
class BatchAction:
    """A batch action available when ≥ 1 card is selected."""

    id: str
    label: str
    callback: Callable[[list[LibraryCardData]], None]
    icon: str = ""
    danger: bool = False


@dataclass(slots=True)
class LibraryQuery:
    """The request emitted by the view when it needs data.

    The host runs the query and calls back :meth:`LibraryView.set_page`.
    """

    query: str = ""
    sort_id: str = ""
    descending: bool = False
    filters: frozenset[str] = field(default_factory=frozenset)
    page: int = 1
    per_page: int = 40


@dataclass(slots=True)
class LibraryPage:
    """The result the host pushes back after :attr:`LibraryQuery`."""

    items: list[LibraryCardData]
    page: int = 1
    total: int | None = None
    has_prev: bool = False
    has_next: bool = False
    library_empty: bool | None = None  # explicit override of the heuristic


@dataclass(frozen=True, slots=True)
class LibraryLabels:
    """All user-visible strings used by :class:`LibraryView`."""

    search_placeholder: str = "Search library…"
    sort_by: str = "Sort by"
    sort_ascending: str = "▲"
    sort_descending: str = "▼"
    layout_grid: str = "▦"
    layout_list: str = "▤"
    reload: str = "Reload"
    loading: str = "Loading…"
    error_prefix: str = "Failed to load:"
    retry: str = "Retry"
    empty_library_hint: str = (
        "Your library is empty. Search for a manga and download it to get started."
    )
    empty_filtered_hint: str = "No manga match the current filters."
    no_results_for_query: str = "No manga match “{query}”."
    results_count: str = "{n} manga"
    results_count_one: str = "1 manga"
    page_of: str = "Page {page} of {total}"
    page_only: str = "Page {page}"
    prev_page: str = "← Prev"
    next_page: str = "Next →"
    selected_count: str = "{n} selected"
    selected_count_one: str = "1 selected"
    select_all: str = "Select all"
    clear_selection: str = "Clear selection"


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_THEME_LIGHT = {
    "bg": "#F5F5F5",
    "fg": "#1A1A1A",
    "muted_fg": "#666666",
    "error_fg": "#C0392B",
    "hint_fg": "#999999",
    "chip_bg": "#EAEAEA",
    "chip_active_bg": "#3B8ED0",
    "chip_active_fg": "#FFFFFF",
    "chip_fg": "#1A1A1A",
    "batch_bg": "#E8F0F9",
    "batch_fg": "#1A1A1A",
    "batch_border": "#B8D0E8",
    "danger_bg": "#C0392B",
    "danger_hover": "#A93226",
    "primary_bg": "#2980B9",
    "primary_hover": "#1F6AA5",
}

_THEME_DARK = {
    "bg": "#1A1A1A",
    "fg": "#DCDCDC",
    "muted_fg": "#909090",
    "error_fg": "#E74C3C",
    "hint_fg": "#666666",
    "chip_bg": "#2E2E2E",
    "chip_active_bg": "#1F6AA5",
    "chip_active_fg": "#FFFFFF",
    "chip_fg": "#DCDCDC",
    "batch_bg": "#1E3A5A",
    "batch_fg": "#DCDCDC",
    "batch_border": "#254A73",
    "danger_bg": "#922B21",
    "danger_hover": "#7B241C",
    "primary_bg": "#1F6AA5",
    "primary_hover": "#1A5488",
}

_SEARCH_DEBOUNCE_MS = 300
_RESIZE_DEBOUNCE_MS = 100
_THEME_POLL_MS = 1000
_CARD_GAP = 8


# ---------------------------------------------------------------------------
# Internal cell widget
# ---------------------------------------------------------------------------

class _LibraryCell(ctk.CTkFrame):
    """Internal wrapper of a MangaCard with a thin reading-progress bar.

    Not exported — internal to :class:`LibraryView`. Reflow grids *cells*,
    not cards, so :class:`MangaCard` stays generic.
    """

    def __init__(
        self,
        master,
        manga_id: str,
        *,
        progress: float = 0.0,
        **kwargs,
    ) -> None:
        kwargs.setdefault("fg_color", "transparent")
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        self.manga_id = manga_id
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._card_holder = ctk.CTkFrame(
            self, fg_color="transparent", corner_radius=0
        )
        self._card_holder.grid(row=0, column=0, sticky="nsew")
        self._card_holder.grid_columnconfigure(0, weight=1)
        self._card_holder.grid_rowconfigure(0, weight=1)

        self._progress_bar: ctk.CTkProgressBar | None = None
        try:
            progress_value = max(0.0, min(1.0, float(progress)))
        except (TypeError, ValueError):
            progress_value = 0.0

        # Only show the bar when the manga is in progress (0 < p < 1).
        if 0.0 < progress_value < 1.0:
            self._progress_bar = ctk.CTkProgressBar(
                self, height=4, corner_radius=0
            )
            self._progress_bar.set(progress_value)
            self._progress_bar.grid(
                row=1, column=0, sticky="ew", pady=(2, 0)
            )

    @property
    def card_holder(self) -> ctk.CTkFrame:
        """The frame the :class:`MangaCard` should be placed in."""
        return self._card_holder

    def refresh_theme(self, theme: dict) -> None:
        """Update the progress bar's colors after a theme change."""
        if self._progress_bar is None:
            return
        try:
            self._progress_bar.configure(
                fg_color=theme.get("chip_bg"),
                progress_color=theme.get("chip_active_bg"),
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

class LibraryView(ctk.CTkFrame):
    """Local library browser with sort, filters, search, and batch actions.

    Parameters:
        master: Parent widget.
        on_load: Required. Called whenever the view needs data (initial
            load, filter change, sort change, search, pagination, retry,
            reload). The host queries its library and calls back
            :meth:`set_page` / :meth:`set_loading` / :meth:`set_error`.
        labels: Localized labels. ``None`` uses English defaults.
        manga_card_labels: Labels forwarded to :class:`MangaCard`.
        sort_options: Data-driven sort options. Empty = no sort dropdown.
        filter_specs: Data-driven filter chips. Empty = no filter row.
        batch_actions: Data-driven batch actions (shown when ≥ 1 card is
            selected). Empty = only *Select all* / *Clear* are shown.
        searchable: Show the search entry (default ``True``).
        selectable: Enable multi-select of cards (default ``True``).
        show_sort: Show the sort dropdown (default ``True``).
        show_filters: Show the filter chips row (default ``True``).
        show_layout_toggle: Show the grid/list toggle (default ``True``).
        show_pagination: Show pagination controls (default ``True``).
        initial_layout: Initial rendering layout.
        initial_sort_id: Id of the sort to select initially. Must be one of
            ``sort_options`` (or empty ``sort_options``).
        initial_sort_descending: Initial sort direction. ``None`` uses the
            option's ``default_descending``.
        initial_filters: Ids of filters active initially.
        per_page: Page size, echoed in :class:`LibraryQuery`.
        card_width: Target card width in ``GRID`` layout.
        cover_size: ``(w, h)`` for covers inside cards.
        keep_results_while_loading: Keep previous results visible while a
            new request is in flight (default ``True``).
        reload_on_activate: Reload automatically when the view is activated
            by :class:`MainView` (default ``False``).
        on_cover_needed: Called once per new card when the cover should be
            loaded. The host calls :meth:`set_cover` when it's ready.
        on_open_manga: Called when the user clicks *View* on a card.
        on_continue_reading: Called on card double-click.
        on_right_click_manga: Called with ``(data, raw_tk_event)`` on
            right-click. The host builds and posts the menu.
        on_download_manga: Called when the user clicks *Download* on a card.
            If ``None`` (default), the button is hidden.
        on_selection_change: Called with the selected
            ``list[LibraryCardData]`` (debounced).
        on_filter_change: Called with the active filter ids whenever the
            user toggles a chip.
        on_sort_change: Called with ``(sort_id, descending)`` whenever the
            user changes the sort.
        on_search_change: Called with the debounced query text.
        on_reload: Called when the user clicks *Reload*.
    """

    def __init__(
        self,
        master,
        *,
        on_load: Callable[[LibraryQuery], None],
        labels: LibraryLabels | None = None,
        manga_card_labels: MangaCardLabels | None = None,
        sort_options: Sequence[LibrarySortOption] = (),
        filter_specs: Sequence[LibraryFilterSpec] = (),
        batch_actions: Sequence[BatchAction] = (),
        searchable: bool = True,
        selectable: bool = True,
        show_sort: bool = True,
        show_filters: bool = True,
        show_layout_toggle: bool = True,
        show_pagination: bool = True,
        initial_layout: LibraryLayout = LibraryLayout.GRID,
        initial_sort_id: str | None = None,
        initial_sort_descending: bool | None = None,
        initial_filters: frozenset[str] = frozenset(),
        per_page: int = 40,
        card_width: int = 180,
        cover_size: tuple[int, int] = (150, 210),
        keep_results_while_loading: bool = True,
        reload_on_activate: bool = False,
        on_cover_needed: Callable[[LibraryCardData], None] | None = None,
        on_open_manga: Callable[[LibraryCardData], None] | None = None,
        on_continue_reading: Callable[[LibraryCardData], None] | None = None,
        on_right_click_manga: Callable[[LibraryCardData, object], None] | None = None,
        on_download_manga: Callable[[LibraryCardData], None] | None = None,
        on_selection_change: Callable[[list[LibraryCardData]], None] | None = None,
        on_filter_change: Callable[[frozenset[str]], None] | None = None,
        on_sort_change: Callable[[str, bool], None] | None = None,
        on_search_change: Callable[[str], None] | None = None,
        on_reload: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        self._labels = labels or LibraryLabels()
        self._manga_card_labels = manga_card_labels
        self._sort_options = list(sort_options)
        self._filter_specs = list(filter_specs)
        self._batch_actions = list(batch_actions)

        self._searchable = searchable
        self._selectable = selectable
        self._show_sort = show_sort and bool(sort_options)
        self._show_filters = show_filters and bool(filter_specs)
        self._show_layout_toggle = show_layout_toggle
        self._show_pagination = show_pagination
        self._layout = initial_layout
        self._per_page = max(1, per_page)
        self._card_width = max(80, card_width)
        self._cover_size = cover_size
        self._keep_results_while_loading = keep_results_while_loading
        self._reload_on_activate = reload_on_activate

        self._on_load_cb = on_load
        self._on_cover_needed_cb = on_cover_needed
        self._on_open_cb = on_open_manga
        self._on_continue_cb = on_continue_reading
        self._on_right_click_cb = on_right_click_manga
        self._on_download_cb = on_download_manga
        self._on_selection_change_cb = on_selection_change
        self._on_filter_change_cb = on_filter_change
        self._on_sort_change_cb = on_sort_change
        self._on_search_change_cb = on_search_change
        self._on_reload_cb = on_reload

        # Sort state.
        self._current_sort_id: str = self._resolve_initial_sort_id(initial_sort_id)
        self._current_sort_descending: bool = self._resolve_initial_sort_desc(
            initial_sort_descending
        )

        # Filter state.
        valid_filter_ids = {s.id for s in self._filter_specs}
        self._active_filter_ids: set[str] = set(initial_filters) & valid_filter_ids

        # Data.
        self._cells: dict[str, _LibraryCell] = {}
        self._cards: dict[str, MangaCard] = {}
        self._library_data: dict[str, LibraryCardData] = {}
        self._selected_ids: set[str] = set()
        self._current_page: LibraryPage | None = None
        self._current_query: LibraryQuery | None = None
        self._loading: bool = False
        self._error: str | None = None
        self._current_cols: int = 0
        #: Columns whose ``weight`` has been set to 1 by a previous
        #: reflow. Tracked explicitly so that a *shrinking* window can
        #: reset the now-unused columns to ``weight=0``.
        self._configured_cols: set[int] = set()

        # Deferred work.
        self._search_after_id: str | None = None
        self._reflow_after_id: str | None = None
        self._theme_after_id: str | None = None
        self._destroyed = False
        self._last_theme_mode: str = self._current_mode()

        self._build()
        self._apply_theme()
        self._render_empty_state()

        self.bind("<Destroy>", self._on_destroy, add="+")
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Public API — state
    # ------------------------------------------------------------------

    @property
    def current_query(self) -> LibraryQuery | None:
        """The most recently emitted :class:`LibraryQuery`, or ``None``."""
        return self._current_query

    def get_selected(self) -> list[LibraryCardData]:
        return [
            self._library_data[mid]
            for mid in self._selected_ids
            if mid in self._library_data
        ]

    def get_search_query(self) -> str:
        return self._search_var.get().strip()

    def get_active_filters(self) -> frozenset[str]:
        return frozenset(self._active_filter_ids)

    def get_sort(self) -> tuple[str, bool]:
        return self._current_sort_id, self._current_sort_descending

    # ------------------------------------------------------------------
    # Public API — host pushes
    # ------------------------------------------------------------------

    def set_loading(self, loading: bool) -> None:
        if self._destroyed:
            return
        self._loading = bool(loading)
        self._render_status_bar()

    def set_error(self, message: str | None) -> None:
        if self._destroyed:
            return
        self._error = message
        if message is not None:
            self._loading = False
        self._render_status_bar()

    def set_page(self, page: LibraryPage) -> None:
        """Replace the current page with ``page``.

        The selection is dropped on every page change — it refers to the
        previous page's items, which are about to be destroyed. The host
        is notified via :attr:`on_selection_change` only when the
        selection was actually non-empty (a spurious event on every page
        change would make the host's "Delete selected" button flicker
        between enabled and disabled states).
        """
        if self._destroyed:
            return
        self._error = None
        self._loading = False
        self._current_page = page

        # New page = new items → drop selection and notify the host.
        had_selection = bool(self._selected_ids)
        self._selected_ids.clear()
        self._refresh_batch_bar()
        if had_selection:
            self._emit_selection_change()

        self._render_cells(page.items)
        self._render_status_bar()
        self._render_pagination()

        if not page.items:
            self._render_empty_state(page)

    def set_cover(self, manga_id: str, image: "Image.Image | None") -> None:
        if self._destroyed:
            return
        card = self._cards.get(manga_id)
        if card is not None:
            card.set_cover(image)

    def set_filter_specs(self, specs: Sequence[LibraryFilterSpec]) -> None:
        """Replace filter chips at runtime; keeps active ones that still exist."""
        if self._destroyed:
            return
        self._filter_specs = list(specs)
        valid = {s.id for s in self._filter_specs}
        self._active_filter_ids &= valid
        self._show_filters = bool(self._filter_specs)
        self._rebuild_filter_chips()

    def set_sort_options(self, options: Sequence[LibrarySortOption]) -> None:
        """Replace the sort options at runtime.

        The sort UI is built once in :meth:`_build`. A runtime change
        updates the existing dropdown in place (values and selection).
        A view constructed **without** sort options has no dropdown to
        update; in that case the call is a no-op (logged at DEBUG) —
        adding a sort UI to an existing view would require rebuilding
        the entire top bar, which is out of scope for a runtime change.
        """
        if self._destroyed:
            return
        self._sort_options = list(options)
        self._show_sort = bool(self._sort_options)
        if self._sort_menu is None:
            if self._sort_options:
                _log.debug(
                    "set_sort_options called but sort UI was not built "
                    "(no sort_options at __init__ time); rebuild the "
                    "view to add sort options"
                )
            return
        self._rebuild_sort_controls()

    def set_batch_actions(self, actions: Sequence[BatchAction]) -> None:
        if self._destroyed:
            return
        self._batch_actions = list(actions)
        self._refresh_batch_bar()

    # ------------------------------------------------------------------
    # Public API — programmatic actions
    # ------------------------------------------------------------------

    def trigger_load(self, page: int = 1) -> None:
        self._load(page=page)

    def focus_search(self) -> None:
        if not self._destroyed and self._searchable and self._search_entry is not None:
            try:
                self._search_entry.focus_set()
            except Exception:
                pass

    def select_all(self) -> None:
        if not self._selectable or self._destroyed:
            return
        for card in self._cards.values():
            card.set_selected(True)
        self._selected_ids = set(self._cards.keys())
        self._refresh_batch_bar()
        self._emit_selection_change()

    def clear_selection(self) -> None:
        if self._destroyed:
            return
        for card in self._cards.values():
            card.set_selected(False)
        self._selected_ids.clear()
        self._refresh_batch_bar()
        self._emit_selection_change()

    # ------------------------------------------------------------------
    # Lifecycle (called by MainView)
    # ------------------------------------------------------------------

    def on_activate(self) -> None:
        if self._reload_on_activate and not self._destroyed:
            page = self._current_query.page if self._current_query else 1
            self._load(page=page)

    def on_deactivate(self) -> None:
        # Nothing to do; kept for symmetry with MainView's duck-typed protocol.
        return

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _resolve_initial_sort_id(self, requested: str | None) -> str:
        if not self._sort_options:
            return requested or ""
        ids = {o.id for o in self._sort_options}
        if requested and requested in ids:
            return requested
        return self._sort_options[0].id

    def _resolve_initial_sort_desc(self, requested: bool | None) -> bool:
        if requested is not None:
            return requested
        for opt in self._sort_options:
            if opt.id == self._current_sort_id:
                return opt.default_descending
        return False

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # --- Top bar: search + sort + direction + layout toggle + reload ---
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        top.grid_columnconfigure(0, weight=1)
        self._top_bar = top

        self._search_var = ctk.StringVar(master=self, value="")
        if self._searchable:
            self._search_entry = ctk.CTkEntry(
                top,
                placeholder_text=self._labels.search_placeholder,
                textvariable=self._search_var,
            )
            self._search_entry.grid(row=0, column=0, sticky="ew")
            self._search_entry.bind("<KeyRelease>", self._on_search_key)
            self._search_entry.bind("<Return>", self._on_search_enter)
        else:
            self._search_entry = None

        self._sort_menu: ctk.CTkOptionMenu | None = None
        self._sort_dir_btn: ctk.CTkButton | None = None
        if self._show_sort:
            self._sort_menu = ctk.CTkOptionMenu(
                top,
                values=self._sort_labels(),
                command=self._on_sort_select,
                width=150,
            )
            self._sort_menu.grid(row=0, column=1, padx=(8, 0))
            self._sort_menu.set(self._current_sort_label())

            self._sort_dir_btn = ctk.CTkButton(
                top,
                text=self._labels.sort_descending
                if self._current_sort_descending
                else self._labels.sort_ascending,
                width=36,
                command=self._on_sort_direction_click,
            )
            self._sort_dir_btn.grid(row=0, column=2, padx=(4, 0))

        if self._show_layout_toggle:
            self._layout_btn = ctk.CTkButton(
                top,
                text=self._layout_button_text(),
                width=36,
                command=self._on_layout_toggle,
            )
            self._layout_btn.grid(row=0, column=3, padx=(8, 0))
        else:
            self._layout_btn = None

        if self._on_reload_cb is not None:
            self._reload_btn = ctk.CTkButton(
                top,
                text=self._labels.reload,
                width=90,
                command=self._on_reload_click,
            )
            self._reload_btn.grid(row=0, column=4, padx=(8, 0))
        else:
            self._reload_btn = None

        # --- Filter chips ---
        self._filters_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._filters_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        self._rebuild_filter_chips()
        if not self._show_filters:
            self._filters_frame.grid_remove()

        # --- Batch bar ---
        self._batch_bar = ctk.CTkFrame(self, corner_radius=6, border_width=1)
        self._batch_bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        self._batch_bar.grid_columnconfigure(1, weight=1)

        self._batch_label = ctk.CTkLabel(
            self._batch_bar, text="", anchor="w", font=("", 12, "bold")
        )
        self._batch_label.grid(row=0, column=0, sticky="w", padx=(10, 8), pady=6)

        self._batch_actions_frame = ctk.CTkFrame(
            self._batch_bar, fg_color="transparent"
        )
        self._batch_actions_frame.grid(row=0, column=1, sticky="e", padx=(0, 10))

        self._select_all_btn = ctk.CTkButton(
            self._batch_actions_frame,
            text=self._labels.select_all,
            width=100,
            height=26,
            fg_color="transparent",
            border_width=1,
            command=self.select_all,
        )
        self._select_all_btn.pack(side="left", padx=(0, 6))

        self._clear_selection_btn = ctk.CTkButton(
            self._batch_actions_frame,
            text=self._labels.clear_selection,
            width=120,
            height=26,
            fg_color="transparent",
            border_width=1,
            command=self.clear_selection,
        )
        self._clear_selection_btn.pack(side="left", padx=(0, 6))

        self._batch_custom_buttons: list[ctk.CTkButton] = []
        self._rebuild_batch_custom_buttons()

        self._batch_bar.grid_remove()  # hidden by default

        # --- Status bar ---
        self._status_bar = ctk.CTkFrame(self, fg_color="transparent")
        self._status_bar.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 4))
        self._status_bar.grid_columnconfigure(0, weight=1)

        self._status_label = ctk.CTkLabel(
            self._status_bar, text="", anchor="w",
            text_color=self._theme()["muted_fg"],
        )
        self._status_label.grid(row=0, column=0, sticky="w")

        self._retry_btn = ctk.CTkButton(
            self._status_bar,
            text=self._labels.retry,
            width=80,
            height=24,
            command=self._on_retry_click,
        )

        # --- Results scroll ---
        self._results_scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent"
        )
        self._results_scroll.grid(row=4, column=0, sticky="nsew", padx=6, pady=2)
        self._results_scroll.bind("<Configure>", self._on_results_configure)

        self._empty_frame = ctk.CTkFrame(
            self._results_scroll, fg_color="transparent"
        )
        self._empty_label = ctk.CTkLabel(
            self._empty_frame,
            text="",
            anchor="center",
            justify="center",
            text_color=self._theme()["hint_fg"],
            font=("", 12),
        )
        self._empty_label.pack(pady=40)

        # --- Pagination ---
        self._pagination = ctk.CTkFrame(self, fg_color="transparent")
        self._pagination.grid(row=5, column=0, sticky="ew", padx=10, pady=(4, 10))
        self._pagination.grid_columnconfigure(1, weight=1)

        self._prev_btn = ctk.CTkButton(
            self._pagination,
            text=self._labels.prev_page,
            width=90,
            command=lambda: self._on_page_nav(-1),
        )
        self._prev_btn.grid(row=0, column=0, sticky="w")

        self._page_label = ctk.CTkLabel(
            self._pagination, text="", anchor="center",
            text_color=self._theme()["muted_fg"],
        )
        self._page_label.grid(row=0, column=1)

        self._next_btn = ctk.CTkButton(
            self._pagination,
            text=self._labels.next_page,
            width=90,
            command=lambda: self._on_page_nav(+1),
        )
        self._next_btn.grid(row=0, column=2, sticky="e")

        self._pagination.grid_remove()

    def _rebuild_filter_chips(self) -> None:
        for child in self._filters_frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        theme = self._theme()
        for spec in self._filter_specs:
            active = spec.id in self._active_filter_ids
            btn = ctk.CTkButton(
                self._filters_frame,
                text=spec.label,
                height=26,
                corner_radius=13,
                fg_color=theme["chip_active_bg"] if active else theme["chip_bg"],
                hover_color=theme["chip_active_bg"] if active else theme["muted_fg"],
                text_color=theme["chip_active_fg"] if active else theme["chip_fg"],
                command=lambda sid=spec.id: self._on_chip_click(sid),
            )
            btn.pack(side="left", padx=(0, 6))
            if spec.tooltip:
                try:
                    btn._nexus_tooltip = spec.tooltip  # type: ignore[attr-defined]
                except Exception:
                    pass

        if self._show_filters:
            self._filters_frame.grid()
        else:
            self._filters_frame.grid_remove()

    def _rebuild_sort_controls(self) -> None:
        """Update the sort dropdown's values without destroying the widget.

        The previous implementation destroyed the sort menu and the sort
        direction button but never recreated them, so a runtime
        ``set_sort_options`` call made the sort UI disappear from the
        layout entirely. The current implementation updates the existing
        widgets in place — which is what the caller expects from a
        "replace the options" API.
        """
        if self._sort_menu is None:
            return
        try:
            self._sort_menu.configure(values=self._sort_labels())
        except Exception:
            _log.exception("Failed to update sort menu values")
            return

        # If the currently selected id is not in the new options, fall
        # back to the first option (or empty).
        valid_ids = {o.id for o in self._sort_options}
        if self._current_sort_id not in valid_ids:
            self._current_sort_id = (
                self._sort_options[0].id if self._sort_options else ""
            )
            self._current_sort_descending = self._resolve_initial_sort_desc(
                None
            )
        try:
            self._sort_menu.set(self._current_sort_label())
        except Exception:
            pass
        self._refresh_sort_direction_button()

    def _rebuild_batch_custom_buttons(self) -> None:
        for btn in self._batch_custom_buttons:
            try:
                btn.destroy()
            except Exception:
                pass
        self._batch_custom_buttons.clear()

        theme = self._theme()
        for action in self._batch_actions:
            text = f"{action.icon}  {action.label}".strip() if action.icon else action.label
            kwargs: dict = {
                "text": text,
                "width": 110,
                "height": 26,
                "command": self._make_batch_handler(action),
            }
            if action.danger:
                kwargs["fg_color"] = theme["danger_bg"]
                kwargs["hover_color"] = theme["danger_hover"]
            btn = ctk.CTkButton(self._batch_actions_frame, **kwargs)
            btn.pack(side="left", padx=(0, 6))
            self._batch_custom_buttons.append(btn)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_status_bar(self) -> None:
        theme = self._theme()
        if self._error:
            self._status_label.configure(
                text=f"{self._labels.error_prefix} {self._error}",
                text_color=theme["error_fg"],
            )
            self._retry_btn.grid(row=0, column=1, sticky="e")
            return

        self._retry_btn.grid_remove()

        if self._loading:
            self._status_label.configure(
                text=self._labels.loading, text_color=theme["muted_fg"]
            )
            return

        page = self._current_page
        if page is not None and page.items:
            n = page.total if page.total is not None else len(page.items)
            text = (
                self._labels.results_count_one
                if n == 1
                else self._labels.results_count.format(n=n)
            )
            self._status_label.configure(text=text, text_color=theme["muted_fg"])
        else:
            self._status_label.configure(text="", text_color=theme["muted_fg"])

    def _render_pagination(self) -> None:
        if not self._show_pagination or self._current_page is None:
            self._pagination.grid_remove()
            return
        page = self._current_page
        if not page.has_prev and not page.has_next:
            self._pagination.grid_remove()
            return
        try:
            self._prev_btn.configure(
                state="normal" if page.has_prev else "disabled"
            )
            self._next_btn.configure(
                state="normal" if page.has_next else "disabled"
            )
        except Exception:
            pass

        if page.total is not None and page.total > 0:
            total_pages = max(1, (page.total + self._per_page - 1) // self._per_page)
            text = self._labels.page_of.format(page=page.page, total=total_pages)
        else:
            text = self._labels.page_only.format(page=page.page)
        self._page_label.configure(text=text)
        self._pagination.grid()

    def _render_empty_state(self, page: LibraryPage | None = None) -> None:
        if page is None:
            self._empty_label.configure(text=self._labels.empty_library_hint)
        else:
            if page.library_empty is True:
                self._empty_label.configure(text=self._labels.empty_library_hint)
            elif page.library_empty is False:
                self._empty_label.configure(
                    text=self._empty_message_for_filters()
                )
            else:
                # Heuristic: if no query, no filters, zero total → library empty.
                q = self._current_query
                total = page.total if page.total is not None else 0
                if (
                    q is not None
                    and total == 0
                    and not q.query
                    and not q.filters
                ):
                    self._empty_label.configure(text=self._labels.empty_library_hint)
                else:
                    self._empty_label.configure(
                        text=self._empty_message_for_filters()
                    )
        self._show_empty_overlay()

    def _empty_message_for_filters(self) -> str:
        q = self._current_query
        if q is not None and q.query:
            return self._labels.no_results_for_query.format(query=q.query)
        return self._labels.empty_filtered_hint

    def _show_empty_overlay(self) -> None:
        self._empty_frame.pack(fill="both", expand=True)
        for cell in self._cells.values():
            try:
                cell.grid_forget()
            except Exception:
                pass

    def _hide_empty_overlay(self) -> None:
        try:
            self._empty_frame.pack_forget()
        except Exception:
            pass

    def _render_cells(self, items: list[LibraryCardData]) -> None:
        # Tear down the previous cells. ``destroy()`` releases the Tk
        # widget but the ``CTkImage`` held by each card is only
        # reclaimed by the Python GC — a paginated browse of 10 pages ×
        # 40 cards can retain 400 covers (~400 MB) until a collection
        # cycle. ``gc.collect()`` is cheap and forces the release
        # immediately.
        for cell in self._cells.values():
            try:
                cell.destroy()
            except Exception:
                pass
        self._cells.clear()
        self._cards.clear()
        self._library_data.clear()
        gc.collect()

        if not items:
            return

        self._hide_empty_overlay()

        for data in items:
            self._library_data[data.manga_id] = data
            cell = self._make_cell(data)
            self._cells[data.manga_id] = cell

            card = cell_card(cell)
            if card is not None:
                self._cards[data.manga_id] = card
                # Ask the host for the cover exactly once.
                if self._on_cover_needed_cb is not None and data.card.cover_url:
                    card.set_cover_loading(True)
                    self._safe_call(self._on_cover_needed_cb, data)

        self._request_reflow(force=True)

    def _make_cell(self, data: LibraryCardData) -> _LibraryCell:
        """Build the cell and its :class:`MangaCard`.

        A card that raises during construction (a malformed
        :class:`MangaCardData`, a bad cover size, a host callback that
        failed at import time) does not blank the whole page: the cell
        is populated with a small ``⚠ <title>`` placeholder instead.
        The other cards on the page render normally.
        """
        cell = _LibraryCell(
            self._results_scroll,
            data.manga_id,
            progress=data.reading_progress,
        )
        try:
            card = MangaCard(
                cell.card_holder,
                data.card,
                labels=self._manga_card_labels,
                on_download=(
                    self._translate_card_cb(self._on_download_cb)
                    if self._on_download_cb is not None
                    else None
                ),
                on_view=(
                    self._translate_card_cb(self._on_open_cb)
                    if self._on_open_cb is not None
                    else None
                ),
                on_double_click=(
                    self._translate_card_cb(self._on_continue_cb)
                    if self._on_continue_cb is not None
                    else None
                ),
                on_right_click=(
                    self._translate_card_right_click_cb(self._on_right_click_cb)
                    if self._on_right_click_cb is not None
                    else None
                ),
                on_select=(
                    self._handle_card_select if self._selectable else None
                ),
                selectable=self._selectable,
                selected=data.manga_id in self._selected_ids,
                orientation=(
                    CardOrientation.HORIZONTAL
                    if self._layout == LibraryLayout.LIST
                    else CardOrientation.VERTICAL
                ),
                cover_size=self._cover_size,
                show_author=True,
                show_status=True,
                show_site=True,
                show_chapter_count=True,
                show_actions=True,
            )
            card.grid(row=0, column=0, sticky="nsew")
            cell._card = card  # type: ignore[attr-defined]
        except Exception:
            _log.exception(
                "LibraryView: failed to build card for {!r}",
                data.manga_id,
            )
            placeholder = ctk.CTkLabel(
                cell.card_holder,
                text=f"⚠ {data.card.title}",
                anchor="center",
                text_color="#E74C3C",
            )
            placeholder.grid(row=0, column=0, sticky="nsew")
            cell._card = None  # type: ignore[attr-defined]
        return cell

    def _translate_card_cb(
        self, cb: Callable[[LibraryCardData], None]
    ) -> Callable[[MangaCardData], None]:
        def _wrapped(card_data: MangaCardData) -> None:
            lib = self._library_data.get(card_data.manga_id)
            if lib is not None:
                self._safe_call(cb, lib)
        return _wrapped

    def _translate_card_right_click_cb(
        self, cb: Callable[[LibraryCardData, object], None]
    ) -> Callable[[MangaCardData, object], None]:
        def _wrapped(card_data: MangaCardData, event) -> None:
            lib = self._library_data.get(card_data.manga_id)
            if lib is not None:
                self._safe_call(cb, lib, event)
        return _wrapped

    def _handle_card_select(self, card_data: MangaCardData, selected: bool) -> None:
        if selected:
            self._selected_ids.add(card_data.manga_id)
        else:
            self._selected_ids.discard(card_data.manga_id)
        self._refresh_batch_bar()
        self._emit_selection_change()

    def _refresh_batch_bar(self) -> None:
        if self._destroyed:
            return
        n = len(self._selected_ids)
        if n == 0 or not self._selectable:
            self._batch_bar.grid_remove()
            return

        text = (
            self._labels.selected_count_one
            if n == 1
            else self._labels.selected_count.format(n=n)
        )
        self._batch_label.configure(text=text)
        theme = self._theme()
        try:
            self._batch_bar.configure(
                fg_color=theme["batch_bg"],
                border_color=theme["batch_border"],
            )
            self._batch_label.configure(text_color=theme["batch_fg"])
        except Exception:
            pass
        self._batch_bar.grid()

    def _emit_selection_change(self) -> None:
        if self._on_selection_change_cb is not None:
            self._safe_call(self._on_selection_change_cb, self.get_selected())

    # ------------------------------------------------------------------
    # Sort
    # ------------------------------------------------------------------

    def _sort_labels(self) -> list[str]:
        return [o.label for o in self._sort_options]

    def _current_sort_label(self) -> str:
        for opt in self._sort_options:
            if opt.id == self._current_sort_id:
                return opt.label
        return self._sort_options[0].label if self._sort_options else ""

    def _on_sort_select(self, label: str) -> None:
        if self._destroyed or self._loading:
            # A load is already in flight — ignore the action to avoid
            # racing a stale page index against a pending fetch.
            return
        for opt in self._sort_options:
            if opt.label == label:
                self._current_sort_id = opt.id
                self._current_sort_descending = opt.default_descending
                break
        self._refresh_sort_direction_button()
        if self._on_sort_change_cb is not None:
            self._safe_call(
                self._on_sort_change_cb,
                self._current_sort_id,
                self._current_sort_descending,
            )
        self._load(page=1)

    def _on_sort_direction_click(self) -> None:
        if self._destroyed or self._loading:
            return
        self._current_sort_descending = not self._current_sort_descending
        self._refresh_sort_direction_button()
        if self._on_sort_change_cb is not None:
            self._safe_call(
                self._on_sort_change_cb,
                self._current_sort_id,
                self._current_sort_descending,
            )
        self._load(page=1)

    def _refresh_sort_direction_button(self) -> None:
        if self._sort_dir_btn is None:
            return
        try:
            self._sort_dir_btn.configure(
                text=self._labels.sort_descending
                if self._current_sort_descending
                else self._labels.sort_ascending
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------

    def _on_chip_click(self, filter_id: str) -> None:
        if self._destroyed or self._loading:
            return
        if filter_id in self._active_filter_ids:
            self._active_filter_ids.discard(filter_id)
        else:
            self._active_filter_ids.add(filter_id)
        self._rebuild_filter_chips()
        if self._on_filter_change_cb is not None:
            self._safe_call(
                self._on_filter_change_cb, self.get_active_filters()
            )
        self._load(page=1)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search_key(self, _event=None) -> None:
        if self._destroyed:
            return
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.after(
            _SEARCH_DEBOUNCE_MS, self._commit_search
        )

    def _on_search_enter(self, _event=None) -> str:
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except Exception:
                pass
            self._search_after_id = None
        self._commit_search()
        return "break"

    def _commit_search(self) -> None:
        self._search_after_id = None
        if self._destroyed or self._loading:
            return
        query = self.get_search_query()
        if self._on_search_change_cb is not None:
            self._safe_call(self._on_search_change_cb, query)
        self._load(page=1)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self, *, page: int) -> None:
        if self._destroyed:
            return
        if page < 1:
            page = 1

        query = LibraryQuery(
            query=self.get_search_query(),
            sort_id=self._current_sort_id,
            descending=self._current_sort_descending,
            filters=frozenset(self._active_filter_ids),
            page=page,
            per_page=self._per_page,
        )
        self._current_query = query
        self._error = None

        if not self._keep_results_while_loading:
            self._render_cells([])
            self._show_empty_overlay()  # neutral in-between state

        self._loading = True
        self._render_status_bar()

        self._safe_call(self._on_load_cb, query)

    def _on_page_nav(self, delta: int) -> None:
        if self._destroyed:
            return
        if self._current_page is None or self._current_query is None:
            return
        if self._loading:
            # A load is already in flight — ignore the click to avoid
            # racing a stale page index against a pending fetch.
            return
        new_page = self._current_page.page + delta
        if new_page < 1:
            return
        self._load(page=new_page)

    def _on_retry_click(self) -> None:
        page = self._current_query.page if self._current_query else 1
        self._load(page=page)

    def _on_reload_click(self) -> None:
        if self._on_reload_cb is not None:
            self._safe_call(self._on_reload_cb)
        page = self._current_query.page if self._current_query else 1
        self._load(page=page)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _layout_button_text(self) -> str:
        return (
            self._labels.layout_list
            if self._layout == LibraryLayout.GRID
            else self._labels.layout_grid
        )

    def _on_layout_toggle(self) -> None:
        self._layout = (
            LibraryLayout.LIST
            if self._layout == LibraryLayout.GRID
            else LibraryLayout.GRID
        )
        try:
            self._layout_btn.configure(text=self._layout_button_text())
        except Exception:
            pass
        self._rebuild_cells_for_layout()

    def _rebuild_cells_for_layout(self) -> None:
        if not self._cells:
            return
        # Recreate cards to switch orientation (MangaCard orientation is
        # fixed at construction).
        items = list(self._library_data.values())
        for cell in self._cells.values():
            try:
                cell.destroy()
            except Exception:
                pass
        self._cells.clear()
        self._cards.clear()

        self._current_cols = 0
        self._configured_cols.clear()
        self._render_cells(items)

    def _on_results_configure(self, _event=None) -> None:
        self._request_reflow()

    def _request_reflow(self, *, force: bool = False) -> None:
        if self._destroyed:
            return
        if self._reflow_after_id is not None:
            try:
                self.after_cancel(self._reflow_after_id)
            except Exception:
                pass
        delay = 0 if force else _RESIZE_DEBOUNCE_MS
        self._reflow_after_id = self.after(delay, self._reflow)

    def _reflow(self) -> None:
        self._reflow_after_id = None
        if self._destroyed or not self._cells:
            return

        available = self._results_scroll.winfo_width()
        if available < self._card_width // 2:
            return

        if self._layout == LibraryLayout.LIST:
            new_cols = 1
        else:
            new_cols = max(1, available // (self._card_width + _CARD_GAP))

        if new_cols == self._current_cols:
            return
        self._current_cols = new_cols

        # Configure the columns that are now visible (weight=1) and the
        # ones that were visible before but are not anymore (weight=0).
        # The union is what makes a shrinking window reset the stale
        # columns — a plain ``range(new_cols)`` would leave them at
        # ``weight=1`` and the grid would keep reserving space for them.
        columns_to_update = set(range(new_cols)) | self._configured_cols
        for col in columns_to_update:
            try:
                self._results_scroll.grid_columnconfigure(
                    col,
                    weight=1 if col < new_cols else 0,
                    uniform="cell" if self._layout == LibraryLayout.GRID else "",
                )
            except Exception:
                pass

        self._configured_cols = set(range(new_cols))

        for cell in self._cells.values():
            try:
                cell.grid_forget()
            except Exception:
                pass

        for idx, cell in enumerate(self._cells.values()):
            row, col = divmod(idx, new_cols)
            try:
                cell.grid(
                    row=row, column=col,
                    padx=_CARD_GAP // 2, pady=_CARD_GAP // 2,
                    sticky="nsew",
                )
            except Exception:
                _log.exception("LibraryView: failed to grid cell")

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------

    def _current_mode(self) -> str:
        try:
            return ctk.get_appearance_mode()
        except Exception:
            return "Dark"

    def _theme(self) -> dict:
        return _THEME_LIGHT if self._last_theme_mode == "Light" else _THEME_DARK

    def _apply_theme(self) -> None:
        if self._destroyed:
            return
        theme = self._theme()
        try:
            self.configure(fg_color=theme["bg"])
        except Exception:
            pass
        try:
            self._results_scroll.configure(fg_color="transparent")
        except Exception:
            pass
        self._rebuild_filter_chips()
        self._render_status_bar()
        self._render_pagination()
        self._refresh_batch_bar()

        # Update the reading-progress bars on the cells, since CTk does
        # not auto-theme them.
        for cell in self._cells.values():
            try:
                cell.refresh_theme(theme)
            except Exception:
                pass

    def _schedule_theme_check(self) -> None:
        if self._destroyed:
            return
        self._theme_after_id = self.after(_THEME_POLL_MS, self._check_theme)

    def _check_theme(self) -> None:
        self._theme_after_id = None
        if self._destroyed:
            return
        try:
            current = self._current_mode()
            if current != self._last_theme_mode:
                self._last_theme_mode = current
                self._apply_theme()
        finally:
            # Always reschedule — a single failure in ``_apply_theme``
            # must not stop the polling loop for the widget's lifetime.
            self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Batch actions
    # ------------------------------------------------------------------

    def _make_batch_handler(self, action: BatchAction):
        def _handler() -> None:
            selected = self.get_selected()
            if not selected:
                return
            self._safe_call(action.callback, selected)
        return _handler

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_call(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:
            _log.exception(
                "LibraryView: callback {!r} raised",
                getattr(cb, "__name__", cb),
            )

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        if self._destroyed:
            return
        self._destroyed = True
        for attr in (
            "_search_after_id",
            "_reflow_after_id",
            "_theme_after_id",
        ):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Private helper (kept at module scope for clarity)
# ---------------------------------------------------------------------------

def cell_card(cell: _LibraryCell) -> MangaCard | None:
    """Return the :class:`MangaCard` stored on a :class:`_LibraryCell`.

    Returns ``None`` when the card failed to build and the cell holds a
    placeholder instead. Callers must guard against ``None``.
    """
    return getattr(cell, "_card", None)
