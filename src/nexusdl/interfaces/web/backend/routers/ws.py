"""WebSocket router for ``/ws/v1/events``.

Thin FastAPI glue between the framework and the framework-agnostic engine
(:mod:`..websocket`). Responsibilities:

1. Extract the JWT from the query string (``?token=<jwt>``) or, as a
   fallback, from the ``Authorization: Bearer <jwt>`` header.
2. Verify the JWT **before** accepting the WebSocket upgrade. On failure
   the endpoint rejects the HTTP upgrade with 403 — no connection slot is
   consumed for an invalid token.
3. Register the connection with :class:`ConnectionManager` (which
   ``accept()``s it and sends the ``hello`` message).
4. Loop on ``receive_json()``, enforce the size cap, validate the
   envelope against :mod:`..schemas.ws`, and delegate dispatch to
   :meth:`ConnectionManager.handle_message`.
5. Disconnect cleanly on any exit path (client drop, error, shutdown).

Nothing else happens here — no business logic, no event subscriptions,
no per-user rate limiting (those belong to the engine, the event bus
bridge, and middleware respectively).

Assumptions
===========

The application :mod:`lifespan` is expected to have initialised:

* ``app.state.auth_service`` — an :class:`~..security.AuthService`
  instance.
* ``app.state.connection_manager`` — a
  :class:`~..websocket.ConnectionManager` (already ``start()``-ed, so its
  heartbeat loop is running).
* ``app.state.bus_bridge`` — a :class:`~..websocket.BusBridge` (already
  ``start()``-ed, so application events reach connected clients).

Both accessors are provided by :mod:`..dependencies`. Their contract is
trivial (``return websocket.app.state.<name>``); they exist as
dependencies so that tests and future transport layers can inject fakes.

Close codes on auth failure
===========================

Because rejection happens **before** ``accept()``, Starlette sends an
HTTP 403 in response to the upgrade request, not a WebSocket close frame.
Browser clients see a generic ``onerror`` / ``onclose(1006)``; native
clients (``websockets``, ``httpx``, curl) see the 403. Clients that need
to distinguish "bad token" from "server down" should call
``GET /api/v1/auth/me`` first and open the socket only on 200.

Logging
=======

Every authentication failure is logged:

* missing token → ``debug`` (common: unauthenticated reconnect),
* invalid / expired token → ``info`` (useful for abuse detection,
  includes a bounded 8-char token prefix so operators can correlate
  without exposing the full token),
* unexpected ``verify_access`` exception (backend outage, bug) →
  ``error`` with traceback.

The client IP is included in every record. The full token is **never**
logged.
"""
from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger as _logger

from ..dependencies import get_auth_service, get_connection_manager
from ..schemas.ws import MAX_MESSAGE_SIZE, WSCloseCode, parse_client_message
from ..security import TokenError
from ..websocket import (
    AuthContext,
    TooManyConnectionsError,
    WSErrorPayload,
)

_log = _logger.bind(component="web.routers.ws")

router = APIRouter(tags=["websocket"])


# ===========================================================================
# Endpoint
# ===========================================================================

@router.websocket("/ws/v1/events")
async def events_endpoint(
    websocket: WebSocket,
    token: Annotated[
        str | None,
        Query(
            description=(
                "JWT access token. Standard for browser clients (WebSocket "
                "API does not allow custom headers). Non-browser clients "
                "may instead send `Authorization: Bearer <jwt>`."
            ),
        ),
    ] = None,
) -> None:
    """Main WebSocket endpoint: authenticated real-time event stream.

    The connection starts subscribed to ``"*"`` (all events). Clients can
    narrow with ``{"type": "subscribe", "data": {"event_types": [...]}}``
    and widen back with ``unsubscribe``. Protocol ``ping``/``pong`` are
    handled by the engine; any other message type routes to a
    registered handler, or produces a typed ``error`` envelope.
    """
    manager = get_connection_manager(websocket)
    service = get_auth_service(websocket)

    # --- 1. Extract and verify the token -------------------------------
    raw_token = _extract_token(websocket, token)
    if not raw_token:
        _log.debug(
            "WS auth rejected: missing token (client={})",
            _client_of(websocket),
        )
        await _reject(
            websocket,
            WSCloseCode.AUTH_FAILED,
            "missing token",
        )
        return

    try:
        payload = await service.verify_access(raw_token)
    except TokenError as exc:
        # Expected auth failure (expired, revoked, invalid signature,
        # wrong type). Log at INFO with a bounded token prefix — enough
        # for operators to correlate client reports, not enough to
        # reconstitute the secret.
        _log.info(
            "WS auth rejected: reason={} token_prefix={} client={}",
            type(exc).__name__,
            raw_token[:8],
            _client_of(websocket),
        )
        await _reject(
            websocket,
            WSCloseCode.AUTH_FAILED,
            "invalid or expired token",
        )
        return
    except Exception:
        # Programming error, backend outage (revocation store unreachable,
        # DB error), or any other *non-credential* failure. Do NOT mask
        # as 403 "invalid token" — the client's credential is not the
        # problem, the server is. Log with traceback.
        _log.exception(
            "WS auth verification raised unexpectedly; client={}",
            _client_of(websocket),
        )
        await _reject(
            websocket,
            WSCloseCode.INTERNAL_ERROR,
            "authentication backend unavailable",
        )
        return

    auth = AuthContext(
        user_id=payload.sub,
        role=payload.role,
        username=payload.username,
    )

    # --- 2. Register the connection ----------------------------------
    try:
        conn = await manager.connect(
            websocket,
            auth,
            subscriptions={"*"},  # opt into everything; narrow via subscribe
        )
    except TooManyConnectionsError:
        # manager.connect() already closed the socket with TRY_AGAIN_LATER.
        _log.warning(
            "WS connection rejected (server at capacity) user={} role={}",
            auth.user_id,
            auth.role.value,
        )
        return

    _log.debug(
        "WS session open id={} user={} role={}",
        conn.id[:8],
        auth.user_id,
        auth.role.value,
    )

    # --- 3. Receive loop ---------------------------------------------
    try:
        await _receive_loop(websocket, manager, conn)
    except asyncio.CancelledError:
        # Server shutting down — the lifespan will call manager.stop(),
        # which closes every connection with SERVICE_RESTART.
        raise
    finally:
        # manager.disconnect() deregisters the connection and closes the
        # transport if it is still open. It is idempotent.
        await manager.disconnect(conn)
        _log.debug("WS session closed id={}", conn.id[:8])


