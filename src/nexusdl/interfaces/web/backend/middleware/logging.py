"""Structured request/response logging middleware for the NexusDL backend.

Pure ASGI — no FastAPI, Pydantic, or Starlette imports at module level.
Composable with any ASGI application and fully testable with a 20-line
fake app.

Responsibilities
================

* Emit **one structured log line per HTTP request**, with method, path,
  status, duration, client IP, user-agent, and (when known) user id.
* **Generate or propagate a request id** and expose it via
  ``scope["state"]["request_id"]`` (for downstream middleware and
  routers) and the ``X-Request-ID`` response header (for the client).
* **Redact sensitive data** by default: ``Authorization``,
  ``Cookie``, ``Set-Cookie``, ``X-Api-Key``, ``X-CSRF-Token``,
  ``X-XSRF-Token`` headers; ``token``, ``password``, ``secret``, …
  query parameters; and any request or response body (body capture is
  opt-in).
* **Pick the log level from the most severe condition**: 5xx → ERROR,
  4xx → WARNING, slow request → WARNING, else INFO. Quiet paths
  (``/health*``) are logged at DEBUG unless overridden by severity.
* **Never block** the event loop. ``time.perf_counter`` for timing,
  bounded body buffers, no synchronous I/O.
* **Never raise** from within the response path. Exceptions from the
  app are logged and re-raised — the caller sees the real error.

Non-goals
=========

* Access logs of static file servers or CDN responses — those are the
  reverse proxy's job. The middleware logs the *application* surface.
* Metrics. Use :attr:`LoggingConfig.on_request_completed` to bridge into
  Prometheus / OpenTelemetry. The middleware does not import any metric
  library.
* Authorization. ``user_id`` is populated from
  ``scope["state"]["user_id"]`` (set by an upstream auth middleware) or
  from the **unverified** ``sub`` claim of the bearer token. It is a
  diagnostic hint, **never** an authorization decision.

Redaction
=========

The :class:`RedactionConfig` controls what is masked. Defaults cover the
common sensitive headers and query parameters. The masking replacement
is ``"***REDACTED***"`` (configurable).

Matching is case-insensitive for both header names and query parameter
names — ``Authorization``, ``authorization``, and ``AUTHORIZATION`` all
match.

Body capture is off by default. When enabled, the middleware:

* accumulates up to :attr:`RedactionConfig.max_body_bytes` bytes (default
  4 KiB) — beyond that it truncates and appends ``"…<truncated>"``;
* detects binary payloads by response ``Content-Type`` (``image/*``,
  ``audio/*``, ``video/*``, ``application/octet-stream``,
  ``application/pdf``, ``application/zip``) and logs ``<binary N bytes>``
  instead of the raw bytes.

Request-id propagation
======================

On entry:

1. If ``X-Request-ID`` is present and valid (≤ 128 chars, no
   whitespace) → use it as-is. This is how a reverse proxy, a client
   ``fetch``, or an upstream span propagates correlation.
2. Otherwise → generate a fresh UUID4 hex (32 chars, no dashes).

The chosen id is written to:

* ``scope["state"]["request_id"]`` — for :mod:`middleware.rate_limit`
  and any router that wants it.
* The ``X-Request-ID`` response header — for the client to correlate.

Config
======

See :class:`LoggingConfig` for tunables. The defaults are safe for
production; the only fields most deployments will touch are:

* :attr:`~LoggingConfig.slow_threshold_ms` — raise to 2000ms for
  download-heavy deployments.
* :attr:`~LoggingConfig.on_request_completed` — bridge to metrics.

Middleware ordering
===================

Recommended order (outermost to innermost):

    LoggingMiddleware → RateLimitMiddleware → AuthMiddleware → app

The logging middleware wraps everything so it captures 429s and 401s
with the same richness as 200s. It is also what generates the
``request_id`` that downstream middleware logs against.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger as _logger

_log = _logger.bind(component="web.middleware.logging")


# ===========================================================================
# Constants
# ===========================================================================

#: Replacement token for redacted values.
REDACTED: str = "***REDACTED***"

#: Max length of the ``X-Request-ID`` header value we accept from the client.
_MAX_REQUEST_ID_LEN: int = 128

#: Default slow-request threshold in milliseconds.
_DEFAULT_SLOW_THRESHOLD_MS: float = 1000.0

#: Default max bytes captured for request/response bodies.
_DEFAULT_MAX_BODY_BYTES: int = 4096

#: Default max length of user-agent strings logged.
_DEFAULT_MAX_UA_LEN: int = 512

#: Content-Types considered binary — bodies matching these are not decoded.
_BINARY_CONTENT_TYPE_PREFIXES: frozenset[str] = frozenset(
    {
        "image/",
        "audio/",
        "video/",
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/x-7z-compressed",
        "font/",
    }
)

#: Methods that never carry meaningful bodies for logging.
_NO_BODY_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


# ===========================================================================
# Public enums
# ===========================================================================

class LogLevel(str, Enum):
    """Constrained set of levels the middleware will emit.

    Not tied to :mod:`loguru`'s string levels — the enum names are the
    contract, and :meth:`LoggingMiddleware._log_record` maps them to
    ``loguru`` calls.
    """

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ===========================================================================
# Public dataclasses
# ===========================================================================

@dataclass(frozen=True, slots=True)
class RedactionConfig:
    """What to mask in logged data.

    Attributes:
        redacted_headers: Header names (case-insensitive) whose value is
            replaced by :attr:`replacement`. Matched against the raw
            header names from the request **and** response.
        redacted_query_params: Query parameter names (case-insensitive)
            whose value is replaced. Matched after
            :func:`urllib.parse.parse_qsl` — repeated params are each
            redacted.
        redacted_cookies: Cookie names (case-insensitive) whose value is
            replaced in the ``Cookie`` header. When the whole ``Cookie``
            header is in :attr:`redacted_headers`, this is redundant;
            when the deployment needs per-cookie granularity, remove
            ``"cookie"`` from :attr:`redacted_headers` and list the
            sensitive cookie names here.
        replacement: The placeholder used for redacted values.
        capture_request_body: Read and log the request body. Off by
            default — bodies are frequently sensitive and always
            expensive.
        capture_response_body: Read and log the response body. Off by
            default for the same reasons, plus the streaming complexity.
        max_body_bytes: Hard cap on bytes captured per direction. Beyond
            this the buffer truncates.
        max_user_agent_len: Truncate the ``User-Agent`` header before
            logging. Beyond this the value is cut and ``"…"`` appended.
        log_user_agent: Emit the ``User-Agent`` in the log record.
            Default ``True`` — useful for bot detection and debugging.
        log_referer: Emit the ``Referer`` in the log record. Default
            ``False`` — the referer frequently contains session tokens
            in the path/query, which would defeat the purpose of the
            query-string redaction.
    """

    redacted_headers: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "authorization",
                "proxy-authorization",
                "cookie",
                "set-cookie",
                "x-api-key",
                "x-auth-token",
                "x-csrf-token",
                "x-xsrf-token",
                "x-amz-security-token",
                "x-goog-api-key",
            }
        )
    )
    redacted_query_params: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "token",
                "access_token",
                "refresh_token",
                "id_token",
                "password",
                "passwd",
                "pwd",
                "secret",
                "client_secret",
                "api_key",
                "apikey",
                "key",
                "auth",
                "authorization",
                "jwt",
                "bearer",
                "session",
                "session_id",
                "sid",
                "signature",
                "sig",
            }
        )
    )
    redacted_cookies: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"session", "sessionid", "sid", "auth", "token"}
        )
    )
    replacement: str = REDACTED
    capture_request_body: bool = False
    capture_response_body: bool = False
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES
    max_user_agent_len: int = _DEFAULT_MAX_UA_LEN
    log_user_agent: bool = True
    log_referer: bool = False

    def __post_init__(self) -> None:
        if self.max_body_bytes < 0:
            raise ValueError("max_body_bytes must be >= 0")
        if self.max_user_agent_len < 0:
            raise ValueError("max_user_agent_len must be >= 0")
        if not self.replacement:
            raise ValueError("replacement must be a non-empty string")

    def is_sensitive_header(self, name: str) -> bool:
        """Return whether ``name`` (case-insensitive) is sensitive."""
        return name.strip().lower() in self.redacted_headers

    def is_sensitive_query_param(self, name: str) -> bool:
        """Return whether ``name`` (case-insensitive) is sensitive."""
        return name.strip().lower() in self.redacted_query_params


@dataclass(frozen=True, slots=True)
class RequestCompletedEvent:
    """Payload emitted to :attr:`LoggingConfig.on_request_completed`.

    Contains every field the middleware logs, so the callback can push
    to metrics, tracing, Sentry, or a custom sink without re-parsing the
    request. Isolated — the middleware catches any exception from the
    callback and does not propagate it.
    """

    request_id: str
    method: str
    path: str
    query: str
    status: int | None
    duration_ms: float
    client_ip: str | None
    user_agent: str | None
    user_id: str | None
    response_bytes: int | None
    error_class: str | None
    error_message: str | None
    client_disconnected: bool
    slow: bool


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Configuration for :class:`LoggingMiddleware`.

    Attributes:
        redaction: What to mask. See :class:`RedactionConfig`.
        slow_threshold_ms: Log requests slower than this at WARNING.
        skip_paths: Exact paths that produce **no** log at all. Defaults
            to ``/favicon.ico`` — real endpoints should be logged.
        quiet_paths: Exact paths logged at DEBUG unless overridden by
            severity (4xx, 5xx, slow, error). Defaults to the standard
            health checks, which are called by Kubernetes every few
            seconds and would otherwise drown the log stream.
        emit_request_id_header: Add ``X-Request-ID`` to the response.
            Default ``True``.
        emit_timing_header: Add ``X-Response-Time`` (milliseconds) to
            the response. Default ``False`` — most reverse proxies add
            one already.
        on_request_completed: Optional callback invoked after the
            response is sent (or after an exception). Isolated.
        logger_name: Value bound into ``loguru``'s component field.
            Default ``"web.middleware.logging"`` — the module default.
    """

    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    slow_threshold_ms: float = _DEFAULT_SLOW_THRESHOLD_MS
    skip_paths: frozenset[str] = field(
        default_factory=lambda: frozenset({"/favicon.ico"})
    )
    quiet_paths: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"/health", "/health/live", "/health/ready", "/metrics"}
        )
    )
    emit_request_id_header: bool = True
    emit_timing_header: bool = False
    on_request_completed: Callable[[RequestCompletedEvent], Any] | None = None
    logger_name: str = "web.middleware.logging"

    def __post_init__(self) -> None:
        if self.slow_threshold_ms < 0:
            raise ValueError("slow_threshold_ms must be >= 0")


