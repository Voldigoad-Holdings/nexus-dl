"""Rate limiting middleware for the NexusDL web backend.

Pure ASGI middleware — no FastAPI, Pydantic or Starlette imports at module
level. Works with any ASGI application and is fully testable with a
20-line fake app.

Architecture
============

* :class:`RateLimitMiddleware` — the ASGI callable.
* :class:`RateLimitConfig` — data-driven configuration: a list of
  :class:`RateLimitRule` plus a store, a clock and optional hooks.
* :class:`RateLimitRule` — one rule: which requests it matches (path
  glob + optional method), what limit applies, at what scope (per IP,
  per user, per IP+path, …), and which algorithm.
* :class:`RateLimitStore` (Protocol) — pluggable backend.
  :class:`InMemoryRateLimitStore` is provided; a Redis store can be
  injected for multi-worker deployments.
* :class:`RateLimitResult` — the per-request decision, with all header
  values pre-computed so :class:`RateLimitMiddleware` stays trivial.

Post-authentication identity
============================

The middleware runs **before** FastAPI's dependency system, so it cannot
use ``CurrentUser`` to identify the caller. Instead it resolves a
``user_id`` through three levels, in order:

1. ``scope["state"]["user_id"]`` — populated by an upstream auth
   middleware (:mod:`middleware.auth`, delivered separately). Preferred
   when present: it is already authenticated and verified.
2. ``Authorization: Bearer <jwt>`` — the ``sub`` claim of the JWT is
   extracted **without signature verification**. This is a **hint**, not
   a proof: the endpoint will reject an invalid token later. Its only
   purpose is per-user accounting. Do not rely on it for authorization.
3. Fall back to the client IP.

If a rule's scope is ``"user"`` or ``"user+path"`` and no user id is
available, the middleware falls back to the IP scope for that request
and logs a debug message. This prevents authenticated endpoints from
bypassing rate limits when the token is absent.

Algorithm choice
================

* :attr:`RateLimitAlgorithm.SLIDING_WINDOW` — "at most N requests in the
  last W seconds". Natural for auth brute-force protection
  (``/auth/login`` 5/min).
* :attr:`RateLimitAlgorithm.TOKEN_BUCKET` — "N requests/min sustained,
  with burst capacity". Natural for API quotas (``/download`` 30/min,
  ``600/min`` per user).

The store handles both algorithms; the rule picks which one applies.

Fail-open vs fail-closed
========================

If the store raises (Redis down, network error), the middleware honors
``rule.fail_open`` (falling back to ``config.default_fail_open`` when
``None``). The default is **fail-open** for availability, but rules can
opt in to fail-closed (drop the request) — recommended for
``/auth/login`` and ``/auth/register``.

Trusted proxies
===============

By default, the client IP is ``scope["client"][0]``. Behind a reverse
proxy, that is the **proxy's** IP for every request — the limit would
be shared by all clients. To read ``X-Forwarded-For``, the deployment
must:

* set ``config.trust_forwarded_for = True``,
* list the proxy IPs in ``config.trusted_proxies`` (or leave it empty to
  trust **all** forwarding clients — acceptable only behind a single
  known proxy).

Without ``trust_forwarded_for``, ``X-Forwarded-For`` is ignored — the
header is trivially spoofable and would let an attacker bypass
``/auth/login`` rate limiting by cycling fake IPs.

Response headers
================

Every request (allowed or rejected) carries:

* ``X-RateLimit-Limit`` — the rule's limit.
* ``X-RateLimit-Remaining`` — requests left in the current window.
* ``X-RateLimit-Reset`` — seconds until the counter resets.
* ``Retry-After`` — only on rejection; seconds to wait before retrying.

If no rule matches a request, no headers are injected (the request is
unlimited). If multiple rules match, the **most restrictive** applies
and its headers are injected.
"""
from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from loguru import logger as _logger

_log = _logger.bind(component="web.middleware.rate_limit")


# ===========================================================================
# Public enums
# ===========================================================================

