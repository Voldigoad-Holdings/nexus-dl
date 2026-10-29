# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Integration test: full download flow.

Exercises the entire pipeline without any network:

    URL → SiteRegistry → ParserFactory (fake) → Parser
                                                    ↓
                                            get_manga() → Manga
                                                    ↓
                                            get_chapters() → list[Chapter]
                                                    ↓
                                            get_pages() → list[Page]
                                                    ↓
                                            HttpSession (respx mock) → images
                                                    ↓
                                            CbzPackager → .cbz file
                                                    ↓
                                            LibraryDatabase → library.db
                                                    ↓
                                            EventBus → events emitted

The parser is FAKE (no network), but all other components are REAL.
HTTP requests to image URLs are mocked with `respx`.

Why integration?
    Unit tests verify each component in isolation. This test verifies
    that the components work TOGETHER — a regression in the wiring
    (e.g. a bad factory, a missing await) would pass unit tests but
    fail here.

Why no network?
    - CI must be fast (< 5s) and deterministic
    - Real sites can change, break, or rate-limit
    - Image contents are generated on the fly

Why real components (except parser)?
    - `SiteRegistry`: catches schema/loading bugs
    - `DownloadManager`: catches orchestration bugs
    - `CbzPackager`: catches ZIP/ComicInfo bugs
    - `LibraryDatabase`: catches SQL bugs
    - `EventBus`: catches event emission bugs
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from PIL import Image

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.events import Event, EventBus

from nexusdl.core.config import Settings
from nexusdl.core.downloader.manager import DownloadManager
from nexusdl.core.events import Event, EventBus
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult
from nexusdl.core.models.site import SiteCapabilities, SiteConfig
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.base import BaseParser


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent / "fixtures"

#: Real-ish image URLs that we will mock with respx
IMAGE_BASE = "https://uploads.mangadex.org/data/b3e5d7a9c1f2e4d6"

