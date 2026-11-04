"""Sites router — ``/api/v1/sites``.

Thin FastAPI glue between HTTP and :class:`~nexusdl.core.registry.site_registry.SiteRegistry`.
Responsibilities:

* Enforce the server's adult-content **policy**
  (``Settings.content.allow_adult``). This is a policy, not a preference:
  a client cannot force ``include_adult=true`` when the server forbids it.
* Expose an opt-out query parameter so a client can filter adult sites
  out per request even when the server allows them.
* Hide **disabled** sites from non-admin users; admins can see them via
  ``?include_disabled=true`` to re-enable them from the settings UI.
* Return 404 — not 403 — for sites the caller is not allowed to see, to
  avoid leaking the existence of adult sites to unauthorized users.

No business logic runs here. Site list, filtering and health live in the
service layer. This module only maps domain objects to
:mod:`..schemas.responses` schemas and translates policy violations into
the standard error envelope.

Promotion note
==============

:class:`SiteListQueryParams` lives here for now because it is specific
to this resource and ``schemas/common.py`` is already delivered. If a
second router needs the same filters, promote it to ``common.py`` —
three lines, no other file changes.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from nexusdl.core.models.enums import Language, UserRole

from ..dependencies import CurrentUser, ServicesDep
from ..schemas.common import QueryParams
from ..schemas.responses import (
    ErrorCode,
    SiteHealthResponse,
    SiteHealthSchema,
    SiteListResponse,
    SiteSchema,
    error_response,
)
from ..security import role_at_least

router = APIRouter(prefix="/api/v1/sites", tags=["sites"])


# ===========================================================================
# Query parameter schema (resource-specific)
# ===========================================================================

class SiteListQueryParams(QueryParams):
    """Query parameters for ``GET /api/v1/sites``.

    * ``language`` — filter to sites whose source language matches.
    * ``include_adult`` — per-request opt-in / opt-out:
        - ``None`` (default): use the server policy (``allow_adult``).
        - ``false``: filter adult sites out, even if the server allows them.
        - ``true``: opt in — only honored when the server policy allows it,
          otherwise the request is rejected with 403 (see the router).
    * ``include_disabled`` — admin-only. Non-admin callers get the normal
      (enabled) list even if they pass ``true``. Admin callers can see
      disabled sites to re-enable them from settings.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    language: Language | None = Field(
        default=None,
        description="Filter by source language.",
    )
    include_adult: bool | None = Field(
        default=None,
        description="``null`` = server policy. ``false`` = hide adult "
                    "sites. ``true`` = include adult sites (only honored "
                    "if the server policy permits it).",
    )
    include_disabled: bool = Field(
        default=False,
        description="Admin only. When ``true`` and the caller is an admin, "
                    "disabled sites are included.",
    )


# ===========================================================================
# Endpoints
# ===========================================================================

@router.get(
    "",
    response_model=SiteListResponse,
    summary="List sites",
    description=(
        "Return every site the caller is allowed to see. Disabled sites "
        "are hidden from non-admin callers. Adult sites are included only "
        "when both the server policy and the caller's request allow it."
    ),
)
async def list_sites(
    filters: Annotated[SiteListQueryParams, Depends()],
    payload: CurrentUser,
    services: ServicesDep,
) -> SiteListResponse:
    # --- 1. Resolve adult policy -------------------------------------
    policy_allows_adult = _policy_allows_adult(services)
    include_adult = _resolve_include_adult(
        requested=filters.include_adult,
        policy_allows=policy_allows_adult,
    )

    # --- 2. Resolve disabled visibility ------------------------------
    is_admin = role_at_least(payload.role, UserRole.ADMIN)
    include_disabled = bool(filters.include_disabled and is_admin)

    # --- 3. Delegate to the registry ---------------------------------
    sites = await services.registry.list_sites(
        include_adult=include_adult,
        include_disabled=include_disabled,
        language=filters.language,
    )

    return SiteListResponse(
        items=[SiteSchema.model_validate(s) for s in sites],
        total=len(sites),
    )


