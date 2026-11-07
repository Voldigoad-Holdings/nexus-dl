"""Deduplicate concurrent and recent download work by key.

The downloader pipeline is naturally idempotent at the file level — the
worker re-downloads a chapter from scratch on every run, and the manager's
``skip_existing`` flag short-circuits already-downloaded chapters by
checking the output file. Neither of those guards against **concurrent**
duplicate submissions: two callers (a CLI command and a web request, two
GUI tabs, two scheduler ticks) can ask to download the same chapter at the
same time, and both will run in parallel, doubling bandwidth usage and
producing two identical output files.

:class:`Deduplicator` is the concurrency guard for that case. It is a
two-tier registry:

* **In-flight** — keys currently being processed by some coroutine. A
  second acquire on an in-flight key returns ``False`` immediately; the
  caller is expected to skip, or to wait on the same result through a
  higher-level mechanism (e.g. the manager's futures).
* **Recently completed** — keys that finished successfully within the last
  ``completed_ttl`` seconds. A fresh acquire on such a key also returns
  ``False`` by default. Callers may override per-call with
  ``skip_completed=False`` (useful for an explicit "re-download" action).

Both tiers are bounded:

* The in-flight set is bounded by the number of concurrent coroutines — no
  explicit cap needed.
* The completed cache is bounded by ``max_completed_entries`` (FIFO
  eviction) and optionally by ``completed_ttl`` (lazy expiry on read).

The module is a pure data structure: no I/O, no events, no imports from
``parsers/``, ``plugins/``, or ``interfaces/``. Wiring it into the pipeline
is the composition root's responsibility — see the "Usage" section below.

Usage
-----
The natural wiring is around a single chapter download, in a thin
orchestration layer above
:class:`~nexusdl.core.downloader.manager.DownloadManager`::

    dedup = Deduplicator(completed_ttl=300.0)  # 5-minute memory

    async def submit_chapter(manga: Manga, chapter: Chapter) -> DownloadResult:
        key = make_chapter_key(manga.id, chapter.id)
        async with dedup.acquire(key) as acquired:
            if not acquired:
                raise NexusDLError(
                    f"chapter {chapter.id!r} is already being downloaded "
                    f"or was completed recently"
                )
            return await do_download(manga, chapter)

The manager itself does **not** import :class:`Deduplicator`. That keeps
the manager focused on orchestration and lets composition roots choose
their own dedup policy (TTL, capacity) independently of the pipeline.

Design invariants
-----------------
* **No blocking** — :meth:`Deduplicator.try_acquire` never waits. A second
  caller on an in-flight key gets ``False`` immediately. Callers who want
  to wait for the first caller's result should await the manager's future,
  not the deduplicator.
* **Bounded memory** — completed entries are capped both in count and
  (when ``completed_ttl`` is set) in age. A long-running server cannot
  leak.
* **Async-safe** — every mutating operation and every read takes
  :attr:`Deduplicator._lock`, matching the convention of
  :class:`~nexusdl.core.downloader.progress.ProgressTracker`.
* **Deterministic** — ``clock`` is injectable so tests can fake time
  without ``freezegun`` or ``asyncio.sleep``.
* **Failure-aware** — the async context manager marks a key as completed
  only when the body exits **without raising**. A failure releases the key
  without caching it, so a retry can acquire it immediately.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from loguru import logger


__all__ = [
    "DEFAULT_COMPLETED_TTL",
    "DEFAULT_MAX_COMPLETED_ENTRIES",
    "DedupStats",
    "Deduplicator",
    "make_chapter_key",
    "make_manga_key",
    "make_page_key",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Default TTL (in seconds) for the recently-completed cache. 5 minutes is
#: long enough to coalesce the typical duplicate-submission patterns
#: (double-click, scheduler overlap, two API calls within the same second)
#: while short enough that an operator who finishes a chapter and
#: deliberately re-downloads it a few minutes later is not surprised.
DEFAULT_COMPLETED_TTL: Final[float | None] = 300.0

#: Default cap on the number of recently-completed entries retained in
#: memory. 10 000 entries × ~100 bytes ≈ 1 MiB, which is negligible for a
#: long-running server and generous for any single session.
DEFAULT_MAX_COMPLETED_ENTRIES: Final[int] = 10_000


# --------------------------------------------------------------------------- #
# Key constructors                                                            #
# --------------------------------------------------------------------------- #
# Keys are namespaced so a single ``Deduplicator`` instance may be shared
# across all the places a composition root wants to deduplicate —
# chapter downloads, whole-manga downloads, page downloads — without any
# risk of collisions.
# --------------------------------------------------------------------------- #


def make_chapter_key(manga_id: str, chapter_id: str) -> str:
    """Return a canonical dedup key for a chapter download.

    Args:
        manga_id: The manga's stable id (as produced by the parser).
        chapter_id: The chapter's stable id (as produced by the parser).

    Returns:
        A namespaced string of the form ``chapter:<manga_id>:<chapter_id>``.
    """
    return f"chapter:{manga_id}:{chapter_id}"


def make_manga_key(manga_id: str) -> str:
    """Return a canonical dedup key for a whole-manga download.

    Args:
        manga_id: The manga's stable id.

    Returns:
        A namespaced string of the form ``manga:<manga_id>``.
    """
    return f"manga:{manga_id}"


def make_page_key(chapter_id: str, page_index: int) -> str:
    """Return a canonical dedup key for a single-page download.

    Args:
        chapter_id: The chapter's stable id.
        page_index: The page's 0-based index within the chapter.

    Returns:
        A namespaced string of the form ``page:<chapter_id>:<page_index>``.
    """
    return f"page:{chapter_id}:{page_index}"


# --------------------------------------------------------------------------- #
# Statistics                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DedupStats:
    """Immutable snapshot of a :class:`Deduplicator`'s counters and state.

    The counters are cumulative for the lifetime of the :class:`Deduplicator`
    instance (reset only by :meth:`Deduplicator.clear`). The
    :attr:`in_flight` and :attr:`completed` fields are instantaneous.

    Invariants:

    * ``hits == hits_in_flight + hits_completed`` — every rejection is
      attributed to exactly one reason.
    * ``in_flight`` counts distinct keys currently held by a coroutine.
    * ``completed`` counts distinct keys in the recently-completed cache,
      including entries that have expired by TTL but not yet been evicted
      by the FIFO. ``is_recently_completed`` returns ``False`` for expired
      entries, so this field may temporarily overstate "recently completed"
      state at read time — it never overstates *retention*.

    Attributes:
        in_flight: Number of keys currently acquired.
        completed: Number of keys in the completed cache.
        hits: Total rejected acquisitions (``try_acquire`` returned
            ``False``).
        hits_in_flight: Subset of :attr:`hits` rejected because the key was
            in-flight.
        hits_completed: Subset of :attr:`hits` rejected because the key was
            recently completed.
        misses: Total successful acquisitions (``try_acquire`` returned
            ``True``).
        evictions: Number of completed entries evicted by FIFO overflow.
            TTL-based expiry is lazy and does **not** increment this
            counter — an entry that merely stops matching
            :meth:`Deduplicator.is_recently_completed` is still retained
            until FIFO evicts it or :meth:`Deduplicator.clear` runs.
    """

    in_flight: int
    completed: int
    hits: int
    hits_in_flight: int
    hits_completed: int
    misses: int
    evictions: int


# --------------------------------------------------------------------------- #
# Deduplicator                                                                #
# --------------------------------------------------------------------------- #


class Deduplicator:
    """Prevent concurrent and recent duplicate work on identical keys.

    See the module docstring for the design contract. This class is
    asyncio-native and must be driven from a single event loop.

    Example:
        >>> dedup = Deduplicator()
        >>> async with dedup.acquire("chapter:a:1") as acquired:
        ...     if acquired:
        ...         await do_download()
        ...     else:
        ...         pass  # someone else is already downloading it
    """

    def __init__(
        self,
        *,
        completed_ttl: float | None = DEFAULT_COMPLETED_TTL,
        max_completed_entries: int = DEFAULT_MAX_COMPLETED_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialise an empty deduplicator.

        Args:
            completed_ttl: Lifetime, in seconds, of a completed entry in the
                recently-completed cache. ``None`` disables TTL-based
                expiry (entries live until FIFO eviction). ``0.0``
                effectively disables the completed tier (every entry is
                considered expired on the next read). Must be ``>= 0``
                when not ``None``.
            max_completed_entries: Cap on the completed cache size, enforced
                by FIFO eviction. ``0`` disables the completed tier
                entirely. Must be ``>= 0``.
            clock: Callable returning a monotonic float (seconds). Defaults
                to :func:`time.monotonic`. Injectable for tests.

        Raises:
            ValueError: If ``max_completed_entries < 0`` or
                ``completed_ttl`` is negative.
        """
        if max_completed_entries < 0:
            raise ValueError(
                f"max_completed_entries must be >= 0, "
                f"got {max_completed_entries}"
            )
        if completed_ttl is not None and completed_ttl < 0:
            raise ValueError(
                f"completed_ttl must be >= 0 or None, got {completed_ttl}"
            )
        self._completed_ttl = completed_ttl
        self._max_completed_entries = max_completed_entries
        self._clock = clock

        #: Keys currently held by a coroutine. ``set.discard`` is atomic
        #: on CPython, which lets :meth:`acquire` release a key even when
        #: its ``finally`` block is cancelled mid-await.
        self._in_flight: set[str] = set()

        #: Key → completed_at (monotonic seconds). Bounded by
        #: ``max_completed_entries``.
        self._completed: dict[str, float] = {}

        #: Insertion-ordered map of completed keys, used for bounded FIFO
        #: eviction. Values are ignored; insertion order encodes recency,
        #: and ``pop`` + re-insert on every store keeps each key at most
        #: once — a hot key that is completed repeatedly cannot evict the
        #: rest of the cache by producing many stale FIFO entries.
        self._completed_order: dict[str, None] = {}

        #: Cumulative counters exposed via :meth:`stats`.
        self._hits_in_flight: int = 0
        self._hits_completed: int = 0
        self._misses: int = 0
        self._evictions: int = 0

        #: Serialises every mutation and every read.
        self._lock = asyncio.Lock()

        self._log = logger.bind(component="downloader.deduplication")

    # ------------------------------------------------------------------ #
    # Public API — acquire / release                                     #
    # ------------------------------------------------------------------ #

    async def try_acquire(
        self, key: str, *, skip_completed: bool = True
    ) -> bool:
        """Try to acquire *key* without blocking.

        Args:
            key: The dedup key. Use one of the ``make_*_key`` helpers to
                avoid collisions across scopes.
            skip_completed: When ``True`` (the default), a key that was
                recently completed is rejected even though no coroutine
                currently holds it. Set to ``False`` to force a re-download
                even when a recent completion is cached — this still
                respects the in-flight gate, so concurrent duplicates are
                always rejected.

        Returns:
            ``True`` if this caller now holds the key, ``False`` if it was
            already held or (with ``skip_completed=True``) recently
            completed.
        """
        async with self._lock:
            if key in self._in_flight:
                self._hits_in_flight += 1
                self._log.debug("Deduplicated {!r} (in-flight)", key)
                return False
            if skip_completed and self._is_recently_completed_locked(key):
                self._hits_completed += 1
                self._log.debug("Deduplicated {!r} (recently completed)", key)
                return False
            self._in_flight.add(key)
            self._misses += 1
            return True

    async def release(self, key: str, *, completed: bool = False) -> None:
        """Release *key*, optionally caching it as recently completed.

        Idempotent and defensive: releasing a key that was never acquired
        is a no-op and does not add it to the completed cache. This is
        intentional — a spurious release must not poison dedup state.

        Args:
            key: The dedup key.
            completed: When ``True`` **and** the key was held by a caller,
                record it in the recently-completed cache. When ``False``
                (the default), the key is simply freed so the next
                :meth:`try_acquire` can succeed immediately.
        """
        async with self._lock:
            was_in_flight = key in self._in_flight
            self._in_flight.discard(key)
            if completed and was_in_flight and self._max_completed_entries > 0:
                self._store_completed_locked(key)

    @asynccontextmanager
    async def acquire(
        self, key: str, *, skip_completed: bool = True
    ) -> AsyncIterator[bool]:
        """Async context manager wrapping :meth:`try_acquire` and :meth:`release`.

        Yields ``True`` when the caller acquired the key, ``False``
        otherwise. On exit, the key is released; it is marked as completed
        **only if** the body exits without raising. Failure and
        cancellation release without caching, so a retry can acquire the
        key immediately.

        ``release`` runs in a ``finally`` block, and a cancellation that
        lands *inside* that block is handled by forcing the in-flight
        release synchronously before propagating. Without this guard, a
        cancel that interrupts ``await self.release(...)`` would leave the
        key permanently in-flight.

        Args:
            key: The dedup key.
            skip_completed: Forwarded to :meth:`try_acquire`.

        Yields:
            ``True`` if acquired, ``False`` otherwise.
        """
        acquired = await self.try_acquire(key, skip_completed=skip_completed)
        success = False
        try:
            yield acquired
            success = True
        finally:
            if acquired:
                try:
                    await self.release(key, completed=success)
                except asyncio.CancelledError:
                    # The cancellation interrupted the lock acquisition in
                    # ``release``. Force the in-flight release — the
                    # ``set.discard`` below is atomic on CPython — so the
                    # key does not remain permanently acquired. The
                    # completed entry, if any, is lost; correctness of
                    # dedup bookkeeping outweighs caching a completion
                    # that was interrupted by cancellation.
                    self._in_flight.discard(key)
                    raise

    # ------------------------------------------------------------------ #
    # Public API — introspection                                         #
    # ------------------------------------------------------------------ #

    async def is_in_flight(self, key: str) -> bool:
        """Return ``True`` iff *key* is currently held by a coroutine.

        Args:
            key: The dedup key.
        """
        async with self._lock:
            return key in self._in_flight

    async def is_recently_completed(self, key: str) -> bool:
        """Return ``True`` iff *key* is in the recently-completed cache and
        its TTL (if any) has not expired.

        Args:
            key: The dedup key.
        """
        async with self._lock:
            return self._is_recently_completed_locked(key)

    async def should_process(
        self, key: str, *, skip_completed: bool = True
    ) -> bool:
        """Return ``True`` iff neither tier would reject *key* right now.

        Convenience for callers who want a single boolean check before
        calling :meth:`try_acquire` — useful when the caller wants to
        report the reason ("already downloading" vs "already downloaded")
        without duplicating the underlying checks.

        **This check is time-of-check-to-time-of-use.** Between this call
        returning ``True`` and a subsequent :meth:`try_acquire`, another
        coroutine may acquire the key. Use :meth:`try_acquire` (or
        :meth:`acquire`) for correctness; use this method only for
        preliminary reporting.

        Args:
            key: The dedup key.
            skip_completed: Forwarded semantics from :meth:`try_acquire`.
        """
        async with self._lock:
            if key in self._in_flight:
                return False
            if skip_completed and self._is_recently_completed_locked(key):
                return False
            return True

    async def stats(self) -> DedupStats:
        """Return an immutable snapshot of the current counters."""
        async with self._lock:
            return DedupStats(
                in_flight=len(self._in_flight),
                completed=len(self._completed),
                hits=self._hits_in_flight + self._hits_completed,
                hits_in_flight=self._hits_in_flight,
                hits_completed=self._hits_completed,
                misses=self._misses,
                evictions=self._evictions,
            )

    async def clear(self) -> None:
        """Drop every in-flight and completed key, and reset all counters.

        Intended for tests and explicit resets. Does **not** cancel
        coroutines that currently hold a key — the in-flight set is only
        bookkeeping; clearing it while work is running means a subsequent
        :meth:`try_acquire` will succeed even though the original caller
        still holds the (now orphaned) key. Callers who clear must accept
        this.
        """
        async with self._lock:
            self._in_flight.clear()
            self._completed.clear()
            self._completed_order.clear()
            self._hits_in_flight = 0
            self._hits_completed = 0
            self._misses = 0
            self._evictions = 0

    # ------------------------------------------------------------------ #
    # Dunder helpers                                                     #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        """Return a compact debug representation.

        Best-effort: reads the sets and dicts without taking the lock, so
        counts may be momentarily inconsistent under concurrency. Fine for
        log output and REPL exploration; callers who need exact values
        should use :meth:`stats`.
        """
        return (
            f"Deduplicator(in_flight={len(self._in_flight)}, "
            f"completed={len(self._completed)}, "
            f"ttl={self._completed_ttl}, "
            f"max={self._max_completed_entries})"
        )

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _is_recently_completed_locked(self, key: str) -> bool:
        """Return ``True`` iff *key* is in the completed cache and not expired.

        Caller must hold :attr:`_lock`.

        Args:
            key: The dedup key.
        """
        completed_at = self._completed.get(key)
        if completed_at is None:
            return False
        if self._completed_ttl is None:
            return True
        return (self._clock() - completed_at) < self._completed_ttl

    def _store_completed_locked(self, key: str) -> None:
        """Record *key* as recently completed and enforce the FIFO cap.

        Caller must hold :attr:`_lock`. Re-completing an already-recorded
        key moves it to the most-recent position in the FIFO instead of
        appending a duplicate entry — otherwise a hot key completed in a
        burst could accumulate many stale FIFO entries and evict every
        other key from the cache.

        Args:
            key: The dedup key.
        """
        completed_at = self._clock()
        self._completed[key] = completed_at
        # Re-inserting moves the key to the end of the insertion order,
        # which is what "most recently completed" means here.
        self._completed_order.pop(key, None)
        self._completed_order[key] = None
        while len(self._completed_order) > self._max_completed_entries:
            oldest_key, _ = self._completed_order.popitem(last=False)
            # Only evict from ``_completed`` if the key is still present.
            # ``release`` may race with another release for the same key
            # via ``clear``; the guard makes eviction idempotent.
            if oldest_key in self._completed:
                del self._completed[oldest_key]
                self._evictions += 1
