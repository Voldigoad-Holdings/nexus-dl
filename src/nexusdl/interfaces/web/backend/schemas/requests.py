"""HTTP request body schemas for the NexusDL backend.

Pydantic v2 models describing every request body accepted by the HTTP API.
Symmetric to :mod:`schemas.responses` — this module validates *inbound*
data; :mod:`schemas.responses` serializes *outbound* data.

Design notes
============

* **Strict on shape, permissive on business rules.**
  Envelope fields use ``extra="forbid"`` — a typo in a field name is a
  400, not a silent drop. Business validation (password policy, site
  authorization, chapter existence, settings key validity) lives in the
  service layer, not here.

* **Immutable once validated.** :attr:`model_config.frozen` is ``True``
  on every request. Instances are safe to pass across layers without
  defensive copying.

* **No legacy ``@validator``.** Only Pydantic v2 ``@field_validator`` /
  ``@model_validator`` are used. Every ``ValueError`` surfaces through
  :class:`~..schemas.responses.ErrorResponse` under
  ``details.fields``.

* **Password policy lives elsewhere.** :class:`RegisterRequest` only
  enforces technical guards (max byte length for bcrypt). The actual
  policy (min length, required character classes) is enforced by
  :class:`~..security.PasswordPolicy` inside
  :meth:`~..security.AuthService.hash_password`. This keeps a single
  source of truth for password rules.

* **Settings keys are open.** :attr:`SettingsPatchRequest.changes` is a
  ``dict[str, Any]`` — the *shape* of a settings patch is data-driven,
  so the request schema cannot know which keys exist. Unknown keys are
  rejected server-side and reported via ``errors`` in the response.

* **Download is a discriminated union.** :class:`DownloadRequest`
  accepts **either** a resolved ``manga_id`` **or** a source ``url`` —
  but never both. Chapter selection is optional; omitting it means
  "all chapters of the manga".

* **Pagination caps align with responses.** :data:`MAX_PAGE_SIZE` is
  imported from :mod:`schemas.responses` so requests and responses can
  never disagree on limits.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from nexusdl.core.models.enums import (
    Language,
    PackagingFormat,
    Priority,
)

# Single source of truth for pagination limits — imported from responses
# so requests and responses can never disagree.
from .responses import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

__all__ = [
    # Constants
    "USERNAME_MIN_LENGTH",
    "USERNAME_MAX_LENGTH",
    "PASSWORD_MAX_BYTES",
    "SEARCH_QUERY_MAX_LENGTH",
    "MAX_CHAPTER_IDS_PER_REQUEST",
    # Base
    "RequestBody",
    # Auth
    "LoginRequest",
    "RegisterRequest",
    "RefreshTokenRequest",
    "LogoutRequest",
    # Search
    "SearchRequest",
    # Download
    "DownloadRequest",
    # Library
    "UpdateProgressRequest",
    # Settings
    "SettingsPatchRequest",
]


# ===========================================================================
# Constants
# ===========================================================================

#: Minimum length for a new username.
USERNAME_MIN_LENGTH: int = 3

#: Maximum length for a username (also the max for login identifier).
USERNAME_MAX_LENGTH: int = 32

#: bcrypt's hard limit. Longer passwords would be silently truncated by
#: bcrypt — we reject them instead.
PASSWORD_MAX_BYTES: int = 72

#: Maximum length for a search query string.
SEARCH_QUERY_MAX_LENGTH: int = 256

#: Hard cap on the number of chapter ids in a single download request.
#: Beyond this, clients should split the work.
MAX_CHAPTER_IDS_PER_REQUEST: int = 500


#: Username pattern: alphanumeric, underscore, dash, dot. No spaces.
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")

#: Very basic email shape (defence-in-depth only; real validation is
#: server-side, and hosts may layer a stricter validator).
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ===========================================================================
# Base
# ===========================================================================

class RequestBody(BaseModel):
    """Base for every request body.

    * ``extra="forbid"`` — strict protocol.
    * ``frozen=True`` — immutable after validation.
    * ``from_attributes=True`` — allows tests and internal code to build
      a request from a dataclass-like object.

    Subclasses must **not** override the config; they add fields and
    validators only.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )


# ===========================================================================
# Auth
# ===========================================================================

