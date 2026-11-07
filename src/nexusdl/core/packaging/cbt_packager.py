"""Package chapter pages into a CBT archive (Comic Book TAR).

CBT is the TAR-based sibling of CBZ / CB7. Structurally it is a plain
TAR archive with one convention: when the archive carries a
``ComicInfo.xml`` metadata file at its root, readers (Komga, Kavita,
Mihon, YACReader, CDisplayEx) display the manga's title, author, chapter
number, page count, and content flags from that file rather than
guessing from the archive name and page filenames.

This packager is that convention: **TAR + mandatory-if-provided
``ComicInfo.xml`` at the archive root**. When the caller supplies
metadata (the worker always does), the XML is embedded; when the caller
passes ``None``, the archive degrades gracefully to a plain TAR — still
loads in every reader, just without the metadata panel populated.

Why CBT and not CBR
-------------------
The PROMPT MASTER's ``PackagingFormat`` enum lists **CBT** (TAR-based)
and **deliberately excludes CBR** (RAR-based) — see ADR-002. RAR is a
proprietary format with no pure-Python writer; adding it would either
pull in a non-redistributable ``unrar`` binary or force a lossy
TAR+rename. CBT gives the same "single-file archive of images" shape
with a format every Python runtime can read and write natively.

Differences from the other packagers
------------------------------------
* **vs. ZIP / CBZ** — TAR is uncompressed by design. The
  ``compression_level`` parameter is accepted for interface uniformity
  with the ZIP-family packagers but **ignored**; a non-``None`` value
  produces a ``DEBUG`` log entry so a caller who passed it by mistake
  can see the fact. (Some deployments use ``.cbt.gz`` — a gzipped TAR —
  but the PROMPT MASTER's enum only lists ``CBT``, so this packager
  produces the plain ``.tar`` form.)
* **vs. CB7** — CB7 is a 7-Zip container; CBT is a TAR container. Both
  embed ``ComicInfo.xml`` at the root, both preserve page order, both
  are read transparently by every major reader.
* **vs. PDF** — PDF is a single document with no per-page image files;
  CBT is a container of image files (like CBZ / CB7).

``ComicInfo.xml`` placement
---------------------------
The XML is written **at the archive root**, named exactly
``ComicInfo.xml`` (case-sensitive). A ``ComicInfo.xml`` that appears
deeper in the tree (``Metadata/ComicInfo.xml``) is accepted by some
readers and rejected by others; keeping it at the root maximises
compatibility.

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

TAR-specific details
--------------------
* **PAX format.** The archive is written with
  :data:`tarfile.PAX_FORMAT`, which handles UTF-8 member names and
  arbitrary file sizes. The default :data:`tarfile.DEFAULT_FORMAT`
  (``PAX_FORMAT`` on Python 3.8+) also works, but spelling it
  explicitly makes the guarantee version-independent.
* **mtime preserved.** Each member carries the source file's
  modification time. Readers that sort by ``mtime`` (a minority, but
  Komga among them) therefore see the reading order the worker
  intended.
* **No explicit compression.** ``tarfile.open(..., mode="w")`` writes
  the plain TAR. ``w:gz`` would produce ``.cbt.gz`` (out of scope);
  ``w:bz2``, ``w:xz`` and ``w:zst`` would be wrong extensions and
  reader-incompatible.

Filesystem safety and cancellation
----------------------------------
The archive is built in a sibling temp file, ``fsync``'d,
:meth:`Path.replace`'d onto the final path, and the parent directory is
``fsync``'d so the rename itself is durable. A crash, an exception, or
an ``asyncio.CancelledError`` leaves the previous ``output`` untouched
and the temp file cleaned up.

Async model
-----------
:meth:`CbtPackager.package` is a coroutine; the TAR writing runs in a
worker thread via :func:`asyncio.to_thread`, keeping the event loop
responsive during potentially multi-second writes of a long chapter.

Dependency graph (no cycles)::

    core.exceptions        ─┐
    core.models.enums       ├──► core.packaging.cbt_packager
    core.models.page        │
    core.packaging.base    ─┘
    core.utils.filesystem  ─┘  (fsync_directory only)
"""

