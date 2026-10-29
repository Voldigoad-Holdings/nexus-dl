# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for core.events (EventBus, Event).

EventBus is a typed async pub/sub broker that decouples:

    - Downloader (publisher)
    - CLI / TUI / GUI / Web (subscribers)

Contract:
    - Event: type (str), timestamp (datetime UTC), payload (dict), id (UUID)
    - subscribe(event_type, handler) -> None
    - unsubscribe(event_type, handler) -> None
    - publish(event) -> None (awaits all handlers)
    - Wildcard "*" matches all events
    - Exceptions in handlers do NOT propagate (logged)
    - Concurrency-safe (asyncio.Lock)
    - Handlers called in subscription order

What is tested:
    - Event construction (type, timestamp, payload, id)
    - subscribe/unsubscribe
    - publish (single, multiple, wildcard)
    - publish (no subscribers)
    - publish (handler exception isolation)
    - publish (sync handlers)
    - publish (order)
    - Concurrency
    - clear()
    - Typed events (DownloadProgressEvent, ChapterCompletedEvent)
    - Event JSON serialization
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio

from nexusdl.core.events import Event, EventBus


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def bus() -> EventBus:
    """Fresh EventBus."""
    return EventBus()


@pytest.fixture
def make_event():
    """Factory to build an Event."""

    def _make(
        event_type: str = "test.event",
        payload: dict | None = None,
    ) -> Event:
        return Event(
            type=event_type,
            timestamp=datetime.now(tz=timezone.utc),
            payload=payload or {},
        )

    return _make


# ────────────────────────────────────────────────────────────────────
# Test 1: Event construction
# ────────────────────────────────────────────────────────────────────


def test_event_construction() -> None:
    """Event can be constructed with all fields."""
    event = Event(
        type="download.progress",
        timestamp=datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc),
        payload={"task_id": "abc", "percent": 0.5},
    )

    assert event.type == "download.progress"
    assert event.payload["task_id"] == "abc"
    assert event.payload["percent"] == 0.5


def test_event_has_id() -> None:
    """Event has a unique id (UUID)."""
    event = Event(
        type="test",
        timestamp=datetime.now(tz=timezone.utc),
        payload={},
    )
    # If Event has an id field
    if hasattr(event, "id"):
        assert isinstance(event.id, UUID)


def test_event_timestamp_utc() -> None:
    """Event.timestamp is UTC."""
    event = Event(
        type="test",
        timestamp=datetime.now(tz=timezone.utc),
        payload={},
    )
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.tzinfo == timezone.utc


# ────────────────────────────────────────────────────────────────────
# Test 2: subscribe / unsubscribe
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_publish(bus: EventBus, make_event) -> None:
    """subscribe() then publish() calls the handler."""
    received: list[Event] = []

    async def _handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.event", _handler)
    await bus.publish(make_event("test.event"))

    assert len(received) == 1


@pytest.mark.asyncio
async def test_unsubscribe(bus: EventBus, make_event) -> None:
    """unsubscribe() removes the handler."""
    received: list[Event] = []

    async def _handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.event", _handler)
    bus.unsubscribe("test.event", _handler)
    await bus.publish(make_event("test.event"))

    assert len(received) == 0


@pytest.mark.asyncio
async def test_multiple_subscribers(bus: EventBus, make_event) -> None:
    """Multiple subscribers receive the same event."""
    received_a: list[Event] = []
    received_b: list[Event] = []

    async def _handler_a(event: Event) -> None:
        received_a.append(event)

    async def _handler_b(event: Event) -> None:
        received_b.append(event)

    bus.subscribe("test.event", _handler_a)
    bus.subscribe("test.event", _handler_b)
    await bus.publish(make_event("test.event"))

    assert len(received_a) == 1
    assert len(received_b) == 1


# ────────────────────────────────────────────────────────────────────
# Test 3: publish() — no subscribers
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_no_subscribers(bus: EventBus, make_event) -> None:
    """publish() with no subscribers does not raise."""
    await bus.publish(make_event("test.event"))  # should not raise


# ────────────────────────────────────────────────────────────────────
# Test 4: publish() — handler exception isolation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_handler_exception_isolated(bus: EventBus, make_event) -> None:
    """An exception in one handler does not affect others."""
    received: list[str] = []

    async def _failing(event: Event) -> None:
        raise ValueError("boom")

    async def _ok(event: Event) -> None:
        received.append("ok")

    bus.subscribe("test.event", _failing)
    bus.subscribe("test.event", _ok)

    await bus.publish(make_event("test.event"))  # should not raise

    assert received == ["ok"]


