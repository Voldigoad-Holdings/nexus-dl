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

Public surface
--------------
:data:`__all__` lists seventeen symbols:

* **Fifteen lazy component exports** — the four widgets and their
  associated data classes, resolved from their submodules on first
  access by :func:`__getattr__`.
* **Two introspection helpers** — :func:`list_components` and
  :func:`get_component`, defined locally in this module. They never
  route through the lazy loader (they are the loader's own accessors),
  and :func:`list_components` reports the *component* surface (fifteen
  names) plus the two helpers, matching :data:`__all__` exactly.

Adding a new component
----------------------
Three steps, one file each:

1. Add an entry to :data:`_COMPONENTS` (the lazy registry) and a
   matching entry to :data:`__all__`.
2. Add the matching import to the ``TYPE_CHECKING`` block so mypy and
   IDEs resolve the symbol.
3. If the component defines its own ``Labels`` dataclass, expose it as
   ``<Component>Labels`` in both :data:`_COMPONENTS` and :data:`__all__`.

An inconsistent addition — a name in ``__all__`` but not in
``_COMPONENTS``, or vice versa — fails the module-level sanity check at
the bottom of this file, so ``import nexusdl.interfaces.gui.components``
raises :class:`RuntimeError` immediately rather than the first
``from ... import *`` in a downstream caller.
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
# (e.g. ``list_components()`` in a build script) still works. Any other
# import failure (a SyntaxError in ``nexusdl/version.py``, a circular
# import) is a genuine bug and must propagate.
try:
    from nexusdl.version import __version__ as __version__
except ImportError:  # pragma: no cover - broken installation
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

#: Names defined *locally* in this module, never routed through
#: :data:`_COMPONENTS`. They are the two introspection helpers. Kept in
#: a separate constant rather than inlined so :func:`list_components`
#: and the ``__all__`` consistency check both derive from one place.
_LOCAL: frozenset[str] = frozenset({"list_components", "get_component"})


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
    """Resolve a public component name on first access (:pep:`562`).

    Raises:
        AttributeError: If ``name`` is not a known public component.
        ImportError: If the component's module cannot be imported because
            an optional dependency is missing (e.g. ``customtkinter`` on
            a Web-only install). The message points at the ``[gui]``
            extra.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    entry = _COMPONENTS.get(name)
    if entry is None:
        available = ", ".join(sorted(list(_COMPONENTS) + list(_LOCAL)))
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
    """Expose the full public API to ``dir()``, IDEs and tab-completion.

    The result is deduplicated: a name that has already been resolved
    (and therefore appears in ``globals()``) must not also appear via
    :data:`__all__`. Python's ``dir()`` contract allows duplicates in
    theory, but every IDE and every tab-completion engine assumes a set.
    """
    return sorted(set(globals().keys()) | set(__all__))


# --- Introspection helpers --------------------------------------------------

def list_components() -> list[str]:
    """Return the sorted list of every public component export.

    The list is the union of:

    * the fifteen **lazy** component exports registered in
      :data:`_COMPONENTS`, and
    * the two **local** helpers (:func:`list_components` itself and
      :func:`get_component`), which are defined in this module's body.

    This matches :data:`__all__` exactly — the invariant is checked by
    the module-level assertion at the bottom of this file. Useful for
    plugin discovery, dynamic GUI builders, parametrized tests, or a
    future "component gallery" debug screen.
    """
    return sorted(set(_COMPONENTS) | _LOCAL)


def get_component(name: str) -> Any:
    """Return the component registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown — convenient for dynamic lookups
    (plugins, config-driven UIs).

    For the two **local** helpers (:func:`list_components`,
    :func:`get_component`), this is exactly a plain attribute access —
    they are already bound in the module namespace.

    For a **lazy** export, this routes through :func:`__getattr__` so
    the cache is populated with the same value a plain attribute access
    would return. This avoids the subtle issue with
    ``__import__(__name__, fromlist=[name])``, which would import a
    **submodule** with the same name as the attribute when one exists
    (e.g. ``fromlist=["progress_widget"]`` would return the submodule
    instead of the ``ProgressWidget`` class).

    Raises:
        KeyError: If ``name`` is not a public component export.
    """
    if name not in _COMPONENTS and name not in _LOCAL:
        available = ", ".join(list_components())
        raise KeyError(
            f"Unknown component {name!r}. Available components: {available}"
        )
    if name in _LOCAL:
        return globals()[name]
    return __getattr__(name)


# --- Sanity: __all__ must exactly match the union of lazy + local exports ---
#
# This assertion runs at import time and would fail on any future edit
# that adds an entry to _COMPONENTS without adding it to __all__ (or
# vice versa), or that adds a local helper without registering it in
# _LOCAL. The cost is a single set comparison at import; the benefit is
# that an inconsistent public surface is caught the moment the package
# loads, not on the first `from nexusdl.interfaces.gui.components import *`
# in a caller.

if frozenset(__all__) != (frozenset(_COMPONENTS) | _LOCAL):
    _missing_from_all = (frozenset(_COMPONENTS) | _LOCAL) - frozenset(__all__)
    _missing_from_exports = frozenset(__all__) - (frozenset(_COMPONENTS) | _LOCAL)
    raise RuntimeError(
        "nexusdl.interfaces.gui.components.__all__ does not match the "
        f"union of _COMPONENTS and _LOCAL. Missing from __all__: "
        f"{sorted(_missing_from_all)}. Listed but not exported: "
        f"{sorted(_missing_from_exports)}."
    )