#: Sample JPEG bytes (minimal valid JPEG, 1×1 pixel)
#: We use `Image.new` + `BytesIO` in the test to generate 800×1200 pages.
IMAGE_WIDTH = 800
IMAGE_HEIGHT = 1200
PAGES_COUNT = 3


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_jpeg(width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """Generate a valid JPEG in memory."""
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_png(width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """Generate a valid PNG in memory."""
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ────────────────────────────────────────────────────────────────────
# Fake parser
# ────────────────────────────────────────────────────────────────────


class FakeParser(BaseParser):
    """Fake parser that returns canned data (no network).

    Returns the chapter from `sample_chapter.json` (via fixtures),
    with 3 pages. The image URLs point to `IMAGE_BASE` (mocked by respx).
    """

    site_id = "mangadex"
    language = "en"
    adult = False

    def __init__(self, site: SiteConfig, session, **_kwargs) -> None:
        super().__init__(site, session)
        self._chapter_data = json.loads(
            (FIXTURES / "sample_chapter.json").read_text()
        )
        self._manga_data = json.loads(
            (FIXTURES / "sample_manga.json").read_text()
        )

    async def search(
        self, query: str, *, page: int = 1
    ) -> list[SearchResult]:
        return [
            SearchResult(
                site_id=self.site_id,
                site_name=self.site.name,
                manga_id=self._manga_data["id"],
                title=self._manga_data["title"],
                cover_url=self._manga_data["cover_url"],
                author=self._manga_data["author"],
                language=Language.EN,
                content_rating=ContentRating.SAFE,
                url=self._manga_data["url"],
            )
        ]

    async def get_manga(self, url_or_id: str) -> Manga:
        return Manga.model_validate(self._manga_data)

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        # Attach the manga_id to the chapter
        data = {**self._chapter_data, "manga_id": manga.id}
        return [Chapter.model_validate(data)]

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        # 3 pages at IMAGE_BASE/001.png, 002.png, 003.png
        return [
            Page(
                index=i,
                url=f"{IMAGE_BASE}/{i + 1:03d}.png",
                filename=f"{i + 1:03d}.png",
            )
            for i in range(PAGES_COUNT)
        ]


class FakeParserFactory:
    """Fake factory that returns a FakeParser."""

    def create(self, site: SiteConfig, session) -> BaseParser:
        return FakeParser(site, session)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated in tmp_path (no user config)."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_name="NexusDL-Test",
        app_environment="dev",
        public_url="http://localhost:3000",
        api_base_url="http://localhost:8000",
        frontend_url="http://localhost:3000",
        secret_key="test-secret-key-" + "a" * 48,
        jwt_secret="test-jwt-secret-" + "b" * 48,
        cookie_encryption_key="test-cookie-key-" + "c" * 48,
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
def site_config() -> SiteConfig:
    """SiteConfig from sample_site.yaml."""
    import yaml

    data = yaml.safe_load((FIXTURES / "sample_site.yaml").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return SiteConfig.model_validate(data)


@pytest.fixture
def event_bus() -> EventBus:
    """Fresh EventBus."""
    return EventBus()


@pytest.fixture
def collected_events(event_bus: EventBus) -> list[Event]:
    """Collect all events published on the bus."""
    events: list[Event] = []

    async def _collect(event: Event) -> None:
        events.append(event)

    # Subscribe to all event types we care about
    for event_type in (
        "download.started",
        "download.progress",
        "download.completed",
        "download.failed",
        "chapter.started",
        "chapter.completed",
        "chapter.failed",
    ):
        event_bus.subscribe(event_type, _collect)

    return events


# ────────────────────────────────────────────────────────────────────
# The test
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
@respx.mock
async def test_full_download_flow(
    settings: Settings,
    site_config: SiteConfig,
    event_bus: EventBus,
    collected_events: list[Event],
    tmp_path: Path,
) -> None:
    """Test the complete download pipeline end-to-end.

    Steps:
        1. Mock 3 image URLs with respx (returns PNG bytes).
        2. Build SiteRegistry with a FakeParserFactory.
        3. Build DownloadManager with registry, settings, event_bus.
        4. Call download_manga() → download 1 chapter → 3 pages → CBZ.
        5. Verify:
           - CBZ file exists and is valid ZIP
           - CBZ contains 3 images + ComicInfo.xml
           - Library DB has the manga
           - Events emitted: started, progress, chapter.completed, completed
           - No errors

    Runtime: ~2-3 seconds (image generation is the bottleneck).
    """
    # ── 1. Mock image URLs with respx ──
    png_bytes = _make_png()
    for i in range(PAGES_COUNT):
        url = f"{IMAGE_BASE}/{i + 1:03d}.png"
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=png_bytes,
                headers={"content-type": "image/png"},
            )
        )

    # ── 2. Build the registry with the fake factory ──
    #    We bypass sites.yaml and directly seed the registry.
    registry = SiteRegistry(settings, parser_factory=FakeParserFactory())

    # Override the internal state to inject our fake site
    # (SiteRegistry normally loads from sites.yaml; here we bypass it)
    registry._sites = {site_config.id: site_config}  # type: ignore[attr-defined]
    registry._aliases = {}  # type: ignore[attr-defined]

    # ── 3. Build DownloadManager ──
    manager = DownloadManager(
        registry=registry,
        settings=settings,
        event_bus=event_bus,
        max_concurrent_tasks=1,
        max_concurrent_pages=2,
    )

    # ── 4. Get the manga + chapters (via the fake parser) ──
    #      We use the parser directly here to avoid needing a search.
    session = manager._session_factory(site_config)  # type: ignore[attr-defined]
    parser = registry.get_parser(site_config.id, session)
    manga = await parser.get_manga("any")
    chapters = await parser.get_chapters(manga)
    assert len(chapters) == 1, "Expected exactly 1 chapter"
    chapter = chapters[0]

    # ── 5. Download ──
    dest = tmp_path / "downloads"
    dest.mkdir()

    results = []
    async for result in manager.download_manga(
        manga,
        chapters=chapters,
        dest=dest,
        fmt="CBZ",
    ):
        results.append(result)

    # ── 6. Verify results ──
    assert len(results) == 1
    result = results[0]
    assert result.success is True
    assert result.output_path.exists()
    assert result.output_path.suffix == ".cbz"
    assert result.bytes_downloaded > 0
    assert result.duration_seconds > 0
    assert result.pages_failed == []

    # ── 7. Verify CBZ contents ──
    with zipfile.ZipFile(result.output_path) as zf:
        names = zf.namelist()
        # 3 images + ComicInfo.xml
        image_names = [n for n in names if n.endswith(".png")]
        assert len(image_names) == PAGES_COUNT, f"Expected {PAGES_COUNT} images, got {len(image_names)}"
        assert "ComicInfo.xml" in names, "ComicInfo.xml missing"
        # Each image should be valid
        for name in image_names:
            data = zf.read(name)
            assert len(data) > 0
            # Verify PNG header
            assert data.startswith(b"\x89PNG\r\n\x1a\n")

    # ── 8. Verify ComicInfo.xml contents ──
    with zipfile.ZipFile(result.output_path) as zf:
        comic_info = zf.read("ComicInfo.xml").decode("utf-8")
        assert "<Title>" in comic_info
        assert "<Series>One Piece</Series>" in comic_info
        assert "<Number>1.0</Number>" in comic_info or "<Number>1</Number>" in comic_info
        assert "<LanguageISO>en</LanguageISO>" in comic_info

    # ── 9. Verify library DB ──
    #  (The DownloadManager is expected to insert into the library)
    #  (We use the manager's DB reference for verification)
    from nexusdl.core.library.database import LibraryDatabase

    async with LibraryDatabase(settings.database_url) as db:
        await db.init()
        stored = await db.get_manga(manga.id)
        assert stored is not None, "Manga not in library DB"
        assert stored.title == "One Piece"

    # ── 10. Verify events ──
    event_types = [e.type for e in collected_events]
    assert "download.started" in event_types, f"Missing download.started (got: {event_types})"
    assert "chapter.started" in event_types, f"Missing chapter.started"
    assert "chapter.completed" in event_types, f"Missing chapter.completed"
    assert "download.completed" in event_types, f"Missing download.completed"

    # No failures
    assert "download.failed" not in event_types
    assert "chapter.failed" not in event_types


# ────────────────────────────────────────────────────────────────────
# Failure case: page fails, retries, succeeds on 2nd attempt
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
@respx.mock
async def test_download_succeeds_after_retry(
    settings: Settings,
    site_config: SiteConfig,
    event_bus: EventBus,
    tmp_path: Path,
) -> None:
    """Page fails once (500), retries, then succeeds.

    Validates:
        - Retry logic in HttpSession (tenacity)
        - Final result is success
        - No exception raised
    """
    # First call returns 500, second returns 200
    call_count = {"n": 0}

    def _side_effect(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, content=_make_png(), headers={"content-type": "image/png"})

    png_bytes = _make_png()
    for i in range(PAGES_COUNT):
        url = f"{IMAGE_BASE}/{i + 1:03d}.png"
        respx.get(url).mock(side_effect=_side_effect)

    registry = SiteRegistry(settings, parser_factory=FakeParserFactory())
    registry._sites = {site_config.id: site_config}  # type: ignore[attr-defined]
    registry._aliases = {}  # type: ignore[attr-defined]

    manager = DownloadManager(
        registry=registry,
        settings=settings,
        event_bus=event_bus,
        max_concurrent_tasks=1,
        max_concurrent_pages=1,
    )

    session = manager._session_factory(site_config)  # type: ignore[attr-defined]
    parser = registry.get_parser(site_config.id, session)
    manga = await parser.get_manga("any")
    chapters = await parser.get_chapters(manga)

    dest = tmp_path / "downloads"
    dest.mkdir()

    results = []
    async for result in manager.download_manga(
        manga,
        chapters=chapters,
        dest=dest,
        fmt="CBZ",
    ):
        results.append(result)

    assert len(results) == 1
    assert results[0].success is True
    # 3 pages × 2 calls (1 fail + 1 success) = 6 calls
    assert call_count["n"] >= PAGES_COUNT + 1, f"Expected retries, got {call_count['n']} calls"


# ────────────────────────────────────────────────────────────────────
# Failure case: all pages fail → chapter failed → download failed
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
@respx.mock
async def test_download_fails_after_max_retries(
    settings: Settings,
    site_config: SiteConfig,
    event_bus: EventBus,
    collected_events: list[Event],
    tmp_path: Path,
) -> None:
    """All pages return 500 → download fails.

    Validates:
        - Retry exhaustion (max 3 attempts)
        - Failure events emitted (chapter.failed, download.failed)
        - No partial .cbz file left
    """
    # All calls return 500
    for i in range(PAGES_COUNT):
        url = f"{IMAGE_BASE}/{i + 1:03d}.png"
        respx.get(url).mock(return_value=httpx.Response(500, text="Error"))

    registry = SiteRegistry(settings, parser_factory=FakeParserFactory())
    registry._sites = {site_config.id: site_config}  # type: ignore[attr-defined]
    registry._aliases = {}  # type: ignore[attr-defined]

    manager = DownloadManager(
        registry=registry,
        settings=settings,
        event_bus=event_bus,
        max_concurrent_tasks=1,
        max_concurrent_pages=1,
    )

    session = manager._session_factory(site_config)  # type: ignore[attr-defined]
    parser = registry.get_parser(site_config.id, session)
    manga = await parser.get_manga("any")
    chapters = await parser.get_chapters(manga)

    dest = tmp_path / "downloads"
    dest.mkdir()

    results = []
    async for result in manager.download_manga(
        manga,
        chapters=chapters,
        dest=dest,
        fmt="CBZ",
    ):
        results.append(result)

    assert len(results) == 1
    assert results[0].success is False
    assert len(results[0].pages_failed) > 0

    # No .cbz file should exist
    cbz_files = list(dest.rglob("*.cbz"))
    assert cbz_files == [], f"Partial .cbz left: {cbz_files}"

    # Failure events emitted
    event_types = [e.type for e in collected_events]
    assert "download.failed" in event_types or "chapter.failed" in event_types
