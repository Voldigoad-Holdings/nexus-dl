"""Manga card widget for the NexusDL GUI.

Presentation-only. Renders a :class:`MangaCardData` projection of a manga
and emits user intents via callbacks. Does **not** load covers, does **not**
fetch chapters, does **not** know about :class:`EventBus`,
:class:`SiteRegistry` or :class:`Settings`.

Universal design notes:
    * The card renders a **projection** (``MangaCardData``), not the domain
      model. When ``Manga`` evolves, only the hosting view changes — use
      :meth:`MangaCardData.from_manga` which is tolerant to field renames.
    * Cover loading is delegated. The view calls :meth:`set_cover_loading`
      before starting its async fetch and :meth:`set_cover` when the image
      is ready (or ``None`` on failure). The card owns no I/O.
    * Every visible section is toggleable (``show_author``, ``show_status``,
      ``show_site``, ``show_chapter_count``, ``show_actions``,
      ``selectable``). Adding a section is a new flag + one render call.
    * Selection state is rendered, not owned: the hosting view keeps the
      source of truth and calls :meth:`set_selected`.
    * All strings come from an injectable :class:`Labels` (i18n-ready).
    * Action buttons are data-driven: only those whose ``on_*`` callback is
      provided are shown.
    * Callbacks are isolated — a misbehaving parent never breaks Tk.
    * Cleans up after itself on ``<Destroy>`` (cancels pending ``after``).

Event binding is idempotent
---------------------------
:meth:`MangaCard._bind_card_events` binds mouse handlers on the card and
every descendant that is not an action button. The method is called from
several places — once from ``__init__``, once per badge-frame rebuild —
and a naive implementation that called ``widget.bind(..., add="+")``
without tracking would attach **duplicate handlers**: a card rebuilt five
times would fire ``_handle_click`` five times on a single user click,
toggling the selection five times (returning it to its original state)
and calling ``on_select`` with contradictory values.

The card therefore tracks every widget it has bound in a
:class:`weakref.WeakSet`. A widget that is destroyed (a rebuilt badge,
a rebuilt checkmark) drops out of the set automatically, so the set
never prevents a legitimate re-bind of a *new* widget at the same
logical position. A widget that has already been bound is skipped on the
next call — which is exactly what makes the "rebuild the badges, then
re-bind the frame" sequence safe.

Weak references (rather than the widget itself in a plain ``set``) are
required because Tk widgets are destroyed explicitly and their
references must not outlive the widget: a plain ``set`` would leak every
widget the card ever created.
"""
from __future__ import annotations

import weakref
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import customtkinter as ctk
from loguru import logger as _logger

from nexusdl.core.models.enums import Language, MangaStatus

if TYPE_CHECKING:
    from PIL import Image

    from nexusdl.core.models.manga import Manga

_log = _logger.bind(component="gui.manga_card")


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------

class CoverState(str, Enum):
    """Lifecycle of the cover image inside the card."""

    IDLE = "idle"          # no image, no fetch in flight
    LOADING = "loading"    # fetch in flight (hosting view responsibility)
    READY = "ready"        # image displayed
    ERROR = "error"        # fetch failed or image invalid


class CardOrientation(str, Enum):
    """Layout direction of the card."""

    HORIZONTAL = "horizontal"  # cover left, info right (list mode)
    VERTICAL = "vertical"      # cover top, info below (grid mode)


