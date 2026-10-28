## 📄 `CONTRIBUTING.md` 

# Contributing to NexusDL

First off, **thank you** for considering contributing to NexusDL! 🎉

NexusDL is an open-source, community-driven project. Whether you're fixing a
typo, adding a parser, improving the UI, or writing documentation, every
contribution matters.

This document is the **single source of truth** for contributing. Please read
it entirely before opening your first pull request.

---

## Table of contents

1. [Code of Conduct](#code-of-conduct)
2. [Ways to contribute](#ways-to-contribute)
3. [Getting started](#getting-started)
4. [Development workflow](#development-workflow)
5. [Coding standards](#coding-standards)
6. [Architecture rules](#architecture-rules)
7. [Adding a new site/parser](#adding-a-new-siteparser)
8. [Adding a plugin](#adding-a-plugin)
9. [Translations](#translations)
10. [Documentation](#documentation)
11. [Tests](#tests)
12. [Commit messages](#commit-messages)
13. [Pull requests](#pull-requests)
14. [Security issues](#security-issues)
15. [Getting help](#getting-help)

---

## Code of Conduct

By participating in this project, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md). Please read it before contributing.

---

## Ways to contribute

You don't need to be a Python expert to contribute. Here are all the ways you
can help:

| Contribution | Skill level | Time |
|---|---|---|
| 🐛 Report a bug | Beginner | 5 min |
| 📝 Improve documentation | Beginner | 15 min |
| 🌍 Translate the UI | Beginner | 1 h |
| 🎨 Design icons/banners | Intermediate | 2 h |
| 🧪 Write tests | Intermediate | 1–4 h |
| 🌐 Add a new site/parser | Intermediate | 2–8 h |
| 🔌 Write a plugin | Advanced | 4–12 h |
| 🏗️ Improve architecture | Advanced | 1 day+ |
| 🔍 Security audit | Expert | Variable |

If you're unsure where to start, look for issues labeled
[`good first issue`](https://github.com/Voldigoad-Holdings/nexus-dl/labels/good%20first%20issue)
or [`help wanted`](https://github.com/Voldigoad-Holdings/nexus-dl/labels/help%20wanted).

---

## Getting started

### Prerequisites

- **Python 3.12+** (mandatory)
- **Git** (2.40+)
- **uv** (recommended) or **pip + venv**
- **Node.js 20+** and **pnpm 9+** (only for frontend work)
- **Docker** (optional, for testing the full stack)
- **Playwright** (optional, for JS-heavy sites)

### Fork & clone

```bash
# 1. Fork the repo on GitHub (click "Fork" top right)

# 2. Clone YOUR fork
git clone https://github.com/<your-username>/nexus-dl.git
cd nexus-dl

# 3. Add upstream remote
git remote add upstream https://github.com/Voldigoad-Holdings/nexus-dl.git
git fetch upstream
```

### Setup the environment

**Option A — With `uv` (recommended, fastest)**

```bash
# Install uv (once)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Python + dependencies
uv python install 3.12
uv venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

# Install in editable mode with dev extras
uv pip install -e ".[dev,cli]"
```

**Option B — With `pip + venv` (classic)**

```bash
# Create venv
python3.12 -m venv .venv
source .venv/bin/activate

# Upgrade pip
python -m pip install --upgrade pip

# Install in editable mode with dev extras
pip install -e ".[dev,cli]"
```

**Option C — With `uv sync` (project-managed)**

```bash
uv sync --extra dev --extra cli
```

### Install pre-commit hooks

```bash
pre-commit install
pre-commit install --hook-type commit-msg
```

### Configure the environment file

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set **at minimum**:

```dotenv
NEXUSDL_SECRET_KEY=<generate-with-python>
NEXUSDL_JWT_SECRET=<generate-with-python>
NEXUSDL_COOKIE_ENCRYPTION_KEY=<generate-with-python>
```

Generate secure random values:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Run the command **three times** to get three distinct keys.

### Frontend setup (only if working on the web UI)

```bash
cd src/nexusdl/interfaces/web/frontend
pnpm install
pnpm dev
```

The frontend runs on `http://localhost:3000` and proxies the API to
`http://localhost:8000`.

### Verify installation

```bash
# Run the test suite (fast subset)
pytest -m "not slow and not integration" -q

# Run linters
ruff check src tests
ruff format --check src tests
mypy src

# Run architecture check
python scripts/check_imports.py

# Run CLI
nexusdl --help
```

If all of the above pass, you're ready to code. 🚀

---

## Development workflow

### 1. Sync with upstream

```bash
git checkout main
git fetch upstream
git rebase upstream/main
```

### 2. Create a feature branch

Branch naming conventions:

| Prefix | Purpose | Example |
|---|---|---|
| `feat/` | New feature | `feat/parser-mangafire` |
| `fix/` | Bug fix | `fix/mangadex-rate-limit` |
| `docs/` | Documentation | `docs/adr-006` |
| `refactor/` | Refactoring | `refactor/session-cache` |
| `perf/` | Performance | `perf/async-batch-download` |
| `test/` | Tests only | `test/packaging-cbz` |
| `chore/` | Tooling, deps | `chore/bump-ruff` |

```bash
git checkout -b feat/parser-mangafire
```

### 3. Code

Follow the [coding standards](#coding-standards) and
[architecture rules](#architecture-rules).

### 4. Test locally

```bash
# Fast unit tests
pytest tests/unit -q

# Full test suite (slower)
pytest -q

# Specific module
pytest tests/unit/core/test_config.py -v

# With coverage
pytest --cov=nexusdl --cov-report=term-missing
```

### 5. Lint and format

```bash
# Fix lint issues
ruff check src tests --fix

# Format
ruff format src tests

# Type check
mypy src
```

### 6. Architecture check

```bash
python scripts/check_imports.py
```

This script enforces the hexagonal architecture rules
(see [Architecture rules](#architecture-rules)).

### 7. Commit

Follow [Conventional Commits](#commit-messages):

```bash
git add .
git commit -m "feat(parsers): add mangafire parser"
```

Pre-commit hooks run automatically. If a hook fails, fix the issue and
re-stage (`git add .`) before committing again.

### 8. Push and open PR

```bash
git push origin feat/parser-mangafire
```

Then open a Pull Request on GitHub. The PR template will guide you through
the required information.

---

## Coding standards

### General

- **Python 3.12+** — use modern syntax (`X | Y`, `match`, `Self`, `StrEnum`, etc.)
- **`from __future__ import annotations`** at the top of every Python file
- **Line length: 100** (enforced by `ruff`)
- **Double quotes** for strings (enforced by `ruff format`)
- **No `print()` in `src/`** — use `loguru`
- **No `TODO`/`FIXME`** in shipped code — open an issue instead

### Typing

- **Strict typing everywhere** — `mypy --strict` must pass
- **No `Any`** unless justified (comment required)
- **No `# type: ignore`** without justification
- Use `Protocol`, `Generic`, `TypeVar`, `TypeAlias` where appropriate

### Docstrings

Google-style docstrings on **all public functions and classes**:

```python
async def download_chapter(
    chapter: Chapter,
    dest: Path,
    *,
    fmt: PackagingFormat = PackagingFormat.CBZ,
) -> DownloadResult:
    """Download a chapter and package it.

    Args:
        chapter: Chapter to download.
        dest: Destination directory.
        fmt: Packaging format (FOLDER, ZIP, CBZ, CB7, PDF, CBT).

    Returns:
        Download result with final path and statistics.

    Raises:
        ChapterDownloadError: If a page fails after all retries.
        PackagingError: If packaging fails.

    Example:
        >>> result = await dl.download_chapter(ch, Path("/manga"))
        >>> print(result.output_path)
        /manga/One Piece/Chapter 001.cbz
    """
```

### Async

- **Everything I/O is async** — no blocking calls in the hot path
- Use `asyncio.TaskGroup` (Python 3.11+) instead of `asyncio.gather`
- Use `Semaphore` to limit concurrency
- **Always** set timeouts
- **Always** close resources (`async with`, `try/finally`)

### Error handling

- **Never** swallow exceptions (`except Exception: pass`)
- Raise **specific** exceptions from the `NexusDLError` hierarchy
- Log errors with **context** (`loguru.bind(task_id=..., site=...)`)
- Use `tenacity` for retries with exponential backoff + jitter

### Logging

- Use `loguru` with `.bind()` context
- Levels: `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- Never log secrets (passwords, tokens, cookies)

### Configuration

- **Never hardcode secrets, URLs, or credentials**
- All secrets via `settings.*` (which reads from `.env`)
- All secrets typed as `SecretStr`
- All site URLs in `data/sites.yaml`

### Naming

| Type | Convention | Example |
|---|---|---|
| Variables | `snake_case` | `page_count` |
| Functions | `snake_case` | `download_page` |
| Classes | `PascalCase` | `DownloadManager` |
| Constants | `SCREAMING_SNAKE_CASE` | `MAX_RETRIES` |
| Private | `_prefix` | `_internal_method` |
| Files (Python) | `snake_case.py` | `http_session.py` |
| Files (docs) | `kebab-case.md` | `getting-started.md` |

### Imports

Order (enforced by `ruff isort`):

```python
# 1. Standard library
import asyncio
from pathlib import Path

# 2. Third-party
import httpx
from pydantic import BaseModel

# 3. First-party (nexusdl)
from nexusdl.core.config import settings
```

---

## Architecture rules

NexusDL follows a **hexagonal architecture**. These rules are enforced by
`scripts/check_imports.py` in pre-commit and CI.

```
interfaces/  ──────►  core/  ◄──────  parsers/
     │                                 │
     └───────── factory ───────────────┘
```

### Rules

1. **`core/` MUST NOT import from `parsers/`, `interfaces/`, or `plugins/`**
2. **`parsers/` MUST NOT import from `interfaces/` or `plugins/`**
3. **`plugins/` MUST NOT import from `interfaces/` or `parsers/`**
4. **`interfaces/`** may import from everything (wires things together)
5. **`core/`** depends only on Python stdlib, third-party libs, and itself
6. **Cross-layer communication** goes through **Protocols** defined in `core/`
   (e.g. `core/registry/protocols.py:ParserFactory`), implemented in `parsers/`

If you need to break a rule, **open an ADR first** in
`docs/source/development/decisions/`.

---

## Adding a new site/parser

NexusDL aims to support many sites. Adding a parser is a **structured,
well-defined task**.

### 1. Check if the site is already supported

```bash
grep -i "site-name" src/nexusdl/data/sites.yaml
```

### 2. Determine the site's category

- **Madara** (WordPress theme) → use `mixins/wordpress_madara.py`
- **MangaThemesia** → use `mixins/mangathemesia.py`
- **FoolSlide** → use `mixins/foolslide.py`
- **REST/GraphQL API** → use `mixins/api_based.py`
- **Cloudflare-protected** → use `mixins/cloudflare.py`
- **JS-rendered** → use `mixins/js_rendered.py`

Check the site's HTML structure and network traffic (DevTools → Network tab)
to identify the category.

### 3. Generate the parser skeleton

```bash
python scripts/generate_parser.py \
    --site-id mangafire \
    --language en \
    --category en \
    --mixin mangathemesia
```

This creates:
- `src/nexusdl/parsers/en/mangafire.py`
- `tests/unit/parsers/test_mangafire_smoke.py`
- Adds an entry to `data/sites.yaml` (as a draft, commented)

### 4. Implement the parser

Implement at minimum:

```python
async def search(self, query: str, *, page: int = 1) -> list[SearchResult]: ...
async def get_manga(self, url_or_id: str) -> Manga: ...
async def get_chapters(self, manga: Manga) -> list[Chapter]: ...
async def get_pages(self, chapter: Chapter) -> list[Page]: ...
```

Use the mixin as a base and only override what's needed.

### 5. Configure `sites.yaml`

Uncomment the draft entry and fill in:

```yaml
sites:
  - id: mangafire
    name: MangaFire
    domains:
      - https://mangafire.to
    parser_class: "nexusdl.parsers.en.mangafire:MangaFireParser"
    language: en
    adult: false
    capabilities:
      supports_search: true
      supports_manga_info: true
      supports_chapters: true
      supports_pages: true
      supports_download: true
      requires_auth: false
      requires_cloudflare_bypass: false
      max_concurrent_downloads: 4
      default_rate_limit_per_second: 2.0
    default_headers:
      User-Agent: "NexusDL/0.1.0 (+https://github.com/Voldigoad-Holdings/nexus-dl)"
    cookies_required: []
```

### 6. Validate the schema

```bash
python scripts/validate_sites.py
```

### 7. Write a smoke test

```python
# tests/unit/parsers/test_mangafire_smoke.py
import pytest
from nexusdl.parsers.en.mangafire import MangaFireParser

@pytest.mark.smoke
@pytest.mark.integration
async def test_search_returns_results(parser: MangaFireParser) -> None:
    results = await parser.search("one piece")
    assert len(results) > 0
    assert results[0].title
```

Smoke tests hit the real network and are only run manually or in a dedicated CI
job. **Do not** add `@pytest.mark.smoke` tests that could break CI.

### 8. Document the parser

Add a short note in the PR description:
- Site category (Madara, API, JS, etc.)
- Whether Playwright/FlareSolverr is required
- Known limitations (rate limits, login, etc.)
- Sample URLs used for testing

### 9. Checklist

- [ ] All four methods implemented
- [ ] `sites.yaml` entry valid (`scripts/validate_sites.py` passes)
- [ ] Smoke test passes locally
- [ ] `ruff check` and `ruff format` clean
- [ ] `mypy --strict` passes
- [ ] No hardcoded URL or selector in the Python file (uses `sites.yaml`)
- [ ] Rate limit respected (uses `self._session.request_rate_limited(...)`)

---

## Adding a plugin

Plugins extend NexusDL without modifying the core. They live in
`src/nexusdl/plugins/` (official) or in user-provided directories.

### 1. Create the plugin directory

```bash
mkdir -p src/nexusdl/plugins/my_plugin
cd src/nexusdl/plugins/my_plugin
```

### 2. Create `nexus.plugin.yaml`

```yaml
name: my_plugin
version: 0.1.0
author: "Your Name"
description: "Short description"
min_nexusdl_version: "0.1.0"
hooks:
  - on_before_download
  - on_after_download
```

### 3. Create `plugin.py`

```python
from __future__ import annotations
from nexusdl.core.models.download import DownloadTask, DownloadResult
from nexusdl.plugins.api import NexusPlugin, PluginContext


class MyPlugin(NexusPlugin):
    name = "my_plugin"
    version = "0.1.0"

    def on_load(self, context: PluginContext) -> None:
        """Called when the plugin is loaded."""
        self._logger = context.logger

    def on_unload(self) -> None:
        """Called when the plugin is unloaded."""

    async def on_before_download(self, task: DownloadTask) -> DownloadTask:
        """Called before a download starts."""
        return task

    async def on_after_download(self, result: DownloadResult) -> DownloadResult:
        """Called after a download completes."""
        return result
```

### 4. Test the plugin

```bash
pytest tests/unit/plugins/ -v
```

See [`src/nexusdl/plugins/_example/`](src/nexusdl/plugins/_example) for a full
working example.

---

## Translations

NexusDL supports multiple languages via JSON files in
`src/nexusdl/data/translations/`.

### Add a new language

```bash
# Copy the template
cp src/nexusdl/data/translations/en.json \
   src/nexusdl/data/translations/xx.json
```

Translate every value. Then update `core/i18n.py` to register the new locale.

### Update existing translations

Compare your locale file against `en.json` (source of truth). Add any missing
keys. Do **not** remove keys that are still used.

### Test translations

```bash
pytest tests/unit/core/test_i18n.py -v
```

---

## Documentation

### Where to write

| Type | Location |
|---|---|
| User docs | `docs/source/usage/` |
| Developer docs | `docs/source/development/` |
| API reference | `docs/source/api/` |
| Installation | `docs/source/installation/` |
| ADRs | `docs/source/development/decisions/` |

### Writing style

- **Clear**, not clever
- **Short** sentences
- **Imperative** for instructions ("Run `pytest`", not "You should run `pytest`")
- **Code blocks** with language tags
- **Links** between related docs

### Build the docs locally

```bash
cd docs
make html
# Open _build/html/index.html
```

---

## Tests

### Structure

```
tests/
├── unit/          # Fast, no network, no disk
├── integration/   # Real filesystem, may use SQLite
├── smoke/         # Real network, run manually only
└── fixtures/      # Shared test data
```

### Naming

- File: `test_<module>.py`
- Function: `test_<behavior>`
- Fixture: `_<name>` (underscore prefix, not exported)

### Coverage

- **Target: 90%+** on `core/`
- **Target: 70%+** overall
- Coverage is measured in CI; PRs that lower coverage significantly are
  blocked

### Run tests

```bash
# Fast unit tests
pytest tests/unit -q

# Full suite
pytest -q

# With coverage
pytest --cov=nexusdl --cov-report=html

# Exclude slow tests
pytest -m "not slow"

# Smoke tests (network)
pytest -m smoke -v
```

### Async tests

Use `pytest-asyncio`:

```python
import pytest

@pytest.mark.asyncio
async def test_something() -> None:
    ...
```

Or set `asyncio_mode = "auto"` (already configured in `pyproject.toml`):

```python
async def test_something() -> None:  # no @pytest.mark.asyncio needed
    ...
```

### Mocking HTTP

Use `respx` to mock `httpx`:

```python
import respx
import httpx

@respx.mock
async def test_search() -> None:
    respx.get("https://example.com/api/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    # ...
```

---

## Commit messages

NexusDL uses **Conventional Commits** (enforced by
`conventional-pre-commit`):

```
<type>(<scope>): <short summary>

<optional body>

<optional footer>
```

### Types

| Type | When |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation |
| `style` | Formatting only |
| `refactor` | Code refactoring |
| `perf` | Performance improvement |
| `test` | Tests only |
| `build` | Build system, deps |
| `ci` | CI/CD |
| `chore` | Tooling, misc |
| `revert` | Revert a previous commit |

### Scopes (suggested)

`core`, `parsers`, `plugins`, `cli`, `gui`, `web`, `deps`, `adr`, `docs`

### Examples

```bash
git commit -m "feat(parsers): add mangafire parser"
git commit -m "fix(core): handle timeout in http_session"
git commit -m "docs(adr): add ADR-006 on proxy rotation"
git commit -m "chore(deps): bump pydantic to 2.7.4"
```

### Breaking changes

```bash
git commit -m "feat(core)!: rename SiteConfig.domains to urls

BREAKING CHANGE: SiteConfig.domains has been renamed to SiteConfig.urls.
Update your sites.yaml files accordingly."
```

---

## Pull requests

### Before opening a PR

- [ ] Branch is up to date with `upstream/main`
- [ ] `pytest tests/unit -q` passes
- [ ] `ruff check src tests` passes
- [ ] `ruff format --check src tests` passes
- [ ] `mypy src` passes
- [ ] `python scripts/check_imports.py` passes
- [ ] `python scripts/validate_sites.py` passes (if `sites.yaml` modified)
- [ ] New code is documented (docstrings)
- [ ] New behavior is tested
- [ ] `CHANGELOG.md` updated (if user-facing change)

### PR template

GitHub will guide you through the template
([`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)).

### Review process

1. A maintainer reviews within **7 days** (best effort)
2. Address feedback by **adding commits** (do not force-push during review
   unless asked)
3. Once approved, a maintainer merges (squash merge preferred)
4. Your branch is deleted automatically

### What gets rejected

PRs will be closed without merge if they:

- Don't follow the Code of Conduct
- Introduce security vulnerabilities
- Add hardcoded secrets, credentials, or site URLs in Python code
- Violate architecture rules
- Break existing tests without justification
- Add untested user-facing features
- Are AI-generated and clearly unverified (we welcome AI-assisted code, but
  you must understand and test what you submit)
- Scope-creep into unrelated areas

---

## Security issues

**Do not open a public issue for security vulnerabilities.**

See [SECURITY.md](SECURITY.md) for the proper reporting process.

Examples of security issues:

- Secret leakage in source, logs, or config
- Authentication bypass
- Path traversal in download packaging
- Unsafe deserialization
- Command injection
- CSRF / XSS in the web UI

---

## Getting help

| Question | Where to ask |
|---|---|
| Bug report | [GitHub Issues](https://github.com/Voldigoad-Holdings/nexus-dl/issues) |
| Feature request | [GitHub Issues](https://github.com/Voldigoad-Holdings/nexus-dl/issues) |
| General question | [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) |
| Security | [SECURITY.md](SECURITY.md) |
| Code of Conduct | [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |

---

## Thank you

Your contributions make NexusDL better. Whether you fixed a typo or wrote a
600-line parser, thank you for your time. 🙏

---

*This file is inspired by the contribution guides of Django, FastAPI, and
Kubernetes, adapted to NexusDL's specific needs.*
```

