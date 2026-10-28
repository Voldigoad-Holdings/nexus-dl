# NexusDL — API Reference

> **Version** : `v1` (stable à partir de `1.0.0`)
> **Base URL (développement)** : `http://localhost:8000`
> **Base URL (production)** : `<NEXUSDL_API_BASE_URL>`
> **Format** : JSON (`application/json`)
> **Encodage** : UTF-8
> **Auth** : JWT Bearer (voir [Authentication](#authentication))

---

## Table of contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Versioning](#versioning)
4. [Rate limiting](#rate-limiting)
5. [Error handling](#error-handling)
6. [Pagination](#pagination)
7. [Endpoints](#endpoints)
   - [Health](#health)
   - [Sites](#sites)
   - [Search](#search)
   - [Manga](#manga)
   - [Chapters](#chapters)
   - [Download](#download)
   - [Library](#library)
   - [Settings](#settings)
8. [WebSocket API](#websocket-api)
9. [OpenAPI / Swagger](#openapi--swagger)
10. [Examples](#examples)

---

## Overview

NexusDL exposes a **REST API** built with [FastAPI](https://fastapi.tiangolo.com/),
consumed by the Next.js frontend and by any third-party client.

The API is designed around the following principles:

- **Resource-oriented** — `/manga/{id}`, `/chapters/{id}`, etc.
- **Async-first** — every endpoint is non-blocking
- **Idempotent where possible** — `GET`, `PUT`, `DELETE` are idempotent
- **Long-running operations** return a `task_id` immediately, and progress is
  delivered via WebSocket or polling
- **No secrets in responses** — `SecretStr` fields are never serialized

---

## Authentication

NexusDL uses **JWT Bearer tokens** for authentication.

### Obtaining a token

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "alice",
  "password": "correct-horse-battery-staple"
}
```

**Response** `200 OK`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "username": "alice",
    "role": "USER"
  }
}
```

### Using the token

Include the token in the `Authorization` header of every authenticated request:

```http
GET /api/v1/library
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### Refreshing a token

Tokens expire after `NEXUSDL_JWT_EXPIRE_MINUTES` (default: 60 minutes).
To refresh before expiration:

```http
POST /api/v1/auth/refresh
Authorization: Bearer <current-token>
```

**Response** `200 OK`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

### Roles

| Role | Permissions |
|---|---|
| `GUEST` | Read-only access to `/sites`, `/search`, `/manga`, `/chapters` (if `NEXUSDL_ALLOW_ANONYMOUS=true`) |
| `USER` | All `GUEST` permissions + `/download`, `/library`, `/settings` (own) |
| `ADMIN` | All `USER` permissions + `/settings` (global), `/users`, `/sites/{id}/reload` |

---

## Versioning

The API is versioned via the URL prefix:

```
/api/v1/...
```

- **`v1`** is the current stable version.
- Breaking changes introduce **`v2`**, and `v1` remains available for at least
  **6 months** after `v2` is published.
- Additive, backward-compatible changes (new fields, new endpoints) do **not**
  require a new version.

The version is also available at runtime:

```http
GET /api/version
```

**Response** `200 OK`:

```json
{
  "api": "v1",
  "app": "0.1.0",
  "python": "3.12.4",
  "build": "dev"
}
```

---

## Rate limiting

The API enforces rate limits to prevent abuse:

| Scope | Limit | Header |
|---|---|---|
| Per IP (unauthenticated) | 60 req / min | `X-RateLimit-Limit` |
| Per user (authenticated) | 600 req / min | `X-RateLimit-Limit` |
| Per endpoint (`/auth/login`) | 5 req / min | `X-RateLimit-Limit` |
| Per endpoint (`/download`) | 30 req / min | `X-RateLimit-Limit` |

Every response includes:

```http
X-RateLimit-Limit: 600
X-RateLimit-Remaining: 599
X-RateLimit-Reset: 1735689600
```

When the limit is exceeded:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 42
Content-Type: application/json

{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Too many requests. Retry after 42 seconds."
  }
}
```

---

## Error handling

All errors follow the same envelope:

```json
{
  "error": {
    "code": "MACHINE_READABLE_CODE",
    "message": "Human-readable message.",
    "details": { "field": "additional context" },
    "request_id": "abc-123"
  }
}
```

### Error codes

| Code | HTTP | Description |
|---|---|---|
| `UNAUTHORIZED` | 401 | Missing or invalid token |
| `FORBIDDEN` | 403 | Valid token but insufficient role |
| `NOT_FOUND` | 404 | Resource does not exist |
| `VALIDATION_ERROR` | 422 | Request body/query invalid |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests |
| `SITE_UNAVAILABLE` | 503 | The upstream site is down |
| `SITE_BLOCKED` | 503 | Cloudflare / anti-bot blocked the request |
| `PARSER_ERROR` | 502 | The parser failed to extract data |
| `DOWNLOAD_ERROR` | 500 | Download task failed |
| `PACKAGING_ERROR` | 500 | Packaging (CBZ/PDF/etc.) failed |
| `LIBRARY_ERROR` | 500 | Local library operation failed |
| `CONFIG_ERROR` | 500 | Configuration is invalid |
| `INTERNAL_ERROR` | 500 | Unexpected server error |

### Request ID

Every response includes an `X-Request-ID` header. Include it when reporting a
bug to help us trace the request:

```http
X-Request-ID: abc-123-456
```

---

## Pagination

List endpoints support pagination via query parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `page` | integer | `1` | Page number (1-based) |
| `per_page` | integer | `20` | Items per page (max: 100) |

**Response**:

```json
{
  "items": [ ... ],
  "total": 142,
  "page": 1,
  "per_page": 20,
  "total_pages": 8,
  "has_next": true,
  "has_prev": false
}
```

---

## Endpoints

### Health

#### `GET /api/v1/health`

Liveness probe. Returns `200` if the process is alive.

**Response** `200 OK`:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "uptime_seconds": 3600
}
```

---

#### `GET /api/v1/health/ready`

Readiness probe. Returns `200` if all dependencies (DB, Playwright,
FlareSolverr) are reachable.

**Response** `200 OK`:

```json
{
  "status": "ready",
  "checks": {
    "database": "ok",
    "playwright": "disabled",
    "flaresolverr": "disabled",
    "library_fts5": "ok"
  }
}
```

**Response** `503 Service Unavailable` if any check fails:

```json
{
  "status": "not_ready",
  "checks": {
    "database": "error: connection refused",
    "library_fts5": "error: FTS5 not available"
  }
}
```

---

### Sites

#### `GET /api/v1/sites`

List all configured sites.

**Query parameters**:

| Parameter | Type | Description |
|---|---|---|
| `language` | string | Filter by language (`fr`, `en`, `kr`, `adult`, ...) |
| `include_adult` | boolean | Include adult sites (default: `false`) |

**Response** `200 OK`:

```json
{
  "items": [
    {
      "id": "mangadex",
      "name": "MangaDex",
      "domains": ["https://mangadex.org"],
      "language": "en",
      "adult": false,
      "capabilities": {
        "supports_search": true,
        "requires_cloudflare_bypass": false,
        "max_concurrent_downloads": 4,
        "default_rate_limit_per_second": 2.0
      }
    }
  ],
  "total": 60
}
```

---

#### `GET /api/v1/sites/{site_id}`

Get one site's configuration.

**Response** `200 OK`:

```json
{
  "id": "mangadex",
  "name": "MangaDex",
  "domains": ["https://mangadex.org"],
  "language": "en",
  "adult": false,
  "capabilities": { ... },
  "default_headers": {
    "User-Agent": "NexusDL/0.1.0"
  }
}
```

**Errors**:
- `404 NOT_FOUND` if `site_id` does not exist.

---

#### `GET /api/v1/sites/{site_id}/health`

Run a health check against the site.

**Response** `200 OK`:

```json
{
  "site_id": "mangadex",
  "status": "healthy",
  "latency_ms": 245,
  "checked_at": "2026-10-28T12:00:00Z"
}
```

**Possible statuses**: `healthy`, `degraded`, `down`, `unknown`.

---

### Search

#### `POST /api/v1/search`

Multi-site search in parallel.

**Request body**:

```json
{
  "query": "one piece",
  "sites": ["mangadex", "mangafire"],
  "languages": ["en", "fr"],
  "include_adult": false,
  "page": 1,
  "per_page": 20
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | ✅ | Search query (min 2 chars) |
| `sites` | string[] | ❌ | Restrict to these site IDs. Empty = all sites |
| `languages` | string[] | ❌ | Restrict to these languages |
| `include_adult` | boolean | ❌ | Include adult sites (default: `false`) |
| `page` | integer | ❌ | Page number (default: `1`) |
| `per_page` | integer | ❌ | Items per page (default: `20`, max: `100`) |

**Response** `200 OK`:

```json
{
  "items": [
    {
      "site_id": "mangadex",
      "site_name": "MangaDex",
      "manga_id": "a1b2c3d4",
      "title": "One Piece",
      "cover_url": "https://uploads.mangadex.org/covers/...",
      "author": "Oda Eiichiro",
      "language": "en",
      "content_rating": "SAFE",
      "url": "https://mangadex.org/title/...",
      "match_score": 0.98
    }
  ],
  "total": 42,
  "page": 1,
  "per_page": 20,
  "sites_queried": 2,
  "sites_failed": [],
  "elapsed_ms": 850
}
```

**Notes**:
- Sites that fail during the search are listed in `sites_failed` (does not
  cause the whole request to fail).
- `match_score` is a float in `[0, 1]`, normalized across sites.
- Adult content is excluded unless `include_adult=true` **and**
  `NEXUSDL_ALLOW_ADULT=true`.

**Errors**:
- `422 VALIDATION_ERROR` if `query` is too short or `per_page > 100`.
- `503 SITE_UNAVAILABLE` if **all** sites fail (rare).

---

### Manga

#### `GET /api/v1/manga/{manga_id}`

Get full manga metadata.

**Query parameters**:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `site_id` | string | — | **Required**. The site the manga belongs to |
| `refresh` | boolean | `false` | Force re-fetch from the site (bypass cache) |

**Response** `200 OK`:

```json
{
  "id": "a1b2c3d4e5f6a7b8",
  "source_id": "1234-uuid",
  "site": "mangadex",
  "title": "One Piece",
  "alternative_titles": ["ワンピース"],
  "description": "A story about pirates...",
  "author": "Oda Eiichiro",
  "artist": "Oda Eiichiro",
  "genres": ["Action", "Adventure", "Comedy"],
  "status": "ONGOING",
  "year": 1997,
  "cover_url": "https://...",
  "language": "en",
  "content_rating": "SAFE",
  "url": "https://mangadex.org/title/...",
  "updated_at": "2026-10-28T12:00:00Z",
  "chapters_count": 1100
}
```

**Errors**:
- `404 NOT_FOUND` if the manga does not exist on the site.
- `503 SITE_UNAVAILABLE` if the site is down.

---

#### `GET /api/v1/manga/{manga_id}/chapters`

List chapters of a manga.

**Query parameters**:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `site_id` | string | — | **Required** |
| `language` | string | — | Filter by language |
| `page` | integer | `1` | |
| `per_page` | integer | `100` | Max: `500` |

**Response** `200 OK`:

```json
{
  "items": [
    {
      "id": "ch1a2b3c4",
      "manga_id": "a1b2c3d4e5f6a7b8",
      "source_id": "chapter-uuid",
      "title": "Romance Dawn",
      "number": 1.0,
      "volume": 1,
      "language": "en",
      "pages_count": 53,
      "published_at": "1997-07-22T00:00:00Z",
      "url": "https://mangadex.org/chapter/..."
    }
  ],
  "total": 1100,
  "page": 1,
  "per_page": 100,
  "total_pages": 11,
  "has_next": true,
  "has_prev": false
}
```

---

### Chapters

#### `GET /api/v1/chapters/{chapter_id}`

Get chapter metadata.

**Query parameters**:

| Parameter | Type | Description |
|---|---|---|
| `site_id` | string | **Required** |
| `manga_id` | string | **Required** |

**Response** `200 OK`:

```json
{
  "id": "ch1a2b3c4",
  "manga_id": "a1b2c3d4e5f6a7b8",
  "source_id": "chapter-uuid",
  "title": "Romance Dawn",
  "number": 1.0,
  "volume": 1,
  "language": "en",
  "pages_count": 53,
  "published_at": "1997-07-22T00:00:00Z",
  "url": "https://mangadex.org/chapter/..."
}
```

---

#### `GET /api/v1/chapters/{chapter_id}/pages`

Get the list of page URLs (for previewing, not downloading).

**Query parameters**:

| Parameter | Type | Description |
|---|---|---|
| `site_id` | string | **Required** |
| `manga_id` | string | **Required** |

**Response** `200 OK`:

```json
{
  "chapter_id": "ch1a2b3c4",
  "pages": [
    {
      "index": 0,
      "url": "https://uploads.mangadex.org/data/.../1.png",
      "filename": "001.png"
    }
  ],
  "total": 53
}
```

> ⚠️ Page URLs are **temporary** (signed URLs from the source site). They may
> expire after a few minutes. Use the download endpoint for permanent storage.

---

### Download

#### `POST /api/v1/download`

Create a download task.

**Request body**:

```json
{
  "manga_id": "a1b2c3d4e5f6a7b8",
  "site_id": "mangadex",
  "chapters": ["ch1a2b3c4", "ch5e6f7g8"],
  "format": "CBZ",
  "dest": "~/Downloads/NexusDL",
  "priority": "NORMAL"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `manga_id` | string | ✅ | |
| `site_id` | string | ✅ | |
| `chapters` | string[] | ❌ | Empty = all chapters |
| `format` | string | ❌ | `FOLDER`, `ZIP`, `CBZ`, `CB7`, `PDF`, `CBT` (default: `CBZ`) |
| `dest` | string | ❌ | Destination path (default: `NEXUSDL_DOWNLOAD_DIR`) |
| `priority` | string | ❌ | `LOW`, `NORMAL`, `HIGH` (default: `NORMAL`) |

**Response** `202 Accepted`:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "manga_id": "a1b2c3d4e5f6a7b8",
  "chapters_count": 2,
  "created_at": "2026-10-28T12:00:00Z",
  "ws_url": "/ws/v1/progress?task_id=550e8400-e29b-41d4-a716-446655440000"
}
```

**Notes**:
- The response is returned **immediately**. Progress is delivered via
  WebSocket (`ws_url`) or polling (`GET /api/v1/download/{task_id}`).
- `format` must be one of the supported formats. `CBR` is **not** supported.

**Errors**:
- `422 VALIDATION_ERROR` if `format` is unsupported or `chapters` is empty and
  the manga has no chapters.

---

#### `GET /api/v1/download/{task_id}`

Poll the status of a download task.

**Response** `200 OK`:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "RUNNING",
  "progress": 0.42,
  "chapters_total": 2,
  "chapters_done": 0,
  "current_chapter": {
    "id": "ch1a2b3c4",
    "number": 1.0,
    "pages_total": 53,
    "pages_done": 22
  },
  "started_at": "2026-10-28T12:00:05Z",
  "error": null
}
```

**Statuses**: `PENDING`, `RUNNING`, `PAUSED`, `DONE`, `FAILED`, `CANCELLED`.

**Errors**:
- `404 NOT_FOUND` if `task_id` is unknown.

---

#### `GET /api/v1/download`

List all download tasks.

**Query parameters**:

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Filter by status |
| `page` | integer | |
| `per_page` | integer | Max: `100` |

**Response** `200 OK`:

```json
{
  "items": [ ... ],
  "total": 12,
  "page": 1,
  "per_page": 20
}
```

---

#### `POST /api/v1/download/{task_id}/pause`

Pause a running download.

**Response** `200 OK`:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PAUSED"
}
```

**Errors**:
- `404 NOT_FOUND` if task unknown.
- `409 CONFLICT` if task is not `RUNNING`.
- `400 INVALID_STATE_TRANSITION` if the transition `RUNNING → PAUSED` is not
  allowed.

---

#### `POST /api/v1/download/{task_id}/resume`

Resume a paused download.

**Response** `200 OK`:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "RUNNING"
}
```

---

#### `DELETE /api/v1/download/{task_id}`

Cancel a download task (and remove it from the queue).

**Response** `204 No Content`.

**Errors**:
- `404 NOT_FOUND` if task unknown.
- `400 INVALID_STATE_TRANSITION` if the task is already `DONE`, `FAILED`, or
  `CANCELLED`.

---

### Library

#### `GET /api/v1/library`

List the local library (downloaded mangas).

**Query parameters**:

| Parameter | Type | Description |
|---|---|---|
| `query` | string | FTS5 full-text search on title/author/description |
| `page` | integer | |
| `per_page` | integer | Max: `100` |

**Response** `200 OK`:

```json
{
  "items": [
    {
      "manga_id": "a1b2c3d4e5f6a7b8",
      "title": "One Piece",
      "author": "Oda Eiichiro",
      "site": "mangadex",
      "chapters_downloaded": 45,
      "chapters_total": 1100,
      "total_size_bytes": 1234567890,
      "last_read": {
        "chapter_id": "ch45x9y8z7",
        "page": 12,
        "updated_at": "2026-10-28T10:00:00Z"
      }
    }
  ],
  "total": 42,
  "page": 1,
  "per_page": 20
}
```

---

#### `GET /api/v1/library/{manga_id}`

Get details of one manga in the library.

**Response** `200 OK`:

```json
{
  "manga_id": "a1b2c3d4e5f6a7b8",
  "title": "One Piece",
  "author": "Oda Eiichiro",
  "site": "mangadex",
  "chapters": [
    {
      "chapter_id": "ch1a2b3c4",
      "number": 1.0,
      "path": "/home/alice/Manga/One Piece/Chapter 001.cbz",
      "size_bytes": 12345678,
      "downloaded_at": "2026-10-20T08:00:00Z"
    }
  ]
}
```

**Errors**:
- `404 NOT_FOUND` if the manga is not in the library.

---

#### `PUT /api/v1/library/{manga_id}/progress`

Save reading progress.

**Request body**:

```json
{
  "chapter_id": "ch45x9y8z7",
  "page": 12
}
```

**Response** `200 OK`:

```json
{
  "manga_id": "a1b2c3d4e5f6a7b8",
  "chapter_id": "ch45x9y8z7",
  "page": 12,
  "updated_at": "2026-10-28T12:00:00Z"
}
```

---

#### `DELETE /api/v1/library/{manga_id}`

Remove a manga from the library (files are **not** deleted by default).

**Query parameters**:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `delete_files` | boolean | `false` | Also delete the downloaded files |

**Response** `204 No Content`.

**Errors**:
- `404 NOT_FOUND` if the manga is not in the library.
- `409 CONFLICT` if a download is currently running for this manga.

---

### Settings

#### `GET /api/v1/settings`

Get the current runtime settings. **Secrets are masked** (`SecretStr` →
`"**********"`).

**Requires**: `USER` role or higher.

**Response** `200 OK`:

```json
{
  "app_name": "NexusDL",
  "app_environment": "dev",
  "log_level": "INFO",
  "http_timeout_seconds": 30,
  "max_concurrent_downloads": 3,
  "allow_adult": false,
  "respect_robots_txt": true,
  "download_dir": "/home/alice/Downloads/NexusDL"
}
```

> 🔒 **Secret fields** (`jwt_secret`, `cookie_encryption_key`, etc.) are
> returned as `"**********"` and can never be read via the API.

---

#### `PATCH /api/v1/settings`

Update runtime settings. **Requires**: `ADMIN` role.

**Request body** (partial update):

```json
{
  "log_level": "DEBUG",
  "max_concurrent_downloads": 5
}
```

**Response** `200 OK`:

```json
{
  "updated": ["log_level", "max_concurrent_downloads"],
  "settings": { ... }
}
```

**Errors**:
- `403 FORBIDDEN` if the user is not `ADMIN`.
- `422 VALIDATION_ERROR` if a field is invalid (e.g. `log_level="VERBOSE"`).

---

## WebSocket API

NexusDL exposes a WebSocket endpoint for real-time events (download progress,
library changes, site health).

### Connection

```
ws://<host>/ws/v1/events?token=<jwt>
wss://<host>/ws/v1/events?token=<jwt>
```

The `token` query parameter is the JWT obtained from `/auth/login`.

### Message format

All messages are JSON objects with a `type` field:

```json
{
  "type": "download.progress",
  "timestamp": "2026-10-28T12:00:00Z",
  "payload": { ... }
}
```

### Message types

| Type | When |
|---|---|
| `download.started` | A download task starts |
| `download.progress` | A download task makes progress |
| `download.completed` | A download task completes |
| `download.failed` | A download task fails |
| `download.paused` | A download task is paused |
| `download.resumed` | A download task is resumed |
| `download.cancelled` | A download task is cancelled |
| `chapter.started` | A chapter starts downloading |
| `chapter.completed` | A chapter finishes downloading |
| `chapter.failed` | A chapter fails to download |
| `library.updated` | The local library changes |
| `site.health.changed` | A site's health status changes |
| `ping` | Server keepalive (every 30s) |
| `pong` | Client response to `ping` |

### Example: `download.progress`

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

### Client → Server messages

| Type | Payload | Description |
|---|---|---|
| `ping` | `{}` | Client keepalive |
| `subscribe` | `{ "topics": ["download", "library"] }` | Subscribe to specific topics |
| `unsubscribe` | `{ "topics": ["download"] }` | Unsubscribe from topics |

### Example: Python client

```python
import asyncio
import json
import websockets


async def main() -> None:
    uri = "ws://localhost:8000/ws/v1/events?token=<jwt>"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "subscribe", "topics": ["download"]}))
        async for message in ws:
            event = json.loads(message)
            if event["type"] == "download.progress":
                print(f"{event['payload']['percent']:.0%} done")
            elif event["type"] == "download.completed":
                print("Download done!")


asyncio.run(main())
```

### Example: JavaScript client (browser)

```javascript
const ws = new WebSocket(`ws://localhost:8000/ws/v1/events?token=${jwt}`);

ws.onopen = () => {
  ws.send(JSON.stringify({ type: "subscribe", topics: ["download"] }));
};

ws.onmessage = (msg) => {
  const event = JSON.parse(msg.data);
  if (event.type === "download.progress") {
    console.log(`${(event.payload.percent * 100).toFixed(0)}%`);
  }
};
```

### Connection lifecycle

- The server sends a `ping` every **30 seconds**.
- The client should respond with a `pong` within **10 seconds**.
- If the client does not respond, the server closes the connection.
- The client should implement **exponential backoff** on reconnection
  (`1s`, `2s`, `4s`, `8s`, …, max `60s`).

---

## OpenAPI / Swagger

NexusDL automatically generates OpenAPI 3.1 documentation:

| URL | Description |
|---|---|
| `GET /api/openapi.json` | Raw OpenAPI spec |
| `GET /docs` | Swagger UI (interactive) |
| `GET /redoc` | ReDoc (readable) |

> 🔒 **In production**, Swagger UI is **disabled** by default
> (`NEXUSDL_APP_ENVIRONMENT=prod`). Enable it with
> `NEXUSDL_API_ENABLE_DOCS=true`.

---

## Examples

### Full workflow: search → download → verify

```bash
#!/usr/bin/env bash
set -euo pipefail

API="http://localhost:8000/api/v1"
TOKEN="$(curl -s -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"..."}' \
  | jq -r .access_token)"

# 1. Search
curl -s -X POST "$API/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"one piece","sites":["mangadex"]}' \
  | jq '.items[] | {title, manga_id, site_id}'

# 2. Create download
TASK_ID="$(curl -s -X POST "$API/download" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "manga_id": "a1b2c3d4e5f6a7b8",
    "site_id": "mangadex",
    "format": "CBZ"
  }' | jq -r .task_id)"

echo "Task: $TASK_ID"

# 3. Poll status
while true; do
  STATUS="$(curl -s "$API/download/$TASK_ID" \
    -H "Authorization: Bearer $TOKEN" \
    | jq -r .status)"
  echo "Status: $STATUS"
  [ "$STATUS" = "DONE" ] && break
  [ "$STATUS" = "FAILED" ] && exit 1
  sleep 5
done
```

### Python client (httpx)

```python
import httpx

API = "http://localhost:8000/api/v1"

with httpx.Client(base_url=API) as client:
    # 1. Login
    r = client.post("/auth/login", json={
        "username": "alice",
        "password": "...",
    })
    token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"

    # 2. Search
    r = client.post("/search", json={
        "query": "one piece",
        "sites": ["mangadex"],
    })
    results = r.json()["items"]
    print(f"Found {len(results)} mangas")

    # 3. Download
    r = client.post("/download", json={
        "manga_id": results[0]["manga_id"],
        "site_id": results[0]["site_id"],
        "format": "CBZ",
    })
    task_id = r.json()["task_id"]
    print(f"Task: {task_id}")
```

---

## Changelog

| Date | Version | Changes |
|---|---|---|
| 2026-10-28 | `v1` (draft) | Initial specification |

---

## See also

- [WebSocket API detail](source/api/websocket.md) *(coming soon)*
- [Python API reference](source/api/python.md) *(coming soon)*
- [Architecture overview](source/development/architecture.md)
- [Configuration reference](source/usage/configuration.md)

---

*This document is part of NexusDL and is licensed under
[GPL-3.0-or-later](../LICENSE).*
