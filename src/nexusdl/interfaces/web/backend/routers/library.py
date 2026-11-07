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

Method resolution policy
------------------------

**Method name fallbacks are tried only when the previous name is
absent** — never when it exists but raises. A method that exists and
raises is a *bug* in the host, not a signal to silently try another
implementation: masking it would turn a broken database into
"library not available" without any actionable diagnostic. Exceptions
from existing methods propagate to the caller as 500
``INTERNAL_ERROR``.

Not-found detection
-------------------

For a **write** or a **delete**, the router treats any of:

* ``False`` return value,
* exception from the service,

as a signal that the row does not exist **or** that the DB is broken.
The two cases are **not** distinguished at the HTTP layer (both are
404 for the write case, 500 for the delete case) but the log contains
the real reason. This is deliberate: a client should not learn whether
a manga exists in another user's library — and should not learn
whether the DB is currently healthy either.

For a **read** (get entry, list), any exception **always** propagates
as 500 — the client asked for data, the server cannot produce it, the
error must be visible.

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

    # ``False`` → the row does not exist for this user.
    # ``None`` → the host wrote successfully but returned no value (the
    #   common ``UPDATE``-style contract).
    # Any other truthy value → host confirmed the write.
    # An exception from the host → 500 already raised in the helper.
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
    """Call the library's list method.

    Tries ``query`` then ``list_mangas``; **falls through to the next
    candidate only if the previous one is absent**. An existing method
    that raises is a host bug and propagates as 500 — silently trying
    the next name would mask a broken database.
    """
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
            _log.exception(
                "library.{} raised while querying user={}",
                name,
                user_id,
            )
            raise _service_error(
                "Failed to query the library.", cause_name=name
            )
    # No method found.
    raise _service_unavailable("Library query is not available.")


async def _library_get_entry(
    services: ServicesDep, *, manga_id: str, user_id: str
) -> Any | None:
    """Fetch a single library entry.

    Tries ``get_entry`` then ``get_manga``; falls through only if the
    first is absent. Returns ``None`` for a true miss (both methods
    missing or the primary returning ``None``). A method that raises
    propagates as 500.
    """
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
            _log.exception(
                "library.{} raised for manga={} user={}",
                name,
                manga_id,
                user_id,
            )
            raise _service_error(
                "Failed to read library entry.", cause_name=name
            )
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
    """Write reading progress.

    Returns:
        * ``False`` — the row is absent for this user (404).
        * ``True``  — the host confirmed the write.
        * ``None``  — the host returned ``None`` (the common pattern for
          a SQL ``UPDATE`` that does not care about the row count).

    A raised exception **always** propagates as 500 — never as ``None``,
    which would look like a successful write. Prior to this fix, an
    exception was silently mapped to ``None`` and the endpoint responded
    200 "saved" even when the DB had rejected the write.
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
            _log.exception(
                "library.{} raised for manga={} user={}",
                name,
                manga_id,
                user_id,
            )
            raise _service_error(
                "Failed to save reading progress.", cause_name=name
            )
    # No writer method at all — cannot write; treat as miss.
    return False


async def _library_delete(
    services: ServicesDep,
    *,
    manga_id: str,
    user_id: str,
    delete_files: bool,
) -> bool:
    """Delete a library entry.

    Returns ``True`` when a row was removed, ``False`` when the row was
    absent. A raised exception from the host propagates as 500 — never
    as ``False`` (which the caller would surface as a misleading 404).
    """
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
            # ``None`` from a delete is common (SQL ``DELETE`` does not
            # return a boolean); assume success unless the host
            # explicitly returned ``False``.
            if result is None:
                return True
            return bool(result)
        except Exception:
            _log.exception(
                "library.{} raised for manga={} user={} files={}",
                name,
                manga_id,
                user_id,
                delete_files,
            )
            raise _service_error(
                "Failed to delete library entry.", cause_name=name
            )
    return False


def _call_with_supported_kwargs(
    method: Any, *args: Any, **kwargs: Any
) -> Any:
    """Call ``method`` filtering ``kwargs`` to those it accepts.

    - **``**kwargs`` in the signature** → everything goes through.
    - **Positional-only parameters** (rare, but legal since 3.8) are
      never passed as keywords — silently dropped from the kwargs dict
      to avoid a ``TypeError`` at call time.
    - **No signature available** (builtins, ``functools.partial``
      without ``__wrapped__``) → everything is passed through
      optimistically.

    Supports both ``**kwargs``-style and keyword-only parameters. The
    filter is conservative: unknown kwargs are silently dropped rather
    than raising. Callers relying on this contract (all five methods
    above) document which kwargs they expect the host to accept.
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
    """Coerce any of the supported return shapes into ``(items, total)``.

    Handles:
        * ``None`` → ``([], 0)``.
        * ``(items, total)`` tuple with integer total.
        * Object with ``.items`` / ``.total`` (either attribute or
          method — a method is called).
        * Bare iterable (list, tuple, generator) → total = len.
    """
    if raw is None:
        return [], 0

    # ``(items, total)`` tuple.
    if (
        isinstance(raw, tuple)
        and len(raw) == 2
        and isinstance(raw[1], int)
    ):
        try:
            items = list(raw[0] or [])
        except TypeError:
            items = []
        return items, int(raw[1])

    # Object with .items / .total (attribute OR callable).
    items_attr = getattr(raw, "items", None)
    total_attr = getattr(raw, "total", None)
    if items_attr is not None or total_attr is not None:
        try:
            items_value = items_attr() if callable(items_attr) else items_attr
        except Exception:
            items_value = None
        try:
            total_value = total_attr() if callable(total_attr) else total_attr
        except Exception:
            total_value = None

        if items_value is not None:
            try:
                items = list(items_value or [])
            except TypeError:
                items = []
            try:
                total = int(total_value) if total_value is not None else len(items)
            except (TypeError, ValueError):
                total = len(items)
            return items, total

    # Bare sequence / iterable.
    try:
        items = list(raw)
    except TypeError:
        return [], 0
    return items, len(items)


