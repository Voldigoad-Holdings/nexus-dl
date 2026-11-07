"""Fetch, validate, and atomically store a single image from a URL.

The worker (``core/downloader/worker.py``) downloads pages by calling
``parser.download_page(page, dest)``; every parser must therefore implement
the same three-step dance:

1. stream the URL to a temporary file via an ``HttpSession``;
2. validate that the bytes actually contain an image (Cloudflare, WAF, and
   HTML error pages are the rule, not the exception);
3. move the validated bytes into place without ever leaving a half-written
   file on disk.

:class:`ImageDownloader` packages those three steps into a reusable
primitive, so parsers do not re-implement them (and drift from each other).
The class is intentionally small: it does **not** manage retries (the caller
builds a :func:`~nexusdl.core.downloader.retry.build_retry` controller
around it, exactly as the worker does), and it does **not** convert formats
(that is :mod:`nexusdl.core.image.converter`'s job).

What `download()` guarantees
----------------------------
* **Atomic** — the destination is either the previous file or the complete
  new one. A crash or a validation failure leaves the destination
  untouched.
* **Clean** — the temporary file is always removed, success or failure. A
  failure to remove the temporary file is logged but never masks the
  original error.
* **Validated** — the returned :class:`ValidationResult` has
  ``is_valid=True``; any invalid payload raises
  :class:`~nexusdl.core.exceptions.ImageValidationError` before the rename.
* **Optionally optimized** — when an
  :class:`~nexusdl.core.image.optimizer.OptimizationOptions` is provided,
  the validated bytes are re-encoded in the same format by
  :func:`~nexusdl.core.image.optimizer.optimize_file` before being moved
  into place. Optimization failure is **not** fatal — the original is kept
  and a warning is logged.
* **No hidden retries** — see the "Retry policy" section below.

Retry policy
------------
:class:`ImageDownloader` performs **exactly one attempt** per call. This is
deliberate: the worker already wraps ``parser.download_page`` in a retry
controller driven by :mod:`nexusdl.core.downloader.retry`, and doubling the
retry layers would multiply the worst-case time by nine. Parsers that call
:class:`ImageDownloader` **outside** the worker (e.g. a CLI ``fetch-page``
subcommand) are expected to build their own retry controller:

    policy = policy_from_settings(settings.downloader.retry)
    async for attempt in build_retry(policy):
        with attempt:
            await downloader.download(url, dest)

The classifier :func:`~nexusdl.core.downloader.retry.is_retryable` already
treats both :class:`~nexusdl.core.exceptions.ImageValidationError` and the
session-level transient errors as retryable, so this pattern works without
any special-casing.

URL safety
----------
Only ``http`` and ``https`` schemes are accepted. A parser producing a
``file://`` or ``ftp://`` URL is a programming error and
:func:`_validate_url_scheme` raises :class:`ValueError` — the caller must
fix the parser, retrying will not help.

Dependency graph (no cycles)::

    core.downloader.retry  ─┐
    core.exceptions         ├──► core.image.downloader
    core.image.optimizer    │
    core.image.validator   ─┘

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency (rule R1).
The ``HttpSession`` is accessed through :class:`HttpSessionProtocol`, a
narrow structural contract — this module never imports the concrete class.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from loguru import logger

from nexusdl.core.downloader.retry import RetryPolicy, build_retry
from nexusdl.core.exceptions import ImageValidationError
from nexusdl.core.image.optimizer import (
    OptimizationOptions,
    OptimizationResult,
    optimize_file,
)
from nexusdl.core.image.validator import (
    DEFAULT_LIMITS,
    ValidationLimits,
    ValidationResult,
    validate_file,
)


__all__ = [
    "HttpSessionProtocol",
    "ImageDownloadResult",
    "ImageDownloader",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Only these URL schemes may be passed to :meth:`ImageDownloader.download`.
#: A parser producing any other scheme is a bug.
_ALLOWED_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Prefix used for the per-call temporary directory under
#: :func:`tempfile.gettempdir`. Included in the directory name so that a
#: system administrator browsing the temp directory can identify where a
#: stray directory came from.
_TEMP_DIR_PREFIX: Final[str] = "nexusdl-image-"


_log = logger.bind(component="image.downloader")


# --------------------------------------------------------------------------- #
# Structural protocol                                                         #
# --------------------------------------------------------------------------- #


class HttpSessionProtocol(Protocol):
    """Minimal HTTP-session surface required by :class:`ImageDownloader`.

    Satisfied by :class:`nexusdl.core.session.http_session.HttpSession`.
    Only the streaming method is needed — this module never inspects
    headers, cookies, or responses directly; validation happens on the
    bytes that land on disk.
    """

    async def stream_download(
        self,
        url: str,
        dest: Path,
        *,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> None:
        """Stream *url* into the file path *dest*.

        Args:
            url: Absolute URL with an ``http`` or ``https`` scheme.
            dest: Destination file path. The file is created or truncated.
                Intermediate directories must already exist.
            on_progress: Optional callback invoked with
                ``(bytes_downloaded, total_bytes_or_None)`` roughly once per
                network chunk.
        """
        ...


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ImageDownloadResult:
    """Outcome of a successful :meth:`ImageDownloader.download` call.

    Constructed only when the whole pipeline (stream → validate →
    optional optimize → atomic rename) succeeded. Failures raise — they do
    not produce a partial result.

    Attributes:
        url: The source URL, verbatim.
        path: The final destination the image was moved to (equal to the
            ``dest`` argument).
        bytes_written: Size of the file on disk after any optimization. When
            :attr:`optimization` is not ``None`` and
            ``optimization.was_optimized`` is ``True``, this is
            ``optimization.optimized_size``; otherwise it is the original
            downloaded size. The value is captured from a ``stat`` on the
            temporary file *before* the atomic rename, so it is available
            even if the destination is modified by an external process
            immediately after the rename.
        validation: The :class:`~nexusdl.core.image.validator.ValidationResult`
            from validating the downloaded bytes. ``is_valid`` is
            guaranteed ``True``, and ``path`` has been updated from the
            temporary file to :attr:`path`.
        optimization: The
            :class:`~nexusdl.core.image.optimizer.OptimizationResult` when
            optimization was requested, or ``None`` when it was not.
            ``optimization.error`` is not ``None`` when optimization was
            attempted but failed — the download still succeeded with the
            original bytes **unless a later step (notably the atomic rename)
            failed**, in which case the exception propagates and this
            result is never returned.
    """

    url: str
    path: Path
    bytes_written: int
    validation: ValidationResult
    optimization: OptimizationResult | None = None


# --------------------------------------------------------------------------- #
# Downloader                                                                  #
# --------------------------------------------------------------------------- #


class ImageDownloader:
    """Fetch + validate + (optionally) optimize a single image URL.

    One instance per composition root. Thread-safe in the sense that
    concurrent :meth:`download` calls on distinct ``dest`` paths do not
    interfere: each call uses a unique temporary file, and there is no
    mutable state on the instance.

    Example:
        >>> downloader = ImageDownloader(session=http_session)
        >>> result = await downloader.download(
        ...     "https://example.org/01.jpg",
        ...     Path("/tmp/pages/01.jpg"),
        ... )
        >>> result.validation.format, result.bytes_written
        ('jpeg', 342_118)

    Args:
        session: The HTTP session used for streaming. Any object satisfying
            :class:`HttpSessionProtocol` works.
        validation_limits: Bounds applied by
            :func:`~nexusdl.core.image.validator.validate_file`. Defaults to
            :data:`~nexusdl.core.image.validator.DEFAULT_LIMITS`.
        optimization: When not ``None``, the validated bytes are passed
            through
            :func:`~nexusdl.core.image.optimizer.optimize_file` before the
            atomic rename. When ``None`` (the default), no optimization is
            performed and the original downloaded bytes are kept.
    """

    def __init__(
        self,
        *,
        session: HttpSessionProtocol,
        validation_limits: ValidationLimits | None = None,
        optimization: OptimizationOptions | None = None,
    ) -> None:
        self._session = session
        self._limits = validation_limits if validation_limits is not None else DEFAULT_LIMITS
        self._optimization = optimization

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> ImageDownloadResult:
        """Download *url* to *dest*, validating (and optionally optimizing).

        The file at *dest* is only written once, atomically, after the
        downloaded bytes have been validated. On any failure (network,
        validation, filesystem) the previous contents of *dest* — or its
        absence — are preserved and the exception propagates.

        Args:
            url: Absolute ``http`` or ``https`` URL. Other schemes raise
                :class:`ValueError`.
            dest: Final destination path. Intermediate directories are
                created. If *dest* exists, it is replaced atomically on
                success.
            on_progress: Optional progress callback forwarded to
                ``session.stream_download``.

        Returns:
            An :class:`ImageDownloadResult` describing the successful
            download.

        Raises:
            ValueError: If *url* uses an unsupported scheme.
            ImageValidationError: If the downloaded bytes do not constitute
                a valid image per
                :func:`~nexusdl.core.image.validator.validate_file`.
            SessionError, ParserResponseError: Any exception raised by
                ``session.stream_download`` — retryability is decided by
                the caller's retry controller.
            OSError: On filesystem failure (disk full, permission denied).
        """
        _validate_url_scheme(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = _make_temp_path(dest)

        try:
            validation = await self._fetch_and_validate(
                url, temp, on_progress=on_progress
            )

            optimization_result: OptimizationResult | None = None
            if self._optimization is not None:
                optimization_result = await optimize_file(temp)
                if optimization_result.error is not None:
                    _log.warning(
                        "Optimization failed for {}: {} (keeping original)",
                        url,
                        optimization_result.error,
                    )

            # Capture the size *before* the rename, so a concurrent
            # modification of the destination (external process, antivirus
            # quarantine, NFS cache) cannot make the result inconsistent
            # with the bytes we actually produced.
            try:
                bytes_written = os.stat(temp).st_size
            except OSError as exc:
                # The temp file disappeared between validation and publish.
                # This should not happen under the worker's ownership of
                # its own temp_dir, but propagate rather than report a
                # phantom success.
                raise OSError(
                    f"temporary file {temp} disappeared before publish: {exc}"
                ) from exc

            # Atomic publish. From this point on, `temp` no longer exists.
            temp.replace(dest)

            # Re-point any paths that still referenced `temp` to `dest`.
            validation = replace(validation, path=dest)
            if (
                optimization_result is not None
                and optimization_result.output_path == temp
            ):
                optimization_result = replace(
                    optimization_result, output_path=dest
                )

            return ImageDownloadResult(
                url=url,
                path=dest,
                bytes_written=bytes_written,
                validation=validation,
                optimization=optimization_result,
            )
        finally:
            # No-op after a successful rename; cleans up on any failure.
            # ``unlink`` failures are logged but never mask the original
            # exception (which is either propagating out of the ``try``
            # or, on the success path, irrelevant since the operation
            # already completed).
            try:
                temp.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning(
                    "Could not remove temporary file {}: {}", temp, exc
                )

    async def download_bytes(
        self, url: str
    ) -> tuple[bytes, ValidationResult]:
        """Download *url* into memory and validate, returning the bytes.

        Intended for small images (covers, thumbnails) where the caller does
        not need the content on disk. The download still goes through a
        temporary file because
        :meth:`HttpSessionProtocol.stream_download` is file-based — the temp
        is removed before this method returns.

        Args:
            url: Absolute ``http`` or ``https`` URL. Other schemes raise
                :class:`ValueError`.

        Returns:
            A ``(data, validation)`` tuple. ``validation.path`` is ``None``
            (matching :func:`~nexusdl.core.image.validator.validate_bytes`
            convention); ``validation.format`` / ``width`` / ``height``
            carry the image metadata.

        Raises:
            ValueError: If *url* uses an unsupported scheme.
            ImageValidationError: If the downloaded bytes do not constitute
                a valid image.
            SessionError, ParserResponseError: As raised by
                ``session.stream_download``.
            OSError: On filesystem failure while writing the temporary file.
        """
        _validate_url_scheme(url)

        with tempfile.TemporaryDirectory(
            prefix=_TEMP_DIR_PREFIX
        ) as tmpdir:
            temp = Path(tmpdir) / "image.bin"
            validation = await self._fetch_and_validate(
                url, temp, on_progress=None
            )
            data = temp.read_bytes()

        return data, replace(validation, path=None)

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    async def _fetch_and_validate(
        self,
        url: str,
        temp: Path,
        *,
        on_progress: Callable[[int, int | None], None] | None,
    ) -> ValidationResult:
        """Stream *url* into *temp* and validate the result.

        Cleanup of *temp* on any failure is the caller's responsibility
        (both public methods use ``try/finally``).

        Args:
            url: Absolute ``http`` or ``https`` URL.
            temp: Temporary file path. Must be inside a directory that
                already exists.
            on_progress: Forwarded to ``stream_download``.

        Returns:
            The ``ValidationResult`` (guaranteed ``is_valid=True``).

        Raises:
            ImageValidationError: If validation fails.
            Any exception raised by ``stream_download``.
        """
        await self._session.stream_download(
            url, temp, on_progress=on_progress
        )
        validation = validate_file(temp, limits=self._limits)
        if not validation.is_valid:
            raise ImageValidationError(
                validation.error or "image validation failed"
            )
        return validation


# --------------------------------------------------------------------------- #
# Module-level helpers                                                        #
# --------------------------------------------------------------------------- #


def _validate_url_scheme(url: str) -> None:
    """Reject *url* unless its scheme is ``http`` or ``https``.

    Args:
        url: The URL to check.

    Raises:
        ValueError: If the scheme is anything else, or if the URL is
            malformed in a way :func:`urlparse` cannot extract a scheme
            from. Both cases are programming errors, not transient
            conditions.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"unsupported URL scheme {scheme!r}; only "
            f"{sorted(_ALLOWED_URL_SCHEMES)} are allowed"
        )


def _make_temp_path(dest: Path) -> Path:
    """Return a unique temp path in the same directory as *dest*.

    Same directory so that :meth:`Path.replace` is a rename, not a
    cross-filesystem copy. Unique name so concurrent downloads to different
    destinations cannot collide.

    Args:
        dest: Final destination path.

    Returns:
        A path that does not exist yet.
    """
    return dest.parent / f".{dest.name}.{uuid4().hex}.tmp"
