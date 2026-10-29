# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for DownloadTask and DownloadResult models.

DownloadTask represents an in-progress (or queued) download, with a
**state machine**:

    PENDING ──► RUNNING ──► DONE
       │           │
       │           ├──► PAUSED ──► RUNNING
       │           │
       │           └──► FAILED
       │
       └──► CANCELLED

DONE, FAILED, CANCELLED are **terminal** (no transitions out).

DownloadResult is the outcome of a completed (or failed) chapter.

What is tested:
    - DownloadTask construction (all fields)
    - Default values (status=PENDING, progress=0.0, priority=NORMAL)
    - UUID4 id
    - progress in [0.0, 1.0]
    - State machine (all valid transitions)
    - Invalid transitions (raise InvalidStateTransitionError)
    - Helper methods (start, complete, fail, pause, resume, cancel)
    - DownloadResult construction (all fields)
    - success coherence with pages_failed
    - pages_failed default is independent
    - Serialization (model_dump, model_dump_json)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from nexusdl.core.exceptions import InvalidStateTransitionError
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


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


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
def valid_task_data(
    fake_manga: Manga,
    fake_chapter: Chapter,
    tmp_path: Path,
) -> dict:
    """Minimal valid DownloadTask data."""
    return {
        "id": str(uuid4()),
        "manga": fake_manga.model_dump(),
        "chapters": [fake_chapter.model_dump()],
        "dest": str(tmp_path / "downloads"),
        "fmt": "CBZ",
        "priority": "NORMAL",
        "created_at": "2026-10-29T12:00:00Z",
        "status": "PENDING",
        "progress": 0.0,
    }


