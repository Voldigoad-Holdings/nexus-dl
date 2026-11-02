"""Domain model for a chapter row.

A :class:`Chapter` is the unit the download pipeline actually downloads:
the worker fetches its pages, packages them into the desired format, and
produces a :class:`~nexusdl.core.models.download.DownloadResult`. A
chapter always belongs to exactly one
:class:`~nexusdl.core.models.manga.Manga`, referenced by
:attr:`Chapter.manga_id`.

The object is a pure, immutable value — no I/O, no logging, no dependency
beyond :mod:`nexusdl.core.models.enums`. It could be lifted into a
standalone library without modification.

Field correspondence with the ``chapters`` table
------------------------------------------------
Every column in ``001_initial.sql`` has a corresponding field here, with
the same exceptions as :class:`~nexusdl.core.models.manga.Manga`:

* ``created_at`` and ``updated_at`` are exposed as optional fields
  (``datetime | None = None``) because the current
  :meth:`nexusdl.core.library.database.LibraryDatabase._row_to_chapter`
  does not hydrate them from the database row. This is a documented gap
  matching the one in :mod:`nexusdl.core.models.manga`; when it is fixed,
  these fields will start arriving populated without any change to this
  module. In the meantime they are correctly persisted by
  :meth:`LibraryDatabase.add_chapter` (which uses
  ``getattr(chapter, "created_at", None)`` — ``None`` means "let the
  database set it to now on insert, preserve on update").
* The unique constraint ``UNIQUE(manga_id, source_id)`` is enforced at the
  schema level; the model does not duplicate it, because no single
  :class:`Chapter` instance can violate a uniqueness constraint against
  *other* rows.

Identity and equality
---------------------
Declared with ``eq=False`` and manual ``__eq__`` / ``__hash__`` by
:attr:`Chapter.id` only — the same convention as
:class:`~nexusdl.core.models.manga.Manga` and
:class:`~nexusdl.core.models.user.User`. Two :class:`Chapter` values with
the same id but different mutable fields (``page_count``, ``title``)
compare equal and hash identically. A chapter does not become a different
chapter because its title was corrected.

The id is generated deterministically from
``sha256(f"{manga_id}:{source_id}")[:16]``, matching every other id in
the pipeline (see the PROMPT MASTER's ID table). :meth:`Chapter.make_id`
is the canonical way to compute it; :meth:`Chapter.create` uses it
automatically.

Number and volume are strings
-----------------------------
:attr:`Chapter.number` and :attr:`Chapter.volume` are **strings**, not
numbers. This is deliberate and matches the schema (``number TEXT``,
``volume TEXT``). Real-world chapter identifiers include ``"10.5"``,
``"SP1"``, ``"Omake 3"``, and whole-volume releases expressed as
``"Vol. 1"``. A numeric type would force lossy coercion. The library's
``get_chapters`` implementation sorts numeric-looking values numerically
and everything else lexicographically, so storing them as strings loses
nothing.

Both fields are **nullable**. A scanner that reads a ``ComicInfo.xml``
with no ``number`` produces a chapter with ``number=None``; a parser that
reads a chapter listing on a site that omits the volume produces a
chapter with ``volume=None``. Consumers that need to display a
placeholder can fall back to ``"?"`` or ``"—"`` at the call site.

URL scheme
----------
:attr:`Chapter.url` accepts both ``https://`` (network-sourced chapters)
and ``file://`` (locally-scanned archives, produced by the scanner via
:meth:`pathlib.Path.as_uri`). The model only enforces a non-empty string;
it does not restrict the scheme.

No relationship to :class:`~nexusdl.core.models.page.Page`
---------------------------------------------------------
A chapter does **not** own its pages. Pages are fetched on demand via
``parser.get_pages(chapter)`` and are scoped to a single download. The
chapter model carries only the metadata the database schema provides
(notably :attr:`page_count`, denormalised from the source site's listing);
the actual :class:`~nexusdl.core.models.page.Page` list is not part of
this value object.

Dependency graph (no cycles)::

    core.models.enums  ──► core.models.chapter

No ``core.library.*``, no ``core.downloader.*``, no I/O.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from nexusdl.core.models.enums import Language


__all__ = ["Chapter"]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Chapter id and manga id pattern — 16 lowercase hex characters, as
#: produced by ``sha256(...)[:16]``. The schema enforces only
#: ``length(...) = 16``; the model is deliberately stricter so that a bug
#: in id generation cannot reach the database and only fail on read.
_HEX16_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{16}$")

#: Length of the canonical truncated-sha256 id.
_ID_LENGTH: Final[int] = 16


# --------------------------------------------------------------------------- #
# Chapter                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, eq=False)
class Chapter:
    """A single chapter of a manga.

    Immutable. To produce a modified copy — e.g. after correcting a title,
    or after the site corrects a page count — use
    :func:`dataclasses.replace`::

        updated = replace(chapter, title="Chapter One")

    The class uses identity-based equality (by :attr:`id`), matching the
    :class:`~nexusdl.core.models.manga.Manga` and
    :class:`~nexusdl.core.models.user.User` conventions. ``Chapter``
    instances are safe to use as dict keys and set members.

    Attributes:
        id: The canonical chapter identifier, a 16-character lowercase hex
            string. Generated by :meth:`make_id` or :meth:`create`; on
            construction from a database row, taken verbatim from the
            ``chapters.id`` column.
        manga_id: The id of the manga this chapter belongs to. Must match
            :attr:`~nexusdl.core.models.manga.Manga.id` of the parent. Not
            validated against a live manga here — that is the
            :class:`~nexusdl.core.models.download.DownloadTask`'s job
            (which holds a reference to both objects) and the schema's
            job (via the ``REFERENCES mangas(id)`` foreign key).
        source_id: The site's own identifier for this chapter — the last
            path segment of the chapter URL, a numeric id, a slug,
            whatever the site uses. Combined with :attr:`manga_id` (via
            the schema's ``UNIQUE`` constraint) it guarantees that the
            deterministic :attr:`id` is collision-free in practice.
        title: Primary display title, verbatim as the source provides it.
            A chapter with no title at all uses the number as title (some
            parsers do this) — the model does not enforce a non-title
            state.
        url: Canonical URL of the chapter. Accepts both ``https://``
            (network-sourced) and ``file://`` (locally-scanned).
        number: Optional chapter number, as a **string**. See the module
            docstring for why this is not numeric. ``None`` when the
            source does not provide one.
        volume: Optional volume identifier, as a **string**. Same
            semantics and nullability as :attr:`number`.
        language: Content language of the chapter. Defaults to
            :attr:`Language.UNKNOWN`; the schema's default is the same.
            In practice a chapter inherits its manga's language, but
            some sites publish translated chapters in mixed-language
            mangas — the per-chapter field exists for that case.
        page_count: Total number of pages in the chapter. Denormalised
            from the source site's listing; maintained by
            :meth:`LibraryDatabase.add_chapter`. Defaults to ``0`` for a
            newly-created chapter whose pages have not been fetched yet.
        published_at: Optional UTC timestamp of publication. ``None``
            when the source does not expose a date.
        created_at: UTC timestamp of creation. ``None`` when the current
            :meth:`LibraryDatabase._row_to_chapter` produces this
            instance — see the module docstring.
        updated_at: UTC timestamp of last modification. Same population
            contract as :attr:`created_at`.
    """

    # --- Required ------------------------------------------------------- #
    id: str
    manga_id: str
    source_id: str
    title: str
    url: str

    # --- Chapter numbering --------------------------------------------- #
    number: str | None = None
    volume: str | None = None

    # --- Content ------------------------------------------------------- #
    language: Language = Language.UNKNOWN
    page_count: int = 0

    # --- Dates --------------------------------------------------------- #
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # Validation                                                         #
    # ------------------------------------------------------------------ #

    def __post_init__(self) -> None:
        """Validate field types and value constraints.

        Raises:
            TypeError: If a field has the wrong Python type (e.g.
                ``id=123``, ``page_count=True``, ``number=1``,
                ``language="EN"``).
            ValueError: If a required string is empty, ``id`` or
                ``manga_id`` does not match the canonical hex16 pattern,
                ``number`` / ``volume`` is an empty string,
                ``page_count`` is negative, a timestamp is timezone-naive,
                or ``created_at > updated_at``.
        """
        self._validate_id("id", self.id)
        self._validate_id("manga_id", self.manga_id)
        self._validate_required_string("source_id", self.source_id)
        self._validate_required_string("title", self.title)
        self._validate_required_string("url", self.url)
        self._validate_optional_string("number", self.number)
        self._validate_optional_string("volume", self.volume)
        self._validate_language()
        self._validate_page_count()
        self._validate_timestamp("published_at", self.published_at)
        self._validate_timestamp("created_at", self.created_at)
        self._validate_timestamp("updated_at", self.updated_at)
        self._validate_timestamp_ordering()

    # --- Individual validators (extracted for readability) ------------ #

    @staticmethod
    def _validate_id(field_name: str, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(
                f"Chapter.{field_name} must be a str, got "
                f"{type(value).__name__}"
            )
        if not _HEX16_PATTERN.match(value):
            raise ValueError(
                f"Chapter.{field_name} must be 16 lowercase hex "
                f"characters, got {value!r}"
            )

    @staticmethod
    def _validate_required_string(field_name: str, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(
                f"Chapter.{field_name} must be a str, got "
                f"{type(value).__name__}"
            )
        if not value.strip():
            raise ValueError(
                f"Chapter.{field_name} must be non-empty"
            )

    @staticmethod
    def _validate_optional_string(
        field_name: str, value: str | None
    ) -> None:
        if value is None:
            return
        if not isinstance(value, str):
            raise TypeError(
                f"Chapter.{field_name} must be a str or None, got "
                f"{type(value).__name__}"
            )
        if not value.strip():
            raise ValueError(
                f"Chapter.{field_name} must be non-empty when provided"
            )

    def _validate_language(self) -> None:
        if not isinstance(self.language, Language):
            raise TypeError(
                f"Chapter.language must be a Language, got "
                f"{type(self.language).__name__}"
            )

    def _validate_page_count(self) -> None:
        if not isinstance(self.page_count, int) or isinstance(
            self.page_count, bool
        ):
            raise TypeError(
                f"Chapter.page_count must be an int, got "
                f"{type(self.page_count).__name__}"
            )
        if self.page_count < 0:
            raise ValueError(
                f"Chapter.page_count must be >= 0, got {self.page_count}"
            )

    @staticmethod
    def _validate_timestamp(
        field_name: str, value: datetime | None
    ) -> None:
        if value is None:
            return
        if not isinstance(value, datetime):
            raise TypeError(
                f"Chapter.{field_name} must be a datetime or None, got "
                f"{type(value).__name__}"
            )
        if value.tzinfo is None:
            raise ValueError(
                f"Chapter.{field_name} must be timezone-aware"
            )

    def _validate_timestamp_ordering(self) -> None:
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.created_at > self.updated_at
        ):
            raise ValueError(
                "Chapter.created_at must be <= Chapter.updated_at "
                f"({self.created_at.isoformat()} > "
                f"{self.updated_at.isoformat()})"
            )

    # ------------------------------------------------------------------ #
    # Identity                                                           #
    # ------------------------------------------------------------------ #

    def __eq__(self, other: object) -> bool:
        """Compare by :attr:`id` only.

        Two chapters with the same id are the same chapter regardless of
        their current ``title``, ``page_count``, or timestamps.
        """
        if not isinstance(other, Chapter):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        """Hash by :attr:`id` only, matching :meth:`__eq__`."""
        return hash(self.id)

    def __repr__(self) -> str:
        """Return a concise debug representation.

        Unlike :class:`User`, :class:`Chapter` has no secret fields, so the
        full content is safe to include in logs and tracebacks. The repr
        keeps only the most useful fields — a complete dump is what
        :meth:`to_public_dict` is for.
        """
        number_display = self.number if self.number is not None else "?"
        return (
            f"Chapter(id={self.id!r}, manga_id={self.manga_id!r}, "
            f"number={number_display!r}, title={self.title!r}, "
            f"pages={self.page_count}, "
            f"language={self.language.value!r})"
        )

    # ------------------------------------------------------------------ #
    # ID generation                                                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def make_id(cls, manga_id: str, source_id: str) -> str:
        """Return the canonical 16-hex-char chapter id for the given pair.

        Formula: ``sha256(f"{manga_id}:{source_id}")[:16]``. This is the
        only id-generation formula used by the pipeline; the library
        scanner, the metadata layer, and :meth:`Chapter.create` all route
        through it (or through an equivalent implementation, guarded by a
        cross-checking unit test).

        Args:
            manga_id: The parent manga's id.
            source_id: The site's own identifier for the chapter.

        Returns:
            A 16-character lowercase hex string.
        """
        return hashlib.sha256(
            f"{manga_id}:{source_id}".encode("utf-8")
        ).hexdigest()[:_ID_LENGTH]

    @classmethod
    def create(
        cls,
        *,
        manga_id: str,
        source_id: str,
        title: str,
        url: str,
        **overrides: Any,
    ) -> Chapter:
        """Construct a chapter with an auto-generated id.

        Computes :attr:`id` via :meth:`make_id` from ``manga_id`` and
        ``source_id``. Every other field is taken from ``overrides``
        verbatim and validated by :meth:`__post_init__`. The
        ``**overrides`` signature mirrors
        :meth:`Manga.create` — the constructor's own validation is the
        single source of truth for what values are acceptable, and
        enumerating every optional keyword argument would duplicate that
        contract without adding safety.

        Use this method (rather than calling :class:`Chapter` directly)
        when you are creating a **new** chapter from application data. Use
        the plain constructor when hydrating from a database row (where
        the id is already known).

        Args:
            manga_id: The parent manga's id.
            source_id: The site's own identifier for the chapter.
            title: The chapter's title.
            url: The chapter's canonical URL.
            **overrides: Any additional field of :class:`Chapter`. See the
                class docstring for the full attribute list.

        Returns:
            A newly-constructed :class:`Chapter` with a computed id.

        Raises:
            TypeError: If any field has the wrong type.
            ValueError: If any field fails semantic validation.
        """
        return cls(
            id=cls.make_id(manga_id, source_id),
            manga_id=manga_id,
            source_id=source_id,
            title=title,
            url=url,
            **overrides,
        )

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    @property
    def display_number(self) -> str:
        """Return a display-friendly chapter number.

        Returns :attr:`number` when set, otherwise a placeholder (``"?"``)
        so that a UI can render every chapter uniformly without a
        per-call-site ``if chapter.number is None`` check. The placeholder
        is deliberately a single character — ``"N/A"`` or ``"—"`` are
        longer and less visually consistent in a chapter list.
        """
        return self.number if self.number is not None else "?"

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the chapter.

        Rendered by the ``GET /api/v1/chapters/{id}`` endpoint and the
        ``GET /api/v1/manga/{id}/chapters`` listing. Enum members are
        serialised to their ``.value``; timestamps are ISO 8601 strings or
        ``None``.

        Returns:
            A dict with every field of the model.
        """
        return {
            "id": self.id,
            "manga_id": self.manga_id,
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "number": self.number,
            "display_number": self.display_number,
            "volume": self.volume,
            "language": self.language.value,
            "page_count": self.page_count,
            "published_at": (
                self.published_at.isoformat()
                if self.published_at is not None
                else None
            ),
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
        }
