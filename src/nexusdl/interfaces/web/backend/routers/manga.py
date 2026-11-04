"""Manga router — ``/api/v1/manga/{id}`` and ``/api/v1/manga/{id}/chapters``.

Thin FastAPI glue for the manga detail + chapter list endpoints.

Resolving ``{manga_id}``
========================

``Manga.id`` is ``sha256(f"{site}:{source_id}")[:16]`` — a **one-way
hash**. The router never guesses the site from the id. Instead it uses
a **library-first** strategy:

1. **Library hit** — the id is stored in the local DB (manga was at
   least scanned/imported). Fast path, always preferred.
2. **Live fallback** — if the id is not in the library **and** the client
   provides ``?url=<source_url>``, the router resolves the site from the
   URL and parses the manga live. This supports the standard discovery
   flow (search → click → view detail → download).
3. **404** — if neither branch applies. The 404 body explains that a
   ``url`` query parameter unlocks live parsing, so a well-behaved client
   can recover without reading the docs.

Safety
======

* **Consistency check.** On the live path, the parsed manga's id is
  compared against ``{manga_id}``. On mismatch the request is rejected
  with 404 — a redirect, a moved page, or a client bug cannot silently
  return the wrong manga.
* **Adult policy.** Sites are assumed to already be filtered by
  :mod:`sites` and :mod:`search`. This router does not re-enforce the
  policy — every manga reachable here has a valid ``site_id`` that was
  already authorized. If a new policy dimension is introduced, it must
  be enforced in ``_resolve_site_from_url`` (single choke point).
* **Path length guard.** ``manga_id`` is capped at 128 characters to
  reject obviously malformed input before any I/O.

Service contract (duck-typed)
=============================

For the **library** branch::

    manga = await services.library.get_manga(manga_id)
    # -> domain Manga | None

    chapters, total = await services.library.get_chapters(
        manga_id=manga_id,
        offset=int, limit=int,
        language=Language | None,
        downloaded_only=bool | None,
    )

For the **live** branch::

    site_id = services.registry.resolve_site_from_url(url)     # sync or async
    parser   = services.registry.get_parser(site_id)            # sync or async
    manga    = await parser.get_manga(url)                      # domain Manga
    chapters = await parser.get_chapters(manga)                 # list[Chapter]

Every lookup is tried against multiple method names (see
:func:`_resolve_site_from_url`, :func:`_get_parser`) so the host can pick
whichever names fit its registry without touching this file. Missing
methods are logged at ``debug`` and treated as "not implemented" —
returning 404 rather than 500, because a missing method is a
*configuration* choice (e.g. library-only deployments).
"""
from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from loguru import logger as _logger

from ..dependencies import CurrentUser, ServicesDep
from ..schemas.common import ChapterQueryParams, PaginationParams
from ..schemas.responses import (
    ChapterListResponse,
    ChapterSummarySchema,
    ErrorCode,
    MangaDetailSchema,
    ReadingProgressSchema,
    error_response,
)

_log = _logger.bind(component="web.routers.manga")

router = APIRouter(prefix="/api/v1", tags=["manga"])

#: Max length accepted for the ``{manga_id}`` path segment. Guards against
#: accidentally long garbage before any I/O is attempted.
_MAX_ID_LENGTH: int = 128


# ===========================================================================
# Endpoints
# ===========================================================================

@router.get(
    "/manga/{manga_id}",
    response_model=MangaDetailSchema,
    summary="Get a manga by id",
    description=(
        "Return full detail for a manga.\n\n"
        "* If the manga is in the caller's library, it is served from "
        "there (`in_library: true`, `reading_progress` populated).\n"
        "* Otherwise, pass `?url=<source_url>` to have the server parse "
        "the manga live from its source site (`in_library: false`).\n"
        "* Without a library match and without `url`, the request returns "
        "404 with a hint.\n\n"
        "The `{manga_id}` value is the canonical NexusDL manga id — "
        "`sha256(site:source_id)[:16]`."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "URL is malformed or the site cannot be resolved.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Manga not found — not in library, or no `url` "
                           "supplied, or the live resolution failed.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "The source site is unreachable or the parser "
                           "rejected the response.",
        },
    },
)
async def get_manga(
    manga_id: Annotated[
        str,
        Path(
            max_length=_MAX_ID_LENGTH,
            description="Canonical NexusDL manga id (16 hex chars by "
                        "default, but any non-empty string is accepted).",
        ),
    ],
    payload: CurrentUser,
    services: ServicesDep,
    url: Annotated[
        str | None,
        Query(
            description=(
                "Source URL for a manga that is **not** in the library. "
                "Ignored when the manga is found locally. Required to "
                "view a manga discovered through search but not yet "
                "downloaded."
            ),
        ),
    ] = None,
    site_id: Annotated[
        str | None,
        Query(
            description=(
                "Optional site id hint. Normally derived from `url`; "
                "provide it only when the site cannot be inferred from "
                "the URL (custom parsers with non-standard URLs)."
            ),
        ),
    ] = None,
) -> MangaDetailSchema:
    manga = await _resolve_manga(
        manga_id=manga_id,
        url=url,
        site_id=site_id,
        services=services,
        user_id=payload.sub,
    )
    return manga


