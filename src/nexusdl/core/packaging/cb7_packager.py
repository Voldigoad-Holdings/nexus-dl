"""Package chapter pages into a CB7 archive (Comic Book 7-Zip).

CB7 is CBZ's higher-ratio sibling: a 7-Zip archive (``.7z`` under the
hood, ``.cb7`` by extension) that follows the same metadata convention
as CBZ — a ``ComicInfo.xml`` at the archive root, read by Komga, Kavita,
Mihon, YACReader, and CDisplayEx.

**Why 7-Zip for manga.** Manga pages are typically JPEG or PNG, both of
which are already compressed, so a second compression pass usually saves
very little. The situations where CB7 pays off are narrower:

* mixed-format chapters (e.g. some pages PNG, some JPEG),
* archival re-packaging of a huge library where a few percent per file
  adds up to gigabytes,
* users explicitly choosing "smallest archive" over "fastest open" in
  the GUI.

For those cases CB7's LZMA2 filter typically produces 5–15% smaller
archives than CBZ's DEFLATE, at a significant CPU cost. The default
compression preset (6) is a reasonable balance; the packager accepts the
full ``0..9`` range so a user who really wants to trade time for size can
choose ``9``.

**Why ``compression_level=0`` is not "no compression".** 7-Zip's
canonical solid format has no per-file STORE flag the way ZIP does. A
preset of ``0`` selects LZMA2's minimum-effort profile, which still
compresses — it is meaningfully faster than preset 9 and meaningfully
smaller than STORED on mixed content, but it is not zero. This is
documented explicitly because a user migrating from ``zip_packager``'s
``compression_level=0 → ZIP_STORED`` semantic might expect the same
here. They should not.

``ComicInfo.xml`` placement
---------------------------
Identical to :class:`~nexusdl.core.packaging.cbz_packager.CbzPackager`:
written **first** in the archive, at the root, named exactly
``ComicInfo.xml``. A page whose basename collides with that name raises
:class:`~nexusdl.core.exceptions.PackagingError` — the resulting archive
would be silently corrupted from a reader's point of view.

Page ordering and filenames
---------------------------
Pages are sorted by :attr:`~nexusdl.core.models.page.Page.index` before
being written. The filename inside the archive is the source file's
basename, preserving the worker's zero-padded convention.

Filesystem safety and cancellation
----------------------------------
The archive is built in a sibling temp file, ``fsync``'d, and
:meth:`Path.replace`'d onto the final path. A crash, an exception, or an
``asyncio.CancelledError`` leaves the previous ``output`` untouched and
the temp file cleaned up.

**Memory use.** Unlike ZIP, ``py7zr`` writes each file to disk as it is
added (it does not buffer the whole archive in memory), so peak memory
is bounded by the largest single file (a page, typically tens to
hundreds of KiB) times the internal block buffer size. Large chapters
do not exhaust memory.

Async model
-----------
:meth:`Cb7Packager.package` is a coroutine; the archiving runs in a
worker thread via :func:`asyncio.to_thread`, keeping the event loop
responsive during what is often the slowest step of a download (7z
compression is markedly slower than DEFLATE).

Dependency graph (no cycles)::

    core.exceptions        ─┐
    core.models.enums       ├──► core.packaging.cb7_packager
    core.models.page        │
    core.packaging.base    ─┘
    (ComicInfo lazily imported at package() call time via TYPE_CHECKING)
"""

from __future__ import annotations

import asyncio
import io
import lzma
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import py7zr
from loguru import logger

from nexusdl.core.exceptions import PackagingError
from nexusdl.core.models.enums import PackagingFormat
from nexusdl.core.models.page import Page
from nexusdl.core.packaging.base import BasePackager

if TYPE_CHECKING:
    from nexusdl.core.packaging.comic_info import ComicInfo


