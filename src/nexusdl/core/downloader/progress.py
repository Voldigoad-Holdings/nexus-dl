"""Progress tracking for the download pipeline.

The worker (``core/downloader/worker.py``) emits per-task progress events on
the shared :class:`~nexusdl.core.events.EventBus`:

* ``download.started``   — ``{task_id, chapter_id, manga_id, page_count, format}``
* ``download.progress``  — ``{task_id, chapter_id, progress, phase}``
* ``download.completed`` — ``{task_id, chapter_id, output_path, ...}``
* ``download.failed``    — ``{task_id, chapter_id, error_type, error_message, ...}``

That is **push** semantics: fine for UIs that want live updates, expensive
for UIs that only care about the current state. This module provides the
**pull** counterpart: a mutable :class:`ProgressTracker` that subscribes to
those four events, maintains an in-memory snapshot of every task, and
exposes immutable :class:`TaskProgress` / :class:`ProgressSnapshot` views.

Wiring
------
The composition root (CLI/TUI/GUI/Web) creates one tracker and subscribes
it to the bus::

    tracker = ProgressTracker()
    for event_type in tracker.subscribed_event_types():
        event_bus.subscribe(event_type, tracker.handle_event)

    # later, whenever the UI needs the current state:
    snapshot = await tracker.snapshot()

No other module imports :class:`ProgressTracker` — the worker never sees
it, the manager never sees it, the queue never sees it. That keeps the
pipeline unaware of presentation concerns.

Design invariants
-----------------
* **Read-safe** — :meth:`ProgressTracker.snapshot` returns an immutable
  tuple. UIs can iterate it without holding any lock.
* **Bounded memory** — terminal tasks are kept in FIFO order and evicted
  once more than ``max_completed_history`` of them exist. A long-running
  server cannot leak.
* **Deterministic** — the tracker takes an injectable ``clock`` callable so
  tests can fake time without ``freezegun`` or ``asyncio.sleep``.
* **Idempotent event handling** — a malformed or duplicated event is
  logged at ``DEBUG`` and ignored; it never raises. ``handle_event`` is a
  bus subscriber and must never propagate.

ETA estimation
--------------
Both :attr:`TaskProgress.remaining_seconds` and
:attr:`ProgressSnapshot.eta_seconds` use linear extrapolation on the
progress rate observed since ``started_at``. When ``progress == 0`` or
``elapsed == 0``, they return ``None`` (insufficient data). The aggregate
ETA is the **maximum** of the active tasks' remaining times — the wall
clock at which the last task finishes, not the sum.

See also
--------
* :mod:`nexusdl.core.downloader.worker` — emits the events consumed here.
* :mod:`nexusdl.core.downloader.queue` — the pipeline's waiting room;
  :class:`ProgressTracker` does not look at it directly, but the two views
  are consistent because the manager drives both from the same tasks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Final, Literal, TypeAlias, cast

from loguru import logger

if TYPE_CHECKING:
    from nexusdl.core.events import Event


__all__ = [
    "PHASE_CANCELLED",
    "PHASE_DONE",
    "PHASE_FAILED",
    "PHASE_PACKAGING",
    "PHASE_PAGES",
    "PHASE_PENDING",
    "Phase",
    "ProgressSnapshot",
    "ProgressTracker",
    "TaskProgress",
]


# --------------------------------------------------------------------------- #
# Phase vocabulary                                                            #
# --------------------------------------------------------------------------- #
# These are user-visible: a UI may display "Downloading pages..." vs
# "Packaging...". The set is closed; the worker only ever emits PHASE_PAGES
# and PHASE_PACKAGING, while the remaining four are set by the tracker
# itself in response to lifecycle events.
# --------------------------------------------------------------------------- #

Phase: TypeAlias = Literal[
    "pending",
    "pages",
    "packaging",
    "done",
    "failed",
    "cancelled",
]

PHASE_PENDING: Final = "pending"
PHASE_PAGES: Final = "pages"
PHASE_PACKAGING: Final = "packaging"
PHASE_DONE: Final = "done"
PHASE_FAILED: Final = "failed"
PHASE_CANCELLED: Final = "cancelled"

#: Phases from which no further updates are accepted. Any event targeting a
#: task in one of these phases is logged and dropped by
#: :meth:`ProgressTracker.update_task`.
_TERMINAL_PHASES: Final[frozenset[str]] = frozenset(
    {PHASE_DONE, PHASE_FAILED, PHASE_CANCELLED}
)

#: Phases that :meth:`ProgressTracker.update_task` may set. Excludes
#: terminal phases (which require dedicated methods) and ``pending`` (which
#: is set by :meth:`ProgressTracker.start_task`, not by an in-flight update).
_UPDATABLE_PHASES: Final[frozenset[str]] = frozenset(
    {PHASE_PENDING, PHASE_PAGES, PHASE_PACKAGING}
)


# --------------------------------------------------------------------------- #
# Event type constants                                                        #
# --------------------------------------------------------------------------- #
# Mirrors the strings emitted by ``worker.py``. Kept as module-private
# constants (not exported) because the tracker is the only consumer that
# needs the literal values — everything else that cares should subscribe to
# the bus with the strings themselves or use ``subscribed_event_types()``.
# --------------------------------------------------------------------------- #

_EVENT_DOWNLOAD_STARTED: Final = "download.started"
_EVENT_DOWNLOAD_PROGRESS: Final = "download.progress"
_EVENT_DOWNLOAD_COMPLETED: Final = "download.completed"
_EVENT_DOWNLOAD_FAILED: Final = "download.failed"

#: Default FIFO cap on terminal tasks retained in memory. 1000 is more than
#: enough for any single session's history while bounding memory for a
#: long-running server: 1000 × ~500 bytes ≈ 500 KiB.
_DEFAULT_MAX_COMPLETED_HISTORY: Final[int] = 1000


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    Default clock for :class:`ProgressTracker`. Factored out so the tracker
    can accept an injectable clock for tests without repeating
    ``lambda: datetime.now(UTC)`` at each call site.
    """
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Immutable progress views                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Immutable snapshot of a single task's progress.

    Produced by :meth:`ProgressTracker.get_task` and included in every
    :class:`ProgressSnapshot`. Callers can safely cache, compare, and pass
    this object around; it has no reference to the tracker's mutable state.

    Attributes:
        task_id: The task's id, as a string (``str(task.id)``).
        chapter_id: The chapter's id, as a string.
        phase: Current phase. See :data:`Phase` for the closed set.
        progress: Completion fraction in ``[0.0, 1.0]``. Taken verbatim
            from the last ``download.progress`` event, or forced to ``1.0``
            once the task completes.
        pages_total: Total pages in the chapter, from
            ``download.started``'s ``page_count``. ``0`` if unknown.
        started_at: UTC timestamp of ``download.started``.
        updated_at: UTC timestamp of the most recent update.
        error: The failure message for tasks in :data:`PHASE_FAILED`;
            ``None`` otherwise.
    """

    task_id: str
    chapter_id: str
    phase: Phase
    progress: float
    pages_total: int
    started_at: datetime
    updated_at: datetime
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        """``True`` iff the task is in a terminal phase.

        Terminal phases are :data:`PHASE_DONE`, :data:`PHASE_FAILED`, and
        :data:`PHASE_CANCELLED`.
        """
        return self.phase in _TERMINAL_PHASES

    @property
    def elapsed_seconds(self) -> float:
        """Seconds elapsed since :attr:`started_at`, at :attr:`updated_at`.

        The value is derived from wall-clock timestamps. It is normally
        positive, but can be negative if the injectable ``clock`` returns
        a value earlier than :attr:`started_at` (a misconfigured test clock,
        for instance). :attr:`remaining_seconds` guards against that case.
        """
        return (self.updated_at - self.started_at).total_seconds()

    @property
    def remaining_seconds(self) -> float | None:
        """Estimated seconds until this task completes.

        Linear extrapolation: ``(1 - progress) / progress * elapsed``. This
        assumes a constant rate since :attr:`started_at`, which is a good
        enough approximation for display.

        Returns:
            ``0.0`` for terminal tasks; ``None`` when there is not enough
            data yet (``progress == 0`` or ``elapsed <= 0``); otherwise the
            estimate in seconds.
        """
        if self.is_terminal:
            return 0.0
        if self.progress <= 0.0:
            return None
        elapsed = self.elapsed_seconds
        if elapsed <= 0.0:
            return None
        return (1.0 - self.progress) / self.progress * elapsed


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Immutable aggregate view of every task the tracker knows about.

    Tasks are ordered by ``(started_at, task_id)``, so two snapshots taken
    in the same session have consistent ordering and can be diffed visually.

    Attributes:
        tasks: All tracked tasks, in submission order.
        captured_at: UTC timestamp at which the snapshot was taken.
    """

    tasks: tuple[TaskProgress, ...]
    captured_at: datetime

    @property
    def total(self) -> int:
        """Total number of tracked tasks, at any phase."""
        return len(self.tasks)

    @property
    def active(self) -> tuple[TaskProgress, ...]:
        """Tasks that are not yet terminal, in submission order."""
        return tuple(t for t in self.tasks if not t.is_terminal)

    @property
    def completed(self) -> int:
        """Number of tasks in :data:`PHASE_DONE`."""
        return sum(1 for t in self.tasks if t.phase == PHASE_DONE)

    @property
    def failed(self) -> int:
        """Number of tasks in :data:`PHASE_FAILED`."""
        return sum(1 for t in self.tasks if t.phase == PHASE_FAILED)

    @property
    def cancelled(self) -> int:
        """Number of tasks in :data:`PHASE_CANCELLED`."""
        return sum(1 for t in self.tasks if t.phase == PHASE_CANCELLED)

    @property
    def is_done(self) -> bool:
        """``True`` iff every tracked task is in a terminal phase.

        An empty snapshot returns ``True`` — vacuously, everything is done.
        """
        return all(t.is_terminal for t in self.tasks)

    @property
    def overall_progress(self) -> float:
        """Weighted average of each task's ``progress``, in ``[0.0, 1.0]``.

        Weights are ``max(pages_total, 1)`` so a 200-page chapter contributes
        more than a 5-page one. Failed and cancelled tasks contribute their
        final ``progress`` value — the fraction of work that was actually
        completed before the failure.

        Returns ``0.0`` for an empty snapshot.
        """
        if not self.tasks:
            return 0.0
        weights = [max(t.pages_total, 1) for t in self.tasks]
        total_weight = sum(weights)
        weighted = sum(
            t.progress * w for t, w in zip(self.tasks, weights, strict=True)
        )
        return weighted / total_weight

    @property
    def eta_seconds(self) -> float | None:
        """Estimated seconds until every active task finishes.

        Returns the **maximum** of the active tasks' remaining times: the
        wall-clock time at which the last task completes, not the sum (tasks
        run in parallel). Terminal tasks contribute zero.

        Returns:
            ``0.0`` when no task is active; ``None`` when at least one
            active task lacks a rate estimate (see
            :attr:`TaskProgress.remaining_seconds`).
        """
        active = [t for t in self.tasks if not t.is_terminal]
        if not active:
            return 0.0
        etas = [t.remaining_seconds for t in active]
        if any(eta is None for eta in etas):
            return None
        # ``etas`` is now guaranteed to contain only floats.
        return max(cast("list[float]", etas))


