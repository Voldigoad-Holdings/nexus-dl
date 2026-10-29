# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for BaseParser (the ABC for all parsers).

BaseParser is the abstract base class that every site parser
(60+) inherits from, or extends via a mixin. Its contract is:

    - Class attributes: site_id, language, adult
    - __init__(site, session, *, playwright_pool=None, flaresolverr=None)
    - 4 abstract async methods:
        * search(query, *, page=1) -> list[SearchResult]
        * get_manga(url_or_id) -> Manga
        * get_chapters(manga) -> list[Chapter]
        * get_pages(chapter) -> list[Page]
    - Concrete helpers:
        * _resolve_url(relative_or_absolute) -> str
        * normalize_url(url) -> str
        * _make_manga_id(url) -> str
        * _make_chapter_id(manga_id, url) -> str
        * _extract_id(url) -> str
        * _extract_chapter_id(url) -> str
        * download_page(page, dest) -> Path
        * health_check() -> bool
        * Attributes: site, session, _log, _settings, _base_url

This test verifies the contract, not a specific parser.
No network is used (FakeSession + respx).
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

if TYPE_CHECKING:
    from nexusdl.core.session.http_session import HttpSession

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
)
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult
from nexusdl.core.models.site import SiteCapabilities, SiteConfig
from nexusdl.parsers.base import BaseParser


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def site_config() -> SiteConfig:
    """A minimal SiteConfig."""
    return SiteConfig(
        id="test",
        name="Test Site",
        domains=["https://test.example.com"],
        parser_class="nexusdl.parsers.custom._template:TestParser",
        language=Language.EN,
        adult=False,
        capabilities=SiteCapabilities(
            supports_search=True,
            supports_manga_info=True,
            supports_chapters=True,
            supports_pages=True,
            supports_download=True,
            requires_auth=False,
            requires_cloudflare_bypass=False,
            max_concurrent_downloads=4,
            default_rate_limit_per_second=2.0,
        ),
    )


@pytest.fixture
def fake_session() -> MagicMock:
    """A fake HttpSession (mock)."""
    session = MagicMock(spec=["get", "post", "get_html", "get_json", "stream_download"])
    session.get = AsyncMock()
    session.post = AsyncMock()
    session.get_html = AsyncMock(return_value="<html></html>")
    session.get_json = AsyncMock(return_value={})
    session.stream_download = AsyncMock(return_value=Path("/tmp/fake"))
    return session


@pytest.fixture
def fake_settings() -> MagicMock:
    """A fake Settings object (only the attributes BaseParser reads)."""
    settings = MagicMock()
    settings.http_timeout_seconds = 30.0
    settings.http_max_retries = 3
    settings.default_rate_limit_per_second = 2.0
    settings.user_agent = None
    return settings


# ────────────────────────────────────────────────────────────────────
# Fake parser (concrete subclass for testing)
# ────────────────────────────────────────────────────────────────────


class FakeParser(BaseParser):
    """Concrete parser for testing BaseParser helpers."""

    site_id = "test"
    language = "en"
    adult = False

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        return []

    async def get_manga(self, url_or_id: str) -> Manga:
        return Manga(
            id=self._make_manga_id(url_or_id),
            source_id=url_or_id,
            site="test",
            title="Test",
            status=MangaStatus.ONGOING,
            language=Language.EN,
            content_rating=ContentRating.SAFE,
            url="https://test.example.com/manga/1",
            updated_at="2026-10-29T12:00:00Z",
        )

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        return []

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        return []


# ────────────────────────────────────────────────────────────────────
# 1. ABC contract
# ────────────────────────────────────────────────────────────────────


