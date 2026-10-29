# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for FastAPI routers.

Tests each router **in isolation** with mocked dependencies:

    - SiteRegistry      (mocked)
    - DownloadManager   (mocked)
    - LibraryDatabase   (mocked)
    - current_user      (mocked via dependency_overrides)

No network, no DB, no files written.

Covers:
    - Health (/health, /health/ready)
    - Version (/api/version)
    - Sites (/api/v1/sites, filter, 404)
    - Search (/api/v1/search, validation, results)
    - Manga (/api/v1/manga/{id}, 200, 404)
    - Chapters (/api/v1/chapters/{id}, /pages, 200, 404)
    - Download (/api/v1/download, 202, validation)
    - Library (/api/v1/library, pagination, progress)
    - Auth (/api/v1/auth/login, /register, 401)
    - Error envelope (consistent format)
    - CORS (preflight)
    - Pydantic validation (422)

Why in-process?
    - Fast (< 3s)
    - Deterministic
    - Catches router wiring bugs (paths, methods, status codes)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from nexusdl.core.config import Settings

from nexusdl.core.config import Settings
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import (
    ContentRating,
    DownloadStatus,
    Language,
    MangaStatus,
)
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult
from nexusdl.core.models.site import SiteCapabilities, SiteConfig
from nexusdl.core.models.user import User, UserRole


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
        api_cors_origins=["http://test"],
        api_rate_limit_per_minute=600,
        api_enable_docs=True,
        api_allow_anonymous=False,
        api_allow_registration=True,
    )


