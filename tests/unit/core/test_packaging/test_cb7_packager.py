# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for Cb7Packager.

Cb7Packager creates a ``.cb7`` archive (7z) containing:
    - All chapter pages (001.png, 002.png, ...)
    - ComicInfo.xml (metadata)

CB7 is the **free-software alternative to CBR** (see ADR-002).
It requires ``py7zr`` (LGPL-2.1+, compatible with GPL-3.0-or-later).

What is tested:
    - Happy path (3 pages + ComicInfo.xml)
    - Format (valid 7z)
    - Contents (all pages present)
    - Order (pages sorted by filename)
    - ComicInfo.xml (present, valid, UTF-8)
    - Output path handling
    - Return type (Path)
    - Empty pages → error
    - Missing page → error
    - Corrupted page → error
    - Compression level validation
    - Existing file (overwrite)
    - supports() method
    - Async behavior
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
from nexusdl.core.packaging.cb7_packager import Cb7Packager
from nexusdl.core.packaging.comic_info import ComicInfo


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_png(width: int = 200, height: int = 300) -> bytes:
    """Generate a valid PNG in memory."""
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _make_pages(tmp_path: Path, count: int = 3) -> list[Path]:
    """Create N pages in tmp_path."""
    pages: list[Path] = []
    for i in range(count):
        p = tmp_path / f"{i + 1:03d}.png"
        p.write_bytes(_make_png())
        pages.append(p)
    return pages


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def packager() -> Cb7Packager:
    """Default Cb7Packager."""
    return Cb7Packager()


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
async def test_package_creates_cb7(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """package() creates a valid CB7 archive."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    result = await packager.package(
        pages=pages,
        output=output,
        metadata=sample_comic_info,
    )

    assert result == output
    assert output.exists()
    assert output.suffix == ".cb7"

    # Verify it's a valid 7z file
    import py7zr
    assert py7zr.is_7zfile(output)


@pytest.mark.asyncio
async def test_package_contains_all_pages(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """All pages are present in the archive."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    await packager.package(pages=pages, output=output, metadata=sample_comic_info)

    import py7zr
    with py7zr.SevenZipFile(output, mode="r") as zf:
        names = zf.getnames()

    for i in range(3):
        assert f"{i + 1:03d}.png" in names


@pytest.mark.asyncio
async def test_package_contains_comic_info(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """ComicInfo.xml is present in the archive."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    await packager.package(pages=pages, output=output, metadata=sample_comic_info)

    import py7zr
    with py7zr.SevenZipFile(output, mode="r") as zf:
        names = zf.getnames()

    assert "ComicInfo.xml" in names


@pytest.mark.asyncio
async def test_package_comic_info_content(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """ComicInfo.xml contains the expected XML content."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    await packager.package(pages=pages, output=output, metadata=sample_comic_info)

    import py7zr
    with py7zr.SevenZipFile(output, mode="r") as zf:
        data = zf.read(["ComicInfo.xml"])
        xml_content = data["ComicInfo.xml"].read().decode("utf-8")

    assert "<ComicInfo>" in xml_content
    assert "<Series>One Piece</Series>" in xml_content
    assert "<Title>Romance Dawn</Title>" in xml_content
    assert "<LanguageISO>en</LanguageISO>" in xml_content


# ────────────────────────────────────────────────────────────────────
# Test 2: Page order
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_pages_sorted(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """Pages are stored in sorted order (001, 002, 003)."""
    pytest.importorskip("py7zr")

    # Create pages in reverse order to test sorting
    pages = []
    for i in reversed(range(1, 4)):
        p = tmp_path / f"{i:03d}.png"
        p.write_bytes(_make_png())
        pages.append(p)

    output = tmp_path / "chapter.cb7"
    await packager.package(pages=pages, output=output, metadata=sample_comic_info)

    import py7zr
    with py7zr.SevenZipFile(output, mode="r") as zf:
        names = zf.getnames()

    # Image names should be sorted
    image_names = [n for n in names if n.endswith(".png")]
    assert image_names == sorted(image_names)


# ────────────────────────────────────────────────────────────────────
# Test 3: Output path handling
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_creates_output_dir(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """package() creates the output directory if missing."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "nested" / "subdir" / "chapter.cb7"

    result = await packager.package(pages=pages, output=output, metadata=sample_comic_info)

    assert output.exists()
    assert output.parent.exists()


@pytest.mark.asyncio
async def test_package_returns_path(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """package() returns a Path."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    result = await packager.package(pages=pages, output=output, metadata=sample_comic_info)

    assert isinstance(result, Path)
    assert result == output


# ────────────────────────────────────────────────────────────────────
# Test 4: Errors
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_empty_pages_raises(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """package([]) raises PackagingError."""
    pytest.importorskip("py7zr")

    output = tmp_path / "chapter.cb7"

    with pytest.raises(PackagingError, match="empty|no pages"):
        await packager.package(pages=[], output=output, metadata=sample_comic_info)


@pytest.mark.asyncio
async def test_package_missing_page_raises(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """A page that doesn't exist raises PackagingError."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=2)
    pages.append(tmp_path / "missing.png")  # doesn't exist

    output = tmp_path / "chapter.cb7"

    with pytest.raises(PackagingError):
        await packager.package(pages=pages, output=output, metadata=sample_comic_info)


@pytest.mark.asyncio
async def test_package_corrupted_page_raises(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """A corrupted page (0 bytes) raises PackagingError."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=2)
    corrupted = tmp_path / "003.png"
    corrupted.write_bytes(b"")  # 0 bytes
    pages.append(corrupted)

    output = tmp_path / "chapter.cb7"

    with pytest.raises(PackagingError):
        await packager.package(pages=pages, output=output, metadata=sample_comic_info)


# ────────────────────────────────────────────────────────────────────
# Test 5: Compression level
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compression_level_valid(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """Compression level between 1 and 9 is accepted."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)

    for level in (1, 5, 9):
        output = tmp_path / f"chapter_l{level}.cb7"
        result = await packager.package(
            pages=pages,
            output=output,
            metadata=sample_comic_info,
            compression_level=level,
        )
        assert result.exists()


@pytest.mark.asyncio
async def test_compression_level_invalid(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """Invalid compression level raises."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    with pytest.raises((ValueError, PackagingError)):
        await packager.package(
            pages=pages,
            output=output,
            metadata=sample_comic_info,
            compression_level=0,
        )

    with pytest.raises((ValueError, PackagingError)):
        await packager.package(
            pages=pages,
            output=output,
            metadata=sample_comic_info,
            compression_level=10,
        )


# ────────────────────────────────────────────────────────────────────
# Test 6: Existing file
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_overwrites_existing(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_comic_info: ComicInfo,
) -> None:
    """package() overwrites an existing .cb7."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    # First run
    await packager.package(pages=pages, output=output, metadata=sample_comic_info)
    size1 = output.stat().st_size

    # Second run (should overwrite)
    await packager.package(pages=pages, output=output, metadata=sample_comic_info)
    size2 = output.stat().st_size

    # Should still be a valid CB7
    import py7zr
    assert py7zr.is_7zfile(output)


# ────────────────────────────────────────────────────────────────────
# Test 7: supports() method
# ────────────────────────────────────────────────────────────────────


def test_supports_cb7(packager: Cb7Packager) -> None:
    """supports(PackagingFormat.CB7) returns True."""
    assert packager.supports(PackagingFormat.CB7) is True


def test_supports_cbz_false(packager: Cb7Packager) -> None:
    """supports(PackagingFormat.CBZ) returns False."""
    assert packager.supports(PackagingFormat.CBZ) is False


def test_supports_cbr_false(packager: Cb7Packager) -> None:
    """supports(PackagingFormat.CBR) returns False (ADR-002)."""
    # CBR is not supported (ADR-002)
    # If the enum has CBR, it should return False
    if hasattr(PackagingFormat, "CBR"):
        assert packager.supports(PackagingFormat.CBR) is False


def test_format_attribute(packager: Cb7Packager) -> None:
    """Cb7Packager.format is PackagingFormat.CB7."""
    assert packager.format == PackagingFormat.CB7


# ────────────────────────────────────────────────────────────────────
# Test 8: Metadata optional
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_package_without_metadata(
    packager: Cb7Packager,
    tmp_path: Path,
) -> None:
    """package() works without metadata (ComicInfo.xml absent)."""
    pytest.importorskip("py7zr")

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    result = await packager.package(pages=pages, output=output, metadata=None)

    assert result.exists()

    import py7zr
    with py7zr.SevenZipFile(output, mode="r") as zf:
        names = zf.getnames()

    # No ComicInfo.xml expected
    assert "ComicInfo.xml" not in names
    # But pages are present
    assert len([n for n in names if n.endswith(".png")]) == 3


# ────────────────────────────────────────────────────────────────────
# Test 9: UTF-8 in ComicInfo
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_comic_info_utf8(
    packager: Cb7Packager,
    tmp_path: Path,
    sample_manga: Manga,
    sample_chapter: Chapter,
) -> None:
    """ComicInfo.xml supports UTF-8 (accents, Japanese)."""
    pytest.importorskip("py7zr")

    comic_info = ComicInfo(
        title="Romance Dawn — 冒険の夜明け",
        series="One Piece",
        number="1",
        language_iso="en",
        page_count=3,
    )

    pages = _make_pages(tmp_path, count=3)
    output = tmp_path / "chapter.cb7"

    await packager.package(pages=pages, output=output, metadata=comic_info)

    import py7zr
    with py7zr.SevenZipFile(output, mode="r") as zf:
        data = zf.read(["ComicInfo.xml"])
        xml_content = data["ComicInfo.xml"].read().decode("utf-8")

    assert "Romance Dawn" in xml_content
    assert "冒険の夜明け" in xml_content
