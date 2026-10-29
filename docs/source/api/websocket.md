# WebSocket API

> **Status**: 🟡 **Draft** (`v1`, stable à partir de `1.0.0`)
> **Endpoint (dev)**: `ws://localhost:8000/ws/v1/events`
> **Endpoint (prod)**: `<NEXUSDL_API_BASE_URL>` (avec `https://` → `wss://`)
> **Format**: JSON messages
> **Auth**: JWT via query parameter (`?token=...`)

NexusDL exposes a **WebSocket API** for real-time events: download progress,
chapter completion, library updates, and site health changes. It is consumed
by the TUI, the GUI, the Next.js frontend, and any third-party client that
wants live updates.

```{note}
This page is a **usage guide**. The full protocol reference lives in
[`docs/API.md#websocket-api`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/API.md#websocket-api).
For REST endpoints, see [rest.md](rest.md).
```

---

## Table of contents

1. [Why WebSocket?](#why-websocket)
2. [Connection](#connection)
3. [Authentication](#authentication)
4. [Message format](#message-format)
5. [Message types](#message-types)
6. [Subscription model](#subscription-model)
7. [Keepalive](#keepalive)
8. [Reconnection](#reconnection)
9. [Client examples](#client-examples)
10. [Debugging](#debugging)
11. [Full reference](#full-reference)

---

## Why WebSocket?

Download progress updates happen **many times per second**. Polling
`GET /download/{task_id}` at that frequency would:

- Waste bandwidth (HTTP headers + JSON envelope per request)
- Overload the server (hundreds of concurrent clients)
- Add latency (up to `poll_interval` seconds)

**WebSocket** provides:

- **Low latency** — updates are pushed in real time
- **Low overhead** — one TCP connection, no per-message headers
- **Bidirectional** — the client can subscribe/unsubscribe dynamically

For one-shot operations (search, get manga), use the [REST API](rest.md).
For live updates, use WebSocket.

---

## Connection

### URL

```
ws://localhost:8000/ws/v1/events?token=<jwt>
```

In production, always use `wss://` (WebSocket over TLS):

```
wss://<NEXUSDL_API_BASE_URL>/ws/v1/events?token=<jwt>
```

```{warning}
Never use `ws://` (unencrypted) in production. The `token` query parameter is
sent in clear text over `ws://`, which would expose your JWT to any network
observer.
```

### Handshake

The server validates the JWT **before** accepting the connection. If the
token is missing or invalid, the handshake fails with `HTTP 401` (before the
WebSocket upgrade), so a standard WebSocket client will raise a connection
error.

```
Client → Server: GET /ws/v1/events?token=... HTTP/1.1
                 Upgrade: websocket
                 Connection: Upgrade
                 Sec-WebSocket-Key: ...
                 Sec-WebSocket-Version: 13

Server → Client: HTTP/1.1 101 Switching Protocols
                 Upgrade: websocket
                 Connection: Upgrade
                 Sec-WebSocket-Accept: ...
```

After the upgrade, all messages are WebSocket frames.

### Close codes

| Code | Reason | Description |
|---|---|---|
| `1000` | Normal | Server or client closed cleanly |
| `1001` | Going away | Server is shutting down |
| `1008` | Policy violation | Invalid token or forbidden role |
| `1011` | Internal error | Server error, try reconnecting |
| `4001` | Token expired | Reconnect with a fresh token |
| `4002` | Rate limited | Too many messages per second |
| `4003` | Subscribe error | Invalid topic in `subscribe` |

---

## Authentication

The JWT is passed via the `token` query parameter:

```
wss://host/ws/v1/events?token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

```{note}
**Why query parameter and not `Authorization` header?** Because the browser
`WebSocket` API does not allow setting custom headers. The query parameter is
the standard workaround, used by many APIs (Slack, Discord, AWS AppSync).
```

For non-browser clients, you **can** use the `Authorization` header instead:

```python
import websockets

headers = {"Authorization": f"Bearer {token}"}
async with websockets.connect(
    "ws://localhost:8000/ws/v1/events",
    additional_headers=headers,
) as ws:
    ...
```

The server accepts **either** `?token=` **or** `Authorization: Bearer ...`.

### Token refresh

The JWT expires after `NEXUSDL_JWT_EXPIRE_MINUTES` (default: 60). When the
token expires, the server closes the connection with code `4001`. The client
must:

1. Refresh the token via `POST /api/v1/auth/refresh`
2. Reconnect with the new token

