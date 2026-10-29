# Linux Installation

> **Audience**: Linux users who want a **native install** of NexusDL.
> **Supported distros**: Debian 12+, Ubuntu 22.04+, Fedora 40+, Arch,
> openSUSE Tumbleweed, Alpine 3.19+.
>
> **Not for you?** → [Docker installation](docker.md) (recommended for
> servers), [macOS](macos.md), [Windows](windows.md).

NexusDL is a **Python 3.12+ application** with optional native executables.
This guide covers both.

---

## Table of contents

1. [Requirements](#requirements)
2. [Install Python 3.12](#install-python-312)
3. [Install NexusDL](#install-nexusdl)
4. [Post-install configuration](#post-install-configuration)
5. [First download](#first-download)
6. [Optional components](#optional-components)
7. [Native executables (PyInstaller)](#native-executables-pyinstaller)
8. [Uninstall](#uninstall)
9. [Troubleshooting](#troubleshooting)
10. [See also](#see-also)

---

## Requirements

| Component | Minimum | Recommended |
|---|---|---|
| **Distribution** | Debian 12, Ubuntu 22.04, Fedora 40, Arch, openSUSE TW, Alpine 3.19 | Latest LTS |
| **Architecture** | x86_64, arm64 | — |
| **Python** | 3.12 | 3.12.4+ |
| **Disk** | 500 MB (core + TUI) | 5 GB (with downloads) |
| **RAM** | 512 MB | 1 GB |
| **Display** | — (CLI/TUI), X11/Wayland (GUI) | — |

### Check your system

```bash
# Distribution
cat /etc/os-release

# Architecture
uname -m
# x86_64 → amd64
# aarch64 → arm64

# Python
python3 --version
# → Python 3.12.x (or higher)
```

---

## Install Python 3.12

If `python3 --version` returns **3.12 or higher**, skip to
[Install NexusDL](#install-nexusdl).

Otherwise, install Python 3.12 using your distribution's package manager
or `uv`.

### Option A — System package (recommended if available)

**Debian 12+ / Ubuntu 22.04+**:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

**Fedora 40+**:

```bash
sudo dnf install -y python3.12 python3.12-devel
```

**Arch Linux**:

```bash
sudo pacman -S python
# Arch always ships the latest Python
```

**openSUSE Tumbleweed**:

```bash
sudo zypper install -y python312 python312-devel
```

**Alpine 3.19+**:

```bash
sudo apk add python3 py3-pip
# Alpine 3.19 ships Python 3.12
```

### Option B — `uv` (fastest, no root)

`uv` installs and manages Python versions **without root**:

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Reload your shell
source ~/.bashrc

# Install Python 3.12
uv python install 3.12

# Verify
uv python find 3.12
```

`uv` downloads a pre-built Python binary (~30 MB) into
`~/.local/share/uv/python/`. No compilation, no root.

### Option C — `pyenv` (compile from source)

Only if you need a very specific patch version:

```bash
# Install build dependencies (Debian/Ubuntu)
sudo apt install -y make build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev \
  libffi-dev liblzma-dev

# Install pyenv
curl https://pyenv.run | bash

# Add to ~/.bashrc
echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init -)"' >> ~/.bashrc
source ~/.bashrc

# Install Python 3.12
pyenv install 3.12
```

Compilation takes **5–15 minutes**. Only use this if options A and B fail.

---

## Install NexusDL

There are **four methods**. Choose **one**.

### Method 1 — `uv` (recommended)

**Fastest, isolated, cross-distro.**

```bash
# Install uv (if not already done)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# Install NexusDL with all extras
uv tool install "nexusdl[all]"

# Verify
nexusdl --version
# → 0.1.0

# Where did it install?
uv tool list
# → nexusdl v0.1.0
```

`uv tool install` creates an isolated environment in
`~/.local/share/uv/tools/nexusdl/` and exposes the CLI binaries in
`~/.local/bin/`.

**Add to PATH** if not already:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

**Update**:

```bash
uv tool upgrade nexusdl
```

**Uninstall**:

```bash
uv tool uninstall nexusdl
```

### Method 2 — `pipx` (isolated)

**Cross-distro, isolated, no root.**

```bash
# Install pipx (Debian/Ubuntu)
sudo apt install -y pipx
pipx ensurepath

# Or with pip
python3.12 -m pip install --user pipx
python3.12 -m pipx ensurepath

# Install NexusDL
pipx install "nexusdl[all]"

# Verify
nexusdl --version
```

**Update**:

```bash
pipx upgrade nexusdl
```

**Uninstall**:

```bash
pipx uninstall nexusdl
```

### Method 3 — `pip` in a virtualenv (classic)

**Most control, most manual.**

```bash
# Create a virtualenv
python3.12 -m venv ~/.venvs/nexusdl

# Activate
source ~/.venvs/nexusdl/bin/activate

# Upgrade pip
python -m pip install --upgrade pip

# Install NexusDL
pip install "nexusdl[all]"

# Verify
nexusdl --version

# Add a shell alias (optional)
echo 'alias nexusdl="source ~/.venvs/nexusdl/bin/activate && nexusdl"' >> ~/.bashrc
```

**Update**:

```bash
source ~/.venvs/nexusdl/bin/activate
pip install --upgrade "nexusdl[all]"
```

### Method 4 — `pip --user` (no isolation, not recommended)

**Only if methods 1–3 fail.** Risks breaking system Python.

```bash
python3.12 -m pip install --user "nexusdl[all]"
```

**Do NOT** use `sudo pip install`. It breaks your system Python on
Debian/Ubuntu and Fedora.

---

## Post-install configuration

NexusDL reads configuration from `.env` (in the current directory or
`~/.config/nexusdl/.env`) and from environment variables.

### Create the config

```bash
# Create the config directory
mkdir -p ~/.config/nexusdl

# Copy the template
nexusdl config init
# Or manually:
curl -sL https://raw.githubusercontent.com/Voldigoad-Holdings/nexus-dl/main/.env.example \
  -o ~/.config/nexusdl/.env

# Set restrictive permissions
chmod 600 ~/.config/nexusdl/.env
```

### Generate secrets

```bash
# Run 3 times, paste each value into .env
python3.12 -c "import secrets; print(secrets.token_hex(32))"
```

Edit `~/.config/nexusdl/.env`:

```dotenv
NEXUSDL_SECRET_KEY=<paste-here>
NEXUSDL_JWT_SECRET=<paste-here>
NEXUSDL_COOKIE_ENCRYPTION_KEY=<paste-here>

NEXUSDL_PUBLIC_URL=https://localhost
NEXUSDL_API_BASE_URL=http://localhost:8000
NEXUSDL_FRONTEND_URL=http://localhost:3000

NEXUSDL_ALLOW_ADULT=false
NEXUSDL_LOG_LEVEL=INFO
```

### Data directories (default)

NexusDL uses `platformdirs` for cross-platform paths. On Linux:

| Directory | Path | Contents |
|---|---|---|
| **Config** | `~/.config/nexusdl/` | `.env`, `config.yaml` |
| **Data** | `~/.local/share/nexusdl/` | `library.db`, `cookies.enc` |
| **Cache** | `~/.cache/nexusdl/` | HTTP cache, temp files |
| **Logs** | `~/.local/state/nexusdl/logs/` | Log files |
| **Downloads** | `~/Downloads/NexusDL/` | Default download dir |

Override any of these in `.env`:

```dotenv
NEXUSDL_DOWNLOAD_DIR=/mnt/data/Manga
NEXUSDL_LIBRARY_DB_PATH=/mnt/data/nexusdl/library.db
```

### Shell completion

```bash
# Bash
nexusdl --install-completion bash

# Zsh
nexusdl --install-completion zsh

# Fish
nexusdl --install-completion fish
```

Reload your shell:

```bash
source ~/.bashrc     # or ~/.zshrc
```

Then:

```bash
nexusdl <TAB>
# → download  sites  search  library  config  ...
```

---

## First download

### 1. Check that NexusDL works

```bash
nexusdl --version
# → 0.1.0

nexusdl sites
# → list of supported sites
```

### 2. Search

```bash
nexusdl search "one piece" --sites mangadex,mangafire
```

Expected output:

```
Found 12 results across 2 sites:
  [MangaDex]  One Piece — Oda Eiichiro
  [MangaDex]  One Piece (Color) — Oda Eiichiro
  [MangaFire] One Piece — Oda Eiichiro
  ...
```

### 3. Download

```bash
nexusdl download "https://mangadex.org/title/..." \
  --chapters 1-3 \
  --format CBZ \
  --dest ~/Manga
```

Expected output:

```
[1/3] Chapter 001 · Romance Dawn
  Downloading 53 pages...
  ████████████████████████████████ 53/53 (100%)
  Packaging as CBZ...
  ✓ ~/Manga/One Piece/Chapter 001.cbz (12.3 MB)
[2/3] Chapter 002 ...
```

### 4. Try the TUI

```bash
nexusdl-tui
```

Keyboard shortcuts:

| Key | Action |
|---|---|
| `s` | Search |
| `d` | Downloads |
| `l` | Library |
| `c` | Config |
| `q` | Quit |

### 5. Try the GUI (desktop only)

```bash
nexusdl-gui
```

Requires a display (X11 or Wayland). On headless servers, this will fail
with `TclError: no display`.

### 6. Try the Web UI (optional)

```bash
nexusdl-web
# → http://localhost:8000 (API)
# → http://localhost:3000 (frontend, if built)
```

See [Web usage](../usage/web.md) for details.

---

## Optional components

### Playwright (Cloudflare bypass)

Required for JS-heavy or Cloudflare-protected sites.

```bash
# Install the Python package (already included in [all])
pip install nexusdl[playwright]

# Install the Chromium browser
playwright install chromium

# Or with system dependencies (Debian/Ubuntu, requires sudo)
sudo playwright install-deps chromium
playwright install chromium
```

**Enable** in `.env`:

```dotenv
NEXUSDL_PLAYWRIGHT_ENABLED=true
```

**Disk space**: ~150 MB per browser.

### FlareSolverr (alternative to Playwright)

Runs as a sidecar container. Useful on headless servers.

```bash
docker run -d \
  --name flaresolverr \
  --restart unless-stopped \
  -p 8191:8191 \
  ghcr.io/flaresolverr/flaresolverr:latest
```

**Enable** in `.env`:

```dotenv
NEXUSDL_FLARESOLVERR_URL=http://localhost:8191/v1
```

### Redis (optional)

For distributed cache (rarely needed for single-user installs).

```bash
# Debian/Ubuntu
sudo apt install -y redis-server

# Fedora
sudo dnf install -y redis

# Arch
sudo pacman -S redis

# Start
sudo systemctl enable --now redis
```

**Enable** in `.env`:

```dotenv
NEXUSDL_REDIS_URL=redis://localhost:6379/0
```

---

## Native executables (PyInstaller)

If you don't want Python, download a **standalone binary** from the
[releases page](https://github.com/Voldigoad-Holdings/nexus-dl/releases).

### Download

```bash
# Detect architecture
ARCH=$(uname -m)
# x86_64 → x86_64
# aarch64 → arm64

# Download the binary
curl -L "https://github.com/Voldigoad-Holdings/nexus-dl/releases/latest/download/nexusdl-linux-${ARCH}" \
  -o /tmp/nexusdl

# Verify the checksum
curl -L "https://github.com/Voldigoad-Holdings/nexus-dl/releases/latest/download/nexusdl-linux-${ARCH}.sha256" \
  -o /tmp/nexusdl.sha256
cd /tmp && sha256sum -c nexusdl.sha256
```

### Install

```bash
# Move to a directory in PATH
sudo install -m 755 /tmp/nexusdl /usr/local/bin/nexusdl

# Verify
nexusdl --version
```

### Notes

- Binary is **~30 MB** per OS.
- Includes Python 3.12 + core + CLI + TUI.
- **Does not include** Playwright browsers (install separately).
- **Does not include** GUI (use `nexusdl-gui` from pip).

---

## Uninstall

### Method 1 (uv)

```bash
uv tool uninstall nexusdl
```

### Method 2 (pipx)

```bash
pipx uninstall nexusdl
```

### Method 3 (venv)

```bash
rm -rf ~/.venvs/nexusdl
```

### Method 4 (pip --user)

```bash
python3.12 -m pip uninstall -y nexusdl
```

### Remove data (optional)

```bash
# Data
rm -rf ~/.local/share/nexusdl/
rm -rf ~/.config/nexusdl/
rm -rf ~/.cache/nexusdl/
rm -rf ~/.local/state/nexusdl/

# Downloads (careful — this deletes your mangas!)
# rm -rf ~/Downloads/NexusDL/
```

### PyInstaller

```bash
sudo rm /usr/local/bin/nexusdl
```

---

## Troubleshooting

### `nexusdl: command not found`

The binary is not in your `PATH`.

**uv/pipx**: add `~/.local/bin` to `PATH`:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

**venv**: activate the venv:

```bash
source ~/.venvs/nexusdl/bin/activate
```

### `ModuleNotFoundError: No module named 'nexusdl'`

Python is using the wrong interpreter.

```bash
which nexusdl
# → should point to ~/.local/bin/nexusdl or similar

# If it points to /usr/bin/nexusdl, uninstall system version
sudo apt remove nexusdl    # if installed via apt
```

### `python3.12: command not found`

Python 3.12 is not installed. See [Install Python 3.12](#install-python-312).

### `error: externally-managed-environment`

Debian 12 / Ubuntu 24.04 block `pip install` outside a venv (PEP 668).

**Solution**: use `uv`, `pipx`, or a venv (methods 1–3).

**Do NOT** use `--break-system-packages`.

### `TclError: no display name and no $DISPLAY`

You're running on a **headless server** and trying to open the GUI.

**Solution**: use `nexusdl` (CLI) or `nexusdl-tui` (TUI) instead. The GUI
requires a display (X11 or Wayland).

### GUI doesn't show on Wayland

CustomTkinter uses Tk, which works under Wayland via **XWayland**.

**Solution**: ensure XWayland is installed (usually is by default):

```bash
# Debian/Ubuntu
sudo apt install -y xwayland
```

Or force X11:

```bash
GDK_BACKEND=x11 nexusdl-gui
```

### `Permission denied` on download directory

The download directory is not writable.

```bash
# Check
ls -ld ~/Downloads/NexusDL

# Fix
mkdir -p ~/Downloads/NexusDL
chmod u+w ~/Downloads/NexusDL
```

Or change the path in `.env`:

```dotenv
NEXUSDL_DOWNLOAD_DIR=/mnt/data/Manga
```

### `429 Too Many Requests`

You're hitting a site's rate limit.

**Solution**: reduce concurrency and rate limit in `sites.yaml` or `.env`:

```dotenv
NEXUSDL_MAX_CONCURRENT_DOWNLOADS=2
NEXUSDL_DEFAULT_RATE_LIMIT_PER_SECOND=1.0
```

### Site is blocked (Cloudflare)

Enable Playwright or FlareSolverr:

```bash
# Playwright
pip install nexusdl[playwright]
playwright install chromium
# In .env:
# NEXUSDL_PLAYWRIGHT_ENABLED=true

# Or FlareSolverr
docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
# In .env:
# NEXUSDL_FLARESOLVERR_URL=http://localhost:8191/v1
```

### SQLite `database is locked`

SQLite doesn't support concurrent writes well.

**Solution**: don't run multiple NexusDL instances on the same database. If
you need parallel downloads, use one instance with
`NEXUSDL_MAX_CONCURRENT_DOWNLOADS`.

### Logs to debug

```bash
# Verbose logs
NEXUSDL_LOG_LEVEL=DEBUG nexusdl search "one piece"

# Follow logs
tail -f ~/.local/state/nexusdl/logs/nexusdl.log
```

---

## See also

- [Docker installation](docker.md) — recommended for servers
- [macOS installation](macos.md)
- [Windows installation](windows.md)
- [Configuration](../usage/configuration.md) — all env vars
- [CLI usage](../usage/cli.md) — all commands
- [Formats](../usage/formats.md) — packaging formats
- [Troubleshooting](../usage/cli.md#troubleshooting) — more solutions
- [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) — ask for help

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
