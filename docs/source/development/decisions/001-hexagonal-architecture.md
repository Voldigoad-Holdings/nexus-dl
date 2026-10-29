# ADR-001: Hexagonal Architecture

- **Status**: ✅ Accepted
- **Date**: 2026-10-28
- **Deciders**: Voldigoad-Holdings (lead maintainer)
- **Supersedes**: —
- **Superseded by**: —

---

## Context

NexusDL is a **multi-site, multi-interface, multi-format** downloader. It
must:

- Support **60+ sites** (French, English, Korean, adult), each with its own
  scraping logic, and receive **new parsers from the community** on a
  regular basis
- Expose **four interfaces** (CLI, TUI, GUI, Web) that must all share the
  same domain logic
- Support **six packaging formats** (FOLDER, ZIP, CBZ, CB7, PDF, CBT) that
  can be added or removed without touching the scrapers
- Be **testable** in isolation — parsers must be mockable, downloaders must
  be simulatable, packaging must be verifiable
- **Replace parsers** when a site goes down, without touching the domain
- **Add plugins** that extend behavior without modifying the core

A **naive layered architecture** (`parsers → core → interfaces`) fails these
requirements:

1. **Testability** — `core` would import `parsers` to instantiate them,
   making `core` untestable without network access.
2. **Coupling** — `core` would depend on the concrete `PlaywrightPool`,
   `HttpSession`, `ParserFactory`, etc.
3. **Replaceability** — swapping a parser would require editing `core`.
4. **Multiple interfaces** — CLI, TUI, GUI, and Web would all re-implement
   the same orchestration logic.
5. **Plugin system** — impossible without circular dependencies.

Additionally, the project's **CI matrix runs on Linux, macOS, and Windows**,
and must enforce that no developer accidentally introduces a dependency from
`core` to `parsers` — because that would break the "single source of truth"
property and make `core` untestable on CI without network.

We need an architecture that:

- **Isolates the domain** from I/O
- **Inverts the direction of dependencies** (core does not depend on adapters)
- **Makes each layer replaceable**
- **Enforces the rules mechanically** (not by convention)

---

## Decision

We adopt **hexagonal architecture** (a.k.a. **ports and adapters**,
Cockburn 2005), organized in **four layers**:

```
┌───────────────────────────────────────────────────────────┐
│               PRIMARY ADAPTERS (interfaces)               │
│     CLI · TUI · GUI · Web API · Web UI · Python API       │
└──────────────────────────┬────────────────────────────────┘
                           │ (driving)
                           ▼
┌───────────────────────────────────────────────────────────┐
│                     CORE (domain)                         │
│   models · downloader · packaging · library · registry ·  │
│   session · events · config · compliance · exceptions     │
└──────────────────────────▲────────────────────────────────┘
                           │ (driven)
                           │
┌──────────────────────────┴────────────────────────────────┐
│             SECONDARY ADAPTERS (parsers, plugins)         │
│    fr/ · en/ · kr/ · adult/ · custom/ · plugins/ · mixins │
└───────────────────────────────────────────────────────────┘
```

### Layer responsibilities

| Layer | Role | Examples |
|---|---|---|
| **`core/`** (domain) | Business logic, orchestration, domain models | `DownloadManager`, `SiteRegistry`, `Manga`, `Chapter`, `EventBus`, `Settings` |
| **`parsers/`** (secondary adapter) | Site-specific scraping logic | `MangaDexParser`, `SushiScanParser`, `NhentaiParser` |
| **`interfaces/`** (primary adapter) | User-facing entry points | `cli_entry.py`, `app.py` (TUI), `main_view.py` (GUI), `main.py` (FastAPI) |
| **`plugins/`** (secondary adapter) | Community extensions | `_example/plugin.py`, third-party plugins |
| **`data/`** (pure data) | Static data files | `sites.yaml`, `sites_schema.json`, translations, migrations |

### Dependency rules (STRICT)

The following rules are **non-negotiable** and enforced by
[`scripts/check_imports.py`](../../../../scripts/check_imports.py) in
pre-commit and CI:

| Rule | Statement | Rationale |
|---|---|---|
| **R1** | `core/` MUST NOT import from `parsers/`, `interfaces/`, or `plugins/` | Core is the domain; it must not know about adapters |
| **R2** | `parsers/` MUST NOT import from `interfaces/` or `plugins/` | Parsers are low-level; they must not know about the UI |
| **R3** | `plugins/` MUST NOT import from `interfaces/` or `parsers/` | Plugins extend the domain; they must not tie to specific UI or scrapers |
| **R4** | `interfaces/` MAY import from anywhere | Interfaces wire everything together (composition root) |
| **R5** | `data/` MUST NOT import from any layer | Data files are not code |
| **R6** | Cross-layer communication happens via **Protocols** defined in `core/` | Dependency inversion (see ADR-003) |

