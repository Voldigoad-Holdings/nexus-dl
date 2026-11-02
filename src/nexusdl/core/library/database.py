"""Async SQLite-backed local library: CRUD, reading progress, migrations, search.

:class:`LibraryDatabase` is the single entry point to the local manga
library. It owns the SQLAlchemy async engine, the migration runner, and the
row-to-model mapping. Every other module in :mod:`nexusdl.core.library`
depends on it *through protocols* rather than by import, so this file is
the only one that talks to SQLite directly.

Architectural decisions
-----------------------
* **Raw SQL over ORM.** The schema lives in hand-written ``.sql`` migration
  files (``migrations/001_initial.sql``, ``migrations/002_add_reading_progress.sql``),
  and FTS5 has no first-class ORM mapping in SQLAlchemy anyway. Executing
  parameterized SQL via ``text()`` keeps a single source of truth for the
  schema and avoids the ORM's table-definition drift.
* **aiosqlite for migrations, SQLAlchemy for the runtime.** The migration
  runner uses a raw ``aiosqlite`` connection so it can call
  :meth:`aiosqlite.Connection.executescript` — which handles multi-statement
  SQL including the ``BEGIN ... END;`` trigger bodies in ``001_initial.sql``.
  SQLAlchemy's async API does not expose ``executescript`` and splitting
  statements by ``;`` breaks on triggers, so this is the correct boundary.
* **Per-connection PRAGMAs.** ``foreign_keys = ON`` is per-connection in
  SQLite; it is set via a ``connect`` event listener so every pooled
  connection gets it, not just the first.
* **In-memory SQLite is rejected.** The migration runner opens a separate
  connection from the runtime engine, and aiosqlite's ``:memory:`` storage
  is per-connection — the schema created by the runner would not be visible
  to the engine. Tests use ``tmp_path / "test.db"`` instead. The error
  message names the workaround.

Row-to-model contract
---------------------
The ``mangas`` table has every column the :class:`~nexusdl.core.models.manga.Manga`
model is expected to expose (``alt_titles``, ``artist``, ``cover_url``,
``cover_path``, ``tags``, ``page_count``, ``chapter_count``). If
``core/models/manga.py`` does not declare one of those, :meth:`_row_to_manga`
raises :class:`TypeError` on construction — a loud signal that the model
and schema have drifted. The reverse mapping in :meth:`add_manga` /
:meth:`update_manga` uses ``getattr(manga, field, default)`` so that a
model that lacks an optional attribute still persists the other fields.

Timestamps and JSON
-------------------
* Every timestamp column stores an ISO 8601 UTC string with a ``Z`` suffix
  (e.g. ``"2024-01-15T10:30:00.123456Z"``). This sorts lexicographically
  in SQLite's default TEXT collation, and round-trips unambiguously.
* ``alt_titles`` and ``tags`` are JSON-encoded TEXT arrays. They are read
  and written through :func:`_encode_json_array` and
  :func:`_decode_json_array`; ``NULL`` and ``"[]"`` both decode to ``[]``.

Dependency graph (no cycles)::

    core.exceptions                 ─┐
    core.library.migrations         ─┤
    core.library.search             ─┼──► core.library.database
    core.models.chapter             ─┤
    core.models.enums               ─┤
    core.models.manga               ─┘

This module is imported by the composition roots and by
:class:`~nexusdl.core.library.scanner.LibraryScanner` (through its
:class:`LibraryRepositoryProtocol`). It does not import any other
``core.library.*`` module except ``migrations`` and ``search``, which are
leaves.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import aiosqlite
from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexusdl.core.exceptions import LibraryError
from nexusdl.core.library.migrations import (
    MigrationFile,
    list_migrations,
)
from nexusdl.core.library.search import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_LIMIT,
    LibrarySearch,
    PaginatedResult,
)
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
from nexusdl.core.models.manga import Manga


__all__ = [
    "LibraryDatabase",
    "ReadingProgress",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Table used by the migration runner to record applied versions. Created by
#: the runner before any migration is applied; the migrations themselves
#: never touch it.
_SCHEMA_VERSION_TABLE: Final[str] = "schema_version"

#: Columns that :meth:`LibraryDatabase.update_manga` is allowed to modify.
#: ``id``, ``site_id``, ``source_id``, ``created_at``, ``last_read_at``,
#: ``page_count``, and ``chapter_count`` are excluded — the first four
#: define identity and are immutable, the last three are maintained by the
#: database itself (``page_count``/``chapter_count`` by :meth:`add_chapter`,
#: ``last_read_at`` by :meth:`set_reading_progress`).
_MANGA_UPDATABLE_FIELDS: Final[tuple[str, ...]] = (
    "title",
    "alt_titles",
    "description",
    "author",
    "artist",
    "status",
    "language",
    "content_rating",
    "is_manga",
    "year",
    "cover_url",
    "cover_path",
    "url",
    "tags",
)

#: Columns that :meth:`LibraryDatabase.add_manga` / :meth:`update_manga`
#: write, in the order they appear in the table. Kept as a tuple so the
#: INSERT statement and the params dict stay in lock-step — a unit test
#: iterates this tuple against the actual INSERT.
_MANGA_WRITE_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "site_id",
    "source_id",
    "title",
    "alt_titles",
    "description",
    "author",
    "artist",
    "status",
    "language",
    "content_rating",
    "is_manga",
    "year",
    "cover_url",
    "cover_path",
    "url",
    "tags",
    "page_count",
    "chapter_count",
    "created_at",
    "updated_at",
    "last_read_at",
)

#: Columns that :meth:`LibraryDatabase.add_chapter` writes. The manga's
#: counters are not here — they are recomputed or incremented in the same
#: transaction.
_CHAPTER_WRITE_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "manga_id",
    "source_id",
    "title",
    "number",
    "volume",
    "language",
    "page_count",
    "url",
    "published_at",
    "created_at",
    "updated_at",
)


_log = logger.bind(component="library.database")


# --------------------------------------------------------------------------- #
# Reading progress model                                                      #
# --------------------------------------------------------------------------- #


# NOTE: ``ReadingProgress`` logically belongs in ``core/models/library.py``.
# It is defined here because ``database.py`` is the only consumer today and
# ``models/library.py`` does not yet exist. When that module is written,
# move this class there and re-export it from ``database.py`` for backward
# compatibility.
class ReadingProgress:
    """One row of the ``reading_progress`` table.

    Immutable snapshot returned by :meth:`LibraryDatabase.get_reading_progress`.
    Carries everything a caller needs to resume reading: which chapter, at
    which page, and when.

    Attributes:
        manga_id: The manga the progress belongs to.
        chapter_id: The chapter currently being read.
        page_index: Zero-based page index within the chapter.
        updated_at: UTC timestamp of the last progress update.
    """

    __slots__ = ("chapter_id", "manga_id", "page_index", "updated_at")

    def __init__(
        self,
        manga_id: str,
        chapter_id: str,
        page_index: int,
        updated_at: datetime,
    ) -> None:
        self.manga_id = manga_id
        self.chapter_id = chapter_id
        self.page_index = page_index
        self.updated_at = updated_at

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ReadingProgress):
            return NotImplemented
        return (
            self.manga_id == other.manga_id
            and self.chapter_id == other.chapter_id
            and self.page_index == other.page_index
            and self.updated_at == other.updated_at
        )

    def __hash__(self) -> int:
        return hash(
            (self.manga_id, self.chapter_id, self.page_index, self.updated_at)
        )

    def __repr__(self) -> str:
        return (
            f"ReadingProgress(manga_id={self.manga_id!r}, "
            f"chapter_id={self.chapter_id!r}, "
            f"page_index={self.page_index}, "
            f"updated_at={self.updated_at.isoformat()!r})"
        )


# --------------------------------------------------------------------------- #
# LibraryDatabase                                                             #
# --------------------------------------------------------------------------- #


class LibraryDatabase:
    """Async SQLite-backed local library.

    See the module docstring for the full contract. One instance per
    process; the constructor does not perform I/O. Call :meth:`init` once
    during startup to apply migrations, and :meth:`close` during shutdown.

    Example:
        >>> db = LibraryDatabase(Path("~/.local/share/nexusdl/library.db"))
        >>> await db.init()
        >>> try:
        ...     await db.add_manga(manga)
        ...     page = await db.list_mangas(page=1, per_page=20)
        ... finally:
        ...     await db.close()
    """

    def __init__(self, database_path: Path | str) -> None:
        """Initialise the object without touching the filesystem.

        Args:
            database_path: Path to the SQLite database file. Created on
                :meth:`init` if it does not exist; its parent directory
                must exist by the time :meth:`init` is called. The
                special value ``":memory:"`` is rejected — see the module
                docstring for the reason and the workaround.

        Raises:
            ValueError: If ``database_path`` is ``":memory:"``. This
                restriction exists because the migration runner uses a
                separate connection from the runtime engine; with
                per-connection in-memory storage, the schema created by
                the runner would not be visible to the engine.
        """
        path_str = str(database_path)
        if path_str == ":memory:":
            raise ValueError(
                "LibraryDatabase does not support in-memory SQLite: the "
                "migration runner uses a separate connection from the "
                "runtime engine, and aiosqlite's :memory: storage is "
                "per-connection. Use a file path instead "
                "(e.g. tmp_path / 'test.db' in tests)."
            )
        self._database_path: Final[Path] = Path(path_str).resolve()
        self._engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self._database_path}",
            echo=False,
            future=True,
        )
        _install_sqlite_pragmas(self._engine)
        self._session_factory: async_sessionmaker[AsyncSession] = (
            async_sessionmaker(
                self._engine,
                expire_on_commit=False,
                class_=AsyncSession,
            )
        )
        self._search = LibrarySearch(
            session_factory=self.session,
            hydrate_manga=self._row_to_manga,
        )
        self._log = logger.bind(
            component="library.database",
            database=str(self._database_path),
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        """Absolute path to the SQLite database file."""
        return self._database_path

    async def init(self) -> None:
        """Create the database if needed, apply pending migrations.

        Idempotent: calling :meth:`init` twice is safe and cheap. The
        runner opens a raw aiosqlite connection to execute each pending
        migration script and then disposes it; the SQLAlchemy engine
        opens its own connections lazily afterwards.

        Raises:
            LibraryError: If a migration fails to apply. The failure is
                atomic — the whole migration's transaction rolls back and
                ``schema_version`` is not advanced, so the same migration
                will be retried on the next :meth:`init`.
        """
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        await self._bootstrap_schema_version()
        await self._apply_pending_migrations()

    async def close(self) -> None:
        """Dispose the engine and release all pooled connections.

        Safe to call more than once. After :meth:`close`, the database
        object should not be used; create a new instance if you need to
        reopen.
        """
        await self._engine.dispose()
        self._log.debug("LibraryDatabase closed")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield an async session, committing on success and rolling back on error.

        This is the only session factory used internally and the one
        passed to :class:`~nexusdl.core.library.search.LibrarySearch`.
        Commit is called at the end of the block regardless of whether
        the body issued any writes; a read-only transaction is cheap and
        keeps the contract simple.

        Yields:
            An :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
        """
        async with self._session_factory() as sess:
            try:
                yield sess
                await sess.commit()
            except Exception:
                await sess.rollback()
                raise

    # ------------------------------------------------------------------ #
    # Mangas — write                                                     #
    # ------------------------------------------------------------------ #

    async def add_manga(self, manga: Manga) -> Manga:
        """Insert or update *manga*.

        The upsert is keyed on ``manga.id``. On conflict, every
        caller-editable field (title, description, status, …) is
        refreshed from *manga*; the DB-managed counters
        (``page_count``, ``chapter_count``), ``created_at``, and
        ``last_read_at`` are **preserved** — they belong to the database,
        not to the caller.

        The returned :class:`Manga` is the input object unchanged. If a
        caller needs the persisted state (e.g. refreshed ``updated_at``),
        it should call :meth:`get_manga` afterwards.

        Args:
            manga: The manga to persist. ``manga.id`` must be set.

        Returns:
            *manga*, unmodified.

        Raises:
            LibraryError: If a unique constraint is violated in a way
                other than the intended id conflict (e.g. two rows with
                the same ``(site_id, source_id)`` under different ids —
                a hash collision, which should be investigated).
        """
        now = _utcnow_iso()
        values = _manga_to_row(manga, now)
        async with self.session() as sess:
            try:
                await sess.execute(
                    text(_MANGA_UPSERT_SQL), values
                )
            except IntegrityError as exc:
                raise LibraryError(
                    f"cannot upsert manga {manga.id!r}: {exc}"
                ) from exc
        self._log.debug("Upserted manga {} ({!r})", manga.id, manga.title)
        return manga

    async def update_manga(self, manga: Manga) -> Manga:
        """Update an existing manga; raise if it does not exist.

        Only the fields in :data:`_MANGA_UPDATABLE_FIELDS` are written.
        ``created_at``, ``last_read_at``, ``page_count``, and
        ``chapter_count`` are preserved. ``updated_at`` is refreshed to
        the current UTC time.

        Args:
            manga: The manga to update. Must have a valid ``id`` that
                exists in the database.

        Returns:
            *manga*, unmodified.

        Raises:
            LibraryError: If no row with ``manga.id`` exists, or the
                update violates a constraint.
        """
        now = _utcnow_iso()
        values = _manga_to_row(manga, now)
        async with self.session() as sess:
            existing = await sess.execute(
                text("SELECT 1 FROM mangas WHERE id = :id"),
                {"id": manga.id},
            )
            if existing.scalar_one_or_none() is None:
                raise LibraryError(f"manga not found: {manga.id}")
            try:
                await sess.execute(
                    text(_MANGA_UPDATE_SQL), values
                )
            except IntegrityError as exc:
                raise LibraryError(
                    f"cannot update manga {manga.id!r}: {exc}"
                ) from exc
        self._log.debug("Updated manga {}", manga.id)
        return manga

    async def delete_manga(
        self, manga_id: str, *, delete_files: bool = False
    ) -> bool:
        """Delete a manga, its chapters, and its reading progress.

        Cascade is enforced at the database level (``ON DELETE CASCADE``
        from ``chapters`` to ``mangas``, and from ``reading_progress`` to
        ``chapters``), so a single ``DELETE FROM mangas`` removes
        everything.

        Args:
            manga_id: The manga to delete.
            delete_files: When ``True``, also delete on-disk artefacts
                referenced by this manga: its ``cover_path`` (if any) and
                any chapter whose ``url`` starts with ``file://``
                (typically locally-scanned archives). Failures to delete
                individual files are logged as warnings and do not abort
                the operation — a missing file is not an error.

        Returns:
            ``True`` iff a row was deleted. ``False`` for an unknown id.
        """
        file_paths: list[Path] = []
        async with self.session() as sess:
            if delete_files:
                cover_row = await sess.execute(
                    text("SELECT cover_path FROM mangas WHERE id = :id"),
                    {"id": manga_id},
                )
                cover = cover_row.scalar_one_or_none()
                if cover:
                    file_paths.append(Path(cover))
                url_rows = await sess.execute(
                    text(
                        "SELECT url FROM chapters WHERE manga_id = :id "
                        "AND url LIKE 'file://%'"
                    ),
                    {"id": manga_id},
                )
                for (url,) in url_rows.all():
                    file_paths.append(_file_url_to_path(url))

            result = await sess.execute(
                text("DELETE FROM mangas WHERE id = :id"),
                {"id": manga_id},
            )
            deleted = result.rowcount > 0

        if deleted and delete_files:
            for path in file_paths:
                _delete_file_quietly(path)

        if deleted:
            self._log.info(
                "Deleted manga {} ({} on-disk file(s) removed)",
                manga_id,
                len(file_paths) if delete_files else 0,
            )
        return deleted

    # ------------------------------------------------------------------ #
    # Mangas — read                                                      #
    # ------------------------------------------------------------------ #

    async def get_manga(self, manga_id: str) -> Manga | None:
        """Return the manga with *manga_id*, or ``None``."""
        async with self.session() as sess:
            result = await sess.execute(
                text("SELECT * FROM mangas WHERE id = :id"),
                {"id": manga_id},
            )
            row = result.mappings().first()
        return None if row is None else self._row_to_manga(dict(row))

    async def list_mangas(
        self, page: int = 1, per_page: int = 50
    ) -> PaginatedResult[Manga]:
        """Return one page of the library, ordered by recency.

        Ordering puts mangas that have been read most recently first;
        never-opened mangas follow, ordered by creation time descending.
        ``id`` breaks ties so pagination is stable across queries.

        Args:
            page: 1-indexed page number. Must be ``>= 1``.
            per_page: Page size, in ``[1, DEFAULT_MAX_LIMIT]``.

        Returns:
            A :class:`~nexusdl.core.library.search.PaginatedResult`.

        Raises:
            ValueError: If ``page`` or ``per_page`` is out of range.
        """
        if page < 1:
            raise ValueError(f"page must be >= 1, got {page}")
        if not (1 <= per_page <= DEFAULT_MAX_LIMIT):
            raise ValueError(
                f"per_page must be in [1, {DEFAULT_MAX_LIMIT}], "
                f"got {per_page}"
            )
        offset = (page - 1) * per_page
        async with self.session() as sess:
            total = int(
                (
                    await sess.execute(text("SELECT COUNT(*) FROM mangas"))
                ).scalar_one()
            )
            if total == 0:
                return PaginatedResult(
                    items=(),
                    total=0,
                    page=page,
                    per_page=per_page,
                    total_pages=0,
                )
            rows = (
                (
                    await sess.execute(
                        text(
                            "SELECT * FROM mangas "
                            "ORDER BY (last_read_at IS NULL) ASC, "
                            "         last_read_at DESC, "
                            "         created_at DESC, "
                            "         id ASC "
                            "LIMIT :limit OFFSET :offset"
                        ),
                        {"limit": per_page, "offset": offset},
                    )
                )
                .mappings()
                .all()
            )
        return PaginatedResult(
            items=tuple(self._row_to_manga(dict(r)) for r in rows),
            total=total,
            page=page,
            per_page=per_page,
            total_pages=(total + per_page - 1) // per_page,
        )

    async def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> PaginatedResult[Manga]:
        """Full-text search, delegating to :class:`LibrarySearch`.

        See :meth:`LibrarySearch.search` for the full contract, including
        the FTS5-vs-LIKE fallback and the user query syntax.

        Args:
            query: Raw user query string.
            limit: Page size, in ``[1, DEFAULT_MAX_LIMIT]``.
            offset: Zero-based offset into the result set.

        Returns:
            A :class:`~nexusdl.core.library.search.PaginatedResult`.
        """
        return await self._search.search(query, limit=limit, offset=offset)

    # ------------------------------------------------------------------ #
    # Chapters                                                           #
    # ------------------------------------------------------------------ #

    async def add_chapter(self, chapter: Chapter) -> Chapter:
        """Insert or update *chapter*, maintaining the manga's counters.

        On **insert**, the parent manga's ``chapter_count`` and
        ``page_count`` are incremented in the same transaction. On
        **update**, both counters are recomputed from scratch to remain
        consistent if a chapter's ``page_count`` was corrected.

        Args:
            chapter: The chapter to persist. ``chapter.id`` and
                ``chapter.manga_id`` must be set; the manga must exist.

        Returns:
            *chapter*, unmodified.

        Raises:
            LibraryError: If the parent manga does not exist, or the
                upsert violates a constraint.
        """
        now = _utcnow_iso()
        values = _chapter_to_row(chapter, now)
        async with self.session() as sess:
            existing = await sess.execute(
                text("SELECT 1 FROM chapters WHERE id = :id"),
                {"id": chapter.id},
            )
            is_new = existing.scalar_one_or_none() is None

            try:
                await sess.execute(text(_CHAPTER_UPSERT_SQL), values)
            except IntegrityError as exc:
                raise LibraryError(
                    f"cannot upsert chapter {chapter.id!r}: {exc}"
                ) from exc

            if is_new:
                await sess.execute(
                    text(
                        "UPDATE mangas SET "
                        "chapter_count = chapter_count + 1, "
                        "page_count = page_count + :pc, "
                        "updated_at = :now "
                        "WHERE id = :mid"
                    ),
                    {
                        "pc": chapter.page_count,
                        "now": now,
                        "mid": chapter.manga_id,
                    },
                )
            else:
                await sess.execute(
                    text(
                        "UPDATE mangas SET "
                        "chapter_count = ("
                        "  SELECT COUNT(*) FROM chapters "
                        "  WHERE manga_id = :mid), "
                        "page_count = ("
                        "  SELECT COALESCE(SUM(page_count), 0) "
                        "  FROM chapters WHERE manga_id = :mid), "
                        "updated_at = :now "
                        "WHERE id = :mid"
                    ),
                    {"mid": chapter.manga_id, "now": now},
                )
        self._log.debug(
            "Upserted chapter {} for manga {} ({})",
            chapter.id,
            chapter.manga_id,
            "insert" if is_new else "update",
        )
        return chapter

    async def get_chapter(self, chapter_id: str) -> Chapter | None:
        """Return the chapter with *chapter_id*, or ``None``."""
        async with self.session() as sess:
            result = await sess.execute(
                text("SELECT * FROM chapters WHERE id = :id"),
                {"id": chapter_id},
            )
            row = result.mappings().first()
        return None if row is None else self._row_to_chapter(dict(row))

    async def get_chapters(self, manga_id: str) -> list[Chapter]:
        """Return every chapter of *manga_id*, in reading order.

        Ordering handles numeric and non-numeric chapter numbers:

        * Numeric numbers (``"1"``, ``"10.5"``) sort first, by numeric
          value.
        * Non-numeric numbers (``"SP1"``, ``"Omake 3"``) follow, sorted
          lexicographically.
        * NULL numbers sort last.
        * ``id`` breaks ties so pagination is stable.

        Args:
            manga_id: The manga whose chapters to return.

        Returns:
            A list of :class:`~nexusdl.core.models.chapter.Chapter`,
            possibly empty.
        """
        async with self.session() as sess:
            result = await sess.execute(
                text(
                    "SELECT * FROM chapters "
                    "WHERE manga_id = :mid "
                    "ORDER BY "
                    "  (number IS NULL) ASC, "
                    "  (number GLOB '[0-9]*' = 0) ASC, "
                    "  (number GLOB '[0-9]*') DESC, "
                    "  CASE WHEN number GLOB '[0-9]*' "
                    "       THEN CAST(number AS REAL) ELSE 0 END ASC, "
                    "  number ASC, "
                    "  id ASC"
                ),
                {"mid": manga_id},
            )
            rows = result.mappings().all()
        return [self._row_to_chapter(dict(r)) for r in rows]

    # ------------------------------------------------------------------ #
    # Reading progress                                                   #
    # ------------------------------------------------------------------ #

    async def set_reading_progress(
        self,
        manga_id: str,
        chapter_id: str,
        page_index: int,
    ) -> None:
        """Persist the current reading position.

        Atomically upserts the ``reading_progress`` row and updates the
        manga's ``last_read_at``. Both checks and both writes happen in
        one transaction, so a failure leaves neither table modified.

        Args:
            manga_id: The manga the progress belongs to.
            chapter_id: The chapter currently being read. Must belong to
                *manga_id* — enforced by the composite foreign key on
                ``reading_progress``.
            page_index: Zero-based page index. Must be ``>= 0`` and
                ``< chapter.page_count``; upper-bound violations are the
                caller's responsibility (the schema only checks ``>= 0``).

        Raises:
            LibraryError: If the manga does not exist, or the chapter does
                not exist or belongs to a different manga. If
                ``page_index`` is negative.
        """
        if page_index < 0:
            raise LibraryError(
                f"page_index must be >= 0, got {page_index}"
            )
        now = _utcnow_iso()
        async with self.session() as sess:
            manga_exists = await sess.execute(
                text("SELECT 1 FROM mangas WHERE id = :id"),
                {"id": manga_id},
            )
            if manga_exists.scalar_one_or_none() is None:
                raise LibraryError(f"manga not found: {manga_id}")

            chapter_ok = await sess.execute(
                text(
                    "SELECT 1 FROM chapters "
                    "WHERE id = :cid AND manga_id = :mid"
                ),
                {"cid": chapter_id, "mid": manga_id},
            )
            if chapter_ok.scalar_one_or_none() is None:
                raise LibraryError(
                    f"chapter {chapter_id!r} not found in manga "
                    f"{manga_id!r}"
                )

            await sess.execute(
                text(
                    "INSERT INTO reading_progress "
                    "  (manga_id, chapter_id, page_index, updated_at) "
                    "VALUES (:mid, :cid, :pi, :now) "
                    "ON CONFLICT(manga_id) DO UPDATE SET "
                    "  chapter_id = excluded.chapter_id, "
                    "  page_index = excluded.page_index, "
                    "  updated_at = excluded.updated_at"
                ),
                {
                    "mid": manga_id,
                    "cid": chapter_id,
                    "pi": page_index,
                    "now": now,
                },
            )

            await sess.execute(
                text(
                    "UPDATE mangas SET last_read_at = :now "
                    "WHERE id = :mid"
                ),
                {"mid": manga_id, "now": now},
            )

    async def get_reading_progress(
        self, manga_id: str
    ) -> ReadingProgress | None:
        """Return the current reading position for *manga_id*, or ``None``.

        Args:
            manga_id: The manga whose progress to fetch.
        """
        async with self.session() as sess:
            result = await sess.execute(
                text(
                    "SELECT manga_id, chapter_id, page_index, updated_at "
                    "FROM reading_progress WHERE manga_id = :mid"
                ),
                {"mid": manga_id},
            )
            row = result.mappings().first()
        if row is None:
            return None
        return ReadingProgress(
            manga_id=row["manga_id"],
            chapter_id=row["chapter_id"],
            page_index=int(row["page_index"]),
            updated_at=_parse_iso(row["updated_at"]),
        )

    # ------------------------------------------------------------------ #
    # Row-to-model mappers (semi-public — used by LibrarySearch)         #
    # ------------------------------------------------------------------ #

    def _row_to_manga(self, row: Mapping[str, Any]) -> Manga:
        """Map a row from the ``mangas`` table to a :class:`Manga`.

        Public convention: pass this method to any code that hydrates
        manga rows from raw SQL. It is the single source of truth for the
        column-to-model mapping.

        Args:
            row: A mapping (dict or SQLAlchemy ``RowMapping``) with the
                columns of the ``mangas`` table.

        Returns:
            A :class:`Manga` instance.
        """
        return Manga(
            id=row["id"],
            site_id=row["site_id"],
            source_id=row["source_id"],
            title=row["title"],
            alt_titles=_decode_json_array(row.get("alt_titles")),
            description=row.get("description"),
            author=row.get("author"),
            artist=row.get("artist"),
            status=MangaStatus(row["status"]),
            language=Language(row["language"]),
            content_rating=ContentRating(row["content_rating"]),
            is_manga=bool(row["is_manga"]),
            year=row.get("year"),
            cover_url=row.get("cover_url"),
            cover_path=(
                Path(row["cover_path"]) if row.get("cover_path") else None
            ),
            url=row["url"],
            tags=_decode_json_array(row.get("tags")),
            page_count=int(row.get("page_count", 0)),
            chapter_count=int(row.get("chapter_count", 0)),
        )

    @staticmethod
    def _row_to_chapter(row: Mapping[str, Any]) -> Chapter:
        """Map a row from the ``chapters`` table to a :class:`Chapter`.

        Args:
            row: A mapping with the columns of the ``chapters`` table.

        Returns:
            A :class:`Chapter` instance.
        """
        return Chapter(
            id=row["id"],
            manga_id=row["manga_id"],
            source_id=row["source_id"],
            title=row["title"],
            number=row.get("number"),
            volume=row.get("volume"),
            language=Language(row["language"]),
            page_count=int(row["page_count"]),
            url=row["url"],
        )

    # ------------------------------------------------------------------ #
    # Migration runner                                                   #
    # ------------------------------------------------------------------ #

    async def _bootstrap_schema_version(self) -> None:
        """Create ``schema_version`` if it does not exist.

        Run before any migration, because migration files do not assume
        the table exists. Idempotent.
        """
        async with aiosqlite.connect(str(self._database_path)) as db:
            await db.execute(
                f"CREATE TABLE IF NOT EXISTS {_SCHEMA_VERSION_TABLE} ("
                "  version    INTEGER PRIMARY KEY, "
                "  applied_at TEXT    NOT NULL"
                ")"
            )
            await db.commit()

    async def _apply_pending_migrations(self) -> None:
        """Apply every migration whose version exceeds the current one.

        Each migration runs in its own connection and its own explicit
        ``BEGIN ... COMMIT`` block, with the ``schema_version`` insert
        included *inside* the same script. A failure rolls back both the
        schema change and the version record, so the same migration is
        retried on the next :meth:`init`.
        """
        async with aiosqlite.connect(str(self._database_path)) as db:
            async with db.execute(
                f"SELECT COALESCE(MAX(version), 0) FROM {_SCHEMA_VERSION_TABLE}"
            ) as cursor:
                row = await cursor.fetchone()
        current = int(row[0]) if row is not None else 0

        migrations = list_migrations()
        pending = [m for m in migrations if m.version > current]
        if not pending:
            self._log.debug(
                "Schema up to date at version {}", current
            )
            return

        for migration in pending:
            await self._apply_one_migration(migration)

    async def _apply_one_migration(self, migration: MigrationFile) -> None:
        """Apply a single migration file atomically with its version record.

        Args:
            migration: The migration to apply.

        Raises:
            LibraryError: If the SQL fails. The whole script's effects are
                rolled back, so the migration can be retried after the
                caller fixes the cause.
        """
        sql = migration.path.read_text(encoding="utf-8")
        applied_at = _utcnow_iso()
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            f"INSERT INTO {_SCHEMA_VERSION_TABLE} (version, applied_at) "
            f"VALUES ({migration.version}, '{applied_at}');\n"
            "COMMIT;"
        )

        try:
            async with aiosqlite.connect(str(self._database_path)) as db:
                await db.executescript(script)
        except (aiosqlite.Error, OSError) as exc:
            # sqlite3 aborts the current transaction on error, so the
            # schema changes and the version insert are both discarded
            # when the connection closes. Nothing to clean up here.
            self._log.exception(
                "Migration {} ({}) failed: {}",
                migration.version,
                migration.path.name,
                exc,
            )
            raise LibraryError(
                f"migration {migration.version} "
                f"({migration.path.name}) failed: {exc}"
            ) from exc

        self._log.info(
            "Applied migration {} ({})",
            migration.version,
            migration.path.name,
        )


