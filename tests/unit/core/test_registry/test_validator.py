# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for SiteValidator.

SiteValidator validates ``sites.yaml`` against:
    - JSON Schema (``sites_schema.json``)
    - Business rules (ADR-002, ADR-004):
        * No CBR format
        * No hardcoded secrets (``password``, ``token``, ``api_key``)
        * No production URLs (``nexusdl.dev``, ``api.nexusdl.*``)
        * Domains must be HTTPS
        * No duplicate site IDs
        * ``parser_class`` must be in ``module:Class`` format
        * ``cookies_required`` must be names only (no values)
    - Cross-checks (``--check-parsers``):
        * ``parser_class`` importable
        * ``parser.site_id`` matches ``site.id``
        * ``parser.language`` matches ``site.language``
        * ``parser.adult`` matches ``site.adult``

What is tested:
    - Valid sites.yaml (no errors)
    - Invalid: missing required fields
    - Invalid: wrong types (adult as string)
    - Invalid: bad parser_class format
    - Invalid: empty domains
    - Invalid: http domains
    - Invalid: duplicate IDs
    - Invalid: hardcoded secrets (ADR-004)
    - Invalid: production URLs (ADR-004)
    - Invalid: CBR format (ADR-002)
    - Invalid: cookies_required with values
    - _comment fields ignored
    - Aliases coherence
    - Categories coherence
    - --strict (warnings → errors)
    - --check-parsers (import + attribute cross-check)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from nexusdl.core.exceptions import ConfigError
from nexusdl.core.models.site import SiteCapabilities
from nexusdl.core.registry.validator import SiteValidator, ValidationReport


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _valid_site(site_id: str = "mangadex") -> dict:
    """Return a minimal valid site dict."""
    return {
        "id": site_id,
        "name": site_id.capitalize(),
        "domains": [f"https://{site_id}.test"],
        "parser_class": f"nexusdl.parsers.en.{site_id}:{site_id.capitalize()}Parser",
        "language": "en",
        "adult": False,
        "capabilities": SiteCapabilities().model_dump(),
        "default_headers": {},
        "cookies_required": [],
    }


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    """Write a sites.yaml and return its path."""
    path = tmp_path / "sites.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _write_schema(tmp_path: Path, schema: dict) -> Path:
    """Write a sites_schema.json and return its path."""
    path = tmp_path / "sites_schema.json"
    path.write_text(json.dumps(schema))
    return path


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def minimal_schema() -> dict:
    """A minimal JSON Schema for testing."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["sites"],
        "properties": {
            "sites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "name", "domains", "parser_class", "language", "adult", "capabilities"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "name": {"type": "string", "minLength": 1},
                        "domains": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "parser_class": {"type": "string", "pattern": "^[a-zA-Z0-9_.]+:[A-Z][a-zA-Z0-9_]+$"},
                        "language": {"type": "string", "enum": ["en", "fr", "ja", "ko", "zh", "adult", "unknown"]},
                        "adult": {"type": "boolean"},
                        "capabilities": {"type": "object"},
                        "default_headers": {"type": "object"},
                        "cookies_required": {"type": "array", "items": {"type": "string"}},
                        "_comment": {"type": "string"},
                    },
                },
            },
            "aliases": {"type": "object", "additionalProperties": {"type": "string"}},
            "categories": {"type": "object"},
            "disabled_sites": {"type": "array", "items": {"type": "string"}},
        },
    }


@pytest.fixture
def validator(tmp_path: Path, minimal_schema: dict) -> SiteValidator:
    """SiteValidator with a temp schema."""
    schema_path = _write_schema(tmp_path, minimal_schema)
    return SiteValidator(schema_path=schema_path)


# ────────────────────────────────────────────────────────────────────
# Test 1: Valid sites.yaml
# ────────────────────────────────────────────────────────────────────


def test_valid_sites_yaml(validator: SiteValidator, tmp_path: Path) -> None:
    """A valid sites.yaml produces no errors."""
    data = {"sites": [_valid_site("mangadex"), _valid_site("sushiscan")]}
    path = _write_yaml(tmp_path, data)

    report = validator.validate(path)

    assert isinstance(report, ValidationReport)
    assert report.is_valid is True
    assert len(report.errors) == 0
    assert report.site_count == 2


def test_valid_with_aliases(validator: SiteValidator, tmp_path: Path) -> None:
    """Aliases referencing existing sites are valid."""
    data = {
        "sites": [_valid_site("mangadex")],
        "aliases": {"md": "mangadex"},
    }
    path = _write_yaml(tmp_path, data)

    report = validator.validate(path)
    assert report.is_valid is True


def test_valid_with_categories(validator: SiteValidator, tmp_path: Path) -> None:
    """Categories matching language values are valid."""
    data = {
        "sites": [_valid_site("mangadex")],
        "categories": {"en": {"default_headers": {"Accept-Language": "en"}}},
    }
    path = _write_yaml(tmp_path, data)

    report = validator.validate(path)
    assert report.is_valid is True


# ────────────────────────────────────────────────────────────────────
# Test 2: JSON Schema violations
# ────────────────────────────────────────────────────────────────────


def test_missing_required_field(validator: SiteValidator, tmp_path: Path) -> None:
    """A site missing 'title' raises ConfigError."""
    site = _valid_site("mangadex")
    del site["name"]
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="name"):
        validator.validate(path)


def test_wrong_type_adult(validator: SiteValidator, tmp_path: Path) -> None:
    """adult='false' (string) raises ConfigError."""
    site = _valid_site("mangadex")
    site["adult"] = "false"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError):
        validator.validate(path)


def test_wrong_type_language(validator: SiteValidator, tmp_path: Path) -> None:
    """language='english' raises ConfigError."""
    site = _valid_site("mangadex")
    site["language"] = "english"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError):
        validator.validate(path)


def test_invalid_parser_class_format(validator: SiteValidator, tmp_path: Path) -> None:
    """parser_class='bad' raises ConfigError."""
    site = _valid_site("mangadex")
    site["parser_class"] = "bad"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError):
        validator.validate(path)


# ────────────────────────────────────────────────────────────────────
# Test 3: Business rules — domains
# ────────────────────────────────────────────────────────────────────


def test_empty_domains(validator: SiteValidator, tmp_path: Path) -> None:
    """Empty domains raises ConfigError."""
    site = _valid_site("mangadex")
    site["domains"] = []
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError):
        validator.validate(path)


def test_http_domains(validator: SiteValidator, tmp_path: Path) -> None:
    """http:// domains raise ConfigError (ADR-004)."""
    site = _valid_site("mangadex")
    site["domains"] = ["http://mangadex.test"]
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="https"):
        validator.validate(path)


