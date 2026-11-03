"""Textual screen for downloading a manga's chapters.

The download screen is the pipeline's execution surface: the user sees
the chapters of one manga, marks the ones they want, picks a packaging
format, and starts the download. The screen displays the download's
progress in real time via the
:class:`~nexusdl.interfaces.cli.widgets.progress_bar.ProgressBar`
widget, and shows the final result when every submitted chapter
completes.

Composition
-----------
Three regions:

* **Header** — manga title and source-site name.
* **Left pane** — a :class:`~nexusdl.interfaces.cli.widgets.chapter_list.ChapterList`
  populated from the manga's chapters. The user marks chapters with
  ``Space`` and can mark all with ``A``; the download button is enabled
  only when at least one chapter is marked.
* **Right pane** — the format selector, the download and cancel
  buttons, and a :class:`~nexusdl.interfaces.cli.widgets.progress_bar.ProgressBar`
  that renders the download's snapshot.

The chapter fetch happens **once**, on mount, via a Textual worker. The
download itself is another worker, exclusive within its group, so a
rapid sequence of download clicks cannot produce two concurrent batches
against the same manager.

Local tracker
-------------
The screen owns its own
:class:`~nexusdl.core.downloader.progress.ProgressTracker` rather than
consuming a shared one. The isolation matters:

* A user who opens this screen for manga A, starts a download, then
  opens it again for manga B, sees only B's downloads in the second
  screen's progress panel. A shared tracker would show A's downloads
  leaking into B's panel.
* When the screen is popped, its tracker is unsubscribed from the
  event bus and garbage-collected. No state persists across screens.

The subscription lifecycle is handled in ``on_mount`` /
``on_unmount``; the tracker is created in ``__init__`` so the
constructor can pass it to the widget tree without a race.

Submit-then-wait, not ``download_manga``
----------------------------------------
The screen submits each marked chapter as a
:class:`~nexusdl.core.models.download.DownloadTask` via
:meth:`~nexusdl.core.downloader.manager.DownloadManager.submit` rather
than using :meth:`~nexusdl.core.downloader.manager.DownloadManager.download_manga`.
The manager's ``download_manga`` is a **sequential** async generator —
it submits one chapter, waits for it, then submits the next — which
defeats ``max_concurrent_tasks``. Submitting the full batch upfront
lets the manager schedule them with its own concurrency limit, which
is what the TUI wants for a batch download.

Completion detection
--------------------
The worker waits until every task in
:attr:`_download_chapter_ids` reaches a terminal phase in the tracker
(via ``TaskProgress.is_terminal``). The wait loop polls every 500 ms
and calls :meth:`_finish_download` when the count of terminal tasks
equals the count of chapters that were actually submitted (after
``skip_existing`` pruned the ones already on disk).

A download that finishes with one or more failures shows a
**partial-success** status ("X succeeded, Y failed") rather than a
uniform "done" — the user must know which chapters need attention.

Cancel semantics
----------------
"Cancel" is best-effort:

* The download worker (which is waiting for completion) is cancelled
  — the wait stops.
* Every in-progress task in the manager is stopped: a ``PENDING`` task
  is cancelled, a ``RUNNING`` task is paused.
* Every task whose manager state was stopped is marked
  :data:`~nexusdl.core.downloader.progress.PHASE_CANCELLED` in the
  tracker, so the progress display reflects the reality immediately
  rather than waiting for the next poll.

A task that finishes between the moment the user clicks Cancel and the
moment the cancel logic reaches it is left alone (already terminal);
its final result stands. There is no "undo" of a completed chapter.

Format selector
---------------
The format list is the five packagers that actually ship:
``CBZ``, ``CB7``, ``ZIP``, ``PDF``, ``FOLDER``. ``CBT`` is excluded
because no ``CbtPackager`` ships in this release — offering a format
whose packager does not exist would produce a ``PackagingError`` at
download time, which is a worse UX than not offering the option. A
future release that ships ``CBT`` (or a plugin that registers one via
:meth:`~nexusdl.core.packaging.factory.PackagerFactory.register`) would
extend this list here.

The default value is ``settings.downloader.default_format`` — the same
default the manager would apply if the caller did not specify one.

Localisation
------------
The screen introduces fifteen keys under ``download.*``:

* ``download.header`` — the header line, with ``{title}`` and ``{site}``
  placeholders.
* ``download.fetch.loading`` — while the chapter list is being fetched.
* ``download.fetch.error`` — the fetch failure, with ``{error}``.
* ``download.fetch.empty`` — the "this manga has no chapters" message.
* ``download.chapters.count_one`` / ``.count_other`` — the marked
  chapter count, with ``{count}``.
* ``download.format.label`` — the format selector's label.
* ``download.button.start`` — the download button's label.
* ``download.button.cancel`` — the cancel button's label.
* ``download.status.idle`` — before any download starts.
* ``download.status.downloading`` — during a download, with ``{active}``.
* ``download.status.done`` — every submitted chapter succeeded.
* ``download.status.done_with_failures`` — some failed, with
  ``{success}`` and ``{failed}``.
* ``download.status.cancelled`` — after a user-triggered cancel.
* ``download.status.error`` — an unexpected failure, with ``{error}``.

A missing key renders the key verbatim, matching the contract every
other widget and screen in this package follows. See
:mod:`nexusdl.core.i18n`.

Dependency graph (no cycles)::

    nexusdl.core.downloader.manager             ─┐
    nexusdl.core.downloader.progress             │
    nexusdl.core.events                          │
    nexusdl.core.i18n                            ├──► interfaces.cli.screens.download
    nexusdl.core.models.*                        │
    nexusdl.core.paths                           │
    nexusdl.core.registry.site_registry          │
    nexusdl.core.session.http_session            │
    nexusdl.interfaces.cli.widgets.chapter_list  │
    nexusdl.interfaces.cli.widgets.progress_bar  │
    textual                                      │
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Label, Select, Static
from textual.worker import Worker

from nexusdl.core.downloader.progress import (
    PHASE_CANCELLED,
    ProgressSnapshot,
    ProgressTracker,
    TaskProgress,
)
from nexusdl.core.i18n import Translator, get_default_translator
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.download import DownloadTask
from nexusdl.core.models.enums import DownloadStatus, PackagingFormat
from nexusdl.core.paths import chapter_output_path, manga_dir
from nexusdl.interfaces.cli.widgets.chapter_list import ChapterList
from nexusdl.interfaces.cli.widgets.progress_bar import ProgressBar

if TYPE_CHECKING:
    from nexusdl.core.config import Settings
    from nexusdl.core.downloader.manager import DownloadManager
    from nexusdl.core.events import EventBus
    from nexusdl.core.models.manga import Manga
    from nexusdl.core.registry.site_registry import SiteRegistry
    from nexusdl.core.session.http_session import HttpSession


__all__ = ["DownloadScreen"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: The five packaging formats that ship a packager in this release. CBT
#: is deliberately absent — see the module docstring's "Format selector"
#: section.
_SHIPPED_FORMATS: Final[tuple[PackagingFormat, ...]] = (
    PackagingFormat.CBZ,
    PackagingFormat.CB7,
    PackagingFormat.ZIP,
    PackagingFormat.PDF,
    PackagingFormat.FOLDER,
)

#: Poll interval for the completion-detection loop inside the download
#: worker. 500 ms is fast enough that the user sees the "done" status
#: promptly after the last chapter completes, slow enough that the loop
#: does not contend with the display timer's own polling.
_COMPLETION_POLL_INTERVAL: Final[float] = 0.5


# --------------------------------------------------------------------------- #
# DownloadScreen                                                              #
# --------------------------------------------------------------------------- #


class DownloadScreen(Screen[None]):
    """A Textual screen for selecting and downloading a manga's chapters.

    See the module docstring for the design contract, the local tracker
    policy, the submit-then-wait orchestration, and the cancel
    semantics.

    Args:
        manga: The manga whose chapters to display. Must already be a
            **fully-fetched** manga, not a search-result stub — see
            :meth:`~nexusdl.interfaces.cli.screens.search.SearchScreen.SearchResultSelected`'s
            docstring for the distinction.
        registry: The loaded site registry.
        session: The HTTP session passed to the parser for the initial
            chapter fetch.
        manager: The download manager that orchestrates every download
            this screen submits.
        event_bus: The shared event bus. The screen subscribes its
            local tracker to every event type the tracker consumes.
        settings: Application settings. Read for the default packaging
            format and the ``skip_existing`` policy.
        translator: The localisation service. When ``None`` (the
            default), the process-wide default translator is used.
        name: Optional Textual screen name.
        id: Optional Textual screen id.
        classes: Optional Textual CSS classes.
    """

    #: ``Escape`` and ``q`` pop the screen. If a download is in progress,
    #: the action asks for confirmation before popping — a user who
    #: accidentally hits ``Escape`` mid-download should not lose their
    #: view of the running batch.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
    ]

    #: Default CSS.
    DEFAULT_CSS: ClassVar[str] = """
    DownloadScreen {
        background: $surface;
    }
    #download-header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $panel;
        text-style: bold;
    }
    #download-chapters-pane {
        width: 3fr;
        height: 1fr;
    }
    #download-progress-pane {
        width: 2fr;
        height: 1fr;
        padding: 0 1;
    }
    #download-controls {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }
    #download-format {
        width: 20;
        margin-right: 1;
    }
    #download-start, #download-cancel {
        width: auto;
        min-width: 12;
        margin-right: 1;
    }
    #download-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        manga: Manga,
        registry: SiteRegistry,
        session: HttpSession,
        manager: DownloadManager,
        event_bus: EventBus,
        settings: Settings,
        translator: Translator | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._manga = manga
        self._registry = registry
        self._session = session
        self._manager = manager
        self._event_bus = event_bus
        self._settings = settings
        self._translator = (
            translator if translator is not None else get_default_translator()
        )

        #: The screen's own tracker. Subscribed in ``on_mount``,
        #: unsubscribed in ``on_unmount``.
        self._tracker = ProgressTracker()

        #: The chapter ids the current download batch has submitted
        #: (after ``skip_existing`` pruning). Empty before the first
        #: download starts, empty again after :meth:`_finish_download`.
        self._download_chapter_ids: set[str] = set()

        #: Whether a download is currently in progress. Guards the
        #: completion check in the worker's poll loop and the toggle
        #: state of the two control buttons.
        self._download_in_progress: bool = False

        #: The currently running download worker, or ``None`` between
        #: downloads. Stored so :meth:`_cancel_download` can cancel the
        #: worker that is waiting for completion.
        self._download_worker: Worker[None] | None = None

        #: The format the next download will produce. Seeded from
        #: ``settings.downloader.default_format``.
        self._current_format: PackagingFormat = (
            settings.downloader.default_format
        )

    # ------------------------------------------------------------------ #
    # Compose                                                            #
    # ------------------------------------------------------------------ #

    def compose(self) -> ComposeResult:
        """Yield the header, the two panes, the controls, and the status line."""
        yield Static("", id="download-header")
        with Horizontal():
            with Vertical(id="download-chapters-pane"):
                yield ChapterList([], id="download-chapter-list")
            with Vertical(id="download-progress-pane"):
                yield ProgressBar(id="download-progress")
                with Horizontal(id="download-controls"):
                    yield Select(
                        options=[
                            (fmt.value.upper(), fmt.value)
                            for fmt in _SHIPPED_FORMATS
                        ],
                        value=self._current_format.value,
                        allow_blank=False,
                        id="download-format",
                    )
                    yield Button(
                        self._translator.translate(
                            "download.button.start"
                        ),
                        id="download-start",
                        variant="success",
                        disabled=True,
                    )
                    yield Button(
                        self._translator.translate(
                            "download.button.cancel"
                        ),
                        id="download-cancel",
                        variant="error",
                        disabled=True,
                    )
        yield Label("", id="download-status")

    def on_mount(self) -> None:
        """Wire the tracker, update the header, and fetch chapters."""
        for event_type in ProgressTracker.subscribed_event_types():
            self._event_bus.subscribe(event_type, self._tracker.handle_event)

        self._update_header()
        self._set_status(
            self._translator.translate("download.fetch.loading")
        )
        self.set_interval(0.2, self._poll_tracker)
        self._fetch_chapters()

    def on_unmount(self) -> None:
        """Unsubscribe the tracker from the bus.

        Called automatically by Textual when the screen is popped. The
        subscription is bound to ``self._tracker.handle_event`` — a
        bound method whose equality is by instance and function, so
        ``unsubscribe`` correctly removes it.
        """
        for event_type in ProgressTracker.subscribed_event_types():
            self._event_bus.unsubscribe(event_type, self._tracker.handle_event)

    # ------------------------------------------------------------------ #
    # Chapter fetch                                                      #
    # ------------------------------------------------------------------ #

    @work(exclusive=True, group="fetch")
    async def _fetch_chapters(self) -> None:
        """Fetch the manga's chapters and populate the chapter list.

        Runs once on mount. A fetch failure is surfaced in the status
        line; the chapter list remains empty and the download button
        stays disabled.
        """
        try:
            parser = self._registry.get_parser(
                self._manga.site_id, self._session
            )
            chapters = await parser.get_chapters(self._manga)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced via status line
            self._set_status(
                self._translator.translate(
                    "download.fetch.error", error=str(exc)
                )
            )
            return

        chapter_list = self.query_one(ChapterList)
        chapter_list.set_chapters(chapters)

        if not chapters:
            self._set_status(
                self._translator.translate("download.fetch.empty")
            )
            return

        self._set_status(
            self._translator.plural(
                "download.chapters.count", len(chapters)
            )
        )

    # ------------------------------------------------------------------ #
    # Download orchestration                                             #
    # ------------------------------------------------------------------ #

    def _start_download(self) -> None:
        """Kick off the download worker for the currently marked chapters.

        Guarded against a second click while a download is in progress.
        The worker is stored so :meth:`_cancel_download` can cancel it.
        """
        if self._download_in_progress:
            return
        self._download_worker = self._run_download()

    @work(exclusive=True, group="download")
    async def _run_download(self) -> None:
        """Submit every marked chapter and wait for the batch to complete.

        The submission path uses
        :meth:`~nexusdl.core.downloader.manager.DownloadManager.submit`
        directly (not ``download_manga``) so the manager's concurrency
        limit applies to the whole batch. See the module docstring's
        "Submit-then-wait" section.

        Waits by polling the tracker every 500 ms until every submitted
        chapter's task is terminal. A ``CancelledError`` (either from
        the user's cancel action or from Textual's teardown) propagates
        after resetting the in-progress flag.
        """
        chapter_list = self.query_one(ChapterList)
        selected = chapter_list.selected_chapters
        if not selected:
            return

        to_submit = self._prune_already_downloaded(selected)
        if not to_submit:
            self._set_status(
                self._translator.translate("download.status.idle")
            )
            return

        self._download_chapter_ids = {c.id for c in to_submit}
        self._download_in_progress = True
        self._update_controls()
        self._set_status(
            self._translator.translate(
                "download.status.downloading", active=len(to_submit)
            )
        )

        try:
            for chapter in to_submit:
                task = DownloadTask(
                    manga=self._manga,
                    chapter=chapter,
                    format=self._current_format,
                )
                await self._manager.submit(task)

            # Wait for every submitted chapter's task to reach a terminal
            # phase. Poll interval matches _COMPLETION_POLL_INTERVAL.
            while True:
                await asyncio.sleep(_COMPLETION_POLL_INTERVAL)
                snapshot = await self._tracker.snapshot()
                tasks = self._tasks_for_snapshot(snapshot)
                if (
                    len(tasks) == len(self._download_chapter_ids)
                    and all(t.is_terminal for t in tasks)
                ):
                    self._finish_download(tasks)
                    return
        except asyncio.CancelledError:
            self._download_in_progress = False
            self._update_controls()
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced via status line
            self._set_status(
                self._translator.translate(
                    "download.status.error", error=str(exc)
                )
            )
            self._download_in_progress = False
            self._update_controls()
            return

    def _prune_already_downloaded(
        self, chapters: tuple[Chapter, ...]
    ) -> list[Chapter]:
        """Filter out chapters whose output file already exists.

        Honours ``settings.downloader.skip_existing``. Uses
        :func:`~nexusdl.core.paths.chapter_output_path` for the path
        lookup so the check matches what the worker would produce.

        Args:
            chapters: The chapters the user marked.

        Returns:
            The subset that still needs downloading, in the input order.
        """
        if not self._settings.downloader.skip_existing:
            return list(chapters)

        base = manga_dir(
            Path(self._settings.storage.downloads_dir), self._manga.title
        )
        keep: list[Chapter] = []
        for chapter in chapters:
            expected = chapter_output_path(
                base, chapter.title, self._current_format
            )
            if not expected.exists():
                keep.append(chapter)
        return keep

    def _finish_download(self, tasks: tuple[TaskProgress, ...]) -> None:
        """Show the final status for a completed batch and reset state."""
        self._download_in_progress = False
        self._update_controls()

        success = sum(1 for t in tasks if t.phase != "failed")
        failed = sum(1 for t in tasks if t.phase == "failed")

        if failed == 0:
            self._set_status(
                self._translator.translate("download.status.done")
            )
        else:
            self._set_status(
                self._translator.translate(
                    "download.status.done_with_failures",
                    success=success,
                    failed=failed,
                )
            )

        self._download_chapter_ids = set()

    def _cancel_download(self) -> None:
        """Stop the download worker and cancel/pause in-flight tasks.

        Best-effort: the worker's wait is cancelled first (so the
        display stops polling for completion), then a separate worker
        walks the current snapshot and stops every non-terminal task
        via the manager's ``cancel`` (pending) or ``pause`` (running)
        APIs. Each stopped task is marked cancelled in the tracker so
        the progress bar updates immediately.

        A task that has already finished — or that finishes between the
        snapshot and the cancel loop — is left alone. See the module
        docstring's "Cancel semantics" section.
        """
        if not self._download_in_progress:
            return
        if self._download_worker is not None and not self._download_worker.is_done:
            self._download_worker.cancel()
        self._do_cancel_tasks()

    @work(exclusive=True, group="cancel")
    async def _do_cancel_tasks(self) -> None:
        """Cancel pending tasks and pause running ones for the batch."""
        snapshot = await self._tracker.snapshot()
        tasks = self._tasks_for_snapshot(snapshot)
        for task in tasks:
            if task.is_terminal:
                continue
            try:
                live = await self._manager.get_task(task.task_id)
                if live is None:
                    continue
                if live.status is DownloadStatus.PENDING:
                    await self._manager.cancel(task.task_id)
                elif live.status is DownloadStatus.RUNNING:
                    await self._manager.pause(task.task_id)
                await self._tracker.cancel_task(task.task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — best-effort, log via status
                continue

        self._download_in_progress = False
        self._update_controls()
        self._set_status(
            self._translator.translate("download.status.cancelled")
        )

    # ------------------------------------------------------------------ #
    # Event handlers                                                     #
    # ------------------------------------------------------------------ #

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route button presses to the corresponding action."""
        if event.button.id == "download-start":
            self._start_download()
        elif event.button.id == "download-cancel":
            self._cancel_download()

    def on_select_changed(self, event: Select.Changed) -> None:
        """Track the format selector's current value."""
        if event.select.id != "download-format":
            return
        if not isinstance(event.value, str):
            return
        try:
            self._current_format = PackagingFormat(event.value)
        except ValueError:
            return

    def on_chapter_list_selection_changed(
        self, event: ChapterList.SelectionChanged
    ) -> None:
        """Update the download button's enablement and the status line."""
        self._update_controls()
        count = len(event.chapters)
        if count == 0 or self._download_in_progress:
            return
        self._set_status(
            self._translator.plural(
                "download.chapters.count", count
            )
        )

    def action_back(self) -> None:
        """Pop the screen, cancelling an in-progress download first."""
        if self._download_in_progress:
            self._cancel_download()
        self.app.pop_screen()

    # ------------------------------------------------------------------ #
    # Poll and rendering helpers                                         #
    # ------------------------------------------------------------------ #

    async def _poll_tracker(self) -> None:
        """Render the current tracker state into the ProgressBar.

        Runs every 200 ms via ``set_interval``. When no download has
        started yet, the filtered snapshot is empty and the widget
        shows its empty state.
        """
        snapshot = await self._tracker.snapshot()
        tasks = self._tasks_for_snapshot(snapshot)
        filtered = ProgressSnapshot(
            tasks=tasks, captured_at=snapshot.captured_at
        )
        self.query_one(ProgressBar).update_snapshot(filtered)

    def _tasks_for_snapshot(
        self, snapshot: ProgressSnapshot
    ) -> tuple[TaskProgress, ...]:
        """Return only the tasks whose chapter is in the current batch.

        When no batch has been submitted yet (or the previous batch has
        fully completed and :attr:`_download_chapter_ids` was reset),
        returns an empty tuple.
        """
        if not self._download_chapter_ids:
            return ()
        return tuple(
            task
            for task in snapshot.tasks
            if task.chapter_id in self._download_chapter_ids
        )

    def _update_header(self) -> None:
        """Refresh the header line with the manga title and site name."""
        site = self._registry.get_site(self._manga.site_id)
        site_name = site.name if site is not None else self._manga.site_id
        self.query_one("#download-header", Static).update(
            self._translator.translate(
                "download.header",
                title=self._manga.title,
                site=site_name,
            )
        )

    def _update_controls(self) -> None:
        """Enable or disable the buttons based on current state."""
        chapter_list = self.query_one(ChapterList)
        has_selection = len(chapter_list.selected_chapters) > 0
        start_btn = self.query_one("#download-start", Button)
        cancel_btn = self.query_one("#download-cancel", Button)
        start_btn.disabled = self._download_in_progress or not has_selection
        cancel_btn.disabled = not self._download_in_progress

    def _set_status(self, message: str) -> None:
        """Replace the status line with *message*."""
        self.query_one("#download-status", Label).update(message)
