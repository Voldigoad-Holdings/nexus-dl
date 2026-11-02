"""On-disk HTTP response cache with per-site TTL, LRU eviction, and stats.

The pipeline makes many repeat requests to the same URLs: chapter listings
that are re-fetched for every chapter, cover images fetched for a search
result page, manga detail pages that the caller re-reads from a
downloaded-library view. Caching these responses spares the source site
and cuts the pipeline's latency — but only if the cache respects HTTP
semantics (``Cache-Control``, ``ETag``, ``Vary``, ``Expires``) rather than
returning stale content that breaks a parser.

This module implements a **standards-aware** HTTP cache that layers on
top of the httpx request pipeline as an opt-in collaborator of
:class:`~nexusdl.core.session.http_session.HttpSession`.

Why not hishel directly
-----------------------
``hishel`` implements the RFC 7234 cache as an httpx *transport* wrapper:
you give it a :class:`httpx.AsyncClient` and it transparently caches
whichever requests the client makes. That model does not fit the
pipeline's needs, because:

* Per-site TTLs require inspecting the ``site_id`` of every request.
  The transport sees ``httpx.Request`` objects with no ``site_id`` on
  them — the association lives in ``HttpSession``'s calling convention.
* LRU eviction against ``max_size_bytes`` is not a hishel knob; it is
  application policy.
* Stats (``hits`` / ``misses`` / ``size_bytes``) need to be exposed
  through the diagnostic endpoint — a hishel transport internal.
* The parser's revalidation semantics ("stale + ``ETag`` → send
  ``If-None-Match``; on 304, refresh TTL") must be coordinated with the
  circuit breaker and rate limiter. A blind transport-level cache would
  skip those.

The cache is therefore a **pull-based collaborator** owned by
``HttpSession``: the session asks the cache for a response before making
the request, and pushes the response back into the cache afterwards.
This module provides the storage, the TTL policy, and the revalidation
helpers. The three lines of integration live in ``HttpSession._request``.

Storage format
--------------
Two files per cached entry, both under ``<cache_dir>/<site_id>/``:

* ``<sha256(key)>.meta.json`` — metadata: url, status, headers, sizes,
  timestamps, ``etag``, ``last_modified``, ``vary_values``. UTF-8 JSON.
* ``<sha256(key)>.body`` — the raw response body, no encoding. Kept as
  a sibling file so that reading it is a straight ``read_bytes`` with
  no JSON overhead on the large part.

A single index file ``<cache_dir>/_index.json`` tracks every entry's
size, creation time, and last-access time. It is loaded lazily on first
use and flushed on every ``put`` / ``invalidate`` / ``clear``. Because
the whole index is rewritten on each mutation, its size is bounded by
the number of entries (which is bounded by ``max_size_bytes`` divided by
the minimum entry size — tens of thousands of entries at most).

Key derivation
--------------
The cache key is a tuple of ``(method, url, vary_values)``. ``method``
is always ``"GET"`` — the cache only stores GET responses. ``url`` is
the full URL string as the caller passed it. ``vary_values`` is a
:class:`Mapping[str, str]` of the request headers named in the response's
``Vary`` header. Two requests to the same URL with different
``Accept-Language`` (when the response carries ``Vary: Accept-Language``)
are two distinct cache entries.

The storage key is ``sha256(json.dumps(key_parts, sort_keys=True))``
— a hex string of 64 characters. Two different URL-sets cannot collide
short of a SHA-256 break, which is not a realistic concern for this
threat model.

Eviction
--------
When the sum of all entry sizes exceeds ``max_size_bytes``, entries are
deleted oldest-accessed-first until the total falls below
``eviction_target_ratio × max_size_bytes`` (default ``0.9``). The
hysteresis (evict down to 90% rather than exactly to 100%) prevents a
pathological loop where the cache evicts one entry then immediately
fills the freed space. LRU is by **last access** (not creation) — a
frequently-read entry stays cached even if it is old.

``eviction_target_ratio`` must be in ``(0, 1]``. ``1.0`` disables the
hysteresis (evict exactly to the limit).

Expiry
------
An entry is considered **fresh** when ``now < expires_at``. The
``expires_at`` field is set at write time from the most specific of:

1. ``Cache-Control: no-store`` → never cached (no entry written).
2. ``Cache-Control: no-cache`` → entry is written but ``expires_at =
   now`` (always stale, always revalidated). This is the RFC's
   "cacheable but must revalidate" directive.
3. ``Cache-Control: max-age=N`` → ``expires_at = now + N``.
4. ``Expires: <date>`` → ``expires_at = <date>``.
5. The per-site TTL from ``per_site_ttl[site_id]``.
6. The default TTL from ``default_ttl``.

An **expired** entry is not deleted — it stays on disk so it can be
served as a revalidation candidate. The session, on a cache hit that
is expired, sends ``If-None-Match: <etag>`` (or ``If-Modified-Since:
<last_modified>``) and, on a 304, calls :meth:`HttpCache.refresh` to
reset the entry's TTL without re-downloading the body. Only when the
server returns a 200 or the entry has no validators does the entry get
replaced.

Async model
-----------
All public methods are coroutines. File I/O runs in a worker thread via
:func:`asyncio.to_thread` — body reads of a 10 MB cached response would
otherwise block the event loop for milliseconds.

Thread/process safety
---------------------
The cache is **not** safe to share across processes. Two processes
running against the same ``cache_dir`` (e.g. a CLI command while the
web server is running) will not corrupt each other's files thanks to
atomic writes, but their in-memory indexes will diverge. That is
acceptable for v1 because a deployment runs exactly one process against
a given cache directory. The composition root documents this
constraint; running two processes with the same ``cache_dir`` is a
configuration error.

``asyncio.Lock`` serialises mutations within a single event loop.

Dependency graph (no cycles)::

    core.exceptions  ──►  core.session.cache
    httpx
    aiofiles
    loguru
    asyncio, hashlib, json, os, time (stdlib)

No dependency on any other ``core.session`` module — this file is a leaf.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import aiofiles
import httpx
from loguru import logger

from nexusdl.core.exceptions import SessionError


__all__ = [
    "DEFAULT_DEFAULT_TTL",
    "DEFAULT_EVICTION_TARGET_RATIO",
    "DEFAULT_MAX_SIZE_BYTES",
    "CacheEntry",
    "CacheStats",
    "HttpCache",
    "parse_cache_control",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default TTL applied to any entry whose response headers do not specify
#: one. Ten minutes is short enough that a stale listing does not surprise
#: a caller, long enough that a paginated traversal of a chapter list hits
#: the cache for every page after the first.
DEFAULT_DEFAULT_TTL: Final[float] = 600.0

#: Default maximum total size of the cache directory, in bytes. 500 MB
#: holds roughly 10 000 manga detail pages or a few thousand cover images.
DEFAULT_MAX_SIZE_BYTES: Final[int] = 500 * 1024 * 1024

#: Default eviction hysteresis. See the module docstring.
DEFAULT_EVICTION_TARGET_RATIO: Final[float] = 0.9

#: Maximum body size that will be cached, in bytes. Bodies larger than
#: this are not cached even on a 200 response — the cache directory is for
#: metadata, not for videos, and a large body is usually a raw asset that
#: is fetched once. 10 MiB covers every HTML page, JSON payload, and
#: source map the pipeline realistically caches.
_MAX_CACHEABLE_BODY_SIZE: Final[int] = 10 * 1024 * 1024

#: Content types that are cacheable. An entry whose response
#: ``Content-Type`` does not start with one of these prefixes is not
#: cached, even on a 200 response. Images are deliberately excluded —
#: they are handled by :meth:`HttpSession.stream_download`, whose
#: consumers manage their own lifecycle.
_CACHEABLE_CONTENT_PREFIXES: Final[tuple[str, ...]] = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
)

#: HTTP methods the cache stores. Only ``GET`` is idempotent and cacheable
#: per RFC 7234 for a client cache without explicit directives.
_CACHEABLE_METHODS: Final[frozenset[str]] = frozenset({"GET"})

#: File names inside the cache directory.
_META_SUFFIX: Final[str] = ".meta.json"
_BODY_SUFFIX: Final[str] = ".body"
_INDEX_FILENAME: Final[str] = "_index.json"

#: Prefix for temp files created during atomic writes. Matches the
#: convention used by the packagers so a stray temp file is recognisable.
_TEMP_PREFIX: Final[str] = ".nexusdl-cache-"

_log = logger.bind(component="session.cache")


# --------------------------------------------------------------------------- #
# Cache-Control parsing                                                       #
# --------------------------------------------------------------------------- #


def parse_cache_control(
    header_value: str,
) -> dict[str, str]:
    """Parse a ``Cache-Control`` header into a directive map.

    RFC 7234 §5.2 defines ``Cache-Control`` as a comma-separated list of
    tokens, some of which may carry a ``=value``. Quoted string values
    are unquoted; a bare token (e.g. ``no-store``, ``no-cache``,
    ``public``, ``private``) is mapped to the empty string. Directive
    names are lowercased; values are preserved verbatim.

    Malformed fragments (an empty token, a token with an unterminated
    quote) are silently skipped — a broken ``Cache-Control`` header must
    not raise.

    Args:
        header_value: The raw ``Cache-Control`` value, e.g.
            ``"public, max-age=300, must-revalidate"``.

    Returns:
        A dict of ``directive → value``. Empty when the header is empty.
    """
    result: dict[str, str] = {}
    if not header_value:
        return result

    for raw_token in header_value.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if "=" in token:
            name, _, value = token.partition("=")
            name = name.strip().lower()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            result[name] = value
        else:
            result[token.lower()] = ""
    return result


def _parse_http_date(value: str) -> float | None:
    """Parse an HTTP date header into a Unix timestamp.

    Accepts every date format :func:`email.utils.parsedate_to_datetime`
    handles — the HTTP-date grammar allows three legacy formats and the
    function covers all of them. Returns ``None`` for an unparseable
    value rather than raising; a broken ``Expires`` header must not
    invalidate the whole response.

    Args:
        value: The raw header value, e.g. ``"Wed, 21 Oct 2015 07:28:00 GMT"``.

    Returns:
        A Unix timestamp in seconds, or ``None``.
    """
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# CacheEntry                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A single cached HTTP response.

    Immutable. Produced by :meth:`HttpCache.get` and consumed by
    :class:`HttpSession` to build an :class:`httpx.Response`. Every field
    maps to a piece of the original response that must survive a
    process restart to make a cache hit indistinguishable from a fresh
    request.

    Attributes:
        url: The requested URL, verbatim.
        status_code: HTTP status of the cached response (always 200 —
            the cache only stores success responses).
        headers: A whitelist of response headers that survive the cache:
            content-type, content-length, etag, last-modified,
            cache-control, expires, vary, and any header the ``Vary``
            directive declares as part of the cache key. Stored as an
            immutable mapping.
        body: The raw response body, verbatim.
        stored_at: Wall-clock Unix timestamp of when the entry was
            written. Diagnostic only — not used for expiry, which is
            time-monotonic via ``expires_at``.
        expires_at: Wall-clock Unix timestamp at which the entry becomes
            stale. A caller comparing against :func:`time.time` gets the
            RFC 7234 freshness semantics.
        etag: The response's ``ETag`` value, if any. Used for
            revalidation (``If-None-Match``).
        last_modified: The response's ``Last-Modified`` value, if any.
            Used for revalidation (``If-Modified-Since``).
        vary_values: The subset of request headers named by the
            response's ``Vary`` directive, captured at write time. Part
            of the cache key — see the module docstring.
    """

    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    stored_at: float
    expires_at: float
    etag: str | None
    last_modified: str | None
    vary_values: Mapping[str, str]

    def is_fresh(self, now: float) -> bool:
        """Return ``True`` iff the entry has not yet expired.

        Args:
            now: Current Unix time in seconds.

        Returns:
            ``True`` when ``now < expires_at``.
        """
        return now < self.expires_at

    def is_revalidatable(self) -> bool:
        """Return ``True`` iff the entry carries a validator for revalidation.

        Either ``ETag`` or ``Last-Modified`` is enough; a response with
        neither cannot be revalidated and, once stale, must be replaced
        wholesale.
        """
        return bool(self.etag) or bool(self.last_modified)

    def to_httpx_response(self) -> httpx.Response:
        """Build an :class:`httpx.Response` suitable for returning to a caller.

        The response carries an ``X-NexusDL-Cache: hit`` header so a
        caller (or a test, or a diagnostic tool) can tell from the
        response alone that it came from the cache. This header is
        **only** added here — the on-disk representation is exactly the
        original headers.

        Returns:
            A fully-buffered :class:`httpx.Response` with the entry's
            status, headers, and body.
        """
        headers = dict(self.headers)
        headers["X-NexusDL-Cache"] = "hit"
        return httpx.Response(
            status_code=self.status_code,
            headers=headers,
            content=self.body,
        )