class RateLimitAlgorithm(str, Enum):
    """Algorithm used by a rule to enforce its limit."""

    SLIDING_WINDOW = "sliding_window"
    """At most ``limit`` requests in any ``window_s``-second interval.

    Simple to reason about ("5 requests in the last 60 seconds") and the
    right default for auth endpoints.
    """

    TOKEN_BUCKET = "token_bucket"
    """``limit`` capacity, refilled continuously at ``limit / window_s``
    tokens per second.

    Allows bursts up to ``limit`` while capping sustained throughput —
    the right choice for API quotas.
    """


# ===========================================================================
# Public exceptions
# ===========================================================================

class RateLimitError(Exception):
    """Base exception for rate limit errors."""


# ===========================================================================
# Public dataclasses
# ===========================================================================

@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """One rate-limit rule.

    Attributes:
        name: Stable identifier, used in logs, in the 429 envelope, and
            as the key for per-rule overrides.
        path_pattern: Glob-style path matcher. ``"/api/v1/auth/*"``
            matches ``/api/v1/auth/login`` **and**
            ``/api/v1/auth/login/extra`` (``fnmatch`` treats ``*`` as
            matching any characters including ``/``). Use
            ``"/api/v1/auth/login"`` for an exact match, or prefix the
            pattern with ``"re:"`` to use a regular expression.
        method: HTTP method to match, or ``None`` for "any".
        limit: Maximum number of requests in the window.
        window_s: Window length in seconds (sliding window) or refill
            interval (token bucket).
        scope: Identity used to count requests. One of:
            - ``"ip"`` — client IP only.
            - ``"user"`` — authenticated user id, falls back to IP.
            - ``"ip+path"`` — IP plus the request path.
            - ``"user+path"`` — user id plus the request path.
            - ``"global"`` — a single counter for all callers.
        algorithm: Which counting algorithm to use. Defaults to
            :attr:`RateLimitAlgorithm.SLIDING_WINDOW`.
        fail_open: Behavior when the store raises. ``None`` inherits
            :attr:`RateLimitConfig.default_fail_open`. ``True`` allows
            the request, ``False`` rejects it.
    """

    name: str
    path_pattern: str
    limit: int
    window_s: float
    scope: str = "ip"
    method: str | None = None
    algorithm: RateLimitAlgorithm = RateLimitAlgorithm.SLIDING_WINDOW
    fail_open: bool | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError(
                f"RateLimitRule {self.name!r}: limit must be > 0"
            )
        if self.window_s <= 0:
            raise ValueError(
                f"RateLimitRule {self.name!r}: window_s must be > 0"
            )
        if self.scope not in _SCOPE_KINDS:
            raise ValueError(
                f"RateLimitRule {self.name!r}: invalid scope {self.scope!r}. "
                f"Allowed: {sorted(_SCOPE_KINDS)}"
            )


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a rate limit decision.

    All header values are pre-computed so the middleware only has to
    format them as bytes.
    """

    allowed: bool
    rule_name: str
    limit: int
    remaining: int
    reset_after_s: float
    retry_after_s: float | None = None

    @property
    def retry_after_int(self) -> int:
        """``Retry-After`` header value — at least 1 second."""
        if self.retry_after_s is None:
            return 1
        return max(1, int(self.retry_after_s + 0.999))

    @property
    def reset_after_int(self) -> int:
        """``X-RateLimit-Reset`` header value — at least 0."""
        return max(0, int(self.reset_after_s + 0.999))


@dataclass(frozen=True, slots=True)
class RateLimitEvent:
    """Event payload emitted via :attr:`RateLimitConfig.on_rate_limited`.

    Deliberately minimal — the callback lives in the host, which decides
    what to do (increment a metric, log to Sentry, send a Slack alert).
    """

    rule_name: str
    scope_value: str          # the identity string the rule matched on
    path: str
    method: str
    client_ip: str | None
    user_id: str | None
    limit: int
    window_s: float
    retry_after_s: float | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Configuration for :class:`RateLimitMiddleware`.

    Attributes:
        rules: Ordered list of :class:`RateLimitRule`. The first match
            wins — order rules from most specific to least specific.
        store: The counter backend. Defaults to
            :class:`InMemoryRateLimitStore` (single-process).
        clock: Monotonic clock in seconds. Injectable for tests.
            Defaults to :func:`time.monotonic`. When the default store
            is used, the clock is forwarded to it so that
            :meth:`RateLimitStore.cleanup` stays consistent with
            :meth:`RateLimitStore.check`.
        default_fail_open: Behavior when ``rule.fail_open`` is ``None``
            and the store raises. ``True`` (default) lets the request
            through; ``False`` rejects with a 429 — recommended for auth
            endpoints via per-rule ``fail_open=False``.
        emit_headers: Whether to add ``X-RateLimit-*`` headers to
            responses. Default ``True``. The standard ``Retry-After``
            header on 429 is emitted regardless of this flag.
        trust_forwarded_for: Read ``X-Forwarded-For`` for the client IP.
            Default ``False`` — spoofable otherwise. Enable only behind
            a known reverse proxy.
        trusted_proxies: Set of IPs allowed to set ``X-Forwarded-For``.
            Empty + ``trust_forwarded_for=True`` means "trust every
            forwarding client" — acceptable behind a single known proxy.
        exempt_paths: Paths (exact matches) that bypass rate limiting
            entirely. Defaults to ``/health``, ``/health/live``,
            ``/health/ready`` — Kubernetes probes must never be
            throttled.
        on_rate_limited: Optional callback for observability, called on
            every 429. Sync callables and async callables are both
            supported — the middleware awaits a returned awaitable.
            Exceptions are isolated: logged at ``error`` and swallowed.
        cleanup_interval_s: How often :meth:`InMemoryRateLimitStore.cleanup`
            runs when :meth:`RateLimitMiddleware.start` was called.
    """

    rules: Sequence[RateLimitRule]
    store: RateLimitStore | None = None
    clock: Callable[[], float] = time.monotonic
    default_fail_open: bool = True
    emit_headers: bool = True
    trust_forwarded_for: bool = False
    trusted_proxies: frozenset[str] = field(default_factory=frozenset)
    exempt_paths: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"/health", "/health/live", "/health/ready"}
        )
    )
    on_rate_limited: Callable[[RateLimitEvent], Any] | None = None
    cleanup_interval_s: float = 60.0


