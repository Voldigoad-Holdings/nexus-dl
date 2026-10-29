# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Smoke tests for the nhentai parser.

nhentai is a **Cloudflare-protected adult site** with a **non-official API**.

These tests require:

    - Network access
    - Playwright (Cloudflare bypass)
    - NEXUSDL_ALLOW_ADULT=true
    - A valid ``cf_clearance`` cookie in .env

They are marked ``@pytest.mark.smoke`` and ``@pytest.mark.integration``,
which means they are:

    - **Skipped by default** (pytest.ini: ``addopts = -m "not smoke"``)
    - **Run manually** with: ``pytest -m smoke``
    - **Run in a scheduled CI job** (weekly, not on PRs)

Why not run on every PR?
    - Network access is slow and flaky
    - nhentai rate-limits aggressive clients
    - Cloudflare may ban CI IPs
    - The site content changes (a fixed assertion would break)

Strategy: **tolerant assertions**
    - Assert that a result is returned, not its exact gallery ID
    - Assert that pages are URLs, not their exact paths
    - Assert that chapters are returned, not their exact count
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

#: Known search query (well-known doujinshi artist, unlikely to disappear)
SEARCH_QUERY = "vanilla"


# ────────────────────────────────────────────────────────────────────
# Skip conditions
# ────────────────────────────────────────────────────────────────────


def _nhentai_available() -> tuple[bool, str]:
    """Check whether nhentai smoke tests can run.

    Returns:
        ``(available, reason)``.
    """
    # 1. Check NEXUSDL_ALLOW_ADULT
    if os.environ.get("NEXUSDL_ALLOW_ADULT", "false").lower() != "true":
        return False, "NEXUSDL_ALLOW_ADULT=false (set to true to run adult site tests)"

    # 2. Check Playwright enabled
    if os.environ.get("NEXUSDL_PLAYWRIGHT_ENABLED", "false").lower() != "true":
        return False, "NEXUSDL_PLAYWRIGHT_ENABLED=false (nhentai requires Cloudflare bypass)"

    # 3. Check cf_clearance cookie
    if not os.environ.get("NEXUSDL_NHENTAI_CF_CLEARANCE"):
        return False, (
            "NEXUSDL_NHENTAI_CF_CLEARANCE not set "
            "(extract from browser: DevTools → Application → Cookies)"
        )

    # 4. Check that nhentai is in sites.yaml
    try:
        from nexusdl.core.config import load_settings
        from nexusdl.core.registry.site_registry import SiteRegistry
        from nexusdl.parsers.factory import DefaultParserFactory

        settings = load_settings()
        registry = SiteRegistry(settings, parser_factory=DefaultParserFactory())
        registry.get_site("nhentai")
    except Exception as exc:  # noqa: BLE001
        return False, f"Cannot load nhentai site: {exc}"

    return True, ""


_AVAILABLE, _SKIP_REASON = _nhentai_available()


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def nhentai_settings():
    """Load real settings (must include cf_clearance)."""
    from nexusdl.core.config import load_settings

    return load_settings()


@pytest.fixture(scope="module")
def nhentai_site(nhentai_settings):
    """Get the nhentai SiteConfig."""
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.parsers.factory import DefaultParserFactory

    registry = SiteRegistry(
        nhentai_settings,
        parser_factory=DefaultParserFactory(),
    )
    return registry.get_site("nhentai")


