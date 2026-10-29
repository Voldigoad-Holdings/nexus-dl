# ADR-003: Parser Factory Dependency Injection

- **Status**: ✅ Accepted
- **Date**: 2026-10-28
- **Deciders**: Voldigoad-Holdings (lead maintainer)
- **Supersedes**: —
- **Superseded by**: —
- **Related**: [ADR-001: Hexagonal Architecture](001-hexagonal-architecture.md)

---

## Context

Per [ADR-001](001-hexagonal-architecture.md), NexusDL follows a **hexagonal
architecture** with the strict dependency rule:

> **`core/` MUST NOT import from `parsers/`, `interfaces/`, or `plugins/`.**

But `core/` needs to **create parser instances** to do its job. The
`SiteRegistry` (in `core/`) is responsible for:

1. Loading the site configuration from `data/sites.yaml`
2. **Instantiating the parser** for each site
3. Exposing parsers to the downloader

The naive approach — `core/registry/site_registry.py` importing parsers
directly — would look like:

```python
# ❌ VIOLATES ADR-001
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.fr.sushiscan import SushiScanParser
from nexusdl.parsers.en.mangadex import MangaDexParser
# ... 60+ more imports

_PARSERS = {
    "sushiscan": SushiScanParser,
    "mangadex": MangaDexParser,
    # ... 60+ entries
}

class SiteRegistry:
    def get_parser(self, site_id: str) -> BaseParser:
        return _PARSERS[site_id](self._sites[site_id], self._session)
```

This approach **violates ADR-001** and creates five concrete problems.

### Problem 1 — `core` becomes untestable without network

Testing `SiteRegistry` would require importing every parser, which itself
imports `HttpSession`, which imports `httpx`, which… At the end of the
chain, `core` tests would need network access to run.

That defeats the entire purpose of hexagonal architecture.

### Problem 2 — Circular imports

`parsers/base.py` needs `core/models/` (for `Manga`, `Chapter`, `Page`).
`core/registry/site_registry.py` needs `parsers/`. When Python resolves
imports at startup, this creates a **circular import** that manifests as
`ImportError: cannot import name 'Manga' from partially initialized module`.

Fixing it with `if TYPE_CHECKING` and lazy imports inside functions is
possible but **fragile**.

### Problem 3 — Impossible to replace parsers

Per **ADR-002**, we do not support CBR. But what if a user wants to add a
parser for a site we do not cover? Today, they would need to **edit
`core/registry/site_registry.py`** — that is, modify the core code.

We want the community to add parsers **without touching `core/`**.

### Problem 4 — Loading is hardcoded

A single `_PARSERS` dict works for 60 parsers. What about:

- **Plugins** adding parsers dynamically (`plugins/my-parser/`)?
- **Tests** replacing a parser with a fake?
- **Experiments** loading only a subset of parsers?

None of this is possible with a hardcoded dict.

### Problem 5 — Testing `core` requires real parsers

A unit test like:

```python
def test_search_returns_results():
    registry = SiteRegistry(settings)
    results = registry.search_all("one piece")
    assert len(results) > 0
```

…would **hit the network** and take 10+ seconds. Slow, flaky, and
dependent on the sites being up.

We want:

```python
def test_search_returns_results():
    registry = SiteRegistry(settings, parser_factory=FakeParserFactory())
    results = registry.search_all("one piece")
    assert len(results) > 0
```

…with **no network** and **no real parsers**.

---

## Decision

We introduce a **`ParserFactory` Protocol** in `core/`, and inject its
implementation into `SiteRegistry` at the **composition root** of each
interface.

This is **dependency inversion**: `core` depends on the **abstraction**
(the Protocol), not the **implementation** (the concrete factory).

### The Protocol (in `core/`)

```python
# src/nexusdl/core/registry/protocols.py
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from nexusdl.core.models.site import SiteConfig
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.parsers.base import BaseParser


class ParserFactory(Protocol):
    """Factory that creates parser instances from a site configuration.

    Implementations live outside `core/` (typically in `parsers/factory.py`).
    The Protocol is defined in `core/` so that `core/` can depend on the
    abstraction without importing concrete parsers.
    """

    def create(
        self,
        site: SiteConfig,
        session: HttpSession,
    ) -> BaseParser:
        """Create a parser for the given site and session.

        Args:
            site: The site configuration (from `data/sites.yaml`).
            session: The HTTP session to use for requests.

        Returns:
            A parser instance.

        Raises:
            ParserNotFoundError: If no parser is registered for `site.id`.
        """
        ...
```

**Note**: `ParserFactory` returns `BaseParser`, which is defined in
`parsers/base.py`. To avoid `core → parsers` at import time, the Protocol
uses `if TYPE_CHECKING:` for the `BaseParser` import — the **type check**
works, but the **runtime import** does not happen.

### The implementation (in `parsers/`)