@router.get(
    "/manga/{manga_id}/chapters",
    response_model=ChapterListResponse,
    summary="List a manga's chapters",
    description=(
        "Return the chapters of a manga, paginated.\n\n"
        "* Library manga: served from the local DB, with per-chapter "
        "`downloaded` and `reading_progress` populated.\n"
        "* Live manga (via `?url=`): parsed from the source site, with "
        "`downloaded=false` and `reading_progress=0` throughout.\n"
        "* Filters: `language`, `downloaded`."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Manga not found.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "The source site is unreachable.",
        },
    },
)
async def list_chapters(
    manga_id: Annotated[str, Path(max_length=_MAX_ID_LENGTH)],
    pagination: Annotated[PaginationParams, Depends()],
    filters: Annotated[ChapterQueryParams, Depends()],
    payload: CurrentUser,
    services: ServicesDep,
    url: Annotated[str | None, Query()] = None,
    site_id: Annotated[str | None, Query()] = None,
) -> ChapterListResponse:
    chapters, total = await _resolve_chapters(
        manga_id=manga_id,
        url=url,
        site_id=site_id,
        pagination=pagination,
        filters=filters,
        services=services,
        user_id=payload.sub,
    )
    items = [_to_chapter_summary(c, manga_id=manga_id) for c in chapters]
    return ChapterListResponse(
        items=items,
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        manga_id=manga_id,
    )


# ===========================================================================
# Resolution — manga
# ===========================================================================

async def _resolve_manga(
    *,
    manga_id: str,
    url: str | None,
    site_id: str | None,
    services: ServicesDep,
    user_id: str | None,
) -> MangaDetailSchema:
    """Library-first, then live fallback, then 404."""
    # 1. Library fast path.
    library_manga = await _library_get_manga(services, manga_id)
    if library_manga is not None:
        return _to_detail_schema(
            library_manga,
            in_library=True,
        )

    # 2. Live fallback requires a URL.
    if not url:
        raise _not_found_manga(manga_id)

    parsed = await _parse_live_manga(
        url=url,
        site_id=site_id,
        services=services,
    )

    # 3. Consistency check — the URL must resolve to the manga we were
    #    asked about.
    parsed_id = _extract_manga_id(parsed)
    if parsed_id is None or parsed_id != manga_id:
        _log.warning(
            "Manga id mismatch: requested={} parsed={} url={}",
            manga_id,
            parsed_id,
            url,
        )
        raise _not_found_manga(manga_id)

    return _to_detail_schema(parsed, in_library=False)


async def _library_get_manga(
    services: ServicesDep, manga_id: str
) -> Any | None:
    """Library lookup, tolerating ``None`` or a raised exception."""
    getter = getattr(services.library, "get_manga", None)
    if not callable(getter):
        return None
    try:
        result = getter(manga_id)
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception:
        _log.exception("library.get_manga raised for {}", manga_id)
        return None


async def _parse_live_manga(
    *,
    url: str,
    site_id: str | None,
    services: ServicesDep,
) -> Any:
    """Resolve the site for ``url``, get its parser, and parse the manga."""
    resolved_site_id = site_id or _resolve_site_from_url(url, services)
    if not resolved_site_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(
                ErrorCode.VALIDATION_ERROR,
                f"Cannot determine which site serves this URL: {url!r}.",
                details={
                    "reason": "SITE_UNRESOLVED",
                    "hint": "Pass `site_id=` explicitly for custom parsers "
                            "whose URLs do not match their domains.",
                },
            ).model_dump(),
        )

    parser = await _get_parser(resolved_site_id, services)
    if parser is None:
        raise _not_found_manga(
            manga_id="<unknown>",
            hint=(
                f"Site {resolved_site_id!r} is not enabled on this server."
            ),
        )

    try:
        manga = parser.get_manga(url)
        if inspect.isawaitable(manga):
            manga = await manga
        return manga
    except Exception:
        _log.exception(
            "parser.get_manga failed site={} url={}", resolved_site_id, url
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_response(
                ErrorCode.PARSER_ERROR,
                f"Failed to parse manga from {resolved_site_id!r}. "
                "The source site may be unavailable or have changed its "
                "layout.",
            ).model_dump(),
        )


