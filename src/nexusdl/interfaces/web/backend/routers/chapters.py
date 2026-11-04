"""Chapters router — ``/api/v1/chapters/{id}`` and ``/api/v1/chapters/{id}/pages``.

FastAPI glue for chapter detail and page listing endpoints.

Resolving ``{chapter_id}``
==========================

``Chapter.id`` is ``sha256(f"{manga_id}:{source_id}")[:16]`` — a
**one-way hash**. The router cannot derive the parent ``manga_id`` from
it, so it uses a **library-first** strategy analogous to
:mod:`manga`:

1. **Library hit** — ``library.get_chapter(chapter_id)`` returns the
   chapter (with ``manga_id`` baked in). Fast path; works offline even
   if the source site is down.
2. **Live fallback** — if the chapter is not in the library **and** the
   client provides ``?url=<manga_source_url>``, the router resolves the
   site from the URL, parses the manga, and searches its chapter list
   for one whose ``id`` matches ``chapter_id``. The consistency check is
   implicit: a parsed chapter whose id does not match is simply not the
   one the caller asked for.
3. **404** — if neither branch applies. The 404 body documents that
   ``?url=`` unlocks live parsing, so a well-behaved client can recover.

No ``?manga_id=`` is required. Adding it would be redundant (library
already resolves it; live re-derives it from the parse) and would
encourage clients to send stale state.

Pages
=====

The ``/pages`` endpoint returns the same way:

* **Library chapter** — ``library.get_pages(chapter_id)`` returns pages
  with ``filename`` set (the packaged file on disk). The client can build
  a static-download URL from ``filename``.
* **Live chapter** — ``parser.get_pages(chapter)`` returns pages with
  ``url`` set (the remote source image, protected by the site's cookies).

No mixing: a chapter is served from exactly one origin per request. The
client knows which by inspecting ``downloaded: bool`` on the chapter
detail response.

Authorization
=============

A chapter is visible iff its parent manga is visible. The adult-content
policy is enforced **once**, at manga access (:mod:`manga`). Adding a
second check here would duplicate the logic and produce confusing double
403s on sites that toggled ``allow_adult`` between the manga request and
the chapter request. Do not add one — if the policy needs to be
enforced at a new choke point, add it in :mod:`manga`, the single
authoritative gate.

Determinism assumption
======================

Live resolution relies on parsers being **deterministic**: the same
manga URL parsed twice yields the same chapter ids. This is a documented
contract of the parser layer (see :class:`BaseParser`). A parser that
violates it causes a live chapter request to 404 — the same failure
mode as a moved page. The response includes a hint so the caller knows
where to look.

Service contract (duck-typed)
=============================

* **Chapter lookup** — ``library.get_chapter(chapter_id)`` **or**
  ``library.get_chapter_with_manga(chapter_id)`` (returns ``Chapter`` or
  a ``(Chapter, Manga)`` tuple). Missing → ``None``.
* **Pages lookup** — ``library.get_pages(chapter_id)`` returns an
  iterable of domain :class:`Page` objects.
* **Live path** — same registry methods as :mod:`manga`:
  ``resolve_site_from_url`` / ``resolve_url`` / ``find_site_for_url``
  and ``get_parser`` / ``parser_for``. All may be sync or async.
"""
from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, status
from loguru import logger as _logger

from ..dependencies import CurrentUser, ServicesDep
from ..schemas.responses import (
    ChapterSummarySchema,
    ErrorCode,
    PageListResponse,
    PageSchema,
    error_response,
)

_log = _logger.bind(component="web.routers.chapters")

router = APIRouter(prefix="/api/v1/chapters", tags=["chapters"])

#: Maximum accepted length for the ``{chapter_id}`` path segment. The
#: canonical id is 16 hex chars; the extra headroom accommodates custom
#: parser id schemes.
_MAX_ID_LENGTH: int = 128


# ===========================================================================
# Endpoints
# ===========================================================================


