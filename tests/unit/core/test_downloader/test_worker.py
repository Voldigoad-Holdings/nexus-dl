# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for DownloadWorker.

DownloadWorker is the component that, given a DownloadTask, downloads
all pages of all chapters, then packages them via the PackagerFactory.

It is used by DownloadManager (which orchestrates queues, concurrency,
and events). This test verifies the worker IN ISOLATION with mocked:

    - parser (FakeParser, no network)
    - session (mocked via respx, no network)
    - packager (Mock, no real files)
    - event_bus (real, but with collected events)

What is tested:
    - Happy path (1 chapter, 3 pages → CBZ)
    - Empty chapter (0 pages → error)
    - Page download failure (retry, then error)
    - Package failure (error propagated)
    - Concurrency (semaphore limits parallel page downloads)
    - Partial failure (some pages fail, others succeed)
    - Progress callback
    - Events (chapter.started, chapter.completed, chapter.failed)
    - Cancellation (Ctrl+C / task.cancel())

Why so many cases?
    DownloadWorker is the "hot path" of NexusDL. A regression here means
    downloads break silently or leak temp files.
"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import respx
from PIL import Image

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.events import Event

from nexusdl.core.config import Settings
from nexusdl.core.downloader.worker import DownloadWorker
from nexusdl.core.events import Event, EventBus
from nexusdl.core.exceptions import (
    ChapterDownloadError,
    DownloadError,
    EmptyChapterError,
    PackagingError,
)
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.download import DownloadResult, DownloadTask
from nexusdl.core.models.enums import (
    ContentRating,
    DownloadStatus,
    Language,
    MangaStatus,
    PackagingFormat,
    Priority,
)
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.exceptions import ParserError


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

IMAGE_BASE = "https://uploads.test.com/data/test"


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_png(width: int = 1, height: int = 1) -> bytes:
    """Generate a minimal valid PNG (default 1×1)."""
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_cbz_bytes() -> bytes:
    """Generate a minimal valid CBZ (ZIP) in memory."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("001.png", _make_png())
        zf.writestr("ComicInfo.xml", "<ComicInfo><Title>Test</Title></ComicInfo>")
    return buf.getvalue()


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings."""
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
        max_concurrent_downloads=1,
        max_concurrent_pages_per_chapter=2,
    )


@pytest.fixture
def fake_manga() -> Manga:
    """A fake Manga."""
    return Manga(
        id="a1b2c3d4e5f6a7b8",
        source_id="a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6",
        site="mangadex",
        title="One Piece",
        author="Oda Eiichiro",
        status=MangaStatus.ONGOING,
        language=Language.EN,
        content_rating=ContentRating.SAFE,
        url="https://mangadex.org/title/a1b2c3d4e5f6a7b8",
        updated_at="2026-10-29T12:00:00Z",
    )


@pytest.fixture
def fake_chapter(fake_manga: Manga) -> Chapter:
    """A fake Chapter with 3 pages."""
    return Chapter(
        id="b3e5d7a9c1f2e4d6",
        manga_id=fake_manga.id,
        source_id="ch-uuid",
        title="Romance Dawn",
        number=1.0,
        volume=1,
        language=Language.EN,
        pages_count=3,
        published_at="1997-07-22T00:00:00Z",
        url="https://mangadex.org/chapter/ch-uuid",
    )


@pytest.fixture
def fake_pages() -> list[Page]:
    """3 fake pages (URLs will be mocked via respx)."""
    return [
        Page(
            index=i,
            url=f"{IMAGE_BASE}/{i + 1:03d}.png",
            filename=f"{i + 1:03d}.png",
        )
        for i in range(3)
    ]


@pytest.fixture
def fake_parser(fake_pages: list[Page]) -> MagicMock:
    """Fake parser that returns fake_pages."""
    parser = MagicMock(spec=BaseParser)
    parser.get_pages = AsyncMock(return_value=fake_pages)
    parser.download_page = AsyncMock(
        side_effect=lambda page, dest: _write_fake_page(page, dest)
    )
    return parser


async def _write_fake_page(page: Page, dest: Path) -> Path:
    """Simulate a page download (no network)."""
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / page.filename
    out.write_bytes(_make_png())
    return out


@pytest.fixture
def fake_packager() -> MagicMock:
    """Fake packager that returns a .cbz path."""
    packager = MagicMock()

    async def _package(pages, output, metadata=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_make_cbz_bytes())
        return output

    packager.package = AsyncMock(side_effect=_package)
    return packager


@pytest.fixture
def fake_packager_factory(fake_packager: MagicMock) -> MagicMock:
    """Fake PackagerFactory that returns fake_packager."""
    factory = MagicMock()
    factory.create = MagicMock(return_value=fake_packager)
    return factory


@pytest.fixture
def event_bus() -> EventBus:
    """Fresh EventBus."""
    return EventBus()


