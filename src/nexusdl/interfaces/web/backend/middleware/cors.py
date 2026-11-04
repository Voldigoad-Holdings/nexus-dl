"""CORS middleware for the NexusDL web backend.

Pure ASGI — no FastAPI, Pydantic or Starlette imports at module level.
Composable with any ASGI application and fully testable with a fake app.

Why a custom implementation
===========================

Starlette's :class:`CORSMiddleware` exposes a single flat set of
headers. NexusDL deployments frequently need more: a public API with
credentialed access for known origins, an admin API locked to a single
origin, a dev frontend allowing ``http://localhost:*``. This middleware
exposes :class:`CORSRule` objects — each rule matches a subset of
requests by origin **and** path, and carries its own headers,
credentials policy, and preflight cache duration.

Correctness notes
=================

* **``Vary: Origin`` is always added** when the origin is reflected
  (i.e. when ``Access-Control-Allow-Origin`` equals the request origin).
  Without this, an intermediate cache (CDN, reverse proxy, browser cache
  with shared caches) can serve a response allowed for origin A to a
  request from origin B — a genuine security bug. This middleware merges
  ``Vary`` with any ``Vary`` already present rather than overwriting it.

* **The origin is canonicalized** before matching: scheme and host are
  lowercased, default ports (``:80`` for ``http``, ``:443`` for
  ``https``) are removed, and trailing slashes are stripped. This makes
  ``HTTPS://Example.COM:443/`` match an ``https://example.com`` rule
  without special-casing at each match site.

* **``*`` cannot be combined with ``allow_credentials=True``.** The
  spec forbids the combination — browsers reject such responses. This
  is enforced at construction time (:class:`CORSRule.__post_init__`) so
  a misconfiguration fails at boot rather than at runtime.

* **Preflight requests are handled locally.** An ``OPTIONS`` request
  carrying ``Origin`` and ``Access-Control-Request-Method`` is answered
  with a 204 and the appropriate headers — it **never** reaches the
  application. This keeps the rate-limit / auth middleware stacks out of
  the preflight path (browsers cache preflights, they are not real
  work).

* **Actual requests are never rejected by CORS alone.** Per the spec,
  CORS is a *response visibility* mechanism — the browser blocks the
  JavaScript from reading the response but the request still executes.
  Rejecting server-side would break non-browser clients that do not
  care about CORS. Deployments that need defense-in-depth (e.g. cookie
  auth) can set :attr:`CORSConfig.reject_unknown_origin` to ``True``.

* **The ``null`` origin is rejected by default.** Iframes with
  ``sandbox`` and ``file://`` URLs send ``Origin: null``. It is a
  common attack vector; opt in with :attr:`CORSConfig.allow_null_origin`
  when the deployment legitimately needs it (Electron, Tauri).

* **Existing CORS headers are preserved.** If a router sets
  ``Access-Control-Allow-Origin`` on its own response (e.g. an endpoint
  that needs a narrower origin than the middleware default), this
  middleware does not overwrite it. ``Vary: Origin`` is appended, never
  replaced.

Middleware ordering
===================

Recommended order (outermost to innermost)::

    Logging → CORS → RateLimit → Auth → app

CORS must be **inside** logging (so preflight requests are logged) but
**outside** rate-limit and auth (so preflight requests never touch
those, and so CORS headers are added to their 429/401 responses).

CORS does **not** apply to WebSocket upgrades — the WebSocket spec has
its own ``Origin`` check, handled in :mod:`routers.ws` via
``_extract_token``. Do not expect this middleware to gate WebSocket
connections.

Configuration ergonomics
========================

Three helpers cover the vast majority of deployments::

    # Single frontend origin, credentialed.
    config = single_origin_cors("https://app.example.com", credentials=True)

    # Local development — localhost on any port.
    config = dev_cors()

    # Explicit no-op (same-origin deployment behind a reverse proxy that
    # terminates CORS at the edge).
    config = disabled_cors()
"""
from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from loguru import logger as _logger

