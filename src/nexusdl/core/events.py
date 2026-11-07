"""In-process event bus for cross-module notifications.

The event bus decouples the modules that **produce** pipeline events (the
worker producing ``download.progress``, the manager producing
``chapter.started``, the library database producing ``library.updated``)
from the modules that **consume** them (the progress tracker, the web
API's WebSocket dispatcher, a future CLI status panel, plugins emitting
custom events). Neither side imports the other; both import this module.

The bus is deliberately small:

* :class:`Event` — a frozen dataclass with four fields (``type``,
  ``timestamp``, ``payload``, ``id``) matching the PROMPT MASTER's
  contract. The payload is a shallow-immutable mapping so a subscriber
  cannot mutate an event that is about to be delivered to another
  subscriber.
* :class:`EventBus` — an in-memory pub/sub with exact-match and
  wildcard subscriptions, per-handler exception isolation, and
  snapshot-based concurrency.

Semantics at a glance
---------------------
**Subscription**

* ``bus.subscribe(event_type, handler)`` adds *handler* to the list for
  *event_type*. The wildcard ``"*"`` receives every event.
* ``bus.unsubscribe(event_type, handler)`` removes **every** occurrence
  of *handler* from that list. Returns ``True`` if at least one was
  removed. Occurrences are matched by **identity** (``is``), not by
  equality — two distinct ``Mock`` objects that compare equal are
  treated as distinct handlers. This is the safe default: a caller who
  unsubscribes one specific handler must not accidentally remove
  another that happens to compare equal.
* A handler subscribed to both a specific event type and ``"*"`` is
  called **twice** per matching event: once from the exact list, once
  from the wildcard list. Subscribing to both is the caller's choice;
  it is not a bug.

**Delivery**

* Handlers are called in **subscription order**, per list. Exact
  handlers run before wildcard handlers — a specific subscriber runs
  before a catch-all. Handlers within each list run in the order they
  were subscribed.
* Handlers are **async** (``async def handler(event: Event) -> None``).
  Synchronous handlers are not supported — a caller with a sync
  function wraps it in an ``async def``. This restriction keeps the
  type clean (``Handler = Callable[[Event], Awaitable[None]]``) and
  avoids mixed sync/async behaviour in the delivery loop.
* Every handler runs inside its own try/except. An exception raised by
  a handler is **logged at WARNING** and **swallowed**; the next
  handler still runs. A single broken subscriber cannot stop delivery
  to the others — the same isolation policy used by
  :class:`~nexusdl.plugins.loader.PluginLoader`.
* ``asyncio.CancelledError`` is **never** caught — it propagates
  through :meth:`EventBus.publish` and cancels the caller as expected.
  Every other ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``,
  ``GeneratorExit``) also propagates; the isolation only covers
  ``Exception`` subclasses, which is the right boundary — a
  ``KeyboardInterrupt`` from a handler must stop the pipeline, not be
  swallowed.

**Concurrency**

* ``subscribe`` / ``unsubscribe`` are **synchronous** — they mutate an
  in-memory dict guarded by a brief :class:`threading.Lock`. There is
  no I/O, no ``await``, and no reason for the caller to pay the cost of
  ``await event_bus.subscribe(...)``. This matches the documented
  example in :mod:`nexusdl.core.downloader.progress`
  (``event_bus.subscribe(...)`` without ``await``).
* ``publish`` is **asynchronous**. It takes a snapshot of the applicable
  handler lists under the same lock (a `tuple` copy of each list, taken
  in microseconds), releases the lock, then awaits each handler in turn.
  Two coroutines that publish concurrently each see their own snapshot
  and each awaits its own handler invocations — they do not block each
  other, and handlers that carry state must be concurrency-safe.
* A handler subscribed **during** a publish does not receive the event
  that is currently being delivered (it was not in the snapshot). It
  receives the **next** event. A handler unsubscribed during a publish
  may still receive the current event — same reasoning, the snapshot
  was taken before the mutation.

Why ``threading.Lock`` and not ``asyncio.Lock``
-----------------------------------------------
The PROMPT MASTER names ``asyncio.Lock`` as the concurrency primitive
for the bus. This implementation uses :class:`threading.Lock` instead,
for three reasons:

1. **``subscribe`` / ``unsubscribe`` are synchronous** (see above). An
   ``asyncio.Lock`` would force them to be ``async def``, breaking the
   documented example in :mod:`nexusdl.core.downloader.progress` and
   the ergonomic principle that a method which never waits should not
   be awaitable.
2. **The critical sections never ``await``.** ``subscribe`` appends to
   a list under a lock; ``publish`` copies a list under a lock. Both
   operations are pure CPU, measured in nanoseconds. ``asyncio.Lock``
   provides no benefit for CPU-bound critical sections.
3. **The lock is thread-safe as well as coroutine-safe.** A caller
   that subscribes from a synchronous setup function (CLI bootstrap,
   test fixtures, pytest hooks) can do so without running an event
   loop first. ``asyncio.Lock`` would raise ``RuntimeError`` in that
   case.

The concurrency guarantee is identical either way: a consistent
snapshot of the handler lists is what drives delivery, and mutations
are serialised.

What this module does **not** do
--------------------------------
* **No persistence, no replay, no history.** An event delivered to
  zero subscribers is gone. A subscriber that was disconnected when an
  event fired does not receive it when it reconnects. Subscribers that
  need state must read it from the source (a database, a stats object)
  rather than reconstructing it from a stream of events.
* **No synchronous dispatch.** Every handler is awaited. A synchronous
  handler that does a blocking I/O call would stall the event loop for
  every other subscriber — wrapping it in ``async def`` forces the
  caller to make that choice explicit (usually via ``asyncio.to_thread``).
* **No ordering across concurrent publishes.** If two publishes are in
  flight, handler A for event1 may run after handler B for event2, even
  if event1 was published first. Serialisation of publishes (which
  would give total order) is deliberately not provided — it would
  block the entire pipeline on a single slow handler. A caller that
  needs total order uses the event's ``timestamp`` or ``id`` fields.
* **No typed event catalog at the type level.** The event *names* are
  centralised in :mod:`nexusdl.core.constants` (twelve ``EVENT_*``
  constants), but ``Event.type`` remains a :class:`str`. A future
  version may introduce a discriminated union of typed event payloads;
  the current contract prioritises simplicity and lets the payload
  shape be a plain mapping.

Dependency graph (no cycles)::

    loguru       ──► core.events
    asyncio, threading, dataclasses, datetime, uuid, types (stdlib)

This module is a **leaf** on the project side: it imports nothing from
``nexusdl`` and is imported by ``downloader``, ``library``, ``plugins``,
``session``, and ``interfaces``. ``loguru`` is a runtime dependency
already used by the rest of the project.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID, uuid4

from loguru import logger


__all__ = ["Event", "EventBus", "Handler", "WILDCARD"]


# --------------------------------------------------------------------------- #
# Constants and types                                                         #
# --------------------------------------------------------------------------- #

#: The wildcard event type. A handler subscribed under this key receives
#: **every** event the bus publishes, regardless of its type. Used by
#: cross-cutting consumers (a global logger, an event recorder, a
#: debug tracer) that need to observe the entire stream.
#:
#: Not a valid value for :attr:`Event.type` — a publisher that wanted to
#: emit a real event named ``"*"`` would collide with the wildcard
#: subscription mechanism. :meth:`EventBus.publish` does not reject the
#: type, but the collision would make the event deliverable to wildcard
#: subscribers twice (once via ``event.type``, once via ``"*"``), which
#: is a bug in the publisher, not in the bus.
WILDCARD: Final[str] = "*"

#: The signature of an event handler. Handlers are **asynchronous**;
#: synchronous handlers are not supported — see the module docstring for
#: the rationale.
#:
#: The event is passed by reference; the handler must not mutate it
#: (the dataclass is frozen and the payload is a shallow-immutable
#: :class:`types.MappingProxyType`, so an attempt to mutate the top
#: level raises). Nested mutable values inside the payload are **not**
#: frozen — a handler that mutates them is undefined behaviour.
Handler = Callable[["Event"], Awaitable[None]]


_log = logger.bind(component="events")


# --------------------------------------------------------------------------- #
# Event                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class Event:
    """A single event delivered on the bus.

    Frozen dataclass — every field is set at construction time and never
    reassigned. Equality is by :attr:`id` (matching the entity-based
    equality used by :class:`~nexusdl.core.models.manga.Manga`,
    :class:`~nexusdl.core.models.chapter.Chapter`, and
    :class:`~nexusdl.core.models.user.User`); two events with the same
    ``id`` are the same event, regardless of their payload.

    **Payload immutability.** The payload is wrapped in a
    :class:`types.MappingProxyType` at construction time, so
    ``event.payload["new_key"] = value`` raises
    :class:`TypeError`. This protects a handler that is currently
    reading the event from being surprised by an earlier handler
    mutating it. The protection is **shallow** — nested mutable values
    (a ``dict`` nested inside the payload, a list of dicts) are not
    copied and not frozen. The pipeline's events carry flat payloads so
    this limitation is not exercised, but a plugin that emits nested
    data should treat the payload as read-only by convention.

    **Independence from the caller.** The payload is always **copied**
    (via ``dict(self.payload)``) before being wrapped, **including**
    when the caller passes a :class:`~types.MappingProxyType` whose
    underlying dict they still hold a reference to. This guarantees
    that a subsequent mutation of the caller's dict cannot reach the
    event's payload, which is the point of freezing.

    Attributes:
        type: The event type, e.g. ``"download.progress"``. The twelve
            canonical event names are defined in
            :mod:`nexusdl.core.constants` as ``EVENT_*`` constants; a
            plugin that emits custom events uses a name namespaced
            under ``plugin.<plugin_name>.*`` so it cannot collide with
            a framework event.
        timestamp: Timezone-aware UTC timestamp of the event. The
            worker, the manager, and the plugin example all use
            ``datetime.now(UTC)``; a caller constructing an event
            manually must do the same (the field rejects naive
            datetimes — a naive timestamp in an event is unambiguously
            a bug, since every other timestamp the pipeline produces is
            UTC).
        payload: A shallow-immutable mapping from string keys to
            arbitrary values. The shape is defined per event type —
            see :mod:`nexusdl.core.constants` for the canonical payload
            fields of the twelve framework events. Empty when the
            caller has nothing to convey (a rare case; most events
            carry at least a ``task_id``).
        id: A UUID4 identifying this event. Exposed so a consumer can
            correlate an event with logs, traces, or a subsequent
            acknowledgement, and used for :meth:`__eq__` /
            :meth:`__hash__`. Generated fresh by default — the caller
            does not have to provide one.
    """

    type: str
    timestamp: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        """Validate field types and freeze the payload.

        Raises:
            TypeError: If a field has the wrong Python type.
            ValueError: If ``type`` is empty or ``timestamp`` is
                timezone-naive.
        """
        if not isinstance(self.type, str):
            raise TypeError(
                f"Event.type must be a str, got "
                f"{type(self.type).__name__}"
            )
        if not self.type:
            raise ValueError("Event.type must be non-empty")

        if not isinstance(self.timestamp, datetime):
            raise TypeError(
                f"Event.timestamp must be a datetime, got "
                f"{type(self.timestamp).__name__}"
            )
        if self.timestamp.tzinfo is None:
            raise ValueError(
                "Event.timestamp must be timezone-aware; use "
                "datetime.now(UTC)"
            )

        if not isinstance(self.payload, Mapping):
            raise TypeError(
                f"Event.payload must be a mapping, got "
                f"{type(self.payload).__name__}"
            )
        # Always copy the payload before wrapping it. This guarantees
        # that a caller who still holds a reference to the dict (or to
        # the backing dict of a MappingProxyType) cannot mutate the
        # event's payload afterwards. The copy is cheap — payloads
        # contain a handful of keys — and unconditional, so the
        # immutability contract does not depend on the input type.
        object.__setattr__(
            self, "payload", MappingProxyType(dict(self.payload))
        )

        if not isinstance(self.id, UUID):
            raise TypeError(
                f"Event.id must be a UUID, got "
                f"{type(self.id).__name__}"
            )

    # ------------------------------------------------------------------ #
    # Identity                                                           #
    # ------------------------------------------------------------------ #

    def __eq__(self, other: object) -> bool:
        """Compare by :attr:`id` only, matching entity semantics."""
        if not isinstance(other, Event):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        """Hash by :attr:`id` only, matching :meth:`__eq__`."""
        return hash(self.id)

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact representation that does not dump the payload.

        Payload values can be large objects, structured data, or (in
        principle) values the caller did not intend to log. Only the
        payload's **keys** are shown — enough to identify the event
        shape without leaking content.
        """
        return (
            f"Event(type={self.type!r}, "
            f"id={self.id!s}, "
            f"timestamp={self.timestamp.isoformat()!r}, "
            f"payload_keys={sorted(self.payload)})"
        )