# ===========================================================================
# Receive loop
# ===========================================================================

async def _receive_loop(
    websocket: WebSocket,
    manager: Any,
    conn: Any,
) -> None:
    """Read, validate, and dispatch incoming client messages.

    Exits cleanly when:

    * the client disconnects (``WebSocketDisconnect``),
    * the engine flags the connection as closed (slow-consumer overflow,
      heartbeat timeout, or an explicit admin close),
    * any transport-level exception occurs.
    """
    while not conn.closed:
        # --- Read ----------------------------------------------------
        try:
            raw = await websocket.receive_json()
        except WebSocketDisconnect:
            # Normal client-initiated disconnect.
            return
        except json.JSONDecodeError:
            # Well-formed frame, invalid JSON — do not drop the client.
            conn.enqueue(
                WSErrorPayload(
                    code="INVALID_JSON",
                    message="Message must be valid JSON.",
                )
            )
            continue
        except UnicodeDecodeError:
            # Binary frame with invalid UTF-8 — ``receive_json`` decodes
            # to text first, so this surfaces here.
            conn.enqueue(
                WSErrorPayload(
                    code="INVALID_ENCODING",
                    message="Message must be UTF-8 encoded text.",
                )
            )
            continue
        except Exception:
            # Transport is dead (TCP reset, protocol error, etc.).
            # The manager's heartbeat / send loop will reap us; log at
            # debug to avoid noise on normal client drops.
            _log.debug("receive_json raised; closing id={}", conn.id[:8])
            return

        if conn.closed:
            # The engine decided to close us between the last iteration
            # and now (e.g. queue overflow triggered by an outgoing event).
            return

        # --- Shape guard --------------------------------------------
        # ``receive_json()`` already fully buffered the message, so the
        # size check below is a **policy** limit, not a transport-level
        # one. The transport-level guard is the ASGI server's
        # ``--ws-max-size`` (or equivalent). We still enforce a policy
        # cap because a single 100 MiB JSON blob is a DoS vector even
        # if the server accepted it.
        if not isinstance(raw, dict):
            conn.enqueue(
                WSErrorPayload(
                    code="INVALID_MESSAGE",
                    message="Message must be a JSON object.",
                )
            )
            continue

        # --- Size guard ---------------------------------------------
        # Use the actual UTF-8 wire bytes, not the Python ``repr`` size.
        # ``str(raw)`` under-counts non-ASCII payloads and over-counts
        # escaped ones, and — critically — raises ``RecursionError``
        # on deeply nested JSON (10k+ levels fit under any reasonable
        # ASGI max-size yet crash ``str(raw)``).
        try:
            serialized = json.dumps(
                raw, ensure_ascii=False, separators=(",", ":"),
            )
            wire_bytes = len(serialized.encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            conn.enqueue(
                WSErrorPayload(
                    code="INVALID_MESSAGE",
                    message="Message could not be serialized.",
                )
            )
            continue

        if wire_bytes > MAX_MESSAGE_SIZE:
            conn.enqueue(
                WSErrorPayload(
                    code="MESSAGE_TOO_BIG",
                    message=(
                        f"Message exceeds the {MAX_MESSAGE_SIZE}-byte limit."
                    ),
                )
            )
            continue

        # --- Envelope validation ------------------------------------
        try:
            parse_client_message(raw)
        except Exception as exc:
            errors = _safe_errors(exc)
            if errors:
                # Expected Pydantic validation failure — report to the
                # client with the normalized error shape.
                conn.enqueue(
                    WSErrorPayload(
                        code="VALIDATION_ERROR",
                        message="Invalid message envelope.",
                        details={"errors": errors},
                    )
                )
            else:
                # ``parse_client_message`` raised something that is
                # *not* a Pydantic validation error — that is a bug in
                # the schema module or an unexpected condition. Do not
                # lie to the client with "validation error" and no
                # details; log it and return an honest internal error.
                _log.exception(
                    "parse_client_message raised unexpectedly id={}",
                    conn.id[:8],
                )
                conn.enqueue(
                    WSErrorPayload(
                        code="INTERNAL_ERROR",
                        message="Failed to validate message.",
                    )
                )
            continue

        # --- Dispatch -----------------------------------------------
        # The engine handles ping/pong/subscribe/unsubscribe natively,
        # routes custom types to registered handlers, and produces a
        # typed error envelope for unknown types. It never raises for
        # these cases (handler exceptions are logged & reported).
        try:
            await manager.handle_message(conn, raw)
        except Exception:
            # ``raw`` is guaranteed to be a ``dict`` at this point —
            # the shape guard above would have ``continue``d otherwise.
            _log.exception(
                "handle_message raised id={} type={}",
                conn.id[:8],
                raw.get("type", "?"),
            )
            # Do not break: a single handler failure must not kill the
            # session. Report it and keep going.
            conn.enqueue(
                WSErrorPayload(
                    code="HANDLER_ERROR",
                    message="An internal handler raised while processing "
                            "your message.",
                )
            )


# ===========================================================================
# Helpers
# ===========================================================================

def _extract_token(
    websocket: WebSocket, query_token: str | None
) -> str | None:
    """Return the JWT from the query string, or fall back to the header.

    Browser ``WebSocket`` has no way to attach custom headers, so the
    query-string form is the canonical one. ``Authorization: Bearer`` is
    supported as a convenience for CLI and native clients.
    """
    if query_token:
        return query_token.strip() or None
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


async def _reject(
    websocket: WebSocket,
    code: WSCloseCode,
    reason: str,
) -> None:
    """Reject a WS upgrade with HTTP 403.

    Called **before** ``accept()``. Starlette turns this into an HTTP 403
    response (the ``code`` argument is not transmitted over the wire in
    this path — it only affects the internal close state, which the ASGI
    server uses to log the rejection reason).

    The caller is responsible for logging; this helper only closes the
    transport. Splitting the two concerns keeps this function trivially
    testable and avoids double-logging when the caller wants different
    log levels for different reasons.
    """
    try:
        await websocket.close(code=int(code), reason=reason)
    except Exception:
        # Best effort: if the socket is already dead there is nothing to
        # do, and raising here would mask the auth error we already
        # decided to log/reject.
        pass


def _client_of(websocket: WebSocket) -> str:
    """Return a printable client identifier (host) for logging.

    ``websocket.client`` is an ``Address`` namedtuple in Starlette, but
    the ASGI spec allows it to be ``None`` (e.g. Unix sockets). Falls
    back to ``"?"`` in every ambiguous case — never raises.
    """
    try:
        client = websocket.client
    except Exception:
        return "?"
    if client is None:
        return "?"
    host = getattr(client, "host", None)
    if host:
        return str(host)
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return "?"


def _safe_errors(exc: BaseException) -> list[dict[str, Any]]:
    """Normalize a Pydantic ``ValidationError`` into a JSON-safe list.

    Mirrors :func:`main._install_exception_handlers`'s normalization so
    the wire format is identical between HTTP validation errors and
    WebSocket validation errors. Only ``loc``, ``msg``, and ``type``
    survive; ``ctx`` / ``input`` / ``url`` are stripped because they
    may contain values that are not JSON-serializable (arbitrary
    Python objects passed to validators). A non-serializable error
    would crash the WebSocket send loop, killing the session — the
    exact opposite of the intent.

    Returns an empty list if the exception is not a Pydantic
    ``ValidationError`` — the caller treats an empty list as "this was
    not a validation error" and handles it accordingly.

    ``errors()`` in Pydantic v2 accepts ``include_url`` and
    ``include_input`` kwargs; earlier versions only accept
    ``include_url``. Both shapes are attempted so this helper works
    across versions.
    """
    errors_fn = getattr(exc, "errors", None)
    if not callable(errors_fn):
        return []

    try:
        raw_errors = list(errors_fn(include_url=False, include_input=False))
    except TypeError:
        try:
            raw_errors = list(errors_fn(include_url=False))
        except TypeError:
            try:
                raw_errors = list(errors_fn())
            except Exception:
                return []
        except Exception:
            return []
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for e in raw_errors:
        if not isinstance(e, dict):
            continue
        out.append(
            {
                "loc": [str(p) for p in e.get("loc", ())],
                "msg": str(e.get("msg", "")),
                "type": str(e.get("type", "")),
            }
        )
    return out
