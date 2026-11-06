"""Chapter table widget for the NexusDL GUI.

Presentation-only. Renders a sortable, filterable, multi-selectable table of
chapter rows. Does **not** fetch chapters, does **not** know about
:class:`EventBus`, :class:`SiteRegistry`, :class:`DownloadManager` or
:class:`Settings`.

Universal design notes:
    * Columns are **data-driven** via :class:`ColumnSpec` — adding a column
      is a single entry, never a rendering change.
    * Sorting is local by default (with natural ordering for numeric-looking
      strings), but can be delegated to the hosting backend via
      ``local_sorting=False`` + ``on_sort_change``.
    * Filtering is programmatic via :meth:`ChapterTable.set_filter` — the
      hosting view can attach its own filter UI.
    * Selection is delegated for actions, not owned: the hosting view reads
      :meth:`ChapterTable.get_selected` on demand or reacts to
      :attr:`on_selection_change`.
    * Row status → color mapping is data-driven via ``status_colors``.
    * Context menu is fully delegated (``on_context_menu``), so the widget
      knows nothing about "download", "delete", "mark read", etc.
    * The underlying ``ttk.Treeview`` does not auto-theme with CTk; the
      widget polls ``ctk.get_appearance_mode()`` and re-applies its style
      when it changes. :meth:`refresh_theme` is exposed for manual use.
    * Callbacks are isolated — a misbehaving parent never breaks Tk.
    * Cleans up after itself on ``<Destroy>``.

Selection across re-renders
---------------------------
:meth:`ChapterTable._render_all` clears the underlying ``ttk.Treeview``
and re-inserts every visible row. A naive implementation would drop the
user's selection on every sort change, filter change, or batch row
update — a user who selects five chapters, then clicks a header to sort,
would find their selection gone and have to re-select.

The implementation captures ``Treeview.selection()`` before the clear,
and re-applies it after the insert loop, skipping ids that are no longer
in the tree (a row filtered out by a predicate change). The user keeps
their selection as long as the rows survive the re-render, which is what
every table UI in every framework does.

Theme polling safety
--------------------
:meth:`ChapterTable._check_theme` runs every second to detect a CTk
appearance-mode change. The re-application of the ttk style
(:meth:`ChapterTable._apply_theme`) touches three separate Tk surfaces —
the style configure, the tag colours, and the row re-render — and any
one of them can raise :class:`tkinter.TclError` (a Tk version mismatch,
a destroyed tree, a font not loaded).

The polling loop therefore wraps the whole detection-and-apply sequence
in a ``try/finally`` that always reschedules the next poll. Without the
``finally``, a single failure would stop the polling loop for the
widget's lifetime, leaving the table stuck on the previous theme and
silently dark-on-light in the user's next session. The ``finally`` is
the cheapest way to make the poll cycle self-healing — the next tick
retries the transition on the (by now hopefully fixed) system.
"""
from __future__ import annotations

import re
import tkinter as tk
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from tkinter import ttk
from typing import TYPE_CHECKING

import customtkinter as ctk
from loguru import logger as _logger

if TYPE_CHECKING:
    from PIL import Image

_log = _logger.bind(component="gui.chapter_table")


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

class ChapterRowStatus(str, Enum):
    """Download / readiness status of a chapter row."""

    NEW = "new"
    READY = "ready"            # pages cached but not packaged
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    PARTIAL = "partial"        # some pages missing
    FAILED = "failed"
    SKIPPED = "skipped"        # already present, skipped intentionally


@dataclass(slots=True)
class ChapterRowData:
    """Displayable projection of a chapter.

    ``extra`` is an escape hatch for optional columns the hosting view adds
    (e.g. ``{"scanlator": "Flame"})`` — the table never inspects it directly,
    only through ``ColumnSpec.formatter``.
    """

    chapter_id: str
    number: str
    title: str = ""
    page_count: int | None = None
    released_at: datetime | None = None
    status: ChapterRowStatus = ChapterRowStatus.NEW
    progress: float = 0.0
    site_name: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """Description of a single table column.

    Attributes:
        key: Unique column identifier. Also used as ``iid`` prefix for sort
            state and as the header sort callback key.
        label: Displayed header text.
        width: Initial pixel width.
        anchor: Text anchor — ``"w"``, ``"center"``, ``"e"``.
        sortable: Whether clicking the header sorts by this column.
        stretch: Whether the column stretches when the widget is resized.
        formatter: Optional ``(row) -> str`` to compute the cell value.
            Required for computed columns; optional for attribute columns.
        sort_key: Optional ``(row) -> comparable`` used for sorting. When
            ``None``, uses the cell string with natural ordering.
    """

    key: str
    label: str
    width: int = 100
    anchor: str = "w"
    sortable: bool = True
    stretch: bool = False
    formatter: Callable[[ChapterRowData], str] | None = None
    sort_key: Callable[[ChapterRowData], object] | None = None


