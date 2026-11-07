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
``core.downloader``, ``core.image``, ``core.session``,
``core.registry``, ``parsers``, ``plugins``, or ``interfaces``.

That leaf position is what makes the package safe to import from anywhere:
a parser, a plugin, the database layer, the worker, and every composition
root all import models, and none of them creates a cycle by doing so.

**The models package is pure standard library.** Despite the PROMPT
MASTER listing ``pydantic`` as a core dependency, no model module uses
it — every model is a ``@dataclass`` from the stdlib. ``pydantic`` is used
by :mod:`nexusdl.core.config` (Settings) and by the API layer's schemas,
not by the domain value objects. That is deliberate: the models are
liftable into a standalone library without any third-party runtime
dependency.

Nine modules
------------
* :mod:`~nexusdl.core.models.enums` - :class:`Language`,
  :class:`MangaStatus`, :class:`ContentRating`, :class:`PackagingFormat`,
  :class:`Priority`, :class:`DownloadStatus`, :class:`SiteHealth`,
  :class:`UserRole`. The absolute leaf - every other module in the
  package imports these.
* :mod:`~nexusdl.core.models.page` - :class:`Page`. The smallest unit
  the pipeline moves around; **zero imports** (not even ``enums``).
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

.. note::
   :class:`ReadingProgress` is currently defined **twice** — inline in
   :mod:`nexusdl.core.library.database` and canonically in
   :mod:`nexusdl.core.models.library`. The two classes are structurally
   identical but the inline one skips validation entirely. The
   consolidation plan (a one-line import swap in ``database.py``) is
   documented in :mod:`nexusdl.core.models.library`. Until then, prefer
   importing from this package — the canonical version is the one that
   survives the swap.

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
to skip this package's ``__getattr__`` hop::

    from nexusdl.core.models.enums import Language

Lazy loading
------------
Twenty-two of the twenty-four exports are resolved through PEP 562's
``__getattr__`` mechanism. The two remaining exports are the
introspection helpers (:func:`list_symbols`, :func:`get_symbol`),
defined directly in this module. Importing ``nexusdl.core.models``
**does not import any submodule** — not even ``enums``.

The value of lazy loading here is more modest than for
:mod:`nexusdl.core.downloader` or :mod:`nexusdl.core.library`, because
every model is stdlib-only and the total import cost of the nine
submodules is small. Still, the pattern is:

* ``from nexusdl.core.models import Language`` → loads ``enums.py`` only
  (~2 ms, stdlib-only).
* ``from nexusdl.core.models import Manga`` → loads ``manga.py``, which
  loads ``enums.py`` and stdlib ``dataclasses``/``datetime`` (~3 ms).
* ``from nexusdl.core.models import DownloadTask`` → loads
  ``download.py``, which loads ``manga.py``, ``chapter.py``, and stdlib
  ``uuid`` (~4 ms).

The public surface is unchanged: ``from nexusdl.core.models import X``
works exactly as before — resolved on first access and cached.

**Why the leaf-to-root ordering guarantee is not needed.** An earlier
version of this file imported every submodule eagerly, in a strict
leaf-to-root order (enums first, then leaves, then composites), to
guarantee that ``from nexusdl.core.models import X`` never triggered a
partially-initialized module. Lazy loading removes that guarantee — each
submodule is imported on first access, in whatever order the caller
happens to access them. This is still **safe** because the graph has no
cycles: every submodule imports ``enums`` (a pure-stdlib leaf) and
composites (``download``) import primitives (``manga``, ``chapter``)
which themselves only import ``enums``. Python's normal import machinery
resolves nested imports correctly in the presence of a cycle-free graph,
regardless of entry point. The only scenario the old ordering protected
against — a partially-initialized module caused by a cycle — cannot
occur by design.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* ``_SITE_ID_PATTERN``, ``_HEX16_PATTERN``, ``_MANGA_ID_PATTERN``,
  ``_INVALID_FILENAME_CHARS``, ``_HTTPS_PREFIX``, ``_TERMINAL_STATUSES``,
  ``_MANGA_ID_LENGTH``, ``_ID_LENGTH``, ``_MAX_CLOCK_SKEW_SECONDS`` —
  compiled regexes and numeric constants.
* ``_validate_*`` helpers in :mod:`~nexusdl.core.models.manga` and
  :mod:`~nexusdl.core.models.chapter` — private to their class's
  ``__post_init__``.
* ``_utcnow`` in :mod:`~nexusdl.core.models.user` — module-private.