### Ports (Protocols) — dependency inversion

Instead of `core` importing `parsers`, we define a **Protocol** in `core/`
and let `parsers` implement it:

```python
# core/registry/protocols.py
from __future__ import annotations
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from nexusdl.core.models.site import SiteConfig
    from nexusdl.core.session.http_session import HttpSession
    from nexusdl.parsers.base import BaseParser


class ParserFactory(Protocol):
    """Factory that creates parser instances from a site configuration."""

    def create(
        self,
        site: SiteConfig,
        session: HttpSession,
    ) -> BaseParser:
        """Create a parser for the given site and session."""
        ...
```

```python
# parsers/factory.py
from nexusdl.core.registry.protocols import ParserFactory
from nexusdl.parsers.fr.sushiscan import SushiScanParser
# ... etc.

class DefaultParserFactory(ParserFactory):
    """Default implementation of the ParserFactory protocol."""

    def create(self, site: SiteConfig, session: HttpSession) -> BaseParser:
        parser_cls = _REGISTRY[site.id]
        return parser_cls(site, session)
```

```python
# core/registry/site_registry.py
class SiteRegistry:
    def __init__(
        self,
        settings: Settings,
        parser_factory: ParserFactory,  # ← injected, not imported
    ) -> None:
        ...
```

```python
# interfaces/cli/cli_entry.py (composition root)
from nexusdl.core.registry.site_registry import SiteRegistry
from nexusdl.parsers.factory import DefaultParserFactory

registry = SiteRegistry(settings, parser_factory=DefaultParserFactory())
```

This is **dependency inversion in practice**: `core` depends only on the
**abstraction** (`ParserFactory`), not the implementation.

See **ADR-003** for a deeper dive into the `ParserFactory` decision.

### Why `data/` is not a layer

`data/` contains **pure data files** (`sites.yaml`, translations, SQL
migrations). It has no Python code except `__init__.py` (empty marker) and
no imports. Its content is **read** by `core/` (via `importlib.resources`)
but never `import`ed as Python modules.

The rule R5 ensures no one accidentally adds Python logic to `data/`.

### Enforcement

The rules are enforced **mechanically**, not by convention:

```bash
# scripts/check_imports.py
$ python scripts/check_imports.py
✅ Architecture OK — 312 file(s) checked, 0 violation.
```

The script is:

- Run in **pre-commit** (blocks the commit if violated)
- Run in **CI** (`.github/workflows/lint.yml`)
- Run in **`just check`** / **`make check`**
- Run in **`scripts/build.py`** before building

If a rule is violated:

```bash
$ python scripts/check_imports.py
❌ 1 violation(s) detected:

  • src/nexusdl/core/downloader/manager.py:
    layer 'core' cannot import 'nexusdl.parsers.mangadex'
```

### Testability

Because `core` does not import concrete adapters, it can be tested **in
isolation** with fake implementations:

```python
# tests/unit/core/test_site_registry.py
from nexusdl.core.registry.site_registry import SiteRegistry


class FakeParserFactory:
    """Fake factory that returns a stub parser."""

    def create(self, site, session):
        return FakeParser(site, session)


def test_registry_lists_sites(tmp_path):
    settings = load_settings()
    registry = SiteRegistry(settings, parser_factory=FakeParserFactory())

    sites = registry.list_sites()
    assert len(sites) > 0
```

No network, no Playwright, no filesystem — just pure logic.

### Composition root

The **only place** where concrete implementations are wired together is the
**composition root** of each interface:

| Interface | Composition root |
|---|---|
| CLI | `nexusdl/interfaces/cli/cli_entry.py` |
| TUI | `nexusdl/interfaces/cli/app.py` |
| GUI | `nexusdl/interfaces/gui/app.py` |
| Web | `nexusdl/interfaces/web/backend/main.py` |
| Python API | user's own code (documented) |

Each composition root instantiates the concrete `DefaultParserFactory`,
`HttpSession`, `DownloadManager`, etc., and injects them into the interfaces.

---

## Consequences

### Positive

- ✅ **Testability** — `core` is tested with fakes; parsers are tested with
  mocks (via `respx`); interfaces are tested with a real `core` but mocked
  parsers.
- ✅ **Replaceability** — a parser can be swapped without touching `core`.
  A new interface can be added without touching `parsers`.
- ✅ **Multiple interfaces** — CLI, TUI, GUI, and Web all share the same
  orchestration code. No duplication.
- ✅ **Plugin system** — plugins can extend behavior via `core` Protocols
  without knowing about parsers or interfaces.
- ✅ **CI enforcement** — `scripts/check_imports.py` prevents regressions.
- ✅ **Conceptual clarity** — new contributors immediately understand where
  to add code.
- ✅ **Onboarding** — a contributor can work on `parsers/` without touching
  `core/` and vice versa.

### Negative

