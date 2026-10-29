#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
#
# NexusDL Docker entrypoint.
#
# Used by:
#   - docker/Dockerfile       (base: CLI + TUI)
#   - docker/Dockerfile.web   (web: FastAPI + Next.js + Nginx)
#   - docker/Dockerfile.gui   (GUI: CustomTkinter + X11)
#
# Responsibilities:
#   1. Verify that volume directories exist and are writable.
#   2. Fix permissions if needed (chown, only if running as root).
#   3. Validate that essential secrets are set (warn, don't block).
#   4. Display a config summary (if NEXUSDL_ENTRYPOINT_VERBOSE=true).
#   5. exec "$@" to transfer signals and exit code.
#
# ⚠️  Never log secrets.
# ⚠️  Idempotent: safe to re-run.
# ⚠️  Works in root and non-root.
# ⚠️  POSIX sh (compatible Alpine / Debian / Ubuntu).
#
# Environment variables:
#   NEXUSDL_CONFIG_DIR             (default: /config)
#   NEXUSDL_DATA_DIR               (default: /data)
#   NEXUSDL_DOWNLOAD_DIR           (default: /downloads)
#   NEXUSDL_ENTRYPOINT_VERBOSE     (default: false)
#   NEXUSDL_ENTRYPOINT_SKIP_CHECK  (default: false)
#
# Exit codes:
#   0   success (or command succeeded)
#   1   volume/permission error
#   2   command failed
#   3   no command provided (unexpected)
#   127 command not found
#   130 interrupted (SIGINT)

# ────────────────────────────────────────────────────────────────────
# Strict mode (POSIX-compatible)
# ────────────────────────────────────────────────────────────────────
set -eu

# Note: `pipefail` is bash-only. We detect bash and enable it.
if [ -n "${BASH_VERSION:-}" ]; then
    set -o pipefail
fi

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────
readonly SCRIPT_NAME="nexusdl-entrypoint"
readonly APP_USER="nexusdl"
readonly APP_UID="${NEXUSDL_UID:-1000}"
readonly APP_GID="${NEXUSDL_GID:-1000}"

# Directories to verify
CONFIG_DIR="${NEXUSDL_CONFIG_DIR:-/config}"
DATA_DIR="${NEXUSDL_DATA_DIR:-/data}"
DOWNLOAD_DIR="${NEXUSDL_DOWNLOAD_DIR:-/downloads}"

# Verbose flag
VERBOSE="${NEXUSDL_ENTRYPOINT_VERBOSE:-false}"
SKIP_CHECK="${NEXUSDL_ENTRYPOINT_SKIP_CHECK:-false}"

# ────────────────────────────────────────────────────────────────────
# Utility functions
# ────────────────────────────────────────────────────────────────────

# Print an informational message (never secrets).
log() {
    printf '[%s] [%s] %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$SCRIPT_NAME" \
        "$*" >&2
}

# Print a warning.
warn() {
    printf '[%s] [%s] WARN: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$SCRIPT_NAME" \
        "$*" >&2
}

# Print an error.
err() {
    printf '[%s] [%s] ERROR: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$SCRIPT_NAME" \
        "$*" >&2
}

# Check if running as root (uid 0).
is_root() {
    [ "$(id -u)" -eq 0 ]
}

# Check if a directory exists.
dir_exists() {
    [ -d "$1" ]
}

# Check if a directory is writable.
dir_writable() {
    [ -d "$1" ] && [ -w "$1" ]
}

# Try to write a temporary file in a directory (real test).
can_write_file() {
    _dir="$1"
    _testfile="$_dir/.write-test-$$-$(date +%s)"
    if ( : > "$_testfile" ) 2>/dev/null; then
        rm -f "$_testfile" 2>/dev/null || true
        return 0
    fi
    return 1
}

# Ensure a directory exists and is writable.
ensure_dir() {
    _dir="$1"
    _name="$2"

    if ! dir_exists "$_dir"; then
        log "Creating missing directory: $_dir"
        if ! mkdir -p "$_dir" 2>/dev/null; then
            err "Cannot create $_name directory: $_dir"
            return 1
        fi
    fi

    if ! dir_writable "$_dir" || ! can_write_file "$_dir"; then
        warn "$_name directory not writable by $(id -un 2>/dev/null || echo unknown): $_dir"

        if is_root; then
            log "Running as root, attempting to fix ownership..."
            # Only chown if the owner is different (avoid slow recursive ops)
            _current_owner="$(stat -c '%u' "$_dir" 2>/dev/null || echo 'unknown')"
            if [ "$_current_owner" != "$APP_UID" ]; then
                if chown -R "$APP_UID:$APP_GID" "$_dir" 2>/dev/null; then
                    log "Fixed ownership of: $_dir"
                else
                    err "Cannot chown $_dir (permission denied?)"
                    return 1
                fi
            fi
            if ! dir_writable "$_dir"; then
                if chmod -R u+rwX "$_dir" 2>/dev/null; then
                    log "Fixed permissions of: $_dir"
                else
                    err "Cannot chmod $_dir"
                    return 1
                fi
            fi
        else
            err "Cannot fix permissions (not root). Fix on host:"
            err "  sudo chown -R $APP_UID:$APP_GID <host-path>"
            return 1
        fi
    fi

    return 0
}

