"""Reduce the byte size of already-validated images without changing format.

The optimizer is the third leg of the image pipeline, alongside the
validator and the (future) converter. Each has a strict, non-overlapping
scope:

* **Validator** — is this a valid image at all? (``validator.py``)
* **Optimizer** — can this image be re-encoded to fewer bytes *in the same
  format*, with imperceptible quality loss? (**this module**)
* **Converter** — should this image become a different format?
  (``converter.py``)

Because the optimizer stays in the source format, it can assume a much
weaker contract than the converter: no pixel data changes shape, no alpha
channels are flattened, no color space is converted. It is a pure "same
image, fewer bytes" pass.

What the optimizer does
-----------------------
Three classes of optimization, all in-format:

1. **Encoding tuning (always applied when the format is supported)**
   * JPEG: re-encode at :attr:`OptimizationOptions.jpeg_quality`
     (default 85), with progressive scan enabled, Huffman-table
     optimization, and Pillow's ``optimize=True``.
   * PNG: re-encode with ``optimize=True`` and
     :attr:`OptimizationOptions.png_compress_level` (default 9, maximum
     zlib compression).
   * WebP: re-encode with :attr:`OptimizationOptions.webp_quality`
     (default 85), :attr:`~OptimizationOptions.webp_method` (default 4),
     and Pillow's own lossy/lossless mode selector.

2. **Metadata stripping (on by default)**
   Removes EXIF, XMP, Photoshop blocks, and embedded comments from the
   image before re-encoding. Preserves ICC profile (color accuracy) and
   PNG/WebP transparency. Applied via
   :func:`_strip_metadata_from_info` — an explicit whitelist so future
   Pillow metadata keys never leak through silently.

3. **EXIF orientation baking (on by default)**
   Applies :func:`PIL.ImageOps.exif_transpose` so the pixel data is
   physically rotated according to the EXIF orientation tag, then strips
   that tag. Result: an image that displays correctly in any reader,
   regardless of EXIF support.

What the optimizer **does not** do
----------------------------------
* It never changes the format. BMP, TIFF, GIF, AVIF, and any format outside
  :data:`_OPTIMIZABLE_FORMATS` are passed through unchanged (with a
  ``reason`` recorded). Convert them first with ``converter.py`` if a
  format change is wanted.
* It never applies palette reduction, aggressive quantization, or
  chroma-subsampling overrides. Those are lossy heuristics with
  format-specific trade-offs and belong in an explicit user-facing
  "compress aggressively" mode, not in the default optimizer.
* It never writes an output file that is *larger* than the input, unless
  :attr:`OptimizationOptions.skip_if_not_smaller` is explicitly disabled.
  A broken original (already-optimal encoding) stays intact by default.

Filesystem safety
-----------------
* **In-place optimization** (``destination=None``) uses a unique temp file
  in the same directory, then :meth:`pathlib.Path.replace` for an atomic
  swap. A crash mid-optimization leaves the original file untouched.
* **Out-of-place optimization** (``destination=P``) writes to ``P`` and
  leaves the source untouched, even if the optimization did not produce a
  smaller file (the original bytes are copied verbatim in that case).
* **Failure** (any exception inside Pillow or the filesystem) is reported
  in :attr:`OptimizationResult.error`; the function never raises for a
  normal optimization failure.

Async-first, thread-safe
------------------------
Both public functions are coroutines, because Pillow encoding is
CPU-bound and can take hundreds of milliseconds per image. They delegate
to ``asyncio.to_thread`` so the event loop stays responsive, and the
internal ``_sync`` functions are the pure implementations that tests can
call directly. No global state, no module-level caches, no locks.

Integration contract
--------------------
The worker (``core/downloader/worker.py``) does **not** currently call the
optimizer — optimization is an opt-in stage the composition root wires in
if the user enables it. Two integration patterns are supported:

**Pattern A — optimize in place after validation**

    result = await optimize_file(dest)
    if result.error is not None:
        raise PageDownloadError(f"optimization failed: {result.error}")

**Pattern B — optimize to a staging directory**

    result = await optimize_file(
        download_tmp_path,
        destination=final_pages_dir / page.filename,
    )

Both are safe on failure: the worker either sees the original bytes (in
place, crash-safe via temp+swap) or the destination is not written at all
(out of place).

Dependency graph
----------------
Only :mod:`PIL`, :mod:`loguru`, and the standard library. No
``core.exceptions`` (errors are reported via the result, not raised). No
``parsers/``, ``plugins/``, or ``interfaces/`` dependency (PROMPT MASTER
rule R1).

See also
--------
* :mod:`nexusdl.core.image.validator` — must run *before* the optimizer.
* :mod:`nexusdl.core.image.converter` — for format changes.
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from loguru import logger
from PIL import Image, ImageOps, UnidentifiedImageError


if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


__all__ = [
    "DEFAULT_OPTIONS",
    "OptimizationOptions",
    "OptimizationResult",
    "optimize_bytes",
    "optimize_file",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Formats this module can optimize. Everything else is passed through
#: unchanged. Extending this set requires adding a matching branch in
#: :func:`_build_save_kwargs` — a unit test iterating this set ensures they
#: stay in sync.
_OPTIMIZABLE_FORMATS: Final[frozenset[str]] = frozenset(
    {"jpeg", "png", "webp"}
)

#: ``img.info`` keys that are unconditionally stripped when
#: :attr:`OptimizationOptions.strip_metadata` is ``True``. This is an
#: **allowlist** (not a denylist) so that new metadata keys Pillow adds in
#: future versions do not silently leak through.
#:
#: ``icc_profile`` is intentionally **not** stripped — it affects color
#: accuracy and its absence on an image that relied on it can produce
#: visibly shifted colors in color-managed readers.
#:
#: ``transparency`` is intentionally **not** stripped — it carries the
#: alpha mask for PNG and WebP and must survive re-encoding.
_STRIPPABLE_INFO_KEYS: Final[frozenset[str]] = frozenset(
    {
        "exif",
        "xmp",
        "XML:com.adobe.xmp",
        "photoshop",
        "comment",
        "Software",
    }
)

#: Pillow format name (uppercase) for each optimizable format (lowercase).
#: ``img.save(..., format=...)`` requires the uppercase form; ``img.format``
#: returns the uppercase form already on some Pillow versions and the
#: lowercase form on others. Normalising here prevents surprises.
_PILLOW_FORMAT_NAMES: Final[dict[str, str]] = {
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
}


_log = logger.bind(component="image.optimizer")


# --------------------------------------------------------------------------- #
# Options and result                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OptimizationOptions:
    """Bounds and choices applied to every optimization.

    Every field is validated at construction time — an invalid options
    object can never reach the optimization code. Fields are grouped by
    concern so a reader can find them by intent rather than by accident.

    Attributes:
        enabled: Master switch. When ``False``, every public function is a
            no-op passthrough that reports ``reason="optimization disabled"``.
            Useful for users who want the code path but not the work.
        skip_if_not_smaller: When ``True`` (default), a re-encoded image that
            is **not** smaller than the original is discarded and the
            original is kept. This is the safety net against pathological
            inputs (already-optimal files, images where re-encoding at the
            chosen quality inflates size, corrupt files that Pillow
            re-encodes poorly). Set to ``False`` only if you actually want
            a potentially larger output file.
        apply_exif_orientation: When ``True`` (default), the EXIF orientation
            tag is baked into the pixel data via
            :func:`PIL.ImageOps.exif_transpose` before re-encoding. Result:
            the image displays correctly in any reader. When ``False``, the
            pixel orientation is preserved as-is (tag included, unless
            :attr:`strip_metadata` removes it).
        strip_metadata: When ``True`` (default), removes the keys listed in
            :data:`_STRIPPABLE_INFO_KEYS` from ``img.info`` before saving.
            ICC profile and transparency are always preserved.
        jpeg_quality: JPEG quality, in ``[1, 95]``. Defaults to 85 — the
            standard "visually lossless for line art" value. Values above
            95 provide no perceptible quality gain and materially increase
            file size, so they are rejected.
        jpeg_progressive: When ``True`` (default), encode JPEG as
            progressive. Smaller files for most manga content; supported
            by every modern reader (Komga, Kavita, Mihon, YACReader,
            CDisplayEx).
        png_compress_level: zlib compression level for PNG, in ``[0, 9]``.
            Defaults to 9 (maximum). Pillow's ``optimize=True`` is always
            set alongside this — the two together consistently produce
            the smallest lossless PNG.
        webp_quality: Lossy WebP quality, in ``[1, 100]``. Defaults to 85.
            Ignored when :attr:`webp_lossless` is ``True``.
        webp_method: WebP encoder effort, in ``[0, 6]``. Higher is slower
            but produces smaller files. Defaults to 4 — the sweet spot
            between encoding time and output size.
        webp_lossless: When ``True``, use WebP's lossless mode. Defaults to
            ``False`` — lossy WebP at q=85 is virtually always smaller than
            lossless for photographic content, while remaining visually
            indistinguishable from lossless for the manga line art the
            pipeline mostly handles.
        excluded_formats: Lowercase format identifiers the user explicitly
            does not want optimized. Useful for keeping particular formats
            at original quality (e.g. ``frozenset({"png"})`` to leave PNGs
            untouched). Applied in addition to the fixed
            :data:`_OPTIMIZABLE_FORMATS` list — both must pass.
    """

    enabled: bool = True
    skip_if_not_smaller: bool = True
    apply_exif_orientation: bool = True
    strip_metadata: bool = True

    jpeg_quality: int = 85
    jpeg_progressive: bool = True

    png_compress_level: int = 9

    webp_quality: int = 85
    webp_method: int = 4
    webp_lossless: bool = False

    excluded_formats: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Validate field ranges.

        Raises:
            ValueError: If any quality or compression parameter is out of
                its documented range, or if ``excluded_formats`` contains a
                value that is not a lowercase string.
        """
        if not (1 <= self.jpeg_quality <= 95):
            raise ValueError(
                f"jpeg_quality must be in [1, 95], got {self.jpeg_quality}"
            )
        if not (0 <= self.png_compress_level <= 9):
            raise ValueError(
                f"png_compress_level must be in [0, 9], "
                f"got {self.png_compress_level}"
            )
        if not (1 <= self.webp_quality <= 100):
            raise ValueError(
                f"webp_quality must be in [1, 100], got {self.webp_quality}"
            )
        if not (0 <= self.webp_method <= 6):
            raise ValueError(
                f"webp_method must be in [0, 6], got {self.webp_method}"
            )
        for fmt in self.excluded_formats:
            if fmt != fmt.lower():
                raise ValueError(
                    f"excluded_formats must contain lowercase identifiers, "
                    f"got {fmt!r}"
                )