- ⚠️ **More files** — the hexagonal layout requires more files than a flat
  `models.py` + `scraper.py` + `cli.py`. Mitigated by the fact that each
  file is small and focused.
- ⚠️ **More boilerplate** — Protocols, factories, and composition roots
  add code that would not exist in a naive design. Mitigated by the
  testability and replaceability gains.
- ⚠️ **Learning curve** — contributors unfamiliar with hexagonal
  architecture may need a few minutes to understand the layout. Mitigated
  by `docs/source/development/architecture.md` and this ADR.
- ⚠️ **Strictness can be annoying** — a "quick fix" that would import
  `parsers` from `core` is blocked. Mitigated by documenting the correct
  pattern (Protocol + injection) in `CONTRIBUTING.md`.

### Neutral

- 🟡 **Performance impact**: none measurable. Protocol dispatch is
  negligibly slower than direct import.
- 🟡 **Runtime overhead**: none. Imports are resolved at startup, not at
  runtime.

---

## Alternatives considered

### Alternative A — Flat layered architecture

```
parsers → core → interfaces
```

**Rejected because:**

- `core` would import `parsers` (to instantiate them), making `core`
  untestable without network.
- A parser swap would require editing `core`.
- Multiple interfaces would duplicate orchestration logic.
- No clean plugin system.

### Alternative B — Event-driven microkernel

```
core (microkernel) ← plugins (all parsing + UI)
```

**Rejected because:**

- Too complex for a solo/small-team project.
- Plugin discovery and versioning would be a huge effort.
- No built-in CLI, TUI, GUI, Web — everything would be a plugin, which is
  overkill.
- Slower to bootstrap.

We keep the **concept** of plugins, but they extend the core rather than
provide it.

### Alternative C — MVC (Model-View-Controller)

```
models → controllers → views
```

**Rejected because:**

- Designed for **web apps with a single interface**, not multi-interface
  tools.
- Does not invert dependencies by itself.
- Does not cleanly separate business logic from I/O.
- The "controller" concept blurs into "view" or "model" in a downloader.

### Alternative D — Clean Architecture (Uncle Bob)

Very similar to hexagonal, with more named layers
(entities, use cases, interface adapters, frameworks).

**Rejected because:**

- **More layers than needed** — `use cases` and `interface adapters` would
  overlap significantly in NexusDL.
- **Naming fatigue** — contributors would ask "is this a use case or an
  interface adapter?" for simple classes.
- **Same guarantees** as hexagonal for our needs.

Hexagonal provides 90% of the benefit with 60% of the ceremony.

### Alternative E — Do nothing (laissez-faire)

Let contributors import whatever they want.

**Rejected because:**

- Inevitably leads to circular imports within 3 months.
- `core` would become untestable.
- The plugin system would be impossible.
- Parser swaps would require core edits.

---

## References

- **Alistair Cockburn**, *Hexagonal Architecture* (2005):
  <https://alistair.cockburn.us/hexagonal-architecture/>
- **Robert C. Martin**, *Clean Architecture* (2017) — for comparison
- **Vaughn Vernon**, *Implementing Domain-Driven Design* (2013)
- **Mark Seemann**, *Dependency Injection Principles, Practices, and
  Patterns* (2019)
- **ADR-003**: Parser Factory Dependency Injection
- **ADR-004**: `sites.yaml` vs `.env` separation
- **`docs/source/development/architecture.md`** — the living reference
- **`scripts/check_imports.py`** — the enforcement script

---

## Review and amendments

This ADR is **living** — if a rule proves too strict, we amend it via a
**new ADR** (e.g. `006-relax-parsers-core-imports.md`), not by editing this
one. This preserves the historical record.

**Amendments log:**

| Date | Change | ADR |
|---|---|---|
| 2026-10-28 | Initial version | 001 |
| — | (none yet) | — |

---

## How to verify compliance

Before opening a PR, run:

```bash
python scripts/check_imports.py
```

Expected output:

```
✅ Architecture OK — N file(s) checked, 0 violation.
```

If it fails, read the error message and apply the fix:

- **Moving logic** from `parsers` to `core` (rare)
- **Defining a Protocol** in `core` (preferred)
- **Injecting** the dependency at the composition root

---

## Summary

| Aspect | Decision |
|---|---|
| Architecture style | Hexagonal (ports and adapters) |
| Layers | `core`, `parsers`, `interfaces`, `plugins`, `data` |
| Dependency direction | `interfaces` → `core` ← `parsers`/`plugins` |
| Cross-layer communication | Protocols in `core`, injected at composition root |
| Enforcement | `scripts/check_imports.py` (pre-commit + CI) |
| Testability | `core` testable with fakes; parsers testable with mocks |
| Alternative rejected | Flat layered, microkernel, MVC, Clean Architecture, laissez-faire |

---

*This ADR is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../../LICENSE).*
