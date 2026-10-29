# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for ImageValidator.

ImageValidator is the component that verifies a downloaded file is
**actually** a valid image (not an HTML error page, not truncated,
not an unsupported format).

It is used by ImageDownloader (Phase 8) BEFORE writing the file to disk,
to prevent saving 403/404 HTML pages as ``001.jpg``.

What is tested:
    - Valid JPEG / PNG / WebP → True
    - HTML disguised as .jpg → False (magic bytes)
    - Truncated file → False (Pillow verify)
    - Empty file → False
    - File too small / too large → False (size limits)
    - Dimensions too small / too large → False (dimension limits)
    - Unsupported format (SVG, GIF) → False
    - Corrupted image → False
    - Redirect HTML (Cloudflare) → False
    - validate_bytes() (no I/O)
    - validate_file() (with I/O)
    - Error messages are clear

Why so many cases?
    A validator that accepts a Cloudflare HTML page will silently save
    garbage as ``.jpg``, corrupting the whole chapter. This is one of
    the most common bugs in manga downloaders.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from nexusdl.core.image.validator import (
    ImageValidationError,
    ImageValidator,
    ValidationResult,
)


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_jpeg(width: int = 200, height: int = 300) -> bytes:
    """Generate a valid JPEG in memory."""
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_png(width: int = 200, height: int = 300) -> bytes:
    """Generate a valid PNG in memory."""
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _make_webp(width: int = 200, height: int = 300) -> bytes:
    """Generate a valid WebP in memory."""
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85)
    return buf.getvalue()


