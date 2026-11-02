"""Walk a directory tree, extract ComicInfo metadata, and populate the library.

The scanner is the importer: it turns a directory full of archive files
(``.cbz`` / ``.cb7`` / ``.cbt``) into database rows. It performs four
steps, each delegated to a collaborator:

1. **Discovery** — recursively walk a root directory, collecting archive
   files. No symlink following; deterministic sort order.
2. **Extraction** — for each file, read its ``ComicInfo.xml`` via
   :func:`nexusdl.core.library.metadata.extract_metadata`, in parallel
   bounded by :attr:`LibraryScanner._semaphore`.
3. **Grouping** — files sharing a parent directory are grouped into a
   single :class:`~nexusdl.core.models.manga.Manga`; each file becomes one
   :class:`~nexusdl.core.models.chapter.Chapter`. A file at the root is its
   own manga (one manga, one chapter).
4. **Import** — each manga is upserted once, then each chapter is upserted.
   Upserts are idempotent, so re-running the scan on the same directory
   produces the same final state.

Failure isolation
-----------------
A failure on one file never aborts the scan. Extraction failures and
import failures are both recorded in :attr:`ScanReport.failures`; every
other file continues to be processed. This mirrors the isolation policy
of :mod:`nexusdl.plugins.loader` and
:mod:`nexusdl.core.downloader.worker`: one bad input is not a systemic
failure.

Grouping rule
-------------
Manga identity for scanned files is derived from the **parent directory**:

* ``root/One Piece/Ch 001.cbz`` → manga source_id = ``"One Piece"``,
  chapter source_id = ``"Ch 001"``.
* ``root/One Piece/Ch 002.cbz`` → same manga source_id, different chapter.
* ``root/Standalone.cbz`` (at root) → manga source_id = ``"Standalone"``,
  chapter source_id = ``"Standalone"``. One file, one manga, one chapter.

Two files with the same basename in different directories (e.g.
``A/Ch 001.cbz`` and ``B/Ch 001.cbz``) belong to different mangas because
their mangas have different ``source_id``, which produces different
``manga_id``, which in turn produces different ``chapter_id``. No
collision is possible.

ComicInfo authority
-------------------
When a file has a ``ComicInfo.xml``, its ``series``, ``title``, ``number``,
``summary``, ``writer``, ``year``, ``language_iso``, and ``page_count``
take precedence over the filename-derived fallbacks. When a file has no
metadata, a minimal :class:`~nexusdl.core.packaging.comic_info.ComicInfo`
is synthesized from the path so the file is still importable.

When multiple files in the same directory carry ComicInfo with
**conflicting** ``series`` values, the first file (by sorted path)
determines the manga's title. This is documented rather than resolved
because there is no universally-correct resolution: two files claiming to
be different series while living in the same folder is a metadata error
upstream, not something the scanner can fix.

Idempotency and incremental scan
--------------------------------
Every import is an upsert. Re-running :meth:`LibraryScanner.scan` on the
same directory produces the same final database state, but each file is
re-extracted and re-upserted. **The v1 scanner does not skip unchanged
files.** Implementing incremental skip would require comparing the file's
mtime against the chapter's ``updated_at`` in the database — doable with
the current schema (``chapters.updated_at`` exists) but adding per-file
round trips to a scan whose dominant cost is the archive read anyway. If
a future deployment needs incremental scanning, that is a targeted change
to :meth:`LibraryScanner.scan`; the API is forward-compatible.

Repository abstraction
----------------------
The scanner does not import :mod:`nexusdl.core.library.database`. It
depends on :class:`LibraryRepositoryProtocol`, a two-method structural
contract that :class:`~nexusdl.core.library.database.LibraryDatabase`
satisfies directly. This keeps :mod:`scanner` a leaf module — the
composition root wires it together with whatever database it built. It
also makes the scanner trivially testable with an in-memory fake.

Dependency graph (no cycles)::

    core.library.database          (via Protocol only)
    core.library.metadata   ─┐
    core.models.manga        │
    core.models.chapter      ├──► core.library.scanner
    core.models.enums        │
    core.exceptions         ─┘

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency (rule R1).
No dependency on ``core.library.database`` — the coupling runs the other
way.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from loguru import logger

from nexusdl.core.exceptions import LibraryError
from nexusdl.core.library.metadata import (
    SUPPORTED_ARCHIVE_EXTENSIONS,
    ExtractedMetadata,
    comic_info_to_chapter,
    comic_info_to_manga,
    extract_metadata,
)
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.manga import Manga
from nexusdl.core.packaging.comic_info import ComicInfo


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_SITE_ID",
    "LibraryRepositoryProtocol",
    "LibraryScanner",
    "ScanFailure",
    "ScanReport",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default site identifier for locally-scanned archives. Every manga the
#: scanner creates gets this ``site_id``, which keeps local entries
#: distinct from network-sourced mangas that happen to share a title.
DEFAULT_SITE_ID: Final[str] = "local"

#: Default number of files processed in parallel during extraction. High
#: enough to saturate an SSD without thrashing a spinning disk.
DEFAULT_MAX_CONCURRENT: Final[int] = 4


_log = logger.bind(component="library.scanner")


# --------------------------------------------------------------------------- #
# Repository protocol                                                         #
# --------------------------------------------------------------------------- #


class LibraryRepositoryProtocol(Protocol):
    """Minimal persistence surface required by :class:`LibraryScanner`.

    Satisfied by :class:`nexusdl.core.library.database.LibraryDatabase`.
    Only the two upsert methods are declared — the scanner never reads
    from the repository, so re-importing a library from scratch and
    updating an existing one are the same code path.
    """

    async def add_manga(self, manga: Manga) -> Manga:
        """Upsert *manga* and return the stored instance."""
        ...

    async def add_chapter(self, chapter: Chapter) -> Chapter:
        """Upsert *chapter* and return the stored instance."""
        ...


# --------------------------------------------------------------------------- #
# Data classes                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ScanFailure:
    """One file that could not be imported, with the reason.

    Immutable and safe to cache or log. The scanner never raises for a
    per-file failure; it records the failure here and continues.

    Attributes:
        archive_path: Absolute path to the archive that failed.
        error_type: Exception class name (``"LibraryError"``,
            ``"OSError"``, …). Stable for grouping and counting.
        error_message: Human-readable error message from the exception.
    """

    archive_path: Path
    error_type: str
    error_message: str


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Immutable summary of one :meth:`LibraryScanner.scan` call.

    Attributes:
        root: Absolute, resolved scan root.
        started_at: UTC timestamp when :meth:`LibraryScanner.scan` began.
        finished_at: UTC timestamp when it returned.
        files_found: Number of archive files discovered under *root*.
        files_imported: Number of chapters successfully upserted. Equal to
            ``files_found - files_failed`` on success.
        files_failed: Number of files that failed extraction or import.
        mangas_imported: Number of distinct mangas upserted. A directory
            with ten chapters contributes one to this count.
        failures: One entry per failed file, in discovery order.
    """

    root: Path
    started_at: datetime
    finished_at: datetime
    files_found: int
    files_imported: int
    files_failed: int
    mangas_imported: int
    failures: tuple[ScanFailure, ...]

    @property
    def ok(self) -> bool:
        """``True`` iff no file failed. A scan that found zero files is
        ``ok=True`` — no failures occurred."""
        return self.files_failed == 0

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock duration of the scan, in seconds."""
        return (self.finished_at - self.started_at).total_seconds()


# --------------------------------------------------------------------------- #
# Scanner                                                                     #
# --------------------------------------------------------------------------- #


class LibraryScanner:
    """Import archive files under a directory into the library database.

    See the module docstring for the full contract. The scanner is
    asyncio-native, has no mutable state beyond its semaphore, and is safe
    to call concurrently on distinct roots (each :meth:`scan` has its own
    ``_ScanState``).
    """

    def __init__(
        self,
        *,
        repository: LibraryRepositoryProtocol,
        site_id: str = DEFAULT_SITE_ID,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialise the scanner.

        Args:
            repository: Persistence backend satisfying
                :class:`LibraryRepositoryProtocol`.
            site_id: ``site_id`` attributed to every manga the scanner
                creates. Defaults to :data:`DEFAULT_SITE_ID` (``"local"``).
                Callers importing from a specific source should pass that
                source's id instead.
            max_concurrent: Maximum number of concurrent archive reads
                during extraction. Must be ``>= 1``.
            clock: Callable returning a timezone-aware ``datetime``.
                Defaults to ``datetime.now(UTC)``. Injectable for tests so
                that :class:`ScanReport` timestamps are deterministic.

        Raises:
            ValueError: If ``max_concurrent < 1``.
        """
        if max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be >= 1, got {max_concurrent}"
            )
        self._repository = repository
        self._site_id = site_id
        self._max_concurrent = max_concurrent
        self._clock: Callable[[], datetime] = (
            clock if clock is not None else lambda: datetime.now(UTC)
        )

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def scan(
        self,
        root: Path,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ScanReport:
        """Scan *root* recursively and import every archive found.

        Args:
            root: Directory to scan. Resolved to an absolute path. Must
                exist and be a directory.
            on_progress: Optional callback invoked after each archive is
                extracted, with ``(processed, total)``. ``total`` is the
                number of archives discovered; the callback is not invoked
                for the walk itself (which is fast). May be called from a
                worker thread — implementations must be thread-safe or
                schedule their own dispatch.

        Returns:
            A :class:`ScanReport` summarising the run. Never raises for a
            per-file failure — those are recorded in
            :attr:`ScanReport.failures`.

        Raises:
            LibraryError: If *root* does not exist or is not a directory.
                This is an input-validation error, not a per-file failure,
                and cannot be recorded in the report.
        """
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            raise LibraryError(
                f"scan root is not a directory: {resolved_root}"
            )

        started_at = self._clock()
        self._log_scan_start(resolved_root)

        paths = await asyncio.to_thread(_walk_archives, resolved_root)
        if on_progress is not None:
            on_progress(0, len(paths))

        extracted = await self._extract_all(paths, on_progress=on_progress)

        failures: dict[Path, ScanFailure] = {}
        successes: list[tuple[Path, ExtractedMetadata]] = []

        for path, meta, exc in extracted:
            if exc is not None:
                failures[path] = ScanFailure(
                    archive_path=path,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            elif meta is not None:
                successes.append((path, meta))

        # Group by manga source_id. First file per group determines the
        # manga's representative metadata.
        groups: dict[str, tuple[Path, ExtractedMetadata]] = {}
        for path, meta in successes:
            key = _manga_source_id(path, resolved_root)
            if key not in groups:
                groups[key] = (path, meta)

        manga_by_key = await self._import_mangas(
            groups, resolved_root, failures
        )

        imported = await self._import_chapters(
            successes, resolved_root, manga_by_key, failures
        )

        finished_at = self._clock()
        report = ScanReport(
            root=resolved_root,
            started_at=started_at,
            finished_at=finished_at,
            files_found=len(paths),
            files_imported=imported,
            files_failed=len(failures),
            mangas_imported=len(manga_by_key),
            failures=tuple(failures[p] for p in sorted(failures)),
        )
        self._log_scan_end(report)
        return report

    async def scan_file(
        self,
        archive_path: Path,
        *,
        root: Path,
    ) -> tuple[Manga, Chapter]:
        """Import a single archive file, returning the inserted rows.

        Intended for a "re-import this file" feature and for tests. The
        ``manga_source_id`` / ``chapter_source_id`` are computed relative
        to *root*, so the same file imported via :meth:`scan` and via
        :meth:`scan_file` (with the same *root*) produces identical ids.

        Args:
            archive_path: Path to a ``.cbz``, ``.cb7``, or ``.cbt`` file.
            root: Directory the file's id should be computed relative to.
                Used only for id computation, not for filesystem access.

        Returns:
            The ``(manga, chapter)`` pair that was upserted.

        Raises:
            LibraryError: If the archive cannot be read or the metadata
                cannot be imported. Unlike :meth:`scan`, this method does
                **not** swallow per-file failures — there is only one
                file, and a caller invoking :meth:`scan_file` expects the
                exception.
        """
        resolved_root = root.resolve()
        resolved_path = archive_path.resolve()

        meta = await extract_metadata(resolved_path)
        manga = self._build_manga(
            meta, manga_source_id=_manga_source_id(resolved_path, resolved_root)
        )
        await self._repository.add_manga(manga)
        chapter = self._build_chapter(meta, manga=manga)
        await self._repository.add_chapter(chapter)
        return manga, chapter

    # ------------------------------------------------------------------ #
    # Internals — extraction                                             #
    # ------------------------------------------------------------------ #

    async def _extract_all(
        self,
        paths: list[Path],
        *,
        on_progress: Callable[[int, int], None] | None,
    ) -> list[tuple[Path, ExtractedMetadata | None, Exception | None]]:
        """Extract metadata for every path, bounded by ``max_concurrent``.

        Returns results in the same order as *paths*, so failure
        reporting is deterministic regardless of completion order.

        Args:
            paths: Archive files discovered by the walk.
            on_progress: Forwarded from :meth:`scan`.
        """
        if not paths:
            return []

        semaphore = asyncio.Semaphore(self._max_concurrent)
        total = len(paths)
        processed = 0
        progress_lock = asyncio.Lock()

        async def _one(
            path: Path,
        ) -> tuple[Path, ExtractedMetadata | None, Exception | None]:
            nonlocal processed
            async with semaphore:
                try:
                    meta = await extract_metadata(path)
                    result: tuple[
                        Path, ExtractedMetadata | None, Exception | None
                    ] = (path, meta, None)
                except Exception as exc:  # noqa: BLE001 — isolation is intentional
                    _log.debug(
                        "Failed to extract metadata from {}: {}: {}",
                        path,
                        type(exc).__name__,
                        exc,
                    )
                    result = (path, None, exc)
            if on_progress is not None:
                async with progress_lock:
                    processed += 1
                    current = processed
                on_progress(current, total)
            return result

        tasks = [asyncio.create_task(_one(p), name=f"scan-{p.name}") for p in paths]
        try:
            return list(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise

    # ------------------------------------------------------------------ #
    # Internals — import                                                 #
    # ------------------------------------------------------------------ #

    async def _import_mangas(
        self,
        groups: dict[str, tuple[Path, ExtractedMetadata]],
        root: Path,
        failures: dict[Path, ScanFailure],
    ) -> dict[str, Manga]:
        """Upsert one manga per group. Record failures for the whole group.

        A manga upsert failure is attributed to every file that belongs to
        the failed group, so :attr:`ScanReport.files_failed` counts each
        affected file rather than just the representative.
        """
        manga_by_key: dict[str, Manga] = {}
        for key in sorted(groups):
            rep_path, rep_meta = groups[key]
            try:
                manga = self._build_manga(rep_meta, manga_source_id=key)
                await self._repository.add_manga(manga)
                manga_by_key[key] = manga
            except Exception as exc:  # noqa: BLE001 — isolation is intentional
                _log.exception(
                    "Failed to import manga for group {!r} (rep={}): {}",
                    key,
                    rep_path,
                    exc,
                )
                for path, _meta in groups.values():
                    pass  # noqa: B007 — placeholder to show intent below
                # Attribute the failure to every file in the failed group.
                for other_key, (other_path, _other_meta) in groups.items():
                    if other_key != key:
                        continue
                    # Find every file belonging to this group. `groups` only
                    # stores the representative, so we scan the whole set
                    # through the caller. The caller passes `groups` which
                    # was built from `successes`, so we can just record the
                    # representative and let _import_chapters skip the rest.
                failures[rep_path] = ScanFailure(
                    archive_path=rep_path,
                    error_type=type(exc).__name__,
                    error_message=(
                        f"cannot import manga for group {key!r}: {exc}"
                    ),
                )
        return manga_by_key

    async def _import_chapters(
        self,
        successes: list[tuple[Path, ExtractedMetadata]],
        root: Path,
        manga_by_key: dict[str, Manga],
        failures: dict[Path, ScanFailure],
    ) -> int:
        """Upsert one chapter per successful file. Count imports."""
        imported = 0
        for path, meta in successes:
            key = _manga_source_id(path, root)
            manga = manga_by_key.get(key)
            if manga is None:
                # Manga upsert failed for this group; already recorded.
                if path not in failures:
                    failures[path] = ScanFailure(
                        archive_path=path,
                        error_type="MangaImportFailed",
                        error_message=(
                            f"manga import failed for group {key!r}"
                        ),
                    )
                continue
            try:
                chapter = self._build_chapter(meta, manga=manga)
                await self._repository.add_chapter(chapter)
                imported += 1
            except Exception as exc:  # noqa: BLE001 — isolation is intentional
                _log.exception(
                    "Failed to import chapter from {}: {}", path, exc
                )
                failures[path] = ScanFailure(
                    archive_path=path,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
        return imported

    # ------------------------------------------------------------------ #
    # Internals — builders                                               #
    # ------------------------------------------------------------------ #

    def _build_manga(
        self, meta: ExtractedMetadata, *, manga_source_id: str
    ) -> Manga:
        """Build a :class:`Manga` for *meta*, using *manga_source_id* as the
        identity anchor.

        Metadata fields come from ``meta.comic_info`` when present,
        otherwise from a synthesized ComicInfo. The ``series`` field of the
        manga is derived from ``manga_source_id`` when the metadata does
        not provide one — this is the only way the manga's title can
        reliably match its identity across re-scans.
        """
        comic_info = meta.comic_info or _synthesize_comic_info(
            meta.archive_path, manga_source_id=manga_source_id
        )
        return comic_info_to_manga(
            comic_info,
            site_id=self._site_id,
            source_id=manga_source_id,
            url=_manga_url(meta.archive_path.parent),
        )

    def _build_chapter(
        self, meta: ExtractedMetadata, *, manga: Manga
    ) -> Chapter:
        """Build a :class:`Chapter` for *meta* under *manga*."""
        comic_info = meta.comic_info or _synthesize_comic_info(
            meta.archive_path,
            manga_source_id=_chapter_manga_source_id(meta, manga),
        )
        return comic_info_to_chapter(
            comic_info,
            manga_id=manga.id,
            source_id=meta.archive_path.stem,
            url=meta.archive_path.as_uri(),
        )

    # ------------------------------------------------------------------ #
    # Internals — logging                                                #
    # ------------------------------------------------------------------ #

    def _log_scan_start(self, root: Path) -> None:
        _log.info(
            "Starting library scan: root={} site_id={} max_concurrent={}",
            root,
            self._site_id,
            self._max_concurrent,
        )

    def _log_scan_end(self, report: ScanReport) -> None:
        level = "info" if report.ok else "warning"
        getattr(_log, level)(
            "Library scan finished: root={} files_found={} imported={} "
            "failed={} mangas={} elapsed={:.2f}s",
            report.root,
            report.files_found,
            report.files_imported,
            report.files_failed,
            report.mangas_imported,
            report.elapsed_seconds,
        )


# --------------------------------------------------------------------------- #
# Module-level helpers                                                       #
# --------------------------------------------------------------------------- #


def _walk_archives(root: Path) -> list[Path]:
    """Return every archive file under *root*, sorted, in one pass.

    Does not follow symlinks — a symlinked directory is treated as a leaf
    entry and its contents are not scanned. This matches the behaviour of
    every other tool that walks a media library (Komga, Kavita, Mihon) and
    eliminates the possibility of an infinite loop through a circular
    symlink structure.

    Args:
        root: Absolute scan root.

    Returns:
        Sorted list of archive paths. Sorting is by full path, so results
        are deterministic across filesystems.
    """
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.suffix.lower() in SUPPORTED_ARCHIVE_EXTENSIONS:
                found.append(candidate)
    found.sort()
    return found


def _manga_source_id(path: Path, root: Path) -> str:
    """Return the manga's stable ``source_id`` for *path*.

    A file inside a subdirectory is grouped with its siblings: the manga's
    ``source_id`` is the parent directory's relative path. A file at the
    scan root is its own manga: the ``source_id`` is the file's stem.
    """
    parent = path.parent
    if parent == root:
        return path.stem
    return str(parent.relative_to(root))


def _chapter_manga_source_id(
    meta: ExtractedMetadata, manga: Manga
) -> str:
    """Return the manga ``source_id`` derived from *manga*.

    Used only when synthesizing ComicInfo in :meth:`_build_chapter`, where
    the manga is already known. Kept as a separate helper because the
    synthesized ``series`` field must match the manga's identity, not the
    chapter's.
    """
    return manga.source_id


def _manga_url(parent_dir: Path) -> str:
    """Return the canonical ``file://`` URL for a manga's directory."""
    return parent_dir.resolve().as_uri()


def _synthesize_comic_info(
    archive_path: Path, *, manga_source_id: str
) -> ComicInfo:
    """Build a minimal :class:`ComicInfo` from a path with no metadata.

    The synthesized metadata matches the same field mapping that
    :func:`comic_info_to_manga` and :func:`comic_info_to_chapter` expect,
    so the rest of the import pipeline does not need to know whether the
    metadata is real or synthesized.

    Args:
        archive_path: Path to the archive being imported.
        manga_source_id: The manga ``source_id`` to use as ``series``.
            This is the only way to keep the synthesized manga's title
            consistent with its identity across re-scans.

    Returns:
        A ``ComicInfo`` with ``title`` and ``series`` populated, all other
        fields at their minimal defaults.
    """
    return ComicInfo(
        title=archive_path.stem,
        series=manga_source_id,
        number="1",
        language_iso="und",
        page_count=0,
    )
