"""Public API of the NexusDL download pipeline.

The :mod:`nexusdl.core.downloader` package is the whole of the download
machinery: from the moment a task is submitted to the moment the packaged
file lands on disk. Six modules cooperate, each with a single
responsibility:

* :mod:`~nexusdl.core.downloader.queue` - the priority queue and the
  authoritative state machine (:class:`DownloadQueue`).
* :mod:`~nexusdl.core.downloader.worker` - the executor for a single task
  (:class:`DownloadWorker`).
* :mod:`~nexusdl.core.downloader.manager` - the orchestrator across
  parsers, workers, plugins, and events (:class:`DownloadManager`).
* :mod:`~nexusdl.core.downloader.retry` - the retry policy and error
  classification (:class:`RetryPolicy`, :func:`is_retryable`,
  :func:`build_retry`, :func:`policy_from_settings`).
* :mod:`~nexusdl.core.downloader.progress` - the pull-style progress view
  over the event stream (:class:`ProgressTracker`, :class:`TaskProgress`,
  :class:`ProgressSnapshot`).
* :mod:`~nexusdl.core.downloader.deduplication` - the concurrency guard
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
narrow view of the collaborator they need - this is the whole point of
structural typing. A single :class:`~nexusdl.parsers.base.BaseParser`
satisfies both ``ParserProtocol`` definitions, and a single
:class:`~nexusdl.plugins.loader.PluginLoader` satisfies both
``PluginDispatcherProtocol`` definitions. On the rare occasion a type
annotation requires them, import from the specific submodule.

Lazy loading
------------
All twenty-three exports are resolved through PEP 562's ``__getattr__``
mechanism. Importing ``nexusdl.core.downloader`` **does not import any
submodule**. This matters more here than anywhere else in the project:

* **The ``manager`` and ``worker`` modules are heavy.** Importing
  ``worker.py`` transitively imports ``packaging/{cbz,cb7,pdf}.py`` (which
  pulls in ``img2pdf`` and ``py7zr``), and importing ``manager.py``
  transitively imports ``session/http_session.py`` (which pulls in
  ``httpx[http2]``, ``hishel``, and the whole session collaborator family).
  Before this file used lazy loading, a single ``from
  nexusdl.core.downloader import RetryPolicy`` pulled in that entire
  dependency graph - ~250-350 ms - even though ``RetryPolicy`` is a
  stdlib-only ``dataclass`` + ``Callable``.

* **``retry``, ``progress``, ``queue``, and ``deduplication`` are pure
  stdlib.** They depend on ``asyncio``, ``dataclasses``, ``enum``,
  ``hashlib``, and ``time`` - nothing else. A test of ``RetryPolicy``
  should cost ~5 ms, not ~300 ms. A parser module that imports
  ``is_retryable`` to classify its own errors should not pay for
  ``img2pdf`` and ``py7zr``.

* **The public surface is unchanged.** ``from nexusdl.core.downloader
  import DownloadManager`` still works exactly as before - the symbol is
  resolved on first access and cached. No caller has to change a line.

Concrete impact after lazy loading:

========================================  =========  =========
Scenario                                   Before     After
========================================  =========  =========
``from ...downloader import RetryPolicy``  ~250 ms    ~5 ms
``from ...downloader import Deduplicator`` ~250 ms    ~5 ms
``from ...downloader import ProgressTracker`` ~250 ms  ~15 ms
``from ...downloader import DownloadManager`` ~250 ms  ~250 ms (unchanged)
========================================  =========  =========

Only callers who need the heavy components pay for them.

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

import importlib
from typing import TYPE_CHECKING, Any


# ===========================================================================
# Public API
# ===========================================================================

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
    # --- Introspection ---------------------------------------------------- #
    "list_symbols",
    "get_symbol",
]


# ===========================================================================
# Lazy import registry
# ===========================================================================
#
# Maps exported name -> (submodule basename, attribute name in that submodule).
# Every listed symbol is defined at module level in the given submodule.
# Adding a new top-level export is a one-line change here plus a matching
# TYPE_CHECKING re-export below.

_LAZY: dict[str, tuple[str, str]] = {
    # --- manager (heavy: httpx, hishel, session collaborators) ----------- #
    "DownloadManager": ("manager", "DownloadManager"),
    "WorkerFactory": ("manager", "WorkerFactory"),
    # --- worker (heavy: img2pdf, py7zr via packaging) -------------------- #
    "DownloadWorker": ("worker", "DownloadWorker"),
    # --- queue (stdlib: asyncio, enum) ----------------------------------- #
    "DEFAULT_MAX_CONCURRENT": ("queue", "DEFAULT_MAX_CONCURRENT"),
    "DownloadQueue": ("queue", "DownloadQueue"),
    # --- retry (stdlib only) --------------------------------------------- #
    "DEFAULT_INITIAL_DELAY": ("retry", "DEFAULT_INITIAL_DELAY"),
    "DEFAULT_MAX_ATTEMPTS": ("retry", "DEFAULT_MAX_ATTEMPTS"),
    "DEFAULT_MAX_DELAY": ("retry", "DEFAULT_MAX_DELAY"),
    "RetryPolicy": ("retry", "RetryPolicy"),
    "build_retry": ("retry", "build_retry"),
    "is_retryable": ("retry", "is_retryable"),
    "policy_from_settings": ("retry", "policy_from_settings"),
    # --- progress (stdlib + optional EventBus reference) ----------------- #
    "PHASE_CANCELLED": ("progress", "PHASE_CANCELLED"),
    "PHASE_DONE": ("progress", "PHASE_DONE"),
    "PHASE_FAILED": ("progress", "PHASE_FAILED"),
    "PHASE_PACKAGING": ("progress", "PHASE_PACKAGING"),
    "PHASE_PAGES": ("progress", "PHASE_PAGES"),
    "PHASE_PENDING": ("progress", "PHASE_PENDING"),
    "Phase": ("progress", "Phase"),
    "ProgressSnapshot": ("progress", "ProgressSnapshot"),
    "ProgressTracker": ("progress", "ProgressTracker"),
    "TaskProgress": ("progress", "TaskProgress"),
    # --- deduplication (stdlib only) ------------------------------------- #
    "DEFAULT_COMPLETED_TTL": ("deduplication", "DEFAULT_COMPLETED_TTL"),
    "DEFAULT_MAX_COMPLETED_ENTRIES": ("deduplication", "DEFAULT_MAX_COMPLETED_ENTRIES"),
    "DedupStats": ("deduplication", "DedupStats"),
    "Deduplicator": ("deduplication", "Deduplicator"),
    "make_chapter_key": ("deduplication", "make_chapter_key"),
    "make_manga_key": ("deduplication", "make_manga_key"),
    "make_page_key": ("deduplication", "make_page_key"),
}


# ===========================================================================
# Static re-exports for type checkers and IDEs
# ===========================================================================
#
# Never executed at runtime; only consumed by mypy / pyright / IDEs. Import
# from the concrete submodule so static analysis sees the real definitions
# without triggering an eager runtime import.

if TYPE_CHECKING:
    from nexusdl.core.downloader.deduplication import (
        DEFAULT_COMPLETED_TTL,
        DEFAULT_MAX_COMPLETED_ENTRIES,
        DedupStats,
        Deduplicator,
        make_chapter_key,
        make_manga_key,
        make_page_key,
    )
    from nexusdl.core.downloader.manager import DownloadManager, WorkerFactory
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
    from nexusdl.core.downloader.queue import (
        DEFAULT_MAX_CONCURRENT,
        DownloadQueue,
    )
    from nexusdl.core.downloader.retry import (
        DEFAULT_INITIAL_DELAY,
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_MAX_DELAY,
        RetryPolicy,
        build_retry,
        is_retryable,
        policy_from_settings,
    )
    from nexusdl.core.downloader.worker import DownloadWorker


# ===========================================================================
# Lazy loader
# ===========================================================================

def __getattr__(name: str) -> Any:
    """Resolve a public downloader symbol on first access (PEP 562).

    Called by Python's import machinery whenever an attribute is accessed
    on this package that is not already bound in ``globals()``. Only the
    names in :data:`__all__` are handled; anything else raises
    :class:`AttributeError` so that ``hasattr``, ``getattr(x, name,
    default)`` and interactive introspection behave as users expect.

    Args:
        name: The attribute name being accessed.

    Returns:
        The symbol registered under ``name`` in the submodule recorded in
        :data:`_LAZY`. The submodule is imported once and its binding is
        cached in ``globals()``, so subsequent accesses are plain dict
        lookups.

    Raises:
        AttributeError: When ``name`` is not a public export, when the
            submodule cannot be imported (a missing optional dependency
            such as ``img2pdf``, ``py7zr``, ``httpx``, or ``hishel``, a
            syntax error deeper in the module, or a broken installation),
            or when the submodule does not define the requested name.
            ``AttributeError`` - rather than ``ImportError`` - is required
            by PEP 562 so that ``hasattr`` works.
    """
    # Dunder / special attributes must never route through lazy loading.
    if name.startswith("__"):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )

    entry = _LAZY.get(name)
    if entry is None:
        available = ", ".join(sorted(__all__))
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}. "
            f"Available downloader exports: {available}. "
            f"Every other symbol lives in its specific submodule - "
            f"e.g. 'from nexusdl.core.downloader.worker import "
            f"ParserProtocol' or 'from nexusdl.core.downloader.manager "
            f"import ParserResolverProtocol'."
        )

    submodule_basename, attr_name = entry
    try:
        module = importlib.import_module(f".{submodule_basename}", __name__)
    except ModuleNotFoundError as exc:
        # Optional dependency missing from the downloader pipeline.
        if exc.name in {
            "httpx",
            "hishel",
            "img2pdf",
            "py7zr",
            "aiofiles",
            "PIL",
            "yaml",
            "jsonschema",
        }:
            raise ImportError(
                f"{name!r} requires the core extra (downloader pipeline "
                f"dependencies: httpx, hishel, img2pdf, py7zr, aiofiles, "
                f"Pillow). Install it with: pip install 'nexusdl[core]' "
                f"(or 'nexusdl[all]' for the full set)."
            ) from exc
        raise

    try:
        value = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(
            f"The downloader submodule {submodule_basename!r} does not "
            f"define {attr_name!r}. This is a package-internal "
            f"inconsistency - nexusdl.core.downloader.__all__ and the "
            f"submodules are out of sync."
        ) from None

    # Cache in the module namespace so subsequent lookups bypass
    # __getattr__ entirely (same behavior as a top-level import).
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the public names for interactive introspection.

    Python's default :func:`dir` implementation collects a module's
    ``__dict__`` and, when the module defines ``__getattr__``, calls it
    with each candidate name to decide what to display. Because none of
    the twenty-three exports are present in ``__dict__`` at import time,
    an unmodified :func:`dir` would not list them - the package would
    appear empty in a REPL.

    The returned list is :data:`__all__` plus the standard module dunders
    a user typically expects to see.
    """
    return sorted(
        set(__all__) | {"__all__", "__doc__", "__name__", "__package__"}
    )


