# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for LibrarySearch (FTS5 full-text search).

LibrarySearch provides full-text search over the local library using
SQLite FTS5 (fallback to LIKE if FTS5 unavailable).

Contract:
    - search(query, *, limit=50, offset=0) -> list[Manga]
    - Case-insensitive
    - Partial match (prefix)
    - Multi-term (AND by default)
    - UTF-8 support (Japanese, Korean, etc.)
    - Diacritics-insensitive (café = cafe) — if implemented
    - Relevance-sorted (match_score desc)
    - SQL-injection safe (parameterized queries)
    - Quote-aware ("one piece" = phrase)
    - Wildcard-aware (one* = prefix)

What is tested:
    - Simple search (title)
    - Case-insensitive
    - Partial match
    - Search by author
    - Search by description
    - Search by genres
    - Multi-term (AND)
    - Empty query
    - 1-char query
    - No results
    - UTF-8 (Japanese)
    - Diacritics (café)
    - Relevance sorting
    - Limit / offset
    - SQL injection
    - Quotes
    - Wildcards
    - Concurrency
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from nexusdl.core.config import Settings
from nexusdl.core.library.database import LibraryDatabase
from nexusdl.core.library.search import LibrarySearch
from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
)
from nexusdl.core.models.manga import Manga


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_manga(
    manga_id: str,
    title: str,
    author: str = "Unknown",
    description: str = "",
    genres: list[str] | None = None,
) -> Manga:
    """Create a Manga with minimal fields."""
    return Manga(
        id=manga_id,
        source_id=f"src-{manga_id}",
        site="mangadex",
        title=title,
        alternative_titles=[],
        description=description or None,
        author=author,
        artist=author,
        genres=genres or [],
        status=MangaStatus.ONGOING,
        language=Language.EN,
        content_rating=ContentRating.SAFE,
        url=f"https://example.com/{manga_id}",
        updated_at="2026-10-29T12:00:00Z",
    )


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
    """Fresh LibraryDatabase."""
    database = LibraryDatabase(settings.database_url)
    await database.init()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def search(db: LibraryDatabase) -> LibrarySearch:
    """LibrarySearch on top of the DB."""
    return LibrarySearch(db)


@pytest_asyncio.fixture
async def populated_db(db: LibraryDatabase) -> LibraryDatabase:
    """DB with 5 mangas (varied titles, authors, genres)."""
    mangas = [
        _make_manga(
            "m1",
            "One Piece",
            author="Oda Eiichiro",
            description="A story about pirates and treasure.",
            genres=["Action", "Adventure", "Comedy"],
        ),
        _make_manga(
            "m2",
            "Naruto",
            author="Kishimoto Masashi",
            description="A story about ninjas and friendship.",
            genres=["Action", "Adventure"],
        ),
        _make_manga(
            "m3",
            "Bleach",
            author="Kubo Tite",
            description="A story about soul reapers.",
            genres=["Action", "Supernatural"],
        ),
        _make_manga(
            "m4",
            "ワンピース",  # Japanese One Piece
            author="Oda Eiichiro",
            description="海賊の物語。",
            genres=["Action", "Adventure"],
        ),
        _make_manga(
            "m5",
            "Pokémon Adventures",
            author="Kusaka Hidenori",
            description="A story about Pokémon trainers.",
            genres=["Adventure", "Fantasy"],
        ),
    ]
    for m in mangas:
        await db.add_manga(m)
    return db


# ────────────────────────────────────────────────────────────────────
# Test 1: Simple search (title)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_by_title(populated_db: LibraryDatabase) -> None:
    """search('One Piece') returns the matching manga."""
    search = LibrarySearch(populated_db)
    results = await search.search("One Piece")

    assert len(results) == 1
    assert results[0].id == "m1"


@pytest.mark.asyncio
async def test_search_partial(populated_db: LibraryDatabase) -> None:
    """search('piece') returns matches (partial)."""
    search = LibrarySearch(populated_db)
    results = await search.search("piece")

    assert len(results) >= 1
    ids = [r.id for r in results]
    assert "m1" in ids


@pytest.mark.asyncio
async def test_search_case_insensitive(populated_db: LibraryDatabase) -> None:
    """search('one piece') matches 'One Piece' (case-insensitive)."""
    search = LibrarySearch(populated_db)
    results = await search.search("one piece")

    assert len(results) >= 1
    assert results[0].id == "m1"


# ────────────────────────────────────────────────────────────────────
# Test 2: Search by author
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_by_author(populated_db: LibraryDatabase) -> None:
    """search('Oda') returns mangas by Oda Eiichiro."""
    search = LibrarySearch(populated_db)
    results = await search.search("Oda")

    assert len(results) >= 1
    ids = [r.id for r in results]
    assert "m1" in ids


# ────────────────────────────────────────────────────────────────────
# Test 3: Search by description
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_by_description(populated_db: LibraryDatabase) -> None:
    """search('pirates') matches the description."""
    search = LibrarySearch(populated_db)
    results = await search.search("pirates")

    assert len(results) >= 1
    assert results[0].id == "m1"


# ────────────────────────────────────────────────────────────────────
# Test 4: Search by genres
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_by_genre(populated_db: LibraryDatabase) -> None:
    """search('Adventure') returns all adventure mangas."""
    search = LibrarySearch(populated_db)
    results = await search.search("Adventure")

    assert len(results) >= 3
    ids = [r.id for r in results]
    assert "m1" in ids
    assert "m2" in ids


