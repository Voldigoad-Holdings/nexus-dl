"""Auth router — ``/api/v1/auth/{login,register,refresh,logout,me}``.

Thin FastAPI glue for authentication. All crypto (JWT, password hashing,
token rotation, replay detection) lives in :mod:`..security`. All user
management (create, lookup, uniqueness) is delegated to an injected
``user_service`` on :class:`~..dependencies.ServicesDep`. This router:

* validates and forwards requests,
* maps typed errors to the standard error envelope,
* **never** logs passwords, tokens, or hashes,
* **never** distinguishes "user does not exist" from "wrong password"
  in a login failure — a uniform 401 with ``reason: INVALID_CREDENTIALS``
  is the only signal a client gets (and the response time is
  deliberately normalized — see :func:`_authenticate`).

Endpoints
=========

* ``POST /api/v1/auth/login``    → :class:`~..schemas.responses.LoginResponse`
* ``POST /api/v1/auth/register`` → :class:`~..schemas.responses.LoginResponse` (201)
* ``POST /api/v1/auth/refresh``  → :class:`~..schemas.responses.RefreshResponse`
* ``POST /api/v1/auth/logout``   → :class:`~..schemas.responses.MessageResponse`
* ``GET  /api/v1/auth/me``       → :class:`~..schemas.responses.UserInfo`

The last two are not in the strict v1 contract but are required for a
complete client session lifecycle (verify session on boot; end it
cleanly). They are additive.

Rate limiting
=============

Per the API contract, ``/auth/login`` must be rate-limited to **5/min
per IP**. This is enforced by :mod:`..middleware.rate_limit`, **not** by
this router. Removing that middleware removes the protection. The same
middleware should protect ``/auth/register`` at a similar rate.

CSRF
====

Authentication is **bearer-only** (``Authorization: Bearer <jwt>``).
CSRF protection is therefore not applicable. If cookie-based sessions
are added in the future, a CSRF middleware must be introduced —
JavaScript ``fetch`` with credentials + cookie auth **is** CSRF-able.

Service contracts (duck-typed)
==============================

Every method below is looked up by multiple names, in order. Any method
may be sync or async (``inspect.isawaitable`` handles both).

**Authentication** (login)::

    # Shape 1 — dedicated authenticate method (may not exist)
    user = await services.auth_service.authenticate(identifier, password)
    user = await services.user_service.authenticate(identifier, password)
    # Returns a user object or ``None``. May raise ``InvalidCredentialsError``.

    # Shape 2 — manual composition (the production path today)
    user = await services.user_service.get_by_identifier(identifier)
    # or: get_by_username / find_by_username / get_by_email / find_by_email
    ok   = services.auth_service.verify_password(password, user.password_hash)

    # In shape 2, if the user is not found, the router still performs a
    # dummy bcrypt verification so that the two branches take the same
    # time. This is the ONLY defense against account enumeration via
    # timing — do not remove it.

**Registration**::

    user = await services.user_service.register(
        username=..., email=..., password=...,
    )
    # or: create_user / create — same signature
    # May raise ``PasswordPolicyError``, ``ValueError("username taken")``, etc.

**Lookup by id** (for ``/me``)::

    user = await services.user_service.get_user(user_id)
    # or: get_by_id / find_by_id / get

**Session-wide revocation** (for ``logout?all_sessions=true``)::

    await services.auth_service.revoke_all_for_user(user_id)
    # or: services.user_service.revoke_all_sessions(user_id)
    # If neither exists, logout returns 200 with
    # ``details.revoked_all_sessions=false``.

Reason vocabulary
=================

Every 401 carries ``details.reason`` — the client should check it for
fine-grained handling:

* ``INVALID_CREDENTIALS`` — wrong username or password → prompt login
* ``TOKEN_EXPIRED``       — JWT expired → attempt refresh
* ``TOKEN_REVOKED``       — session terminated → force re-login
* ``TOKEN_INVALID``       — malformed/wrong signature → force re-login
* ``TOKEN_TYPE_MISMATCH`` — access used as refresh (or vice versa) → bug
* ``SESSION_STALE``       — token valid but user was deleted → force re-login
* ``TOKEN_OWNERSHIP_MISMATCH`` — logout tried to revoke another user's
  refresh-token family → client bug or attack
"""
from __future__ import annotations

