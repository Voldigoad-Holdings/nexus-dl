"""HTTP response schemas for the NexusDL backend.

Pydantic v2 models describing every successful HTTP response and the
standard error envelope. Independent from :mod:`schemas.ws` — WebSocket
and HTTP are distinct contracts that only share the domain enums from
:mod:`nexusdl.core.models.enums`.

Design notes
============

* **Generics-first pagination.** :class:`PaginatedResponse` is a generic
  Pydantic model parameterised over the item type. ``total_pages``,
  ``has_next`` and ``has_prev`` are :func:`pydantic.computed_field` —
  computed from ``total``/``page``/``per_page`` and impossible to become
  inconsistent.
* **Standard error envelope.** :class:`ErrorResponse` matches the API
  contract exactly: ``{"error": {"code", "message", "details",
  "request_id"}}``. Use :func:`error_response` to build one without
  manual nesting.
* **Strict envelopes, permissive payloads.** Envelope models use
  ``extra="forbid"`` (a typo in a field name is a bug, not a feature).
  Domain payloads use ``extra="allow"`` so plugins and future versions
  can extend them without breaking old clients.
* **``from_attributes=True`` everywhere.** Domain objects
  (:class:`~nexusdl.core.models.manga.Manga`,
  :class:`~nexusdl.core.models.chapter.Chapter`, …) can be passed
  directly to ``Schema.model_validate(obj)``.
* **Secrets are never leaked.** :class:`SettingFieldSchema.current_value`
  is ``None`` for secret fields — only ``is_set: bool`` is exposed.
  :class:`SiteSchema` carries cookie *names*, never values.
* **Open error code vocabulary.** :class:`ErrorCode` exists for
  autocompletion and documentation; :attr:`ErrorBody.code` is typed as
  ``str`` so plugins can introduce new codes.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

from nexusdl.core.models.enums import (
    ContentRating,
    DownloadStatus,
    Language,
    MangaStatus,
    PackagingFormat,
    Priority,
    SiteHealth,
    UserRole,
)

__all__ = [
    # Constants
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "API_VERSION",
    # Error codes
    "ErrorCode",
    # Base / common
    "ErrorBody",
    "ErrorResponse",
    "MessageResponse",
    "ResourceIdResponse",
    "PaginatedResponse",
    "error_response",
    # Health / version
    "ComponentHealth",
    "HealthResponse",
    "ReadinessResponse",
    "VersionResponse",
    # Auth
    "UserInfo",
    "TokenResponse",
    "LoginResponse",
    "RefreshResponse",
    # Sites
    "SiteCapabilitiesSchema",
    "SiteSchema",
    "SiteListResponse",
    "SiteHealthSchema",
    "SiteHealthResponse",
    # Manga / chapters / pages
    "MangaSummarySchema",
    "MangaDetailSchema",
    "ChapterSummarySchema",
    "PageSchema",
    "ChapterListResponse",
    "PageListResponse",
    # Search
    "SearchResponse",
    # Downloads
    "DownloadTaskSchema",
    "DownloadTaskListResponse",
    "DownloadSubmitResponse",
    # Library
    "ReadingProgressSchema",
    "LibraryEntrySchema",
    "LibraryListResponse",
    # Settings
    "SettingFieldType",
    "SettingFieldSchema",
    "SettingsSectionSchema",
    "SettingsResponse",
    "SettingsUpdateResponse",
]


# ===========================================================================
# Constants
# ===========================================================================

#: Default page size when the request omits ``per_page``.
DEFAULT_PAGE_SIZE: int = 20

#: Hard cap enforced by the request layer; responses never exceed it.
MAX_PAGE_SIZE: int = 100

#: Version of the HTTP API contract — independent from the package version.
API_VERSION: str = "v1"


# ===========================================================================
# Error codes
# ===========================================================================

class ErrorCode(str, Enum):
    """Well-known error codes returned in :attr:`ErrorBody.code`.

    Open vocabulary — plugins and future features may introduce new codes.
    The router always emits ``str(code)`` so this enum exists only for
    autocompletion and grep-ability.
    """

    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    SITE_UNAVAILABLE = "SITE_UNAVAILABLE"
    SITE_BLOCKED = "SITE_BLOCKED"
    PARSER_ERROR = "PARSER_ERROR"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    PACKAGING_ERROR = "PACKAGING_ERROR"
    LIBRARY_ERROR = "LIBRARY_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# ===========================================================================
# Base model classes
# ===========================================================================

class _ResponseModel(BaseModel):
    """Base for every *envelope* response.

    Strict protocol: ``extra="forbid"``, frozen, ``from_attributes=True``.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )


