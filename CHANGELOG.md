# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- Initial project structure (Phase 0):
  - `pyproject.toml` with extras `cli`, `gui`, `web`, `playwright`, `redis`, `dev`, `all`
  - `.env.example` with all `NEXUSDL_*` environment variables
  - `.editorconfig`, `.gitattributes`, `.gitignore`, `.dockerignore`
  - `.pre-commit-config.yaml` with ruff, mypy, and custom hooks
  - `.python-version` pinned to 3.12
  - `nexus.dl` project manifest
  - 5 Architecture Decision Records (`docs/source/development/decisions/`)
  - `scripts/check_imports.py` (hexagonal architecture enforcement)
  - `scripts/validate_sites.py` (JSON Schema validation for `sites.yaml`)

### Changed

- N/A

### Deprecated

- N/A

### Removed

- N/A

### Fixed

- N/A

### Security

- N/A

---

## [0.1.0] - TBD

Initial public alpha release. Not yet published.

### Added

- **Core**
  - `core/models/` — Pydantic v2 domain models (Manga, Chapter, Page, Site, User, etc.)
  - `core/enums.py` — Language, MangaStatus, ContentRating, PackagingFormat, Priority, DownloadStatus, UserRole, SiteHealth
  - `core/config.py` — Pydantic Settings v2, `SecretStr` everywhere, no hardcoded secrets
  - `core/events.py` — Typed `EventBus` (Observer pattern)
  - `core/exceptions.py` — Custom exception hierarchy (`NexusDLError` base)
  - `core/paths.py` — Cross-platform paths via `platformdirs`
  - `core/logger.py` — Loguru-based structured logging (json / text / pretty)
  - `core/compliance.py` — Optional `robots.txt` enforcement

- **Session**
  - `core/session/http_session.py` — Async `httpx` session with cache, retries, circuit breaker
  - `core/session/cache.py` — `hishel` HTTP cache
  - `core/session/circuit_breaker.py` — Per-site circuit breaker
  - `core/session/cookie_manager.py` — Encrypted cookie persistence
  - `core/session/playwright_pool.py` — Pooled Chromium contexts for JS sites
  - `core/session/flaresolverr.py` — FlareSolverr fallback
  - `core/session/rate_limiter.py` — Token-bucket rate limiter
  - `core/session/proxy_manager.py` — Proxy rotation
  - `core/session/user_agents.py` — Rotating user agents

- **Registry**
  - `core/registry/protocols.py` — `ParserFactory` Protocol (dependency inversion)
  - `core/registry/site_registry.py` — Central site registry with injected factory
  - `core/registry/loader.py` — YAML loader with schema validation
  - `core/registry/validator.py` — `sites.yaml` validator

- **Downloader**
  - `core/downloader/manager.py` — `DownloadManager` orchestrator
  - `core/downloader/worker.py` — Per-task worker
  - `core/downloader/queue.py` — Priority queue
  - `core/downloader/progress.py` — Progress tracking
  - `core/downloader/retry.py` — Retry strategies
  - `core/downloader/deduplication.py` — SHA-256 checksum deduplication

- **Packaging**
  - `core/packaging/base.py` — `BasePackager` ABC
  - `core/packaging/factory.py` — `PackagerFactory`
  - `core/packaging/zip_packager.py` — ZIP (no metadata)
  - `core/packaging/cbz_packager.py` — CBZ with `ComicInfo.xml`
  - `core/packaging/cb7_packager.py` — CB7 (7z) with `ComicInfo.xml`
  - `core/packaging/pdf_packager.py` — PDF via `img2pdf`
  - `core/packaging/folder_packager.py` — Raw folder
  - `core/packaging/comic_info.py` — Full ComicRack `ComicInfo.xml` model

- **Image**
  - `core/image/downloader.py` — Async image download with checksum
  - `core/image/converter.py` — WebP/AVIF → JPEG
  - `core/image/optimizer.py` — Lossless recompression
  - `core/image/validator.py` — Reject HTML-error-masquerading-as-JPEG

- **Library**
  - `core/library/database.py` — Async SQLite via `sqlalchemy[asyncio]`
  - `core/library/metadata.py` — Metadata normalization
  - `core/library/scanner.py` — Local library scanner
  - `core/library/search.py` — FTS5 full-text search
  - `core/library/migrations/` — Alembic migrations

- **Parsers**
  - `parsers/base.py` — `BaseParser` ABC
  - `parsers/factory.py` — `ParserFactory` implementation
  - `parsers/mixins/` — WordPress Madara, MangaThemesia, FoolSlide, API-based, Cloudflare, JS-rendered
  - `parsers/fr/` — 21 French sites
  - `parsers/en/` — 21 English sites
  - `parsers/kr/` — 3 Korean sites
  - `parsers/adult/` — 5 adult sites (nhentai, hentaizone, pururin, scan-hentai, x-manga)
  - `parsers/custom/` — Template for community parsers

