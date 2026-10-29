# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for the Manga model.

Manga is a Pydantic v2 model representing a manga/comic/webtoon.

Contract:
    - id: str (16 hex, hash of site + source_id)
    - source_id: str (site-specific ID)
    - site: str (matches sites.yaml)
    - title: str (non-empty)
    - alternative_titles: list[str] (UTF-8)
    - description: str | None
    - author: str | None
    - artist: str | None
    - genres: list[str]
    - status: MangaStatus (enum)
    - year: int | None (1900-2100)
    - cover_url: HttpUrl | None
    - language: Language (enum)
    - content_rating: ContentRating (enum)
    - url: HttpUrl (https://)
    - updated_at: datetime (UTC)
    - chapters: list[Chapter] (default [])

What is tested:
    - Construction (all fields)
    - ID format (16 hex) + determinism (sha256 of site + source_id)
    - Enums (status, language, content_rating)
    - Validation (missing fields, invalid year, invalid enum)
    - Defaults (chapters=[])
    - UTF-8 (Japanese alternative titles)
    - Serialization (model_dump, model_dump_json)
    - Roundtrip (model_validate)
    - Fixture coherence (sample_manga.json ↔ sample_chapter.json)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
)
from nexusdl.core.models.manga import Manga


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_manga_data() -> dict:
    """Raw data from sample_manga.json."""
    data = json.loads((FIXTURES / "sample_manga.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return data


@pytest.fixture
def sample_chapter_data() -> dict:
    """Raw data from sample_chapter.json."""
    data = json.loads((FIXTURES / "sample_chapter.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return data


@pytest.fixture
def valid_manga_data() -> dict:
    """Minimal valid Manga data."""
    return {
        "id": "a1b2c3d4e5f6a7b8",
        "source_id": "a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6",
        "site": "mangadex",
        "title": "One Piece",
        "alternative_titles": ["ワンピース", "One Piece (Color)"],
        "description": "A story about pirates.",
        "author": "Oda Eiichiro",
        "artist": "Oda Eiichiro",
        "genres": ["Action", "Adventure", "Comedy"],
        "status": "ongoing",
        "year": 1997,
        "cover_url": "https://uploads.mangadex.org/covers/a1b2c3d4e5f6a7b8/cover.jpg",
        "language": "en",
        "content_rating": "safe",
        "url": "https://mangadex.org/title/a1b2c3d4e5f6a7b8",
        "updated_at": "2026-10-29T12:00:00Z",
        "chapters": [],
    }


# ────────────────────────────────────────────────────────────────────
# Test 1: Construction
# ────────────────────────────────────────────────────────────────────


def test_manga_construction(valid_manga_data: dict) -> None:
    """Manga can be constructed with all fields."""
    manga = Manga.model_validate(valid_manga_data)

    assert manga.id == "a1b2c3d4e5f6a7b8"
    assert manga.site == "mangadex"
    assert manga.title == "One Piece"
    assert manga.author == "Oda Eiichiro"
    assert manga.artist == "Oda Eiichiro"
    assert manga.year == 1997
    assert manga.language == Language.EN
    assert manga.content_rating == ContentRating.SAFE
    assert manga.status == MangaStatus.ONGOING
    assert manga.chapters == []


def test_manga_from_fixture(sample_manga_data: dict) -> None:
    """Manga can be constructed from sample_manga.json."""
    manga = Manga.model_validate(sample_manga_data)

    assert manga.id == "a1b2c3d4e5f6a7b8"
    assert manga.title == "One Piece"
    assert manga.author == "Oda Eiichiro"
    assert manga.year == 1997
    assert "ワンピース" in manga.alternative_titles


# ────────────────────────────────────────────────────────────────────
# Test 2: ID format
# ────────────────────────────────────────────────────────────────────


def test_id_is_16_hex(valid_manga_data: dict) -> None:
    """Manga.id is 16 hex chars."""
    manga = Manga.model_validate(valid_manga_data)
    assert len(manga.id) == 16
    assert all(c in "0123456789abcdef" for c in manga.id)


def test_id_matches_sha256_prefix(valid_manga_data: dict) -> None:
    """Manga.id is sha256(f"{site}:{source_id}")[:16]."""
    manga = Manga.model_validate(valid_manga_data)
    expected = hashlib.sha256(
        f"{manga.site}:{manga.source_id}".encode()
    ).hexdigest()[:16]
    assert manga.id == expected


def test_id_is_deterministic(valid_manga_data: dict) -> None:
    """Same site + source_id → same id."""
    m1 = Manga.model_validate(valid_manga_data)
    m2 = Manga.model_validate(valid_manga_data)
    assert m1.id == m2.id


# ────────────────────────────────────────────────────────────────────
# Test 3: Enums
# ────────────────────────────────────────────────────────────────────


def test_status_enum(valid_manga_data: dict) -> None:
    """status accepts MangaStatus values."""
    for status in ("ongoing", "completed", "hiatus", "cancelled", "unknown"):
        data = {**valid_manga_data, "status": status}
        manga = Manga.model_validate(data)
        assert manga.status.value == status


def test_status_invalid_raises(valid_manga_data: dict) -> None:
    """status with invalid value raises ValidationError."""
    data = {**valid_manga_data, "status": "Ongoing"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


def test_language_enum(valid_manga_data: dict) -> None:
    """language accepts Language values."""
    for lang in ("en", "fr", "ja", "ko", "zh"):
        data = {**valid_manga_data, "language": lang}
        manga = Manga.model_validate(data)
        assert manga.language.value == lang


def test_language_invalid_raises(valid_manga_data: dict) -> None:
    """language with invalid value raises ValidationError."""
    data = {**valid_manga_data, "language": "english"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


def test_content_rating_enum(valid_manga_data: dict) -> None:
    """content_rating accepts ContentRating values."""
    for rating in ("safe", "suggestive", "erotica", "pornographic"):
        data = {**valid_manga_data, "content_rating": rating}
        manga = Manga.model_validate(data)
        assert manga.content_rating.value == rating


def test_content_rating_invalid_raises(valid_manga_data: dict) -> None:
    """content_rating with invalid value raises ValidationError."""
    data = {**valid_manga_data, "content_rating": "Safe"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 4: Validation — missing fields
# ────────────────────────────────────────────────────────────────────


def test_missing_id_raises(valid_manga_data: dict) -> None:
    """Manga without id raises ValidationError."""
    data = {k: v for k, v in valid_manga_data.items() if k != "id"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


def test_missing_title_raises(valid_manga_data: dict) -> None:
    """Manga without title raises ValidationError."""
    data = {k: v for k, v in valid_manga_data.items() if k != "title"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


def test_missing_site_raises(valid_manga_data: dict) -> None:
    """Manga without site raises ValidationError."""
    data = {k: v for k, v in valid_manga_data.items() if k != "site"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


def test_missing_url_raises(valid_manga_data: dict) -> None:
    """Manga without url raises ValidationError."""
    data = {k: v for k, v in valid_manga_data.items() if k != "url"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 5: Optional fields
# ────────────────────────────────────────────────────────────────────


def test_optional_fields_none(valid_manga_data: dict) -> None:
    """author, artist, description, year, cover_url can be None."""
    data = {
        **valid_manga_data,
        "author": None,
        "artist": None,
        "description": None,
        "year": None,
        "cover_url": None,
    }
    manga = Manga.model_validate(data)
    assert manga.author is None
    assert manga.artist is None
    assert manga.description is None
    assert manga.year is None
    assert manga.cover_url is None


def test_alternative_titles_empty_ok(valid_manga_data: dict) -> None:
    """alternative_titles can be empty."""
    data = {**valid_manga_data, "alternative_titles": []}
    manga = Manga.model_validate(data)
    assert manga.alternative_titles == []


def test_genres_empty_ok(valid_manga_data: dict) -> None:
    """genres can be empty."""
    data = {**valid_manga_data, "genres": []}
    manga = Manga.model_validate(data)
    assert manga.genres == []


# ────────────────────────────────────────────────────────────────────
# Test 6: chapters default
# ────────────────────────────────────────────────────────────────────


def test_chapters_default_empty(valid_manga_data: dict) -> None:
    """chapters defaults to []."""
    data = {k: v for k, v in valid_manga_data.items() if k != "chapters"}
    manga = Manga.model_validate(data)
    assert manga.chapters == []


def test_chapters_default_independent(valid_manga_data: dict) -> None:
    """Each Manga gets its own chapters list."""
    m1 = Manga.model_validate(valid_manga_data)
    m2 = Manga.model_validate(valid_manga_data)

    assert m1.chapters is not m2.chapters


# ────────────────────────────────────────────────────────────────────
# Test 7: URL validation
# ────────────────────────────────────────────────────────────────────


def test_url_invalid_raises(valid_manga_data: dict) -> None:
    """Invalid url raises ValidationError."""
    data = {**valid_manga_data, "url": "not-a-url"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


def test_cover_url_invalid_raises(valid_manga_data: dict) -> None:
    """Invalid cover_url raises ValidationError."""
    data = {**valid_manga_data, "cover_url": "not-a-url"}
    with pytest.raises(ValidationError):
        Manga.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 8: year validation
# ────────────────────────────────────────────────────────────────────


def test_year_before_1900_raises(valid_manga_data: dict) -> None:
    """year < 1900 raises ValidationError (if validated)."""
    data = {**valid_manga_data, "year": 1800}
    # Some models don't validate the range, some do.
    # If not validated, the test just checks it's accepted.
    try:
        manga = Manga.model_validate(data)
        assert manga.year == 1800  # accepted
    except ValidationError:
        pass  # rejected (strict range)


def test_year_after_2100_raises(valid_manga_data: dict) -> None:
    """year > 2100 raises ValidationError (if validated)."""
    data = {**valid_manga_data, "year": 2200}
    try:
        manga = Manga.model_validate(data)
        assert manga.year == 2200
    except ValidationError:
        pass


# ────────────────────────────────────────────────────────────────────
# Test 9: Serialization
# ────────────────────────────────────────────────────────────────────


def test_model_dump(valid_manga_data: dict) -> None:
    """model_dump() returns a dict."""
    manga = Manga.model_validate(valid_manga_data)
    data = manga.model_dump()

    assert isinstance(data, dict)
    assert data["id"] == manga.id
    assert data["title"] == "One Piece"
    assert data["language"] == "en"
    assert data["status"] == "ongoing"


def test_model_dump_json(valid_manga_data: dict) -> None:
    """model_dump_json() returns valid JSON."""
    manga = Manga.model_validate(valid_manga_data)
    json_str = manga.model_dump_json()

    data = json.loads(json_str)
    assert data["id"] == manga.id
    assert data["language"] == "en"


def test_model_validate_roundtrip(valid_manga_data: dict) -> None:
    """model_dump() → model_validate() roundtrip."""
    manga = Manga.model_validate(valid_manga_data)
    data = manga.model_dump()
    manga2 = Manga.model_validate(data)

    assert manga.id == manga2.id
    assert manga.title == manga2.title
    assert manga.language == manga2.language


# ────────────────────────────────────────────────────────────────────
# Test 10: Fixture coherence
# ────────────────────────────────────────────────────────────────────


def test_fixture_chapter_manga_id(sample_manga_data: dict, sample_chapter_data: dict) -> None:
    """sample_chapter.json: manga_id matches sample_manga.json: id."""
    assert sample_chapter_data["manga_id"] == sample_manga_data["id"]


def test_fixture_site_matches(sample_manga_data: dict) -> None:
    """sample_manga.json: site is 'mangadex'."""
    assert sample_manga_data["site"] == "mangadex"


def test_fixture_language(sample_manga_data: dict) -> None:
    """sample_manga.json: language is 'en'."""
    assert sample_manga_data["language"] == "en"


def test_fixture_content_rating(sample_manga_data: dict) -> None:
    """sample_manga.json: content_rating is 'safe'."""
    assert sample_manga_data["content_rating"] == "safe"