class _DomainModel(BaseModel):
    """Base for *domain* payload models nested under an envelope.

    Allows extra fields: plugins and forward-compatible versions can
    extend a manga / site / chapter schema without breaking clients.
    """

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )


#: Generic item type used by :class:`PaginatedResponse`.
T = TypeVar("T")


# ===========================================================================
# Common envelopes
# ===========================================================================

class ErrorBody(_ResponseModel):
    """The ``error`` body returned on failure."""

    code: str = Field(
        description="Stable SCREAMING_SNAKE_CASE error code. "
                    "See :class:`ErrorCode` for the standard set.",
    )
    message: str = Field(description="Human-readable message, safe to display.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured details (validation errors, etc.).",
    )
    request_id: str | None = Field(
        default=None,
        description="Correlation id echoed from the request headers or "
                    "generated by the server.",
    )


class ErrorResponse(_ResponseModel):
    """Standard error envelope returned by every non-2xx HTTP response."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        json_schema_extra={
            "example": {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request body.",
                    "details": {
                        "fields": {
                            "query": "field required",
                        }
                    },
                    "request_id": "req-7a2b4f6e",
                }
            }
        },
    )

    error: ErrorBody


class MessageResponse(_ResponseModel):
    """Generic confirmation message for actions without a payload."""

    message: str = Field(description="Human-readable confirmation.")
    details: dict[str, Any] = Field(default_factory=dict)


class ResourceIdResponse(_ResponseModel):
    """Response carrying the id of a freshly created resource."""

    id: str = Field(description="Id of the created resource.")
    message: str = ""


class PaginatedResponse(_ResponseModel, Generic[T]):
    """Generic paginated envelope used by every list endpoint.

    Fields:

    * ``items`` — the current page's items.
    * ``total`` — total number of items across all pages.
    * ``page`` — 1-based page index.
    * ``per_page`` — page size.
    * ``total_pages`` — computed from ``total`` / ``per_page``.
    * ``has_next`` / ``has_prev`` — computed from ``page`` / ``total_pages``.
    """

    items: list[T] = Field(description="Items in the current page.")
    total: int = Field(ge=0, description="Total number of items across all pages.")
    page: int = Field(ge=1, description="1-based page index.")
    per_page: int = Field(
        ge=1, le=MAX_PAGE_SIZE, description="Page size (max 100)."
    )

    @computed_field
    @property
    def total_pages(self) -> int:
        """Number of pages. Zero when the collection is empty."""
        if self.total <= 0:
            return 0
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @computed_field
    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @computed_field
    @property
    def has_prev(self) -> bool:
        return self.page > 1


def error_response(
    code: str | ErrorCode,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ErrorResponse:
    """Build a validated :class:`ErrorResponse`.

    Accepts either a raw ``str`` (for plugins) or an :class:`ErrorCode`
    member (for the standard set).
    """
    return ErrorResponse(
        error=ErrorBody(
            code=str(getattr(code, "value", code)),
            message=message,
            details=details or {},
            request_id=request_id,
        )
    )


# ===========================================================================
# Health / readiness / version
# ===========================================================================

class ComponentHealth(_ResponseModel):
    """Health of a single backend component."""

    name: str = Field(description="Component identifier (`library`, `redis`, …).")
    status: SiteHealth = Field(
        description="Current status. Reuses :class:`SiteHealth` — the "
                    "four-level vocabulary (healthy/degraded/down/unknown) "
                    "is generic."
    )
    message: str | None = Field(default=None)
    latency_ms: float | None = Field(
        default=None, ge=0.0,
        description="Probe latency, in milliseconds.",
    )


class HealthResponse(_ResponseModel):
    """Liveness probe — the process is running and can respond.

    Never fails if the process is alive. Use :class:`ReadinessResponse`
    for dependency checks.
    """

    status: Literal["healthy", "degraded", "down"]
    version: str = Field(description="Package version, e.g. `3.0.0`.")
    api_version: str = Field(
        default=API_VERSION, description="HTTP API contract version."
    )
    uptime_seconds: float = Field(ge=0.0)
    timestamp: datetime


class ReadinessResponse(_ResponseModel):
    """Readiness probe — the process can serve traffic.

    Returns 200 with ``ready=true`` or 503 with ``ready=false``. The
    ``components`` list explains *why* if not ready.
    """

    ready: bool
    components: list[ComponentHealth] = Field(default_factory=list)
    timestamp: datetime


class VersionResponse(_ResponseModel):
    """Version and build metadata for the running backend."""

    version: str
    api_version: str = API_VERSION
    python_version: str = Field(description="`sys.version` string.")
    git_commit: str | None = Field(default=None)
    build_date: datetime | None = Field(default=None)


# ===========================================================================
# Auth
# ===========================================================================

class UserInfo(_ResponseModel):
    """Public user information. Never includes secrets or hashes."""

    id: str
    username: str
    role: UserRole = UserRole.USER
    email: str | None = Field(default=None)
    created_at: datetime | None = Field(default=None)


class TokenResponse(_ResponseModel):
    """Token pair returned by login and refresh."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "access_expires_in": 900,
                "refresh_expires_in": 604800,
                "token_type": "Bearer",
            }
        },
    )

    access_token: str
    refresh_token: str
    access_expires_in: int = Field(ge=1)
    refresh_expires_in: int = Field(ge=1)
    token_type: str = "Bearer"

    @classmethod
    def from_pair(cls, pair: Any) -> TokenResponse:
        """Build from a :class:`~..security.TokenPair` dataclass.

        Duck-typed via ``to_dict()`` — no import of ``security`` needed
        (avoids the circular dependency, allows any compatible producer).
        """
        return cls.model_validate(pair.to_dict())