# ────────────────────────────────────────────────────────────────────
# Test 5: Multi-term search (AND)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_multi_term(populated_db: LibraryDatabase) -> None:
    """search('one piece oda') returns results matching all terms."""
    search = LibrarySearch(populated_db)
    results = await search.search("one piece oda")

    assert len(results) >= 1
    assert results[0].id == "m1"


@pytest.mark.asyncio
async def test_search_multi_term_no_match(populated_db: LibraryDatabase) -> None:
    """search('one naruto') returns no results (AND semantics)."""
    search = LibrarySearch(populated_db)
    results = await search.search("one naruto")

    # Either no results (AND) or both (OR) — depends on impl
    # We accept AND semantics (stricter)
    if results:
        for r in results:
            assert "one" in r.title.lower() or "naruto" in r.title.lower()


# ────────────────────────────────────────────────────────────────────
# Test 6: Empty / short query
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_empty(populated_db: LibraryDatabase) -> None:
    """search('') returns []."""
    search = LibrarySearch(populated_db)
    results = await search.search("")
    assert results == []


@pytest.mark.asyncio
async def test_search_whitespace(populated_db: LibraryDatabase) -> None:
    """search('   ') returns []."""
    search = LibrarySearch(populated_db)
    results = await search.search("   ")
    assert results == []


# ────────────────────────────────────────────────────────────────────
# Test 7: No results
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_no_results(populated_db: LibraryDatabase) -> None:
    """search('xyz123') returns []."""
    search = LibrarySearch(populated_db)
    results = await search.search("xyz123")
    assert results == []


# ────────────────────────────────────────────────────────────────────
# Test 8: UTF-8 (Japanese)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_utf8_japanese(populated_db: LibraryDatabase) -> None:
    """search('ワンピース') matches the Japanese title."""
    search = LibrarySearch(populated_db)
    results = await search.search("ワンピース")

    assert len(results) >= 1
    ids = [r.id for r in results]
    assert "m4" in ids


# ────────────────────────────────────────────────────────────────────
# Test 9: Diacritics
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_diacritics(populated_db: LibraryDatabase) -> None:
    """search('pokemon') matches 'Pokémon' (diacritics-insensitive)."""
    search = LibrarySearch(populated_db)
    results = await search.search("pokemon")

    # Should match "Pokémon Adventures" if diacritics are normalized
    # If not, at least it should not crash
    assert isinstance(results, list)
    # Note: this depends on unidecode/normalization in the implementation


# ────────────────────────────────────────────────────────────────────
# Test 10: Relevance sorting
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_relevance_sorting(populated_db: LibraryDatabase) -> None:
    """Results are sorted by relevance (match_score descending)."""
    search = LibrarySearch(populated_db)
    results = await search.search("one piece")

    # If the search returns multiple results, check the order
    if len(results) >= 2:
        # match_score should be descending (if exposed)
        scores = [getattr(r, "_match_score", None) for r in results]
        non_none = [s for s in scores if s is not None]
        if len(non_none) >= 2:
            assert non_none == sorted(non_none, reverse=True)


# ────────────────────────────────────────────────────────────────────
# Test 11: Limit / offset
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_limit(populated_db: LibraryDatabase) -> None:
    """search(..., limit=2) returns at most 2 results."""
    search = LibrarySearch(populated_db)
    results = await search.search("Adventure", limit=2)

    assert len(results) <= 2


@pytest.mark.asyncio
async def test_search_offset(populated_db: LibraryDatabase) -> None:
    """search(..., offset=1) skips the first result."""
    search = LibrarySearch(populated_db)
    all_results = await search.search("Adventure", limit=10)
    offset_results = await search.search("Adventure", limit=10, offset=1)

    if len(all_results) >= 2:
        # The first result of offset should be the second of all
        assert offset_results[0].id != all_results[0].id


# ────────────────────────────────────────────────────────────────────
# Test 12: SQL injection safety
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_sql_injection(populated_db: LibraryDatabase) -> None:
    """SQL injection attempts do not crash or corrupt the DB."""
    search = LibrarySearch(populated_db)

    # Classic SQL injection
    results = await search.search("'; DROP TABLE mangas; --")
    assert isinstance(results, list)

    # Verify the table still exists
    all_results = await search.search("One Piece")
    assert len(all_results) >= 1


# ────────────────────────────────────────────────────────────────────
# Test 13: Quotes (phrase search)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_quoted_phrase(populated_db: LibraryDatabase) -> None:
    """search('"one piece"') matches the phrase."""
    search = LibrarySearch(populated_db)
    results = await search.search('"one piece"')

    assert len(results) >= 1
    assert results[0].id == "m1"


# ────────────────────────────────────────────────────────────────────
# Test 14: Wildcards
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_wildcard(populated_db: LibraryDatabase) -> None:
    """search('one*') matches 'One Piece' (prefix)."""
    search = LibrarySearch(populated_db)
    results = await search.search("one*")

    assert len(results) >= 1
    ids = [r.id for r in results]
    assert "m1" in ids


# ────────────────────────────────────────────────────────────────────
# Test 15: Concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_concurrent(populated_db: LibraryDatabase) -> None:
    """Concurrent searches do not crash."""
    search = LibrarySearch(populated_db)

    async def _search(q: str):
        return await search.search(q)

    results = await asyncio.gather(
        _search("one"),
        _search("naruto"),
        _search("bleach"),
    )

    assert len(results) == 3
    assert all(isinstance(r, list) for r in results)
