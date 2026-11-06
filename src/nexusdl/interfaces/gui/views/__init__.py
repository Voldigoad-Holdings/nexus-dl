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

Public surface
--------------
:data:`__all__` lists twenty-nine symbols:

* **Twenty-five lazy exports** — the five view classes, their labels,
  their data contracts, and a handful of helper types. Resolved from
  their submodules on first access by :func:`__getattr__`.
* **Four introspection helpers** — :func:`list_views`,
  :func:`get_view`, and the two dunder methods :func:`__dir__` and the
  module's own ``__version__``. The first two are defined locally and
  never route through the lazy loader.

:func:`list_views` is **narrower** than :data:`__all__` — it reports the
five *view class names* only (the widgets a shell can host), not the
data/labels/enums that live alongside them. A caller that wants every
public symbol iterates :data:`__all__`; a caller that wants to build a
"view gallery" iterates :func:`list_views`. The two lists are
intentionally distinct: exposing a :class:`LibrarySortOption` as a
"view" would be misleading, and forcing every public export into
:func:`list_views` would make the function useless to its actual
audience.

Adding a new view
-----------------
Three steps, one file each:

1. Create ``<new_view>.py`` in this package, defining a
   ``<New>View`` class, a ``<New>Labels`` frozen dataclass, and the
   data contracts the view consumes.
2. Add the exports to :data:`_VIEWS` (one entry per exported name) and
   the view class name to :data:`_VIEW_CLASS_NAMES`. Add every new
   public symbol to :data:`__all__`.
3. Add the matching imports to the ``TYPE_CHECKING`` block so mypy and
   IDEs resolve the symbols.

An inconsistent addition — a name in ``__all__`` but not in ``_VIEWS``,
or a view class name in ``_VIEW_CLASS_NAMES`` but not in ``_VIEWS`` —
fails the module-level sanity check at the bottom of this file, so
``import nexusdl.interfaces.gui.views`` raises :class:`RuntimeError`
immediately rather than the first ``from ... import *`` in a
downstream caller.
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
# (e.g. ``list_views()`` in a build script) still works. Any other
# import failure (a SyntaxError in ``nexusdl/version.py``, a circular
# import) is a genuine bug and must propagate.
try:
    from nexusdl.version import __version__ as __version__
except ImportError:  # pragma: no cover - broken installation
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

#: Names defined *locally* in this module, never routed through
#: :data:`_VIEWS`. They are the two introspection helpers. Kept in a
#: separate constant rather than inlined so :func:`list_views` and the
#: ``__all__`` consistency check both derive from one place.
_LOCAL: frozenset[str] = frozenset({"list_views", "get_view"})

#: Names that identify a *view class* (as opposed to its data / labels
#: types). Kept explicit so :func:`list_views` is stable across renames
#: of helper types. Every entry **must** also be present in
#: :data:`_VIEWS` — the sanity check at the bottom of this file
#: enforces that invariant.
_VIEW_CLASS_NAMES: frozenset[str] = frozenset(
    {
        "MainView",
        "SearchView",
        "LibraryView",
        "DownloadView",
        "SettingsView",
    }
)


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
        available = ", ".join(sorted(set(_VIEWS) | _LOCAL))
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
    """Expose the full public API to ``dir()``, IDEs and tab-completion.

    The result is deduplicated: a name that has already been resolved
    (and therefore appears in ``globals()``) must not also appear via
    :data:`__all__`. Python's ``dir()`` contract allows duplicates in
    theory, but every IDE and every tab-completion engine assumes a set.
    """
    return sorted(set(globals().keys()) | set(__all__))


# --- Introspection helpers --------------------------------------------------

def list_views() -> list[str]:
    """Return the sorted list of public view class names.

    Only actual view classes (widgets that can be placed in the shell) are
    returned — data types, labels, enums, and helpers are excluded. This
    is **narrower** than :data:`__all__`, intentionally: the shell
    cannot host a :class:`LibrarySortOption`, and forcing every public
    export into this function would make it useless to its actual
    audience (a "view gallery" debug screen, a parametrized test that
    instantiates every view, a config-driven shell that lists the
    available destinations).

    A caller that wants every public symbol iterates :data:`__all__`
    directly. A caller that wants the view classes uses this function.

    Useful for parametrized tests, debugging screens, or a future
    "component gallery" / "view registry" feature.
    """
    return sorted(_VIEW_CLASS_NAMES)


def get_view(name: str) -> Any:
    """Return the view class registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown or when it refers to a non-view
    symbol. Convenience for dynamic lookups (plugins, config-driven
    shells).

    For a **view class** in :data:`_VIEW_CLASS_NAMES`, this routes
    through :func:`__getattr__` so the cache is populated with the same
    value a plain attribute access would return. This avoids the subtle
    issue with ``__import__(__name__, fromlist=[name])``, which would
    import a **submodule** with the same name as the attribute when one
    exists (e.g. ``fromlist=["main_view"]`` would return the submodule
    instead of the ``MainView`` class).

    Raises:
        KeyError: If ``name`` is not one of the registered view classes.
    """
    if name not in _VIEW_CLASS_NAMES:
        available = ", ".join(sorted(_VIEW_CLASS_NAMES))
        raise KeyError(
            f"Unknown view class {name!r}. Available view classes: {available}"
        )
    return __getattr__(name)


# --- Sanity: __all__ must exactly match the union of lazy + local exports ---
#
# This assertion runs at import time and would fail on any future edit
# that adds an entry to _VIEWS without adding it to __all__ (or vice
# versa), or that adds a local helper without registering it in _LOCAL.
# The cost is a single set comparison at import; the benefit is that an
# inconsistent public surface is caught the moment the package loads, not
# on the first `from nexusdl.interfaces.gui.views import *` in a caller.

if frozenset(__all__) != (frozenset(_VIEWS) | _LOCAL):
    _missing_from_all = (frozenset(_VIEWS) | _LOCAL) - frozenset(__all__)
    _missing_from_exports = frozenset(__all__) - (frozenset(_VIEWS) | _LOCAL)
    raise RuntimeError(
        "nexusdl.interfaces.gui.views.__all__ does not match the union "
        f"of _VIEWS and _LOCAL. Missing from __all__: "
        f"{sorted(_missing_from_all)}. Listed but not exported: "
        f"{sorted(_missing_from_exports)}."
    )

#: Every view class name must also be a lazy export. A name in
#: :data:`_VIEW_CLASS_NAMES` that is not in :data:`_VIEWS` is a package
#: inconsistency — :func:`get_view` would find it in
#: :data:`_VIEW_CLASS_NAMES` but :func:`__getattr__` would reject it.
if not _VIEW_CLASS_NAMES <= frozenset(_VIEWS):
    _orphans = _VIEW_CLASS_NAMES - frozenset(_VIEWS)
    raise RuntimeError(
        "nexusdl.interfaces.gui.views._VIEW_CLASS_NAMES contains names "
        f"that are not in _VIEWS: {sorted(_orphans)}."
    )
