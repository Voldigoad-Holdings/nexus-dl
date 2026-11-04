"""Public API of the NexusDL domain models.

The :mod:`nexusdl.core.models` package defines every **domain value object**
the framework passes between layers: the manga, the chapter, the page, the
site, the user, the search result, the download task and result, the
reading progress, and every enumeration those objects carry.

Architectural role
------------------
The models package is the **leaf of the domain dependency graph**. Every
model depends only on :mod:`nexusdl.core.models.enums` (and, in the case
of :class:`~nexusdl.core.models.download.DownloadTask`, on
:class:`~nexusdl.core.models.manga.Manga` and
:class:`~nexusdl.core.models.chapter.Chapter`, which themselves depend
only on enums). No model imports from ``core.library``,
``core.downloader``, ``core.image``, ``core.session``, ``core.registry``,
``parsers``, ``plugins``, or ``interfaces``.

That leaf position is what makes the package safe to import from anywhere:
a parser, a plugin, the database layer, the worker, and every composition
root all import models, and none of them creates a cycle by doing so.

Seven modules
-------------
* :mod:`~nexusdl.core.models.enums` - :class:`Language`,
  :class:`MangaStatus`, :class:`ContentRating`, :class:`PackagingFormat`,
  :class:`Priority`, :class:`DownloadStatus`, :class:`SiteHealth`,
  :class:`UserRole`. The absolute leaves - every other module in the
  package imports these.
* :mod:`~nexusdl.core.models.page` - :class:`Page`. The smallest unit
  the pipeline moves around; zero imports.
* :mod:`~nexusdl.core.models.manga` - :class:`Manga`. The central content
  entity.
* :mod:`~nexusdl.core.models.chapter` - :class:`Chapter`. The unit the
  worker downloads.
* :mod:`~nexusdl.core.models.user` - :class:`User`. The account model,
  with ``password_hash`` redacted in its repr and excluded from
  :meth:`User.to_public_dict`.
* :mod:`~nexusdl.core.models.site` - :class:`SiteConfig`,
  :class:`SiteCapabilities`, :class:`SiteHealthSnapshot`. Static site
  configuration plus runtime health.
* :mod:`~nexusdl.core.models.search` - :class:`SearchFilters`,
  :class:`SearchResult`, :class:`SearchResultPage`. The **search pipeline
  request/response shapes**.
* :mod:`~nexusdl.core.models.download` - :class:`DownloadTask`,
  :class:`DownloadResult`. The **download pipeline request/response
  shapes**.
* :mod:`~nexusdl.core.models.library` - :class:`ReadingProgress`,
  :class:`LibraryStats`. Aggregate library state.

.. note::
   ``nexusdl.core.models.library`` (the **model** for library data) is
   unrelated to :mod:`nexusdl.core.library` (the **subsystem** that owns
   the SQLite database). The same name appears twice in the tree for
   historical reasons; the model file is a value object, the subsystem is
   a service.

Usage
-----
The most common import pattern from application code::

    from nexusdl.core.models import (
        Chapter,
        ContentRating,
        Language,
        Manga,
        MangaStatus,
        PackagingFormat,
        Priority,
    )

    manga = await parser.get_manga(url)
    if manga.content_rating is not ContentRating.SAFE:
        ...

    for chapter in await parser.get_chapters(manga):
        task = DownloadTask(
            manga=manga,
            chapter=chapter,
            format=PackagingFormat.CBZ,
            priority=Priority.NORMAL,
        )

For search plumbing::

    from nexusdl.core.models import (
        SearchFilters,
        SearchResult,
        SearchResultPage,
    )

For download pipeline plumbing::

    from nexusdl.core.models import DownloadResult, DownloadTask

For enum-only access in a hot path, prefer the direct submodule import
to skip this package's `__getattr__` hop::

    from nexusdl.core.models.enums import Language

Lazy loading
------------
All twenty-two exports are resolved through PEP 562's ``__getattr__``
mechanism. Importing ``nexusdl.core.models`` **does not import any
submodule** - not even ``enums``.

This matters because some model modules pull in **pydantic** (used by
:class:`~nexusdl.core.models.manga.Manga`, :class:`~nexusdl.core.models.chapter.Chapter`,
etc.), and a caller that only needs, say, :class:`Language` should not
pay pydantic's ~30 ms import cost. With lazy loading:

* ``from nexusdl.core.models import Language`` → loads ``enums.py`` only
  (~2 ms, stdlib-only).
* ``from nexusdl.core.models import Manga`` → loads ``manga.py``, which
  loads ``enums.py`` and pydantic (~30 ms).
* ``from nexusdl.core.models import DownloadTask`` → loads
  ``download.py``, which loads ``manga.py``, ``chapter.py``, and pydantic
  (~30 ms).

The public surface is unchanged: ``from nexusdl.core.models import X``
works exactly as before - resolved on first access and cached.

**Why the leaf-to-root ordering guarantee is no longer needed.** An
earlier version of this file imported every submodule eagerly, in a
strict leaf-to-root order (enums first, then leaves, then composites),
to guarantee that ``from nexusdl.core.models import X`` never triggered
a partially-initialized module. Lazy loading removes that guarantee -
each submodule is imported on first access, in whatever order the caller
happens to access them. This is still **safe** because the graph has no
cycles: every submodule imports ``enums`` (a pure-stdlib leaf) and
composites (``download``) import primitives (``manga``, ``chapter``)
which themselves only import ``enums``. Python's normal import
machinery resolves nested imports correctly in the presence of a
cycle-free graph, regardless of entry point. The only scenario the old
ordering protected against - a partially-initialized module caused by a
cycle - cannot occur by design.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* ``_SITE_ID_PATTERN``, ``_HEX16_PATTERN``, ``_MANGA_ID_PATTERN``,
  ``_INVALID_FILENAME_CHARS``, ``_HTTPS_PREFIX``, ``_TERMINAL_STATUSES``,
  ``_MANGA_ID_LENGTH``, ``_ID_LENGTH`` - compiled regexes and numeric
  constants.
* ``_validate_*`` helpers in :mod:`~nexusdl.core.models.manga` and
  :mod:`~nexusdl.core.models.chapter` - private to their class's
  ``__post_init__``.
* ``_utcnow`` in :mod:`~nexusdl.core.models.user` - module-private.

Dependency graph (no cycles)::

    enums.py       (pure leaf - stdlib only)
        ^
        |
    +---+----+--------+--------+--------+---------+
    |        |        |        |        |         |
  page     user    manga   chapter   site    search   library
  (leaf)  (enums) (enums)  (enums) (enums) (enums)  (leaf)
                                              ^
                                              |
                                          download
                                  (manga + chapter + enums)

The arrows above describe **runtime dependencies at first use**, not
imports performed by this file. This file imports **nothing** at module
load - the nine submodules are loaded lazily, on first attribute access.
Python's normal import machinery resolves the nested imports correctly
regardless of which symbol is accessed first (the graph is cycle-free,
so ordering does not matter).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- Enums ----------------------------------------------------------- #
    "ContentRating",
    "DownloadStatus",
    "Language",
    "MangaStatus",
    "PackagingFormat",
    "Priority",
    "SiteHealth",
    "UserRole",
    # --- Core content ---------------------------------------------------- #
    "Chapter",
    "Manga",
    "Page",
    # --- Site ------------------------------------------------------------ #
    "SiteCapabilities",
    "SiteConfig",
    "SiteHealthSnapshot",
    # --- User ------------------------------------------------------------ #
    "User",
    # --- Search pipeline ------------------------------------------------- #
    "SearchFilters",
    "SearchResult",
    "SearchResultPage",
    # --- Download pipeline ---------------------------------------------- #
    "DownloadResult",
    "DownloadTask",
    # --- Library state --------------------------------------------------- #
    "LibraryStats",
    "ReadingProgress",
    # --- Introspection --------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (submodule basename, attribute name in that submodule).
# Every listed symbol is defined at module level in the given submodule.
# Adding a new top-level export is a one-line change here plus a matching
# TYPE_CHECKING re-export below.

_LAZY: dict[str, tuple[str, str]] = {
    # --- enums (pure stdlib, ~2 ms) -------------------------------------- #
    "ContentRating": ("enums", "ContentRating"),
    "DownloadStatus": ("enums", "DownloadStatus"),
    "Language": ("enums", "Language"),
    "MangaStatus": ("enums", "MangaStatus"),
    "PackagingFormat": ("enums", "PackagingFormat"),
    "Priority": ("enums", "Priority"),
    "SiteHealth": ("enums", "SiteHealth"),
    "UserRole": ("enums", "UserRole"),
    # --- page (stdlib only, zero in-package imports) --------------------- #
    "Page": ("page", "Page"),
    # --- chapter (enums + pydantic) -------------------------------------- #
    "Chapter": ("chapter", "Chapter"),
    # --- manga (enums + pydantic) ---------------------------------------- #
    "Manga": ("manga", "Manga"),
    # --- site (enums + pydantic) ----------------------------------------- #
    "SiteCapabilities": ("site", "SiteCapabilities"),
    "SiteConfig": ("site", "SiteConfig"),
    "SiteHealthSnapshot": ("site", "SiteHealthSnapshot"),
    # --- user (enums + pydantic) ----------------------------------------- #
    "User": ("user", "User"),
    # --- search (enums + pydantic) --------------------------------------- #
    "SearchFilters": ("search", "SearchFilters"),
    "SearchResult": ("search", "SearchResult"),
    "SearchResultPage": ("search", "SearchResultPage"),
    # --- download (manga + chapter + enums + pydantic) ------------------- #
    "DownloadResult": ("download", "DownloadResult"),
    "DownloadTask": ("download", "DownloadTask"),
    # --- library (models, stdlib only) ----------------------------------- #
    "LibraryStats": ("library", "LibraryStats"),
    "ReadingProgress": ("library", "ReadingProgress"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.download import DownloadResult, DownloadTask
    from nexusdl.core.models.enums import (
        ContentRating,
        DownloadStatus,
        Language,
        MangaStatus,
        PackagingFormat,
        Priority,
        SiteHealth,
        UserRole,
    )
    from nexusdl.core.models.library import LibraryStats, ReadingProgress
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.page import Page
    from nexusdl.core.models.search import (
        SearchFilters,
        SearchResult,
        SearchResultPage,
    )
    from nexusdl.core.models.site import (
        SiteCapabilities,
        SiteConfig,
        SiteHealthSnapshot,
    )
    from nexusdl.core.models.user import User


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public domain model on first access (PEP 562).

    Called by Python's import machinery whenever an attribute is accessed
    on this package that is not already bound in ``globals()``. Only the
    names in :data:`__all__` are handled; anything else raises
    :class:`AttributeError` so that ``hasattr``, ``getattr(x, name,
    default)`` and interactive introspection behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The symbol registered under ``name`` in the submodule recorded in
        :data:`_LAZY`. The submodule is imported once and its binding is
        cached in ``globals()``, so subsequent accesses are plain dict
        lookups.

    Raises:
        AttributeError: When ``name`` is not a public export, when the
            submodule cannot be imported (a missing optional dependency
            such as ``pydantic``, a syntax error deeper in the module, or
            a broken installation), or when the submodule does not define
            the requested name. ``AttributeError`` - rather than
            ``ImportError`` - is required by PEP 562 so that ``hasattr``
            works.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _LAZY.get(name)
    if entry is None:
        available = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available model exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.models.enums import "
            f"DownloadStatus' or 'from nexusdl.core.models.download "
            f"import DownloadTask'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the models package.
        if exc.name in {"pydantic", "pydantic_settings"}:
            raise ImportError(
                f"{name!r} requires the core extra (pydantic). "
                f"Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The models submodule {submodule_basename!r} does not define "
            f"{attr_name!r}. This is a package-internal inconsistency - "
            f"nexusdl.core.models.__all__ and the submodules are out of "
            f"sync."
        ) from None

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the public names for interactive introspection.

    Python's default :func:`dir` implementation collects a module's
    ``__dict__`` and, when the module defines ``__getattr__``, calls it
    with each candidate name to decide what to display. Because none of
    the twenty-two exports are present in ``__dict__`` at import time, an
    unmodified :func:`dir` would not list them - the package would appear
    empty in a REPL.

    The returned list is :data:`__all__` plus the standard module dunders
    a user typically expects to see.
    """
    return sorted(
        set(__all__) | {"__all__", "__doc__", "__name__", "__package__"}
    )


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and tooling that needs to
    enumerate the domain models' public surface without importing every
    submodule.
    """
    # Only the twenty-two public exports, not the introspection helpers
    # themselves - otherwise ``list_symbols`` would include itself.
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the domain-model symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, schema exporters, composition-root builders
    that resolve a symbol from a config string).

    Raises:
        KeyError: If ``name`` is not a public model export.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown model symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.models.<submodule>' directly."
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
