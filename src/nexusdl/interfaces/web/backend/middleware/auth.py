"""Authentication middleware for the NexusDL web backend.

Pure ASGI — no FastAPI, Pydantic, or Starlette imports at module level.
Composable with any ASGI application and fully testable with a fake app.

Purpose
=======

This middleware sits between :mod:`middleware.rate_limit` (outer) and
the routers (inner). Its single job is to **populate**
``scope["state"]`` with the authenticated identity so that:

* :mod:`middleware.rate_limit` can bucket by ``user`` instead of falling
  back to IP — the original motivation for shipping it *before* the
  routers.
* :mod:`middleware.logging` can include ``user_id`` in every log record
  without re-decoding the token.
* :mod:`..dependencies.get_current_payload` can **reuse** the already-
  verified :class:`~..security.TokenPayload` instead of re-verifying it
  (avoids a second revocation-store round-trip per request).

Two modes
=========

**Populate** (default) — the middleware attempts to extract and verify
the JWT. On success it populates ``scope["state"]`` and passes through.
On failure (no token, expired token, bad signature…) it **passes
through untouched**. The endpoint's own ``CurrentUser`` dependency will
produce the eventual 401 if authentication is required.

**Enforce** (opt-in) — for paths matched by ``AuthConfig.require_auth_paths``,
the middleware **rejects** the request if authentication fails, before
the router is reached. This is a defence-in-depth safety net: a future
route that forgets ``CurrentUser`` cannot accidentally become public.

The default configuration is **populate-only** — enforce is empty. This
mirrors the pattern used by FastAPI applications where ``CurrentUser``
is the canonical authorization gate; the middleware only exists to
provide early identity context.

State contract
==============

On successful authentication, the middleware populates::

    scope["state"]["user_id"]      # str
    scope["state"]["user_role"]    # UserRole
    scope["state"]["user_name"]    # str | None
    scope["state"]["auth_payload"] # TokenPayload (full)
    scope["state"]["auth_token_source"] # "header" | "query"

A downstream dependency can then do::

    payload = request.scope["state"].get("auth_payload")
    if payload is not None:
        return payload   # already verified — no re-decode
    # Otherwise, fall back to normal verification.

Token extraction
================

1. ``Authorization: Bearer <jwt>`` — the standard, always supported.
2. ``?token=<jwt>`` — **only if** ``AuthConfig.allow_query_token`` is
   ``True`` (default ``False``). Query tokens are logged by proxies and
   appear in browser history; use only when the deployment cannot set
   headers (e.g. an embedded iframe scenario, or a legacy CLI tool).

The header takes precedence when both are present.

Skip rules
==========

The middleware short-circuits (passes through untouched) on:

* **Non-HTTP scopes** — ``websocket`` (:mod:`routers.ws` handles its own
  auth via ``_extract_token``), ``lifespan``, ``http.response``.
* **Public paths** — configurable via ``public_paths`` and
  ``public_path_patterns``. Defaults cover ``/health*``, ``/auth/login``,
  ``/auth/register``, ``/auth/refresh``, ``/docs*``, ``/openapi.json``,
  ``/redoc*``, ``/static/*``, ``/favicon.ico``.
* **Trivial methods** — ``HEAD`` and ``OPTIONS`` carry no useful auth
  context and, in the OPTIONS case, are already handled by CORS.
* **Already-authenticated scopes** — if ``scope["state"]["user_id"]``
  is set (e.g. by a test fixture), the middleware does not overwrite it.

Rejection
=========

Only enforce mode rejects. The response is a **401** with the standard
error envelope, plus an ``X-Auth-Reason`` header echoing the reason
(``MISSING_TOKEN``, ``TOKEN_EXPIRED``, ``TOKEN_REVOKED``,
``TOKEN_INVALID``, ``TOKEN_TYPE_MISMATCH``) for non-browser clients that
need to distinguish "log in again" from "wait and retry". The token
value is **never** included in the response or in the logs.

Middleware ordering
===================

Recommended order (outermost to innermost)::

    Logging → CORS → RateLimit → Auth → app

Auth sits **inside** rate-limit so rate limits can bucket by user, and
**outside** the routers so ``CurrentUser`` finds ``auth_payload`` ready.
Preflight requests are already answered by CORS, so auth never sees
them.
"""
from __future__ import annotations

