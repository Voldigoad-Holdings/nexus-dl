# Windows Installation

> **Audience**: Windows users (10 + 11, x64 + ARM64) who want a **native
> install** of NexusDL.
> **Supported versions**: Windows 10 (build 1809+), Windows 11.
>
> **Not for you?** → [Docker installation](docker.md) (recommended for
> servers), [Linux](linux.md), [macOS](macos.md).

NexusDL is a **Python 3.12+ application** with optional native `.exe`
executables. This guide covers both.

---

## Table of contents

1. [Requirements](#requirements)
2. [Install Python 3.12](#install-python-312)
3. [Install NexusDL](#install-nexusdl)
4. [Post-install configuration](#post-install-configuration)
5. [First download](#first-download)
6. [Optional components](#optional-components)
7. [Native .exe executable](#native-exe-executable)
8. [WSL2 alternative](#wsl2-alternative)
9. [Windows-specific notes](#windows-specific-notes)
10. [Uninstall](#uninstall)
11. [Troubleshooting](#troubleshooting)
12. [See also](#see-also)

---

## Requirements

| Component | Minimum | Recommended |
|---|---|---|
| **Windows** | 10 (build 1809+) | 11 (23H2+) |
| **Architecture** | x64, ARM64 | — |
| **Python** | 3.12 | 3.12.4+ |
| **Disk** | 500 MB (core + TUI) | 5 GB (with downloads) |
| **RAM** | 512 MB | 1 GB |
| **PowerShell** | 5.1 | 7.4+ |

### Check your system

Open **PowerShell** (press `Win + X` → **Terminal** or **Windows
PowerShell**):

```powershell
# Windows version
[System.Environment]::OSVersion.Version
# → Major 10, Build 22631 (Windows 11)

# Architecture
$env:PROCESSOR_ARCHITECTURE
# → AMD64 (x64)
# → ARM64 (ARM)

# Python
python --version
# → Python 3.12.x (or higher)
```

---

## Install Python 3.12

If `python --version` returns **3.12 or higher**, skip to
[Install NexusDL](#install-nexusdl).

Otherwise, install Python 3.12 using winget, Chocolatey, or `uv`.

### Option A — winget (recommended, Windows 10+)

**winget** is the official Windows package manager (built-in on Windows 11,
installable on Windows 10).

```powershell
# Install Python 3.12
winget install Python.Python.3.12

# Verify (restart PowerShell first)
python --version
# → Python 3.12.x
```

**Important**: winget installs Python to
`%LOCALAPPDATA%\Programs\Python\Python312\`. The installer adds it to
`PATH` automatically. **Restart PowerShell** after installation for
`PATH` to take effect.

### Option B — Chocolatey

**Chocolatey** is a community package manager for Windows.

```powershell
# Install Chocolatey (admin PowerShell)
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))

# Install Python 3.12
choco install python312 -y

# Verify
python --version
```

### Option C — python.org installer

Download the official installer from
<https://www.python.org/downloads/windows/>.

**Important**: during installation, **check** "Add Python to PATH".

### Option D — `uv` (fastest, no admin)

```powershell
# Install uv
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Reload PowerShell
. $PROFILE

# Install Python 3.12
uv python install 3.12

# Verify
uv python find 3.12
```

`uv` downloads a portable Python into `%LOCALAPPDATA%\uv\python\`. No
admin required.

---

## Install NexusDL

There are **four methods**. Choose **one**.

### Method 1 — `uv` (recommended)

**Fastest, isolated, no admin.**

```powershell
# Install uv (if not already done)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
. $PROFILE

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
`%LOCALAPPDATA%\uv\tools\nexusdl\` and exposes the CLI binaries in
`%USERPROFILE%\.local\bin\`.

**Add to PATH** if not already:

```powershell
# Add to user PATH permanently
[Environment]::SetEnvironmentVariable(
    "Path",
    "$env:Path;$env:USERPROFILE\.local\bin",
    "User"
)
# Restart PowerShell
```

**Update**:

```powershell
uv tool upgrade nexusdl
```

**Uninstall**:

```powershell
uv tool uninstall nexusdl
```

### Method 2 — `pipx` (isolated)

**Cross-platform, isolated, no admin.**

```powershell
# Install pipx
python -m pip install --user pipx
python -m pipx ensurepath

# Restart PowerShell
# Install NexusDL
pipx install "nexusdl[all]"

# Verify
nexusdl --version
```

**Update**:

```powershell
pipx upgrade nexusdl
```

**Uninstall**:

```powershell
pipx uninstall nexusdl
```

### Method 3 — `pip` in a virtualenv (classic)

**Most control, most manual.**

```powershell
# Create a virtualenv
python -m venv $env:USERPROFILE\.venvs\nexusdl

# Activate
& $env:USERPROFILE\.venvs\nexusdl\Scripts\Activate.ps1

# Upgrade pip
python -m pip install --upgrade pip

# Install NexusDL
pip install "nexusdl[all]"

# Verify
nexusdl --version
```

**Update**:

```powershell
& $env:USERPROFILE\.venvs\nexusdl\Scripts\Activate.ps1
pip install --upgrade "nexusdl[all]"
```

### Method 4 — winget / Chocolatey (future)

```powershell
# Not yet available
winget install VoldigoadHoldings.NexusDL
choco install nexusdl
```

Track progress in
[issue #XXX](https://github.com/Voldigoad-Holdings/nexus-dl/issues).

---

## Post-install configuration

NexusDL reads configuration from `.env` (in the current directory or
`%APPDATA%\nexusdl\.env`) and from environment variables.

### Create the config

```powershell
# Create the config directory
New-Item -ItemType Directory -Force -Path $env:APPDATA\nexusdl

# Copy the template
nexusdl config init
# Or manually:
Invoke-WebRequest `
    -Uri "https://raw.githubusercontent.com/Voldigoad-Holdings/nexus-dl/main/.env.example" `
    -OutFile "$env:APPDATA\nexusdl\.env"
```

### Generate secrets

```powershell
# Run 3 times, paste each value into .env
python -c "import secrets; print(secrets.token_hex(32))"
```

Edit `%APPDATA%\nexusdl\.env` (open with Notepad, VS Code, or any editor):

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

**Note**: paths in `.env` must use **forward slashes** or **escaped
backslashes**:

```dotenv
# ✅ RIGHT
NEXUSDL_DOWNLOAD_DIR=C:/Users/Alice/Downloads/NexusDL
NEXUSDL_DOWNLOAD_DIR=C:\\Users\\Alice\\Downloads\\NexusDL

# ❌ WRONG — single backslash is interpreted as escape
NEXUSDL_DOWNLOAD_DIR=C:\Users\Alice\Downloads\NexusDL
```

Or use `~` (expanded by `platformdirs`):

```dotenv
NEXUSDL_DOWNLOAD_DIR=~/Downloads/NexusDL
```

### Data directories (default)

NexusDL uses `platformdirs` for cross-platform paths. On Windows:

| Directory | Path | Contents |
|---|---|---|
| **Config** | `%APPDATA%\nexusdl\` | `.env`, `config.yaml` |
| **Data** | `%LOCALAPPDATA%\nexusdl\` | `library.db`, `cookies.enc` |
| **Cache** | `%LOCALAPPDATA%\nexusdl\cache\` | HTTP cache, temp files |
| **Logs** | `%LOCALAPPDATA%\nexusdl\logs\` | Log files |
| **Downloads** | `%USERPROFILE%\Downloads\NexusDL\` | Default download dir |

Override any of these in `.env`:

```dotenv
NEXUSDL_DOWNLOAD_DIR=D:/Manga
NEXUSDL_LIBRARY_DB_PATH=D:/nexusdl/library.db
```

### Shell completion (PowerShell)

```powershell
# Generate the completion script
nexusdl --show-completion powershell | Out-String | Invoke-Expression

# Save permanently
nexusdl --show-completion powershell > $env:USERPROFILE\nexusdl-completion.ps1
Add-Content -Path $PROFILE -Value ". `"$env:USERPROFILE\nexusdl-completion.ps1`""
```

Or, since you rarely use PowerShell completion with Typer, use
**`--help`** on each command.

---

## First download

### 1. Check that NexusDL works

```powershell
nexusdl --version
# → 0.1.0

nexusdl sites
# → list of supported sites
```

### 2. Search

```powershell
nexusdl search "one piece" --sites mangadex,mangafire
```

### 3. Download

```powershell
nexusdl download "https://mangadex.org/title/..." `
  --chapters 1-3 `
  --format CBZ `
  --dest "$env:USERPROFILE\Downloads\Manga"
```

**Note**: PowerShell uses **backtick** (`` ` ``) for line continuation,
not backslash (`\`).

### 4. Try the TUI

**Important**: the TUI (Textual) requires **Windows Terminal** or a
modern console. **Do NOT** use the legacy `cmd.exe`.

```powershell
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

### 5. Try the GUI

```powershell
nexusdl-gui
```

CustomTkinter on Windows uses **Win32**. No dependencies needed beyond
Python.

### 6. Try the Web UI (optional)

```powershell
nexusdl-web
# → http://localhost:8000 (API)
# → http://localhost:3000 (frontend, if built)
```

---

## Optional components

### Playwright (Cloudflare bypass)

```powershell
# Install the Python package (already included in [all])
pip install nexusdl[playwright]

# Install the Chromium browser
playwright install chromium
```

**Enable** in `.env`:

```dotenv
NEXUSDL_PLAYWRIGHT_ENABLED=true
```

**Disk space**: ~250 MB per browser (Windows binaries are larger).

**ARM64 note**: Playwright on Windows ARM64 is **supported** since v1.44.
Chromium runs natively (no emulation).

### FlareSolverr (alternative)

```powershell
# Requires Docker Desktop
docker run -d `
  --name flaresolverr `
  --restart unless-stopped `
  -p 8191:8191 `
  ghcr.io/flaresolverr/flaresolverr:latest
```

**Enable** in `.env`:

```dotenv
NEXUSDL_FLARESOLVERR_URL=http://localhost:8191/v1
```

### Redis (optional)

Redis on Windows is **unofficial** (Microsoft's port is outdated). Use
**WSL2** or **Memurai** (Redis-compatible).

**WSL2**:

```powershell
wsl --install -d Ubuntu
# Then in WSL:
sudo apt install redis-server
```

**Memurai**:

```powershell
winget install Memurai.MemuraiDeveloper
```

**Enable** in `.env`:

```dotenv
NEXUSDL_REDIS_URL=redis://localhost:6379/0
```

---

## Native .exe executable

If you don't want Python, download the **`nexusdl.exe`** standalone from
the [releases page](https://github.com/Voldigoad-Holdings/nexus-dl/releases).

### Download

```powershell
# Detect architecture
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }

# Download
Invoke-WebRequest `
    -Uri "https://github.com/Voldigoad-Holdings/nexus-dl/releases/latest/download/nexusdl-windows-${arch}.exe" `
    -OutFile "$env:USERPROFILE\Downloads\nexusdl.exe"

# Verify checksum
Invoke-WebRequest `
    -Uri "https://github.com/Voldigoad-Holdings/nexus-dl/releases/latest/download/nexusdl-windows-${arch}.exe.sha256" `
    -OutFile "$env:USERPROFILE\Downloads\nexusdl.exe.sha256"
Get-FileHash -Algorithm SHA256 "$env:USERPROFILE\Downloads\nexusdl.exe"
# Compare with the .sha256 file
```

### Install

```powershell
# Create a directory for binaries
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\bin"

# Move the .exe
Move-Item "$env:USERPROFILE\Downloads\nexusdl.exe" "$env:USERPROFILE\bin\nexusdl.exe"

# Add to PATH
[Environment]::SetEnvironmentVariable(
    "Path",
    "$env:Path;$env:USERPROFILE\bin",
    "User"
)

# Restart PowerShell
nexusdl --version
```

### Windows Defender / SmartScreen

On first run, Windows Defender **SmartScreen** may block the `.exe` with:

> "Windows protected your PC — Microsoft Defender SmartScreen prevented
> an unrecognized app from starting."

**Why**: the `.exe` is **unsigned**. Code signing requires an **EV
certificate** (~$300/year). NexusDL is a volunteer open-source project
without funding.

**Option A — Click "More info" → "Run anyway"** (one-time):

1. In the SmartScreen dialog, click **More info**.
2. Click **Run anyway**.

**Option B — Unblock via PowerShell**:

```powershell
Unblock-File -Path "$env:USERPROFILE\bin\nexusdl.exe"
```

**Option C — Add to Defender exclusions** (not recommended):

```powershell
# Admin PowerShell
Add-MpPreference -ExclusionPath "$env:USERPROFILE\bin\nexusdl.exe"
```

### Notes

- Binary is **~35 MB** (Windows binaries are larger than Linux/macOS).
- Includes Python 3.12 + core + CLI + TUI.
- **Does not include** Playwright browsers (install separately).
- **Does not include** GUI (use `nexusdl-gui` from pip).

---

## WSL2 alternative

If you have trouble with the native Windows install, **WSL2** gives you a
full Linux environment.

### Install WSL2

```powershell
# Admin PowerShell
wsl --install

# Restart, then:
wsl --set-default-version 2

# Install Ubuntu
wsl --install -d Ubuntu

# Enter Ubuntu
wsl
```

### Install NexusDL in WSL2

Then follow the [Linux installation guide](linux.md) **inside** WSL2.

### Access files from Windows

WSL2 mounts Windows drives under `/mnt/c/`, `/mnt/d/`, etc.:

```bash
# In WSL2
nexusdl download "https://..." \
  --dest /mnt/c/Users/Alice/Downloads/Manga
```

Downloads are visible in Windows Explorer at
`C:\Users\Alice\Downloads\Manga`.

### WSL2 vs native

| Aspect | Native | WSL2 |
|---|---|---|
| **Setup** | ~10 min | ~15 min |
| **Performance** | Native | Native (with WSL2 kernel) |
| **Filesystem** | Native NTFS | ext4 (faster) but `/mnt/c/` is slow |
| **GUI** | Native | Requires WSLg (Windows 11) or X server |
| **Docker** | Docker Desktop | Docker inside WSL2 |
| **Best for** | Casual users | Developers, power users |

---

## Windows-specific notes

### Path syntax

**Forward slashes** work everywhere in Python and NexusDL:

```powershell
nexusdl download "https://..." --dest "C:/Users/Alice/Manga"  # ✅
nexusdl download "https://..." --dest "C:\Users\Alice\Manga"  # ✅
nexusdl download "https://..." --dest "C:\Users\Alice\Manga"  # ❌ (in .env: `\U` is an escape)
```

In `.env` files, **always use forward slashes** or double backslashes:

```dotenv
NEXUSDL_DOWNLOAD_DIR=C:/Users/Alice/Manga       # ✅
NEXUSDL_DOWNLOAD_DIR=C:\\Users\\Alice\\Manga    # ✅
NEXUSDL_DOWNLOAD_DIR=C:\Users\Alice\Manga       # ❌ `\U` is a Python escape
```

### Long paths (> 260 characters)

Windows **limits paths to 260 characters** by default. Some manga titles
are very long, and the download path + title can exceed this.

**Enable long paths**:

1. Open **Registry Editor** (`regedit`).
2. Navigate to
   `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\FileSystem`.
3. Set `LongPathsEnabled` to **1**.
4. Restart.

Or via PowerShell (admin):

```powershell
Set-ItemProperty `
    -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
    -Name 'LongPathsEnabled' `
    -Value 1
```

**Alternative**: use a short download directory:

```dotenv
NEXUSDL_DOWNLOAD_DIR=C:/Manga
```

### Antivirus / Defender

Windows Defender **scans** every file NexusDL writes. This slows downloads
by 30–50 %.

**Option A — Exclude the download directory** (recommended):

```powershell
# Admin PowerShell
Add-MpPreference -ExclusionPath "$env:USERPROFILE\Downloads\NexusDL"
```

**Option B — Exclude the DB directory**:

```powershell
Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\nexusdl"
```

**Do NOT** disable Defender entirely — just exclude the specific paths.

### File system case-insensitivity

Windows NTFS is **case-insensitive** (but case-preserving). `OnePiece.cbz`
and `onepiece.cbz` are the **same file**.

**Impact**: if two mangas differ only by case, they may overwrite each
other.

**Workaround**: no easy fix. Rename conflicts manually after downloading,
or use the CLI with `--dest` per manga.

### Line endings

NexusDL writes files with **LF** endings (cross-platform). If you use
`--format FOLDER` and open `.txt` files in Notepad, they may show `^M`
characters.

**Solution**: use `--format CBZ` or `--format PDF` (binary formats, no
line endings).

### Execution policy

PowerShell **blocks** scripts by default:

```
.\activate.ps1 : File ... cannot be loaded because running scripts is disabled on this system.
```

**Solution** for the current user:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Do NOT** set `Unrestricted` or `Bypass` globally.

### Windows Terminal

**Recommended** for the TUI. Install from
[Microsoft Store](https://aka.ms/terminal) or via winget:

```powershell
winget install Microsoft.WindowsTerminal
```

Set it as the default terminal in **Windows Settings → System → For
developers → Terminal**.

### GUI on Windows

CustomTkinter uses **Win32** (no extra dependencies). Works on Windows
10 + 11, x64 + ARM64.

If the GUI looks **blurry** on high-DPI displays, set DPI scaling:

```powershell
# Right-click nexusdl-gui.exe → Properties → Compatibility
# → Override high DPI scaling → Application
```

---

## Uninstall

### Method 1 (uv)

```powershell
uv tool uninstall nexusdl
```

### Method 2 (pipx)

```powershell
pipx uninstall nexusdl
```

### Method 3 (venv)

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.venvs\nexusdl"
```

### Method 4 (.exe)

```powershell
Remove-Item "$env:USERPROFILE\bin\nexusdl.exe"
# Remove from PATH manually
```

### Remove data (optional)

```powershell
Remove-Item -Recurse -Force "$env:APPDATA\nexusdl"
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\nexusdl"

# Downloads (careful!)
# Remove-Item -Recurse -Force "$env:USERPROFILE\Downloads\NexusDL"
```

---

## Troubleshooting

### `nexusdl: command not found`

The binary is not in your `PATH`.

**uv/pipx**:

```powershell
[Environment]::SetEnvironmentVariable(
    "Path",
    "$env:Path;$env:USERPROFILE\.local\bin",
    "User"
)
# Restart PowerShell
```

**venv**:

```powershell
& $env:USERPROFILE\.venvs\nexusdl\Scripts\Activate.ps1
```

### `python: command not found`

Python is not installed, or not in `PATH`.

**Solution**:

```powershell
# Check
Get-Command python

# If empty, reinstall with winget
winget install Python.Python.3.12

# Restart PowerShell
```

Or, if you used python.org installer, re-run it with "Add Python to PATH"
checked.

### `error: externally-managed-environment` (PEP 668)

**This error does NOT occur on Windows.** PEP 668 is Linux-only (for
system-managed Python).

But you **may** see `pip: command not found` if using `uv`'s Python. Use:

```powershell
uv pip install ...
```

Or use the venv approach.

### `'nexusdl' is not recognized as an internal or external command`

Same as `command not found`. See above.

### `Cannot find the path specified`

The download directory doesn't exist.

```powershell
New-Item -ItemType Directory -Force -Path "C:\Manga"
```

Or use `~/`:

```dotenv
NEXUSDL_DOWNLOAD_DIR=~/Downloads/NexusDL
```

### `The file name(s) would be too long for the destination folder`

Windows long path limit. See [Long paths](#long-paths--260-characters).

### `Access is denied` on download directory

Permissions issue.

```powershell
# Check
Get-Acl "C:\Users\Alice\Downloads\NexusDL"

# Grant permission
icacls "C:\Users\Alice\Downloads\NexusDL" /grant "$env:USERNAME:(OI)(CI)F" /T
```

### SmartScreen blocks `nexusdl.exe`

See [Windows Defender / SmartScreen](#windows-defender--smartscreen).

### TUI renders incorrectly

You're likely using the legacy `cmd.exe`. Use **Windows Terminal**:

```powershell
winget install Microsoft.WindowsTerminal
```

Then open Windows Terminal and run `nexusdl-tui` again.

### `TclError: no display name and no $DISPLAY`

You're running over SSH without X11 forwarding. Use CLI or TUI instead.

### `429 Too Many Requests`

Reduce concurrency:

```dotenv
NEXUSDL_MAX_CONCURRENT_DOWNLOADS=2
NEXUSDL_DEFAULT_RATE_LIMIT_PER_SECOND=1.0
```

### Site is blocked (Cloudflare)

```powershell
pip install nexusdl[playwright]
playwright install chromium
# In .env: NEXUSDL_PLAYWRIGHT_ENABLED=true
```

### `database is locked`

SQLite doesn't support concurrent writes.

**Solution**: don't run multiple NexusDL instances.

### GUI is blurry on high-DPI

Right-click `nexusdl-gui.exe` → Properties → Compatibility → Override
high DPI scaling → **Application**.

### Logs to debug

```powershell
# Verbose logs
$env:NEXUSDL_LOG_LEVEL="DEBUG"
nexusdl search "one piece"

# Follow logs
Get-Content "$env:LOCALAPPDATA\nexusdl\logs\nexusdl.log" -Wait -Tail 50
```

### Antivirus blocks downloads

Add the download directory to exclusions. See
[Antivirus / Defender](#antivirus--defender).

---

## See also

- [Docker installation](docker.md) — recommended for servers
- [Linux installation](linux.md)
- [macOS installation](macos.md)
- [Configuration](../usage/configuration.md) — all env vars
- [CLI usage](../usage/cli.md) — all commands
- [Formats](../usage/formats.md) — packaging formats
- [GUI usage](../usage/gui.md) — GUI-specific guide
- [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) — ask for help

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
