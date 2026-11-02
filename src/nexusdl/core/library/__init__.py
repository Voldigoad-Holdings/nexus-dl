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
"""

from __future__ import annotations

# --- Migrations (discovery only — the runner lives in database.py) --------- #
from nexusdl.core.library.migrations import (
    MIGRATIONS_DIR,
    MigrationFile,
    list_migrations,
)

# --- Metadata (archive I/O + domain conversion) ---------------------------- #
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

# --- Search (FTS5 + LIKE fallback; paginated results) ---------------------- #
from nexusdl.core.library.search import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_LIMIT,
    LibrarySearch,
    PaginatedResult,
    build_fts_query,
    build_like_patterns,
)

# --- Scanner (recursive import via LibraryRepositoryProtocol) ------------- #
from nexusdl.core.library.scanner import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_SITE_ID,
    LibraryRepositoryProtocol,
    LibraryScanner,
    ScanFailure,
    ScanReport,
)

# --- Database (the main entry point) --------------------------------------- #
from nexusdl.core.library.database import (
    LibraryDatabase,
    ReadingProgress,
)

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
]