import hmac
import inspect
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger as _logger

from nexusdl.core.models.enums import UserRole

from ..dependencies import CurrentUser, ServicesDep
from ..schemas.requests import (
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterRequest,
)
from ..schemas.responses import (
    ErrorCode,
    LoginResponse,
    MessageResponse,
    RefreshResponse,
    TokenResponse,
    UserInfo,
    error_response,
)
from ..security import (
    InvalidCredentialsError,
    InvalidTokenError,
    PasswordPolicyError,
    SecurityError,
    TokenError,
    TokenExpiredError,
    TokenRevokedError,
    TokenTypeMismatchError,
)

_log = _logger.bind(component="web.routers.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


#: Pre-computed bcrypt hash used as a "dummy" verification target when a
#: user is not found on the manual login path. The purpose is to spend
#: the same wall-clock time (roughly one bcrypt cost-12 verification,
#: ~100 ms) as a real password check, so an attacker cannot distinguish
#: "user does not exist" from "wrong password" by timing.
#:
#: The value is a fixed, well-known hash — it is not a secret. It must
#: match the hasher's configured cost so the timing is comparable. If the
#: deployment changes ``bcrypt__rounds`` (see ``PasswordHasher`` in
#: ``security.py``), this constant should be regenerated with the same
#: cost. A mismatch only degrades the protection; it never breaks
#: authentication.
_DUMMY_PASSWORD_HASH: str = (
    # "$2b$12$" + 22 chars of salt + 31 chars of hash.
    "$2b$12$C6UzMDM.H6dfI/f/IKcEeO3Y7Y8B2pV/2nJ7x3I5eYtQz9Vv8p1uq"
)


# ===========================================================================
# Login
# ===========================================================================


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Authenticate and obtain a token pair",
    description=(
        "Exchange a username (or email) and password for an access token "
        "and a refresh token.\n\n"
        "On failure the response is **uniformly 401** — the server does "
        "not distinguish 'user does not exist' from 'wrong password' to "
        "prevent account enumeration. The **response time is also "
        "normalized** (a dummy bcrypt verification runs when the user "
        "does not exist) so the distinction is not observable via timing "
        "either. Check `error.details.reason` for `INVALID_CREDENTIALS`.\n\n"
        "**Rate limited**: 5 attempts/min per IP (enforced by middleware)."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Invalid credentials.",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Login rate limit exceeded (middleware).",
        },
    },
)
async def login(
    body: LoginRequest,
    services: ServicesDep,
) -> LoginResponse:
    user = await _authenticate(
        services, identifier=body.username, password=body.password
    )

    if user is None:
        _log.info(
            "login failed (identifier length={})", len(body.username)
        )
        raise _unauthorized(
            "Invalid username or password.",
            reason="INVALID_CREDENTIALS",
        )

    pair = await _issue_token_pair(services, user)
    _log.info(
        "login success user_id={} role={}",
        _user_id(user),
        _user_role(user).value,
    )

    return LoginResponse(
        user=_to_user_info(user),
        token=_to_token_response(pair),
    )


# ===========================================================================
# Register
# ===========================================================================


@router.post(
    "/register",
    response_model=LoginResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and obtain a token pair",
    description=(
        "Create a new account and immediately return a token pair (auto-"
        "login).\n\n"
        "The password **policy** (min length, character classes, banned "
        "substrings) is enforced server-side. A violation returns 422 "
        "with the specific violations listed in `error.details.violations`.\n\n"
        "A duplicate username or email returns 409. The response body "
        "does not distinguish which one collided unless the underlying "
        "user store chose to include it in `error.message`."
    ),
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Username or email already taken.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Password policy violation.",
        },
    },
)
async def register(
    body: RegisterRequest,
    services: ServicesDep,
) -> LoginResponse:
    try:
        user = await _register_user(
            services,
            username=body.username,
            email=body.email,
            password=body.password,
        )
    except Exception as exc:
        raise _translate_auth_error(exc, context="register") from exc

    pair = await _issue_token_pair(services, user)
    _log.info("register success user_id={}", _user_id(user))

    return LoginResponse(
        user=_to_user_info(user),
        token=_to_token_response(pair),
    )