@router.get(
    "/{chapter_id}",
    response_model=ChapterSummarySchema,
    summary="Get a chapter by id",
    description=(
        "Return a chapter's metadata.\n\n"
        "* If the chapter is in the caller's library, it is served from "
        "there (`downloaded: true`, `reading_progress` populated, works "
        "offline).\n"
        "* Otherwise, pass `?url=<manga_source_url>` to have the server "
        "parse the parent manga live and locate the chapter by id "
        "(`downloaded: false`, `reading_progress: 0`).\n"
        "* Without a library match and without `url`, the request returns "
        "404 with a hint.\n\n"
        "The `{chapter_id}` value is the canonical NexusDL chapter id — "
        "`sha256(manga_id:source_id)[:16]`."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "URL is malformed or the site cannot be resolved.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Chapter not found — not in library, or no "
                           "`url` supplied, or the live resolution failed.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "The source site is unreachable or the parser "
                           "rejected the response.",
        },
    },
)
async def get_chapter(
    chapter_id: Annotated[
        str,
        Path(
            max_length=_MAX_ID_LENGTH,
            description="Canonical NexusDL chapter id.",
        ),
    ],
    payload: CurrentUser,
    services: ServicesDep,
    url: Annotated[
        str | None,
        Query(
            description=(
                "Parent manga's source URL. Required when the chapter is "
                "not in the caller's library; ignored when it is."
            ),
        ),
    ] = None,
    site_id: Annotated[
        str | None,
        Query(
            description=(
                "Optional site id hint. Normally derived from `url`."
            ),
        ),
    ] = None,
) -> ChapterSummarySchema:
    chapter = await _resolve_chapter(
        chapter_id=chapter_id,
        url=url,
        site_id=site_id,
        services=services,
        user_id=payload.sub,
    )
    return _to_chapter_summary(chapter, chapter_id=chapter_id)


@router.get(
    "/{chapter_id}/pages",
    response_model=PageListResponse,
    summary="List a chapter's pages",
    description=(
        "Return the ordered pages of a chapter.\n\n"
        "* **Library chapter**: each page carries `filename` (packaged "
        "file on disk). `url` may also be present for reference.\n"
        "* **Live chapter** (via `?url=`): each page carries `url` (the "
        "remote source image). `filename` is `null`.\n"
        "* The response tells the two apart via `downloaded: bool`.\n\n"
        "Pages are returned in reading order, `index` starting at 0."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Chapter not found.",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "The source site is unreachable.",
        },
    },
)
async def list_chapter_pages(
    chapter_id: Annotated[str, Path(max_length=_MAX_ID_LENGTH)],
    payload: CurrentUser,
    services: ServicesDep,
    url: Annotated[str | None, Query()] = None,
    site_id: Annotated[str | None, Query()] = None,
) -> PageListResponse:
    chapter, pages, downloaded = await _resolve_pages(
        chapter_id=chapter_id,
        url=url,
        site_id=site_id,
        services=services,
        user_id=payload.sub,
    )

    page_schemas = [_to_page_schema(p, index=i) for i, p in enumerate(pages)]

    return PageListResponse(
        manga_id=_extract_manga_id(chapter) or "",
        chapter_id=chapter_id,
        pages=page_schemas,
        total=len(page_schemas),
    )


# ===========================================================================
# Resolution — chapter
# ===========================================================================


async def _resolve_chapter(
    *,
    chapter_id: str,
    url: str | None,
    site_id: str | None,
    services: ServicesDep,
    user_id: str | None,
) -> Any:
    """Library-first, then live fallback, then 404."""
    # 1. Library fast path.
    library_chapter = await _library_get_chapter(services, chapter_id)
    if library_chapter is not None:
        return library_chapter

    # 2. Live fallback requires a URL.
    if not url:
        raise _not_found_chapter(chapter_id)

    parsed_chapter = await _parse_live_chapter(
        chapter_id=chapter_id,
        url=url,
        site_id=site_id,
        services=services,
    )
    if parsed_chapter is None:
        # The URL did not yield a chapter with this id — either the
        # chapter was deleted at source, or the parser is not
        # deterministic, or the client sent the wrong URL.
        raise _not_found_chapter(chapter_id, hint=(
            "The live parse did not yield a chapter with this id. "
            "Verify the `url` points to the parent manga and that the "
            "chapter still exists at source."
        ))

    return parsed_chapter


