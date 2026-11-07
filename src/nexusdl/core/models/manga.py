"""Domain model for a manga row.

A :class:`Manga` is the central content entity of the library: the object
the database schema (``mangas``) mirrors, that the FTS5 index searches, and
that every UI surface displays. It is a pure, immutable data structure —
no I/O, no logging, no dependency beyond :mod:`nexusdl.core.models.enums`.

Field correspondence with the ``mangas`` table
----------------------------------------------
Every column in ``001_initial.sql`` has a corresponding field here, with
three exceptions:

* ``rowid`` — SQLite internal, never surfaced.
* ``created_at``, ``updated_at``, ``last_read_at`` — exposed as optional
  fields (``datetime | None = None``) because the current
  :meth:`nexusdl.core.library.database.LibraryDatabase._row_to_manga` does
  not hydrate them from the database row. This is a documented gap: when
  it is fixed, these fields will start arriving populated without any
  change to this module. In the meantime they are safe to accept via
  :meth:`Manga.create` and are ignored by the current pipeline, and are
  correctly persisted by :meth:`LibraryDatabase.add_manga` (which uses
  ``getattr(manga, "created_at", None)`` — reading ``None`` means "let the
  database set it to now on insert, preserve on update").
* The table's unique constraint ``UNIQUE(site_id, source_id)`` is enforced
  at the schema level; the model does not duplicate it, because no single
  :class:`Manga` instance can violate a uniqueness constraint against
  *other* rows.

Identity and equality
---------------------
Declared with ``eq=False`` and manual ``__eq__`` / ``__hash__`` by
:attr:`Manga.id` only — the same convention used by
:class:`~nexusdl.core.models.user.User`. Two :class:`Manga` values with the
same id but different mutable fields (``chapter_count``, ``page_count``,
``last_read_at``, …) compare equal and hash identically. This matches
entity semantics in DDD: a manga does not become a different manga because
its chapter count changed.

The id is generated deterministically from
``sha256(f"{site_id}:{source_id}")[:16]``, matching every other id in the
pipeline (see the PROMPT MASTER's ID table). :meth:`Manga.make_id` is the
canonical way to compute it; :meth:`Manga.create` uses it automatically.

Required vs optional fields
---------------------------
The 5 required fields (``id``, ``site_id``, ``source_id``, ``title``,
``url``) are the minimal set that identifies and describes a manga. They
are the same 5 fields every constructor path passes:

* ``comic_info_to_manga`` (library scanner)
* ``_row_to_manga`` (database reads)
* ``Manga.create`` (application-level construction)

Everything else has a default that matches the schema's ``DEFAULT``
clauses. The defaults are the same ones the schema declares — no drift
between model and database is possible without a migration.

URL scheme
----------
:attr:`Manga.url` accepts both ``https://`` (for network mangas) and
``file://`` (for locally-scanned mangas, which the scanner produces via
:meth:`pathlib.Path.as_uri`). The model only enforces a non-empty string.

:attr:`Manga.cover_url`, in contrast, is **HTTPS-only**: it always names a
remote image, and a plaintext cover URL is a MITM surface with no upside.
A parser that gets an HTTP cover URL from a site should upgrade it to
HTTPS before assigning it, or leave ``cover_url=None``. The URL is parsed
with :func:`urllib.parse.urlparse` rather than a bare string-prefix check,
so malformed inputs (``"https://"`` with no host) are rejected.

Timestamp ordering
------------------
``created_at``, ``updated_at``, and ``last_read_at`` are checked for
chronological consistency (``created_at <= updated_at`` and
``last_read_at >= created_at``) with a small tolerance
(:data:`_MAX_CLOCK_SKEW_SECONDS`) for clock skew during multi-system
imports and NTP drift. Beyond that tolerance, an inverted ordering is a
real data-integrity bug and is rejected.

Dependency graph (no cycles)::

    core.models.enums  ──► core.models.manga

No SQLAlchemy, no loguru, no I/O. This file could be lifted into a
standalone library without modification.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
)


__all__ = ["Manga"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Site identifier pattern, identical to the one in
#: :class:`nexusdl.core.models.site.SiteConfig` and ``sites_schema.json``.
_SITE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9_-]*$"
)

#: Manga id pattern — 16 lowercase hex characters, as produced by
#: ``sha256(...)[:16]``. The schema enforces only ``length(id) = 16``; the
#: model is deliberately stricter so that a bug in id generation cannot
#: reach the database and only fail on read.
_MANGA_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{16}$"
)

#: Expected scheme for :attr:`Manga.cover_url`. Compared case-insensitively
#: via :func:`urllib.parse.urlparse` (RFC 3986 §3.1).
_HTTPS_SCHEME: Final[str] = "https"

#: Length of the canonical truncated-sha256 manga id.
_MANGA_ID_LENGTH: Final[int] = 16

#: Tolerance applied to timestamp-ordering checks. Imports of manga rows
#: from other systems occasionally suffer a few seconds of NTP drift; a
#: small skew is not a data-integrity bug.
_MAX_CLOCK_SKEW_SECONDS: Final[int] = 5


def _is_https_url(value: str) -> bool:
    """Return ``True`` iff *value* is a well-formed absolute HTTPS URL.

    Performs a full parse rather than a string-prefix check so that
    ``"https://"`` (no host) and ``"https:// space"`` (whitespace in
    netloc) are rejected. Scheme is compared case-insensitively —
    ``"HTTPS://x"`` is accepted per RFC 3986 §3.1.

    Args:
        value: The candidate URL.

    Returns:
        ``True`` for well-formed absolute HTTPS URLs with a non-empty host.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme.lower() == _HTTPS_SCHEME and bool(parsed.netloc)


