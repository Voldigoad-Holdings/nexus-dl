"""Priority queue for :class:`DownloadTask` objects.

The queue is the waiting room of the download pipeline: it holds tasks that
have been submitted but not yet started, orders them by priority, and hands
them out one at a time subject to a concurrency limit. It is a pure data
structure — no I/O, no events, no plugins. The manager owns the workflow.

Ordering
--------
Tasks are popped in this order:

1. :attr:`~nexusdl.core.models.enums.Priority.HIGH` before
   :attr:`~nexusdl.core.models.enums.Priority.NORMAL` before
   :attr:`~nexusdl.core.models.enums.Priority.LOW`.
2. At equal priority, FIFO (submission order).

The queue uses :mod:`heapq` with a tuple sort key
``(-priority.value, sequence, task_id)``. Negating the priority turns the
min-heap into a max-heap on priority; the monotonic sequence breaks ties by
submission order.

Concurrency limit
-----------------
:meth:`next` refuses to return a task when
``len(self._running) >= self._max_concurrent``. It returns ``None`` instead
of blocking, so the caller decides how to wait (typically via
``asyncio.sleep`` or an event subscription). The limit applies to
:attr:`~nexusdl.core.models.enums.DownloadStatus.RUNNING` tasks only —
PENDING, PAUSED, and terminal tasks do not count against it.

State machine
-------------
The queue enforces the transition table documented in the PROMPT MASTER::

    PENDING   → RUNNING | CANCELLED
    RUNNING   → DONE | PAUSED | FAILED
    PAUSED    → RUNNING
    DONE      → (terminal)
    FAILED    → (terminal)
    CANCELLED → (terminal)

Any other transition raises
:class:`~nexusdl.core.exceptions.InvalidStateTransitionError`. All state
mutations happen under the internal lock, so two concurrent callers never
observe an inconsistent state.

Public API
----------
The seven methods from the PROMPT MASTER:

* :meth:`submit` — add a PENDING task to the queue.
* :meth:`get` — retrieve a task by ID.
* :meth:`list` — list tasks, optionally filtered by status.
* :meth:`next` — pop the highest-priority PENDING task
  (``PENDING → RUNNING``), respecting the concurrency limit.
* :meth:`pause` — ``RUNNING → PAUSED``.
* :meth:`resume` — ``PAUSED → RUNNING``.
* :meth:`cancel` — ``PENDING → CANCELLED``.

Two extensions used by the manager:

* :meth:`mark_done` — ``RUNNING → DONE``.
* :meth:`mark_failed` — ``RUNNING → FAILED``.

These two are not part of the user-facing API. They exist because the queue
is the single authority on task status, and the manager must report terminal
transitions through it. Without them, the queue's running count would drift
out of sync with reality — `list()`, `next()`, and the concurrency limit all
depend on accurate status.

Why `resume()` does not check `max_concurrent`
----------------------------------------------
Per the state diagram, ``PAUSED → RUNNING`` is unconditionally valid. The
user explicitly requested the task to resume; honouring that even when it
temporarily pushes the running count above the limit is the correct
trade-off. *New* tasks are still gated by :meth:`next`. A ``DEBUG`` log is
emitted when the count exceeds the limit so the operator can see it.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Mapping
from itertools import count
from types import MappingProxyType
from typing import Final
from uuid import UUID

from loguru import logger

from nexusdl.core.exceptions import (
    InvalidStateTransitionError,
    NexusDLError,
)
from nexusdl.core.models.download import DownloadTask
from nexusdl.core.models.enums import DownloadStatus


__all__ = ["DownloadQueue"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default concurrency limit. Matches the module-level default used by
#: :class:`~nexusdl.core.downloader.worker.DownloadWorker` for pages, so a
#: freshly-initialised queue and worker are consistent out of the box. The
#: manager typically overrides both from ``Settings.downloader``.
DEFAULT_MAX_CONCURRENT: Final[int] = 4

#: Closed transition table for :class:`DownloadStatus`.
#:
#: Every state maps to the set of states it may transition **to**. Terminal
#: states map to the empty frozenset. Any transition not listed here is
#: rejected by :meth:`DownloadQueue._transition`. This table is the single
#: source of truth for the state machine — the manager, the CLI, the API
#: handlers, and the tests all rely on it without duplicating it.
_VALID_TRANSITIONS: Final[
    Mapping[DownloadStatus, frozenset[DownloadStatus]]
] = MappingProxyType(
    {
        DownloadStatus.PENDING: frozenset(
            {DownloadStatus.RUNNING, DownloadStatus.CANCELLED}
        ),
        DownloadStatus.RUNNING: frozenset(
            {
                DownloadStatus.DONE,
                DownloadStatus.PAUSED,
                DownloadStatus.FAILED,
            }
        ),
        DownloadStatus.PAUSED: frozenset({DownloadStatus.RUNNING}),
        DownloadStatus.DONE: frozenset(),
        DownloadStatus.FAILED: frozenset(),
        DownloadStatus.CANCELLED: frozenset(),
    }
)


# --------------------------------------------------------------------------- #
# Queue                                                                       #
# --------------------------------------------------------------------------- #


class DownloadQueue:
    """Priority queue of :class:`DownloadTask` objects with a concurrency gate.

    See the module docstring for the ordering, concurrency, and state
    machine contracts. This class is asyncio-native and must be used from a
    single event loop.

    Example:
        >>> queue = DownloadQueue(max_concurrent=4)
        >>> await queue.submit(task)
        >>> next_task = await queue.next()  # returns None if saturated
        >>> # ... run the task ...
        >>> await queue.mark_done(str(next_task.id))
    """

    def __init__(self, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> None:
        """Initialise an empty queue.

        Args:
            max_concurrent: Maximum number of :attr:`DownloadStatus.RUNNING`
                tasks. Must be ``>= 1``. Applies only to tasks handed out by
                :meth:`next`; :meth:`resume` may temporarily exceed it.

        Raises:
            ValueError: If ``max_concurrent < 1``.
        """
        if max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be >= 1, got {max_concurrent}"
            )
        self._max_concurrent = max_concurrent

        #: All tasks ever submitted, keyed by ``str(task.id)``. Retains
        #: terminal tasks so that :meth:`get` / :meth:`list` can report
        #: history. Bounded by user behaviour (a session submits finitely
        #: many tasks).
        self._tasks: dict[str, DownloadTask] = {}

        #: Min-heap of ``(-priority.value, sequence, task_id)`` for PENDING
        #: tasks only. Entries become stale when a PENDING task is cancelled
        #: or transitions elsewhere; :meth:`next` skips them lazily.
        self._heap: list[tuple[int, int, str]] = []

        #: Monotonic counter guaranteeing FIFO at equal priority.
        self._sequence = count()

        #: Ids of tasks currently in :attr:`DownloadStatus.RUNNING`. Kept in
        #: sync with ``_tasks`` by every transition that enters or leaves
        #: RUNNING; ``len(self._running)`` is the O(1) concurrency gate.
        self._running: set[str] = set()

        #: Serialises every mutation. Reads also take it to ensure
        #: `get`/`list` never observe a half-updated state.
        self._lock = asyncio.Lock()

        self._log = logger.bind(component="downloader.queue")

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def submit(self, task: DownloadTask) -> None:
        """Add a PENDING task to the queue.

        The task is enqueued at its :attr:`DownloadTask.priority`. Submission
        is O(log n) in the number of pending tasks.

        Args:
            task: The task to enqueue. Must be in
                :attr:`DownloadStatus.PENDING`.

        Raises:
            NexusDLError: If a task with the same ``str(task.id)`` is already
                in the queue (including terminal ones — the registry retains
                them).
            InvalidStateTransitionError: If ``task.status`` is not
                :attr:`DownloadStatus.PENDING`.
        """
        task_id = str(task.id)
        async with self._lock:
            if task_id in self._tasks:
                raise NexusDLError(
                    f"task {task_id!r} is already in the queue"
                )
            if task.status is not DownloadStatus.PENDING:
                raise InvalidStateTransitionError(
                    f"cannot submit task {task_id!r}: status is "
                    f"{task.status.value!r}, expected "
                    f"{DownloadStatus.PENDING.value!r}"
                )
            self._tasks[task_id] = task
            heapq.heappush(
                self._heap,
                (-int(task.priority.value), next(self._sequence), task_id),
            )
            self._log.debug(
                "Submitted task {} (priority={}, pending={})",
                task_id,
                task.priority.name,
                len(self._heap),
            )

    async def get(self, task_id: str) -> DownloadTask | None:
        """Return the task with id ``task_id``, or ``None`` if unknown.

        Args:
            task_id: The task's id, as a string.
        """
        async with self._lock:
            return self._tasks.get(task_id)

    async def list(
        self, status: DownloadStatus | None = None
    ) -> list[DownloadTask]:
        """Return tasks, optionally filtered by ``status``.

        Args:
            status: When provided, only tasks in that exact status are
                returned. When ``None``, every task ever submitted is
                returned, including terminal ones.

        Returns:
            A new list. Mutating it does not affect the queue. The returned
            tasks are the queue's own objects — the caller must not mutate
            their ``status`` field directly (use :meth:`pause`,
            :meth:`resume`, :meth:`cancel`, :meth:`mark_done`,
            :meth:`mark_failed` instead, which validate transitions).
        """
        async with self._lock:
            if status is None:
                return list(self._tasks.values())
            return [
                task
                for task in self._tasks.values()
                if task.status is status
            ]

    async def next(self) -> DownloadTask | None:
        """Pop the highest-priority PENDING task and mark it RUNNING.

        Returns ``None`` when either:

        * the number of RUNNING tasks already equals ``max_concurrent``;
        * no PENDING task is available (including when the heap holds only
          stale entries left by cancelled tasks — those are discarded).

        The transition ``PENDING → RUNNING`` happens atomically under the
        lock, so two concurrent callers never both receive the same task.

        Returns:
            The next task to run, already in
            :attr:`DownloadStatus.RUNNING`, or ``None``.
        """
        async with self._lock:
            if len(self._running) >= self._max_concurrent:
                return None
            while self._heap:
                _, _, task_id = heapq.heappop(self._heap)
                task = self._tasks.get(task_id)
                if task is None or task.status is not DownloadStatus.PENDING:
                    # Stale entry — the task was cancelled or otherwise left
                    # PENDING between submission and this pop.
                    continue
                self._transition(task, DownloadStatus.RUNNING)
                self._running.add(task_id)
                self._log.debug(
                    "Dispatched task {} (running={}/{})",
                    task_id,
                    len(self._running),
                    self._max_concurrent,
                )
                return task
            return None

    async def pause(self, task_id: str) -> bool:
        """Transition a RUNNING task to PAUSED.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if no
            task with that id exists.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                RUNNING.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            self._transition(task, DownloadStatus.PAUSED)
            self._running.discard(task_id)
            return True

    async def resume(self, task_id: str) -> bool:
        """Transition a PAUSED task back to RUNNING.

        See the module docstring for why this method does not check
        ``max_concurrent``: the transition ``PAUSED → RUNNING`` is
        unconditionally valid per the state diagram, and honouring an
        explicit user request is preferable to silently rejecting it. New
        tasks are still gated by :meth:`next`.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if no
            task with that id exists.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                PAUSED.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            self._transition(task, DownloadStatus.RUNNING)
            self._running.add(task_id)
            if len(self._running) > self._max_concurrent:
                self._log.debug(
                    "resume pushed RUNNING count above max_concurrent: "
                    "{} > {}",
                    len(self._running),
                    self._max_concurrent,
                )
            return True

    async def cancel(self, task_id: str) -> bool:
        """Transition a PENDING task to CANCELLED.

        Cancelling a RUNNING, PAUSED, or terminal task is rejected by the
        state machine (see :data:`_VALID_TRANSITIONS`). To cancel a running
        task, pause it first; the manager is then responsible for stopping
        its worker via :class:`asyncio.CancelledError`.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if no
            task with that id exists.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                PENDING.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            self._transition(task, DownloadStatus.CANCELLED)
            # PENDING → CANCELLED does not affect ``_running`` (the task was
            # not running). The heap still holds a stale entry for this id;
            # :meth:`next` skips it lazily.
            return True

    async def mark_done(self, task_id: str) -> bool:
        """Transition a RUNNING task to DONE.

        Manager-only API. Called by
        :class:`~nexusdl.core.downloader.manager.DownloadManager` after the
        worker returns a successful :class:`DownloadResult`. Not part of the
        user-facing surface.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if no
            task with that id exists.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                RUNNING.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            self._transition(task, DownloadStatus.DONE)
            self._running.discard(task_id)
            return True

    async def mark_failed(self, task_id: str) -> bool:
        """Transition a RUNNING task to FAILED.

        Manager-only API. Called by
        :class:`~nexusdl.core.downloader.manager.DownloadManager` after the
        worker returns a failed :class:`DownloadResult`. Not part of the
        user-facing surface.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if no
            task with that id exists.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                RUNNING.
        """
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            self._transition(task, DownloadStatus.FAILED)
            self._running.discard(task_id)
            return True

    # ------------------------------------------------------------------ #
    # Dunder helpers                                                     #
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        """Return the total number of tasks in the queue, at any status.

        Reads ``_tasks`` without taking :attr:`_lock`. On CPython this is
        safe because ``len(dict)`` is atomic and every mutation of
        ``_tasks`` is a single ``dict[key] = value`` assignment — no
        multi-step mutation of ``_tasks`` alone is observable. The method
        is intentionally synchronous so it can be used in ``len(queue)``
        and logging without awaiting.

        For the number of tasks *eligible to run*, use
        ``len(await queue.list(DownloadStatus.PENDING))`` or check
        :meth:`next`'s return value.
        """
        return len(self._tasks)

    def __contains__(self, task_id: object) -> bool:
        """Return ``True`` iff a task with id ``task_id`` is in the queue.

        Accepts both :class:`uuid.UUID` and :class:`str`. Any other type
        returns ``False`` rather than raising — ``in`` is expected to be
        total.
        """
        if isinstance(task_id, UUID):
            return str(task_id) in self._tasks
        if isinstance(task_id, str):
            return task_id in self._tasks
        return False

    def __repr__(self) -> str:
        """Return a compact debug representation.

        ``pending`` counts actual :attr:`DownloadStatus.PENDING` tasks, not
        heap entries — the heap may hold stale entries for tasks that were
        cancelled after submission, and reporting the heap size would
        overstate the pending count.
        """
        pending = sum(
            1
            for task in self._tasks.values()
            if task.status is DownloadStatus.PENDING
        )
        return (
            f"DownloadQueue(tasks={len(self._tasks)}, "
            f"pending={pending}, "
            f"running={len(self._running)}/{self._max_concurrent})"
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _transition(
        self, task: DownloadTask, new_status: DownloadStatus
    ) -> None:
        """Validate and apply a state transition.

        Private — every public method above calls this while holding
        :attr:`_lock`. The transition table lives in
        :data:`_VALID_TRANSITIONS`.

        Args:
            task: The task to transition.
            new_status: The target status.

        Raises:
            InvalidStateTransitionError: If ``new_status`` is not reachable
                from ``task.status`` per the state diagram.
        """
        old_status = task.status
        allowed = _VALID_TRANSITIONS.get(old_status, frozenset())
        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"cannot transition task {task.id!s} from "
                f"{old_status.value!r} to {new_status.value!r}"
            )
        task.status = new_status
