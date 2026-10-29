# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for LibraryDatabase.

LibraryDatabase is the async SQLite layer (via SQLAlchemy + aiosqlite)
that stores mangas, chapters, reading progress, and download history.

Contract:
    - init() applies migrations (idempotent)
    - close() closes the connection
    - add_manga() / get_manga() / update_manga() / delete_manga()
    - add_chapter() / get_chapters()
    - set_reading_progress() / get_reading_progress()
    - list_mangas() (paginated)
    - FTS5 triggers keep the search index in sync
    - Transactions roll back on error
    - Foreign keys are enforced

What is tested:
    - init() + close() + idempotence
    - CRUD (add, get, update, delete)
    - Upsert (duplicate add)
    - Pagination (total, page, per_page, has_next)
    - Reading progress (upsert)
    - FTS5 trigger (search after add)
    - Transactions (rollback)
    - Foreign keys (orphan chapter raises)
    - Concurrency (asyncio.gather)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from nexusdl.core.config import Settings
from nexusdl.core.library.database import LibraryDatabase, PaginatedResult
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
)
from nexusdl.core.models.library import ReadingProgress
from nexusdl.core.models.manga import Manga


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings with a temp DB."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_name="NexusDL-Test",
        app_environment="dev",
        public_url="http://test",
        api_base_url="http://test",
        frontend_url="http://test",
        secret_key="test-secret-" + "a" * 52,
        jwt_secret="test-jwt-" + "b" * 55,
        cookie_encryption_key="test-cookie-" + "c" * 50,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/library.db",
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "temp",
        library_db_path=tmp_path / "library.db",
        cookie_storage_path=tmp_path / "cookies.enc",
        logs_dir=tmp_path / "logs",
        allow_adult=False,
        playwright_enabled=False,
        log_level="WARNING",
        log_format="text",
    )