@pytest.fixture
def collected_events(event_bus: EventBus) -> list[Event]:
    """Collect events published on the bus."""
    events: list[Event] = []

    async def _collect(event: Event) -> None:
        events.append(event)

    for event_type in (
        "chapter.started",
        "chapter.completed",
        "chapter.failed",
        "download.progress",
    ):
        event_bus.subscribe(event_type, _collect)

    return events


@pytest.fixture
def fake_task(fake_manga: Manga, fake_chapter: Chapter, tmp_path: Path) -> DownloadTask:
    """A fake DownloadTask with 1 chapter."""
    return DownloadTask(
        id=uuid4(),
        manga=fake_manga,
        chapters=[fake_chapter],
        dest=tmp_path / "downloads",
        fmt=PackagingFormat.CBZ,
        priority=Priority.NORMAL,
        created_at=datetime.now(tz=timezone.utc),
        status=DownloadStatus.RUNNING,
    )


@pytest.fixture
def worker(
    settings: Settings,
    fake_parser: MagicMock,
    fake_packager_factory: MagicMock,
    event_bus: EventBus,
) -> DownloadWorker:
    """A DownloadWorker with mocked dependencies."""
    return DownloadWorker(
        parser=fake_parser,
        packager_factory=fake_packager_factory,
        settings=settings,
        event_bus=event_bus,
        max_concurrent_pages=2,
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: Happy path
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_happy_path(
    worker: DownloadWorker,
    fake_task: DownloadTask,
    collected_events: list[Event],
) -> None:
    """run() downloads 3 pages, packages as CBZ, emits events."""
    result = await worker.run(fake_task)

    # Result
    assert isinstance(result, DownloadResult)
    assert result.task_id == fake_task.id
    assert result.success is True
    assert result.pages_failed == []
    assert result.output_path.exists()
    assert result.output_path.suffix == ".cbz"
    assert result.bytes_downloaded > 0
    assert result.duration_seconds > 0

    # Events
    event_types = [e.type for e in collected_events]
    assert "chapter.started" in event_types
    assert "chapter.completed" in event_types
    assert "chapter.failed" not in event_types


# ────────────────────────────────────────────────────────────────────
# Test 2: Empty chapter
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_empty_chapter(
    worker: DownloadWorker,
    fake_parser: MagicMock,
    fake_task: DownloadTask,
    collected_events: list[Event],
) -> None:
    """run() raises EmptyChapterError if get_pages returns []."""
    fake_parser.get_pages = AsyncMock(return_value=[])

    with pytest.raises(EmptyChapterError):
        await worker.run(fake_task)

    # Should emit chapter.failed (or not emit anything, depending on impl)
    # At minimum, no chapter.completed
    event_types = [e.type for e in collected_events]
    assert "chapter.completed" not in event_types


# ────────────────────────────────────────────────────────────────────
# Test 3: Page download failure
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_page_download_failure(
    worker: DownloadWorker,
    fake_parser: MagicMock,
    fake_task: DownloadTask,
    collected_events: list[Event],
) -> None:
    """run() raises ChapterDownloadError if all pages fail."""

    async def _failing_download(page: Page, dest: Path) -> Path:
        raise httpx.ConnectError("Connection refused")

    fake_parser.download_page = AsyncMock(side_effect=_failing_download)

    with pytest.raises((ChapterDownloadError, DownloadError)):
        await worker.run(fake_task)

    # Should emit chapter.failed
    event_types = [e.type for e in collected_events]
    assert "chapter.failed" in event_types


# ────────────────────────────────────────────────────────────────────
# Test 4: Partial failure
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_partial_failure(
    worker: DownloadWorker,
    fake_parser: MagicMock,
    fake_task: DownloadTask,
    fake_packager_factory: MagicMock,
) -> None:
    """run() succeeds with pages_failed if some pages fail."""
    call_count = {"n": 0}

    async def _partial_download(page: Page, dest: Path) -> Path:
        call_count["n"] += 1
        if call_count["n"] == 2:  # second page fails
            raise httpx.ConnectError("Connection refused")
        return await _write_fake_page(page, dest)

    fake_parser.download_page = AsyncMock(side_effect=_partial_download)

    result = await worker.run(fake_task)

    assert result.success is True
    assert len(result.pages_failed) == 1
    assert result.output_path.exists()


# ────────────────────────────────────────────────────────────────────
# Test 5: Package failure
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_package_failure(
    worker: DownloadWorker,
    fake_packager_factory: MagicMock,
    fake_task: DownloadTask,
    collected_events: list[Event],
) -> None:
    """run() raises PackagingError if packaging fails."""
    fake_packager_factory.create.return_value.package = AsyncMock(
        side_effect=PackagingError("disk full")
    )

    with pytest.raises(PackagingError):
        await worker.run(fake_task)

    event_types = [e.type for e in collected_events]
    assert "chapter.failed" in event_types


# ────────────────────────────────────────────────────────────────────
# Test 6: _download_pages — concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_pages_concurrency(
    worker: DownloadWorker,
    fake_parser: MagicMock,
    fake_task: DownloadTask,
    tmp_path: Path,
) -> None:
    """_download_pages() respects max_concurrent_pages."""
    active = {"current": 0, "max": 0}
    lock = asyncio.Lock()

    async def _track_concurrency(page: Page, dest: Path) -> Path:
        async with lock:
            active["current"] += 1
            active["max"] = max(active["max"], active["current"])
        await asyncio.sleep(0.05)  # simulate network
        async with lock:
            active["current"] -= 1
        return await _write_fake_page(page, dest)

    fake_parser.download_page = AsyncMock(side_effect=_track_concurrency)

    # Worker has max_concurrent_pages=2
    pages = fake_parser.get_pages.return_value
    await worker._download_pages(pages, tmp_path / "pages")

    # Max concurrency should be ≤ 2
    assert active["max"] <= 2, f"Concurrency exceeded: {active['max']}"


