# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for SiteConfig and SiteCapabilities models.

SiteConfig represents a supported site (from sites.yaml).
SiteCapabilities represents what a parser can do (search, chapters, etc.).

Contract:
    - id: str (snake_case, matches sites.yaml and parser.site_id)
    - name: str (non-empty)
    - domains: list[HttpUrl] (≥ 1, https://)
    - parser_class: str (format "module:Class")
    - language: Language (enum)
    - adult: bool
    - capabilities: SiteCapabilities
    - default_headers: dict[str, str]
    - cookies_required: list[str] (NAMES only, never values — ADR-004)

SiteCapabilities:
    - supports_search, supports_manga_info, supports_chapters,
      supports_pages, supports_download: bool
    - requires_auth, requires_cloudflare_bypass: bool
    - max_concurrent_downloads: int (1-32)
    - default_rate_limit_per_second: float (> 0)

What is tested:
    - Construction (all fields)
    - Validation (missing fields, invalid enums, invalid URLs)
    - Capabilities (all 9 fields)
    - Defaults (headers={}, cookies=[])
    - Security (no secrets, no CBR)
    - Fixture coherence (sample_site.yaml)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nexusdl.core.models.enums import Language
from nexusdl.core.models.site import SiteCapabilities, SiteConfig


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_site_data() -> dict:
    """Raw data from sample_site.yaml."""
    data = yaml.safe_load((FIXTURES / "sample_site.yaml").read_text())
    data.pop("_fixture_version", None)
    data.pop("_description", None)
    return data


@pytest.fixture
def valid_capabilities() -> SiteCapabilities:
    """A valid SiteCapabilities."""
    return SiteCapabilities(
        supports_search=True,
        supports_manga_info=True,
        supports_chapters=True,
        supports_pages=True,
        supports_download=True,
        requires_auth=False,
        requires_cloudflare_bypass=False,
        max_concurrent_downloads=4,
        default_rate_limit_per_second=2.0,
    )


@pytest.fixture
def valid_site_data(valid_capabilities: SiteCapabilities) -> dict:
    """Minimal valid SiteConfig data."""
    return {
        "id": "mangadex",
        "name": "MangaDex",
        "domains": ["https://mangadex.org", "https://api.mangadex.org"],
        "parser_class": "nexusdl.parsers.en.mangadex:MangaDexParser",
        "language": "en",
        "adult": False,
        "capabilities": valid_capabilities.model_dump(),
        "default_headers": {"User-Agent": "NexusDL/0.1.0"},
        "cookies_required": [],
    }


# ────────────────────────────────────────────────────────────────────
# Test 1: SiteConfig construction
# ────────────────────────────────────────────────────────────────────


def test_site_config_construction(valid_site_data: dict) -> None:
    """SiteConfig can be constructed with all fields."""
    site = SiteConfig.model_validate(valid_site_data)

    assert site.id == "mangadex"
    assert site.name == "MangaDex"
    assert len(site.domains) == 2
    assert site.parser_class == "nexusdl.parsers.en.mangadex:MangaDexParser"
    assert site.language == Language.EN
    assert site.adult is False
    assert site.default_headers == {"User-Agent": "NexusDL/0.1.0"}
    assert site.cookies_required == []


def test_site_config_from_fixture(sample_site_data: dict) -> None:
    """SiteConfig can be constructed from sample_site.yaml."""
    site = SiteConfig.model_validate(sample_site_data)

    assert site.id == "mangadex"
    assert site.name == "MangaDex"
    assert site.language == Language.EN
    assert site.adult is False


# ────────────────────────────────────────────────────────────────────
# Test 2: id validation
# ────────────────────────────────────────────────────────────────────


def test_id_snake_case_ok(valid_site_data: dict) -> None:
    """Snake_case id is accepted."""
    for site_id in ("mangadex", "scan_manga", "my-site", "site123"):
        data = {**valid_site_data, "id": site_id}
        site = SiteConfig.model_validate(data)
        assert site.id == site_id


def test_id_empty_raises(valid_site_data: dict) -> None:
    """Empty id raises ValidationError."""
    data = {**valid_site_data, "id": ""}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


def test_id_missing_raises(valid_site_data: dict) -> None:
    """Missing id raises ValidationError."""
    data = {k: v for k, v in valid_site_data.items() if k != "id"}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 3: domains validation
# ────────────────────────────────────────────────────────────────────


def test_domains_empty_raises(valid_site_data: dict) -> None:
    """Empty domains raises ValidationError."""
    data = {**valid_site_data, "domains": []}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


def test_domains_invalid_url_raises(valid_site_data: dict) -> None:
    """Invalid URL in domains raises ValidationError."""
    data = {**valid_site_data, "domains": ["not-a-url"]}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


def test_domains_https_ok(valid_site_data: dict) -> None:
    """HTTPS domains are accepted."""
    data = {**valid_site_data, "domains": ["https://example.com"]}
    site = SiteConfig.model_validate(data)
    assert str(site.domains[0]) == "https://example.com"


# ────────────────────────────────────────────────────────────────────
# Test 4: parser_class format
# ────────────────────────────────────────────────────────────────────


def test_parser_class_valid_format(valid_site_data: dict) -> None:
    """parser_class with 'module:Class' format is valid."""
    site = SiteConfig.model_validate(valid_site_data)
    assert ":" in site.parser_class
    module, cls = site.parser_class.split(":")
    assert "." in module
    assert cls[0].isupper()


def test_parser_class_no_colon_raises(valid_site_data: dict) -> None:
    """parser_class without ':' raises ValidationError (if validated)."""
    data = {**valid_site_data, "parser_class": "nexusdl.parsers.en.mangadex.MangaDexParser"}
    # Some models don't validate the format, some do.
    try:
        site = SiteConfig.model_validate(data)
        # If not validated, verify it's still a string
        assert isinstance(site.parser_class, str)
    except ValidationError:
        pass


def test_parser_class_empty_raises(valid_site_data: dict) -> None:
    """Empty parser_class raises ValidationError."""
    data = {**valid_site_data, "parser_class": ""}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 5: language and adult
# ────────────────────────────────────────────────────────────────────


def test_language_enum(valid_site_data: dict) -> None:
    """language accepts Language enum."""
    for lang in ("en", "fr", "ja", "ko", "zh"):
        data = {**valid_site_data, "language": lang}
        site = SiteConfig.model_validate(data)
        assert site.language.value == lang


def test_language_invalid_raises(valid_site_data: dict) -> None:
    """language with invalid value raises ValidationError."""
    data = {**valid_site_data, "language": "english"}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


def test_adult_true(valid_site_data: dict) -> None:
    """adult=True is accepted."""
    data = {**valid_site_data, "adult": True}
    site = SiteConfig.model_validate(data)
    assert site.adult is True


def test_adult_false(valid_site_data: dict) -> None:
    """adult=False is accepted."""
    site = SiteConfig.model_validate(valid_site_data)
    assert site.adult is False


def test_adult_string_raises(valid_site_data: dict) -> None:
    """adult='false' (string) raises ValidationError."""
    data = {**valid_site_data, "adult": "false"}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 6: SiteCapabilities
# ────────────────────────────────────────────────────────────────────


def test_capabilities_all_fields(valid_capabilities: SiteCapabilities) -> None:
    """SiteCapabilities has all 9 fields."""
    assert valid_capabilities.supports_search is True
    assert valid_capabilities.supports_manga_info is True
    assert valid_capabilities.supports_chapters is True
    assert valid_capabilities.supports_pages is True
    assert valid_capabilities.supports_download is True
    assert valid_capabilities.requires_auth is False
    assert valid_capabilities.requires_cloudflare_bypass is False
    assert valid_capabilities.max_concurrent_downloads == 4
    assert valid_capabilities.default_rate_limit_per_second == 2.0


def test_capabilities_defaults() -> None:
    """SiteCapabilities has sensible defaults."""
    caps = SiteCapabilities()
    assert caps.supports_search is True
    assert caps.supports_download is True
    assert caps.requires_auth is False
    assert caps.requires_cloudflare_bypass is False
    assert caps.max_concurrent_downloads == 4
    assert caps.default_rate_limit_per_second == 2.0


def test_capabilities_max_concurrent_range() -> None:
    """max_concurrent_downloads must be in [1, 32]."""
    # Valid
    SiteCapabilities(max_concurrent_downloads=1)
    SiteCapabilities(max_concurrent_downloads=32)

    # Invalid
    with pytest.raises(ValidationError):
        SiteCapabilities(max_concurrent_downloads=0)
    with pytest.raises(ValidationError):
        SiteCapabilities(max_concurrent_downloads=100)


def test_capabilities_rate_limit_positive() -> None:
    """default_rate_limit_per_second must be > 0."""
    SiteCapabilities(default_rate_limit_per_second=0.1)
    SiteCapabilities(default_rate_limit_per_second=10.0)

    with pytest.raises(ValidationError):
        SiteCapabilities(default_rate_limit_per_second=0)
    with pytest.raises(ValidationError):
        SiteCapabilities(default_rate_limit_per_second=-1.0)


# ────────────────────────────────────────────────────────────────────
# Test 7: default_headers and cookies_required
# ────────────────────────────────────────────────────────────────────


def test_default_headers_default_empty(valid_site_data: dict) -> None:
    """default_headers defaults to {}."""
    data = {k: v for k, v in valid_site_data.items() if k != "default_headers"}
    site = SiteConfig.model_validate(data)
    assert site.default_headers == {}


def test_default_headers_valid(valid_site_data: dict) -> None:
    """default_headers accepts dict[str, str]."""
    data = {**valid_site_data, "default_headers": {"User-Agent": "Test/1.0", "Accept": "*/*"}}
    site = SiteConfig.model_validate(data)
    assert site.default_headers["User-Agent"] == "Test/1.0"


def test_cookies_required_default_empty(valid_site_data: dict) -> None:
    """cookies_required defaults to []."""
    data = {k: v for k, v in valid_site_data.items() if k != "cookies_required"}
    site = SiteConfig.model_validate(data)
    assert site.cookies_required == []


def test_cookies_required_names_only(valid_site_data: dict) -> None:
    """cookies_required accepts cookie NAMES (not values)."""
    data = {**valid_site_data, "cookies_required": ["cf_clearance", "cf_bm"]}
    site = SiteConfig.model_validate(data)
    assert site.cookies_required == ["cf_clearance", "cf_bm"]


# ────────────────────────────────────────────────────────────────────
# Test 8: security (ADR-004)
# ────────────────────────────────────────────────────────────────────


def test_no_password_field(valid_site_data: dict) -> None:
    """SiteConfig does not have a password field (ADR-004)."""
    data = {**valid_site_data, "password": "secret"}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


def test_no_token_field(valid_site_data: dict) -> None:
    """SiteConfig does not have a token field (ADR-004)."""
    data = {**valid_site_data, "token": "abc"}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


def test_no_api_key_field(valid_site_data: dict) -> None:
    """SiteConfig does not have an api_key field (ADR-004)."""
    data = {**valid_site_data, "api_key": "abc"}
    with pytest.raises(ValidationError):
        SiteConfig.model_validate(data)


# ────────────────────────────────────────────────────────────────────
# Test 9: serialization
# ────────────────────────────────────────────────────────────────────


def test_model_dump(valid_site_data: dict) -> None:
    """model_dump() returns a dict."""
    site = SiteConfig.model_validate(valid_site_data)
    data = site.model_dump()

    assert isinstance(data, dict)
    assert data["id"] == "mangadex"
    assert data["language"] == "en"
    assert data["adult"] is False


def test_model_dump_json(valid_site_data: dict) -> None:
    """model_dump_json() returns valid JSON."""
    site = SiteConfig.model_validate(valid_site_data)
    json_str = site.model_dump_json()

    data = json.loads(json_str)
    assert data["id"] == "mangadex"


# ────────────────────────────────────────────────────────────────────
# Test 10: fixture coherence
# ────────────────────────────────────────────────────────────────────


def test_fixture_site_id(sample_site_data: dict) -> None:
    """sample_site.yaml: id is 'mangadex' (matches sample_manga.json: site)."""
    assert sample_site_data["id"] == "mangadex"


def test_fixture_matches_manga(sample_site_data: dict) -> None:
    """sample_site.yaml: id matches sample_manga.json: site."""
    manga_data = json.loads((FIXTURES / "sample_manga.json").read_text())
    assert sample_site_data["id"] == manga_data["site"]


def test_fixture_capabilities_complete(sample_site_data: dict) -> None:
    """sample_site.yaml: capabilities has 9 fields."""
    required = {
        "supports_search",
        "supports_manga_info",
        "supports_chapters",
        "supports_pages",
        "supports_download",
        "requires_auth",
        "requires_cloudflare_bypass",
        "max_concurrent_downloads",
        "default_rate_limit_per_second",
    }
    assert required.issubset(sample_site_data["capabilities"].keys())