async def _library_get_chapter(
    services: ServicesDep, chapter_id: str
) -> Any | None:
    """Fetch a chapter from the library, tolerant to return shape."""
    for name in ("get_chapter", "get_chapter_with_manga"):
        method = getattr(services.library, name, None)
        if not callable(method):
            continue
        try:
            result = method(chapter_id)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            _log.exception("library.{} raised for {}", name, chapter_id)
            continue
        if result is None:
            continue
        # Tolerate a (Chapter, Manga) tuple.
        if isinstance(result, tuple):
            chapter = result[0] if result else None
            if chapter is not None:
                return chapter
            continue
        return result
    return None


async def _parse_live_chapter(
    *,
    chapter_id: str,
    url: str,
    site_id: str | None,
    services: ServicesDep,
) -> Any | None:
    """Parse the parent manga and find a chapter whose id matches."""
    resolved_site_id = site_id or _resolve_site_from_url(url, services)
    if not resolved_site_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(
                ErrorCode.VALIDATION_ERROR,
                f"Cannot determine which site serves this URL: {url!r}.",
                details={
                    "reason": "SITE_UNRESOLVED",
                    "hint": "Pass `site_id=` explicitly for custom "
                            "parsers whose URLs do not match their "
                            "domains.",
                },
            ).model_dump(),
        )

    parser = await _get_parser(resolved_site_id, services)
    if parser is None:
        raise _not_found_chapter(chapter_id, hint=(
            f"Site {resolved_site_id!r} is not enabled on this server."
        ))

    manga = await _call_parser(parser, "get_manga", url)
    chapters = await _call_parser(parser, "get_chapters", manga)
    for candidate in chapters or ():
        if _extract_chapter_id(candidate) == chapter_id:
            return candidate
    return None


# ===========================================================================
# Resolution — pages
# ===========================================================================


async def _resolve_pages(
    *,
    chapter_id: str,
    url: str | None,
    site_id: str | None,
    services: ServicesDep,
    user_id: str | None,
) -> tuple[Any, list[Any], bool]:
    """Return ``(chapter, pages, downloaded)``.

    ``downloaded`` tells the caller whether the pages came from the
    library (``True``) or from a live parse (``False``). The two lists
    have different field semantics — see the module docstring.
    """
    # 1. Library chapter + pages.
    library_chapter = await _library_get_chapter(services, chapter_id)
    if library_chapter is not None:
        library_pages = await _library_get_pages(services, chapter_id)
        if library_pages is not None:
            return library_chapter, list(library_pages), True
        # Chapter exists in library but pages were not stored (partial
        # download, DB-only entry, …). Fall through to live if possible.

    # 2. Live fallback requires a URL.
    if not url:
        raise _not_found_chapter(chapter_id)

    parsed_chapter = await _parse_live_chapter(
        chapter_id=chapter_id,
        url=url,
        site_id=site_id,
        services=services,
    )
    if parsed_chapter is None:
        raise _not_found_chapter(chapter_id, hint=(
            "The live parse did not yield a chapter with this id. "
            "Verify the `url` points to the parent manga."
        ))

    resolved_site_id = site_id or _resolve_site_from_url(url, services)
    parser = await _get_parser(resolved_site_id or "", services)
    if parser is None:
        raise _not_found_chapter(chapter_id, hint=(
            f"Site {resolved_site_id!r} is not enabled on this server."
        ))

    pages = await _call_parser(parser, "get_pages", parsed_chapter)
    return parsed_chapter, list(pages or []), False


async def _library_get_pages(
    services: ServicesDep, chapter_id: str
) -> list[Any] | None:
    """Fetch pages from the library, tolerant to shape.

    Returns ``None`` when the method is absent or fails — the caller
    falls through to the live path.
    """
    method = getattr(services.library, "get_pages", None)
    if not callable(method):
        return None
    try:
        result = method(chapter_id)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        _log.exception("library.get_pages raised for {}", chapter_id)
        return None
    if result is None:
        return None
    try:
        return list(result)
    except TypeError:
        return None


# ===========================================================================
# Registry + parser helpers
# ===========================================================================
#
# These mirror the helpers in ``manga.py`` deliberately: the two routers
# share the same registry / parser contract but must remain independent
# modules so each can evolve without touching the other. Any change to
# the registry interface should land in both files in the same PR.


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