# --------------------------------------------------------------------------- #
# SQL statement constants                                                     #
# --------------------------------------------------------------------------- #
# Defined as module-level strings so they are compiled once and so the
# column lists are visible without scrolling through the methods. Kept in
# lock-step with :data:`_MANGA_WRITE_FIELDS` and :data:`_CHAPTER_WRITE_FIELDS`
# by unit tests iterating both.
# --------------------------------------------------------------------------- #

_MANGA_UPSERT_SQL: Final[str] = (
    "INSERT INTO mangas ("
    "  id, site_id, source_id, title, alt_titles, description, author, "
    "  artist, status, language, content_rating, is_manga, year, "
    "  cover_url, cover_path, url, tags, page_count, chapter_count, "
    "  created_at, updated_at, last_read_at"
    ") VALUES ("
    "  :id, :site_id, :source_id, :title, :alt_titles, :description, "
    "  :author, :artist, :status, :language, :content_rating, :is_manga, "
    "  :year, :cover_url, :cover_path, :url, :tags, :page_count, "
    "  :chapter_count, :created_at, :updated_at, :last_read_at"
    ") ON CONFLICT(id) DO UPDATE SET "
    "  title = excluded.title, "
    "  alt_titles = excluded.alt_titles, "
    "  description = excluded.description, "
    "  author = excluded.author, "
    "  artist = excluded.artist, "
    "  status = excluded.status, "
    "  language = excluded.language, "
    "  content_rating = excluded.content_rating, "
    "  is_manga = excluded.is_manga, "
    "  year = excluded.year, "
    "  cover_url = excluded.cover_url, "
    "  cover_path = excluded.cover_path, "
    "  url = excluded.url, "
    "  tags = excluded.tags, "
    "  updated_at = excluded.updated_at"
)