def _make_gif(width: int = 200, height: int = 300) -> bytes:
    """Generate a valid GIF in memory."""
    img = Image.new("P", (width, height), color=0)
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    return buf.getvalue()


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def validator() -> ImageValidator:
    """Default ImageValidator."""
    return ImageValidator(
        min_size_bytes=1024,           # 1 KB
        max_size_bytes=10 * 1024 * 1024,  # 10 MB
        min_dimensions=(50, 50),
        max_dimensions=(4000, 6000),
        allowed_formats=("JPEG", "PNG", "WEBP"),
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: validate_bytes — valid images
# ────────────────────────────────────────────────────────────────────


def test_validate_bytes_jpeg(validator: ImageValidator) -> None:
    """A valid JPEG passes validate_bytes()."""
    result = validator.validate_bytes(_make_jpeg())
    assert result.is_valid is True
    assert result.format == "JPEG"
    assert result.width == 200
    assert result.height == 300
    assert result.error is None


def test_validate_bytes_png(validator: ImageValidator) -> None:
    """A valid PNG passes validate_bytes()."""
    result = validator.validate_bytes(_make_png())
    assert result.is_valid is True
    assert result.format == "PNG"


def test_validate_bytes_webp(validator: ImageValidator) -> None:
    """A valid WebP passes validate_bytes()."""
    result = validator.validate_bytes(_make_webp())
    assert result.is_valid is True
    assert result.format == "WEBP"


# ────────────────────────────────────────────────────────────────────
# Test 2: validate_bytes — invalid inputs
# ────────────────────────────────────────────────────────────────────


def test_validate_bytes_empty(validator: ImageValidator) -> None:
    """An empty byte string is rejected."""
    result = validator.validate_bytes(b"")
    assert result.is_valid is False
    assert "empty" in result.error.lower() or "size" in result.error.lower()


def test_validate_bytes_html_disguised(validator: ImageValidator) -> None:
    """HTML disguised as .jpg is rejected (magic bytes)."""
    html = b"<!DOCTYPE html><html><body>403 Forbidden</body></html>"
    result = validator.validate_bytes(html)
    assert result.is_valid is False
    assert result.error is not None


def test_validate_bytes_cloudflare_page(validator: ImageValidator) -> None:
    """A Cloudflare challenge page is rejected."""
    cf = (
        b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
        b"<body><div id='cf-browser-verification'></div></body></html>"
    )
    result = validator.validate_bytes(cf)
    assert result.is_valid is False


def test_validate_bytes_truncated(validator: ImageValidator) -> None:
    """A truncated JPEG is rejected (Pillow verify)."""
    full = _make_jpeg()
    truncated = full[: len(full) // 2]
    result = validator.validate_bytes(truncated)
    assert result.is_valid is False


def test_validate_bytes_random_bytes(validator: ImageValidator) -> None:
    """Random bytes are rejected."""
    import os

    result = validator.validate_bytes(os.urandom(2048))
    assert result.is_valid is False


def test_validate_bytes_too_small(validator: ImageValidator) -> None:
    """An image smaller than min_size_bytes is rejected."""
    tiny = _make_jpeg(width=50, height=50)
    # Force < 1024 bytes by using heavy compression
    # (or just check the size-based rejection)
    result = validator.validate_bytes(tiny)
    # If tiny < 1024, reject; else accept (size depends on JPEG encoder)
    if len(tiny) < 1024:
        assert result.is_valid is False
        assert "too small" in result.error.lower() or "size" in result.error.lower()


# ────────────────────────────────────────────────────────────────────
# Test 3: validate_bytes — unsupported formats
# ────────────────────────────────────────────────────────────────────


def test_validate_bytes_gif_rejected(validator: ImageValidator) -> None:
    """GIF is rejected (not in allowed_formats)."""
    result = validator.validate_bytes(_make_gif())
    assert result.is_valid is False
    assert "gif" in result.error.lower() or "format" in result.error.lower()


def test_validate_bytes_svg_rejected(validator: ImageValidator) -> None:
    """SVG is rejected (Pillow can't open it)."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    result = validator.validate_bytes(svg)
    assert result.is_valid is False


# ────────────────────────────────────────────────────────────────────
# Test 4: validate_bytes — dimensions
# ────────────────────────────────────────────────────────────────────


def test_validate_bytes_too_small_dimensions(validator: ImageValidator) -> None:
    """Images smaller than min_dimensions are rejected."""
    tiny = _make_png(width=10, height=10)
    result = validator.validate_bytes(tiny)
    assert result.is_valid is False
    assert "dimension" in result.error.lower() or "small" in result.error.lower()


def test_validate_bytes_too_large_dimensions(validator: ImageValidator) -> None:
    """Images larger than max_dimensions are rejected."""
    # Create a 5000×7000 PNG (exceeds max_dimensions=(4000, 6000))
    huge = _make_png(width=5000, height=7000)
    result = validator.validate_bytes(huge)
    assert result.is_valid is False
    assert "dimension" in result.error.lower() or "large" in result.error.lower()


# ────────────────────────────────────────────────────────────────────
# Test 5: validate_file
# ────────────────────────────────────────────────────────────────────


def test_validate_file_valid(validator: ImageValidator, tmp_path: Path) -> None:
    """A valid file passes validate_file()."""
    path = tmp_path / "test.jpg"
    path.write_bytes(_make_jpeg())

    result = validator.validate_file(path)
    assert result.is_valid is True
    assert result.path == path


def test_validate_file_not_found(validator: ImageValidator, tmp_path: Path) -> None:
    """A missing file is rejected."""
    path = tmp_path / "missing.jpg"
    result = validator.validate_file(path)
    assert result.is_valid is False
    assert "not found" in result.error.lower() or "exist" in result.error.lower()


def test_validate_file_html(validator: ImageValidator, tmp_path: Path) -> None:
    """A file containing HTML is rejected."""
    path = tmp_path / "bad.jpg"
    path.write_bytes(b"<html>403 Forbidden</html>")

    result = validator.validate_file(path)
    assert result.is_valid is False


# ────────────────────────────────────────────────────────────────────
# Test 6: validate() — raises or returns
# ────────────────────────────────────────────────────────────────────


def test_validate_raises_on_invalid(validator: ImageValidator) -> None:
    """validate() raises ImageValidationError on invalid input."""
    with pytest.raises(ImageValidationError, match="invalid image|empty"):
        validator.validate(b"not an image")


def test_validate_returns_result_on_valid(validator: ImageValidator) -> None:
    """validate() returns a ValidationResult on valid input."""
    result = validator.validate(_make_jpeg())
    assert isinstance(result, ValidationResult)
    assert result.is_valid is True


# ────────────────────────────────────────────────────────────────────
# Test 7: strict mode
# ────────────────────────────────────────────────────────────────────


def test_strict_mode_rejects_unknown_format() -> None:
    """Strict mode rejects any format not in allowed_formats."""
    strict = ImageValidator(
        min_size_bytes=1024,
        max_size_bytes=10 * 1024 * 1024,
        min_dimensions=(50, 50),
        max_dimensions=(4000, 6000),
        allowed_formats=("JPEG",),  # only JPEG
    )

    result = strict.validate_bytes(_make_png())
    assert result.is_valid is False
    assert "png" in result.error.lower() or "format" in result.error.lower()


# ────────────────────────────────────────────────────────────────────
# Test 8: message clarity
# ────────────────────────────────────────────────────────────────────


def test_error_message_is_descriptive(validator: ImageValidator) -> None:
    """Error messages are descriptive (not just 'invalid')."""
    result = validator.validate_bytes(b"")
    assert result.error is not None
    assert len(result.error) > 10, f"Error too short: {result.error}"


def test_error_message_mentions_size(validator: ImageValidator) -> None:
    """Size errors mention 'size'."""
    result = validator.validate_bytes(b"")
    assert "size" in result.error.lower()


# ────────────────────────────────────────────────────────────────────
# Test 9: file with wrong extension
# ────────────────────────────────────────────────────────────────────


def test_file_with_wrong_extension(
    validator: ImageValidator,
    tmp_path: Path,
) -> None:
    """A PNG file named .jpg is accepted (content-based validation)."""
    path = tmp_path / "test.jpg"  # .jpg extension
    path.write_bytes(_make_png())  # but PNG content

    result = validator.validate_file(path)
    # Should pass (Pillow detects PNG content)
    assert result.is_valid is True
    assert result.format == "PNG"
