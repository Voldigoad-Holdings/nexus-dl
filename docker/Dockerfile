# syntax=docker/dockerfile:1.7-labs
# ════════════════════════════════════════════════════════════════════
# NexusDL — Base Dockerfile (CLI + TUI)
#
# Fichier : docker/Dockerfile
#
# Image    : ghcr.io/voldigoad-holdings/nexus-dl
# Build    : docker buildx build --platform linux/amd64,linux/arm64 \
#              -f docker/Dockerfile -t nexusdl .
#
# Contenu  : Python 3.12 + NexusDL[cli] + tini
# Exclut   : Playwright browsers (installer séparément)
#            GUI (CustomTkinter — voir Dockerfile.gui)
#            Web frontend (Next.js — voir Dockerfile.web)
#            Tests, docs, frontend
#
# ⚠️  Image non-root (UID 1000, GID 1000).
# ⚠️  Toutes les données persistantes dans /data et /downloads.
# ⚠️  Configuration via /config/.env (ou variables d'env).
# ════════════════════════════════════════════════════════════════════

# ────────────────────────────────────────────────────────────────────
# ARGs — métadonnées (surchargées par le CI)
# ────────────────────────────────────────────────────────────────────
ARG PYTHON_VERSION=3.12
ARG DEBIAN_VERSION=bookworm
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION=0.1.0

# ────────────────────────────────────────────────────────────────────
# STAGE 1 — builder
# ────────────────────────────────────────────────────────────────────
# Installe les deps Python dans un venv isolé.
# Ce stage est jeté à la fin (multi-stage build).
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS builder

# Empêche Python d'écrire des .pyc (inutiles dans l'image finale)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

# Dépendances système pour compiler (si besoin de wheels natifs)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libffi-dev \
        libssl-dev \
        pkg-config \
 && rm -rf /var/lib/apt/lists/*

# Crée un venv isolé
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip + install uv (build backend)
RUN pip install --upgrade pip \
 && pip install "uv>=0.4,<1.0"

# Copie uniquement les fichiers de dépendances (cache optimal)
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./

# Installe NexusDL + extras CLI dans le venv
# --mount=type=cache persiste le cache uv entre les builds
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        -e ".[cli]"

# Copie le code source (après les deps → cache optimal)
COPY src/ ./src/
COPY config/ ./config/

# Réinstalle en mode editable pour inclure le code final
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        --no-deps -e .

# Vérifie que le CLI est fonctionnel
RUN /opt/venv/bin/nexusdl --version

# ────────────────────────────────────────────────────────────────────
# STAGE 2 — runtime
# ────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS runtime

# ────────────────────────────────────────────────────────────────────
# ARGs (re-déclarés pour être utilisables dans LABEL)
# ────────────────────────────────────────────────────────────────────
ARG BUILD_DATE
ARG VCS_REF
ARG VERSION

# ────────────────────────────────────────────────────────────────────
# Métadonnées OCI (https://github.com/opencontainers/image-spec)
# ────────────────────────────────────────────────────────────────────
LABEL org.opencontainers.image.title="NexusDL" \
      org.opencontainers.image.description="Multi-site manga, webtoon, comic and doujinshi downloader" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/Voldigoad-Holdings/nexus-dl" \
      org.opencontainers.image.url="https://github.com/Voldigoad-Holdings/nexus-dl" \
      org.opencontainers.image.documentation="https://github.com/Voldigoad-Holdings/nexus-dl/tree/main/docs" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.authors="Voldigoad-Holdings <drxenon487@gmail.com>" \
      org.opencontainers.image.vendor="Voldigoad-Holdings"

# ────────────────────────────────────────────────────────────────────
# Variables d'environnement runtime
# ────────────────────────────────────────────────────────────────────
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Chemins NexusDL (cohérents avec platformdirs)
    NEXUSDL_CONFIG_DIR=/config \
    NEXUSDL_DATA_DIR=/data \
    NEXUSDL_LOGS_DIR=/data/logs \
    NEXUSDL_DOWNLOAD_DIR=/downloads \
    NEXUSDL_TEMP_DIR=/tmp/nexusdl \
    NEXUSDL_LIBRARY_DB_PATH=/data/library.db \
    NEXUSDL_COOKIE_STORAGE_PATH=/data/cookies.enc \
    # Environnement
    NEXUSDL_APP_ENVIRONMENT=prod \
    NEXUSDL_LOG_FORMAT=json \
    NEXUSDL_LOG_LEVEL=INFO

# ────────────────────────────────────────────────────────────────────
# Dépendances système runtime (minimales)
# ────────────────────────────────────────────────────────────────────
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
        tzdata \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# ────────────────────────────────────────────────────────────────────
# Utilisateur non-root
# ────────────────────────────────────────────────────────────────────
# UID/GID 1000 est le standard Debian (évite les conflits avec les volumes)
ARG UID=1000
ARG GID=1000

RUN groupadd --gid "${GID}" nexusdl \
 && useradd --uid "${UID}" --gid "${GID}" \
            --shell /bin/bash \
            --create-home \
            --home-dir /home/nexusdl \
            nexusdl

# ────────────────────────────────────────────────────────────────────
# Répertoires de travail
# ────────────────────────────────────────────────────────────────────
# /app       → code (read-only en prod)
# /config    → .env, config.yaml (bind mount recommandé)
# /data      → library.db, cookies.enc, logs (volume)
# /downloads → mangas téléchargés (volume)
# /tmp       → temp (tmpfs recommandé)
RUN mkdir -p /app /config /data /downloads /tmp/nexusdl \
 && chown -R nexusdl:nexusdl /app /config /data /downloads /tmp/nexusdl \
 && chmod 755 /app /config /data /downloads \
 && chmod 1777 /tmp

WORKDIR /app

# ────────────────────────────────────────────────────────────────────
# Copie du venv + code depuis le builder
# ────────────────────────────────────────────────────────────────────
COPY --from=builder --chown=nexusdl:nexusdl /opt/venv /opt/venv
COPY --from=builder --chown=nexusdl:nexusdl /build/src /app/src
COPY --from=builder --chown=nexusdl:nexusdl /build/config /app/config

# ────────────────────────────────────────────────────────────────────
# Healthcheck
# ────────────────────────────────────────────────────────────────────
# Vérifie que le CLI répond (rapide, pas d'I/O réseau).
# Pour le Web, voir Dockerfile.web (healthcheck HTTP).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD nexusdl --version || exit 1

# ────────────────────────────────────────────────────────────────────
# Entrypoint et signal handling
# ────────────────────────────────────────────────────────────────────
# tini = PID 1 (reap zombies, forward signals)
# entrypoint.sh = gestion des permissions des volumes + exec "$@"
COPY --chown=nexusdl:nexusdl docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["nexusdl", "--help"]

# Signal d'arrêt (tini forward correctement)
STOPSIGNAL SIGTERM

# ────────────────────────────────────────────────────────────────────
# Utilisateur final
# ────────────────────────────────────────────────────────────────────
USER nexusdl:nexusdl
