"""GUI views package for NexusDL.

Exposes the application views consumed by the GUI shell
(:class:`~nexusdl.interfaces.gui.views.main_view.MainView`). All exports
are **lazy** — importing this package never triggers ``customtkinter``,
``PIL``, ``ttk`` or any Tk initialization. Symbols are resolved on first
attribute access via :pep:`562` (``__getattr__``).

Why lazy:
    * ``nexusdl.interfaces.gui.views`` may be imported transitively by
      the CLI, TUI, Web backend, or the test suite. Those environments
      do not necessarily have ``customtkinter`` installed, and Tk must
      never be initialized in a headless process.
    * Importing a view module pulls its components (which pull ``PIL``
      and :class:`ttk.Treeview` for some). Only desktop-GUI code paths
      should pay that cost.

Why alias the ``Labels`` classes:
    * Every view defines its own ``Labels`` frozen dataclass locally
      (``SearchLabels``, ``LibraryLabels``, ``DownloadLabels``,
      ``SettingsLabels``, ``MainViewLabels``). To avoid clashing
      exports, they are re-exported here as ``<View>Labels``. New views
      should follow the same convention: define ``Labels`` locally,
      expose ``<View>Labels`` here.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# --- Package version (soft read from nexusdl.version) -----------------------

try:
    from nexusdl.version import __version__ as __version__
except Exception:  # pragma: no cover - defensive fallback
    __version__ = "3.0.0"


# --- Public API -------------------------------------------------------------

__all__ = [
    # Shell
    "MainView",
    "MainViewLabels",
    "ViewSpec",
    # Search
    "SearchView",
    "SearchLabels",
    "SearchLayout",
    "SearchRequest",
    "SearchPage",
    # Library
    "LibraryView",
    "LibraryLabels",
    "LibraryLayout",
    "LibraryCardData",
    "LibrarySortOption",
    "LibraryFilterSpec",
    "LibraryQuery",
    "LibraryPage",
    "BatchAction",
    # Downloads
    "DownloadView",
    "DownloadLabels",
    "DownloadTaskData",
    "DownloadFilterSpec",
    "default_filters",
    # Settings
    "SettingsView",
    "SettingsLabels",
    "SettingsSchema",
    "SettingSection",
    "SettingGroup",
    "SettingField",
    "FieldKind",
    "ApplyResult",
    # Introspection helpers
    "list_views",
    "get_view",
]


# --- Lazy import registry ---------------------------------------------------
#
# Maps exported name -> (module_basename, attribute_in_module).
# Adding a new view is a one-line change here and a matching
# TYPE_CHECKING re-export below. No other file must be modified.
#
_VIEWS: dict[str, tuple[str, str]] = {
    # Shell
    "MainView": ("main_view", "MainView"),
    "MainViewLabels": ("main_view", "MainViewLabels"),
    "ViewSpec": ("main_view", "ViewSpec"),
    # Search
    "SearchView": ("search_view", "SearchView"),
    "SearchLabels": ("search_view", "SearchLabels"),
    "SearchLayout": ("search_view", "SearchLayout"),
    "SearchRequest": ("search_view", "SearchRequest"),
    "SearchPage": ("search_view", "SearchPage"),
    # Library
    "LibraryView": ("library_view", "LibraryView"),
    "LibraryLabels": ("library_view", "LibraryLabels"),
    "LibraryLayout": ("library_view", "LibraryLayout"),
    "LibraryCardData": ("library_view", "LibraryCardData"),
    "LibrarySortOption": ("library_view", "LibrarySortOption"),
    "LibraryFilterSpec": ("library_view", "LibraryFilterSpec"),
    "LibraryQuery": ("library_view", "LibraryQuery"),
    "LibraryPage": ("library_view", "LibraryPage"),
    "BatchAction": ("library_view", "BatchAction"),
    # Downloads
    "DownloadView": ("download_view", "DownloadView"),
    "DownloadLabels": ("download_view", "DownloadLabels"),
    "DownloadTaskData": ("download_view", "DownloadTaskData"),
    "DownloadFilterSpec": ("download_view", "DownloadFilterSpec"),
    "default_filters": ("download_view", "default_filters"),
    # Settings
    "SettingsView": ("settings_view", "SettingsView"),
    "SettingsLabels": ("settings_view", "SettingsLabels"),
    "SettingsSchema": ("settings_view", "SettingsSchema"),
    "SettingSection": ("settings_view", "SettingSection"),
    "SettingGroup": ("settings_view", "SettingGroup"),
    "SettingField": ("settings_view", "SettingField"),
    "FieldKind": ("settings_view", "FieldKind"),
    "ApplyResult": ("settings_view", "ApplyResult"),
}


# --- Static re-exports for type checkers and IDEs ---------------------------
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs.

if TYPE_CHECKING:
    from .download_view import (
        DownloadFilterSpec,
        DownloadLabels,
        DownloadTaskData,
        DownloadView,
        default_filters,
    )
    from .library_view import (
        BatchAction,
        LibraryCardData,
        LibraryFilterSpec,
        LibraryLabels,
        LibraryLayout,
        LibraryPage,
        LibraryQuery,
        LibrarySortOption,
        LibraryView,
    )
    from .main_view import MainView, MainViewLabels, ViewSpec
    from .search_view import (
        SearchLabels,
        SearchLayout,
        SearchPage,
        SearchRequest,
        SearchView,
    )
    from .settings_view import (
        ApplyResult,
        FieldKind,
        SettingField,
        SettingGroup,
        SettingSection,
        SettingsLabels,
        SettingsSchema,
        SettingsView,
    )


# --- Lazy loader ------------------------------------------------------------

def __getattr__(name: str) -> Any:
    """Resolve a public name on first access (:pep:`562`).

    Raises:
        AttributeError: If ``name`` is not a known public view symbol.
        ImportError: If the view's module cannot be imported because an
            optional dependency is missing (e.g. ``customtkinter`` on a
            Web-only install). The message points at the ``[gui]`` extra.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    entry = _VIEWS.get(name)
    if entry is None:
        available = ", ".join(sorted(_VIEWS))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available views: {available}"
        )

    module_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{module_basename}", __name__)
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


# --- Introspection helpers --------------------------------------------------

# Names that identify a *view class* (as opposed to its data/labels types).
# Kept explicit so ``list_views()`` is stable across renames of helper types.
_VIEW_CLASS_NAMES: tuple[str, ...] = (
    "MainView",
    "SearchView",
    "LibraryView",
    "DownloadView",
    "SettingsView",
)


def list_views() -> list[str]:
    """Return the sorted list of public view class names.

    Only actual view classes (widgets that can be placed in the shell) are
    returned — data types, labels, enums, and helpers are excluded. Useful
    for parametrized tests, debugging screens, or a future "component
    gallery" / "view registry" feature.
    """
    return sorted(_VIEW_CLASS_NAMES)


def get_view(name: str) -> Any:
    """Return the view class registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown or when it refers to a non-view
    symbol. Convenience for dynamic lookups (plugins, config-driven shells).

    Raises:
        KeyError: If ``name`` is not one of the registered view classes.
    """
    if name not in _VIEW_CLASS_NAMES:
        available = ", ".join(_VIEW_CLASS_NAMES)
        raise KeyError(
            f"Unknown view class {name!r}. Available view classes: {available}"
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
