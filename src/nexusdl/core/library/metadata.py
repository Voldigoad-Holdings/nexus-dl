"""Read and write ``ComicInfo.xml`` embedded in archive files.

This module is the bridge between **archive files on disk** and the
**:class:`~nexusdl.core.packaging.comic_info.ComicInfo` model**. It is what
the library scanner (``scanner.py``) uses to turn a directory full of
``.cbz`` / ``.cb7`` / ``.cbt`` files into database rows, and what a
"repair metadata" or "write metadata" feature uses to persist an updated
``ComicInfo.xml`` back into an existing archive.

The ComicInfo *model* and its XML serialization
(:meth:`~nexusdl.core.packaging.comic_info.ComicInfo.from_xml` /
``to_xml``) live in :mod:`nexusdl.core.packaging.comic_info` — this module
does not duplicate them. What it adds is:

* **Format detection** — which archive container holds the file
  (:class:`ArchiveFormat`).
* **Reading** — locate the ``ComicInfo.xml`` entry *inside* the archive
  (case-insensitive, anywhere in the tree), extract its bytes, parse into a
  :class:`~nexusdl.core.packaging.comic_info.ComicInfo`.
* **Writing** — rebuild the archive with the new ``ComicInfo.xml`` in the
  root, atomically, preserving every other entry byte-for-byte.
* **Domain conversion** — turn a :class:`ComicInfo` into the
  :class:`~nexusdl.core.models.manga.Manga` and
  :class:`~nexusdl.core.models.chapter.Chapter` objects the scanner feeds
  to :meth:`~nexusdl.core.library.database.LibraryDatabase.add_manga`.

Supported containers
--------------------
Read and write are supported for the three formats that can carry a
``ComicInfo.xml``:

* ``.cbz`` — ZIP (the canonical format NexusDL produces)
* ``.cb7`` — 7-Zip (via :mod:`py7zr`)
* ``.cbt`` — TAR (including gz / bz2 / xz compression, auto-detected)

``.pdf`` is **not** supported: PDF has no standard mechanism for embedding
``ComicInfo.xml``. Attempting to read or write metadata on a ``.pdf``
raises :class:`~nexusdl.core.exceptions.LibraryError` with a message
naming the file and the reason.

Write cost
----------
**Every write rebuilds the archive.** ZIP, 7z, and TAR have no standard
in-place single-entry update, and even the formats that support appending
a new file would leave the old ``ComicInfo.xml`` as a duplicate. Rebuilding
is the only correct behaviour, and it comes with a cost:

* **CBZ / CBT** — entries are streamed from source to destination, so peak
  memory is one entry at a time (tens of KiB for typical manga pages).
* **CB7** — :mod:`py7zr`'s API forces materializing the entire archive in
  memory. A 500 MB CB7 will consume 500 MB of RAM to update its metadata.
  This is a hard limitation of the library; the caller is informed via the
  :meth:`write_comic_info` docstring and can gate on file size if desired.

All writes are atomic: the rebuilt archive is written to a sibling
temporary file, ``fsync``'d, and :meth:`Path.replace`'d onto the original.
A crash mid-write leaves the original archive intact.

Async model
-----------
All filesystem and archive work runs in a worker thread via
``asyncio.to_thread``. The pure conversion functions
(:func:`comic_info_to_manga`, :func:`comic_info_to_chapter`) are
synchronous — they touch nothing but Python objects.

Dependency graph (no cycles)::

    core.exceptions             ─┐
    core.models.enums            │
    core.models.manga            ├──► core.library.metadata
    core.models.chapter          │
    core.packaging.comic_info   ─┘

No ``parsers/``, ``plugins/``, or ``interfaces/`` dependency (rule R1).
No dependency on ``core.library.database`` or ``core.library.scanner`` —
this module is a leaf: the scanner and the database depend on it, not the
other way round.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypeAlias

from loguru import logger
from py7zr import SevenZipFile

from nexusdl.core.exceptions import LibraryError
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.enums import ContentRating, Language, MangaStatus
from nexusdl.core.models.manga import Manga
from nexusdl.core.packaging.comic_info import ComicInfo

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = [
    "COMIC_INFO_ENTRY_NAME",
    "SUPPORTED_ARCHIVE_EXTENSIONS",
    "ArchiveFormat",
    "ExtractedMetadata",
    "comic_info_to_chapter",
    "comic_info_to_manga",
    "detect_archive_format",
    "extract_metadata",
    "read_comic_info",
    "write_comic_info",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Canonical name of the metadata entry inside an archive. Matching is
#: case-insensitive and basename-only; see :func:`_find_comic_info_entry`.
COMIC_INFO_ENTRY_NAME: Final[str] = "ComicInfo.xml"

#: File extensions recognised as archives carrying a ``ComicInfo.xml``.
#: Lowercase, with the leading dot — compared against
#: ``path.suffix.lower()``.
SUPPORTED_ARCHIVE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".cbz", ".cb7", ".cbt"}
)

#: Type alias for the three container formats this module reads and writes.
ArchiveFormat: TypeAlias = Literal["cbz", "cb7", "cbt"]

#: Mapping from extension to :data:`ArchiveFormat`. Kept in sync with
#: :data:`SUPPORTED_ARCHIVE_EXTENSIONS` by a unit test that iterates both.
_EXTENSION_TO_FORMAT: Final[dict[str, ArchiveFormat]] = {
    ".cbz": "cbz",
    ".cb7": "cb7",
    ".cbt": "cbt",
}

#: Temp-file prefix for atomic writes. Matches the convention used in
#: :mod:`nexusdl.core.image.optimizer` and :mod:`nexusdl.core.image.converter`
#: so a stray temp file is recognisable.
_TEMP_PREFIX_FMT: Final[str] = ".{name}.meta-"


_log = logger.bind(component="library.metadata")


# --------------------------------------------------------------------------- #
# Data classes                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExtractedMetadata:
    """Rich metadata view of one archive file, ready for the scanner.

    Immutable. The scanner uses this object as the single source of truth
    for "what did we learn about this file?" and passes it to the
    conversion helpers (:func:`comic_info_to_manga`,
    :func:`comic_info_to_chapter`).

    Attributes:
        archive_path: Absolute path to the archive.
        archive_format: Which container the archive uses.
        file_size: Size of the archive on disk, in bytes.
        modified_at: Last modification time from the filesystem, as a
            timezone-aware UTC datetime.
        comic_info: Parsed ``ComicInfo.xml`` if the archive contained one,
            otherwise ``None``. A missing ``ComicInfo.xml`` is not an
            error — many archives in the wild lack metadata, and the
            scanner can still import them using filename-derived fallback
            values (or skip them, at the caller's discretion).
    """

    archive_path: Path
    archive_format: ArchiveFormat
    file_size: int
    modified_at: datetime
    comic_info: ComicInfo | None


# --------------------------------------------------------------------------- #
# Format detection                                                            #
# --------------------------------------------------------------------------- #


def detect_archive_format(path: Path) -> ArchiveFormat | None:
    """Return the archive format of *path* by its extension, or ``None``.

    Detection is extension-only — the file's magic bytes are not
    inspected. This is deliberate: extension-only detection is
    deterministic, has no failure modes, and the extension is what every
    tool that produces these files gets right. A file with a ``.cbz``
    extension that is actually a 7z is a malformed archive and will fail
    during open with a clear error from the archive library.

    Args:
        path: The path to inspect.

    Returns:
        ``"cbz"``, ``"cb7"``, or ``"cbt"`` when the extension matches (case
        insensitively); ``None`` for any other extension (including
        ``.pdf`` and ``.zip`` — callers who want to import a raw ``.zip``
        should rename it to ``.cbz`` first, which signals intent).
    """
    return _EXTENSION_TO_FORMAT.get(path.suffix.lower())


# --------------------------------------------------------------------------- #
# Public API — read                                                          #
# --------------------------------------------------------------------------- #


async def read_comic_info(archive_path: Path) -> ComicInfo | None:
    """Read and parse the ``ComicInfo.xml`` from *archive_path*.

    Locates the first entry in the archive whose *basename* matches
    ``comicinfo.xml`` case-insensitively, extracts its bytes, and parses
    them via :meth:`ComicInfo.from_xml`. The entry may live at the archive
    root or in any subdirectory — real-world archives use both layouts.

    Args:
        archive_path: Path to a ``.cbz``, ``.cb7``, or ``.cbt`` file.

    Returns:
        The parsed :class:`ComicInfo`, or ``None`` when the archive
        contains no ``ComicInfo.xml`` entry. A missing metadata entry is
        **not** an error — callers fall back to filename-derived metadata.

    Raises:
        LibraryError: If the archive format is unsupported, the archive
            cannot be opened (corrupt or truncated), or the embedded
            ``ComicInfo.xml`` cannot be parsed.
    """
    return await asyncio.to_thread(_read_comic_info_sync, archive_path)


async def extract_metadata(archive_path: Path) -> ExtractedMetadata:
    """Read filesystem stats and ``ComicInfo.xml`` in a single pass.

    Convenience wrapper combining :func:`read_comic_info` with the file
    stats the scanner needs. Performs the ``stat`` and the archive read in
    a single worker-thread hop, so the caller pays one round-trip instead
    of two.

    Args:
        archive_path: Path to a ``.cbz``, ``.cb7``, or ``.cbt`` file.

    Returns:
        An :class:`ExtractedMetadata`. ``comic_info`` is ``None`` when the
        archive contains no metadata entry.

    Raises:
        LibraryError: If the file does not exist, the archive format is
            unsupported, or the archive is corrupt. ``FileNotFoundError``
            is **not** raised — a missing file is a library-level error
            here, not a lower-level one, so the scanner can report it
            uniformly.
    """
    return await asyncio.to_thread(_extract_metadata_sync, archive_path)


# --------------------------------------------------------------------------- #
# Public API — write                                                         #
# --------------------------------------------------------------------------- #


async def write_comic_info(
    archive_path: Path,
    comic_info: ComicInfo,
) -> None:
    """Rebuild *archive_path* with ``comic_info`` as its ``ComicInfo.xml``.

    Every other entry in the archive is preserved byte-for-byte. The
    rebuild is **atomic**: a new archive is written to a sibling temp file,
    ``fsync``'d, and renamed over the original. On any failure the original
    archive is untouched.

    An existing ``ComicInfo.xml`` (anywhere in the tree, any casing) is
    removed and replaced with a fresh one at the archive root. Newer
    readers (Komga, Kavita, Mihon, YACReader) find metadata at the root by
    convention.

    **Cost warning.** CB7 writes load the entire archive into memory —
    see the module docstring.

    Args:
        archive_path: Path to an existing ``.cbz``, ``.cb7``, or ``.cbt``
            file. The file is modified in place.
        comic_info: The metadata to persist. Serialized via
            :meth:`ComicInfo.to_xml`.

    Raises:
        LibraryError: If the archive format is unsupported, the file does
            not exist or cannot be read, or the rebuild fails at any step.
            The original archive is untouched on failure.
    """
    await asyncio.to_thread(_write_comic_info_sync, archive_path, comic_info)


# --------------------------------------------------------------------------- #
# Public API — conversions                                                   #
# --------------------------------------------------------------------------- #


def comic_info_to_manga(
    comic_info: ComicInfo,
    *,
    site_id: str,
    source_id: str,
    url: str,
) -> Manga:
    """Convert a :class:`ComicInfo` into a :class:`Manga` for insertion.

    Applies the field mapping documented by the ComicRack convention and
    reverse-maps the ISO 639-1 language code to the
    :class:`~nexusdl.core.models.enums.Language` enum. Fields absent from
    the :class:`ComicInfo` are left at their domain defaults.

    The id is generated deterministically from
    ``sha256(f"{site_id}:{source_id}")[:16]``, matching the id contract
    used by every other part of the pipeline. Re-importing the same file
    therefore produces the same id, so ``add_manga`` performs an upsert
    rather than a duplicate insert.

    Args:
        comic_info: The parsed metadata.
        site_id: The site identifier to attribute the manga to. For
            locally-scanned archives, ``"local"`` is the convention.
        source_id: Stable identifier within the site. Typically the
            archive's relative path under the scan root.
        url: Canonical URL for the manga. For local files this is
            usually the ``file://`` URL of the archive.

    Returns:
        A fully-populated :class:`Manga`, ready to be passed to
        :meth:`~nexusdl.core.library.database.LibraryDatabase.add_manga`.
    """
    manga_id = _make_manga_id(site_id, source_id)
    return Manga(
        id=manga_id,
        site_id=site_id,
        source_id=source_id,
        title=comic_info.series or comic_info.title,
        description=comic_info.summary,
        author=comic_info.writer,
        language=_language_from_iso(comic_info.language_iso),
        status=MangaStatus.UNKNOWN,
        content_rating=ContentRating.SAFE,
        is_manga=_is_manga(comic_info.manga),
        year=comic_info.year,
        url=url,
    )


def comic_info_to_chapter(
    comic_info: ComicInfo,
    *,
    manga_id: str,
    source_id: str,
    url: str,
) -> Chapter:
    """Convert a :class:`ComicInfo` into a :class:`Chapter` for insertion.

    The id is generated deterministically from
    ``sha256(f"{manga_id}:{source_id}")[:16]``, matching the id contract
    used by every other part of the pipeline.

    Args:
        comic_info: The parsed metadata.
        manga_id: The id of the manga this chapter belongs to. Must be the
            id produced by :func:`comic_info_to_manga` for the *same*
            archive (or, when one archive contains multiple chapters, the
            id the scanner has chosen for the logical manga).
        source_id: Stable identifier within the manga. Typically the
            archive's relative path under the scan root, or a
            chapter-specific component of it.
        url: Canonical URL for the chapter. For local files this is
            usually the ``file://`` URL of the archive.

    Returns:
        A fully-populated :class:`Chapter`, ready to be passed to
        :meth:`~nexusdl.core.library.database.LibraryDatabase.add_chapter`.
    """
    chapter_id = _make_chapter_id(manga_id, source_id)
    return Chapter(
        id=chapter_id,
        manga_id=manga_id,
        source_id=source_id,
        title=comic_info.title,
        number=comic_info.number or "1",
        volume=comic_info.volume,
        language=_language_from_iso(comic_info.language_iso),
        page_count=comic_info.page_count,
        url=url,
    )


# --------------------------------------------------------------------------- #
# Internal — synchronous read                                                #
# --------------------------------------------------------------------------- #


def _read_comic_info_sync(archive_path: Path) -> ComicInfo | None:
    """Synchronous implementation of :func:`read_comic_info`."""
    fmt = _require_format(archive_path)
    if not archive_path.is_file():
        raise LibraryError(f"archive not found: {archive_path}")

    try:
        if fmt == "cbz":
            payload = _read_from_zip(archive_path)
        elif fmt == "cb7":
            payload = _read_from_sevenzip(archive_path)
        else:  # fmt == "cbt"
            payload = _read_from_tar(archive_path)
    except LibraryError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure here is a corrupt archive
        raise LibraryError(
            f"cannot read archive {archive_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if payload is None:
        return None

    try:
        return ComicInfo.from_xml(payload.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — parse failure = invalid metadata
        raise LibraryError(
            f"cannot parse {COMIC_INFO_ENTRY_NAME} from {archive_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _extract_metadata_sync(archive_path: Path) -> ExtractedMetadata:
    """Synchronous implementation of :func:`extract_metadata`."""
    fmt = _require_format(archive_path)
    if not archive_path.is_file():
        raise LibraryError(f"archive not found: {archive_path}")

    stat = archive_path.stat()
    comic_info = _read_comic_info_sync(archive_path)
    return ExtractedMetadata(
        archive_path=archive_path,
        archive_format=fmt,
        file_size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        comic_info=comic_info,
    )


def _read_from_zip(path: Path) -> bytes | None:
    """Extract ComicInfo.xml bytes from a ZIP-based archive, or ``None``."""
    with zipfile.ZipFile(path, "r") as archive:
        entry = _find_comic_info_entry(archive.namelist())
        if entry is None:
            return None
        return archive.read(entry)


def _read_from_sevenzip(path: Path) -> bytes | None:
    """Extract ComicInfo.xml bytes from a 7z-based archive, or ``None``."""
    with SevenZipFile(path, "r") as archive:
        entry = _find_comic_info_entry(archive.getnames())
        if entry is None:
            return None
        extracted = archive.read(targets=[entry])
        payload = extracted.get(entry)
        if payload is None:
            return None
        return payload.read()


def _read_from_tar(path: Path) -> bytes | None:
    """Extract ComicInfo.xml bytes from a TAR-based archive, or ``None``."""
    with tarfile.open(path, "r:*") as archive:
        entry = _find_comic_info_entry(archive.getnames())
        if entry is None:
            return None
        member = archive.extractfile(entry)
        if member is None:
            return None
        return member.read()


# --------------------------------------------------------------------------- #
# Internal — synchronous write                                               #
# --------------------------------------------------------------------------- #


def _write_comic_info_sync(
    archive_path: Path, comic_info: ComicInfo
) -> None:
    """Synchronous implementation of :func:`write_comic_info`."""
    fmt = _require_format(archive_path)
    if not archive_path.is_file():
        raise LibraryError(f"archive not found: {archive_path}")

    xml_bytes = comic_info.to_xml().encode("utf-8")

    try:
        if fmt == "cbz":
            _rebuild_zip(archive_path, xml_bytes)
        elif fmt == "cb7":
            _rebuild_sevenzip(archive_path, xml_bytes)
        else:  # fmt == "cbt"
            _rebuild_tar(archive_path, xml_bytes)
    except LibraryError:
        raise
    except Exception as exc:  # noqa: BLE001 — rebuild failure = corrupt target
        raise LibraryError(
            f"cannot rebuild archive {archive_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _rebuild_zip(path: Path, xml_bytes: bytes) -> None:
    """Rebuild a ZIP archive with the new ComicInfo.xml, atomically."""
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=_TEMP_PREFIX_FMT.format(name=path.name),
        suffix=".tmp",
        delete=False,
    ) as raw_out:
        temp_path = Path(raw_out.name)
    try:
        with (
            zipfile.ZipFile(path, "r") as src,
            zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as dst,
        ):
            for info in src.infolist():
                if _is_comic_info_name(info.filename):
                    continue
                with src.open(info, "r") as src_entry:
                    # ``ZipFile.writestr`` accepts a file-like object and
                    # streams it, so peak memory is one entry at a time.
                    dst.writestr(info, src_entry.read())
            dst.writestr(COMIC_INFO_ENTRY_NAME, xml_bytes)
        _fsync_and_replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _rebuild_sevenzip(path: Path, xml_bytes: bytes) -> None:
    """Rebuild a 7z archive with the new ComicInfo.xml, atomically.

    **Loads the entire archive into memory.** See the module docstring for
    the rationale and the size caveat.
    """
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=_TEMP_PREFIX_FMT.format(name=path.name),
        suffix=".tmp",
        delete=False,
    ) as raw_out:
        temp_path = Path(raw_out.name)
    try:
        with SevenZipFile(path, "r") as src:
            content: dict[str, io.BytesIO] = src.readall()
            # ``readall`` returns ``dict[str, BytesIO]`` for every entry
            # with a stream; some entries (directories) may lack content.
            entries = {
                name: buf
                for name, buf in content.items()
                if not _is_comic_info_name(name) and buf is not None
            }
        with SevenZipFile(temp_path, "w") as dst:
            for name, buf in entries.items():
                dst.writef(buf, name)
            dst.writef(io.BytesIO(xml_bytes), COMIC_INFO_ENTRY_NAME)
        _fsync_and_replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _rebuild_tar(path: Path, xml_bytes: bytes) -> None:
    """Rebuild a TAR archive with the new ComicInfo.xml, atomically."""
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=_TEMP_PREFIX_FMT.format(name=path.name),
        suffix=".tmp",
        delete=False,
    ) as raw_out:
        temp_path = Path(raw_out.name)
    try:
        with (
            tarfile.open(path, "r:*") as src,
            tarfile.open(temp_path, "w") as dst,
        ):
            for member in src.getmembers():
                if _is_comic_info_name(member.name):
                    continue
                fileobj = src.extractfile(member)
                if fileobj is None:
                    # Directory entries and other non-file members.
                    dst.addfile(member)
                    continue
                # ``tarfile`` streams ``fileobj`` into ``dst`` with the
                # member's size already declared, so peak memory is one
                # entry at a time.
                dst.addfile(member, fileobj)

        # Append the new ComicInfo.xml as a distinct member.
        info = tarfile.TarInfo(name=COMIC_INFO_ENTRY_NAME)
        info.size = len(xml_bytes)
        info.mtime = int(datetime.now(UTC).timestamp())
        with tarfile.open(temp_path, "a") as dst:
            dst.addfile(info, io.BytesIO(xml_bytes))

        _fsync_and_replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_and_replace(temp_path: Path, target: Path) -> None:
    """``fsync`` *temp_path* then atomically replace *target*."""
    with temp_path.open("rb+") as fh:
        os.fsync(fh.fileno())
    temp_path.replace(target)


# --------------------------------------------------------------------------- #
# Internal — helpers                                                         #
# --------------------------------------------------------------------------- #


def _require_format(path: Path) -> ArchiveFormat:
    """Return the archive format of *path*, raising if unsupported.

    Raises:
        LibraryError: If the extension is not one of ``.cbz``, ``.cb7``,
            or ``.cbt``. The message distinguishes PDF (deliberately
            unsupported) from genuinely unknown extensions.
    """
    fmt = detect_archive_format(path)
    if fmt is not None:
        return fmt
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        raise LibraryError(
            f"PDF archives cannot carry {COMIC_INFO_ENTRY_NAME}: {path}. "
            f"Use ComicInfo-compatible formats (.cbz, .cb7, .cbt)."
        )
    raise LibraryError(
        f"unsupported archive format {suffix!r} for {path}; "
        f"supported formats are "
        f"{sorted(SUPPORTED_ARCHIVE_EXTENSIONS)}"
    )


def _find_comic_info_entry(names: Iterator[str]) -> str | None:
    """Return the first archive entry whose basename matches ComicInfo.xml.

    Case-insensitive; matches at the archive root and inside any
    subdirectory. Returns ``None`` when no entry matches.
    """
    target = COMIC_INFO_ENTRY_NAME.lower()
    for name in names:
        basename = name.rsplit("/", 1)[-1].lower()
        if basename == target:
            return name
    return None


def _is_comic_info_name(name: str) -> bool:
    """Return ``True`` iff *name* is a ComicInfo.xml entry (case-insensitive)."""
    return name.rsplit("/", 1)[-1].lower() == COMIC_INFO_ENTRY_NAME.lower()


def _make_manga_id(site_id: str, source_id: str) -> str:
    """Return the deterministic 16-hex-char manga id.

    Matches the id contract used throughout the pipeline:
    ``sha256(f"{site_id}:{source_id}")[:16]``.
    """
    return hashlib.sha256(
        f"{site_id}:{source_id}".encode("utf-8")
    ).hexdigest()[:16]


def _make_chapter_id(manga_id: str, source_id: str) -> str:
    """Return the deterministic 16-hex-char chapter id.

    Matches the id contract used throughout the pipeline:
    ``sha256(f"{manga_id}:{source_id}")[:16]``.
    """
    return hashlib.sha256(
        f"{manga_id}:{source_id}".encode("utf-8")
    ).hexdigest()[:16]


def _language_from_iso(iso: str | None) -> Language:
    """Reverse-map an ISO 639-1 code to a :class:`Language` member.

    Handles the ``"und"`` sentinel and unrecognised codes by returning
    :attr:`Language.UNKNOWN`. The mapping assumes the :class:`Language`
    enum values are the uppercased ISO codes for every standard language —
    which is the case by construction.
    """
    if not iso:
        return Language.UNKNOWN
    normalized = iso.strip().lower()
    if normalized in ("", "und", "unknown"):
        return Language.UNKNOWN
    try:
        return Language(normalized.upper())
    except ValueError:
        return Language.UNKNOWN


def _is_manga(manga_field: str | None) -> bool:
    """Interpret the ComicInfo ``manga`` field as a boolean.

    ComicRack's convention: ``"YesAndRightToLeft"`` (and the rarer
    ``"Yes"``) indicate a manga; ``"No"`` and ``"NoAndRightToLeft"``
    indicate non-manga (western comics, webtoons). Any other value,
    including ``None``, defaults to ``True`` — this project's audience
    is manga-first.
    """
    if manga_field is None:
        return True
    normalized = manga_field.strip().lower()
    if normalized.startswith("no"):
        return False
    return True
