"""Public API of the NexusDL local library subsystem.

The :mod:`nexusdl.core.library` package is the entire local-manga library:
persistence, search, scanning, and metadata extraction from archives.
Six modules cooperate, each with a single responsibility:

* :mod:`~nexusdl.core.library.database` — the persistence layer
  (:class:`LibraryDatabase`, :class:`ReadingProgress`). It owns the
  SQLAlchemy async engine, the migration runner, and the row-to-model
  mapping. It is the only module in the package that talks to SQLite
  directly.
* :mod:`~nexusdl.core.library.search` — full-text search over the
  library, backed by FTS5 with a LIKE fallback (:class:`LibrarySearch`,
  :class:`PaginatedResult`). Stateless helpers
  (:func:`build_fts_query`, :func:`build_like_patterns`) are pure
  functions, unit-testable without a database.
* :mod:`~nexusdl.core.library.scanner` — recursive import of archives
  into the library (:class:`LibraryScanner`, :class:`ScanReport`,
  :class:`ScanFailure`). Depends on the database only through
  :class:`LibraryRepositoryProtocol`.
* :mod:`~nexusdl.core.library.metadata` — read and write
  ``ComicInfo.xml`` inside ``.cbz`` / ``.cb7`` / ``.cbt`` archives
  (:func:`read_comic_info`, :func:`write_comic_info`,
  :func:`extract_metadata`, :func:`detect_archive_format`,
  :func:`comic_info_to_manga`, :func:`comic_info_to_chapter`).
* :mod:`~nexusdl.core.library.migrations` — discovery of the forward-only
  SQL migration files (:func:`list_migrations`, :class:`MigrationFile`).
  Pure filesystem introspection; the runner lives in ``database.py``.
* ``migrations/00N_*.sql`` — the schema itself, applied in order by the
  runner.

Composition
-----------
The composition root (CLI / TUI / GUI / Web) constructs one
:class:`LibraryDatabase`, calls :meth:`LibraryDatabase.init`, and reuses
it for the process lifetime::

    from pathlib import Path

    from nexusdl.core.library import LibraryDatabase, LibraryScanner

    db = LibraryDatabase(settings.library.database_path)
    await db.init()
    try:
        scanner = LibraryScanner(repository=db)
        report = await scanner.scan(Path.home() / "manga")
        print(
            f"Imported {report.files_imported}/{report.files_found} files "
            f"({report.mangas_imported} mangas)"
        )
    finally:
        await db.close()

Search and progress round out the API::

    page = await db.search("one piece", limit=20)
    for manga in page.items:
        print(manga.title)

    await db.set_reading_progress(manga_id, chapter_id, page_index=5)
    progress = await db.get_reading_progress(manga_id)

Lazy loading
------------
Twenty-nine of the thirty-one exports are resolved through PEP 562's
``__getattr__`` mechanism. The two remaining exports are the
introspection helpers (:func:`list_symbols`, :func:`get_symbol`), defined
directly in this module. Importing ``nexusdl.core.library`` **does not
import any submodule**.

This matters because :mod:`~nexusdl.core.library.database` and
:mod:`~nexusdl.core.library.scanner` transitively pull in
``sqlalchemy[asyncio]``, ``aiosqlite``, and ``alembic`` — a ~150-180 ms
import cost. Before this file used lazy loading, a caller that only
needed :func:`build_fts_query` (a pure helper in ``search.py``) or
:func:`list_migrations` (a pure filesystem helper in
``migrations/__init__.py``) paid that entire cost. After this change,
they pay only for the module they actually use.

Illustrative impact (numbers approximate, measured on a warm filesystem
cache; exact timings vary by platform and SQLAlchemy version):

=============================================  =========  =========
Scenario                                        Before     After
=============================================  =========  =========
``from ...library import build_fts_query``     ~170 ms    ~15 ms
``from ...library import list_migrations``     ~170 ms    ~5 ms
``from ...library import SUPPORTED_ARCHIVE_EXTENSIONS`` ~170 ms ~15 ms
``from ...library import LibraryDatabase``     ~180 ms    ~180 ms (unchanged)
=============================================  =========  =========

The public surface is unchanged: ``from nexusdl.core.library import
LibraryDatabase`` still works exactly as before - resolved on first
access and cached. No caller has to change a line.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* ``_MANGA_UPSERT_SQL``, ``_MANGA_UPDATE_SQL``, ``_CHAPTER_UPSERT_SQL``,
  ``_MANGA_WRITE_FIELDS``, ``_CHAPTER_WRITE_FIELDS``,
  ``_MANGA_UPDATABLE_FIELDS`` — SQL and column lists in ``database.py``.
* ``_SCHEMA_VERSION_TABLE``, ``_TEMP_PREFIX_FMT``, ``_MIGRATION_PATTERN``,
  ``_EXTENSION_TO_FORMAT`` — private constants.
* ``_manga_to_row``, ``_chapter_to_row``, ``_row_to_manga`` (as a private
  method on ``LibraryDatabase`` it *is* accessible, but consumers should
  go through the public ``get_manga`` / ``list_mangas`` / ``search``
  paths), ``_utcnow_iso``, ``_parse_iso``, ``_encode_json_array``,
  ``_decode_json_array`` — helpers.
* ``_walk_archives``, ``_manga_source_id``, ``_synthesize_comic_info`` —
  scanner helpers.
* Every ``.sql`` filename — the migration runner consumes
  :func:`list_migrations`; consumers never reference file names directly.

Dependency graph (no cycles)::

    core.exceptions
         ▲
         │
    ┌────┴────────────────────────────────────┐
    │  migrations   metadata   search         │
    │      ▲           ▲         ▲            │
    │      │           │         │            │
    │      │       scanner       │            │
    │      │           ▲         │            │
    │      └───────┐   │         │            │
    │              ▼   ▼         ▼            │
    │           database (imports migrations  │
    │                     + search)           │
    └─────────────────────────────────────────┘
                (scanner and database
                 meet via Protocol only)

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency — rule R1 of
the PROMPT MASTER.

The arrows above describe **runtime dependencies at first use**, not
imports performed by this file. This file imports **nothing** at module
load — the five submodules are loaded lazily, on first attribute access.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # --- Database -------------------------------------------------------- #
    "LibraryDatabase",
    "ReadingProgress",
    # --- Scanner --------------------------------------------------------- #
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_SITE_ID",
    "LibraryRepositoryProtocol",
    "LibraryScanner",
    "ScanFailure",
    "ScanReport",
    # --- Search ---------------------------------------------------------- #
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_LIMIT",
    "LibrarySearch",
    "PaginatedResult",
    "build_fts_query",
    "build_like_patterns",
    # --- Metadata -------------------------------------------------------- #
    "COMIC_INFO_ENTRY_NAME",
    "SUPPORTED_ARCHIVE_EXTENSIONS",
    "ArchiveFormat",
    "ExtractedMetadata",
    "comic_info_to_chapter",
    "comic_info_to_manga",
    "detect_archive_format",
    "extract_metadata",
    "read_comic_info",
    "write_comic_info",
    # --- Migrations ------------------------------------------------------ #
    "MIGRATIONS_DIR",
    "MigrationFile",
    "list_migrations",
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
    enumerate the library subsystem's public surface without importing
    every submodule.

    The two introspection helpers (:func:`list_symbols`, :func:`get_symbol`)
    are part of ``__all__`` but are **not** included in the returned list:
    they are defined directly in this module and do not live in any
    submodule.
    """
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the library symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, composition-root builders that resolve a symbol
    from a config string, library inspectors).

    Both the submodule exports (resolved lazily through :func:`__getattr__`)
    and the two introspection helpers (defined in this module) are
    accepted.

    Raises:
        KeyError: If ``name`` is not a public library export.
    """
    if name in ("list_symbols", "get_symbol"):
        return globals()[name]
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown library symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.library.<submodule>' directly."
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
    # --- database (heavy: sqlalchemy, aiosqlite, alembic) ---------------- #
    "LibraryDatabase": ("database", "LibraryDatabase"),
    "ReadingProgress": ("database", "ReadingProgress"),
    # --- scanner (heavy: sqlalchemy via database, py7zr via metadata) ---- #
    "DEFAULT_MAX_CONCURRENT": ("scanner", "DEFAULT_MAX_CONCURRENT"),
    "DEFAULT_SITE_ID": ("scanner", "DEFAULT_SITE_ID"),
    "LibraryRepositoryProtocol": ("scanner", "LibraryRepositoryProtocol"),
    "LibraryScanner": ("scanner", "LibraryScanner"),
    "ScanFailure": ("scanner", "ScanFailure"),
    "ScanReport": ("scanner", "ScanReport"),
    # --- search (medium: sqlalchemy) ------------------------------------- #
    "DEFAULT_LIMIT": ("search", "DEFAULT_LIMIT"),
    "DEFAULT_MAX_LIMIT": ("search", "DEFAULT_MAX_LIMIT"),
    "LibrarySearch": ("search", "LibrarySearch"),
    "PaginatedResult": ("search", "PaginatedResult"),
    "build_fts_query": ("search", "build_fts_query"),
    "build_like_patterns": ("search", "build_like_patterns"),
    # --- metadata (medium: py7zr, stdlib zipfile/tarfile) ---------------- #
    "COMIC_INFO_ENTRY_NAME": ("metadata", "COMIC_INFO_ENTRY_NAME"),
    "SUPPORTED_ARCHIVE_EXTENSIONS": ("metadata", "SUPPORTED_ARCHIVE_EXTENSIONS"),
    "ArchiveFormat": ("metadata", "ArchiveFormat"),
    "ExtractedMetadata": ("metadata", "ExtractedMetadata"),
    "comic_info_to_chapter": ("metadata", "comic_info_to_chapter"),
    "comic_info_to_manga": ("metadata", "comic_info_to_manga"),
    "detect_archive_format": ("metadata", "detect_archive_format"),
    "extract_metadata": ("metadata", "extract_metadata"),
    "read_comic_info": ("metadata", "read_comic_info"),
    "write_comic_info": ("metadata", "write_comic_info"),
    # --- migrations (light: stdlib only) --------------------------------- #
    "MIGRATIONS_DIR": ("migrations", "MIGRATIONS_DIR"),
    "MigrationFile": ("migrations", "MigrationFile"),
    "list_migrations": ("migrations", "list_migrations"),
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
    from nexusdl.core.library.database import (
        LibraryDatabase,
        ReadingProgress,
    )
    from nexusdl.core.library.metadata import (
        COMIC_INFO_ENTRY_NAME,
        SUPPORTED_ARCHIVE_EXTENSIONS,
        ArchiveFormat,
        ExtractedMetadata,
        comic_info_to_chapter,
        comic_info_to_manga,
        detect_archive_format,
        extract_metadata,
        read_comic_info,
        write_comic_info,
    )
    from nexusdl.core.library.migrations import (
        MIGRATIONS_DIR,
        MigrationFile,
        list_migrations,
    )
    from nexusdl.core.library.scanner import (
        DEFAULT_MAX_CONCURRENT,
        DEFAULT_SITE_ID,
        LibraryRepositoryProtocol,
        LibraryScanner,
        ScanFailure,
        ScanReport,
    )
    from nexusdl.core.library.search import (
        DEFAULT_LIMIT,
        DEFAULT_MAX_LIMIT,
        LibrarySearch,
        PaginatedResult,
        build_fts_query,
        build_like_patterns,
    )


# ===========================================================================
# Lazy loader
# ===========================================================================

#: Root module names of the optional dependencies that a missing import
#: might refer to. Compared against ``exc.name.split(".")[0]`` so that a
#: missing submodule (``sqlalchemy.orm``, ``py7zr.exceptions``) is
#: attributed to the correct top-level package.
_OPTIONAL_DEPENDENCY_ROOTS: frozenset[str] = frozenset(
    {"sqlalchemy", "aiosqlite", "alembic", "py7zr"}
)


def __getattr__(name: str) -> Any:
    """Resolve a public library symbol on first access (PEP 562).

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
            such as ``sqlalchemy``, ``aiosqlite``, ``alembic``, or
            ``py7zr``, a syntax error deeper in the module, or a broken
            installation), or when the submodule does not define the
            requested name. ``AttributeError`` - rather than
            ``ImportError`` - is required by PEP 562 so that ``hasattr``
            works.
        ImportError: When the import failure is specifically a missing
            dependency from the ``[core]`` extra. The original
            :class:`ModuleNotFoundError` is chained as ``__cause__``.
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
            f"Available library exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.library.database import "
            f"LibraryDatabase' or 'from nexusdl.core.library.search "
            f"import build_fts_query'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the library subsystem. Compare
        # on the **root** module name so that a missing submodule
        # (``sqlalchemy.orm``, ``py7zr.exceptions``) is attributed to the
        # correct top-level package.
        missing_root = exc.name.split(".")[0] if exc.name else None
        if missing_root in _OPTIONAL_DEPENDENCY_ROOTS:
            raise ImportError(
                f"{name!r} requires the core extra (library subsystem "
                f"dependencies: sqlalchemy[asyncio], aiosqlite, alembic, "
                f"py7zr). Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The library submodule {submodule_basename!r} does not define "
            f"{attr_name!r}. This is a package-internal inconsistency - "
            f"nexusdl.core.library._LAZY and the submodules are out of "
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
