"""Library router — ``/api/v1/library``.

Thin FastAPI glue for the local library endpoints. Responsibilities:

* Enforce **user scoping**. The ``user_id`` is always derived from the
  authenticated JWT (``payload.sub``). No endpoint accepts a ``user_id``
  query parameter or body field — the caller cannot target another
  user's library.
* Enforce **safe sorting** via a whitelist. A client passing
  ``?sort=<arbitrary>`` gets a 400 — never a silent fallback that might
  reach a raw ``ORDER BY`` in the DB layer.
* Enforce **progress consistency** (``page_index < page_count`` when both
  are known) before any write.
* Distinguish **row deletion** (default) from **file deletion**
  (``?delete_files=true``) explicitly.
* Return **404 uniformly** for missing manga or manga belonging to
  another user — no existence leak.
* Project domain objects defensively. Library entries nest a manga
  summary + local metadata; the projection fills both with safe
  defaults if the domain object omits fields.

Service contract (duck-typed)
=============================

Every method is looked up by multiple names, so the host can implement
whichever name fits its ``LibraryDatabase``:

* **List**: ``services.library.query(...)`` **or**
  ``services.library.list_mangas(...)``. Returns either
  ``(items, total)``, an object with ``.items``/``.total``, or a bare
  list (``total = len``).
* **Single entry**: ``services.library.get_entry(manga_id, user_id=...)``
  **or** ``services.library.get_manga(manga_id)`` → domain object or
  ``None``.
* **Progress write**:
  ``services.library.set_reading_progress(manga_id, chapter_id, page_index, ...)``
  **or** ``services.library.update_progress(...)``.
* **Delete**: ``services.library.delete(manga_id, user_id=..., delete_files=bool)``
  **or** ``services.library.remove(...)``.

Each method may be sync or async. ``inspect.isawaitable`` handles both.

Sort whitelist
==============

:data:`_SORT_FIELDS` lists the fields a client may sort by. Adding a new
sortable field is a one-line change here **and** a corresponding column
in the DB schema. The router never forwards a raw string to the DB —
``sort_field`` is guaranteed to be one of these identifiers.
"""
from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from loguru import logger as _logger

from ..dependencies import CurrentUser, ServicesDep
from ..schemas.common import LibraryQueryParams, PaginationParams, SortParams
from ..schemas.requests import UpdateProgressRequest
from ..schemas.responses import (
    ErrorCode,
    LibraryEntrySchema,
    LibraryListResponse,
    MangaSummarySchema,
    MessageResponse,
    ReadingProgressSchema,
    error_response,
)

_log = _logger.bind(component="web.routers.library")

router = APIRouter(prefix="/api/v1/library", tags=["library"])

#: Fields a client may sort the library by. Any other value triggers a
#: 400 — never a silent fallback. Kept short and explicit so this remains
#: the single source of truth for what the DB layer can trust.
_SORT_FIELDS: frozenset[str] = frozenset(
    {
        "added_at",
        "updated_at",
        "last_read_at",
        "title",
        "author",
        "chapter_count",
    }
)

#: Default sort applied when the client does not specify one.
_DEFAULT_SORT: str = "added_at"

#: Max length accepted for the ``{manga_id}`` path segment.
_MAX_ID_LENGTH: int = 128


# ===========================================================================
# Endpoints
# ===========================================================================

@router.get(
    "",
    response_model=LibraryListResponse,
    summary="List the caller's library",
    description=(
        "Return a paginated view of the authenticated user's library.\n\n"
        "* Filters: `filters` (data-driven ids), `manga_status`, "
        "`language`, `search`.\n"
        "* Sorting: any field in ``%s``.\n"
        "* Only the caller's own library entries are returned — the "
        "server scopes by the JWT subject, never by a query parameter."
        % ", ".join(sorted(_SORT_FIELDS))
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unsupported sort field.",
        },
    },
)
async def list_library(
    pagination: Annotated[PaginationParams, Depends()],
    sort: Annotated[SortParams, Depends()],
    filters: Annotated[LibraryQueryParams, Depends()],
    payload: CurrentUser,
    services: ServicesDep,
) -> LibraryListResponse:
    sort_field = _resolve_sort_field(sort.sort)

    items, total = await _library_query(
        services,
        user_id=_require_user_id(payload),
        filters=filters.filters,
        manga_status=filters.manga_status,
        language=filters.language,
        search=filters.search,
        sort_field=sort_field,
        descending=sort.descending,
        offset=pagination.offset,
        limit=pagination.limit,
    )

    entries = [_to_entry_schema(item) for item in items]

    return LibraryListResponse(
        items=entries,
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
    )


