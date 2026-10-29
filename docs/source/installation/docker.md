# Docker Installation

> **Audience**: users who want to self-host NexusDL with **Docker**.
> **Recommended for**: NAS, VPS, Raspberry Pi, home servers, cloud
> deployments.
>
> **Not recommended for**: desktop users who want a native GUI (use the
> [native install](../installation/linux.md) instead).

Docker is the **recommended** way to self-host NexusDL. The images are
**multi-arch** (`linux/amd64` + `linux/arm64`), **minimal** (non-root,
read-only filesystem where possible), and **reproducible** (pinned base
images).

---

## Table of contents

1. [Requirements](#requirements)
2. [Images](#images)
3. [Quick start (Docker Compose)](#quick-start-docker-compose)
4. [Quick start (Docker CLI)](#quick-start-docker-cli)
5. [Configuration](#configuration)
6. [Volumes and persistence](#volumes-and-persistence)
7. [Reverse proxy](#reverse-proxy)
8. [Upgrading](#upgrading)
9. [Backup and restore](#backup-and-restore)
10. [Security hardening](#security-hardening)
11. [Troubleshooting](#troubleshooting)
12. [See also](#see-also)

---

## Requirements

| Component | Minimum | Recommended |
|---|---|---|
| **Docker Engine** | 24.0+ | 26.0+ |
| **Docker Compose** | v2.20+ | v2.27+ |
| **RAM** | 512 MB | 2 GB |
| **Disk** | 2 GB (image + data) | 20 GB (for downloads) |
| **CPU** | 1 core | 2 cores |
| **Architecture** | `amd64` or `arm64` | — |

### Check your Docker installation

```bash
docker --version          # → Docker version 26.x.x
docker compose version    # → Docker Compose version v2.x.x
docker buildx version     # → github.com/docker/buildx v0.x.x
```

### Check your architecture

```bash
uname -m
# x86_64 → linux/amd64
# aarch64 or arm64 → linux/arm64
```

The images are **multi-arch**. Docker pulls the right one for your
architecture automatically.

---

## Images

NexusDL publishes **three images** to GitHub Container Registry (GHCR):

| Image | Tag | Size | Purpose |
|---|---|---|---|
| `ghcr.io/voldigoad-holdings/nexus-dl` | `latest`, `0.1.0` | ~180 MB | **Base** (CLI + TUI) |
| `ghcr.io/voldigoad-holdings/nexus-dl-web` | `latest`, `0.1.0` | ~350 MB | **Web** (FastAPI + Next.js) |
| `ghcr.io/voldigoad-holdings/nexus-dl-gui` | `latest`, `0.1.0` | ~400 MB | **GUI** (X11 forwarding) |

### Base image (`nexus-dl`)

Includes:

- Python 3.12
- NexusDL core + CLI + TUI (`pip install nexusdl[cli]`)
- No Playwright browsers (install separately if needed)
- Runs as user `nexusdl` (UID 1000)

**Use case**: scheduled downloads via cron, batch processing, CI.

### Web image (`nexus-dl-web`)

Includes:

- Everything in base
- FastAPI + Uvicorn + WebSocket
- Next.js frontend (built, static export)
- Nginx reverse proxy

**Use case**: self-hosted web UI, API for third-party clients.

### GUI image (`nexus-dl-gui`)

Includes:

- Everything in base
- CustomTkinter + X11 client
- Runs via **X11 forwarding** (needs `$DISPLAY`)

**Use case**: Docker-based Linux GUI (advanced).

```{note}
**Most users should use the web image** — it works in any browser,
no X11 forwarding needed.
```

---

## Quick start (Docker Compose)

The **easiest** way to run NexusDL.

### 1. Create a directory

```bash
mkdir -p ~/nexusdl && cd ~/nexusdl
```

### 2. Create `.env`

```bash
cat > .env << 'EOF'
# Secrets — generate with: python -c "import secrets; print(secrets.token_hex(32))"
NEXUSDL_SECRET_KEY=CHANGE_ME_64_HEX_CHARS
NEXUSDL_JWT_SECRET=CHANGE_ME_64_HEX_CHARS
NEXUSDL_COOKIE_ENCRYPTION_KEY=CHANGE_ME_64_HEX_CHARS

# Deployment URLs
NEXUSDL_PUBLIC_URL=http://localhost:3000
NEXUSDL_API_BASE_URL=http://localhost:8000
NEXUSDL_FRONTEND_URL=http://localhost:3000

# Feature flags
NEXUSDL_ALLOW_ADULT=false
NEXUSDL_LOG_LEVEL=INFO
NEXUSDL_LOG_FORMAT=json
EOF
chmod 600 .env
```

Generate three distinct secrets:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# Run 3 times, paste each value into .env
```

### 3. Create `docker-compose.yml`

```yaml
services:
  nexusdl:
    image: ghcr.io/voldigoad-holdings/nexus-dl-web:latest
    container_name: nexusdl
    restart: unless-stopped
    ports:
      - "8000:8000"   # API
      - "3000:3000"   # Web UI
    env_file:
      - .env
    volumes:
      - ./data:/data
      - ./config:/config
      - ./downloads:/downloads
    environment:
      - NEXUSDL_DOWNLOAD_DIR=/downloads
      - NEXUSDL_LIBRARY_DB_PATH=/data/library.db
      - NEXUSDL_COOKIE_STORAGE_PATH=/data/cookies.enc
      - NEXUSDL_LOGS_DIR=/data/logs
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    security_opt:
      - no-new-privileges:true
    read_only: false
    tmpfs:
      - /tmp:size=256m
```

### 4. Start

```bash
docker compose up -d
```

### 5. Verify

```bash
docker compose logs -f nexusdl
# Wait for: "Application startup complete."

curl http://localhost:8000/api/v1/health
# → {"status":"ok","version":"0.1.0"}
```

### 6. Open the web UI

```
http://localhost:3000
```

You should see the NexusDL web interface.

---

## Quick start (Docker CLI)

If you don't want Docker Compose:

```bash
# 1. Create volumes
docker volume create nexusdl-data
docker volume create nexusdl-config
docker volume create nexusdl-downloads

# 2. Run the web image
docker run -d \
  --name nexusdl \
  --restart unless-stopped \
  -p 8000:8000 \
  -p 3000:3000 \
  --env-file .env \
  -v nexusdl-data:/data \
  -v nexusdl-config:/config \
  -v nexusdl-downloads:/downloads \
  -e NEXUSDL_DOWNLOAD_DIR=/downloads \
  -e NEXUSDL_LIBRARY_DB_PATH=/data/library.db \
  -e NEXUSDL_COOKIE_STORAGE_PATH=/data/cookies.enc \
  --security-opt no-new-privileges:true \
  ghcr.io/voldigoad-holdings/nexus-dl-web:latest

# 3. Verify
docker logs -f nexusdl
```

### CLI-only usage

For batch downloads, use the **base image**:

```bash
docker run --rm \
  --env-file .env \
  -v nexusdl-downloads:/downloads \
  -e NEXUSDL_DOWNLOAD_DIR=/downloads \
  ghcr.io/voldigoad-holdings/nexus-dl:latest \
  nexusdl download "https://mangadex.org/title/..." --format cbz
```

### TUI usage

```bash
docker run --rm -it \
  --env-file .env \
  -v nexusdl-downloads:/downloads \
  -e NEXUSDL_DOWNLOAD_DIR=/downloads \
  ghcr.io/voldigoad-holdings/nexus-dl:latest \
  nexusdl-tui
```

---

## Configuration

All configuration goes through **environment variables** (`.env`). See
[Configuration](../usage/configuration.md) for the full reference.

### Minimal `.env`

```dotenv
# Required secrets
NEXUSDL_SECRET_KEY=<64-hex>
NEXUSDL_JWT_SECRET=<64-hex>
NEXUSDL_COOKIE_ENCRYPTION_KEY=<64-hex>

# Deployment
NEXUSDL_PUBLIC_URL=http://localhost:3000
NEXUSDL_API_BASE_URL=http://localhost:8000
NEXUSDL_FRONTEND_URL=http://localhost:3000
```

### Optional `.env` overrides

```dotenv
# Content
NEXUSDL_ALLOW_ADULT=false
NEXUSDL_CONTENT_FILTER_STRICT=false

# Runtime
NEXUSDL_LOG_LEVEL=INFO
NEXUSDL_LOG_FORMAT=json
NEXUSDL_MAX_CONCURRENT_DOWNLOADS=3
NEXUSDL_HTTP_TIMEOUT_SECONDS=30

# Playwright (optional — see "Playwright" below)
NEXUSDL_PLAYWRIGHT_ENABLED=false

# FlareSolverr (optional)
# NEXUSDL_FLARESOLVERR_URL=http://flaresolverr:8191/v1

# Site credentials (optional)
# NEXUSDL_NHENTAI_CF_CLEARANCE=<value>
# NEXUSDL_MANGADEX_CLIENT_ID=<id>
# NEXUSDL_MANGADEX_CLIENT_SECRET=<secret>
```

### Environment variable precedence

Docker Compose reads `.env` from the **same directory** as
`docker-compose.yml`. The `env_file:` directive loads it explicitly.
Environment variables set via `environment:` override `.env`.

**Order (highest to lowest):**

1. `environment:` in `docker-compose.yml`
2. `env_file:` (your `.env`)
3. Default values in `core/config.py`

---

## Volumes and persistence

NexusDL writes to **three directories**. Mount them as volumes to persist
data across container restarts.

| Volume | Container path | Purpose |
|---|---|---|
| `nexusdl-data` | `/data` | SQLite DB, encrypted cookies, logs |
| `nexusdl-config` | `/config` | User overrides (`config.yaml`) |
| `nexusdl-downloads` | `/downloads` | Downloaded mangas |

### With Docker Compose (bind mounts)

```yaml
volumes:
  - ./data:/data
  - ./config:/config
  - ./downloads:/downloads
```

Data is stored in `./data/`, `./config/`, `./downloads/` next to
`docker-compose.yml`. **Easy to backup** (`tar czf backup.tar.gz data/ config/ downloads/`).

### With Docker CLI (named volumes)

```bash
docker volume create nexusdl-data
docker volume create nexusdl-config
docker volume create nexusdl-downloads

docker run -d \
  -v nexusdl-data:/data \
  -v nexusdl-config:/config \
  -v nexusdl-downloads:/downloads \
  ...
```

**Backup** a named volume:

```bash
docker run --rm \
  -v nexusdl-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/nexusdl-data.tar.gz -C /data .
```

### Permissions

The container runs as user `nexusdl` (UID **1000**, GID **1000**). The
volumes must be writable by UID 1000.

**Bind mounts** — ensure the host directories are owned by UID 1000:

```bash
sudo chown -R 1000:1000 ./data ./config ./downloads
```

Or run the container as your host user:

```yaml
services:
  nexusdl:
    user: "${UID}:${GID}"   # requires UID/GID in your shell env
```

**Named volumes** — Docker handles permissions automatically.

---

## Reverse proxy

To expose NexusDL on a **domain** with HTTPS, put a reverse proxy in front.

### Caddy (simplest)

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
    # Web UI
    handle {
        reverse_proxy nexusdl:3000
    }
}
```

`docker-compose.yml` (add):

```yaml
services:
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config

volumes:
  caddy-data:
  caddy-config:
```

Caddy handles **HTTPS automatically** (Let's Encrypt).

### Nginx

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

    # Web UI
    location / {
        proxy_pass http://nexusdl:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

**Important**: after putting a reverse proxy in front, **update** `.env`:

```dotenv
NEXUSDL_PUBLIC_URL=https://nexusdl.example.com
NEXUSDL_API_BASE_URL=https://nexusdl.example.com
NEXUSDL_FRONTEND_URL=https://nexusdl.example.com
NEXUSDL_API_CORS_ORIGINS=["https://nexusdl.example.com"]
```

Then restart:

```bash
docker compose restart nexusdl
```

---

## Upgrading

### Docker Compose

```bash
cd ~/nexusdl

# 1. Pull the new image
docker compose pull

# 2. Recreate the container
docker compose up -d

# 3. Check logs
docker compose logs -f nexusdl
```

Your data is preserved (volumes).

### Docker CLI

```bash
# 1. Pull
docker pull ghcr.io/voldigoad-holdings/nexus-dl-web:latest

# 2. Stop and remove (data is in volumes)
docker stop nexusdl
docker rm nexusdl

# 3. Re-run with the same volumes
docker run -d --name nexusdl ... ghcr.io/voldigoad-holdings/nexus-dl-web:latest
```

### Rollback

If an upgrade breaks something, pin to a specific version:

```yaml
services:
  nexusdl:
    image: ghcr.io/voldigoad-holdings/nexus-dl-web:0.1.0   # ← specific tag
```

Available tags: `latest`, `0.1.0`, `0.1`, `0.1.0-rc1`, etc.

---

## Backup and restore

### Backup

```bash
#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="$HOME/nexusdl-backups"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d-%H%M%S)

# Stop the container (avoids SQLite corruption)
docker compose -f ~/nexusdl/docker-compose.yml stop nexusdl

# Archive data + config (not downloads, they're large)
tar czf "$BACKUP_DIR/nexusdl-backup-$DATE.tar.gz" \
  -C ~/nexusdl \
  data/ config/

# Restart
docker compose -f ~/nexusdl/docker-compose.yml start nexusdl

echo "Backup: $BACKUP_DIR/nexusdl-backup-$DATE.tar.gz"
```

### Restore

```bash
# 1. Stop
docker compose -f ~/nexusdl/docker-compose.yml down

# 2. Restore
cd ~/nexusdl
tar xzf ~/nexusdl-backups/nexusdl-backup-20261028-120000.tar.gz

# 3. Fix permissions
sudo chown -R 1000:1000 ./data ./config

# 4. Restart
docker compose up -d
```

### Automated backups

Add a cron job:

```cron
0 3 * * * /home/user/scripts/nexusdl-backup.sh >> /var/log/nexusdl-backup.log 2>&1
```

---

## Security hardening

### Non-root user (already default)

The image runs as user `nexusdl` (UID 1000). **Never** run as root:

```yaml
# ❌ WRONG
user: "0:0"

# ✅ RIGHT (default, no need to specify)
```

### Read-only filesystem

Mount the container's filesystem as read-only where possible:

```yaml
services:
  nexusdl:
    read_only: true
    tmpfs:
      - /tmp:size=256m
      - /run:size=64m
    volumes:
      - ./data:/data
      - ./config:/config
      - ./downloads:/downloads
```

### Drop capabilities

```yaml
services:
  nexusdl:
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE  # only if listening on port < 1024
```

### `no-new-privileges`

```yaml
services:
  nexusdl:
    security_opt:
      - no-new-privileges:true
```

### Secrets management

- **Never** commit `.env` to Git.
- Use `chmod 600 .env` on the host.
- Consider **Docker secrets** or **HashiCorp Vault** for production:

```yaml
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

Then in `.env`:

```dotenv
NEXUSDL_JWT_SECRET_FILE=/run/secrets/jwt_secret
NEXUSDL_COOKIE_ENCRYPTION_KEY_FILE=/run/secrets/cookie_key
```

(Pydantic Settings supports `_FILE` suffixes for secret files.)

### Network isolation

```yaml
services:
  nexusdl:
    networks:
      - internal
  caddy:
    networks:
      - internal
      - public

networks:
  internal:
    internal: true
  public:
```

The NexusDL container has **no direct internet exposure** — only Caddy can reach it.

### HTTPS only

- Always use **HTTPS** in production (Caddy or Nginx + Let's Encrypt).
- Set `NEXUSDL_PUBLIC_URL` and `NEXUSDL_API_BASE_URL` to `https://`.
- Enable **HSTS** in your reverse proxy.

---

## Troubleshooting

### Container exits immediately

```bash
docker compose logs nexusdl
```

Common causes:

| Error | Cause | Fix |
|---|---|---|
| `NEXUSDL_JWT_SECRET is missing` | `.env` not loaded | Check `env_file:` in compose |
| `Permission denied: /data/library.db` | Volume owned by root | `sudo chown -R 1000:1000 ./data` |
| `address already in use` | Port 8000/3000 taken | Change `ports:` or stop the other service |

### `curl: (7) Failed to connect`

The container may still be starting:

```bash
docker compose ps
docker compose logs -f nexusdl
```

Wait for `Application startup complete.` (5–30 seconds).

### Healthcheck fails

```bash
docker compose exec nexusdl curl http://localhost:8000/api/v1/health
```

If it works manually but not via healthcheck, the `HEALTHCHECK` command may be missing. Check the Dockerfile.

### Data not persisting

```bash
docker compose down
docker volume ls | grep nexusdl
docker compose up -d
```

If the volume is missing, the `volumes:` directive in `docker-compose.yml`
is wrong.

### Downloads fail with 403

The site is blocking the container's IP. Solutions:

1. **Add a proxy**:

   ```dotenv
   NEXUSDL_PROXY_URL=http://user:pass@proxy.example.com:8080
   ```

2. **Enable Playwright** (for Cloudflare sites):

   ```yaml
   services:
     nexusdl:
       environment:
         - NEXUSDL_PLAYWRIGHT_ENABLED=true
   ```

   Then install the browser **inside the container**:

   ```bash
   docker compose exec nexusdl playwright install chromium
   ```

   Or use a **custom Dockerfile** with Playwright pre-installed.

3. **Enable FlareSolverr** (see below).

### FlareSolverr setup

Add FlareSolverr as a sidecar:

```yaml
services:
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    restart: unless-stopped
    environment:
      - LOG_LEVEL=info
    networks:
      - internal

  nexusdl:
    # ...
    environment:
      - NEXUSDL_FLARESOLVERR_URL=http://flaresolverr:8191/v1
    networks:
      - internal
```

### Playwright browsers missing

The base image **does not** include Playwright browsers (to keep it small).
Install them inside the container:

```bash
docker compose exec nexusdl playwright install --with-deps chromium
```

**This is ephemeral** — the browsers are lost when the container is recreated.

**Solution**: build a **custom image**:

```dockerfile
FROM ghcr.io/voldigoad-holdings/nexus-dl-web:latest

USER root
RUN playwright install --with-deps chromium
USER nexusdl
```

Then in `docker-compose.yml`:

```yaml
services:
  nexusdl:
    build: .
    image: nexusdl-custom:latest
```

### Container is slow

Check resource usage:

```bash
docker stats nexusdl
```

If RAM is at 100%:

- Increase Docker memory (Docker Desktop → Settings → Resources).
- Reduce `NEXUSDL_MAX_CONCURRENT_DOWNLOADS` (default 3).
- Reduce `NEXUSDL_MAX_CONCURRENT_PAGES_PER_CHAPTER` (default 8).

If CPU is at 100%:

- `NEXUSDL_LOG_LEVEL=WARNING` (less I/O).
- `NEXUSDL_PLAYWRIGHT_ENABLED=false` if not needed.

### WebSocket doesn't work

If the web UI doesn't show live progress:

1. Check the browser console for `WebSocket connection failed`.
2. Ensure the reverse proxy forwards `/ws/*` with `Upgrade` headers (see [Reverse proxy](#reverse-proxy)).
3. Ensure `NEXUSDL_API_CORS_ORIGINS` includes your frontend URL.

### ARM64 issues

On Raspberry Pi 4/5 or Apple Silicon:

```bash
docker pull ghcr.io/voldigoad-holdings/nexus-dl-web:latest
docker inspect ghcr.io/voldigoad-holdings/nexus-dl-web:latest | grep Architecture
# → "Architecture": "arm64"
```

If you see `amd64` on an ARM host, Docker is using emulation (slow). Force the right architecture:

```bash
docker pull --platform linux/arm64 ghcr.io/voldigoad-holdings/nexus-dl-web:latest
```

---

## See also

- [Configuration](../usage/configuration.md) — all env vars
- [Formats](../usage/formats.md) — packaging formats
- [Web usage](../usage/web.md) — the web UI
- [CLI usage](../usage/cli.md) — the CLI
- [Native Linux install](linux.md) — install without Docker
- [Native macOS install](macos.md)
- [Native Windows install](windows.md)
- [Docker Compose file](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docker/docker-compose.yml) — the reference
- [Dockerfile](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docker/Dockerfile) — the base image
- [Dockerfile.web](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/docker/Dockerfile.web) — the web image

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
