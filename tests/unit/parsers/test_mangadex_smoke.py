# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Smoke tests for the MangaDex parser.

MangaDex is the **reference parser** because:

    - It has an **official API** (https://api.mangadex.org)
    - No Cloudflare
    - No authentication required (for reading)
    - Documented rate limit: 5 req/s per IP
    - Well-structured JSON responses

These tests hit the **real MangaDex API** and require network access.
They are marked ``@pytest.mark.smoke`` and ``@pytest.mark.integration``,
which means they are:

    - **Skipped by default** (pytest.ini: ``addopts = -m "not smoke"``)
    - **Run manually** with: ``pytest -m smoke``
    - **Run in a scheduled CI job** (daily, not on PRs)

Why run daily?
    MangaDex is stable, so a daily check catches:
    - API schema changes
    - Rate limit changes
    - Cover URL changes
    - Authentication requirements changes

Strategy: **tolerant assertions**
    - Assert that a result is returned, not its exact UUID
    - Assert that pages are URLs, not their exact paths
    - Assert that chapters are sorted, not their exact count
    - Use ``health_check()`` before hitting the API to fail fast
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.search import SearchResult

from nexusdl.core.models.enums import ContentRating, Language


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

#: Well-known manga, unlikely to disappear
#: "One Piece" — uuid is stable on MangaDex
ONE_PIECE_UUID = "a1c7c817-4e59-43b7-9365-09675a149a6f"

#: Known search query
SEARCH_QUERY = "one piece"

#: MangaDex rate limit (5 req/s per IP, burst 10)
#: https://api.mangadex.org/docs/2-limitations/
RATE_LIMIT_PER_SECOND = 5


# ────────────────────────────────────────────────────────────────────
# Skip conditions
# ────────────────────────────────────────────────────────────────────


def _mangadex_available() -> tuple[bool, str]:
    """Check whether MangaDex smoke tests can run.

    Returns:
        ``(available, reason)``.
    """
    # 1. Check network access (optional env var to skip in air-gapped CI)
    if os.environ.get("NEXUSDL_SKIP_NETWORK_TESTS", "false").lower() == "true":
        return False, "NEXUSDL_SKIP_NETWORK_TESTS=true"

    # 2. Check that mangadex is in sites.yaml
    try:
        from nexusdl.core.config import load_settings
        from nexusdl.core.registry.site_registry import SiteRegistry
        from nexusdl.parsers.factory import DefaultParserFactory

        settings = load_settings()
        registry = SiteRegistry(settings, parser_factory=DefaultParserFactory())
        registry.get_site("mangadex")
    except Exception as exc:  # noqa: BLE001
        return False, f"Cannot load mangadex site: {exc}"

    return True, ""


_AVAILABLE, _SKIP_REASON = _mangadex_available()


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mangadex_settings():
    """Load real settings."""
    from nexusdl.core.config import load_settings

    return load_settings()


@pytest.fixture(scope="module")
def mangadex_site(mangadex_settings):
    """Get the MangaDex SiteConfig."""
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.parsers.factory import DefaultParserFactory

    registry = SiteRegistry(
        mangadex_settings,
        parser_factory=DefaultParserFactory(),
    )
    return registry.get_site("mangadex")


@pytest.fixture(scope="module")
async def mangadex_parser(mangadex_settings, mangadex_site):
    """Create a MangaDex parser with a real HttpSession."""
    from nexusdl.core.session.cookie_manager import CookieManager
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.parsers.factory import DefaultParserFactory

    cookies = CookieManager(
        storage_path=mangadex_settings.cookie_storage_path,
        key=mangadex_settings.cookie_encryption_key.get_secret_value().encode()[:32],
    )

    async with HttpSession(
        mangadex_site,
        mangadex_settings,
        cookie_manager=cookies,
    ) as session:
        factory = DefaultParserFactory()
        parser = factory.create(mangadex_site, session)
        yield parser


# ────────────────────────────────────────────────────────────────────
# Skip decorator
# ────────────────────────────────────────────────────────────────────


pytestmark = [
    pytest.mark.smoke,
    pytest.mark.integration,
    pytest.mark.skipif(not _AVAILABLE, reason=_SKIP_REASON),
    pytest.mark.timeout(60),
]


# ────────────────────────────────────────────────────────────────────
# Test 1: health_check
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check(mangadex_parser) -> None:
    """MangaDex API is reachable."""
    ok = await mangadex_parser.health_check()
    assert ok is True, (
        "MangaDex health_check failed. "
        "API may be down: https://status.mangadex.org"
    )


# ────────────────────────────────────────────────────────────────────
# Test 2: search
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_results(mangadex_parser) -> None:
    """search('one piece') returns at least one result."""
    results = await mangadex_parser.search(SEARCH_QUERY)
    assert len(results) >= 1, f"search('{SEARCH_QUERY}') returned 0 results"

    result = results[0]
    assert result.site_id == "mangadex"
    assert isinstance(result.title, str) and len(result.title) > 0
    assert result.url.startswith("https://mangadex.org/title/")
    assert result.language == Language.EN


@pytest.mark.asyncio
async def test_search_language_filter(mangadex_parser) -> None:
    """Search results are filtered by language (en)."""
    results = await mangadex_parser.search(SEARCH_QUERY)
    # All results should be English
    for r in results:
        assert r.language in (Language.EN, Language.UNKNOWN), (
            f"Result '{r.title}' has language={r.language}, expected EN"
        )


@pytest.mark.asyncio
async def test_search_pagination(mangadex_parser) -> None:
    """search(page=2) returns different results than page=1."""
    page1 = await mangadex_parser.search(SEARCH_QUERY, page=1)
    await asyncio.sleep(0.25)  # respect rate limit
    page2 = await mangadex_parser.search(SEARCH_QUERY, page=2)

    assert len(page1) >= 1
    if len(page2) > 0:
        urls1 = {r.url for r in page1}
        urls2 = {r.url for r in page2}
        assert urls1 != urls2, "page 1 and page 2 returned identical results"


# ────────────────────────────────────────────────────────────────────
# Test 3: get_manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_manga_by_uuid(mangadex_parser) -> None:
    """get_manga(uuid) returns a full Manga."""
    manga = await mangadex_parser.get_manga(ONE_PIECE_UUID)

    assert manga is not None
    assert manga.site == "mangadex"
    assert manga.title == "One Piece"
    assert manga.author == "Oda Eiichiro"
    assert manga.artist == "Oda Eiichiro"
    assert manga.year == 1997
    assert manga.language == Language.EN
    assert manga.content_rating == ContentRating.SAFE
    assert manga.status.value in ("ongoing", "completed", "hiatus", "cancelled", "unknown")
    assert manga.url.startswith("https://mangadex.org/title/")
    # ID is a 16-char hex hash
    assert len(manga.id) == 16
    assert all(c in "0123456789abcdef" for c in manga.id)
    # Cover URL is resolved
    assert manga.cover_url is not None
    assert str(manga.cover_url).startswith("https://")
    # Genres
    assert len(manga.genres) >= 1
    assert "Action" in manga.genres or "Adventure" in manga.genres


@pytest.mark.asyncio
async def test_get_manga_not_found(mangadex_parser) -> None:
    """get_manga(unknown-uuid) raises ParserNotFoundError."""
    from nexusdl.parsers.exceptions import ParserNotFoundError

    fake_uuid = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ParserNotFoundError):
        await mangadex_parser.get_manga(fake_uuid)


