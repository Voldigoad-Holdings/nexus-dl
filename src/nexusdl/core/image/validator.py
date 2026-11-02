"""Validate downloaded image files before they enter the packaging pipeline.

Every page the downloader writes to disk passes through this module before
the worker accepts it. The validator exists because the downloader cannot
trust what an HTTP response actually contains: sites routinely serve
Cloudflare challenge pages, WAF blocks, and HTML error pages behind URLs
that *should* return an image, and a naive pipeline would happily write
those to a ``.cbz``.

What is rejected
----------------
Every rejection path is enumerated — a new rejection rule is a deliberate
edit to this file, not a silent policy change:

1. **Empty payloads** — zero-byte files.
2. **HTML / XML responses** — detected by a cheap prefix sniff *before*
   Pillow decodes anything, so the error message can mention the likely
   cause (Cloudflare, WAF block, error page).
3. **Undersized / oversized files** — below
   :attr:`ValidationLimits.min_file_size_bytes` (default 100 B) or above
   :attr:`ValidationLimits.max_file_size_bytes` (default 50 MiB).
4. **Undersized / oversized dimensions** — below
   :attr:`ValidationLimits.min_width` × :attr:`ValidationLimits.min_height`
   (default 50×50) or above :attr:`ValidationLimits.max_width` ×
   :attr:`ValidationLimits.max_height` (default 10 000×30 000 — the height
   is generous because webtoons produce very tall single-page strips).
5. **Disallowed formats** — anything outside
   :attr:`ValidationLimits.allowed_formats` (``jpeg``, ``png``, ``webp``,
   ``avif``, ``bmp``, ``tiff``). SVG is rejected because Pillow cannot
   decode it and its presence in a page slot is almost always a site
   error. GIF is rejected because manga pages are static; an animated GIF
   in a page slot is usually a tracking pixel or an ad banner.
6. **Truncated / corrupted files** — detected by Pillow's
   :meth:`PIL.Image.Image.verify`, which checks structural integrity
   without decoding all pixels. This catches the common cases (JPEG
   missing EOI, PNG with bad CRCs); it is not exhaustive — a file whose
   first half is intact but second half is missing may still pass. The
   packager's own structural checks are the last line of defence.
7. **Decompression bombs** — Pillow's own guard against images whose
   declared dimensions would exhaust memory if fully decoded. Reported as
   a distinct error message so operators can distinguish "huge image" from
   "corrupted image".

Public API
----------
Three entry points, one shared result type:

* :func:`validate_bytes` — validate in-memory bytes, return a
  :class:`ValidationResult`.
* :func:`validate_file` — validate a file on disk, return a
  :class:`ValidationResult`. This is what
  :meth:`nexusdl.core.downloader.worker.DownloadWorker._download_one_page`
  calls.
* :func:`validate` — strict wrapper around :func:`validate_bytes` that
  raises :class:`~nexusdl.core.exceptions.ImageValidationError` on failure.
  Provided for call sites that prefer exceptions over return-value checks.

Design invariants
-----------------
* **No hidden I/O** — :func:`validate_bytes` never touches the filesystem;
  :func:`validate_file` does exactly one ``stat`` and one ``read_bytes``.
* **No cross-layer imports** — only :mod:`nexusdl.core.exceptions`,
  :mod:`loguru`, and Pillow. No ``parsers/``, ``plugins/``, or
  ``interfaces/`` dependency (PROMPT MASTER rule R1).
* **Configurable limits** — every bound is in :class:`ValidationLimits`,
  which is injectable per call. The module-level :data:`DEFAULT_LIMITS` is
  the source of truth; no other module should hardcode a limit.
* **Fast rejection** — HTML and size checks run before Pillow is invoked,
  so the common failure modes cost a handful of byte comparisons.
* **Deterministic** — no time, no randomness, no global state. Two calls
  with the same input return equal :class:`ValidationResult` objects.

Integration contract
--------------------
The worker relies on exactly two things:

* ``validate_file(path)`` returns a :class:`ValidationResult`.
* ``result.is_valid: bool`` and ``result.error: str | None`` are always
  populated consistently — ``is_valid=True`` implies ``error is None``,
  and ``is_valid=False`` implies ``error`` is a non-empty human-readable
  message.

See ``nexusdl/core/downloader/worker.py::_download_one_page`` for the call
site.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from loguru import logger
from PIL import Image, UnidentifiedImageError

from nexusdl.core.exceptions import ImageValidationError


__all__ = [
    "DEFAULT_LIMITS",
    "ValidationLimits",
    "ValidationResult",
    "validate",
    "validate_bytes",
    "validate_file",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: First byte of any HTML or XML document after stripping a UTF-8 BOM and
#: leading whitespace. No supported image format starts with ``<``, so this
#: is a safe prefix test.
_HTML_PREFIX: Final[bytes] = b"<"

#: Number of leading bytes inspected for HTML detection. 512 bytes is enough
#: to skip a UTF-8 BOM, whitespace, and any XML declaration while keeping the
#: check effectively free.
_HTML_SNIFF_BYTES: Final[int] = 512

#: UTF-8 byte order mark, stripped before the HTML prefix check.
_UTF8_BOM: Final[bytes] = b"\xef\xbb\xbf"

#: Formats that pass validation. Lowercase ``PIL.Image.Image.format`` values.
#: See the module docstring for why GIF and SVG are excluded.
_ALLOWED_FORMATS: Final[frozenset[str]] = frozenset(
    {"jpeg", "png", "webp", "avif", "bmp", "tiff"}
)

_log = logger.bind(component="image.validator")


# --------------------------------------------------------------------------- #
# Limits                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    """Bounds applied to every image validation.

    All bounds are inclusive of the boundary value except where noted. Every
    field is validated at construction time — an invalid limits object can
    never reach :func:`validate_bytes` or :func:`validate_file`.

    Attributes:
        min_file_size_bytes: Minimum file size, in bytes. Files smaller than
            this are rejected before any decoding. Defaults to 100 bytes,
            which is smaller than any plausible image header while large
            enough to catch "empty response" and "single newline" failures.
        max_file_size_bytes: Maximum file size, in bytes. Files larger than
            this are rejected before being read into memory. Defaults to
            50 MiB, which comfortably exceeds any realistic manga page while
            bounding memory usage.
        min_width: Minimum image width, in pixels. Defaults to 50, which
            catches tracking pixels and 1×1 placeholders without rejecting
            unusual small content.
        min_height: Minimum image height, in pixels. Defaults to 50.
        max_width: Maximum image width, in pixels. Defaults to 10 000.
        max_height: Maximum image height, in pixels. Defaults to 30 000,
            generous enough for webtoon strips while staying below Pillow's
            decompression-bomb threshold.
        allowed_formats: Lowercase Pillow format identifiers that pass
            validation. Defaults to :data:`_ALLOWED_FORMATS`.
    """

    min_file_size_bytes: int = 100
    max_file_size_bytes: int = 50 * 1024 * 1024  # 50 MiB
    min_width: int = 50
    min_height: int = 50
    max_width: int = 10_000
    max_height: int = 30_000
    allowed_formats: frozenset[str] = _ALLOWED_FORMATS

    def __post_init__(self) -> None:
        """Validate internal consistency of the limits.

        Raises:
            ValueError: If any pair of lower/upper bounds is inverted, if any
                bound is negative, or if ``allowed_formats`` is empty. Raised
                at construction time so the invalid limits cannot reach the
                validation code.
        """
        if self.min_file_size_bytes < 0:
            raise ValueError(
                f"min_file_size_bytes must be >= 0, got "
                f"{self.min_file_size_bytes}"
            )
        if self.max_file_size_bytes < self.min_file_size_bytes:
            raise ValueError(
                f"max_file_size_bytes ({self.max_file_size_bytes}) must be "
                f">= min_file_size_bytes ({self.min_file_size_bytes})"
            )
        if self.min_width < 1:
            raise ValueError(
                f"min_width must be >= 1, got {self.min_width}"
            )
        if self.max_width < self.min_width:
            raise ValueError(
                f"max_width ({self.max_width}) must be >= min_width "
                f"({self.min_width})"
            )
        if self.min_height < 1:
            raise ValueError(
                f"min_height must be >= 1, got {self.min_height}"
            )
        if self.max_height < self.min_height:
            raise ValueError(
                f"max_height ({self.max_height}) must be >= min_height "
                f"({self.min_height})"
            )
        if not self.allowed_formats:
            raise ValueError("allowed_formats must not be empty")


#: Module-wide defaults. Callers that do not pass ``limits`` explicitly use
#: this object. Site-specific overrides should be passed per call rather than
#: mutating module state.
DEFAULT_LIMITS: Final[ValidationLimits] = ValidationLimits()


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of an image validation.

    Immutable and safe to cache, compare, or return across threads. On
    success, ``is_valid`` is ``True``, ``error`` is ``None``, and
    ``format`` / ``width`` / ``height`` describe the image. On failure,
    ``is_valid`` is ``False``, ``error`` is a non-empty human-readable
    message, and the other fields reflect whatever was determined before the
    rejection — ``format`` is ``None`` when Pillow could not identify the
    format at all, and ``width`` / ``height`` are ``0`` in that case.

    Attributes:
        is_valid: ``True`` iff every check passed.
        format: Lowercase Pillow format identifier (``"jpeg"``, ``"png"``,
            …), or ``None`` when the format could not be determined.
        width: Image width in pixels, or ``0`` if unknown.
        height: Image height in pixels, or ``0`` if unknown.
        error: Non-empty human-readable rejection reason; ``None`` on
            success.
        path: Filesystem path the result refers to, when produced by
            :func:`validate_file`; ``None`` for :func:`validate_bytes`.
    """

    is_valid: bool
    format: str | None = None
    width: int = 0
    height: int = 0
    error: str | None = None
    path: Path | None = None


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def validate_bytes(
    data: bytes,
    *,
    limits: ValidationLimits | None = None,
) -> ValidationResult:
    """Validate *data* as an image and return the outcome.

    Never raises for an invalid image — the rejection reason is reported in
    the returned :class:`ValidationResult`. Programming errors (invalid
    *limits*) surface as ``ValueError`` from
    :class:`ValidationLimits`'s constructor.

    Args:
        data: Raw bytes to validate.
        limits: Bounds to apply. When ``None``, :data:`DEFAULT_LIMITS` is
            used.

    Returns:
        A :class:`ValidationResult`. ``is_valid`` is ``True`` on success;
        on failure, ``error`` carries a human-readable reason.
    """
    lim = limits if limits is not None else DEFAULT_LIMITS
    size = len(data)

    # --- 1. Empty payload --------------------------------------------- #
    if size == 0:
        _log.debug("Rejected image: empty payload")
        return _invalid("file is empty")

    # --- 2. HTML / XML prefix ----------------------------------------- #
    # Performed before Pillow so the error message can name the likely
    # cause; Pillow would otherwise report a generic "cannot identify
    # image file" for what is almost always a Cloudflare/WAF page.
    if _looks_like_html(data):
        _log.debug("Rejected image: HTML/XML payload detected")
        return _invalid(
            "response is HTML/XML, not an image "
            "(likely a Cloudflare challenge, WAF block, or error page)"
        )

    # --- 3. File-size bounds ------------------------------------------ #
    if size < lim.min_file_size_bytes:
        _log.debug(
            "Rejected image: {} bytes < minimum {} bytes",
            size,
            lim.min_file_size_bytes,
        )
        return _invalid(
            f"file is too small ({size} bytes < "
            f"{lim.min_file_size_bytes} bytes)"
        )
    if size > lim.max_file_size_bytes:
        _log.debug(
            "Rejected image: {} bytes > maximum {} bytes",
            size,
            lim.max_file_size_bytes,
        )
        return _invalid(
            f"file is too large ({size} bytes > "
            f"{lim.max_file_size_bytes} bytes)"
        )

    # --- 4. Decode with Pillow ---------------------------------------- #
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").lower()
            width, height = img.size

            if not fmt:
                _log.debug("Rejected image: Pillow reported no format")
                return _invalid(
                    "Pillow reported no format after successful open",
                    width=width,
                    height=height,
                )

            if fmt not in lim.allowed_formats:
                _log.debug("Rejected image: disallowed format {!r}", fmt)
                return _invalid(
                    f"disallowed image format: {fmt!r} "
                    f"(allowed: {sorted(lim.allowed_formats)})",
                    format_name=fmt,
                    width=width,
                    height=height,
                )

            if width < lim.min_width or height < lim.min_height:
                _log.debug(
                    "Rejected image: {}x{} below minimum {}x{}",
                    width,
                    height,
                    lim.min_width,
                    lim.min_height,
                )
                return _invalid(
                    f"image dimensions too small: {width}x{height} "
                    f"(minimum {lim.min_width}x{lim.min_height})",
                    format_name=fmt,
                    width=width,
                    height=height,
                )
            if width > lim.max_width or height > lim.max_height:
                _log.debug(
                    "Rejected image: {}x{} above maximum {}x{}",
                    width,
                    height,
                    lim.max_width,
                    lim.max_height,
                )
                return _invalid(
                    f"image dimensions too large: {width}x{height} "
                    f"(maximum {lim.max_width}x{lim.max_height})",
                    format_name=fmt,
                    width=width,
                    height=height,
                )

            # --- 5. Structural integrity ------------------------------ #
            # ``verify()`` checks the container structure (JPEG EOI marker,
            # PNG CRCs, …) without decoding all pixels. It is the standard
            # fast integrity check and catches the common truncation and
            # corruption cases.
            img.verify()

            _log.trace(
                "Valid image: format={} dims={}x{} bytes={}",
                fmt,
                width,
                height,
                size,
            )
            return ValidationResult(
                is_valid=True,
                format=fmt,
                width=width,
                height=height,
            )

    except Image.DecompressionBombError as exc:
        _log.debug("Rejected image: decompression bomb ({})", exc)
        return _invalid(f"image is too large to process safely: {exc}")
    except UnidentifiedImageError:
        _log.debug("Rejected image: Pillow could not identify the format")
        return _invalid(
            "unrecognised image format "
            "(Pillow could not decode the header)"
        )
    except (OSError, SyntaxError) as exc:
        _log.debug("Rejected image: decode/verify failed ({})", exc)
        return _invalid(f"image data is truncated or corrupted: {exc}")


