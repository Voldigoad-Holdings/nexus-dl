"""Convert images between formats, with optional resize and metadata control.

The third and last leg of the image pipeline. Each stage has a strict,
non-overlapping scope:

* **Validator** — is this a valid image at all? (``validator.py``)
* **Optimizer** — can it be re-encoded smaller *in the same format*?
  (``optimizer.py``)
* **Converter** — should it become a **different** format? (**this module**)

Because the converter changes format, it must handle everything the
optimizer can ignore: pixel mode translation, alpha flattening, ICC profile
preservation, and codec availability. It does so through a single, explicit
pipeline — no hidden heuristics, no automatic format choice.

What the converter does
-----------------------
Four transformation stages, in order:

1. **Decode** — open the source, force-decode so corruption surfaces at a
   single point, warn (but continue) if the source has multiple frames
   (only the first is kept).
2. **Resize** — when :attr:`ConversionOptions.max_dimension` is set and
   the larger side exceeds it, scale down preserving aspect ratio. **No
   upscaling** — a 500×500 source stays 500×500 regardless of
   ``max_dimension``.
3. **Mode adaptation** — put the image in a mode the target codec accepts:
   * JPEG target: flatten alpha on :attr:`ConversionOptions.background_color`,
     convert to RGB.
   * WebP / AVIF target: convert to RGBA (alpha preserved) or RGB
     depending on the source.
   * PNG target: preserve the source mode (PNG handles L, LA, RGB, RGBA,
     P, I, F, CMYK natively).
4. **Encode** — save with format-specific kwargs mirroring the optimizer
   where they overlap (quality, progressive, compress level, etc.).

What the converter does **not** do
----------------------------------
* It does **not** refuse same-format conversion. Converting JPEG→JPEG with
  ``max_dimension=1500`` is a valid "resize and strip EXIF" operation.
* It does **not** auto-pick a target format. ``target_format`` is
  mandatory in :class:`ConversionOptions`.
* It does **not** retry. Network errors are not a thing here — the source
  is a local file already validated by the pipeline.
* It does **not** batch. One call, one file. Batch callers loop.

Supported targets
-----------------
:data:`SUPPORTED_TARGET_FORMATS` is computed at import time from
:data:`PIL.Image.SAVE`. Baseline support:

* ``jpeg``, ``png``, ``webp`` — always available in Pillow.
* ``avif`` — available when Pillow ≥ 11 (native) or when the
  ``pillow-avif-plugin`` package is installed. Absent on older installs,
  and the converter will **not** pretend otherwise: an unsupported target
  raises :class:`ValueError` at :class:`ConversionOptions` construction
  time, before any I/O happens.

BMP, TIFF, GIF, and SVG are deliberately **not** conversion targets —
they are either too large (BMP), useless for static manga (TIFF, GIF), or
undecodable by Pillow (SVG). Users who genuinely need them can call
Pillow directly.

Filesystem safety
-----------------
Writes are atomic, using the same temp-file-plus-rename pattern as
:mod:`nexusdl.core.image.optimizer`. A crash mid-conversion leaves the
previous contents of ``dest`` untouched.

Async, thread-safe
------------------
:func:`convert` is a coroutine; the CPU-bound work runs in a worker thread
via ``asyncio.to_thread``. No mutable module state, no locks.

Dependency graph (no cycles)::

    core.exceptions  ← core.image.converter → PIL, loguru, stdlib

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency (rule R1).
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from loguru import logger
from PIL import Image, UnidentifiedImageError

from nexusdl.core.exceptions import ImageConversionError

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


__all__ = [
    "SUPPORTED_TARGET_FORMATS",
    "ConversionOptions",
    "convert",
]


# --------------------------------------------------------------------------- #
# Supported target formats — discovered from Pillow's save registry          #
# --------------------------------------------------------------------------- #

#: Mapping of lowercase format identifiers to Pillow's uppercase format
#: names. Kept in sync with :data:`SUPPORTED_TARGET_FORMATS` by a unit test
#: that iterates both. Note: this map intentionally differs from the one in
#: :mod:`nexusdl.core.image.optimizer` — the optimizer never targets AVIF
#: (it only optimizes JPEG/PNG/WebP in place), while the converter does when
#: the codec is available.
_PILLOW_FORMAT_NAMES: Final[dict[str, str]] = {
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "avif": "AVIF",
}

#: Lowercase identifiers of formats the converter can write. Computed from
#: :data:`PIL.Image.SAVE` so a runtime without AVIF support does not pretend
#: it can produce it. Callers can display this set to a user before asking
#: for a target format.
SUPPORTED_TARGET_FORMATS: Final[frozenset[str]] = frozenset(
    fmt
    for fmt, pillow_name in _PILLOW_FORMAT_NAMES.items()
    if pillow_name in Image.SAVE
)

#: Temp file prefix used by :func:`_atomic_write`. Matches the optimizer's
#: convention so an operator scanning the temp directory sees a single,
#: recognisable pattern.
_TEMP_PREFIX_FMT: Final[str] = ".{name}.convert-"

#: Reasonable working size when Pillow needs a solid background for alpha
#: flattening. Small enough not to matter, large enough to hold any
#: practical image's alpha mask.
_BACKGROUND_MODE: Final[str] = "RGB"

_log = logger.bind(component="image.converter")


# --------------------------------------------------------------------------- #
# Options                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConversionOptions:
    """All parameters controlling a conversion.

    Every field is validated at construction time. If you cannot build a
    :class:`ConversionOptions` instance, :func:`convert` cannot be called
    with it — no partial-validation path exists at conversion time.

    Attributes:
        target_format: Lowercase target format identifier. Must be a member
            of :data:`SUPPORTED_TARGET_FORMATS`; the error message on an
            unsupported value lists the currently-supported set.
        quality: Encoder quality, in ``[1, 100]``. Applies to JPEG, WebP
            (lossy), and AVIF. Ignored for PNG and WebP lossless.
        max_dimension: When set, the larger side of the image is scaled
            down to this value (preserving aspect ratio). Images already
            at or below the limit are **not** upscaled. ``None`` disables
            resizing entirely.
        strip_exif: When ``True`` (default), the EXIF block is not carried
            into the output. When ``False``, the source EXIF is passed
            through to the encoder, which may include it or not depending
            on target format.
        background_color: RGB triplet used as the background when a source
            with transparency is flattened for a format that cannot
            represent alpha (JPEG only). Default is white; the tuple is
            clamped to ``[0, 255]`` per channel.
        png_compress_level: zlib compression level for PNG, in ``[0, 9]``.
            Defaults to 9 (maximum). Ignored for other targets.
        webp_lossless: When ``True``, use WebP's lossless mode (ignores
            :attr:`quality`). Defaults to ``False``.
        webp_method: WebP encoder effort, in ``[0, 6]``. Higher is slower
            but produces smaller files. Defaults to 4.
        preserve_icc_profile: When ``True`` (default), the source ICC
            profile is passed through to the encoder so color accuracy is
            maintained. When ``False``, the profile is dropped.
    """

    target_format: str

    quality: int = 85
    max_dimension: int | None = None
    strip_exif: bool = True
    background_color: tuple[int, int, int] = (255, 255, 255)

    png_compress_level: int = 9

    webp_lossless: bool = False
    webp_method: int = 4

    preserve_icc_profile: bool = True

    def __post_init__(self) -> None:
        """Validate field ranges and cross-field consistency.

        Raises:
            ValueError: If ``target_format`` is not in
                :data:`SUPPORTED_TARGET_FORMATS`, if any numeric bound is
                out of range, if ``background_color`` has the wrong arity
                or an out-of-range channel, or if ``max_dimension`` is
                zero or negative.
        """
        if self.target_format not in SUPPORTED_TARGET_FORMATS:
            raise ValueError(
                f"unsupported target_format {self.target_format!r}; "
                f"supported formats are "
                f"{sorted(SUPPORTED_TARGET_FORMATS)}"
            )
        if not (1 <= self.quality <= 100):
            raise ValueError(
                f"quality must be in [1, 100], got {self.quality}"
            )
        if self.max_dimension is not None and self.max_dimension < 1:
            raise ValueError(
                f"max_dimension must be >= 1 or None, "
                f"got {self.max_dimension}"
            )
        if len(self.background_color) != 3:
            raise ValueError(
                f"background_color must have exactly 3 channels, "
                f"got {len(self.background_color)}"
            )
        for channel in self.background_color:
            if not (0 <= channel <= 255):
                raise ValueError(
                    f"background_color channels must be in [0, 255], "
                    f"got {self.background_color!r}"
                )
        if not (0 <= self.png_compress_level <= 9):
            raise ValueError(
                f"png_compress_level must be in [0, 9], "
                f"got {self.png_compress_level}"
            )
        if not (0 <= self.webp_method <= 6):
            raise ValueError(
                f"webp_method must be in [0, 6], got {self.webp_method}"
            )


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


async def convert(
    src: Path,
    dest: Path,
    options: ConversionOptions,
) -> Path:
    """Convert *src* to *dest* according to *options*.

    The conversion runs in a worker thread (via ``asyncio.to_thread``) so
    the event loop stays responsive. On success, *dest* contains a
    freshly-encoded image in ``options.target_format``; on any failure,
    *dest* is left untouched (atomic write via temp-file-plus-rename).

    ``src`` and ``dest`` **may be the same path** — the atomic write
    handles it transparently, and the operation degenerates to an
    in-place conversion (resize, re-encode, EXIF strip).

    Args:
        src: Path to the source image. Must exist and be readable.
        dest: Path to write the converted image to. Intermediate
            directories are created as needed. If *dest* already exists,
            it is replaced atomically on success.
        options: Conversion parameters. Must be constructed **before**
            this call — an invalid :class:`ConversionOptions` raises
            :class:`ValueError` at construction time, not here.

    Returns:
        *dest*, unchanged. Returned for chaining convenience.

    Raises:
        ImageConversionError: If the source cannot be decoded, is not a
            recognised image, exceeds Pillow's decompression-bomb
            threshold, or the target encoder rejects the image (e.g.
            AVIF codec unexpectedly unavailable at runtime, or a mode
            that cannot be converted). The underlying cause is always
            chained as ``__cause__``.
    """
    return await asyncio.to_thread(_convert_sync, src, dest, options)


# --------------------------------------------------------------------------- #
# Synchronous implementation                                                  #
# --------------------------------------------------------------------------- #


def _convert_sync(
    src: Path,
    dest: Path,
    options: ConversionOptions,
) -> Path:
    """Synchronous implementation of :func:`convert`.

    Extracted so tests can call it without an event loop, and so future
    batch converters can invoke it in their own worker pool without a
    nested ``to_thread`` hop.
    """
    payload = _encode_from_file(src, options)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(dest, payload)
    except OSError as exc:
        raise ImageConversionError(
            f"cannot write converted image to {dest}: {exc}"
        ) from exc

    return dest


def _encode_from_file(src: Path, options: ConversionOptions) -> bytes:
    """Read, transform, and encode *src* into bytes.

    All transformation stages run inside a single ``Image.open`` context so
    Pillow's file handle is released deterministically.
    """
    try:
        with Image.open(src) as img:
            # Force-decode so corruption surfaces here, not in `save()`.
            img.load()

            frames = getattr(img, "n_frames", 1)
            if isinstance(frames, int) and frames > 1:
                _log.warning(
                    "Source {} has {} frames; only the first is kept "
                    "when converting to {}",
                    src,
                    frames,
                    options.target_format,
                )

            working, owns_working = _prepare_working_image(img, options)
            try:
                return _encode(working, options)
            finally:
                if owns_working:
                    working.close()
    except FileNotFoundError as exc:
        raise ImageConversionError(f"source not found: {src}") from exc
    except UnidentifiedImageError as exc:
        raise ImageConversionError(
            f"source is not a recognised image: {src}"
        ) from exc
    except Image.DecompressionBombError as exc:
        raise ImageConversionError(
            f"source exceeds Pillow's decompression-bomb threshold: {src}: "
            f"{exc}"
        ) from exc
    except ImageConversionError:
        raise
    except OSError as exc:
        raise ImageConversionError(
            f"cannot read source image {src}: {exc}"
        ) from exc


def _prepare_working_image(
    img: PILImage,
    options: ConversionOptions,
) -> tuple[PILImage, bool]:
    """Apply resize and mode adaptation. Return ``(image, owns_image)``.

    ``owns_image`` is ``True`` when the returned image is a **new** object
    that the caller must close; ``False`` when it is the source image (or a
    reference to one owned by the source context manager). This lets the
    caller use a simple ``finally`` block without tracking intermediate
    allocations.

    The pipeline closes any intermediate image it discards, so no leak
    occurs when both a resize and a mode change happen.
    """
    working: PILImage = img
    owns_working = False

    if options.max_dimension is not None:
        resized = _resize_if_needed(working, options.max_dimension)
        if resized is not working:
            working = resized
            owns_working = True

    prepared = _prepare_mode(working, options)
    if prepared is not working:
        if owns_working:
            working.close()
        working = prepared
        owns_working = True

    return working, owns_working


def _resize_if_needed(img: PILImage, max_dimension: int) -> PILImage:
    """Return *img* resized so its larger side is at most *max_dimension*.

    **Never upscales.** If both sides are already ``<= max_dimension``,
    returns *img* unchanged. Otherwise, scales down preserving aspect
    ratio, using Lanczos resampling.
    """
    width, height = img.size
    if max(width, height) <= max_dimension:
        return img
    if width >= height:
        new_w = max_dimension
        new_h = max(1, round(height * max_dimension / width))
    else:
        new_h = max_dimension
        new_w = max(1, round(width * max_dimension / height))
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _prepare_mode(
    img: PILImage,
    options: ConversionOptions,
) -> PILImage:
    """Return *img* converted to a mode the target codec accepts.

    Returns *img* unchanged when its current mode is already valid for the
    target. The caller is responsible for closing the returned image if it
    differs from *img*.
    """
    target = options.target_format

    if target == "jpeg":
        # JPEG cannot carry alpha. Flatten on the configured background.
        if _has_alpha(img):
            rgba = img.convert("RGBA")
            try:
                background = Image.new(
                    _BACKGROUND_MODE, rgba.size, options.background_color
                )
                try:
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    return background
                except Exception:
                    background.close()
                    raise
            finally:
                rgba.close()
        if img.mode not in ("RGB", "L"):
            # Convert CMYK, P, I, F, 1, LA, etc. to RGB. L is preserved
            # because grayscale JPEGs are legitimately smaller.
            return img.convert("RGB")
        return img

    if target in ("webp", "avif"):
        # Both support RGB and RGBA only; normalize.
        if _has_alpha(img):
            if img.mode == "RGBA":
                return img
            return img.convert("RGBA")
        if img.mode == "RGB":
            return img
        return img.convert("RGB")

    if target == "png":
        # PNG handles every mode Pillow can produce; preserve.
        return img

    # Unreachable — __post_init__ already rejected unsupported targets.
    raise ImageConversionError(
        f"no mode adaptation defined for target {target!r}"
    )


def _encode(img: PILImage, options: ConversionOptions) -> bytes:
    """Encode *img* into bytes with the target format's save kwargs."""
    pillow_format = _PILLOW_FORMAT_NAMES[options.target_format]
    kwargs = _build_save_kwargs(img, options)
    buf = io.BytesIO()
    try:
        img.save(buf, format=pillow_format, **kwargs)
    except (KeyError, OSError, ValueError) as exc:
        raise ImageConversionError(
            f"failed to encode image as {options.target_format!r}: {exc}"
        ) from exc
    return buf.getvalue()