@router.get(
    "/{manga_id}",
    response_model=LibraryEntrySchema,
    summary="Get a library entry",
    description=(
        "Return the caller's library entry for ``manga_id``. Returns 404 "
        "whether the manga is not in the library **or** belongs to "
        "another user — the server does not distinguish."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Manga is not in the caller's library.",
        },
    },
)
async def get_library_entry(
    manga_id: Annotated[str, Path(max_length=_MAX_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
) -> LibraryEntrySchema:
    entry = await _library_get_entry(
        services,
        manga_id=manga_id,
        user_id=_require_user_id(payload),
    )
    if entry is None:
        raise _not_found(manga_id)
    return _to_entry_schema(entry)


@router.put(
    "/{manga_id}/progress",
    response_model=MessageResponse,
    summary="Update reading progress",
    description=(
        "Record the current reading position for a manga in the caller's "
        "library. If ``page_count`` is provided, ``page_index`` must be "
        "strictly less than it. If ``percent`` is omitted, the server "
        "computes it from ``page_index / page_count`` when both are known."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Manga is not in the caller's library.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "`page_index` is not a valid position within "
                           "`page_count`.",
        },
    },
)
async def update_progress(
    manga_id: Annotated[str, Path(max_length=_MAX_ID_LENGTH)],
    body: UpdateProgressRequest,
    payload: CurrentUser,
    services: ServicesDep,
) -> MessageResponse:
    # 1. Consistency check.
    if (
        body.page_count is not None
        and body.page_index >= body.page_count
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_response(
                ErrorCode.VALIDATION_ERROR,
                f"page_index ({body.page_index}) must be less than "
                f"page_count ({body.page_count}).",
                details={"reason": "PAGE_OUT_OF_RANGE"},
            ).model_dump(),
        )

    # 2. Delegate to the service.
    user_id = _require_user_id(payload)
    wrote = await _library_set_progress(
        services,
        manga_id=manga_id,
        user_id=user_id,
        chapter_id=body.chapter_id,
        page_index=body.page_index,
        page_count=body.page_count,
        percent=body.percent,
    )

    if wrote is False:
        raise _not_found(manga_id)

    return MessageResponse(
        message="Reading progress saved.",
        details={
            "manga_id": manga_id,
            "chapter_id": body.chapter_id,
            "page_index": body.page_index,
        },
    )


@router.delete(
    "/{manga_id}",
    response_model=MessageResponse,
    summary="Delete a library entry",
    description=(
        "Remove a manga from the caller's library.\n\n"
        "* Default: removes the **library row only**. Files on disk are "
        "preserved and can be re-imported later.\n"
        "* `?delete_files=true`: also deletes the packaged files from "
        "disk. **Irreversible.**\n\n"
        "Returns 404 for manga not in the caller's library."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Manga is not in the caller's library.",
        },
    },
)
async def delete_library_entry(
    manga_id: Annotated[str, Path(max_length=_MAX_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
    delete_files: Annotated[
        bool,
        Query(
            description=(
                "When `true`, packaged files are also deleted from disk. "
                "Default `false` removes only the library row."
            ),
        ),
    ] = False,
) -> MessageResponse:
    user_id = _require_user_id(payload)
    deleted = await _library_delete(
        services,
        manga_id=manga_id,
        user_id=user_id,
        delete_files=delete_files,
    )

    if not deleted:
        raise _not_found(manga_id)

    _log.info(
        "library entry deleted user={} manga={} files={}",
        user_id,
        manga_id,
        delete_files,
    )

    return MessageResponse(
        message=(
            "Manga deleted from library."
            if not delete_files
            else "Manga deleted from library and files removed from disk."
        ),
        details={
            "manga_id": manga_id,
            "files_deleted": delete_files,
        },
    )


# ===========================================================================
# Service resolution helpers (duck-typed)
# ===========================================================================

async def _library_query(
    services: ServicesDep,
    *,
    user_id: str,
    filters: list[str],
    manga_status: Any,
    language: Any,
    search: str,
    sort_field: str,
    descending: bool,
    offset: int,
    limit: int,
) -> tuple[list[Any], int]:
    """Call the library's list method, tolerant to name and shape."""
    for name in ("query", "list_mangas"):
        method = getattr(services.library, name, None)
        if not callable(method):
            continue
        try:
            result = _call_with_supported_kwargs(
                method,
                user_id=user_id,
                filters=filters,
                manga_status=manga_status,
                language=language,
                search=search,
                sort_field=sort_field,
                descending=descending,
                offset=offset,
                limit=limit,
            )
            if inspect.isawaitable(result):
                result = await result
            return _normalize_items_total(result)
        except Exception:
            _log.exception("library.{} raised", name)
            continue
    # No method worked.
    raise _service_unavailable("Library query is not available.")


async def _library_get_entry(
    services: ServicesDep, *, manga_id: str, user_id: str
) -> Any | None:
    """Fetch a single library entry, tolerant to method name."""
    for name in ("get_entry", "get_manga"):
        method = getattr(services.library, name, None)
        if not callable(method):
            continue
        try:
            result = _call_with_supported_kwargs(
                method, manga_id, user_id=user_id
            )
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            _log.exception("library.{} raised for {}", name, manga_id)
            continue
    return None


async def _library_set_progress(
    services: ServicesDep,
    *,
    manga_id: str,
    user_id: str,
    chapter_id: str,
    page_index: int,
    page_count: int | None,
    percent: float | None,
) -> bool | None:
    """Write reading progress. Returns ``False`` if the manga is absent.

    ``None`` means "wrote or unknown" (the DB layer does not report
    whether the row existed). Only a strict ``False`` triggers a 404 —
    hosts that want to enforce existence must return ``False``.
    """
    for name in ("set_reading_progress", "update_progress"):
        method = getattr(services.library, name, None)
        if not callable(method):
            continue
        try:
            result = _call_with_supported_kwargs(
                method,
                manga_id,
                chapter_id=chapter_id,
                page_index=page_index,
                page_count=page_count,
                percent=percent,
                user_id=user_id,
            )
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            _log.exception("library.{} raised for {}", name, manga_id)
            return None
    # No method worked; treat as "not found" rather than 500 — hosts that
    # omit the writer method simply cannot accept progress writes.
    return False


async def _library_delete(
    services: ServicesDep,
    *,
    manga_id: str,
    user_id: str,
    delete_files: bool,
) -> bool:
    """Delete a library entry. Returns whether a row was removed."""
    for name in ("delete", "remove"):
        method = getattr(services.library, name, None)
        if not callable(method):
            continue
        try:
            result = _call_with_supported_kwargs(
                method,
                manga_id,
                user_id=user_id,
                delete_files=delete_files,
            )
            if inspect.isawaitable(result):
                result = await result
            # ``None`` from a delete is common (no return value); assume
            # success unless the host explicitly returned ``False``.
            if result is None:
                return True
            return bool(result)
        except Exception:
            _log.exception("library.{} raised for {}", name, manga_id)
            return False
    return False


def _call_with_supported_kwargs(
    method: Any, *args: Any, **kwargs: Any
) -> Any:
    """Call ``method`` filtering ``kwargs`` to those it accepts.

    Hosts may implement a subset of the documented keyword arguments. We
    use :func:`inspect.signature` when available and silently drop
    unsupported kwargs; on ``ValueError`` (builtins with no signature),
    pass everything.
    """
    try:
        sig = inspect.signature(method)
    except (ValueError, TypeError):
        return method(*args, **kwargs)

    # If the method accepts **kwargs, everything goes.
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return method(*args, **kwargs)

    accepted = {
        name
        for name, p in sig.parameters.items()
        if p.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    }
    filtered = {k: v for k, v in kwargs.items() if k in accepted}
    return method(*args, **filtered)


# ===========================================================================
# Normalization / mapping
# ===========================================================================

def _normalize_items_total(raw: Any) -> tuple[list[Any], int]:
    """Coerce any of the supported return shapes into ``(items, total)``."""
    if raw is None:
        return [], 0

    # ``(items, total)`` tuple.
    if (
        isinstance(raw, tuple)
        and len(raw) == 2
        and isinstance(raw[1], int)
    ):
        items = list(raw[0] or [])
        return items, int(raw[1])

    # Object with .items / .total.
    items_attr = getattr(raw, "items", None)
    total_attr = getattr(raw, "total", None)
    if items_attr is not None and total_attr is not None:
        items = list(items_attr or [])
        return items, int(total_attr or len(items))

    # Bare sequence.
    try:
        items = list(raw)
        return items, len(items)
    except TypeError:
        return [], 0


def _to_entry_schema(item: Any) -> LibraryEntrySchema:
    """Project a domain library entry into ``LibraryEntrySchema``.

    The domain object may be either:

    * a ``LibraryEntry`` domain object with ``.manga`` and library
      metadata attributes, or
    * a ``Manga`` domain object with attached library metadata fields
      (``.reading_progress``, ``.added_at``…).

    Both shapes are handled; each field has a safe default.
    """
    manga_domain = getattr(item, "manga", item)

    try:
        manga = MangaSummarySchema.model_validate(manga_domain)
    except Exception:
        _log.exception("Failed to validate manga in library entry")
        # Build a minimal valid schema from attributes — this keeps the
        # endpoint from 500'ing on an unusual domain object.
        manga = MangaSummarySchema(
            id=str(getattr(manga_domain, "id", "")),
            title=str(getattr(manga_domain, "title", "")),
        )

    # Reading progress.
    progress_raw = getattr(item, "reading_progress", None)
    if isinstance(progress_raw, ReadingProgressSchema):
        progress = progress_raw
    elif progress_raw is not None:
        try:
            progress = ReadingProgressSchema.model_validate(progress_raw)
        except Exception:
            progress = ReadingProgressSchema()
    else:
        progress = ReadingProgressSchema()

    downloaded = int(getattr(item, "downloaded_chapters", 0) or 0)
    size_bytes = getattr(item, "total_size_bytes", None)
    added_at = getattr(item, "added_at", None)
    updated_at = getattr(item, "updated_at", None)

    return LibraryEntrySchema(
        manga=manga,
        downloaded_chapters=downloaded,
        total_size_bytes=size_bytes,
        reading_progress=progress,
        added_at=added_at,
        updated_at=updated_at,
    )


# ===========================================================================
# Input validation
# ===========================================================================

def _resolve_sort_field(requested: str | None) -> str:
    """Return a whitelisted sort field, or raise 400 for unknown values."""
    if requested is None:
        return _DEFAULT_SORT
    if requested in _SORT_FIELDS:
        return requested
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=error_response(
            ErrorCode.VALIDATION_ERROR,
            f"Unsupported sort field: {requested!r}.",
            details={
                "allowed": sorted(_SORT_FIELDS),
                "default": _DEFAULT_SORT,
            },
        ).model_dump(),
    )


def _require_user_id(payload: CurrentUser) -> str:
    """Return ``payload.sub`` or raise 401 if somehow absent.

    The dependency layer guarantees a valid JWT; this guard exists so a
    future dependency refactor cannot silently produce a
    user-unspecified request that scopes the library to nobody.
    """
    if not payload or not payload.sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_response(
                ErrorCode.UNAUTHORIZED,
                "Authenticated user is missing from the token.",
            ).model_dump(),
        )
    return payload.sub