```{tip}
Refresh the token **proactively** before it expires (e.g. at 50 minutes) to
avoid a visible disconnect.
```

---

## Message format

All messages are **JSON objects** with at least these fields:

```json
{
  "type": "download.progress",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": { ... }
}
```

| Field | Type | Description |
|---|---|---|
| `type` | `string` | Event type (see [Message types](#message-types)) |
| `timestamp` | `string` (ISO 8601) | Server-side emission time, UTC |
| `payload` | `object` | Type-specific data |
| `request_id` | `string` (optional) | Echo of the client's `request_id` if present |

### Client → Server messages

Client messages use the same envelope, with `type` in `ping`, `pong`,
`subscribe`, `unsubscribe`:

```json
{
  "type": "subscribe",
  "topics": ["download", "library"],
  "request_id": "abc-123"
}
```

The server replies with a `subscribed` confirmation (or `error`).

---

## Message types

### Server → Client

#### `download.started`

Emitted when a download task starts.

```json
{
  "type": "download.started",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "manga_id": "a1b2c3d4e5f6a7b8",
    "chapters_count": 2
  }
}
```

#### `download.progress`

Emitted frequently during a download (throttled to **max 10 Hz** per task).

```json
{
  "type": "download.progress",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "chapter_id": "ch1a2b3c4",
    "chapter_number": 1.0,
    "pages_done": 22,
    "pages_total": 53,
    "percent": 0.42,
    "bytes_downloaded": 1234567,
    "bytes_total": 2939234
  }
}
```

#### `download.completed`

Emitted when a download task finishes successfully.

```json
{
  "type": "download.completed",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "output_path": "/home/alice/Manga/One Piece/Chapters 1-2.cbz",
    "duration_seconds": 42.3
  }
}
```

#### `download.failed`

```json
{
  "type": "download.failed",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "error": "ChapterDownloadError: page 12 failed after 3 retries"
  }
}
```

#### `download.paused`, `download.resumed`, `download.cancelled`

```json
{
  "type": "download.paused",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

#### `chapter.started`

```json
{
  "type": "chapter.started",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "chapter_id": "ch1a2b3c4",
    "chapter_number": 1.0
  }
}
```

#### `chapter.completed`

```json
{
  "type": "chapter.completed",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "chapter_id": "ch1a2b3c4",
    "pages_count": 53,
    "output_path": "/home/alice/Manga/One Piece/Chapter 001.cbz"
  }
}
```

#### `chapter.failed`

```json
{
  "type": "chapter.failed",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "chapter_id": "ch1a2b3c4",
    "error": "PageDownloadError: page 12 failed after 3 retries"
  }
}
```

#### `library.updated`

```json
{
  "type": "library.updated",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "manga_id": "a1b2c3d4e5f6a7b8",
    "operation": "added",
    "title": "One Piece"
  }
}
```

`operation` ∈ `added`, `removed`, `updated`.

#### `site.health.changed`

```json
{
  "type": "site.health.changed",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "site_id": "mangadex",
    "old_status": "healthy",
    "new_status": "degraded",
    "reason": "HTTP 429 from upstream"
  }
}
```

#### `ping` (server keepalive)

Sent every **30 seconds** by the server:

```json
{
  "type": "ping",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {}
}
```

The client **must** respond with `pong` within 10 seconds (see
[Keepalive](#keepalive)).

#### `error`

```json
{
  "type": "error",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "code": "INVALID_TOPIC",
    "message": "Unknown topic 'foo'. Valid: download, library, site, chapter"
  }
}
```

### Client → Server

#### `subscribe`

```json
{
  "type": "subscribe",
  "topics": ["download", "library"],
  "request_id": "abc-123"
}
```

Server confirms:

```json
{
  "type": "subscribed",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": {
    "topics": ["download", "library"],
    "request_id": "abc-123"
  }
}
```

#### `unsubscribe`

```json
{
  "type": "unsubscribe",
  "topics": ["download"],
  "request_id": "abc-124"
}
```

#### `ping` (client)

```json
{
  "type": "ping",
  "timestamp": "2026-10-28T12:00:00Z"
}
```

#### `pong` (response to server `ping`)

```json
{
  "type": "pong",
  "timestamp": "2026-10-28T12:00:00Z"
}
```

---

## Subscription model

A client can subscribe to **one or more topics**. By default (on connection),
the server sends **all** events the client's role is allowed to receive.

To reduce noise, subscribe explicitly:

| Topic | Events |
|---|---|
| `download` | `download.*`, `chapter.*` |
| `library` | `library.*` |
| `site` | `site.health.changed` |

```python
await ws.send(json.dumps({
    "type": "subscribe",
    "topics": ["download"],
}))
```

To receive **only one specific task**, add a filter:

```python
await ws.send(json.dumps({
    "type": "subscribe",
    "topics": ["download"],
    "filter": {"task_id": "550e8400-e29b-41d4-a716-446655440000"},
}))
```

> ⚠️ Filters are **server-side** and additive. Multiple subscriptions to the
> same topic with different filters are merged.

---

## Keepalive

The server sends a `ping` every **30 seconds**. The client must respond with
`pong` within **10 seconds**; otherwise, the server closes the connection
with code `1001` (going away).

### Why ping/pong?

- **Detect dead connections** — a half-open TCP connection looks alive but
  isn't
- **Traverse NAT/proxies** — some proxies drop idle connections after 60s
- **Measure latency** — the client can use ping/pong timestamps

### Client implementation (Python)

```python
import asyncio
import json
import websockets