@pytest.fixture(scope="module")
async def nhentai_parser(nhentai_settings, nhentai_site):
    """Create an nhentai parser with a real HttpSession + Playwright."""
    from nexusdl.core.session.cookie_manager import CookieManager
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.core.session.playwright_pool import PlaywrightPool
    from nexusdl.parsers.factory import DefaultParserFactory

    cookies = CookieManager(
        storage_path=nhentai_settings.cookie_storage_path,
        key=nhentai_settings.cookie_encryption_key.get_secret_value().encode()[:32],
    )

    # Playwright (required for Cloudflare)
    playwright = None
    if nhentai_settings.playwright_enabled:
        playwright = PlaywrightPool(
            max_contexts=nhentai_settings.playwright_max_contexts,
        )
        await playwright.start()

    try:
        async with HttpSession(
            nhentai_site,
            nhentai_settings,
            cookie_manager=cookies,
        ) as session:
            factory = DefaultParserFactory()
            parser = factory.create(nhentai_site, session)
            # Inject playwright (nhentai parser needs it)
            if playwright is not None:
                parser._playwright = playwright
            yield parser
    finally:
        if playwright is not None:
            await playwright.stop()


# ────────────────────────────────────────────────────────────────────
# Skip decorator
# ────────────────────────────────────────────────────────────────────


pytestmark = [
    pytest.mark.smoke,
    pytest.mark.integration,
    pytest.mark.skipif(not _AVAILABLE, reason=_SKIP_REASON),
    pytest.mark.timeout(90),  # longer for Cloudflare + Playwright
]


# ────────────────────────────────────────────────────────────────────
# Test 1: health_check
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check(nhentai_parser) -> None:
    """nhentai is reachable (via Cloudflare bypass)."""
    ok = await nhentai_parser.health_check()
    assert ok is True, (
        "nhentai health_check failed. "
        "Check that cf_clearance is valid and Playwright is enabled."
    )


# ────────────────────────────────────────────────────────────────────
# Test 2: search
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_results(nhentai_parser) -> None:
    """search() returns at least one result."""
    results = await nhentai_parser.search(SEARCH_QUERY)
    assert len(results) >= 1, f"search('{SEARCH_QUERY}') returned 0 results"

    result = results[0]
    assert result.site_id == "nhentai"
    assert isinstance(result.title, str) and len(result.title) > 0
    assert result.url.startswith("https://nhentai.net/g/")
    # Content rating is always pornographic
    assert result.content_rating == ContentRating.PORNOGRAPHIC


@pytest.mark.asyncio
async def test_search_pagination(nhentai_parser) -> None:
    """search(page=2) returns different results than page=1."""
    page1 = await nhentai_parser.search(SEARCH_QUERY, page=1)
    await asyncio.sleep(0.5)  # respect rate limit
    page2 = await nhentai_parser.search(SEARCH_QUERY, page=2)

    assert len(page1) >= 1
    if len(page2) > 0:
        urls1 = {r.url for r in page1}
        urls2 = {r.url for r in page2}
        assert urls1 != urls2, "page 1 and page 2 returned identical results"


# ────────────────────────────────────────────────────────────────────
# Test 3: get_manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_manga(nhentai_parser) -> None:
    """get_manga(id) returns a full Manga."""
    results = await nhentai_parser.search(SEARCH_QUERY)
    assert len(results) >= 1, "Cannot test get_manga: no search results"

    # Find a manga ID from the URL
    url = results[0].url
    # nhentai URLs are https://nhentai.net/g/{id}/
    manga_id = url.rstrip("/").split("/")[-1]

    manga = await nhentai_parser.get_manga(manga_id)

    assert manga is not None
    assert manga.site == "nhentai"
    assert len(manga.title) > 0
    assert manga.content_rating == ContentRating.PORNOGRAPHIC
    assert manga.url.startswith("https://nhentai.net/g/")
    assert len(manga.id) == 16  # hash

    # Cover is on t.nhentai.net or i.nhentai.net
    assert manga.cover_url is not None
    cover = str(manga.cover_url)
    assert "nhentai.net" in cover

    # Tags are mapped to genres
    assert isinstance(manga.genres, list)
    # nhentai galleries always have tags


