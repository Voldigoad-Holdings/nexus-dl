"""Search router — ``POST /api/v1/search``.

Thin FastAPI glue for the multi-site search endpoint. Responsibilities:

* Enforce the **adult content policy** (``Settings.content.allow_adult``)
  using the same semantics as :mod:`sites`.
* **Reject empty searches** early: a request with no query, no
  ``site_ids`` and no ``language`` would trigger a fan-out across every
  site for nothing. Rejecting with 422 keeps the fan-out tractable and
  tells the client what they did wrong.
* Delegate execution to the host's search service. The router is
  **agnostic about how the service is implemented** — it accepts several
  return shapes via :func:`_normalize_outcome` so both
  ``services.search_web(request)`` (Web-native, multi-site) and
  ``services.search(request)`` (GUI-style, single-site) can drive it
  without a router change.
* **Best-effort fan-out.** When some sites fail to respond, the response
  is still 200 — the failing site ids are exposed in ``sites_failed`` so
  the client can surface them, retry, or ignore them. Never a 500 just
  because 2 out of 60 sites timed out.
* Project to :class:`~..schemas.responses.SearchResponse` (a
  ``PaginatedResponse[MangaSummarySchema]`` with query echo and
  per-request site metadata).

Rate limiting
=============

This endpoint is expensive (fan-out N sites). The per-user rate limit
(``30/min`` per the API contract) is enforced by the
:mod:`..middleware.rate_limit` layer, not here. Removing that middleware
removes the protection — do not rely on this router for throttling.

Service contract
================

The router looks for a search service on ``ServicesDep`` in this order:

1. ``await services.search_web(request)`` — preferred. Receives the
   validated :class:`~..schemas.requests.SearchRequest` and returns
   either a rich object (``.items``, ``.total``, ``.sites_queried``,
   ``.sites_failed``) or a simpler shape (see :func:`_normalize_outcome`).
2. ``await services.search(request)`` — fallback for services that
   implement the GUI contract. The service is expected to adapt single-
   site searches to the Web shape internally.

If neither method exists, the router raises a **500** with a clear
message — silent fallbacks would make composition-root bugs invisible.

Normalized return shapes accepted by :func:`_normalize_outcome`:

* Object with ``.items``, ``.total``, ``.sites_queried``, ``.sites_failed``
  — the recommended shape.
* Object with ``.items`` and ``.total`` only — ``sites_queried`` is
  inferred from the request, ``sites_failed`` is ``[]``.
* ``tuple(items, total)`` — same inference as above.
* ``list[item]`` — ``total = len(items)``, same inference as above.
"""
from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from loguru import logger as _logger

from ..dependencies import CurrentUser, ServicesDep
from ..schemas.requests import SearchRequest
from ..schemas.responses import (
    ErrorCode,
    MangaSummarySchema,
    SearchResponse,
    error_response,
)

_log = _logger.bind(component="web.routers.search")

router = APIRouter(prefix="/api/v1", tags=["search"])


# ===========================================================================
# Endpoint
# ===========================================================================