_MANGA_UPDATE_SQL: Final[str] = (
    "UPDATE mangas SET "
    + ", ".join(f"{f} = :{f}" for f in _MANGA_UPDATABLE_FIELDS)
    + ", updated_at = :updated_at "
    "WHERE id = :id"
)

_CHAPTER_UPSERT_SQL: Final[str] = (
    "INSERT INTO chapters ("
    "  id, manga_id, source_id, title, number, volume, language, "
    "  page_count, url, published_at, created_at, updated_at"
    ") VALUES ("
    "  :id, :manga_id, :source_id, :title, :number, :volume, :language, "
    "  :page_count, :url, :published_at, :created_at, :updated_at"
    ") ON CONFLICT(id) DO UPDATE SET "
    "  title = excluded.title, "
    "  number = excluded.number, "
    "  volume = excluded.volume, "
    "  language = excluded.language, "
    "  page_count = excluded.page_count, "
    "  url = excluded.url, "
    "  published_at = excluded.published_at, "
    "  updated_at = excluded.updated_at"
)


# --------------------------------------------------------------------------- #
# Module-level helpers                                                       #
# --------------------------------------------------------------------------- #


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Attach a ``connect`` listener that enables the required PRAGMAs.

    ``foreign_keys`` is per-connection in SQLite; without this listener,
    cascade-delete silently becomes a no-op on any pooled connection that
    was not explicitly initialised. ``WAL`` improves concurrent-read
    behaviour at negligible cost; ``busy_timeout`` prevents transient
    ``database is locked`` errors during long transactions.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
        finally:
            cursor.close()


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(value: str | None) -> datetime:
    """Parse an ISO 8601 string produced by :func:`_utcnow_iso` back to UTC.

    Accepts both ``Z`` and ``+00:00`` suffixes. A ``None`` or malformed
    value is treated as the Unix epoch — never expected in practice, but
    more useful than raising in a mapper.
    """
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _log.warning("Malformed timestamp in database: {!r}", value)
        return datetime.fromtimestamp(0, tz=UTC)