# ────────────────────────────────────────────────────────────────────
# Test 4: get_chapters (nhentai = 1 chapter per gallery)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chapters(nhentai_parser) -> None:
    """get_chapters() returns exactly 1 chapter (a gallery is a chapter)."""
    results = await nhentai_parser.search(SEARCH_QUERY)
    assert len(results) >= 1

    url = results[0].url
    manga_id = url.rstrip("/").split("/")[-1]
    manga = await nhentai_parser.get_manga(manga_id)
    chapters = await nhentai_parser.get_chapters(manga)

    assert len(chapters) == 1, f"Expected 1 chapter, got {len(chapters)}"

    chapter = chapters[0]
    assert isinstance(chapter.id, str) and len(chapter.id) == 16
    assert chapter.manga_id == manga.id
    assert chapter.url.startswith("https://nhentai.net/g/")
    assert chapter.pages_count is not None
    assert chapter.pages_count >= 1


# ────────────────────────────────────────────────────────────────────
# Test 5: get_pages
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pages(nhentai_parser) -> None:
    """get_pages() returns page URLs sorted by index."""
    results = await nhentai_parser.search(SEARCH_QUERY)
    assert len(results) >= 1

    url = results[0].url
    manga_id = url.rstrip("/").split("/")[-1]
    manga = await nhentai_parser.get_manga(manga_id)
    chapters = await nhentai_parser.get_chapters(manga)
    assert len(chapters) >= 1

    pages = await nhentai_parser.get_pages(chapters[0])

    assert len(pages) >= 1, "get_pages() returned 0 pages"

    for i, page in enumerate(pages):
        assert page.index == i, f"Page {i} has index={page.index}"
        assert page.url.startswith("https://"), f"Page {i} URL not absolute"
        assert "nhentai.net" in page.url
        assert page.filename, f"Page {i} filename empty"
        assert page.filename.endswith((".jpg", ".jpeg", ".png", ".webp")), (
            f"Page {i} filename has unexpected extension: {page.filename}"
        )


# ────────────────────────────────────────────────────────────────────
# Test 6: cover URL
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cover_url(nhentai_parser) -> None:
    """The cover URL points to nhentai's CDN."""
    results = await nhentai_parser.search(SEARCH_QUERY)
    assert len(results) >= 1

    manga_id = results[0].url.rstrip("/").split("/")[-1]
    manga = await nhentai_parser.get_manga(manga_id)

    assert manga.cover_url is not None
    cover = str(manga.cover_url)
    # nhentai covers are served from t.nhentai.net (thumbnails)
    # or i.nhentai.net (images)
    assert "nhentai.net" in cover, f"Unexpected cover URL: {cover}"


# ────────────────────────────────────────────────────────────────────
# Test 7: adult flag
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adult_flag(nhentai_site) -> None:
    """nhentai is marked as adult."""
    assert nhentai_site.adult is True


# ────────────────────────────────────────────────────────────────────
# Test 8: Cloudflare bypass
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloudflare_bypass(nhentai_parser, nhentai_settings) -> None:
    """The parser can fetch despite Cloudflare."""
    ok = await nhentai_parser.health_check()
    assert ok is True, (
        "Cloudflare bypass failed. "
        "Ensure Playwright is enabled and cf_clearance cookie is valid."
    )


# ────────────────────────────────────────────────────────────────────
# Test 9: language tag
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_language(nhentai_parser) -> None:
    """nhentai is tagged as English or UNKNOWN (mixed languages)."""
    results = await nhentai_parser.search(SEARCH_QUERY)
    assert len(results) >= 1

    result = results[0]
    # nhentai galleries are often multilingual, tagged as EN or UNKNOWN
    assert result.language in (Language.EN, Language.UNKNOWN)


# ────────────────────────────────────────────────────────────────────
# Test 10: no rate limit triggered
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_rate_limit(nhentai_parser) -> None:
    """3 sequential searches do not trigger 429."""
    for i in range(3):
        results = await nhentai_parser.search(f"test{i}")
        assert isinstance(results, list)
        await asyncio.sleep(0.5)  # nhentai is more aggressive with rate limits
