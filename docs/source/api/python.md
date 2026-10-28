## 📄 `docs/source/api/python.md` — complet

# Python API

> **Stability**: 🟡 **Alpha** (pre-`1.0.0`). Public APIs may change until `1.0.0`.
> Subscribe to [releases](https://github.com/Voldigoad-Holdings/nexus-dl/releases)
> to be notified of breaking changes.
>
> **Python**: 3.12+

NexusDL exposes a **public Python API** in addition to its CLI, TUI, GUI, and
Web interfaces. This page documents the modules that are **stable enough** to
be used programmatically.

If you are building an integration, a plugin, or an alternative frontend, this
is the place to start.

```{note}
The Python API is **not** a thin wrapper around the CLI. It is the
fundamental layer — the CLI, TUI, GUI, and Web API all consume it.
```

---

## Table of contents

1. [Quick start](#quick-start)
2. [Public modules](#public-modules)
3. [Configuration](#configuration)
4. [Domain models](#domain-models)
5. [Session & HTTP](#session--http)
6. [Site registry](#site-registry)
7. [Parsers](#parsers)
8. [Downloader](#downloader)
9. [Packaging](#packaging)
10. [Library](#library)
11. [Events](#events)
12. [Plugins](#plugins)
13. [Exceptions](#exceptions)
14. [Stability guarantees](#stability-guarantees)

---

## Quick start

### Minimal example — search and download

```python
import asyncio
from pathlib import Path

from nexusdl.core.config import load_settings
from nexusdl.core.downloader.manager import DownloadManager
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.factory import ParserFactory


async def main() -> None:
    # 1. Load configuration (.env + defaults + env vars)
    settings = load_settings()

    # 2. Build the site registry with an injected parser factory
    registry = SiteRegistry(settings, parser_factory=ParserFactory())

    # 3. Search across all enabled sites
    results = [r async for r in registry.search_all("one piece")]
    print(f"{len(results)} results")

    # 4. Download the first result
    manager = DownloadManager(registry, settings)
    async for result in manager.download_manga(
        results[0].manga,
        dest=Path("~/Manga").expanduser(),
        fmt="CBZ",
    ):
        print(f"✓ {result.output_path}")


if __name__ == "__main__":
    asyncio.run(main())
```

### Minimal example — single-site parser

```python
import asyncio

from nexusdl.core.config import load_settings
from nexusdl.parsers.en.mangadex import MangaDexParser
from nexusdl.core.session.http_session import HttpSession


async def main() -> None:
    settings = load_settings()
    site = settings.sites.get("mangadex")

    async with HttpSession(site, settings) as session:
        parser = MangaDexParser(site, session)
        results = await parser.search("one piece")
        for r in results[:5]:
            print(r.title, r.url)


asyncio.run(main())
```

---

## Public modules

The following modules are **public** and documented here. Everything else is
**internal** (`_`-prefixed or not listed) and may change without notice.

| Module | Stability | Description |
|---|---|---|
| `nexusdl.core.config` | 🟢 Stable | `Settings`, `load_settings()` |
| `nexusdl.core.models` | 🟡 Alpha | Pydantic models: `Manga`, `Chapter`, `Page`, `SiteConfig`, `SearchResult`, `DownloadTask`, `DownloadResult`, `User` |
| `nexusdl.core.enums` | 🟢 Stable | `Language`, `MangaStatus`, `ContentRating`, `PackagingFormat`, `Priority`, `DownloadStatus`, `UserRole`, `SiteHealth` |
| `nexusdl.core.session` | 🟡 Alpha | `HttpSession`, `CookieManager`, `PlaywrightPool`, `FlareSolverr`, `RateLimiter`, `ProxyManager` |
| `nexusdl.core.registry` | 🟡 Alpha | `SiteRegistry`, `ParserFactory` (Protocol) |
| `nexusdl.core.downloader` | 🟡 Alpha | `DownloadManager`, `DownloadWorker`, `DownloadQueue` |
| `nexusdl.core.packaging` | 🟢 Stable | `BasePackager`, `PackagerFactory`, `ComicInfo` |
| `nexusdl.core.library` | 🟡 Alpha | `LibraryDatabase`, `LibraryScanner`, `LibrarySearch` |
| `nexusdl.core.events` | 🟢 Stable | `EventBus`, `Event` |
| `nexusdl.parsers` | 🟡 Alpha | `BaseParser`, all concrete parsers, `ParserFactory` (implementation) |
| `nexusdl.plugins` | 🟡 Alpha | `NexusPlugin` (Protocol), `PluginContext`, `PluginLoader` |

Legend:
- 🟢 **Stable** — public API, breaking changes only on major version bumps
- 🟡 **Alpha** — public API, may change before `1.0.0`
- 🔴 **Internal** — not documented, may change without notice

---

## Configuration

### `nexusdl.core.config`

```{eval-rst}
.. automodule:: nexusdl.core.config
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### `load_settings()`

Loads and validates the runtime configuration from:

1. Default values (hardcoded in `Settings`)
2. `config/config.yaml` (if present)
3. Environment variables prefixed with `NEXUSDL_`
4. Explicit keyword arguments (if provided)

```python
from nexusdl.core.config import load_settings

settings = load_settings()

# Access
print(settings.app_name)                # "NexusDL"
print(settings.log_level)               # "INFO"
print(settings.max_concurrent_downloads)  # 3

# Secrets are `SecretStr` — never serialized as plain strings
print(settings.jwt_secret.get_secret_value())  # only if you really need it
```

### `Settings` (Pydantic v2 model)

All fields, their types, defaults, and descriptions are documented in the
autodoc above. Key fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `app_name` | `str` | `"NexusDL"` | Application name |
| `app_environment` | `Literal["dev","staging","prod"]` | `"dev"` | Deployment environment |
| `public_url` | `HttpUrl` | — | Public URL of the frontend |
| `api_base_url` | `HttpUrl` | — | Public URL of the API |
| `frontend_url` | `HttpUrl` | — | URL of the Next.js frontend |
| `secret_key` | `SecretStr` | — | Global secret (session, signing) |
| `jwt_secret` | `SecretStr` | — | JWT signing secret |
| `cookie_encryption_key` | `SecretStr` | — | Cookie encryption key (Fernet) |
| `database_url` | `str` | `"sqlite+aiosqlite:///./data/library.db"` | Database URL |
| `http_timeout_seconds` | `float` | `30.0` | HTTP timeout |
| `http_max_retries` | `int` | `3` | Max retries |
| `respect_robots_txt` | `bool` | `True` | Enforce robots.txt |
| `default_rate_limit_per_second` | `float` | `2.0` | Default rate limit |
| `max_concurrent_downloads` | `int` | `3` | Max parallel downloads |
| `allow_adult` | `bool` | `False` | Enable adult sites |
| `playwright_enabled` | `bool` | `False` | Enable Playwright |
| `flaresolverr_url` | `HttpUrl \| None` | `None` | FlareSolverr URL |
| `download_dir` | `Path` | platformdirs | Download directory |
| `log_level` | `Literal["TRACE","DEBUG","INFO","WARNING","ERROR","CRITICAL"]` | `"INFO"` | Log level |
| `log_format` | `Literal["json","text","pretty"]` | `"pretty"` | Log format |

See `.env.example` for the exhaustive list.

---

## Domain models

### `nexusdl.core.models`

All models are **immutable Pydantic v2** models.

```{eval-rst}
.. automodule:: nexusdl.core.models.manga
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.models.chapter
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.models.page
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.models.site
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.models.search
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.models.download
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.models.library
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.models.user
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Enums

```{eval-rst}
.. automodule:: nexusdl.core.models.enums
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### IDs

| Entity | Type | Format |
|---|---|---|
| `Manga.id` | `str` | `sha256(f"{site}:{source_id}")[:16]` |
| `Chapter.id` | `str` | `sha256(f"{manga_id}:{source_id}")[:16]` |
| `Page.index` | `int` | 0-based |
| `DownloadTask.id` | `UUID` | UUID4 (volatile) |
| `User.id` | `UUID` | UUID4 (persistent) |

### Example

```python
from datetime import datetime, timezone
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.enums import Language, MangaStatus, ContentRating

manga = Manga(
    id="a1b2c3d4e5f6a7b8",
    source_id="1234-uuid",
    site="mangadex",
    title="One Piece",
    author="Oda Eiichiro",
    status=MangaStatus.ONGOING,
    language=Language.EN,
    content_rating=ContentRating.SAFE,
    url="https://mangadex.org/title/...",
    updated_at=datetime.now(timezone.utc),
)
```

---

## Session & HTTP

### `nexusdl.core.session`

```{eval-rst}
.. automodule:: nexusdl.core.session.http_session
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.session.cookie_manager
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.session.playwright_pool
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.session.flaresolverr
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.session.rate_limiter
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.session.proxy_manager
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Example — HTTP session with cookie persistence

```python
import asyncio
from pathlib import Path
from nexusdl.core.config import load_settings
from nexusdl.core.session.cookie_manager import CookieManager
from nexusdl.core.session.http_session import HttpSession


async def main() -> None:
    settings = load_settings()
    cookies = CookieManager(
        storage_path=Path("~/.config/nexusdl/cookies.enc").expanduser(),
        key=settings.cookie_encryption_key.get_secret_value().encode(),
    )

    site = settings.sites.get("nhentai")
    async with HttpSession(site, settings, cookie_manager=cookies) as session:
        html = await session.get_html("https://nhentai.net/g/123456/")
        print(html[:200])


asyncio.run(main())
```

---

## Site registry

### `nexusdl.core.registry`

```{eval-rst}
.. automodule:: nexusdl.core.registry.site_registry
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.registry.protocols
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Example — list sites

```python
from nexusdl.core.config import load_settings
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.factory import ParserFactory

settings = load_settings()
registry = SiteRegistry(settings, parser_factory=ParserFactory())

# All sites
for site in registry.list_sites():
    print(site.id, site.name, site.language.value)

# Filter by language
for site in registry.list_sites(language="fr"):
    print(site.id)

# Include adult
for site in registry.list_sites(include_adult=True):
    print(site.id)
```

### Example — multi-site search

```python
import asyncio
from nexusdl.core.config import load_settings
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.factory import ParserFactory


async def main() -> None:
    settings = load_settings()
    registry = SiteRegistry(settings, parser_factory=ParserFactory())

    async for result in registry.search_all(
        "one piece",
        languages=["en", "fr"],
        include_adult=False,
        max_concurrent=8,
    ):
        print(f"{result.site_name}: {result.title}")


asyncio.run(main())
```

---

## Parsers

### `nexusdl.parsers.base`

```{eval-rst}
.. automodule:: nexusdl.parsers.base
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.parsers.capabilities
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.parsers.factory
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.parsers.exceptions
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Writing a custom parser

See the [Adding a parser](../development/adding_parsers.md) guide. In short:

```python
from nexusdl.parsers.base import BaseParser
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.chapter import Chapter
from nexusdl.core.models.page import Page
from nexusdl.core.models.search import SearchResult


class MySiteParser(BaseParser):
    site_id = "mysite"
    language = "en"
    adult = False

    async def search(self, query: str, *, page: int = 1) -> list[SearchResult]:
        """Search mangas matching the query."""
        # ...
        return []

    async def get_manga(self, url_or_id: str) -> Manga:
        """Fetch full manga metadata."""
        # ...
        raise NotImplementedError

    async def get_chapters(self, manga: Manga) -> list[Chapter]:
        """Fetch the chapter list."""
        # ...
        return []

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Fetch the page URLs of a chapter."""
        # ...
        return []
```

---

## Downloader

### `nexusdl.core.downloader`

```{eval-rst}
.. automodule:: nexusdl.core.downloader.manager
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.downloader.worker
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.downloader.queue
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.downloader.progress
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Example — download with progress callback

```python
import asyncio
from pathlib import Path
from nexusdl.core.config import load_settings
from nexusdl.core.downloader.manager import DownloadManager
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.factory import ParserFactory


async def main() -> None:
    settings = load_settings()
    registry = SiteRegistry(settings, parser_factory=ParserFactory())
    manager = DownloadManager(registry, settings)

    def on_progress(downloaded: int, total: int) -> None:
        pct = (downloaded / total * 100) if total else 0.0
        print(f"\r{pct:5.1f}%  {downloaded:,}/{total:,} bytes", end="")

    async for result in manager.download_manga(
        manga=some_manga,
        chapters=[ch1, ch2],
        dest=Path("~/Manga").expanduser(),
        fmt="CBZ",
        on_progress=on_progress,
    ):
        print(f"\n✓ {result.output_path} ({result.duration_seconds:.1f}s)")


asyncio.run(main())
```

### State machine — `DownloadTask.status`

```
PENDING ──► RUNNING ──► DONE
   │           │
   │           ├──► PAUSED ──► RUNNING
   │           │
   │           └──► FAILED
   │
   └──► CANCELLED
```

`DONE`, `FAILED`, `CANCELLED` are **terminal**. Any other transition raises
`InvalidStateTransitionError`.

---

## Packaging

### `nexusdl.core.packaging`

```{eval-rst}
.. automodule:: nexusdl.core.packaging.base
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.packaging.factory
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.packaging.comic_info
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Example — package images into CBZ

```python
import asyncio
from pathlib import Path
from nexusdl.core.packaging.cbz_packager import CbzPackager
from nexusdl.core.packaging.comic_info import ComicInfo


async def main() -> None:
    packager = CbzPackager()
    result = await packager.package(
        pages=[Path("001.png"), Path("002.png"), Path("003.png")],
        output=Path("Chapter 001.cbz"),
        metadata=ComicInfo(
            title="Chapter 001",
            series="One Piece",
            number="1",
            language_iso="en",
            page_count=3,
        ),
    )
    print(f"✓ {result}")


asyncio.run(main())
```

---

## Library

### `nexusdl.core.library`

```{eval-rst}
.. automodule:: nexusdl.core.library.database
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.library.scanner
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.core.library.search
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Example — full-text search

```python
import asyncio
from nexusdl.core.config import load_settings
from nexusdl.core.library.database import LibraryDatabase


async def main() -> None:
    settings = load_settings()
    async with LibraryDatabase(settings.database_url) as db:
        results = await db.search("one piece", limit=10)
        for manga in results:
            print(manga.title, manga.author)


asyncio.run(main())
```

---

## Events

### `nexusdl.core.events`

```{eval-rst}
.. automodule:: nexusdl.core.events
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Example — subscribe to download events

```python
import asyncio
from nexusdl.core.events import Event, EventBus


async def main() -> None:
    bus = EventBus()

    async def on_progress(event: Event) -> None:
        payload = event.payload
        print(f"Task {payload['task_id']}: {payload['percent']:.0%}")

    bus.subscribe("download.progress", on_progress)

    # ... publish events from elsewhere
    await bus.publish(Event(
        type="download.progress",
        payload={"task_id": "...", "percent": 0.42},
    ))


asyncio.run(main())
```

### Standard event types

| Type | Payload |
|---|---|
| `download.started` | `{task_id, manga_id, chapters_count}` |
| `download.progress` | `{task_id, chapter_id, pages_done, pages_total, percent}` |
| `download.completed` | `{task_id, output_path, duration_seconds}` |
| `download.failed` | `{task_id, error}` |
| `download.paused` | `{task_id}` |
| `download.resumed` | `{task_id}` |
| `download.cancelled` | `{task_id}` |
| `chapter.started` | `{task_id, chapter_id}` |
| `chapter.completed` | `{task_id, chapter_id, pages_count}` |
| `chapter.failed` | `{task_id, chapter_id, error}` |
| `library.updated` | `{manga_id, operation}` |
| `site.health.changed` | `{site_id, old_status, new_status}` |

---

## Plugins

### `nexusdl.plugins`

```{eval-rst}
.. automodule:: nexusdl.plugins.api
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.plugins.hooks
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:

.. automodule:: nexusdl.plugins.loader
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

### Example — minimal plugin

```python
from nexusdl.plugins.api import NexusPlugin, PluginContext
from nexusdl.core.models.download import DownloadTask, DownloadResult


class MyPlugin(NexusPlugin):
    name = "my_plugin"
    version = "0.1.0"

    def on_load(self, context: PluginContext) -> None:
        self._logger = context.logger

    def on_unload(self) -> None:
        pass

    async def on_before_download(self, task: DownloadTask) -> DownloadTask:
        # e.g. modify dest, format, priority
        return task

    async def on_after_download(self, result: DownloadResult) -> DownloadResult:
        # e.g. post-process, upload, notify
        return result
```

See the [Plugins guide](../development/plugins.md) for details.

---

## Exceptions

### `nexusdl.core.exceptions`

```{eval-rst}
.. automodule:: nexusdl.core.exceptions
   :members:
   :undoc-members:
   :show-inheritance:
   :noindex:
```

All NexusDL exceptions inherit from `NexusDLError`:

```
NexusDLError (base)
├── ConfigError
├── ParserError
│   ├── ParserNotFoundError
│   ├── ParserResponseError
│   └── ParserParseError
├── DownloadError
│   ├── ChapterDownloadError
│   └── PageDownloadError
├── PackagingError
├── LibraryError
├── ComplianceError
├── SessionError
│   ├── SessionTimeoutError
│   ├── SessionRateLimitError
│   └── SessionCloudflareError
├── PluginError
└── InvalidStateTransitionError
```

### Example

```python
from nexusdl.core.exceptions import NexusDLError, DownloadError, ParserError

try:
    async for result in manager.download_manga(...):
        ...
except DownloadError as exc:
    print(f"Download failed: {exc}")
except ParserError as exc:
    print(f"Parser failed: {exc}")
except NexusDLError as exc:
    print(f"Something went wrong: {exc}")
```

---

## Stability guarantees

NexusDL follows **Semantic Versioning 2.0.0**:

| Change | Version bump |
|---|---|
| Breaking change in a 🟢 **Stable** API | **Major** (`1.0.0` → `2.0.0`) |
| Breaking change in a 🟡 **Alpha** API | **Minor** (`0.1.0` → `0.2.0`) |
| New feature (backward-compatible) | **Minor** |
| Bug fix (backward-compatible) | **Patch** |

### What counts as a breaking change?

- Renaming or removing a public class, function, or attribute
- Changing the type of a parameter or return value
- Changing the behavior of a public method in a backward-incompatible way
- Removing a field from a public Pydantic model
- Adding a **required** field to a public Pydantic model
- Changing exception types raised

### What does NOT count as a breaking change?

- Adding a new optional field to a Pydantic model
- Adding a new method to a class
- Adding a new module
- Changing internal (non-`_`-prefixed but undocumented) implementation
- Changing log messages
- Changing `__repr__` output

### Deprecation policy

When we plan to remove a public API:

1. We mark it with `@deprecated` (Python 3.13+) or `typing_extensions.deprecated`.
2. We emit a `DeprecationWarning` at runtime.
3. We document it in `CHANGELOG.md` under **Deprecated**.
4. We keep it for **at least one minor version** before removal.
5. We remove it in the next **major** version.

---

## See also

- [REST API](rest.md) — HTTP endpoints
- [WebSocket API](websocket.md) — real-time events
- [Configuration](../usage/configuration.md) — env vars reference
- [Architecture](../development/architecture.md) — hexagonal design
- [Adding a parser](../development/adding_parsers.md) — implement a new site
- [Plugins](../development/plugins.md) — extend NexusDL

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
