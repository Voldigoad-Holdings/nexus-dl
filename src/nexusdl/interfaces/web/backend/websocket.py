"""WebSocket engine for the NexusDL FastAPI backend.

Framework-agnostic, headless-safe connection manager powering
``/ws/v1/events``. This module intentionally avoids importing FastAPI,
Starlette, or Pydantic — the transport is described by the
:class:`WebSocketLike` protocol, and the endpoint
(:mod:`nexusdl.interfaces.web.backend.routers.ws`) is the only
FastAPI-aware layer.

Responsibilities
================

* **Connection registry** — async-lock-protected mapping of ``id`` to
  :class:`Connection`.
* **Per-connection send queues** — decouple broadcasters from slow
  clients (backpressure = bounded queue + controlled disconnect).
* **Per-connection subscriptions** — clients opt into event types, with
  glob support (``"download.*"``) and a ``"*"`` catch-all. System event
  types (``ping``, ``pong``, ``error``, ``hello``) bypass subscriptions.
* **Broadcast filters** — ``roles``, ``user_ids``, ``connection_ids`` and
  a custom ``predicate`` are AND-ed. ``force=True`` bypasses the
  subscription check but not the other filters.
* **Heartbeat** — a single manager-level task scans, pings, and reaps
  dead connections at a fixed interval. No per-connection task
  explosion.
* **EventBus bridge** — :class:`BusBridge` wires application events to
  WS broadcasts declaratively (``bus_type → ws_type`` mapping, optional
  role gate, optional payload transform).
* **Introspection** — :meth:`ConnectionManager.stats` for observability.

Wire format
===========

Server → client::

    {
      "id": "<uuid>",
      "type": "<event type>",
      "ts": "<ISO 8601 UTC>",
      "data": { ... }
    }

Errors::

    {
      "id": "<uuid>",
      "type": "error",
      "ts": "<ISO 8601 UTC>",
      "error": {
        "code": "<SCREAMING_SNAKE_CASE>",
        "message": "<human readable>",
        "details": { ... },
        "request_id": "<uuid or null>"
      }
    }

Client → server (handled by :meth:`ConnectionManager.handle_message`)::

    {"type": "subscribe", "data": {"event_types": ["download.*", "library.updated"]}}
    {"type": "unsubscribe", "data": {"event_types": ["download.progress"]}}
    {"type": "ping"}
    {"type": "pong"}

Custom message types can be routed via
:meth:`ConnectionManager.register_handler`. Unknown types produce an
:class:`WSError` on the client side without disconnecting.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Protocol, runtime_checkable

from loguru import logger as _logger

from nexusdl.core.models.enums import UserRole

_log = _logger.bind(component="web.backend.websocket")


# ===========================================================================
# Public constants
# ===========================================================================

#: Event types that are always delivered regardless of the client's
#: subscriptions — clients must always receive their own pongs and errors.
SYSTEM_EVENT_TYPES: frozenset[str] = frozenset(
    {"ping", "pong", "error", "hello"}
)

#: Default per-connection queue size (number of buffered outgoing messages).
DEFAULT_QUEUE_SIZE: int = 256

#: Default heartbeat configuration.
DEFAULT_HEARTBEAT_INTERVAL_S: float = 25.0
DEFAULT_HEARTBEAT_TIMEOUT_S: float = 60.0

#: Default maximum concurrent connections (safety valve).
DEFAULT_MAX_CONNECTIONS: int = 2000


#: Total ordering of roles, from least to most privileged. Used to
#: implement "minimum role" gating in :class:`BusBridgeConfig`.
_ROLE_RANK: dict[UserRole, int] = {
    UserRole.GUEST: 0,
    UserRole.USER: 1,
    UserRole.ADMIN: 2,
}


# ===========================================================================
# Public enums
# ===========================================================================

class WSEventType(str, Enum):
    """Well-known WS event types.

    The enum is **optional** — :class:`WSMessage` accepts any string as
    ``type``, so a plugin or future feature can push a new event without
    touching this enum. It exists for autocompletion and to document the
    protocol.
    """

    # Download lifecycle
    DOWNLOAD_STARTED = "download.started"
    DOWNLOAD_PROGRESS = "download.progress"
    DOWNLOAD_COMPLETED = "download.completed"
    DOWNLOAD_FAILED = "download.failed"
    DOWNLOAD_PAUSED = "download.paused"
    DOWNLOAD_RESUMED = "download.resumed"
    DOWNLOAD_CANCELLED = "download.cancelled"

    # Chapter lifecycle
    CHAPTER_STARTED = "chapter.started"
    CHAPTER_COMPLETED = "chapter.completed"
    CHAPTER_FAILED = "chapter.failed"

    # Application-wide
    LIBRARY_UPDATED = "library.updated"
    SITE_HEALTH_CHANGED = "site.health.changed"

    # Protocol-level
    HELLO = "hello"
    PING = "ping"
    PONG = "pong"
    ERROR = "error"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"


class WSCloseCode(IntEnum):
    """WebSocket close codes, including NexusDL application codes.

    Standard codes (RFC 6455) use the 1000-4999 range; custom application
    codes live in 4000-4999. Clients should treat any non-1000 code as
    abnormal but reconnectable (except 4003/4004, which mean "back off").
    """

    # RFC 6455
    NORMAL = 1000
    GOING_AWAY = 1001
    PROTOCOL_ERROR = 1002
    UNSUPPORTED_DATA = 1003
    POLICY_VIOLATION = 1008
    MESSAGE_TOO_BIG = 1009
    INTERNAL_ERROR = 1011
    SERVICE_RESTART = 1012
    TRY_AGAIN_LATER = 1013

    # NexusDL application codes
    AUTH_FAILED = 4000
    HEARTBEAT_TIMEOUT = 4001
    SLOW_CONSUMER = 4002
    TOO_MANY_CONNECTIONS = 4003
    FORBIDDEN = 4004


# ===========================================================================
# Public exceptions
# ===========================================================================

class WSError(Exception):
    """Base exception for WebSocket errors."""


class TooManyConnectionsError(WSError):
    """Raised by :meth:`ConnectionManager.connect` when the server is at
    capacity. The connection is closed with
    :attr:`WSCloseCode.TRY_AGAIN_LATER` before this exception is raised.
    """


class ConnectionClosedError(WSError):
    """Raised when trying to operate on a closed connection."""


# ===========================================================================
# Public protocols
# ===========================================================================

@runtime_checkable
class WebSocketLike(Protocol):
    """Structural protocol matching :class:`fastapi.WebSocket` /
    :class:`starlette.websockets.WebSocket` and most ASGI WebSocket
    implementations.

    Any object implementing these four coroutines can drive a
    :class:`Connection`. Tests use a :class:`FakeWebSocket` with the same
    shape.
    """

    async def accept(self, subprotocol: str | None = None) -> None: ...

    async def send_json(self, data: Any) -> None: ...

    async def receive_json(self) -> Any: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


# ===========================================================================
# Public dataclasses
# ===========================================================================

@dataclass(frozen=True, slots=True)
class AuthContext:
    """Authentication context attached to a connection.

    The endpoint (:mod:`routers.ws`) decodes the JWT and builds this via
    :meth:`from_claims`. :class:`ConnectionManager` never touches JWTs.
    """

    user_id: str | None = None
    role: UserRole = UserRole.GUEST
    username: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def guest(cls) -> AuthContext:
        """Return an anonymous context."""
        return cls()

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> AuthContext:
        """Build a context from decoded JWT claims.

        Missing / malformed fields fall back to guest. Never raises — the
        endpoint decides whether a missing identity is acceptable.

        Recognized claims: ``sub`` (user id), ``username``, ``role``.
        """
        raw_role = claims.get("role", "guest")
        try:
            role = UserRole(str(raw_role))
        except (ValueError, TypeError):
            role = UserRole.GUEST
        return cls(
            user_id=str(claims["sub"]) if "sub" in claims else None,
            role=role,
            username=claims.get("username"),
            extra={k: v for k, v in claims.items()
                   if k not in ("sub", "username", "role")},
        )


@dataclass(slots=True)
class WSMessage:
    """A server-to-client message.

    Both :class:`WSMessage` and :class:`WSError` implement ``to_dict()``
    returning a JSON-ready mapping — the send loop is agnostic between
    the two.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "ts": self.ts.isoformat(),
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> WSMessage:
        ts_raw = raw.get("ts")
        ts: datetime
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        elif isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            ts = datetime.now(timezone.utc)

        data = raw.get("data", {})
        # Defensive: preserve a caller-supplied id only if it is a
        # non-empty value. ``str(None)`` would produce the literal
        # ``"None"`` — avoid that trap.
        raw_id = raw.get("id")
        msg_id = str(raw_id) if raw_id else str(uuid.uuid4())
        return cls(
            type=str(raw.get("type", "")),
            data=dict(data) if isinstance(data, Mapping) else {},
            ts=ts,
            id=msg_id,
        )


