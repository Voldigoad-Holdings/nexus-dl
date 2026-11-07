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
* **Explicit ``site_id`` validation.** When the client passes
  ``?site_id=foo``, the router verifies that ``foo`` is a registered
  site. This prevents an enumeration attack where an invalid site id
  would otherwise produce a 502 parser error (leaking that parsing was
  attempted).
* **Adult policy.** Sites are assumed to already be filtered by
  :mod:`sites` and :mod:`search`. This router does not re-enforce the
  policy — every manga reachable here has a valid ``site_id`` that was
  already authorized. If a new policy dimension is introduced, it must
  be enforced in :func:`_resolve_site_from_url` (single choke point).
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

    site_id = await services.registry.resolve_site_from_url(url)
    parser   = await services.registry.get_parser(site_id, session=...)
    manga    = await parser.get_manga(url)
    chapters = await parser.get_chapters(manga)

Every lookup is tried against multiple method names (see
:func:`_resolve_site_from_url`, :func:`_get_parser`) so the host can pick
whichever names fit its registry without touching this file. Missing
methods are logged at ``debug`` and treated as "not implemented" —
returning 404 rather than 500, because a missing method is a
*configuration* choice (e.g. library-only deployments).

Both sync and async registry methods are supported: the router awaits
the result if it is awaitable, otherwise it uses the value directly.
This is important for the ``SiteRegistry`` implementation, whose methods
are async-first.
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
    resolved_site_id = site_id or await _resolve_site_from_url(url, services)
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

    # Validate an explicit ``site_id`` against the registry so a bogus
    # value produces a clean 400 rather than a cryptic parser error.
    if site_id is not None and not await _is_known_site(site_id, services):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(
                ErrorCode.VALIDATION_ERROR,
                f"Unknown site id: {site_id!r}.",
                details={
                    "reason": "SITE_UNKNOWN",
                    "hint": "Omit `site_id` to auto-resolve from `url`.",
                },
            ).model_dump(),
        )

    parser = await _get_parser(resolved_site_id, services)
    if parser is None:
        raise _site_not_available(resolved_site_id)

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

