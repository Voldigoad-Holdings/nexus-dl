"""GUI components package for NexusDL.

This package exposes the reusable CustomTkinter widgets used by the desktop
GUI. All exports are **lazy** — importing this module never triggers
``customtkinter``, ``PIL``, ``ttk`` or any Tk initialization. Symbols are
resolved on first attribute access via :pep:`562` (``__getattr__``).

Why lazy:
    * ``nexusdl.interfaces.gui.components`` may be imported transitively by
      the CLI, TUI, Web backend or the test suite. Those environments do
      not necessarily have ``customtkinter`` installed, and Tk must never
      be initialized in a headless process.
    * Startup cost of importing Tk + PIL + loguru is non-trivial; only
      desktop-GUI code paths pay it, and only when a widget is actually
      needed.

Why alias the ``Labels`` classes:
    * Multiple widgets define their own ``Labels`` frozen dataclass
      (currently ``progress_widget`` and ``manga_card``). To avoid a name
      clash at package level, they are re-exported as
      ``<Component>Labels``. New components should follow the same
      convention: define ``Labels`` locally, expose ``<Component>Labels``
      here.
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
    # Site selection
    "SiteDropdown",
    # Progress / tasks
    "ProgressWidget",
    "ProgressState",
    "ProgressLabels",
    # Manga display
    "MangaCard",
    "MangaCardData",
    "CardOrientation",
    "CoverState",
    "MangaCardLabels",
    # Chapter table
    "ChapterTable",
    "ChapterRowData",
    "ChapterRowStatus",
    "ColumnSpec",
    "ChapterTableLabels",
    "default_columns",
    # Introspection helpers
    "list_components",
    "get_component",
]


# --- Lazy import registry ---------------------------------------------------
#
# Maps exported name -> (module_basename, attribute_in_module).
# Adding a new component is a one-line change here and a matching
# TYPE_CHECKING re-export below. No other file must be modified.
#
_COMPONENTS: dict[str, tuple[str, str]] = {
    # Site dropdown
    "SiteDropdown": ("site_dropdown", "SiteDropdown"),
    # Progress widget
    "ProgressWidget": ("progress_widget", "ProgressWidget"),
    "ProgressState": ("progress_widget", "ProgressState"),
    "ProgressLabels": ("progress_widget", "Labels"),
    # Manga card
    "MangaCard": ("manga_card", "MangaCard"),
    "MangaCardData": ("manga_card", "MangaCardData"),
    "CardOrientation": ("manga_card", "CardOrientation"),
    "CoverState": ("manga_card", "CoverState"),
    "MangaCardLabels": ("manga_card", "Labels"),
    # Chapter table
    "ChapterTable": ("chapter_table", "ChapterTable"),
    "ChapterRowData": ("chapter_table", "ChapterRowData"),
    "ChapterRowStatus": ("chapter_table", "ChapterRowStatus"),
    "ColumnSpec": ("chapter_table", "ColumnSpec"),
    "ChapterTableLabels": ("chapter_table", "ChapterTableLabels"),
    "default_columns": ("chapter_table", "default_columns"),
}


# --- Static re-exports for type checkers and IDEs ---------------------------
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs.

if TYPE_CHECKING:
    from .chapter_table import (
        ChapterRowData,
        ChapterRowStatus,
        ChapterTable,
        ChapterTableLabels,
        ColumnSpec,
        default_columns,
    )
    from .manga_card import (
        CardOrientation,
        CoverState,
        Labels as MangaCardLabels,
        MangaCard,
        MangaCardData,
    )
    from .progress_widget import (
        Labels as ProgressLabels,
        ProgressState,
        ProgressWidget,
    )
    from .site_dropdown import SiteDropdown


# --- Lazy loader ------------------------------------------------------------

def __getattr__(name: str) -> Any:
    """Resolve a public name on first access (:pep:`562`).

    Raises:
        AttributeError: If ``name`` is not a known public component.
        ImportError: If the component's module cannot be imported because
            an optional dependency is missing (e.g. ``customtkinter`` on a
            Web-only install). The message points at the ``[gui]`` extra.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    entry = _COMPONENTS.get(name)
    if entry is None:
        available = ", ".join(sorted(_COMPONENTS))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available components: {available}"
        )

    module_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{module_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Surface a friendly message when the GUI extra is missing.
        if exc.name in {"customtkinter", "PIL", "tkinter"}:
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

def list_components() -> list[str]:
    """Return the sorted list of public component names.

    Useful for plugin discovery, dynamic GUI builders, parametrized tests,
    or a future "component gallery" debug screen.
    """
    return sorted(_COMPONENTS.keys())


def get_component(name: str) -> Any:
    """Return the component registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown — convenient for dynamic lookups
    (plugins, config-driven UIs).

    Raises:
        KeyError: If ``name`` is not a registered component.
    """
    if name not in _COMPONENTS:
        available = ", ".join(sorted(_COMPONENTS))
        raise KeyError(
            f"Unknown component {name!r}. Available components: {available}"
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
