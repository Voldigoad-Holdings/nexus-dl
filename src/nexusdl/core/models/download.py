"""Download domain models — a task in flight and the outcome of a task.

Two value objects compose the entire download pipeline from the outside:

* :class:`DownloadTask` — the **request**: what to download, from which
  site, in which format, at which priority. Created by the manager from a
  :class:`~nexusdl.core.models.manga.Manga` /
  :class:`~nexusdl.core.models.chapter.Chapter` pair, submitted to the
  :class:`~nexusdl.core.downloader.queue.DownloadQueue`, and eventually
  executed by a :class:`~nexusdl.core.downloader.worker.DownloadWorker`.
  Carries the only **mutable** field in the model layer — :attr:`status` —
  whose transitions are driven exclusively by the queue.
* :class:`DownloadResult` — the **outcome**: the terminal snapshot a worker
  (or the manager, when synthesising a terminal result after a
  cancellation or shutdown) produces. Immutable, complete, and
  self-describing.

Identity
--------
:attr:`DownloadTask.id` is a **UUID4**, not a truncated sha256 like every
other id in the pipeline. This is deliberate and matches the PROMPT
MASTER's ID table:

* Mangas and chapters are persistent entities — their ids must be stable
  across runs and derive deterministically from ``(site, source)`` pairs so
  that re-imports upsert instead of duplicating. A deterministic hash is
  the correct choice.
* A download task is **ephemeral** — it is never persisted, it disappears
  when the process exits, and re-submitting the same manga/chapter
  legitimately produces a *different* task (the user may submit the same
  chapter twice on purpose to force a re-download). A UUID4 gives
  collision-free uniqueness at zero cost.

:attr:`DownloadResult.task_id` is a :class:`uuid.UUID` matching
:attr:`DownloadTask.id`, so a result can always be traced back to its
originating task by construction.

Mutability
----------
:class:`DownloadTask` is a **slotted dataclass with a guarded
``__setattr__``**: every field is write-once at construction time, with a
single documented exception — :attr:`DownloadTask.status` may be reassigned
at any point. This matches the contract used by
:meth:`~nexusdl.core.downloader.queue.DownloadQueue._transition`, which
mutates ``task.status`` directly after validating the transition against
its state-machine table.

Every other mutation attempt raises :class:`AttributeError` at the call
site — a bug in orchestrating code that tries to reassign ``task.manga``
after the task has been dispatched surfaces immediately, not via a
subtle mismatch later.

:class:`DownloadResult` is fully frozen. It has no mutable phase: it is
created once, when a task terminates.

Terminal state of a result
--------------------------
A :class:`DownloadResult` always carries a **terminal**
:class:`~nexusdl.core.models.enums.DownloadStatus`:

* :attr:`~DownloadStatus.DONE` — the worker succeeded. ``output_path``
  must be set; ``error`` must be ``None``.
* :attr:`~DownloadStatus.FAILED` — the worker failed. ``error`` must be
  set; ``output_path`` must be ``None``.
* :attr:`~DownloadStatus.CANCELLED` — the task was cancelled before it
  finished (manager shutdown, generator aborted). ``error`` **may** be
  set (an explanatory ``NexusDLError``) or ``None``; ``output_path``
  must be ``None``.

Any other status raises :class:`ValueError` at construction. This
invariant is enforced because both the manager and every API consumer
rely on it: a ``DownloadResult`` with ``status == RUNNING`` would be a
nonsense value that a UI cannot render.

Dependency graph (no cycles)::

    core.models.chapter    ─┐
    core.models.enums       ├──► core.models.download
    core.models.manga      ─┘

No ``parsers/``, no ``plugins/``, no I/O, no logging. Both classes could
be lifted into a standalone library together with their three imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import (
    DownloadStatus,
    PackagingFormat,
    Priority,
)
from nexusdl.core.models.manga import Manga


__all__ = ["DownloadResult", "DownloadTask"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Statuses a :class:`DownloadResult` may carry. Restricted to terminal
#: values — see the module docstring for the per-status invariants on
#: ``output_path`` and ``error``.
_TERMINAL_STATUSES: Final[frozenset[DownloadStatus]] = frozenset(
    {
        DownloadStatus.DONE,
        DownloadStatus.FAILED,
        DownloadStatus.CANCELLED,
    }
)


# --------------------------------------------------------------------------- #
# DownloadTask                                                                #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DownloadTask:
    """A single unit of download work: one chapter of one manga.

    Mutable **only** on :attr:`status`. Every other field is write-once at
    construction time and rejected on any later reassignment attempt. This
    asymmetry is enforced by :meth:`__setattr__`; a bug in orchestrating
    code that tries to reassign ``task.manga`` after the task has been
    dispatched fails loudly at the call site.

    Not frozen as a whole because the queue must drive
    ``PENDING → RUNNING → DONE|PAUSED|FAILED`` and
    ``PENDING → CANCELLED`` in place. See
    :data:`nexusdl.core.downloader.queue._VALID_TRANSITIONS` for the
    canonical transition table.

    Attributes:
        id: Stable identifier of the task, a fresh UUID4 generated by the
            default factory. Never reassigned. Stringified (via
            :func:`str`) by the manager when stored in event payloads and
            future maps.
        manga: The manga the chapter belongs to. The task holds a direct
            reference — not an id — so the worker, the plugins, and the
            event emitters can read metadata without a database round-trip.
        chapter: The chapter to download. Must belong to :attr:`manga`;
            this invariant is checked at construction time.
        format: The desired output format. One of
            :class:`~nexusdl.core.models.enums.PackagingFormat`'s members.
        priority: Scheduling priority. Used by
            :class:`~nexusdl.core.downloader.queue.DownloadQueue` to order
            pending tasks. Defaults to
            :attr:`~nexusdl.core.models.enums.Priority.NORMAL`.
        status: Lifecycle state of the task. **The only mutable field.**
            Initialised to
            :attr:`~nexusdl.core.models.enums.DownloadStatus.PENDING` and
            transitioned exclusively by the queue. Reassignment to any
            value that is not a :class:`DownloadStatus` member raises
            :class:`TypeError`.
        created_at: Timezone-aware UTC timestamp of construction. Used for
            diagnostics; a future scheduling tweak could also use it as a
            FIFO tie-break. Never reassigned.
    """

    id: UUID = field(default_factory=uuid4)
    manga: Manga = field(default=None)  # type: ignore[assignment]
    chapter: Chapter = field(default=None)  # type: ignore[assignment]
    format: PackagingFormat = field(default=None)  # type: ignore[assignment]
    priority: Priority = Priority.NORMAL
    status: DownloadStatus = DownloadStatus.PENDING
    created_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )

    # ------------------------------------------------------------------ #
    # Construction                                                       #
    # ------------------------------------------------------------------ #

    def __post_init__(self) -> None:
        """Validate all required fields and the manga↔chapter invariant.

        Raises:
            TypeError: If any required field is missing (dataclass ``field``
                defaulted to ``None`` but never supplied by the caller), or
                has the wrong Python type.
            ValueError: If :attr:`chapter` does not belong to
                :attr:`manga`, or :attr:`created_at` is naive.
        """
        # ``field(default=None)`` is used above so that ``manga`` /
        # ``chapter`` / ``format`` remain required from the caller's point
        # of view — passing ``None`` explicitly is caught here rather than
        # silently accepted.
        if self.manga is None:
            raise TypeError("DownloadTask.manga is required")
        if self.chapter is None:
            raise TypeError("DownloadTask.chapter is required")
        if self.format is None:
            raise TypeError("DownloadTask.format is required")

        if not isinstance(self.id, UUID):
            raise TypeError(
                f"DownloadTask.id must be a UUID, got "
                f"{type(self.id).__name__}"
            )
        if not isinstance(self.manga, Manga):
            raise TypeError(
                f"DownloadTask.manga must be a Manga, got "
                f"{type(self.manga).__name__}"
            )
        if not isinstance(self.chapter, Chapter):
            raise TypeError(
                f"DownloadTask.chapter must be a Chapter, got "
                f"{type(self.chapter).__name__}"
            )
        if not isinstance(self.format, PackagingFormat):
            raise TypeError(
                f"DownloadTask.format must be a PackagingFormat, got "
                f"{type(self.format).__name__}"
            )
        if not isinstance(self.priority, Priority):
            raise TypeError(
                f"DownloadTask.priority must be a Priority, got "
                f"{type(self.priority).__name__}"
            )
        if not isinstance(self.status, DownloadStatus):
            raise TypeError(
                f"DownloadTask.status must be a DownloadStatus, got "
                f"{type(self.status).__name__}"
            )
        if not isinstance(self.created_at, datetime):
            raise TypeError(
                f"DownloadTask.created_at must be a datetime, got "
                f"{type(self.created_at).__name__}"
            )
        if self.created_at.tzinfo is None:
            raise ValueError(
                "DownloadTask.created_at must be timezone-aware"
            )

        # The chapter must belong to the manga the task declares. A
        # mismatch would let the worker download one chapter while the
        # events and the plugin pipeline refer to a different manga.
        if self.chapter.manga_id != self.manga.id:
            raise ValueError(
                f"DownloadTask.chapter belongs to manga "
                f"{self.chapter.manga_id!r}, but the task's manga is "
                f"{self.manga.id!r}"
            )

    # ------------------------------------------------------------------ #
    # Field immutability                                                 #
    # ------------------------------------------------------------------ #

    def __setattr__(self, name: str, value: Any) -> None:
        """Allow only :attr:`status` to be reassigned after construction.

        During :meth:`__init__`, every slot is still unset when its value
        is first assigned — ``hasattr`` returns ``False`` — so the
        ``object.__setattr__`` fallback runs normally and the dataclass
        constructor works untouched.

        After :meth:`__post_init__` has run, every field is populated, so
        ``hasattr`` returns ``True`` for anything other than :attr:`status`
        and the guard raises :class:`AttributeError`.

        :attr:`status` is special-cased **before** the ``hasattr`` check,
        so the queue can transition tasks in place. The value is
        type-checked here so that a stray ``task.status = "RUNNING"``
        (a string, not a
        :class:`~nexusdl.core.models.enums.DownloadStatus` member) fails
        fast with a clear message rather than silently corrupting the
        queue's invariants.

        Args:
            name: Attribute name.
            value: Value to set.

        Raises:
            TypeError: If *name* is ``"status"`` and *value* is not a
                :class:`~nexusdl.core.models.enums.DownloadStatus`.
            AttributeError: If *name* refers to a field already set.
        """
        if name == "status":
            if not isinstance(value, DownloadStatus):
                raise TypeError(
                    f"DownloadTask.status must be a DownloadStatus, got "
                    f"{type(value).__name__}"
                )
            object.__setattr__(self, name, value)
            return
        if hasattr(self, name):
            raise AttributeError(
                f"DownloadTask.{name} is immutable after construction"
            )
        object.__setattr__(self, name, value)

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a concise debug representation.

        Full manga/chapter objects are summarised to id + title, keeping
        the repr usable in logs without exploding to hundreds of
        characters.
        """
        return (
            f"DownloadTask(id={self.id!s}, "
            f"manga={self.manga.title!r} ({self.manga.id}), "
            f"chapter={self.chapter.title!r} ({self.chapter.id}), "
            f"format={self.format.value!r}, "
            f"priority={self.priority.name}, "
            f"status={self.status.value!r})"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the task.

        Rendered by the ``GET /api/v1/download/{task_id}`` and
        ``GET /api/v1/download`` endpoints. Includes the manga and chapter
        **titles** alongside their ids so the response is self-contained —
        a caller rendering a list of tasks does not need a second lookup
        per task.

        Timestamps are ISO 8601 strings. Enums are serialised to their
        ``.value`` — :attr:`Priority` by ``.name`` because its numeric
        value is not meaningful to a human reader.

        Returns:
            A dict with all public fields.
        """
        return {
            "id": str(self.id),
            "manga_id": self.manga.id,
            "manga_title": self.manga.title,
            "chapter_id": self.chapter.id,
            "chapter_title": self.chapter.title,
            "format": self.format.value,
            "priority": self.priority.name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }


# --------------------------------------------------------------------------- #
# DownloadResult                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Terminal outcome of a download task.

    Immutable. Created exactly once per task, at the moment the task
    reaches a terminal state (:attr:`DONE`, :attr:`FAILED`, or
    :attr:`CANCELLED`). The invariants relating :attr:`status`,
    :attr:`output_path`, and :attr:`error` are enforced at construction
    time — a ``DownloadResult`` that says ``DONE`` but has no output path
    cannot be built.

    Both the worker (which produces ``DONE`` / ``FAILED``) and the
    manager (which synthesises ``CANCELLED`` results during shutdown or
    generator abort) create instances of this class. They pass through
    :meth:`~nexusdl.plugins.loader.PluginLoader.dispatch_on_after_download`
    unchanged (a plugin that wants to transform the result returns a new
    :class:`DownloadResult`, not a mutated one).

    Attributes:
        task_id: The :attr:`DownloadTask.id` this result belongs to.
        status: Terminal status — one of
            :attr:`~nexusdl.core.models.enums.DownloadStatus.DONE`,
            :attr:`~DownloadStatus.FAILED`, or
            :attr:`~DownloadStatus.CANCELLED`. Any other value raises
            :class:`ValueError`.
        output_path: Final on-disk location of the packaged artifact.
            Required and non-``None`` for :attr:`DONE`; must be ``None``
            for :attr:`FAILED` and :attr:`CANCELLED`.
        pages_downloaded: Number of pages that were downloaded and
            written. ``0`` on failure before any page completed; the
            actual count for ``DONE``. Always ``>= 0``.
        bytes_downloaded: Total bytes downloaded across all pages, or
            ``0`` when no pages were fetched. Always ``>= 0``.
        duration_seconds: Wall-clock duration of the task, in seconds.
            Always ``>= 0``.
        error: The exception that caused the failure. Required for
            :attr:`FAILED`; may be set or ``None`` for
            :attr:`CANCELLED`; must be ``None`` for :attr:`DONE`.
        completed_at: UTC timestamp of when the result was produced.
            Timezone-aware.
    """

    task_id: UUID
    status: DownloadStatus
    output_path: Path | None
    pages_downloaded: int
    bytes_downloaded: int
    duration_seconds: float
    error: Exception | None
    completed_at: datetime

    def __post_init__(self) -> None:
        """Validate types and per-status invariants.

        Raises:
            TypeError: On wrong Python types (e.g. ``pages_downloaded``
                bool, ``status`` string).
            ValueError: On invalid status, negative counters or duration,
                naive timestamp, or an (``output_path``, ``error``)
                combination inconsistent with :attr:`status`.
        """
        if not isinstance(self.task_id, UUID):
            raise TypeError(
                f"DownloadResult.task_id must be a UUID, got "
                f"{type(self.task_id).__name__}"
            )
        if not isinstance(self.status, DownloadStatus):
            raise TypeError(
                f"DownloadResult.status must be a DownloadStatus, got "
                f"{type(self.status).__name__}"
            )
        if self.status not in _TERMINAL_STATUSES:
            raise ValueError(
                f"DownloadResult.status must be terminal "
                f"({sorted(s.value for s in _TERMINAL_STATUSES)}), "
                f"got {self.status.value!r}"
            )

        for field_name, value in (
            ("pages_downloaded", self.pages_downloaded),
            ("bytes_downloaded", self.bytes_downloaded),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"DownloadResult.{field_name} must be an int, got "
                    f"{type(value).__name__}"
                )
            if value < 0:
                raise ValueError(
                    f"DownloadResult.{field_name} must be >= 0, "
                    f"got {value}"
                )

        if not isinstance(self.duration_seconds, (int, float)) or isinstance(
            self.duration_seconds, bool
        ):
            raise TypeError(
                f"DownloadResult.duration_seconds must be a number, got "
                f"{type(self.duration_seconds).__name__}"
            )
        if self.duration_seconds < 0:
            raise ValueError(
                f"DownloadResult.duration_seconds must be >= 0, "
                f"got {self.duration_seconds}"
            )

        if not isinstance(self.completed_at, datetime):
            raise TypeError(
                f"DownloadResult.completed_at must be a datetime, got "
                f"{type(self.completed_at).__name__}"
            )
        if self.completed_at.tzinfo is None:
            raise ValueError(
                "DownloadResult.completed_at must be timezone-aware"
            )

        if self.output_path is not None and not isinstance(
            self.output_path, Path
        ):
            raise TypeError(
                f"DownloadResult.output_path must be a Path or None, got "
                f"{type(self.output_path).__name__}"
            )

        if self.error is not None and not isinstance(self.error, Exception):
            raise TypeError(
                f"DownloadResult.error must be an Exception or None, got "
                f"{type(self.error).__name__}"
            )

        # Per-status invariants.
        if self.status is DownloadStatus.DONE:
            if self.output_path is None:
                raise ValueError(
                    "DownloadResult with status=DONE must have "
                    "output_path set"
                )
            if self.error is not None:
                raise ValueError(
                    "DownloadResult with status=DONE must have error=None"
                )
        elif self.status is DownloadStatus.FAILED:
            if self.error is None:
                raise ValueError(
                    "DownloadResult with status=FAILED must have error set"
                )
            if self.output_path is not None:
                raise ValueError(
                    "DownloadResult with status=FAILED must have "
                    "output_path=None"
                )
        else:  # CANCELLED
            if self.output_path is not None:
                raise ValueError(
                    "DownloadResult with status=CANCELLED must have "
                    "output_path=None"
                )
            # error may be set (explanatory) or None — no constraint.

    # ------------------------------------------------------------------ #
    # Convenience                                                        #
    # ------------------------------------------------------------------ #

    @property
    def is_success(self) -> bool:
        """``True`` iff :attr:`status` is :attr:`DownloadStatus.DONE`."""
        return self.status is DownloadStatus.DONE

    @property
    def is_failure(self) -> bool:
        """``True`` iff :attr:`status` is :attr:`DownloadStatus.FAILED`."""
        return self.status is DownloadStatus.FAILED

    @property
    def is_cancelled(self) -> bool:
        """``True`` iff :attr:`status` is :attr:`DownloadStatus.CANCELLED`."""
        return self.status is DownloadStatus.CANCELLED

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a concise debug representation.

        The error, when present, is summarised to its type name and message
        — the full traceback belongs in the worker's log, not in the repr
        of the result object.
        """
        error_repr = (
            f"{type(self.error).__name__}({self.error})"
            if self.error is not None
            else "None"
        )
        return (
            f"DownloadResult(task_id={self.task_id!s}, "
            f"status={self.status.value!r}, "
            f"pages={self.pages_downloaded}, "
            f"bytes={self.bytes_downloaded}, "
            f"duration={self.duration_seconds:.2f}s, "
            f"error={error_repr})"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the result.

        The :attr:`error`, when present, is rendered as a
        ``{"type": ..., "message": ...}`` object rather than an exception
        instance, so the output is directly consumable by a FastAPI handler
        and renders readably in a UI.

        Returns:
            A dict with ``task_id`` (string), ``status``, ``output_path``
            (string or ``None``), ``pages_downloaded``,
            ``bytes_downloaded``, ``duration_seconds``, ``error``
            (nested dict or ``None``), and ``completed_at`` (ISO 8601).
        """
        return {
            "task_id": str(self.task_id),
            "status": self.status.value,
            "output_path": (
                str(self.output_path)
                if self.output_path is not None
                else None
            ),
            "pages_downloaded": self.pages_downloaded,
            "bytes_downloaded": self.bytes_downloaded,
            "duration_seconds": self.duration_seconds,
            "error": (
                {
                    "type": type(self.error).__name__,
                    "message": str(self.error),
                }
                if self.error is not None
                else None
            ),
            "completed_at": self.completed_at.isoformat(),
        }
