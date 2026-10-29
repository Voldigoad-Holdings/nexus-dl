# Architecture

> **Reference**: This page is the **living architecture reference**. The
> decisions are recorded in the [ADRs](decisions/index.md); this page
> explains the **current** state of the code.
>
> - [ADR-001: Hexagonal Architecture](decisions/001-hexagonal-architecture.md)
> - [ADR-002: No CBR Support](decisions/002-no-cbr.md)
> - [ADR-003: Parser Factory DI](decisions/003-parser-factory-di.md)
> - [ADR-004: sites.yaml vs .env](decisions/004-sites-yaml-vs-env.md)
> - [ADR-005: Multi-platform Builds](decisions/005-multi-platform-builds.md)

NexusDL is a **multi-site, multi-interface, multi-format** manga downloader
built on **hexagonal architecture** (ports and adapters). This page
describes:

- The **layers** and their responsibilities
- The **data flow** from a URL to a packaged file
- The **key components** and how they interact
- The **patterns** used throughout the codebase
- The **rules** that keep the architecture clean

```{note}
If you are new to hexagonal architecture, read
[ADR-001](decisions/001-hexagonal-architecture.md) first. This page assumes
you understand the basic concepts (ports, adapters, dependency inversion).
```

---

## Table of contents

1. [High-level diagram](#high-level-diagram)
2. [Layers](#layers)
3. [Data flow](#data-flow)
4. [Key components](#key-components)
5. [Patterns](#patterns)
6. [Rules and enforcement](#rules-and-enforcement)
7. [Extension points](#extension-points)
8. [Testing strategy](#testing-strategy)
9. [Performance considerations](#performance-considerations)
10. [See also](#see-also)

---

## High-level diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     INTERFACES (adapters primaires)             │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
│  │   CLI    │  │   TUI    │  │   GUI    │  │   Web    │         │
│  │ (Typer)  │  │(Textual) │  │  (CTk)   │  │(FastAPI) │         │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘         │
│       │             │             │             │               │
│       └─────────────┴──────┬──────┴─────────────┘               │
│                            │                                    │
│                            ▼                                    │
│                    ┌──────────────┐                             │
│                    │ Composition  │  ← dependency injection     │
│                    │    Root      │                             │
│                    └───────┬──────┘                             │
└────────────────────────────┼────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                       CORE (domain)                             │
│                                                                 │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐                 │
│  │  Models    │  │ Downloader │  │ Packaging  │                 │
│  │ (Pydantic) │  │  (orches.) │  │  (formats) │                 │
│  └────────────┘  └────────────┘  └────────────┘                 │
│                                                                 │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐                 │
│  │ Registry   │  │  Session   │  │  Library   │                 │
│  │  (sites)   │  │ (HTTP/CF)  │  │ (SQLite)   │                 │
│  └────────────┘  └────────────┘  └────────────┘                 │
│                                                                 │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐                 │
│  │  Events    │  │   Config   │  │ Compliance │                 │
│  │ (pub/sub)  │  │ (settings) │  │ (robots)   │                 │
│  └────────────┘  └────────────┘  └────────────┘                 │
└────────────────────────────┬────────────────────────────────────┘
                             │  (via Protocols)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                  ADAPTERS SECONDAIRES                           │
│                                                                 │
│  ┌────────────────────────────────────────────────┐             │
│  │         Parsers (per-site scrapers)            │             │
│  │  fr/ · en/ · kr/ · adult/ · custom/ · mixins/  │             │
│  └────────────────────────────────────────────────┘             │
│                                                                 │
│  ┌──────────────────┐    ┌──────────────────┐                   │
│  │  Playwright      │    │  FlareSolverr    │                   │
│  │  (browser pool)  │    │  (CF bypass)     │                   │
│  └──────────────────┘    └──────────────────┘                   │
│                                                                 │
│  ┌────────────────────────────────────────────────┐             │
│  │              Plugins (community)               │             │
│  └────────────────────────────────────────────────┘             │
└─────────────────────────────────────────────────────────────────┘

                         DATA (files)
┌─────────────────────────────────────────────────────────────────┐
│  data/sites.yaml · data/sites_schema.json · translations/ · SQL │
└─────────────────────────────────────────────────────────────────┘
```

**Reading the diagram:**

- **Arrows point inward** to `core/` — `interfaces/` and `parsers/` depend
  on `core/`, never the reverse.
- **Composition root** is the only place where concrete implementations
  are instantiated.
- **Protocols** (not shown) live in `core/registry/protocols.py` — they are
  the "ports" of the hexagon.
- **Data files** are read by `core/` at startup; they contain no code.

---

## Layers

### `core/` — Domain (the center)

The **business logic**. No I/O, no framework, no concrete adapters.

| Module | Responsibility |
|---|---|
| `core/models/` | Pydantic models (immutable, validated) |
| `core/enums.py` (in `models/`) | Enumerations (`Language`, `MangaStatus`, etc.) |
| `core/config.py` | `Settings` (Pydantic Settings), `load_settings()` |
| `core/exceptions.py` | Exception hierarchy rooted at `NexusDLError` |
| `core/events.py` | `EventBus` (typed publish/subscribe) |
| `core/paths.py` | Cross-platform paths (`platformdirs`) |
| `core/logger.py` | Loguru configuration |
| `core/compliance.py` | `robots.txt` enforcement |
| `core/registry/` | `SiteRegistry`, `ParserFactory` Protocol |
| `core/session/` | `HttpSession`, `CookieManager`, `PlaywrightPool`, `FlareSolverr`, `RateLimiter`, `ProxyManager`, `CircuitBreaker`, `HttpCache` |
| `core/downloader/` | `DownloadManager`, `Worker`, `Queue`, `Progress`, `Deduplication` |
| `core/packaging/` | `BasePackager`, `PackagerFactory`, one packager per format |
| `core/image/` | `Downloader`, `Converter`, `Optimizer`, `Validator` |
| `core/library/` | `LibraryDatabase`, `Scanner`, `Search` (FTS5) |

**Rules:**

- ✅ Depends on Python stdlib and third-party libs
- ✅ May import from `core/` submodules
- ❌ **NEVER** imports from `parsers/`, `interfaces/`, or `plugins/`
- ❌ No `if __name__ == "__main__"` (this is a library)

### `parsers/` — Secondary adapters (scrapers)

Site-specific scraping logic. One module per site.

| Module | Responsibility |
|---|---|
| `parsers/base.py` | `BaseParser` ABC (4 abstract methods) |
| `parsers/capabilities.py` | `SiteCapabilities` (metadata) |
| `parsers/exceptions.py` | `ParserError` hierarchy |
| `parsers/factory.py` | `DefaultParserFactory`, `register()` |
| `parsers/mixins/` | Reusable behaviors (Madara, MangaThemesia, FoolSlide, API, Cloudflare, JS) |
| `parsers/fr/`, `en/`, `kr/`, `adult/` | Concrete parsers, one per site |
| `parsers/custom/` | Template for community parsers |

**Rules:**

- ✅ Imports from `core/` (models, session, exceptions)
- ✅ Self-registers via `register()` at import time
- ❌ **NEVER** imports from `interfaces/` or `plugins/`
- ❌ **NEVER** contains URLs (they live in `data/sites.yaml`)

### `interfaces/` — Primary adapters (entry points)

User-facing surfaces. Four interfaces, one per usage mode.

| Module | Responsibility |
|---|---|
| `interfaces/cli/` | Typer commands + Textual TUI |
| `interfaces/gui/` | CustomTkinter desktop app |
| `interfaces/web/backend/` | FastAPI REST + WebSocket |
| `interfaces/web/frontend/` | Next.js (separate, excluded from Python wheel) |

**Rules:**

- ✅ May import from everything
- ✅ Contains the **composition root** of each interface
- ❌ **NEVER** contains business logic (that belongs in `core/`)
- ❌ **NEVER** is imported by `core/` or `parsers/`

### `plugins/` — Secondary adapters (community extensions)

Third-party extensions, discovered at startup.

| Module | Responsibility |
|---|---|
| `plugins/api.py` | `NexusPlugin` Protocol, `PluginContext` |
| `plugins/hooks.py` | Lifecycle hook dispatch |
| `plugins/loader.py` | Discovery (entry points, folders, Git repos) |
| `plugins/validators.py` | Manifest validation |
| `plugins/_example/` | Reference plugin |

**Rules:**

- ✅ Imports from `core/`
- ❌ **NEVER** imports from `interfaces/` or `parsers/`
- ❌ Runs unsandboxed (see [Plugins](plugins.md#security-considerations))

### `data/` — Static data

Pure data files. No Python code except `__init__.py` (empty marker).

| File | Purpose |
|---|---|
| `sites.yaml` | Site list (metadata, versioned) |
| `sites_schema.json` | JSON Schema for `sites.yaml` |
| `default_config.yaml` | Runtime defaults |
| `comic_info_template.xml` | ComicInfo.xml template |
| `user_agents.txt` | User-agent rotation list |
| `translations/*.json` | Interface i18n |

**Rules:**

- ✅ Read by `core/` at startup
- ❌ No imports
- ❌ No Python logic (except `__init__.py`)

---

## Data flow

Here is what happens when a user runs:

```bash
nexusdl download "https://mangadex.org/title/1234" --format cbz
```

### Step-by-step

```
1. CLI parses the URL and format
   │
   ▼
2. Composition root (cli_entry.py) instantiates:
   - Settings (from .env)
   - HttpSession (httpx + hishel + retry + rate limit)
   - DefaultParserFactory (parsers/factory.py)
   - SiteRegistry (with injected factory)
   - DownloadManager (with registry + settings)
   │
   ▼
3. DownloadManager.download_manga(manga, ...)
   │
   ├──► 3a. SiteRegistry.get_site("mangadex")
   │         → loads data/sites.yaml, returns SiteConfig
   │
   ├──► 3b. SiteRegistry.get_parser("mangadex", session)
   │         → ParserFactory.create(site, session)
   │         → MangaDexParser(site, session)
   │
   ├──► 3c. parser.get_manga("1234")
   │         → HTTP GET https://mangadex.org/api/manga/1234
   │         → returns Manga(id, title, author, ..., chapters=[])
   │
   ├──► 3d. parser.get_chapters(manga)
   │         → HTTP GET https://mangadex.org/api/manga/1234/chapters
   │         → returns list[Chapter]
   │
   ├──► 3e. For each chapter:
   │     │
   │     ├──► parser.get_pages(chapter)
   │     │       → HTTP GET https://mangadex.org/api/chapter/xyz/pages
   │     │       → returns list[Page]
   │     │
   │     ├──► image.downloader.download(pages, temp_dir)
   │     │       → for each page: HTTP GET with retry, validate JPEG
   │     │       → returns list[Path] on disk
   │     │
   │     ├──► packager.package(pages, output, metadata=ComicInfo(...))
   │     │       → writes CBZ (ZIP + ComicInfo.xml)
   │     │       → returns Path to .cbz
   │     │
   │     └──► EventBus.publish(Event("chapter.completed", ...))
   │             → CLI/GUI/Web subscribers receive the event
   │
   └──► EventBus.publish(Event("download.completed", ...))
        → CLI prints "✓ Done", GUI updates progress, Web pushes via WS
   │
   ▼
4. DownloadResult(output_path, bytes_downloaded, duration_seconds)
   returned to the caller
```

### Key observations

- **The parser does no downloading** — it only returns URLs. The
  `image/downloader.py` downloads the pages.
- **The parser does no packaging** — `packaging/` handles that.
- **The parser does no orchestration** — `downloader/manager.py` handles that.
- **The interface does no logic** — it wires components and displays results.
- **Events flow through `EventBus`** — every progress, completion, or error
  is published as a typed event.

---

## Key components

### `SiteRegistry` — the site catalog

Loaded once at startup. Reads `data/sites.yaml`, validates against
`sites_schema.json`, exposes `get_site()` and `list_sites()`. Injects the
`ParserFactory` (see [ADR-003](decisions/003-parser-factory-di.md)).

```python
class SiteRegistry:
    def __init__(self, settings: Settings, parser_factory: ParserFactory) -> None: ...
    def get_site(self, site_id: str) -> SiteConfig: ...
    def get_parser(self, site_id: str, session: HttpSession) -> BaseParser: ...
    def list_sites(self, *, language: str | None = None, include_adult: bool = False) -> list[SiteConfig]: ...
    async def search_all(self, query: str, ...) -> AsyncIterator[SearchResult]: ...
```

### `HttpSession` — the HTTP layer

Wraps `httpx.AsyncClient` with:

- **`hishel` cache** — HTTP cache (ETag, Last-Modified)
- **`CircuitBreaker`** — per-site failure isolation
- **`RateLimiter`** — token bucket per site
- **`CookieManager`** — encrypted cookie persistence
- **`ProxyManager`** — proxy rotation
- **`tenacity` retry** — exponential backoff with jitter

```python
class HttpSession:
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, ...) -> None: ...
    async def get(self, url: str, **kwargs) -> httpx.Response: ...
    async def stream_download(self, url: str, dest: Path, ...) -> Path: ...
    async def get_html(self, url: str, **kwargs) -> str: ...
    async def get_json(self, url: str, **kwargs) -> dict: ...
```

### `DownloadManager` — the orchestrator

Coordinates the whole flow:

```python
class DownloadManager:
    def __init__(self, registry: SiteRegistry, settings: Settings) -> None: ...
    def submit(self, task: DownloadTask) -> UUID: ...
    def cancel(self, task_id: UUID) -> None: ...
    def pause(self, task_id: UUID) -> None: ...
    def resume(self, task_id: UUID) -> None: ...
    def download_manga(self, manga: Manga, *, chapters: list[Chapter] | None, dest: Path, fmt: PackagingFormat, on_progress: ProgressCallback | None) -> AsyncIterator[DownloadResult]: ...
```

Internally:

- A **priority queue** of tasks
- A **`Worker` per task** (async)
- A **semaphore** limiting global concurrency
- Per-chapter concurrency for page downloads

### `PackagerFactory` — format strategy

One `BasePackager` per format:

```python
class BasePackager(ABC):
    format: ClassVar[PackagingFormat]

    @abstractmethod
    async def package(self, pages: list[Path], output: Path, *, metadata: ComicInfo | None = None) -> Path: ...

class PackagerFactory:
    def create(self, fmt: PackagingFormat) -> BasePackager: ...
```

Concrete packagers: `FolderPackager`, `ZipPackager`, `CbzPackager`,
`Cb7Packager`, `PdfPackager`, `CbtPackager`.

> **CBR is intentionally not implemented** — see
> [ADR-002](decisions/002-no-cbr.md).

### `LibraryDatabase` — local library

Async SQLite via `sqlalchemy[asyncio]` + `aiosqlite`. FTS5 full-text search
on titles, authors, descriptions.

```python
class LibraryDatabase:
    async def init(self) -> None: ...
    async def close(self) -> None: ...
    async def add_manga(self, manga: Manga) -> int: ...
    async def search(self, query: str, *, limit: int = 50) -> list[Manga]: ...
    async def set_reading_progress(self, manga_id: str, chapter_id: str, page: int) -> None: ...
```

### `EventBus` — typed pub/sub

Decouples the core from the interfaces:

```python
class EventBus:
    def subscribe(self, event_type: str, handler: Callable[[Event], Awaitable[None]]) -> None: ...
    async def publish(self, event: Event) -> None: ...
```

Standard event types: `download.started`, `download.progress`,
`download.completed`, `download.failed`, `chapter.completed`,
`library.updated`, `site.health.changed`.

### `Settings` — configuration

Pydantic Settings, `SecretStr` for secrets, `extra="forbid"`.

See [ADR-004](decisions/004-sites-yaml-vs-env.md) for the `.env` vs
`sites.yaml` split.

---

## Patterns

NexusDL uses the following patterns consistently:

| Pattern | Where | Why |
|---|---|---|
| **Hexagonal architecture** | Overall | See [ADR-001](decisions/001-hexagonal-architecture.md) |
| **Protocol (duck typing)** | `ParserFactory`, `NexusPlugin` | Lighter than ABC; PEP 544 |
| **Factory** | `ParserFactory`, `PackagerFactory` | Decouple creation from use |
| **Strategy** | `BasePackager` per format | Add a format = add a class |
| **Repository** | `LibraryDatabase` | Abstract SQLite behind a repository |
| **Observer** | `EventBus` | Decouple producers from consumers |
| **Circuit breaker** | `CircuitBreaker` per site | Isolate failing sites |
| **Rate limiter** | Token bucket per site | Respect upstream limits |
| **Retry with backoff** | `tenacity` + jitter | Handle transient failures |
| **Dependency injection** | Composition root | No global state |
| **Async-first** | Everywhere | Non-blocking I/O |

### Anti-patterns (avoided)

- ❌ **God object** — `DownloadManager` is large but delegates to
  `Worker`, `Queue`, `Progress`, `Deduplication`.
- ❌ **Singleton** — no global `_instance`. Composition root passes
  dependencies explicitly.
- ❌ **Service locator** — no `Container.get("Session")`. Injection at
  construction time.
- ❌ **Anemic model** — Pydantic models have validation, methods, and
  invariants (e.g. `DownloadTask.transition_to(status)` raises on invalid
  transitions).
- ❌ **Magic** — no `@inject` decorators, no auto-wiring, no hidden
  configuration.

---

## Rules and enforcement

### The 6 dependency rules (from ADR-001)

| Rule | Statement | Checked by |
|---|---|---|
| **R1** | `core/` never imports `parsers/`, `interfaces/`, `plugins/` | `scripts/check_imports.py` |
| **R2** | `parsers/` never imports `interfaces/`, `plugins/` | `scripts/check_imports.py` |
| **R3** | `plugins/` never imports `interfaces/`, `parsers/` | `scripts/check_imports.py` |
| **R4** | `interfaces/` may import anything | — |
| **R5** | `data/` imports nothing | `scripts/check_imports.py` |
| **R6** | Cross-layer uses Protocols in `core/` | Code review |

### CI checks

Every PR runs:

```bash
# 1. Architecture rules
python scripts/check_imports.py

# 2. sites.yaml validation
python scripts/validate_sites.py

# 3. Lint
ruff check src tests

# 4. Format
ruff format --check src tests

# 5. Types
mypy src

# 6. Tests (3 OS × 2 Python)
pytest tests/unit
```

All must pass before merge.

### Manual review checklist

- [ ] No new dependency from `core/` to `parsers/` or `interfaces/`
- [ ] New components use Protocols, not concrete classes
- [ ] Async I/O everywhere (no blocking calls in hot paths)
- [ ] Resources closed (`async with`, `try/finally`)
- [ ] No secrets in code, no URLs in `sites.yaml`
- [ ] No `print()` in `src/` (use `loguru`)
- [ ] New public APIs have docstrings

---

## Extension points

NexusDL can be extended **without modifying the core** in five ways:

### 1. Add a new site (parser)

Create `src/nexusdl/parsers/<lang>/<site>.py` with a `BaseParser` subclass,
self-register with `register()`, add an entry to `data/sites.yaml`.

See [Adding a parser](adding_parsers.md).

### 2. Add a new format

Create `src/nexusdl/packaging/<format>_packager.py` with a `BasePackager`
subclass, register it in `PackagerFactory`.

### 3. Add a new interface

Create `src/nexusdl/interfaces/<name>/` with a composition root. Reuse
`core/` for everything.

### 4. Write a plugin

See [Plugins](plugins.md). Two hooks are enough for most use cases.

### 5. Add a new session backend

Create a class in `core/session/`, implement the protocol used by
`HttpSession`, inject it at the composition root. Rare.

---

## Testing strategy

The architecture is designed for **testability at every layer**.

| Layer | Test type | Tools | Example |
|---|---|---|---|
| **`core/`** | Unit | `pytest`, `FakeParserFactory` | `test_site_registry.py` |
| **`parsers/`** | Unit + smoke | `respx`, `pytest-asyncio` | `test_mangadex_smoke.py` |
| **`interfaces/`** | Unit + integration | `httpx.AsyncClient`, `pytest` | `test_web_routers.py` |
| **`plugins/`** | Unit | `pytest`, `MagicMock` | `test_plugin.py` |
| **Cross-cutting** | Integration | `pytest`, real filesystem | `test_full_download_flow.py` |

### Key principle

**`core/` tests do not touch the network.** They use fakes for every
adapter:

```python
class FakeParserFactory:
    def create(self, site, session):
        return FakeParser(site, session)

def test_registry_with_fake_parser():
    registry = SiteRegistry(settings, parser_factory=FakeParserFactory())
    ...
```

**`parsers/` tests use `respx`** to mock HTTP responses:

```python
@respx.mock
async def test_search():
    respx.get("https://mangadex.org/api/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    ...
```

**`interfaces/` tests use `httpx.AsyncClient`** to test the FastAPI app
in-process:

```python
async with httpx.AsyncClient(app=app, base_url="http://test") as client:
    response = await client.get("/api/v1/health")
```

**No test hits the real network** except `@pytest.mark.smoke` tests, which
run on a schedule, not on PRs.

---

## Performance considerations

### Concurrency

- **Global semaphore** (`settings.max_concurrent_downloads`, default 3)
  limits parallel tasks.
- **Per-chapter semaphore** (`settings.max_concurrent_pages_per_chapter`,
  default 8) limits parallel page downloads.
- **Per-site rate limiter** (`settings.default_rate_limit_per_second`,
  default 2.0) respects upstream limits.

### Caching

- **HTTP cache** (`hishel`) stores responses on disk. A second search on
  the same query is nearly instant.
- **Library deduplication** uses SHA-256 checksums to avoid re-downloading
  identical pages.

### Streaming

- **Images stream to disk**, not to memory. A 100-page chapter uses
  <10 MB RAM.
- **Progress updates are throttled** to 10 Hz per task to avoid event
  storms.

### Lazy loading

- **Parsers are imported on demand** by the `SiteRegistry`. Only sites
  actually used are instantiated.
- **Playwright browsers are launched lazily** — only when a
  Cloudflare-protected site is accessed.
- **Frontend code is split** — Next.js code-splits by route.

### Benchmarks

`scripts/benchmark.py` measures:

- Search latency per site (p50, p95, p99)
- Download throughput (pages/second)
- Packaging time per format
- Memory usage per task

Run with:

```bash
just benchmark
```

---

## See also

- [ADRs](decisions/index.md) — the decisions behind this architecture
- [Adding a parser](adding_parsers.md) — how to scrape a new site
- [Plugins](plugins.md) — how to extend NexusDL
- [Contributing](contributing.md) — how to contribute
- [Python API](../api/python.md) — programmatic use
- [REST API](../api/rest.md) — HTTP endpoints
- [WebSocket API](../api/websocket.md) — real-time events
- [`scripts/check_imports.py`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/scripts/check_imports.py) — the enforcement script

---

*This page is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../LICENSE).*