#: Module-wide defaults. Callers that do not pass ``options`` explicitly use
#: this object.
DEFAULT_OPTIONS: Final[OptimizationOptions] = OptimizationOptions()


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """Outcome of an optimization attempt.

    Immutable and safe to cache. ``was_optimized`` is the only field a
    caller needs to branch on for the happy path; ``reason`` is populated
    when ``was_optimized`` is ``False`` and there is something to explain
    (disabled, excluded format, not smaller, unsupported format). ``error``
    is populated when the optimization failed — a distinct signal from
    "chose not to optimize".

    Invariants:

    * ``was_optimized=True`` ⟹ ``optimized_size < original_size`` (unless
      :attr:`OptimizationOptions.skip_if_not_smaller` was ``False``, in
      which case the two sizes are unrelated but ``optimized_size`` is the
      size of the written bytes).
    * ``was_optimized=False and error is None`` ⟹ ``optimized_size ==
      original_size`` (no data was written).
    * ``error is not None`` ⟹ ``was_optimized=False`` and the caller's
      original data is intact.

    Attributes:
        was_optimized: ``True`` iff the output is a re-encoded image that
            was actually written (or returned, for :func:`optimize_bytes`).
        original_size: Size of the input, in bytes.
        optimized_size: Size of the output, in bytes. Equal to
            ``original_size`` when no optimization was performed.
        format: Lowercase Pillow format identifier, or ``None`` when the
            format could not be determined.
        reason: Human-readable explanation when ``was_optimized`` is
            ``False`` **and** ``error is None``. E.g. ``"format 'gif' is
            not optimizable"``, ``"re-encoded version was not smaller"``.
        error: Human-readable error message when the optimization failed.
            ``None`` on success and on deliberate no-ops.
        output_path: Filesystem path the optimized data was written to,
            when produced by :func:`optimize_file`; ``None`` for
            :func:`optimize_bytes`.
    """

    was_optimized: bool
    original_size: int
    optimized_size: int
    format: str | None = None
    reason: str | None = None
    error: str | None = None
    output_path: Path | None = None

    @property
    def savings_bytes(self) -> int:
        """Absolute size reduction in bytes; ``0`` when nothing was saved."""
        return max(0, self.original_size - self.optimized_size)

    @property
    def savings_ratio(self) -> float:
        """Size reduction as a fraction of the original in ``[0.0, 1.0]``.

        Returns ``0.0`` for an empty original (undefined ratio).
        """
        if self.original_size == 0:
            return 0.0
        return self.savings_bytes / self.original_size