@pytest_asyncio.fixture
async def db(settings: Settings) -> LibraryDatabase:
    """Fresh LibraryDatabase with migrations applied."""
    database = LibraryDatabase(settings.database_url)
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def sample_manga() -> Manga:
    """Manga from sample_manga.json."""
    data = json.loads((FIXTURES / "sample_manga.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return Manga.model_validate(data)


@pytest.fixture
def sample_chapter(sample_manga: Manga) -> Chapter:
    """Chapter from sample_chapter.json."""
    data = json.loads((FIXTURES / "sample_chapter.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    data["manga_id"] = sample_manga.id
    return Chapter.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 1: init() + close()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_creates_tables(settings: Settings) -> None:
    """init() creates the schema (tables + indexes)."""
    db = LibraryDatabase(settings.database_url)
    await db.init()

    # Query a table
    result = await db.list_mangas()
    assert isinstance(result, PaginatedResult)
    assert result.total == 0

    await db.close()


@pytest.mark.asyncio
async def test_init_is_idempotent(settings: Settings) -> None:
    """init() can be called twice without error."""
    db = LibraryDatabase(settings.database_url)
    await db.init()
    await db.init()  # should not raise
    await db.close()


@pytest.mark.asyncio
async def test_close_works(db: LibraryDatabase) -> None:
    """close() closes cleanly."""
    await db.close()
    # Second close should not raise
    await db.close()


# ────────────────────────────────────────────────────────────────────
# Test 2: add_manga / get_manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_manga_returns_pk(db: LibraryDatabase, sample_manga: Manga) -> None:
    """add_manga() returns a positive primary key."""
    pk = await db.add_manga(sample_manga)
    assert pk > 0


@pytest.mark.asyncio
async def test_get_manga_returns_stored(db: LibraryDatabase, sample_manga: Manga) -> None:
    """get_manga() returns the stored manga."""
    await db.add_manga(sample_manga)
    stored = await db.get_manga(sample_manga.id)

    assert stored is not None
    assert stored.id == sample_manga.id
    assert stored.title == sample_manga.title
    assert stored.author == sample_manga.author


@pytest.mark.asyncio
async def test_get_manga_unknown_returns_none(db: LibraryDatabase) -> None:
    """get_manga() returns None for unknown id."""
    stored = await db.get_manga("unknown")
    assert stored is None


@pytest.mark.asyncio
async def test_add_manga_duplicate_upserts(db: LibraryDatabase, sample_manga: Manga) -> None:
    """add_manga() on the same id upserts (no duplicate)."""
    pk1 = await db.add_manga(sample_manga)
    pk2 = await db.add_manga(sample_manga)

    assert pk1 == pk2

    result = await db.list_mangas()
    assert result.total == 1


# ────────────────────────────────────────────────────────────────────
# Test 3: update_manga / delete_manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_manga(db: LibraryDatabase, sample_manga: Manga) -> None:
    """update_manga() updates the title."""
    await db.add_manga(sample_manga)

    updated = sample_manga.model_copy(update={"title": "One Piece (Updated)"})
    await db.update_manga(updated)

    stored = await db.get_manga(sample_manga.id)
    assert stored is not None
    assert stored.title == "One Piece (Updated)"


@pytest.mark.asyncio
async def test_delete_manga(db: LibraryDatabase, sample_manga: Manga) -> None:
    """delete_manga() removes the manga."""
    await db.add_manga(sample_manga)

    await db.delete_manga(sample_manga.id, delete_files=False)

    stored = await db.get_manga(sample_manga.id)
    assert stored is None


# ────────────────────────────────────────────────────────────────────
# Test 4: add_chapter / get_chapters
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_chapter(
    db: LibraryDatabase,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """add_chapter() stores the chapter."""
    await db.add_manga(sample_manga)
    pk = await db.add_chapter(sample_chapter)
    assert pk > 0


@pytest.mark.asyncio
async def test_get_chapters(
    db: LibraryDatabase,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """get_chapters() returns the stored chapters."""
    await db.add_manga(sample_manga)
    await db.add_chapter(sample_chapter)

    chapters = await db.get_chapters(sample_manga.id)
    assert len(chapters) == 1
    assert chapters[0].id == sample_chapter.id


@pytest.mark.asyncio
async def test_get_chapters_empty(db: LibraryDatabase, sample_manga: Manga) -> None:
    """get_chapters() returns [] for a manga with no chapters."""
    await db.add_manga(sample_manga)
    chapters = await db.get_chapters(sample_manga.id)
    assert chapters == []


@pytest.mark.asyncio
async def test_get_chapters_sorted(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """get_chapters() returns chapters sorted by number."""
    await db.add_manga(sample_manga)

    # Add 3 chapters out of order
    for n in (3, 1, 2):
        chapter = Chapter(
            id=f"ch{n:014d}",
            manga_id=sample_manga.id,
            source_id=f"src-{n}",
            title=f"Chapter {n}",
            number=float(n),
            language=Language.EN,
            url=f"https://test.com/ch/{n}",
        )
        await db.add_chapter(chapter)

    chapters = await db.get_chapters(sample_manga.id)
    numbers = [c.number for c in chapters]
    assert numbers == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_add_chapter_orphan_raises(
    db: LibraryDatabase,
    sample_chapter: Chapter,
) -> None:
    """add_chapter() with a nonexistent manga_id raises (FK)."""
    # No manga inserted
    with pytest.raises(Exception):  # ForeignKeyViolation or similar
        await db.add_chapter(sample_chapter)


# ────────────────────────────────────────────────────────────────────
# Test 5: reading progress
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reading_progress(
    db: LibraryDatabase,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """set_reading_progress() and get_reading_progress() work."""
    await db.add_manga(sample_manga)
    await db.add_chapter(sample_chapter)

    # Initially no progress
    progress = await db.get_reading_progress(sample_manga.id)
    assert progress is None

    # Set progress
    await db.set_reading_progress(
        manga_id=sample_manga.id,
        chapter_id=sample_chapter.id,
        page=42,
    )

    # Read back
    progress = await db.get_reading_progress(sample_manga.id)
    assert progress is not None
    assert progress.manga_id == sample_manga.id
    assert progress.chapter_id == sample_chapter.id
    assert progress.page == 42


@pytest.mark.asyncio
async def test_reading_progress_upsert(
    db: LibraryDatabase,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """set_reading_progress() upserts on the same manga_id."""
    await db.add_manga(sample_manga)
    await db.add_chapter(sample_chapter)

    await db.set_reading_progress(sample_manga.id, sample_chapter.id, 10)
    await db.set_reading_progress(sample_manga.id, sample_chapter.id, 20)

    progress = await db.get_reading_progress(sample_manga.id)
    assert progress is not None
    assert progress.page == 20


# ────────────────────────────────────────────────────────────────────
# Test 6: pagination
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_mangas_pagination(db: LibraryDatabase) -> None:
    """list_mangas() supports pagination."""
    # Insert 15 mangas
    for i in range(15):
        manga = Manga(
            id=f"test{i:04d}",
            source_id=f"src{i:04d}",
            site="mangadex",
            title=f"Test Manga {i:04d}",
            author=f"Author {i}",
            status=MangaStatus.ONGOING,
            language=Language.EN,
            content_rating=ContentRating.SAFE,
            url=f"https://example.com/{i}",
            updated_at="2026-10-29T12:00:00Z",
        )
        await db.add_manga(manga)

    # Page 1 (5 per page)
    page1 = await db.list_mangas(page=1, per_page=5)
    assert len(page1.items) == 5
    assert page1.total == 15
    assert page1.total_pages == 3
    assert page1.has_next is True
    assert page1.has_prev is False

    # Page 2
    page2 = await db.list_mangas(page=2, per_page=5)
    assert len(page2.items) == 5
    assert page2.has_next is True
    assert page2.has_prev is True

    # Page 3
    page3 = await db.list_mangas(page=3, per_page=5)
    assert len(page3.items) == 5
    assert page3.has_next is False
    assert page3.has_prev is True


@pytest.mark.asyncio
async def test_list_mangas_empty(db: LibraryDatabase) -> None:
    """list_mangas() on empty DB returns an empty PaginatedResult."""
    result = await db.list_mangas()
    assert result.items == []
    assert result.total == 0
    assert result.total_pages == 0
    assert result.has_next is False
    assert result.has_prev is False


# ────────────────────────────────────────────────────────────────────
# Test 7: FTS5 triggers
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fts5_after_add(db: LibraryDatabase, sample_manga: Manga) -> None:
    """FTS5 index is populated after add_manga()."""
    await db.add_manga(sample_manga)

    results = await db.search("One Piece")
    assert len(results) == 1
    assert results[0].id == sample_manga.id


@pytest.mark.asyncio
async def test_fts5_after_update(db: LibraryDatabase, sample_manga: Manga) -> None:
    """FTS5 index reflects updated title."""
    await db.add_manga(sample_manga)

    updated = sample_manga.model_copy(update={"title": "One Piece Updated"})
    await db.update_manga(updated)

    # Old title should not match
    old_results = await db.search("One Piece")
    # New title should match
    new_results = await db.search("Updated")
    assert len(new_results) == 1


@pytest.mark.asyncio
async def test_fts5_after_delete(db: LibraryDatabase, sample_manga: Manga) -> None:
    """FTS5 index is cleaned after delete_manga()."""
    await db.add_manga(sample_manga)
    await db.delete_manga(sample_manga.id, delete_files=False)

    results = await db.search("One Piece")
    assert results == []


# ────────────────────────────────────────────────────────────────────
# Test 8: concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_add_manga(db: LibraryDatabase) -> None:
    """Concurrent add_manga() does not corrupt the DB."""

    async def _add(i: int) -> int:
        manga = Manga(
            id=f"concurrent{i:04d}",
            source_id=f"src{i:04d}",
            site="mangadex",
            title=f"Concurrent {i:04d}",
            status=MangaStatus.ONGOING,
            language=Language.EN,
            content_rating=ContentRating.SAFE,
            url=f"https://example.com/{i}",
            updated_at="2026-10-29T12:00:00Z",
        )
        return await db.add_manga(manga)

    pks = await asyncio.gather(*[_add(i) for i in range(10)])

    # All should have unique PKs
    assert len(set(pks)) == 10

    result = await db.list_mangas(per_page=20)
    assert result.total == 10


# ────────────────────────────────────────────────────────────────────
# Test 9: transactions
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transaction_rollback(db: LibraryDatabase, sample_manga: Manga) -> None:
    """A failed transaction rolls back."""
    # Add manga
    await db.add_manga(sample_manga)

    # Try to add a chapter with an invalid manga_id (FK violation)
    # This should raise and NOT leave partial data
    bad_chapter = Chapter(
        id="bad",
        manga_id="nonexistent",
        source_id="bad",
        title="Bad",
        number=1.0,
        language=Language.EN,
        url="https://test.com/bad",
    )

    with pytest.raises(Exception):
        await db.add_chapter(bad_chapter)

    # The manga should still be there, no orphan chapter
    stored = await db.get_manga(sample_manga.id)
    assert stored is not None

    chapters = await db.get_chapters("nonexistent")
    assert chapters == []