# ===========================================================================
# Refresh
# ===========================================================================


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="Rotate a refresh token into a new pair",
    description=(
        "Exchange a valid refresh token for a fresh access+refresh pair. "
        "The old refresh token is invalidated as part of the rotation.\n\n"
        "**Replay detection**: presenting an already-rotated refresh "
        "token revokes the entire token family. All sessions derived from "
        "the same original login will be logged out — the standard "
        "OAuth 2.1 protection against token theft.\n\n"
        "On failure, check `error.details.reason`:\n"
        "* `TOKEN_EXPIRED` — the token is past its natural `exp` → force re-login.\n"
        "* `TOKEN_REVOKED` — the family was revoked (logout / replay) → force re-login.\n"
        "* `TOKEN_INVALID` — malformed / wrong signature → force re-login."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Refresh token is invalid, expired, or revoked.",
        },
    },
)
async def refresh(
    body: RefreshTokenRequest,
    services: ServicesDep,
) -> RefreshResponse:
    try:
        pair = await _refresh_token_pair(services, body.refresh_token)
    except Exception as exc:
        raise _translate_auth_error(exc, context="refresh") from exc

    _log.debug("refresh success")
    return RefreshResponse(token=_to_token_response(pair))


# ===========================================================================
# Logout
# ===========================================================================


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Revoke the current session (or all sessions)",
    description=(
        "End the current session or every session for the caller.\n\n"
        "* `refresh_token=<token>`: revoke **that token's family** — all "
        "sessions derived from the same original login. **The token must "
        "belong to the calling user** — a token from another user returns "
        "`error.details.reason: TOKEN_OWNERSHIP_MISMATCH` (a 403), not a "
        "silent revocation.\n"
        "* `all_sessions=true`: revoke **every active session** for the "
        "calling user. Requires the service layer to expose "
        "`revoke_all_for_user`; otherwise the response reports "
        "`revoked_all_sessions: false` so the client can warn.\n"
        "* Neither: no-op.\n\n"
        "**Idempotent** — always returns 200 for the normal paths, even "
        "if the token was already revoked or never existed. Logout must "
        "never fail the client."
    ),
)
async def logout(
    body: LogoutRequest,
    payload: CurrentUser,
    services: ServicesDep,
) -> MessageResponse:
    user_id = _require_user_id(payload)
    details: dict[str, Any] = {
        "all_sessions_requested": body.all_sessions,
        "refresh_token_provided": bool(body.refresh_token),
    }

    # 1) all_sessions — best-effort.
    if body.all_sessions:
        revoked_all = await _revoke_all_for_user(services, user_id)
        details["revoked_all_sessions"] = revoked_all
        if not revoked_all:
            _log.warning(
                "logout all_sessions requested but not supported "
                "user_id={}",
                user_id,
            )
        _log.info(
            "logout user_id={} all_sessions=true revoked_all={}",
            user_id,
            revoked_all,
        )
        return MessageResponse(
            message=(
                "All sessions terminated."
                if revoked_all
                else "Session marked for revocation; global revocation "
                     "is not configured on this server."
            ),
            details=details,
        )

    # 2) Family revocation via refresh_token — best-effort, with
    #    ownership enforcement.
    if body.refresh_token:
        family, owner = await _extract_refresh_family_and_owner(
            services, body.refresh_token
        )
        if family is None:
            # Malformed / expired / already revoked — nothing to do.
            details["revoked_family"] = False
            _log.info(
                "logout user_id={} refresh_family_revoked=False (reason=unparsable)",
                user_id,
            )
            return MessageResponse(
                message="Refresh token family revoked.",
                details=details,
            )

        # Ownership check: the caller can only revoke their own family.
        if owner is not None and owner != user_id:
            _log.warning(
                "logout: refresh_token belongs to user {!r} but caller "
                "is {!r} (attempted cross-user family revocation)",
                owner,
                user_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_response(
                    ErrorCode.FORBIDDEN,
                    "This refresh token does not belong to the caller.",
                    details={
                        "reason": "TOKEN_OWNERSHIP_MISMATCH",
                    },
                ).model_dump(),
            )

        try:
            await _revoke_family(services, family)
            details["revoked_family"] = True
        except Exception:
            _log.debug(
                "family revoke raised during logout for user_id={}",
                user_id,
            )
            details["revoked_family"] = False
        _log.info(
            "logout user_id={} refresh_family_revoked={}",
            user_id,
            details.get("revoked_family"),
        )
        return MessageResponse(
            message="Refresh token family revoked.",
            details=details,
        )

    # 3) Neither — no-op.
    _log.debug("logout no-op user_id={}", user_id)
    return MessageResponse(
        message="No session to revoke.",
        details=details,
    )


