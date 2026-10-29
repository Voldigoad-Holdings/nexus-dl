# macOS Installation

> **Audience**: macOS users (Intel + Apple Silicon) who want a **native
> install** of NexusDL.
> **Supported versions**: macOS 12 (Monterey) and later.
>
> **Not for you?** → [Docker installation](docker.md) (recommended for
> servers), [Linux](linux.md), [Windows](windows.md).

NexusDL is a **Python 3.12+ application** with optional native `.app`
bundles. This guide covers both.

---

## Table of contents

1. [Requirements](#requirements)
2. [Install Python 3.12](#install-python-312)
3. [Install NexusDL](#install-nexusdl)
4. [Post-install configuration](#post-install-configuration)
5. [First download](#first-download)
6. [Optional components](#optional-components)
7. [Native .app bundle](#native-app-bundle)
8. [macOS-specific notes](#macos-specific-notes)
9. [Uninstall](#uninstall)
10. [Troubleshooting](#troubleshooting)
11. [See also](#see-also)

---

## Requirements

| Component | Minimum | Recommended |
|---|---|---|
| **macOS** | 12 (Monterey) | 14 (Sonoma) |
| **Architecture** | x86_64 (Intel), arm64 (Apple Silicon) | — |
| **Python** | 3.12 | 3.12.4+ |
| **Disk** | 500 MB (core + TUI) | 5 GB (with downloads) |
| **RAM** | 512 MB | 1 GB |

### Check your system

```bash
# macOS version
sw_vers
# → ProductVersion: 14.5.0

# Architecture
uname -m
# → arm64 (Apple Silicon)
# → x86_64 (Intel)

# Python
python3 --version
# → Python 3.12.x (or higher)
```

---

## Install Python 3.12

If `python3 --version` returns **3.12 or higher**, skip to
[Install NexusDL](#install-nexusdl).

Otherwise, install Python 3.12 using Homebrew or `uv`.

### Option A — Homebrew (recommended)

```bash
# Install Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python 3.12
brew install python@3.12

# Verify
python3.12 --version
# → Python 3.12.x
```

**Apple Silicon note**: Homebrew installs to `/opt/homebrew/` (not
`/usr/local/`). The `python3.12` binary is in `/opt/homebrew/bin/`, which
should already be in your `PATH`.

If `python3.12` is not found:

```bash
echo 'export PATH="/opt/homebrew/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Option B — `uv` (fastest, no Homebrew)

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Reload shell
source ~/.zshrc

# Install Python 3.12
uv python install 3.12

# Verify
uv python find 3.12
```

`uv` downloads a **portable Python** (~30 MB) into
`~/.local/share/uv/python/`. No Homebrew, no compilation.

### Option C — python.org installer

Download the official installer from
<https://www.python.org/downloads/macos/>.

After installation, `python3.12` is in
`/Library/Frameworks/Python.framework/Versions/3.12/bin/`. Add it to
`PATH`:

```bash
echo 'export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

---

## Install NexusDL

There are **four methods**. Choose **one**.

### Method 1 — `uv` (recommended)

**Fastest, isolated, no Homebrew.**

```bash
# Install uv (if not already done)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.zshrc

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
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
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

**Cross-platform, isolated, no root.**

```bash
# Install pipx
brew install pipx
pipx ensurepath

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
```

**Update**:

```bash
source ~/.venvs/nexusdl/bin/activate
pip install --upgrade "nexusdl[all]"
```

### Method 4 — Homebrew formula (coming soon)

```bash
# Not yet available
brew install nexusdl
```

Track progress in
[issue #XXX](https://github.com/Voldigoad-Holdings/nexus-dl/issues).

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

NexusDL uses `platformdirs` for cross-platform paths. On macOS:

| Directory | Path | Contents |
|---|---|---|
| **Config** | `~/Library/Application Support/nexusdl/` | `.env`, `config.yaml` |
| **Data** | `~/Library/Application Support/nexusdl/` | `library.db`, `cookies.enc` |
| **Cache** | `~/Library/Caches/nexusdl/` | HTTP cache, temp files |
| **Logs** | `~/Library/Logs/nexusdl/` | Log files |
| **Downloads** | `~/Downloads/NexusDL/` | Default download dir |

Override any of these in `.env`:

```dotenv
NEXUSDL_DOWNLOAD_DIR=/Volumes/ExternalDrive/Manga
NEXUSDL_LIBRARY_DB_PATH=/Volumes/ExternalDrive/nexusdl/library.db
```

### Shell completion

NexusDL supports **Zsh** (default on macOS) and **Bash**:

```bash
# Zsh (default)
nexusdl --install-completion zsh

# Bash
nexusdl --install-completion bash

# Fish (if installed)
nexusdl --install-completion fish
```

Reload your shell:

```bash
source ~/.zshrc
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
```

### 4. Try the TUI

```bash
nexusdl-tui
```

### 5. Try the GUI

```bash
nexusdl-gui
```

On first launch, macOS may block the app with **"NexusDL can't be opened
because the developer cannot be verified"**. See
[Gatekeeper](#gatekeeper) below.

### 6. Try the Web UI (optional)

```bash
nexusdl-web
# → http://localhost:8000 (API)
# → http://localhost:3000 (frontend, if built)
```

---

## Optional components

### Playwright (Cloudflare bypass)

```bash
# Install the Python package (already included in [all])
pip install nexusdl[playwright]

# Install the Chromium browser
playwright install chromium
```

**Enable** in `.env`:

```dotenv
NEXUSDL_PLAYWRIGHT_ENABLED=true
```

**Disk space**: ~200 MB per browser.

**Apple Silicon note**: Playwright ships **arm64** builds of Chromium.
Native performance.

### FlareSolverr (alternative)

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

```bash
brew install redis
brew services start redis
```

**Enable** in `.env`:

```dotenv
NEXUSDL_REDIS_URL=redis://localhost:6379/0
```

---

## Native .app bundle

If you don't want Python, download the **`NexusDL.app`** bundle from the
[releases page](https://github.com/Voldigoad-Holdings/nexus-dl/releases).

### Download and install

```bash
# Download the .dmg
curl -L "https://github.com/Voldigoad-Holdings/nexus-dl/releases/latest/download/NexusDL-macos-universal.dmg" \
  -o ~/Downloads/NexusDL.dmg

# Mount the .dmg
hdiutil attach ~/Downloads/NexusDL.dmg

# Copy to Applications
cp -R /Volumes/NexusDL/NexusDL.app /Applications/

# Unmount
hdiutil detach /Volumes/NexusDL

# Verify
open /Applications/NexusDL.app
```

The `.app` bundle is **universal** (Intel + Apple Silicon).

### Gatekeeper

On first launch, macOS blocks unsigned apps with:

> "NexusDL" cannot be opened because the developer cannot be verified.

**Option A — Right-click → Open** (one-time):

1. Right-click (or Ctrl+click) the app in Finder.
2. Select **Open**.
3. Click **Open** in the dialog.

macOS remembers this choice.

**Option B — System Settings**:

1. Open **System Settings** → **Privacy & Security**.
2. Scroll to the **Security** section.
3. Click **Open Anyway** next to the NexusDL message.
4. Confirm with your password.

**Option C — Remove quarantine attribute** (advanced):

```bash
xattr -d com.apple.quarantine /Applications/NexusDL.app
```

**Why is it unsigned?** Code signing requires an **Apple Developer
account** ($99/year). NexusDL is a volunteer open-source project without
funding. We plan to sign once we have a sponsor.

---

## macOS-specific notes

### Apple Silicon (M1/M2/M3)

NexusDL runs **natively** on Apple Silicon. No Rosetta emulation needed.

**To verify**:

```bash
file $(which nexusdl)
# → Mach-O 64-bit executable arm64
```

If you see `x86_64`, you installed the Intel version. Reinstall with the
arm64 method (`uv`, `pipx`, or Homebrew).

### TCC (Transparency, Consent, Control)

macOS **protects** certain directories (`~/Desktop`, `~/Documents`,
`~/Downloads`). When NexusDL tries to write there, macOS shows a prompt:

> "Terminal" would like to access files in your "Downloads" folder.

**Click "OK"** (or grant access in **System Settings → Privacy & Security
→ Files and Folders**).

If you denied the prompt by accident:

```bash
tccutil reset SystemPolicyDownloadsFolder
```

Then re-run NexusDL and accept the prompt.

### `~/.config/nexusdl/` vs `~/Library/Application Support/nexusdl/`

NexusDL uses `~/Library/Application Support/nexusdl/` by default (macOS
convention, per `platformdirs`). If you prefer `~/.config/nexusdl/`
(Linux convention), override in `.env`:

```dotenv
NEXUSDL_LIBRARY_DB_PATH=~/.config/nexusdl/library.db
NEXUSDL_COOKIE_STORAGE_PATH=~/.config/nexusdl/cookies.enc
```

### Spotlight indexing

Spotlight **indexes** `~/Downloads/` and `~/Documents/`. If you download
hundreds of CBZ files, Spotlight will index them (slower downloads).

**Exclude the download directory from Spotlight**:

1. **System Settings** → **Siri & Spotlight** → **Spotlight Privacy**.
2. Click **+** and add `~/Downloads/NexusDL/`.

Or via command line:

```bash
sudo mdutil -i off ~/Downloads/NexusDL
```

### File system case sensitivity

macOS APFS is **case-insensitive by default** (but case-preserving).
`~/Manga/OnePiece.cbz` and `~/Manga/onepiece.cbz` are **the same file**.

**Impact**: if two mangas differ only by case (`OnePiece` vs `Onepiece`),
they may overwrite each other.

**Workaround**: use a case-sensitive APFS volume:

1. Open **Disk Utility** → **File** → **New Image** → **Blank Image**.
2. Format: **APFS (Case-sensitive)**.
3. Mount the volume and set `NEXUSDL_DOWNLOAD_DIR=/Volumes/CaseSensitive/Manga`.

### Homebrew vs uv vs pipx

| Method | Best for | Notes |
|---|---|---|
| **`uv`** | Most users | Fastest, isolated, no root |
| **`pipx`** | Users who already use pipx | Isolated, brew-managed |
| **venv** | Developers | Most control |
| **Homebrew** | Future | Not yet available |

### Performance

- **Downloads** are I/O-bound. SSD > HDD.
- **Packaging** (CBZ) is CPU-bound. Apple Silicon is ~2× faster than Intel.
- **Playwright** is memory-heavy. ~500 MB RAM for 4 concurrent contexts.

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

### Method 4 (Homebrew, if applicable)

```bash
brew uninstall nexusdl
```

### Remove data (optional)

```bash
rm -rf ~/Library/Application\ Support/nexusdl/
rm -rf ~/Library/Caches/nexusdl/
rm -rf ~/Library/Logs/nexusdl/

# Downloads (careful!)
# rm -rf ~/Downloads/NexusDL/
```

### .app bundle

```bash
rm -rf /Applications/NexusDL.app
```

---

## Troubleshooting

### `nexusdl: command not found`

The binary is not in your `PATH`.

**uv/pipx**:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

**venv**:

```bash
source ~/.venvs/nexusdl/bin/activate
```

### `python3.12: command not found` after Homebrew

Homebrew on Apple Silicon installs to `/opt/homebrew/bin/`:

```bash
echo 'export PATH="/opt/homebrew/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### `error: externally-managed-environment` (PEP 668)

Homebrew Python **blocks** `pip install` outside a venv.

**Solution**: use `uv`, `pipx`, or a venv.

**Do NOT** use `--break-system-packages`.

### `zsh: killed` when running `nexusdl`

macOS may **kill** processes that exceed memory limits, especially on
8 GB Macs with Playwright enabled.

**Solution**: reduce concurrency:

```dotenv
NEXUSDL_MAX_CONCURRENT_DOWNLOADS=1
NEXUSDL_MAX_CONCURRENT_PAGES_PER_CHAPTER=4
NEXUSDL_PLAYWRIGHT_MAX_CONTEXTS=1
```

### Gatekeeper blocks `nexusdl-gui`

See [Gatekeeper](#gatekeeper) above.

### `TclError: no display name and no $DISPLAY`

You're running over **SSH** without X11 forwarding.

**Solution**: use `nexusdl` (CLI) or `nexusdl-tui` (TUI). Or enable X11
forwarding:

```bash
ssh -X user@host
nexusdl-gui
```

### Slow downloads on Apple Silicon

Playwright may be using the **Intel** Chromium via Rosetta.

**Solution**: reinstall Playwright for arm64:

```bash
playwright uninstall
playwright install chromium
```

### `Operation not permitted` on `~/Downloads`

TCC (macOS privacy) is blocking NexusDL.

**Solution**:

```bash
tccutil reset SystemPolicyDownloadsFolder
# Then re-run NexusDL and accept the prompt
```

### `database is locked`

SQLite doesn't support concurrent writes.

**Solution**: don't run multiple NexusDL instances on the same database.

### `429 Too Many Requests`

You're hitting a site's rate limit.

**Solution**: reduce concurrency:

```dotenv
NEXUSDL_MAX_CONCURRENT_DOWNLOADS=2
NEXUSDL_DEFAULT_RATE_LIMIT_PER_SECOND=1.0
```

### Site is blocked (Cloudflare)

Enable Playwright or FlareSolverr:

```bash
pip install nexusdl[playwright]
playwright install chromium
# In .env: NEXUSDL_PLAYWRIGHT_ENABLED=true
```

### Logs to debug

```bash
# Verbose logs
NEXUSDL_LOG_LEVEL=DEBUG nexusdl search "one piece"

# Follow logs
tail -f ~/Library/Logs/nexusdl/nexusdl.log
```

### Check if NexusDL is running on arm64

```bash
arch -x86_64 nexusdl --version   # forces Intel (Rosetta)
arch -arm64 nexusdl --version    # forces arm64

file $(which nexusdl)            # shows the actual arch
```

---

## See also

- [Docker installation](docker.md) — recommended for servers
- [Linux installation](linux.md)
- [Windows installation](windows.md)
- [Configuration](../usage/configuration.md) — all env vars
- [CLI usage](../usage/cli.md) — all commands
- [Formats](../usage/formats.md) — packaging formats
- [GUI usage](../usage/gui.md) — GUI-specific guide
- [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) — ask for help

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
