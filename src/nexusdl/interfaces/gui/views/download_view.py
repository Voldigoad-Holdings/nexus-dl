"""Download view for the NexusDL GUI.

Presentation + orchestration for the active download queue. Does **not**
manage downloads, does **not** know about :class:`DownloadManager`,
:class:`EventBus`, :class:`Settings`. The hosting screen bridges
``download.*`` events into :meth:`set_tasks` / :meth:`add_task` /
:meth:`update_task` / :meth:`remove_task`, and provides the per-task and
global action callbacks.

Universal design notes:
    * Each task is rendered as an internal :class:`_TaskCard` that wraps a
      reusable :class:`ProgressWidget` and adds a task header (title,
      site badge, remove/open-folder buttons).
    * :meth:`update_task` distinguishes *status changes* (which affect the
      active filter, the counters, and the global actions) from *progress
      updates* (which only re-render the affected card). A 100 Hz event
      stream is fine — :class:`ProgressWidget` throttles internally to
      10 Hz.
    * Filter chips are **data-driven** via :class:`DownloadFilterSpec`.
      A new filter (e.g. ``"warning"``) is a single entry.
      :func:`default_filters` provides the standard All/Active/Queued/
      Completed/Failed set, but the host may override entirely.
    * Global actions (Pause all / Resume all / Cancel all / Clear finished)
      are auto-hidden when they would do nothing — no dead buttons.
    * The queue-paused banner is host-pushed state
      (:meth:`set_queue_paused`). The view never invents it.
    * The *Remove* (``×``) affordance is shown only on terminal tasks
      (DONE/FAILED/CANCELLED). Active tasks are *cancelled*, not removed.
    * Counters reflect the **full queue** (not the filtered view), so the
      host can use them as ground truth for badges and window titles.
    * Empty states distinguish *queue empty* from *filter mismatch*.
    * Implements ``on_activate`` / ``on_deactivate`` so :class:`MainView`
      can wire view lifecycle.
    * Cleans up after itself on ``<Destroy>``. Callbacks are isolated.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

import customtkinter as ctk
from loguru import logger as _logger

from nexusdl.core.models.enums import DownloadStatus

from ..components.progress_widget import (
    Labels as ProgressWidgetLabels,
    ProgressState,
    ProgressWidget,
)

if TYPE_CHECKING:
    from PIL import Image

_log = _logger.bind(component="gui.download_view")


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DownloadTaskData:
    """Projection of a download task for display.

    Decoupled from the domain :class:`DownloadTask` so field-name variations
    never ripple into the widget. The hosting view builds these from
    ``download.*`` events or from the manager snapshot.
    """

    task_id: str
    manga_title: str = ""
    chapter_label: str = ""  # e.g. "Ch. 1095" or "Chapter 1095 — Title"
    site_name: str = ""
    status: DownloadStatus = DownloadStatus.PENDING

    # Primary progress (chapters, pages, or files — the host decides).
    completed: int = 0
    total: int | None = None

    # Optional byte-level progress.
    bytes_completed: int = 0
    bytes_total: int | None = None
    speed: float | None = None
    speed_unit: str = "B/s"
    eta_seconds: float | None = None

    # Optional sub-progress (current page inside current chapter, etc.).
    sub_label: str = ""
    sub_completed: int = 0
    sub_total: int | None = None

    # Metadata.
    error: str | None = None
    message: str = ""
    output_format: str = ""
    output_path: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DownloadFilterSpec:
    """One filter chip for the task list.

    ``matches`` receives a status and returns whether the task should be
    visible. The meaning is entirely data-driven — ``"active"``,
    ``"warning"``, ``"site:fr"`` (matching a custom field) — the view does
    not interpret it.
    """

    id: str
    label: str
    matches: Callable[[DownloadStatus], bool]


@dataclass(frozen=True, slots=True)
class DownloadLabels:
    """All user-visible strings used by :class:`DownloadView`."""

    # Counters (shown in the top bar).
    counter_active: str = "active"
    counter_queued: str = "queued"
    counter_done: str = "done"
    counter_failed: str = "failed"
    counter_empty: str = "No downloads"
    counter_separator: str = "  ·  "

    # Global actions.
    pause_all: str = "Pause all"
    resume_all: str = "Resume all"
    cancel_all: str = "Cancel all"
    clear_finished: str = "Clear finished"
    open_output_dir: str = "Open folder"

    # Per-task actions (header buttons).
    remove: str = "×"
    open_folder: str = "📁"

    # Filter labels (used by :func:`default_filters`).
    filter_all: str = "All"
    filter_active: str = "Active"
    filter_queued: str = "Queued"
    filter_completed: str = "Completed"
    filter_failed: str = "Failed"

    # Empty states.
    empty_all: str = "No downloads yet."
    empty_filtered: str = "No downloads match this filter."

    # Queue paused banner.
    queue_paused: str = "The download queue is paused."
    resume_queue: str = "Resume queue"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def default_filters(labels: DownloadLabels | None = None) -> list[DownloadFilterSpec]:
    """Return the standard All / Active / Queued / Completed / Failed chips."""
    lbl = labels or DownloadLabels()
    return [
        DownloadFilterSpec("all", lbl.filter_all, lambda s: True),
        DownloadFilterSpec(
            "active",
            lbl.filter_active,
            lambda s: s in (DownloadStatus.RUNNING, DownloadStatus.PAUSED),
        ),
        DownloadFilterSpec(
            "queued", lbl.filter_queued, lambda s: s == DownloadStatus.PENDING
        ),
        DownloadFilterSpec(
            "completed", lbl.filter_completed, lambda s: s == DownloadStatus.DONE
        ),
        DownloadFilterSpec(
            "failed",
            lbl.filter_failed,
            lambda s: s in (DownloadStatus.FAILED, DownloadStatus.CANCELLED),
        ),
    ]


def _is_terminal(status: DownloadStatus) -> bool:
    return status in (
        DownloadStatus.DONE,
        DownloadStatus.FAILED,
        DownloadStatus.CANCELLED,
    )


def _progress_state_from(task: DownloadTaskData) -> ProgressState:
    """Adapt a :class:`DownloadTaskData` into the generic :class:`ProgressState`."""
    return ProgressState(
        status=task.status,
        completed=task.completed,
        total=task.total,
        bytes_completed=task.bytes_completed,
        bytes_total=task.bytes_total,
        speed=task.speed,
        speed_unit=task.speed_unit,
        eta_seconds=task.eta_seconds,
        message=task.message,
        sub_label=task.sub_label,
        sub_completed=task.sub_completed,
        sub_total=task.sub_total,
        error=task.error,
    )


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_THEME_LIGHT = {
    "bg": "#F5F5F5",
    "panel_bg": "#FFFFFF",
    "fg": "#1A1A1A",
    "muted_fg": "#666666",
    "border": "#CCCCCC",
    "chip_bg": "#EAEAEA",
    "chip_active_bg": "#3B8ED0",
    "chip_active_fg": "#FFFFFF",
    "chip_fg": "#1A1A1A",
    "chip_hover": "#DADADA",
    "banner_bg": "#FFF3B0",
    "banner_fg": "#7A5A00",
    "hint_fg": "#999999",
    "danger_bg": "#C0392B",
    "danger_hover": "#A93226",
    "site_badge_bg": "#34495E",
    "site_badge_fg": "#FFFFFF",
}

_THEME_DARK = {
    "bg": "#1A1A1A",
    "panel_bg": "#242424",
    "fg": "#DCDCDC",
    "muted_fg": "#909090",
    "border": "#333333",
    "chip_bg": "#2E2E2E",
    "chip_active_bg": "#1F6AA5",
    "chip_active_fg": "#FFFFFF",
    "chip_fg": "#DCDCDC",
    "chip_hover": "#3A3A3A",
    "banner_bg": "#4A3A00",
    "banner_fg": "#FFD966",
    "hint_fg": "#666666",
    "danger_bg": "#922B21",
    "danger_hover": "#7B241C",
    "site_badge_bg": "#2C3E50",
    "site_badge_fg": "#FFFFFF",
}

_THEME_POLL_MS = 1000
_CARD_GAP = 8
_TASK_CARD_MIN_HEIGHT = 96


# ---------------------------------------------------------------------------
# Internal: task card
# ---------------------------------------------------------------------------

class _TaskCard(ctk.CTkFrame):
    """Internal composite rendering a single download task.

    Wraps a :class:`ProgressWidget` and adds a task header (title, site
    badge, per-task buttons). Not exported — internal to
    :class:`DownloadView`.
    """

    def __init__(
        self,
        master,
        task: DownloadTaskData,
        *,
        labels: DownloadLabels,
        progress_labels: ProgressWidgetLabels,
        theme_provider: Callable[[], dict],
        on_pause: Callable[[], None] | None,
        on_resume: Callable[[], None] | None,
        on_cancel: Callable[[], None] | None,
        on_retry: Callable[[], None] | None,
        on_remove: Callable[[], None] | None,
        on_open_folder: Callable[[], None] | None,
        on_double_click: Callable[[], None] | None,
        on_right_click: Callable[[object], None] | None,
    ) -> None:
        super().__init__(
            master,
            corner_radius=8,
            fg_color=theme_provider()["panel_bg"],
            border_width=1,
            border_color=theme_provider()["border"],
        )
        self._task = task
        self._labels = labels
        self._theme_provider = theme_provider
        self._on_double_click = on_double_click
        self._on_right_click = on_right_click

        self.grid_columnconfigure(0, weight=1)

        # --- Header ---
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        self._header.grid_columnconfigure(1, weight=1)

        self._title_label = ctk.CTkLabel(
            self._header,
            text="",
            anchor="w",
            justify="left",
            font=("", 13, "bold"),
            text_color=theme_provider()["fg"],
        )
        self._title_label.grid(row=0, column=0, sticky="w")

        self._site_badge = ctk.CTkLabel(
            self._header,
            text="",
            corner_radius=4,
            height=18,
            padx=6,
            font=("", 10),
            fg_color=theme_provider()["site_badge_bg"],
            text_color=theme_provider()["site_badge_fg"],
        )
        self._site_badge.grid(row=0, column=1, sticky="w", padx=(8, 0))

        # Right-side header buttons (open folder / remove).
        self._header_actions = ctk.CTkFrame(
            self._header, fg_color="transparent"
        )
        self._header_actions.grid(row=0, column=2, sticky="e")

        self._open_folder_btn: ctk.CTkButton | None = None
        if on_open_folder is not None:
            self._open_folder_btn = ctk.CTkButton(
                self._header_actions,
                text=labels.open_folder,
                width=32,
                height=24,
                fg_color="transparent",
                border_width=1,
                command=on_open_folder,
            )

        self._remove_btn: ctk.CTkButton | None = None
        if on_remove is not None:
            self._remove_btn = ctk.CTkButton(
                self._header_actions,
                text=labels.remove,
                width=28,
                height=24,
                fg_color="transparent",
                hover_color=theme_provider()["danger_bg"],
                border_width=1,
                command=on_remove,
            )

        # --- Progress widget ---
        self._progress = ProgressWidget(
            self,
            labels=progress_labels,
            on_pause=on_pause,
            on_resume=on_resume,
            on_cancel=on_cancel,
            on_retry=on_retry,
            show_actions=True,
            show_sub=True,
            show_bytes=True,
            show_speed=True,
            show_eta=True,
            show_count=True,
            show_percent=True,
        )
        self._progress.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 6))

        # Bind double-click and right-click on the header only.
        for widget in (self._header, self._title_label, self._site_badge):
            if self._on_double_click is not None:
                widget.bind(
                    "<Double-Button-1>",
                    lambda _e: self._on_double_click(),
                )
            if self._on_right_click is not None:
                widget.bind(
                    "<Button-3>",
                    lambda e: self._on_right_click(e),
                )
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

        # Initial render.
        self.update(task)

    @property
    def task(self) -> DownloadTaskData:
        return self._task

    def update(self, task: DownloadTaskData) -> None:
        """Merge ``task`` into the card (state + header + button visibility)."""
        self._task = task
        self._render_header(task)
        self._progress.set_state(_progress_state_from(task))
        self._refresh_header_buttons(task)

    def _render_header(self, task: DownloadTaskData) -> None:
        title_parts: list[str] = []
        if task.manga_title:
            title_parts.append(task.manga_title)
        if task.chapter_label:
            title_parts.append(task.chapter_label)
        title = " — ".join(title_parts) if title_parts else task.task_id

        # Truncate defensively on very long titles.
        max_chars = 90
        if len(title) > max_chars:
            title = title[: max_chars - 1].rstrip() + "…"
        try:
            self._title_label.configure(text=title)
        except Exception:
            pass

        if task.site_name:
            try:
                self._site_badge.configure(text=task.site_name)
                self._site_badge.grid()
            except Exception:
                pass
        else:
            try:
                self._site_badge.grid_remove()
            except Exception:
                pass

    def _refresh_header_buttons(self, task: DownloadTaskData) -> None:
        terminal = _is_terminal(task.status)
        show_remove = terminal and self._remove_btn is not None
        show_open = (
            task.status == DownloadStatus.DONE
            and task.output_path
            and self._open_folder_btn is not None
        )

        if self._remove_btn is not None:
            try:
                if show_remove:
                    self._remove_btn.pack(side="right")
                else:
                    self._remove_btn.pack_forget()
            except Exception:
                pass

        if self._open_folder_btn is not None:
            try:
                if show_open:
                    self._open_folder_btn.pack(side="right", padx=(0, 6))
                else:
                    self._open_folder_btn.pack_forget()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

class DownloadView(ctk.CTkFrame):
    """The download queue screen.

    Parameters:
        master: Parent widget.
        labels: Localized labels. ``None`` uses English defaults.
        progress_labels: Labels forwarded to :class:`ProgressWidget`.
        filter_specs: Data-driven filter chips. ``None`` uses
            :func:`default_filters`.
        on_pause: Per-task callback ``(task_id) -> None``.
        on_resume: Per-task callback ``(task_id) -> None``.
        on_cancel: Per-task callback ``(task_id) -> None``.
        on_retry: Per-task callback ``(task_id) -> None``.
        on_remove: Per-task callback ``(task_id) -> None``. If ``None``,
            the *Remove* (``×``) button is hidden on terminal tasks.
        on_open_folder: Per-task callback ``(task_id) -> None``. If ``None``,
            the *Open folder* button is hidden on DONE tasks.
        on_open_output_dir: Global callback, no argument — opens the base
            output directory.
        on_pause_all / on_resume_all / on_cancel_all / on_clear_finished:
            Global callbacks. Pass ``None`` to hide the corresponding button.
        on_resume_queue: Callback invoked by the *Resume queue* banner
            button. If ``None``, the queue-paused banner does not render a
            button (the host should provide one).
        on_task_double_click: Called with ``(task_id)`` on header
            double-click.
        on_task_context_menu: Called with ``(task_id, raw_tk_event)`` on
            header right-click.
        on_filter_change: Called with the new filter id when the user clicks
            a chip.
        on_tasks_change: Called with the full ``list[DownloadTaskData]``
            on every mutation (add/remove/update/set). Useful for a global
            badge or window title.
    """

    def __init__(
        self,
        master,
        *,
        labels: DownloadLabels | None = None,
        progress_labels: ProgressWidgetLabels | None = None,
        filter_specs: Sequence[DownloadFilterSpec] | None = None,
        on_pause: Callable[[str], None] | None = None,
        on_resume: Callable[[str], None] | None = None,
        on_cancel: Callable[[str], None] | None = None,
        on_retry: Callable[[str], None] | None = None,
        on_remove: Callable[[str], None] | None = None,
        on_open_folder: Callable[[str], None] | None = None,
        on_open_output_dir: Callable[[], None] | None = None,
        on_pause_all: Callable[[], None] | None = None,
        on_resume_all: Callable[[], None] | None = None,
        on_cancel_all: Callable[[], None] | None = None,
        on_clear_finished: Callable[[], None] | None = None,
        on_resume_queue: Callable[[], None] | None = None,
        on_task_double_click: Callable[[str], None] | None = None,
        on_task_context_menu: Callable[[str, object], None] | None = None,
        on_filter_change: Callable[[str], None] | None = None,
        on_tasks_change: Callable[[list[DownloadTaskData]], None] | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        self._labels = labels or DownloadLabels()
        self._progress_labels = progress_labels or ProgressWidgetLabels()
        self._filters = list(filter_specs) if filter_specs is not None else default_filters(self._labels)

        self._on_pause_cb = on_pause
        self._on_resume_cb = on_resume
        self._on_cancel_cb = on_cancel
        self._on_retry_cb = on_retry
        self._on_remove_cb = on_remove
        self._on_open_folder_cb = on_open_folder
        self._on_open_output_dir_cb = on_open_output_dir
        self._on_pause_all_cb = on_pause_all
        self._on_resume_all_cb = on_resume_all
        self._on_cancel_all_cb = on_cancel_all
        self._on_clear_finished_cb = on_clear_finished
        self._on_resume_queue_cb = on_resume_queue
        self._on_task_double_click_cb = on_task_double_click
        self._on_task_context_menu_cb = on_task_context_menu
        self._on_filter_change_cb = on_filter_change
        self._on_tasks_change_cb = on_tasks_change

        # Data.
        self._tasks: dict[str, DownloadTaskData] = {}
        self._cards: dict[str, _TaskCard] = {}
        self._active_filter_id: str = self._filters[0].id if self._filters else "all"
        self._queue_paused: bool = False
        self._destroyed = False
        self._theme_after_id: str | None = None
        self._last_theme_mode: str = self._current_mode()

        self._build()
        self._apply_theme()
        self._render_all()

        self.bind("<Destroy>", self._on_destroy)
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Public API — host pushes tasks
    # ------------------------------------------------------------------

    def set_tasks(self, tasks: Sequence[DownloadTaskData]) -> None:
        """Replace the entire queue."""
        if self._destroyed:
            return
        self._tasks = {t.task_id: t for t in tasks}
        self._render_all()
        self._emit_tasks_change()

    def add_task(self, task: DownloadTaskData) -> None:
        """Add one task to the queue."""
        if self._destroyed:
            return
        self._tasks[task.task_id] = task
        if self._matches_active_filter(task):
            self._render_all()
        else:
            self._refresh_counters()
            self._refresh_global_actions()
        self._emit_tasks_change()

    def update_task(self, task_id: str, **changes) -> None:
        """Update fields of a task.

        Status changes re-render the list (they affect filter visibility and
        counters). Pure progress updates only re-render the affected card.
        """
        if self._destroyed:
            return
        task = self._tasks.get(task_id)
        if task is None:
            return

        old_status = task.status
        try:
            new_task = replace(task, **changes)
        except TypeError:
            _log.warning("update_task: unknown field in {!r}", changes.keys())
            return
        self._tasks[task_id] = new_task

        if old_status == new_task.status:
            card = self._cards.get(task_id)
            if card is not None:
                card.update(new_task)
            self._refresh_counters()
            self._refresh_global_actions()
        else:
            self._render_all()
        self._emit_tasks_change()

    def remove_task(self, task_id: str) -> None:
        """Remove one task from the queue."""
        if self._destroyed:
            return
        if task_id not in self._tasks:
            return
        del self._tasks[task_id]
        self._render_all()
        self._emit_tasks_change()

    def clear_all(self) -> None:
        """Remove every task from the queue (view-side only)."""
        if self._destroyed:
            return
        self._tasks.clear()
        self._render_all()
        self._emit_tasks_change()

    def set_queue_paused(self, paused: bool) -> None:
        """Show / hide the queue-paused banner."""
        if self._destroyed:
            return
        self._queue_paused = bool(paused)
        self._refresh_banner()

    def set_filter(self, filter_id: str) -> None:
        """Programmatically switch the active filter chip."""
        if self._destroyed:
            return
        if not any(f.id == filter_id for f in self._filters):
            raise KeyError(f"Unknown filter id: {filter_id!r}")
        if filter_id == self._active_filter_id:
            return
        self._active_filter_id = filter_id
        self._rebuild_filter_chips()
        self._render_all()

    def get_active_filter(self) -> str:
        return self._active_filter_id

    def get_tasks(self) -> list[DownloadTaskData]:
        return list(self._tasks.values())

    # ------------------------------------------------------------------
    # Lifecycle (called by MainView)
    # ------------------------------------------------------------------

    def on_activate(self) -> None:
        # No auto-refresh here — the host pushes via events continuously.
        return

    def on_deactivate(self) -> None:
        return

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # --- Top bar: counters + global actions ---
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        top.grid_columnconfigure(0, weight=1)

        self._counters_label = ctk.CTkLabel(
            top,
            text="",
            anchor="w",
            text_color=self._theme()["muted_fg"],
            font=("", 12),
        )
        self._counters_label.grid(row=0, column=0, sticky="w")

        self._global_actions = ctk.CTkFrame(top, fg_color="transparent")
        self._global_actions.grid(row=0, column=1, sticky="e")

        self._pause_all_btn: ctk.CTkButton | None = None
        if self._on_pause_all_cb is not None:
            self._pause_all_btn = ctk.CTkButton(
                self._global_actions,
                text=self._labels.pause_all,
                width=100,
                height=26,
                command=self._invoke(self._on_pause_all_cb),
            )

        self._resume_all_btn: ctk.CTkButton | None = None
        if self._on_resume_all_cb is not None:
            self._resume_all_btn = ctk.CTkButton(
                self._global_actions,
                text=self._labels.resume_all,
                width=100,
                height=26,
                command=self._invoke(self._on_resume_all_cb),
            )

        self._cancel_all_btn: ctk.CTkButton | None = None
        if self._on_cancel_all_cb is not None:
            self._cancel_all_btn = ctk.CTkButton(
                self._global_actions,
                text=self._labels.cancel_all,
                width=100,
                height=26,
                fg_color=self._theme()["danger_bg"],
                hover_color=self._theme()["danger_hover"],
                command=self._invoke(self._on_cancel_all_cb),
            )

        self._clear_finished_btn: ctk.CTkButton | None = None
        if self._on_clear_finished_cb is not None:
            self._clear_finished_btn = ctk.CTkButton(
                self._global_actions,
                text=self._labels.clear_finished,
                width=120,
                height=26,
                fg_color="transparent",
                border_width=1,
                command=self._invoke(self._on_clear_finished_cb),
            )

        self._open_output_btn: ctk.CTkButton | None = None
        if self._on_open_output_dir_cb is not None:
            self._open_output_btn = ctk.CTkButton(
                self._global_actions,
                text=self._labels.open_output_dir,
                width=110,
                height=26,
                fg_color="transparent",
                border_width=1,
                command=self._invoke(self._on_open_output_dir_cb),
            )

        # --- Filter chips ---
        self._filters_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._filters_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        self._rebuild_filter_chips()

        # --- Queue paused banner ---
        self._banner = ctk.CTkFrame(self, corner_radius=6)
        self._banner.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        self._banner.grid_columnconfigure(0, weight=1)

        self._banner_label = ctk.CTkLabel(
            self._banner, text=self._labels.queue_paused, anchor="w"
        )
        self._banner_label.grid(row=0, column=0, sticky="w", padx=10, pady=6)

        self._banner_btn: ctk.CTkButton | None = None
        if self._on_resume_queue_cb is not None:
            self._banner_btn = ctk.CTkButton(
                self._banner,
                text=self._labels.resume_queue,
                width=130,
                height=26,
                command=self._invoke(self._on_resume_queue_cb),
            )
            self._banner_btn.grid(row=0, column=1, padx=10, pady=6)

        self._banner.grid_remove()

        # --- Scrollable task list ---
        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=3, column=0, sticky="nsew", padx=6, pady=2)
        self._scroll.grid_columnconfigure(0, weight=1)

        # --- Empty state ---
        self._empty_frame = ctk.CTkFrame(self._scroll, fg_color="transparent")
        self._empty_label = ctk.CTkLabel(
            self._empty_frame,
            text="",
            anchor="center",
            justify="center",
            text_color=self._theme()["hint_fg"],
            font=("", 12),
        )
        self._empty_label.pack(pady=40)

    def _rebuild_filter_chips(self) -> None:
        for child in self._filters_frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        theme = self._theme()
        for spec in self._filters:
            active = spec.id == self._active_filter_id
            btn = ctk.CTkButton(
                self._filters_frame,
                text=spec.label,
                height=26,
                corner_radius=13,
                fg_color=theme["chip_active_bg"] if active else theme["chip_bg"],
                hover_color=theme["chip_active_bg"] if active else theme["chip_hover"],
                text_color=theme["chip_active_fg"] if active else theme["chip_fg"],
                command=lambda sid=spec.id: self._on_chip_click(sid),
            )
            btn.pack(side="left", padx=(0, 6))

    # ------------------------------------------------------------------
    # Filtering / rendering
    # ------------------------------------------------------------------

    def _matches_active_filter(self, task: DownloadTaskData) -> bool:
        spec = next(
            (f for f in self._filters if f.id == self._active_filter_id), None
        )
        if spec is None:
            return True
        try:
            return bool(spec.matches(task.status))
        except Exception:
            _log.exception("Filter {!r} raised in matches()", spec.id)
            return True

    def _visible_tasks(self) -> list[DownloadTaskData]:
        if not self._filters:
            return list(self._tasks.values())
        return [t for t in self._tasks.values() if self._matches_active_filter(t)]

    def _render_all(self) -> None:
        if self._destroyed:
            return

        # Tear down existing cards.
        for card in self._cards.values():
            try:
                card.destroy()
            except Exception:
                pass
        self._cards.clear()

        visible = self._visible_tasks()

        if not visible:
            self._render_empty_state()
        else:
            try:
                self._empty_frame.pack_forget()
            except Exception:
                pass

            for task in visible:
                card = self._make_card(task)
                self._cards[task.task_id] = card
                card.grid(
                    row=len(self._cards) - 1,
                    column=0,
                    sticky="ew",
                    padx=2,
                    pady=_CARD_GAP // 2,
                )

        self._refresh_counters()
        self._refresh_global_actions()
        self._refresh_banner()

    def _make_card(self, task: DownloadTaskData) -> _TaskCard:
        return _TaskCard(
            self._scroll,
            task,
            labels=self._labels,
            progress_labels=self._progress_labels,
            theme_provider=self._theme,
            on_pause=self._task_cb(self._on_pause_cb, task.task_id),
            on_resume=self._task_cb(self._on_resume_cb, task.task_id),
            on_cancel=self._task_cb(self._on_cancel_cb, task.task_id),
            on_retry=self._task_cb(self._on_retry_cb, task.task_id),
            on_remove=self._task_cb(self._on_remove_cb, task.task_id),
            on_open_folder=self._task_cb(self._on_open_folder_cb, task.task_id),
            on_double_click=(
                self._task_cb(self._on_task_double_click_cb, task.task_id)
                if self._on_task_double_click_cb is not None
                else None
            ),
            on_right_click=(
                self._task_event_cb(self._on_task_context_menu_cb, task.task_id)
                if self._on_task_context_menu_cb is not None
                else None
            ),
        )

    def _render_empty_state(self) -> None:
        has_any = bool(self._tasks)
        if has_any:
            text = self._labels.empty_filtered
        else:
            text = self._labels.empty_all
        try:
            self._empty_label.configure(text=text)
        except Exception:
            pass
        try:
            self._empty_frame.pack(fill="both", expand=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Counters + global actions
    # ------------------------------------------------------------------

    def _refresh_counters(self) -> None:
        counts = {
            "active": 0,
            "queued": 0,
            "done": 0,
            "failed": 0,
        }
        for task in self._tasks.values():
            if task.status in (DownloadStatus.RUNNING, DownloadStatus.PAUSED):
                counts["active"] += 1
            elif task.status == DownloadStatus.PENDING:
                counts["queued"] += 1
            elif task.status == DownloadStatus.DONE:
                counts["done"] += 1
            elif task.status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED):
                counts["failed"] += 1

        parts: list[str] = []
        if counts["active"]:
            parts.append(f"{counts['active']} {self._labels.counter_active}")
        if counts["queued"]:
            parts.append(f"{counts['queued']} {self._labels.counter_queued}")
        if counts["done"]:
            parts.append(f"{counts['done']} {self._labels.counter_done}")
        if counts["failed"]:
            parts.append(f"{counts['failed']} {self._labels.counter_failed}")

        text = (
            self._labels.counter_separator.join(parts)
            if parts
            else self._labels.counter_empty
        )
        try:
            self._counters_label.configure(text=text)
        except Exception:
            pass

    def _refresh_global_actions(self) -> None:
        has_active_or_pending = any(
            t.status in (DownloadStatus.RUNNING, DownloadStatus.PENDING)
            for t in self._tasks.values()
        )
        has_paused = any(
            t.status == DownloadStatus.PAUSED for t in self._tasks.values()
        )
        has_non_terminal = any(
            not _is_terminal(t.status) for t in self._tasks.values()
        )
        has_terminal = any(
            _is_terminal(t.status) for t in self._tasks.values()
        )

        def _toggle(btn: ctk.CTkButton | None, visible: bool) -> None:
            if btn is None:
                return
            try:
                if visible:
                    btn.pack(side="left", padx=(6, 0))
                else:
                    btn.pack_forget()
            except Exception:
                pass

        _toggle(self._pause_all_btn, has_active_or_pending)
        _toggle(self._resume_all_btn, has_paused)
        _toggle(self._cancel_all_btn, has_non_terminal)
        _toggle(self._clear_finished_btn, has_terminal)
        _toggle(self._open_output_btn, True)

    def _refresh_banner(self) -> None:
        if not self._queue_paused:
            try:
                self._banner.grid_remove()
            except Exception:
                pass
            return
        theme = self._theme()
        try:
            self._banner.configure(fg_color=theme["banner_bg"])
            self._banner_label.configure(text_color=theme["banner_fg"])
        except Exception:
            pass
        try:
            self._banner.grid()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_chip_click(self, filter_id: str) -> None:
        if filter_id == self._active_filter_id:
            return
        self._active_filter_id = filter_id
        self._rebuild_filter_chips()
        self._render_all()
        if self._on_filter_change_cb is not None:
            self._safe_call(self._on_filter_change_cb, filter_id)

    # ------------------------------------------------------------------
    # Callback helpers
    # ------------------------------------------------------------------

    def _task_cb(
        self, cb: Callable[[str], None] | None, task_id: str
    ) -> Callable[[], None] | None:
        if cb is None:
            return None

        def _wrapped() -> None:
            self._safe_call(cb, task_id)

        return _wrapped

    def _task_event_cb(
        self, cb: Callable[[str, object], None] | None, task_id: str
    ) -> Callable[[object], None] | None:
        if cb is None:
            return None

        def _wrapped(event) -> None:
            self._safe_call(cb, task_id, event)

        return _wrapped

    @staticmethod
    def _invoke(cb: Callable[[], None]) -> Callable[[], None]:
        def _wrapped() -> None:
            try:
                cb()
            except Exception:
                _log.exception("DownloadView global callback raised")
        return _wrapped

    @staticmethod
    def _safe_call(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:
            _log.exception(
                "DownloadView: callback {!r} raised",
                getattr(cb, "__name__", cb),
            )

    def _emit_tasks_change(self) -> None:
        if self._on_tasks_change_cb is not None:
            self._safe_call(self._on_tasks_change_cb, list(self._tasks.values()))

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
            self._scroll.configure(fg_color="transparent")
        except Exception:
            pass
        self._rebuild_filter_chips()
        self._refresh_banner()
        self._refresh_counters()

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
    # Teardown
    # ------------------------------------------------------------------

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._destroyed = True
        if self._theme_after_id is not None:
            try:
                self.after_cancel(self._theme_after_id)
            except Exception:
                pass
            self._theme_after_id = None
