"""Main view (application shell) for the NexusDL GUI.

The shell owns **navigation only**:

* a nav (left sidebar or top bar) listing the registered views,
* a content area that swaps between views,
* optional global status bar,
* optional keyboard shortcuts ``Ctrl+1``..``Ctrl+9``.

It knows **no concrete view**, **no service**, **no model**. Views are
declared declaratively via :class:`ViewSpec` — each spec carries a
``factory(master) -> widget`` that the composition root writes, closing over
whatever services that particular view needs. Adding a new screen is a new
:class:`ViewSpec` entry. Nothing here needs to change.

Universal design notes:
    * Views are **lazy by default**: a view's factory is invoked only the
      first time it is activated, keeping startup fast and tests headless.
    * Views can be **kept alive** (default — state preserved across swaps)
      or **destroyed on hide** (``destroy_on_hide=True`` — memory reclaim
      for heavy screens), at the host's discretion per view spec.
    * **Lifecycle hooks** are duck-typed: if a view defines
      ``on_activate()`` / ``on_deactivate()``, the shell calls them at the
      right moment. Views that don't need them simply don't define them.
    * **Nav position** is runtime-switchable between ``"left"`` and
      ``"top"``; the sidebar is collapsible (icon-only mode) when enabled.
    * **Keyboard shortcuts** ``Ctrl+1``..``Ctrl+9`` map to the first nine
      views, in registration order. Bindings are added with ``add="+"`` on
      the top-level window so the host's own bindings are preserved.
    * **State is serializable** via :meth:`MainView.get_state` /
      :meth:`MainView.set_state` — no coupling to ``Settings``.
    * The shell follows the CTk appearance mode via a 1 s poll (nav labels
      do not re-theme themselves).
    * All callbacks are isolated — a misbehaving host never breaks Tk.
    * Cleans up after itself on ``<Destroy>``.

Layout rebuild semantics
------------------------
:meth:`MainView.set_nav_position` and :meth:`MainView.set_state` may call
:meth:`MainView._rebuild_layout`, which tears down the nav frame, the
content frame, and every live view instance before rebuilding them under
the new grid layout.

The teardown is **explicit and hook-respecting**: for every live view, the
shell calls ``on_deactivate()`` (giving the view a chance to release
threads, subscriptions, and other resources), then destroys the widget.
A layout switch is a legitimate time for a view to lose transient state
(a window resize is not a UX-level navigation event, but a left-to-right
swap is a large change that the host should observe). The alternative —
silently destroying the widgets without calling ``on_deactivate`` —
leaks whatever the view's ``__init__`` acquired.

Because the views are destroyed, the current view is re-activated after
the rebuild: its factory is called again and ``on_activate`` is called
on the fresh instance. The view spec is responsible for rebuilding the
state it needs from its own source (the database, the settings object,
the last request). A view that cannot rebuild its state from scratch
should be marked ``destroy_on_hide=False`` and register itself in the
shell's preload list — the layout rebuild respects that.

Idempotent set_state
--------------------
:meth:`MainView.set_state` used to call ``set_nav_position`` and
``set_current`` unconditionally. When the target state matched the
current one, this triggered a full ``_rebuild_layout`` (destroying and
recreating every live view) followed by another view swap — a double
``on_activate`` and unnecessary work. The current implementation checks
each field against the current one and only acts when it actually
changes.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import customtkinter as ctk
from loguru import logger as _logger

_log = _logger.bind(component="gui.main_view")


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ViewSpec:
    """Declarative description of one application view.

    Attributes:
        id: Stable unique identifier (also used for shortcut indexing and
            state persistence).
        label: Displayed label in the nav.
        icon: Short glyph or emoji shown before the label (and alone when
            the sidebar is collapsed).
        factory: ``(master) -> widget``. Called lazily on first activation.
            The returned widget **must** be a :class:`ctk.CTkFrame`
            (or subclass).
        tooltip: Optional long label used in tooltips / accessibility.
        destroy_on_hide: If ``True``, the view is destroyed when hidden and
            rebuilt on return. Defaults to the shell's ``destroy_on_hide``
            value.
        shortcut: Optional override of the automatic ``Ctrl+N`` shortcut
            (e.g. ``"Control-s"``). Use ``""`` to disable.
    """

    id: str
    label: str
    icon: str
    factory: Callable[[ctk.CTkFrame], ctk.CTkFrame]
    tooltip: str = ""
    destroy_on_hide: bool | None = None
    shortcut: str | None = None


@dataclass(frozen=True, slots=True)
class MainViewLabels:
    """All user-visible strings used by :class:`MainView`."""

    collapse: str = "◀"
    expand: str = "▶"
    status_info: str = ""
    status_warning: str = "Warning"
    status_error: str = "Error"
    status_success: str = "OK"


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_THEME_LIGHT = {
    "bg": "#F5F5F5",
    "nav_bg": "#ECECEC",
    "nav_item_bg": "#ECECEC",
    "nav_item_hover": "#DDDDDD",
    "nav_item_active": "#3B8ED0",
    "nav_item_active_fg": "#FFFFFF",
    "nav_item_fg": "#1A1A1A",
    "separator": "#CCCCCC",
    "status_bg": "#E8E8E8",
    "status_info": "#1A1A1A",
    "status_warning": "#7A5A00",
    "status_error": "#C0392B",
    "status_success": "#1E6E39",
}

_THEME_DARK = {
    "bg": "#1A1A1A",
    "nav_bg": "#1F1F1F",
    "nav_item_bg": "#1F1F1F",
    "nav_item_hover": "#2E2E2E",
    "nav_item_active": "#1F6AA5",
    "nav_item_active_fg": "#FFFFFF",
    "nav_item_fg": "#DCDCDC",
    "separator": "#333333",
    "status_bg": "#242424",
    "status_info": "#DCDCDC",
    "status_warning": "#FFD966",
    "status_error": "#E74C3C",
    "status_success": "#A4E5B8",
}

_THEME_POLL_MS = 1000
_STATUS_HIDE_MS = 5000
_COLLAPSED_WIDTH = 52
_NAV_POSITIONS = ("left", "top")
_STATUS_KINDS = ("info", "warning", "error", "success")


# ---------------------------------------------------------------------------
# Lifecycle protocol (duck-typed, optional)
# ---------------------------------------------------------------------------

@runtime_checkable
class _ActivatableView(Protocol):
    """A view that wants activation callbacks from the shell."""

    def on_activate(self) -> None: ...


@runtime_checkable
class _DeactivatableView(Protocol):
    """A view that wants deactivation callbacks from the shell."""

    def on_deactivate(self) -> None: ...


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class MainView(ctk.CTkFrame):
    """Application shell hosting a set of navigable views.

    Parameters:
        master: Parent widget.
        views: Ordered list of :class:`ViewSpec`. Order defines the
            ``Ctrl+1``..``Ctrl+9`` shortcut mapping and the nav order.
        labels: Localized labels. ``None`` uses English defaults.
        nav_position: ``"left"`` (sidebar) or ``"top"`` (top bar).
        nav_width: Sidebar width in pixels (left mode).
        nav_collapsible: Enable the collapse/expand toggle (left mode only).
        nav_initial_collapsed: Start collapsed if ``nav_collapsible``.
        lazy: If ``True`` (default), view factories are invoked on first
            activation. If ``False``, all views are built at construction.
        destroy_on_hide: Default destroy-on-hide policy. Per-spec overrides
            take precedence. ``False`` (default) = keep-alive.
        initial_view_id: View to show first. Defaults to ``views[0].id``.
        show_status_bar: Show the optional status bar at the bottom.
        shortcuts_enabled: Enable ``Ctrl+1``..``Ctrl+9`` shortcuts.
        on_active_view_change: Called with ``(view_id, label)`` whenever the
            active view changes. Useful to set the window title.
        on_shortcut: Optional override — if set, shortcuts call this instead
            of performing navigation. Called with ``(view_id)``.
    """

    def __init__(
        self,
        master,
        *,
        views: Sequence[ViewSpec],
        labels: MainViewLabels | None = None,
        nav_position: str = "left",
        nav_width: int = 200,
        nav_collapsible: bool = True,
        nav_initial_collapsed: bool = False,
        lazy: bool = True,
        destroy_on_hide: bool = False,
        initial_view_id: str | None = None,
        show_status_bar: bool = False,
        shortcuts_enabled: bool = True,
        on_active_view_change: Callable[[str, str], None] | None = None,
        on_shortcut: Callable[[str], None] | None = None,
        **kwargs,
    ) -> None:
        if not views:
            raise ValueError("MainView requires at least one ViewSpec")
        _validate_view_ids(views)
        if nav_position not in _NAV_POSITIONS:
            raise ValueError(f"Invalid nav_position: {nav_position!r}")

        kwargs.setdefault("corner_radius", 0)
        super().__init__(master, **kwargs)

        self._labels = labels or MainViewLabels()
        self._views = list(views)
        self._views_by_id = {v.id: v for v in self._views}
        self._lazy = lazy
        self._default_destroy_on_hide = destroy_on_hide
        self._nav_position = nav_position
        self._nav_width = max(_COLLAPSED_WIDTH, nav_width)
        self._nav_collapsible = nav_collapsible and nav_position == "left"
        self._collapsed = bool(nav_initial_collapsed) and self._nav_collapsible
        self._show_status_bar = show_status_bar
        self._shortcuts_enabled = shortcuts_enabled
        self._on_active_view_change = on_active_view_change
        self._on_shortcut = on_shortcut

        self._instances: dict[str, ctk.CTkFrame] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self._current_view_id: str | None = None

        if initial_view_id is not None and initial_view_id not in self._views_by_id:
            raise KeyError(f"initial_view_id not in views: {initial_view_id!r}")
        self._initial_view_id = initial_view_id or self._views[0].id

        # Runtime state.
        self._status_after_id: str | None = None
        self._theme_after_id: str | None = None
        self._destroyed = False
        self._last_theme_mode: str = self._current_mode()
        self._shortcut_bindings: list[tuple[str, str]] = []

        self._build()
        self._apply_theme()
        self._install_shortcuts()

        # Eager-build every view if lazy is off.
        if not self._lazy:
            for spec in self._views:
                self._ensure_view_instance(spec.id)

        self.set_current(self._initial_view_id, notify=False)

        self.bind("<Destroy>", self._on_destroy, add="+")
        self._schedule_theme_check()

    # ------------------------------------------------------------------
    # Public API — navigation
    # ------------------------------------------------------------------

    @property
    def current_view_id(self) -> str | None:
        """The id of the currently active view, or ``None`` before activation."""
        return self._current_view_id

    def set_current(self, view_id: str, *, notify: bool = True) -> None:
        """Activate the view registered under ``view_id``.

        Raises:
            KeyError: If ``view_id`` is unknown.
        """
        if self._destroyed:
            return
        if view_id not in self._views_by_id:
            raise KeyError(f"Unknown view id: {view_id!r}")
        if view_id == self._current_view_id:
            return

        previous_id = self._current_view_id
        self._current_view_id = view_id

        # Hide previous.
        if previous_id is not None:
            self._hide_view(previous_id)

        # Ensure / show target.
        instance = self._ensure_view_instance(view_id)
        self._show_view(view_id, instance)
        self._refresh_nav_selection()

        if notify and self._on_active_view_change is not None:
            spec = self._views_by_id[view_id]
            self._safe_call(self._on_active_view_change, view_id, spec.label)

    def get_view(self, view_id: str) -> ctk.CTkFrame | None:
        """Return the live view instance, or ``None`` if not built yet."""
        return self._instances.get(view_id)

    def preload(self, view_id: str) -> None:
        """Build ``view_id`` now without activating it."""
        if self._destroyed:
            return
        self._ensure_view_instance(view_id)

    def preload_all(self) -> None:
        """Build all views now (useful after the splash screen)."""
        if self._destroyed:
            return
        for spec in self._views:
            self._ensure_view_instance(spec.id)

    def register_view(self, spec: ViewSpec, *, activate: bool = False) -> None:
        """Register a new view at runtime.

        Raises:
            ValueError: On duplicate id.
        """
        if self._destroyed:
            return
        if spec.id in self._views_by_id:
            raise ValueError(f"Duplicate view id: {spec.id!r}")
        self._views.append(spec)
        self._views_by_id[spec.id] = spec
        self._rebuild_nav()
        self._reinstall_shortcuts()
        if activate:
            self.set_current(spec.id)

    # ------------------------------------------------------------------
    # Public API — nav
    # ------------------------------------------------------------------

    def set_nav_position(self, position: str) -> None:
        """Switch the nav between ``"left"`` and ``"top"`` at runtime."""
        if self._destroyed or position == self._nav_position:
            return
        if position not in _NAV_POSITIONS:
            raise ValueError(f"Invalid nav position: {position!r}")
        self._nav_position = position
        if position == "top":
            self._collapsed = False
        self._rebuild_layout()

    def set_nav_collapsed(self, collapsed: bool) -> None:
        """Collapse or expand the sidebar (left mode only)."""
        if self._destroyed or not self._nav_collapsible:
            return
        new = bool(collapsed)
        if new == self._collapsed:
            return
        self._collapsed = new
        self._refresh_nav_selection()
        self._refresh_nav_labels()
        self._nav_frame.configure(
            width=_COLLAPSED_WIDTH if new else self._nav_width
        )

    def toggle_nav_collapsed(self) -> None:
        if self._nav_collapsible:
            self.set_nav_collapsed(not self._collapsed)

    # ------------------------------------------------------------------
    # Public API — status bar
    # ------------------------------------------------------------------

    def set_status(
        self,
        text: str,
        *,
        kind: str = "info",
        auto_hide_ms: int | None = _STATUS_HIDE_MS,
    ) -> None:
        """Show a status message (or clear it with empty ``text``)."""
        if self._destroyed or not self._show_status_bar:
            return
        if kind not in _STATUS_KINDS:
            kind = "info"

        if self._status_after_id is not None:
            try:
                self.after_cancel(self._status_after_id)
            except Exception:
                pass
            self._status_after_id = None

        if not text:
            self._status_label.configure(text="")
            self._refresh_status_theme()
            return

        theme = self._theme()
        color = theme.get(f"status_{kind}", theme["status_info"])
        try:
            self._status_label.configure(text=text, text_color=color)
        except Exception:
            return
        self._refresh_status_theme()

        if auto_hide_ms and auto_hide_ms > 0:
            self._status_after_id = self.after(
                auto_hide_ms, self._clear_status
            )

    def _clear_status(self) -> None:
        self._status_after_id = None
        if self._destroyed:
            return
        try:
            self._status_label.configure(text="")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API — state
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of the shell state."""
        return {
            "current_view": self._current_view_id,
            "nav_collapsed": self._collapsed,
            "nav_position": self._nav_position,
        }

    def set_state(self, state: dict[str, Any]) -> None:
        """Restore a snapshot produced by :meth:`get_state`.

        Unknown or missing keys are silently ignored — safe against version
        drift.

        Each field is checked against the current one and only acted upon
        when it actually changes. A state that matches the current one is
        a no-op — without the idempotency check, a "same state" call
        would rebuild the layout (destroying every live view) and then
        rebuild it back, causing a double ``on_activate`` on the current
        view for no visible reason.
        """
        if self._destroyed:
            return
        # Phase 1: layout state (may rebuild the structure).
        pos = state.get("nav_position")
        if (
            isinstance(pos, str)
            and pos in _NAV_POSITIONS
            and pos != self._nav_position
        ):
            self.set_nav_position(pos)
        coll = state.get("nav_collapsed")
        if isinstance(coll, bool) and coll != self._collapsed:
            self.set_nav_collapsed(coll)
        # Phase 2: current view (no rebuild).
        current = state.get("current_view")
        if isinstance(current, str) and current in self._views_by_id:
            self.set_current(current)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        """(Re)construct the outer grid based on nav_position.

        Every live view is deactivated (``on_deactivate()``) and
        destroyed before the structural frames are torn down — the views
        live inside ``_content_frame`` and destroying the frame without
        notifying them would leak whatever they hold (threads, event
        subscriptions, async tasks). The current view is re-activated
        after the rebuild; its factory is called fresh, and the view is
        responsible for reconstructing the state it needs.

        See the module docstring's "Layout rebuild semantics" section.
        """
        # Phase 1: deactivate every live view and destroy them
        # explicitly so a view's on_deactivate hook always runs.
        for view_id in list(self._instances.keys()):
            instance = self._instances.get(view_id)
            if instance is None:
                continue
            if isinstance(instance, _DeactivatableView) and callable(
                getattr(instance, "on_deactivate", None)
            ):
                try:
                    instance.on_deactivate()
                except Exception:
                    _log.exception(
                        "on_deactivate raised for view {!r} during layout "
                        "rebuild",
                        view_id,
                    )
            try:
                instance.destroy()
            except Exception:
                pass
        self._instances.clear()
        self._nav_buttons.clear()

        # Phase 2: tear down the structural frames.
        for attr in ("_nav_frame", "_content_frame", "_status_bar"):
            existing = getattr(self, attr, None)
            if existing is not None:
                try:
                    existing.destroy()
                except Exception:
                    pass
                setattr(self, attr, None)
        for child in self.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        # Phase 3: build the new layout.
        if self._nav_position == "left":
            self.grid_columnconfigure(0, weight=0)
            self.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self.grid_rowconfigure(1, weight=0)
            nav_row, nav_col, nav_sticky = 0, 0, "ns"
            content_row, content_col, content_sticky = 0, 1, "nsew"
            status_row = 1
        else:  # top
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(0, weight=0)
            self.grid_rowconfigure(1, weight=1)
            self.grid_rowconfigure(2, weight=0)
            nav_row, nav_col, nav_sticky = 0, 0, "ew"
            content_row, content_col, content_sticky = 1, 0, "nsew"
            status_row = 2

        # Nav.
        if self._nav_position == "left":
            self._nav_frame = ctk.CTkFrame(
                self,
                width=_COLLAPSED_WIDTH if self._collapsed else self._nav_width,
                corner_radius=0,
                fg_color=self._theme()["nav_bg"],
            )
            self._nav_frame.grid_propagate(False)
        else:
            self._nav_frame = ctk.CTkFrame(
                self, corner_radius=0, fg_color=self._theme()["nav_bg"]
            )
        self._nav_frame.grid(
            row=nav_row, column=nav_col, sticky=nav_sticky
        )
        self._rebuild_nav()

        # Content.
        self._content_frame = ctk.CTkFrame(
            self, corner_radius=0, fg_color=self._theme()["bg"]
        )
        self._content_frame.grid(
            row=content_row, column=content_col, sticky=content_sticky
        )
        self._content_frame.grid_columnconfigure(0, weight=1)
        self._content_frame.grid_rowconfigure(0, weight=1)

        # Status bar (optional).
        if self._show_status_bar:
            self._status_bar = ctk.CTkFrame(
                self, corner_radius=0, fg_color=self._theme()["status_bg"]
            )
            self._status_bar.grid(
                row=status_row, column=0, columnspan=2, sticky="ew"
            )
            self._status_label = ctk.CTkLabel(
                self._status_bar, text="", anchor="w", font=("", 11)
            )
            self._status_label.pack(fill="x", padx=10, pady=4)

        # Phase 4: re-activate the current view (if any). Its instance
        # was destroyed in phase 1, so this rebuilds it from the spec.
        if self._current_view_id is not None:
            current_id = self._current_view_id
            self._current_view_id = None
            self.set_current(current_id, notify=False)

    def _rebuild_nav(self) -> None:
        if not hasattr(self, "_nav_frame") or self._nav_frame is None:
            return
        for child in self._nav_frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        self._nav_buttons.clear()

        horizontal = self._nav_position == "top"
        for spec in self._views:
            button = ctk.CTkButton(
                self._nav_frame,
                text="",
                anchor="w" if not horizontal else "center",
                fg_color="transparent",
                hover_color=self._theme()["nav_item_hover"],
                text_color=self._theme()["nav_item_fg"],
                command=lambda vid=spec.id: self.set_current(vid),
            )
            if horizontal:
                button.pack(side="left", padx=2, pady=4, fill="y")
            else:
                button.pack(fill="x", padx=6, pady=2)
            self._nav_buttons[spec.id] = button

        if self._nav_collapsible and self._nav_position == "left":
            self._collapse_btn = ctk.CTkButton(
                self._nav_frame,
                text="",
                width=28,
                height=24,
                fg_color="transparent",
                border_width=1,
                command=self.toggle_nav_collapsed,
            )
            self._collapse_btn.pack(side="bottom", pady=(0, 8), padx=6)
        else:
            self._collapse_btn = None

        self._refresh_nav_labels()
        self._refresh_nav_selection()

    def _refresh_nav_labels(self) -> None:
        for view_id, button in self._nav_buttons.items():
            spec = self._views_by_id[view_id]
            if self._collapsed:
                text = spec.icon
            else:
                text = f"{spec.icon}  {spec.label}" if spec.icon else spec.label
            try:
                button.configure(text=text)
            except Exception:
                pass
        if getattr(self, "_collapse_btn", None) is not None:
            try:
                self._collapse_btn.configure(
                    text=self._labels.expand if self._collapsed else self._labels.collapse
                )
            except Exception:
                pass

    def _refresh_nav_selection(self) -> None:
        theme = self._theme()
        for view_id, button in self._nav_buttons.items():
            active = view_id == self._current_view_id
            try:
                button.configure(
                    fg_color=theme["nav_item_active"] if active else "transparent",
                    text_color=(
                        theme["nav_item_active_fg"] if active else theme["nav_item_fg"]
                    ),
                    hover_color=theme["nav_item_hover"],
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # View lifecycle
    # ------------------------------------------------------------------

    def _ensure_view_instance(self, view_id: str) -> ctk.CTkFrame:
        if view_id in self._instances:
            return self._instances[view_id]
        spec = self._views_by_id[view_id]
        try:
            instance = spec.factory(self._content_frame)
        except Exception:
            _log.exception("View factory raised for {!r}", view_id)
            # Build a fallback so the shell keeps working.
            instance = ctk.CTkLabel(
                self._content_frame,
                text=f"View {view_id!r} failed to load.",
                anchor="center",
                text_color="#E74C3C",
            )
        try:
            instance.grid(row=0, column=0, sticky="nsew")
            instance.grid_remove()  # hidden until activated
        except Exception:
            pass
        self._instances[view_id] = instance
        return instance

    def _show_view(self, view_id: str, instance: ctk.CTkFrame) -> None:
        try:
            instance.grid()
        except Exception:
            try:
                instance.pack(fill="both", expand=True)
            except Exception:
                _log.exception("Failed to show view {!r}", view_id)
        # Call on_activate() if defined.
        if isinstance(instance, _ActivatableView) and callable(
            getattr(instance, "on_activate", None)
        ):
            try:
                instance.on_activate()
            except Exception:
                _log.exception("on_activate raised for view {!r}", view_id)

    def _hide_view(self, view_id: str) -> None:
        instance = self._instances.get(view_id)
        if instance is None:
            return
        # on_deactivate first, while the view is still mounted.
        if isinstance(instance, _DeactivatableView) and callable(
            getattr(instance, "on_deactivate", None)
        ):
            try:
                instance.on_deactivate()
            except Exception:
                _log.exception("on_deactivate raised for view {!r}", view_id)

        try:
            instance.grid_remove()
        except Exception:
            try:
                instance.pack_forget()
            except Exception:
                pass

        spec = self._views_by_id[view_id]
        destroy = (
            spec.destroy_on_hide
            if spec.destroy_on_hide is not None
            else self._default_destroy_on_hide
        )
        if destroy:
            try:
                instance.destroy()
            except Exception:
                pass
            self._instances.pop(view_id, None)

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------

    def _install_shortcuts(self) -> None:
        if not self._shortcuts_enabled:
            return
        # Idempotent — a second install would attach duplicate handlers
        # to the top-level window.
        self._uninstall_shortcuts()
        try:
            top = self.winfo_toplevel()
        except Exception:
            return

        for idx, spec in enumerate(self._views[:9], start=1):
            if spec.shortcut is None:
                sequence = f"<Control-Key-{idx}>"
            elif spec.shortcut == "":
                continue
            else:
                sequence = spec.shortcut
            try:
                bind_id = top.bind(
                    sequence,
                    self._make_shortcut_handler(spec.id),
                    add="+",
                )
                if bind_id:
                    self._shortcut_bindings.append((sequence, bind_id))
            except Exception:
                _log.debug("Failed to bind shortcut {}", sequence)

    def _reinstall_shortcuts(self) -> None:
        self._uninstall_shortcuts()
        self._install_shortcuts()

    def _uninstall_shortcuts(self) -> None:
        if not self._shortcut_bindings:
            return
        try:
            top = self.winfo_toplevel()
        except Exception:
            self._shortcut_bindings.clear()
            return
        for sequence, bind_id in self._shortcut_bindings:
            try:
                top.unbind(sequence, bind_id)
            except Exception:
                pass
        self._shortcut_bindings.clear()

    def _make_shortcut_handler(self, view_id: str) -> Callable[[Any], str]:
        def _handler(_event=None) -> str:
            if self._on_shortcut is not None:
                self._safe_call(self._on_shortcut, view_id)
            else:
                try:
                    self.set_current(view_id)
                except Exception:
                    _log.exception("Shortcut handler failed for {!r}", view_id)
            return "break"
        return _handler

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
        if getattr(self, "_nav_frame", None) is not None:
            try:
                self._nav_frame.configure(fg_color=theme["nav_bg"])
            except Exception:
                pass
        if getattr(self, "_content_frame", None) is not None:
            try:
                self._content_frame.configure(fg_color=theme["bg"])
            except Exception:
                pass
        self._refresh_status_theme()
        self._refresh_nav_selection()
        self._refresh_nav_labels()

    def _refresh_status_theme(self) -> None:
        if not getattr(self, "_show_status_bar", False):
            return
        if getattr(self, "_status_bar", None) is None:
            return
        theme = self._theme()
        try:
            self._status_bar.configure(fg_color=theme["status_bg"])
        except Exception:
            pass
        try:
            current_color = self._status_label.cget("text_color")
        except Exception:
            current_color = None
        if not getattr(self._status_label, "_kind_color", None):
            try:
                self._status_label.configure(text_color=theme["status_info"])
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
    # Misc
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_call(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:
            _log.exception(
                "MainView: callback {!r} raised",
                getattr(cb, "__name__", cb),
            )

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        if self._destroyed:
            return
        self._destroyed = True
        self._uninstall_shortcuts()
        for attr in ("_status_after_id", "_theme_after_id"):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_view_ids(views: Sequence[ViewSpec]) -> None:
    seen: set[str] = set()
    for spec in views:
        if not spec.id or not isinstance(spec.id, str):
            raise ValueError(f"ViewSpec.id must be a non-empty string: {spec!r}")
        if spec.id in seen:
            raise ValueError(f"Duplicate view id: {spec.id!r}")
        seen.add(spec.id)