```python
# src/nexusdl/parsers/factory.py
from __future__ import annotations

from typing import TYPE_CHECKING

from nexusdl.core.registry.protocols import ParserFactory
from nexusdl.parsers.exceptions import ParserNotFoundError

if TYPE_CHECKING:
    from nexusdl.core.models.site import SiteConfig
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.parsers.base import BaseParser


_REGISTRY: dict[str, type[BaseParser]] = {}


def register(site_id: str, parser_cls: type[BaseParser]) -> None:
    """Register a parser class for a site ID.

    Args:
        site_id: The site ID (e.g. ``"mangadex"``).
        parser_cls: The parser class to register.
    """
    _REGISTRY[site_id] = parser_cls


class DefaultParserFactory(ParserFactory):
    """Default implementation of the ``ParserFactory`` protocol.

    Parsers are registered via :func:`register` at import time (see
    ``parsers/__init__.py``). The factory looks them up by ``site.id``.
    """

    def create(
        self,
        site: SiteConfig,
        session: HttpSession,
    ) -> BaseParser:
        """Create a parser for the given site and session.

        Args:
            site: The site configuration.
            session: The HTTP session.

        Returns:
            A parser instance.

        Raises:
            ParserNotFoundError: If no parser is registered for ``site.id``.
        """
        parser_cls = _REGISTRY.get(site.id)
        if parser_cls is None:
            raise ParserNotFoundError(
                f"No parser registered for site '{site.id}'. "
                f"Available: {sorted(_REGISTRY)}"
            )
        return parser_cls(site, session)
```

### The `SiteRegistry` (in `core/`)

```python
# src/nexusdl/core/registry/site_registry.py
from __future__ import annotations

from typing import TYPE_CHECKING

from nexusdl.core.registry.protocols import ParserFactory

if TYPE_CHECKING:
    from nexusdl.core.models.site import SiteConfig
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.parsers.base import BaseParser
    from nexusdl.core.config import Settings


class SiteRegistry:
    """Central registry of supported sites.

    The registry does NOT import concrete parsers. It receives a
    ``ParserFactory`` at construction time (dependency inversion).
    """

    def __init__(
        self,
        settings: Settings,
        parser_factory: ParserFactory,  # ← injected, not imported
    ) -> None:
        """Initialize the registry.

        Args:
            settings: The application settings.
            parser_factory: The parser factory to use. The registry does not
                care whether it is the real ``DefaultParserFactory`` or a
                fake for testing.
        """
        self._settings = settings
        self._parser_factory = parser_factory
        self._sites: dict[str, SiteConfig] = {}
        self._load_sites()

    def _load_sites(self) -> None:
        """Load ``sites.yaml`` and validate it against the schema."""
        # ... loads from importlib.resources, validates, populates self._sites
        ...

    def get_site(self, site_id: str) -> SiteConfig:
        """Return the configuration for a site."""
        if site_id not in self._sites:
            raise KeyError(f"Unknown site: {site_id}")
        return self._sites[site_id]

    def get_parser(
        self,
        site_id: str,
        session: HttpSession,
    ) -> BaseParser:
        """Return a parser for a site."""
        site = self.get_site(site_id)
        return self._parser_factory.create(site, session)

    def list_sites(
        self,
        *,
        language: str | None = None,
        include_adult: bool = False,
    ) -> list[SiteConfig]:
        """List configured sites, optionally filtered."""
        ...
```

### The composition root (in `interfaces/`)

The **only** place where concrete implementations are wired together:

```python
# src/nexusdl/interfaces/cli/cli_entry.py
from nexusdl.core.config import load_settings
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.factory import DefaultParserFactory


def main() -> None:
    settings = load_settings()

    # Composition root: wire concrete implementations here
    registry = SiteRegistry(
        settings,
        parser_factory=DefaultParserFactory(),
    )

    # ... pass registry to the CLI commands
```

Every interface has its own composition root:

| Interface | Composition root |
|---|---|
| CLI | `nexusdl/interfaces/cli/cli_entry.py` |
| TUI | `nexusdl/interfaces/cli/app.py` |
| GUI | `nexusdl/interfaces/gui/app.py` |
| Web | `nexusdl/interfaces/web/backend/main.py` |

### Auto-registration of parsers

To avoid manual `register()` calls in 60+ parsers, we use a **registration
loop** in `parsers/__init__.py`:

```python
# src/nexusdl/parsers/__init__.py
"""Parsers for all supported sites.

Each parser module calls ``register()`` at import time to declare itself.
Importing this package registers all built-in parsers.
"""

from nexusdl.parsers import adult, en, fr, kr  # noqa: F401
from nexusdl.parsers.custom import _template  # noqa: F401
from nexusdl.parsers.factory import DefaultParserFactory, register

__all__ = ["DefaultParserFactory", "register"]
```

