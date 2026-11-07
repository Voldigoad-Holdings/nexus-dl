"""Package chapter pages into a plain ZIP archive.

The ZIP packager is the simplest member of the packager family. Unlike
CBZ and CB7, it does **not** embed ``ComicInfo.xml`` metadata — the ZIP
format has no convention for it, and users who want ComicRack-compatible
metadata should use the CBZ packager. The ``metadata`` parameter is
accepted for interface uniformity (see
:class:`~nexusdl.core.packaging.base.BasePackager`) but is intentionally
ignored; the log records the fact at ``DEBUG`` level so a caller who
passes metadata by mistake can see it.

Output shape
------------
The produced ZIP contains exactly one entry per page, named after the
source file the worker created. The worker's naming convention
(``"0001.jpg"``, ``"0002.jpg"``, … — zero-padded and derived from
:attr:`~nexusdl.core.models.page.Page.index`) is preserved verbatim, so
every comic reader that sorts entries lexicographically (Komga, Kavita,
Mihon, YACReader, CDisplayEx) displays the pages in reading order without
any special handling.

Pages are sorted by :attr:`Page.index` before being written, so a caller
that passes them out of order still gets a correctly-ordered archive. A
duplicate filename (two pages mapping to the same source name) raises
:class:`~nexusdl.core.exceptions.PackagingError` — silently overwriting
one page with another is never the right behaviour.

Compression
-----------
The ZIP format supports STORED (no compression) and DEFLATED. Manga
pages are usually JPEG or PNG, both of which are already compressed, so
DEFLATE typically saves only a few percent. The packager therefore maps
``compression_level`` to the two methods explicitly:

* ``compression_level=0`` → ``ZIP_STORED`` (fastest, no compression)
* ``compression_level=1..9`` → ``ZIP_DEFLATED`` at that level
* ``compression_level=None`` → ``ZIP_DEFLATED`` at level 6 (the
  conventional default)

The value comes from ``settings.packaging.compression_level``; a user
who wants faster packaging sets it to 0, and a user who wants smaller
archives at the cost of CPU sets it to 9.

Filesystem safety
-----------------
Writes are atomic, using the same temp-file-plus-rename pattern as the
image optimizer and converter. A crash or cancellation mid-write leaves
the previous ``output`` untouched and cleans up the temp file. When
cancellation arrives after the rename completes, the output file is
already complete and valid; the caller only loses the return value.

**Why this packager does not use ``utils.filesystem.atomic_write_bytes``.**
The shared atomic-write helper in
:mod:`nexusdl.core.utils.filesystem` serialises a ``bytes`` payload to
disk; it cannot express "write a ZIP archive entry by entry with the
``zipfile`` module". The pattern (temp file in the same directory,
fsync, ``Path.replace``, fsync the parent directory) is the same, but
the writing step is streaming and has to be done inline. The
duplication is therefore **intentional** and small — one helper
(:func:`_fsync_and_replace`) that does the fsync-plus-rename contract.
The rest of the atomic-write surface is provided by ``utils.filesystem``
and used by the other packagers where a ``bytes`` payload is the
natural shape.

Async model
-----------
:meth:`ZipPackager.package` is a coroutine; the actual zipping runs in a
worker thread via :func:`asyncio.to_thread`, so the event loop stays
responsive during the (potentially multi-second) compression of a
chapter with hundreds of pages.

Dependency graph (no cycles)::

    core.exceptions        ─┐
    core.models.enums       ├──► core.packaging.zip_packager
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


__all__ = ["ZipPackager"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default DEFLATE compression level when the caller does not supply one.
#: Matches the Python ``zipfile`` default and the ZIP ecosystem convention.
_DEFAULT_COMPRESSION_LEVEL: Final[int] = 6

#: Maximum DEFLATE compression level accepted. ``zipfile`` itself enforces
#: this bound, but validating here surfaces a ``PackagingError`` instead of
#: a raw ``ValueError`` from the ``zipfile`` internals.
_MAX_COMPRESSION_LEVEL: Final[int] = 9

#: Required extension for the output path. A mismatch (``.cbz``, ``.7z``,
#: no extension) means the caller chose the wrong packager — that is worth
#: a clear error, not a silently misnamed file.
_REQUIRED_SUFFIX: Final[str] = ".zip"

#: Temp-file prefix for atomic writes. Matches the convention used in the
#: image optimizer/converter so a stray temp file is recognisable.
_TEMP_PREFIX_FMT: Final[str] = ".{name}.zip-"


_log = logger.bind(component="packaging.zip")


# --------------------------------------------------------------------------- #
# ZipPackager                                                                 #
# --------------------------------------------------------------------------- #


class ZipPackager(BasePackager):
    """Pack chapter pages into a plain ``.zip`` archive.

    Stateless. One instance may be reused across concurrent
    :meth:`package` calls — no instance-level lock, no shared mutable
    state beyond the logger's binding (which is itself thread-safe).

    See the module docstring for the full contract.
    """

    format = PackagingFormat.ZIP

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
        """Package *pages* into a ZIP archive at *output*.

        Args:
            pages: The chapter's pages, each paired with the file the
                worker downloaded it to. Order does not matter — the
                packager sorts by :attr:`Page.index` before writing.
            output: Destination ``.zip`` path. Its extension must be
                ``.zip`` (case-insensitive); the parent directory is
                created if it does not exist. An existing file at this
                path is replaced atomically.
            metadata: Accepted for interface uniformity with CBZ / CB7
                packagers but **ignored** — the ZIP format has no
                embedded metadata convention. A ``DEBUG`` log records the
                fact when a non-``None`` value is passed.
            compression_level: ``0`` for STORE, ``1..9`` for DEFLATE,
                ``None`` for DEFLATE at :data:`_DEFAULT_COMPRESSION_LEVEL`.
                Out-of-range values raise :class:`PackagingError`.

        Returns:
            *output*, unchanged.

        Raises:
            PackagingError: If *pages* is empty, *output* has the wrong
                extension, a source file is missing, two pages resolve to
                the same archive entry name, the compression level is out
                of range, or any I/O step fails.
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
                "cannot package an empty page list into a ZIP archive"
            )

        if output.suffix.lower() != _REQUIRED_SUFFIX:
            raise PackagingError(
                f"ZIP packager requires a {_REQUIRED_SUFFIX!r} output path, "
                f"got {output.name!r}"
            )

        if metadata is not None:
            _log.debug(
                "ZipPackager ignores metadata (ComicInfo is a CBZ/CB7-only "
                "convention); use PackagingFormat.CBZ if you need it "
                "embedded"
            )

        compression_method, compresslevel = _resolve_compression(
            compression_level
        )

        # Validate every source file before opening the output, so a
        # missing page fails fast rather than after we have written half
        # an archive.
        sorted_pages = sorted(pages, key=lambda pair: pair[0].index)
        seen_names: set[str] = set()
        for page, src in sorted_pages:
            if not src.is_file():
                raise PackagingError(
                    f"source file for page {page.index} does not exist: "
                    f"{src}"
                )
            arcname = src.name
            if arcname in seen_names:
                raise PackagingError(
                    f"duplicate archive entry name {arcname!r} "
                    f"(page index {page.index})"
                )
            seen_names.add(arcname)

        # Prepare the output directory and temp file. Same directory so
        # ``Path.replace`` is a rename, not a cross-filesystem copy.
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
                f"failed to create ZIP archive {output}: {exc}"
            ) from exc
        except BaseException:
            # ``CancelledError`` and any other BaseException must clean up
            # the temp file before propagating.
            _safe_unlink(temp_path)
            raise

        _log.info(
            "Packaged ZIP archive: {} ({} pages)",
            output,
            len(sorted_pages),
        )
        return output


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #


def _resolve_compression(
    level: int | None,
) -> tuple[int, int | None]:
    """Map *level* to a ``(zipfile compression method, compresslevel)`` pair.

    Args:
        level: ``None`` for the default, ``0`` for STORE, ``1..9`` for
            DEFLATE.

    Returns:
        A tuple suitable for ``zipfile.ZipFile(compression=..., compresslevel=...)``.

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
       semantics — the check-then-rename window is a no-op for a
       single writer (the packager).
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
