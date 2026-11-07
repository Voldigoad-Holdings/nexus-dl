"""WebSocket message schemas for the NexusDL backend.

Pydantic v2 counterpart of :mod:`nexusdl.interfaces.web.backend.websocket`.
This module is the **router-facing boundary**: it validates incoming client
messages and documents / serializes outgoing server messages.

Dependency direction is strictly **schemas → websocket**. The
:mod:`websocket` module never imports Pydantic — this keeps it testable
without the framework and lets non-FastAPI transports reuse it.

Design notes
============

* **Extensible `type: str`.** Event envelopes accept any ``str`` for
  ``type``; :class:`WSEventType` is exposed for autocompletion and
  documentation but is *not* enforced. A plugin can broadcast
  ``plugin.event.x`` without any change here.
* **Two envelope families.**
  - :class:`ServerMessage` wraps ``data: dict`` — used for events,
    ``hello``, ``ping``, ``pong``.
  - :class:`ServerErrorMessage` wraps ``error: ServerErrorBody`` — used
    exclusively for ``type == "error"``. Clients discriminate on
    ``type == "error"``.
* **Extra policies.** Envelopes use ``extra="forbid"`` (strict protocol).
  The ``data`` payloads use ``extra="allow"`` so plugins and forward-
  compatible domain events can carry extra fields without breaking old
  clients.
* **Typed client union.** :data:`ClientMessageTyped` is a ``Union`` that
  tries the typed variants first, then falls back to the universal
  envelope. Use :func:`parse_client_message` to narrow, then
  ``isinstance(msg, ClientSubscribeMessage)`` etc.
* **Wire shape is stable.** ``ServerMessage.model_validate(ws_msg.to_dict())``
  round-trips losslessly; ``datetime`` serializes to ISO 8601 with ``Z``
  (when serialized via ``model_dump(mode="json")``), matching
  :meth:`WSMessage.to_dict`. Typed subclasses like
  :class:`ServerHelloMessage` are **optional narrowing**: the base
  :class:`ServerMessage` accepts any dict-shaped ``data``, so a
  ``ServerHelloMessage`` deserialized as ``ServerMessage`` keeps the
  raw dict — clients that need the typed shape should re-validate
  against the typed subclass.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
)

# Reuse the exact enums / dataclasses defined by the framework-agnostic
# engine. This is the *only* permitted dependency direction.
from ..websocket import (
    WSCloseCode,
    WSErrorPayload,
    WSEventType,
    WSMessage,
)

__all__ = [
    # Constants
    "MAX_MESSAGE_SIZE",
    "KNOWN_CLIENT_TYPES",
    "KNOWN_SERVER_TYPES",
    "CLIENT_PROTOCOL_TYPES",
    # Auth / connection
    "AuthInfo",
    "ConnectionInfo",
    # Server → client envelopes
    "ServerMessage",
    "ServerErrorMessage",
    "ServerErrorBody",
    # Server → client protocol messages
    "ServerHelloMessage",
    "ServerPingMessage",
    "ServerPongMessage",
    # Server → client domain events (typed, for OpenAPI)
    "DownloadStartedEvent",
    "DownloadProgressEvent",
    "DownloadCompletedEvent",
    "DownloadFailedEvent",
    "DownloadPausedEvent",
    "DownloadResumedEvent",
    "DownloadCancelledEvent",
    "ChapterStartedEvent",
    "ChapterCompletedEvent",
    "ChapterFailedEvent",
    "LibraryUpdatedEvent",
    "SiteHealthChangedEvent",
    # Client → server envelopes
    "ClientMessage",
    "ClientSubscribeMessage",
    "ClientUnsubscribeMessage",
    "ClientPingMessage",
    "ClientPongMessage",
    "ClientMessageTyped",
    # Parsers / adapters
    "parse_client_message",
    "ServerMessageAdapter",
    "ClientMessageAdapter",
    # Converters
    "ws_to_server_message",
    "server_message_to_ws",
    "ws_to_server_error",
    "server_error_to_ws",
    # Re-exports (eager — this module already imports Pydantic)
    "WSCloseCode",
    "WSEventType",
]


# ===========================================================================
# Constants
# ===========================================================================

#: Hard cap on incoming WS message size, in bytes. Enforced by the
#: receive loop in :mod:`..routers.ws` — this constant is the **authoritative
#: limit** that the router reads, so that client and server agree on the
#: cap. It is declared here because the schema module is the natural
#: home for the protocol's wire-level constants.
MAX_MESSAGE_SIZE: int = 64 * 1024  # 64 KiB

#: Protocol message types handled natively by
#: :meth:`ConnectionManager.handle_message`.
CLIENT_PROTOCOL_TYPES: frozenset[str] = frozenset(
    {"subscribe", "unsubscribe", "ping", "pong"}
)

#: Event types the current shell knows how to route — never enforced, just
#: exposed for autocompletion and testing.
KNOWN_CLIENT_TYPES: frozenset[str] = CLIENT_PROTOCOL_TYPES

#: Server-side event types documented here.
KNOWN_SERVER_TYPES: frozenset[str] = frozenset(
    {e.value for e in WSEventType}
)


# ===========================================================================
# Base models
# ===========================================================================

class _StrictEnvelope(BaseModel):
    """Base for envelopes: strict protocol, JSON-ready, immutable once built."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class _EventEnvelope(_StrictEnvelope):
    """Base for outbound event envelopes.

    Provides ``id`` auto-generation, ISO-8601 ``ts``, and open-typed
    ``data``. Subclasses override ``type`` to a ``Literal`` for OpenAPI
    clarity, but always accept extra keys inside ``data``.
    """

    # ``default_factory`` (not a ``Field(default=...)`` plus a
    # ``field_validator``) — Pydantic v2 only runs ``field_validator``
    # on **provided** values, not on defaults. Using ``default=""`` with
    # a validator that fills the gap would leave ``id=""`` on every
    # instance constructed without an explicit id.
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique message id (UUID4 as string). Generated on the "
                    "server; echoed for trace correlation.",
    )
    ts: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Server-side emission timestamp (ISO 8601, UTC).",
    )