# --------------------------------------------------------------------------- #
# EventBus                                                                    #
# --------------------------------------------------------------------------- #


class EventBus:
    """In-memory publish/subscribe bus.

    See the module docstring for the full contract. One instance per
    composition root, shared across every module in the pipeline. The
    bus holds no resources beyond its subscription dict — there is no
    ``close()`` or ``__aexit__``; a bus is garbage-collected when its
    last reference goes away.

    Example:
        >>> bus = EventBus()

        >>> async def on_started(event: Event) -> None:
        ...     print(f"download started: {event.payload['task_id']}")

        >>> bus.subscribe(EVENT_DOWNLOAD_STARTED, on_started)
        >>> await bus.publish(Event(
        ...     type=EVENT_DOWNLOAD_STARTED,
        ...     timestamp=datetime.now(UTC),
        ...     payload={"task_id": "abc"},
        ... ))
        download started: abc

    Args:
        (none) — a bus is constructed empty. Subscription happens after.
    """

    __slots__ = ("_handlers", "_lock")

    def __init__(self) -> None:
        #: Mapping from event type to the ordered list of handlers
        #: subscribed under that type. The special key :data:`WILDCARD`
        #: holds wildcard subscribers, which are delivered every event.
        self._handlers: dict[str, list[Handler]] = {}
        #: Serialises every mutation of :attr:`_handlers` and every
        #: snapshot taken by :meth:`publish`. See the module docstring
        #: for why it is :class:`threading.Lock` and not
        #: :class:`asyncio.Lock`.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Subscription lifecycle                                             #
    # ------------------------------------------------------------------ #

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register *handler* to receive every event of *event_type*.

        The handler is appended to the end of the list for *event_type*;
        handlers run in subscription order. Calling :meth:`subscribe`
        twice with the same ``(event_type, handler)`` pair registers the
        handler **twice** — the caller is responsible for deduplication
        when that is not the intended behaviour.

        Wildcard subscriptions use ``event_type=WILDCARD`` and receive
        every event regardless of its type.

        This method is **synchronous**; see the module docstring for the
        rationale.

        Args:
            event_type: The event type to subscribe to, e.g.
                ``EVENT_DOWNLOAD_PROGRESS``, or :data:`WILDCARD` for
                every event. Must be a non-empty string.
            handler: An asynchronous callable accepting an
                :class:`Event`. Must be an ``async def`` function or
                any callable returning an awaitable.

        Raises:
            ValueError: If *event_type* is empty.
            TypeError: If *event_type* is not a string, or *handler* is
                not callable.
        """
        if not isinstance(event_type, str):
            raise TypeError(
                f"event_type must be a str, got "
                f"{type(event_type).__name__}"
            )
        if not event_type:
            raise ValueError("event_type must be non-empty")
        if not callable(handler):
            raise TypeError(
                f"handler must be callable, got "
                f"{type(handler).__name__}"
            )

        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> bool:
        """Remove every occurrence of *handler* from *event_type*'s list.

        Idempotent — calling this on a handler that is not subscribed is
        a no-op and returns ``False``. If the handler was subscribed
        multiple times (see :meth:`subscribe`), every occurrence is
        removed.

        Occurrences are matched by **identity** (``is``), not equality
        (``==``). This matters for test doubles (``unittest.mock.Mock``,
        ``AsyncMock``) and for callables with a custom ``__eq__``: a
        caller that wants to remove *this* handler must not accidentally
        remove another handler that merely compares equal to it. Regular
        functions and bound methods — the shapes the pipeline actually
        uses — compare identically under ``is`` and ``==``, so this
        stricter matching is invisible in practice and safer in the edge
        cases.

        Note that a handler subscribed to **both** a specific event type
        and :data:`WILDCARD` must be unsubscribed from each separately
        — unsubscribing from one does not affect the other.

        This method is **synchronous**; see the module docstring for the
        rationale.

        Args:
            event_type: The event type the handler was subscribed under,
                or :data:`WILDCARD` to remove from the wildcard list.
            handler: The handler to remove.

        Returns:
            ``True`` if at least one occurrence was removed, ``False``
            if the handler was not subscribed to *event_type*.

        Raises:
            ValueError: If *event_type* is empty.
            TypeError: If *event_type* is not a string, or *handler* is
                not callable.
        """
        if not isinstance(event_type, str):
            raise TypeError(
                f"event_type must be a str, got "
                f"{type(event_type).__name__}"
            )
        if not event_type:
            raise ValueError("event_type must be non-empty")
        if not callable(handler):
            raise TypeError(
                f"handler must be callable, got "
                f"{type(handler).__name__}"
            )

        with self._lock:
            listeners = self._handlers.get(event_type)
            if not listeners:
                return False
            before = len(listeners)
            # Identity-based removal: ``is not`` instead of ``!=`` so
            # a mock or a custom ``__eq__`` cannot cause an unrelated
            # handler to be removed.
            remaining = [h for h in listeners if h is not handler]
            if len(remaining) == before:
                return False
            if remaining:
                self._handlers[event_type] = remaining
            else:
                del self._handlers[event_type]
            return True

    # ------------------------------------------------------------------ #
    # Delivery                                                           #
    # ------------------------------------------------------------------ #

    async def publish(self, event: Event) -> None:
        """Deliver *event* to every subscribed handler.

        Delivery order:

        1. Handlers subscribed to ``event.type`` exactly, in
           subscription order.
        2. Handlers subscribed to :data:`WILDCARD`, in subscription
           order.

        Each handler runs inside its own try/except. An exception raised
        by a handler is logged at ``WARNING`` and swallowed; the next
        handler still runs. ``asyncio.CancelledError`` is never caught —
        it propagates and cancels the caller. Every other
        ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``,
        ``GeneratorExit``) also propagates, because the isolation only
        covers ``Exception`` subclasses.

        The handler lists are snapshotted under the lock **before**
        delivery begins, so a handler subscribed during this call does
        not receive the current event (it receives the next one), and a
        handler unsubscribed during this call may still receive the
        current event (it was in the snapshot). See the module docstring
        for the reasoning.

        Concurrent calls to :meth:`publish` run their deliveries
        concurrently — the snapshot allows each call to proceed without
        blocking on the other. Handlers that carry mutable state must be
        concurrency-safe (the ``ProgressTracker`` in
        :mod:`nexusdl.core.downloader.progress` uses an internal
        :class:`asyncio.Lock` precisely for this reason).

        Args:
            event: The event to deliver. Must be an :class:`Event`
                instance; a duck-typed lookalike is rejected because the
                snapshot-iteration logic and the payload accessor in
                downstream handlers assume the concrete type.

        Raises:
            TypeError: If *event* is not an :class:`Event`.
            asyncio.CancelledError: Propagated from any handler (or from
                the caller itself, in which case delivery stops at the
                point of cancellation).
            BaseException: Any non-``Exception`` raised by a handler
                (``KeyboardInterrupt``, ``SystemExit``,
                ``GeneratorExit``) propagates. This is intentional: a
                Ctrl+C from a handler must stop the pipeline, not be
                swallowed.
        """
        if not isinstance(event, Event):
            raise TypeError(
                f"publish() expects an Event, got "
                f"{type(event).__name__}"
            )

        # Snapshot the applicable handler lists while holding the lock.
        # Both copies are `tuple(...)` so the delivery loop does not
        # observe any mutation made by a handler during the loop.
        with self._lock:
            exact = tuple(self._handlers.get(event.type, ()))
            wildcard = tuple(self._handlers.get(WILDCARD, ()))

        for handler in exact:
            await self._invoke(handler, event)
        for handler in wildcard:
            await self._invoke(handler, event)

    # ------------------------------------------------------------------ #
    # Diagnostics and test cleanup                                       #
    # ------------------------------------------------------------------ #

    def subscriber_count(self, event_type: str) -> int:
        """Return the number of handlers subscribed to *event_type* exactly.

        Wildcard subscribers are **not** counted — a caller that wants
        to know the effective number of handlers for an event computes
        ``subscriber_count(event.type) + subscriber_count(WILDCARD)``.

        Args:
            event_type: The event type to count. May be :data:`WILDCARD`
                to count wildcard subscribers.

        Returns:
            The number of registered handlers, or ``0`` for an unknown
            event type.
        """
        with self._lock:
            return len(self._handlers.get(event_type, ()))

    def clear(self) -> None:
        """Remove every subscription.

        Intended for tests and for a composition root that wants to
        reset the bus between test cases. Production code should not
        call this — a live pipeline expects its subscriptions to stay
        in place for the process lifetime.
        """
        with self._lock:
            self._handlers.clear()

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        with self._lock:
            types = len(self._handlers)
            total = sum(len(v) for v in self._handlers.values())
        return f"EventBus(event_types={types}, handlers={total})"

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _invoke(handler: Handler, event: Event) -> None:
        """Await *handler* with the event, isolating any exception it raises.

        The isolation is the whole point of the bus: one broken
        subscriber must never stop delivery to the others. The
        ``except Exception`` clause deliberately excludes
        :class:`asyncio.CancelledError` (which is a
        :class:`BaseException` subclass, not an :class:`Exception`) so
        cancellation propagates untouched. Every other ``BaseException``
        (``KeyboardInterrupt``, ``SystemExit``) also propagates for the
        same reason — a Ctrl+C must not be swallowed.

        Args:
            handler: The handler to invoke.
            event: The event to pass.
        """
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — isolation is the contract
            _log.warning(
                "Event handler {!r} failed for event {!r}: "
                "{}: {}",
                handler,
                event.type,
                type(exc).__name__,
                exc,
            )