class LoginResponse(_ResponseModel):
    """Successful login response — tokens + user profile."""

    user: UserInfo
    token: TokenResponse


class RefreshResponse(_ResponseModel):
    """Successful token-refresh response — new token pair only."""

    token: TokenResponse


# ===========================================================================
# Sites
# ===========================================================================

class SiteCapabilitiesSchema(_DomainModel):
    """Feature flags declared by a site.

    ``extra="allow"`` so future capabilities can be added without breaking
    older clients.
    """

    supports_search: bool = True
    supports_download: bool = True
    supports_library_scan: bool = False
    supports_reading: bool = False
    supports_adult: bool = False
    requires_auth: bool = False
    requires_js: bool = False
    uses_cloudflare: bool = False
    uses_flaresolverr: bool = False


class SiteSchema(_DomainModel):
    """Public site configuration.

    **Never** exposes cookie values, credentials, or scraped selectors.
    ``cookies_required`` carries cookie **names** only.
    """

    id: str
    name: str
    domains: list[str] = Field(default_factory=list)
    language: Language = Language.UNKNOWN
    adult: bool = False
    capabilities: SiteCapabilitiesSchema = Field(
        default_factory=SiteCapabilitiesSchema
    )
    cookies_required: list[str] = Field(
        default_factory=list,
        description="Names of cookies this site requires, if any. Values "
                    "are never exposed.",
    )
    categories: list[str] = Field(default_factory=list)
    max_concurrent_downloads: int = Field(default=1, ge=1, le=32)
    default_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Non-secret default headers (User-Agent, Accept…).",
    )


class SiteListResponse(_ResponseModel):
    """Flat (non-paginated) list of sites — there are ~60 of them."""

    items: list[SiteSchema]
    total: int = Field(ge=0)


class SiteHealthSchema(_ResponseModel):
    """Runtime health of a site."""

    site_id: str
    status: SiteHealth = SiteHealth.UNKNOWN
    latency_ms: float | None = Field(default=None, ge=0.0)
    last_checked_at: datetime | None = Field(default=None)
    last_error: str | None = Field(default=None)
    error_count_24h: int = Field(default=0, ge=0)


class SiteHealthResponse(_ResponseModel):
    """Response for ``GET /api/v1/sites/{id}/health``."""

    health: SiteHealthSchema


# ===========================================================================
# Manga / chapters / pages
# ===========================================================================

class ReadingProgressSchema(_DomainModel):
    """Reading progress for a manga.

    ``chapter_id`` is ``None`` when the user has never opened the manga.
    """

    chapter_id: str | None = Field(default=None)
    chapter_number: str | None = Field(default=None)
    page_index: int = Field(default=0, ge=0)
    page_count: int | None = Field(default=None, ge=0)
    percent: float = Field(default=0.0, ge=0.0, le=1.0)
    last_read_at: datetime | None = Field(default=None)