# --------------------------------------------------------------------------- #
# Public API — async                                                     #
# --------------------------------------------------------------------------- #


async def optimize_bytes(
    data: bytes,
    *,
    options: OptimizationOptions | None = None,
) -> tuple[bytes, OptimizationResult]:
    """Optimize *data* as an image and return ``(output_bytes, result)``.

    Never raises for an optimization failure — the reason is reported in
    ``result.error`` and ``result.was_optimized`` is ``False``. When
    ``was_optimized`` is ``False``, the returned bytes are identical to
    *data*; when ``True``, they are the smaller re-encoded image.

    Pillow work runs in a worker thread (via ``asyncio.to_thread``) so the
    event loop is not blocked during encoding.

    Args:
        data: Raw image bytes. Must have been produced by
            :mod:`nexusdl.core.image.validator` (or otherwise be a valid
            image) — this function does not re-run validation.
        options: Bounds and choices. When ``None``, :data:`DEFAULT_OPTIONS`
            is used.

    Returns:
        A ``(bytes, OptimizationResult)`` tuple. The bytes are always safe
        to write to disk regardless of ``result.was_optimized``.
    """
    opts = options if options is not None else DEFAULT_OPTIONS
    return await asyncio.to_thread(_optimize_bytes_sync, data, opts)


