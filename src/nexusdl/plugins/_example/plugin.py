"""Reference NexusDL plugin — demonstrates every hook of the ``NexusPlugin``
protocol as a strictly-transparent passthrough.

This module is deliberately pedagogical:

* Every hook is exercised, so a plugin author can read one file and see the
  complete lifecycle.
* Nothing in the download pipeline is mutated — each hook returns the object
  it received. A reference example must never surprise.
* No exception escapes a hook: the loader would isolate it anyway, but doing
  it here too demonstrates the "one bad plugin never breaks the pipeline"
  contract in practice.
* The outside world is touched only through sanctioned channels: the plugin
  ``logger``, the shared ``event_bus``, and a JSON stats file under
  ``context.data_dir`` (the only writable path a plugin is given).

Layout of this example plugin (already present in the repo)::

    _example/
    ├── __init__.py           # re-exports ``ExamplePlugin``
    ├── nexus.plugin.yaml     # manifest — see docs/development/plugins.md
    └── plugin.py             # ← this file

To activate the example, copy the whole ``_example/`` directory into your
user plugin directory (see ``docs/development/plugins.md``) and rename the
directory so it no longer starts with ``_`` (the loader skips reserved
names). The manifest must live side-by-side with ``plugin.py``.

The class below **structurally** satisfies :class:`nexusdl.plugins.api.NexusPlugin`:

* mandatory members: ``name``, ``version``, ``on_load``, ``on_unload``;
* optional hooks: the four async pipeline hooks — implemented here for
  completeness and declared in the manifest's ``hooks:`` list, so the loader
  dispatches to them.

Static conformance is checked by ``mypy --strict`` via the
``if TYPE_CHECKING:`` assertion at the bottom of this file.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final
from uuid import uuid4

from nexusdl.core.events import Event
from nexusdl.plugins.api import PluginContext
from nexusdl.plugins.hooks import (
    HOOK_ON_AFTER_DOWNLOAD,
    HOOK_ON_BEFORE_DOWNLOAD,
    HOOK_ON_ERROR,
    HOOK_ON_PAGE_DOWNLOADED,
)

if TYPE_CHECKING:
    from nexusdl.core.models.download import DownloadResult, DownloadTask
    from nexusdl.core.models.page import Page
    from nexusdl.plugins.api import NexusPlugin


# --------------------------------------------------------------------------- #
# Module constants                                                            #
# --------------------------------------------------------------------------- #

#: Custom event types namespaced under ``plugin.example.*`` to avoid
#: colliding with framework events (``download.*``, ``chapter.*``, …).
#: See ``nexusdl.core.events`` for the list of standard event types.
_EVENT_DOWNLOAD_COMPLETED: Final[str] = "plugin.example.download_completed"
_EVENT_DOWNLOAD_FAILED: Final[str] = "plugin.example.download_failed"

#: Filename under ``context.data_dir`` used to persist cross-run statistics.
_STATS_FILENAME: Final[str] = "example_plugin_stats.json"

#: Upper bound on the size of the stats file we are willing to read back.
#: Protects against a corrupted or hostile file whose ``read_text()`` would
#: otherwise be unbounded.
_MAX_STATS_FILE_BYTES: Final[int] = 64 * 1024  # 64 KiB


# --------------------------------------------------------------------------- #
# Example plugin                                                              #
# --------------------------------------------------------------------------- #


class ExamplePlugin:
    """A fully-implemented, harmless reference plugin.

    It counts lifecycle events, persists a small stats file in
    ``context.data_dir``, and republishes two custom events on the shared
    :class:`~nexusdl.core.events.EventBus`. It is safe to enable in any
    environment and is a good starting point to copy for your own plugins.

    Threading model: the loader guarantees no two hooks of the *same* plugin
    instance run concurrently, but the ``asyncio.Lock`` is still taken
    defensively around the mutable ``_stats`` dict — it costs nothing and
    makes the class correct even if that guarantee is ever relaxed.
    """

    __slots__ = ("_ctx", "_lock", "_stats", "_stats_path")

    #: Plugin identifier — must match ``name:`` in ``nexus.plugin.yaml``.
    #: Declared ``ClassVar`` for the same reason as in
    #: :class:`~nexusdl.plugins.api.NexusPlugin`: it is a class-level
    #: constant, not instance state, and ``mypy --strict`` uses ``ClassVar``
    #: to enforce that no plugin ever mutates its own identity at runtime.
    name: ClassVar[str] = "example"

    #: Plugin version — must match ``version:`` in ``nexus.plugin.yaml``.
    version: ClassVar[str] = "1.0.0"

    def __init__(self) -> None:
        # Populated by ``on_load``. ``None`` before load / after unload, so
        # every hook below treats it as a guard rather than assuming it exists.
        self._ctx: PluginContext | None = None
        self._stats_path: Path | None = None
        # Guards the in-memory dict against concurrent hooks.
        self._lock = asyncio.Lock()
        self._stats: dict[str, int] = {
            "tasks_started": 0,
            "tasks_completed": 0,
            "pages_downloaded": 0,
            "errors_seen": 0,
        }

    # ------------------------------------------------------------------ #
    # Lifecycle hooks (synchronous, per the NexusPlugin protocol)         #
    # ------------------------------------------------------------------ #

    def on_load(self, context: PluginContext) -> None:
        """Receive the plugin context and restore any persisted state.

        Args:
            context: Framework-provided services (see
                :class:`~nexusdl.plugins.api.PluginContext`). Store the fields
                the plugin needs on ``self``; do not mutate the context.
        """
        self._ctx = context
        self._stats_path = context.data_dir / _STATS_FILENAME
        self._restore_stats(context)
        context.logger.info(
            "ExamplePlugin v{} loaded (data_dir={}, plugin_dir={})",
            self.version,
            context.data_dir,
            context.plugin_dir,
        )

    def on_unload(self) -> None:
        """Persist stats and release the context.

        Synchronous by contract. The stats file is tiny and bounded
        (see :data:`_MAX_STATS_FILE_BYTES`), so the write is safe to do
        inline even when ``on_unload`` is invoked from an async shutdown
        path.
        """
        if self._ctx is None or self._stats_path is None:
            return

        ctx = self._ctx
        stats_path = self._stats_path
        try:
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            stats_path.write_text(
                json.dumps(self._stats, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            ctx.logger.info(
                "ExamplePlugin unloaded. Persisted stats: {}", self._stats
            )
        except (OSError, TypeError, ValueError) as exc:
            # ``OSError`` covers write failures, ``TypeError`` / ``ValueError``
            # cover a corrupted ``_stats`` payload (defensive: the dict is
            # built from primitives only). Never raise from a lifecycle hook:
            # the framework is shutting down.
            ctx.logger.warning(
                "Could not persist plugin stats on unload: {}", exc
            )
        finally:
            self._ctx = None
            self._stats_path = None

    # ------------------------------------------------------------------ #
    # Download pipeline hooks (asynchronous)                              #
    # ------------------------------------------------------------------ #

    async def on_before_download(self, task: DownloadTask) -> DownloadTask:
        """Inspect a task about to run and return it unchanged.

        Args:
            task: The download task scheduled by the
                :class:`~nexusdl.core.downloader.manager.DownloadManager`.

        Returns:
            The same task, unmodified. Returning a *modified* task is allowed
            (e.g. to bump its priority or patch metadata), but this example
            deliberately stays transparent.
        """
        ctx = self._ctx
        if ctx is None:
            return task

        async with self._lock:
            self._stats["tasks_started"] += 1

        ctx.logger.debug(
            HOOK_ON_BEFORE_DOWNLOAD + ": task_id={} status={}",
            task.id,
            task.status,
        )
        return task

    async def on_after_download(
        self, result: DownloadResult
    ) -> DownloadResult:
        """Observe a completed download and republish a custom event.

        Args:
            result: The outcome produced by the
                :class:`~nexusdl.core.downloader.worker.DownloadWorker`.

        Returns:
            The same result, unmodified.
        """
        ctx = self._ctx
        if ctx is None:
            return result

        async with self._lock:
            self._stats["tasks_completed"] += 1
            snapshot = dict(self._stats)

        ctx.logger.info(
            HOOK_ON_AFTER_DOWNLOAD + ": result={!r}", result
        )
        await self._safe_publish(
            _EVENT_DOWNLOAD_COMPLETED,
            {"plugin": self.name, "stats": snapshot},
        )
        return result

    async def on_page_downloaded(self, page: Page, path: Path) -> None:
        """Observe a single page that was written to disk.

        Args:
            page: The page metadata (``index`` is 0-based).
            path: Where the image was written.
        """
        ctx = self._ctx
        if ctx is None:
            return

        async with self._lock:
            self._stats["pages_downloaded"] += 1

        size = _safe_size(path)
        ctx.logger.trace(
            HOOK_ON_PAGE_DOWNLOADED + ": index={} size={}",
            page.index,
            size if size >= 0 else "<unavailable>",
        )

    async def on_error(self, task_id: str, error: Exception) -> None:
        """Observe an error raised while running a download task.

        Args:
            task_id: Identifier of the task that failed.
            error: The exception raised by the worker. Cancellations are
                ``BaseException`` subclasses and never reach this hook, so
                everything received here is a genuine failure.
        """
        ctx = self._ctx
        if ctx is None:
            return

        async with self._lock:
            self._stats["errors_seen"] += 1

        ctx.logger.error(
            HOOK_ON_ERROR + ": task_id={} error={}: {}",
            task_id,
            type(error).__name__,
            error,
        )
        await self._safe_publish(
            _EVENT_DOWNLOAD_FAILED,
            {
                "plugin": self.name,
                "task_id": task_id,
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _restore_stats(self, context: PluginContext) -> None:
        """Load persisted counters from disk into ``self._stats``.

        Silently starts fresh on any failure. The read is capped at
        :data:`_MAX_STATS_FILE_BYTES` so a corrupted file cannot hang
        ``on_load``.

        Args:
            context: Plugin context (used for the logger).
        """
        stats_path = self._stats_path
        if stats_path is None:
            # ``on_load`` always sets ``self._stats_path`` before calling
            # this method; the explicit check replaces a previous ``assert``
            # so that running under ``python -O`` does not turn this into a
            # silent ``AttributeError`` on the next line.
            return

        if not stats_path.is_file():
            return

        try:
            with stats_path.open("r", encoding="utf-8") as fh:
                raw = fh.read(_MAX_STATS_FILE_BYTES + 1)
        except OSError as exc:
            context.logger.warning(
                "Could not read plugin stats ({}); starting fresh.", exc
            )
            return

        if len(raw) > _MAX_STATS_FILE_BYTES:
            context.logger.warning(
                "Plugin stats file exceeds {} bytes; ignoring and starting fresh.",
                _MAX_STATS_FILE_BYTES,
            )
            return

        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            context.logger.warning(
                "Could not parse plugin stats ({}); starting fresh.", exc
            )
            return

        if not isinstance(parsed, dict):
            context.logger.warning(
                "Plugin stats file is not a JSON object; starting fresh."
            )
            return

        for key in self._stats:
            value = parsed.get(key)
            # ``type(x) is int`` rejects ``bool`` (which is a subclass of
            # ``int``) and any JSON floating-point, keeping the counters
            # strictly integer.
            if type(value) is int and value >= 0:
                self._stats[key] = value

    async def _safe_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Publish a custom event without letting failures escape the hook.

        The :class:`~nexusdl.core.events.EventBus` already isolates handler
        exceptions, but this wrapper also protects against a misconfigured bus
        (e.g. ``publish`` raising because it was already closed during
        shutdown). ``asyncio.CancelledError`` is a ``BaseException`` and is
        therefore **not** caught here — cancellation must always propagate.

        Args:
            event_type: Namespaced event type, e.g. ``plugin.example.foo``.
            payload: JSON-serialisable mapping carried by the event.
        """
        ctx = self._ctx
        if ctx is None:  # defensive: callers already guarantee this
            return
        try:
            await ctx.event_bus.publish(
                Event(
                    type=event_type,
                    timestamp=datetime.now(UTC),
                    payload=payload,
                    id=uuid4(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a hook must never break the pipeline
            ctx.logger.warning(
                "Failed to publish custom event {}: {}", event_type, exc
            )


# --------------------------------------------------------------------------- #
# Module-level helpers                                                        #
# --------------------------------------------------------------------------- #


def _safe_size(path: Path) -> int:
    """Return the size in bytes of *path*, or ``-1`` if it cannot be read.

    Args:
        path: File to stat.

    Returns:
        Size in bytes, or ``-1`` when ``stat`` fails (e.g. the file was
        removed by a cleanup routine between download and observation).
    """
    try:
        return path.stat().st_size
    except OSError:
        return -1


# --------------------------------------------------------------------------- #
# Static conformance check — verifies under ``mypy --strict`` that this      #
# example still satisfies the ``NexusPlugin`` protocol. Runtime no-op        #
# (``TYPE_CHECKING`` is False at import time).                                #
# --------------------------------------------------------------------------- #
if TYPE_CHECKING:
    _: NexusPlugin = ExamplePlugin()
