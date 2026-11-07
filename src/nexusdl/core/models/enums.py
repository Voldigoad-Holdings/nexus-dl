"""Domain enumerations for NexusDL.

This module is the **leaf** of the model dependency graph: every other
model, every parser, every interface, and every backend module that needs
an enum imports from here. It has no in-package dependencies — only
:mod:`enum` and the standard library — which is why it can be imported
from anywhere without creating an import cycle.

Two styles, two purposes
------------------------
:class:`~enum.StrEnum` (Python 3.11+) is used for every enumeration whose
members are, conceptually, **names**: a language, a status, a rating, a
format, a role, a health level. StrEnum members compare equal to their
string value (``Language.FR == "FR"``), serialize to their value in JSON
without a custom encoder, and match the ``TEXT`` columns in the SQL schema
byte-for-byte.

:class:`~enum.IntEnum` is used for the one enumeration whose members are
**ordered numerically**: :class:`Priority`. The download queue's heap
sorts by ``-int(task.priority.value)``, and the pipeline compares
priorities numerically in several places. An IntEnum gives both the
comparison semantics and the ``.name`` attribute for logging.

Schema correspondence
---------------------
Every value stored in a ``TEXT`` column with a ``CHECK`` constraint must
match a member of the corresponding enumeration **exactly**. The current
mapping (kept in sync with ``migrations/001_initial.sql``):

======================  ==========================  ==========================
Column                  Enumeration                 Allowed values
======================  ==========================  ==========================
``mangas.language``     :class:`Language`           ``FR, EN, JA, KO, ZH, ES,
                                                    PT, IT, DE, RU, AR,
                                                    UNKNOWN``
``mangas.status``       :class:`MangaStatus`        ``ONGOING, COMPLETED,
                                                    HIATUS, CANCELLED,
                                                    UNKNOWN``
``mangas.content_``     :class:`ContentRating`      ``SAFE, SUGGESTIVE,
``rating``                                          EROTICA, PORNOGRAPHIC``
``users.role``          :class:`UserRole`           ``GUEST, USER, ADMIN``
======================  ==========================  ==========================

The other enums (:class:`PackagingFormat`, :class:`Priority`,
:class:`DownloadStatus`, :class:`SiteHealth`) are not persisted as columns
today; they exist only as in-memory domain values.

Value casing
------------
Because the four persisted enumerations are uppercased in the SQL schema,
their members are uppercased here too (``Language.FR = "FR"``, not
``"fr"``). :class:`PackagingFormat` is not persisted and its values are
**lowercase** (``"cbz"``, not ``"CBZ"``) because they double as the
identifier used in event payloads and mirror the file-extension convention
of the packagers (``.cbz``, ``.cb7``, ``.cbt``). :class:`DownloadStatus`
and :class:`SiteHealth` follow the uppercase convention of the other
status enumerations, even though neither is currently persisted — a
future schema column will want the same format.

Adding a member
---------------
Adding a member to a **persisted** enumeration is a **breaking change**:

1. Add the member here (and update the table above).
2. Add a migration that extends the corresponding ``CHECK`` constraint
   and, if the new value should be the default, changes the column
   default.
3. Audit every consumer that exhaustively matches on the enumeration.
   ``mypy --strict`` does **not** flag non-exhaustive ``match`` by itself —
   it would need ``warn_unreachable = True`` in ``[tool.mypy]`` **and**
   the ``match`` case to be provably exhaustive for the type. In practice,
   expect code review to be the safety net, not the type checker.

Adding a member to a **non-persisted** enumeration is a minor change — no
migration required. Renaming is a breaking change for both categories.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


__all__ = [
    "ContentRating",
    "DownloadStatus",
    "Language",
    "MangaStatus",
    "PackagingFormat",
    "Priority",
    "SiteHealth",
    "UserRole",
]


# --------------------------------------------------------------------------- #
# Content metadata                                                            #
# --------------------------------------------------------------------------- #


class Language(StrEnum):
    """Content language of a manga.

    The ISO 639-1 code in uppercase form, matching the ``CHECK`` constraint
    on ``mangas.language``. :attr:`UNKNOWN` maps to ``"und"`` (undetermined)
    when serialised into ``ComicInfo`` — see
    :data:`nexusdl.core.downloader.worker._LANGUAGE_ISO_639_1`.

    The order of members matters for display only; it follows the same
    order as the schema's ``CHECK`` constraint so a reader comparing the
    two files sees them aligned.
    """

    FR = "FR"
    EN = "EN"
    JA = "JA"
    KO = "KO"
    ZH = "ZH"
    ES = "ES"
    PT = "PT"
    IT = "IT"
    DE = "DE"
    RU = "RU"
    AR = "AR"
    UNKNOWN = "UNKNOWN"


class MangaStatus(StrEnum):
    """Publication status of a manga.

    Matches the ``CHECK`` constraint on ``mangas.status``.
    :attr:`UNKNOWN` is the schema's default and the value assigned by
    :func:`~nexusdl.core.library.metadata.comic_info_to_manga` for every
    locally-scanned archive, because ``ComicInfo.xml`` does not carry a
    publication-status field.
    """

    ONGOING = "ONGOING"
    COMPLETED = "COMPLETED"
    HIATUS = "HIATUS"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ContentRating(StrEnum):
    """Content rating of a manga, declared from safest to most explicit.

    Matches the ``CHECK`` constraint on ``mangas.content_rating``. The
    declaration order below (:attr:`SAFE` → :attr:`PORNOGRAPHIC`) is the
    **semantic** order from least to most explicit — and it is **not**
    the alphabetical order of the string values. A caller who needs a
    numeric comparison must materialise the ordering explicitly::

        _CONTENT_RATING_ORDER: tuple[ContentRating, ...] = (
            ContentRating.SAFE,
            ContentRating.SUGGESTIVE,
            ContentRating.EROTICA,
            ContentRating.PORNOGRAPHIC,
        )

        def allows_up_to(
            rating: ContentRating, cap: ContentRating
        ) -> bool:
            return (
                _CONTENT_RATING_ORDER.index(rating)
                <= _CONTENT_RATING_ORDER.index(cap)
            )

    **Do not** compare ``rating.value`` with ``<`` or ``<=``. The
    alphabetical order of the string values is arbitrary relative to the
    semantic order:

    * ``"EROTICA" < "PORNOGRAPHIC" < "SAFE" < "SUGGESTIVE"``

    so a comparison like ``rating.value <= ContentRating.SUGGESTIVE.value``
    happens to accept ``SAFE`` and ``SUGGESTIVE`` **and also accepts**
    ``EROTICA`` and ``PORNOGRAPHIC`` under some other naive tests — the
    point being that **no** string comparison matches the intended
    semantics across every combination. The only correct approaches are:

    1. **Explicit allowlist** — ``rating in {ContentRating.SAFE,
       ContentRating.SUGGESTIVE}`` for a "safe only" gate. This is the
       recommended pattern for any simple binary gate.
    2. **Explicit ordered tuple** — as shown above, for a "cap at X"
       gate.

    :class:`~enum.StrEnum` members do not support ``<`` between themselves
    (Python raises ``TypeError``), which is the safest default — it forces
    a caller to think about the ordering rather than write a comparison
    that silently misbehaves.
    """

    SAFE = "SAFE"
    SUGGESTIVE = "SUGGESTIVE"
    EROTICA = "EROTICA"
    PORNOGRAPHIC = "PORNOGRAPHIC"


# --------------------------------------------------------------------------- #
# Packaging and pipeline                                                      #
# --------------------------------------------------------------------------- #


class PackagingFormat(StrEnum):
    """Output container format for a packaged chapter.

    Values are **lowercase** because they double as identifiers in event
    payloads and mirror the file-extension convention of the packagers
    (``.zip``, ``.cbz``, ``.cb7``, ``.pdf``, ``.tar``). ``FOLDER`` has no
    extension — the packager produces a directory.

    **CBR is deliberately absent** (ADR-002): the ``rarfile`` library
    requires an external ``unrar`` binary whose licensing is incompatible
    with GPL-3.0-or-later. Users who need CBR can convert from CB7 with an
    external tool. Adding ``CBR`` here requires a matching ``.cbr`` entry
    in the packager registry and a documented licensing resolution —
    neither of which exists today.

    Note that the value of :attr:`CBT` is ``"cbt"``, but the file extension
    the worker produces for a CBT is ``.tar``. **Both** the worker and the
    manager carry a ``_FORMAT_EXTENSIONS`` map that must agree on this
    value:

    * :data:`nexusdl.core.downloader.worker._FORMAT_EXTENSIONS`
    * :data:`nexusdl.core.downloader.manager._FORMAT_EXTENSIONS`

    The two are intentionally kept as separate copies with a
    cross-checking test; changing one without the other would make the
    manager's ``skip_existing`` check disagree with the worker's output
    path. ``"cbt"`` names the format, ``.tar`` names the container.
    """

    FOLDER = "folder"
    ZIP = "zip"
    CBZ = "cbz"
    CB7 = "cb7"
    PDF = "pdf"
    CBT = "cbt"


class Priority(IntEnum):
    """Download task priority.

    Numerically ordered: ``LOW < NORMAL < HIGH``. The download queue sorts
    by descending priority (``-int(priority.value)``) and falls back to
    submission order (FIFO) at equal priority.

    Using :class:`~enum.IntEnum` — rather than :class:`~enum.StrEnum`, as
    every other enumeration in this module uses — is deliberate. Priority
    is compared numerically (``task.priority > other.priority``) in more
    places than it is compared by name, and the queue's heap key requires
    an integer. The ``.name`` attribute is still available for logging
    (``"HIGH"``, ``"NORMAL"``, ``"LOW"``).
    """

    LOW = 0
    NORMAL = 1
    HIGH = 2


class DownloadStatus(StrEnum):
    """Lifecycle state of a download task.

    The state machine — enforced by
    :data:`nexusdl.core.downloader.queue._VALID_TRANSITIONS` — is::

        PENDING   → RUNNING | CANCELLED
        RUNNING   → DONE | PAUSED | FAILED
        PAUSED    → RUNNING
        DONE      → (terminal)
        FAILED    → (terminal)
        CANCELLED → (terminal)

    Terminal states are :attr:`DONE`, :attr:`FAILED`, and
    :attr:`CANCELLED`. Any transition not listed here raises
    :class:`~nexusdl.core.exceptions.InvalidStateTransitionError`.

    Uppercase to match the convention of the other status enumerations,
    even though :class:`DownloadStatus` is not persisted as a column today.
    A future queue table would want the same format.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# --------------------------------------------------------------------------- #