class _DomainData(_StrictEnvelope):
    """Base for *domain* payloads nested under ``data``.

    Domain payloads allow extra fields: plugins and forward-compatible
    versions can extend them without breaking clients.
    """

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# ===========================================================================
# Auth / connection info
# ===========================================================================

class AuthInfo(_DomainData):
    """The authenticated identity echoed back in the ``hello`` message."""

    user_id: str | None = Field(
        default=None,
        description="User id from the JWT `sub` claim, or `null` for guests.",
    )
    role: Literal["guest", "user", "admin"] = Field(
        default="guest",
        description="User role at connection time.",
    )
    username: str | None = Field(default=None)


class ConnectionInfo(_DomainData):
    """Metadata included in the ``hello`` message."""

    connection_id: str
    heartbeat_interval_s: float = Field(
        ge=1.0,
        description="Seconds between server-initiated pings.",
    )
    auth: AuthInfo


# ===========================================================================
# Server → client envelopes
# ===========================================================================

class ServerMessage(_EventEnvelope):
    """Universal outbound envelope.

    Matches :meth:`WSMessage.to_dict` exactly. Used for events, ``hello``,
    ``ping``, ``pong`` — anything that is not an error.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "id": "7a2b4f6e-1c8d-4e2a-9b3f-1234567890ab",
                "type": "download.progress",
                "ts": "2025-01-15T12:34:56.789+00:00",
                "data": {
                    "task_id": "01HPQ9J2X3K4M5N6P7Q8R9S0T1",
                    "status": "running",
                    "completed": 42,
                    "total": 120,
                    "speed_bps": 812345.0,
                },
            }
        },
    )

    type: str = Field(description="Event type. Open vocabulary — plugins may extend.")
    data: dict[str, Any] = Field(default_factory=dict)


class ServerErrorBody(_DomainData):
    """The ``error`` body of a :class:`ServerErrorMessage`."""

    code: str = Field(
        description="Stable SCREAMING_SNAKE_CASE error code "
                    "(e.g. `RATE_LIMIT_EXCEEDED`, `PARSER_ERROR`).",
    )
    message: str = Field(description="Human-readable message.")
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(default=None)


class ServerErrorMessage(_StrictEnvelope):
    """Outbound error envelope.

    Distinct from :class:`ServerMessage` because errors carry an ``error``
    body, not ``data``. Clients must discriminate on ``type == "error"``.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "id": "7a2b4f6e-1c8d-4e2a-9b3f-1234567890ab",
                "type": "error",
                "ts": "2025-01-15T12:34:56.789+00:00",
                "error": {
                    "code": "UNKNOWN_MESSAGE_TYPE",
                    "message": "Unknown message type: 'explode'",
                    "details": {},
                    "request_id": None,
                },
            }
        },
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: Literal["error"] = "error"
    ts: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    error: ServerErrorBody


# ===========================================================================
# Server → client protocol messages
# ===========================================================================

class ServerHelloMessage(ServerMessage):
    """First message sent after a connection is accepted.

    Provides the connection id, the heartbeat interval, and the
    authenticated identity.
    """

    type: Literal["hello"] = "hello"
    data: ConnectionInfo  # type: ignore[assignment]


class ServerPingMessage(ServerMessage):
    """Server-initiated heartbeat ping."""

    type: Literal["ping"] = "ping"


class ServerPongMessage(ServerMessage):
    """Server's reply to a client ping."""

    type: Literal["pong"] = "pong"