# ===========================================================================
# Me
# ===========================================================================


@router.get(
    "/me",
    response_model=UserInfo,
    summary="Return the authenticated user's profile",
    description=(
        "Return the profile of the currently authenticated user.\n\n"
        "Requires a valid access token. Returns 401 with "
        "`details.reason: SESSION_STALE` if the user no longer exists "
        "(deleted after the token was issued). The client should force "
        "re-login in that case."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated, or user no longer exists.",
        },
    },
)
async def me(
    payload: CurrentUser,
    services: ServicesDep,
) -> UserInfo:
    user_id = _require_user_id(payload)
    user = await _get_user_by_id(services, user_id)
    if user is None:
        _log.info(
            "me: user_id={} present in token but not in store", user_id
        )
        raise _unauthorized(
            "Your session refers to a user that no longer exists.",
            reason="SESSION_STALE",
        )
    return _to_user_info(user)


# ===========================================================================
# Authentication flow (multi-shape)
# ===========================================================================


async def _authenticate(
    services: ServicesDep, *, identifier: str, password: str
) -> Any | None:
    """Authenticate a user. Returns the user or ``None``.

    Tries in order:

    1. ``auth_service.authenticate(identifier, password)``
    2. ``user_service.authenticate(identifier, password)``
    3. Manual: lookup by identifier + ``auth_service.verify_password``

    **Errors**:

    * Returns ``None`` on any authentication failure (wrong password,
      unknown user, or ``InvalidCredentialsError`` raised by the
      underlying service).
    * Raises on infrastructure failure (DB error, no auth service
      configured) so the caller can return 500 rather than 401.

    **Timing hardening (manual path).** When the user does not exist,
    the router still performs a **dummy bcrypt verification** against
    :data:`_DUMMY_PASSWORD_HASH`. Without this, an attacker can
    distinguish "user does not exist" (fast: 5ms) from "user exists but
    wrong password" (slow: ~100ms bcrypt) purely by measuring response
    time — defeating the whole point of the uniform 401 response. The
    dummy check throws away the result; it only exists to burn the same
    CPU cost as a real verification.
    """
    for svc_attr in ("auth_service", "user_service"):
        svc = getattr(services, svc_attr, None)
        if svc is None:
            continue
        method = getattr(svc, "authenticate", None)
        if not callable(method):
            continue
        try:
            result = method(identifier, password)
            if inspect.isawaitable(result):
                result = await result
            return result
        except InvalidCredentialsError:
            return None
        # Any other exception propagates — infrastructure error.

    # Manual fallback (the production path with the current AuthService).
    auth_service = getattr(services, "auth_service", None)
    if auth_service is None:
        raise RuntimeError(
            "No authentication service is configured "
            "(need `services.auth_service` or `services.user_service`)."
        )
    verify = getattr(auth_service, "verify_password", None)
    if not callable(verify):
        raise RuntimeError(
            "auth_service.verify_password is not available."
        )

    user = await _find_user_for_login(services, identifier)
    if user is None:
        # Timing hardening: spend the same wall-clock time as a real
        # verification would. Never leak the result.
        await _dummy_verify_password(verify, password)
        return None

    hashed = _password_hash(user)
    if not hashed:
        # User exists but has no password hash — cannot authenticate.
        # Spend the dummy cost anyway so an attacker cannot distinguish
        # this branch from "user exists with wrong password".
        await _dummy_verify_password(verify, password)
        return None

    try:
        ok = verify(password, hashed)
        if inspect.isawaitable(ok):
            ok = await ok
    except Exception:
        # A malformed hash (corruption) raises inside ``verify_password``.
        # Do not surface the raw exception — log it and treat as
        # "credentials invalid" so the client gets the uniform 401.
        # ``_log.exception`` is used (not ``_log.info``) because the
        # server-side operator must see the corrupted hash.
        _log.exception("verify_password raised on user_id={}", _user_id(user))
        return None
    return user if ok else None