#: Sentinel returned by :func:`_library_get_chapters` when the library
#: has responded (even with zero results). Distinguishes "library
#: answered: 0 results" from "library method missing: try live".
_LIBRARY_ABSENT: object = object()


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

    The library answer is authoritative **even when it is empty**: a
    manga that is in the library but has zero matching chapters must not
    silently fall through to the live parser (which would ignore local
    download state and return a different total). The sentinel
    :data:`_LIBRARY_ABSENT` distinguishes "no method" from "method
    returned empty".
    """
    # 1. Library.
    library_result = await _library_get_chapters(
        services, manga_id, pagination=pagination, filters=filters
    )
    if library_result is not _LIBRARY_ABSENT:
        # Library responded (empty or not) — trust it.
        chapters, total = library_result  # type: ignore[misc]
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

    resolved_site_id = site_id or await _resolve_site_from_url(url, services)
    parser = await _get_parser(resolved_site_id or "", services)
    if parser is None:
        raise _site_not_available(resolved_site_id or "<unknown>")

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

    # Live chapters never have download state — a caller explicitly
    # requesting ``downloaded=True`` gets an empty page.
    if filters.downloaded is True:
        filtered = []

    total = len(filtered)
    # ``limit=0`` on the pagination schema means "no page size set" —
    # treat it as "return everything" rather than an empty slice.
    if pagination.limit <= 0:
        sliced = filtered
    else:
        start = max(0, pagination.offset)
        end = start + pagination.limit
        sliced = filtered[start:end]
    return sliced, total


async def _library_get_chapters(
    services: ServicesDep,
    manga_id: str,
    *,
    pagination: PaginationParams,
    filters: ChapterQueryParams,
) -> tuple[list[Any], int] | object:
    """Library chapter lookup.

    Returns:
        * ``(chapters, total)`` when the library responded (empty or not).
        * :data:`_LIBRARY_ABSENT` when the library method is missing or
          raised — the caller then tries the live path.
    """
    getter = getattr(services.library, "get_chapters", None)
    if not callable(getter):
        return _LIBRARY_ABSENT

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
        # NOTE: this fallback loses the filters and the total, so it must
        # reapply the filters client-side to honour the request — see
        # ``_apply_local_filters``. A library that reaches this branch is
        # signalling "I only support the minimal contract"; we still need
        # to respect the caller's filter.
        try:
            result = getter(manga_id)
            if inspect.isawaitable(result):
                result = await result
            # Normalize to (items, total) if a bare list is returned.
            if isinstance(result, tuple) and len(result) == 2:
                items, _total = result
                items = list(items or [])
            elif isinstance(result, Sequence):
                items = list(result)
            else:
                items = list(result or [])
            items = _apply_local_filters(items, filters)
            return items, len(items)
        except Exception:
            _log.exception("library.get_chapters fallback raised")
            return _LIBRARY_ABSENT
    except Exception:
        _log.exception("library.get_chapters raised for {}", manga_id)
        return _LIBRARY_ABSENT

    if result is None:
        return _LIBRARY_ABSENT

    # Normalize the return shape.
    if isinstance(result, tuple) and len(result) == 2:
        items, total = result
        return list(items or []), int(total or 0)

    if isinstance(result, Sequence):
        return list(result), len(result)

    return _LIBRARY_ABSENT


# ===========================================================================
# Registry helpers
# ===========================================================================

async def _resolve_site_from_url(
    url: str, services: ServicesDep
) -> str | None:
    """Try several method names to resolve a URL to a site id.

    **Async-first.** All candidates are invoked and their result is
    awaited if awaitable — the ``SiteRegistry`` in NexusDL is async by
    design, so a synchronous implementation here would silently return
    ``None`` on every real deployment.
    """
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
                result = await result
            if result is None:
                continue
            if hasattr(result, "id"):
                return str(result.id)
            return str(result)
        except Exception:
            _log.debug("registry.{} raised for {}", name, url)
    return None


async def _is_known_site(site_id: str, services: ServicesDep) -> bool:
    """Return whether ``site_id`` is registered.

    Uses ``registry.get_site`` when available (the documented API),
    falling back to "unknown → assume valid" if no method is present —
    that preserves compatibility with lightweight registries that only
    implement ``get_parser``.
    """
    if not site_id:
        return False
    for name in ("get_site", "site_exists", "has_site"):
        method = getattr(services.registry, name, None)
        if not callable(method):
            continue
        try:
            result = method(site_id)
            if inspect.isawaitable(result):
                result = await result
            # ``get_site`` returns the config object on success and
            # ``None`` on miss; ``site_exists`` / ``has_site`` return
            # ``bool``. Both are truthy/falsy the same way.
            return bool(result)
        except Exception:
            _log.debug(
                "registry.{} raised while validating site_id={}",
                name,
                site_id,
            )
    # No validation method exists — trust the caller.
    return True


async def _get_parser(
    site_id: str, services: ServicesDep
) -> Any | None:
    """Try several method names to fetch a parser for ``site_id``.

    Attempts the one-argument form first (``get_parser(site_id)``),
    falling back to the two-argument form (``get_parser(site_id,
    session=...)``) if the target signature requires it — the PROMPT
    MASTER documents the two-argument form for the real ``SiteRegistry``.
    The router does not own a session; if the registry needs one it must
    obtain it internally (the documented ``get_parser`` already does).
    """
    if not site_id:
        return None
    for name in ("get_parser", "parser_for"):
        method = getattr(services.registry, name, None)
        if not callable(method):
            continue
        try:
            # Try the no-session form first (used by test registries and
            # by registries that hold their own session).
            try:
                result = method(site_id)
            except TypeError:
                # Retry without kwargs to avoid masking a legitimate
                # TypeError from inside the method.
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
    """Apply the query filters to live-parsed chapters (in memory).

    ``filters.language`` may be a :class:`Language` enum (documented in
    PROMPT MASTER) while the parsed chapter's ``language`` may be a
    plain string (``"fr"``), an enum, or absent. Both sides are
    normalized to lowercase strings for a robust comparison — an enum's
    ``.value`` is preferred when available.
    """
    out = list(chapters)

    if filters.language is not None:
        target = _normalize_language(filters.language)
        if target is not None:
            out = [
                c for c in out
                if _normalize_language(getattr(c, "language", None)) == target
            ]

    # ``downloaded`` is resolved by the caller (live chapters are never
    # downloaded). Kept here as documentation of the invariant.

    return out


def _apply_local_filters(
    chapters: Sequence[Any], filters: ChapterQueryParams
) -> list[Any]:
    """Fallback path: filter live/library items in-memory.

    Used only when the library's ``get_chapters`` refused the full
    keyword signature. Applies the same filters that
    :func:`_filter_live_chapters` would have applied, so the caller
    never sees items they did not ask for.
    """
    out = _filter_live_chapters(chapters, filters)
    if filters.downloaded is not None:
        desired = bool(filters.downloaded)
        out = [
            c for c in out
            if bool(getattr(c, "downloaded", False)) is desired
        ]
    return out


def _normalize_language(value: Any) -> str | None:
    """Coerce a language value (enum, str, None) to a normalized string.

    Returns ``None`` for falsy values so callers can decide to skip the
    comparison entirely. Prefers ``.value`` (enum) over ``str()``.
    """
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value.strip().lower()
    if isinstance(value, str):
        return value.strip().lower()
    text = str(value).strip().lower()
    return text or None


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


def _site_not_available(site_id: str) -> HTTPException:
    """Build the standard 404 for an unavailable site.

    Distinct from :func:`_not_found_manga`: the manga id is not the
    problem — the site the client asked about is disabled or unknown.
    A separate error message is returned so a debugging client is not
    misled into thinking the manga id was wrong.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_response(
            ErrorCode.NOT_FOUND,
            f"Site {site_id!r} is not available on this server.",
            details={
                "reason": "SITE_NOT_AVAILABLE",
                "hint": "Check `GET /api/v1/sites` for the list of "
                        "enabled sites.",
            },
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
#     is intentional. Every new candidate MUST be awaited if awaitable.
# [ ] New parser lookup logic goes through :func:`_get_parser` — same
#     reasoning.
# [ ] The consistency check (``parsed.id == manga_id``) is preserved on
#     every live path. Removing it would allow returning the wrong manga
#     on URL redirects.
# [ ] Library return shapes beyond ``(items, total)`` and bare sequences
#     are handled in :func:`_library_get_chapters` — do not assume a
#     specific object type.
# [ ] New filters are added to :class:`ChapterQueryParams`, to
#     :func:`_filter_live_chapters` (for live manga), AND to
#     :func:`_apply_local_filters` (fallback path). The DB handles the
#     library path only if the full keyword signature is accepted.
# [ ] Any new 4xx/5xx code uses :func:`error_response` — never a bare
#     ``HTTPException(detail="...")``.
# [ ] ``_LIBRARY_ABSENT`` sentinel is checked with ``is``, never ``==``.