# ────────────────────────────────────────────────────────────────────
# Test 4: Business rules — duplicate IDs
# ────────────────────────────────────────────────────────────────────


def test_duplicate_ids(validator: SiteValidator, tmp_path: Path) -> None:
    """Duplicate site IDs raise ConfigError."""
    data = {
        "sites": [_valid_site("mangadex"), _valid_site("mangadex")],
    }
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="duplicate"):
        validator.validate(path)


# ────────────────────────────────────────────────────────────────────
# Test 5: Business rules — secrets (ADR-004)
# ────────────────────────────────────────────────────────────────────


def test_hardcoded_password(validator: SiteValidator, tmp_path: Path) -> None:
    """password field raises ConfigError (ADR-004)."""
    site = _valid_site("mangadex")
    site["password"] = "secret"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="password|secret"):
        validator.validate(path)


def test_hardcoded_token(validator: SiteValidator, tmp_path: Path) -> None:
    """token field raises ConfigError (ADR-004)."""
    site = _valid_site("mangadex")
    site["token"] = "abc"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="token|secret"):
        validator.validate(path)


def test_hardcoded_api_key(validator: SiteValidator, tmp_path: Path) -> None:
    """api_key field raises ConfigError (ADR-004)."""
    site = _valid_site("mangadex")
    site["api_key"] = "abc"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="api_key|secret"):
        validator.validate(path)


def test_cookies_required_with_values(validator: SiteValidator, tmp_path: Path) -> None:
    """cookies_required with 'name=value' raises ConfigError."""
    site = _valid_site("mangadex")
    site["cookies_required"] = ["cf_clearance=abc123"]
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="cookie|value"):
        validator.validate(path)


# ────────────────────────────────────────────────────────────────────
# Test 6: Business rules — production URLs (ADR-004)
# ────────────────────────────────────────────────────────────────────


def test_production_url(validator: SiteValidator, tmp_path: Path) -> None:
    """nexusdl.dev URL raises ConfigError (ADR-004)."""
    site = _valid_site("mangadex")
    site["domains"] = ["https://nexusdl.dev"]
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="nexusdl|production"):
        validator.validate(path)