@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Search manga across sites",
    description=(
        "Search a free-text query across one or more sites, or browse a "
        "specific site with an empty query.\n\n"
        "* At least one of `query`, `site_ids` or `language` must be "
        "provided — an empty search would trigger a fan-out across every "
        "enabled site for nothing.\n"
        "* `include_adult` is honored only when the server policy allows "
        "adult content (`Settings.content.allow_adult`). Forcing `true` "
        "against the policy returns 403.\n"
        "* The search is **best-effort**: sites that time out or error "
        "are reported in `sites_failed` and do not fail the request.\n"
        "* This endpoint is rate-limited (30/min per user by default)."
    ),
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Missing search criteria or invalid query.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Adult content is disabled by policy.",
        },
    },
)
async def search(
    body: SearchRequest,
    payload: CurrentUser,
    services: ServicesDep,
) -> SearchResponse:
    # --- 1. Validate criteria ----------------------------------------
    _validate_criteria(body)

    # --- 2. Resolve effective adult flag -----------------------------
    policy_allows_adult = _policy_allows_adult(services)
    include_adult = _resolve_include_adult(
        requested=body.include_adult,
        policy_allows=policy_allows_adult,
    )

    # --- 3. Delegate to the service ----------------------------------
    search_callable, method_name = _resolve_search_method(services)
    try:
        raw_result = await _invoke_search(
            search_callable,
            body,
            include_adult=include_adult,
            requested_by=payload.sub,
        )
    except Exception:
        _log.exception(
            "Search failed (method={}, user={}, query_len={}, sites={})",
            method_name,
            payload.sub,
            len(body.query),
            body.site_ids or "<all>",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_response(
                ErrorCode.PARSER_ERROR,
                "Search failed. Please retry in a moment.",
            ).model_dump(),
        )

    # --- 4. Normalize + project --------------------------------------
    outcome = _normalize_outcome(raw_result, body)
    _log.debug(
        "Search user={} method={} items={} total={} failed_sites={}",
        payload.sub,
        method_name,
        len(outcome.items),
        outcome.total,
        len(outcome.sites_failed),
    )

    items = [MangaSummarySchema.model_validate(m) for m in outcome.items]

    return SearchResponse(
        items=items,
        total=outcome.total,
        page=outcome.page,
        per_page=body.per_page,
        query=body.query,
        sites_queried=list(outcome.sites_queried),
        sites_failed=list(outcome.sites_failed),
    )


# ===========================================================================
# Validation
# ===========================================================================

def _validate_criteria(body: SearchRequest) -> None:
    """Reject requests that would trigger a pointless full fan-out.

    A request must target **something**: a query string, a language
    filter, or an explicit ``site_ids`` list. All three empty/absent
    means the client is asking "give me the whole internet" — which
    neither the site list nor the underlying HTTP budget can support.
    """
    has_query = bool(body.query.strip())
    has_sites = bool(body.site_ids)
    has_language = body.language is not None

    if not (has_query or has_sites or has_language):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_response(
                ErrorCode.VALIDATION_ERROR,
                "Provide at least one of 'query', 'site_ids', or 'language'.",
                details={
                    "reason": "EMPTY_SEARCH",
                    "hint": "A completely empty search would query every "
                            "enabled site for nothing.",
                },
            ).model_dump(),
        )


# ===========================================================================
# Adult policy
# ===========================================================================

def _policy_allows_adult(services: ServicesDep) -> bool:
    """Return the server's adult-content policy. Never raises."""
    try:
        return bool(services.settings.content.allow_adult)
    except Exception:
        return False


def _resolve_include_adult(
    *, requested: bool | None, policy_allows: bool
) -> bool:
    """Compute the effective ``include_adult`` flag (same as sites).

    * ``None`` → use policy.
    * ``False`` → filter adult sites out (always safe).
    * ``True`` + policy → include them.
    * ``True`` + no policy → **403** (explicit bypass attempt).
    """
    if requested is None:
        return policy_allows
    if requested is False:
        return False
    if policy_allows:
        return True
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=error_response(
            ErrorCode.FORBIDDEN,
            "Adult content is disabled by the server policy.",
            details={
                "reason": "ADULT_CONTENT_DISABLED",
                "hint": "An administrator can enable it via "
                        "`PATCH /api/v1/settings` "
                        "(key: `content.allow_adult`).",
            },
        ).model_dump(),
    )


# ===========================================================================
# Service resolution / invocation
# ===========================================================================

def _resolve_search_method(
    services: ServicesDep,
) -> tuple[Any, str]:
    """Return ``(callable, name)`` for the search method to invoke.

    Prefers ``services.search_web`` (Web-native) then ``services.search``
    (GUI-compatible). Raising here is intentional: a missing method is a
    composition-root bug, not a runtime condition — we want it loud.
    """
    for name in ("search_web", "search"):
        method = getattr(services, name, None)
        if callable(method):
            return method, name
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=error_response(
            ErrorCode.INTERNAL_ERROR,
            "Search service is not configured "
            "(expected `search_web` or `search` on the service object).",
        ).model_dump(),
    )


