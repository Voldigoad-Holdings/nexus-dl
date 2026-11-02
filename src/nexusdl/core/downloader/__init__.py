"""Public API of the NexusDL download pipeline.

The :mod:`nexusdl.core.downloader` package is the whole of the download
machinery: from the moment a task is submitted to the moment the packaged
file lands on disk. Six modules cooperate, each with a single
responsibility:

* :mod:`~nexusdl.core.downloader.queue` — the priority queue and the
  authoritative state machine (:class:`DownloadQueue`).
* :mod:`~nexusdl.core.downloader.worker` — the executor for a single task
  (:class:`DownloadWorker`).
* :mod:`~nexusdl.core.downloader.manager` — the orchestrator across
  parsers, workers, plugins, and events (:class:`DownloadManager`).
* :mod:`~nexusdl.core.downloader.retry` — the retry policy and error
  classification (:class:`RetryPolicy`, :func:`is_retryable`,
  :func:`build_retry`, :func:`policy_from_settings`).
* :mod:`~nexusdl.core.downloader.progress` — the pull-style progress view
  over the event stream (:class:`ProgressTracker`, :class:`TaskProgress`,
  :class:`ProgressSnapshot`).
* :mod:`~nexusdl.core.downloader.deduplication` — the concurrency guard
  for duplicate submissions (:class:`Deduplicator`).

Composition
-----------
The composition root (CLI / TUI / GUI / Web) wires the pieces together::

    from nexusdl.core.downloader import (
        DownloadManager,
        DownloadWorker,
        WorkerFactory,
    )
    from nexusdl.plugins import PluginLoader

    plugin_loader = PluginLoader(settings=settings, event_bus=bus)
    await plugin_loader.load_all()

    def worker_factory(parser) -> DownloadWorker:
        return DownloadWorker(
            parser=parser,
            packager_factory=packager_factory,
            settings=settings,
            event_bus=bus,
            plugin_dispatcher=plugin_loader,
        )

    manager = DownloadManager(
        registry=registry,       # satisfies ParserResolverProtocol
        settings=settings,
        event_bus=bus,
        worker_factory=worker_factory,
        plugin_dispatcher=plugin_loader,
    )

    async for result in manager.download_manga(manga):
        print(result.status, result.output_path)

The structural protocols (``ParserProtocol``, ``ParserResolverProtocol``,
``PluginDispatcherProtocol``) are **internal to their modules** and are not
re-exported here. ``worker.py`` and ``manager.py`` each define their own
narrow view of the collaborator they need — this is the whole point of
structural typing. A single :class:`~nexusdl.parsers.base.BaseParser`
satisfies both ``ParserProtocol`` definitions, and a single
:class:`~nexusdl.plugins.loader.PluginLoader` satisfies both
``PluginDispatcherProtocol`` definitions. On the rare occasion a type
annotation requires them, import from the specific submodule.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported because they are private to their
module and are allowed to change without notice:

* ``_FORMAT_EXTENSIONS`` (in ``worker.py`` and ``manager.py``)
* ``_VALID_TRANSITIONS`` (in ``queue.py``)
* ``_TERMINAL_PHASES`` (in ``progress.py``)
* ``_EVENT_*`` constants (in ``worker.py`` and ``manager.py``)
* ``ParserProtocol``, ``ParserResolverProtocol``,
  ``PluginDispatcherProtocol`` (in ``worker.py`` and ``manager.py``)
"""

from __future__ import annotations

# --- Manager (composition root entry point) --------------------------------- #
from nexusdl.core.downloader.manager import DownloadManager, WorkerFactory

# --- Worker (single-task executor) ------------------------------------------ #
from nexusdl.core.downloader.worker import DownloadWorker

# --- Queue (priority + state machine) --------------------------------------- #
from nexusdl.core.downloader.queue import (
    DEFAULT_MAX_CONCURRENT,
    DownloadQueue,
)

# --- Retry (policy + classification) ---------------------------------------- #
from nexusdl.core.downloader.retry import (
    DEFAULT_INITIAL_DELAY,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELAY,
    RetryPolicy,
    build_retry,
    is_retryable,
    policy_from_settings,
)

# --- Progress (pull-style view over events) --------------------------------- #
from nexusdl.core.downloader.progress import (
    PHASE_CANCELLED,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_PACKAGING,
    PHASE_PAGES,
    PHASE_PENDING,
    Phase,
    ProgressSnapshot,
    ProgressTracker,
    TaskProgress,
)

# --- Deduplication (concurrency guard) -------------------------------------- #
from nexusdl.core.downloader.deduplication import (
    DEFAULT_COMPLETED_TTL,
    DEFAULT_MAX_COMPLETED_ENTRIES,
    DedupStats,
    Deduplicator,
    make_chapter_key,
    make_manga_key,
    make_page_key,
)

__all__ = [
    # --- Manager ---------------------------------------------------------- #
    "DownloadManager",
    "WorkerFactory",
    # --- Worker ----------------------------------------------------------- #
    "DownloadWorker",
    # --- Queue ------------------------------------------------------------ #
    "DEFAULT_MAX_CONCURRENT",
    "DownloadQueue",
    # --- Retry ------------------------------------------------------------ #
    "DEFAULT_INITIAL_DELAY",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY",
    "RetryPolicy",
    "build_retry",
    "is_retryable",
    "policy_from_settings",
    # --- Progress --------------------------------------------------------- #
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
    # --- Deduplication ---------------------------------------------------- #
    "DEFAULT_COMPLETED_TTL",
    "DEFAULT_MAX_COMPLETED_ENTRIES",
    "DedupStats",
    "Deduplicator",
    "make_chapter_key",
    "make_manga_key",
    "make_page_key",
]
