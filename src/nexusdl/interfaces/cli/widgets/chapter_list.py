"""Textual widget for displaying and selecting the chapters of a manga.

A :class:`ChapterList` renders the chapters of one manga in a fixed-
column table, with per-row read/current status, a multi-select checkbox,
and keyboard-driven activation. It is the widget a search-result screen
opens after the user picks a manga, and the widget a library screen
opens when the user selects an entry from their collection.

Two distinct concepts, one widget
---------------------------------
The widget exposes **two separate selection states**, because the
pipeline's UI needs both:

* **Cursor** — the row the user has highlighted with the arrow keys.
  Textual manages this natively; the user moves it, the widget only
  reads it.
* **Marked set** — the rows the user has explicitly marked for a bulk
  action (``"download all marked chapters"``, ``"mark as read"``). The
  widget owns the set, toggles it on ``Space``, and exposes it via
  :attr:`ChapterList.selected_chapters`.

The two are deliberately independent: activating a chapter with
``Enter`` (or a double-click) emits :class:`ChapterList.ChapterActivated`
for the cursor row, regardless of what is marked. Marking and activating
are separate gestures that a user expects to compose freely — mark
three chapters, activate one of them to preview it, then bulk-download
all three.

Status column
-------------
Every row has a two-character status cell that renders one of three
glyphs:

* ``▶`` (bold cyan) — the **current chapter**, the one the user last
  read (passed by the parent screen from the manga's
  :class:`~nexusdl.core.models.library.ReadingProgress`).
* ``✓`` (green) — a **read chapter**. Read state is computed by the
  widget from two inputs the parent supplies: an explicit
  ``read_chapter_ids`` set (for a future "explicitly marked as read"
  feature) and the position of the current chapter. Every chapter that
  appears **before** the current chapter in the list is auto-inferred
  as read; every chapter after is unread. Chapters in
  ``read_chapter_ids`` are always read regardless of position.
* ``·`` (dim) — an **unread chapter**.

The auto-inference is a heuristic, not a fact. A user who jumps ahead
to chapter 50 will see chapters 1–49 marked read even if they have
never opened them. That is the standard behaviour of every manga
reader (Mihon, Komga, Tachiyomi) — "progress" means "how far along",
not "which chapters were opened". A caller that wants a more precise
model passes ``read_chapter_ids`` explicitly, overriding the inference.

What the widget does **not** do
-------------------------------
* **No sorting.** The parent supplies chapters in the order it wants
  them displayed. :meth:`~nexusdl.core.library.database.LibraryDatabase.get_chapters`
  already returns chapters in a numeric-aware order; a search screen
  that wants them reverse-chronological sorts them itself. The widget
  preserves the input order verbatim.
* **No fetching.** The widget never reads from the database, the
  parser, or the registry. Every chapter it displays is in the
  sequence it was constructed with.
* **No pagination or virtualization.** A 5 000-chapter list renders as
  5 000 rows. DataTable handles scrolling, but the widget does not
  chunk the data. A parent with a genuinely large list filters before
  passing it in.
* **No download action.** Activating a chapter posts a message; the
  parent screen decides what to do with it (open a reader, enqueue a
  download, display a preview). The widget is a picker, not a
  dispatcher.

Message handler names
---------------------
The parent screen handles the two messages with::

    def on_chapter_list_chapter_activated(
        self, event: ChapterList.ChapterActivated
    ) -> None:
        chapter = event.chapter
        ...

    def on_chapter_list_selection_changed(
        self, event: ChapterList.SelectionChanged
    ) -> None:
        marked = event.chapters
        ...

The long handler names are the correct outcome of Textual's nested-
message convention — they disambiguate each message from any other
widget's ``ChapterActivated`` or ``SelectionChanged``.

No markup injection
-------------------
Every cell is built with :meth:`textual.content.Text.append`, never
``Text.from_markup``. A chapter title containing ``[`` or ``]`` renders
literally, matching the policy of every other widget in this package.

Localisation
------------
The widget introduces six translation keys under ``chapter.list.*``:

* ``chapter.list.title`` — the header title, singular form.
* ``chapter.list.title_other`` — the header title, plural form, with a
  ``{count}`` placeholder.
* ``chapter.list.selected_one`` / ``.selected_other`` — the "selected"
  fragment appended to the header when at least one chapter is
  marked.
* ``chapter.list.column.number`` — the number column header.
* ``chapter.list.column.title`` — the title column header.
* ``chapter.list.column.pages`` — the pages column header.
* ``chapter.list.empty`` — the empty-state placeholder.
* ``chapter.list.activate_hint`` — the hint text shown at the bottom
  when the table has focus, telling the user that ``Enter`` opens the
  highlighted chapter and ``Space`` marks it.

A missing key renders the key verbatim, matching the contract every
other widget in this package follows. See :mod:`nexusdl.core.i18n`.

Dependency graph (no cycles)::

    nexusdl.core.i18n           ─┐
    nexusdl.core.models.chapter  ├──► interfaces.cli.widgets.chapter_list
    textual                      │

No private attribute of any core module is touched. The widget could be
lifted into a standalone widget library that imports ``nexusdl.core``
through its public sub-package surface only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, Final

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Text
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable, Label, Static

from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.models.chapter import Chapter

if TYPE_CHECKING:
    pass


__all__ = ["ChapterList"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so a future CSS refactor has a
#: single place to update the selectors and the ``DEFAULT_CSS`` string
#: below cannot drift from the ``query_one`` calls.
_ID_HEADER: Final[str] = "chapter-list-header"
_ID_TABLE: Final[str] = "chapter-list-table"
_ID_HINT: Final[str] = "chapter-list-hint"
_ID_EMPTY: Final[str] = "chapter-list-empty"

#: CSS classes used by the widget's DOM.
_CLASS_HIDDEN: Final[str] = "hidden"

#: Status cell glyphs — see the module docstring's "Status column"
#: section.
_STATUS_CURRENT: Final[str] = "▶"
_STATUS_READ: Final[str] = "✓"
_STATUS_UNREAD: Final[str] = "·"

#: Selection cell glyphs. Rendered for both the checked and unchecked
#: state — a blank cell would leave the column visually ragged across
#: rows.
_SELECT_ON: Final[str] = "☑"
_SELECT_OFF: Final[str] = "☐"

#: Rich-text styles per state. Chosen so a scan of the list immediately
#: distinguishes current from read from unread, and selected from
#: unselected, without requiring the user to read the glyphs.
_STATUS_CURRENT_STYLE: Final[str] = "bold cyan"
_STATUS_READ_STYLE: Final[str] = "green"
_STATUS_UNREAD_STYLE: Final[str] = "dim"
_SELECT_ON_STYLE: Final[str] = "bold green"
_SELECT_OFF_STYLE: Final[str] = "dim"

#: Placeholder shown in the number column when a chapter has neither a
#: ``number`` nor a ``volume``. Also used for the pages column when
#: ``page_count`` is zero.
_PLACEHOLDER: Final[str] = "—"

#: Fixed column widths, in cells. The status and selection columns are
#: exactly two characters wide (they hold exactly one glyph and a
#: trailing space); the number column holds up to five characters —
#: enough for ``"10.5"`` or ``"Vol. 1"`` without wrapping; the pages
#: column holds up to four (``"9999"``). The title column is left at
#: ``auto`` so it takes whatever width remains.
_STATUS_COLUMN_WIDTH: Final[int] = 2
_SELECT_COLUMN_WIDTH: Final[int] = 2
_NUMBER_COLUMN_WIDTH: Final[int] = 8
_PAGES_COLUMN_WIDTH: Final[int] = 6

#: Fixed height of the hint row, in cells. One line is enough.
_HINT_HEIGHT: Final[int] = 1


# --------------------------------------------------------------------------- #
# ChapterList                                                                 #
# --------------------------------------------------------------------------- #


class ChapterList(Vertical):
    """A Textual widget displaying one manga's chapters as a selectable table.

    See the module docstring for the design contract, the two selection
    states, the status column, and the empty-state behaviour.

    Args:
        chapters: The chapters to display, in **display order**. The
            widget preserves the order verbatim — see the module
            docstring's "What the widget does not do" section.
        current_chapter_id: The chapter the user last read, when known.
            The widget renders it with a ``▶`` glyph and marks every
            chapter that appears before it as read. ``None`` (the
            default) means no chapter is highlighted and no chapter is
            auto-inferred as read.
        read_chapter_ids: Optional set of chapter ids that should be
            rendered as read regardless of their position relative to
            the current chapter. Intended for a future "explicitly
            marked as read" feature; passing an empty set (the
            default) uses only the auto-inferred read state.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        id: Optional Textual widget id. Typically the parent screen
            assigns one so it can query the widget later.
        classes: Optional Textual CSS classes. Same purpose as *id*.
    """

    #: Default CSS for the widget, applied automatically by Textual. The
    #: application's ``theme.tcss`` may override any rule by targeting
    #: ``ChapterList`` or the ``#chapter-list-*`` ids.
    DEFAULT_CSS: ClassVar[str] = f"""
    ChapterList {{
        height: 1fr;
        border: solid $primary;
    }}
    #{_ID_HEADER} {{
        height: 1;
        padding: 0 1;
        background: $surface;
        text-style: bold;
    }}
    #{_ID_TABLE} {{
        height: 1fr;
    }}
    #{_ID_HINT} {{
        height: {_HINT_HEIGHT};
        padding: 0 1;
        background: $surface;
        color: $text-muted;
        text-style: italic;
    }}
    #{_ID_EMPTY} {{
        height: 1fr;
        padding: 2;
        content-align: center middle;
        color: $text-muted;
        text-align: center;
    }}
    .{_CLASS_HIDDEN} {{
        display: none;
    }}
    """

    #: Keyboard bindings. ``Space`` toggles the marked state of the
    #: cursor row; ``A`` marks every chapter; ``Ctrl+D`` clears the
    #: marked set. ``Enter`` is handled by the :class:`~textual.widgets.DataTable`
    #: itself via ``on_data_table_row_selected`` — the widget does not
    #: bind it.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("space", "toggle_select", "Mark / unmark", show=True),
        Binding("a", "select_all", "Mark all", show=False),
        Binding("ctrl+d", "deselect_all", "Clear marks", show=False),
    ]

    # ------------------------------------------------------------------ #
    # Messages                                                           #
    # ------------------------------------------------------------------ #

    class ChapterActivated(Message):
        """Emitted when the user activates a chapter.

        Fired by ``Enter`` on the cursor row or by a double-click, and
        carries the full :class:`~nexusdl.core.models.chapter.Chapter`
        — not just its id — so the parent screen can act on it without
        a second lookup. The chapter model is immutable, so the message
        holds a safe snapshot.
        """

        def __init__(self, chapter: Chapter) -> None:
            """Store the activated chapter on the message.

            Args:
                chapter: The chapter the user activated.
            """
            self.chapter = chapter
            super().__init__()

    class SelectionChanged(Message):
        """Emitted when the marked set changes.

        Carries the **full current set** of marked chapters, in list
        order — not a delta. This matches the semantics of a "selection
        changed" message in a UI framework: the receiver is expected to
        replace its understanding of the selection with what the message
        carries.
        """

        def __init__(self, chapters: tuple[Chapter, ...]) -> None:
            """Store the new marked set on the message.

            Args:
                chapters: The chapters currently marked, in list order.
                    Empty when the user has cleared every mark.
            """
            self.chapters = chapters
            super().__init__()

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        chapters: Sequence[Chapter],
        *,
        current_chapter_id: str | None = None,
        read_chapter_ids: frozenset[str] = frozenset(),
        translator: Translator | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._translator = (
            translator if translator is not None else get_default_translator()
        )
        self._chapters: list[Chapter] = list(chapters)
        self._by_id: dict[str, Chapter] = {c.id: c for c in chapters}
        self._current_chapter_id = current_chapter_id
        self._explicit_read_ids: frozenset[str] = read_chapter_ids
        self._read_ids: frozenset[str] = self._compute_read_set()
        self._selected_ids: set[str] = set()

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the widget's header, table, hint row, and empty placeholder."""
        yield Label("", id=_ID_HEADER)
        yield DataTable(
            id=_ID_TABLE,
            cursor_type="row",
            zebra_stripes=True,
        )
        yield Label("", id=_ID_HINT)
        yield Static("", id=_ID_EMPTY, classes=_CLASS_HIDDEN)

    def on_mount(self) -> None:
        """Declare the columns, render the initial rows, and focus the table."""
        table = self.query_one(f"#{_ID_TABLE}", DataTable)
        table.add_column(
            "", width=_STATUS_COLUMN_WIDTH, key="status"
        )
        table.add_column(
            "", width=_SELECT_COLUMN_WIDTH, key="select"
        )
        table.add_column(
            self._translator.translate("chapter.list.column.number"),
            width=_NUMBER_COLUMN_WIDTH,
            key="number",
        )
        table.add_column(
            self._translator.translate("chapter.list.column.title"),
            key="title",
        )
        table.add_column(
            self._translator.translate("chapter.list.column.pages"),
            width=_PAGES_COLUMN_WIDTH,
            key="pages",
        )
        self.query_one(f"#{_ID_HINT}", Label).update(
            self._translator.translate("chapter.list.activate_hint")
        )
        self._rebuild()
        table.focus()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def set_chapters(self, chapters: Sequence[Chapter]) -> None:
        """Replace the displayed chapters.

        Marked chapters whose ids are not in the new list are dropped
        from the marked set — the user's mark on a chapter that no
        longer exists is not preserved. A :class:`SelectionChanged`
        message is posted when the marked set actually changes.

        Args:
            chapters: The new list, in display order.
        """
        previous = frozenset(self._selected_ids)
        self._chapters = list(chapters)
        self._by_id = {c.id: c for c in chapters}
        self._selected_ids &= set(self._by_id)
        self._read_ids = self._compute_read_set()
        if self.is_mounted:
            self._rebuild()
        if frozenset(self._selected_ids) != previous:
            self.post_message(self.SelectionChanged(self.selected_chapters))

    def set_current_chapter(self, chapter_id: str | None) -> None:
        """Update the chapter marked as currently being read.

        Recomputes the auto-inferred read set and re-renders the table.
        When the new current chapter is in the displayed list, the
        table's cursor is moved to it so the user's view stays anchored
        on where they left off.

        Args:
            chapter_id: The chapter id, or ``None`` to clear the current
                chapter indicator.
        """
        if self._current_chapter_id == chapter_id:
            return
        self._current_chapter_id = chapter_id
        self._read_ids = self._compute_read_set()
        if self.is_mounted:
            self._rebuild()
            if chapter_id is not None:
                self._scroll_to(chapter_id)

    def set_read_chapters(self, read_chapter_ids: frozenset[str]) -> None:
        """Replace the explicit "read" set.

        The auto-inferred read set (every chapter before the current
        chapter) is merged with *read_chapter_ids* on top, so a caller
        that wants to mark chapters beyond the current position as read
        supplies them here.

        Args:
            read_chapter_ids: The explicit read set. Passing an empty
                set reverts to auto-inference only.
        """
        if self._explicit_read_ids == read_chapter_ids:
            return
        self._explicit_read_ids = read_chapter_ids
        self._read_ids = self._compute_read_set()
        if self.is_mounted:
            self._rebuild()

    @property
    def selected_chapters(self) -> tuple[Chapter, ...]:
        """Return the currently marked chapters, in display order.

        A snapshot — mutating the returned tuple does not affect the
        widget's marked set. Empty when the user has not marked
        anything.
        """
        return tuple(
            chapter
            for chapter in self._chapters
            if chapter.id in self._selected_ids
        )

    @property
    def current_chapter(self) -> Chapter | None:
        """Return the chapter marked as currently being read, or ``None``."""
        if self._current_chapter_id is None:
            return None
        return self._by_id.get(self._current_chapter_id)

    # ------------------------------------------------------------------ #
    # Actions                                                            #
    # ------------------------------------------------------------------ #

    def action_toggle_select(self) -> None:
        """Toggle the marked state of the cursor row.

        Bound to ``Space``. The cursor row's marked state flips, the
        row's visual updates in place, and a
        :class:`SelectionChanged` message is posted with the new
        marked set. A no-op when the cursor is not on a row.
        """
        chapter = self._cursor_chapter()
        if chapter is None:
            return

        if chapter.id in self._selected_ids:
            self._selected_ids.discard(chapter.id)
        else:
            self._selected_ids.add(chapter.id)

        self._update_select_cell(chapter.id)
        self._update_header()
        self.post_message(self.SelectionChanged(self.selected_chapters))

    def action_select_all(self) -> None:
        """Mark every chapter currently displayed.

        Bound to ``A``. Posts a :class:`SelectionChanged` message even
        when the marked set was already complete — a caller that wants
        to react to the "select all" gesture (a toast, a status line
        update) receives the message unconditionally.
        """
        self._selected_ids = set(self._by_id.keys())
        self._rebuild()
        self.post_message(self.SelectionChanged(self.selected_chapters))

    def action_deselect_all(self) -> None:
        """Clear the marked set.

        Bound to ``Ctrl+D``. A no-op (no message posted) when the set
        was already empty, to avoid spurious messages on an idle
        gesture.
        """
        if not self._selected_ids:
            return
        self._selected_ids.clear()
        self._rebuild()
        self.post_message(self.SelectionChanged(()))

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        """Post a :class:`ChapterActivated` message for the activated row.

        Fired by the :class:`~textual.widgets.DataTable` on ``Enter``
        and on row double-click. The event's ``row_key`` is the chapter
        id the widget supplied when it added the row; mapping it back
        to the :class:`Chapter` is a dictionary lookup.
        """
        chapter_id = str(event.row_key.value)
        chapter = self._by_id.get(chapter_id)
        if chapter is not None:
            self.post_message(self.ChapterActivated(chapter))

    # ------------------------------------------------------------------ #
    # Rendering                                                          #
    # ------------------------------------------------------------------ #

    def _rebuild(self) -> None:
        """Repopulate the table from the current state.

        Called on every change that affects more than one row — chapter
        list replacement, current-chapter move, marked-set bulk change.
        A single-row mark toggle does **not** trigger a rebuild — it
        updates the row's selection cell in place via
        :meth:`_update_select_cell`, which is orders of magnitude
        cheaper.
        """
        table = self.query_one(f"#{_ID_TABLE}", DataTable)
        empty = self.query_one(f"#{_ID_EMPTY}", Static)

        table.clear()

        if not self._chapters:
            table.add_class(_CLASS_HIDDEN)
            empty.update(
                self._translator.translate("chapter.list.empty")
            )
            empty.remove_class(_CLASS_HIDDEN)
            self._update_header()
            return

        empty.add_class(_CLASS_HIDDEN)
        table.remove_class(_CLASS_HIDDEN)

        for chapter in self._chapters:
            table.add_row(
                self._status_cell(chapter),
                self._select_cell(chapter),
                self._number_cell(chapter),
                self._title_cell(chapter),
                self._pages_cell(chapter),
                key=chapter.id,
            )

        self._update_header()

    def _update_select_cell(self, chapter_id: str) -> None:
        """Update only the selection cell of the row for *chapter_id*.

        Called by :meth:`action_toggle_select` — the four other cells of
        the row do not change on a mark toggle. A no-op when the row
        cannot be located (defensive; the caller always passes an id
        that came from a row).
        """
        chapter = self._by_id.get(chapter_id)
        if chapter is None:
            return
        table = self.query_one(f"#{_ID_TABLE}", DataTable)
        try:
            row_index = table.get_row_index(chapter_id)
        except KeyError:
            return
        table.update_cell_at(
            Coordinate(row=row_index, column=1),
            self._select_cell(chapter),
        )

    def _scroll_to(self, chapter_id: str) -> None:
        """Move the table's cursor to the row for *chapter_id*.

        A no-op when the chapter is not in the table — a defensive
        guarantee against a race between the caller's state change and
        the widget's rebuild.
        """
        table = self.query_one(f"#{_ID_TABLE}", DataTable)
        try:
            row_index = table.get_row_index(chapter_id)
        except KeyError:
            return
        table.move_cursor(row=row_index)

    def _update_header(self) -> None:
        """Refresh the header's title and selected-count text."""
        header = self.query_one(f"#{_ID_HEADER}", Label)
        total = len(self._chapters)
        selected = len(self._selected_ids)

        if total == 0:
            header.update("")
            return

        title = self._translator.plural("chapter.list.title", total)
        if selected == 0:
            header.update(title)
            return

        fragment = self._translator.plural(
            "chapter.list.selected", selected
        )
        header.update(f"{title}  ·  {fragment}")

    # ------------------------------------------------------------------ #
    # Cell builders                                                      #
    # ------------------------------------------------------------------ #

    def _status_cell(self, chapter: Chapter) -> Text:
        """Return the status cell content for *chapter*.

        Renders one of the three state glyphs documented in the module
        docstring's "Status column" section. The comparison against
        :attr:`_read_ids` is a set membership test — O(1) per row.
        """
        if chapter.id == self._current_chapter_id:
            return Text(_STATUS_CURRENT, style=_STATUS_CURRENT_STYLE)
        if chapter.id in self._read_ids:
            return Text(_STATUS_READ, style=_STATUS_READ_STYLE)
        return Text(_STATUS_UNREAD, style=_STATUS_UNREAD_STYLE)

    def _select_cell(self, chapter: Chapter) -> Text:
        """Return the selection cell content for *chapter*."""
        if chapter.id in self._selected_ids:
            return Text(_SELECT_ON, style=_SELECT_ON_STYLE)
        return Text(_SELECT_OFF, style=_SELECT_OFF_STYLE)

    @staticmethod
    def _number_cell(chapter: Chapter) -> Text:
        """Return the number cell content for *chapter*.

        Prefers the chapter's ``number``; falls back to a ``"vN"``
        form when only a volume is set; falls back to the placeholder
        when neither is available.
        """
        if chapter.number:
            return Text(chapter.number)
        if chapter.volume:
            return Text(f"v{chapter.volume}")
        return Text(_PLACEHOLDER, style="dim")

    @staticmethod
    def _title_cell(chapter: Chapter) -> Text:
        """Return the title cell content for *chapter*."""
        return Text(chapter.title)

    @staticmethod
    def _pages_cell(chapter: Chapter) -> Text:
        """Return the pages cell content for *chapter*.

        Renders the placeholder in a dim style when the chapter has
        no page count yet — a newly listed chapter from a site that
        does not expose the count on the chapter-listing page.
        """
        if chapter.page_count > 0:
            return Text(str(chapter.page_count))
        return Text(_PLACEHOLDER, style="dim")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _cursor_chapter(self) -> Chapter | None:
        """Return the chapter under the table's cursor, or ``None``."""
        table = self.query_one(f"#{_ID_TABLE}", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
        except (LookupError, KeyError):
            return None
        return self._by_id.get(str(row_key.value))

    def _compute_read_set(self) -> frozenset[str]:
        """Return the effective "read" set for the current state.

        Combines two inputs:

        * The explicit :attr:`_explicit_read_ids` the caller passed.
        * Every chapter that appears **before** the current chapter in
          the displayed list — see the module docstring's "Status
          column" section for why this heuristic is the right default.

        When the current chapter is not in the displayed list (a stale
        reference, or a parent that cleared the read state), only the
        explicit set applies.
        """
        read: set[str] = set(self._explicit_read_ids)
        if not self._current_chapter_id:
            return frozenset(read)

        current_index: int | None = None
        for index, chapter in enumerate(self._chapters):
            if chapter.id == self._current_chapter_id:
                current_index = index
                break
        if current_index is None:
            return frozenset(read)

        for chapter in self._chapters[:current_index]:
            read.add(chapter.id)
        return frozenset(read)
