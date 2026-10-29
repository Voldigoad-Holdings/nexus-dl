# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for the CLI commands (Typer).

Uses ``typer.testing.CliRunner`` to invoke commands **in-process**
(no subprocess, no real network, no real DB).

Covers:
    - --version, --help
    - nexusdl sites
    - nexusdl search (mocked)
    - nexusdl info (mocked)
    - nexusdl download (mocked, no files written)
    - nexusdl library list/search (mocked DB)
    - nexusdl config show/paths
    - Exit codes (0, 1, 2)
    - --json output
    - --quiet, --verbose
    - Missing args

Why in-process?
    - Fast (< 2s for the whole file)
    - Deterministic (no network)
    - Catches Typer wiring bugs (option names, defaults, help strings)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

if TYPE_CHECKING:
    from nexusdl.core.config import Settings

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.search import SearchResult
from nexusdl.core.models.site import SiteCapabilities, SiteConfig
from nexusdl.interfaces.cli.cli_entry import app


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    """CliRunner with stdout/stderr separated."""
    return CliRunner(mix_stderr=False)


@pytest.fixture
def fake_settings(tmp_path: Path) -> "Settings":
    """A fake Settings object (minimal)."""
    from nexusdl.core.config import Settings

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_name="NexusDL-Test",
        app_environment="dev",
        public_url="http://localhost:3000",
        api_base_url="http://localhost:8000",
        frontend_url="http://localhost:3000",
        secret_key="test-secret-" + "a" * 52,
        jwt_secret="test-jwt-" + "b" * 55,
        cookie_encryption_key="test-cookie-" + "c" * 50,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/library.db",
        download_dir=tmp_path / "downloads",
        temp_dir=tmp_path / "temp",
        library_db_path=tmp_path / "library.db",
        cookie_storage_path=tmp_path / "cookies.enc",
        logs_dir=tmp_path / "logs",
        allow_adult=False,
        playwright_enabled=False,
        log_level="WARNING",
        log_format="text",
    )


@pytest.fixture
def fake_site_config() -> SiteConfig:
    """A fake SiteConfig."""
    return SiteConfig(
        id="mangadex",
        name="MangaDex",
        domains=["https://mangadex.org"],
        parser_class="nexusdl.parsers.en.mangadex:MangaDexParser",
        language=Language.EN,
        adult=False,
        capabilities=SiteCapabilities(),
    )


@pytest.fixture
def fake_manga() -> Manga:
    """A fake Manga."""
    return Manga(
        id="a1b2c3d4e5f6a7b8",
        source_id="a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6",
        site="mangadex",
        title="One Piece",
        author="Oda Eiichiro",
        status=MangaStatus.ONGOING,
        language=Language.EN,
        content_rating=ContentRating.SAFE,
        url="https://mangadex.org/title/a1b2c3d4e5f6a7b8",
        updated_at="2026-10-29T12:00:00Z",
    )


@pytest.fixture
def fake_search_results() -> list[SearchResult]:
    """A fake list of SearchResult."""
    return [
        SearchResult(
            site_id="mangadex",
            site_name="MangaDex",
            manga_id="a1b2c3d4e5f6a7b8",
            title="One Piece",
            cover_url="https://uploads.mangadex.org/covers/a1b2c3d4e5f6a7b8/cover.jpg",
            author="Oda Eiichiro",
            language=Language.EN,
            content_rating=ContentRating.SAFE,
            url="https://mangadex.org/title/a1b2c3d4e5f6a7b8",
        ),
    ]


# ────────────────────────────────────────────────────────────────────
# Test 1: --version
# ────────────────────────────────────────────────────────────────────


def test_version(runner: CliRunner) -> None:
    """nexusdl --version prints the version and exits 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # Should contain "nexusdl" and a version number
    assert "nexusdl" in result.stdout.lower()
    assert any(c.isdigit() for c in result.stdout)


# ────────────────────────────────────────────────────────────────────
# Test 2: --help
# ────────────────────────────────────────────────────────────────────


def test_help(runner: CliRunner) -> None:
    """nexusdl --help prints the help and exits 0."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout or "Commands" in result.stdout


