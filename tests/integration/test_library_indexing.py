# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Integration tests for the local library.

Exercises the full library pipeline WITHOUT any network:

    Files on disk (CBZ)
        ↓
    LibraryScanner.scan()
        ↓
    LibraryDatabase.add_manga() + add_chapter()
        ↓
    FTS5 index (triggers) → searchable
        ↓
    search() → list[Manga]
    get_reading_progress() → ReadingProgress
    update_manga() → Manga
    delete_manga() → files removed (optional)

Why integration?
    Unit tests for `LibraryDatabase` mock SQLAlchemy. This test uses
    a REAL SQLite file (via aiosqlite) so it catches:
    - SQL syntax errors
    - Migration failures
    - FTS5 tokenizer issues
    - Foreign key violations
    - Trigger bugs

Why no network?
    - The library is local-only
    - Files are generated on the fly (CBZ with ComicInfo.xml)
    - FTS5 tests use local data

Why tmp_path?
    - Each test gets an isolated DB
    - No leftover files between runs
    - Parallel-safe (pytest-xdist)
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from nexusdl.core.config import Settings

from nexusdl.core.config import Settings
from nexusdl.core.library.database import LibraryDatabase
from nexusdl.core.library.scanner import LibraryScanner
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
)
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


async def _check_fts5_available(db: LibraryDatabase) -> bool:
    """Check if SQLite was compiled with FTS5.

    Returns:
        True if FTS5 is available, False otherwise.
    """
    try:
        async with db._engine.connect() as conn:  # type: ignore[attr-defined]
            from sqlalchemy import text

            result = await conn.execute(text("PRAGMA compile_options"))
            options = [row[0] for row in result]
            return any("FTS5" in opt.upper() for opt in options)
    except Exception:  # noqa: BLE001
        return False


def _make_cbz(
    path: Path,
    *,
    title: str,
    series: str,
    number: str,
    page_count: int = 3,
    language: str = "en",
) -> None:
    """Create a minimal valid CBZ with ComicInfo.xml.

    Args:
        path: Output .cbz path.
        title: Chapter title.
        series: Manga title.
        number: Chapter number.
        page_count: Number of fake pages.
        language: ISO language code.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Minimal valid PNG (1×1 transparent)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6360000002000001e221bc330000000049454e44ae"
        "426082"
    )

    comic_info = f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Title>{title}</Title>
  <Series>{series}</Series>
  <Number>{number}</Number>
  <LanguageISO>{language}</LanguageISO>
  <PageCount>{page_count}</PageCount>
</ComicInfo>
"""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(page_count):
            zf.writestr(f"{i + 1:03d}.png", png_bytes)
        zf.writestr("ComicInfo.xml", comic_info)


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
        public_url="http://localhost:3000",
        api_base_url="http://localhost:8000",
        frontend_url="http://localhost:3000",
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


