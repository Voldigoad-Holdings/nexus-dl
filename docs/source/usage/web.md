# Web Usage

> **Audience**: users who want to access NexusDL from a **browser** —
> phone, tablet, another computer, or a shared household.
>
> **Recommended for**: self-hosting, remote access, multi-device.
>
> **Not for you?** → [CLI](cli.md), [GUI](gui.md), [Docker install](../installation/docker.md).

NexusDL ships with a **modern web application** — a **FastAPI** backend
(REST + WebSocket) and a **Next.js 14** frontend (App Router, Tailwind,
shadcn/ui). It is fully responsive (desktop, tablet, mobile) and
installable as a **PWA**.

```{note}
The web UI is a **thin client** over the REST API. Everything it does,
you can do with `curl` or the [Python API](../api/python.md). See the
[API reference](../api/rest.md).
```

---

## Table of contents

1. [Quick start](#quick-start)
2. [Login and accounts](#login-and-accounts)
3. [Interface overview](#interface-overview)
4. [Search](#search)
5. [Downloads](#downloads)
6. [Library](#library)
7. [Settings](#settings)
8. [Real-time updates](#real-time-updates)
9. [PWA (install on phone)](#pwa-install-on-phone)
10. [Multi-user](#multi-user)
11. [Security](#security)
12. [Reverse proxy](#reverse-proxy)
13. [Troubleshooting](#troubleshooting)
14. [See also](#see-also)

---

## Quick start

### Development (local)

```bash
# Terminal 1 — backend
nexusdl-web

# Terminal 2 — frontend (if not built)
cd src/nexusdl/interfaces/web/frontend
pnpm install
pnpm dev
```

- **API**: <http://localhost:8000>
- **Frontend**: <http://localhost:3000>
- **Swagger UI**: <http://localhost:8000/docs>

### Production (Docker)

See [Docker installation](../installation/docker.md).

```bash
docker compose up -d
# → http://localhost:3000
```

### First launch

1. Open <http://localhost:3000>.
2. The app redirects to **`/auth/login`** if you have no account.
3. **Register** (first user becomes `ADMIN`).
4. Log in.
5. You're on the **home** page.

```{warning}
**The first registered user is `ADMIN`.** Register immediately after
deployment, before exposing the instance to the internet.
```

---

## Login and accounts

NexusDL uses **JWT Bearer tokens** (via `python-jose` + `bcrypt`).

### Registration

`/auth/register` (available only if no user exists, or if `ADMIN` opens it
via Settings).

Fields:

- **Username** (3–32 chars, alphanumeric + `_-`)
- **Email** (optional, for password reset)
- **Password** (min 8 chars)

### Login

`/auth/login`:

- **Username** or **email**
- **Password**
- **"Remember me"** — extends token lifetime from 1h to 30 days

### Roles

| Role | Permissions |
|---|---|
| **GUEST** | Read-only (`/sites`, `/search`, `/manga`, `/chapters`) — only if `NEXUSDL_API_ALLOW_ANONYMOUS=true` |
| **USER** | All `GUEST` + `/download`, `/library`, `/settings` (own) |
| **ADMIN** | All `USER` + `/settings` (global), `/users`, `/sites/{id}/reload` |

### Password reset

If email is configured (`NEXUSDL_SMTP_*` — planned for v0.2.0):

1. Click **"Forgot password?"** on login.
2. Enter email.
3. Check inbox for reset link.
4. Set new password.

**If email is not configured**: reset via CLI:

```bash
nexusdl users reset-password --username alice
```

### Session expiry

- **Default**: 1 hour.
- **"Remember me"**: 30 days.
- **Auto-refresh**: the frontend refreshes the token silently at 50 min.

### Logout

Click your avatar (top right) → **Logout**. The token is revoked
server-side (added to a deny-list).

---

## Interface overview

```
┌─────────────────────────────────────────────────────────────────┐
│  NexusDL                          🔍  🌙  👤 alice  ▾           │
├──────────┬──────────────────────────────────────────────────────┤
│          │                                                      │
│ 🏠 Home  │   Search                                             │
│ 🔍 Search│   ┌────────────────────────────────────────────────┐ │
│ ⬇ Downloads  │ Search mangas...                     [Search] │ │
│ 📚 Library│  └────────────────────────────────────────────────┘ │
│ ⚙ Settings│                                                     │
│          │   ┌──────────┐ ┌──────────┐ ┌──────────┐            │
│ ──────── │   │          │ │          │ │          │            │
│          │   │  Cover   │ │  Cover   │ │  Cover   │            │
│ v0.1.0   │   │          │ │          │ │          │            │
│          │   └──────────┘ └──────────┘ └──────────┘            │
│          │   One Piece    Naruto       Bleach                 │
│          │   Oda E.       Kishimoto    Kubo T.                │
│          │   [MangaDex]   [MangaDex]   [MangaDex]             │
│          │                                                     │
└──────────┴──────────────────────────────────────────────────────┘
```

### Top bar

- **🔍 Search icon** — quick access to search (focus the input)
- **🌙 Theme toggle** — switch dark/light/system
- **👤 Avatar menu** — profile, settings, logout

### Sidebar (left)

| Item | Icon | Route |
|---|---|---|
| **Home** | 🏠 | `/` |
| **Search** | 🔍 | `/search` |
| **Downloads** | ⬇ | `/downloads` |
| **Library** | 📚 | `/library` |
| **Settings** | ⚙ | `/settings` |

On **mobile**, the sidebar becomes a **hamburger menu** (top left).

### Responsive breakpoints

| Width | Layout |
|---|---|
| **< 640 px** | Mobile (sidebar hidden, hamburger) |
| **640–1024 px** | Tablet (sidebar narrow) |
| **> 1024 px** | Desktop (sidebar full) |

---

## Search

**Route**: `/search`

### How to search

1. Type a query (min 2 chars).
2. **Enter** or click **Search**.
3. Results stream in **as they arrive** from each site.

### Filters

| Filter | Options |
|---|---|
| **Sites** | Dropdown (multi-select), default "All" |
| **Language** | `Any`, `en`, `fr`, `kr`, `ja`, … |
| **Include adult** | Toggle (only if `NEXUSDL_ALLOW_ADULT=true`) |

### Result card

Each card shows:

- **Cover** (lazy-loaded)
- **Title** (max 2 lines)
- **Author**
- **Site badge** (e.g. `MangaDex`)
- **Content rating** (`SAFE`, `SUGGESTIVE`, `EROTICA`, `PORNOGRAPHIC`)
- **Match score** (hover)

### Actions

| Action | Result |
|---|---|
| Click card | Open **`/manga/[id]`** (detail page) |
| Right-click | Context menu (Open, Copy URL, Open in new tab) |
| **Download** button (hover) | Quick download (opens format picker) |

### Pagination

- **Infinite scroll** (default) — loads more as you scroll.
- **"Load more"** button (if infinite scroll disabled).

### Empty state

If no results:

> **No results for "asdf".**
> Try a shorter query, or check the [supported sites](/sites).

### Error state

If a site fails:

> **Some sites returned errors.**
> MangaDex, MangaFire responded. Scan-Manga timed out.
> [Retry failed sites]

---

## Downloads

**Route**: `/downloads`

### View modes

| Mode | Description |
|---|---|
| **List** (default) | Compact list, one row per task |
| **Grid** | Card view with covers |
| **Table** | Detailed table (sortable) |

### Each task shows

- **Cover** (small)
- **Manga title** + **chapter number**
- **Progress bar** with percentage
- **Current page / total pages**
- **Speed** (pages/s, MB/s)
- **ETA**
- **Status badge** (`Pending`, `Running`, `Paused`, `Done`, `Failed`, `Cancelled`)
- **Actions**: pause, resume, cancel, open folder, retry

### Live updates

Downloads update in **real time** via **WebSocket**
(`/ws/v1/events`). No page reload needed.

Events consumed:

- `download.started`
- `download.progress` (throttled to 10 Hz per task)
- `download.completed`
- `download.failed`
- `download.paused` / `download.resumed` / `download.cancelled`
- `chapter.started` / `chapter.completed` / `chapter.failed`

### Batch operations

- **Pause all** (running tasks)
- **Resume all** (paused tasks)
- **Cancel all** (with confirmation)
- **Clear completed** (removes `Done` tasks from the list, does **not**
  delete files)

### Filters

| Filter | Options |
|---|---|
| **Status** | All, Running, Paused, Done, Failed, Cancelled |
| **Site** | Any site from `sites.yaml` |
| **Date** | Today, Last 7 days, Last 30 days, All |

### Toast notifications

When a download finishes (or fails), a **toast** appears in the bottom
right:

> ✅ **One Piece · Chapter 001** completed
> `12.3 MB` · `45s`
> [Open folder] [View in library]

---

## Library

**Route**: `/library`

### How to browse

1. The library shows all downloaded mangas.
2. Click a manga → **`/manga/[id]`** (detail page).
3. Click a chapter → **reading view** (image viewer).

### Layout

- **Grid** (default): covers in a responsive grid.
- **List**: table view (title, author, chapters, size, last read).

### Search

The search bar uses **FTS5 full-text search** on:

- Title
- Author
- Description
- Genres

Results update **as you type** (debounced 200ms).

### Filters

| Filter | Options |
|---|---|
| **Reading status** | All, Not started, Reading, Completed |
| **Site** | Any site |
| **Language** | `en`, `fr`, `kr`, … |
| **Genre** | Multi-select from collected genres |

### Sort

| Sort | Options |
|---|---|
| **Title** | A–Z, Z–A |
| **Author** | A–Z, Z–A |
| **Chapters** | Ascending, Descending |
| **Last read** | Recent first, Oldest first |
| **Added** | Recent first, Oldest first |

### Detail page (`/manga/[id]`)

Shows:

- **Header**: cover, title, author, status, year, genres
- **Description** (collapsible)
- **Actions**: Download all, Update, Remove from library, Open folder
- **Chapter list** (paginated, 100 per page)
  - Each chapter: number, title, pages count, download status
  - Click to open **reader**
  - Multi-select for batch download

### Reader view

**Route**: `/manga/[id]/read/[chapterId]`

A **minimal reader** with:

- **Left-to-right** or **right-to-left** (manga mode, from `ComicInfo.xml`)
- **Single page** or **double page** (spread)
- **Zoom** (pinch on mobile, `+`/`-` on desktop)
- **Keyboard navigation** (`←`/`→`, `Page Up`/`Page Down`)
- **Chapter navigation** (previous/next)
- **Progress save** (auto-saves on scroll stop, throttled to 5s)

```{note}
The reader is **basic by design**. For advanced reading, use
[Komga](https://komga.org/) or [Kavita](https://www.kavitareader.com/) —
both work with NexusDL's `ComicInfo.xml`.
```

---

## Settings

**Route**: `/settings`

**Requires**: `USER` role (own settings) or `ADMIN` (global settings).

### Sections

#### Profile

- Username (read-only)
- Email (editable)
- Change password
- Delete account (with confirmation)

#### Downloads

- **Download directory** (read-only — set in `.env`)
- **Default format** (dropdown: `CBZ`, `CB7`, `PDF`, `FOLDER`, `ZIP`, `CBT`)
- **Concurrent downloads** (slider 1–10)
- **Concurrent pages per chapter** (slider 1–16)

#### Appearance

- **Theme** (Light, Dark, System)
- **Accent color** (indigo, cyan, emerald, rose) — *planned v0.2.0*
- **Language** (en, fr, es, de)

#### Content

- **Allow adult** (toggle) — **ADMIN only**
- **Respect robots.txt** (toggle) — **ADMIN only**
- **Strict content filter** (toggle) — **ADMIN only**

#### Network

- **HTTP timeout** (number, 5–120s)
- **Max retries** (number, 0–10)
- **Enable Playwright** (toggle) — **ADMIN only**
- **FlareSolverr URL** (text)
- **Proxy URL** (text)

#### Advanced (ADMIN)

- **Log level** (dropdown)
- **Log format** (json, text, pretty)
- **Reload sites.yaml** (button)
- **Health check** (button)
- **Download logs** (button — downloads `.zip` of logs)

#### Users (ADMIN)

- List of users
- Add user
- Change role
- Reset password
- Delete user

```{warning}
Some settings require **restarting the backend**. The UI shows a
"Restart required" banner and a **Restart** button (Docker only).
```

---

## Real-time updates

The web UI uses **WebSocket** (`/ws/v1/events`) for live updates.

### What streams

| Event | Triggers |
|---|---|
| `download.progress` | Progress bar update (throttled 10 Hz) |
| `download.completed` | Toast + task moved to "Done" |
| `download.failed` | Toast + task moved to "Failed" |
| `library.updated` | Library refresh (if you're on `/library`) |
| `site.health.changed` | Status badge update in Settings |

### Connection indicator

The top-right corner shows a **small dot**:

- 🟢 **Connected** — WS is open
- 🟠 **Reconnecting** — WS closed, backoff in progress
- 🔴 **Offline** — no network

Hover the dot for details:

> Connected · 124 ms latency · 4 subscriptions

### Reconnection

If the WS drops:

1. The UI shows **"Reconnecting…"**.
2. Automatic retry with exponential backoff (1s, 2s, 4s, …, 60s).
3. On reconnect, the UI **re-fetches** current state via REST
   (`GET /api/v1/download`).

### Fallback

If WS fails 3 times in a row, the UI falls back to **polling** every 5s
(REST `GET /api/v1/download`).

---

## PWA (install on phone)

The web UI is installable as a **Progressive Web App**.

### Android (Chrome)

1. Open the site in Chrome.
2. Tap the **"Install"** banner (or **⋮ → "Install app"**).
3. The app appears on your home screen.

### iOS (Safari)

1. Open the site in Safari.
2. Tap **Share** (⬆).
3. Tap **"Add to Home Screen"**.
4. Name it and tap **Add**.

### Desktop (Chrome, Edge)

1. Open the site in Chrome or Edge.
2. Click the **install icon** (⊕) in the address bar.

### PWA features

- ✅ **Offline shell** (UI loads even without network)
- ✅ **Push notifications** (planned v0.2.0)
- ✅ **Background sync** (planned v0.2.0)
- ✅ **Full-screen** (no address bar)

### Limitations

- ❌ Downloads **require** the backend (no offline downloads)
- ❌ Library browsing **requires** the backend
- ❌ iOS PWA has limited storage (50 MB)

---

## Multi-user

NexusDL supports **multiple users** on the same instance.

### User isolation

Each `USER`:

- Has its **own** download queue.
- Has its **own** reading progress.
- Sees the **same** library (shared).

```{note}
Library sharing is **intentional** — a household shares the same manga
collection. If you need per-user libraries, run multiple instances.
```

### Admin-only actions

Only `ADMIN` can:

- Change **global** settings (`ALLOW_ADULT`, `RESPECT_ROBOTS_TXT`, etc.)
- Reload `sites.yaml`
- Add, remove, or change role of users
- View **all** users' downloads
- Access logs

### Disable registration

After creating your admin user, **disable registration**:

```dotenv
NEXUSDL_API_ALLOW_REGISTRATION=false
```

Then only `ADMIN` can create users.

---

## Security

### HTTPS

**Always** deploy behind **HTTPS**. In production:

- Use **Caddy** (automatic Let's Encrypt) or **Nginx** + Certbot.
- Set `NEXUSDL_PUBLIC_URL=https://your.domain`.
- Enable **HSTS** in your reverse proxy.

See [Reverse proxy](#reverse-proxy) and
[Docker installation](../installation/docker.md).

### Secrets

- **Never** commit `.env` to Git.
- Use **Docker secrets** or **HashiCorp Vault** for production.
- Rotate `JWT_SECRET` if a leak is suspected.

### CORS

Restrict to your frontend origin:

```dotenv
NEXUSDL_API_CORS_ORIGINS=["https://nexusdl.example.com"]
```

### Rate limiting

The API enforces:

- **60 req/min** per IP (unauthenticated)
- **600 req/min** per user (authenticated)
- **5 req/min** for `/auth/login`

Configure in `.env`:

```dotenv
NEXUSDL_API_RATE_LIMIT_PER_MINUTE=60
```

### API docs in production

Disable Swagger UI in production:

```dotenv
NEXUSDL_APP_ENVIRONMENT=prod
NEXUSDL_API_ENABLE_DOCS=false
```

### User data

- Passwords: **bcrypt** hashing (`passlib[bcrypt]`).
- Cookies: **Fernet** encryption (`cryptography`).
- JWTs: **HS256** by default; use **RS256** for production multi-service.

### CSRF

The API is **stateless** (JWT in `Authorization: Bearer`). CSRF does
**not** apply to stateless auth. The frontend stores the token in
**memory** (not `localStorage`) to prevent XSS token theft.

### CSP headers

The frontend ships with:

```
Content-Security-Policy: default-src 'self'; img-src 'self' data: https:; script-src 'self' 'unsafe-inline'
```

**Note**: `'unsafe-inline'` is required by Next.js. A stricter CSP is
planned for v0.2.0.

### Logging

The API logs:

- All auth events (login, logout, failed login)
- All downloads
- **No secrets** (passwords, tokens)

---

## Reverse proxy

### Caddy (recommended, HTTPS auto)

`Caddyfile`:

```caddyfile
nexusdl.example.com {
    # API
    handle /api/* {
        reverse_proxy nexusdl:8000
    }
    # WebSocket
    handle /ws/* {
        reverse_proxy nexusdl:8000
    }
    # Frontend
    handle {
        reverse_proxy nexusdl:3000
    }
}
```

Caddy automatically provisions **Let's Encrypt** HTTPS.

### Nginx (HTTPS manual)

`nginx.conf`:

```nginx
server {
    listen 443 ssl http2;
    server_name nexusdl.example.com;

    ssl_certificate     /etc/letsencrypt/live/nexusdl.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nexusdl.example.com/privkey.pem;

    client_max_body_size 500M;

    # API
    location /api/ {
        proxy_pass http://nexusdl:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://nexusdl:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    # Frontend
    location / {
        proxy_pass http://nexusdl:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Traefik

Labels (Docker Compose):

```yaml
services:
  nexusdl:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.nexusdl.rule=Host(`nexusdl.example.com`)"
      - "traefik.http.routers.nexusdl.tls.certresolver=letsencrypt"
      - "traefik.http.services.nexusdl.loadbalancer.server.port=3000"
```

### After setting up reverse proxy

Update `.env`:

```dotenv
NEXUSDL_PUBLIC_URL=https://nexusdl.example.com
NEXUSDL_API_BASE_URL=https://nexusdl.example.com
NEXUSDL_FRONTEND_URL=https://nexusdl.example.com
NEXUSDL_API_CORS_ORIGINS=["https://nexusdl.example.com"]
```

Restart the backend:

```bash
docker compose restart nexusdl
```

---

## Troubleshooting

### Blank page after login

**Cause**: frontend can't reach the API.

**Fix**:

1. Open the browser console (`F12`).
2. Check for **CORS errors** or **Network errors**.
3. Ensure `NEXUSDL_API_BASE_URL` is correct.
4. Ensure `NEXUSDL_API_CORS_ORIGINS` includes your frontend URL.

### `401 Unauthorized` on every request

**Cause**: token expired or invalid.

**Fix**:

1. Log out and log in again.
2. If persistent, clear browser storage (DevTools → Application →
   Storage → Clear).
3. Check `NEXUSDL_JWT_SECRET` hasn't changed.

### WebSocket doesn't connect

**Cause**: reverse proxy doesn't forward `/ws/*` with `Upgrade` headers.

**Fix**: see [Reverse proxy](#reverse-proxy) — ensure `proxy_http_version 1.1`,
`Upgrade`, `Connection "upgrade"`.

### Progress bar doesn't update

**Cause**: WebSocket disconnected, fallback polling disabled.

**Fix**:

1. Check connection indicator (top right).
2. Open browser console for WS errors.
3. Refresh the page.
4. Check reverse proxy WS config.

### Downloads stuck at 0%

**Cause**: backend can't reach the site (Cloudflare, rate limit).

**Fix**:

1. Check backend logs.
2. Enable Playwright or FlareSolverr (see [Configuration](configuration.md)).
3. Check site status in **Settings → Health check**.

### Login page loops

**Cause**: cookies blocked or `NEXUSDL_PUBLIC_URL` mismatch.

**Fix**:

1. Enable cookies in browser.
2. Ensure `NEXUSDL_PUBLIC_URL` matches the URL in the address bar
   (including `https://` vs `http://`).

### Can't register first user

**Cause**: a user already exists.

**Fix**:

1. Check the database:
   ```bash
   docker compose exec nexusdl sqlite3 /data/library.db "SELECT id, username, role FROM users;"
   ```
2. If you forgot the password, reset via CLI:
   ```bash
   docker compose exec nexusdl nexusdl users reset-password --username alice
   ```
3. Or delete the user (DANGER):
   ```bash
   docker compose exec nexusdl sqlite3 /data/library.db "DELETE FROM users WHERE username='alice';"
   ```

### PWA install button missing

**Cause**: PWA requires HTTPS (except `localhost`).

**Fix**: deploy behind HTTPS.

### Slow on mobile

**Cause**: large cover images, many results.

**Fix**:

1. Reduce results per page (Settings → Search → 20 per page).
2. Use "Data saver" mode in browser.
3. Ensure covers are cached (CDN, reverse proxy cache).

### Memory usage grows

**Cause**: WebSocket connections not closed, or Playwright contexts
leaking.

**Fix**:

1. Check `docker stats nexusdl`.
2. Restart the backend (`docker compose restart nexusdl`).
3. Report a bug if it recurs.

---

## See also

- [REST API](../api/rest.md) — full API reference
- [WebSocket API](../api/websocket.md) — real-time events
- [Configuration](configuration.md) — all env vars
- [Docker installation](../installation/docker.md) — production deployment
- [Formats](formats.md) — packaging formats
- [GUI usage](gui.md) — desktop alternative
- [CLI usage](cli.md) — command-line alternative
- [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) — ask for help

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
