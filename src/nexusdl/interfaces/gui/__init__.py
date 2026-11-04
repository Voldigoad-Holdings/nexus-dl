"""GUI package for NexusDL.

This package is the entry point for all desktop-GUI code. It exposes:

* the **composition root** (:class:`NexusDLApp`, :class:`AppServices`,
  :class:`AppHooks`, :class:`GUILabels`) that wires the shell to the
  application services,
* the **theme system** (:class:`Theme`, :func:`get_theme`,
  :func:`theme_provider`, …) — headless-safe, always eager,
* the **views** (``SearchView``, ``LibraryView``, ``DownloadView``,
  ``SettingsView``, ``MainView``),
* the **components** (``MangaCard``, ``ProgressWidget``, ``SiteDropdown``,
  ``ChapterTable``).

Loading strategy
================

Because this package is imported early in many code paths (CLI, TUI, Web
backend, tests), it uses a **hybrid** loading strategy:

* :mod:`.theme` is imported **eagerly**. It is guaranteed headless-safe
  (its module docstring forbids ``customtkinter`` imports at load time),
  so ``from nexusdl.interfaces.gui import get_theme`` works instantly in
  any context. Do **not** add toolkit imports to ``theme.py``.

* **Everything else is lazy** via :pep:`562` (``__getattr__``). Importing
  this package never triggers ``customtkinter``, ``PIL``, ``ttk``, or
  Tk initialization. Widgets load only when a symbol is first accessed.

If a lazy symbol cannot be imported because ``customtkinter`` (or ``PIL``,
``tkinter``) is missing, a clear :class:`ImportError` points at the
``[gui]`` extra instead of the raw ``ModuleNotFoundError``.

Sub-package shortcuts
=====================

``gui.views`` and ``gui.components`` retain their own lazy loaders. When
you access, for instance, ``gui.SearchView``, this package forwards to
``gui.views.SearchView`` transparently (two-level lazy chain), caching the
resolved class in ``globals()`` on success.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# --- Package version (soft read from nexusdl.version) -----------------------

try:
    from nexusdl.version import __version__ as __version__
except Exception:  # pragma: no cover - defensive fallback
    __version__ = "3.0.0"


# ===========================================================================
# EAGER IMPORTS — headless-safe
# ===========================================================================
#
# The theme subsystem is deliberately imported here, not lazily. Its
# module docstring forbids ``customtkinter`` imports at load time, so it
# is safe in CLI / TUI / Web / CI contexts. Keep it eager so theme
# helpers are available without any hidden lazy machinery.

from .theme import (  # noqa: E402
    NEXUS_DEFAULT,
    NEXUS_HIGH_CONTRAST,
    Colors,
    FontSizes,
    Motion,
    Radii,
    Sizes,
    Spacing,
    Theme,
    ThemeMode,
    Typography,
    apply_appearance_mode,
    get_color,
    get_color_map,
    get_color_pair,
    get_mode,
    get_theme,
    list_themes,
    register_theme,
    set_theme,
    theme_provider,
)


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- App / composition root (lazy) ---
    "NexusDLApp",
    "GUILabels",
    "AppHooks",
    "AppServices",
    "AsyncBridge",
    "SettingsLike",
    "EventBusLike",
    "SiteRegistryLike",
    "DownloadManagerLike",
    "LibraryLike",
    "ImageCacheLike",
    "StateStoreLike",
    "VIEW_SEARCH",
    "VIEW_LIBRARY",
    "VIEW_DOWNLOADS",
    "VIEW_SETTINGS",
    # --- View classes (lazy) ---
    "MainView",
    "SearchView",
    "LibraryView",
    "DownloadView",
    "SettingsView",
    "ViewSpec",
    # --- Component classes (lazy) ---
    "MangaCard",
    "MangaCardData",
    "ProgressWidget",
    "ProgressState",
    "SiteDropdown",
    "ChapterTable",
    "ChapterRowData",
    # --- Theme (eager) ---
    "Theme",
    "ThemeMode",
    "Colors",
    "Spacing",
    "Radii",
    "FontSizes",
    "Typography",
    "Motion",
    "Sizes",
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
    "NEXUS_DEFAULT",
    "NEXUS_HIGH_CONTRAST",
    # --- Introspection ---
    "list_views",
    "list_components",
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (subpackage_basename, attribute_in_that_subpackage).
# ``subpackage_basename`` is resolved against this package, so ``"app"``
# means ``nexusdl.interfaces.gui.app``, ``"views"`` means
# ``nexusdl.interfaces.gui.views``, etc. Adding a new symbol at the top
# level is a one-line change here + a matching TYPE_CHECKING re-export.

_SYMBOLS: dict[str, tuple[str, str]] = {
    # --- App / composition root ---
    "NexusDLApp": ("app", "NexusDLApp"),
    "GUILabels": ("app", "GUILabels"),
    "AppHooks": ("app", "AppHooks"),
    "AppServices": ("app", "AppServices"),
    "AsyncBridge": ("app", "AsyncBridge"),
    "SettingsLike": ("app", "SettingsLike"),
    "EventBusLike": ("app", "EventBusLike"),
    "SiteRegistryLike": ("app", "SiteRegistryLike"),
    "DownloadManagerLike": ("app", "DownloadManagerLike"),
    "LibraryLike": ("app", "LibraryLike"),
    "ImageCacheLike": ("app", "ImageCacheLike"),
    "StateStoreLike": ("app", "StateStoreLike"),
    "VIEW_SEARCH": ("app", "VIEW_SEARCH"),
    "VIEW_LIBRARY": ("app", "VIEW_LIBRARY"),
    "VIEW_DOWNLOADS": ("app", "VIEW_DOWNLOADS"),
    "VIEW_SETTINGS": ("app", "VIEW_SETTINGS"),
    # --- Views (routed through gui.views lazy loader) ---
    "MainView": ("views", "MainView"),
    "SearchView": ("views", "SearchView"),
    "LibraryView": ("views", "LibraryView"),
    "DownloadView": ("views", "DownloadView"),
    "SettingsView": ("views", "SettingsView"),
    "ViewSpec": ("views", "ViewSpec"),
    # --- Components (routed through gui.components lazy loader) ---
    "MangaCard": ("components", "MangaCard"),
    "MangaCardData": ("components", "MangaCardData"),
    "ProgressWidget": ("components", "ProgressWidget"),
    "ProgressState": ("components", "ProgressState"),
    "SiteDropdown": ("components", "SiteDropdown"),
    "ChapterTable": ("components", "ChapterTable"),
    "ChapterRowData": ("components", "ChapterRowData"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs.

if TYPE_CHECKING:
    from .app import (
        AppHooks,
        AppServices,
        AsyncBridge,
        DownloadManagerLike,
        EventBusLike,
        GUILabels,
        ImageCacheLike,
        LibraryLike,
        NexusDLApp,
        SettingsLike,
        SiteRegistryLike,
        StateStoreLike,
        VIEW_DOWNLOADS,
        VIEW_LIBRARY,
        VIEW_SEARCH,
        VIEW_SETTINGS,
    )
    from .components import (
        ChapterRowData,
        ChapterTable,
        MangaCard,
        MangaCardData,
        ProgressState,
        ProgressWidget,
        SiteDropdown,
    )
    from .views import (
        DownloadView,
        LibraryView,
        MainView,
        SearchView,
        SettingsView,
        ViewSpec,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public name on first access (:pep:`562`).

    Raises:
        AttributeError: If ``name`` is not a known public symbol.
        ImportError: If the symbol's module cannot be imported because an
            optional dependency is missing (e.g. ``customtkinter`` on a
            Web-only install). The message points at the ``[gui]`` extra.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _SYMBOLS.get(name)
    if entry is None:
        available = ", ".join(sorted(_SYMBOLS))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available lazy symbols: {available}"
        )

    subpackage, attr_name = entry
    try:
        module = importlib.import_module(f".{subpackage}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name in {"customtkinter", "PIL", "tkinter", "ttk"}:
            raise ImportError(
                f"{name!r} requires the GUI extra. "
                f"Install it with: pip install 'nexusdl[gui]'"
            ) from exc
        raise

    value = getattr(module, attr_name)

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose the full public API to ``dir()``, IDEs and tab-completion."""
    return sorted(list(globals().keys()) + list(__all__))


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy symbols.

    Does **not** include theme (eager) symbols — those are already in
    ``globals()`` and visible via ``dir()``. Use :func:`dir` for the
    complete picture.
    """
    return sorted(_SYMBOLS.keys())


def get_symbol(name: str) -> Any:
    """Return the top-level symbol registered under ``name``.

    Works for both eager (theme) and lazy (app / views / components)
    symbols — it just forwards to :func:`getattr`. Convenient for
    dynamic lookups (config-driven shells, plugins, parametrized tests).

    Raises:
        AttributeError: If ``name`` is not a known public symbol.
    """
    return getattr(__import__(__name__, fromlist=[name]), name)


def list_views() -> list[str]:
    """Return the sorted list of public view class names.

    Forwards to :func:`nexusdl.interfaces.gui.views.list_views` — see that
    module's docstring for what counts as a "view class".
    """
    from .views import list_views as _lv
    return _lv()


def list_components() -> list[str]:
    """Return the sorted list of public component names.

    Forwards to :func:`nexusdl.interfaces.gui.components.list_components`.
    """
    from .components import list_components as _lc
    return _lc()