async def optimize_file(
    source: Path,
    *,
    destination: Path | None = None,
    options: OptimizationOptions | None = None,
) -> OptimizationResult:
    """Optimize *source* and (optionally) write the result.

    Behaviour depends on *destination*:

    * ``destination=None`` — in-place optimization. A unique temp file is
      created in the same directory as *source*, the optimized bytes are
      written to it, and :meth:`Path.replace` swaps it in atomically. A
      crash mid-optimization leaves the original untouched.
    * ``destination=P`` where ``P != source`` — out-of-place optimization.
      The optimized bytes are written to ``P``; ``source`` is untouched.
      If ``P == source`` (same path), behaves like in-place.
    * **Failure or no-op** — when the optimizer fails, decides not to
      optimize (e.g. ``skip_if_not_smaller`` fired), or the format is not
      optimizable, the original bytes are written verbatim to
      *destination* (when set and different from *source*), or nothing is
      written (in-place). Either way, the caller's data is preserved.

    The whole operation (read, optimize, write) runs in a worker thread so
    the event loop is not blocked.

    Args:
        source: Path to the image to optimize. Must exist and be readable.
        destination: Where to write the result. ``None`` means in-place
            (safe via temp+swap). When a different path, the parent
            directory is created if necessary.
        options: Bounds and choices. When ``None``, :data:`DEFAULT_OPTIONS`
            is used.

    Returns:
        An :class:`OptimizationResult` with ``output_path`` set to the path
        the resulting bytes ended up at (``source`` for in-place, the given
        ``destination`` for out-of-place).
    """
    opts = options if options is not None else DEFAULT_OPTIONS
    return await asyncio.to_thread(
        _optimize_file_sync, source, destination, opts
    )


# --------------------------------------------------------------------------- #
# Internal — pure synchronous implementations                                #
# --------------------------------------------------------------------------- #


