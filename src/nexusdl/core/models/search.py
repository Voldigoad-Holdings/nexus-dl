"""Search domain models — results of querying one or more source sites.

This module defines the value objects that flow through the search
pipeline:

* :class:`SearchFilters` — the **requested** filters for a search. What
  the caller wants, independent of what any particular site supports.
* :class:`SearchResult` — one manga hit as returned by a site's search
  endpoint. A **partial** view: typically only the fields a listing row
  exposes (title, URL, cover, author, …). Not a
  :class:`~nexusdl.core.models.manga.Manga` — a ``Manga`` requires fields
  (``content_rating``, ``is_manga``, …) that search listings do not
  reliably provide.
* :class:`SearchResultPage` — one site's paginated response to a single
  search call. Carries pagination state so the caller can request the next
  page without re-deriving it from site-specific conventions.

Search pipeline and where these models fit
------------------------------------------
::

    caller
      │
      │ SearchFilters (optional)
      ▼
    SiteRegistry.search_all(query, filters=...)
      │
      │ per-site dispatch
      ▼
    BaseParser.search(query, *, page=1, filters=...)   ← future extension
      │
      │ returns SearchResultPage (or a sequence of SearchResult)
      ▼
    SiteRegistry yields SearchResult objects one by one
      │
      ▼
    caller (CLI listing / web cards / library import)

The v1 parser signature is ``search(query, *, page=1)`` per the PROMPT
MASTER — **no filters parameter**. :class:`SearchFilters` exists because
``SiteCapabilities.filters`` is a first-class flag on every site config,
and because a future ADR will extend ``BaseParser.search`` to accept it.
Until then, parsers that advertise ``capabilities.filters`` should apply
the filters client-side after fetching the site's unfiltered listing. The
registry passes them through unchanged.

A search result is not a manga
------------------------------
Do not attempt to convert a :class:`SearchResult` into a
:class:`~nexusdl.core.models.manga.Manga`. The required fields on
``Manga`` (``language``, ``status``, ``content_rating``, ``is_manga``)
are not reliably present in search listings. A caller who wants the full
manga must fetch it::

    # Instead of: manga = result.to_manga()   ← does not exist, on purpose
    manga = await parser.get_manga(result.url)

This constraint is intentional. Providing a lossy ``to_manga()`` would
tempt callers to store half-populated rows in the library, and every bug
caused by a missing ``content_rating`` or a defaulted ``language`` would
be traced back to this module.

Deterministic identity
----------------------
:attr:`SearchResult.manga_id` computes the canonical manga identifier from
:attr:`~SearchResult.site_id` and :attr:`~SearchResult.source_id` using
exactly the formula used by every other part of the pipeline::

    sha256(f"{site_id}:{source_id}")[:16]

Two search results from the same site with the same ``source_id`` — a
pagination artefact, a site returning duplicates, a user re-searching —
produce the same ``manga_id`` and can therefore be deduplicated, keyed in
a dict, or referenced in a subsequent API call without a full manga fetch.

Dependency graph (no cycles)::

    core.models.enums  ──► core.models.search

No ``core.library.*``, no ``core.downloader.*``, no I/O. This file could be
lifted into a standalone library without modification.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Final

from nexusdl.core.models.enums import (
    ContentRating,
    Language,
    MangaStatus,
)


__all__ = [
    "SearchFilters",
    "SearchResult",
    "SearchResultPage",
]


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Site identifier pattern, identical to the one in
#: :class:`nexusdl.core.models.site.SiteConfig` and ``sites_schema.json``.
#: Kept as a compiled regex per-module rather than imported to avoid a
#: cross-module dependency for a single pattern — the two definitions are
#: guarded by a unit test that asserts equality.
_SITE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Length of the canonical truncated-sha256 manga id. Same value as
#: ``Manga.id`` and ``Chapter.id`` — see the PROMPT MASTER's ID table.
_MANGA_ID_LENGTH: Final[int] = 16

#: Site URLs and cover URLs must be absolute HTTPS. HTTP is rejected —
#: every source site NexusDL supports serves HTTPS, and accepting HTTP
#: would open a downgrade surface.
_HTTPS_PREFIX: Final[str] = "https://"


# --------------------------------------------------------------------------- #
# SearchFilters                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Requested filters for a search query.

    A **hint**: what the caller wants, not what the site will honour. A
    parser that advertises ``capabilities.filters == True`` may apply these
    server-side (once the future ADR extends ``BaseParser.search`` to
    accept them) or client-side; a parser with ``capabilities.filters ==
    False`` ignores them entirely and returns its unfiltered listing.

    Every field defaults to a "no filter" value, so
    ``SearchFilters()`` is a valid no-op filter that a caller can pass
    unconditionally.

    Attributes:
        status: Restrict to mangas with this publication status. ``None``
            means "any status".
        language: Restrict to mangas in this language. ``None`` means "any
            language". Note that a single site often serves a *primary*
            language (its :attr:`~SiteConfig.language`) — this filter is
            about the manga, not the site.
        year_min: Inclusive lower bound on the manga's release year.
            ``None`` means "no lower bound". Must be ``>= 0``.
        year_max: Inclusive upper bound on the manga's release year.
            ``None`` means "no upper bound". Must be ``>= 0`` and
            ``>= year_min`` when both are set.
        tags: Genre / tag names the caller is interested in. Empty tuple
            means "no tag filter". Must not contain empty strings.
        content_rating: Restrict to mangas with this content rating.
            ``None`` means "any rating". Callers implementing an
            adult-content gate should set this to a non-``PORNOGRAPHIC`` /
            non-``EROTICA`` value **in addition to** filtering on
            :attr:`SiteConfig.adult`.
    """

    status: MangaStatus | None = None
    language: Language | None = None
    year_min: int | None = None
    year_max: int | None = None
    tags: tuple[str, ...] = ()
    content_rating: ContentRating | None = None

    def __post_init__(self) -> None:
        """Validate field types and cross-field consistency.

        Raises:
            TypeError: If a field has the wrong Python type (e.g.
                ``status="ongoing"``, ``tags`` not a tuple).
            ValueError: If ``year_min`` / ``year_max`` is negative,
                ``year_min > year_max``, or ``tags`` contains an empty
                string.
        """
        if self.status is not None and not isinstance(
            self.status, MangaStatus
        ):
            raise TypeError(
                f"SearchFilters.status must be a MangaStatus or None, "
                f"got {type(self.status).__name__}"
            )
        if self.language is not None and not isinstance(
            self.language, Language
        ):
            raise TypeError(
                f"SearchFilters.language must be a Language or None, "
                f"got {type(self.language).__name__}"
            )
        if self.content_rating is not None and not isinstance(
            self.content_rating, ContentRating
        ):
            raise TypeError(
                f"SearchFilters.content_rating must be a ContentRating or "
                f"None, got {type(self.content_rating).__name__}"
            )
        if self.year_min is not None:
            if not isinstance(self.year_min, int) or isinstance(
                self.year_min, bool
            ):
                raise TypeError(
                    f"SearchFilters.year_min must be an int or None, "
                    f"got {type(self.year_min).__name__}"
                )
            if self.year_min < 0:
                raise ValueError(
                    f"SearchFilters.year_min must be >= 0, "
                    f"got {self.year_min}"
                )
        if self.year_max is not None:
            if not isinstance(self.year_max, int) or isinstance(
                self.year_max, bool
            ):
                raise TypeError(
                    f"SearchFilters.year_max must be an int or None, "
                    f"got {type(self.year_max).__name__}"
                )
            if self.year_max < 0:
                raise ValueError(
                    f"SearchFilters.year_max must be >= 0, "
                    f"got {self.year_max}"
                )
        if (
            self.year_min is not None
            and self.year_max is not None
            and self.year_min > self.year_max
        ):
            raise ValueError(
                f"SearchFilters.year_min ({self.year_min}) must be <= "
                f"year_max ({self.year_max})"
            )
        if not isinstance(self.tags, tuple):
            raise TypeError(
                f"SearchFilters.tags must be a tuple, got "
                f"{type(self.tags).__name__}"
            )
        for tag in self.tags:
            if not isinstance(tag, str):
                raise TypeError(
                    f"SearchFilters.tags entries must be str, got "
                    f"{type(tag).__name__}"
                )
            if not tag:
                raise ValueError(
                    "SearchFilters.tags must not contain empty strings"
                )

    @property
    def is_empty(self) -> bool:
        """``True`` iff no filter is set — the filter is a no-op.

        Useful for callers that want to omit the ``filters=`` argument
        entirely when the user has not selected anything, and for tests
        asserting that a parser received a no-op filter set.
        """
        return (
            self.status is None
            and self.language is None
            and self.year_min is None
            and self.year_max is None
            and not self.tags
            and self.content_rating is None
        )