@dataclass(slots=True)
class MangaCardData:
    """The displayable projection of a manga.

    This is intentionally decoupled from :class:`~nexusdl.core.models.manga.Manga`
    so that domain-model changes never ripple into the widget. The hosting view
    builds this via :meth:`from_manga` (which tolerates field renames) or
    manually.
    """

    manga_id: str
    title: str
    cover_url: str | None = None
    author: str | None = None
    status: MangaStatus = MangaStatus.UNKNOWN
    language: Language = Language.UNKNOWN
    site_name: str = ""
    chapter_count: int | None = None
    description: str | None = None

    @classmethod
    def from_manga(cls, manga: "Manga", *, site_name: str = "") -> "MangaCardData":
        """Build a card projection from a :class:`Manga` domain instance.

        Reads a documented subset of fields **defensively** so future
        additions to ``Manga`` do not require changes here. Field-name
        variations (``cover_url`` / ``thumbnail_url`` / ``cover``, etc.)
        are handled gracefully.
        """
        cover_url = (
            getattr(manga, "cover_url", None)
            or getattr(manga, "thumbnail_url", None)
            or getattr(manga, "cover", None)
        )
        author = (
            getattr(manga, "author", None)
            or getattr(manga, "artist", None)
        )

        raw_status = getattr(manga, "status", MangaStatus.UNKNOWN)
        status = raw_status if isinstance(raw_status, MangaStatus) else _coerce(
            MangaStatus, raw_status, MangaStatus.UNKNOWN
        )

        raw_lang = getattr(manga, "language", Language.UNKNOWN)
        language = raw_lang if isinstance(raw_lang, Language) else _coerce(
            Language, raw_lang, Language.UNKNOWN
        )

        chapter_count = (
            getattr(manga, "chapter_count", None)
            or getattr(manga, "total_chapters", None)
        )

        return cls(
            manga_id=str(getattr(manga, "id", "")),
            title=str(getattr(manga, "title", "") or "Untitled"),
            cover_url=cover_url,
            author=author,
            status=status,
            language=language,
            site_name=site_name or str(getattr(manga, "site_id", "")),
            chapter_count=chapter_count,
            description=getattr(manga, "description", None),
        )


@dataclass(frozen=True, slots=True)
class Labels:
    """All user-visible strings used by :class:`MangaCard`.

    Inject a translated instance from the hosting view. English defaults
    keep the widget usable standalone.
    """

    unknown_author: str = "Unknown author"
    unknown_site: str = "Unknown site"
    untitled: str = "Untitled"
    chapters_one: str = "{n} chapter"
    chapters_other: str = "{n} chapters"
    chapters_unknown: str = "— chapters"
    download: str = "Download"
    view: str = "View"
    status_ongoing: str = "Ongoing"
    status_completed: str = "Completed"
    status_hiatus: str = "Hiatus"
    status_cancelled: str = "Cancelled"
    status_unknown: str = "Unknown"
    cover_placeholder: str = "📖"
    cover_error: str = "⚠"
    cover_loading: str = "…"
    checkmark: str = "✓"


# ---------------------------------------------------------------------------
# Presentation constants
# ---------------------------------------------------------------------------

_MANGA_STATUS_COLOR: dict[MangaStatus, str] = {
    MangaStatus.ONGOING: "#3B8ED0",
    MangaStatus.COMPLETED: "#2ECC71",
    MangaStatus.HIATUS: "#F1C40F",
    MangaStatus.CANCELLED: "#E74C3C",
    MangaStatus.UNKNOWN: "#95A5A6",
}

_MANGA_STATUS_ICON: dict[MangaStatus, str] = {
    MangaStatus.ONGOING: "▶",
    MangaStatus.COMPLETED: "✓",
    MangaStatus.HIATUS: "‖",
    MangaStatus.CANCELLED: "✗",
    MangaStatus.UNKNOWN: "?",
}

_STATUS_LABEL_KEY: dict[MangaStatus, str] = {
    MangaStatus.ONGOING: "status_ongoing",
    MangaStatus.COMPLETED: "status_completed",
    MangaStatus.HIATUS: "status_hiatus",
    MangaStatus.CANCELLED: "status_cancelled",
    MangaStatus.UNKNOWN: "status_unknown",
}