@dataclass(frozen=True, slots=True)
class ChapterTableLabels:
    """All user-visible strings used by :class:`ChapterTable`."""

    status_new: str = "New"
    status_ready: str = "Ready"
    status_downloading: str = "Downloading"
    status_downloaded: str = "Downloaded"
    status_partial: str = "Partial"
    status_failed: str = "Failed"
    status_skipped: str = "Skipped"
    unknown_pages: str = "—"
    unknown_date: str = "—"
    empty_hint: str = "No chapters"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_STATUS_COLORS_LIGHT: dict[ChapterRowStatus, str | None] = {
    ChapterRowStatus.NEW: None,
    ChapterRowStatus.READY: "#D6E4F0",
    ChapterRowStatus.DOWNLOADING: "#CFE4F6",
    ChapterRowStatus.DOWNLOADED: "#D4F0DC",
    ChapterRowStatus.PARTIAL: "#F0ECD2",
    ChapterRowStatus.FAILED: "#F0D6D6",
    ChapterRowStatus.SKIPPED: "#E8E8E8",
}

_DEFAULT_STATUS_COLORS_DARK: dict[ChapterRowStatus, str | None] = {
    ChapterRowStatus.NEW: None,
    ChapterRowStatus.READY: "#1E3A5A",
    ChapterRowStatus.DOWNLOADING: "#1E3A5A",
    ChapterRowStatus.DOWNLOADED: "#1E4A32",
    ChapterRowStatus.PARTIAL: "#4A451E",
    ChapterRowStatus.FAILED: "#4A1E1E",
    ChapterRowStatus.SKIPPED: "#2E2E2E",
}

_THEME_LIGHT = {
    "bg": "#FFFFFF",
    "fg": "#1A1A1A",
    "alt_bg": "#F7F7F7",
    "heading_bg": "#E8E8E8",
    "heading_fg": "#1A1A1A",
    "heading_active_bg": "#DADADA",
    "selected_bg": "#3B8ED0",
    "selected_fg": "#FFFFFF",
    "border": "#CCCCCC",
}

_THEME_DARK = {
    "bg": "#1A1A1A",
    "fg": "#DCDCDC",
    "alt_bg": "#202020",
    "heading_bg": "#2B2B2B",
    "heading_fg": "#DCDCDC",
    "heading_active_bg": "#333333",
    "selected_bg": "#1F6AA5",
    "selected_fg": "#FFFFFF",
    "border": "#333333",
}

_TREEVIEW_STYLE_NAME = "ChapterTable.Treeview"
_HEADING_STYLE_NAME = "ChapterTable.Treeview.Heading"
_CLAM_INITIALIZED = False
_SELECT_DEBOUNCE_MS = 20
_THEME_POLL_MS = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Matches an integer or a decimal number, with **either** ``.`` or ``,``
#: as the decimal separator. Continental European locales emit
#: ``"1,5"`` where Anglo locales emit ``"1.5"``; a natural-sort
#: implementation that only handled ``.`` would sort ``"1,5"`` as text
#: (after ``"2"``, before ``"bonus"``) instead of as a number (before
#: ``"2"``). The ``replace(",", ".")`` normalisation happens in
#: :func:`_natural_key`.
_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


def _natural_key(value: str) -> tuple:
    """Natural ordering key for ``value``.

    ``"1"`` < ``"2"`` < ``"10"`` < ``"12.5"`` < ``"bonus"``.
    Both ``"."`` and ``","`` are accepted as decimal separators so a
    French chapter list (``"1,5"``) sorts before ``"2"``.
    """
    parts: list[tuple[int, object]] = []
    for chunk in _NUMBER_RE.split(value):
        if not chunk:
            continue
        if _NUMBER_RE.fullmatch(chunk):
            try:
                normalized = chunk.replace(",", ".")
                parts.append((0, float(normalized)))
            except ValueError:
                parts.append((1, chunk.lower()))
        else:
            parts.append((1, chunk.lower()))
    return tuple(parts)


