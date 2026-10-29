# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Pytest configuration and shared fixtures.

This file is loaded automatically by pytest. It provides:

    - `settings` (isolated Settings with fake secrets)
    - `event_bus` (fresh EventBus)
    - `collected_events` (list of published events)
    - `library_db` (LibraryDatabase in tmp_path)
    - `fake_manga`, `fake_chapter`, `fake_pages` (valid models)
    - `fake_site`, `fake_site_capabilities` (valid SiteConfig)
    - `FakeParser`, `FakeParserFactory` (no network)
    - `parser_factory` (instance of FakeParserFactory)
    - `fake_http_session` (MagicMock)
    - `respx_mock` (global respx router)
    - `tmp_download_dir`, `tmp_logs_dir` (isolated dirs)

Auto-use fixtures:
    - `clean_env` (clean NEXUSDL_* env vars)
    - `clean_logger` (clean loguru handlers)
    - `reset_registry` (reset parser registry)

Markers (registered in pytest_configure):
    - `unit`: fast, no I/O
    - `integration`: real filesystem, SQLite
    - `smoke`: real network (skipped by default)
    - `slow`: slow tests (skipped by default)
    - `network`: requires network (skipped by default)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.events import Event
    from nexusdl.core.library.database import LibraryDatabase
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.models.page import Page
    from nexusdl.core.models.site import SiteCapabilities, SiteConfig
    from nexusdl.parsers.base import BaseParser


# ════════════════════════════════════════════════════════════════════
# PYTEST CONFIGURATION
# ════════════════════════════════════════════════════════════════════


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: fast, no I/O")
    config.addinivalue_line("markers", "integration: real filesystem, SQLite")
    config.addinivalue_line("markers", "smoke: real network (skipped by default)")
    config.addinivalue_line("markers", "slow: slow tests (skipped by default)")
    config.addinivalue_line("markers", "network: requires network (skipped by default)")


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip smoke/slow/network tests unless explicitly requested."""
    skip_smoke = pytest.mark.skip(reason="smoke test (use -m smoke to run)")
    skip_slow = pytest.mark.skip(reason="slow test (use -m slow to run)")
    skip_network = pytest.mark.skip(reason="network test (use -m network to run)")

    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(skip_smoke)
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
        if "network" in item.keywords:
            item.add_marker(skip_network)


# ════════════════════════════════════════════════════════════════════
# AUTO-USE FIXTURES (cleanup between tests)
# ════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all NEXUSDL_* env vars before/after each test."""
    for key in list(os.environ):
        if key.startswith("NEXUSDL_"):
            monkeypatch.delenv(key, raising=False)
    yield
    for key in list(os.environ):
        if key.startswith("NEXUSDL_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def clean_logger() -> None:
    """Remove all loguru handlers before/after each test."""
    try:
        from loguru import logger
    except ImportError:
        yield
        return
    logger.remove()
    yield
    logger.remove()


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    """Reset the global parser registry before/after each test."""
    try:
        from nexusdl.parsers.factory import reset_registry as _reset
    except ImportError:
        yield
        return
    _reset()
    yield
    _reset()


# ════════════════════════════════════════════════════════════════════
# SETTINGS
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def settings(tmp_path: Path) -> "Settings":
    """Isolated Settings with fake secrets and tmp_path.

    - `.env` is NOT loaded (`_env_file=None`)
    - All paths are under `tmp_path`
    - Secrets are fake but valid (64 hex chars)
    - `allow_adult=False`, `playwright_enabled=False`
    """
    from nexusdl.core.config import Settings

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_name="NexusDL-Test",
        app_environment="dev",
        public_url="https://test.example.com",
        api_base_url="https://api.test.example.com",
        frontend_url="https://test.example.com",
        secret_key="a" * 64,
        jwt_secret="b" * 64,
        cookie_encryption_key="c" * 64,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/library.db",
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "temp",
        library_db_path=tmp_path / "library.db",
        cookie_storage_path=tmp_path / "cookies.enc",
        logs_dir=tmp_path / "logs",
        allow_adult=False,
        playwright_enabled=False,
        log_level="DEBUG",
        log_format="text",
        max_concurrent_downloads=2,
        max_concurrent_pages_per_chapter=2,
        default_rate_limit_per_second=100.0,
    )


