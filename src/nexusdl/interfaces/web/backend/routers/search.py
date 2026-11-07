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

Optional keyword: the service method may accept ``requested_by`` (the
caller's user id) as a keyword-only argument. The router inspects the
signature and forwards it when present — services that do not declare it
are called with a single positional argument. No ``TypeError`` probing:
signature introspection is authoritative.

If the service accepts an ``include_adult`` keyword, the router forwards
it explicitly (in addition to embedding it in the request copy). If the
service declares neither, only the request copy carries the resolved
flag — but the copy step is only attempted on Pydantic v2 models;
services whose request object does not support ``model_copy`` are called
with the original request unchanged (the policy decision is then the
service's responsibility, as documented in the audit log).

Normalized return shapes accepted by :func:`_normalize_outcome`:

* Object with ``.items``, ``.total``, ``.sites_queried``, ``.sites_failed``
  — the recommended shape.
* Object with ``.items`` and ``.total`` only — ``sites_queried`` is
  inferred from the request, ``sites_failed`` is ``[]``.
* ``tuple(items, total)``, ``tuple(items, total, page)``, and
  ``tuple(items, total, page, sites_queried, sites_failed)`` — all
  common paginated-result conventions are accepted.
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
    except HTTPException:
        # A service may legitimately raise ``HTTPException`` (e.g. its own
        # 404 for an unknown ``site_ids`` entry, its own 403 for a
        # policy check that must surface verbatim). Never mask it as a
        # generic 502 — the client deserves the real status code.
        raise
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

    The router inspects the target's signature (via
    :func:`inspect.signature`) to decide which keyword arguments to
    forward. This is authoritative and version-independent — no
    ``TypeError`` message parsing, no accidental catch of a
    ``TypeError`` raised *inside* the service's own body.

    Forwarded kwargs:

    * ``requested_by`` — only if the callee declares it. Common for
      services that need the caller's identity for audit or per-user
      filtering.
    * ``include_adult`` — only if the callee declares it *and* the
      request copy also carries it. The copy is authoritative; the
      kwarg is a convenience for services that inspect the kwarg
      directly.

    The request copy: the router attempts ``body.model_copy(update=...)``
    to inject the resolved ``include_adult``. This is Pydantic v2 only.
    If the request object is not a Pydantic model (msgspec, dataclass,
    frozen class without ``model_copy``), the original request is
    passed unchanged — the resolved flag is then only forwarded via
    kwarg, if the callee supports it. This is documented explicitly
    (see the module docstring) — the router never silently drops the
    policy decision when the service declares ``include_adult`` as a
    kwarg.
    """
    resolved_request = _with_resolved_adult(body, include_adult)
    kwargs = _introspect_kwargs(
        search_callable,
        include_adult=include_adult,
        requested_by=requested_by,
    )

    result = search_callable(resolved_request, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _with_resolved_adult(
    body: SearchRequest, include_adult: bool
) -> SearchRequest:
    """Return a copy of ``body`` with ``include_adult`` forced to the policy.

    If ``body`` supports ``model_copy`` (Pydantic v2), the copy is
    authoritative. Otherwise the original request is returned
    unchanged — service implementations must then honor the
    ``include_adult`` kwarg (which the router forwards when declared).
    """
    copy_method = getattr(body, "model_copy", None)
    if callable(copy_method):
        try:
            return copy_method(update={"include_adult": include_adult})
        except Exception:
            # A field is missing or the copy method failed unexpectedly —
            # fall through to the plain pass.
            _log.debug(
                "model_copy(update={'include_adult': ...}) failed on "
                "request; passing original to search service"
            )
    return body


def _introspect_kwargs(
    callee: Any,
    *,
    include_adult: bool,
    requested_by: str | None,
) -> dict[str, Any]:
    """Return the kwargs the callee accepts, filtered to what we have.

    Returns an empty dict when introspection is not available (e.g. for
    C extensions or partials without ``__wrapped__``). Never raises —
    introspection failure degrades gracefully to the positional call,
    which is the least common denominator contract.
    """
    try:
        sig = inspect.signature(callee)
    except (TypeError, ValueError):
        return {}

    kwargs: dict[str, Any] = {}
    params = sig.parameters

    # ``**kwargs`` in the callee means any keyword is accepted.
    accepts_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )

    if "requested_by" in params or accepts_var_kw:
        if requested_by is not None:
            kwargs["requested_by"] = requested_by

    if "include_adult" in params or accepts_var_kw:
        kwargs["include_adult"] = include_adult

    return kwargs


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
    * Tuple of length 1–5 — ``(items,)``, ``(items, total)``,
      ``(items, total, page)``,
      ``(items, total, page, sites_queried)``, or
      ``(items, total, page, sites_queried, sites_failed)``.
    * List of items.

    Missing or wrongly-typed fields fall back to safe defaults rather
    than raising — a well-behaved client should never see a 502 because
    a service returned ``total=None``.
    """
    default_sites_queried: tuple[str, ...] = tuple(
        str(s) for s in (request.site_ids or ())
    )
    default_page: int = _coerce_int(
        getattr(request, "page", 1), default=1, minimum=1
    )

    if raw is None:
        return _Outcome((), 0, default_page, default_sites_queried, ())

    # --- List/tuple shapes -------------------------------------------
    if isinstance(raw, (list, tuple)):
        return _outcome_from_sequence(
            raw, default_page, default_sites_queried
        )

    # --- Object shapes -----------------------------------------------
    items = list(getattr(raw, "items", None) or [])
    total = _coerce_int(
        getattr(raw, "total", None),
        default=len(items),
        minimum=0,
    )
    page = _coerce_int(
        getattr(raw, "page", None),
        default=default_page,
        minimum=1,
    )

    sites_queried = _coerce_site_list(
        getattr(raw, "sites_queried", None),
        default=default_sites_queried,
    )
    sites_failed = _coerce_site_list(
        getattr(raw, "sites_failed", None),
        default=(),
    )

    return _Outcome(items, total, page, sites_queried, sites_failed)


def _outcome_from_sequence(
    raw: Sequence[Any],
    default_page: int,
    default_sites_queried: tuple[str, ...],
) -> _Outcome:
    """Normalize the various ``list``/``tuple`` return conventions.

    Recognized shapes:

    * ``(items,)`` — 1-tuple, only items.
    * ``(items, total)`` — 2-tuple, the most common.
    * ``(items, total, page)`` — 3-tuple.
    * ``(items, total, page, sites_queried)`` — 4-tuple.
    * ``(items, total, page, sites_queried, sites_failed)`` — 5-tuple.
    * a plain ``list[items]`` (no total).

    The **discriminator** is ``isinstance(raw[1], int)``: if the second
    element is an ``int``, the sequence is a paginated-result tuple; if
    not, it is a plain list of items.
    """
    if len(raw) == 0:
        return _Outcome((), 0, default_page, default_sites_queried, ())

    # Plain list of items → no total element.
    if len(raw) == 1:
        items = list(raw[0]) if isinstance(raw[0], (list, tuple)) else list(raw)
        return _Outcome(
            items, len(items), default_page, default_sites_queried, ()
        )

    if not (len(raw) >= 2 and isinstance(raw[1], int)):
        # First element is not a paginated tuple — treat the whole
        # sequence as items.
        return _Outcome(
            list(raw), len(raw), default_page, default_sites_queried, ()
        )

    items_raw = raw[0]
    try:
        items = list(items_raw) if items_raw is not None else []
    except TypeError:
        # Non-iterable first element — this is a shape bug; return
        # empty rather than 500.
        items = []

    total = _coerce_int(raw[1], default=len(items), minimum=0)

    page = (
        _coerce_int(raw[2], default=default_page, minimum=1)
        if len(raw) >= 3
        else default_page
    )

    sites_queried = (
        _coerce_site_list(raw[3], default=default_sites_queried)
        if len(raw) >= 4
        else default_sites_queried
    )

    sites_failed = (
        _coerce_site_list(raw[4], default=())
        if len(raw) >= 5
        else ()
    )

    return _Outcome(items, total, page, sites_queried, sites_failed)


def _coerce_int(value: Any, *, default: int, minimum: int) -> int:
    """Coerce ``value`` to ``int`` with a floor, or return ``default``.

    Accepts real ``int`` (and ``bool``, which is a subclass), numeric
    strings, and floats (truncated). Everything else — ``None``, ``""``,
    ``"two"``, complex objects, ``NaN`` — falls back to ``default``.
    Values below ``minimum`` are clamped. This is defensive: a service
    returning ``total=None`` or ``page="2"`` should not surface as a
    502 to the client.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return minimum if not value else max(minimum, 1)
    if isinstance(value, int):
        return max(minimum, value)
    if isinstance(value, float):
        try:
            iv = int(value)
        except (ValueError, OverflowError):
            return default
        return max(minimum, iv)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return max(minimum, int(stripped))
        except ValueError:
            try:
                return max(minimum, int(float(stripped)))
            except (ValueError, OverflowError):
                return default
    return default


def _coerce_site_list(
    value: Any, *, default: tuple[str, ...]
) -> tuple[str, ...]:
    """Coerce ``value`` to a tuple of site-id strings.

    Handles the ``"mangadex"`` vs ``["mangadex"]`` footgun: a bare
    string is wrapped in a 1-tuple. Iterables are consumed into strings.
    Anything else returns ``default``.
    """
    if value is None:
        return default
    if isinstance(value, str):
        s = value.strip()
        return (s,) if s else default
    try:
        return tuple(str(s) for s in value if s is not None)
    except TypeError:
        return default


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
#     AND documented in the module docstring. Every new tuple shape must
#     be covered by a test in ``tests/integration/test_search.py``.
# [ ] Rate limits are declared in :mod:`..middleware.rate_limit`, never
#     inline here.
# [ ] Error envelopes use :func:`error_response` — never build
#     ``{"error": {...}}`` by hand.
