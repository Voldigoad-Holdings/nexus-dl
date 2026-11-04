"""Search view for the NexusDL GUI.

Presentation + orchestration only. Does **not** run searches, does **not**
load covers, does **not** know about :class:`EventBus`, :class:`SiteRegistry`,
:class:`HttpSession` or :class:`Settings`. The hosting screen injects:

* an already-authorized list of :class:`SiteConfig` (adult/language/disabled
  filtering is the host's job),
* ``on_search(request)`` — the host runs the async search and calls back
  :meth:`SearchView.set_results` / :meth:`SearchView.set_loading` /
  :meth:`SearchView.set_error`,
* ``on_cover_needed(manga)`` — the host runs the async fetch and calls back
  :meth:`SearchView.set_cover`.

Because the search backend is a callback, the same view works with
``registry.search_all``, a single-parser search, an external API, or a mock
— without any change here.

Universal design notes:
    * Responsive reflow on ``<Configure>`` (debounced 100 ms). Two layouts
      are supported: ``GRID`` (N columns) and ``LIST`` (one card per row).
      Reflow is skipped when the computed column count did not change.
    * The view is race-safe: :attr:`current_request` echoes the latest
      request, so the host can ignore obsolete responses by comparing
      ``page.query`` with ``current_request.query``.
    * ``keep_results_while_loading=True`` (default) keeps the previous
      results visible while a new search is in flight, so the grid does not
      blink between pages.
    * Pagination is hidden automatically when there is only one page.
    * ``show_site_filter=False`` turns this into a single-site search view.
    * Missing covers are the host's responsibility — the view only displays
      what it is given, and calls ``on_cover_needed`` once per new card.
    * ``selectable=True`` enables multi-select of cards; the resulting list
      of :class:`MangaCardData` is emitted via ``on_selection_change``.
    * Every string comes from an injectable :class:`SearchLabels`.
    * All callbacks are isolated — a misbehaving host never breaks Tk.
    * Cleans up after itself on ``<Destroy>``.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import customtkinter as ctk
from loguru import logger as _logger

from nexusdl.core.models.site import SiteConfig

from ..components.manga_card import (
    CardOrientation,
    Labels as MangaCardLabels,
    MangaCard,
    MangaCardData,
)
from ..components.site_dropdown import SiteDropdown

if TYPE_CHECKING:
    from PIL import Image

_log = _logger.bind(component="gui.search_view")


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

class SearchLayout(str, Enum):
    """Rendering layout of the results grid."""

    GRID = "grid"
    LIST = "list"


@dataclass(slots=True)
class SearchRequest:
    """Immutable description of a search request.

    Attributes:
        query: The free-text query (may be empty for "browse" mode — the
            host decides whether that is allowed).
        site_id: ``None`` means "all sites" (subject to the host's policy).
        page: 1-based page index.
        per_page: Page size, echoed to the host for convenience.
    """

    query: str
    site_id: str | None = None
    page: int = 1
    per_page: int = 24


@dataclass(slots=True)
class SearchPage:
    """Result of a search request, as displayed by the view.

    Attributes:
        items: Ordered list of :class:`MangaCardData` to display.
        page: The 1-based page index this result refers to.
        total: Optional total number of items across all pages.
        has_prev: Whether a previous page exists.
        has_next: Whether a next page exists.
        query: The query string this result is for — echoed for display and
            used by the host to drop stale responses.
        elapsed_ms: Optional server-reported duration, used for telemetry
            display only.
    """

    items: list[MangaCardData]
    page: int = 1
    total: int | None = None
    has_prev: bool = False
    has_next: bool = False
    query: str = ""
    elapsed_ms: float | None = None


@dataclass(frozen=True, slots=True)
class SearchLabels:
    """All user-visible strings used by :class:`SearchView`."""

    search_placeholder: str = "Search manga…"
    search_button: str = "Search"
    empty_hint: str = "Type a query and press Enter to search."
    no_results: str = "No results for “{query}”."
    error_prefix: str = "Search failed:"
    retry: str = "Retry"
    loading: str = "Searching…"
    prev_page: str = "← Prev"
    next_page: str = "Next →"
    page_of: str = "Page {page} of {total}"
    page_only: str = "Page {page}"
    results_for: str = "{n} result(s) for “{query}”"
    results_for_site: str = "{n} result(s) for “{query}” in {site}"
    select_all: str = "Select all"
    clear_selection: str = "Clear selection"
    selected_count: str = "{n} selected"


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_THEME_LIGHT = {
    "bg": "#F5F5F5",
    "panel_bg": "#FFFFFF",
    "fg": "#1A1A1A",
    "muted_fg": "#666666",
    "border": "#CCCCCC",
    "error_bg": "#FDECEA",
    "error_fg": "#C0392B",
    "hint_fg": "#999999",
}

_THEME_DARK = {
    "bg": "#1A1A1A",
    "panel_bg": "#242424",
    "fg": "#DCDCDC",
    "muted_fg": "#909090",
    "border": "#333333",
    "error_bg": "#3A1F1F",
    "error_fg": "#E74C3C",
    "hint_fg": "#666666",
}

_RESIZE_DEBOUNCE_MS = 100
_THEME_POLL_MS = 1000
_CARD_GAP = 8


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class SearchView(ctk.CTkFrame):
    """A search screen: site filter, search bar, results grid, pagination.

    Parameters:
        master: Parent widget.
        sites: Already-authorized sites for the site filter dropdown.
        labels: Localized strings. ``None`` uses English defaults.
        manga_card_labels: Labels forwarded to :class:`MangaCard`.
        layout: Initial results layout.
        per_page: Page size, echoed in :class:`SearchRequest`.
        card_width: Target card width in ``GRID`` layout.
        card_height: Target card height in ``GRID`` layout (unused by
            :class:`MangaCard`, kept for host-provided fixed-size cells).
        cover_size: ``(w, h)`` for covers inside cards.
        show_site_filter: Show the site dropdown. ``False`` for a single-site
            deep-link search view.
        show_pagination: Show the prev/next controls when applicable.
        keep_results_while_loading: Keep previous results visible while a
            new request is in flight.
        selectable: Enable multi-select of cards.
        on_search: Called with the :class:`SearchRequest` whenever the user
            triggers a search (button, Enter, pagination, retry).
        on_download_manga: Called when the user clicks *Download* on a card.
        on_view_manga: Called when the user opens a card.
        on_cover_needed: Called once per new card when the cover should be
            loaded. The host calls :meth:`set_cover` when it's ready.
        on_site_change: Called with the new selected site id (or ``None``).
        on_selection_change: Called with the selected ``MangaCardData`` list.
    """

    def __init__(
        self,
        master,
        *,
        sites: Sequence[SiteConfig],
        labels: SearchLabels | None = None,
        manga_card_labels: MangaCardLabels | None = None,
        layout: SearchLayout = SearchLayout.GRID,
        per_page: int = 24,
        card_width: int = 180,
        card_height: int = 300,
        cover_size: tuple[int, int] = (150, 210),
        show_site_filter: bool = True,
        show_pagination: bool = True,
        keep_results_while_loading: bool = True,
        selectable: bool = False,
        on_search: Callable[[SearchRequest], None],
        on_download_manga: Callable[[MangaCardData], None] | None = None,
        on_view_manga: Callable[[MangaCardData], None] | None = None,
        on_cover_needed: Callable[[MangaCardData], None] | None = None,
        on_site_change: Callable[[str | None], None] | None = None,
        on_selection_change: Callable[[list[MangaCardData]], None] | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        self._labels = labels or SearchLabels()
        self._manga_card_labels = manga_card_labels
        self._layout = layout
        self._per_page = per_page
        self._card_width = card_width
        self._card_height = card_height
        self._cover_size = cover_size
        self._show_site_filter = show_site_filter
        self._show_pagination = show_pagination
        self._keep_results_while_loading = keep_results_while_loading
        self._selectable = selectable

        self._on_search_cb = on_search
        self._on_download_cb = on_download_manga
        self._on_view_cb = on_view_manga
        self._on_cover_needed_cb = on_cover_needed
        self._on_site_change_cb = on_site_change
        self._on_selection_change_cb = on_selection_change

        # Data / state.
        self._cards: dict[str, MangaCard] = {}
        self._card_data_by_id: dict[str, MangaCardData] = {}
        self._selected_ids: set[str] = set()
        self._current_page: SearchPage | None = None
        self._current_request: SearchRequest | None = None
        self._last_successful_request: SearchRequest | None = None
        self._loading: bool = False
        self._error: str | None = None
        self._current_cols: int = 0

        # Deferred/async state.
        self._reflow_after_id: str | None = None
        self._theme_after_id: str | None = None
        self._destroyed = False
        self._last_theme_mode: str = self._current_mode()

        self._build(sites)
        self._apply_theme()
        self._render_empty_state()

        self.bind("<Destroy>", self._on_destroy)
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Public API — state
    # ------------------------------------------------------------------

    @property
    def current_request(self) -> SearchRequest | None:
        """The most recently emitted :class:`SearchRequest`, or ``None``."""
        return self._current_request

    def get_query(self) -> str:
        return self._search_var.get().strip()

    def get_selected_site_id(self) -> str | None:
        if not self._show_site_filter or self._site_dropdown is None:
            return None
        return self._site_dropdown.get_selected_id()

    def get_selected(self) -> list[MangaCardData]:
        return [
            self._card_data_by_id[mid]
            for mid in self._selected_ids
            if mid in self._card_data_by_id
        ]

    # ------------------------------------------------------------------
    # Public API — host pushes
    # ------------------------------------------------------------------

    def set_loading(self, loading: bool) -> None:
        """Show / hide the loading indicator."""
        if self._destroyed:
            return
        self._loading = bool(loading)
        self._render_status_bar()

    def set_error(self, message: str | None) -> None:
        """Show an error banner, or clear it when ``message`` is ``None``."""
        if self._destroyed:
            return
        self._error = message
        if message is not None:
            self._loading = False
        self._render_status_bar()

    def set_results(self, page: SearchPage) -> None:
        """Replace the current results with ``page``."""
        if self._destroyed:
            return
        self._error = None
        self._loading = False
        self._current_page = page
        self._last_successful_request = self._current_request

        # Clear selection: it refers to a previous result set.
        self._selected_ids.clear()

        self._render_cards(page.items)
        self._render_status_bar()
        self._render_pagination()
        if not page.items:
            self._render_empty_state_for_query(page.query)

    def set_page(self, page: int, *, total: int | None = None) -> None:
        """Update pagination controls without re-rendering the cards.

        Useful for lazy backends that stream pages; typically the host
        calls :meth:`set_results` instead.
        """
        if self._destroyed or self._current_page is None:
            return
        self._current_page.page = page
        if total is not None:
            self._current_page.total = total
        self._current_page.has_prev = page > 1
        self._render_pagination()

    def set_cover(self, manga_id: str, image: "Image.Image | None") -> None:
        """Push a loaded cover image to the matching card."""
        if self._destroyed:
            return
        card = self._cards.get(manga_id)
        if card is None:
            return
        card.set_cover(image)

    def set_sites(self, sites: Sequence[SiteConfig]) -> None:
        """Replace the sites available in the filter dropdown."""
        if self._destroyed or self._site_dropdown is None:
            return
        self._site_dropdown.update_sites(sites)

    def set_layout(self, layout: SearchLayout) -> None:
        """Switch between ``GRID`` and ``LIST`` layouts at runtime."""
        if self._destroyed or layout == self._layout:
            return
        self._layout = layout
        self._current_cols = -1  # force reflow
        self._rebuild_cards_for_layout()
        self._request_reflow()

    # ------------------------------------------------------------------
    # Public API — programmatic actions
    # ------------------------------------------------------------------

    def trigger_search(self) -> None:
        """Run a search with the current query / site / page 1."""
        self._emit_search(page=1)

    def trigger_retry(self) -> None:
        """Re-run the last request (used by the error banner)."""
        if self._last_successful_request is not None:
            req = self._last_successful_request
            self._emit_search(
                page=req.page,
                query=req.query,
                site_id=req.site_id,
            )
        else:
            self.trigger_search()

    def focus_search(self) -> None:
        """Give keyboard focus to the search entry."""
        if not self._destroyed:
            try:
                self._search_entry.focus_set()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self, sites: Sequence[SiteConfig]) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # --- Top bar: site dropdown + search entry + button ---
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        top.grid_columnconfigure(1, weight=1)

        self._site_dropdown: SiteDropdown | None = None
        if self._show_site_filter:
            self._site_dropdown = SiteDropdown(
                top,
                sites=list(sites),
                on_change=self._handle_site_change,
                allow_clear=True,
                searchable=True,
            )
            self._site_dropdown.grid(row=0, column=0, sticky="w", padx=(0, 8))

        self._search_var = ctk.StringVar(master=self, value="")
        self._search_entry = ctk.CTkEntry(
            top,
            placeholder_text=self._labels.search_placeholder,
            textvariable=self._search_var,
        )
        self._search_entry.grid(row=0, column=1, sticky="ew")
        self._search_entry.bind("<Return>", self._on_enter_pressed)
        self._search_entry.bind("<Escape>", self._on_escape_pressed)

        self._search_btn = ctk.CTkButton(
            top,
            text=self._labels.search_button,
            width=100,
            command=self._on_search_button_click,
        )
        self._search_btn.grid(row=0, column=2, padx=(8, 0))

        # --- Status bar: loading / error / results count ---
        self._status_bar = ctk.CTkFrame(self, fg_color="transparent")
        self._status_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
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
            command=self.trigger_retry,
        )

        # --- Results scroll ---
        self._results_scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
        )
        self._results_scroll.grid(row=2, column=0, sticky="nsew", padx=6, pady=2)
        self._results_scroll.bind("<Configure>", self._on_results_configure)

        # --- Empty state (overlay inside results scroll) ---
        self._empty_frame = ctk.CTkFrame(
            self._results_scroll, fg_color="transparent",
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
        self._pagination.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 10))
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

    # ------------------------------------------------------------------
    # Rendering — state
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
                text=self._labels.loading,
                text_color=theme["muted_fg"],
            )
            return

        page = self._current_page
        if page is not None and page.items:
            count_text = self._format_result_count(page)
            self._status_label.configure(text=count_text, text_color=theme["muted_fg"])
        else:
            self._status_label.configure(text="", text_color=theme["muted_fg"])

    def _format_result_count(self, page: SearchPage) -> str:
        n = page.total if page.total is not None else len(page.items)
        site_label = ""
        if self._site_dropdown is not None:
            selected = self._site_dropdown.get_selected()
            if selected is not None:
                site_label = selected.name
        if site_label:
            return self._labels.results_for_site.format(
                n=n, query=page.query, site=site_label
            )
        return self._labels.results_for.format(n=n, query=page.query)

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

    def _render_empty_state(self) -> None:
        self._empty_label.configure(text=self._labels.empty_hint)
        self._show_empty()

    def _render_empty_state_for_query(self, query: str) -> None:
        if query:
            self._empty_label.configure(
                text=self._labels.no_results.format(query=query)
            )
        else:
            self._empty_label.configure(text=self._labels.empty_hint)
        self._show_empty()

    def _show_empty(self) -> None:
        self._empty_frame.pack(fill="both", expand=True)
        for card in self._cards.values():
            try:
                card.grid_forget()
            except Exception:
                pass

    def _hide_empty(self) -> None:
        try:
            self._empty_frame.pack_forget()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Rendering — cards
    # ------------------------------------------------------------------

    def _render_cards(self, items: list[MangaCardData]) -> None:
        # Tear down previous cards.
        for card in self._cards.values():
            try:
                card.destroy()
            except Exception:
                pass
        self._cards.clear()
        self._card_data_by_id.clear()

        if not items:
            self._render_empty_state_for_query(
                self._current_page.query if self._current_page else ""
            )
            return

        self._hide_empty()

        for data in items:
            self._card_data_by_id[data.manga_id] = data
            card = self._make_card(data)
            self._cards[data.manga_id] = card

            # Ask the host for the cover exactly once per card.
            if self._on_cover_needed_cb is not None and data.cover_url:
                card.set_cover_loading(True)
                self._safe_call(self._on_cover_needed_cb, data)

        self._request_reflow(force=True)

    def _make_card(self, data: MangaCardData) -> MangaCard:
        orientation = (
            CardOrientation.HORIZONTAL
            if self._layout == SearchLayout.LIST
            else CardOrientation.VERTICAL
        )
        return MangaCard(
            self._results_scroll,
            data,
            labels=self._manga_card_labels,
            on_download=self._on_download_cb,
            on_view=self._on_view_cb,
            on_select=(
                self._handle_card_select if self._selectable else None
            ),
            selectable=self._selectable,
            selected=data.manga_id in self._selected_ids,
            orientation=orientation,
            cover_size=self._cover_size,
            show_author=True,
            show_status=True,
            show_site=True,
            show_chapter_count=True,
            show_actions=True,
        )

    def _rebuild_cards_for_layout(self) -> None:
        """Destroy and recreate cards to reflect a layout change."""
        items = list(self._card_data_by_id.values())
        if not items:
            return
        current_page = self._current_page
        current_query = current_page.query if current_page else ""
        self._render_cards(items)
        if current_page is not None:
            current_page.query = current_query

    def _handle_card_select(self, manga: MangaCardData, is_selected: bool) -> None:
        if is_selected:
            self._selected_ids.add(manga.manga_id)
        else:
            self._selected_ids.discard(manga.manga_id)
        if self._on_selection_change_cb is not None:
            self._safe_call(self._on_selection_change_cb, self.get_selected())

    # ------------------------------------------------------------------
    # Reflow
    # ------------------------------------------------------------------

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
        if self._destroyed or not self._cards:
            return

        available = self._results_scroll.winfo_width()
        if available < self._card_width // 2:
            return

        if self._layout == SearchLayout.LIST:
            new_cols = 1
        else:
            new_cols = max(1, available // (self._card_width + _CARD_GAP))

        if new_cols == self._current_cols:
            return
        self._current_cols = new_cols

        for col in range(max(self._current_cols, new_cols, 1)):
            try:
                self._results_scroll.grid_columnconfigure(
                    col, weight=1 if col < new_cols else 0,
                    uniform="card" if self._layout == SearchLayout.GRID else "",
                )
            except Exception:
                pass

        for card in self._cards.values():
            try:
                card.grid_forget()
            except Exception:
                pass

        for idx, card in enumerate(self._cards.values()):
            row, col = divmod(idx, new_cols)
            try:
                card.grid(
                    row=row,
                    column=col,
                    padx=_CARD_GAP // 2,
                    pady=_CARD_GAP // 2,
                    sticky="nsew",
                )
            except Exception:
                _log.exception("SearchView: failed to grid card")

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_enter_pressed(self, _event=None) -> str:
        self._emit_search(page=1)
        return "break"

    def _on_escape_pressed(self, _event=None) -> str:
        self._search_var.set("")
        return "break"

    def _on_search_button_click(self) -> None:
        self._emit_search(page=1)

    def _on_page_nav(self, delta: int) -> None:
        if self._current_page is None or self._current_request is None:
            return
        new_page = self._current_page.page + delta
        if new_page < 1:
            return
        self._emit_search(
            page=new_page,
            query=self._current_request.query,
            site_id=self._current_request.site_id,
        )

    def _handle_site_change(self, selected: SiteConfig | None) -> None:
        new_id = selected.id if selected is not None else None
        if self._on_site_change_cb is not None:
            self._safe_call(self._on_site_change_cb, new_id)
        # Update result count text if results are visible.
        self._render_status_bar()

    def _emit_search(
        self,
        *,
        page: int,
        query: str | None = None,
        site_id: str | None = None,
    ) -> None:
        if self._destroyed:
            return
        if query is None:
            query = self.get_query()
        if site_id is None:
            site_id = self.get_selected_site_id()

        request = SearchRequest(
            query=query,
            site_id=site_id,
            page=max(1, page),
            per_page=self._per_page,
        )
        self._current_request = request
        self._error = None

        if not self._keep_results_while_loading:
            self._render_cards([])
        self._loading = True
        self._render_status_bar()

        self._safe_call(self._on_search_cb, request)

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
        self._render_status_bar()
        self._render_pagination()
        self._render_empty_state() if not self._cards else None

    def _schedule_theme_check(self) -> None:
        if self._destroyed:
            return
        self._theme_after_id = self.after(_THEME_POLL_MS, self._check_theme)

    def _check_theme(self) -> None:
        self._theme_after_id = None
        if self._destroyed:
            return
        current = self._current_mode()
        if current != self._last_theme_mode:
            self._last_theme_mode = current
            self._apply_theme()
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_call(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:
            _log.exception(
                "SearchView: callback {!r} raised",
                getattr(cb, "__name__", cb),
            )

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._destroyed = True
        for attr in ("_reflow_after_id", "_theme_after_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