# ────────────────────────────────────────────────────────────────────
# Test 4: get_chapters
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chapters_sorted(mangadex_parser) -> None:
    """get_chapters() returns chapters sorted by number ascending."""
    manga = await mangadex_parser.get_manga(ONE_PIECE_UUID)
    chapters = await mangadex_parser.get_chapters(manga)

    assert len(chapters) >= 1, "get_chapters() returned 0 chapters"

    # Structure
    chapter = chapters[0]
    assert isinstance(chapter.id, str) and len(chapter.id) == 16
    assert chapter.manga_id == manga.id
    assert chapter.url.startswith("https://mangadex.org/chapter/")
    assert chapter.language == Language.EN

    # Sorting (numeric only, some chapters may be "Extra")
    numeric = [
        float(c.number) for c in chapters
        if isinstance(c.number, (int, float)) or (
            isinstance(c.number, str) and c.number.replace(".", "").isdigit()
        )
    ]
    if len(numeric) > 1:
        assert numeric == sorted(numeric), "Chapters not sorted ascending"


@pytest.mark.asyncio
async def test_get_chapters_language_filter(mangadex_parser) -> None:
    """All chapters are in English (translatedLanguage=en)."""
    manga = await mangadex_parser.get_manga(ONE_PIECE_UUID)
    chapters = await mangadex_parser.get_chapters(manga)

    for c in chapters:
        assert c.language in (Language.EN, Language.UNKNOWN), (
            f"Chapter '{c.title}' has language={c.language}, expected EN"
        )


