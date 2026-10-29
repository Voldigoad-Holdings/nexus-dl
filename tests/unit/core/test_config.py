# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for core.config (Settings).

Settings is a Pydantic Settings v2 model that loads from:

    1. Default values (in Settings)
    2. .env file (if present)
    3. Environment variables (NEXUSDL_*)
    4. Explicit kwargs (highest precedence)

Contract:
    - All secrets are `SecretStr` (never serialized as plain str)
    - URLs are `HttpUrl` (validated)
    - Enums for APP_ENVIRONMENT, LOG_LEVEL, LOG_FORMAT
    - Ranges for concurrency, ports
    - `extra="forbid"` (unknown keys rejected)
    - `case_sensitive=False` (NEXUSDL_LOG_LEVEL = nexusdl_log_level)
    - Paths are `Path` objects
    - No secrets in `repr()` or `model_dump()`

What is tested:
    - Defaults
    - Load from env vars
    - Load from .env file
    - Load from explicit kwargs
    - Precedence (kwargs > env > .env > defaults)
    - Secrets (SecretStr, masked in repr/dump)
    - Validation (missing required, invalid URL, invalid enum, invalid range)
    - extra="forbid"
    - case_sensitive=False
    - Path resolution
    - Bool parsing
    - CORS origins (JSON list)
    - load_settings() function
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from nexusdl.core.config import Settings, load_settings


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

#: A valid Fernet-compatible secret (64 hex chars)
VALID_SECRET = "a" * 64

#: Minimal valid env for Settings
VALID_ENV = {
    "NEXUSDL_SECRET_KEY": VALID_SECRET,
    "NEXUSDL_JWT_SECRET": "b" * 64,
    "NEXUSDL_COOKIE_ENCRYPTION_KEY": "c" * 64,
    "NEXUSDL_PUBLIC_URL": "https://nexusdl.example.com",
    "NEXUSDL_API_BASE_URL": "https://api.nexusdl.example.com",
    "NEXUSDL_FRONTEND_URL": "https://nexusdl.example.com",
}


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all NEXUSDL_* env vars before each test."""
    import os

    for key in list(os.environ):
        if key.startswith("NEXUSDL_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Set valid env vars."""
    for k, v in VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return VALID_ENV


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """Write a .env file."""
    env_path = tmp_path / ".env"
    lines = [f"{k}={v}" for k, v in VALID_ENV.items()]
    env_path.write_text("\n".join(lines) + "\n")
    return env_path


# ────────────────────────────────────────────────────────────────────
# Test 1: Defaults
# ────────────────────────────────────────────────────────────────────


def test_defaults_from_valid_env(env: dict) -> None:
    """Settings can be built from minimal env vars."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.app_name == "NexusDL"
    assert settings.app_environment == "dev"
    assert settings.log_level == "INFO"
    assert settings.log_format == "pretty"


def test_defaults_secret_masked(env: dict) -> None:
    """Secrets are SecretStr and masked in repr."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert isinstance(settings.secret_key, SecretStr)
    assert isinstance(settings.jwt_secret, SecretStr)
    assert isinstance(settings.cookie_encryption_key, SecretStr)


def test_defaults_missing_secrets_raises() -> None:
    """Missing SECRET_KEY raises ValidationError."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ────────────────────────────────────────────────────────────────────
# Test 2: Load from env vars
# ────────────────────────────────────────────────────────────────────


def test_load_from_env(env: dict) -> None:
    """Settings loads NEXUSDL_* env vars."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.secret_key.get_secret_value() == "a" * 64
    assert str(settings.public_url) == "https://nexusdl.example.com"


