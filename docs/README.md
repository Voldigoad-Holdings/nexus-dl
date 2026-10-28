## 📄 `docs/README.md` 

# NexusDL — Documentation

This directory contains the **source** of the NexusDL documentation site.

- **Live site** (once deployed): `<docs URL from repository settings>`
- **Local preview**: run `just docs-serve` or `make docs-serve`
- **Built output**: `docs/_build/html/index.html`

> ℹ️ This file (`docs/README.md`) is for **contributors writing
> documentation**. For the project overview, see the
> [root README](../README.md).

---

## Table of contents

1. [Structure](#structure)
2. [Quick start](#quick-start)
3. [Writing documentation](#writing-documentation)
4. [Building locally](#building-locally)
5. [Markdown vs reStructuredText](#markdown-vs-restructuredtext)
6. [Conventions](#conventions)
7. [Adding a new page](#adding-a-new-page)
8. [Adding an ADR](#adding-an-adr)
9. [API reference](#api-reference)
10. [Translations](#translations)
11. [Deployment](#deployment)
12. [Getting help](#getting-help)

---

## Structure

```
docs/
├── README.md                       ← you are here (contributor guide)
├── API.md                          ← hand-written REST/WebSocket reference
└── source/                         ← Sphinx source root
    ├── conf.py                     ← Sphinx configuration
    ├── index.rst                   ← documentation homepage (toctree)
    ├── Makefile                    ← local `make html` helper
    ├── _static/
    │   ├── custom.css              ← custom theme overrides
    │   └── logo.png                ← header logo
    ├── _templates/
    │   └── layout.html             ← Sphinx layout override
    ├── api/
    │   ├── python.md               ← Python API (autodoc)
    │   ├── rest.md                 ← REST API (points to ../../API.md)
    │   └── websocket.md            ← WebSocket API
    ├── development/
    │   ├── architecture.md         ← hexagonal architecture
    │   ├── adding_sites.md         ← how to add a new site
    │   ├── adding_parsers.md       ← how to write a parser
    │   ├── contributing.md         ← documentation contribution guide
    │   ├── plugins.md              ← plugin system
    │   └── decisions/              ← Architecture Decision Records
    │       ├── 001-hexagonal-architecture.md
    │       ├── 002-no-cbr.md
    │       ├── 003-parser-factory-di.md
    │       ├── 004-sites-yaml-vs-env.md
    │       └── 005-multi-platform-builds.md
    ├── installation/
    │   ├── docker.md
    │   ├── linux.md
    │   ├── macos.md
    │   └── windows.md
    └── usage/
        ├── cli.md
        ├── configuration.md
        ├── formats.md
        ├── gui.md
        └── web.md
```

### Which folder for which topic?

| Folder | Content | Audience |
|---|---|---|
| `api/` | Technical API reference (Python, REST, WebSocket) | Developers integrating with NexusDL |
| `development/` | Architecture, parsers, plugins, ADRs | Contributors |
| `installation/` | Per-OS installation guides | End users |
| `usage/` | CLI, GUI, Web, configuration, formats | End users |

---

## Quick start

```bash
# 1. Install docs dependencies
pip install -e ".[docs]"
# or:
uv pip install -e ".[docs]"

# 2. Build the docs
just docs
# or:
make -C docs html

# 3. Preview in browser (Linux/macOS)
xdg-open docs/_build/html/index.html     # Linux
open docs/_build/html/index.html         # macOS
start docs\_build\html\index.html        # Windows

# 4. Or serve on a local HTTP server
just docs-serve
# → http://localhost:8001
```

---

## Writing documentation

### Sources of truth

- **`README.md` (root)** — project vitrine: features, quickstart, badges.
  **Do not duplicate this content in `docs/source/`.** Link to it instead.
- **`CONTRIBUTING.md` (root)** — development workflow: branch naming,
  commits, PRs. **Link to it** from `docs/source/development/contributing.md`.
- **`CODE_OF_CONDUCT.md` (root)** — community rules. Link to it.
- **`SECURITY.md` (root)** — security policy. Link to it.
- **`CHANGELOG.md` (root)** — release notes. Link to it.
- **`docs/API.md`** — REST + WebSocket API reference (hand-written).
  `docs/source/api/rest.md` and `websocket.md` should **link** to it, not
  duplicate it.

Everything else lives in `docs/source/`.

### Documentation principles

1. **Accuracy over completeness** — better to leave a section TODO than to
   document something wrongly.
2. **Examples over theory** — a good code sample beats three paragraphs of
   explanation.
3. **Links over duplication** — link to the source of truth; do not copy.
4. **English only** for `docs/source/` — translations are a separate effort
   (see [Translations](#translations)).
5. **Imperative mood** for instructions — "Run `just docs`", not "You should
   run `just docs`".
6. **Second person** for user docs — "You can configure…" not "The user can
   configure…".
7. **No marketing language** — factual, precise, terse.
8. **No secrets** — never paste tokens, passwords, cookies, or private URLs
   in any doc. If needed, use `**********` or `<placeholder>`.

---

## Building locally

### With `just` (recommended)

```bash
just docs          # build once → docs/_build/html/
just docs-serve    # build + serve on http://localhost:8001
just docs-clean    # remove docs/_build/
```

### With `make`

```bash
make -C docs html       # build
make -C docs clean      # clean
make -C docs linkcheck  # check external links (slow)
```

### With `sphinx-build` directly

```bash
sphinx-build -b html docs/source docs/_build/html
sphinx-build -b html -W docs/source docs/_build/html  # treat warnings as errors
sphinx-build -b linkcheck docs/source docs/_build/linkcheck
```

> ⚠️ **`-W` flag**: treat warnings as errors. Use it in CI. Locally, you can
> skip it while iterating.

---

## Markdown vs reStructuredText

Sphinx supports **both**. NexusDL uses:

| Format | Extension | Use case |
|---|---|---|
| **Markdown (MyST)** | `.md` | **Default** for new content |
| **reStructuredText** | `.rst` | Only `index.rst` (toctree) |

The **MyST parser** (`myst-parser`) is enabled in `conf.py`, which means
**Markdown is a first-class citizen** in Sphinx.

### Why Markdown?

- Contributors already know Markdown (GitHub, README).
- MyST supports Sphinx directives (`{toctree}`, `{code-block}`, `{note}`…).
- Easier to review in PRs.

### Why `index.rst` stays `.rst`?

`index.rst` uses `{toctree}` to declare the navigation tree. Keeping it in
`.rst` avoids a circular parse issue with MyST in some Sphinx versions. You
**never need to edit it** except when adding a top-level page.

---

## Conventions

### File names

- **Lowercase, kebab-case**: `adding-sites.md`, not `AddingSites.md`.
- **One topic per file**.
- **Numeric prefix** for ordered content: `001-hexagonal-architecture.md`.

### Headings

- **`#` (H1)** — page title, appears once at the top.
- **`##` (H2)** — major sections.
- **`###` (H3)** — subsections.
- **Never skip levels** (`#` → `###` is wrong).

### Code blocks

Always specify the language:

````markdown
```python
def hello() -> None:
    print("world")
```
````

Supported: `python`, `bash`, `yaml`, `toml`, `json`, `typescript`, `tsx`,
`dockerfile`, `sql`, `text`.

### Admonitions

MyST supports GitHub-style callouts:

```markdown
> **Note**
> This is a note.

> **Warning**
> This is a warning.

> **Tip**
> This is a tip.
```

Or Sphinx directives for more control:

````markdown
```{note}
This is a note.
```

```{warning}
This is a warning.
```

```{tip}
This is a tip.
```
````

### Tables

Prefer Markdown tables for simple data:

```markdown
| Column A | Column B |
|----------|----------|
| value 1  | value 2  |
```

For complex tables, use the `list-table` directive.

### Links

| Target | Syntax | Example |
|---|---|---|
| Internal doc | `[text](relative/path.md)` | `[Architecture](../development/architecture.md)` |
| Root README | `[text](../../README.md)` | `[root README](../../README.md)` |
| GitHub issue | `[#123](https://github.com/.../issues/123)` | |
| External | `[text](https://example.com)` | |

> ⚠️ **Always use relative links** for internal docs. Absolute paths break
> when the site is deployed at a sub-path.

### Images

```markdown
![Alt text](../_static/screenshot.png)
```

- Store images in `docs/source/_static/`.
- Prefer `.svg` (scalable, small) or `.png` (screenshots).
- Keep image size **below 500 KB**.

### Line length

- **100 characters** max (consistent with Python).
- Markdown tables and URLs may exceed this — disable the rule for those
  blocks if needed.

---

## Adding a new page

1. **Create the file** in the right folder:

   ```bash
   touch docs/source/usage/my-new-page.md
   ```

2. **Write the content** with a single `# H1` at the top.

3. **Register it in `index.rst`** (or in the relevant sub-toctree):

   ```rst
   .. toctree::
      :maxdepth: 2
      :caption: Usage

      usage/cli
      usage/configuration
      usage/my-new-page
   ```

   > Note: no `.md` extension in the `toctree`.

4. **Build locally** and check for warnings:

   ```bash
   sphinx-build -b html -W docs/source docs/_build/html
   ```

5. **Commit**:

   ```bash
   git add docs/source/usage/my-new-page.md docs/source/index.rst
   git commit -m "docs(usage): add my-new-page"
   git push
   ```

---

## Adding an ADR

An **Architecture Decision Record (ADR)** documents a significant technical
choice, its context, and its consequences. NexusDL uses ADRs for every
non-trivial architectural decision.

### Format

Each ADR is a Markdown file in `docs/source/development/decisions/`, named
`NNN-short-title.md` (NNN = zero-padded number).

```markdown
# ADR-NNN: Short Title

- **Status**: Proposed | Accepted | Rejected | Superseded by ADR-MMM
- **Date**: YYYY-MM-DD
- **Deciders**: names or roles
- **Supersedes**: ADR-MMM (if applicable)

## Context

What is the situation that motivates the decision? What constraints apply?
What alternatives were considered?

## Decision

What is the decision? State it clearly and concisely.

## Consequences

What are the positive and negative consequences? What becomes easier? What
becomes harder?

## Alternatives considered

What other options were evaluated? Why were they rejected?

## References

Links to related ADRs, issues, RFCs, or external resources.
```

### Workflow

1. **Create the file**:

   ```bash
   cp docs/source/development/decisions/001-hexagonal-architecture.md \
      docs/source/development/decisions/006-my-decision.md
   ```

   Edit the file. Update the number and title.

2. **Register it** in `docs/source/development/decisions/index.md` (or in the
   `toctree` of the parent page):

   ```rst
   .. toctree::
      :maxdepth: 1

      001-hexagonal-architecture
      002-no-cbr
      003-parser-factory-di
      004-sites-yaml-vs-env
      005-multi-platform-builds
      006-my-decision
   ```

3. **Reference it** from the code:

   ```python
   """..."""

   # See ADR-006: docs/source/development/decisions/006-my-decision.md
   ```

4. **Commit** with a `docs(adr):` prefix:

   ```bash
   git commit -m "docs(adr): add ADR-006 on my decision"
   ```

### When to write an ADR

Write an ADR when you:

- Change the architecture (add a layer, change a dependency rule)
- Add or remove a critical dependency
- Change a public API contract
- Decide between two non-trivial options
- Break a convention or rule documented elsewhere

**Do not** write an ADR for bug fixes, typo corrections, or routine additions.

---

## API reference

NexusDL has **three API surfaces**:

| Surface | Documented in | Maintained by |
|---|---|---|
| **Python API** | `docs/source/api/python.md` (autodoc) | Developers |
| **REST API** | `docs/API.md` (hand-written) | Backend team |
| **WebSocket API** | `docs/API.md` + `docs/source/api/websocket.md` | Backend team |

### Python API — autodoc

`docs/source/api/python.md` uses Sphinx **autodoc** to extract docstrings
from `src/nexusdl/`:

```rst
```{eval-rst}
.. automodule:: nexusdl.core.models.manga
   :members:
   :undoc-members:
   :show-inheritance:
```
```

> ⚠️ **Docstrings are the source of truth** for the Python API. If you change
> a public function, update its docstring. Autodoc will regenerate on the
> next build.

### REST API

`docs/API.md` is the source of truth. `docs/source/api/rest.md` is a short
landing page that:

1. Links to `docs/API.md` (rendered on GitHub).
2. Embeds the OpenAPI spec generated by FastAPI:

   ````markdown
   ```{openapi}
   :spec: ../../_static/openapi.json
   ```
   ````

   (Not yet enabled — requires an `openapi.json` artifact to be committed or
   fetched at build time.)

### WebSocket API

`docs/API.md` documents the WebSocket API. `docs/source/api/websocket.md`
provides a **quick start** with three client examples (Python, JavaScript,
`websocat`).

---

## Translations

NexusDL's documentation is **English-only** for now. Translations are not in
scope for `1.0.0`.

When translations become a priority, we will:

1. Adopt [`sphinx-intl`](https://www.sphinx-doc.org/en/master/usage/advanced/intl.html).
2. Use `.po` files stored in `docs/locales/<lang>/LC_MESSAGES/`.
3. Accept pull requests on a per-language basis.

**Do not** translate `docs/source/` in place. It breaks the build.

---

## Deployment

The documentation site is built and deployed automatically by GitHub Actions.

### CI workflow

`.github/workflows/docs.yml` (to be created in Phase 20):

1. Installs `.[docs]`.
2. Runs `sphinx-build -b html -W docs/source docs/_build/html`
   (warnings = errors).
3. Uploads `docs/_build/html` as an artifact.
4. Deploys to **GitHub Pages** (branch `gh-pages`) on tag push.

### Manual deployment

If you need to deploy manually:

```bash
# Build
sphinx-build -b html -W docs/source docs/_build/html

# Push to gh-pages
just docs-deploy   # (recipe to be added in Phase 18)
```

### Environment variables

The docs build respects `NO_COLOR` and `SPHINXOPTS`:

```bash
NO_COLOR=1 SPHINXOPTS="-W --keep-going" sphinx-build -b html docs/source docs/_build/html
```

---

## Getting help

| Question | Where |
|---|---|
| Bug in the docs | [Open an issue](https://github.com/Voldigoad-Holdings/nexus-dl/issues/new?labels=documentation) |
| Documentation improvement | [Open a PR](https://github.com/Voldigoad-Holdings/nexus-dl/compare) |
| Sphinx / MyST question | [Sphinx docs](https://www.sphinx-doc.org/) · [MyST docs](https://myst-parser.readthedocs.io/) |
| General question | [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) |

---

## License

This documentation is part of NexusDL and is licensed under
[GPL-3.0-or-later](../LICENSE).