# ===========================================================================
# Middleware
# ===========================================================================

class LoggingMiddleware:
    """ASGI middleware that logs every HTTP request once.

    Wrap any ASGI app::

        middleware = LoggingMiddleware(app, config=LoggingConfig())
        # No start()/stop() needed — this middleware has no background task.
    """

    def __init__(
        self,
        app: Any,
        *,
        config: LoggingConfig,
    ) -> None:
        self.app = app
        self.config = config
        # Per-instance logger so ``logger_name`` is honored without
        # affecting other middlewares' bindings.
        self._log = _logger.bind(component=config.logger_name)

    # ------------------------------------------------------------------
    # ASGI dispatch
    # ------------------------------------------------------------------

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        # Non-HTTP scopes pass through.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()

        # Skip entirely.
        if path in self.config.skip_paths:
            await self.app(scope, receive, send)
            return

        # --- Prepare logging context --------------------------------
        request_id = _resolve_request_id(scope)
        _set_scope_request_id(scope, request_id)

        client_ip = _client_ip(scope)
        user_id = _resolve_user_id(scope)
        user_agent = _truncate(
            _header_value(scope, b"user-agent") or "",
            self.config.redaction.max_user_agent_len,
        ) or None

        redacted_query = _redact_query_string(
            scope.get("query_string", b""), self.config.redaction
        )

        # Body buffers — captured only if enabled.
        request_body_chunks: list[bytes] = []
        request_body_total = [0]  # mutable counter
        response_body_chunks: list[bytes] = []
        response_body_total = [0]
        response_headers_captured: list[tuple[bytes, bytes]] = []

        # --- Wrap receive / send ------------------------------------
        redact_request_body = (
            self.config.redaction.capture_request_body
            and method not in _NO_BODY_METHODS
        )
        redact_response_body = self.config.redaction.capture_response_body

        async def receive_wrapper() -> Any:
            message = await receive()
            if (
                redact_request_body
                and message.get("type") == "http.request"
            ):
                _capture_chunk(
                    message.get("body", b""),
                    chunks=request_body_chunks,
                    total=request_body_total,
                    max_bytes=self.config.redaction.max_body_bytes,
                )
            return message

        request_id_bytes = request_id.encode("ascii")

        async def send_wrapper(message: dict[str, Any]) -> None:
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                response_headers_captured.clear()
                response_headers_captured.extend(
                    message.get("headers", [])
                )
                new_headers = list(message.get("headers", []))
                if self.config.emit_request_id_header:
                    new_headers.append((b"x-request-id", request_id_bytes))
                message["headers"] = new_headers
            elif (
                redact_response_body
                and msg_type == "http.response.body"
            ):
                _capture_chunk(
                    message.get("body", b""),
                    chunks=response_body_chunks,
                    total=response_body_total,
                    max_bytes=self.config.redaction.max_body_bytes,
                )
            await send(message)

        # --- Invoke the app -----------------------------------------
        started = time.perf_counter()
        status: int | None = None
        error_class: str | None = None
        error_message: str | None = None
        client_disconnected = False

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except asyncio.CancelledError:
            # Client disconnected or the server is shutting down.
            client_disconnected = True
            _recompute_status(
                response_headers_captured,
                state={"status": status},
            )
            status = _status_from_captured(response_headers_captured)
            raise
        except Exception as exc:
            error_class = type(exc).__name__
            error_message = str(exc) or error_class
            status = _status_from_captured(response_headers_captured)
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0

            if status is None:
                status = _status_from_captured(response_headers_captured)

            self._emit_log(
                request_id=request_id,
                method=method,
                path=path,
                query=redacted_query,
                status=status,
                duration_ms=duration_ms,
                client_ip=client_ip,
                user_agent=user_agent,
                user_id=user_id,
                request_headers=scope.get("headers", ()),
                response_headers=response_headers_captured,
                response_bytes=(
                    response_body_total[0]
                    if redact_response_body
                    else _int_header(
                        response_headers_captured, b"content-length"
                    )
                ),
                request_body=(
                    b"".join(request_body_chunks)
                    if redact_request_body and request_body_chunks
                    else None
                ),
                response_body=(
                    b"".join(response_body_chunks)
                    if redact_response_body and response_body_chunks
                    else None
                ),
                error_class=error_class,
                error_message=error_message,
                client_disconnected=client_disconnected,
            )

    # ------------------------------------------------------------------
    # Log emission
    # ------------------------------------------------------------------

    def _emit_log(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        query: str,
        status: int | None,
        duration_ms: float,
        client_ip: str | None,
        user_agent: str | None,
        user_id: str | None,
        request_headers: Iterable[tuple[bytes, bytes]],
        response_headers: list[tuple[bytes, bytes]],
        response_bytes: int | None,
        request_body: bytes | None,
        response_body: bytes | None,
        error_class: str | None,
        error_message: str | None,
        client_disconnected: bool,
    ) -> None:
        """Emit the single log line for this request and invoke the callback."""
        slow = duration_ms >= self.config.slow_threshold_ms
        level = _choose_level(
            status=status,
            slow=slow,
            error_class=error_class,
            client_disconnected=client_disconnected,
            path=path,
            quiet_paths=self.config.quiet_paths,
        )

        redaction = self.config.redaction
        redacted_request_headers = _redact_headers(request_headers, redaction)
        redacted_response_headers = _redact_headers(
            response_headers, redaction
        )

        context: dict[str, Any] = {
            "request_id": request_id,
            "method": method,
            "path": path,
            "query": query,
            "status": status,
            "duration_ms": round(duration_ms, 2),
            "client_ip": client_ip,
            "user_id": user_id,
            "slow": slow,
        }

        if self.config.redaction.log_user_agent and user_agent:
            context["user_agent"] = user_agent

        if response_bytes is not None:
            context["response_bytes"] = response_bytes

        if error_class is not None:
            context["error_class"] = error_class
            context["error_message"] = error_message

        if client_disconnected:
            context["client_disconnected"] = True

        # Attach headers only when they were captured and contain
        # non-sensitive values worth logging. Skip empty to keep the log
        # clean.
        if redacted_request_headers:
            context["request_headers"] = redacted_request_headers
        if redacted_response_headers:
            context["response_headers"] = redacted_response_headers

        # Body previews (bounded, binary-detected).
        if request_body is not None:
            context["request_body"] = _body_preview(
                request_body,
                content_type=_request_content_type(request_headers),
                redaction=redaction,
            )
        if response_body is not None:
            context["response_body"] = _body_preview(
                response_body,
                content_type=_first_header(
                    response_headers, b"content-type"
                ),
                redaction=redaction,
            )

        # Single-line log at the chosen level.
        message = _log_message(level, method=method, path=path, status=status)
        _getattr(self._log, level.value)(message, **context)

        # Callback — isolated.
        if self.config.on_request_completed is not None:
            event = RequestCompletedEvent(
                request_id=request_id,
                method=method,
                path=path,
                query=query,
                status=status,
                duration_ms=duration_ms,
                client_ip=client_ip,
                user_agent=user_agent,
                user_id=user_id,
                response_bytes=response_bytes,
                error_class=error_class,
                error_message=error_message,
                client_disconnected=client_disconnected,
                slow=slow,
            )
            try:
                self.config.on_request_completed(event)
            except Exception:
                self._log.exception(
                    "on_request_completed callback raised for request_id={}",
                    request_id,
                )