def test_production_api_url(validator: SiteValidator, tmp_path: Path) -> None:
    """api.nexusdl.dev URL raises ConfigError (ADR-004)."""
    site = _valid_site("mangadex")
    site["domains"] = ["https://api.nexusdl.dev"]
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError):
        validator.validate(path)


# ────────────────────────────────────────────────────────────────────
# Test 7: Business rules — CBR (ADR-002)
# ────────────────────────────────────────────────────────────────────


def test_cbr_format_rejected(validator: SiteValidator, tmp_path: Path) -> None:
    """CBR format raises ConfigError (ADR-002)."""
    site = _valid_site("mangadex")
    # If the schema allows a 'formats' field
    site["formats"] = ["CBR"]
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    # Either the schema rejects 'formats' (extra field) or the
    # business rule rejects 'CBR'. Both are valid.
    with pytest.raises(ConfigError):
        validator.validate(path)


# ────────────────────────────────────────────────────────────────────
# Test 8: _comment ignored
# ────────────────────────────────────────────────────────────────────


def test_comment_ignored(validator: SiteValidator, tmp_path: Path) -> None:
    """_comment fields are ignored (no error)."""
    site = _valid_site("mangadex")
    site["_comment"] = "This is a comment"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    report = validator.validate(path)
    assert report.is_valid is True


# ────────────────────────────────────────────────────────────────────
# Test 9: Aliases coherence
# ────────────────────────────────────────────────────────────────────


def test_alias_referencing_unknown_site(validator: SiteValidator, tmp_path: Path) -> None:
    """Alias referencing an unknown site raises ConfigError."""
    data = {
        "sites": [_valid_site("mangadex")],
        "aliases": {"md": "unknown_site"},
    }
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="alias|unknown"):
        validator.validate(path)


# ────────────────────────────────────────────────────────────────────
# Test 10: --strict (warnings → errors)
# ────────────────────────────────────────────────────────────────────


def test_strict_mode(validator: SiteValidator, tmp_path: Path) -> None:
    """In strict mode, warnings become errors."""
    # adult=True but language='en' (not 'adult') → warning
    site = _valid_site("nhentai")
    site["adult"] = True
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    # Non-strict: valid (warning only)
    report = validator.validate(path, strict=False)
    assert report.is_valid is True
    assert len(report.warnings) >= 1

    # Strict: invalid (warning becomes error)
    with pytest.raises(ConfigError):
        validator.validate(path, strict=True)


# ────────────────────────────────────────────────────────────────────
# Test 11: --check-parsers (import + cross-check)
# ────────────────────────────────────────────────────────────────────


def test_check_parsers_import_error(validator: SiteValidator, tmp_path: Path) -> None:
    """--check-parsers raises if parser_class can't be imported."""
    site = _valid_site("nonexistent")
    site["parser_class"] = "nonexistent.module:NonexistentParser"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    with pytest.raises(ConfigError, match="import|not found"):
        validator.validate(path, check_parsers=True)


def test_check_parsers_site_id_mismatch(validator: SiteValidator, tmp_path: Path) -> None:
    """--check-parsers raises if parser.site_id != site.id."""
    # Use a known module (the test's own FakeParser)
    site = _valid_site("mangadex")
    site["parser_class"] = "tests.unit.core.test_registry.test_validator:FakeParser"
    data = {"sites": [site]}
    path = _write_yaml(tmp_path, data)

    # FakeParser.site_id = "fake" ≠ "mangadex"
    with pytest.raises(ConfigError, match="site_id"):
        validator.validate(path, check_parsers=True)


# ────────────────────────────────────────────────────────────────────
# Test 12: Report structure
# ────────────────────────────────────────────────────────────────────


def test_report_has_structure(validator: SiteValidator, tmp_path: Path) -> None:
    """ValidationReport exposes errors, warnings, site_count."""
    data = {"sites": [_valid_site("mangadex")]}
    path = _write_yaml(tmp_path, data)

    report = validator.validate(path)
    assert hasattr(report, "errors")
    assert hasattr(report, "warnings")
    assert hasattr(report, "site_count")
    assert hasattr(report, "is_valid")
    assert isinstance(report.errors, list)
    assert isinstance(report.warnings, list)
    assert isinstance(report.site_count, int)


# ────────────────────────────────────────────────────────────────────
# Fake parser (for --check-parsers test)
# ────────────────────────────────────────────────────────────────────


class FakeParser:
    """Fake parser with a different site_id."""

    site_id = "fake"
    language = "en"
    adult = False