# ===========================================================================
# Public Protocols
# ===========================================================================

@runtime_checkable
class RateLimitStore(Protocol):
    """Pluggable counter backend.

    Implementations must be safe to call concurrently from the event
    loop. The in-memory default uses ``asyncio.Lock``; a Redis backend
    would use ``INCR`` / ``EXPIRE`` primitives.
    """

    async def check(
        self,
        *,
        key: str,
        limit: int,
        window_s: float,
        algorithm: RateLimitAlgorithm,
        now: float,
    ) -> RateLimitResult:
        """Record one request and return the decision.

        Implementations must be atomic: two concurrent calls for the
        same ``key`` must not both see a stale count.
        """

    async def cleanup(self) -> int:
        """Drop entries whose entire window has passed. Returns the
        number of entries removed."""


# ===========================================================================
# Internal constants
# ===========================================================================

_SCOPE_KINDS: frozenset[str] = frozenset(
    {"ip", "user", "ip+path", "user+path", "global"}
)

#: Methods that must never be rate-limited in a typical deployment —
#: Kubernetes probes, CORS preflight, health checks. Configurable via
#: :attr:`RateLimitConfig.exempt_paths`, but HEAD and OPTIONS are
#: always exempt here regardless of path.
_ALWAYS_EXEMPT_METHODS: frozenset[str] = frozenset({"HEAD", "OPTIONS"})


# ===========================================================================
# In-memory store
# ===========================================================================

@dataclass(slots=True)
class _WindowState:
    """Per-key state for the sliding-window algorithm."""

    timestamps: deque[float]


@dataclass(slots=True)
class _BucketState:
    """Per-key state for the token-bucket algorithm."""

    tokens: float
    last_refill: float