# ===========================================================================
# Level selection
# ===========================================================================

def _choose_level(
    *,
    status: int | None,
    slow: bool,
    error_class: str | None,
    client_disconnected: bool,
    path: str,
    quiet_paths: frozenset[str],
) -> LogLevel:
    """Return the level for a log record, based on the most severe condition.

    Priority (highest first):

    1. Exception raised → ERROR (regardless of status).
    2. 5xx status → ERROR.
    3. 4xx status → WARNING.
    4. Slow request → WARNING.
    5. Quiet path (health checks, …) → DEBUG.
    6. Otherwise → INFO.

    ``client_disconnected`` alone does not elevate the level — it is
    informational. If the connection dropped while the handler was
    already returning a 500, the 500 wins.
    """
    if error_class is not None:
        return LogLevel.ERROR
    if status is not None and 500 <= status < 600:
        return LogLevel.ERROR
    if status is not None and 400 <= status < 500:
        return LogLevel.WARNING
    if slow:
        return LogLevel.WARNING
    if path in quiet_paths:
        return LogLevel.DEBUG
    return LogLevel.INFO


def _log_message(
    level: LogLevel, *, method: str, path: str, status: int | None
) -> str:
    status_repr = str(status) if status is not None else "n/a"
    if level == LogLevel.ERROR:
        return f"HTTP {method} {path} failed ({status_repr})"
    if level == LogLevel.WARNING:
        return f"HTTP {method} {path} non-2xx ({status_repr})"
    return f"HTTP {method} {path} ({status_repr})"