async def main() -> None:
    uri = "ws://localhost:8000/ws/v1/events?token=<jwt>"

    async with websockets.connect(uri, ping_interval=None) as ws:
        # Handle server-initiated pings manually
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif msg["type"] == "download.progress":
                print(f"{msg['payload']['percent']:.0%}")


asyncio.run(main())
```

> 💡 `websockets` sends its own keepalive pings by default. Set
> `ping_interval=None` to disable them, and handle the server's application-level
> `ping` messages yourself.

### Client implementation (JavaScript)

```javascript
const ws = new WebSocket(uri);

ws.onmessage = (msg) => {
  const event = JSON.parse(msg.data);
  if (event.type === "ping") {
    ws.send(JSON.stringify({ type: "pong" }));
  } else if (event.type === "download.progress") {
    console.log(`${(event.payload.percent * 100).toFixed(0)}%`);
  }
};
```

---

## Reconnection

Network glitches, server restarts, or token expiry can close the WebSocket.
Clients should **reconnect automatically** with **exponential backoff**:

| Attempt | Delay |
|---|---|
| 1 | 1 s |
| 2 | 2 s |
| 3 | 4 s |
| 4 | 8 s |
| 5 | 16 s |
| 6 | 32 s |
| 7+ | 60 s (cap) |

Add **jitter** (`±20%`) to avoid thundering herd.

### Reconnection pseudocode

```python
import asyncio
import json
import logging
import random

import websockets

logger = logging.getLogger(__name__)


async def connect_with_retry(uri: str, get_token) -> None:
    """Connect to the WebSocket, reconnecting on failure."""
    attempt = 0
    while True:
        try:
            token = await get_token()
            url = f"{uri}?token={token}"
            async with websockets.connect(url) as ws:
                attempt = 0  # reset on success
                logger.info("Connected")
                await handle(ws)
        except websockets.ConnectionClosed as exc:
            logger.warning("Disconnected: %s", exc)
        except Exception as exc:
            logger.error("Connection failed: %s", exc)

        # Exponential backoff with jitter
        delay = min(60, 2 ** attempt)
        jitter = delay * 0.2 * (random.random() * 2 - 1)
        wait = max(1, delay + jitter)
        logger.info("Reconnecting in %.1fs (attempt %d)", wait, attempt + 1)
        await asyncio.sleep(wait)
        attempt += 1


async def handle(ws) -> None:
    async for raw in ws:
        msg = json.loads(raw)
        if msg["type"] == "ping":
            await ws.send(json.dumps({"type": "pong"}))
```

### Missed events

WebSocket is **fire-and-forget**: messages are not queued for disconnected
clients. If a client disconnects mid-download:

1. **On reconnect**, the client should fetch the **current state** via REST:
   ```http
   GET /api/v1/download/{task_id}
   ```
2. The `task_id` is remembered from `download.started` before the disconnect.
3. The REST endpoint returns the authoritative `status` and `progress`.

This is the **recommended pattern**: WebSocket for live updates, REST for
state recovery.

---

## Client examples

### Python (websockets + asyncio)

```python
import asyncio
import json
import websockets