# ===========================================================================
# Resolution — chapters
# ===========================================================================

async def _resolve_chapters(
    *,
    manga_id: str,
    url: str | None,
    site_id: str | None,
    pagination: PaginationParams,
    filters: ChapterQueryParams,
    services: ServicesDep,
    user_id: str | None,
) -> tuple[list[Any], int]:
    """Library-first, then live fallback, then 404.

    Returns ``(chapter_slice, total)`` where ``total`` is the count
    **before** pagination — matching :class:`PaginatedResponse`'s contract.
    """
    # 1. Library.
    library_result = await _library_get_chapters(
        services, manga_id, pagination=pagination, filters=filters
    )
    if library_result is not None:
        chapters, total = library_result
        if chapters or total > 0:
            return chapters, total

    # 2. Live fallback requires a URL.
    if not url:
        raise _not_found_manga(manga_id)

    manga = await _parse_live_manga(
        url=url, site_id=site_id, services=services
    )
    parsed_id = _extract_manga_id(manga)
    if parsed_id is None or parsed_id != manga_id:
        raise _not_found_manga(manga_id)

    resolved_site_id = site_id or _resolve_site_from_url(url, services)
    parser = await _get_parser(resolved_site_id or "", services)
    if parser is None:
        raise _not_found_manga(manga_id)

    try:
        raw_chapters = parser.get_chapters(manga)
        if inspect.isawaitable(raw_chapters):
            raw_chapters = await raw_chapters
    except Exception:
        _log.exception(
            "parser.get_chapters failed site={} manga={}",
            resolved_site_id,
            manga_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_response(
                ErrorCode.PARSER_ERROR,
                "Failed to fetch chapter list from the source site.",
            ).model_dump(),
        )

    all_chapters = list(raw_chapters or [])
    filtered = _filter_live_chapters(all_chapters, filters)

    # Live chapters never have download state — filter accordingly.
    if filters.downloaded is True:
        filtered = []

    total = len(filtered)
    start = pagination.offset
    end = start + pagination.limit
    return filtered[start:end], total


async def _library_get_chapters(
    services: ServicesDep,
    manga_id: str,
    *,
    pagination: PaginationParams,
    filters: ChapterQueryParams,
) -> tuple[list[Any], int] | None:
    """Library chapter lookup. Returns ``None`` if the method is absent."""
    getter = getattr(services.library, "get_chapters", None)
    if not callable(getter):
        return None
    try:
        result = getter(
            manga_id=manga_id,
            offset=pagination.offset,
            limit=pagination.limit,
            language=filters.language,
            downloaded_only=filters.downloaded,
        )
        if inspect.isawaitable(result):
            result = await result
    except TypeError:
        # Some implementations use positional args or omit some kwargs.
        try:
            result = getter(manga_id)
            if inspect.isawaitable(result):
                result = await result
            # Normalize to (items, total) if a bare list is returned.
            if isinstance(result, Sequence) and not isinstance(result, tuple):
                result = (list(result), len(result))
            else:
                result = (list(result or []), 0)
        except Exception:
            _log.exception("library.get_chapters fallback raised")
            return None
    except Exception:
        _log.exception("library.get_chapters raised for {}", manga_id)
        return None

    if result is None:
        return None

    # Normalize the return shape.
    if isinstance(result, tuple) and len(result) == 2:
        items, total = result
        return list(items or []), int(total or 0)

    if isinstance(result, Sequence):
        return list(result), len(result)

    return None


# ===========================================================================
# Registry helpers
# ===========================================================================

