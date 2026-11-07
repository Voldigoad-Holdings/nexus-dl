"""Package chapter pages into a CBZ archive (Comic Book ZIP).

CBZ is the dominant manga/comic archive format. Structurally, it is a
plain ZIP file with one convention: when the archive carries a
``ComicInfo.xml`` metadata file, readers (Komga, Kavita, Mihon, YACReader,
CDisplayEx) display the manga's title, author, chapter number, page count,
and content flags from that file rather than guessing from the archive
name and page filenames.

This packager is that convention: **ZIP + mandatory-if-provided
``ComicInfo.xml`` at the archive root**. When the caller supplies
metadata (the worker always does), the XML is embedded; when the caller
passes ``None``, the archive degrades gracefully to a plain ZIP — still
loads in every reader, just without the metadata panel populated.

Differences from :class:`~nexusdl.core.packaging.zip_packager.ZipPackager`
--------------------------------------------------------------------------
Both write the same container; the only behavioural differences are:

* **File extension**: CBZ requires ``.cbz``; ZIP requires ``.zip``.
* **Metadata**: CBZ embeds ``ComicInfo.xml`` when a
  :class:`~nexusdl.core.packaging.comic_info.ComicInfo` is passed. ZIP
  always ignores metadata (the ZIP format has no such convention).

Everything else — the atomic-write mechanics, the compression-level
mapping, the page-ordering and duplicate-name validation, the cleanup
contract on exceptions — is identical. The two files are deliberately
kept self-contained rather than sharing a private base class:

* the shared logic is ~40 lines and stable,
* :mod:`nexusdl.core.utils.filesystem` now provides the canonical
  atomic-write primitive (``atomic_write_bytes``) that replaces the
  local ``_fsync_and_replace`` helper, and
  :mod:`nexusdl.core.utils.filesystem.fsync_directory` provides the
  directory-durability helper. A future refactor of this file will
  import those, at which point the local helper disappears. The current
  duplication is kept because reopening an already-shipped file to
  refactor it is riskier than the readability gain justifies — the same
  policy applied to the other packagers.
* a private intermediate base class would couple the two files'
  evolution with no practical benefit today.

``ComicInfo.xml`` placement
---------------------------
The XML is written **at the archive root**, named exactly
``ComicInfo.xml`` (case-sensitive). This is what every reader expects. A
``ComicInfo.xml`` that appears deeper in the tree (``Metadata/ComicInfo.xml``)
is accepted by some readers and rejected by others; keeping it at the root
maximises compatibility.

Page ordering and filenames
---------------------------
Pages are sorted by :attr:`~nexusdl.core.models.page.Page.index` before
being written. The filename inside the archive is the source file's
basename — the worker's convention (``"0001.jpg"``, ``"0002.jpg"``, …) is
preserved verbatim, so every reader that sorts entries lexicographically
(Komga, Kavita, Mihon, YACReader, CDisplayEx) displays the pages in
reading order.

A duplicate filename (two pages mapping to the same basename) raises
:class:`~nexusdl.core.exceptions.PackagingError` — silently overwriting
one page with another is never acceptable.

Compression
-----------
Identical mapping to :class:`~nexusdl.core.packaging.zip_packager.ZipPackager`:
``0`` → STORED, ``1..9`` → DEFLATED, ``None`` → DEFLATED at level 6.

Filesystem safety and cancellation
----------------------------------
The archive is built in a sibling temp file, ``fsync``'d,
:meth:`Path.replace`'d onto the final path, and the parent directory is
``fsync``'d so the rename itself is durable. A crash, an exception, or an
``asyncio.CancelledError`` leaves the previous ``output`` untouched and
the temp file cleaned up.

Async model
-----------
:meth:`CbzPackager.package` is a coroutine; the zipping runs in a worker
thread via :func:`asyncio.to_thread`, keeping the event loop responsive
during potentially multi-second compression of a long chapter.

Dependency graph (no cycles)::

    core.exceptions        ─┐
    core.models.enums       ├──► core.packaging.cbz_packager
    core.models.page        │
    core.packaging.base    ─┘
    core.utils.filesystem  ─┘  (fsync_directory only)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

from loguru import logger

from nexusdl.core.exceptions import PackagingError
from nexusdl.core.models.enums import PackagingFormat
from nexusdl.core.models.page import Page
from nexusdl.core.packaging.base import BasePackager
from nexusdl.core.utils.filesystem import fsync_directory

if TYPE_CHECKING:
    from nexusdl.core.packaging.comic_info import ComicInfo


__all__ = ["CbzPackager"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the metadata entry inside the archive. Case-sensitive; every
#: reader that recognizes ``ComicInfo.xml`` matches this spelling.
_COMIC_INFO_ENTRY: Final[str] = "ComicInfo.xml"

#: Default DEFLATE compression level, matching
#: :class:`~nexusdl.core.packaging.zip_packager.ZipPackager` and the ZIP
#: ecosystem convention.
_DEFAULT_COMPRESSION_LEVEL: Final[int] = 6

#: Maximum DEFLATE compression level accepted.
_MAX_COMPRESSION_LEVEL: Final[int] = 9

#: Required extension for the output path.
_REQUIRED_SUFFIX: Final[str] = ".cbz"

#: Temp-file prefix for atomic writes.
_TEMP_PREFIX_FMT: Final[str] = ".{name}.cbz-"


_log = logger.bind(component="packaging.cbz")


# --------------------------------------------------------------------------- #
# CbzPackager                                                                 #
# --------------------------------------------------------------------------- #


class CbzPackager(BasePackager):
    """Package chapter pages into a ``.cbz`` archive.

    Stateless. One instance may be reused across concurrent
    :meth:`package` calls — no instance-level lock, no shared mutable
    state.

    See the module docstring for the full contract.
    """

    format = PackagingFormat.CBZ

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
        """Package *pages* into a CBZ archive at *output*.

        Args:
            pages: The chapter's pages, each paired with the file the
                worker downloaded them to. Order does not matter — the
                packager sorts by :attr:`Page.index` before writing.
            output: Destination ``.cbz`` path. Its extension must be
                ``.cbz`` (case-insensitive); the parent directory is
                created if it does not exist. An existing file at this
                path is replaced atomically.
            metadata: Optional
                :class:`~nexusdl.core.packaging.comic_info.ComicInfo`.
                When provided, its serialized XML is embedded as
                ``ComicInfo.xml`` at the archive root. When ``None``, no
                metadata entry is written — the archive is a valid but
                metadata-less CBZ (still loads in every reader).
            compression_level: ``0`` for STORE, ``1..9`` for DEFLATE,
                ``None`` for DEFLATE at
                :data:`_DEFAULT_COMPRESSION_LEVEL`. Out-of-range values
                raise :class:`PackagingError`.

        Returns:
            *output*, unchanged.

        Raises:
            PackagingError: If *pages* is empty, *output* has the wrong
                extension, a source file is missing, two pages resolve to
                the same archive entry name, the compression level is out
                of range, the metadata cannot be serialized, or any I/O
                step fails.
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
                "cannot package an empty page list into a CBZ archive"
            )

        if output.suffix.lower() != _REQUIRED_SUFFIX:
            raise PackagingError(
                f"CBZ packager requires a {_REQUIRED_SUFFIX!r} output "
                f"path, got {output.name!r}"
            )

        compression_method, compresslevel = _resolve_compression(
            compression_level
        )

        sorted_pages = sorted(pages, key=lambda pair: pair[0].index)
        seen_names: set[str] = set()
        for page, src in sorted_pages:
            if not src.is_file():
                raise PackagingError(
                    f"source file for page {page.index} does not exist: "
                    f"{src}"
                )
            arcname = src.name
            if arcname == _COMIC_INFO_ENTRY:
                # A page whose basename is literally "ComicInfo.xml"
                # would collide with the metadata entry we may write.
                # Reject unconditionally — even when metadata is None,
                # the archive would be silently corrupted from a reader's
                # point of view (the file would look like metadata but
                # contain image data).
                raise PackagingError(
                    f"page {page.index} filename {arcname!r} collides "
                    f"with the reserved metadata entry name"
                )
            if arcname in seen_names:
                raise PackagingError(
                    f"duplicate archive entry name {arcname!r} "
                    f"(page index {page.index})"
                )
            seen_names.add(arcname)

        comic_info_bytes = _serialize_metadata(metadata)

        output.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp = tempfile.mkstemp(
            dir=output.parent,
            prefix=_TEMP_PREFIX_FMT.format(name=output.name),
            suffix=".tmp",
        )
        # ``os.close`` can fail on a corrupted fd but there is nothing
        # useful we can do — the temp file exists and its path is what
        # matters. Guard the close so a pathological fd state does not
        # prevent cleanup of the (already-created) temp file.
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path = Path(raw_temp)

        try:
            with zipfile.ZipFile(
                temp_path,
                mode="w",
                compression=compression_method,
                compresslevel=compresslevel,
            ) as archive:
                if comic_info_bytes is not None:
                    archive.writestr(_COMIC_INFO_ENTRY, comic_info_bytes)
                for page, src in sorted_pages:
                    archive.write(src, arcname=src.name)
            _fsync_and_replace(temp_path, output)
        except (
            OSError,
            ValueError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as exc:
            # ``BadZipFile`` and ``LargeZipFile`` are not subclasses of
            # ``OSError`` or ``ValueError`` — they derive from
            # ``Exception`` directly. Without them in this tuple, a
            # corrupted write would fall through to the ``BaseException``
            # branch below and propagate raw, hiding the ``PackagingError``
            # contract from the caller.
            _safe_unlink(temp_path)
            raise PackagingError(
                f"failed to create CBZ archive {output}: {exc}"
            ) from exc
        except BaseException:
            # ``CancelledError`` and any other BaseException must clean up
            # the temp file before propagating.
            _safe_unlink(temp_path)
            raise

        _log.info(
            "Packaged CBZ archive: {} ({} pages{})",
            output,
            len(sorted_pages),
            ", with ComicInfo.xml" if comic_info_bytes is not None else "",
        )
        return output


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #
# NOTE: ``_resolve_compression`` and ``_fsync_and_replace`` duplicate the
# implementations in ``zip_packager.py`` (and, for fsync+replace,
# ``pdf_packager.py``). The duplication is deliberate and reviewed: the
# helpers are ~10 lines each, stable, and extracting them now would mean
# reopening already-delivered files. When ``zip_packager.py`` /
# ``pdf_packager.py`` are next opened for any reason, all three files will
# import the atomic-write primitive from ``core/utils/filesystem.py``
# (which already exists) and the local helpers will disappear. The
# directory-fsync step is already imported from
# ``core/utils/filesystem.fsync_directory``.
# --------------------------------------------------------------------------- #


def _resolve_compression(
    level: int | None,
) -> tuple[int, int | None]:
    """Map *level* to a ``(zipfile compression method, compresslevel)`` pair.

    Args:
        level: ``None`` for the default, ``0`` for STORE, ``1..9`` for
            DEFLATE.

    Returns:
        A tuple suitable for
        ``zipfile.ZipFile(compression=..., compresslevel=...)``.

    Raises:
        PackagingError: If *level* is not ``None`` and not an ``int`` in
            ``[0, 9]``.
    """
    if level is None:
        return zipfile.ZIP_DEFLATED, _DEFAULT_COMPRESSION_LEVEL
    if not isinstance(level, int) or isinstance(level, bool):
        raise PackagingError(
            f"compression_level must be an int or None, got "
            f"{type(level).__name__}"
        )
    if not (0 <= level <= _MAX_COMPRESSION_LEVEL):
        raise PackagingError(
            f"compression_level must be in [0, {_MAX_COMPRESSION_LEVEL}], "
            f"got {level}"
        )
    if level == 0:
        return zipfile.ZIP_STORED, None
    return zipfile.ZIP_DEFLATED, level


def _serialize_metadata(metadata: ComicInfo | None) -> bytes | None:
    """Serialize *metadata* to UTF-8 bytes for embedding, or return ``None``.

    A ``None`` *metadata* produces ``None`` — the CBZ packager does not
    insert a stub ``ComicInfo.xml``, because a stub would override what a
    reader would otherwise infer from the archive name and file listing.
    An empty-but-present metadata entry is worse than no entry.

    Args:
        metadata: The optional ``ComicInfo`` instance.

    Returns:
        UTF-8-encoded XML bytes, or ``None`` when *metadata* is ``None``.

    Raises:
        PackagingError: If :meth:`ComicInfo.to_xml` raises.
    """
    if metadata is None:
        return None
    try:
        return metadata.to_xml().encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — serialization is a black box
        raise PackagingError(
            f"cannot serialize ComicInfo metadata: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _safe_unlink(path: Path) -> None:
    """Best-effort removal of *path*, never raising.

    Used in the exception-handling paths of :meth:`_package_sync` so that
    a failure to clean up a temp file (permissions, FS read-only) does
    not replace the exception the caller actually needs to see.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _fsync_and_replace(temp_path: Path, target: Path) -> None:
    """``fsync`` *temp_path*, rename it onto *target*, fsync the parent.

    Three steps, each of which is required for the atomic-write contract
    documented in the module docstring:

    1. **fsync the temp file's content.** Opens the file read-only —
       ``fsync(2)`` accepts an fd in ``O_RDONLY`` and flushes all dirty
       pages of the inode regardless of the open mode, so a write-capable
       fd is not required (and would fail on some filesystems where the
       file is read-only by design).
    2. **Atomically rename the temp file onto the target.** ``Path.replace``
       is atomic on every filesystem that supports POSIX rename
       semantics.
    3. **fsync the parent directory.** Without this step a crash between
       the rename and the next directory flush can revert the rename —
       the target file disappears, the temp file reappears under its
       old name. This is the "durability of the rename itself"
       guarantee that :func:`nexusdl.core.utils.filesystem.fsync_directory`
       exists to provide.

    Args:
        temp_path: The just-written temporary file.
        target: The final destination path.

    Raises:
        OSError: If the fsync or the rename fails.
    """
    with temp_path.open("rb") as fh:
        os.fsync(fh.fileno())
    temp_path.replace(target)
    fsync_directory(target.parent)