def _encode_json_array(items: Sequence[Any] | None) -> str | None:
    """Serialize *items* to JSON array TEXT; ``None`` for empty/absent."""
    if not items:
        return None
    try:
        return json.dumps(list(items), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        _log.warning(
            "Could not serialize JSON array ({}); storing NULL", exc
        )
        return None


def _decode_json_array(raw: str | None) -> list[str]:
    """Parse a JSON array TEXT back to a list of strings. Never raises."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _manga_to_row(manga: Manga, now: str) -> dict[str, Any]:
    """Build the parameter dict for :data:`_MANGA_UPSERT_SQL` / ``_UPDATE_SQL``.

    Uses ``getattr`` defensively for fields the model may not expose.
    Required fields (id, site_id, source_id, title, url) are accessed
    directly — a model missing them is a hard error.
    """
    created_at = getattr(manga, "created_at", None)
    updated_at = getattr(manga, "updated_at", None)
    last_read_at = getattr(manga, "last_read_at", None)
    return {
        "id": manga.id,
        "site_id": manga.site_id,
        "source_id": manga.source_id,
        "title": manga.title,
        "alt_titles": _encode_json_array(
            getattr(manga, "alt_titles", None)
        ),
        "description": getattr(manga, "description", None),
        "author": getattr(manga, "author", None),
        "artist": getattr(manga, "artist", None),
        "status": manga.status.value,
        "language": manga.language.value,
        "content_rating": manga.content_rating.value,
        "is_manga": 1 if manga.is_manga else 0,
        "year": getattr(manga, "year", None),
        "cover_url": _string_or_none(getattr(manga, "cover_url", None)),
        "cover_path": _string_or_none(getattr(manga, "cover_path", None)),
        "url": manga.url,
        "tags": _encode_json_array(getattr(manga, "tags", None)),
        "page_count": int(getattr(manga, "page_count", 0)),
        "chapter_count": int(getattr(manga, "chapter_count", 0)),
        "created_at": _timestamp_or_now(created_at, now),
        "updated_at": now,
        "last_read_at": _string_or_none(last_read_at),
    }


def _chapter_to_row(chapter: Chapter, now: str) -> dict[str, Any]:
    """Build the parameter dict for :data:`_CHAPTER_UPSERT_SQL`."""
    created_at = getattr(chapter, "created_at", None)
    return {
        "id": chapter.id,
        "manga_id": chapter.manga_id,
        "source_id": chapter.source_id,
        "title": chapter.title,
        "number": getattr(chapter, "number", None),
        "volume": getattr(chapter, "volume", None),
        "language": chapter.language.value,
        "page_count": int(chapter.page_count),
        "url": chapter.url,
        "published_at": _string_or_none(
            getattr(chapter, "published_at", None)
        ),
        "created_at": _timestamp_or_now(created_at, now),
        "updated_at": now,
    }


def _timestamp_or_now(
    value: datetime | str | None, fallback: str
) -> str:
    """Coerce *value* to an ISO string; use *fallback* when absent/invalid."""
    if value is None:
        return fallback
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # Assume string already in ISO form; store verbatim.
    return str(value)


def _string_or_none(value: Any) -> str | None:
    """Coerce *value* to a string, mapping ``None`` to ``None``."""
    if value is
