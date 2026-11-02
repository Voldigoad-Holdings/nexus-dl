"""Package chapter pages into a single PDF, lossless.

The PDF packager is the odd one out of the packager family: it does not
produce a container of image files, it produces **one document** with one
page per chapter page. The other packagers (FOLDER, ZIP, CBZ, CB7, CBT)
preserve the individual images; PDF embeds them losslessly in a document
format that manga readers (YACReader, CDisplayEx, MComix) accept.

Lossless by design
------------------
:mod:`img2pdf` embeds JPEG, JPEG2000, and PNG data **without
re-encoding**, wrapping the existing byte stream in PDF container
objects. The output PDF is (page-for-page) byte-for-byte equivalent to
the input images. This is the entire reason ``img2pdf`` is preferred over
the obvious alternative (Pillow's ``save("pdf")``), which re-encodes
every image and silently degrades quality on repeated conversions.

Consequently, :attr:`PdfPackager.format` is documented in the PROMPT
MASTER as "lossless", and this packager **does not offer a
quality/compression knob**. The :attr:`compression_level` parameter is
accepted for interface uniformity with ZIP/CBZ/CB7 and **ignored** with a
``DEBUG`` log, because ``img2pdf`` has no concept of a compression level.

Format handling
---------------
``img2pdf`` natively embeds JPEG and PNG files. Everything else —
WebP, AVIF, BMP, TIFF, and any future format the image pipeline supports —
is converted to PNG first (losslessly, via Pillow) in a temporary
directory and then embedded. The two-step process is transparent and only
invoked when needed:

1. Try to embed the source files directly.
2. If ``img2pdf`` rejects any of them (unknown format, alpha channel
   edge case, corrupted container), convert **all** pages to PNG and
   retry once.
3. If the retry also fails, raise :class:`~nexusdl.core.exceptions.PackagingError`.

Multi-frame images (multi-page TIFF, animated GIF — though the validator
rejects GIFs) are converted to their **first frame** with a ``WARNING``
log entry, matching the converter's behaviour. Silent page loss is never
acceptable; the log makes the trade-off visible.

PDF metadata from ComicInfo
---------------------------
PDF has no convention for embedding a full ``ComicInfo.xml``; the
ComicRack metadata is a ZIP-family concept. What PDF does support is a
small set of document properties (title, author, subject, keywords,
creation date), and the packager maps the corresponding
:class:`~nexusdl.core.packaging.comic_info.ComicInfo` fields onto them:

* ``ComicInfo.title`` → PDF ``Title``
* ``ComicInfo.writer`` → PDF ``Author``
* ``ComicInfo.summary`` → PDF ``Subject``
* ``ComicInfo.genres`` → PDF ``Keywords`` (comma-joined)
* ``ComicInfo.year`` / ``month`` / ``day`` → PDF ``CreationDate``

Fields absent from the ``ComicInfo`` are omitted from the PDF; no
placeholder values are inserted. A ``ComicInfo`` that carries only the
five required fields produces a PDF with only a ``Title``. This matches
the "ComicInfo is optional" contract used everywhere else in the
pipeline.

Filesystem safety and cancellation
----------------------------------
Writes are atomic: the PDF is built in a
:class:`tempfile.TemporaryDirectory` inside the output directory, then
``fsync``'d and :meth:`Path.replace`'d onto the final path. The temp
directory is guaranteed to be cleaned up even if the async caller is
cancelled mid-build, because cleanup happens inside the worker thread
via the ``TemporaryDirectory`` context manager, not in the caller.

Async model
-----------
:meth:`PdfPackager.package` is a coroutine; the actual PDF build runs in
a worker thread via :func:`asyncio.to_thread`, so the event loop stays
responsive during what can be a multi-second operation for a long
chapter.

Dependency graph (no cycles)::

    core.exceptions        ─┐
    core.models.enums       ├──► core.packaging.pdf_packager
    core.models.page        │
    core.packaging.base    ─┘
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import img2pdf
from loguru import logger
from PIL import Image

from nexusdl.core.exceptions import PackagingError
from nexusdl.core.models.enums import PackagingFormat
from nexusdl.core.models.page import Page
from nexusdl.core.packaging.base import BasePackager

if TYPE_CHECKING:
    from nexusdl.core.packaging.comic_info import ComicInfo


__all__ = ["PdfPackager"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Required extension for the output path. A mismatch (``.cbz``, ``.zip``,
#: no extension) means the caller chose the wrong packager.
_REQUIRED_SUFFIX: Final[str] = ".pdf"

#: Prefix pattern for the temporary working directory. Matches the
#: convention used by the other packagers so a stray temp directory is
#: recognisable.
_TEMP_DIR_PREFIX_FMT: Final[str] = ".{name}.pdf-"

#: Formats that ``img2pdf`` can embed directly, without any Pillow
#: intervention. Lowercase ``PIL.Image.Image.format`` values.
#: Everything else is converted to PNG before being handed to img2pdf.
_IMG2PDF_NATIVE_FORMATS: Final[frozenset[str]] = frozenset({"jpeg", "png"})

#: Filename for the intermediate PDF inside the temp directory. Never
#: visible to the caller; the final file is produced by ``Path.replace``.
_INTERMEDIATE_PDF_NAME: Final[str] = "output.pdf"

#: Filename template for a PNG conversion during the fallback path. The
#: zero-padded ``page.index`` preserves reading order and prevents
#: filename collisions.
_PNG_TEMPLATE: Final[str] = "page-{index:06d}.png"

#: White background color used when flattening alpha channels before PNG
#: conversion. Same value used by the image converter for JPEG targets.
_BACKGROUND: Final[tuple[int, int, int]] = (255, 255, 255)


_log = logger.bind(component="packaging.pdf")


# --------------------------------------------------------------------------- #
# PdfPackager                                                                 #
# --------------------------------------------------------------------------- #


class PdfPackager(BasePackager):
    """Package chapter pages into a single, lossless ``.pdf``.

    Stateless. One instance may be reused across concurrent
    :meth:`package` calls — no instance-level lock, no shared mutable
    state.

    See the module docstring for the full contract.
    """

    format = PackagingFormat.PDF

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
        """Package *pages* into a single PDF at *output*.

        Args:
            pages: The chapter's pages, each paired with the file the
                worker downloaded it to. Order does not matter — the
                packager sorts by :attr:`Page.index` before building the
                PDF.
            output: Destination ``.pdf`` path. Its extension must be
                ``.pdf`` (case-insensitive); the parent directory is
                created if it does not exist. An existing file at this
                path is replaced atomically.
            metadata: Optional
                :class:`~nexusdl.core.packaging.comic_info.ComicInfo`.
                A subset of its fields is mapped onto PDF document
                properties; see the module docstring for the mapping.
                ``None`` produces a PDF with no metadata (only the
                reader's defaults).
            compression_level: **Ignored** — img2pdf is lossless by
                design and has no compression level. A non-``None`` value
                produces a ``DEBUG`` log entry so a caller who passed it
                by mistake can see the fact.

        Returns:
            *output*, unchanged.

        Raises:
            PackagingError: If *pages* is empty, *output* has the wrong
                extension, a source file is missing, or the PDF build
                fails at any step (including after the PNG-conversion
                fallback).
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
                "cannot package an empty page list into a PDF"
            )

        if output.suffix.lower() != _REQUIRED_SUFFIX:
            raise PackagingError(
                f"PDF packager requires a {_REQUIRED_SUFFIX!r} output "
                f"path, got {output.name!r}"
            )

        if compression_level is not None:
            _log.debug(
                "PdfPackager ignores compression_level={} — img2pdf is "
                "lossless by design",
                compression_level,
            )

        sorted_pages = sorted(pages, key=lambda pair: pair[0].index)
        for page, src in sorted_pages:
            if not src.is_file():
                raise PackagingError(
                    f"source file for page {page.index} does not exist: "
                    f"{src}"
                )

        metadata_kwargs = _build_metadata_kwargs(metadata)

        output.parent.mkdir(parents=True, exist_ok=True)

        # Everything happens inside a TemporaryDirectory so that an
        # abandoned thread (caller cancelled via to_thread) still cleans
        # up its intermediate files when the thread eventually finishes.
        with tempfile.TemporaryDirectory(
            dir=output.parent,
            prefix=_TEMP_DIR_PREFIX_FMT.format(name=output.name),
        ) as raw_tmpdir:
            tmpdir = Path(raw_tmpdir)
            intermediate = tmpdir / _INTERMEDIATE_PDF_NAME

            direct_paths = [src for _, src in sorted_pages]
            try:
                _write_pdf(direct_paths, intermediate, metadata_kwargs)
            except Exception as exc:  # noqa: BLE001 — see rationale below
                # ``img2pdf`` raises a number of format-specific
                # exceptions (ImageOpenError, AlphaChannelError,
                # PdfTooLargeError, and ``OSError``/``ValueError`` from
                # Pillow's own decoder). Rather than enumerate them all —
                # which would go stale as soon as a new Pillow plugin
                # raises something new — we treat any failure as a signal
                # to retry with the PNG-conversion fallback.
                #
                # ``BaseException`` (including ``CancelledError``) is
                # **not** caught here, because that would swallow
                # cancellation. The fallback only handles ordinary
                # ``Exception`` subclasses, which is what image and I/O
                # failures are.
                _log.debug(
                    "Direct img2pdf failed ({}: {}); converting all "
                    "pages to PNG and retrying",
                    type(exc).__name__,
                    exc,
                )
                converted_paths = _convert_pages_to_png(
                    sorted_pages, tmpdir
                )
                try:
                    _write_pdf(
                        converted_paths, intermediate, metadata_kwargs
                    )
                except Exception as exc2:  # noqa: BLE001
                    raise PackagingError(
                        f"failed to create PDF {output}: {exc2}"
                    ) from exc2

            try:
                _fsync_and_replace(intermediate, output)
            except OSError as exc:
                raise PackagingError(
                    f"cannot finalise PDF {output}: {exc}"
                ) from exc

        _log.info(
            "Packaged PDF: {} ({} pages)",
            output,
            len(sorted_pages),
        )
        return output