- **Interfaces**
  - `interfaces/cli/` — Typer commands + Textual TUI
  - `interfaces/gui/` — CustomTkinter desktop app
  - `interfaces/web/backend/` — FastAPI REST + WebSocket
  - `interfaces/web/frontend/` — Next.js 14 App Router + TailwindCSS + shadcn/ui

- **Plugins**
  - `plugins/api.py` — `NexusPlugin` Protocol + `PluginContext`
  - `plugins/hooks.py` — Lifecycle hooks
  - `plugins/loader.py` — Dynamic plugin loader
  - `plugins/validators.py` — Plugin manifest validator
  - `plugins/_example/` — Reference plugin

- **Infrastructure**
  - Multi-platform CI matrix (Linux, macOS, Windows)
  - Docker images: base, GUI (X11), web (full stack)
  - Nginx reverse proxy config
  - Multi-stage Dockerfiles
  - PyInstaller / Nuitka build scripts

### Formats supported

- `FOLDER` — raw folder
- `ZIP` — standard zip
- `CBZ` — Comic Book ZIP with `ComicInfo.xml`
- `CB7` — Comic Book 7z with `ComicInfo.xml`
- `PDF` — one page per image
- `CBT` — Comic Book TAR (optional)

> **CBR is intentionally not supported** — see [ADR-002](docs/source/development/decisions/002-no-cbr.md) for legal and licensing reasons (RAR is proprietary and incompatible with GPL-3.0).

### Security

- All secrets handled via `pydantic.SecretStr`
- Secrets loaded from `.env` only, never hardcoded
- `scripts/check_imports.py` fails on any hardcoded GitHub token, AWS key, or private key block
- `no-token-leak` pre-commit hook blocks accidental token commits
- Cookies encrypted at rest with `cryptography.Fernet`
- JWT authentication with `HS256` (configurable to `RS256` in production)
- Rate limiting middleware on API
- CORS restricted to configured origins
- Robots.txt enforced by default (`NEXUSDL_RESPECT_ROBOTS_TXT=true`)
- Content filtering opt-in (`NEXUSDL_ALLOW_ADULT=false` by default)

### Known limitations

- CBR not supported (see ADR-002)
- Watermarking not implemented (ethical/legal concerns)
- Some sites may require manual cookie refresh (Cloudflare Turnstile)
- Playwright must be installed separately (`playwright install chromium`)

---

## Version history

| Version | Date | Status | Notes |
|---------|------|--------|-------|
| 0.1.0 | TBD | In development | Initial public alpha |
| — | — | — | No previous releases |

---

## Upcoming (planned)

### 0.2.0

- [ ] Plugin marketplace (official registry)
- [ ] Web UI: reading mode (OPDS-compatible)
- [ ] Mobile apps (Kotlin Multiplatform + Swift)
- [ ] Additional parsers (30+ new sites)
- [ ] Torrent / DDL fallback for dead sites

### 0.3.0

- [ ] Distributed downloads (worker pool via Redis)
- [ ] Auto-update of `sites.yaml` via remote manifest
- [ ] Annotation / bookmark system
- [ ] Statistics dashboard

### 1.0.0

- [ ] API stability guarantee
- [ ] Semantic versioning commitment
- [ ] Full documentation coverage
- [ ] 95%+ test coverage
- [ ] Fuzzing (hypothesis) on all parsers

---

## How to update this file

This changelog follows the [Conventional Commits](https://www.conventionalcommits.org/)
specification. Commit types map to changelog sections:

| Commit prefix | Changelog section |
|---------------|-------------------|
| `feat:` | **Added** |
| `fix:` | **Fixed** |
| `perf:` | **Changed** |
| `refactor:` | **Changed** |
| `docs:` | **Changed** (documentation only) |
| `style:` | *(ignored)* |
| `test:` | *(ignored)* |
| `build:` | **Changed** (build system) |
| `ci:` | **Changed** (CI) |
| `chore:` | *(ignored)* |
| `revert:` | **Removed** |

Breaking changes must include `BREAKING CHANGE:` in the commit body and will
be listed in a dedicated section of the changelog with migration notes.

For automated changelog generation (future), the project will use
[`commitizen`](https://commitizen-tools.github.io/commitizen/) or
[`git-cliff`](https://git-cliff.org/).

---

[Unreleased]: https://github.com/Voldigoad-Holdings/nexus-dl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Voldigoad-Holdings/nexus-dl/releases/tag/v0.1.0