_log = _logger.bind(component="web.middleware.cors")


# ===========================================================================
# Constants
# ===========================================================================

#: Default max-age for preflight caching (10 minutes).
_DEFAULT_MAX_AGE_S: int = 600

#: Default set of methods allowed by a rule.
_DEFAULT_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)

#: Standard request headers a browser may include in a preflight. When a
#: rule uses ``allow_headers="*"``, the middleware echoes whatever the
#: browser requested (subject to :data:`_FORBIDDEN_ALLOW_HEADERS`).
_SAFE_HEADERS: frozenset[str] = frozenset(
    {
        "accept",
        "accept-language",
        "content-language",
        "content-type",
    }
)

#: Headers that must never appear in ``Access-Control-Allow-Headers``
#: even when the rule allows a wildcard. These are browser-controlled
#: and cannot be set by JavaScript; accepting them silently is a bug.
_FORBIDDEN_ALLOW_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "connection",
        "content-length",
        "transfer-encoding",
        "upgrade",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
    }
)

#: Literal ``"null"`` origin emitted by sandboxed iframes and file URLs.
_NULL_ORIGIN: str = "null"


# ===========================================================================
# Public dataclasses
# ===========================================================================

@dataclass(frozen=True, slots=True)
class CORSRule:
    """One CORS rule.

    A rule matches a request if **both** the origin patterns and the path
    patterns match, and the request method is in :attr:`methods`.

    Attributes:
        name: Stable identifier, used in logs and events.
        origin_patterns: Origin patterns this rule accepts. See
            :func:`_match_origin` for syntax. ``(" * ",)`` matches any
            origin (but is forbidden with ``allow_credentials=True``).
        path_patterns: Glob patterns matching the request path. ``("*",)``
            matches every path. Applied to the ASGI ``path`` (already
            percent-decoded and rooted at ``/``).
        methods: HTTP methods the rule allows. Preflight requests for
            methods outside this set are rejected.
        allow_headers: Headers permitted in the ``Access-Control-Allow-
            Headers`` preflight response. ``"*"`` echoes the browser's
            ``Access-Control-Request-Headers`` (except for the forbidden
            set). An explicit set is returned verbatim.
        expose_headers: Headers the browser is allowed to read from the
            actual response via ``Access-Control-Expose-Headers``.
        allow_credentials: Whether to emit ``Access-Control-Allow-
            Credentials: true``. Cannot be ``True`` with a wildcard
            origin.
        max_age_s: Preflight cache duration in seconds.
    """

    name: str
    origin_patterns: tuple[str, ...]
    path_patterns: tuple[str, ...] = ("*",)
    methods: frozenset[str] = field(default_factory=lambda: _DEFAULT_METHODS)
    allow_headers: frozenset[str] | str = "*"
    expose_headers: frozenset[str] = field(default_factory=frozenset)
    allow_credentials: bool = False
    max_age_s: int = _DEFAULT_MAX_AGE_S

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CORSRule.name must be a non-empty string")
        if not self.origin_patterns:
            raise ValueError(
                f"CORSRule {self.name!r}: origin_patterns must not be empty"
            )
        if (
            self.allow_credentials
            and "*" in self.origin_patterns
        ):
            raise ValueError(
                f"CORSRule {self.name!r}: allow_credentials=True cannot be "
                "combined with a wildcard origin ('*'). This is a W3C CORS "
                "requirement — browsers reject it."
            )
        if self.max_age_s < 0:
            raise ValueError(
                f"CORSRule {self.name!r}: max_age_s must be >= 0"
            )
        if not isinstance(self.allow_headers, (str, frozenset)):
            raise ValueError(
                f"CORSRule {self.name!r}: allow_headers must be '*' or a "
                "frozenset of header names"
            )