@pytest.fixture
def settings_prod(tmp_path: Path) -> "Settings":
    """Settings in production mode (for testing prod-specific behavior)."""
    from nexusdl.core.config import Settings

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_name="NexusDL-Prod",
        app_environment="prod",
        public_url="https://nexusdl.example.com",
        api_base_url="https://api.nexusdl.example.com",
        frontend_url="https://nexusdl.example.com",
        secret_key="d" * 64,
        jwt_secret="e" * 64,
        cookie_encryption_key="f" * 64,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/library.db",
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "temp",
        library_db_path=tmp_path / "library.db",
        cookie_storage_path=tmp_path / "cookies.enc",
        logs_dir=tmp_path / "logs",
        allow_adult=False,
        playwright_enabled=False,
        log_level="INFO",
        log_format="json",
    )


# ════════════════════════════════════════════════════════════════════
# EVENTBUS
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus() -> "EventBus":
    """Fresh EventBus."""
    from nexusdl.core.events import EventBus

    return EventBus()


@pytest.fixture
def collected_events(event_bus: "EventBus") -> list["Event"]:
    """List that collects all events published on the bus."""
    events: list["Event"] = []

    async def _collect(event: "Event") -> None:
        events.append(event)

    # Subscribe to all common event types
    for event_type in (
        "download.started",
        "download.progress",
        "download.completed",
        "download.failed",
        "download.paused",
        "download.resumed",
        "download.cancelled",
        "chapter.started",
        "chapter.completed",
        "chapter.failed",
        "library.updated",
        "site.health.changed",
        "*",  # wildcard if supported
    ):
        event_bus.subscribe(event_type, _collect)

    return events


# ════════════════════════════════════════════════════════════════════
# DIRECTORIES
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def tmp_download_dir(tmp_path: Path) -> Path:
    """Isolated download directory."""
    d = tmp_path / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def tmp_logs_dir(tmp_path: Path) -> Path:
    """Isolated logs directory."""
    d = tmp_path / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ════════════════════════════════════════════════════════════════════
# DOMAIN MODELS (fake but valid)
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_site_capabilities() -> "SiteCapabilities":
    """Valid SiteCapabilities."""
    from nexusdl.core.models.site import SiteCapabilities

    return SiteCapabilities(
        supports_search=True,
        supports_manga_info=True,
        supports_chapters=True,
        supports_pages=True,
        supports_download=True,
        requires_auth=False,
        requires_cloudflare_bypass=False,
        max_concurrent_downloads=4,
        default_rate_limit_per_second=100.0,
    )


@pytest.fixture
def fake_site(fake_site_capabilities: "SiteCapabilities") -> "SiteConfig":
    """Valid SiteConfig."""
    from nexusdl.core.models.enums import Language
    from nexusdl.core.models.site import SiteConfig

    return SiteConfig(
        id="test-site",
        name="Test Site",
        domains=["https://test.example.com"],
        parser_class="nexusdl.parsers.en.test:TestParser",
        language=Language.EN,
        adult=False,
        capabilities=fake_site_capabilities,
        default_headers={"User-Agent": "NexusDL-Test/0.1.0"},
        cookies_required=[],
    )


@pytest.fixture
def fake_manga() -> "Manga":
    """Valid Manga."""
    from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
    from nexusdl.core.models.manga import Manga

    return Manga(
        id="a1b2c3d4e5f6a7b8",
        source_id="a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6",
        site="test-site",
        title="One Piece",
        alternative_titles=["ワンピース"],
        description="A story about pirates.",
        author="Oda Eiichiro",
        artist="Oda Eiichiro",
        genres=["Action", "Adventure"],
        status=MangaStatus.ONGOING,
        year=1997,
        language=Language.EN,
        content_rating=ContentRating.SAFE,
        url="https://test.example.com/title/a1b2c3d4e5f6a7b8",
        updated_at=datetime.now(tz=timezone.utc),
    )


@pytest.fixture
def fake_chapter(fake_manga: "Manga") -> "Chapter":
    """Valid Chapter."""
    from nexusdl.core.models.chapter import Chapter
    from nexusdl.core.models.enums import Language

    return Chapter(
        id="b3e5d7a9c1f2e4d6",
        manga_id=fake_manga.id,
        source_id="ch-uuid",
        title="Romance Dawn",
        number=1.0,
        volume=1,
        language=Language.EN,
        pages_count=3,
        published_at=datetime.now(tz=timezone.utc),
        url="https://test.example.com/chapter/ch-uuid",
    )


@pytest.fixture
def fake_pages() -> list["Page"]:
    """3 valid Pages."""
    from nexusdl.core.models.page import Page

    return [
        Page(
            index=i,
            url=f"https://test.example.com/pages/{i + 1:03d}.jpg",
            filename=f"{i + 1:03d}.jpg",
        )
        for i in range(3)
    ]


# ════════════════════════════════════════════════════════════════════
# FAKE PARSER (no network)
# ════════════════════════════════════════════════════════════════════


