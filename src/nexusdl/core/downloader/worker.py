"""Execute a single :class:`DownloadTask` end to end.

The worker is the innermost layer of the download pipeline. It knows how to
take one chapter from "pending" to "packaged file on disk", and nothing
else: it does not manage queues, cross-chapter retries, pause/resume, or
task-lifecycle events. Those are the manager's responsibility. Keeping the
worker focused on a single task makes it trivial to test with a mock parser
and an in-memory packager.

Dependency inversion
--------------------
Two dependencies live outside ``core/`` and are therefore accessed through
**structural protocols** defined in this module, not through concrete
imports (PROMPT MASTER rules R1 and R6, ADR-003):

* :class:`ParserProtocol` — satisfied by
  :class:`nexusdl.parsers.base.BaseParser`.
* :class:`PluginDispatcherProtocol` — satisfied by
  :class:`nexusdl.plugins.loader.PluginLoader`.

Everything else lives in ``core/`` and is imported directly. In particular,
:class:`~nexusdl.core.packaging.factory.PackagerFactory`,
:class:`~nexusdl.core.packaging.comic_info.ComicInfo`, and the retry
controller from :mod:`nexusdl.core.downloader.retry` are all core modules.

Integration contracts (fixed — do not change without updating the worker)
--------------------------------------------------------------------------
The worker is built by a ``worker_factory`` created in the composition root
(CLI/TUI/GUI/Web). That factory closes over the plugin dispatcher and passes
it to the worker. Concretely, every one of the following is a *stable*
interface the worker depends on:

* ``Settings.downloader.max_concurrent_pages: int`` — page concurrency
* ``Settings.downloader.retry`` — duck-typed, passed to
  :func:`nexusdl.core.downloader.retry.policy_from_settings`
* ``Settings.storage.downloads_dir: Path | str`` — base output directory
* ``Settings.packaging.compression_level: int | None`` — optional pass-through
* ``PackagerFactory.create(fmt) -> BasePackager`` — the packager factory
* ``BasePackager.package(pages, output, *, metadata=None, compression_level=None) -> Path``
  where ``pages`` is ``Sequence[tuple[Page, Path]]`` in reading order.
* ``validate_file(path) -> ValidationResult`` with ``.is_valid: bool`` and
  ``.error: str | None`` — from :mod:`nexusdl.core.image.validator`
* ``ComicInfo(...)`` accepts ``title, series, number, language_iso,
  page_count, summary, year, manga, web, scan_information`` — from
  :mod:`nexusdl.core.packaging.comic_info`
* ``EventBus.publish(Event(type=, timestamp=, payload=, id=))`` — from
  :mod:`nexusdl.core.events`
* ``slugify(text) -> str`` — from the ``python-slugify`` package
* ``DownloadStatus.DONE`` / ``DownloadStatus.FAILED``,
  ``PackagingFormat.{FOLDER,ZIP,CBZ,CB7,PDF,CBT}``, ``Language.*`` — enums
* ``DownloadTask.id, .chapter, .manga, .format`` — read-only
* ``DownloadResult(task_id, status, output_path, pages_downloaded,
  bytes_downloaded, duration_seconds, error, completed_at)`` — construction
* ``Chapter.id, .title, .number, .url`` — read-only
* ``Manga.id, .title, .language, .description, .year, .is_manga`` — read-only
* ``Page.index, .url, .filename`` — read-only
* Exceptions: ``PageDownloadError``, ``ChapterDownloadError``,
  ``EmptyChapterError`` — from :mod:`nexusdl.core.exceptions`

If any of these changes, the worker is the *only* file that needs to be
touched — everything else in the pipeline depends on the worker through the
:meth:`DownloadWorker.run` signature, which is frozen by the PROMPT MASTER.

Concurrency and cancellation
----------------------------
Pages are downloaded concurrently using :class:`asyncio.TaskGroup` bounded
by a :class:`asyncio.Semaphore`. A single page failure cancels the
remaining downloads of the same chapter — the worker fails the chapter fast
rather than waiting for pages that will be discarded anyway.

``asyncio.CancelledError`` always propagates untouched. The three cases the
``TaskGroup`` unwinding must handle are covered explicitly:

1. Parent cancelled directly → ``except asyncio.CancelledError: raise``.
2. Every child failure was a cancellation → re-raise as
   ``CancelledError() from eg``.
3. At least one real child error → wrap in :class:`ChapterDownloadError`
   from the first real error.

Failure semantics
-----------------
Any exception other than :class:`asyncio.CancelledError` is caught, logged,
reported to plugins via ``dispatch_on_error``, and returned as a
:class:`DownloadResult` with ``status=FAILED`` and ``error=<cause>``. The
worker therefore **never raises for a "normal" failure** — one broken
chapter cannot abort a multi-chapter run. This matches how the manager
iterates chapters.

Retry policy
------------
The retry policy is built once per :meth:`run` call via
:func:`nexusdl.core.downloader.retry.policy_from_settings` and applied to
each page via :func:`nexusdl.core.downloader.retry.build_retry`. The
classifier (:func:`~nexusdl.core.downloader.retry.is_retryable`) decides
which exceptions are worth retrying; the worker does not duplicate that
knowledge.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from loguru import logger

from nexusdl.core.downloader.retry import (
    RetryPolicy,
    build_retry,
    policy_from_settings,
)
from nexusdl.core.events import Event, EventBus
from nexusdl.core.exceptions import (
    ChapterDownloadError,
    EmptyChapterError,
    PageDownloadError,
)
from nexusdl.core.image.validator import validate_file as validate_image_file
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.download import DownloadResult, DownloadTask
from nexusdl.core.models.enums import DownloadStatus, Language, PackagingFormat
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.packaging.comic_info import ComicInfo
from nexusdl.core.packaging.factory import PackagerFactory

from slugify import slugify

if TYPE_CHECKING:
    from nexusdl.core.config import Settings


__all__ = [
    "DownloadWorker",
    "ParserProtocol",
    "PluginDispatcherProtocol",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Standard event types emitted by the worker (see ``nexusdl.core.events``).
_EVENT_DOWNLOAD_STARTED: Final[str] = "download.started"
_EVENT_DOWNLOAD_PROGRESS: Final[str] = "download.progress"
_EVENT_DOWNLOAD_COMPLETED: Final[str] = "download.completed"
_EVENT_DOWNLOAD_FAILED: Final[str] = "download.failed"

#: Extension per packaging format. ``FOLDER`` produces a directory, hence
#: the empty extension.
_FORMAT_EXTENSIONS: Final[Mapping[PackagingFormat, str]] = MappingProxyType(
    {
        PackagingFormat.FOLDER: "",
        PackagingFormat.ZIP: ".zip",
        PackagingFormat.CBZ: ".cbz",
        PackagingFormat.CB7: ".cb7",
        PackagingFormat.PDF: ".pdf",
        PackagingFormat.CBT: ".tar",
    }
)

#: ISO 639-1 codes for each :class:`Language` member. ``UNKNOWN`` maps to
#: ``"und"`` (undetermined), valid per ISO 639-2 and accepted by ComicInfo
#: readers (Komga, Kavita, Mihon, YACReader, CDisplayEx).
_LANGUAGE_ISO_639_1: Final[Mapping[Language, str]] = MappingProxyType(
    {
        Language.FR: "fr",
        Language.EN: "en",
        Language.JA: "ja",
        Language.KO: "ko",
        Language.ZH: "zh",
        Language.ES: "es",
        Language.PT: "pt",
        Language.IT: "it",
        Language.DE: "de",
        Language.RU: "ru",
        Language.AR: "ar",
        Language.UNKNOWN: "und",
    }
)

#: Fraction of the chapter-level progress attributed to page downloads.
#: The remaining 5% covers packaging.
_PAGES_PROGRESS_WEIGHT: Final[float] = 0.95

#: Default concurrency limit when ``Settings.downloader.max_concurrent_pages``
#: is absent (defensive — the field is documented in the PROMPT MASTER).
_DEFAULT_MAX_CONCURRENT_PAGES: Final[int] = 4


# --------------------------------------------------------------------------- #
# Structural protocols                                                        #
# --------------------------------------------------------------------------- #


class ParserProtocol(Protocol):
    """Minimal parser interface required by the worker.

    Satisfied by :class:`nexusdl.parsers.base.BaseParser`. Only the two
    methods the worker actually uses are declared.
    """

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered list of pages for *chapter*."""
        ...

    async def download_page(self, page: Page, dest: Path) -> None:
        """Download *page* to the file path *dest*."""
        ...


