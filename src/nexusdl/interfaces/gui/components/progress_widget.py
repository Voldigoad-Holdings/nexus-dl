"""Progress widget for the NexusDL GUI.

Presentation-only: takes :class:`ProgressState` snapshots and renders them.
Does not know about :class:`EventBus`, :class:`DownloadManager`, or the
library. The hosting view bridges download events into ``ProgressState``
instances and pushes them via :meth:`ProgressWidget.set_state`.

Universal design notes:
    * Every display section is independently toggleable (actions, bytes,
      speed, ETA, count, percent, sub-progress). A future usage can hide
      what makes no sense without touching the rendering code.
    * Internal redraws are rate-limited to ``refresh_interval_ms`` (default
      10 Hz) so a high-frequency event stream cannot saturate Tk. Multiple
      ``set_state`` calls within the window coalesce into a single redraw.
    * Determinate vs. indeterminate mode is chosen automatically: unknown
      total + RUNNING → indeterminate animation; otherwise determinate.
    * Button set adapts to the current status. Only buttons whose callback
      is provided are shown. Adding a new action is a new ``on_*`` kwarg —
      no branching in the rendering code.
    * No hardcoded site names, no hardcoded URLs. Everything visible comes
      from the injected :class:`ProgressState` or the :class:`Labels` map.
    * Safe against out-of-order updates: the latest ``set_state`` wins.
    * Cleans up after itself on ``<Destroy>`` (stops the indeterminate
      animation, cancels pending ``after`` callbacks).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import customtkinter as ctk
from loguru import logger as _logger

from nexusdl.core.models.enums import DownloadStatus

_log = _logger.bind(component="gui.progress_widget")


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ProgressState:
    """Snapshot of a long-running task, as rendered by :class:`ProgressWidget`.

    All numeric fields are non-negative. ``total``/``bytes_total``/``sub_total``
    may be ``None`` to signal an unknown or unbounded quantity.

    Attributes:
        status: Current lifecycle state. Drives bar color, primary action
            button, and indeterminate mode.
        completed: Primary completed count (items, pages, chapters, files…).
        total: Primary total count, or ``None`` if unknown.
        bytes_completed: Optional byte-level completed count (extra text).
        bytes_total: Optional byte-level total, or ``None``.
        speed: Optional speed value (bytes/s, items/s — see ``speed_unit``).
        speed_unit: Unit suffix for ``speed`` (e.g. ``"B/s"``, ``"items/s"``).
        eta_seconds: Optional estimated seconds remaining.
        message: Optional custom status line overriding the status label.
        sub_label: Optional label for the current sub-item ("Chapter 12 / 45").
        sub_completed: Sub-item completed count.
        sub_total: Sub-item total, or ``None`` for an unbounded sub-phase.
        error: Optional error text shown when ``status == FAILED``.
    """

    status: DownloadStatus = DownloadStatus.PENDING
    completed: int = 0
    total: int | None = None
    bytes_completed: int = 0
    bytes_total: int | None = None
    speed: float | None = None
    speed_unit: str = "B/s"
    eta_seconds: float | None = None
    message: str = ""
    sub_label: str = ""
    sub_completed: int = 0
    sub_total: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Labels:
    """All user-visible strings used by :class:`ProgressWidget`.

    Frozen dataclass — inject a translated instance from the hosting view to
    localize. Defaults are English fallbacks so the widget works standalone.
    """

    pending: str = "Pending"
    running: str = "Running"
    paused: str = "Paused"
    done: str = "Done"
    failed: str = "Failed"
    cancelled: str = "Cancelled"
    pause: str = "Pause"
    resume: str = "Resume"
    cancel: str = "Cancel"
    retry: str = "Retry"
    eta: str = "ETA"
    unknown: str = "—"


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[DownloadStatus, str] = {
    DownloadStatus.PENDING: "○",
    DownloadStatus.RUNNING: "▶",
    DownloadStatus.PAUSED: "‖",
    DownloadStatus.DONE: "✓",
    DownloadStatus.FAILED: "✗",
    DownloadStatus.CANCELLED: "—",
}

_STATUS_COLOR: dict[DownloadStatus, str] = {
    DownloadStatus.PENDING: "#95A5A6",
    DownloadStatus.RUNNING: "#3B8ED0",
    DownloadStatus.PAUSED: "#F1C40F",
    DownloadStatus.DONE: "#2ECC71",
    DownloadStatus.FAILED: "#E74C3C",
    DownloadStatus.CANCELLED: "#95A5A6",
}

_LABEL_FOR_STATUS = {
    DownloadStatus.PENDING: "pending",
    DownloadStatus.RUNNING: "running",
    DownloadStatus.PAUSED: "paused",
    DownloadStatus.DONE: "done",
    DownloadStatus.FAILED: "failed",
    DownloadStatus.CANCELLED: "cancelled",
}

_MUTED_TEXT = ("gray40", "gray60")
_PRIMARY_TEXT = ("gray10", "gray90")
_BAR_BG = ("gray80", "gray25")
_ERROR_MAX_CHARS = 80


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_bytes(n: int | float) -> str:
    """Format a byte count as a human-readable string (binary units)."""
    size = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as ``MM:SS`` or ``H:MM:SS``."""
    if seconds < 0:
        return "—"
    total = int(seconds)
    if total < 3600:
        m, s = divmod(total, 60)
        return f"{m:02d}:{s:02d}"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _format_speed(value: float, unit: str) -> str:
    """Format a speed value for the given unit."""
    if unit in ("B/s", "bytes/s"):
        return f"{_format_bytes(int(value))}/s"
    return f"{value:.1f} {unit}"


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _ratio(completed: int, total: int | None) -> float:
    if total is None or total <= 0:
        return 0.0
    return _clamp01(completed / total)


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class ProgressWidget(ctk.CTkFrame):
    """A compact, throttled progress panel for any long-running task.

    Parameters:
        master: Parent widget.
        labels: Localized labels. ``None`` uses English defaults.
        on_pause: Invoked when the user clicks *Pause*. If ``None``, the
            button is hidden.
        on_resume: Invoked when the user clicks *Resume*. If ``None``, the
            button is hidden.
        on_cancel: Invoked when the user clicks *Cancel*. If ``None``, the
            button is hidden.
        on_retry: Invoked when the user clicks *Retry* (only shown when
            ``status`` is ``FAILED`` or ``CANCELLED``).
        show_actions: Master switch for the action button row.
        show_sub: Master switch for the sub-progress block.
        show_bytes: Show byte counters when ``bytes_total`` is available.
        show_speed: Show speed when ``speed`` is available.
        show_eta: Show ETA when ``eta_seconds`` is available.
        show_count: Show ``completed / total`` in the metrics row.
        show_percent: Show the percentage on the right of the bar.
        refresh_interval_ms: Minimum interval between two redraws (ms).
            Defaults to ``100`` (10 Hz). Set to ``0`` to disable throttling.
    """

    def __init__(
        self,
        master,
        *,
        labels: Labels | None = None,
        on_pause: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        on_retry: Callable[[], None] | None = None,
        show_actions: bool = True,
        show_sub: bool = True,
        show_bytes: bool = True,
        show_speed: bool = True,
        show_eta: bool = True,
        show_count: bool = True,
        show_percent: bool = True,
        refresh_interval_ms: int = 100,
        **kwargs,
    ) -> None:
        super().__init__(master, corner_radius=8, **kwargs)

        self._labels = labels or Labels()
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_cancel = on_cancel
        self._on_retry = on_retry

        self._show_actions = show_actions
        self._show_sub = show_sub
        self._show_bytes = show_bytes
        self._show_speed = show_speed
        self._show_eta = show_eta
        self._show_count = show_count
        self._show_percent = show_percent
        self._refresh_interval_ms = max(0, refresh_interval_ms)

        # State.
        self._state = ProgressState()
        self._pending_state: ProgressState | None = None
        self._refresh_after_id: str | None = None
        self._destroyed = False

        # Render bookkeeping.
        self._bar_mode: str = "determinate"
        self._rendered_status: DownloadStatus | None = None
        self._animating = False

        self._build()
        self._render(force=True)

        self.bind("<Destroy>", self._on_destroy)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> ProgressState:
        """Return the currently displayed :class:`ProgressState`."""
        return self._state

    def set_state(self, state: ProgressState) -> None:
        """Schedule ``state`` for display, coalescing redraws.

        Safe to call as often as the caller wants — the widget throttles
        redraws to ``refresh_interval_ms``.
        """
        if self._destroyed:
            return
        self._pending_state = state
        if self._refresh_after_id is None:
            if self._refresh_interval_ms == 0:
                self._apply_pending()
            else:
                self._refresh_after_id = self.after(
                    self._refresh_interval_ms, self._apply_pending
                )

    def reset(self) -> None:
        """Reset the widget to an idle :class:`ProgressState`."""
        self.set_state(ProgressState())

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        # --- Header row: icon + message + actions ---
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        self._header.grid_columnconfigure(1, weight=1)

        self._icon_label = ctk.CTkLabel(
            self._header, text="○", width=20, text_color=_STATUS_COLOR[DownloadStatus.PENDING]
        )
        self._icon_label.grid(row=0, column=0, padx=(0, 6))

        self._message_label = ctk.CTkLabel(
            self._header, text="", anchor="w", text_color=_PRIMARY_TEXT
        )
        self._message_label.grid(row=0, column=1, sticky="ew")

        self._actions = ctk.CTkFrame(self._header, fg_color="transparent")
        self._actions.grid(row=0, column=2, sticky="e")
        self._build_actions(self._actions)

        # --- Main bar ---
        self._main_bar = ctk.CTkProgressBar(
            self,
            mode="determinate",
            height=10,
            corner_radius=5,
            fg_color=_BAR_BG,
            progress_color=_STATUS_COLOR[DownloadStatus.PENDING],
        )
        self._main_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        self._main_bar.set(0.0)

        # --- Metrics row ---
        self._metrics = ctk.CTkFrame(self, fg_color="transparent")
        self._metrics.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 4))
        self._metrics.grid_columnconfigure(0, weight=1)

        self._metrics_left = ctk.CTkFrame(self._metrics, fg_color="transparent")
        self._metrics_left.grid(row=0, column=0, sticky="w")

        self._percent_label = ctk.CTkLabel(
            self._metrics_left, text="", text_color=_MUTED_TEXT
        )
        self._count_label = ctk.CTkLabel(
            self._metrics_left, text="", text_color=_MUTED_TEXT
        )
        self._bytes_label = ctk.CTkLabel(
            self._metrics_left, text="", text_color=_MUTED_TEXT
        )

        self._metrics_right = ctk.CTkFrame(self._metrics, fg_color="transparent")
        self._metrics_right.grid(row=0, column=1, sticky="e")

        self._speed_label = ctk.CTkLabel(
            self._metrics_right, text="", text_color=_MUTED_TEXT
        )
        self._eta_label = ctk.CTkLabel(
            self._metrics_right, text="", text_color=_MUTED_TEXT
        )

        # --- Sub-progress block ---
        self._sub_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._sub_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        self._sub_frame.grid_columnconfigure(0, weight=1)

        self._sub_label = ctk.CTkLabel(
            self._sub_frame, text="", anchor="w", text_color=_MUTED_TEXT
        )
        self._sub_label.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        self._sub_bar = ctk.CTkProgressBar(
            self._sub_frame,
            mode="determinate",
            height=6,
            corner_radius=3,
            fg_color=_BAR_BG,
            progress_color=_STATUS_COLOR[DownloadStatus.RUNNING],
        )
        self._sub_bar.grid(row=1, column=0, sticky="ew")
        self._sub_bar.set(0.0)

    def _build_actions(self, parent) -> None:
        self._action_buttons: dict[str, ctk.CTkButton] = {}

        if self._on_pause is not None:
            self._action_buttons["pause"] = ctk.CTkButton(
                parent,
                text=self._labels.pause,
                width=76,
                height=28,
                command=self._invoke(self._on_pause),
            )
        if self._on_resume is not None:
            self._action_buttons["resume"] = ctk.CTkButton(
                parent,
                text=self._labels.resume,
                width=76,
                height=28,
                command=self._invoke(self._on_resume),
            )
        if self._on_retry is not None:
            self._action_buttons["retry"] = ctk.CTkButton(
                parent,
                text=self._labels.retry,
                width=76,
                height=28,
                command=self._invoke(self._on_retry),
            )
        if self._on_cancel is not None:
            self._action_buttons["cancel"] = ctk.CTkButton(
                parent,
                text=self._labels.cancel,
                width=76,
                height=28,
                fg_color=("#C0392B", "#922B21"),
                hover_color=("#A93226", "#7B241C"),
                command=self._invoke(self._on_cancel),
            )

    @staticmethod
    def _invoke(cb: Callable[[], None]) -> Callable[[], None]:
        """Wrap a callback so exceptions never break the Tk main loop."""
        def _wrapped() -> None:
            try:
                cb()
            except Exception:
                _log.exception("ProgressWidget action callback raised")
        return _wrapped

    # ------------------------------------------------------------------
    # Throttled rendering
    # ------------------------------------------------------------------

    def _apply_pending(self) -> None:
        self._refresh_after_id = None
        if self._destroyed:
            return
        if self._pending_state is None:
            return
        self._state = self._pending_state
        self._pending_state = None
        self._render(force=False)

    def _render(self, *, force: bool) -> None:
        status = self._state.status

        if force or status != self._rendered_status:
            self._rendered_status = status
            self._render_status(status)
            self._render_actions(status)
            self._render_sub_visibility()

        self._render_bar()
        self._render_metrics()
        self._render_sub()

    def _render_status(self, status: DownloadStatus) -> None:
        color = _STATUS_COLOR.get(status, _STATUS_COLOR[DownloadStatus.PENDING])
        icon = _STATUS_ICON.get(status, "○")
        self._icon_label.configure(text=icon, text_color=color)
        try:
            self._main_bar.configure(progress_color=color)
        except Exception:
            pass

        msg = self._resolve_message(status)
        self._message_label.configure(text=msg)

    def _resolve_message(self, status: DownloadStatus) -> str:
        if self._state.message:
            return self._state.message
        if status == DownloadStatus.FAILED and self._state.error:
            err = self._state.error
            if len(err) > _ERROR_MAX_CHARS:
                err = err[: _ERROR_MAX_CHARS - 1] + "…"
            return err
        key = _LABEL_FOR_STATUS.get(status, "pending")
        return getattr(self._labels, key, "")

    def _render_actions(self, status: DownloadStatus) -> None:
        if not self._show_actions or not self._action_buttons:
            self._actions.grid_remove()
            return
        self._actions.grid()

        visible: list[str] = []
        if status in (DownloadStatus.PENDING, DownloadStatus.RUNNING):
            visible.append("pause")
        elif status == DownloadStatus.PAUSED:
            visible.append("resume")
        elif status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            visible.append("retry")

        if status in (
            DownloadStatus.PENDING,
            DownloadStatus.RUNNING,
            DownloadStatus.PAUSED,
        ):
            visible.append("cancel")

        for name, button in self._action_buttons.items():
            if name in visible:
                button.pack(side="left", padx=(4, 0))
            else:
                button.pack_forget()

    def _render_bar(self) -> None:
        indeterminate = self._should_animate()
        desired = "indeterminate" if indeterminate else "determinate"

        if self._bar_mode != desired:
            if self._animating:
                try:
                    self._main_bar.stop()
                except Exception:
                    pass
                self._animating = False
            try:
                self._main_bar.configure(mode=desired)
            except Exception:
                pass
            self._bar_mode = desired

        if indeterminate:
            if not self._animating:
                try:
                    self._main_bar.start()
                    self._animating = True
                except Exception:
                    pass
        else:
            if self._animating:
                try:
                    self._main_bar.stop()
                except Exception:
                    pass
                self._animating = False
            self._main_bar.set(_ratio(self._state.completed, self._state.total))

    def _should_animate(self) -> bool:
        if self._state.status != DownloadStatus.RUNNING:
            return False
        return self._state.total is None or self._state.total <= 0

    def _render_metrics(self) -> None:
        state = self._state
        total_known = state.total is not None and state.total > 0

        # Percent
        if self._show_percent and total_known:
            pct = int(round(_ratio(state.completed, state.total) * 100))
            self._percent_label.configure(text=f"{pct}%")
            self._percent_label.pack(side="left", padx=(0, 8))
        else:
            self._percent_label.pack_forget()

        # Count
        if self._show_count and total_known:
            self._count_label.configure(
                text=f"{state.completed:,} / {state.total:,}"
            )
            self._count_label.pack(side="left", padx=(0, 8))
        else:
            self._count_label.pack_forget()

        # Bytes
        if (
            self._show_bytes
            and state.bytes_total is not None
            and state.bytes_total > 0
        ):
            self._bytes_label.configure(
                text=f"{_format_bytes(state.bytes_completed)} / "
                f"{_format_bytes(state.bytes_total)}"
            )
            self._bytes_label.pack(side="left", padx=(0, 8))
        else:
            self._bytes_label.pack_forget()

        # Speed
        if self._show_speed and state.speed is not None and state.speed >= 0:
            self._speed_label.configure(
                text=_format_speed(state.speed, state.speed_unit)
            )
            self._speed_label.pack(side="right", padx=(8, 0))
        else:
            self._speed_label.pack_forget()

        # ETA
        if self._show_eta and state.eta_seconds is not None:
            self._eta_label.configure(
                text=f"{self._labels.eta} {_format_duration(state.eta_seconds)}"
            )
            self._eta_label.pack(side="right", padx=(8, 0))
        else:
            self._eta_label.pack_forget()

    def _render_sub_visibility(self) -> None:
        if not self._show_sub:
            self._sub_frame.grid_remove()
            return
        if self._state.sub_label or self._state.sub_total is not None:
            self._sub_frame.grid()
        else:
            self._sub_frame.grid_remove()

    def _render_sub(self) -> None:
        if not self._show_sub:
            return
        state = self._state

        if state.sub_label:
            self._sub_label.configure(text=state.sub_label)
            self._sub_label.grid()
        else:
            self._sub_label.configure(text="")
            self._sub_label.grid_remove()

        if state.sub_total is not None and state.sub_total > 0:
            self._sub_bar.set(_ratio(state.sub_completed, state.sub_total))
            self._sub_bar.grid()
        else:
            self._sub_bar.grid_remove()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        self._destroyed = True

        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except Exception:
                pass
            self._refresh_after_id = None

        if self._animating:
            try:
                self._main_bar.stop()
            except Exception:
                pass
            self._animating = False