def _cell_value(row: ChapterRowData, spec: ColumnSpec) -> str:
    """Compute the displayed cell text for ``row`` given ``spec``."""
    if spec.formatter is not None:
        try:
            return spec.formatter(row)
        except Exception:
            _log.exception("ColumnSpec.formatter raised for column {!r}", spec.key)
            return ""
    raw = getattr(row, spec.key, "")
    if raw is None:
        return ""
    return str(raw)


def _sort_value(row: ChapterRowData, spec: ColumnSpec) -> object:
    """Compute the sort key for ``row`` on column ``spec``."""
    if spec.sort_key is not None:
        try:
            return spec.sort_key(row)
        except Exception:
            _log.exception("ColumnSpec.sort_key raised for column {!r}", spec.key)
    raw = getattr(row, spec.key, None)
    if raw is None:
        return (2, "")  # sort nulls last
    if isinstance(raw, (int, float)):
        return (0, float(raw))
    if isinstance(raw, datetime):
        return (0, raw.timestamp())
    return (1, _natural_key(str(raw)))


def _ensure_clam_theme() -> None:
    """Ensure the ``clam`` ttk theme is active (required for styling).

    ``clam`` is the only ttk theme that reliably honours
    ``tag_configure(background=...)`` and ``style.configure(...)`` on
    every platform. The other built-in themes — ``default`` on
    Windows, ``aqua`` on macOS, ``alt`` — either ignore background
    colours entirely (Windows ``vista``) or re-apply their own
    internal theming at render time (macOS ``aqua``). The switch is
    therefore made **unconditionally** (except when ``clam`` is already
    active), not just when the current theme is missing from a list.
    """
    global _CLAM_INITIALIZED
    if _CLAM_INITIALIZED:
        return
    try:
        style = ttk.Style()
        current = style.theme_use()
        if current != "clam":
            style.theme_use("clam")
    except tk.TclError:
        _log.debug("ttk theme switch skipped (Tk not ready)")
    _CLAM_INITIALIZED = True


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class ChapterTable(ctk.CTkFrame):
    """A sortable, filterable, multi-selectable chapter table.

    Parameters:
        master: Parent widget.
        columns: Column specifications. Use :func:`default_columns` for the
            standard chapter table layout.
        labels: Localized strings. ``None`` uses English defaults.
        selectmode: ``"none"``, ``"browse"`` (single), or ``"extended"``.
        height: Number of visible rows (Treeview hint).
        row_height: Row height in pixels.
        local_sorting: If ``True`` (default), sorting is done in the widget.
            If ``False``, clicking a header calls :attr:`on_sort_change` and
            leaves ordering to the hosting view.
        default_sort_key: Column key to sort by initially. ``None`` keeps the
            insertion order.
        default_sort_descending: Initial sort direction.
        status_colors: Override of the status → background color mapping.
            Pass ``{status: None}`` to disable coloring for a status.
        on_selection_change: Called with ``list[ChapterRowData]`` whenever
            the selection changes (debounced).
        on_double_click: Called with the :class:`ChapterRowData` on
            double-click.
        on_context_menu: Called with ``(row, raw_tk_event)`` on right click.
            The hosting view is responsible for building and posting the menu.
        on_sort_change: Called with ``(column_key, descending)`` when the
            user clicks a sortable header. Always called, even when
            ``local_sorting=True`` (useful for telemetry).
    """

    def __init__(
        self,
        master,
        *,
        columns: Sequence[ColumnSpec],
        labels: ChapterTableLabels | None = None,
        selectmode: str = "extended",
        height: int = 12,
        row_height: int = 26,
        local_sorting: bool = True,
        default_sort_key: str | None = "number",
        default_sort_descending: bool = False,
        status_colors: dict[ChapterRowStatus, str | None] | None = None,
        on_selection_change: Callable[[list[ChapterRowData]], None] | None = None,
        on_double_click: Callable[[ChapterRowData], None] | None = None,
        on_context_menu: Callable[[ChapterRowData, object], None] | None = None,
        on_sort_change: Callable[[str, bool], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, corner_radius=8, **kwargs)

        if selectmode not in ("none", "browse", "extended"):
            raise ValueError(f"Invalid selectmode: {selectmode!r}")
        if not columns:
            raise ValueError("ChapterTable requires at least one column")

        self._columns = list(columns)
        self._columns_by_key = {c.key: c for c in self._columns}
        self._labels = labels or ChapterTableLabels()
        self._selectmode = selectmode
        self._row_height = row_height
        self._local_sorting = local_sorting
        self._status_colors_override = status_colors

        self._on_selection_change = on_selection_change
        self._on_double_click = on_double_click
        self._on_context_menu = on_context_menu
        self._on_sort_change = on_sort_change

        # Data state.
        self._rows: list[ChapterRowData] = []
        self._rows_by_id: dict[str, ChapterRowData] = {}
        self._filter: Callable[[ChapterRowData], bool] | None = None
        self._sort_key: str | None = default_sort_key
        self._sort_descending: bool = default_sort_descending

        # Widget state.
        self._destroyed = False
        self._select_after_id: str | None = None
        self._theme_after_id: str | None = None
        self._last_theme_mode: str = self._current_mode()
        self._current_status_colors: dict[ChapterRowStatus, str | None] = {}

        _ensure_clam_theme()
        self._build(height=height, row_height=row_height)
        self._apply_theme()

        self.bind("<Destroy>", self._on_destroy, add="+")
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Public API — data
    # ------------------------------------------------------------------

    @property
    def labels(self) -> ChapterTableLabels:
        return self._labels

    def set_rows(self, rows: Iterable[ChapterRowData]) -> None:
        """Replace all rows with ``rows`` and re-render."""
        if self._destroyed:
            return
        self._rows = list(rows)
        self._rows_by_id = {r.chapter_id: r for r in self._rows}
        self._render_all()

    def update_row(self, chapter_id: str, **changes) -> None:
        """Update in-place the row identified by ``chapter_id``.

        Only recomputes and re-renders that row, unless the changed fields
        affect the current sort key — in which case the whole table is
        re-sorted and re-rendered.

        Pass ``status``, ``progress``, ``page_count``, etc. as kwargs.
        """
        if self._destroyed or chapter_id not in self._rows_by_id:
            return
        row = self._rows_by_id[chapter_id]
        for key, value in changes.items():
            if key == "extra" and isinstance(value, dict):
                row.extra.update(value)
            elif hasattr(row, key):
                setattr(row, key, value)
            else:
                _log.debug("update_row: unknown field {!r}", key)

        if self._affects_sort(changes.keys()):
            self._render_all()
        else:
            self._render_row(row)

    def remove_row(self, chapter_id: str) -> None:
        if self._destroyed or chapter_id not in self._rows_by_id:
            return
        del self._rows_by_id[chapter_id]
        self._rows = [r for r in self._rows if r.chapter_id != chapter_id]
        try:
            self._tree.delete(chapter_id)
        except tk.TclError:
            pass
        self._update_empty_state()

    def clear(self) -> None:
        if self._destroyed:
            return
        self._rows = []
        self._rows_by_id = {}
        try:
            self._tree.delete(*self._tree.get_children())
        except tk.TclError:
            pass
        self._update_empty_state()

    def get_row(self, chapter_id: str) -> ChapterRowData | None:
        return self._rows_by_id.get(chapter_id)

    # ------------------------------------------------------------------
    # Public API — filter / sort
    # ------------------------------------------------------------------

    def set_filter(
        self, predicate: Callable[[ChapterRowData], bool] | None
    ) -> None:
        """Apply / clear a row filter and re-render."""
        if self._destroyed:
            return
        self._filter = predicate
        self._render_all()

    def set_sort(self, column_key: str | None, descending: bool = False) -> None:
        """Programmatically set the sort column and direction."""
        if self._destroyed:
            return
        if column_key is not None and column_key not in self._columns_by_key:
            raise KeyError(f"Unknown column: {column_key!r}")
        self._sort_key = column_key
        self._sort_descending = descending
        if self._local_sorting:
            self._render_all()

    # ------------------------------------------------------------------
    # Public API — selection
    # ------------------------------------------------------------------

    def get_selected(self) -> list[ChapterRowData]:
        try:
            ids = self._tree.selection()
        except tk.TclError:
            return []
        return [self._rows_by_id[i] for i in ids if i in self._rows_by_id]

    def select_all(self) -> None:
        if self._destroyed or self._selectmode == "none":
            return
        try:
            self._tree.selection_set(self._tree.get_children())
        except tk.TclError:
            pass

    def clear_selection(self) -> None:
        if self._destroyed:
            return
        try:
            self._tree.selection_remove(*self._tree.selection())
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Public API — theme
    # ------------------------------------------------------------------

    def refresh_theme(self) -> None:
        """Re-apply the light/dark theme (call after a runtime theme switch)."""
        if self._destroyed:
            return
        self._last_theme_mode = self._current_mode()
        self._apply_theme()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self, *, height: int, row_height: int) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        container = ctk.CTkFrame(self, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)

        self._tree = ttk.Treeview(
            container,
            columns=[c.key for c in self._columns],
            show="headings",
            selectmode=self._selectmode,
            height=height,
            style=_TREEVIEW_STYLE_NAME,
        )

        for spec in self._columns:
            self._tree.heading(
                spec.key,
                text=spec.label,
                anchor=spec.anchor,
                command=(self._make_header_command(spec.key) if spec.sortable else ""),
            )
            self._tree.column(
                spec.key,
                width=spec.width,
                anchor=spec.anchor,
                stretch=spec.stretch,
                minwidth=40,
            )

        self._tree.grid(row=0, column=0, sticky="nsew")
        self._tree.configure(rowheight=row_height)

        scrollbar = ctk.CTkScrollbar(
            container,
            command=self._tree.yview,
            width=14,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._empty_label = ctk.CTkLabel(
            container,
            text=self._labels.empty_hint,
            text_color=("gray50", "gray60"),
            fg_color="transparent",
        )
        self._empty_label.grid(row=1, column=0, columnspan=2, pady=8)
        self._empty_label.grid_remove()

        # Events.
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._tree.bind("<Double-Button-1>", self._on_tree_double_click)
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self._tree.bind(seq, self._on_tree_context_menu)
        self._tree.bind("<Control-a>", self._on_select_all_event)
        self._tree.bind("<Command-a>", self._on_select_all_event)  # macOS

    def _make_header_command(self, key: str) -> Callable[[], None]:
        def _cmd() -> None:
            self._on_header_click(key)
        return _cmd

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _visible_rows(self) -> list[ChapterRowData]:
        rows = self._rows
        if self._filter is not None:
            try:
                rows = [r for r in rows if self._filter(r)]
            except Exception:
                _log.exception("ChapterTable filter predicate raised")
                rows = list(self._rows)
        if self._sort_key is None or not self._local_sorting:
            return rows
        spec = self._columns_by_key[self._sort_key]
        try:
            return sorted(
                rows,
                key=lambda r: _sort_value(r, spec),
                reverse=self._sort_descending,
            )
        except Exception:
            _log.exception("ChapterTable sort raised for key {!r}", self._sort_key)
            return rows

    def _render_all(self) -> None:
        if self._destroyed:
            return

        # Capture the current selection *before* clearing the tree. A
        # sort, filter, or batch-update must not silently drop the
        # user's selection — see the module docstring's
        # "Selection across re-renders" section.
        try:
            previous_selection = set(self._tree.selection())
        except tk.TclError:
            previous_selection = set()

        self._clear_tree()
        rows = self._visible_rows()

        status_colors = self._current_status_colors
        for idx, row in enumerate(rows):
            tag = self._row_tag(row, idx)
            try:
                self._tree.insert(
                    "",
                    "end",
                    iid=row.chapter_id,
                    values=tuple(_cell_value(row, spec) for spec in self._columns),
                    tags=(tag,),
                )
            except tk.TclError:
                # Duplicate iid (should not happen) — log and skip.
                _log.warning("ChapterTable: duplicate chapter_id {!r}", row.chapter_id)

        # Restore selection for rows that survived the re-render. A row
        # filtered out by a predicate change is no longer in the tree,
        # and ``selection_add`` for a missing iid is silently ignored
        # by Tk — the guard on ``exists`` is what makes the call cheap.
        if previous_selection:
            try:
                for iid in previous_selection:
                    if self._tree.exists(iid):
                        self._tree.selection_add(iid)
            except tk.TclError:
                pass

        self._apply_status_tag_colors(status_colors)
        self._update_heading_sort_indicators()
        self._update_empty_state()

    def _render_row(self, row: ChapterRowData) -> None:
        if self._destroyed:
            return
        try:
            exists = self._tree.exists(row.chapter_id)
        except tk.TclError:
            exists = False
        values = tuple(_cell_value(row, spec) for spec in self._columns)
        if exists:
            try:
                self._tree.item(row.chapter_id, values=values)
            except tk.TclError:
                pass
        else:
            # Row is now visible (e.g. filter just started matching it).
            self._render_all()
            return

        # Re-apply per-row tag color, in case status changed.
        tag = self._status_tag(row.status)
        try:
            self._tree.item(row.chapter_id, tags=(tag,))
        except tk.TclError:
            pass
        self._apply_status_tag_colors(self._current_status_colors)

    def _clear_tree(self) -> None:
        try:
            children = self._tree.get_children()
            if children:
                self._tree.delete(*children)
        except tk.TclError:
            pass

    def _update_empty_state(self) -> None:
        try:
            empty = len(self._tree.get_children()) == 0
        except tk.TclError:
            empty = True
        if empty:
            self._empty_label.grid()
        else:
            self._empty_label.grid_remove()

    # ------------------------------------------------------------------
    # Tags / status colors
    # ------------------------------------------------------------------

    @staticmethod
    def _status_tag(status: ChapterRowStatus) -> str:
        return f"status_{status.value}"

    def _row_tag(self, row: ChapterRowData, index: int) -> str:
        # Priority: status color > alternating row.
        if self._current_status_colors.get(row.status) is not None:
            return self._status_tag(row.status)
        return "row_even" if index % 2 == 0 else "row_odd"

    def _apply_status_tag_colors(
        self, status_colors: dict[ChapterRowStatus, str | None]
    ) -> None:
        theme = self._theme()
        self._tree.tag_configure("row_even", background=theme["bg"])
        self._tree.tag_configure("row_odd", background=theme["alt_bg"])
        for status, color in status_colors.items():
            tag = self._status_tag(status)
            if color is None:
                self._tree.tag_configure(tag, background="")
            else:
                self._tree.tag_configure(tag, background=color)

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    def _on_header_click(self, key: str) -> None:
        if self._destroyed:
            return
        spec = self._columns_by_key.get(key)
        if spec is None or not spec.sortable:
            return

        if self._sort_key == key:
            new_desc = not self._sort_descending
        else:
            new_desc = False

        self._sort_key = key
        self._sort_descending = new_desc
        self._update_heading_sort_indicators()

        if self._on_sort_change is not None:
            self._safe_call(self._on_sort_change, key, new_desc)

        if self._local_sorting:
            self._render_all()

    def _update_heading_sort_indicators(self) -> None:
        for spec in self._columns:
            if not spec.sortable:
                continue
            if spec.key == self._sort_key:
                arrow = " ▼" if self._sort_descending else " ▲"
                self._tree.heading(spec.key, text=spec.label + arrow)
            else:
                self._tree.heading(spec.key, text=spec.label)

    def _affects_sort(self, changed_fields: Iterable[str]) -> bool:
        if self._sort_key is None:
            return False
        if self._sort_key in changed_fields:
            return True
        # 'number' formatting may depend on other fields — safe fallback.
        return False

    # ------------------------------------------------------------------
    # Selection events
    # ------------------------------------------------------------------

    def _on_tree_select(self, _event=None) -> None:
        if self._destroyed or self._on_selection_change is None:
            return
        if self._select_after_id is not None:
            try:
                self.after_cancel(self._select_after_id)
            except Exception:
                pass
        self._select_after_id = self.after(
            _SELECT_DEBOUNCE_MS, self._emit_selection_change
        )

    def _emit_selection_change(self) -> None:
        self._select_after_id = None
        if self._destroyed:
            return
        self._safe_call(self._on_selection_change, self.get_selected())

    def _on_select_all_event(self, _event=None) -> str:
        self.select_all()
        return "break"

    def _on_tree_double_click(self, event) -> None:
        if self._destroyed or self._on_double_click is None:
            return
        iid = self._tree.identify_row(event.y)
        if iid and iid in self._rows_by_id:
            self._safe_call(self._on_double_click, self._rows_by_id[iid])

    def _on_tree_context_menu(self, event) -> None:
        if self._destroyed or self._on_context_menu is None:
            return
        iid = self._tree.identify_row(event.y)
        if not iid or iid not in self._rows_by_id:
            return
        # Ensure the clicked row is selected so the host can act on selection.
        try:
            if iid not in self._tree.selection():
                self._tree.selection_set(iid)
        except tk.TclError:
            pass
        self._safe_call(self._on_context_menu, self._rows_by_id[iid], event)

    # ------------------------------------------------------------------
    # Theming
    # ------------------------------------------------------------------

    def _current_mode(self) -> str:
        try:
            return ctk.get_appearance_mode()  # "Light" | "Dark"
        except Exception:
            return "Dark"

    def _theme(self) -> dict:
        return _THEME_LIGHT if self._last_theme_mode == "Light" else _THEME_DARK

    def _status_colors_for_theme(self) -> dict[ChapterRowStatus, str | None]:
        if self._status_colors_override is not None:
            return dict(self._status_colors_override)
        return (
            dict(_DEFAULT_STATUS_COLORS_LIGHT)
            if self._last_theme_mode == "Light"
            else dict(_DEFAULT_STATUS_COLORS_DARK)
        )

    def _apply_theme(self) -> None:
        if self._destroyed:
            return
        theme = self._theme()

        try:
            style = ttk.Style()
            style.configure(
                _TREEVIEW_STYLE_NAME,
                background=theme["bg"],
                foreground=theme["fg"],
                fieldbackground=theme["bg"],
                bordercolor=theme["border"],
                borderwidth=0,
                relief="flat",
                rowheight=self._row_height,
            )
            style.configure(
                _HEADING_STYLE_NAME,
                background=theme["heading_bg"],
                foreground=theme["heading_fg"],
                bordercolor=theme["border"],
                borderwidth=0,
                relief="flat",
                padding=(6, 4),
            )
            style.map(
                _TREEVIEW_STYLE_NAME,
                background=[("selected", theme["selected_bg"])],
                foreground=[("selected", theme["selected_fg"])],
            )
            style.map(
                _HEADING_STYLE_NAME,
                background=[("active", theme["heading_active_bg"])],
            )
        except tk.TclError:
            _log.exception("ChapterTable: failed to apply ttk style")
            return  # don't touch tags or rows if the style itself failed

        self._current_status_colors = self._status_colors_for_theme()
        try:
            self._apply_status_tag_colors(self._current_status_colors)
        except tk.TclError:
            _log.exception("ChapterTable: failed to apply tag colors")

        # Re-render visible rows to pick up the alternating bg correctly.
        if self._rows:
            try:
                self._render_all()
            except Exception:
                _log.exception("ChapterTable: failed to re-render on theme change")

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
            # See the module docstring's "Theme polling safety" section.
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
                "ChapterTable: callback {!r} raised",
                getattr(cb, "__name__", cb),
            )

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        if self._destroyed:
            return
        self._destroyed = True
        for attr in ("_select_after_id", "_theme_after_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def default_columns(
    labels: ChapterTableLabels | None = None,
) -> list[ColumnSpec]:
    """Return the standard NexusDL chapter-table column set.

    The hosting view can use this directly or extend it with extra columns
    (e.g. scanlator, file size) without touching the widget.
    """
    lbl = labels or ChapterTableLabels()

    def _fmt_status(row: ChapterRowData) -> str:
        return getattr(lbl, f"status_{row.status.value}", row.status.value)

    def _fmt_pages(row: ChapterRowData) -> str:
        return str(row.page_count) if row.page_count is not None else lbl.unknown_pages

    def _fmt_released(row: ChapterRowData) -> str:
        return row.released_at.strftime("%Y-%m-%d") if row.released_at else lbl.unknown_date

    def _fmt_progress(row: ChapterRowData) -> str:
        pct = max(0.0, min(1.0, float(row.progress)))
        return f"{int(round(pct * 100))}%"

    return [
        ColumnSpec("number", "#", width=60, anchor="e", sortable=True),
        ColumnSpec("title", "Title", width=320, anchor="w", sortable=True, stretch=True),
        ColumnSpec("status", "Status", width=110, anchor="center", formatter=_fmt_status),
        ColumnSpec("page_count", "Pages", width=70, anchor="e", formatter=_fmt_pages),
        ColumnSpec("progress", "Prog.", width=70, anchor="e", formatter=_fmt_progress),
        ColumnSpec("released_at", "Released", width=120, anchor="center", formatter=_fmt_released),
    ]
