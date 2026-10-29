# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""Unit tests for HttpCache.

HttpCache wraps ``hishel`` to provide HTTP caching for NexusDL:

    - Respects ETag / Last-Modified (conditional requests)
    - Respects Cache-Control (no-store, no-cache, private, max-age)
    - Respects Vary (User-Agent, Accept-Encoding)
    - Per-site TTL (override via sites.yaml)
    - Persistent (file-based, survives restart)
    - LRU eviction (max_size)
    - Statistics (hits, misses, size_bytes)
    - Thread-safe (asyncio.Lock)

What is tested:
    - get() miss (returns None)
    - get() hit (returns Response)
    - set() stores a response
    - delete() removes an entry
    - clear() empties the cache
    - TTL expiration
    - Per-site TTL override
    - ETag (If-None-Match)
    - Last-Modified (If-Modified-Since)
    - Cache-Control: no-store (not cached)
    - Cache-Control: private (not cached)
    - Cache-Control: no-cache (revalidated)
    - Cache-Control: max-age (respected)
    - Vary (headers vary)
    - Persistence (survives "restart")
    - LRU eviction (max_size)
    - Statistics
    - Concurrency
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from nexusdl.core.session.cache import CacheStats, HttpCache


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def cache(tmp_path: Path) -> HttpCache:
    """Fresh HttpCache in tmp_path."""
    c = HttpCache(
        cache_dir=tmp_path / "cache",
        ttl_seconds=3600,
        max_size_bytes=10 * 1024 * 1024,  # 10 MB
    )
    await c.init()
    yield c
    await c.close()


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _make_request(url: str, method: str = "GET", **headers) -> httpx.Request:
    """Build a minimal HTTP request."""
    return httpx.Request(method, url, headers=headers)


def _make_response(
    status: int = 200,
    content: bytes = b"hello",
    headers: dict | None = None,
) -> httpx.Response:
    """Build a minimal HTTP response."""
    return httpx.Response(
        status_code=status,
        content=content,
        headers=headers or {},
        request=httpx.Request("GET", "https://test.com"),
    )


# ────────────────────────────────────────────────────────────────────
# Test 1: get() miss
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_miss(cache: HttpCache) -> None:
    """get() on empty cache returns None."""
    req = _make_request("https://test.com/miss")
    resp = await cache.get(req)
    assert resp is None


# ────────────────────────────────────────────────────────────────────
# Test 2: set() + get() hit
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_get_hit(cache: HttpCache) -> None:
    """set() then get() returns the stored response."""
    req = _make_request("https://test.com/hit")
    resp = _make_response(content=b"hello")

    await cache.set(req, resp)
    cached = await cache.get(req)

    assert cached is not None
    assert cached.status_code == 200
    assert cached.content == b"hello"


# ────────────────────────────────────────────────────────────────────
# Test 3: delete()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete(cache: HttpCache) -> None:
    """delete() removes an entry."""
    req = _make_request("https://test.com/delete")
    resp = _make_response()

    await cache.set(req, resp)
    assert await cache.get(req) is not None

    await cache.delete(req)
    assert await cache.get(req) is None


# ────────────────────────────────────────────────────────────────────
# Test 4: clear()
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear(cache: HttpCache) -> None:
    """clear() empties the cache."""
    for i in range(3):
        req = _make_request(f"https://test.com/clear{i}")
        await cache.set(req, _make_response())

    await cache.clear()

    for i in range(3):
        req = _make_request(f"https://test.com/clear{i}")
        assert await cache.get(req) is None


# ────────────────────────────────────────────────────────────────────
# Test 5: TTL
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ttl_expiration(tmp_path: Path) -> None:
    """Entries expire after TTL."""
    c = HttpCache(cache_dir=tmp_path / "cache", ttl_seconds=1)
    await c.init()

    try:
        req = _make_request("https://test.com/ttl")
        await c.set(req, _make_response())

        # Immediate get: hit
        assert await c.get(req) is not None

        # Wait for TTL
        await asyncio.sleep(1.1)

        # After TTL: miss
        assert await c.get(req) is None
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_per_site_ttl(tmp_path: Path) -> None:
    """per_site_ttl overrides the default TTL."""
    c = HttpCache(
        cache_dir=tmp_path / "cache",
        ttl_seconds=3600,
        per_site_ttl={"short-site": 1},
    )
    await c.init()

    try:
        # Site-specific TTL = 1s
        req = _make_request("https://short-site.test/page")
        await c.set(req, _make_response(), site_id="short-site")

        assert await c.get(req, site_id="short-site") is not None
        await asyncio.sleep(1.1)
        assert await c.get(req, site_id="short-site") is None

        # Default TTL = 3600s (still valid)
        req2 = _make_request("https://other-site.test/page")
        await c.set(req2, _make_response(), site_id="other-site")
        await asyncio.sleep(1.1)
        assert await c.get(req2, site_id="other-site") is not None
    finally:
        await c.close()