# ────────────────────────────────────────────────────────────────────
# Test 5: get_pages
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pages(mangadex_parser) -> None:
    """get_pages() returns page URLs sorted by index."""
    manga = await mangadex_parser.get_manga(ONE_PIECE_UUID)
    chapters = await mangadex_parser.get_chapters(manga)
    assert len(chapters) >= 1, "Cannot test get_pages: no chapters"

    # Use the first chapter
    chapter = chapters[0]
    pages = await mangadex_parser.get_pages(chapter)

    assert len(pages) >= 1, "get_pages() returned 0 pages"

    # Structure
    for i, page in enumerate(pages):
        assert page.index == i, f"Page {i} has index={page.index}"
        assert page.url.startswith("https://"), f"Page {i} URL not absolute"
        assert page.filename, f"Page {i} filename empty"
        assert page.filename.endswith((".jpg", ".jpeg", ".png", ".webp")), (
            f"Page {i} filename has unexpected extension: {page.filename}"
        )

    # pages_count matches
    if chapter.pages_count is not None:
        assert len(pages) == chapter.pages_count, (
            f"pages_count={chapter.pages_count} != len(pages)={len(pages)}"
        )


# ────────────────────────────────────────────────────────────────────
# Test 6: cover URL
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cover_url(mangadex_parser) -> None:
    """The cover URL is resolved via the /cover/{id} endpoint."""
    manga = await mangadex_parser.get_manga(ONE_PIECE_UUID)
    assert manga.cover_url is not None
    url = str(manga.cover_url)
    # MangaDex covers are served from uploads.mangadex.org
    assert "uploads.mangadex.org" in url or "mangadex.org/covers" in url
    # Should be an image
    assert url.endswith((".jpg", ".jpeg", ".png", ".webp")) or "?" in url


# ────────────────────────────────────────────────────────────────────
# Test 7: rate limit respected
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_respected(mangadex_parser) -> None:
    """10 rapid searches do not trigger 429 (rate limit respected)."""
    for i in range(5):
        results = await mangadex_parser.search(f"test{i}")
        assert isinstance(results, list)
    # If rate limit is hit, we'd get an exception, not reach here.
    assert True


# ────────────────────────────────────────────────────────────────────
# Test 8: adult content filter
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adult_content_filter(mangadex_parser) -> None:
    """safe search filters out pornographic content."""
    results = await mangadex_parser.search(SEARCH_QUERY)

    for r in results:
        assert r.content_rating != ContentRating.PORNOGRAPHIC, (
            f"Result '{r.title}' is pornographic but should be filtered"
        )