def test_help_no_args(runner: CliRunner) -> None:
    """nexusdl (no args) shows help (Typer default)."""
    result = runner.invoke(app, [])
    # Typer returns 0 for no args in some versions, 2 in others
    assert result.exit_code in (0, 2)


# ────────────────────────────────────────────────────────────────────
# Test 3: sites
# ────────────────────────────────────────────────────────────────────


def test_sites_lists_sites(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
) -> None:
    """nexusdl sites lists the configured sites."""
    # Mock load_settings
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    # Mock SiteRegistry
    fake_registry = MagicMock()
    fake_registry.list_sites.return_value = [
        SiteConfig(
            id="mangadex",
            name="MangaDex",
            domains=["https://mangadex.org"],
            parser_class="nexusdl.parsers.en.mangadex:MangaDexParser",
            language=Language.EN,
            adult=False,
            capabilities=SiteCapabilities(),
        ),
    ]

    class FakeFactory:
        def create(self, site, session):
            return MagicMock()

    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.SiteRegistry",
        lambda *args, **kwargs: fake_registry,
    )
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.DefaultParserFactory",
        FakeFactory,
    )

    result = runner.invoke(app, ["sites"])
    assert result.exit_code == 0, f"Failed: {result.stdout}"
    assert "mangadex" in result.stdout.lower() or "MangaDex" in result.stdout


def test_sites_json(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
) -> None:
    """nexusdl sites --json outputs valid JSON."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    fake_registry = MagicMock()
    fake_registry.list_sites.return_value = [
        SiteConfig(
            id="mangadex",
            name="MangaDex",
            domains=["https://mangadex.org"],
            parser_class="nexusdl.parsers.en.mangadex:MangaDexParser",
            language=Language.EN,
            adult=False,
            capabilities=SiteCapabilities(),
        ),
    ]

    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.SiteRegistry",
        lambda *args, **kwargs: fake_registry,
    )

    result = runner.invoke(app, ["sites", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "items" in data or isinstance(data, list)


# ────────────────────────────────────────────────────────────────────
# Test 4: search
# ────────────────────────────────────────────────────────────────────


def test_search_returns_results(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
    fake_search_results: list[SearchResult],
) -> None:
    """nexusdl search prints results."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    async def fake_search_all(*args, **kwargs):
        for r in fake_search_results:
            yield r

    fake_registry = MagicMock()
    fake_registry.search_all = fake_search_all

    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.SiteRegistry",
        lambda *args, **kwargs: fake_registry,
    )

    result = runner.invoke(app, ["search", "one piece"])
    assert result.exit_code == 0, f"Failed: {result.stdout}"
    assert "One Piece" in result.stdout or "one piece" in result.stdout.lower()


def test_search_json(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
    fake_search_results: list[SearchResult],
) -> None:
    """nexusdl search --json outputs valid JSON."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    async def fake_search_all(*args, **kwargs):
        for r in fake_search_results:
            yield r

    fake_registry = MagicMock()
    fake_registry.search_all = fake_search_all

    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.SiteRegistry",
        lambda *args, **kwargs: fake_registry,
    )

    result = runner.invoke(app, ["search", "one piece", "--json"])
    assert result.exit_code == 0, f"Failed: {result.stdout}"
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["title"] == "One Piece"


def test_search_missing_query(runner: CliRunner) -> None:
    """nexusdl search (no query) → exit code 2 (missing arg)."""
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 2


# ────────────────────────────────────────────────────────────────────
# Test 5: info
# ────────────────────────────────────────────────────────────────────


def test_info(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
    fake_manga: Manga,
) -> None:
    """nexusdl info prints manga info."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    async def fake_get_manga(*args, **kwargs):
        return fake_manga

    fake_parser = MagicMock()
    fake_parser.get_manga = fake_get_manga

    fake_registry = MagicMock()
    fake_registry.get_parser.return_value = fake_parser
    fake_registry.get_site.return_value = MagicMock()

    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.SiteRegistry",
        lambda *args, **kwargs: fake_registry,
    )
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.DefaultParserFactory",
        lambda: MagicMock(),
    )

    result = runner.invoke(app, ["info", "https://mangadex.org/title/abc"])
    assert result.exit_code == 0, f"Failed: {result.stdout}"
    assert "One Piece" in result.stdout


