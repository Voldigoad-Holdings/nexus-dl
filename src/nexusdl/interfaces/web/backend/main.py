"""FastAPI application factory and lifespan for the NexusDL web backend.

This is the **composition root** for the HTTP + WebSocket surface. It is
the only module that:

* reads :class:`Settings` to build the middleware configs,
* initializes lifecycle resources (:class:`ConnectionManager`,
  :class:`BusBridge`),
* registers the exception handlers that produce the standard
  ``{"error": {...}}`` envelope,
* mounts the routers via :func:`~.routers.mount_all`.

Two entry points are exposed:

* :func:`create_app` — the **factory**. Receives a pre-built
  :class:`~.dependencies.AppServices` container so that tests, fixtures,
  and alternate compositions can inject their own implementation. Every
  test uses this.

* :func:`create_app_from_env` — the **zero-argument factory** for
  ``uvicorn --factory`` / gunicorn. It calls the project's
  ``build_services()`` composition root and forwards the result to
  :func:`create_app`.

The module-level ``main()`` function is the CLI entrypoint for
single-worker development / staging::

    python -m nexusdl.interfaces.web.backend.main

For **multi-worker production**, use uvicorn directly::

    uvicorn nexusdl.interfaces.web.backend.main:create_app_from_env \\
        --factory --workers 4 --host 0.0.0.0 --port 8000

Multi-worker execution requires an import string, not an app instance —
uvicorn re-imports the module in each worker process, calls
:func:`create_app_from_env`, and gets an independent app + independent
service container per worker. When deploying this way, configure a
**shared** :class:`~.security.RevocationStore` (Redis) and a **shared**
rate-limit store (:class:`~.middleware.rate_limit.RateLimitStore`
backed by Redis) so that limits and revocations are consistent across
workers.

Middleware ordering
===================

The stack is built via :func:`~.middleware.build_middleware_stack`,
which returns a list in **runtime order**::

    Logging   →   CORS   →   Auth   →   RateLimit   →   app

Auth runs *before* rate-limit because rate-limit reads
``scope["state"]["user_id"]`` which auth populates. Logging wraps
everything so it sees 401s, 429s, and preflights alike. CORS sits
outside auth and rate-limit so preflight requests never consume a
rate-limit token and never require authentication.

**Do not** use ``app.add_middleware`` after building the stack — it
would push onto a LIFO list and produce a runtime order different from
what the configs expect. The factory exclusively uses
``FastAPI(middleware=[...])`` for this reason.

Exception handlers
==================

Routers use :func:`~.schemas.responses.error_response` to build the
standard envelope and raise it as an ``HTTPException`` detail. FastAPI's
default handler would wrap it as ``{"detail": {"error": {...}}}``. The
custom handlers installed here **unwrap** that structure so the wire
format is always::

    {"error": {"code": "...", "message": "...", "details": {...},
               "request_id": "..."}}

Four handlers are registered, in this precedence:

1. ``HTTPException`` — routers' own errors (with unwrap).
2. ``RequestValidationError`` — Pydantic validation of the body.
3. ``NexusDLError`` (from :mod:`nexusdl.core.exceptions`) — any domain
   exception that escaped a router's ``try``/``except``.
4. ``Exception`` — catch-all, logs with a request id, returns 500.

Static frontend
===============

When ``settings.frontend.static_dir`` points to an existing directory,
the factory mounts it at ``/`` with ``html=True``. This serves the
Next.js static export (or any SPA) as an origin-relative frontend while
the API stays under ``/api/v1`` and ``/ws/v1``. The mount is registered
**after** all routers, so ``/api/*`` and ``/health*`` are unaffected.

If the frontend is served by nginx or Caddy in production (the more
common pattern), leave ``static_dir`` unset and the mount is skipped.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger as _logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexusdl.core.exceptions import NexusDLError
from nexusdl.version import __version__

from .dependencies import AppServices
from .middleware import (
    AuthConfig,
    CORSConfig,
    LoggingConfig,
    RateLimitConfig,
    build_middleware_stack,
    default_rules,
    dev_cors,
    disabled_cors,
    single_origin_cors,
)
from .routers import list_routers, mount_all
from .schemas.responses import API_VERSION, error_response
from .websocket import (
    BusBridge,
    ConnectionManager,
    ConnectionManagerConfig,
    default_bus_bridge_config,
)

_log = _logger.bind(component="web.backend.main")


# ===========================================================================
# Constants
# ===========================================================================

_DEFAULT_HOST: str = "0.0.0.0"
_DEFAULT_PORT: int = 8000

#: Mapping of status code → default error code, used by the HTTPException
#: handler for exceptions raised by FastAPI/Starlette internals (routers
#: always set their own code).
_STATUS_TO_ERROR_CODE: dict[int, str] = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMIT_EXCEEDED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    502: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
    504: "INTERNAL_ERROR",
}

#: OpenAPI tag metadata — one entry per router namespace.
_OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "auth",
        "description": "Login, register, refresh, logout, and profile.",
    },
    {
        "name": "sites",
        "description": "Catalogue of enabled sites and their runtime health.",
    },
    {
        "name": "settings",
        "description": "Read and update application settings (admin-only writes).",
    },
    {
        "name": "search",
        "description": "Multi-site search and per-site browsing.",
    },
    {
        "name": "manga",
        "description": "Manga detail and chapter lists (library-first).",
    },
    {
        "name": "chapters",
        "description": "Chapter detail and page listings (library-first).",
    },
    {
        "name": "library",
        "description": "The caller's local library (list, progress, delete).",
    },
    {
        "name": "download",
        "description": (
            "Download queue management. Long-running operations are "
            "streamed over the WebSocket at /ws/v1/events."
        ),
    },
    {
        "name": "health",
        "description": "Liveness, readiness, and version probes.",
    },
    {
        "name": "websocket",
        "description": (
            "Real-time event stream at /ws/v1/events. Auth via the "
            "`token` query parameter (browsers cannot set headers on a "
            "WebSocket upgrade)."
        ),
    },
]


# ===========================================================================
# App factory
# ===========================================================================

def create_app(
    services: AppServices,
    *,
    cors_config: CORSConfig | None = None,
    auth_config: AuthConfig | None = None,
    rate_limit_config: RateLimitConfig | None = None,
    logging_config: LoggingConfig | None = None,
    ws_config: ConnectionManagerConfig | None = None,
    static_dir: Path | None = None,
    mount_static: bool = True,
    routers_only: set[str] | None = None,
    routers_skip: set[str] | None = None,
    expose_openapi: bool = True,
) -> FastAPI:
    """Build the FastAPI application.

    Parameters:
        services: The pre-built :class:`~.dependencies.AppServices`
            container. Must expose ``settings`` (for config derivation)
            and every field the routers consume. Built elsewhere — do
            not construct it in this module.
        cors_config: Override the CORS configuration. When ``None``,
            derives from ``services.settings`` (dev mode →
            :func:`dev_cors`, ``frontend.url`` set →
            :func:`single_origin_cors`, otherwise :func:`disabled_cors`).
        auth_config: Override the auth middleware configuration. When
            ``None``, uses the default :class:`AuthConfig`. If
            ``services.auth_service`` is ``None``, the middleware is
            skipped with a warning.
        rate_limit_config: Override the rate-limit configuration. When
            ``None``, uses :func:`default_rules`.
        logging_config: Override the logging configuration. When
            ``None``, uses the default :class:`LoggingConfig`.
        ws_config: Override the WebSocket engine configuration. When
            ``None``, uses :class:`ConnectionManagerConfig` defaults.
        static_dir: Directory to mount at ``/`` as a static SPA. When
            ``None``, falls back to ``services.settings.frontend.static_dir``
            if set. Ignored when ``mount_static=False``.
        mount_static: Master switch for static mounting. Set to
            ``False`` in tests to keep the app minimal.
        routers_only: If provided, mount only these routers. Useful for
            unit tests. Mutually exclusive with ``routers_skip``.
        routers_skip: If provided, skip these routers. Mutually exclusive
            with ``routers_only``.
        expose_openapi: Whether to serve ``/openapi.json`` and ``/docs``.
            Set to ``False`` for production deployments where the schema
            is published elsewhere.

    Returns:
        A fully-configured :class:`FastAPI` instance, ready for
        ``uvicorn.run(app, ...)`` or ``TestClient(app)``.

    Raises:
        ValueError: If ``services`` is missing required attributes, or
            if both ``routers_only`` and ``routers_skip`` are provided.
    """
    _validate_services(services)
    if routers_only is not None and routers_skip is not None:
        raise ValueError(
            "create_app: 'routers_only' and 'routers_skip' are mutually exclusive"
        )

    settings = services.settings
    is_dev = _is_dev(settings)

    # --- 1. Build the middleware stack in runtime order -----------------
    cors_cfg = cors_config if cors_config is not None else _derive_cors(settings)
    logging_cfg = logging_config if logging_config is not None else LoggingConfig()
    rate_cfg = rate_limit_config if rate_limit_config is not None else RateLimitConfig(
        rules=default_rules(),
        trust_forwarded_for=_get(settings, "api", "trust_forwarded_for", default=False),
        trusted_proxies=frozenset(
            _get(settings, "api", "trusted_proxies", default=())
        ),
    )

    auth_service = getattr(services, "auth_service", None)
    if auth_service is None:
        _log.warning(
            "create_app: services.auth_service is None — auth middleware "
            "will be disabled. Endpoints using CurrentUser will return "
            "500 at runtime."
        )
        auth_cfg = None
        stack_auth_service = None
    else:
        auth_cfg = auth_config if auth_config is not None else AuthConfig()
        stack_auth_service = auth_service

    stack = build_middleware_stack(
        logging=logging_cfg,
        cors=cors_cfg,
        auth=auth_cfg,
        auth_service=stack_auth_service,
        rate_limit=rate_cfg,
    )

    # --- 2. Create the app with the middleware already in place --------
    app = FastAPI(
        title="NexusDL API",
        version=__version__,
        description=(
            f"NexusDL multi-site manga download platform — HTTP API "
            f"(contract version {API_VERSION}).\n\n"
            "Authentication uses a bearer JWT obtained via `POST "
            "/api/v1/auth/login`. Long-running operations stream "
            "progress over the WebSocket at `/ws/v1/events`.\n\n"
            "See `/docs` for the interactive OpenAPI UI."
        ),
        openapi_url="/openapi.json" if expose_openapi else None,
        docs_url="/docs" if expose_openapi else None,
        redoc_url="/redoc" if expose_openapi else None,
        openapi_tags=_OPENAPI_TAGS,
        middleware=stack,
        lifespan=_make_lifespan(ws_config=ws_config),
    )

    # --- 3. Attach the service container --------------------------------
    # Done before lifespan so tests that disable lifespan still see it.
    app.state.services = services

    # --- 4. Register exception handlers --------------------------------
    _install_exception_handlers(app)

    # --- 5. Mount routers ----------------------------------------------
    mounted = mount_all(app, only=routers_only, skip=routers_skip)
    _log.info(
        "create_app: {} router(s) mounted (api {}): {}",
        len(mounted),
        API_VERSION,
        ", ".join(mounted) if mounted else "<none>",
    )

    # --- 6. Mount static frontend (optional, LAST) ---------------------
    resolved_static = _resolve_static_dir(settings, static_dir, mount_static)
    if resolved_static is not None:
        _mount_static(app, resolved_static)

    # --- 7. Startup logging --------------------------------------------
    _log_startup(app, services=services, is_dev=is_dev, mounted=mounted)

    return app


# ===========================================================================
# Lifespan
# ===========================================================================

def _make_lifespan(*, ws_config: ConnectionManagerConfig | None):
    """Return a lifespan context manager bound to ``ws_config``.

    A closure factory (rather than a module-level async function) so that
    the WebSocket configuration can be overridden per-app while still
    using the standard FastAPI ``lifespan=`` parameter.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services = _services_from_app(app)
        settings = services.settings

        _log.info(
            "NexusDL web backend starting (version {}, api {})",
            __version__,
            API_VERSION,
        )

        # --- 1. Domain-level startup hook -----------------------------
        startup_hook = getattr(services, "on_startup_async", None)
        if callable(startup_hook):
            try:
                await _maybe_await(startup_hook())
            except Exception:
                _log.exception(
                    "services.on_startup_async raised; aborting startup"
                )
                raise

        # --- 2. WebSocket engine --------------------------------------
        manager = ConnectionManager(
            config=ws_config or _derive_ws_config(settings)
        )
        await manager.start()
        app.state.connection_manager = manager
        _log.info("WebSocket engine started")

        # --- 3. EventBus → WebSocket bridge ---------------------------
        bridge: BusBridge | None = None
        event_bus = getattr(services, "event_bus", None)
        if event_bus is None:
            _log.warning(
                "services.event_bus is None — WS clients will receive "
                "no domain events. Set `event_bus` on the service "
                "container to enable real-time streaming."
            )
        else:
            try:
                bridge = BusBridge(
                    manager=manager,
                    event_bus=event_bus,
                    config=default_bus_bridge_config(),
                )
                bridge.start()
                app.state.bus_bridge = bridge
                _log.info("EventBus → WebSocket bridge started")
            except Exception:
                _log.exception(
                    "Failed to start the EventBus → WebSocket bridge; "
                    "WebSocket clients will connect but receive no events"
                )

        try:
            yield
        finally:
            # --- Shutdown: reverse order -----------------------------
            if bridge is not None:
                try:
                    bridge.stop()
                except Exception:
                    _log.exception("BusBridge.stop raised during shutdown")

            try:
                await manager.stop()
            except Exception:
                _log.exception(
                    "ConnectionManager.stop raised during shutdown"
                )

            shutdown_hook = getattr(services, "on_shutdown_async", None)
            if callable(shutdown_hook):
                try:
                    await _maybe_await(shutdown_hook())
                except Exception:
                    _log.exception(
                        "services.on_shutdown_async raised during shutdown"
                    )

            _log.info("NexusDL web backend stopped")

    return lifespan