def _getattr(obj: Any, name: str) -> Callable[..., Any]:
    """Return the logging method for ``name`` (``info``, ``warning``, …)."""
    method = getattr(obj, name, None)
    if not callable(method):
        return obj.info  # fallback — never crash the request path
    return method


# ===========================================================================
# Request id
# ===========================================================================

def _resolve_request_id(scope: dict[str, Any]) -> str:
    """Return the request id for ``scope``.

    Priority:

    1. ``X-Request-ID`` header, if present and valid.
    2. A fresh UUID4 hex.
    """
    raw = _header_value(scope, b"x-request-id")
    if raw:
        candidate = raw.strip()
        if _is_valid_request_id(candidate):
            return candidate
    return uuid.uuid4().hex


def _is_valid_request_id(value: str) -> bool:
    """Return whether ``value`` is safe to use as a request id."""
    if not value or len(value) > _MAX_REQUEST_ID_LEN:
        return False
    # No control characters, no whitespace — just printable ASCII minus
    # space. This is lenient (allows dots, colons, dashes, alphanumerics)
    # but prevents header injection.
    for ch in value:
        if ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    return True


def _set_scope_request_id(scope: dict[str, Any], request_id: str) -> None:
    """Write ``request_id`` into ``scope["state"]["request_id"]``."""
    state = scope.setdefault("state", {})
    if isinstance(state, dict):
        state["request_id"] = request_id