# --------------------------------------------------------------------------- #
# Mutable tracker                                                             #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _MutableTaskState:
    """Internal mutable state for one task. Never leaves this module."""

    task_id: str
    chapter_id: str
    phase: Phase
    progress: float
    pages_total: int
    started_at: datetime
    updated_at: datetime
    error: str | None = None
    sequence: int = 0

    def to_immutable(self) -> TaskProgress:
        """Project this mutable state onto an immutable :class:`TaskProgress`."""
        return TaskProgress(
            task_id=self.task_id,
            chapter_id=self.chapter_id,
            phase=self.phase,
            progress=self.progress,
            pages_total=self.pages_total,
            started_at=self.started_at,
            updated_at=self.updated_at,
            error=self.error,
        )


class ProgressTracker:
    """Subscribe to download events and expose pull-style progress views.

    One instance per composition root. Thread-safe for asyncio: every public
    method takes an :class:`asyncio.Lock`, so a single tracker can be shared
    across concurrent handlers.

    Example:
        >>> tracker = ProgressTracker()
        >>> for event_type in tracker.subscribed_event_types():
        ...     event_bus.subscribe(event_type, tracker.handle_event)
        >>> # ...after some downloads...
        >>> snap = await tracker.snapshot()
        >>> print(f"{snap.overall_progress:.0%} done, ETA {snap.eta_seconds}s")

    Args:
        max_completed_history: Maximum number of terminal tasks retained in
            memory. Once exceeded, the oldest terminal task is evicted (FIFO
            by transition time). Set to ``0`` to disable history entirely
            — useful in tests that want to assert that memory does not grow.
            The default (1000) is generous.
        clock: Callable returning a timezone-aware ``datetime``. Defaults to
            ``datetime.now(UTC)``. Injectable for tests.
    """

    def __init__(
        self,
        *,
        max_completed_history: int = _DEFAULT_MAX_COMPLETED_HISTORY,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if max_completed_history < 0:
            raise ValueError(
                f"max_completed_history must be >= 0, got {max_completed_history}"
            )
        self._max_completed_history = max_completed_history
        self._clock = clock
        self._tasks: dict[str, _MutableTaskState] = {}
        #: Insertion-ordered dict of terminal task ids. Replaces a deque so
        #: that re-registering the same id cannot leave duplicate entries
        #: (which would let an old entry evict a freshly-resubmitted task).
        self._terminal_order: dict[str, None] = {}
        self._sequence = count()
        self._lock = asyncio.Lock()
        self._log = logger.bind(component="downloader.progress")

    # ------------------------------------------------------------------ #
    # EventBus integration                                                #
    # ------------------------------------------------------------------ #

    @classmethod
    def subscribed_event_types(cls) -> tuple[str, ...]:
        """Return the event types this tracker wants to receive.

        Intended for composition roots that wire the tracker to the bus::

            for event_type in ProgressTracker.subscribed_event_types():
                event_bus.subscribe(event_type, tracker.handle_event)

        Returns:
            The four ``download.*`` event types the tracker handles.
        """
        return (
            _EVENT_DOWNLOAD_STARTED,
            _EVENT_DOWNLOAD_PROGRESS,
            _EVENT_DOWNLOAD_COMPLETED,
            _EVENT_DOWNLOAD_FAILED,
        )

    async def handle_event(self, event: Event) -> None:
        """Dispatch a bus event to the appropriate tracker method.

        This is the only method intended to be subscribed to the
        :class:`~nexusdl.core.events.EventBus`. It is defensive: malformed
        or unknown events are logged at ``DEBUG`` and dropped. It never
        raises, because the bus isolates subscriber exceptions anyway — but
        we log locally so the tracker's own misbehaviour is visible without
        enabling bus-level tracing.

        Args:
            event: The bus event. Only its ``type`` and ``payload`` are used.
        """
        payload = event.payload
        event_type = event.type

        if event_type == _EVENT_DOWNLOAD_STARTED:
            await self._handle_started(payload)
            return
        if event_type == _EVENT_DOWNLOAD_PROGRESS:
            await self._handle_progress(payload)
            return
        if event_type == _EVENT_DOWNLOAD_COMPLETED:
            await self._handle_completed(payload)
            return
        if event_type == _EVENT_DOWNLOAD_FAILED:
            await self._handle_failed(payload)
            return
        # Not ours; ignore silently.

    # ------------------------------------------------------------------ #
    # Public mutators (also called directly by handle_event)             #
    # ------------------------------------------------------------------ #

    async def start_task(
        self,
        task_id: str,
        chapter_id: str,
        *,
        pages_total: int = 0,
    ) -> None:
        """Begin tracking a task.

        If the id is already tracked, the existing entry is overwritten and
        any stale terminal-history reference is dropped. Idempotent — the
        composition root may call this on ``download.started`` without
        checking whether the tracker already knows about the task.

        Args:
            task_id: The task's id, as a string.
            chapter_id: The chapter's id, as a string.
            pages_total: Total pages in the chapter. ``0`` when unknown.
        """
        now = self._clock()
        async with self._lock:
            # If this id was previously terminal, remove its history entry
            # so a future eviction cannot target the new registration.
            self._terminal_order.pop(task_id, None)
            self._tasks[task_id] = _MutableTaskState(
                task_id=task_id,
                chapter_id=chapter_id,
                phase=PHASE_PENDING,
                progress=0.0,
                pages_total=max(0, pages_total),
                started_at=now,
                updated_at=now,
                sequence=next(self._sequence),
            )

    async def update_task(
        self,
        task_id: str,
        *,
        progress: float,
        phase: str | None = None,
    ) -> None:
        """Update a task's progress and optionally its phase.

        Updates targeting an unknown task id, or a task already in a
        terminal phase, are logged at ``DEBUG`` and dropped. This is
        intentional: the download pipeline emits events with slight timing
        skew (a ``progress`` event may arrive after a ``failed`` event), and
        silently dropping late updates is the correct behaviour.

        Args:
            task_id: The task's id, as a string.
            progress: Completion fraction. Clamped to ``[0.0, 1.0]``.
            phase: Optional phase string. When provided, must be one of
                :data:`_UPDATABLE_PHASES` (``pending``, ``pages``,
                ``packaging``). Passing a terminal phase or an unknown value
                is logged and dropped; use :meth:`complete_task`,
                :meth:`fail_task`, or :meth:`cancel_task` for terminal
                transitions.
        """
        now = self._clock()
        async with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                self._log.debug(
                    "update_task for unknown task_id={}; ignoring", task_id
                )
                return
            if state.phase in _TERMINAL_PHASES:
                self._log.debug(
                    "update_task for terminal task_id={} (phase={}); ignoring",
                    task_id,
                    state.phase,
                )
                return
            if phase is not None:
                if phase in _TERMINAL_PHASES:
                    self._log.debug(
                        "update_task called with terminal phase={!r} for "
                        "task_id={}; use complete_task/fail_task/cancel_task "
                        "instead",
                        phase,
                        task_id,
                    )
                    return
                if phase not in _UPDATABLE_PHASES:
                    self._log.debug(
                        "update_task called with unknown phase={!r} for "
                        "task_id={}; ignoring",
                        phase,
                        task_id,
                    )
                    return
                state.phase = cast("Phase", phase)
            state.progress = max(0.0, min(1.0, float(progress)))
            state.updated_at = now

    async def complete_task(self, task_id: str) -> None:
        """Mark a task as done.

        Idempotent: calling on a terminal task is a no-op (logged at
        ``DEBUG``). Unknown task ids are also ignored.

        Args:
            task_id: The task's id, as a string.
        """
        now = self._clock()
        async with self._lock:
            state = self._tasks.get(task_id)
            if state is None or state.phase in _TERMINAL_PHASES:
                return
            state.phase = PHASE_DONE
            state.progress = 1.0
            state.error = None
            state.updated_at = now
            self._record_terminal(task_id)

    async def fail_task(self, task_id: str, *, error: str | None = None) -> None:
        """Mark a task as failed, preserving its last progress value.

        Idempotent. Unknown task ids are ignored.

        Args:
            task_id: The task's id, as a string.
            error: Optional human-readable error message. Stored on the
                resulting :class:`TaskProgress` for display.
        """
        now = self._clock()
        async with self._lock:
            state = self._tasks.get(task_id)
            if state is None or state.phase in _TERMINAL_PHASES:
                return
            state.phase = PHASE_FAILED
            state.error = error
            state.updated_at = now
            self._record_terminal(task_id)

    async def cancel_task(self, task_id: str) -> None:
        """Mark a task as cancelled, preserving its last progress value.

        Idempotent. Unknown task ids are ignored.

        Args:
            task_id: The task's id, as a string.
        """
        now = self._clock()
        async with self._lock:
            state = self._tasks.get(task_id)
            if state is None or state.phase in _TERMINAL_PHASES:
                return
            state.phase = PHASE_CANCELLED
            state.updated_at = now
            self._record_terminal(task_id)

    async def remove_task(self, task_id: str) -> bool:
        """Remove a task from the tracker entirely.

        The terminal-history map also drops the id lazily, so calling this
        on a terminal task is safe and does not leave a dangling reference
        in :attr:`_terminal_order`.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` iff a task with that id was tracked.
        """
        async with self._lock:
            removed = self._tasks.pop(task_id, None) is not None
            self._terminal_order.pop(task_id, None)
            return removed

    async def clear(self) -> None:
        """Drop every tracked task. Intended for tests and explicit resets."""
        async with self._lock:
            self._tasks.clear()
            self._terminal_order.clear()

    # ------------------------------------------------------------------ #
    # Public read API                                                     #
    # ------------------------------------------------------------------ #

    async def get_task(self, task_id: str) -> TaskProgress | None:
        """Return an immutable snapshot of a single task, or ``None``.

        Args:
            task_id: The task's id, as a string.
        """
        async with self._lock:
            state = self._tasks.get(task_id)
            return None if state is None else state.to_immutable()

    async def snapshot(self) -> ProgressSnapshot:
        """Return an immutable snapshot of every tracked task.

        Tasks are ordered by ``(started_at, sequence)`` — submission order,
        even when the injected clock returns identical timestamps for
        several tasks. The public ``TaskProgress`` view does not expose
        ``sequence``; it is an ordering key internal to this class.

        Returns:
            A :class:`ProgressSnapshot` frozen at the current clock value.
        """
        async with self._lock:
            now = self._clock()
            ordered = sorted(
                self._tasks.values(),
                key=lambda s: (s.started_at, s.sequence),
            )
            tasks = tuple(s.to_immutable() for s in ordered)
            return ProgressSnapshot(tasks=tasks, captured_at=now)

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _record_terminal(self, task_id: str) -> None:
        """Record *task_id* as terminal and evict on overflow.

        Caller must hold :attr:`_lock`. Uses an insertion-ordered dict so a
        task that is completed, removed, and resubmitted does not leave a
        stale entry capable of evicting the new registration.

        Args:
            task_id: The id of a task that just became terminal.
        """
        # Re-registering the same id replaces the entry in place, keeping
        # insertion order at the most-recent position.
        self._terminal_order.pop(task_id, None)
        self._terminal_order[task_id] = None
        while len(self._terminal_order) > self._max_completed_history:
            oldest, _ = self._terminal_order.popitem(last=False)
            state = self._tasks.get(oldest)
            # Only evict if the task is still terminal. A user could, in
            # theory, re-submit the same id after removing it — in that
            # case the map entry is stale and must not evict the new task.
            if state is not None and state.phase in _TERMINAL_PHASES:
                del self._tasks[oldest]

    async def _handle_started(self, payload: dict[str, object]) -> None:
        """Process a ``download.started`` event payload.

        Registers the task in the tracker. Without this, tasks that fail
        before emitting any ``download.progress`` (e.g. ``EmptyChapterError``
        raised from ``get_pages``) would never be visible in snapshots.

        Args:
            payload: The event's payload dict.
        """
        task_id = payload.get("task_id")
        chapter_id = payload.get("chapter_id")
        if not isinstance(task_id, str):
            self._log.debug(
                "Ignoring download.started event without task_id: {}", payload
            )
            return
        if not isinstance(chapter_id, str):
            self._log.debug(
                "Ignoring download.started event without chapter_id: {}",
                payload,
            )
            return
        page_count = payload.get("page_count")
        pages_total = page_count if isinstance(page_count, int) else 0
        await self.start_task(task_id, chapter_id, pages_total=pages_total)

    async def _handle_progress(self, payload: dict[str, object]) -> None:
        """Process a ``download.progress`` event payload."""
        task_id = payload.get("task_id")
        progress = payload.get("progress")
        phase = payload.get("phase", PHASE_PAGES)
        if not isinstance(task_id, str):
            self._log.debug(
                "Ignoring download.progress event without task_id: {}", payload
            )
            return
        if not isinstance(progress, (int, float)):
            self._log.debug(
                "Ignoring download.progress event without numeric progress: {}",
                payload,
            )
            return
        phase_str: str | None = phase if isinstance(phase, str) else None
        await self.update_task(
            task_id, progress=float(progress), phase=phase_str
        )

    async def _handle_completed(self, payload: dict[str, object]) -> None:
        """Process a ``download.completed`` event payload."""
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            self._log.debug(
                "Ignoring download.completed event without task_id: {}",
                payload,
            )
            return
        await self.complete_task(task_id)

    async def _handle_failed(self, payload: dict[str, object]) -> None:
        """Process a ``download.failed`` event payload."""
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            self._log.debug(
                "Ignoring download.failed event without task_id: {}", payload
            )
            return
        error_value = payload.get("error_message")
        error = error_value if isinstance(error_value, str) else None
        await self.fail_task(task_id, error=error)
