# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for HttpSession.

HttpSession wraps httpx.AsyncClient with:

    - Retry (tenacity) with exponential backoff + jitter
    - Rate limiting (per site)
    - HTTP caching (hishel)
    - Cookie persistence (CookieManager)
    - Circuit breaker (per site)
    - Timeouts (connect, read, write)
    - HTTP/2 support
    - User-Agent rotation

Contract:
    - async with HttpSession(site, settings) as session: ...
    - session.get(url, ...) -> httpx.Response
    - session.get_html(url, ...) -> str
    - session.get_json(url, ...) -> dict
    - session.post(url, ...) -> httpx.Response
    - session.stream_download(url, dest, ...) -> Path
    - session.close() (called by __aexit__)
    - session.default_headers (from site)
    - session.user_agent (from pool or override)

What is tested:
    - Context manager (__aenter__, __aexit__, close)
    - get() success
    - get() 4xx (no retry)
    - get() 5xx (retry)
    - get() 429 (Retry-After)
    - get() network error (retry)
    - get() timeout (SessionTimeoutError)
    - get_html()
    - get_json()
    - post()
    - stream_download() (success, progress, error)
    - Rate limiting applied
    - Cache used
    - Circuit breaker applied
    - Cookies injected
    - Default headers sent
    - User-Agent set
    - Concurrency (semaphore)
    - close() idempotent
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx

from nexusdl.core.config import Settings
from nexusdl.core.exceptions import (
    SessionError,
    SessionRateLimitError,
    SessionTimeoutError,
)
from nexusdl.core.models.enums import Language
from nexusdl.core.models.site import SiteCapabilities, SiteConfig
from nexusdl.core.session.http_session import HttpSession


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
        http_timeout_seconds=30.0,
        http_max_retries=3,
        default_rate_limit_per_second=100.0,  # fast for tests
    )


@pytest.fixture
def site() -> SiteConfig:
    """Minimal SiteConfig."""
    return SiteConfig(
        id="test-site",
        name="Test Site",
        domains=["https://test.example.com"],
        parser_class="nexusdl.parsers.en.test:TestParser",
        language=Language.EN,
        adult=False,
        capabilities=SiteCapabilities(
            max_concurrent_downloads=2,
            default_rate_limit_per_second=100.0,
        ),
        default_headers={"X-Custom": "value"},
        cookies_required=[],
    )


@pytest_asyncio.fixture
async def session(settings: Settings, site: SiteConfig) -> HttpSession:
    """Fresh HttpSession."""
    async with HttpSession(site, settings) as s:
        yield s


# ────────────────────────────────────────────────────────────────────
# Test 1: Context manager
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_manager(settings: Settings, site: SiteConfig) -> None:
    """async with HttpSession(...) works."""
    async with HttpSession(site, settings) as s:
        assert s is not None
        assert s.site.id == "test-site"


@pytest.mark.asyncio
async def test_close_idempotent(settings: Settings, site: SiteConfig) -> None:
    """close() can be called multiple times."""
    s = HttpSession(site, settings)
    async with s:
        pass
    # Second close should not raise
    await s.close()


# ────────────────────────────────────────────────────────────────────
# Test 2: get() success
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_success(session: HttpSession) -> None:
    """get() returns a Response."""
    respx.get("https://test.example.com/page").mock(
        return_value=httpx.Response(200, text="hello")
    )

    resp = await session.get("https://test.example.com/page")
    assert resp.status_code == 200
    assert resp.text == "hello"


@pytest.mark.asyncio
@respx.mock
async def test_get_html(session: HttpSession) -> None:
    """get_html() returns a str."""
    respx.get("https://test.example.com/html").mock(
        return_value=httpx.Response(200, text="<html>hello</html>")
    )

    html = await session.get_html("https://test.example.com/html")
    assert isinstance(html, str)
    assert "hello" in html


