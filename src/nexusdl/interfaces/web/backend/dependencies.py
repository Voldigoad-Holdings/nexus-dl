"""FastAPI dependency layer for the NexusDL web backend.

This is the **only** module in the backend that eagerly imports FastAPI
utilities (``Request``, ``WebSocket``, ``HTTPBearer``, ``HTTPException``,
``Depends``). It is imported exclusively by the routers, which are
themselves FastAPI-only. No headless code path imports this file — the
"headless-safe" rule applies to :mod:`security`, :mod:`websocket`, and
:mod:`middleware`, not here.

What this module provides
=========================

**Service container accessors**
    * :func:`get_services` — the master dependency, returns the
      :class:`AppServices` container stored on ``app.state.services``.
    * :data:`ServicesDep` — ``Annotated[AppServices, Depends(get_services)]``,
      the type every router uses.
    * Granular accessors (:func:`get_settings`, :func:`get_registry`, …)
      for the rare endpoints that need only one service and would
      prefer a narrower signature.
    * :func:`get_auth_service` and :func:`get_connection_manager` accept
      either a ``Request`` or a ``WebSocket`` and are meant to be called
      as plain functions from WebSocket endpoints (which do not support
      ``Depends()`` injection).

**Authentication dependencies**
    * :func:`get_current_payload` — the JWT-bearing user. **Reuses**
      ``scope["state"]["auth_payload"]`` set by
      :class:`~.middleware.auth.AuthMiddleware` to avoid a second
      verification per request. Falls back to standard ``HTTPBearer``
      extraction when the middleware is not installed.
    * :data:`CurrentUser` — ``Annotated[TokenPayload,
      Depends(get_current_payload)]``, the standard "authenticated user"
      type.
    * :func:`get_optional_payload` and :data:`OptionalUser` — same but
      returns ``None`` when no valid token is present. Used by endpoints
      that serve both authenticated and anonymous clients (e.g. search
      that adapts to the caller's adult-content permission).
    * :func:`require_role` — factory returning a dependency enforcing a
      minimum role. :data:`AdminUser` is pre-built for
      :attr:`~UserRole.ADMIN`.

**Request metadata**
    * :func:`get_request_id` and :data:`RequestIdDep` — the request id
      set by :class:`~.middleware.logging.LoggingMiddleware`, or a
      fresh one when the middleware is not installed.

Testing
=======

FastAPI's standard override pattern applies::

    app.dependency_overrides[get_services] = lambda: FakeAppServices()
    app.dependency_overrides[get_current_payload] = lambda: TokenPayload(
        sub="test-user", role=UserRole.USER, jti="t", iat=0, exp=9999999999,
        typ="access",
    )

For tests that want to exercise the real verification path (as
``tests/integration/test_auth_flow.py`` does), simply do **not** override
``get_current_payload`` and provide a real :class:`AuthServiceLike` on
the fake ``AppServices``.

Container contract
==================

:class:`AppServices` is a :class:`typing.Protocol`. Any object with the
required attributes satisfies it structurally. The concrete
implementation is built once in the application's ``lifespan`` and stored
on ``app.state.services``. Adding a new service is one field on the
protocol, plus a wiring line in the composition root — routers that do
not use it are unaffected.

Layering
========

``dependencies.py`` must **not** import from ``gui/`` or from
``interfaces/web/backend/routers/``. It imports from:

* ``fastapi`` / ``fastapi.security`` — allowed (this is the FastAPI layer).
* ``nexusdl.core.models.enums`` — for :class:`UserRole`.
* :mod:`.security` — for :class:`TokenError` hierarchy and
  :class:`TokenPayload`.

Anything else is a layering violation.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Protocol, runtime_checkable

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger as _logger

from nexusdl.core.models.enums import UserRole

from .schemas.responses import ErrorCode, error_response
from .security import (
    TokenError,
    TokenExpiredError,
    TokenInvalidError if False else TokenError,  # keep mypy quiet — see below
    TokenPayload,
    TokenRevokedError,
    TokenTypeMismatchError,
    role_at_least,
)

_log = _logger.bind(component="web.dependencies")


# ===========================================================================
# Service container Protocols
# ===========================================================================
#
# These are structural contracts, not concrete types. The composition root
# (``main.py`` lifespan) is free to use any concrete implementation as long
# as it satisfies the shape. Fields are typed ``Any`` on purpose — the
# routers do their own ``getattr`` / duck-typing on the method names they
# support (e.g. ``search_web`` vs ``search``), so a stricter type would
# only introduce false negatives and force imports from service modules
# that would break the layering.

class AppServices(Protocol):
    """Structural container for every service the web layer needs.

    The concrete implementation is built once in the application's
    ``lifespan`` and stored on ``app.state.services``. Adding a new field
    is a one-line change here (plus the wiring in the composition root);
    no other file is affected.

    A field may be ``None`` (e.g. ``user_service`` on a deployment without
    user management), but the **attribute must exist**. Use
    ``getattr(services, "user_service", None)`` in routers that treat the
    service as optional.
    """

    # --- Required, used by every router that needs a service -----------
    settings: Any
    event_bus: Any
    registry: Any
    downloader: Any
    library: Any
    image_cache: Any
    state_store: Any

    # --- Required for authentication flows -----------------------------
    auth_service: Any

    # --- Optional, checked with getattr() in routers ------------------
    user_service: Any
    # Methods on the container itself (settings schema, search, health)
    # are accessed via ``getattr(services, name, None)`` in routers; not
    # declared here to keep the protocol minimal and forward-compatible.


@runtime_checkable
class AuthServiceLike(Protocol):
    """Structural shape of the security module's :class:`AuthService`.

    Declared here (not imported) so that tests can inject a fake with a
    minimal surface, and so that alternate implementations (a mock for
    integration tests, a JWT-less auth service for dev) satisfy the same
    contract without inheriting from the concrete class.
    """

    async def verify_access(self, token: str) -> TokenPayload: ...


@runtime_checkable
class ConnectionManagerLike(Protocol):
    """Structural shape of :class:`~.websocket.ConnectionManager`.

    Only the methods used by :mod:`routers.ws` are declared. Any object
    with these methods (including the real manager, a test double) works.
    """

    async def connect(
        self,
        websocket: Any,
        auth: Any,
        *,
        subscriptions: Any = ...,
    ) -> Any: ...

    async def disconnect(self, connection: Any) -> None: ...

    async def handle_message(
        self, connection: Any, raw: Any
    ) -> None: ...


# ===========================================================================
# Master dependency — the service container
# ===========================================================================

def get_services(request: Request) -> AppServices:
    """Return the :class:`AppServices` container from ``app.state``.

    Raises:
        HTTPException: 500 if the container is missing — this is always a
            composition-root bug, never a client error. Surfacing it as a
            500 (not a crash) keeps the error envelope uniform.
    """
    services = getattr(request.app.state, "services", None)
    if services is None:
        _log.error(
            "app.state.services is not set — the composition root failed "
            "to initialize the service container."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "The server is not properly configured.",
                details={
                    "hint": "app.state.services was not set during startup.",
                },
            ).model_dump(),
        )
    return services


#: The type every router uses to inject the service container.
ServicesDep = Annotated[AppServices, Depends(get_services)]


# ===========================================================================
# Granular accessors (rarely needed — prefer ServicesDep)
# ===========================================================================
#
# These are here for the rare endpoint that only needs one service and
# would prefer a narrower signature. They are also useful for
# parametrized tests that want to override a single service without
# rebuilding the whole container.

def get_settings(services: ServicesDep) -> Any:
    return services.settings


def get_event_bus(services: ServicesDep) -> Any:
    return services.event_bus


def get_registry(services: ServicesDep) -> Any:
    return services.registry


def get_downloader(services: ServicesDep) -> Any:
    return services.downloader


def get_library(services: ServicesDep) -> Any:
    return services.library


def get_image_cache(services: ServicesDep) -> Any:
    return services.image_cache


def get_state_store(services: ServicesDep) -> Any:
    return services.state_store


def get_user_service(services: ServicesDep) -> Any:
    """Return the user service, raising 500 if not configured."""
    user_service = getattr(services, "user_service", None)
    if user_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "User management is not configured on this server.",
            ).model_dump(),
        )
    return user_service


# ===========================================================================
# WebSocket-oriented accessors (plain functions, not Depends)
# ===========================================================================
#
# WebSocket endpoints do not support ``Depends()`` injection the same way
# HTTP endpoints do. Routers call these directly with the WebSocket
# instance:
#
#     service = get_auth_service(websocket)
#     manager = get_connection_manager(websocket)
#
# The ``connection`` parameter accepts either a ``Request`` or a
# ``WebSocket`` — both expose ``.app``. Do not type it more narrowly, or
# ``routers/ws.py`` will need two variants.

def get_auth_service(connection: Request | WebSocket) -> AuthServiceLike:
    """Return the auth service from ``app.state.services.auth_service``.

    Accepts a ``Request`` or a ``WebSocket``. Raises a 500 if the service
    is not configured — a missing auth service on a public deployment is
    a composition-root bug.
    """
    services = getattr(connection.app.state, "services", None)
    if services is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "The server is not properly configured.",
                details={"hint": "app.state.services is not set."},
            ).model_dump(),
        )
    auth_service = getattr(services, "auth_service", None)
    if auth_service is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Authentication is not configured on this server.",
            ).model_dump(),
        )
    return auth_service


def get_connection_manager(
    connection: Request | WebSocket,
) -> ConnectionManagerLike:
    """Return ``app.state.connection_manager``.

    The connection manager is a lifecycle resource, not a "service" —
    it lives directly on ``app.state`` (managed by ``lifespan``), not
    inside ``app.state.services``. This distinction keeps the service
    container stable while allowing lifecycle-managed resources to
    evolve independently.

    Raises:
        HTTPException: 500 if the manager has not been initialized. This
            indicates that the router is mounted but the WebSocket engine
            was not started — always a configuration bug.
    """
    manager = getattr(connection.app.state, "connection_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "The WebSocket engine is not initialized.",
                details={
                    "hint": "app.state.connection_manager was not set "
                            "during startup.",
                },
            ).model_dump(),
        )
    return manager


def get_bus_bridge(connection: Request | WebSocket) -> Any:
    """Return ``app.state.bus_bridge`` (used by tests and admin tools).

    Optional — many deployments access the bridge only through the
    lifespan. Returns ``None`` rather than raising when absent.
    """
    return getattr(connection.app.state, "bus_bridge", None)


# ===========================================================================
# HTTPBearer scheme
# ===========================================================================
#
# ``auto_error=False`` is essential: the default ``auto_error=True``
# raises ``HTTPException(403)`` when the header is missing, which is both
# the wrong status (401 is correct) and a different response envelope
# than the rest of the API. We extract credentials with auto_error=False
# and raise our own typed errors below.

_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description=(
        "JWT access token. Obtain one via `POST /api/v1/auth/login` and "
        "pass it as `Authorization: Bearer <token>`."
    ),
)


# ===========================================================================
# Authentication dependencies
# ===========================================================================

async def get_current_payload(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
    services: ServicesDep,
) -> TokenPayload:
    """Return the verified token payload for the current request.

    **Reuse first**: if :class:`~.middleware.auth.AuthMiddleware` has
    already populated ``scope["state"]["auth_payload"]``, that payload is
    returned directly — no second JWT verification, no second revocation-
    store hit. This is the common case in production.

    **Fallback**: otherwise, extract the bearer token via
    :class:`HTTPBearer` and verify it against
    :attr:`AppServices.auth_service`. This path is exercised by:

    * unit tests that do not install the middleware,
    * non-standard deployments that call the API without
      ``AuthMiddleware``,
    * integration tests that explicitly disable the middleware to test
      the fallback.

    Raises:
        HTTPException: 401 for any authentication failure, with a
            ``reason`` in ``details`` matching the API contract:
            ``MISSING_TOKEN``, ``TOKEN_EXPIRED``, ``TOKEN_REVOKED``,
            ``TOKEN_INVALID``, ``TOKEN_TYPE_MISMATCH``.
    """
    # 1) Reuse the middleware's work when available.
    cached = _cached_payload(request)
    if cached is not None:
        return cached

    # 2) Fall back to standard extraction + verification.
    if credentials is None or not credentials.credentials:
        raise _unauthorized(
            "Authentication is required for this endpoint.",
            reason="MISSING_TOKEN",
            request=request,
        )

    try:
        return await services.auth_service.verify_access(
            credentials.credentials
        )
    except TokenExpiredError as exc:
        raise _unauthorized(
            "The access token has expired.",
            reason="TOKEN_EXPIRED",
            request=request,
        ) from exc
    except TokenRevokedError as exc:
        raise _unauthorized(
            "The access token has been revoked.",
            reason="TOKEN_REVOKED",
            request=request,
        ) from exc
    except TokenTypeMismatchError as exc:
        raise _unauthorized(
            "Wrong token type — this endpoint requires an access token.",
            reason="TOKEN_TYPE_MISMATCH",
            request=request,
        ) from exc
    except TokenError as exc:
        raise _unauthorized(
            "The access token is invalid.",
            reason="TOKEN_INVALID",
            request=request,
        ) from exc


#: Standard "authenticated user" type.
CurrentUser = Annotated[TokenPayload, Depends(get_current_payload)]


async def get_optional_payload(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
    services: ServicesDep,
) -> TokenPayload | None:
    """Return the token payload, or ``None`` when authentication is absent.

    Same reuse-then-verify logic as :func:`get_current_payload`, but
    **never raises** on authentication failure — it returns ``None``.
    Use this for endpoints that serve both authenticated and anonymous
    clients (public search, manga detail, health status via API, …).

    Do **not** use this on endpoints that require authentication. The
    ``None`` return will quietly turn into a client-side bug when the
    endpoint assumes a payload is present.
    """
    try:
        return await get_current_payload(
            request=request,
            credentials=credentials,
            services=services,
        )
    except HTTPException:
        return None


#: "Authenticated user, or None if anonymous" type.
OptionalUser = Annotated[TokenPayload | None, Depends(get_optional_payload)]


# ===========================================================================
# Role enforcement
# ===========================================================================

def require_role(
    required: UserRole,
) -> Callable[[TokenPayload], Awaitable[TokenPayload]]:
    """Return a dependency enforcing a minimum role.

    The check uses :func:`~.security.role_at_least`, which understands
    the hierarchy ``GUEST < USER < ADMIN``. A user with a *higher* role
    than ``required`` passes.

    Example::

        AdminOnly = Annotated[TokenPayload, Depends(require_role(UserRole.ADMIN))]

        @router.delete("/api/v1/users/{id}")
        async def delete_user(id: str, admin: AdminOnly) -> ...: ...

    Raises:
        HTTPException: 403 with ``reason`` set to ``INSUFFICIENT_ROLE``
            and the required role documented in ``details``.
    """
    async def _checker(payload: CurrentUser) -> TokenPayload:
        if not role_at_least(payload.role, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_response(
                    ErrorCode.FORBIDDEN,
                    f"This endpoint requires {required.value!r} or higher.",
                    details={
                        "reason": "INSUFFICIENT_ROLE",
                        "required_role": required.value,
                        "actual_role": payload.role.value,
                    },
                ).model_dump(),
            )
        return payload

    return _checker


#: Pre-built "must be admin" dependency.
AdminUser = Annotated[TokenPayload, Depends(require_role(UserRole.ADMIN))]


# ===========================================================================
# Request metadata
# ===========================================================================

def get_request_id(request: Request) -> str | None:
    """Return the request id set by ``LoggingMiddleware``, if available.

    Returns ``None`` when the middleware is not installed — routers that
    want a strict guarantee should generate one themselves via
    :mod:`uuid` and use it in their response envelope.
    """
    state = request.scope.get("state") or {}
    if isinstance(state, dict):
        rid = state.get("request_id")
        if isinstance(rid, str) and rid:
            return rid
    return None


#: Request id dependency alias.
RequestIdDep = Annotated[str | None, Depends(get_request_id)]


# ===========================================================================
# Helpers
# ===========================================================================

def _cached_payload(request: Request) -> TokenPayload | None:
    """Return the payload cached by :class:`~.middleware.auth.AuthMiddleware`.

    The middleware writes ``scope["state"]["auth_payload"]`` after a
    successful verification. Reading it here avoids a second JWT
    verification and a second revocation-store round-trip per request —
    a measurable win on high-traffic endpoints.
    """
    state = request.scope.get("state") or {}
    if not isinstance(state, dict):
        return None
    payload = state.get("auth_payload")
    if isinstance(payload, TokenPayload):
        return payload
    return None


def _unauthorized(
    message: str,
    *,
    reason: str,
    request: Request | None = None,
) -> HTTPException:
    """Build a 401 with the standard error envelope and RFC 6750 header."""
    request_id = (
        get_request_id(request) if request is not None else None
    )
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=error_response(
            ErrorCode.UNAUTHORIZED,
            message,
            details={"reason": reason},
            request_id=request_id,
        ).model_dump(),
        headers={
            "WWW-Authenticate": f'Bearer error="{_oauth_error(reason)}"',
            "X-Auth-Reason": reason,
        },
    )


def _oauth_error(reason: str) -> str:
    """Map our internal reason to an RFC 6750 ``error`` value.

    The ``WWW-Authenticate`` header is what browsers and API clients
    read to understand the failure. Values per RFC 6750:

    * ``invalid_request`` — the request lacked a required token.
    * ``invalid_token``   — the token was malformed, expired, or revoked.
    * ``insufficient_scope`` — the token lacks the required permissions
      (used here only by :func:`require_role`, which builds its own
      header — this function is for the 401 paths).
    """
    return (
        "invalid_request" if reason == "MISSING_TOKEN" else "invalid_token"
    )


# ===========================================================================
# Public surface
# ===========================================================================

__all__ = [
    # --- Service Protocols ---
    "AppServices",
    "AuthServiceLike",
    "ConnectionManagerLike",
    # --- Master dependency ---
    "get_services",
    "ServicesDep",
    # --- Granular accessors ---
    "get_settings",
    "get_event_bus",
    "get_registry",
    "get_downloader",
    "get_library",
    "get_image_cache",
    "get_state_store",
    "get_user_service",
    # --- WebSocket accessors (plain calls) ---
    "get_auth_service",
    "get_connection_manager",
    "get_bus_bridge",
    # --- Authentication dependencies ---
    "get_current_payload",
    "get_optional_payload",
    "get_request_id",
    "require_role",
    # --- Type aliases ---
    "CurrentUser",
    "OptionalUser",
    "AdminUser",
    "RequestIdDep",
]
