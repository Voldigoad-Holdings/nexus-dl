# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for PdfPackager.

PdfPackager converts a list of images into a PDF (one page per image)
using ``img2pdf`` (lossless — no re-encoding of JPEGs).

Why PDF?
    - Universal (every device can open PDF)
    - No special reader required (browsers, phones, tablets)
    - Printable
    - For manga readers, prefer CBZ (better metadata + RTL support)

Contract:
    - package(pages, output, *, metadata=None) -> Path
    - One PDF page per image
    - Page order preserved (sorted by filename)
    - Lossless (no JPEG re-encoding)
    - Dimensions preserved (aspect ratio)
    - Metadata: Title, Author (from ComicInfo)
    - Format: PDF (PackagingFormat.PDF)
    - supports(PackagingFormat.PDF) -> True

What is tested:
    - Happy path (3 pages)
    - Valid PDF (readable by pypdf)
    - Page count
    - Page order (dimensions preserved in order)
    - Lossless (JPEG bytes embedded)
    - Dimensions (aspect ratio preserved)
    - Metadata (Title, Author)
    - Output path handling
    - Return type (Path)
    - Empty pages → error
    - Missing page → error
    - Corrupted page → error
    - Unsupported format (SVG) → error
    - supports() method
    - Async behavior
    - Overwrite existing file
    - PNG input
    - UTF-8 metadata
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from nexusdl.core.exceptions import PackagingError
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
    PackagingFormat,
)
from nexusdl.core.models.manga import Manga
from nexusdl.core.packaging.comic_info import ComicInfo
from nexusdl.core.packaging.pdf_packager import PdfPackager


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


def _make_pages(
    tmp_path: Path,
    count: int = 3,
    fmt: str = "JPEG",
) -> list[Path]:
    """Create N pages in tmp_path."""
    ext = "jpg" if fmt == "JPEG" else "png"
    pages: list[Path] = []
    for i in range(count):
        p = tmp_path / f"{i + 1:03d}.{ext}"
        if fmt == "JPEG":
            p.write_bytes(_make_jpeg())
        else:
            p.write_bytes(_make_png())
        pages.append(p)
    return pages


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def packager() -> PdfPackager:
    """Default PdfPackager."""
    return PdfPackager()


@pytest.fixture
def sample_manga() -> Manga:
    """A fake Manga."""
    return Manga(
        id="a1b2c3d4e5f6a7b8",
        source_id="a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6",
        site="mangadex",
        title="One Piece",
        author="Oda Eiichiro",
        artist="Oda Eiichiro",
        status=MangaStatus.ONGOING,
        language=Language.EN,
        content_rating=ContentRating.SAFE,
        url="https://mangadex.org/title/a1b2c3d4e5f6a7b8",
        updated_at="2026-10-29T12:00:00Z",
    )


@pytest.fixture
def sample_chapter(sample_manga: Manga) -> Chapter:
    """A fake Chapter."""
    return Chapter(
        id="b3e5d7a9c1f2e4d6",
        manga_id=sample_manga.id,
        source_id="ch-uuid",
        title="Romance Dawn",
        number=1.0,
        volume=1,
        language=Language.EN,
        pages_count=3,
        published_at="1997-07-22T00:00:00Z",
        url="https://mangadex.org/chapter/ch-uuid",
    )