def _optimize_bytes_sync(
    data: bytes,
    options: OptimizationOptions,
) -> tuple[bytes, OptimizationResult]:
    """Synchronous implementation of :func:`optimize_bytes`.

    Extracted so tests can call it directly without an event loop, and so
    :func:`_optimize_file_sync` can reuse it without a nested thread hop.
    """
    original_size = len(data)

    if not options.enabled:
        return data, OptimizationResult(
            was_optimized=False,
            original_size=original_size,
            optimized_size=original_size,
            reason="optimization disabled",
        )

    if original_size == 0:
        return data, OptimizationResult(
            was_optimized=False,
            original_size=0,
            optimized_size=0,
            error="file is empty",
        )

    try:
        with Image.open(io.BytesIO(data)) as img:
            raw_format = img.format or ""
            fmt = raw_format.lower()

            if not fmt:
                return data, OptimizationResult(
                    was_optimized=False,
                    original_size=original_size,
                    optimized_size=original_size,
                    error="Pillow reported no format after open",
                )

            if fmt in options.excluded_formats:
                return data, OptimizationResult(
                    was_optimized=False,
                    original_size=original_size,
                    optimized_size=original_size,
                    format=fmt,
                    reason=f"format {fmt!r} excluded by user",
                )

            if fmt not in _OPTIMIZABLE_FORMATS:
                return data, OptimizationResult(
                    was_optimized=False,
                    original_size=original_size,
                    optimized_size=original_size,
                    format=fmt,
                    reason=f"format {fmt!r} is not optimizable",
                )

            # Bake EXIF orientation into pixels, if requested.
            working: PILImage = img
            owns_working = False
            if options.apply_exif_orientation:
                transposed = ImageOps.exif_transpose(img)
                if transposed is not img:
                    working = transposed
                    owns_working = True

            try:
                if options.strip_metadata:
                    _strip_metadata_from_info(working)

                save_kwargs = _build_save_kwargs(fmt, options)
                pillow_format = _PILLOW_FORMAT_NAMES[fmt]

                buf = io.BytesIO()
                working.save(buf, format=pillow_format, **save_kwargs)
                optimized = buf.getvalue()
            finally:
                if owns_working:
                    working.close()

    except Image.DecompressionBombError as exc:
        _log.debug("Rejected optimization: decompression bomb ({})", exc)
        return data, OptimizationResult(
            was_optimized=False,
            original_size=original_size,
            optimized_size=original_size,
            error=f"decompression bomb: {exc}",
        )
    except UnidentifiedImageError as exc:
        _log.debug("Rejected optimization: unrecognised format ({})", exc)
        return data, OptimizationResult(
            was_optimized=False,
            original_size=original_size,
            optimized_size=original_size,
            error=f"unrecognised image format: {exc}",
        )
    except (OSError, ValueError, SyntaxError) as exc:
        _log.debug("Rejected optimization: re-encode failed ({})", exc)
        return data, OptimizationResult(
            was_optimized=False,
            original_size=original_size,
            optimized_size=original_size,
            error=f"failed to re-encode image: {exc}",
        )

    optimized_size = len(optimized)
    if options.skip_if_not_smaller and optimized_size >= original_size:
        _log.debug(
            "Optimization produced {} bytes (>= {} original); discarding",
            optimized_size,
            original_size,
        )
        return data, OptimizationResult(
            was_optimized=False,
            original_size=original_size,
            optimized_size=original_size,
            format=fmt,
            reason="re-encoded version was not smaller",
        )

    _log.trace(
        "Optimized {}: {} -> {} bytes ({:.1f}% saved)",
        fmt,
        original_size,
        optimized_size,
        100.0 * (original_size - optimized_size) / original_size,
    )
    return optimized, OptimizationResult(
        was_optimized=True,
        original_size=original_size,
        optimized_size=optimized_size,
        format=fmt,
    )


