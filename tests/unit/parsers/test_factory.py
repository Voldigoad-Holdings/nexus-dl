# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for ParserFactory (ADR-003).

The factory is the mechanism that lets ``core/`` request a parser
without importing ``parsers/`` directly:

    # In core/registry/site_registry.py:
    parser = self._parser_factory.create(site, session)

    # In interfaces/cli/cli_entry.py (composition root):
    registry = SiteRegistry(
        settings,
        parser_factory=DefaultParserFactory(),
    )

This test verifies:

    - register() adds a parser to the internal registry
    - create() returns an instance of the registered class
    - create() raises ParserNotFoundError if not registered
    - Duplicate registration raises or warns (depending on policy)
    - DefaultParserFactory implements the ParserFactory Protocol
    - The registry is isolated between tests (via reset_registry fixture)
    - Thread-safety (concurrent register() calls)

No network, no real parsers (fake parsers only).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from nexusdl.core.models.site import SiteConfig
    from nexusdl.core.session.http_session import HttpSession

from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult
from nexusdl.core.models.site import SiteCapabilities, SiteConfig
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import (
    DefaultParserFactory,
    ParserNotFoundError,
    get_registered_parser,
    list_registered_parsers,
    register,
    reset_registry,
    unregister,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_registry_fixture() -> None:
    """Reset the global parser registry before and after each test.

    Without this, tests would leak state (one test registering a parser
    would affect another test).
    """
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def site_config() -> SiteConfig:
    """Minimal SiteConfig for testing."""
    return SiteConfig(
        id="test",
        name="Test Site",
        domains=["https://test.example.com"],
        parser_class="nexusdl.parsers.custom._template:TestParser",
        language=Language.EN,
        adult=False,
        capabilities=SiteCapabilities(),
    )


@pytest.fixture
def fake_session() -> MagicMock:
    """Fake HttpSession (mock)."""
    session = MagicMock(spec=["get", "post", "get_html", "get_json", "stream_download"])
    session.get = AsyncMock()
    session.get_html = AsyncMock(return_value="<html></html>")
    session.get_json = AsyncMock(return_value={})
    return session


# ────────────────────────────────────────────────────────────────────
# Fake parsers (concrete subclasses for testing)
# ────────────────────────────────────────────────────────────────────


class FakeParserA(BaseParser):
    """Fake parser A."""

    site_id = "site_a"
    language = "en"
    adult = False

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        return []

    async def get_manga(self, url_or_id: str) -> Manga:
        raise NotImplementedError

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        return []

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        return []


class FakeParserB(BaseParser):
    """Fake parser B."""

    site_id = "site_b"
    language = "fr"
    adult = True

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        return []

    async def get_manga(self, url_or_id: str) -> Manga:
        raise NotImplementedError

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        return []

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        return []


# ────────────────────────────────────────────────────────────────────
# 1. Basic registration
# ────────────────────────────────────────────────────────────────────


def test_register_adds_parser() -> None:
    """register() adds a parser to the registry."""
    register("site_a", FakeParserA)

    parsers = list_registered_parsers()
    assert "site_a" in parsers
    assert parsers["site_a"] is FakeParserA


def test_register_multiple() -> None:
    """Multiple parsers can be registered."""
    register("site_a", FakeParserA)
    register("site_b", FakeParserB)

    parsers = list_registered_parsers()
    assert len(parsers) == 2
    assert "site_a" in parsers
    assert "site_b" in parsers


def test_register_overwrites_by_default() -> None:
    """Re-registering a site_id overwrites (with a warning)."""
    register("site_a", FakeParserA)
    register("site_a", FakeParserB)

    parsers = list_registered_parsers()
    assert len(parsers) == 1
    assert parsers["site_a"] is FakeParserB


def test_register_rejects_non_parser() -> None:
    """register() rejects classes that don't extend BaseParser."""
    with pytest.raises(TypeError, match="BaseParser"):
        register("site_x", object)  # type: ignore[arg-type]


def test_register_rejects_empty_site_id() -> None:
    """register() rejects empty site_id."""
    with pytest.raises(ValueError, match="site_id"):
        register("", FakeParserA)


def test_unregister_removes_parser() -> None:
    """unregister() removes a parser."""
    register("site_a", FakeParserA)
    assert get_registered_parser("site_a") is FakeParserA

    unregister("site_a")
    assert get_registered_parser("site_a") is None


def test_unregister_missing_is_noop() -> None:
    """unregister() on unknown site_id doesn't raise."""
    unregister("never_registered")  # should not raise


# ────────────────────────────────────────────────────────────────────
# 2. Factory: create()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_instance(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """create() returns an instance of the registered parser."""
    register("test", FakeParserA)

    factory = DefaultParserFactory()
    parser = factory.create(site_config, fake_session)

    assert isinstance(parser, FakeParserA)
    assert parser.site is site_config
    assert parser.session is fake_session


@pytest.mark.asyncio
async def test_create_unknown_raises(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """create() raises ParserNotFoundError for unknown site."""
    factory = DefaultParserFactory()

    with pytest.raises(ParserNotFoundError, match="no_such_site"):
        factory.create(site_config.model_copy(update={"id": "no_such_site"}), fake_session)


@pytest.mark.asyncio
async def test_create_with_mixin_parser(
    site_config: SiteConfig,
    fake_session: MagicMock,
) -> None:
    """create() works with a parser that uses a mixin."""

    class FakeMixin:
        def helper(self) -> str:
            return "mixin"

    class MixedParser(FakeMixin, BaseParser):
        site_id = "test"
        language = "en"
        adult = False

        async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
            return []

        async def get_manga(self, url_or_id: str) -> Manga:
            raise NotImplementedError

        async def get_chapters(self, manga: Manga) -> list[Chapter]:
            return []

        async def get_pages(self, chapter: Chapter) -> list[Page]:
            return []

    register("test", MixedParser)
    factory = DefaultParserFactory()
    parser = factory.create(site_config, fake_session)

    assert isinstance(parser, MixedParser)
    assert isinstance(parser, FakeMixin)
    assert parser.helper() == "mixin"


# ────────────────────────────────────────────────────────────────────
# 3. Protocol compliance
# ────────────────────────────────────────────────────────────────────


def test_default_factory_implements_protocol() -> None:
    """DefaultParserFactory satisfies the ParserFactory Protocol."""
    from nexusdl.core.registry.protocols import ParserFactory

    factory = DefaultParserFactory()
    assert isinstance(factory, ParserFactory)


def test_protocol_has_create() -> None:
    """ParserFactory Protocol has a create() method."""
    from nexusdl.core.registry.protocols import ParserFactory

    assert hasattr(ParserFactory, "create")


# ────────────────────────────────────────────────────────────────────
# 4. Isolation and state
# ────────────────────────────────────────────────────────────────────


def test_reset_registry_clears_all() -> None:
    """reset_registry() clears all parsers."""
    register("site_a", FakeParserA)
    register("site_b", FakeParserB)
    assert len(list_registered_parsers()) == 2

    reset_registry()
    assert len(list_registered_parsers()) == 0


def test_list_registered_returns_copy() -> None:
    """list_registered_parsers() returns a copy (not the internal dict)."""
    register("site_a", FakeParserA)

    parsers = list_registered_parsers()
    parsers["site_x"] = FakeParserB  # mutate the copy

    # Internal state should be unchanged
    assert "site_x" not in list_registered_parsers()


def test_get_registered_parser() -> None:
    """get_registered_parser() returns the class or None."""
    register("site_a", FakeParserA)
    assert get_registered_parser("site_a") is FakeParserA
    assert get_registered_parser("unknown") is None


# ────────────────────────────────────────────────────────────────────
# 5. Concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_registration() -> None:
    """Concurrent register() calls don't crash."""

    async def _register(i: int) -> None:
        # Simulate a bit of async work before registering
        await asyncio.sleep(0)
        register(f"site_{i}", FakeParserA)

    await asyncio.gather(*[_register(i) for i in range(20)])

    parsers = list_registered_parsers()
    assert len(parsers) == 20


# ────────────────────────────────────────────────────────────────────
# 6. Attribute validation
# ────────────────────────────────────────────────────────────────────


def test_register_rejects_missing_site_id() -> None:
    """register() rejects parsers without site_id."""

    class NoSiteId(BaseParser):
        site_id = ""  # empty
        language = "en"
        adult = False

        async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
            return []

        async def get_manga(self, url_or_id: str) -> Manga:
            raise NotImplementedError

        async def get_chapters(self, manga: Manga) -> list[Chapter]:
            return []

        async def get_pages(self, chapter: Chapter) -> list[Page]:
            return []

    with pytest.raises(ValueError, match="site_id"):
        register("test", NoSiteId)


def test_register_mismatched_site_id() -> None:
    """register() uses the registration key, not the class attribute.

    This allows aliasing (registering the same parser under multiple IDs),
    but it's the caller's responsibility to keep them in sync.
    """
    register("alias", FakeParserA)
    assert get_registered_parser("alias") is FakeParserA
    # Note: FakeParserA.site_id is "site_a", not "alias".
    # This is intentional (some parsers share code via aliases).
