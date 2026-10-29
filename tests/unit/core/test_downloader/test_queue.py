# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for DownloadQueue.

DownloadQueue is the priority queue that holds DownloadTasks until a
worker picks them up. It enforces:

    - Priority ordering (HIGH > NORMAL > LOW)
    - FIFO within the same priority
    - State machine (PENDING → RUNNING → DONE/FAILED/CANCELLED)
    - Concurrency limit (max N tasks RUNNING)
    - Idempotent operations (submit duplicate → error)
    - Thread-safety (asyncio.Lock)

This test verifies all of the above WITHOUT network or DB.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

if TYPE_CHECKING:
    from nexusdl.core.config import Settings

from nexusdl.core.config import Settings
from nexusdl.core.downloader.queue import DownloadQueue
from nexusdl.core.exceptions import InvalidStateTransitionError
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.download import DownloadTask
from nexusdl.core.models.enums import (
    ContentRating,
    DownloadStatus,
    Language,
    MangaStatus,
    PackagingFormat,
    Priority,
)
from nexusdl.core.models.manga import Manga


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
        max_concurrent_downloads=2,
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
    """A fake Chapter."""
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
def make_task(
    fake_manga: Manga,
    fake_chapter: Chapter,
    tmp_path: Path,
):
    """Factory to build DownloadTask with configurable priority/status."""

    def _make(
        priority: Priority = Priority.NORMAL,
        status: DownloadStatus = DownloadStatus.PENDING,
        task_id: UUID | None = None,
    ) -> DownloadTask:
        return DownloadTask(
            id=task_id or uuid4(),
            manga=fake_manga,
            chapters=[fake_chapter],
            dest=tmp_path / "downloads",
            fmt=PackagingFormat.CBZ,
            priority=priority,
            created_at=datetime.now(tz=timezone.utc),
            status=status,
        )

    return _make


@pytest.fixture
async def queue(settings: Settings) -> DownloadQueue:
    """Fresh DownloadQueue."""
    q = DownloadQueue(settings=settings, max_concurrent=2)
    yield q
    await q.shutdown()  # cleanup


# ────────────────────────────────────────────────────────────────────
# Test 1: submit()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_returns_task_id(queue: DownloadQueue, make_task) -> None:
    """submit() returns the task_id."""
    task = make_task()
    task_id = await queue.submit(task)

    assert task_id == task.id
    assert isinstance(task_id, UUID)


@pytest.mark.asyncio
async def test_submit_duplicate_raises(queue: DownloadQueue, make_task) -> None:
    """submit() raises on duplicate task_id."""
    task = make_task()
    await queue.submit(task)

    with pytest.raises(ValueError, match="already exists"):
        await queue.submit(task)


@pytest.mark.asyncio
async def test_submit_increments_size(queue: DownloadQueue, make_task) -> None:
    """submit() increases the queue size."""
    assert queue.size() == 0
    await queue.submit(make_task())
    assert queue.size() == 1
    await queue.submit(make_task())
    assert queue.size() == 2


# ────────────────────────────────────────────────────────────────────
# Test 2: get()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_task(queue: DownloadQueue, make_task) -> None:
    """get() returns the task by id."""
    task = make_task()
    await queue.submit(task)

    retrieved = queue.get(task.id)
    assert retrieved is not None
    assert retrieved.id == task.id


@pytest.mark.asyncio
async def test_get_unknown_returns_none(queue: DownloadQueue) -> None:
    """get() returns None for unknown id."""
    assert queue.get(uuid4()) is None


# ────────────────────────────────────────────────────────────────────
# Test 3: list()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_all(queue: DownloadQueue, make_task) -> None:
    """list() returns all tasks."""
    await queue.submit(make_task())
    await queue.submit(make_task())
    await queue.submit(make_task())

    assert len(queue.list()) == 3


@pytest.mark.asyncio
async def test_list_by_status(queue: DownloadQueue, make_task) -> None:
    """list(status=...) filters by status."""
    await queue.submit(make_task(status=DownloadStatus.PENDING))
    await queue.submit(make_task(status=DownloadStatus.PENDING))
    await queue.submit(make_task(status=DownloadStatus.DONE))

    pending = queue.list(status=DownloadStatus.PENDING)
    assert len(pending) == 2

    done = queue.list(status=DownloadStatus.DONE)
    assert len(done) == 1


# ────────────────────────────────────────────────────────────────────
# Test 4: priority ordering
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_priority_ordering(queue: DownloadQueue, make_task) -> None:
    """HIGH priority tasks are returned before NORMAL and LOW."""
    low = make_task(priority=Priority.LOW)
    normal = make_task(priority=Priority.NORMAL)
    high = make_task(priority=Priority.HIGH)

    await queue.submit(low)
    await queue.submit(normal)
    await queue.submit(high)

    # next() should return high first
    first = await queue.next()
    assert first.id == high.id

    second = await queue.next()
    assert second.id == normal.id

    third = await queue.next()
    assert third.id == low.id


@pytest.mark.asyncio
async def test_fifo_within_priority(queue: DownloadQueue, make_task) -> None:
    """FIFO within the same priority."""
    first = make_task(priority=Priority.NORMAL)
    second = make_task(priority=Priority.NORMAL)
    third = make_task(priority=Priority.NORMAL)

    await queue.submit(first)
    await asyncio.sleep(0.001)  # ensure different created_at
    await queue.submit(second)
    await asyncio.sleep(0.001)
    await queue.submit(third)

    assert (await queue.next()).id == first.id
    assert (await queue.next()).id == second.id
    assert (await queue.next()).id == third.id