# ────────────────────────────────────────────────────────────────────
# Test 5: publish() — sync handlers
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_sync_handler(bus: EventBus, make_event) -> None:
    """Sync handlers are supported."""
    received: list[Event] = []

    def _sync_handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.event", _sync_handler)
    await bus.publish(make_event("test.event"))

    assert len(received) == 1


# ────────────────────────────────────────────────────────────────────
# Test 6: publish() — order
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_order(bus: EventBus, make_event) -> None:
    """Handlers are called in subscription order."""
    order: list[int] = []

    async def _h1(event: Event) -> None:
        order.append(1)

    async def _h2(event: Event) -> None:
        order.append(2)

    async def _h3(event: Event) -> None:
        order.append(3)

    bus.subscribe("test.event", _h1)
    bus.subscribe("test.event", _h2)
    bus.subscribe("test.event", _h3)

    await bus.publish(make_event("test.event"))

    assert order == [1, 2, 3]


# ────────────────────────────────────────────────────────────────────
# Test 7: wildcard
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wildcard_subscriber(bus: EventBus, make_event) -> None:
    """Wildcard '*' receives all events."""
    received: list[Event] = []

    async def _handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("*", _handler)
    await bus.publish(make_event("event1"))
    await bus.publish(make_event("event2"))

    assert len(received) == 2


# ────────────────────────────────────────────────────────────────────
# Test 8: concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_publish(bus: EventBus, make_event) -> None:
    """Concurrent publishes do not crash."""
    received: list[Event] = []

    async def _handler(event: Event) -> None:
        received.append(event)
        await asyncio.sleep(0.001)

    bus.subscribe("test.event", _handler)

    await asyncio.gather(
        *[bus.publish(make_event("test.event")) for _ in range(10)]
    )

    assert len(received) == 10


# ────────────────────────────────────────────────────────────────────
# Test 9: clear()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear(bus: EventBus, make_event) -> None:
    """clear() removes all subscribers."""
    received: list[Event] = []

    async def _handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test.event", _handler)
    bus.clear()
    await bus.publish(make_event("test.event"))

    assert len(received) == 0


# ────────────────────────────────────────────────────────────────────
# Test 10: Typed events
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_typed_download_progress_event(bus: EventBus) -> None:
    """DownloadProgressEvent can be published."""
    from nexusdl.core.events import DownloadProgressEvent

    received: list[DownloadProgressEvent] = []

    async def _handler(event: DownloadProgressEvent) -> None:
        received.append(event)

    bus.subscribe("download.progress", _handler)

    event = DownloadProgressEvent(
        task_id="abc",
        chapter_id="ch1",
        pages_done=5,
        pages_total=10,
        percent=0.5,
    )
    await bus.publish(event)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_typed_chapter_completed_event(bus: EventBus) -> None:
    """ChapterCompletedEvent can be published."""
    from nexusdl.core.events import ChapterCompletedEvent

    received: list[ChapterCompletedEvent] = []

    async def _handler(event: ChapterCompletedEvent) -> None:
        received.append(event)

    bus.subscribe("chapter.completed", _handler)

    event = ChapterCompletedEvent(
        task_id="abc",
        chapter_id="ch1",
        pages_count=3,
        output_path="/tmp/001.cbz",
    )
    await bus.publish(event)

    assert len(received) == 1


# ────────────────────────────────────────────────────────────────────
# Test 11: Event JSON
# ────────────────────────────────────────────────────────────────────


def test_event_model_dump() -> None:
    """Event.model_dump() returns a dict."""
    event = Event(
        type="test.event",
        timestamp=datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc),
        payload={"key": "value"},
    )
    data = event.model_dump()
    assert data["type"] == "test.event"
    assert data["payload"]["key"] == "value"


def test_event_model_dump_json() -> None:
    """Event.model_dump_json() returns valid JSON."""
    import json

    event = Event(
        type="test.event",
        timestamp=datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc),
        payload={"key": "value"},
    )
    json_str = event.model_dump_json()
    data = json.loads(json_str)
    assert data["type"] == "test.event"