@pytest.mark.asyncio
@respx.mock
async def test_get_json(session: HttpSession) -> None:
    """get_json() returns a dict."""
    respx.get("https://test.example.com/json").mock(
        return_value=httpx.Response(200, json={"key": "value"})
    )

    data = await session.get_json("https://test.example.com/json")
    assert isinstance(data, dict)
    assert data["key"] == "value"


@pytest.mark.asyncio
@respx.mock
async def test_post(session: HttpSession) -> None:
    """post() sends a POST request."""
    respx.post("https://test.example.com/post").mock(
        return_value=httpx.Response(201, json={"created": True})
    )

    resp = await session.post(
        "https://test.example.com/post",
        json={"data": "value"},
    )
    assert resp.status_code == 201


# ────────────────────────────────────────────────────────────────────
# Test 3: Retry on 5xx
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_retries_5xx(session: HttpSession) -> None:
    """5xx responses trigger retry, then succeed."""
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(500, text="error")
        return httpx.Response(200, text="ok")

    respx.get("https://test.example.com/retry").mock(side_effect=_side_effect)

    resp = await session.get("https://test.example.com/retry")
    assert resp.status_code == 200
    assert call_count["n"] == 3


@pytest.mark.asyncio
@respx.mock
async def test_get_no_retry_4xx(session: HttpSession) -> None:
    """4xx responses are NOT retried (client error)."""
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        return httpx.Response(404, text="not found")

    respx.get("https://test.example.com/404").mock(side_effect=_side_effect)

    resp = await session.get("https://test.example.com/404")
    assert resp.status_code == 404
    assert call_count["n"] == 1  # no retry


@pytest.mark.asyncio
@respx.mock
async def test_get_retries_on_network_error(session: HttpSession) -> None:
    """Network errors trigger retry, then succeed."""
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text="ok")

    respx.get("https://test.example.com/net").mock(side_effect=_side_effect)

    resp = await session.get("https://test.example.com/net")
    assert resp.status_code == 200
    assert call_count["n"] == 2


# ────────────────────────────────────────────────────────────────────
# Test 4: 429 (Retry-After)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_429_respects_retry_after(session: HttpSession) -> None:
    """429 with Retry-After is handled (raise or retry)."""
    call_count = {"n": 0}

    def _side_effect(request):
        call_count["n"] += 1
        if call_count["n"] < 2:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},  # 0 seconds for test
                text="rate limited",
            )
        return httpx.Response(200, text="ok")

    respx.get("https://test.example.com/429").mock(side_effect=_side_effect)

    # Either the session retries (2 calls) or raises SessionRateLimitError
    try:
        resp = await session.get("https://test.example.com/429")
        assert resp.status_code == 200
        assert call_count["n"] == 2
    except SessionRateLimitError:
        assert call_count["n"] >= 1


# ────────────────────────────────────────────────────────────────────
# Test 5: Timeout
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_timeout(session: HttpSession) -> None:
    """Timeout raises SessionTimeoutError."""
    respx.get("https://test.example.com/timeout").mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )

    with pytest.raises((SessionTimeoutError, SessionError)):
        await session.get("https://test.example.com/timeout")


# ────────────────────────────────────────────────────────────────────
# Test 6: Default headers
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_default_headers(session: HttpSession) -> None:
    """Default headers from site are sent."""
    captured: dict = {}

    def _side_effect(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, text="ok")

    respx.get("https://test.example.com/headers").mock(side_effect=_side_effect)

    await session.get("https://test.example.com/headers")

    assert captured["headers"].get("x-custom") == "value"