__all__ = ["Cb7Packager"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Filename of the metadata entry inside the archive. Case-sensitive;
#: identical to the CBZ convention and to what every reader recognizes.
_COMIC_INFO_ENTRY: Final[str] = "ComicInfo.xml"

#: Default LZMA2 preset, used when the caller passes ``compression_level=None``.
#: Matches 7-Zip's own default and the CBZ packager's DEFLATE default for
#: consistency of "what happens out of the box".
_DEFAULT_COMPRESSION_LEVEL: Final[int] = 6

#: Maximum LZMA2 preset accepted. ``py7zr`` uses the same ``0..9`` scale as
#: :mod:`lzma`, so ``9`` is the highest-effort preset.
_MAX_COMPRESSION_LEVEL: Final[int] = 9

#: Required extension for the output path.
_REQUIRED_SUFFIX: Final[str] = ".cb7"

#: Temp-file prefix for atomic writes.
_TEMP_PREFIX_FMT: Final[str] = ".{name}.cb7-"


_log = logger.bind(component="packaging.cb7")


# --------------------------------------------------------------------------- #
# Cb7Packager                                                                 #
# --------------------------------------------------------------------------- #


class Cb7Packager(BasePackager):
    """Package chapter pages into a ``.cb7`` archive.

    Stateless. One instance may be reused across concurrent
    :meth:`package` calls — no instance-level lock, no shared mutable
    state.

    See the module docstring for the full contract, including the CB7
    ``compression_level`` caveat.
    """

    format = PackagingFormat.CB7

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
        """Package *pages* into a CB7 archive at *output*.

        Args:
            pages: The chapter's pages, each paired with the file the
                worker downloaded them to. Order does not matter — the
                packager sorts by :attr:`Page.index` before writing.
            output: Destination ``.cb7`` path. Its extension must be
                ``.cb7`` (case-insensitive); the parent directory is
                created if it does not exist. An existing file at this
                path is replaced atomically.
            metadata: Optional
                :class:`~nexusdl.core.packaging.comic_info.ComicInfo`.
                When provided, its serialized XML is embedded as
                ``ComicInfo.xml`` at the archive root. When ``None``, no
                metadata entry is written.
            compression_level: LZMA2 preset. ``None`` for the default
                (:data:`_DEFAULT_COMPRESSION_LEVEL`), ``0..9`` for an
                explicit preset. **Not** the ZIP meaning: ``0`` still
                applies LZMA2 compression at its lowest effort — see the
                module docstring. Out-of-range values raise
                :class:`PackagingError`.

        Returns:
            *output*, unchanged.

        Raises:
            PackagingError: If *pages* is empty, *output* has the wrong
                extension, a source file is missing, two pages resolve to
                the same archive entry name, a page's basename collides
                with the reserved ``ComicInfo.xml``, the compression
                level is out of range, the metadata cannot be serialized,
                or any I/O or ``py7zr`` step fails.
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
                "cannot package an empty page list into a CB7 archive"
            )

        if output.suffix.lower() != _REQUIRED_SUFFIX:
            raise PackagingError(
                f"CB7 packager requires a {_REQUIRED_SUFFIX!r} output "
                f"path, got {output.name!r}"
            )

        preset = _resolve_preset(compression_level)

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
        os.close(fd)
        temp_path = Path(raw_temp)

        filters = [{"id": lzma.FILTER_LZMA2, "preset": preset}]

        try:
            with py7zr.SevenZipFile(temp_path, mode="w", filters=filters) as archive:
                if comic_info_bytes is not None:
                    archive.writef(
                        io.BytesIO(comic_info_bytes),
                        _COMIC_INFO_ENTRY,
                    )
                for _page, src in sorted_pages:
                    archive.write(src, arcname=src.name)
            _fsync_and_replace(temp_path, output)
        except (OSError, ValueError, py7zr.Bad7zFile, py7zr.PasswordRequired) as exc:
            temp_path.unlink(missing_ok=True)
            raise PackagingError(
                f"failed to create CB7 archive {output}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except BaseException:
            # ``CancelledError`` and any other BaseException must clean up
            # the temp file before propagating. ``py7zr``'s own exceptions
            # (``Bad7zFile``, ``PasswordRequired``) are ``Exception``
            # subclasses and are handled by the branch above; this branch
            # exists for the general cleanup obligation.
            temp_path.unlink(missing_ok=True)
            raise

        _log.info(
            "Packaged CB7 archive: {} ({} pages{}, preset={})",
            output,
            len(sorted_pages),
            ", with ComicInfo.xml" if comic_info_bytes is not None else "",
            preset,
        )
        return output


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #
# NOTE: ``_serialize_metadata`` and ``_fsync_and_replace`` duplicate the
# implementations in ``cbz_packager.py`` (and, for fsync+replace,
# ``zip_packager.py`` and ``pdf_packager.py``). The duplication is
# deliberate and reviewed: the helpers are ~10 lines each, stable, and
# extracting them now would mean reopening already-delivered files. When
# ``core/utils/filesystem.py`` is written, all four packagers will import
# from there and this duplication disappears in a single edit.
# --------------------------------------------------------------------------- #


def _resolve_preset(level: int | None) -> int:
    """Map *level* to an LZMA2 preset integer.

    Args:
        level: ``None`` for the default, ``0..9`` for an explicit preset.

    Returns:
        An integer in ``[0, 9]`` suitable for
        ``{"id": lzma.FILTER_LZMA2, "preset": <int>}``.

    Raises:
        PackagingError: If *level* is not ``None`` and not an ``int`` in
            ``[0, 9]``.
    """
    if level is None:
        return _DEFAULT_COMPRESSION_LEVEL
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
    return level


def _serialize_metadata(metadata: ComicInfo | None) -> bytes | None:
    """Serialize *metadata* to UTF-8 bytes for embedding, or return ``None``.

    A ``None`` *metadata* produces ``None`` — the CB7 packager does not
    insert a stub ``ComicInfo.xml``, for the same reason as the CBZ
    packager: a stub would override reader inference and produce worse
    results than no entry at all.

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


def _fsync_and_replace(temp_path: Path, target: Path) -> None:
    """``fsync`` *temp_path* then atomically replace *target*.

    Args:
        temp_path: The just-written temporary file.
        target: The final destination path.

    Raises:
        OSError: If the fsync or the rename fails.
    """
    with temp_path.open("rb+") as fh:
        os.fsync(fh.fileno())
    temp_path.replace(target)