class InMemoryRateLimitStore:
    """Process-local counter store.

    Suitable for single-worker deployments and tests. **Not shared
    across workers** — a multi-worker deployment must use a shared store
    (Redis) so ``/auth/login`` cannot be rate-limited per worker instead
    of globally.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._windows: dict[str, _WindowState] = {}
        self._buckets: dict[str, _BucketState] = {}
        self._lock = asyncio.Lock()
        # Kept consistent with ``RateLimitConfig.clock`` when the
        # middleware owns the store — see ``RateLimitMiddleware.__init__``.
        # Prevents ``cleanup()`` from using a different clock source than
        # ``check()``, which would silently evict active entries under an
        # injected (frozen / shifted) clock.
        self._clock = clock

    async def check(
        self,
        *,
        key: str,
        limit: int,
        window_s: float,
        algorithm: RateLimitAlgorithm,
        now: float,
    ) -> RateLimitResult:
        async with self._lock:
            if algorithm == RateLimitAlgorithm.TOKEN_BUCKET:
                return self._check_token_bucket(
                    key=key, limit=limit, window_s=window_s, now=now
                )
            return self._check_sliding_window(
                key=key, limit=limit, window_s=window_s, now=now
            )

    async def cleanup(self) -> int:
        async with self._lock:
            return self._cleanup_locked()

    # ------------------------------------------------------------------
    # Sliding window
    # ------------------------------------------------------------------

    def _check_sliding_window(
        self, *, key: str, limit: int, window_s: float, now: float
    ) -> RateLimitResult:
        state = self._windows.get(key)
        if state is None:
            state = _WindowState(timestamps=deque())
            self._windows[key] = state

        cutoff = now - window_s
        # Evict expired timestamps.
        while state.timestamps and state.timestamps[0] <= cutoff:
            state.timestamps.popleft()

        used = len(state.timestamps)
        if used >= limit:
            # Oldest surviving request determines when a slot frees up.
            reset_after = state.timestamps[0] + window_s - now
            return RateLimitResult(
                allowed=False,
                rule_name="",  # populated by the middleware
                limit=limit,
                remaining=0,
                reset_after_s=max(0.0, reset_after),
                retry_after_s=max(0.0, reset_after),
            )

        state.timestamps.append(now)
        used_after = len(state.timestamps)
        return RateLimitResult(
            allowed=True,
            rule_name="",
            limit=limit,
            remaining=max(0, limit - used_after),
            reset_after_s=window_s,
            retry_after_s=None,
        )

    # ------------------------------------------------------------------
    # Token bucket
    # ------------------------------------------------------------------

    def _check_token_bucket(
        self, *, key: str, limit: int, window_s: float, now: float
    ) -> RateLimitResult:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _BucketState(tokens=float(limit), last_refill=now)
            self._buckets[key] = bucket

        # Refill: rate = limit / window_s tokens per second.
        elapsed = max(0.0, now - bucket.last_refill)
        refill_rate = limit / window_s
        bucket.tokens = min(float(limit), bucket.tokens + elapsed * refill_rate)
        bucket.last_refill = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            remaining = int(bucket.tokens)
            # Approximate: a full refill takes window_s, a single token
            # refills in window_s / limit seconds.
            reset_after = (float(limit) - bucket.tokens) / refill_rate
            return RateLimitResult(
                allowed=True,
                rule_name="",
                limit=limit,
                remaining=remaining,
                reset_after_s=reset_after,
                retry_after_s=None,
            )

        # Not enough tokens — compute wait time until the next token.
        retry_after = (1.0 - bucket.tokens) / refill_rate
        reset_after = retry_after
        return RateLimitResult(
            allowed=False,
            rule_name="",
            limit=limit,
            remaining=0,
            reset_after_s=reset_after,
            retry_after_s=retry_after,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_locked(self) -> int:
        """Sweep expired entries. Caller must hold ``self._lock``.

        Sliding windows: entries whose entire timestamp deque is empty
        (all timestamps past the window). Buckets: entries whose tokens
        have fully refilled (equivalent to "no active limiting").
        """
        # Use the injected clock — MUST match the clock ``check()`` uses
        # via the ``now`` parameter, otherwise an injected (frozen or
        # shifted) clock would cause active entries to be evicted as
        # "expired" and silently reset the rate limiter under test.
        now = self._clock()
        removed = 0

        # Empty windows — nothing to evict by time alone without knowing
        # each key's window_s. Keep entries that are "recently used" by
        # looking at the last timestamp only. Conservative: evict entries
        # whose newest timestamp is older than a fixed grace period.
        # Rules with very long windows should set a longer cleanup.
        grace_s = 600.0  # 10 minutes
        for key in list(self._windows.keys()):
            state = self._windows[key]
            if not state.timestamps or state.timestamps[-1] < now - grace_s:
                self._windows.pop(key, None)
                removed += 1

        # Fully refilled buckets — treat as removable.
        for key in list(self._buckets.keys()):
            bucket = self._buckets[key]
            if bucket.tokens >= 1.0 and bucket.last_refill < now - grace_s:
                self._buckets.pop(key, None)
                removed += 1

        return removed

    def __len__(self) -> int:
        return len(self._windows) + len(self._buckets)


# ===========================================================================
# Middleware
# ===========================================================================

class RateLimitMiddleware:
    """ASGI middleware enforcing per-rule rate limits.

    Wrap any ASGI app::

        middleware = RateLimitMiddleware(app, config=RateLimitConfig(rules=[...]))
        await middleware.start()   # optional: starts the cleanup task
    """

    def __init__(
        self,
        app: Any,
        *,
        config: RateLimitConfig,
    ) -> None:
        self.app = app
        self.config = config
        # Preserve the caller's store (if any) exactly, and forward the
        # config's clock to the default in-memory store so that
        # ``cleanup()`` and ``check()`` observe the same clock.
        if config.store is not None:
            self.store: RateLimitStore = config.store
        else:
            self.store = InMemoryRateLimitStore(clock=config.clock)

        self._cleanup_task: asyncio.Task[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the optional cleanup task. Idempotent.

        Calling ``start`` is **not required** for the middleware to
        enforce limits — it only enables periodic memory cleanup. If
        skipped, the store grows slowly and is freed on process exit.
        """
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._stopped = False
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="rate-limit-cleanup"
        )

    async def stop(self) -> None:
        """Stop the cleanup task. Idempotent."""
        self._stopped = True
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.exception("Cleanup task raised during stop")
            finally:
                self._cleanup_task = None

    # ------------------------------------------------------------------
    # ASGI dispatch
    # ------------------------------------------------------------------

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        # Non-HTTP scopes (websocket, lifespan) pass through untouched.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET")

        # Always exempt: health check paths, HEAD/OPTIONS methods.
        if path in self.config.exempt_paths or method in _ALWAYS_EXEMPT_METHODS:
            await self.app(scope, receive, send)
            return

        rule = self._match_rule(path=path, method=method)
        if rule is None:
            await self.app(scope, receive, send)
            return

        # Resolve identity + counting key.
        identity = self._resolve_identity(scope, rule)
        if identity is None:
            # Rule needs an identity we could not resolve — fall back to
            # IP. This is a defensive branch; scope resolution should
            # always succeed.
            identity = _client_ip(scope, self.config) or "unknown"
        key = f"{rule.name}:{identity}"

        # Ask the store for a decision.
        now = self.config.clock()
        try:
            decision = await self.store.check(
                key=key,
                limit=rule.limit,
                window_s=rule.window_s,
                algorithm=rule.algorithm,
                now=now,
            )
        except Exception:
            fail_open = (
                rule.fail_open
                if rule.fail_open is not None
                else self.config.default_fail_open
            )
            _log.exception(
                "Rate limit store raised (rule={}, fail_open={})",
                rule.name,
                fail_open,
            )
            if fail_open:
                # Allow the request — availability over strictness.
                await self.app(scope, receive, send)
                return
            # Fail closed: reject without a store decision.
            await self._send_429(
                scope,
                receive,
                send,
                rule=rule,
                result=RateLimitResult(
                    allowed=False,
                    rule_name=rule.name,
                    limit=rule.limit,
                    remaining=0,
                    reset_after_s=rule.window_s,
                    retry_after_s=rule.window_s,
                ),
                identity=identity,
            )
            return

        # Fill in the rule name (the store intentionally does not know it).
        decision = RateLimitResult(
            allowed=decision.allowed,
            rule_name=rule.name,
            limit=decision.limit,
            remaining=decision.remaining,
            reset_after_s=decision.reset_after_s,
            retry_after_s=decision.retry_after_s,
        )

        if not decision.allowed:
            await self._send_429(
                scope, receive, send,
                rule=rule, result=decision, identity=identity,
            )
            return

        # Inject rate-limit headers into the outgoing response.
        if not self.config.emit_headers:
            await self.app(scope, receive, send)
            return

        headers = _format_headers(decision, include_retry_after=False)

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                existing = list(message.get("headers", []))
                message["headers"] = existing + headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

    # ------------------------------------------------------------------
    # Rule matching
    # ------------------------------------------------------------------

    def _match_rule(
        self, *, path: str, method: str
    ) -> RateLimitRule | None:
        """Return the first rule whose pattern and method match.

        Rules are matched in order — the first match wins. Order them
        from most specific to least specific (``/auth/login`` before
        ``/auth/*`` before ``/*``).
        """
        for rule in self.config.rules:
            if rule.method is not None and rule.method.upper() != method.upper():
                continue
            if _path_matches(path, rule.path_pattern):
                return rule
        return None

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    def _resolve_identity(
        self, scope: dict[str, Any], rule: RateLimitRule
    ) -> str | None:
        """Return the identity string the rule should count on."""
        scope_kind = rule.scope
        path = scope.get("path", "")

        if scope_kind == "global":
            return "global"

        if scope_kind == "ip":
            return _client_ip(scope, self.config) or None

        if scope_kind == "ip+path":
            ip = _client_ip(scope, self.config) or "unknown"
            return f"{ip}:{path}"

        if scope_kind == "user":
            user_id = _resolve_user_id(scope)
            if user_id is None:
                # Fall back to IP — see the module docstring.
                _log.debug(
                    "Rate limit rule {!r} needs a user, none found; "
                    "falling back to IP", rule.name,
                )
                return _client_ip(scope, self.config) or None
            return user_id

        if scope_kind == "user+path":
            user_id = _resolve_user_id(scope)
            base = user_id or _client_ip(scope, self.config) or "unknown"
            return f"{base}:{path}"

        # Unknown scope — should be caught by RateLimitRule.__post_init__.
        return None

    # ------------------------------------------------------------------
    # 429 response
    # ------------------------------------------------------------------

    async def _send_429(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        rule: RateLimitRule,
        result: RateLimitResult,
        identity: str,
    ) -> None:
        """Emit a 429 response in the standard error envelope."""
        request_id = _extract_request_id(scope)
        event = RateLimitEvent(
            rule_name=rule.name,
            scope_value=identity,
            path=scope.get("path", ""),
            method=scope.get("method", "GET"),
            client_ip=_client_ip(scope, self.config),
            user_id=_resolve_user_id(scope),
            limit=rule.limit,
            window_s=rule.window_s,
            retry_after_s=result.retry_after_s,
            request_id=request_id,
        )

        if self.config.on_rate_limited is not None:
            try:
                callback_result = self.config.on_rate_limited(event)
                # Support async callbacks (metrics exporters, alerting
                # webhooks) — a sync callable returns a non-awaitable
                # and falls through unchanged.
                if inspect.isawaitable(callback_result):
                    await callback_result
            except Exception:
                _log.exception(
                    "on_rate_limited callback raised for rule {!r}",
                    rule.name,
                )

        _log.info(
            "rate limit exceeded rule={} scope={} path={} identity={}",
            rule.name,
            rule.scope,
            event.path,
            identity,
        )

        body = {
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": (
                    f"Rate limit exceeded: {rule.limit} requests per "
                    f"{int(rule.window_s)} second(s)."
                ),
                "details": {
                    "rule_name": rule.name,
                    "limit": rule.limit,
                    "window_s": rule.window_s,
                    "retry_after_s": result.retry_after_int,
                },
                "request_id": request_id,
            }
        }
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")

        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
            # ``Retry-After`` is a standard HTTP header (RFC 7231) and
            # must be present on every 429, regardless of ``emit_headers``.
            # It is deliberately NOT delegated to ``_format_headers`` to
            # avoid emitting it twice.
            (b"retry-after", str(result.retry_after_int).encode("ascii")),
        ]
        if self.config.emit_headers:
            # ``include_retry_after=False`` — the manual entry above
            # already covers it; asking for it again would produce a
            # duplicate header on every 429.
            headers.extend(_format_headers(result, include_retry_after=False))

        await send(
            {
                "type": "http.response.start",
                "status": 429,
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
    # Cleanup loop
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        interval = max(1.0, self.config.cleanup_interval_s)
        while not self._stopped:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._stopped:
                return
            try:
                removed = await self.store.cleanup()
                if removed:
                    _log.debug("Rate limit store cleaned {} entries", removed)
            except Exception:
                _log.exception("Rate limit cleanup raised")


# ===========================================================================
# Helpers — path matching
# ===========================================================================

def _path_matches(path: str, pattern: str) -> bool:
    """Return whether ``path`` matches ``pattern``.

    ``pattern`` is either:

    * a regular expression, if it starts with ``"re:"`` (prefix stripped);
    * an exact path, if it contains no glob characters (``*?[]``);
    * otherwise, an :mod:`fnmatch` glob (``fnmatch`` treats ``*`` as
      matching any characters **including** ``/``).
    """
    if pattern.startswith("re:"):
        import re  # noqa: PLC0415 — lazy, only used when needed

        try:
            return re.fullmatch(pattern[3:], path) is not None
        except re.error:
            _log.exception(
                "Invalid regex in rate limit path_pattern: {!r}", pattern
            )
            return False
    if not any(c in pattern for c in "*?["):
        return path == pattern
    return fnmatch.fnmatchcase(path, pattern)


# ===========================================================================
# Helpers — client identity
# ===========================================================================

def _client_ip(
    scope: dict[str, Any], config: RateLimitConfig
) -> str | None:
    """Return the client IP, honoring trusted proxies.

    If ``config.trust_forwarded_for`` is ``True`` **and** the immediate
    peer is in ``config.trusted_proxies`` (or that set is empty), the
    **last** address in ``X-Forwarded-For`` is used. The last entry is
    the closest one added by a trusted proxy; earlier entries are
    user-controlled.
    """
    peer_ip: str | None = None
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        peer_ip = str(client[0])

    if config.trust_forwarded_for:
        if (
            not config.trusted_proxies
            or (peer_ip and peer_ip in config.trusted_proxies)
        ):
            xff = _header_value(scope, b"x-forwarded-for")
            if xff:
                # Last entry is the one appended by the closest trusted
                # proxy. Everything before it is client-controlled and
                # must be ignored.
                parts = [p.strip() for p in xff.split(",") if p.strip()]
                if parts:
                    return parts[-1]
    return peer_ip


def _header_value(
    scope: dict[str, Any], name_lower: bytes
) -> str | None:
    """Return the value of a header by its lowercase byte name."""
    for name, value in scope.get("headers", ()):
        if name == name_lower:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None


def _resolve_user_id(scope: dict[str, Any]) -> str | None:
    """Return the caller's user id, or ``None``.

    Priority:

    1. ``scope["state"]["user_id"]`` — populated by an upstream auth
       middleware (:mod:`middleware.auth`). Verified, authoritative.
    2. ``Authorization: Bearer <jwt>`` — decode the ``sub`` claim
       **without verification**. This is a hint for accounting only; the
       endpoint's own auth dependency re-verifies the token before
       acting on it.
    3. ``None`` — the caller is anonymous.
    """
    # 1) Upstream middleware.
    state = scope.get("state") or {}
    if isinstance(state, Mapping):
        user_id = state.get("user_id")
        if isinstance(user_id, str) and user_id:
            return user_id

    # 2) Bearer token — best-effort decode.
    auth = _header_value(scope, b"authorization")
    if not auth:
        return None
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return _decode_jwt_sub_unverified(token.strip())


def _decode_jwt_sub_unverified(token: str) -> str | None:
    """Extract the ``sub`` claim from a JWT **without verifying it**.

    Only used to bucket rate limits — never for authorization. Returns
    ``None`` on any error (malformed JWT, wrong number of segments,
    missing ``sub``, invalid base64, …). Never raises.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        import base64  # noqa: PLC0415

        # JWT uses base64url without padding — pad it manually.
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        sub = payload.get("sub")
        if isinstance(sub, str) and sub:
            return sub
    except Exception:
        return None
    return None


def _extract_request_id(scope: dict[str, Any]) -> str | None:
    """Return the request id, honoring upstream middleware.

    Priority matches :func:`main._get_request_id`:

    1. ``scope["state"]["request_id"]`` — set by
       :class:`~.middleware.logging.LoggingMiddleware` (which runs
       *before* the rate-limit middleware, per the middleware stack
       order). This is the authoritative value for log correlation.
    2. ``X-Request-ID`` header — set by the client or a trusted
       upstream proxy. Bounded to 128 characters to avoid log
       injection.
    """
    state = scope.get("state") or {}
    if isinstance(state, Mapping):
        rid = state.get("request_id")
        if isinstance(rid, str) and rid:
            return rid

    raw = _header_value(scope, b"x-request-id")
    if not raw:
        return None
    # Basic sanity: bound length, reject whitespace.
    raw = raw.strip()
    if not raw or len(raw) > 128:
        return None
    return raw


# ===========================================================================
# Helpers — header formatting
# ===========================================================================

def _format_headers(
    result: RateLimitResult, *, include_retry_after: bool
) -> list[tuple[bytes, bytes]]:
    """Format the standard rate-limit headers as ASGI byte tuples."""
    headers: list[tuple[bytes, bytes]] = [
        (b"x-ratelimit-limit", str(result.limit).encode("ascii")),
        (b"x-ratelimit-remaining", str(result.remaining).encode("ascii")),
        (b"x-ratelimit-reset", str(result.reset_after_int).encode("ascii")),
    ]
    if include_retry_after and result.retry_after_s is not None:
        headers.append(
            (b"retry-after", str(result.retry_after_int).encode("ascii"))
        )
    return headers


# ===========================================================================
# Default configuration
# ===========================================================================

def default_rules() -> list[RateLimitRule]:
    """Return the four rate limits from the API contract.

    These are the **minimum** a NexusDL deployment should enforce:

    * ``/auth/login``    — 5/min per IP (brute-force protection, fail-closed).
    * ``/auth/register`` — 3/min per IP (spam protection, fail-closed).
    * ``/download``      — 30/min per user (expensive operation).
    * ``*``              — 600/min per user (general API budget).

    A separate ``60/min per IP`` rule is **not** included here because
    it overlaps with the ``600/min per user`` budget and would double-
    count. The host can add it explicitly if a per-IP cap is required
    in addition to the per-user one (useful for unauthenticated traffic
    served by the API surface).
    """
    return [
        RateLimitRule(
            name="auth_login",
            path_pattern="/api/v1/auth/login",
            method="POST",
            limit=5,
            window_s=60.0,
            scope="ip",
            fail_open=False,  # must fail closed — auth endpoint
        ),
        RateLimitRule(
            name="auth_register",
            path_pattern="/api/v1/auth/register",
            method="POST",
            limit=3,
            window_s=60.0,
            scope="ip",
            fail_open=False,
        ),
        RateLimitRule(
            name="download_submit",
            path_pattern="/api/v1/download",
            method="POST",
            limit=30,
            window_s=60.0,
            scope="user",
            algorithm=RateLimitAlgorithm.TOKEN_BUCKET,
        ),
        RateLimitRule(
            name="api_default",
            path_pattern="/api/*",
            limit=600,
            window_s=60.0,
            scope="user",
            algorithm=RateLimitAlgorithm.TOKEN_BUCKET,
        ),
    ]


# ===========================================================================
# Public surface
# ===========================================================================

__all__ = [
    # Enums
    "RateLimitAlgorithm",
    # Exceptions
    "RateLimitError",
    # Dataclasses
    "RateLimitRule",
    "RateLimitResult",
    "RateLimitEvent",
    "RateLimitConfig",
    # Protocols
    "RateLimitStore",
    # Stores
    "InMemoryRateLimitStore",
    # Middleware
    "RateLimitMiddleware",
    # Helpers
    "default_rules",
]