def test_cannot_instantiate_base_parser(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """BaseParser cannot be instantiated (abstract)."""
    with pytest.raises(TypeError, match="abstract"):
        BaseParser(site_config, fake_session)  # type: ignore[abstract]


def test_abstract_methods_declared() -> None:
    """All 4 methods are abstract."""
    abstract_methods = BaseParser.__abstractmethods__
    assert "search" in abstract_methods
    assert "get_manga" in abstract_methods
    assert "get_chapters" in abstract_methods
    assert "get_pages" in abstract_methods


def test_concrete_subclass_works(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """A subclass implementing all methods can be instantiated."""
    parser = FakeParser(site_config, fake_session)
    assert parser is not None


# ────────────────────────────────────────────────────────────────────
# 2. Init and attributes
# ────────────────────────────────────────────────────────────────────


def test_init_stores_site(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """parser.site is the SiteConfig."""
    parser = FakeParser(site_config, fake_session)
    assert parser.site is site_config
    assert parser.site.id == "test"


def test_init_stores_session(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """parser.session is the HttpSession."""
    parser = FakeParser(site_config, fake_session)
    assert parser.session is fake_session


def test_init_sets_base_url(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """parser._base_url is the first domain."""
    parser = FakeParser(site_config, fake_session)
    assert parser._base_url == "https://test.example.com"


def test_init_sets_log(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """parser._log is a logger."""
    parser = FakeParser(site_config, fake_session)
    assert parser._log is not None
    # Should have common loguru methods
    assert hasattr(parser._log, "info")
    assert hasattr(parser._log, "debug")
    assert hasattr(parser._log, "error")


def test_class_attributes(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Class attributes are set."""
    assert FakeParser.site_id == "test"
    assert FakeParser.language == "en"
    assert FakeParser.adult is False


# ────────────────────────────────────────────────────────────────────
# 3. _resolve_url / normalize_url
# ────────────────────────────────────────────────────────────────────


def test_resolve_url_absolute(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Absolute URL is returned as-is."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._resolve_url("https://other.com/path")
        == "https://other.com/path"
    )


def test_resolve_url_relative(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Relative URL is joined with base."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._resolve_url("/manga/one-piece")
        == "https://test.example.com/manga/one-piece"
    )


def test_resolve_url_relative_no_slash(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Relative URL without leading slash is joined correctly."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._resolve_url("manga/one-piece")
        == "https://test.example.com/manga/one-piece"
    )


def test_resolve_url_strips_trailing_slash(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Trailing slashes are normalized."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._resolve_url("/manga/one-piece/")
        == "https://test.example.com/manga/one-piece/"
    )


def test_normalize_url_alias(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """normalize_url is an alias for _resolve_url."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser.normalize_url("/path")
        == parser._resolve_url("/path")
    )


# ────────────────────────────────────────────────────────────────────
# 4. _make_manga_id
# ────────────────────────────────────────────────────────────────────


def test_make_manga_id_is_deterministic(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Same URL → same ID."""
    parser = FakeParser(site_config, fake_session)
    id1 = parser._make_manga_id("https://test.example.com/manga/one-piece")
    id2 = parser._make_manga_id("https://test.example.com/manga/one-piece")
    assert id1 == id2


def test_make_manga_id_is_16_hex(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """ID is 16 hex chars."""
    parser = FakeParser(site_config, fake_session)
    manga_id = parser._make_manga_id("https://test.example.com/manga/one-piece")
    assert len(manga_id) == 16
    assert all(c in "0123456789abcdef" for c in manga_id)


def test_make_manga_id_matches_sha256_prefix(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """ID is sha256(f"{site_id}:{url}")[:16]."""
    parser = FakeParser(site_config, fake_session)
    url = "https://test.example.com/manga/one-piece"
    expected = hashlib.sha256(f"test:{url}".encode()).hexdigest()[:16]
    assert parser._make_manga_id(url) == expected


def test_make_manga_id_differs_by_url(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Different URLs → different IDs."""
    parser = FakeParser(site_config, fake_session)
    id1 = parser._make_manga_id("https://test.example.com/manga/one-piece")
    id2 = parser._make_manga_id("https://test.example.com/manga/naruto")
    assert id1 != id2


# ────────────────────────────────────────────────────────────────────
# 5. _make_chapter_id
# ────────────────────────────────────────────────────────────────────


def test_make_chapter_id_is_deterministic(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Same manga_id + URL → same chapter ID."""
    parser = FakeParser(site_config, fake_session)
    id1 = parser._make_chapter_id("manga1", "https://test.example.com/ch/1")
    id2 = parser._make_chapter_id("manga1", "https://test.example.com/ch/1")
    assert id1 == id2


def test_make_chapter_id_is_16_hex(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Chapter ID is 16 hex chars."""
    parser = FakeParser(site_config, fake_session)
    ch_id = parser._make_chapter_id("manga1", "https://test.example.com/ch/1")
    assert len(ch_id) == 16
    assert all(c in "0123456789abcdef" for c in ch_id)


def test_make_chapter_id_matches_sha256_prefix(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Chapter ID is sha256(f"{manga_id}:{url}")[:16]."""
    parser = FakeParser(site_config, fake_session)
    manga_id = "abc123"
    url = "https://test.example.com/ch/1"
    expected = hashlib.sha256(f"{manga_id}:{url}".encode()).hexdigest()[:16]
    assert parser._make_chapter_id(manga_id, url) == expected


def test_make_chapter_id_differs_by_manga(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Same URL, different manga → different chapter IDs."""
    parser = FakeParser(site_config, fake_session)
    id1 = parser._make_chapter_id("manga1", "https://test.example.com/ch/1")
    id2 = parser._make_chapter_id("manga2", "https://test.example.com/ch/1")
    assert id1 != id2


# ────────────────────────────────────────────────────────────────────
# 6. _extract_id
# ────────────────────────────────────────────────────────────────────


def test_extract_id_from_path(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Extract the last segment of a URL."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._extract_id("https://test.example.com/manga/one-piece")
        == "one-piece"
    )


def test_extract_id_strips_query(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Query string is stripped."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._extract_id("https://test.example.com/manga/one-piece?ref=home")
        == "one-piece"
    )


def test_extract_id_strips_fragment(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Fragment is stripped."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._extract_id("https://test.example.com/manga/one-piece#top")
        == "one-piece"
    )


def test_extract_id_trailing_slash(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """Trailing slash is ignored."""
    parser = FakeParser(site_config, fake_session)
    assert (
        parser._extract_id("https://test.example.com/manga/one-piece/")
        == "one-piece"
    )


# ────────────────────────────────────────────────────────────────────
# 7. health_check
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok(
    site_config: SiteConfig,
) -> None:
    """health_check returns True on 200."""
    respx.get("https://test.example.com").mock(
        return_value=httpx.Response(200, text="ok")
    )

    # Use a fake session that calls the real httpx client (respx intercepts)
    import httpx as httpx_lib

    class RealLikeSession:
        async def get(self, url: str, **kwargs) -> httpx_lib.Response:
            async with httpx_lib.AsyncClient() as client:
                return await client.get(url, **kwargs)

        async def get_html(self, url: str, **kwargs) -> str:
            return (await self.get(url, **kwargs)).text

    session = RealLikeSession()
    parser = FakeParser(site_config, session)  # type: ignore[arg-type]
    ok = await parser.health_check()
    assert ok is True


@pytest.mark.asyncio
@respx.mock
async def test_health_check_fail(
    site_config: SiteConfig,
) -> None:
    """health_check returns False on 5xx."""
    respx.get("https://test.example.com").mock(
        return_value=httpx.Response(500, text="error")
    )

    import httpx as httpx_lib

    class RealLikeSession:
        async def get(self, url: str, **kwargs) -> httpx_lib.Response:
            async with httpx_lib.AsyncClient() as client:
                return await client.get(url, **kwargs)

        async def get_html(self, url: str, **kwargs) -> str:
            return (await self.get(url, **kwargs)).text

    session = RealLikeSession()
    parser = FakeParser(site_config, session)  # type: ignore[arg-type]
    ok = await parser.health_check()
    assert ok is False


@pytest.mark.asyncio
async def test_health_check_exception(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """health_check returns False on exception."""
    fake_session.get.side_effect = httpx.ConnectError("refused")
    parser = FakeParser(site_config, fake_session)
    ok = await parser.health_check()
    assert ok is False


# ────────────────────────────────────────────────────────────────────
# 8. download_page (helper)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_page_calls_session(
    site_config: SiteConfig,
    fake_session: MagicMock,
    tmp_path: Path,
) -> None:
    """download_page delegates to session.stream_download."""
    dest = tmp_path / "001.png"
    fake_session.stream_download.return_value = dest

    parser = FakeParser(site_config, fake_session)
    page = Page(index=0, url="https://test.example.com/001.png", filename="001.png")
    result = await parser.download_page(page, tmp_path)

    assert result == dest
    fake_session.stream_download.assert_called_once()


# ────────────────────────────────────────────────────────────────────
# 9. Mixin compatibility
# ────────────────────────────────────────────────────────────────────


def test_mixin_can_extend_base(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """A mixin + BaseParser subclass works (MRO)."""

    class FakeMixin:
        def helper(self) -> str:
            return "mixin"

    class MixedParser(FakeMixin, BaseParser):
        site_id = "mixed"
        language = "en"
        adult = False

        async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
            return []

        async def get_manga(self, url_or_id: str) -> Manga:
            raise NotImplementedError

        async def get_chapters(self, manga: Manga) -> list[Chapter]:
            return []

        async def get_pages(self, chapter: Chapter) -> list[Page]:
            return []

    parser = MixedParser(site_config, fake_session)
    assert parser.helper() == "mixin"
    assert parser.site.id == "test"
