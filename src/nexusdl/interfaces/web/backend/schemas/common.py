"""Common query-parameter schemas shared across HTTP endpoints.

Symmetric to :mod:`schemas.requests` (request **bodies**) and
:mod:`schemas.responses` (response bodies): this module describes the
**query-string** parameters that are shared by more than one endpoint.

Design notes
============

* **No FastAPI import.** Query parameter models are Pydantic v2 models
  whose fields are read from the query string when used via
  ``Annotated[X, Depends()]`` in a FastAPI router. They can also be
  validated independently (``X.model_validate({...})``) — useful for
  tests and for transports that are not FastAPI.

* **Composition, not inheritance.** :class:`PaginationParams`,
  :class:`SortParams`, and the resource-specific query models are
  independent. A router composes them:

  .. code-block:: python

      @router.get("/api/v1/library")
      async def list_library(
          pagination: Annotated[PaginationParams, Depends()],
          sort: Annotated[SortParams, Depends()],
          filters: Annotated[LibraryQueryParams, Depends()],
      ): ...

  Adding a filter is one new field on the resource query model — never a
  change to the pagination or sort schemas.

* **Strict on shape, permissive on transport.** ``extra="forbid"`` applies
  when a model is validated directly. When used via ``Depends()``,
  FastAPI ignores unknown query parameters — this is intentional
  forward-compatibility (a client sending ``?foo=bar`` does not break).

* **The ``sort`` field is validated for *shape* only.** Whether the field
  is actually sortable for a given resource is a business concern owned
  by the router (e.g. ``sort_field = sort.sort or "added_at"``). This
  keeps :class:`SortParams` free of resource-specific knowledge.

* **Time ranges are inclusive.** :class:`TimeRangeParams` rejects
  ``since > until`` up front, before any service sees the values.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from nexusdl.core.models.enums import (
    DownloadStatus,
    Language,
    MangaStatus,
)

__all__ = [
    # Constants
    "DEFAULT_PAGE",
    "DEFAULT_PER_PAGE",
    "MAX_PER_PAGE",
    # Enums
    "SortOrder",
    # Base
    "QueryParams",
    # Query parameter models
    "PaginationParams",
    "SortParams",
    "TimeRangeParams",
    "LibraryQueryParams",
    "DownloadQueryParams",
    "ChapterQueryParams",
    # Helpers
    "parse_bool_query",
    "parse_csv_query",
    "parse_enum_csv",
    "paginate",
    "clamp_page",
]


# ===========================================================================
# Constants
# ===========================================================================

#: Default page index (1-based).
DEFAULT_PAGE: int = 1

#: Default page size when the client omits ``per_page``.
DEFAULT_PER_PAGE: int = 20

#: Hard cap on ``per_page`` — enforced here and mirrored by
#: :data:`schemas.responses.MAX_PAGE_SIZE`. Kept identical so requests and
#: responses can never disagree.
MAX_PER_PAGE: int = 100


# ===========================================================================
# Enums
# ===========================================================================

class SortOrder(str, Enum):
    """Sort direction for :class:`SortParams`."""

    ASC = "asc"
    DESC = "desc"


# ===========================================================================
# Base
# ===========================================================================

class QueryParams(BaseModel):
    """Base class for query parameter groups.

    Subclasses are typically used in FastAPI routers as
    ``Annotated[MyParams, Depends()]``. Every Pydantic field becomes one
    query parameter (with its default and constraints).

    ``extra="forbid"`` prevents typos when the model is validated directly
    (``MyParams.model_validate({...})``). Via ``Depends()``, FastAPI
    silently ignores unknown query parameters — the practical effect of
    ``extra="forbid"`` there is limited, but it does not hurt and
    documents the strictness intent.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# ===========================================================================
# Reusable query parameter models
# ===========================================================================

class PaginationParams(QueryParams):
    """Standard pagination parameters.

    Provides :attr:`offset` and :attr:`limit` properties so service-layer
    code can pass them straight to a database query without re-deriving
    from ``page``/``per_page``.
    """

    page: int = Field(
        default=DEFAULT_PAGE,
        ge=1,
        description="1-based page index.",
        examples=[1],
    )
    per_page: int = Field(
        default=DEFAULT_PER_PAGE,
        ge=1,
        le=MAX_PER_PAGE,
        description=f"Page size, 1-{MAX_PER_PAGE}.",
        examples=[20],
    )

    @property
    def offset(self) -> int:
        """SQL-style offset derived from ``page`` and ``per_page``."""
        return (self.page - 1) * self.per_page

    @property
    def limit(self) -> int:
        """Alias for ``per_page`` — symmetric with :attr:`offset`."""
        return self.per_page


