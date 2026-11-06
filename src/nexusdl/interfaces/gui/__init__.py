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

Public surface
==============

:data:`__all__` lists sixty-three symbols, split into:

* **four app-level introspection helpers** (:func:`list_views`,
  :func:`list_components`, :func:`list_symbols`, :func:`get_symbol`)
  defined locally in this module,
* **twenty-nine lazy symbols** — the composition root, the view classes,
  the component classes — resolved on first access from their submodule,
* **thirty theme symbols** — imported eagerly so a caller can read a
  theme without paying for `customtkinter`.

Every lazy symbol is registered in :data:`_SYMBOLS`; every local helper
is registered in :data:`_LOCAL`. Adding a new export is a one-line change
to :data:`__all__` plus the matching registry entry; :data:`__all__` is
asserted to equal the union of the two registries at import time, so a
future addition cannot ship with an inconsistent surface.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# --- Package version (soft read from nexusdl.version) -----------------------
#
# A missing ``nexusdl.version`` module is a broken installation, not a
# runtime error the GUI can recover from — but we do not want to crash
# the whole package import on a partially-installed environment. The
# fallback exists so a caller that only wants to inspect the package
# (e.g. ``list_symbols()`` in a build script) still works. Any other
# import failure (a SyntaxError in ``nexusdl/version.py``, a circular
# import) is a genuine bug and must propagate.
try:
    from nexusdl.version import __version__ as __version__
except ImportError:  # pragma: no cover - broken installation
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

#: Names defined *locally* in this module, never routed through
#: :data:`_SYMBOLS`. They are the four introspection helpers. Kept in a
#: separate constant rather than inlined so :func:`list_symbols` and the
#: ``__all__`` consistency check both derive from one place.
_LOCAL: frozenset[str] = frozenset(
    {"list_views", "list_components", "list_symbols", "get_symbol"}
)

#: Names imported eagerly from :mod:`.theme`. They never route through
#: :func:`__getattr__` — they are already bound in ``globals()`` before
#: any lazy access happens. The set is the exact complement of
#: ``_SYMBOLS ∪ _LOCAL`` within ``__all__``.
_EAGER: frozenset[str] = frozenset(
    name for name in __all__ if name not in _SYMBOLS and name not in _LOCAL
)


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
        available = ", ".join(sorted(list(_SYMBOLS) + list(_LOCAL)))
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
    """Expose the full public API to ``dir()``, IDEs and tab-completion.

    The result is deduplicated: a name that has already been resolved
    (and therefore appears in ``globals()``) must not also appear via
    :data:`__all__`. Every IDE and every tab-completion engine assumes
    the list is a set.
    """
    return sorted(set(globals().keys()) | set(__all__))


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level exports (lazy + eager + local).

    The list is the union of:

    * the twenty-nine **lazy** symbols in :data:`_SYMBOLS`,
    * the thirty **eager** symbols imported from :mod:`.theme`,
    * the four **local** introspection helpers (:func:`list_views`,
      :func:`list_components`, :func:`list_symbols`, :func:`get_symbol`).

    This matches :data:`__all__` exactly — the invariant is checked by
    the module-level assertion at the bottom of this file.
    """
    return sorted(set(_SYMBOLS) | _LOCAL | _EAGER)


def get_symbol(name: str) -> Any:
    """Return the top-level symbol registered under ``name``.

    Works for **every** public symbol:

    * a **lazy** symbol routes through :func:`__getattr__` so the cache
      is populated with the same value a plain attribute access would
      return. The alternative — ``__import__(__name__, fromlist=[name])``
      — would import a *submodule* whose name matches the attribute (the
      pattern is fragile if a future export ever shares its name with a
      subpackage, e.g. a hypothetical ``"views"`` export).
    * an **eager** theme symbol is already bound in ``globals()`` — a
      plain dict lookup.
    * a **local** helper is already bound in ``globals()`` — a plain
      dict lookup.

    Convenient for dynamic lookups (config-driven shells, plugins,
    parametrized tests).

    Raises:
        KeyError: If ``name`` is not a public symbol.
    """
    if (
        name not in _SYMBOLS
        and name not in _LOCAL
        and name not in _EAGER
    ):
        available = ", ".join(list_symbols())
        raise KeyError(
            f"Unknown symbol {name!r}. Available symbols: {available}"
        )
    if name in _LOCAL or name in _EAGER:
        return globals()[name]
    return __getattr__(name)


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


# ===========================================================================
# Sanity: __all__ must exactly match the union of lazy + local + eager
# ===========================================================================
# This assertion runs at import time and would fail on any future edit
# that adds a symbol to one registry without adding it to __all__ (or
# vice versa), or that changes the eager import list without updating
# __all__. The cost is a single set comparison at import; the benefit is
# that an inconsistent public surface is caught the moment the package
# loads, not on the first `from nexusdl.interfaces.gui import *` in a
# caller.

if frozenset(__all__) != (frozenset(_SYMBOLS) | _LOCAL | _EAGER):
    _missing_from_all = (
        (frozenset(_SYMBOLS) | _LOCAL | _EAGER) - frozenset(__all__)
    )
    _missing_from_exports = frozenset(__all__) - (
        frozenset(_SYMBOLS) | _LOCAL | _EAGER
    )
    raise RuntimeError(
        "nexusdl.interfaces.gui.__all__ does not match the union of "
        f"_SYMBOLS, _LOCAL, and _EAGER. Missing from __all__: "
        f"{sorted(_missing_from_all)}. Listed but not exported: "
        f"{sorted(_missing_from_exports)}."
    )