async def _call_parser(parser: Any, method_name: str, *args: Any) -> Any:
    """Invoke ``parser.<method_name>(*args)``, awaiting if needed."""
    method = getattr(parser, method_name, None)
    if not callable(method):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_response(
                ErrorCode.PARSER_ERROR,
                f"Parser does not implement {method_name!r}.",
            ).model_dump(),
        )
    try:
        result = method(*args)
        if inspect.isawaitable(result):
            result = await result
        return result
    except HTTPException:
        raise
    except Exception:
        _log.exception("parser.{} raised", method_name)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_response(
                ErrorCode.PARSER_ERROR,
                f"Parser failed to execute {method_name!r}. The source "
                "site may be unavailable or have changed its layout.",
            ).model_dump(),
        )


# ===========================================================================
# Mapping helpers
# ===========================================================================


def _to_chapter_summary(chapter: Any, *, chapter_id: str) -> ChapterSummarySchema:
    """Project a domain chapter into ``ChapterSummarySchema``.

    Fills ``id`` from ``chapter_id`` when the domain object omits it
    (defensive: the path is authoritative).
    """
    try:
        summary = ChapterSummarySchema.model_validate(chapter)
    except Exception:
        _log.exception("Failed to validate chapter into ChapterSummarySchema")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to serialize chapter.",
            ).model_dump(),
        )
    if not summary.id:
        summary = summary.model_copy(update={"id": chapter_id})
    return summary


def _to_page_schema(page: Any, *, index: int) -> PageSchema:
    """Project a domain page into ``PageSchema``.

    The library path returns pages with ``filename`` set; the live path
    with ``url`` set. Both may be present when the domain object carries
    them. ``index`` is authoritative from the router — ensures pages are
    emitted in order even if the domain objects have stale indices.
    """
    try:
        if isinstance(page, PageSchema):
            return page.model_copy(update={"index": index})
        # Domain object: read attributes defensively.
        return PageSchema(
            index=index,
            url=str(getattr(page, "url", "") or ""),
            filename=_maybe_str(getattr(page, "filename", None)),
            size_bytes=_maybe_int(getattr(page, "size_bytes", None)),
            width=_maybe_int(getattr(page, "width", None)),
            height=_maybe_int(getattr(page, "height", None)),
        )
    except Exception:
        _log.exception("Failed to project page at index {}", index)
        # Never fail the whole response for one bad page; emit an empty
        # entry so the client can flag the gap.
        return PageSchema(index=index, url="")


def _extract_manga_id(chapter: Any) -> str | None:
    """Return the chapter's ``manga_id``, or ``None``."""
    if chapter is None:
        return None
    raw = getattr(chapter, "manga_id", None)
    return str(raw) if raw is not None else None


def _extract_chapter_id(chapter: Any) -> str | None:
    """Return the chapter's ``id``, or ``None``."""
    if chapter is None:
        return None
    raw = getattr(chapter, "id", None)
    return str(raw) if raw is not None else None


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Errors
# ===========================================================================


def _not_found_chapter(
    chapter_id: str, *, hint: str | None = None
) -> HTTPException:
    """Standard 404 for a chapter the caller cannot resolve."""
    details: dict[str, Any] = {
        "hint": hint
        or (
            "If this chapter is not in your library, provide the parent "
            "manga's source URL via the `url` query parameter to parse "
            "the chapter live."
        ),
    }
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_response(
            ErrorCode.NOT_FOUND,
            f"Chapter {chapter_id!r} not found.",
            details=details,
        ).model_dump(),
    )


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] Adult-content enforcement is **not** duplicated here. The policy
#     is enforced at manga access (manga.py); adding a second check
#     would produce double-403 confusion. If you must re-enforce, do it
#     in manga.py — that is the single authoritative gate.
#
# [ ] New registry / parser helpers stay in this module's
#     ``_resolve_site_from_url`` / ``_get_parser`` / ``_call_parser``.
#     Do not import from ``manga.py`` — the routers are independent
#     modules by design; the duplication is intentional and small.
#
# [ ] Any new ``?url=`` based resolution preserves the "parser is
#     deterministic" assumption. If a future parser violates it, the
#     fix goes in the parser, not here.
#
# [ ] New page fields are added to ``PageSchema`` (responses.py) and
#     populated in ``_to_page_schema``. Never ``model_validate`` a raw
#     page object directly — the router overrides ``index`` to guarantee
#     ordering.
#
# [ ] A failed page projection does **not** fail the whole response.
#     ``_to_page_schema`` emits an empty ``url`` entry on error; the
#     client can flag the gap and retry the specific page.