async def _dummy_verify_password(
    verify: Any, password: str
) -> None:
    """Run a bcrypt verification against a fixed dummy hash.

    The result is discarded. Exceptions (a broken hasher, missing
    bcrypt) are logged at ``debug`` and swallowed — this path is a
    pure timing mitigation, it must never fail the request.
    """
    try:
        result = verify(password, _DUMMY_PASSWORD_HASH)
        if inspect.isawaitable(result):
            await result
    except Exception:
        _log.debug("dummy password verification raised (ignored)")


async def _find_user_for_login(
    services: ServicesDep, identifier: str
) -> Any | None:
    """Look up a user by username or email."""
    user_service = getattr(services, "user_service", None)
    if user_service is None:
        return None

    # 1) Single method that accepts any identifier.
    for name in ("get_by_identifier", "find_by_identifier", "find_user"):
        method = getattr(user_service, name, None)
        if not callable(method):
            continue
        try:
            result = method(identifier)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return result
        except Exception:
            _log.debug("user_service.{} raised", name)

    # 2) Try username first.
    for name in ("get_by_username", "find_by_username"):
        method = getattr(user_service, name, None)
        if not callable(method):
            continue
        try:
            result = method(identifier)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return result
        except Exception:
            pass

    # 3) Fall back to email if the identifier looks like one.
    if "@" in identifier:
        for name in ("get_by_email", "find_by_email"):
            method = getattr(user_service, name, None)
            if not callable(method):
                continue
            try:
                result = method(identifier.lower())
                if inspect.isawaitable(result):
                    result = await result
                if result is not None:
                    return result
            except Exception:
                pass
    return None


# ===========================================================================
# Registration flow (multi-shape)
# ===========================================================================


async def _register_user(
    services: ServicesDep,
    *,
    username: str,
    email: str | None,
    password: str,
) -> Any:
    """Create a new user. Raises on policy / uniqueness / infra failure."""
    user_service = getattr(services, "user_service", None)
    if user_service is None:
        raise RuntimeError(
            "No user service is configured "
            "(need `services.user_service`)."
        )

    for name in ("register", "create_user", "create"):
        method = getattr(user_service, name, None)
        if not callable(method):
            continue
        result = method(
            username=username, email=email, password=password
        )
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            # A `None` return is a contract violation — the router
            # cannot issue tokens for a user it does not have. This
            # must be a 500, not a silent success: otherwise the
            # client believes registration worked while the store
            # has no matching user record.
            raise RuntimeError(
                f"user_service.{name} returned None (expected a user). "
                "The user store may have accepted the request but the "
                "created user was not returned — this is a "
                "composition-root bug."
            )
        return result

    raise RuntimeError(
        "user_service has no register/create_user method."
    )


# ===========================================================================
# Token issuance / rotation
# ===========================================================================


async def _issue_token_pair(services: ServicesDep, user: Any) -> Any:
    """Issue a fresh token pair for ``user``."""
    auth_service = getattr(services, "auth_service", None)
    if auth_service is None:
        raise RuntimeError("auth_service is not configured")
    method = getattr(auth_service, "issue_pair", None)
    if not callable(method):
        raise RuntimeError("auth_service.issue_pair is not available")
    pair = method(
        user_id=_user_id(user),
        role=_user_role(user),
        username=_user_name(user),
    )
    if inspect.isawaitable(pair):
        pair = await pair
    return pair


async def _refresh_token_pair(
    services: ServicesDep, refresh_token: str
) -> Any:
    """Rotate a refresh token via ``auth_service.refresh``.

    Uses ``auth_service.refresh`` (not ``verify_refresh``) so that the
    OAuth 2.1 replay detection path is exercised — the security module
    handles family revocation when a revoked token is presented.
    """
    auth_service = getattr(services, "auth_service", None)
    if auth_service is None:
        raise RuntimeError("auth_service is not configured")
    method = getattr(auth_service, "refresh", None)
    if not callable(method):
        raise RuntimeError("auth_service.refresh is not available")
    pair = method(refresh_token)
    if inspect.isawaitable(pair):
        pair = await pair
    return pair