async def main() -> None:
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
    uri = f"ws://localhost:8000/ws/v1/events?token={token}"

    async with websockets.connect(uri, ping_interval=None) as ws:
        # Subscribe to download events
        await ws.send(json.dumps({
            "type": "subscribe",
            "topics": ["download"],
            "request_id": "my-client-1",
        }))

        async for raw in ws:
            msg = json.loads(raw)
            t = msg["type"]

            if t == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif t == "subscribed":
                print(f"Subscribed to: {msg['payload']['topics']}")
            elif t == "download.started":
                print(f"Started: {msg['payload']['task_id']}")
            elif t == "download.progress":
                p = msg["payload"]
                print(f"\r{p['percent']:.0%} ({p['pages_done']}/{p['pages_total']})", end="")
            elif t == "download.completed":
                print(f"\nDone: {msg['payload']['output_path']}")
            elif t == "download.failed":
                print(f"\nFailed: {msg['payload']['error']}")


asyncio.run(main())
```

### JavaScript (browser)

```javascript
class NexusWS {
  constructor(uri, token) {
    this.uri = uri;
    this.token = token;
    this.attempt = 0;
    this.ws = null;
  }

  connect() {
    this.ws = new WebSocket(`${this.uri}?token=${this.token}`);
    this.ws.onopen = () => {
      this.attempt = 0;
      this.ws.send(JSON.stringify({
        type: "subscribe",
        topics: ["download", "library"],
      }));
    };
    this.ws.onmessage = (msg) => this.handleMessage(JSON.parse(msg.data));
    this.ws.onclose = (evt) => {
      if (evt.code !== 1000 && evt.code !== 1008) {
        this.reconnect();
      }
    };
  }

  handleMessage(event) {
    if (event.type === "ping") {
      this.ws.send(JSON.stringify({ type: "pong" }));
    } else if (event.type === "download.progress") {
      console.log(`${(event.payload.percent * 100).toFixed(0)}%`);
    }
  }

  reconnect() {
    const delay = Math.min(60000, 1000 * 2 ** this.attempt);
    const jitter = delay * 0.2 * (Math.random() * 2 - 1);
    this.attempt += 1;
    console.log(`Reconnecting in ${Math.round((delay + jitter) / 1000)}s...`);
    setTimeout(() => this.connect(), Math.max(1000, delay + jitter));
  }
}

new NexusWS("ws://localhost:8000/ws/v1/events", token).connect();
```

### `websocat` (terminal)

```bash
# Connect with token
websocat "ws://localhost:8000/ws/v1/events?token=<jwt>"

# Then type messages interactively:
> {"type":"subscribe","topics":["download"]}
> {"type":"ping"}

# Or send a subscribe on connection:
echo '{"type":"subscribe","topics":["download"]}' | \
  websocat "ws://localhost:8000/ws/v1/events?token=<jwt>"
```

### `wscat` (Node.js)

```bash
npm install -g wscat
wscat -c "ws://localhost:8000/ws/v1/events?token=<jwt>"
> {"type":"subscribe","topics":["download"]}
```

---

## Debugging

### Enable verbose logging

On the client side, enable WebSocket frame logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

On the server side, set:

```dotenv
NEXUSDL_LOG_LEVEL=DEBUG
```

and look for `ws.connection.opened`, `ws.message.received`,
`ws.message.sent` in the logs.

### Common issues

| Symptom | Cause | Fix |
|---|---|---|
| Connection fails immediately | Invalid JWT | Check `?token=` and refresh |
| `HTTP 401` on handshake | Token expired | Refresh via `/auth/refresh` |
| Connection closes after 10s | Client didn't respond to `ping` | Handle `ping` → `pong` |
| Connection closes after 60min | JWT expired | Refresh proactively |
| No events received | Wrong topic subscribed | Subscribe to the right topic |
| Only some events | Filters exclude them | Remove `filter` or widen it |
| `4003 Subscribe error` | Invalid topic name | Use `download`, `library`, `site` |
| `429` | Too many messages | Slow down; batch updates if possible |

### Health check

```bash
# REST
curl http://localhost:8000/api/v1/health

# WebSocket (simple connectivity test)
websocat "ws://localhost:8000/ws/v1/events?token=<jwt>" <<< '{"type":"ping"}'
```

---

## Full reference

The complete protocol specification (all message types, all fields, all
payloads, exact close codes) lives in
[`docs/API.md#websocket-api`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/API.md#websocket-api).

**Why not duplicate it here?** Because documentation must have **one source
of truth**. This page is a **usage guide**; `docs/API.md` is the **reference**.

See also:

- [REST API](rest.md) — HTTP endpoints
- [Python API](python.md) — programmatic use from Python
- [Configuration](../usage/configuration.md) — env vars reference
- [Architecture](../development/architecture.md) — hexagonal design

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