# ===========================================================================
# Server → client domain events (typed, for OpenAPI)
# ===========================================================================
#
# These subclass :class:`ServerMessage` to narrow ``type`` and give the
# ``data`` field a concrete schema. They are optional: the generic
# :class:`ServerMessage` always works for the router's response model.
#
# Note: full domain payloads live in :mod:`schemas.responses` (once
# available). For now each ``*Data`` class here is the WS-transport shape
# and is intentionally permissive (``extra="allow"``).


class DownloadStartedData(_DomainData):
    task_id: str
    manga_id: str | None = None
    chapter_id: str | None = None
    manga_title: str = ""
    chapter_label: str = ""
    site_name: str = ""
    site_id: str = ""


class DownloadProgressData(_DomainData):
    task_id: str
    status: str = Field(
        description="One of: pending, running, paused, done, failed, cancelled.",
    )
    completed: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    bytes_completed: int = Field(default=0, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    speed_bps: float | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    chapter_label: str = ""
    page_index: int = Field(default=0, ge=0)
    page_count: int | None = Field(default=None, ge=0)
    error: str | None = None


class DownloadCompletedData(_DomainData):
    task_id: str
    path: str = ""
    output_format: str = ""


class DownloadFailedData(_DomainData):
    task_id: str
    error: str = ""


class DownloadPausedData(_DomainData):
    task_id: str


class DownloadResumedData(_DomainData):
    task_id: str


class DownloadCancelledData(_DomainData):
    task_id: str


class ChapterStartedData(_DomainData):
    task_id: str
    chapter_id: str
    number: str = ""
    title: str = ""


class ChapterCompletedData(_DomainData):
    task_id: str
    chapter_id: str
    pages: int = Field(default=0, ge=0)
    output_path: str = ""


class ChapterFailedData(_DomainData):
    task_id: str
    chapter_id: str
    error: str = ""


class LibraryUpdatedData(_DomainData):
    manga_id: str | None = None
    action: str = Field(
        default="updated",
        description="One of: added, updated, deleted, reindexed.",
    )


class SiteHealthChangedData(_DomainData):
    site_id: str
    health: Literal["healthy", "degraded", "down", "unknown"]
    reason: str | None = None


# --- Typed event subclasses (for OpenAPI enum clarity) ---------------------


class DownloadStartedEvent(ServerMessage):
    type: Literal["download.started"] = "download.started"
    data: DownloadStartedData  # type: ignore[assignment]


class DownloadProgressEvent(ServerMessage):
    type: Literal["download.progress"] = "download.progress"
    data: DownloadProgressData  # type: ignore[assignment]


class DownloadCompletedEvent(ServerMessage):
    type: Literal["download.completed"] = "download.completed"
    data: DownloadCompletedData  # type: ignore[assignment]


class DownloadFailedEvent(ServerMessage):
    type: Literal["download.failed"] = "download.failed"
    data: DownloadFailedData  # type: ignore[assignment]


class DownloadPausedEvent(ServerMessage):
    type: Literal["download.paused"] = "download.paused"
    data: DownloadPausedData  # type: ignore[assignment]


class DownloadResumedEvent(ServerMessage):
    type: Literal["download.resumed"] = "download.resumed"
    data: DownloadResumedData  # type: ignore[assignment]


class DownloadCancelledEvent(ServerMessage):
    type: Literal["download.cancelled"] = "download.cancelled"
    data: DownloadCancelledData  # type: ignore[assignment]


class ChapterStartedEvent(ServerMessage):
    type: Literal["chapter.started"] = "chapter.started"
    data: ChapterStartedData  # type: ignore[assignment]


class ChapterCompletedEvent(ServerMessage):
    type: Literal["chapter.completed"] = "chapter.completed"
    data: ChapterCompletedData  # type: ignore[assignment]


class ChapterFailedEvent(ServerMessage):
    type: Literal["chapter.failed"] = "chapter.failed"
    data: ChapterFailedData  # type: ignore[assignment]


class LibraryUpdatedEvent(ServerMessage):
    type: Literal["library.updated"] = "library.updated"
    data: LibraryUpdatedData  # type: ignore[assignment]


class SiteHealthChangedEvent(ServerMessage):
    type: Literal["site.health.changed"] = "site.health.changed"
    data: SiteHealthChangedData  # type: ignore[assignment]


# ===========================================================================
# Client → server envelopes
# ===========================================================================

class ClientMessage(_StrictEnvelope):
    """Universal inbound envelope.

    Always valid for any non-empty ``type``. Used by the router as the
    primary validation target; the manager's
    :meth:`ConnectionManager.handle_message` does its own dispatch on
    ``type`` and does not require narrower schemas.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {"type": "ping"},
                {"type": "subscribe", "data": {"event_types": ["download.*"]}},
                {"type": "unsubscribe", "data": {"event_types": ["download.progress"]}},
                {"type": "pong"},
            ]
        },
    )

    type: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class ClientSubscribeData(_DomainData):
    event_types: list[str] = Field(default_factory=list)


class ClientUnsubscribeData(_DomainData):
    event_types: list[str] = Field(default_factory=list)


class ClientSubscribeMessage(ClientMessage):
    """Client requests additional event subscriptions.

    ``data`` is optional on the wire (an empty ``subscribe`` is valid —
    it means "no change"), so a ``default_factory`` is provided to make
    ``{"type": "subscribe"}`` (without ``data``) validate as
    :class:`ClientSubscribeMessage` rather than falling back to the
    universal :class:`ClientMessage`. Without the default, the fallback
    would always win for the "no data" case, and the handler would
    never see a typed message.
    """

    type: Literal["subscribe"] = "subscribe"
    data: ClientSubscribeData = Field(  # type: ignore[assignment]
        default_factory=ClientSubscribeData
    )


class ClientUnsubscribeMessage(ClientMessage):
    """Client drops event subscriptions."""

    type: Literal["unsubscribe"] = "unsubscribe"
    data: ClientUnsubscribeData = Field(  # type: ignore[assignment]
        default_factory=ClientUnsubscribeData
    )


class ClientPingMessage(ClientMessage):
    """Client-initiated heartbeat ping."""

    type: Literal["ping"] = "ping"


class ClientPongMessage(ClientMessage):
    """Client replies to a server heartbeat ping."""

    type: Literal["pong"] = "pong"


# ===========================================================================
# Discriminated union + adapter
# ===========================================================================
#
# Order matters: typed branches first, universal fallback last. The
# universal envelope matches *any* string type, so it always succeeds when
# the typed branches fail.
#
# This lets the router either:
#   - call `parse_client_message(raw)` and `isinstance`-narrow, or
#   - just `ClientMessage.model_validate(raw)` for a raw envelope.

ClientMessageTyped = Union[
    ClientSubscribeMessage,
    ClientUnsubscribeMessage,
    ClientPingMessage,
    ClientPongMessage,
    ClientMessage,
]

ClientMessageAdapter: TypeAdapter[ClientMessageTyped] = TypeAdapter(
    ClientMessageTyped
)


def parse_client_message(raw: Any) -> ClientMessageTyped:
    """Validate ``raw`` as the most specific client-message type.

    Returns a :class:`ClientSubscribeMessage` / :class:`ClientUnsubscribeMessage`
    / :class:`ClientPingMessage` / :class:`ClientPongMessage` for known
    protocol types, or a plain :class:`ClientMessage` for anything else
    (including custom plugin message types).

    Note: the transport-level size cap (:data:`MAX_MESSAGE_SIZE`) is
    enforced by the WebSocket receive loop in :mod:`..routers.ws`
    **before** this function is called. This function only validates the
    message **shape**, not its size.

    Raises:
        pydantic.ValidationError: If ``raw`` is not a valid client message
            at all (missing/empty ``type``, wrong top-level shape, or
            ``data`` present and not a dict).
    """
    return ClientMessageAdapter.validate_python(raw)


# ===========================================================================
# Server-side adapter (for the router's outgoing model)
# ===========================================================================

ServerMessageAdapter: TypeAdapter[Union[ServerMessage, ServerErrorMessage]] = (
    TypeAdapter(Union[ServerMessage, ServerErrorMessage])
)


# ===========================================================================
# Converters — dataclass ↔ schema
# ===========================================================================
#
# The wire shape is identical; conversion is lossless. These helpers keep
# the two worlds in sync if either side ever evolves.

def ws_to_server_message(msg: WSMessage) -> ServerMessage:
    """Convert a :class:`WSMessage` dataclass into a validated schema."""
    return ServerMessage.model_validate(msg.to_dict())


def server_message_to_ws(schema: ServerMessage) -> WSMessage:
    """Convert a :class:`ServerMessage` schema back into a dataclass."""
    return WSMessage.from_dict(schema.model_dump(mode="json"))


def ws_to_server_error(err: WSErrorPayload) -> ServerErrorMessage:
    """Convert a :class:`WSErrorPayload` dataclass into a validated schema."""
    return ServerErrorMessage.model_validate(err.to_dict())


def server_error_to_ws(schema: ServerErrorMessage) -> WSErrorPayload:
    """Convert a :class:`ServerErrorMessage` schema back into a dataclass."""
    body = schema.error
    return WSErrorPayload(
        code=body.code,
        message=body.message,
        details=dict(body.details) if body.details else None,
        request_id=body.request_id,
        id=schema.id,
        ts=schema.ts,
    )