# ===========================================================================
# Revocation helpers
# ===========================================================================


async def _revoke_all_for_user(
    services: ServicesDep, user_id: str
) -> bool:
    """Revoke every session for ``user_id``. Returns success flag.

    Tries ``auth_service.revoke_all_for_user`` then
    ``user_service.revoke_all_sessions``. Returns ``False`` (not an
    exception) when neither exists, so the caller can respond 200 with an
    honest ``revoked_all_sessions: false``.
    """
    for svc_attr in ("auth_service", "user_service"):
        svc = getattr(services, svc_attr, None)
        if svc is None:
            continue
        for name in ("revoke_all_for_user", "revoke_all_sessions"):
            method = getattr(svc, name, None)
            if not callable(method):
                continue
            try:
                result = method(user_id)
                if inspect.isawaitable(result):
                    await result
                return True
            except Exception:
                _log.exception(
                    "services.{}.{} raised for user_id={}",
                    svc_attr,
                    name,
                    user_id,
                )
                return False
    return False


async def _extract_refresh_family_and_owner(
    services: ServicesDep, refresh_token: str
) -> tuple[str | None, str | None]:
    """Return ``(family_id, owner_user_id)`` for ``refresh_token``.

    Best-effort: a malformed or already-revoked token yields
    ``(None, None)`` (logout must not fail on this). The owner is
    read from the token's ``sub`` claim — used by :func:`logout` to
    refuse cross-user family revocation.
    """
    auth_service = getattr(services, "auth_service", None)
    if auth_service is None:
        return None, None
    verify = getattr(auth_service, "verify_refresh", None)
    if not callable(verify):
        return None, None
    try:
        payload = verify(refresh_token)
        if inspect.isawaitable(payload):
            payload = await payload
    except Exception:
        return None, None
    family = getattr(payload, "family", None)
    owner = getattr(payload, "sub", None)
    return (
        str(family) if family else None,
        str(owner) if owner else None,
    )


async def _revoke_family(services: ServicesDep, family: str) -> None:
    """Revoke ``family`` on the auth service. Raises on failure."""
    auth_service = getattr(services, "auth_service", None)
    if auth_service is None:
        raise RuntimeError("auth_service is not configured")
    method = getattr(auth_service, "revoke_family", None)
    if not callable(method):
        raise RuntimeError("auth_service.revoke_family is not available")
    result = method(family)
    if inspect.isawaitable(result):
        await result


# ===========================================================================
# User lookup (for ``/me``)
# ===========================================================================


async def _get_user_by_id(
    services: ServicesDep, user_id: str
) -> Any | None:
    """Fetch a user by id, tolerant to method name."""
    user_service = getattr(services, "user_service", None)
    if user_service is None:
        return None
    for name in ("get_user", "get_by_id", "find_by_id", "get"):
        method = getattr(user_service, name, None)
        if not callable(method):
            continue
        try:
            result = method(user_id)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return result
        except Exception:
            _log.debug("user_service.{} raised for {}", name, user_id)
    return None


# ===========================================================================
# Projections
# ===========================================================================


def _to_user_info(user: Any) -> UserInfo:
    """Project a domain user into :class:`UserInfo`. Never includes secrets."""
    return UserInfo(
        id=_user_id(user),
        username=_user_name(user),
        role=_user_role(user),
        email=_maybe_str(_get(user, "email")),
        created_at=_get(user, "created_at", "registered_at"),
    )