# --------------------------------------------------------------------------- #
# Internal helpers                                                           #
# --------------------------------------------------------------------------- #


def _write_pdf(
    image_paths: Sequence[Path],
    output_path: Path,
    metadata_kwargs: dict[str, Any],
) -> None:
    """Run ``img2pdf`` to build the PDF at *output_path*.

    No exception translation: the caller inspects any failure and decides
    whether to retry with the PNG-conversion fallback.

    Args:
        image_paths: Paths to the images to embed, in reading order.
        output_path: Where to write the PDF.
        metadata_kwargs: Keyword arguments to pass to
            :func:`img2pdf.convert`. Empty when no metadata is available.
    """
    with output_path.open("wb") as fh:
        img2pdf.convert(
            *[str(path) for path in image_paths],
            outputstream=fh,
            **metadata_kwargs,
        )


def _convert_pages_to_png(
    pages: Sequence[tuple[Page, Path]],
    tmpdir: Path,
) -> list[Path]:
    """Convert every page to a PNG file in *tmpdir* and return their paths.

    Used as the fallback when ``img2pdf`` rejects the source images. The
    conversion is lossless in the sense that no *additional* loss is
    introduced beyond what Pillow's re-encode inherently costs — the goal
    is to produce a format img2pdf can embed, not to preserve the exact
    source bytes.

    Args:
        pages: The pages to convert, in reading order.
        tmpdir: Destination directory. Assumed to exist.

    Returns:
        The paths to the PNG files, in the same order as *pages*.
    """
    converted: list[Path] = []
    for page, src in pages:
        dst = tmpdir / _PNG_TEMPLATE.format(index=page.index)
        _flatten_to_png(src, dst)
        converted.append(dst)
    return converted