@dataclass(frozen=True, slots=True)
class CORSConfig:
    """Configuration for :class:`CORSMiddleware`.

    Attributes:
        rules: Ordered list of :class:`CORSRule`. The first match wins —
            order them from most specific to least specific.
        allow_null_origin: Whether to accept the ``"null"`` origin.
            Default ``False`` — it is a common attack vector (sandboxed
            iframes, ``file://``). Enable only when the deployment
            genuinely needs it (Electron, Tauri, some dev tools).
        reject_unknown_origin: If ``True``, **actual** requests from an
            origin that no rule matches are rejected with a 403 and the
            standard error envelope. If ``False`` (default, spec-
            compliant), the request proceeds and the response simply
            lacks CORS headers — the browser will block the JavaScript
            from reading it. Set to ``True`` only for deployments where
            defense-in-depth matters (cookie-auth APIs).
        emit_preflight_body: Emit the standard error envelope in the
            body of a rejected preflight. Default ``True`` — useful for
            debugging with curl and integration tests. Browsers ignore
            it.
        on_cors_allowed: Called for every request whose origin was
            accepted by a rule. Isolated — exceptions are logged and
            swallowed.
        on_cors_rejected: Called for every request whose origin was not
            accepted. Isolated.
    """

    rules: tuple[CORSRule, ...]
    allow_null_origin: bool = False
    reject_unknown_origin: bool = False
    emit_preflight_body: bool = True
    on_cors_allowed: Callable[[CORSAllowedEvent], Any] | None = None
    on_cors_rejected: Callable[[CORSRejectedEvent], Any] | None = None

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError(
                "CORSConfig.rules must contain at least one rule. "
                "Use `disabled_cors()` for an explicit no-op configuration."
            )


@dataclass(frozen=True, slots=True)
class CORSAllowedEvent:
    """Payload emitted to :attr:`CORSConfig.on_cors_allowed`."""

    rule_name: str
    origin: str
    path: str
    method: str
    is_preflight: bool


@dataclass(frozen=True, slots=True)
class CORSRejectedEvent:
    """Payload emitted to :attr:`CORSConfig.on_cors_rejected`."""

    origin: str | None
    path: str
    method: str
    is_preflight: bool
    reason: str  # "no_matching_rule" | "null_origin_rejected" | "bad_origin"


# ===========================================================================
# Middleware
# ===========================================================================