def _resolve_site_from_url(url: str, services: ServicesDep) -> str | None:
    """Try several method names to resolve a URL to a site id."""
    for name in (
        "resolve_site_from_url",
        "resolve_url",
        "find_site_for_url",
    ):
        method = getattr(services.registry, name, None)
        if not callable(method):
            continue
        try:
            result = method(url)
            if inspect.isawaitable(result):
                # Async resolution would need to be awaited by an async
                # caller — we are synchronous here, so we skip and log.
                _log.debug(
                    "registry.{} returned an awaitable; skipping", name
                )
                continue
            if result is None:
                continue
            if hasattr(result, "id"):
                return str(result.id)
            return str(result)
        except Exception:
            _log.debug("registry.{} raised for {}", name, url)
    return None


async def _get_parser(
    site_id: str, services: ServicesDep
) -> Any | None:
    """Try several method names to fetch a parser for ``site_id``."""
    if not site_id:
        return None
    for name in ("get_parser", "parser_for"):
        method = getattr(services.registry, name, None)
        if not callable(method):
            continue
        try:
            result = method(site_id)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return result
        except Exception:
            _log.debug("registry.{} raised for {}", name, site_id)
    return None


# ===========================================================================
# Mapping helpers
# ===========================================================================

def _to_detail_schema(
    manga: Any,
    *,
    in_library: bool,
) -> MangaDetailSchema:
    """Project a domain ``Manga`` to ``MangaDetailSchema``.

    Always sets ``in_library`` and ``reading_progress`` explicitly, even
    if the domain object omits them. Defaults are safe: ``False`` and an
    empty :class:`ReadingProgressSchema`.
    """
    try:
        base = MangaDetailSchema.model_validate(manga)
    except Exception:
        _log.exception("Failed to validate manga into MangaDetailSchema")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to serialize manga detail.",
            ).model_dump(),
        )

    # The domain object may already carry reading_progress; keep it.
    progress = base.reading_progress or ReadingProgressSchema()

    return base.model_copy(
        update={
            "in_library": in_library,
            "reading_progress": progress,
        }
    )


def _to_chapter_summary(
    chapter: Any, *, manga_id: str
) -> ChapterSummarySchema:
    """Project a domain ``Chapter`` to ``ChapterSummarySchema``.

    Fills ``manga_id`` if the domain object does not carry it — the
    caller already knows it, so it is authoritative.
    """
    try:
        summary = ChapterSummarySchema.model_validate(chapter)
    except Exception:
        _log.exception(
            "Failed to validate chapter into ChapterSummarySchema"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to serialize chapter.",
            ).model_dump(),
        )
    if not summary.manga_id:
        summary = summary.model_copy(update={"manga_id": manga_id})
    return summary


def _extract_manga_id(manga: Any) -> str | None:
    """Return the domain manga's ``id`` attribute, or ``None``."""
    if manga is None:
        return None
    raw = getattr(manga, "id", None)
    if raw is None:
        return None
    return str(raw)


def _filter_live_chapters(
    chapters: Sequence[Any], filters: ChapterQueryParams
) -> list[Any]:
    """Apply the query filters to live-parsed chapters (in memory)."""
    out = list(chapters)

    if filters.language is not None:
        out = [
            c for c in out
            if getattr(c, "language", None) == filters.language
        ]

    # ``downloaded`` is resolved by the caller (live chapters are never
    # downloaded). Kept here as documentation of the invariant.

    return out


# ===========================================================================
# Errors
# ===========================================================================

def _not_found_manga(
    manga_id: str, *, hint: str | None = None
) -> HTTPException:
    """Build the standard 404 for a manga the caller cannot see."""
    details: dict[str, Any] = {
        "hint": hint
        or (
            "If this manga is not in your library, provide the source URL "
            "via the `url` query parameter to parse it live."
        ),
    }
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_response(
            ErrorCode.NOT_FOUND,
            f"Manga {manga_id!r} not found.",
            details=details,
        ).model_dump(),
    )


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] New site-resolution logic stays inside :func:`_resolve_site_from_url`
#     or adds a new method name to its candidate list — the choke point
#     is intentional.
# [ ] New parser lookup logic goes through :func:`_get_parser` — same
#     reasoning.
# [ ] The consistency check (``parsed.id == manga_id``) is preserved on
#     every live path. Removing it would allow returning the wrong manga
#     on URL redirects.
# [ ] Library return shapes beyond ``(items, total)`` and bare sequences
#     are handled in :func:`_library_get_chapters` — do not assume a
#     specific object type.
# [ ] New filters are added to :class:`ChapterQueryParams` AND to
#     :func:`_filter_live_chapters` (for live manga) — the DB handles the
#     library path.
# [ ] Any new 4xx/5xx code uses :func:`error_response` — never a bare
#     ``HTTPException(detail="...")``.
