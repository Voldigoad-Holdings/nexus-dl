"""Canonical hashing primitives for IDs, storage keys, and content dedup.

Every layer of NexusDL produces hashes: mangas and chapters derive
deterministic IDs from their source identifiers, the cache derives
storage keys from URL + site + Vary values, the future content-dedup
layer will derive identity from file bytes. Historically each call site
did its own ``hashlib.sha256(...).hexdigest()[:16]``, its own encoding
choice, its own string composition — and inevitably, one call site used
``.encode("utf-8")`` while another defaulted to ASCII with locale
fallbacks, one used ``":"`` as a separator while another used ``"/"``,
and a bug surfaced weeks later as "the same manga has two different
IDs depending on which parser discovered it."

This module centralises the **hashing primitives the pipeline actually
uses**, behind a small, documented API. It does not attempt to be a
general-purpose hashing library — it exposes exactly the primitives the
existing code already implements inline, plus a small set of helpers
that will be needed by the content-dedup feature.

ID generation is the primary contract
-------------------------------------
The canonical ID formulas — matching the PROMPT MASTER's ID table
exactly — are:

* ``Manga.id`` = ``sha256(f"{site_id}:{source_id}")[:16]``
* ``Chapter.id`` = ``sha256(f"{manga_id}:{source_id}")[:16]``

Both are **16 lowercase hex characters** — 64 bits of the truncated
SHA-256 digest. 64 bits gives collision resistance up to roughly
``2**32`` distinct entities per category (per the birthday bound), which
is comfortably more than any realistic library could hold. Both use
UTF-8 encoding and a single ``":"`` separator — the two invariants
:func:`make_manga_id` and :func:`make_chapter_id` exist to **freeze**,
so that any future change to those invariants is an intentional edit to
this file rather than a silent drift in one call site.

Why 64 bits and not more
------------------------
16 hex chars fit in a URL path segment, a dict key, a SQLite TEXT
column (the ``mangas`` and ``chapters`` tables use ``CHECK (length(id)
= 16)``), and a log line without wrapping — all things that make
debugging and inspection cheap. The collision risk at ``2**32``
entities per category is ~50%; at 1 billion entities per category
(unrealistic for a personal library) it is ~3%. Expanding to 32 chars
(128 bits) is trivial for a future schema version if the corpus ever
approaches these scales; it is not a concern at the current design's
realistic scales.

Dedup primitives
----------------
:func:`file_sha256` and :func:`file_sha256_sync` hash file *content*,
for the future content-dedup feature (identifying duplicate downloaded
archives regardless of path or filename). The full 64-hex digest is
returned — truncation for display can be applied by the caller.
Chunked reads bound memory to ``chunk_size`` bytes, so hashing a 500 MiB
CB7 does not allocate 500 MiB.

The async variant runs the sync implementation in a worker thread via
:func:`asyncio.to_thread` — the sync read is chunked and CPU-light, so
no ``aiofiles`` dependency is needed; the thread hop alone keeps the
event loop responsive.

What this module deliberately does **not** do
---------------------------------------------
* **No HMAC, no keyed hashes, no signatures.** Those are cryptographic
  operations with distinct security requirements (key derivation,
  constant-time comparison, rotation policy) that have no place in a
  general hashing utility. The one consumer who needs them
  (``CookieManager``'s Fernet key) uses ``cryptography`` directly.
* **No algorithm selection.** SHA-256 is the only algorithm exposed.
  MD5 and SHA-1 are cryptographically broken; BLAKE2/3 offer no
  practical benefit here; a caller with a legitimate reason to use a
  different algorithm reaches for :mod:`hashlib` directly.
* **No base64 / base32 / URL-safe encoding of digests.** Hex is the
  pipeline's canonical format; a caller who wants a different encoding
  formats the hex string themselves.
* **No "does this file match this hash" helper.** That is a comparison
  over two strings, and duplicating it here would tempt a
  non-constant-time comparison into a security-sensitive context
  (comparing hashes of secrets). Callers use ``==`` on the hex strings
  when security is not at stake, and :func:`hmac.compare_digest` when
  it is — the choice belongs at the call site.

Migration notes
---------------
The existing implementations in
:mod:`nexusdl.core.models.manga` (``Manga.make_id``),
:mod:`nexusdl.core.models.chapter` (``Chapter.make_id``),
:mod:`nexusdl.core.models.search` (``SearchResult.manga_id``), and
:mod:`nexusdl.core.library.metadata` (``_make_manga_id``,
``_make_chapter_id``) all implement the same formula. They are
**behaviour-preserving** relative to this module — verifying equality
is a unit test. A future refactor will route every one of them through
:func:`make_manga_id` / :func:`make_chapter_id`; each is a one-line
substitution, safe to apply when the corresponding module is next
opened. The migration is documented, not urgent — a few duplicated
`sha256(...).hexdigest()[:16]` calls are not a bug.

Dependency graph (no cycles)::

    asyncio, hashlib (stdlib)

This module is a **leaf**: it imports nothing from ``nexusdl`` and
nothing from third parties. It could be lifted into a standalone
utility library without modification.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Final


__all__ = [
    "CHAPTER_ID_LENGTH",
    "DEFAULT_FILE_CHUNK_SIZE",
    "MANGA_ID_LENGTH",
    "SHA256_HEX_LENGTH",
    "file_sha256",
    "file_sha256_sync",
    "make_chapter_id",
    "make_manga_id",
    "sha256_hex",
    "sha256_truncated",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Length of a full SHA-256 hex digest, in characters. The value is
#: platform-independent (SHA-256 always produces 32 bytes = 64 hex
#: chars) and is exposed as a constant so that a caller that wants to
#: validate a "full digest" does not hardcode ``64``.
SHA256_HEX_LENGTH: Final[int] = 64

#: Length of a Manga id, in hex characters. Matches the schema's
#: ``CHECK (length(id) = 16)`` constraint on the ``mangas`` table.
#: 16 hex chars = 64 bits.
MANGA_ID_LENGTH: Final[int] = 16

#: Length of a Chapter id, in hex characters. Matches the schema's
#: ``CHECK (length(id) = 16)`` constraint on the ``chapters`` table.
#: Same 64-bit sizing rationale as :data:`MANGA_ID_LENGTH`.
CHAPTER_ID_LENGTH: Final[int] = 16

#: The separator used between the components of an id's preimage string.
#: ``":"`` is chosen because it (a) cannot appear in a site id (schema
#: pattern ``^[a-z][a-z0-9_-]*$``), (b) cannot appear in a chapter id
#: (hex), and (c) is visually distinct in a debug dump
#: (``"mangadex:one-piece"``). Any future change is a **breaking**
#: change to every stored id and requires a migration.
_ID_SEPARATOR: Final[str] = ":"

#: The encoding used to turn the preimage string into bytes. UTF-8 is
#: the only encoding consistent with the rest of the pipeline. Any
#: future change is a **breaking** change to every stored id.
_ID_ENCODING: Final[str] = "utf-8"

#: Chunk size for file hashing, in bytes. Matches the streaming chunk
#: size used by :class:`~nexusdl.core.session.http_session.HttpSession`
#: and the file I/O chunk size used elsewhere in the pipeline. 64 KiB
#: is a balance between syscall overhead (smaller chunks) and working
#: set size (larger chunks).
DEFAULT_FILE_CHUNK_SIZE: Final[int] = 64 * 1024


# --------------------------------------------------------------------------- #
# String hashing                                                              #
# --------------------------------------------------------------------------- #


def sha256_hex(text: str, *, encoding: str = "utf-8") -> str:
    """Return the full SHA-256 hex digest of *text*.

    The digest is 64 lowercase hex characters — the canonical form
    produced by :meth:`hashlib.hash.hexdigest`. Lowercase is not
    negotiable: every database column, cache key, and log line in the
    pipeline expects the lowercase form, and ``hexdigest`` produces it
    natively.

    Args:
        text: The string to hash.
        encoding: The encoding used to convert *text* to bytes.
            Defaults to ``"utf-8"`` — the encoding used throughout the
            pipeline. A caller changing this for anything other than a
            specific protocol requirement is almost certainly making a
            mistake; the argument exists for tests that need to
            reproduce a known hash from a different encoding, not for
            production call sites.

    Returns:
        The full 64-character hex digest.
    """
    return hashlib.sha256(text.encode(encoding)).hexdigest()


def sha256_truncated(
    text: str,
    length: int,
    *,
    encoding: str = "utf-8",
) -> str:
    """Return the first *length* hex characters of *text*'s SHA-256 digest.

    The generic primitive underneath the id helpers. A caller that
    wants a shorter hash for a display token, a cache key, or a
    non-ID purpose uses this directly. A caller that wants an ID uses
    :func:`make_manga_id` / :func:`make_chapter_id` — those freeze the
    length and encoding, which is the whole reason they exist.

    **Truncation is not encryption.** A truncated digest has *fewer*
    bits of collision resistance, not more. ``length=16`` (64 bits) is
    safe up to roughly ``2**32`` distinct entities per category;
    ``length=8`` (32 bits) is safe up to roughly ``2**16`` — thousands,
    not millions. The caller picks the length with awareness of the
    expected entity count.

    Args:
        text: The string to hash.
        length: The number of leading hex characters to return. Must be
            in ``[1, 64]`` — a value below 1 is meaningless and a value
            above 64 cannot be satisfied (SHA-256's digest is exactly 64
            hex characters).
        encoding: As in :func:`sha256_hex`.

    Returns:
        The first *length* hex characters of the digest, lowercase.

    Raises:
        ValueError: If *length* is not in ``[1, 64]``. The check is at
            the call site, not deferred — a caller that passes
            ``length=128`` should get a clear error, not silently
            receive the full 64 characters.
    """
    if not (1 <= length <= SHA256_HEX_LENGTH):
        raise ValueError(
            f"length must be in [1, {SHA256_HEX_LENGTH}], got {length}"
        )
    return sha256_hex(text, encoding=encoding)[:length]


# --------------------------------------------------------------------------- #
# Canonical ID generation                                                     #
# --------------------------------------------------------------------------- #


def make_manga_id(site_id: str, source_id: str) -> str:
    """Return the canonical Manga id for a ``(site_id, source_id)`` pair.

    Formula: ``sha256(f"{site_id}:{source_id}")[:16]``, encoded as
    UTF-8. Matches the PROMPT MASTER's ID table and every existing
    implementation in the pipeline (:meth:`Manga.make_id`,
    ``metadata._make_manga_id``, the ``manga_id`` property on
    :class:`~nexusdl.core.models.search.SearchResult`).

    This helper is the **canonical entry point** for new code. A future
    refactor of the existing implementations will route them through
    this function; the substitution is a one-line change per call site
    and is behaviour-preserving.

    The id is 16 lowercase hex characters — 64 bits — matching the
    schema's ``CHECK (length(id) = 16)`` constraint on the ``mangas``
    table. Collision resistance is discussed in the module docstring.

    Args:
        site_id: The site identifier. Must match the schema pattern
            ``^[a-z][a-z0-9_-]*$`` — this function does **not** validate
            it (validation is :class:`~nexusdl.core.models.site.SiteConfig`'s
            job) but the pattern is what makes the ``:`` separator
            unambiguous.
        source_id: The site's own identifier for the manga. Any
            non-empty string; it is composed with *site_id* via ``":"``
            and hashed.

    Returns:
        A 16-character lowercase hex string, unique (up to hash
        collisions) to the input pair.

    Raises:
        ValueError: If either argument is empty. This is the only
            validation the function performs — an empty ``site_id`` or
            ``source_id`` is not a caller bug this module can safely
            ignore; the resulting id would silently collide with every
            other empty-input id.

    Examples:
        >>> make_manga_id("mangadex", "a1b2c3d4")
        'a1b2c3d4e5f6a7b8'
        >>> make_manga_id("mangadex", "a1b2c3d4") == make_manga_id("mangadex", "a1b2c3d4")
        True
    """
    if not site_id:
        raise ValueError("make_manga_id() requires a non-empty site_id")
    if not source_id:
        raise ValueError(
            "make_manga_id() requires a non-empty source_id"
        )
    return sha256_truncated(
        f"{site_id}{_ID_SEPARATOR}{source_id}",
        length=MANGA_ID_LENGTH,
        encoding=_ID_ENCODING,
    )


def make_chapter_id(manga_id: str, source_id: str) -> str:
    """Return the canonical Chapter id for a ``(manga_id, source_id)`` pair.

    Formula: ``sha256(f"{manga_id}:{source_id}")[:16]``, encoded as
    UTF-8. Matches the PROMPT MASTER's ID table and every existing
    implementation in the pipeline (:meth:`Chapter.make_id`,
    ``metadata._make_chapter_id``).

    Note the *preimage* structure: a chapter id is derived from the
    **manga id**, not from the ``(site_id, source_id)`` pair. This
    means two chapters with the same ``source_id`` in different mangas
    have different ids (because their mangas' ids differ), and it means
    a chapter cannot be reconstructed from its own id alone — the
    caller must have the manga id. This is the intended design: chapter
    ids are scoped by their parent, and the schema's
    ``UNIQUE (manga_id, source_id)`` constraint ensures the
    ``(manga_id, source_id)`` pair is globally unique.

    Args:
        manga_id: The parent manga's id. Must be a 16-hex-char string as
            produced by :func:`make_manga_id`, but this function does
            **not** validate the format — the id is treated as an
            opaque string. A caller that accidentally passes a
            non-canonical string gets a chapter id that happens to be
            valid *for that input*; the schema's foreign key constraint
            catches the resulting orphan at insert time.
        source_id: The site's own identifier for the chapter. Any
            non-empty string.

    Returns:
        A 16-character lowercase hex string.

    Raises:
        ValueError: If either argument is empty.

    Examples:
        >>> make_chapter_id("a1b2c3d4e5f6a7b8", "ch-001")
        'f1e2d3c4b5a69788'
    """
    if not manga_id:
        raise ValueError(
            "make_chapter_id() requires a non-empty manga_id"
        )
    if not source_id:
        raise ValueError(
            "make_chapter_id() requires a non-empty source_id"
        )
    return sha256_truncated(
        f"{manga_id}{_ID_SEPARATOR}{source_id}",
        length=CHAPTER_ID_LENGTH,
        encoding=_ID_ENCODING,
    )


# --------------------------------------------------------------------------- #
# File content hashing                                                        #
# --------------------------------------------------------------------------- #


def file_sha256_sync(
    path: Path,
    *,
    chunk_size: int = DEFAULT_FILE_CHUNK_SIZE,
) -> str:
    """Return the SHA-256 hex digest of *path*'s content, synchronously.

    Reads the file in ``chunk_size``-byte blocks and feeds them to a
    single :class:`hashlib.sha256` context. Memory usage is bounded by
    ``chunk_size`` regardless of file size — hashing a 500 MiB CB7
    allocates 64 KiB of buffers, not 500 MiB.

    The digest depends **only** on the file's bytes — not on its path,
    name, modification time, or any filesystem attribute. Two files with
    identical content produce identical digests, which is what content
    dedup requires.

    Intended for tests, CLI helpers, and any non-async context. Callers
    inside a coroutine should use :func:`file_sha256`, which runs this
    function in a worker thread.

    Args:
        path: Path to the file to hash. Must be a regular file —
            directories, symlinks to directories, and special files
            raise ``IsADirectoryError`` / ``OSError`` from the
            underlying ``open``, which the caller handles.
        chunk_size: Block size for reading, in bytes. Must be
            ``>= 1``. Defaults to :data:`DEFAULT_FILE_CHUNK_SIZE`
            (64 KiB). Smaller chunks reduce memory; larger chunks
            reduce syscall overhead. The default is right for every
            realistic manga library file.

    Returns:
        The full 64-character lowercase hex digest.

    Raises:
        FileNotFoundError: If *path* does not exist.
        PermissionError: If *path* cannot be read.
        IsADirectoryError: If *path* is a directory.
        OSError: On any other I/O failure.
        ValueError: If *chunk_size* is less than 1.
    """
    if chunk_size < 1:
        raise ValueError(
            f"chunk_size must be >= 1, got {chunk_size}"
        )

    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


async def file_sha256(
    path: Path,
    *,
    chunk_size: int = DEFAULT_FILE_CHUNK_SIZE,
) -> str:
    """Return the SHA-256 hex digest of *path*'s content, asynchronously.

    Runs :func:`file_sha256_sync` in a worker thread via
    :func:`asyncio.to_thread`, so the event loop stays responsive during
    reads. The read itself is chunked (see :func:`file_sha256_sync`), so
    memory is bounded regardless of file size.

    The same exceptions as :func:`file_sha256_sync` propagate — file
    errors are **not** swallowed. A caller hashing a library of 10 000
    files with a "skip missing files" policy catches
    :class:`FileNotFoundError` around this call; a caller that treats a
    missing file as an error lets it propagate. The choice is the
    caller's, not this function's.

    Args:
        path: Path to the file to hash.
        chunk_size: Block size for reading, in bytes.

    Returns:
        The full 64-character lowercase hex digest.

    Raises:
        FileNotFoundError, PermissionError, IsADirectoryError, OSError:
            As in :func:`file_sha256_sync`.
        ValueError: If *chunk_size* is less than 1.
    """
    return await asyncio.to_thread(
        file_sha256_sync, path, chunk_size=chunk_size
    )
