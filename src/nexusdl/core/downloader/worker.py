"""Execute a single :class:`DownloadTask` end to end.

The worker is the innermost layer of the download pipeline. It knows how to
take one chapter from "pending" to "packaged file on disk", and nothing
else: it does not manage queues, retries across chapters, pause/resume, or
task-lifecycle events. Those are the manager's responsibility
(``core/downloader/manager.py``). Keeping the worker focused on a single
task makes it trivial to test with a mock parser and a real (in-memory)
packager.

Dependency inversion
--------------------
Two dependencies live outside ``core/`` and are therefore accessed through
**structural protocols** defined in this module, not through concrete
imports (see ADR-003 and PROMPT MASTER rule R1):

* :class:`ParserProtocol` — satisfied by
  :class:`nexusdl.parsers.base.BaseParser`.
* :class:`PluginDispatcherProtocol` — satisfied by
  :class:`nexusdl.plugins.loader.PluginLoader`.

Everything else lives in ``core/`` and is imported directly. In particular,
``BasePackager`` and ``PackagerFactory`` are already part of the core.

Concurrency and cancellation
----------------------------
Pages are downloaded concurrently using :class:`asyncio.TaskGroup` bounded
by a :class:`asyncio.Semaphore`, so a single page failure cancels the
remaining downloads in the same chapter rather than waiting for them to
finish. ``asyncio.CancelledError`` is always propagated untouched — the
worker never turns cancellation into a "failed result", because the manager
needs to distinguish the two.

Failure semantics
-----------------
Any exception other than :class:`asyncio.CancelledError` is caught, logged,
reported to plugins via ``dispatch_on_error``, and returned as a
:class:`DownloadResult` with ``status=FAILED`` and ``error=<cause>``. The
worker therefore **never raises for a "normal" failure** — one broken
chapter cannot abort a multi-chapter run. This is the same convention the
manager uses when iterating chapters.
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
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
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
from nexusdl.core.utils.text import slugify

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

#: Names of the events emitted by the worker. Framework-standard types (see
#: ``nexusdl.core.events``); not to be confused with ``chapter.*`` events,
#: which the manager emits when iterating a manga.
_EVENT_DOWNLOAD_STARTED: Final[str] = "download.started"
_EVENT_DOWNLOAD_PROGRESS: Final[str] = "download.progress"
_EVENT_DOWNLOAD_COMPLETED: Final[str] = "download.completed"
_EVENT_DOWNLOAD_FAILED: Final[str] = "download.failed"

#: Extension per packaging format. ``FOLDER`` has no extension because the
#: packager produces a directory, not a file.
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

#: ISO 639-1 codes for each :class:`Language` enum member. ``UNKNOWN`` maps
#: to ``"und"`` (undetermined), which is valid per ISO 639-2 and accepted by
#: ComicInfo readers.
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

#: Fraction of the chapter-level progress attributed to page downloads. The
#: remaining 5% covers packaging.
_PAGES_PROGRESS_WEIGHT: Final[float] = 0.95

#: Default settings values used when the corresponding ``Settings`` field is
#: absent. Centralised here so a configuration change is a single-place edit
#: and the worker remains usable in tests with a minimal stub.
_DEFAULT_MAX_CONCURRENT_PAGES: Final[int] = 4
_DEFAULT_RETRY_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_RETRY_INITIAL_DELAY: Final[float] = 0.5
_DEFAULT_RETRY_MAX_DELAY: Final[float] = 30.0


# --------------------------------------------------------------------------- #
# Structural protocols                                                        #
# --------------------------------------------------------------------------- #


class ParserProtocol(Protocol):
    """Minimal parser interface required by the worker.

    Satisfied by :class:`nexusdl.parsers.base.BaseParser`. Only the two
    methods the worker actually uses are declared, so this protocol does not
    couple the worker to the full parser surface.
    """

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Return the ordered list of pages for *chapter*."""
        ...

    async def download_page(self, page: Page, dest: Path) -> None:
        """Download *page* to *dest* (a file path, not a directory)."""
        ...