# --------------------------------------------------------------------------- #
# SearchResult                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One manga hit as returned by a site's search endpoint.

    Immutable. Carries only the fields a listing row reliably exposes — see
    the module docstring for why this is not a
    :class:`~nexusdl.core.models.manga.Manga`.

    Attributes:
        site_id: Identifier of the site that returned this hit. Matches the
            :attr:`~nexusdl.core.models.site.SiteConfig.id` of the site and
            is stable for the lifetime of the site.
        source_id: The site's own identifier for this manga — the last path
            segment of the manga URL, a numeric id, a slug, whatever the
            site uses. Opaque to NexusDL; the only constraint is that it
            uniquely identifies the manga **within its site**.
        title: Display title, exactly as returned by the site. Not
            normalised, not translated, not trimmed beyond what
            :meth:`str.strip` would do (which the model does not apply).
        url: Canonical URL of the manga on the site. Absolute, must start
            with ``https://``.
        cover_url: Optional URL of the cover image. When present, must
            start with ``https://``. Sites that lazily load covers may
            return ``None`` even for otherwise-complete listings.
        author: Optional author name, verbatim from the site. Some sites
            return a comma-separated list of authors, others a single
            string, others none. The model does not parse this — callers
            who need structured authors fetch the full manga.
        description: Optional short description or synopsis. The site may
            truncate this in a listing context; the full description
            requires a manga detail fetch.
        language: Optional content language, when the site provides it.
            ``None`` when the site does not expose it and the caller
            should fall back to :attr:`~SiteConfig.language`.
        year: Optional release year, when the site exposes it in the
            listing. ``None`` is common.
        status: Optional publication status. ``None`` is common.
        content_rating: Optional content rating. ``None`` when the site
            does not expose per-manga rating in the listing — callers who
            need it must fetch the full manga. Callers gating adult
            content should treat ``None`` conservatively (assume the
            worst) rather than assuming ``SAFE``.
        relevance: Optional relevance score, when the site exposes one.
            ``None`` means "not scored". Positive floats only; the scale
            is site-defined and not comparable across sites. Consumers
            that want a cross-site ranking must define their own
            normalisation.
    """

    site_id: str
    source_id: str
    title: str
    url: str
    cover_url: str | None = None
    author: str | None = None
    description: str | None = None
    language: Language | None = None
    year: int | None = None
    status: MangaStatus | None = None
    content_rating: ContentRating | None = None
    relevance: float | None = None

    def __post_init__(self) -> None:
        """Validate field types and value constraints.

        Raises:
            TypeError: If a field has the wrong Python type.
            ValueError: If ``site_id`` does not match the site-id pattern,
                ``source_id`` / ``title`` is empty, ``url`` / ``cover_url``
                does not start with ``https://``, ``year`` is negative, or
                ``relevance`` is negative.
        """
        # --- Required strings ---------------------------------------- #
        if not isinstance(self.site_id, str):
            raise TypeError(
                f"SearchResult.site_id must be a str, got "
                f"{type(self.site_id).__name__}"
            )
        if not _SITE_ID_PATTERN.match(self.site_id):
            raise ValueError(
                f"SearchResult.site_id must match "
                f"{_SITE_ID_PATTERN.pattern!r}, got {self.site_id!r}"
            )
        for field_name, value in (
            ("source_id", self.source_id),
            ("title", self.title),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"SearchResult.{field_name} must be a str, got "
                    f"{type(value).__name__}"
                )
            if not value.strip():
                raise ValueError(
                    f"SearchResult.{field_name} must be non-empty"
                )

        # --- URLs ----------------------------------------------------- #
        if not isinstance(self.url, str):
            raise TypeError(
                f"SearchResult.url must be a str, got "
                f"{type(self.url).__name__}"
            )
        if not self.url.startswith(_HTTPS_PREFIX):
            raise ValueError(
                f"SearchResult.url must start with {_HTTPS_PREFIX!r}, "
                f"got {self.url!r}"
            )
        if self.cover_url is not None:
            if not isinstance(self.cover_url, str):
                raise TypeError(
                    f"SearchResult.cover_url must be a str or None, got "
                    f"{type(self.cover_url).__name__}"
                )
            if not self.cover_url.startswith(_HTTPS_PREFIX):
                raise ValueError(
                    f"SearchResult.cover_url must start with "
                    f"{_HTTPS_PREFIX!r}, got {self.cover_url!r}"
                )

        # --- Optional strings ---------------------------------------- #
        for field_name, value in (
            ("author", self.author),
            ("description", self.description),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(
                    f"SearchResult.{field_name} must be a str or None, "
                    f"got {type(value).__name__}"
                )

        # --- Optional enums ------------------------------------------ #
        if self.language is not None and not isinstance(
            self.language, Language
        ):
            raise TypeError(
                f"SearchResult.language must be a Language or None, "
                f"got {type(self.language).__name__}"
            )
        if self.status is not None and not isinstance(
            self.status, MangaStatus
        ):
            raise TypeError(
                f"SearchResult.status must be a MangaStatus or None, "
                f"got {type(self.status).__name__}"
            )
        if self.content_rating is not None and not isinstance(
            self.content_rating, ContentRating
        ):
            raise TypeError(
                f"SearchResult.content_rating must be a ContentRating or "
                f"None, got {type(self.content_rating).__name__}"
            )

        # --- Optional numeric ---------------------------------------- #
        if self.year is not None:
            if not isinstance(self.year, int) or isinstance(self.year, bool):
                raise TypeError(
                    f"SearchResult.year must be an int or None, got "
                    f"{type(self.year).__name__}"
                )
            if self.year < 0:
                raise ValueError(
                    f"SearchResult.year must be >= 0, got {self.year}"
                )
        if self.relevance is not None:
            if not isinstance(self.relevance, (int, float)) or isinstance(
                self.relevance, bool
            ):
                raise TypeError(
                    f"SearchResult.relevance must be a number or None, "
                    f"got {type(self.relevance).__name__}"
                )
            if self.relevance < 0:
                raise ValueError(
                    f"SearchResult.relevance must be >= 0, "
                    f"got {self.relevance}"
                )

    # ------------------------------------------------------------------ #
    # Derived identity                                                   #
    # ------------------------------------------------------------------ #

    @property
    def manga_id(self) -> str:
        """Return the canonical 16-hex-char manga identifier.

        Computed deterministically from :attr:`site_id` and
        :attr:`source_id` using the formula shared by every model in the
        pipeline: ``sha256(f"{site_id}:{source_id}")[:16]``. Two search
        results for the same manga from the same site always produce the
        same ``manga_id``, so callers can deduplicate, key by id, or
        reference the manga in a subsequent API call without fetching it
        first.
        """
        return hashlib.sha256(
            f"{self.site_id}:{self.source_id}".encode("utf-8")
        ).hexdigest()[:_MANGA_ID_LENGTH]

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the result.

        Renders enums as their string values (``"ONGOING"``, ``"EN"``, …)
        so the output is directly consumable by a FastAPI handler without
        further transformation. Optional fields that are ``None`` are
        preserved as ``None``, not omitted — the consumer can distinguish
        "field not provided by the site" from "field provided and empty",
        which matters for UIs that render a placeholder for unknown
        fields.

        The ``manga_id`` field is included as a convenience so that
        serialisers do not need to recompute it.

        Returns:
            A dict with every public field plus the computed
            ``manga_id``.
        """
        return {
            "manga_id": self.manga_id,
            "site_id": self.site_id,
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "cover_url": self.cover_url,
            "author": self.author,
            "description": self.description,
            "language": (
                self.language.value if self.language is not None else None
            ),
            "year": self.year,
            "status": (
                self.status.value if self.status is not None else None
            ),
            "content_rating": (
                self.content_rating.value
                if self.content_rating is not None
                else None
            ),
            "relevance": self.relevance,
        }


