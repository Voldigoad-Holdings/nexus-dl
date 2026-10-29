# CLI Usage

> **Audience**: users who want to drive NexusDL from the terminal.
>
> **Recommended for**: scripting, cron jobs, batch processing, power users.
>
> **Not for you?** → [GUI](gui.md), [TUI](#tui), [Web](web.md).

NexusDL's CLI is built with [Typer](https://typer.tiangolo.com/). Every
command supports `--help`, shell completion, and machine-readable JSON
output.

```{note}
The CLI is the **reference implementation** — the TUI, GUI, and Web UI
call the same `core/` APIs. If a feature exists in the CLI, it exists
everywhere.
```

---

## Table of contents

1. [Installation](#installation)
2. [Global options](#global-options)
3. [Command overview](#command-overview)
4. [`nexusdl search`](#nexusdl-search)
5. [`nexusdl info`](#nexusdl-info)
6. [`nexusdl download`](#nexusdl-download)
7. [`nexusdl sites`](#nexusdl-sites)
8. [`nexusdl library`](#nexusdl-library)
9. [`nexusdl update`](#nexusdl-update)
10. [`nexusdl config`](#nexusdl-config)
11. [`nexusdl plugins`](#nexusdl-plugins)
12. [`nexusdl users`](#nexusdl-users)
13. [`nexusdl benchmark`](#nexusdl-benchmark)
14. [TUI](#tui)
15. [Scripting examples](#scripting-examples)
16. [Shell completion](#shell-completion)
17. [Troubleshooting](#troubleshooting)
18. [See also](#see-also)

---

## Installation

```bash
pip install "nexusdl[cli]"
# or
uv tool install "nexusdl[cli]"
```

Verify:

```bash
nexusdl --version
# → nexusdl, version 0.1.0
```

---

## Global options

Available on **every** command:

| Option | Description |
|---|---|
| `--help` | Show help for the command |
| `--version` | Show version and exit |
| `--log-level LEVEL` | Override `NEXUSDL_LOG_LEVEL` (`TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `--log-format FORMAT` | Override `NEXUSDL_LOG_FORMAT` (`json`, `text`, `pretty`) |
| `--config-dir PATH` | Override `~/.config/nexusdl/` |
| `--no-color` | Disable ANSI colors |
| `--json` | Output machine-readable JSON (where supported) |
| `--quiet` / `-q` | Suppress non-error output |
| `--verbose` / `-v` | Increase verbosity (repeatable: `-vvv`) |

### Precedence

CLI options **override** environment variables, which override `.env`,
which overrides defaults.

```bash
# .env has LOG_LEVEL=INFO
NEXUSDL_LOG_LEVEL=WARNING nexusdl --log-level DEBUG search "one piece"
# → DEBUG wins
```

---

## Command overview

```
nexusdl
├── search        Search mangas across sites
├── info          Show manga details
├── download      Download a manga or chapter
├── sites         List supported sites
├── library       Manage the local library
│   ├── list      List all downloaded mangas
│   ├── search    Full-text search in the library
│   ├── show      Show manga details
│   ├── remove    Remove a manga from the library
│   └── update    Update a manga (new chapters)
├── update        Update all mangas in the library
├── config        Manage configuration
│   ├── init      Create a `.env` file
│   ├── show      Show current configuration (secrets masked)
│   ├── paths     Show config/data/cache/logs paths
│   └── validate  Validate configuration
├── plugins       Manage plugins
│   ├── list      List installed plugins
│   ├── install   Install a plugin
│   ├── remove    Remove a plugin
│   └── info      Show plugin details
├── users         Manage users (ADMIN only)
│   ├── list      List all users
│   ├── add       Create a user
│   ├── remove    Delete a user
│   └── reset-password  Reset a user's password
├── benchmark     Run performance benchmarks
└── tui           Launch the Textual TUI
```

Get help:

```bash
nexusdl --help
nexusdl <command> --help
```

---

## `nexusdl search`

Search mangas across supported sites.

### Usage

```bash
nexusdl search QUERY [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--sites` | `str` | (all) | Comma-separated site IDs |
| `--language` | `str` | (all) | Filter by language (`en`, `fr`, …) |
| `--include-adult` | flag | `false` | Include adult sites |
| `--limit` | `int` | `20` | Max results per site |
| `--page` | `int` | `1` | Page number |
| `--json` | flag | `false` | Output JSON |
| `--format` | `str` | `text` | Output format (`text`, `json`, `table`) |

### Examples

**Simple search**:

```bash
nexusdl search "one piece"
```

Output:

```
Found 42 results across 60 sites:

  [MangaDex] One Piece — Oda Eiichiro
  [MangaFire] One Piece — Oda Eiichiro
  [MangaKakalot] One Piece — Oda Eiichiro
  ...
```

**Restrict sites**:

```bash
nexusdl search "one piece" --sites mangadex,mangafire
```

**Search by language**:

```bash
nexusdl search "one piece" --language fr
```

**Include adult**:

```bash
nexusdl search "one piece" --include-adult
```

**JSON output** (for scripting):

```bash
nexusdl search "one piece" --json | jq '.[0]'
```

Output:

```json
{
  "site_id": "mangadex",
  "site_name": "MangaDex",
  "manga_id": "a1b2c3d4",
  "title": "One Piece",
  "author": "Oda Eiichiro",
  "language": "en",
  "content_rating": "SAFE",
  "url": "https://mangadex.org/title/...",
  "match_score": 0.98
}
```

**Table output** (for terminals):

```bash
nexusdl search "one piece" --format table
```

```
┌───────────┬───────────┬──────────────────────────────┬─────────────────┐
│ Site      │ ID        │ Title                        │ Author          │
├───────────┼───────────┼──────────────────────────────┼─────────────────┤
│ MangaDex  │ a1b2c3d4  │ One Piece                    │ Oda Eiichiro    │
│ MangaFire │ e5f6g7h8  │ One Piece                    │ Oda Eiichiro    │
└───────────┴───────────┴──────────────────────────────┴─────────────────┘
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | At least one result |
| `1` | No results |
| `2` | Invalid arguments |
| `3` | All sites failed |
| `4` | Rate limited |

---

## `nexusdl info`

Show detailed manga metadata.

### Usage

```bash
nexusdl info URL [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--site` | `str` | (auto-detect from URL) | Site ID |
| `--json` | flag | `false` | Output JSON |
| `--chapters` | flag | `false` | Include chapter list |

### Examples

**Basic info**:

```bash
nexusdl info "https://mangadex.org/title/a1b2c3d4"
```

Output:

```
One Piece
═══════════════════════════════════════════════════════
Author:       Oda Eiichiro
Artist:       Oda Eiichiro
Status:       Ongoing
Year:         1997
Language:     en
Rating:       SAFE
Genres:       Action, Adventure, Comedy
Chapters:     1100
URL:          https://mangadex.org/title/a1b2c3d4

Description:
  A story about pirates...

Alternative titles:
  • ワンピース
  • One Piece (Color)
```

**With chapter list**:

```bash
nexusdl info "https://mangadex.org/title/a1b2c3d4" --chapters
```

Output adds:

```
Chapters (1100):
  #1    Romance Dawn                    53 pages  1997-07-22
  #2    They Call Him "Straw Hat Luffy" 20 pages  1997-07-29
  ...
```

**JSON output**:

```bash
nexusdl info "https://mangadex.org/title/a1b2c3d4" --json
```

---

## `nexusdl download`

Download a manga or specific chapters.

### Usage

```bash
nexusdl download URL [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--site` | `str` | (auto-detect) | Site ID |
| `--chapters` | `str` | (all) | Chapter range (`1-3`, `1,2,5`, `all`) |
| `--format` | `str` | `CBZ` | `FOLDER`, `ZIP`, `CBZ`, `CB7`, `PDF`, `CBT` |
| `--dest` | `Path` | `NEXUSDL_DOWNLOAD_DIR` | Destination directory |
| `--overwrite` | flag | `false` | Overwrite existing files |
| `--skip-existing` | flag | `true` | Skip already-downloaded chapters |
| `--priority` | `str` | `NORMAL` | `LOW`, `NORMAL`, `HIGH` |
| `--concurrency` | `int` | `3` | Max parallel chapters |
| `--page-concurrency` | `int` | `8` | Max parallel pages per chapter |
| `--json` | flag | `false` | Output JSON (one object per chapter) |
| `--no-progress` | flag | `false` | Disable progress bar (for CI) |

### Examples

**Download all chapters**:

```bash
nexusdl download "https://mangadex.org/title/a1b2c3d4"
```

**Download specific chapters**:

```bash
nexusdl download "https://mangadex.org/title/a1b2c3d4" --chapters 1-3
nexusdl download "https://mangadex.org/title/a1b2c3d4" --chapters 1,5,10
```

**Custom format**:

```bash
nexusdl download "https://..." --format CB7
nexusdl download "https://..." --format PDF
nexusdl download "https://..." --format FOLDER
```

**Custom destination**:

```bash
nexusdl download "https://..." --dest ~/Manga
nexusdl download "https://..." --dest /mnt/external/Manga
```

**High concurrency** (fast connection):

```bash
nexusdl download "https://..." --concurrency 5 --page-concurrency 16
```

**Low concurrency** (fragile site):

```bash
nexusdl download "https://..." --concurrency 1 --page-concurrency 4
```

**Scripted** (JSON output, no progress):

```bash
nexusdl download "https://..." --json --no-progress | while read -r line; do
  path=$(echo "$line" | jq -r .output_path)
  echo "Done: $path"
done
```

**Overwrite existing**:

```bash
nexusdl download "https://..." --overwrite
```

### Output (default)

```
[1/3] Chapter 001 · Romance Dawn
  Downloading 53 pages...
  ████████████████████████████████ 53/53 (100%)  12.3 MB/s
  Packaging as CBZ...
  ✓ ~/Manga/One Piece/Chapter 001.cbz (12.3 MB, 4.2s)

[2/3] Chapter 002 · They Call Him "Straw Hat Luffy"
  ...
```

### Output (JSON)

```json
{"task_id":"...","chapter_id":"...","chapter_number":1.0,"output_path":"/home/alice/Manga/One Piece/Chapter 001.cbz","bytes_downloaded":12345678,"duration_seconds":4.2,"pages_failed":[],"success":true}
{"task_id":"...","chapter_id":"...","chapter_number":2.0,"output_path":"/home/alice/Manga/One Piece/Chapter 002.cbz","bytes_downloaded":9876543,"duration_seconds":3.1,"pages_failed":[],"success":true}
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | All chapters downloaded |
| `1` | Some chapters failed |
| `2` | Invalid arguments |
| `3` | Manga not found |
| `4` | Rate limited |
| `5` | Disk full |
| `130` | Interrupted (Ctrl+C) |

### Resuming

If a download is interrupted, re-run the same command. NexusDL skips
already-downloaded pages (via SHA-256 deduplication).

---

## `nexusdl sites`

List supported sites.

### Usage

```bash
nexusdl sites [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--language` | `str` | (all) | Filter by language |
| `--include-adult` | flag | `false` | Include adult sites |
| `--json` | flag | `false` | Output JSON |
| `--format` | `str` | `table` | `table`, `json`, `plain` |

### Examples

**List all**:

```bash
nexusdl sites
```

Output:

```
┌───────────────┬───────────────┬──────────┬───────┬──────────┐
│ ID            │ Name          │ Language │ Adult │ Status   │
├───────────────┼───────────────┼──────────┼───────┼──────────┤
│ mangadex      │ MangaDex      │ en       │ ❌    │ ✅       │
│ mangafire     │ MangaFire     │ en       │ ❌    │ ✅       │
│ sushiscan     │ SushiScan     │ fr       │ ❌    │ ✅       │
│ ...           │ ...           │ ...      │ ...   │ ...      │
└───────────────┴───────────────┴──────────┴───────┴──────────┘

Total: 60 sites
```

**Filter by language**:

```bash
nexusdl sites --language fr
```

**Include adult**:

```bash
nexusdl sites --include-adult
```

**JSON** (for scripting):

```bash
nexusdl sites --json | jq '.[] | select(.language == "fr") | .id'
```

---

## `nexusdl library`

Manage the local library.

### `nexusdl library list`

List all downloaded mangas.

```bash
nexusdl library list
```

Output:

```
┌──────────────────┬──────────────┬──────────┬────────────┐
│ Title            │ Author       │ Chapters │ Size       │
├──────────────────┼──────────────┼──────────┼────────────┤
│ One Piece        │ Oda Eiichiro │ 1100/1100│ 16.2 GB    │
│ Naruto           │ Kishimoto    │ 700/700  │ 8.4 GB     │
│ ...              │ ...          │ ...      │ ...        │
└──────────────────┴──────────────┴──────────┴────────────┘

Total: 42 mangas, 54.3 GB
```

**Options**:

- `--json` — JSON output
- `--sort {title,author,chapters,size}` — sort by field
- `--reverse` — reverse sort

### `nexusdl library search`

Full-text search in the library (FTS5).

```bash
nexusdl library search "one piece"
```

Output:

```
Found 1 result:

  One Piece (MangaDex) — Oda Eiichiro
    1100 chapters, 16.2 GB
    Last read: Chapter 45, page 12 (2026-10-20)
```

### `nexusdl library show`

Show a manga's details.

```bash
nexusdl library show "One Piece"
```

Output:

```
One Piece
═══════════════════════════════════════════════════════
MangaDex · 1100/1100 chapters · 16.2 GB
Last read: Chapter 45, page 12 (2026-10-20)

Chapters:
  [✅] #1    Romance Dawn                    12.3 MB
  [✅] #2    They Call Him "Straw Hat Luffy" 9.8 MB
  ...
```

### `nexusdl library remove`

Remove a manga from the library.

```bash
nexusdl library remove "One Piece"
```

**Prompts**:

```
Remove "One Piece" from the library?
  [ ] Keep downloaded files (only remove from DB)
  [x] Delete downloaded files (~16.2 GB)

[y/N]:
```

**Options**:

- `--keep-files` — keep files on disk
- `--yes` / `-y` — skip confirmation

### `nexusdl library update`

Update a manga (fetch new chapters).

```bash
nexusdl library update "One Piece"
```

Output:

```
Updating One Piece from MangaDex...
  Current: 1100 chapters
  Latest:  1102 chapters

Found 2 new chapters:
  #1101  The Final Battle
  #1102  Epilogue

Download new chapters? [y/N]: y
[1/2] Chapter 1101 ...
```

---

## `nexusdl update`

Update **all** mangas in the library.

```bash
nexusdl update
```

Options:

- `--concurrency N` — parallel updates (default 2)
- `--yes` / `-y` — skip confirmations
- `--dry-run` — show what would be updated without downloading

Example (cron):

```bash
nexusdl update --yes --concurrency 1
```

---

## `nexusdl config`

Manage configuration.

### `nexusdl config init`

Create `.env` from template.

```bash
nexusdl config init
```

Creates `~/.config/nexusdl/.env` with placeholder secrets and opens it in
`$EDITOR`.

### `nexusdl config show`

Show current configuration (secrets masked).

```bash
nexusdl config show
```

Output:

```
app_name: NexusDL
app_environment: dev
secret_key: **********
jwt_secret: **********
cookie_encryption_key: **********
log_level: INFO
log_format: pretty
max_concurrent_downloads: 3
...
```

### `nexusdl config paths`

Show all paths.

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

### `nexusdl config validate`

Validate `.env` and `sites.yaml`.

```bash
nexusdl config validate
```

Output:

```
✅ .env valid
✅ sites.yaml valid (60 sites)
✅ database reachable
✅ Fernet key valid
```

If invalid:

```
❌ .env invalid: NEXUSDL_SECRET_KEY must be 64 hex chars
❌ sites.yaml invalid: site 'mysite' missing 'parser_class'
```

---

## `nexusdl plugins`

Manage plugins.

### `nexusdl plugins list`

```bash
nexusdl plugins list
```

Output:

```
┌──────────────────┬─────────┬─────────┬──────────┐
│ Name             │ Version │ Author  │ Enabled  │
├──────────────────┼─────────┼─────────┼──────────┤
│ discord_notifier │ 0.1.0   │ Jane D. │ ✅       │
│ image_optimizer  │ 0.2.0   │ John D. │ ✅       │
└──────────────────┴─────────┴─────────┴──────────┘
```

### `nexusdl plugins install`

```bash
nexusdl plugins install my-plugin
```

Installs from:
- **PyPI**: `nexusdl-<name>`
- **Local**: `./path/to/plugin`
- **Git**: `git+https://...`

### `nexusdl plugins remove`

```bash
nexusdl plugins remove my-plugin
```

### `nexusdl plugins info`

```bash
nexusdl plugins info discord_notifier
```

Output:

```
Name:        discord_notifier
Version:     0.1.0
Author:      Jane Doe <jane@example.com>
License:     GPL-3.0-or-later
Hooks:       on_load, on_unload, on_after_download
Requires:    nexusdl>=0.1.0, aiohttp>=3.9
Installed:   /home/alice/.config/nexusdl/plugins/discord_notifier/
```

---

## `nexusdl users`

Manage users (ADMIN only).

### `nexusdl users list`

```bash
nexusdl users list
```

Output:

```
┌────────┬───────┬─────────────────────┬────────────┐
│ ID     │ Name  │ Email               │ Role       │
├────────┼───────┼─────────────────────┼────────────┤
│ 550... │ alice │ alice@example.com   │ ADMIN      │
│ 551... │ bob   │ bob@example.com     │ USER       │
└────────┴───────┴─────────────────────┴────────────┘
```

### `nexusdl users add`

```bash
nexusdl users add --username bob --role USER
```

Prompts for password.

### `nexusdl users remove`

```bash
nexusdl users remove --username bob
```

### `nexusdl users reset-password`

```bash
nexusdl users reset-password --username bob
```

Prompts for new password.

---

## `nexusdl benchmark`

Run performance benchmarks.

```bash
nexusdl benchmark
nexusdl benchmark --site mangadex
nexusdl benchmark --output results.json
```

---

## TUI

Launch the **Textual TUI** (terminal UI).

```bash
nexusdl-tui
```

Or:

```bash
nexusdl tui
```

The TUI has 6 screens (Search, Downloads, Library, Logs, Settings, Help).
See the [TUI guide](#tui-guide) below.

### TUI vs CLI

| Aspect | CLI | TUI |
|---|---|---|
| **Interaction** | One-shot commands | Full app |
| **Best for** | Scripting, cron | Interactive browsing |
| **Output** | Text / JSON | Rich UI |
| **Mouse** | No | Yes |

---

## TUI guide

### Navigation

| Key | Action |
|---|---|
| `s` | Search |
| `d` | Downloads |
| `l` | Library |
| `g` | Logs |
| `c` | Settings |
| `?` | Help |
| `q` | Quit |
| `Esc` | Back / cancel |

### Search screen

1. Press `s`.
2. Type a query.
3. Press `Enter`.
4. Use `↑` / `↓` to navigate results.
5. Press `Enter` on a result to open **Manga Detail**.
6. Select chapters with `Space`.
7. Press `d` to download.

### Downloads screen

1. Press `d`.
2. Use `↑` / `↓` to select a task.
3. Press `p` to pause, `r` to resume, `c` to cancel.

### Library screen

1. Press `l`.
2. Use `↑` / `↓` to select a manga.
3. Press `Enter` to view chapters.

---

## Scripting examples

### Update library daily (cron)

```bash
# ~/.config/nexusdl/update.sh
#!/usr/bin/env bash
set -euo pipefail

nexusdl update --yes --concurrency 1 --log-level WARNING

if [[ $? -ne 0 ]]; then
    curl -X POST "$DISCORD_WEBHOOK" \
        -d '{"content":"NexusDL update failed"}'
fi
```

```cron
0 3 * * * ~/.config/nexusdl/update.sh >> ~/.local/state/nexusdl/update.log 2>&1
```

### Download a manga from a text file (batch)

```bash
#!/usr/bin/env bash
set -euo pipefail

while IFS= read -r url; do
    nexusdl download "$url" --format CBZ --no-progress
done < urls.txt
```

### Search and download top result

```bash
#!/usr/bin/env bash
set -euo pipefail

URL=$(nexusdl search "one piece" --json | jq -r '.[0].url')
nexusdl download "$URL" --chapters 1-5 --format CBZ
```

### Pipe JSON to jq for processing

```bash
nexusdl sites --json \
    | jq '.[] | select(.language == "fr" and .capabilities.supports_search)' \
    | jq -r '.id'
```

### Check for new chapters (Nagios-style)

```bash
#!/usr/bin/env bash
set -euo pipefail

OUTPUT=$(nexusdl update --dry-run --json 2>&1)

if echo "$OUTPUT" | jq -e 'length > 0' > /dev/null; then
    echo "NEW: $(echo "$OUTPUT" | jq length) new chapters available"
    exit 1
fi

echo "OK: no new chapters"
exit 0
```

---

## Shell completion

### Bash

```bash
nexusdl --install-completion bash
source ~/.bashrc
```

Then:

```bash
nexusdl <TAB>
# → search  info  download  sites  library  config  ...
```

### Zsh

```bash
nexusdl --install-completion zsh
source ~/.zshrc
```

### Fish

```bash
nexusdl --install-completion fish
```

### PowerShell

```powershell
nexusdl --show-completion powershell | Out-String | Invoke-Expression
```

---

## Troubleshooting

### `nexusdl: command not found`

The binary is not in `PATH`.

**Fix**:

```bash
# uv
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# pip --user
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

### `NEXUSDL_SECRET_KEY is missing`

**Fix**:

```bash
nexusdl config init
# Edit the .env file
```

### `no matching distribution found for nexusdl`

**Cause**: PyPI package name is `nexusdl` (no hyphen).

**Fix**:

```bash
pip install nexusdl[cli]   # ✅
pip install nexus-dl       # ❌
```

### `ModuleNotFoundError: No module named 'nexusdl'`

Wrong Python interpreter.

**Fix**:

```bash
which nexusdl
# → should point to ~/.local/bin/nexusdl or .venv/bin/nexusdl
```

### Rate limited (`429`)

**Fix**:

```bash
nexusdl search "..." --sites mangadex   # use fewer sites
# Or reduce concurrency
nexusdl download "..." --concurrency 1
```

### `Permission denied` on download directory

**Fix**:

```bash
chmod u+w ~/Downloads/NexusDL
# Or change directory
nexusdl download "..." --dest ~/Manga
```

### `KeyboardInterrupt` doesn't stop

Press `Ctrl+C` **three times** within 2 seconds — forces immediate exit.

### `--json` output is not valid JSON

Ensure you're using `--json` (not `--format json` on `search`). Both work
on `search`, only `--json` works on `download`.

**Fix**:

```bash
nexusdl download "..." --json | jq .   # ✅
nexusdl download "..." --format json   # ❌ (not supported on download)
```

### Exit code 4 (rate limited)

Wait, then retry with lower concurrency.

```bash
sleep 60
nexusdl download "..." --concurrency 1 --page-concurrency 4
```

### Logs are too verbose

```bash
nexusdl --log-level WARNING search "..."
```

---

## See also

- [TUI guide](#tui-guide) — interactive terminal UI
- [GUI usage](gui.md) — desktop GUI
- [Web usage](web.md) — browser-based UI
- [Configuration](configuration.md) — all env vars
- [Formats](formats.md) — packaging formats
- [Python API](../api/python.md) — programmatic use
- [REST API](../api/rest.md) — HTTP endpoints
- [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) — ask for help

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