# ────────────────────────────────────────────────────────────────────
# Test 5: pause / resume
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_running_task(queue: DownloadQueue, make_task) -> None:
    """pause() transitions RUNNING → PAUSED."""
    task = make_task(status=DownloadStatus.PENDING)
    await queue.submit(task)
    await queue.next()  # marks RUNNING

    await queue.pause(task.id)
    assert queue.get(task.id).status == DownloadStatus.PAUSED


@pytest.mark.asyncio
async def test_pause_invalid_transition(queue: DownloadQueue, make_task) -> None:
    """pause() on DONE task raises InvalidStateTransitionError."""
    task = make_task(status=DownloadStatus.DONE)
    await queue.submit(task)

    with pytest.raises(InvalidStateTransitionError):
        await queue.pause(task.id)


@pytest.mark.asyncio
async def test_resume_paused_task(queue: DownloadQueue, make_task) -> None:
    """resume() transitions PAUSED → RUNNING."""
    task = make_task(status=DownloadStatus.PENDING)
    await queue.submit(task)
    await queue.next()  # RUNNING
    await queue.pause(task.id)

    await queue.resume(task.id)
    assert queue.get(task.id).status == DownloadStatus.RUNNING


@pytest.mark.asyncio
async def test_resume_not_paused_raises(queue: DownloadQueue, make_task) -> None:
    """resume() on PENDING raises InvalidStateTransitionError."""
    task = make_task(status=DownloadStatus.PENDING)
    await queue.submit(task)

    with pytest.raises(InvalidStateTransitionError):
        await queue.resume(task.id)


# ────────────────────────────────────────────────────────────────────
# Test 6: cancel
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_pending(queue: DownloadQueue, make_task) -> None:
    """cancel() on PENDING → CANCELLED."""
    task = make_task()
    await queue.submit(task)

    await queue.cancel(task.id)
    assert queue.get(task.id).status == DownloadStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_running(queue: DownloadQueue, make_task) -> None:
    """cancel() on RUNNING → CANCELLED."""
    task = make_task()
    await queue.submit(task)
    await queue.next()  # RUNNING

    await queue.cancel(task.id)
    assert queue.get(task.id).status == DownloadStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_terminal_raises(queue: DownloadQueue, make_task) -> None:
    """cancel() on DONE/FAILED raises InvalidStateTransitionError."""
    done_task = make_task(status=DownloadStatus.DONE)
    await queue.submit(done_task)

    with pytest.raises(InvalidStateTransitionError):
        await queue.cancel(done_task.id)


# ────────────────────────────────────────────────────────────────────
# Test 7: next() and concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_next_empty_returns_none(queue: DownloadQueue) -> None:
    """next() on empty queue returns None."""
    assert await queue.next() is None


@pytest.mark.asyncio
async def test_next_respects_concurrency(queue: DownloadQueue, make_task) -> None:
    """next() returns None when max_concurrent tasks are RUNNING."""
    # max_concurrent = 2
    await queue.submit(make_task())
    await queue.submit(make_task())
    await queue.submit(make_task())

    # First two run
    first = await queue.next()
    second = await queue.next()
    assert first is not None
    assert second is not None

    # Third should not start (concurrency limit)
    third = await queue.next()
    assert third is None


@pytest.mark.asyncio
async def test_next_transitions_to_running(queue: DownloadQueue, make_task) -> None:
    """next() transitions PENDING → RUNNING."""
    task = make_task()
    await queue.submit(task)

    returned = await queue.next()
    assert returned.id == task.id
    assert queue.get(task.id).status == DownloadStatus.RUNNING


# ────────────────────────────────────────────────────────────────────
# Test 8: state machine
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_transitions(queue: DownloadQueue, make_task) -> None:
    """Invalid transitions raise InvalidStateTransitionError."""
    # DONE → PAUSED
    done = make_task(status=DownloadStatus.DONE)
    await queue.submit(done)
    with pytest.raises(InvalidStateTransitionError):
        await queue.pause(done.id)

    # CANCELLED → RUNNING
    cancelled = make_task(status=DownloadStatus.CANCELLED)
    await queue.submit(cancelled)
    with pytest.raises(InvalidStateTransitionError):
        await queue.resume(cancelled.id)


# ────────────────────────────────────────────────────────────────────
# Test 9: concurrency (async)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_submits(queue: DownloadQueue, make_task) -> None:
    """Concurrent submit() calls are thread-safe."""

    async def _submit() -> UUID:
        return await queue.submit(make_task())

    ids = await asyncio.gather(*[_submit() for _ in range(10)])

    # All should have unique IDs
    assert len(set(ids)) == 10
    assert queue.size() == 10


@pytest.mark.asyncio
async def test_concurrent_next(queue: DownloadQueue, make_task) -> None:
    """Concurrent next() calls respect the concurrency limit."""
    # Submit 5 tasks
    for _ in range(5):
        await queue.submit(make_task())

    # Concurrently call next() 5 times
    results = await asyncio.gather(*[queue.next() for _ in range(5)])

    # At most 2 should be non-None (max_concurrent=2)
    non_none = [r for r in results if r is not None]
    assert len(non_none) <= 2


# ────────────────────────────────────────────────────────────────────
# Test 10: cleanup
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_clears(queue: DownloadQueue, make_task) -> None:
    """shutdown() clears the queue."""
    await queue.submit(make_task())
    await queue.submit(make_task())

    await queue.shutdown()
    assert queue.size() == 0
    assert queue.list() == []