@pytest.fixture
async def db(settings: Settings) -> "LibraryDatabase":
    """Fresh LibraryDatabase with migrations applied."""
    database = LibraryDatabase(settings.database_url)
    await database.init()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def sample_manga() -> Manga:
    """Sample Manga from fixtures."""
    data = json.loads((FIXTURES / "sample_manga.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return Manga.model_validate(data)


@pytest.fixture
def sample_chapter(sample_manga: Manga) -> Chapter:
    """Sample Chapter from fixtures."""
    data = json.loads((FIXTURES / "sample_chapter.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    data["manga_id"] = sample_manga.id
    return Chapter.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 1: FTS5 availability
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fts5_is_available(db: LibraryDatabase) -> None:
    """Verify SQLite was compiled with FTS5.

    If this fails, the library search will not work. The fallback
    (LIKE-based) is implemented, but slower. CI should always pass
    this test.
    """
    available = await _check_fts5_available(db)
    assert available, (
        "SQLite was not compiled with FTS5. "
        "Install sqlite3 with FTS5 support: "
        "https://www.sqlite.org/fts5.html"
    )


# ────────────────────────────────────────────────────────────────────
# Test 2: Add manga + chapter
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_add_manga_and_chapter(
    db: LibraryDatabase,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """Insert a manga and a chapter, then verify they're stored."""
    manga_pk = await db.add_manga(sample_manga)
    assert manga_pk > 0

    chapter_pk = await db.add_chapter(sample_chapter)
    assert chapter_pk > 0

    # Verify manga
    stored = await db.get_manga(sample_manga.id)
    assert stored is not None
    assert stored.id == sample_manga.id
    assert stored.title == "One Piece"
    assert stored.author == "Oda Eiichiro"

    # Verify chapter count
    chapters = await db.get_chapters(sample_manga.id)
    assert len(chapters) == 1
    assert chapters[0].id == sample_chapter.id


# ────────────────────────────────────────────────────────────────────
# Test 3: FTS5 search (title)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fts5_search_by_title(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """Search by title uses FTS5."""
    await db.add_manga(sample_manga)

    # Exact match
    results = await db.search("One Piece")
    assert len(results) == 1
    assert results[0].id == sample_manga.id

    # Lowercase match (FTS5 is case-insensitive)
    results = await db.search("one piece")
    assert len(results) == 1

    # Partial match
    results = await db.search("piece")
    assert len(results) == 1

    # No match
    results = await db.search("Naruto")
    assert len(results) == 0


# ────────────────────────────────────────────────────────────────────
# Test 4: FTS5 search (author)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fts5_search_by_author(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """Search by author uses FTS5."""
    await db.add_manga(sample_manga)

    results = await db.search("Oda")
    assert len(results) == 1

    results = await db.search("Eiichiro")
    assert len(results) == 1


# ────────────────────────────────────────────────────────────────────
# Test 5: FTS5 search (description + genres)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fts5_search_by_description_and_genres(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """Search by description and genres uses FTS5."""
    await db.add_manga(sample_manga)

    # Description
    results = await db.search("pirates")
    assert len(results) == 1

    # Genres
    results = await db.search("Adventure")
    assert len(results) == 1

    results = await db.search("Shounen")
    assert len(results) == 1


# ────────────────────────────────────────────────────────────────────
# Test 6: FTS5 Unicode (Japanese title)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_fts5_unicode_search(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """FTS5 handles UTF-8 (Japanese alternative titles)."""
    await db.add_manga(sample_manga)

    results = await db.search("ワンピース")
    assert len(results) == 1


# ────────────────────────────────────────────────────────────────────
# Test 7: Reading progress
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reading_progress(
    db: LibraryDatabase,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """Save and retrieve reading progress."""
    await db.add_manga(sample_manga)
    await db.add_chapter(sample_chapter)

    # Initially no progress
    progress = await db.get_reading_progress(sample_manga.id)
    assert progress is None

    # Save progress
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

    # Update progress
    await db.set_reading_progress(
        manga_id=sample_manga.id,
        chapter_id=sample_chapter.id,
        page=53,
    )
    progress = await db.get_reading_progress(sample_manga.id)
    assert progress is not None
    assert progress.page == 53


# ────────────────────────────────────────────────────────────────────
# Test 8: Update manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_manga(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """Update manga metadata."""
    await db.add_manga(sample_manga)

    # Update title
    updated = sample_manga.model_copy(update={"title": "One Piece (Updated)"})
    await db.update_manga(updated)

    stored = await db.get_manga(sample_manga.id)
    assert stored is not None
    assert stored.title == "One Piece (Updated)"

    # FTS5 index should reflect the new title
    results = await db.search("Updated")
    assert len(results) == 1


# ────────────────────────────────────────────────────────────────────
# Test 9: Delete manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_manga(
    db: LibraryDatabase,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """Delete manga removes it and its chapters from DB."""
    await db.add_manga(sample_manga)
    await db.add_chapter(sample_chapter)

    # Delete (no files)
    await db.delete_manga(sample_manga.id, delete_files=False)

    # Verify gone
    stored = await db.get_manga(sample_manga.id)
    assert stored is None

    # Chapters gone too
    chapters = await db.get_chapters(sample_manga.id)
    assert len(chapters) == 0

    # FTS5 index cleaned
    results = await db.search("One Piece")
    assert len(results) == 0


# ────────────────────────────────────────────────────────────────────
# Test 10: Deduplication
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_deduplication_by_id(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """Adding the same manga twice does not create duplicates."""
    pk1 = await db.add_manga(sample_manga)
    pk2 = await db.add_manga(sample_manga)

    # Same primary key (upsert)
    assert pk1 == pk2

    # Only one row
    results = await db.search("One Piece")
    assert len(results) == 1


# ────────────────────────────────────────────────────────────────────
# Test 11: Pagination
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pagination(db: LibraryDatabase) -> None:
    """Pagination with per_page and total_pages."""
    # Insert 15 mangas
    for i in range(15):
        manga = Manga(
            id=f"test{i:02d}",
            source_id=f"src{i:02d}",
            site="mangadex",
            title=f"Test Manga {i:02d}",
            author=f"Author {i:02d}",
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


# ────────────────────────────────────────────────────────────────────
# Test 12: LibraryScanner (files on disk)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_scanner_indexes_existing_cbz(
    db: LibraryDatabase,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """LibraryScanner finds CBZ files and indexes them."""
    # Create a fake library on disk
    library_dir = tmp_path / "library"
    cbz_path = library_dir / "One Piece" / "Chapter 001.cbz"
    _make_cbz(
        cbz_path,
        title="Romance Dawn",
        series="One Piece",
        number="1",
        page_count=3,
    )

    # Scan
    scanner = LibraryScanner(db, settings)
    stats = await scanner.scan(library_dir)

    assert stats.files_found == 1
    assert stats.files_indexed == 1
    assert stats.files_failed == 0

    # Verify the manga is in the DB
    results = await db.search("One Piece")
    assert len(results) == 1
    assert results[0].title == "One Piece"


# ────────────────────────────────────────────────────────────────────
# Test 13: Scanner skips already-indexed
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_scanner_skips_already_indexed(
    db: LibraryDatabase,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Scanner skips files already in the DB (by checksum)."""
    library_dir = tmp_path / "library"
    cbz_path = library_dir / "One Piece" / "Chapter 001.cbz"
    _make_cbz(
        cbz_path,
        title="Romance Dawn",
        series="One Piece",
        number="1",
    )

    scanner = LibraryScanner(db, settings)

    # First scan
    stats1 = await scanner.scan(library_dir)
    assert stats1.files_indexed == 1

    # Second scan (no change)
    stats2 = await scanner.scan(library_dir)
    assert stats2.files_found == 1
    assert stats2.files_indexed == 0  # skipped
    assert stats2.files_skipped == 1


# ────────────────────────────────────────────────────────────────────
# Test 14: Concurrent inserts (race condition)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_inserts(
    db: LibraryDatabase,
    sample_manga: Manga,
) -> None:
    """Concurrent add_manga() calls do not raise (upsert)."""

    async def _add(_: int) -> int:
        return await db.add_manga(sample_manga)

    # 5 concurrent inserts
    pks = await asyncio.gather(*[_add(i) for i in range(5)])

    # All should return the same PK (upsert)
    assert len(set(pks)) == 1

    # Only one row
    results = await db.search("One Piece")
    assert len(results) == 1