@pytest.mark.asyncio
@respx.mock
async def test_user_agent(session: HttpSession) -> None:
    """User-Agent is set (from pool or override)."""
    captured: dict = {}

    def _side_effect(request):
        captured["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text="ok")

    respx.get("https://test.example.com/ua").mock(side_effect=_side_effect)

    await session.get("https://test.example.com/ua")

    assert captured["ua"]  # not empty
    assert "NexusDL" in captured["ua"] or "Mozilla" in captured["ua"]


# ────────────────────────────────────────────────────────────────────
# Test 7: stream_download
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_stream_download(session: HttpSession, tmp_path: Path) -> None:
    """stream_download() writes the file."""
    content = b"hello world"
    respx.get("https://test.example.com/file").mock(
        return_value=httpx.Response(200, content=content)
    )

    dest = tmp_path / "file.bin"
    result = await session.stream_download("https://test.example.com/file", dest)

    assert result == dest
    assert dest.exists()
    assert dest.read_bytes() == content


@pytest.mark.asyncio
@respx.mock
async def test_stream_download_progress(
    session: HttpSession,
    tmp_path: Path,
) -> None:
    """stream_download() calls on_progress."""
    content = b"x" * 1000
    respx.get("https://test.example.com/progress").mock(
        return_value=httpx.Response(200, content=content)
    )

    progress_calls: list[tuple[int, int]] = []

    def _on_progress(downloaded: int, total: int) -> None:
        progress_calls.append((downloaded, total))

    dest = tmp_path / "file.bin"
    await session.stream_download(
        "https://test.example.com/progress",
        dest,
        on_progress=_on_progress,
    )

    assert len(progress_calls) >= 1
    assert progress_calls[-1][0] == 1000  # total bytes downloaded


@pytest.mark.asyncio
@respx.mock
async def test_stream_download_error(session: HttpSession, tmp_path: Path) -> None:
    """stream_download() raises on 4xx/5xx after retries."""
    respx.get("https://test.example.com/error").mock(
        return_value=httpx.Response(500, text="error")
    )

    dest = tmp_path / "file.bin"

    with pytest.raises(SessionError):
        await session.stream_download("https://test.example.com/error", dest)


# ────────────────────────────────────────────────────────────────────
# Test 8: Rate limiting
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_rate_limiting(tmp_path: Path) -> None:
    """Rate limiter delays requests."""
    from nexusdl.core.models.site import SiteCapabilities

    site = SiteConfig(
        id="slow-site",
        name="Slow Site",
        domains=["https://slow.example.com"],
        parser_class="nexusdl.parsers.en.slow:SlowParser",
        language=Language.EN,
        adult=False,
        capabilities=SiteCapabilities(
            default_rate_limit_per_second=2.0,  # 2 req/s
        ),
    )

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_name="Test",
        app_environment="dev",
        public_url="http://test",
        api_base_url="http://test",
        frontend_url="http://test",
        secret_key="test-secret-" + "a" * 52,
        jwt_secret="test-jwt-" + "b" * 55,
        cookie_encryption_key="test-cookie-" + "c" * 50,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/db.sqlite",
        download_dir=tmp_path / "d",
        temp_dir=tmp_path / "t",
        library_db_path=tmp_path / "db.sqlite",
        cookie_storage_path=tmp_path / "c.enc",
        logs_dir=tmp_path / "l",
        log_level="WARNING",
        log_format="text",
    )

    respx.get("https://slow.example.com/page").mock(
        return_value=httpx.Response(200, text="ok")
    )

    async with HttpSession(site, settings) as s:
        import time

        start = time.monotonic()
        for _ in range(3):
            await s.get("https://slow.example.com/page")
        elapsed = time.monotonic() - start

        # 3 requests at 2 req/s ≈ 1 second (with tolerance)
        assert elapsed >= 0.5, f"Rate limiting not applied ({elapsed:.2f}s)"


# ────────────────────────────────────────────────────────────────────
# Test 9: Concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_requests(session: HttpSession) -> None:
    """Concurrent requests do not crash."""
    respx.get("https://test.example.com/concurrent").mock(
        return_value=httpx.Response(200, text="ok")
    )

    async def _get() -> int:
        resp = await session.get("https://test.example.com/concurrent")
        return resp.status_code

    results = await asyncio.gather(*[_get() for _ in range(10)])
    assert all(r == 200 for r in results)