@dataclass(slots=True)
class WSErrorPayload:
    """A server-to-client error message in the standard envelope."""

    code: str
    message: str
    details: dict[str, Any] | None = None
    request_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": WSEventType.ERROR.value,
            "ts": self.ts.isoformat(),
            "error": {
                "code": self.code,
                "message": self.message,
                "details": dict(self.details) if self.details else {},
                "request_id": self.request_id,
            },
        }


@dataclass(frozen=True, slots=True)
class ConnectionManagerConfig:
    """Tunables for :class:`ConnectionManager`.

    Attributes:
        queue_size: Max buffered outgoing messages per connection.
        heartbeat_interval_s: How often the manager pings all connections.
        heartbeat_timeout_s: How long without a pong before reaping.
        max_connections: Server-wide connection cap. New connections above
            the cap are rejected with
            :attr:`WSCloseCode.TRY_AGAIN_LATER`.
    """

    queue_size: int = DEFAULT_QUEUE_SIZE
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S
    max_connections: int = DEFAULT_MAX_CONNECTIONS


@dataclass(slots=True)
class ManagerStats:
    """Snapshot returned by :meth:`ConnectionManager.stats`."""

    active_connections: int
    peak_connections: int
    total_connections: int
    messages_sent: int
    messages_dropped: int
    broadcasts: int
    errors: int
    uptime_s: float