class FakeParser:
    """Fake parser that returns canned data (no network).

    Implements the BaseParser contract without any I/O.
    """

    site_id = "test-site"
    language = "en"
    adult = False

    def __init__(self, site: "SiteConfig", session: Any, **_kwargs: Any) -> None:
        self.site = site
        self.session = session

    async def search(self, query: str, *, page: int = 1) -> list[Any]:
        """Return 1 canned SearchResult."""
        from nexusdl.core.models.enums import ContentRating, Language
        from nexusdl.core.models.search import SearchResult

        return [
            SearchResult(
                site_id=self.site_id,
                site_name=self.site.name,
                manga_id="a1b2c3d4e5f6a7b8",
                title=f"{query} (result)",
                cover_url=None,
                author="Test Author",
                language=Language.EN,
                content_rating=ContentRating.SAFE,
                url="https://test.example.com/manga/1",
            )
        ]

    async def get_manga(self, url_or_id: str) -> "Manga":
        """Return a canned Manga."""
        from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
        from nexusdl.core.models.manga import Manga

        return Manga(
            id="a1b2c3d4e5f6a7b8",
            source_id=url_or_id,
            site=self.site_id,
            title="One Piece",
            status=MangaStatus.ONGOING,
            language=Language.EN,
            content_rating=ContentRating.SAFE,
            url="https://test.example.com/manga/1",
            updated_at=datetime.now(tz=timezone.utc),
        )

    async def get_chapters(self, manga: "Manga") -> list["Chapter"]:
        """Return 1 canned Chapter."""
        from nexusdl.core.models.chapter import Chapter
        from nexusdl.core.models.enums import Language

        return [
            Chapter(
                id="b3e5d7a9c1f2e4d6",
                manga_id=manga.id,
                source_id="ch-uuid",
                title="Romance Dawn",
                number=1.0,
                language=Language.EN,
                pages_count=3,
                url="https://test.example.com/chapter/ch-uuid",
            )
        ]

    async def get_pages(self, chapter: "Chapter") -> list["Page"]:
        """Return 3 canned Pages."""
        from nexusdl.core.models.page import Page

        return [
            Page(
                index=i,
                url=f"https://test.example.com/pages/{i + 1:03d}.jpg",
                filename=f"{i + 1:03d}.jpg",
            )
            for i in range(3)
        ]

    async def download_page(self, page: "Page", dest: Path) -> Path:
        """Fake download (write 1 byte)."""
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / page.filename
        out.write_bytes(b"fake-image-bytes")
        return out

    async def health_check(self) -> bool:
        """Always healthy."""
        return True


class FakeParserFactory:
    """Fake ParserFactory (ADR-003) that returns FakeParser."""

    def __init__(self) -> None:
        self.calls: list[tuple["SiteConfig", Any]] = []

    def create(self, site: "SiteConfig", session: Any) -> "BaseParser":
        """Create a FakeParser and record the call."""
        self.calls.append((site, session))
        return FakeParser(site, session)  # type: ignore[return-value]


@pytest.fixture
def parser_factory() -> FakeParserFactory:
    """Instance of FakeParserFactory."""
    return FakeParserFactory()


@pytest.fixture
def fake_parser(fake_site: "SiteConfig") -> FakeParser:
    """Instance of FakeParser."""
    return FakeParser(fake_site, session=None)


# ════════════════════════════════════════════════════════════════════
# FAKE HTTP SESSION
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_http_session() -> MagicMock:
    """Mock HttpSession (no network)."""
    session = MagicMock(
        spec=[
            "get",
            "post",
            "get_html",
            "get_json",
            "stream_download",
            "__aenter__",
            "__aexit__",
            "close",
        ]
    )
    session.get = AsyncMock()
    session.post = AsyncMock()
    session.get_html = AsyncMock(return_value="<html></html>")
    session.get_json = AsyncMock(return_value={})
    session.stream_download = AsyncMock(return_value=Path("/tmp/fake"))
    session.close = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


# ════════════════════════════════════════════════════════════════════
# LIBRARY DATABASE
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
async def library_db(settings: "Settings") -> "LibraryDatabase":
    """Isolated LibraryDatabase with migrations applied."""
    from nexusdl.core.library.database import LibraryDatabase

    db = LibraryDatabase(settings.database_url)
    await db.init()
    try:
        yield db
    finally:
        await db.close()


# ════════════════════════════════════════════════════════════════════
# RESPX (global HTTP mock)
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def respx_mock() -> Any:
    """Provide a respx router (auto-cleaned)."""
    try:
        import respx
    except ImportError:
        pytest.skip("respx not installed")

    with respx.mock(assert_all_called=False) as router:
        yield router