class MangaSummarySchema(_DomainModel):
    """Lightweight manga projection for search results and lists."""

    id: str
    title: str
    cover_url: str | None = Field(default=None)
    author: str | None = Field(default=None)
    status: MangaStatus = MangaStatus.UNKNOWN
    language: Language = Language.UNKNOWN
    content_rating: ContentRating = ContentRating.SAFE
    site_id: str = ""
    site_name: str = ""
    chapter_count: int | None = Field(default=None, ge=0)
    in_library: bool = False


class MangaDetailSchema(_DomainModel):
    """Full manga detail.

    Returned by ``GET /api/v1/manga/{id}``. Includes library metadata
    inline so the detail page can render in a single round-trip.
    """

    id: str
    title: str
    alt_titles: list[str] = Field(default_factory=list)
    cover_url: str | None = Field(default=None)
    author: str | None = Field(default=None)
    artist: str | None = Field(default=None)
    description: str | None = Field(default=None)
    status: MangaStatus = MangaStatus.UNKNOWN
    language: Language = Language.UNKNOWN
    content_rating: ContentRating = ContentRating.SAFE
    site_id: str = ""
    site_name: str = ""
    source_url: str | None = Field(default=None)
    chapter_count: int | None = Field(default=None, ge=0)
    genres: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1900, le=2200)

    # Library metadata (populated only if the manga is in the library).
    in_library: bool = False
    downloaded_chapters: int = Field(default=0, ge=0)
    total_size_bytes: int | None = Field(default=None, ge=0)
    reading_progress: ReadingProgressSchema = Field(
        default_factory=ReadingProgressSchema
    )

    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)


class ChapterSummarySchema(_DomainModel):
    """Chapter projection used in list endpoints."""

    id: str
    manga_id: str
    number: str = Field(description="Display string — `1`, `12.5`, `Bonus`…")
    title: str = ""
    page_count: int | None = Field(default=None, ge=0)
    released_at: datetime | None = Field(default=None)
    source_url: str | None = Field(default=None)
    language: Language = Language.UNKNOWN
    scanlator: str | None = Field(default=None)

    # Status of the *local* copy, if any.
    downloaded: bool = False
    reading_progress: float = Field(default=0.0, ge=0.0, le=1.0)


class PageSchema(_DomainModel):
    """A single page within a chapter."""

    index: int = Field(ge=0, description="0-based page index.")
    url: str = Field(description="Source URL (may require auth cookies).")
    filename: str | None = Field(default=None)
    size_bytes: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class ChapterListResponse(PaginatedResponse[ChapterSummarySchema]):
    """Paginated list of chapters, with the parent manga id for clarity."""

    manga_id: str


class PageListResponse(_ResponseModel):
    """All pages of a chapter, in order. Not paginated."""

    manga_id: str
    chapter_id: str
    pages: list[PageSchema]
    total: int = Field(ge=0)


# ===========================================================================
# Search
# ===========================================================================

class SearchResponse(PaginatedResponse[MangaSummarySchema]):
    """Response for ``POST /api/v1/search``."""

    query: str = Field(default="", description="Echo of the request query.")
    sites_queried: list[str] = Field(
        default_factory=list,
        description="Ids of the sites that were actually queried.",
    )
    sites_failed: list[str] = Field(
        default_factory=list,
        description="Ids of sites that failed to respond (best-effort search).",
    )


# ===========================================================================
# Downloads
# ===========================================================================

class DownloadTaskSchema(_DomainModel):
    """A single download task, as returned by ``GET /api/v1/download``."""

    task_id: str
    manga_id: str | None = Field(default=None)
    manga_title: str = ""
    chapter_id: str | None = Field(default=None)
    chapter_label: str = ""
    site_id: str = ""
    site_name: str = ""

    status: DownloadStatus = DownloadStatus.PENDING
    priority: Priority = Priority.NORMAL

    completed: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    bytes_completed: int = Field(default=0, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    speed_bps: float | None = Field(default=None, ge=0.0)
    eta_seconds: float | None = Field(default=None, ge=0.0)

    output_format: PackagingFormat | None = Field(default=None)
    output_path: str | None = Field(default=None)
    error: str | None = Field(default=None)

    created_at: datetime | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)