# Check if a secret is set (non-empty). Never logs the value.
check_secret() {
    _name="$1"
    _value="$(eval "printf '%s' \"\${$_name:-}\"")"
    if [ -z "$_value" ]; then
        warn "Secret '$_name' is not set or empty."
        return 1
    fi
    return 0
}

# ────────────────────────────────────────────────────────────────────
# 1. Umask (files 644, dirs 755)
# ────────────────────────────────────────────────────────────────────
umask 022

# ────────────────────────────────────────────────────────────────────
# 2. Display banner (if verbose)
# ────────────────────────────────────────────────────────────────────
if [ "$VERBOSE" = "true" ]; then
    log "NexusDL container starting"
    log "User:      $(id -un 2>/dev/null || echo unknown) (uid=$(id -u), gid=$(id -g))"
    log "Config:    $CONFIG_DIR"
    log "Data:      $DATA_DIR"
    log "Downloads: $DOWNLOAD_DIR"
    log "Env:       ${NEXUSDL_APP_ENVIRONMENT:-unset}"
    log "Command:   $*"
fi

# ────────────────────────────────────────────────────────────────────
# 3. Verify volumes (unless skipped)
# ────────────────────────────────────────────────────────────────────
if [ "$SKIP_CHECK" != "true" ]; then
    log "Verifying volume directories..."
    ensure_dir "$CONFIG_DIR" "config" || exit 1
    ensure_dir "$DATA_DIR" "data" || exit 1
    ensure_dir "$DOWNLOAD_DIR" "downloads" || exit 1
    log "All volumes OK."
fi

# ────────────────────────────────────────────────────────────────────
# 4. Verify config (.env)
# ────────────────────────────────────────────────────────────────────
ENV_FILE="$CONFIG_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    warn "No .env file found at $ENV_FILE"
    warn "NexusDL requires NEXUSDL_SECRET_KEY, NEXUSDL_JWT_SECRET,"
    warn "NEXUSDL_COOKIE_ENCRYPTION_KEY."
    warn "Mount a .env file or pass them as environment variables."
    warn "Continuing anyway (will fail on first command that needs them)."
fi

# ────────────────────────────────────────────────────────────────────
# 5. Verify secrets (warn, don't block)
# ────────────────────────────────────────────────────────────────────
missing_secrets=0
check_secret "NEXUSDL_SECRET_KEY" || missing_secrets=$((missing_secrets + 1))
check_secret "NEXUSDL_JWT_SECRET" || missing_secrets=$((missing_secrets + 1))
check_secret "NEXUSDL_COOKIE_ENCRYPTION_KEY" || missing_secrets=$((missing_secrets + 1))

if [ "$missing_secrets" -gt 0 ]; then
    warn "$missing_secrets secret(s) missing. See .env.example for the required values."
fi

# ────────────────────────────────────────────────────────────────────
# 6. Check that the command exists
# ────────────────────────────────────────────────────────────────────
if [ "$#" -eq 0 ]; then
    warn "No command provided. Defaulting to 'nexusdl --help'."
    set -- nexusdl --help
fi

CMD="$1"
if ! command -v "$CMD" >/dev/null 2>&1; then
    # If command not found but it's not the first binary, let exec fail with a clear message
    err "Command not found: $CMD"
    exit 127
fi

# ────────────────────────────────────────────────────────────────────
# 7. Check that nexusdl is functional (only if invoked as nexusdl*)
# ────────────────────────────────────────────────────────────────────
case "$CMD" in
    nexusdl|nexusdl-tui|nexusdl-gui|nexusdl-web)
        if ! timeout 5 "$CMD" --version >/dev/null 2>&1; then
            warn "$CMD --version failed (binary may be broken)."
        elif [ "$VERBOSE" = "true" ]; then
            _version="$(timeout 5 "$CMD" --version 2>/dev/null | head -1)"
            log "Version: $_version"
        fi
        ;;
esac

# ────────────────────────────────────────────────────────────────────
# 8. Display summary (if verbose)
# ────────────────────────────────────────────────────────────────────
if [ "$VERBOSE" = "true" ]; then
    log "Entrypoint checks complete."
    log "Executing: $*"
fi

# ────────────────────────────────────────────────────────────────────
# 9. exec (transfer PID 1, signals, exit code)
# ────────────────────────────────────────────────────────────────────
exec "$@"