class LoginRequest(RequestBody):
    """Body for ``POST /api/v1/auth/login``.

    The :attr:`username` field accepts **either** a username **or** an
    email address — the service resolves which one it is. This keeps the
    client simple and the API future-proof (adding email-based login does
    not require a new endpoint).
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "username": "jane_doe",
                "password": "correct horse battery staple",
            }
        },
    )

    username: str = Field(
        min_length=1,
        max_length=USERNAME_MAX_LENGTH,
        description="Username or email address.",
    )
    password: str = Field(
        min_length=1,
        max_length=PASSWORD_MAX_BYTES,
        description=f"Plain password. Max {PASSWORD_MAX_BYTES} bytes "
                    "(bcrypt limit).",
    )


class RegisterRequest(RequestBody):
    """Body for ``POST /api/v1/auth/register``.

    Enforces only **technical** guards (username shape, password byte
    length, confirmation equality). The actual password **policy**
    (min length, character classes, banned substrings) is enforced by
    :class:`~..security.PasswordPolicy` inside
    :meth:`~..security.AuthService.hash_password` so the policy stays in
    one place.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "username": "jane_doe",
                "email": "jane@example.com",
                "password": "correct horse battery staple",
                "password_confirm": "correct horse battery staple",
            }
        },
    )

    username: str = Field(
        min_length=USERNAME_MIN_LENGTH,
        max_length=USERNAME_MAX_LENGTH,
        description="3-32 characters: letters, digits, underscore, dash, dot.",
    )
    email: str | None = Field(
        default=None,
        max_length=320,
        description="Optional email. Enables email-based login and "
                    "password recovery.",
    )
    password: str = Field(
        min_length=1,
        max_length=PASSWORD_MAX_BYTES,
        description=f"Plain password. Max {PASSWORD_MAX_BYTES} bytes.",
    )
    password_confirm: str = Field(
        min_length=1,
        max_length=PASSWORD_MAX_BYTES,
        description="Must equal ``password``.",
    )

    @field_validator("username")
    @classmethod
    def _validate_username(cls, v: str) -> str:
        if not _USERNAME_PATTERN.match(v):
            raise ValueError(
                "must contain only letters, digits, '_', '-', or '.'"
            )
        return v

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _EMAIL_PATTERN.match(v):
            raise ValueError("not a valid email address")
        return v.lower()

    @model_validator(mode="after")
    def _passwords_match(self) -> RegisterRequest:
        if self.password != self.password_confirm:
            raise ValueError("password_confirm must equal password")
        return self


class RefreshTokenRequest(RequestBody):
    """Body for ``POST /api/v1/auth/refresh``."""

    refresh_token: str = Field(
        min_length=1,
        description="The refresh token to rotate into a new token pair.",
    )


class LogoutRequest(RequestBody):
    """Body for ``POST /api/v1/auth/logout``.

    Both fields are optional:

    * ``refresh_token`` — revokes the specific refresh-token family (all
      sessions derived from the same original login).
    * ``all_sessions`` — when ``True``, revokes every active token family
      for the authenticated user (logout-everywhere).
    """

    refresh_token: str | None = Field(
        default=None,
        description="If provided, the corresponding refresh-token family "
                    "is revoked server-side.",
    )
    all_sessions: bool = Field(
        default=False,
        description="If ``True``, revoke every session for the calling user.",
    )


# ===========================================================================
# Search
# ===========================================================================