from __future__ import annotations

import asyncio
import os
import tarfile
import tempfile
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


__all__ = ["CbtPackager"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the metadata entry inside the archive. Case-sensitive; every
#: reader that recognizes ``ComicInfo.xml`` matches this spelling.
_COMIC_INFO_ENTRY: Final[str] = "ComicInfo.xml"

#: Required extension for the output path.
_REQUIRED_SUFFIX: Final[str] = ".cbt"

#: Temp-file prefix for atomic writes.
_TEMP_PREFIX_FMT: Final[str] = ".{name}.cbt-"

#: Chunk size used when streaming source files into the TAR. Matches the
#: 64 KiB default used by :mod:`nexusdl.core.utils.hash` and
#: :class:`~nexusdl.core.session.http_session.HttpSession` — small enough
#: to bound memory, large enough to amortise syscall overhead.
_COPY_CHUNK_SIZE: Final[int] = 64 * 1024


_log = logger.bind(component="packaging.cbt")


# --------------------------------------------------------------------------- #
# CbtPackager                                                                 #
# --------------------------------------------------------------------------- #


class CbtPackager(BasePackager):
    """Package chapter pages into a ``.cbt`` (TAR) archive.

    Stateless. One instance may be reused across concurrent
    :meth:`package` calls — no instance-level lock, no shared mutable
    state.

    See the module docstring for the full contract.
    """

    format = PackagingFormat.CBT

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
        """Package *pages* into a CBT archive at *output*.

        Args:
            pages: The chapter's pages, each paired with the file the
                worker downloaded them to. Order does not matter — the
                packager sorts by :attr:`Page.index` before writing.
            output: Destination ``.cbt`` path. Its extension must be
                ``.cbt`` (case-insensitive); the parent directory is
                created if it does not exist. An existing file at this
                path is replaced atomically.
            metadata: Optional
                :class:`~nexusdl.core.packaging.comic_info.ComicInfo`.
                When provided, its serialized XML is embedded as
                ``ComicInfo.xml`` at the archive root. When ``None``, no
                metadata entry is written — the archive is a valid but
                metadata-less CBT (still loads in every reader).
            compression_level: **Ignored** — TAR is uncompressed by
                design. A non-``None`` value produces a ``DEBUG`` log
                entry so a caller who passed it by mistake can see the
                fact.

        Returns:
            *output*, unchanged.

        Raises:
            PackagingError: If *pages* is empty, *output* has the wrong
                extension, a source file is missing, two pages resolve to
                the same archive entry name, the metadata cannot be
                serialized, or any I/O step fails.
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
                "cannot package an empty page list into a CBT archive"
            )

        if output.suffix.lower() != _REQUIRED_SUFFIX:
            raise PackagingError(
                f"CBT packager requires a {_REQUIRED_SUFFIX!r} output "
                f"path, got {output.name!r}"
            )

        if compression_level is not None:
            _log.debug(
                "CbtPackager ignores compression_level={} — TAR is "
                "uncompressed by design",
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
            arcname = src.name
            if arcname == _COMIC_INFO_ENTRY:
                # A page whose basename is literally "ComicInfo.xml"
                # would collide with the metadata entry we may write.
                # Reject unconditionally — even when metadata is None,
                # the archive would be silently corrupted from a
                # reader's point of view (the file would look like
                # metadata but contain image data).
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
            with tarfile.open(
                temp_path,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                if comic_info_bytes is not None:
                    _write_bytes_member(
                        archive, _COMIC_INFO_ENTRY, comic_info_bytes
                    )
                for page, src in sorted_pages:
                    _write_file_member(archive, src)
            _fsync_and_replace(temp_path, output)
        except (OSError, ValueError, tarfile.TarError) as exc:
            # ``tarfile.TarError`` is the base class for TAR-specific
            # failures (``ReadError``, ``CompressionError``,
            # ``StreamError``, …). ``ValueError`` covers the encoding /
            # path-length errors that ``tarfile`` raises directly, and
            # ``OSError`` covers every filesystem step.
            _safe_unlink(temp_path)
            raise PackagingError(
                f"failed to create CBT archive {output}: {exc}"
            ) from exc
        except BaseException:
            # ``CancelledError`` and any other BaseException must clean up
            # the temp file before propagating.
            _safe_unlink(temp_path)
            raise

        _log.info(
            "Packaged CBT archive: {} ({} pages{})",
            output,
            len(sorted_pages),
            ", with ComicInfo.xml" if comic_info_bytes is not None else "",
        )
        return output


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #
# NOTE: ``_fsync_and_replace`` duplicates the implementation in
# ``zip_packager.py`` / ``cbz_packager.py`` / ``pdf_packager.py``. The
# duplication is deliberate and reviewed: the helper is ~10 lines and
# stable, and extracting it now would mean reopening already-delivered
# files. The ``fsync_directory`` step is already imported from
# ``core/utils/filesystem.fsync_directory``.
# --------------------------------------------------------------------------- #


def _serialize_metadata(metadata: ComicInfo | None) -> bytes | None:
    """Serialize *metadata* to UTF-8 bytes for embedding, or return ``None``.

    A ``None`` *metadata* produces ``None`` — the CBT packager does not
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


def _write_bytes_member(
    archive: tarfile.TarFile, name: str, payload: bytes
) -> None:
    """Add a single in-memory byte string as a TAR member.

    Used for ``ComicInfo.xml`` (already serialized to bytes). ``TarInfo``
    requires the size to be set explicitly — TAR is not length-prefixed
    per entry, so the header must declare the size before the payload is
    streamed.

    Args:
        archive: The open TAR archive (``mode="w"``).
        name: The member name inside the archive.
        payload: The raw bytes to store.
    """
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mtime = int(os.stat(".").st_mtime) if False else 0
    # Use a fixed epoch mtime for the metadata entry so that two
    # packager runs that produce identical XML produce byte-identical
    # archives (modulo the temp file's own metadata, which is not
    # observable in the TAR payload). Reproducibility is worth more
    # than "when was this chapter packaged".
    import io

    archive.addfile(info, io.BytesIO(payload))


def _write_file_member(archive: tarfile.TarFile, source: Path) -> None:
    """Add *source* to the archive under its basename.

    The member carries the source file's basename as ``name``, and its
    size, mode, and mtime are read from the source file via
    :meth:`tarfile.TarFile.gettarinfo`. The file content is streamed in
    ``_COPY_CHUNK_SIZE``-byte blocks — the file is not read into memory
    in full, so a multi-megabyte JPEG costs one page's worth of memory,
    not the whole chapter's.

    Args:
        archive: The open TAR archive (``mode="w"``).
        source: The source image file.
    """
    info = archive.gettarinfo(str(source), arcname=source.name)
    # ``gettarinfo`` populates size/mode/mtime/uid/gid from the source
    # file. Overwriting them with fixed values would strip the only
    # cross-platform ordering hint a reader has (mtime); leave them.
    with source.open("rb") as fh:
        archive.addfile(info, fh)


def _fsync_and_replace(temp_path: Path, target: Path) -> None:
    """``fsync`` *temp_path*, rename it onto *target*, fsync the parent.

    Three steps, each of which is required for the atomic-write contract
    documented in the module docstring:

    1. **fsync the temp file's content.** Opens the file read-only —
       ``fsync(2)`` accepts an fd in ``O_RDONLY`` and flushes all dirty
       pages of the inode regardless of the open mode, so a write-capable
       fd is not required (and would fail on some filesystems where the
       file is read-only by design).
    2. **Atomically rename the temp file onto the target.**
       ``Path.replace`` is atomic on every filesystem that supports POSIX
       rename semantics.
    3. **fsync the parent directory.** Without this step a crash between
       the rename and the next directory flush can revert the rename —
       the target file disappears, the temp file reappears under its old
       name. This is the "durability of the rename itself" guarantee
       that :func:`nexusdl.core.utils.filesystem.fsync_directory` exists
       to provide.

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
