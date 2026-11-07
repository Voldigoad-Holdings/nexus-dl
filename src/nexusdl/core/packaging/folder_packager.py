"""Package chapter pages into a plain directory.

The folder packager is the only member of the family that produces
something other than a single file: it writes every page as an individual
image inside a target directory, plus — when metadata is provided — a
``ComicInfo.xml`` at the directory root. It is the format readers that
operate on "a folder of images" expect (MComix, YACReader's folder
mode, Komga and Kavita's ``.cbz``-or-folder library configuration).

What is guaranteed
------------------
* **Lossless** — pages are copied byte-for-byte (via
  :func:`shutil.copy2`, which preserves the modification time so readers
  that sort by ``mtime`` still see a coherent order). No re-encoding, no
  format conversion, no metadata rewriting of the image files themselves.
* **Atomic from the caller's perspective** — the target directory is
  either the previous content or the new content, never a mix. The build
  happens in a sibling temporary directory, and only becomes visible via
  a directory rename. A crash or cancellation before that rename leaves
  the previous content intact.
* **Reading-order sorted** — pages are copied in :attr:`Page.index` order.
  Because the worker names files ``"0001.jpg"``, ``"0002.jpg"``, …,
  lexicographic file listing also matches reading order. Readers that
  sort numerically by name see the correct sequence without any special
  handling.
* **Metadata embedded on demand** — when a
  :class:`~nexusdl.core.packaging.comic_info.ComicInfo` is passed, its
  serialized ``ComicInfo.xml`` is written to the directory root. When it
  is ``None``, no ``ComicInfo.xml`` is written (readers that expect one
  fall back to filename-derived metadata, exactly as they would for any
  folder of images).

Atomicity for directories
-------------------------
Unlike a file, a non-empty directory cannot be replaced atomically in
portable POSIX filesystem semantics — ``Path.replace`` on a non-empty
directory raises ``OSError`` on every mainstream filesystem. The packager
therefore uses a two-step dance:

1. If the target directory exists, rename it to a sibling backup name
   (``".<name>.old-<uuid>"``). The target no longer exists.
2. Rename the freshly-built temporary directory to the target.
3. ``fsync`` the target's parent directory so the rename itself is
   durable across a crash.
4. Delete the backup. If deletion fails, a warning is logged and the
   backup is left on disk — the target itself is intact, which is what
   matters.

If step 2 fails (extremely rare — the parent directory would have to have
become read-only between steps 1 and 2), the backup is renamed back to
the target, restoring the previous state. The caller sees an exception
and a clean rollback.

The window during which the target does not exist is bounded by two
``rename`` syscalls and contains no I/O on behalf of the packager — in
practice it is measured in microseconds. Any concurrent reader that
races the swap sees either the old content or the new content or an
``ENOENT`` — never a partially-written directory. This is the best
atomicity guarantee a directory can offer on POSIX, and it matches what
every tool that maintains a folder-based content directory does.

Async model
-----------
:meth:`FolderPackager.package` is a coroutine; the filesystem work runs
in a worker thread via :func:`asyncio.to_thread`, so the event loop stays
responsive while copying hundreds of page files.

Dependency graph (no cycles)::

    core.exceptions        ─┐
    core.models.enums       ├──► core.packaging.folder_packager
    core.models.page        │
    core.packaging.base    ─┘
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from loguru import logger

from nexusdl.core.exceptions import PackagingError
from nexusdl.core.models.enums import PackagingFormat
from nexusdl.core.models.page import Page
from nexusdl.core.packaging.base import BasePackager

if TYPE_CHECKING:
    from nexusdl.core.packaging.comic_info import ComicInfo


__all__ = ["FolderPackager"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the metadata file written at the directory root when a
#: :class:`ComicInfo` is provided. Matches the CBZ/CB7 convention and
#: what Komga/Kavita/MComix look for in a folder-based library.
_COMIC_INFO_FILENAME: Final[str] = "ComicInfo.xml"

#: Prefix pattern for the temporary build directory. Matches the
#: convention used by the other packagers so a stray temp directory is
#: recognisable.
_TEMP_DIR_PREFIX_FMT: Final[str] = ".{name}.folder-"

#: Prefix pattern for the backup directory created during an atomic swap.
#: Ends with a UUID to guarantee uniqueness even when the same target is
#: rebuilt many times in quick succession.
_BACKUP_DIR_PREFIX_FMT: Final[str] = ".{name}.old-"

#: Suffix a target directory may carry (case-insensitive). FOLDER has no
#: extension by design, but a caller may have written ``MyChapter.folder``
#: by hand; we tolerate it rather than reject it.
_ALLOWED_SUFFIXES: Final[frozenset[str]] = frozenset({"", ".folder"})


_log = logger.bind(component="packaging.folder")


# --------------------------------------------------------------------------- #
# FolderPackager                                                              #
# --------------------------------------------------------------------------- #


class FolderPackager(BasePackager):
    """Package chapter pages into a plain directory of image files.

    Stateless. One instance may be reused across concurrent
    :meth:`package` calls — no instance-level lock, no shared mutable
    state.

    See the module docstring for the full contract, including the
    directory-atomicity model.
    """

    format = PackagingFormat.FOLDER

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def package(
        self,
        pages: Sequence[tuple[Page, Path]],
        output: Path,
        *,
        metadata: ComicInfo | None = None,
        compression_level: int | None = None,
    ) -> Path:
        """Package *pages* into a directory at *output*.

        Args:
            pages: The chapter's pages, each paired with the file the
                worker downloaded it to. Order does not matter — the
                packager sorts by :attr:`Page.index` before copying.
            output: Destination directory. Its suffix must be ``""`` or
                ``".folder"`` (case-insensitive); the parent directory is
                created if it does not exist. An existing directory at
                this path is replaced atomically (see the module
                docstring for the exact semantics).
            metadata: Optional
                :class:`~nexusdl.core.packaging.comic_info.ComicInfo`.
                When provided, its serialized XML is written to
                ``<output>/ComicInfo.xml``. When ``None``, no
                ``ComicInfo.xml`` is written. Unlike the ZIP packager,
                FOLDER readers **do** look for this file, so honoring it
                is the right default behaviour when the worker supplies
                one.
            compression_level: **Ignored** — a directory of already-
                compressed images has no compression to apply. A
                non-``None`` value produces a ``DEBUG`` log entry so a
                caller who passed it by mistake can see the fact.

        Returns:
            *output*, unchanged.

        Raises:
            PackagingError: If *pages* is empty, *output* has the wrong
                suffix, a source file is missing, or the build fails at
                any step (including the atomic swap).
        """
        return await asyncio.to_thread(
            self._package_sync,
            pages,
            output,
            metadata,
            compression_level,
        )

    # ------------------------------------------------------------------ #
    # Synchronous implementation                                         #
    # ------------------------------------------------------------------ #

    def _package_sync(
        self,
        pages: Sequence[tuple[Page, Path]],
        output: Path,
        metadata: ComicInfo | None,
        compression_level: int | None,
    ) -> Path:
        """Synchronous implementation of :meth:`package`.

        Extracted so tests can call it without an event loop.
        """
        if not pages:
            raise PackagingError(
                "cannot package an empty page list into a directory"
            )

        if output.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise PackagingError(
                f"Folder packager requires a target directory with no "
                f"suffix (or a {'.folder'!r} suffix), got {output.name!r}"
            )

        if compression_level is not None:
            _log.debug(
                "FolderPackager ignores compression_level={} — a "
                "directory of already-compressed images has nothing to "
                "compress",
                compression_level,
            )

        sorted_pages = sorted(pages, key=lambda pair: pair[0].index)
        seen_names: set[str] = set()
        for page, src in sorted_pages:
            if not src.is_file():
                raise PackagingError(
                    f"source file for page {page.index} does not exist: "
                    f"{src}"
                )
            name = src.name
            if name in seen_names:
                raise PackagingError(
                    f"duplicate page filename {name!r} "
                    f"(page index {page.index})"
                )
            seen_names.add(name)

        output.parent.mkdir(parents=True, exist_ok=True)

        # We do **not** use ``tempfile.TemporaryDirectory`` here: after
        # the atomic rename the temp directory becomes the output
        # directory, and the context manager's ``__exit__`` would either
        # raise ``FileNotFoundError`` (Python ≤ 3.9) or silently call
        # ``rmtree(..., ignore_errors=True)`` on a path that no longer
        # exists (3.10+). Neither behaviour is what we want, and neither
        # is documented in the class. ``mkdtemp`` plus an explicit
        # best-effort cleanup in ``finally`` gives us the same safety
        # with no surprises.
        raw_tmpdir = tempfile.mkdtemp(
            dir=output.parent,
            prefix=_TEMP_DIR_PREFIX_FMT.format(name=output.name),
        )
        tmpdir = Path(raw_tmpdir)
        # Set as soon as the temp directory has been renamed onto
        # ``output`` — the cleanup below must skip the ``rmtree`` in that
        # case (there is nothing left to delete).
        renamed = False

        try:
            for page, src in sorted_pages:
                dst = tmpdir / src.name
                try:
                    shutil.copy2(src, dst)
                except OSError as exc:
                    raise PackagingError(
                        f"cannot copy page {page.index} to {dst}: {exc}"
                    ) from exc

            if metadata is not None:
                _write_comic_info(tmpdir, metadata)

            # fsync the build directory so its entries are durable
            # before the rename makes it visible.
            _fsync_directory(tmpdir)

            try:
                _atomic_replace_directory(tmpdir, output)
            except OSError as exc:
                raise PackagingError(
                    f"cannot finalise directory {output}: {exc}"
                ) from exc
            renamed = True

            # ``fsync`` the parent directory so the rename itself is
            # durable — a crash between the rename and the next
            # directory flush can otherwise revert it.
            _fsync_directory(output.parent)
        finally:
            if not renamed:
                # Best-effort cleanup of the temp directory. A failure
                # here (permissions, FS read-only) must not mask the
                # exception the caller actually needs to see.
                shutil.rmtree(tmpdir, ignore_errors=True)

        _log.info(
            "Packaged folder: {} ({} pages{})",
            output,
            len(sorted_pages),
            ", with ComicInfo.xml" if metadata is not None else "",
        )
        return output


# --------------------------------------------------------------------------- #
# Internal helpers                                                           #
# --------------------------------------------------------------------------- #


def _write_comic_info(directory: Path, comic_info: ComicInfo) -> None:
    """Serialize *comic_info* and write it as ``ComicInfo.xml``.

    Args:
        directory: The build directory (the eventual output directory).
        comic_info: Metadata to serialize. ``to_xml()`` must return a
            string; it is UTF-8 encoded on write.

    Raises:
        PackagingError: If the serialization raises or the write fails.
    """
    try:
        xml = comic_info.to_xml()
    except Exception as exc:  # noqa: BLE001 — serialization is a black box
        raise PackagingError(
            f"cannot serialize ComicInfo metadata: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        (directory / _COMIC_INFO_FILENAME).write_text(
            xml, encoding="utf-8"
        )
    except OSError as exc:
        raise PackagingError(
            f"cannot write {_COMIC_INFO_FILENAME} to {directory}: {exc}"
        ) from exc


def _fsync_directory(directory: Path) -> None:
    """Flush the directory's metadata to disk so entries are durable.

    Opening a directory read-only and calling ``fsync`` on it makes the
    directory entries (not the file contents) durable. This is the
    correct primitive after a batch of file creations whose *names* must
    survive a crash immediately after the subsequent rename.

    Non-POSIX platforms (Windows) do not support opening a directory;
    the operation is best-effort there and a failure is logged at
    ``DEBUG`` — the rename that follows is still the atomicity boundary,
    and Windows's journaled filesystems provide the durability guarantee
    on their own.

    Args:
        directory: The directory to flush. Must exist.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        # Windows (no directory fds), or a directory the caller lost
        # access to. Log and move on — the subsequent rename is what
        # matters for atomicity.
        _log.debug(
            "fsync_directory({}): cannot open directory", directory
        )
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        # Best-effort. A failed fsync does not invalidate the rename.
        _log.debug(
            "fsync_directory({}): fsync failed: {}", directory, exc
        )
    finally:
        # ``os.close`` can raise ``EBADF`` / ``EIO`` on a filesystem
        # that failed mid-operation. The close is cleanup, not part of
        # the caller's contract — surfacing its failure would violate
        # the "best-effort" promise of this helper.
        try:
            os.close(fd)
        except OSError:
            pass