class PluginDispatcherProtocol(Protocol):
    """Minimal plugin-dispatcher interface required by the worker.

    Satisfied by :class:`nexusdl.plugins.loader.PluginLoader`. Only the two
    observer-only hooks the worker emits are declared — the manager
    dispatches ``on_before_download`` / ``on_after_download``.
    """

    async def dispatch_on_page_downloaded(
        self, page: Page, path: Path
    ) -> None: ...

    async def dispatch_on_error(
        self, task_id: str, error: Exception
    ) -> None: ...


# --------------------------------------------------------------------------- #
# Worker                                                                      #
# --------------------------------------------------------------------------- #


class DownloadWorker:
    """Execute a single :class:`DownloadTask` end to end.

    Lifecycle of one :meth:`run` call:

    1. Fetch the chapter's page list from the parser.
    2. Build the retry policy once from settings.
    3. Download pages concurrently to a temporary directory, with per-page
       retry and per-page image validation.
    4. Build :class:`ComicInfo` metadata from the manga and chapter.
    5. Package pages into the requested format via the packager factory.
    6. Clean up temporary files in ``finally``.

    ``asyncio.CancelledError`` is always propagated untouched. Any other
    exception is caught, logged, reported to plugins via ``dispatch_on_error``,
    and returned as a failed :class:`DownloadResult` — the worker never
    raises for a "normal" failure.

    State
    -----
    A worker instance is bound to a single :meth:`run` at a time. Do not
    share one worker across concurrent invocations. The manager creates one
    worker per task (via its injected ``worker_factory``), so this constraint
    is naturally satisfied.
    """

    def __init__(
        self,
        parser: ParserProtocol,
        packager_factory: PackagerFactory,
        settings: Settings,
        event_bus: EventBus,
        plugin_dispatcher: PluginDispatcherProtocol | None = None,
    ) -> None:
        """Initialise the worker (no I/O, no side effects on the environment).

        The first four arguments mirror the PROMPT MASTER contract for
        ``DownloadWorker`` exactly. ``plugin_dispatcher`` is an optional
        extension captured by the composition root's ``worker_factory``; when
        ``None``, no plugin hooks are invoked (tests, or contexts where the
        plugin subsystem is not loaded).

        Args:
            parser: Parser bound to the chapter's site, already configured
                with an HTTP session.
            packager_factory: Factory producing the packager matching the
                task's ``format``.
            settings: Application settings, used for concurrency, retry
                policy, compression level, and output directory.
            event_bus: Shared event bus. The worker emits
                ``download.started`` / ``download.progress`` /
                ``download.completed`` / ``download.failed``.
            plugin_dispatcher: Optional dispatcher for the two observer-only
                plugin hooks (``on_page_downloaded`` and ``on_error``).
        """
        self._parser = parser
        self._packager_factory = packager_factory
        self._settings = settings
        self._event_bus = event_bus
        self._plugin_dispatcher = plugin_dispatcher
        self._log = logger.bind(component="downloader.worker")

        # Per-worker semaphore: the concurrency limit applies *within* a
        # chapter, not across chapters.
        self._semaphore = asyncio.Semaphore(self._max_concurrent_pages())

        # Serialises progress-counter updates and event emission so that
        # ``download.progress`` events arrive in completion order.
        self._progress_lock = asyncio.Lock()

        # Reset at the start of each ``run`` call.
        self._pages_completed: int = 0

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def run(
        self,
        task: DownloadTask,
        *,
        output_path: Path | None = None,
    ) -> DownloadResult:
        """Execute *task* and return the outcome.

        Args:
            task: The chapter download to perform. Read-only — the worker
                never mutates it. State transitions are the manager's
                responsibility.
            output_path: Optional explicit output path. When ``None``, a
                default is computed from
                ``settings.storage.downloads_dir``, the manga title, and the
                chapter title — see :meth:`_compute_output_path`.

        Returns:
            A :class:`DownloadResult` with ``status`` either
            :attr:`DownloadStatus.DONE` or :attr:`DownloadStatus.FAILED`.
            On failure the cause is available as ``result.error``.
        """
        started_at = time.monotonic()
        self._pages_completed = 0

        chapter = task.chapter
        manga = task.manga
        self._log.info(
            "Starting download: manga={!r} chapter={!r} format={}",
            manga.title,
            chapter.title,
            task.format.value,
        )

        try:
            pages = await self._parser.get_pages(chapter)
            if not pages:
                raise EmptyChapterError(
                    f"chapter {chapter.id!r} ({chapter.title!r}) has no pages"
                )

            target = output_path or self._compute_output_path(task)
            retry_policy = policy_from_settings(
                getattr(self._settings.downloader, "retry", None)
            )

            await self._emit_event(
                _EVENT_DOWNLOAD_STARTED,
                {
                    "task_id": str(task.id),
                    "chapter_id": chapter.id,
                    "manga_id": manga.id,
                    "page_count": len(pages),
                    "format": task.format.value,
                },
            )

            temp_dir = self._create_temp_dir(target)
            try:
                await self._download_pages(
                    task, pages, temp_dir, retry_policy
                )
                await self._emit_progress(task, _PAGES_PROGRESS_WEIGHT, phase="pages")

                pages_with_paths = self._collect_page_paths(pages, temp_dir)
                bytes_total = self._sum_page_sizes(pages_with_paths)

                await self._emit_progress(task, 0.97, phase="packaging")
                output = await self._package(task, pages_with_paths, target)
            finally:
                self._cleanup_temp_dir(temp_dir)

            elapsed = time.monotonic() - started_at
            result = DownloadResult(
                task_id=task.id,
                status=DownloadStatus.DONE,
                output_path=output,
                pages_downloaded=len(pages),
                bytes_downloaded=bytes_total,
                duration_seconds=elapsed,
                error=None,
                completed_at=datetime.now(UTC),
            )
            await self._emit_event(
                _EVENT_DOWNLOAD_COMPLETED,
                {
                    "task_id": str(task.id),
                    "chapter_id": chapter.id,
                    "output_path": str(output),
                    "pages_downloaded": len(pages),
                    "bytes_downloaded": bytes_total,
                    "duration_seconds": elapsed,
                },
            )
            self._log.info(
                "Completed download: chapter={!r} -> {} ({} pages, {:.1f}s)",
                chapter.title,
                output,
                len(pages),
                elapsed,
            )
            return result

        except asyncio.CancelledError:
            # Cancellation is a terminal state, not an error. The temp-dir
            # ``finally`` above has already run. Propagate so the manager
            # can transition the task to CANCELLED.
            self._log.debug("Download cancelled: chapter={!r}", chapter.title)
            raise

        except Exception as exc:
            elapsed = time.monotonic() - started_at
            self._log.exception(
                "Download failed: chapter={!r} after {:.1f}s: {}",
                chapter.title,
                elapsed,
                exc,
            )
            await self._safe_dispatch_on_error(task, exc)
            await self._emit_event(
                _EVENT_DOWNLOAD_FAILED,
                {
                    "task_id": str(task.id),
                    "chapter_id": chapter.id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "duration_seconds": elapsed,
                },
            )
            return DownloadResult(
                task_id=task.id,
                status=DownloadStatus.FAILED,
                output_path=None,
                pages_downloaded=self._pages_completed,
                bytes_downloaded=0,
                duration_seconds=elapsed,
                error=exc,
                completed_at=datetime.now(UTC),
            )

    # ------------------------------------------------------------------ #
    # Page download                                                      #
    # ------------------------------------------------------------------ #

    async def _download_pages(
        self,
        task: DownloadTask,
        pages: list[Page],
        temp_dir: Path,
        retry_policy: RetryPolicy,
    ) -> None:
        """Download all pages to *temp_dir*, honouring concurrency and retries.

        Uses :class:`asyncio.TaskGroup` so that the first page failure
        cancels the remaining downloads. The resulting
        :class:`BaseExceptionGroup` is unwrapped into either a bare
        :class:`asyncio.CancelledError` (when every child failure was a
        cancellation) or a :class:`ChapterDownloadError` (otherwise).

        Args:
            task: The task being executed (for ids and events).
            pages: Pages to download, in reading order.
            temp_dir: Destination directory for page files.
            retry_policy: Retry parameters applied per page.

        Raises:
            ChapterDownloadError: If at least one page failed after exhausting
                retries.
            asyncio.CancelledError: If the parent was cancelled, or if every
                child failure was a cancellation.
        """
        total = len(pages)
        try:
            async with asyncio.TaskGroup() as tg:
                for page in pages:
                    tg.create_task(
                        self._download_one_page(
                            task, page, temp_dir, total, retry_policy
                        ),
                        name=f"download-page-{page.index}",
                    )
        except asyncio.CancelledError:
            # Parent cancelled directly (not via a child failure).
            raise
        except BaseExceptionGroup as eg:
            real_errors = [
                exc
                for exc in eg.exceptions
                if not isinstance(exc, asyncio.CancelledError)
            ]
            if not real_errors:
                # Every child failure was a cancellation: propagate.
                raise asyncio.CancelledError() from eg
            raise ChapterDownloadError(
                f"failed to download {len(real_errors)} of {total} "
                f"page(s) for chapter {task.chapter.id!r}"
            ) from real_errors[0]

    async def _download_one_page(
        self,
        task: DownloadTask,
        page: Page,
        temp_dir: Path,
        total_pages: int,
        retry_policy: RetryPolicy,
    ) -> None:
        """Download, validate, and report a single page.

        The retry controller from
        :func:`nexusdl.core.downloader.retry.build_retry` decides which
        exceptions are worth retrying — the worker never duplicates that
        knowledge. Validation rejects HTML pages masquerading as images by
        raising :class:`PageDownloadError`, which is retryable.

        Args:
            task: The task being executed.
            page: The page to download.
            temp_dir: Directory to write the page file into.
            total_pages: Total number of pages in the chapter (for progress).
            retry_policy: Retry parameters.

        Raises:
            PageDownloadError: If the page could not be downloaded or
                validated after exhausting retries.
            asyncio.CancelledError: If the task was cancelled.
        """
        dest = temp_dir / self._page_filename(page)

        async with self._semaphore:
            try:
                async for attempt in build_retry(retry_policy):
                    with attempt:
                        await self._parser.download_page(page, dest)
                        validation = validate_image_file(dest)
                        if not validation.is_valid:
                            raise PageDownloadError(
                                f"page {page.index} failed validation: "
                                f"{validation.error}"
                            )
            except PageDownloadError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise PageDownloadError(
                    f"unexpected error downloading page {page.index}: {exc}"
                ) from exc

        # Success path: dispatch plugins, then report progress. Both run
        # outside the semaphore so a slow subscriber does not block the next
        # page's download.
        await self._safe_dispatch_on_page_downloaded(page, dest)
        await self._record_page_progress(task, total_pages)

    async def _record_page_progress(
        self, task: DownloadTask, total_pages: int
    ) -> None:
        """Increment the completed-page counter and emit a progress event.

        The lock guarantees progress events are emitted in completion order,
        so downstream consumers never see progress go backwards.

        Args:
            task: The task being executed.
            total_pages: Total number of pages in the chapter.
        """
        async with self._progress_lock:
            self._pages_completed += 1
            completed = self._pages_completed
        progress = (completed / total_pages) * _PAGES_PROGRESS_WEIGHT
        await self._emit_progress(task, progress, phase="pages")

    # ------------------------------------------------------------------ #
    # Packaging                                                          #
    # ------------------------------------------------------------------ #

    async def _package(
        self,
        task: DownloadTask,
        pages_with_paths: list[tuple[Page, Path]],
        output_path: Path,
    ) -> Path:
        """Package downloaded pages into the requested format.

        Passes ``Sequence[tuple[Page, Path]]`` to the packager so each
        implementation can sort by ``page.index`` if it wants to, and can
        ignore the ``Page`` and sort by filename if the naming convention is
        enough (the worker produces zero-padded filenames — see
        :meth:`_page_filename`).

        Args:
            task: The task being executed (source of ``format`` and metadata).
            pages_with_paths: Each page paired with the file it was written
                to, in reading order.
            output_path: Where the packager should produce its output. For
                ``FOLDER`` this is a directory; for every other format it is
                a file path.

        Returns:
            The path the packager actually produced.

        Raises:
            PackagingError: If the packager fails. The caller treats this as
                a normal download failure.
        """
        packager = self._packager_factory.create(task.format)
        comic_info = self._build_comic_info(task, len(pages_with_paths))
        compression_level = getattr(
            self._settings.packaging, "compression_level", None
        )
        return await packager.package(
            pages_with_paths,
            output_path,
            metadata=comic_info,
            compression_level=compression_level,
        )

    def _build_comic_info(
        self, task: DownloadTask, page_count: int
    ) -> ComicInfo:
        """Build :class:`ComicInfo` metadata from the task's manga and chapter.

        Fields not exposed by the manga model are left ``None`` — packagers
        embed what is present, and readers (Komga / Kavita / Mihon /
        YACReader) handle missing fields gracefully.

        The ``manga`` field follows the ComicRack convention used by every
        reader: ``"YesAndRightToLeft"`` for manga, ``"No"`` for
        non-manga (western comics, webtoons).

        Args:
            task: The task being executed.
            page_count: Number of pages in the packaged output.

        Returns:
            A populated :class:`ComicInfo` instance.
        """
        manga: Manga = task.manga
        chapter: Chapter = task.chapter
        language_iso = _LANGUAGE_ISO_639_1.get(manga.language, "und")
        is_manga = bool(getattr(manga, "is_manga", True))
        return ComicInfo(
            title=chapter.title,
            series=manga.title,
            number=str(getattr(chapter, "number", "1")),
            language_iso=language_iso,
            page_count=page_count,
            summary=getattr(manga, "description", None),
            year=getattr(manga, "year", None),
            manga="YesAndRightToLeft" if is_manga else "No",
            web=getattr(chapter, "url", None),
            scan_information=f"Downloaded by NexusDL from {manga.id}",
        )

    # ------------------------------------------------------------------ #
    # Filesystem helpers                                                 #
    # ------------------------------------------------------------------ #

    def _compute_output_path(self, task: DownloadTask) -> Path:
        """Compute the default output path for *task*.

        Layout: ``<downloads_dir>/<manga-slug>/<chapter-slug><ext>``, where
        ``ext`` comes from :data:`_FORMAT_EXTENSIONS`. ``FOLDER`` produces a
        directory (no extension).

        Args:
            task: The task being executed.

        Returns:
            The default output path. The parent directory is created by
            :meth:`_create_temp_dir`.
        """
        downloads_dir = Path(self._settings.storage.downloads_dir)
        manga_dir = downloads_dir / slugify(task.manga.title)
        chapter_slug = slugify(task.chapter.title)
        extension = _FORMAT_EXTENSIONS[task.format]
        return manga_dir / f"{chapter_slug}{extension}"

    @staticmethod
    def _create_temp_dir(target: Path) -> Path:
        """Create a temporary directory next to *target* and return it.

        Keeping the temp directory on the same filesystem as the final
        output lets the packager move / rename files without a cross-device
        copy. ``target.parent`` is created if necessary.

        Args:
            target: The final output path (file or directory).

        Returns:
            The path to the freshly created temporary directory.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=".nexusdl-", dir=target.parent))

    @staticmethod
    def _cleanup_temp_dir(temp_dir: Path) -> None:
        """Remove *temp_dir* and its contents if it still exists.

        Idempotent and safe to call from a ``finally`` block. The operation
        is synchronous — deleting a chapter's worth of pages is bounded to a
        few dozen milliseconds and does not warrant an ``asyncio.to_thread``
        hop during shutdown.

        Args:
            temp_dir: The temporary directory to remove.
        """
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=False)
        except OSError as exc:
            logger.warning(
                "Could not remove temp directory {}: {}", temp_dir, exc
            )

    @staticmethod
    def _collect_page_paths(
        pages: list[Page], temp_dir: Path
    ) -> list[tuple[Page, Path]]:
        """Pair each page with its file path in *temp_dir*.

        Args:
            pages: Pages in reading order.
            temp_dir: Directory containing the downloaded page files.

        Returns:
            A list of ``(page, path)`` tuples, in the same order as *pages*.
        """
        return [
            (page, temp_dir / DownloadWorker._page_filename(page))
            for page in pages
        ]

    @staticmethod
    def _sum_page_sizes(pages_with_paths: list[tuple[Page, Path]]) -> int:
        """Return the total size in bytes of the downloaded page files.

        Args:
            pages_with_paths: Each page paired with the file it was written
                to.

        Returns:
            The sum of file sizes in bytes.
        """
        return sum(path.stat().st_size for _, path in pages_with_paths)

    @staticmethod
    def _page_filename(page: Page) -> str:
        """Return the on-disk filename for *page*.

        Prefers ``page.filename`` when the model exposes one. Otherwise
        derives the extension from the page URL and uses a zero-padded
        1-based index so that CBZ / ComicInfo readers display pages in
        order (``sorted()`` on filenames matches reading order).

        Args:
            page: The page to name.

        Returns:
            A filename (no directory component).
        """
        existing = getattr(page, "filename", None)
        if existing:
            return str(existing)
        url_path = Path(urlparse(page.url).path)
        suffix = url_path.suffix or ".jpg"
        return f"{page.index + 1:04d}{suffix}"

    # ------------------------------------------------------------------ #
    # Event emission                                                     #
    # ------------------------------------------------------------------ #

    async def _emit_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Publish *event_type* on the bus, swallowing and logging failures.

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
                    id=uuid4(),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "Could not publish event {}: {}", event_type, exc
            )

    async def _emit_progress(
        self,
        task: DownloadTask,
        progress: float,
        *,
        phase: str,
    ) -> None:
        """Emit a ``download.progress`` event.

        Args:
            task: The task being executed.
            progress: Completion fraction, clamped to ``[0.0, 1.0]``.
            phase: Current phase (``"pages"``, ``"packaging"``). Surfaced
                in the event payload so consumers can display phase-specific
                messages.
        """
        await self._emit_event(
            _EVENT_DOWNLOAD_PROGRESS,
            {
                "task_id": str(task.id),
                "chapter_id": task.chapter.id,
                "progress": max(0.0, min(1.0, progress)),
                "phase": phase,
            },
        )

    # ------------------------------------------------------------------ #
    # Plugin dispatch                                                    #
    # ------------------------------------------------------------------ #

    async def _safe_dispatch_on_page_downloaded(
        self, page: Page, path: Path
    ) -> None:
        """Dispatch ``on_page_downloaded`` without letting failures escape.

        Args:
            page: The page that was downloaded.
            path: The file it was written to.
        """
        if self._plugin_dispatcher is None:
            return
        try:
            await self._plugin_dispatcher.dispatch_on_page_downloaded(
                page, path
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "Plugin dispatch_on_page_downloaded failed for index={}: {}",
                page.index,
                exc,
            )

    async def _safe_dispatch_on_error(
        self, task: DownloadTask, error: Exception
    ) -> None:
        """Dispatch ``on_error`` without letting failures escape.

        Args:
            task: The task that failed.
            error: The exception that caused the failure.
        """
        if self._plugin_dispatcher is None:
            return
        try:
            await self._plugin_dispatcher.dispatch_on_error(
                str(task.id), error
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "Plugin dispatch_on_error failed for task_id={}: {}",
                task.id,
                exc,
            )

    # ------------------------------------------------------------------ #
    # Settings accessors (with fallbacks)                                #
    # ------------------------------------------------------------------ #

    def _max_concurrent_pages(self) -> int:
        """Return the page-download concurrency limit from settings."""
        return int(
            getattr(
                self._settings.downloader,
                "max_concurrent_pages",
                _DEFAULT_MAX_CONCURRENT_PAGES,
            )
        )