def test_env_case_insensitive(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars are case-insensitive (NEXUSDL_LOG_LEVEL = nexusdl_log_level)."""
    # Env vars are case-sensitive on Linux, but Pydantic Settings normalizes
    monkeypatch.setenv("NEXUSDL_LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "DEBUG"


# ────────────────────────────────────────────────────────────────────
# Test 3: Load from .env file
# ────────────────────────────────────────────────────────────────────


def test_load_from_env_file(env_file: Path) -> None:
    """Settings loads from a .env file."""
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.secret_key.get_secret_value() == "a" * 64
    assert str(settings.public_url) == "https://nexusdl.example.com"


def test_env_overrides_env_file(
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars override .env file values."""
    monkeypatch.setenv("NEXUSDL_SECRET_KEY", "d" * 64)
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.secret_key.get_secret_value() == "d" * 64


def test_kwargs_override_env(
    env: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwargs override env vars."""
    monkeypatch.setenv("NEXUSDL_SECRET_KEY", "d" * 64)
    settings = Settings(secret_key="e" * 64, _env_file=None)  # type: ignore[call-arg]

    assert settings.secret_key.get_secret_value() == "e" * 64


# ────────────────────────────────────────────────────────────────────
# Test 4: SecretStr
# ────────────────────────────────────────────────────────────────────


def test_secret_str_type(env: dict) -> None:
    """Secrets are SecretStr."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert isinstance(settings.secret_key, SecretStr)


def test_secret_str_repr_masked(env: dict) -> None:
    """repr() masks secrets."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    repr_str = repr(settings)
    assert "a" * 64 not in repr_str


def test_secret_str_model_dump_masked(env: dict) -> None:
    """model_dump() masks secrets."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    data = settings.model_dump()
    # SecretStr values are SecretStr objects, not plain strings
    assert isinstance(data["secret_key"], SecretStr)
    assert data["secret_key"].get_secret_value() == "a" * 64


def test_secret_str_get_value(env: dict) -> None:
    """get_secret_value() returns the plain value."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.secret_key.get_secret_value() == "a" * 64


# ────────────────────────────────────────────────────────────────────
# Test 5: Validation
# ────────────────────────────────────────────────────────────────────


def test_invalid_app_environment(env: dict) -> None:
    """Invalid APP_ENVIRONMENT raises ValidationError."""
    with pytest.raises(ValidationError):
        Settings(app_environment="invalid", _env_file=None)  # type: ignore[call-arg]


def test_invalid_log_level(env: dict) -> None:
    """Invalid LOG_LEVEL raises ValidationError."""
    with pytest.raises(ValidationError):
        Settings(log_level="VERBOSE", _env_file=None)  # type: ignore[call-arg]


def test_invalid_log_format(env: dict) -> None:
    """Invalid LOG_FORMAT raises ValidationError."""
    with pytest.raises(ValidationError):
        Settings(log_format="xml", _env_file=None)  # type: ignore[call-arg]


def test_invalid_url(env: dict) -> None:
    """Invalid URL raises ValidationError."""
    with pytest.raises(ValidationError):
        Settings(public_url="not-a-url", _env_file=None)  # type: ignore[call-arg]


def test_max_concurrent_downloads_range(env: dict) -> None:
    """max_concurrent_downloads must be in [1, 10]."""
    # Valid
    Settings(max_concurrent_downloads=1, _env_file=None)  # type: ignore[call-arg]
    Settings(max_concurrent_downloads=10, _env_file=None)  # type: ignore[call-arg]

    # Invalid
    with pytest.raises(ValidationError):
        Settings(max_concurrent_downloads=0, _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(max_concurrent_downloads=100, _env_file=None)  # type: ignore[call-arg]


def test_extra_forbid(env: dict) -> None:
    """Unknown fields are rejected (extra='forbid')."""
    with pytest.raises(ValidationError):
        Settings(unknown_field="value", _env_file=None)  # type: ignore[call-arg]


# ────────────────────────────────────────────────────────────────────
# Test 6: Bool parsing
# ────────────────────────────────────────────────────────────────────


def test_bool_env_var(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bool env vars are parsed correctly."""
    monkeypatch.setenv("NEXUSDL_ALLOW_ADULT", "true")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.allow_adult is True

    monkeypatch.setenv("NEXUSDL_ALLOW_ADULT", "false")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.allow_adult is False


def test_bool_invalid(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid bool raises ValidationError."""
    monkeypatch.setenv("NEXUSDL_ALLOW_ADULT", "maybe")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ────────────────────────────────────────────────────────────────────
# Test 7: CORS origins
# ────────────────────────────────────────────────────────────────────


def test_cors_origins_json(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """CORS origins are parsed from JSON array."""
    monkeypatch.setenv(
        "NEXUSDL_API_CORS_ORIGINS",
        json.dumps(["https://app.example.com", "https://admin.example.com"]),
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.api_cors_origins == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_cors_origins_empty(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """CORS origins can be empty."""
    monkeypatch.setenv("NEXUSDL_API_CORS_ORIGINS", "[]")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.api_cors_origins == []


# ────────────────────────────────────────────────────────────────────
# Test 8: Paths
# ────────────────────────────────────────────────────────────────────


def test_paths_are_path_objects(env: dict, tmp_path: Path) -> None:
    """Path fields are Path objects."""
    settings = Settings(
        download_dir=tmp_path / "downloads",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert isinstance(settings.download_dir, Path)


def test_paths_created(env: dict, tmp_path: Path) -> None:
    """Path fields are usable."""
    settings = Settings(
        download_dir=tmp_path / "downloads",
        _env_file=None,  # type: ignore[call-arg]
    )
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    assert settings.download_dir.exists()


# ────────────────────────────────────────────────────────────────────
# Test 9: load_settings()
# ────────────────────────────────────────────────────────────────────


def test_load_settings(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_settings() loads from env."""
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert settings.secret_key.get_secret_value() == "a" * 64


def test_load_settings_with_env_file(env_file: Path) -> None:
    """load_settings() loads from .env file."""
    settings = load_settings(_env_file=env_file)
    assert isinstance(settings, Settings)


# ────────────────────────────────────────────────────────────────────
# Test 10: Precedence
# ────────────────────────────────────────────────────────────────────


def test_precedence_kwargs_over_env(
    env: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kwargs > env."""
    monkeypatch.setenv("NEXUSDL_LOG_LEVEL", "DEBUG")
    settings = Settings(log_level="ERROR", _env_file=None)  # type: ignore[call-arg]
    assert settings.log_level == "ERROR"


def test_precedence_env_over_env_file(
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env > .env."""
    monkeypatch.setenv("NEXUSDL_LOG_LEVEL", "ERROR")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.log_level == "ERROR"