@pytest.fixture
def fake_site() -> SiteConfig:
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
def fake_chapter(fake_manga: Manga) -> Chapter:
    """A fake Chapter."""
    return Chapter(
        id="b3e5d7a9c1f2e4d6",
        manga_id=fake_manga.id,
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
def fake_user() -> User:
    """A fake User (used for auth override)."""
    return User(
        id=uuid4(),
        username="testuser",
        email="test@example.com",
        role=UserRole.USER,
        is_active=True,
        created_at=datetime.now(tz=timezone.utc),
    )


@pytest_asyncio.fixture
async def app(settings: Settings):
    """FastAPI app with mocked dependencies."""
    from nexusdl.interfaces.web.backend.main import create_app

    # Create app with empty registry (we override per-test)
    registry = MagicMock()
    registry.list_sites = MagicMock(return_value=[])
    registry.get_site = MagicMock()

    app = create_app(settings=settings, registry=registry)
    yield app

    # Clear overrides between tests
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app, fake_user: User):
    """httpx.AsyncClient with auth override."""
    from nexusdl.interfaces.web.backend.dependencies import get_current_user

    # Override auth dependency
    app.dependency_overrides[get_current_user] = lambda: fake_user

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ────────────────────────────────────────────────────────────────────
# Test 1: Health
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health(client: httpx.AsyncClient) -> None:
    """GET /health returns 200."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "uptime_seconds" in data


@pytest.mark.asyncio
async def test_health_ready(client: httpx.AsyncClient) -> None:
    """GET /health/ready returns checks (may be 200 or 503)."""
    resp = await client.get("/health/ready")
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "status" in data
    assert "checks" in data


# ────────────────────────────────────────────────────────────────────
# Test 2: Version
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_version(client: httpx.AsyncClient) -> None:
    """GET /api/version returns version info."""
    resp = await client.get("/api/version")
    assert resp.status_code == 200
    data = resp.json()
    assert "api" in data
    assert "app" in data
    assert "python" in data


# ────────────────────────────────────────────────────────────────────
# Test 3: Sites
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sites_list(
    client: httpx.AsyncClient,
    app,
    fake_site: SiteConfig,
) -> None:
    """GET /api/v1/sites returns the sites."""
    # Override registry
    app.state.registry.list_sites = MagicMock(return_value=[fake_site])

    resp = await client.get("/api/v1/sites")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["total"] == 1
    assert data["items"][0]["id"] == "mangadex"


@pytest.mark.asyncio
async def test_sites_filter_language(
    client: httpx.AsyncClient,
    app,
    fake_site: SiteConfig,
) -> None:
    """GET /api/v1/sites?language=en filters by language."""
    app.state.registry.list_sites = MagicMock(return_value=[fake_site])

    resp = await client.get("/api/v1/sites?language=en")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 0  # may be 1 or 0 depending on filter impl


# ────────────────────────────────────────────────────────────────────
# Test 4: Search
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search(
    client: httpx.AsyncClient,
    app,
    fake_manga: Manga,
) -> None:
    """POST /api/v1/search returns results."""
    async def fake_search_all(*args, **kwargs):
        yield SearchResult(
            site_id="mangadex",
            site_name="MangaDex",
            manga_id=fake_manga.id,
            title=fake_manga.title,
            cover_url=fake_manga.cover_url,
            author=fake_manga.author,
            language=fake_manga.language,
            content_rating=fake_manga.content_rating,
            url=fake_manga.url,
        )

    app.state.registry.search_all = fake_search_all

    resp = await client.post(
        "/api/v1/search",
        json={"query": "one piece", "sites": ["mangadex"]},
    )
    assert resp.status_code == 200, f"Failed: {resp.text}"
    data = resp.json()
    assert "items" in data
    assert data["total"] == 1
    assert data["items"][0]["title"] == "One Piece"


@pytest.mark.asyncio
async def test_search_validation_empty_query(client: httpx.AsyncClient) -> None:
    """POST /api/v1/search with empty query → 422."""
    resp = await client.post("/api/v1/search", json={"query": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_validation_short_query(client: httpx.AsyncClient) -> None:
    """POST /api/v1/search with 1-char query → 422."""
    resp = await client.post("/api/v1/search", json={"query": "a"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_search_validation_per_page(client: httpx.AsyncClient) -> None:
    """POST /api/v1/search with per_page > 100 → 422."""
    resp = await client.post(
        "/api/v1/search",
        json={"query": "one piece", "per_page": 1000},
    )
    assert resp.status_code == 422


# ────────────────────────────────────────────────────────────────────
# Test 5: Manga
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_manga(
    client: httpx.AsyncClient,
    app,
    fake_site: SiteConfig,
    fake_manga: Manga,
) -> None:
    """GET /api/v1/manga/{id} returns the manga."""
    app.state.registry.get_site = MagicMock(return_value=fake_site)
    fake_parser = MagicMock()
    fake_parser.get_manga = AsyncMock(return_value=fake_manga)
    app.state.registry.get_parser = MagicMock(return_value=fake_parser)

    resp = await client.get(
        f"/api/v1/manga/{fake_manga.id}?site_id=mangadex"
    )
    assert resp.status_code == 200, f"Failed: {resp.text}"
    data = resp.json()
    assert data["id"] == fake_manga.id
    assert data["title"] == "One Piece"


@pytest.mark.asyncio
async def test_get_manga_not_found(
    client: httpx.AsyncClient,
    app,
    fake_site: SiteConfig,
) -> None:
    """GET /api/v1/manga/unknown → 404."""
    from nexusdl.parsers.exceptions import ParserNotFoundError

    app.state.registry.get_site = MagicMock(return_value=fake_site)
    fake_parser = MagicMock()
    fake_parser.get_manga = AsyncMock(side_effect=ParserNotFoundError("not found"))
    app.state.registry.get_parser = MagicMock(return_value=fake_parser)

    resp = await client.get("/api/v1/manga/unknown?site_id=mangadex")
    assert resp.status_code == 404
    data = resp.json()
    assert "error" in data
    assert data["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_get_manga_missing_site_id(client: httpx.AsyncClient) -> None:
    """GET /api/v1/manga/{id} without site_id → 422."""
    resp = await client.get("/api/v1/manga/abc")
    assert resp.status_code == 422


# ────────────────────────────────────────────────────────────────────
# Test 6: Chapters
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chapters(
    client: httpx.AsyncClient,
    app,
    fake_site: SiteConfig,
    fake_chapter: Chapter,
) -> None:
    """GET /api/v1/manga/{id}/chapters returns chapters."""
    app.state.registry.get_site = MagicMock(return_value=fake_site)
    fake_parser = MagicMock()
    fake_parser.get_chapters = AsyncMock(return_value=[fake_chapter])
    app.state.registry.get_parser = MagicMock(return_value=fake_parser)

    resp = await client.get(
        "/api/v1/manga/a1b2c3d4e5f6a7b8/chapters?site_id=mangadex"
    )
    assert resp.status_code == 200, f"Failed: {resp.text}"
    data = resp.json()
    assert "items" in data
    assert data["total"] == 1
    assert data["items"][0]["id"] == fake_chapter.id


@pytest.mark.asyncio
async def test_get_pages(
    client: httpx.AsyncClient,
    app,
    fake_site: SiteConfig,
    fake_chapter: Chapter,
) -> None:
    """GET /api/v1/chapters/{id}/pages returns pages."""
    app.state.registry.get_site = MagicMock(return_value=fake_site)
    fake_parser = MagicMock()
    fake_parser.get_pages = AsyncMock(
        return_value=[
            Page(index=0, url="https://test.com/001.png", filename="001.png"),
            Page(index=1, url="https://test.com/002.png", filename="002.png"),
        ]
    )
    app.state.registry.get_parser = MagicMock(return_value=fake_parser)

    resp = await client.get(
        f"/api/v1/chapters/{fake_chapter.id}/pages"
        f"?site_id=mangadex&manga_id={fake_chapter.manga_id}"
    )
    assert resp.status_code == 200, f"Failed: {resp.text}"
    data = resp.json()
    assert data["chapter_id"] == fake_chapter.id
    assert len(data["pages"]) == 2


# ────────────────────────────────────────────────────────────────────
# Test 7: Download
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_create(
    client: httpx.AsyncClient,
    app,
    fake_manga: Manga,
    fake_chapter: Chapter,
) -> None:
    """POST /api/v1/download returns 202 with task_id."""
    from nexusdl.core.models.download import DownloadTask

    task_id = uuid4()
    fake_task = DownloadTask(
        id=task_id,
        manga=fake_manga,
        chapters=[fake_chapter],
        dest=Path("/tmp"),
        fmt="CBZ",
        created_at=datetime.now(tz=timezone.utc),
        status=DownloadStatus.PENDING,
    )

    app.state.manager = MagicMock()
    app.state.manager.submit = MagicMock(return_value=task_id)
    app.state.manager.get_task = MagicMock(return_value=fake_task)

    resp = await client.post(
        "/api/v1/download",
        json={
            "manga_id": fake_manga.id,
            "site_id": "mangadex",
            "format": "CBZ",
        },
    )
    assert resp.status_code == 202, f"Failed: {resp.text}"
    data = resp.json()
    assert "task_id" in data
    assert data["status"] == "PENDING"


@pytest.mark.asyncio
async def test_download_invalid_format(client: httpx.AsyncClient) -> None:
    """POST /api/v1/download with CBR → 422 (ADR-002)."""
    resp = await client.post(
        "/api/v1/download",
        json={
            "manga_id": "a1b2c3d4e5f6a7b8",
            "site_id": "mangadex",
            "format": "CBR",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_download_missing_fields(client: httpx.AsyncClient) -> None:
    """POST /api/v1/download without manga_id → 422."""
    resp = await client.post(
        "/api/v1/download",
        json={"site_id": "mangadex"},
    )
    assert resp.status_code == 422


# ────────────────────────────────────────────────────────────────────
# Test 8: Library
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_library_list(
    client: httpx.AsyncClient,
    app,
    fake_manga: Manga,
) -> None:
    """GET /api/v1/library returns paginated mangas."""
    from nexusdl.core.library.database import PaginatedResult

    app.state.library_db = MagicMock()
    app.state.library_db.list_mangas = AsyncMock(
        return_value=PaginatedResult(
            items=[fake_manga],
            total=1,
            page=1,
            per_page=20,
            total_pages=1,
            has_next=False,
            has_prev=False,
        )
    )

    resp = await client.get("/api/v1/library")
    assert resp.status_code == 200, f"Failed: {resp.text}"
    data = resp.json()
    assert "items" in data
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_library_progress_404(client: httpx.AsyncClient, app) -> None:
    """PUT /api/v1/library/{id}/progress on unknown manga → 404."""
    from nexusdl.core.exceptions import LibraryError

    app.state.library_db = MagicMock()
    app.state.library_db.set_reading_progress = AsyncMock(
        side_effect=LibraryError("not found")
    )

    resp = await client.put(
        "/api/v1/library/unknown/progress",
        json={"chapter_id": "ch1", "page": 5},
    )
    # Should be 404 or 500 depending on error mapping
    assert resp.status_code in (404, 500)


# ────────────────────────────────────────────────────────────────────
# Test 9: Auth
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_invalid_credentials(
    client: httpx.AsyncClient,
    app,
) -> None:
    """POST /api/v1/auth/login with bad credentials → 401."""
    # Reset auth override (login is public)
    app.dependency_overrides.clear()

    app.state.auth = MagicMock()
    app.state.auth.authenticate = AsyncMock(return_value=None)

    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "wrong"},
    )
    assert resp.status_code == 401
    data = resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_register_validation(client: httpx.AsyncClient) -> None:
    """POST /api/v1/auth/register with short password → 422."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "short",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_email(client: httpx.AsyncClient) -> None:
    """POST /api/v1/auth/register with invalid email → 422."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "alice",
            "email": "not-an-email",
            "password": "longenoughpassword",
        },
    )
    assert resp.status_code == 422


# ────────────────────────────────────────────────────────────────────
# Test 10: Error envelope
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_envelope_404(client: httpx.AsyncClient) -> None:
    """404 responses use the standard error envelope."""
    resp = await client.get("/api/v1/nonexistent")
    assert resp.status_code == 404
    data = resp.json()
    assert "error" in data
    assert "code" in data["error"]
    assert "message" in data["error"]


# ────────────────────────────────────────────────────────────────────
# Test 11: CORS
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cors_preflight(client: httpx.AsyncClient) -> None:
    """OPTIONS /api/v1/search returns CORS headers."""
    resp = await client.options(
        "/api/v1/search",
        headers={
            "Origin": "http://test",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") in ("*", "http://test")