def _atomic_replace_directory(tmpdir: Path, target: Path) -> None:
    """Atomically (as far as POSIX allows) replace *target* with *tmpdir*.

    If *target* does not exist, a single ``rename`` is used — genuinely
    atomic in the absence of a concurrent creator (the packager is the
    only writer for a given output path in practice). If *target*
    exists, it is renamed to a unique backup, the new directory is
    renamed to *target*, and the backup is deleted. If the second rename
    fails, the backup is restored so the previous state is preserved.

    Args:
        tmpdir: The freshly-built directory. It must exist and be
            complete. On return, it no longer exists at this path.
        target: The final destination. May or may not exist.

    Raises:
        OSError: If any rename fails for a reason other than the target
            already existing in the simple-rename case. If the restore
            rename also fails, the raised exception names *both* failures
            and the surviving backup path so the operator can recover
            manually. This exception is translated to
            :class:`PackagingError` by the caller.
    """
    if not target.exists():
        tmpdir.rename(target)
        return

    backup = target.parent / (
        _BACKUP_DIR_PREFIX_FMT.format(name=target.name) + uuid4().hex
    )
    target.rename(backup)
    try:
        tmpdir.rename(target)
    except OSError as rename_exc:
        # Restore the previous state before propagating.
        try:
            backup.rename(target)
        except OSError as restore_exc:
            _log.error(
                "FATAL: could not restore previous directory {} from {}: "
                "{}",
                target,
                backup,
                restore_exc,
            )
            # Raise a combined error so the caller sees both the original
            # rename failure and the failed rollback (with the surviving
            # backup path). Without this, the caller only sees the
            # rename failure and would not know that the target is now
            # missing and a backup survives.
            raise OSError(
                f"cannot replace directory {target!s} and cannot "
                f"restore previous state from backup at {backup!s}: "
                f"rename failed with {rename_exc!r}; restore failed "
                f"with {restore_exc!r}"
            ) from rename_exc
        raise
    # The target is now correct. Deleting the backup is a cleanup step;
    # a failure here leaves a stray directory but does not affect the
    # output. Log at warning so an operator can clean up manually.
    try:
        shutil.rmtree(backup)
    except OSError as exc:
        _log.warning(
            "Could not remove old directory backup {}: {}", backup, exc
        )