def _flatten_to_png(src: Path, dst: Path) -> None:
    """Convert *src* to a PNG at *dst*, flattening alpha onto white.

    Args:
        src: Source image file. Must be readable by Pillow.
        dst: Destination PNG path.

    Raises:
        PackagingError: If the source cannot be opened or the PNG cannot
            be written.
    """
    try:
        with Image.open(src) as img:
            frames = getattr(img, "n_frames", 1)
            if isinstance(frames, int) and frames > 1:
                _log.warning(
                    "Multi-frame image {} ({} frames); only the first "
                    "frame will appear in the PDF",
                    src,
                    frames,
                )
            if _has_alpha(img):
                _flatten_alpha_to_png(img, dst)
            else:
                _save_rgb_or_l_to_png(img, dst)
    except PackagingError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure here is "bad image"
        raise PackagingError(
            f"cannot convert {src} to PNG for PDF embedding: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _has_alpha(img: Image.Image) -> bool:
    """Return ``True`` iff *img* carries alpha information."""
    if img.mode in ("RGBA", "LA", "PA"):
        return True
    return img.mode == "P" and "transparency" in img.info


def _flatten_alpha_to_png(img: Image.Image, dst: Path) -> None:
    """Flatten an alpha-carrying image onto white and save as PNG."""
    rgba = img.convert("RGBA")
    try:
        background = Image.new("RGB", rgba.size, _BACKGROUND)
        try:
            background.paste(rgba, mask=rgba.getchannel("A"))
            background.save(dst, format="PNG")
        finally:
            background.close()
    finally:
        rgba.close()


def _save_rgb_or_l_to_png(img: Image.Image, dst: Path) -> None:
    """Save a non-alpha image as PNG, converting exotic modes to RGB."""
    if img.mode in ("RGB", "L"):
        img.save(dst, format="PNG")
        return
    converted = img.convert("RGB")
    try:
        converted.save(dst, format="PNG")
    finally:
        converted.close()


def _build_metadata_kwargs(
    comic_info: ComicInfo | None,
) -> dict[str, Any]:
    """Map the available ``ComicInfo`` fields to img2pdf keyword arguments.

    Args:
        comic_info: The metadata to translate, or ``None``.

    Returns:
        A dict ready to be splatted into :func:`img2pdf.convert`.
        Empty when *comic_info* is ``None`` or provides no translatable
        fields.
    """
    if comic_info is None:
        return {}

    kwargs: dict[str, Any] = {}

    title = getattr(comic_info, "title", None)
    if title:
        kwargs["title"] = str(title)

    writer = getattr(comic_info, "writer", None)
    if writer:
        kwargs["author"] = str(writer)

    summary = getattr(comic_info, "summary", None)
    if summary:
        kwargs["subject"] = str(summary)

    genres = getattr(comic_info, "genres", None)
    if genres:
        try:
            keywords = ", ".join(str(g) for g in genres if g)
        except TypeError:
            keywords = ""
        if keywords:
            kwargs["keywords"] = keywords

    creation_date = _build_pdf_creation_date(comic_info)
    if creation_date is not None:
        kwargs["creationdate"] = creation_date

    return kwargs


def _build_pdf_creation_date(comic_info: ComicInfo) -> str | None:
    """Build a PDF ``CreationDate`` string from the ComicInfo date fields.

    PDF date format is ``D:YYYYMMDDHHmmSS`` — a fixed-width, colon-free
    encoding defined by the PDF specification. Hour, minute, and second
    are set to zero because a manga's publication time of day is not
    known (and is not recorded by ComicInfo in the first place).

    Args:
        comic_info: The metadata carrying ``year`` / ``month`` / ``day``.

    Returns:
        A PDF-format date string, or ``None`` when no usable year is
        present.
    """
    year = getattr(comic_info, "year", None)
    if not isinstance(year, int) or year <= 0 or year > 9999:
        return None
    month = getattr(comic_info, "month", None)
    month = month if isinstance(month, int) and 1 <= month <= 12 else 1
    day = getattr(comic_info, "day", None)
    day = day if isinstance(day, int) and 1 <= day <= 31 else 1
    return f"D:{year:04d}{month:02d}{day:02d}000000"


def _fsync_and_replace(temp_path: Path, target: Path) -> None:
    """``fsync`` *temp_path* then atomically replace *target*.

    Args:
        temp_path: The just-written intermediate file.
        target: The final destination path.

    Raises:
        OSError: If the fsync or the rename fails.
    """
    with temp_path.open("rb+") as fh:
        os.fsync(fh.fileno())
    temp_path.replace(target)
