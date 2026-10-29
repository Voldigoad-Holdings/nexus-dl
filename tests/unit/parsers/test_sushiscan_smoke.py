# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Smoke tests for the SushiScan parser.

SushiScan is a **French manga site** built on the **WordPress Madara** theme.
It is **not Cloudflare-protected**, so these tests can run in a scheduled
CI job without special cookies or Playwright.

These tests require:

    - Network access
    - NEXUSDL_ALLOW_ADULT=false (SushiScan is general audience)

They are marked ``@pytest.mark.smoke`` and ``@pytest.mark.integration``,
which means they are:

    - **Skipped by default** (pytest.ini: ``addopts = -m "not smoke"``)
    - **Run manually** with: ``pytest -m smoke``
    - **Run in a scheduled CI job** (daily, not on PRs)

Why run daily?
    Madara-based sites are relatively stable, so a daily check catches:
    - Selector changes (Madara updates)
    - Domain changes (mirror switching)
    - Rate limit changes
    - Language mismatches

Strategy: **tolerant assertions**
    - Assert that a result is returned, not its exact title
    - Assert that pages are URLs, not their exact paths
    - Assert that chapters are sorted, not their exact count
"""
from __future__ import annotations

import asyncio
import os
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

#: Well-known French manga (One Piece is universal)
SEARCH_QUERY = "one piece"

#: SushiScan rate limit (Madara-based, ~2 req/s)
RATE_LIMIT_PER_SECOND = 2


# ────────────────────────────────────────────────────────────────────
# Skip conditions
# ────────────────────────────────────────────────────────────────────


def _sushiscan_available() -> tuple[bool, str]:
    """Check whether SushiScan smoke tests can run.

    Returns:
        ``(available, reason)``.
    """
    # 1. Check network access
    if os.environ.get("NEXUSDL_SKIP_NETWORK_TESTS", "false").lower() == "true":
        return False, "NEXUSDL_SKIP_NETWORK_TESTS=true"

    # 2. Check that sushiscan is in sites.yaml
    try:
        from nexusdl.core.config import load_settings
        from nexusdl.core.registry.site_registry import SiteRegistry
        from nexusdl.parsers.factory import DefaultParserFactory

        settings = load_settings()
        registry = SiteRegistry(settings, parser_factory=DefaultParserFactory())
        registry.get_site("sushiscan")
    except Exception as exc:  # noqa: BLE001
        return False, f"Cannot load sushiscan site: {exc}"

    return True, ""


_AVAILABLE, _SKIP_REASON = _sushiscan_available()


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def sushiscan_settings():
    """Load real settings."""
    from nexusdl.core.config import load_settings

    return load_settings()


@pytest.fixture(scope="module")
def sushiscan_site(sushiscan_settings):
    """Get the SushiScan SiteConfig."""
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.parsers.factory import DefaultParserFactory

    registry = SiteRegistry(
        sushiscan_settings,
        parser_factory=DefaultParserFactory(),
    )
    return registry.get_site("sushiscan")


@pytest.fixture(scope="module")
async def sushiscan_parser(sushiscan_settings, sushiscan_site):
    """Create a SushiScan parser with a real HttpSession."""
    from nexusdl.core.session.cookie_manager import CookieManager
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.parsers.factory import DefaultParserFactory

    cookies = CookieManager(
        storage_path=sushiscan_settings.cookie_storage_path,
        key=sushiscan_settings.cookie_encryption_key.get_secret_value().encode()[:32],
    )

    async with HttpSession(
        sushiscan_site,
        sushiscan_settings,
        cookie_manager=cookies,
    ) as session:
        factory = DefaultParserFactory()
        parser = factory.create(sushiscan_site, session)
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
async def test_health_check(sushiscan_parser) -> None:
    """SushiScan is reachable."""
    ok = await sushiscan_parser.health_check()
    assert ok is True, (
        "SushiScan health_check failed. "
        "The site may be down or the domain changed."
    )


# ────────────────────────────────────────────────────────────────────
# Test 2: search
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_results(sushiscan_parser) -> None:
    """search('one piece') returns at least one result."""
    results = await sushiscan_parser.search(SEARCH_QUERY)
    assert len(results) >= 1, f"search('{SEARCH_QUERY}') returned 0 results"

    result = results[0]
    assert result.site_id == "sushiscan"
    assert isinstance(result.title, str) and len(result.title) > 0
    assert result.url.startswith("https://sushiscan.")
    # SushiScan is a French site
    assert result.language == Language.FR


@pytest.mark.asyncio
async def test_search_pagination(sushiscan_parser) -> None:
    """search(page=2) returns different results than page=1."""
    page1 = await sushiscan_parser.search(SEARCH_QUERY, page=1)
    await asyncio.sleep(0.5)  # respect rate limit
    page2 = await sushiscan_parser.search(SEARCH_QUERY, page=2)

    assert len(page1) >= 1
    if len(page2) > 0:
        urls1 = {r.url for r in page1}
        urls2 = {r.url for r in page2}
        assert urls1 != urls2, "page 1 and page 2 returned identical results"


# ────────────────────────────────────────────────────────────────────
# Test 3: get_manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_manga(sushiscan_parser) -> None:
    """get_manga(url) returns a full Manga."""
    results = await sushiscan_parser.search(SEARCH_QUERY)
    assert len(results) >= 1, "Cannot test get_manga: no search results"

    url = results[0].url
    manga = await sushiscan_parser.get_manga(url)

    assert manga is not None
    assert manga.site == "sushiscan"
    assert len(manga.title) > 0
    assert manga.url.startswith("https://sushiscan.")
    assert manga.language == Language.FR
    # SushiScan is general audience (some mature content but not adult-only)
    assert manga.content_rating in (
        ContentRating.SAFE,
        ContentRating.SUGGESTIVE,
    )
    # ID is 16-char hex
    assert len(manga.id) == 16
    assert all(c in "0123456789abcdef" for c in manga.id)


# ────────────────────────────────────────────────────────────────────
# Test 4: get_chapters
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chapters_sorted(sushiscan_parser) -> None:
    """get_chapters() returns chapters sorted by number ascending."""
    results = await sushiscan_parser.search(SEARCH_QUERY)
    assert len(results) >= 1

    manga = await sushiscan_parser.get_manga(results[0].url)
    chapters = await sushiscan_parser.get_chapters(manga)

    assert len(chapters) >= 1, "get_chapters() returned 0 chapters"

    chapter = chapters[0]
    assert isinstance(chapter.id, str) and len(chapter.id) == 16
    assert chapter.manga_id == manga.id
    assert chapter.url.startswith("https://sushiscan.")
    assert chapter.language == Language.FR

    # Sorting (numeric only)
    numeric = [
        float(c.number) for c in chapters
        if isinstance(c.number, (int, float)) or (
            isinstance(c.number, str) and c.number.replace(".", "").isdigit()
        )
    ]
    if len(numeric) > 1:
        assert numeric == sorted(numeric), "Chapters not sorted ascending"


# ────────────────────────────────────────────────────────────────────
# Test 5: get_pages
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pages(sushiscan_parser) -> None:
    """get_pages() returns page URLs sorted by index."""
    results = await sushiscan_parser.search(SEARCH_QUERY)
    assert len(results) >= 1

    manga = await sushiscan_parser.get_manga(results[0].url)
    chapters = await sushiscan_parser.get_chapters(manga)
    assert len(chapters) >= 1, "Cannot test get_pages: no chapters"

    pages = await sushiscan_parser.get_pages(chapters[0])

    assert len(pages) >= 1, "get_pages() returned 0 pages"

    for i, page in enumerate(pages):
        assert page.index == i, f"Page {i} has index={page.index}"
        assert page.url.startswith("https://"), f"Page {i} URL not absolute"
        assert page.filename, f"Page {i} filename empty"
        assert page.filename.endswith((".jpg", ".jpeg", ".png", ".webp")), (
            f"Page {i} filename has unexpected extension: {page.filename}"
        )


# ────────────────────────────────────────────────────────────────────
# Test 6: cover URL
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cover_url(sushiscan_parser) -> None:
    """The cover URL points to SushiScan's uploads directory."""
    results = await sushiscan_parser.search(SEARCH_QUERY)
    assert len(results) >= 1

    manga = await sushiscan_parser.get_manga(results[0].url)

    assert manga.cover_url is not None
    cover = str(manga.cover_url)
    # Madara covers are in /wp-content/uploads/
    assert "sushiscan." in cover or "wp-content" in cover


