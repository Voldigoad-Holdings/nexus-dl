# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Smoke tests for the HentaiZone parser.

These tests hit the **real HentaiZone site** and require:

    - Network access
    - Playwright (HentaiZone is Cloudflare-protected)
    - NEXUSDL_ALLOW_ADULT=true
    - A valid ``cf_clearance`` cookie in .env (or the session will be blocked)

They are marked ``@pytest.mark.smoke`` and ``@pytest.mark.integration``,
which means they are:

    - **Skipped by default** (pytest.ini: ``addopts = -m "not smoke"``)
    - **Run manually** with: ``pytest -m smoke``
    - **Run in a scheduled CI job** (weekly, not on PRs)

Why not run on every PR?
    - Network access is slow and flaky
    - HentaiZone rate-limits aggressive clients
    - Cloudflare may ban CI IPs
    - The site content changes (a fixed assertion would break)

Strategy: **tolerant assertions**
    - Assert that a result is returned, not its exact title
    - Assert that pages are URLs, not their exact paths
    - Assert that chapters are sorted, not their exact count
"""
from __future__ import annotations

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
# Skip conditions
# ────────────────────────────────────────────────────────────────────


def _hentaizone_available() -> tuple[bool, str]:
    """Check whether HentaiZone smoke tests can run.

    Returns:
        ``(available, reason)`` — if ``available`` is False, ``reason``
        explains why.
    """
    # 1. Check NEXUSDL_ALLOW_ADULT
    if os.environ.get("NEXUSDL_ALLOW_ADULT", "false").lower() != "true":
        return False, "NEXUSDL_ALLOW_ADULT=false (set to true to run adult site tests)"

    # 2. Check Playwright enabled
    if os.environ.get("NEXUSDL_PLAYWRIGHT_ENABLED", "false").lower() != "true":
        return False, "NEXUSDL_PLAYWRIGHT_ENABLED=false (HentaiZone requires Cloudflare bypass)"

    # 3. Check cf_clearance cookie
    if not os.environ.get("NEXUSDL_HENTAIZONE_CF_CLEARANCE"):
        return False, (
            "NEXUSDL_HENTAIZONE_CF_CLEARANCE not set "
            "(extract from browser: DevTools → Application → Cookies)"
        )

    return True, ""


_AVAILABLE, _SKIP_REASON = _hentaizone_available()


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def hentaizone_settings():
    """Load real settings (must include cf_clearance)."""
    from nexusdl.core.config import load_settings

    return load_settings()


@pytest.fixture(scope="module")
def hentaizone_site(hentaizone_settings):
    """Get the HentaiZone SiteConfig from sites.yaml."""
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.parsers.factory import DefaultParserFactory

    registry = SiteRegistry(
        hentaizone_settings,
        parser_factory=DefaultParserFactory(),
    )
    return registry.get_site("hentaizone")


@pytest.fixture(scope="module")
async def hentaizone_parser(hentaizone_settings, hentaizone_site):
    """Create a HentaiZone parser with a real HttpSession."""
    from nexusdl.core.session.cookie_manager import CookieManager
    from nexusdl.core.session.flaresolverr import FlareSolverr
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.core.session.playwright_pool import PlaywrightPool
    from nexusdl.parsers.factory import DefaultParserFactory

    # Cookie manager (required for cf_clearance)
    cookies = CookieManager(
        storage_path=hentaizone_settings.cookie_storage_path,
        key=hentaizone_settings.cookie_encryption_key.get_secret_value().encode()[:32],
    )

    # Playwright pool (required for Cloudflare)
    playwright = None
    if hentaizone_settings.playwright_enabled:
        playwright = PlaywrightPool(
            max_contexts=hentaizone_settings.playwright_max_contexts,
        )
        await playwright.start()

    # FlareSolverr (optional)
    flaresolverr = None
    if hentaizone_settings.flaresolverr_url:
        flaresolverr = FlareSolverr(hentaizone_settings.flaresolverr_url)

    try:
        async with HttpSession(
            hentaizone_site,
            hentaizone_settings,
            cookie_manager=cookies,
        ) as session:
            factory = DefaultParserFactory()
            parser = factory.create(hentaizone_site, session)
            # Inject playwright (some parsers need it)
            if playwright is not None:
                parser._playwright = playwright
            if flaresolverr is not None:
                parser._flaresolverr = flaresolverr
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
    pytest.mark.timeout(60),
]


# ────────────────────────────────────────────────────────────────────
# Test 1: health_check
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check(hentaizone_parser) -> None:
    """The site responds to a HEAD/GET on the base URL."""
    ok = await hentaizone_parser.health_check()
    assert ok is True, "HentaiZone health_check failed (site may be down or Cloudflare blocking)"


# ────────────────────────────────────────────────────────────────────
# Test 2: search
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_results(hentaizone_parser) -> None:
    """search() returns at least one result for a common query.

    We use a generic query ("a") because site content changes.
    Tolerant assertion: at least 1 result, no fixed count.
    """
    results = await hentaizone_parser.search("a")
    assert len(results) >= 1, "search() returned 0 results (site structure may have changed)"

    result = results[0]
    assert isinstance(result.site_id, str) and result.site_id == "hentaizone"
    assert isinstance(result.title, str) and len(result.title) > 0
    assert result.url.startswith("http")
    assert result.language == Language.EN or result.language == Language.UNKNOWN


# ────────────────────────────────────────────────────────────────────
# Test 3: search pagination
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_pagination(hentaizone_parser) -> None:
    """search(page=2) returns different results than page=1."""
    page1 = await hentaizone_parser.search("a", page=1)
    page2 = await hentaizone_parser.search("a", page=2)

    # Both should return results
    assert len(page1) >= 1
    # Page 2 may be empty on some sites (last page)
    if len(page2) > 0:
        # Different results (URLs should not overlap)
        urls1 = {r.url for r in page1}
        urls2 = {r.url for r in page2}
        assert urls1 != urls2, "page 1 and page 2 returned identical results"


# ────────────────────────────────────────────────────────────────────
# Test 4: get_manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_manga(hentaizone_parser) -> None:
    """get_manga(url) returns a full Manga object."""
    # First, find a manga via search
    results = await hentaizone_parser.search("a")
    assert len(results) >= 1, "Cannot test get_manga: no search results"

    url = results[0].url
    manga = await hentaizone_parser.get_manga(url)

    assert manga is not None
    assert manga.site == "hentaizone"
    assert len(manga.title) > 0
    assert manga.url.startswith("http")
    assert manga.language in (Language.EN, Language.UNKNOWN)
    assert manga.content_rating in (
        ContentRating.EROTICA,
        ContentRating.PORNOGRAPHIC,
    )
    # author may be None (some sites don't list it)
    assert manga.author is None or isinstance(manga.author, str)


# ────────────────────────────────────────────────────────────────────
# Test 5: get_chapters
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chapters_sorted(hentaizone_parser) -> None:
    """get_chapters() returns chapters sorted by number ascending."""
    results = await hentaizone_parser.search("a")
    assert len(results) >= 1, "Cannot test get_chapters: no search results"

    manga = await hentaizone_parser.get_manga(results[0].url)
    chapters = await hentaizone_parser.get_chapters(manga)

    assert len(chapters) >= 1, "get_chapters() returned 0 chapters"

    # Verify chapter structure
    chapter = chapters[0]
    assert isinstance(chapter.id, str) and len(chapter.id) == 16
    assert chapter.manga_id == manga.id
    assert chapter.url.startswith("http")
    assert chapter.language == manga.language

    # Verify sorting (numbers should be ascending)
    # Note: some chapters may have non-numeric numbers (e.g. "Extra"),
    # so we only check numeric ones.
    numeric_numbers = [
        float(c.number) for c in chapters
        if isinstance(c.number, (int, float)) or (
            isinstance(c.number, str) and c.number.replace(".", "").isdigit()
        )
    ]
    if len(numeric_numbers) > 1:
        assert numeric_numbers == sorted(numeric_numbers), (
            "Chapters not sorted ascending"
        )


# ────────────────────────────────────────────────────────────────────
# Test 6: get_pages
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pages(hentaizone_parser) -> None:
    """get_pages() returns page URLs sorted by index."""
    results = await hentaizone_parser.search("a")
    assert len(results) >= 1, "Cannot test get_pages: no search results"

    manga = await hentaizone_parser.get_manga(results[0].url)
    chapters = await hentaizone_parser.get_chapters(manga)
    assert len(chapters) >= 1, "Cannot test get_pages: no chapters"

    pages = await hentaizone_parser.get_pages(chapters[0])

    assert len(pages) >= 1, "get_pages() returned 0 pages"

    # Verify structure
    for i, page in enumerate(pages):
        assert page.index == i, f"Page {i} has index={page.index}"
        assert page.url.startswith("http"), f"Page {i} URL not absolute"
        assert page.filename, f"Page {i} filename empty"
        assert page.filename.endswith((".jpg", ".jpeg", ".png", ".webp")), (
            f"Page {i} filename has unexpected extension: {page.filename}"
        )


# ────────────────────────────────────────────────────────────────────
# Test 7: adult flag
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adult_flag(hentaizone_site) -> None:
    """HentaiZone is marked as adult."""
    assert hentaizone_site.adult is True, "HentaiZone should be marked adult=True"


# ────────────────────────────────────────────────────────────────────
# Test 8: language
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_language(hentaizone_parser) -> None:
    """HentaiZone is English (or UNKNOWN if mixed)."""
    assert hentaizone_parser.language in ("en", Language.EN)


# ────────────────────────────────────────────────────────────────────
# Test 9: Cloudflare bypass
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloudflare_bypass(hentaizone_parser, hentaizone_settings) -> None:
    """The parser can fetch a page despite Cloudflare."""
    # health_check already fetches the base URL, which is behind CF
    ok = await hentaizone_parser.health_check()
    assert ok is True, (
        "Cloudflare bypass failed. "
        "Ensure Playwright is enabled and cf_clearance cookie is valid."
    )


# ────────────────────────────────────────────────────────────────────
# Test 10: no rate limit triggered
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_rate_limit(hentaizone_parser) -> None:
    """3 sequential searches do not trigger 429 (rate limit respected)."""
    for i in range(3):
        results = await hentaizone_parser.search(f"test{i}")
        # If rate limit is hit, the parser would raise SessionRateLimitError
        # (or return empty due to retry exhaustion). We assert no exception.
        assert isinstance(results, list)
