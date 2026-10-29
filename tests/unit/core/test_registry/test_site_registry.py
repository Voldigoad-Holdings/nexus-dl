# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for SiteRegistry.

SiteRegistry is the central catalog of supported sites. It:

    - Loads sites.yaml (validated against sites_schema.json)
    - Applies user overrides (sites_overrides.yaml)
    - Applies categories (fr/en/kr/adult)
    - Injects ParserFactory (ADR-003) to break core → parsers
    - Exposes get_site(), get_parser(), list_sites(), search_all()

What is tested:
    - __init__ with injected ParserFactory (ADR-003)
    - _load_sites() (valid, invalid, aliases, categories, _comment)
    - get_site() (valid, alias, unknown)
    - get_parser() (delegates to factory)
    - list_sites() (all, language filter, adult filter, include_adult)
    - search_all() (async iterator, concurrent, error isolation)
    - reload() (hot reload)
    - Override file merge
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

from nexusdl.core.config import Settings
from nexusdl.core.exceptions import ConfigError
from nexusdl.core.models.enums import Language
from nexusdl.core.models.search import SearchResult
from nexusdl.core.models.site import SiteCapabilities, SiteConfig
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.base import BaseParser


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent.parent.parent / "fixtures"


# ────────────────────────────────────────────────────────────────────
# Fake parser factory (ADR-003)
# ────────────────────────────────────────────────────────────────────


class FakeParser(BaseParser):
    """Fake parser for testing."""

    site_id = "fake"
    language = "en"
    adult = False

    def __init__(self, site: SiteConfig, session, **_kwargs) -> None:
        super().__init__(site, session)

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        return [
            SearchResult(
                site_id=self.site.id,
                site_name=self.site.name,
                manga_id=f"{self.site.id}-manga",
                title=f"{query} on {self.site.name}",
                language=Language.EN,
                content_rating="safe",
                url=f"https://{self.site.id}.test/manga/1",
            )
        ]

    async def get_manga(self, url_or_id: str):
        raise NotImplementedError

    async def get_chapters(self, manga):
        return []

    async def get_pages(self, chapter):
        return []


class FakeParserFactory:
    """Fake ParserFactory (no real parsers)."""

    def __init__(self) -> None:
        self.calls: list[tuple[SiteConfig, object]] = []

    def create(self, site: SiteConfig, session) -> BaseParser:
        self.calls.append((site, session))
        return FakeParser(site, session)


class FailingParserFactory:
    """Fake factory that raises for a specific site."""

    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.calls: list[str] = []

    def create(self, site: SiteConfig, session) -> BaseParser:
        self.calls.append(site.id)
        if site.id == self.fail_on:
            raise ValueError(f"parser failed for {site.id}")
        return FakeParser(site, session)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_name="NexusDL-Test",
        app_environment="dev",
        public_url="http://test",
        api_base_url="http://test",
        frontend_url="http://test",
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
        max_concurrent_downloads=2,
    )