# ===========================================================================
# Connection
# ===========================================================================

class Connection:
    """A live WebSocket connection owned by the manager.

    **Do not instantiate directly** — always go through
    :meth:`ConnectionManager.connect`. The manager owns the lifecycle
    (accept → send loop → heartbeat scan → close → deregister).
    """

    def __init__(
        self,
        websocket: WebSocketLike,
        auth: AuthContext,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self.id: str = str(uuid.uuid4())
        self.websocket = websocket
        self.auth = auth
        self.subscriptions: set[str] = set()
        self.created_at: datetime = datetime.now(timezone.utc)
        self.last_pong_at: datetime = self.created_at
        self.closed: bool = False

        self._close_initiated: bool = False
        self._queue_size = queue_size
        self._heartbeat_interval_s = heartbeat_interval_s
        self._send_queue: asyncio.Queue[WSMessage | WSErrorPayload] = asyncio.Queue(
            maxsize=queue_size
        )
        self._send_task: asyncio.Task[None] | None = None
        # Invoked after a *self-initiated* close (currently: slow-consumer
        # overflow) so the manager can remove us from the registry without
        # waiting for the next heartbeat scan. Idempotent on the manager
        # side (``disconnect``).
        self._reap_callback: Callable[[Connection], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Lifecycle (driven by the manager)
    # ------------------------------------------------------------------

    def set_reap_callback(
        self, cb: Callable[[Connection], Awaitable[None]]
    ) -> None:
        """Install the callback invoked after a self-initiated close."""
        self._reap_callback = cb

    async def accept(self, *, subprotocol: str | None = None) -> None:
        """Accept the WebSocket and start the send loop + hello message.

        ``subprotocol`` is forwarded to the underlying WebSocket. The
        send loop and hello message are **always** started here — even if
        the transport rejects the subprotocol kwarg (some ASGI servers
        only accept the bare ``accept()``).
        """
        if subprotocol is not None:
            try:
                await self.websocket.accept(subprotocol=subprotocol)
            except TypeError:
                # Some implementations don't accept the kwarg.
                await self.websocket.accept()
        else:
            await self.websocket.accept()

        self._send_task = asyncio.create_task(
            self._send_loop(), name=f"ws-send-{self.id[:8]}"
        )
        hello = WSMessage(
            type=WSEventType.HELLO.value,
            data={
                "connection_id": self.id,
                "heartbeat_interval_s": self._heartbeat_interval_s,
                "auth": {
                    "user_id": self.auth.user_id,
                    "role": self.auth.role.value,
                },
            },
        )
        self.enqueue(hello)

    async def close(
        self,
        code: WSCloseCode = WSCloseCode.NORMAL,
        reason: str = "",
    ) -> None:
        """Close the connection cleanly. Idempotent.

        Idempotence is guaranteed by the ``_close_initiated`` guard: the
        first caller owns the close, subsequent callers return
        immediately. This is important because the manager, the send
        loop, and the endpoint can all race to close a connection.
        """
        if self._close_initiated:
            return
        self._close_initiated = True
        self.closed = True

        if self._send_task is not None and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                # Distinguish the send task's cancellation (expected —
                # we just cancelled it above) from our own cancellation
                # (the caller of ``close()`` was cancelled). Re-raise
                # only in the latter case so asyncio cancellation
                # semantics are preserved.
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    raise
            except Exception:
                _log.exception("Send loop raised during close")

        try:
            await self.websocket.close(code=int(code), reason=reason)
        except Exception:
            _log.debug("websocket.close raised (likely already closed)")

    # ------------------------------------------------------------------
    # Enqueue / dispatch
    # ------------------------------------------------------------------

    def enqueue(self, message: WSMessage | WSErrorPayload) -> bool:
        """Queue ``message`` for sending. Returns ``False`` on overflow.

        On overflow (queue full), the connection is scheduled to close with
        :attr:`WSCloseCode.SLOW_CONSUMER`. This is intentional: a slow
        client must not be allowed to grow unbounded server memory.
        """
        if self.closed:
            return False
        try:
            self._send_queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            self._schedule_overflow_close()
            return False

    def _schedule_overflow_close(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop — mark closed and let the manager reap it on
            # the next heartbeat scan.
            self.closed = True
            return
        loop.create_task(
            self._overflow_close_task(),
            name=f"ws-overflow-{self.id[:8]}",
        )

    async def _overflow_close_task(self) -> None:
        """Close the connection and notify the manager for deregistration."""
        try:
            await self.close(WSCloseCode.SLOW_CONSUMER, "client too slow")
        finally:
            cb = self._reap_callback
            if cb is not None:
                try:
                    await cb(self)
                except Exception:
                    _log.exception("Reap callback raised during overflow")

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def matches_event(self, event_type: str) -> bool:
        """Return whether this client wants to receive ``event_type``.

        System types always match. Otherwise, matches:
        - exact subscription (``"download.started"``),
        - prefix wildcard (``"download.*"`` matches ``"download.started"``),
        - global wildcard (``"*"``).
        """
        if event_type in SYSTEM_EVENT_TYPES:
            return True
        if "*" in self.subscriptions:
            return True
        if event_type in self.subscriptions:
            return True
        for sub in self.subscriptions:
            if sub.endswith(".*") and event_type.startswith(sub[:-1]):
                return True
        return False

    # ------------------------------------------------------------------
    # Internal send loop
    # ------------------------------------------------------------------

    async def _send_loop(self) -> None:
        while not self.closed:
            try:
                msg = await self._send_queue.get()
            except asyncio.CancelledError:
                return
            except Exception:
                _log.exception("Send queue .get raised unexpectedly")
                return
            try:
                await self.websocket.send_json(msg.to_dict())
            except asyncio.CancelledError:
                return
            except Exception:
                # Transport died; mark closed, let the manager reap us.
                _log.debug("websocket.send_json raised; closing {}", self.id[:8])
                self.closed = True
                return


# ===========================================================================
# ConnectionManager
# ===========================================================================

Handler = Callable[[Connection, WSMessage], Awaitable[None]]


class ConnectionManager:
    """The engine behind ``/ws/v1/events``.

    Owns the connection registry, the heartbeat loop, and all broadcast /
    send operations. Framework-agnostic — see module docstring.

    Usage from the FastAPI router::

        @router.websocket("/ws/v1/events")
        async def ws_endpoint(ws: WebSocket, token: str = Query(...)):
            try:
                claims = decode_jwt(token)
            except Exception:
                await ws.close(code=WSCloseCode.AUTH_FAILED, reason="invalid")
                return
            conn = await manager.connect(ws, AuthContext.from_claims(claims))
            try:
                while True:
                    raw = await ws.receive_json()
                    await manager.handle_message(conn, raw)
            except WebSocketDisconnect:
                pass
            finally:
                await manager.disconnect(conn)
    """

    def __init__(
        self,
        *,
        config: ConnectionManagerConfig | None = None,
    ) -> None:
        self._config = config or ConnectionManagerConfig()
        if self._config.heartbeat_timeout_s <= self._config.heartbeat_interval_s:
            raise ValueError(
                "heartbeat_timeout_s must be > heartbeat_interval_s"
            )

        self._connections: dict[str, Connection] = {}
        self._lock = asyncio.Lock()
        self._handlers: dict[str, Handler] = {}
        self._default_handler: Handler | None = None

        self._started_at: datetime = datetime.now(timezone.utc)
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stopping: bool = False

        # Cumulative counters.
        self._total_connections: int = 0
        self._peak_connections: int = 0
        self._messages_sent: int = 0
        self._messages_dropped: int = 0
        self._broadcasts: int = 0
        self._errors: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the heartbeat loop. Idempotent."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._stopping = False
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="ws-heartbeat"
        )
        _log.info(
            "ConnectionManager started (heartbeat {:.1f}s, timeout {:.1f}s)",
            self._config.heartbeat_interval_s,
            self._config.heartbeat_timeout_s,
        )

    async def stop(self) -> None:
        """Stop accepting, close all connections, stop heartbeat. Idempotent."""
        self._stopping = True
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.exception("Heartbeat loop raised during stop")
            finally:
                self._heartbeat_task = None
        await self.close_all(
            WSCloseCode.SERVICE_RESTART, "server shutting down"
        )
        _log.info("ConnectionManager stopped")

    # ------------------------------------------------------------------
    # Connect / disconnect
    # ------------------------------------------------------------------

    async def connect(
        self,
        websocket: WebSocketLike,
        auth: AuthContext | None = None,
        *,
        subscriptions: Iterable[str] | None = None,
        subprotocol: str | None = None,
    ) -> Connection:
        """Register a new connection and start its send loop.

        Raises:
            TooManyConnectionsError: If the server is at capacity. The
                WebSocket is closed with
                :attr:`WSCloseCode.TRY_AGAIN_LATER` before the exception.
        """
        auth = auth or AuthContext.guest()

        # 1) Reserve a slot in the registry (under lock).
        async with self._lock:
            if len(self._connections) >= self._config.max_connections:
                at_capacity = True
                conn: Connection | None = None
            else:
                at_capacity = False
                conn = Connection(
                    websocket,
                    auth,
                    queue_size=self._config.queue_size,
                    heartbeat_interval_s=self._config.heartbeat_interval_s,
                )
                conn.set_reap_callback(self.disconnect)
                if subscriptions is not None:
                    conn.subscriptions = set(subscriptions)
                self._connections[conn.id] = conn
                self._total_connections += 1
                if len(self._connections) > self._peak_connections:
                    self._peak_connections = len(self._connections)

        if at_capacity or conn is None:
            try:
                await websocket.close(
                    code=int(WSCloseCode.TRY_AGAIN_LATER),
                    reason="server at capacity",
                )
            except Exception:
                pass
            raise TooManyConnectionsError(
                f"connection limit reached ({self._config.max_connections})"
            )

        # 2) Accept + start the send loop (outside the lock).
        #
        # CRITICAL: always call ``conn.accept(...)`` — never call
        # ``websocket.accept(...)`` directly here — otherwise the send
        # loop and hello message are never started, and the client can
        # never receive anything.
        try:
            await conn.accept(subprotocol=subprotocol)
        except Exception:
            _log.exception("Failed to accept WebSocket {}", conn.id[:8])
            async with self._lock:
                self._connections.pop(conn.id, None)
            raise

        _log.debug(
            "WS connected id={} user={} role={} total={}",
            conn.id[:8],
            auth.user_id,
            auth.role.value,
            len(self._connections),
        )
        return conn

    async def disconnect(
        self,
        connection: Connection,
        *,
        code: WSCloseCode = WSCloseCode.NORMAL,
        reason: str = "server disconnect",
    ) -> None:
        """Deregister and close ``connection``. Idempotent.

        The ``code`` / ``reason`` parameters are only used when this call
        is the one that actually closes the connection (i.e. the
        connection was not already closed by the send loop, an overflow,
        or a concurrent caller).
        """
        async with self._lock:
            existed = self._connections.pop(connection.id, None) is not None
        if existed:
            _log.debug(
                "WS disconnected id={} total={}",
                connection.id[:8],
                len(self._connections),
            )
        if not connection.closed:
            try:
                await connection.close(code, reason)
            except Exception:
                _log.exception("Connection.close raised during disconnect")

    # ------------------------------------------------------------------
    # Inbound messages
    # ------------------------------------------------------------------

    def register_handler(self, message_type: str, handler: Handler) -> None:
        """Register a custom handler for ``message_type``.

        The handler is called with ``(connection, parsed_message)``.
        Exceptions are logged and an :class:`WSErrorPayload` is sent to the
        client; the connection stays open.
        """
        self._handlers[message_type] = handler

    def set_default_handler(self, handler: Handler) -> None:
        """Handler called for unknown message types (instead of an error)."""
        self._default_handler = handler

    async def handle_message(
        self, connection: Connection, raw: Any
    ) -> None:
        """Route an incoming client message.

        Supported native types: ``ping``, ``pong``, ``subscribe``,
        ``unsubscribe``. Custom types go through registered handlers.
        Anything else produces a WSError on the client (no disconnect).
        """
        if connection.closed:
            return

        if not isinstance(raw, Mapping):
            await self._send_error(
                connection,
                code="INVALID_MESSAGE",
                message="Message must be a JSON object",
            )
            return

        msg_type = str(raw.get("type", ""))
        data = raw.get("data", {})

        # --- Protocol natives ---
        if msg_type == WSEventType.PING.value:
            connection.enqueue(WSMessage(type=WSEventType.PONG.value))
            return

        if msg_type == WSEventType.PONG.value:
            connection.last_pong_at = datetime.now(timezone.utc)
            return

        if msg_type == WSEventType.SUBSCRIBE.value:
            types = data.get("event_types") if isinstance(data, Mapping) else None
            if isinstance(types, list):
                connection.subscriptions.update(str(t) for t in types)
            return

        if msg_type == WSEventType.UNSUBSCRIBE.value:
            types = data.get("event_types") if isinstance(data, Mapping) else None
            if isinstance(types, list):
                connection.subscriptions.difference_update(str(t) for t in types)
            return

        # --- Custom handlers ---
        handler = self._handlers.get(msg_type)
        if handler is None and self._default_handler is not None:
            handler = self._default_handler
        if handler is not None:
            try:
                await handler(connection, WSMessage.from_dict(raw))
            except Exception as exc:
                self._errors += 1
                _log.exception("WS handler for {!r} raised", msg_type)
                await self._send_error(
                    connection,
                    code="HANDLER_ERROR",
                    message=f"Handler for {msg_type!r} failed: {exc}",
                )
            return

        # --- Unknown ---
        await self._send_error(
            connection,
            code="UNKNOWN_MESSAGE_TYPE",
            message=f"Unknown message type: {msg_type!r}",
        )

    async def _send_error(
        self,
        connection: Connection,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        self._errors += 1
        connection.enqueue(
            WSErrorPayload(
                code=code,
                message=message,
                details=details,
                request_id=request_id,
            )
        )

    # ------------------------------------------------------------------
    # Broadcast / send
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        message: WSMessage,
        *,
        user_ids: Iterable[str] | None = None,
        roles: Iterable[UserRole] | None = None,
        connection_ids: Iterable[str] | None = None,
        predicate: Callable[[Connection], bool] | None = None,
        force: bool = False,
    ) -> int:
        """Broadcast ``message`` to matching connections.

        Filters are AND-ed:

        * ``user_ids`` — only connections authenticated as one of these.
        * ``roles`` — only connections with one of these roles.
        * ``connection_ids`` — only these connection ids.
        * ``predicate`` — custom filter.
        * ``force`` — if ``False`` (default), the client's subscription
          must match ``message.type``. ``True`` bypasses the subscription
          check (useful for admin-pushed alerts).

        Returns:
            Number of connections the message was enqueued to.
        """
        target_user_ids = set(user_ids) if user_ids is not None else None
        target_roles = set(roles) if roles is not None else None
        target_conn_ids = (
            set(connection_ids) if connection_ids is not None else None
        )

        async with self._lock:
            snapshot = list(self._connections.values())

        sent = 0
        dropped = 0
        for conn in snapshot:
            if conn.closed:
                continue
            if not force and not conn.matches_event(message.type):
                continue
            if target_user_ids is not None and conn.auth.user_id not in target_user_ids:
                continue
            if target_roles is not None and conn.auth.role not in target_roles:
                continue
            if target_conn_ids is not None and conn.id not in target_conn_ids:
                continue
            if predicate is not None:
                try:
                    if not predicate(conn):
                        continue
                except Exception:
                    _log.exception("broadcast predicate raised; skipping conn")
                    continue
            if conn.enqueue(message):
                sent += 1
            else:
                dropped += 1

        self._messages_sent += sent
        self._messages_dropped += dropped
        self._broadcasts += 1
        return sent

    async def send_to_user(self, user_id: str, message: WSMessage) -> int:
        """Send ``message`` to every connection authenticated as ``user_id``.

        Respects the client's subscription (like :meth:`broadcast` with
        ``force=False``). For an imperative "always deliver" semantic use
        ``broadcast(message, user_ids=[user_id], force=True)``.
        """
        return await self.broadcast(message, user_ids=[user_id])

    async def send_to_connection(
        self, connection_id: str, message: WSMessage
    ) -> bool:
        """Send ``message`` to exactly one connection. Returns delivery bool."""
        async with self._lock:
            conn = self._connections.get(connection_id)
        if conn is None or conn.closed:
            return False
        if not conn.matches_event(message.type):
            return False
        ok = conn.enqueue(message)
        if ok:
            self._messages_sent += 1
        else:
            self._messages_dropped += 1
        return ok

    async def close_all(
        self, code: WSCloseCode = WSCloseCode.NORMAL, reason: str = ""
    ) -> None:
        """Close all connections gracefully."""
        async with self._lock:
            snapshot = list(self._connections.values())
            self._connections.clear()
        # Fire all closes concurrently.
        await asyncio.gather(
            *(c.close(code, reason) for c in snapshot),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def get_connection(self, connection_id: str) -> Connection | None:
        async with self._lock:
            return self._connections.get(connection_id)

    async def list_connections(self) -> list[Connection]:
        async with self._lock:
            return list(self._connections.values())

    def stats(self) -> ManagerStats:
        """Return a snapshot of the manager's counters."""
        return ManagerStats(
            active_connections=len(self._connections),
            peak_connections=self._peak_connections,
            total_connections=self._total_connections,
            messages_sent=self._messages_sent,
            messages_dropped=self._messages_dropped,
            broadcasts=self._broadcasts,
            errors=self._errors,
            uptime_s=(datetime.now(timezone.utc) - self._started_at).total_seconds(),
        )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        interval = self._config.heartbeat_interval_s
        timeout = self._config.heartbeat_timeout_s
        while not self._stopping:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self._stopping:
                return

            now = datetime.now(timezone.utc)
            async with self._lock:
                snapshot = list(self._connections.values())

            reaped = 0
            for conn in snapshot:
                if conn.closed:
                    # Self-closed by the send loop (transport error) or
                    # by an overflow task that raced us. Reap from the
                    # registry — otherwise the slot leaks and eventually
                    # saturates ``max_connections``.
                    reaped += 1
                    asyncio.create_task(
                        self.disconnect(conn),
                        name=f"ws-reap-{conn.id[:8]}",
                    )
                    continue
                elapsed = (now - conn.last_pong_at).total_seconds()
                if elapsed > timeout:
                    reaped += 1
                    # Schedule the close + deregistration; do not block
                    # the heartbeat scan.
                    asyncio.create_task(
                        self.disconnect(
                            conn,
                            code=WSCloseCode.HEARTBEAT_TIMEOUT,
                            reason="heartbeat timeout",
                        ),
                        name=f"ws-hb-{conn.id[:8]}",
                    )
                    continue
                conn.enqueue(WSMessage(type=WSEventType.PING.value))

            if reaped:
                _log.debug("Heartbeat reaped {} connection(s)", reaped)


# ===========================================================================
# EventBus bridge
# ===========================================================================

@dataclass(frozen=True, slots=True)
class BusBridgeConfig:
    """Declarative description of how to translate EventBus events to WS.

    Attributes:
        event_map: Mapping of ``event_bus_type -> ws_message_type``. Adding
            a new event to broadcast is a single entry.
        require_role: Optional per-``ws_type`` **minimum** role — only
            connections with **at least** this role receive the message.
            Used for admin-only streams. The comparison uses
            :data:`_ROLE_RANK`, so ``USER`` implies ``USER`` or ``ADMIN``
            and ``ADMIN`` implies ``ADMIN`` only.
        transform: Optional per-``event_bus_type`` function mapping the
            raw payload to the WS ``data`` dict. When absent, the raw
            payload is used as-is (after being coerced to ``dict``).
            Return ``None`` from ``transform`` to **drop** the event.
    """

    event_map: Mapping[str, str]
    require_role: Mapping[str, UserRole] = field(default_factory=dict)
    transform: Mapping[str, Callable[[Any], dict[str, Any] | None]] = field(
        default_factory=dict
    )


class BusBridge:
    """Wire application :class:`EventBus` events to WS broadcasts.

    Encapsulates the subscription lifecycle so the host only has to call
    :meth:`start` and :meth:`stop`. All EventBus handlers are registered
    here and unregistered on stop.
    """

    def __init__(
        self,
        manager: ConnectionManager,
        event_bus: Any,
        config: BusBridgeConfig,
    ) -> None:
        self._manager = manager
        self._bus = event_bus
        self._config = config
        self._handles: list[tuple[str, Any]] = []

    def start(self) -> None:
        """Subscribe to every event in ``config.event_map``. Idempotent."""
        if self._handles:
            return
        for bus_type, ws_type in self._config.event_map.items():
            handler = self._make_handler(bus_type, ws_type)
            try:
                handle = self._bus.subscribe(bus_type, handler)
            except Exception:
                _log.exception("Failed to subscribe to bus event {!r}", bus_type)
                continue
            self._handles.append((bus_type, (handler, handle)))
        _log.info("BusBridge started with {} mapping(s)", len(self._handles))

    def stop(self) -> None:
        """Unsubscribe from all events. Idempotent."""
        for bus_type, (handler, handle) in self._handles:
            unsubscribe = getattr(self._bus, "unsubscribe", None)
            if unsubscribe is None:
                continue
            # Try the handle form first, but only if the bus actually
            # returned a handle (``subscribe`` returns ``None`` on the
            # default EventBus, in which case ``unsubscribe(None)`` is
            # meaningless). Whatever the outcome — success, wrong
            # signature, or error — fall through to the two-argument
            # form as a last resort.
            unsubscribed = False
            if handle is not None:
                try:
                    unsubscribe(handle)
                    unsubscribed = True
                except TypeError:
                    # Wrong signature — fall through.
                    pass
                except Exception:
                    _log.debug(
                        "BusBridge unsubscribe(handle) failed for {!r}",
                        bus_type,
                    )
            if not unsubscribed:
                try:
                    unsubscribe(bus_type, handler)
                except Exception:
                    _log.debug(
                        "BusBridge unsubscribe(event_type, handler) "
                        "failed for {!r}",
                        bus_type,
                    )
        self._handles.clear()
        _log.info("BusBridge stopped")

    # ------------------------------------------------------------------

    def _make_handler(
        self, bus_type: str, ws_type: str
    ) -> Callable[[Any], Awaitable[None]]:
        require_role = self._config.require_role.get(ws_type)
        transform = self._config.transform.get(bus_type)

        async def handler(event: Any) -> None:
            raw_payload = getattr(event, "payload", event)
            if transform is not None:
                try:
                    data = transform(raw_payload)
                except Exception:
                    _log.exception(
                        "BusBridge transform raised for {!r}", bus_type
                    )
                    return
                if data is None:
                    return
            elif isinstance(raw_payload, Mapping):
                data = dict(raw_payload)
            else:
                data = {"value": raw_payload}

            message = WSMessage(type=ws_type, data=data)
            try:
                if require_role is None:
                    await self._manager.broadcast(message)
                else:
                    min_rank = _ROLE_RANK.get(require_role, 0)
                    # ``require_role`` is documented as a *minimum*, so
                    # accept every role ranked at or above it. Using
                    # ``broadcast(roles=...)`` would do exact matching and
                    # silently drop e.g. ADMIN recipients for a
                    # ``require_role=USER`` stream.
                    def _role_ok(c: Connection) -> bool:
                        return _ROLE_RANK.get(c.auth.role, 0) >= min_rank
                    await self._manager.broadcast(message, predicate=_role_ok)
            except Exception:
                _log.exception(
                    "BusBridge broadcast failed for {!r} -> {!r}",
                    bus_type,
                    ws_type,
                )

        return handler


# ===========================================================================
# Default configuration factory
# ===========================================================================

def default_bus_bridge_config() -> BusBridgeConfig:
    """Return the standard EventBus → WS mapping used by NexusDL.

    Covers every event listed in the API contract. Hosts can extend by
    constructing their own :class:`BusBridgeConfig` (this is a helper, not
    a closed set).
    """
    return BusBridgeConfig(
        event_map={
            # Downloads
            "download.started": WSEventType.DOWNLOAD_STARTED.value,
            "download.progress": WSEventType.DOWNLOAD_PROGRESS.value,
            "download.completed": WSEventType.DOWNLOAD_COMPLETED.value,
            "download.failed": WSEventType.DOWNLOAD_FAILED.value,
            "download.paused": WSEventType.DOWNLOAD_PAUSED.value,
            "download.resumed": WSEventType.DOWNLOAD_RESUMED.value,
            "download.cancelled": WSEventType.DOWNLOAD_CANCELLED.value,
            # Chapters
            "chapter.started": WSEventType.CHAPTER_STARTED.value,
            "chapter.completed": WSEventType.CHAPTER_COMPLETED.value,
            "chapter.failed": WSEventType.CHAPTER_FAILED.value,
            # App-wide
            "library.updated": WSEventType.LIBRARY_UPDATED.value,
            "site.health.changed": WSEventType.SITE_HEALTH_CHANGED.value,
        },
        # No role gate by default — every authenticated user sees the
        # standard streams. Hosts that push admin-only events can add
        # entries to ``require_role`` in their own config.
    )
