# GUI Usage

> **Audience**: desktop users who prefer a graphical interface over the CLI
> or TUI.
>
> **Platforms**: Linux (X11/Wayland), macOS 12+, Windows 10+.
> **Not available**: headless servers, Docker without X11 forwarding.
>
> **Not for you?** → [CLI usage](cli.md), [Web usage](web.md).

NexusDL ships with a **native desktop GUI** built with
[CustomTkinter](https://github.com/TomSchimansky/CustomTkinter). It provides
a modern, dark-mode-first interface with the same capabilities as the CLI.

```{note}
The GUI is a **thin layer** over `core/`. It calls the same
`DownloadManager`, `SiteRegistry`, and `EventBus` as the CLI. There is no
separate logic — what works in the GUI works in the CLI, and vice versa.
```

---

## Table of contents

1. [Launching the GUI](#launching-the-gui)
2. [First launch](#first-launch)
3. [Interface overview](#interface-overview)
4. [The 5 views](#the-5-views)
5. [Keyboard shortcuts](#keyboard-shortcuts)
6. [Themes](#themes)
7. [Settings](#settings)
8. [System tray](#system-tray)
9. [Notifications](#notifications)
10. [Limitations](#limitations)
11. [Troubleshooting](#troubleshooting)
12. [See also](#see-also)

---

## Launching the GUI

### From the command line

```bash
nexusdl-gui
```

### From the desktop

- **Linux**: search **"NexusDL"** in your application launcher (if installed
  via `.deb` or `.AppImage`).
- **macOS**: double-click **`/Applications/NexusDL.app`**.
- **Windows**: search **"NexusDL"** in the Start menu (if installed via
  `.exe` or winget).

### With a specific config

```bash
NEXUSDL_CONFIG_DIR=/path/to/config nexusdl-gui
```

### With a specific log level

```bash
NEXUSDL_LOG_LEVEL=DEBUG nexusdl-gui
```

---

## First launch

On first launch, the GUI:

1. **Loads `.env`** from `~/.config/nexusdl/` (or `%APPDATA%\nexusdl\`
   on Windows).
2. **Validates secrets**. If `NEXUSDL_SECRET_KEY`,
   `NEXUSDL_JWT_SECRET`, or `NEXUSDL_COOKIE_ENCRYPTION_KEY` are missing,
   it shows a **Setup Wizard**.
3. **Initializes the library** (`library.db`). If the file doesn't exist,
   it creates it.
4. **Loads `sites.yaml`** (read-only, from the package).
5. **Opens the main window**.

### Setup Wizard

If secrets are missing, the wizard asks you to:

1. **Generate secrets** (one-click)
2. **Choose a download directory** (default: `~/Downloads/NexusDL/`)
3. **Choose a theme** (Dark, Light, System)
4. **Enable or disable** adult content
5. **Confirm** and write `.env`

The wizard writes to:

| Platform | File |
|---|---|
| Linux | `~/.config/nexusdl/.env` |
| macOS | `~/Library/Application Support/nexusdl/.env` |
| Windows | `%APPDATA%\nexusdl\.env` |

### Skip the wizard

If you already have a `.env` (e.g. from CLI usage), the wizard is skipped
automatically.

---

## Interface overview

```
┌─────────────────────────────────────────────────────────────────┐
│  NexusDL                                         [─] [□] [×]    │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────┐                                                   │
│  │          │   Search                                          │
│  │  NexusDL │   ┌───────────────────────────────────────────┐   │
│  │          │   │ Search query...              [🔍 Search] │   │
│  │ ──────── │   └───────────────────────────────────────────┘   │
│  │          │                                                   │
│  │ 🔍 Search│   ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │ ⬇ Down-  │   │          │ │          │ │          │          │
│  │   loads  │   │  Cover   │ │  Cover   │ │  Cover   │          │
│  │ 📚 Library│  │          │ │          │ │          │          │
│  │ ⚙ Settings│  └──────────┘ └──────────┘ └──────────┘          │
│  │          │   One Piece    Naruto       Bleach               │
│  │          │   Oda E.       Kishimoto    Kubo T.              │
│  │ ──────── │   [MangaDex]   [MangaDex]   [MangaDex]           │
│  │          │                                                   │
│  │ v0.1.0   │                                                   │
│  └──────────┘                                                   │
├─────────────────────────────────────────────────────────────────┤
│  Status: Ready  ·  60 sites loaded  ·  0 downloads active       │
└─────────────────────────────────────────────────────────────────┘
```

### Sidebar (left)

5 navigation buttons:

| Icon | View | Shortcut |
|---|---|---|
| 🔍 | Search | `Ctrl+1` |
| ⬇ | Downloads | `Ctrl+2` |
| 📚 | Library | `Ctrl+3` |
| ⚙ | Settings | `Ctrl+4` |
| (bottom) | Version | — |

### Main content (right)

Changes based on the selected view. See [The 5 views](#the-5-views).

### Status bar (bottom)

Live status:

- **Left**: current state (`Ready`, `Searching...`, `Downloading...`)
- **Center**: `N sites loaded`, `M downloads active`
- **Right**: connection status (green = online, orange = degraded, red = offline)

---

## The 5 views

### 1. Search view

**Purpose**: find mangas across all supported sites.

**Layout**:

- **Top**: search bar with query input, site filter, and "Search" button
- **Center**: grid of `MangaCard` widgets (cover, title, author, site)
- **Bottom**: pagination controls (`<`, `1/5`, `>`)

**Actions**:

| Action | Result |
|---|---|
| Type + Enter | Trigger search |
| Click a card | Open **Manga Detail** (chapters list) |
| Right-click a card | Context menu (Download all, Copy URL, Open in browser) |
| Click `⋮` on a card | Same context menu |

**Filters**:

- **Site**: dropdown of supported sites (or "All")
- **Language**: dropdown (`Any`, `en`, `fr`, `kr`, …)
- **Adult**: toggle (only visible if `NEXUSDL_ALLOW_ADULT=true`)

**Performance**:

- Search **across all sites** is parallel (uses `SiteRegistry.search_all`).
- Results appear as they arrive (streaming).
- Failed sites are shown as an orange badge with the error.

### 2. Downloads view

**Purpose**: monitor download tasks in real time.

**Layout**:

- **Top**: filter (`All`, `Running`, `Paused`, `Done`, `Failed`)
- **Center**: list of `DownloadItem` widgets
- **Bottom**: controls (`Pause all`, `Resume all`, `Cancel all`)

**Each `DownloadItem` shows**:

- Manga title + chapter number
- Progress bar with percentage
- Current page / total pages
- Speed (pages/s, MB/s)
- ETA
- Status icon (⏳ pending, ⬇ running, ⏸ paused, ✅ done, ❌ failed)
- Actions: pause, resume, cancel, open folder, retry

**Live updates**: The GUI subscribes to `EventBus` (`download.progress`,
`download.completed`, `download.failed`). Updates are throttled to **10 Hz**
per task to avoid UI lag.

**Batch operations**:

- **Pause all**: pauses every running task.
- **Resume all**: resumes every paused task.
- **Cancel all**: cancels every pending/running task (with confirmation).

### 3. Library view

**Purpose**: browse downloaded mangas.

**Layout**:

- **Top**: search bar (FTS5 full-text), filter (`All`, `Reading`, `Completed`, `Not started`)
- **Center**: grid of downloaded mangas
- **Bottom**: `Update all` button (checks for new chapters)

**Each item shows**:

- Cover (or placeholder)
- Title
- Author
- `X / Y chapters` (downloaded / total)
- Last read chapter + page
- Progress ring (percentage read)

**Actions**:

| Action | Result |
|---|---|
| Click a manga | Open **Library Detail** (chapters list) |
| Double-click | Resume reading from last page |
| Right-click | Context menu (Update, Remove, Open folder, Reveal in file manager) |
| Drag & drop | Move to another library folder (future) |

**FTS5 search**: type in the search bar → instant results from
`library.db` FTS5 table. Searches title, author, description, genres.

### 4. Settings view

**Purpose**: edit user preferences (subset of `.env`).

**Sections**:

#### General

| Setting | Type | Default |
|---|---|---|
| Language | dropdown | `en` |
| Theme | dropdown (Dark, Light, System) | `System` |
| Start minimized | toggle | `false` |
| Check for updates | dropdown (Never, Daily, Weekly) | `Weekly` |

#### Downloads

| Setting | Type | Default |
|---|---|---|
| Download directory | path picker | `~/Downloads/NexusDL` |
| Default format | dropdown (FOLDER, ZIP, CBZ, CB7, PDF, CBT) | `CBZ` |
| Max concurrent downloads | slider (1–10) | `3` |
| Max concurrent pages per chapter | slider (1–16) | `8` |

#### Content

| Setting | Type | Default |
|---|---|---|
| Allow adult content | toggle | `false` |
| Respect robots.txt | toggle | `true` |
| Content filter (strict) | toggle | `false` |

#### Network

| Setting | Type | Default |
|---|---|---|
| HTTP timeout | number (seconds) | `30` |
| Max retries | number | `3` |
| Enable Playwright | toggle | `false` |
| FlareSolverr URL | text | (empty) |
| Proxy URL | text | (empty) |

#### Advanced

| Setting | Type | Default |
|---|---|---|
| Log level | dropdown | `INFO` |
| Log format | dropdown | `pretty` |
| Open config folder | button | — |
| Open logs folder | button | — |
| Reset settings | button | — |

**Note**: changes are **saved to `.env`** immediately. A confirmation
dialog appears for destructive changes (reset settings).

```{warning}
**Some settings require a restart** (e.g. log level, max concurrent
downloads). The GUI shows a "Restart required" banner.
```

### 5. Manga Detail view (modal)

**Purpose**: view a manga's info and chapters, start downloads.

**Layout**:

- **Top**: cover, title, author, status, year, genres
- **Center**: description (scrollable)
- **Bottom**: list of chapters with checkboxes (multi-select)

**Actions**:

| Action | Result |
|---|---|
| Click a chapter | Open preview (page thumbnails) |
| Check multiple chapters | Select for batch download |
| Click "Download selected" | Queue selected chapters |
| Click "Download all" | Queue all chapters |
| Click "Update" | Re-fetch chapter list from site |

**Filters**:

- **Language**: dropdown (source site may have multiple)
- **Status**: `All`, `Downloaded`, `Not downloaded`

---

## Keyboard shortcuts

### Global

| Shortcut | Action |
|---|---|
| `Ctrl+Q` | Quit |
| `Ctrl+,` | Open Settings |
| `F1` | Open Help |
| `F5` | Refresh current view |
| `Ctrl+F` | Focus search bar |
| `Esc` | Close modal / clear search |

### Navigation

| Shortcut | Action |
|---|---|
| `Ctrl+1` | Search view |
| `Ctrl+2` | Downloads view |
| `Ctrl+3` | Library view |
| `Ctrl+4` | Settings view |
| `Ctrl+Tab` | Next view |
| `Ctrl+Shift+Tab` | Previous view |

### Search view

| Shortcut | Action |
|---|---|
| `Enter` | Trigger search |
| `↓` / `↑` | Navigate results |
| `Enter` (on card) | Open manga detail |
| `Ctrl+D` | Download all |

### Downloads view

| Shortcut | Action |
|---|---|
| `Space` | Pause/resume selected |
| `Delete` | Cancel selected |
| `Ctrl+Shift+P` | Pause all |
| `Ctrl+Shift+R` | Resume all |
| `Ctrl+Shift+C` | Cancel all |

### Library view

| Shortcut | Action |
|---|---|
| `Enter` | Open manga detail |
| `Ctrl+O` | Open folder |
| `Delete` | Remove from library (with confirmation) |
| `Ctrl+U` | Update all |

### macOS

On macOS, `Ctrl` is replaced by **`⌘`** (Command). The shortcuts above are
the same, with `⌘` instead of `Ctrl`.

---

## Themes

NexusDL ships with **three themes**:

| Theme | Description |
|---|---|
| **Dark** | Dark background, indigo accents (default) |
| **Light** | Light background, indigo accents |
| **System** | Follows OS preference (dark or light) |

### Change theme

**Settings → General → Theme**, or via `.env`:

```dotenv
NEXUSDL_GUI_THEME=dark   # dark | light | system
```

### Custom theme (advanced)

The theme is defined in `interfaces/gui/theme.py`. You can override colors
by setting environment variables:

```dotenv
NEXUSDL_GUI_ACCENT_COLOR=#6366f1   # indigo 500
NEXUSDL_GUI_BG_COLOR=#0f172a       # slate 900
```

**Note**: custom color env vars are planned for v0.2.0.

### Accessibility

- **High contrast**: not yet supported (planned).
- **Font size**: respects OS DPI settings.
- **Screen reader**: limited (CustomTkinter doesn't fully support ARIA).
  Use the [CLI](cli.md) or [TUI](cli.md#tui) if you rely on screen readers.

---

## Settings

### Where settings are stored

Settings are stored in **`.env`** (same file used by CLI, TUI, Web). This
means:

- ✅ **One source of truth** — GUI, CLI, and Web read the same `.env`.
- ✅ **Portable** — copy `.env` to another machine, same settings.
- ✅ **Version-controllable** — you can commit a sanitized `.env.example`.

### Precedence

`.env` < environment variables < **GUI changes** (immediate).

**Note**: GUI changes are written to `.env` and take effect immediately
(for runtime settings) or require restart (for startup settings).

### Backup and restore

```bash
# Backup
cp ~/.config/nexusdl/.env ~/.config/nexusdl/.env.backup

# Restore
cp ~/.config/nexusdl/.env.backup ~/.config/nexusdl/.env
```

The GUI has **Settings → Advanced → Backup / Restore** buttons.

### Reset to defaults

**Settings → Advanced → Reset settings** (confirmation required).

This:
- Deletes `~/.config/nexusdl/.env`
- Removes `~/.local/share/nexusdl/config.yaml` (if exists)
- Shows the Setup Wizard on next launch

**Does NOT delete**:
- `library.db` (your mangas)
- `cookies.enc` (your sessions)
- `~/Downloads/NexusDL/` (your files)

---

## System tray

On launch, the GUI adds an **icon to the system tray**:

- **Linux**: depends on DE (GNOME needs an extension, KDE supports natively).
- **macOS**: menu bar icon.
- **Windows**: system tray.

### Tray menu

| Action | Result |
|---|---|
| Open | Bring window to front |
| Pause all | Pause all downloads |
| Resume all | Resume all downloads |
| Quit | Quit NexusDL |

### Minimize to tray

**Settings → General → Start minimized**: when checked, the GUI starts in
the tray (no window).

Click the tray icon to show the window.

### Disable tray

```dotenv
NEXUSDL_GUI_TRAY_ENABLED=false
```

**Note**: `NEXUSDL_GUI_TRAY_ENABLED` is planned for v0.2.0.

---

## Notifications

NexusDL sends **native notifications** for:

- ✅ Download completed
- ❌ Download failed
- 🔄 Update available

### Linux

Uses `notify-send` (requires `libnotify`):

```bash
sudo apt install libnotify-bin    # Debian/Ubuntu
sudo dnf install libnotify         # Fedora
```

### macOS

Uses `osascript` (built-in). Requires Notification Center permissions on
first use.

### Windows

Uses `win10toast` (bundled). No setup needed.

### Disable notifications

```dotenv
NEXUSDL_GUI_NOTIFICATIONS=false
```

**Note**: `NEXUSDL_GUI_NOTIFICATIONS` is planned for v0.2.0.

---

## Limitations

The GUI is **not** a full-featured desktop app. Known limitations:

| Limitation | Reason | Workaround |
|---|---|---|
| **No image viewer** | Out of scope | Use Komga, Kavita, or YACReader |
| **No page preview** | CustomTkinter can't display images efficiently | Use [Web UI](web.md) or an external reader |
| **No plugin management** | Planned for v0.2.0 | Use CLI: `nexusdl plugins` |
| **No multi-window** | Single-window by design | Use [Web UI](web.md) for multi-tab |
| **No drag & drop between views** | CustomTkinter limitation | Use context menu |
| **No custom themes (yet)** | Planned for v0.2.0 | Only Dark, Light, System |
| **No screen reader support** | CustomTkinter limitation | Use [CLI](cli.md) or [TUI](cli.md#tui) |
| **No Wayland support (native)** | Tk uses XWayland | Works via XWayland (default) |
| **No ARM64 builds (Windows)** | PyInstaller limitation | Use Python install |

### Known bugs

- **Blurry on high-DPI displays (Windows)**: right-click the `.exe` →
  Properties → Compatibility → Override high DPI scaling → Application.
- **Tray icon missing on GNOME**: install the
  [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).
- **Wayland flicker**: switch to X11 session (`GDK_BACKEND=x11`).

---

## Troubleshooting

### GUI doesn't start

**Cause**: Python or CustomTkinter missing.

**Fix**:

```bash
# Reinstall with GUI extra
pip install "nexusdl[gui]"

# Verify
python -c "import customtkinter; print(customtkinter.__version__)"
```

### `TclError: no display name and no $DISPLAY`

**Cause**: running on a **headless server** (SSH, Docker without X11).

**Fix**:

- Use the **CLI** (`nexusdl`) or **TUI** (`nexusdl-tui`).
- Or enable X11 forwarding over SSH (`ssh -X`).
- Or use the [Web UI](web.md).

### Window is blank / black

**Cause**: GPU driver or Tk compatibility issue.

**Fix**:

- **Wayland**: force X11 (`GDK_BACKEND=x11 nexusdl-gui`).
- **NVIDIA**: install proprietary drivers.
- **VM**: enable 3D acceleration.

### Blurry text on Windows

**Fix**: right-click `nexusdl-gui.exe` → Properties → Compatibility →
Override high DPI scaling → **Application**.

### Search returns no results

**Cause**: rate limit, network issue, or adult content not enabled.

**Fix**:

- Check `NEXUSDL_LOG_LEVEL=DEBUG nexusdl-gui` logs.
- Ensure `.env` has valid secrets.
- If searching adult sites, set `NEXUSDL_ALLOW_ADULT=true`.

### Downloads don't start

**Cause**: destination not writable, or site blocked.

**Fix**:

- Check that `NEXUSDL_DOWNLOAD_DIR` exists and is writable.
- Check the **Downloads** view for error messages.
- Check the **Logs** (Settings → Advanced → Open logs folder).

### Notifications not showing (Linux)

**Cause**: `notify-send` missing.

**Fix**:

```bash
sudo apt install libnotify-bin
# or
sudo dnf install libnotify
```

### System tray icon missing (GNOME)

**Cause**: GNOME hides tray icons by default.

**Fix**: install
[AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).

### GUI freezes during download

**Cause**: rare — a blocking operation on the main thread.

**Fix**:

- Report a bug with the steps to reproduce.
- Use the [CLI](cli.md) as a workaround.

### Reset the GUI

**Fix**: delete settings:

```bash
rm -rf ~/.config/nexusdl/nexusdl-gui.json
```

The GUI recreates it on next launch.

**Does NOT delete** `.env`, `library.db`, or your downloads.

### Check GUI logs

```bash
# Linux/macOS
tail -f ~/.local/state/nexusdl/logs/nexusdl.log

# Windows
Get-Content "$env:LOCALAPPDATA\nexusdl\logs\nexusdl.log" -Wait -Tail 50
```

---

## See also

- [CLI usage](cli.md) — command-line interface
- [Web usage](web.md) — browser-based interface
- [Configuration](configuration.md) — all env vars
- [Formats](formats.md) — packaging formats
- [Linux installation](../installation/linux.md) — GUI setup on Linux
- [macOS installation](../installation/macos.md) — `.app` bundle
- [Windows installation](../installation/windows.md) — `.exe` bundle
- [CustomTkinter docs](https://customtkinter.tomschimansky.com/) — the GUI framework
- [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) — ask for help

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
