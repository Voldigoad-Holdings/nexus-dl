# Plugin Development

> **Stability**: 🟡 **Alpha** (pre-`1.0.0`). The plugin API may change until
> `1.0.0`. Subscribe to [releases](https://github.com/Voldigoad-Holdings/nexus-dl/releases)
> to be notified of breaking changes.

NexusDL supports **plugins** that extend its behavior without modifying the
core. Plugins can:

- Hook into the download lifecycle (`before_download`, `after_download`)
- React to page downloads (`on_page_downloaded`)
- Notify external systems (Discord, Telegram, webhooks)
- Post-process downloaded files (rename, compress, upload)
- Add **custom parsers** (via `register()` — see [ADR-003](../development/decisions/003-parser-factory-di.md))
- Validate data before it enters the library

This guide walks you through **writing, testing, and distributing** a plugin.

```{note}
Plugins run **in the same Python process** as NexusDL. They have **full
access** to the filesystem, network, and environment. Only install plugins
you trust.
```

---

## Table of contents

1. [Quick start](#quick-start)
2. [Plugin anatomy](#plugin-anatomy)
3. [The `NexusPlugin` protocol](#the-nexusplugin-protocol)
4. [Lifecycle hooks](#lifecycle-hooks)
5. [The manifest](#the-manifest)
6. [Accessing NexusDL internals](#accessing-nexusdl-internals)
7. [Testing a plugin](#testing-a-plugin)
8. [Distributing a plugin](#distributing-a-plugin)
9. [Security considerations](#security-considerations)
10. [Examples](#examples)
11. [API reference](#api-reference)

---

## Quick start

A minimal plugin has **three files**:

```
my_plugin/
├── nexus.plugin.yaml   ← manifest
├── plugin.py           ← implementation
└── __init__.py         ← exports the plugin class
```

### 1. Create the manifest

```yaml
# my_plugin/nexus.plugin.yaml
name: my_plugin
version: 0.1.0
author: "Your Name"
email: "you@example.com"
description: "A minimal NexusDL plugin."
min_nexusdl_version: "0.1.0"
max_nexusdl_version: "1.0.0"     # optional, exclusive
license: GPL-3.0-or-later
homepage: "https://github.com/you/nexusdl-my-plugin"
hooks:
  - on_load
  - on_unload
  - on_before_download
  - on_after_download
```

### 2. Implement the plugin

```python
# my_plugin/plugin.py
from __future__ import annotations

from typing import TYPE_CHECKING

from nexusdl.plugins.api import NexusPlugin

if TYPE_CHECKING:
    from nexusdl.core.models.download import DownloadTask, DownloadResult
    from nexusdl.core.models.page import Page
    from nexusdl.plugins.api import PluginContext


class MyPlugin(NexusPlugin):
    """A minimal NexusDL plugin."""

    name = "my_plugin"
    version = "0.1.0"

    def on_load(self, context: PluginContext) -> None:
        """Called when the plugin is loaded."""
        self._logger = context.logger
        self._logger.info(f"{self.name} v{self.version} loaded")

    def on_unload(self) -> None:
        """Called when the plugin is unloaded."""
        if hasattr(self, "_logger"):
            self._logger.info(f"{self.name} unloaded")

    async def on_before_download(self, task: DownloadTask) -> DownloadTask:
        """Called before a download task starts."""
        return task

    async def on_after_download(self, result: DownloadResult) -> DownloadResult:
        """Called after a download task completes."""
        return result


__all__ = ["MyPlugin"]
```

### 3. Export the class

```python
# my_plugin/__init__.py
from my_plugin.plugin import MyPlugin

__all__ = ["MyPlugin"]
```

### 4. Install and enable

```bash
# Copy the plugin to the plugins directory
cp -r my_plugin/ ~/.config/nexusdl/plugins/

# Or install as a pip package
pip install nexusdl-my-plugin
```

Then in `.env`:

```dotenv
NEXUSDL_PLUGINS_ENABLED=true
NEXUSDL_PLUGINS=my_plugin
```

That's it — NexusDL will load the plugin on startup.

---

## Plugin anatomy

A plugin is a **Python package** with:

| File | Required | Purpose |
|---|---|---|
| `nexus.plugin.yaml` | ✅ | Manifest (name, version, hooks, dependencies) |
| `__init__.py` | ✅ | Exports the plugin class |
| `plugin.py` | ✅ (convention) | Main implementation |
| `tests/` | Recommended | Unit tests for the plugin |
| `README.md` | Recommended | Documentation |
| `LICENSE` | Required for distribution | License (must be GPL-3.0-compatible) |
| `pyproject.toml` | Optional | For `pip install` distribution |

### Directory layout (recommended)

```
nexusdl-my-plugin/
├── my_plugin/
│   ├── __init__.py
│   ├── plugin.py
│   ├── nexus.plugin.yaml
│   └── tests/
│       └── test_plugin.py
├── pyproject.toml
├── README.md
└── LICENSE
```

---

## The `NexusPlugin` protocol

`NexusPlugin` is a **Protocol** (PEP 544) defined in
`nexusdl/plugins/api.py`. A class does **not** need to inherit from it
structurally — any class implementing the required attributes and methods
is accepted.

```python
# nexusdl/plugins/api.py (excerpt)
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexusdl.core.models.download import DownloadTask, DownloadResult
    from nexusdl.core.models.page import Page
    from nexusdl.plugins.api import PluginContext


@runtime_checkable
class NexusPlugin(Protocol):
    """Public interface for NexusDL plugins.

    Implementations must define the required attributes and methods.
    Optional methods can be omitted — the plugin loader checks with
    :func:`hasattr` before calling them.
    """

    # ── Required attributes ──
    name: str
    version: str

    # ── Required methods ──
    def on_load(self, context: PluginContext) -> None:
        """Called once when the plugin is loaded."""
        ...

    def on_unload(self) -> None:
        """Called once when the plugin is unloaded (graceful shutdown)."""
        ...

    # ── Optional methods (implement as needed) ──
    async def on_before_download(self, task: DownloadTask) -> DownloadTask:
        """Called before a download starts. Return the (possibly modified) task."""
        ...

    async def on_after_download(self, result: DownloadResult) -> DownloadResult:
        """Called after a download completes. Return the (possibly modified) result."""
        ...

    async def on_page_downloaded(self, page: Page, path: Path) -> None:
        """Called after each page is downloaded and written to disk."""
        ...

    async def on_error(
        self,
        task_id: str,
        error: Exception,
    ) -> None:
        """Called when a download fails."""
        ...
```

### Why a Protocol (and not an ABC)?

- **Lighter**: no need to inherit, no metaclass.
- **Duck-typed**: any object with the right shape works.
- **Easier to test**: a `MagicMock` with the right attributes works.
- **`runtime_checkable`**: `isinstance(obj, NexusPlugin)` works at runtime.

See **PEP 544** for details.

---

## Lifecycle hooks

Plugins are notified at specific points in the download lifecycle. All
hooks are **async** except `on_load` and `on_unload`.

### Hook order

```
Application startup
  │
  ├── Plugin loaded
  │     └── on_load(context)                        ← sync
  │
  ├── Download task created
  │     └── on_before_download(task)  → task'       ← async
  │
  ├── For each page:
  │     ├── Page downloaded
  │     └── on_page_downloaded(page, path)          ← async
  │
  ├── Chapter packaged
  │
  ├── Download task completed
  │     └── on_after_download(result) → result'     ← async
  │
  ├── On error (any step)
  │     └── on_error(task_id, error)                ← async
  │
  └── Application shutdown
        └── on_unload()                             ← sync
```

### `on_load(context)` — sync

Called **once** when the plugin is loaded, before any download starts.

**Purpose**: initialize resources, read config, connect to external services.

```python
def on_load(self, context: PluginContext) -> None:
    self._logger = context.logger
    self._settings = context.settings
    self._http = context.http_session  # optional, may be None
    self._logger.info(f"{self.name} loaded")
```

`PluginContext` provides:

| Attribute | Type | Purpose |
|---|---|---|
| `logger` | `loguru.Logger` | Pre-configured logger with plugin name |
| `settings` | `Settings` | Read-only application settings |
| `http_session` | `HttpSession \| None` | Shared HTTP session (may be None) |
| `event_bus` | `EventBus` | Publish/subscribe to NexusDL events |
| `plugin_dir` | `Path` | The plugin's own directory |
| `data_dir` | `Path` | Per-plugin data directory (for persistent state) |

### `on_before_download(task)` — async

Called **before** a download task starts. Can **modify** the task.

**Purpose**: validate inputs, adjust destination, change format, add
metadata.

```python
async def on_before_download(self, task: DownloadTask) -> DownloadTask:
    if task.manga.content_rating == "PORNOGRAPHIC":
        if not self._settings.allow_adult:
            raise PluginError("Adult content is disabled")
    return task
```

**Rules**:
- Return the task (possibly modified).
- Raise an exception to **abort** the download.
- Do **not** block for more than a few seconds (use `asyncio.sleep` if needed).
- Do **not** access the network unless absolutely necessary.

### `on_after_download(result)` — async

Called **after** a download task completes successfully. Can **modify**
the result (e.g. rename file, update metadata).

**Purpose**: post-processing, notifications, uploads.

```python
async def on_after_download(self, result: DownloadResult) -> DownloadResult:
    await self._notify_discord(f"Downloaded: {result.output_path.name}")
    return result
```

**Rules**:
- Return the result (possibly modified).
- Do **not** delete the output file.
- Do **not** raise for non-critical errors (log them instead).

### `on_page_downloaded(page, path)` — async

Called **after each page** is downloaded and written to disk. The file is
already on disk at `path`.

**Purpose**: per-page processing (optimization, watermarking, validation,
OCR).

```python
async def on_page_downloaded(self, page: Page, path: Path) -> None:
    if path.stat().st_size < 1024:
        raise PluginError(f"Page {page.index} is suspiciously small")
```

**Rules**:
- The file **must not be deleted**.
- If the file must be modified, do so **atomically** (temp file + rename).
- Do **not** rename or move the file (the packager expects it in place).

### `on_error(task_id, error)` — async

Called **when a download fails**. The task is already marked `FAILED`.

**Purpose**: notifications, metrics, diagnostics.

```python
async def on_error(self, task_id: str, error: Exception) -> None:
    await self._notify_discord(f"Task {task_id} failed: {error}")
```

**Rules**:
- Do **not** try to resume the download (use a separate API for that).
- Do **not** raise (the error is already being logged).

### `on_unload()` — sync

Called **once** when NexusDL shuts down (graceful) or the plugin is
disabled. Use it to close connections, flush buffers, etc.

```python
def on_unload(self) -> None:
    if hasattr(self, "_http"):
        # don't close the shared session, NexusDL handles it
        pass
    self._logger.info("Plugin unloaded")
```

**Rules**:
- Must **not** raise (wrap in try/except if unsure).
- Must complete in **< 1 second**.
- Do **not** assume network access (may be shutting down).

---

## The manifest

`nexus.plugin.yaml` declares the plugin's metadata and requirements.

### Schema

```yaml
name: str                      # required, snake_case, unique
version: str                   # required, SemVer
author: str                    # required
email: str                     # optional
description: str               # required, short
homepage: str                  # optional, URL
license: str                   # required, SPDX identifier
min_nexusdl_version: str       # required, SemVer (>=)
max_nexusdl_version: str       # optional, SemVer (<), exclusive
hooks: list[str]               # required, at least one
dependencies: list[str]        # optional, pip package specifiers
python_requires: str           # optional, PEP 440 specifier
```

### Example — full manifest

```yaml
name: discord_notifier
version: 1.2.0
author: "Jane Doe"
email: "jane@example.com"
description: "Sends a Discord notification on every completed download."
homepage: "https://github.com/janedoe/nexusdl-discord-notifier"
license: GPL-3.0-or-later
min_nexusdl_version: "0.1.0"
max_nexusdl_version: "2.0.0"
hooks:
  - on_load
  - on_unload
  - on_after_download
  - on_error
dependencies:
  - "aiohttp>=3.9"
python_requires: ">=3.12"
```

### Validation

The manifest is validated at load time against
`nexusdl/plugins/validators.py`. If invalid, the plugin is **not loaded**
and a clear error is logged.

Common validation errors:

| Error | Cause |
|---|---|
| `Unknown hook: foo` | `hooks` contains an unsupported hook name |
| `Missing required field: license` | `license` is empty |
| `Invalid version: 1.0` | `version` is not SemVer |
| `Incompatible with NexusDL 0.2.0` | `min_nexusdl_version: 0.3.0` |
| `Dependency not installed: aiohttp` | `dependencies` lists a missing package |

---

## Accessing NexusDL internals

Plugins can access NexusDL internals **through `PluginContext`**, not by
importing `core` directly. This keeps plugins forward-compatible.

### What plugins CAN access

- `context.settings` — application settings (read-only)
- `context.logger` — pre-configured logger
- `context.http_session` — shared HTTP session (optional)
- `context.event_bus` — publish/subscribe to events
- `context.plugin_dir` — the plugin's own directory
- `context.data_dir` — a per-plugin data directory

### What plugins MUST NOT do

- **Import `nexusdl.interfaces`** — breaks the interface abstraction
- **Import concrete parsers** — use `ParserFactory` if needed (rare)
- **Modify `settings`** — it's immutable
- **Close `http_session`** — NexusDL manages its lifecycle
- **Access `_private` attributes** of core objects — no stability guarantee
- **Run blocking I/O** in async hooks — use `asyncio.to_thread`

### Accessing the library

If the plugin needs to query the library (e.g. to compute stats):

```python
async def on_after_download(self, result: DownloadResult) -> DownloadResult:
    db = self._context.library_db  # provided via PluginContext
    progress = await db.get_reading_progress(result.chapter.manga_id)
    self._logger.info(f"Reading progress: {progress}")
    return result
```

`library_db` is exposed in `PluginContext` as an optional attribute.

---

## Testing a plugin

Plugins are **Python packages** and can be tested like any Python code.

### Recommended structure

```
my_plugin/
├── plugin.py
├── nexus.plugin.yaml
└── tests/
    ├── conftest.py
    └── test_plugin.py
```

### Example test

```python
# my_plugin/tests/test_plugin.py
from __future__ import annotations

from pathlib import Path

import pytest

from nexusdl.core.models.download import DownloadResult, DownloadTask
from nexusdl.plugins.api import PluginContext

from my_plugin.plugin import MyPlugin


@pytest.fixture
def plugin() -> MyPlugin:
    return MyPlugin()


@pytest.fixture
def context(tmp_path: Path) -> PluginContext:
    return PluginContext(
        logger=__import__("loguru").logger,
        settings=...,          # use a fixture from nexusdl test helpers
        http_session=None,
        event_bus=...,
        plugin_dir=tmp_path,
        data_dir=tmp_path / "data",
    )


def test_on_load(plugin: MyPlugin, context: PluginContext) -> None:
    plugin.on_load(context)
    assert plugin._logger is not None


def test_on_before_download_returns_task(plugin: MyPlugin, context: PluginContext) -> None:
    plugin.on_load(context)
    task = ...  # create a sample DownloadTask
    import asyncio
    result = asyncio.run(plugin.on_before_download(task))
    assert result == task


@pytest.mark.asyncio
async def test_on_after_download_logs(plugin: MyPlugin, context: PluginContext) -> None:
    plugin.on_load(context)
    result = ...  # create a sample DownloadResult
    out = await plugin.on_after_download(result)
    assert out == result
```

### Using NexusDL's test helpers

NexusDL exposes fixtures in `tests/conftest.py`. Plugins can import them
via `pytest_plugins`:

```python
# my_plugin/tests/conftest.py
pytest_plugins = [
    "nexusdl.tests.fixtures",   # exposes `sample_settings`, `sample_task`, ...
]
```

### Mocking the context

For unit tests, `PluginContext` can be a simple `MagicMock`:

```python
from unittest.mock import AsyncMock, MagicMock

context = MagicMock(spec=PluginContext)
context.logger = MagicMock()
context.settings = MagicMock()
context.http_session = None
```

---

## Distributing a plugin

### Option A — pip package (recommended)

Publish to PyPI as a normal package:

```toml
# pyproject.toml
[project]
name = "nexusdl-my-plugin"
version = "0.1.0"
dependencies = ["nexusdl>=0.1.0"]

[project.entry-points."nexusdl.plugins"]
my_plugin = "my_plugin:MyPlugin"
```

```bash
# Install
pip install nexusdl-my-plugin

# Enable in .env
NEXUSDL_PLUGINS=my_plugin
```

NexusDL discovers it via `importlib.metadata.entry_points(group="nexusdl.plugins")`.

### Option B — local folder

Copy the plugin folder to `~/.config/nexusdl/plugins/my_plugin/`:

```bash
cp -r my_plugin/ ~/.config/nexusdl/plugins/
```

NexusDL discovers it by scanning the plugins directory.

### Option C — Git repository

Point to a Git repo in `~/.config/nexusdl/plugins/my_plugin.yaml`:

```yaml
# ~/.config/nexusdl/plugins/my_plugin.yaml
source: git
url: https://github.com/you/nexusdl-my-plugin.git
ref: v0.1.0
```

NexusDL clones and installs it on startup (requires `git`).

### Distribution checklist

Before publishing:

- [ ] License is **GPL-3.0-compatible** (required)
- [ ] `README.md` explains what the plugin does
- [ ] `nexus.plugin.yaml` is valid
- [ ] `version` is SemVer
- [ ] `min_nexusdl_version` matches the actual minimum
- [ ] Tests pass (`pytest`)
- [ ] No secrets in the code or manifest
- [ ] `dependencies` are minimal and pinned

---

## Security considerations

Plugins run **in the same Python process** as NexusDL. They have:

- **Full filesystem access** (read/write/delete any file the user can)
- **Full network access** (make any HTTP request)
- **Full environment access** (`os.environ`, including secrets)
- **Ability to `import` anything** (subprocess, ctypes, etc.)

```{warning}
**NexusDL does NOT sandbox plugins.** A malicious plugin can steal your
credentials, delete your files, or exfiltrate your data. Only install
plugins you trust, from sources you trust.
```

### Recommendations for plugin authors

1. **Be transparent** — put your real name and email in the manifest.
2. **Sign your releases** — use GPG-signed Git tags and PyPI attestations.
3. **Minimize dependencies** — every dependency is an attack surface.
4. **Don't require secrets** — ask the user to configure them via `.env`.
5. **Don't phone home** — no telemetry unless the user explicitly opts in.
6. **Validate inputs** — never trust `task`, `result`, or `page` blindly.
7. **Handle errors gracefully** — log and return, don't crash NexusDL.
8. **Respect the user's data** — don't upload files without consent.

### Recommendations for users

1. **Check the source** — read `plugin.py` before installing.
2. **Check the license** — must be GPL-3.0-compatible.
3. **Check the author** — real name, verifiable profile.
4. **Run in a sandbox** — Docker container, VM, or dedicated user.
5. **Monitor network** — the plugin should not make unexpected requests.
6. **Report suspicious plugins** — see [SECURITY.md](../../../SECURITY.md).

### Future: sandboxing

We are exploring **subprocess-based plugins** with a limited IPC API, so
that plugins cannot access the main process directly. This is a future
enhancement — tracked in
[issue #XXX](https://github.com/Voldigoad-Holdings/nexus-dl/issues).

---

## Examples

### Example 1 — Discord notifier

Notifies a Discord webhook on download completion.

```python
# nexusdl_discord_notifier/plugin.py
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import aiohttp

from nexusdl.plugins.api import NexusPlugin

if TYPE_CHECKING:
    from nexusdl.core.models.download import DownloadResult
    from nexusdl.plugins.api import PluginContext


class DiscordNotifier(NexusPlugin):
    name = "discord_notifier"
    version = "0.1.0"

    def on_load(self, context: PluginContext) -> None:
        self._logger = context.logger
        self._webhook = os.environ.get("NEXUSDL_DISCORD_WEBHOOK")
        if not self._webhook:
            raise ValueError("NEXUSDL_DISCORD_WEBHOOK is not set")

    def on_unload(self) -> None:
        pass

    async def on_after_download(self, result: DownloadResult) -> DownloadResult:
        async with aiohttp.ClientSession() as session:
            await session.post(
                self._webhook,
                json={"content": f"✅ Downloaded: `{result.output_path.name}`"},
            )
        return result
```

Manifest:

```yaml
name: discord_notifier
version: 0.1.0
author: "Jane Doe"
description: "Sends a Discord notification on every completed download."
license: GPL-3.0-or-later
min_nexusdl_version: "0.1.0"
hooks:
  - on_load
  - on_unload
  - on_after_download
dependencies:
  - "aiohttp>=3.9"
```

### Example 2 — Image optimizer

Recompresses each page with Pillow to save space.

```python
# nexusdl_image_optimizer/plugin.py
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

from nexusdl.plugins.api import NexusPlugin

if TYPE_CHECKING:
    from nexusdl.core.models.page import Page
    from nexusdl.plugins.api import PluginContext


class ImageOptimizer(NexusPlugin):
    name = "image_optimizer"
    version = "0.1.0"
    quality = 85

    def on_load(self, context: PluginContext) -> None:
        self._logger = context.logger

    def on_unload(self) -> None:
        pass

    async def on_page_downloaded(self, page: Page, path: Path) -> None:
        # Recompress atomically
        tmp = path.with_suffix(".tmp")
        with Image.open(path) as img:
            img.save(tmp, quality=self.quality, optimize=True)
        tmp.replace(path)
        self._logger.debug(f"Optimized {path.name}")
```

### Example 3 — Custom parser via plugin

Register a new site without forking NexusDL.

```python
# nexusdl_mysite/plugin.py
from __future__ import annotations

from typing import TYPE_CHECKING

from nexusdl.parsers.factory import register
from nexusdl.parsers.base import BaseParser
from nexusdl.plugins.api import NexusPlugin

if TYPE_CHECKING:
    from nexusdl.plugins.api import PluginContext


class MySiteParser(BaseParser):
    site_id = "mysite"
    language = "en"
    adult = False

    async def search(self, query, *, page=1):
        ...

    async def get_manga(self, url_or_id):
        ...

    async def get_chapters(self, manga):
        ...

    async def get_pages(self, chapter):
        ...


class MySitePlugin(NexusPlugin):
    name = "mysite"
    version = "0.1.0"

    def on_load(self, context: PluginContext) -> None:
        register("mysite", MySiteParser)
        context.logger.info("Registered MySiteParser")

    def on_unload(self) -> None:
        # Note: unregistering is not supported (registry is global).
        # The parser will remain available until NexusDL restarts.
        pass
```

See [ADR-003](decisions/003-parser-factory-di.md) for the registration
pattern.

---

## API reference

### `nexusdl.plugins.api`

```{eval-rst}
.. automodule:: nexusdl.plugins.api
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### `nexusdl.plugins.hooks`

```{eval-rst}
.. automodule:: nexusdl.plugins.hooks
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### `nexusdl.plugins.loader`

```{eval-rst}
.. automodule:: nexusdl.plugins.loader
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### `nexusdl.plugins.validators`

```{eval-rst}
.. automodule:: nexusdl.plugins.validators
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

---

## See also

- [Architecture](architecture.md) — hexagonal design
- [ADR-003: Parser Factory DI](decisions/003-parser-factory-di.md) — how to register a parser
- [Adding a parser](adding_parsers.md) — for built-in parsers
- [`src/nexusdl/plugins/_example/`](../../../src/nexusdl/plugins/_example/) — reference implementation
- [Security policy](../../../SECURITY.md) — reporting a malicious plugin
- [NexusDL Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) — ask questions

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
