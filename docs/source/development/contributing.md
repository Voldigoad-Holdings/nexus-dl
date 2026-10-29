# Contributing

> **Source of truth**: [`CONTRIBUTING.md`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md)
>
> This page is a **documentation-specific** companion to the main guide.
> Read the full `CONTRIBUTING.md` first.

NexusDL welcomes contributions of all kinds: code, parsers, plugins, docs,
translations, design, testing, and bug reports.

```{note}
This page focuses on **contributing to the documentation itself**
(Sphinx, MyST, build, preview). For everything else — code style, tests,
commits, PRs — see the full [`CONTRIBUTING.md`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md).
```

---

## Table of contents

1. [Before you start](#before-you-start)
2. [Where to contribute](#where-to-contribute)
3. [Documentation workflow](#documentation-workflow)
4. [Writing style](#writing-style)
5. [Building and previewing](#building-and-previewing)
6. [Adding a new page](#adding-a-new-page)
7. [Adding an ADR](#adding-an-adr)
8. [Adding API reference](#adding-api-reference)
9. [Translations](#translations)
10. [Review checklist](#review-checklist)
11. [Getting help](#getting-help)

---

## Before you start

1. **Read the main guide** — [`CONTRIBUTING.md`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md)
   covers the workflow, code style, commit format, and PR process.
2. **Read the Code of Conduct** — [`CODE_OF_CONDUCT.md`](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CODE_OF_CONDUCT.md).
3. **Check open issues** — [Issues](https://github.com/Voldigoad-Holdings/nexus-dl/issues)
   and [Discussions](https://github.com/Voldigoad-Holdings/nexus-dl/discussions)
   may already cover your topic.
4. **Set up your environment** — `just install-dev` (see
   [CONTRIBUTING.md → Getting started](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md#getting-started)).

---

## Where to contribute

| Area | Where | Guide |
|---|---|---|
| 🐍 **Core code** | `src/nexusdl/core/` | [CONTRIBUTING.md](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md#coding-standards) |
| 🌐 **Parsers** | `src/nexusdl/parsers/` | [Adding a parser](adding_parsers.md) |
| 🔌 **Plugins** | `src/nexusdl/plugins/` | [Plugins](plugins.md) |
| 🖥️ **Interfaces** | `src/nexusdl/interfaces/` | [Architecture](architecture.md) |
| 📝 **Documentation** | `docs/source/` | **This page** |
| 🌍 **Translations** | `src/nexusdl/data/translations/` | [Translations](#translations) |
| 🎨 **Design** | `assets/` | Open a Discussion first |
| 🧪 **Tests** | `tests/` | [CONTRIBUTING.md → Tests](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md#tests) |

---

## Documentation workflow

### 1. Fork and clone

```bash
# Fork on GitHub, then:
git clone https://github.com/<your-username>/nexus-dl.git
cd nexus-dl

# Add upstream
git remote add upstream https://github.com/Voldigoad-Holdings/nexus-dl.git
git fetch upstream
```

### 2. Install docs dependencies

```bash
# With uv (recommended)
uv pip install -e ".[docs]"

# Or with pip
pip install -e ".[docs]"
```

This installs:

- `sphinx` — documentation generator
- `sphinx-rtd-theme` — Read the Docs theme
- `myst-parser` — Markdown support
- `sphinx-autodoc-typehints` — type hints in autodoc
- `sphinx-copybutton` — copy button on code blocks
- `sphinx-design` — tabs, cards, grids

### 3. Create a branch

Follow the branch naming from `CONTRIBUTING.md`:

```bash
git checkout -b docs/my-new-page
```

### 4. Write

- **Markdown (MyST)** for new content — `.md` files
- **reStructuredText** only for `index.rst` (toctree)
- **Google-style docstrings** in Python code (autodoc extracts them)

### 5. Build and preview

```bash
# Build once
just docs

# Or with make
make -C docs html

# Preview in browser
xdg-open docs/_build/html/index.html     # Linux
open docs/_build/html/index.html         # macOS
start docs\_build\html\index.html        # Windows

# Or serve on localhost
just docs-serve
# → http://localhost:8001
```

### 6. Check for warnings

```bash
# Warnings as errors (what CI does)
sphinx-build -b html -W --keep-going docs/source docs/_build/html
```

If a link is broken, Sphinx prints:

```
WARNING: document isn't included in any toctree: 'usage/new-page'
```

**Fix every warning before pushing.**

### 7. Commit and push

```bash
git add docs/source/usage/new-page.md docs/source/index.rst
git commit -m "docs(usage): add new-page"
git push origin docs/my-new-page
```

### 8. Open a PR

See [CONTRIBUTING.md → Pull requests](https://github.com/Voldigoad-Holdings/nexus-dl/blob/main/CONTRIBUTING.md#pull-requests).
The PR template will remind you of the checklist.

---

## Writing style

NexusDL docs follow these rules (adapted from the
[Google Developer Documentation Style Guide](https://developers.google.com/style)):

### Voice and tone

- **Second person** — "You can configure…" (not "The user can configure…")
- **Imperative** for instructions — "Run `just docs`" (not "You should run…")
- **Active voice** — "Sphinx generates the HTML" (not "The HTML is generated")
- **Present tense** — "This function returns…" (not "This function will return…")
- **No marketing** — "NexusDL downloads mangas" (not "NexusDL revolutionizes…")

### Structure

- **One `#` (H1) per page** — the page title
- **`##` (H2) for major sections**, `###` (H3) for subsections
- **Never skip heading levels** (`#` → `###` is wrong)
- **Table of contents** at the top of pages longer than ~200 lines
- **Short paragraphs** — 3–5 lines
- **Lists over prose** for enumerations
- **Tables over lists** for comparisons

### Code

- **Language tag on every code block** — ` ```python `, ` ```bash `, ` ```yaml `
- **Minimal, runnable examples** — no `...` unless the omission is obvious
- **Comments in code** — explain *why*, not *what*
- **Output shown** — if a command produces output, show it

### Links

| Target | Syntax | Example |
|---|---|---|
| Internal doc | `[text](relative/path.md)` | `[Architecture](architecture.md)` |
| Root file | `[text](../../../README.md)` | `[root README](../../../README.md)` |
| GitHub file | `[text](https://github.com/.../blob/main/...)` | |
| External | `[text](https://example.com)` | |

**Always use relative links** for internal docs. Absolute paths break when
the site is deployed at a sub-path.

### Admonitions

Use MyST admonitions for notes, warnings, tips:

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