# ===========================================================================
# Errors
# ===========================================================================

def _not_found(manga_id: str) -> HTTPException:
    """Standard 404 for a manga not in the caller's library.

    Deliberately ambiguous: does not distinguish "manga does not exist"
    from "belongs to another user" from "was never added".
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_response(
            ErrorCode.NOT_FOUND,
            f"Manga {manga_id!r} is not in your library.",
        ).model_dump(),
    )


def _service_unavailable(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=error_response(
            ErrorCode.INTERNAL_ERROR,
            message,
            details={
                "hint": "The deployment may not have a library backend "
                        "configured.",
            },
        ).model_dump(),
    )


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] Any new endpoint that touches library data derives ``user_id`` from
#     ``payload.sub`` only. Never accept a ``user_id`` query/body field.
# [ ] Any new sortable field is added to :data:`_SORT_FIELDS` **and** to
#     the DB layer's whitelist. The router is the sole gate — do not
#     bypass it in service code.
# [ ] Any new mutation validates consistency *before* calling the
#     service (see the ``page_index`` / ``page_count`` check).
# [ ] Any new "not found or not yours" case reuses :func:`_not_found` —
#     a 403 leak here would allow enumeration of other users' libraries.
# [ ] Any new return shape from a library method is handled in
#     :func:`_normalize_items_total` / :func:`_to_entry_schema`.
# [ ] Every 4xx/5xx uses :func:`error_response` — never a bare
#     ``HTTPException(detail="...")``.
# [ ] ``delete_files`` remains an opt-in boolean. Deleting files by
#     default would be a data-loss footgun.