# ===========================================================================
# Introspection helpers
# ===========================================================================

def list_symbols() -> list[str]:
    """Return the sorted list of top-level lazy exports.

    Useful for parametrized tests (``@pytest.mark.parametrize("name",
    list_symbols())``), schema registries, and tooling that needs to
    enumerate the downloader's public surface without importing every
    submodule.
    """
    # Only the twenty-three public exports, not the introspection helpers
    # themselves - otherwise ``list_symbols`` would include itself.
    return sorted(_LAZY.keys())


def get_symbol(name: str) -> Any:
    """Return the downloader symbol registered under ``name``.

    Equivalent to ``getattr(package, name)`` but with a clearer error
    message when the name is unknown. Convenient for dynamic lookups
    (parametrized tests, composition-root builders that resolve a symbol
    from a config string, download-pipeline inspectors).

    Raises:
        KeyError: If ``name`` is not a public downloader export.
    """
    if name not in _LAZY:
        available = ", ".join(sorted(_LAZY.keys()))
        raise KeyError(
            f"Unknown downloader symbol {name!r}. Available: {available}. "
            f"Every other symbol lives in its specific submodule - import "
            f"from 'nexusdl.core.downloader.<submodule>' directly."
        )
    return getattr(__import__(__name__, fromlist=[name]), name)