# --------------------------------------------------------------------------- #
# SearchResultPage                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SearchResultPage:
    """One site's paginated response to a single search call.

    Immutable. The page carries every piece of state a caller needs to
    request the next page — the caller does **not** need to know the
    site's pagination convention.

    Attributes:
        site_id: Identifier of the site that produced this page. Every
            result in :attr:`items` must carry this same ``site_id``.
        query: The raw query string the page was produced for. Stored
            verbatim so a future "request next page" implementation can
            forward it unchanged. Not normalised — sites sometimes apply
            their own tokenisation, and re-normalising risks drift.
        items: The page's results, in the site's own order (typically
            relevance-descending or date-descending, depending on the
            site). Empty is valid and means "no matches".
        page: The 1-indexed page number this response represents. Must be
            ``>= 1``.
        next_page: The page number to request to obtain the next page, or
            ``None`` when this is the last page. When set, must be
            strictly greater than :attr:`page`.
        total: Optional total number of results across all pages, when
            the site reports one. ``None`` means "unknown", not zero.
            Must be ``>= 0`` when set.
    """

    site_id: str
    query: str
    items: tuple[SearchResult, ...]
    page: int = 1
    next_page: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        """Validate field types, cross-field consistency, and item site ids.

        Raises:
            TypeError: If a field has the wrong Python type.
            ValueError: If ``site_id`` does not match the site-id pattern,
                ``query`` is empty, ``items`` contains a result from a
                different site, ``page`` is ``< 1``, ``next_page <= page``,
                or ``total`` is negative.
        """
        if not isinstance(self.site_id, str):
            raise TypeError(
                f"SearchResultPage.site_id must be a str, got "
                f"{type(self.site_id).__name__}"
            )
        if not _SITE_ID_PATTERN.match(self.site_id):
            raise ValueError(
                f"SearchResultPage.site_id must match "
                f"{_SITE_ID_PATTERN.pattern!r}, got {self.site_id!r}"
            )
        if not isinstance(self.query, str):
            raise TypeError(
                f"SearchResultPage.query must be a str, got "
                f"{type(self.query).__name__}"
            )
        if not self.query:
            raise ValueError("SearchResultPage.query must be non-empty")
        if not isinstance(self.items, tuple):
            raise TypeError(
                f"SearchResultPage.items must be a tuple, got "
                f"{type(self.items).__name__}"
            )
        for item in self.items:
            if not isinstance(item, SearchResult):
                raise TypeError(
                    "SearchResultPage.items must contain SearchResult "
                    f"instances, got {type(item).__name__}"
                )
            if item.site_id != self.site_id:
                raise ValueError(
                    f"SearchResultPage.items contains a result for site "
                    f"{item.site_id!r}, but the page's site_id is "
                    f"{self.site_id!r}"
                )
        if not isinstance(self.page, int) or isinstance(self.page, bool):
            raise TypeError(
                f"SearchResultPage.page must be an int, got "
                f"{type(self.page).__name__}"
            )
        if self.page < 1:
            raise ValueError(
                f"SearchResultPage.page must be >= 1, got {self.page}"
            )
        if self.next_page is not None:
            if not isinstance(self.next_page, int) or isinstance(
                self.next_page, bool
            ):
                raise TypeError(
                    f"SearchResultPage.next_page must be an int or None, "
                    f"got {type(self.next_page).__name__}"
                )
            if self.next_page <= self.page:
                raise ValueError(
                    f"SearchResultPage.next_page ({self.next_page}) must "
                    f"be > page ({self.page})"
                )
        if self.total is not None:
            if not isinstance(self.total, int) or isinstance(
                self.total, bool
            ):
                raise TypeError(
                    f"SearchResultPage.total must be an int or None, got "
                    f"{type(self.total).__name__}"
                )
            if self.total < 0:
                raise ValueError(
                    f"SearchResultPage.total must be >= 0, got {self.total}"
                )

    # ------------------------------------------------------------------ #
    # Convenience                                                        #
    # ------------------------------------------------------------------ #

    @property
    def count(self) -> int:
        """Number of results on this page — ``len(self.items)``.

        Provided as a named property rather than making callers write
        ``len(page.items)`` at every call site, which improves readability
        of pagination-related logic.
        """
        return len(self.items)

    @property
    def is_empty(self) -> bool:
        """``True`` iff this page contains no results."""
        return not self.items

    @property
    def has_more(self) -> bool:
        """``True`` iff a subsequent page can be requested.

        Equivalent to ``self.next_page is not None``. Exposed as a named
        property because most callers care about this boolean rather than
        about the page number itself.
        """
        return self.next_page is not None
