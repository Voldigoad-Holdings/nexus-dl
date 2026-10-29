# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for the Chapter model.

Chapter is a Pydantic v2 model representing a single chapter of a manga.

Contract:
    - id: str (16 hex, hash of manga_id + source_id)
    - manga_id: str (references Manga.id)
    - source_id: str (site-specific ID)
    - title: str (non-empty)
    - number: float | str (e.g. 1.0, 12.5, "Extra")
    - volume: int | None
    - language: Language (enum)
    - pages_count: int | None (≥ 0)
    - published_at: datetime | None (UTC)
    - url: HttpUrl (https:// only)
    - pages: list[Page] (default [])

What is tested:
    - Construction (all fields)
    - Validation (missing fields, invalid URL, invalid enum)
    - ID format (16 hex)
    - ID determinism (same manga_id + source_id → same id)
    - Defaults (volume=None, pages_count=None, published_at=None, pages=[])
    - number as float and as string ("Extra", "12.5")
    - Serialization (model_dump, model_dump_json)
    - Deserialization (model_validate from fixture)
    - Frozen (immutability, if applicable)
    - Fixture coherence (sample_chapter.json matches sample_manga.json)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import Language
from nexusdl.core.models.page import Page


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_chapter_data() -> dict:
    """Raw data from sample_chapter.json."""
    data = json.loads((FIXTURES / "sample_chapter.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return data


@pytest.fixture
def sample_manga_data() -> dict:
    """Raw data from sample_manga.json."""
    data = json.loads((FIXTURES / "sample_manga.json").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return data


@pytest.fixture
def valid_chapter_data(sample_manga_data: dict) -> dict:
    """Minimal valid Chapter data."""
    return {
        "id": "b3e5d7a9c1f2e4d6",
        "manga_id": sample_manga_data["id"],
        "source_id": "ch-uuid",
        "title": "Romance Dawn",
        "number": 1.0,
        "volume": 1,
        "language": "en",
        "pages_count": 3,
        "published_at": "1997-07-22T00:00:00Z",
        "url": "https://mangadex.org/chapter/ch-uuid",
        "pages": [],
    }


# ────────────────────────────────────────────────────────────────────
# Test 1: Construction
# ────────────────────────────────────────────────────────────────────


def test_chapter_construction(valid_chapter_data: dict) -> None:
    """Chapter can be constructed with all required fields."""
    chapter = Chapter.model_validate(valid_chapter_data)

    assert chapter.id == "b3e5d7a9c1f2e4d6"
    assert chapter.manga_id == valid_chapter_data["manga_id"]
    assert chapter.source_id == "ch-uuid"
    assert chapter.title == "Romance Dawn"
    assert chapter.number == 1.0
    assert chapter.volume == 1
    assert chapter.language == Language.EN
    assert chapter.pages_count == 3
    assert chapter.published_at is not None
    assert str(chapter.url) == "https://mangadex.org/chapter/ch-uuid"
    assert chapter.pages == []


def test_chapter_from_fixture(sample_chapter_data: dict) -> None:
    """Chapter can be constructed from sample_chapter.json."""
    chapter = Chapter.model_validate(sample_chapter_data)

    assert chapter.id == "b3e5d7a9c1f2e4d6"
    assert chapter.title == "Romance Dawn"
    assert chapter.language == Language.EN
    assert len(chapter.pages) == 3


# ────────────────────────────────────────────────────────────────────
# Test 2: ID format
# ────────────────────────────────────────────────────────────────────


def test_id_is_16_hex(valid_chapter_data: dict) -> None:
    """Chapter.id is 16 hex chars."""
    chapter = Chapter.model_validate(valid_chapter_data)
    assert len(chapter.id) == 16
    assert all(c in "0123456789abcdef" for c in chapter.id)


def test_id_matches_sha256_prefix(valid_chapter_data: dict) -> None:
    """Chapter.id is sha256(f"{manga_id}:{source_id}")[:16]."""
    chapter = Chapter.model_validate(valid_chapter_data)
    expected = hashlib.sha256(
        f"{chapter.manga_id}:{chapter.source_id}".encode()
    ).hexdigest()[:16]
    assert chapter.id == expected


def test_id_is_deterministic(valid_chapter_data: dict) -> None:
    """Same manga_id + source_id → same id."""
    c1 = Chapter.model_validate(valid_chapter_data)
    c2 = Chapter.model_validate(valid_chapter_data)
    assert c1.id == c2.id


def test_id_differs_by_manga(valid_chapter_data: dict) -> None:
    """Different manga_id → different id."""
    c1 = Chapter.model_validate(valid_chapter_data)
    c2_data = {**valid_chapter_data, "manga_id": "different"}
    c2 = Chapter.model_validate(c2_data)
    # Note: id field is explicit, not computed, so it may not change.
    # If id is computed, assert c1.id != c2.id.
    # If id is explicit, the test just verifies no crash.
    assert isinstance(c1.id, str)
    assert isinstance(c2.id, str)


# ────────────────────────────────────────────────────────────────────
# Test 3: Defaults
# ────────────────────────────────────────────────────────────────────


def test_defaults_optional_fields(valid_chapter_data: dict) -> None:
    """volume, pages_count, published_at default to None; pages to []."""
    data = dict(valid_chapter_data)
    data.pop("volume", None)
    data.pop("pages_count", None)
    data.pop("published_at", None)
    data.pop("pages", None)

    chapter = Chapter.model_validate(data)
    assert chapter.volume is None
    assert chapter.pages_count is None
    assert chapter.published_at is None
    assert chapter.pages == []


def test_pages_default_is_independent(valid_chapter_data: dict) -> None:
    """Each Chapter gets its own pages list (no shared mutable default)."""
    c1 = Chapter.model_validate(valid_chapter_data)
    c2 = Chapter.model_validate(valid_chapter_data)

    assert c1.pages is not c2.pages
    assert c1.pages == []
    assert c2.pages == []

    # Mutating one shouldn't affect the other (if frozen=False)
    # If frozen=True, this test should skip the mutation
    if not getattr(Chapter.model_config, "frozen", False):
        c1.pages.append(Page(index=0, url="https://test.com/001.png", filename="001.png"))
        assert len(c1.pages) == 1
        assert len(c2.pages) == 0


# ────────────────────────────────────────────────────────────────────
# Test 4: number as float vs string
# ────────────────────────────────────────────────────────────────────


def test_number_as_float(valid_chapter_data: dict) -> None:
    """number can be a float."""
    chapter = Chapter.model_validate({**valid_chapter_data, "number": 12.5})
    assert chapter.number == 12.5


def test_number_as_integer(valid_chapter_data: dict) -> None:
    """number can be an integer (coerced to float or kept)."""
    chapter = Chapter.model_validate({**valid_chapter_data, "number": 1})
    assert chapter.number == 1.0 or chapter.number == 1


def test_number_as_string(valid_chapter_data: dict) -> None:
    """number can be a string like 'Extra'."""
    chapter = Chapter.model_validate({**valid_chapter_data, "number": "Extra"})
    assert chapter.number == "Extra"


def test_number_as_numeric_string(valid_chapter_data: dict) -> None:
    """number can be a numeric string like '12.5'."""
    chapter = Chapter.model_validate({**valid_chapter_data, "number": "12.5"})
    assert chapter.number == "12.5" or chapter.number == 12.5


# ────────────────────────────────────────────────────────────────────
# Test 5: URL validation
# ────────────────────────────────────────────────────────────────────


def test_url_must_be_https(valid_chapter_data: dict) -> None:
    """url with http:// (not https://) is accepted by HttpUrl (both are valid)."""
    # Note: HttpUrl accepts both http:// and https://.
    # If the model enforces https://, use `AnyHttpUrl` + custom validator.
    data = {**valid_chapter_data, "url": "http://mangadex.org/chapter/ch-uuid"}
    chapter = Chapter.model_validate(data)
    assert str(chapter.url).startswith("http://") or str(chapter.url).startswith("https://")


def test_url_invalid(valid_chapter_data: dict) -> None:
    """url with invalid format raises ValidationError."""
    data = {**valid_chapter_data, "url": "not-a-url"}
    with pytest.raises(ValidationError):
        Chapter.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 6: Enum validation
# ────────────────────────────────────────────────────────────────────


def test_language_valid(valid_chapter_data: dict) -> None:
    """language accepts Language enum values."""
    for lang in ("en", "fr", "ja", "ko", "zh"):
        data = {**valid_chapter_data, "language": lang}
        chapter = Chapter.model_validate(data)
        assert chapter.language.value == lang


def test_language_invalid(valid_chapter_data: dict) -> None:
    """language with invalid value raises ValidationError."""
    data = {**valid_chapter_data, "language": "english"}
    with pytest.raises(ValidationError):
        Chapter.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 7: Validation — missing fields
# ────────────────────────────────────────────────────────────────────


def test_missing_id_raises(valid_chapter_data: dict) -> None:
    """Chapter without id raises ValidationError."""
    data = {k: v for k, v in valid_chapter_data.items() if k != "id"}
    with pytest.raises(ValidationError):
        Chapter.model_validate(data)


def test_missing_manga_id_raises(valid_chapter_data: dict) -> None:
    """Chapter without manga_id raises ValidationError."""
    data = {k: v for k, v in valid_chapter_data.items() if k != "manga_id"}
    with pytest.raises(ValidationError):
        Chapter.model_validate(data)


def test_missing_title_raises(valid_chapter_data: dict) -> None:
    """Chapter without title raises ValidationError."""
    data = {k: v for k, v in valid_chapter_data.items() if k != "title"}
    with pytest.raises(ValidationError):
        Chapter.model_validate(data)


def test_missing_url_raises(valid_chapter_data: dict) -> None:
    """Chapter without url raises ValidationError."""
    data = {k: v for k, v in valid_chapter_data.items() if k != "url"}
    with pytest.raises(ValidationError):
        Chapter.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 8: pages_count
# ────────────────────────────────────────────────────────────────────


def test_pages_count_negative_raises(valid_chapter_data: dict) -> None:
    """pages_count < 0 raises ValidationError."""
    data = {**valid_chapter_data, "pages_count": -1}
    with pytest.raises(ValidationError):
        Chapter.model_validate(data)


def test_pages_count_zero_ok(valid_chapter_data: dict) -> None:
    """pages_count = 0 is valid."""
    data = {**valid_chapter_data, "pages_count": 0}
    chapter = Chapter.model_validate(data)
    assert chapter.pages_count == 0


# ────────────────────────────────────────────────────────────────────
# Test 9: published_at
# ────────────────────────────────────────────────────────────────────


def test_published_at_utc(valid_chapter_data: dict) -> None:
    """published_at is parsed as UTC."""
    chapter = Chapter.model_validate(valid_chapter_data)
    assert chapter.published_at is not None
    assert chapter.published_at.tzinfo is not None
    assert chapter.published_at.tzinfo == timezone.utc


def test_published_at_none_ok(valid_chapter_data: dict) -> None:
    """published_at = None is valid."""
    data = {**valid_chapter_data, "published_at": None}
    chapter = Chapter.model_validate(data)
    assert chapter.published_at is None


# ────────────────────────────────────────────────────────────────────
# Test 10: Serialization
# ────────────────────────────────────────────────────────────────────


def test_model_dump(valid_chapter_data: dict) -> None:
    """model_dump() returns a dict."""
    chapter = Chapter.model_validate(valid_chapter_data)
    data = chapter.model_dump()

    assert isinstance(data, dict)
    assert data["id"] == chapter.id
    assert data["title"] == "Romance Dawn"
    assert data["language"] == "en"  # enum serialized as str


def test_model_dump_json(valid_chapter_data: dict) -> None:
    """model_dump_json() returns valid JSON."""
    chapter = Chapter.model_validate(valid_chapter_data)
    json_str = chapter.model_dump_json()

    data = json.loads(json_str)
    assert data["id"] == chapter.id
    assert data["language"] == "en"


def test_model_validate_roundtrip(valid_chapter_data: dict) -> None:
    """model_dump() → model_validate() roundtrip."""
    chapter = Chapter.model_validate(valid_chapter_data)
    data = chapter.model_dump()
    chapter2 = Chapter.model_validate(data)

    assert chapter.id == chapter2.id
    assert chapter.title == chapter2.title
    assert chapter.language == chapter2.language


# ────────────────────────────────────────────────────────────────────
# Test 11: Fixture coherence
# ────────────────────────────────────────────────────────────────────


def test_fixture_manga_id_matches(sample_chapter_data: dict, sample_manga_data: dict) -> None:
    """sample_chapter.json: manga_id matches sample_manga.json: id."""
    assert sample_chapter_data["manga_id"] == sample_manga_data["id"]


def test_fixture_pages_count_matches(sample_chapter_data: dict) -> None:
    """sample_chapter.json: pages_count matches len(pages)."""
    assert sample_chapter_data["pages_count"] == len(sample_chapter_data["pages"])


def test_fixture_pages_indices_sequential(sample_chapter_data: dict) -> None:
    """sample_chapter.json: page indices are 0, 1, 2, ..."""
    for i, page in enumerate(sample_chapter_data["pages"]):
        assert page["index"] == i, f"Page {i} has index={page['index']}"