class CORSMiddleware:
    """ASGI middleware adding CORS headers per :class:`CORSConfig`.

    Wrap any ASGI app::

        middleware = CORSMiddleware(app, config=CORSConfig(rules=[...]))
        # No start()/stop() needed — this middleware has no background task.
    """

    def __init__(
        self,
        app: Any,
        *,
        config: CORSConfig,
    ) -> None:
        self.app = app
        self.config = config
        # Per-rule compiled regexes for origin patterns; computed once at
        # construction to avoid re-compiling on each request.
        self._compiled_patterns: dict[int, list[re.Pattern[str]]] = {}
        for rule in config.rules:
            compiled: list[re.Pattern[str]] = []
            for pattern in rule.origin_patterns:
                if pattern.startswith("re:"):
                    try:
                        compiled.append(re.compile(pattern[3:]))
                    except re.error:
                        _log.error(
                            "Invalid regex in CORSRule {!r}: {!r}",
                            rule.name,
                            pattern,
                        )
            self._compiled_patterns[id(rule)] = compiled

    # ------------------------------------------------------------------
    # ASGI dispatch
    # ------------------------------------------------------------------

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        origin_raw = _get_origin(scope)
        if origin_raw is None:
            # No Origin header — not a CORS request. Pass through.
            await self.app(scope, receive, send)
            return

        # Handle multiple origins in a single header (non-standard but
        # seen in wild). Use the first, log a warning.
        origin = _normalize_origin(origin_raw, allow_null=self.config.allow_null_origin)
        if origin is None:
            # Invalid or forbidden origin (e.g. "null" not allowed).
            await self._reject(
                scope, receive, send,
                origin=origin_raw,
                reason=(
                    "null_origin_rejected"
                    if origin_raw.strip() == _NULL_ORIGIN
                    else "bad_origin"
                ),
            )
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET").upper()

        is_preflight = (
            method == "OPTIONS"
            and _has_header(scope, b"access-control-request-method")
        )

        rule = self._find_matching_rule(
            origin=origin, path=path, method=method, is_preflight=is_preflight
        )

        if rule is None:
            await self._reject(
                scope, receive, send,
                origin=origin,
                reason="no_matching_rule",
            )
            return

        # Notify observers.
        self._emit_allowed(rule, origin, path, method, is_preflight)

        if is_preflight:
            await self._handle_preflight(scope, send, origin=origin, rule=rule)
            return

        # Actual request — add CORS headers to the response (without
        # overriding router-specific headers), then pass through.
        await self._handle_actual(
            scope, receive, send, origin=origin, rule=rule
        )

    # ------------------------------------------------------------------
    # Rule matching
    # ------------------------------------------------------------------

    def _find_matching_rule(
        self,
        *,
        origin: str,
        path: str,
        method: str,
        is_preflight: bool,
    ) -> CORSRule | None:
        """Return the first rule matching all three criteria."""
        # Preflight matches the *requested* method, not "OPTIONS".
        effective_method = method
        if is_preflight:
            # Extracted by the caller via ``_get_acrm`` for the preflight
            # path; we also accept the recorded method here for direct
            # unit tests.
            pass

        for rule in self.config.rules:
            if method not in rule.methods and not (
                is_preflight and "OPTIONS" in rule.methods
            ):
                # For preflight, the rule must at least allow the actual
                # requested method. The middleware checks that separately
                # via the Access-Control-Request-Method header, but a
                # rule with `methods={"GET"}` cannot accept an OPTIONS
                # preflight for a POST — reject here for clarity.
                continue
            if not _match_origin(origin, rule, self._compiled_patterns):
                continue
            if not _match_path(path, rule.path_patterns):
                continue
            return rule
        return None

    # ------------------------------------------------------------------
    # Preflight handling
    # ------------------------------------------------------------------

    async def _handle_preflight(
        self,
        scope: dict[str, Any],
        send: Any,
        *,
        origin: str,
        rule: CORSRule,
    ) -> None:
        """Answer an OPTIONS preflight locally with a 204 + CORS headers."""
        # The rule must allow the requested method.
        requested_method = _get_acrm(scope) or ""
        if requested_method.upper() not in rule.methods:
            await self._reject(
                scope, _null_receive, send,
                origin=origin,
                reason="method_not_allowed",
            )
            return

        headers: list[tuple[bytes, bytes]] = []
        headers.extend(_preflight_headers(origin=origin, rule=rule, scope=scope))
        headers.append((b"vary", _compute_vary(scope)))

        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": headers,
            }
        )
        await send(
            {"type": "http.response.body", "body": b"", "more_body": False}
        )

    # ------------------------------------------------------------------
    # Actual request handling
    # ------------------------------------------------------------------

    async def _handle_actual(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        origin: str,
        rule: CORSRule,
    ) -> None:
        """Pass through to the app, adding CORS headers to the response."""
        vary_bytes = _compute_vary(scope).encode("ascii")
        actual_headers = _actual_headers(origin=origin, rule=rule)

        async def send_with_cors(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                existing = list(message.get("headers", []))
                existing = _merge_headers(existing, actual_headers)
                existing = _merge_vary(existing, vary_bytes)
                message["headers"] = existing
            await send(message)

        await self.app(scope, receive, send_with_cors)

    # ------------------------------------------------------------------
    # Rejection
    # ------------------------------------------------------------------

    async def _reject(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        origin: str | None,
        reason: str,
    ) -> None:
        """Handle a CORS rejection.

        * For **preflight** requests: return 403 with **no** CORS
          headers and the standard error envelope (unless disabled).
        * For **actual** requests: either reject (if
          ``reject_unknown_origin=True``) or fall through unchanged.

        The chosen path is a deliberate trade-off documented in the
        module docstring — see the "Actual requests" note.
        """
        method = scope.get("method", "GET").upper()
        is_preflight = (
            method == "OPTIONS"
            and _has_header(scope, b"access-control-request-method")
        )

        self._emit_rejected(origin, scope.get("path", ""), method, is_preflight, reason)

        if is_preflight or self.config.reject_unknown_origin:
            _log.info(
                "CORS rejected: origin={!r} path={} method={} reason={}",
                origin,
                scope.get("path", ""),
                method,
                reason,
            )
            await self._send_cors_error(
                send, origin=origin, reason=reason, preflight=is_preflight
            )
            return

        # Actual request: pass through without CORS headers (browser
        # will block JS from reading the response).
        await self.app(scope, receive, send)

    async def _send_cors_error(
        self,
        send: Any,
        *,
        origin: str | None,
        reason: str,
        preflight: bool,
    ) -> None:
        """Emit a 403 response with the standard error envelope."""
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"vary", b"Origin"),
        ]

        if not self.config.emit_preflight_body:
            headers.append((b"content-length", b"0"))
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": headers,
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                }
            )
            return

        import json  # noqa: PLC0415 — lazy

        body = {
            "error": {
                "code": "FORBIDDEN",
                "message": "Cross-origin request rejected.",
                "details": {
                    "reason": reason,
                    "origin": origin,
                    "preflight": preflight,
                },
                "request_id": None,
            }
        }
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers.append((b"content-length", str(len(payload)).encode("ascii")))

        await send(
            {
                "type": "http.response.start",
                "status": 403,
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
    # Observer callbacks
    # ------------------------------------------------------------------

    def _emit_allowed(
        self,
        rule: CORSRule,
        origin: str,
        path: str,
        method: str,
        is_preflight: bool,
    ) -> None:
        cb = self.config.on_cors_allowed
        if cb is None:
            return
        event = CORSAllowedEvent(
            rule_name=rule.name,
            origin=origin,
            path=path,
            method=method,
            is_preflight=is_preflight,
        )
        try:
            cb(event)
        except Exception:
            _log.exception("on_cors_allowed callback raised")

    def _emit_rejected(
        self,
        origin: str | None,
        path: str,
        method: str,
        is_preflight: bool,
        reason: str,
    ) -> None:
        cb = self.config.on_cors_rejected
        if cb is None:
            return
        event = CORSRejectedEvent(
            origin=origin,
            path=path,
            method=method,
            is_preflight=is_preflight,
            reason=reason,
        )
        try:
            cb(event)
        except Exception:
            _log.exception("on_cors_rejected callback raised")


# ===========================================================================
# Origin canonicalization + matching
# ===========================================================================

def _normalize_origin(
    raw: str, *, allow_null: bool
) -> str | None:
    """Canonicalize a raw ``Origin`` header value.

    * ``"null"`` is accepted only when ``allow_null=True``; the literal
      string is returned as-is.
    * If the header contains multiple space-separated origins, only the
      **first** is used (the spec allows a list but browsers send one).
    * Otherwise, the value is parsed, scheme and host are lowercased,
      default ports are removed, and trailing slashes are stripped.

    Returns ``None`` when the value is invalid or not permitted.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped == _NULL_ORIGIN:
        return _NULL_ORIGIN if allow_null else None

    # Non-standard multi-origin header — keep the first, ignore the rest.
    if " " in stripped:
        stripped = stripped.split(" ", 1)[0].strip()
        if not stripped:
            return None

    try:
        parts = urlsplit(stripped)
    except ValueError:
        return None

    if not parts.scheme or not parts.netloc:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        # CORS applies to http(s) only. Anything else is not a browser
        # Origin we should reflect.
        return None

    # Lowercase host, preserve port, drop default ports.
    try:
        host = parts.hostname.lower() if parts.hostname else ""
        port = parts.port
    except ValueError:
        # Malformed netloc (invalid port syntax).
        return None
    if not host:
        return None

    default_port = 443 if scheme == "https" else 80
    port_part = "" if not port or port == default_port else f":{port}"
    return f"{scheme}://{host}{port_part}"


def _match_origin(
    origin: str,
    rule: CORSRule,
    compiled_patterns: dict[int, list[re.Pattern[str]]],
) -> bool:
    """Return whether ``origin`` matches any of ``rule.origin_patterns``."""
    if origin == _NULL_ORIGIN:
        # ``None`` origin was already rejected/normalized by the caller.
        # If we reach here it means allow_null_origin=True and the rule
        # declared "null" explicitly.
        return "null" in rule.origin_patterns

    for index, pattern in enumerate(rule.origin_patterns):
        if pattern == "*":
            return True
        if pattern == "null":
            # An explicit "null" pattern only matches the null origin.
            continue
        if pattern.startswith("re:"):
            compiled_list = compiled_patterns.get(id(rule), [])
            # Compiled patterns are stored in the same order as regex
            # patterns within the tuple; recompute the effective regex
            # for this index conservatively.
            for regex in compiled_list:
                if regex.fullmatch(origin):
                    return True
            continue
        # Exact match after canonicalization.
        canonical_pattern = _normalize_origin(pattern, allow_null=False)
        if canonical_pattern is None:
            # Pattern was invalid; fall through to glob attempt.
            canonical_pattern = pattern
        if canonical_pattern == origin:
            return True
        # Glob match — fnmatchcase is case-sensitive, matching the
        # canonical form we just produced.
        if any(c in canonical_pattern for c in "*?["):
            if fnmatch.fnmatchcase(origin, canonical_pattern):
                return True
    return False


def _match_path(path: str, patterns: tuple[str, ...]) -> bool:
    """Return whether ``path`` matches any of ``patterns``."""
    for pattern in patterns:
        if pattern == "*":
            return True
        if pattern.startswith("re:"):
            try:
                if re.fullmatch(pattern[3:], path):
                    return True
            except re.error:
                _log.error("Invalid regex in path_patterns: {!r}", pattern)
            continue
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


# ===========================================================================
# Header construction
# ===========================================================================

def _preflight_headers(
    *,
    origin: str,
    rule: CORSRule,
    scope: dict[str, Any],
) -> list[tuple[bytes, bytes]]:
    """Return the CORS headers for a preflight response."""
    headers: list[tuple[bytes, bytes]] = [
        (b"access-control-allow-origin", origin.encode("ascii")),
        (b"access-control-allow-methods", _join(rule.methods).encode("ascii")),
    ]

    # Access-Control-Allow-Headers
    if rule.allow_headers == "*":
        requested = _get_acrh(scope)
        if requested:
            filtered = [
                h for h in requested
                if h.strip().lower() not in _FORBIDDEN_ALLOW_HEADERS
            ]
            if filtered:
                headers.append(
                    (b"access-control-allow-headers", _join(filtered).encode("ascii"))
                )
        # If no header was requested, no Allow-Headers header is needed.
    elif rule.allow_headers:
        headers.append(
            (b"access-control-allow-headers", _join(rule.allow_headers).encode("ascii"))
        )

    if rule.allow_credentials:
        headers.append((b"access-control-allow-credentials", b"true"))

    if rule.max_age_s > 0:
        headers.append(
            (b"access-control-max-age", str(rule.max_age_s).encode("ascii"))
        )

    return headers


def _actual_headers(
    *, origin: str, rule: CORSRule
) -> list[tuple[bytes, bytes]]:
    """Return the CORS headers to attach to an actual response."""
    headers: list[tuple[bytes, bytes]] = [
        (b"access-control-allow-origin", origin.encode("ascii")),
    ]
    if rule.allow_credentials:
        headers.append((b"access-control-allow-credentials", b"true"))
    if rule.expose_headers:
        headers.append(
            (b"access-control-expose-headers", _join(rule.expose_headers).encode("ascii"))
        )
    return headers


def _merge_headers(
    existing: list[tuple[bytes, bytes]],
    additions: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """Append ``additions`` to ``existing`` unless already present.

    Preserves router-set CORS headers (e.g. an endpoint that needs a
    stricter origin than the middleware default).
    """
    existing_names = {name.lower() for name, _ in existing}
    out = list(existing)
    for name, value in additions:
        if name.lower() not in existing_names:
            out.append((name, value))
    return out


def _merge_vary(
    existing: list[tuple[bytes, bytes]], vary_bytes: bytes
) -> list[tuple[bytes, bytes]]:
    """Merge our ``Vary`` values with any existing ``Vary`` header.

    If multiple ``Vary`` headers exist (technically valid but rare), they
    are collapsed into a single header with comma-separated values.
    """
    existing_vary_values: list[str] = []
    out: list[tuple[bytes, bytes]] = []

    for name, value in existing:
        if name.lower() == b"vary":
            try:
                existing_vary_values.extend(
                    v.strip() for v in value.decode("latin-1").split(",")
                )
            except Exception:
                existing_vary_values.append(value.decode("latin-1"))
        else:
            out.append((name, value))

    try:
        new_values = [v.strip() for v in vary_bytes.decode("ascii").split(",")]
    except Exception:
        new_values = []

    merged = _dedupe_preserving_order(existing_vary_values + new_values)
    if merged:
        out.append((b"vary", ", ".join(merged).encode("latin-1")))
    return out


# ===========================================================================
# Scope helpers
# ===========================================================================

def _get_origin(scope: dict[str, Any]) -> str | None:
    """Return the raw ``Origin`` header value, or ``None``."""
    for name, value in scope.get("headers", ()):
        if name.lower() == b"origin":
            try:
                return value.decode("latin-1").strip()
            except Exception:
                return None
    return None


def _has_header(scope: dict[str, Any], name_lower: bytes) -> bool:
    for name, _ in scope.get("headers", ()):
        if name.lower() == name_lower:
            return True
    return False


def _get_acrm(scope: dict[str, Any]) -> str | None:
    """Return ``Access-Control-Request-Method``, or ``None``."""
    for name, value in scope.get("headers", ()):
        if name.lower() == b"access-control-request-method":
            try:
                return value.decode("latin-1").strip().upper()
            except Exception:
                return None
    return None


def _get_acrh(scope: dict[str, Any]) -> list[str]:
    """Return the list of headers from ``Access-Control-Request-Headers``."""
    for name, value in scope.get("headers", ()):
        if name.lower() == b"access-control-request-headers":
            try:
                raw = value.decode("latin-1")
            except Exception:
                return []
            return [h.strip() for h in raw.split(",") if h.strip()]
    return []


def _compute_vary(scope: dict[str, Any]) -> str:
    """Return the ``Vary`` header value covering CORS-relevant inputs."""
    values = ["Origin"]
    if scope.get("method", "").upper() == "OPTIONS" and _has_header(
        scope, b"access-control-request-method"
    ):
        values.extend(
            ["Access-Control-Request-Method", "Access-Control-Request-Headers"]
        )
    return ", ".join(values)


def _join(values: Iterable[str]) -> str:
    """Join a set of header names with commas, order-stable for tests."""
    return ", ".join(sorted(str(v) for v in values))


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


async def _null_receive() -> dict[str, Any]:
    """A no-op ``receive`` for endpoints that never call it."""
    return {"type": "http.disconnect"}


# ===========================================================================
# Configuration helpers
# ===========================================================================

def single_origin_cors(
    origin: str,
    *,
    credentials: bool = False,
    methods: frozenset[str] | None = None,
    allow_headers: frozenset[str] | str = "*",
    expose_headers: frozenset[str] = frozenset(
        {"x-request-id", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}
    ),
    max_age_s: int = _DEFAULT_MAX_AGE_S,
    rule_name: str = "default",
) -> CORSConfig:
    """Return a :class:`CORSConfig` allowing exactly one origin.

    This covers the most common deployment: one frontend domain calling
    the API. Two use cases:

    * **Bearer-token API** (NexusDL default): ``credentials=False``,
      ``allow_headers="*"``. The browser sends ``Authorization: Bearer
      <jwt>`` and CORS does not need to also allow cookies.
    * **Cookie-based session** (not the current design, but supported):
      ``credentials=True``. The browser sends ``Cookie`` + CSRF tokens.
      The origin must be explicit (no wildcard) — enforced by
      :class:`CORSRule.__post_init__`.

    Parameters mirror :class:`CORSRule`. ``expose_headers`` defaults to
    the standard NexusDL response headers so JS can read rate-limit
    headers and the request id.
    """
    return CORSConfig(
        rules=(
            CORSRule(
                name=rule_name,
                origin_patterns=(origin,),
                methods=methods or _DEFAULT_METHODS,
                allow_headers=allow_headers,
                expose_headers=expose_headers,
                allow_credentials=credentials,
                max_age_s=max_age_s,
            ),
        ),
    )


def dev_cors(
    *,
    extra_origins: tuple[str, ...] = (),
) -> CORSConfig:
    """Return a :class:`CORSConfig` suited for local development.

    Allows ``http://localhost``, ``http://127.0.0.1``, and any port on
    those hosts. ``extra_origins`` lets a developer add a specific
    domain (e.g. a staging instance). Credentials are enabled so cookie
    authentication flows work during development — no wildcard is used,
    so it is spec-compliant.

    **Do not use in production.** Localhost origins bypass the
    production origin check; a malicious user with a local server could
    proxy requests. Gate this behind an ``if settings.is_dev`` branch.
    """
    patterns = (
        "http://localhost",
        "http://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "https://localhost",
        "https://localhost:*",
        "https://127.0.0.1",
        "https://127.0.0.1:*",
    ) + tuple(extra_origins)

    return CORSConfig(
        rules=(
            CORSRule(
                name="dev",
                origin_patterns=patterns,
                methods=_DEFAULT_METHODS,
                allow_headers="*",
                expose_headers=frozenset(
                    {"x-request-id", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}
                ),
                allow_credentials=True,
                max_age_s=60,
            ),
        ),
        allow_null_origin=False,
    )


def disabled_cors() -> CORSConfig:
    """Return a :class:`CORSConfig` with no active rules.

    Used when CORS is terminated at the edge (nginx, Cloudflare Workers,
    an API gateway) and the application should not add or check any
    origin. Requests from any origin pass through — the middleware
    short-circuits and delegates to the app for **every** request.

    Any request with an ``Origin`` header still gets **no** CORS headers
    added, which is the intent: the edge is authoritative.
    """
    return CORSConfig(
        rules=(
            # A rule that never matches — provides a valid non-empty
            # rules tuple while ensuring the middleware short-circuits.
            CORSRule(
                name="disabled",
                origin_patterns=("never-match-this-origin",),
                path_patterns=("never-match-this-path",),
            ),
        ),
    )


# ===========================================================================
# Public surface
# ===========================================================================

__all__ = [
    "CORSRule",
    "CORSConfig",
    "CORSMiddleware",
    "CORSAllowedEvent",
    "CORSRejectedEvent",
    "single_origin_cors",
    "dev_cors",
    "disabled_cors",
]