_LANGUAGE_FLAG: dict[Language, str] = {
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

_BASE_BG = ("gray96", "gray14")
_HOVER_BG = ("gray88", "gray20")
_SELECTED_BG = ("#D6E4F0", "#1A3D5C")
_HOVER_LEAVE_MS = 20
_PRIMARY_TEXT = ("gray10", "gray90")
_MUTED_TEXT = ("gray40", "gray60")
_COVER_BG = ("gray88", "gray22")
_BADGE_TEXT = "#FFFFFF"
_SITE_BADGE_BG = ("#34495E", "#2C3E50")
_LANG_BADGE_BG = ("#16A085", "#13866A")
_DOWNLOAD_BTN_BG = ("#2980B9", "#1F6AA5")
_DOWNLOAD_BTN_HOVER = ("#1F6AA5", "#1A5488")
_VIEW_BTN_BG = ("gray85", "gray25")
_VIEW_BTN_HOVER = ("gray75", "gray30")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce(enum_cls, value, default):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def _flag_for(language: Language) -> str:
    return _LANGUAGE_FLAG.get(language, _LANGUAGE_FLAG[Language.UNKNOWN])


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class MangaCard(ctk.CTkFrame):
    """A compact, hover-aware, selectable manga card.

    Parameters:
        master: Parent widget.
        manga: The projection to display.
        labels: Localized labels. ``None`` uses English defaults.
        on_click: Invoked with the current :class:`MangaCardData` when the
            user clicks the card in non-selectable mode.
        on_right_click: Invoked with ``(data, raw_tk_event)`` on right-click.
        on_double_click: Invoked with the data on double-click.
        on_select: Invoked with ``(data, new_selected)`` on click when
            ``selectable=True``. The card updates its own visual state and
            the hosting view remains the source of truth via
            :meth:`set_selected`.
        on_download: Invoked with the data when *Download* is clicked. If
            ``None``, the button is hidden.
        on_view: Invoked with the data when *View* is clicked. If ``None``,
            the button is hidden.
        selectable: Enables selection highlighting + click-to-toggle.
        selected: Initial selection state.
        orientation: Layout direction.
        cover_size: ``(width, height)`` of the cover box, in pixels.
        show_author: Render the author line.
        show_status: Render the status badge.
        show_site: Render the site + language badges.
        show_chapter_count: Render the chapter count line.
        show_actions: Render the action button row (if any callback is set).
    """

    def __init__(
        self,
        master,
        manga: MangaCardData,
        *,
        labels: Labels | None = None,
        on_click: Callable[[MangaCardData], None] | None = None,
        on_right_click: Callable[[MangaCardData, object], None] | None = None,
        on_double_click: Callable[[MangaCardData], None] | None = None,
        on_select: Callable[[MangaCardData, bool], None] | None = None,
        on_download: Callable[[MangaCardData], None] | None = None,
        on_view: Callable[[MangaCardData], None] | None = None,
        selectable: bool = False,
        selected: bool = False,
        orientation: CardOrientation = CardOrientation.HORIZONTAL,
        cover_size: tuple[int, int] = (72, 96),
        show_author: bool = True,
        show_status: bool = True,
        show_site: bool = True,
        show_chapter_count: bool = True,
        show_actions: bool = True,
        **kwargs,
    ) -> None:
        kwargs.setdefault("corner_radius", 8)
        kwargs.setdefault("fg_color", _BASE_BG)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", ("gray75", "gray28"))
        super().__init__(master, **kwargs)

        self._manga = manga
        self._labels = labels or Labels()

        self._on_click = on_click
        self._on_right_click = on_right_click
        self._on_double_click = on_double_click
        self._on_select = on_select
        self._on_download = on_download
        self._on_view = on_view

        self._selectable = selectable
        self._selected = bool(selected) and selectable
        self._orientation = orientation
        self._cover_size = cover_size
        self._show_author = show_author
        self._show_status = show_status
        self._show_site = show_site
        self._show_chapter_count = show_chapter_count
        self._show_actions = show_actions

        self._cover_state = CoverState.IDLE
        self._cover_image: ctk.CTkImage | None = None
        self._check_overlay: ctk.CTkLabel | None = None
        self._action_buttons: dict[str, ctk.CTkButton] = {}

        self._hover = False
        self._leave_after_id: str | None = None
        self._destroyed = False

        #: Every widget the card has already bound its mouse handlers on.
        #: A ``WeakSet`` (not a plain ``set``) so that a destroyed widget
        #: — a rebuilt badge, a rebuilt checkmark — drops out on its own,
        #: and a new widget at the same logical position can be bound
        #: again. The guard against double-binding is what makes
        #: repeated rebuilds safe — see the module docstring's
        #: "Event binding is idempotent" section.
        self._bound_widgets: weakref.WeakSet = weakref.WeakSet()

        self._build()
        self._render()
        self._bind_card_events(self)
        self._refresh_hover()

        self.bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def manga(self) -> MangaCardData:
        """Return the currently displayed projection."""
        return self._manga

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_manga(self, manga: MangaCardData) -> None:
        """Replace the displayed projection and re-render."""
        if self._destroyed:
            return
        self._manga = manga
        self._render()

    def set_selected(self, selected: bool) -> None:
        """Update the selection state (rendered, not owned)."""
        if self._destroyed:
            return
        new = bool(selected) and self._selectable
        if new == self._selected:
            return
        self._selected = new
        self._render_selection()
        self._refresh_hover()

    def set_cover_loading(self, loading: bool) -> None:
        """Show / hide the loading placeholder.

        Call ``True`` before starting the async fetch, then call
        :meth:`set_cover` when the image is ready (or ``None`` on failure).
        """
        if self._destroyed:
            return
        if loading:
            self._cover_state = CoverState.LOADING
            self._cover_label.configure(
                image=None,
                text=self._labels.cover_loading,
                text_color=_MUTED_TEXT,
            )
        elif self._cover_state == CoverState.LOADING:
            self._cover_state = CoverState.IDLE
            self._cover_label.configure(
                image=None,
                text=self._labels.cover_placeholder,
                text_color=_MUTED_TEXT,
            )

    def set_cover(self, image: "Image.Image | None") -> None:
        """Display a loaded cover image, or an error placeholder if ``None``."""
        if self._destroyed:
            return
        if image is None:
            self._cover_state = CoverState.ERROR
            self._cover_image = None
            self._cover_label.configure(
                image=None,
                text=self._labels.cover_error,
                text_color="#E74C3C",
            )
            return
        try:
            self._cover_image = ctk.CTkImage(
                light_image=image,
                dark_image=image,
                size=self._cover_size,
            )
            self._cover_label.configure(image=self._cover_image, text="")
            self._cover_state = CoverState.READY
        except Exception:
            _log.exception("MangaCard: failed to set cover image")
            self._cover_state = CoverState.ERROR
            self._cover_label.configure(
                image=None,
                text=self._labels.cover_error,
                text_color="#E74C3C",
            )

    def update_labels(self, labels: Labels) -> None:
        """Swap all visible strings (i18n change without re-creating the card)."""
        if self._destroyed:
            return
        self._labels = labels
        if "download" in self._action_buttons:
            self._action_buttons["download"].configure(text=labels.download)
        if "view" in self._action_buttons:
            self._action_buttons["view"].configure(text=labels.view)
        self._render_info()
        self._render_cover_placeholder()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        horizontal = self._orientation == CardOrientation.HORIZONTAL

        if horizontal:
            self.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(0, weight=1)
        else:
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(1, weight=1)

        # Cover.
        self._cover_frame = ctk.CTkFrame(
            self,
            width=self._cover_size[0],
            height=self._cover_size[1],
            corner_radius=6,
            fg_color=_COVER_BG,
        )
        self._cover_frame.pack_propagate(False)
        self._cover_frame.grid_propagate(False)
        self._cover_frame.grid(
            row=0,
            column=0,
            padx=(10, 8) if horizontal else 10,
            pady=(10, 4) if not horizontal else 10,
            sticky="n",
        )

        self._cover_label = ctk.CTkLabel(
            self._cover_frame,
            text=self._labels.cover_placeholder,
            text_color=_MUTED_TEXT,
            font=("", 22),
        )
        self._cover_label.place(relx=0.5, rely=0.5, anchor="center")

        # Info column.
        info_col = 1 if horizontal else 0
        info_row = 0 if horizontal else 1
        self._info = ctk.CTkFrame(self, fg_color="transparent")
        self._info.grid(
            row=info_row,
            column=info_col,
            sticky="nsew",
            padx=(0, 10) if horizontal else 10,
            pady=(10, 8) if horizontal else (0, 10),
        )
        self._info.grid_columnconfigure(0, weight=1)

        self._title_label = ctk.CTkLabel(
            self._info,
            text="",
            anchor="w",
            justify="left",
            text_color=_PRIMARY_TEXT,
            font=("", 14, "bold"),
        )
        self._title_label.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        self._author_label = ctk.CTkLabel(
            self._info,
            text="",
            anchor="w",
            text_color=_MUTED_TEXT,
            font=("", 11),
        )
        self._author_label.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        self._badges_frame = ctk.CTkFrame(self._info, fg_color="transparent")
        self._badges_frame.grid(row=2, column=0, sticky="w", pady=(0, 6))

        self._chapter_label = ctk.CTkLabel(
            self._info,
            text="",
            anchor="w",
            text_color=_MUTED_TEXT,
            font=("", 11),
        )
        self._chapter_label.grid(row=3, column=0, sticky="ew", pady=(0, 6))

        self._actions_frame = ctk.CTkFrame(self._info, fg_color="transparent")
        self._actions_frame.grid(row=4, column=0, sticky="w")
        self._build_actions(self._actions_frame)

    def _build_actions(self, parent) -> None:
        if self._on_download is not None:
            self._action_buttons["download"] = ctk.CTkButton(
                parent,
                text=self._labels.download,
                width=100,
                height=26,
                font=("", 11),
                fg_color=_DOWNLOAD_BTN_BG,
                hover_color=_DOWNLOAD_BTN_HOVER,
                command=self._invoke_action(self._on_download),
            )
            self._action_buttons["download"].pack(side="left", padx=(0, 6))
        if self._on_view is not None:
            self._action_buttons["view"] = ctk.CTkButton(
                parent,
                text=self._labels.view,
                width=80,
                height=26,
                font=("", 11),
                fg_color=_VIEW_BTN_BG,
                hover_color=_VIEW_BTN_HOVER,
                command=self._invoke_action(self._on_view),
            )
            self._action_buttons["view"].pack(side="left")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        if self._destroyed:
            return
        self._render_cover_placeholder()
        self._render_info()
        self._render_actions_visibility()
        self._render_selection()

    def _render_cover_placeholder(self) -> None:
        if self._cover_state == CoverState.READY:
            return
        if self._cover_state == CoverState.LOADING:
            self._cover_label.configure(
                image=None,
                text=self._labels.cover_loading,
                text_color=_MUTED_TEXT,
            )
        elif self._cover_state == CoverState.ERROR:
            self._cover_label.configure(
                image=None,
                text=self._labels.cover_error,
                text_color="#E74C3C",
            )
        else:
            self._cover_label.configure(
                image=None,
                text=self._labels.cover_placeholder,
                text_color=_MUTED_TEXT,
            )

    def _render_info(self) -> None:
        m = self._manga
        max_chars = 32 if self._orientation == CardOrientation.HORIZONTAL else 24

        title = m.title or self._labels.untitled
        self._title_label.configure(text=_truncate(title, max_chars))

        if self._show_author:
            author = m.author or self._labels.unknown_author
            self._author_label.configure(text=_truncate(author, max_chars))
            self._author_label.grid()
        else:
            self._author_label.grid_remove()

        self._render_badges()

        if self._show_chapter_count:
            text = self._format_chapter_count(m.chapter_count)
            if text:
                self._chapter_label.configure(text=text)
                self._chapter_label.grid()
            else:
                self._chapter_label.grid_remove()
        else:
            self._chapter_label.grid_remove()

    def _render_badges(self) -> None:
        for child in self._badges_frame.winfo_children():
            child.destroy()

        rendered = False

        if self._show_status:
            status = self._manga.status
            color = _MANGA_STATUS_COLOR.get(
                status, _MANGA_STATUS_COLOR[MangaStatus.UNKNOWN]
            )
            icon = _MANGA_STATUS_ICON.get(status, "?")
            key = _STATUS_LABEL_KEY.get(status, "status_unknown")
            text = getattr(self._labels, key, "")
            badge = self._make_badge(self._badges_frame, f"{icon} {text}", color)
            badge.pack(side="left", padx=(0, 6))
            rendered = True

        if self._show_site and self._manga.site_name:
            badge = self._make_badge(
                self._badges_frame, self._manga.site_name, _SITE_BADGE_BG
            )
            badge.pack(side="left", padx=(0, 6))
            rendered = True

        if self._show_site:
            badge = self._make_badge(
                self._badges_frame,
                _flag_for(self._manga.language),
                _LANG_BADGE_BG,
            )
            badge.pack(side="left")
            rendered = True

        if rendered:
            self._badges_frame.grid()
        else:
            self._badges_frame.grid_remove()

        # Bind mouse handlers on the *new* badges only. The
        # ``_badges_frame`` itself has been bound in ``__init__`` and
        # remains bound — binding it again on every rebuild would
        # accumulate handlers (see the module docstring's "Event binding
        # is idempotent" section). The ``WeakSet`` guard in
        # ``_bind_card_events`` makes the call safe either way.
        for badge in self._badges_frame.winfo_children():
            self._bind_card_events(badge)

    def _make_badge(self, parent, text: str, bg_color) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            parent,
            text=text,
            fg_color=bg_color,
            text_color=_BADGE_TEXT,
            corner_radius=4,
            height=18,
            padx=6,
            pady=0,
            font=("", 10),
        )

    def _render_actions_visibility(self) -> None:
        if self._show_actions and self._action_buttons:
            self._actions_frame.grid()
        else:
            self._actions_frame.grid_remove()

    def _render_selection(self) -> None:
        if not self._selectable:
            try:
                self.configure(border_width=0)
            except Exception:
                pass
            self._remove_check_overlay()
            return

        if self._selected:
            try:
                self.configure(border_width=2, border_color="#3B8ED0")
            except Exception:
                pass
            self._show_check_overlay()
        else:
            try:
                self.configure(border_width=1, border_color=("gray75", "gray28"))
            except Exception:
                pass
            self._remove_check_overlay()

    def _show_check_overlay(self) -> None:
        if self._check_overlay is not None:
            return
        self._check_overlay = ctk.CTkLabel(
            self._cover_frame,
            text=self._labels.checkmark,
            width=20,
            height=20,
            corner_radius=10,
            fg_color="#3B8ED0",
            text_color="#FFFFFF",
            font=("", 12, "bold"),
        )
        self._check_overlay.place(relx=1.0, rely=0.0, x=-4, y=4, anchor="ne")
        # The overlay is a fresh widget — bind mouse handlers on it so a
        # click on the checkmark still activates the card.
        self._bind_card_events(self._check_overlay)

    def _remove_check_overlay(self) -> None:
        if self._check_overlay is None:
            return
        try:
            self._check_overlay.destroy()
        except Exception:
            pass
        self._check_overlay = None

    def _format_chapter_count(self, n: int | None) -> str:
        if n is None or n < 0:
            return self._labels.chapters_unknown
        if n == 0:
            return ""
        if n == 1:
            return self._labels.chapters_one.format(n=n)
        return self._labels.chapters_other.format(n=n)

    # ------------------------------------------------------------------
    # Hover
    # ------------------------------------------------------------------

    def _refresh_hover(self) -> None:
        if self._destroyed:
            return
        if self._selectable and self._selected:
            bg = _SELECTED_BG
        elif self._hover:
            bg = _HOVER_BG
        else:
            bg = _BASE_BG
        try:
            self.configure(fg_color=bg)
        except Exception:
            pass

    def _handle_enter(self, _event=None) -> None:
        if self._leave_after_id is not None:
            try:
                self.after_cancel(self._leave_after_id)
            except Exception:
                pass
            self._leave_after_id = None
        if not self._hover:
            self._hover = True
            self._refresh_hover()

    def _handle_leave(self, _event=None) -> None:
        if self._leave_after_id is not None:
            return
        self._leave_after_id = self.after(_HOVER_LEAVE_MS, self._do_leave)

    def _do_leave(self) -> None:
        self._leave_after_id = None
        if self._destroyed:
            return
        if self._hover:
            self._hover = False
            self._refresh_hover()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_click(self, _event=None) -> None:
        if self._destroyed:
            return
        if self._selectable:
            new_state = not self._selected
            self._selected = new_state
            self._render_selection()
            self._refresh_hover()
            if self._on_select is not None:
                self._safe_call(self._on_select, self._manga, new_state)
            return
        if self._on_click is not None:
            self._safe_call(self._on_click, self._manga)

    def _handle_right_click(self, event=None) -> None:
        if self._destroyed or self._on_right_click is None:
            return
        self._safe_call(self._on_right_click, self._manga, event)

    def _handle_double_click(self, _event=None) -> None:
        if self._destroyed or self._on_double_click is None:
            return
        self._safe_call(self._on_double_click, self._manga)

    def _invoke_action(self, cb: Callable[[MangaCardData], None]):
        def _wrapped() -> None:
            self._safe_call(cb, self._manga)
        return _wrapped

    @staticmethod
    def _safe_call(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:
            _log.exception(
                "MangaCard: callback {!r} raised",
                getattr(cb, "__name__", cb),
            )

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def _bind_card_events(self, widget) -> None:
        """Recursively bind card-level events to ``widget`` and its children.

        Action buttons (and their descendants) are skipped so they do not
        trigger card clicks.

        **Idempotent.** A widget that has already been bound by this card
        is skipped on subsequent calls — see the module docstring's
        "Event binding is idempotent" section. Without the guard, a card
        whose badges are rebuilt (via :meth:`set_manga`) would attach a
        new handler on every rebuild, and a single user click on the
        badge frame would fire ``_handle_click`` as many times as the
        card had been rebuilt.
        """
        if self._destroyed:
            return
        if widget in self._bound_widgets:
            return
        self._bound_widgets.add(widget)

        action_widgets = set(self._action_buttons.values())

        def _is_action(w) -> bool:
            cur = w
            while cur is not None:
                if cur in action_widgets:
                    return True
                cur = getattr(cur, "master", None)
            return False

        if _is_action(widget):
            return

        for sequence, handler in (
            ("<Button-1>", self._handle_click),
            ("<Button-3>", self._handle_right_click),
            ("<Double-Button-1>", self._handle_double_click),
            ("<Enter>", self._handle_enter),
            ("<Leave>", self._handle_leave),
        ):
            try:
                widget.bind(sequence, handler, add="+")
            except Exception:
                pass
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass

        for child in widget.winfo_children():
            self._bind_card_events(child)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _on_destroy(self, event=None) -> None:
        if event is not None and getattr(event, "widget", None) is not self:
            return
        if self._destroyed:
            return
        self._destroyed = True
        if self._leave_after_id is not None:
            try:
                self.after_cancel(self._leave_after_id)
            except Exception:
                pass
            self._leave_after_id = None