class SortParams(QueryParams):
    """Sort field + direction.

    The :attr:`sort` field is validated for **shape only** (max length,
    trimmed, ``None`` when empty). Whether the field actually exists on
    the resource is a business concern owned by the router — see the
    module docstring.
    """

    sort: str | None = Field(
        default=None,
        max_length=64,
        description="Field name to sort by. ``null`` means 'use the "
                    "resource default' (declared at the router).",
        examples=["added_at"],
    )
    order: SortOrder = Field(
        default=SortOrder.DESC,
        description="Sort direction.",
        examples=[SortOrder.DESC.value],
    )

    @field_validator("sort")
    @classmethod
    def _normalize_sort(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @property
    def descending(self) -> bool:
        """Convenience boolean for service-layer use."""
        return self.order == SortOrder.DESC


class TimeRangeParams(QueryParams):
    """Inclusive time range for history-style endpoints.

    Rejects ``since > until`` up front so services can trust the range.
    Both bounds are optional; a one-sided range is valid.
    """

    since: datetime | None = Field(
        default=None,
        description="Inclusive lower bound (ISO 8601).",
    )
    until: datetime | None = Field(
        default=None,
        description="Inclusive upper bound (ISO 8601).",
    )

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.since is not None and self.until is not None:
            if self.since > self.until:
                raise ValueError(
                    "'since' must be earlier than or equal to 'until'"
                )
        return self


class LibraryQueryParams(QueryParams):
    """Resource-specific filters for ``GET /api/v1/library``.

    ``filters`` is the primary data-driven mechanism: the client sends the
    ids of the filter chips it has activated, and the service maps them to
    DB predicates. ``manga_status`` / ``language`` / ``search`` are
    convenience shortcuts for the most common filters.
    """

    filters: list[str] = Field(
        default_factory=list,
        description="Filter ids (data-driven, client-defined).",
        examples=[["unread", "reading"]],
    )
    manga_status: MangaStatus | None = Field(
        default=None,
        description="Filter by publication status.",
    )
    language: Language | None = Field(
        default=None,
        description="Filter by source language.",
    )
    search: str = Field(
        default="",
        max_length=256,
        description="Free-text search within the library.",
    )

    @field_validator("filters")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for f in v:
            f = f.strip()
            if f and f not in seen:
                seen.add(f)
                out.append(f)
        return out

    @field_validator("search")
    @classmethod
    def _trim_search(cls, v: str) -> str:
        return v.strip()


class DownloadQueryParams(QueryParams):
    """Resource-specific filters for ``GET /api/v1/download``."""

    status: list[DownloadStatus] = Field(
        default_factory=list,
        description="Filter by status. Empty list = all statuses.",
        examples=[["running", "pending"]],
    )
    manga_id: str | None = Field(
        default=None,
        description="Restrict to tasks belonging to this manga.",
    )
    chapter_id: str | None = Field(
        default=None,
        description="Restrict to tasks belonging to this chapter.",
    )

    @field_validator("status")
    @classmethod
    def _dedupe(cls, v: list[DownloadStatus]) -> list[DownloadStatus]:
        seen: set[DownloadStatus] = set()
        out: list[DownloadStatus] = []
        for s in v:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out


class ChapterQueryParams(QueryParams):
    """Resource-specific filters for ``GET /api/v1/manga/{id}/chapters``."""

    language: Language | None = Field(
        default=None,
        description="Filter by chapter language.",
    )
    downloaded: bool | None = Field(
        default=None,
        description="``true`` = only downloaded, ``false`` = only not "
                    "downloaded, ``null`` = all.",
    )


# ===========================================================================
# Helpers — query-string parsing
# ===========================================================================
#
# FastAPI parses standard typed query params natively. These helpers are
# for the rarer cases where a router reads raw query strings
# (``request.query_params.get(...)``) or normalizes values from a
# non-FastAPI transport.

_TRUE_STRINGS = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "n", "off"})


def parse_bool_query(
    value: str | bool | None, *, default: bool = False
) -> bool:
    """Parse a truthy/falsy query-string value.

    Accepts ``true/false``, ``1/0``, ``yes/no``, ``y/n``, ``on/off``
    (case-insensitive). ``None`` returns ``default``.

    Raises:
        ValueError: If ``value`` is a string that is not in the accepted
            set.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    v = value.strip().lower()
    if v in _TRUE_STRINGS:
        return True
    if v in _FALSE_STRINGS:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def parse_csv_query(value: str | None) -> list[str]:
    """Split ``?tag=a,b,c`` into ``["a", "b", "c"]``.

    Empty items are dropped; surrounding whitespace is stripped. ``None``
    or an empty string yields ``[]``.
    """
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_enum_csv(
    value: str | None, enum_cls: type[Enum]
) -> list[Any]:
    """Split ``?status=running,pending`` and validate each item against
    ``enum_cls``.

    Raises:
        ValueError: If any item is not a valid member of ``enum_cls``.
            The message lists the allowed values, so the client gets a
            useful error without a round-trip.
    """
    items = parse_csv_query(value)
    out: list[Any] = []
    for raw in items:
        try:
            out.append(enum_cls(raw))
        except ValueError as exc:
            allowed = ", ".join(str(m.value) for m in enum_cls)
            raise ValueError(
                f"Invalid {enum_cls.__name__} value {raw!r}. "
                f"Allowed: {allowed}"
            ) from exc
    return out


# ===========================================================================
# Helpers — pagination in memory
# ===========================================================================
#
# For services that already have the full collection in memory (settings,
# sites, plugins, …) and prefer not to hit the DB. Do **not** use these for
# real database pagination — pass ``offset``/``limit`` to the query instead.

def paginate(
    items: Iterable[Any], params: PaginationParams
) -> list[Any]:
    """Slice ``items`` according to ``params``.

    Synchronous and non-streaming — intended for small collections only.
    For large collections, use ``params.offset`` and ``params.limit``
    directly in the database query.

    Args:
        items: The full collection.
        params: The pagination parameters.

    Returns:
        The slice of ``items`` corresponding to the requested page.
    """
    all_items = items if isinstance(items, list) else list(items)
    start = params.offset
    end = start + params.per_page
    return all_items[start:end]


def clamp_page(page: int, total: int, per_page: int) -> int:
    """Clamp ``page`` to the valid range for ``total`` items.

    Returns ``1`` when ``total <= 0``. Useful when a client requests
    ``page=999`` on a 3-page collection — the service returns the last
    valid page instead of an empty result, which is friendlier UX and
    avoids an erroneous 404.

    Args:
        page: The requested page (1-based).
        total: Total number of items across all pages.
        per_page: Page size.

    Returns:
        The closest valid page index.
    """
    if total <= 0 or per_page <= 0:
        return 1
    total_pages = max(1, (total + per_page - 1) // per_page)
    return max(1, min(page, total_pages))
