"""Site selection dropdown for the NexusDL GUI.

Presentation-only component: it renders a list of :class:`SiteConfig`
objects and emits a selection event. It does **not** fetch sites, does
**not** know about :class:`Settings`, :class:`SiteRegistry` or
:class:`EventBus` — the hosting view wires those and passes an
already-filtered list.

Universal design notes:
    * The list can be updated at runtime via :meth:`SiteDropdown.update_sites`
      (e.g. after a settings change enables adult content).
    * Health indicators are refreshed via :meth:`SiteDropdown.update_health`,
      so the hosting view can subscribe once to ``site.health.changed``
      events and push updates here without re-creating the widget.
    * Any :class:`Language` value is supported out of the box; unknown
      values fall back to a generic globe emoji.
    * Grouping is data-driven (``group_by="language"``) and can be turned
      off or changed without touching the rendering code.
    * No hardcoded site names, no hardcoded URLs — display data comes
      exclusively from the ``SiteConfig`` instances passed in.

Flag emoji fallback
-------------------
The language column uses emoji flags (``🇫🇷``, ``🇬🇧``, …) which are
Unicode **Regional Indicator** pairs. They render as the intended
two-letter flag only when a colour-emoji font is installed — Noto Color
Emoji on Linux (``fonts-noto-color-emoji``), Segoe UI Emoji on Windows,
Apple Color Emoji on macOS. On a minimal Linux install without a colour-
emoji font, Tk falls back to drawing the two Regional Indicators as
plain letters or as two missing-glyph boxes. The widget does not detect
this: it always renders the emoji, and a deployment that cares about
the fallback installs the font. A future ``use_emoji_flags=False``
option (out of scope for this file) would render ``"FR"``, ``"EN"``
instead; the strings live in :data:`_LANGUAGE_FLAGS` so the change is a
single-table swap.

Search and diacritics
---------------------
:meth:`SiteDropdown._visible_sites` matches the search query against
``site.name`` and ``site.id`` using a **diacritic-insensitive**,
case-insensitive comparison. A user typing ``"equipe"`` matches a site
named ``"L'Équipe"``, and ``"mangadex"`` matches ``"MangaDex"``. The
normalisation uses :func:`unicodedata.normalize` with the ``NFKD`` form
and strips combining marks — the same primitive :func:`slugify.slugify`
uses internally, kept local here so the widget has no runtime
dependency on the slugify package.
"""
from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence

import customtkinter as ctk
from loguru import logger as _logger

from nexusdl.core.models.enums import Language, SiteHealth
from nexusdl.core.models.site import SiteConfig

_log = _logger.bind(component="gui.site_dropdown")

# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_LANGUAGE_FLAGS: dict[Language, str] = {
    Language.FR: "🇫🇷",
    Language.EN: "🇬🇧",
    Language.JA: "🇯🇵",
    Language.KO: "🇰🇷",
    Language.ZH: "🇨🇳",
    Language.ES: "🇪🇸",
    Language.PT: "🇵🇹",
    Language.IT: "🇮🇹",
    Language.DE: "🇩🇪",
    Language.RU: "🇷🇺",
    Language.AR: "🇸🇦",
    Language.UNKNOWN: "🌐",
}

_HEALTH_COLORS: dict[SiteHealth, str] = {
    SiteHealth.HEALTHY: "#2ECC71",
    SiteHealth.DEGRADED: "#F1C40F",
    SiteHealth.DOWN: "#E74C3C",
    SiteHealth.UNKNOWN: "#95A5A6",
}

_BUTTON_BG = ("gray95", "gray13")
_BUTTON_HOVER_BG = ("gray85", "gray20")
_SELECTED_BG = ("#3B8ED0", "#1F6AA5")
_FOCUSED_BG = ("#D6E4F0", "#1A3D5C")
_HOVER_BG = ("#E8E8E8", "#2B2B2B")

_DEFAULT_TEXT_COLOR = ("gray10", "gray90")
_SELECTED_TEXT_COLOR = ("white", "white")
_MUTED_TEXT_COLOR = ("gray40", "gray60")

_SELECTED_PREFIX = "✓  "
_EMPTY_LABEL = "—"