import base64
import binascii
import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl

from loguru import logger as _logger

from nexusdl.core.models.enums import UserRole

from ..security import (
    InvalidTokenError,
    TokenError,
    TokenExpiredError,
    TokenPayload,
    TokenRevokedError,
    TokenTypeMismatchError,
)

_log = _logger.bind(component="web.middleware.auth")


# ===========================================================================
# Constants
# ===========================================================================

#: Methods that never carry a useful auth context.
_TRIVIAL_METHODS: frozenset[str] = frozenset({"HEAD", "OPTIONS"})

#: Query parameter used for the token-in-query fallback.
_QUERY_TOKEN_NAME: str = "token"

#: Log prefix length for the token (we never log the full token).
_TOKEN_PREFIX_LEN: int = 8


# ===========================================================================
# Public Protocols
# ===========================================================================

@runtime_checkable
class AuthServiceLike(Protocol):
    """Minimal shape of :class:`~..security.AuthService` required here.

    Only ``verify_access`` is used. Implementations must be async (or
    return an awaitable), consistent with :class:`AuthService`.
    """

    async def verify_access(self, token: str) -> TokenPayload: ...


# ===========================================================================
# Public dataclasses
# ===========================================================================

@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Configuration for :class:`AuthMiddleware`.

    Attributes:
        public_paths: Exact paths that bypass authentication entirely.
            Defaults cover health checks, login/register/refresh,
            docs, openapi, static assets.
        public_path_patterns: Glob patterns (``fnmatch``) matching public
            paths. ``"/docs/*"`` matches ``/docs/swagger`` etc. Prefix a
            pattern with ``"re:"`` to use a regular expression.
        require_auth_paths: Exact paths that **must** carry a valid
            token. When empty (default), the middleware never rejects —
            it only populates state for downstream consumers. Set this
            for defence-in-depth on sensitive routes.
        require_auth_patterns: Glob patterns for required-auth paths.
        allow_query_token: Accept the JWT from ``?token=`` in addition to
            the ``Authorization`` header. Default ``False`` — query
            tokens end up in proxy logs and browser history.
        emit_reason_header: Include ``X-Auth-Reason`` on enforcement
            rejections. Default ``True`` — helps native clients decide
            between "log in again" and "wait and retry".
        on_auth_success: Called on every successful verification. Isolated.
            Sync callables and async callables are both supported — the
            middleware awaits a returned awaitable.
        on_auth_failure: Called on every failed verification in enforce
            mode. In populate mode, **expected** failures (token expired,
            invalid, …) are silent; **unexpected** exceptions raised by
            ``verify_access`` are logged regardless of mode, so a broken
            auth service is not invisible. Isolated (sync/async both
            supported).
    """

    public_paths: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "/",
                "/health",
                "/health/live",
                "/health/ready",
                "/api/version",
                "/favicon.ico",
                "/openapi.json",
                "/docs",
                "/redoc",
                "/api/v1/auth/login",
                "/api/v1/auth/register",
                "/api/v1/auth/refresh",
            }
        )
    )
    public_path_patterns: tuple[str, ...] = (
        "/docs/*",
        "/docs",
        "/redoc/*",
        "/redoc",
        "/static/*",
        "/assets/*",
        "/_next/*",
    )
    require_auth_paths: frozenset[str] = field(default_factory=frozenset)
    require_auth_patterns: tuple[str, ...] = ()
    allow_query_token: bool = False
    emit_reason_header: bool = True
    on_auth_success: Callable[[AuthSuccessEvent], Any] | None = None
    on_auth_failure: Callable[[AuthFailureEvent], Any] | None = None


@dataclass(frozen=True, slots=True)
class AuthSuccessEvent:
    """Payload emitted to :attr:`AuthConfig.on_auth_success`."""

    user_id: str
    role: UserRole
    path: str
    method: str
    token_source: str  # "header" | "query"


@dataclass(frozen=True, slots=True)
class AuthFailureEvent:
    """Payload emitted to :attr:`AuthConfig.on_auth_failure`."""

    reason: str
    path: str
    method: str
    token_prefix: str | None  # first 8 chars, or None if no token found


# ===========================================================================
# Middleware
# ===========================================================================

class AuthMiddleware:
    """ASGI middleware populating ``scope["state"]`` with the caller's identity.

    Wrap any ASGI app::

        middleware = AuthMiddleware(
            app,
            config=AuthConfig(),
            auth_service=services.auth_service,
        )
        # No start()/stop() — no background task.
    """

    def __init__(
        self,
        app: Any,
        *,
        config: AuthConfig,
        auth_service: AuthServiceLike,
    ) -> None:
        self.app = app
        self.config = config
        self.auth_service = auth_service

    # ------------------------------------------------------------------
    # ASGI dispatch
    # ------------------------------------------------------------------

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        # Non-HTTP scopes (websocket, lifespan, …) bypass auth entirely.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")

        # Trivial methods carry no auth context — pass through.
        if method in _TRIVIAL_METHODS:
            await self.app(scope, receive, send)
            return

        # Public paths bypass auth entirely.
        if self._is_public(path):
            await self.app(scope, receive, send)
            return

        # Already populated (upstream middleware, test fixture) — do not
        # overwrite. Still pass through.
        state = scope.get("state") or {}
        if isinstance(state, dict) and state.get("user_id"):
            await self.app(scope, receive, send)
            return

        # Enforce mode?
        enforce = self._requires_auth(path)

        # Extract token.
        token, source = self._extract_token(scope)
        if token is None:
            if enforce:
                await self._reject(
                    scope, send,
                    reason="MISSING_TOKEN",
                    token_prefix=None,
                )
                return
            # Populate mode: no token, pass through.
            await self.app(scope, receive, send)
            return

        # Verify.
        try:
            payload = await self.auth_service.verify_access(token)
        except TokenExpiredError:
            if enforce:
                await self._reject(
                    scope, send,
                    reason="TOKEN_EXPIRED",
                    token_prefix=_token_prefix(token),
                )
                return
            await self.app(scope, receive, send)
            return
        except TokenRevokedError:
            if enforce:
                await self._reject(
                    scope, send,
                    reason="TOKEN_REVOKED",
                    token_prefix=_token_prefix(token),
                )
                return
            await self.app(scope, receive, send)
            return
        except TokenTypeMismatchError:
            if enforce:
                await self._reject(
                    scope, send,
                    reason="TOKEN_TYPE_MISMATCH",
                    token_prefix=_token_prefix(token),
                )
                return
            await self.app(scope, receive, send)
            return
        except InvalidTokenError:
            if enforce:
                await self._reject(
                    scope, send,
                    reason="TOKEN_INVALID",
                    token_prefix=_token_prefix(token),
                )
                return
            await self.app(scope, receive, send)
            return
        except TokenError:
            # Any other security-module token error.
            if enforce:
                await self._reject(
                    scope, send,
                    reason="TOKEN_INVALID",
                    token_prefix=_token_prefix(token),
                )
                return
            await self.app(scope, receive, send)
            return
        except Exception:
            # Store outage, misconfiguration, etc. — never let it fail
            # silently in enforce mode; pass through in populate mode.
            _log.exception("Auth verification raised unexpectedly")
            if enforce:
                await self._reject(
                    scope, send,
                    reason="TOKEN_INVALID",
                    token_prefix=_token_prefix(token),
                )
                return
            await self.app(scope, receive, send)
            return

        # --- Success: populate state and dispatch.
        self._populate(scope, payload, source=source)
        await self._emit_success(payload, path, method, source)
        await self.app(scope, receive, send)

    # ------------------------------------------------------------------
    # Path matching
    # ------------------------------------------------------------------

    def _is_public(self, path: str) -> bool:
        """Return whether ``path`` bypasses authentication entirely."""
        if path in self.config.public_paths:
            return True
        for pattern in self.config.public_path_patterns:
            if _matches(path, pattern):
                return True
        return False

    def _requires_auth(self, path: str) -> bool:
        """Return whether ``path`` is in enforce mode."""
        if path in self.config.require_auth_paths:
            return True
        for pattern in self.config.require_auth_patterns:
            if _matches(path, pattern):
                return True
        return False

    # ------------------------------------------------------------------
    # Token extraction
    # ------------------------------------------------------------------

    def _extract_token(
        self, scope: dict[str, Any]
    ) -> tuple[str | None, str]:
        """Return ``(token, source)``.

        ``source`` is ``"header"``, ``"query"``, or ``""`` (none). Header
        takes precedence over query when both are present.
        """
        # 1) Authorization header.
        header_token = _bearer_token(scope)
        if header_token:
            return header_token, "header"

        # 2) Query string.
        if self.config.allow_query_token:
            query_token = _query_token(scope)
            if query_token:
                return query_token, "query"

        return None, ""

    # ------------------------------------------------------------------
    # State population
    # ------------------------------------------------------------------

    def _populate(
        self,
        scope: dict[str, Any],
        payload: TokenPayload,
        *,
        source: str,
    ) -> None:
        """Write the authenticated identity to ``scope["state"]``.

        Never overwrites an existing ``user_id`` — an upstream middleware
        or test fixture may have set one, and it takes precedence.
        """
        state = scope.setdefault("state", {})
        if not isinstance(state, dict):
            # Non-dict state (rare) — do not attempt to populate.
            return
        if state.get("user_id"):
            return
        state["user_id"] = payload.sub
        state["user_role"] = payload.role
        if payload.username:
            state["user_name"] = payload.username
        state["auth_payload"] = payload
        state["auth_token_source"] = source

    # ------------------------------------------------------------------
    # Rejection (enforce mode only)
    # ------------------------------------------------------------------

    async def _reject(
        self,
        scope: dict[str, Any],
        send: Any,
        *,
        reason: str,
        token_prefix: str | None,
    ) -> None:
        """Emit a 401 with the standard error envelope."""
        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()

        await self._emit_failure(reason, path, method, token_prefix)

        _log.info(
            "auth rejected: path={} method={} reason={} token_prefix={}",
            path,
            method,
            reason,
            token_prefix or "<none>",
        )

        body = {
            "error": {
                "code": "UNAUTHORIZED",
                "message": "Authentication is required for this endpoint.",
                "details": {"reason": reason},
                "request_id": _request_id_from_scope(scope),
            }
        }
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")

        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
            (b"www-authenticate", f'Bearer error="{_oauth_error(reason)}"'.encode("ascii")),
        ]
        if self.config.emit_reason_header:
            headers.append((b"x-auth-reason", reason.encode("ascii")))

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": payload,
                "more_body": False,
            }
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def _emit_success(
        self,
        payload: TokenPayload,
        path: str,
        method: str,
        source: str,
    ) -> None:
        cb = self.config.on_auth_success
        if cb is None:
            return
        event = AuthSuccessEvent(
            user_id=payload.sub,
            role=payload.role,
            path=path,
            method=method,
            token_source=source,
        )
        try:
            result = cb(event)
            # Support async callbacks (metrics exporters, tracing hooks,
            # alerting webhooks). A sync callable returns a non-awaitable
            # and falls through unchanged.
            if inspect.isawaitable(result):
                await result
        except Exception:
            _log.exception("on_auth_success callback raised")

    async def _emit_failure(
        self,
        reason: str,
        path: str,
        method: str,
        token_prefix: str | None,
    ) -> None:
        cb = self.config.on_auth_failure
        if cb is None:
            return
        event = AuthFailureEvent(
            reason=reason,
            path=path,
            method=method,
            token_prefix=token_prefix,
        )
        try:
            result = cb(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            _log.exception("on_auth_failure callback raised")


# ===========================================================================
# Helpers — path matching
# ===========================================================================

def _matches(path: str, pattern: str) -> bool:
    """Return whether ``path`` matches ``pattern``.

    Supports exact match, ``fnmatch`` globs, and ``"re:"`` prefixed
    regular expressions.
    """
    if pattern.startswith("re:"):
        import re  # noqa: PLC0415

        try:
            return re.fullmatch(pattern[3:], path) is not None
        except re.error:
            _log.error("Invalid regex in auth path pattern: {!r}", pattern)
            return False
    if not any(c in pattern for c in "*?["):
        return path == pattern
    import fnmatch  # noqa: PLC0415

    return fnmatch.fnmatchcase(path, pattern)


# ===========================================================================
# Helpers — token extraction
# ===========================================================================

def _bearer_token(scope: dict[str, Any]) -> str | None:
    """Return the bearer token from ``Authorization``, if present."""
    for name, value in scope.get("headers", ()):
        if name.lower() != b"authorization":
            continue
        try:
            raw = value.decode("latin-1")
        except Exception:
            return None
        scheme, _, token = raw.partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = token.strip()
        return token or None
    return None


def _query_token(scope: dict[str, Any]) -> str | None:
    """Return the token from ``?token=``, if present."""
    raw = scope.get("query_string", b"")
    if not raw:
        return None
    try:
        decoded = raw.decode("latin-1", errors="replace")
        pairs = parse_qsl(decoded, keep_blank_values=False)
    except Exception:
        return None
    for name, value in pairs:
        if name == _QUERY_TOKEN_NAME:
            value = value.strip()
            return value or None
    return None


def _token_prefix(token: str) -> str:
    """Return the first ``_TOKEN_PREFIX_LEN`` chars of ``token`` for logging."""
    return token[:_TOKEN_PREFIX_LEN] if token else ""


# ===========================================================================
# Helpers — misc
# ===========================================================================

def _request_id_from_scope(scope: dict[str, Any]) -> str | None:
    """Return the request id set by :mod:`middleware.logging`, if any."""
    state = scope.get("state") or {}
    if isinstance(state, dict):
        rid = state.get("request_id")
        if isinstance(rid, str) and rid:
            return rid
    # Fall back to the raw header for direct-invocation tests.
    for name, value in scope.get("headers", ()):
        if name.lower() == b"x-request-id":
            try:
                return value.decode("latin-1").strip() or None
            except Exception:
                return None
    return None


def _oauth_error(reason: str) -> str:
    """Map our internal reason to an RFC 6750 ``error`` code.

    Used in the ``WWW-Authenticate`` response header. Falls back to
    ``invalid_token`` for unknown reasons (which is the conservative,
    spec-compliant value).
    """
    mapping = {
        "MISSING_TOKEN": "invalid_request",
        "TOKEN_EXPIRED": "invalid_token",
        "TOKEN_REVOKED": "invalid_token",
        "TOKEN_INVALID": "invalid_token",
        "TOKEN_TYPE_MISMATCH": "invalid_token",
    }
    return mapping.get(reason, "invalid_token")


# ===========================================================================
# Default configuration
# ===========================================================================

def default_config() -> AuthConfig:
    """Return the default production configuration.

    Populate-only — no path is enforced. Deployments that want
    defense-in-depth add specific entries to ``require_auth_paths``::

        AuthConfig(
            require_auth_paths=frozenset({"/api/v1/auth/me"}),
            require_auth_patterns=("/api/v1/settings*",),
        )
    """
    return AuthConfig()


# ===========================================================================
# Public surface
# ===========================================================================

__all__ = [
    "AuthConfig",
    "AuthSuccessEvent",
    "AuthFailureEvent",
    "AuthMiddleware",
    "AuthServiceLike",
    "default_config",
]