# Runtime state                                                               #
# --------------------------------------------------------------------------- #


class SiteHealth(StrEnum):
    """Observed health state of a source site.

    Set by the
    :class:`~nexusdl.core.registry.site_registry.SiteRegistry` on every
    health check, surfaced in ``GET /api/v1/sites/{id}/health`` and in
    :class:`~nexusdl.core.models.site.SiteHealthSnapshot`. Not persisted as
    a column today — health is a runtime concern, not part of the site's
    static configuration.

    Meaning:

    * :attr:`HEALTHY` — the last check succeeded.
    * :attr:`DEGRADED` — the last check succeeded but slow (or the last
      few checks alternated between success and failure). UIs should show
      a warning indicator but still allow downloads.
    * :attr:`DOWN` — the last check failed. Downloads from the site will
      likely fail; UIs should show an error indicator.
    * :attr:`UNKNOWN` — no check has been performed yet in this process.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Identity and authorisation                                                  #
# --------------------------------------------------------------------------- #


class UserRole(StrEnum):
    """Authorisation role of a user account.

    Matches the ``CHECK`` constraint on ``users.role``. Declared from least
    to most privileged, but **the order is descriptive only** — the auth
    layer must use explicit comparisons (``role == UserRole.ADMIN``), not
    ``>=`` against the member, because :class:`~enum.StrEnum` members do
    not support ordering comparisons.

    Meaning:

    * :attr:`GUEST` — anonymous request, never persisted as a row. Reserved
      so that request handlers can pass a consistent ``UserRole`` value
      through the pipeline even when there is no authenticated user.
    * :attr:`USER` — an authenticated account with the default
      permissions. The schema's default.
    * :attr:`ADMIN` — an account with administrative privileges (settings
      mutation, plugin management, user management). Granted only by an
      explicit provisioning step — never through self-registration.
    """

    GUEST = "GUEST"
    USER = "USER"
    ADMIN = "ADMIN"
