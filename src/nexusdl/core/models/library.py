"""Library domain value objects.

This module is the **canonical home** for the value objects that describe
library state: reading progress and library-wide statistics. Both are pure
data structures — no I/O, no logging, no SQLAlchemy, no dependency beyond
the standard library.

The one thing that lives here that also exists elsewhere
--------------------------------------------------------
:class:`ReadingProgress` is currently defined **twice**: inline in
:mod:`nexusdl.core.library.database` (see the ``NOTE`` at the top of that
class) and here. The duplication is a documented piece of technical debt.
The plan is:

1. **Now** — :class:`ReadingProgress` lives here as the canonical
   definition, and :class:`LibraryStats` is added fresh.
2. **Next time ``database.py`` is opened** — replace the inline
   ``ReadingProgress`` class with::

       from nexusdl.core.models.library import ReadingProgress

   This is a one-line change; the rest of ``database.py`` is unaffected.
   After it, :meth:`LibraryDatabase.get_reading_progress` returns this
   module's class.

**The two classes are structurally identical but their validation differs.**
The inline class in ``database.py`` accepts any argument without checking —
it has ``__slots__`` and an ``__init__`` but no ``__post_init__``. This
class validates every field (non-empty strings, non-negative
``page_index``, tz-aware ``updated_at``). The consequence for the
consolidation is:

* After consolidation, a caller that was passing structurally-invalid
  values (e.g. ``page_index=-1``) to the *inline* class will start
  receiving :class:`ValueError`. That is intentional — the inline class
  was silently accepting invalid data.
* ``isinstance(x, ReadingProgress)`` will only return ``True`` for the
  consolidated class **after** the import swap. Today, ``isinstance``
  checks that mix the two — importing from ``database`` in one place and
  from ``models.library`` in another — return ``False`` for the wrong
  class. That is exactly the bug the consolidation removes, and it is why
  this docstring leads with the issue rather than burying it.

Until consolidation, callers should import :class:`ReadingProgress` from
**one** module only — prefer this one, which will survive the swap.

:class:`LibraryStats` is entirely new — no duplication. It exists so the
CLI (``nexusdl library stats``), the web dashboard, and the health-check
endpoint all render aggregate library numbers from a single, well-typed
value object rather than each summing the same columns in ad-hoc SQL.

Invariants and what the model does **not** enforce
--------------------------------------------------
:class:`LibraryStats` documents the invariant
``manga_count == 0 ⇒ chapter_count == 0 and page_count == 0`` but does
**not** enforce it. Reason: the aggregate query that will populate this
object reads the three tables independently, and a concurrent writer can
make the three counts momentarily inconsistent (a chapter being inserted
while the manga row already exists, or vice-versa during a delete). A
strict check at construction would turn a transient race into a spurious
:class:`ValueError`. Consumers that need the invariant can check
:attr:`LibraryStats.is_empty` and validate the derived counts at their
level if they care.

Dependency graph (no cycles)::

    (no in-package imports)

Both classes are pure. This file could be lifted into a standalone library
without modification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


__all__ = ["LibraryStats", "ReadingProgress"]


# --------------------------------------------------------------------------- #
# ReadingProgress                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReadingProgress:
    """The user's current reading position in one manga.

    One record per manga: the position is the *current* one, not a history.
    Returned by
    :meth:`~nexusdl.core.library.database.LibraryDatabase.get_reading_progress`
    and consumed by every UI to resume at the last-read page.

    Immutable. The only way to "update" a progress record is to replace it
    via :meth:`~nexusdl.core.library.database.LibraryDatabase.set_reading_progress`,
    which upserts the row and refreshes :attr:`updated_at`.

    Chapter-level completion is **not** encoded here — that is derived at
    query time from ``page_index == chapter.page_count - 1``. Storing a
    completion flag would go stale whenever a chapter's page count was
    corrected.

    **Note on the duplicate definition in ``database.py``.** That class is
    structurally identical but skips validation entirely (no
    ``__post_init__``). This class is the canonical, validating version;
    the consolidation plan is documented in the module docstring.
    ``isinstance`` checks that mix the two classes return ``False`` for the
    wrong class until the import swap is performed.

    Attributes:
        manga_id: The manga this progress belongs to. Matches
            :attr:`~nexusdl.core.models.manga.Manga.id`.
        chapter_id: The chapter currently being read. The database enforces,
            via a composite foreign key, that this chapter belongs to
            :attr:`manga_id`.
        page_index: **Zero-based** page position within
            :attr:`chapter_id`. ``0`` means "at the first page". The
            upper bound is ``chapter.page_count - 1`` at read time; the
            model only checks ``>= 0`` because a chapter's page count is
            not known here.
        updated_at: Timezone-aware UTC timestamp of the most recent
            progress update. Matches the ``updated_at`` column of the
            ``reading_progress`` table.
    """

    manga_id: str
    chapter_id: str
    page_index: int
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate field types and value constraints.

        Raises:
            TypeError: If a field has the wrong Python type (e.g.
                ``page_index=True``, ``updated_at`` not a ``datetime``).
            ValueError: If a required string is empty, ``page_index`` is
                negative, or ``updated_at`` is timezone-naive.
        """
        for field_name, value in (
            ("manga_id", self.manga_id),
            ("chapter_id", self.chapter_id),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"ReadingProgress.{field_name} must be a str, got "
                    f"{type(value).__name__}"
                )
            if not value.strip():
                raise ValueError(
                    f"ReadingProgress.{field_name} must be non-empty"
                )
        if not isinstance(self.page_index, int) or isinstance(
            self.page_index, bool
        ):
            raise TypeError(
                f"ReadingProgress.page_index must be an int, got "
                f"{type(self.page_index).__name__}"
            )
        if self.page_index < 0:
            raise ValueError(
                f"ReadingProgress.page_index must be >= 0, "
                f"got {self.page_index}"
            )
        if not isinstance(self.updated_at, datetime):
            raise TypeError(
                f"ReadingProgress.updated_at must be a datetime, got "
                f"{type(self.updated_at).__name__}"
            )
        if self.updated_at.tzinfo is None:
            raise ValueError(
                "ReadingProgress.updated_at must be timezone-aware"
            )

    # ------------------------------------------------------------------ #
    # Convenience                                                        #
    # ------------------------------------------------------------------ #

    @property
    def page_number(self) -> int:
        """Return the **1-based** page number for display.

        Equivalent to ``page_index + 1``. Provided so UI code reads
        naturally (``f"Resume at page {progress.page_number}"``) and so
        off-by-one calculations are not duplicated at call sites.
        """
        return self.page_index + 1

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the progress.

        Rendered for the ``GET /api/v1/library/{id}`` endpoint and for
        similar read APIs. Both :attr:`page_index` (0-based) and
        :attr:`page_number` (1-based) are included so clients can pick the
        one that matches their storage convention without doing arithmetic
        that could drift from the model.

        Returns:
            A dict with ``manga_id``, ``chapter_id``, ``page_index``,
            ``page_number``, and ``updated_at`` (ISO 8601).
        """
        return {
            "manga_id": self.manga_id,
            "chapter_id": self.chapter_id,
            "page_index": self.page_index,
            "page_number": self.page_number,
            "updated_at": self.updated_at.isoformat(),
        }


# --------------------------------------------------------------------------- #
# LibraryStats                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LibraryStats:
    """Aggregate counts describing the current state of the local library.

    Produced by :meth:`LibraryDatabase.get_stats` (future) and rendered by
    CLI ``nexusdl library stats``, the web dashboard, and the health check
    endpoint. A single value object keeps every consumer reporting the same
    numbers and the same semantics — "how many mangas", "how many
    chapters", "how many pages" — computed from the same aggregate SQL.

    Every field is a non-negative integer or an optional timezone-aware
    timestamp. The invariants enforced at construction are exactly the ones
    the underlying aggregates can guarantee: never negative, never naive.

    **The zero-manga invariant is documented but not enforced.** A library
    with ``manga_count == 0`` should have ``chapter_count == 0`` and
    ``page_count == 0`` (the schema's foreign keys enforce the relation),
    but the three counts in this object are computed independently and a
    concurrent writer can make them momentarily inconsistent. A strict
    check at construction would turn a transient race into a spurious
    ``ValueError``. Consumers that need the invariant should check
    :attr:`is_empty` and verify the derived counts at their level if they
    care.

    Attributes:
        manga_count: Total number of manga rows in the library.
        chapter_count: Total number of chapter rows in the library.
            Matches the sum of every manga's ``chapter_count`` when the
            denormalised counters are consistent; the two are computed
            independently so a bug in either surfaces as a visible
            discrepancy rather than hiding behind a single source.
        page_count: Total number of pages across every chapter in the
            library. Same cross-checking rationale as
            :attr:`chapter_count`.
        last_updated_at: The most recent ``updated_at`` timestamp across
            the ``mangas`` table, or ``None`` for an empty library. Not a
            wall-clock "now" — it is the last time the library content
            changed. Used to decide whether a stale dashboard view needs
            refreshing.
    """

    manga_count: int
    chapter_count: int
    page_count: int
    last_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate field types and value constraints.

        Raises:
            TypeError: If a count is not an ``int`` (``bool`` is rejected
                explicitly even though it is a subclass), or
                ``last_updated_at`` is neither ``None`` nor a
                ``datetime``.
            ValueError: If any count is negative, or
                ``last_updated_at`` is timezone-naive.
        """
        for field_name, value in (
            ("manga_count", self.manga_count),
            ("chapter_count", self.chapter_count),
            ("page_count", self.page_count),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"LibraryStats.{field_name} must be an int, got "
                    f"{type(value).__name__}"
                )
            if value < 0:
                raise ValueError(
                    f"LibraryStats.{field_name} must be >= 0, got {value}"
                )
        if self.last_updated_at is not None:
            if not isinstance(self.last_updated_at, datetime):
                raise TypeError(
                    f"LibraryStats.last_updated_at must be a datetime or "
                    f"None, got {type(self.last_updated_at).__name__}"
                )
            if self.last_updated_at.tzinfo is None:
                raise ValueError(
                    "LibraryStats.last_updated_at must be timezone-aware"
                )

    # ------------------------------------------------------------------ #
    # Convenience                                                        #
    # ------------------------------------------------------------------ #

    @property
    def is_empty(self) -> bool:
        """``True`` iff the library contains no mangas.

        A library with ``manga_count == 0`` necessarily has zero chapters
        and zero pages in a quiescent state (the schema's foreign keys
        enforce the relationship). This property checks only the manga
        count — see the class docstring for why the model does not
        additionally validate the other two counts against it.
        """
        return self.manga_count == 0

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the statistics.

        Rendered verbatim by the ``GET /api/v1/library/stats`` endpoint
        (future) and by the CLI's ``--json`` output mode. Enums do not
        appear here; only counts and one optional timestamp.

        Returns:
            A dict with ``manga_count``, ``chapter_count``,
            ``page_count``, ``is_empty``, and ``last_updated_at`` (ISO
            8601 or ``None``).
        """
        return {
            "manga_count": self.manga_count,
            "chapter_count": self.chapter_count,
            "page_count": self.page_count,
            "is_empty": self.is_empty,
            "last_updated_at": (
                self.last_updated_at.isoformat()
                if self.last_updated_at is not None
                else None
            ),
        }