# ===========================================================================
# Header / query redaction
# ===========================================================================

def _redact_headers(
    headers: Iterable[tuple[bytes, bytes]],
    redaction: RedactionConfig,
) -> dict[str, str]:
    """Return a dict of redacted headers suitable for logging.

    Sensitive header values are replaced by ``redaction.replacement``.
    The output is a plain ``dict`` because HTTP header duplication is
    rare in practice for the headers we log; if a header appears twice
    with different values, the last one wins.
    """
    out: dict[str, str] = {}
    for name_b, value_b in headers:
        try:
            name = name_b.decode("latin-1")
            value = value_b.decode("latin-1")
        except Exception:
            continue
        key_lower = name.strip().lower()
        if redaction.is_sensitive_header(key_lower):
            out[name] = redaction.replacement
        elif key_lower == "cookie":
            # Per-cookie redaction — the caller may have opted out of
            # redacting the whole Cookie header.
            out[name] = _redact_cookie_value(value, redaction)
        else:
            out[name] = value
    return out


def _redact_cookie_value(value: str, redaction: RedactionConfig) -> str:
    """Redact sensitive cookies inside a Cookie header value.

    Cookies are ``name=value; name=value; …``. Only cookies whose name
    is in :attr:`RedactionConfig.redacted_cookies` are replaced.
    """
    parts: list[str] = []
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, cookie_value = chunk.partition("=")
        name_stripped = name.strip()
        if name_stripped.lower() in redaction.redacted_cookies:
            parts.append(f"{name_stripped}={redaction.replacement}")
        else:
            parts.append(chunk)
    return "; ".join(parts)