def _to_token_response(pair: Any) -> TokenResponse:
    """Convert a :class:`TokenPair` (or equivalent) into :class:`TokenResponse`.

    A **successful** login/refresh must return non-empty tokens. If the
    projection produces an empty ``access_token`` or ``refresh_token``,
    the client cannot authenticate and will retry indefinitely — so we
    raise a 500 instead of returning a plausible-looking but useless
    response. A previous version returned the empty pair silently,
    causing infinite client re-login loops with no diagnostic.
    """
    response: TokenResponse
    try:
        response = TokenResponse.from_pair(pair)
    except Exception:
        # Fallback for hosts that return a plain dict or an object with
        # the same fields but no ``to_dict``.
        if isinstance(pair, Mapping):
            response = TokenResponse.model_validate(dict(pair))
        else:
            response = TokenResponse(
                access_token=str(getattr(pair, "access_token", "") or ""),
                refresh_token=str(getattr(pair, "refresh_token", "") or ""),
                access_expires_in=int(
                    getattr(pair, "access_expires_in", 0) or 0
                ),
                refresh_expires_in=int(
                    getattr(pair, "refresh_expires_in", 0) or 0
                ),
                token_type=str(
                    getattr(pair, "token_type", "Bearer") or "Bearer"
                ),
            )

    if not response.access_token or not response.refresh_token:
        _log.error(
            "token projection produced empty token(s); "
            "access_present={} refresh_present={} pair_type={}",
            bool(response.access_token),
            bool(response.refresh_token),
            type(pair).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(
                ErrorCode.INTERNAL_ERROR,
                "Failed to issue a valid token pair.",
            ).model_dump(),
        )
    return response


# ===========================================================================
# Error translation
# ===========================================================================


def _translate_auth_error(
    exc: BaseException, *, context: str
) -> HTTPException:
    """Map a service exception to the appropriate HTTP error.

    Handles both the typed exceptions from :mod:`..security` (via
    ``isinstance``) and unknown service exceptions (via class-name
    matching). Never leaks a secret in the returned message.

    Rules:

    * Typed ``PasswordPolicyError`` → 422 with violations.
    * Typed ``Token*Error`` → 401 with the corresponding reason.
    * Typed ``InvalidCredentialsError`` → 401.
    * Typed ``SecurityError`` (other) → 500.
    * Class name contains ``PasswordPolicy`` → 422.
    * Class name contains ``AlreadyExists`` / ``Duplicate`` / ``Taken``
      → 409.
    * ``ValueError`` whose message contains ``taken`` / ``duplicate`` /
      ``already exists`` / ``already registered`` → 409.
      **The substring ``exists`` alone is NOT sufficient** — it matches
      ``"does not exist"`` and would produce a false-positive conflict.
    * Anything else → 500.
    """
    # Typed exceptions from the security module.
    if isinstance(exc, PasswordPolicyError):
        violations = list(getattr(exc, "violations", []) or [])
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_response(
                ErrorCode.VALIDATION_ERROR,
                "Password does not satisfy the policy.",
                details={"violations": violations},
            ).model_dump(),
        )
    if isinstance(exc, TokenExpiredError):
        return _unauthorized("Token has expired.", reason="TOKEN_EXPIRED")
    if isinstance(exc, TokenRevokedError):
        return _unauthorized(
            "Token has been revoked.", reason="TOKEN_REVOKED"
        )
    if isinstance(exc, TokenTypeMismatchError):
        return _unauthorized(
            "Token type mismatch.",
            reason="TOKEN_TYPE_MISMATCH",
        )
    if isinstance(exc, InvalidTokenError):
        return _unauthorized("Invalid token.", reason="TOKEN_INVALID")
    if isinstance(exc, InvalidCredentialsError):
        return _unauthorized(
            "Invalid username or password.",
            reason="INVALID_CREDENTIALS",
        )
    if isinstance(exc, TokenError):
        return _unauthorized("Invalid token.", reason="TOKEN_INVALID")
    if isinstance(exc, SecurityError):
        # Any other SecurityError = internal misconfiguration.
        _log.exception("security error (context={})", context)
        return _internal_error("Authentication system error.")

    # Class-name fallback for hosts that raise their own exceptions.
    name = type(exc).__name__
    msg = str(exc) or name
    msg_lower = msg.lower()

    if "PasswordPolicy" in name:
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_response(
                ErrorCode.VALIDATION_ERROR,
                "Password does not satisfy the policy.",
            ).model_dump(),
        )
    if "AlreadyExists" in name or "Duplicate" in name or "Taken" in name:
        return _conflict(msg)
    if isinstance(exc, ValueError):
        # Only match explicit "already registered" phrasing. Substring
        # "exists" alone matches "does not exist" — a false positive
        # that would turn a legitimate "user not found" bug into a
        # misleading 409 Conflict.
        if any(
            k in msg_lower
            for k in (
                "taken",
                "already exists",
                "already registered",
                "duplicate",
            )
        ):
            return _conflict(msg)

    _log.exception("auth error (context={})", context)
    return _internal_error(
        "An unexpected error occurred while processing authentication."
    )


