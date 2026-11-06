"""Theme system for the NexusDL GUI.

Centralizes every visual token — colors, spacing, radii, typography, motion
timings, and layout sizes — behind a single, swappable, extensible
:class:`Theme`.

Design constraints:

    * **No** :mod:`customtkinter` import at module load. This module is safe
      to import in headless contexts (CLI, CI, backend, tests). CTk is only
      imported lazily inside :func:`get_mode` / :func:`apply_appearance_mode`.
    * **Backward compatible.** :func:`theme_provider` returns a
      ``Callable[[], dict[str, str]]`` that resolves *every* key used by
      existing components (``bg``, ``fg``, ``panel_bg``, ``nav_bg``,
      ``section_nav_bg``, ``chip_active_bg``, ``batch_bg``,
      ``heading_active_bg``, …). Adopting it is a one-line change in a
      host file — no component needs to be reopened.
    * **Extensible.** :func:`register_theme` lets plugins ship new themes;
      :func:`set_theme` switches at runtime; adding a new color token is a
      single entry in both ``light`` and ``dark`` maps.
    * **Semantic + legacy names coexist.** New code should prefer
      ``background`` / ``surface`` / ``text_primary`` / ``accent`` /
      ``danger`` / etc. Legacy aliases (``bg``, ``panel_bg``, ``fg``,
      ``error_fg`` …) are derived automatically so both stay in sync.
    * **Non-color tokens** (:class:`Spacing`, :class:`Radii`,
      :class:`Typography`, :class:`Motion`, :class:`Sizes`) expose the
      magic numbers that used to be scattered across components.
    * **Snapshot / restore.** :meth:`Theme.to_dict` /
      :meth:`Theme.from_dict` round-trip cleanly — useful for plugins,
      settings persistence, and debugging.

Typical host usage::

    from nexusdl.interfaces.gui.theme import (
        get_theme, get_mode, set_theme, theme_provider,
        apply_appearance_mode,
    )

    # Apply the appearance mode (light/dark) at startup:
    apply_appearance_mode("dark")

    # Select a theme (default is "nexus"):
    set_theme("nexus")

    # New components can accept a ``theme_provider`` param directly:
    ProgressWidget(..., theme_provider=theme_provider())

    # Legacy components can be migrated in place without reopening them:
    legacy_component._theme = theme_provider()

    # Access tokens directly:
    theme = get_theme()
    pad = theme.spacing.md
    font = theme.typography.font("lg", weight="bold")
    duration = theme.motion.toast_duration_ms
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from loguru import logger as _logger

# NB: customtkinter is intentionally NOT imported here. See module docstring.

__all__ = [
    # Types
    "ThemeMode",
    "Theme",
    "Colors",
    "Spacing",
    "Radii",
    "FontSizes",
    "Typography",
    "Motion",
    "Sizes",
    # Registry / accessors
    "get_theme",
    "set_theme",
    "register_theme",
    "list_themes",
    "get_mode",
    "get_color",
    "get_color_pair",
    "get_color_map",
    "theme_provider",
    "apply_appearance_mode",
    # Built-in themes
    "NEXUS_DEFAULT",
    "NEXUS_HIGH_CONTRAST",
]


_log = _logger.bind(component="gui.theme")

ThemeMode = Literal["light", "dark"]


# ===========================================================================
# Non-color token dataclasses
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Spacing:
    """Consistent spacing scale (pixels)."""

    xs: int = 2
    sm: int = 4
    md: int = 8
    lg: int = 12
    xl: int = 16
    xxl: int = 24
    xxxl: int = 32


@dataclass(frozen=True, slots=True)
class Radii:
    """Corner-radius scale (pixels)."""

    none: int = 0
    sm: int = 4
    md: int = 6
    lg: int = 8
    xl: int = 12
    pill: int = 999


@dataclass(frozen=True, slots=True)
class FontSizes:
    """Font-size scale (pixels)."""

    xs: int = 10
    sm: int = 11
    md: int = 12
    lg: int = 13
    xl: int = 14
    xxl: int = 16
    display: int = 22


@dataclass(frozen=True, slots=True)
class Typography:
    """Font families and size scale.

    Use :meth:`font` / :meth:`mono` to get ready-to-pass Tk font tuples.
    Empty family strings (``""``) fall back to the Tk default font, which
    is the safest, most portable choice across Linux/macOS/Windows. Hosts
    that want a branded typeface can set ``family_sans`` / ``family_mono``
    to any installed family.
    """

    family_sans: str = ""
    family_mono: str = ""
    sizes: FontSizes = field(default_factory=FontSizes)

    def font(
        self, size: str = "md", weight: str = "normal"
    ) -> tuple[str, int, str]:
        """Return a Tk font tuple ``(family, size, weight)``."""
        return (self.family_sans, getattr(self.sizes, size), weight)

    def mono(
        self, size: str = "md", weight: str = "normal"
    ) -> tuple[str, int, str]:
        """Return a monospace Tk font tuple."""
        return (self.family_mono, getattr(self.sizes, size), weight)


@dataclass(frozen=True, slots=True)
class Motion:
    """Animation and debounce timings (milliseconds)."""

    fast_ms: int = 120
    normal_ms: int = 200
    slow_ms: int = 320
    theme_poll_ms: int = 1000
    search_debounce_ms: int = 300
    reflow_debounce_ms: int = 100
    selection_debounce_ms: int = 20
    progress_throttle_ms: int = 100
    toast_duration_ms: int = 2600
    status_duration_ms: int = 5000
    flash_duration_ms: int = 900
    hover_leave_ms: int = 20


@dataclass(frozen=True, slots=True)
class Sizes:
    """Layout dimensions (pixels)."""

    nav_width: int = 200
    nav_width_collapsed: int = 52
    button_height: int = 36
    input_height: int = 32
    row_height: int = 26
    card_cover_width: int = 150
    card_cover_height: int = 210
    card_width: int = 180
    popup_width_min: int = 320
    popup_height: int = 360
    toast_max_width: int = 400
    icon_sm: int = 16
    icon_md: int = 20
    icon_lg: int = 24
    border_width: int = 1
    border_width_strong: int = 2
    scrollbar_width: int = 14


# ===========================================================================
# Colors
# ===========================================================================

class Colors:
    """A pair of flat color maps (one per mode) plus alias derivation.

    Kept as a plain class (not a dataclass) because the alias expansion
    runs on construction and cached results are consulted on every token
    lookup. Construction cost is paid once per process — negligible.
    """

    __slots__ = ("_light", "_dark")

    def __init__(
        self,
        light: Mapping[str, str],
        dark: Mapping[str, str],
    ) -> None:
        self._light = MappingProxyType(_expand_aliases(dict(light)))
        self._dark = MappingProxyType(_expand_aliases(dict(dark)))

    @property
    def light(self) -> Mapping[str, str]:
        return self._light

    @property
    def dark(self) -> Mapping[str, str]:
        return self._dark

    def resolve(self, mode: ThemeMode = "dark") -> dict[str, str]:
        """Return a flat ``dict`` of all tokens for ``mode``."""
        return dict(self._dark if mode == "dark" else self._light)

    def get(
        self, key: str, mode: ThemeMode = "dark", default: str | None = None
    ) -> str | None:
        """Single-token lookup. Returns ``default`` if ``key`` is unknown."""
        source = self._dark if mode == "dark" else self._light
        return source.get(key, default)

    def pair(self, key: str) -> tuple[str, str]:
        """Return ``(light, dark)`` for CTk widgets that want auto-theme pairs.

        Raises:
            KeyError: If ``key`` is missing from either mode. This indicates
                a malformed theme (tokens must be symmetric across modes).
        """
        try:
            return (self._light[key], self._dark[key])
        except KeyError as exc:
            missing_mode = "light" if key not in self._light else "dark"
            raise KeyError(
                f"Color token {key!r} missing from {missing_mode} mode; "
                "themes must be symmetric across modes"
            ) from exc

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {"light": dict(self._light), "dark": dict(self._dark)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Mapping[str, str]]) -> Colors:
        return cls(light=data["light"], dark=data["dark"])


# ---------------------------------------------------------------------------
# Alias derivation (canonical -> legacy)
# ---------------------------------------------------------------------------
#
# Existing components use historically-grown key names. Rather than force
# a rewrite, we derive legacy keys from canonical ones so both coexist.
# A migration is complete when a component stops using the alias; the alias
# can stay forever, it costs one dict entry.

_ALIASES: dict[str, str] = {
    # Backgrounds
    "bg": "background",
    "panel_bg": "surface",
    "alt_bg": "surface_alt",
    "nav_bg": "surface_alt",
    "section_nav_bg": "surface_alt",
    "nav_item_bg": "surface_alt",
    "base_bg": "surface",
    "cover_bg": "surface_alt",
    "bar_bg": "surface_alt",
    "heading_bg": "surface_alt",
    "status_bg": "surface_alt",
    "batch_bg": "info_soft",
    # Foregrounds
    "fg": "text_primary",
    "muted_fg": "text_muted",
    "hint_fg": "text_hint",
    "nav_item_fg": "text_primary",
    "heading_fg": "text_primary",
    "status_info": "text_primary",
    "batch_fg": "text_primary",
    "chip_fg": "text_primary",
    # Accent / interaction
    "nav_item_hover": "surface_hover",
    "nav_item_active": "accent",
    "nav_item_active_fg": "accent_fg",
    "heading_active_bg": "surface_hover",
    "hover_bg": "surface_hover",
    "chip_active_bg": "accent",
    "chip_active_fg": "accent_fg",
    "selected_bg": "accent",
    "selected_fg": "accent_fg",
    "selection_bg": "accent",
    "selection_fg": "accent_fg",
    "primary_bg": "accent",
    "primary_hover": "accent_hover",
    "download_btn_bg": "accent",
    "download_btn_hover": "accent_hover",
    # Danger
    "error_fg": "danger",
    "status_error": "danger",
    "danger_bg": "danger",
    # Warning / feedback
    "status_warning": "warning",
    "banner_bg": "warning_soft",
    "banner_fg": "warning",
    "flash_bg": "warning_soft",
    # Success
    "success_bg": "success_soft",
    # Misc
    "badge_text": "text_inverted",
    "site_badge_bg": "accent",
    "site_badge_fg": "text_inverted",
    "lang_badge_bg": "success",
    "lang_badge_fg": "text_inverted",
    "view_btn_bg": "surface_alt",
    "view_btn_hover": "surface_hover",
    "batch_border": "border_strong",
    "chip_bg": "surface_alt",
    "chip_hover": "surface_hover",
    # Domain: progress status
    "progress_pending": "text_muted",
    "progress_running": "accent",
    "progress_paused": "warning",
    "progress_done": "success",
    "progress_failed": "danger",
    "progress_cancelled": "text_muted",
    # Domain: manga status
    "manga_ongoing": "accent",
    "manga_completed": "success",
    "manga_hiatus": "warning",
    "manga_cancelled": "danger",
    "manga_unknown": "text_muted",
    # Domain: site health
    "health_healthy": "success",
    "health_degraded": "warning",
    "health_down": "danger",
    "health_unknown": "text_muted",
    # Toast (feedback kind = success / error reuse already-defined pairs)
    "toast_success_bg": "success_soft",
    "toast_success_fg": "success",
    "toast_error_bg": "warning_soft",
    "toast_error_fg": "danger",
}


def _expand_aliases(palette: dict[str, str]) -> dict[str, str]:
    """Add every legacy alias to ``palette`` in place, then return it."""
    for alias, canonical in _ALIASES.items():
        if alias in palette:
            continue  # an explicit override wins over the alias
        if canonical in palette:
            palette[alias] = palette[canonical]
    return palette


# ===========================================================================
# Theme
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Theme:
    """A complete visual theme."""

    id: str
    name: str
    colors: Colors
    spacing: Spacing = field(default_factory=Spacing)
    radii: Radii = field(default_factory=Radii)
    typography: Typography = field(default_factory=Typography)
    motion: Motion = field(default_factory=Motion)
    sizes: Sizes = field(default_factory=Sizes)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict (for plugins / settings)."""
        return {
            "id": self.id,
            "name": self.name,
            "colors": self.colors.to_dict(),
            "spacing": asdict(self.spacing),
            "radii": asdict(self.radii),
            "typography": {
                "family_sans": self.typography.family_sans,
                "family_mono": self.typography.family_mono,
                "sizes": asdict(self.typography.sizes),
            },
            "motion": asdict(self.motion),
            "sizes": asdict(self.sizes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Theme:
        typo = data.get("typography", {})
        return cls(
            id=data["id"],
            name=data["name"],
            colors=Colors.from_dict(data["colors"]),
            spacing=Spacing(**data.get("spacing", {})),
            radii=Radii(**data.get("radii", {})),
            typography=Typography(
                family_sans=typo.get("family_sans", ""),
                family_mono=typo.get("family_mono", ""),
                sizes=FontSizes(**typo.get("sizes", {})),
            ),
            motion=Motion(**data.get("motion", {})),
            sizes=Sizes(**data.get("sizes", {})),
        )


# ===========================================================================
# Built-in palettes
# ===========================================================================

_NEXUS_LIGHT: dict[str, str] = {
    # Canonical surface / text
    "background": "#F5F5F5",
    "surface": "#FFFFFF",
    "surface_alt": "#ECECEC",
    "surface_hover": "#DDDDDD",
    "surface_selected": "#D6E4F0",
    "text_primary": "#1A1A1A",
    "text_muted": "#666666",
    "text_hint": "#999999",
    "text_inverted": "#FFFFFF",
    # Borders / separators
    "border": "#CCCCCC",
    "border_strong": "#B8C8D8",
    "separator": "#CCCCCC",
    # Accent
    "accent": "#3B8ED0",
    "accent_hover": "#2E7AB8",
    "accent_fg": "#FFFFFF",
    # Feedback
    "danger": "#C0392B",
    "danger_hover": "#A93226",
    "warning": "#B7791F",
    "warning_soft": "#FFF3B0",
    "success": "#2ECC71",
    "success_soft": "#D4F0DC",
    "info": "#3B8ED0",
    "info_soft": "#E8F0F9",
    # Overlay
    "overlay": "#00000040",
    # Semantic extras kept here for full coverage
    "alt_row": "#F7F7F7",
}

_NEXUS_DARK: dict[str, str] = {
    "background": "#1A1A1A",
    "surface": "#242424",
    "surface_alt": "#1F1F1F",
    "surface_hover": "#2E2E2E",
    "surface_selected": "#1A3D5C",
    "text_primary": "#DCDCDC",
    "text_muted": "#909090",
    "text_hint": "#666666",
    "text_inverted": "#FFFFFF",
    "border": "#333333",
    "border_strong": "#254A73",
    "separator": "#333333",
    "accent": "#1F6AA5",
    "accent_hover": "#1A5488",
    "accent_fg": "#FFFFFF",
    "danger": "#922B21",
    "danger_hover": "#7B241C",
    "warning": "#FFD966",
    "warning_soft": "#4A3A00",
    "success": "#2ECC71",
    "success_soft": "#1E4A32",
    "info": "#1F6AA5",
    "info_soft": "#1E3A5A",
    "overlay": "#00000080",
    "alt_row": "#202020",
}

_HC_LIGHT: dict[str, str] = {
    "background": "#FFFFFF",
    "surface": "#FFFFFF",
    "surface_alt": "#F0F0F0",
    "surface_hover": "#E0E0E0",
    "surface_selected": "#CCE0F0",
    "text_primary": "#000000",
    "text_muted": "#333333",
    "text_hint": "#444444",
    "text_inverted": "#FFFFFF",
    "border": "#000000",
    "border_strong": "#000000",
    "separator": "#000000",
    "accent": "#0056B3",
    "accent_hover": "#003D80",
    "accent_fg": "#FFFFFF",
    "danger": "#A00000",
    "danger_hover": "#700000",
    "warning": "#8A5A00",
    "warning_soft": "#FFF0B0",
    "success": "#006600",
    "success_soft": "#CCE5CC",
    "info": "#0056B3",
    "info_soft": "#CCE0F0",
    "overlay": "#00000060",
    "alt_row": "#F0F0F0",
}

_HC_DARK: dict[str, str] = {
    "background": "#000000",
    "surface": "#0A0A0A",
    "surface_alt": "#101010",
    "surface_hover": "#1F1F1F",
    "surface_selected": "#1F3A5F",
    "text_primary": "#FFFFFF",
    "text_muted": "#DCDCDC",
    "text_hint": "#C0C0C0",
    "text_inverted": "#000000",
    "border": "#FFFFFF",
    "border_strong": "#FFFFFF",
    "separator": "#FFFFFF",
    "accent": "#4FA3E8",
    "accent_hover": "#7AB8F0",
    "accent_fg": "#000000",
    "danger": "#FF6B5B",
    "danger_hover": "#FF9080",
    "warning": "#FFD966",
    "warning_soft": "#3A2E00",
    "success": "#5BE38C",
    "success_soft": "#0E3A1F",
    "info": "#4FA3E8",
    "info_soft": "#0E2A45",
    "overlay": "#000000A0",
    "alt_row": "#101010",
}


# ===========================================================================
# Built-in themes
# ===========================================================================

NEXUS_DEFAULT = Theme(
    id="nexus",
    name="Nexus Default",
    colors=Colors(_NEXUS_LIGHT, _NEXUS_DARK),
)

NEXUS_HIGH_CONTRAST = Theme(
    id="nexus-hc",
    name="Nexus High Contrast",
    colors=Colors(_HC_LIGHT, _HC_DARK),
    sizes=Sizes(border_width=2, border_width_strong=3),
)


# ===========================================================================
# Registry
# ===========================================================================

_THEMES: dict[str, Theme] = {
    NEXUS_DEFAULT.id: NEXUS_DEFAULT,
    NEXUS_HIGH_CONTRAST.id: NEXUS_HIGH_CONTRAST,
}

_active_theme_id: str = NEXUS_DEFAULT.id


def get_theme() -> Theme:
    """Return the currently active :class:`Theme`."""
    return _THEMES[_active_theme_id]


def set_theme(theme_id: str) -> None:
    """Switch the active theme.

    Raises:
        KeyError: If ``theme_id`` is not registered.
    """
    if theme_id not in _THEMES:
        available = ", ".join(sorted(_THEMES))
        raise KeyError(
            f"Unknown theme {theme_id!r}. Available themes: {available}"
        )
    global _active_theme_id
    _active_theme_id = theme_id


def register_theme(theme: Theme) -> None:
    """Register a new theme (plugin or user-provided).

    If the ``id`` is already registered, the new theme **replaces** it —
    useful for user overrides applied at startup.
    """
    _THEMES[theme.id] = theme


def list_themes() -> list[str]:
    """Return the sorted list of registered theme ids."""
    return sorted(_THEMES.keys())


# ===========================================================================
# Mode detection / appearance
# ===========================================================================

def get_mode() -> ThemeMode:
    """Return ``"light"`` or ``"dark"``.

    Uses :func:`customtkinter.get_appearance_mode` when available,
    otherwise falls back to ``"dark"``. Never raises.
    """
    try:
        import customtkinter as ctk  # noqa: PLC0415 — lazy on purpose
    except Exception:
        # CTk unavailable (headless): fallback is expected, debug only.
        _log.debug("customtkinter unavailable; defaulting to dark mode")
        return "dark"
    try:
        ctk_mode = str(ctk.get_appearance_mode()).lower()
    except Exception:
        _log.debug("ctk.get_appearance_mode() raised; defaulting to dark mode")
        return "dark"
    return "light" if ctk_mode == "light" else "dark"


def apply_appearance_mode(mode: ThemeMode) -> None:
    """Set the CTk appearance mode (``"light"`` / ``"dark"``).

    No-op when :mod:`customtkinter` is unavailable — safe to call in
    headless code paths (CLI, tests).
    """
    try:
        import customtkinter as ctk  # noqa: PLC0415
    except Exception:
        _log.debug("customtkinter unavailable; appearance mode not applied")
        return
    try:
        ctk.set_appearance_mode("Light" if mode == "light" else "Dark")
    except Exception:
        _log.exception("ctk.set_appearance_mode({!r}) raised", mode)


# ===========================================================================
# Color accessors
# ===========================================================================

def get_color(
    name: str,
    *,
    mode: ThemeMode | None = None,
    default: str | None = None,
) -> str | None:
    """Return the resolved color for token ``name``.

    Both canonical (``surface``) and legacy (``panel_bg``) names are
    supported. Returns ``default`` (``None`` by default) when the token
    does not exist — this matches ``dict.get`` semantics and lets callers
    decide whether a missing token is an error.
    """
    m = mode or get_mode()
    return get_theme().colors.get(name, mode=m, default=default)


def get_color_pair(name: str) -> tuple[str, str]:
    """Return ``(light_value, dark_value)`` for CTk auto-theme widgets.

    Example::

        ctk.CTkFrame(fg_color=get_color_pair("surface"))

    Raises:
        KeyError: If ``name`` is missing from either mode. Themes must be
            symmetric across modes.
    """
    return get_theme().colors.pair(name)


def get_color_map(*, mode: ThemeMode | None = None) -> dict[str, str]:
    """Return a flat ``dict`` of all resolved color tokens for ``mode``.

    This is what components historically expect from their internal
    ``_THEME_LIGHT`` / ``_THEME_DARK`` dicts. Both canonical and legacy
    keys are present.
    """
    m = mode or get_mode()
    return get_theme().colors.resolve(m)


def theme_provider() -> Callable[[], dict[str, str]]:
    """Return a zero-arg callable producing the current color map.

    Drop-in replacement for the ``theme_provider`` pattern used by
    components: the returned callable polls :func:`get_mode` on each call,
    so it follows runtime theme switches automatically.

    Migration recipe for an existing component::

        # Before:
        from .local_theme import LIGHT, DARK
        def _theme(self): return LIGHT if ... else DARK

        # After (one line):
        from ..theme import theme_provider
        self._theme = theme_provider()

    No other line needs to change.
    """
    return get_color_map