Dependency graph (no cycles)::

    page.py (leaf — stdlib only, no in-package imports)

    enums.py (leaf — stdlib only)
        ^
        |
    +---+----+-------+-------+--------+---------+
    |        |       |       |        |         |
  user    manga   chapter  site   search    library

                                    ^
                                    |
                                download
                       (manga + chapter + enums)

The arrows above describe **runtime dependencies at first use**, not
imports performed by this file. This file imports **nothing** at module
load — the nine submodules are loaded lazily, on first attribute access.
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
# Introspection helpers
# ===========================================================================
#
# Defined before ``__getattr__`` and ``_LAZY`` so they are always bound in
# ``globals()`` — ``__getattr__`` is only invoked for names that are *not*
# already present, so these bypass the lazy registry entirely.

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and tooling that needs to
    enumerate the domain models' public surface without importing every
    submodule.

    The two introspection helpers (:func:`list_symbols`, :func:`get_symbol`)
    are part of ``__all__`` but are **not** included in the returned list:
    they are defined directly in this module and do not live in any
    submodule.
    """
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the domain-model symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, schema exporters, composition-root builders
    that resolve a symbol from a config string).

    Both the submodule exports (resolved lazily through
    :func:`__getattr__`) and the two introspection helpers (defined in
    this module) are accepted.

    Raises:
        KeyError: If ``name`` is not a public model export.
    """
    if name in ("list_symbols", "get_symbol"):
        return globals()[name]
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown model symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.models.<submodule>' directly."
        )
    return __getattr__(name)


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
    # --- chapter (enums + stdlib dataclasses) ---------------------------- #
    "Chapter": ("chapter", "Chapter"),
    # --- manga (enums + stdlib dataclasses) ------------------------------ #
    "Manga": ("manga", "Manga"),
    # --- site (enums + stdlib dataclasses) ------------------------------- #
    "SiteCapabilities": ("site", "SiteCapabilities"),
    "SiteConfig": ("site", "SiteConfig"),
    "SiteHealthSnapshot": ("site", "SiteHealthSnapshot"),
    # --- user (enums + stdlib dataclasses) ------------------------------- #
    "User": ("user", "User"),
    # --- search (enums + stdlib dataclasses) ----------------------------- #
    "SearchFilters": ("search", "SearchFilters"),
    "SearchResult": ("search", "SearchResult"),
    "SearchResultPage": ("search", "SearchResultPage"),
    # --- download (manga + chapter + enums + stdlib) --------------------- #
    "DownloadResult": ("download", "DownloadResult"),
    "DownloadTask": ("download", "DownloadTask"),
    # --- library (stdlib only) ------------------------------------------ #
    "LibraryStats": ("library", "LibraryStats"),
    "ReadingProgress": ("library", "ReadingProgress"),
}


# Sanity check: ``__all__`` must be exactly the union of the lazy registry
# and the two helpers defined in this module. A mismatch means someone
# added a symbol to one place and forgot the other — silent divergence that
# only surfaces at runtime as ``AttributeError`` or ``ImportError`` in a
# specific import path.
_INTROSPECTION_HELPERS = frozenset({"list_symbols", "get_symbol"})
assert set(__all__) == set(_LAZY) | _INTROSPECTION_HELPERS, (
    f"__all__ / _LAZY mismatch: "
    f"extra in __all__={set(__all__) - set(_LAZY) - _INTROSPECTION_HELPERS}, "
    f"extra in _LAZY={set(_LAZY) - set(__all__)}"
)


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
            submodule cannot be imported (a syntax error deeper in the
            module, or a broken installation), or when the submodule does
            not define the requested name. ``AttributeError`` - rather
            than ``ImportError`` - is required by PEP 562 so that
            ``hasattr`` works.
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
    module = importlib.import_module(f".{submodule_basename}", __name__)

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The models submodule {submodule_basename!r} does not define "
            f"{attr_name!r}. This is a package-internal inconsistency - "
            f"nexusdl.core.models._LAZY and the submodules are out of "
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
    the lazy exports are present in ``__dict__`` at import time, an
    unmodified :func:`dir` would not list them - the package would appear
    empty in a REPL.

    The returned list is :data:`__all__`, the module dunders, and any
    attribute already bound in ``globals()`` (whether via a resolved lazy
    import or via external assignment). This matches Python's contract
    that ``__dir__`` should report everything accessible through
    ``getattr``.
    """
    return sorted(
        set(globals().keys())
        | set(__all__)
        | {"__all__", "__doc__", "__name__", "__package__"}
    )
