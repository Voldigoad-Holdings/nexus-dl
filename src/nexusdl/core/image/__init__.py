"""Public API of the NexusDL image pipeline.

The :mod:`nexusdl.core.image` package is the complete image-processing
toolchain: validation, in-format optimization, format conversion, and
URL-based downloading. Four modules cooperate, each with a single,
non-overlapping responsibility:

* :mod:`~nexusdl.core.image.validator` — decides whether bytes or a file
  constitute a valid image (:class:`ValidationResult`,
  :func:`validate`, :func:`validate_bytes`, :func:`validate_file`,
  :class:`ValidationLimits`).
* :mod:`~nexusdl.core.image.optimizer` — re-encodes an already-validated
  image **in the same format** to fewer bytes, optionally baking EXIF
  orientation and stripping metadata (:class:`OptimizationOptions`,
  :class:`OptimizationResult`, :func:`optimize_bytes`,
  :func:`optimize_file`).
* :mod:`~nexusdl.core.image.converter` — re-encodes an image into a
  **different** format, with optional resize and mode adaptation
  (:class:`ConversionOptions`, :func:`convert`,
  :data:`SUPPORTED_TARGET_FORMATS`).
* :mod:`~nexusdl.core.image.downloader` — fetches a URL, validates the
  bytes, optionally optimizes, and atomically writes the result
  (:class:`ImageDownloader`, :class:`ImageDownloadResult`).

Pipeline order
--------------
The stages compose in a fixed order, and each stage assumes its inputs
have passed the previous stage:

    download ──► validate ──► [optimize] ──► [convert] ──► write

* :class:`ImageDownloader` folds the first three stages into one call.
* Callers who already have bytes on disk (e.g. a CLI ``optimize``
  subcommand, a test fixture) use the individual stages directly.
* The optimizer and converter are mutually exclusive for the same input
  on the same pass — the optimizer stays in the source format, the
  converter changes it. Pick one per pass.

Composition
-----------
Every public entry point is async except ``validate``, ``validate_bytes``,
and ``validate_file``, which are synchronous because they are CPU-bound
and never touch the network:

    from nexusdl.core.image import ImageDownloader, OptimizationOptions

    downloader = ImageDownloader(
        session=http_session,
        optimization=OptimizationOptions(jpeg_quality=85),
    )
    result = await downloader.download(url, dest)
    print(result.validation.format, result.bytes_written)

For the lower-level stages:

    from nexusdl.core.image import (
        ConversionOptions,
        convert,
        optimize_file,
        validate_file,
    )

    validation = validate_file(path)
    if not validation.is_valid:
        raise NexusDLError(validation.error or "invalid image")

    await optimize_file(path)                    # in place
    await convert(path, target, ConversionOptions(target_format="webp"))

The structural protocol ``HttpSessionProtocol`` (defined in
``downloader.py``) is deliberately **not** re-exported. It exists only as
an injection point for :class:`ImageDownloader`; in practice, callers pass
a concrete :class:`~nexusdl.core.session.http_session.HttpSession` and never
need to name the protocol.

Stability
---------
``__all__`` is the public contract. Adding a symbol is a minor change;
removing or renaming one is a breaking change.

The following are **not** re-exported, on purpose — they are private
implementation details, stable only within their own module:

* ``_ALLOWED_FORMATS``, ``_OPTIMIZABLE_FORMATS``,
  ``_PILLOW_FORMAT_NAMES``, ``_STRIPPABLE_INFO_KEYS`` — internal format
  tables, kept in sync by unit tests.
* ``_atomic_write``, ``_convert_sync``, ``_optimize_bytes_sync``,
  ``_optimize_file_sync``, ``_looks_like_html``, ``_invalid``,
  ``_validate_url_scheme``, ``_make_temp_path`` — private helpers.
* ``HttpSessionProtocol`` — injection point for :class:`ImageDownloader`;
  passes any object satisfying it without needing the type.

Dependency graph (no cycles within the package)::

    validator   ──► core.exceptions
    optimizer   ──► (PIL, loguru only)
    converter   ──► core.exceptions
    downloader  ──► core.downloader.retry, core.exceptions,
                    core.image.optimizer, core.image.validator

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency — rule R1 of
the PROMPT MASTER.
"""

from __future__ import annotations

# --- Validator (decode-only, synchronous) ---------------------------------- #
from nexusdl.core.image.validator import (
    DEFAULT_LIMITS,
    ValidationLimits,
    ValidationResult,
    validate,
    validate_bytes,
    validate_file,
)

# --- Optimizer (same-format re-encode, asynchronous) ----------------------- #
from nexusdl.core.image.optimizer import (
    DEFAULT_OPTIONS,
    OptimizationOptions,
    OptimizationResult,
    optimize_bytes,
    optimize_file,
)

# --- Converter (format change, asynchronous) ------------------------------- #
from nexusdl.core.image.converter import (
    SUPPORTED_TARGET_FORMATS,
    ConversionOptions,
    convert,
)

# --- Downloader (stream + validate + optional optimize + atomic write) ----- #
from nexusdl.core.image.downloader import (
    ImageDownloadResult,
    ImageDownloader,
)

__all__ = [
    # --- Validator ------------------------------------------------------- #
    "DEFAULT_LIMITS",
    "ValidationLimits",
    "ValidationResult",
    "validate",
    "validate_bytes",
    "validate_file",
    # --- Optimizer ------------------------------------------------------- #
    "DEFAULT_OPTIONS",
    "OptimizationOptions",
    "OptimizationResult",
    "optimize_bytes",
    "optimize_file",
    # --- Converter ------------------------------------------------------- #
    "SUPPORTED_TARGET_FORMATS",
    "ConversionOptions",
    "convert",
    # --- Downloader ------------------------------------------------------ #
    "ImageDownloadResult",
    "ImageDownloader",
]