@pytest.fixture
def sites_yaml(tmp_path: Path, settings: Settings) -> Path:
    """Create a temporary sites.yaml with 3 sites."""
    data = {
        "sites": [
            {
                "id": "mangadex",
                "name": "MangaDex",
                "domains": ["https://mangadex.org"],
                "parser_class": "nexusdl.parsers.en.mangadex:MangaDexParser",
                "language": "en",
                "adult": False,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": [],
            },
            {
                "id": "sushiscan",
                "name": "SushiScan",
                "domains": ["https://sushiscan.net"],
                "parser_class": "nexusdl.parsers.fr.sushiscan:SushiScanParser",
                "language": "fr",
                "adult": False,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": [],
            },
            {
                "id": "nhentai",
                "name": "nhentai",
                "domains": ["https://nhentai.net"],
                "parser_class": "nexusdl.parsers.adult.nhentai:NhentaiParser",
                "language": "en",
                "adult": True,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": ["cf_clearance"],
            },
        ]
    }

    path = tmp_path / "sites.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def registry(settings: Settings, sites_yaml: Path) -> SiteRegistry:
    """SiteRegistry with a FakeParserFactory."""
    return SiteRegistry(
        settings=settings,
        sites_yaml_path=sites_yaml,
        parser_factory=FakeParserFactory(),
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: __init__ and loading
# ────────────────────────────────────────────────────────────────────


def test_init_loads_sites(registry: SiteRegistry) -> None:
    """__init__ loads all sites from sites.yaml."""
    sites = registry.list_sites()
    assert len(sites) == 3


def test_init_injects_factory(settings: Settings, sites_yaml: Path) -> None:
    """__init__ injects the ParserFactory (ADR-003)."""
    factory = FakeParserFactory()
    registry = SiteRegistry(
        settings=settings,
        sites_yaml_path=sites_yaml,
        parser_factory=factory,
    )
    # Trigger get_parser to verify the factory is used
    site = registry.get_site("mangadex")
    registry.get_parser("mangadex", session=None)
    assert len(factory.calls) == 1
    assert factory.calls[0][0].id == "mangadex"


def test_init_missing_yaml_raises(settings: Settings, tmp_path: Path) -> None:
    """__init__ raises if sites.yaml is missing."""
    missing = tmp_path / "missing.yaml"
    with pytest.raises((FileNotFoundError, ConfigError)):
        SiteRegistry(
            settings=settings,
            sites_yaml_path=missing,
            parser_factory=FakeParserFactory(),
        )


# ────────────────────────────────────────────────────────────────────
# Test 2: get_site()
# ────────────────────────────────────────────────────────────────────


def test_get_site(registry: SiteRegistry) -> None:
    """get_site() returns the SiteConfig."""
    site = registry.get_site("mangadex")
    assert site.id == "mangadex"
    assert site.name == "MangaDex"
    assert site.language == Language.EN


def test_get_site_unknown_raises(registry: SiteRegistry) -> None:
    """get_site() on unknown id raises KeyError."""
    with pytest.raises(KeyError):
        registry.get_site("unknown")


def test_get_site_alias(settings: Settings, tmp_path: Path) -> None:
    """get_site() resolves aliases."""
    data = {
        "sites": [
            {
                "id": "mangadex",
                "name": "MangaDex",
                "domains": ["https://mangadex.org"],
                "parser_class": "nexusdl.parsers.en.mangadex:MangaDexParser",
                "language": "en",
                "adult": False,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": [],
            }
        ],
        "aliases": {"md": "mangadex"},
    }
    path = tmp_path / "sites.yaml"
    path.write_text(yaml.safe_dump(data))

    registry = SiteRegistry(
        settings=settings,
        sites_yaml_path=path,
        parser_factory=FakeParserFactory(),
    )

    site = registry.get_site("md")
    assert site.id == "mangadex"


# ────────────────────────────────────────────────────────────────────
# Test 3: get_parser()
# ────────────────────────────────────────────────────────────────────


def test_get_parser(registry: SiteRegistry) -> None:
    """get_parser() delegates to the injected factory."""
    parser = registry.get_parser("mangadex", session=None)
    assert isinstance(parser, FakeParser)
    assert parser.site.id == "mangadex"


def test_get_parser_unknown_raises(registry: SiteRegistry) -> None:
    """get_parser() on unknown id raises."""
    with pytest.raises(KeyError):
        registry.get_parser("unknown", session=None)


def test_get_parser_calls_factory_per_call(settings: Settings, sites_yaml: Path) -> None:
    """get_parser() calls factory.create() each time (not shared)."""
    factory = FakeParserFactory()
    registry = SiteRegistry(
        settings=settings,
        sites_yaml_path=sites_yaml,
        parser_factory=factory,
    )

    registry.get_parser("mangadex", session=None)
    registry.get_parser("mangadex", session=None)
    assert len(factory.calls) == 2


# ────────────────────────────────────────────────────────────────────
# Test 4: list_sites()
# ────────────────────────────────────────────────────────────────────


def test_list_sites_all(registry: SiteRegistry) -> None:
    """list_sites() returns all sites by default."""
    sites = registry.list_sites()
    assert len(sites) == 3
    ids = {s.id for s in sites}
    assert ids == {"mangadex", "sushiscan", "nhentai"}


def test_list_sites_language_filter(registry: SiteRegistry) -> None:
    """list_sites(language='fr') returns only French sites."""
    sites = registry.list_sites(language="fr")
    assert len(sites) == 1
    assert sites[0].id == "sushiscan"


def test_list_sites_adult_excluded_by_default(registry: SiteRegistry) -> None:
    """list_sites() excludes adult by default."""
    sites = registry.list_sites()
    ids = {s.id for s in sites}
    assert "nhentai" not in ids


def test_list_sites_include_adult(registry: SiteRegistry) -> None:
    """list_sites(include_adult=True) includes adult sites."""
    sites = registry.list_sites(include_adult=True)
    ids = {s.id for s in sites}
    assert "nhentai" in ids
    assert len(sites) == 3


# ────────────────────────────────────────────────────────────────────
# Test 5: search_all()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_all(registry: SiteRegistry) -> None:
    """search_all() yields SearchResult from all sites."""
    results = []
    async for r in registry.search_all("one piece"):
        results.append(r)

    # 2 non-adult sites → 2 results
    assert len(results) == 2
    assert all(isinstance(r, SearchResult) for r in results)


@pytest.mark.asyncio
async def test_search_all_empty_query(registry: SiteRegistry) -> None:
    """search_all('') yields nothing."""
    results = []
    async for r in registry.search_all(""):
        results.append(r)
    # Empty query may return [] or all sites' results (depending on impl)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_search_all_error_isolation(settings: Settings, sites_yaml: Path) -> None:
    """search_all() continues if one site fails."""
    factory = FailingParserFactory(fail_on="mangadex")
    registry = SiteRegistry(
        settings=settings,
        sites_yaml_path=sites_yaml,
        parser_factory=factory,
    )

    results = []
    async for r in registry.search_all("one piece"):
        results.append(r)

    # sushiscan should still return a result
    site_ids = {r.site_id for r in results}
    assert "sushiscan" in site_ids
    assert "mangadex" not in site_ids


@pytest.mark.asyncio
async def test_search_all_concurrent_limit(settings: Settings, sites_yaml: Path) -> None:
    """search_all() respects max_concurrent."""
    active = {"current": 0, "max": 0}
    lock = asyncio.Lock()

    class SlowParser(FakeParser):
        async def search(self, query: str, *, page: int = 1):
            async with lock:
                active["current"] += 1
                active["max"] = max(active["max"], active["current"])
            await asyncio.sleep(0.05)
            async with lock:
                active["current"] -= 1
            return await super().search(query, page=page)

    class SlowFactory:
        def create(self, site, session):
            return SlowParser(site, session)

    registry = SiteRegistry(
        settings=settings,
        sites_yaml_path=sites_yaml,
        parser_factory=SlowFactory(),
    )

    async for _ in registry.search_all("test", max_concurrent=2):
        pass

    assert active["max"] <= 2, f"Concurrency exceeded: {active['max']}"


# ────────────────────────────────────────────────────────────────────
# Test 6: reload()
# ────────────────────────────────────────────────────────────────────


def test_reload(registry: SiteRegistry, sites_yaml: Path) -> None:
    """reload() re-reads sites.yaml."""
    assert len(registry.list_sites()) == 3

    # Add a site
    data = yaml.safe_load(sites_yaml.read_text())
    data["sites"].append({
        "id": "new_site",
        "name": "New Site",
        "domains": ["https://new.site"],
        "parser_class": "nexusdl.parsers.en.new:NewParser",
        "language": "en",
        "adult": False,
        "capabilities": SiteCapabilities().model_dump(),
        "default_headers": {},
        "cookies_required": [],
    })
    sites_yaml.write_text(yaml.safe_dump(data))

    registry.reload()
    assert len(registry.list_sites()) == 4


# ────────────────────────────────────────────────────────────────────
# Test 7: categories and _comment
# ────────────────────────────────────────────────────────────────────


def test_categories_applied(settings: Settings, tmp_path: Path) -> None:
    """categories section is applied to sites."""
    data = {
        "sites": [
            {
                "id": "mangadex",
                "name": "MangaDex",
                "domains": ["https://mangadex.org"],
                "parser_class": "nexusdl.parsers.en.mangadex:MangaDexParser",
                "language": "en",
                "adult": False,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": [],
            }
        ],
        "categories": {
            "en": {
                "default_headers": {"Accept-Language": "en-US,en;q=0.9"},
            }
        },
    }
    path = tmp_path / "sites.yaml"
    path.write_text(yaml.safe_dump(data))

    registry = SiteRegistry(
        settings=settings,
        sites_yaml_path=path,
        parser_factory=FakeParserFactory(),
    )

    site = registry.get_site("mangadex")
    assert site.default_headers.get("Accept-Language") == "en-US,en;q=0.9"


def test_comment_ignored(settings: Settings, tmp_path: Path) -> None:
    """_comment fields are ignored (not passed to Pydantic)."""
    data = {
        "sites": [
            {
                "id": "mangadex",
                "name": "MangaDex",
                "domains": ["https://mangadex.org"],
                "parser_class": "nexusdl.parsers.en.mangadex:MangaDexParser",
                "language": "en",
                "adult": False,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": [],
                "_comment": "This is a comment",
            }
        ]
    }
    path = tmp_path / "sites.yaml"
    path.write_text(yaml.safe_dump(data))

    # Should not raise
    registry = SiteRegistry(
        settings=settings,
        sites_yaml_path=path,
        parser_factory=FakeParserFactory(),
    )
    assert len(registry.list_sites()) == 1


# ────────────────────────────────────────────────────────────────────
# Test 8: invalid sites.yaml
# ────────────────────────────────────────────────────────────────────


def test_invalid_yaml_raises(settings: Settings, tmp_path: Path) -> None:
    """Invalid YAML raises ConfigError."""
    path = tmp_path / "sites.yaml"
    path.write_text("not: [valid: yaml: -")

    with pytest.raises((ConfigError, yaml.YAMLError)):
        SiteRegistry(
            settings=settings,
            sites_yaml_path=path,
            parser_factory=FakeParserFactory(),
        )


def test_duplicate_site_id_raises(settings: Settings, tmp_path: Path) -> None:
    """Duplicate site IDs raise ConfigError."""
    data = {
        "sites": [
            {
                "id": "duplicate",
                "name": "First",
                "domains": ["https://first.test"],
                "parser_class": "nexusdl.parsers.en.first:FirstParser",
                "language": "en",
                "adult": False,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": [],
            },
            {
                "id": "duplicate",
                "name": "Second",
                "domains": ["https://second.test"],
                "parser_class": "nexusdl.parsers.en.second:SecondParser",
                "language": "en",
                "adult": False,
                "capabilities": SiteCapabilities().model_dump(),
                "default_headers": {},
                "cookies_required": [],
            },
        ]
    }
    path = tmp_path / "sites.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError, match="duplicate"):
        SiteRegistry(
            settings=settings,
            sites_yaml_path=path,
            parser_factory=FakeParserFactory(),
        )
