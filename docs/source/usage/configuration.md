# Configuration

> **Audience**: users who want to configure NexusDL (CLI, TUI, GUI, Web,
> Docker).
>
> **Reference**: This page is the **source of truth** for all environment
> variables. For the architecture behind the split between `.env` and
> `sites.yaml`, see [ADR-004](../development/decisions/004-sites-yaml-vs-env.md).

NexusDL is configured via:

1. **`.env`** — environment variables (secrets, deployment, feature flags)
2. **`config.yaml`** — optional user overrides (rarely needed)
3. **`sites.yaml`** — site metadata (versioned, not user-editable)
4. **CLI arguments** — highest precedence

---

## Table of contents

1. [Configuration hierarchy](#configuration-hierarchy)
2. [Where files live](#where-files-live)
3. [Minimal configuration](#minimal-configuration)
4. [Environment variables reference](#environment-variables-reference)
5. [Secrets](#secrets)
6. [Feature flags](#feature-flags)
7. [Storage paths](#storage-paths)
8. [Logging](#logging)
9. [Rate limits and concurrency](#rate-limits-and-concurrency)
10. [Playwright and FlareSolverr](#playwright-and-flaresolverr)
11. [Proxy](#proxy)
12. [API and Web](#api-and-web)
13. [Site-specific credentials](#site-specific-credentials)
14. [Advanced patterns](#advanced-patterns)
15. [Troubleshooting](#troubleshooting)

---

## Configuration hierarchy

NexusDL reads configuration in **this order** (later overrides earlier):

```
1. Default values         (hardcoded in core/config.py)
   ↓
2. config/config.yaml     (optional, user overrides)
   ↓
3. .env file              (secrets, deployment)
   ↓
4. Environment variables  (NEXUSDL_*)
   ↓
5. CLI arguments          (--log-level, --dest, etc.)
```

**Example** — `NEXUSDL_LOG_LEVEL` set in 3 places:

```dotenv
# .env
NEXUSDL_LOG_LEVEL=INFO
```

```bash
# Environment variable
export NEXUSDL_LOG_LEVEL=DEBUG
```

```bash
# CLI argument (highest)
nexusdl --log-level ERROR search "one piece"
```

**Result**: `ERROR` wins.

---

## Where files live

NexusDL uses `platformdirs` for cross-platform paths.

| File | Linux | macOS | Windows |
|---|---|---|---|
| **Config** | `~/.config/nexusdl/` | `~/Library/Application Support/nexusdl/` | `%APPDATA%\nexusdl\` |
| **Data** | `~/.local/share/nexusdl/` | `~/Library/Application Support/nexusdl/` | `%LOCALAPPDATA%\nexusdl\` |
| **Cache** | `~/.cache/nexusdl/` | `~/Library/Caches/nexusdl/` | `%LOCALAPPDATA%\nexusdl\cache\` |
| **Logs** | `~/.local/state/nexusdl/logs/` | `~/Library/Logs/nexusdl/` | `%LOCALAPPDATA%\nexusdl\logs\` |
| **Downloads** | `~/Downloads/NexusDL/` | `~/Downloads/NexusDL/` | `%USERPROFILE%\Downloads\NexusDL\` |

### Files

| File | Location | Purpose |
|---|---|---|
| `.env` | config dir | Environment variables (secrets, deployment) |
| `config.yaml` | config dir | Optional user overrides |
| `library.db` | data dir | SQLite database (library, progress) |
| `cookies.enc` | data dir | Encrypted cookies |
| `cache/` | cache dir | HTTP cache (hishel) |
| `logs/nexusdl.log` | logs dir | Main log file |
| `sites.yaml` | package data | Site metadata (**not** user-editable) |

### Display current paths

```bash
nexusdl config paths
```

Output:

```
Config:     /home/alice/.config/nexusdl
Data:       /home/alice/.local/share/nexusdl
Cache:      /home/alice/.cache/nexusdl
Logs:       /home/alice/.local/state/nexusdl/logs
Downloads:  /home/alice/Downloads/NexusDL
```

---

## Minimal configuration

NexusDL **requires only three secrets** to start:

```dotenv
NEXUSDL_SECRET_KEY=<64-hex>
NEXUSDL_JWT_SECRET=<64-hex>
NEXUSDL_COOKIE_ENCRYPTION_KEY=<64-hex>
```

Generate them with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# Run 3 times, use each value once
```

Everything else has sensible defaults.

### Create the minimal `.env`

```bash
mkdir -p ~/.config/nexusdl
cat > ~/.config/nexusdl/.env << 'EOF'
NEXUSDL_SECRET_KEY=<paste-1>
NEXUSDL_JWT_SECRET=<paste-2>
NEXUSDL_COOKIE_ENCRYPTION_KEY=<paste-3>
EOF
chmod 600 ~/.config/nexusdl/.env
```

That's enough to run `nexusdl search "..."`.

---

## Environment variables reference

All variables are prefixed with **`NEXUSDL_`**.

### Application

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_APP_NAME` | `str` | `NexusDL` | Display name |
| `NEXUSDL_APP_VERSION` | `str` | (from package) | App version |
| `NEXUSDL_APP_ENVIRONMENT` | `dev` \| `staging` \| `prod` | `dev` | Deployment env |
| `NEXUSDL_PUBLIC_URL` | `HttpUrl` | (required in prod) | Public frontend URL |
| `NEXUSDL_API_BASE_URL` | `HttpUrl` | (required in prod) | Public API URL |
| `NEXUSDL_FRONTEND_URL` | `HttpUrl` | (required in prod) | Frontend URL |

### Security

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_SECRET_KEY` | `SecretStr` | (required) | Global secret |
| `NEXUSDL_JWT_SECRET` | `SecretStr` | (required) | JWT signing secret |
| `NEXUSDL_JWT_ALGORITHM` | `str` | `HS256` | JWT algorithm (`HS256`, `RS256`) |
| `NEXUSDL_JWT_EXPIRE_MINUTES` | `int` | `60` | JWT expiry |
| `NEXUSDL_COOKIE_ENCRYPTION_KEY` | `SecretStr` | (required) | Fernet key (cookies) |

### Database

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_DATABASE_URL` | `str` | `sqlite+aiosqlite:///./data/library.db` | DB URL |
| `NEXUSDL_DATABASE_ECHO` | `bool` | `false` | Log SQL (dev only) |

### HTTP

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_HTTP_TIMEOUT_SECONDS` | `float` | `30.0` | Request timeout |
| `NEXUSDL_HTTP_MAX_RETRIES` | `int` | `3` | Max retries |
| `NEXUSDL_HTTP_USER_AGENT` | `str` | (from UA pool) | Custom User-Agent |
| `NEXUSDL_RESPECT_ROBOTS_TXT` | `bool` | `true` | Enforce `robots.txt` |

### Rate limits

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_DEFAULT_RATE_LIMIT_PER_SECOND` | `float` | `2.0` | Default per-site rate limit |
| `NEXUSDL_MAX_CONCURRENT_DOWNLOADS` | `int` | `3` | Max parallel tasks |
| `NEXUSDL_MAX_CONCURRENT_PAGES_PER_CHAPTER` | `int` | `8` | Max parallel pages |

### Playwright

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_PLAYWRIGHT_ENABLED` | `bool` | `false` | Enable Playwright |
| `NEXUSDL_PLAYWRIGHT_HEADLESS` | `bool` | `true` | Headless Chromium |
| `NEXUSDL_PLAYWRIGHT_MAX_CONTEXTS` | `int` | `4` | Max concurrent contexts |

### FlareSolverr

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_FLARESOLVERR_URL` | `HttpUrl \| None` | `None` | FlareSolverr endpoint |
| `NEXUSDL_FLARESOLVERR_TIMEOUT_SECONDS` | `int` | `60` | FlareSolverr timeout |

### Proxy

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_PROXY_URL` | `str \| None` | `None` | Single proxy URL |
| `NEXUSDL_PROXY_FILE` | `Path \| None` | `None` | YAML proxy list |

### Content

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_ALLOW_ADULT` | `bool` | `false` | Enable adult sites |
| `NEXUSDL_ALLOW_VIOLENT` | `bool` | `true` | Allow violent/gore |
| `NEXUSDL_CONTENT_FILTER_STRICT` | `bool` | `false` | Whitelist mode |

### Storage

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_DOWNLOAD_DIR` | `Path` | platformdirs | Download directory |
| `NEXUSDL_TEMP_DIR` | `Path` | system temp | Temp directory |
| `NEXUSDL_LIBRARY_DB_PATH` | `Path` | platformdirs | SQLite DB path |
| `NEXUSDL_COOKIE_STORAGE_PATH` | `Path` | platformdirs | Encrypted cookies |
| `NEXUSDL_LOGS_DIR` | `Path` | platformdirs | Logs directory |

### Logging

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_LOG_LEVEL` | `TRACE` \| `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` | `INFO` | Log level |
| `NEXUSDL_LOG_FORMAT` | `json` \| `text` \| `pretty` | `pretty` | Log format |
| `NEXUSDL_LOG_ROTATION` | `str` | `10 MB` | Rotation size/time |
| `NEXUSDL_LOG_RETENTION` | `str` | `30 days` | Retention policy |

### API

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_API_CORS_ORIGINS` | `list[str]` | `[]` | CORS origins |
| `NEXUSDL_API_RATE_LIMIT_PER_MINUTE` | `int` | `60` | Per-IP rate limit |
| `NEXUSDL_API_ENABLE_DOCS` | `bool` | `true` in dev, `false` in prod | Swagger/ReDoc |
| `NEXUSDL_API_ALLOW_ANONYMOUS` | `bool` | `false` | Allow anonymous reads |

### Redis (optional)

| Variable | Type | Default | Description |
|---|---|---|---|
| `NEXUSDL_REDIS_URL` | `str \| None` | `None` | Redis URL |

---

## Secrets

Secrets are typed as `SecretStr` and **never** logged, serialized, or
exposed via the API.

### Generate a secret

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# → 8a3f...  (64 hex chars = 32 bytes)
```

### Where to put secrets

**In `.env`** (never in `config.yaml`, never in code):

```dotenv
NEXUSDL_SECRET_KEY=8a3f...
NEXUSDL_JWT_SECRET=2b7c...
NEXUSDL_COOKIE_ENCRYPTION_KEY=4d9e...
```

### Rotate a secret

| Secret | Rotation | Effect |
|---|---|---|
| `SECRET_KEY` | Safe anytime | Invalidates signed sessions |
| `JWT_SECRET` | Safe anytime | Invalidates all JWTs (users re-login) |
| `COOKIE_ENCRYPTION_KEY` | **Requires deleting `cookies.enc`** | Old cookies become unreadable |

**To rotate `COOKIE_ENCRYPTION_KEY`**:

```bash
# 1. Delete the cookie store (users will need to re-authenticate)
rm ~/.local/share/nexusdl/cookies.enc

# 2. Change the key in .env
# 3. Restart NexusDL
```

### Verify a secret is set

```bash
nexusdl config show
```

Output (secrets masked):

```
app_name: NexusDL
app_environment: dev
secret_key: **********
jwt_secret: **********
cookie_encryption_key: **********
...
```

### Docker secrets

Use `_FILE` suffixes (Pydantic Settings supports them):

```yaml
# docker-compose.yml
services:
  nexusdl:
    secrets:
      - jwt_secret
      - cookie_key

secrets:
  jwt_secret:
    file: ./secrets/jwt_secret.txt
  cookie_key:
    file: ./secrets/cookie_key.txt
```

```dotenv
# .env
NEXUSDL_JWT_SECRET_FILE=/run/secrets/jwt_secret
NEXUSDL_COOKIE_ENCRYPTION_KEY_FILE=/run/secrets/cookie_key
```

---

## Feature flags

Feature flags control **what NexusDL does**, not **how**.

### `NEXUSDL_ALLOW_ADULT`

**Default**: `false`

Enable adult sites (18+). This is **both** a metadata filter (only sites
with `adult: true` are loaded) **and** a policy decision (you assume
responsibility).

```dotenv
NEXUSDL_ALLOW_ADULT=true
```

**Effect**: `nexusdl sites --include-adult` returns adult sites.
`nexusdl search "..." --include-adult` includes them.

**Note**: `sites.yaml[nhentai].adult: true` is **metadata** (the site IS
adult). `NEXUSDL_ALLOW_ADULT` is **policy** (this deployment allows
adult). Both must be true.

### `NEXUSDL_RESPECT_ROBOTS_TXT`

**Default**: `true`

Enforce `robots.txt` before each request. Disabling it means **you** assume
responsibility for respecting the site's terms.

```dotenv
NEXUSDL_RESPECT_ROBOTS_TXT=false   # ⚠️ Not recommended
```

**Effect**: requests to paths disallowed by `robots.txt` are skipped.

### `NEXUSDL_PLAYWRIGHT_ENABLED`

**Default**: `false`

Enable Playwright (headless Chromium) for JS-rendered or Cloudflare sites.

```dotenv
NEXUSDL_PLAYWRIGHT_ENABLED=true
```

**Requires**: `playwright install chromium`.

### `NEXUSDL_API_ENABLE_DOCS`

**Default**: `true` in `dev`, `false` in `prod`

Enable `/docs` (Swagger UI) and `/redoc`.

```dotenv
NEXUSDL_APP_ENVIRONMENT=prod
NEXUSDL_API_ENABLE_DOCS=false
```

**Security**: in prod, disable it or protect it with auth.

### `NEXUSDL_API_ALLOW_ANONYMOUS`

**Default**: `false`

Allow `GUEST` role to access `/sites`, `/search`, `/manga`, `/chapters`
without auth.

```dotenv
NEXUSDL_API_ALLOW_ANONYMOUS=true
```

**Use case**: public read-only NexusDL instance.

### `NEXUSDL_CONTENT_FILTER_STRICT`

**Default**: `false`

Whitelist mode. Only mangas with genres in a configured allowlist are
downloaded.

```dotenv
NEXUSDL_CONTENT_FILTER_STRICT=true
```

**Note**: not yet implemented in `core/compliance.py` (planned).

---

## Storage paths

Override default paths in `.env`:

```dotenv
NEXUSDL_DOWNLOAD_DIR=/mnt/data/Manga
NEXUSDL_LIBRARY_DB_PATH=/mnt/data/nexusdl/library.db
NEXUSDL_COOKIE_STORAGE_PATH=/mnt/data/nexusdl/cookies.enc
NEXUSDL_TEMP_DIR=/mnt/data/tmp
NEXUSDL_LOGS_DIR=/var/log/nexusdl
```

### Path rules

- **Use absolute paths** — `~` is expanded, but relative paths are
  relative to the **cwd**, not the config dir.
- **Windows**: use forward slashes (`C:/Manga`) or escaped backslashes
  (`C:\\Manga`), never `C:\Manga` (escape).
- **Create the directory first** — NexusDL won't `mkdir` recursively
  unless `parents=True` is safe.

### Example — download to external drive

```dotenv
# Linux
NEXUSDL_DOWNLOAD_DIR=/mnt/external/Manga

# macOS
NEXUSDL_DOWNLOAD_DIR=/Volumes/ExternalDrive/Manga

# Windows
NEXUSDL_DOWNLOAD_DIR=E:/Manga
```

### Permissions

The download directory must be **writable** by the NexusDL process.

**Linux/macOS**:

```bash
mkdir -p /mnt/data/Manga
chmod 755 /mnt/data/Manga
```

**Windows**:

```powershell
New-Item -ItemType Directory -Force -Path "E:\Manga"
```

---

## Logging

NexusDL uses **loguru** with structured logging.

### Log levels

| Level | When |
|---|---|
| `TRACE` | Very verbose (HTTP headers, parsed HTML) |
| `DEBUG` | Verbose (requests, parser steps) |
| `INFO` | Default (search results, download progress) |
| `WARNING` | Degraded (rate limit hit, fallback) |
| `ERROR` | Failure (site down, download failed) |
| `CRITICAL` | Fatal (config invalid, DB corrupted) |

```dotenv
NEXUSDL_LOG_LEVEL=INFO
```

### Log formats

| Format | Use case |
|---|---|
| `pretty` | Human-readable, colored (default in dev) |
| `text` | Plain text (log aggregators) |
| `json` | Structured JSON (ELK, Loki, Datadog) |

```dotenv
NEXUSDL_LOG_FORMAT=json
```

### Rotation and retention

```dotenv
NEXUSDL_LOG_ROTATION=10 MB
NEXUSDL_LOG_RETENTION=30 days
```

Or by time:

```dotenv
NEXUSDL_LOG_ROTATION=1 day
NEXUSDL_LOG_RETENTION=90 days
```

### Where logs are written

- **File**: `<logs_dir>/nexusdl.log`
- **stdout**: if `NEXUSDL_LOG_FORMAT` is `text` or `pretty`
- **stderr**: errors only

### Example — JSON logs for Loki

```dotenv
NEXUSDL_LOG_LEVEL=INFO
NEXUSDL_LOG_FORMAT=json
```

Log line:

```json
{"text":"Downloaded chapter 1","record":{"time":{"repr":"2026-10-28T12:00:00Z"},"level":{"name":"INFO"},"extra":{"task_id":"...","manga":"One Piece"}}}
```

---

## Rate limits and concurrency

### Global concurrency

```dotenv
NEXUSDL_MAX_CONCURRENT_DOWNLOADS=3
```

Limits the number of **parallel download tasks** (mangas). Default `3`.

**Recommendation**:

- **1** for very slow internet
- **3** for most users
- **5–8** for fast connections (may trigger site rate limits)

### Per-chapter page concurrency

```dotenv
NEXUSDL_MAX_CONCURRENT_PAGES_PER_CHAPTER=8
```

Limits **parallel page downloads within one chapter**. Default `8`.

**Recommendation**:

- **4** for fragile sites
- **8** for most sites
- **16** for fast APIs (rare)

### Per-site rate limit

Set in `sites.yaml` (not in `.env`):

```yaml
capabilities:
  default_rate_limit_per_second: 2.0   # 2 requests per second
```

Global default (if not overridden in `sites.yaml`):

```dotenv
NEXUSDL_DEFAULT_RATE_LIMIT_PER_SECOND=2.0
```

### Combined effect

For a chapter with 50 pages:

- **Pages per second** ≈ `min(rate_limit, pages_per_chapter_concurrency × (1 / avg_latency))`
- **Chapters in parallel** ≈ `max_concurrent_downloads`

### Tuning tips

| Symptom | Fix |
|---|---|
| `429 Too Many Requests` | Reduce `MAX_CONCURRENT_DOWNLOADS` and `DEFAULT_RATE_LIMIT_PER_SECOND` |
| Downloads slow | Increase `MAX_CONCURRENT_PAGES_PER_CHAPTER` |
| Site bans IP | Reduce `DEFAULT_RATE_LIMIT_PER_SECOND`, add proxy |
| High CPU | Reduce `MAX_CONCURRENT_PAGES_PER_CHAPTER` (image validation is CPU-heavy) |

---

## Playwright and FlareSolverr

### Playwright

```dotenv
NEXUSDL_PLAYWRIGHT_ENABLED=true
NEXUSDL_PLAYWRIGHT_HEADLESS=true
NEXUSDL_PLAYWRIGHT_MAX_CONTEXTS=4
```

Install the browser:

```bash
playwright install chromium
```

**When to enable**: JS-rendered sites (React, Vue, Angular SPAs) or
Cloudflare Turnstile.

**Resource usage**: ~500 MB RAM per context. Reduce
`MAX_CONTEXTS` on low-RAM systems.

### FlareSolverr

```dotenv
NEXUSDL_FLARESOLVERR_URL=http://localhost:8191/v1
NEXUSDL_FLARESOLVERR_TIMEOUT_SECONDS=60
```

Run FlareSolverr:

```bash
docker run -d --name flaresolverr \
  -p 8191:8191 \
  ghcr.io/flaresolverr/flaresolverr:latest
```

**When to enable**: Cloudflare-protected sites where Playwright fails.

**Trade-off**: FlareSolverr runs a browser **outside** the NexusDL process
— higher resource use but better Cloudflare compatibility.

### Both together

**Recommended**: enable **both**. NexusDL tries FlareSolverr first (if
configured), falls back to Playwright (if enabled), falls back to plain
HTTP.

---

## Proxy

### Single proxy

```dotenv
NEXUSDL_PROXY_URL=http://user:pass@proxy.example.com:8080
```

Or SOCKS5:

```dotenv
NEXUSDL_PROXY_URL=socks5://proxy.example.com:1080
```

### Proxy file (rotation)

```dotenv
NEXUSDL_PROXY_FILE=/etc/nexusdl/proxies.yaml
```

```yaml
# /etc/nexusdl/proxies.yaml
proxies:
  - url: http://user1:pass@proxy1.example.com:8080
    region: eu
  - url: http://user2:pass@proxy2.example.com:8080
    region: us
  - url: socks5://proxy3.example.com:1080
    region: asia
```

NexusDL rotates proxies **round-robin** and falls back on failure.

**See**: `config/proxy.example.yaml` for the full schema.

---

## API and Web

### CORS origins

```dotenv
NEXUSDL_API_CORS_ORIGINS=["https://nexusdl.example.com","http://localhost:3000"]
```

**Note**: must be a **JSON array**, not comma-separated.

### API rate limit

```dotenv
NEXUSDL_API_RATE_LIMIT_PER_MINUTE=60
```

Per-IP rate limit on the API. Authenticated users get 10× this.

### Public URLs (required in prod)

```dotenv
NEXUSDL_APP_ENVIRONMENT=prod
NEXUSDL_PUBLIC_URL=https://nexusdl.example.com
NEXUSDL_API_BASE_URL=https://nexusdl.example.com
NEXUSDL_FRONTEND_URL=https://nexusdl.example.com
```

These are used for:
- JWT `iss` / `aud` claims
- OAuth redirect URLs (future)
- CORS preflight
- OpenAPI server URL

### Disable docs in prod

```dotenv
NEXUSDL_APP_ENVIRONMENT=prod
NEXUSDL_API_ENABLE_DOCS=false
```

---

## Site-specific credentials

For sites that require authentication (`requires_auth: true` in
`sites.yaml`), provide credentials in `.env`:

```dotenv
# nhentai (requires cookie, not credentials)
NEXUSDL_NHENTAI_CF_CLEARANCE=<value-from-browser>

# MangaDex (requires OAuth client)
NEXUSDL_MANGADEX_CLIENT_ID=<id>
NEXUSDL_MANGADEX_CLIENT_SECRET=<secret>

# Hypothetical site with login
NEXUSDL_MYSITE_USERNAME=alice
NEXUSDL_MYSITE_PASSWORD=<password>
```

### Naming convention

```
NEXUSDL_<SITE_ID_UPPERCASE>_<FIELD_UPPERCASE>
```

Where `<FIELD>` is one of:
- `USERNAME`, `PASSWORD` (login)
- `CLIENT_ID`, `CLIENT_SECRET` (OAuth)
- Cookie names in **UPPER_SNAKE** (`CF_CLEARANCE`, `CF_BM`)

### Cookies

`sites.yaml` declares **cookie names** only:

```yaml
cookies_required:
  - cf_clearance
  - cf_bm
```

`.env` provides **values**:

```dotenv
NEXUSDL_NHENTAI_CF_CLEARANCE=<value>
NEXUSDL_NHENTAI_CF_BM=<value>
```

See [ADR-004](../development/decisions/004-sites-yaml-vs-env.md).

### How to extract cookies from the browser

1. Open the site in **Firefox** or **Chrome**.
2. Open DevTools (`F12`) → **Application** (Chrome) or **Storage** (Firefox).
3. Navigate to **Cookies** → the site.
4. Find the cookie name (`cf_clearance`, `cf_bm`, etc.).
5. Copy the **Value**.
6. Paste into `.env` as `NEXUSDL_<SITE>_<COOKIE>=<value>`.

**Warning**: cookies expire. Refresh them every few days/weeks.

---

## Advanced patterns

### Systemd service (Linux)

`~/.config/systemd/user/nexusdl-web.service`:

```ini
[Unit]
Description=NexusDL Web
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h
EnvironmentFile=%h/.config/nexusdl/.env
ExecStart=%h/.local/bin/nexusdl-web
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Enable:

```bash
systemctl --user daemon-reload
systemctl --user enable --now nexusdl-web
systemctl --user status nexusdl-web
journalctl --user -u nexusdl-web -f
```

### Cron job (scheduled downloads)

`~/.config/nexusdl/update.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

export NEXUSDL_CONFIG_DIR="$HOME/.config/nexusdl"

# Update all mangas in the library
nexusdl library update --all

# Notify on failure
if [[ $? -ne 0 ]]; then
    curl -X POST "$NEXUSDL_DISCORD_WEBHOOK" \
        -d '{"content":"NexusDL update failed"}'
fi
```

Cron (daily at 3am):

```cron
0 3 * * * ~/.config/nexusdl/update.sh >> ~/.local/state/nexusdl/update.log 2>&1
```

### Docker Compose (full stack)

See [Docker installation](../installation/docker.md).

### Kubernetes ConfigMap + Secret

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: nexusdl-config
data:
  NEXUSDL_APP_ENVIRONMENT: "prod"
  NEXUSDL_LOG_FORMAT: "json"
  NEXUSDL_ALLOW_ADULT: "false"
---
apiVersion: v1
kind: Secret
metadata:
  name: nexusdl-secrets
type: Opaque
stringData:
  NEXUSDL_SECRET_KEY: "..."
  NEXUSDL_JWT_SECRET: "..."
  NEXUSDL_COOKIE_ENCRYPTION_KEY: "..."
```

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nexusdl
spec:
  template:
    spec:
      containers:
        - name: nexusdl
          image: ghcr.io/voldigoad-holdings/nexus-dl-web:latest
          envFrom:
            - configMapRef:
                name: nexusdl-config
            - secretRef:
                name: nexusdl-secrets
```

### Multi-profile configuration

Keep multiple `.env` files and load by environment:

```bash
# Development
cp .env.example .env.dev
# Edit .env.dev
nexusdl --env-file .env.dev search "..."

# Production
cp .env.example .env.prod
# Edit .env.prod
nexusdl --env-file .env.prod search "..."
```

**Note**: `--env-file` support is planned for `nexusdl` CLI.

---

## Troubleshooting

### `NEXUSDL_SECRET_KEY is missing`

**Cause**: `.env` not found or not loaded.

**Fix**:

```bash
# Check where NexusDL looks for .env
nexusdl config paths

# Create the file
mkdir -p ~/.config/nexusdl
echo "NEXUSDL_SECRET_KEY=..." > ~/.config/nexusdl/.env
```

### `.env` not being read

**Cause**: `.env` is in the wrong directory.

**Fix**: NexusDL looks for `.env` in:
1. Current directory (`.env`)
2. Config directory (`~/.config/nexusdl/.env`)

Move `.env` to `~/.config/nexusdl/` or use `--env-file` (planned).

### `ValueError: Invalid boolean for ALLOW_ADULT`

**Cause**: value is not `true`/`false`/`1`/`0`.

**Fix**:

```dotenv
NEXUSDL_ALLOW_ADULT=false       # ✅
NEXUSDL_ALLOW_ADULT=False       # ✅
NEXUSDL_ALLOW_ADULT=0           # ✅
NEXUSDL_ALLOW_ADULT=no          # ❌
NEXUSDL_ALLOW_ADULT=""          # ❌
```

### `ValueError: Invalid URL for PUBLIC_URL`

**Cause**: missing scheme (`https://`).

**Fix**:

```dotenv
NEXUSDL_PUBLIC_URL=https://nexusdl.example.com   # ✅
NEXUSDL_PUBLIC_URL=nexusdl.example.com            # ❌
```

### CORS errors in browser console

**Cause**: `NEXUSDL_API_CORS_ORIGINS` doesn't include the frontend URL.

**Fix**:

```dotenv
NEXUSDL_API_CORS_ORIGINS=["https://nexusdl.example.com"]
```

Restart NexusDL.

### Rotating `COOKIE_ENCRYPTION_KEY` breaks cookies

**Cause**: old cookies were encrypted with the old key.

**Fix**: delete `cookies.enc` and re-authenticate.

```bash
rm ~/.local/share/nexusdl/cookies.enc
```

### `database is locked`

**Cause**: multiple NexusDL processes on the same DB.

**Fix**: run a single instance. Use `NEXUSDL_MAX_CONCURRENT_DOWNLOADS`
for parallelism.

### Logs are too verbose

**Cause**: `NEXUSDL_LOG_LEVEL=DEBUG` or `TRACE`.

**Fix**:

```dotenv
NEXUSDL_LOG_LEVEL=INFO
```

### Logs are JSON but I want text

**Fix**:

```dotenv
NEXUSDL_LOG_FORMAT=text
```

---

## See also

- [ADR-004: sites.yaml vs .env](../development/decisions/004-sites-yaml-vs-env.md) — the split explained
- [`sites.yaml`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/data/sites.yaml) — site metadata
- [`sites_schema.json`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/src/nexusdl/data/sites_schema.json) — schema
- [CLI usage](cli.md) — command reference
- [Formats](formats.md) — packaging formats
- [Docker installation](../installation/docker.md) — env vars in Docker
- [`config/config.example.yaml`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/config/config.example.yaml) — YAML override example
- [`config/proxy.example.yaml`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/config/proxy.example.yaml) — proxy file example
- [`config/logging.yaml`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/config/logging.yaml) — logging config

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