class SearchRequest(RequestBody):
    """Body for ``POST /api/v1/search``.

    Empty :attr:`query` combined with a non-empty :attr:`site_ids` means
    "browse this site". Both empty is rejected at the router (the service
    can still choose to accept it if it makes sense for its backend).

    :attr:`include_adult` defaults to ``None`` — meaning "use the server
    default" (derived from ``Settings.content.allow_adult``). Clients that
    want to override explicitly pass ``true`` / ``false`` (subject to
    authorization).
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "query": "one piece",
                "site_ids": ["mangadex", "mangafire"],
                "language": "en",
                "page": 1,
                "per_page": 24,
            }
        },
    )

    query: str = Field(
        default="",
        max_length=SEARCH_QUERY_MAX_LENGTH,
        description="Free-text query. Empty means 'browse'.",
    )
    site_ids: list[str] | None = Field(
        default=None,
        description="Restrict to these sites (must be enabled and "
                    "authorized). ``null`` means 'all authorized sites'.",
    )
    language: Language | None = Field(
        default=None,
        description="Filter by source language. ``null`` = no filter.",
    )
    include_adult: bool | None = Field(
        default=None,
        description="Override the server's adult-content setting. "
                    "``null`` = use the server default.",
    )
    page: int = Field(
        default=1, ge=1,
        description="1-based page index.",
    )
    per_page: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1, le=MAX_PAGE_SIZE,
        description=f"Page size (1-{MAX_PAGE_SIZE}).",
    )

    @field_validator("site_ids")
    @classmethod
    def _dedupe_site_ids(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        seen: set[str] = set()
        out: list[str] = []
        for sid in v:
            sid = sid.strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
        return out or None

    @field_validator("query")
    @classmethod
    def _trim_query(cls, v: str) -> str:
        return v.strip()


# ===========================================================================
# Download
# ===========================================================================

class DownloadRequest(RequestBody):
    """Body for ``POST /api/v1/download``.

    Two entry points (mutually exclusive):

    * **Resolved** — pass ``manga_id`` (and optionally ``chapter_ids``).
      The client has already parsed the site and knows what it wants.
    * **URL-based** — pass ``url``. The server parses it against the
      registry to resolve the manga / chapters.

    Providing both ``manga_id`` and ``url`` is rejected (ambiguous).
    Providing neither is rejected (nothing to download).

    ``chapter_ids`` semantics:

    * omitted (``None``) — download every chapter of the manga,
    * empty list — **invalid** (would download nothing),
    * non-empty list — download exactly those chapters.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "manga_id": "abc123def456",
                "chapter_ids": ["ch1", "ch2"],
                "packaging_format": "CBZ",
                "priority": "NORMAL",
            }
        },
    )

    manga_id: str | None = Field(
        default=None,
        description="Resolved manga id. Mutually exclusive with ``url``.",
    )
    url: str | None = Field(
        default=None,
        description="Source URL. The server resolves it against the "
                    "registry. Mutually exclusive with ``manga_id``.",
    )
    chapter_ids: list[str] | None = Field(
        default=None,
        description="Chapters to download. ``null`` = all chapters. "
                    "Must be non-empty if provided.",
    )
    packaging_format: PackagingFormat | None = Field(
        default=None,
        description="Override the server's default packaging format.",
    )
    priority: Priority = Field(
        default=Priority.NORMAL,
        description="Scheduling priority.",
    )

    @field_validator("chapter_ids")
    @classmethod
    def _dedupe_chapter_ids(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not v:
            raise ValueError(
                "if provided, chapter_ids must be a non-empty list "
                "(omit the field to download all chapters)"
            )
        if len(v) > MAX_CHAPTER_IDS_PER_REQUEST:
            raise ValueError(
                f"chapter_ids cannot exceed {MAX_CHAPTER_IDS_PER_REQUEST} "
                "entries per request — split the work into multiple calls"
            )
        seen: set[str] = set()
        out: list[str] = []
        for cid in v:
            cid = cid.strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
        if not out:
            raise ValueError("chapter_ids must contain at least one id")
        return out

    @model_validator(mode="after")
    def _manga_or_url(self) -> DownloadRequest:
        has_manga = bool(self.manga_id)
        has_url = bool(self.url)
        if has_manga and has_url:
            raise ValueError(
                "provide either 'manga_id' or 'url', not both"
            )
        if not has_manga and not has_url:
            raise ValueError(
                "provide 'manga_id' or 'url' to identify what to download"
            )
        return self


# ===========================================================================
# Library
# ===========================================================================

class UpdateProgressRequest(RequestBody):
    """Body for ``PUT /api/v1/library/{manga_id}/progress``.

    The manga id is in the path; this body only carries the reading
    position. ``percent`` is optional — if omitted, the server computes it
    from ``page_index / page_count`` when ``page_count`` is known.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "chapter_id": "abc123def456",
                "page_index": 12,
                "page_count": 40,
            }
        },
    )

    chapter_id: str = Field(
        min_length=1,
        description="Id of the chapter currently being read.",
    )
    page_index: int = Field(
        ge=0,
        description="0-based page index within the chapter.",
    )
    page_count: int | None = Field(
        default=None,
        ge=1,
        description="Total pages in the chapter. Optional — improves "
                    "percent accuracy on the server.",
    )
    percent: float | None = Field(
        default=None,
        ge=0.0, le=1.0,
        description="Progress from 0.0 to 1.0. If omitted, the server "
                    "computes it from ``page_index / page_count``.",
    )


# ===========================================================================
# Settings
# ===========================================================================

class SettingsPatchRequest(RequestBody):
    """Body for ``PATCH /api/v1/settings``.

    :attr:`changes` maps *dotted setting keys* to their new values. The
    set of valid keys is **data-driven** (derived from
    :class:`~..views.settings_view.SettingsSchema` in the GUI, and from
    :class:`Settings` in the backend), so this schema cannot enumerate
    them — it only guarantees the shape is ``dict[str, Any]``.

    Unknown keys, type mismatches, and policy violations are detected by
    the service and reported via ``errors`` in
    :class:`~.responses.SettingsUpdateResponse`.

    :attr:`etag` supports optimistic concurrency: when provided, the
    patch is applied only if the current settings etag matches. On a
    mismatch, the server returns 409 Conflict with the new etag in the
    response.

    :attr:`reset_to_default` — when ``True``, every key in ``changes`` is
    interpreted as "reset to default" regardless of its value. This
    avoids having to encode ``null``-means-reset semantics.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "changes": {
                    "content.allow_adult": True,
                    "http.timeout_seconds": 45.0,
                },
                "etag": "7a2b4f6e1c8d4e2a9b3f",
            }
        },
    )

    changes: dict[str, Any] = Field(
        min_length=1,
        description="Mapping of dotted setting keys to new values. "
                    "Unknown keys are rejected server-side.",
    )
    etag: str | None = Field(
        default=None,
        description="Optimistic concurrency token from a previous GET. "
                    "If provided and stale, the patch is rejected with 409.",
    )
    reset_to_default: bool = Field(
        default=False,
        description="If ``True``, every key in ``changes`` is reset to its "
                    "default (its value is ignored).",
    )

    @field_validator("changes")
    @classmethod
    def _validate_changes(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("changes must contain at least one key")
        for key in v:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("change keys must be non-empty strings")
        return v
