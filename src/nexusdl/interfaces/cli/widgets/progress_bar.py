"""Textual widget for displaying download progress.

The download pipeline emits a stream of ``download.*`` events; a
:class:`~nexusdl.core.downloader.progress.ProgressTracker` aggregates
them into a :class:`~nexusdl.core.downloader.progress.ProgressSnapshot`;
this widget renders that snapshot. The three layers are cleanly
separated, and each has a single job:

* **Worker / manager / library** — emit events, no rendering.
* **ProgressTracker** — aggregates events into an immutable snapshot,
  no rendering.
* **ProgressBar** — renders a snapshot, no event handling.

The widget therefore has **no knowledge of events**. Its entire public
API is :meth:`ProgressBar.update_snapshot`: the parent screen subscribes
to the event bus, feeds the tracker, and periodically passes the
resulting snapshot to this widget. Any refresh cadence, throttling, or
debouncing belongs to the parent, not to the widget.

What the widget displays
------------------------
Three visual regions, top to bottom:

1. **Header** — a fixed title and a live status line summarising the
   snapshot (``"3 active · 5 done · 1 failed · ETA 2m 15s"``).
2. **Overall bar** — the aggregate progress across every active task,
   rendered by Textual's own :class:`~textual.widgets.ProgressBar`.
3. **Task rows** — one row per active task, followed by the most
   recently completed tasks (up to ``max_completed_visible``). Each row
   is a fixed-column layout: status icon, task label, per-task progress
   bar (Unicode blocks), and a right-aligned info column (percentage,
   ETA, phase or final status).

When the snapshot has no tasks at all, the three regions are replaced
by an empty-state placeholder. When the snapshot has tasks but none are
active, the overall bar reads ``100%`` and the completed tasks remain
visible — the user can see the final state of a just-finished batch
without the panel disappearing.

Info column width and truncation
--------------------------------
The info column reserves a fixed width of :data:`_INFO_WIDTH` cells
and is right-aligned. Every string that goes into it is **truncated**
to that width before being passed to ``Static.update`` — the widget
never relies on Textual's overflow clipping to keep the column
boundaries. The truncation is silent (no ellipsis; see
:func:`_truncate`) but it is **applied uniformly** across all phases,
including the "done" / "cancelled" / "waiting" markers and the
percentage-ETA-phase line for active tasks.

An earlier version only truncated the failure case; the active-task
line (``"100%  45m 12s  packaging"`` — 25 characters) could overflow
the 24-cell column and be clipped by the CSS, cutting off the phase
label mid-word. The current code sizes the column at
:data:`_INFO_WIDTH` = 30 cells to fit the worst realistic case (a
three-digit percentage, a duration up to hours, and a "packaging"
label) and truncates every phase uniformly so a future locale that
produces a longer string cannot silently break the layout.

Incremental rendering
---------------------
:meth:`update_snapshot` is designed to be called many times per second.
It **reconciles** the widget tree against the new snapshot rather than
rebuilding it:

* If the set of visible tasks (and their order) is unchanged, each row
  is updated in place via :meth:`textual.widgets.Static.update` — no
  widget construction, no DOM churn.
* Only when the visible set changes (a task becomes terminal, a new
  task appears, an old one scrolls out of the ``max_completed_visible``
  window) are rows added or removed, and even then only the changed
  rows.

This matters because the pipeline can produce 20+ progress events per
second for a chapter with hundreds of pages, and a naive full rebuild
per update would visibly flicker. The reconciliation keeps the widget
correct and smooth without requiring the parent to throttle its calls.

Task ordering
-------------
Active tasks appear in **submission order** (oldest first) — matching
the order the ``ProgressTracker`` produces from its snapshots. Terminal
tasks appear **below** the active ones, most-recently-completed first.
This is the layout a download manager uses: the things you are watching
in progress at the top, the things that just finished underneath.

The ordering relies on ``ProgressSnapshot.tasks`` preserving insertion
order — a contract documented on the tracker (see
:mod:`nexusdl.core.downloader.progress`). If a future tracker version
returns tasks in a different order (for example, sorted by
``task_id``), the widget's "most-recently-completed first" promise
silently degrades to "last in id order first". The tracker's docstring
is the authority for this invariant; the widget does not defensively
re-sort because sorting by ``task_id`` would produce a *different*
wrong order and would hide the tracker's regression rather than
surface it.

Task labels
-----------
A :class:`~nexusdl.core.downloader.progress.TaskProgress` carries only
``task_id`` and ``chapter_id`` — no title. The widget therefore accepts
an optional ``title_resolver`` callable that maps a ``TaskProgress`` to
a human-readable label (typically the chapter title). When ``None``
(the default), the widget falls back to a truncated ``chapter_id``.
The parent screen has the task → title mapping (it created the tasks)
and should pass the resolver at construction time.

Character choices
-----------------
The per-task progress bar uses ``█`` (U+2588) for filled cells and
``░`` (U+2591) for empty ones — the WGL4 block-elements range that
renders identically in every terminal font the pipeline targets. The
status icons (``⟳`` / ``✓`` / ``✗`` / ``⊘`` / ``⏳``) are likewise
chosen from the non-emoji symbol repertoire. An emoji-based bar would
break in mono-space terminals and get rendered as two cells on some
fonts, breaking the fixed-column layout.

Terminal safety
---------------
Every user-visible string built by the widget is either a translation
(``str`` from the translator) or a widget-level ``Text`` produced via
:meth:`textual.content.Text.append`. No markup is ever interpreted — a
chapter title containing ``[`` or ``]`` renders literally, matching the
policy of the sibling :mod:`site_selector` widget.

Dependency graph (no cycles)::

    nexusdl.core.downloader.progress    ─┐
    nexusdl.core.i18n                    ├──► interfaces.cli.widgets.progress_bar
    nexusdl.core.utils.time              │
    textual                              │

The widget imports from :mod:`nexusdl.core` through its public
sub-packages only. No private attribute of any core module is touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Text
from textual.widgets import Label, ProgressBar as TextualProgressBar, Static

from nexusdl.core.downloader.progress import (
    PHASE_CANCELLED,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_PACKAGING,
    PHASE_PAGES,
    PHASE_PENDING,
    ProgressSnapshot,
    TaskProgress,
)
from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.utils.time import format_duration

if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = ["ProgressBar"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so a future CSS refactor has a
#: single place to update the selectors and the ``DEFAULT_CSS`` string
#: below cannot drift from the ``query_one`` calls.
_ID_HEADER: Final[str] = "progress-bar-header"
_ID_TITLE: Final[str] = "progress-bar-title"
_ID_STATUS: Final[str] = "progress-bar-status"
_ID_OVERALL: Final[str] = "progress-bar-overall"
_ID_ROWS: Final[str] = "progress-bar-rows"
_ID_EMPTY: Final[str] = "progress-bar-empty"

#: CSS classes used by the widget's DOM and its ``DEFAULT_CSS``.
_CLASS_HIDDEN: Final[str] = "hidden"
_CLASS_ROW: Final[str] = "task-row"
_CLASS_ICON: Final[str] = "task-icon"
_CLASS_NAME: Final[str] = "task-name"
_CLASS_BAR: Final[str] = "task-bar"
_CLASS_INFO: Final[str] = "task-info"

#: Width of each per-task progress bar, in cells. Twenty cells is the
#: smallest width that still reads as "progress" while leaving room for
#: the label and the info column in an 80-column terminal.
_BAR_WIDTH: Final[int] = 20

#: Filled and empty block characters for the per-task bar. See the
#: module docstring's "Character choices" section.
_BAR_FILLED: Final[str] = "█"
_BAR_EMPTY: Final[str] = "░"

#: Default maximum number of terminal tasks kept visible below the
#: active ones. The full snapshot may contain hundreds of completed
#: tasks after a long session; only the most recent few remain useful
#: in the panel.
_DEFAULT_MAX_COMPLETED_VISIBLE: Final[int] = 5

#: Status icon per phase. Keyed by the ``Phase`` literal values from
#: :mod:`nexusdl.core.downloader.progress`.
_PHASE_ICON: Final[dict[str, str]] = {
    PHASE_PENDING: "⏳",
    PHASE_PAGES: "⟳",
    PHASE_PACKAGING: "⟳",
    PHASE_DONE: "✓",
    PHASE_FAILED: "✗",
    PHASE_CANCELLED: "⊘",
}

#: Human-readable phase label for the two active phases. Used to tell
#: the user what the task is currently doing — the percentage alone
#: does not distinguish page downloads from packaging.
_PHASE_LABEL: Final[dict[str, str]] = {
    PHASE_PAGES: "pages",
    PHASE_PACKAGING: "packaging",
}

#: Width of the info column, in cells. Sized for the worst realistic
#: case: ``"100%  23h 59m 59s  packaging"`` (28 characters) with two
#: cells of slack. Every string is truncated to this width by
#: :func:`_truncate` before being passed to ``Static.update`` — see
#: the module docstring's "Info column width and truncation" section.
_INFO_WIDTH: Final[int] = 30


# --------------------------------------------------------------------------- #
# ProgressBar                                                                 #
# --------------------------------------------------------------------------- #


class ProgressBar(Vertical):
    """A Textual widget for displaying download progress.

    See the module docstring for the design contract, the incremental
    rendering strategy, and the character choices. The widget is a
    **display-only** container: it has no bindings, performs no I/O, and
    does not subscribe to the event bus. The parent screen owns every
    one of those concerns.

    Args:
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        title_resolver: Optional callable mapping a
            :class:`~nexusdl.core.downloader.progress.TaskProgress` to a
            display label. When ``None``, the widget falls back to a
            truncated ``chapter_id``. The parent screen should provide
            a resolver when it has the task → title mapping (which it
            almost always does).
        max_completed_visible: Maximum number of terminal tasks kept
            visible below the active ones. ``0`` hides completed tasks
            entirely; a large value shows the full backlog. Defaults to
            :data:`_DEFAULT_MAX_COMPLETED_VISIBLE` (5).
        id: Optional Textual widget id. Typically the parent screen
            assigns one so it can query the widget later.
        classes: Optional Textual CSS classes. Same purpose as *id*.
    """

    #: Default CSS, applied automatically by Textual. The application's
    #: ``theme.tcss`` may override any of these rules by targeting
    #: ``ProgressBar`` or the ``#progress-bar-*`` ids.
    DEFAULT_CSS: ClassVar[str] = f"""
    ProgressBar {{
        height: 1fr;
        border: solid $primary;
        padding: 0;
    }}
    #{_ID_HEADER} {{
        height: 1;
        padding: 0 1;
        background: $surface;
    }}
    #{_ID_TITLE} {{
        width: auto;
        text-style: bold;
    }}
    #{_ID_STATUS} {{
        width: 1fr;
        text-align: right;
        color: $text-muted;
    }}
    #{_ID_OVERALL} {{
        height: 3;
        padding: 0 2;
    }}
    #{_ID_ROWS} {{
        height: 1fr;
        padding: 0 1;
    }}
    .{_CLASS_ROW} {{
        height: 1;
    }}
    .{_CLASS_ICON} {{
        width: 3;
    }}
    .{_CLASS_NAME} {{
        width: 1fr;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    .{_CLASS_BAR} {{
        width: {_BAR_WIDTH};
    }}
    .{_CLASS_INFO} {{
        width: {_INFO_WIDTH};
        text-align: right;
        color: $text-muted;
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

    def __init__(
        self,
        *,
        translator: Translator | None = None,
        title_resolver: Callable[[TaskProgress], str] | None = None,
        max_completed_visible: int = _DEFAULT_MAX_COMPLETED_VISIBLE,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._translator = (
            translator if translator is not None else get_default_translator()
        )
        self._title_resolver = title_resolver
        self._max_completed_visible = max(0, max_completed_visible)
        #: Latest snapshot, whether or not the widget has been mounted.
        #: ``on_mount`` renders it if it was set before the widget was
        #: composed.
        self._snapshot: ProgressSnapshot | None = None
        #: ``task_id`` → the five widgets composing that task's row
        #: (row, icon, name, bar, info). Used by the incremental
        #: renderer.
        self._task_widgets: dict[
            str, tuple[Horizontal, Static, Static, Static, Static]
        ] = {}
        #: The task ids currently displayed, in display order. Compared
        #: against the new order on each update to decide between an
        #: in-place update and a rebuild.
        self._visible_order: tuple[str, ...] = ()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the widget's four static regions.

        Header, overall bar, scrollable row container, empty-state
        placeholder. Only one of ``rows`` and ``empty`` is visible at
        any time — toggled via the ``hidden`` class by the renderer.
        """
        with Horizontal(id=_ID_HEADER):
            yield Label(
                self._translator.translate("downloads.progress.title"),
                id=_ID_TITLE,
            )
            yield Label("", id=_ID_STATUS)
        yield TextualProgressBar(
            total=100,
            show_eta=False,
            show_percentage=True,
            id=_ID_OVERALL,
        )
        yield VerticalScroll(id=_ID_ROWS)
        yield Static("", id=_ID_EMPTY, classes=_CLASS_HIDDEN)

    def on_mount(self) -> None:
        """Render whatever snapshot was set before mount, if any."""
        self._render(self._snapshot)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def update_snapshot(self, snapshot: ProgressSnapshot) -> None:
        """Replace the displayed state with a fresh snapshot.

        Idempotent, cheap when nothing has changed, and safe to call at
        whatever cadence the parent chooses. The widget reconciles its
        DOM against the snapshot: task rows whose progress has changed
        are updated in place; rows for tasks that have appeared,
        disappeared, or moved are added or removed.

        A call made before the widget is mounted is safe — the snapshot
        is stored and rendered by :meth:`on_mount`. A call made during
        unmount is a no-op: the widget's children may already be
        detached, and rendering against a torn-down tree would raise
        ``NoMatches`` from ``query_one``.

        Args:
            snapshot: The current progress snapshot, as produced by
                :meth:`~nexusdl.core.downloader.progress.ProgressTracker.snapshot`.
        """
        self._snapshot = snapshot
        if self.is_mounted:
            self._render(snapshot)

    # ------------------------------------------------------------------ #
    # Rendering                                                          #
    # ------------------------------------------------------------------ #

    def _render(self, snapshot: ProgressSnapshot | None) -> None:
        """Reconcile the widget tree with *snapshot*.

        Handles three states:

        * **No snapshot / empty snapshot** — the empty-state placeholder
          is shown; the row container and the overall bar are hidden.
        * **Snapshot with active tasks** — the overall bar reflects the
          aggregate progress; the row container holds one row per
          visible task.
        * **Snapshot with only terminal tasks** — the overall bar reads
          100%; the row container holds up to
          ``max_completed_visible`` rows for the most recently finished
          tasks.
        """
        # Query once; every branch below uses the same three widgets.
        overall = self.query_one(f"#{_ID_OVERALL}", TextualProgressBar)
        status = self.query_one(f"#{_ID_STATUS}", Label)
        rows = self.query_one(f"#{_ID_ROWS}", VerticalScroll)
        empty = self.query_one(f"#{_ID_EMPTY}", Static)

        if snapshot is None or snapshot.total == 0:
            overall.update(progress=0)
            status.update(
                self._translator.translate("downloads.progress.empty_status")
            )
            self._clear_rows()
            rows.add_class(_CLASS_HIDDEN)
            empty.update(
                self._translator.translate("downloads.progress.no_downloads")
            )
            empty.remove_class(_CLASS_HIDDEN)
            return

        # Overall bar and status are cheap to update unconditionally.
        overall.update(progress=snapshot.overall_progress * 100)
        status.update(self._status_text(snapshot))

        visible = self._visible_tasks(snapshot)
        if not visible:
            self._clear_rows()
            rows.add_class(_CLASS_HIDDEN)
            empty.update(
                self._translator.translate("downloads.progress.no_active")
            )
            empty.remove_class(_CLASS_HIDDEN)
            return

        empty.add_class(_CLASS_HIDDEN)
        rows.remove_class(_CLASS_HIDDEN)

        new_order = tuple(task.task_id for task in visible)
        if new_order != self._visible_order:
            self._rebuild_rows(visible)
            self._visible_order = new_order
        else:
            self._update_rows_in_place(visible)

    def _visible_tasks(
        self, snapshot: ProgressSnapshot
    ) -> tuple[TaskProgress, ...]:
        """Return the tasks to display, in display order.

        Active tasks first, in submission order (the order the snapshot
        preserves from the tracker); then up to ``max_completed_visible``
        terminal tasks, most recently finished first.

        The ordering relies on ``ProgressSnapshot.tasks`` preserving
        insertion order — see the module docstring's "Task ordering"
        section. The widget does **not** re-sort: sorting by task id
        would produce a different wrong order and would hide the
        tracker's regression rather than surface it.
        """
        active = tuple(t for t in snapshot.tasks if not t.is_terminal)
        terminal = tuple(t for t in snapshot.tasks if t.is_terminal)

        if self._max_completed_visible > 0:
            recent = terminal[-self._max_completed_visible :]
            # Reverse so the most-recently-finished appears on top of
            # the completed section.
            recent = recent[::-1]
        else:
            recent = ()

        return active + recent

    # ------------------------------------------------------------------ #
    # Row construction and update                                        #
    # ------------------------------------------------------------------ #

    def _rebuild_rows(self, tasks: tuple[TaskProgress, ...]) -> None:
        """Replace the row container's children with fresh rows for *tasks*.

        Called only when the visible set has changed — see
        :meth:`_render`'s use of :attr:`_visible_order`. Existing rows
        are dropped without being inspected; a rebuild is a fresh
        construction of the entire visible set.
        """
        rows_container = self.query_one(f"#{_ID_ROWS}", VerticalScroll)
        rows_container.remove_children()
        self._task_widgets.clear()

        new_widgets: list[Horizontal] = []
        for task in tasks:
            widgets = self._make_row(task)
            self._task_widgets[task.task_id] = widgets
            new_widgets.append(widgets[0])

        if new_widgets:
            rows_container.mount_all(new_widgets)

    def _update_rows_in_place(self, tasks: tuple[TaskProgress, ...]) -> None:
        """Update each visible row's content without touching the DOM.

        Called when the visible set and its order have not changed —
        only progress values, phase labels, and info strings need to be
        refreshed. Every row in *tasks* has a live widget tuple in
        :attr:`_task_widgets`; the map is not touched.
        """
        for task in tasks:
            widgets = self._task_widgets.get(task.task_id)
            if widgets is None:  # pragma: no cover — defensive only
                continue
            self._update_row_in_place(widgets, task)

    def _clear_rows(self) -> None:
        """Remove every row and reset the reconciliation state."""
        rows_container = self.query_one(f"#{_ID_ROWS}", VerticalScroll)
        rows_container.remove_children()
        self._task_widgets.clear()
        self._visible_order = ()

    def _make_row(
        self, task: TaskProgress
    ) -> tuple[Horizontal, Static, Static, Static, Static]:
        """Construct a task row from five widgets.

        Returns the row and its four child ``Static`` widgets so that
        subsequent updates can call ``.update()`` on each without a
        second ``query_one`` traversal per row per refresh.
        """
        icon = Static(self._icon(task), classes=_CLASS_ICON)
        name = Static(self._task_label(task), classes=_CLASS_NAME)
        bar = Static(self._bar_text(task), classes=_CLASS_BAR)
        info = Static(self._info_text(task), classes=_CLASS_INFO)
        row = Horizontal(icon, name, bar, info, classes=_CLASS_ROW)
        return row, icon, name, bar, info

    def _update_row_in_place(
        self,
        widgets: tuple[Horizontal, Static, Static, Static, Static],
        task: TaskProgress,
    ) -> None:
        """Refresh the four child ``Static`` widgets with *task*'s data."""
        _row, icon, name, bar, info = widgets
        # The icon and name rarely change during a task's lifetime; a
        # phase transition (pages → packaging → done) does change the
        # icon, and a future resolver that depends on the phase would
        # change the name. Updating both is cheap enough to do
        # unconditionally.
        icon.update(self._icon(task))
        name.update(self._task_label(task))
        bar.update(self._bar_text(task))
        info.update(self._info_text(task))

    # ------------------------------------------------------------------ #
    # Row content builders                                               #
    # ------------------------------------------------------------------ #

    def _icon(self, task: TaskProgress) -> str:
        """Return the status icon for *task*."""
        return _PHASE_ICON.get(task.phase, "·")

    def _task_label(self, task: TaskProgress) -> str:
        """Return the display label for *task*.

        Uses the parent-supplied ``title_resolver`` when available;
        otherwise falls back to a truncated ``chapter_id`` (first twelve
        characters), which is enough to disambiguate concurrent tasks
        visually while remaining short enough to fit the name column.
        """
        if self._title_resolver is not None:
            return self._title_resolver(task)
        return task.chapter_id[:12]

    def _bar_text(self, task: TaskProgress) -> Text:
        """Return the per-task bar rendered as ``Text``.

        Filled portion is coloured by phase: red for a failure, green
        for a completion, dim for a cancellation, cyan while in
        progress. The empty portion is uniformly dim. No markup is
        interpreted, so a caller cannot accidentally inject styles via
        a task value.
        """
        filled = int(task.progress * _BAR_WIDTH)
        empty = _BAR_WIDTH - filled

        if task.phase == PHASE_FAILED:
            style = "red"
        elif task.phase == PHASE_DONE:
            style = "green"
        elif task.phase == PHASE_CANCELLED:
            style = "dim"
        else:
            style = "cyan"

        text = Text()
        if filled > 0:
            text.append(_BAR_FILLED * filled, style=style)
        if empty > 0:
            text.append(_BAR_EMPTY * empty, style="dim")
        return text

    def _info_text(self, task: TaskProgress) -> str:
        """Return the right-aligned info column content for *task*.

        Content depends on the task's phase:

        * ``done`` — a short completion marker.
        * ``failed`` — the error message, truncated to fit the column.
        * ``cancelled`` — ``"cancelled"``.
        * ``pending`` — ``"waiting"``.
        * ``pages`` / ``packaging`` — percentage, ETA (when known), and
          the phase label.

        **Every** branch passes its result through :func:`_truncate` to
        :data:`_INFO_WIDTH`, not just the ``failed`` one. An earlier
        version only truncated the error string and could overflow the
        column for an active task whose phase label combined with a
        long ETA (``"100%  23h 59m 59s  packaging"`` is 28 cells
        against a 24-cell column). The uniform truncation is defensive
        against a future locale that produces a longer label.
        """
        if task.phase == PHASE_DONE:
            return _truncate("done", _INFO_WIDTH)
        if task.phase == PHASE_FAILED:
            err = task.error or "failed"
            return _truncate(err, _INFO_WIDTH)
        if task.phase == PHASE_CANCELLED:
            return _truncate("cancelled", _INFO_WIDTH)
        if task.phase == PHASE_PENDING:
            return _truncate("waiting", _INFO_WIDTH)

        pct = int(task.progress * 100)
        phase_label = _PHASE_LABEL.get(task.phase, "")
        eta = task.remaining_seconds
        if eta is not None and eta > 0:
            line = f"{pct}%  {format_duration(eta)}  {phase_label}"
        else:
            line = f"{pct}%  {phase_label}"
        return _truncate(line, _INFO_WIDTH)

    def _status_text(self, snapshot: ProgressSnapshot) -> str:
        """Return the header status line for *snapshot*.

        Format: ``"<n> active · <m> done · <k> failed · ETA <duration>"``,
        omitting any zero-count section and any ETA that is not
        applicable (no active tasks, or insufficient data).
        """
        parts: list[str] = [f"{len(snapshot.active)} active"]
        if snapshot.completed:
            parts.append(f"{snapshot.completed} done")
        if snapshot.failed:
            parts.append(f"{snapshot.failed} failed")
        if snapshot.cancelled:
            parts.append(f"{snapshot.cancelled} cancelled")

        base = " · ".join(parts)
        eta = snapshot.eta_seconds
        if eta is not None and eta > 0:
            base = f"{base} · ETA {format_duration(eta)}"
        return base


# --------------------------------------------------------------------------- #
# Module-level helpers                                                        #
# --------------------------------------------------------------------------- #


def _truncate(value: str, width: int) -> str:
    """Truncate *value* to at most *width* characters, no ellipsis.

    The info column already reserves a fixed width; an ellipsis would
    overflow into the next column. Dropping the tail is the safest
    behaviour for an error message where the beginning is typically the
    most informative part — and for the active-task line where the
    phase label ("packaging") is the part a reader would most want to
    see, the column is sized (see :data:`_INFO_WIDTH`) so that the
    worst realistic combination still fits with slack. Truncation
    therefore never fires in normal operation; it is a defensive guard
    for a future locale or phase label that exceeds the budget.
    """
    if len(value) <= width:
        return value
    return value[:width]