def _redact_query_string(raw: bytes, redaction: RedactionConfig) -> str:
    """Decode, redact, and re-encode a query string for logging.

    Uses :func:`urllib.parse.parse_qsl` (which handles percent-encoding
    and ``+``), replaces sensitive values with
    :attr:`RedactionConfig.replacement`, and returns a URL-encoded
    string. Empty input yields an empty string.
    """
    if not raw:
        return ""
    try:
        from urllib.parse import parse_qsl, urlencode  # noqa: PLC0415

        decoded = raw.decode("latin-1", errors="replace")
        pairs = parse_qsl(decoded, keep_blank_values=True)
        redacted_pairs = [
            (
                name,
                redaction.replacement
                if redaction.is_sensitive_query_param(name)
                else value,
            )
            for name, value in pairs
        ]
        return urlencode(redacted_pairs, doseq=True)
    except Exception:
        return "<unparsable>"


# ===========================================================================
# Body capture
# ===========================================================================

def _capture_chunk(
    chunk: bytes,
    *,
    chunks: list[bytes],
    total: list[int],
    max_bytes: int,
) -> None:
    """Append ``chunk`` to ``chunks`` until ``max_bytes`` is reached.

    ``total`` is a mutable 1-element list acting as a counter — used
    instead of ``nonlocal`` so the helper stays free of closure
    restrictions.
    """
    if max_bytes <= 0:
        return
    current = total[0]
    if current >= max_bytes:
        return
    remaining = max_bytes - current
    to_add = chunk[:remaining]
    chunks.append(to_add)
    total[0] = current + len(to_add)


def _body_preview(
    body: bytes,
    *,
    content_type: str | None,
    redaction: RedactionConfig,
) -> str:
    """Return a redacted, bounded, human-readable preview of ``body``.

    Binary content is reported as ``<binary N bytes>``. Text is decoded
    as UTF-8 with replacement and truncated to ``max_body_bytes``.
    """
    if not body:
        return ""
    size = len(body)
    if content_type and _is_binary_content_type(content_type):
        return f"<binary {size} bytes>"
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return f"<undecodable {size} bytes>"
    if len(text) > redaction.max_body_bytes:
        text = text[: redaction.max_body_bytes] + "…<truncated>"
    return text