async def _invoke_search(
    search_callable: Any,
    body: SearchRequest,
    *,
    include_adult: bool,
    requested_by: str | None,
) -> Any:
    """Invoke the search method, forwarding the resolved adult flag.

    Two calling conventions are supported:

    * ``search(request: SearchRequest)`` — the request object carries
      ``include_adult`` already; the router mutates ``body``? No — the
      request is frozen. Instead, we pass the resolved flag via a
      dedicated kwarg so the service cannot miss the policy decision.
    * Service implementations that accept
      ``search(request, *, include_adult, requested_by)`` — ideal.

    If the service only takes a positional ``request`` argument, the
    router falls back to passing ``body.model_copy(update={"include_adult":
    include_adult})`` so the resolved value travels with the request.
    """
    # Build a request with the resolved include_adult so services that
    # only inspect `request.include_adult` still see the policy decision.
    resolved_request = body.model_copy(
        update={"include_adult": include_adult}
    )

    # Try the rich contract first.
    try:
        result = search_callable(
            resolved_request,
            requested_by=requested_by,
        )
    except TypeError as exc:
        # Signature mismatch → fallback to plain positional call.
        if "requested_by" not in str(exc):
            raise
        result = search_callable(resolved_request)

    if inspect.isawaitable(result):
        return await result
    return result


# ===========================================================================
# Outcome normalization
# ===========================================================================

@dataclass(frozen=True, slots=True)
class _Outcome:
    """Internal normalized form of a search result."""

    items: list[Any]
    total: int
    page: int
    sites_queried: tuple[str, ...]
    sites_failed: tuple[str, ...]


def _normalize_outcome(raw: Any, request: SearchRequest) -> _Outcome:
    """Coerce any of the supported service return shapes into :class:`_Outcome`.

    Supported shapes (see module docstring):

    * Rich object: ``.items``, ``.total``, ``.sites_queried``,
      ``.sites_failed`` — used verbatim.
    * Sparse object: ``.items``, ``.total`` — sites inferred from request.
    * Tuple: ``(items, total)``.
    * List: ``items`` (total = len).
    """
    items: list[Any] = []
    total: int = 0
    page: int = request.page
    sites_queried: tuple[str, ...] = tuple(request.site_ids or ())
    sites_failed: tuple[str, ...] = ()

    if raw is None:
        return _Outcome((), 0, page, sites_queried, sites_failed)

    if isinstance(raw, (list, tuple)) and not isinstance(raw, _Outcome):
        # Plain collection or (items, total) tuple.
        if len(raw) == 2 and isinstance(raw[1], int):
            items = list(raw[0])
            total = int(raw[1])
        else:
            items = list(raw)
            total = len(items)
        return _Outcome(
            tuple(items), total, page, sites_queried, sites_failed
        )

    # Object path.
    items = list(getattr(raw, "items", []) or [])
    total = int(getattr(raw, "total", len(items)) or len(items))
    page = int(getattr(raw, "page", request.page) or request.page)

    raw_queried = getattr(raw, "sites_queried", None)
    if raw_queried:
        sites_queried = tuple(str(s) for s in raw_queried)

    raw_failed = getattr(raw, "sites_failed", None)
    if raw_failed:
        sites_failed = tuple(str(s) for s in raw_failed)

    return _Outcome(items, total, page, sites_queried, sites_failed)


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] New filters on the search request are added to
#     :class:`~..schemas.requests.SearchRequest`, not bolted onto this
#     router.
# [ ] Any new visibility rule follows the "silently filter, reflect in
#     response" pattern (adult policy on site_ids), not a silent 403
#     leak.
# [ ] New normalization shapes are added to :func:`_normalize_outcome`
#     AND documented in the module docstring.
# [ ] Rate limits are declared in :mod:`..middleware.rate_limit`, never
#     inline here.
# [ ] Error envelopes use :func:`error_response` — never build
#     ``{"error": {...}}`` by hand.