# ===========================================================================
# Exception handlers
# ===========================================================================

def _install_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that produce the standard error envelope."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Handle HTTPException raised by routers or FastAPI internals.

        Routers raise ``HTTPException(detail=error_response(...).model_dump())``
        — i.e. ``detail`` is ``{"error": {...}}``. FastAPI's default
        handler would wrap it as ``{"detail": {"error": {...}}}``. We
        unwrap so the wire format is always ``{"error": {...}}``.

        If the detail is *not* our envelope (an HTTPException raised by
        FastAPI internals, a third-party middleware, or a router bug),
        we wrap it defensively in the standard shape.
        """
        detail = exc.detail

        # Router-produced envelope: use as-is.
        if isinstance(detail, dict) and set(detail.keys()) == {"error"}:
            return JSONResponse(
                status_code=exc.status_code,
                content=detail,
                headers=exc.headers,
            )

        # Wrap an arbitrary detail in the standard envelope.
        code = _STATUS_TO_ERROR_CODE.get(exc.status_code, "INTERNAL_ERROR")
        message = (
            str(detail)
            if detail
            else _default_message_for(exc.status_code)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response(
                code,
                message,
                request_id=_get_request_id(request),
            ).model_dump(),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Handle Pydantic validation errors with the standard envelope."""
        try:
            errors = exc.errors(include_url=False, include_input=False)
        except TypeError:
            errors = exc.errors()

        # FastAPI's errors() may include non-JSON-safe objects — coerce.
        safe_errors: list[dict[str, Any]] = []
        for e in errors:
            safe_errors.append(
                {
                    "loc": [str(p) for p in e.get("loc", ())],
                    "msg": str(e.get("msg", "")),
                    "type": str(e.get("type", "")),
                }
            )

        return JSONResponse(
            status_code=422,
            content=error_response(
                "VALIDATION_ERROR",
                "Request validation failed.",
                details={"errors": safe_errors},
                request_id=_get_request_id(request),
            ).model_dump(),
        )

    @app.exception_handler(NexusDLError)
    async def nexusdl_exception_handler(
        request: Request, exc: NexusDLError
    ) -> JSONResponse:
        """Handle domain exceptions that escaped a router."""
        code, status_code = _map_nexusdl_error(exc)
        _log.exception(
            "Unhandled NexusDLError in {} {}: {}",
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=status_code,
            content=error_response(
                code,
                str(exc) or type(exc).__name__,
                details={"exception": type(exc).__name__},
                request_id=_get_request_id(request),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Catch-all for any exception not covered above."""
        request_id = _get_request_id(request)
        _log.exception(
            "Unhandled exception in {} {} (request_id={})",
            request.method,
            request.url.path,
            request_id,
        )
        return JSONResponse(
            status_code=500,
            content=error_response(
                "INTERNAL_ERROR",
                "An unexpected error occurred.",
                details={"exception": type(exc).__name__},
                request_id=request_id,
            ).model_dump(),
        )


def _map_nexusdl_error(exc: BaseException) -> tuple[str, int]:
    """Map a domain exception to ``(error_code, http_status)``.

    Uses :func:`isinstance` when possible (for the typed exceptions in
    :mod:`nexusdl.core.exceptions`), falls back to class-name matching
    for hosts that raise their own subclasses.
    """
    name = type(exc).__name__

    # Class-name matching — robust to subclassing.
    if "Compliance" in name or "Blocked" in name:
        return "FORBIDDEN", 403
    if "NotFound" in name:
        return "NOT_FOUND", 404
    if "InvalidStateTransition" in name or "StateTransition" in name:
        return "CONFLICT", 409
    if "Timeout" in name:
        return "INTERNAL_ERROR", 504
    if "Unavailable" in name or "Retry" in name:
        return "SERVICE_UNAVAILABLE", 503
    if "Config" in name:
        return "CONFIG_ERROR", 500
    if "Parser" in name:
        return "PARSER_ERROR", 502
    if "Download" in name:
        return "DOWNLOAD_ERROR", 500
    if "Library" in name:
        return "LIBRARY_ERROR", 500
    if "Session" in name:
        return "INTERNAL_ERROR", 500

    return "INTERNAL_ERROR", 500


# ===========================================================================
# Static frontend mounting
# ===========================================================================

def _resolve_static_dir(
    settings: Any,
    override: Path | None,
    mount_static: bool,
) -> Path | None:
    """Resolve the static frontend directory, or return ``None``."""
    if not mount_static:
        return None

    candidate = override or _get(settings, "frontend", "static_dir", default=None)
    if candidate is None:
        return None

    try:
        path = Path(candidate).expanduser().resolve()
    except (TypeError, ValueError):
        _log.warning(
            "Invalid frontend.static_dir: {!r} — static mount skipped",
            candidate,
        )
        return None

    if not path.exists() or not path.is_dir():
        _log.warning(
            "frontend.static_dir {!r} does not exist or is not a directory "
            "— static mount skipped",
            str(path),
        )
        return None

    return path


def _mount_static(app: FastAPI, static_dir: Path) -> None:
    """Mount the static SPA at ``/``, after all routers."""
    try:
        from starlette.staticfiles import StaticFiles  # noqa: PLC0415
    except ImportError:
        _log.warning(
            "starlette.staticfiles is not available — static mount skipped"
        )
        return

    try:
        app.mount(
            "/",
            StaticFiles(directory=str(static_dir), html=True),
            name="static-frontend",
        )
    except Exception:
        _log.exception(
            "Failed to mount static directory {!r} at /", str(static_dir)
        )
        return

    _log.info(
        "Static frontend mounted at / from {!r}",
        str(static_dir),
    )


# ===========================================================================
# Derivation helpers (settings → configs)
# ===========================================================================

def _derive_cors(settings: Any) -> CORSConfig:
    """Build the CORS config from settings."""
    is_dev = _is_dev(settings)

    if is_dev:
        return dev_cors(extra_origins=_get_dev_extra_origins(settings))

    frontend_url = _get_frontend_url(settings)
    if frontend_url:
        return single_origin_cors(frontend_url, credentials=False)

    return disabled_cors()


def _derive_ws_config(settings: Any) -> ConnectionManagerConfig:
    """Build the WebSocket engine config from settings."""
    return ConnectionManagerConfig(
        queue_size=_get(settings, "websocket", "queue_size", default=256),
        heartbeat_interval_s=_get(
            settings, "websocket", "heartbeat_interval_s", default=25.0
        ),
        heartbeat_timeout_s=_get(
            settings, "websocket", "heartbeat_timeout_s", default=60.0
        ),
        max_connections=_get(
            settings, "websocket", "max_connections", default=2000
        ),
    )


def _is_dev(settings: Any) -> bool:
    """Return whether the deployment is in development mode."""
    env = _get(settings, "app", "environment", default=None)
    if isinstance(env, str) and env.lower() in ("dev", "development", "local"):
        return True
    dev_flag = _get(settings, "development", "enabled", default=None)
    if isinstance(dev_flag, bool):
        return dev_flag
    return False


def _get_frontend_url(settings: Any) -> str | None:
    """Return the configured frontend origin, or ``None``."""
    for path in (("frontend", "url"), ("app", "frontend_url")):
        value = _get(settings, *path, default=None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    env = os.environ.get("NEXUSDL_FRONTEND_URL")
    if env and env.strip():
        return env.strip()
    return None


def _get_dev_extra_origins(settings: Any) -> tuple[str, ...]:
    """Return additional origins to allow in dev mode."""
    value = _get(settings, "frontend", "dev_origins", default=None)
    if not value:
        return ()
    if isinstance(value, str):
        return (value.strip(),)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v).strip() for v in value if v)
    return ()


def _get(settings: Any, *path: str, default: Any = None) -> Any:
    """Walk ``settings`` by nested attribute names, returning ``default``."""
    current = settings
    for name in path:
        if current is None:
            return default
        current = getattr(current, name, None)
    return current if current is not None else default


# ===========================================================================
# Validation helpers
# ===========================================================================

def _validate_services(services: Any) -> None:
    """Verify that ``services`` exposes the minimum surface this module needs.

    Only fields consumed **by this file** are checked. Router-specific
    fields (``registry``, ``downloader``, …) are validated lazily by the
    routers that use them — a partial deployment is legal as long as it
    only mounts routers whose dependencies are present.
    """
    if services is None:
        raise ValueError("create_app: 'services' must not be None")
    if not hasattr(services, "settings") or services.settings is None:
        raise ValueError(
            "create_app: 'services.settings' is required — the factory "
            "reads CORS, logging, rate-limit, and WebSocket configuration "
            "from it."
        )


def _services_from_app(app: FastAPI) -> AppServices:
    """Return ``app.state.services``, raising a clear error if missing."""
    services = getattr(app.state, "services", None)
    if services is None:
        raise RuntimeError(
            "app.state.services is not set — did you forget to call "
            "create_app(services) instead of FastAPI(...)?"
        )
    return services


# ===========================================================================
# Logging helpers
# ===========================================================================

def _log_startup(
    app: FastAPI,
    *,
    services: AppServices,
    is_dev: bool,
    mounted: list[str],
) -> None:
    """Emit a compact startup summary."""
    _log.info("NexusDL web backend configured (version {})", __version__)
    _log.info("Environment: {}", "development" if is_dev else "production")
    _log.info("Mounted routers ({}): {}", len(mounted), ", ".join(mounted))
    _log.info(
        "WebSocket endpoint: /ws/v1/events "
        "(JWT via ?token= or Authorization: Bearer)"
    )
    all_routers = set(list_routers())
    skipped = all_routers - set(mounted)
    if skipped:
        _log.info("Skipped routers ({}): {}", len(skipped), ", ".join(sorted(skipped)))
    if hasattr(app, "docs_url") and app.docs_url:
        _log.info("OpenAPI UI: {}", app.docs_url)


# ===========================================================================
# Misc helpers
# ===========================================================================

async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if awaitable, else return it unchanged."""
    import inspect  # noqa: PLC0415

    if inspect.isawaitable(value):
        return await value
    return value


def _get_request_id(request: Request) -> str | None:
    """Extract the request id set by ``LoggingMiddleware``."""
    state = request.scope.get("state") or {}
    if isinstance(state, dict):
        rid = state.get("request_id")
        if isinstance(rid, str) and rid:
            return rid
    for name, value in request.scope.get("headers", ()):
        if name.lower() == b"x-request-id":
            try:
                return value.decode("latin-1").strip() or None
            except Exception:
                return None
    return None


def _default_message_for(status_code: int) -> str:
    """Return a human-friendly default message for a status code."""
    return {
        400: "Bad request.",
        401: "Authentication is required.",
        403: "You do not have permission to perform this action.",
        404: "Not found.",
        405: "Method not allowed.",
        409: "Conflict.",
        413: "Request entity too large.",
        415: "Unsupported media type.",
        422: "Request validation failed.",
        429: "Rate limit exceeded.",
        500: "An internal server error occurred.",
        501: "Not implemented.",
        502: "Upstream error.",
        503: "The service is temporarily unavailable.",
        504: "Upstream timed out.",
    }.get(status_code, "An error occurred.")


# ===========================================================================
# CLI entry point
# ===========================================================================

def create_app_from_env() -> FastAPI:
    """Zero-argument factory for ``uvicorn --factory`` / gunicorn.

    Calls the project's composition root (``build_services``) and
    forwards the resulting container to :func:`create_app`. Used by
    production deployments that need multiple workers::

        uvicorn nexusdl.interfaces.web.backend.main:create_app_from_env \\
            --factory --workers 4 --host 0.0.0.0 --port 8000

    Each worker process calls this factory independently and gets an
    independent app + services. Configure a shared
    :class:`~.security.RevocationStore` and rate-limit store (Redis) so
    that state is consistent across workers.
    """
    from nexusdl.core.bootstrap import build_services  # noqa: PLC0415

    services = build_services()
    return create_app(services)


def main() -> None:
    """Single-worker dev/staging entrypoint: ``python -m ...main``.

    For production multi-worker, use ``uvicorn --factory`` (see
    :func:`create_app_from_env`). This function intentionally does not
    shell out to a subprocess or re-import the module — it builds the
    services and the app in-process and hands them to uvicorn directly,
    which is the simplest, most debuggable pattern for local work.
    """
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        sys.stderr.write(
            "uvicorn is required to run the web backend.\n"
            "Install it with: pip install 'nexusdl[web]'\n"
        )
        sys.exit(1)

    from nexusdl.core.bootstrap import build_services  # noqa: PLC0415

    services = build_services()
    app = create_app(services)

    settings = services.settings
    host = str(_get(settings, "api", "host", default=_DEFAULT_HOST))
    port = int(_get(settings, "api", "port", default=_DEFAULT_PORT))
    reload_enabled = _is_dev(settings)
    log_level = str(
        _get(settings, "logging", "level", default="info")
    ).lower()

    _print_banner(host=host, port=port, reload_enabled=reload_enabled)

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload_enabled,
        log_config=None,       # we use loguru everywhere
        access_log=False,      # LoggingMiddleware handles it
        log_level=log_level,
    )


def _print_banner(*, host: str, port: int, reload_enabled: bool) -> None:
    """Print a compact banner when the CLI is invoked."""
    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    base = f"http://{display_host}:{port}"
    banner = f"""
╔══════════════════════════════════════════════════════════════╗
║                    NexusDL Web Backend                       ║
╠══════════════════════════════════════════════════════════════╣
║  Version : {__version__:<50}║
║  API     : {API_VERSION:<50}║
║  Docs    : {base + "/docs":<50}║
║  Health  : {base + "/health":<50}║
║  WS      : {base.replace("http", "ws") + "/ws/v1/events":<50}║
║  Reload  : {("yes" if reload_enabled else "no"):<50}║
╚══════════════════════════════════════════════════════════════╝
""".strip()
    sys.stderr.write(banner + "\n\n")


if __name__ == "__main__":
    main()