class PluginDispatcherProtocol(Protocol):
    """Minimal plugin-dispatcher interface required by the worker.

    Satisfied by :class:`nexusdl.plugins.loader.PluginLoader`. Only the two
    hooks the worker emits are declared; the manager dispatches the other
    two (``on_before_download``, ``on_after_download``).
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

    The worker:

    1. Fetches the chapter's page list from the parser.
    2. Downloads pages concurrently to a temporary directory, with per-page
       retry and per-page image validation.
    3. Builds :class:`ComicInfo` metadata from the manga and chapter.
    4. Packages pages into the requested format via the
       :class:`PackagerFactory`.
    5. Cleans up temporary files in ``finally``.

    ``asyncio.CancelledError`` is always propagated untouched — the manager
    is responsible for translating cancellation into a state transition.

    Any other exception is caught, logged, reported to plugins via
    ``dispatch_on_error``, and returned as a failed
    :class:`DownloadResult` — the worker never raises for a "normal" failure.

    State
    -----
    The worker holds only progress-accounting state (``_pages_completed`` is
    reset at each :meth:`run`). A single instance must **not** be shared
    across concurrent :meth:`run` invocations; the manager creates one
    worker per task, so this constraint is naturally satisfied.
    """

    def __init__(
        self,
        *,
        parser: ParserProtocol,
        packager_factory: PackagerFactory,
        settings: Settings,
        event_bus: EventBus,
        plugin_dispatcher: PluginDispatcherProtocol | None = None,
    ) -> None:
        """Initialise the worker (no I/O, no side effects on the environment).

        Args:
            parser: Parser bound to the chapter's site, already configured
                with an HTTP session.
            packager_factory: Factory that produces the packager matching
                the task's ``format``.
            settings: Application settings, used for concurrency, retry
                policy, compression level, and the default output directory.
            event_bus: Shared event bus. The worker emits
                ``download.started`` / ``download.progress`` /
                ``download.completed`` / ``download.failed``.
            plugin_dispatcher: Optional plugin dispatcher. When ``None``, no
                plugin hooks are invoked — useful for tests and for contexts
                where the plugin subsystem is not loaded.
        """
        self._parser = parser
        self._packager_factory = packager_factory
        self._settings = settings
        self._event_bus = event_bus
        self._plugin_dispatcher = plugin_dispatcher
        self._log = logger.bind(component="downloader.worker")

        #: Concurrency limiter for page downloads. Created per worker (i.e.
        #: per task) so the limit applies *within* a chapter, not across
        #: chapters.
        self._semaphore = asyncio.Semaphore(self._max_concurrent_pages())

        #: Serialises progress-counter updates and event emission so that
        #: ``download.progress`` events arrive in completion order.
        self._progress_lock = asyncio.Lock()

        #: Reset at the start of each :meth:`run`.
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
            task: The chapter download to perform. Read-only: the worker
                does not mutate it. State transitions are the manager's
                responsibility.
            output_path: Optional explicit output path. When ``None``, a
                default is computed from
                ``settings.storage.downloads_dir``, the manga title, and
                the chapter title — see :meth:`_compute_output_path`.

        Returns:
            A :class:`DownloadResult` with ``status`` either
            :attr:`DownloadStatus.DONE` (success) or
            :attr:`DownloadStatus.FAILED` (failure). On failure the cause is
            available as ``result.error``.
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
                await self._download_pages(task, pages, temp_dir)
                await self._emit_progress(task, 1.0, phase="pages")

                pages_with_paths = self._collect_page_paths(pages, temp_dir)
                output = await self._package(task, pages_with_paths, target)
                bytes_total = sum(
                    path.stat().st_size for _, path in pages_with_paths
                )
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
            # ``finally`` above has already run. Re-raise so the manager
            # can transition the task to CANCELLED.
            self._log.debug(
                "Download cancelled: chapter={!r}", chapter.title
            )
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
    ) -> None:
        """Download all pages to *temp_dir*, honouring concurrency and retries.

        Uses :class:`asyncio.TaskGroup` so that a first page failure cancels
        the remaining downloads rather than waiting for them to finish. The
        resulting :class:`BaseExceptionGroup` is unwrapped into either a bare
        :class:`asyncio.CancelledError` (if all failures were cancellations)
        or a :class:`ChapterDownloadError` (otherwise).

        Args:
            task: The task being executed (for ids and events).
            pages: Pages to download, in reading order.
            temp_dir: Destination directory for page files.

        Raises:
            ChapterDownloadError: If at least one page failed to download
                after exhausting retries.
            asyncio.CancelledError: If the task (parent) was cancelled, or
                if every child failure was a cancellation.
        """
        total = len(pages)
        try:
            async with asyncio.TaskGroup() as tg:
                for page in pages:
                    tg.create_task(
                        self._download_one_page(task, page, temp_dir, total),
                        name=f"download-page-{page.index}",
                    )
        except asyncio.CancelledError:
            # Parent task cancelled directly (not a child cancellation).
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
    ) -> None:
        """Download, validate, and report a single page.

        The download is retried according to ``settings.downloader.retry``
        (or the module defaults). The downloaded file is validated by
        :func:`nexusdl.core.image.validator.validate_file` and retried if
        the validator rejects it — HTML / Cloudflare pages masquerading as
        images are the common case.

        Args:
            task: The task being executed.
            page: The page to download.
            temp_dir: Directory to write the page file into.
            total_pages: Total number of pages in the chapter (for progress).

        Raises:
            PageDownloadError: If the page could not be downloaded or
                validated after exhausting retries.
            asyncio.CancelledError: If the task was cancelled.
        """
        dest = temp_dir / self._page_filename(page)

        async with self._semaphore:
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(self._retry_max_attempts()),
                    wait=wait_exponential_jitter(
                        initial=self._retry_initial_delay(),
                        max=self._retry_max_delay(),
                    ),
                    retry=retry_if_exception_type(PageDownloadError),
                    reraise=True,
                ):
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

        The lock guarantees that progress events are emitted in completion
        order, so downstream consumers never see progress go backwards.

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

        Args:
            task: The task being executed (source of ``format`` and metadata).
            pages_with_paths: Each page paired with the file it was written
                to, in reading order.
            output_path: Where the packager should produce its output. For
                ``FOLDER`` this is a directory; for every other format it is
                a file path.

        Returns:
            The path the packager actually produced (the packager may adjust
            the extension).

        Raises:
            PackagingError: If the packager fails. The caller treats this
                like any other download failure.
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

        Fields not exposed by the manga model are left ``None``. This is a
        best-effort projection: packagers embed whatever is present in
        ``ComicInfo.xml``, and readers (Komga / Kavita / Mihon / YACReader)
        handle missing fields gracefully.

        Args:
            task: The task being executed.
            page_count: Number of pages in the packaged output.

        Returns:
            A populated :class:`ComicInfo` instance.
        """
        manga: Manga = task.manga
        chapter: Chapter = task.chapter
        language_iso = _LANGUAGE_ISO_639_1.get(manga.language, "und")
        return ComicInfo(
            title=chapter.title,
            series=manga.title,
            number=str(getattr(chapter, "number", "1")),
            language_iso=language_iso,
            page_count=page_count,
            summary=getattr(manga, "description", None),
            year=getattr(manga, "year", None),
            manga=bool(getattr(manga, "is_manga", True)),
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
            :meth:`_create_temp_dir`, not here.
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

        Idempotent and safe to call from a ``finally`` block: missing or
        already-removed directories are ignored. The operation is
        synchronous — deleting a chapter's worth of pages is bounded to a
        few dozen milliseconds and does not warrant an ``asyncio.to_thread``
        hop during shutdown.

        Args:
            temp_dir: The temporary directory to remove.
        """
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=False)
        except OSError as exc:
            # Cleanup failures must not mask the original outcome.
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
    def _page_filename(page: Page) -> str:
        """Return the on-disk filename for *page*.

        Prefers ``page.filename`` when the model exposes one, otherwise
        derives the extension from the page URL and uses a zero-padded
        1-based index so that CBZ / ComicInfo readers display pages in
        order.

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

        Publishing failures must never abort a download — a subscriber
        exception is the bus's problem, not the worker's.

        Args:
            event_type: Fully qualified event type (e.g.
                ``download.started``).
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

    def _retry_max_attempts(self) -> int:
        """Return the retry attempt count from settings."""
        return int(
            getattr(
                getattr(self._settings.downloader, "retry", None),
                "max_attempts",
                _DEFAULT_RETRY_MAX_ATTEMPTS,
            )
        )

    def _retry_initial_delay(self) -> float:
        """Return the retry initial delay (seconds) from settings."""
        return float(
            getattr(
                getattr(self._settings.downloader, "retry", None),
                "initial_delay",
                _DEFAULT_RETRY_INITIAL_DELAY,
            )
        )

    def _retry_max_delay(self) -> float:
        """Return the retry maximum delay (seconds) from settings."""
        return float(
            getattr(
                getattr(self._settings.downloader, "retry", None),
                "max_delay",
                _DEFAULT_RETRY_MAX_DELAY,
            )
        )