# ────────────────────────────────────────────────────────────────────
# Test 7: _download_pages — all fail
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_pages_all_fail(
    worker: DownloadWorker,
    fake_parser: MagicMock,
    fake_task: DownloadTask,
    tmp_path: Path,
) -> None:
    """_download_pages() raises if all pages fail."""

    async def _failing_download(page: Page, dest: Path) -> Path:
        raise httpx.ConnectError("Connection refused")

    fake_parser.download_page = AsyncMock(side_effect=_failing_download)

    with pytest.raises(ChapterDownloadError):
        await worker._download_pages(
            fake_parser.get_pages.return_value,
            tmp_path / "pages",
        )


# ────────────────────────────────────────────────────────────────────
# Test 8: _package
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_delegates_to_factory(
    worker: DownloadWorker,
    fake_packager_factory: MagicMock,
    fake_chapter: Chapter,
    tmp_path: Path,
) -> None:
    """_package() delegates to PackagerFactory.create()."""
    pages = [tmp_path / f"{i:03d}.png" for i in range(3)]
    for p in pages:
        p.write_bytes(_make_png())

    output = tmp_path / "output.cbz"

    result = await worker._package(
        pages=pages,
        chapter=fake_chapter,
        output_path=output,
        fmt=PackagingFormat.CBZ,
    )

    assert result == output
    fake_packager_factory.create.assert_called_once_with(PackagingFormat.CBZ)


# ────────────────────────────────────────────────────────────────────
# Test 9: Progress callback
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_progress_callback(
    worker: DownloadWorker,
    fake_task: DownloadTask,
) -> None:
    """Progress callback is called during download."""
    progress_calls: list[tuple[int, int]] = []

    def _on_progress(downloaded: int, total: int) -> None:
        progress_calls.append((downloaded, total))

    # Attach callback (implementation may vary)
    worker._on_progress = _on_progress

    await worker.run(fake_task)

    # Should have at least one call
    assert len(progress_calls) > 0


# ────────────────────────────────────────────────────────────────────
# Test 10: Cancellation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancellation(
    worker: DownloadWorker,
    fake_parser: MagicMock,
    fake_task: DownloadTask,
) -> None:
    """Cancelling the task raises CancelledError and cleans up."""
    started = asyncio.Event()

    async def _slow_download(page: Page, dest: Path) -> Path:
        started.set()
        await asyncio.sleep(10)
        return await _write_fake_page(page, dest)

    fake_parser.download_page = AsyncMock(side_effect=_slow_download)

    task = asyncio.create_task(worker.run(fake_task))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # No .cbz file should remain
    cbz_files = list(fake_task.dest.rglob("*.cbz")) if fake_task.dest.exists() else []
    assert cbz_files == []


# ────────────────────────────────────────────────────────────────────
# Test 11: Timeout per page
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_page_timeout(
    worker: DownloadWorker,
    fake_parser: MagicMock,
    fake_task: DownloadTask,
) -> None:
    """A page that exceeds the timeout is marked failed."""
    call_count = {"n": 0}

    async def _slow_first(page: Page, dest: Path) -> Path:
        call_count["n"] += 1
        if call_count["n"] == 1:
            await asyncio.sleep(100)  # would hang
        return await _write_fake_page(page, dest)

    fake_parser.download_page = AsyncMock(side_effect=_slow_first)

    # The worker should timeout the first page and continue
    # (implementation may vary: timeout or skip)
    try:
        result = await worker.run(fake_task)
        # If no exception, at least one page should be failed
        assert len(result.pages_failed) >= 1 or not result.success
    except (ChapterDownloadError, DownloadError):
        pass  # acceptable