Each parser module:

```python
# src/nexusdl/parsers/en/mangadex.py
from nexusdl.parsers.base import BaseParser
from nexusdl.parsers.factory import register


class MangaDexParser(BaseParser):
    site_id = "mangadex"
    ...


register("mangadex", MangaDexParser)
```

This is **the only** non-local side effect allowed in a parser module: it
registers itself with the factory. This is the **plugin pattern**: `core`
does not know about `mangadex`, but `parsers/__init__.py` does.

For **plugins**, the same `register()` function is used:

```python
# src/nexusdl/plugins/my_plugin/__init__.py
from nexusdl.parsers.factory import register
from .parser import MySiteParser

register("mysite", MySiteParser)
```

The plugin is discovered by `PluginLoader` and imports itself, which
triggers `register()`.

---

## Consequences

### Positive

- ✅ **ADR-001 compliance** — `core` never imports `parsers`. Verified by
  `scripts/check_imports.py`.
- ✅ **Testability** — `core` is tested with a `FakeParserFactory`; no
  network, no parsers, no `httpx`.
- ✅ **Replaceability** — parsers can be added without touching `core`.
  A plugin can register its own parser.
- ✅ **Composition root clarity** — each interface explicitly wires
  implementations. There is no "magic" import chain.
- ✅ **No circular imports** — `core` imports nothing from `parsers` at
  runtime (only `if TYPE_CHECKING`).
- ✅ **Lazy loading** — parsers are only imported when the registry is
  constructed. We can construct a `SiteRegistry` for tests without
  importing any parser.
- ✅ **Plugin compatibility** — the same `register()` API is used for
  built-in parsers and plugins.

### Negative

- ⚠️ **More boilerplate** — a Protocol, a default factory, a registration
  function, a composition root. Mitigated by the fact that these are
  **small**, **stable**, and **shared** files (one Protocol, one factory,
  one registration function). Each parser adds only 1 line (`register(...)`).
- ⚠️ **Two-step wiring** — you must (1) import the parsers package, and
  (2) pass `DefaultParserFactory()` at the composition root. Mitigated by
  the `parsers/__init__.py` doing the import in one line.
- ⚠️ **`TYPE_CHECKING` for `BaseParser`** — the Protocol references
  `BaseParser`, which is in `parsers/`. We use `if TYPE_CHECKING:` to
  avoid the runtime import. This is **idiomatic Python** but requires
  understanding the pattern.
- ⚠️ **Debugging** — if a parser is not registered, the error is
  `ParserNotFoundError` at runtime (not `ImportError` at startup).
  Mitigated by a **clear error message** listing available parsers.

### Neutral

- 🟡 **Performance** — the registry lookup is a dict `get()` call. Negligible
  overhead.
- 🟡 **Startup time** — importing all parsers takes ~100 ms. Acceptable;
  can be optimized later with lazy per-parser loading if needed.

---

## Alternatives considered

### Alternative A — Direct imports in `core` (rejected)

**Rejected because:** violates ADR-001, causes circular imports, makes
`core` untestable without network.

### Alternative B — Global singleton with lazy imports (rejected)

**Idea:** `core/registry/site_registry.py` imports parsers inside a function
(`def _load_parsers(): from nexusdl.parsers.fr.sushiscan import ...`).

**Rejected because:**

- Still **violates ADR-001** (the import happens at runtime).
- Makes circular imports **possible** (they just happen later).
- Untestable (the imports happen inside `get_parser()`, so tests need to
  patch them).
- Loses the clarity of an explicit composition root.

### Alternative C — Entry-point plugins (Python packaging) (rejected)

**Idea:** use `importlib.metadata.entry_points(group="nexusdl.parsers")` to
discover parsers via `pyproject.toml`.

**Rejected because:**

- **Overkill** for built-in parsers (they are shipped in the same package).
- Requires each parser to be a **separate distribution**, which is not the
  case — they all live in `src/nexusdl/parsers/`.
- Adds a dependency on `importlib.metadata` (slow on first call).
- Would work for **third-party plugins**, but we already handle those via
  the plugin system (see ADR-001).
- Kept as a **future enhancement** for external parsers distributed as
  separate packages.

### Alternative D — Dependency injection framework (rejected)

**Idea:** use `dependency-injector`, `injector`, or `punq`.

**Rejected because:**

- Adds a **non-trivial dependency** for a problem solvable with a Protocol.
- Frameworks bring **magic** (decorators, wiring, config) that obscures
  the composition root.
- **Debugging** becomes harder ("why is this parser instantiated?").
- We prefer **explicit** wiring at the composition root — it is 3 lines of
  code per interface.

### Alternative E — Global registry with import side effect (chosen)

**Chosen because:**

- Keeps `core` clean.
- Minimal boilerplate (1 line per parser).
- Explicit composition root.
- Plugin-compatible (`register()` works for plugins too).
- Testable with a `FakeParserFactory`.