# --------------------------------------------------------------------------- #
# Manga                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, eq=False)
class Manga:
    """A manga tracked by the library.

    Immutable. To produce a modified copy — e.g. after updating metadata
    from a re-scan, or after adding a chapter — use
    :func:`dataclasses.replace`::

        updated = replace(manga, chapter_count=manga.chapter_count + 1)

    The class uses identity-based equality (by :attr:`id`), matching the
    :class:`~nexusdl.core.models.user.User` convention. ``Manga`` instances
    are safe to use as dict keys and set members.

    Attributes:
        id: The canonical manga identifier, a 16-character lowercase hex
            string. Generated by :meth:`make_id` or :meth:`create`; on
            construction from a database row, taken verbatim from the
            ``mangas.id`` column.
        site_id: Identifier of the source site. Matches
            :attr:`~nexusdl.core.models.site.SiteConfig.id`. For
            locally-scanned archives, ``"local"`` by convention.
        source_id: The site's own identifier for this manga — a slug, an
            opaque numeric id, whatever the site uses. Combined with
            :attr:`site_id` (via the schema's ``UNIQUE`` constraint) it
            guarantees that the deterministic :attr:`id` is collision-free
            in practice.
        title: Primary display title, verbatim as the source provides it.
        url: Canonical URL of the manga. Accepts both ``https://``
            (network-sourced) and ``file://`` (locally-scanned).
        language: Primary content language. Defaults to
            :attr:`Language.UNKNOWN`; the schema's default is the same.
        status: Publication status. Defaults to
            :attr:`MangaStatus.UNKNOWN`.
        content_rating: Content rating. Defaults to
            :attr:`ContentRating.SAFE` — the schema's default. Callers
            implementing an adult-content gate should filter on this
            field **and** on :attr:`SiteConfig.adult`.
        is_manga: ``True`` for Japanese-style manga (right-to-left), which
            ``ComicInfo`` expresses as ``"YesAndRightToLeft"``; ``False``
            for western comics and webtoons (``"No"``). Defaults to
            ``True`` — the project's primary audience.
        alt_titles: Alternate titles (native script, romanized, English
            translation, …). Stored as a JSON array in the database;
            accepted here as any sequence and normalised to a tuple in
            :meth:`__post_init__`. Order and duplicates are preserved —
            both may be significant (e.g. the same title romanised two
            ways).
        description: Optional synopsis. Not required, frequently absent
            for lesser-known entries.
        author: Optional author name, verbatim. May be a comma-separated
            list when the source site exposes one; the model does not
            parse it.
        artist: Optional artist name, for cases where author and artist
            differ. ``None`` when unknown.
        year: Optional release year. ``None`` when the site does not
            expose it.
        cover_url: Optional HTTPS URL of the cover image. Rejected at
            construction if it is not a well-formed absolute HTTPS URL.
        cover_path: Optional local filesystem path to a cached cover
            image. Populated by a "download cover" step (future); does not
            affect any current pipeline.
        tags: Genre / tag names. Stored as a JSON array in the database;
            accepted here as any sequence and normalised to a tuple in
            :meth:`__post_init__`. Order and duplicates are preserved.
        page_count: Total number of pages across all chapters.
            Denormalised from ``chapters`` for query speed; maintained by
            :meth:`LibraryDatabase.add_chapter`. Defaults to ``0`` for
            a newly-imported manga whose chapters have not been scanned
            yet.
        chapter_count: Total number of chapters. Same denormalisation and
            maintenance as :attr:`page_count`. Defaults to ``0``.
        created_at: UTC timestamp of creation. ``None`` when the current
            :meth:`LibraryDatabase._row_to_manga` produces this instance —
            see the module docstring. Populated when set explicitly
            (typically by :meth:`create` or a future database fix).
        updated_at: UTC timestamp of last modification. Same population
            contract as :attr:`created_at`.
        last_read_at: UTC timestamp of the most recent reading session.
            ``None`` for a manga that has never been opened. Same
            population contract as :attr:`created_at`.
    """

    # --- Required ------------------------------------------------------- #
    id: str
    site_id: str
    source_id: str
    title: str
    url: str

    # --- Content ------------------------------------------------------- #
    language: Language = Language.UNKNOWN
    status: MangaStatus = MangaStatus.UNKNOWN
    content_rating: ContentRating = ContentRating.SAFE
    is_manga: bool = True

    # --- Descriptive metadata ----------------------------------------- #
    alt_titles: Sequence[str] = ()
    description: str | None = None
    author: str | None = None
    artist: str | None = None
    year: int | None = None

    # --- Assets -------------------------------------------------------- #
    cover_url: str | None = None
    cover_path: Path | None = None

    # --- Classification ----------------------------------------------- #
    tags: Sequence[str] = ()

    # --- Denormalised counters ---------------------------------------- #
    page_count: int = 0
    chapter_count: int = 0

    # --- Timestamps ---------------------------------------------------- #
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_read_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # Validation                                                         #
    # ------------------------------------------------------------------ #

    def __post_init__(self) -> None:
        """Validate field types, value constraints, and normalise sequences.

        Raises:
            TypeError: If a field has the wrong Python type (e.g.
                ``id=123``, ``is_manga=1``, ``year="2024"``,
                ``alt_titles="One Piece"``, ``cover_url=123``).
            ValueError: If a required string is empty, ``id`` does not
                match the canonical pattern, ``site_id`` does not match
                the site-id pattern, ``cover_url`` is not a well-formed
                HTTPS URL, ``page_count`` / ``chapter_count`` / ``year``
                is negative, ``alt_titles`` / ``tags`` contains an empty
                string, or a timestamp ordering invariant is violated.
        """
        self._validate_id()
        self._validate_site_id()
        self._validate_source_id()
        self._validate_title()
        self._validate_url()
        self._validate_enums()
        self._validate_is_manga()
        self._validate_alt_titles()
        self._validate_optional_strings()
        self._validate_year()
        self._validate_cover()
        self._validate_tags()
        self._validate_counters()
        self._validate_timestamps()

    # --- Individual validators (extracted for readability) ------------ #

    def _validate_id(self) -> None:
        if not isinstance(self.id, str):
            raise TypeError(
                f"Manga.id must be a str, got {type(self.id).__name__}"
            )
        if not _MANGA_ID_PATTERN.match(self.id):
            raise ValueError(
                f"Manga.id must be 16 lowercase hex characters, "
                f"got {self.id!r}"
            )

    def _validate_site_id(self) -> None:
        if not isinstance(self.site_id, str):
            raise TypeError(
                f"Manga.site_id must be a str, got "
                f"{type(self.site_id).__name__}"
            )
        if not _SITE_ID_PATTERN.match(self.site_id):
            raise ValueError(
                f"Manga.site_id must match "
                f"{_SITE_ID_PATTERN.pattern!r}, got {self.site_id!r}"
            )

    def _validate_source_id(self) -> None:
        if not isinstance(self.source_id, str):
            raise TypeError(
                f"Manga.source_id must be a str, got "
                f"{type(self.source_id).__name__}"
            )
        if not self.source_id.strip():
            raise ValueError("Manga.source_id must be non-empty")

    def _validate_title(self) -> None:
        if not isinstance(self.title, str):
            raise TypeError(
                f"Manga.title must be a str, got "
                f"{type(self.title).__name__}"
            )
        if not self.title.strip():
            raise ValueError("Manga.title must be non-empty")

    def _validate_url(self) -> None:
        if not isinstance(self.url, str):
            raise TypeError(
                f"Manga.url must be a str, got {type(self.url).__name__}"
            )
        if not self.url.strip():
            raise ValueError("Manga.url must be non-empty")

    def _validate_enums(self) -> None:
        if not isinstance(self.language, Language):
            raise TypeError(
                f"Manga.language must be a Language, got "
                f"{type(self.language).__name__}"
            )
        if not isinstance(self.status, MangaStatus):
            raise TypeError(
                f"Manga.status must be a MangaStatus, got "
                f"{type(self.status).__name__}"
            )
        if not isinstance(self.content_rating, ContentRating):
            raise TypeError(
                f"Manga.content_rating must be a ContentRating, got "
                f"{type(self.content_rating).__name__}"
            )

    def _validate_is_manga(self) -> None:
        if not isinstance(self.is_manga, bool):
            raise TypeError(
                f"Manga.is_manga must be a bool, got "
                f"{type(self.is_manga).__name__}"
            )

    def _validate_alt_titles(self) -> None:
        if isinstance(self.alt_titles, str):
            # A common mistake — pass a single string thinking it will be
            # treated as "one alt title". Reject explicitly with a clearer
            # message than the generic sequence error.
            raise TypeError(
                "Manga.alt_titles must be a sequence of str, not a single "
                "str — wrap it in a tuple (e.g. ('One Piece',))"
            )
        if not isinstance(self.alt_titles, Sequence):
            raise TypeError(
                f"Manga.alt_titles must be a sequence, got "
                f"{type(self.alt_titles).__name__}"
            )
        for alt in self.alt_titles:
            if not isinstance(alt, str):
                raise TypeError(
                    f"Manga.alt_titles entries must be str, got "
                    f"{type(alt).__name__}"
                )
            if not alt.strip():
                raise ValueError(
                    "Manga.alt_titles must not contain empty strings"
                )
        # Normalise to an immutable tuple regardless of what was passed.
        object.__setattr__(self, "alt_titles", tuple(self.alt_titles))

    def _validate_optional_strings(self) -> None:
        for field_name, value in (
            ("description", self.description),
            ("author", self.author),
            ("artist", self.artist),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(
                    f"Manga.{field_name} must be a str or None, got "
                    f"{type(value).__name__}"
                )

    def _validate_year(self) -> None:
        if self.year is None:
            return
        if not isinstance(self.year, int) or isinstance(self.year, bool):
            raise TypeError(
                f"Manga.year must be an int or None, got "
                f"{type(self.year).__name__}"
            )
        if self.year < 0:
            raise ValueError(
                f"Manga.year must be >= 0, got {self.year}"
            )

    def _validate_cover(self) -> None:
        if self.cover_url is not None:
            if not isinstance(self.cover_url, str):
                raise TypeError(
                    f"Manga.cover_url must be a str or None, got "
                    f"{type(self.cover_url).__name__}"
                )
            if not _is_https_url(self.cover_url):
                raise ValueError(
                    f"Manga.cover_url must be a well-formed absolute "
                    f"HTTPS URL, got {self.cover_url!r}"
                )
        if self.cover_path is not None and not isinstance(
            self.cover_path, Path
        ):
            raise TypeError(
                f"Manga.cover_path must be a Path or None, got "
                f"{type(self.cover_path).__name__}"
            )

    def _validate_tags(self) -> None:
        if isinstance(self.tags, str):
            raise TypeError(
                "Manga.tags must be a sequence of str, not a single "
                "str — wrap it in a tuple (e.g. ('action',))"
            )
        if not isinstance(self.tags, Sequence):
            raise TypeError(
                f"Manga.tags must be a sequence, got "
                f"{type(self.tags).__name__}"
            )
        for tag in self.tags:
            if not isinstance(tag, str):
                raise TypeError(
                    f"Manga.tags entries must be str, got "
                    f"{type(tag).__name__}"
                )
            if not tag.strip():
                raise ValueError("Manga.tags must not contain empty strings")
        object.__setattr__(self, "tags", tuple(self.tags))

    def _validate_counters(self) -> None:
        for field_name, value in (
            ("page_count", self.page_count),
            ("chapter_count", self.chapter_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"Manga.{field_name} must be an int, got "
                    f"{type(value).__name__}"
                )
            if value < 0:
                raise ValueError(
                    f"Manga.{field_name} must be >= 0, got {value}"
                )

    def _validate_timestamps(self) -> None:
        for field_name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("last_read_at", self.last_read_at),
        ):
            if value is None:
                continue
            if not isinstance(value, datetime):
                raise TypeError(
                    f"Manga.{field_name} must be a datetime or None, "
                    f"got {type(value).__name__}"
                )
            if value.tzinfo is None:
                raise ValueError(
                    f"Manga.{field_name} must be timezone-aware"
                )
        skew = timedelta(seconds=_MAX_CLOCK_SKEW_SECONDS)
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at + skew < self.created_at
        ):
            raise ValueError(
                "Manga.created_at must be <= Manga.updated_at "
                f"(tolerating {_MAX_CLOCK_SKEW_SECONDS}s clock skew) "
                f"({self.created_at.isoformat()} > "
                f"{self.updated_at.isoformat()})"
            )
        if (
            self.created_at is not None
            and self.last_read_at is not None
            and self.last_read_at + skew < self.created_at
        ):
            raise ValueError(
                "Manga.last_read_at must be >= Manga.created_at "
                f"(tolerating {_MAX_CLOCK_SKEW_SECONDS}s clock skew) "
                f"({self.last_read_at.isoformat()} < "
                f"{self.created_at.isoformat()})"
            )

    # ------------------------------------------------------------------ #
    # Identity                                                           #
    # ------------------------------------------------------------------ #

    def __eq__(self, other: object) -> bool:
        """Compare by :attr:`id` only.

        Two manga with the same id are the same manga regardless of their
        current ``page_count``, ``chapter_count``, or timestamps.
        """
        if not isinstance(other, Manga):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        """Hash by :attr:`id` only, matching :meth:`__eq__`."""
        return hash(self.id)

    def __repr__(self) -> str:
        """Return a concise debug representation.

        Unlike :class:`User`, :class:`Manga` has no secret fields, so the
        full content is safe to include in logs and tracebacks. The repr
        keeps only the most useful fields — a complete dump is what
        :meth:`to_public_dict` is for.
        """
        return (
            f"Manga(id={self.id!r}, site_id={self.site_id!r}, "
            f"title={self.title!r}, "
            f"language={self.language.value!r}, "
            f"status={self.status.value!r})"
        )

    # ------------------------------------------------------------------ #
    # ID generation                                                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def make_id(cls, site_id: str, source_id: str) -> str:
        """Return the canonical 16-hex-char manga id for the given pair.

        Formula: ``sha256(f"{site_id}:{source_id}")[:16]``. This is the
        only id-generation formula used by the pipeline; the library
        scanner, the metadata layer, and :meth:`Manga.create` all route
        through it (or through an equivalent implementation, guarded by a
        cross-checking unit test).

        Args:
            site_id: The site identifier.
            source_id: The site's own identifier for the manga.

        Returns:
            A 16-character lowercase hex string.
        """
        return hashlib.sha256(
            f"{site_id}:{source_id}".encode("utf-8")
        ).hexdigest()[:_MANGA_ID_LENGTH]

    @classmethod
    def create(
        cls,
        *,
        site_id: str,
        source_id: str,
        title: str,
        url: str,
        **overrides: Any,
    ) -> Manga:
        """Construct a manga with an auto-generated id.

        Computes :attr:`id` via :meth:`make_id` from ``site_id`` and
        ``source_id``. Every other field is taken from ``overrides``
        verbatim and validated by :meth:`__post_init__`. The
        ``**overrides`` signature is deliberately untyped — the
        constructor's own validation is the single source of truth for
        what values are acceptable.

        Use this method (rather than calling :class:`Manga` directly) when
        you are creating a **new** manga from application data. Use the
        plain constructor when hydrating from a database row (where the id
        is already known).

        Args:
            site_id: The site identifier.
            source_id: The site's own identifier for the manga.
            title: The manga's title.
            url: The manga's canonical URL.
            **overrides: Any additional field of :class:`Manga`. See the
                class docstring for the full attribute list. Cannot
                override ``id``, ``site_id``, ``source_id``, ``title``,
                or ``url`` — those are computed or taken from the
                positional arguments.

        Returns:
            A newly-constructed :class:`Manga` with a computed id.

        Raises:
            TypeError: If any field has the wrong type, or if an override
                collides with one of the explicit parameters
                (``id``, ``site_id``, ``source_id``, ``title``, ``url``).
            ValueError: If any field fails semantic validation.
        """
        # Reject overrides that would collide with the explicit parameters.
        # Without this check, a caller passing ``id="..."`` (or any of the
        # other five) would crash deep in ``cls(...)`` with a cryptic
        # ``__init__() got multiple values for keyword argument 'id'``.
        reserved = frozenset({"id", "site_id", "source_id", "title", "url"})
        collisions = reserved & set(overrides)
        if collisions:
            raise TypeError(
                "Manga.create() got conflicting override(s) "
                f"{sorted(collisions)!r}; these parameters are computed "
                "or positional. Remove them from **overrides."
            )
        return cls(
            id=cls.make_id(site_id, source_id),
            site_id=site_id,
            source_id=source_id,
            title=title,
            url=url,
            **overrides,
        )

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the manga.

        Renders enums as their string values and :class:`pathlib.Path`
        (``cover_path``) as a string, so the output is directly consumable
        by a FastAPI handler without transformation.

        Timestamps are rendered as ISO 8601 strings, or ``None`` — the
        caller can distinguish "not set" from "set to epoch" without
        ambiguity.

        ``alt_titles`` and ``tags`` are serialised as JSON arrays (Python
        lists), matching the schema's JSON1 representation.

        Returns:
            A dict with every field of the model, except ``rowid`` (which
            does not exist on the model).
        """
        return {
            "id": self.id,
            "site_id": self.site_id,
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "language": self.language.value,
            "status": self.status.value,
            "content_rating": self.content_rating.value,
            "is_manga": self.is_manga,
            "alt_titles": list(self.alt_titles),
            "description": self.description,
            "author": self.author,
            "artist": self.artist,
            "year": self.year,
            "cover_url": self.cover_url,
            "cover_path": (
                str(self.cover_path)
                if self.cover_path is not None
                else None
            ),
            "tags": list(self.tags),
            "page_count": self.page_count,
            "chapter_count": self.chapter_count,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at is not None
                else None
            ),
            "updated_at": (
                self.updated_at.isoformat()
                if self.updated_at is not None
                else None
            ),
            "last_read_at": (
                self.last_read_at.isoformat()
                if self.last_read_at is not None
                else None
            ),
        }
