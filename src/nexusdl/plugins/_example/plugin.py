"""Reference NexusDL plugin — demonstrates every hook of the ``NexusPlugin`` protocol.

This module is deliberately pedagogical and side-effect-bounded:

* It never mutates the download pipeline (each hook is a transparent passthrough
  that returns the *same* object it received).
* It never raises out of a hook — every failure is logged and swallowed so a
  misbehaving example can never break a real download.
* It touches the outside world only through sanctioned channels: the plugin
  ``logger``, the shared ``event_bus``, and a tiny JSON stats file under
  ``context.data_dir``.

Layout of this example plugin (already present in the repo)::

    _example/
    ├── __init__.py
    ├── nexus.plugin.yaml   # manifest — see docs/development/plugins.md
    └── plugin.py           # ← this file

To activate it, copy the whole ``_example/`` directory into your user plugin
directory (see ``docs/development/plugins.md``) and rename the directory. The
manifest must live side-by-side with ``plugin.py``.

The class below structurally satisfies :class:`nexusdl.plugins.api.NexusPlugin`;
because that protocol is **structural** (``typing.Protocol``), no explicit
inheritance is required. The ``TYPE_CHECKING`` block at the bottom statically
asserts the conformance, so any drift between this example and the protocol
surfaces in ``mypy --strict``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nexusdl.core.events import Event
from nexusdl.core.models.download import DownloadResult, DownloadTask
from nexusdl.core.models.page import Page
from nexusdl.plugins.api import PluginContext


# Custom event types namespaced under ``plugin.example.*`` to avoid colliding
# with the framework's own ``download.*``, ``chapter.*`` etc. (see
# ``core/events.py`` for the list of standard events).
_EVENT_DOWNLOAD_COMPLETED = "plugin.example.download_completed"
_EVENT_DOWNLOAD_FAILED = "plugin.example.download_failed"

_STATS_FILENAME = "example_plugin_stats.json"


class ExamplePlugin:
    """A fully-implemented, harmless reference plugin.

    It counts lifecycle events, persists a small stats file in
    ``context.data_dir``, and republishes two custom events on the shared
    :class:`~nexusdl.core.events.EventBus`. It is safe to enable in any
    environment and is a good starting point to copy for your own plugins.
    """

    #: Plugin identifier — must match ``name:`` in ``nexus.plugin.yaml``.
    name: str = "example"

    #: Plugin version — must match ``version:`` in ``nexus.plugin.yaml``.
    version: str = "1.0.0"

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
            context: Framework-provided context exposing ``logger``,
                ``settings``, ``http_session`` (optional), ``event_bus``,
                ``plugin_dir`` and ``data_dir``.
        """
        self._ctx = context
        self._stats_path = context.data_dir / _STATS_FILENAME

        # ``on_load`` is synchronous by contract, and the stats file is bounded
        # to a few hundred bytes, so a synchronous read is acceptable here.
        try:
            if self._stats_path.is_file():
                raw = json.loads(self._stats_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in self._stats:
                        value = raw.get(key)
                        if isinstance(value, int) and value >= 0:
                            self._stats[key] = value
        except (OSError, json.JSONDecodeError) as exc:
            # Corruption is non-fatal: log and start fresh.
            context.logger.warning(
                "Could not restore previous plugin stats ({}); starting fresh.",
                exc,
            )

        context.logger.info(
            "ExamplePlugin v{} loaded (data_dir={}, plugin_dir={})",
            self.version,
            context.data_dir,
            context.plugin_dir,
        )

    def on_unload(self) -> None:
        """Persist stats and release the context.

        Synchronous by contract. The stats file is tiny, so the write is
        bounded and will not meaningfully stall the event loop even when
        ``on_unload`` is invoked from an async shutdown path.
        """
        if self._ctx is None or self._stats_path is None:
            return

        try:
            self._stats_path.parent.mkdir(parents=True, exist_ok=True)
            self._stats_path.write_text(
                json.dumps(self._stats, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            self._ctx.logger.info(
                "ExamplePlugin unloaded. Persisted stats: {}",
                self._stats,
            )
        except OSError as exc:
            # Never raise from a lifecycle hook: the framework is shutting down.
            self._ctx.logger.warning(
                "Could not persist plugin stats on unload: {}", exc
            )
        finally:
            self._ctx = None
            self._stats_path = None

    # ------------------------------------------------------------------ #
    # Download pipeline hooks (asynchronous)                             #
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
        if self._ctx is None:
            return task

        async with self._lock:
            self._stats["tasks_started"] += 1

        # Optional: the context may expose a shared ``HttpSession``. It is
        # ``None`` when the plugin is loaded in a context that has no network
        # access (e.g. the offline TUI settings screen).
        # session = self._ctx.http_session
        # if session is not None:
        #     ...

        self._ctx.logger.debug(
            "on_before_download: task_id={} status={}", task.id, task.status
        )
        return task

    async def on_after_download(self, result: DownloadResult) -> DownloadResult:
        """Observe a completed download and republish a custom event.

        Args:
            result: The outcome produced by the
                :class:`~nexusdl.core.downloader.worker.DownloadWorker`.

        Returns:
            The same result, unmodified.
        """
        if self._ctx is None:
            return result

        async with self._lock:
            self._stats["tasks_completed"] += 1
            snapshot = dict(self._stats)

        self._ctx.logger.info("on_after_download: result={!r}", result)

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
        if self._ctx is None:
            return

        async with self._lock:
            self._stats["pages_downloaded"] += 1

        self._ctx.logger.trace(
            "on_page_downloaded: index={} size={} bytes",
            page.index,
            _safe_size(path),
        )

    async def on_error(self, task_id: str, error: Exception) -> None:
        """Observe an error raised while running a download task.

        Args:
            task_id: Identifier of the task that failed.
            error: The exception raised by the worker. Cancellations are
                ``BaseException`` subclasses and never reach this hook, so
                everything received here is a genuine failure.
        """
        if self._ctx is None:
            return

        async with self._lock:
            self._stats["errors_seen"] += 1

        self._ctx.logger.error(
            "on_error: task_id={} error={}: {}",
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

    async def _safe_publish(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Publish a custom event without letting failures escape the hook.

        The :class:`~nexusdl.core.events.EventBus` already isolates handler
        exceptions, but this wrapper also protects against a misconfigured bus
        (e.g. ``publish`` raising ``RuntimeError`` because it was already
        closed during shutdown).

        Args:
            event_type: Namespaced event type, e.g. ``plugin.example.foo``.
            payload: JSON-serialisable mapping carried by the event.
        """
        if self._ctx is None:
            return
        try:
            await self._ctx.event_bus.publish(
                Event(
                    type=event_type,
                    timestamp=datetime.now(UTC),
                    payload=payload,
                    id=uuid4(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a hook must never break the pipeline
            self._ctx.logger.warning(
                "Failed to publish custom event {}: {}", event_type, exc
            )


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


# ---------------------------------------------------------------------- #
# Static conformance check — verifies in ``mypy --strict`` that this       #
# example still implements the full ``NexusPlugin`` protocol. It is a      #
# runtime no-op (``TYPE_CHECKING`` is False at import time).               #
# ---------------------------------------------------------------------- #
if TYPE_CHECKING:
    from nexusdl.plugins.api import NexusPlugin

    _: NexusPlugin = ExamplePlugin()