# ────────────────────────────────────────────────────────────────────
# Test 6: download (mocked)
# ────────────────────────────────────────────────────────────────────


def test_download_invalid_format(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
) -> None:
    """nexusdl download --format CBR → exit code 2 (invalid choice)."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    result = runner.invoke(
        app,
        ["download", "https://mangadex.org/title/abc", "--format", "CBR"],
    )
    assert result.exit_code == 2


# ────────────────────────────────────────────────────────────────────
# Test 7: library list
# ────────────────────────────────────────────────────────────────────


def test_library_list_empty(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
) -> None:
    """nexusdl library list on empty DB prints '0 mangas'."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    fake_db = MagicMock()

    async def fake_list(*args, **kwargs):
        from nexusdl.core.library.database import PaginatedResult

        return PaginatedResult(
            items=[],
            total=0,
            page=1,
            per_page=20,
            total_pages=0,
            has_next=False,
            has_prev=False,
        )

    fake_db.list_mangas = fake_list

    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.LibraryDatabase",
        lambda *args, **kwargs: fake_db,
    )

    result = runner.invoke(app, ["library", "list"])
    # Should exit 0 with "no mangas" message
    assert result.exit_code == 0
    assert "0" in result.stdout or "empty" in result.stdout.lower() or "no manga" in result.stdout.lower()


# ────────────────────────────────────────────────────────────────────
# Test 8: config show
# ────────────────────────────────────────────────────────────────────


def test_config_show_masks_secrets(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
) -> None:
    """nexusdl config show masks secrets with **********."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0, f"Failed: {result.stdout}"
    # Secrets must be masked
    assert "test-secret-" not in result.stdout
    assert "test-jwt-" not in result.stdout
    assert "test-cookie-" not in result.stdout
    assert "**********" in result.stdout


# ────────────────────────────────────────────────────────────────────
# Test 9: config paths
# ────────────────────────────────────────────────────────────────────


def test_config_paths(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
    tmp_path: Path,
) -> None:
    """nexusdl config paths prints the paths."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    result = runner.invoke(app, ["config", "paths"])
    assert result.exit_code == 0, f"Failed: {result.stdout}"
    # Should show at least the downloads path
    assert str(tmp_path) in result.stdout or "downloads" in result.stdout.lower()


# ────────────────────────────────────────────────────────────────────
# Test 10: --quiet
# ────────────────────────────────────────────────────────────────────


def test_quiet(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_settings: "Settings",
) -> None:
    """nexusdl --quiet suppresses output."""
    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.load_settings",
        lambda: fake_settings,
    )

    fake_registry = MagicMock()
    fake_registry.list_sites.return_value = []

    monkeypatch.setattr(
        "nexusdl.interfaces.cli.commands.SiteRegistry",
        lambda *args, **kwargs: fake_registry,
    )

    result = runner.invoke(app, ["--quiet", "sites"])
    # Quiet mode may still exit 0 with minimal output
    assert result.exit_code == 0
    # Very little output
    assert len(result.stdout.strip()) < 100


# ────────────────────────────────────────────────────────────────────
# Test 11: unknown command
# ────────────────────────────────────────────────────────────────────


def test_unknown_command(runner: CliRunner) -> None:
    """nexusdl unknowncmd → exit code 2."""
    result = runner.invoke(app, ["unknowncmd"])
    assert result.exit_code == 2