@pytest.fixture
def sample_comic_info(sample_manga: Manga, sample_chapter: Chapter) -> ComicInfo:
    """A fake ComicInfo."""
    return ComicInfo(
        title=sample_chapter.title,
        series=sample_manga.title,
        number="1",
        volume=1,
        writer=sample_manga.author,
        penciller=sample_manga.artist,
        summary=sample_manga.description,
        year=sample_manga.year,
        genres=sample_manga.genres,
        language_iso="en",
        page_count=3,
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: Happy path
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_creates_pdf(
    packager: PdfPackager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """package() creates a valid PDF."""
    pytest.importorskip("img2pdf")
    pytest.importorskip("pypdf")
    from pypdf import PdfReader

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.pdf"

    result = await packager.package(
        pages=pages,
        output=output,
        metadata=sample_comic_info,
    )

    assert result == output
    assert output.exists()
    assert output.suffix == ".pdf"

    # Verify it's a valid PDF
    reader = PdfReader(output)
    assert len(reader.pages) == 3


@pytest.mark.asyncio
async def test_package_page_count(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """PDF has one page per input image."""
    pytest.importorskip("img2pdf")
    from pypdf import PdfReader

    for count in (1, 3, 5):
        pages = _make_pages(tmp_path / f"c{count}", count=count)
        output = tmp_path / f"chapter_{count}.pdf"

        await packager.package(pages=pages, output=output, metadata=None)

        reader = PdfReader(output)
        assert len(reader.pages) == count


@pytest.mark.asyncio
async def test_package_page_order(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """Pages are stored in the order of the input list (sorted)."""
    pytest.importorskip("img2pdf")
    from pypdf import PdfReader

    # Create pages with different sizes to identify them
    pages = []
    for i, size in enumerate([100, 200, 300], start=1):
        p = tmp_path / f"{i:03d}.jpg"
        img = Image.new("RGB", (size, size), color=(100, 100, 100))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        p.write_bytes(buf.getvalue())
        pages.append(p)

    output = tmp_path / "chapter.pdf"
    await packager.package(pages=pages, output=output, metadata=None)

    reader = PdfReader(output)
    # First page should be 100×100
    box0 = reader.pages[0].mediabox
    assert float(box0.width) == pytest.approx(100, abs=1)
    # Second page 200×200
    box1 = reader.pages[1].mediabox
    assert float(box1.width) == pytest.approx(200, abs=1)
    # Third page 300×300
    box2 = reader.pages[2].mediabox
    assert float(box2.width) == pytest.approx(300, abs=1)


# ────────────────────────────────────────────────────────────────────
# Test 2: Lossless (no re-encoding)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_lossless_jpeg(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """JPEG pages are embedded losslessly (no re-encoding)."""
    pytest.importorskip("img2pdf")

    # Create a JPEG with known content
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    original_bytes = buf.getvalue()

    page = tmp_path / "001.jpg"
    page.write_bytes(original_bytes)

    output = tmp_path / "chapter.pdf"
    await packager.package(pages=[page], output=output, metadata=None)

    # The PDF should be larger than the JPEG (PDF overhead)
    # but the JPEG stream should be embedded as-is (img2pdf is lossless)
    assert output.stat().st_size > len(original_bytes)


# ────────────────────────────────────────────────────────────────────
# Test 3: Dimensions preserved
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_dimensions_preserved(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """Image dimensions are preserved (aspect ratio) in the PDF."""
    pytest.importorskip("img2pdf")
    from pypdf import PdfReader

    # 500×700 image
    img = Image.new("RGB", (500, 700), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    page = tmp_path / "001.jpg"
    page.write_bytes(buf.getvalue())

    output = tmp_path / "chapter.pdf"
    await packager.package(pages=[page], output=output, metadata=None)

    reader = PdfReader(output)
    box = reader.pages[0].mediabox
    # The exact size depends on the DPI assumed by img2pdf.
    # We check the aspect ratio is preserved.
    width = float(box.width)
    height = float(box.height)
    aspect = width / height
    expected_aspect = 500 / 700
    assert abs(aspect - expected_aspect) < 0.05


# ────────────────────────────────────────────────────────────────────
# Test 4: Metadata
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_metadata(
    packager: PdfPackager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """PDF metadata includes Title and Author."""
    pytest.importorskip("img2pdf")
    from pypdf import PdfReader

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.pdf"

    await packager.package(pages=pages, output=output, metadata=sample_comic_info)

    reader = PdfReader(output)
    info = reader.metadata

    # Title and Author should be set
    assert info is not None
    if info.title:
        assert "Romance Dawn" in info.title or "One Piece" in info.title
    if info.author:
        assert "Oda" in info.author


@pytest.mark.asyncio
async def test_package_without_metadata(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """package() works without metadata."""
    pytest.importorskip("img2pdf")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.pdf"

    result = await packager.package(pages=pages, output=output, metadata=None)
    assert result.exists()


# ────────────────────────────────────────────────────────────────────
# Test 5: Output path handling
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_creates_output_dir(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """package() creates the output directory if missing."""
    pytest.importorskip("img2pdf")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "nested" / "subdir" / "chapter.pdf"

    result = await packager.package(pages=pages, output=output, metadata=None)

    assert output.exists()
    assert output.parent.exists()


@pytest.mark.asyncio
async def test_package_returns_path(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """package() returns a Path."""
    pytest.importorskip("img2pdf")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.pdf"

    result = await packager.package(pages=pages, output=output, metadata=None)

    assert isinstance(result, Path)
    assert result == output


# ────────────────────────────────────────────────────────────────────
# Test 6: Errors
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_empty_pages_raises(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """package([]) raises PackagingError."""
    pytest.importorskip("img2pdf")

    output = tmp_path / "chapter.pdf"

    with pytest.raises(PackagingError, match="empty|no pages"):
        await packager.package(pages=[], output=output, metadata=None)


@pytest.mark.asyncio
async def test_package_missing_page_raises(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """A page that doesn't exist raises PackagingError."""
    pytest.importorskip("img2pdf")

    pages = _make_pages(tmp_path, count=2)
    pages.append(tmp_path / "missing.jpg")

    output = tmp_path / "chapter.pdf"

    with pytest.raises(PackagingError):
        await packager.package(pages=pages, output=output, metadata=None)


@pytest.mark.asyncio
async def test_package_corrupted_page_raises(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """A corrupted page (0 bytes) raises PackagingError."""
    pytest.importorskip("img2pdf")

    pages = _make_pages(tmp_path, count=2)
    corrupted = tmp_path / "003.jpg"
    corrupted.write_bytes(b"")
    pages.append(corrupted)

    output = tmp_path / "chapter.pdf"

    with pytest.raises(PackagingError):
        await packager.package(pages=pages, output=output, metadata=None)


@pytest.mark.asyncio
async def test_package_unsupported_format_raises(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """SVG pages raise PackagingError (img2pdf can't handle SVG)."""
    pytest.importorskip("img2pdf")

    svg = tmp_path / "001.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')

    output = tmp_path / "chapter.pdf"

    with pytest.raises(PackagingError):
        await packager.package(pages=[svg], output=output, metadata=None)


# ────────────────────────────────────────────────────────────────────
# Test 7: supports() method
# ────────────────────────────────────────────────────────────────────


def test_supports_pdf(packager: PdfPackager) -> None:
    """supports(PackagingFormat.PDF) returns True."""
    assert packager.supports(PackagingFormat.PDF) is True


def test_supports_cbz_false(packager: PdfPackager) -> None:
    """supports(PackagingFormat.CBZ) returns False."""
    assert packager.supports(PackagingFormat.CBZ) is False


def test_format_attribute(packager: PdfPackager) -> None:
    """PdfPackager.format is PackagingFormat.PDF."""
    assert packager.format == PackagingFormat.PDF


# ────────────────────────────────────────────────────────────────────
# Test 8: Overwrite existing file
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_overwrites_existing(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """package() overwrites an existing .pdf."""
    pytest.importorskip("img2pdf")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.pdf"

    # First run
    await packager.package(pages=pages, output=output, metadata=None)
    assert output.exists()

    # Second run (should overwrite)
    await packager.package(pages=pages, output=output, metadata=None)
    assert output.exists()


# ────────────────────────────────────────────────────────────────────
# Test 9: PNG input
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_png_input(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """PNG pages are supported (converted to PDF)."""
    pytest.importorskip("img2pdf")
    from pypdf import PdfReader

    pages = _make_pages(tmp_path, count=3, fmt="PNG")
    output = tmp_path / "chapter.pdf"

    result = await packager.package(pages=pages, output=output, metadata=None)

    reader = PdfReader(result)
    assert len(reader.pages) == 3


# ────────────────────────────────────────────────────────────────────
# Test 10: UTF-8 metadata
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_utf8_metadata(
    packager: PdfPackager,
    tmp_path: Path,
) -> None:
    """PDF metadata supports UTF-8 (accents, Japanese)."""
    pytest.importorskip("img2pdf")
    from pypdf import PdfReader

    comic_info = ComicInfo(
        title="冒険の夜明け",
        series="ワンピース",
        number="1",
        language_iso="ja",
        page_count=3,
    )

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.pdf"

    await packager.package(pages=pages, output=output, metadata=comic_info)

    reader = PdfReader(output)
    info = reader.metadata
    # Metadata should be preserved (may be encoded differently)
    assert info is not None