class DownloadTaskListResponse(_ResponseModel):
    """Response for ``GET /api/v1/download``.

    Flat (not paginated) — the queue is small and clients usually want
    the whole picture. ``counts`` pre-aggregates by status so the UI can
    render badges without a full scan.
    """

    items: list[DownloadTaskSchema]
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Pre-aggregated counts by status "
                    "(`running`, `pending`, `paused`, `done`, `failed`, "
                    "`cancelled`).",
    )
    queue_paused: bool = False


class DownloadSubmitResponse(_ResponseModel):
    """Response for ``POST /api/v1/download`` — 202 Accepted."""

    task_id: str
    accepted: bool = True
    queue_position: int | None = Field(default=None, ge=0)
    estimated_start_at: datetime | None = Field(default=None)
    message: str = ""


# ===========================================================================
# Library
# ===========================================================================

class LibraryEntrySchema(_DomainModel):
    """A library entry — manga summary + local metadata."""

    manga: MangaSummarySchema
    downloaded_chapters: int = Field(default=0, ge=0)
    total_size_bytes: int | None = Field(default=None, ge=0)
    reading_progress: ReadingProgressSchema = Field(
        default_factory=ReadingProgressSchema
    )
    added_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)


class LibraryListResponse(PaginatedResponse[LibraryEntrySchema]):
    """Response for ``GET /api/v1/library``."""


# ===========================================================================
# Settings
# ===========================================================================

class SettingFieldType(str, Enum):
    """Type of a settings field — drives the client-side widget."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ENUM = "enum"
    SECRET = "secret"
    PATH = "path"
    READONLY = "readonly"


class SettingFieldSchema(_ResponseModel):
    """Description + current value of one setting.

    **Secrets are never returned.** For ``SECRET`` fields,
    :attr:`current_value` is ``None`` and :attr:`is_set` tells the client
    whether a value exists. A ``PATCH`` that omits the field leaves the
    secret unchanged; only an explicit value overwrites it.
    """

    key: str = Field(description="Dotted path, e.g. `content.allow_adult`.")
    label: str
    type: SettingFieldType
    description: str = ""

    default: Any = Field(default=None)
    current_value: Any = Field(
        default=None,
        description="Current value. ``None`` for secret fields — check "
                    "``is_set`` instead.",
    )
    is_set: bool | None = Field(
        default=None,
        description="For secret fields only: whether a non-empty value exists.",
    )

    choices: list[str] | None = Field(
        default=None, description="Allowed values for ``enum`` fields."
    )
    min_value: float | int | None = Field(default=None)
    max_value: float | int | None = Field(default=None)

    readonly: bool = False
    requires_restart: bool = False
    advanced: bool = Field(
        default=False,
        description="Whether to hide this field behind an 'advanced' toggle.",
    )


class SettingsSectionSchema(_ResponseModel):
    """A settings section."""

    id: str
    label: str
    description: str = ""
    icon: str = ""
    fields: list[SettingFieldSchema]


class SettingsResponse(_ResponseModel):
    """Response for ``GET /api/v1/settings``.

    ``etag`` supports optimistic concurrency: send it back via
    ``If-Match`` on ``PATCH`` to reject concurrent modifications.
    """

    sections: list[SettingsSectionSchema]
    version: str = Field(description="Schema version — bumped on breaking changes.")
    etag: str | None = Field(default=None)


class SettingsUpdateResponse(_ResponseModel):
    """Response for ``PATCH /api/v1/settings``."""

    success: bool
    applied_keys: list[str] = Field(default_factory=list)
    requires_restart: bool = False
    errors: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of field key → error message for rejected fields.",
    )
    message: str = ""
    new_etag: str | None = Field(default=None)


# ===========================================================================
# Convenience — status-set used by DownloadTaskListResponse.counts
# ===========================================================================

def compute_status_counts(
    items: list[DownloadTaskSchema],
) -> dict[str, int]:
    """Compute the ``counts`` dict for :class:`DownloadTaskListResponse`.

    Kept here (not on the router) so the aggregation logic is testable in
    isolation and centralised for every caller.
    """
    counts: dict[str, int] = {
        "pending": 0,
        "running": 0,
        "paused": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
    }
    for item in items:
        key = str(item.status.value)
        if key in counts:
            counts[key] += 1
    return counts
