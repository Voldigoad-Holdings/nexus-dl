## 📄 `docs/README.md` — complet (v2.0)


# NexusDL — Documentation Contributor Guide

> **Audience**: contributors who write, edit, or review **documentation**.
>
> **This file is NOT the project README.** For the project overview (features,
> quickstart, badges), see [`../README.md`](../README.md).
>
> **This file is NOT a user manual.** For usage, see the rendered docs at
> [the documentation site](https://github.com/Voldigoad-Holdings/nexus-dl/tree/main/docs)
> or read the sources under [`source/`](source/).

NexusDL uses [Sphinx](https://www.sphinx-doc.org/) with the
[MyST parser](https://myst-parser.readthedocs.io/) to build its documentation
from Markdown files. This page explains how to contribute.

```{note}
**Rendered docs**: once CI is set up, the docs are published to GitHub Pages
at `<SPHINX_HTML_BASEURL>` (configured via `NEXUSDL_PUBLIC_URL`). Until then,
build them locally with `just docs`.
```

---

## Table of contents

1. [Directory structure](#directory-structure)
2. [Quick start](#quick-start)
3. [Writing documentation](#writing-documentation)
4. [Building locally](#building-locally)
5. [Markdown vs reStructuredText](#markdown-vs-restructuredtext)
6. [Conventions](#conventions)
7. [Adding a page](#adding-a-page)
8. [Adding an ADR](#adding-an-adr)
9. [API reference](#api-reference)
10. [Deployment](#deployment)
11. [Getting help](#getting-help)

---

## Directory structure

```
docs/
├── README.md                              ← you are here (contributor guide)
├── API.md                                 ← REST + WebSocket reference (GitHub-rendered)
└── source/                                ← Sphinx source root
    ├── conf.py                            ← Sphinx configuration
    ├── index.rst                          ← docs homepage + toctrees
    ├── Makefile                           ← Sphinx build helper
    │
    ├── _static/
    │   ├── custom.css                     ← theme overrides
    │   └── logo.png                       ← header logo (200×200)
    ├── _templates/
    │   └── layout.html                    ← Sphinx layout override
    │
    ├── api/
    │   ├── python.md                      ← Python API (autodoc)
    │   ├── rest.md                        ← REST API (links to ../../API.md)
    │   └── websocket.md                   ← WebSocket API (links to ../../API.md)
    │
    ├── development/
    │   ├── architecture.md                ← hexagonal architecture
    │   ├── adding_sites.md                ← add a site (sites.yaml)
    │   ├── adding_parsers.md              ← implement a parser (Python)
    │   ├── contributing.md                ← docs-specific contributor guide
    │   ├── plugins.md                     ← plugin system
    │   └── decisions/                     ← Architecture Decision Records
    │       ├── index.md                   ← ADR index + template
    │       ├── 001-hexagonal-architecture.md
    │       ├── 002-no-cbr.md
    │       ├── 003-parser-factory-di.md
    │       ├── 004-sites-yaml-vs-env.md
    │       └── 005-multi-platform-builds.md
    │
    ├── installation/
    │   ├── docker.md
    │   ├── linux.md
    │   ├── macos.md
    │   └── windows.md
    │
    └── usage/
        ├── cli.md
        ├── configuration.md
        ├── formats.md
        ├── gui.md
        └── web.md
```

### Which folder for which topic?

| Folder | Purpose | Audience |
|---|---|---|
| `source/api/` | Technical API reference (Python, REST, WebSocket) | Developers integrating with NexusDL |
| `source/development/` | Architecture, parsers, plugins, ADRs | Contributors |
| `source/installation/` | Per-OS install guides | End users |
| `source/usage/` | CLI, GUI, Web, configuration, formats | End users |

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

# 3. Preview in browser
xdg-open docs/_build/html/index.html     # Linux
open docs/_build/html/index.html         # macOS
start docs\_build\html\index.html        # Windows

# 4. Or serve on a local HTTP server
just docs-serve
# → http://localhost:8001
```

### Docs dependencies (`[docs]` extra)

The `[docs]` extra in `pyproject.toml` installs:

| Package | Purpose |
|---|---|
| `sphinx>=7.3` | Documentation generator |
| `sphinx-rtd-theme>=2.0` | Read the Docs theme |
| `myst-parser>=3.0` | Markdown support |
| `sphinx-autodoc-typehints>=2.0` | Type hints in autodoc |
| `sphinx-copybutton>=0.5` | Copy button on code blocks |
| `sphinx-design>=0.6` | Cards, grids, tabs |
| `sphinx-sitemap>=2.6` | `sitemap.xml` for SEO |
| `sphinx-autobuild>=2024.4` | Live reload |

---

## Writing documentation

### Sources of truth

**Do not duplicate content across files.** Each piece of information has
**one** authoritative source:

| Content | Source of truth | Rendered by |
|---|---|---|
| **Project overview** (features, badges) | [`../README.md`](../README.md) | GitHub + PyPI |
| **Contributing workflow** | [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | GitHub |
| **Code of Conduct** | [`../CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) | GitHub |
| **Security policy** | [`../SECURITY.md`](../SECURITY.md) | GitHub |
| **Changelog** | [`../CHANGELOG.md`](../CHANGELOG.md) | GitHub |
| **REST + WebSocket API** | [`API.md`](API.md) | GitHub |
| **Architecture decisions** | [`source/development/decisions/`](source/development/decisions/) | Sphinx |
| **Installation guides** | [`source/installation/`](source/installation/) | Sphinx |
| **Usage guides** | [`source/usage/`](source/usage/) | Sphinx |
| **Python API** | Docstrings in `src/nexusdl/` | Sphinx autodoc |
| **Contributing to docs** | **This file** | GitHub |

### Principles

1. **Accuracy over completeness** — better a TODO than a lie.
2. **Examples over theory** — a good code sample beats three paragraphs.
3. **Link over duplicate** — link to the source of truth; do not copy.
4. **English only** — translations are out of scope for `1.0.0`.
5. **Imperative mood** — "Run `just docs`", not "You should run `just docs`".
6. **Second person** — "You can configure…", not "The user can configure…".
7. **No marketing** — factual, precise, terse.
8. **No secrets** — never paste tokens, cookies, or private URLs.

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
make -C docs strict     # build with -W (warnings = errors)
make -C docs serve      # build + serve
make -C docs linkcheck  # check external links (slow)
make -C docs clean      # clean
```

### With `sphinx-build` directly

```bash
sphinx-build -b html docs/source docs/_build/html
sphinx-build -b html -W --keep-going docs/source docs/_build/html   # strict
sphinx-build -b linkcheck docs/source docs/_build/linkcheck
```

> ⚠️ **`-W`** treats warnings as errors. Use it locally before pushing —
> CI runs with `-W --keep-going`.

### Common warnings

| Warning | Cause | Fix |
|---|---|---|
| `document isn't included in any toctree` | Page not registered in `index.rst` | Add to the relevant toctree |
| `toctree contains reference to nonexisting document` | Typo in toctree | Check the file exists |
| `Unknown directive type "note"` | MyST not enabled | Enable `myst_parser` in `conf.py` |
| `Duplicate explicit target name` | Two `_label:` with same name | Rename one |
| `Title underline too short` | RST `====` shorter than title | Extend underline |

---

## Markdown vs reStructuredText

Sphinx supports **both**. NexusDL uses:

| Format | Extension | Use case |
|---|---|---|
| **Markdown (MyST)** | `.md` | **Default** for new content |
| **reStructuredText** | `.rst` | Only `index.rst` (toctree) |

The **MyST parser** is enabled in `conf.py`, so **Markdown is a
first-class citizen**. Contributors already know Markdown (GitHub,
README), and MyST supports all Sphinx directives (`{toctree}`, `{note}`,
`{eval-rst}`).

### Why `index.rst` stays `.rst`?

`index.rst` uses `{toctree}` to declare the navigation tree. Keeping it in
`.rst` avoids a circular parse issue with MyST in some Sphinx versions.
You **never need to edit it** except when adding a top-level page.

---

## Conventions

### File names

- **Lowercase, kebab-case**: `adding-sites.md`, not `AddingSites.md`.
- **One topic per file**.
- **Numeric prefix** for ordered content: `001-hexagonal-architecture.md`.

### Headings

- **`#` (H1)** — page title, appears **once** at the top.
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

MyST supports Sphinx directives:

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

```{danger}
This is a danger.
```
````

Or GitHub-style blockquotes for simple cases:

```markdown
> **Note**
> This is a note.
```

### Tables

Prefer Markdown tables for simple data. For complex tables, use the
`list-table` directive.

### Links

| Target | Syntax | Example |
|---|---|---|
| Internal doc | `[text](relative/path.md)` | `[Architecture](../development/architecture.md)` |
| Root README | `[text](../../README.md)` | `[root README](../../README.md)` |
| GitHub file | `[text](https://github.com/.../blob/main/...)` | |
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
- **Alt text is mandatory** (accessibility).

### Line length

- **100 characters** max (same as Python).
- Markdown tables and URLs may exceed — that's fine.

### Things to avoid

- ❌ "Simply", "just", "obviously" — condescending
- ❌ "Easy", "quick" — subjective
- ❌ Emojis in body text (OK in tables/headings)
- ❌ Screenshots of text (use code blocks)
- ❌ "Click here" — use descriptive link text
- ❌ Duplicating content from `README.md` or `CONTRIBUTING.md` — link instead

---

## Adding a page

1. **Create the file** in the right folder:

   ```bash
   touch docs/source/usage/my-new-page.md
   ```

2. **Write the content** with a single `# H1`.

3. **Register it in `index.rst`** (or in the relevant sub-toctree):

   ```rst
   .. toctree::
      :maxdepth: 2
      :caption: Usage

      usage/cli
      usage/configuration
      usage/my-new-page
   ```

   > **Note**: no `.md` extension in the toctree.

4. **Build locally** and check for warnings:

   ```bash
   sphinx-build -b html -W --keep-going docs/source docs/_build/html
   ```

5. **Commit**:

   ```bash
   git add docs/source/usage/my-new-page.md docs/source/index.rst
   git commit -m "docs(usage): add my-new-page"
   git push
   ```

---

## Adding an ADR

**Architecture Decision Records** document significant technical choices.

### Format

Each ADR is a Markdown file in `docs/source/development/decisions/`, named
`NNN-short-title.md`.

```markdown
# ADR-NNN: Short Title

- **Status**: Proposed | ✅ Accepted | ❌ Rejected | ⬆️ Superseded by ADR-MMM
- **Date**: YYYY-MM-DD
- **Deciders**: names or roles
- **Supersedes**: — (or ADR-MMM)
- **Superseded by**: — (or ADR-MMM)

## Context
What problem? What constraints?

## Decision
What did we decide?

## Consequences
What becomes easier? What becomes harder?

## Alternatives considered
What else was evaluated? Why rejected?

## References
Related ADRs, issues, RFCs, external docs.
```

### Workflow

1. **Copy an existing ADR**:

   ```bash
   cd docs/source/development/decisions/
   cp 005-multi-platform-builds.md 006-my-decision.md
   ```

2. **Update the metadata** (number, title, date, status `Proposed`).

3. **Register it** in `docs/source/development/decisions/index.md`:
   - Add to the toctree.
   - Add to the summary table.

4. **Reference it** from the code:

   ```python
   # See ADR-006: docs/source/development/decisions/006-my-decision.md
   ```

5. **Commit** with `docs(adr):` prefix:

   ```bash
   git commit -m "docs(adr): add ADR-006 on my decision"
   ```

6. **Once merged**, update the status to `✅ Accepted` in a follow-up PR.

### When to write an ADR

- ✅ Architecture change
- ✅ Critical dependency added or removed
- ✅ Public API contract change
- ✅ Non-trivial choice between options
- ✅ Breaking a documented convention
- ❌ Bug fix, typo, routine addition

---

## API reference

NexusDL has **three API surfaces**:

| Surface | Source of truth | Rendered by |
|---|---|---|
| **Python API** | Docstrings in `src/nexusdl/` | `source/api/python.md` (autodoc) |
| **REST API** | [`API.md`](API.md) | `source/api/rest.md` (links to it) |
| **WebSocket API** | [`API.md#websocket-api`](API.md#websocket-api) | `source/api/websocket.md` (links to it) |

### Python API — autodoc

`source/api/python.md` uses Sphinx **autodoc**:

```rst
```{eval-rst}
.. automodule:: nexusdl.core.models.manga
   :members:
   :undoc-members:
   :show-inheritance:
```
```

**Docstrings are the source of truth** for the Python API. Update the
docstring, not the Markdown file.

**Requirements**:

- `from __future__ import annotations` at the top of every Python file.
- Google-style docstrings on all public functions and classes.
- Type hints on all parameters and return values.

### REST and WebSocket API

`docs/API.md` is the source of truth. The Sphinx pages (`source/api/rest.md`
and `source/api/websocket.md`) are **navigation hubs** — they link to
`API.md` and do not duplicate it.

If you change an endpoint:

1. Update `API.md` (schema, examples, error codes).
2. Update `source/api/rest.md` if the endpoint overview table changed.
3. Update `source/api/websocket.md` if message types changed.

---

## Deployment

The documentation is built and deployed automatically by GitHub Actions.

### CI workflow (`.github/workflows/docs.yml`)

1. Installs `.[docs]`.
2. Runs `sphinx-build -b html -W --keep-going docs/source docs/_build/html`.
3. Uploads `docs/_build/html` as an artifact.
4. Deploys to **GitHub Pages** (branch `gh-pages`) on tag push.

### Manual deployment

```bash
# Build
just docs

# Or with strict warnings
sphinx-build -b html -W --keep-going docs/source docs/_build/html
```

### Environment variables

The docs build respects:

- `SPHINX_HTML_BASEURL` — canonical URL for `sitemap.xml` (default:
  `https://nexusdl.example.com/`)
- `SPHINXOPTS` — extra Sphinx options
- `NO_COLOR` — disable ANSI colors in build output

---

## Getting help

| Question | Where |
|---|---|
| Bug in the docs | [Open an issue](https://github.com/Voldigoad-Holdings/nexus-dl/issues/new?labels=documentation) |
| Documentation improvement | [Open a PR](https://github.com/Voldigoad-Holdings/nexus-dl/compare) |
| Sphinx question | [Sphinx docs](https://www.sphinx-doc.org/) |
| MyST question | [MyST docs](https://myst-parser.readthedocs.io/) |
| General question | [GitHub Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions) |

---

## License

This documentation is part of NexusDL and is licensed under
[GPL-3.0-or-later](../LICENSE).