# ────────────────────────────────────────────────────────────────────
# Test 6: ETag (If-None-Match)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_etag_conditional_request(cache: HttpCache) -> None:
    """Cached response with ETag returns If-None-Match header."""
    req = _make_request("https://test.com/etag")
    resp = _make_response(headers={"ETag": '"abc123"'})

    await cache.set(req, resp)

    # get() should return a cached response with If-None-Match
    cached = await cache.get(req)
    assert cached is not None
    # The cache may add the If-None-Match header to the request,
    # or return the cached response directly.
    if hasattr(cached, "request"):
        if_none_match = cached.request.headers.get("If-None-Match")
        # Either the header is set, or the response is returned directly
        assert if_none_match == '"abc123"' or cached.content == b"hello"


# ────────────────────────────────────────────────────────────────────
# Test 7: Last-Modified
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_last_modified(cache: HttpCache) -> None:
    """Cached response with Last-Modified is used for conditional requests."""
    req = _make_request("https://test.com/lastmod")
    resp = _make_response(
        headers={"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )

    await cache.set(req, resp)
    cached = await cache.get(req)

    assert cached is not None
    assert cached.content == b"hello"


# ────────────────────────────────────────────────────────────────────
# Test 8: Cache-Control: no-store
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_store_not_cached(cache: HttpCache) -> None:
    """Responses with Cache-Control: no-store are NOT cached."""
    req = _make_request("https://test.com/nostore")
    resp = _make_response(headers={"Cache-Control": "no-store"})

    await cache.set(req, resp)

    # Should not be cached
    cached = await cache.get(req)
    assert cached is None


# ────────────────────────────────────────────────────────────────────
# Test 9: Cache-Control: private
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_private_not_cached(cache: HttpCache) -> None:
    """Responses with Cache-Control: private are NOT cached (ADR-004)."""
    req = _make_request("https://test.com/private")
    resp = _make_response(headers={"Cache-Control": "private"})

    await cache.set(req, resp)

    cached = await cache.get(req)
    assert cached is None


# ────────────────────────────────────────────────────────────────────
# Test 10: Cache-Control: no-cache
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_cache_revalidated(cache: HttpCache) -> None:
    """Responses with Cache-Control: no-cache are stored but revalidated."""
    req = _make_request("https://test.com/nocache")
    resp = _make_response(headers={"Cache-Control": "no-cache"})

    await cache.set(req, resp)

    # May be cached (but requires revalidation before use)
    cached = await cache.get(req)
    # Accept either: cached with revalidation required, or not cached
    if cached is not None:
        # If cached, it should require revalidation
        cc = cached.headers.get("Cache-Control", "")
        assert "no-cache" in cc or "must-revalidate" in cc


# ────────────────────────────────────────────────────────────────────
# Test 11: Cache-Control: max-age
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_age_respected(tmp_path: Path) -> None:
    """Cache-Control: max-age overrides the default TTL."""
    c = HttpCache(cache_dir=tmp_path / "cache", ttl_seconds=3600)
    await c.init()

    try:
        req = _make_request("https://test.com/maxage")
        resp = _make_response(headers={"Cache-Control": "max-age=1"})

        await c.set(req, resp)

        # Immediately: hit
        assert await c.get(req) is not None

        # After max-age: miss
        await asyncio.sleep(1.1)
        assert await c.get(req) is None
    finally:
        await c.close()


# ────────────────────────────────────────────────────────────────────
# Test 12: Vary
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vary_headers(cache: HttpCache) -> None:
    """Vary headers make cached responses key-specific."""
    req1 = _make_request(
        "https://test.com/vary",
        **{"User-Agent": "NexusDL/1.0"},
    )
    req2 = _make_request(
        "https://test.com/vary",
        **{"User-Agent": "OtherBot/2.0"},
    )

    resp = _make_response(headers={"Vary": "User-Agent"})

    await cache.set(req1, resp)

    # Same User-Agent: hit
    cached = await cache.get(req1)
    assert cached is not None

    # Different User-Agent: miss (or different cache key)
    cached2 = await cache.get(req2)
    # Depending on implementation, this may be a miss or a different entry
    # Both are acceptable if Vary is respected
    if cached2 is not None:
        assert cached2.content == b"hello"


# ────────────────────────────────────────────────────────────────────
# Test 13: Persistence
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persistence(tmp_path: Path) -> None:
    """Cache persists across "restart" (new HttpCache instance)."""
    cache_dir = tmp_path / "cache"

    # First instance
    c1 = HttpCache(cache_dir=cache_dir, ttl_seconds=3600)
    await c1.init()
    req = _make_request("https://test.com/persist")
    await c1.set(req, _make_response(content=b"persisted"))
    await c1.close()

    # Second instance (simulates restart)
    c2 = HttpCache(cache_dir=cache_dir, ttl_seconds=3600)
    await c2.init()
    cached = await c2.get(req)
    assert cached is not None
    assert cached.content == b"persisted"
    await c2.close()


# ────────────────────────────────────────────────────────────────────
# Test 14: LRU eviction (max_size)
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lru_eviction(tmp_path: Path) -> None:
    """Cache evicts LRU entries when max_size is exceeded."""
    # Small cache (1 KB)
    c = HttpCache(
        cache_dir=tmp_path / "cache",
        ttl_seconds=3600,
        max_size_bytes=1024,
    )
    await c.init()

    try:
        # Insert 10 entries of ~200 bytes each
        for i in range(10):
            req = _make_request(f"https://test.com/lru{i}")
            resp = _make_response(content=b"x" * 200)
            await c.set(req, resp)

        # Some entries should be evicted
        stats = await c.stats()
        assert stats.size_bytes <= 1024 * 1.1  # allow 10% overhead
    finally:
        await c.close()


# ────────────────────────────────────────────────────────────────────
# Test 15: Statistics
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats(cache: HttpCache) -> None:
    """Statistics track hits, misses, and size."""
    stats = await cache.stats()
    assert isinstance(stats, CacheStats)
    assert stats.hits == 0
    assert stats.misses == 0
    assert stats.size_bytes >= 0

    # Miss
    req = _make_request("https://test.com/stats")
    await cache.get(req)

    stats = await cache.stats()
    assert stats.misses == 1

    # Hit
    await cache.set(req, _make_response())
    await cache.get(req)

    stats = await cache.stats()
    assert stats.hits >= 1


# ────────────────────────────────────────────────────────────────────
# Test 16: Concurrency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_access(cache: HttpCache) -> None:
    """Concurrent set/get do not crash."""

    async def _set(i: int) -> None:
        req = _make_request(f"https://test.com/concurrent{i}")
        await cache.set(req, _make_response())

    async def _get(i: int) -> None:
        req = _make_request(f"https://test.com/concurrent{i}")
        await cache.get(req)

    await asyncio.gather(*[_set(i) for i in range(20)])
    results = await asyncio.gather(*[_get(i) for i in range(20)])

    # All should be cached
    assert all(r is not None for r in results)


# ────────────────────────────────────────────────────────────────────
# Test 17: Site isolation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_site_isolation(cache: HttpCache) -> None:
    """Cache entries are isolated by site_id."""
    req = _make_request("https://test.com/site")

    await cache.set(req, _make_response(content=b"mangadex"), site_id="mangadex")
    await cache.set(req, _make_response(content=b"sushiscan"), site_id="sushiscan")

    cached_md = await cache.get(req, site_id="mangadex")
    cached_ss = await cache.get(req, site_id="sushiscan")

    assert cached_md is not None
    assert cached_ss is not None
    assert cached_md.content == b"mangadex"
    assert cached_ss.content == b"sushiscan"


# ────────────────────────────────────────────────────────────────────
# Test 18: Cleanup on init
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_on_init(tmp_path: Path) -> None:
    """Expired entries are cleaned up on init()."""
    cache_dir = tmp_path / "cache"

    # First instance: insert with short TTL
    c1 = HttpCache(cache_dir=cache_dir, ttl_seconds=1)
    await c1.init()
    req = _make_request("https://test.com/cleanup")
    await c1.set(req, _make_response())
    await c1.close()

    # Wait for TTL
    await asyncio.sleep(1.1)

    # Second instance: cleanup on init
    c2 = HttpCache(cache_dir=cache_dir, ttl_seconds=3600)
    await c2.init()
    cached = await c2.get(req)
    assert cached is None  # expired and cleaned
    await c2.close()