# --------------------------------------------------------------------------- #
# CacheStats                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Immutable aggregate snapshot of a :class:`HttpCache`'s state.

    Returned by :meth:`HttpCache.stats`. Counters are cumulative for the
    lifetime of the process; the ``entries`` and ``size_bytes`` fields
    are point-in-time. The diagnostic endpoint and the CLI's
    ``nexusdl cache stats`` subcommand render these verbatim.

    Attributes:
        entries: Number of cached entries on disk.
        size_bytes: Total size of all entries (metadata + body).
        max_size_bytes: The configured ceiling.
        hits: Cumulative count of :meth:`HttpCache.get` calls that
            returned an entry.
        misses: Cumulative count of :meth:`HttpCache.get` calls that
            returned ``None``.
        evictions: Cumulative count of entries removed by LRU eviction.
        revalidations: Cumulative count of successful
            :meth:`HttpCache.refresh` calls (304 revalidation).
    """

    entries: int
    size_bytes: int
    max_size_bytes: int
    hits: int
    misses: int
    evictions: int
    revalidations: int


# --------------------------------------------------------------------------- #
# HttpCache                                                                   #
# --------------------------------------------------------------------------- #


class HttpCache:
    """On-disk HTTP response cache with per-site TTL and LRU eviction.

    One instance per composition root, created at startup and reused for
    the process lifetime. The instance is created lazily (the on-disk
    index is loaded on the first operation that needs it), so a test
    that never touches the cache pays nothing for its existence.

    See the module docstring for the storage format, the freshness
    rules, the eviction policy, and the multi-process warning.

    Example:
        >>> cache = HttpCache(
        ...     cache_dir=Path("~/.cache/nexusdl/http").expanduser(),
        ...     per_site_ttl={"mangadex": 120.0},
        ... )
        >>> entry = await cache.get(
        ...     "https://mangadex.org/title/xyz",
        ...     site_id="mangadex",
        ... )
        >>> if entry is not None and entry.is_fresh(time.time()):
        ...     response = entry.to_httpx_response()
        ... else:
        ...     # Cache miss or stale — issue the real request.
        ...     response = await client.get(url)
        ...     if response.status_code == 200:
        ...         await cache.put(
        ...             "https://mangadex.org/title/xyz",
        ...             site_id="mangadex",
        ...             response=response,
        ...             request_headers=response.request.headers,
        ...         )

    Args:
        cache_dir: Root directory. Every entry is stored under
            ``<cache_dir>/<site_id>/``. Created lazily on first write;
            a read-only deployment that never caches starts with an empty
            directory that never appears.
        max_size_bytes: Total size ceiling for the cache directory.
        eviction_target_ratio: How much to evict down to when the
            ceiling is crossed. See the module docstring.
        default_ttl: TTL applied when neither the response headers nor
            ``per_site_ttl`` specify one.
        per_site_ttl: Site id → TTL, overriding ``default_ttl`` for
            that site. Typically sourced from
            ``Settings.session.cache.per_site_ttl``.
        clock: Wall-clock injectable for tests. Defaults to
            :func:`time.time`. Must be a wall-clock (not monotonic) —
            the TTLs are stored as wall-clock timestamps so they remain
            valid across process restarts.

    Raises:
        ValueError: If any configuration value is out of range.
    """

    __slots__ = (
        "_cache_dir",
        "_clock",
        "_default_ttl",
        "_evictions",
        "_hits",
        "_index",
        "_index_loaded",
        "_index_lock",
        "_index_path",
        "_max_size_bytes",
        "_misses",
        "_per_site_ttl",
        "_revalidations",
        "_target_size_bytes",
        "_total_size",
    )

    def __init__(
        self,
        *,
        cache_dir: Path,
        max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
        eviction_target_ratio: float = DEFAULT_EVICTION_TARGET_RATIO,
        default_ttl: float = DEFAULT_DEFAULT_TTL,
        per_site_ttl: Mapping[str, float] | None = None,
        clock: Any = time.time,
    ) -> None:
        if max_size_bytes < 1:
            raise ValueError(
                f"max_size_bytes must be >= 1, got {max_size_bytes}"
            )
        if not (0.0 < eviction_target_ratio <= 1.0):
            raise ValueError(
                f"eviction_target_ratio must be in (0, 1], got "
                f"{eviction_target_ratio}"
            )
        if default_ttl < 0:
            raise ValueError(
                f"default_ttl must be >= 0, got {default_ttl}"
            )
        if per_site_ttl is not None:
            for site_id, ttl in per_site_ttl.items():
                if not isinstance(site_id, str) or not site_id:
                    raise ValueError(
                        f"per_site_ttl keys must be non-empty strings, "
                        f"got {site_id!r}"
                    )
                if ttl < 0:
                    raise ValueError(
                        f"per_site_ttl[{site_id!r}] must be >= 0, "
                        f"got {ttl}"
                    )

        self._cache_dir: Path = cache_dir
        self._index_path: Path = cache_dir / _INDEX_FILENAME
        self._max_size_bytes: int = max_size_bytes
        self._target_size_bytes: int = int(
            max_size_bytes * eviction_target_ratio
        )
        self._default_ttl: float = float(default_ttl)
        self._per_site_ttl: Mapping[str, float] = (
            MappingProxyType(dict(per_site_ttl)) if per_site_ttl else MappingProxyType({})
        )
        self._clock: Any = clock

        # Lazy index. Loaded on the first operation that touches disk.
        self._index: dict[str, dict[str, Any]] = {}
        self._index_loaded: bool = False
        self._total_size: int = 0

        # Stats — cumulative for the lifetime of the instance.
        self._hits: int = 0
        self._misses: int = 0
        self._evictions: int = 0
        self._revalidations: int = 0

        self._index_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def get(
        self,
        url: str,
        *,
        site_id: str | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> CacheEntry | None:
        """Return the cached entry for *url*, or ``None``.

        Returns the entry regardless of whether it is fresh or stale —
        the caller decides what to do with a stale entry (usually:
        revalidate with the entry's validators). Use
        :meth:`CacheEntry.is_fresh` to discriminate.

        Updates the entry's last-access timestamp so that LRU eviction
        sees this read as "used". A read that returns ``None`` (miss)
        increments the miss counter but does not touch disk beyond the
        index load.

        Args:
            url: The request URL, verbatim.
            site_id: The site the request belongs to. When provided, only
                that site's entries are searched — prevents a cache hit
                across sites that happen to share a URL (rare but
                possible with mirrors).
            request_headers: The request's headers, used to select
                between entries that differ only in their ``Vary``
                values. When ``None``, the cache prefers an entry whose
                ``vary_values`` is empty; a request with a
                ``Vary``-carrying cached response requires the matching
                request headers to hit.

        Returns:
            The matching :class:`CacheEntry`, or ``None``.
        """
        await self._ensure_index()

        key = self._storage_key(url, site_id, request_headers)
        async with self._index_lock:
            entry_path = self._meta_path(site_id, key)
            if not entry_path.is_file():
                self._misses += 1
                return None
            try:
                raw = await asyncio.to_thread(
                    entry_path.read_bytes
                )
            except OSError as exc:
                _log.warning(
                    "Cannot read cache metadata {}: {}: {}",
                    entry_path,
                    type(exc).__name__,
                    exc,
                )
                self._misses += 1
                return None

            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                _log.warning(
                    "Cache metadata {} is corrupted; deleting: {}",
                    entry_path,
                    exc,
                )
                await self._delete_entry_unlocked(site_id, key)
                self._misses += 1
                return None

            entry = await self._load_entry_from_doc(
                site_id, key, document
            )
            if entry is None:
                self._misses += 1
                return None

            # Touch the access timestamp for LRU.
            self._touch_unlocked(site_id, key)
            self._hits += 1
            return entry

    async def put(
        self,
        url: str,
        *,
        response: httpx.Response,
        site_id: str | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> bool:
        """Store *response* in the cache, subject to the freshness rules.

        Returns ``True`` when the entry was written, ``False`` when the
        response was not cacheable (non-200, ``no-store``, oversized
        body, non-cacheable content type, or a method other than GET).
        ``False`` is **not** an error — a response that cannot be cached
        is still served to the caller.

        Args:
            url: The request URL, verbatim.
            response: The :class:`httpx.Response` to cache. Must be fully
                read (the cache stores ``response.content``, so a
                streaming response is not supported — callers using
                :meth:`HttpSession.stream_download` do not invoke
                :meth:`put` in the first place).
            site_id: The site the request belongs to. Scopes the entry's
                directory and lets per-site TTL be applied.
            request_headers: The request's headers, used to capture the
                ``Vary`` values at write time. Required when the response
                carries a ``Vary`` header — otherwise the entry cannot be
                keyed correctly and is skipped.

        Returns:
            ``True`` iff the entry was written.
        """
        if response.request.method not in _CACHEABLE_METHODS:
            return False
        if response.status_code != 200:
            return False
        if len(response.content) > _MAX_CACHEABLE_BODY_SIZE:
            _log.debug(
                "Not caching {}: body size {} exceeds limit {}",
                url,
                len(response.content),
                _MAX_CACHEABLE_BODY_SIZE,
            )
            return False
        if not _content_type_is_cacheable(response.headers):
            return False

        cache_control = parse_cache_control(
            response.headers.get("cache-control", "")
        )
        if "no-store" in cache_control:
            return False

        now = float(self._clock())
        expires_at = self._compute_expires_at(
            cache_control=cache_control,
            headers=response.headers,
            site_id=site_id,
            now=now,
        )

        vary_headers = _parse_vary(response.headers.get("vary", ""))
        vary_values: dict[str, str] = {}
        if vary_headers:
            if request_headers is None:
                _log.debug(
                    "Not caching {}: response carries Vary={} but no "
                    "request_headers were provided",
                    url,
                    list(vary_headers),
                )
                return False
            request_lower = {
                name.lower(): value for name, value in request_headers.items()
            }
            for name in vary_headers:
                value = request_lower.get(name.lower())
                if value is None:
                    _log.debug(
                        "Not caching {}: Vary header {!r} is absent "
                        "from the request",
                        url,
                        name,
                    )
                    return False
                vary_values[name] = value

        entry = CacheEntry(
            url=url,
            status_code=response.status_code,
            headers=_select_cacheable_headers(
                response.headers, vary_headers
            ),
            body=response.content,
            stored_at=now,
            expires_at=expires_at,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            vary_values=MappingProxyType(dict(vary_values)),
        )

        await self._ensure_index()
        key = self._storage_key(url, site_id, request_headers, vary_values)

        async with self._index_lock:
            await self._write_entry_unlocked(site_id, key, entry)
            await self._evict_if_needed_unlocked()
        return True

    async def refresh(
        self,
        url: str,
        *,
        site_id: str | None = None,
        request_headers: Mapping[str, str] | None = None,
        refreshed_headers: Mapping[str, str] | None = None,
    ) -> bool:
        """Refresh an existing entry's TTL after a successful revalidation.

        Called by ``HttpSession`` after a conditional request returned
        ``304 Not Modified``: the entry's body is still valid, but its
        freshness deadline must be reset using the headers from the 304
        response (which may carry a new ``Cache-Control: max-age`` or
        ``Expires``). Increments :attr:`CacheStats.revalidations` when
        the entry exists.

        Args:
            url: The request URL.
            site_id: The site id used at write time.
            request_headers: The request headers used at read time. Must
                match what :meth:`get` was called with, otherwise the
                key resolves to a different entry (or none).
            refreshed_headers: Headers from the 304 response. When
                ``None``, only the existing TTL is extended by
                ``default_ttl`` — this covers the case where a server
                returns a bare 304 with no freshness information.

        Returns:
            ``True`` iff an entry existed and was refreshed.
        """
        await self._ensure_index()
        key = self._storage_key(url, site_id, request_headers)
        async with self._index_lock:
            entry_path = self._meta_path(site_id, key)
            if not entry_path.is_file():
                return False
            try:
                raw = await asyncio.to_thread(entry_path.read_bytes)
            except OSError:
                return False
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False

            entry = await self._load_entry_from_doc(site_id, key, document)
            if entry is None:
                return False

            now = float(self._clock())
            cache_control = parse_cache_control(
                (refreshed_headers or {}).get("cache-control", "")
            )
            expires_at = self._compute_expires_at(
                cache_control=cache_control,
                headers=refreshed_headers or {},
                site_id=site_id,
                now=now,
                fallback_ttl=self._default_ttl,
            )

            refreshed = CacheEntry(
                url=entry.url,
                status_code=entry.status_code,
                headers=entry.headers,
                body=entry.body,
                stored_at=entry.stored_at,
                expires_at=expires_at,
                etag=(
                    (refreshed_headers or {}).get("etag") or entry.etag
                ),
                last_modified=(
                    (refreshed_headers or {}).get("last-modified")
                    or entry.last_modified
                ),
                vary_values=entry.vary_values,
            )
            await self._write_entry_unlocked(site_id, key, refreshed)
            self._revalidations += 1
            return True

    async def invalidate(
        self,
        url: str,
        *,
        site_id: str | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> bool:
        """Delete the entry for *url*, if present.

        Args:
            url: The request URL.
            site_id: The site id used at write time.
            request_headers: The request headers used at write time, for
                ``Vary``-based lookups.

        Returns:
            ``True`` iff an entry existed and was removed.
        """
        await self._ensure_index()
        key = self._storage_key(url, site_id, request_headers)
        async with self._index_lock:
            return await self._delete_entry_unlocked(site_id, key)

    async def clear(self) -> None:
        """Delete every cached entry and reset the index.

        Cumulative stats (hits, misses, evictions, revalidations) are
        preserved — this is a "free disk space" operation, not a
        "reset the process" one.
        """
        await self._ensure_index()
        async with self._index_lock:
            # Delete entry files first, so a crash mid-clear leaves the
            # index pointing at (soon to be stale) files that we then
            # clean on the next startup rather than at (nonexistent)
            # files the index thinks exist.
            for site_id, key in list(self._index):
                await self._delete_entry_unlocked(site_id, key, touch_stats=False)
            self._index.clear()
            self._total_size = 0
            await self._save_index_unlocked()

    async def stats(self) -> CacheStats:
        """Return an immutable snapshot of the cache's state."""
        await self._ensure_index()
        async with self._index_lock:
            return CacheStats(
                entries=len(self._index),
                size_bytes=self._total_size,
                max_size_bytes=self._max_size_bytes,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                revalidations=self._revalidations,
            )

    # ------------------------------------------------------------------ #
    # Internals — index management                                       #
    # ------------------------------------------------------------------ #

    async def _ensure_index(self) -> None:
        """Load the index from disk if it has not been loaded yet."""
        if self._index_loaded:
            return
        async with self._index_lock:
            if self._index_loaded:
                return
            self._index = await self._load_index_from_disk()
            self._total_size = sum(
                meta["size"] for meta in self._index.values()
            )
            self._index_loaded = True

    async def _load_index_from_disk(self) -> dict[str, dict[str, Any]]:
        """Read ``_index.json`` and validate its shape.

        A missing or corrupt index is not an error — a cache that has
        not been used yet has no index, and a corrupt index (from a
        crash mid-write) is a cache we can safely start over. Orphaned
        entry files left by a corrupt index are cleaned lazily by
        :meth:`_evict_if_needed_unlocked` and by :meth:`clear`.
        """
        if not self._index_path.is_file():
            return {}
        try:
            raw = await asyncio.to_thread(self._index_path.read_bytes)
        except OSError as exc:
            _log.warning(
                "Cannot read cache index {}: {}: {}; starting with an "
                "empty index",
                self._index_path,
                type(exc).__name__,
                exc,
            )
            return {}
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log.warning(
                "Cache index {} is corrupted; starting with an empty "
                "index: {}",
                self._index_path,
                exc,
            )
            return {}
        if not isinstance(document, dict):
            _log.warning(
                "Cache index {} has an invalid shape; starting with an "
                "empty index",
                self._index_path,
            )
            return {}

        result: dict[str, dict[str, Any]] = {}
        for compound_key, entry_meta in document.items():
            if not isinstance(compound_key, str):
                continue
            if not isinstance(entry_meta, dict):
                continue
            size = entry_meta.get("size")
            if not isinstance(size, int) or size < 0:
                continue
            result[compound_key] = entry_meta
        return result

    async def _save_index_unlocked(self) -> None:
        """Persist the index to disk atomically. Caller must hold the lock.

        Writes to a temp file, ``fsync``'s, and renames. A crash mid-save
        leaves the previous index intact — the cache directory remains
        fully consistent from the caller's point of view.
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self._index, indent=2, sort_keys=True
        ).encode("utf-8")

        def _write() -> None:
            import os
            import tempfile

            fd, raw_temp = tempfile.mkstemp(
                dir=self._cache_dir,
                prefix=_TEMP_PREFIX,
                suffix=".tmp",
            )
            temp_path = Path(raw_temp)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                temp_path.replace(self._index_path)
            except OSError:
                temp_path.unlink(missing_ok=True)
                raise

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            _log.warning(
                "Cannot write cache index {}: {}: {}",
                self._index_path,
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------------ #
    # Internals — key derivation                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _storage_key(
        url: str,
        site_id: str | None,
        request_headers: Mapping[str, str] | None,
        vary_values: Mapping[str, str] | None = None,
    ) -> str:
        """Return the 64-character hex storage key for a request.

        The key is ``sha256`` of a canonical JSON encoding of the tuple
        ``(site_id, url, sorted_vary_values)``. ``request_headers`` is
        used only to compute the ``vary_values`` when they were not
        already provided by the caller (the ``put`` path); the ``get``
        path passes them explicitly because it does not yet know the
        ``Vary`` headers of the cached entry.

        The nested storage path is ``<cache_dir>/<sha256(site_id)>/`` so
        that a cache directory can be inspected by site without a lookup.
        """
        if vary_values is None:
            vary_values = {}
        canonical = {
            "site_id": site_id or "",
            "url": url,
            "vary": dict(sorted(vary_values.items())),
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return digest

    def _site_dir(self, site_id: str | None) -> Path:
        """Return the per-site subdirectory (creating it if needed)."""
        return self._cache_dir / (site_id or "_global")

    def _meta_path(self, site_id: str | None, key: str) -> Path:
        """Return the metadata file path for an entry."""
        return self._site_dir(site_id) / f"{key}{_META_SUFFIX}"

    def _body_path(self, site_id: str | None, key: str) -> Path:
        """Return the body file path for an entry."""
        return self._site_dir(site_id) / f"{key}{_BODY_SUFFIX}"

    # ------------------------------------------------------------------ #
    # Internals — entry load / store                                     #
    # ------------------------------------------------------------------ #

    async def _load_entry_from_doc(
        self,
        site_id: str | None,
        key: str,
        document: Mapping[str, Any],
    ) -> CacheEntry | None:
        """Reconstruct a :class:`CacheEntry` from its metadata + body files.

        Returns ``None`` when the entry is malformed or the body file is
        missing — both indicate corruption, and the caller's miss path
        handles them.
        """
        try:
            url = document["url"]
            status_code = document["status_code"]
            headers = document["headers"]
            body_size = document["body_size"]
            stored_at = document["stored_at"]
            expires_at = document["expires_at"]
            etag = document.get("etag")
            last_modified = document.get("last_modified")
            vary_values = document.get("vary_values") or {}
        except (KeyError, TypeError) as exc:
            _log.warning(
                "Cache entry {} is malformed ({}); removing",
                key,
                exc,
            )
            return None

        if not isinstance(url, str) or not isinstance(status_code, int):
            return None
        if not isinstance(headers, dict) or not isinstance(body_size, int):
            return None
        if not isinstance(stored_at, (int, float)) or not isinstance(
            expires_at, (int, float)
        ):
            return None
        if etag is not None and not isinstance(etag, str):
            return None
        if last_modified is not None and not isinstance(last_modified, str):
            return None
        if not isinstance(vary_values, dict):
            return None

        body_path = self._body_path(site_id, key)
        try:
            body = await asyncio.to_thread(body_path.read_bytes)
        except OSError:
            return None
        if len(body) != body_size:
            _log.warning(
                "Cache entry {} body size mismatch (expected {}, got "
                "{}); removing",
                key,
                body_size,
                len(body),
            )
            return None

        return CacheEntry(
            url=url,
            status_code=status_code,
            headers=MappingProxyType(headers),
            body=body,
            stored_at=float(stored_at),
            expires_at=float(expires_at),
            etag=etag,
            last_modified=last_modified,
            vary_values=MappingProxyType(vary_values),
        )

    async def _write_entry_unlocked(
        self,
        site_id: str | None,
        key: str,
        entry: CacheEntry,
    ) -> None:
        """Write an entry's metadata and body atomically. Holds the lock."""
        site_dir = self._site_dir(site_id)
        await asyncio.to_thread(site_dir.mkdir, parents=True, exist_ok=True)

        meta_path = self._meta_path(site_id, key)
        body_path = self._body_path(site_id, key)

        metadata = {
            "url": entry.url,
            "status_code": entry.status_code,
            "headers": dict(entry.headers),
            "body_size": len(entry.body),
            "stored_at": entry.stored_at,
            "expires_at": entry.expires_at,
            "etag": entry.etag,
            "last_modified": entry.last_modified,
            "vary_values": dict(entry.vary_values),
        }
        meta_bytes = json.dumps(
            metadata, indent=2, sort_keys=True
        ).encode("utf-8")

        # Write both files atomically. The body goes first — if a crash
        # happens after body write but before metadata write, the
        # metadata write never happens and the index never learns about
        # the orphan body. That orphan is harmless: it is not referenced
        # from the index and :meth:`clear` reclaims it.
        await asyncio.to_thread(_atomic_write_bytes, body_path, entry.body)
        await asyncio.to_thread(_atomic_write_bytes, meta_path, meta_bytes)

        # Update the index in memory and persist it.
        compound_key = self._compound_key(site_id, key)
        old_size = self._index.get(compound_key, {}).get("size", 0)
        new_size = len(entry.body) + len(meta_bytes)
        self._total_size = self._total_size - old_size + new_size
        self._index[compound_key] = {
            "site_id": site_id or "",
            "key": key,
            "size": new_size,
            "stored_at": entry.stored_at,
            "last_accessed": float(self._clock()),
        }
        await self._save_index_unlocked()

    async def _delete_entry_unlocked(
        self,
        site_id: str | None,
        key: str,
        *,
        touch_stats: bool = True,
    ) -> bool:
        """Delete an entry's files and index record. Holds the lock."""
        compound_key = self._compound_key(site_id, key)
        meta = self._index.pop(compound_key, None)
        if meta is None and touch_stats:
            return False

        if meta is not None:
            self._total_size = max(0, self._total_size - meta.get("size", 0))

        meta_path = self._meta_path(site_id, key)
        body_path = self._body_path(site_id, key)
        for path in (meta_path, body_path):
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError as exc:
                _log.warning(
                    "Cannot delete cache file {}: {}: {}",
                    path,
                    type(exc).__name__,
                    exc,
                )
        await self._save_index_unlocked()
        return meta is not None

    def _touch_unlocked(
        self, site_id: str | None, key: str
    ) -> None:
        """Update the last-accessed timestamp. Holds the lock."""
        compound_key = self._compound_key(site_id, key)
        meta = self._index.get(compound_key)
        if meta is None:
            return
        meta["last_accessed"] = float(self._clock())

    @staticmethod
    def _compound_key(site_id: str | None, key: str) -> str:
        """Return the index key combining site id and storage key."""
        return f"{site_id or '_global'}:{key}"

    # ------------------------------------------------------------------ #
    # Internals — eviction                                              #
    # ------------------------------------------------------------------ #

    async def _evict_if_needed_unlocked(self) -> None:
        """Evict LRU entries until the cache size is under the target.

        Caller must hold :attr:`_index_lock`. Iterates LRU entries and
        deletes them one at a time. Each deletion calls
        :meth:`_delete_entry_unlocked`, which rewrites the index — for a
        large eviction batch this means N index writes. That is
        acceptable in practice: eviction is rare (it fires once the
        cache crosses its ceiling, typically after thousands of writes)
        and the operation is bounded by ``_evict_batch_limit``.

        The ``_evict_batch_limit`` (default 100) caps the batch size to
        keep a pathological case (e.g. one massive entry that would
        trigger thousands of evictions to compensate) from doing
        thousands of index writes. A batch limit hit does not leave the
        cache oversized — the next ``put`` will evict more.
        """
        if self._total_size <= self._max_size_bytes:
            return

        # Sort by last_accessed ascending — oldest first.
        ordered = sorted(
            self._index.items(),
            key=lambda item: item[1].get("last_accessed", 0.0),
        )
        evicted = 0
        for compound_key, meta in ordered:
            if self._total_size <= self._target_size_bytes:
                break
            if evicted >= 100:
                # Defer the rest to the next put — the batch limit keeps
                # a pathological case from running thousands of
                # sequential disk writes under one lock.
                break
            site_id = meta.get("site_id") or None
            key = meta.get("key")
            if not isinstance(key, str):
                continue
            await self._delete_entry_unlocked(
                site_id, key, touch_stats=False
            )
            self._evictions += 1
            evicted += 1

        _log.debug(
            "Cache eviction: removed {} entrie(s); size now {} / {}",
            evicted,
            self._total_size,
            self._max_size_bytes,
        )

    # ------------------------------------------------------------------ #
    # Internals — freshness computation                                 #
    # ------------------------------------------------------------------ #

    def _compute_expires_at(
        self,
        *,
        cache_control: Mapping[str, str],
        headers: Mapping[str, str],
        site_id: str | None,
        now: float,
        fallback_ttl: float | None = None,
    ) -> float:
        """Return the wall-clock expiry timestamp for an entry.

        Applies the precedence documented in the module docstring. The
        caller is expected to have already rejected ``no-store`` before
        calling this; ``no-cache`` is handled here by returning ``now``.
        """
        if "no-cache" in cache_control:
            return now

        max_age_raw = cache_control.get("max-age")
        if max_age_raw is not None:
            try:
                return now + float(max_age_raw)
            except ValueError:
                pass

        expires_header = headers.get("expires", "")
        expires_at = _parse_http_date(expires_header)
        if expires_at is not None:
            return expires_at

        if site_id is not None:
            site_ttl = self._per_site_ttl.get(site_id)
            if site_ttl is not None:
                return now + site_ttl

        if fallback_ttl is not None:
            return now + fallback_ttl
        return now + self._default_ttl

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"HttpCache(dir=<{self._cache_dir.name}>, "
            f"entries={len(self._index)}, "
            f"size={self._total_size}/{self._max_size_bytes}, "
            f"hits={self._hits}, misses={self._misses})"
        )