# ────────────────────────────────────────────────────────────────────
# Test 7: language
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_language(sushiscan_site, sushiscan_parser) -> None:
    """SushiScan is a French site."""
    assert sushiscan_site.language == Language.FR
    assert sushiscan_parser.language in ("fr", Language.FR)


# ────────────────────────────────────────────────────────────────────
# Test 8: adult flag
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adult_flag(sushiscan_site) -> None:
    """SushiScan is marked as general audience (adult=False)."""
    assert sushiscan_site.adult is False


# ────────────────────────────────────────────────────────────────────
# Test 9: Madara theme compliance
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_madara_theme(sushiscan_parser) -> None:
    """The parser uses the WordPressMadaraMixin."""
    # Check MRO includes WordPressMadaraMixin
    from nexusdl.parsers.mixins.wordpress_madara import WordPressMadaraMixin

    assert isinstance(sushiscan_parser, WordPressMadaraMixin), (
        "SushiScanParser must inherit from WordPressMadaraMixin"
    )


# ────────────────────────────────────────────────────────────────────
# Test 10: no rate limit triggered
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_rate_limit(sushiscan_parser) -> None:
    """3 sequential searches do not trigger 429."""
    for i in range(3):
        results = await sushiscan_parser.search(f"test{i}")
        assert isinstance(results, list)
        await asyncio.sleep(0.5)  # respect rate limit