@router.get(
    "/{site_id}",
    response_model=SiteSchema,
    summary="Get a single site",
    description=(
        "Return one site's configuration. Returns 404 whether the site "
        "does not exist or is not visible to the caller (adult policy or "
        "disabled), to avoid leaking existence."
    ),
)
async def get_site(
    site_id: str,
    payload: CurrentUser,
    services: ServicesDep,
) -> SiteSchema:
    site = await _get_visible_site(
        site_id=site_id,
        payload=payload,
        services=services,
    )
    return SiteSchema.model_validate(site)


@router.get(
    "/{site_id}/health",
    response_model=SiteHealthResponse,
    summary="Get a site's runtime health",
    description=(
        "Return the site's current runtime status (healthy / degraded / "
        "down / unknown), last check timestamp, and 24-hour error count. "
        "Returns 404 for sites not visible to the caller."
    ),
)
async def get_site_health(
    site_id: str,
    payload: CurrentUser,
    services: ServicesDep,
) -> SiteHealthResponse:
    # Authorization first: a site you cannot see has no health to show.
    site = await _get_visible_site(
        site_id=site_id,
        payload=payload,
        services=services,
    )

    info = await services.registry.get_health(site.id)
    if info is None:
        # Site exists but has never been probed. Return a neutral
        # "unknown" health rather than 404 — the site is real, it just
        # has no data yet.
        return SiteHealthResponse(
            health=SiteHealthSchema(site_id=site.id),
        )

    return SiteHealthResponse(
        health=SiteHealthSchema.model_validate(info),
    )


# ===========================================================================
# Helpers
# ===========================================================================

def _policy_allows_adult(services: ServicesDep) -> bool:
    """Return the server's adult-content policy.

    Never raises. If the settings object is malformed, defaults to
    "no adult" — the safe choice.
    """
    try:
        return bool(services.settings.content.allow_adult)
    except Exception:
        return False


def _resolve_include_adult(
    *, requested: bool | None, policy_allows: bool
) -> bool:
    """Compute the effective ``include_adult`` flag.

    Rules:

    * ``requested is None`` → use policy.
    * ``requested is False`` → filter adult sites (always safe).
    * ``requested is True`` + ``policy_allows`` → include them.
    * ``requested is True`` + ``not policy_allows`` → **403** — an
      explicit attempt to bypass policy. Rejecting loudly (rather than
      silently overriding to ``False``) surfaces misconfigured clients
      and prevents accidental UI bugs from hiding themselves.
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


async def _get_visible_site(
    *,
    site_id: str,
    payload: CurrentUser,
    services: ServicesDep,
) -> object:
    """Return the site if the caller is allowed to see it, else 404.

    Combines three visibility rules:

    * The site must exist.
    * Adult sites are visible only when the server policy allows it.
    * Disabled sites are visible only to admins.

    A single 404 is raised for **all** rejection reasons — the caller
    cannot distinguish "does not exist" from "not authorized", which
    prevents enumeration of adult or disabled sites.
    """
    site = await services.registry.get_site(site_id)
    if site is None:
        _raise_not_found(site_id)

    # Adult check.
    is_adult = bool(getattr(site, "adult", False))
    policy_allows_adult = _policy_allows_adult(services)
    if is_adult and not policy_allows_adult:
        _raise_not_found(site_id)

    # Disabled check — admins bypass.
    is_disabled = bool(getattr(site, "disabled", False))
    if is_disabled:
        is_admin = role_at_least(payload.role, UserRole.ADMIN)
        if not is_admin:
            _raise_not_found(site_id)

    return site


def _raise_not_found(site_id: str) -> None:
    """Raise the standard 404 for a hidden or missing site."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=error_response(
            ErrorCode.NOT_FOUND,
            f"Site {site_id!r} not found.",
        ).model_dump(),
    )


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] New query params live on :class:`SiteListQueryParams` (or a new
#     local ``*QueryParams``) — do not add them to ``common.py`` unless
#     a second router needs them.
# [ ] Any new visibility rule follows the "one 404 for all refusals"
#     pattern in :func:`_get_visible_site` (avoid leaking existence).
# [ ] New fields returned by the registry are added to
#     :class:`~..schemas.responses.SiteSchema` — not serialized ad hoc.
# [ ] Any new admin-only feature is gated by
#     :func:`~..security.role_at_least` with ``UserRole.ADMIN``.