@pytest.fixture
def task(fake_manga: Manga, fake_chapter: Chapter, tmp_path: Path) -> DownloadTask:
    """A valid DownloadTask."""
    return DownloadTask(
        id=uuid4(),
        manga=fake_manga,
        chapters=[fake_chapter],
        dest=tmp_path / "downloads",
        fmt=PackagingFormat.CBZ,
        priority=Priority.NORMAL,
        created_at=datetime.now(tz=timezone.utc),
        status=DownloadStatus.PENDING,
        progress=0.0,
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: DownloadTask construction
# ────────────────────────────────────────────────────────────────────


def test_task_construction(task: DownloadTask) -> None:
    """DownloadTask can be constructed with all fields."""
    assert isinstance(task.id, UUID)
    assert task.manga.title == "One Piece"
    assert len(task.chapters) == 1
    assert task.fmt == PackagingFormat.CBZ
    assert task.priority == Priority.NORMAL
    assert task.status == DownloadStatus.PENDING
    assert task.progress == 0.0


def test_task_id_is_uuid4(task: DownloadTask) -> None:
    """task.id is a UUID4."""
    assert isinstance(task.id, UUID)
    assert task.id.version == 4


# ────────────────────────────────────────────────────────────────────
# Test 2: Defaults
# ────────────────────────────────────────────────────────────────────


def test_task_defaults(
    fake_manga: Manga,
    fake_chapter: Chapter,
    tmp_path: Path,
) -> None:
    """Optional fields have correct defaults."""
    task = DownloadTask(
        id=uuid4(),
        manga=fake_manga,
        chapters=[fake_chapter],
        dest=tmp_path / "downloads",
        fmt=PackagingFormat.CBZ,
        created_at=datetime.now(tz=timezone.utc),
    )

    assert task.priority == Priority.NORMAL
    assert task.status == DownloadStatus.PENDING
    assert task.progress == 0.0
    assert task.error is None


# ────────────────────────────────────────────────────────────────────
# Test 3: progress validation
# ────────────────────────────────────────────────────────────────────


def test_progress_zero(task: DownloadTask) -> None:
    """progress = 0.0 is valid."""
    task.progress = 0.0
    assert task.progress == 0.0


def test_progress_one(task: DownloadTask) -> None:
    """progress = 1.0 is valid."""
    task.progress = 1.0
    assert task.progress == 1.0


def test_progress_negative_raises(task: DownloadTask) -> None:
    """progress < 0 raises ValidationError."""
    with pytest.raises(ValidationError):
        task.progress = -0.1


def test_progress_above_one_raises(task: DownloadTask) -> None:
    """progress > 1.0 raises ValidationError."""
    with pytest.raises(ValidationError):
        task.progress = 1.1


# ────────────────────────────────────────────────────────────────────
# Test 4: State machine — valid transitions
# ────────────────────────────────────────────────────────────────────


def test_pending_to_running(task: DownloadTask) -> None:
    """PENDING → RUNNING is valid."""
    task.transition_to(DownloadStatus.RUNNING)
    assert task.status == DownloadStatus.RUNNING


def test_running_to_done(task: DownloadTask) -> None:
    """RUNNING → DONE is valid."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.DONE)
    assert task.status == DownloadStatus.DONE


def test_running_to_failed(task: DownloadTask) -> None:
    """RUNNING → FAILED is valid."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.FAILED)
    assert task.status == DownloadStatus.FAILED


def test_running_to_paused(task: DownloadTask) -> None:
    """RUNNING → PAUSED is valid."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.PAUSED)
    assert task.status == DownloadStatus.PAUSED


def test_paused_to_running(task: DownloadTask) -> None:
    """PAUSED → RUNNING is valid."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.PAUSED)
    task.transition_to(DownloadStatus.RUNNING)
    assert task.status == DownloadStatus.RUNNING


def test_pending_to_cancelled(task: DownloadTask) -> None:
    """PENDING → CANCELLED is valid."""
    task.transition_to(DownloadStatus.CANCELLED)
    assert task.status == DownloadStatus.CANCELLED


def test_running_to_cancelled(task: DownloadTask) -> None:
    """RUNNING → CANCELLED is valid."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.CANCELLED)
    assert task.status == DownloadStatus.CANCELLED


def test_paused_to_cancelled(task: DownloadTask) -> None:
    """PAUSED → CANCELLED is valid."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.PAUSED)
    task.transition_to(DownloadStatus.CANCELLED)
    assert task.status == DownloadStatus.CANCELLED


# ────────────────────────────────────────────────────────────────────
# Test 5: State machine — invalid transitions
# ────────────────────────────────────────────────────────────────────


def test_done_to_running_raises(task: DownloadTask) -> None:
    """DONE → RUNNING is invalid (terminal state)."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.DONE)

    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(DownloadStatus.RUNNING)


def test_done_to_paused_raises(task: DownloadTask) -> None:
    """DONE → PAUSED is invalid."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.DONE)

    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(DownloadStatus.PAUSED)


def test_failed_to_running_raises(task: DownloadTask) -> None:
    """FAILED → RUNNING is invalid (terminal state)."""
    task.transition_to(DownloadStatus.RUNNING)
    task.transition_to(DownloadStatus.FAILED)

    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(DownloadStatus.RUNNING)


def test_cancelled_to_running_raises(task: DownloadTask) -> None:
    """CANCELLED → RUNNING is invalid."""
    task.transition_to(DownloadStatus.CANCELLED)

    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(DownloadStatus.RUNNING)


def test_pending_to_done_raises(task: DownloadTask) -> None:
    """PENDING → DONE is invalid (must go through RUNNING)."""
    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(DownloadStatus.DONE)


def test_pending_to_paused_raises(task: DownloadTask) -> None:
    """PENDING → PAUSED is invalid (must go through RUNNING)."""
    with pytest.raises(InvalidStateTransitionError):
        task.transition_to(DownloadStatus.PAUSED)


# ────────────────────────────────────────────────────────────────────
# Test 6: Helper methods
# ────────────────────────────────────────────────────────────────────


def test_start(task: DownloadTask) -> None:
    """start() transitions PENDING → RUNNING."""
    task.start()
    assert task.status == DownloadStatus.RUNNING


def test_complete(task: DownloadTask) -> None:
    """complete() transitions RUNNING → DONE and sets progress=1.0."""
    task.start()
    task.complete()
    assert task.status == DownloadStatus.DONE
    assert task.progress == 1.0


def test_fail(task: DownloadTask) -> None:
    """fail() transitions RUNNING → FAILED and sets error."""
    task.start()
    task.fail("connection refused")
    assert task.status == DownloadStatus.FAILED
    assert task.error == "connection refused"


def test_pause(task: DownloadTask) -> None:
    """pause() transitions RUNNING → PAUSED."""
    task.start()
    task.pause()
    assert task.status == DownloadStatus.PAUSED


def test_resume(task: DownloadTask) -> None:
    """resume() transitions PAUSED → RUNNING."""
    task.start()
    task.pause()
    task.resume()
    assert task.status == DownloadStatus.RUNNING


def test_cancel_from_pending(task: DownloadTask) -> None:
    """cancel() on PENDING → CANCELLED."""
    task.cancel()
    assert task.status == DownloadStatus.CANCELLED


def test_cancel_from_running(task: DownloadTask) -> None:
    """cancel() on RUNNING → CANCELLED."""
    task.start()
    task.cancel()
    assert task.status == DownloadStatus.CANCELLED


def test_cancel_from_done_raises(task: DownloadTask) -> None:
    """cancel() on DONE raises InvalidStateTransitionError."""
    task.start()
    task.complete()

    with pytest.raises(InvalidStateTransitionError):
        task.cancel()


# ────────────────────────────────────────────────────────────────────
# Test 7: DownloadResult
# ────────────────────────────────────────────────────────────────────


def test_result_construction(task: DownloadTask, fake_chapter: Chapter, tmp_path: Path) -> None:
    """DownloadResult can be constructed."""
    result = DownloadResult(
        task_id=task.id,
        chapter=fake_chapter,
        output_path=tmp_path / "chapter.cbz",
        bytes_downloaded=123456,
        duration_seconds=4.2,
        pages_failed=[],
        success=True,
    )

    assert result.task_id == task.id
    assert result.chapter.id == fake_chapter.id
    assert result.bytes_downloaded == 123456
    assert result.duration_seconds == 4.2
    assert result.pages_failed == []
    assert result.success is True


def test_result_defaults(task: DownloadTask, fake_chapter: Chapter, tmp_path: Path) -> None:
    """DownloadResult has correct defaults."""
    result = DownloadResult(
        task_id=task.id,
        chapter=fake_chapter,
        output_path=tmp_path / "chapter.cbz",
        bytes_downloaded=0,
        duration_seconds=0.0,
        success=False,
    )

    assert result.pages_failed == []
    assert result.success is False


def test_result_pages_failed_default_independent(
    task: DownloadTask,
    fake_chapter: Chapter,
    tmp_path: Path,
) -> None:
    """Each DownloadResult gets its own pages_failed list."""
    r1 = DownloadResult(
        task_id=task.id,
        chapter=fake_chapter,
        output_path=tmp_path / "a.cbz",
        bytes_downloaded=0,
        duration_seconds=0.0,
        success=False,
    )
    r2 = DownloadResult(
        task_id=task.id,
        chapter=fake_chapter,
        output_path=tmp_path / "b.cbz",
        bytes_downloaded=0,
        duration_seconds=0.0,
        success=False,
    )

    assert r1.pages_failed is not r2.pages_failed
    r1.pages_failed.append(0)
    assert r2.pages_failed == []


# ────────────────────────────────────────────────────────────────────
# Test 8: Serialization
# ────────────────────────────────────────────────────────────────────


def test_task_serialization(task: DownloadTask) -> None:
    """DownloadTask can be serialized to dict and JSON."""
    data = task.model_dump()
    assert isinstance(data, dict)
    assert data["fmt"] == "CBZ"
    assert data["priority"] == "NORMAL"
    assert data["status"] == "PENDING"

    json_str = task.model_dump_json()
    parsed = json.loads(json_str)
    assert parsed["fmt"] == "CBZ"


def test_task_roundtrip(task: DownloadTask) -> None:
    """DownloadTask → dict → DownloadTask roundtrip."""
    data = task.model_dump()
    task2 = DownloadTask.model_validate(data)
    assert task2.id == task.id
    assert task2.status == task.status


def test_result_serialization(task: DownloadTask, fake_chapter: Chapter, tmp_path: Path) -> None:
    """DownloadResult can be serialized to dict."""
    result = DownloadResult(
        task_id=task.id,
        chapter=fake_chapter,
        output_path=tmp_path / "chapter.cbz",
        bytes_downloaded=1024,
        duration_seconds=1.0,
        success=True,
    )

    data = result.model_dump()
    assert isinstance(data, dict)
    assert data["bytes_downloaded"] == 1024
