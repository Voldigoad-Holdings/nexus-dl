"""Orchestrate :class:`DownloadTask` objects across parsers, workers, queue,
plugins, events, and the shared lifecycle.

The manager is the download pipeline's conductor. It knows:

* **how to run a task** — through an injected
  :class:`~nexusdl.core.downloader.worker.DownloadWorker`, built by an
  injected ``worker_factory``;
* **which parser to use** — through an injected registry satisfying
  :class:`ParserResolverProtocol`;
* **how many may run at once** — delegated entirely to
  :class:`~nexusdl.core.downloader.queue.DownloadQueue` via its own
  ``max_concurrent`` gate, itself wired from
  ``Settings.downloader.max_concurrent_tasks``;
* **what to tell the rest of the system** — ``download.*`` events from
  the worker, ``chapter.*`` events from the manager itself, and
  ``download.paused``/``resumed``/``cancelled`` events on lifecycle
  operations.

The manager does **not** know:

* how pages are downloaded (worker);
* how chapters are paginated (parser);
* what a plugin does (dispatcher);
* what a queue entry looks like (queue);
* what the output file is called (worker).

Each of those is a well-isolated collaborator, injected as a structural
Protocol or as a ready-to-use object. The result is a manager whose only
logic is orchestration — dispatching, tracking completion, resolving
futures, and emitting lifecycle events.

Public API
----------
Exactly the surface required by the PROMPT MASTER, plus the two extension
methods the rest of the pipeline needs:

* :meth:`submit` — enqueue a task.
* :meth:`get_task` — retrieve a task by id.
* :meth:`list_tasks` — list tasks, optionally filtered by status.
* :meth:`pause` — stop a running task (best-effort).
* :meth:`resume` — restart a paused task.
* :meth:`cancel` — cancel a pending task.
* :meth:`stop` — graceful shutdown.
* :meth:`download_manga` — **async generator** yielding a
  :class:`~nexusdl.core.models.download.DownloadResult` per chapter.

Scheduling model
----------------
``submit`` and ``_run_task``'s completion both call ``_try_dispatch``, which
drains :meth:`DownloadQueue.next` until it returns ``None`` (queue
saturated). Each dispatched task runs in its own :class:`asyncio.Task`.
This self-scheduling pattern avoids a background polling loop entirely.

Cancellation, pause, resume
---------------------------
``asyncio.CancelledError`` is always propagated untouched at the level of
``_run_task``'s outer scope. When the manager cancels a worker (via
``pause``, ``stop``, or a generator abort), the future associated with the
task is resolved to a synthetic failed result so that ``download_manga``
does not hang. The queue's status is the authority — see
:mod:`nexusdl.core.downloader.queue` for the full state machine.

Config
------
The manager reads:

* ``Settings.downloader.max_concurrent_tasks: int`` — concurrency limit
  (used to construct the queue; defaults to ``DownloadQueue``'s own
  default when absent);
* ``Settings.storage.downloads_dir: Path | str`` — base output directory,
  used only by :meth:`_expected_output_path` for the ``skip_existing`` check;
  the worker computes the actual output path.

Dependency graph (no cycles)::

    core.exceptions  ← core.downloader.manager → core.downloader.queue
                                                  core.downloader.worker
                                                  core.events
                                                  core.models.*
                                                  (parsers, plugins via
                                                   Protocols only)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeAlias

from loguru import logger
from slugify import slugify

from nexusdl.core.downloader.queue import DownloadQueue
from nexusdl.core.events import Event, EventBus
from nexusdl.core.exceptions import (
    InvalidStateTransitionError,
    NexusDLError,
)
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.download import DownloadResult, DownloadTask
from nexusdl.core.models.enums import (
    DownloadStatus,
    PackagingFormat,
    Priority,
)
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.downloader.worker import DownloadWorker


__all__ = [
    "DownloadManager",
    "ParserResolverProtocol",
    "ParserProtocol",
    "PluginDispatcherProtocol",
    "WorkerFactory",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default concurrency when ``Settings.downloader.max_concurrent_tasks`` is
#: absent. Matches :data:`nexusdl.core.downloader.queue.DEFAULT_MAX_CONCURRENT`
#: so a freshly-initialised manager is consistent out of the box.
_DEFAULT_MAX_CONCURRENT_TASKS: Final[int] = 4

#: Extension per packaging format, mirrored from
#: :mod:`nexusdl.core.downloader.worker` **only** for the ``skip_existing``
#: path check. The actual output file is produced by the worker; the manager
#: needs the same convention to know whether a chapter was already downloaded.
#: A test that iterates every ``PackagingFormat`` value ensures the two maps
#: stay in sync.
_FORMAT_EXTENSIONS: Final[dict[PackagingFormat, str]] = {
    PackagingFormat.FOLDER: "",
    PackagingFormat.ZIP: ".zip",
    PackagingFormat.CBZ: ".cbz",
    PackagingFormat.CB7: ".cb7",
    PackagingFormat.PDF: ".pdf",
    PackagingFormat.CBT: ".tar",
}

#: Event types emitted by the manager. The ``download.*`` progress events
#: and terminal events are emitted by the worker; the manager is responsible
#: for the lifecycle events (pause/resume/cancel) and the chapter-level
#: events emitted by :meth:`download_manga`.
_EVENT_DOWNLOAD_PAUSED: Final = "download.paused"
_EVENT_DOWNLOAD_RESUMED: Final = "download.resumed"
_EVENT_DOWNLOAD_CANCELLED: Final = "download.cancelled"
_EVENT_CHAPTER_STARTED: Final = "chapter.started"
_EVENT_CHAPTER_COMPLETED: Final = "chapter.completed"
_EVENT_CHAPTER_FAILED: Final = "chapter.failed"


# --------------------------------------------------------------------------- #
# Structural protocols                                                        #
# --------------------------------------------------------------------------- #


class ParserProtocol(Protocol):
    """Minimal parser surface the manager touches directly.

    The manager uses :meth:`get_chapters` only in :meth:`download_manga` when
    the caller omits the chapter list. The worker uses :meth:`get_pages`
    and :meth:`download_page`; the manager never calls those — it just
    forwards the parser to the ``worker_factory``.
    """

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Return the ordered list of chapters for *manga*."""
        ...

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered list of pages for *chapter*."""
        ...

    async def download_page(self, page: Page, dest: Path) -> None:
        """Download *page* to the file path *dest*."""
        ...


class ParserResolverProtocol(Protocol):
    """Registry surface used by the manager to obtain a parser for a site.

    Satisfied by any object that can produce a ready-to-use parser for a
    given ``site_id`` — typically an adapter around
    :class:`~nexusdl.core.registry.site_registry.SiteRegistry` that binds
    the shared :class:`~nexusdl.core.session.http_session.HttpSession` once
    at construction time. This keeps the manager free of session lifecycle
    concerns.
    """

    def get_parser(self, site_id: str) -> ParserProtocol:
        """Return a parser for *site_id*.

        Raises:
            ParserNotFoundError: If no parser is registered for the site.
        """
        ...


class PluginDispatcherProtocol(Protocol):
    """Plugin-dispatch surface used by the manager.

    The worker handles the two observer-only hooks
    (``on_page_downloaded`` and ``on_error``); the manager handles the two
    transform hooks (:meth:`dispatch_on_before_download` and
    :meth:`dispatch_on_after_download`), because task and result mutation
    belong at the orchestration level, not the execution level.
    """

    async def dispatch_on_before_download(
        self, task: DownloadTask
    ) -> DownloadTask: ...

    async def dispatch_on_after_download(
        self, result: DownloadResult
    ) -> DownloadResult: ...


#: Callable that produces a :class:`DownloadWorker` for a given parser.
#: The composition root closes over packager_factory, settings, event_bus,
#: and the plugin dispatcher; only the parser varies per task.
WorkerFactory: TypeAlias = Callable[[ParserProtocol], "DownloadWorker"]


# --------------------------------------------------------------------------- #
# Manager                                                                     #
# --------------------------------------------------------------------------- #


class DownloadManager:
    """Orchestrate :class:`DownloadTask` execution end to end.

    See the module docstring for the full contract. The manager is
    asyncio-native and must be driven from a single event loop.
    """

    def __init__(
        self,
        *,
        registry: ParserResolverProtocol,
        settings: Settings,
        event_bus: EventBus,
        worker_factory: WorkerFactory,
        plugin_dispatcher: PluginDispatcherProtocol | None = None,
    ) -> None:
        """Initialise the manager.

        Args:
            registry: Resolves a site id to a parser. Typically a thin
                adapter around :class:`SiteRegistry` with the shared HTTP
                session bound in.
            settings: Application settings, used for
                ``downloader.max_concurrent_tasks`` and
                ``storage.downloads_dir``.
            event_bus: Shared event bus. The manager emits ``download.paused``,
                ``download.resumed``, ``download.cancelled``, and
                ``chapter.started`` / ``chapter.completed`` / ``chapter.failed``.
            worker_factory: Callable producing a fresh
                :class:`~nexusdl.core.downloader.worker.DownloadWorker` for a
                given parser. Injected so tests can substitute a stub worker
                without touching the manager.
            plugin_dispatcher: Optional dispatcher for the two transform
                hooks. When ``None``, plugins are bypassed; this is the
                normal case in tests and in contexts where the plugin
                subsystem is not loaded.
        """
        max_concurrent = int(
            getattr(
                settings.downloader,
                "max_concurrent_tasks",
                _DEFAULT_MAX_CONCURRENT_TASKS,
            )
        )
        self._queue = DownloadQueue(max_concurrent=max_concurrent)
        self._registry = registry
        self._settings = settings
        self._event_bus = event_bus
        self._worker_factory = worker_factory
        self._plugin_dispatcher = plugin_dispatcher
        self._log = logger.bind(component="downloader.manager")

        #: Id → asyncio.Task running the worker for that task. Populated by
        #: :meth:`_try_dispatch` and :meth:`resume`; drained by
        #: ``_run_task``'s ``finally`` and by :meth:`stop`.
        self._running_workers: dict[str, asyncio.Task[None]] = {}

        #: Id → future resolved when the worker finishes. Populated by
        #: :meth:`submit` when the caller wants the result (typically
        #: :meth:`download_manga`); absent otherwise.
        self._futures: dict[str, asyncio.Future[DownloadResult]] = {}

        #: Set by :meth:`stop`. Once ``True``, :meth:`submit` raises and
        #: :meth:`_try_dispatch` refuses to start new work.
        self._stopping: bool = False

    # ------------------------------------------------------------------ #
    # Public API — lifecycle                                              #
    # ------------------------------------------------------------------ #

    async def submit(self, task: DownloadTask) -> None:
        """Add a task to the queue and try to dispatch it immediately.

        Dispatch is opportunistic: if the queue is saturated, the task
        stays PENDING until a worker slot frees up. ``submit`` never
        blocks on capacity.

        Args:
            task: The task to enqueue. Must be in
                :attr:`DownloadStatus.PENDING`.

        Raises:
            NexusDLError: If the manager is stopping, or if the task id is
                already registered.
            InvalidStateTransitionError: If ``task.status`` is not PENDING.
        """
        if self._stopping:
            raise NexusDLError("download manager is stopping")
        await self._queue.submit(task)
        self._log.debug(
            "Submitted task {} (pending={})",
            task.id,
            len(await self._queue.list(DownloadStatus.PENDING)),
        )
        await self._try_dispatch()

    async def get_task(self, task_id: str) -> DownloadTask | None:
        """Return the task with id ``task_id``, or ``None``.

        Args:
            task_id: The task's id, as a string.
        """
        return await self._queue.get(task_id)

    async def list_tasks(
        self, status: DownloadStatus | None = None
    ) -> list[DownloadTask]:
        """Return tasks, optionally filtered by status.

        Args:
            status: When provided, only tasks in that exact status are
                returned. When ``None``, every task ever submitted is
                returned, including terminal ones.
        """
        return await self._queue.list(status)

    async def pause(self, task_id: str) -> bool:
        """Stop a running task (best-effort).

        The queue transitions RUNNING → PAUSED and the underlying worker's
        :class:`asyncio.Task` is cancelled. The worker's ``finally`` blocks
        clean up temporary files; the task becomes resumable via
        :meth:`resume`, which spawns a fresh worker.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if
            it does not exist.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                RUNNING.
        """
        task = await self._queue.get(task_id)
        if task is None:
            return False

        # Queue validates the transition before we touch the worker.
        await self._queue.pause(task_id)

        worker_task = self._running_workers.get(task_id)
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()

        await self._emit_event(
            _EVENT_DOWNLOAD_PAUSED,
            {
                "task_id": task_id,
                "chapter_id": task.chapter.id,
            },
        )
        self._log.info("Paused task {}", task_id)
        return True

    async def resume(self, task_id: str) -> bool:
        """Restart a paused task.

        The queue transitions PAUSED → RUNNING and a fresh worker is
        spawned. Because the previous worker was cancelled, the chapter is
        re-downloaded from scratch — there is no partial-page checkpoint in
        the current design.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if
            it does not exist.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                PAUSED.
        """
        task = await self._queue.get(task_id)
        if task is None:
            return False

        await self._queue.resume(task_id)
        if self._stopping:
            # Manager is shutting down; queue status is now RUNNING but no
            # worker will run. Callers should not reach this path.
            return True

        worker_task = asyncio.create_task(
            self._run_task(task),
            name=f"download-task-{task_id}-resumed",
        )
        self._running_workers[task_id] = worker_task

        await self._emit_event(
            _EVENT_DOWNLOAD_RESUMED,
            {
                "task_id": task_id,
                "chapter_id": task.chapter.id,
            },
        )
        self._log.info("Resumed task {}", task_id)
        return True

    async def cancel(self, task_id: str) -> bool:
        """Cancel a PENDING task.

        Only :attr:`DownloadStatus.PENDING` tasks may be cancelled per the
        state machine. To stop a running task, use :meth:`pause`. A paused
        task cannot be cancelled; it must be resumed first, at which point
        it will either finish or fail on its own.

        Args:
            task_id: The task's id, as a string.

        Returns:
            ``True`` if the task was found and transitioned, ``False`` if
            it does not exist.

        Raises:
            InvalidStateTransitionError: If the task exists but is not
                PENDING.
        """
        task = await self._queue.get(task_id)
        if task is None:
            return False

        await self._queue.cancel(task_id)
        await self._emit_event(
            _EVENT_DOWNLOAD_CANCELLED,
            {
                "task_id": task_id,
                "chapter_id": task.chapter.id,
            },
        )
        self._log.info("Cancelled task {}", task_id)
        return True

    async def stop(self) -> None:
        """Stop all in-flight work and shut the manager down.

        Idempotent. After this method returns:

        * no new task may be submitted (:meth:`submit` raises);
        * every RUNNING worker has been cancelled and awaited;
        * every PENDING task has been cancelled;
        * every outstanding future has been resolved to a failed result so
          that :meth:`download_manga` callers do not hang.

        The manager cannot be restarted after :meth:`stop`; a new instance
        must be created.
        """
        if self._stopping:
            return
        self._stopping = True
        self._log.info("Stopping download manager")

        # Cancel pending tasks first so they never dispatch.
        pending = await self._queue.list(DownloadStatus.PENDING)
        for task in pending:
            task_id = str(task.id)
            try:
                await self._queue.cancel(task_id)
            except InvalidStateTransitionError:
                # Already transitioned by a concurrent caller.
                pass
            future = self._futures.pop(task_id, None)
            if future is not None and not future.done():
                future.set_result(
                    self._make_synthetic_result(
                        task,
                        status=DownloadStatus.CANCELLED,
                        error=NexusDLError(
                            "download manager stopped before task started"
                        ),
                    )
                )

        # Cancel running workers.
        running = list(self._running_workers.values())
        for worker_task in running:
            worker_task.cancel()

        # Await their termination (including CancelledError).
        if running:
            await asyncio.gather(*running, return_exceptions=True)

        # Resolve any futures the workers' finally blocks may have missed
        # (e.g. when a worker was in a raw sleep when cancelled).
        for task_id, future in list(self._futures.items()):
            if not future.done():
                task = await self._queue.get(task_id)
                if task is not None:
                    future.set_result(
                        self._make_synthetic_result(
                            task,
                            status=DownloadStatus.FAILED,
                            error=NexusDLError("download manager stopped"),
                        )
                    )
        self._futures.clear()
        self._running_workers.clear()
        self._log.info("Download manager stopped")

    # ------------------------------------------------------------------ #
    # Public API — download_manga (async generator)                      #
    # ------------------------------------------------------------------ #

    async def download_manga(
        self,
        manga: Manga,
        *,
        chapters: Sequence[Chapter] | None = None,
        format: PackagingFormat = PackagingFormat.CBZ,
        skip_existing: bool = True,
        priority: Priority = Priority.NORMAL,
    ) -> AsyncIterator[DownloadResult]:
        """Download every chapter of *manga*, yielding one result per chapter.

        This is an **async generator**: results are produced one at a time,
        in chapter order, so a caller can display progress or stop early
        without coordinating a separate task. The generator is sequential
        by design — it awaits each chapter's completion before submitting
        the next — because the caller controls pacing.

        For parallel chapter downloads (e.g. from a web server), submit
        tasks individually via :meth:`submit` instead.

        Args:
            manga: The manga to download. Its ``site_id`` must resolve to
                a registered parser.
            chapters: The chapters to process, in order. When ``None``,
                chapters are fetched via the manga's parser and iterated
                in the parser's native order.
            format: Packaging format applied to every chapter. Defaults to
                :attr:`PackagingFormat.CBZ`.
            skip_existing: When ``True``, chapters whose expected output
                already exists on disk are skipped (no task is created and
                no result is yielded for them).
            priority: Priority assigned to every submitted task.

        Yields:
            One :class:`DownloadResult` per non-skipped chapter. Failed
            chapters yield a FAILED result rather than raising, so the
            generator can continue with the next chapter unless the
            generator itself is cancelled.
        """
        if chapters is None:
            parser = self._registry.get_parser(manga.site_id)
            chapters = await parser.get_chapters(manga)

        for chapter in chapters:
            if self._stopping:
                self._log.debug(
                    "download_manga stopping before chapter {!r}",
                    chapter.title,
                )
                return
            if skip_existing and self._is_already_downloaded(
                manga, chapter, format
            ):
                self._log.debug(
                    "Skipping already-downloaded chapter: {} / {}",
                    manga.title,
                    chapter.title,
                )
                continue

            task = DownloadTask(
                manga=manga,
                chapter=chapter,
                format=format,
                priority=priority,
            )
            task_id = str(task.id)
            future: asyncio.Future[DownloadResult] = (
                asyncio.get_running_loop().create_future()
            )
            self._futures[task_id] = future

            await self._emit_event(
                _EVENT_CHAPTER_STARTED,
                {
                    "manga_id": manga.id,
                    "chapter_id": chapter.id,
                    "task_id": task_id,
                },
            )

            try:
                await self.submit(task)
            except Exception as exc:
                self._futures.pop(task_id, None)
                self._log.exception(
                    "Could not submit chapter {!r}: {}", chapter.title, exc
                )
                yield self._make_synthetic_result(
                    task, status=DownloadStatus.FAILED, error=exc
                )
                await self._emit_event(
                    _EVENT_CHAPTER_FAILED,
                    {
                        "manga_id": manga.id,
                        "chapter_id": chapter.id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
                continue

            try:
                result = await future
            except asyncio.CancelledError:
                # Generator cancelled (e.g. caller broke out of async for).
                # Cancel the worker if it is still running, then propagate.
                worker_task = self._running_workers.get(task_id)
                if worker_task is not None and not worker_task.done():
                    worker_task.cancel()
                raise
            finally:
                self._futures.pop(task_id, None)

            if result.status is DownloadStatus.DONE:
                await self._emit_event(
                    _EVENT_CHAPTER_COMPLETED,
                    {
                        "manga_id": manga.id,
                        "chapter_id": chapter.id,
                        "task_id": task_id,
                        "output_path": (
                            str(result.output_path)
                            if result.output_path is not None
                            else None
                        ),
                    },
                )
            else:
                await self._emit_event(
                    _EVENT_CHAPTER_FAILED,
                    {
                        "manga_id": manga.id,
                        "chapter_id": chapter.id,
                        "task_id": task_id,
                        "error_type": (
                            type(result.error).__name__
                            if result.error is not None
                            else None
                        ),
                        "error_message": (
                            str(result.error)
                            if result.error is not None
                            else None
                        ),
                    },
                )
            yield result

    # ------------------------------------------------------------------ #
    # Internals — dispatch                                               #
    # ------------------------------------------------------------------ #

    async def _try_dispatch(self) -> None:
        """Drain the queue until saturated, spawning one task per task.

        Called from :meth:`submit`, :meth:`resume`, and each worker's
        ``finally`` block. The queue's own ``next`` gate serialises pops,
        so concurrent ``_try_dispatch`` calls cannot over-dispatch.
        """
        if self._stopping:
            return
        while True:
            task = await self._queue.next()
            if task is None:
                return
            task_id = str(task.id)
            worker_task = asyncio.create_task(
                self._run_task(task),
                name=f"download-task-{task_id}",
            )
            self._running_workers[task_id] = worker_task
            self._log.debug("Dispatched task {}", task_id)

    async def _run_task(self, task: DownloadTask) -> None:
        """Execute one task through the worker, plugins, and the queue.

        Lifecycle:

        1. Dispatch ``on_before_download`` (plugin may replace the task).
        2. Resolve the parser for the (possibly replaced) task.
        3. Build and run the worker.
        4. Dispatch ``on_after_download`` (plugin may replace the result).
        5. Transition the queue status to DONE or FAILED.
        6. Resolve the task's future (if any).
        7. Trigger the next dispatch.

        Cancellation is propagated so the ``asyncio.Task`` is marked
        cancelled, but the associated future is resolved to a synthetic
        failed result so ``download_manga`` does not hang.

        Args:
            task: The task popped from the queue, already in RUNNING.
        """
        task_id = str(task.id)
        result: DownloadResult | None = None
        try:
            task = await self._dispatch_on_before_download(task)
            task_id = str(task.id)

            parser = self._registry.get_parser(task.manga.site_id)
            worker = self._worker_factory(parser)
            result = await worker.run(task)
            result = await self._dispatch_on_after_download(result)

            if result.status is DownloadStatus.DONE:
                await self._queue.mark_done(task_id)
            else:
                await self._queue.mark_failed(task_id)

        except asyncio.CancelledError:
            # Manager-initiated cancellation (pause / stop / generator
            # abort). The queue's status has already been handled by the
            # caller. Resolve the future so `download_manga` does not hang.
            if result is None:
                result = self._make_synthetic_result(
                    task,
                    status=DownloadStatus.FAILED,
                    error=NexusDLError("download task was cancelled"),
                )
            raise
        except Exception as exc:
            self._log.exception(
                "Unexpected error running task {}: {}", task_id, exc
            )
            if result is None:
                result = self._make_synthetic_result(
                    task, status=DownloadStatus.FAILED, error=exc
                )
            try:
                await self._queue.mark_failed(task_id)
            except InvalidStateTransitionError:
                # A concurrent pause/cancel already moved the task out of
                # RUNNING; the queue is the authority and we respect it.
                self._log.debug(
                    "Task {} no longer RUNNING; skipping mark_failed", task_id
                )
        finally:
            self._running_workers.pop(task_id, None)
            future = self._futures.pop(task_id, None)
            if future is not None and not future.done():
                if result is not None:
                    future.set_result(result)
                else:
                    future.cancel()
            if not self._stopping:
                try:
                    await self._try_dispatch()
                except Exception:
                    self._log.exception("Error during follow-up dispatch")

    # ------------------------------------------------------------------ #
    # Internals — plugin dispatch                                        #
    # ------------------------------------------------------------------ #

    async def _dispatch_on_before_download(
        self, task: DownloadTask
    ) -> DownloadTask:
        """Delegate ``on_before_download`` to the plugin dispatcher."""
        if self._plugin_dispatcher is None:
            return task
        try:
            return await self._plugin_dispatcher.dispatch_on_before_download(
                task
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "Plugin dispatch_on_before_download failed for task {}: {}",
                task.id,
                exc,
            )
            return task

    async def _dispatch_on_after_download(
        self, result: DownloadResult
    ) -> DownloadResult:
        """Delegate ``on_after_download`` to the plugin dispatcher."""
        if self._plugin_dispatcher is None:
            return result
        try:
            return await self._plugin_dispatcher.dispatch_on_after_download(
                result
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "Plugin dispatch_on_after_download failed for task {}: {}",
                result.task_id,
                exc,
            )
            return result

    # ------------------------------------------------------------------ #
    # Internals — synthetic results and filesystem                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_synthetic_result(
        task: DownloadTask,
        *,
        status: DownloadStatus,
        error: Exception,
    ) -> DownloadResult:
        """Build a terminal :class:`DownloadResult` without running a worker.

        Used when the manager must resolve a task's future without a
        worker having produced a result: manager shutdown, submission
        failure, or worker cancellation. The synthetic result carries
        ``pages_downloaded=0`` and ``bytes_downloaded=0`` because no data
        was actually written.

        Args:
            task: The task the result belongs to.
            status: The terminal status to record.
            error: The error to attach.
        """
        return DownloadResult(
            task_id=task.id,
            status=status,
            output_path=None,
            pages_downloaded=0,
            bytes_downloaded=0,
            duration_seconds=0.0,
            error=error,
            completed_at=datetime.now(UTC),
        )

    def _is_already_downloaded(
        self,
        manga: Manga,
        chapter: Chapter,
        format: PackagingFormat,
    ) -> bool:
        """Return ``True`` iff the expected output for this chapter exists.

        Best-effort check based on the same path convention as
        :meth:`nexusdl.core.downloader.worker.DownloadWorker._compute_output_path`.
        The worker remains the authority for the actual path; the manager
        only needs a boolean for ``skip_existing``.

        Args:
            manga: The manga being processed.
            chapter: The chapter being checked.
            format: The packaging format.
        """
        return self._expected_output_path(manga, chapter, format).exists()

    def _expected_output_path(
        self,
        manga: Manga,
        chapter: Chapter,
        format: PackagingFormat,
    ) -> Path:
        """Compute the expected output path for a chapter.

        Mirrors the worker's convention exactly. If the worker changes its
        naming scheme, this method must change in lock-step — the two are
        cross-checked by a unit test that iterates every
        :class:`PackagingFormat` value.

        Args:
            manga: The manga being processed.
            chapter: The chapter being checked.
            format: The packaging format.
        """
        downloads_dir = Path(self._settings.storage.downloads_dir)
        manga_dir = downloads_dir / slugify(manga.title)
        chapter_slug = slugify(chapter.title)
        extension = _FORMAT_EXTENSIONS[format]
        return manga_dir / f"{chapter_slug}{extension}"

    # ------------------------------------------------------------------ #
    # Internals — event emission                                         #
    # ------------------------------------------------------------------ #

    async def _emit_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Publish an event, swallowing and logging failures.

        Publishing failures must never abort a download. Cancellation is
        re-raised.

        Args:
            event_type: Fully qualified event type.
            payload: JSON-serialisable mapping.
        """
        try:
            await self._event_bus.publish(
                Event(
                    type=event_type,
                    timestamp=datetime.now(UTC),
                    payload=payload,
                    id=__import__("uuid").uuid4(),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "Could not publish event {}: {}", event_type, exc
            )