def _to_entry_schema(item: Any) -> LibraryEntrySchema:
    """Project a domain library entry into ``LibraryEntrySchema``.

    The domain object may be either:

    * a ``LibraryEntry`` domain object with ``.manga`` and library
      metadata attributes, or
    * a ``Manga`` domain object with attached library metadata fields
      (``.reading_progress``, ``.added_at``…).

    Both shapes are handled; each field has a safe default.

    The manga summary projection is **not** wrapped in a try/except fallback
    that builds ``MangaSummarySchema(id=..., title=...)`` — that pattern
    is a trap: ``MangaSummarySchema`` has required fields beyond ``id``
    and ``title``, so the "fallback" itself raised ``ValidationError``
    and produced a 500. Instead, the exception is allowed to propagate
    with a clear log, so a broken domain object surfaces as a 500 with
    a diagnostic rather than a misleading 200 with a fake manga.
    """
    manga_domain = getattr(item, "manga", item)

    try:
        manga = MangaSummarySchema.model_validate(manga_domain)
    except Exception:
        _log.exception(
            "Failed to project manga domain into MangaSummarySchema; "
            "item_type={}",
            type(manga_domain).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to serialize library entry.",
            ).model_dump(),
        )

    # Reading progress.
    progress_raw = getattr(item, "reading_progress", None)
    if isinstance(progress_raw, ReadingProgressSchema):
        progress = progress_raw
    elif progress_raw is not None:
        try:
            progress = ReadingProgressSchema.model_validate(progress_raw)
        except Exception:
            _log.exception(
                "Failed to project reading_progress; using empty default"
            )
            progress = ReadingProgressSchema()
    else:
        progress = ReadingProgressSchema()

    downloaded = _coerce_int(getattr(item, "downloaded_chapters", 0), default=0)
    size_bytes = _coerce_optional_int(getattr(item, "total_size_bytes", None))
    added_at = getattr(item, "added_at", None)
    updated_at = getattr(item, "updated_at", None)

    try:
        return LibraryEntrySchema(
            manga=manga,
            downloaded_chapters=downloaded,
            total_size_bytes=size_bytes,
            reading_progress=progress,
            added_at=added_at,
            updated_at=updated_at,
        )
    except Exception:
        _log.exception(
            "Failed to build LibraryEntrySchema; item_type={}",
            type(item).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to serialize library entry.",
            ).model_dump(),
        )


def _coerce_int(value: Any, *, default: int) -> int:
    """Best-effort int coercion; returns ``default`` on failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_int(value: Any) -> int | None:
    """Best-effort int coercion; returns ``None`` on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Input validation
# ===========================================================================

def _resolve_sort_field(requested: str | None) -> str:
    """Return a whitelisted sort field, or raise 400 for unknown values.

    Comparison is **case-insensitive and whitespace-trimmed**: ``"Title"``
    and ``" title "`` are normalized to ``"title"`` before the whitelist
    check, matching the API contract that sort fields are identifiers
    (not free-text). A value that survives normalization but is still
    not in :data:`_SORT_FIELDS` produces the 400 — never a silent
    fallback to the default.
    """
    if requested is None:
        return _DEFAULT_SORT
    normalized = str(requested).strip().lower()
    if normalized in _SORT_FIELDS:
        return normalized
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
    # ``TokenPayload`` is a Pydantic model: it is never falsy. Only the
    # ``.sub`` presence check is meaningful — the ``not payload`` branch
    # was a no-op that misled readers into thinking ``payload`` could be
    # ``None`` here.
    if not getattr(payload, "sub", None):
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


def _service_error(message: str, *, cause_name: str) -> HTTPException:
    """500 for a service method that exists and raised.

    Distinct from :func:`_service_unavailable` (which is raised when
    **no** method exists). The distinction matters for debugging: the
    former points at a host bug (broken DB query, unreachable
    connection), the latter at a deployment choice. Both are 500 —
    a broken DB must never look like a 404 to the client.
    """
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=error_response(
            ErrorCode.INTERNAL_ERROR,
            message,
            details={"cause": f"library.{cause_name}"},
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
# [ ] Method-name fallbacks only trigger on **absence**, not on
#     exceptions. An existing method that raises is a bug — it must
#     surface as 500, never be masked as "not found" or "unavailable".
# [ ] Any new read path that returns ``None`` for the service must
#     distinguish "the row does not exist" (404) from "the DB raised"
#     (500) explicitly. Wrapping both in the same branch is forbidden.