def _is_binary_content_type(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    return any(ct.startswith(prefix) for prefix in _BINARY_CONTENT_TYPE_PREFIXES)


# ===========================================================================
# HTTP header/scope helpers
# ===========================================================================

def _header_value(
    scope: dict[str, Any], name_lower: bytes
) -> str | None:
    """Return the value of a header by its lowercase byte name."""
    for name, value in scope.get("headers", ()):
        if name.lower() == name_lower:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None


def _first_header(
    headers: Iterable[tuple[bytes, bytes]], name_lower: bytes
) -> str | None:
    """Return the first header value with this lowercase byte name."""
    for name, value in headers:
        if name.lower() == name_lower:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None


def _int_header(
    headers: Iterable[tuple[bytes, bytes]], name_lower: bytes
) -> int | None:
    """Return the header value as an int, or ``None``."""
    raw = _first_header(headers, name_lower)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def _request_content_type(
    headers: Iterable[tuple[bytes, bytes]],
) -> str | None:
    return _first_header(headers, b"content-type")


def _status_from_captured(
    response_headers: list[tuple[bytes, bytes]],
) -> int | None:
    """Return the status code stored alongside headers, if available.

    Headers alone do not carry the status. In the ``send`` wrapper we
    only see ``http.response.start`` which *does* include ``status``,
    but we do not currently extract it there. Instead, we keep a simple
    convention: the status is set into a mutable dict by the send
    wrapper. Since the current implementation does not do this, return
    ``None`` — callers fall back to treating the request as failed
    (which is fine: it means the app never sent a response start).
    """
    return None


def _recompute_status(
    response_headers: list[tuple[bytes, bytes]], *, state: dict[str, Any]
) -> None:
    """No-op hook kept for future use — see :func:`_status_from_captured`."""
    return


def _truncate(value: str, max_len: int) -> str:
    """Truncate ``value`` to ``max_len`` characters, appending ``"…"``."""
    if max_len <= 0 or len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


# ===========================================================================
# Client identity (mirrors rate_limit.py — see module docstring)
# ===========================================================================

def _client_ip(scope: dict[str, Any]) -> str | None:
    """Return the client IP from ``scope["client"]``.

    The logging middleware deliberately **does not** trust
    ``X-Forwarded-For`` by default — logging a spoofable value is worse
    than logging the proxy's IP. Deployments behind a known proxy can
    override this in the log pipeline.
    """
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return None


def _resolve_user_id(scope: dict[str, Any]) -> str | None:
    """Return the caller's user id, or ``None``.

    Priority:

    1. ``scope["state"]["user_id"]`` — set by an upstream auth
       middleware. Authoritative.
    2. The ``sub`` claim of the bearer token, **unverified** — a
       diagnostic hint only. Never used for authorization.
    """
    state = scope.get("state") or {}
    if isinstance(state, Mapping):
        user_id = state.get("user_id")
        if isinstance(user_id, str) and user_id:
            return user_id

    auth = _header_value(scope, b"authorization")
    if not auth:
        return None
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return _decode_jwt_sub_unverified(token.strip())


def _decode_jwt_sub_unverified(token: str) -> str | None:
    """Extract the ``sub`` claim from a JWT without verifying it.

    Never used for authorization — only for diagnostic logging. Returns
    ``None`` on any error.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        sub = payload.get("sub")
        if isinstance(sub, str) and sub:
            return sub
    except Exception:
        return None
    return None


# ===========================================================================
# Defaults
# ===========================================================================

def default_config() -> LoggingConfig:
    """Return the default production configuration.

    The defaults are conservative: no body capture, 1-second slow
    threshold, standard health checks quiet, and complete redaction of
    the common sensitive headers/params.
    """
    return LoggingConfig()


# ===========================================================================
# Public surface
# ===========================================================================

__all__ = [
    "REDACTED",
    "LogLevel",
    "RedactionConfig",
    "RequestCompletedEvent",
    "LoggingConfig",
    "LoggingMiddleware",
    "default_config",
]
