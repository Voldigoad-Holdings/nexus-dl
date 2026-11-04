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
"""
from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger as _logger

from ..dependencies import get_auth_service, get_connection_manager
from ..schemas.ws import MAX_MESSAGE_SIZE, WSCloseCode, parse_client_message
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
        await _reject(
            websocket,
            WSCloseCode.AUTH_FAILED,
            "missing token",
        )
        return

    try:
        payload = await service.verify_access(raw_token)
    except Exception:
        # TokenExpired / Revoked / Invalid / TypeMismatch — all become
        # the same 403 rejection. The client can't act differently on
        # the subtypes at the HTTP layer, so we don't distinguish here.
        await _reject(
            websocket,
            WSCloseCode.AUTH_FAILED,
            "invalid or expired token",
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

        # --- Size guard ---------------------------------------------
        if not isinstance(raw, (dict, list)) or len(str(raw)) > MAX_MESSAGE_SIZE:
            conn.enqueue(
                WSErrorPayload(
                    code="MESSAGE_TOO_BIG",
                    message=(
                        f"Message exceeds the {MAX_MESSAGE_SIZE} byte limit "
                        "or is not a JSON object."
                    ),
                )
            )
            continue

        # --- Envelope validation ------------------------------------
        try:
            parse_client_message(raw)
        except Exception as exc:
            conn.enqueue(
                WSErrorPayload(
                    code="VALIDATION_ERROR",
                    message="Invalid message envelope.",
                    details={"errors": _safe_errors(exc)},
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
    """
    try:
        await websocket.close(code=int(code), reason=reason)
    except Exception:
        # Best effort: if the socket is already dead there is nothing to
        # do, and raising here would mask the auth error we already
        # decided to log/reject.
        pass


def _safe_errors(exc: BaseException) -> list[dict[str, Any]]:
    """Normalize a Pydantic ``ValidationError`` into a JSON-safe list.

    ``errors()`` in Pydantic v2 accepts ``include_url`` and
    ``include_input`` kwargs; earlier versions only accept ``include_url``.
    Both shapes are attempted so this helper works across versions.
    """
    errors_fn = getattr(exc, "errors", None)
    if not callable(errors_fn):
        return []
    try:
        return list(errors_fn(include_url=False, include_input=False))
    except TypeError:
        try:
            return list(errors_fn(include_url=False))
        except TypeError:
            return list(errors_fn())
    except Exception:
        return []