def validate_file(
    path: Path,
    *,
    limits: ValidationLimits | None = None,
) -> ValidationResult:
    """Validate the image file at *path* and return the outcome.

    Performs a single ``stat`` (for size bounds), a single ``read_bytes``
    (bounded by :attr:`ValidationLimits.max_file_size_bytes`), then delegates
    to :func:`validate_bytes`. The returned :class:`ValidationResult` always
    carries ``path=path`` so callers can correlate the outcome with the file
    they asked about.

    Never raises for an invalid file — the rejection reason is reported in
    the returned :class:`ValidationResult`.

    Args:
        path: Filesystem path to validate.
        limits: Bounds to apply. When ``None``, :data:`DEFAULT_LIMITS` is
            used.

    Returns:
        A :class:`ValidationResult` with ``path`` populated.
    """
    lim = limits if limits is not None else DEFAULT_LIMITS

    if not path.is_file():
        _log.debug("Rejected image: path does not exist: {}", path)
        return _invalid(f"file does not exist: {path}", path=path)

    try:
        size = path.stat().st_size
    except OSError as exc:
        _log.debug("Rejected image: cannot stat {}: {}", path, exc)
        return _invalid(f"cannot stat file {path}: {exc}", path=path)

    if size == 0:
        _log.debug("Rejected image: empty file at {}", path)
        return _invalid("file is empty", path=path)
    if size < lim.min_file_size_bytes:
        _log.debug(
            "Rejected image: {} bytes < minimum {} bytes (path={})",
            size,
            lim.min_file_size_bytes,
            path,
        )
        return _invalid(
            f"file is too small ({size} bytes < "
            f"{lim.min_file_size_bytes} bytes)",
            path=path,
        )
    if size > lim.max_file_size_bytes:
        _log.debug(
            "Rejected image: {} bytes > maximum {} bytes (path={})",
            size,
            lim.max_file_size_bytes,
            path,
        )
        return _invalid(
            f"file is too large ({size} bytes > "
            f"{lim.max_file_size_bytes} bytes)",
            path=path,
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        _log.debug("Rejected image: cannot read {}: {}", path, exc)
        return _invalid(f"cannot read file {path}: {exc}", path=path)

    result = validate_bytes(data, limits=lim)
    if result.path is not None:
        # Defensive: validate_bytes never sets ``path``, but guard against
        # future changes so we never clobber an already-populated field.
        return result
    return ValidationResult(
        is_valid=result.is_valid,
        format=result.format,
        width=result.width,
        height=result.height,
        error=result.error,
        path=path,
    )


def validate(
    data: bytes,
    *,
    limits: ValidationLimits | None = None,
) -> ValidationResult:
    """Validate *data* as an image, raising on failure.

    Strict counterpart to :func:`validate_bytes`, provided for call sites
    that prefer exceptions over return-value checks. On success the same
    :class:`ValidationResult` is returned; on failure,
    :class:`~nexusdl.core.exceptions.ImageValidationError` is raised with the
    same message that would have been in ``result.error``.

    Args:
        data: Raw bytes to validate.
        limits: Bounds to apply. When ``None``, :data:`DEFAULT_LIMITS` is
            used.

    Returns:
        The :class:`ValidationResult` from :func:`validate_bytes`, guaranteed
        to have ``is_valid=True``.

    Raises:
        ImageValidationError: If the image fails validation. The exception
            message is the same as ``result.error`` would have been.
    """
    result = validate_bytes(data, limits=limits)
    if not result.is_valid:
        raise ImageValidationError(
            result.error or "image validation failed"
        )
    return result


# --------------------------------------------------------------------------- #
# Internals                                                                   #
# --------------------------------------------------------------------------- #


def _looks_like_html(data: bytes) -> bool:
    """Return ``True`` iff *data* begins with something that looks like HTML/XML.

    Strips a UTF-8 BOM and leading whitespace from the first
    :data:`_HTML_SNIFF_BYTES` bytes, then checks whether the result starts
    with ``<``. No supported image format begins with that byte, so the test
    has no false positives against legitimate images.

    Args:
        data: Raw bytes to inspect.

    Returns:
        ``True`` when the payload starts with ``<`` after BOM and whitespace
        stripping; ``False`` otherwise, including for empty or
        all-whitespace payloads (those are caught by other checks with
        clearer messages).
    """
    head = data[:_HTML_SNIFF_BYTES]
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM) :]
    stripped = head.lstrip()
    if not stripped:
        return False
    return stripped.startswith(_HTML_PREFIX)


def _invalid(
    error: str,
    *,
    format_name: str | None = None,
    width: int = 0,
    height: int = 0,
    path: Path | None = None,
) -> ValidationResult:
    """Build a failed :class:`ValidationResult`.

    Private helper used by every rejection path so that the invariant
    ``is_valid=False ⟹ error is a non-empty string`` is enforced in exactly
    one place.

    Args:
        error: Human-readable rejection reason. Must be non-empty; an empty
            string is replaced by a generic message so callers never have to
            handle ``error=""``.
        format_name: Lowercase Pillow format identifier, if known.
        width: Image width in pixels, if known.
        height: Image height in pixels, if known.
        path: Filesystem path, when produced by :func:`validate_file`.

    Returns:
        A :class:`ValidationResult` with ``is_valid=False``.
    """
    return ValidationResult(
        is_valid=False,
        format=format_name,
        width=width,
        height=height,
        error=error or "image validation failed",
        path=path,
    )
