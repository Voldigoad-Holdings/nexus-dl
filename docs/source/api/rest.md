# REST API

> **Status**: 🟡 **Draft** (`v1`, stable à partir de `1.0.0`)
> **Base URL (dev)**: `http://localhost:8000`
> **Base URL (prod)**: `<NEXUSDL_API_BASE_URL>`
> **Format**: JSON (`application/json`)
> **Auth**: JWT Bearer

NexusDL exposes a **REST API** built with
[FastAPI](https://fastapi.tiangolo.com/). It is the primary interface used by
the Next.js frontend and is designed to be consumed by any third-party client
(mobile apps, bots, scripts, integrations).

```{note}
This page is a **navigation hub**. The full endpoint-by-endpoint reference
lives in [`docs/API.md`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/API.md).
Use the links below to jump to the relevant section, or read the full
reference for the complete specification.
```

---

## Table of contents

1. [Design principles](#design-principles)
2. [Quick start](#quick-start)
3. [Authentication](#authentication)
4. [Endpoints overview](#endpoints-overview)
5. [Error handling](#error-handling)
6. [Pagination](#pagination)
7. [Rate limiting](#rate-limiting)
8. [OpenAPI & Swagger](#openapi--swagger)
9. [Examples](#examples)
10. [Full reference](#full-reference)

---

## Design principles

The NexusDL REST API is designed around the following principles:

- **Resource-oriented** — `/manga/{id}`, `/chapters/{id}`, `/library/{id}`
- **Async-first** — every endpoint is non-blocking, backed by `asyncio`
- **Idempotent where possible** — `GET`, `PUT`, `DELETE` are idempotent
- **Long-running operations return a task** — `POST /download` returns a
  `task_id` immediately; progress is delivered via
  [WebSocket](websocket.md) or polling
- **No secrets in responses** — `SecretStr` fields are **never** serialized
- **Versioned via URL prefix** — `/api/v1/...`
- **Fully documented via OpenAPI** — `/api/openapi.json`

### Why REST (and not gRPC, GraphQL, etc.)?

- **Broad compatibility** — every language, every platform, every HTTP client
- **Simple debugging** — `curl`, Postman, browser DevTools
- **Cacheable** — HTTP semantics (`ETag`, `Cache-Control`)
- **OpenAPI ecosystem** — codegen, mocking, validation

For real-time, we use **WebSocket** (see [websocket.md](websocket.md)).
For batch operations, use the REST API + WebSocket.

---

## Quick start

### 1. Login

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"your-password"}'
```

Response:

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

### 2. Use the token

```bash
TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
curl http://localhost:8000/api/v1/library \
  -H "Authorization: Bearer $TOKEN"
```

### 3. Search and download

```bash
# Search
curl -X POST http://localhost:8000/api/v1/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"one piece","sites":["mangadex"]}'

# Download
curl -X POST http://localhost:8000/api/v1/download \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "manga_id": "a1b2c3d4e5f6a7b8",
    "site_id": "mangadex",
    "format": "CBZ"
  }'
```

### 4. Follow progress via WebSocket

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
                print(f"{event['payload']['percent']:.0%}")


asyncio.run(main())
```

---

## Authentication

NexusDL uses **JWT Bearer tokens**. The token is obtained via
`POST /api/v1/auth/login` and must be included in the `Authorization`
header of every authenticated request.

```http
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### Roles

| Role | Permissions |
|---|---|
| `GUEST` | Read-only (`/sites`, `/search`, `/manga`, `/chapters`) — only if `NEXUSDL_ALLOW_ANONYMOUS=true` |
| `USER` | All `GUEST` + `/download`, `/library`, `/settings` (own) |
| `ADMIN` | All `USER` + global `/settings`, `/users`, `/sites/{id}/reload` |

See [`docs/API.md#authentication`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/API.md#authentication)
for full details.

---

## Endpoints overview

| Category | Endpoint | Description |
|---|---|---|
| **Health** | `GET /api/v1/health` | Liveness probe |
| | `GET /api/v1/health/ready` | Readiness probe (DB, Playwright, FlareSolverr) |
| **Sites** | `GET /api/v1/sites` | List all sites |
| | `GET /api/v1/sites/{id}` | One site's config |
| | `GET /api/v1/sites/{id}/health` | Health check one site |
| **Search** | `POST /api/v1/search` | Multi-site parallel search |
| **Manga** | `GET /api/v1/manga/{id}` | Full manga metadata |
| | `GET /api/v1/manga/{id}/chapters` | Chapter list (paginated) |
| **Chapters** | `GET /api/v1/chapters/{id}` | Chapter metadata |
| | `GET /api/v1/chapters/{id}/pages` | Page URLs (temporary) |
| **Download** | `POST /api/v1/download` | Create a download task |
| | `GET /api/v1/download/{task_id}` | Poll a task |
| | `GET /api/v1/download` | List all tasks |
| | `POST /api/v1/download/{task_id}/pause` | Pause a task |
| | `POST /api/v1/download/{task_id}/resume` | Resume a task |
| | `DELETE /api/v1/download/{task_id}` | Cancel a task |
| **Library** | `GET /api/v1/library` | List local library |
| | `GET /api/v1/library/{id}` | One manga in library |
| | `PUT /api/v1/library/{id}/progress` | Save reading progress |
| | `DELETE /api/v1/library/{id}` | Remove from library |
| **Settings** | `GET /api/v1/settings` | Get runtime settings (secrets masked) |
| | `PATCH /api/v1/settings` | Update settings (`ADMIN` only) |
| **Auth** | `POST /api/v1/auth/login` | Obtain a JWT |
| | `POST /api/v1/auth/refresh` | Refresh a JWT |
| **Version** | `GET /api/version` | API + app version |

For the full request/response schemas of each endpoint, see
[`docs/API.md#endpoints`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/API.md#endpoints).

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
| `FORBIDDEN` | 403 | Valid token, insufficient role |
| `NOT_FOUND` | 404 | Resource does not exist |
| `VALIDATION_ERROR` | 422 | Request body/query invalid |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests |
| `SITE_UNAVAILABLE` | 503 | Upstream site is down |
| `SITE_BLOCKED` | 503 | Cloudflare / anti-bot blocked |
| `PARSER_ERROR` | 502 | Parser failed to extract data |
| `DOWNLOAD_ERROR` | 500 | Download task failed |
| `PACKAGING_ERROR` | 500 | Packaging failed |
| `LIBRARY_ERROR` | 500 | Library operation failed |
| `CONFIG_ERROR` | 500 | Configuration is invalid |
| `INTERNAL_ERROR` | 500 | Unexpected server error |

Every response includes an `X-Request-ID` header. Include it when reporting a
bug to help trace the request.

See [`docs/API.md#error-handling`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/API.md#error-handling)
for the full details.

---

## Pagination

List endpoints support `page` and `per_page`:

| Parameter | Type | Default | Max |
|---|---|---|---|
| `page` | integer | `1` | — |
| `per_page` | integer | `20` | `100` |

Response envelope:

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

## Rate limiting

| Scope | Limit |
|---|---|
| Per IP (unauthenticated) | 60 req / min |
| Per user (authenticated) | 600 req / min |
| `/auth/login` | 5 req / min |
| `/download` | 30 req / min |

Every response includes:

```http
X-RateLimit-Limit: 600
X-RateLimit-Remaining: 599
X-RateLimit-Reset: 1735689600
```

When exceeded:

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

## OpenAPI & Swagger

The API is auto-documented via OpenAPI 3.1:

| URL | Description |
|---|---|
| `GET /api/openapi.json` | Raw OpenAPI spec |
| `GET /docs` | Swagger UI (interactive) |
| `GET /redoc` | ReDoc (readable) |

```{warning}
In **production** (`NEXUSDL_APP_ENVIRONMENT=prod`), Swagger UI and ReDoc are
**disabled by default** for security. Enable with
`NEXUSDL_API_ENABLE_DOCS=true` **only** on a trusted network.
```

### Generating a client

Because the API is OpenAPI-compliant, you can auto-generate clients:

```bash
# Python
openapi-python-client generate --url http://localhost:8000/api/openapi.json

# TypeScript
npx openapi-typescript http://localhost:8000/api/openapi.json -o api.d.ts

# Go
oapi-codegen -generate types,client http://localhost:8000/api/openapi.json > api.go
```

---

## Examples

### Python (httpx)

```python
import httpx

API = "http://localhost:8000/api/v1"

with httpx.Client(base_url=API, timeout=30) as client:
    # 1. Login
    r = client.post("/auth/login", json={
        "username": "alice",
        "password": "your-password",
    })
    r.raise_for_status()
    token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"

    # 2. Search
    r = client.post("/search", json={
        "query": "one piece",
        "sites": ["mangadex"],
    })
    r.raise_for_status()
    results = r.json()["items"]
    print(f"Found {len(results)} mangas")

    # 3. Download
    r = client.post("/download", json={
        "manga_id": results[0]["manga_id"],
        "site_id": results[0]["site_id"],
        "format": "CBZ",
    })
    r.raise_for_status()
    task_id = r.json()["task_id"]
    print(f"Task: {task_id}")

    # 4. Poll
    while True:
        r = client.get(f"/download/{task_id}")
        r.raise_for_status()
        status = r.json()["status"]
        if status in ("DONE", "FAILED", "CANCELLED"):
            break
        print(f"Status: {status}")
```

### JavaScript / TypeScript (fetch)

```typescript
const API = "http://localhost:8000/api/v1";

async function main(): Promise<void> {
  // 1. Login
  const loginRes = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "alice", password: "your-password" }),
  });
  const { access_token } = await loginRes.json();

  // 2. Search
  const searchRes = await fetch(`${API}/search`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${access_token}`,
    },
    body: JSON.stringify({ query: "one piece", sites: ["mangadex"] }),
  });
  const { items } = await searchRes.json();
  console.log(`Found ${items.length} mangas`);

  // 3. Download
  const downloadRes = await fetch(`${API}/download`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${access_token}`,
    },
    body: JSON.stringify({
      manga_id: items[0].manga_id,
      site_id: items[0].site_id,
      format: "CBZ",
    }),
  });
  const { task_id } = await downloadRes.json();
  console.log(`Task: ${task_id}`);
}

main();
```

### Bash (curl + jq)

```bash
#!/usr/bin/env bash
set -euo pipefail

API="http://localhost:8000/api/v1"

# 1. Login
TOKEN="$(curl -s -X POST "$API/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"your-password"}' \
  | jq -r .access_token)"

# 2. Search
curl -s -X POST "$API/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"one piece","sites":["mangadex"]}' \
  | jq '.items[] | {title, manga_id, site_id}'

# 3. Download
TASK_ID="$(curl -s -X POST "$API/download" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"manga_id":"a1b2c3d4e5f6a7b8","site_id":"mangadex","format":"CBZ"}' \
  | jq -r .task_id)"

echo "Task: $TASK_ID"

# 4. Poll
while true; do
  STATUS="$(curl -s "$API/download/$TASK_ID" \
    -H "Authorization: Bearer $TOKEN" \
    | jq -r .status)"
  echo "Status: $STATUS"
  [[ "$STATUS" == "DONE" || "$STATUS" == "FAILED" ]] && break
  sleep 5
done
```

---

## Full reference

The complete endpoint-by-endpoint specification (request schemas, response
schemas, all error codes, WebSocket protocol) lives in
[`docs/API.md`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docs/API.md).

**Why not duplicate it here?** Because documentation must have **one source
of truth**. `docs/API.md` is:

- Rendered on GitHub, so reviewers can comment on it in PRs
- Versioned with the code (same commit)
- Referenced from the OpenAPI spec

See also:

- [WebSocket API](websocket.md) — real-time events
- [Python API](python.md) — programmatic use from Python
- [Configuration](../usage/configuration.md) — env vars reference
- [Architecture](../development/architecture.md) — hexagonal design

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