# ===========================================================================
# Small helpers
# ===========================================================================


def _unauthorized(message: str, *, reason: str) -> HTTPException:
    """Build a 401 with the standard envelope and a ``reason`` in details."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=error_response(
            ErrorCode.UNAUTHORIZED,
            message,
            details={"reason": reason},
        ).model_dump(),
        headers={"WWW-Authenticate": "Bearer"},
    )


def _conflict(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=error_response(ErrorCode.CONFLICT, message).model_dump(),
    )


def _internal_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=error_response(ErrorCode.INTERNAL_ERROR, message).model_dump(),
    )


def _require_user_id(payload: CurrentUser) -> str:
    """Return ``payload.sub``, raising 401 if somehow absent."""
    # ``TokenPayload`` (Pydantic v2) is never falsy — only ``sub``
    # presence is meaningful.
    if not getattr(payload, "sub", None):
        raise _unauthorized(
            "Authenticated user is missing from the token.",
            reason="TOKEN_INVALID",
        )
    return payload.sub


def _password_hash(user: Any) -> str | None:
    """Extract the password hash from a user object, tolerant to field name."""
    for attr in ("password_hash", "hashed_password", "password"):
        value = getattr(user, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _user_id(user: Any) -> str:
    return str(_get(user, "id", "user_id", default="") or "")


def _user_name(user: Any) -> str:
    return str(_get(user, "username", "name", default="") or "")


def _user_role(user: Any) -> UserRole:
    raw = _get(user, "role", default=None)
    if isinstance(raw, UserRole):
        return raw
    if raw is None:
        return UserRole.USER
    try:
        return UserRole(str(raw))
    except (ValueError, TypeError):
        return UserRole.USER


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first non-``None`` attribute among ``names``."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


# ===========================================================================
# Self-audit checklist
# ===========================================================================
#
# When extending this router, verify:
#
# [ ] No new log statement contains a password, token, hash, or full
#     username. Passwords never logged — check every ``_log.info`` /
#     ``_log.debug`` call.
# [ ] Login failures remain **uniform 401**. Do not add an
#     "user not found" vs "wrong password" distinction anywhere in the
#     response or in a log line that could be surfaced to the client.
# [ ] The manual authentication path **must** run a dummy bcrypt
#     verification when the user is not found. Removing it enables
#     account enumeration via timing (measured in milliseconds between
#     the fast "user not found" branch and the ~100ms "wrong password"
#     branch).
# [ ] Every refresh goes through ``auth_service.refresh`` (which performs
#     replay detection). Do not compose ``verify_refresh`` + ``issue_pair``
#     — that path bypasses family revocation and is a security regression.
# [ ] Logout remains **idempotent** for the no-op / already-revoked
#     cases. Any new revocation path must not raise out of the handler
#     for those cases; catch, log at debug, and return 200. The **only**
#     exception is cross-user family revocation, which returns 403 — see
#     the checklist item below.
# [ ] The refresh-token family owner is checked against the caller's
#     ``payload.sub``. Removing this check enables a targeted DoS where
#     any authenticated user can log out any other user by presenting
#     their refresh token.
# [ ] ``/me`` never re-verifies the JWT (the ``CurrentUser`` dependency
#     does). It only looks up the user by ``payload.sub``.
# [ ] New custom reasons for ``details.reason`` are documented in the
#     module docstring.
# [ ] Cookie-based auth was **not** added without also adding CSRF
#     middleware. Bearer-only is safe by construction; cookies are not.
# [ ] Rate limiting for new endpoints is documented as "middleware's
#     job" — do not implement a token bucket inside the router.
# [ ] ``_to_token_response`` never returns empty tokens. A successful
#     login/refresh with an empty token is a 500, not a 200 with useless
#     data.
