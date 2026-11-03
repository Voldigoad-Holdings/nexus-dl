"""Textual widget for displaying a live stream of loguru log records.

The pipeline logs through :mod:`loguru` via per-module bound loggers —
every line carries a ``component`` binding naming the emitting module
(``"downloader.worker"``, ``"session.http"``, …). This widget renders
that stream in the TUI, with level colouring, per-component tagging,
level filtering, and pause/resume so a user can read historical output
without the display scrolling out from under them.

The widget is a **display surface**: it does not configure loguru, does
not subscribe to the logger, and does not buffer through the event bus.
The composition root wires the stream with one call::

    viewer = LogViewer()

    def _sink(message) -> None:
        viewer.push_entry(LogEntry.from_loguru_record(message.record))

    logger.add(_sink, level=settings.logging.level)

:meth:`LogViewer.push_entry` is **thread-safe and non-blocking** — a
loguru sink may run in any thread (including worker threads spawned by
:mod:`asyncio.to_thread`), and the widget's drain loop runs on the app
thread. The bridge is a :class:`queue.SimpleQueue`: the sink enqueues,
a :class:`textual.timer.Timer` started on mount drains every 100 ms.

Ring buffer and filter rebuild
------------------------------
Two buffers, two purposes:

* **Pending queue** — a :class:`queue.SimpleQueue` holding entries the
  sink has pushed since the last drain. Thread-safe and unbounded (in
  practice drained every 100 ms).
* **Ring buffer** — a :class:`collections.deque` with
  ``maxlen=_RING_SIZE`` holding the most recent entries. Bounded so a
  long-running session's memory footprint stays flat.

:class:`~textual.widgets.RichLog` displays the **currently visible**
entries (those passing the level filter). When the user changes the
filter, the display is rebuilt from the ring buffer — so the filtered
view shows the maximum amount of history the widget retains, not just
entries logged after the change. Entries older than ``_RING_SIZE`` are
not retained (documented).

Pause semantics
---------------
:meth:`LogViewer.toggle_pause` stops the widget from auto-scrolling to
the bottom after each new entry. Entries continue to be **appended**
while paused — the user can still scroll down manually to see them, and
the display remains live. When pause is released, the display snaps to
the bottom (the most recent entry).

This is the standard "freeze the view" model used by every log viewer
in every IDE. The alternative — stopping entry ingestion while paused —
would silently drop log lines from the ring buffer, which is a data-loss
surprise the user did not ask for.

Localisation
------------
The widget introduces five translation keys under ``logs.viewer.*``:

* ``logs.viewer.title`` — the header title.
* ``logs.viewer.level`` — the level filter placeholder.
* ``logs.viewer.pause`` / ``logs.viewer.resume`` — the toggle button's
  two labels.
* ``logs.viewer.clear`` — the clear button's label.

A missing key renders the key verbatim, matching the contract every
other widget in this package follows. See :mod:`nexusdl.core.i18n`.

Level filtering
---------------
The level filter is a
:class:`~textual.widgets.Select` whose value is one of ``"TRACE"``,
``"DEBUG"``, ``"INFO"``, ``"SUCCESS"``, ``"WARNING"``, ``"ERROR"``,
``"CRITICAL"``. The **floor** is the selected level: entries with a
level at or above the floor are shown, entries below are filtered out.
This matches the semantics of ``logger.add(level=...)`` — a filter of
``INFO`` hides ``DEBUG`` and ``TRACE``, not ``WARNING`` and above.

Colour palette
--------------
Level colours follow the same conventions as :mod:`loguru`'s default
formatter — ``DEBUG`` dim cyan, ``INFO`` default, ``WARNING`` yellow,
``ERROR`` red, ``CRITICAL`` bold red — so a user reading both the TUI
and the stderr output sees the same visual language. The component tag
is uniformly dim cyan; a per-component colour scheme was considered and
rejected: with dozens of components, distinct colours become
indistinguishable after about six, and a card that visually screams
"look, a different component" is noise, not signal.

Character choices
-----------------
The pause / resume button label uses the Unicode media-control symbols
``❚❚`` (U+275A HEAVY VERTICAL BAR, doubled) for pause and ``▶``
(U+25B6 BLACK RIGHT-POINTING TRIANGLE) for resume — the same glyphs
every terminal-based media player uses. Not emoji, so they render in
mono-space cells identically across fonts.

Dependency graph (no cycles)::

    nexusdl.core.i18n    ─┐
    textual               ├──► interfaces.cli.widgets.log_viewer
    collections, queue    │
    datetime             ─┘

The widget does **not** import :mod:`loguru` — the composition root's
sink extracts the record fields and calls
:meth:`LogEntry.from_loguru_record` with a plain mapping. This keeps
the widget testable without a running loguru instance, and keeps the
dependency direction strictly ``interfaces → core`` with no leak into
loguru's internal types.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any, ClassVar, Final

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Text
from textual.widgets import Button, Label, RichLog, Select

from nexusdl.core.i18n import Translator, get_default_translator

if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = ["LogEntry", "LogViewer"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Textual IDs. Kept as private constants so a future CSS refactor has a
#: single place to update the selectors and the ``DEFAULT_CSS`` string
#: below cannot drift from the ``query_one`` calls.
_ID_HEADER: Final[str] = "log-viewer-header"
_ID_TITLE: Final[str] = "log-viewer-title"
_ID_LEVEL: Final[str] = "log-viewer-level"
_ID_PAUSE: Final[str] = "log-viewer-pause"
_ID_CLEAR: Final[str] = "log-viewer-clear"
_ID_BODY: Final[str] = "log-viewer-body"

#: Level floor ordering. Higher numbers are more severe. The dict is the
#: single source of truth for both the ``Select`` widget's options and
#: the numeric floor comparison in :meth:`LogViewer._passes_filter`.
_LEVEL_ORDER: Final[dict[str, int]] = {
    "TRACE": 0,
    "DEBUG": 1,
    "INFO": 2,
    "SUCCESS": 3,
    "WARNING": 4,
    "ERROR": 5,
    "CRITICAL": 6,
}

#: Rich-text style per level. Matches loguru's own default formatter so
#: a user reading both streams sees the same visual language.
_LEVEL_STYLE: Final[dict[str, str]] = {
    "TRACE": "dim",
    "DEBUG": "dim cyan",
    "INFO": "",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold red",
}

#: Style for the per-component tag appended after the level.
_COMPONENT_STYLE: Final[str] = "dim cyan"

#: Style for the timestamp column.
_TIMESTAMP_STYLE: Final[str] = "dim"

#: Style for exception tracebacks.
_EXCEPTION_STYLE: Final[str] = "dim red"

#: Maximum number of entries retained in the ring buffer. 2000 is
#: roughly twenty seconds at 100 entries/second, or many minutes at the
#: pipeline's typical rate (a few entries per second). Enough history
#: for a filter-change rebuild; bounded enough that a multi-hour session
#: does not exhaust memory.
_RING_SIZE: Final[int] = 2000

#: Maximum number of visible lines retained by the RichLog. Matches
#: ``_RING_SIZE`` — the ring buffer is the sole source of truth, and no
#: path writes more than ``_RING_SIZE`` entries to the display between
#: clears, so the two never disagree.
_MAX_VISIBLE_LINES: Final[int] = _RING_SIZE

#: Drain interval for the pending queue, in seconds. 100 ms is fine for
#: log display — the user cannot read faster, and the sub-second batched
#: update avoids Textual DOM churn on high-frequency sources.
_DRAIN_INTERVAL_SECONDS: Final[float] = 0.1

#: Fixed width, in characters, of the level column. Chosen to fit
#: ``"CRITICAL"`` (8 chars) with a trailing space. Keeps the column
#: aligned across every level so the message start is vertically
#: consistent.
_LEVEL_COLUMN_WIDTH: Final[int] = 8

#: Fixed width, in characters, of the timestamp column
#: (``"HH:MM:SS.mmm"``). Twelve characters of content plus one space.
_TIMESTAMP_COLUMN_WIDTH: Final[int] = 12


# --------------------------------------------------------------------------- #
# LogEntry                                                                    #
# --------------------------------------------------------------------------- #


class LogEntry:
    """One log record, in the widget's display shape.

    Immutable, plain-data — no loguru dependency, no async, no side
    effects. Built by the composition root's loguru sink via
    :meth:`from_loguru_record`; built directly in tests.

    Attributes:
        level: The log level name, upper-cased (``"INFO"``, ``"ERROR"``,
            …). The widget matches it against :data:`_LEVEL_ORDER` for
            filtering, and against :data:`_LEVEL_STYLE` for colouring.
            A level name the widget does not recognise is treated as
            ``INFO`` for filtering and rendered with the default style —
            the widget never rejects a log record.
        timestamp: Timezone-aware datetime, produced by loguru's record
            field ``time``. The widget displays only the time-of-day
            component; the full timestamp is retained for a future
            "export" or "inspect line" feature.
        component: The ``component`` extra set by
            :func:`~nexusdl.core.logger.get_logger` (or by a module's
            direct ``logger.bind(component=...)`` call). Empty when the
            record was not bound — the widget then renders no tag.
        message: The formatted log message.
        exception: A formatted exception traceback, when the record
            carries one. ``None`` for the common case. Rendered on
            separate lines below the main record, styled dim red.
    """

    __slots__ = ("component", "exception", "level", "message", "timestamp")

    def __init__(
        self,
        *,
        level: str,
        timestamp: datetime,
        component: str = "",
        message: str,
        exception: str | None = None,
    ) -> None:
        self.level = level.upper()
        self.timestamp = timestamp
        self.component = component
        self.message = message
        self.exception = exception

    @classmethod
    def from_loguru_record(cls, record: Mapping[str, Any]) -> LogEntry:
        """Build a :class:`LogEntry` from a loguru record mapping.

        The composition root's sink calls this with the ``record``
        attribute of a loguru :class:`~loguru.Message`. Every field is
        read defensively — a loguru record with a missing or malformed
        field is rendered with a sensible default rather than raising,
        because a logging widget that crashes the app is worse than a
        log line that loses a field.

        Args:
            record: The ``record`` mapping from a loguru message.
                Typically a :class:`dict`; the method accepts any
                mapping.

        Returns:
            A :class:`LogEntry` populated from the record.
        """
        level_raw: Any = record.get("level")
        level_name: str = (
            getattr(level_raw, "name", None)
            or str(level_raw)
            or "INFO"
        )

        timestamp_raw: Any = record.get("time")
        if isinstance(timestamp_raw, datetime):
            timestamp = timestamp_raw
        else:
            timestamp = datetime.now(UTC)

        extra_raw: Any = record.get("extra") or {}
        if isinstance(extra_raw, Mapping):
            component = str(extra_raw.get("component", "") or "")
        else:
            component = ""

        message_raw: Any = record.get("message")
        message = str(message_raw) if message_raw is not None else ""

        exc_raw: Any = record.get("exception")
        exception: str | None = None
        if exc_raw is not None:
            try:
                exception = exc_raw.format()
            except Exception:  # noqa: BLE001 — best-effort formatting
                exception = str(exc_raw)

        return cls(
            level=level_name,
            timestamp=timestamp,
            component=component,
            message=message,
            exception=exception,
        )

    def __repr__(self) -> str:
        """Return a compact debug representation.

        Deliberately omits the exception traceback — that can be
        hundreds of lines and would flood a test's failure message.
        """
        return (
            f"LogEntry(level={self.level!r}, "
            f"component={self.component!r}, "
            f"message={self.message!r}, "
            f"has_exception={self.exception is not None})"
        )


# --------------------------------------------------------------------------- #
# LogViewer                                                                   #
# --------------------------------------------------------------------------- #


class LogViewer(Vertical):
    """A Textual widget displaying a live stream of log records.

    See the module docstring for the stream contract, the ring buffer /
    filter rebuild model, the pause semantics, and the colour palette.

    Args:
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used — see
            :func:`~nexusdl.core.i18n.get_default_translator`.
        initial_level: The level filter the widget starts with. Must be
            one of the keys in :data:`_LEVEL_ORDER`; a value not in the
            dict is coerced to ``"INFO"``. Defaults to ``"INFO"``.
        id: Optional Textual widget id. Typically the parent screen
            assigns one so it can query the widget later.
        classes: Optional Textual CSS classes. Same purpose as *id*.
    """

    #: Default CSS for the widget, applied automatically by Textual. The
    #: application's ``theme.tcss`` may override any rule by targeting
    #: ``LogViewer`` or the ``#log-viewer-*`` ids.
    DEFAULT_CSS: ClassVar[str] = f"""
    LogViewer {{
        height: 1fr;
        border: solid $primary;
    }}
    #{_ID_HEADER} {{
        height: 3;
        padding: 0 1;
        background: $surface;
    }}
    #{_ID_TITLE} {{
        width: auto;
        margin-right: 2;
        text-style: bold;
    }}
    #{_ID_LEVEL} {{
        width: 14;
        margin-right: 1;
    }}
    #{_ID_PAUSE} {{
        width: auto;
        min-width: 10;
        margin-right: 1;
    }}
    #{_ID_CLEAR} {{
        width: auto;
        min-width: 8;
    }}
    #{_ID_BODY} {{
        height: 1fr;
        padding: 0 1;
    }}
    """

    #: Keyboard bindings. ``Ctrl+L`` is the conventional "clear log"
    #: shortcut and does not conflict with any Textual default. Pause is
    #: button-only — the button is always visible, and adding a
    #: modifier-heavy binding for a two-state toggle was judged not
    #: worth the cognitive load.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+l", "clear", "Clear log", show=False),
    ]

    def __init__(
        self,
        *,
        translator: Translator | None = None,
        initial_level: str = "INFO",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._translator = (
            translator if translator is not None else get_default_translator()
        )
        normalised_level = (
            initial_level.upper()
            if initial_level.upper() in _LEVEL_ORDER
            else "INFO"
        )
        self._level_floor: int = _LEVEL_ORDER[normalised_level]
        self._level_name: str = normalised_level
        self._paused: bool = False

        #: The persistent ring buffer. Bounded by ``_RING_SIZE``.
        self._ring: deque[LogEntry] = deque(maxlen=_RING_SIZE)

        #: The pending queue, filled by :meth:`push_entry` from any
        #: thread and drained by :meth:`_drain` on the app thread.
        #: Reassigned on :meth:`clear` to drop in-flight entries.
        self._pending: SimpleQueue[LogEntry] = SimpleQueue()

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the widget's header and RichLog body."""
        with Horizontal(id=_ID_HEADER):
            yield Label(
                self._translator.translate("logs.viewer.title"),
                id=_ID_TITLE,
            )
            yield Select(
                options=[
                    (name, name) for name in _LEVEL_ORDER
                ],
                value=self._level_name,
                allow_blank=False,
                id=_ID_LEVEL,
            )
            yield Button(
                self._pause_label(),
                id=_ID_PAUSE,
                variant="warning" if self._paused else "default",
            )
            yield Button(
                self._translator.translate("logs.viewer.clear"),
                id=_ID_CLEAR,
                variant="error",
            )
        yield RichLog(
            id=_ID_BODY,
            max_lines=_MAX_VISIBLE_LINES,
            wrap=True,
            highlight=False,
        )

    def on_mount(self) -> None:
        """Drain any pre-mount backlog and start the interval timer.

        The RichLog is focused on mount so keyboard scrolling works
        immediately — the standard behaviour of a Textual log panel.
        """
        self._drain()
        self.set_interval(_DRAIN_INTERVAL_SECONDS, self._drain)
        self.query_one(f"#{_ID_BODY}", RichLog).focus()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def push_entry(self, entry: LogEntry) -> None:
        """Enqueue a log entry for display.

        **Thread-safe and non-blocking** — may be called from a loguru
        sink running in any thread, including worker threads. The entry
        is queued and processed on the app thread by the drain timer
        within 100 ms.

        The call is a no-op under no conditions; entries are always
        queued. If the widget has been cleared concurrently, an entry
        queued just before the clear may or may not be displayed —
        acceptable for a sub-100-ms race in a log display.

        Args:
            entry: The record to display.
        """
        self._pending.put(entry)

    def toggle_pause(self) -> None:
        """Flip the auto-scroll pause state.

        When paused, new entries continue to append to the log, but the
        view does not auto-scroll to the bottom — the user can scroll
        down manually to see fresh entries while keeping their place in
        history. Unpausing snaps the view to the most recent entry.
        """
        self._paused = not self._paused
        self._update_pause_button()
        if not self._paused:
            self.query_one(f"#{_ID_BODY}", RichLog).scroll_end(animate=False)

    def clear(self) -> None:
        """Clear every displayed entry and the internal ring buffer.

        In-flight entries queued before the call may still appear — the
        pending queue is replaced, so a fresh queue accepts new pushes
        without racing against an about-to-be-drained old queue.
        """
        self._ring.clear()
        self._pending = SimpleQueue()
        self.query_one(f"#{_ID_BODY}", RichLog).clear()

    def action_clear(self) -> None:
        """Bound to ``Ctrl+L``. Delegates to :meth:`clear`."""
        self.clear()

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to the corresponding action."""
        if event.button.id == _ID_PAUSE:
            self.toggle_pause()
        elif event.button.id == _ID_CLEAR:
            self.clear()

    def on_select_changed(self, event: Select.Changed) -> None:
        """Apply the new level floor and rebuild the display."""
        if event.select.id != _ID_LEVEL:
            return
        value = event.value
        if not isinstance(value, str):
            return
        normalised = value.upper()
        if normalised not in _LEVEL_ORDER:
            return
        if _LEVEL_ORDER[normalised] == self._level_floor:
            return
        self._level_name = normalised
        self._level_floor = _LEVEL_ORDER[normalised]
        self._rebuild_from_ring()

    # ------------------------------------------------------------------ #
    # Drain / append / rebuild                                           #
    # ------------------------------------------------------------------ #

    def _drain(self) -> None:
        """Pop every queued entry and append it to the display.

        Runs on the app thread (started by :meth:`on_mount` via
        ``set_interval``). Bounded by the pending queue's current size,
        which is bounded by the push rate over 100 ms.
        """
        rich_log = self.query_one(f"#{_ID_BODY}", RichLog)
        while True:
            try:
                entry = self._pending.get_nowait()
            except Empty:
                break
            self._ring.append(entry)
            if self._passes_filter(entry):
                rich_log.write(
                    self._render_entry(entry),
                    scroll_end=not self._paused,
                )

    def _rebuild_from_ring(self) -> None:
        """Re-emit the ring buffer's filtered entries to the RichLog.

        Called on a level-filter change. Clears the display and writes
        back every retained entry that passes the new filter, so a user
        who changes the filter sees the maximum amount of history the
        widget retained — not just entries logged after the change.
        """
        rich_log = self.query_one(f"#{_ID_BODY}", RichLog)
        rich_log.clear()
        for entry in self._ring:
            if self._passes_filter(entry):
                rich_log.write(
                    self._render_entry(entry),
                    scroll_end=False,
                )
        if not self._paused:
            rich_log.scroll_end(animate=False)

    def _passes_filter(self, entry: LogEntry) -> bool:
        """Return ``True`` iff *entry* meets the current level floor."""
        entry_rank = _LEVEL_ORDER.get(entry.level, _LEVEL_ORDER["INFO"])
        return entry_rank >= self._level_floor

    # ------------------------------------------------------------------ #
    # Rendering                                                          #
    # ------------------------------------------------------------------ #

    def _render_entry(self, entry: LogEntry) -> Text:
        """Return a fully styled ``Text`` for a single log entry.

        The layout is fixed-column:

        * timestamp — 12 chars, dim;
        * level — 8 chars (padded), styled per level;
        * component tag — optional, dim cyan, wrapped in ``[...]``;
        * message — verbatim, unstyled;
        * exception traceback (if any) — dim red, on subsequent lines.

        No markup is interpreted — :class:`~textual.content.Text`
        appends every character literally, so a message containing
        ``[bold]`` renders literally.
        """
        text = Text()

        timestamp = entry.timestamp.strftime("%H:%M:%S.%f")[:-3]
        text.append(timestamp, style=_TIMESTAMP_STYLE)
        text.append(" ", style="")

        text.append(
            entry.level.ljust(_LEVEL_COLUMN_WIDTH),
            style=_LEVEL_STYLE.get(entry.level, ""),
        )
        text.append(" ", style="")

        if entry.component:
            text.append(f"[{entry.component}]", style=_COMPONENT_STYLE)
            text.append(" ", style="")

        text.append(entry.message, style="")

        if entry.exception:
            text.append("\n")
            text.append(entry.exception, style=_EXCEPTION_STYLE)

        return text

    # ------------------------------------------------------------------ #
    # Header helpers                                                     #
    # ------------------------------------------------------------------ #

    def _pause_label(self) -> str:
        """Return the pause button label for the current state."""
        key = (
            "logs.viewer.resume" if self._paused else "logs.viewer.pause"
        )
        return self._translator.translate(key)

    def _update_pause_button(self) -> None:
        """Update the pause button's label and variant to match state."""
        button = self.query_one(f"#{_ID_PAUSE}", Button)
        button.label = self._pause_label()
        button.variant = "warning" if self._paused else "default"
