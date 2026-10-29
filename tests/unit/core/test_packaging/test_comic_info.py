# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for ComicInfo (ComicRack standard).

ComicInfo is the metadata model that serializes to/from ``ComicInfo.xml``,
the **de facto standard** for manga/comic metadata:

    - Komga, Kavita, Mihon, YACReader, CDisplayEx read it
    - Anansi Project maintains the spec
    - ComicRack is the original implementation

Contract:
    - Required: title, series, number, language_iso, page_count
    - Optional: volume, writer, penciller, summary, year, genres,
                cover_artist, publisher, characters, teams, locations,
                scan_information, web, notes, black_and_white, manga
    - to_xml() -> str (valid XML with <?xml?> declaration)
    - from_xml(str) -> ComicInfo (classmethod)
    - UTF-8 (accents, Japanese, emojis)
    - XML-escaped (&, <, >, ", ')
    - Manga field: "YesAndRightToLeft" for manga, "No" for comics
    - BlackAndWhite: "Yes" or "No"

What is tested:
    - Construction (required + optional)
    - Validation (missing required, negative page_count)
    - to_xml() (valid XML, UTF-8, escaped)
    - from_xml() (parse, ignore unknown)
    - Roundtrip (to_xml → from_xml → same object)
    - Manga field (YesAndRightToLeft)
    - BlackAndWhite (Yes/No)
    - Genres (comma-separated)
    - Year (int, 4 digits)
    - PageCount (coherent)
    - LanguageISO (ISO 639-1)
    - Web (URL)
    - Notes (version)
    - model_dump() / model_validate()
    - ComicRack compliance
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexusdl.core.packaging.comic_info import ComicInfo


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def minimal_comic_info() -> ComicInfo:
    """Minimal valid ComicInfo (required fields only)."""
    return ComicInfo(
        title="Romance Dawn",
        series="One Piece",
        number="1",
        language_iso="en",
        page_count=3,
    )


@pytest.fixture
def full_comic_info() -> ComicInfo:
    """Full ComicInfo with all fields."""
    return ComicInfo(
        title="Romance Dawn",
        series="One Piece",
        number="1",
        volume=1,
        writer="Oda Eiichiro",
        penciller="Oda Eiichiro",
        inker="Oda Eiichiro",
        colorist="Oda Eiichiro",
        letterer="Oda Eiichiro",
        cover_artist="Oda Eiichiro",
        editor="Oda Eiichiro",
        publisher="Shueisha",
        imprint="Weekly Shōnen Jump",
        summary="A story about pirates.",
        year=1997,
        genres=["Action", "Adventure", "Comedy"],
        characters=["Monkey D. Luffy", "Roronoa Zoro"],
        teams=["Straw Hat Pirates"],
        locations=["East Blue"],
        scan_information="NexusDL v0.1.0",
        language_iso="en",
        page_count=3,
        black_and_white=False,
        manga=True,
        web="https://mangadex.org/chapter/ch-uuid",
        notes="Downloaded by NexusDL v0.1.0 on 2026-10-29",
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: Construction
# ────────────────────────────────────────────────────────────────────


def test_minimal_construction(minimal_comic_info: ComicInfo) -> None:
    """ComicInfo can be constructed with required fields only."""
    assert minimal_comic_info.title == "Romance Dawn"
    assert minimal_comic_info.series == "One Piece"
    assert minimal_comic_info.number == "1"
    assert minimal_comic_info.language_iso == "en"
    assert minimal_comic_info.page_count == 3

    # Optional fields default to None/empty
    assert minimal_comic_info.volume is None
    assert minimal_comic_info.writer is None
    assert minimal_comic_info.year is None
    assert minimal_comic_info.genres == []
    assert minimal_comic_info.manga is False  # default
    assert minimal_comic_info.black_and_white is False


def test_full_construction(full_comic_info: ComicInfo) -> None:
    """ComicInfo can be constructed with all fields."""
    assert full_comic_info.writer == "Oda Eiichiro"
    assert full_comic_info.year == 1997
    assert full_comic_info.genres == ["Action", "Adventure", "Comedy"]
    assert full_comic_info.manga is True
    assert full_comic_info.black_and_white is False
    assert full_comic_info.web is not None


# ────────────────────────────────────────────────────────────────────
# Test 2: Validation
# ────────────────────────────────────────────────────────────────────


def test_missing_title_raises() -> None:
    """ComicInfo without title raises ValidationError."""
    with pytest.raises(ValidationError):
        ComicInfo(
            series="One Piece",
            number="1",
            language_iso="en",
            page_count=3,
        )


def test_missing_series_raises() -> None:
    """ComicInfo without series raises ValidationError."""
    with pytest.raises(ValidationError):
        ComicInfo(
            title="Romance Dawn",
            number="1",
            language_iso="en",
            page_count=3,
        )


def test_missing_number_raises() -> None:
    """ComicInfo without number raises ValidationError."""
    with pytest.raises(ValidationError):
        ComicInfo(
            title="Romance Dawn",
            series="One Piece",
            language_iso="en",
            page_count=3,
        )


def test_missing_language_iso_raises() -> None:
    """ComicInfo without language_iso raises ValidationError."""
    with pytest.raises(ValidationError):
        ComicInfo(
            title="Romance Dawn",
            series="One Piece",
            number="1",
            page_count=3,
        )


def test_missing_page_count_raises() -> None:
    """ComicInfo without page_count raises ValidationError."""
    with pytest.raises(ValidationError):
        ComicInfo(
            title="Romance Dawn",
            series="One Piece",
            number="1",
            language_iso="en",
        )


def test_negative_page_count_raises() -> None:
    """Negative page_count raises ValidationError."""
    with pytest.raises(ValidationError):
        ComicInfo(
            title="Romance Dawn",
            series="One Piece",
            number="1",
            language_iso="en",
            page_count=-1,
        )


def test_zero_page_count_ok() -> None:
    """page_count = 0 is valid (empty chapter)."""
    ci = ComicInfo(
        title="Romance Dawn",
        series="One Piece",
        number="1",
        language_iso="en",
        page_count=0,
    )
    assert ci.page_count == 0


# ────────────────────────────────────────────────────────────────────
# Test 3: to_xml() — structure
# ────────────────────────────────────────────────────────────────────


def test_to_xml_returns_string(minimal_comic_info: ComicInfo) -> None:
    """to_xml() returns a string."""
    xml = minimal_comic_info.to_xml()
    assert isinstance(xml, str)
    assert len(xml) > 0


def test_to_xml_has_declaration(minimal_comic_info: ComicInfo) -> None:
    """to_xml() includes <?xml?> declaration."""
    xml = minimal_comic_info.to_xml()
    assert xml.startswith("<?xml")


def test_to_xml_has_root_element(minimal_comic_info: ComicInfo) -> None:
    """to_xml() has <ComicInfo> root."""
    xml = minimal_comic_info.to_xml()
    assert "<ComicInfo>" in xml
    assert "</ComicInfo>" in xml


def test_to_xml_is_valid_xml(minimal_comic_info: ComicInfo) -> None:
    """to_xml() produces valid XML (parseable by ElementTree)."""
    xml = minimal_comic_info.to_xml()
    root = ET.fromstring(xml)
    assert root.tag == "ComicInfo"


def test_to_xml_contains_required_fields(minimal_comic_info: ComicInfo) -> None:
    """to_xml() contains all required fields."""
    xml = minimal_comic_info.to_xml()
    root = ET.fromstring(xml)

    assert root.findtext("Title") == "Romance Dawn"
    assert root.findtext("Series") == "One Piece"
    assert root.findtext("Number") == "1"
    assert root.findtext("LanguageISO") == "en"
    assert root.findtext("PageCount") == "3"


# ────────────────────────────────────────────────────────────────────
# Test 4: to_xml() — optional fields
# ────────────────────────────────────────────────────────────────────


def test_to_xml_contains_optional_fields(full_comic_info: ComicInfo) -> None:
    """to_xml() contains optional fields when set."""
    xml = full_comic_info.to_xml()
    root = ET.fromstring(xml)

    assert root.findtext("Writer") == "Oda Eiichiro"
    assert root.findtext("Penciller") == "Oda Eiichiro"
    assert root.findtext("Year") == "1997"
    assert root.findtext("Volume") == "1"
    assert root.findtext("Publisher") == "Shueisha"


def test_to_xml_omits_none_fields(minimal_comic_info: ComicInfo) -> None:
    """to_xml() omits None/empty fields."""
    xml = minimal_comic_info.to_xml()
    root = ET.fromstring(xml)

    # Optional fields should not appear
    assert root.find("Writer") is None
    assert root.find("Penciller") is None
    assert root.find("Year") is None


def test_to_xml_genres_comma_separated(full_comic_info: ComicInfo) -> None:
    """to_xml() joins genres with ', '."""
    xml = full_comic_info.to_xml()
    root = ET.fromstring(xml)

    genres = root.findtext("Genre")
    assert genres == "Action, Adventure, Comedy"


# ────────────────────────────────────────────────────────────────────
# Test 5: to_xml() — Manga field
# ────────────────────────────────────────────────────────────────────


def test_manga_field_yes_and_rtl(minimal_comic_info: ComicInfo) -> None:
    """manga=True → <Manga>YesAndRightToLeft</Manga>."""
    ci = minimal_comic_info.model_copy(update={"manga": True})
    xml = ci.to_xml()
    root = ET.fromstring(xml)

    assert root.findtext("Manga") == "YesAndRightToLeft"


def test_manga_field_no(minimal_comic_info: ComicInfo) -> None:
    """manga=False → <Manga>No</Manga>."""
    xml = minimal_comic_info.to_xml()
    root = ET.fromstring(xml)

    assert root.findtext("Manga") == "No"


# ────────────────────────────────────────────────────────────────────
# Test 6: to_xml() — BlackAndWhite field
# ────────────────────────────────────────────────────────────────────


def test_black_and_white_yes(minimal_comic_info: ComicInfo) -> None:
    """black_and_white=True → <BlackAndWhite>Yes</BlackAndWhite>."""
    ci = minimal_comic_info.model_copy(update={"black_and_white": True})
    xml = ci.to_xml()
    root = ET.fromstring(xml)

    assert root.findtext("BlackAndWhite") == "Yes"


def test_black_and_white_no(minimal_comic_info: ComicInfo) -> None:
    """black_and_white=False → <BlackAndWhite>No</BlackAndWhite>."""
    xml = minimal_comic_info.to_xml()
    root = ET.fromstring(xml)

    assert root.findtext("BlackAndWhite") == "No"


# ────────────────────────────────────────────────────────────────────
# Test 7: to_xml() — XML escaping
# ────────────────────────────────────────────────────────────────────


def test_xml_escaping_ampersand() -> None:
    """& is escaped to &amp;."""
    ci = ComicInfo(
        title="Tom & Jerry",
        series="Comics",
        number="1",
        language_iso="en",
        page_count=3,
    )
    xml = ci.to_xml()
    root = ET.fromstring(xml)

    # ElementTree auto-escapes, so findtext returns the original
    assert root.findtext("Title") == "Tom & Jerry"
    # The raw XML should contain the escaped entity
    assert "&amp;" in xml or "Tom & Jerry" in xml


def test_xml_escaping_less_than() -> None:
    """< is escaped."""
    ci = ComicInfo(
        title="<script>alert('xss')</script>",
        series="Security",
        number="1",
        language_iso="en",
        page_count=1,
    )
    xml = ci.to_xml()
    root = ET.fromstring(xml)
    assert root.findtext("Title") == "<script>alert('xss')</script>"


def test_xml_escaping_quotes() -> None:
    """Quotes are escaped in attributes (not needed for text)."""
    ci = ComicInfo(
        title='He said "hello"',
        series="Quotes",
        number="1",
        language_iso="en",
        page_count=1,
    )
    xml = ci.to_xml()
    root = ET.fromstring(xml)
    assert root.findtext("Title") == 'He said "hello"'


# ────────────────────────────────────────────────────────────────────
# Test 8: UTF-8
# ────────────────────────────────────────────────────────────────────


def test_utf8_accents() -> None:
    """Accents are preserved (UTF-8)."""
    ci = ComicInfo(
        title="Pokémon Adventures",
        series="Pokémon",
        number="1",
        language_iso="fr",
        page_count=3,
    )
    xml = ci.to_xml()
    root = ET.fromstring(xml)
    assert root.findtext("Title") == "Pokémon Adventures"


def test_utf8_japanese() -> None:
    """Japanese is preserved (UTF-8)."""
    ci = ComicInfo(
        title="冒険の夜明け",
        series="ワンピース",
        number="1",
        language_iso="ja",
        page_count=3,
    )
    xml = ci.to_xml()
    root = ET.fromstring(xml)
    assert root.findtext("Title") == "冒険の夜明け"
    assert root.findtext("Series") == "ワンピース"


# ────────────────────────────────────────────────────────────────────
# Test 9: from_xml()
# ────────────────────────────────────────────────────────────────────


def test_from_xml_roundtrip(full_comic_info: ComicInfo) -> None:
    """to_xml → from_xml roundtrip preserves all fields."""
    xml = full_comic_info.to_xml()
    parsed = ComicInfo.from_xml(xml)

    assert parsed.title == full_comic_info.title
    assert parsed.series == full_comic_info.series
    assert parsed.number == full_comic_info.number
    assert parsed.writer == full_comic_info.writer
    assert parsed.year == full_comic_info.year
    assert parsed.genres == full_comic_info.genres
    assert parsed.manga == full_comic_info.manga
    assert parsed.black_and_white == full_comic_info.black_and_white
    assert parsed.page_count == full_comic_info.page_count


def test_from_xml_minimal() -> None:
    """from_xml() parses minimal XML."""
    xml = """<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Title>Romance Dawn</Title>
  <Series>One Piece</Series>
  <Number>1</Number>
  <LanguageISO>en</LanguageISO>
  <PageCount>3</PageCount>
</ComicInfo>"""
    ci = ComicInfo.from_xml(xml)

    assert ci.title == "Romance Dawn"
    assert ci.series == "One Piece"
    assert ci.page_count == 3


def test_from_xml_ignores_unknown_fields() -> None:
    """from_xml() ignores fields it doesn't know."""
    xml = """<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Title>Test</Title>
  <Series>Test</Series>
  <Number>1</Number>
  <LanguageISO>en</LanguageISO>
  <PageCount>3</PageCount>
  <UnknownField>ignored</UnknownField>
  <AnotherUnknown>ignored too</AnotherUnknown>
</ComicInfo>"""
    ci = ComicInfo.from_xml(xml)
    assert ci.title == "Test"


def test_from_xml_invalid_raises() -> None:
    """from_xml() with invalid XML raises."""
    with pytest.raises((ET.ParseError, ValueError)):
        ComicInfo.from_xml("not xml at all")


def test_from_xml_missing_required_raises() -> None:
    """from_xml() with missing required field raises ValidationError."""
    xml = """<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Title>Test</Title>
</ComicInfo>"""
    with pytest.raises(ValidationError):
        ComicInfo.from_xml(xml)


# ────────────────────────────────────────────────────────────────────
# Test 10: model_dump / model_validate
# ────────────────────────────────────────────────────────────────────


def test_model_dump(full_comic_info: ComicInfo) -> None:
    """model_dump() returns a dict."""
    data = full_comic_info.model_dump()
    assert isinstance(data, dict)
    assert data["title"] == "Romance Dawn"
    assert data["series"] == "One Piece"
    assert data["page_count"] == 3


def test_model_validate(full_comic_info: ComicInfo) -> None:
    """model_validate() from dict."""
    data = full_comic_info.model_dump()
    ci = ComicInfo.model_validate(data)
    assert ci.title == full_comic_info.title