This is what we implemented.

---

## Example — writing a test with `FakeParserFactory`

```python
# tests/unit/core/test_site_registry.py
from __future__ import annotations

from pathlib import Path

import pytest

from nexusdl.core.config import load_settings
from nexusdl.core.models.manga import Manga
from nexusdl.core.models.search import SearchResult
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.core.session.http_session import HttpSession
from nexusdl.parsers.base import BaseParser


class FakeParser(BaseParser):
    """Fake parser that returns canned data."""

    site_id = "fake"
    language = "en"
    adult = False

    async def search(self, query, *, page=1):
        return [
            SearchResult(
                site_id="fake",
                site_name="Fake",
                manga_id="m1",
                title=f"Result for {query}",
                language="en",
                content_rating="SAFE",
                url="https://example.com/m1",
            )
        ]

    async def get_manga(self, url_or_id):
        ...

    async def get_chapters(self, manga):
        return []

    async def get_pages(self, chapter):
        return []


class FakeParserFactory:
    """Fake factory that returns a FakeParser."""

    def create(self, site, session):
        return FakeParser(site, session)


@pytest.mark.asyncio
async def test_registry_search_returns_results() -> None:
    settings = load_settings()
    registry = SiteRegistry(settings, parser_factory=FakeParserFactory())

    results = [
        r async for r in registry.search_all("one piece")
    ]

    assert len(results) > 0
    assert results[0].title == "Result for one piece"
```

**No network. No real parsers. Fast. Deterministic.** This is the payoff
of the Protocol + injection.

---

## References

- **Alistair Cockburn** — *Hexagonal Architecture*:
  <https://alistair.cockburn.us/hexagonal-architecture/>
- **Martin Fowler** — *Inversion of Control*:
  <https://martinfowler.com/bliki/InversionOfControl.html>
- **Martin Fowler** — *Dependency Injection*:
  <https://martinfowler.com/articles/injection.html>
- **PEP 544** — *Protocols: Structural subtyping (static duck typing)*:
  <https://peps.python.org/pep-0544/>
- **Related ADRs**:
  - [ADR-001: Hexagonal Architecture](001-hexagonal-architecture.md)
  - [ADR-002: No CBR Support](002-no-cbr.md)
  - [ADR-004: sites.yaml vs .env](004-sites-yaml-vs-env.md)
  - [ADR-005: Multi-platform builds](005-multi-platform-builds.md)

---

## Review and amendments

This ADR is **living** — if we later adopt an entry-point-based plugin
system for external parsers (Alternative C), we will write a new ADR
(e.g. `006-entry-point-plugins.md`) that **extends** this one.

**Amendments log:**

| Date | Change | ADR |
|---|---|---|
| 2026-10-28 | Initial version | 003 |
| — | (none yet) | — |

---

## How to verify compliance

```bash
# 1. Ensure core does not import parsers at runtime
grep -rE "^from nexusdl\.parsers|^import nexusdl\.parsers" \
  src/nexusdl/core/ \
  | grep -v "TYPE_CHECKING" \
  | grep -v "protocols.py" \
  && echo "❌ core imports parsers" \
  || echo "✅ core does not import parsers"

# 2. Ensure the Protocol is defined in core
grep -q "class ParserFactory(Protocol)" \
  src/nexusdl/core/registry/protocols.py \
  && echo "✅ ParserFactory Protocol defined in core" \
  || echo "❌ ParserFactory Protocol missing"

# 3. Ensure SiteRegistry takes parser_factory as a parameter
grep -q "parser_factory: ParserFactory" \
  src/nexusdl/core/registry/site_registry.py \
  && echo "✅ SiteRegistry injects ParserFactory" \
  || echo "❌ SiteRegistry does not inject ParserFactory"

# 4. Run the architecture checker
python scripts/check_imports.py
```

Expected output:

```
✅ core does not import parsers
✅ ParserFactory Protocol defined in core
✅ SiteRegistry injects ParserFactory
✅ Architecture OK — N file(s) checked, 0 violation.
```

---

## Summary

| Aspect | Decision |
|---|---|
| Problem | `core` needs parsers, but must not import them |
| Solution | Protocol `ParserFactory` in `core/`, injected at composition root |
| Registration | Each parser calls `register()` at import time |
| Discovery | `parsers/__init__.py` imports all parsers, triggering registration |
| Composition root | Each interface wires `DefaultParserFactory()` explicitly |
| Testability | `FakeParserFactory` for tests, no network |
| Plugin support | Same `register()` API for plugins |
| Alternative rejected | Direct imports, global singleton, entry-point plugins, DI framework |
| ADR-001 compliance | ✅ enforced by `scripts/check_imports.py` |

---

*This ADR is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../../LICENSE).*