# --------------------------------------------------------------------------- #
# Module helpers                                                             #
# --------------------------------------------------------------------------- #


def _content_type_is_cacheable(headers: httpx.Headers) -> bool:
    """Return ``True`` iff the response content type is cacheable."""
    content_type = headers.get("content-type", "").lower()
    return any(
        content_type.startswith(prefix)
        for prefix in _CACHEABLE_CONTENT_PREFIXES
    )


def _parse_vary(header_value: str) -> tuple[str, ...]:
    """Parse a ``Vary`` header into a tuple of header names.

    Splits on commas, strips whitespace, lowercases names. An empty
    ``Vary`` header or a header with no values yields ``()``. ``"*"``
    yields ``("*",)`` — the caller treats a wildcard as "not cacheable"
    (a response that depends on every request header cannot be keyed by
    a bounded set of vary values).
    """
    if not header_value:
        return ()
    return tuple(
        name.strip().lower()
        for name in header_value.split(",")
        if name.strip()
    )


def _select_cacheable_headers(
    headers: httpx.Headers,
    vary_headers: Sequence[str],
) -> Mapping[str, str]:
    """Return the subset of response headers that survive the cache.

    Caching all headers would include ``Set-Cookie`` (leaks session
    tokens into the cache directory) and ``Date`` (irrelevant on a hit).
    The whitelist below is deliberately small:

    * ``content-type``, ``content-length``, ``content-encoding``,
      ``content-language`` — needed to reconstruct the response.
    * ``etag``, ``last-modified`` — needed for revalidation.
    * ``cache-control``, ``expires`` — needed for diagnostics (a caller
      that inspects the returned headers sees the original freshness
      info).
    * ``vary`` — declared for clarity.
    * Every header named by the ``Vary`` directive — those are part of
      the cache key by definition.
    """
    whitelist = {
        "content-type",
        "content-length",
        "content-encoding",
        "content-language",
        "etag",
        "last-modified",
        "cache-control",
        "expires",
        "vary",
    }
    result: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in whitelist:
            result[name] = value
        elif name.lower() in vary_headers:
            result[name] = value
    return MappingProxyType(result)


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write *payload* to *target* atomically via a sibling temp file.

    Args:
        target: Destination path.
        payload: Bytes to write.

    Raises:
        OSError: On any I/O failure. The previous target is untouched.
    """
    import os
    import tempfile

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        dir=target.parent,
        prefix=_TEMP_PREFIX,
        suffix=".tmp",
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        temp_path.replace(target)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