def _build_save_kwargs(
    img: PILImage,
    options: ConversionOptions,
) -> dict[str, Any]:
    """Return the Pillow ``save()`` kwargs for the target format."""
    target = options.target_format
    kwargs: dict[str, Any] = {}

    if target == "jpeg":
        kwargs["quality"] = options.quality
        kwargs["optimize"] = True
        kwargs["progressive"] = True
    elif target == "png":
        kwargs["optimize"] = True
        kwargs["compress_level"] = options.png_compress_level
    elif target == "webp":
        kwargs["quality"] = options.quality
        kwargs["method"] = options.webp_method
        kwargs["lossless"] = options.webp_lossless
    elif target == "avif":
        kwargs["quality"] = options.quality
    # Unreachable: __post_init__ rejected any other value.

    if options.preserve_icc_profile and "icc_profile" in img.info:
        kwargs["icc_profile"] = img.info["icc_profile"]

    if not options.strip_exif and "exif" in img.info:
        kwargs["exif"] = img.info["exif"]

    return kwargs


def _has_alpha(img: PILImage) -> bool:
    """Return ``True`` iff *img* carries an alpha channel or palette transparency."""
    if img.mode in ("RGBA", "LA", "PA"):
        return True
    return img.mode == "P" and "transparency" in img.info


# --------------------------------------------------------------------------- #
# Internal — atomic file write                                                #
# --------------------------------------------------------------------------- #
# NOTE: This helper is duplicated from
# ``nexusdl.core.image.optimizer._atomic_write`` pending extraction into
# ``nexusdl.core.utils.filesystem.atomic_write``. Once that module exists,
# both call sites import the shared implementation and this copy is deleted.
# The duplication is intentional and reviewed: the pattern is stable, and
# extracting now would mean reopening a delivered file.
# --------------------------------------------------------------------------- #


def _atomic_write(target: Path, payload: bytes) -> None:
    """Write *payload* to *target* atomically via a sibling temp file.

    Uses :func:`tempfile.mkstemp` so concurrent writers to the same target
    never collide on the temp path. On any failure the temp file is removed
    and *target* is untouched.

    Raises:
        OSError: If any step (temp creation, write, replace) fails.
    """
    fd, raw_temp = tempfile.mkstemp(
        dir=target.parent,
        prefix=_TEMP_PREFIX_FMT.format(name=target.name),
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
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