POPUP_WIDTH_MIN = 320
POPUP_HEIGHT = 360
ROW_HEIGHT = 32
BUTTON_HEIGHT = 36


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_search_text(value: str) -> str:
    """Return *value* lowercased with combining marks stripped.

    A user typing ``"equipe"`` matches a site named ``"L'Équipe"``; a
    user typing ``"MangaDex"`` matches ``"mangadex"``. The transform is
    the same primitive :func:`slugify.slugify` uses internally, kept
    local so the widget does not depend on the slugify package at
    runtime.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return (
        decomposed.encode("ASCII", "ignore").decode("ASCII").lower()
    )


def _flag_for(language: Language | str) -> str:
    """Return the flag emoji for ``language`` (globe fallback on unknown)."""
    try:
        lang = language if isinstance(language, Language) else Language(language)
    except (TypeError, ValueError):
        return _LANGUAGE_FLAGS[Language.UNKNOWN]
    return _LANGUAGE_FLAGS.get(lang, _LANGUAGE_FLAGS[Language.UNKNOWN])


def _group_key_for(site: SiteConfig, group_by: str | None) -> str:
    """Return the grouping key for ``site`` for the given ``group_by`` mode."""
    if group_by == "language":
        try:
            lang = (
                site.language
                if isinstance(site.language, Language)
                else Language(site.language)
            )
        except (TypeError, ValueError):
            return "OTHER"
        return lang.name
    return ""


def _health_of(site: SiteConfig) -> SiteHealth:
    """Best-effort extraction of :class:`SiteHealth` from ``site``."""
    value = getattr(site, "health", SiteHealth.UNKNOWN)
    if isinstance(value, SiteHealth):
        return value
    try:
        return SiteHealth(value)
    except (TypeError, ValueError):
        return SiteHealth.UNKNOWN


def _force_widget_focus(widget) -> None:
    """Give ``widget`` keyboard focus, bypassing the WM's focus policy.

    Toplevels created with ``overrideredirect(True)`` do not receive
    WM focus automatically on Windows and on some Linux window
    managers (i3, sway, GNOME with strict focus). ``focus_set`` waits
    for the WM and is silently ignored in those environments, so the
    popup's keyboard navigation (arrow keys, Enter, Escape) would not
    work until the user clicked inside.

    ``focus_force`` asks Tk to set focus directly without going
    through the WM, which is what every popular Tk dropdown
    implementation (combobox, autocomplete) uses for exactly this
    reason. Wrapped in ``try/except`` because a destroyed widget
    raises :class:`tkinter.TclError`.
    """
    try:
        widget.focus_force()
        widget.lift()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class SiteDropdown(ctk.CTkFrame):
    """A compact, searchable, health-aware site selector.

    Presentation-only by design. The hosting view is responsible for:

    * feeding an already-authorized list of :class:`SiteConfig` (i.e.
      calling ``registry.list_sites(include_adult=settings.allow_adult)``),
    * pushing health updates via :meth:`update_health` (typically from a
      ``site.health.changed`` subscription on the :class:`EventBus`),
    * reacting to :attr:`on_change`.

    Parameters:
        master: Parent widget.
        sites: Pre-filtered, pre-authorized sites to display.
        on_change: Callback invoked with the newly selected
            :class:`SiteConfig` (or ``None`` when cleared). Always called
            from the Tk main thread; exceptions are logged, never
            propagated.
        initial_site_id: Site id to preselect. Ignored if unknown.
        searchable: Whether to show a search box in the popup.
        group_by: ``None`` for a flat list, ``"language"`` (default) to
            group rows by language code.
        show_health: Whether to render a colored health dot per row.
        allow_clear: If ``True`` (default), expose a "Clear selection"
            entry that sets the selection to ``None``.
    """

    def __init__(
        self,
        master,
        *,
        sites: Sequence[SiteConfig],
        on_change: Callable[[SiteConfig | None], None] | None = None,
        initial_site_id: str | None = None,
        searchable: bool = True,
        group_by: str | None = "language",
        show_health: bool = True,
        allow_clear: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)

        self._on_change = on_change
        self._searchable = searchable
        self._group_by = group_by
        self._show_health = show_health
        self._allow_clear = allow_clear

        self._sites: list[SiteConfig] = list(sites)
        self._by_id: dict[str, SiteConfig] = {s.id: s for s in self._sites}
        if len(self._by_id) != len(self._sites):
            raise ValueError("Duplicate site IDs in the provided sites list")

        self._health: dict[str, SiteHealth] = {s.id: _health_of(s) for s in self._sites}
        self._selected_id: str | None = None
        self._focused_id: str | None = None

        # Popup state — all ``None`` while the popup is closed.
        self._popup: ctk.CTkToplevel | None = None
        self._popup_list: ctk.CTkScrollableFrame | None = None
        self._search_var: ctk.StringVar | None = None
        self._search_entry: ctk.CTkEntry | None = None
        self._root_bind_id: str | None = None

        #: Set to ``True`` by :meth:`_on_destroy`. Guards the second
        #: ``<Destroy>`` binding that Tk delivers to child widgets.
        self._destroyed: bool = False

        self._build_button()

        if initial_site_id is not None and initial_site_id in self._by_id:
            self._apply_selection(initial_site_id, notify=False)
        elif self._sites:
            self._apply_selection(self._sites[0].id, notify=False)
        else:
            self._refresh_button()

        # ``add="+"`` preserves any ``<Destroy>`` binding a future
        # subclass might add — the widget itself has none today, but
        # this method may be subclassed.
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_selected(self) -> SiteConfig | None:
        """Return the currently selected :class:`SiteConfig`, or ``None``."""
        if self._selected_id is None:
            return None
        return self._by_id.get(self._selected_id)

    def get_selected_id(self) -> str | None:
        """Return the currently selected site id, or ``None``."""
        return self._selected_id

    def set_selected(self, site_id: str | None, *, notify: bool = True) -> None:
        """Programmatically select ``site_id`` (or clear with ``None``).

        Raises:
            KeyError: If ``site_id`` is not among the known sites.
            ValueError: If ``site_id`` is ``None`` while ``allow_clear``
                is ``False``.
        """
        if site_id is None:
            if not self._allow_clear:
                raise ValueError("Clearing the selection is not allowed")
        elif site_id not in self._by_id:
            raise KeyError(f"Unknown site id: {site_id!r}")
        self._apply_selection(site_id, notify=notify)

    def update_sites(self, sites: Sequence[SiteConfig]) -> None:
        """Replace the displayed sites.

        Known health entries are preserved across the swap; stale ones are
        dropped. If the previously selected site is gone, the first
        available site is auto-selected and :attr:`on_change` fires.
        """
        new_sites = list(sites)
        new_by_id = {s.id: s for s in new_sites}
        if len(new_by_id) != len(new_sites):
            raise ValueError("Duplicate site IDs in the provided sites list")

        self._sites = new_sites
        self._by_id = new_by_id

        for site in new_sites:
            self._health.setdefault(site.id, _health_of(site))
        for stale_id in list(self._health.keys()):
            if stale_id not in new_by_id:
                del self._health[stale_id]

        if self._selected_id is None or self._selected_id not in new_by_id:
            if new_sites:
                self._apply_selection(new_sites[0].id, notify=True)
            elif self._selected_id is not None:
                self._apply_selection(None, notify=True)

        if self._popup is not None:
            self._focused_id = self._resolve_initial_focus()
            self._refresh_list()

    def update_health(self, site_id: str, health: SiteHealth) -> None:
        """Update the health indicator for ``site_id``."""
        if site_id not in self._by_id:
            return
        self._health[site_id] = health
        if site_id == self._selected_id:
            self._refresh_button()
        if self._popup is not None:
            self._refresh_list()

    # ------------------------------------------------------------------
    # Button
    # ------------------------------------------------------------------

    def _build_button(self) -> None:
        self._button = ctk.CTkFrame(
            self,
            corner_radius=6,
            height=BUTTON_HEIGHT,
            fg_color=_BUTTON_BG,
            border_width=1,
            border_color=("gray70", "gray30"),
        )
        self._button.pack(fill="both", expand=True)
        self._button.pack_propagate(False)

        self._health_dot = ctk.CTkLabel(
            self._button,
            text="●",
            width=16,
            text_color=_HEALTH_COLORS[SiteHealth.UNKNOWN],
        )
        self._health_dot.pack(side="left", padx=(8, 4))

        self._name_label = ctk.CTkLabel(
            self._button,
            text=_EMPTY_LABEL,
            anchor="w",
            text_color=_DEFAULT_TEXT_COLOR,
        )
        self._name_label.pack(side="left", fill="x", expand=True)

        self._lang_label = ctk.CTkLabel(self._button, text="", width=24)
        self._lang_label.pack(side="left", padx=(4, 4))

        self._arrow_label = ctk.CTkLabel(
            self._button,
            text="▾",
            width=16,
            text_color=_MUTED_TEXT_COLOR,
        )
        self._arrow_label.pack(side="right", padx=(4, 8))

        for widget in (
            self._button,
            self._health_dot,
            self._name_label,
            self._lang_label,
            self._arrow_label,
        ):
            widget.bind("<Button-1>", self._on_button_click)
            widget.bind("<Enter>", self._on_button_enter)
            widget.bind("<Leave>", self._on_button_leave)
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _refresh_button(self) -> None:
        if self._selected_id is None:
            self._name_label.configure(text=_EMPTY_LABEL)
            self._lang_label.configure(text="")
            self._health_dot.configure(text_color=_HEALTH_COLORS[SiteHealth.UNKNOWN])
            return
        site = self._by_id[self._selected_id]
        self._name_label.configure(text=site.name)
        self._lang_label.configure(text=_flag_for(site.language))
        health = self._health.get(site.id, SiteHealth.UNKNOWN)
        self._health_dot.configure(text_color=_HEALTH_COLORS[health])

    def _on_button_enter(self, _event=None) -> None:
        try:
            self._button.configure(fg_color=_BUTTON_HOVER_BG)
        except Exception:
            pass

    def _on_button_leave(self, _event=None) -> None:
        try:
            self._button.configure(fg_color=_BUTTON_BG)
        except Exception:
            pass

    def _on_button_click(self, _event=None) -> None:
        self._toggle_popup()

    # ------------------------------------------------------------------
    # Popup lifecycle
    # ------------------------------------------------------------------

    def _toggle_popup(self) -> None:
        if self._popup is not None:
            self._close_popup()
        else:
            self._open_popup()

    def _open_popup(self) -> None:
        if self._popup is not None:
            return

        popup = ctk.CTkToplevel(self)
        popup.withdraw()  # avoid flicker before geometry is set
        popup.overrideredirect(True)
        try:
            popup.attributes("-topmost", True)
        except Exception:
            pass
        self._popup = popup

        if self._searchable:
            self._search_var = ctk.StringVar(master=popup)
            self._search_entry = ctk.CTkEntry(
                popup,
                placeholder_text="Search sites…",
                textvariable=self._search_var,
            )
            self._search_entry.pack(fill="x", padx=8, pady=(8, 4))
            self._search_entry.bind("<KeyRelease>", self._on_search_changed)
            self._search_entry.bind("<Down>", self._on_key_down)
            self._search_entry.bind("<Up>", self._on_key_up)
            self._search_entry.bind("<Return>", self._on_key_return)
            self._search_entry.bind("<Escape>", self._on_escape)

        self._popup_list = ctk.CTkScrollableFrame(popup)
        self._popup_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._focused_id = self._resolve_initial_focus()
        self._refresh_list()

        popup.deiconify()
        self._position_popup()
        popup.lift()

        popup.bind("<Escape>", self._on_escape)
        # Defensive focus fix for WMs where overrideredirect drops focus.
        popup.bind(
            "<Button-1>",
            lambda _e: (self._search_entry.focus_set() if self._search_entry else None),
            add="+",
        )

        try:
            self._root_bind_id = self.winfo_toplevel().bind(
                "<Button-1>", self._on_root_click, add="+"
            )
        except Exception:
            self._root_bind_id = None

        # Give keyboard focus to the popup. ``focus_force`` bypasses
        # the WM's focus policy, which is required for overrideredirect
        # Toplevels on Windows and some Linux WMs — a plain
        # ``focus_set`` would be silently ignored there and the
        # popup's arrow-key navigation would not work until the user
        # clicked inside it.
        focus_target = self._search_entry if self._search_entry is not None else popup
        popup.after(10, lambda: _force_widget_focus(focus_target))

    def _close_popup(self) -> None:
        if self._root_bind_id is not None:
            try:
                self.winfo_toplevel().unbind("<Button-1>", self._root_bind_id)
            except Exception:
                pass
            self._root_bind_id = None

        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:
                pass
            self._popup = None

        self._popup_list = None
        self._search_entry = None
        self._search_var = None
        self._focused_id = None

    def _position_popup(self) -> None:
        if self._popup is None:
            return
        self.update_idletasks()
        try:
            btn_x = self._button.winfo_rootx()
            btn_y = self._button.winfo_rooty()
            btn_w = self._button.winfo_width()
            btn_h = self._button.winfo_height()
        except Exception:
            return

        width = max(btn_w, POPUP_WIDTH_MIN)
        height = POPUP_HEIGHT
        x = btn_x
        y = btn_y + btn_h + 2

        try:
            screen_w = self._popup.winfo_screenwidth()
            if x + width > screen_w:
                x = max(0, screen_w - width)
        except Exception:
            pass

        self._popup.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------------
    # Popup content
    # ------------------------------------------------------------------

    def _resolve_initial_focus(self) -> str | None:
        visible = self._visible_sites()
        if self._selected_id is not None and any(
            s.id == self._selected_id for s in visible
        ):
            return self._selected_id
        return visible[0].id if visible else None

    def _visible_sites(self) -> list[SiteConfig]:
        query = ""
        if self._searchable and self._search_var is not None:
            query = self._search_var.get().strip()
        if not query:
            return list(self._sites)
        normalised = _normalise_search_text(query)
        return [
            s
            for s in self._sites
            if normalised in _normalise_search_text(s.name)
            or normalised in _normalise_search_text(s.id)
        ]

    def _refresh_list(self) -> None:
        if self._popup_list is None:
            return
        for child in self._popup_list.winfo_children():
            child.destroy()

        if self._allow_clear:
            self._build_clear_row()

        visible = self._visible_sites()
        if not visible:
            if not self._allow_clear:
                empty = ctk.CTkLabel(
                    self._popup_list,
                    text="No sites match.",
                    anchor="center",
                    text_color=_MUTED_TEXT_COLOR,
                )
                empty.pack(fill="x", pady=20)
            return

        grouped: dict[str, list[SiteConfig]] = {}
        for site in visible:
            key = _group_key_for(site, self._group_by) if self._group_by else ""
            grouped.setdefault(key, []).append(site)

        for key in sorted(grouped.keys()):
            if key:
                header = ctk.CTkLabel(
                    self._popup_list,
                    text=key,
                    anchor="w",
                    text_color=_MUTED_TEXT_COLOR,
                )
                header.pack(fill="x", padx=4, pady=(8, 2))
            for site in sorted(grouped[key], key=lambda s: s.name.lower()):
                self._build_row(site)

    def _build_clear_row(self) -> None:
        if self._popup_list is None:
            return
        row = ctk.CTkFrame(
            self._popup_list,
            fg_color="transparent",
            height=ROW_HEIGHT,
            corner_radius=4,
        )
        row.pack(fill="x", pady=(4, 1), padx=2)
        row.pack_propagate(False)
        label = ctk.CTkLabel(
            row,
            text="—  Clear selection —",
            anchor="w",
            text_color=_MUTED_TEXT_COLOR,
        )
        label.pack(side="left", fill="x", expand=True, padx=8)
        for widget in (row, label):
            widget.bind("<Button-1>", lambda _e: self._on_clear_click())
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _build_row(self, site: SiteConfig) -> None:
        if self._popup_list is None:
            return

        is_selected = site.id == self._selected_id
        is_focused = site.id == self._focused_id

        if is_selected:
            base_bg = _SELECTED_BG
            text_color = _SELECTED_TEXT_COLOR
        elif is_focused:
            base_bg = _FOCUSED_BG
            text_color = _DEFAULT_TEXT_COLOR
        else:
            base_bg = "transparent"
            text_color = _DEFAULT_TEXT_COLOR

        row = ctk.CTkFrame(
            self._popup_list,
            fg_color=base_bg,
            height=ROW_HEIGHT,
            corner_radius=4,
        )
        row.pack(fill="x", pady=1, padx=2)
        row.pack_propagate(False)

        clickable: list = [row]

        if self._show_health:
            health = self._health.get(site.id, SiteHealth.UNKNOWN)
            dot = ctk.CTkLabel(
                row, text="●", width=16, text_color=_HEALTH_COLORS[health]
            )
            dot.pack(side="left", padx=(6, 2))
            clickable.append(dot)

        lang = ctk.CTkLabel(row, text=_flag_for(site.language), width=24)
        lang.pack(side="left", padx=(2, 4))
        clickable.append(lang)

        name_text = f"{_SELECTED_PREFIX}{site.name}" if is_selected else site.name
        name = ctk.CTkLabel(row, text=name_text, anchor="w", text_color=text_color)
        name.pack(side="left", fill="x", expand=True)
        clickable.append(name)

        for widget in clickable:
            widget.bind("<Button-1>", lambda _e, s=site: self._on_row_click(s))
            widget.bind(
                "<Enter>", lambda _e, r=row, b=base_bg: self._on_row_enter(r, b)
            )
            widget.bind(
                "<Leave>", lambda _e, r=row, b=base_bg: self._on_row_leave(r, b)
            )
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _on_row_enter(self, row, base_bg) -> None:
        if base_bg == _SELECTED_BG:
            return
        try:
            row.configure(fg_color=_HOVER_BG)
        except Exception:
            pass

    def _on_row_leave(self, row, base_bg) -> None:
        try:
            row.configure(fg_color=base_bg)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_row_click(self, site: SiteConfig) -> None:
        self._apply_selection(site.id, notify=True)
        self._close_popup()

    def _on_clear_click(self) -> None:
        self._apply_selection(None, notify=True)
        self._close_popup()

    def _on_search_changed(self, _event=None) -> None:
        visible = self._visible_sites()
        if self._focused_id is not None and not any(
            s.id == self._focused_id for s in visible
        ):
            self._focused_id = visible[0].id if visible else None
        self._refresh_list()

    def _move_focus(self, delta: int) -> None:
        visible = self._visible_sites()
        if not visible:
            return
        ids = [s.id for s in visible]
        try:
            current = ids.index(self._focused_id) if self._focused_id else -1
        except ValueError:
            current = -1
        self._focused_id = ids[(current + delta) % len(ids)]
        self._refresh_list()

    def _on_key_down(self, _event=None) -> str:
        self._move_focus(+1)
        return "break"

    def _on_key_up(self, _event=None) -> str:
        self._move_focus(-1)
        return "break"

    def _on_key_return(self, _event=None) -> str:
        if self._focused_id is not None and self._focused_id in self._by_id:
            self._on_row_click(self._by_id[self._focused_id])
        return "break"

    def _on_escape(self, _event=None) -> str:
        self._close_popup()
        return "break"

    def _on_root_click(self, event) -> None:
        if self._popup is None:
            return
        try:
            px = self._popup.winfo_rootx()
            py = self._popup.winfo_rooty()
            pw = self._popup.winfo_width()
            ph = self._popup.winfo_height()
        except Exception:
            return
        inside = (px <= event.x_root < px + pw) and (py <= event.y_root < py + ph)
        if not inside:
            self._close_popup()

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _apply_selection(self, site_id: str | None, *, notify: bool) -> None:
        if site_id == self._selected_id:
            return
        self._selected_id = site_id
        self._refresh_button()
        if notify and self._on_change is not None:
            try:
                self._on_change(self.get_selected())
            except Exception:
                # A misbehaving parent callback must never break the widget.
                _log.exception("SiteDropdown.on_change callback raised")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        if self._destroyed:
            # A second <Destroy> event for the same widget would re-enter
            # the cleanup below on a partially-torn-down widget tree.
            # Guarding makes the callback idempotent.
            return
        self._destroyed = True
        try:
            self._close_popup()
        except Exception:
            # A destroy handler must never raise — Tk would log a
            # traceback on stderr and continue, which is noisier than
            # a swallowed cleanup error.
            pass