def _optimize_file_sync(
    source: Path,
    destination: Path | None,
    options: OptimizationOptions,
) -> OptimizationResult:
    """Synchronous implementation of :func:`optimize_file`.

    See :func:`optimize_file` for the full behaviour matrix. This function
    is what actually touches the filesystem — the only I/O the optimizer
    performs is here.
    """
    if not source.is_file():
        return OptimizationResult(
            was_optimized=False,
            original_size=0,
            optimized_size=0,
            error=f"source file does not exist: {source}",
        )

    try:
        data = source.read_bytes()
    except OSError as exc:
        return OptimizationResult(
            was_optimized=False,
            original_size=0,
            optimized_size=0,
            error=f"cannot read source file {source}: {exc}",
        )

    optimized_data, result = _optimize_bytes_sync(data, options)

    # If optimization failed or chose not to run, the bytes we should end
    # up writing (when out-of-place) are the original ones.
    payload = optimized_data if result.was_optimized else data

    target = destination if destination is not None else source
    in_place = target == source

    if in_place:
        try:
            _atomic_write(source, payload)
        except OSError as exc:
            return replace(
                result,
                was_optimized=False,
                optimized_size=result.original_size,
                error=f"cannot write {source}: {exc}",
                output_path=source,
            )
        return replace(result, output_path=source)

    # Out-of-place write.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    except OSError as exc:
        return replace(
            result,
            was_optimized=False,
            optimized_size=result.original_size,
            error=f"cannot write {target}: {exc}",
            output_path=None,
        )
    return replace(result, output_path=target)


# --------------------------------------------------------------------------- #
# Internals — save kwargs and metadata                                       #
# --------------------------------------------------------------------------- #


def _build_save_kwargs(
    fmt: str, options: OptimizationOptions
) -> dict[str, Any]:
    """Return the Pillow ``save()`` kwargs for *fmt*.

    Every branch corresponds to one entry in :data:`_OPTIMIZABLE_FORMATS`.
    A test iterates both to guarantee they stay in lockstep. The function
    is pure: it never mutates the image, only builds a kwargs dict.

    Args:
        fmt: Lowercase format identifier, guaranteed to be in
            :data:`_OPTIMIZABLE_FORMATS` by the caller.
        options: User-provided optimization choices.

    Returns:
        A dict ready to be splatted into ``Image.save(..., **kwargs)``.
    """
    if fmt == "jpeg":
        return {
            "quality": options.jpeg_quality,
            "progressive": options.jpeg_progressive,
            "optimize": True,
        }
    if fmt == "png":
        return {
            "optimize": True,
            "compress_level": options.png_compress_level,
        }
    if fmt == "webp":
        return {
            "quality": options.webp_quality,
            "method": options.webp_method,
            "lossless": options.webp_lossless,
        }
    # Unreachable: _OPTIMIZABLE_FORMATS and this function are kept in sync
    # by a unit test. Raising here turns a future drift into a loud failure
    # rather than a silent no-op.
    raise ValueError(
        f"no save kwargs defined for format {fmt!r}; "
        f"_OPTIMIZABLE_FORMATS and _build_save_kwargs are out of sync"
    )


def _strip_metadata_from_info(img: PILImage) -> None:
    """Remove the allowlisted metadata keys from *img*'s ``info`` dict.

    An allowlist (rather than a denylist) guarantees that new metadata keys
    added by future Pillow versions never leak through silently. ICC
    profile and transparency are deliberately retained — see
    :data:`_STRIPPABLE_INFO_KEYS`.

    Args:
        img: The image whose metadata should be stripped. Mutated in place.
    """
    for key in _STRIPPABLE_INFO_KEYS:
        img.info.pop(key, None)


# --------------------------------------------------------------------------- #
# Internals — atomic file writes                                             #
# --------------------------------------------------------------------------- #


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write *payload* to *target* atomically via a sibling temp file.

    Uses :func:`tempfile.mkstemp` so concurrent writers of the same target
    never collide on the temp path. On any failure, the temp file is
    removed and the original *target* is left untouched — the caller sees
    an ``OSError`` and can report it, but the data on disk is intact.

    Args:
        target: Final path to write to.
        payload: Bytes to write.

    Raises:
        OSError: If any step (temp creation, write, replace) fails.
    """
    fd, raw_temp = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.opt-",
        suffix=".tmp",
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        temp_path.replace(target)
    except OSError:
        # Best-effort cleanup. If unlink itself fails, we still want the
        # original exception to propagate, so swallow the unlink failure.
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
